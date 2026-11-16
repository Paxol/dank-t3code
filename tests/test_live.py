#!/usr/bin/env python3
"""The live connection against a fake T3 server on loopback.

The fake checks what the real server checks: the pairing code and scope on the
token exchange, which must carry no DPoP proof so the server issues a bearer
token, the bearer token on the ticket request, and the ticket on the WebSocket
upgrade. It then streams a config snapshot and a CLI Proxy source update, the
thread shells, and answers usage summaries when the token has the usage scope.
It expects an Ack per chunk. No real credentials, keyring or T3 server are used.
Run with:

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

PAIRING_CODE = "PAIR-CODE-123"
TICKET = "ticket-xyz"
EXPIRES_AT = time.time() + 30 * 86400


def session_token(scopes=("orchestration:read", "diagnostics:read"), expires_at=EXPIRES_AT, method="bearer-access-token"):
    """A token shaped like T3's: base64url claims, a dot, and a signature only the server checks."""
    claims = {"v": 1, "kind": "session", "sid": "s1", "sub": "cli-issued-session", "scopes": list(scopes),
              "method": method, "iat": int(time.time() * 1000), "exp": int(expires_at * 1000)}
    return base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode() + ".c2lnbmF0dXJl"


TOKEN = session_token()


def provider(driver, used, email):
    return {
        "driver": driver, "instanceId": driver, "displayName": driver.title(), "enabled": True, "installed": True,
        "auth": {"status": "authenticated", "email": email, "label": "Pro"},
        "usageLimits": {"checkedAt": "2026-11-06T21:00:00.000Z", "windows": [
            {"id": "five_hour", "kind": "session", "label": "Session", "usedPercent": used,
             "resetsAt": "2099-01-01T00:00:00.000Z", "windowDurationMins": 300}]},
    }


NOW = time.time()


def iso(offset_seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(NOW + offset_seconds))


def shell(ident, title, **fields):
    thread = {
        "id": ident, "projectId": "p1", "title": title, "providerInstanceId": "claudeAgent",
        "modelSelection": {"provider": "claudeAgent", "model": "claude-opus-4-1"}, "branch": None,
        "worktreePath": None, "lineage": {"parentThreadId": None, "relationshipToParent": None, "rootThreadId": ident},
        "latestRunId": "r1", "latestRunStartedAt": iso(-600), "latestRunCompletedAt": iso(-300), "activeRunId": None,
        "activityRunStatus": None, "activityRunStartedAt": None, "status": "completed", "pendingRuntimeRequest": None,
        "pendingGuestMessageCount": 0, "latestVisibleMessage": None, "latestUserMessageAt": iso(-700),
        "providerInstanceHistory": ["claudeAgent"], "goal": None, "itemCount": 3, "visibleItemCount": 3,
        "createdAt": iso(-3600), "updatedAt": iso(-300), "archivedAt": None, "settledOverride": None, "settledAt": None,
        "lastVisitedAt": iso(-200), "deletedAt": None,
    }
    thread.update(fields)
    return thread


SHELL_SNAPSHOT = {"kind": "snapshot", "snapshot": {
    "schemaVersion": 2, "snapshotSequence": 10, "archivedThreads": [],
    "projects": [{"id": "p1", "title": "Bar", "workspaceRoot": "/nowhere/bar", "projectIcon":
                  {"kind": "monogram", "text": "BR", "color": "teal"}, "scripts": [],
                  "createdAt": iso(-9000), "updatedAt": iso(-9000)}],
    "threads": [
        shell("t-approval", "Needs approval", pendingRuntimeRequest={"id": "q", "kind": "command", "createdAt": iso(-10)}),
        shell("t-working", "Working", activityRunStatus="running", activityRunStartedAt=iso(-120), status="running",
              activeRunId="r2", latestRunCompletedAt=None, pullRequests=[{"number": 42, "source": "created"}]),
        shell("t-limited", "Limited", status="failed", lastErrorClass="usage_limit", usageLimitResetAt=iso(3600)),
        shell("t-done", "Done", lastVisitedAt=iso(-400)),
        shell("t-sub", "Subagent", lineage={"parentThreadId": "t-done", "relationshipToParent": "subagent",
                                            "rootThreadId": "t-done"}, activityRunStatus="running"),
        shell("t-settled", "Settled", settledAt=iso(-50), settledOverride="settled"),
        shell("t-overridden", "Settled by override", settledOverride="settled"),
        shell("t-delegated", "Delegated", delegatedFrom={"threadId": "t-done"}),
        shell("t-orphan", "No project", projectId="gone", latestRunId=None, latestRunCompletedAt=None, status="idle"),
    ]}}


