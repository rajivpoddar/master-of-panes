#!/usr/bin/env python3
"""MoP-owned assignment recommendation for an idle free dev slot.

Recommends durable existing-PR work before the highest-priority open
status:todo issue. CI/E2E failures remain a GitHub check-rollup source, while
numbered-slot E2E boundaries come only from exact PM-ops obligations joined to
the live PR tuple. This file is installed with the MoP release; it is a
read-only recommendation gate and never mutates slot, PR, issue, or workflow
state.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any


DEFAULT_REPO = "heydonna-app/heydonna-app"
GH_BIN = "gh"
CI_FAILURE_SCAN_CACHE = Path("/tmp/heydonna-free-slot-ci-scan.json")
CI_FAILURE_SCAN_CACHE_SECONDS = 60
TODO_SCAN_CACHE = Path("/tmp/heydonna-free-slot-todo-scan.json")
TODO_SCAN_CACHE_SECONDS = 60
PM_OPS_DB = Path(
    os.environ.get(
        "PM_OPS_DB",
        str(
            Path.home()
            / ".claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db"
        ),
    )
)
WORKFLOW_FILES = ("ci.yml", "e2e.yml")
# Canonical statusCheckRollup workflowName values for the two paid lanes
# (workflow display names; file basenames are accepted as aliases).
CI_E2E_WORKFLOW_NAMES = {"CI", "E2E Smoke Tests", "ci.yml", "e2e.yml"}
BAD_CONCLUSIONS = {"failure", "cancelled", "timed_out", "action_required"}
READY_POOL_BLOCKING_LABELS = {
    "blocked",
    "dependency-blocked",
    "pm-blocked",
    "status:blocked",
    "status:dependency-blocked",
    "status:pm-blocked",
}


def positive_int(value: Any) -> int:
    try:
        return max(0, int(str(value)))
    except (TypeError, ValueError):
        return 0


def issue_priority(labels: list[Any]) -> int:
    for label in labels:
        name = str(label.get("name") or label if isinstance(label, dict) else label)
        match = re.search(r"\bP\s*([0-9])\b", name, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return 99


def issue_label_names(labels: Any) -> set[str]:
    if not isinstance(labels, list):
        return set()
    return {
        str(label.get("name") or label if isinstance(label, dict) else label).strip().lower()
        for label in labels
        if str(label.get("name") or label if isinstance(label, dict) else label).strip()
    }


def is_valid_unblocked_ready_pool_issue(row: dict[str, Any]) -> bool:
    """Require a real status:todo issue and reject explicit blocking labels."""
    labels = issue_label_names(row.get("labels"))
    title = str(row.get("title") or "").strip()
    return (
        bool(title)
        and "status:todo" in labels
        and not labels.intersection(READY_POOL_BLOCKING_LABELS)
    )


def _read_cache(cache: Path, seconds: int) -> Any | None:
    try:
        if not cache.is_file():
            return None
        if time.time() - cache.stat().st_mtime > seconds:
            return None
        return json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(cache: Path, value: Any) -> None:
    try:
        cache.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def run_gh_json(subcommand: list[str], fields: list[str], timeout: int = 25) -> Any:
    gh = os.environ.get("GH_BIN", GH_BIN)
    repo = os.environ.get("GH_REPO", DEFAULT_REPO)
    proc = subprocess.run(
        [gh, *subcommand, "--repo", repo, "--json", ",".join(fields)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh {' '.join(subcommand)} rc={proc.returncode}: "
            f"{(proc.stderr or proc.stdout).strip() or 'no output'}"
        )
    try:
        return json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh returned invalid JSON: {exc}") from exc


def run_gh_api(path: str, timeout: int = 25) -> Any:
    gh = os.environ.get("GH_BIN", GH_BIN)
    repo = os.environ.get("GH_REPO", DEFAULT_REPO)
    proc = subprocess.run(
        [gh, "api", f"repos/{repo}/{path.lstrip('/')}"],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh api {path} rc={proc.returncode}: "
            f"{(proc.stderr or proc.stdout).strip() or 'no output'}"
        )
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh api returned invalid JSON for {path}: {exc}") from exc


def build_ci_candidates(open_prs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map every open PR's current-head CI/E2E check state to red candidates.

    A PR is a candidate when its current-head check rollup contains a red
    CI (`ci.yml`) or E2E (`e2e.yml`) check. We read the PR's check rollup
    (statusCheckRollup) rather than workflow-run rows because workflow runs
    can be reported as `skipped`/`cancelled` even when a check within them is
    genuinely red at the current head.
    """
    candidates: list[dict[str, Any]] = []
    for pr in open_prs:
        number = positive_int(pr.get("number"))
        head = str(pr.get("headRefOid") or "")
        if not number or not head:
            continue
        checks = pr.get("statusCheckRollup") or []
        red_checks: list[dict[str, Any]] = []
        for check in checks:
            if not isinstance(check, dict):
                continue
            conclusion = str(check.get("conclusion") or "").upper()
            if conclusion not in {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}:
                continue
            workflow = str(
                check.get("workflowName")
                or check.get("workflow")
                or check.get("name")
                or ""
            )
            # Only real CI/E2E workflow checks may produce a ci_rework
            # candidate. statusCheckRollup also carries unrelated checks
            # (lint/security/notify), and a red non-CI check must never block
            # a free slot from selecting status:todo work.
            if workflow not in CI_E2E_WORKFLOW_NAMES:
                continue
            red_checks.append({**check, "workflow": workflow})
        if not red_checks:
            continue
        latest = max(
            red_checks,
            key=lambda row: str(
                row.get("completedAt")
                or row.get("completed_at")
                or row.get("startedAt")
                or ""
            ),
        )
        names = {str(c.get("name") or "") for c in red_checks}
        candidates.append(
            {
                "pr": number,
                "run_id": str(latest.get("detailsUrl") or ""),
                "head_sha": head,
                "url": str(latest.get("detailsUrl") or ""),
                "category": ", ".join(sorted(names)),
                "workflow": str(latest.get("workflow") or ""),
                "conclusion": str(latest.get("conclusion") or ""),
                "completed_at": str(
                    latest.get("completedAt")
                    or latest.get("completed_at")
                    or latest.get("startedAt")
                    or ""
                ),
            }
        )
    return sorted(candidates, key=lambda row: str(row.get("completed_at") or ""), reverse=True)


