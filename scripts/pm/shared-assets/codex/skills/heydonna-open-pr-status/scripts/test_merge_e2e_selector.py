"""Replay #12531's native E2E pair through merge(), without live effects.

CI receipt authentication has its own owning suite. Here its already-verified
required set is supplied at that seam; the E2E selector/guards run unchanged.
"""
import copy
import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

HERE = Path(__file__).parent
NATIVE = json.loads((HERE / "fixtures/e2e-selector-12531-native.json").read_text())
HEAD = NATIVE["pr"]["head"]["sha"]
SUBSTANTIVE = 41394794842
WRAPPER = 41394795537


class World:
    def __init__(self):
        spec = importlib.util.spec_from_file_location(
            "selector_guard", os.environ.get("MERGE_UNDER_TEST", str(HERE / "merge.py")))
        self.guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.guard)
        self.pr = dict(copy.deepcopy(NATIVE["pr"]), state="open", draft=False,
                       base={"ref": "main"})
        self.pr["head"]["repo"] = {"full_name": "Scribie/heydonna-app"}
        self.runs = copy.deepcopy(NATIVE["runs"])
        self.jobs = copy.deepcopy(NATIVE["jobs"])
        self.required = False
        self.reads = []

    def api(self, path):
        if path == "pulls/12531":
            return self.pr
        if path == "git/ref/heads/main":
            return {"object": {"sha": HEAD}}
        if path.startswith("compare/"):
            return {"merge_base_commit": {"sha": HEAD}}
        raise AssertionError(f"Unexpected API: {path}")

    def pages(self, path, key):
        self.reads.append(path)
        if "workflows/e2e.yml/runs?" in path:
            return self.runs
        if path.startswith("actions/runs/") and "/jobs?filter=latest&" in path:
            return self.jobs[path.split("/")[2]]
        raise AssertionError(f"Unexpected pages: {path}")

    def evaluate(self):
        guard = self.guard
        real_workflow_proof = guard.workflow_proof

        def workflow_proof(pr, workflow, names):
            if workflow == "ci.yml":
                return {"run": 41394794962, "attempt": 1, "head": HEAD,
                        "classifier_required_set": {
                            "head": HEAD, "e2e_required": self.required,
                            "ts_changed": True, "python_changed": False}}
            return real_workflow_proof(pr, workflow, names)

        with patch.object(guard, "api", self.api), patch.object(guard, "pages", self.pages), \
                patch.object(guard, "workflow_proof", workflow_proof), \
                patch.object(guard, "site_classifier_exemption", return_value=None), \
                patch.object(guard, "control_plane_classifier_exemption", return_value=None), \
                patch.object(guard, "classifier_exemption", return_value=None), \
                patch.object(guard, "workflow_requirements", side_effect=AssertionError("Policy fallback")), \
                patch.object(guard, "command", side_effect=AssertionError("Live command")):
            return guard.merge(12531, HEAD)

    def refuse(self):
        with pytest.raises(self.guard.Refusal):
            self.evaluate()

    def newest(self):
        return next(r for r in self.runs if r["id"] == WRAPPER)

    def classifier(self, run_id=WRAPPER):
        return next(j for j in self.jobs[str(run_id)] if j["name"] == "classify-change-scope")


def test_exact_native_pair_selects_executed_classifier_not_label_wrapper():
    w = World()
    result = w.evaluate()
    assert result["status"] == "READY_TO_MERGE"
    proof = result["workflows"]["e2e.yml"]
    assert (proof["run"], proof["attempt"], proof["head"], proof["skipped"]) == (
        SUBSTANTIVE, 1, HEAD, True)


@pytest.mark.parametrize("case", ["empty", "only_skipped", "missing_classifier", "no_classify_step"])
def test_no_executed_classifier_refuses(case):
    w = World()
    if case == "empty":
        w.runs = []
    elif case == "only_skipped":
        w.runs = [w.newest()]
    elif case == "missing_classifier":
        w.jobs[str(SUBSTANTIVE)].remove(w.classifier(SUBSTANTIVE))
    else:
        w.classifier(SUBSTANTIVE)["steps"] = []
    w.refuse()


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "neutral", None])
def test_newest_executed_classifier_failure_or_inconclusive_never_falls_back(conclusion):
    w = World()
    w.newest().update(conclusion=conclusion, status="completed" if conclusion else "in_progress")
    w.classifier().update(conclusion=conclusion, status=w.newest()["status"],
                          steps=copy.deepcopy(w.classifier(SUBSTANTIVE)["steps"]))
    w.refuse()
    assert not any(f"/{SUBSTANTIVE}/jobs?" in p for p in w.reads)


