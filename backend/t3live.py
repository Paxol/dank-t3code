#!/usr/bin/env python3
"""Live connection to the local T3 Code server. The Limits part is ported from
T3 Code Bar's LimitsLiveConnection (macOS); threads and usage follow the same
RPCs T3 Code's own sidebar and Usage page use.

    - Limits: the `subscribeServerConfig` stream, with provider snapshots and
      CLI Proxy pooled accounts (`orchestration:read`).
    - Threads: the `orchestration.subscribeShell` stream of thread shells
      (`orchestration:read`).
    - Cost and tokens: `server.getUsageSummary`, the server's priced usage
      buckets (`diagnostics:read`, optional).

    1. The user creates a pairing code in T3 Code with "View environment"
       (`orchestration:read`) and, for live cost and tokens, "View diagnostics
       and usage" (`diagnostics:read`), and gives it to the widget.
    2. The code is exchanged at loopback `/oauth/token` for a DPoP-bound access
       token with at most those two scopes. The pairing code is never stored.
    3. A WebSocket ticket is issued with that token, the socket opens at
       `ws://127.0.0.1:<port>/ws`, and the widget subscribes to the server config
       and the thread shells.
    4. Only "Remember" stores the widget's own token and key, in the desktop
       keyring (secret-tool). Nothing else is persisted.

The process reads JSON commands on stdin and prints one JSON line per change:

  {"kind": "limits", "view": {...}}         the rendered Limits tab
  {"kind": "inbox", "snapshot": {...}}      the thread inbox, or null while not live
  {"kind": "usage", "period": 7, ...}       a reply to a usage command

Without a connection, or during an outage, the Limits tab falls back to T3 Code's
saved provider files, as the original app does, and the widget falls back to its
own readers of `statev2.sqlite` and the usage cache.

Commands:
  {"cmd": "pair", "port": 3773, "code": "...", "remember": false}
  {"cmd": "reconnectSaved", "port": 3773}
  {"cmd": "disconnect"}
  {"cmd": "clock", "twentyFour": true}
  {"cmd": "usage", "period": 30, "seq": 1}
  {"cmd": "refresh"}
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import queue
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import t3bar  # noqa: E402

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    HAVE_CRYPTO = True
except ImportError:  # pragma: no cover - reported to the user instead
    HAVE_CRYPTO = False

REQUEST_ID = 0  # subscribeServerConfig
SHELL_ID = 1  # orchestration.subscribeShell
FIRST_USAGE_ID = 2  # each server.getUsageSummary call takes the next id
SUBSCRIBE = {
    "_tag": "Request", "id": REQUEST_ID, "tag": "subscribeServerConfig",
    "payload": {"usageLimitSources": True, "workspaceSnapshotsOnDemand": True}, "headers": [],
}
ACK = {"_tag": "Ack", "requestId": REQUEST_ID}
INTERRUPT = {"_tag": "Interrupt", "requestId": REQUEST_ID, "interruptors": []}
PING = {"_tag": "Ping"}
READ_SCOPE = "orchestration:read"
USAGE_SCOPE = "diagnostics:read"
REQUESTED_SCOPES = [READ_SCOPE, USAGE_SCOPE]
MAX_MESSAGE = 32 * 1024 * 1024  # a thread shell snapshot lists every active thread
RETRY_SECONDS = 3
USAGE_TIMEOUT_SECONDS = 60
LIVE_FOOTNOTE = ("Live from the local T3 Code server, as T3 Code's Usage page shows it. "
                 "Read-only; this widget never contacts a provider.")
STATUS_TEXT = {
    "cacheOnly": "Not connected. Showing T3 Code's saved provider checks.",
    "connecting": "Connecting…",
    "live": "Connected",
    "paused": "Connection paused.",
    "retrying": "Disconnected. Reconnecting…",
}


class LiveError(Exception):
    """A failure that stops the connection until the user acts. Transport errors retry instead."""

    MESSAGES = {
        "destination": "Enter a local T3 Code port from 1 to 65535.",
        "authorization": "Pairing failed. Create a new read-only code.",
        "protocolMismatch": "This T3 Code version returned an unsupported Limits stream.",
        "expired": "The access token expired. Pair again in T3 Code.",
        "storage": "Could not save the access token in the keyring. Connect again without Remember.",
        "crypto": "The live connection needs the Python cryptography package (python-cryptography).",
    }

    def __init__(self, code: str):
        super().__init__(self.MESSAGES[code])
        self.code = code


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def compact(value) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def valid_port(port) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise LiveError("destination")
    return port


def valid_scopes(scopes) -> list[str]:
    """The read scope, optionally with usage. Anything that can change the environment is refused."""
    if not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes):
        raise LiveError("authorization")
    if READ_SCOPE not in scopes or len(set(scopes)) != len(scopes) or not set(scopes) <= set(REQUESTED_SCOPES):
        raise LiveError("authorization")
    return [scope for scope in REQUESTED_SCOPES if scope in scopes]


# ---------------------------------------------------------------------------
# DPoP (LimitsDPoP.swift): a proof binds the access token to the widget's key.
# It does not authenticate the server.
# ---------------------------------------------------------------------------


class DPoP:
    def __init__(self, raw: bytes | None = None):
        if not HAVE_CRYPTO:
            raise LiveError("crypto")
        if raw is None:
            self.key = ec.generate_private_key(ec.SECP256R1())
        else:
            if len(raw) != 32:
                raise LiveError("authorization")
            self.key = ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())

    def raw(self) -> bytes:
        return self.key.private_numbers().private_value.to_bytes(32, "big")

    def proof(self, method: str, url: str, token: str | None = None) -> str:
        numbers = self.key.public_key().public_numbers()
        header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": {
            "kty": "EC", "crv": "P-256",
            "x": b64url(numbers.x.to_bytes(32, "big")), "y": b64url(numbers.y.to_bytes(32, "big"))}}
        target = url.split("#", 1)[0].split("?", 1)[0]
        payload = {"htm": method, "htu": target, "jti": str(uuid.uuid4()).upper(), "iat": int(time.time())}
        if token:
            payload["ath"] = b64url(hashlib.sha256(token.encode("utf-8")).digest())
        body = b64url(compact(header)) + "." + b64url(compact(payload))
        r, s = decode_dss_signature(self.key.sign(body.encode("ascii"), ec.ECDSA(hashes.SHA256())))
        return body + "." + b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


# ---------------------------------------------------------------------------
# Credential store: the desktop keyring through secret-tool. Only the widget's
# own exchanged token, its port, expiry, scopes and private key are stored.
# ---------------------------------------------------------------------------

KEYRING_ATTRIBUTES = ["service", "t3codebar.limits", "account", "local-read-only"]


class KeyringStore:
    def __init__(self):
        self.tool = shutil.which("secret-tool")

    def load(self) -> dict | None:
        if not self.tool:
            return None
        try:
            result = subprocess.run([self.tool, "lookup", *KEYRING_ATTRIBUTES], capture_output=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise LiveError("storage") from error
        if result.returncode != 0 or not result.stdout.strip():
            return None
        try:
            data = json.loads(result.stdout)
            # Tokens saved before usage was live carry only the read scope.
            return {
                "port": valid_port(data["port"]), "token": str(data["token"]),
                "expiresAt": float(data["expiresAt"]), "privateKey": base64.b64decode(data["privateKey"]),
                "scopes": valid_scopes(data.get("scopes", [READ_SCOPE])),
            }
        except (ValueError, KeyError, TypeError, LiveError) as error:
            raise LiveError("storage") from error

    def save(self, credential: dict) -> None:
        if not self.tool:
            raise LiveError("storage")
        secret = json.dumps({
            "port": credential["port"], "token": credential["token"], "expiresAt": credential["expiresAt"],
            "privateKey": base64.b64encode(credential["privateKey"]).decode("ascii"),
            "scopes": credential.get("scopes", [READ_SCOPE]),
        })
        try:
            result = subprocess.run([self.tool, "store", "--label=T3 Code Bar Limits", *KEYRING_ATTRIBUTES],
                                    input=secret.encode("utf-8"), capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise LiveError("storage") from error
        if result.returncode != 0:
            raise LiveError("storage")

    def delete(self) -> None:
        if not self.tool:
            return
        try:
            subprocess.run([self.tool, "clear", *KEYRING_ATTRIBUTES], capture_output=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise LiveError("storage") from error


# ---------------------------------------------------------------------------
# Loopback transport. Only 127.0.0.1 is ever contacted, with no proxies,
# cookies or redirects.
# ---------------------------------------------------------------------------


def http_post(port: int, path: str, headers: dict, body: bytes = b"") -> dict:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        conn.request("POST", path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read(1_048_577)
        status = response.status
    finally:
        conn.close()
    if status != 200:
        if 300 <= status <= 499:
            raise LiveError("authorization")
        raise OSError(f"HTTP {status}")
    if len(data) > 1_048_576:
        raise LiveError("protocolMismatch")
    try:
        value = json.loads(data)
    except ValueError as error:
        raise LiveError("protocolMismatch") from error
    if not isinstance(value, dict):
        raise LiveError("protocolMismatch")
    return value


class WebSocket:
    """A minimal RFC 6455 client for one loopback text stream."""

    GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, port: int, target: str):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.send_lock = threading.Lock()
        self.buffer = bytearray()
        self.fragments: bytearray | None = None
        try:
            self._handshake(port, target)
        except BaseException:
            self.close()
            raise

    def _handshake(self, port: int, target: str) -> None:
        key = base64.b64encode(os.urandom(16))
        request = (
            f"GET {target} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key.decode()}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("closed during handshake")
            data += chunk
            if len(data) > 65536:
                raise LiveError("protocolMismatch")
        head, _, rest = bytes(data).partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        if status in (401, 403):
            raise LiveError("authorization")
        if status != 101:
            raise OSError(f"WebSocket upgrade failed ({status})")
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1(key + self.GUID).digest()).decode()
        if headers.get("sec-websocket-accept") != expected:
            raise LiveError("protocolMismatch")
        self.buffer += rest

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self.send_lock:
            self.sock.sendall(bytes(header) + mask + masked)

    def send_json(self, value) -> None:
        self._send_frame(0x1, json.dumps(value, separators=(",", ":")).encode("utf-8"))

    def _parse(self) -> str | None:
        while True:
            buf = self.buffer
            if len(buf) < 2:
                return None
            fin, opcode = buf[0] & 0x80, buf[0] & 0x0F
            masked, length = buf[1] & 0x80, buf[1] & 0x7F
            offset = 2
            if length == 126:
                if len(buf) < 4:
                    return None
                length = struct.unpack("!H", buf[2:4])[0]
                offset = 4
            elif length == 127:
                if len(buf) < 10:
                    return None
                length = struct.unpack("!Q", buf[2:10])[0]
                offset = 10
            if length > MAX_MESSAGE:
                raise LiveError("protocolMismatch")
            mask = b""
            if masked:
                if len(buf) < offset + 4:
                    return None
                mask = bytes(buf[offset:offset + 4])
                offset += 4
            if len(buf) < offset + length:
                return None
            payload = bytes(buf[offset:offset + length])
            del buf[:offset + length]
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                raise ConnectionError("closed by server")
            if opcode in (0x1, 0x2):
                self.fragments = bytearray(payload)
            elif opcode == 0x0 and self.fragments is not None:
                self.fragments += payload
            else:
                raise LiveError("protocolMismatch")
            if len(self.fragments) > MAX_MESSAGE:
                raise LiveError("protocolMismatch")
            if fin:
                message, self.fragments = bytes(self.fragments), None
                try:
                    return message.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise LiveError("protocolMismatch") from error

    def recv(self, timeout: float) -> str | None:
        """The next text message, or None when nothing complete arrives in time."""
        self.sock.settimeout(timeout)
        while True:
            message = self._parse()
            if message is not None:
                return message
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                return None
            if not chunk:
                raise ConnectionError("closed")
            self.buffer += chunk

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def rpc_frames(text: str) -> list[dict]:
    """Effect RPC's JSON socket codec sends one frame or an array of frames."""
    try:
        value = json.loads(text)
    except ValueError as error:
        raise LiveError("protocolMismatch") from error
    frames = value if isinstance(value, list) else [value]
    if not all(isinstance(frame, dict) for frame in frames):
        raise LiveError("protocolMismatch")
    return frames


