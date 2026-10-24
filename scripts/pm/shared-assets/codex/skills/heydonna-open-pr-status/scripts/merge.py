#!/usr/bin/env python3
"""Verify actual CI/E2E execution and squash a PR with a head pin."""

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from classifier_requirements import classify as workflow_requirements

REPO = "heydonna-app/heydonna-app"
SAME_REPO_NAMES = {REPO.lower(), "scribie/heydonna-app"}
WORKFLOWS = {"ci.yml": ("typescript", "python", "test"), "e2e.yml": ("e2e",)}
SITE_EXEMPTION_WORKFLOW = "ci-dummy.yml"
SITE_CLASSIFIER_JOB_NAME = "detect-docs-only"


class Refusal(RuntimeError):
    pass


def command(*args):
    result = subprocess.run(args, text=True, capture_output=True, timeout=120)
    if result.returncode:
        raise Refusal(result.stderr.strip() or result.stdout.strip() or "command failed")
    return result.stdout


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
    # GitHub resolves the heydonna-app/heydonna-app alias to the canonical Scribie/heydonna-app.
    head_repo = pr["head"].get("repo") or {}
    base_repo = pr["base"].get("repo") or {}
    same_repo = (head_repo.get("full_name") or "").lower() in SAME_REPO_NAMES and (
        head_repo.get("id") is None or base_repo.get("id") is None or head_repo.get("id") == base_repo.get("id"))
    if pr["base"]["ref"] != "main" or not same_repo:
        raise Refusal("PR_MUST_TARGET_MAIN_IN_SAME_REPOSITORY")
    return pr


def successful(item):
    return item.get("status") == "completed" and item.get("conclusion") == "success"


def workflow_proof(pr, workflow, names):
    head = pr["head"]["sha"]
    runs = pages(f"actions/workflows/{workflow}/runs?event=pull_request&head_sha={head}&per_page=100", "workflow_runs")
    runs.sort(key=lambda run: (run["id"], run.get("run_attempt", 1)), reverse=True)
    for run in runs:
        if (run["head_sha"] != head or run["event"] != "pull_request"
                or run["head_branch"] != pr["head"]["ref"]
                or run.get("path", "").split("@")[0] != f".github/workflows/{workflow}"):
            continue
        jobs = pages(f"actions/runs/{run['id']}/jobs?filter=latest&per_page=100", "jobs")
        scope_jobs = [job for job in jobs if job.get("name") == CLASSIFIER_JOB_NAME]
        scope = scope_jobs[0] if scope_jobs else None
        if scope and scope.get("conclusion") == "skipped":
            continue
        by_name = {job["name"]: job for job in jobs}
        test_only = None
        if scope is not None and successful(scope):
            exempt = classifier_exemption(pr, workflow, run, scope)
            if exempt is not None:
                return exempt
            if workflow == "ci.yml" and len(scope_jobs) == 1:
                try:
                    test_only = app_test_only_classifier_proof(pr, workflow, run, scope)
                except (Refusal, OSError, ValueError, KeyError,
                        subprocess.TimeoutExpired):
                    test_only = None
            if workflow == "ci.yml" and len(scope_jobs) == 1:
                required_set = ci_required_set_proof(pr, run, scope)
                if required_set is not None:
                    verify_ci_required_jobs(run, jobs, required_set)
                    return {"run": run["id"], "attempt": run.get("run_attempt", 1),
                            "head": head, "classifier_required_set": required_set}
        # A verified app-test-only receipt lets the classifier skip the python job.
        required = [name for name in names
                    if not (test_only is not None and name == "python"
                            and by_name.get(name, {}).get("conclusion") == "skipped")]
        if not successful(run) or not all(successful(by_name.get(name, {})) for name in required):
            raise Refusal(f"WORKFLOW_NOT_GREEN workflow={workflow} run={run['id']}")
        # These steps distinguish real test execution from successful wrapper jobs.
        # ci.yml runs unit tests in sharded "unit-tests (N)" jobs; legacy runs used typescript.
        unit_shards = sorted(name for name in by_name if name.startswith("unit-tests ("))
        if workflow == "ci.yml":
            checks = tuple((name, "Unit tests") for name in unit_shards) or (("typescript", "Unit tests"),)
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
    "schema_version": 1,
    "paid_ci_exempt": True,
    "product_changed": False,
    "ci_required": False,
    "e2e_required": False,
}
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
    """Raw job log text via the existing gh transport (no JSON decode)."""
    return command("gh", "api", f"repos/{REPO}/actions/jobs/{job_id}/logs")


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
        if receipt.get(key) != expected:
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


