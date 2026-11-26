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


def stamp(minutes):
    """A run_started_at value; different calls must give different seconds."""
    import datetime
    base = datetime.datetime(2026, 11, 23, 12, 0, 0, tzinfo=datetime.timezone.utc)
    return (base + datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_pr(head=HEAD, labels=PENDING, merged=False):
    pr = {"number": NUMBER,
          "head": {"sha": head, "ref": REF, "repo": {"full_name": "heydonna-app/heydonna-app"}},
          "state": "open", "draft": False, "base": {"ref": "main"},
          "labels": [{"name": name} for name in labels]}
    if merged:
        pr["merged"] = True
        pr["merge_commit_sha"] = "d" * 40
    return pr


def workflow_run(workflow, conclusion, minutes=0, status="completed", attempt=1):
    return {"id": RUN_IDS[workflow], "run_attempt": attempt, "head_sha": HEAD,
            "event": "pull_request", "head_branch": REF,
            "path": f".github/workflows/{workflow}", "status": status,
            "conclusion": conclusion, "run_started_at": stamp(minutes),
            "pull_requests": [{"number": NUMBER}]}


def workflow_jobs(workflow, conclusion, without_steps):
    jobs = []
    for name, step_names in REQUIRED_STEPS[workflow].items():
        steps = [] if without_steps else [
            {"name": step, "status": "completed", "conclusion": "success"} for step in step_names]
        jobs.append({"id": 100 + len(jobs), "name": name, "status": "completed",
                     "conclusion": conclusion if conclusion else "skipped",
                     "steps": steps, "run_id": RUN_IDS[workflow],
                     "run_attempt": 1, "head_sha": HEAD})
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
        if path.startswith("git/commits/"):
            # Synthetic merge-commit parents for a workflow sha in a stubbed
            # classifier log: [BASE, HEAD] mirrors the GitHub test-merge shape.
            return {"parents": [{"sha": BASE}, {"sha": HEAD}]}
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
    # Ordinary fixture fallback requires E2E; never consult live canonical policy.
    merge.workflow_requirements = lambda *_: {"e2e_required": True}
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


# ---- app test-only classifier receipt (canonical + legacy spelling) ----
import hashlib  # noqa: E402
import json  # noqa: E402

BASE = "1" * 40
WFSHA = "2" * 40
RULES_BYTES = b'{"schema_version": 1}'
RULES_SHA = hashlib.sha256(RULES_BYTES).hexdigest()
TEST_FILE = "scripts/ci/tests/test_nysd_migration_scope_gate.py"
CANONICAL = {"schema_version": 1, "scope": "test_only", "test_only": True,
             "control_plane_only": False, "paid_ci_exempt": False,
             "product_changed": False, "ci_required": True, "e2e_required": False,
             "rules_sha256": RULES_SHA, "changed_files": [TEST_FILE]}
LEGACY = {"schema_version": 1, "scope": "app_test_only", "app_test_only": True,
          "control_plane_only": False, "paid_ci_exempt": False,
          "product_changed": False, "ci_required": True, "e2e_required": False,
          "rules_sha256": RULES_SHA}


def classifier_proof(receipt, head_in_log=HEAD, rules_bytes=RULES_BYTES, pr_files=(TEST_FILE,),
                     legacy_globs=None, no_receipt=False):
    lines = ["2026-10-18T19:36:11.0Z " + json.dumps(receipt)] if not no_receipt else ["no receipt"]
    lines += [f"BASE_SHA: {BASE}", f"HEAD_SHA: {head_in_log}", f"WORKFLOW_SHA: {WFSHA}",
              "EVENT_NAME: pull_request", f"PR_NUMBER: {NUMBER}"]
    merge.job_log = lambda job_id: "\n".join(lines)
    merge.api = lambda path: {"parents": [{"sha": BASE}, {"sha": HEAD}]}
    merge.contents_identity = lambda path, ref: (
        ("blob-" + path, rules_bytes) if path.endswith("change-scope-rules.json")
        else ("blob-" + path, b""))
    rules = dict(json.loads(RULES_BYTES), **({"app_test_only": legacy_globs} if legacy_globs else {}))
    if legacy_globs:
        merge.contents_identity = lambda path, ref: (
            ("blob-" + path, json.dumps(rules).encode()) if path.endswith("change-scope-rules.json")
            else ("blob-" + path, b""))
    merge.command = lambda *args: json.dumps([[{"filename": name} for name in pr_files]])
    pr = make_pr()
    run = workflow_run("ci.yml", "success")
    scope = {"id": 7, "status": "completed", "conclusion": "success"}
    return merge.app_test_only_classifier_proof(pr, "ci.yml", run, scope)


def t9_canonical_test_only_receipt_is_accepted():
    proof = classifier_proof(dict(CANONICAL))
    assert proof is not None and proof["exempt"] == "test_only", proof
    assert proof["head"] == HEAD and proof["rules_sha256"] == RULES_SHA, proof


def t10_legacy_app_test_only_receipt_is_still_accepted():
    receipt = dict(LEGACY, rules_sha256=hashlib.sha256(
        json.dumps(dict(json.loads(RULES_BYTES), app_test_only=["scripts/ci/tests/**"])).encode()
    ).hexdigest())
    proof = classifier_proof(receipt, legacy_globs=["scripts/ci/tests/**"])
    assert proof is not None and proof["exempt"] == "app_test_only", proof


def t11_product_changed_true_is_refused():
    assert classifier_proof(dict(CANONICAL, product_changed=True)) is None


def t12_e2e_required_true_is_refused():
    assert classifier_proof(dict(CANONICAL, e2e_required=True)) is None


def t13_ci_not_required_or_paid_exempt_is_refused():
    assert classifier_proof(dict(CANONICAL, ci_required=False)) is None
    assert classifier_proof(dict(CANONICAL, paid_ci_exempt=True)) is None
    assert classifier_proof(dict(CANONICAL, test_only=False)) is None


def t14_head_or_rules_mismatch_is_refused():
    assert classifier_proof(dict(CANONICAL), head_in_log=OTHER) is None
    assert classifier_proof(dict(CANONICAL, rules_sha256="f" * 64)) is None


def t15_missing_receipt_is_refused():
    assert classifier_proof(dict(CANONICAL), no_receipt=True) is None


def t16_receipt_files_must_equal_pr_files():
    assert classifier_proof(dict(CANONICAL), pr_files=(TEST_FILE, "app/page.tsx")) is None
    assert classifier_proof(dict(CANONICAL, changed_files=[TEST_FILE, "app/page.tsx"])) is None


# ---- site lane confinement accepts control-plane paths (PR #10394) ----
SITE_RULES = json.dumps({
    "schema_version": 1,
    "control_plane_only": ["docs/**", "benchmarks/**", "*.md"],
    "site": ["website/**", "docs-site/**", "blog/**", "video/**"],
}).encode()


def site_confined(pr_files, rules=SITE_RULES):
    merge.command = lambda *args: json.dumps([[{"filename": name} for name in pr_files]])
    return merge.site_paths_confined(make_pr(), rules)


def t17_site_lane_accepts_control_plane_paths_beside_site_paths():
    assert site_confined(["website/index.html", "benchmarks/proofreading/reports/a.md",
                          "docs/plans/x.md", "docs-site/pages/a.mdx"]) is True
    assert site_confined(["website/index.html"]) is True


def t18_site_lane_refuses_app_product_ci_or_fixture_paths():
    for extra in ("app/page.tsx", "lib/x.ts", "convex/a.ts", "scripts/ci/change_scope.py",
                  ".github/workflows/ci.yml", "tests/e2e/specs/core/a.spec.ts",
                  "scripts/ci/tests/test_x.py"):
        assert site_confined(["website/index.html", "docs/plans/x.md", extra]) is False, extra


def t19_site_lane_fails_closed_on_empty_or_missing_rules():
    assert site_confined([]) is False
    assert site_confined(["website/index.html"], rules=b'{"schema_version": 1}') is False
    assert site_confined(["website/index.html"], rules=b"not json") is False


def t20_site_lane_checks_renamed_previous_filename():
    merge.command = lambda *args: json.dumps([[{"filename": "docs/b.md",
                                               "previous_filename": "app/a.tsx"}]])
    assert merge.site_paths_confined(make_pr(), SITE_RULES) is False


# ---- exact-head run selection: substantive vs gate-only verifier runs ----
# One head can carry several pull_request CI/E2E runs: the substantive run (real
# jobs) and gate-only verifier runs (only classify-change-scope + the aggregate
# gate job; every other roster job skipped with no steps). The merge decision
# binds to the newest SUBSTANTIVE run at its newest attempt.
#   PR #14629 rule: the newest gate-only verifier NEWER than the selected
#   substantive run must still be green.
#   PR #14626 rule (CTO ts 1795403483.247259): an OLDER verifier-only run is
#   superseded by a newer substantive run and never decides; a newer red or
#   pending substantive run still refuses.
SCOPE_JOB = "classify-change-scope"
E2E_LEGS = ("Run E2E auto-process-critical", "Run E2E core-rest shard 1/2",
            "Run E2E core-rest shard 2/2")
CI_GATE = "Require exemption or exact-head real CI success"
E2E_GATE = "Require exemption or exact-head real E2E success"


_JOB_IDS = __import__("itertools").count(500)


def _job(name, conclusion, steps=(), run_id=None, attempt=1):
    return {"id": next(_JOB_IDS), "name": name, "status": "completed",
            "conclusion": conclusion, "steps": list(steps),
            "run_id": run_id, "run_attempt": attempt, "head_sha": HEAD}


def _step(name, conclusion):
    return {"name": name, "status": "completed", "conclusion": conclusion}


def _run(workflow, run_id, attempt, conclusion, minutes, status="completed"):
    return {"id": run_id, "run_attempt": attempt, "head_sha": HEAD, "event": "pull_request",
            "head_branch": REF, "path": f".github/workflows/{workflow}", "status": status,
            "conclusion": conclusion, "run_started_at": stamp(minutes),
            "pull_requests": [{"number": NUMBER}]}


def _scope_steps(workflow):
    classifier = "Classify paid CI scope" if workflow == "ci.yml" else "Classify paid E2E scope"
    return [_step("Set up job", "success"), _step(classifier, "success"),
            _step("Complete runner", "success"), _step("Complete job", "success")]


def ci_verifier_entry(run_id, attempt, conclusion, minutes):
    """Gate-only ci run: classifier + aggregate gate ran, every other roster job skipped."""
    test_steps = [_step("Set up job", "success"),
                  _step("Require all CI phases", "skipped"),
                  _step("Confirm current paid CI success", "skipped"),
                  _step(CI_GATE, conclusion),
                  _step("Complete runner", "success"), _step("Complete job", "success")]
    jobs = [_job(SCOPE_JOB, "success", _scope_steps("ci.yml"), run_id, attempt),
            _job("test", conclusion, test_steps, run_id, attempt)]
    for name in merge.verifier_job_names("ci.yml"):
        if name not in (SCOPE_JOB, "test"):
            jobs.append(_job(name, "skipped", (), run_id, attempt))
    return _run("ci.yml", run_id, attempt, conclusion, minutes), jobs


def ci_substantive_entry(run_id, attempt, conclusion, minutes):
    """Paid ci run: real jobs executed, aggregate carries the paid verdict."""
    test_steps = [_step("Set up job", "success"),
                  _step("Require all CI phases", conclusion),
                  _step("Confirm current paid CI success", conclusion),
                  _step(CI_GATE, "skipped"),
                  _step("Complete runner", "success"), _step("Complete job", "success")]
    jobs = [_job(SCOPE_JOB, "success", _scope_steps("ci.yml"), run_id, attempt),
            _job("typescript", conclusion, [_step("Unit tests", conclusion)], run_id, attempt),
            _job("unit-tests (1)", conclusion, [_step("Unit tests", conclusion)], run_id, attempt),
            _job("python", conclusion, (), run_id, attempt),
            _job("test", conclusion, test_steps, run_id, attempt)]
    for name in ("unit-select", "unit-prechecks", "unit-tests-isolated", "unit-gate",
                 "unit-cost-receipt", "ci-receipt-publish", "functional-selection-manifest"):
        jobs.append(_job(name, conclusion, (), run_id, attempt))
    for name in ("python-modal", "python-docx", "python-contracts", "ci-infra", "theme-palette"):
        jobs.append(_job(name, "skipped", (), run_id, attempt))
    return _run("ci.yml", run_id, attempt, conclusion, minutes), jobs


def e2e_verifier_entry(run_id, attempt, conclusion, minutes):
    """Gate-only e2e run: aggregate gate ran, every browser leg skipped."""
    steps = [_step("Set up job", "success"),
             _step("Record whether this run starts paid E2E", "success")]
    steps += [_step(leg, "skipped") for leg in E2E_LEGS]
    steps += [_step("Final verdict", "skipped"),
              _step("Confirm current paid E2E success", "skipped"),
              _step(E2E_GATE, conclusion),
              _step("Complete runner", "success"), _step("Complete job", "success")]
    jobs = [_job(SCOPE_JOB, "success", _scope_steps("e2e.yml"), run_id, attempt),
            _job("e2e", conclusion, steps, run_id, attempt)]
    for name in ("teardown-pr-modal-apps", "rerun-on-infra-failure", "e2e-log-capture"):
        jobs.append(_job(name, "skipped", (), run_id, attempt))
    return _run("e2e.yml", run_id, attempt, conclusion, minutes), jobs


def e2e_substantive_entry(run_id, attempt, conclusion, minutes):
    """Paid e2e run: browser legs executed, aggregate carries the paid verdict."""
    steps = [_step("Set up job", "success"),
             _step("Record whether this run starts paid E2E", "success")]
    steps += [_step(leg, conclusion) for leg in E2E_LEGS]
    steps += [_step("Final verdict", conclusion),
              _step("Confirm current paid E2E success", conclusion),
              _step(E2E_GATE, "skipped"),
              _step("Complete runner", "success"), _step("Complete job", "success")]
    jobs = [_job(SCOPE_JOB, "success", _scope_steps("e2e.yml"), run_id, attempt),
            _job("e2e", conclusion, steps, run_id, attempt),
            _job("e2e-log-capture", "success", (), run_id, attempt),
            _job("teardown-pr-modal-apps", "skipped", (), run_id, attempt),
            _job("rerun-on-infra-failure", "skipped", (), run_id, attempt)]
    return _run("e2e.yml", run_id, attempt, conclusion, minutes), jobs


class RunSetStub(Stub):
    """Stub whose exact-head run set is explicit: {workflow file: [(run, jobs), ...]}."""

    def pages(self, path, key):
        self.calls.append(path)
        run_set = self.world["run_set"]
        if "/runs?" in path:
            workflow = path.split("actions/workflows/")[1].split("/")[0]
            return [run for run, _ in run_set.get(workflow, [])]
        if "/jobs?" in path:
            run_id = int(path.split("actions/runs/")[1].split("/")[0])
            for entries in run_set.values():
                for run, jobs in entries:
                    if run["id"] == run_id:
                        return jobs
        raise AssertionError(f"unexpected pages path: {path}")


def install_run_set(run_set):
    stub = RunSetStub({"run_set": run_set})
    merge.api, merge.pages, merge.command = stub.api, stub.pages, stub.command
    merge.workflow_requirements = lambda *_: {"e2e_required": True}
    merge.job_log = lambda job_id: "no classifier receipt"  # never a control-plane/test-only receipt
    return stub


def pr14629_run_set(**overrides):
    """The #14629 shape: newest runs are gate-only verifiers, substantive below."""
    run_set = {
        "ci.yml": [ci_verifier_entry(45087101884, 2, "success", 40),   # verifier, attempt 2 green
                   ci_verifier_entry(45086965553, 1, "failure", 30),   # verifier, attempt 1 red
                   ci_substantive_entry(45086965482, 1, "success", 20),
                   ci_verifier_entry(45086745712, 1, "failure", 10)],
        "e2e.yml": [e2e_verifier_entry(45087101843, 2, "success", 40),
                    e2e_verifier_entry(45086965508, 1, "failure", 30),
                    e2e_substantive_entry(45086965486, 1, "success", 20),
                    e2e_verifier_entry(45086745600, 1, "failure", 10)]}
    run_set.update(overrides)
    return run_set


def expect_any_refusal(fn):
    try:
        fn()
    except merge.Refusal:
        return
    raise AssertionError("expected a Refusal")


def t21_pr14629_selects_substantive_runs_when_gate_only_verifier_is_newest():
    install_run_set(pr14629_run_set())
    out = merge.merge(NUMBER, HEAD)
    assert out["status"] == "READY_TO_MERGE", out
    assert out["workflows"]["ci.yml"]["run"] == 45086965482, out["workflows"]
    assert out["workflows"]["e2e.yml"]["run"] == 45086965486, out["workflows"]


def t22_no_substantive_run_at_the_head_still_refuses():
    for workflow, entry in (("ci.yml", ci_verifier_entry), ("e2e.yml", e2e_verifier_entry)):
        install_run_set(pr14629_run_set(**{workflow: [entry(7001, 2, "success", 60)]}))
        expect_refusal(lambda: merge.merge(NUMBER, HEAD), f"WORKFLOW_MISSING workflow={workflow}")


def t23_gate_only_verifier_newer_than_substantive_failed_still_refuses():
    for workflow, ver, sub in (("ci.yml", ci_verifier_entry, ci_substantive_entry),
                               ("e2e.yml", e2e_verifier_entry, e2e_substantive_entry)):
        bad = [ver(7002, 2, "failure", 40), sub(7000, 1, "success", 20)]
        install_run_set(pr14629_run_set(**{workflow: bad}))
        expect_refusal(lambda: merge.merge(NUMBER, HEAD), "VERIFIER_NOT_GREEN")


def t24_newest_substantive_run_red_is_not_shopped_past_for_an_older_green():
    for workflow, entry in (("ci.yml", ci_substantive_entry), ("e2e.yml", e2e_substantive_entry)):
        runs = [entry(7005, 1, "failure", 40), entry(7003, 1, "success", 20)]
        install_run_set(pr14629_run_set(**{workflow: runs}))
        expect_refusal(lambda: merge.merge(NUMBER, HEAD), f"WORKFLOW_NOT_GREEN workflow={workflow}")


def t25_gate_only_run_with_classifier_exemption_is_still_honoured():
    # Only ci.yml attempts the in-loop exemption for a gate-only verifier run;
    # e2e exemptions travel through the app-test-only or ci-dummy lanes instead.
    exemption = {"run": 7006, "exempt": "control_plane_only"}
    original = merge.classifier_exemption
    try:
        install_run_set({"ci.yml": [ci_verifier_entry(7006, 1, "success", 60)]})
        merge.classifier_exemption = lambda *args, **kwargs: exemption
        assert merge.workflow_proof(make_pr(), "ci.yml", merge.WORKFLOWS["ci.yml"]) == exemption
    finally:
        merge.classifier_exemption = original


def t26_pr14626_older_red_verifier_superseded_by_newer_green_substantive_allows():
    # The exact refusal shape of PR #14626 at dacdb72f (CTO ts 1795403483.247259):
    # newest run a green substantive, below it an older red verifier-only run and
    # an older red substantive-with-failed-receipt-publish. The superseded red
    # verifier must not veto.
    run_set = pr14629_run_set(**{
        "ci.yml": [ci_substantive_entry(45096280214, 1, "success", 60),
                   ci_substantive_entry(45081008267, 1, "success", 30),
                   ci_verifier_entry(45081008513, 1, "failure", 30),
                   ci_verifier_entry(45080932030, 1, "failure", 20)],
        "e2e.yml": [e2e_substantive_entry(45096280208, 1, "success", 60),
                    e2e_verifier_entry(45081008556, 1, "success", 30)]})
    install_run_set(run_set)
    out = merge.merge(NUMBER, HEAD)
    assert out["status"] == "READY_TO_MERGE", out
    assert out["workflows"]["ci.yml"]["run"] == 45096280214, out["workflows"]


def t27_newer_red_substantive_still_refuses_with_older_verifier_noise():
    for workflow, ver, sub in (("ci.yml", ci_verifier_entry, ci_substantive_entry),
                               ("e2e.yml", e2e_verifier_entry, e2e_substantive_entry)):
        runs = [sub(7005, 1, "failure", 60), sub(7003, 1, "success", 30),
                ver(7001, 1, "failure", 10)]
        install_run_set(pr14629_run_set(**{workflow: runs}))
        expect_any_refusal(lambda: merge.merge(NUMBER, HEAD))


def t28_newer_pending_substantive_still_refuses():
    pending_run = {"id": 7007, "run_attempt": 1, "head_sha": HEAD, "event": "pull_request",
                   "head_branch": REF, "path": ".github/workflows/ci.yml",
                   "status": "in_progress", "conclusion": None,
                   "run_started_at": stamp(90), "pull_requests": [{"number": NUMBER}]}
    pending_jobs = [{"id": 901, "name": "test", "status": "in_progress", "conclusion": None,
                     "steps": [], "run_id": 7007, "run_attempt": 1, "head_sha": HEAD}]
    run_set = pr14629_run_set(**{
        "ci.yml": [(pending_run, pending_jobs),
                   ci_substantive_entry(7003, 1, "success", 30)]})
    install_run_set(run_set)
    expect_any_refusal(lambda: merge.merge(NUMBER, HEAD))


def t29_red_verifier_with_no_substantive_run_still_refuses():
    for workflow, entry in (("ci.yml", ci_verifier_entry), ("e2e.yml", e2e_verifier_entry)):
        install_run_set(pr14629_run_set(**{workflow: [entry(7008, 1, "failure", 60)]}))
        expect_refusal(lambda: merge.merge(NUMBER, HEAD), "VERIFIER_NOT_GREEN")


# ---- CTO rework controls (ts 1795416141.013939): the native required verifier ----
# The same-second CI waiver now also requires the CURRENT successful native
# required-context verifier (ci-dummy.yml at this exact PR/head, bound to the
# attempt that published the green contexts, with an authenticated mixed-scope
# classifier receipt whose bindings carry this HEAD_SHA). Without it, a
# gate-only red plus a tied paid success alone must REFUSE.
DUMMY_RID = 8001
DUMMY_WF = "ci-dummy.yml"


def native_verifier_entry(run_id, attempt, minutes, receipt_head=HEAD, receipt=None,
                          classify_job_steps=None, test_gate="success", e2e_gate="success"):
    """A green ci-dummy.yml run: the required-context exemption verifier."""
    receipt = receipt if receipt is not None else _classifier_receipt_json()
    lines = _classifier_lines(dict(receipt), workflow_sha=WFSHA)
    if receipt_head is not None:
        lines = [json.dumps(dict(receipt)), f"BASE_SHA: {BASE}",
                 f"HEAD_SHA: {receipt_head}", f"WORKFLOW_SHA: {WFSHA}",
                 "EVENT_NAME: pull_request", f"PR_NUMBER: {NUMBER}"]
    scope_steps = ([_step("Set up job", "success"), _step("Classify paid CI scope", "success"),
                    _step("Complete runner", "success"), _step("Complete job", "success")]
                   if classify_job_steps is None else classify_job_steps)
    jobs = [_job("detect-docs-only", "success", scope_steps, run_id, attempt),
            _job("validate-detect", "success", (), run_id, attempt),
            _job("test", "success" if test_gate == "success" else "failure",
                 [_step("Set up job", "success"),
                  _step("Require exemption or exact-head real CI success", test_gate),
                  _step("Complete runner", "success"), _step("Complete job", "success")],
                 run_id, attempt),
            _job("e2e", "success" if e2e_gate == "success" else "failure",
                 [_step("Set up job", "success"),
                  _step("Require exemption or exact-head real E2E success", e2e_gate),
                  _step("Complete runner", "success"), _step("Complete job", "success")],
                 run_id, attempt)]
    run = {"id": run_id, "run_attempt": attempt, "head_sha": HEAD, "event": "pull_request",
           "head_branch": REF, "path": f".github/workflows/{DUMMY_WF}", "status": "completed",
           "conclusion": "success", "run_started_at": stamp(minutes),
           "pull_requests": [{"number": NUMBER}]}
    return run, jobs, lines


def install_with_native_verifier(entries, native_entries):
    """Install ci/e2e run-set entries plus ci-dummy.yml native verifier runs."""
    stub = RunSetStub({"run_set": {**entries, DUMMY_WF: native_entries}})
    merge.api, merge.pages, merge.command = stub.api, stub.pages, stub.command
    merge.workflow_requirements = lambda *_: {"e2e_required": True}
    merge.job_log = lambda job_id: _log_for_run_set(DUMMY_LOGS, job_id)
    return stub


DUMMY_LOGS = {}


def _log_for_run_set(logs, job_id):
    if job_id in logs:
        return "\n".join(logs[job_id])
    return "no classifier receipt"


def t30_same_second_gate_only_verifier_beside_green_substantive_proceeds():
    # The #14638 shape (CTO ts 1795413605.307949 + REWORK 1795416141.013939):
    # the gate-only CI sibling and the paid run STARTED IN THE SAME UTC SECOND,
    # and the CURRENT successful native required verifier (ci-dummy.yml run
    # 45108723956 attempt 5 shape) is bound to this exact PR/head with an
    # authenticated mixed-scope receipt. The verifier's sole failure is its own
    # exemption gate; the paid run is native-bound and green; the native
    # required verifier is green. Only then is the verifier superseded.
    run, jobs, lines = native_verifier_entry(DUMMY_RID, 5, 90)
    DUMMY_LOGS.clear()
    DUMMY_LOGS[jobs[0]["id"]] = lines
    run_set = pr14629_run_set(**{
        "ci.yml": [ci_verifier_entry(7009, 1, "failure", 60),
                   ci_substantive_entry(7010, 1, "success", 60)]})
    install_with_native_verifier(run_set, [(run, jobs)])
    out = merge.merge(NUMBER, HEAD)
    assert out["status"] == "READY_TO_MERGE", out
    assert out["workflows"]["ci.yml"]["run"] == 7010, out["workflows"]


def t31_same_second_verifier_red_outside_its_gate_still_refuses():
    # Not narrow enough to pass a REAL failed verifier: a red job beside the
    # gate makes the run no longer a gate-only sibling, so it refuses (fail
    # closed) instead of being waived as a start-race artefact.
    bad_jobs_run, jobs = ci_verifier_entry(7011, 1, "failure", 60)
    for job in jobs:
        if job["name"] == "python":
            job["conclusion"] = "failure"
    run_set = pr14629_run_set(**{
        "ci.yml": [(bad_jobs_run, jobs), ci_substantive_entry(7012, 1, "success", 60)]})
    install_run_set(run_set)
    expect_any_refusal(lambda: merge.merge(NUMBER, HEAD))


def t31b_same_second_verifier_with_a_second_failed_step_still_refuses():
    # Only the gate step may be red for the waiver; any additional failed step in
    # the aggregate is a genuine failure and refuses.
    run, jobs = ci_verifier_entry(7019, 1, "failure", 60)
    for job in jobs:
        if job["name"] == "test":
            job["steps"] = [dict(step) for step in job["steps"]]
            job["steps"].insert(2, _step("Require all CI phases", "failure"))
    run_set = pr14629_run_set(**{
        "ci.yml": [(run, jobs), ci_substantive_entry(7020, 1, "success", 60)]})
    install_run_set(run_set)
    expect_any_refusal(lambda: merge.merge(NUMBER, HEAD))


def t32_same_second_verifier_with_no_green_substantive_still_refuses():
    # A real failed verifier with no later green substantive run keeps refusing.
    run_set = pr14629_run_set(**{
        "ci.yml": [ci_verifier_entry(7013, 1, "failure", 60),
                   ci_substantive_entry(7014, 1, "failure", 60)]})
    install_run_set(run_set)
    expect_any_refusal(lambda: merge.merge(NUMBER, HEAD))


def t33_same_second_verifier_beside_pending_substantive_still_refuses():
    pending_run = {"id": 7015, "run_attempt": 1, "head_sha": HEAD, "event": "pull_request",
                   "head_branch": REF, "path": ".github/workflows/ci.yml",
                   "status": "in_progress", "conclusion": None,
                   "run_started_at": stamp(60), "pull_requests": [{"number": NUMBER}]}
    pending_jobs = [{"id": 950, "name": "test", "status": "in_progress", "conclusion": None,
                     "steps": [], "run_id": 7015, "run_attempt": 1, "head_sha": HEAD}]
    run_set = pr14629_run_set(**{
        "ci.yml": [ci_verifier_entry(7016, 1, "failure", 60), (pending_run, pending_jobs)]})
    install_run_set(run_set)
    expect_any_refusal(lambda: merge.merge(NUMBER, HEAD))


def t34_same_second_verifier_beside_unbound_substantive_still_refuses():
    # No bound green verifier + green substantive pair: the substantive run must
    # be natively bound to this exact PR, else the ambiguity still refuses.
    unbound_run, jobs = ci_substantive_entry(7017, 1, "success", 60)
    unbound_run["pull_requests"] = [{"number": NUMBER}, {"number": NUMBER + 1}]
    run_set = pr14629_run_set(**{
        "ci.yml": [ci_verifier_entry(7018, 1, "failure", 60), (unbound_run, jobs)]})
    install_run_set(run_set)
    expect_any_refusal(lambda: merge.merge(NUMBER, HEAD))


def _classifier_receipt_json(**overrides):
    receipt = {"schema_version": 1, "scope": "mixed", "test_only": False,
               "app_test_only": False, "control_plane_only": False,
               "paid_ci_exempt": False, "product_changed": True,
               "ci_required": True, "e2e_required": False,
               "ts_changed": True, "python_changed": True,
               "rules_sha256": RULES_SHA, "changed_files": [TEST_FILE]}
    receipt.update(overrides)
    return receipt


def _classifier_lines(receipt, workflow_sha=WFSHA):
    return [json.dumps(receipt), f"BASE_SHA: {BASE}", f"HEAD_SHA: {HEAD}",
            f"WORKFLOW_SHA: {workflow_sha}", "EVENT_NAME: pull_request",
            f"PR_NUMBER: {NUMBER}"]


def _numbered_step(number, name, conclusion):
    return {"number": number, "name": name, "status": "completed", "conclusion": conclusion}


def e2e_exempted_entry(run_id, attempt, conclusion, minutes, receipt):
    """Gate-only e2e run that passed its exemption gate with every leg skipped."""
    steps = [_numbered_step(1, "Set up job", "success"),
             _numbered_step(2, "Record whether this run starts paid E2E", "success")]
    number = 3
    for leg in ("Run E2E auto-process-critical", "Run E2E core-rest shard 1/2",
                "Run E2E core-rest shard 2/2", "Final verdict",
                "Confirm current paid E2E success"):
        steps.append(_numbered_step(number, leg, "skipped"))
        number += 1
    steps.append(_numbered_step(number, E2E_GATE, "success"))
    steps.append(_numbered_step(number + 1, "Complete runner", "success"))
    steps.append(_numbered_step(number + 2, "Complete job", "success"))
    jobs = [_job(SCOPE_JOB, "success", _scope_steps("e2e.yml"), run_id, attempt),
            _job("e2e", conclusion, steps, run_id, attempt),
            _job("teardown-pr-modal-apps", "skipped", (), run_id, attempt),
            _job("rerun-on-infra-failure", "skipped", (), run_id, attempt),
            _job("e2e-log-capture", "skipped", (), run_id, attempt)]
    return _run("e2e.yml", run_id, attempt, conclusion, minutes), jobs


def install_e2e_duplicates(entries, receipts_by_scope_id):
    merge.pages = lambda path, key: (
        [run for run, _ in entries] if "/runs?" in path else
        next(jobs for run, jobs in entries
             if run["id"] == int(path.split("actions/runs/")[1].split("/")[0])))
    merge.job_log = lambda job_id: "\n".join(_classifier_lines(receipts_by_scope_id[job_id]))
    merge.command = lambda *args: json.dumps([[{"filename": TEST_FILE}]])


def e2e_classifier_proof(receipt):
    return {"head": HEAD, "rules_sha256": receipt["rules_sha256"], "workflow_sha": WFSHA}


def t35_identical_duplicate_e2e_exemptions_are_one_exemption():
    # #14638 second hold (CTO ts 1795413884.774109): two same-head, same-second
    # E2E runs, both successful exemptions with identical classifier output.
    receipt = _classifier_receipt_json()
    entries = [e2e_exempted_entry(7101, 1, "success", 60, receipt),
               e2e_exempted_entry(7102, 1, "success", 60, receipt)]
    install_e2e_duplicates(entries, {entry[1][0]["id"]: receipt for entry in entries})
    proof = merge.app_test_only_e2e_proof(make_pr(), e2e_classifier_proof(receipt))
    assert proof is not None and proof["skipped"] is True, proof
    assert proof["run"] == 7102, proof


def t36_duplicate_e2e_exemptions_must_match_the_classifier_output():
    for other in ({"e2e_required": True}, {"scope": "test_only"}, {"ci_required": False},
                  {"changed_files": [TEST_FILE, "app/page.tsx"]}):
        base = _classifier_receipt_json()
        changed = dict(base, **other)
        entries = [e2e_exempted_entry(7103, 1, "success", 60, base),
                   e2e_exempted_entry(7104, 1, "success", 60, base)]
        install_e2e_duplicates(entries, {entry[1][0]["id"]: receipt
                                         for entry, receipt in zip(entries, (base, changed))})
        assert merge.app_test_only_e2e_proof(make_pr(), e2e_classifier_proof(base)) is None, other


def t37_mixed_duplicate_e2e_pair_red_or_pending_still_refuses():
    receipt = _classifier_receipt_json()
    red_run, red_jobs = e2e_exempted_entry(7105, 1, "failure", 60, receipt)
    pending = {"id": 7106, "run_attempt": 1, "head_sha": HEAD, "event": "pull_request",
               "head_branch": REF, "path": ".github/workflows/e2e.yml",
               "status": "in_progress", "conclusion": None,
               "run_started_at": stamp(60), "pull_requests": [{"number": NUMBER}]}
    good = e2e_exempted_entry(7107, 1, "success", 60, receipt)
    for second in ((red_run, red_jobs), (pending, [])):
        entries = [good, second]
        scopes = {entry[1][0]["id"] if entry[1] else 0: receipt for entry in entries}
        install_e2e_duplicates(entries, scopes)
        assert merge.app_test_only_e2e_proof(make_pr(), e2e_classifier_proof(receipt)) is None, second[0]["id"]


def t38_duplicate_e2e_pair_executed_vs_exempt_still_refuses():
    receipt = _classifier_receipt_json()
    good = e2e_exempted_entry(7108, 1, "success", 60, receipt)
    executed_run, executed_jobs = e2e_substantive_entry(7109, 1, "success", 60)
    install_e2e_duplicates([good, (executed_run, executed_jobs)],
                           {good[1][0]["id"]: receipt, executed_jobs[0]["id"]: receipt})
    assert merge.app_test_only_e2e_proof(make_pr(), e2e_classifier_proof(receipt)) is None


def t39_three_identical_duplicate_e2e_exemptions_are_still_one_exemption():
    # The rule is "ALL of them are successful exemptions", not "exactly two":
    # a third identical same-second exemption at the same head must not turn an
    # otherwise-clean trio into a refusal.
    receipt = _classifier_receipt_json()
    entries = [e2e_exempted_entry(7110, 1, "success", 60, receipt),
               e2e_exempted_entry(7111, 1, "success", 60, receipt),
               e2e_exempted_entry(7112, 1, "success", 60, receipt)]
    install_e2e_duplicates(entries, {entry[1][0]["id"]: receipt for entry in entries})
    proof = merge.app_test_only_e2e_proof(make_pr(), e2e_classifier_proof(receipt))
    assert proof is not None and proof["skipped"] is True, proof
    assert proof["run"] == 7112, proof


def t40_third_duplicate_e2e_exemption_that_differs_still_refuses():
    # Ordering is irrelevant to the rule: whichever position the odd run takes,
    # a trio where one exemption's classifier output differs must refuse.
    base = _classifier_receipt_json()
    changed = dict(base, changed_files=[TEST_FILE, "app/page.tsx"])
    for receipts in ((base, base, changed), (base, changed, base), (changed, base, base)):
        entries = [e2e_exempted_entry(7113 + index, 1, "success", 60, base)
                   for index in range(3)]
        install_e2e_duplicates(
            entries,
            {entry[1][0]["id"]: receipt for entry, receipt in zip(entries, receipts)})
        assert merge.app_test_only_e2e_proof(make_pr(), e2e_classifier_proof(base)) is None, receipts


# ---- CTO rework refusal controls (ts 1795416141.013939) ----

def t41_gate_red_plus_tied_paid_success_without_native_verifier_refuses():
    # The CTO's t30 control: WITHOUT the current successful native required
    # verifier, a gate-only red plus a tied paid success alone must REFUSE.
    # The failed gate can also mean untrusted scope or invalid binding.
    run_set = pr14629_run_set(**{
        "ci.yml": [ci_verifier_entry(7021, 1, "failure", 60),
                   ci_substantive_entry(7022, 1, "success", 60)]})
    install_run_set(run_set)  # no ci-dummy.yml runs at all
    expect_refusal(lambda: merge.merge(NUMBER, HEAD), "VERIFIER_NOT_GREEN")


def t42_gate_red_plus_tied_paid_success_with_red_native_verifier_refuses():
    # The native verifier exists but its required-context test job is red: it
    # is not a CURRENT successful verifier, so the refusal stands.
    bad_ver, bad_jobs, lines = native_verifier_entry(DUMMY_RID + 1, 5, 90, test_gate="failure")
    DUMMY_LOGS.clear()
    DUMMY_LOGS[bad_jobs[0]["id"]] = lines
    run_set = pr14629_run_set(**{
        "ci.yml": [ci_verifier_entry(7023, 1, "failure", 60),
                   ci_substantive_entry(7024, 1, "success", 60)]})
    install_with_native_verifier(run_set, [(bad_ver, bad_jobs)])
    expect_refusal(lambda: merge.merge(NUMBER, HEAD), "VERIFIER_NOT_GREEN")


def t43_gate_red_plus_tied_paid_success_with_unbound_native_verifier_refuses():
    # The native verifier is green-shaped but bound to ANOTHER PR: not bound
    # to the exact PR/head, so it cannot supersede the gate-only red.
    other_ver, other_jobs, lines = native_verifier_entry(DUMMY_RID + 2, 5, 90)
    other_ver["pull_requests"] = [{"number": NUMBER + 1}]
    DUMMY_LOGS.clear()
    DUMMY_LOGS[other_jobs[0]["id"]] = lines
    run_set = pr14629_run_set(**{
        "ci.yml": [ci_verifier_entry(7025, 1, "failure", 60),
                   ci_substantive_entry(7026, 1, "success", 60)]})
    install_with_native_verifier(run_set, [(other_ver, other_jobs)])
    expect_refusal(lambda: merge.merge(NUMBER, HEAD), "VERIFIER_NOT_GREEN")


def t44_gate_red_plus_tied_paid_success_with_wrong_head_native_verifier_refuses():
    # The native verifier's classifier receipt is bound to a DIFFERENT head:
    # the authenticated binding (HEAD_SHA) fails, so it cannot supersede.
    wrong_ver, wrong_jobs, lines = native_verifier_entry(DUMMY_RID + 3, 5, 90,
                                                        receipt_head=OTHER)
    DUMMY_LOGS.clear()
    DUMMY_LOGS[wrong_jobs[0]["id"]] = lines
    run_set = pr14629_run_set(**{
        "ci.yml": [ci_verifier_entry(7027, 1, "failure", 60),
                   ci_substantive_entry(7028, 1, "success", 60)]})
    install_with_native_verifier(run_set, [(wrong_ver, wrong_jobs)])
    expect_refusal(lambda: merge.merge(NUMBER, HEAD), "VERIFIER_NOT_GREEN")


def t45_gate_red_plus_tied_paid_success_with_missing_classifier_receipt_refuses():
    # The native verifier ran but its classifier log has NO schema_version=1
    # receipt: nothing authenticates the scope decision, so it cannot supersede.
    no_ver, no_jobs, _lines = native_verifier_entry(DUMMY_RID + 4, 5, 90)
    DUMMY_LOGS.clear()
    DUMMY_LOGS[no_jobs[0]["id"]] = ["no classifier receipt here"]
    run_set = pr14629_run_set(**{
        "ci.yml": [ci_verifier_entry(7029, 1, "failure", 60),
                   ci_substantive_entry(7030, 1, "success", 60)]})
    install_with_native_verifier(run_set, [(no_ver, no_jobs)])
    expect_refusal(lambda: merge.merge(NUMBER, HEAD), "VERIFIER_NOT_GREEN")


def t46_native_verifier_control_plane_receipt_cannot_supersede():
    # Only a ci_required=true mixed/product receipt supports the paid lanes'
    # gate skip; a control_plane_only exemption receipt is a different fact.
    cp_receipt = {"schema_version": 1, "scope": "control_plane_only",
                  "control_plane_only": True, "paid_ci_exempt": True,
                  "product_changed": False, "ci_required": False,
                  "e2e_required": False, "test_only": False,
                  "rules_sha256": RULES_SHA, "changed_files": [TEST_FILE]}
    cp_ver, cp_jobs, lines = native_verifier_entry(DUMMY_RID + 5, 5, 90,
                                                   receipt=cp_receipt)
    DUMMY_LOGS.clear()
    DUMMY_LOGS[cp_jobs[0]["id"]] = lines
    run_set = pr14629_run_set(**{
        "ci.yml": [ci_verifier_entry(7031, 1, "failure", 60),
                   ci_substantive_entry(7032, 1, "success", 60)]})
    install_with_native_verifier(run_set, [(cp_ver, cp_jobs)])
    expect_refusal(lambda: merge.merge(NUMBER, HEAD), "VERIFIER_NOT_GREEN")


def t47_valid_14638_control_with_native_verifier_and_paid_run_proceeds():
    # The valid #14638 control: same-second gate-only CI sibling, tied bound
    # green paid run, AND the current successful native required verifier
    # (run 45108723956 attempt 5 shape) bound to the exact PR/head. READY.
    run, jobs, lines = native_verifier_entry(45108723956, 5, 90)
    DUMMY_LOGS.clear()
    DUMMY_LOGS[jobs[0]["id"]] = lines
    run_set = pr14629_run_set(**{
        "ci.yml": [ci_verifier_entry(45108847861, 1, "failure", 60),
                   ci_substantive_entry(45108848017, 1, "success", 60)]})
    install_with_native_verifier(run_set, [(run, jobs)])
    out = merge.merge(NUMBER, HEAD)
    assert out["status"] == "READY_TO_MERGE", out
    assert out["workflows"]["ci.yml"]["run"] == 45108848017, out["workflows"]


# ---- CTO rework refusal controls: E2E duplicate authentication ----

def e2e_skipped_aggregate_entry(run_id, attempt, minutes, receipt, with_steps=False,
                               receipt_head=HEAD, e2e_job_name="e2e"):
    """An e2e.yml run whose aggregate job is marked skipped (GitHub: all legs skipped)."""
    e2e_steps = ([_numbered_step(1, "Set up job", "success"),
                  _numbered_step(2, "Run E2E auto-process-critical", "success")]
                 if with_steps else [])
    jobs = [_job(SCOPE_JOB, "success", _scope_steps("e2e.yml"), run_id, attempt),
            _job(e2e_job_name, "skipped", e2e_steps, run_id, attempt),
            _job("teardown-pr-modal-apps", "skipped", (), run_id, attempt),
            _job("rerun-on-infra-failure", "skipped", (), run_id, attempt),
            _job("e2e-log-capture", "skipped", (), run_id, attempt)]
    run = {"id": run_id, "run_attempt": attempt, "head_sha": HEAD, "event": "pull_request",
           "head_branch": REF, "path": ".github/workflows/e2e.yml", "status": "completed",
           "conclusion": "success", "run_started_at": stamp(minutes),
           "pull_requests": [{"number": NUMBER}]}
    lines = ([json.dumps(dict(receipt)), f"BASE_SHA: {BASE}",
              f"HEAD_SHA: {receipt_head}", f"WORKFLOW_SHA: {WFSHA}",
              "EVENT_NAME: pull_request", f"PR_NUMBER: {NUMBER}"]
             if receipt is not None else ["no classifier receipt"])
    return run, jobs, lines


def t48_duplicate_e2e_with_wrong_classifier_head_sha_and_no_classify_step_refuses():
    # The CTO's first repro: a duplicate carrying a WRONG classifier HEAD_SHA
    # and NO classify step must refuse. The skipped-aggregate path must not
    # bypass the classifier-step and binding authentication.
    receipt = _classifier_receipt_json()
    good = e2e_exempted_entry(7201, 1, "success", 60, receipt)
    # Duplicate with a wrong HEAD_SHA in its log and a scope job whose steps
    # carry NO "Classify paid E2E scope" step at all.
    bad_run, bad_jobs, bad_lines = e2e_skipped_aggregate_entry(
        7202, 1, 60, dict(receipt), receipt_head=OTHER)
    bad_jobs[0]["steps"] = [_step("Set up job", "success"),
                            _step("Complete runner", "success"),
                            _step("Complete job", "success")]
    entries = [good, (bad_run, bad_jobs)]
    install_e2e_duplicates(entries, {good[1][0]["id"]: receipt, bad_jobs[0]["id"]: None})
    merge.job_log = lambda job_id: "\n".join(
        _classifier_lines(receipt) if job_id == good[1][0]["id"] else
        ([line for line in bad_lines if not line.startswith("{")]))
    assert merge.app_test_only_e2e_proof(make_pr(), e2e_classifier_proof(receipt)) is None


def t49_duplicate_e2e_marked_skipped_with_successful_browser_step_refuses():
    # The CTO's second repro: a duplicate marked skipped with a SUCCESSFUL
    # browser step must refuse. A skipped aggregate must have EMPTY steps.
    receipt = _classifier_receipt_json()
    good = e2e_exempted_entry(7203, 1, "success", 60, receipt)
    bad_run, bad_jobs, bad_lines = e2e_skipped_aggregate_entry(
        7204, 1, 60, dict(receipt), with_steps=True)
    entries = [good, (bad_run, bad_jobs)]
    install_e2e_duplicates(entries, {good[1][0]["id"]: receipt, bad_jobs[0]["id"]: receipt})
    merge.job_log = lambda job_id: "\n".join(
        _classifier_lines(receipt) if job_id == good[1][0]["id"] else bad_lines)
    assert merge.app_test_only_e2e_proof(make_pr(), e2e_classifier_proof(receipt)) is None


def t50_duplicate_e2e_skipped_aggregate_with_empty_steps_and_identical_identity_is_one():
    # The valid skipped-aggregate control: empty steps, authenticated identical
    # identity, non-null in every field. Still one exemption.
    receipt = _classifier_receipt_json()
    good = e2e_exempted_entry(7205, 1, "success", 60, receipt)
    skip_run, skip_jobs, skip_lines = e2e_skipped_aggregate_entry(
        7206, 1, 60, dict(receipt))
    entries = [good, (skip_run, skip_jobs)]
    install_e2e_duplicates(entries, {good[1][0]["id"]: receipt, skip_jobs[0]["id"]: receipt})
    merge.job_log = lambda job_id: "\n".join(
        _classifier_lines(receipt) if job_id == good[1][0]["id"] else skip_lines)
    proof = merge.app_test_only_e2e_proof(make_pr(), e2e_classifier_proof(receipt))
    assert proof is not None and proof["skipped"] is True, proof


def t51_duplicate_e2e_with_null_classifier_identity_refuses():
    # A candidate whose classifier identity has a NULL field (e.g. no rules
    # sha) is not identical to a non-null identity: dedup requires non-null.
    receipt = _classifier_receipt_json()
    null_receipt = dict(receipt, rules_sha256=None)
    good = e2e_exempted_entry(7207, 1, "success", 60, receipt)
    bad_run, bad_jobs, bad_lines = e2e_skipped_aggregate_entry(
        7208, 1, 60, dict(receipt))
    entries = [good, (bad_run, bad_jobs)]
    install_e2e_duplicates(entries, {good[1][0]["id"]: receipt, bad_jobs[0]["id"]: null_receipt})
    merge.job_log = lambda job_id: "\n".join(
        _classifier_lines(receipt) if job_id == good[1][0]["id"]
        else _classifier_lines(null_receipt))
    assert merge.app_test_only_e2e_proof(make_pr(), e2e_classifier_proof(receipt)) is None


# ---- CTO rework-4 refusal controls: newer unusable verifier must NOT fall through ----

def t52_newer_unusable_native_verifier_does_not_fall_through_to_older_valid_refuses():
    # The CTO's ambiguous-ordering repro: the NEWEST native verifier at the
    # head is unusable (a red test job), an OLDER valid green native verifier
    # exists below it. Without rework-4 the loop skips past the unusable one to
    # the older valid green; with it, the newest attempt is authoritative and
    # the gate-only red is NOT superseded -> refuse (VERIFIER_NOT_GREEN).
    bad_ver, bad_jobs, lines = native_verifier_entry(DUMMY_RID, 5, 100, test_gate="failure")
    DUMMY_LOGS.clear()
    DUMMY_LOGS[bad_jobs[0]["id"]] = lines
    good_ver, good_jobs, good_lines = native_verifier_entry(45200, 5, 90)
    DUMMY_LOGS[good_jobs[0]["id"]] = good_lines
    run_set = pr14629_run_set(**{
        "ci.yml": [ci_verifier_entry(7101, 1, "failure", 60),
                   ci_substantive_entry(7102, 1, "success", 60)]})
    install_with_native_verifier(run_set, [(bad_ver, bad_jobs), (good_ver, good_jobs)])
    expect_refusal(lambda: merge.merge(NUMBER, HEAD), "VERIFIER_NOT_GREEN")


def t53_newer_native_verifier_with_invalid_timestamp_does_not_fall_through_refuses():
    # The CTO's invalid-timestamp repro: the newest native verifier's
    # run_started_at is malformed (not an ISO 8601 Z stamp). The newest attempt
    # is authoritative, so an invalid timestamp refuses even if an older valid
    # green verifier exists below it -> refuse (VERIFIER_NOT_GREEN).
    bad_ver, bad_jobs, lines = native_verifier_entry(DUMMY_RID, 5, 100)
    bad_ver["run_started_at"] = "not-a-timestamp"
    DUMMY_LOGS.clear()
    DUMMY_LOGS[bad_jobs[0]["id"]] = lines
    good_ver, good_jobs, good_lines = native_verifier_entry(45201, 5, 90)
    DUMMY_LOGS[good_jobs[0]["id"]] = good_lines
    run_set = pr14629_run_set(**{
        "ci.yml": [ci_verifier_entry(7103, 1, "failure", 60),
                   ci_substantive_entry(7104, 1, "success", 60)]})
    install_with_native_verifier(run_set, [(bad_ver, bad_jobs), (good_ver, good_jobs)])
    expect_refusal(lambda: merge.merge(NUMBER, HEAD), "VERIFIER_NOT_GREEN")


def t54_newer_native_verifier_missing_attempt_metadata_refuses():
    # The CTO's missing-attempt repro: the newest native verifier omits
    # run_attempt. The CTO made run_attempt a required native attempt metadata
    # field, so the newest attempt is refused regardless of an older valid green
    # verifier below it -> refuse (VERIFIER_NOT_GREEN).
    bad_ver, bad_jobs, lines = native_verifier_entry(DUMMY_RID, 5, 100)
    bad_ver["run_attempt"] = None
    DUMMY_LOGS.clear()
    DUMMY_LOGS[bad_jobs[0]["id"]] = lines
    good_ver, good_jobs, good_lines = native_verifier_entry(45202, 5, 90)
    DUMMY_LOGS[good_jobs[0]["id"]] = good_lines
    run_set = pr14629_run_set(**{
        "ci.yml": [ci_verifier_entry(7105, 1, "failure", 60),
                   ci_substantive_entry(7106, 1, "success", 60)]})
    install_with_native_verifier(run_set, [(bad_ver, bad_jobs), (good_ver, good_jobs)])
    expect_refusal(lambda: merge.merge(NUMBER, HEAD), "VERIFIER_NOT_GREEN")


def t55_newest_native_verifier_is_the_valid_green_one_proceeds():
    # Positive control: the newest native verifier IS the valid green one. It
    # supersedes the same-second gate-only red -> READY. (t52-t54 prove the
    # refusals; t47 + t55 together show the fix does not over-close.)
    run, jobs, lines = native_verifier_entry(45203, 5, 100)
    DUMMY_LOGS.clear()
    DUMMY_LOGS[jobs[0]["id"]] = lines
    run_set = pr14629_run_set(**{
        "ci.yml": [ci_verifier_entry(7107, 1, "failure", 60),
                   ci_substantive_entry(7108, 1, "success", 60)]})
    install_with_native_verifier(run_set, [(run, jobs)])
    out = merge.merge(NUMBER, HEAD)
    assert out["status"] == "READY_TO_MERGE", out
    assert out["workflows"]["ci.yml"]["run"] == 7108, out["workflows"]


# ---- CTO rework-5 refusal controls (ts 1795444808.341169) ----
# One fact per control. Each is RED on merge.py b24e34c9 and GREEN on the rework.

def _rework5_ci(native):
    """Same-second gate-only red + tied paid green, plus the given ci-dummy runs."""
    DUMMY_LOGS.clear()
    for _run_, jobs, lines in native:
        DUMMY_LOGS[jobs[0]["id"]] = lines
    run_set = pr14629_run_set(**{
        "ci.yml": [ci_verifier_entry(7401, 1, "failure", 60),
                   ci_substantive_entry(7402, 1, "success", 60)]})
    install_with_native_verifier(run_set, [(run, jobs) for run, jobs, _ in native])


def t56_native_verifier_invalid_timestamp_sorting_below_a_valid_one_refuses():
    # An invalid stamp that string-sorts BELOW the valid green run used to be
    # ordered away and ignored. Every matching stamp is validated first.
    good = native_verifier_entry(45401, 5, 100)
    stale = native_verifier_entry(45400, 5, 90)
    stale[0]["run_started_at"] = "2026-11-23 10:00:00"  # no T/Z: sorts below any valid stamp
    _rework5_ci([good, stale])
    expect_refusal(lambda: merge.merge(NUMBER, HEAD), "VERIFIER_NOT_GREEN")


def t57_native_verifier_same_second_red_at_a_lower_run_id_refuses():
    # Two candidates in the newest second are ambiguous. The higher run id must
    # never win the tie, even when it is the green one.
    green = native_verifier_entry(45411, 5, 100)
    red = native_verifier_entry(45410, 5, 100, test_gate="failure")
    red[0]["conclusion"] = "failure"
    _rework5_ci([green, red])
    expect_refusal(lambda: merge.merge(NUMBER, HEAD), "VERIFIER_NOT_GREEN")


def t58_native_verifier_missing_timestamp_refuses_without_keyerror():
    good = native_verifier_entry(45421, 5, 100)
    nostamp = native_verifier_entry(45420, 5, 90)
    del nostamp[0]["run_started_at"]
    _rework5_ci([good, nostamp])
    expect_refusal(lambda: merge.merge(NUMBER, HEAD), "VERIFIER_NOT_GREEN")


def t59_native_verifier_run_attempt_must_be_a_positive_integer():
    # Jobs carry the same attempt value as the run, so only the attempt's type
    # and sign are in question (never the job/run binding).
    for bad in (0, -1, "5", 5.0, True):
        run, jobs, lines = native_verifier_entry(45430, 5, 100)
        run["run_attempt"] = bad
        for job in jobs:
            job["run_attempt"] = bad
        _rework5_ci([(run, jobs, lines)])
        try:
            expect_refusal(lambda: merge.merge(NUMBER, HEAD), "VERIFIER_NOT_GREEN")
        except Exception as exc:  # noqa: BLE001 - name the offending value
            raise AssertionError(f"run_attempt={bad!r}: {type(exc).__name__}: {exc}") from exc


def _install_skipped_e2e(entries):
    """e2e.yml skipped-aggregate runs: [(run, jobs, classifier log lines), ...]."""
    merge.pages = lambda path, key: (
        [run for run, _, _ in entries] if "/runs?" in path else
        next(jobs for run, jobs, _ in entries
             if run["id"] == int(path.split("actions/runs/")[1].split("/")[0])))
    logs = {jobs[0]["id"]: "\n".join(lines) for _, jobs, lines in entries}
    merge.job_log = lambda job_id: logs[job_id]
    merge.command = lambda *args: json.dumps([[{"filename": TEST_FILE}]])


def t60_sole_skipped_e2e_aggregate_with_only_head_sha_wrong_refuses():
    # Green classifier step, valid receipt, skipped shape: ONLY the logged
    # HEAD_SHA is wrong. The skipped shape must not bypass authentication.
    receipt = _classifier_receipt_json()
    entry = e2e_skipped_aggregate_entry(7501, 1, 60, dict(receipt), receipt_head=OTHER)
    _install_skipped_e2e([entry])
    assert merge.app_test_only_e2e_proof(make_pr(), e2e_classifier_proof(receipt)) is None


def t61_two_skipped_e2e_runs_sharing_the_same_wrong_head_refuse():
    # Identical wrong identity on both runs is equality, not authentication.
    receipt = _classifier_receipt_json()
    entries = [e2e_skipped_aggregate_entry(7502, 1, 60, dict(receipt), receipt_head=OTHER),
               e2e_skipped_aggregate_entry(7503, 1, 60, dict(receipt), receipt_head=OTHER)]
    _install_skipped_e2e(entries)
    assert merge.app_test_only_e2e_proof(make_pr(), e2e_classifier_proof(receipt)) is None


def t62_sole_and_paired_authenticated_skipped_e2e_aggregates_still_proceed():
    # Green controls for the skipped shape: the correct head must not over-close.
    receipt = _classifier_receipt_json()
    _install_skipped_e2e([e2e_skipped_aggregate_entry(7504, 1, 60, dict(receipt))])
    proof = merge.app_test_only_e2e_proof(make_pr(), e2e_classifier_proof(receipt))
    assert proof is not None and proof["skipped"] is True and proof["run"] == 7504, proof
    _install_skipped_e2e([e2e_skipped_aggregate_entry(7505, 1, 60, dict(receipt)),
                          e2e_skipped_aggregate_entry(7506, 1, 60, dict(receipt))])
    proof = merge.app_test_only_e2e_proof(make_pr(), e2e_classifier_proof(receipt))
    assert proof is not None and proof["skipped"] is True and proof["run"] == 7506, proof


def t63_skipped_e2e_aggregate_authenticates_pr_event_files_rules_and_workflow():
    # The rest of the authenticated bindings, for the skipped shape.
    receipt = _classifier_receipt_json()
    cases = {
        "pr_number": (dict(receipt), lambda line: f"PR_NUMBER: {NUMBER + 1}"
                      if line.startswith("PR_NUMBER") else line),
        "event_name": (dict(receipt), lambda line: "EVENT_NAME: push"
                       if line.startswith("EVENT_NAME") else line),
        "workflow_sha": (dict(receipt), lambda line: f"WORKFLOW_SHA: {'9' * 40}"
                         if line.startswith("WORKFLOW_SHA") else line),
        "receipt_files": (dict(receipt, changed_files=[TEST_FILE, "app/page.tsx"]), None),
        "rules_sha": (dict(receipt, rules_sha256="f" * 64), None),
    }
    for label, (case_receipt, rewrite) in cases.items():
        run, jobs, lines = e2e_skipped_aggregate_entry(7507, 1, 60, case_receipt)
        if rewrite is not None:
            lines = [rewrite(line) for line in lines]
        _install_skipped_e2e([(run, jobs, lines)])
        assert merge.app_test_only_e2e_proof(
            make_pr(), e2e_classifier_proof(receipt)) is None, label


# ---- carried-over unit-gate attempt (run 45305541606, attempt 2) ----
# GitHub's jobs API rewrites run_attempt to the latest attempt and mints new
# job ids for jobs it did not re-execute. The reissued jobs keep the original
# start and completion. PR #14582 attempt 2 carried the attempt-1 unit gate
# (receipt run_attempt "1"; gate 138159904929 and reissued 138167137778 both
# completed at 01:59:36Z, before attempt 2 began at 02:16:57Z). A lower attempt
# number is not enough: that attempt's own jobs must show the same execution.


_UNSET = object()


def _unit_schedule_log(run_id, head, receipt_attempt, count=3):
    shards = [f"unit-tests ({n})" for n in range(1, count + 1)]
    receipt = {"skip_matrix": False, "scheduled_count": count, "mode": "selected",
               "zero_proof": "", "reason": "selected", "head": head,
               "run_id": str(run_id), "run_attempt": str(receipt_attempt)}
    body = json.dumps(receipt, indent=2).splitlines()
    lines = [f"  CI_EXECUTION_SHA: {head}",
             "  UNIT_SELECT_RESULT: success",
             "  UNIT_PRECHECKS_RESULT: success",
             "  UNIT_TESTS_RESULT: success",
             f"  UNIT_SCHEDULED_COUNT: {count}",
             f"  EXPECTED_RUN_ID: {run_id}",
             f"  UNIT_JOB_HEAD_SHA: {head}",
             "  UNIT_SCHEDULE: " + body[0],
             *body[1:],
             json.dumps({"event": "unit_jobs_verified", "run_id": str(run_id),
                         "head_sha": head, "declared_count": count,
                         "observed_jobs": shards}, separators=(",", ":"))]
    return "\n".join(lines)


def _carried_unit_world(run_attempt, receipt_attempt, *, gate_started, shard_started,
                        gate_completed=_UNSET, shard_completed=_UNSET, historical="mirror",
                        attempt_starts=None):
    """Dynamic unit lane: three shards plus the jobs verify_ci_required_jobs reads.

    historical="mirror" is the named attempt's own jobs for this same execution,
    under new ids. "earlier" is a different execution on that attempt.
    "shard-mismatch" keeps the gate and swaps one shard. attempt_starts is the
    GitHub attempt record's run_started_at keyed by attempt number. The default
    opens the named attempt at the gate's own start, so a mirrored execution
    sits inside that attempt. The jobs list still copies those timestamps into
    every later attempt; only the attempt record says which one ran the gate.
    """
    if gate_completed is _UNSET:
        gate_completed = gate_started
    if shard_completed is _UNSET:
        shard_completed = shard_started
    run_id = 88064
    run = {"id": run_id, "run_attempt": run_attempt, "head_sha": HEAD,
           "run_started_at": stamp(40)}
    jobs = []

    def add(name, conclusion, steps, started=None, completed=None):
        job = _job(name, conclusion, steps, run_id, run_attempt)
        if started is not None:
            job["started_at"] = started
        if completed is not None:
            job["completed_at"] = completed
        jobs.append(job)
        return job

    add("typescript", "success", [_step("Typecheck", "success")])
    add("unit-tests-isolated", "success", [_step("Isolated unit tests", "success")])
    add("unit-select", "success", [_step("Emit trusted unit matrix", "success")],
        gate_started, gate_completed)
    for number in (1, 2, 3):
        add(f"unit-tests ({number})", "success", [_step("Unit tests", "success")],
            shard_started, shard_completed)
    gate = add("unit-gate", "success",
               [_step("Validate unit lane and scheduled jobs", "success")],
               gate_started, gate_completed)
    for name in ("python", "python-docx", "python-contracts",
                 "python-modal (0)", "python-modal (1)", "python-modal (2)"):
        add(name, "skipped", [])
    add("test", "success", [_step("Require all CI phases", "success"),
                            _step("Confirm current paid CI success", "success")])

    def pages(path, key):
        pages.calls.append(path)
        assert key == "jobs", key
        assert f"actions/runs/{run_id}/attempts/{receipt_attempt}/jobs?" in path, path
        mirrored = []
        for job in jobs:
            if job["name"] != "unit-gate" and not merge.unit_family(job["name"]):
                continue
            copy = {"id": job["id"] + 10_000_000, "name": job["name"],
                    "status": job["status"], "conclusion": job["conclusion"],
                    "steps": [dict(step) for step in job.get("steps", [])],
                    "run_id": job["run_id"], "run_attempt": receipt_attempt,
                    "head_sha": job["head_sha"], "started_at": job.get("started_at"),
                    "completed_at": job.get("completed_at")}
            if historical == "earlier":
                copy["started_at"] = stamp(3)
                copy["completed_at"] = stamp(4)
            elif historical == "shard-mismatch" and job["name"] == "unit-tests (2)":
                copy["started_at"] = stamp(3)
                copy["completed_at"] = stamp(4)
            mirrored.append(copy)
        return mirrored

    pages.calls = []
    merge.pages = pages
    if attempt_starts is None:
        attempt_starts = {receipt_attempt: gate_started} if isinstance(gate_started, str) else {}

    def api(path):
        api.calls.append(path)
        prefix = f"actions/runs/{run_id}/attempts/"
        assert path.startswith(prefix), path
        asked = int(path[len(prefix):])
        opened = attempt_starts.get(asked)
        if opened is None:
            return None
        return {"id": run_id, "run_attempt": asked, "head_sha": HEAD,
                "run_started_at": opened}

    api.calls = []
    merge.api = api
    merge.job_log = lambda job_id: (
        _unit_schedule_log(run_id, HEAD, receipt_attempt) if job_id == gate["id"] else "")
    required = {"head": HEAD, "ts_changed": True, "python_changed": False}
    return run, jobs, required


def t64_carried_over_attempt_receipt_attests_shards_that_actually_ran():
    # The attempt-2 jobs API says run_attempt 2; the gate log, written on
    # attempt 1, says "1". The named attempt's jobs match this execution.
    run, jobs, required = _carried_unit_world(
        2, 1, gate_started=stamp(0), shard_started=stamp(0),
        gate_completed=stamp(10), shard_completed=stamp(10))
    assert merge.unit_gate_attested_count(run, jobs, required) == 3
    assert any("/attempts/1/jobs?" in path for path in merge.pages.calls), merge.pages.calls
    merge.verify_ci_required_jobs(run, jobs, required)


def t65_reexecuted_gate_with_an_older_receipt_attempt_still_refuses():
    run, jobs, required = _carried_unit_world(
        2, 1, gate_started=stamp(40), shard_started=stamp(40))
    assert merge.unit_gate_attested_count(run, jobs, required) is None
    try:
        merge.verify_ci_required_jobs(run, jobs, required)
    except merge.Refusal as exc:
        assert str(exc) == "UNIT_ATTESTATION_REQUIRED run=88064", exc
    else:
        raise AssertionError("re-executed gate accepted an attempt-1 receipt")


def t66_carried_over_gate_with_a_reexecuted_shard_still_refuses():
    # A shard that actually ran on this attempt is not covered by the older gate.
    run, jobs, required = _carried_unit_world(
        2, 1, gate_started=stamp(0), shard_started=stamp(0))
    for job in jobs:
        if job["name"] == "unit-tests (2)":
            job["started_at"] = stamp(40)
    assert merge.unit_gate_attested_count(run, jobs, required) is None


def t67_carried_over_gate_claiming_the_current_attempt_still_refuses():
    # started_at is before this attempt, so a receipt numbered as this attempt is a lie.
    run, jobs, required = _carried_unit_world(
        2, 2, gate_started=stamp(0), shard_started=stamp(0))
    assert merge.unit_gate_attested_count(run, jobs, required) is None


def t68_reexecuted_gate_whose_receipt_matches_this_attempt_attests():
    run, jobs, required = _carried_unit_world(
        2, 2, gate_started=stamp(41), shard_started=stamp(41))
    assert merge.unit_gate_attested_count(run, jobs, required) == 3
    merge.verify_ci_required_jobs(run, jobs, required)


def t69_same_attempt_receipt_without_timestamps_still_attests():
    # Pre-existing fixtures omit started_at. Equal attempts stay valid, padded
    # attempt text does not. No historical lookup on this path.
    run, jobs, required = _carried_unit_world(
        1, 1, gate_started=None, shard_started=None)
    assert merge.unit_gate_attested_count(run, jobs, required) == 3
    assert merge.pages.calls == []
    run, jobs, required = _carried_unit_world(
        1, "01", gate_started=None, shard_started=None)
    assert merge.unit_gate_attested_count(run, jobs, required) is None
    assert merge.pages.calls == []


def t70_wrong_historical_attempt_does_not_bind():
    # Attempt 3 reissues a gate that executed on attempt 2. A receipt naming
    # attempt 1 must not bind just because attempt 1 also has a successful gate:
    # that gate is an earlier execution (different start and completion).
    run, jobs, required = _carried_unit_world(
        3, 1, gate_started=stamp(0), shard_started=stamp(0),
        gate_completed=stamp(10), shard_completed=stamp(10), historical="earlier")
    assert merge.unit_gate_attested_count(run, jobs, required) is None
    assert any("/attempts/1/jobs?" in path for path in merge.pages.calls), merge.pages.calls


def t71_carried_gate_without_completion_does_not_bind():
    # A start before this attempt, with no completion, is not a carried execution.
    run, jobs, required = _carried_unit_world(
        2, 1, gate_started=stamp(0), shard_started=stamp(0),
        gate_completed=None, shard_completed=stamp(10))
    assert merge.unit_gate_attested_count(run, jobs, required) is None


def t72_carried_gate_completing_after_attempt_start_does_not_bind():
    # Started before this attempt, but finished after the attempt began.
    run, jobs, required = _carried_unit_world(
        2, 1, gate_started=stamp(0), shard_started=stamp(0),
        gate_completed=stamp(41), shard_completed=stamp(10))
    assert merge.unit_gate_attested_count(run, jobs, required) is None


def t73_carried_gate_does_not_cover_a_different_shard_execution():
    # The named attempt's gate is this execution; one shard is not.
    run, jobs, required = _carried_unit_world(
        3, 2, gate_started=stamp(0), shard_started=stamp(0),
        gate_completed=stamp(10), shard_completed=stamp(10),
        historical="shard-mismatch")
    assert merge.unit_gate_attested_count(run, jobs, required) is None


def t74_copied_gate_binds_only_the_attempt_whose_window_contains_it():
    # The gate ran in attempt 1. Attempts 2 and 3 reissue that row with the
    # same timestamps, so a jobs-list match cannot tell them apart. At attempt
    # 3, naming 2 must refuse and naming 1 must pass. The shards started before
    # attempt 1 and were copied in with the gate; that still belongs to attempt 1.
    starts = {1: stamp(0), 2: stamp(20), 3: stamp(40)}
    common = dict(gate_started=stamp(5), shard_started=stamp(-5),
                  gate_completed=stamp(10), shard_completed=stamp(4),
                  attempt_starts=starts)
    run, jobs, required = _carried_unit_world(3, 2, **common)
    refused = merge.unit_gate_attested_count(run, jobs, required)
    assert refused is None, refused
    assert "actions/runs/88064/attempts/2" in merge.api.calls, merge.api.calls
    run, jobs, required = _carried_unit_world(3, 1, **common)
    assert merge.unit_gate_attested_count(run, jobs, required) == 3
    assert "actions/runs/88064/attempts/1" in merge.api.calls, merge.api.calls
    merge.verify_ci_required_jobs(run, jobs, required)


def t75_ci_label_verdict_with_skipped_literal_reads_uses_only_the_executed_attempt():
    def rerun(conclusion="success", minutes=40):
        run, jobs = ci_verifier_entry(8002, 2, conclusion, minutes)
        jobs.append(_job("literal-test-reads", "skipped", (), run["id"], run["run_attempt"]))
        return run, jobs

    def proof(*entries):
        install_run_set({"ci.yml": entries})
        return merge.workflow_proof(make_pr(), "ci.yml", merge.WORKFLOWS["ci.yml"])

    paid = ci_substantive_entry(8001, 1, "success", 20)
    for verifier in (rerun(), rerun(minutes=20)):
        assert proof(verifier, paid) == {"run": paid[0]["id"], "attempt": 1, "head": HEAD}

    expect_refusal(lambda: proof(rerun()), "WORKFLOW_MISSING workflow=ci.yml")
    wrong_head = ci_substantive_entry(8001, 1, "success", 20)
    wrong_head[0]["head_sha"] = OTHER
    expect_refusal(lambda: proof(rerun(), wrong_head), "WORKFLOW_MISSING workflow=ci.yml")
    red = ci_substantive_entry(8001, 1, "failure", 20)
    expect_refusal(lambda: proof(rerun(), red), "WORKFLOW_NOT_GREEN workflow=ci.yml")
    expect_refusal(lambda: proof(rerun("failure"), paid), "VERIFIER_SHAPE_AMBIGUOUS")

    ran_literal_reads = rerun()
    literal = ran_literal_reads[1][-1]
    literal.update(conclusion="success", steps=[_step("Generate literal test-read set", "success")])
    expect_refusal(lambda: proof(ran_literal_reads, paid), "VERIFIER_SHAPE_AMBIGUOUS")


def t76_ci_label_verdict_with_skipped_convex_deploy_check_pair_uses_the_paid_run():
    # #15010 / #14931 / #15008 (CTO ts 1795717168.102149): the admission rerun also lists
    # convex-deploy-check + teardown-convex-deploy-check, skipped with zero steps.
    def rerun(conclusion="success", minutes=40, pair=("convex-deploy-check", "teardown-convex-deploy-check")):
        run, jobs = ci_verifier_entry(8002, 2, conclusion, minutes)
        for name in ("literal-test-reads",) + tuple(pair):
            jobs.append(_job(name, "skipped", (), run["id"], run["run_attempt"]))
        return run, jobs

    def proof(*entries):
        install_run_set({"ci.yml": entries})
        return merge.workflow_proof(make_pr(), "ci.yml", merge.WORKFLOWS["ci.yml"])

    paid = ci_substantive_entry(8001, 1, "success", 20)
    # Both green: the authenticated wrapper yields to the run that executed the tests.
    assert proof(rerun(), paid) == {"run": paid[0]["id"], "attempt": 1, "head": HEAD}
    # One red: a red paid run still refuses; a red wrapper is never authenticated.
    expect_refusal(lambda: proof(rerun(), ci_substantive_entry(8001, 1, "failure", 20)),
                   "WORKFLOW_NOT_GREEN workflow=ci.yml")
    expect_refusal(lambda: proof(rerun("failure"), paid), "VERIFIER_SHAPE_AMBIGUOUS")
    # None substantive: the wrapper alone never proves CI.
    expect_refusal(lambda: proof(rerun()), "WORKFLOW_MISSING workflow=ci.yml")
    # Pending wrapper is not authenticated.
    pending = rerun()
    pending[0].update(status="in_progress", conclusion=None)
    expect_refusal(lambda: proof(pending, paid), "VERIFIER_SHAPE_AMBIGUOUS")
    # Only one of the pair, or a pair job that ran / belongs to another attempt, refuses.
    expect_refusal(lambda: proof(rerun(pair=("convex-deploy-check",)), paid), "VERIFIER_SHAPE_AMBIGUOUS")
    for mutate in (lambda j: j.update(conclusion="success", steps=[_step("Deploy check", "success")]),
                   lambda j: j.update(steps=[_step("Deploy check", "skipped")]),
                   lambda j: j.update(run_attempt=1)):
        bad = rerun()
        mutate(next(j for j in bad[1] if j["name"] == "teardown-convex-deploy-check"))
        expect_refusal(lambda: proof(bad, paid), "VERIFIER_SHAPE_AMBIGUOUS")


def _tests():
    return sorted((k, v) for k, v in list(globals().items())
                  if len(k) > 1 and k[0] == "t" and k[1].isdigit() and callable(v))


import unittest  # noqa: E402


class MergeReviewGateRemovalTests(unittest.TestCase):
    def test_all_focused_checks(self):
        for name, fn in _tests():
            with self.subTest(check=name):
                fn()


if __name__ == "__main__":
    for name, fn in _tests():
        check(name, fn)
    failed = [result for result in RESULTS if not result[1]]
    for name, ok, detail in RESULTS:
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else " :: " + detail))
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    sys.exit(1 if failed else 0)
