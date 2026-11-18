"""Per-run batching of identical 12b gate reads in pr-open-snapshot.

The hourly collector was re-walking main-merge carry-forward once per PR per
phase (gh compare, commits/main, git rev-parse). Successful identical reads
collapse; failures stay uncached so a later call can retry.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts/pm/heartbeat"


def _load():
    spec = importlib.util.spec_from_file_location("pr_open_snapshot_read_memo", HERE / "pr-open-snapshot.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


prsnap = _load()


def test_memoize_batches_identical_success_and_retries_failure():
    calls = {"n": 0}

    def fetch(kind):
        calls["n"] += 1
        if kind == "bad":
            raise RuntimeError("transient")
        return {"kind": kind}

    batched = prsnap._memoize(fetch)
    assert batched("ok") == {"kind": "ok"}
    assert batched("ok") == {"kind": "ok"}
    assert calls["n"] == 1
    for _ in range(2):
        try:
            batched("bad")
        except RuntimeError:
            pass
        else:
            raise AssertionError("failure must propagate")
    assert calls["n"] == 3


def test_verdict_cache_follows_comment_edits_and_collapses_repeats():
    calls = {"n": 0}
    head = "d" * 40

    def fake_verdict(comments, reviewed_head, login, repo, number):
        calls["n"] += 1
        body = (comments or [{}])[0].get("body") or ""
        return "PASS" if "PASS" in body and reviewed_head == head else None

    prsnap._REVIEW_GATE.reviewed_12b_verdict = fake_verdict
    prsnap._VERDICT_CACHE.clear()
    pr = {"number": 221, "headRefOid": head, "comments": [{"body": "no marker", "author": {"login": "pm-reviewer"}}]}
    assert prsnap._latest_12b_verdict(pr, head, "pm-reviewer") is None
    assert prsnap._latest_12b_verdict(pr, head, "pm-reviewer") is None
    assert calls["n"] == 1
    pr["comments"] = [{"body": f"PM 12b: PASS at {head}", "author": {"login": "pm-reviewer"}}]
    assert prsnap._latest_12b_verdict(pr, head, "pm-reviewer") == "PASS"
    assert prsnap._latest_12b_verdict(pr, head, "pm-reviewer") == "PASS"
    assert calls["n"] == 2


def test_reviewer_login_caches_only_a_successful_read(monkeypatch):
    prsnap._REVIEWER_LOGIN.clear()
    answers = iter(["", "pm-reviewer", "other"])

    def fake():
        return next(answers)

    monkeypatch.setattr(prsnap, "_reviewer_login_impl", fake)
    assert prsnap._authorized_12b_reviewer() == ""
    assert prsnap._authorized_12b_reviewer() == "pm-reviewer"
    assert prsnap._authorized_12b_reviewer() == "pm-reviewer"
