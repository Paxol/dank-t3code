#!/usr/bin/env python3
"""Read-only data backend for the T3 Code Bar DMS widget.

A Linux port of the data layer of eliophan/t3code-bar (macOS, Swift). It reads
T3 Code's local state and prints JSON for the QML widget. It never writes to
T3 Code's files, never reads credentials, and never touches the network.

Subcommands:
  watch [--interval S]      one JSON line per change of the thread inbox
  threads                   one inbox snapshot
  preview THREAD_ID         start of the thread's latest assistant reply
  limits                    provider limits from T3 Code's provider caches
  usage [--period N]        cost and token report (N = 1, 7, 30 or 90 days)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def state_dir(override: str | None) -> str:
    """T3_CODE_BAR_STATE_DIR, then T3CODE_HOME/userdata, then ~/.t3/userdata."""
    for value in (override, os.environ.get("T3_CODE_BAR_STATE_DIR")):
        if value and value.strip():
            return os.path.expanduser(value.strip())
    home = os.environ.get("T3CODE_HOME", "").strip()
    if home:
        return os.path.join(os.path.expanduser(home), "userdata")
    return os.path.expanduser("~/.t3/userdata")


def read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def read_json(path: str):
    text = read_text(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def runtime_info(base: str) -> dict:
    data = read_json(os.path.join(base, "server-runtime.json"))
    return data if isinstance(data, dict) else {}


def environment_id(base: str) -> str | None:
    text = (read_text(os.path.join(base, "environment-id")) or "").strip()
    return text or None


def server_online(runtime: dict) -> bool:
    """Process presence only, as in the original; not an HTTP health check."""
    pid = runtime.get("pid")
    if not isinstance(pid, int) or pid <= 0 or pid > 2**31 - 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def runtime_port(runtime: dict) -> int | None:
    port = runtime.get("port")
    if isinstance(port, int) and 1 <= port <= 65535:
        return port
    origin = runtime.get("origin")
    if isinstance(origin, str):
        match = re.search(r":(\d+)(?:/|$)", origin)
        if match and 1 <= int(match.group(1)) <= 65535:
            return int(match.group(1))
    return None


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def parse_date(value) -> float | None:
    """ISO 8601 (with or without fractional seconds) to epoch milliseconds."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp() * 1000.0


def now_ms() -> float:
    return time.time() * 1000.0


# ---------------------------------------------------------------------------
# Threads (port of DatabaseManager.swift and ThreadModel.swift)
# ---------------------------------------------------------------------------


class ReadError(Exception):
    MESSAGES = {
        "missing": "T3 Code data not found. Open T3 Code or check the configured data directory.",
        "unavailable": "Cannot read T3 Code data. Check file permissions.",
        "incompatibleSchema": "This T3 Code database schema is not supported.",
        "busy": "T3 Code data is busy. Retrying automatically.",
        "invalidData": "T3 Code data could not be decoded. Retrying automatically.",
    }

    def __init__(self, code: str):
        super().__init__(self.MESSAGES.get(code, code))
        self.code = code


def open_db(base: str) -> sqlite3.Connection:
    path = os.path.join(base, "statev2.sqlite")
    if not os.path.exists(path):
        raise ReadError("missing")
    try:
        # Read-only, without immutable=1 so WAL changes stay visible.
        uri = "file:" + path.replace("?", "%3f").replace("#", "%23") + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=0.25, check_same_thread=False)
        conn.execute("PRAGMA query_only = 1")
        return conn
    except sqlite3.Error as error:
        raise classify(error) from error


def classify(error: sqlite3.Error) -> ReadError:
    text = str(error).lower()
    if "locked" in text or "busy" in text:
        return ReadError("busy")
    if "unable to open" in text or "permission" in text or "readonly" in text or "disk i/o" in text:
        return ReadError("unavailable")
    if "no such table" in text or "no such column" in text:
        return ReadError("incompatibleSchema")
    return ReadError("invalidData")


LATEST_FAILURE_SQL = """
(
    SELECT CASE WHEN r.status = 'failed' THEN (
        SELECT json_extract(ti.payload_json, '$.failure')
        FROM orchestration_v2_projection_turn_items ti
        WHERE ti.thread_id = r.thread_id AND ti.run_id = r.run_id
          AND ti.type = 'error' AND ti.status = 'failed'
          AND ti.node_id IS json_extract(r.payload_json, '$.rootNodeId')
        ORDER BY ti.updated_at DESC, ti.ordinal DESC, ti.turn_item_id DESC
        LIMIT 1
    ) END
    FROM orchestration_v2_projection_runs r
    WHERE r.thread_id = t.thread_id
      AND r.status != 'queued'
      AND NOT (r.status = 'cancelled' AND json_extract(r.payload_json, '$.startedAt') IS NULL)
      AND COALESCE(json_extract(r.payload_json, '$.silent'), 0) != 1
    ORDER BY r.completed_at IS NULL DESC, r.completed_at DESC, r.ordinal DESC
    LIMIT 1
)
"""

THREAD_WAIT_TOOL_SQL = """
ti.type = 'dynamic_tool' AND (
    json_extract(ti.payload_json, '$.toolName') GLOB '*t3_thread_wait'
    OR (json_extract(ti.payload_json, '$.toolName') GLOB '*delegate_task'
        AND json_extract(ti.payload_json, '$.input.mode') = 'wait')
)
"""

BLOCKED_ON_THREAD_WAIT_SQL = f"""
(
    SELECT CASE WHEN r.status = 'running'
        AND EXISTS (
            SELECT 1 FROM orchestration_v2_projection_turn_items ti
            WHERE ti.thread_id = r.thread_id AND ti.run_id = r.run_id
              AND ti.status IN ('running', 'waiting') AND {THREAD_WAIT_TOOL_SQL})
        AND NOT EXISTS (
            SELECT 1 FROM orchestration_v2_projection_turn_items ti
            WHERE ti.thread_id = r.thread_id AND ti.run_id = r.run_id
              AND ti.type IN ('command_execution', 'dynamic_tool', 'subagent', 'file_change', 'file_search', 'web_search', 'compaction')
              AND ti.status IN ('running', 'waiting')
              AND NOT (ti.type = 'subagent' AND json_extract(ti.payload_json, '$.origin') = 'app_owned')
              AND NOT ({THREAD_WAIT_TOOL_SQL}))
        THEN 1 ELSE 0 END
    FROM orchestration_v2_projection_runs r
    WHERE r.thread_id = t.thread_id
      AND r.status IN ('preparing', 'starting', 'running', 'waiting')
      AND COALESCE(json_extract(r.payload_json, '$.silent'), 0) != 1
    ORDER BY r.ordinal DESC, r.run_id DESC
    LIMIT 1
)
"""

ACTIVE_RUN = """
    FROM orchestration_v2_projection_runs r
    WHERE r.thread_id = t.thread_id
      AND r.status IN ('preparing', 'starting', 'running', 'waiting')
      AND COALESCE(json_extract(r.payload_json, '$.silent'), 0) != 1
    ORDER BY r.ordinal DESC, r.run_id DESC
    LIMIT 1
"""

LATEST_RUN = """
    FROM orchestration_v2_projection_runs r
    WHERE r.thread_id = t.thread_id
      AND r.status != 'queued'
      AND NOT (r.status = 'cancelled' AND json_extract(r.payload_json, '$.startedAt') IS NULL)
      AND COALESCE(json_extract(r.payload_json, '$.silent'), 0) != 1
    ORDER BY r.completed_at IS NULL DESC, r.completed_at DESC, r.ordinal DESC
    LIMIT 1
"""

LATEST_PROVIDER_THREAD = """
    FROM orchestration_v2_projection_provider_threads pt
    WHERE pt.thread_id = t.thread_id ORDER BY pt.last_run_ordinal DESC LIMIT 1
"""


