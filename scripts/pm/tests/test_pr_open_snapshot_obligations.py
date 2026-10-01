"""Tests for the pr-open-snapshot.py deterministic open-PR obligation sync.

Rajiv 2026-09-30 00:58/01:00 IST, thread C0ALZJHGE49/1790707604.948989:
"the pr's should never get stuck" / "there's already an obligation system we
have. reuse that instead of adding a new hook". These tests cover the three
concerns (pr_admission / pr_red_unowned / pr_merge_ask), the owned-by-slot
skip (draft / pm-blocked / MoP-owned), and stale-PR resolution -- all through
the obligation-upsert/-resolve CLI calls, which are mocked here (no live DB
writes from a test run).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts/pm/heartbeat"


def _load(name: str, base: Path = HERE):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), base / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


prsnap = _load("pr-open-snapshot")


def _check(name, conclusion="SUCCESS", status="COMPLETED", started="2026-09-29T10:00:00Z", completed="2026-09-29T10:10:00Z"):
    return {
        "name": name,
        "workflowName": name,
        "conclusion": conclusion,
        "status": status,
        "startedAt": started,
        "completedAt": completed,
    }


def _pr(number, head="a" * 40, labels=None, draft=False, checks=None, title="Some PR"):
    return {
        "number": number,
        "headRefOid": head,
        "isDraft": draft,
        "title": title,
        "labels": [{"name": name} for name in (labels or [])],
        "statusCheckRollup": checks or [],
    }


# --- gate_state -------------------------------------------------------------


def test_gate_state_red_when_required_gate_fails():
    pr = _pr(1, checks=[_check("test", conclusion="FAILURE"), _check("e2e")])
    state = prsnap.gate_state(pr)
    assert state["required_red"] == ["test"]
    assert state["green_ready"] is False


def test_gate_state_green_ready_requires_real_duration():
    short = _check("test", started="2026-09-29T10:00:00Z", completed="2026-09-29T10:01:00Z")
    long_e2e = _check("e2e", started="2026-09-29T10:00:00Z", completed="2026-09-29T10:20:00Z")
    pr = _pr(2, checks=[short, long_e2e])
    state = prsnap.gate_state(pr)
    # "test" ran under 5 minutes -> not counted as a real green gate.
    assert state["green_ready"] is False


def test_gate_state_green_ready_true_with_both_gates_long_and_green():
    pr = _pr(
        3,
        checks=[
            _check("test", started="2026-09-29T10:00:00Z", completed="2026-09-29T10:10:00Z"),
            _check("e2e", started="2026-09-29T10:00:00Z", completed="2026-09-29T10:20:00Z"),
        ],
    )
    state = prsnap.gate_state(pr)
    assert state["required_red"] == []
    assert state["green_ready"] is True


# --- sync_pr_obligations -----------------------------------------------------


def test_sync_creates_pr_admission_when_unowned_and_unadmitted():
    pr = _pr(100, labels=[], checks=[])
    with patch.object(prsnap, "_obligation_upsert") as up, patch.object(prsnap, "_obligation_resolve") as res, \
         patch.object(prsnap, "_resolve_stale_pr_obligations"):
        created = prsnap.sync_pr_obligations([pr], mop_owned_prs=set())
    kinds = {c["kind"] for c in created}
    assert "pr_admission" in kinds
    up_kinds = {call.kwargs["kind"] for call in up.call_args_list}
    assert "pr_admission" in up_kinds
    res_kinds = {call.kwargs["kind"] for call in res.call_args_list}
    # admission was created (not resolved) but red/merge_ask should resolve
    assert "pr_red_unowned" in res_kinds
    assert "pr_merge_ask" in res_kinds


def test_sync_creates_pr_red_unowned_when_required_gate_fails():
    pr = _pr(101, labels=["ci-head:" + "a" * 40], head="a" * 40, checks=[_check("test", conclusion="FAILURE"), _check("e2e")])
    with patch.object(prsnap, "_obligation_upsert") as up, patch.object(prsnap, "_obligation_resolve"), \
         patch.object(prsnap, "_resolve_stale_pr_obligations"):
        created = prsnap.sync_pr_obligations([pr], mop_owned_prs=set())
    kinds = {c["kind"] for c in created}
    assert "pr_red_unowned" in kinds
    assert "pr_admission" not in kinds  # ci-head label matches current head


def test_sync_creates_pr_merge_ask_when_green_and_admitted():
    head = "b" * 40
    pr = _pr(
        102,
        labels=["ci-head:" + head],
        head=head,
        checks=[
            _check("test", started="2026-09-29T10:00:00Z", completed="2026-09-29T10:10:00Z"),
            _check("e2e", started="2026-09-29T10:00:00Z", completed="2026-09-29T10:20:00Z"),
        ],
    )
    with patch.object(prsnap, "_obligation_upsert") as up, patch.object(prsnap, "_obligation_resolve"), \
         patch.object(prsnap, "_resolve_stale_pr_obligations"):
        created = prsnap.sync_pr_obligations([pr], mop_owned_prs=set())
    kinds = {c["kind"] for c in created}
    assert kinds == {"pr_merge_ask"}


def test_sync_skips_draft_pr_and_resolves_existing_obligations():
    pr = _pr(103, draft=True, checks=[])
    with patch.object(prsnap, "_obligation_upsert") as up, patch.object(prsnap, "_obligation_resolve") as res, \
         patch.object(prsnap, "_resolve_stale_pr_obligations"):
        created = prsnap.sync_pr_obligations([pr], mop_owned_prs=set())
    assert created == []
    up.assert_not_called()
    resolved_kinds = {call.kwargs["kind"] for call in res.call_args_list}
    assert resolved_kinds == set(prsnap.OBLIGATION_KINDS)


def test_sync_skips_pm_blocked_pr():
    pr = _pr(104, labels=["pm-blocked:product"], checks=[])
    with patch.object(prsnap, "_obligation_upsert") as up, patch.object(prsnap, "_obligation_resolve"), \
         patch.object(prsnap, "_resolve_stale_pr_obligations"):
        created = prsnap.sync_pr_obligations([pr], mop_owned_prs=set())
    assert created == []
    up.assert_not_called()


def test_sync_skips_mop_owned_pr():
    pr = _pr(105, labels=[], checks=[])
    with patch.object(prsnap, "_obligation_upsert") as up, patch.object(prsnap, "_obligation_resolve"), \
         patch.object(prsnap, "_resolve_stale_pr_obligations"):
        created = prsnap.sync_pr_obligations([pr], mop_owned_prs={105})
    assert created == []
    up.assert_not_called()


def test_sync_calls_stale_resolution_with_seen_open_prs():
    pr = _pr(106, labels=[], checks=[])
    with patch.object(prsnap, "_obligation_upsert"), patch.object(prsnap, "_obligation_resolve"), \
         patch.object(prsnap, "_resolve_stale_pr_obligations") as stale:
        prsnap.sync_pr_obligations([pr], mop_owned_prs=set())
    stale.assert_called_once_with({106})


# --- CLI plumbing (mocked subprocess, must never touch the live DB) --------


def test_obligation_upsert_invokes_expected_cli_shape():
    with patch.object(prsnap, "PM_OPS_CLI", HERE / "pr-open-snapshot.py"), patch("subprocess.run") as run:
        prsnap._obligation_upsert(kind="pr_admission", pr=1, head="deadbeef", title="t", action="a")
    assert run.called
    argv = run.call_args.args[0]
    assert "obligation-upsert" in argv
    assert "--kind" in argv and "pr_admission" in argv
    assert "--pr" in argv and "1" in argv
    assert "--evidence-json" in argv


def test_obligation_resolve_swallows_nonzero_exit_and_exceptions():
    with patch.object(prsnap, "PM_OPS_CLI", HERE / "pr-open-snapshot.py"), patch("subprocess.run", side_effect=Exception("boom")):
        # must not raise
        prsnap._obligation_resolve(kind="pr_admission", pr=1, reason="x")


def test_obligation_calls_are_noop_when_cli_missing():
    with patch.object(prsnap, "PM_OPS_CLI", Path("/nonexistent/pm-ops.py")), patch("subprocess.run") as run:
        prsnap._obligation_upsert(kind="pr_admission", pr=1, head="h", title="t", action="a")
        prsnap._obligation_resolve(kind="pr_admission", pr=1, reason="x")
    run.assert_not_called()


# --- ownership claim (Rajiv follow-up: ci-repair-agent already on #8525) ----


def _now_iso(minutes_ago=0):
    import datetime

    ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes_ago)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_is_fresh_owner_claim_true_within_ttl():
    row = {"owner": "ci-repair-agent", "updated_at": _now_iso(minutes_ago=10)}
    assert prsnap._is_fresh_owner_claim(row) is True


def test_is_fresh_owner_claim_false_when_expired():
    row = {"owner": "ci-repair-agent", "updated_at": _now_iso(minutes_ago=90)}
    assert prsnap._is_fresh_owner_claim(row) is False


def test_is_fresh_owner_claim_false_when_owner_is_pm_or_empty():
    assert prsnap._is_fresh_owner_claim({"owner": "PM", "updated_at": _now_iso()}) is False
    assert prsnap._is_fresh_owner_claim({"owner": "", "updated_at": _now_iso()}) is False
    assert prsnap._is_fresh_owner_claim(None) is False


def test_sync_skips_upsert_when_pr_red_unowned_freshly_claimed():
    pr = _pr(107, labels=["ci-head:" + "c" * 40], head="c" * 40, checks=[_check("test", conclusion="FAILURE"), _check("e2e")])
    claimed_row = {"owner": "ci-repair-agent", "updated_at": _now_iso(minutes_ago=5)}
    with patch.object(prsnap, "_obligation_upsert") as up, patch.object(prsnap, "_obligation_resolve"), \
         patch.object(prsnap, "_resolve_stale_pr_obligations"), \
         patch.object(prsnap, "_existing_obligation", side_effect=lambda kind, pr_num: claimed_row if kind == "pr_red_unowned" else None):
        created = prsnap.sync_pr_obligations([pr], mop_owned_prs=set())
    assert created == []
    up.assert_not_called()


def test_sync_still_upserts_when_claim_expired():
    pr = _pr(108, labels=["ci-head:" + "d" * 40], head="d" * 40, checks=[_check("test", conclusion="FAILURE"), _check("e2e")])
    stale_row = {"owner": "ci-repair-agent", "updated_at": _now_iso(minutes_ago=90)}
    with patch.object(prsnap, "_obligation_upsert") as up, patch.object(prsnap, "_obligation_resolve"), \
         patch.object(prsnap, "_resolve_stale_pr_obligations"), \
         patch.object(prsnap, "_existing_obligation", side_effect=lambda kind, pr_num: stale_row if kind == "pr_red_unowned" else None):
        created = prsnap.sync_pr_obligations([pr], mop_owned_prs=set())
    kinds = {c["kind"] for c in created}
    assert "pr_red_unowned" in kinds


def test_obligation_upsert_only_passes_owner_flag_when_given():
    with patch.object(prsnap, "PM_OPS_CLI", HERE / "pr-open-snapshot.py"), patch("subprocess.run") as run:
        prsnap._obligation_upsert(kind="pr_red_unowned", pr=1, head="h" * 40, title="t", action="a")
    argv = run.call_args.args[0]
    assert "--owner" not in argv


# --- sync path skips the review-thread GraphQL read (Stop hook budget is 8s) ----------------


def test_sync_obligations_fetch_skips_review_thread_graphql():
    class _Proc:
        returncode = 0
        stdout = '[{"number": 7, "headRefOid": "' + "a" * 40 + '"}]'
        stderr = ""

    def _boom():
        raise AssertionError("review threads must not be fetched on the sync path")

    with patch.object(prsnap.subprocess, "run", return_value=_Proc()), patch.object(prsnap, "fetch_review_threads", _boom):
        rows = prsnap.fetch_open_prs(with_threads=False)
    assert rows[0]["reviewThreads"] is None

    with patch.object(prsnap.subprocess, "run", return_value=_Proc()), patch.object(prsnap, "fetch_review_threads", lambda: {7: []}):
        assert prsnap.fetch_open_prs()[0]["reviewThreads"] == []


# --- 12b BLOCK / rework-queued / merge-ask-pending holds (Rajiv 2026-10-01 13:17 IST,
# C0ALZJHGE49/1790840684.251169: "why are so many pr's stuck at pm 12b review?") ----


def _future_iso(minutes=60):
    import datetime

    ts = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=minutes)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _sync(pr, rows):
    with patch.object(prsnap, "_obligation_upsert") as up, patch.object(prsnap, "_obligation_resolve") as res, \
         patch.object(prsnap, "_resolve_stale_pr_obligations"), \
         patch.object(prsnap, "_existing_obligation", side_effect=lambda kind, n: rows.get(kind)):
        created = prsnap.sync_pr_obligations([pr], mop_owned_prs=set())
    return created, up, res


def test_rework_queued_row_suppresses_pr_admission_and_classifies():
    pr = _pr(200)
    row = {"owner": "pm-rework-queued", "updated_at": _now_iso(minutes_ago=120),
           "suppress_until": _future_iso(), "evidence_json": "{}"}
    created, up, _ = _sync(pr, {"pr_admission": row})
    assert created == [] and not up.called
    assert prsnap.admission_hold(pr, {"pr_admission": row}) == prsnap.REWORK_QUEUED
    assert prsnap.classify_pr(pr, holds={200: prsnap.REWORK_QUEUED})["next"] == "rework-queued (no slot)"


def test_rework_queued_row_for_old_head_reraises_admission():
    pr = _pr(201, head="e" * 40)
    row = {"owner": "pm-rework-queued", "updated_at": _now_iso(minutes_ago=120),
           "suppress_until": _future_iso(), "evidence_json": '{"head": "' + "f" * 40 + '"}'}
    created, _, _ = _sync(pr, {"pr_admission": row})
    assert {"pr": 201, "kind": "pr_admission"} in created


def test_rework_queued_headless_row_expired_reraises_admission():
    pr = _pr(202)
    row = {"owner": "pm-rework-queued", "updated_at": _now_iso(minutes_ago=300),
           "suppress_until": _now_iso(minutes_ago=5), "evidence_json": "{}"}
    created, _, _ = _sync(pr, {"pr_admission": row})
    assert {"pr": 202, "kind": "pr_admission"} in created


def test_12b_block_comment_on_current_head_is_rework_queued():
    head = "1234567" + "0" * 33
    pr = _pr(203, head=head)
    pr["comments"] = [{"body": "PM_OPUS_REVIEW: BLOCK @1234567 - missing guard"}]
    created, up, _ = _sync(pr, {})
    assert created == [] and not up.called
    assert prsnap.classify_pr(pr)["next"] == prsnap.REWORK_QUEUED
    pr["headRefOid"] = "9" * 40  # new head -> back to review-admit
    assert prsnap.classify_pr(pr)["next"] == "PM 12b review"
    created, _, _ = _sync(pr, {})
    assert {"pr": 203, "kind": "pr_admission"} in created


def test_ci_exempt_with_pending_merge_ask_is_not_review_admit():
    pr = _pr(204)
    pr["files"] = [{"path": "docs-site/pages/faq.mdx"}]
    ask = {"owner": "pr-merges", "updated_at": _now_iso(minutes_ago=200),
           "suppress_until": None, "evidence_json": '{"head": "' + "a" * 40 + '"}'}
    created, up, res = _sync(pr, {"pr_merge_ask": ask})
    assert created == [] and not up.called
    assert "pr_merge_ask" not in {c.kwargs["kind"] for c in res.call_args_list}
    hold = prsnap.admission_hold(pr, {"pr_merge_ask": ask})
    assert hold == prsnap.MERGE_ASK_PENDING
    assert prsnap.classify_pr(pr, holds={204: hold})["next"] == "merge-ask-pending"


def test_ci_exempt_without_ask_raises_merge_ask_not_admission():
    pr = _pr(205)
    pr["files"] = [{"path": "docs-site/pages/faq.mdx"}]
    created, _, res = _sync(pr, {})
    assert created == [{"pr": 205, "kind": "pr_merge_ask"}]
    assert "pr_admission" in {c.kwargs["kind"] for c in res.call_args_list}
    assert prsnap.classify_pr(pr)["next"] == "CI-exempt -> merge ask"


def test_later_same_head_12b_pass_releases_earlier_block():
    head = "7654321" + "0" * 33
    pr = _pr(206, head=head)
    pr["comments"] = [
        {"body": "PM_OPUS_REVIEW: BLOCK @7654321 - missing guard"},
        {"body": "PM_OPUS_REVIEW: PASS @7654321"},
    ]
    rework_row = {"owner": "pm-rework-queued", "updated_at": _now_iso(minutes_ago=30),
                  "suppress_until": _future_iso(), "evidence_json": '{"head": "' + head + '"}'}
    assert prsnap.admission_hold(pr, {"pr_admission": rework_row}) is None
    created, _, _ = _sync(pr, {"pr_admission": rework_row})
    assert {"pr": 206, "kind": "pr_admission"} in created
    assert prsnap.classify_pr(pr)["next"] != prsnap.REWORK_QUEUED
    pr["comments"].append({"body": "PM_OPUS_REVIEW: BLOCK @7654321 - regression"})
    assert prsnap.admission_hold(pr, {}) == prsnap.REWORK_QUEUED


def test_ci_exempt_with_red_gate_gets_no_merge_ask_and_keeps_red():
    pr = _pr(207, checks=[_check("test", conclusion="FAILURE")])
    pr["files"] = [{"path": "docs-site/pages/faq.mdx"}]
    ask = {"owner": "pr-merges", "updated_at": _now_iso(minutes_ago=10),
           "suppress_until": None, "evidence_json": '{"head": "' + "a" * 40 + '"}'}
    created, up, res = _sync(pr, {"pr_merge_ask": ask})
    assert {"pr": 207, "kind": "pr_red_unowned"} in created
    assert "pr_merge_ask" not in {c.kwargs["kind"] for c in up.call_args_list}
    assert "pr_merge_ask" in {c.kwargs["kind"] for c in res.call_args_list}
    assert prsnap.admission_hold(pr, {"pr_merge_ask": ask}) is None