def _valid_sha(value: Any) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{40}", str(value or "")))


def _read_authoritative_slot_boundaries() -> tuple[list[dict[str, Any]], str | None]:
    """Read durable slot-required E2E boundaries without creating authority.

    ``pr_phase_a`` obligations owned by ``slot`` are the existing PM-ops
    boundary source. CI rollups and lifecycle labels are deliberately not
    used to manufacture a numbered-slot packet. A malformed slot-owned row
    makes the audit incomplete so callers cannot fall through to Ready Pool.
    """
    if not PM_OPS_DB.is_file():
        return [], f"authoritative_boundary_unavailable:missing_db:{PM_OPS_DB}"
    try:
        connection = sqlite3.connect(
            f"file:{PM_OPS_DB}?mode=ro", uri=True, timeout=5
        )
        try:
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(obligations)")
            }
            required = {
                "id",
                "status",
                "kind",
                "pr",
                "issue",
                "owner",
                "required_action",
                "evidence_json",
            }
            if not required.issubset(columns):
                return [], "authoritative_boundary_incomplete:obligations_schema"
            rows = connection.execute(
                """
                SELECT id, status, kind, pr, issue, owner, required_action, evidence_json
                  FROM obligations
                 WHERE status = 'open' AND kind = 'pr_phase_a'
                """
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        return [], f"authoritative_boundary_unavailable:{exc}"

    boundaries: list[dict[str, Any]] = []
    for row in rows:
        obligation_id, status, kind, pr_value, issue_value, owner, required_action, evidence_text = row
        pr = positive_int(pr_value)
        # Non-slot phase-A work is authoritative, but not a claimable
        # numbered-slot boundary and must not be treated as one.
        if str(owner or "") != "slot":
            continue
        if status != "open" or kind != "pr_phase_a" or not pr:
            return [], f"authoritative_boundary_incomplete:obligation={obligation_id}"
        try:
            evidence = json.loads(evidence_text or "{}")
        except (TypeError, json.JSONDecodeError):
            return [], f"authoritative_boundary_incomplete:obligation={obligation_id}"
        if not isinstance(evidence, dict):
            return [], f"authoritative_boundary_incomplete:obligation={obligation_id}"
        head = str(evidence.get("head") or "")
        dedup = str(evidence.get("dedup") or "")
        spec = str(evidence.get("spec") or "")
        action = str(required_action or "")
        # A durable slot-owned phase-A obligation can represent another
        # boundary (review, capture, or product rework). It is authoritative
        # but not claimable as numbered-slot E2E, so leave it visible to its
        # owning flow without manufacturing a slot recommendation here.
        if "e2e" not in f"{action} {spec}".lower():
            continue
        if (
            not _valid_sha(head)
            or not dedup
            or not spec
            or not action
            or (
                evidence.get("pr") is not None
                and positive_int(evidence.get("pr")) != pr
            )
            or (
                issue_value
                and evidence.get("issue") is not None
                and positive_int(evidence.get("issue")) != positive_int(issue_value)
            )
        ):
            return [], f"authoritative_boundary_incomplete:obligation={obligation_id}"
        boundaries.append(
            {
                "obligation_id": int(obligation_id),
                "pr": pr,
                "issue": positive_int(issue_value) or None,
                "head_sha": head,
                "next_boundary": "numbered-slot-e2e",
                "boundary_kind": "numbered-slot-e2e",
                "packet_id": str(
                    evidence.get("packet_id") or f"obligation:{obligation_id}"
                ),
                "handoff_id": str(evidence.get("handoff_id") or dedup),
                "wake_condition": action,
                "spec": spec,
                "dedup": dedup,
            }
        )
    return boundaries, None


def build_authoritative_slot_e2e_candidates(
    open_prs: list[dict[str, Any]],
    boundaries: list[dict[str, Any]],
    excluded_prs: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Join durable boundaries to each PR's exact live tuple."""
    excluded_prs = excluded_prs or set()
    by_pr = {
        positive_int(pr.get("number")): pr
        for pr in open_prs
        if isinstance(pr, dict) and positive_int(pr.get("number"))
    }
    candidates: list[dict[str, Any]] = []
    for boundary in boundaries:
        pr_number = positive_int(boundary.get("pr"))
        pr = by_pr.get(pr_number)
        if not pr or pr_number in excluded_prs:
            continue
        head = str(pr.get("headRefOid") or "")
        state = str(pr.get("state") or "").upper()
        mergeable = str(pr.get("mergeable") or "").upper()
        if mergeable not in {"MERGEABLE", "CLEAN"}:
            mergeable = str(pr.get("mergeStateStatus") or "").upper()
        if (
            state != "OPEN"
            or bool(pr.get("isDraft"))
            or mergeable not in {"MERGEABLE", "CLEAN"}
            or not _valid_sha(head)
            or head.lower() != str(boundary.get("head_sha") or "").lower()
            or not str(pr.get("headRefName") or "")
        ):
            continue
        candidates.append(
            {
                **boundary,
                "branch": str(pr.get("headRefName") or ""),
                "updated_at": str(pr.get("updatedAt") or ""),
                "lifecycle_labels": [
                    str(label.get("name") or label if isinstance(label, dict) else label)
                    for label in pr.get("labels") or []
                ],
            }
        )
    return sorted(
        candidates,
        key=lambda row: (str(row.get("updated_at") or ""), int(row.get("pr") or 0)),
        reverse=True,
    )


def github_slot_e2e_candidates(
    excluded_prs: set[int],
) -> tuple[list[dict[str, Any]], str | None]:
    """Audit PM-ops boundaries against every open PR's live tuple."""
    boundaries, boundary_error = _read_authoritative_slot_boundaries()
    if boundary_error:
        return [], boundary_error
    try:
        open_prs = run_gh_json(
            ["pr", "list", "--state", "open", "--limit", "1000"],
            [
                "number",
                "state",
                "isDraft",
                "mergeable",
                "mergeStateStatus",
                "headRefName",
                "headRefOid",
                "labels",
                "updatedAt",
            ],
        )
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
        return [], f"open_pr_boundary_audit_unavailable:{exc}"
    if not isinstance(open_prs, list):
        return [], "open_pr_boundary_audit_invalid_envelope"
    return build_authoritative_slot_e2e_candidates(open_prs, boundaries, excluded_prs), None


def github_ci_failure_candidates(
    excluded_prs: set[int],
) -> tuple[list[dict[str, Any]], str | None]:
    """Return open-PR CI/E2E failures at each PR's current head via check rollup."""
    cached = _read_cache(CI_FAILURE_SCAN_CACHE, CI_FAILURE_SCAN_CACHE_SECONDS)
    if cached is not None:
        # The cache is head-bound: a PR pushed to a new head inside the TTL
        # must not keep a stale red recommendation for the old head. A cheap
        # head-only read (no statusCheckRollup) re-validates every cached
        # candidate; any stale entry invalidates the cache and forces a
        # fresh rollup scan so a new red head is never missed.
        try:
            current = run_gh_json(
                ["pr", "list", "--state", "open"],
                ["number", "headRefOid"],
            )
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
            return [], f"github_scan_unavailable:{exc}"
        if not isinstance(current, list):
            return [], "github_scan_invalid_envelope"
        heads = {
            positive_int(row.get("number")): str(row.get("headRefOid") or "")
            for row in current
        }
        if any(
            not isinstance(candidate, dict)
            or positive_int(candidate.get("pr")) not in heads
            or str(candidate.get("head_sha") or "")
            != heads[positive_int(candidate.get("pr"))]
            for candidate in cached
        ):
            cached = None
    if cached is None:
        try:
            open_prs = run_gh_json(
                ["pr", "list", "--state", "open"],
                [
                    "number",
                    "headRefOid",
                    "headRefName",
                    "updatedAt",
                    "statusCheckRollup",
                ],
            )
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
            return [], f"github_scan_unavailable:{exc}"
        if not isinstance(open_prs, list):
            return [], "github_scan_invalid_envelope"

        candidates = build_ci_candidates(open_prs)
        _write_cache(CI_FAILURE_SCAN_CACHE, candidates)
        cached = candidates

    return [
        candidate
        for candidate in cached
        if isinstance(candidate, dict)
        and positive_int(candidate.get("pr")) not in excluded_prs
    ], None


def github_todo_candidates(
    excluded_issues: set[int],
) -> tuple[list[dict[str, Any]], str | None]:
    """Return open status:todo issues as the Ready Pool source."""
    cached = _read_cache(TODO_SCAN_CACHE, TODO_SCAN_CACHE_SECONDS)
    if cached is None:
        try:
            rows = run_gh_json(
                ["issue", "list", "--state", "open", "--label", "status:todo"],
                ["number", "title", "labels", "updatedAt"],
            )
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
            return [], f"todo_scan_unavailable:{exc}"
        if not isinstance(rows, list):
            return [], "todo_scan_invalid_envelope"
        _write_cache(TODO_SCAN_CACHE, rows)
        cached = rows

    candidates: list[dict[str, Any]] = []
    for row in cached:
        if not isinstance(row, dict):
            continue
        if not is_valid_unblocked_ready_pool_issue(row):
            continue
        issue = positive_int(row.get("number"))
        if not issue or issue in excluded_issues:
            continue
        candidates.append(
            {
                "issue": issue,
                "title": str(row.get("title") or ""),
                "priority": issue_priority(row.get("labels") or []),
                "updated_at": str(row.get("updatedAt") or ""),
                "url": f"https://github.com/{DEFAULT_REPO}/issues/{issue}",
            }
        )
    return candidates, None


def inspect_gate(
    slot: int,
    excluded_prs: set[int],
    excluded_issues: set[int] | None = None,
    ci_failures: list[dict[str, Any]] | None = None,
    todo_issues: list[dict[str, Any]] | None = None,
    slot_e2e_candidates: list[dict[str, Any]] | None = None,
    boundary_audit_error: str | None = None,
) -> dict[str, Any]:
    excluded_issues = excluded_issues or set()
    ci_failures = ci_failures or []
    todo_issues = todo_issues or []
    slot_e2e_candidates = slot_e2e_candidates or []
    result: dict[str, Any] = {
        "allowed": False,
        "slot": slot,
        "recommendation_kind": None,
        "rework_packet_count": 0,
        "rework_pr_count": 0,
        "ci_failure_count": 0,
        "ready_pool_size": 0,
        "recommended_obligation_id": None,
        "recommended_pr": None,
        "recommended_issue": None,
        "recommended_packet": None,
        "recommended_run_id": None,
        "recommended_ci_url": None,
        "recommended_category": None,
        "recommended_action": None,
        "slot_e2e_boundary_count": 0,
        "boundary_audit_complete": boundary_audit_error is None,
        "slot_dispatch_wedge_id": None,
        "reason": "no_assignable_obligation",
    }

    ci_candidates = [
        candidate
        for candidate in ci_failures
        if positive_int(candidate.get("pr")) not in excluded_prs
    ]
    slot_e2e_candidates = [
        candidate
        for candidate in slot_e2e_candidates
        if positive_int(candidate.get("pr")) not in excluded_prs
    ]
    todo_issues = [
        candidate
        for candidate in todo_issues
        if positive_int(candidate.get("issue")) not in excluded_issues
    ]
    result["ci_failure_count"] = len(ci_candidates)
    result["slot_e2e_boundary_count"] = len(slot_e2e_candidates)
    result["ready_pool_size"] = len(todo_issues)
    if slot_e2e_candidates:
        candidate = slot_e2e_candidates[0]
        pr = positive_int(candidate["pr"])
        result.update(
            {
                "allowed": True,
                "recommendation_kind": "slot_e2e",
                "recommended_pr": pr,
                "recommended_issue": positive_int(candidate.get("issue")) or None,
                "recommended_obligation_id": positive_int(candidate.get("obligation_id")) or None,
                "recommended_packet": str(candidate.get("packet_id") or ""),
                "recommended_action": (
                    "claim_slot "
                    f"pr={pr} slot={slot} obligation={candidate.get('obligation_id')} "
                    "verify exact repository/issue/branch/head/expected_epoch and handoff "
                    "before numbered-slot E2E"
                ),
                "recommended_category": "numbered-slot-e2e",
                "reason": "authoritative_numbered_slot_e2e_boundary_available",
            }
        )
        return result

    if ci_candidates:
        candidate = max(ci_candidates, key=lambda row: str(row.get("completed_at") or ""))
        pr = positive_int(candidate["pr"])
        result.update(
            {
                "allowed": True,
                "recommendation_kind": "ci_rework",
                "recommended_pr": pr,
                "recommended_issue": positive_int(candidate.get("issue")) or None,
                "recommended_run_id": str(candidate.get("run_id") or ""),
                "recommended_ci_url": str(candidate.get("url") or ""),
                "recommended_category": str(candidate.get("category") or ""),
                "recommended_action": (
                    "claim_slot "
                    f"pr={pr} slot={slot} resolve the exact branch/head/expected_epoch/handoff "
                    "from the current GitHub and MoP tuple before assign-rework"
                ),
                "reason": "ci_failure_rework_available",
            }
        )
        return result

    if boundary_audit_error:
        result.update(
            {
                "boundary_audit_reason": boundary_audit_error,
                "reason": "authoritative_boundary_audit_incomplete",
            }
        )
        return result

    if todo_issues:
        recommendation = min(
            todo_issues,
            key=lambda row: (
                int(row.get("priority") or 99),
                str(row.get("updated_at") or ""),
            ),
        )
        result.update(
            {
                "allowed": True,
                "recommendation_kind": "todo",
                "recommended_issue": positive_int(recommendation.get("issue")),
                "recommended_action": "assign-todo",
                "reason": "ready_pool_work_available",
            }
        )
        return result

    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", type=int, required=True, choices=range(1, 5))
    parser.add_argument("--exclude-pr", action="append", default=[], type=int)
    parser.add_argument("--exclude-issue", action="append", default=[], type=int)
    args = parser.parse_args()

    ci_failures, ci_error = github_ci_failure_candidates(set(args.exclude_pr))
    slot_e2e, slot_e2e_error = github_slot_e2e_candidates(set(args.exclude_pr))
    todo_issues, todo_error = github_todo_candidates(set(args.exclude_issue))
    result = inspect_gate(
        args.slot,
        set(args.exclude_pr),
        set(args.exclude_issue),
        ci_failures,
        todo_issues,
        slot_e2e,
        slot_e2e_error,
    )
    if ci_error:
        result["ci_triage_reason"] = ci_error
    if todo_error:
        result["todo_scan_reason"] = todo_error
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
