"""Focused proof for the pm-ops `issue-thread` record (Rajiv DM D0AMF0XE6TS thread
1794720837.453299, ts 1794720957.200479: "go. track the issue thread in pm ops db to save
lookups.").

Covers canonical source scripts/pm/shared-assets/claude/scripts/pm-ops-legacy.py:
  - `issue-thread set|get` persist and read one row per issue;
  - resolution order when unset: (a) issue body Source/thread_ts (dev channel or DM),
    (b) HeyDonna Alerts PR thread from the Slack bridge DB, (c) NONE;
  - the customer channel is never stored, returned or resolved to;
  - a stored customer/garbage row is never returned.

Hermetic: runs the canonical source as a subprocess against a tmp PM_OPS_DB and a tmp
PM_OPS_BRIDGE_DB, with a stub `gh` first on PATH. No live DB, Slack or GitHub.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[3]
CANDIDATE = ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "scripts" / "pm-ops-legacy.py"

DEV = "C0ALZJHGE49"
CUSTOMER = "C0A56RX6FNW"
DM = "D0AMF0XE6TS"


def make_gh(tmp_path: Path, body: str | None) -> Path:
    """Stub gh: `issue view ... --json body` prints {"body": ...}; body None = failure."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    if body is None:
        gh.write_text("#!/bin/sh\necho boom >&2\nexit 1\n")
    else:
        payload = tmp_path / "gh-body.json"
        payload.write_text(json.dumps({"body": body}))
        gh.write_text(f"#!/bin/sh\ncat {payload}\n")
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    return bindir


def make_bridge(tmp_path: Path, rows: list[tuple[str, str, str, str]]) -> Path:
    """rows: (channel_id, thread_ts, started_by_name, first_message)."""
    db = tmp_path / "bridge.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE threads (thread_ts TEXT NOT NULL, channel_id TEXT NOT NULL, topic TEXT, "
        "status TEXT NOT NULL DEFAULT 'active', started_by TEXT, started_by_name TEXT, "
        "first_message TEXT, message_count INTEGER NOT NULL DEFAULT 0, "
        "last_activity TEXT NOT NULL DEFAULT (datetime('now')), "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')), PRIMARY KEY (thread_ts, channel_id))"
    )
    for channel, thread_ts, by, first in rows:
        con.execute(
            "INSERT INTO threads(thread_ts, channel_id, started_by_name, first_message) VALUES (?,?,?,?)",
            (thread_ts, channel, by, first),
        )
    con.commit()
    con.close()
    return db


def run(tmp_path: Path, *args: str, body: str | None = "", bridge: Path | None = None,
        db: str | None = None) -> subprocess.CompletedProcess[str]:
    bindir = make_gh(tmp_path, body)
    env = dict(
        os.environ,
        PM_OPS_DB=db or str(tmp_path / "pm-ops.db"),
        PM_OPS_BRIDGE_DB=str(bridge or (tmp_path / "absent-bridge.db")),
        PATH=f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
    )
    return subprocess.run(
        [sys.executable, "-I", str(CANDIDATE), *args],
        env=env, capture_output=True, text=True,
    )


def stored_rows(tmp_path: Path) -> list[tuple]:
    con = sqlite3.connect(tmp_path / "pm-ops.db")
    try:
        return con.execute("SELECT issue, channel, thread_ts, source FROM issue_threads ORDER BY issue").fetchall()
    finally:
        con.close()


def test_canonical_source_is_present() -> None:
    assert CANDIDATE.is_file(), f"canonical source missing: {CANDIDATE}"


def test_set_then_get_round_trips_without_resolving(tmp_path) -> None:
    done = run(tmp_path, "issue-thread", "set", "--issue", "13671", "--channel", DEV,
               "--thread", "1794296562.075779", "--source", "manual")
    assert done.returncode == 0, done.stderr
    got = run(tmp_path, "issue-thread", "get", "--issue", "13671", "--no-resolve", body=None)
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip().split("\t") == [DEV, "1794296562.075779", "manual"]
    assert stored_rows(tmp_path) == [(13671, DEV, "1794296562.075779", "manual")]


def test_get_json_format(tmp_path) -> None:
    run(tmp_path, "issue-thread", "set", "--issue", "5", "--channel", DM, "--thread", "1793109340.474709")
    got = run(tmp_path, "issue-thread", "get", "--issue", "5", "--format", "json", body=None)
    assert got.returncode == 0, got.stderr
    assert json.loads(got.stdout) == {
        "issue": 5, "channel": DM, "thread_ts": "1793109340.474709", "source": "manual",
    }


def test_set_refuses_customer_channel_and_malformed_targets(tmp_path) -> None:
    for channel, thread in ((CUSTOMER, "1794296562.075779"), (DEV, "not-a-ts"), ("lowercase", "1794296562.075779")):
        done = run(tmp_path, "issue-thread", "set", "--issue", "9", "--channel", channel, "--thread", thread)
        assert done.returncode == 2, f"{channel}/{thread} must be refused: {done.stdout}{done.stderr}"
    assert not (tmp_path / "pm-ops.db").exists() or stored_rows(tmp_path) == []


def test_unset_with_nothing_found_prints_none_and_exits_1(tmp_path) -> None:
    got = run(tmp_path, "issue-thread", "get", "--issue", "424242", body="no thread reference here")
    assert got.returncode == 1
    assert got.stdout.strip() == "NONE"
    assert stored_rows(tmp_path) == []


