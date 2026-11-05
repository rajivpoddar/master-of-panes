"""Observed 03:16 report/refusal, real claim reader -> snapshot -> composer.

All native writer work uses an isolated fixture ledger. No collectors or sends.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts/pm/shared-assets/claude/scripts"
FIXTURE = json.loads((Path(__file__).parent / "fixtures/heartbeat-report-0316.json").read_text())


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / "pm/heartbeat" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def lane(tmp_path, monkeypatch):
    db = tmp_path / "pm-ops.db"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PM_OPS_DB", str(db))
    subprocess.run([sys.executable, str(SCRIPTS / "pm-ops-legacy.py"), "init"], check=True, capture_output=True)
    snapshot, composer = load("pr-open-snapshot"), load("heartbeat-compose")
    return db, snapshot, composer


def pr(number):
    head = FIXTURE["pr_heads"][str(number)]
    return {"number": number, "headRefOid": head, "isDraft": False, "title": "Observed red",
            "labels": [{"name": "ci-head:" + head}], "statusCheckRollup": [
                {"name": "test", "conclusion": "FAILURE", "status": "COMPLETED", "workflowName": "CI"},
                {"name": "e2e", "conclusion": "SUCCESS", "status": "COMPLETED", "workflowName": "E2E Smoke Tests"}]}


def claim(lane, number=12858, *, head=None, expired=False, head_missing=False):
    evidence = {} if head_missing else {"head": head or pr(number)["headRefOid"]}
    until = datetime.now(timezone.utc) + timedelta(minutes=-5 if expired else 30)
    result = subprocess.run([sys.executable, str(SCRIPTS / "pm-ops-legacy.py"), "obligation-upsert",
                             "--kind", "pr_red_unowned", "--pr", str(number), "--owner", "ci-repair-agent",
                             "--suppress-until", until.isoformat(), "--evidence-json", json.dumps(evidence),
                             "--action", "Consume the existing CI repair terminal.", "--print-id"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return int(result.stdout.strip())


def snapshot_rows(lane, monkeypatch, capsys, prs):
    _, snapshot, _ = lane
    monkeypatch.setattr(snapshot, "fetch_open_prs", lambda: prs)
    monkeypatch.setattr(snapshot, "fetch_mop_owned_prs", lambda: set())
    monkeypatch.setattr(sys, "argv", ["pr-open-snapshot.py"])
    assert snapshot.main() == 0
    return json.loads(capsys.readouterr().out)


def report(composer, rows):
    now = datetime(2026, 11, 5, 21, 46, tzinfo=timezone.utc)
    slots = {"slots": [{"slot": n, "occupied": True, "issue": issue, "assigned_at": "2026-11-05T20:00:00Z",
                         "last_activity": now.isoformat(), "active_turn_state": "active", "status": "working"}
                        for n, issue in enumerate([12674, 12034, 11995, 12791], 1)]}
    return composer.compose("1h", now, {"events": 4, "users": 1, "top": []}, {}, rows,
                            slots=slots, include_slots=True, agents={"running": 3, "completed": [], "inactive": []},
                            training=("*Training:* retained metric", []),
                            pending_actions=({"backlog": 0, "items": []}, {"items": []}))


def test_same_head_live_canonical_claim_and_true_unowned_remain_distinct(lane, monkeypatch, capsys):
    db, _, composer = lane
    row_id = claim(lane)
    before = db.read_bytes()
    rows = snapshot_rows(lane, monkeypatch, capsys, [pr(12858), pr(12822)])
    owned = next(r for r in rows if r["number"] == 12858)
    unowned = next(r for r in rows if r["number"] == 12822)
    assert owned["state"].startswith("red: test") and "unowned" not in owned["state"]
    assert "owner: ci-repair-agent" in owned["owner"] and not owned["actions"]
    assert owned["next"] == "await existing owner terminal"
    assert "unowned" in unowned["state"] and "ci-repair-agent" in unowned["actions"][0]
    assert "preserve existing incident ownership" not in unowned["actions"][0]
    text = report(composer, rows)
    assert "2 red (1 unowned)" in text and "owner: ci-repair-agent" in text
    assert "*Agents:* 3 listed · 3 running · 0 completed · 0 inactive" in text
    assert "*Slots (4):*" in text and "12791" in text and "4 errors / 1 users" in text
    assert "PM stops" not in text and "Step 3c" not in text
    assert db.read_bytes() == before
    with closing(sqlite3.connect(db)) as con:
        assert con.execute("SELECT owner FROM obligations WHERE id=?", (row_id,)).fetchone()[0] == "ci-repair-agent"


@pytest.mark.parametrize("mode", ["wrong_head", "missing_head", "expired_claim"])
def test_stale_or_unbound_owner_is_unknown_not_known_live(lane, monkeypatch, capsys, mode):
    claim(lane, head="f" * 40 if mode == "wrong_head" else None,
          expired=mode == "expired_claim", head_missing=mode == "missing_head")
    rows = snapshot_rows(lane, monkeypatch, capsys, [pr(12858)])
    row = rows[0]
    assert row["state"] == "red: test, ownership unknown"
    assert row["owner"] == "ownership unknown"
    assert row["next"] == "reconcile ownership evidence"
    assert "reconcile ownership evidence before dispatch" in row["actions"][0]
    assert "owner: ci-repair-agent" not in report(lane[2], rows)


def test_unavailable_ownership_evidence_is_not_reported_unowned(lane, monkeypatch, capsys):
    lane[1].PM_OPS_DB = lane[0].parent / "missing.db"
    rows = snapshot_rows(lane, monkeypatch, capsys, [pr(12858)])
    assert rows[0]["state"] == "red: test, ownership unknown"
    assert not lane[1].PM_OPS_DB.exists()


def test_observed_refusal_and_metrics_only_agent_line(lane):
    composer = lane[2]
    assert "housekeeping instruction" in FIXTURE["native_refusal"]
    assert "PM stops C+I+stale in Step 3c" in FIXTURE["observed_agent_line"]
    doc = {"running": 3, "completed": [{"description": "finished", "idle_min": 10}],
           "inactive": [{"description": "waiting", "idle_min": 80}]}
    line = composer.agents_line(doc)
    assert "5 listed · 3 running · 1 completed · 1 inactive" in line
    assert "finished (completed" in line and "waiting (inactive" in line
    assert "PM stops" not in line and "Step 3c" not in line
