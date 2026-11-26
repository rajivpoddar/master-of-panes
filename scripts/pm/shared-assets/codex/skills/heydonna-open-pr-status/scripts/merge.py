#!/usr/bin/env python3
"""Verify actual CI/E2E execution and squash a PR with a head pin."""

import argparse
import datetime
import fnmatch
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from classifier_requirements import classify as workflow_requirements

REPO = "Scribie/heydonna-app"
# The pre-transfer name stays accepted: GitHub can still report it for the same repository.
SAME_REPO_NAMES = {REPO.lower(), "heydonna-app/heydonna-app"}
WORKFLOWS = {"ci.yml": ("typescript", "python", "test"), "e2e.yml": ("e2e",)}
SITE_EXEMPTION_WORKFLOW = "ci-dummy.yml"
SITE_CLASSIFIER_JOB_NAME = "detect-docs-only"
SITE_CLASSIFIER_TEST_STEP = "Run classifier unit tests (every test that exercises change_scope.py)"


class Refusal(RuntimeError):
    pass


def command(*args):
    result = subprocess.run(args, text=True, capture_output=True, timeout=120)
    if result.returncode:
        raise Refusal(result.stderr.strip() or result.stdout.strip() or "command failed")
    return result.stdout


API_RETRY_DELAY = 0.5


class ReadFailed(Refusal):
    """A GitHub read that failed on BOTH attempts: a non-zero gh exit or a
    subprocess timeout. Typed so the proof path can name it, instead of leaking
    a raw gh error or, for a timeout, an uncaught subprocess.TimeoutExpired."""


class GitCommitsReadFailed(ReadFailed):
    """git/commits/<sha> unreadable on both attempts (the parent read the
    synthetic-merge ancestry check depends on)."""


def read_with_retry(fn, *args):
    """One retry for a read the proof path would otherwise swallow into "unproven".

    command() raises Refusal for any non-zero gh exit, so a transient GitHub
    error (5xx, rate limit, network blip) on a compare / ref / file-list read
    inside the swallowing proofs used to look exactly like a failed proof and
    surfaced as a generic WORKFLOW_NOT_GREEN on a genuinely green run (PR #14762,
    run 45385386399). command() also runs gh with a 120 s timeout, and a
    subprocess.TimeoutExpired is not a Refusal, so a timeout used to escape the
    retry on the first attempt. Both a Refusal and a TimeoutExpired are retried
    once; a second failure raises the typed ReadFailed (cause chained), so the
    proof stays fail-closed and names what failed."""
    try:
        return fn(*args)
    except (Refusal, subprocess.TimeoutExpired):
        time.sleep(API_RETRY_DELAY)
    try:
        return fn(*args)
    except (Refusal, subprocess.TimeoutExpired) as exc:
        cause = (f"timeout after {exc.timeout}s" if isinstance(exc, subprocess.TimeoutExpired)
                 else str(exc))
        raise ReadFailed(f"READ_FAILED attempts=2 read={' '.join(map(str, args))[:200]} "
                         f"cause={cause[:200]}") from exc


def commit_parents(sha):
    """Parents of git/commits/<sha>, retried once; a persistent failure or
    timeout raises the typed GitCommitsReadFailed. Callers feed the result to
    merge_parent_base, whose ancestry and PR-head checks are unchanged."""
    try:
        return read_with_retry(api, f"git/commits/{sha}").get("parents", [])
    except ReadFailed as exc:
        raise GitCommitsReadFailed(str(exc)) from exc


def api(path):
    return json.loads(command("gh", "api", f"repos/{REPO}/{path}"))


def pages(path, key):
    result = json.loads(command("gh", "api", "--paginate", "--slurp", f"repos/{REPO}/{path}"))
    return [item for page in result for item in page[key]]


def pr_at(number, head):
    pr = api(f"pulls/{number}")
    if pr["head"]["sha"] != head:
        raise Refusal("HEAD_CHANGED")
    if pr.get("merged"):
        return pr
    if pr["state"] != "open" or pr["draft"]:
        raise Refusal("PR_NOT_OPEN_NON_DRAFT")
    # The canonical repository is Scribie/heydonna-app; the pre-transfer heydonna-app/heydonna-app alias also resolves to it.
    head_repo = pr["head"].get("repo") or {}
    base_repo = pr["base"].get("repo") or {}
    same_repo = (head_repo.get("full_name") or "").lower() in SAME_REPO_NAMES and (
        head_repo.get("id") is None or base_repo.get("id") is None or head_repo.get("id") == base_repo.get("id"))
    if pr["base"]["ref"] != "main" or not same_repo:
        raise Refusal("PR_MUST_TARGET_MAIN_IN_SAME_REPOSITORY")
    return pr


def successful(item):
    return item.get("status") == "completed" and item.get("conclusion") == "success"


def bound_attempt_jobs(pr, run, jobs, *, allow_failed=False):
    """Bind every native job to the selected exact PR, head, run and attempt."""
    return (native_pr_binding_ok(run, pr) and bool(jobs)
            and all(j.get("id") is not None and j.get("name") for j in jobs)
            and len({j.get("id") for j in jobs}) == len(jobs)
            and len({j.get("name") for j in jobs}) == len(jobs)
            and all(j.get("run_id") == run.get("id") and j.get("run_attempt") == run.get("run_attempt", 1)
                    and j.get("head_sha") == pr["head"]["sha"] and j.get("status") == "completed"
                    and j.get("conclusion") in (("success", "skipped", "failure") if allow_failed else ("success", "skipped")) for j in jobs))


def verifier_job_names(workflow):
    return ({"classify-change-scope", "test", "ci-infra", "unit-tests-isolated", "unit-select", "python-contracts", "unit-prechecks", "typescript", "python-docx", "unit-gate", "theme-palette", "python-modal", "python", "unit-tests (${{ matrix.shard }})", "unit-cost-receipt", "ci-receipt-publish", "functional-selection-manifest"} if workflow == "ci.yml" else {"classify-change-scope", "teardown-pr-modal-apps", "e2e", "rerun-on-infra-failure", "e2e-log-capture"})


CI_LITERAL_READS_JOB = "literal-test-reads"
CI_CONVEX_DEPLOY_CHECK_JOBS = frozenset({"convex-deploy-check", "teardown-convex-deploy-check"})
LEGACY_CI_VERIFIER_JOBS = {"test", "classify-change-scope", "python-contracts", "unit-tests", "unit-tests-isolated", "typescript", "python-docx", "python", "theme-palette", "ci-receipt-publish", "functional-selection-manifest", "ci-infra", "python-modal"}
LEGACY_CI_WORKFLOW_BLOB = "eb973c3382478b845d1d0541f12f95ee4efc7d33"


def legacy_ci_roster_authenticated(pr, run, jobs):
    """Accept the complete older roster only for its bound workflow blob."""
    if {j.get("name") for j in jobs} != LEGACY_CI_VERIFIER_JOBS:
        return False
    scope = next(j for j in jobs if j.get("name") == CLASSIFIER_JOB_NAME)
    bindings = classifier_bindings(strip_log_framing(job_log(scope["id"])))
    if not bindings or bindings["HEAD_SHA"] != pr["head"]["sha"] or not event_binding_ok(bindings, run, pr) or not pr_binding_ok(bindings, run, pr):
        return False
    base, execution = bindings["BASE_SHA"], bindings["WORKFLOW_SHA"]
    if not HEX40.fullmatch(base) or not HEX40.fullmatch(execution):
        return False
    trusted = merge_parent_base(base, pr["head"]["sha"], commit_parents(execution))
    if trusted is None:
        return False
    path = ".github/workflows/ci.yml"
    at_base, at_execution = contents_identity(path, trusted), contents_identity(path, execution)
    return bool(at_base and at_execution and at_base[0] == at_execution[0] == LEGACY_CI_WORKFLOW_BLOB)


def verifier_only_outcome(pr, workflow, run, jobs):
    """Identify a completed native gate-only sibling, never paid execution."""
    outcome = run.get("conclusion")
    if run.get("status") != "completed" or outcome not in ("success", "failure"): return None
    aggregate = "test" if workflow == "ci.yml" else "e2e"
    if not native_pr_binding_ok(run, pr) or not jobs or any(j.get("id") is None or not j.get("name") for j in jobs): return None
    if len({j.get("id") for j in jobs}) != len(jobs) or len({j.get("name") for j in jobs}) != len(jobs): return None
    if any(j.get("run_id") != run.get("id") or j.get("run_attempt") != run.get("run_attempt", 1)
           or j.get("head_sha") != pr["head"]["sha"] or j.get("status") != "completed"
           or j.get("conclusion") not in ((outcome,) if j.get("name") == aggregate else ("success", "skipped")) for j in jobs): return None
    by_name = {j["name"]: j for j in jobs}
    expected = verifier_job_names(workflow)
    # The label-verdict rerun also lists literal-test-reads. Like the rest
    # of the paid roster it must be skipped with no steps (checked below).
    # The current ci.yml rerun also lists the convex deploy-check pair; it is
    # accepted only with BOTH jobs present, each skipped with zero steps, at
    # this run id/attempt/head (CTO C0ALZJHGE49 ts 1795717168.102149).
    rerun_rosters = (expected | {CI_LITERAL_READS_JOB},
                     expected | {CI_LITERAL_READS_JOB} | CI_CONVEX_DEPLOY_CHECK_JOBS)
    green_ci_rerun = (workflow == "ci.yml" and outcome == "success"
                      and any(set(by_name) == roster for roster in rerun_rosters))
    if green_ci_rerun and any(by_name[n].get("conclusion") != "skipped" or by_name[n].get("steps") != []
                              for n in CI_CONVEX_DEPLOY_CHECK_JOBS & set(by_name)): return None
    if (set(by_name) != expected and not green_ci_rerun
            and not (workflow == "ci.yml" and legacy_ci_roster_authenticated(pr, run, jobs))): return None
    scope=by_name.get(CLASSIFIER_JOB_NAME,{})
    scope_step="Classify paid CI scope" if workflow == "ci.yml" else "Classify paid E2E scope"
    if not successful(scope) or len([s for s in scope.get("steps",[]) if s.get("name") == scope_step and successful(s)]) != 1: return False
    name="test" if workflow == "ci.yml" else "e2e"
    job=by_name.get(name,{})
    if job.get("conclusion") != outcome: return None
    steps=job.get("steps",[])
    if not steps or len({s.get("name") for s in steps}) != len(steps): return False
    gate="Require exemption or exact-head real CI success" if workflow == "ci.yml" else "Require exemption or exact-head real E2E success"
    skipped=("Require all CI phases", "Confirm current paid CI success") if workflow == "ci.yml" else ("Run E2E auto-process-critical", "Run E2E core-rest shard 1/2", "Run E2E core-rest shard 2/2", "Final verdict", "Confirm current paid E2E success")
    for prefix in skipped:
        matches=[s for s in steps if s.get("name","").startswith(prefix)]
        if len(matches)!=1 or matches[0].get("status") != "completed" or matches[0].get("conclusion") != "skipped": return False
    gate_steps=[s for s in steps if s.get("name") == gate]
    if len(gate_steps)!=1 or gate_steps[0].get("status") != "completed" or gate_steps[0].get("conclusion") != outcome: return None
    plumbing={"Set up job", "Record whether this run starts paid E2E", gate, "Complete runner", "Complete job"}
    if any(s.get("status") != "completed" or not (s.get("conclusion") == "skipped" or (s.get("name") in plumbing and successful(s)) or (s.get("name") == gate and s.get("conclusion") == outcome)) for s in steps): return None
    if any(j.get("name") not in (CLASSIFIER_JOB_NAME,name) and (j.get("conclusion") != "skipped" or j.get("steps") != []) for j in jobs): return False
    if workflow == "e2e.yml":
        capture=by_name.get("e2e-log-capture",{})
        if capture.get("conclusion") != "skipped" or capture.get("steps") != []: return False
    return outcome


def verify_substantive_after_wrapper(pr, workflow, run, jobs):
    """A verifier never substitutes for a current native paid producer."""
    if not successful(run) or not bound_attempt_jobs(pr, run, jobs):
        raise Refusal(f"SUBSTANTIVE_RUN_NOT_GREEN_OR_UNBOUND workflow={workflow} run={run['id']}")
    name="test" if workflow == "ci.yml" else "e2e"
    job=next((j for j in jobs if j.get("name") == name),{})
    verdict="Confirm current paid CI success" if workflow == "ci.yml" else "Confirm current paid E2E success"
    if not successful(job) or len([s for s in job.get("steps",[]) if s.get("name") == verdict and successful(s)]) != 1:
        raise Refusal(f"SUBSTANTIVE_PAID_VERDICT_MISSING workflow={workflow} run={run['id']}")
    if workflow == "e2e.yml" and not successful(next((j for j in jobs if j.get("name") == "e2e-log-capture"),{})):
        raise Refusal(f"SUBSTANTIVE_E2E_CAPTURE_MISSING run={run['id']}")