def thread_query(failure: str, blocked: str) -> str:
    return f"""
    SELECT
        t.thread_id AS thread_id,
        t.title AS title,
        t.project_id AS project_id,
        COALESCE(p.title, 'No Project') AS project_title,
        t.created_at AS created_at,
        t.updated_at AS updated_at,
        (SELECT r.status {ACTIVE_RUN}) AS active_run_status,
        (SELECT COALESCE(json_extract(r.payload_json, '$.workStartedAt'),
                         json_extract(r.payload_json, '$.startedAt'), r.requested_at) {ACTIVE_RUN}) AS active_run_started_at,
        (SELECT r.status {LATEST_RUN}) AS latest_run_status,
        (SELECT COALESCE(json_extract(r.payload_json, '$.startedAt'), r.requested_at) {LATEST_RUN}) AS latest_run_started_at,
        (SELECT r.completed_at {LATEST_RUN}) AS latest_run_completed_at,
        (SELECT COUNT(*) FROM orchestration_v2_projection_runtime_requests req
            WHERE req.thread_id = t.thread_id AND req.status = 'pending' AND req.kind = 'user_input') AS pending_user_input,
        (SELECT COUNT(*) FROM orchestration_v2_projection_runtime_requests req
            WHERE req.thread_id = t.thread_id AND req.status = 'pending' AND req.kind != 'user_input') AS pending_approval,
        json_extract(t.payload_json, '$.branch') AS branch,
        json_extract(t.payload_json, '$.worktreePath') AS worktree_path,
        json_extract(t.payload_json, '$.modelSelection.model') AS model_name,
        (SELECT number FROM projection_thread_pull_requests pr WHERE pr.thread_id = t.thread_id LIMIT 1) AS pr_number,
        CASE WHEN json_extract(t.payload_json, '$.pinnedAt') IS NOT NULL THEN 1 ELSE 0 END AS is_pinned,
        json_extract(t.payload_json, '$.snoozedUntil') AS snoozed_until,
        CASE WHEN (SELECT json_extract(pt.payload_json, '$.goal.status') {LATEST_PROVIDER_THREAD}) = 'active'
            THEN 1 ELSE 0 END AS has_goal,
        json_extract(t.payload_json, '$.snoozedAt') AS snoozed_at,
        json_extract(t.payload_json, '$.pinOrderKey') AS pin_order_key,
        json_extract(t.payload_json, '$.activeOrderKey') AS active_order_key,
        (SELECT group_concat(DISTINCT json_extract(r.payload_json, '$.providerInstanceId'))
            FROM orchestration_v2_projection_runs r WHERE r.thread_id = t.thread_id) AS provider_history,
        (SELECT m.updated_at FROM orchestration_v2_projection_messages m
            WHERE m.thread_id = t.thread_id AND m.role = 'user' ORDER BY m.updated_at DESC LIMIT 1) AS latest_user_message_at,
        p.project_icon_json AS project_icon_json,
        p.favicon_path AS favicon_path,
        p.workspace_root AS workspace_root,
        json_extract(t.payload_json, '$.unsettledAt') AS unsettled_at,
        json_extract(t.payload_json, '$.lastVisitedAt') AS last_visited_at,
        (SELECT json_extract(pt.payload_json, '$.status') {LATEST_PROVIDER_THREAD}) AS session_status,
        (SELECT json_extract(pt.payload_json, '$.updatedAt') {LATEST_PROVIDER_THREAD}) AS session_updated_at,
        {failure} AS latest_failure,
        (SELECT COUNT(*) FROM json_each(t.payload_json, '$.guestShare.messages') g
            WHERE json_extract(g.value, '$.status') = 'pending') AS pending_guest_messages,
        EXISTS (SELECT 1 FROM orchestration_v2_projection_runs r WHERE r.thread_id = t.thread_id) AS has_runs,
        COALESCE({blocked}, 0) AS blocked_on_thread_wait
    FROM orchestration_v2_projection_threads t
    LEFT JOIN projection_projects p ON t.project_id = p.project_id
    WHERE t.thread_id NOT LIKE 'thread:%' AND t.deleted_at IS NULL AND t.archived_at IS NULL
      AND json_extract(t.payload_json, '$.settledAt') IS NULL
      AND COALESCE(json_extract(t.payload_json, '$.lineage.sideChat'), 0) != 1
      AND COALESCE(json_extract(t.payload_json, '$.lineage.relationshipToParent'), '') != 'subagent'
    """


STATUS_LABELS = {
    "working": "Working", "goal": "Goal", "approval": "Approval", "input": "Input", "request": "Request",
    "woke": "Woke", "failed": "Failed", "limited": "Limited", "waiting": "Waiting", "done": "Done",
    "settled": "Settled",
}
STATUS_PRIORITY = {
    "approval": 8, "input": 7, "request": 7, "goal": 6, "working": 5, "failed": 4, "limited": 4,
    "woke": 3, "done": 1, "settled": 0, "waiting": 0,
}
ATTENTION = {"working", "goal", "approval", "input", "request", "failed", "limited", "woke"}
RUNNING = {"working", "goal"}
LABEL_IN_BAR = {"approval", "input", "request", "woke", "failed", "limited"}
ALERTING = {"approval", "input", "request", "failed", "limited"}

PROVIDER_DRIVERS = [
    ("codex", "openai"), ("claudeagent", "claude"), ("cursor", "cursor"), ("grok", "grok"),
    ("opencode", "opencode"), ("antigravity", "antigravity"), ("fx", "fx"), ("kiro", "kiro"),
    ("pi", "pi"), ("muse", "muse"),
]


def provider_kind(raw: str) -> str:
    name = raw.lower()
    for prefix, kind in PROVIDER_DRIVERS:
        if name == prefix or name.startswith(prefix + "_"):
            return kind
    if "grok" in name or "xai" in name:
        return "grok"
    if any(word in name for word in ("claude", "anthropic", "sonnet", "opus", "haiku")):
        return "claude"
    if "gemini" in name or "google" in name:
        return "gemini"
    if any(word in name for word in ("gpt", "openai", "codex")) or name.startswith(("o3", "o4")):
        return "openai"
    return "unknown"


def provider_kinds(raws: list[str]) -> list[dict]:
    result: list[dict] = []
    seen = set()
    for raw in raws:
        kind = provider_kind(raw)
        if kind in seen:
            continue
        seen.add(kind)
        result.append({"kind": kind, "initials": raw[:2].upper() if kind == "unknown" else ""})
    return result or [{"kind": "openai", "initials": ""}]


MONOGRAM_PALETTE = [
    ("gray", "#9ca3af"), ("red", "#f87171"), ("orange", "#fb923c"), ("amber", "#fbbf24"),
    ("yellow", "#facc15"), ("lime", "#a3e635"), ("green", "#4ade80"), ("emerald", "#34d399"),
    ("teal", "#2dd4bf"), ("cyan", "#22d3ee"), ("sky", "#38bdf8"), ("blue", "#60a5fa"),
    ("indigo", "#818cf8"), ("violet", "#a78bfa"), ("purple", "#c084fc"), ("fuchsia", "#e879f9"),
    ("pink", "#f472b6"), ("rose", "#fb7185"),
]

FAVICON_CANDIDATES = [
    "favicon.svg", "favicon.png", "public/favicon.svg", "public/favicon.png", "app/favicon.png",
    "app/icon.svg", "app/icon.png", "src/favicon.svg", "src/app/icon.svg", "src/app/icon.png",
    "assets/icon.svg", "assets/icon.png", "assets/logo.svg", "assets/logo.png", ".idea/icon.svg",
]
IMAGE_EXTENSIONS = (".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif")


def monogram(name: str) -> str:
    clean = unicodedata.normalize("NFC", name).strip()
    words = re.findall(r"[^\W_]+", clean)
    if not words:
        return "PR"
    first = words[0]
    second = next((ch for ch in first[1:] if ch.isdigit()), None)
    if second is None:
        second = words[-1][0] if len(words) > 1 else first[-1]
    return (first[0] + second).upper()[:2]


def hash_color(name: str) -> str:
    target = unicodedata.normalize("NFC", name).strip().lower() or "project"
    n = 0
    for char in target:
        n = (n * 31 + ord(char)) % len(MONOGRAM_PALETTE)
    return MONOGRAM_PALETTE[n][1]


def color_by_name(name) -> str:
    if isinstance(name, str):
        key = name.strip().lower()
        for entry, hex_value in MONOGRAM_PALETTE:
            if entry == key:
                return hex_value
    return "#60a5fa"


_favicon_cache: dict[tuple, str | None] = {}


def resolve_favicon(root: str | None, favicon: str | None) -> str | None:
    key = (root or "", favicon or "")
    if key in _favicon_cache:
        return _favicon_cache[key]
    found = None
    paths = []
    if favicon:
        paths.append(favicon if favicon.startswith("/") else os.path.join(root or "", favicon))
    if root and os.path.isdir(root):
        paths += [os.path.join(root, candidate) for candidate in FAVICON_CANDIDATES]
    for path in paths:
        if path.lower().endswith(IMAGE_EXTENSIONS) and os.path.isfile(path):
            found = path
            break
    _favicon_cache[key] = found
    return found


def project_badge(title: str, icon_json: str | None, favicon: str | None, root: str | None) -> dict:
    icon = None
    if icon_json:
        try:
            icon = json.loads(icon_json)
        except ValueError:
            icon = None
    if not isinstance(icon, dict):
        icon = None
    if title.strip().lower() in ("no project", "noproject") or (icon and icon.get("name") == "message-square-dashed"):
        return {"kind": "none", "color": color_by_name((icon or {}).get("color") or "gray")}
    if icon:
        kind = icon.get("kind")
        if kind == "monogram":
            return {"kind": "monogram", "text": icon.get("text") or monogram(title), "color": color_by_name(icon.get("color"))}
        if kind == "emoji" and icon.get("emoji"):
            return {"kind": "emoji", "text": icon["emoji"]}
        if kind == "lucide":
            return {"kind": "folder", "color": color_by_name(icon.get("color"))}
    image = resolve_favicon(root, favicon)
    if image:
        return {"kind": "image", "path": image}
    return {"kind": "monogram", "text": monogram(title), "color": hash_color(title)}


def derive_status(row: dict, now: float) -> tuple[str, bool]:
    """Returns (status, hidden_by_snooze). Mirrors ThreadItem.status and the snooze rules."""
    snoozed_until = parse_date(row["snoozed_until"])
    snoozed_at = parse_date(row["snoozed_at"])
    completed_at = parse_date(row["latest_run_completed_at"])
    last_visited = parse_date(row["last_visited_at"])
    session_updated = parse_date(row["session_updated_at"])
    latest = row["latest_run_status"]
    pending_approval = int(row["pending_approval"] or 0)
    pending_input = int(row["pending_user_input"] or 0)

    completed_while_snoozed = bool(latest == "completed" and snoozed_at and completed_at and completed_at > snoozed_at)
    fresh_failure = row["session_status"] == "error" and (
        True if snoozed_at is None else (session_updated is not None and session_updated > snoozed_at)
    )
    raised_hand = pending_approval > 0 or pending_input > 0 or fresh_failure or completed_while_snoozed
    if snoozed_until is not None and snoozed_until > now and not raised_hand:
        return "settled", True

    woke_at = None
    if snoozed_until is not None:
        if raised_hand:
            woke_at = completed_at if completed_while_snoozed else (session_updated or snoozed_at)
        elif snoozed_until <= now:
            woke_at = snoozed_until
    is_woke = woke_at is not None and (last_visited is None or last_visited < woke_at)

    failure_class = None
    if row["latest_failure"]:
        try:
            failure = json.loads(row["latest_failure"])
            if isinstance(failure, dict):
                failure_class = failure.get("class")
                row["_reset_at"] = parse_date(failure.get("resetAt"))
        except ValueError:
            pass
    row["_failure_class"] = failure_class

    active = row["active_run_status"]
    if pending_approval > 0:
        return "approval", False
    if pending_input > 0:
        return "input", False
    if int(row["pending_guest_messages"] or 0) > 0:
        return "request", False
    if active == "running" and row["blocked_on_thread_wait"]:
        return "waiting", False
    if active in ("starting", "preparing", "running", "waiting"):
        return ("goal" if row["has_goal"] else "working"), False
    if latest in ("failed", "error"):
        return ("limited" if failure_class == "usage_limit" else "failed"), False
    if is_woke:
        return "woke", False
    if not row["has_runs"]:
        return "waiting", False
    if latest == "completed" and completed_at is not None and last_visited is not None and completed_at > last_visited:
        return "done", False
    return "settled", False


