#!/usr/bin/env python3
"""The live Limits connection against a fake T3 server on loopback.

The fake checks what the real server checks: the DPoP proof signature, method,
URL and access-token hash on every request, the pairing code and scope on the
token exchange, and the ticket on the WebSocket upgrade. It then streams a
config snapshot and a CLI Proxy source update, and expects an Ack per chunk.
No real credentials, keyring or T3 server are used. Run with:

    python3 -I tests/test_live.py
"""

import base64
import hashlib
import json
import os
import queue
import socket
import struct
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
import t3live  # noqa: E402

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature  # noqa: E402

PAIRING_CODE = "PAIR-CODE-123"
TOKEN = "access-token-abc"
TICKET = "ticket-xyz"


def unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def verify_proof(proof, method, url, token=None):
    head, body, signature = proof.split(".")
    header, payload = json.loads(unb64(head)), json.loads(unb64(body))
    assert header["typ"] == "dpop+jwt" and header["alg"] == "ES256", header
    jwk = header["jwk"]
    key = ec.EllipticCurvePublicNumbers(int.from_bytes(unb64(jwk["x"]), "big"), int.from_bytes(unb64(jwk["y"]), "big"),
                                        ec.SECP256R1()).public_key()
    raw = unb64(signature)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    key.verify(der, f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))
    assert payload["htm"] == method, payload
    assert payload["htu"] == url, (payload["htu"], url)
    assert abs(payload["iat"] - time.time()) < 60
    if token:
        assert payload["ath"] == base64.urlsafe_b64encode(hashlib.sha256(token.encode()).digest()).rstrip(b"=").decode()
    else:
        assert "ath" not in payload
    return jwk


def provider(driver, used, email):
    return {
        "driver": driver, "instanceId": driver, "displayName": driver.title(), "enabled": True, "installed": True,
        "auth": {"status": "authenticated", "email": email, "label": "Pro"},
        "usageLimits": {"checkedAt": "2026-11-06T21:00:00.000Z", "windows": [
            {"id": "five_hour", "kind": "session", "label": "Session", "usedPercent": used,
             "resetsAt": "2099-01-01T00:00:00.000Z", "windowDurationMins": 300}]},
    }


class FakeT3:
    def __init__(self, token_type="DPoP", scope="orchestration:read"):
        self.token_type, self.scope = token_type, scope
        self.acks, self.pings, self.requests = 0, 0, []
        self.key = None
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode()
                base = f"http://127.0.0.1:{fake.port}"
                fake.requests.append(self.path)
                if self.path == "/oauth/token":
                    jwk = verify_proof(self.headers["DPoP"], "POST", base + "/oauth/token")
                    form = dict(urllib.parse.parse_qsl(body))
                    if form.get("subject_token") != PAIRING_CODE:
                        return self.reply(401, {"code": "auth_invalid"})
                    assert form["scope"] == "orchestration:read"
                    assert form["subject_token_type"] == "urn:t3:params:oauth:token-type:environment-bootstrap"
                    fake.key = jwk
                    return self.reply(200, {"access_token": TOKEN, "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
                                            "token_type": fake.token_type, "expires_in": 3600, "scope": fake.scope})
                if self.path == "/api/auth/websocket-ticket/rpc":
                    if self.headers.get("Authorization") != f"DPoP {TOKEN}":
                        return self.reply(401, {"code": "auth_invalid"})
                    jwk = verify_proof(self.headers["DPoP"], "POST", base + self.path, TOKEN)
                    assert jwk == fake.key, "proof key differs from the bound key"
                    return self.reply(200, {"ticket": TICKET, "expiresAt": "2099-01-01T00:00:00.000Z"})
                self.reply(404, {})

            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                query = dict(urllib.parse.parse_qsl(url.query))
                if url.path != "/ws" or query.get("wsTicket") != TICKET or query.get("orchestrationProtocol") != "2":
                    return self.reply(401, {})
                verify_proof(query["dpop"], "GET", f"http://127.0.0.1:{fake.port}/ws", TICKET)
                key = self.headers["Sec-WebSocket-Key"].encode()
                accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()).decode()
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()
                self.wfile.flush()
                fake.serve_socket(self.connection)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @staticmethod
    def send(sock, value):
        data = json.dumps(value).encode()
        header = bytes([0x81]) + (bytes([len(data)]) if len(data) < 126 else bytes([126]) + struct.pack("!H", len(data)))
        sock.sendall(header + data)

    @staticmethod
    def read(sock):
        def exact(n):
            data = b""
            while len(data) < n:
                chunk = sock.recv(n - len(data))
                if not chunk:
                    raise ConnectionError
                data += chunk
            return data
        b0, b1 = exact(2)
        assert b1 & 0x80, "client frames must be masked"
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack("!H", exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", exact(8))[0]
        mask = exact(4)
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(exact(length)))
        return b0 & 0x0F, payload

    def serve_socket(self, sock):
        sock.settimeout(20)
        try:
            opcode, payload = self.read(sock)
            request = json.loads(payload)
            assert request["_tag"] == "Request" and request["tag"] == "subscribeServerConfig", request
            assert request["payload"]["usageLimitSources"] is True
            rid = request["id"]
            # Sources may arrive before the snapshot; they must not be dropped.
            self.send(sock, {"_tag": "Chunk", "requestId": rid, "values": [
                {"version": 1, "type": "usageLimitSourcesUpdated", "payload": {"sources": [
                    {"id": "proxy", "kind": "cliproxy", "label": "CLI Proxy", "checkedAt": "2026-11-06T21:00:00.000Z",
                     "accounts": [
                         {"id": "a.json", "driver": "codex", "email": "ann@example.com", "plan": "Plus",
                          "usageLimits": {"checkedAt": "2026-11-06T21:00:00.000Z", "windows": [
                              {"id": "w", "kind": "weekly", "label": "Weekly", "usedPercent": 40}]}},
                         {"id": "b.json", "driver": "codex", "email": "bob@example.com",
                          "usageLimits": {"checkedAt": "2026-11-06T21:00:00.000Z", "windows": [
                              {"id": "w", "kind": "weekly", "label": "Weekly", "usedPercent": 60}]}}]},
                    {"id": "empty", "kind": "cliproxy", "label": "Spare proxy", "checkedAt": "2026-11-06T21:00:00.000Z",
                     "accounts": []}]}}]})
            self.send(sock, [{"_tag": "Chunk", "requestId": rid, "values": [
                {"version": 1, "type": "snapshot", "config": {"providers": [provider("claudeAgent", 25, "me@example.com")],
                                                               "settings": {"secret": "ignored"}}},
                {"version": 1, "type": "settingsUpdated", "payload": {"settings": {}}}]}])
            while True:
                opcode, payload = self.read(sock)
                if opcode == 0x8:
                    return
                frame = json.loads(payload)
                if frame["_tag"] == "Ack":
                    assert frame["requestId"] == rid
                    self.acks += 1
                elif frame["_tag"] == "Ping":
                    self.pings += 1
                    self.send(sock, {"_tag": "Pong"})
                elif frame["_tag"] == "Interrupt":
                    return
        except (ConnectionError, OSError):
            return


