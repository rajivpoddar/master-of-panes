"""Green CI is not a product-readiness disposition (2026-10-02 incident)."""

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

HERE = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts/pm/heartbeat"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


snapshot = _load("pr-open-snapshot")
composer = _load("heartbeat-compose")


def _pr(number=9010, head="a" * 40):
    return {
        "number": number, "headRefOid": head, "title": "product change",
        "isDraft": False, "labels": [{"name": f"ci-head:{head}"}],
        "statusCheckRollup": [
            {"name": name, "conclusion": "SUCCESS", "status": "COMPLETED",
             "startedAt": "2026-10-02T10:00:00Z", "completedAt": "2026-10-02T10:20:00Z"}
            for name in ("test", "e2e")
        ],
        "reviewThreads": [],
    }


def _disposition(pull, disposition="blocked", **overrides):
    return {
        "schema_version": 1, "pr": str(pull["number"]), "head_sha": pull["headRefOid"],
        "status": "resolved", "resolution": disposition, "blocked_reason": "product",
        **overrides,
    }


@pytest.mark.parametrize("resolution", ["blocked", None])
def test_green_product_pr_without_ready_disposition_has_no_merge_ask(resolution):
    pr = _pr()
    if resolution:
        pr["ci_reconciliation"] = _disposition(pr, resolution)
    row = snapshot.classify_pr(pr)
    assert row["state"] == "CI+E2E green"
    assert row["next"] != "CTO merge ask"
    assert "readiness" in row["next"]
    post = composer.compose("1h", datetime.now(timezone.utc), {"events": 0}, {}, [row])
    assert "merge-ready" not in post and "*Asks:*" not in post
    assert composer.CTO not in post
    assert f"#{pr['number']}" in post and "CI+E2E green" in post


def test_blocked_and_missing_dispositions_do_not_create_merge_obligations():
    blocked, missing = _pr(), _pr(9011)
    blocked["ci_reconciliation"] = _disposition(blocked)
    with patch.object(snapshot, "_obligation_upsert") as up, \
         patch.object(snapshot, "_obligation_resolve") as resolve, \
         patch.object(snapshot, "_existing_obligation", return_value=None), \
         patch.object(snapshot, "_resolve_stale_pr_obligations"):
        created = snapshot.sync_pr_obligations([blocked, missing], set())
    assert created == []
    up.assert_not_called()
    assert {c.kwargs["pr"] for c in resolve.call_args_list
            if c.kwargs["kind"] == "pr_merge_ask"} == {9010, 9011}


@pytest.mark.parametrize("overrides", [
    {"head_sha": "b" * 40}, {"pr": "9011"}, {"status": "pending"},
    {"status": "superseded"}, {"resolution": "blocked"},
])
def test_stale_or_unresolved_receipt_is_not_readiness(overrides):
    pr = _pr()
    pr["ci_reconciliation"] = _disposition(pr, "merge_ready", **overrides)
    assert snapshot.classify_pr(pr)["next"] != "CTO merge ask"


def test_exact_head_ready_receipt_preserves_legitimate_merge_ask():
    pr = _pr()
    pr["ci_reconciliation"] = _disposition(pr, "merge_ready")
    assert snapshot.classify_pr(pr)["next"] == "CTO merge ask"


@pytest.mark.parametrize("hold", ["body", "review"])
def test_existing_product_hold_wins_over_ready_receipt(hold):
    pr = _pr()
    pr["ci_reconciliation"] = _disposition(pr, "merge_ready")
    if hold == "body":
        pr["body"] = "DO NOT MERGE: rendered proof missing"
    else:
        pr["comments"] = [{"body": f"PM_OPUS_REVIEW: BLOCK {pr['headRefOid']}"}]
    assert snapshot.classify_pr(pr)["next"] != "CTO merge ask"


def test_live_producer_reads_existing_reconciliation_sentinel(tmp_path, monkeypatch):
    # Sanitized production shapes: exact-head resolved/blocker and absent receipt.
    blocked, missing = _pr(), _pr(9011)
    receipt = _disposition(blocked, blocked_reason="capture")
    (tmp_path / "pm-required-ci-reconcile-9010.json").write_text(json.dumps(receipt))
    monkeypatch.setenv("CI_SUCCESS_SENTINEL_DIR", str(tmp_path))
    proc = type("Proc", (), {"returncode": 0, "stdout": json.dumps([blocked, missing]), "stderr": ""})()
    with patch.object(snapshot.subprocess, "run", return_value=proc):
        prs = snapshot.fetch_open_prs(with_threads=False)
    rows = [snapshot.classify_pr(pr) for pr in prs]
    assert rows[0]["next"] == "PM readiness blocked: capture"
    assert rows[1]["next"] == "PM readiness unverified"
    assert len(rows) == 2 and all(r["state"] == "CI+E2E green" for r in rows)
    post = composer.compose("1h", datetime.now(timezone.utc), {"events": 0}, {}, rows)
    assert "2 open PRs" in post and "merge-ready" not in post and "*Asks:*" not in post


@pytest.mark.parametrize("contents", ["{bad", "[]", "null"])
def test_unreadable_disposition_preserves_inventory(tmp_path, monkeypatch, contents):
    pr = _pr()
    (tmp_path / "pm-required-ci-reconcile-9010.json").write_text(contents)
    monkeypatch.setenv("CI_SUCCESS_SENTINEL_DIR", str(tmp_path))
    proc = type("Proc", (), {"returncode": 0, "stdout": json.dumps([pr]), "stderr": ""})()
    with patch.object(snapshot.subprocess, "run", return_value=proc):
        rows = snapshot.fetch_open_prs(with_threads=False)
    assert len(rows) == 1
    assert snapshot.classify_pr(rows[0])["next"] == "PM readiness unverified"
