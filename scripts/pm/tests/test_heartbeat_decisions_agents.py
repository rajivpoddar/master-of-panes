"""Rajiv's hourly decisions + inactive-agent report; collectors are read-only."""

import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[1] / "shared-assets"
SOURCE = ASSETS / "claude/scripts/pm/heartbeat/heartbeat-compose.py"
SPEC = importlib.util.spec_from_file_location("heartbeat_decisions_agents", SOURCE)
composer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(composer)
NOW = datetime(2026, 10, 22, 4, tzinfo=timezone.utc)
HEADER = "*Decisions pending on Rajiv:*"


def iso(days=0):
    return (NOW - timedelta(days=days)).isoformat()


def row(n=1, **changes):
    return dict(id=n, status="open", kind="rajiv_decision", owner="rajiv", target_type="issue",
                target_id=str(10000 + n), issue=10000 + n, pr=None, title=f"Decision {n}",
                required_action=None, created_at=iso(2), updated_at=iso(), **changes)


def changed_row(n=1, **changes):
    result = row(n)
    result.update(changes)
    return result


def ledger(path, rows):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE obligations (id INTEGER PRIMARY KEY, status TEXT, kind TEXT, owner TEXT,"
                     " target_type TEXT, target_id TEXT, issue INTEGER, pr INTEGER, title TEXT,"
                     " required_action TEXT, created_at TEXT, updated_at TEXT)")
        for item in rows:
            keys = list(item)
            conn.execute(f"INSERT INTO obligations ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
                         [item[k] for k in keys])


def test_decision_ledger_exact_selection_freshness_and_no_writes(tmp_path):
    path = tmp_path / "pm-ops.db"
    rows = [changed_row(i, kind=kind, owner="pm") for i, kind in enumerate(
        ("rajiv_decision", "rajiv_decision_pending", "decision_pending", "product_decision"), 1)]
    rows += [changed_row(5, kind="credential_issue"),
             changed_row(6, updated_at=iso(7), created_at=iso(90)),  # inclusive freshness, not creation age
             changed_row(7, updated_at=iso(7.01), title="STALE_SECRET_TITLE"),
             changed_row(8, status="resolved", issue=10707, title="already decided"),
             changed_row(9, kind="followup", owner="pm"),
             changed_row(10, updated_at="invalid")]
    ledger(path, rows)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    doc = composer.read_decisions(path, NOW)
    assert {r["id"] for r in doc["items"]} == {1, 2, 3, 4, 5, 6}
    assert doc["stale"] == 2
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    text = "\n".join(composer.decision_block(doc, NOW))
    assert "10707" not in text and "STALE_SECRET_TITLE" not in text
    assert "age 90d" in text  # age is since creation; freshness uses updated_at


def test_decision_ledger_unavailable_does_not_create_a_database(tmp_path):
    path = tmp_path / "missing.db"
    assert composer.read_decisions(path, NOW) is None
    assert not path.exists()
    ledger(path, [])
    assert composer.read_decisions(path, NOW) == {"items": [], "stale": 0}


def test_decisions_cap_routes_age_and_stale_count():
    items = [changed_row(i, owner="rajiv" if i % 2 else "cto", title="a\n" + "long phrase " * 20)
             for i in range(1, 9)]
    lines = composer.decision_block({"items": items, "stale": 10}, NOW)
    assert lines[0] == HEADER
    assert len([line for line in lines if "age " in line]) == 6
    assert any("held by Rajiv" in line for line in lines)
    assert any("via CTO" in line for line in lines)
    assert "• +2 more" in lines and "• +10 stale (ledger triage)" in lines
    assert not any("\n" in line for line in lines)
    assert max(map(len, lines)) < 200
    assert composer.decision_block({"items": [], "stale": 0}, NOW) == [HEADER + " None."]
    assert composer.decision_block({"items": [], "stale": 2}, NOW) == [HEADER + " None.", "• +2 stale (ledger triage)"]
    assert composer.decision_block(None, NOW) == [HEADER + " unavailable"]


@pytest.mark.parametrize("record,minutes,state", [
    ({"type": "assistant", "message": {"stop_reason": "end_turn"}}, 100, "done"),
    ({"type": "assistant", "message": {"stop_reason": "tool_use"}}, 59, "active"),
    ({"type": "user", "message": {}}, 60, "inactive"),
    ({"type": "assistant", "message": {"stop_reason": None}}, 100, "inactive"),
    ({"type": "api-request"}, 1440, "inactive"),
    ({"type": "api-request"}, 1440.01, "expired"),
    (None, 100, "unknown"),
])
def test_agent_classifier_is_pure(record, minutes, state):
    assert composer.classify_agent(record, NOW.timestamp() - minutes * 60, NOW) == state


