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
        self.run.setdefault("run_started_at", "2026-10-24T07:48:30Z")  # fixture predates it
        self.jobs = copy.deepcopy(FIXTURE["jobs"])
        # The fixture's aggregate predates the exemption gate step; the current
        # workflow runs it (skipped) before a paid verdict. Shape it to match.
        next(j for j in self.jobs if j["name"] == "test")["steps"].insert(1, {
            "name": "Require exemption or exact-head real CI success",
            "status": "completed", "conclusion": "skipped"})
        self.log = FIXTURE["log"]
        self.job_logs = {}  # job id -> log text; any other job reads the classifier log
        self.files = copy.deepcopy(FIXTURE["files"])
        self.parents = FIXTURE["parents"][:]
        self.blobs = copy.deepcopy(FIXTURE["blob_shas"])
        self.rules = FIXTURE["rules"]
        self.e2e = dict(self.run, id=40075590960, path=".github/workflows/e2e.yml")
        scope = copy.deepcopy(self.jobs[0])
        scope.update(run_id=self.e2e["id"], steps=[{
            "name": "Classify paid E2E scope", "status": "completed", "conclusion": "success"}])
        self.e2e_jobs = [scope, dict(scope, id=scope["id"]+1, name="e2e",
                                     conclusion="skipped", steps=[]),
                         dict(scope, id=scope["id"]+2, name="e2e-log-capture",
                              conclusion="skipped", steps=[])]
        self.commands = []
        self.reads = []
        self.runs = None
        # Trusted ci.yml bytes at the classifier's workflow sha. None = unproven (the default):
        # a run is then never treated as a legacy static three-shard matrix.
        self.ci_yml = None

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
            if "workflows/ci.yml?ref=" in path and self.ci_yml is not None:
                body = self.ci_yml.encode()
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
            return self.job_logs.get(args[-1].split("/")[-2], self.log)
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


# The legacy ci.yml shape: one static `unit-tests` job with a literal three-shard matrix and no
# selector jobs. Only a trusted workflow like this may keep the fixed three-shard requirement.
LEGACY_CI_YML = """\
name: CI
jobs:
  typescript:
    runs-on: ubuntu-latest
  unit-tests:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        shard: [1, 2, 3]
    steps:
      - run: echo unit
  unit-tests-isolated:
    runs-on: ubuntu-latest
"""

OTHER = "f" * 40
STAMP = "2026-11-23T18:48:28.0088459Z"
GATE_STEP = "Validate unit lane and scheduled jobs"
PLACEHOLDER = "unit-tests (${{ matrix.shard }})"


def unit_gate_log(run, count, env=None, receipt=None, event=None, extra=()):
    """The unit gate's real log shape: timestamped env lines, multi-line UNIT_SCHEDULE, one event.

    A None override deletes that key, so each negative control edits exactly one fact.
    """
    run_id, attempt, skip = str(run["id"]), str(run.get("run_attempt", 1)), count == 0
    rec = {"skip_matrix": skip, "scheduled_count": count, "mode": "zero" if skip else "targeted",
           "zero_proof": "non_ts_rows_only" if skip else None, "reason": "zero_proof" if skip else "selected",
           "predicted_s": [], "head": HEAD, "run_id": run_id, "run_attempt": attempt}
    facts = {"CI_EXECUTION_SHA": HEAD, "CI_EVENT_NAME": "pull_request", "TS_CHANGED": "true",
             "UNIT_SELECT_RESULT": "success", "UNIT_PRECHECKS_RESULT": "success",
             "UNIT_TESTS_RESULT": "skipped" if skip else "success", "UNIT_SCHEDULED_COUNT": str(count),
             "EXPECTED_RUN_ID": run_id, "ACTIONS_READ_TOKEN": "***", "UNIT_JOB_HEAD_SHA": HEAD}
    ev = {"event": "unit_jobs_verified", "run_id": run_id, "head_sha": HEAD, "declared_count": count,
          "observed_jobs": [f"unit-tests ({n})" for n in range(1, count + 1)]}
    for target, changes in ((rec, receipt), (facts, env), (ev, event)):
        for key, value in (changes or {}).items():
            target.pop(key, None) if value is None else target.__setitem__(key, value)
    body = json.dumps(rec, indent=2).splitlines()
    lines = [f"{STAMP} env:"]
    lines += [f"{STAMP}   {k}: {facts[k]}" for k in list(facts)[:6] if k in facts]
    lines += [f"{STAMP}   UNIT_SCHEDULE: {body[0]}"] + body[1:]  # only the first line is timestamped
    lines += [f"{STAMP}   {k}: {facts[k]}" for k in list(facts)[6:] if k in facts]
    lines += [f"{STAMP} ##[endgroup]", f"{STAMP} " + json.dumps(ev, separators=(",", ":")), *extra]
    return "\n".join(lines) + "\n"