def native_required_verifier_green(pr, workflow, run, jobs):
    """The CURRENT successful native required-context verifier (CTO ts
    1795416141.013939): the ci-dummy.yml (ci.yml for its roster) run whose
    required-context jobs carry the exemption gate that justified the paid
    lanes' skip, natively bound to this exact PR and head, at the attempt
    that published the green contexts.

    A gate-only red plus a tied paid success alone must REFUSE: the failed
    gate can mean an untrusted scope or invalid binding, and only this
    verifier proves the required contexts are green at this head.
    """
    if not successful(run) or not native_pr_binding_ok(run, pr):
        return False
    attempt = run.get("run_attempt")
    if type(attempt) is not int or attempt < 1:
        return False
    head = pr["head"]["sha"]
    if (run.get("head_sha") != head or run.get("event") != "pull_request"
            or run.get("head_branch") != pr["head"]["ref"]
            or run.get("path", "").split("@")[0] != f".github/workflows/{SITE_EXEMPTION_WORKFLOW}"):
        return False
    if not bound_attempt_jobs(pr, run, jobs):
        return False
    aggregate = "test" if workflow == "ci.yml" else "e2e"
    by_name = {job.get("name"): job for job in jobs}
    if set(by_name) != {"detect-docs-only", "validate-detect", "test", "e2e"}:
        return False
    scope = by_name.get(SITE_CLASSIFIER_JOB_NAME, {})
    if not successful(scope):
        return False
    # The classifier receipt and its authenticated bindings prove the scope
    # decision this verifier ran on is bound to this exact PR/head.
    lines = strip_log_framing(job_log(scope["id"]))
    receipt = classifier_receipt_object(lines, "mixed")
    if receipt is None or receipt.get("ci_required") is not True:
        return False
    bindings = classifier_bindings(lines)
    if (bindings is None or bindings.get("HEAD_SHA") != head
            or not event_binding_ok(bindings, run, pr)
            or not pr_binding_ok(bindings, run, pr)):
        return False
    required = [job for job in jobs if job.get("name") in ("test", "e2e")]
    for job in required:
        gate = ("Require exemption or exact-head real CI success" if job["name"] == "test"
                else "Require exemption or exact-head real E2E success")
        steps = job.get("steps", [])
        if not successful(job):
            return False
        if len([step for step in steps if step.get("name") == gate and successful(step)]) != 1:
            return False
    return True


def run_started_at_valid(value):
    """True only for a real UTC second stamp (YYYY-MM-DDTHH:MM:SSZ, ASCII digits)."""
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value, re.ASCII):
        return False
    try:
        datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def native_required_verifier_for(pr, workflow):
    """Find the newest CURRENT successful native required verifier for this head.

    The newest ci-dummy run at this head must ITSELF be a green verifier: an
    unusable newer attempt (unbound, wrong head, or a receipt log with no
    schema_version=1 line) never falls through to an older valid verifier.
    Ordering is only ever by a VALIDATED start second (CTO rework-4, ts
    1795433408.345089, and rework-5, ts 1795444808.341169):
      * every matching run's run_started_at must be present and parse BEFORE
        any ordering, so a bad stamp can neither raise KeyError nor sort below
        a valid run and be ignored;
      * more than one candidate in the newest second is ambiguous and refuses
        (never a run-id tie-break);
      * the selected run must carry a positive integer native run_attempt.
    """
    runs = pages(
        f"actions/workflows/{SITE_EXEMPTION_WORKFLOW}/runs?event=pull_request&head_sha={pr['head']['sha']}&per_page=100",
        "workflow_runs")
    matching = [run for run in runs if (
        run.get("head_sha") == pr["head"]["sha"] and run.get("event") == "pull_request"
        and run.get("head_branch") == pr["head"]["ref"]
        and run.get("path", "").split("@")[0] == f".github/workflows/{SITE_EXEMPTION_WORKFLOW}")]
    if not matching:
        return False
    if not all(run_started_at_valid(run.get("run_started_at")) for run in matching):
        return False
    matching.sort(key=lambda run: run["run_started_at"], reverse=True)
    newest = matching[0]
    if sum(run["run_started_at"] == newest["run_started_at"] for run in matching) != 1:
        return False
    attempt = newest.get("run_attempt")
    if type(attempt) is not int or attempt < 1:
        return False
    if not successful(newest):
        return False
    jobs = pages(f"actions/runs/{newest['id']}/jobs?filter=latest&per_page=100", "jobs")
    return native_required_verifier_green(pr, workflow, newest, jobs)


def verifier_gate_only_red_is_superseded(pr, workflow, verifier, selected):
    """A same-second gate-only verifier that lost a start race does not veto.

    Narrow rule (CTO ts 1795413605.307949): the verifier's ONLY red must be its
    own exemption gate, and the selected substantive run must be a native
    PR/head/attempt-bound paid run whose verdict steps succeeded at the same
    head. A real failed job, an unbound or unverified substantive run, or any
    red/pending sibling at the same second still refuses.
    """
    outcome, verifier_run, verifier_jobs = verifier
    run, jobs = selected
    if outcome != "failure":
        return False
    if verifier_run.get("run_started_at") != run.get("run_started_at"):
        return False
    if not (native_pr_binding_ok(verifier_run, pr) and native_pr_binding_ok(run, pr)):
        return False
    name = "test" if workflow == "ci.yml" else "e2e"
    gate = ("Require exemption or exact-head real CI success" if workflow == "ci.yml"
            else "Require exemption or exact-head real E2E success")
    verdict = ("Confirm current paid CI success" if workflow == "ci.yml"
               else "Confirm current paid E2E success")
    v_aggregate = next((job for job in verifier_jobs if job.get("name") == name), None)
    if v_aggregate is None or v_aggregate.get("conclusion") != "failure":
        return False
    failed = [step.get("name") for step in v_aggregate.get("steps", [])
              if step.get("conclusion") == "failure"]
    if failed != [gate]:
        return False
    aggregate = next((job for job in jobs if job.get("name") == name), {})
    if not successful(run) or not successful(aggregate):
        return False
    if not bound_attempt_jobs(pr, run, jobs):
        return False
    if not any(step.get("name") in (verdict, "Require all CI phases") and successful(step)
               for step in aggregate.get("steps", [])):
        return False
    # The CTO's control (ts 1795416141.013939): a gate-only red plus a tied
    # paid success alone must REFUSE. The failed gate can also mean an
    # untrusted scope or invalid binding; only the CURRENT successful native
    # required verifier, bound to this exact PR/head and its attempt, can
    # supersede it. Its absence keeps the refusal.
    return native_required_verifier_for(pr, workflow)


def cleanup_only_e2e(pr, workflow, run, jobs):
    if workflow != "e2e.yml" or not successful(run) or not bound_attempt_jobs(pr, run, jobs):
        return False
    if {j.get("name") for j in jobs} != verifier_job_names(workflow):
        return False
    cleanup = next(j for j in jobs if j["name"] == "teardown-pr-modal-apps")
    expected = {"Set up job", "Install Modal CLI", "Delete per-PR Modal apps", "Complete runner", "Complete job"}
    steps = cleanup.get("steps", [])
    return (successful(cleanup) and len(steps) == len(expected)
            and {s.get("name") for s in steps} == expected and all(successful(s) for s in steps)
            and all(j.get("conclusion") == "skipped" and j.get("steps") == []
                    for j in jobs if j is not cleanup))


def workflow_proof(pr, workflow, names):
    head = pr["head"]["sha"]
    runs = pages(f"actions/workflows/{workflow}/runs?event=pull_request&head_sha={head}&per_page=100", "workflow_runs")
    eligible = [r for r in runs if r.get("head_sha") == head and r.get("event") == "pull_request"
                and r.get("head_branch") == pr["head"]["ref"]
                and r.get("path", "").split("@")[0] == f".github/workflows/{workflow}"]
    for run in eligible:
        try:
            value = run.get("run_started_at")
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
                raise ValueError("invalid timestamp")
            datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            raise Refusal(f"RUN_ATTEMPT_START_INVALID workflow={workflow} run={run.get('id')}")
    eligible.sort(key=lambda r: (r["run_started_at"], r["id"]), reverse=True)
    latest_verifier = None
    selected = None
    selected_exemption = None
    for run in eligible:
        if (selected is not None and latest_verifier is not None
                and run["run_started_at"] < min(selected[0]["run_started_at"], latest_verifier[1]["run_started_at"])):
            break
        jobs = pages(f"actions/runs/{run['id']}/jobs?filter=latest&per_page=100", "jobs")
        if cleanup_only_e2e(pr, workflow, run, jobs):
            continue
        if (run.get("status") == "completed" and run.get("conclusion") == "skipped"
                and bound_attempt_jobs(pr, run, jobs)
                and {j.get("name") for j in jobs} == verifier_job_names(workflow)
                and all(j.get("conclusion") == "skipped" and j.get("steps") == [] for j in jobs)):
            continue
        verifier = verifier_only_outcome(pr, workflow, run, jobs)
        if verifier:
            if (verifier == "success" and workflow == "ci.yml"
                    and not any(j["name"] == CI_LITERAL_READS_JOB for j in jobs)):
                # The new label-verdict shape is a sibling, not an exemption producer.
                scope = next(j for j in jobs if j.get("name") == CLASSIFIER_JOB_NAME)
                exempt = classifier_exemption(pr, workflow, run, scope)
                if exempt is not None:
                    if selected is not None and selected[0]["run_started_at"] == run["run_started_at"]:
                        raise Refusal(f"SUBSTANTIVE_ORDER_AMBIGUOUS workflow={workflow}")
                    if selected is None:
                        selected = (run, jobs)
                        selected_exemption = exempt
                    continue
            if latest_verifier is None:
                latest_verifier = (verifier, run, jobs)
            elif latest_verifier[1]["run_started_at"] == run["run_started_at"]:
                raise Refusal(f"VERIFIER_ORDER_AMBIGUOUS workflow={workflow}")
            continue
        name = "test" if workflow == "ci.yml" else "e2e"
        aggregate = next((j for j in jobs if j.get("name") == name), {})
        gate = "Require exemption or exact-head real CI success" if workflow == "ci.yml" else "Require exemption or exact-head real E2E success"
        paid = "Confirm current paid CI success" if workflow == "ci.yml" else "Confirm current paid E2E success"
        steps = aggregate.get("steps", [])
        paid_execution = any(s.get("name") in (paid, "Require all CI phases")
                             and s.get("status") == "completed" and s.get("conclusion") in ("success", "failure") for s in steps)
        if paid_execution and not bound_attempt_jobs(pr, run, jobs, allow_failed=True):
            raise Refusal(f"SUBSTANTIVE_RUN_NOT_BOUND workflow={workflow} run={run['id']}")
        gate_steps = [s for s in steps if s.get("name") == gate]
        paid_names = [s.get("name") for s in steps if s.get("name") in (paid, "Require all CI phases")]
        if (gate_steps or paid_execution) and (len(gate_steps) != 1 or len(paid_names) != len(set(paid_names)) or not paid_execution
                           or gate_steps[0].get("status") != "completed"
                           or gate_steps[0].get("conclusion") != "skipped"):
            raise Refusal(f"VERIFIER_SHAPE_AMBIGUOUS workflow={workflow} run={run['id']}")
        if selected is not None and selected[0]["run_started_at"] == run["run_started_at"]:
            raise Refusal(f"SUBSTANTIVE_ORDER_AMBIGUOUS workflow={workflow}")
        if selected is None:
            selected = (run, jobs)
    if latest_verifier and latest_verifier[0] != "success":
        # The newest substantive run decides. A verifier only outranks it when it
        # started after (or, fail-closed, at the same second as) the selected run;
        # an older verifier-only sibling was superseded by that later paid run.
        superseded = (selected is not None
                      and latest_verifier[1]["run_started_at"] < selected[0]["run_started_at"])
        if (not superseded and selected is not None
                and latest_verifier[1]["run_started_at"] == selected[0]["run_started_at"]
                and verifier_gate_only_red_is_superseded(pr, workflow, latest_verifier, selected)):
            superseded = True
        if not superseded:
            raise Refusal(f"VERIFIER_NOT_GREEN workflow={workflow} run={latest_verifier[1]['id']}")
    if selected is None:
        raise Refusal(f"WORKFLOW_MISSING workflow={workflow}")
    run, jobs = selected
    if not bound_attempt_jobs(pr, run, jobs, allow_failed=True):
        raise Refusal(f"SUBSTANTIVE_RUN_NOT_BOUND workflow={workflow} run={run['id']}")
    if selected_exemption is not None:
        return selected_exemption
    if workflow == "ci.yml":
        if not successful(run):
            raise Refusal(f"WORKFLOW_NOT_GREEN workflow={workflow} run={run['id']}")
        for job in jobs:
            if job.get("conclusion") not in ("success", "skipped"):
                raise Refusal(f"JOB_NOT_GREEN_OR_UNBOUND job={job['name']} run={run['id']}")
    if True:
        scope_jobs = [job for job in jobs if job.get("name") == CLASSIFIER_JOB_NAME]
        scope = scope_jobs[0] if scope_jobs else None
        if scope and scope.get("conclusion") == "skipped":
            raise Refusal(f"WORKFLOW_NOT_GREEN workflow={workflow} run={run['id']}")
        by_name = {job["name"]: job for job in jobs}
        test_only = None
        required_set_unproven = []
        if scope is not None and successful(scope):
            exempt = classifier_exemption(pr, workflow, run, scope)
            if exempt is not None:
                return exempt
            if latest_verifier:
                verify_substantive_after_wrapper(pr, workflow, run, jobs)
            if workflow == "ci.yml" and len(scope_jobs) == 1:
                try:
                    test_only = app_test_only_classifier_proof(pr, workflow, run, scope)
                except GitCommitsReadFailed:
                    # Typed, not a silent "no receipt": name it in the refusal.
                    required_set_unproven.append("git_commits_read")
                    test_only = None
                except (Refusal, OSError, ValueError, KeyError,
                        subprocess.TimeoutExpired):
                    test_only = None
            if workflow == "ci.yml" and len(scope_jobs) == 1:
                required_set = ci_required_set_proof(pr, run, scope, required_set_unproven)
                if required_set is not None:
                    verify_ci_required_jobs(run, jobs, required_set)
                    return {"run": run["id"], "attempt": run.get("run_attempt", 1),
                            "head": head, "classifier_required_set": required_set}
        if latest_verifier and scope is None:
            verify_substantive_after_wrapper(pr, workflow, run, jobs)
        # A verified app-test-only receipt lets the classifier skip the python job.
        required = [name for name in names
                    if not (test_only is not None and name == "python"
                            and by_name.get(name, {}).get("conclusion") == "skipped")]
        if not successful(run) or not all(successful(by_name.get(name, {})) for name in required):
            # A native required-set receipt whose proof came back unproven falls
            # through to this legacy typescript/python/test check, which cannot
            # pass on the sharded layout. Name the unproven sub-check instead of
            # reporting a green run as red (#14762).
            detail = (f" required_set_unproven={required_set_unproven[0]}"
                      if required_set_unproven else "")
            raise Refusal(f"WORKFLOW_NOT_GREEN workflow={workflow} run={run['id']}{detail}")
        # These steps distinguish real test execution from successful wrapper jobs.
        # ci.yml runs unit tests in sharded "unit-tests (N)" jobs; legacy runs used typescript.
        unit_shards = sorted(name for name in by_name if name.startswith("unit-tests ("))
        if workflow == "ci.yml":
            if (test_only is not None and unit_selected_by_python_only_area(jobs, run, test_only)):
                # The selector attested zero JS shards for a Python-only change and the Python
                # lanes ran green: "Unit tests" is satisfied by selection, not by a step that
                # the skipped native placeholder cannot have. MoP, 2026-11-25, #14809.
                checks = ()
            else:
                checks = (tuple((name, "Unit tests") for name in unit_shards)
                          or (("typescript", "Unit tests"),))
        else:
            checks = (
                ("e2e", "Run E2E auto-process-critical"),
                ("e2e", "Run E2E core-rest shard 1/2"),
                ("e2e", "Run E2E core-rest shard 2/2"))
        for job_name, prefix in checks:
            if not any(step["name"].startswith(prefix) and successful(step)
                       for step in by_name[job_name].get("steps", [])):
                raise Refusal(f"TEST_EXECUTION_MISSING workflow={workflow} run={run['id']} step={prefix}")
        proof = {"run": run["id"], "attempt": run.get("run_attempt", 1), "head": head}
        if test_only is not None:
            proof["app_test_only"] = test_only
        return proof
    raise Refusal(f"WORKFLOW_MISSING workflow={workflow}")