def test_resolves_from_issue_body_and_persists(tmp_path) -> None:
    body = "## Source\nRajiv, C0ALZJHGE49 thread 1793944749.704259, ts 1793980825.036029\n"
    got = run(tmp_path, "issue-thread", "get", "--issue", "12983", body=body)
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip().split("\t") == [DEV, "1793944749.704259", "issue-body"]
    assert stored_rows(tmp_path) == [(12983, DEV, "1793944749.704259", "issue-body")]
    # Second call is served from the stored row: gh failing must not matter.
    again = run(tmp_path, "issue-thread", "get", "--issue", "12983", body=None)
    assert again.returncode == 0, again.stderr
    assert again.stdout.strip().split("\t")[:2] == [DEV, "1793944749.704259"]


def test_issue_body_accepts_dm_thread_and_archive_url(tmp_path) -> None:
    dm_body = "Source: DM D0AMF0XE6TS thread_ts: 1793109340.474709"
    dm = run(tmp_path, "issue-thread", "get", "--issue", "1", body=dm_body)
    assert dm.stdout.strip().split("\t")[:2] == [DM, "1793109340.474709"]
    url_body = "see https://heydonna.slack.com/archives/C0ALZJHGE49/p1793350227955789?thread_ts=1793350100.000100"
    url = run(tmp_path, "issue-thread", "get", "--issue", "2", body=url_body)
    assert url.stdout.strip().split("\t")[:2] == [DEV, "1793350100.000100"]


def test_issue_body_never_resolves_to_customer_or_other_channels(tmp_path) -> None:
    body = (
        f"Customer report: {CUSTOMER} thread 1790000000.000100\n"
        "PM channel C0AGWPQFKHA thread 1790000001.000100\n"
    )
    got = run(tmp_path, "issue-thread", "get", "--issue", "7", body=body)
    assert got.returncode == 1 and got.stdout.strip() == "NONE", got.stdout + got.stderr
    assert stored_rows(tmp_path) == []


def test_issue_body_first_eligible_reference_wins_in_document_order(tmp_path) -> None:
    body = (
        f"{CUSTOMER} thread 1790000000.000100 (customer, skipped)\n"
        f"{DEV} thread 1791111111.111111 (first eligible)\n"
        f"{DEV} thread 1792222222.222222 (later)\n"
    )
    got = run(tmp_path, "issue-thread", "get", "--issue", "8", body=body)
    assert got.stdout.strip().split("\t")[:2] == [DEV, "1791111111.111111"]


def test_falls_back_to_pr_alert_thread_when_body_has_none(tmp_path) -> None:
    bridge = make_bridge(tmp_path, [
        (DEV, "1794000000.000100", "HeyDonna Alerts", "PR #13700 opened: https://github.com/Scribie/heydonna-app/pull/13700|x"),
        (DEV, "1794000100.000100", "HeyDonna Alerts", "Branch `agent/13671-issue-thread` PR #13700 CI green"),
        (DEV, "1794000200.000100", "Someone Else", "PR #13700 is not an alert thread"),
        (CUSTOMER, "1794000300.000100", "HeyDonna Alerts", "PR #13700 customer channel is never a target"),
    ])
    got = run(tmp_path, "issue-thread", "get", "--issue", "13671", "--pr", "13700",
              body="no thread here", bridge=bridge)
    assert got.returncode == 0, got.stderr
    channel, thread, source = got.stdout.strip().split("\t")
    assert (channel, thread, source) == (DEV, "1794000100.000100", "pr-alert-thread")


def test_issue_body_beats_pr_alert_thread(tmp_path) -> None:
    bridge = make_bridge(tmp_path, [(DEV, "1794000100.000100", "HeyDonna Alerts", "PR #13700 CI green")])
    got = run(tmp_path, "issue-thread", "get", "--issue", "13671", "--pr", "13700",
              body=f"{DEV} thread 1793000000.000100", bridge=bridge)
    assert got.stdout.strip().split("\t") == [DEV, "1793000000.000100", "issue-body"]


def test_gh_failure_still_falls_through_to_pr_alert_thread(tmp_path) -> None:
    bridge = make_bridge(tmp_path, [(DEV, "1794000100.000100", "HeyDonna Alerts", "PR #13700 CI green")])
    got = run(tmp_path, "issue-thread", "get", "--issue", "13671", "--pr", "13700", body=None, bridge=bridge)
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip().split("\t")[2] == "pr-alert-thread"


def test_stored_customer_row_is_never_returned(tmp_path) -> None:
    db = tmp_path / "pm-ops.db"
    run(tmp_path, "init")
    con = sqlite3.connect(db)
    con.execute("INSERT INTO issue_threads VALUES (11, ?, '1794296562.075779', 'bad', 'now')", (CUSTOMER,))
    con.commit()
    con.close()
    got = run(tmp_path, "issue-thread", "get", "--issue", "11", "--no-resolve", body=None)
    assert got.returncode == 1 and got.stdout.strip() == "NONE"


def test_no_resolve_and_refresh_flags(tmp_path) -> None:
    body = f"{DEV} thread 1793944749.704259"
    first = run(tmp_path, "issue-thread", "get", "--issue", "20", "--no-resolve", body=body)
    assert first.returncode == 1 and stored_rows(tmp_path) == [], "--no-resolve must not resolve or persist"
    run(tmp_path, "issue-thread", "set", "--issue", "20", "--channel", DEV, "--thread", "1790000000.000100")
    stale = run(tmp_path, "issue-thread", "get", "--issue", "20", body=body)
    assert stale.stdout.strip().split("\t")[1] == "1790000000.000100", "stored row wins without --refresh"
    fresh = run(tmp_path, "issue-thread", "get", "--issue", "20", "--refresh", body=body)
    assert fresh.stdout.strip().split("\t")[1] == "1793944749.704259"
    assert stored_rows(tmp_path) == [(20, DEV, "1793944749.704259", "issue-body")]
