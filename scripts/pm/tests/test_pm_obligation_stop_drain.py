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
    # Default severity "high" is a FAST row, so the pre-existing drain tests still prove the unchanged
    # block-every-Stop contract. HOURLY behaviour is tested with explicit severity="normal" rows below.
    row = dict(id=ident, status="open", kind="followup", severity="high", horizon="daily",
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
def test_fast_daily_and_unknown_high_kind_are_actions(lane, grouped):
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
    {"evidence_json": {"handoff": {"status": "accepted"}}},
    {"evidence_json": {"handoff": {"status": "running", "run_id": "one", "obligation_id": 999}}},
])
def test_claim_wake_or_wrong_unaccepted_receipt_does_not_handle(lane, extra):
    add(lane, 301, **extra)
    assert invoke(lane)[0].get("decision") == "block"


def test_future_suppress_until_defers_explicitly_postponed_row(lane):
    """Rajiv C0ALZJHGE49 thread 1793935184.677639, ts 1793935184.677639 and 1793935270.961189: postpone so Stop does not flag it."""
    add(lane, 301, owner="pm -> assigned-worker", suppress_until=FUTURE)
    result, _ = invoke(lane)
    assert result.get("decision") != "block"
    assert "obligation:301 [future]" in reason(result)


def test_postpone_list_and_clear_blocker(lane):
    env = lane[2]
    base = [sys.executable, str(WRITER), "obligation-upsert", "--kind", "followup",
            "--target-type", "task", "--target-id", "postpone", "--action", "Do work",
            "--severity", "high"]
    subprocess.run(base + ["--blocker", "blocked", "--suppress-until", FUTURE],
                   env=env, check=True, capture_output=True)
    def rows():
        result = subprocess.run([sys.executable, str(WRITER), "obligation-list", "--actionable", "--format", "json"],
                                env=env, check=True, capture_output=True, text=True)
        return json.loads(result.stdout)["rows"]
    assert rows() == []
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    subprocess.run(base + ["--blocker", "", "--suppress-until", past, "--next-review-at", FUTURE],
                   env=env, check=True, capture_output=True)
    with closing(sqlite3.connect(lane[1])) as con:
        assert con.execute("SELECT blocker FROM obligations WHERE target_id='postpone'").fetchone()[0] is None
    assert len(rows()) == 1
    result, _ = invoke(lane)
    assert result.get("decision") == "block" and "[runnable]" in reason(result)


@pytest.mark.parametrize("evidence", [
    {"outcome": "UNKNOWN"},
    {"handoff": {"status": "accepted", "queuedSubmissionId": "accepted-1",
                 "obligation_id": 301, "target_id": "301"}},
])
def test_metadata_upsert_preserves_custody_and_scheduling(lane, evidence):
    add(lane, 301, severity="high", horizon="daily", owner="worker-claim",
        blocker="waiting", next_review_at=FUTURE, evidence_json=evidence,
        required_action="Resend notification")
    add(lane, 302)
    def read():
        with closing(sqlite3.connect(lane[1])) as con:
            con.row_factory = sqlite3.Row
            return dict(con.execute("SELECT * FROM obligations WHERE id=301").fetchone())
    before = read()
    base = [sys.executable, str(WRITER), "obligation-upsert", "--kind", "followup",
            "--target-type", "task", "--target-id", "301"]
    subprocess.run(base + ["--suppress-until", FUTURE, "--blocker", ""],
                   env=lane[2], check=True, capture_output=True)
    after = read()
    assert {k: v for k, v in after.items() if k not in {"updated_at", "blocker", "suppress_until"}} == {
        k: v for k, v in before.items() if k not in {"updated_at", "blocker", "suppress_until"}}
    assert after["blocker"] is None and after["suppress_until"] == FUTURE
    result, _ = invoke(lane)
    assert result.get("decision") == "block" and "obligation:302 [runnable]" in reason(result)
    assert "obligation:301 [future]" in reason(result)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    subprocess.run(base + ["--suppress-until", past], env=lane[2], check=True, capture_output=True)
    result, _ = invoke(lane)
    assert "obligation:302 [runnable]" in reason(result)
    assert "Resend notification" not in reason(result)
    assert read()["evidence_json"] == before["evidence_json"]
    subprocess.run(base + ["--evidence-json", "{}"], env=lane[2], check=True, capture_output=True)
    assert read()["evidence_json"] == "{}"


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
        evidence["review_reason"] = "Agreed review window for checking correspondence evidence."
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


