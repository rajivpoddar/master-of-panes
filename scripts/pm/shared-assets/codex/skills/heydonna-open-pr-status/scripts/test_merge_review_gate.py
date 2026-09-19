#!/usr/bin/env python3
"""Focused contract tests for the off-slot review-verdict gate in merge.py.

Hermetic: merge.api / merge.pages / merge.read_review_marker are stubbed.
No subprocess, network, GitHub, file, or slot effect.
"""

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("merge_under_test", os.path.join(HERE, "merge.py"))
merge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(merge)

HEAD = "a" * 40
OTHER = "b" * 40
MAIN = "c" * 40


def make_pr(number=7922, head=HEAD, labels=(), merged=False):
    pr = {"number": number, "head": {"sha": head, "ref": "fix/x",
          "repo": {"full_name": "heydonna-app/heydonna-app"}},
          "state": "open", "draft": False, "base": {"ref": "main"},
          "labels": [{"name": name} for name in labels]}
    if merged:
        pr["merged"] = True
        pr["merge_commit_sha"] = "d" * 40
    return pr


def genuine_marker(number=7922, head=HEAD):
    return "\n".join([
        "VERDICT: APPROVE",
        "COMPANION_VERDICT: APPROVE",
        "FINAL_REVIEWER_VERDICT: APPROVE",
        "MARKER_PROVENANCE: codex-review-companion",
        "TYPE: code-review",
        "TIMESTAMP: 1789808023",
        "ISSUE: #7914",
        f"PR: #{number}",
        f"HEAD_SHA: {head}",
        f"headRefOid: {head}",
        "--- Blockers (0) ---",
        "--- Findings (0) ---",
        "--- Review Output ---",
        "VERDICT: APPROVE",
        "",
    ])


class Stub:
    def __init__(self, pr, reviews=(), marker=None):
        self.calls = []
        self.pr = pr
        self.reviews = reviews
        self.marker = marker

    def api(self, path):
        self.calls.append(path)
        if path == f"pulls/{self.pr['number']}":
            return self.pr
        if path == f"pulls/{self.pr['number']}/reviews":
            return list(self.reviews)
        raise AssertionError(f"unexpected api path: {path}")

    def pages(self, path, key):
        self.calls.append("PAGES:" + path)
        raise AssertionError("workflow proof must not run before the review gate")

    def read_marker(self, number):
        return self.marker


def run_gate(pr, reviews=(), marker=None):
    stub = Stub(pr, reviews, marker)
    merge.api = stub.api
    merge.pages = stub.pages
    merge.read_review_marker = stub.read_marker
    return stub, merge.review_gate(pr, HEAD)


RESULTS = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - test harness reports
        RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
    else:
        RESULTS.append((name, True, ""))


def expect_refusal(fn, fragment):
    try:
        fn()
    except merge.Refusal as exc:
        assert fragment in str(exc), f"want {fragment!r} in {exc}"
        return str(exc)
    raise AssertionError(f"expected Refusal containing {fragment!r}")


PENDING = ("pm-state:pm-review-pending",)


def t1_missing_verdict_refuses_zero_workflow():
    pr = make_pr(labels=PENDING)
    stub = Stub(pr, [], None)
    merge.api = stub.api
    merge.pages = stub.pages
    merge.read_review_marker = stub.read_marker
    try:
        merge.merge(7922, HEAD)
    except merge.Refusal as exc:
        assert "OFFSLOT_REVIEW_VERDICT_MISSING" in str(exc), exc
    else:
        raise AssertionError("expected OFFSLOT_REVIEW_VERDICT_MISSING")
    assert not any(c.startswith("PAGES:") for c in stub.calls), stub.calls
    assert not any("actions/workflows" in c for c in stub.calls), stub.calls


def t2_stale_head_marker_refuses():
    pr = make_pr(labels=PENDING)
    run_gate(pr, [], genuine_marker(head=OTHER))