def set_unit_matrix(w, count, gate=True, **log):
    """Replace the unit shards with what the gate attests: count shards, or ONE skipped placeholder."""
    template = copy.deepcopy(next(j for j in w.jobs if j["name"] == "unit-tests (1)"))
    w.jobs[:] = [j for j in w.jobs if not j["name"].startswith("unit-tests (")]
    base = max(j["id"] for j in w.jobs)
    if count == 0:
        w.jobs.append(dict(template, id=base + 1, name=PLACEHOLDER, conclusion="skipped", steps=[]))
    for n in range(1, count + 1):
        w.jobs.append(dict(template, id=base + 1 + n, name=f"unit-tests ({n})"))
    if not gate:
        return None
    job = dict(template, id=base + 10, name="unit-gate", conclusion="success",
               steps=[{"name": GATE_STEP, "status": "completed", "conclusion": "success"}])
    w.jobs.append(job)
    w.job_logs[str(job["id"])] = unit_gate_log(w.run, count, **log)
    return job


def test_attested_zero_scheduled_shards_with_skipped_placeholder_allows():
    w = World()
    add_ts(w)
    gate = set_unit_matrix(w, 0)
    assert w.evaluate()[0]["status"] == "READY_TO_MERGE"
    assert any(args[-1].endswith(f"/jobs/{gate['id']}/logs") for args in w.commands)


def test_attested_uncertain_zero_schedule_allows():
    w = World()
    add_ts(w)
    set_unit_matrix(w, 0, receipt={"mode": "uncertain", "zero_proof": None, "reason": "selector_unavailable"})
    assert w.evaluate()[0]["status"] == "READY_TO_MERGE"


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5])
def test_attested_scheduled_shards_all_green_allow(count):
    w = World()
    add_ts(w)
    set_unit_matrix(w, count)
    assert w.evaluate()[0]["status"] == "READY_TO_MERGE"


@pytest.mark.parametrize("count,shard", [(3, 1), (3, 3), (5, 5)])
@pytest.mark.parametrize("state", ["failure", "cancelled", "timed_out", "skipped"])
def test_scheduled_shard_red_or_skipped_refuses(count, shard, state):
    w = World()
    add_ts(w)
    set_unit_matrix(w, count)
    w.job(f"unit-tests ({shard})")["conclusion"] = state
    with pytest.raises(RuntimeError):
        w.evaluate()


@pytest.mark.parametrize("count,missing", [(2, 2), (4, 4), (5, 3)])
def test_scheduled_shard_missing_refuses(count, missing):
    w = World()
    add_ts(w)
    set_unit_matrix(w, count)
    w.jobs.remove(w.job(f"unit-tests ({missing})"))
    with pytest.raises(RuntimeError):
        w.evaluate()


def test_skipped_matrix_without_any_unit_gate_refuses():
    w = World()
    add_ts(w)
    set_unit_matrix(w, 0, gate=False)
    with pytest.raises(RuntimeError):
        w.evaluate()


@pytest.mark.parametrize("break_gate", ["red", "no_step", "step_skipped", "empty_log", "garbage_log",
                                        "foreign_head", "foreign_run", "foreign_attempt", "duplicate"])
def test_skipped_matrix_with_untrusted_unit_gate_refuses(break_gate):
    w = World()
    add_ts(w)
    gate = set_unit_matrix(w, 0)
    if break_gate == "red":
        gate["conclusion"] = "failure"
    elif break_gate == "no_step":
        gate["steps"] = []
    elif break_gate == "step_skipped":
        gate["steps"][0]["conclusion"] = "skipped"
    elif break_gate == "empty_log":
        w.job_logs[str(gate["id"])] = ""
    elif break_gate == "garbage_log":
        w.job_logs[str(gate["id"])] = f"{STAMP} nothing useful\n"
    elif break_gate == "foreign_head":
        gate["head_sha"] = OTHER
    elif break_gate == "foreign_run":
        gate["run_id"] += 1
    elif break_gate == "foreign_attempt":
        gate["run_attempt"] = 0
    else:
        w.jobs.append(dict(gate, id=gate["id"] + 1))
    with pytest.raises(RuntimeError):
        w.evaluate()


