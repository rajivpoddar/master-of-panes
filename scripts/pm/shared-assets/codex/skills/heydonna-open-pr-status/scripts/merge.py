#!/usr/bin/env python3
"""Verify actual CI/E2E execution and squash a PR with a head pin."""

import argparse
import json
import os
import re
import subprocess
import sys

REPO = "heydonna-app/heydonna-app"
WORKFLOWS = {"ci.yml": ("typescript", "python", "test"), "e2e.yml": ("e2e",)}

# Off-slot review-verdict gate (PR #7922 merge-path violation correction).
# An OFF-SLOT PR (no slot:<n> label) carrying pm-state:pm-review-pending must
# have an APPROVE verdict bound to the exact current head before any merge
# effect. Otherwise the merge refuses with OFFSLOT_REVIEW_VERDICT_MISSING.
REVIEW_PENDING_LABEL = "pm-state:pm-review-pending"
MARKER_DIR = "/tmp"
MARKER_PREFIX = "codex-app-code-review-"
# GitHub author associations that count as the canonical reviewer identity.
# Automation/bot approvals (NONE) never satisfy this gate.
REVIEWER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


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
    if pr["base"]["ref"] != "main" or (pr["head"].get("repo") or {}).get("full_name") != REPO:
        raise Refusal("PR_MUST_TARGET_MAIN_IN_SAME_REPOSITORY")
    return pr



def pr_labels(pr):
    return [label["name"] for label in pr.get("labels", [])]


def has_slot_label(pr):
    return any(name.startswith("slot:") for name in pr_labels(pr))


def exact_head_github_approval(pr, head):
    """Return an accepted reviewer approval bound to head, else None.

    Only an APPROVED review submitted against the exact head commit by a
    canonical reviewer identity (OWNER/MEMBER/COLLABORATOR association)
    counts. Fetch failure means no approval (fail closed downstream).
    """
    try:
        reviews = api(f"pulls/{pr['number']}/reviews")
    except (Refusal, OSError, ValueError, subprocess.TimeoutExpired):
        return None
    for review in reviews or []:
        if (review.get("state") == "APPROVED"
                and (review.get("commit_id") or "") == head
                and review.get("author_association") in REVIEWER_ASSOCIATIONS):
            return {"user": ((review.get("user") or {}).get("login") or "?"),
                    "review_id": review.get("id")}
    return None


def marker_path(number):
    return os.path.join(MARKER_DIR, f"{MARKER_PREFIX}{number}.txt")


