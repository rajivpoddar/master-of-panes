#!/usr/bin/env python3
"""Generate a deterministic delivery retro for a merged HeyDonna PR."""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO = "heydonna-app/heydonna-app"
VERSION = 12
MOP_BASE_URL = "http://127.0.0.1:3100"
COMPANION_MARKER_DIR = Path("/tmp/codex-review-companion")
CANONICAL_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
RETRO_STORE_DIR = Path(
    os.environ.get("HEYDONNA_RETRO_STORE_DIR")
    or str(Path.home() / ".claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/retros")
)
PM_OPERATOR_BIN = Path(
    os.environ.get("HEYDONNA_PM_OPERATOR_BIN")
    or "/Users/rajiv/.claude/pm-operator/current/bin/pm-operator"
)
PM_OPS_DB = Path(
    os.environ.get("PM_OPS_DB")
    or "/Users/rajiv/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db"
)
CLEANUP_START_SLA_SECONDS = int(os.environ.get("HEYDONNA_CLEANUP_START_SLA_SECONDS") or "900")
PM_REVIEW_MARKER_GLOBS = (
    "/tmp/pm-claude-code-review-{pr}-*.md",
    "/tmp/pm-opus-code-review-{pr}-*.md",
    "/tmp/pm-claude-plan-review-{pr}-*.md",
    "/tmp/pm-opus-plan-review-{pr}-*.md",
    "/tmp/pm-claude-plan-review-{issue}-*.md",
    "/tmp/pm-opus-plan-review-{issue}-*.md",
    "/tmp/pm-plan-patch-{issue}-*.patch",
)


TERM_PATTERNS = {
    "ci_failure": r"ci[- ]failure|CI failure|failed CI|unit-test-fail|code-regression",
    "wall_budget_timeout": r"wall[- ]budget|timeout|timed out|cancelled|canceled",
    "capture_cache": r"capture|recapture|fixture|LLM proxy|proxy cache|cache miss|R2 cold|cold[- ]fetch|cold stall",
    "dependency": r"dependency|blocked on|linchpin|predecessor|supersedes",
    "local_preflight": r"local preflight|affected[-_ ]test|failed[-_ ]suite|rerun locally|passes locally|local repro",
    "review_rework": r"rework|REQUEST_CHANGES|NEEDS_REVISION|PM Claude|PM-Opus|Codex Review|P[0-2]",
    "rescope": r"rescope|split_and_reimplement|split and reimplement|scope too large",
    "ready_packet": r"READY_PACKET|merge-ready|merge ready|qa-passed-awaiting-ci",
}

RESCUE_HEAD_BINDING_PATTERN = (
    r"(?:\bhead_or_plan_sha\b\s*[:=]\s*|\bexact[ -]+head\b\s*[:=]?\s*|"
    r"\bbound[ -]+to\b\s*)(?P<head>[0-9a-f]{7,40})(?![0-9a-f])"
)


def sh(cmd: list[str]) -> str:
    env = os.environ.copy()
    env["PATH"] = f"{CANONICAL_PATH}:{env.get('PATH', '')}"
    if cmd and cmd[0] == "gh":
        gh = shutil.which("gh", path=env["PATH"])
        if not gh:
            raise FileNotFoundError("gh not found on canonical PATH")
        cmd = [gh, *cmd[1:]]
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.PIPE, env=env)
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(exc.stderr or exc.output or "")
        raise


def gh_json(args: list[str]) -> Any:
    return json.loads(sh(["gh", *args]))


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def hours_between(start: str | None, end: str | None) -> float | None:
    s = parse_time(start)
    e = parse_time(end)
    if not s or not e:
        return None
    return round((e - s).total_seconds() / 3600.0, 2)


def first_issue(pr: dict[str, Any], explicit: str | None) -> str | None:
    if explicit:
        return explicit
    haystack = "\n".join([pr.get("title") or "", pr.get("body") or ""])
    m = re.search(r"#(\d+)", haystack)
    return m.group(1) if m else None


def labels(pr: dict[str, Any]) -> list[str]:
    return [str(item.get("name")) for item in pr.get("labels") or [] if item.get("name")]