PROC = NOW.timestamp() - 30 * 3600  # PM claude process start


def pm_session(root, name="session", minutes=0, started_h=2):
    """started_h = hours before NOW that the session's first record was written."""
    path = root / f"{name}.jsonl"
    stamp0 = datetime.fromtimestamp(NOW.timestamp() - started_h * 3600, timezone.utc).isoformat()
    path.write_text(json.dumps({"type": "user", "timestamp": stamp0}) + "\n")
    stamp = NOW.timestamp() - minutes * 60
    os.utime(path, (stamp, stamp))
    return path


def transcript(root, name, minutes, record, meta=None, session="session"):
    if not (root / f"{session}.jsonl").exists():
        pm_session(root, session)
    path = root / session / "subagents" / f"agent-{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "user"}) + "\n" + json.dumps(record) + "\n\n")
    stamp = NOW.timestamp() - minutes * 60
    os.utime(path, (stamp, stamp))
    if meta is not None:
        path.with_suffix(".meta.json").write_text(json.dumps(meta))
    return path


def test_agent_collector_reads_tail_and_metadata_without_mutation(tmp_path):
    active = {"type": "assistant", "message": {"stop_reason": "tool_use"}}
    done = {"type": "assistant", "message": {"stop_reason": "end_turn", "content": "x" * 100000}}
    path = transcript(tmp_path, "idle", 85, active, {"description": "Review\nreport", "agentType": "reviewer"})
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    transcript(tmp_path, "active", 10, active)
    transcript(tmp_path, "done", 100, done)
    transcript(tmp_path, "expired", 1441, active)
    transcript(tmp_path, "fallback", 70, active, {"agentType": "specialist"})
    doc = composer.read_agents(tmp_path, NOW, PROC)
    assert doc["running"] == 1
    assert [r["description"] for r in doc["completed"]] == ["agent-done"]
    assert [r["description"] for r in doc["inactive"]] == ["Review\nreport", "specialist"]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert composer.agents_line(doc) == ("*Agents:* 4 listed · 1 running · 1 completed · 2 inactive — PM stops C+I in Step 3c: "
        "agent-done (completed, last write 100m ago), Review report (inactive 85m), specialist (inactive 70m)")
    plain = composer.agents_line({"running": 2, "inactive": [], "unreadable": 0})
    assert plain == "*Agents:* 2 listed · 2 running · 0 completed · 0 inactive — PM stops C+I in Step 3c"


def test_all_pm_pane_sessions_in_24h_count_including_pre_clear(tmp_path):
    active = {"type": "assistant", "message": {"stop_reason": "tool_use"}}
    end = {"type": "assistant", "message": {"stop_reason": "end_turn"}}
    # Session before a /clear: still in the pane, must be counted.
    transcript(tmp_path, "pre-clear-done", 300, end, {"description": "Pre clear done"}, session="old")
    transcript(tmp_path, "pre-clear-idle", 200, active, {"description": "Pre clear idle"}, session="old")
    pm_session(tmp_path, "old", minutes=600)
    # Session older than 24h is excluded, as is any agent older than 24h.
    transcript(tmp_path, "ancient", 1500, active, session="ancient")
    pm_session(tmp_path, "ancient", minutes=1500, started_h=40)  # prior claude process
    transcript(tmp_path, "current-working", 5, active, session="current")
    transcript(tmp_path, "current-idle", 60, active, {"description": "Current review"}, session="current")
    transcript(tmp_path, "current-too-old", 1441, active, session="current")
    transcript(tmp_path, "current-done", 1, end, {"description": "Current done"}, session="current")
    transcript(tmp_path, "current-unreadable", 0, {}, session="current").write_text("broken json\n")
    pm_session(tmp_path, "current", minutes=2)
    doc = composer.read_agents(tmp_path, NOW, PROC)
    assert doc["running"] == 1
    assert sorted(r["description"] for r in doc["completed"]) == ["Current done", "Pre clear done"]
    assert sorted(r["description"] for r in doc["inactive"]) == ["Current review", "Pre clear idle"]
    assert doc["unreadable"] == 1
    line = composer.agents_line(doc)
    assert line.startswith("*Agents:* 5 listed · 1 running · 2 completed · 2 inactive — PM stops C+I in Step 3c: ")
    assert "ancient" not in line