def sort_key_cmp(a: dict, b: dict) -> int:
    """Pinned/manual inbox order of DatabaseManager.fetchActiveThreads."""
    if a["isPinned"] != b["isPinned"]:
        return -1 if a["isPinned"] else 1
    if a["isPinned"]:
        ka, kb = a["_pin"], b["_pin"]
        if ka is not None and kb is not None and ka != kb:
            return -1 if ka < kb else 1
        if ka is not None and kb is None:
            return -1
        if ka is None and kb is not None:
            return 1
        if a["createdAt"] != b["createdAt"]:
            return -1 if a["createdAt"] > b["createdAt"] else 1
    else:
        ka, kb = a["_active"], b["_active"]
        if ka is None and kb is not None:
            return -1
        if ka is not None and kb is None:
            return 1
        if ka is not None and kb is not None:
            if ka != kb:
                return -1 if ka < kb else 1
        else:
            ea = max(a["createdAt"], a["_unsettled"] or 0)
            eb = max(b["createdAt"], b["_unsettled"] or 0)
            if ea != eb:
                return -1 if ea > eb else 1
    return -1 if a["id"] < b["id"] else (1 if a["id"] > b["id"] else 0)


def fetch_threads(base: str) -> list[dict]:
    conn = open_db(base)
    try:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(thread_query(LATEST_FAILURE_SQL, BLOCKED_ON_THREAD_WAIT_SQL)).fetchall()
        except sqlite3.OperationalError as error:
            if "no such" not in str(error):
                raise classify(error) from error
            # A build without turn items still lists threads; only Limited and Waiting need them.
            try:
                rows = conn.execute(thread_query("NULL", "NULL")).fetchall()
            except sqlite3.Error as inner:
                raise classify(inner) from inner
        except sqlite3.Error as error:
            raise classify(error) from error
    finally:
        conn.close()

    now = now_ms()
    items = []
    for raw in rows:
        item = thread_item(dict(raw), now)
        if item is not None:
            items.append(item)
    return sort_threads(items)


def thread_item(row: dict, now: float) -> dict | None:
    """One inbox row from the columns of `thread_query`. None when a snooze hides it.

    The live connection builds the same columns from T3's thread shells, so both
    sources share the status rules."""
    if row["thread_id"] is None or row["title"] is None or row["project_id"] is None:
        raise ReadError("invalidData")
    created = parse_date(row["created_at"])
    updated = parse_date(row["updated_at"])
    if created is None or updated is None:
        raise ReadError("invalidData")
    status, hidden = derive_status(row, now)
    if hidden:
        return None
    history = [p.strip() for p in (row["provider_history"] or "").split(",") if p.strip()]
    if not history and row["model_name"]:
        history = [row["model_name"]]
    active_started = parse_date(row["active_run_started_at"])
    latest_started = parse_date(row["latest_run_started_at"])
    completed = parse_date(row["latest_run_completed_at"])
    if status in RUNNING:
        started = active_started or latest_started or updated
    elif status == "waiting":
        started = completed
    else:
        started = None
    branch = (row["branch"] or "").strip()
    return {
        "id": row["thread_id"],
        "title": row["title"],
        "projectTitle": row["project_title"],
        "badge": project_badge(row["project_title"], row["project_icon_json"], row["favicon_path"], row["workspace_root"]),
        "branch": branch,
        "isPinned": bool(row["is_pinned"]),
        "prNumber": row["pr_number"],
        "providers": provider_kinds(history),
        "status": status,
        "statusSince": started,
        "limitResetAt": row.get("_reset_at") if status == "limited" else None,
        "completedAt": completed,
        "lastActivityAt": parse_date(row["latest_user_message_at"]) or updated,
        "createdAt": created,
        "updatedAt": updated,
        "_pin": row["pin_order_key"],
        "_active": row["active_order_key"],
        "_unsettled": parse_date(row["unsettled_at"]),
    }


def sort_threads(items: list[dict]) -> list[dict]:
    import functools

    items.sort(key=functools.cmp_to_key(sort_key_cmp))
    for item in items:
        for key in ("_pin", "_active", "_unsettled"):
            item.pop(key, None)
    return items


def summarize(threads: list[dict], live: bool) -> dict:
    """ThreadSummary: the strongest status, and the badge count the bar shows."""
    status, attention, running = "settled", 0, 0
    if live:
        for thread in threads:
            current = thread["status"]
            if STATUS_PRIORITY[current] > STATUS_PRIORITY[status]:
                status = current
            if current in ATTENTION:
                attention += 1
            if current in RUNNING:
                running += 1
    badge = 0
    if status in RUNNING:
        badge = running
    elif status in ATTENTION:
        badge = sum(1 for t in threads if STATUS_PRIORITY[t["status"]] == STATUS_PRIORITY[status])
    return {
        "status": status,
        "label": STATUS_LABELS[status],
        "showsLabel": status in LABEL_IN_BAR,
        "attention": attention,
        "running": running,
        "badge": badge,
    }


def snapshot(base: str) -> dict:
    runtime = runtime_info(base)
    online = server_online(runtime)
    result = {
        "ok": True,
        "error": None,
        "errorCode": None,
        "serverOnline": online,
        "environmentId": environment_id(base),
        "port": runtime_port(runtime),
        "stateDir": base,
        "threads": [],
    }
    try:
        result["threads"] = fetch_threads(base)
    except ReadError as error:
        result.update(ok=False, error=str(error), errorCode=error.code)
    live = online and result["ok"]
    result["live"] = live
    result["summary"] = summarize(result["threads"], live)
    return result


# ---------------------------------------------------------------------------
# Previews (ThreadPreview.swift)
# ---------------------------------------------------------------------------


def excerpt(markdown: str, max_lines: int = 5, max_chars: int = 360) -> str:
    text = re.sub(r"```[\s\S]*?(```|$)", "", markdown)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    lines = []
    for raw in text.split("\n"):
        line = raw.strip()
        line = re.sub(r"^#{1,6}\s+", "", line)
        line = re.sub(r"^>\s?", "", line)
        line = re.sub(r"^[-*]\s+", "• ", line)
        for marker in ("**", "__", "`"):
            line = line.replace(marker, "")
        line = line.strip()
        if line:
            lines.append(line)
    if not lines:
        return ""
    truncated = len(lines) > max_lines
    result = "\n".join(lines[:max_lines])
    if len(result) > max_chars:
        cut = result[:max_chars]
        space = cut.rfind(" ")
        result = cut[:space] if space > 0 else cut
        truncated = True
    return result + "…" if truncated else result


