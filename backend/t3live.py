#!/usr/bin/env python3
"""Live Limits connection to the local T3 Code server, ported from T3 Code Bar's
LimitsLiveConnection (macOS).

This is the source T3 Code's own Usage page uses: the `subscribeServerConfig`
RPC stream, with provider snapshots and CLI Proxy pooled accounts.

    1. The user creates a pairing code in T3 Code with only "View environment"
       (`orchestration:read`) and gives it to the widget.
    2. The code is exchanged at loopback `/oauth/token` for a DPoP-bound access
       token with exactly that scope. The pairing code is never stored.
    3. A WebSocket ticket is issued with that token, the socket opens at
       `ws://127.0.0.1:<port>/ws`, and the widget subscribes to the server config.
    4. Only "Remember" stores the widget's own token and key, in the desktop
       keyring (secret-tool). Nothing else is persisted.

The process reads JSON commands on stdin and prints the rendered Limits tab as
one JSON line whenever it changes. Without a connection, or during an outage,
it falls back to T3 Code's saved provider files, as the original app does.

Commands:
  {"cmd": "pair", "port": 3773, "code": "...", "remember": false}
  {"cmd": "reconnectSaved", "port": 3773}
  {"cmd": "disconnect"}
  {"cmd": "clock", "twentyFour": true}
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

REQUEST_ID = 0
SUBSCRIBE = {
    "_tag": "Request", "id": REQUEST_ID, "tag": "subscribeServerConfig",
    "payload": {"usageLimitSources": True, "workspaceSnapshotsOnDemand": True}, "headers": [],
}
ACK = {"_tag": "Ack", "requestId": REQUEST_ID}
INTERRUPT = {"_tag": "Interrupt", "requestId": REQUEST_ID, "interruptors": []}
PING = {"_tag": "Ping"}
MAX_MESSAGE = 8 * 1024 * 1024
RETRY_SECONDS = 3
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
# own exchanged token, its port, expiry and private key are stored.
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
            return {
                "port": valid_port(data["port"]), "token": str(data["token"]),
                "expiresAt": float(data["expiresAt"]), "privateKey": base64.b64decode(data["privateKey"]),
            }
        except (ValueError, KeyError, TypeError, LiveError) as error:
            raise LiveError("storage") from error

    def save(self, credential: dict) -> None:
        if not self.tool:
            raise LiveError("storage")
        secret = json.dumps({
            "port": credential["port"], "token": credential["token"], "expiresAt": credential["expiresAt"],
            "privateKey": base64.b64encode(credential["privateKey"]).decode("ascii"),
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


def is_request_id(value) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool) and value == REQUEST_ID) or value == str(REQUEST_ID)


# ---------------------------------------------------------------------------
# Stream state (LimitsLiveData)
# ---------------------------------------------------------------------------


class LiveData:
    def __init__(self):
        self.providers: list[dict] = []
        self.sources: list[dict] = []
        self.notices: list[str] = []

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

    def view(self) -> dict:
        with self.lock:
            status, data, last = self.status, self.data, self.last_updated
            credential = self.credential
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
        }
        return view

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
            if self.credential is not None or self.status in ("connecting", "live", "retrying"):
                self.status, self.message = "paused", None

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
            "scope": "orchestration:read",
            "client_label": "T3 Code Bar Limits",
            "client_device_type": "desktop",
            "client_os": "linux",
        }, quote_via=urllib.parse.quote)
        result = http_post(port, path, {
            "Content-Type": "application/x-www-form-urlencoded",
            "DPoP": signer.proof("POST", f"http://127.0.0.1:{port}{path}"),
        }, form.encode("ascii"))
        token, seconds = result.get("access_token"), result.get("expires_in")
        scopes = result.get("scope").split() if isinstance(result.get("scope"), str) else None
        # Only a DPoP-bound token with exactly the read scope is accepted, never a plain bearer token.
        if (result.get("token_type") != "DPoP"
                or result.get("issued_token_type") != "urn:ietf:params:oauth:token-type:access_token"
                or scopes != ["orchestration:read"]
                or not isinstance(token, str) or not token
                or isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 < seconds < float("inf")):
            raise LiveError("authorization")
        return {"port": port, "token": token, "expiresAt": time.time() + seconds, "privateKey": signer.raw()}

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
                        self.socket = None
                    self.set_status(run, "failed", str(error))
                return
            except (OSError, ValueError, http.client.HTTPException):
                if ws:
                    ws.close()
                if not self.current(run):
                    return
                with self.lock:
                    self.socket = None
                self.set_status(run, "retrying")
                if not self._sleep(run, RETRY_SECONDS):
                    return

    def _stream(self, run: int, ws: WebSocket, credential: dict) -> None:
        ws.send_json(SUBSCRIBE)
        connected = last_frame = last_ping = time.monotonic()
        has_snapshot = False
        state = LiveData()
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
                tag = frame.get("_tag")
                if tag == "Pong":
                    continue
                if tag == "Exit":
                    if not self.current(run):
                        return
                    if "Authorization" in json.dumps(frame.get("exit")):
                        raise LiveError("authorization")
                    raise ConnectionError("stream ended")
                if tag != "Chunk" or not is_request_id(frame.get("requestId")) or not isinstance(frame.get("values"), list):
                    raise LiveError("protocolMismatch")
                changed = False
                for value in frame["values"]:
                    if isinstance(value, dict) and value.get("type") == "snapshot":
                        has_snapshot = True
                    changed = state.apply(value) or changed
                ws.send_json(ACK)
                if not self.current(run):
                    return
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
    last = None
    while True:
        try:
            kind, payload = events.get(timeout=5)
        except queue.Empty:
            kind, payload = "render", None  # "Checked 5m ago" and saved files change without an event
        if kind == "eof":
            connection.stop()
            return 0
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
            payload = None  # drop the pairing code as soon as it is handed over
        try:
            view = connection.view()
        except Exception as error:  # noqa: BLE001 - keep the stream alive
            print(f"t3live: render failed: {error}", file=sys.stderr, flush=True)
            continue
        text = json.dumps(view, separators=(",", ":"), ensure_ascii=False)
        if text != last:
            last = text
            sys.stdout.write(text + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, BrokenPipeError):
        sys.exit(0)