FOREIGN_OR_INVALID_RECEIPTS = {
    "receipt_other_head": {"receipt": {"head": OTHER}},
    "receipt_other_run": {"receipt": {"run_id": "1"}},
    "receipt_other_attempt": {"receipt": {"run_attempt": "2"}},
    "receipt_attempt_missing": {"receipt": {"run_attempt": None}},
    "env_other_head": {"env": {"CI_EXECUTION_SHA": OTHER}},
    "env_other_job_head": {"env": {"UNIT_JOB_HEAD_SHA": OTHER}},
    "env_other_run": {"env": {"EXPECTED_RUN_ID": "1"}},
    "env_fact_missing": {"env": {"UNIT_PRECHECKS_RESULT": None}},
    "env_count_differs": {"env": {"UNIT_SCHEDULED_COUNT": "1"}},
    "matrix_reported_success": {"env": {"UNIT_TESTS_RESULT": "success"}},
    "event_other_head": {"event": {"head_sha": OTHER}},
    "event_other_run": {"event": {"run_id": "1"}},
    "event_declared_differs": {"event": {"declared_count": 1}},
    "event_observed_differs": {"event": {"observed_jobs": ["unit-tests (1)"]}},
    "event_missing": {"event": {"event": "unit_gate_failed"}},
    "event_duplicated": {"extra": (f'{STAMP} {{"event":"unit_jobs_verified","run_id":"1"}}',)},
    "zero_mode_without_proof": {"receipt": {"zero_proof": ""}},
    "uncertain_with_proof": {"receipt": {"mode": "uncertain", "zero_proof": "x"}},
    "uncertain_reason_is_zero_proof": {"receipt": {"mode": "uncertain", "zero_proof": None}},
    "uncertain_bad_reason": {"receipt": {"mode": "uncertain", "zero_proof": None, "reason": "Not Snake"}},
    "unknown_skip_mode": {"receipt": {"mode": "targeted"}},
}


@pytest.mark.parametrize("name", sorted(FOREIGN_OR_INVALID_RECEIPTS))
def test_receipt_for_another_head_run_or_attempt_or_unattested_never_counts(name):
    w = World()
    add_ts(w)
    set_unit_matrix(w, 0, **FOREIGN_OR_INVALID_RECEIPTS[name])
    with pytest.raises(RuntimeError):
        w.evaluate()


def test_foreign_receipt_cannot_excuse_a_scheduled_failure():
    w = World()
    add_ts(w)
    set_unit_matrix(w, 3, receipt={"head": OTHER})
    w.job("unit-tests (2)")["conclusion"] = "failure"
    with pytest.raises(RuntimeError):
        w.evaluate()


@pytest.mark.parametrize("contradiction", ["real_shard_beside_zero", "placeholder_not_skipped",
                                           "no_placeholder", "extra_shard_beyond_count"])
def test_attestation_contradicted_by_the_job_inventory_refuses(contradiction):
    w = World()
    add_ts(w)
    if contradiction == "extra_shard_beyond_count":
        set_unit_matrix(w, 2)
        w.jobs.append(dict(w.job("unit-tests (1)"), id=max(j["id"] for j in w.jobs) + 1, name="unit-tests (3)"))
    else:
        set_unit_matrix(w, 0)
        if contradiction == "real_shard_beside_zero":
            w.jobs.append(dict(w.job("unit-gate"), id=max(j["id"] for j in w.jobs) + 1, name="unit-tests (1)",
                               steps=[{"name": "Unit tests", "status": "completed", "conclusion": "success"}]))
        elif contradiction == "placeholder_not_skipped":
            w.job(PLACEHOLDER)["conclusion"] = "success"
        else:
            w.jobs.remove(w.job(PLACEHOLDER))
    with pytest.raises(RuntimeError):
        w.evaluate()


STALE_ATTESTATIONS = ["receipt_other_attempt", "receipt_other_head", "receipt_other_run",
                      "receipt_attempt_missing", "event_other_head"]