# ---------- classifier exemption (control-plane-only PRs) ----------
CLASSIFIER_JOB_NAME = "classify-change-scope"
TEST_ONLY_SCOPE = "app_test_only"  # legacy receipt spelling, still accepted
CANONICAL_TEST_ONLY_SCOPE = "test_only"  # canonical classifier receipt: scope=test_only, test_only=true
EXEMPTION_RECEIPT = {
    "paid_ci_exempt": True,
    "product_changed": False,
    "ci_required": False,
    "e2e_required": False,
}
CLASSIFIER_SOURCE = "scripts/ci/change_scope.py"
# The canonical classifier self-exempts these two files; do not extend this to scripts/ci/**.
CLASSIFIER_SELF_EDIT_PATHS = (CLASSIFIER_SOURCE, "scripts/ci/tests/test_change_scope.py")
REQUIRED_BINDINGS = ("BASE_SHA", "HEAD_SHA", "WORKFLOW_SHA", "EVENT_NAME", "PR_NUMBER")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
TS = re.compile(r"^\s*\d{4}-\d\d-\d\dT[0-9:.]+Z\s?")
# Real GitHub Actions log framing emits "KEY: value"; the "=" form is also accepted.
BINDING = re.compile(r"^\s*([A-Z_]+)\s*[:=]\s*(\S+?)\s*$")
PR_NUMBER_KEY = re.compile(r"^\s*PR_NUMBER\s*[:=]")
EVENT_NAME_KEY = re.compile(r"^\s*EVENT_NAME\b")


def job_log(job_id):
    """Read raw Actions logs; strip_log_framing removes their ANSI colours.

    Newer gh refuses colour-framed logs, while older gh lacks the escape flag.
    Only the observed terminal refusal permits another GET with that flag.
    """
    path = f"repos/{REPO}/actions/jobs/{job_id}/logs"
    try:
        return command("gh", "api", "--method", "GET", path)
    except Refusal as exc:
        if str(exc) != ("the response contains terminal escape sequences; "
                        "pass --allow-escape-sequences to output it anyway"):
            raise
    return command("gh", "api", "--method", "GET", "--allow-escape-sequences", path)


def strip_log_framing(text):
    """Remove only the standard log framing: ANSI colour and leading timestamps."""
    out = []
    for line in ANSI.sub("", text).splitlines():
        match = TS.match(line)
        out.append(line[match.end():] if match else line)
    return out


def classifier_receipt(lines, expected_scope):
    """Exactly one correctly scoped schema_version=1 receipt, else None."""
    receipt = classifier_receipt_object(lines, expected_scope)
    if receipt is None:
        return None
    for key, expected in EXEMPTION_RECEIPT.items():
        if receipt.get(key) is not expected:
            return None
    if receipt.get("control_plane_only") is not (expected_scope == "control_plane_only"):
        return None
    rules = receipt.get("rules_sha256")
    if not isinstance(rules, str) or not HEX64.match(rules):
        return None
    return receipt


def classifier_receipt_object(lines, expected_scope):
    """Exactly one schema_version=1 JSON object with the requested scope."""
    found = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            obj = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("schema_version") == 1:
            found.append(obj)
    if len(found) != 1:
        return None
    receipt = found[0]
    if receipt.get("scope") != expected_scope:
        return None
    return receipt


def classifier_bindings(lines):
    """Unique env bindings from the classifier log, else None.

    Accepts both the real log form (``KEY: value``) and ``KEY=value``, with
    optional surrounding whitespace.
    """
    seen = {key: set() for key in REQUIRED_BINDINGS}
    for line in lines:
        match = BINDING.match(line)
        if match is None:
            # Malformed optional bindings must not enable an absence fallback.
            if PR_NUMBER_KEY.match(line) or EVENT_NAME_KEY.match(line):
                return None
            continue
        key, value = match.group(1), match.group(2)
        if key in seen:
            seen[key].add(value)
    # Older logs omit PR_NUMBER and/or EVENT_NAME. Callers bind their true absence
    # to the same native run, never to a default or a different receipt.
    optional = ("PR_NUMBER", "EVENT_NAME")
    if any(len(values) != 1 for key, values in seen.items() if key not in optional):
        return None
    if any(len(seen[key]) > 1 for key in optional):
        return None
    if seen["PR_NUMBER"] and not next(iter(seen["PR_NUMBER"])).isdigit():
        return None
    return {key: (next(iter(values)) if values else None) for key, values in seen.items()}


def pr_binding_ok(bindings, run, pr):
    """Logged PR_NUMBER must match; if not logged, the run must be natively bound to only this PR."""
    if bindings["PR_NUMBER"] is not None:
        return bindings["PR_NUMBER"] == str(pr["number"])
    return native_pr_binding_ok(run, pr)


def native_pr_binding_ok(run, pr):
    associations = run.get("pull_requests")
    return (isinstance(associations, list) and len(associations) == 1
            and isinstance(associations[0], dict)
            and associations[0].get("number") == pr["number"])


def event_binding_ok(bindings, run, pr):
    if run.get("event") != "pull_request":
        return False
    if bindings["EVENT_NAME"] is not None:
        return bindings["EVENT_NAME"] == run["event"]
    return native_pr_binding_ok(run, pr)


def contents_identity(path, ref):
    """(blob sha, bytes) for a repo path at a ref through the GitHub contents API."""
    payload = api(f"contents/{path}?ref={ref}")
    if not isinstance(payload, dict) or "sha" not in payload:
        return None
    blob = payload["sha"]
    encoded = payload.get("content") or ""
    try:
        import base64
        raw = base64.b64decode(encoded) if encoded else b""
    except Exception:
        return None
    return blob, raw


def merge_parent_base(base_sha, head_sha, parents):
    """Trusted base ref for a pull_request synthetic merge commit, else None.

    The classifier logs the event's pull_request.base.sha, while GitHub builds
    the synthetic merge commit (WORKFLOW_SHA) on the base-branch tip at build
    time. When main advanced in between (PR #14727 run 45334892718: BASE_SHA
    2da0d1f7, first parent c2411b55, two commits ahead), the first parent is a
    strict descendant of BASE_SHA, not BASE_SHA itself. That is not a defect in
    the run, so an exact [base, head] parent match is not required. The second
    parent must still be the exact PR head, and a differing first parent must
    be BOTH a strict descendant of BASE_SHA and part of the protected base
    branch: it must equal the GitHub-verified target base branch tip
    (git/ref/heads/main) or be its ancestor (CTO REVISE, C0ALZJHGE49 ts
    1795510573.793869). Descent alone is not enough - a descendant on a side
    branch can satisfy compare-status "ahead", and its workflow/classifier
    blobs would then become the trust reference. If the verified tip is
    unreadable, or the compare against BASE_SHA or the tip is unavailable or
    not conclusive, the advanced path refuses (fail closed). The exact
    BASE_SHA first parent still needs no compare at all.
    """
    shas = [parent.get("sha") if isinstance(parent, dict) else None for parent in parents]
    if (len(shas) != 2 or shas[1] != head_sha
            or not isinstance(shas[0], str) or not HEX40.fullmatch(shas[0])):
        return None
    if shas[0] == base_sha:
        return base_sha
    try:
        compared = read_with_retry(api, f"compare/{base_sha}...{shas[0]}")
        main_tip = read_with_retry(api, "git/ref/heads/main")["object"]["sha"]
    except (Refusal, ValueError, KeyError, TypeError):
        return None
    if not (isinstance(compared, dict) and compared.get("status") == "ahead"):
        return None
    if not (isinstance(main_tip, str) and HEX40.fullmatch(main_tip)):
        return None
    if shas[0] == main_tip:
        return shas[0]
    try:
        on_base_branch = read_with_retry(api, f"compare/{main_tip}...{shas[0]}")
    except (Refusal, ValueError):
        return None
    if isinstance(on_base_branch, dict) and on_base_branch.get("status") == "behind":
        return shas[0]
    return None


