"""Bulk list with the CI rollup, per-PR files/body/comments only where classification needs them,
and a whole-run budget that reports honest unknowns.

One `gh pr list --json ...statusCheckRollup,files,body,comments` over every open PR timed out
3 x 60s. The bulk list now omits files, body and comments (statusCheckRollup stays); a PR gets a
per-PR read only if classifying it touches one of them. Drafts classify from the list alone.
"""
import importlib.util
import sys
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from unittest.mock import patch

import pytest

PATH = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts/pm/heartbeat/pr-open-snapshot.py"
spec = importlib.util.spec_from_file_location("snapshot_light_list", PATH)
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)

HEAD = "a" * 40
OK = lambda name: {"name": name, "conclusion": "SUCCESS", "status": "COMPLETED"}
RED = lambda name: {"name": name, "conclusion": "FAILURE", "status": "COMPLETED"}
RUNNING = lambda name: {"name": name, "status": "IN_PROGRESS"}


def pr(number, rollup=(), labels=(), draft=False, head=HEAD):
    return {
        "number": number, "headRefOid": head, "title": f"t{number}", "isDraft": draft,
        "labels": [{"name": n} for n in labels], "statusCheckRollup": list(rollup),
        "_authorized_12b_reviewer": "bot",
    }


def touched(listed):
    probe = snapshot._ProbePR(listed)
    with patch.object(snapshot, "_existing_obligation", return_value=None):
        row = snapshot.classify_pr(probe, set(), snapshot._PRHold(probe), None)
    return probe.touched, row


@pytest.fixture(autouse=True)
def fresh_state():
    snapshot._RUN_STARTED = None
    snapshot._RUN.update(prs=None, rows={}, done=False)
    snapshot._read_pr_details = snapshot._memoize(snapshot._read_pr_details_uncached)
    yield
    snapshot._RUN_STARTED = None


def ok(stdout):
    return CompletedProcess([], 0, stdout=stdout, stderr="")


def test_bulk_list_has_rollup_and_no_files_body_comments():
    fields = set(snapshot.LIST_FIELDS.split(","))
    assert "statusCheckRollup" in fields
    assert not fields & {"files", "body", "comments"}
    listed = '[{"number": 1, "headRefOid": "%s", "isDraft": true, "labels": []}]' % HEAD
    with patch.object(snapshot.subprocess, "run", return_value=ok(listed)) as run, \
         patch.object(snapshot, "_read_ci_reconciliation", return_value=None):
        snapshot._fetch_open_prs_live(with_threads=False)
    assert run.call_count == 1  # the list only; no per-PR read while listing


@pytest.mark.parametrize("listed", [
    pr(1, draft=True),                                         # draft: labels only
    pr(2, rollup=[RUNNING("test")]),                           # CI running
    pr(3, rollup=[OK("test"), OK("e2e")]),                     # green, not admitted: no merge ask
    pr(4, labels=["qa-failed-rework"], rollup=[RED("test")]),  # slot rework wins over red
    pr(5, labels=["pm-blocked:infra"], rollup=[OK("test")]),   # held
])
def test_classification_that_never_reads_details_needs_no_read(listed):
    needs, row = touched(listed)
    assert needs is False and not row["state"].startswith("unknown")


@pytest.mark.parametrize("listed", [
    pr(6, rollup=[RED("test")]),                               # red: 12b hold reads comments
    pr(7, labels=["ci-head:" + "c" * 40]),                     # stale admission: body, files, comments
    pr(8),                                                     # not admitted, no checks
    pr(9, labels=["ci-head:" + HEAD], rollup=[OK("test"), OK("e2e")]),  # admitted green: merge ask
])
def test_classification_that_reads_details_gets_a_per_pr_read(listed):
    assert touched(listed)[0] is True


def test_draft_behavior_is_unchanged_including_advisory_red():
    needs, row = touched(pr(10, draft=True, rollup=[RED("lint")]))
    assert needs is False
    assert row["state"] == "draft (advisory red: lint)" and row["next"] == "slot rework (draft)"


def test_details_are_read_once_per_head_and_failures_are_not_cached():
    body = '{"headRefOid": "%s", "files": [{"path": "docs/a.md"}], "body": "b", "comments": []}' % HEAD
    one = pr(11)
    with patch.object(snapshot.subprocess, "run", return_value=ok(body)) as run:
        snapshot._attach_details([one])
        snapshot._attach_details([pr(11)])
    assert run.call_count == 1 and one["files"] == [{"path": "docs/a.md"}]
    assert "statusCheckRollup" not in snapshot.DETAIL_FIELDS
    failed = pr(12)
    with patch.object(snapshot.subprocess, "run", side_effect=TimeoutExpired("gh", 25)) as run, \
         patch.object(snapshot.time, "sleep"):
        snapshot._attach_details([failed])
    assert run.call_count == snapshot.DETAIL_ATTEMPTS and failed["unknownReason"]
    later = pr(12)
    with patch.object(snapshot.subprocess, "run", return_value=ok(body)):
        snapshot._attach_details([later])
    assert "unknownReason" not in later  # the failure was not memoized