def request_key(value) -> int | None:
    """Effect RPC echoes the request id as sent, a number here, or as its string form."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def is_request_id(value) -> bool:
    return request_key(value) == REQUEST_ID


def shell_request(options: dict) -> dict:
    """Subscribe to thread shells. Idle subagents and older settled threads never reach the
    inbox, so servers that can leave them out are asked to."""
    payload = {}
    if options.get("shellThreadFamilies") is True:
        payload["omitIdleSubagents"] = True
        if options.get("shellOlderSettled") is True:
            payload["omitOlderSettled"] = True
    return {"_tag": "Request", "id": SHELL_ID, "tag": "orchestration.subscribeShell", "payload": payload,
            "headers": []}


# ---------------------------------------------------------------------------
# Stream state (LimitsLiveData)
# ---------------------------------------------------------------------------


class LiveData:
    def __init__(self):
        self.providers: list[dict] = []
        self.sources: list[dict] = []
        self.notices: list[str] = []
        self.shell_options: dict = {}

    def copy(self) -> "LiveData":
        other = LiveData()
        other.providers, other.sources, other.notices = list(self.providers), list(self.sources), list(self.notices)
        return other

    def apply(self, event) -> bool:
        if not isinstance(event, dict) or event.get("version") != 1 or isinstance(event.get("version"), bool):
            raise LiveError("protocolMismatch")
        kind = event.get("type")
        if kind == "snapshot":
            config = event.get("config")
            if not isinstance(config, dict) or not isinstance(config.get("providers"), list):
                raise LiveError("protocolMismatch")
            self.providers = self._providers(config["providers"])
            self.shell_options = {key: config.get(key) for key in ("shellThreadFamilies", "shellOlderSettled")}
            if "usageLimitSources" in config and config["usageLimitSources"] is not None:
                self._sources(config["usageLimitSources"])
            return True
        if kind == "providerStatuses":
            payload = event.get("payload")
            if not isinstance(payload, dict) or not isinstance(payload.get("providers"), list):
                raise LiveError("protocolMismatch")
            self.providers = self._providers(payload["providers"])
            return True
        if kind == "usageLimitSourcesUpdated":
            payload = event.get("payload")
            if not isinstance(payload, dict) or "sources" not in payload:
                raise LiveError("protocolMismatch")
            self._sources(payload["sources"])
            return True
        # Settings, keybindings, themes and event types a newer server adds carry no limits.
        return False

    @staticmethod
    def _providers(raw: list) -> list[dict]:
        return [record for record in (t3bar.decode_limits_record(item, "provider") for item in raw) if record]

    def _sources(self, raw) -> None:
        if not isinstance(raw, list):
            raise LiveError("protocolMismatch")
        records, warnings = [], []
        for source in raw:
            if not isinstance(source, dict):
                continue
            ident, label, accounts = source.get("id"), source.get("label"), source.get("accounts")
            if not isinstance(ident, str) or not isinstance(label, str) or not isinstance(accounts, list):
                continue
            if isinstance(source.get("error"), str):
                warnings.append(f"{label}: {source['error']}")
            elif not accounts:
                warnings.append(f"{label}: No accounts reported.")
            for account in accounts:
                if not isinstance(account, dict):
                    continue
                account_id, driver, limits = account.get("id"), account.get("driver"), account.get("usageLimits")
                if not isinstance(account_id, str) or not isinstance(driver, str) or not isinstance(limits, dict):
                    continue
                workspace = account.get("workspaceId") if isinstance(account.get("workspaceId"), str) else None
                email = account.get("email") if isinstance(account.get("email"), str) else None
                name = account_id[:-5] if account_id.lower().endswith(".json") else account_id
                record = t3bar.decode_limits_record({
                    "driver": driver, "enabled": True, "installed": True,
                    "instanceId": f"source:{ident}:{account_id}" + (f":{workspace}" if workspace else ""),
                    "displayName": None if email else name,
                    "auth": {"status": "authenticated", "label": account.get("plan"), "email": email,
                             "workspaceId": workspace},
                    "usageLimits": limits,
                }, "source")
                if record:
                    record["isSource"] = True
                    records.append(record)
        self.sources, self.notices = records, warnings


# ---------------------------------------------------------------------------
# Thread shells (orchestration.subscribeShell)
# ---------------------------------------------------------------------------


class ShellState:
    """Active thread shells and projects, kept current from the shell stream."""

    def __init__(self):
        self.threads: dict[str, dict] = {}
        self.projects: dict[str, dict] = {}
        self.ready = False

    def copy(self) -> "ShellState":
        other = ShellState()
        other.threads, other.projects, other.ready = dict(self.threads), dict(self.projects), self.ready
        return other

    def apply(self, item) -> bool:
        """False for items that carry nothing the inbox shows. Malformed items are skipped:
        a newer server's shell must not take the Limits stream down with it."""
        if not isinstance(item, dict):
            return False
        kind = item.get("kind")
        if kind == "snapshot":
            snapshot = item.get("snapshot")
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("threads"), list):
                return False
            self.threads = {t["id"]: t for t in snapshot["threads"] if isinstance(t, dict) and isinstance(t.get("id"), str)}
            projects = snapshot.get("projects") if isinstance(snapshot.get("projects"), list) else []
            self.projects = {p["id"]: p for p in projects if isinstance(p, dict) and isinstance(p.get("id"), str)}
            self.ready = True
            return True
        if kind == "thread.updated":
            thread = item.get("thread")
            if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
                return False
            if item.get("location") == "active":
                self.threads[thread["id"]] = thread
            else:
                self.threads.pop(thread["id"], None)  # archived
            return True
        if kind == "thread.removed":
            if item.get("location") == "active" and isinstance(item.get("threadId"), str):
                return self.threads.pop(item["threadId"], None) is not None
            return False
        if kind == "project.updated":
            project = item.get("project")
            if not isinstance(project, dict) or not isinstance(project.get("id"), str):
                return False
            self.projects[project["id"]] = project
            return True
        if kind == "project.removed":
            if isinstance(item.get("projectId"), str):
                return self.projects.pop(item["projectId"], None) is not None
            return False
        return False  # "synchronized" and kinds a newer server adds

    def items(self, now: float) -> list[dict]:
        items = []
        for thread in self.threads.values():
            row = shell_row(thread, self.projects)
            if row is None:
                continue
            try:
                item = t3bar.thread_item(row, now)
            except (t3bar.ReadError, TypeError, ValueError):
                continue
            if item is not None:
                items.append(item)
        return t3bar.sort_threads(items)


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None


