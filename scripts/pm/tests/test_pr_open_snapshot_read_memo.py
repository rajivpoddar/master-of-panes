"""Per-run batching of identical 12b gate reads in pr-open-snapshot.

The hourly collector was re-walking main-merge carry-forward once per PR per
phase (gh compare, commits/main, git rev-parse). Successful identical reads
and definite verdicts collapse. Unresolved lookups (None) and indeterminate
verdicts stay uncached so a later call in the same run can retry.
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
_REAL_VERDICT = prsnap._REVIEW_GATE.reviewed_12b_verdict
_REAL_RESOLVE = prsnap._REVIEW_GATE.resolve_commit


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

    lookups = {"n": 0}

    def lookup(sha):
        lookups["n"] += 1
        if lookups["n"] == 1:
            return None
        if sha == "no":
            return False
        return sha

    batched_lookup = prsnap._memoize(lookup)
    assert batched_lookup("abc") is None
    assert batched_lookup("abc") == "abc"
    assert batched_lookup("abc") == "abc"
    assert lookups["n"] == 2
    assert batched_lookup("no") is False
    assert batched_lookup("no") is False
    assert lookups["n"] == 3


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
    # Indeterminate None is not cached, so the second call retries.
    assert calls["n"] == 2
    pr["comments"] = [{"body": f"PM 12b: PASS at {head}", "author": {"login": "pm-reviewer"}}]
    assert prsnap._latest_12b_verdict(pr, head, "pm-reviewer") == "PASS"
    assert prsnap._latest_12b_verdict(pr, head, "pm-reviewer") == "PASS"
    assert calls["n"] == 3


def test_reviewer_login_caches_only_a_successful_read(monkeypatch):
    prsnap._REVIEWER_LOGIN.clear()
    answers = iter(["", "pm-reviewer", "other"])

    def fake():
        return next(answers)

    monkeypatch.setattr(prsnap, "_reviewer_login_impl", fake)
    assert prsnap._authorized_12b_reviewer() == ""
    assert prsnap._authorized_12b_reviewer() == "pm-reviewer"
    assert prsnap._authorized_12b_reviewer() == "pm-reviewer"


def test_unavailable_lookup_then_same_head_block_suppresses_merge_ask():
    """Real 12b gate. First commit lookup is unavailable; the None verdict is
    not cached. The same head then resolves to BLOCK and suppresses the
    merge-ready CTO merge ask. The definite BLOCK stays cached.
    """
    head = "ab" * 20
    calls = {"n": 0}

    def lookup(sha):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return str(sha).lower()

    prsnap._VERDICT_CACHE.clear()
    prsnap._REVIEW_GATE.reviewed_12b_verdict = _REAL_VERDICT
    prsnap._REVIEW_GATE.resolve_commit = prsnap._memoize(lookup)
    pr = {
        "number": 14080,
        "headRefOid": head,
        "body": "ready",
        "_authorized_12b_reviewer": "pm-reviewer",
        "comments": [{
            "body": f"PM 12b: BLOCK at {head}",
            "user": {"login": "pm-reviewer"},
            "created_at": "2026-11-18T00:00:00Z",
            "id": 1,
        }],
        "ci_reconciliation": {
            "pr": 14080,
            "head_sha": head,
            "status": "resolved",
            "resolution": "merge_ready",
        },
    }
    try:
        assert prsnap.merge_ask_next(pr) == "CTO merge ask"
        assert calls["n"] == 2
        assert prsnap.merge_ask_next(pr) == prsnap.REWORK_QUEUED
        before = calls["n"]
        assert prsnap.merge_ask_next(pr) == prsnap.REWORK_QUEUED
        assert calls["n"] == before
    finally:
        prsnap._REVIEW_GATE.reviewed_12b_verdict = _REAL_VERDICT
        prsnap._REVIEW_GATE.resolve_commit = _REAL_RESOLVE
        prsnap._VERDICT_CACHE.clear()