# ---- Two-speed drain (Rajiv D0AMF0XE6TS thread 1795338483.502879, ts 1795352601.197289, "Go") ----

def load_speed():
    """The hook is bash plus an embedded Python heredoc: compile everything except the final main() call."""
    src = (HOOKS / "pm-ops-sync-stop-validator.sh").read_text()
    body = src.split("<<'PYEOF'\n", 1)[1].rsplit("\nPYEOF", 1)[0].rsplit("\nmain()", 1)[0]
    namespace = {"__name__": "stop_drain_speed"}
    exec(compile(body, "pm-ops-sync-stop-validator.sh", "exec"), namespace)
    return namespace["speed"]


FINISHED_LANE = {"finished_at": "2026-01-01T00:00:00Z", "rc": 0, "reason": "done"}

SPEED_TABLE = [
    # FAST: customer/lockout, P0/high, due Slack/merge-ask/cadence, finished lanes, CI reconcile
    ({"kind": "customer_followup"}, "FAST"),
    ({"kind": "customer_recovery"}, "FAST"),
    ({"kind": "customer_wait"}, "FAST"),          # FAST beats the HOURLY "wait" token
    ({"kind": "lockout_followup"}, "FAST"),
    ({"kind": "lockout_watch"}, "FAST"),          # FAST beats the HOURLY "watch" token
    ({"kind": "p0_custody"}, "FAST"),
    ({"kind": "followup", "severity": "P0"}, "FAST"),
    ({"kind": "followup", "severity": "high"}, "FAST"),
    ({"kind": "followup", "severity": "critical"}, "FAST"),
    ({"kind": "followup", "target_type": "customer"}, "FAST"),
    # A Slack thread is a destination, not urgency: only the kind's own words make a Slack row FAST.
    ({"kind": "slack_reply_due", "target_type": "slack_thread"}, "FAST"),
    ({"kind": "slack_reply", "target_type": "slack_thread"}, "FAST"),
    ({"kind": "customer_reply", "target_type": "slack_thread"}, "FAST"),
    ({"kind": "promised_followup", "target_type": "slack_thread"}, "FAST"),
    ({"kind": "promised_followup", "target_type": "github_pr"}, "FAST"),
    ({"kind": "slack_request_stalled", "target_type": "slack_thread"}, "FAST"),   # unclassified: FAST
    ({"kind": "slack_intake_reconciliation", "target_type": "slack_thread"}, "FAST"),
    ({"kind": "slack_watch", "target_type": "slack_thread"}, "HOURLY"),
    ({"kind": "slack_redate", "target_type": "slack_thread"}, "HOURLY"),
    ({"kind": "followup", "target_type": "slack_thread"}, "HOURLY"),
    ({"kind": "followup", "target_type": "slack_thread", "severity": "high"}, "FAST"),
    ({"kind": "followup", "target_type": "transcript"}, "FAST"),
    ({"kind": "merge_ask"}, "FAST"),
    ({"kind": "cadence_inject"}, "FAST"),
    ({"kind": "admission_due"}, "FAST"),
    ({"kind": "ci_failure_reconcile"}, "FAST"),
    ({"kind": "pr_red_unowned"}, "FAST"),
    ({"kind": "security_followup"}, "FAST"),
    ({"kind": "proxy_run", "evidence_json": json.dumps(FINISHED_LANE)}, "FAST"),
    # HOURLY: routine re-dates, backlog/parked/watch/hold/wait rows, tooling, queued launches, running lanes
    ({"kind": "followup", "severity": "normal"}, "HOURLY"),
    ({"kind": "pm_followup"}, "HOURLY"),
    ({"kind": "ci_followup"}, "HOURLY"),
    ({"kind": "ops_tooling_followup", "target_type": "ops_tooling"}, "HOURLY"),
    ({"kind": "something", "target_type": "tooling"}, "HOURLY"),
    ({"kind": "pr_parked"}, "HOURLY"),
    ({"kind": "parking_review"}, "HOURLY"),
    ({"kind": "merge_hold"}, "HOURLY"),
    ({"kind": "pm_hold"}, "HOURLY"),
    ({"kind": "held_no_spend"}, "HOURLY"),
    ({"kind": "ext_wait"}, "HOURLY"),
    ({"kind": "agent_launch_queued"}, "HOURLY"),
    ({"kind": "burn_queued"}, "HOURLY"),
    ({"kind": "proxy_run", "evidence_json": json.dumps({"lane": "running"})}, "HOURLY"),
    ({"kind": "proxy_run", "evidence_json": "{}"}, "HOURLY"),
    # Unknown or unclassifiable defaults to FAST (the pre-change behaviour)
    ({"kind": "never_seen_before_kind"}, "FAST"),
    ({"kind": "investigation"}, "FAST"),
    ({"kind": None, "severity": None, "target_type": None}, "FAST"),
    ({}, "FAST"),
    ({"kind": "proxy_run", "evidence_json": "not json"}, "FAST"),
    ({"kind": "proxy_run", "evidence_json": "[]"}, "FAST"),
]


