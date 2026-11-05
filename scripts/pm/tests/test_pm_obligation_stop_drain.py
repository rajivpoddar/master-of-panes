"""Real Stop shell + group runner, isolated SQLite and unrelated-hook stubs.

Never invokes a live hook, MoP, GitHub, a collector, or a publisher.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[3]
HOOKS = ROOT / "scripts/pm/shared-assets/claude/hooks"
WRITER = ROOT / "scripts/pm/shared-assets/claude/scripts/pm-ops-legacy.py"
FUTURE = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()


@pytest.fixture
def lane(tmp_path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name in ("pm-ops-sync-stop-validator.sh", "pm-stop-group-runner.py"):
        shutil.copy2(HOOKS / name, hooks / name)
    for name in ("ops-audit-stop-validator.sh", "slack-reply-due.sh", "idle-slot-due.sh"):
        (hooks / name).write_text("#!/bin/bash\ncat >/dev/null\nexit 0\n")
    db = tmp_path / "ledger.db"
    env = {**os.environ, "HOME": str(tmp_path), "PM_OPS_DB": str(db),
           "PM_STOP_HOOK_ROOT": str(hooks),
           "PM_STOP_GROUP_LOG": str(tmp_path / "group.log"),
           "PM_STOP_GROUP_DEBUG_LOG": str(tmp_path / "debug.log"),
           "PM_STOP_FINDING_COOLDOWN_STATE": str(tmp_path / "cooldown.json")}
    env.pop("PM_STOP_GROUP_COMMANDS_JSON", None)
    subprocess.run([sys.executable, str(WRITER), "init"], env=env, check=True, capture_output=True)
    # The OLD hook's status dependency is isolated too, for causal baseline.
    status = tmp_path / "status.py"
    status.write_text("#!/usr/bin/env python3\nimport json,os\nprint(json.dumps({'db':os.environ['PM_OPS_DB']}))\n")
    status.chmod(0o755)
    env["PM_OPS"] = str(status)
    return hooks, db, env, tmp_path


def add(lane, ident, **extra):
    _, db, _, _ = lane
    row = dict(id=ident, status="open", kind="followup", severity="normal", horizon="daily",
               title=f"Commitment {ident}", owner="pm", required_action="Inspect retained source and return the result.",
               created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
               target_type="task", target_id=str(ident), evidence_json="{}")
    if "evidence_json" in extra and isinstance(extra["evidence_json"], dict):
        extra["evidence_json"] = json.dumps(extra["evidence_json"])
    row.update(extra)
    # A transaction context commits but does not close the connection. Close
    # fixture writers before byte snapshots so later GC cannot checkpoint WAL.
    with closing(sqlite3.connect(db)) as con, con:
        con.execute(f"INSERT INTO obligations ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", list(row.values()))


def invoke(lane, *, grouped=True, payload=None, env=None):
    hooks, _, base_env, _ = lane
    command = ([sys.executable, str(hooks / "pm-stop-group-runner.py"), "obligations"] if grouped
               else ["bash", str(hooks / "pm-ops-sync-stop-validator.sh")])
    start = time.monotonic()
    result = subprocess.run(command, env=env or base_env,
                            input=json.dumps(payload or {"session_id": "real-pm-A"}),
                            text=True, capture_output=True, timeout=9.8)
    assert result.returncode == 0, result.stderr
    parsed = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert parsed, "A truthful status/decision must be visible"
    return parsed[-1], time.monotonic() - start


def reason(result):
    return result.get("message") or result.get("reason", "")


def resolve(lane, ident):
    _, db, env, _ = lane
    with closing(sqlite3.connect(db)) as con, con:
        updated, evidence, kind = con.execute("SELECT updated_at,evidence_json,kind FROM obligations WHERE id=?", (ident,)).fetchone()
    result = subprocess.run([sys.executable, str(WRITER), "obligation-resolve", "--kind", kind,
                             "--id", str(ident), "--if-unclaimed", "--expect-updated-at", updated,
                             "--expect-evidence-sha256", hashlib.sha256(evidence.encode()).hexdigest(),
                             "--reason", "fixture action completed"], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr + result.stdout
    with closing(sqlite3.connect(db)) as con, con:
        assert con.execute("SELECT status FROM obligations WHERE id=?", (ident,)).fetchone()[0] == "resolved"


@pytest.mark.parametrize("grouped", [False, True])
def test_normal_daily_and_unknown_high_kind_are_actions(lane, grouped):
    add(lane, 301)
    add(lane, 302, kind="new_deliberate_commitment", severity="high", horizon="hourly")
    result, elapsed = invoke(lane, grouped=grouped)
    assert result.get("decision") == "block"
    assert "obligation:301" in reason(result) and "obligation:302" in reason(result)
    assert elapsed < 1, elapsed


def test_one_of_two_and_unrelated_resolution_cannot_waive(lane):
    add(lane, 301)
    add(lane, 302)
    add(lane, 303, status="resolved", resolved_at=datetime.now(timezone.utc).isoformat())
    transcript = lane[3] / "transcript.jsonl"
    transcript.write_text(json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:00Z",
                                      "message": {"content": "do the work"}}) + "\n")
    payload = {"session_id": "real-pm-A", "transcript_path": str(transcript)}
    assert invoke(lane, payload=payload)[0].get("decision") == "block"
    resolve(lane, 301)
    result, _ = invoke(lane, payload=payload)
    assert result.get("decision") == "block" and "obligation:302" in reason(result)
    resolve(lane, 302)
    assert invoke(lane, payload=payload)[0].get("decision") != "block"


def test_cooldown_does_not_waive_drain(lane):
    add(lane, 301)
    for _ in range(3):
        assert invoke(lane)[0].get("decision") == "block"


def test_long_turn_still_drains_without_changing_other_bypass(lane):
    add(lane, 301)
    transcript = lane[3] / "long.jsonl"
    transcript.write_text(json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:00Z",
                                      "message": {"content": "real prompt"}}) + "\n")
    (lane[0] / "ops-audit-stop-validator.sh").write_text('echo \'{"decision":"block","message":"UNRELATED"}\'\n')
    result, _ = invoke(lane, payload={"session_id": "real-pm-A", "transcript_path": str(transcript)})
    assert result.get("decision") == "block" and "PM_OBLIGATION_DRAIN" in reason(result)
    assert "UNRELATED" not in reason(result)


def test_waits_visible_and_last_runnable_completion_allows_stop(lane):
    add(lane, 301)
    add(lane, 302, next_review_at=FUTURE, blocker="Review vendor terminal at the agreed review window.",
        evidence_json={"review_reason": "agreed vendor review window", "source": "retained-thread:123"})
    add(lane, 303, blocker="Waiting for Rajiv approval; no new execution authority.")
    add(lane, 304, evidence_json={"handoff": {"status": "accepted", "queuedSubmissionId": "accepted-1",
                                                "obligation_id": 304, "target_id": "304"}})
    before = lane[1].read_bytes()
    result, _ = invoke(lane)
    assert result.get("decision") == "block"
    assert "future=1" in reason(result) and "held=1" in reason(result) and "in_flight=1" in reason(result)
    assert lane[1].read_bytes() == before
    resolve(lane, 301)
    result, _ = invoke(lane)
    assert result.get("decision") != "block" and "open commitments=3" in reason(result)
    with closing(sqlite3.connect(lane[1])) as con, con:
        assert con.execute("SELECT count(*) FROM obligations WHERE status='open'").fetchone()[0] == 3


@pytest.mark.parametrize("extra", [
    {"owner": "pm -> assigned-worker", "suppress_until": FUTURE},
    {"evidence_json": {"handoff": {"status": "accepted"}}},
    {"evidence_json": {"handoff": {"status": "running", "run_id": "one", "obligation_id": 999}}},
])
def test_claim_wake_or_wrong_unaccepted_receipt_does_not_handle(lane, extra):
    add(lane, 301, **extra)
    assert invoke(lane)[0].get("decision") == "block"


def native_queue_receipt(**extra):
    return {"status": "queued_for_task_consumption", "queueAccepted": True,
            "executionOwnership": "queued", "queuedSubmissionId": "native-submission-A",
            "clientUserMessageId": "native-client-A", "threadId": "existing-owner-A", **extra}


@pytest.mark.parametrize("binding", ["item", "execution"])
def test_actual_native_queue_success_waits_without_redispatch(lane, binding):
    receipt = native_queue_receipt(**({"obligation_id": 301} if binding == "item" else {}))
    evidence = {"receipt": receipt}
    if binding == "execution":
        evidence["execution"] = {k: receipt[k] for k in ("threadId", "clientUserMessageId", "queuedSubmissionId")}
        evidence["execution"]["obligation_id"] = 301
    add(lane, 301, required_action="Dispatch another worker for this item.", evidence_json=evidence)
    add(lane, 302)
    before = lane[1].read_bytes()
    result, _ = invoke(lane)
    assert result.get("decision") == "block" and "in_flight=1" in reason(result)
    assert "obligation:302 [runnable]" in reason(result)
    assert "Dispatch another worker" not in reason(result)
    assert lane[1].read_bytes() == before
    resolve(lane, 302)
    result, _ = invoke(lane)
    assert result.get("decision") != "block" and "in_flight=1" in reason(result)
    with closing(sqlite3.connect(lane[1])) as con, con:
        assert con.execute("SELECT status FROM obligations WHERE id=301").fetchone()[0] == "open"


@pytest.mark.parametrize("mode", ["copied_item", "missing_binding", "wrong_native_execution", "generic_unbound"])
def test_foreign_or_unbound_acceptance_requires_safe_reconciliation(lane, mode):
    receipt = native_queue_receipt()
    evidence = {"receipt": receipt}
    if mode == "copied_item":
        receipt["obligation_id"] = 999
    elif mode == "wrong_native_execution":
        receipt["obligation_id"] = 301
        evidence["execution"] = {"obligation_id": 301, "threadId": "another-owner-B",
                                 "clientUserMessageId": "another-client-B", "queuedSubmissionId": "another-submission-B"}
    elif mode == "generic_unbound":
        evidence = {"receipt": {"status": "running", "run_id": "unrelated-run"}}
    add(lane, 301, required_action="Dispatch another worker for this item.", evidence_json=evidence)
    result, _ = invoke(lane)
    assert result.get("decision") == "block" and "obligation:301 [classify]" in reason(result)
    assert "in_flight=" not in reason(result)
    assert "Do not redispatch or replay" in reason(result)
    assert "Dispatch another worker" not in reason(result)


@pytest.mark.parametrize("wait_kind", ["authority", "unavailable", "future"])
def test_unbound_receipt_reconciliation_converges_to_documented_wait(lane, wait_kind):
    receipt = {"status": "running", "run_id": "unrelated-run"}
    evidence = {"receipt": receipt}
    action = "Dispatch another worker for this item."
    add(lane, 301, required_action=action, evidence_json=evidence)
    add(lane, 302)
    result, _ = invoke(lane)
    assert result.get("decision") == "block" and "obligation:301 [classify]" in reason(result)
    assert "Do not redispatch or replay" in reason(result)
    blocker, disposition = {
        "authority": ("Waiting for Rajiv approval in the original thread.", "held"),
        "unavailable": ("Required correspondence evidence unavailable; retain the original receipt.", "unverified"),
        "future": ("Review remains scheduled at the agreed evidence window.", "future"),
    }[wait_kind]
    args = [sys.executable, str(WRITER), "obligation-upsert", "--kind", "followup",
            "--target-type", "task", "--target-id", "301", "--severity", "normal",
            "--horizon", "daily", "--action", action, "--blocker", blocker, "--print-id"]
    if wait_kind == "future":
        evidence["review_reason"] = "Agreed review window for the unavailable correspondence evidence."
        args += ["--next-review-at", FUTURE]
    args += ["--evidence-json", json.dumps(evidence)]
    recorded = subprocess.run(args, env=lane[2], capture_output=True, text=True)
    assert recorded.returncode == 0 and recorded.stdout.strip() == "301", recorded.stdout + recorded.stderr
    result, _ = invoke(lane)
    assert result.get("decision") == "block" and "obligation:302 [runnable]" in reason(result)
    assert f"obligation:301 [{disposition}]" in reason(result)
    assert "obligation:301 [classify]" not in reason(result)
    assert action not in reason(result)
    resolve(lane, 302)
    result, _ = invoke(lane)
    assert result.get("decision") != "block" and f"obligation:301 [{disposition}]" in reason(result)
    with closing(sqlite3.connect(lane[1])) as con, con:
        status, retained = con.execute("SELECT status,evidence_json FROM obligations WHERE id=301").fetchone()
    assert status == "open" and json.loads(retained)["receipt"] == receipt


def test_inventory_excluded_without_hiding_waits(lane):
    add(lane, 301, kind="ready_pool")
    add(lane, 302, kind="triage_retained_scope")
    add(lane, 303, blocker="Waiting for customer reply in the original thread.")
    result, _ = invoke(lane)
    assert result.get("decision") != "block"
    assert "open commitments=1" in reason(result) and "obligation:303" in reason(result)


@pytest.mark.parametrize("custody", ["UNKNOWN", "UNKNOWN_CONSUMED_NO_RETRY", "uncertain/consumed/no-retry"])
def test_protected_effect_is_not_resend_instruction(lane, custody):
    add(lane, 301, required_action="Resend notification and charge again.", evidence_json={"outcome": custody})
    result, _ = invoke(lane)
    assert result.get("decision") != "block"
    assert "Protected effect custody" in reason(result) and "Resend notification" not in reason(result)


def test_uncertain_nested_receipt_and_legacy_clear_not_replayed(lane):
    add(lane, 301, evidence_json={"receipt": {"state": "unknown"}}, required_action="Resend")
    add(lane, 302, kind="pm-self-clear", required_action="/clear")
    assert invoke(lane)[0].get("decision") != "block"


def test_unestablished_prerequisite_is_safe_classification(lane):
    add(lane, 301, required_action="After PR merge and deploy, tell the customer it is live.")
    result, _ = invoke(lane)
    assert result.get("decision") == "block"
    assert "[classify]" in reason(result) and "Do not perform the conditional effect" in reason(result)
    assert "tell the customer it is live" not in reason(result)


def test_later_merge_or_review_clause_does_not_hide_current_next_step(lane):
    add(lane, 301, required_action="Next: drive the existing PR and route its remaining child. After both land, record final proof.",
        blocker="Remaining child and final proof are incomplete.")
    add(lane, 302, required_action="Add the already-authorized local report correction now. Review the budget on the agreed future date.",
        blocker="Local report correction remains unfinished.", next_review_at=FUTURE)
    result, _ = invoke(lane)
    assert result.get("decision") == "block"
    assert "obligation:301 [classify]" in reason(result) and "obligation:302 [classify]" in reason(result)
    assert "held=" not in reason(result) and "future=" not in reason(result)


def test_unavailable_row_does_not_hide_unrelated_local_work(lane):
    add(lane, 301, blocker="MoP evidence unavailable; retain current owner.")
    add(lane, 302)
    result, _ = invoke(lane)
    assert result.get("decision") == "block" and "unverified=1" in reason(result)
    resolve(lane, 302)
    assert invoke(lane)[0].get("decision") != "block"


@pytest.mark.parametrize("mode", ["missing", "malformed_db", "malformed_schema", "malformed_row", "locked"])
def test_read_failure_or_malformed_state_is_visible_fail_open(lane, mode):
    db = lane[1]
    lock = None
    if mode == "missing":
        db.unlink()
    elif mode == "malformed_db":
        db.write_bytes(b"not a sqlite database")
    elif mode == "malformed_schema":
        with closing(sqlite3.connect(db)) as con, con:
            con.execute("DROP TABLE obligations")
    elif mode == "malformed_row":
        add(lane, 301, evidence_json="not JSON")
    else:
        lock = sqlite3.connect(db)
        lock.execute("PRAGMA journal_mode=DELETE")
        lock.execute("BEGIN EXCLUSIVE")
    try:
        result, elapsed = invoke(lane)
        assert result.get("decision") != "block"
        assert "UNKNOWN" in reason(result) or "unverified=1" in reason(result)
        assert elapsed < 1, elapsed
    finally:
        if lock:
            lock.rollback()
            lock.close()


@pytest.mark.parametrize("command", [
    "echo '{\"decision\":\"block\",\"message\":\"PARTIAL\"}'; exec sleep 3",
    "echo '{\"decision\":\"block\",\"message\":\"PARTIAL\"}'; exit 2",
])
def test_child_timeout_or_failure_cannot_be_generic_hard_block(lane, command):
    (lane[0] / "pm-ops-sync-stop-validator.sh").write_text(command + "\n")
    result, elapsed = invoke(lane)
    assert result.get("decision") != "block" and "PM_OBLIGATION_DRAIN_UNAVAILABLE" in reason(result)
    assert "PARTIAL" not in reason(result) and "PM_STOP_CHILD" not in reason(result)
    assert elapsed < 3, elapsed


def test_other_validator_failure_policy_retained(lane):
    (lane[0] / "ops-audit-stop-validator.sh").write_text("exit 2\n")
    result, _ = invoke(lane)
    assert result.get("decision") == "block" and "PM_STOP_CHILD_FAILED" in reason(result)


def test_no_network_status_collector_or_hook_registration_change(lane):
    marker = lane[3] / "forbidden"
    trap = lane[3] / "trap.py"
    trap.write_text(f"#!/usr/bin/env python3\nopen({str(marker)!r},'w').write('called')\nraise SystemExit(99)\n")
    trap.chmod(0o755)
    env = {**lane[2], "PM_OPS": str(trap), "PR_OPEN_SNAPSHOT": str(trap), "MOP_URL": "http://127.0.0.1:1"}
    add(lane, 301)
    assert invoke(lane, env=env)[0].get("decision") == "block"
    assert not marker.exists()
    spec = importlib.util.spec_from_file_location("isolated_stop_runner", HOOKS / "pm-stop-group-runner.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    children = module.default_groups(lane[0])["obligations"]
    assert max(c.timeout for c in children) < 10
    assert next(c.timeout for c in children if c.name == "pm-ops-sync-stop-validator") <= 2
