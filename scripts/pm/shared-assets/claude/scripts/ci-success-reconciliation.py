#!/usr/bin/env python3
"""Durably surface and close terminal-green CI reconciliation work."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any


DEFAULT_REPO = "heydonna-app/heydonna-app"
DEFAULT_GUARD = (
    "/Users/rajiv/Downloads/projects/heydonna-app/"
    "scripts/ci/pre-merge-current-head-ci-guard.sh"
)
DEFAULT_PM_OPS = "/Users/rajiv/.claude/scripts/pm-ops.py"
# Terminal-green promotion uses only the direct exact-head CTO handoff below.
# An optional notify transport may post the already-bound handoff; it never
# mutates PR state. The empty default skips notification and the durable
# event/obligation handoff remains the delivery authority.
DEFAULT_MERGE_READY_ALERT = ""
CODEX_FOLLOWUP_KIND = "codex_followup_reconcile"
CI_RECONCILE_KIND = "ci_reconcile"
MERGE_READY_REVIEW_KIND = "merge_ready_review"
CTO_MENTION = "<@U0BNFGX2UAX>"
# Canonical current-head PM review marker location and marker-contract age cap.
MARKER_AGE_MAX_S = 86400
# Explicit evidence paths are supplied by a caller and therefore cannot rely
# on the canonical PR/head filename for provenance.  The PM review transport
# contract currently admits only these reviewer identities for such paths.
EXPLICIT_PM_REVIEW_AGENT = "pm-kimi3-code-reviewer"
BLOCKED_REASON_RE = re.compile(
    r"^blocker:\s*(?P<reason>codex|capture|rebase|ci|product|infra|pm-gate)"
    r"(?:\s*(?:-|:)\s*(?P<detail>.+))?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
CODEX_FOLLOWUP_DETAIL_RE = re.compile(
    r"\bdeferred\s+P2\b.*\bno\s+follow-up\s+issue\b", re.IGNORECASE
)
PASS_RE = re.compile(
    r"MERGE_GUARD: PASS pr=(?P<pr>\d+) head=(?P<head>[0-9a-f]{40}) "
    r"\[CI\] PASS success_run=(?P<ci>\d+)[^;\n]*required_job=\S+[^;\n]*; "
    r"\[E2E Smoke Tests\] PASS success_run=(?P<e2e>\d+)[^;\n]*required_job=\S+[^;\n]*;"
)
EXEMPT_PASS_RE = re.compile(
    r"MERGE_GUARD: PASS pr=(?P<pr>\d+) head=(?P<head>[0-9a-f]{40}) "
    r"exemption=control_plane_only rules_sha256=(?P<rules>[0-9a-f]{64});"
)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def run_json(command: list[str], *, cwd: Path | None = None) -> Any:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return json.loads(completed.stdout)


def label_names(row: dict[str, Any]) -> set[str]:
    return {
        item["name"]
        for item in row.get("labels") or []
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


def is_success_candidate(row: dict[str, Any]) -> bool:
    labels = label_names(row)
    return (
        "pm-state:qa-passed-awaiting-ci" in labels
        or "pm-state:pm-review-pending" in labels
        or {"pm-state:blocked-rework", "pm-blocked:ci"}.issubset(labels)
    )


def appears_green(row: dict[str, Any]) -> bool:
    successful_workflows = {
        str(check.get("workflowName") or "")
        for check in row.get("statusCheckRollup") or []
        if isinstance(check, dict)
        and check.get("status") == "COMPLETED"
        and check.get("conclusion") == "SUCCESS"
    }
    return {"CI", "E2E Smoke Tests"}.issubset(successful_workflows)


def open_candidates(gh_bin: str, repo: str) -> list[dict[str, Any]]:
    rows = run_json(
        [
            gh_bin,
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "number,headRefOid,labels,statusCheckRollup",
        ]
    )
    if not isinstance(rows, list):
        return []
    return [
        row
        for row in rows
        if isinstance(row, dict) and is_success_candidate(row) and appears_green(row)
    ]


def exact_green(
    guard: Path, pr: int, *, cwd: Path
) -> tuple[str, int, int, str] | None:
    # Bind the exact-green evaluation to the guard's PROMOTION mode: promotion
    # reflects the PR's own exact-head pull_request CI/E2E runs and the
    # exact-head proof gates, never current-main CI/E2E health. A red main run
    # is a default-mode merge gate (CTO merge safety), not a promotion
    # suppressor. See incident cp-ci-success-readiness-main-health-promotion-gate.
    completed = subprocess.run(
        [str(guard), "--mode", "promotion", str(pr)],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=90,
    )
    output = f"{completed.stdout}\n{completed.stderr}".strip()
    if completed.returncode != 0:
        return None
    match = PASS_RE.search(output)
    if match and int(match.group("pr")) == pr:
        return (
            match.group("head"),
            int(match.group("ci")),
            int(match.group("e2e")),
            output,
        )
    exempt = EXEMPT_PASS_RE.search(output)
    if exempt and int(exempt.group("pr")) == pr:
        return (exempt.group("head"), 0, 0, output)
    return None


def sentinel_path(directory: Path, pr: int) -> Path:
    return directory / f"pm-required-ci-reconcile-{pr}.json"


def run_pm_ops(args: argparse.Namespace, command: list[str]) -> None:
    pm_ops = Path(args.pm_ops)
    if not pm_ops.is_file():
        raise RuntimeError(f"pm-ops missing: {pm_ops}")
    completed = subprocess.run(
        [sys.executable, str(pm_ops), *command],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(detail or f"pm-ops exited {completed.returncode}")


def upsert_codex_followup_obligation(
    args: argparse.Namespace, *, head: str, proof: Path
) -> None:
    evidence = {
        "head_sha": head,
        "pm_stop_actionable": 1,
        "proof": str(proof),
        "required_skill": "codex-comment-processing",
        "source": "ci-success-reconciliation",
    }
    action = (
        f"Invoke Skill(codex-comment-processing) for PR #{args.pr} at exact head "
        f"{head}. Re-read every unresolved Codex thread; for each non-gating P2 "
        "deferral, file and link a concrete follow-up issue, reply inline, and "
        "resolve the thread. Then rerun pm-readiness-contract and, on PASS, resolve "
        "this CI event as merge_ready with the generated proof via "
        "ci-success-reconciliation.py --resolve --resolution merge_ready "
        "so the obligation closes. Do not edit labels directly "
        "or rerun CI unless the head changes."
    )
    run_pm_ops(
        args,
        [
            "obligation-upsert",
            "--kind",
            CODEX_FOLLOWUP_KIND,
            "--severity",
            "high",
            "--target-type",
            "pr",
            "--target-id",
            str(args.pr),
            "--pr",
            str(args.pr),
            "--owner",
            "pm",
            "--horizon",
            "hourly",
            "--next-review-at",
            now_iso(),
            "--dedupe-group",
            f"{CODEX_FOLLOWUP_KIND}:{args.pr}:{head}",
            "--title",
            f"PR #{args.pr} needs Codex P2 follow-up reconciliation",
            "--action",
            action,
            "--blocker",
            "deferred_codex_followup_missing",
            "--evidence-json",
            json.dumps(evidence, sort_keys=True),
        ],
    )


def resolve_codex_followup_obligation(
    args: argparse.Namespace, *, reason: str, head: str
) -> None:
    run_pm_ops(
        args,
        [
            "obligation-resolve",
            "--kind",
            CODEX_FOLLOWUP_KIND,
            "--target-type",
            "pr",
            "--target-id",
            str(args.pr),
            "--pr",
            str(args.pr),
            "--reason",
            reason,
            "--external-state",
            f"head={head}",
        ],
    )


def upsert_ci_reconcile_obligation(
    args: argparse.Namespace,
    *,
    pr: int,
    head: str,
    ci_run_id: int,
    e2e_run_id: int,
) -> None:
    """Durably wake PM for one terminal-green PR missed by the typed transition.

    The pm-ops obligation upsert dedupes on (kind, target_type, target_id, pr)
    for open rows, so repeated watchdog ticks update the same wake row instead
    of minting duplicates; the sentinel's head_sha check keeps a resolved event
    closed (identical head/event is idempotent) while a new head is a new tuple.
    """

    evidence = {
        "head_sha": head,
        "ci_run_id": str(ci_run_id),
        "e2e_run_id": str(e2e_run_id),
        "pm_stop_actionable": 1,
        "required_skill": "ci-success-reconciliation",
        "source": "ci-success-reconciliation-watchdog",
    }
    action = (
        f"PR #{pr} is terminal green at exact head {head} "
        f"(CI run {ci_run_id} / E2E run {e2e_run_id}) with PM state still "
        "awaiting reconciliation. Run Skill(ci-success-reconciliation) and "
        "pm-readiness-contract now, then execute exactly one typed merge-ready "
        "or blocker transition and resolve the CI event with that disposition. "
        "Do not rerun CI, recapture, or edit labels directly."
    )
    run_pm_ops(
        args,
        [
            "obligation-upsert",
            "--kind",
            CI_RECONCILE_KIND,
            "--severity",
            "high",
            "--target-type",
            "pr",
            "--target-id",
            str(pr),
            "--pr",
            str(pr),
            "--owner",
            "pm",
            "--horizon",
            "hourly",
            "--next-review-at",
            now_iso(),
            "--dedupe-group",
            f"{CI_RECONCILE_KIND}:{pr}:{head}",
            "--title",
            f"PR #{pr} terminal CI success requires reconciliation",
            "--action",
            action,
            "--blocker",
            "ci_success_pending_reconciliation",
            "--evidence-json",
            json.dumps(evidence, sort_keys=True),
        ],
    )


def resolve_ci_reconcile_obligation(
    args: argparse.Namespace, *, pr: int, reason: str, head: str
) -> None:
    run_pm_ops(
        args,
        [
            "obligation-resolve",
            "--kind",
            CI_RECONCILE_KIND,
            "--target-type",
            "pr",
            "--target-id",
            str(pr),
            "--pr",
            str(pr),
            "--reason",
            reason,
            "--external-state",
            f"head={head}",
        ],
    )


def review_marker_path(directory: Path, pr: int, head: str) -> Path:
    # Canonical current-head PM review marker location.
    return directory / f"pm-claude-code-review-{pr}-{head}.md"


def review_marker_binds_head(
    directory: Path,
    pr: int,
    head: str,
    *,
    marker_path: Path | None = None,
) -> bool:
    """Phase-a review marker binds the exact head (guard row 3).

    The canonical marker file must exist, be younger than the contract's
    86400s cap, carry the exact headRefOid, one recognized verdict class
    (codex-review-companion APPROVE, PM Claude/PM Opus PASS, or PM_OVERRIDE
    APPROVE with the identity fields), runtime_control_point, and the phase-a
    pass_scope/readiness_ceiling contract. Explicit evidence paths additionally
    bind the marker body to the requested PR and the sanctioned PM transport
    identity fields; canonical default paths retain their legacy compatibility
    contract. This is the consumer's pre-check before it writes the merge-ready
    promotion proof; the existing merge-ready handler re-validates the proof
    and the unresolved-thread gate fail closed.
    """
    # Callers with an explicit evidence path have already supplied the
    # authoritative artifact.  Keep canonical full-head derivation as the
    # default, while validating explicit paths with the same contract.
    explicit_marker = marker_path is not None
    marker = marker_path if explicit_marker else review_marker_path(directory, pr, head)
    try:
        if not marker.is_file():
            return False
        if time.time() - marker.stat().st_mtime > MARKER_AGE_MAX_S:
            return False
        text = marker.read_text(encoding="utf-8")
    except OSError:
        return False
    if not re.search(rf"^headRefOid:\s*{re.escape(head)}\s*$", text, re.MULTILINE):
        return False
    if re.search(
        r"^MARKER_PROVENANCE[:=]\s*codex-review-companion($|\s)",
        text,
        re.MULTILINE,
    ):
        ok = (
            re.search(r"^FINAL_REVIEWER_VERDICT[:=]\s*APPROVE($|\s)", text, re.MULTILINE)
            and re.search(rf"^(?:PR|pr)[:=]\s*#?{pr}($|\s)", text, re.MULTILINE)
            and re.search(
                rf"^(?:HEAD_SHA|headRefOid)[:=]\s*{re.escape(head)}($|\s)",
                text,
                re.MULTILINE,
            )
        )
    elif re.search(r"^(?:PM_CLAUDE_REVIEW|PM_OPUS_REVIEW)[:=]\s*PASS($|\s)", text, re.MULTILINE):
        ok = True
    elif re.search(r"^PM_OVERRIDE[:=]\s*APPROVE($|\s)", text, re.MULTILINE):
        ok = all(
            re.search(
                rf"^override_{field}:\s*[^ \t\r\n].+",
                text,
                re.MULTILINE,
            )
            for field in ("identity", "timestamp", "source_citation", "rationale")
        )
    else:
        ok = False
    if not ok:
        return False
    if explicit_marker and not re.search(
        rf"^(?:PR|pr)[:=]\s*#?{pr}($|\s)", text, re.MULTILINE
    ):
        # Every explicitly supplied evidence class, including PM_OVERRIDE and
        # Codex compatibility artifacts, must bind its body to this PR.  A
        # shared head or canonical directory is not sufficient identity.
        return False
    if explicit_marker and re.search(
        r"^(?:PM_CLAUDE_REVIEW|PM_OPUS_REVIEW)[:=]\s*PASS($|\s)",
        text,
        re.MULTILINE,
    ):
        # An explicitly named artifact is not trusted merely because it has a
        # matching head.  Bind its body to the requested PR and the runner
        # identity fields emitted by the sanctioned PM code-review transport.
        required_identity = (
            rf"^(?:PR|pr)[:=]\s*#?{pr}($|\s)",
            r"^review_model:\s*(kimi3|fable)($|\s)",
            r"^model_attempts:\s*(kimi3|fable)($|\s)",
            r"^fallback_reason:\s*none($|\s)",
            rf"^agent:\s*{re.escape(EXPLICIT_PM_REVIEW_AGENT)}($|\s)",
        )
        if not all(re.search(pattern, text, re.MULTILINE) for pattern in required_identity):
            return False
        model = re.search(r"^review_model:\s*(kimi3|fable)($|\s)", text, re.MULTILINE)
        attempts = re.search(r"^model_attempts:\s*(kimi3|fable)($|\s)", text, re.MULTILINE)
        if model is None or attempts is None or model.group(1) != attempts.group(1):
            return False
    if not re.search(r"^runtime_control_point:\s*[^ \t\r\n].+", text, re.MULTILINE):
        return False
    if not re.search(r"^pass_scope:\s*phase-a($|\s)", text, re.MULTILINE):
        return False
    if not re.search(r"^readiness_ceiling:\s*[^ \t\r\n].+", text, re.MULTILINE):
        return False
    return True


def _merge_ready_handoff_path(args: argparse.Namespace, *, pr: int, head: str) -> Path:
    return Path(args.sentinel_dir) / f"pm-merge-ready-handoff-{pr}-{head}.json"


def _merge_ready_message_path(args: argparse.Namespace, *, pr: int, head: str) -> Path:
    return Path(args.sentinel_dir) / f"pm-merge-ready-handoff-{pr}-{head}.msg"


def _run_pm_ops_durable(args: argparse.Namespace, command: list[str]) -> None:
    """Write an existing event/obligation receipt without any legacy writer."""
    run_pm_ops(args, command)


def promote_to_merge_ready(
    args: argparse.Namespace,
    *,
    pr: int,
    head: str,
    ci_run_id: int = 0,
    e2e_run_id: int = 0,
) -> tuple[bool, str]:
    """Persist and deliver one exact-head CTO merge-ready handoff.

    This is intentionally a handoff, not a PR-state mutation.  The existing
    PM-ops event/obligation ledger is the durable outbox and the existing
    transition-alert transport performs the one Slack post.  Both are keyed by
    ``(pr, head)``; a transport interruption can therefore be resumed and a
    duplicate alert is suppressed by the transport's own key.
    """
    try:
        live_head = current_open_pr_head(args.gh_bin, args.repo, pr)
    except (OSError, subprocess.SubprocessError, RuntimeError, json.JSONDecodeError) as exc:
        return False, f"merge-ready handoff head read failed: {exc}"
    if live_head != head:
        return False, f"merge-ready handoff head drift event={head} current={live_head}"

    marker = review_marker_path(Path(args.marker_dir), pr, head)
    handoff_path = _merge_ready_handoff_path(args, pr=pr, head=head)
    message_path = _merge_ready_message_path(args, pr=pr, head=head)
    payload = load_json(handoff_path)
    if payload and payload.get("status") == "sent":
        return True, str(handoff_path)

    timestamp = now_iso()
    handoff = {
        "schema_version": 1,
        "event": "merge_ready",
        "source": "ci-success-reconciliation",
        "status": "pending",
        "pr": pr,
        "head_sha": head,
        "ci_run_id": int(ci_run_id),
        "e2e_run_id": int(e2e_run_id),
        "review_marker": str(marker),
        "destination": "C0ALZJHGE49",
        "mention": CTO_MENTION,
        "dedupe_key": f"merge-ready:{pr}:{head}",
        "created_at": str(payload.get("created_at") or timestamp),
        "updated_at": timestamp,
    }
    atomic_write(handoff_path, handoff)
    message = (
        f"*PR #{pr} — MERGE READY* {CTO_MENTION}\n"
        f"Exact head: `{head}`\n"
        f"CI run: `{ci_run_id}`\n"
        f"E2E Smoke Tests run: `{e2e_run_id}`\n"
        "Terminal required pull_request jobs are green at this head. "
        "CTO merge task owns the next head-pinned merge decision; no label or "
        "workflow mutation was performed by this handoff."
    )
    message_path.parent.mkdir(parents=True, exist_ok=True)
    message_path.write_text(message + "\n", encoding="utf-8")

    try:
        _run_pm_ops_durable(
            args,
            [
                "record",
                "--source",
                "ci-success-reconciliation",
                "--event",
                "merge_ready_handoff",
                "--target-type",
                "pr",
                "--target-id",
                str(pr),
                "--pr",
                str(pr),
                "--head-sha",
                head,
                "--dedupe-key",
                f"merge-ready:{pr}:{head}",
                "--payload-json",
                json.dumps(handoff, sort_keys=True),
            ],
        )
        _run_pm_ops_durable(
            args,
            [
                "obligation-upsert",
                "--kind",
                MERGE_READY_REVIEW_KIND,
                "--severity",
                "high",
                "--target-type",
                "pr",
                "--target-id",
                str(pr),
                "--pr",
                str(pr),
                "--owner",
                "cto",
                "--horizon",
                "hourly",
                "--next-review-at",
                timestamp,
                "--dedupe-group",
                f"merge-ready:{pr}:{head}",
                "--title",
                f"PR #{pr} is merge-ready at exact head {head}",
                "--action",
                f"CTO merge task: re-read PR #{pr} at exact head {head}, run the current-head merge guard, and decide the head-pinned merge. See durable handoff {handoff_path}.",
                "--blocker",
                "terminal_green_merge_ready_handoff",
                "--evidence-json",
                json.dumps(handoff, sort_keys=True),
            ],
        )
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return False, f"merge-ready handoff ledger refused: {exc}"

    alert_raw = getattr(args, "merge_ready_alert", DEFAULT_MERGE_READY_ALERT) or ""
    if not alert_raw:
        handoff.update(
            {
                "status": "sent",
                "notification_receipt": "notification_skipped_no_transport",
                "updated_at": now_iso(),
            }
        )
        atomic_write(handoff_path, handoff)
        return True, str(handoff_path)
    alert = Path(alert_raw)
    if not alert.is_file() or not os.access(alert, os.X_OK):
        return False, f"merge-ready notification transport missing: {alert}"
    try:
        completed = subprocess.run(
            [
                str(alert),
                "--event",
                "external-action-required",
                "--pr",
                str(pr),
                "--head",
                head,
                "--state",
                "merge-ready",
                "--reason",
                "terminal_green_merge_ready",
                "--message-file",
                str(message_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"merge-ready notification transport: {exc}"
    output = f"{completed.stdout}\n{completed.stderr}".strip()
    if completed.returncode != 0 or (
        "MERGE_READY_NOTIFY_OK" not in output
        and "MERGE_READY_NOTIFY_SKIPPED" not in output
    ):
        return False, f"merge-ready notification refused: {output[-300:]}"

    handoff.update({"status": "sent", "notification_receipt": output[-500:], "updated_at": now_iso()})
    atomic_write(handoff_path, handoff)
    return True, str(handoff_path)


def blocked_proof_reason(
    proof: Path, *, pr: int, head: str, blocked_next_action: str | None
) -> str:
    try:
        text = proof.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read blocked proof: {exc}") from exc

    required = {
        "READY_PACKET: BLOCKED": r"^READY_PACKET:\s*BLOCKED\s*$",
        f"PR: {pr}": rf"^PR:\s*{pr}\s*$",
        f"headRefOid: {head}": rf"^headRefOid:\s*{re.escape(head)}\s*$",
    }
    for label, pattern in required.items():
        if not re.search(pattern, text, re.MULTILINE):
            raise ValueError(f"blocked proof missing exact marker {label}")

    match = BLOCKED_REASON_RE.search(text)
    if not match:
        raise ValueError("blocked proof missing recognized typed blocker")
    reason = match.group("reason").lower()
    detail = (match.group("detail") or "").strip()
    if blocked_next_action == "codex_followup" and (
        reason != "codex" or not CODEX_FOLLOWUP_DETAIL_RE.search(detail)
    ):
        raise ValueError(
            "codex_followup requires blocker: codex - deferred P2 has no follow-up issue"
        )
    return reason


def materialize(args: argparse.Namespace) -> int:
    directory = Path(args.sentinel_dir)
    guard = Path(args.guard)
    guard_cwd = Path(args.guard_cwd)
    result: dict[str, Any] = {
        "schema_version": 1,
        "source": "ci-success-reconciliation-watchdog",
        "materialized": [],
        "promoted": [],
        "promotion_blocked": [],
        "already_pending": [],
        "not_green": [],
        "errors": [],
    }
    try:
        candidates = open_candidates(args.gh_bin, args.repo)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        result["degraded"] = True
        result["errors"].append(f"candidate_query:{exc}")
        print(json.dumps(result, sort_keys=True))
        return 2

    checks_attempted = 0
    for row in candidates:
        pr = int(row["number"])
        if args.pr and pr != args.pr:
            continue
        expected_head = str(row.get("headRefOid") or "")
        path = sentinel_path(directory, pr)
        existing = load_json(path)
        if (
            existing.get("head_sha") == expected_head
            and existing.get("status") in {"pending", "in_progress", "resolved"}
        ):
            result["already_pending"].append(pr)
            if existing.get("status") in {"pending", "in_progress"}:
                try:
                    upsert_ci_reconcile_obligation(
                        args,
                        pr=pr,
                        head=expected_head,
                        ci_run_id=int(existing.get("ci_run_id") or 0),
                        e2e_run_id=int(existing.get("e2e_run_id") or 0),
                    )
                except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                    result["errors"].append(f"PR#{pr}:obligation:{exc}")
            continue
        if checks_attempted >= args.max_checks:
            break
        checks_attempted += 1
        try:
            proof = exact_green(guard, pr, cwd=guard_cwd)
        except (subprocess.SubprocessError, OSError) as exc:
            result["errors"].append(f"PR#{pr}:guard:{exc}")
            continue
        if proof is None:
            result["not_green"].append(pr)
            continue
        head, ci_run, e2e_run, guard_output = proof
        if expected_head and expected_head != head:
            result["errors"].append(
                f"PR#{pr}:head_drift:list={expected_head}:guard={head}"
            )
            continue

        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            existing = load_json(path)
            if (
                existing.get("head_sha") == head
                and existing.get("status") in {"pending", "in_progress", "resolved"}
            ):
                result["already_pending"].append(pr)
                continue
            if (
                "pm-state:qa-passed-awaiting-ci" in label_names(row)
                and review_marker_binds_head(Path(args.marker_dir), pr, head)
            ):
                # Terminal-success handoff: exact-head CI/E2E rollup green
                # (guard rows 1-2) and the phase-a review marker binding the
                # same head (guard row 3).  The direct handoff writes the
                # existing event/obligation ledger and posts the CTO alert;
                # it does not mutate PR state and invokes no legacy writer.
                promoted_ok, detail = promote_to_merge_ready(
                    args,
                    pr=pr,
                    head=head,
                    ci_run_id=ci_run,
                    e2e_run_id=e2e_run,
                )
                if promoted_ok:
                    timestamp = now_iso()
                    payload = {
                        "schema_version": 1,
                        "source": "ci-success-reconciliation-watchdog",
                        "status": "resolved",
                        "resolution": "merge_ready",
                        "promotion": "terminal_success",
                        "event": "success",
                        "pr": str(pr),
                        "run_id": str(e2e_run),
                        "ci_run_id": str(ci_run),
                        "e2e_run_id": str(e2e_run),
                        "head_sha": head,
                        "alert_thread_ts": "unknown",
                        "reason": (
                            "exact-head control-plane exemption promoted"
                            if ci_run == 0 and e2e_run == 0
                            else "exact-head terminal CI/E2E success promoted"
                        ),
                        "proof": detail,
                        "guard_output": guard_output,
                        "created_at": timestamp,
                        "promoted_at": timestamp,
                        "updated_at": timestamp,
                    }
                    atomic_write(path, payload)
                    result["promoted"].append(pr)
                    continue
                result["promotion_blocked"].append({"pr": pr, "reason": detail})
            timestamp = now_iso()
            payload = {
                "schema_version": 1,
                "source": "ci-success-reconciliation-watchdog",
                "status": "pending",
                "event": "success",
                "pr": str(pr),
                "run_id": str(e2e_run),
                "ci_run_id": str(ci_run),
                "e2e_run_id": str(e2e_run),
                "head_sha": head,
                "alert_thread_ts": "unknown",
                "reason": (
                    "exact-head control-plane exemption needs PM state reconciliation"
                    if ci_run == 0 and e2e_run == 0
                    else "exact-head CI and E2E are green but PM state needs reconciliation"
                ),
                "guard_output": guard_output,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            atomic_write(path, payload)
            try:
                upsert_ci_reconcile_obligation(
                    args,
                    pr=pr,
                    head=head,
                    ci_run_id=ci_run,
                    e2e_run_id=e2e_run,
                )
            except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                result["errors"].append(f"PR#{pr}:obligation:{exc}")
            result["materialized"].append(pr)

    result["candidate_count"] = len(candidates)
    result["checks_attempted"] = checks_attempted
    result["materialized_count"] = len(result["materialized"])
    print(json.dumps(result, sort_keys=True))
    return 0


def current_head(gh_bin: str, repo: str, pr: int) -> str:
    value = run_json(
        [gh_bin, "pr", "view", str(pr), "--repo", repo, "--json", "headRefOid"]
    )
    if not isinstance(value, dict) or not value.get("headRefOid"):
        raise RuntimeError(f"cannot read PR #{pr} head")
    return str(value["headRefOid"])


def current_open_pr_head(gh_bin: str, repo: str, pr: int) -> str:
    """Read the live PR identity required before a CTO handoff."""
    value = run_json(
        [
            gh_bin,
            "pr",
            "view",
            str(pr),
            "--repo",
            repo,
            "--json",
            "headRefOid,state,isDraft",
        ]
    )
    if not isinstance(value, dict):
        raise RuntimeError(f"cannot read PR #{pr} identity")
    head = str(value.get("headRefOid") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise RuntimeError(f"PR #{pr} has invalid live head")
    if str(value.get("state") or "").upper() != "OPEN":
        raise RuntimeError(f"PR #{pr} is not open")
    if value.get("isDraft") is not False:
        raise RuntimeError(f"PR #{pr} is draft or draft state is unavailable")
    return head


def claim(args: argparse.Namespace) -> int:
    path = sentinel_path(Path(args.sentinel_dir), args.pr)
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        data = load_json(path)
        if not data:
            print(json.dumps({"status": "missing", "pr": args.pr}))
            return 1
        head = current_head(args.gh_bin, args.repo, args.pr)
        event_head = str(data.get("head_sha") or "")
        if event_head and event_head != head:
            data.update(
                {
                    "status": "superseded",
                    "resolution": "head_drift",
                    "current_head": head,
                    "resolved_at": now_iso(),
                    "updated_at": now_iso(),
                }
            )
            atomic_write(path, data)
            print(
                json.dumps(
                    {
                        "status": "superseded",
                        "pr": args.pr,
                        "event_head": event_head,
                        "current_head": head,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if data.get("status") in {"resolved", "superseded"}:
            print(json.dumps(data, sort_keys=True))
            return 0
        if data.get("status") == "in_progress":
            print(json.dumps({**data, "claim": "already_claimed"}, sort_keys=True))
            return 0
        data.update(
            {
                "status": "in_progress",
                "claimed_at": now_iso(),
                "current_head": head,
                "updated_at": now_iso(),
            }
        )
        atomic_write(path, data)
        print(json.dumps(data, sort_keys=True))
    return 0


def resolve(args: argparse.Namespace) -> int:
    path = sentinel_path(Path(args.sentinel_dir), args.pr)
    proof = Path(args.proof) if args.proof else None
    # Only blocked resolution requires a caller-authored proof. merge_ready is
    # The canonical single handoff path: resolve() verifies the exact-head
    # phase-a review marker (guard row 3, mirroring the watchdog consumer),
    # then persists and delivers the direct CTO handoff. No caller-authored
    # proof ceremony or legacy-writer dependency is required.
    if args.resolution == "blocked" and (proof is None or not proof.is_file()):
        print(
            f"CI_SUCCESS_RESOLUTION_REFUSED missing proof for {args.resolution}",
            file=sys.stderr,
        )
        return 2
    if args.blocked_next_action and args.resolution != "blocked":
        print(
            "CI_SUCCESS_RESOLUTION_REFUSED --blocked-next-action requires --resolution blocked",
            file=sys.stderr,
        )
        return 2
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        data = load_json(path)
        if not data:
            print(json.dumps({"status": "missing", "pr": args.pr}))
            return 1
        head = current_head(args.gh_bin, args.repo, args.pr)
        event_head = str(data.get("head_sha") or "")
        if args.resolution != "superseded" and event_head and event_head != head:
            print(
                f"CI_SUCCESS_RESOLUTION_REFUSED head drift event={event_head} current={head}",
                file=sys.stderr,
            )
            return 2
        # Idempotent replay: an already-resolved merge_ready receipt at the
        # exact same head returns the existing receipt without re-promoting or
        # re-notifying. A stale head is superseded above.
        if (
            args.resolution == "merge_ready"
            and data.get("status") == "resolved"
            and data.get("resolution") == "merge_ready"
            and str(data.get("head_sha") or "") == head
        ):
            print(json.dumps(data, sort_keys=True))
            return 0
        blocked_reason = "none"
        if args.resolution == "blocked":
            assert proof is not None
            try:
                blocked_reason = blocked_proof_reason(
                    proof,
                    pr=args.pr,
                    head=head,
                    blocked_next_action=args.blocked_next_action,
                )
            except ValueError as exc:
                print(
                    f"CI_SUCCESS_RESOLUTION_REFUSED invalid blocked proof: {exc}",
                    file=sys.stderr,
                )
                return 2
        # Canonical merge_ready handoff: the exact-head phase-a review marker
        # must bind the current head before the durable CTO handoff is written
        # (guard row 3, same as the watchdog consumer); the returned handoff
        # path is recorded in the resolved sentinel as the audit receipt.
        if args.resolution == "merge_ready":
            if not review_marker_binds_head(Path(args.marker_dir), args.pr, head):
                print(
                    "CI_SUCCESS_RESOLUTION_REFUSED merge_ready promotion: "
                    f"missing/invalid exact-head phase-a review marker for "
                    f"PR #{args.pr} head={head}",
                    file=sys.stderr,
                )
                return 2
            # Slack ingress may leave a pending sentinel without a bound head
            # or run IDs.  Never turn that unbound wake into a CTO handoff:
            # re-run the exact-head promotion guard and require its real
            # pull_request CI/E2E evidence before any ledger or alert effect.
            exact = exact_green(Path(args.guard), args.pr, cwd=Path(args.guard_cwd))
            if exact is None or exact[0] != head:
                print(
                    "CI_SUCCESS_RESOLUTION_REFUSED merge_ready promotion: "
                    "exact-head required-job guard did not admit current head",
                    file=sys.stderr,
                )
                return 2
            _, ci_run_id, e2e_run_id, _ = exact
            promoted_ok, detail = promote_to_merge_ready(
                args,
                pr=args.pr,
                head=head,
                ci_run_id=ci_run_id,
                e2e_run_id=e2e_run_id,
            )
            if not promoted_ok:
                print(
                    f"CI_SUCCESS_RESOLUTION_REFUSED merge_ready promotion: {detail}",
                    file=sys.stderr,
                )
                return 2
            proof = Path(detail)
        try:
            if args.blocked_next_action == "codex_followup":
                assert proof is not None
                upsert_codex_followup_obligation(args, head=head, proof=proof)
            elif args.resolution in {"merge_ready", "superseded"}:
                resolve_codex_followup_obligation(
                    args,
                    reason=f"ci_success_{args.resolution}",
                    head=head,
                )
            resolve_ci_reconcile_obligation(
                args,
                pr=args.pr,
                reason=f"ci_success_{args.resolution}",
                head=head,
            )
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            print(
                f"CI_SUCCESS_RESOLUTION_REFUSED successor obligation: {exc}",
                file=sys.stderr,
            )
            return 2
        timestamp = now_iso()
        data.update(
            {
                "status": "superseded"
                if args.resolution == "superseded"
                else "resolved",
                "resolution": args.resolution,
                "proof": str(proof) if proof else "none",
                "promotion": (
                    "terminal_success"
                    if args.resolution == "merge_ready"
                    else "none"
                ),
                "blocked_reason": blocked_reason,
                "blocked_next_action": args.blocked_next_action or "none",
                "current_head": head,
                "resolved_at": timestamp,
                "updated_at": timestamp,
            }
        )
        atomic_write(path, data)
        print(json.dumps(data, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    mode = value.add_mutually_exclusive_group(required=True)
    mode.add_argument("--materialize", action="store_true")
    mode.add_argument("--claim", action="store_true")
    mode.add_argument("--resolve", action="store_true")
    value.add_argument("--pr", type=int)
    value.add_argument(
        "--resolution", choices=("merge_ready", "blocked", "superseded")
    )
    value.add_argument("--proof")
    value.add_argument("--blocked-next-action", choices=("codex_followup",))
    value.add_argument("--repo", default=os.environ.get("CI_SUCCESS_REPO", DEFAULT_REPO))
    value.add_argument("--gh-bin", default=os.environ.get("CI_SUCCESS_GH_BIN", "gh"))
    value.add_argument(
        "--pm-ops", default=os.environ.get("CI_SUCCESS_PM_OPS", DEFAULT_PM_OPS)
    )
    value.add_argument(
        "--merge-ready-alert",
        default=os.environ.get(
            "CI_SUCCESS_MERGE_READY_ALERT", DEFAULT_MERGE_READY_ALERT
        ),
    )
    value.add_argument(
        "--marker-dir",
        default=os.environ.get("CI_SUCCESS_MARKER_DIR", "/tmp"),
    )
    value.add_argument("--guard", default=os.environ.get("CI_SUCCESS_GUARD", DEFAULT_GUARD))
    value.add_argument(
        "--guard-cwd",
        default=os.environ.get(
            "CI_SUCCESS_GUARD_CWD", "/Users/rajiv/Downloads/projects/heydonna-app"
        ),
    )
    value.add_argument(
        "--sentinel-dir",
        default=os.environ.get("CI_SUCCESS_SENTINEL_DIR", "/tmp"),
    )
    value.add_argument(
        "--max-checks",
        type=int,
        default=int(os.environ.get("CI_SUCCESS_MAX_CHECKS", "1")),
    )
    return value


def main() -> int:
    args = parser().parse_args()
    if args.materialize:
        return materialize(args)
    if not args.pr:
        raise SystemExit("--pr is required for --claim/--resolve")
    if args.claim:
        return claim(args)
    if not args.resolution:
        raise SystemExit("--resolution is required for --resolve")
    return resolve(args)


if __name__ == "__main__":
    raise SystemExit(main())
