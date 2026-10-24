"""Required language families from an authentic classifier, with zero live effects."""
import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

HERE = Path(__file__).parent
FIXTURE = json.loads((HERE / "fixtures/required-jobs-native.json").read_text())
HEAD = FIXTURE["pr"]["head"]["sha"]
BASE = FIXTURE["bindings"]["BASE_SHA"]


def load():
    path = os.environ.get("MERGE_UNDER_TEST", str(HERE / "merge.py"))
    spec = importlib.util.spec_from_file_location("required_jobs_guard", path)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    return guard


class World:
    def __init__(self):
        self.pr = copy.deepcopy(FIXTURE["pr"])
        self.run = copy.deepcopy(FIXTURE["run"])
        self.jobs = copy.deepcopy(FIXTURE["jobs"])
        self.log = FIXTURE["log"]
        self.files = copy.deepcopy(FIXTURE["files"])
        self.parents = FIXTURE["parents"][:]
        self.blobs = copy.deepcopy(FIXTURE["blob_shas"])
        self.rules = FIXTURE["rules"]
        self.e2e = None
        self.e2e_jobs = []
        self.commands = []
        self.reads = []
        self.runs = None

    def receipt(self, **updates):
        lines = self.log.splitlines()
        obj = json.loads(lines[-1])
        obj.update(updates)
        self.log = "\n".join(lines[:-1] + [json.dumps(obj)]) + "\n"

    def job(self, name):
        return next(j for j in self.jobs if j["name"] == name)

    def api(self, path):
        self.reads.append(path)
        if path == "pulls/10992":
            return self.pr
        if path == "git/ref/heads/main":
            return {"object": {"sha": BASE}}
        if path.startswith("compare/"):
            return {"merge_base_commit": {"sha": BASE}}
        if path.startswith("git/commits/"):
            return {"parents": [{"sha": sha} for sha in self.parents]}
        if path in self.blobs:
            body = self.rules.encode() if "rules.json?" in path else b"unused blob body"
            return {"sha": self.blobs[path], "content": base64.b64encode(body).decode()}
        raise AssertionError(f"Unexpected API: {path}")

    def pages(self, path, key):
        self.reads.append(path)
        if "ci-dummy.yml/runs?" in path:
            return []
        if "ci.yml/runs?" in path:
            return self.runs if self.runs is not None else [self.run]
        if "e2e.yml/runs?" in path:
            return [self.e2e] if self.e2e else []
        if "/jobs?" in path:
            return self.jobs if str(self.run["id"]) in path else self.e2e_jobs
        raise AssertionError(f"Unexpected pages: {path}")

    def command(self, *args):
        self.commands.append(args)
        if args[:2] == ("gh", "api") and args[-1].endswith("/logs"):
            return self.log
        if args[:2] == ("gh", "api") and "/files?" in args[-1]:
            return json.dumps([self.files])
        raise AssertionError(f"Mutation or unexpected transport: {args}")

    def evaluate(self):
        guard = load()
        with patch.object(guard, "api", self.api), patch.object(guard, "pages", self.pages), \
                patch.object(guard, "command", self.command), \
                patch.object(guard, "workflow_requirements", side_effect=AssertionError("Unbound policy fallback")), \
                patch.object(guard.subprocess, "run", side_effect=AssertionError("Live subprocess")):
            return guard.merge(10992, HEAD), guard


def test_native_python_only_guard_entrypoint():
    w = World()
    result, _ = w.evaluate()
    assert result["status"] == "READY_TO_MERGE"
    assert result["workflows"]["ci.yml"]["run"] == 40075590949
    flags = result["workflows"]["ci.yml"]["classifier_required_set"]
    assert flags["ts_changed"] is False and flags["python_changed"] is True
    assert result["workflows"]["e2e.yml"]["skipped"] is True
    assert all(args[:2] == ("gh", "api") for args in w.commands)


@pytest.mark.parametrize("name", ["python", "python-contracts", "python-docx",
                                  "python-modal (0)", "python-modal (1)", "python-modal (2)"])
@pytest.mark.parametrize("conclusion", ["failure", "skipped", "cancelled", None, "missing"])
def test_each_required_python_phase_must_pass(name, conclusion):
    w = World()
    if conclusion == "missing":
        w.jobs.remove(w.job(name))
    else:
        w.job(name)["conclusion"] = conclusion
        if conclusion is None:
            w.job(name)["status"] = "in_progress"
    with pytest.raises(RuntimeError):
        w.evaluate()


@pytest.mark.parametrize("change", ["head", "base", "workflow", "rule_digest", "rule_bytes",
    "workflow_blob", "classifier_blob", "unknown", "flag_missing", "flag_string",
    "paths", "rename_origin", "native_pr", "multiple_pr", "native_event", "native_branch",
    "native_path", "native_head", "scope_missing", "scope_duplicate", "duplicate_job",
    "old_attempt", "foreign_job", "job_head", "workflow_red", "workflow_pending"])
