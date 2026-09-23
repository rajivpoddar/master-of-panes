#!/usr/bin/env python3
"""Regression proof that the off-slot review-verdict gate is gone from merge.py.

Rajiv directive Ev0C3RLNGPJS (2026-09-23): the Codex bot already reviews a PR
when it is marked ready, so the extra off-slot exact-head review-verdict gate is
not required and must not be replaced by another marker/review gate. This file
replaces the former gate-acceptance contract: it proves the gate is removed and
that every retained refusal is unchanged.

Hermetic: merge.api / merge.pages / merge.command are stubbed and no review or
marker lookup is permitted. No subprocess, network, GitHub, file, or slot
effect. Set MERGE_UNDER_TEST to load a different merge.py (RED witness).
"""

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MERGE_PATH = os.environ.get("MERGE_UNDER_TEST") or os.path.join(HERE, "merge.py")
spec = importlib.util.spec_from_file_location("merge_under_test", MERGE_PATH)
merge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(merge)

HEAD = "a" * 40
OTHER = "b" * 40
MAIN = "c" * 40
NUMBER = 8122
REF = "fix/8122"
PENDING = ("pm-state:pm-review-pending",)
RUN_IDS = {"ci.yml": 4242, "e2e.yml": 4243}
REQUIRED_STEPS = {
    "ci.yml": {"typescript": ("Unit tests",), "python": (), "test": ()},
    "e2e.yml": {"e2e": ("Run E2E auto-process-critical",
                        "Run E2E core-rest shard 1/2",
                        "Run E2E core-rest shard 2/2")},
}


def make_pr(head=HEAD, labels=PENDING, merged=False):
    pr = {"number": NUMBER,
          "head": {"sha": head, "ref": REF, "repo": {"full_name": "heydonna-app/heydonna-app"}},
          "state": "open", "draft": False, "base": {"ref": "main"},
          "labels": [{"name": name} for name in labels]}
    if merged:
        pr["merged"] = True
        pr["merge_commit_sha"] = "d" * 40
    return pr


def workflow_run(workflow, conclusion):
    return {"id": RUN_IDS[workflow], "run_attempt": 1, "head_sha": HEAD,
            "event": "pull_request", "head_branch": REF,
            "path": f".github/workflows/{workflow}", "status": "completed",
            "conclusion": conclusion}


def workflow_jobs(workflow, conclusion, without_steps):
    jobs = []
    for name, step_names in REQUIRED_STEPS[workflow].items():
        steps = [] if without_steps else [
            {"name": step, "status": "completed", "conclusion": "success"} for step in step_names]
        jobs.append({"id": 100 + len(jobs), "name": name, "status": "completed",
                     "conclusion": conclusion, "steps": steps})
    return jobs


class Stub:
    def __init__(self, world):
        self.world = world
        self.calls = []
        self.pr = make_pr(head=OTHER if world.get("head_drift") else HEAD,
                          labels=world.get("labels", PENDING))

    def api(self, path):
        self.calls.append(path)
        world = self.world
        if path == f"pulls/{NUMBER}":
            return dict(self.pr)
        if path == "git/ref/heads/main":
            return {"object": {"sha": world.get("main", MAIN)}}
        if path.startswith("compare/"):
            return {"merge_base_commit": {"sha": world.get("merge_base", world.get("main", MAIN))},
                    "html_url": "https://example.invalid/compare",
                    "files": world.get("files", [])}
        raise AssertionError(f"unexpected api path: {path}")

    def pages(self, path, key):
        self.calls.append(path)
        world = self.world
        if "/runs?" in path:
            workflow = "ci.yml" if "/workflows/ci.yml/" in path else "e2e.yml"
            if workflow in world.get("missing_workflows", ()):
                return []
            return [workflow_run(workflow, world.get("conclusions", {}).get(workflow, "success"))]
        if "/jobs?" in path:
            run_id = int(path.split("actions/runs/")[1].split("/")[0])
            workflow = next(name for name, value in RUN_IDS.items() if value == run_id)
            return workflow_jobs(workflow, world.get("conclusions", {}).get(workflow, "success"),
                                 workflow in world.get("without_steps", ()))
        raise AssertionError(f"unexpected pages path: {path}")

    def command(self, *args):
        self.calls.append(" ".join(str(arg) for arg in args))
        if args[:3] != ("gh", "pr", "merge"):
            raise AssertionError(f"unexpected command: {args}")
        if self.world.get("merge_fails"):
            raise merge.Refusal("merge command failed")
        self.pr = make_pr(labels=self.world.get("labels", PENDING), merged=True)
        return ""