def read_review_marker(number):
    try:
        with open(marker_path(number), "r", encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


def validate_review_marker(text, number, head):
    """Validate the canonical machine-local review marker.

    Contract from genuine companion output (observed PR #7925 marker):
    first line VERDICT: APPROVE, COMPANION_VERDICT/FINAL_REVIEWER_VERDICT
    APPROVE, MARKER_PROVENANCE codex-review-companion, TYPE code-review,
    bare-epoch TIMESTAMP, PR #<number>, HEAD_SHA == head, Findings scaffold,
    no unexpanded shell in the scaffold header, no open blockers.
    Raises Refusal on any deviation. Returns the marker source receipt.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "VERDICT: APPROVE":
        raise Refusal(f"OFFSLOT_REVIEW_MARKER_INVALID pr={number}: missing VERDICT: APPROVE")
    try:
        end = next(i for i, line in enumerate(lines) if line.startswith("--- Findings ("))
    except StopIteration:
        raise Refusal(f"OFFSLOT_REVIEW_MARKER_INVALID pr={number}: missing Findings scaffold")
    header = lines[:end]
    if any(re.search(r"\$\(|\$[A-Z]", line) for line in header):
        raise Refusal(f"OFFSLOT_REVIEW_MARKER_INVALID pr={number}: unexpanded shell in scaffold")
    if any("BLOCKER_STATUS: OPEN" in line for line in lines):
        raise Refusal(f"OFFSLOT_REVIEW_MARKER_INVALID pr={number}: open blockers present")
    required = ("COMPANION_VERDICT: APPROVE", "FINAL_REVIEWER_VERDICT: APPROVE",
                "MARKER_PROVENANCE: codex-review-companion", "TYPE: code-review",
                f"PR: #{number}")
    for want in required:
        if want not in header:
            raise Refusal(f"OFFSLOT_REVIEW_MARKER_INVALID pr={number}: missing {want}")
    stamp = next((line.split(":", 1)[1].strip() for line in header
                  if line.startswith("TIMESTAMP:")), "")
    if not re.fullmatch(r"[0-9]{9,11}", stamp):
        raise Refusal(f"OFFSLOT_REVIEW_MARKER_INVALID pr={number}: bad TIMESTAMP")
    sha = next((line.split(":", 1)[1].strip() for line in header
                if line.startswith("HEAD_SHA:")), "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or sha != head:
        raise Refusal(f"OFFSLOT_REVIEW_MARKER_INVALID pr={number}: HEAD_SHA mismatch")
    for line in header:
        if line.startswith("headRefOid:") and line.split(":", 1)[1].strip() != head:
            raise Refusal(f"OFFSLOT_REVIEW_MARKER_INVALID pr={number}: headRefOid mismatch")
    return {"marker": marker_path(number), "head_sha": sha}


def review_gate(pr, head):
    """Fail-closed off-slot review-verdict gate.

    Slot-origin PRs and PRs without the pending label are behavior-identical
    (skipped/inert receipts). An off-slot PR carrying the pending label must
    present an exact-head APPROVE verdict (canonical GitHub approval first,
    machine-local marker fallback). Returns a receipt dict; raises Refusal
    with OFFSLOT_REVIEW_VERDICT_MISSING otherwise, before any merge effect.
    """
    number = pr["number"]
    if has_slot_label(pr):
        return {"review": "SKIPPED_SLOT_ORIGIN"}
    if REVIEW_PENDING_LABEL not in pr_labels(pr):
        return {"review": "INERT_PENDING_LABEL_ABSENT"}
    approval = exact_head_github_approval(pr, head)
    if approval:
        return {"review": "VERDICT_OK", "source": "github", **approval}
    marker = read_review_marker(number)
    if marker is not None:
        receipt = validate_review_marker(marker, number, head)
        return {"review": "VERDICT_OK", "source": "marker", **receipt}
    raise Refusal(f"OFFSLOT_REVIEW_VERDICT_MISSING pr={number} head={head}")


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
        scope = next((job for job in jobs if job["name"] == "classify-change-scope"), None)
        if scope and scope.get("conclusion") == "skipped":
            continue
        by_name = {job["name"]: job for job in jobs}
        if scope is not None and successful(scope):
            exempt = classifier_exemption(pr, workflow, run, scope)
            if exempt is not None:
                return exempt
        if not successful(run) or not all(successful(by_name.get(name, {})) for name in names):
            raise Refusal(f"WORKFLOW_NOT_GREEN workflow={workflow} run={run['id']}")
        # These steps distinguish real test execution from successful wrapper jobs.
        checks = (("typescript", "Unit tests"),) if workflow == "ci.yml" else (
            ("e2e", "Run E2E auto-process-critical"),
            ("e2e", "Run E2E core-rest shard 1/2"),
            ("e2e", "Run E2E core-rest shard 2/2"))
        for job_name, prefix in checks:
            if not any(step["name"].startswith(prefix) and successful(step)
                       for step in by_name[job_name].get("steps", [])):
                raise Refusal(f"TEST_EXECUTION_MISSING workflow={workflow} run={run['id']} step={prefix}")
        return {"run": run["id"], "attempt": run.get("run_attempt", 1), "head": head}
    raise Refusal(f"WORKFLOW_MISSING workflow={workflow}")


# ---------- classifier exemption (control-plane-only PRs) ----------
CLASSIFIER_JOB_NAME = "classify-change-scope"
EXEMPTION_RECEIPT = {
    "schema_version": 1,
    "scope": "control_plane_only",
    "paid_ci_exempt": True,
    "control_plane_only": True,
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


def classifier_receipt(lines):
    """Exactly one schema_version=1 JSON receipt, else None."""
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
    for key, expected in EXEMPTION_RECEIPT.items():
        if receipt.get(key) != expected:
            return None
    rules = receipt.get("rules_sha256")
    if not isinstance(rules, str) or not HEX64.match(rules):
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
            continue
        key, value = match.group(1), match.group(2)
        if key in seen:
            seen[key].add(value)
    if any(len(values) != 1 for values in seen.values()):
        return None
    return {key: next(iter(values)) for key, values in seen.items()}


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


def classifier_exemption(pr, workflow, run, scope):
    """Fail-closed verifier. Returns an exemption proof, or None to fall through."""
    head = pr["head"]["sha"]
    if not successful(run) or not successful(scope):
        return None
    lines = strip_log_framing(job_log(scope["id"]))
    receipt = classifier_receipt(lines)
    if receipt is None:
        return None
    bindings = classifier_bindings(lines)
    if bindings is None:
        return None
    base_sha, head_sha = bindings["BASE_SHA"], bindings["HEAD_SHA"]
    workflow_sha, event = bindings["WORKFLOW_SHA"], bindings["EVENT_NAME"]
    if not (HEX40.match(base_sha) and HEX40.match(workflow_sha)):
        return None
    if head_sha != head or event != "pull_request" or bindings["PR_NUMBER"] != str(pr["number"]):
        return None
    commit = api(f"git/commits/{workflow_sha}")
    parents = [parent["sha"] for parent in commit.get("parents", [])]
    if parents != [base_sha, head_sha]:
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
    return {"run": run["id"], "attempt": run.get("run_attempt", 1), "head": head,
            "scope_job_id": scope["id"], "workflow_sha": workflow_sha,
            "rules_sha256": receipt["rules_sha256"], "exempt": "control_plane_only"}


def merge(number, head, apply=False, unrelated_main=None):
    pr = pr_at(number, head)
    if pr.get("merged"):
        return {"status": "ALREADY_MERGED", "head": head, "merge_commit": pr["merge_commit_sha"]}
    gate = review_gate(pr, head)
    proof = {workflow: workflow_proof(pr, workflow, names) for workflow, names in WORKFLOWS.items()}
    main = api("git/ref/heads/main")["object"]["sha"]
    comparison = api(f"compare/{head}...{main}")
    later = comparison["merge_base_commit"]["sha"] != main
    result = {"status": "READY_TO_MERGE", "pr": number, "head": head, "main": main, "workflows": proof,
              "review": gate}
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