@pytest.mark.parametrize("field,value", [
    ("head_sha", "f" * 40), ("head_branch", "other"), ("event", "push"),
    ("path", ".github/workflows/other.yml"), ("pull_requests", [{"number": 999}])])
def test_foreign_newer_run_excluded(field, value):
    w = World()
    w.newest()[field] = value
    assert w.evaluate()["workflows"]["e2e.yml"]["run"] == SUBSTANTIVE
    assert not any(f"/{WRAPPER}/jobs?" in p for p in w.reads)


@pytest.mark.parametrize("case", ["old_attempt", "wrong_run", "wrong_head", "duplicate_scope",
                                 "partial_wrapper", "wrapper_infra_red", "pending_classifier"])
def test_ambiguous_or_unbound_wrapper_cannot_be_ignored(case):
    w = World()
    scope = w.classifier()
    if case == "old_attempt": scope["run_attempt"] = 0
    elif case == "wrong_run": scope["run_id"] = SUBSTANTIVE
    elif case == "wrong_head": scope["head_sha"] = "f" * 40
    elif case == "duplicate_scope": w.jobs[str(WRAPPER)].append(copy.deepcopy(scope))
    elif case == "partial_wrapper": w.jobs[str(WRAPPER)][1]["conclusion"] = "success"
    elif case == "wrapper_infra_red": w.newest()["conclusion"] = "failure"
    elif case == "pending_classifier": scope.update(status="queued", conclusion=None)
    w.refuse()


@pytest.mark.parametrize("state", ["failure", "skipped", "cancelled", None])
def test_required_e2e_non_success_refuses(state):
    w = World()
    w.required = True
    run = next(r for r in w.runs if r["id"] == SUBSTANTIVE)
    run["conclusion"] = state
    next(j for j in w.jobs[str(SUBSTANTIVE)] if j["name"] == "e2e")["conclusion"] = state
    w.refuse()


def test_latest_attempt_failure_cannot_fall_back_to_old_attempt():
    w = World()
    w.runs = [r for r in w.runs if r["id"] == SUBSTANTIVE]
    latest = dict(w.runs[0], run_attempt=2, conclusion="failure")
    w.runs.append(latest)
    for job in w.jobs[str(SUBSTANTIVE)]:
        job["run_attempt"] = 2
    w.classifier(SUBSTANTIVE)["conclusion"] = "failure"
    w.refuse()


def test_successful_workflow_cannot_mask_failed_classifier():
    w = World()
    w.newest()["conclusion"] = "success"
    w.classifier().update(conclusion="failure", steps=copy.deepcopy(w.classifier(SUBSTANTIVE)["steps"]))
    w.refuse()


@pytest.mark.parametrize("state", ["failure", "skipped", "cancelled", None, "missing", "no_steps"])
def test_required_e2e_workflow_success_is_insufficient(state):
    w = World()
    w.required = True
    jobs = w.jobs[str(SUBSTANTIVE)]
    e2e = next(j for j in jobs if j["name"] == "e2e")
    if state == "missing":
        jobs.remove(e2e)
    else:
        e2e.update(conclusion="success" if state == "no_steps" else state,
                   status="in_progress" if state is None else "completed")
    w.refuse()


def test_required_e2e_still_accepts_only_executed_success():
    w = World()
    w.required = True
    e2e = next(j for j in w.jobs[str(SUBSTANTIVE)] if j["name"] == "e2e")
    e2e.update(conclusion="success", steps=[{
        "name": name, "status": "completed", "conclusion": "success"} for name in (
            "Run E2E auto-process-critical", "Run E2E core-rest shard 1/2",
            "Run E2E core-rest shard 2/2")])
    assert w.evaluate()["workflows"]["e2e.yml"]["run"] == SUBSTANTIVE


def test_repeated_readonly_selection_is_stable():
    w = World()
    assert w.evaluate() == w.evaluate()
