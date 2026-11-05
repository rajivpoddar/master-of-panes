"""Rajiv 1793882883.242249: collectors report routine work, not ledger copies.

Real producer entrypoints and the canonical writer, with a disposable ledger
and stubbed GitHub/process boundaries. Never reads or writes the live PM ledger.
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts"
sys.path.insert(0, str(SCRIPTS))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PM_OPS_DB", str(tmp_path / "ops.db"))
    ops = load("tracking_purpose_ops", "pm-ops-legacy.py")
    kinds = ["orphaned_issue", "customer_followup", "billing_reconciliation",
             "followup", "acceptance_test", "pr_admission"]
    for index, kind in enumerate(kinds, 10):
        evidence = {"source": "Rajiv: explicitly track this commitment",
                    "outcome": "UNKNOWN_CONSUMED_NO_RETRY"}
        if kind == "pr_admission":
            evidence = {"HELD: retained safety proof": True}
        args = argparse.Namespace(
            kind=kind, target_type="github_issue", target_id=str(index),
            pr=None, issue=index, slot=None, owner="existing-owner",
            severity="high", horizon="hourly", title="Explicit commitment",
            action="Preserve the promised acceptance and original uncertain effect; no resend",
            blocker="Actual acceptance is incomplete", evidence=[],
            evidence_json=json.dumps(evidence), print_id=False,
            suppress_until="2099-01-01T00:00:00Z" if kind == "customer_followup" else None,
        )
        ops.upsert_obligation(args)
    return ops


def rows(ops):
    return [dict(row) for row in ops.open_obligations(readonly=True)]


def test_repeated_lifecycle_collection_preserves_explicit_scope(ledger, tmp_path, monkeypatch):
    m = load("tracking_purpose_lifecycle", "pm/heartbeat/issue-lifecycle.py")
    monkeypatch.setattr(m, "STATE", tmp_path / "lifecycle.json")
    monkeypatch.setattr(m, "PM_OPS", SCRIPTS / "pm-ops-legacy.py")
    monkeypatch.setattr(m, "ledger_path", lambda: ledger.DB_PATH)
    issues = [dict(number=n, state="OPEN", labels=["status:done"], references=[], title="Routine")
              for n in (10, 99)]
    monkeypatch.setattr(m, "collect", lambda since: (issues, [], [], [], lambda n: False))
    before = rows(ledger)
    for _ in range(3):
        result = m.run()
        assert [r["issue"] for r in result["findings"]] == [10, 99]
        assert "done_open=2" in result["lines"][1]
    assert rows(ledger) == before  # same-kind explicit row is not overwritten or auto-resolved


def test_repeated_pr_compatibility_entrypoint_never_mirrors(ledger, monkeypatch, capsys):
    m = load("tracking_purpose_pr", "pm/heartbeat/pr-open-snapshot.py")
    monkeypatch.setattr(m, "PM_OPS_CLI", SCRIPTS / "pm-ops-legacy.py")
    monkeypatch.setattr(m, "PM_OPS_DB", ledger.DB_PATH)
    monkeypatch.setattr(m, "fetch_mop_owned_prs", lambda: set())
    monkeypatch.setattr(m, "fetch_open_prs", lambda **kw: [
        dict(number=99, headRefOid="a" * 40, labels=[], isDraft=False, title="Routine", statusCheckRollup=[])
    ])
    monkeypatch.setattr(sys, "argv", ["pr-open-snapshot.py", "--sync-obligations"])
    before = rows(ledger)
    for _ in range(3):
        assert m.main() == 0
        assert json.loads(capsys.readouterr().out) == {"obligations_synced": []}
    assert rows(ledger) == before


def test_pr_summary_keeps_red_actions_and_explicit_holds(monkeypatch, capsys):
    m = load("tracking_purpose_pr_summary", "pm/heartbeat/pr-open-snapshot.py")
    head = "a" * 40
    pr = dict(number=99, headRefOid=head, labels=[{"name": "ci-head:" + head}],
              isDraft=False, title="Actual red", statusCheckRollup=[
                  {"name": "test", "conclusion": "FAILURE", "status": "COMPLETED"}])
    monkeypatch.setattr(m, "fetch_mop_owned_prs", lambda: set())
    monkeypatch.setattr(m, "fetch_open_prs", lambda **kw: [pr])
    monkeypatch.setattr(m, "fetch_live_claims", lambda prs: {})
    monkeypatch.setattr(m, "_existing_obligation", lambda kind, n: None)
    monkeypatch.setattr(sys, "argv", ["pr-open-snapshot.py"])
    assert m.main() == 0
    report = json.loads(capsys.readouterr().out)[0]
    assert "red" in report["state"].lower()
    assert report["actions"]  # work remains visible without a second obligation
    hold = {"owner": "pm-hotfix-rework", "evidence_json": json.dumps({"head": head})}
    assert m.admission_hold(pr, {"pr_admission": hold})


def test_triage_findings_do_not_mirror_into_ledger(ledger, monkeypatch):
    m = load("tracking_purpose_triage", "backlog-triage.py")
    monkeypatch.setattr(m, "PM_OPS", SCRIPTS / "pm-ops-legacy.py")
    buckets = {key: [dict(number=99, reason="routine metadata")]
               for key in ("todo_priority_label_drift", "backlog_promote_candidate", "backlog_metadata_missing")}
    before = rows(ledger)
    for _ in range(3):
        m.upsert_triage_obligations(buckets, None)
    assert rows(ledger) == before


def test_selector_keeps_pick_and_dependency_warning_without_sync(tmp_path, monkeypatch, capsys):
    m = load("tracking_purpose_selector", "ready_pool_select.py")
    issue = dict(number=99, title="Ready issue", labels=[{"name": "status:todo"}, {"name": "P1"}],
                 body="", createdAt="2026-11-01T00:00:00Z")
    source = tmp_path / "issues.json"
    source.write_text(json.dumps([issue]))
    monkeypatch.setattr(m, "load_open_prs", lambda: [])
    monkeypatch.setattr(m, "prefetch_dependencies", lambda issues, cache: None)
    monkeypatch.setattr(m, "fetch_dependency_graph", lambda gated: ({}, []))
    warning = {"issue": 88, "flags": ["CYCLE"], "action": "Inspect the existing dependency cycle"}
    monkeypatch.setattr(m, "dependency_hell_report", lambda *args: ([warning], []))
    sync = Mock(side_effect=AssertionError("routine dependency warning must not touch obligations"))
    monkeypatch.setattr(m, "sync_obligations", sync)
    for _ in range(3):
        assert m.main(["--issues-json", str(source), "--sync-obligations"]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["pick"]["issue"] == 99
        assert warning in result["dependency_hell"]
    sync.assert_not_called()


def test_guarded_retirement_preserves_changed_and_claimed_rows(ledger, capsys):
    before = rows(ledger)
    claimed = next(row for row in before if row["kind"] == "customer_followup")
    for row, changes, expected in [(before[0], {"expect_updated_at": "stale"}, "skipped_changed"),
                                  (before[1], {"expect_evidence_sha256": "0" * 64}, "skipped_changed"),
                                  (claimed, {}, "skipped_claimed")]:
        args = argparse.Namespace(kind=row["kind"], target_type=row["target_type"],
            target_id=row["target_id"], pr=row["pr"], issue=row["issue"], slot=None,
            id=row["id"], if_unclaimed=True, reason="routine tracking retained in GitHub/PM",
            expect_updated_at=row["updated_at"], expect_evidence_sha256=ledger.evidence_fingerprint(row["evidence_json"]))
        for name, value in changes.items():
            setattr(args, name, value)
        ledger.resolve_obligation(args)
        assert capsys.readouterr().out.strip() == expected
    assert rows(ledger) == before