def preview(base: str, thread_id: str) -> dict:
    try:
        conn = open_db(base)
    except ReadError:
        return {"threadId": thread_id, "text": "", "updatedAt": None}
    try:
        row = conn.execute(
            """
            SELECT substr(json_extract(payload_json, '$.text'), 1, 4000), updated_at
            FROM orchestration_v2_projection_messages
            WHERE thread_id = ?1 AND role = 'assistant'
              AND COALESCE(json_extract(payload_json, '$.text'), '') != ''
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (thread_id,),
        ).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        conn.close()
    if not row:
        return {"threadId": thread_id, "text": "", "updatedAt": None}
    return {"threadId": thread_id, "text": excerpt(row[0] or ""), "updatedAt": parse_date(row[1])}


# ---------------------------------------------------------------------------
# Limits (LimitsReader, LimitsMerge, LimitsModel, LimitsFormat)
# ---------------------------------------------------------------------------

DRIVER_LABELS = {
    "codex": "Codex", "claudeAgent": "Claude", "antigravity": "Antigravity", "cursor": "Cursor",
    "grok": "Grok", "fx": "fx", "kiro": "Kiro", "muse": "Muse Code", "acpRegistry": "ACP Registry",
    "pi": "Pi", "opencode": "OpenCode",
}
DRIVER_ICONS = {
    "codex": "openai", "claudeAgent": "claude", "antigravity": "antigravity", "cursor": "cursor",
    "grok": "grok", "fx": "fx", "kiro": "kiro", "muse": "muse", "pi": "pi", "opencode": "opencode",
}
KIND_ORDER = {"session": 0, "weekly": 1, "monthly": 2}
KIND_NAMES = ["session", "weekly", "monthly", "other"]
STALE_AFTER_MS = 30 * 60 * 1000


def driver_label(driver: str) -> str:
    return DRIVER_LABELS.get(driver, driver)


def js_round(value: float) -> int:
    return int(math.floor(value + 0.5))


def number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def decode_limits_record(data, fallback_id: str) -> dict | None:
    if not isinstance(data, dict) or not isinstance(data.get("driver"), str) or not data["driver"]:
        return None
    auth = data.get("auth") if isinstance(data.get("auth"), dict) else {}
    limits = data.get("usageLimits")
    decoded = None
    if isinstance(limits, dict):
        windows = []
        for window in limits.get("windows") or []:
            if not isinstance(window, dict) or not isinstance(window.get("id"), str):
                continue
            used = number(window.get("usedPercent"))
            if used is None:
                continue
            kind = window.get("kind")
            windows.append({
                "id": window["id"],
                "kind": KIND_ORDER.get(kind, 3),
                "label": window.get("label") if isinstance(window.get("label"), str) else window["id"],
                "used": used,
                "resetsAt": parse_date(window.get("resetsAt")),
                "durationMins": number(window.get("windowDurationMins")),
            })
        credits = None
        if isinstance(limits.get("resetCredits"), dict):
            count = number(limits["resetCredits"].get("availableCount"))
            if count is not None:
                credits = {
                    "count": int(max(0, min(count, 1_000_000))),
                    "nextExpiresAt": parse_date(limits["resetCredits"].get("nextExpiresAt")),
                }
        external = None
        usage = limits.get("externalUsage")
        if isinstance(usage, dict) and isinstance(usage.get("label"), str) and isinstance(usage.get("url"), str):
            external = {"label": usage["label"], "url": usage["url"]}
        unavailable = limits.get("unavailable") if isinstance(limits.get("unavailable"), dict) else None
        decoded = {
            "checkedAt": parse_date(limits.get("checkedAt")) or 0.0,
            "windows": windows,
            "fingerprint": limits.get("credentialFingerprint"),
            "credits": credits,
            "external": external,
            "unavailableReason": (unavailable.get("reason") or "") if unavailable else None,
            "unavailableMessage": unavailable.get("message") if unavailable else None,
        }
    instance = data.get("instanceId") if isinstance(data.get("instanceId"), str) and data.get("instanceId") else fallback_id
    name = data.get("displayName") if isinstance(data.get("displayName"), str) else None
    return {
        "instanceID": instance,
        "driver": data["driver"],
        "enabled": data.get("enabled") is True,
        "installed": data.get("installed") is True,
        "available": data.get("availability") != "unavailable",
        "displayName": name,
        "trimmedName": name.strip() if name and name.strip() else None,
        "authenticated": auth.get("status") == "authenticated",
        "plan": auth.get("label") if isinstance(auth.get("label"), str) and auth.get("label") else None,
        "email": auth.get("email") if isinstance(auth.get("email"), str) else None,
        "workspaceID": auth.get("workspaceId") if isinstance(auth.get("workspaceId"), str) else None,
        "limits": decoded,
    }


def limits_notice(limits: dict) -> str | None:
    reason = limits["unavailableReason"]
    if reason == "unsupported":
        return limits["unavailableMessage"] or "This account has no subscription limits."
    if reason == "probeFailed":
        return limits["unavailableMessage"] or "Could not read limits."
    return "No limits reported." if not limits["windows"] else None


def base_key(record: dict) -> str | None:
    email = (record["email"] or "").strip().lower()
    if email:
        return f"{record['driver']}:{email}"
    fingerprint = (record["limits"] or {}).get("fingerprint")
    if isinstance(fingerprint, str) and fingerprint:
        return f"{record['driver']}:credential:{fingerprint}"
    return None


def merged_accounts(visible: list[dict], enabled: list[dict]) -> list[dict]:
    workspaces: dict[str, set] = {}
    for record in enabled:
        key = base_key(record)
        if key and record["workspaceID"]:
            workspaces.setdefault(key, set()).add(record["workspaceID"])

    def merge_key(record):
        key = base_key(record)
        if not key:
            return None
        if record["workspaceID"]:
            return f"{key}:{record['workspaceID']}"
        known = workspaces.get(key)
        if known is None:
            return key
        return f"{key}:{next(iter(known))}" if len(known) == 1 else None

    order, merged, plans, credits = [], {}, {}, {}
    for record in visible:
        limits = record["limits"]
        if not limits or limits_notice(limits) is not None:
            continue
        key = merge_key(record) or f"local:{record['instanceID']}"
        if record["plan"] and (key not in plans or limits["checkedAt"] > plans[key][0]):
            plans[key] = (limits["checkedAt"], record["plan"])
        if limits["credits"] and (key not in credits or limits["checkedAt"] > credits[key][0]):
            credits[key] = (limits["checkedAt"], limits["credits"])
        if key not in merged:
            order.append(key)
            merged[key] = {
                "id": record["instanceID"], "driver": record["driver"], "displayName": record["trimmedName"],
                "plan": record["plan"], "email": record["email"], "limits": dict(limits),
            }
            continue
        current = merged[key]
        newest = dict(limits) if limits["checkedAt"] > current["limits"]["checkedAt"] else dict(current["limits"])
        newest["credits"] = credits.get(key, (0, None))[1]
        current["displayName"] = current["displayName"] or record["trimmedName"]
        # A CLI Proxy account with an email drops the provider's generic name, so its initials show.
        if record.get("isSource") and record["email"] and (current["displayName"] or "").lower() == driver_label(record["driver"]).lower():
            current["displayName"] = None
        current["plan"] = plans.get(key, (0, None))[1] or current["plan"] or record["plan"]
        current["limits"] = newest

    accounts = [merged[key] for key in order]
    ranked = sorted(accounts, key=lambda a: ((a["displayName"] or a["email"] or a["id"]).lower(), a["id"]))

    def initials(item):
        if not item["email"]:
            return None
        parts = item["email"].split("@")[:2]
        return "".join(part[0].upper() for part in parts if part)

    result = []
    for item in accounts:
        same = [a for a in ranked if a["driver"] == item["driver"]]
        abbreviation = initials(item)
        duplicates = [a for a in same if a["displayName"] is None and initials(a) == abbreviation]
        suffix = ""
        if len(duplicates) > 1:
            suffix = f" {next((i for i, a in enumerate(duplicates) if a['id'] == item['id']), 0) + 1}"
        name = item["displayName"] or ((abbreviation + suffix) if len(same) > 1 and abbreviation else None)
        result.append({
            "id": item["id"], "driver": item["driver"], "displayName": name, "plan": item["plan"],
            "checkedAt": item["limits"]["checkedAt"], "windows": item["limits"]["windows"],
            "credits": item["limits"]["credits"],
            "nameOrder": next((i for i, a in enumerate(ranked) if a["id"] == item["id"]), 0),
        })
    return result


def elapsed_fraction(window: dict, now: float) -> float | None:
    reset, minutes = window.get("resetsAt"), window.get("durationMins")
    if reset is None or minutes is None:
        return None
    duration = minutes * 60_000
    if duration <= 0:
        return None
    return max(0.0, min(1.0, (duration - (round(reset) - round(now))) / duration))


def pace(used: float, elapsed: float) -> str:
    delta = used - elapsed * 100
    return "ahead" if delta > 5 else ("under" if delta < -5 else "on")


def resolve_member(account: dict, window: dict, now: float) -> dict:
    """A Codex or Claude session shows 100% used while the weekly window is exhausted."""
    exhausted = None
    if window["kind"] == 0:
        for other in account["windows"]:
            if other["used"] >= 100 and (
                other["id"] == "seven_day" if account["driver"] == "claudeAgent"
                else account["driver"] == "codex" and other["kind"] == 1
            ):
                exhausted = other
                break
    if exhausted is None:
        return {"account": account, "window": window, "refill": window["used"]}
    floor = max(round(now), round(account["checkedAt"]))
    resets_at, refill = None, 0.0
    weekly, session = exhausted.get("resetsAt"), window.get("resetsAt")
    if weekly is not None and session is not None and round(weekly) > floor and round(session) > floor:
        if round(session) <= round(weekly):
            resets_at, refill = weekly, 100.0
        elif window["used"] >= 100:
            resets_at, refill = session, 100.0
        else:
            resets_at, refill = weekly, 100 - min(100, max(0, window["used"]))
    shown = dict(window, used=100.0, resetsAt=resets_at, durationMins=None)
    return {"account": account, "window": shown, "refill": refill}


def pool_key(driver: str, window: dict) -> str:
    return f"{window['kind']}" if driver == "codex" else f"{window['kind']}:{window['id']}"


def limit_rows(accounts: list[dict], now: float) -> list[dict]:
    keys, pools = [], {}
    for account in accounts:
        for window in account["windows"]:
            member = resolve_member(account, window, now)
            shared = pool_key(account["driver"], member["window"])
            key = shared
            if any(m["account"]["id"] == account["id"] for m in pools.get(shared, [])):
                key = f"{member['window']['kind']}:{member['window']['id']}"
            if key not in pools:
                keys.append(key)
                pools[key] = []
            pools[key].append(member)
    rows = []
    for key in keys:
        members = pools[key]
        head = members[0]["window"]
        used = plain_sum(m["window"]["used"] for m in members) / len(members)
        paced = [(m["window"]["used"], f) for m in members if (f := elapsed_fraction(m["window"], now)) is not None]
        row_pace = None
        if paced:
            row_pace = pace(plain_sum(p[0] for p in paced) / len(paced), plain_sum(p[1] for p in paced) / len(paced))
        resets = sorted(
            [(m, m["window"]["resetsAt"]) for m in members if m["window"].get("resetsAt") is not None],
            key=lambda pair: round(pair[1]),
        )
        rows.append({
            "id": key if members[0]["account"]["driver"] == "codex" else head["id"],
            "kind": head["kind"], "label": head["label"], "members": members,
            "used": js_round(used), "remaining": js_round(100 - used), "pace": row_pace,
            "resets": [{"accountID": m["account"]["id"], "at": at,
                        "restores": js_round(m["refill"] / len(members))} for m, at in resets],
        })
    rows.sort(key=lambda r: r["kind"])
    return rows


def display_order(driver: str, rows: list[dict]) -> list[dict]:
    if driver == "antigravity":
        def stem(ident):
            for suffix in ("-5h", "-weekly", "-quota"):
                if ident.endswith(suffix):
                    return ident[: -len(suffix)]
            return ident
        return sorted(rows, key=lambda r: (0 if r["id"].startswith("gemini-") else 1, stem(r["id"]).lower(), r["kind"]))
    if driver == "cursor":
        order = ["totalPercentUsed", "autoPercentUsed", "apiPercentUsed"]
        ids = {r["id"] for r in rows}
        both = "autoPercentUsed" in ids and "apiPercentUsed" in ids
        kept = [r for r in rows if not both or r["id"] != "totalPercentUsed"]
        return sorted(kept, key=lambda r: order.index(r["id"]) if r["id"] in order else len(order))
    return rows


def fmt_duration(ms: float) -> str:
    value = max(0.0, ms)
    days = int(value // 86_400_000)
    hours = int((value % 86_400_000) // 3_600_000)
    minutes = int((value % 3_600_000) // 60_000)
    if days > 0:
        return f"{days}d {hours}h"
    return f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"


def fmt_ago(at: float, now: float) -> str:
    age = max(0.0, round(now) - round(at))
    unit = 60_000 if age < 3_600_000 else (3_600_000 if age < 86_400_000 else 86_400_000)
    seconds = int((age // unit) * unit / 1000)
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    return f"{hours}h ago" if hours < 24 else f"{hours // 24}d ago"


def fmt_resets_in(reset: float | None, now: float) -> str | None:
    if reset is None:
        return None
    remaining = round(reset) - round(now)
    if remaining <= 0:
        return "resets now"
    if remaining < 60_000:
        return "Resets momentarily"
    return f"resets in {fmt_duration(remaining)}"


def fmt_clock(at: float, now: float, twenty_four: bool) -> str:
    target = datetime.fromtimestamp(at / 1000).astimezone()
    today = datetime.fromtimestamp(now / 1000).astimezone()
    if twenty_four:
        clock = f"{target.hour:02d}:{target.minute:02d}"
    else:
        clock = f"{(target.hour % 12) or 12}:{target.minute:02d} {'AM' if target.hour < 12 else 'PM'}"
    offset = (target.date() - today.date()).days
    if offset == 0:
        return clock
    if offset == 1:
        return f"tomorrow at {clock}"
    if offset == -1:
        return f"yesterday at {clock}"
    dated = f"{target.month}/{target.day}" + ("" if target.year == today.year else f"/{target.year}")
    return f"{dated} {clock}"


def fmt_credits(credits: dict, now: float, compact: bool) -> str:
    expires = fmt_duration(round(credits["nextExpiresAt"]) - round(now)) if credits.get("nextExpiresAt") else None
    if credits["count"] == 0:
        return "No reset credits banked"
    if compact:
        return f"{credits['count']} banked" + (f" · expires in {expires}" if expires else "")
    noun = "reset credit" if credits["count"] == 1 else "reset credits"
    return f"{credits['count']} {noun} banked" + (f" · next expires in {expires}" if expires else "")


def cache_directory(base: str) -> str:
    """T3 writes provider snapshots to `<base>/../caches`, next to the state directory."""
    return os.path.join(os.path.dirname(os.path.normpath(base)), "caches")


def read_cache_records(base: str):
    """(records, skipped files), or (None, 0) when the caches directory does not exist."""
    directory = cache_directory(base)
    if not os.path.isdir(directory):
        return None, 0
    records, skipped = [], 0
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if not name.lower().endswith(".json") or not os.path.isfile(path):
            continue
        record = decode_limits_record(read_json(path), name[:-5])
        if record is None:
            skipped += 1
        else:
            records.append(record)
    return records, skipped


def read_limits(base: str, twenty_four: bool) -> dict:
    records, skipped = read_cache_records(base)
    if records is None:
        return {"state": "noCacheDirectory", "groups": [], "notices": [], "links": [], "skippedFiles": 0,
                "staleness": None, "footnote": LIMITS_FOOTNOTE}
    return render_limits(records, skipped, twenty_four)


def render_limits(records: list[dict], skipped: int, twenty_four: bool, source_notices=None,
                  footnote: str | None = None) -> dict:
    """T3's merge (LimitsMerge.snapshot) and layout (LimitsLayout.groups), rendered for the widget."""
    now = now_ms()
    visible = [r for r in records if r["enabled"] and r["installed"] and r["available"] and r["limits"] is not None]
    accounts = merged_accounts(visible, [r for r in records if r["enabled"]])
    notices = list(source_notices or [])
    for record in visible:
        if record.get("isSource"):
            continue
        limits = record["limits"]
        if limits["unavailableReason"] == "unsupported":
            continue
        notice = limits_notice(limits)
        if notice:
            notices.append(f"{record['trimmedName'] or record['driver']}: {notice}")
    links, link_order = {}, []
    for record in visible:
        external = record["limits"]["external"]
        if not external or not record["authenticated"]:
            continue
        name = record["displayName"] or record["instanceID"]
        if external["url"] not in links:
            link_order.append(external["url"])
            links[external["url"]] = {"label": external["label"], "url": external["url"],
                                      "message": record["limits"]["unavailableMessage"], "accounts": []}
        if name not in links[external["url"]]["accounts"]:
            links[external["url"]]["accounts"].append(name)
    link_list = []
    for url in link_order:
        link = links[url]
        body = "View usage in ChatGPT with your connected account." if url == "https://chatgpt.com/#settings/Usage" else link["message"]
        link_list.append({"label": link["label"], "url": url if url.startswith("https://") else None,
                          "body": body, "accounts": link["accounts"]})

    # Group by driver, accounts by soonest reset of the first pool (LimitsLayout.groups).
    driver_order, by_driver = [], {}
    for account in accounts:
        if account["driver"] not in by_driver:
            driver_order.append(account["driver"])
            by_driver[account["driver"]] = []
        by_driver[account["driver"]].append(account)
    groups = []
    for driver in driver_order:
        members = by_driver[driver]
        all_windows = sorted([w for a in members for w in a["windows"]], key=lambda w: w["kind"])
        first = all_windows[0] if all_windows else None

        def soonest(account, first=first, driver=driver):
            if first is None:
                return math.inf
            window = next((w for w in account["windows"] if pool_key(driver, w) == pool_key(driver, first)), None)
            if window is None:
                return math.inf
            reset = resolve_member(account, window, now)["window"].get("resetsAt")
            return round(reset) if reset is not None else math.inf

        members = sorted(members, key=lambda a: (soonest(a), a["nameOrder"]))
        rows = display_order(driver, limit_rows(members, now))
        shows_name = len(members) > 1
        out_accounts = []
        for account in members:
            line = [account["displayName"] or driver_label(driver)] if shows_name else []
            if account["plan"]:
                line.append(account["plan"])
            line.append(f"Checked {fmt_ago(account['checkedAt'], now)}")
            out_accounts.append({"id": account["id"], "name": account["displayName"] or driver_label(driver),
                                 "line": " · ".join(line)})
        out_rows = []
        for row in rows:
            bars = []
            for member in row["members"]:
                window = member["window"]
                reset = next((r for r in row["resets"] if r["accountID"] == member["account"]["id"]), None)
                even = elapsed_fraction(window, now)
                resets_text = None
                if window.get("resetsAt") is not None:
                    resets_text = fmt_clock(window["resetsAt"], now, twenty_four)
                    countdown = fmt_resets_in(window["resetsAt"], now)
                    if countdown:
                        resets_text += " · " + (("in " + countdown[len("resets in "):]) if countdown.startswith("resets in ") else countdown)
                credits = member["account"].get("credits")
                bars.append({
                    "account": member["account"]["displayName"] or driver_label(driver),
                    "left": js_round(100 - min(100, max(0, window["used"]))),
                    "used": min(100.0, max(0.0, window["used"])),
                    "evenPaceLeft": js_round((1 - even) * 100) if even is not None else None,
                    "restores": reset["restores"] if reset and reset["restores"] > 0 else 0,
                    "resets": resets_text,
                    "credits": fmt_credits(credits, now, True) if credits and credits["count"] > 0 else None,
                })
            out_rows.append({"id": row["id"], "label": row["label"], "kind": KIND_NAMES[row["kind"]],
                             "remaining": row["remaining"], "pace": row["pace"], "bars": bars})
        groups.append({"driver": driver, "label": driver_label(driver), "icon": DRIVER_ICONS.get(driver, ""),
                       "accounts": out_accounts, "rows": out_rows})

    newest = max((a["checkedAt"] for a in accounts), default=None)
    staleness = None
    if newest is not None and now - newest > STALE_AFTER_MS:
        staleness = (f"T3 Code last checked {fmt_ago(newest, now)}. "
                     "These numbers may be out of date. Open T3 Code to refresh them.")
    state = "noLimitsReported" if not accounts and not notices and not link_list else "ok"
    return {"state": state, "groups": groups, "notices": notices, "links": link_list, "skippedFiles": skipped,
            "staleness": staleness, "footnote": footnote or LIMITS_FOOTNOTE}


LIMITS_FOOTNOTE = "Read from T3 Code's last check of each provider. This widget never contacts a provider."

# ---------------------------------------------------------------------------
# Usage (UsageCacheDecoder, UsagePricing, UsageSelection, UsageReport)
# ---------------------------------------------------------------------------

USAGE_KINDS = ["codex", "claude", "grok", "cursor", "opencode", "antigravity", "pi", "muse"]
USAGE_STYLE = {
    "codex": ("Codex", "openai", None), "claude": ("Claude Code", "claude", "#d97757"),
    "grok": ("Grok Build", "grok", None), "cursor": ("Cursor", "cursor", "#8b8b8b"),
    "opencode": ("OpenCode", "opencode", "#5b9bbd"), "antigravity": ("Antigravity", "antigravity", "#8c7bd1"),
    "pi": ("Pi", "pi", "#4d9f8a"), "muse": ("Muse Code", "muse", "#0866ff"),
}
READER_ORDER = ["claude", "codex", "grok", "pi", "muse"]
SUPPORTED_VERSIONS = range(4, 12)
HOUR_MS = 3_600_000.0
DAY_MS = 86_400_000.0
FILE_SLACK_MS = 129_600_000.0
RETENTION_MS = 7_948_800_000.0
FIRST_PARTY = {"anthropic", "deepseek", "gemini", "meta", "mistral", "openai", "xai"}
UNPRICEABLE = {"<synthetic>", "synthetic", "opus", "sonnet", "haiku", "fable"}


def cache_generation(name: str) -> int | None:
    prefix, suffix = "usage-scan-cache", ".json"
    if not (name.startswith(prefix) and name.endswith(suffix)):
        return None
    middle = name[len(prefix):-len(suffix)]
    if not middle:
        return 0
    if middle.startswith("-v") and middle[2:].isdigit() and int(middle[2:]) > 0:
        return int(middle[2:])
    return None


def locate_cache(base: str):
    """Highest generation whose own `version` is supported. Never picks by modification time alone."""
    try:
        names = os.listdir(base)
    except OSError:
        return ("missing", None, None)
    candidates = []
    for name in names:
        generation = cache_generation(name)
        if generation is None:
            continue
        path = os.path.join(base, name)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        candidates.append((generation, path, stat.st_mtime))
    if not candidates:
        return ("missing", None, None)
    candidates.sort(key=lambda c: -c[0])
    skipped = []
    for _, path, mtime in candidates:
        version = cache_version(path)
        if version is None:
            continue
        if version not in SUPPORTED_VERSIONS:
            skipped.append((version, mtime))
            continue
        newer = next((s for s in skipped if s[1] > mtime), None)
        if newer:
            return ("unsupported", None, newer[0])
        return ("available", path, version)
    return ("unsupported", None, skipped[0][0] if skipped else None)


def cache_version(path: str) -> int | None:
    try:
        with open(path, "rb") as handle:
            head = handle.read(65536).decode("utf-8", "replace")
    except OSError:
        return None
    match = re.search(r'"version"\s*:\s*(\d+)', head)
    if match:
        return int(match.group(1))
    data = read_json(path)
    if isinstance(data, dict) and isinstance(data.get("version"), int):
        return data["version"]
    return None


def is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def decode_row(row, models, sessions, version):
    """One 11 to 13 item row tuple. None when the row is invalid."""
    if not isinstance(row, list) or len(row) < 11:
        return None
    for index in (0, 1, 3, 4, 5, 6, 7, 10):
        if not is_num(row[index]):
            return None

    def idx(value, count):
        if not is_num(value) or value != int(value):
            return None
        value = int(value)
        return value if 0 <= value < count else None

    def integer(value):
        return int(value) if abs(value) < 9e15 else None

    model = idx(row[1], len(models))
    speed = idx(row[10], 3)
    numbers = [integer(row[i]) for i in (3, 4, 5, 6, 7)]
    if model is None or speed is None or any(n is None for n in numbers):
        return None
    rate_model = None
    if len(row) > 11:
        if is_num(row[11]):
            rate = idx(row[11], len(models))
            if rate is None:
                return None
            rate_model = models[rate]
        elif row[11] is not None:
            return None
    session = ""
    if is_num(row[2]):
        s = idx(row[2], len(sessions))
        session = sessions[s] if s is not None else ""
    key = row[8] if isinstance(row[8], str) else None
    turn = row[12] if len(row) > 12 and isinstance(row[12], str) else None
    return {
        "ts": float(row[0]), "model": models[model], "rateModel": rate_model, "session": session,
        "uncached": numbers[0], "cached": numbers[1], "creation": numbers[2], "output": numbers[3],
        "reasoning": numbers[4], "reported": float(row[9]) if is_num(row[9]) else None,
        "speed": speed, "key": key, "turn": turn if version >= 7 else None,
    }


def load_dataset(path: str) -> dict:
    data = read_json(path)
    if not isinstance(data, dict):
        raise ValueError("unreadable")
    version = data.get("version")
    if version not in SUPPORTED_VERSIONS:
        raise ValueError("unsupported")
    models = [m if isinstance(m, str) else "" for m in data.get("models") or []]
    sessions = [s if isinstance(s, str) else "" for s in data.get("sessions") or []]
    sources = []
    for key, value in (data.get("sources") or {}).items():
        if isinstance(value, dict) and isinstance(value.get("dir"), str):
            sources.append({"kind": key.split("\x00", 1)[0], "dir": value["dir"]})
    files, rejected = [], 0
    for file_path, entry in (data.get("files") or {}).items():
        if not isinstance(entry, dict) or not isinstance(entry.get("p"), str) or not is_num(entry.get("m")):
            rejected += 1
            continue
        records, valid = [], True
        for field in ("r", "t"):
            for row in entry.get(field) or []:
                record = decode_row(row, models, sessions, version)
                if record is None:
                    valid = False
                    continue
                if entry["p"] == "claude" and record["model"] == "<synthetic>":
                    continue
                records.append(record)
        if not valid or any(r[k] < 0 for r in records for k in ("uncached", "cached", "creation", "output", "reasoning")):
            rejected += 1
            continue
        files.append({"path": file_path, "kind": entry["p"], "modified": float(entry["m"]),
                      "malformed": int(entry.get("mr") or 0) if is_num(entry.get("mr")) else 0, "records": records})
    return {"version": version, "sources": sources, "files": files, "rejected": rejected,
            "modified": os.stat(path).st_mtime * 1000}


def load_prices(base: str):
    table: dict[str, dict] = {}
    data = read_json(os.path.join(base, "usage-model-rates.json"))
    document = data.get("document") if isinstance(data, dict) else None
    order = []
    if isinstance(document, dict):
        for name, entry in document.items():
            if not isinstance(entry, dict):
                continue
            values = {k: float(v) for k, v in entry.items() if is_num(v) and "cost" in k}
            standard = token_rates(values, "", None)
            if standard is None:
                continue
            key = name.strip().lower()
            if not key:
                continue
            multiplier = None
            pse = entry.get("provider_specific_entry")
            if isinstance(pse, dict) and is_num(pse.get("fast")) and pse["fast"] > 0:
                multiplier = float(pse["fast"])
            fast = {k: v * multiplier for k, v in standard.items()} if multiplier else token_rates(values, "_priority", standard)
            if key not in table:
                order.append(key)
            table[key] = {"standard": standard, "fast": fast, "ultrafast": token_rates(values, "_ultrafast", standard)}
    # `provider/name` also answers to `name`, first-party providers win, conflicts cancel.
    candidates, alias_order = {}, []
    for key in order:
        if "/" not in key:
            continue
        alias = key[key.rfind("/") + 1:]
        if not alias or alias in table:
            continue
        first_party = key[: key.find("/")] in FIRST_PARTY
        rate = table[key]
        if alias in candidates:
            held_rate, held_fp = candidates[alias]
            if first_party and not held_fp:
                candidates[alias] = (rate, True)
            elif held_rate is not None and first_party == held_fp:
                same = held_rate["standard"] == rate["standard"]
                fast = held_rate["fast"] or rate["fast"]
                ultra = held_rate["ultrafast"] or rate["ultrafast"]
                ok = same and fast == (rate["fast"] or fast) and ultra == (rate["ultrafast"] or ultra)
                candidates[alias] = ({"standard": held_rate["standard"], "fast": fast, "ultrafast": ultra} if ok else None, first_party)
        else:
            candidates[alias] = (rate, first_party)
            alias_order.append(alias)
    for alias in alias_order:
        if candidates[alias][0] is not None:
            table[alias] = candidates[alias][0]

    overrides, aliases = {}, {}
    settings = read_json(os.path.join(base, "settings.json"))
    if isinstance(settings, dict):
        raw_overrides = settings.get("usagePriceOverrides")
        if isinstance(raw_overrides, dict):
            for model, price in raw_overrides.items():
                if not isinstance(price, dict):
                    continue
                inp, out = price.get("inputCostPerMillionTokens"), price.get("outputCostPerMillionTokens")
                if not (is_num(inp) and is_num(out)):
                    continue
                read = price.get("cacheReadCostPerMillionTokens")
                write = price.get("cacheWriteCostPerMillionTokens")
                overrides[model.strip()] = {"standard": {
                    "input": inp / 1e6, "output": out / 1e6,
                    "cacheRead": (read if is_num(read) else inp) / 1e6,
                    "cacheWrite": (write if is_num(write) else inp) / 1e6,
                }, "fast": None, "ultrafast": None}
        raw_aliases = settings.get("usageModelAliases")
        if isinstance(raw_aliases, dict):
            raw = {k: v for k, v in raw_aliases.items() if isinstance(v, str)}
            for model in raw:
                seen, target = {model}, raw[model]
                while target in raw and target not in seen:
                    seen.add(target)
                    target = raw[target]
                if target not in raw:
                    aliases[model] = target
    return table, overrides, aliases


def token_rates(values, suffix, standard):
    inp, out = values.get("input_cost_per_token" + suffix), values.get("output_cost_per_token" + suffix)
    if inp is None or out is None:
        return None

    def cache(name, standard_rate):
        if name + suffix in values:
            return values[name + suffix]
        if standard and standard["input"] > 0 and standard_rate is not None:
            return standard_rate / standard["input"] * inp
        return inp

    return {
        "input": inp, "output": out,
        "cacheRead": cache("cache_read_input_token_cost", standard["cacheRead"] if standard else None),
        "cacheWrite": cache("cache_creation_input_token_cost", standard["cacheWrite"] if standard else None),
    }


def rate_for_model(table, model: str):
    key = model.strip().lower()
    if "[" in key:
        key = key[: key.find("[")]
    name = key[key.rfind("/") + 1:] if "/" in key else key
    if not name or name in UNPRICEABLE:
        return None
    return table.get(key)


def plain_sum(values) -> float:
    """Left-to-right addition, as Swift and JavaScript do. Python's sum() compensates
    float error since 3.12, which moves a rounded cent away from T3 Code's numbers."""
    total = 0.0
    for value in values:
        total += value
    return total


def categories(record, rates):
    return {
        "input": record["uncached"] * rates["input"], "cacheRead": record["cached"] * rates["cacheRead"],
        "cacheWrite": record["creation"] * rates["cacheWrite"], "output": record["output"] * rates["output"],
    }


def price_record(record, lookup, overrides):
    override = overrides.get(record["model"].strip())
    rate = override if override else lookup(record["rateModel"] or record["model"])
    reported = None if override else (record["reported"] if record["reported"] is not None and math.isfinite(record["reported"]) else None)
    if rate is None:
        return {"cost": reported or 0.0, "source": "unpriced" if reported is None else "reported",
                "cats": None, "premium": 0.0, "savings": 0.0}
    active = rate[("standard", "fast", "ultrafast")[record["speed"]]] or rate["standard"]
    cats = categories(record, active)
    total = plain_sum(cats.values())
    savings = record["cached"] * (active["input"] - active["cacheRead"])
    if reported is not None and total <= 0:
        return {"cost": reported, "source": "reported", "cats": None, "premium": 0.0, "savings": savings}
    premium = 0.0 if record["speed"] == 0 else total - plain_sum(categories(record, rate["standard"]).values())
    scale = reported / total if reported is not None else 1.0
    return {"cost": reported if reported is not None else total, "source": "priced" if reported is None else "reported",
            "cats": {k: v * scale for k, v in cats.items()}, "premium": premium * scale, "savings": savings}


def local_day(ms: float) -> int:
    local = datetime.fromtimestamp(ms / 1000).astimezone()
    return (local.date() - datetime(1970, 1, 1).date()).days


def day_start_ms(day: int) -> float:
    date = datetime(1970, 1, 1) + timedelta(days=day)
    return date.astimezone().timestamp() * 1000  # naive datetime interpreted as local time


def make_window(days: int, now: float) -> dict:
    if days == 1:
        until = math.floor(now / 60_000) * 60_000
        since = until - 24 * HOUR_MS
        return {"hourly": True, "since": since, "until": until, "sinceDay": local_day(since), "untilDay": local_day(until),
                "now": now}
    today = local_day(now)
    return {"hourly": False, "sinceDay": today - (max(days, 1) - 1), "untilDay": today, "now": now}


def window_slot(window, ts):
    if window["hourly"]:
        if not window["since"] <= ts < window["until"]:
            return None
        return (local_day(ts), int((ts - window["since"]) // HOUR_MS))
    margin = 14 * HOUR_MS
    if not (window["sinceDay"] * DAY_MS - margin <= ts < (window["untilDay"] + 1) * DAY_MS + margin):
        return None
    day = local_day(ts)
    return (day, -1) if window["sinceDay"] <= day <= window["untilDay"] else None


def file_cutoff(window) -> float:
    start = (window["since"] if window["hourly"] else window["sinceDay"] * DAY_MS) - FILE_SLACK_MS
    return max(start, window["now"] - RETENTION_MS)


def is_within(path: str, directory: str) -> bool:
    path, directory = os.path.normpath(path), os.path.normpath(directory)
    return path == directory or path.startswith(directory.rstrip("/") + "/")


def js_round6(value: float) -> float:
    return math.floor(value * 1e6 + 0.5) / 1e6


def build_report(dataset: dict, window: dict, prices) -> dict:
    table, overrides, aliases = prices
    rate_cache = {}

    def lookup(name):
        if name not in rate_cache:
            rate_cache[name] = rate_for_model(table, name)
        return rate_cache[name]

    cutoff = file_cutoff(window)
    ordered = [s for k in READER_ORDER for s in dataset["sources"] if s["kind"] == k]
    ordered += [s for s in dataset["sources"] if s["kind"] not in READER_ORDER]
    groups = [[f for f in dataset["files"] if f["kind"] == s["kind"] and f["modified"] >= cutoff and is_within(f["path"], s["dir"])]
              for s in ordered]

    # Codex sessions that appear in more than one file (forks and subagents).
    first_file, shared, index = {}, set(), 0
    for group in groups:
        for file in group:
            if file["kind"] == "codex":
                previous = ""
                for record in file["records"]:
                    s = record["session"]
                    if s and s != previous:
                        previous = s
                        if s in first_file:
                            if first_file[s] != index:
                                shared.add(s)
                        else:
                            first_file[s] = index
            index += 1

    columns = report_columns(window)

    buckets, bucket_index = [], {}
    seen = set()
    duplicates = 0
    provider_sessions: dict[str, int] = {}
    malformed = 0
    for source_index, group in enumerate(groups):
        kind = ordered[source_index]["kind"]
        sessions = set()
        for file in group:
            malformed += file["malformed"]
            occurrences = {}
            is_codex = file["kind"] == "codex" and bool(shared)
            for record in file["records"]:
                key = ("t", record["key"]) if record["key"] is not None else None
                if is_codex and record["session"] in shared:
                    base_key_ = (record["session"], record["ts"], record["model"], record["uncached"], record["cached"],
                                 record["creation"], record["output"], record["reasoning"])
                    occurrences[base_key_] = occurrences.get(base_key_, 0) + 1
                    key = ("c", base_key_, occurrences[base_key_])
                if key is not None:
                    if key in seen:
                        duplicates += 1
                        continue
                    seen.add(key)
                slot = window_slot(window, record["ts"])
                if slot is None:
                    continue
                if record["session"]:
                    sessions.add(record["session"])
                model = record["model"]
                rate_model = record["rateModel"]
                if model in aliases:
                    model, rate_model = aliases[model], None
                priced = price_record(dict(record, model=model, rateModel=rate_model), lookup, overrides)
                processed = record["uncached"] + record["cached"] + record["creation"] + record["output"]
                column = columns[slot[1]] if window["hourly"] else columns[slot[0] - window["sinceDay"]]
                amount = column["byProvider"].setdefault(file["kind"], {"cost": 0.0, "tokens": 0})
                amount["cost"] += priced["cost"]
                amount["tokens"] += processed
                bkey = (slot[0], slot[1], source_index, model)
                if bkey not in bucket_index:
                    bucket_index[bkey] = len(buckets)
                    buckets.append({"kind": file["kind"], "model": model, "uncached": 0, "cached": 0, "creation": 0,
                                    "output": 0, "reasoning": 0, "cost": 0.0, "savings": 0.0, "cats": None,
                                    "fast": 0.0, "ultrafast": 0.0, "premium": 0.0, "records": 0, "unpriced": 0})
                bucket = buckets[bucket_index[bkey]]
                for field in ("uncached", "cached", "creation", "output", "reasoning"):
                    bucket[field] += record[field]
                bucket["cost"] += priced["cost"]
                bucket["savings"] += priced["savings"]
                if priced["cats"]:
                    cats = bucket["cats"] or {"input": 0.0, "cacheRead": 0.0, "cacheWrite": 0.0, "output": 0.0}
                    for field, value in priced["cats"].items():
                        cats[field] += value
                    bucket["cats"] = cats
                if record["speed"] == 1:
                    bucket["fast"] += priced["cost"]
                if record["speed"] == 2:
                    bucket["ultrafast"] += priced["cost"]
                bucket["premium"] += priced["premium"]
                bucket["records"] += 1
                if priced["source"] == "unpriced":
                    bucket["unpriced"] += 1
        if sessions:
            provider_sessions[kind] = provider_sessions.get(kind, 0) + len(sessions)

    return finish_report(buckets, columns, provider_sessions, window["hourly"], duplicates=duplicates,
                         rejected=dataset["rejected"], malformed=malformed)


def report_columns(window: dict) -> list[dict]:
    if window["hourly"]:
        return [{"start": window["since"] + i * HOUR_MS, "byProvider": {}} for i in range(24)]
    return [{"day": d, "start": day_start_ms(d), "byProvider": {}} for d in range(window["sinceDay"], window["untilDay"] + 1)]


def finish_report(buckets: list[dict], columns: list[dict], provider_sessions: dict, hourly: bool,
                  duplicates: int = 0, rejected: int = 0, malformed: int = 0) -> dict:
    """Totals, providers, models and chart of the Cost and Tokens tabs (UsageReport), from
    (day or hour, provider, model) buckets."""

    def empty_tokens():
        return {"uncached": 0, "cached": 0, "creation": 0, "output": 0, "reasoning": 0}

    totals = empty_tokens()
    cost = savings = 0.0
    by_type = {"input": 0.0, "cacheRead": 0.0, "cacheWrite": 0.0, "output": 0.0}
    by_speed = {"fast": 0.0, "ultrafast": 0.0, "premium": 0.0}
    records = unpriced = 0
    providers, models = {}, {}
    for bucket in buckets:
        for field in totals:
            totals[field] += bucket[field]
        cost += bucket["cost"]
        savings += bucket["savings"]
        if bucket["cats"]:
            for field in by_type:
                by_type[field] += js_round6(bucket["cats"][field])
        for field in by_speed:
            by_speed[field] += js_round6(bucket[field])
        records += bucket["records"]
        unpriced += bucket["unpriced"]
        provider = providers.setdefault(bucket["kind"], {"tokens": empty_tokens(), "cost": 0.0, "records": 0})
        model = models.setdefault((bucket["kind"], bucket["model"]),
                                  {"tokens": empty_tokens(), "cost": 0.0, "records": 0, "unpriced": 0})
        for field in totals:
            provider["tokens"][field] += bucket[field]
            model["tokens"][field] += bucket[field]
        provider["cost"] += bucket["cost"]
        provider["records"] += bucket["records"]
        model["cost"] += bucket["cost"]
        model["records"] += bucket["records"]
        model["unpriced"] += bucket["unpriced"]
    by_type["other"] = max(0.0, cost - plain_sum(by_type.values()))
    by_speed["standard"] = max(0.0, cost - by_speed["fast"] - by_speed["ultrafast"])

    def processed(tokens):
        return tokens["uncached"] + tokens["cached"] + tokens["creation"] + tokens["output"]

    def style(kind):
        label, icon, accent = USAGE_STYLE.get(kind, (kind, "", "#8b8b8b"))
        return {"kind": kind, "label": label, "icon": icon, "accent": accent}

    def order(kind):
        return (USAGE_KINDS.index(kind), "") if kind in USAGE_KINDS else (len(USAGE_KINDS), kind)

    provider_list = []
    for kind in sorted(providers, key=order):
        p = providers[kind]
        if processed(p["tokens"]) <= 0 and p["cost"] <= 0:
            continue
        provider_list.append(dict(style(kind), cost=p["cost"], tokens=processed(p["tokens"]),
                                  sessions=provider_sessions.get(kind, 0),
                                  costShare=(p["cost"] / cost) if cost > 0 else 0.0,
                                  tokenShare=(processed(p["tokens"]) / processed(totals)) if processed(totals) > 0 else 0.0))
    model_list = sorted(
        ({"kind": k, "name": n, "icon": style(k)["icon"], "cost": m["cost"], "tokens": processed(m["tokens"]),
          "unpriced": m["records"] > 0 and m["unpriced"] >= m["records"]} for (k, n), m in models.items()),
        key=lambda m: (-m["cost"], -m["tokens"], m["kind"], m["name"]),
    )
    chart_kinds = [p["kind"] for p in provider_list]
    chart = [{"start": c["start"], "values": [{"kind": k, "cost": c["byProvider"].get(k, {}).get("cost", 0.0),
                                                "tokens": c["byProvider"].get(k, {}).get("tokens", 0)} for k in chart_kinds]}
             for c in columns]
    input_total = totals["uncached"] + totals["cached"] + totals["creation"]
    return {
        "cost": cost, "processed": processed(totals), "tokens": totals,
        "cacheHit": (totals["cached"] / input_total) if input_total > 0 else None,
        "cacheSavings": savings, "costByType": by_type, "costBySpeed": by_speed,
        "providers": provider_list, "models": model_list, "chart": chart, "hourly": hourly,
        "sessions": sum(provider_sessions.values()), "records": records, "unpriced": unpriced,
        "unpricedShare": (unpriced / records) if records else 0.0, "duplicatesDropped": duplicates,
        "rejectedFiles": rejected, "malformed": malformed,
    }


def usage_request(days: int, now: float, time_zone: str) -> tuple[dict, dict]:
    """The `server.getUsageSummary` input T3 Code's Usage page sends for a period, and the
    window its buckets are laid out in. The past 24 hours are hourly, from the last minute."""
    window = make_window(days, now)

    def day(index: int) -> str:
        return (datetime(1970, 1, 1) + timedelta(days=index)).strftime("%Y-%m-%d")

    request = {"sinceDay": day(window["sinceDay"]), "untilDay": day(window["untilDay"]), "timeZone": time_zone,
               "resolution": "hour" if window["hourly"] else "day"}
    if window["hourly"]:
        def iso(ms: float) -> str:
            return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(ms % 1000):03d}Z"

        request.update(sinceTime=iso(window["since"]), untilTime=iso(window["until"]))
    return request, window


def report_from_summary(summary, window: dict) -> dict:
    """The Cost and Tokens tabs from the local T3 server's usage summary: the same buckets
    T3 Code's Usage page draws, already priced by the server."""
    if not isinstance(summary, dict) or not isinstance(summary.get("buckets"), list):
        raise ValueError("unsupported summary")
    columns = report_columns(window)
    buckets, index = [], {}
    for raw in summary["buckets"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("provider"), str) or not isinstance(raw.get("model"), str):
            continue
        totals = raw.get("totals") if isinstance(raw.get("totals"), dict) else {}
        tokens = {field: int(totals[key]) if is_num(totals.get(key)) and totals[key] >= 0 else 0
                  for field, key in (("uncached", "uncachedInputTokens"), ("cached", "cachedInputTokens"),
                                     ("creation", "cacheCreationTokens"), ("output", "outputTokens"),
                                     ("reasoning", "reasoningTokens"))}
        if window["hourly"]:
            start = parse_date(raw.get("hourStart"))
            if start is None:
                continue
            column = int((start - window["since"]) // HOUR_MS)
            key = (column, raw["provider"], raw["model"])
        else:
            try:
                parsed = datetime.strptime(str(raw.get("day")), "%Y-%m-%d")
            except ValueError:
                continue
            column = (parsed - datetime(1970, 1, 1)).days - window["sinceDay"]
            key = (column, raw["provider"], raw["model"])
        if not 0 <= column < len(columns):
            continue

        def amount(name):
            value = raw.get(name)
            return float(value) if is_num(value) and math.isfinite(value) else 0.0

        cost = amount("costUsd")
        processed_tokens = tokens["uncached"] + tokens["cached"] + tokens["creation"] + tokens["output"]
        chart_amount = columns[column]["byProvider"].setdefault(raw["provider"], {"cost": 0.0, "tokens": 0})
        chart_amount["cost"] += cost
        chart_amount["tokens"] += processed_tokens
        # Buckets per transcript file fold into one per (slot, provider, model), as in the cache report.
        if key not in index:
            index[key] = len(buckets)
            buckets.append({"kind": raw["provider"], "model": raw["model"], "uncached": 0, "cached": 0, "creation": 0,
                            "output": 0, "reasoning": 0, "cost": 0.0, "savings": 0.0, "cats": None,
                            "fast": 0.0, "ultrafast": 0.0, "premium": 0.0, "records": 0, "unpriced": 0})
        bucket = buckets[index[key]]
        for field, value in tokens.items():
            bucket[field] += value
        bucket["cost"] += cost
        bucket["savings"] += amount("cacheSavingsUsd")
        cats = raw.get("categoryCostUsd")
        if isinstance(cats, dict):
            total = bucket["cats"] or {"input": 0.0, "cacheRead": 0.0, "cacheWrite": 0.0, "output": 0.0}
            for field in total:
                value = cats.get(field)
                total[field] += float(value) if is_num(value) and math.isfinite(value) else 0.0
            bucket["cats"] = total
        bucket["fast"] += amount("fastCostUsd")
        bucket["ultrafast"] += amount("ultrafastCostUsd")
        bucket["premium"] += amount("speedPremiumUsd")
        bucket["records"] += int(raw["records"]) if is_num(raw.get("records")) else 0
        bucket["unpriced"] += int(raw["unpricedRecords"]) if is_num(raw.get("unpricedRecords")) else 0

    # Bucket session counts double-count sessions that span days or models; sources count them once.
    sessions, skipped, malformed = {}, 0, 0
    for source in summary.get("sources") or []:
        if not isinstance(source, dict):
            continue
        provider = (source.get("fingerprint") or {}).get("provider") if isinstance(source.get("fingerprint"), dict) else None
        if isinstance(provider, str) and is_num(source.get("distinctSessions")):
            sessions[provider] = sessions.get(provider, 0) + int(source["distinctSessions"])
        skipped += int(source["skippedFiles"]) if is_num(source.get("skippedFiles")) else 0
        malformed += int(source["malformedRecords"]) if is_num(source.get("malformedRecords")) else 0
    return finish_report(buckets, columns, sessions, window["hourly"], rejected=skipped, malformed=malformed)


def usage(base: str, days: int) -> dict:
    state, path, info = locate_cache(base)
    now = now_ms()
    if state == "missing":
        return {"state": "missing", "message": "Open T3 Code's Usage page once so it writes its usage cache."}
    if state == "unsupported":
        return {"state": "unsupported",
                "message": "T3 Code changed its usage format" + (f" (v{info})" if info else "") + ". Update this widget."}
    try:
        dataset = load_dataset(path)
    except (ValueError, OSError):
        return {"state": "busy", "message": "T3 Code is rewriting its usage cache. Retrying."}
    report = build_report(dataset, make_window(days, now), load_prices(base))
    report.update(state="ok", period=days, cacheModified=dataset["modified"],
                  cacheAge=fmt_ago(dataset["modified"], now), version=dataset["version"])
    return report


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def emit(payload) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n")
    sys.stdout.flush()


def signature(base: str):
    parts = []
    for name in ("statev2.sqlite", "statev2.sqlite-wal", "server-runtime.json", "environment-id"):
        try:
            stat = os.stat(os.path.join(base, name))
            parts.append((stat.st_size, stat.st_mtime_ns))
        except OSError:
            parts.append(None)
    return tuple(parts)


def watch(base: str, interval: float) -> None:
    """Emits the inbox whenever it changes. Re-reads on file changes and every few seconds
    regardless, because snooze timers and server liveness change without a write."""
    last_output, last_sig, last_read = None, None, 0.0
    while True:
        sig = signature(base)
        now = time.monotonic()
        if sig != last_sig or now - last_read >= 5 or last_output is None:
            last_sig, last_read = sig, now
            payload = snapshot(base)
            text = json.dumps(payload, sort_keys=True)
            if text != last_output:
                last_output = text
                emit(payload)
        time.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state-dir", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    w = sub.add_parser("watch")
    w.add_argument("--interval", type=float, default=1.0)
    sub.add_parser("threads")
    p = sub.add_parser("preview")
    p.add_argument("thread_id")
    lim = sub.add_parser("limits")
    lim.add_argument("--clock", choices=["12", "24"], default="24")
    u = sub.add_parser("usage")
    u.add_argument("--period", type=int, choices=[1, 7, 30, 90], default=30)
    args = parser.parse_args()
    base = state_dir(args.state_dir)

    try:
        if args.command == "watch":
            watch(base, max(0.25, args.interval))
        elif args.command == "threads":
            emit(snapshot(base))
        elif args.command == "preview":
            emit(preview(base, args.thread_id))
        elif args.command == "limits":
            emit(read_limits(base, args.clock == "24"))
        elif args.command == "usage":
            emit(usage(base, args.period))
    except (KeyboardInterrupt, BrokenPipeError):
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