def test_never_says_none_inactive_while_completed_agents_exist_and_caps_descriptions():
    doc = {"running": 0, "inactive": [], "completed": [{"description": f"c{i}", "idle_min": 5 + i} for i in range(6)]}
    line = composer.agents_line(doc)
    assert "none inactive" not in line and "6 completed" in line
    assert "c3" in line and "c4" not in line and "+2 more" in line


@pytest.mark.parametrize("minutes,available", [(15, True), (15.01, False)])
def test_parent_session_freshness_is_fifteen_minutes_inclusive(tmp_path, minutes, available):
    transcript(tmp_path, "recent-child", 1, {"type": "user"})
    pm_session(tmp_path, minutes=minutes)
    doc = composer.read_agents(tmp_path, NOW, PROC)
    if available:
        assert doc["running"] == 1
    else:
        assert doc is None  # a recently written child cannot revive a dead parent


def test_session_selection_requires_top_level_jsonl_not_directory_or_child_mtime(tmp_path):
    transcript(tmp_path, "old-child", 1, {"type": "user"}, session="old")
    pm_session(tmp_path, "old", minutes=5)
    missing_parent = transcript(tmp_path, "orphan-child", 0, {"type": "user"}, session="orphan")
    # Move the fixture's parent record out of the top-level *.jsonl namespace.
    (tmp_path / "orphan.jsonl").rename(tmp_path / "orphan.session-record")
    pm_session(tmp_path, "newest", minutes=1)  # no subagent directory: no fallback to old
    doc = composer.read_agents(tmp_path, NOW, PROC)
    # Every top-level session in 24h counts (old-child is a live pane session); the orphan has no session record.
    assert doc == {"running": 1, "completed": [], "inactive": [], "unreadable": 0}
    assert missing_parent.exists()


def test_no_top_level_session_record_is_unavailable(tmp_path):
    transcript(tmp_path, "orphan", 1, {"type": "user"})
    (tmp_path / "session.jsonl").rename(tmp_path / "session.other")
    assert composer.read_agents(tmp_path, NOW, PROC) is None


def test_agent_line_caps_names_and_reports_unreadable_inputs(tmp_path):
    doc = {"running": 1, "inactive": [{"description": f"agent{i}", "idle_min": 80} for i in range(7)]}
    line = composer.agents_line(doc)
    assert "8 listed · 1 running · 0 completed · 7 inactive" in line and "agent3" in line and "agent4" not in line and "+3 more" in line
    transcript(tmp_path, "bad", 80, {}).write_text("broken json\n")
    bad = tmp_path / "session/subagents/agent-bad.jsonl"
    os.utime(bad, (NOW.timestamp() - 4800,) * 2)
    doc = composer.read_agents(tmp_path, NOW, PROC)
    assert doc["unreadable"] == 1 and doc["running"] == 0
    assert "1 unreadable" in composer.agents_line(doc)
    assert composer.read_agents(tmp_path / "absent", NOW, PROC) is None
    assert composer.agents_line(None) == "*Agents:* unavailable"


@pytest.mark.parametrize("mode", ["1h", "3h"])
def test_shared_composer_orders_agents_and_exactly_one_decision_spacer(mode):
    doc = {"items": [row()], "stale": 2}
    out = composer.compose(mode, NOW, {"events": 0}, {}, [], include_slots=True, slots={"slots": []},
                           decisions=doc, agents={"running": 1, "inactive": []},
                           training=("*Training:* no run active", []))  # hermetic: never probe the live Spark
    lines = out.splitlines()
    i = lines.index(HEADER)
    assert lines[i - 1] == "" and lines[i - 2] != ""
    assert "\n\n\n" + HEADER not in out
    if mode == "1h":
        # Training line (Rajiv 1792694694.833149) follows Agents, directly before the decisions spacer.
        assert lines[i - 2] == "*Training:* no run active"
        assert lines[i - 3] == "*Agents:* 1 listed · 1 running · 0 completed · 0 inactive — PM stops C+I in Step 3c"
        assert out.index("*Slots") < out.index("*Agents") < out.index(HEADER)
        assert not out.startswith(composer.CTO)
    else:
        assert "*Agents:*" not in out and out.startswith(composer.CTO)