def test_prs_that_already_carry_details_are_not_read_again():
    complete = {**pr(40), "files": [], "body": "", "comments": []}  # e.g. an older cached payload
    with patch.object(snapshot.subprocess, "run") as run:
        snapshot._attach_details([complete])
    run.assert_not_called()
    assert "unknownReason" not in complete


def test_details_for_a_moved_head_fail_closed():
    moved = pr(13, head="b" * 40)  # the list saw b..., the view returns a...
    body = '{"headRefOid": "%s", "files": [], "body": "", "comments": []}' % HEAD
    with patch.object(snapshot.subprocess, "run", return_value=ok(body)):
        snapshot._attach_details([moved])
    assert moved["unknownReason"]


def test_unknown_reason_is_an_honest_row_for_non_drafts_only():
    row = snapshot.classify_pr({**pr(14), "unknownReason": "PR details unavailable"})
    assert row["state"] == "unknown: PR details unavailable" and row["actions"] == []
    draft = snapshot.classify_pr({**pr(15, draft=True), "unknownReason": "collector budget exhausted"})
    assert draft["state"] == "draft"


def test_partial_rows_keep_final_rows_and_mark_the_rest_unknown_without_reads():
    done = {1: {"number": 1, "state": "CI running", "next": "await terminal", "actions": []}}
    with patch.object(snapshot.subprocess, "run") as run:
        rows = snapshot._partial_rows([pr(1), pr(2), pr(3, draft=True)], done)
    run.assert_not_called()
    by = {r["number"]: r for r in rows}
    assert by[1]["state"] == "CI running"
    assert by[2]["state"] == "unknown: collector budget exhausted" and by[2]["next"] == "rerun snapshot"
    assert by[3]["state"] == "draft"  # a draft classifies exactly from the list
    assert [r["number"] for r in rows] == [3, 2, 1]


def test_main_reads_details_only_for_prs_whose_classification_needs_them(capsys):
    listed = [pr(20, draft=True), pr(21, rollup=[RUNNING("test")]), pr(22)]
    read = []

    def attach(prs):
        read.extend(p["number"] for p in prs)
        for p in prs:
            p.update(files=[], body="", comments=[])

    with patch.object(sys, "argv", ["pr-open-snapshot.py"]), \
         patch.object(snapshot, "fetch_mop_owned_prs", return_value=set()), \
         patch.object(snapshot, "fetch_open_prs", return_value=listed), \
         patch.object(snapshot, "fetch_live_claims", return_value={}), \
         patch.object(snapshot, "_existing_obligation", return_value=None), \
         patch.object(snapshot, "_attach_details", side_effect=attach), \
         patch.object(snapshot, "_prewarm_12b_verdicts"):
        assert snapshot.main() == 0
    assert read == [22]  # not the draft, not the CI-running PR
    assert '"number": 22' in capsys.readouterr().out


def test_main_reports_unknown_when_the_12b_verdict_read_timed_out(capsys):
    def timed_out(*args, **kwargs):
        snapshot._note_verdict_timeout()  # what a refused or timed-out verdict read does
        return None

    with patch.object(sys, "argv", ["pr-open-snapshot.py"]), \
         patch.object(snapshot, "fetch_mop_owned_prs", return_value=set()), \
         patch.object(snapshot, "fetch_open_prs", return_value=[pr(30)]), \
         patch.object(snapshot, "fetch_live_claims", return_value={}), \
         patch.object(snapshot, "_existing_obligation", return_value=None), \
         patch.object(snapshot, "admission_hold", side_effect=timed_out), \
         patch.object(snapshot, "_attach_details", side_effect=lambda prs: [p.update(files=[], body="", comments=[]) for p in prs]), \
         patch.object(snapshot, "_prewarm_12b_verdicts"):
        assert snapshot.main() == 0
    out = capsys.readouterr().out
    assert "unknown: 12b verdict timed out" in out and "PM 12b review" not in out


def test_reads_stop_when_the_run_deadline_has_passed():
    snapshot._RUN_STARTED = snapshot.time.monotonic() - 10_000
    with patch.object(snapshot.subprocess, "run") as run:
        value, error = snapshot._run_gh_json(["gh", "x"], timeout=20, attempts=3, until=45)
        with pytest.raises(TimeoutError):
            snapshot._deadline_guard(lambda: 1)()
    assert value is None and "budget exhausted" in error
    run.assert_not_called()