def shell_pr_number(thread: dict):
    for link in [thread.get("linkedPullRequest"), *(thread.get("pullRequests") or []), thread.get("branchPullRequest")]:
        if isinstance(link, dict) and link.get("source") != "stack-dismissed":
            number = link.get("number")
            if isinstance(number, int) and not isinstance(number, bool):
                return number
    return None


def shell_row(thread, projects: dict) -> dict | None:
    """The columns `t3bar.thread_query` reads from statev2.sqlite, from one thread shell. None
    for rows the inbox leaves out: deleted, archived, settled, subagent and side-chat threads.

    The shell summarizes what the query counts: its one pending request stands for the pending
    count, and a failed latest run stands for the provider session's error state."""
    if not isinstance(thread, dict) or not isinstance(thread.get("id"), str) or thread["id"].startswith("thread:"):
        return None
    if thread.get("deletedAt") or thread.get("archivedAt") or thread.get("settledAt"):
        return None
    lineage = thread.get("lineage") if isinstance(thread.get("lineage"), dict) else {}
    if lineage.get("sideChat") is True or lineage.get("relationshipToParent") == "subagent":
        return None
    project = projects.get(thread.get("projectId")) or {}
    pending = thread.get("pendingRuntimeRequest") if isinstance(thread.get("pendingRuntimeRequest"), dict) else None
    status = thread.get("status")
    latest = status if isinstance(status, str) and status not in ("idle", "queued") else None
    failure = None
    if _text(thread.get("lastErrorClass")):
        failure = json.dumps({"class": thread["lastErrorClass"], "resetAt": thread.get("usageLimitResetAt")})
    goal = thread.get("goal") if isinstance(thread.get("goal"), dict) else {}
    model = thread.get("modelSelection") if isinstance(thread.get("modelSelection"), dict) else {}
    history = [p for p in thread.get("providerInstanceHistory") or [] if isinstance(p, str) and p]
    if not history and _text(thread.get("providerInstanceId")):
        history = [thread["providerInstanceId"]]
    icon = project.get("projectIcon")
    guests = thread.get("pendingGuestMessageCount")
    return {
        "thread_id": thread["id"],
        "title": thread.get("title") if isinstance(thread.get("title"), str) else None,
        "project_id": thread.get("projectId") if isinstance(thread.get("projectId"), str) else None,
        "project_title": _text(project.get("title")) or "No Project",
        "created_at": thread.get("createdAt"),
        "updated_at": thread.get("updatedAt"),
        "active_run_status": thread.get("activityRunStatus"),
        "active_run_started_at": thread.get("activityRunStartedAt"),
        "latest_run_status": latest,
        "latest_run_started_at": thread.get("latestRunStartedAt") or thread.get("latestRunRequestedAt"),
        "latest_run_completed_at": thread.get("latestRunCompletedAt"),
        "pending_user_input": 1 if pending and pending.get("kind") == "user_input" else 0,
        "pending_approval": 1 if pending and pending.get("kind") != "user_input" else 0,
        "branch": thread.get("branch") if isinstance(thread.get("branch"), str) else None,
        "worktree_path": thread.get("worktreePath"),
        "model_name": _text(model.get("model")),
        "pr_number": shell_pr_number(thread),
        "is_pinned": 1 if thread.get("pinnedAt") else 0,
        "snoozed_until": thread.get("snoozedUntil"),
        "has_goal": 1 if goal.get("status") == "active" else 0,
        "snoozed_at": thread.get("snoozedAt"),
        "pin_order_key": _text(thread.get("pinOrderKey")),
        "active_order_key": _text(thread.get("activeOrderKey")),
        "provider_history": ",".join(history),
        "latest_user_message_at": thread.get("latestUserMessageAt"),
        "project_icon_json": json.dumps(icon) if isinstance(icon, dict) else None,
        "favicon_path": _text(project.get("faviconPath")),
        "workspace_root": _text(project.get("workspaceRoot")),
        "unsettled_at": thread.get("unsettledAt"),
        "last_visited_at": thread.get("lastVisitedAt"),
        "session_status": "error" if latest == "failed" else None,
        "session_updated_at": thread.get("latestRunCompletedAt") or thread.get("updatedAt"),
        "latest_failure": failure if latest in ("failed", "error") else None,
        "pending_guest_messages": guests if isinstance(guests, int) and not isinstance(guests, bool) else 0,
        "has_runs": 1 if thread.get("latestRunId") or thread.get("activeRunId") else 0,
        "blocked_on_thread_wait": 1 if thread.get("blockedOnThreadWait") is True else 0,
    }