def comment_blob(pr: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in pr.get("comments") or []:
        parts.append(item.get("body") or "")
    for item in pr.get("reviews") or []:
        parts.append(item.get("body") or "")
    return "\n".join(parts)


def term_counts(blob: str) -> dict[str, int]:
    return {name: len(re.findall(pattern, blob, re.IGNORECASE)) for name, pattern in TERM_PATTERNS.items()}


def get_runs(branch: str) -> tuple[list[dict[str, Any]], str | None]:
    try:
        data = gh_json([
            "run",
            "list",
            "--repo",
            REPO,
            "--branch",
            branch,
            "--limit",
            "200",
            "--json",
            "databaseId,workflowName,conclusion,status,createdAt,startedAt,updatedAt,headSha,event,url,attempt",
        ])
        return data if isinstance(data, list) else [], None
    except Exception as exc:  # noqa: BLE001 - surface non-fatal collection failure in retro.
        return [], f"gh run list failed for branch {branch}: {exc}"


def classify_ci_run(run: dict[str, Any], final_head: str, merged_at: str = "") -> str:
    """Classify a historical run without claiming it was stale when it ran.

    ``final_head`` is the PR head observed after merge. A different run SHA only
    proves that the run belongs to an earlier head. It does not prove that a
    newer head existed when the run was queued or executing.
    """
    workflow = run.get("workflowName") or "unknown"
    conclusion = run.get("conclusion") or run.get("status") or "unknown"
    head = str(run.get("headSha") or "")
    is_final = bool(final_head and head == final_head)
    if workflow not in {"CI", "E2E Smoke Tests"}:
        return "non_ci_e2e"
    if conclusion == "skipped":
        return "skipped"
    if conclusion in {"success", "neutral"}:
        return "success_final_head" if is_final else "success_earlier_head"
    if conclusion == "failure":
        return "workflow_failure_final_head" if is_final else "workflow_failure_earlier_head"
    if conclusion in {"timed_out", "startup_failure"}:
        return "wall_budget_or_timeout_final_head" if is_final else "wall_budget_or_timeout_earlier_head"
    if conclusion in {"cancelled", "canceled"}:
        if merged_at and run.get("updatedAt"):
            completed = parse_time(str(run.get("updatedAt")))
            merged = parse_time(merged_at)
            if completed and merged and completed >= merged:
                return "post_merge_cancel"
        return "cancelled_final_head" if is_final else "cancelled_earlier_head"
    if conclusion in {"in_progress", "queued", "waiting", "requested", "pending"}:
        return "non_terminal_final_head" if is_final else "non_terminal_earlier_head"
    return "unknown_final_head" if is_final else "unknown_earlier_head"


def run_summary(runs: list[dict[str, Any]], final_head: str, merged_at: str = "") -> dict[str, Any]:
    by_workflow: Counter[str] = Counter()
    bad_by_class: Counter[str] = Counter()
    real_ci_e2e = 0
    failed_or_cancelled = 0
    skipped_ci_e2e = 0
    workflow_failures = 0
    cancellations = 0
    post_merge_cancellations = 0
    timeouts_or_startup_failures = 0
    bad_final_head = 0
    bad_earlier_head = 0
    bad_examples: list[dict[str, Any]] = []
    for run in runs:
        if run.get("event") not in {"pull_request", "workflow_dispatch"}:
            continue
        workflow = run.get("workflowName") or "unknown"
        conclusion = run.get("conclusion") or run.get("status") or "unknown"
        key = f"{workflow}|{conclusion}"
        by_workflow[key] += 1
        if workflow in {"CI", "E2E Smoke Tests"}:
            if conclusion == "skipped":
                skipped_ci_e2e += 1
            else:
                real_ci_e2e += 1
            if conclusion in {"failure", "cancelled", "canceled", "timed_out", "startup_failure"}:
                classification = classify_ci_run(run, final_head, merged_at)
                if classification == "post_merge_cancel":
                    post_merge_cancellations += 1
                    continue
                failed_or_cancelled += 1
                if conclusion == "failure":
                    workflow_failures += 1
                elif conclusion in {"cancelled", "canceled"}:
                    cancellations += 1
                else:
                    timeouts_or_startup_failures += 1
                bad_by_class[classification] += 1
                if classification.endswith("_final_head"):
                    bad_final_head += 1
                else:
                    bad_earlier_head += 1
                if len(bad_examples) < 30:
                    head = str(run.get("headSha") or "")
                    bad_examples.append({
                        "databaseId": run.get("databaseId"),
                        "workflowName": workflow,
                        "conclusion": conclusion,
                        "createdAt": run.get("createdAt"),
                        "startedAt": run.get("startedAt"),
                        "updatedAt": run.get("updatedAt"),
                        "headSha": head,
                        "headShort": head[:8],
                        "finalHead": bool(final_head and head == final_head),
                        "classification": classification,
                        "url": run.get("url"),
                    })
    return {
        "listed_runs": len(runs),
        "real_ci_e2e_non_skipped": real_ci_e2e,
        "failed_or_cancelled_ci_e2e": failed_or_cancelled,
        "workflow_failure_ci_e2e": workflow_failures,
        "cancelled_ci_e2e": cancellations,
        "post_merge_cancelled_ci_e2e": post_merge_cancellations,
        "timed_out_or_startup_failure_ci_e2e": timeouts_or_startup_failures,
        "skipped_ci_e2e": skipped_ci_e2e,
        "bad_final_head_ci_e2e": bad_final_head,
        "bad_earlier_head_ci_e2e": bad_earlier_head,
        "bad_ci_e2e_by_class": dict(sorted(bad_by_class.items())),
        "bad_ci_e2e_examples": bad_examples,
        "by_workflow_conclusion": dict(sorted(by_workflow.items())),
    }


def pm_ops_events(pr_number: int) -> tuple[list[dict[str, Any]], str | None]:
    """Read durable PM transition evidence without mutating the PM ledger."""

    if not PM_OPS_DB.exists():
        return [], f"PM ops DB missing: {PM_OPS_DB}"
    try:
        con = sqlite3.connect(f"file:{PM_OPS_DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT id, ts, source, event_type, target_type, target_id,
                   pr, issue, slot, head_sha, payload_json
            FROM events
            WHERE pr=?
            ORDER BY ts ASC, id ASC
            """,
            (pr_number,),
        ).fetchall()
        con.close()
    except Exception as exc:  # noqa: BLE001 - retros remain available in degraded mode.
        return [], f"PM ops event read failed for PR #{pr_number}: {exc}"

    events: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
        except json.JSONDecodeError:
            item["payload"] = {}
        events.append(item)
    return events, None


def _event_time(event: dict[str, Any]) -> datetime | None:
    return parse_time(str(event.get("ts") or ""))


def _run_time(run: dict[str, Any]) -> datetime | None:
    return parse_time(str(run.get("createdAt") or ""))


def validated_review_rescue_citation(pr: dict[str, Any]) -> dict[str, str | bool]:
    """Recognize an explicit, validated Kimi3/Fable review-cap rescue citation.

    The citation is deliberately limited to the immutable PR body.  A rescue
    is valid only when one of the live, auditable terminal/evidence shapes is
    present in the same body block as the model and review-cap rescue marker.
    Historical rescue citations may bind to the current head or to one unique
    commit ancestor listed on the PR; arbitrary, missing, or ambiguous tokens
    fail closed.
    """

    body = pr.get("body") if isinstance(pr, dict) else None
    if not isinstance(body, str) or not body.strip():
        return {"valid": False, "source": "none", "terminal": ""}

    paragraphs = re.split(r"\n\s*\n", body)
    regions = list(paragraphs)
    for index, paragraph in enumerate(paragraphs[:-1]):
        if re.search(r"(?im)^\s*#{1,6}[^\n]*cap\s*\+\s*rescue", paragraph):
            regions.append(f"{paragraph}\n{paragraphs[index + 1]}")

    for region in regions:
        lines = [line for line in region.splitlines() if line.strip()]

        def nearby_window(*patterns: str) -> str | None:
            for start in range(len(lines)):
                window = "\n".join(lines[start : start + 2])
                if all(re.search(pattern, window, re.IGNORECASE) for pattern in patterns):
                    return window
            return None

        def nearby(*patterns: str) -> bool:
            return nearby_window(*patterns) is not None

        def resolves_head_binding(window: str) -> bool:
            tokens = [
                match.group("head").lower()
                for match in re.finditer(RESCUE_HEAD_BINDING_PATTERN, window, re.IGNORECASE)
            ]
            if len(tokens) != 1:
                return False
            head = str(pr.get("headRefOid") or "").strip().lower()
            known_heads = {
                head,
                *(
                    str(commit.get("oid") or "").strip().lower()
                    if isinstance(commit, dict)
                    else str(commit or "").strip().lower()
                    for commit in (pr.get("commits") or [])
                ),
            }
            known_heads = {
                candidate
                for candidate in known_heads
                if re.fullmatch(r"[0-9a-f]{40}", candidate)
            }
            return len(
                {candidate for candidate in known_heads if candidate.startswith(tokens[0])}
            ) == 1

        if not nearby(r"\b(?:kimi3|fable)\b", r"\brescue\b"):
            continue
        if not nearby(
            r"(?:code[ -]+review(?:[ -]+reached)?[ -]+cap|review[ -]+cap|cap\s*\+\s*rescue)",
            r"\brescue\b",
        ):
            continue
        if re.search(r"\bterminal\s*[:=]\s*FAILED\b", region, re.IGNORECASE):
            continue
        if re.search(r"\b(?:rescue\b.{0,120}\bFAILED\b|\bFAILED\b.{0,120}\brescue)\b", region, re.IGNORECASE):
            continue

        terminals = re.findall(r"\b(PATCH_READY|NO_PATCH_REQUIRED)\b", region, re.IGNORECASE)
        if not terminals or len({terminal.upper() for terminal in terminals}) != 1:
            continue
        terminal = terminals[0].upper()

        if terminal == "NO_PATCH_REQUIRED":
            ledger = re.search(
                r"\bledger\b\s*[:=]?\s*([A-Za-z0-9][A-Za-z0-9._/-]*)",
                region,
                re.IGNORECASE,
            )
            ledger_value = ledger.group(1).lower() if ledger else ""
            ledger_valid = ledger_value not in {
                "",
                "none",
                "null",
                "missing",
                "unknown",
                "n/a",
                "skip_further_review",
            }
            skip_further_review = re.search(
                r"\bskip_further_review\b\s*[:=]\s*true\b",
                region,
                re.IGNORECASE,
            )
            validated_window = nearby_window(
                r"\bvalidated\b\s+(?:\*\*\s*)?PASS\b",
                r"\bexact[ -]+head\b\s*[:=]?\s*[0-9a-f]{7,40}(?![0-9a-f])",
                r"\bNO_PATCH_REQUIRED\b",
            )
            ledger_window = nearby_window(
                r"\bNO_PATCH_REQUIRED\b",
                r"\bledger\b\s*[:=]?\s*[A-Za-z0-9][A-Za-z0-9._/-]*",
                r"\bskip_further_review\b\s*[:=]\s*true\b",
                RESCUE_HEAD_BINDING_PATTERN,
            )
            if validated_window and resolves_head_binding(validated_window):
                return {"valid": True, "source": "pr_body", "terminal": terminal}
            if ledger_valid and skip_further_review and ledger_window and resolves_head_binding(ledger_window):
                return {"valid": True, "source": "pr_body", "terminal": terminal}
            continue

        validated = re.search(r"\(\s*VALIDATED\s*\)", region, re.IGNORECASE)
        authorization_pass = re.search(
            r"\bRESCUE_AUTHORIZATION\b\s*:\s*PASS\b",
            region,
            re.IGNORECASE,
        )
        patch_window = nearby_window(
            r"\bPATCH_READY\b",
            r"\(\s*VALIDATED\s*\)",
            r"\bRESCUE_AUTHORIZATION\b\s*:\s*PASS\b",
            RESCUE_HEAD_BINDING_PATTERN,
        )
        if validated and authorization_pass and patch_window and resolves_head_binding(patch_window):
            return {"valid": True, "source": "pr_body", "terminal": terminal}

    return {"valid": False, "source": "none", "terminal": ""}


def healthy_review_rescue_body_citation(transition_facts: dict[str, Any]) -> bool:
    """Return true only for a validated body citation without typed rescue events."""

    return bool(transition_facts.get("review_rescue_body_citation_valid")) and int(
        transition_facts.get("review_rescue_or_rescope_typed_events") or 0
    ) == 0


def transition_evidence(
    pr: dict[str, Any],
    runs: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Derive missed transitions from event ordering, never comment keywords."""

    ci_runs = sorted(
        [
            run
            for run in runs
            if run.get("workflowName") in {"CI", "E2E Smoke Tests"}
            and run.get("event") in {"pull_request", "workflow_dispatch"}
            and (run.get("conclusion") or run.get("status")) != "skipped"
            and _run_time(run)
        ],
        key=lambda run: _run_time(run) or datetime.min.replace(tzinfo=timezone.utc),
    )
    by_type: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_type.setdefault(str(event.get("event_type") or ""), []).append(event)

    capture_required = by_type.get("capture_local_required", [])
    capture_passes = by_type.get("capture_local_pass", [])
    ci_requests = by_type.get("ci_requested", [])
    preflight_passes = by_type.get("ci_local_preflight_pass", [])
    # Dependency blocks return before the generic block_pr event is recorded.
    # dependency_watch_started is the authoritative durable event emitted by
    # pm-transition for this path.
    dependency_blocks = by_type.get("dependency_watch_started", [])

    capture_before_ci_missed = False
    for required in capture_required:
        head = str(required.get("head_sha") or "")
        required_at = _event_time(required)
        if not head or not required_at:
            continue
        requested = [
            event
            for event in ci_requests
            if str(event.get("head_sha") or "") == head
            and str((event.get("payload") or {}).get("mode") or "initial") == "initial"
            and _event_time(event)
            and _event_time(event) >= required_at
        ]
        for request in requested:
            request_at = _event_time(request)
            passed_before = any(
                str(event.get("head_sha") or "") == head
                and _event_time(event)
                and request_at
                and required_at <= _event_time(event) <= request_at
                for event in capture_passes
            )
            if not passed_before:
                capture_before_ci_missed = True
                break
        if capture_before_ci_missed:
            break

    local_preflight_before_rerun_missed = False
    for request in ci_requests:
        payload = request.get("payload") or {}
        if str(payload.get("mode") or "initial") != "rerun":
            continue
        head = str(request.get("head_sha") or "")
        request_at = _event_time(request)
        failed_run = str(payload.get("failed_run") or "")
        passed_before = any(
            str(event.get("head_sha") or "") == head
            and _event_time(event)
            and request_at
            and _event_time(event) <= request_at
            and (
                not failed_run
                or str((event.get("payload") or {}).get("failed_run") or "") == failed_run
            )
            for event in preflight_passes
        )
        if not passed_before:
            local_preflight_before_rerun_missed = True
            break

    moving_head_ci_missed = False
    initial_requests = [
        event
        for event in ci_requests
        if str((event.get("payload") or {}).get("mode") or "initial") == "initial"
    ]
    for request in initial_requests:
        head = str(request.get("head_sha") or "")
        request_at = _event_time(request)
        if not head or not request_at:
            continue
        cancelled = any(
            str(run.get("headSha") or "") == head
            and str(run.get("conclusion") or "").lower() in {"cancelled", "canceled"}
            and _run_time(run)
            and _run_time(run) >= request_at
            for run in ci_runs
        )
        newer_head_requested = any(
            str(other.get("head_sha") or "") not in {"", head}
            and _event_time(other)
            and _event_time(other) > request_at
            for other in initial_requests
        )
        if cancelled and newer_head_requested:
            moving_head_ci_missed = True
            break

    dependency_slot_release_events = [
        event
        for event in by_type.get("slot_released", [])
        if "dependency" in str((event.get("payload") or {}).get("reason") or "")
    ]
    dependency_release_events = [
        event
        for event in dependency_blocks
        if str((event.get("payload") or {}).get("released_slots") or "")
        .strip()
        .lower()
        not in {"", "none", "null"}
    ]
    dependency_release_events.extend(dependency_slot_release_events)
    dependency_no_release_required_events = [
        event
        for event in by_type.get("stale_slot_label_ignored", [])
        if str((event.get("payload") or {}).get("reason") or "") == "block-pr:dependency"
        and str((event.get("payload") or {}).get("action") or "")
        == "label_removed_no_release"
        and str((event.get("payload") or {}).get("mop_status") or "")
        in {"free", "mismatch"}
    ]
    dependency_slot_release_missed = False
    for block in dependency_blocks:
        payload = block.get("payload") or {}
        released_slots = str(payload.get("released_slots") or "").strip().lower()
        if released_slots not in {"", "none", "null"}:
            continue
        block_at = _event_time(block)
        block_payload = block.get("payload") or {}
        release_near_block = any(
            _event_time(event)
            and block_at
            and abs((_event_time(event) - block_at).total_seconds()) <= 300
            for event in dependency_slot_release_events
        )
        no_release_required = any(
            _event_time(event)
            and block_at
            and 0 <= (block_at - _event_time(event)).total_seconds() <= 60
            and (
                not block.get("slot")
                or not event.get("slot")
                or str(event["slot"]) == str(block["slot"])
            )
            and (
                not str(block_payload.get("branch") or "")
                or not str((event.get("payload") or {}).get("branch") or "")
                or str((event.get("payload") or {}).get("branch"))
                == str(block_payload.get("branch"))
            )
            for event in dependency_no_release_required_events
        )
        if not release_near_block and not no_release_required:
            dependency_slot_release_missed = True
            break

    merged_at = parse_time(pr.get("mergedAt"))
    cleanup_starts = [event for event in by_type.get("cleanup_started", []) if _event_time(event)]
    first_cleanup_start = min((_event_time(event) for event in cleanup_starts), default=None)
    cleanup_start_delay_seconds = (
        int((first_cleanup_start - merged_at).total_seconds())
        if merged_at and first_cleanup_start
        else None
    )
    pending_cleanup = bool(
        {"pm-state:merged-cleanup-pending", "pm-cleanup:needed"}.intersection(labels(pr))
    )
    cleanup_closeout_missed = bool(
        pending_cleanup
        and merged_at
        and (
            cleanup_start_delay_seconds > CLEANUP_START_SLA_SECONDS
            if cleanup_start_delay_seconds is not None
            else (datetime.now(timezone.utc) - merged_at).total_seconds() > CLEANUP_START_SLA_SECONDS
        )
    )
    review_rescue_body_citation = validated_review_rescue_citation(pr)
    review_rescue_or_rescope_typed_events = sum(
        len(by_type.get(event_type, []))
        for event_type in (
            "cto_rescue_escalated",
            "rescope_decide",
            "cto_disposition_consumed",
            # The review-cap dispatch producer emits exactly one typed event
            # per frozen (pr|issue, head, kind) tuple (receipt + dedupe-key),
            # releases/parks the owning slot, and routes the rescue off-slot.
            # Counting it closes the #7314/#7301 false "missed transition"
            # after a capped review terminal was already dispatched.
            "review_cap_dispatched",
        )
    )
    review_rescue_body_citation_valid = bool(review_rescue_body_citation["valid"])
    review_rescue_or_rescope_event_count = review_rescue_or_rescope_typed_events + int(
        review_rescue_body_citation_valid
    )
    review_rescue_body_recovery = review_rescue_body_citation_valid and (
        review_rescue_or_rescope_typed_events == 0
    )

    return {
        "event_count": len(events),
        "capture_before_ci_missed": capture_before_ci_missed,
        "local_preflight_before_rerun_missed": local_preflight_before_rerun_missed,
        "moving_head_ci_missed": moving_head_ci_missed,
        "dependency_slot_release_missed": dependency_slot_release_missed,
        "dependency_block_events": len(dependency_blocks),
        "dependency_release_events": len(dependency_release_events),
        "dependency_no_release_required_events": len(
            dependency_no_release_required_events
        ),
        "review_rescue_or_rescope_events": review_rescue_or_rescope_event_count,
        "review_rescue_or_rescope_typed_events": review_rescue_or_rescope_typed_events,
        "review_rescue_body_citation_valid": review_rescue_body_citation_valid,
        "review_rescue_body_citation_source": str(review_rescue_body_citation["source"]),
        "review_rescue_body_citation_terminal": str(review_rescue_body_citation["terminal"]),
        "review_rescue_body_recovery": review_rescue_body_recovery,
        "cleanup_closeout_missed": cleanup_closeout_missed,
        "cleanup_start_delay_seconds": cleanup_start_delay_seconds,
        "cleanup_start_sla_seconds": CLEANUP_START_SLA_SECONDS,
        "cleanup_started_at": first_cleanup_start.isoformat() if first_cleanup_start else None,
        "cleanup_pending_at_retro": pending_cleanup,
        "event_types": dict(sorted(Counter(str(event.get("event_type") or "") for event in events).items())),
    }


def review_budget_summary(pr_number: int, issue: str | None, head: str | None) -> tuple[dict[str, Any], str | None]:
    """Use the same review-loop circuit breaker as pm-transition.

    Retro root causes can be noisy because comments and reconstructed evidence
    overcount broad "review churn". The missed PM transition should only claim
    rescue/split was required when the authoritative same-layer/same-class
    budget gate says so.
    """

    if (
        not PM_OPERATOR_BIN.is_file()
        or PM_OPERATOR_BIN.is_symlink()
        or not os.access(PM_OPERATOR_BIN, os.X_OK)
    ):
        return {
            "decision": "unknown",
            "status": "degraded",
            "reason": "pm_operator_missing_or_not_executable",
        }, f"PM Operator binary unavailable: {PM_OPERATOR_BIN}"
    cmd = [str(PM_OPERATOR_BIN), "review-budget", "--pr", str(pr_number), "--json"]
    if issue:
        cmd.extend(["--issue", str(issue)])
    if head:
        cmd.extend(["--head", str(head)])
    env = os.environ.copy()
    env["PATH"] = f"{CANONICAL_PATH}:{env.get('PATH', '')}"
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=25, env=env)
    except Exception as exc:  # noqa: BLE001 - retro should remain best-effort.
        return {
            "decision": "unknown",
            "status": "degraded",
            "reason": "review_budget_failed",
        }, f"review budget failed for PR #{pr_number}: {exc}"
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {
            "decision": "unknown",
            "status": "degraded",
            "reason": "review_budget_parse_error",
            "stdout": (proc.stdout or "")[-1000:],
            "stderr": (proc.stderr or "")[-1000:],
        }, f"review budget parse error for PR #{pr_number}"
    if not isinstance(data, dict) or not data:
        return {
            "decision": "unknown",
            "status": "degraded",
            "reason": "review_budget_empty",
            "returncode": proc.returncode,
        }, f"review budget empty for PR #{pr_number}"
    data.setdefault("status", "ok" if proc.returncode in (0, 1) else "degraded")
    if proc.returncode not in (0, 1):
        return data, f"review budget command exited {proc.returncode} for PR #{pr_number}"
    return data, None


def review_summary(pr: dict[str, Any]) -> dict[str, Any]:
    states = Counter(str(item.get("state") or "UNKNOWN") for item in pr.get("reviews") or [])
    return {
        "comments": len(pr.get("comments") or []),
        "reviews": dict(sorted(states.items())),
    }


def safe_json(text: str) -> Any:
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return json.loads(cleaned)


def normalize_review_type(value: str | None) -> str:
    raw = (value or "").lower()
    if "plan" in raw:
        return "plan"
    if "code" in raw:
        return "code"
    if "qa" in raw:
        return "qa"
    if "arch" in raw or "architecture" in raw:
        return "arch"
    return "unknown"


def infer_review_type(text: str) -> str:
    lowered = text.lower()
    if re.search(r"plan[-_ ]?review|implementation plan|phase 2|review-type[: =]+plan", lowered):
        return "plan"
    if re.search(r"code[-_ ]?review|codex review|phase 4|review-type[: =]+code", lowered):
        return "code"
    if re.search(r"qa[-_ ]?review|qa tester|review-type[: =]+qa", lowered):
        return "qa"
    if re.search(r"arch[-_ ]?review|architecture review|review-type[: =]+arch", lowered):
        return "arch"
    return "unknown"


def marker_field(text: str, pattern: str, default: str = "") -> str:
    match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else default


def event_timestamp(event: dict[str, Any]) -> str:
    for key in ("timestamp", "created_at", "createdAt", "time", "ts"):
        value = event.get(key)
        if value:
            return str(value)
    return ""


def event_before_created(event_ts: str, created_at: str) -> bool:
    if not event_ts or not created_at:
        return False
    created = parse_time(created_at)
    if not created:
        return False
    parsed = parse_time(event_ts)
    if parsed:
        return parsed < created
    try:
        numeric = float(event_ts)
    except ValueError:
        return False
    # MoP/Slack-style timestamps are seconds since epoch, often with decimals.
    if numeric > 10_000_000_000:
        numeric = numeric / 1000.0
    return datetime.fromtimestamp(numeric, tz=timezone.utc) < created


def event_payload_text(event: dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = safe_json(payload)
        except Exception:
            return payload
    try:
        return json.dumps(payload, ensure_ascii=True, default=str)
    except TypeError:
        return str(payload)


def fetch_mop_events(slot: int) -> tuple[list[dict[str, Any]], str | None]:
    url = f"{MOP_BASE_URL}/events?slot={slot}&limit=500&type=PostToolUse"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            data = safe_json(response.read().decode("utf-8", errors="replace"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return [], f"MoP event fetch failed for slot {slot}: {exc}"
    events = data.get("events") if isinstance(data, dict) else data
    if not isinstance(events, list):
        return [], f"MoP event fetch returned unexpected shape for slot {slot}"
    return [event for event in events if isinstance(event, dict)], None


def companion_review_summary(pr: dict[str, Any], issue: str | None) -> tuple[dict[str, Any], list[str]]:
    """Collect slot-side and PM-side plan/code/qa/arch review rounds into the delivery retro."""
    pr_number = str(pr.get("number") or "")
    branch = str(pr.get("headRefName") or "")
    created_at = str(pr.get("createdAt") or "")
    warnings: list[str] = []
    events: list[dict[str, Any]] = []
    markers: list[dict[str, Any]] = []

    for slot in range(1, 5):
        slot_events, warning = fetch_mop_events(slot)
        if warning:
            warnings.append(warning)
        for event in slot_events:
            ts = event_timestamp(event)
            if event_before_created(ts, created_at):
                continue
            payload_text = event_payload_text(event)
            haystack = f"{payload_text}\n{event}"
            if pr_number and pr_number not in haystack and (not branch or branch not in haystack):
                continue
            if not re.search(r"codex|claude.*review|plan review|code review|qa review|arch review|verdict", haystack, re.I):
                continue
            review_type = infer_review_type(haystack)
            if review_type not in {"plan", "code", "qa", "arch"}:
                continue
            events.append({
                "slot": str(slot),
                "timestamp": ts,
                "review_type": review_type,
                "tool": str(event.get("tool_name") or ""),
                "mop_event_id": event.get("id"),
            })

    if COMPANION_MARKER_DIR.is_dir():
        for marker in sorted(COMPANION_MARKER_DIR.glob("*.md")):
            try:
                text = marker.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                warnings.append(f"Companion marker read failed for {marker}: {exc}")
                continue
            if pr_number and not re.search(rf"\bPR #?{re.escape(pr_number)}\b|#{re.escape(pr_number)}\b", text):
                if not branch or branch not in text:
                    continue
            review_type = normalize_review_type(
                marker_field(text, r"review[-_ ]?type[: =]+(plan|code|qa|arch)", "")
            )
            if review_type == "unknown":
                review_type = infer_review_type(text)
            verdict = marker_field(
                text,
                r"(?:VERDICT|COMPANION_VERDICT|FINAL_REVIEWER_VERDICT)[: ]+(APPROVE|REVISE|REJECT|REQUEST_CHANGES|NEEDS_REVISION|NEEDS_DEEPER_INVESTIGATION|PASS|BLOCK)",
                "UNKNOWN",
            ).upper()
            round_label = marker_field(text, r"\b((?:Round|R)[ -]?[0-9]+)\b", "R?")
            markers.append({
                "path": str(marker),
                "review_type": review_type,
                "verdict": verdict,
                "round": round_label.replace(" ", ""),
                "mtime": marker.stat().st_mtime,
            })
    else:
        warnings.append(f"Companion marker dir missing: {COMPANION_MARKER_DIR}")

    seen_marker_paths = {item["path"] for item in markers}
    for pattern in PM_REVIEW_MARKER_GLOBS:
        if "{issue}" in pattern and not issue:
            continue
        for marker_name in sorted(glob.glob(pattern.format(pr=pr_number, issue=issue or ""))):
            marker = Path(marker_name)
            marker_path = str(marker)
            if marker_path in seen_marker_paths:
                continue
            try:
                text = marker.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                warnings.append(f"PM review marker read failed for {marker}: {exc}")
                continue
            if marker.suffix == ".patch":
                review_type = "plan"
                verdict = "PATCH"
                round_label = "R?"
            else:
                review_type = infer_review_type(f"{marker.name}\n{text}")
                verdict = marker_field(
                    text,
                    r"(?:PM_CLAUDE_REVIEW|PM_OPUS_REVIEW|PM_CLAUDE_PLAN_REVIEW|PM_OPUS_PLAN_REVIEW|VERDICT)[: ]+(PASS|BLOCK|APPROVE|REVISE|REJECT|REQUEST_CHANGES|NEEDS_REVISION|NEEDS_DEEPER_INVESTIGATION)",
                    "UNKNOWN",
                ).upper()
                round_label = marker_field(text, r"\b((?:Round|R)[ -]?[0-9]+)\b", "R?")
            markers.append({
                "path": marker_path,
                "review_type": review_type,
                "verdict": verdict,
                "round": round_label.replace(" ", ""),
                "mtime": marker.stat().st_mtime,
            })
            seen_marker_paths.add(marker_path)

    github_mentions: list[dict[str, Any]] = []
    github_by_type: Counter[str] = Counter()
    for source_name, items in (("comment", pr.get("comments") or []), ("review", pr.get("reviews") or [])):
        for index, item in enumerate(items):
            body = item.get("body") or ""
            if not re.search(r"PM[- ]?(?:Claude|Opus)|Codex|REQUEST_CHANGES|NEEDS_REVISION|plan[-_ ]?review|code[-_ ]?review|qa[-_ ]?review|arch[-_ ]?review", body, re.I):
                continue
            review_type = infer_review_type(body)
            if review_type == "unknown" and source_name == "review":
                review_type = "code"
            # A GitHub artifact is one review observation. Never infer a count
            # from arbitrary prose such as test names (R2-411), HTTP status
            # codes, issue numbers, or summarized round ranges.
            github_by_type[review_type] += 1
            if len(github_mentions) < 25:
                github_mentions.append({
                    "source": source_name,
                    "index": index,
                    "review_type": review_type,
                    "observations": 1,
                    # Compatibility for existing report consumers. This is an
                    # observation count, not a prose-derived round estimate.
                    "estimated_rounds": 1,
                    "state": item.get("state"),
                    "author": ((item.get("author") or {}).get("login") if isinstance(item.get("author"), dict) else None),
                })

    marker_by_type = Counter(item["review_type"] for item in markers)
    by_type: Counter[str] = Counter()
    source_by_type: dict[str, str] = {}
    for review_type in sorted(set(marker_by_type) | set(github_by_type)):
        if marker_by_type.get(review_type, 0):
            by_type[review_type] = marker_by_type[review_type]
            source_by_type[review_type] = "durable_marker"
        else:
            by_type[review_type] = github_by_type[review_type]
            source_by_type[review_type] = "github_observation"
    by_verdict = Counter(item["verdict"] for item in markers)
    by_slot = Counter(item["slot"] for item in events)
    plan_code_rounds = by_type.get("plan", 0) + by_type.get("code", 0)
    direct_rounds = len(markers)
    github_estimated_rounds = sum(github_by_type.values())
    total_rounds = sum(by_type.values())
    source_kinds = set(source_by_type.values())
    if source_kinds == {"durable_marker"}:
        source_quality = "durable_markers"
    elif source_kinds == {"github_observation"}:
        source_quality = "github_observations"
    elif source_kinds:
        source_quality = "mixed_marker_and_github_observations"
    else:
        source_quality = "none"
    return {
        "schema": "heydonna_pr_review_rounds",
        "measurement": "recorded_review_evidence",
        "round_source_quality": source_quality,
        "events_count": len(events),
        "mop_events_are_telemetry_only": True,
        "markers_count": len(markers),
        "direct_rounds": direct_rounds,
        "github_estimated_rounds": github_estimated_rounds,
        "total_rounds": total_rounds,
        "plan_rounds": by_type.get("plan", 0),
        "code_rounds": by_type.get("code", 0),
        "qa_rounds": by_type.get("qa", 0),
        "arch_rounds": by_type.get("arch", 0),
        "unknown_rounds": by_type.get("unknown", 0),
        "plan_code_rounds": plan_code_rounds,
        "direct_by_review_type": dict(sorted(marker_by_type.items())),
        "github_estimated_by_review_type": dict(sorted(github_by_type.items())),
        "by_review_type": dict(sorted(by_type.items())),
        "decision_source_by_review_type": dict(sorted(source_by_type.items())),
        "by_verdict": dict(sorted(by_verdict.items())),
        "by_slot": dict(sorted(by_slot.items())),
        "events": events[:25],
        "markers": markers[:25],
        "github_mentions": github_mentions[:25],
    }, warnings


def missed_pm_transitions(
    root_causes: list[str],
    counts: dict[str, int],
    runs: dict[str, Any],
    review_budget: dict[str, Any],
    transition_facts: dict[str, Any],
) -> list[str]:
    transitions: list[str] = []
    bad_classes = runs.get("bad_ci_e2e_by_class") or {}
    if transition_facts.get("capture_before_ci_missed"):
        transitions.append("capture-before-ci")
    if transition_facts.get("local_preflight_before_rerun_missed"):
        transitions.append("local-failed-suite-preflight-before-ci-rerun")
    if any(
        key in {"workflow_failure_final_head", "wall_budget_or_timeout_final_head"}
        for key, value in bad_classes.items()
        if value
    ):
        transitions.append("classify-current-head-ci-before-retry")
    if transition_facts.get("moving_head_ci_missed"):
        transitions.append("stop-ci-reruns-on-moving-head")
    if transition_facts.get("dependency_slot_release_missed"):
        transitions.append("release-slot-until-dependency-merge")
    if (
        review_budget.get("decision") == "rescue_required"
        and int(transition_facts.get("review_rescue_or_rescope_events") or 0) == 0
        and not transition_facts.get("review_rescue_body_citation_valid")
    ):
        transitions.append("rescue-or-split-after-review-cap")
    if "cleanup_label_hygiene" in root_causes:
        transitions.append("cleanup-stale-status-labels")
    if "cleanup_closeout_miss" in root_causes:
        transitions.append("finish-cleanup-pr-closeout")
    return sorted(set(transitions))


def classify(
    pr: dict[str, Any],
    counts: dict[str, int],
    runs: dict[str, Any],
    review_rounds: dict[str, Any],
    review_budget: dict[str, Any],
    transition_facts: dict[str, Any],
) -> tuple[str, list[str], list[str], list[str]]:
    labs = labels(pr)
    root_causes: list[str] = []
    followups: list[str] = []
    bad_classes = runs.get("bad_ci_e2e_by_class") or {}
    workflow_or_budget_failures = sum(
        int(value or 0)
        for key, value in bad_classes.items()
        if key.startswith("workflow_failure_") or key.startswith("wall_budget_or_timeout_")
    )
    proven_moving_head_cancels = (
        int(bad_classes.get("cancelled_earlier_head") or 0)
        if transition_facts.get("moving_head_ci_missed")
        else 0
    )
    changed = int(pr.get("changedFiles") or 0)
    churn = int(pr.get("additions") or 0) + int(pr.get("deletions") or 0)
    if (
        runs["real_ci_e2e_non_skipped"] >= 8
        or workflow_or_budget_failures >= 2
        or proven_moving_head_cancels >= 2
    ):
        root_causes.append("ci_churn")
        followups.append("Require local failed-suite preflight and root-cause classification before CI rerun.")
    if transition_facts.get("moving_head_ci_missed") and int(bad_classes.get("cancelled_earlier_head") or 0) >= 2:
        root_causes.append("superseded_head_ci_churn")
        followups.append("Avoid label-gated CI while the head is still moving; proven superseded cancellations are CI waste, but earlier-head failures are not.")
    if transition_facts.get("local_preflight_before_rerun_missed"):
        root_causes.append("local_preflight_missing")
        followups.append("Do not rerun CI after a genuine failure until the affected local suite/preflight passes on the same head. Stale/superseded cancellations alone do not require local preflight.")
    if any(
        key.startswith("wall_budget_or_timeout_") and int(value or 0) > 0
        for key, value in bad_classes.items()
    ):
        root_causes.append("wall_budget_or_timeout")
        followups.append("Treat wall-budget/cancel as a product/test hang until local repro or fresh capture proves otherwise.")
    if counts["capture_cache"] > 0:
        root_causes.append("capture_or_llm_proxy_cache")
        if transition_facts.get("capture_before_ci_missed"):
            followups.append("For prompt/proofread/SC/formatting changes, refresh capture on current head before label-gated CI.")
    if counts["dependency"] > 0:
        root_causes.append("dependency_wait")
        followups.append("Name the blocking PR/issue and release the slot while a dependency is genuinely pending.")
    if counts["review_rework"] >= 8:
        root_causes.append("review_rework_loop")
        followups.append("Review churn was high. Use the review-budget gate to distinguish true same-class loops from normal multi-layer review.")
    if int(review_rounds.get("plan_code_rounds") or 0) >= 4:
        root_causes.append("broad_review_churn")
        followups.append("Plan/code review volume was high. Treat this as a delivery smell, but use the review-budget gate before calling it a hard cap miss.")
    if (
        review_budget.get("decision") == "rescue_required"
        and not healthy_review_rescue_body_citation(transition_facts)
    ):
        root_causes.append("review_cap_reached")
        followups.append("Review-budget circuit breaker fired: run PM Codex PR rescue or rescope before any ordinary rework assignment.")
    if counts["rescope"] > 0:
        root_causes.append("rescope_signal")
    if changed >= 12 or churn >= 1500:
        root_causes.append("large_scope")
        followups.append("Split future work earlier when changed files or review classes cross the circuit-breaker threshold.")
    if pr.get("state") == "MERGED" and any(label.startswith("status:") for label in labs):
        root_causes.append("cleanup_label_hygiene")
        followups.append("cleanup-pr must strip stale status:* labels from merged PRs and linked issues.")
    if transition_facts.get("cleanup_closeout_missed"):
        root_causes.append("cleanup_closeout_miss")
        followups.append("Start cleanup-pr within 15 minutes of merge and keep merged-cleanup-pending visible until closed-clean proof lands.")

    if not root_causes:
        root_causes.append("smooth_or_low_signal")

    unique_roots = sorted(set(root_causes))
    attribution = delay_attribution(unique_roots)
    verdict = attribution["verdict"]
    return verdict, unique_roots, sorted(set(followups)), missed_pm_transitions(
        unique_roots, counts, runs, review_budget, transition_facts
    )


def delay_attribution(root_causes: list[str]) -> dict[str, str]:
    """Separate the visible wait from the upstream process that created it."""
    roots = set(root_causes)
    review = bool(
        roots
        & {
            "review_rework_loop",
            "broad_review_churn",
            "review_cap_reached",
            "rescope_signal",
        }
    )
    ci = bool(roots & {"ci_churn", "wall_budget_or_timeout"})

    if "cleanup_closeout_miss" in roots:
        proximate = originating = "cleanup"
    else:
        if ci:
            proximate = "ci"
        elif "dependency_wait" in roots:
            proximate = "dependency"
        elif review:
            proximate = "review"
        elif "large_scope" in roots:
            proximate = "scope"
        else:
            proximate = "none"

        # Review/rework is the originating cause when it produced intermediate
        # heads and CI churn. Calling that delayed_by_ci hides the control-plane
        # intervention that would have prevented the extra runs.
        if review:
            originating = "review"
        elif "dependency_wait" in roots:
            originating = "dependency"
        elif ci:
            originating = "ci"
        elif "large_scope" in roots:
            originating = "scope"
        else:
            originating = "none"

    verdict_by_origin = {
        "cleanup": "delayed_by_cleanup",
        "review": "delayed_by_review",
        "dependency": "delayed_by_dependency",
        "ci": "delayed_by_ci",
        "scope": "scope_risk",
        "none": "smooth_or_low_signal",
    }
    return {
        "proximate_delay": proximate,
        "originating_cause": originating,
        "verdict": verdict_by_origin[originating],
    }


def md_escape(value: Any) -> str:
    text = str(value if value is not None else "")
    return text.replace("\n", " ").strip()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def durable_paths(pr_number: int) -> tuple[Path, Path]:
    return RETRO_STORE_DIR / f"pr-{pr_number}.md", RETRO_STORE_DIR / f"pr-{pr_number}.json"


def render_markdown(retro: dict[str, Any]) -> str:
    pr = retro["pr"]
    lines = [
        f"# PR #{pr['number']} Delivery Retro",
        "",
        f"retro_status: complete",
        f"retro_version: {VERSION}",
        f"generated_at: {retro['generated_at']}",
        f"issue: {retro.get('issue') or 'unknown'}",
        f"verdict: {retro['verdict']}",
        f"root_causes: {', '.join(retro['root_causes'])}",
        "",
        "## PR",
        f"- Title: {md_escape(pr['title'])}",
        f"- URL: {pr['url']}",
        f"- State: {pr['state']}",
        f"- Created: {pr.get('createdAt')}",
        f"- Merged: {pr.get('mergedAt')}",
        f"- Lead time hours: {retro.get('lead_time_hours')}",
        f"- Changed files: {pr.get('changedFiles')} (+{pr.get('additions')} / -{pr.get('deletions')})",
        f"- Labels at retro: {', '.join(retro['labels']) or 'none'}",
        "",
        "## CI and E2E",
        f"- Listed runs sampled: {retro['runs']['listed_runs']}",
        f"- Real CI/E2E non-skipped runs: {retro['runs']['real_ci_e2e_non_skipped']}",
        f"- Failed/cancelled CI/E2E runs: {retro['runs']['failed_or_cancelled_ci_e2e']}",
        f"- Workflow failures: {retro['runs']['workflow_failure_ci_e2e']}",
        f"- Cancelled runs: {retro['runs']['cancelled_ci_e2e']}",
        f"- Timed-out/startup-failure runs: {retro['runs']['timed_out_or_startup_failure_ci_e2e']}",
        f"- Bad final-head CI/E2E runs: {retro['runs']['bad_final_head_ci_e2e']}",
        f"- Bad earlier-head CI/E2E runs: {retro['runs']['bad_earlier_head_ci_e2e']} (not automatically stale or waste)",
        f"- Skipped CI/E2E rows: {retro['runs']['skipped_ci_e2e']}",
        f"- Bad CI/E2E classes: {json.dumps(retro['runs']['bad_ci_e2e_by_class'], sort_keys=True)}",
        "",
        "## Review and Comments",
        f"- PR comments: {retro['reviews']['comments']}",
        f"- Review states: {json.dumps(retro['reviews']['reviews'], sort_keys=True)}",
        f"- Signal counts: {json.dumps(retro['term_counts'], sort_keys=True)}",
        "",
        "## PM Transition Evidence",
        f"- Evidence: {json.dumps(retro.get('transition_evidence', {}), sort_keys=True)}",
        "",
        "## Recorded Review Evidence",
        f"- Total counted evidence: {retro['review_rounds']['total_rounds']}",
        f"- Plan evidence: {retro['review_rounds']['plan_rounds']}",
        f"- Code evidence: {retro['review_rounds']['code_rounds']}",
        f"- QA evidence: {retro['review_rounds']['qa_rounds']}",
        f"- Arch evidence: {retro['review_rounds']['arch_rounds']}",
        f"- Evidence quality: {retro['review_rounds']['round_source_quality']}",
        f"- MoP tool events (telemetry only): {retro['review_rounds']['events_count']}",
        f"- Marker verdicts: {json.dumps(retro['review_rounds']['by_verdict'], sort_keys=True)}",
        f"- Review budget decision: {retro.get('review_budget', {}).get('decision', 'unknown')}",
        f"- Review budget same-class hot: {json.dumps(retro.get('review_budget', {}).get('same_class_hot', []), sort_keys=True)}",
        "",
        "## Classification",
        f"- Verdict: {retro['verdict']}",
        f"- Proximate delay: {retro['delay_attribution']['proximate_delay']}",
        f"- Originating cause: {retro['delay_attribution']['originating_cause']}",
        f"- Root causes: {', '.join(retro['root_causes'])}",
        "",
        "## Follow-ups",
    ]
    if retro["followups"]:
        lines.extend([f"- {item}" for item in retro["followups"]])
    else:
        lines.append("- None.")
    lines.extend(["", "## Missed PM Transitions"])
    if retro.get("missed_pm_transitions"):
        lines.extend([f"- {item}" for item in retro["missed_pm_transitions"]])
    else:
        lines.append("- None.")
    if retro.get("warnings"):
        lines.extend(["", "## Warnings"])
        lines.extend([f"- {item}" for item in retro["warnings"]])
    lines.append("")
    return "\n".join(lines)


def write_outputs(retro: dict[str, Any], out_md: Path, out_json: Path) -> tuple[Path, Path]:
    pr_number = int(retro["pr"]["number"])
    durable_md, durable_json = durable_paths(pr_number)
    retro.setdefault("storage", {})
    retro["storage"].update({
        "durable_json": str(durable_json),
        "durable_md": str(durable_md),
        "tmp_json": str(out_json),
        "tmp_md": str(out_md),
    })

    json_text = json.dumps(retro, indent=2, sort_keys=True) + "\n"
    md_text = render_markdown(retro)

    atomic_write(out_json, json_text)
    atomic_write(out_md, md_text)
    if out_json.resolve() != durable_json.resolve():
        atomic_write(durable_json, json_text)
    if out_md.resolve() != durable_md.resolve():
        atomic_write(durable_md, md_text)
    return durable_md, durable_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--issue")
    parser.add_argument("--out", default="")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    out_md = Path(args.out or f"/tmp/pr-retro-{args.pr}.md")
    out_json = Path(args.json_out or f"/tmp/pr-retro-{args.pr}.json")

    pr = gh_json([
        "pr",
        "view",
        str(args.pr),
        "--repo",
        REPO,
        "--json",
        "number,title,state,createdAt,mergedAt,closedAt,labels,comments,reviews,additions,deletions,changedFiles,headRefName,headRefOid,commits,url,body,isDraft",
    ])
    if pr.get("state") != "MERGED":
        raise SystemExit(f"PR #{args.pr} is not merged; state={pr.get('state')}")

    issue = first_issue(pr, args.issue)
    blob = comment_blob(pr)
    counts = term_counts(blob)
    runs, run_warning = get_runs(pr.get("headRefName") or "")
    runs_summary = run_summary(
        runs,
        str(pr.get("headRefOid") or ""),
        str(pr.get("mergedAt") or ""),
    )
    review_rounds, review_warnings = companion_review_summary(pr, issue)
    review_budget, review_budget_warning = review_budget_summary(
        int(pr.get("number")),
        issue,
        str(pr.get("headRefOid") or ""),
    )
    ledger_events, ledger_warning = pm_ops_events(int(pr.get("number")))
    transition_facts = transition_evidence(pr, runs, ledger_events)
    verdict, root_causes, followups, missed_transitions = classify(
        pr,
        counts,
        runs_summary,
        review_rounds,
        review_budget,
        transition_facts,
    )
    attribution = delay_attribution(root_causes)

    retro = {
        "schema": "heydonna_pr_delivery_retro",
        "version": VERSION,
        "retro_done": True,
        "data_quality": os.environ.get("HEYDONNA_RETRO_DATA_QUALITY", "complete"),
        "source_kind": os.environ.get("HEYDONNA_RETRO_SOURCE_KIND", "live_collection"),
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "issue": issue,
        "pr": {
            "number": pr.get("number"),
            "title": pr.get("title"),
            "url": pr.get("url"),
            "state": pr.get("state"),
            "createdAt": pr.get("createdAt"),
            "mergedAt": pr.get("mergedAt"),
            "headRefName": pr.get("headRefName"),
            "headRefOid": pr.get("headRefOid"),
            "changedFiles": pr.get("changedFiles"),
            "additions": pr.get("additions"),
            "deletions": pr.get("deletions"),
        },
        "lead_time_hours": hours_between(pr.get("createdAt"), pr.get("mergedAt")),
        "labels": labels(pr),
        "runs": runs_summary,
        "reviews": review_summary(pr),
        "review_rounds": review_rounds,
        "review_budget": review_budget,
        "term_counts": counts,
        "transition_evidence": transition_facts,
        "verdict": verdict,
        "delay_attribution": attribution,
        "root_causes": root_causes,
        "followups": followups,
        "missed_pm_transitions": missed_transitions,
        "warnings": ([run_warning] if run_warning else [])
        + ([ledger_warning] if ledger_warning else [])
        + ([review_budget_warning] if review_budget_warning else [])
        + review_warnings,
    }

    durable_md, durable_json = write_outputs(retro, out_md, out_json)
    print(
        f"PR_RETRO_OK pr={args.pr} verdict={verdict} root_causes={','.join(root_causes)} "
        f"md={out_md} json={out_json} durable_md={durable_md} durable_json={durable_json} "
        f"data_quality={retro['data_quality']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