def classifier_exemption(pr, workflow, run, scope, expected_scope="control_plane_only", *, jobs=()):
    """Fail-closed verifier. Returns an exemption proof, or None to fall through."""
    head = pr["head"]["sha"]
    if not successful(run) or not successful(scope):
        return None
    lines = strip_log_framing(job_log(scope["id"]))
    receipt = classifier_receipt(lines, expected_scope)
    if receipt is None:
        return None
    bindings = classifier_bindings(lines)
    if bindings is None:
        return None
    base_sha, head_sha = bindings["BASE_SHA"], bindings["HEAD_SHA"]
    workflow_sha = bindings["WORKFLOW_SHA"]
    if not (HEX40.match(base_sha) and HEX40.match(workflow_sha)):
        return None
    if (head_sha != head or not event_binding_ok(bindings, run, pr)
            or not pr_binding_ok(bindings, run, pr)):
        return None
    commit_parent_rows = commit_parents(workflow_sha)
    parents = [parent["sha"] for parent in commit_parent_rows]
    trusted_base = base_sha
    if workflow == SITE_EXEMPTION_WORKFLOW and expected_scope in ("site", "control_plane_only"):
        # BASE_SHA is the common merge base, while GitHub's synthetic merge
        # commit first parent is the current base-branch tip. Main may advance
        # between those points; bind the second parent to the exact PR head.
        if (len(parents) != 2 or not HEX40.match(parents[0])
                or parents[1] != head_sha):
            return None
    else:
        trusted_base = merge_parent_base(base_sha, head_sha, commit_parent_rows)
        if trusted_base is None:
            return None
    workflow_path = f".github/workflows/{workflow}"
    control_plane = workflow == SITE_EXEMPTION_WORKFLOW and expected_scope == "control_plane_only"
    classifier_self_edit = False
    for path in (workflow_path, CLASSIFIER_SOURCE, "scripts/ci/change-scope-rules.json"):
        at_base = contents_identity(path, trusted_base)
        at_workflow = contents_identity(path, workflow_sha)
        if at_base is None or at_workflow is None:
            return None
        if at_base[0] != at_workflow[0]:
            if path != CLASSIFIER_SOURCE or not control_plane:
                return None
            classifier_self_edit = True
    rules = contents_identity("scripts/ci/change-scope-rules.json", workflow_sha)
    if rules is None:
        return None
    import hashlib
    if hashlib.sha256(rules[1]).hexdigest() != receipt["rules_sha256"]:
        return None
    if workflow == SITE_EXEMPTION_WORKFLOW and expected_scope == "site" \
            and not site_paths_confined(pr, rules[1]):
        return None
    site_only_paths = False
    if classifier_self_edit:
        # The PR can change the receipt and skip its own tests. A green test
        # job alone is not evidence that the conditional classifier step ran.
        test_jobs = [job for job in jobs if job.get("name") == "test"]
        if (len(test_jobs) != 1 or not successful(test_jobs[0])
                or not bound_attempt_jobs(pr, run, [scope, test_jobs[0]])):
            return None
        steps = test_jobs[0].get("steps")
        if not isinstance(steps, list):
            return None
        classifier_steps = [step for step in steps
                            if isinstance(step, dict) and step.get("name") == SITE_CLASSIFIER_TEST_STEP]
        if len(classifier_steps) != 1 or not successful(classifier_steps[0]):
            return None
        if ((run["pull_requests"][0].get("base") or {}).get("sha") != base_sha
                or merge_parent_base(base_sha, head_sha, commit_parent_rows) is None
                or not control_plane_self_edit_confined(pr, base_sha, receipt, rules[1])):
            return None
    elif control_plane and not control_plane_paths_confined(pr, rules[1]):
        # The shared classifier's marketing-only rule (every path under
        # website/**) deliberately emits a scope=control_plane_only receipt with
        # site_scoped=false while ownership stays "site" (change_scope.py
        # site_deploy_only). Its paths sit in the trusted `site` glob group, not
        # the control-plane groups, so the control-plane confinement above refused
        # such a PR and it fell through to a paid ci.yml lookup (WORKFLOW_MISSING,
        # PR #14787). Accept it ONLY when the complete file set (incl. rename
        # sources) matches the trusted `site` globs alone; any app, product,
        # app_ci, fixture or other path still fails closed.
        if not site_only_paths_confined(pr, rules[1]):
            return None
        site_only_paths = True
    proof = {"run": run["id"], "attempt": run.get("run_attempt", 1), "head": head,
             "scope_job_id": scope["id"], "workflow_sha": workflow_sha,
             "rules_sha256": receipt["rules_sha256"], "exempt": expected_scope}
    if site_only_paths:
        proof["site_only_paths"] = True
    return proof


def app_test_only_classifier_proof(pr, workflow, run, scope):
    """Verify the exact-head, non-paid app-test-only receipt from CI."""
    head = pr["head"]["sha"]
    if workflow != "ci.yml" or not successful(run) or not successful(scope):
        return None
    lines = strip_log_framing(job_log(scope["id"]))
    receipt = classifier_receipt_object(lines, CANONICAL_TEST_ONLY_SCOPE)
    scope_name = CANONICAL_TEST_ONLY_SCOPE
    if receipt is not None:
        if receipt.get("test_only") is not True:
            return None
    else:
        receipt = classifier_receipt_object(lines, TEST_ONLY_SCOPE)
        scope_name = TEST_ONLY_SCOPE
        if receipt is None or receipt.get("app_test_only") is not True:
            return None
    expected = {"control_plane_only": False, "paid_ci_exempt": False,
                "product_changed": False, "ci_required": True, "e2e_required": False}
    if any(receipt.get(key) is not value for key, value in expected.items()):
        return None
    rules_sha = receipt.get("rules_sha256")
    if not isinstance(rules_sha, str) or not HEX64.fullmatch(rules_sha):
        return None
    bindings = classifier_bindings(lines)
    if bindings is None:
        return None
    base_sha, head_sha, workflow_sha = (bindings["BASE_SHA"], bindings["HEAD_SHA"],
                                        bindings["WORKFLOW_SHA"])
    if not (HEX40.fullmatch(base_sha) and HEX40.fullmatch(workflow_sha)):
        return None
    if (head_sha != head or run.get("head_sha") != head or run.get("event") != "pull_request"
            or run.get("head_branch") != pr["head"]["ref"]
            or run.get("path", "").split("@")[0] != ".github/workflows/ci.yml"
            or bindings["EVENT_NAME"] != "pull_request"
            or not pr_binding_ok(bindings, run, pr)):
        return None
    trusted_base = merge_parent_base(base_sha, head_sha, commit_parents(workflow_sha))
    if trusted_base is None:
        return None
    for path in (".github/workflows/ci.yml", "scripts/ci/change_scope.py",
                 "scripts/ci/change-scope-rules.json"):
        at_base = contents_identity(path, trusted_base)
        at_workflow = contents_identity(path, workflow_sha)
        if at_base is None or at_workflow is None or at_base[0] != at_workflow[0]:
            return None
    rules = contents_identity("scripts/ci/change-scope-rules.json", workflow_sha)
    if rules is None:
        return None
    import hashlib
    if hashlib.sha256(rules[1]).hexdigest() != rules_sha:
        return None
    if scope_name == CANONICAL_TEST_ONLY_SCOPE:
        # The classifier decides test_only in code (is_unit_test_path), not via a
        # rules glob list, so bind the trusted receipt's changed_files to the
        # exact PR file set instead.
        if not receipt_files_match_pr(pr, receipt):
            return None
    elif not paths_confined(pr, rules[1], (TEST_ONLY_SCOPE,)):
        return None
    return {"run": run["id"], "attempt": run.get("run_attempt", 1), "head": head,
            "scope_job_id": scope["id"], "workflow_sha": workflow_sha,
            "rules_sha256": rules_sha, "exempt": scope_name}


def receipt_files_match_pr(pr, receipt):
    """True only when receipt changed_files equals the complete PR file set."""
    try:
        changed = receipt.get("changed_files")
        if (not isinstance(changed, list) or not changed
                or not all(isinstance(path, str) and path for path in changed)):
            return False
        pages_json = json.loads(read_with_retry(
            command, "gh", "api", "--paginate", "--slurp",
            f"repos/{REPO}/pulls/{pr['number']}/files?per_page=100"))
        if not isinstance(pages_json, list) or not pages_json:
            return False
        paths = set()
        for page in pages_json:
            if not isinstance(page, list):
                return False
            for item in page:
                if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
                    return False
                paths.add(item["filename"])
                previous = item.get("previous_filename")
                if previous is not None:
                    if not isinstance(previous, str):
                        return False
                    paths.add(previous)
        return bool(paths) and paths == set(changed)
    except (KeyError, TypeError, ValueError, Refusal, OSError, subprocess.TimeoutExpired):
        return False


def ci_required_set_proof(pr, run, scope, unproven=None):
    """Bind language gates to the same trusted native CI receipt, never PR prose.

    Returns None when unproven. When a native required-set receipt was found but
    a later sub-check failed, the failing check's code is appended to `unproven`
    so the caller can name it in the refusal; a run with no native receipt (the
    legacy layout) records nothing."""
    unproven = unproven if unproven is not None else []

    def fail(reason):
        unproven.append(reason)
        return None

    lines = strip_log_framing(job_log(scope["id"]))
    receipts = [classifier_receipt_object(lines, name)
                for name in ("product", "mixed", "editor_product")]
    receipt = next((item for item in receipts if item is not None), None)
    if receipt is None:
        return None
    expected = {"ci_required": True, "paid_ci_exempt": False,
                "control_plane_only": False, "product_changed": True,
                "ci_infra_scoped": False}
    if any(receipt.get(key) is not value for key, value in expected.items()):
        return fail("receipt_flags")
    flags = ("ts_changed", "python_changed", "e2e_required")
    if any(type(receipt.get(key)) is not bool for key in flags):
        return fail("receipt_flags")
    if not (receipt["ts_changed"] or receipt["python_changed"]):
        return fail("receipt_no_language")
    bindings = classifier_bindings(lines)
    if bindings is None:
        return fail("bindings_unparsed")
    base, head, workflow = (bindings["BASE_SHA"], bindings["HEAD_SHA"],
                            bindings["WORKFLOW_SHA"])
    if not (HEX40.fullmatch(base) and HEX40.fullmatch(workflow)):
        return fail("bindings_shape")
    if (head != pr["head"]["sha"] or run.get("head_sha") != head
            or run.get("head_branch") != pr["head"]["ref"]
            or run.get("path", "").split("@")[0] != ".github/workflows/ci.yml"
            or not successful(run) or not successful(scope)):
        return fail("run_binding")
    if not event_binding_ok(bindings, run, pr):
        return fail("event_binding")
    if not pr_binding_ok(bindings, run, pr) or not native_pr_binding_ok(run, pr):
        return fail("pr_association")
    try:
        workflow_parents = commit_parents(workflow)
    except GitCommitsReadFailed:
        return fail("git_commits_read")
    trusted_base = merge_parent_base(base, head, workflow_parents)
    if trusted_base is None:
        return fail("merge_parent_base")
    for path in (".github/workflows/ci.yml", "scripts/ci/change_scope.py",
                 "scripts/ci/change-scope-rules.json"):
        before, selected = contents_identity(path, trusted_base), contents_identity(path, workflow)
        if before is None or selected is None or before[0] != selected[0]:
            return fail("tooling_identity")
    rules = contents_identity("scripts/ci/change-scope-rules.json", workflow)
    import hashlib
    if rules is None or hashlib.sha256(rules[1]).hexdigest() != receipt.get("rules_sha256"):
        return fail("rules_digest")
    if not receipt_files_match_pr(pr, receipt):
        return fail("receipt_files_vs_pr")
    return {"head": head, "run": run["id"], "attempt": run.get("run_attempt", 1),
            "scope_job_id": scope["id"], "workflow_sha": workflow,
            "rules_sha256": receipt["rules_sha256"],
            **{key: receipt[key] for key in flags}}


UNIT_GATE_STEP = "Validate unit lane and scheduled jobs"
UNIT_GATE_ENV = ("CI_EXECUTION_SHA", "UNIT_SELECT_RESULT", "UNIT_PRECHECKS_RESULT",
                 "UNIT_TESTS_RESULT", "UNIT_SCHEDULED_COUNT", "EXPECTED_RUN_ID",
                 "UNIT_JOB_HEAD_SHA")
UNIT_ENV_LINE = re.compile(r"^  ([A-Z][A-Z_]*): ?(.*)$")


def unit_family(name):
    """Every job the unit matrix can report: shards, the plain name, a template-named placeholder."""
    return name == "unit-tests" or name.startswith("unit-tests (")


UNIT_NATIVE_PLACEHOLDER = "unit-tests (${{ matrix.shard }})"
UNIT_SELECTOR_JOBS = ("unit-select", "unit-prechecks", "unit-gate")
UNIT_DYNAMIC_MARKERS = UNIT_SELECTOR_JOBS + ("UNIT_SCHEDULE",)
LEGACY_UNIT_SHARDS = ("unit-tests (1)", "unit-tests (2)", "unit-tests (3)")