def test_invalid_or_ambiguous_provenance_never_removes_required_jobs(change):
    w = World()
    if change in ("head", "base", "workflow"):
        key = {"head":"HEAD_SHA", "base":"BASE_SHA", "workflow":"WORKFLOW_SHA"}[change]
        w.log = w.log.replace(f"{key}={FIXTURE['bindings'][key]}", f"{key}={'f'*40}")
    elif change == "rule_digest": w.receipt(rules_sha256="f"*64)
    elif change == "rule_bytes": w.rules += " "
    elif change.endswith("_blob"):
        name = ".github/workflows/ci.yml" if change == "workflow_blob" else "scripts/ci/change_scope.py"
        w.blobs[f"contents/{name}?ref={FIXTURE['bindings']['WORKFLOW_SHA']}"] = "f"*40
    elif change == "unknown": w.receipt(scope="unknown")
    elif change == "flag_missing": w.receipt(ts_changed=None)
    elif change == "flag_string": w.receipt(ts_changed="false")
    elif change == "paths": w.files.append({"filename":"app/runtime.ts"})
    elif change == "rename_origin": w.files[0]["previous_filename"] = "app/runtime.ts"
    elif change == "native_pr": w.run["pull_requests"] = []
    elif change == "multiple_pr": w.run["pull_requests"] *= 2
    elif change.startswith("native_"):
        field = {"native_event":"event", "native_branch":"head_branch", "native_path":"path", "native_head":"head_sha"}[change]
        w.run[field] = "other"
    elif change == "scope_missing": w.jobs.remove(w.job("classify-change-scope"))
    elif change in ("scope_duplicate", "duplicate_job"):
        w.jobs.append(copy.deepcopy(w.job("classify-change-scope" if change == "scope_duplicate" else "python")))
    elif change == "old_attempt": w.job("python")["run_attempt"] = 0
    elif change == "foreign_job": w.job("python")["run_id"] += 1
    elif change == "job_head": w.job("python")["head_sha"] = "f"*40
    elif change == "workflow_red": w.run["conclusion"] = "failure"
    elif change == "workflow_pending": w.run.update(status="queued", conclusion=None)
    with pytest.raises((RuntimeError, AssertionError)):
        w.evaluate()


@pytest.mark.parametrize("name", ["python", "python-contracts", "python-docx", "python-modal (0)", "test"])
def test_successful_job_without_actual_execution_is_not_proof(name):
    w = World()
    w.job(name)["steps"] = []
    with pytest.raises(RuntimeError):
        w.evaluate()


@pytest.mark.parametrize("name", ["typescript", "unit-tests", "unit-tests-isolated", "ci-infra"])
@pytest.mark.parametrize("state", ["failure", "cancelled", "timed_out", None])
def test_no_optional_red_or_infrastructure_failure_waiver(name, state):
    w = World()
    w.job(name).update(conclusion=state, status="completed" if state else "queued")
    with pytest.raises(RuntimeError):
        w.evaluate()


def add_ts(w):
    w.receipt(ts_changed=True)
    w.job("typescript").update(conclusion="success", steps=[{"name":"Typecheck", "status":"completed", "conclusion":"success"}])
    w.job("unit-tests-isolated").update(conclusion="success", steps=[{"name":"Isolated unit tests", "status":"completed", "conclusion":"success"}])
    unit = w.job("unit-tests")
    w.jobs.remove(unit)
    for i in (1, 2, 3):
        j = copy.deepcopy(unit)
        j.update(id=unit["id"]+i, name=f"unit-tests ({i})", conclusion="success",
                 steps=[{"name":"Unit tests", "status":"completed", "conclusion":"success"}])
        w.jobs.append(j)


def test_mixed_required_python_and_ts_are_both_proved():
    w = World()
    add_ts(w)
    assert w.evaluate()[0]["status"] == "READY_TO_MERGE"


def test_required_ts_passes_with_explicitly_unrequired_python_skipped():
    w = World()
    add_ts(w)
    w.receipt(python_changed=False)
    for job in w.jobs:
        if job["name"].startswith("python"):
            job.update(conclusion="skipped", steps=[])
    assert w.evaluate()[0]["status"] == "READY_TO_MERGE"


@pytest.mark.parametrize("name", ["typescript", "unit-tests-isolated", "unit-tests (1)", "unit-tests (2)", "unit-tests (3)"])
def test_required_ts_family_skips_refuse(name):
    w = World()
    add_ts(w)
    w.job(name)["conclusion"] = "skipped"
    with pytest.raises(RuntimeError):
        w.evaluate()


def test_latest_substantive_red_is_not_replaced_by_old_green():
    w = World()
    old = copy.deepcopy(w.run)
    w.run.update(run_attempt=2, conclusion="failure")
    w.runs = [old, w.run]
    with pytest.raises(RuntimeError):
        w.evaluate()


@pytest.mark.parametrize("required,state", [(True,"skipped"),(True,"failure"),(False,"failure"),(False,"cancelled")])
def test_no_e2e_red_or_required_skip_waiver(required, state):
    w = World()
    w.receipt(e2e_required=required)
    w.e2e = dict(w.run, id=40075590960, path=".github/workflows/e2e.yml", conclusion=state)
    with pytest.raises(RuntimeError):
        w.evaluate()


def test_exact_head_successful_e2e_wrapper_with_skipped_tests():
    w = World()
    w.e2e = dict(w.run, id=40075590960, path=".github/workflows/e2e.yml")
    w.e2e_jobs = [dict(w.jobs[0], run_id=w.e2e["id"], name="e2e", conclusion="skipped", steps=[])]
    result, _ = w.evaluate()
    assert result["workflows"]["e2e.yml"]["run"] == w.e2e["id"]
    assert result["workflows"]["e2e.yml"]["skipped"] is True