@pytest.mark.parametrize("stale", STALE_ATTESTATIONS)
def test_three_of_five_with_a_stale_attestation_refuses_instead_of_falling_back(stale):
    # The selector claimed five shards; shards 4 and 5 have no job records. A receipt from
    # another attempt/head/run used to be discarded, dropping the requirement to shards 1-3,
    # which the incomplete inventory satisfies. It must refuse.
    w = World()
    add_ts(w)
    set_unit_matrix(w, 5, **FOREIGN_OR_INVALID_RECEIPTS[stale])
    for missing in (4, 5):
        w.jobs.remove(w.job(f"unit-tests ({missing})"))
    with pytest.raises(RuntimeError, match="UNIT_ATTESTATION_REQUIRED"):
        w.evaluate()


def test_three_of_five_with_the_matching_attempt_still_refuses_for_the_missing_shards():
    w = World()
    add_ts(w)
    set_unit_matrix(w, 5)
    for missing in (4, 5):
        w.jobs.remove(w.job(f"unit-tests ({missing})"))
    with pytest.raises(RuntimeError):
        w.evaluate()


# A red shard is refused by the generic job-green guard; a skipped one passes that guard and is
# refused by the attestation/required-job checks. Either way the run never reads as mergeable.
SCHEDULED_RED_REFUSALS = r"JOB_NOT_GREEN_OR_UNBOUND|UNIT_ATTESTATION_REQUIRED|REQUIRED_JOB_NOT_GREEN"


@pytest.mark.parametrize("state", ["failure", "skipped"])
@pytest.mark.parametrize("stale", STALE_ATTESTATIONS)
def test_scheduled_red_with_a_stale_attestation_refuses(stale, state):
    w = World()
    add_ts(w)
    set_unit_matrix(w, 3, **FOREIGN_OR_INVALID_RECEIPTS[stale])
    w.job("unit-tests (2)")["conclusion"] = state
    with pytest.raises(RuntimeError, match=SCHEDULED_RED_REFUSALS):
        w.evaluate()


@pytest.mark.parametrize("state", ["failure", "skipped"])
def test_scheduled_red_with_the_matching_attempt_refuses(state):
    w = World()
    add_ts(w)
    set_unit_matrix(w, 3)
    w.job("unit-tests (2)")["conclusion"] = state
    with pytest.raises(RuntimeError, match=SCHEDULED_RED_REFUSALS):
        w.evaluate()


@pytest.mark.parametrize("gate", ["missing", "no_step", "empty_log", "garbage_log"])
def test_dynamic_selector_run_without_a_valid_attestation_refuses(gate):
    # Missing or invalid attestation: shards 1-3 are all green, yet the selector jobs prove the
    # run is dynamic, so the fixed three-shard fallback must not apply.
    w = World()
    add_ts(w)
    job = set_unit_matrix(w, 3, gate=gate != "missing")
    if gate == "missing":
        w.jobs.append(dict(w.job("unit-tests (1)"), id=max(j["id"] for j in w.jobs) + 1, name="unit-select"))
    elif gate == "no_step":
        job["steps"] = []
    elif gate == "empty_log":
        w.job_logs[str(job["id"])] = ""
    else:
        w.job_logs[str(job["id"])] = f"{STAMP} nothing useful\n"
    with pytest.raises(RuntimeError, match="UNIT_ATTESTATION_REQUIRED"):
        w.evaluate()


@pytest.mark.parametrize("second", ["unit-tests", "unit-tests (${{ matrix.other }})"])
def test_attested_zero_with_multiple_skipped_placeholders_refuses(second):
    # Job names are unique per run, so a second placeholder carries a different name. The old
    # `all(placeholder)` predicate accepted the pair; attested zero is exactly ONE native job.
    w = World()
    add_ts(w)
    set_unit_matrix(w, 0)
    w.jobs.append(dict(w.job(PLACEHOLDER), id=max(j["id"] for j in w.jobs) + 1, name=second))
    with pytest.raises(RuntimeError, match="UNIT_JOBS_CONTRADICT_ATTESTATION"):
        w.evaluate()


@pytest.mark.parametrize("placeholder", ["unit-tests", "unit-tests (${{ matrix.other }})"])
def test_attested_zero_requires_the_exact_native_placeholder_name(placeholder):
    w = World()
    add_ts(w)
    set_unit_matrix(w, 0)
    w.job(PLACEHOLDER)["name"] = placeholder
    with pytest.raises(RuntimeError, match="UNIT_JOBS_CONTRADICT_ATTESTATION"):
        w.evaluate()