class MemoryStore:
    tool = "memory"

    def __init__(self):
        self.saved = None

    def load(self):
        return self.saved

    def save(self, credential):
        self.saved = dict(credential)

    def delete(self):
        self.saved = None


def wait_for(connection, predicate, seconds=10):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate(connection):
            return True
        time.sleep(0.05)
    return False


def make_connection():
    state = tempfile.mkdtemp()
    connection = t3live.LiveConnection(os.path.join(state, "userdata"), True, queue.Queue())
    connection.store = MemoryStore()
    return connection


def main():
    failures = []

    def check(name, condition):
        print(("ok   " if condition else "FAIL ") + name)
        if not condition:
            failures.append(name)

    # Full flow
    fake = FakeT3()
    connection = make_connection()
    connection.pair(fake.port, "  " + PAIRING_CODE + "\n", True)
    check("pairs and goes live", wait_for(connection, lambda c: c.status == "live"))
    check("acks every chunk", wait_for(connection, lambda c: fake.acks >= 2))
    view = connection.view()
    groups = {g["driver"]: g for g in view["groups"]}
    check("live view is connected", view["live"]["connected"] and view["live"]["status"] == "live")
    check("provider from the snapshot", "claudeAgent" in groups and groups["claudeAgent"]["rows"][0]["remaining"] == 75)
    check("pooled CLI Proxy accounts", "codex" in groups and len(groups["codex"]["accounts"]) == 2)
    check("pooled accounts named by initials", sorted(a["name"] for a in groups["codex"]["accounts"]) == ["AE", "BE"])
    check("empty source notice", "Spare proxy: No accounts reported." in view["notices"])
    check("live footnote", view["footnote"] == t3live.LIVE_FOOTNOTE)
    check("remembered token stored with its key",
          connection.store.saved is not None and connection.store.saved["token"] == TOKEN
          and len(connection.store.saved["privateKey"]) == 32)
    check("pairing code never stored", PAIRING_CODE not in json.dumps({**connection.store.saved, "privateKey": ""}))
    check("pings the stream", wait_for(connection, lambda c: fake.pings >= 1, 8))

    # Reconnect with the remembered token, as at launch.
    saved = connection.store.saved
    connection.stop()
    check("stop pauses", connection.status == "paused")
    again = make_connection()
    again.store.saved = saved
    again.reconnect_saved(fake.port, quiet=True)
    check("reconnects with the saved token", wait_for(again, lambda c: c.status == "live"))
    check("saved token is not exchanged again", fake.requests.count("/oauth/token") == 1)
    again.disconnect()
    check("disconnect forgets the token", again.status == "cacheOnly" and again.store.saved is None and again.data is None)

    # Saved token for another port.
    other = make_connection()
    other.store.saved = dict(saved, port=fake.port + 1 if fake.port < 65535 else 1)
    other.reconnect_saved(fake.port)
    check("saved token for another port is refused", other.status == "failed" and "port" in other.message)

    # Expired saved token.
    expired = make_connection()
    expired.store.saved = dict(saved, expiresAt=time.time() - 1)
    expired.reconnect_saved(fake.port)
    check("expired token asks to pair again", expired.status == "failed" and "expired" in expired.message)

    # Wrong code, plain bearer token, extra scope.
    wrong = make_connection()
    wrong.pair(fake.port, "nope", False)
    check("wrong code fails", wait_for(wrong, lambda c: c.status == "failed") and "Pairing failed" in wrong.message)
    for token_type, scope, name in [("Bearer", "orchestration:read", "plain bearer token refused"),
                                    ("DPoP", "orchestration:read orchestration:operate", "extra scope refused")]:
        strict = FakeT3(token_type, scope)
        refused = make_connection()
        refused.pair(strict.port, PAIRING_CODE, False)
        check(name, wait_for(refused, lambda c: c.status == "failed") and refused.data is None)
        strict.server.shutdown()

    # Server goes away: retry, keep the last data, and merge it with saved files.
    fake.server.shutdown()
    fake.server.server_close()
    outage = make_connection()
    outage.store.saved = saved
    outage.reconnect_saved(fake.port)
    check("unreachable server retries", wait_for(outage, lambda c: c.status == "retrying", 5))
    outage.stop()

    print("OK" if not failures else f"{len(failures)} failed")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