@pytest.mark.parametrize("fields,expected", SPEED_TABLE,
                         ids=[f"{i:02d}-{(f.get('kind') or 'none')}-{e}" for i, (f, e) in enumerate(SPEED_TABLE)])
def test_speed_classification_table(fields, expected):
    assert load_speed()(fields) == expected


def close_rows(lane, *idents):
    """Close fixture rows in the throwaway test ledger (never a live DB)."""
    with closing(sqlite3.connect(lane[1])) as con, con:
        for ident in idents:
            con.execute("UPDATE obligations SET status='resolved' WHERE id=?", (ident,))


def export_batch(lane, out=None, db=None):
    """Run the hook's read-only `--hourly-batch` export (what pm-hourly-post.sh calls) against the fixture ledger."""
    out = out or lane[3] / "routine-batch.json"
    env = {**lane[2], **({"PM_OPS_DB": str(db)} if db else {})}
    result = subprocess.run(["bash", str(lane[0] / "pm-ops-sync-stop-validator.sh"), "--hourly-batch", str(out)],
                            env=env, stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=9.8)
    assert result.returncode == 0, result.stderr
    return result.stdout, out


def seven_rows_five_fast(lane):
    add(lane, 501, severity="normal", kind="customer_followup")
    add(lane, 502, severity="normal", kind="p0_custody")
    add(lane, 503, severity="high")
    add(lane, 504, severity="normal", kind="merge_ask")
    add(lane, 505, severity="normal", kind="slack_reply_due", target_type="slack_thread")   # due Slack reply: FAST
    add(lane, 506, severity="normal")                                                        # routine followup
    add(lane, 507, severity="normal", kind="slack_watch", target_type="slack_thread")        # routine Slack watch


@pytest.mark.parametrize("grouped", [False, True])
def test_hourly_only_runnable_set_allows_stop_and_stop_never_lists_the_batch(lane, grouped):
    add(lane, 401, severity="normal")
    add(lane, 402, severity="normal", kind="pm_followup")
    before = lane[1].read_bytes()
    for _ in range(3):
        result, _ = invoke(lane, grouped=grouped)
        assert result.get("decision") != "block", "an hourly-only set must allow Stop"
        assert "fast-runnable=0 hourly-runnable=2" in reason(result) and "open commitments=2" in reason(result)
        # Stop is not a batch trigger (CTO REVISE #2 ts 1795354512.689229): no list, no marker, nothing to spend.
        assert "obligation:401" not in reason(result) and "Hourly routine batch" not in reason(result)
    assert lane[1].read_bytes() == before, "storage semantics unchanged: hook never writes the ledger"


@pytest.mark.parametrize("grouped", [False, True])
def test_three_plus_fast_rows_block_with_a_fast_scoped_prompt(lane, grouped):
    """CTO REVISE 1795353556.350659: five FAST rows used to push every routine row off the three-row list."""
    seven_rows_five_fast(lane)
    for _ in range(3):
        blocked, _ = invoke(lane, grouped=grouped)
        assert blocked.get("decision") == "block" and "Handle every currently runnable FAST" in reason(blocked)
        assert "fast-runnable=5 hourly-runnable=2" in reason(blocked)
        assert "remaining runnable/classification=2" in reason(blocked)
        assert "obligation:506" not in reason(blocked) and "obligation:507" not in reason(blocked)