def usage_summary(request):
    hourly = request.get("resolution") == "hour"
    bucket = {"day": request["untilDay"], "provider": "claude", "model": "claude-opus-4-1", "sourcePath": "/a.jsonl",
              "totals": {"uncachedInputTokens": 100, "cachedInputTokens": 900, "cacheCreationTokens": 0,
                         "outputTokens": 50, "reasoningTokens": 0},
              "costUsd": 1.5, "cacheSavingsUsd": 0.25,
              "categoryCostUsd": {"input": 0.5, "cacheRead": 0.25, "cacheWrite": 0, "output": 0.75},
              "costSource": "modelPriced", "records": 4, "unpricedRecords": 0, "sessions": 1}
    other = dict(bucket, sourcePath="/b.jsonl", costUsd=0.5, fastCostUsd=0.5, sessions=1)
    codex = dict(bucket, provider="codex", model="gpt-5", costUsd=2.0, categoryCostUsd=None, records=2, unpricedRecords=2,
                 costSource="unpriced")
    if hourly:
        last_hour = t3live.t3bar.parse_date(request["untilTime"]) - 1800_000
        start = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t3live.t3bar.parse_date(request["sinceTime"]) / 1000
                                                                     + ((last_hour - t3live.t3bar.parse_date(request["sinceTime"])) // 3_600_000) * 3600))
        for item in (bucket, other, codex):
            item["hourStart"] = start
    return {"contractVersion": 1, "readAt": iso(-5), "timeZone": request["timeZone"], "sinceDay": request["sinceDay"],
            "untilDay": request["untilDay"], "buckets": [bucket, other, codex],
            "sources": [{"fingerprint": {"hostId": "h", "provider": "claude", "resolvedHomePath": "/h", "volumeId": "1"},
                         "status": "ok", "scannedFiles": 2, "skippedFiles": 1, "malformedRecords": 3,
                         "distinctSessions": 1, "message": None},
                        {"fingerprint": {"hostId": "h", "provider": "codex", "resolvedHomePath": "/h", "volumeId": "1"},
                         "status": "ok", "scannedFiles": 1, "skippedFiles": 0, "malformedRecords": 0,
                         "distinctSessions": 1, "message": None}],
            "pricing": {"status": "fresh", "source": "litellm", "fetchedAt": None, "knownModels": 10},
            "scanDurationMs": 12}


class FakeT3:
    def __init__(self, token_type="Bearer", scope=None, grants="orchestration:read diagnostics:read", tokens=(TOKEN,),
                 code=PAIRING_CODE, descriptor=None):
        self.token_type, self.grants = token_type, grants.split()
        self.code = code
        self.descriptor = descriptor  # what /.well-known/t3/environment answers; None for 404
        self.projections, self.socket_queries = [], []
        self.summary = usage_summary  # answers server.getUsageSummary
        self.scope = scope
        self.tokens = set(tokens)  # the tokens the server knows; removing one revokes it
        self.acks, self.shell_acks, self.pings, self.requests = 0, 0, 0, []
        self.shell_payload, self.usage_requests = None, []
        self.proofs = []
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
                fake.requests.append(self.path)
                fake.proofs.append(self.headers.get("DPoP"))
                if self.path == "/oauth/token":
                    form = dict(urllib.parse.parse_qsl(body))
                    if form.get("subject_token") != fake.code:
                        return self.reply(400, {"error": "invalid_grant"})
                    assert form["scope"] == "orchestration:read diagnostics:read", form["scope"]
                    assert form["subject_token_type"] == "urn:t3:params:oauth:token-type:environment-bootstrap"
                    # The server grants the requested scopes the code allows. A DPoP proof would make
                    # it a one-hour DPoP token; without one it is a bearer token for 30 days.
                    granted = fake.scope or " ".join(s for s in form["scope"].split() if s in fake.grants)
                    return self.reply(200, {"access_token": TOKEN, "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
                                            "token_type": fake.token_type, "expires_in": 30 * 86400, "scope": granted})
                if self.path == "/api/auth/websocket-ticket/rpc":
                    auth = self.headers.get("Authorization") or ""
                    if not auth.startswith("Bearer ") or auth[7:] not in fake.tokens:
                        return self.reply(401, {"code": "auth_invalid"})
                    return self.reply(200, {"ticket": TICKET, "expiresAt": "2099-01-01T00:00:00.000Z"})
                self.reply(404, {})

            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                query = dict(urllib.parse.parse_qsl(url.query))
                if url.path == "/.well-known/t3/environment":
                    return self.reply(200, fake.descriptor) if fake.descriptor else self.reply(404, {})
                # The protocol parameter goes only to servers whose descriptor says version 2.
                protocol = (fake.descriptor or {}).get("orchestrationProtocolVersion", 2)
                fake.socket_queries.append(query)
                if (url.path != "/ws" or query.get("wsTicket") != TICKET
                        or query.get("orchestrationProtocol") != ("2" if protocol == 2 else None)):
                    return self.reply(401, {})
                assert "dpop" not in query, "a bearer socket carries no proof"
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
                                                               "settings": {"secret": "ignored"},
                                                               "shellThreadFamilies": True, "shellOlderSettled": True}},
                {"version": 1, "type": "settingsUpdated", "payload": {"settings": {}}}]}])
            while True:
                opcode, payload = self.read(sock)
                if opcode == 0x8:
                    return
                frame = json.loads(payload)
                if frame["_tag"] == "Request" and frame["tag"] == "orchestration.subscribeShell":
                    self.shell_payload, shell_id = frame["payload"], frame["id"]
                    # As T3 Code 0.1.8 does: the snapshot, then an enrichment with no threads and the
                    # projects whose repository identity was resolved.
                    enrichment = {"kind": "snapshot", "resolvedRepositoryIdentityRoots": ["/nowhere/bar"], "snapshot": dict(
                        SHELL_SNAPSHOT["snapshot"], threads=[],
                        projects=[dict(SHELL_SNAPSHOT["snapshot"]["projects"][0], title="Bar (resolved)")])}
                    self.send(sock, {"_tag": "Chunk", "requestId": shell_id, "values": [SHELL_SNAPSHOT, enrichment]})
                    self.send(sock, {"_tag": "Chunk", "requestId": shell_id, "values": [
                        {"kind": "thread.updated", "sequence": 11, "location": "archive",
                         "thread": shell("t-done", "Done", archivedAt=iso(-1))},
                        {"kind": "synchronized"}]})
                elif frame["_tag"] == "Request" and frame["tag"] == "orchestration.getThreadProjection":
                    self.projections.append(frame["payload"]["threadId"])
                    self.send(sock, {"_tag": "Exit", "requestId": frame["id"], "exit": {"_tag": "Success", "value": {
                        "thread": {"id": frame["payload"]["threadId"]}, "messages": [
                            {"role": "assistant", "text": "Older reply", "createdAt": iso(-900), "updatedAt": iso(-900)},
                            {"role": "user", "text": "Question", "createdAt": iso(-800), "updatedAt": iso(-800)},
                            {"role": "assistant", "text": "## Latest\n**Done** with `it`.", "createdAt": iso(-700),
                             "updatedAt": iso(-650)}]}}})
                elif frame["_tag"] == "Request" and frame["tag"] == "server.getUsageSummary":
                    self.usage_requests.append(frame["payload"])
                    if "diagnostics:read" in self.grants:
                        exit_value = {"_tag": "Success", "value": self.summary(frame["payload"])}
                    else:
                        exit_value = {"_tag": "Failure", "cause": {"_tag": "Fail", "error": {
                            "_tag": "EnvironmentAuthorizationError",
                            "message": "The authenticated token is missing required scope: diagnostics:read."}}}
                    self.send(sock, {"_tag": "Exit", "requestId": frame["id"], "exit": exit_value})
                elif frame["_tag"] == "Ack":
                    if frame["requestId"] == rid:
                        self.acks += 1
                    else:
                        assert frame["requestId"] == shell_id, frame
                        self.shell_acks += 1
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