def classifier_exemption(pr, workflow, run, scope, expected_scope="control_plane_only"):
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
    commit = api(f"git/commits/{workflow_sha}")
    parents = [parent["sha"] for parent in commit.get("parents", [])]
    if workflow == SITE_EXEMPTION_WORKFLOW and expected_scope in ("site", "control_plane_only"):
        # BASE_SHA is the common merge base, while GitHub's synthetic merge
        # commit first parent is the current base-branch tip. Main may advance
        # between those points; bind the second parent to the exact PR head.
        if (len(parents) != 2 or not HEX40.match(parents[0])
                or parents[1] != head_sha):
            return None
    elif parents != [base_sha, head_sha]:
        return None
    workflow_path = f".github/workflows/{workflow}"
    for path in (workflow_path, "scripts/ci/change_scope.py", "scripts/ci/change-scope-rules.json"):
        at_base = contents_identity(path, base_sha)
        at_workflow = contents_identity(path, workflow_sha)
        if at_base is None or at_workflow is None or at_base[0] != at_workflow[0]:
            return None
    rules = contents_identity("scripts/ci/change-scope-rules.json", workflow_sha)
    if rules is None:
        return None
    import hashlib
    if hashlib.sha256(rules[1]).hexdigest() != receipt["rules_sha256"]:
        return None
    if workflow == SITE_EXEMPTION_WORKFLOW and expected_scope == "site" \
            and not site_paths_confined(pr, rules[1]):
        return None
    if (workflow == SITE_EXEMPTION_WORKFLOW and expected_scope == "control_plane_only"
            and not control_plane_paths_confined(pr, rules[1])):
        return None
    return {"run": run["id"], "attempt": run.get("run_attempt", 1), "head": head,
            "scope_job_id": scope["id"], "workflow_sha": workflow_sha,
            "rules_sha256": receipt["rules_sha256"], "exempt": expected_scope}


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
    commit = api(f"git/commits/{workflow_sha}")
    if [parent["sha"] for parent in commit.get("parents", [])] != [base_sha, head_sha]:
        return None
    for path in (".github/workflows/ci.yml", "scripts/ci/change_scope.py",
                 "scripts/ci/change-scope-rules.json"):
        at_base = contents_identity(path, base_sha)
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
        pages_json = json.loads(command(
            "gh", "api", "--paginate", "--slurp",
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


def ci_required_set_proof(pr, run, scope):
    """Bind language gates to the same trusted native CI receipt, never PR prose."""
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
        return None
    flags = ("ts_changed", "python_changed", "e2e_required")
    if any(type(receipt.get(key)) is not bool for key in flags):
        return None
    if not (receipt["ts_changed"] or receipt["python_changed"]):
        return None
    bindings = classifier_bindings(lines)
    if bindings is None:
        return None
    base, head, workflow = (bindings["BASE_SHA"], bindings["HEAD_SHA"],
                            bindings["WORKFLOW_SHA"])
    if not (HEX40.fullmatch(base) and HEX40.fullmatch(workflow)):
        return None
    if (head != pr["head"]["sha"] or run.get("head_sha") != head
            or run.get("head_branch") != pr["head"]["ref"]
            or run.get("path", "").split("@")[0] != ".github/workflows/ci.yml"
            or not successful(run) or not successful(scope)
            or not event_binding_ok(bindings, run, pr)
            or not pr_binding_ok(bindings, run, pr)
            or not native_pr_binding_ok(run, pr)):
        return None
    parents = api(f"git/commits/{workflow}").get("parents", [])
    if [item["sha"] for item in parents] != [base, head]:
        return None
    for path in (".github/workflows/ci.yml", "scripts/ci/change_scope.py",
                 "scripts/ci/change-scope-rules.json"):
        before, selected = contents_identity(path, base), contents_identity(path, workflow)
        if before is None or selected is None or before[0] != selected[0]:
            return None
    rules = contents_identity("scripts/ci/change-scope-rules.json", workflow)
    import hashlib
    if (rules is None or hashlib.sha256(rules[1]).hexdigest() != receipt.get("rules_sha256")
            or not receipt_files_match_pr(pr, receipt)):
        return None
    return {"head": head, "run": run["id"], "attempt": run.get("run_attempt", 1),
            "scope_job_id": scope["id"], "workflow_sha": workflow,
            "rules_sha256": receipt["rules_sha256"],
            **{key: receipt[key] for key in flags}}


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
    groups = {
        "ts_changed": {"typescript": "Typecheck", "unit-tests-isolated": "Isolated unit tests",
                       **{f"unit-tests ({i})": "Unit tests" for i in (1, 2, 3)}},
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


def app_test_only_e2e_proof(pr, classifier_proof):
    """Allow E2E absence or an exact-head successful run with its E2E job skipped."""
    head = pr["head"]["sha"]
    runs = pages(f"actions/workflows/e2e.yml/runs?event=pull_request&head_sha={head}&per_page=100",
                 "workflow_runs")
    runs.sort(key=lambda run: (run["id"], run.get("run_attempt", 1)), reverse=True)
    matching = [run for run in runs if (
        run.get("head_sha") == head and run.get("event") == "pull_request"
        and run.get("head_branch") == pr["head"]["ref"]
        and run.get("path", "").split("@")[0] == ".github/workflows/e2e.yml")]
    if not matching:
        return {**classifier_proof, "workflow": "e2e.yml", "run": None, "skipped": True}
    run = matching[0]
    if not successful(run):
        return None
    jobs = pages(f"actions/runs/{run['id']}/jobs?filter=latest&per_page=100", "jobs")
    if "ts_changed" in classifier_proof and any(
            job.get("run_id") != run["id"]
            or job.get("run_attempt") != run.get("run_attempt", 1)
            or job.get("head_sha") != head
            or job.get("status") != "completed"
            or job.get("conclusion") not in ("success", "skipped") for job in jobs):
        return None
    e2e_jobs = [job for job in jobs if job.get("name") == "e2e"]
    if len(e2e_jobs) != 1 or e2e_jobs[0].get("conclusion") != "skipped":
        return None
    return {**classifier_proof, "workflow": "e2e.yml", "run": run["id"],
            "attempt": run.get("run_attempt", 1), "skipped": True}


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


def control_plane_paths_confined(pr, rules_bytes):
    """Require every changed path to match trusted control-plane classifier globs."""
    return paths_confined(pr, rules_bytes,
                          ("control_plane_only", "control_plane_legacy", "control_plane_ci"))


def paths_confined(pr, rules_bytes, rule_names):
    try:
        rules = json.loads(rules_bytes)
        pattern_lists = [rules.get(name, []) for name in rule_names]
        if any(not isinstance(patterns, list) or not all(
                isinstance(pattern, str) and pattern for pattern in patterns)
               for patterns in pattern_lists):
            return False
        patterns = [pattern for group in pattern_lists for pattern in group]
        if not patterns:
            return False
        pages_json = json.loads(command(
            "gh", "api", "--paginate", "--slurp",
            f"repos/{REPO}/pulls/{pr['number']}/files?per_page=100"))
        if not isinstance(pages_json, list) or not pages_json:
            return False
        paths = []
        for page in pages_json:
            if not isinstance(page, list):
                return False
            for item in page:
                if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
                    return False
                paths.append(item["filename"])
                previous = item.get("previous_filename")
                if previous is not None:
                    if not isinstance(previous, str):
                        return False
                    paths.append(previous)
        return bool(paths) and all(
            any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)
            for path in paths)
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
                                    expected_scope=expected_scope)
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