def test_hourly_batch_export_returns_routine_rows_while_three_plus_fast_rows_stay_open(lane):
    """CTO REVISE #2: a standing FAST item must not keep the routine batch from the hourly lane."""
    seven_rows_five_fast(lane)
    before = lane[1].read_bytes()
    for _ in range(3):   # stateless: every hour (every call) sees the same routine rows, whatever the FAST set is
        stdout, out = export_batch(lane)
        data = json.loads(out.read_text())
        assert "ROUTINE_BATCH_EXPORTED hourly=2 fast=5 rows=2 ids=0" in stdout
        assert data["fast_runnable"] == 5 and data["hourly_runnable"] == 2 and data["deferred"] == 0
        assert [r["id"] for r in data["rows"]] == [506, 507] and data["ids"] == []
        assert all(r["owner"] and r["title"] and r["step"] for r in data["rows"])
    assert lane[1].read_bytes() == before, "the export is read-only"
    assert invoke(lane, grouped=False)[0].get("decision") == "block", "FAST rows still block Stop"
    close_rows(lane, 501, 502, 503, 504, 505)
    assert invoke(lane, grouped=False)[0].get("decision") != "block", "an hourly-only set allows Stop"
    assert [r["id"] for r in json.loads(export_batch(lane)[1].read_text())["rows"]] == [506, 507]


def test_large_routine_batch_export_is_bounded_rows_then_ids_then_a_count(lane):
    for ident in range(900, 960):
        add(lane, ident, severity="normal")
    data = json.loads(export_batch(lane)[1].read_text())
    assert data["hourly_runnable"] == 60 and len(data["rows"]) == 8 and len(data["ids"]) == 40
    assert data["rows"][0]["id"] == 900 and data["ids"][0] == 908 and data["deferred"] == 12


def test_export_failure_writes_nothing_and_reports_unavailable(lane):
    stdout, out = export_batch(lane, db=lane[3] / "missing.db")
    assert stdout.startswith("ROUTINE_BATCH_UNAVAILABLE") and not out.exists()


@pytest.mark.parametrize("extra", [{"kind": "never_seen_before_kind"}, {"kind": "investigation"},
                                   {"kind": "followup", "severity": "P1"}])
def test_unknown_or_fast_row_blocks_every_stop(lane, extra):
    add(lane, 601, **{"severity": "normal", **extra})
    for _ in range(3):
        result, _ = invoke(lane)
        assert result.get("decision") == "block" and "obligation:601 [runnable]" in reason(result)


@pytest.mark.parametrize("evidence,always_blocks", [
    ({"lane": "proxy-hd-builder-aaaa"}, False),
    (FINISHED_LANE, True),
])
def test_proxy_run_blocks_every_stop_only_after_the_lane_ended(lane, evidence, always_blocks):
    add(lane, 701, kind="proxy_run", severity="normal", evidence_json=evidence,
        required_action="Monitor proxy run and act on its result")
    for _ in range(2):
        result, _ = invoke(lane)
        assert (result.get("decision") == "block") is always_blocks


def run_wrapper(lane, bucket_hour):
    """Run a path-rewritten COPY of pm-hourly-post.sh with stubbed compose/Slack/cleanup, all under tmp (no live /tmp, ~/.claude or Slack)."""
    tmp = lane[3]
    scripts = tmp / "claude-scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "sakshi-heartbeat.sh").write_text("#!/bin/bash\nprintf '*Hourly - 10:13 IST*\\n\\nbody\\n'\n")
    (scripts / "slack-send.sh").write_text(
        "#!/bin/bash\nn=$(ls \"$STUB_SENDS\" | wc -l | tr -d ' ')\ncat > \"$STUB_SENDS/send-$n.txt\"\n"
        "echo \"OK ts=1795354000.00$n channel=C0ALZJHGE49\"\n")
    (scripts / "hourly-thread-ts").write_text("#!/bin/bash\nexit 0\n")
    (scripts / "tmux-dead-window-cleanup.sh").write_text("#!/bin/bash\nexit 0\n")
    sends = tmp / "sends"
    sends.mkdir(exist_ok=True)
    wrapper = (ROOT / "scripts/pm/shared-assets/claude/scripts/pm-hourly-post.sh").read_text()
    wrapper = wrapper.replace("/Users/rajiv/.claude/scripts/", f"{scripts}/").replace("/tmp/heartbeat-1h", f"{tmp}/heartbeat-1h")
    wrapper = wrapper.replace("BUCKET=$(date +%Y-%m-%d:%H)", f"BUCKET={bucket_hour}")
    copy = tmp / "pm-hourly-post.sh"
    copy.write_text(wrapper)
    env = {**lane[2], "STUB_SENDS": str(sends), "PM_STOP_DRAIN_HOOK": str(lane[0] / "pm-ops-sync-stop-validator.sh")}
    result = subprocess.run(["bash", str(copy)], env=env, stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=30)
    return result, sorted(sends.glob("send-*.txt")), tmp / ".claude" / "hourly-heartbeat-posts" / bucket_hour


