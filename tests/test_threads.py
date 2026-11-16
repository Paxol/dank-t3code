#!/usr/bin/env python3
"""Checks which statev2.sqlite threads reach the inbox, against a synthetic database.

    python3 -I tests/test_threads.py
"""

import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
import t3bar  # noqa: E402

SCHEMA = """
CREATE TABLE orchestration_v2_projection_threads (
 thread_id TEXT PRIMARY KEY, title TEXT NOT NULL, project_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, payload_json TEXT NOT NULL,
 deleted_at TEXT, archived_at TEXT);
CREATE TABLE projection_projects (project_id TEXT PRIMARY KEY, title TEXT, project_icon_json TEXT,
 favicon_path TEXT, workspace_root TEXT);
CREATE TABLE orchestration_v2_projection_runs (thread_id TEXT, status TEXT, payload_json TEXT, ordinal INTEGER,
 run_id TEXT, requested_at TEXT, completed_at TEXT);
CREATE TABLE orchestration_v2_projection_runtime_requests (thread_id TEXT, status TEXT, kind TEXT);
CREATE TABLE projection_thread_pull_requests (thread_id TEXT, number INTEGER);
CREATE TABLE orchestration_v2_projection_provider_threads (thread_id TEXT, payload_json TEXT, last_run_ordinal INTEGER);
CREATE TABLE orchestration_v2_projection_messages (thread_id TEXT, role TEXT, created_at TEXT, updated_at TEXT,
 payload_json TEXT);
CREATE TABLE orchestration_v2_projection_turn_items (thread_id TEXT, run_id TEXT, type TEXT, status TEXT,
 node_id TEXT, payload_json TEXT, updated_at TEXT, ordinal INTEGER, turn_item_id TEXT);
INSERT INTO projection_projects VALUES ('p', 'Project', NULL, NULL, '/nowhere');
"""

THREADS = {
    "kept": "{}",
    "not-overridden": '{"settledOverride":"unsettled"}',
    "settled": '{"settledAt":"2026-11-01T00:00:00Z"}',
    "settled-override": '{"settledOverride":"settled"}',
    "delegated": '{"delegatedFrom":{"threadId":"kept"}}',
    "side-chat": '{"lineage":{"sideChat":true}}',
    "subagent": '{"lineage":{"relationshipToParent":"subagent"}}',
}


def main():
    failures = 0

    def check(name, condition):
        nonlocal failures
        print(("ok   " if condition else "FAIL ") + name)
        failures += 0 if condition else 1

    with tempfile.TemporaryDirectory() as base:
        conn = sqlite3.connect(os.path.join(base, "statev2.sqlite"))
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO orchestration_v2_projection_threads VALUES (?, ?, 'p', ?, ?, ?, NULL, NULL)",
            [(ident, ident, "2026-11-01T00:00:00Z", "2026-11-01T00:00:00Z", payload)
             for ident, payload in THREADS.items()])
        conn.commit()
        conn.close()
        ids = {thread["id"] for thread in t3bar.fetch_threads(base)}

    check("ordinary threads are listed", ids == {"kept", "not-overridden"})
    for ident in ("settled", "settled-override", "delegated", "side-chat", "subagent"):
        check(f"{ident} thread is left out", ident not in ids)
    print("OK" if not failures else f"{failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