def native_unit_placeholder(job):
    """The one job a skipped dynamic matrix reports: the unexpanded template name, skipped, no steps."""
    return (job["name"] == UNIT_NATIVE_PLACEHOLDER and job.get("status") == "completed"
            and job.get("conclusion") == "skipped" and not job.get("steps"))


# The only zero-schedule proofs the CI area selector issues (change_scope.select_unit_by_area):
# every classified row is non-TypeScript, or there are no rows. An uncertain skip carries an
# empty proof and is never a selection by file area.
ZERO_SELECTION_PROOFS = frozenset({"non_ts_rows_only", "none_rows_only"})
PYTEST_FOOTER = re.compile(
    r"^=+ (?P<framed>.+?) in [0-9.]+s(?: \([0-9:]+\))? =+$"
    r"|^(?P<quiet>\d+ \w+(?:, \d+ \w+)*) in [0-9.]+s(?: \([0-9:]+\))?$")
PYTEST_COUNT = re.compile(r"(\d+) (passed|failed|errors?)\b")


def pytest_totals(lines):
    """(passed, failed_or_errored) summed over every pytest footer in the log lines."""
    passed = bad = 0
    for line in lines:
        footer = PYTEST_FOOTER.match(line.strip())
        if footer is None:
            continue
        for count, kind in PYTEST_COUNT.findall(footer.group("framed") or footer.group("quiet")):
            if kind == "passed":
                passed += int(count)
            else:
                bad += int(count)
    return passed, bad


# A Python lane is any ci.yml job whose own `if:` is gated on a `lane_python*` scope output. The
# set is read from the authenticated workflow bytes, never listed here (rule 60).
PYTHON_LANE_GATE = re.compile(r"needs\.scope\.outputs\.lane_python\w*")
WORKFLOW_JOB_ID = re.compile(r"  ([A-Za-z0-9_][A-Za-z0-9_-]*):\s*$")
TEST_RUNNER = re.compile(r"-m pytest\b|\bnode --test\b")
NODE_SUMMARY = re.compile(r"^(?:\u2139|#) (pass|fail) (\d+)$")
SHARD_LIST = re.compile(r"shard:\s*\[\s*(\d+(?:\s*,\s*\d+)*)\s*\]\s*")


def workflow_job_ids(text):
    """Top-level job ids under the one `jobs:` key, in file order."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.rstrip() == "jobs:"]
    if len(starts) != 1:
        return []
    ids = []
    for line in lines[starts[0] + 1:]:
        if re.match(r"[^\s#]", line):
            break
        match = WORKFLOW_JOB_ID.fullmatch(line)
        if match:
            ids.append(match.group(1))
    return ids


def job_condition(block):
    """The job-level `if:` expression: the key line plus its indented continuation."""
    out, inside = [], False
    for line in block:
        if not inside:
            if re.match(r"    if:", line):
                inside = True
                out.append(line)
        elif not line.strip() or len(line) - len(line.lstrip()) > 4:
            out.append(line)
        else:
            break
    return "\n".join(out)


def job_run_names(job, block):
    """Names the job reports at run time, or None unless its matrix is one literal shard list.

    An expression-valued matrix, `include`/`exclude`, a second axis, a renamed job or a repeated
    shard cannot be expanded from the workflow text, so the expected set would be a guess.
    """
    code = [line for line in block if not line.lstrip().startswith("#")]
    if any(re.match(r"    name:", line) for line in code):
        return None
    at = [i for i, line in enumerate(code) if "matrix:" in line]
    if not at:
        return [job]
    if len(at) != 1 or not re.fullmatch(r"      matrix:\s*", code[at[0]]):
        return None
    children = []
    for line in code[at[0] + 1:]:
        if not line.strip():
            continue
        if len(line) - len(line.lstrip()) <= 6:
            break
        children.append(line.strip())
    literal = SHARD_LIST.fullmatch(children[0]) if len(children) == 1 else None
    if literal is None:
        return None
    shards = re.split(r"\s*,\s*", literal.group(1))
    if len(set(shards)) != len(shards):
        return None
    return [f"{job} ({shard})" for shard in shards]


def step_script(step):
    """The step's `run:` text when it is a one-line plain value or a literal block, else None.

    The text is what the Actions log echoes for the step, so it is the step's identity in the log.
    Folded, quoted or multi-line plain values are not read: the caller then has no identity.
    """
    for at, line in enumerate(step):
        match = re.fullmatch(r"        run:[ \t]*(.*?)[ \t]*", line)
        if match is None:
            continue
        value, rest = match.group(1), step[at + 1:]
        if value in ("|", "|-", "|+"):
            body = []
            for raw in rest:
                if raw.strip() and len(raw) - len(raw.lstrip()) <= 8:
                    break
                body.append(raw)
            lead = next((len(raw) - len(raw.lstrip()) for raw in body if raw.strip()), None)
            if lead is None or any(raw.strip() and raw[:lead].strip() for raw in body):
                return None
            return normalized_script([raw[lead:] if raw.strip() else "" for raw in body])
        following = next((raw for raw in rest if raw.strip()), "")
        if (not value or value[0] in "\"'>&*!|[{%@" or " #" in value
                or len(following) - len(following.lstrip()) > 8):
            return None
        return normalized_script([value])
    return None


def normalized_script(lines):
    """Script lines without trailing blanks, as one text; None for an empty script."""
    text = "\n".join(line.rstrip() for line in lines).rstrip("\n")
    return text or None


def job_test_steps(block):
    """{step name: (runner kinds, run script)} of the job's unconditional test steps, or None.

    A step guarded by `if:` or `continue-on-error:` can legitimately skip or fail, so it is not
    the job's execution proof. A test step with no name cannot be matched to the run's steps, a
    name used twice is ambiguous, and one whose `run:` text cannot be read has no identity in
    the log; each of those is None.
    """
    steps, current = [], None
    for line in block:
        if re.match(r"      - ", line):
            current = [line]
            steps.append(current)
        elif re.match(r"    \S", line):
            current = None
        elif current is not None:
            current.append(line)
    required = {}
    for step in steps:
        body = [line for line in step if not line.lstrip().startswith("#")]
        if not any(TEST_RUNNER.search(line) for line in body):
            continue
        if any(re.match(r"        (if|continue-on-error):", line) for line in body):
            continue
        head = re.fullmatch(r"      - name:\s*(.+?)\s*", body[0])
        if head is None:
            return None
        name, script = head.group(1).strip("'\""), step_script(step)
        if script is None or name in required:
            return None
        text = "\n".join(body)
        required[name] = ({kind for kind, marker in (("pytest", "-m pytest"), ("node", "node --test"))
                           if marker in text}, script)
    return required


def python_lane_requirements(text):
    """{run job name: {test step name: (kinds, script)}} for every Python lane, or None on doubt."""
    required = {}
    for job in workflow_job_ids(text):
        block = workflow_job_block(text, job)
        if block is None:
            return None
        if not PYTHON_LANE_GATE.search(job_condition(block)):
            continue
        names, tests = job_run_names(job, block), job_test_steps(block)
        if names is None or not tests:
            return None
        required.update({name: tests for name in names})
    return required or None


EXPRESSION = re.compile(r"\$\{\{.*?\}\}", re.S)


def log_step_blocks(lines):
    """[(echoed script, output lines)] for every `Run` group of a framing-stripped job log.

    The `##[group]Run <first line>` title only repeats the script's first line, which the group
    body then prints in full: a step's echo is that body up to its `shell:` trailer, and its own
    output is everything after `##[endgroup]` up to the next group. Step names are not in the
    raw log, so the echoed script is the only handle on which output belongs to which step.
    """
    blocks, at = [], 0
    while at < len(lines):
        if not lines[at].startswith("##[group]Run "):
            at += 1
            continue
        echo = []
        at += 1
        while at < len(lines) and not lines[at].startswith("##[endgroup]"):
            echo.append(lines[at])
            at += 1
        at += 1
        output = []
        while at < len(lines) and not lines[at].startswith("##[group]"):
            output.append(lines[at])
            at += 1
        trailer = [i for i, line in enumerate(echo) if line.startswith("shell: ")]
        blocks.append((normalized_script(echo[:trailer[-1]] if trailer else echo) or "", output))
    return blocks


def script_matches(template, echoed):
    """True when the echoed script is the workflow's `run:` text with each ${{ }} expression valued.

    The literal parts must appear in order, the first as the start and the last as the end, so a
    script that merely shares a first line with the step (or a longer script that contains it)
    is not that step. A template with no literal text matches nothing.
    """
    parts = EXPRESSION.split(template)
    if not "".join(parts).strip():
        return False
    if len(parts) == 1:
        return template == echoed
    first, last = parts[0], parts[-1]
    if len(echoed) < len(first) + len(last) or not echoed.startswith(first) or not echoed.endswith(last):
        return False
    at, stop = len(first), len(echoed) - len(last)
    for part in parts[1:-1]:
        found = echoed.find(part, at, stop)
        if found < 0:
            return False
        at = found + len(part)
    return True


def step_block(blocks, script):
    """Index of the one log block that is this step's own, or None when none or several match."""
    own = [at for at, (echoed, _) in enumerate(blocks) if script_matches(script, echoed)]
    return own[0] if len(own) == 1 else None


def block_executed(output, kinds):
    """True when one block's own output shows nonzero passes for each runner kind and no failure."""
    passed, bad = pytest_totals(output)
    node = {"pass": 0, "fail": 0}
    for line in output:
        summary = NODE_SUMMARY.match(line.strip())
        if summary:
            node[summary.group(1)] += int(summary.group(2))
    if bad or node["fail"]:
        return False
    return not (("pytest" in kinds and passed <= 0) or ("node" in kinds and node["pass"] <= 0))


def steps_executed(blocks, steps):
    """True when every required step owns a DISTINCT log block whose own output proves a pass.

    One-to-one assignment of steps to blocks. Each block is found by its step's script, never by
    the job's pooled output, so another step's footer cannot stand in for a step that produced
    none. A step needs exactly one candidate block (none or several fail closed), and no block
    may be assigned to two steps: two green step records with the same command, or two `${{ }}`
    templates that both match one echo, still have only the one logged execution between them.
    """
    owned = {name: step_block(blocks, script) for name, (_, script) in steps.items()}
    taken = list(owned.values())
    if None in taken or len(set(taken)) != len(taken):
        return False
    return all(block_executed(blocks[owned[name]][1], kinds) for name, (kinds, _) in steps.items())


