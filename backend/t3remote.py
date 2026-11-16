#!/usr/bin/env python3
"""Remote T3 Code environments, reached by a pairing link. Ported from T3 Code Bar's
manual connections (`bar-connection`): only the direct route, without SSH, T3 Connect
or the desktop catalog import.

    - Pairing links: `https://host/pair#token=CODE`, `t3code://pair?pairingUrl=...`
      and `https://app.t3.codes/pair?host=...#token=CODE`.
    - Destinations: any HTTPS origin; plain HTTP only to loopback or to a Tailscale
      peer that the running Tailscale daemon lists.
    - Identity: `/.well-known/t3/environment` names the environment. It is fetched
      before a code is redeemed and again before every connection, and a server that
      now names another environment is refused.
    - Saved connections: `connections.json` in the widget's config folder, without
      credentials. Each grant is stored in the desktop keyring under its environment
      and origin.
    - Usage across environments: each usage source belongs to one environment, so a
      transcript folder two servers both scan counts once.
"""

from __future__ import annotations

import calendar
import http.client
import ipaddress
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import time
import urllib.parse

MAX_INPUT = 8192
MAX_DESCRIPTOR = 1024 * 1024
MAX_LABEL = 256
MAX_ENVIRONMENTS = 64
PAIRING_ALPHABET = set("23456789ABCDEFGHJKLMNPQRSTUVWXYZ")
PAIRING_CODE_LENGTH = 12
DESCRIPTOR_PATH = "/.well-known/t3/environment"
PROTOCOL_VERSIONS = (1, 2)
OS_NAMES = ("darwin", "linux", "windows", "unknown")
ARCH_NAMES = ("arm64", "x64", "other")
MACHINES = ("server", "cloud", "linux", "desktop", "laptop", "mac-mini", "mac-studio")
TAILSCALE_BINARIES = ("/usr/bin/tailscale", "/usr/local/bin/tailscale", "/usr/sbin/tailscale",
                      "/opt/homebrew/bin/tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale")
TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
KEYRING_SERVICE = "t3codebar.remote"
HTTP_TIMEOUT = 10


class RemoteError(Exception):
    """A problem with a remote environment the user has to act on."""

    MESSAGES = {
        "invalidLink": ("Paste the full pairing link from T3 Code: Settings → Connections → Authorized clients → "
                        "Create link, then copy the link."),
        "invalidCode": "This pairing link has no valid pairing code. Create a new link in T3 Code.",
        "destination": ("Plain HTTP is only allowed to this computer or to a Tailscale peer. "
                        "Use the server's HTTPS address."),
        "tailscale": "Tailscale does not list this address as a connected peer. Check that Tailscale is running.",
        "unreachable": "Could not reach this T3 Code server. Check the address and that the server is running.",
        "descriptor": "This address did not identify itself as a T3 Code environment.",
        "protocol": "This T3 Code server uses an orchestration protocol this widget does not support.",
        "wrongEnvironment": ("This address now serves a different T3 Code environment. "
                             "Remove the connection and pair again."),
        "conflict": "This address is saved for a different environment. Remove that connection first.",
        "registry": "Could not read or save the list of environments.",
        "full": "Too many environments. Remove one first.",
        "unknown": "This environment is not in the list.",
    }

    def __init__(self, code: str, detail: str | None = None):
        super().__init__(detail or self.MESSAGES[code])
        self.code = code


# ---------------------------------------------------------------------------
# Pairing links and origins
# ---------------------------------------------------------------------------


