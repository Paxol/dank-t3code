#!/usr/bin/env python3
"""Remote environments: pairing-link parsing, destination checks, the environment
descriptor, the saved list, usage across environments, and the hub against fake T3
servers on loopback. No real keyring, Tailscale or T3 server is used. Run with:

    python3 -I tests/test_remote.py
"""

import json
import os
import queue
import stat
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
import t3live  # noqa: E402
import t3remote  # noqa: E402
import test_live  # noqa: E402
from test_live import FakeT3, iso  # noqa: E402

CODE = "ABCD2345EFGH"


def descriptor(environment, label, machine="server", protocol=2):
    return {"environmentId": environment, "label": label, "serverVersion": "0.1.8",
            "platform": {"os": "linux", "arch": "x64", "machine": machine},
            "orchestrationProtocolVersion": protocol, "capabilities": {}}


def summary_for(host, path="/home/me/.claude", cost=1.5):
    """A usage summary whose bucket names the folder its source scanned, as T3 Code's does."""
    def summary(request):
        bucket = {"day": request["untilDay"], "provider": "claude", "model": "claude-opus-4-1", "sourcePath": path,
                  "totals": {"uncachedInputTokens": 100, "cachedInputTokens": 0, "cacheCreationTokens": 0,
                             "outputTokens": 50, "reasoningTokens": 0},
                  "costUsd": cost, "records": 1, "unpricedRecords": 0, "sessions": 1}
        if request.get("resolution") == "hour":
            bucket["hourStart"] = request["sinceTime"]
        return {"contractVersion": 6, "readAt": iso(-5), "timeZone": request["timeZone"],
                "sinceDay": request["sinceDay"], "untilDay": request["untilDay"], "resolution": request["resolution"],
                "buckets": [bucket],
                "sources": [{"fingerprint": {"hostId": host, "provider": "claude", "resolvedHomePath": path,
                                             "volumeId": "v1"}, "status": "ok", "distinctSessions": 1}]}
    return summary


class MemoryKeyring:
    """Grants per environment and origin, as RemoteKeyring keeps them."""
    tool = "memory"
    saved: dict = {}

    def __init__(self, entry):
        self.key = (entry["id"], entry["origin"])

    def load(self):
        return MemoryKeyring.saved.get(self.key)

    def save(self, credential):
        MemoryKeyring.saved[self.key] = dict(credential)

    def delete(self):
        MemoryKeyring.saved.pop(self.key, None)


def wait(predicate, seconds=10):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def next_event(hub, kind, seconds=10):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            event, payload = hub.events.get(timeout=0.2)
        except queue.Empty:
            continue
        if event == kind:
            return payload
    return None


def remote(hub, environment):
    return next((r for r in hub.environments()["list"] if r["id"] == environment), None)