def python_lanes_executed(jobs, test_only):
    """True only when EVERY Python job the authenticated workflow schedules ran green with results.

    The required set is read from the ci.yml bytes ci_required_set_proof bound (same blob at the
    trusted base and at the classifier's workflow sha): each job gated on a `lane_python*` output,
    expanded over its literal shard list. The run must report exactly that set. A missing shard,
    an extra or duplicate one, a skipped one or one that did not succeed refuses. EACH of a
    job's unconditional test steps must have succeeded and must have its own result: the step's
    block in the job log is found by its `run:` script and that block's own output must show
    nonzero passes with no failure (pytest footers, or the node --test summary). Totals are never
    pooled across a job, so a sibling step's passing footer cannot vouch for a green step that
    produced none, and a green step record alone is not proof. Steps are assigned to blocks
    one-to-one: a block that two steps would both claim (same command, or two expression
    templates matching one echo) is one execution, so it cannot prove both. Any doubt about the
    workflow, a log, or which block is which step is False.
    """
    try:
        selected = contents_identity(".github/workflows/ci.yml", test_only["workflow_sha"])
        required = (None if selected is None
                    else python_lane_requirements(selected[1].decode("utf-8", "replace")))
    except (Refusal, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return False
    if required is None:
        return False
    lane_ids = {name.split(" (", 1)[0] for name in required}
    reported = [job["name"] for job in jobs if job["name"].split(" (", 1)[0] in lane_ids]
    if sorted(reported) != sorted(required):
        return False
    for job in jobs:
        steps = required.get(job["name"])
        if steps is None:
            continue
        if not successful(job):
            return False
        for name in steps:
            ran = [step for step in job.get("steps", []) if step.get("name") == name]
            if len(ran) != 1 or not successful(ran[0]):
                return False
        try:
            blocks = log_step_blocks(strip_log_framing(job_log(job["id"])))
        except (Refusal, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            return False
        if not steps_executed(blocks, steps):
            return False
    return True


def unit_selected_by_python_only_area(jobs, run, test_only):
    """True when the gate attested a zero-JS selection by area and every Python lane executed.

    Reads the selector's own attestation (unit-gate receipt: mode zero with a selector zero
    proof, count 0, bound to this head, run and attempt); it never guesses from file paths.
    The unit family must be exactly the one skipped native placeholder, so a selected JS shard
    that did not execute, an uncertain or unknown selection and a legacy matrix all return False.
    """
    unit = [job for job in jobs if unit_family(job["name"])]
    return (len(unit) == 1 and native_unit_placeholder(unit[0])
            and unit_gate_attested_count(run, jobs, test_only, zero_only=True) == 0
            and python_lanes_executed(jobs, test_only))


def workflow_job_block(text, job):
    """Body lines of one top-level workflow job, or None unless it is defined exactly once."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if re.fullmatch(rf"  {re.escape(job)}:\s*", line)]
    if len(starts) != 1:
        return None
    block = []
    for line in lines[starts[0] + 1:]:
        if re.match(r"  [A-Za-z0-9_][A-Za-z0-9_-]*:\s*$", line) or re.match(r"[^\s#]", line):
            break
        block.append(line)
    return block


def legacy_static_unit_matrix(jobs, required_set):
    """True only when the trusted ci.yml itself proves the legacy static three-shard unit matrix.

    The workflow bytes are the ones ci_required_set_proof bound (same blob at base and at the
    classifier's workflow sha). A run is legacy-static only if no selector job reported, the unit
    family is exactly shards 1..3, and that workflow defines `unit-tests` once with a literal
    `shard: [1, 2, 3]` matrix and none of the selector markers. Any doubt is "not legacy".
    """
    if any(job["name"] in UNIT_SELECTOR_JOBS for job in jobs):
        return False
    if {job["name"] for job in jobs if unit_family(job["name"])} != set(LEGACY_UNIT_SHARDS):
        return False
    try:
        selected = contents_identity(".github/workflows/ci.yml", required_set["workflow_sha"])
    except (Refusal, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return False
    if selected is None:
        return False
    text = selected[1].decode("utf-8", "replace")
    block = workflow_job_block(text, "unit-tests")
    # A literal matrix is a mapping (`matrix:` alone on its line); an expression-valued matrix
    # (`matrix: ${{ fromJSON(...) }}`) is the dynamic selector's.
    if (block is None or any(marker in text for marker in UNIT_DYNAMIC_MARKERS)
            or any(re.search(r"\bmatrix:\s*\S", line) for line in block)):
        return False
    literal = re.compile(r"\s+shard:\s*\[\s*1\s*,\s*2\s*,\s*3\s*\]\s*")
    return sum(bool(literal.fullmatch(line)) for line in block) == 1


def unit_gate_log_facts(lines):
    """Unique env facts, the one UNIT_SCHEDULE object and the one verified event of the gate step."""
    facts, schedules, events = {}, [], []
    i = 0
    while i < len(lines):
        line = lines[i]
        match = UNIT_ENV_LINE.match(line)
        if match and match.group(1) == "UNIT_SCHEDULE":
            if match.group(2).strip() != "{":
                return None
            body = [match.group(2).strip()]
            i += 1
            while i < len(lines) and lines[i].rstrip() != "}":
                body.append(lines[i])
                i += 1
            if i >= len(lines):
                return None
            body.append("}")
            try:
                schedules.append(json.loads("\n".join(body)))
            except ValueError:
                return None
        elif match and match.group(1) in UNIT_GATE_ENV:
            if match.group(1) in facts:
                return None
            facts[match.group(1)] = match.group(2).strip()
        elif line.strip().startswith("{"):
            try:
                obj = json.loads(line.strip())
            except ValueError:
                obj = None
            if isinstance(obj, dict) and obj.get("event") == "unit_jobs_verified":
                events.append(obj)
        i += 1
    if len(schedules) != 1 or len(events) != 1 or set(facts) != set(UNIT_GATE_ENV):
        return None
    return facts, schedules[0], events[0]


def _utc_instant(value):
    """GitHub Zulu timestamp, or None. Fractional seconds are optional."""
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    text = value[:-1]
    if "." in text:
        head, frac = text.split(".", 1)
        if not frac.isdigit():
            return None
        text = f"{head}.{frac[:6].ljust(6, '0')}"
        fmt = "%Y-%m-%dT%H:%M:%S.%f"
    else:
        fmt = "%Y-%m-%dT%H:%M:%S"
    try:
        return datetime.datetime.strptime(text, fmt)
    except ValueError:
        return None


def _positive_attempt(value):
    """1, 2, ... as an int or its canonical decimal text. Rejects bool, '', '01'."""
    if type(value) is int and value >= 1:
        return value
    if type(value) is str and value.isdigit():
        number = int(value)
        if number >= 1 and str(number) == value:
            return number
    return None


def _started_before(job, attempt_started):
    started = _utc_instant(job.get("started_at"))
    return started is not None and attempt_started is not None and started < attempt_started


def _completed_before(job, attempt_started):
    """A real execution that finished strictly before this attempt began."""
    started = _utc_instant(job.get("started_at"))
    completed = _utc_instant(job.get("completed_at"))
    return (started is not None and completed is not None and attempt_started is not None
            and started <= completed < attempt_started)


def _same_execution(original, reissued):
    """Reissued job ids still carry the original start and completion strings."""
    started, completed = original.get("started_at"), original.get("completed_at")
    return (isinstance(started, str) and isinstance(completed, str)
            and started == reissued.get("started_at")
            and completed == reissued.get("completed_at")
            and _utc_instant(started) is not None
            and _utc_instant(completed) is not None)


def _original_execution(original, reissued, run, attempt):
    """One job from the receipt's attempt is the execution the reissued job copies."""
    head = run.get("head_sha")
    return (_same_execution(original, reissued)
            and original.get("name") == reissued.get("name")
            and original.get("run_id") == reissued.get("run_id") == run.get("id")
            and original.get("head_sha") == reissued.get("head_sha") == head
            and isinstance(head, str) and HEX40.fullmatch(head) is not None
            and _positive_attempt(original.get("run_attempt")) == attempt
            and original.get("status") == "completed"
            and original.get("conclusion") == reissued.get("conclusion")
            and successful(original) == successful(reissued))


def _one_original(originals, reissued, run, attempt):
    found = [job for job in originals if _original_execution(job, reissued, run, attempt)]
    return found[0] if len(found) == 1 else None


def jobs_for_attempt(run_id, attempt):
    """Jobs GitHub recorded for one run attempt, or None when that proof is unavailable."""
    if type(run_id) is not int or type(attempt) is not int or run_id < 1 or attempt < 1:
        return None
    try:
        found = pages(
            f"actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100", "jobs")
    except (Refusal, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return None
    if not isinstance(found, list) or any(not isinstance(job, dict) for job in found):
        return None
    return found


def attempt_record(run_id, attempt):
    """GitHub's workflow-run record for one attempt, or None if that boundary is unavailable.

    The jobs list restamps a finished gate into later attempts. This record is the
    attempt's own metadata (run id, head, attempt number, run_started_at).
    """
    if type(run_id) is not int or type(attempt) is not int or run_id < 1 or attempt < 1:
        return None
    try:
        found = api(f"actions/runs/{run_id}/attempts/{attempt}")
    except (Refusal, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return None
    if not isinstance(found, dict):
        return None
    return found


def _gate_ran_during_attempt(record, run, gate, attempt):
    """The matched gate executed during the receipt-named attempt.

    Copied job rows keep the original run, head and timestamps, so those fields
    only prove the row was visible in the named attempt. The attempt record places
    the execution: its run_started_at ≤ started_at ≤ completed_at < the current
    attempt's run_started_at. A missing, mismatched or unparseable record refuses.
    """
    if not isinstance(record, dict):
        return False
    head = run.get("head_sha")
    if (record.get("id") != run.get("id")
            or record.get("head_sha") != head
            or not isinstance(head, str) or HEX40.fullmatch(head) is None
            or _positive_attempt(record.get("run_attempt")) != attempt):
        return False
    opened = _utc_instant(record.get("run_started_at"))
    started = _utc_instant(gate.get("started_at"))
    completed = _utc_instant(gate.get("completed_at"))
    current = _utc_instant(run.get("run_started_at"))
    if opened is None or started is None or completed is None or current is None:
        return False
    return opened <= started <= completed < current


def _gate_step_succeeded(job):
    steps = job.get("steps")
    return isinstance(steps, list) and any(
        successful(step) and step.get("name") == UNIT_GATE_STEP for step in steps)


def receipt_attempt_binds(receipt_attempt, run, gate, jobs):
    """The schedule receipt names the attempt that actually executed the gate.

    On "re-run failed jobs", GitHub reports the latest run_attempt and a new job
    id for successes it did not re-execute, but keeps the original start and
    completion (run 45305541606: attempt-1 gate 138159904929 and attempt-2 gate
    138167137778 both completed at 01:59:36Z, before attempt 2 began at
    02:16:57Z). A carried receipt binds only when that named attempt's own jobs
    show this same execution for the gate and for every unit shard: same run,
    same head, same timestamps, despite the reissued id. That jobs row can be
    restamped into a later attempt, so the named attempt's own record must place
    the gate: its run_started_at ≤ the matched gate's started_at ≤ completed_at
    < this attempt's run_started_at. An unavailable or invalid record refuses.
    Shards that started earlier may be copied into the attempt that ran the gate;
    they still match by execution, not by that window. The gate and those shards
    must have completed before this attempt. A lower attempt number alone is not
    that proof. A job re-executed on this attempt must carry this attempt's
    number, and that path does not consult older attempts.
    """
    current = _positive_attempt(run.get("run_attempt", 1))
    attested = _positive_attempt(receipt_attempt)
    if current is None or attested is None or attested > current:
        return False
    attempt_started = _utc_instant(run.get("run_started_at"))
    if _started_before(gate, attempt_started):
        if attested >= current:
            return False
        if not _completed_before(gate, attempt_started):
            return False
        family = [job for job in jobs if unit_family(job["name"])]
        if (not family or not all(_started_before(job, attempt_started) for job in family)
                or not all(_completed_before(job, attempt_started) for job in family)):
            return False
        originals = jobs_for_attempt(run.get("id"), attested)
        if originals is None:
            return False
        original_gate = _one_original(originals, gate, run, attested)
        if original_gate is None or not _gate_step_succeeded(original_gate):
            return False
        if not _gate_ran_during_attempt(
                attempt_record(run.get("id"), attested), run, original_gate, attested):
            return False
        return all(_one_original(originals, job, run, attested) is not None for job in family)
    return attested == current


def unit_gate_attested_count(run, jobs, required_set, zero_only=False):
    """Shard count the unit gate attested for THIS head, run and attempt, else None.

    zero_only narrows the attestation to a zero-JS schedule the area selector proved
    (mode zero with a ZERO_SELECTION_PROOFS proof); an uncertain skip or any scheduled
    shard then returns None.

    Mirrors the gate's own validation of its schedule receipt. A receipt for another
    head or run, a failed or missing gate, or any doubt returns None. The receipt's
    run_attempt is the attempt that executed the gate. On a carried-over job that
    attempt is earlier than the one GitHub stamps on the latest jobs, and it binds
    only when that attempt's jobs still show the same start and completion. None is never an
    attestation: on dynamic-selector CI the caller refuses, and only a workflow
    that proves a legacy static three-shard matrix keeps the fixed shard requirement.
    """
    head, run_id = required_set["head"], str(run["id"])
    gates = [job for job in jobs if job["name"] == "unit-gate"]
    if len(gates) != 1:
        return None
    gate = gates[0]
    if (not successful(gate) or gate.get("run_id") != run["id"]
            or gate.get("run_attempt") != run.get("run_attempt", 1)
            or gate.get("head_sha") != head
            or not any(successful(s) and s["name"] == UNIT_GATE_STEP for s in gate.get("steps", []))):
        return None
    try:
        parsed = unit_gate_log_facts(strip_log_framing(job_log(gate["id"])))
    except (Refusal, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return None
    if parsed is None:
        return None
    facts, receipt, event = parsed
    skip = str(receipt.get("skip_matrix", "")).lower()
    count = str(receipt.get("scheduled_count", ""))
    mode, reason, proof = receipt.get("mode"), receipt.get("reason"), receipt.get("zero_proof")
    if (facts["CI_EXECUTION_SHA"] != head or facts["UNIT_JOB_HEAD_SHA"] != head
            or facts["EXPECTED_RUN_ID"] != run_id or receipt.get("head") != head
            or str(receipt.get("run_id")) != run_id
            or not receipt_attempt_binds(receipt.get("run_attempt"), run, gate, jobs)
            or facts["UNIT_SCHEDULED_COUNT"] != count
            or facts["UNIT_SELECT_RESULT"] != "success"
            or facts["UNIT_PRECHECKS_RESULT"] != "success"
            or facts["UNIT_TESTS_RESULT"] != ("skipped" if skip == "true" else "success")):
        return None
    if zero_only and (skip != "true" or mode != "zero" or proof not in ZERO_SELECTION_PROOFS):
        return None
    if skip == "true":
        if mode == "zero":
            attested = isinstance(proof, str) and bool(proof.strip())
        else:
            attested = (mode == "uncertain" and not proof and isinstance(reason, str)
                        and reason != "zero_proof"
                        and re.fullmatch(r"[a-z][a-z0-9_]*", reason) is not None)
        if count != "0" or not attested:
            return None
    elif count not in ("1", "2", "3", "4", "5"):
        return None
    declared = int(count)
    if (str(event.get("run_id")) != run_id or event.get("head_sha") != head
            or event.get("declared_count") != declared
            or event.get("observed_jobs") != [f"unit-tests ({n})" for n in range(1, declared + 1)]):
        return None
    return declared


def verify_ci_required_jobs(run, jobs, required_set):
    """Mirror the trusted CI language families; wrappers alone never prove execution."""
    if (len({job["name"] for job in jobs}) != len(jobs)
            or len({job.get("id") for job in jobs}) != len(jobs)):
        raise Refusal(f"AMBIGUOUS_JOB_IDENTITY workflow=ci.yml run={run['id']}")
    by_name = {job["name"]: job for job in jobs}
    # No allowance for failed optional jobs, failed infra, or a job from another attempt.
    for job in jobs:
        if (job.get("run_id") != run["id"]
                or job.get("run_attempt") != run.get("run_attempt", 1)
                or job.get("head_sha") != required_set["head"]
                or job.get("conclusion") not in ("success", "skipped")
                or job.get("status") != "completed"):
            raise Refusal(f"JOB_NOT_GREEN_OR_UNBOUND job={job['name']} run={run['id']}")
    # Dynamic-selector CI requires THIS attempt's unit-gate attestation: exactly the scheduled
    # shards (0..5), and an unscheduled matrix is exactly one native skipped placeholder. A
    # missing, stale (other head/run/attempt) or invalid attestation refuses; it never falls
    # back to the legacy shard set. Only a workflow that proves a legacy static 1..3 matrix
    # keeps the fixed three-shard requirement.
    ts_shards = {f"unit-tests ({i})": "Unit tests" for i in (1, 2, 3)}
    if required_set["ts_changed"]:
        declared = unit_gate_attested_count(run, jobs, required_set)
        if declared is None:
            if not legacy_static_unit_matrix(jobs, required_set):
                raise Refusal(f"UNIT_ATTESTATION_REQUIRED run={run['id']}")
        else:
            expected = [f"unit-tests ({n})" for n in range(1, declared + 1)]
            family = [job for job in jobs if unit_family(job["name"])]
            if declared == 0:
                # Zero scheduled: the matrix is exactly one native skipped placeholder, never
                # real shards and never several placeholders.
                contradicts = len(family) != 1 or not native_unit_placeholder(family[0])
            else:
                # N scheduled: only shards 1..N may exist; the required-job loop proves each one.
                contradicts = any(job["name"] not in expected for job in family)
            if contradicts:
                raise Refusal(f"UNIT_JOBS_CONTRADICT_ATTESTATION count={declared} run={run['id']}")
            ts_shards = {name: "Unit tests" for name in expected}
    groups = {
        "ts_changed": {"typescript": "Typecheck", "unit-tests-isolated": "Isolated unit tests",
                       **ts_shards},
        "python_changed": {"python": "Modal shared unit tests",
                           "python-docx": "Modal docx unit tests",
                           "python-contracts": "CI parallel workflow contracts",
                           **{f"python-modal ({i})": "Modal audio unit tests" for i in (0, 1, 2)}},
    }
    for flag, phases in groups.items():
        if required_set[flag]:
            for name, step in phases.items():
                job = by_name.get(name, {})
                if not successful(job):
                    raise Refusal(f"REQUIRED_JOB_NOT_GREEN job={name} run={run['id']}")
                if not any(successful(s) and s["name"].startswith(step)
                           for s in job.get("steps", [])):
                    raise Refusal(f"TEST_EXECUTION_MISSING job={name} step={step} run={run['id']}")
        else:
            # A skipped matrix has one unexpanded native job; an executed matrix
            # has the full shard set. Missing or partial families are not skips.
            names = list(phases)
            matrix = "unit-tests" if flag == "ts_changed" else "python-modal"
            if matrix in by_name:
                names = [name for name in names if not name.startswith(matrix + " (")]
                names.append(matrix)
                if any(name.startswith(matrix + " (") for name in by_name):
                    raise Refusal(f"AMBIGUOUS_JOB_IDENTITY job={matrix} run={run['id']}")
            elif flag == "ts_changed":
                # The same skipped matrix can also be reported as a template-named placeholder.
                holders = [name for name in by_name if unit_family(name) and "{{" in name]
                if holders:
                    if any(unit_family(name) and "{{" not in name for name in by_name):
                        raise Refusal(f"AMBIGUOUS_JOB_IDENTITY job={matrix} run={run['id']}")
                    names = [name for name in names if not name.startswith("unit-tests (")] + holders
            for name in names:
                job = by_name.get(name, {})
                if not (successful(job) or (job.get("status") == "completed"
                                            and job.get("conclusion") == "skipped")):
                    raise Refusal(f"OPTIONAL_JOB_NOT_SUCCESS_OR_SKIPPED job={name} run={run['id']}")
    aggregate = by_name.get("test", {})
    if not successful(aggregate) or not any(
            successful(step) and step["name"] == "Require all CI phases"
            for step in aggregate.get("steps", [])):
        raise Refusal(f"CI_AGGREGATE_NOT_EXECUTED run={run['id']}")


def e2e_classifier_authenticated(pr, run, scope, classifier_proof):
    """The run's own classifier receipt, authenticated, else None.

    Binds THIS run's classifier log to the actual PR number, head SHA and
    event, to the PR's real file set, and to the trusted rules sha256 and
    workflow sha of the CI classifier proof. Every E2E candidate, whatever its
    aggregate shape (skipped or gate-passed), passes this BEFORE any shape or
    duplicate-equality check (CTO rework-5, ts 1795444808.341169).
    """
    if classifier_proof.get("head") != pr["head"]["sha"]:
        return None
    lines = strip_log_framing(job_log(scope["id"]))
    receipts = [classifier_receipt_object(lines, name) for name in
                ("test_only", "app_test_only", "product", "mixed", "editor_product")]
    receipts = [receipt for receipt in receipts if receipt is not None]
    if len(receipts) != 1:
        return None
    receipt = receipts[0]
    bindings = classifier_bindings(lines)
    if (bindings is None or receipt.get("ci_required") is not True
            or receipt.get("e2e_required") is not False
            or receipt.get("rules_sha256") != classifier_proof.get("rules_sha256")
            or bindings.get("HEAD_SHA") != pr["head"]["sha"]
            or bindings.get("WORKFLOW_SHA") != classifier_proof.get("workflow_sha")
            or not event_binding_ok(bindings, run, pr)
            or not pr_binding_ok(bindings, run, pr)
            or not receipt_files_match_pr(pr, receipt)):
        return None
    return receipt


def exempt_success_e2e_job(pr, run, scope, job, classifier_proof):
    """Accept only a bound native exemption aggregate with no paid execution."""
    if not successful(job):
        return False
    if e2e_classifier_authenticated(pr, run, scope, classifier_proof) is None:
        return False
    steps = job.get("steps")
    if not isinstance(steps, list) or not steps:
        return False
    if (any(not isinstance(step, dict) or step.get("status") != "completed"
            or type(step.get("number")) is not int or step["number"] < 1
            for step in steps)
            or len({step.get("name") for step in steps}) != len(steps)
            or len({step.get("number") for step in steps}) != len(steps)):
        return False
    required_skips = ("Run E2E auto-process-critical", "Run E2E core-rest shard 1/2",
                      "Run E2E core-rest shard 2/2", "Final verdict",
                      "Confirm current paid E2E success")
    skipped_steps = []
    for prefix in required_skips:
        matches = [step for step in steps if isinstance(step.get("name"), str)
                   and (step["name"].startswith(prefix) if prefix.startswith("Run E2E ")
                        else step["name"] == prefix)]
        if len(matches) != 1 or matches[0].get("conclusion") != "skipped":
            return False
        skipped_steps.append(matches[0])
    gate = "Require exemption or exact-head real E2E success"
    gates = [step for step in steps if step.get("name") == gate]
    if (len(gates) != 1 or not successful(gates[0])
            or any(step["number"] >= gates[0]["number"] for step in skipped_steps)):
        return False
    plumbing = {"Set up job", "Record whether this run starts paid E2E",
                gate, "Complete runner", "Complete job"}
    return all(step.get("conclusion") == "skipped"
               or (step.get("name") in plumbing and successful(step))
               for step in steps)


def e2e_duplicate_classifier_identity(scope):
    """The classifier facts two duplicate E2E runs must agree on, else None."""
    lines = strip_log_framing(job_log(scope["id"]))
    receipts = [classifier_receipt_object(lines, name) for name in
                ("test_only", "app_test_only", "product", "mixed", "editor_product")]
    receipts = [receipt for receipt in receipts if receipt is not None]
    if len(receipts) != 1:
        return None
    receipt = receipts[0]
    bindings = classifier_bindings(lines)
    if bindings is None:
        return None
    identity = (receipt.get("scope"), receipt.get("e2e_required"), receipt.get("ci_required"),
                receipt.get("rules_sha256"), bindings.get("WORKFLOW_SHA"),
                bindings.get("HEAD_SHA"), bindings.get("BASE_SHA"),
                json.dumps(receipt.get("changed_files"), sort_keys=True))
    if any(value is None for value in identity):
        return None
    return identity


def e2e_exemption_shape(pr, run, jobs, scope, classifier_proof):
    """True only for one wholly-skipped exemption run, by either accepted shape.

    The aggregate gate job is `skipped` when the paid legs never started and
    `success` when it ran the exemption gate itself; both must pass every other
    bound-shape check. Anything else is not an exemption.
    """
    if not successful(run) or not successful(scope):
        return False
    e2e_jobs = [job for job in jobs if job.get("name") == "e2e"]
    capture_jobs = [job for job in jobs if job.get("name") == "e2e-log-capture"]
    if (len(e2e_jobs) != 1 or len(capture_jobs) != 1
            or capture_jobs[0].get("status") != "completed"
            or capture_jobs[0].get("conclusion") != "skipped"
            or capture_jobs[0].get("steps") != []):
        return False
    if any(job.get("status") != "completed"
           or job.get("conclusion") not in ("success", "skipped") for job in jobs):
        return False
    e2e_job = e2e_jobs[0]
    if e2e_job.get("conclusion") == "skipped":
        # A skipped aggregate must have EXECUTED NOTHING (CTO ts
        # 1795416141.013939): any executed step is a browser leg or gate
        # that ran, and a skipped aggregate is not an exemption receipt.
        # The capture job skipped-with-empty-steps binds the skipped shape
        # the same way the gate-passed shape is bound below. The caller has
        # already authenticated this run's classifier (rework-5).
        return e2e_job.get("steps") == []
    return exempt_success_e2e_job(pr, run, scope, e2e_job, classifier_proof)


def app_test_only_e2e_proof(pr, classifier_proof):
    """Select the newest substantive E2E run; only wholly skipped wrappers may yield."""
    head = pr["head"]["sha"]
    runs = pages(f"actions/workflows/e2e.yml/runs?event=pull_request&head_sha={head}&per_page=100",
                 "workflow_runs")
    matching = [run for run in runs if (
        run.get("head_sha") == head and run.get("event") == "pull_request"
        and run.get("head_branch") == pr["head"]["ref"]
        and run.get("path", "").split("@")[0] == ".github/workflows/e2e.yml")]
    for run in matching:
        try:
            value = run.get("run_started_at")
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
                return None
            datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return None
    matching.sort(key=lambda run: (run["run_started_at"], run["id"]), reverse=True)
    selected = None
    selected_time = None
    duplicate_identity = None
    for run in matching:
        if selected is not None and run["run_started_at"] < selected_time:
            return selected
        if not native_pr_binding_ok(run, pr):
            return None
        jobs = pages(f"actions/runs/{run['id']}/jobs?filter=latest&per_page=100", "jobs")
        if (len({job.get("name") for job in jobs}) != len(jobs)
                or len({job.get("id") for job in jobs}) != len(jobs)
                or any(job.get("run_id") != run["id"]
                       or job.get("run_attempt") != run.get("run_attempt", 1)
                       or job.get("head_sha") != head for job in jobs)):
            return None
        if cleanup_only_e2e(pr, "e2e.yml", run, jobs):
            continue
        scope_jobs = [job for job in jobs if job.get("name") == CLASSIFIER_JOB_NAME]
        if len(scope_jobs) != 1:
            return None
        scope = scope_jobs[0]
        # Label-event wrappers have no executed classifier. Do not use this
        # exception to ignore pending/failed/ambiguous classifiers or infra red.
        if (run.get("status") == "completed" and run.get("conclusion") == "skipped"
                and bound_attempt_jobs(pr, run, jobs)
                and {job.get("name") for job in jobs} == verifier_job_names("e2e.yml")
                and all(job.get("status") == "completed"
                        and job.get("conclusion") == "skipped"
                        and not job.get("steps") for job in jobs)):
            continue
        # Authenticate THIS run's classifier (PR, head, event, receipt files,
        # trusted rules/workflow) before any aggregate-shape or duplicate-
        # equality check, for the skipped shape too (CTO rework-5, ts
        # 1795444808.341169): a sole skipped aggregate, or two skipped runs
        # sharing the same wrong HEAD_SHA, must not bypass it.
        if (not successful(run) or not successful(scope)
                or e2e_classifier_authenticated(pr, run, scope, classifier_proof) is None):
            return None
        if selected is not None:
            # Duplicate runs at the same head and second are ONE exemption only
            # when ALL of them are successful exemptions with a non-null
            # IDENTICAL authenticated classifier identity (CTO ts
            # 1795416141.013939). Every candidate gets the same full proof as
            # the first: one unique classify step, authenticated bindings
            # (HEAD_SHA), the classifier-receipt tuple, and the exemption
            # job shape (empty steps for a skipped aggregate). Any other
            # difference - a failure, a pending run, a different scope, an
            # executed-vs-exempt mismatch - still refuses.
            if run["run_started_at"] != selected_time:
                return None
            if (len([step for step in scope.get("steps", [])
                    if step.get("name") == "Classify paid E2E scope"
                    and successful(step)]) != 1
                    or sum(step.get("name") == "Classify paid E2E scope"
                           for step in scope.get("steps", [])) != 1):
                return None
            if (e2e_duplicate_classifier_identity(scope) != duplicate_identity
                    or duplicate_identity is None
                    or not e2e_exemption_shape(pr, run, jobs, scope, classifier_proof)):
                return None
            continue
        if (not successful(run) or not successful(scope)
                or len([step for step in scope.get("steps", [])
                        if step.get("name") == "Classify paid E2E scope" and successful(step)]) != 1
                or sum(step.get("name") == "Classify paid E2E scope"
                       for step in scope.get("steps", [])) != 1
                or any(job.get("status") != "completed"
                       or job.get("conclusion") not in ("success", "skipped") for job in jobs)):
            return None
        exempt = e2e_exemption_shape(pr, run, jobs, scope, classifier_proof)
        if not exempt:
            return None
        selected = {**classifier_proof, "workflow": "e2e.yml", "run": run["id"],
                    "attempt": run.get("run_attempt", 1), "skipped": True}
        selected_time = run["run_started_at"]
        # Only an exemption may be matched by later duplicates; an executed run
        # never merges with a skipped sibling.
        duplicate_identity = e2e_duplicate_classifier_identity(scope)
    return selected


def site_paths_confined(pr, rules_bytes):
    """Require the complete PR file set to match trusted site or control-plane globs.

    The classifier receipt (scope=site, product_changed=false, ci/e2e not
    required) is already verified by the caller. A site diff may carry
    control_plane_only paths (docs/**, benchmarks/**, ...) alongside site paths;
    the classifier exempts both. Any path outside both rule groups (app, product,
    app_ci, fixtures) still fails closed.
    """
    return paths_confined(pr, rules_bytes,
                          ("site", "control_plane_only", "control_plane_legacy"))


def site_only_paths_confined(pr, rules_bytes):
    """Require every changed path to match the trusted `site` classifier globs only."""
    return paths_confined(pr, rules_bytes, ("site",))


CONTROL_PLANE_RULE_GROUPS = ("control_plane_only", "control_plane_legacy", "control_plane_ci")


def control_plane_paths_confined(pr, rules_bytes):
    """Require every changed path to match trusted control-plane classifier globs."""
    return paths_confined(pr, rules_bytes, CONTROL_PLANE_RULE_GROUPS,
                          exact_paths=CLASSIFIER_SELF_EDIT_PATHS)


def control_plane_self_edit_confined(pr, base_sha, receipt, rules_bytes):
    """Check the classifier's own edit against GitHub's immutable base/head diff."""
    comparison = read_with_retry(api, f"compare/{base_sha}...{pr['head']['sha']}")
    files = comparison.get("files") if isinstance(comparison, dict) else None
    # GitHub compare caps file rows at 300, even with commit pagination.
    if not isinstance(files, list) or not 0 < len(files) < 300:
        return False
    paths = changed_paths(files)
    logged = receipt.get("changed_files")
    return (paths is not None and CLASSIFIER_SOURCE in paths
            and isinstance(logged, list) and all(isinstance(p, str) for p in logged)
            and set(logged) == paths
            and paths_match_rules(paths, rules_bytes, CONTROL_PLANE_RULE_GROUPS,
                                  exact_paths=CLASSIFIER_SELF_EDIT_PATHS))


def changed_paths(files):
    paths = set()
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
            return None
        paths.add(item["filename"])
        previous = item.get("previous_filename")
        if previous is not None:
            if not isinstance(previous, str):
                return None
            paths.add(previous)
    return paths


def paths_match_rules(paths, rules_bytes, rule_names, *, exact_paths=()):
    rules = json.loads(rules_bytes)
    if not isinstance(rules, dict):
        return False
    pattern_lists = [rules.get(name, []) for name in rule_names]
    if any(not isinstance(patterns, list) or not all(
            isinstance(pattern, str) and pattern for pattern in patterns)
           for patterns in pattern_lists):
        return False
    patterns = [pattern for group in pattern_lists for pattern in group]
    return bool(paths) and all(
        path in exact_paths or any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)
        for path in paths)


def paths_confined(pr, rules_bytes, rule_names, *, exact_paths=()):
    try:
        pages_json = json.loads(command(
            "gh", "api", "--paginate", "--slurp",
            f"repos/{REPO}/pulls/{pr['number']}/files?per_page=100"))
        if not isinstance(pages_json, list) or not all(isinstance(page, list) for page in pages_json):
            return False
        paths = changed_paths(item for page in pages_json for item in page)
        return paths is not None and paths_match_rules(paths, rules_bytes, rule_names,
                                                       exact_paths=exact_paths)
    except (KeyError, TypeError, ValueError, Refusal, OSError, subprocess.TimeoutExpired):
        return False


def ci_exemption_proof(pr, expected_scope):
    """Accept a confined exact-head decision from the shared CI Exemption run."""
    head = pr["head"]["sha"]
    runs = pages(
        f"actions/workflows/{SITE_EXEMPTION_WORKFLOW}/runs?event=pull_request&head_sha={head}&per_page=100",
        "workflow_runs")
    runs.sort(key=lambda run: (run["id"], run.get("run_attempt", 1)), reverse=True)
    expected_path = f".github/workflows/{SITE_EXEMPTION_WORKFLOW}"
    for run in runs:
        if (run.get("head_sha") != head or run.get("event") != "pull_request"
                or run.get("head_branch") != pr["head"]["ref"]
                or run.get("path", "").split("@")[0] != expected_path):
            continue
        if not successful(run):
            return None
        jobs = pages(f"actions/runs/{run['id']}/jobs?filter=latest&per_page=100", "jobs")
        scope_jobs = [job for job in jobs if job.get("name") == SITE_CLASSIFIER_JOB_NAME]
        if len(scope_jobs) != 1 or not successful(scope_jobs[0]):
            return None
        return classifier_exemption(pr, SITE_EXEMPTION_WORKFLOW, run, scope_jobs[0],
                                    expected_scope=expected_scope, jobs=jobs)
    return None


def site_classifier_exemption(pr):
    return ci_exemption_proof(pr, "site")


def control_plane_classifier_exemption(pr):
    return ci_exemption_proof(pr, "control_plane_only")


def merge(number, head, apply=False, unrelated_main=None):
    pr = pr_at(number, head)
    if pr.get("merged"):
        return {"status": "ALREADY_MERGED", "head": head, "merge_commit": pr["merge_commit_sha"]}
    try:
        site_exempt = site_classifier_exemption(pr)
    except (Refusal, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        site_exempt = None
    try:
        control_plane_exempt = (None if site_exempt is not None
                                else control_plane_classifier_exemption(pr))
    except (Refusal, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        control_plane_exempt = None
    exemption = site_exempt or control_plane_exempt
    if exemption is not None:
        proof = {workflow: dict(exemption) for workflow in WORKFLOWS}
    else:
        ci_proof = workflow_proof(pr, "ci.yml", WORKFLOWS["ci.yml"])
        required_set = ci_proof.get("classifier_required_set")
        test_only = ci_proof.get("app_test_only")
        e2e_proof = app_test_only_e2e_proof(pr, test_only) if test_only else None
        if test_only and e2e_proof is None:
            raise Refusal("UNREQUIRED_E2E_NOT_SUCCESSFULLY_SKIPPED")
        if required_set is not None:
            if required_set["e2e_required"]:
                e2e_proof = workflow_proof(pr, "e2e.yml", WORKFLOWS["e2e.yml"])
            else:
                e2e_proof = app_test_only_e2e_proof(pr, required_set)
                if e2e_proof is None:
                    raise Refusal("UNREQUIRED_E2E_NOT_SUCCESSFULLY_SKIPPED")
        if e2e_proof is None:
            required = workflow_requirements(number, head)
            if required["e2e_required"]:
                e2e_proof = workflow_proof(pr, "e2e.yml", WORKFLOWS["e2e.yml"])
            else:
                e2e_proof = {**required, "required": False, "skipped": True,
                             "source": "canonical_main_classifier"}
        proof = {"ci.yml": ci_proof, "e2e.yml": e2e_proof}
    main = api("git/ref/heads/main")["object"]["sha"]
    comparison = api(f"compare/{head}...{main}")
    later = comparison["merge_base_commit"]["sha"] != main
    result = {"status": "READY_TO_MERGE", "pr": number, "head": head, "main": main,
              "workflows": proof}
    if later:
        result["main_delta"] = {"base": comparison["merge_base_commit"]["sha"],
            "head": main, "url": comparison["html_url"],
            "files": [{"filename": file["filename"], "status": file["status"],
                       "previous_filename": file.get("previous_filename")} for file in comparison.get("files", [])]}
        if unrelated_main != main:
            result["status"] = "MAIN_DELTA_REVIEW_REQUIRED"
            if apply:
                raise Refusal(f"MAIN_DELTA_REVIEW_REQUIRED main={main}")
    if not apply:
        return result
    pr_at(number, head)
    if api("git/ref/heads/main")["object"]["sha"] != main:
        raise Refusal("MAIN_CHANGED_DURING_CHECK")
    try:
        command("gh", "pr", "merge", str(number), "--repo", REPO, "--squash",
                "--delete-branch", "--match-head-commit", head)
    except (Refusal, OSError, subprocess.TimeoutExpired) as exc:
        raise Refusal(f"MERGE_UNCERTAIN head={head}: {exc}") from exc
    try:
        stored = pr_at(number, head)
        if not stored.get("merged") or not stored.get("merge_commit_sha"):
            raise Refusal("MERGE_NOT_CONFIRMED")
    except (Refusal, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        raise Refusal(f"MERGE_UNCERTAIN head={head}: {exc}") from exc
    return {**result, "status": "MERGED", "merge_commit": stored["merge_commit_sha"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--head", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--unrelated-main")
    args = parser.parse_args()
    if args.pr <= 0 or not re.fullmatch(r"[0-9a-f]{40}", args.head):
        parser.error("positive PR number and full lowercase head SHA required")
    if args.unrelated_main and not re.fullmatch(r"[0-9a-f]{40}", args.unrelated_main):
        parser.error("--unrelated-main requires the full inspected main SHA")
    try:
        print(json.dumps(merge(args.pr, args.head, args.apply, args.unrelated_main), sort_keys=True))
        return 0
    except (Refusal, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "REFUSED", "reason": str(exc)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