def test_cli_collects_live_readonly_inputs_and_saves_them(tmp_path, monkeypatch, capsys):
    quiet = tmp_path / "quiet.json"
    quiet.write_text('{"events":0}')
    prs = tmp_path / "prs.json"
    prs.write_text("[]")
    decisions = {"items": [row()], "stale": 2}
    agents = {"running": 4, "inactive": []}
    monkeypatch.setattr(composer, "read_decisions", lambda *a: decisions)
    monkeypatch.setattr(composer, "read_agents", lambda *a: agents)
    monkeypatch.setattr(composer, "cleanup_status_alert", lambda: None)
    saved = tmp_path / "saved"
    monkeypatch.setattr(sys, "argv", [str(SOURCE), "--mode", "1h", "--axiom-json", str(quiet),
                                    "--map-json", str(quiet), "--prs-json", str(prs),
                                    "--no-slots", "--no-active-users", "--save-inputs", str(saved)])
    assert composer.main() == 0
    out = capsys.readouterr().out
    assert HEADER in out and "*Agents:* 4 listed · 4 running · 0 completed · 0 inactive" in out
    assert json.loads((saved / "decisions.json").read_text()) == decisions
    assert json.loads((saved / "agents.json").read_text()) == agents


def test_deployed_skill_contract_is_shipped_in_manifest():
    skill = ASSETS / "claude/skills/hourly-heartbeat/SKILL.md"
    text = skill.read_text()
    step2 = text.split("STEP 2 — check:", 1)[1].split("STEP 3", 1)[0]
    assert HEADER in step2 and "blank spacer" in step2 and "*Agents:*" in step2
    assert "read-only" in step2.lower() and "ledger triage" in step2
    # Rajiv 2026-10-23 (C0ALZJHGE49 ts 1792770807.935489): count every subagent of the PM pane's
    # sessions in the last 24h, including sessions before a /clear.
    assert "last 24h" in step2 and "/clear" in step2 and "completed" in step2
    assert "none inactive" in step2 and "Never print" in step2
    manifest = json.loads((ASSETS / "manifest.json").read_text())
    for path in (SOURCE, skill):
        entries = [e for e in manifest["entries"] if e["source_path"] == str(path.relative_to(ASSETS))]
        assert len(entries) == 1
        assert entries[0]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_only_current_process_direct_uncollected_agents_count(tmp_path):
    active = {"type": "assistant", "message": {"stop_reason": "tool_use"}}
    end = {"type": "assistant", "message": {"stop_reason": "end_turn"}}
    # Old-process session (started before the PM claude process): excluded entirely, even if written recently.
    transcript(tmp_path, "old-proc-done", 5, end, {"description": "OLD PROCESS"}, session="oldproc")
    transcript(tmp_path, "old-proc-live", 5, active, {"description": "OLD PROCESS LIVE"}, session="oldproc")
    pm_session(tmp_path, "oldproc", minutes=5, started_h=40)
    # Current process: a /clear chain of two sessions.
    transcript(tmp_path, "cur-done", 20, end, {"description": "Current completed", "spawnDepth": 1}, session="cur1")
    transcript(tmp_path, "cur-run", 3, active, {"description": "Current running", "spawnDepth": 1}, session="cur2")
    # Nested sub-subagent (spawnDepth 2) shares the dir but is not listed by the pane.
    transcript(tmp_path, "nested", 3, active, {"description": "NESTED", "spawnDepth": 2}, session="cur2")
    transcript(tmp_path, "nested-done", 3, end, {"description": "NESTED DONE", "spawnDepth": 2}, session="cur2")
    # Result already delivered (meta.finished) => the pane dropped it.
    transcript(tmp_path, "collected", 10, end, {"description": "COLLECTED", "finished": True, "spawnDepth": 1}, session="cur2")
    # Finished before this process started (stale transcript in a current-process session) => not counted.
    stale = transcript(tmp_path, "pre-proc-done", 31 * 60, end, {"description": "PRE PROC"}, session="cur1")
    pm_session(tmp_path, "cur1", minutes=30, started_h=20)
    pm_session(tmp_path, "cur2", minutes=2, started_h=1)
    doc = composer.read_agents(tmp_path, NOW, PROC)
    assert doc["running"] == 1
    assert [r["description"] for r in doc["completed"]] == ["Current completed"]
    assert doc["inactive"] == []
    line = composer.agents_line(doc)
    assert line.startswith("*Agents:* 2 listed · 1 running · 1 completed · 0 inactive")
    for banned in ("OLD PROCESS", "NESTED", "COLLECTED", "PRE PROC"):
        assert banned not in line


def test_unknown_pm_process_start_is_unavailable_not_a_guess(tmp_path, monkeypatch):
    transcript(tmp_path, "a", 1, {"type": "user"})
    monkeypatch.setattr(composer, "pm_process_start", lambda *a, **k: None)
    assert composer.read_agents(tmp_path, NOW) is None