def usage_reply(connection, period, seconds=10):
    """Sends a usage command and waits for its reply on the event queue."""
    connection.usage(period, period * 10)
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            kind, payload = connection.events.get(timeout=0.2)
        except queue.Empty:
            continue
        if kind == "usage" and payload["seq"] == period * 10:
            return payload
    return None


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
    check("pairing sends no DPoP proof", fake.requests[0] == "/oauth/token" and not any(fake.proofs))
    check("remembered token stored without a key",
          connection.store.saved is not None and connection.store.saved["token"] == TOKEN
          and "privateKey" not in connection.store.saved)
    check("expiry from the token's own claim", abs(connection.credential["expiresAt"] - EXPIRES_AT) < 1)
    check("pairing code never stored", PAIRING_CODE not in json.dumps(connection.store.saved))
    check("token carries the usage scope", connection.credential["scopes"] == ["orchestration:read", "diagnostics:read"]
          and connection.store.saved["scopes"] == ["orchestration:read", "diagnostics:read"])

    # Threads from the shell stream.
    check("threads go live", wait_for(connection, lambda c: c.view()["live"]["threads"]))
    check("shell asks to omit idle subagents and older settled threads",
          fake.shell_payload == {"omitIdleSubagents": True, "omitOlderSettled": True})
    check("acks every shell chunk", wait_for(connection, lambda c: fake.shell_acks >= 2))
    check("archived thread leaves the inbox", wait_for(connection, lambda c: all(
        t["id"] != "t-done" for t in (c.inbox() or {"threads": [{"id": "t-done"}]})["threads"])))
    inbox = connection.inbox()
    by_id = {t["id"]: t for t in inbox["threads"]}
    check("inbox is live", inbox["live"] and inbox["serverOnline"] and inbox["source"] == "live")
    check("enrichment snapshot keeps the threads and updates the project",
          len(inbox["threads"]) == 4 and by_id["t-approval"]["projectTitle"] == "Bar (resolved)")
    check("subagent and settled threads are left out", "t-sub" not in by_id and "t-settled" not in by_id)
    check("settled-override and delegated threads are left out",
          "t-overridden" not in by_id and "t-delegated" not in by_id)
    check("approval from the pending request", by_id["t-approval"]["status"] == "approval")
    check("working from the active run", by_id["t-working"]["status"] == "working"
          and by_id["t-working"]["statusSince"] is not None)
    check("pull request number", by_id["t-working"]["prNumber"] == 42)
    check("usage limit with its reset", by_id["t-limited"]["status"] == "limited"
          and by_id["t-limited"]["limitResetAt"] is not None)
    check("thread without runs waits", by_id["t-orphan"]["status"] == "waiting"
          and by_id["t-orphan"]["projectTitle"] == "No Project")
    check("project badge from the shell", by_id["t-approval"]["badge"] == {"kind": "monogram", "text": "BR",
                                                                             "color": "#2dd4bf"})
    check("summary from live threads", inbox["summary"]["status"] == "approval" and inbox["summary"]["badge"] == 1)

    # Cost and tokens from the usage summary.
    week = usage_reply(connection, 7)
    check("usage reply arrives", week is not None and week["error"] is None and week["report"] is not None)
    report = week["report"]
    check("usage request window", fake.usage_requests[0]["resolution"] == "day"
          and fake.usage_requests[0]["timeZone"] and "sinceTime" not in fake.usage_requests[0])
    check("usage totals", abs(report["cost"] - 4.0) < 1e-9 and report["processed"] == 3150
          and report["tokens"]["cached"] == 2700)
    check("usage is live", report["live"] and report["state"] == "ok" and report["period"] == 7)
    check("usage providers and models", [p["kind"] for p in report["providers"]] == ["codex", "claude"]
          and report["models"][0]["name"] == "claude-opus-4-1" and report["models"][1]["unpriced"])
    check("usage sessions from sources", report["sessions"] == 2)
    check("usage counts skipped files and bad lines", report["rejectedFiles"] == 1 and report["malformed"] == 3)
    check("usage chart puts today last", len(report["chart"]) == 7 and sum(v["cost"] for v in report["chart"][-1]["values"]) == 4.0)
    check("usage cost by speed", abs(report["costBySpeed"]["fast"] - 0.5) < 1e-9)
    day = usage_reply(connection, 1)
    check("24h usage is hourly", day is not None and day["report"] is not None and day["report"]["hourly"]
          and fake.usage_requests[-1]["resolution"] == "hour" and "sinceTime" in fake.usage_requests[-1]
          and len(day["report"]["chart"]) == 24 and sum(v["cost"] for v in day["report"]["chart"][-1]["values"]) == 4.0)
    check("pings the stream", wait_for(connection, lambda c: fake.pings >= 1, 8))

    # Reconnect with the remembered token, as at launch.
    saved = connection.store.saved
    connection.stop()
    check("stop pauses", connection.status == "paused")
    again = make_connection()
    again.store.saved = saved
    check("stop drops the live inbox", connection.inbox() is None)
    again.reconnect_saved(fake.port, quiet=True)
    check("reconnects with the saved token", wait_for(again, lambda c: c.status == "live"))
    check("saved token keeps its scopes", again.view()["live"]["usage"])
    check("saved token is not exchanged again", fake.requests.count("/oauth/token") == 1)
    again.disconnect()
    check("disconnect forgets the token", again.status == "cacheOnly" and again.store.saved is None and again.data is None)

    # Saved token for another port: T3 Code moved, and the token follows it.
    other = make_connection()
    other.store.saved = dict(saved, port=fake.port + 1 if fake.port < 65535 else 1)
    other.reconnect_saved(fake.port)
    check("saved token follows the server's port", wait_for(other, lambda c: c.status == "live")
          and other.store.saved["port"] == fake.port)
    other.stop()

    # Expired saved token: refused and forgotten.
    expired = make_connection()
    expired.store.saved = dict(saved, expiresAt=time.time() - 1)
    expired.reconnect_saved(fake.port)
    check("expired token asks to pair again", expired.status == "failed" and "expired" in expired.message)
    check("expired token is deleted", expired.store.saved is None)

    # A saved token without an expiry is kept until the server refuses it.
    lasting = make_connection()
    lasting.store.saved = dict(saved, expiresAt=None)
    lasting.reconnect_saved(fake.port)
    check("token without expiry connects", wait_for(lasting, lambda c: c.status == "live"))
    lasting.stop()

    # An access token from `t3 auth session issue`, pasted instead of a code.
    exchanges = fake.requests.count("/oauth/token")
    pasted = make_connection()
    pasted.pair(fake.port, " " + TOKEN + "\n", True)
    check("pasted access token connects", wait_for(pasted, lambda c: c.view()["live"]["threads"]))
    check("pasted token is not exchanged", fake.requests.count("/oauth/token") == exchanges)
    check("pasted token's scopes come from its claims", pasted.view()["live"]["usage"]
          and pasted.store.saved["token"] == TOKEN)
    pasted.stop()
    for token, name in [(session_token(scopes=("orchestration:read", "orchestration:operate")), "token that can change T3 refused"),
                        (session_token(expires_at=time.time() - 60), "expired pasted token refused")]:
        refused = make_connection()
        refused.pair(fake.port, token, False)
        check(name, wait_for(refused, lambda c: c.status == "failed") and refused.store.saved is None
              and fake.requests.count("/oauth/token") == exchanges)

    # Revoked on the server: the ticket request is refused, and the saved token is deleted.
    revoked_token = session_token(scopes=("orchestration:read",))
    fake.tokens.add(revoked_token)
    revoked = make_connection()
    revoked.pair(fake.port, revoked_token, True)
    check("token goes live before revocation", wait_for(revoked, lambda c: c.status == "live")
          and revoked.store.saved is not None)
    fake.tokens.discard(revoked_token)
    revoked.stop()
    revoked.reconnect_saved(fake.port)
    check("revoked token says so", wait_for(revoked, lambda c: c.status == "failed") and "revoked" in revoked.message)
    check("revoked token is forgotten", revoked.store.saved is None and revoked.credential is None)

    # Following T3 Code's runtime file: pause while it is down, reconnect when it is back or moved.
    follower = make_connection()
    follower.store.saved = dict(saved)
    follower.follow_server(True, fake.port)
    check("server start reconnects the saved token", wait_for(follower, lambda c: c.status == "live"))
    follower.follow_server(False, fake.port)
    check("server stop pauses", follower.status == "paused" and follower.status_text() == t3live.OFFLINE_TEXT
          and follower.view()["live"]["retained"])
    follower.follow_server(True, fake.port)
    check("server back resumes", wait_for(follower, lambda c: c.status == "live"))
    moved = FakeT3()
    follower.follow_server(True, moved.port)
    check("server on a new port is followed", wait_for(follower, lambda c: c.status == "live" and moved.acks >= 1)
          and follower.credential["port"] == moved.port and follower.store.saved["port"] == moved.port)
    follower.stop()
    moved.server.shutdown()
    idle = make_connection()
    idle.follow_server(True, fake.port)
    check("server start without a saved token stays onboarding", idle.status == "cacheOnly")

    # A code with only View environment: threads and limits are live, usage is not.
    read_only = FakeT3(grants="orchestration:read")
    viewer = make_connection()
    viewer.pair(read_only.port, PAIRING_CODE, False)
    check("read-only code connects", wait_for(viewer, lambda c: c.view()["live"]["threads"]))
    check("read-only code has no live usage", not viewer.view()["live"]["usage"])
    refused_usage = usage_reply(viewer, 30)
    check("usage without the scope says so", refused_usage is not None and refused_usage["report"] is None
          and "View diagnostics" in refused_usage["error"] and not read_only.usage_requests)
    viewer.stop()
    read_only.server.shutdown()

    # Keyring entries from earlier versions.
    tool_dir = tempfile.mkdtemp()
    tool = os.path.join(tool_dir, "secret-tool")
    with open(tool, "w") as handle:
        handle.write(f"#!/bin/sh\ncat {os.path.join(tool_dir, 'entry.json')}\n")
    os.chmod(tool, 0o700)
    keyring = t3live.KeyringStore()
    keyring.tool = tool

    def stored(entry):
        with open(os.path.join(tool_dir, "entry.json"), "w") as handle:
            json.dump(entry, handle)
        return keyring.load()

    # Saved before usage was live: no scopes, so it reads as the read scope only.
    check("saved token without scopes reads as view only",
          stored({"port": fake.port, "token": TOKEN, "expiresAt": time.time() + 600})["scopes"] == ["orchestration:read"])
    # Saved by the DPoP version: bound to a key this version no longer has, so it reads as expired.
    check("DPoP-era saved token reads as expired", t3live.is_expired(stored(
        {"port": fake.port, "token": TOKEN, "expiresAt": time.time() + 600, "privateKey": base64.b64encode(b"k" * 32).decode()})))

    # Wrong code, DPoP-bound token, extra scope.
    wrong = make_connection()
    wrong.pair(fake.port, "nope", False)
    check("wrong code fails", wait_for(wrong, lambda c: c.status == "failed") and "rejected this pairing code" in wrong.message)
    for token_type, scope, name in [("DPoP", "orchestration:read", "DPoP-bound token refused"),
                                    ("Bearer", "orchestration:read orchestration:operate", "extra scope refused")]:
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