def test_attested_zero_placeholder_that_ran_steps_refuses():
    w = World()
    add_ts(w)
    set_unit_matrix(w, 0)
    w.job(PLACEHOLDER)["steps"] = [{"name": "Unit tests", "status": "completed", "conclusion": "skipped"}]
    with pytest.raises(RuntimeError, match="UNIT_JOBS_CONTRADICT_ATTESTATION"):
        w.evaluate()


def test_legacy_static_matrix_is_proved_only_by_the_trusted_workflow():
    w = World()
    add_ts(w)
    w.ci_yml = LEGACY_CI_YML
    assert w.evaluate()[0]["status"] == "READY_TO_MERGE"


@pytest.mark.parametrize("workflow", [
    None,  # the workflow bytes prove nothing
    LEGACY_CI_YML.replace("[1, 2, 3]", "[1, 2, 3, 4, 5]"),
    LEGACY_CI_YML.replace("shard: [1, 2, 3]", "shard: ${{ fromJSON(needs.unit-select.outputs.matrix) }}"),
    LEGACY_CI_YML + "  unit-gate:\n    runs-on: ubuntu-latest\n",
    LEGACY_CI_YML.replace("  unit-tests:", "  unit-prechecks:\n    runs-on: ubuntu-latest\n  unit-tests:"),
])
def test_legacy_three_shard_fallback_refuses_when_the_workflow_does_not_prove_a_static_matrix(workflow):
    w = World()
    add_ts(w)
    w.ci_yml = workflow
    with pytest.raises(RuntimeError, match="UNIT_ATTESTATION_REQUIRED"):
        w.evaluate()


def test_legacy_static_matrix_with_selector_jobs_in_the_run_refuses():
    # The trusted workflow looks legacy, but the run itself reports a selector job: dynamic CI.
    w = World()
    add_ts(w)
    w.ci_yml = LEGACY_CI_YML
    w.jobs.append(dict(w.job("unit-tests (1)"), id=max(j["id"] for j in w.jobs) + 1, name="unit-select"))
    with pytest.raises(RuntimeError, match="UNIT_ATTESTATION_REQUIRED"):
        w.evaluate()


def test_legacy_static_matrix_with_a_missing_shard_refuses():
    w = World()
    add_ts(w)
    w.ci_yml = LEGACY_CI_YML
    w.jobs.remove(w.job("unit-tests (3)"))
    with pytest.raises(RuntimeError):
        w.evaluate()


@pytest.mark.parametrize("name", ["typescript", "unit-tests-isolated"])
def test_attested_zero_shards_never_excuse_the_other_required_ts_jobs(name):
    w = World()
    add_ts(w)
    set_unit_matrix(w, 0)
    w.job(name)["conclusion"] = "skipped"
    with pytest.raises(RuntimeError):
        w.evaluate()


def test_python_only_template_named_skipped_matrix_allows():
    w = World()
    w.job("unit-tests")["name"] = PLACEHOLDER
    assert w.evaluate()[0]["status"] == "READY_TO_MERGE"


def test_python_only_template_named_placeholder_beside_real_shards_refuses():
    w = World()
    unit = w.job("unit-tests")
    unit["name"] = PLACEHOLDER
    w.jobs.append(dict(unit, id=max(j["id"] for j in w.jobs) + 1, name="unit-tests (1)", conclusion="success"))
    with pytest.raises(RuntimeError):
        w.evaluate()


def test_mixed_required_python_and_ts_are_both_proved():
    w = World()
    add_ts(w)
    w.ci_yml = LEGACY_CI_YML
    assert w.evaluate()[0]["status"] == "READY_TO_MERGE"


def test_required_ts_passes_with_explicitly_unrequired_python_skipped():
    w = World()
    add_ts(w)
    w.ci_yml = LEGACY_CI_YML
    w.receipt(python_changed=False)
    for job in w.jobs:
        if job["name"].startswith("python"):
            job.update(conclusion="skipped", steps=[])
    assert w.evaluate()[0]["status"] == "READY_TO_MERGE"


@pytest.mark.parametrize("name", ["typescript", "unit-tests-isolated", "unit-tests (1)", "unit-tests (2)", "unit-tests (3)"])
def test_required_ts_family_skips_refuse(name):
    w = World()
    add_ts(w)
    w.ci_yml = LEGACY_CI_YML
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
    result, _ = w.evaluate()
    assert result["workflows"]["e2e.yml"]["run"] == w.e2e["id"]
    assert result["workflows"]["e2e.yml"]["skipped"] is True