def main():
    failures = 0

    def check(name, condition):
        nonlocal failures
        print(("ok   " if condition else "FAIL ") + name)
        failures += 0 if condition else 1

    # ----- pairing links -----
    accepted = {
        f"https://server.example/pair#token={CODE}": ("https://server.example/", CODE),
        f"t3code://pair?pairingUrl=https%3A%2F%2Fserver.example%2Fpair%23token%3D{CODE}": ("https://server.example/", CODE),
        f"https://app.t3.codes/pair?host=https%3A%2F%2Fserver.example#token={CODE}": ("https://server.example/", CODE),
        f"https://app.t3.codes/pair?host=server.example:8443#token={CODE}": ("https://server.example:8443/", CODE),
        "http://localhost:3773/pair#token=abcd2345efgh": ("http://127.0.0.1:3773/", CODE),
        f"  https://Server.Example:443/#token={CODE}  ": ("https://server.example/", CODE),
    }
    for text, expected in accepted.items():
        check(f"link accepted: {text.strip()[:60]}", t3remote.parse_pairing_link(text) == expected)
    rejected = [
        CODE, "https://server.example/", f"https://server.example/pair#token={CODE}&token={CODE}",
        f"https://server.example/other#token={CODE}", f"https://server.example/pair?x=1#token={CODE}",
        "https://server.example/pair#token=invalid", f"https://user@server.example/pair#token={CODE}",
        f"https://server.example/pair#code={CODE}", f"t3code://threads/env/thread#token={CODE}",
        f"ftp://server.example/pair#token={CODE}", "x" * 9000,
    ]
    for text in rejected:
        try:
            t3remote.parse_pairing_link(text)
            check(f"link refused: {text[:60]}", False)
        except t3remote.RemoteError:
            check(f"link refused: {text[:60]}", True)

    # ----- destinations -----
    def allowed(origin, peer=lambda address: False):
        try:
            t3remote.check_destination(origin, peer)
            return True
        except t3remote.RemoteError as error:
            return error.code

    check("https anywhere", allowed("https://server.example/") is True)
    check("http to loopback", allowed("http://127.0.0.1:3773/") is True and allowed("http://[::1]:3773/") is True)
    check("http to a LAN address is refused", allowed("http://192.168.1.20:3773/") == "destination")
    check("http to a host name is refused", allowed("http://server.example/") == "destination")
    check("http to an unverified tailnet address is refused", allowed("http://100.100.1.2:3773/") == "tailscale")
    check("http to a verified Tailscale peer", allowed("http://100.100.1.2:3773/", lambda a: a == "100.100.1.2") is True)
    check("Tailscale v6 range", t3remote.is_tailnet_address("fd7a:115c:a1e0::1")
          and not t3remote.is_tailnet_address("fd00::1"))
    with tempfile.TemporaryDirectory() as folder:
        fake_tailscale = os.path.join(folder, "tailscale")
        with open(fake_tailscale, "w") as handle:
            handle.write("#!/bin/sh\necho '{\"BackendState\":\"Running\",\"Peer\":{\"k\":{\"TailscaleIPs\":[\"100.100.1.2\"]}}}'\n")
        os.chmod(fake_tailscale, 0o755)
        check("Tailscale status lists the peer", t3remote.tailscale_peer("100.100.1.2", [fake_tailscale])
              and not t3remote.tailscale_peer("100.100.1.3", [fake_tailscale]))

    # ----- descriptor -----
    check("descriptor parsed", t3remote.parse_descriptor(descriptor("env", "Box")) == {
        "environmentId": "env", "label": "Box", "os": "linux", "machine": "server", "protocol": 2,
        "serverVersion": "0.1.8"})
    for name, broken in [("no id", dict(descriptor("e", "L"), environmentId=" ")),
                         ("bad os", dict(descriptor("e", "L"), platform={"os": "beos", "arch": "x64"})),
                         ("no capabilities", {k: v for k, v in descriptor("e", "L").items() if k != "capabilities"})]:
        try:
            t3remote.parse_descriptor(broken)
            check(f"descriptor refused: {name}", False)
        except t3remote.RemoteError as error:
            check(f"descriptor refused: {name}", error.code == "descriptor")
    try:
        t3remote.parse_descriptor(descriptor("e", "L", protocol=3))
        check("unknown protocol refused", False)
    except t3remote.RemoteError as error:
        check("unknown protocol refused", error.code == "protocol")

    # ----- saved list -----
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "t3codebar", "connections.json")
        registry = t3remote.Registry(path)
        registry.upsert(t3remote.parse_descriptor(descriptor("a", "Alpha")), "https://a.example/")
        check("saved with owner-only permissions", stat.S_IMODE(os.stat(path).st_mode) == 0o600)
        check("saved without credentials", "token" not in open(path).read())
        try:
            registry.upsert(t3remote.parse_descriptor(descriptor("b", "Beta")), "https://a.example/")
            check("an address saved for another environment is refused", False)
        except t3remote.RemoteError as error:
            check("an address saved for another environment is refused", error.code == "conflict")
        registry.upsert(t3remote.parse_descriptor(descriptor("a", "Alpha 2")), "https://a2.example/")
        reread = t3remote.Registry(path)
        check("pairing again updates the entry", [(e["id"], e["label"], e["origin"]) for e in reread.environments]
              == [("a", "Alpha 2", "https://a2.example/")])
        reread.remove("a")
        check("removed", t3remote.Registry(path).environments == [])
        with open(path, "w") as handle:
            handle.write("{not json")
        broken = t3remote.Registry(path)
        try:
            broken.upsert(t3remote.parse_descriptor(descriptor("c", "C")), "https://c.example/")
            changed = True
        except t3remote.RemoteError:
            changed = False
        check("an unreadable list is left alone", broken.error and not changed and open(path).read() == "{not json")

    # ----- usage across environments -----
    request = {"sinceDay": "2026-11-10", "untilDay": "2026-11-16", "timeZone": "UTC", "resolution": "day"}
    shared_a, shared_b = summary_for("host-1")(request), summary_for("host-1")(request)
    shared_b["readAt"] = iso(-60)
    merged, notes = t3remote.merge_summaries([("a", "Alpha", shared_a), ("b", "Beta", shared_b)])
    check("a folder two servers scan counts once", len(merged["buckets"]) == 1 and len(merged["sources"]) == 1)
    check("the newest reader owns it", merged["buckets"][0] is shared_a["buckets"][0] and "Beta" in notes[0])
    other = summary_for("host-2", "/home/you/.claude", 2.0)(request)
    merged, notes = t3remote.merge_summaries([("a", "Alpha", shared_a), ("c", "Gamma", other)])
    check("different machines both count", len(merged["buckets"]) == 2 and notes == [])
    failed = dict(summary_for("host-1")(request), readAt=iso(0))
    failed["sources"] = [dict(failed["sources"][0], status="failed")]
    merged, _ = t3remote.merge_summaries([("f", "Failed", failed), ("a", "Alpha", shared_a)])
    check("an ok reader wins over a newer failed one", merged["buckets"] == shared_a["buckets"])
    check("a summary for another window is refused",
          not t3remote.summary_matches(dict(shared_a, sinceDay="2026-01-01"), request)
          and t3remote.summary_matches(shared_a, request))

    # ----- the hub against fake servers -----
    state = tempfile.mkdtemp()
    base = os.path.join(state, "userdata")
    os.makedirs(base)
    with open(os.path.join(base, "environment-id"), "w") as handle:
        handle.write("local-env")
    alpha = FakeT3(code=CODE, descriptor=descriptor("env-alpha", "Alpha box", "desktop"))
    alpha.summary = summary_for("host-alpha", "/home/a/.claude", 1.5)
    beta = FakeT3(code=CODE, descriptor=descriptor("env-beta", "Beta box", protocol=1))
    beta.summary = summary_for("host-beta", "/home/b/.claude", 2.0)
    registry = t3remote.Registry(os.path.join(state, "config", "connections.json"))
    MemoryKeyring.saved = {}
    hub = t3live.Hub(base, True, queue.Queue(), registry=registry, verify_peer=lambda a: False, keyring=MemoryKeyring)
    hub.local.store = test_live.MemoryStore()

    hub.add(f"http://127.0.0.1:{alpha.port}/pair#token={CODE}", 1)
    reply = next_event(hub, "pairing")
    check("pairing link connects a remote environment", reply and reply["ok"] and reply["environmentId"] == "env-alpha"
          and reply["label"] == "Alpha box")
    check("the grant is saved under its environment and origin",
          ("env-alpha", f"http://127.0.0.1:{alpha.port}/") in MemoryKeyring.saved
          and "token" not in open(registry.path).read())
    check("remote goes online", wait(lambda: (remote(hub, "env-alpha") or {}).get("state") == "online"
                                     and remote(hub, "env-alpha")["threadsReady"]))
    env = remote(hub, "env-alpha")
    ids = {t["id"] for t in env["threads"]}
    check("remote threads listed with the same filters", "t-approval" in ids and not {"t-sub", "t-settled",
                                                                                     "t-overridden", "t-delegated"} & ids)
    approval = next(t for t in env["threads"] if t["id"] == "t-approval")
    check("remote threads carry their environment", approval["environmentId"] == "env-alpha" and approval["remote"]
          and approval["key"] == "remote/env-alpha/t-approval" and approval["machine"] == "desktop")
    check("remote thread links", approval["appUrl"] == "t3code://threads/env-alpha/t-approval"
          and approval["browserUrl"] == f"http://127.0.0.1:{alpha.port}/env-alpha/t-approval")
    check("remote projects never use local files", all(t["badge"]["kind"] != "image" for t in env["threads"]))
    check("remote checks identity before connecting", alpha.requests.count("/oauth/token") == 1)

    hub.preview("env-alpha", "t-approval", 7)
    preview = next_event(hub, "preview")
    check("remote preview from the thread projection", preview and preview["ok"] and preview["seq"] == 7
          and preview["text"] == "Latest\nDone with it." and alpha.projections == ["t-approval"])
    hub.preview("env-alpha", "not-listed", 8)
    preview = next_event(hub, "preview")
    check("unlisted threads are not read", preview and not preview["ok"] and alpha.projections == ["t-approval"])

    hub.add(f"http://127.0.0.1:{beta.port}/pair#token={CODE}", 2)
    check("a second environment", (next_event(hub, "pairing") or {}).get("ok")
          and wait(lambda: (remote(hub, "env-beta") or {}).get("state") == "online"))
    check("protocol 1 servers are asked without the v2 parameter",
          beta.socket_queries and "orchestrationProtocol" not in beta.socket_queries[-1]
          and alpha.socket_queries[-1].get("orchestrationProtocol") == "2")
    check("environments listed by label", [e["label"] for e in hub.environments()["list"]] == ["Alpha box", "Beta box"])

    # Limits for one environment and for all of them.
    hub.select("env-alpha")
    view = hub.view()
    check("one remote's limits", view["live"]["remote"] and view["live"]["label"] == "Alpha box"
          and view["live"]["connected"] and view["groups"])
    hub.select("all")
    view = hub.view()
    accounts = {g["driver"]: [a["name"] for a in g["accounts"]] for g in view["groups"]}
    check("all environments' limits", "All environments: Local, Alpha box, Beta box" in view["footnote"]
          and not view["live"].get("remote"))
    # Both fakes report the same Claude login and the same two CLI Proxy accounts.
    check("the same account on two servers shows once", len(accounts.get("claudeAgent", [])) == 1
          and len(accounts.get("codex", [])) == 2)

    # Usage: one remote, then all of them, each source counted once.
    def usage(environment, period=7):
        hub.usage(period, period * 100, environment)
        return next_event(hub, "usage")

    one = usage("env-beta")
    check("one remote's usage", one and one["report"] and abs(one["report"]["cost"] - 2.0) < 1e-9
          and one["fallback"] is False)
    every = usage("all")
    check("all environments' usage adds up", every and every["report"] and abs(every["report"]["cost"] - 3.5) < 1e-9
          and every["fallback"] is True)
    check("usage says Local is not included", any("Local is not included" in n
                                                   for n in every["report"]["environmentNotes"]))
    beta.summary = summary_for("host-alpha", "/home/a/.claude", 1.5)
    every = usage("all")
    check("a folder both servers scan counts once", every and abs(every["report"]["cost"] - 1.5) < 1e-9
          and any("Counted once" in n for n in every["report"]["environmentNotes"]))
    hourly = usage("all", 1)
    check("past 24 hours across environments", hourly and hourly["report"] and hourly["report"]["cost"] > 0)
    missing = usage("env-gone")
    check("a missing environment says so", missing and missing["error"] and missing["fallback"] is False)

    # Pairing refusals.
    hub.add(f"http://127.0.0.1:{alpha.port}/pair#token=ZZZZ2345EFGH", 3)
    reply = next_event(hub, "pairing")
    check("a rejected code keeps the existing grant", reply and not reply["ok"] and "rejected" in reply["error"]
          and remote(hub, "env-alpha")["state"] == "online")
    alpha.descriptor = descriptor("env-other", "Imposter")
    hub.add(f"http://127.0.0.1:{alpha.port}/pair#token={CODE}", 4)
    reply = next_event(hub, "pairing")
    check("an address saved for another environment is refused", reply and not reply["ok"]
          and "different environment" in reply["error"])
    hub.add("http://192.168.1.20:3773/pair#token=" + CODE, 5)
    reply = next_event(hub, "pairing")
    check("a LAN address is refused before any request", reply and not reply["ok"] and "HTTPS" in reply["error"])

    # A server that now names another environment is not trusted with the grant.
    before = alpha.requests.count("/api/auth/websocket-ticket/rpc")
    connection = hub.remotes["env-alpha"]
    connection.connect(connection.credential, True)
    check("a changed environment stops the connection", wait(lambda: remote(hub, "env-alpha")["state"] == "failed")
          and "different T3 Code environment" in remote(hub, "env-alpha")["text"]
          and alpha.requests.count("/api/auth/websocket-ticket/rpc") == before)
    alpha.descriptor = descriptor("env-alpha", "Alpha box", "desktop")

    # A revoked grant asks for a new link.
    beta.tokens.clear()
    hub.remotes["env-beta"].connect(hub.remotes["env-beta"].credential, True)
    check("a revoked grant needs access", wait(lambda: remote(hub, "env-beta")["state"] == "needsAccess")
          and "revoked" in remote(hub, "env-beta")["text"]
          and ("env-beta", f"http://127.0.0.1:{beta.port}/") not in MemoryKeyring.saved)
    beta.tokens.add(test_live.TOKEN)

    # Offline: the last threads stay, marked offline, and it reconnects.
    hub.remotes["env-alpha"].connect(hub.remotes["env-alpha"].credential, True)
    check("reconnects", wait(lambda: remote(hub, "env-alpha")["state"] == "online"))
    session = remote(hub, "env-alpha")["session"]
    alpha.server.shutdown()
    alpha.server.server_close()
    hub.remotes["env-alpha"].connect(hub.remotes["env-alpha"].credential, True)
    check("offline keeps the last threads", wait(lambda: remote(hub, "env-alpha")["state"] == "offline")
          and remote(hub, "env-alpha")["threads"] and "last available data" in remote(hub, "env-alpha")["text"])

    # Restart: the saved list and grants reconnect.
    again = t3live.Hub(base, True, queue.Queue(), registry=t3remote.Registry(registry.path),
                       verify_peer=lambda a: False, keyring=MemoryKeyring)
    check("a restart lists the saved environments", [e["id"] for e in again.environments()["list"]]
          == ["env-alpha", "env-beta"])
    check("a saved grant is used after a restart", wait(lambda: (remote(again, "env-alpha") or {}).get("state")
                                                         == "offline"))
    check("a deleted grant needs access after a restart", remote(again, "env-beta")["state"] == "needsAccess")
    again.stop()

    # Forget and remove.
    hub.forget("env-alpha")
    check("forget drops the grant and data", remote(hub, "env-alpha")["state"] == "needsAccess"
          and not remote(hub, "env-alpha")["threads"] and not any(k[0] == "env-alpha" for k in MemoryKeyring.saved)
          and session >= 1)
    hub.remove("env-alpha")
    check("remove drops it from the list", remote(hub, "env-alpha") is None
          and [e["id"] for e in t3remote.Registry(registry.path).environments] == ["env-beta"])

    # A link for this computer's own server pairs the local connection.
    local = FakeT3(code=CODE, descriptor=descriptor("local-env", "This machine"))
    hub.add(f"http://127.0.0.1:{local.port}/pair#token={CODE}", 9)
    reply = next_event(hub, "pairing")
    check("a link for this computer pairs locally", reply and reply["ok"] and reply["local"]
          and wait(lambda: hub.local.status == "live") and remote(hub, "local-env") is None)
    hub.stop()

    print("OK" if not failures else f"{failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