def local_time_zone() -> str:
    """The IANA zone usage days are bucketed in; T3 refuses fixed offsets across DST."""
    zone = os.environ.get("TZ", "").lstrip(":").strip()
    if zone and "/" in zone and not zone.startswith("/") or zone == "UTC":
        return zone
    for path in ([zone] if zone.startswith("/") else []) + ["/etc/localtime"]:
        try:
            target = os.path.realpath(path)
        except OSError:
            continue
        if "/zoneinfo/" in target:
            name = target.split("/zoneinfo/", 1)[1]
            for prefix in ("posix/", "right/"):
                name = name[len(prefix):] if name.startswith(prefix) else name
            if name:
                return name
    text = (t3bar.read_text("/etc/timezone") or "").strip()
    return text or "UTC"


# ---------------------------------------------------------------------------
# Connection (LimitsLiveConnection)
# ---------------------------------------------------------------------------


class LiveConnection:
    def __init__(self, base: str, twenty_four: bool, events: queue.Queue):
        self.base = base
        self.twenty_four = twenty_four
        self.events = events
        self.store = KeyringStore()
        self.lock = threading.Lock()
        self.generation = 0
        self.status = "cacheOnly"
        self.message: str | None = None
        self.data: LiveData | None = None
        self.last_updated: float | None = None
        self.credential: dict | None = None
        self.remembered = False
        self.socket: WebSocket | None = None
        self.worker: threading.Thread | None = None
        self.shell: ShellState | None = None
        self.next_usage_id = FIRST_USAGE_ID
        self.pending_usage: dict[int, dict] = {}

    # ----- state -----

    def notify(self) -> None:
        self.events.put(("render", None))

    def set_status(self, run: int | None, status: str, message: str | None = None) -> bool:
        with self.lock:
            if run is not None and run != self.generation:
                return False
            self.status, self.message = status, message
        self.notify()
        return True

    def current(self, run: int) -> bool:
        return run == self.generation

    def status_text(self) -> str:
        if self.status == "failed":
            return self.message or "Could not connect to local T3 Code."
        return STATUS_TEXT.get(self.status, self.status)

    def scopes(self) -> list[str]:
        credential = self.credential
        return list(credential.get("scopes", [READ_SCOPE])) if credential else []

    def usage_available(self) -> bool:
        return self.status == "live" and self.socket is not None and USAGE_SCOPE in self.scopes()

    def view(self) -> dict:
        with self.lock:
            status, data, last = self.status, self.data, self.last_updated
            credential = self.credential
            scopes = self.scopes()
            usage_live = self.usage_available()
            threads_live = status == "live" and self.shell is not None
        cache, skipped = t3bar.read_cache_records(self.base)
        live_now = data is not None and status == "live"
        if data is None:
            if cache is None:
                view = {"state": "noCacheDirectory", "groups": [], "notices": [], "links": [], "skippedFiles": 0,
                        "staleness": None, "footnote": t3bar.LIMITS_FOOTNOTE}
            else:
                view = t3bar.render_limits(cache, skipped, self.twenty_four)
        else:
            # A live config is authoritative, so deleted or disabled providers are not revived from the
            # files. During an outage the last live data merges with newer saved checks (newest wins).
            records = data.providers + data.sources if live_now else data.providers + (cache or []) + data.sources
            view = t3bar.render_limits(records, skipped, self.twenty_four, data.notices,
                                       LIVE_FOOTNOTE if live_now else None)
        view["live"] = {
            "status": status,
            "text": self.status_text(),
            "connected": live_now,
            "retained": data is not None and not live_now,
            "lastUpdatedAt": last,
            "lastUpdatedAgo": t3bar.fmt_ago(last, t3bar.now_ms()) if last else None,
            "port": credential["port"] if credential else None,
            "remembered": self.remembered,
            "available": HAVE_CRYPTO,
            "keyring": self.store.tool is not None,
            "scopes": scopes,
            "threads": threads_live,
            "usage": usage_live,
        }
        return view

    def inbox(self) -> dict | None:
        """The thread inbox from the shell stream, in `t3bar.snapshot`'s shape. None while
        not live, so the widget shows its own reading of statev2.sqlite."""
        with self.lock:
            if self.status != "live" or self.shell is None:
                return None
            shell, credential = self.shell, self.credential
        threads = shell.items(t3bar.now_ms())
        return {
            "ok": True, "error": None, "errorCode": None, "serverOnline": True,
            "environmentId": t3bar.environment_id(self.base),
            "port": credential["port"] if credential else None,
            "stateDir": self.base, "threads": threads, "live": True,
            "summary": t3bar.summarize(threads, True), "source": "live",
        }

    # ----- usage -----

    def usage(self, period, seq) -> None:
        """Asks the server for one period's usage summary. The reply arrives as a "usage" event;
        without live usage it carries an error, and the widget reads the usage cache instead."""
        days = period if period in (1, 7, 30, 90) else 30
        with self.lock:
            ws = self.socket if self.usage_available() else None
            if ws is None:
                reason = ("The pairing code does not include View diagnostics and usage."
                          if self.status == "live" else "Not connected.")
                request_id = None
            else:
                request_id = self.next_usage_id
                self.next_usage_id += 1
                payload, window = t3bar.usage_request(days, t3bar.now_ms(), local_time_zone())
                self.pending_usage[request_id] = {"period": days, "seq": seq, "window": window,
                                                  "sent": time.monotonic()}
        if request_id is None:
            self.events.put(("usage", {"period": days, "seq": seq, "report": None, "error": reason}))
            return
        try:
            ws.send_json({"_tag": "Request", "id": request_id, "tag": "server.getUsageSummary",
                          "payload": payload, "headers": []})
        except OSError:
            self._usage_failed(request_id, "The connection dropped.")

    def _usage_failed(self, request_id: int, reason: str) -> None:
        with self.lock:
            pending = self.pending_usage.pop(request_id, None)
        if pending:
            self.events.put(("usage", {"period": pending["period"], "seq": pending["seq"], "report": None,
                                       "error": reason}))

    def fail_pending_usage(self, reason: str, older_than: float | None = None) -> None:
        now = time.monotonic()
        with self.lock:
            ids = [i for i, p in self.pending_usage.items() if older_than is None or now - p["sent"] > older_than]
        for request_id in ids:
            self._usage_failed(request_id, reason)

    def _usage_reply(self, request_id: int, exit_value) -> None:
        with self.lock:
            pending = self.pending_usage.pop(request_id, None)
        if pending is None:
            return
        reply = {"period": pending["period"], "seq": pending["seq"], "report": None, "error": None}
        if isinstance(exit_value, dict) and exit_value.get("_tag") == "Success":
            summary = exit_value.get("value")
            try:
                report = t3bar.report_from_summary(summary, pending["window"])
                read_at = t3bar.parse_date(summary.get("readAt")) or t3bar.now_ms()
                report.update(state="ok", period=pending["period"], live=True, cacheModified=read_at,
                              cacheAge=t3bar.fmt_ago(read_at, t3bar.now_ms()), version=summary.get("contractVersion"),
                              refreshing=any(isinstance(s, dict) and s.get("refreshing") is True
                                             for s in summary.get("sources") or []))
                reply["report"] = report
            except (ValueError, TypeError, AttributeError, KeyError):
                reply["error"] = "This T3 Code version returned an unsupported usage summary."
        else:
            text = json.dumps(exit_value)
            if "EnvironmentAuthorizationError" in text or "missing required scope" in text:
                reply["error"] = "The pairing code does not include View diagnostics and usage."
            elif "UsageReadError" in text:
                reply["error"] = "T3 Code could not read usage. Retrying."
            else:
                reply["error"] = "The usage request failed."
        self.events.put(("usage", reply))

    # ----- commands -----

    def stop(self) -> None:
        with self.lock:
            self.generation += 1
            ws, self.socket = self.socket, None
        if ws:
            def interrupt():
                try:
                    ws.sock.settimeout(1)
                    ws.send_json(INTERRUPT)
                except OSError:
                    pass
                ws.close()
            threading.Thread(target=interrupt, daemon=True).start()
        with self.lock:
            self.shell = None
            if self.credential is not None or self.status in ("connecting", "live", "retrying"):
                self.status, self.message = "paused", None
        self.fail_pending_usage("The connection closed.")

    def _start(self, target, *args) -> int:
        with self.lock:
            self.generation += 1
            run = self.generation
            self.status, self.message = "connecting", None
        self.notify()
        self.worker = threading.Thread(target=target, args=(run, *args), daemon=True)
        self.worker.start()
        return run

    def pair(self, port, code, remember: bool) -> None:
        self.stop()
        with self.lock:
            self.credential, self.data, self.last_updated = None, None, None
        self._start(self._pair_worker, port, code if isinstance(code, str) else "", bool(remember))

    def reconnect_saved(self, port, quiet: bool = False) -> None:
        """quiet: the launch-time attempt, where a missing token keeps the onboarding state."""
        if not quiet:
            self.stop()
        with self.lock:
            if quiet and self.status in ("live", "connecting"):
                return
            self.credential = None
        try:
            valid_port(port)
            if not HAVE_CRYPTO:
                raise LiveError("crypto")
            saved = self.store.load()
            if saved is None:
                if not quiet:
                    self.set_status(None, "failed", "No saved token. Enter a new read-only pairing code.")
                return
            if saved["port"] != port:
                raise LiveError("destination")
            if saved["expiresAt"] <= time.time():
                raise LiveError("expired")
        except LiveError as error:
            self.set_status(None, "failed", str(error))
            return
        with self.lock:
            self.credential, self.remembered = saved, True
        self._start(self._connection_worker, saved)

    def disconnect(self) -> None:
        self.stop()
        with self.lock:
            self.credential, self.data, self.last_updated, self.remembered = None, None, None, False
        try:
            self.store.delete()
            self.set_status(None, "cacheOnly")
        except LiveError as error:
            self.set_status(None, "failed", str(error))

    # ----- workers -----

    def _pair_worker(self, run: int, port, code: str, remember: bool) -> None:
        try:
            port = valid_port(port)
            code = code.strip()
            if not code or len(code.encode("utf-8")) >= 16384:
                raise LiveError("authorization")
            grant = self._exchange(code, port)
            if not self.current(run):
                return
            if remember:
                self.store.save(grant)
            else:
                self.store.delete()
            with self.lock:
                if not self.current(run):
                    return
                self.credential, self.remembered = grant, remember
        except LiveError as error:
            self.set_status(run, "failed", str(error))
            return
        except Exception:  # noqa: BLE001 - transport failures read as one message
            self.set_status(run, "failed", "Could not connect to local T3 Code. Try connecting again.")
            return
        self._connection_worker(run, grant)

    def _exchange(self, code: str, port: int) -> dict:
        signer = DPoP()
        path = "/oauth/token"
        form = urllib.parse.urlencode({
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": code,
            "subject_token_type": "urn:t3:params:oauth:token-type:environment-bootstrap",
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "scope": " ".join(REQUESTED_SCOPES),
            "client_label": "T3 Code Bar",
            "client_device_type": "desktop",
            "client_os": "linux",
        }, quote_via=urllib.parse.quote)
        result = http_post(port, path, {
            "Content-Type": "application/x-www-form-urlencoded",
            "DPoP": signer.proof("POST", f"http://127.0.0.1:{port}{path}"),
        }, form.encode("ascii"))
        token, seconds = result.get("access_token"), result.get("expires_in")
        scopes = result.get("scope").split() if isinstance(result.get("scope"), str) else None
        # Only a DPoP-bound token is accepted, never a plain bearer token. The server grants the
        # requested scopes the code allows: the read scope, and usage when the code includes it.
        if (result.get("token_type") != "DPoP"
                or result.get("issued_token_type") != "urn:ietf:params:oauth:token-type:access_token"
                or not isinstance(token, str) or not token
                or isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 < seconds < float("inf")):
            raise LiveError("authorization")
        return {"port": port, "token": token, "expiresAt": time.time() + seconds, "privateKey": signer.raw(),
                "scopes": valid_scopes(scopes)}

    def _sleep(self, run: int, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not self.current(run):
                return False
            time.sleep(0.2)
        return self.current(run)

    def _connection_worker(self, run: int, credential: dict) -> None:
        while self.current(run):
            ws = None
            try:
                if credential["expiresAt"] <= time.time():
                    raise LiveError("expired")
                signer = DPoP(credential["privateKey"])
                port, token = valid_port(credential["port"]), credential["token"]
                path = "/api/auth/websocket-ticket/rpc"
                issued = http_post(port, path, {
                    "Authorization": f"DPoP {token}",
                    "DPoP": signer.proof("POST", f"http://127.0.0.1:{port}{path}", token),
                })
                ticket, expires = issued.get("ticket"), t3bar.parse_date(issued.get("expiresAt"))
                if not isinstance(ticket, str) or not ticket or expires is None or expires <= t3bar.now_ms():
                    raise LiveError("protocolMismatch")
                if not self.current(run):
                    return
                # A WebSocket cannot carry headers, so the upgrade proof rides in the `dpop` parameter
                # and names the socket URL as the HTTP request the server sees.
                proof = signer.proof("GET", f"http://127.0.0.1:{port}/ws", ticket)
                query = urllib.parse.urlencode({"wsTicket": ticket, "orchestrationProtocol": "2", "dpop": proof})
                ws = WebSocket(port, "/ws?" + query)
                with self.lock:
                    if not self.current(run):
                        ws.close()
                        return
                    self.socket = ws
                self._stream(run, ws, credential)
            except LiveError as error:
                if ws:
                    ws.close()
                if self.current(run):
                    with self.lock:
                        self.socket, self.shell = None, None
                    self.fail_pending_usage("The connection closed.")
                    self.set_status(run, "failed", str(error))
                return
            except (OSError, ValueError, http.client.HTTPException):
                if ws:
                    ws.close()
                if not self.current(run):
                    return
                with self.lock:
                    self.socket, self.shell = None, None
                self.fail_pending_usage("The connection dropped.")
                self.set_status(run, "retrying")
                if not self._sleep(run, RETRY_SECONDS):
                    return

    def _stream(self, run: int, ws: WebSocket, credential: dict) -> None:
        ws.send_json(SUBSCRIBE)
        connected = last_frame = last_ping = time.monotonic()
        has_snapshot = shell_sent = False
        state = LiveData()
        shell = ShellState()
        while self.current(run):
            now = time.monotonic()
            if now - last_ping >= 5:
                if time.time() >= credential["expiresAt"]:
                    raise LiveError("expired")
                if now - last_frame > 15 or (not has_snapshot and now - connected > 15):
                    raise ConnectionError("stream went quiet")
                ws.send_json(PING)
                last_ping = now
            text = ws.recv(1.0)
            if text is None:
                continue
            last_frame = time.monotonic()
            for frame in rpc_frames(text):
                tag, key = frame.get("_tag"), request_key(frame.get("requestId"))
                if tag == "Pong":
                    continue
                if tag == "Exit" and key is not None and key >= FIRST_USAGE_ID:
                    self._usage_reply(key, frame.get("exit"))
                    continue
                if tag == "Exit" and key == SHELL_ID:
                    # Threads fall back to statev2.sqlite; the Limits stream stays live.
                    with self.lock:
                        if self.current(run):
                            self.shell = None
                    self.notify()
                    continue
                if tag == "Exit":
                    if not self.current(run):
                        return
                    if "Authorization" in json.dumps(frame.get("exit")):
                        raise LiveError("authorization")
                    raise ConnectionError("stream ended")
                if tag != "Chunk" or key not in (REQUEST_ID, SHELL_ID) or not isinstance(frame.get("values"), list):
                    raise LiveError("protocolMismatch")
                if key == SHELL_ID:
                    changed = False
                    for value in frame["values"]:
                        changed = shell.apply(value) or changed
                    ws.send_json({"_tag": "Ack", "requestId": SHELL_ID})
                    if changed and shell.ready:
                        with self.lock:
                            if not self.current(run):
                                return
                            self.shell = shell.copy()
                        self.notify()
                    continue
                changed = False
                for value in frame["values"]:
                    if isinstance(value, dict) and value.get("type") == "snapshot":
                        has_snapshot = True
                    changed = state.apply(value) or changed
                ws.send_json(ACK)
                if not self.current(run):
                    return
                if has_snapshot and not shell_sent:
                    # The config says which shell options this server understands.
                    ws.send_json(shell_request(state.shell_options))
                    shell_sent = True
                if changed and has_snapshot:
                    with self.lock:
                        if not self.current(run):
                            return
                        self.data = state.copy()
                        self.last_updated = t3bar.now_ms()
                        self.status, self.message = "live", None
                    self.notify()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def read_commands(events: queue.Queue) -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            command = json.loads(line)
        except ValueError:
            continue
        if isinstance(command, dict):
            events.put(("command", command))
    events.put(("eof", None))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--clock", choices=["12", "24"], default="24")
    args = parser.parse_args()
    base = t3bar.state_dir(args.state_dir)

    events: queue.Queue = queue.Queue()
    connection = LiveConnection(base, args.clock == "24", events)
    threading.Thread(target=read_commands, args=(events,), daemon=True).start()

    # At launch, reconnect with a remembered token. A missing token keeps the onboarding state.
    port = t3bar.runtime_port(t3bar.runtime_info(base))
    if port:
        connection.reconnect_saved(port, quiet=True)

    events.put(("render", None))
    last: dict[str, str | None] = {"limits": None, "inbox": None}

    def emit(kind: str, body: dict, dedupe: bool) -> None:
        text = json.dumps(dict(kind=kind, **body), separators=(",", ":"), ensure_ascii=False)
        if dedupe:
            if text == last[kind]:
                return
            last[kind] = text
        sys.stdout.write(text + "\n")
        sys.stdout.flush()

    while True:
        try:
            kind, payload = events.get(timeout=5)
        except queue.Empty:
            # "Checked 5m ago", saved files and snooze timers change without an event.
            kind, payload = "render", None
        if kind == "eof":
            connection.stop()
            return 0
        if kind == "usage":
            emit("usage", payload, False)
            continue
        if kind == "command":
            cmd = payload.get("cmd")
            if cmd == "pair":
                connection.pair(payload.get("port"), payload.get("code"), payload.get("remember", False))
            elif cmd == "reconnectSaved":
                connection.reconnect_saved(payload.get("port"))
            elif cmd == "disconnect":
                connection.disconnect()
            elif cmd == "clock":
                connection.twenty_four = bool(payload.get("twentyFour", True))
            elif cmd == "usage":
                connection.usage(payload.get("period"), payload.get("seq"))
            payload = None  # drop the pairing code as soon as it is handed over
        connection.fail_pending_usage("T3 Code took too long to read usage.", USAGE_TIMEOUT_SECONDS)
        try:
            emit("limits", {"view": connection.view()}, True)
            emit("inbox", {"snapshot": connection.inbox()}, True)
        except Exception as error:  # noqa: BLE001 - keep the stream alive
            print(f"t3live: render failed: {error}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, BrokenPipeError):
        sys.exit(0)