def t3_github_approve_exact_head_allows():
    pr = make_pr(labels=PENDING)
    reviews = [{"id": 1, "state": "APPROVED", "commit_id": HEAD,
                "author_association": "OWNER", "user": {"login": "rajivpoddar"}}]
    stub, gate = run_gate(pr, reviews, None)
    assert gate["review"] == "VERDICT_OK" and gate["source"] == "github", gate


def t4_marker_approve_exact_head_allows():
    pr = make_pr(labels=PENDING)
    stub, gate = run_gate(pr, [], genuine_marker())
    assert gate["review"] == "VERDICT_OK" and gate["source"] == "marker", gate
    assert gate["head_sha"] == HEAD, gate


def t5_prose_only_marker_refuses():
    pr = make_pr(labels=PENDING)
    marker = "VERDICT: APPROVE\nLooks good, ship it.\n"
    expect_refusal(lambda: run_gate(pr, [], marker), "OFFSLOT_REVIEW_MARKER_INVALID")


def t5b_unrelated_approver_falls_through_to_missing():
    pr = make_pr(labels=PENDING)
    reviews = [{"id": 2, "state": "APPROVED", "commit_id": HEAD,
                "author_association": "NONE", "user": {"login": "chatgpt-codex-connector"}}]
    expect_refusal(lambda: run_gate(pr, reviews, None), "OFFSLOT_REVIEW_VERDICT_MISSING")


def t5c_forged_timestamp_refuses():
    pr = make_pr(labels=PENDING)
    marker = genuine_marker().replace("TIMESTAMP: 1789808023", "TIMESTAMP: $(date +%s)")
    expect_refusal(lambda: run_gate(pr, [], marker), "OFFSLOT_REVIEW_MARKER_INVALID")


def t5d_open_blockers_refuse():
    pr = make_pr(labels=PENDING)
    marker = genuine_marker().replace("--- Blockers (0) ---",
                                      "--- Blockers (1) ---\nBLOCKER_STATUS: OPEN")
    expect_refusal(lambda: run_gate(pr, [], marker), "OFFSLOT_REVIEW_MARKER_INVALID")


def t6_slot_origin_skipped_no_review_fetch():
    pr = make_pr(labels=("slot:2",) + PENDING)
    stub, gate = run_gate(pr, [{"id": 9}], genuine_marker())
    assert gate == {"review": "SKIPPED_SLOT_ORIGIN"}, gate
    assert not any("reviews" in c for c in stub.calls), stub.calls


def t7_pending_absent_inert():
    pr = make_pr(labels=("pm-state:qa-passed-awaiting-ci",))
    stub, gate = run_gate(pr, [], None)
    assert gate == {"review": "INERT_PENDING_LABEL_ABSENT"}, gate
    assert stub.calls == [], stub.calls


def t8_already_merged_idempotent_no_gate():
    pr = make_pr(labels=PENDING, merged=True)
    stub = Stub(pr, [], None)
    merge.api = stub.api
    out = merge.merge(7922, HEAD)
    assert out["status"] == "ALREADY_MERGED", out
    assert "review" not in out, out
    assert not any("reviews" in c for c in stub.calls), stub.calls


for name, fn in sorted([(k, v) for k, v in list(globals().items()) if k.startswith("t") and callable(v)]):
    check(name, fn)

# t2 expects a refusal; wrap it explicitly
RESULTS = [r for r in RESULTS if r[0] != "t2_stale_head_marker_refuses"]
check("t2_stale_head_marker_refuses",
      lambda: expect_refusal(
          lambda: run_gate(make_pr(labels=PENDING), [], genuine_marker(head=OTHER)),
          "OFFSLOT_REVIEW_MARKER_INVALID"))

failed = [r for r in RESULTS if not r[1]]
for name, ok, detail in RESULTS:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else " :: " + detail))
print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
sys.exit(1 if failed else 0)