def install(world=None):
    world = {} if world is None else world
    stub = Stub(world)
    merge.api, merge.pages, merge.command = stub.api, stub.pages, stub.command
    return stub


RESULTS = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - harness reports
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


def t1_offslot_pending_label_green_pair_is_not_blocked():
    stub = install()
    out = merge.merge(NUMBER, HEAD)
    assert out["status"] == "READY_TO_MERGE", out
    assert "review" not in out, out
    assert not [call for call in stub.calls if "/reviews" in call or "marker" in call.lower()], stub.calls


def t2_offslot_pending_label_applies_the_head_pinned_merge():
    stub = install()
    out = merge.merge(NUMBER, HEAD, apply=True)
    assert out["status"] == "MERGED", out
    assert any(call.startswith("gh pr merge") and "--match-head-commit " + HEAD in call
               for call in stub.calls), stub.calls


def t3_red_required_workflow_still_refuses():
    for workflow in ("ci.yml", "e2e.yml"):
        install({"conclusions": {workflow: "failure"}})
        expect_refusal(lambda: merge.merge(NUMBER, HEAD), "WORKFLOW_NOT_GREEN")


def t4_missing_required_workflow_still_refuses():
    for workflow in ("ci.yml", "e2e.yml"):
        install({"missing_workflows": (workflow,)})
        expect_refusal(lambda: merge.merge(NUMBER, HEAD), "WORKFLOW_MISSING")


def t5_head_drift_still_refuses():
    install({"head_drift": True})
    expect_refusal(lambda: merge.merge(NUMBER, HEAD), "HEAD_CHANGED")


def t6_relevant_later_main_movement_still_refuses():
    world = {"main": MAIN, "merge_base": "e" * 40,
             "files": [{"filename": "convex/workspaces.ts", "status": "modified"}]}
    install(world)
    assert merge.merge(NUMBER, HEAD)["status"] == "MAIN_DELTA_REVIEW_REQUIRED"
    install(dict(world))
    expect_refusal(lambda: merge.merge(NUMBER, HEAD, apply=True), "MAIN_DELTA_REVIEW_REQUIRED")


def t7_missing_test_step_execution_still_refuses():
    for workflow in ("ci.yml", "e2e.yml"):
        install({"without_steps": (workflow,)})
        expect_refusal(lambda: merge.merge(NUMBER, HEAD), "TEST_EXECUTION_MISSING")


def t8_review_gate_symbols_and_marker_lookup_are_gone():
    for name in ("review_gate", "validate_review_marker", "read_review_marker",
                 "marker_path", "exact_head_github_approval", "has_slot_label",
                 "pr_labels", "REVIEW_PENDING_LABEL", "MARKER_PREFIX", "MARKER_DIR",
                 "REVIEWER_ASSOCIATIONS"):
        assert not hasattr(merge, name), name
    source = open(MERGE_PATH, encoding="utf-8").read()
    for token in ("OFFSLOT_REVIEW", "pm-review-pending", "codex-app-code-review"):
        assert token not in source, token


for name, fn in sorted([(k, v) for k, v in list(globals().items())
                        if k.startswith("t") and callable(v)]):
    check(name, fn)

failed = [result for result in RESULTS if not result[1]]
for name, ok, detail in RESULTS:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else " :: " + detail))
print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
sys.exit(1 if failed else 0)
