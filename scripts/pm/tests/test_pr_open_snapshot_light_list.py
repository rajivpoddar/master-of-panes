"""Light bulk PR list, conditional memoized per-PR details, bounded retry under one deadline.

The single `gh pr list --json ...statusCheckRollup,files,body,comments` read for
every open PR timed out 3 x 60s and killed the collector. The bulk list is light;
heavy fields are read per non-draft PR, once per (number, head, updatedAt).
"""
import importlib.util
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from unittest.mock import patch

import pytest

PATH = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts/pm/heartbeat/pr-open-snapshot.py"
spec = importlib.util.spec_from_file_location("snapshot_light_list", PATH)
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)

HEAD = "a" * 40
DETAILS = '{"headRefOid": "%s", "statusCheckRollup": [], "files": [{"path": "docs/a.md"}], "body": "b", "comments": []}' % HEAD


@pytest.fixture(autouse=True)
def fresh_state():
    snapshot._RUN_STARTED = None
    snapshot._read_pr_details = snapshot._memoize(snapshot._read_pr_details_uncached)
    yield
    snapshot._RUN_STARTED = None


def ok(stdout):
    return CompletedProcess([], 0, stdout=stdout, stderr="")


def test_list_is_light_and_drafts_skip_details():
    prs = '[{"number": 1, "headRefOid": "%s", "isDraft": true, "labels": []}]' % HEAD
    with patch.object(snapshot.subprocess, "run", return_value=ok(prs)) as run, \
         patch.object(snapshot, "_read_ci_reconciliation", return_value=None):
        rows = snapshot._fetch_open_prs_live(with_threads=False)
    assert run.call_count == 1  # the list only; no per-PR read for a draft
    fields = run.call_args.args[0][run.call_args.args[0].index("--json") + 1].split(",")
    assert not {"statusCheckRollup", "files", "body", "comments"} & set(fields)
    assert "statusCheckRollup" not in rows[0] and not rows[0].get("detailsUnavailable")


def test_non_draft_details_are_read_once_and_memoized():
    pr = {"number": 2, "headRefOid": HEAD, "isDraft": False, "updatedAt": "t1"}
    with patch.object(snapshot.subprocess, "run", return_value=ok(DETAILS)) as run:
        snapshot._attach_details([dict(pr)])
        again = dict(pr)
        snapshot._attach_details([again])
    assert run.call_count == 1  # identical (number, head, updatedAt) is one read
    assert again["files"] == [{"path": "docs/a.md"}] and not again.get("detailsUnavailable")
    with patch.object(snapshot.subprocess, "run", return_value=ok(DETAILS)) as run:
        snapshot._attach_details([{**pr, "updatedAt": "t2"}])
    assert run.call_count == 1  # a changed updatedAt is a miss


def test_details_failure_is_unknown_not_empty_and_not_cached():
    pr = {"number": 3, "headRefOid": HEAD, "isDraft": False, "labels": [], "title": "t"}
    with patch.object(snapshot.subprocess, "run", side_effect=TimeoutExpired("gh", 25)) as run, \
         patch.object(snapshot.time, "sleep"):
        snapshot._attach_details([pr])
    assert run.call_count == snapshot.DETAIL_ATTEMPTS
    assert pr["detailsUnavailable"] is True and "statusCheckRollup" not in pr
    row = snapshot.classify_pr(pr)
    assert row["state"] == "unknown: PR details unavailable" and row["actions"] == []
    later = dict(pr)
    del later["detailsUnavailable"]
    with patch.object(snapshot.subprocess, "run", return_value=ok(DETAILS)):
        snapshot._attach_details([later])
    assert not later.get("detailsUnavailable")  # the failure was not memoized


def test_details_for_a_moved_head_fail_closed():
    pr = {"number": 4, "headRefOid": "b" * 40, "isDraft": False}  # list saw b..., view returns a...
    with patch.object(snapshot.subprocess, "run", return_value=ok(DETAILS)):
        snapshot._attach_details([pr])
    assert pr["detailsUnavailable"] is True


def test_reads_stop_when_the_run_deadline_has_passed():
    snapshot._RUN_STARTED = snapshot.time.monotonic() - 10_000
    with patch.object(snapshot.subprocess, "run") as run:
        value, error = snapshot._run_gh_json(["gh", "x"], timeout=20, attempts=3, until=45)
        with pytest.raises(TimeoutError):
            snapshot._deadline_guard(lambda: 1)()
    assert value is None and "budget exhausted" in error
    run.assert_not_called()
