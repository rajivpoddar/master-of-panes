"""One verdict window over the whole 12b pass, local git included (added to the repo; not executed on the host).

CTO REVISE on 17e81e94 (C0ALZJHGE49 thread 1795022560.124969): the 130s cutoff guarded
the gh reads only. A late compare could use 60s and then start a separate 60s
`git merge-tree`, and the verdict worker pool waits for it, so the hourly run could hit
210s without emitting rows. Every read of the verdict pass (gh api, local git, reviewer
login) now shares one window, a timed-out verdict is reported unknown, and rows that
need no verdict still emit.
"""
import importlib.util
import time
from pathlib import Path
from subprocess import TimeoutExpired
from unittest.mock import patch

import pytest

PATH = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts/pm/heartbeat/pr-open-snapshot.py"
spec = importlib.util.spec_from_file_location("snapshot_verdict_deadline", PATH)
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)

HEAD = "c" * 40
PASS_MARKER = [{"body": "PM 12b: PASS at " + "d" * 40, "user": {"login": "pm"},
                "created_at": "2026-11-18T00:00:00Z", "id": 1}]
WINDOW_LEFT = 10  # seconds of the verdict window left when each case starts


def make_pr(number, rollup, draft=False):
    return {"number": number, "headRefOid": HEAD, "title": f"t{number}", "isDraft": draft, "labels": [],
            "statusCheckRollup": rollup, "files": [], "body": "", "comments": PASS_MARKER,
            "_authorized_12b_reviewer": "pm", "reviewThreads": [], "ci_reconciliation": None}


def slow_gate(comments, head, login, repo, number):
    """Stands in for the gate's carry-forward walk: its separate `git merge-tree` step."""
    snapshot._REVIEW_GATE.subprocess.check_output(
        ["git", "-C", "/x", "merge-tree", "--write-tree", "a", "b"], stderr=snapshot.subprocess.DEVNULL, timeout=60)
    return "PASS"


def stalled_git(calls):
    """A git that never answers: it runs to whatever timeout it was given, which spends that much of the window."""
    def fake(cmd, *args, **kwargs):
        calls.append(kwargs["timeout"])
        snapshot._RUN_STARTED -= kwargs["timeout"]
        raise TimeoutExpired(cmd, kwargs["timeout"])
    return fake


@pytest.fixture(autouse=True)
def window():
    original = snapshot._REVIEW_GATE.reviewed_12b_verdict
    snapshot._REVIEW_GATE.reviewed_12b_verdict = slow_gate
    snapshot._VERDICT_CACHE.clear()
    snapshot._RUN_STARTED = time.monotonic() - (snapshot.VERDICT_UNTIL_SECONDS - WINDOW_LEFT)
    yield
    snapshot._REVIEW_GATE.reviewed_12b_verdict = original
    snapshot._VERDICT_CACHE.clear()
    snapshot._RUN_STARTED = None


def test_stalled_git_is_clamped_to_the_window_and_no_later_read_starts():
    calls = []
    with patch.object(snapshot.subprocess, "check_output", stalled_git(calls)), \
         patch.object(snapshot, "_authorized_12b_reviewer", return_value="pm"):
        snapshot._prewarm_12b_verdicts([make_pr(1, [])])
        assert len(calls) == 1 and calls[0] <= WINDOW_LEFT  # the gate asked for 60s; the window allowed ~10s
        snapshot._prewarm_12b_verdicts([make_pr(n, []) for n in range(2, 8)])
    assert len(calls) == 1  # the window is closed: no git read started for the other six PRs
    assert time.monotonic() - snapshot._RUN_STARTED >= snapshot.VERDICT_UNTIL_SECONDS


def test_rows_still_emit_and_only_verdict_rows_are_unknown():
    needs_verdict = make_pr(10, [])  # no gate result yet: classify reads the 12b verdict
    ci_running = make_pr(11, [{"name": "test", "status": "IN_PROGRESS"}])  # needs no verdict
    draft = make_pr(12, [], draft=True)
    prs = [needs_verdict, ci_running, draft]
    calls = []
    with patch.object(snapshot.subprocess, "check_output", stalled_git(calls)), \
         patch.object(snapshot, "_authorized_12b_reviewer", return_value="pm"), \
         patch.object(snapshot, "_existing_obligation", return_value=None):
        holds = snapshot._admission_holds(prs)
        rows = {row["number"]: row for row in (snapshot.classify_pr(pr, set(), holds, {}) for pr in prs)}
    assert len(calls) == 1 and holds == {}
    assert rows[10]["state"] == "unknown: 12b verdict timed out" and rows[10]["actions"] == []
    assert rows[11]["state"] == "CI running"
    assert rows[12]["state"] == "draft"


def test_reviewer_login_read_is_inside_the_window():
    snapshot._RUN_STARTED = time.monotonic() - 10_000
    before = snapshot._verdict_timeouts()
    with patch.object(snapshot.subprocess, "run") as run:
        assert snapshot._reviewer_login_impl() == ""
    run.assert_not_called()
    assert snapshot._verdict_timeouts() == before + 1