def test_hourly_wrapper_exports_one_private_batch_with_counts_only_in_slack_while_fast_rows_stay_open(lane):
    """CTO REVISE #2/#3: one private routine-batch.json per bucket, counts-only report line, report post recorded apart from lane acceptance."""
    seven_rows_five_fast(lane)
    first, sends, claim = run_wrapper(lane, "2026-11-22:10")
    assert first.returncode == 0, first.stderr + first.stdout
    assert "ROUTINE_BATCH_REPORT_POSTED" in first.stdout and "hourly_runnable=2 fast_open=5" in first.stdout
    assert "ROUTINE_BATCH_HANDOFF" not in first.stdout, "the wrapper no longer claims a lane handoff; MoP records acceptance"
    assert len(sends) == 1
    posted = sends[0].read_text()
    assert "*Routine batch*: 2 runnable routine row(s)" in posted and "5 FAST row(s) stay on the Stop drain" in posted
    assert "obligation:" not in posted and "owner=" not in posted, "no ledger owners, titles or required actions in Slack"
    private = json.loads((claim / "routine-batch.json").read_text())
    assert private["hourly_runnable"] == 2 and private["fast_runnable"] == 5
    assert {row["id"] for row in private["rows"]} == {506, 507} and all(row["title"] for row in private["rows"])
    assert not (claim / "routine-batch.section.txt").exists() and not (claim / "routine-batch.handoff").exists()
    record = (claim / "routine-batch.posted").read_text()
    assert "outcome=REPORT_POSTED" in record and "parent_ts=1795354000.000" in record and "lane_acceptance=recorded-by-MoP" in record
    # A second invocation in the same hour finds the claim, posts nothing and records nothing again.
    again, sends_again, _ = run_wrapper(lane, "2026-11-22:10")
    assert "HOURLY_ALREADY_POSTED" in again.stdout and "ROUTINE_BATCH_REPORT_POSTED" not in again.stdout
    assert len(sends_again) == 1
    # The Stop hook is not a second trigger: with the same rows it never lists a batch.
    assert "Hourly routine batch" not in reason(invoke(lane, grouped=False)[0])
    # Next hour exports the still-open routine rows again, once.
    nxt, sends_next, _ = run_wrapper(lane, "2026-11-22:11")
    assert nxt.stdout.count("ROUTINE_BATCH_REPORT_POSTED") == 1 and len(sends_next) == 2


def test_hourly_wrapper_posts_without_a_batch_when_the_export_fails_or_nothing_is_routine(lane):
    add(lane, 801, severity="high")   # FAST only: nothing routine to hand off
    done, sends, claim = run_wrapper(lane, "2026-11-22:10")
    assert done.returncode == 0 and "*Routine batch" not in sends[0].read_text()
    assert "ROUTINE_BATCH_REPORT_POSTED" not in done.stdout and not (claim / "routine-batch.posted").exists()
    (lane[3] / "missing-hook-run").mkdir()
    env_lane = (lane[0], lane[1], {**lane[2], "PM_OPS_DB": str(lane[3] / "missing.db")}, lane[3])
    failed, sends2, _ = run_wrapper(env_lane, "2026-11-22:11")
    assert failed.returncode == 0 and "ROUTINE_BATCH_SKIPPED" in failed.stdout and "POSTED ts=" in failed.stdout
    assert len(sends2) == 2


def test_hourly_thread_ts_sends_the_bucket_to_mop_so_the_lane_binds_this_buckets_batch():
    src = (HOOKS.parent / "scripts" / "hourly-thread-ts").read_text()
    assert '"receipt": a.receipt.strip(), "bucket": a.bucket' in src


def test_stop_hook_has_no_hourly_marker_and_never_touches_user_prompt_submit():
    """Rule 50: confined to the Stop obligation hook; no gate in front of PM input or tools; no second batch trigger."""
    src = (HOOKS / "pm-ops-sync-stop-validator.sh").read_text()
    assert "UserPromptSubmit" not in src and "PreToolUse" not in src
    assert "hourly_pass_due" not in src and "fcntl" not in src and "PM_OBLIGATION_HOURLY" not in src
