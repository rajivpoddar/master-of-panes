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