def normalized_origin(url: str) -> str:
    """`scheme://host[:port]/`, from a URL with no path, query, fragment or credentials."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError as error:
        raise RemoteError("invalidLink") from error
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise RemoteError("invalidLink")
    if parts.username is not None or parts.password is not None:
        raise RemoteError("invalidLink")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise RemoteError("invalidLink")
    host = parts.hostname.lower()
    if parts.scheme == "http" and host == "localhost":
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    default = 443 if parts.scheme == "https" else 80
    netloc = host if port is None or port == default else f"{host}:{port}"
    return f"{parts.scheme}://{netloc}/"


def _single(fields: list[tuple[str, str]], name: str) -> str:
    if len(fields) != 1 or fields[0][0] != name:
        raise RemoteError("invalidLink")
    return fields[0][1]


def parse_pairing_link(text) -> tuple[str, str]:
    """(origin, pairing code) from a pairing link. A bare code or a plain address is refused:
    the link names the server the code belongs to."""
    if not isinstance(text, str) or len(text) > MAX_INPUT:
        raise RemoteError("invalidLink")
    text = text.strip()
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError as error:
        raise RemoteError("invalidLink") from error
    if parts.scheme == "t3code":
        if parts.netloc != "pair" or parts.path not in ("", "/"):
            raise RemoteError("invalidLink")
        inner = _single(urllib.parse.parse_qsl(parts.query, keep_blank_values=True), "pairingUrl")
        if inner.strip().lower().startswith("t3code:"):
            raise RemoteError("invalidLink")
        return parse_pairing_link(inner)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise RemoteError("invalidLink")
    if parts.scheme == "https" and (parts.hostname or "").lower() == "app.t3.codes" and parts.path == "/pair":
        host = _single(urllib.parse.parse_qsl(parts.query, keep_blank_values=True), "host").strip()
        if not host:
            raise RemoteError("invalidLink")
        target = host if "://" in host else f"https://{host}"
        if parts.fragment:
            target = target.split("#", 1)[0] + "#" + parts.fragment
        return parse_pairing_link(target)
    code = None
    if parts.fragment:
        code = _single(urllib.parse.parse_qsl(parts.fragment, keep_blank_values=True), "token")
    path = parts.path
    if code is not None and path == "/pair":
        path = "/"
    origin = normalized_origin(urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, "")))
    return origin, pairing_code(code)


def pairing_code(code) -> str:
    if not isinstance(code, str):
        raise RemoteError("invalidCode")
    code = code.strip().upper()
    if len(code) != PAIRING_CODE_LENGTH or not set(code) <= PAIRING_ALPHABET:
        raise RemoteError("invalidCode")
    return code


# ---------------------------------------------------------------------------
# Destinations
# ---------------------------------------------------------------------------


def origin_parts(origin: str) -> tuple[str, str, int, bool]:
    """(host, Host header, port, https) of a normalized origin."""
    parts = urllib.parse.urlsplit(origin)
    secure = parts.scheme == "https"
    return parts.hostname or "", parts.netloc, parts.port or (443 if secure else 80), secure


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def is_tailnet_address(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    network = TAILNET_V4 if address.version == 4 else TAILNET_V6
    return address in network


def tailscale_peer(address: str, binaries=TAILSCALE_BINARIES) -> bool:
    """True when the running Tailscale daemon lists `address` as one of a peer's addresses."""
    for binary in binaries:
        if not os.path.isfile(binary):
            continue
        try:
            result = subprocess.run([binary, "status", "--json"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode != 0 or len(result.stdout) > 4 * 1024 * 1024:
            return False
        try:
            status = json.loads(result.stdout)
        except ValueError:
            return False
        if not isinstance(status, dict) or status.get("BackendState") != "Running":
            return False
        peers = status.get("Peer") if isinstance(status.get("Peer"), dict) else {}
        return any(isinstance(peer, dict) and isinstance(peer.get("TailscaleIPs"), list)
                   and address in peer["TailscaleIPs"] for peer in peers.values())
    return False


def check_destination(origin: str, verify_peer=tailscale_peer) -> None:
    """HTTPS anywhere. Plain HTTP only to loopback, or to a Tailscale peer the local daemon
    confirms: a private LAN address or an unverified 100.64/10 address is refused."""
    host, _, _, secure = origin_parts(origin)
    if secure:
        return
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError as error:
        raise RemoteError("destination") from error
    if address.is_loopback:
        return
    if is_tailnet_address(str(address)):
        if verify_peer(str(address)):
            return
        raise RemoteError("tailscale")
    raise RemoteError("destination")


def http_connection(origin: str, timeout: float = HTTP_TIMEOUT) -> http.client.HTTPConnection:
    """A connection to the origin itself: http.client uses no proxy and follows no redirect.
    HTTPS checks the certificate against the system's trust store."""
    host, _, port, secure = origin_parts(origin)
    host = host.strip("[]")
    if secure:
        return http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl.create_default_context())
    return http.client.HTTPConnection(host, port, timeout=timeout)


# ---------------------------------------------------------------------------
# Environment descriptor
# ---------------------------------------------------------------------------


def _text(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def parse_descriptor(value) -> dict:
    if not isinstance(value, dict):
        raise RemoteError("descriptor")
    environment, label = _text(value.get("environmentId")), _text(value.get("label"))
    platform = value.get("platform") if isinstance(value.get("platform"), dict) else None
    version = value.get("orchestrationProtocolVersion", 1)
    if (environment is None or label is None or len(label.encode("utf-8")) > MAX_LABEL or platform is None
            or platform.get("os") not in OS_NAMES or platform.get("arch") not in ARCH_NAMES
            or _text(value.get("serverVersion")) is None or not isinstance(value.get("capabilities"), dict)):
        raise RemoteError("descriptor")
    if isinstance(version, bool) or version not in PROTOCOL_VERSIONS:
        raise RemoteError("protocol")
    machine = platform.get("machine")
    return {
        "environmentId": environment, "label": label, "os": platform["os"],
        "machine": machine if machine in MACHINES else None, "protocol": version,
        "serverVersion": _text(value.get("serverVersion")),
    }


def describe(origin: str, verify_peer=tailscale_peer) -> dict:
    """The environment this origin serves. Raises OSError when it cannot be reached."""
    check_destination(origin, verify_peer)
    conn = http_connection(origin)
    try:
        conn.request("GET", DESCRIPTOR_PATH, headers={"Accept": "application/json"})
        response = conn.getresponse()
        data = response.read(MAX_DESCRIPTOR + 1)
        status = response.status
    finally:
        conn.close()
    if status != 200 or len(data) > MAX_DESCRIPTOR:
        raise RemoteError("descriptor")
    try:
        return parse_descriptor(json.loads(data))
    except ValueError as error:
        raise RemoteError("descriptor") from error


# ---------------------------------------------------------------------------
# Saved connections
# ---------------------------------------------------------------------------


def config_dir() -> str:
    override = os.environ.get("T3_CODE_BAR_CONFIG_DIR", "").strip()
    if override:
        return os.path.expanduser(override)
    base = os.environ.get("XDG_CONFIG_HOME", "").strip() or os.path.expanduser("~/.config")
    return os.path.join(base, "t3codebar")


class Registry:
    """The environments the user paired, without credentials. A file this widget cannot read
    is left alone and the list becomes read-only, so a bad edit is never overwritten."""

    FIELDS = ("id", "label", "origin", "machine", "os", "protocol")

    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(config_dir(), "connections.json")
        self.environments: list[dict] = []
        self.error: str | None = None
        self.load()

    def load(self) -> None:
        self.environments, self.error = [], None
        try:
            with open(self.path, "rb") as handle:
                data = handle.read(MAX_DESCRIPTOR + 1)
        except FileNotFoundError:
            return
        except OSError:
            self.error = RemoteError.MESSAGES["registry"]
            return
        try:
            value = json.loads(data)
            if len(data) > MAX_DESCRIPTOR or not isinstance(value, dict) or value.get("version") != 1:
                raise ValueError
            entries = []
            for item in value.get("environments", []):
                entry = {key: item.get(key) for key in self.FIELDS}
                if not _text(entry["id"]) or not _text(entry["label"]):
                    raise ValueError
                entry["origin"] = normalized_origin(entry["origin"])
                entry["protocol"] = entry["protocol"] if entry["protocol"] in PROTOCOL_VERSIONS else 1
                entry["machine"] = entry["machine"] if entry["machine"] in MACHINES else None
                entries.append(entry)
            if len(entries) > MAX_ENVIRONMENTS or len({e["id"] for e in entries}) != len(entries):
                raise ValueError
        except (ValueError, TypeError, AttributeError, RemoteError):
            self.error = f"{RemoteError.MESSAGES['registry']} Fix or delete {self.path}."
            return
        self.environments = entries

    def find(self, environment: str) -> dict | None:
        return next((dict(e) for e in self.environments if e["id"] == environment), None)

    def owner_of(self, origin: str) -> str | None:
        return next((e["id"] for e in self.environments if e["origin"] == origin), None)

    def upsert(self, descriptor: dict, origin: str) -> dict:
        if self.error:
            raise RemoteError("registry", self.error)
        owner = self.owner_of(origin)
        if owner is not None and owner != descriptor["environmentId"]:
            raise RemoteError("conflict")
        entry = {"id": descriptor["environmentId"], "label": descriptor["label"], "origin": origin,
                 "machine": descriptor.get("machine"), "os": descriptor.get("os"),
                 "protocol": descriptor.get("protocol", 1)}
        others = [e for e in self.environments if e["id"] != entry["id"]]
        if len(others) >= MAX_ENVIRONMENTS:
            raise RemoteError("full")
        self._write(others + [entry])
        return dict(entry)

    def remove(self, environment: str) -> None:
        if self.error:
            raise RemoteError("registry", self.error)
        self._write([e for e in self.environments if e["id"] != environment])

    def _write(self, entries: list[dict]) -> None:
        folder = os.path.dirname(self.path)
        try:
            os.makedirs(folder, mode=0o700, exist_ok=True)
            fd, temp = tempfile.mkstemp(prefix=".connections.", dir=folder)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump({"version": 1, "environments": entries}, handle, indent=2)
                os.chmod(temp, 0o600)
                os.replace(temp, self.path)
            except BaseException:
                try:
                    os.unlink(temp)
                except OSError:
                    pass
                raise
        except OSError as error:
            raise RemoteError("registry") from error
        self.environments = [dict(e) for e in entries]


# ---------------------------------------------------------------------------
# Grants in the keyring
# ---------------------------------------------------------------------------


class RemoteKeyring:
    """One environment's grant, in the desktop keyring through secret-tool. The same store
    interface as `t3live.KeyringStore`, so a remote connection reuses the local one's code."""

    def __init__(self, environment: str, origin: str, error_type=RemoteError):
        self.tool = shutil.which("secret-tool")
        self.environment, self.origin = environment, origin
        self.error_type = error_type
        self.attributes = ["service", KEYRING_SERVICE, "environment", environment, "origin", origin]

    def _fail(self, error=None):
        raise (self.error_type("storage") if self.error_type is not RemoteError
               else RemoteError("registry", "Could not use the keyring.")) from error

    def load(self) -> dict | None:
        if not self.tool:
            return None
        try:
            result = subprocess.run([self.tool, "lookup", *self.attributes], capture_output=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as error:
            self._fail(error)
        if result.returncode != 0 or not result.stdout.strip():
            return None
        try:
            data = json.loads(result.stdout)
            if (data.get("version") != 1 or data.get("environment") != self.environment
                    or data.get("origin") != self.origin or not isinstance(data.get("token"), str)):
                return None
            expires_at = data.get("expiresAt")
            return {"origin": self.origin, "environment": self.environment, "token": data["token"],
                    "expiresAt": None if expires_at is None else float(expires_at),
                    "scopes": [s for s in data.get("scopes", []) if isinstance(s, str)]}
        except (ValueError, TypeError, AttributeError):
            return None

    def save(self, credential: dict) -> None:
        if not self.tool:
            self._fail()
        secret = json.dumps({"version": 1, "environment": self.environment, "origin": self.origin,
                             "token": credential["token"], "expiresAt": credential.get("expiresAt"),
                             "scopes": credential.get("scopes", [])})
        try:
            result = subprocess.run([self.tool, "store", f"--label=T3 Code Bar · {self.environment}", *self.attributes],
                                    input=secret.encode("utf-8"), capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as error:
            self._fail(error)
        if result.returncode != 0:
            self._fail()

    def delete(self) -> None:
        if not self.tool:
            return
        try:
            subprocess.run([self.tool, "clear", *self.attributes], capture_output=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as error:
            self._fail(error)


# ---------------------------------------------------------------------------
# Thread links
# ---------------------------------------------------------------------------


def link_component(value: str) -> str:
    return urllib.parse.quote(value, safe="-._~")


def thread_links(origin: str | None, environment: str, thread: str) -> dict:
    path = f"{link_component(environment)}/{link_component(thread)}"
    return {"app": f"t3code://threads/{path}", "browser": f"{origin}{path}" if origin else None}


# ---------------------------------------------------------------------------
# Usage across environments (bar-usage merge.rs)
# ---------------------------------------------------------------------------

ECHOED_FIELDS = ("sinceDay", "untilDay", "timeZone", "resolution", "sinceTime", "untilTime")


def summary_matches(summary, request: dict) -> bool:
    """A summary counts only for the exact window it was asked for."""
    if not isinstance(summary, dict) or not isinstance(summary.get("buckets"), list):
        return False
    for field in ECHOED_FIELDS:
        if field in request and field in summary and summary[field] != request[field]:
            return False
    if request.get("resolution") == "hour":
        return all(isinstance(b, dict) and b.get("hourStart") for b in summary["buckets"])
    return True


def _fingerprint(source) -> tuple | None:
    fingerprint = source.get("fingerprint") if isinstance(source, dict) else None
    if not isinstance(fingerprint, dict):
        return None
    key = tuple(fingerprint.get(k) for k in ("hostId", "provider", "resolvedHomePath", "volumeId"))
    return key if all(isinstance(k, str) for k in key) else None


def _read_at(summary: dict) -> float:
    text = summary.get("readAt")
    try:
        return float(calendar.timegm(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S"))) if isinstance(text, str) else 0.0
    except ValueError:
        return 0.0


def merge_summaries(contributions: list[tuple[str, str, dict]]) -> tuple[dict, list[str]]:
    """One summary from several environments' `(id, label, summary)`. Each usage source
    (host, provider, home folder, volume) belongs to one environment: the newest reader with
    an ok status, then partial, then failed. Only the owner's buckets for that source count, so
    a folder two servers both scan is not counted twice. The other readers are named in the
    returned notes. A newer partial reader adding cells its owner lacks is not ported."""
    ordered = sorted(contributions, key=lambda c: (-_read_at(c[2]), c[0].casefold(), c[0]))
    owners: dict[tuple, str] = {}
    duplicates: list[str] = []
    for status in ("ok", "partial", "failed"):
        for environment, label, summary in ordered:
            for source in summary.get("sources") or []:
                key = _fingerprint(source)
                if key is None or source.get("status") != status:
                    continue
                if key in owners:
                    duplicates.append(f"{label}: {key[2]}")
                else:
                    owners[key] = environment
    buckets, sources, read_times = [], [], []
    for environment, _, summary in contributions:
        owned = [s for s in summary.get("sources") or []
                 if _fingerprint(s) is not None and s.get("status") != "missing"
                 and owners.get(_fingerprint(s)) == environment]
        providers = {s["fingerprint"]["provider"] for s in owned}
        paths = {(s["fingerprint"]["provider"], s["fingerprint"]["resolvedHomePath"]) for s in owned}
        for bucket in summary.get("buckets") or []:
            if not isinstance(bucket, dict):
                continue
            path = bucket.get("sourcePath")
            if isinstance(path, str):
                owns = (bucket.get("provider"), path) in paths
            else:
                owns = bucket.get("provider") in providers
            if owns:
                buckets.append(bucket)
        sources += owned
        if isinstance(summary.get("readAt"), str):
            read_times.append(summary["readAt"])
    merged = {"buckets": buckets, "sources": sources, "readAt": min(read_times) if read_times else None}
    notes = [f"Counted once, from another environment: {', '.join(duplicates)}."] if duplicates else []
    return merged, notes
