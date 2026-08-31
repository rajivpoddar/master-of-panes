#!/usr/bin/env python3
"""Publish one canonical CI investigation verdict to its current PR head.

The comment is idempotent for (PR, run, attempt, head).  It is also the
durable admission record consumed by the same-head rerun wrapper.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


CAUSAL_CAPTURE_CLASS = "CAPTURE_REQUIRED_EXACT_HEAD__STRICT_R2_MULTI_STAGE_MISS"
# Read-only CI investigations may establish a pre-existing E2E harness flake
# that recovered on its in-run retry.  This is intentionally a single narrow
# classification lane; product, fixture, and ambiguous classes remain refused
# by the packet adapter below.
RERUNNABLE_TEST_CLASS = "test"
# PM's report taxonomy uses a more specific wall-budget label for the same
# already-supported rerunnable-test lane.  Keep the emitted canonical verdict
# on the existing ``test`` class; admit only this exact producer label.
RERUNNABLE_TEST_CLASS_ALIASES = {
    RERUNNABLE_TEST_CLASS,
    "test_shared_harness_wall_budget",
}
FIXTURE_KEY_RE = re.compile(
    r"^fixtures/llm/v2/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*/[0-9a-f]{64}\.bin$"
)
CAPTURE_WORKFLOW_NAME = "E2E LLM Proxy Capture (manual)"
CAPTURE_REQUIRED_STEPS = frozenset(
    {
        "Verify exact PR head checkout",
        "Capture NY, ACR, and General wizard auto-process",
        "Capture full AI proofreading smoke suite",
        "Capture legal-deposition proofread and section-format paths",
    }
)
CAPTURE_PROMOTION_STEP = "Validate and promote staged capture fixtures"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def current_attempt(run: dict[str, Any]) -> int:
    """Return the authoritative run attempt; never synthesize attempt one."""
    raw = run.get("run_attempt")
    if isinstance(raw, bool) or not re.fullmatch(r"[1-9][0-9]*", str(raw or "")):
        raise ValueError("run_attempt is missing or malformed")
    return int(raw)


def view_attempt(run: dict[str, Any]) -> int:
    """Return the attempt field emitted by ``gh run view`` without fallback."""
    raw = run.get("attempt")
    if isinstance(raw, bool) or not re.fullmatch(r"[1-9][0-9]*", str(raw or "")):
        raise ValueError("attempt is missing or malformed")
    return int(raw)


def gh_json(args: list[str], *, input_text: str | None = None) -> Any:
    proc = subprocess.run(
        ["gh", *args],
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"gh {' '.join(args)} failed")
    return json.loads(proc.stdout or "null")


def normalize_sha(verdict: dict[str, Any]) -> str:
    return str(
        verdict.get("sha")
        or verdict.get("run_head_sha")
        or verdict.get("head_sha")
        or ""
    ).strip()


def verdict_fingerprint(verdict: dict[str, Any]) -> str:
    """Return the stable repeated-flake signature for a verdict."""
    fast = verdict.get("fast_fingerprint")
    if isinstance(fast, dict):
        signature = str(fast.get("signature") or fast.get("causal_signature") or "").strip()
        if signature:
            return signature
    if str(verdict.get("classification") or "").strip().lower() == RERUNNABLE_TEST_CLASS:
        signature = str(
            verdict.get("causal_fingerprint")
            or verdict.get("first_causal_boundary")
            or verdict.get("root_cause")
            or verdict.get("first_failing_call_site")
            or verdict.get("classification")
            or ""
        ).strip()
        return signature[:240]
    return str(
        verdict.get("root_cause")
        or verdict.get("first_failing_call_site")
        or verdict.get("causal_fingerprint")
        or verdict.get("first_causal_boundary")
        or verdict.get("classification")
        or ""
    ).strip()[:240]


def open_followup_issue(verdict: dict[str, Any]) -> bool:
    """True when the verdict carries an open follow-up issue reference."""
    for key in ("followup_issue", "follow_up_issue", "flake_followup", "preexisting_followup"):
        value = str(verdict.get(key) or "").strip()
        if value:
            return True
    return False


def quarantine_state(
    verdict: dict[str, Any],
    *,
    prior_comments: list[dict[str, Any]],
    store_path: Path,
) -> dict[str, Any]:
    """Rolling 7-day CI-only flake quarantine.

    A verdict whose local repro is ``passed`` (CI-only flake) is quarantined
    when the same fingerprint already appeared in a prior verdict with no open
    follow-up issue. The store is the canonical rolling counter; PR comments
    are the fallback evidence for older verdicts that predate the store.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)
    signature = verdict_fingerprint(verdict)
    if not signature:
        return {
            "quarantine_blocked": False,
            "quarantine_reason": None,
            "prior_occurrences": 0,
        }

    store: dict[str, dict[str, Any]] = {}
    if store_path.is_file():
        try:
            store = json.loads(store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            store = {}
    entries = [
        entry
        for entry in store.values()
        if isinstance(entry, dict)
        and str(entry.get("signature") or "") == signature
        and str(entry.get("run_id") or "") != str(verdict.get("run_id") or "")
        and _parse_ts(entry.get("ts")) >= cutoff
    ]
    prior_occurrences = len(entries)
    if prior_occurrences == 0:
        for comment in prior_comments:
            body = str(comment.get("body") or "")
            marker = "<!-- ci-verdict: "
            start = body.find(marker)
            while start >= 0:
                end = body.find("-->", start)
                if end < 0:
                    break
                try:
                    prior = json.loads(body[start + len(marker): end])
                except json.JSONDecodeError:
                    start = body.find(marker, end)
                    continue
                if str(prior.get("run_id")) != str(verdict.get("run_id")) and verdict_fingerprint(prior) == signature:
                    prior_occurrences += 1
                start = body.find(marker, end)

    blocked = (
        prior_occurrences >= 1
        and str(verdict.get("local_repro_result") or "").strip().lower() == "passed"
        and not open_followup_issue(verdict)
    )
    result = {
        "quarantine_blocked": blocked,
        "quarantine_reason": (
            "repeated_ci_only_flake" if blocked else None
        ),
        "prior_occurrences": prior_occurrences,
    }
    store[signature] = {
        "signature": signature,
        "ts": now.isoformat(),
        "run_id": str(verdict.get("run_id") or ""),
        "local_repro_result": str(verdict.get("local_repro_result") or ""),
    }
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps(store, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def _parse_ts(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _causal_terminal_from_verdict(
    verdict: dict[str, Any], *, pr: int, run_id: int, attempt: int, head: str
) -> dict[str, Any] | None:
    """Build the sole structured causal terminal emitted by this publisher."""
    classification = verdict.get("classification")
    terminal_class = verdict.get("terminal_class")
    if classification is not None and terminal_class is not None and classification != terminal_class:
        raise ValueError("causal classification fields conflict")
    resolved = classification if classification is not None else terminal_class
    if resolved is None:
        return None
    if resolved != CAUSAL_CAPTURE_CLASS:
        return None
    if (
        verdict.get("run_id") not in (run_id, str(run_id))
        or int(verdict.get("pr") or 0) != pr
        or int(verdict.get("attempt") or 0) != attempt
        or verdict.get("run_head_sha", head) != head
        or verdict.get("current_pr_head_sha", head) != head
        or verdict.get("current_for_pr") not in (True, "true")
    ):
        raise ValueError("causal terminal tuple is not exact-head")
    primary_keys = verdict.get("primary_keys")
    alias_keys = verdict.get("alias_keys")
    if (
        not isinstance(primary_keys, list)
        or not isinstance(alias_keys, list)
        or not primary_keys
        or not alias_keys
        or any(not isinstance(key, str) or not FIXTURE_KEY_RE.fullmatch(key) for key in primary_keys)
        or any(not isinstance(key, str) or not FIXTURE_KEY_RE.fullmatch(key) for key in alias_keys)
        or len(set(primary_keys)) != len(primary_keys)
        or len(set(alias_keys)) != len(alias_keys)
        or set(primary_keys) & set(alias_keys)
    ):
        raise ValueError("causal terminal fixture-key inventory is invalid")
    terminal = {
        "schema_version": 1,
        "terminal_class": CAUSAL_CAPTURE_CLASS,
        "run_id": run_id,
        "attempt": attempt,
        "pr": pr,
        "head_sha": head,
        "run_head_sha": head,
        "current_pr_head_sha": head,
        "current_for_pr": True,
        "primary_keys": sorted(primary_keys),
        "alias_keys": sorted(alias_keys),
    }
    terminal["terminal_sha256"] = hashlib.sha256(
        json.dumps(terminal, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return terminal


def validate(
    verdict: dict[str, Any], *, pr: int, run_id: int, run: dict[str, Any], pull: dict[str, Any]
) -> tuple[int, str]:
    attempt = current_attempt(run)
    run_sha = str(run.get("head_sha") or "")
    pr_sha = str(((pull.get("head") or {}).get("sha")) or "")
    if str(verdict.get("run_id")) != str(run_id):
        raise ValueError("verdict run_id does not match requested run")
    if int(verdict.get("attempt") or 0) != attempt:
        raise ValueError("verdict attempt does not match current run attempt")
    if int(verdict.get("pr") or 0) != pr:
        raise ValueError("verdict PR does not match requested PR")
    if not run_sha or run_sha != pr_sha:
        raise ValueError("run is not for the current PR head")
    if normalize_sha(verdict) != run_sha:
        raise ValueError("verdict SHA does not match run/current PR head")
    if verdict.get("current_for_pr") not in (True, "true"):
        raise ValueError("verdict is not classified current_for_pr=true")
    _causal_terminal_from_verdict(
        verdict, pr=pr, run_id=run_id, attempt=attempt, head=run_sha
    )
    return attempt, run_sha


def capture_success_verdict(
    *,
    run: dict[str, Any],
    pull: dict[str, Any],
    dispatch_receipt: dict[str, Any],
    pr: int,
    run_id: int,
    capture_profile: str,
    repo: str = "heydonna-app/heydonna-app",
) -> dict[str, Any]:
    """Adopt one completed capture run into the existing PR verdict record.

    The workflow already owns fixture generation and promotion.  This adapter
    only turns its authenticated, exact-head success into the comment schema
    consumed by the existing PM sweep; it never treats a caller-authored
    success flag or prose as evidence.
    """
    if capture_profile != "full":
        raise ValueError("capture-success adoption currently requires the full profile")
    if (
        run.get("databaseId") != run_id
        or run.get("repository") != repo
        or run.get("workflowName") != CAPTURE_WORKFLOW_NAME
        or run.get("event") != "workflow_dispatch"
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
    ):
        raise ValueError("capture run is not a completed successful manual capture")
    head = str(run.get("headSha") or "")
    branch = str(run.get("headBranch") or "")
    if not SHA_RE.fullmatch(head) or not branch:
        raise ValueError("capture run head or branch is malformed")
    expected_title = f"remote-capture-pr-{pr}-head-{head}"
    if run.get("displayTitle") != expected_title:
        raise ValueError("capture run display title is not the exact PR/head tuple")
    pull_head = (pull.get("head") or {}) if isinstance(pull, dict) else {}
    if (
        pull.get("number") != pr
        or pull_head.get("sha") != head
        or pull_head.get("ref") != branch
    ):
        raise ValueError("capture run does not match the current PR head")
    run_pulls = run.get("pull_requests")
    if not isinstance(run_pulls, list) or not any(
        item.get("number") == pr
        and (item.get("head") or {}).get("sha") == head
        and (item.get("head") or {}).get("ref") == branch
        for item in run_pulls
        if isinstance(item, dict)
    ):
        raise ValueError("capture run PR membership is not exact")

    jobs = [job for job in run.get("jobs") or [] if job.get("name") == "e2e-capture"]
    if len(jobs) != 1 or jobs[0].get("conclusion") != "success":
        raise ValueError("capture run has no unique successful capture job")
    outcomes = {
        str(step.get("name") or ""): str(step.get("conclusion") or "")
        for step in jobs[0].get("steps") or []
    }
    missing = sorted(name for name in CAPTURE_REQUIRED_STEPS if outcomes.get(name) != "success")
    if missing or outcomes.get(CAPTURE_PROMOTION_STEP) != "success":
        raise ValueError("capture fixture promotion/readability steps did not pass")

    if not isinstance(dispatch_receipt, dict):
        raise ValueError("capture dispatch receipt is not an object")
    if (
        dispatch_receipt.get("schema_version") != 1
        or dispatch_receipt.get("repository", repo) != repo
        or dispatch_receipt.get("pr") != pr
        or dispatch_receipt.get("head_sha") != head
        or dispatch_receipt.get("head_branch") != branch
        or dispatch_receipt.get("run_id") != run_id
        or dispatch_receipt.get("workflow_ref") != branch
        or dispatch_receipt.get("capture_profile", "full") != capture_profile
        or not SHA_RE.fullmatch(str(dispatch_receipt.get("control_sha") or ""))
    ):
        raise ValueError("capture dispatch receipt is not the exact trusted tuple")
    control_sha = str(dispatch_receipt["control_sha"])
    return {
        "schema_version": 3,
        "verdict": "GREEN",
        "severity": "GREEN",
        "classification": "capture-required",
        "requested_owner_action": "consume-capture-success",
        "blocking_for_merge": True,
        "required_check_failure": True,
        "pr": pr,
        "run_id": str(run_id),
        "attempt": view_attempt(run),
        "sha": head,
        "run_head_sha": head,
        "current_pr_head_sha": head,
        "current_for_pr": True,
        "branch": branch,
        "workflow_name": CAPTURE_WORKFLOW_NAME,
        "workflow_event": "workflow_dispatch",
        "capture_profile": capture_profile,
        "capture_conclusion": "success",
        "fixture_promotion": {
            "status": "success",
            "step": CAPTURE_PROMOTION_STEP,
            "readability_verified": True,
        },
        "trusted_control_sha": control_sha,
        "watch_runs": [str(run_id)],
        "source_capture_run": str(run_id),
    }


def capture_success_report(verdict: dict[str, Any]) -> str:
    """Render deterministic prose from the validated run, never from prose input."""
    return (
        "Exact-head remote capture completed successfully; fixture promotion and "
        "readability checks passed. The existing PM capture gate may consume "
        f"run {verdict['run_id']} once for head {verdict['run_head_sha']}."
    )


def verdict_from_investigation(
    packet: dict[str, Any],
    *,
    pr: int,
    run_id: int,
    run: dict[str, Any],
    pull: dict[str, Any],
) -> dict[str, Any]:
    """Materialize the canonical rerun verdict from one PM investigation packet.

    The investigation agent is read-only; its packet is not itself a durable
    PR verdict.  This adapter is deliberately narrow and still binds the
    packet to the live run/attempt/PR head before returning a verdict for the
    existing publisher.  The legacy ``--verdict`` path remains unchanged.
    """
    if not isinstance(packet, dict):
        raise ValueError("investigation packet must be a JSON object")
    packet_pr = packet.get("pr")
    packet_run = packet.get("run_id")
    if str(packet_pr) != str(pr):
        raise ValueError("investigation packet PR does not match requested PR")
    if str(packet_run) != str(run_id):
        raise ValueError("investigation packet run_id does not match requested run")

    supplied_heads = [
        str(packet.get(key) or "").strip()
        for key in ("sha", "run_head_sha", "head_sha", "current_pr_head_sha")
        if packet.get(key) not in (None, "")
    ]
    if supplied_heads and any(value != supplied_heads[0] for value in supplied_heads):
        raise ValueError("investigation packet contains conflicting head identities")

    attempt, head = validate(
        {
            "run_id": run_id,
            "attempt": packet.get("attempt"),
            "pr": pr,
            "sha": packet.get("sha") or packet.get("head_sha") or packet.get("run_head_sha"),
            "current_for_pr": packet.get("current_for_pr"),
        },
        pr=pr,
        run_id=run_id,
        run=run,
        pull=pull,
    )
    workflow = str(run.get("name") or run.get("workflow_name") or "").strip()
    event = str(run.get("event") or "").strip()
    if workflow not in {"CI", "E2E Smoke Tests"}:
        raise ValueError("investigation run workflow is not label-gated CI/E2E")
    if event != "pull_request":
        raise ValueError("investigation run event is not pull_request")
    if str(run.get("conclusion") or "").lower() not in {
        "failure",
        "timed_out",
        "cancelled",
    }:
        raise ValueError("investigation packet requires a terminal-bad run")

    classification = str(packet.get("classification") or "").strip().lower()
    if classification not in {"infra", *RERUNNABLE_TEST_CLASS_ALIASES}:
        raise ValueError("investigation classification must be INFRA or TEST")
    if classification in RERUNNABLE_TEST_CLASS_ALIASES:
        classification = RERUNNABLE_TEST_CLASS
    local_result = str(packet.get("local_repro_result") or "").strip().lower()
    local_evidence = " ".join(
        str(packet.get(key) or "").strip().lower()
        for key in ("local_repro_gate", "local_proof", "local_repro_reason")
    )
    no_local_equivalent = (
        local_result.replace("-", "_") in {"no_local_equivalent", "impossible"}
        or "no-local-equivalent" in local_evidence
        or "no_local_equivalent" in local_evidence
    )
    if classification == "infra" and not no_local_equivalent:
        raise ValueError(
            "investigation local result must be NO_LOCAL_EQUIVALENT"
        )
    if classification == RERUNNABLE_TEST_CLASS and local_result != "passed":
        raise ValueError(
            "test investigation local result must be PASSED"
        )
    requested_action = str(packet.get("requested_owner_action") or "").strip()
    requested_action_normalized = requested_action.lower()
    if requested_action_normalized not in {
        "",
        "none",
        "rerun-after-proof",
        "none (infra, no product surface)",
    }:
        raise ValueError(
            "investigation packet must request rerun-after-proof"
        )
    recommendation = str(packet.get("pm_transition_recommendation") or "").lower()
    if classification == RERUNNABLE_TEST_CLASS and (
        "unchanged-head rerun" not in recommendation
        or any(term in recommendation for term in ("slot", "capture", "product rework"))
    ):
        raise ValueError("test investigation lacks sanctioned rerun recommendation")
    if requested_action_normalized != "rerun-after-proof" and (
        (
            "unchanged-head rerun" not in recommendation
            and not (
                requested_action_normalized
                in {"none", "none (infra, no product surface)"}
                and classification == "infra"
                and no_local_equivalent
            )
        )
        or any(term in recommendation for term in ("slot", "capture", "product rework"))
    ):
        raise ValueError("investigation packet lacks sanctioned rerun recommendation")
    if packet.get("blocking_for_merge") not in (True, "true"):
        raise ValueError("investigation packet must mark blocking_for_merge=true")
    if packet.get("required_check_failure") not in (True, "true"):
        raise ValueError("investigation packet must mark required_check_failure=true")
    if not str(
        packet.get("first_causal_boundary")
        or packet.get("root_cause")
        or packet.get("causal_fingerprint")
        or ""
    ).strip():
        raise ValueError("investigation packet is missing first causal boundary")

    verdict = dict(packet)
    verdict.update(
        {
            "schema_version": max(int(packet.get("schema_version") or 1), 3),
            "run_id": run_id,
            "attempt": attempt,
            "pr": pr,
            "sha": head,
            "run_head_sha": head,
            "current_pr_head_sha": head,
            "current_for_pr": True,
            "workflow": workflow,
            "workflow_name": workflow,
            "event": event,
            "workflow_event": event,
            "run_attempt": attempt,
            "rerun_authorization": {
                "action": "rerun-after-proof",
                "run_id": str(run_id),
                "attempt": attempt,
                "head_sha": head,
                "single_use": True,
            },
            "verdict": "GREY",
            "severity": "GREY",
            "classification": classification,
            "local_repro_result": (
                "impossible" if classification == "infra" else "passed"
            ),
            "local_repro_reason": (
                "NO_LOCAL_EQUIVALENT"
                if classification == "infra"
                else str(
                    packet.get("local_repro_reason")
                    or "IN_RUN_RETRY_RECOVERED"
                )
            ),
            "requested_owner_action": "rerun-after-proof",
            "blocking_for_merge": True,
            "required_check_failure": True,
        }
    )
    return verdict


def render_comment(report: str, verdict: dict[str, Any], run_id: int, attempt: int, head: str) -> str:
    compact = json.dumps(verdict, sort_keys=True, separators=(",", ":"))
    causal = _causal_terminal_from_verdict(
        verdict,
        pr=int(verdict.get("pr") or 0),
        run_id=run_id,
        attempt=attempt,
        head=head,
    )
    causal_compact = (
        json.dumps(causal, sort_keys=True, separators=(",", ":"))
        if causal is not None
        else ""
    )
    causal_marker = (
        f"<!-- ci-causal-terminal: {causal_compact} -->\n"
        if causal is not None
        else ""
    )
    return (
        "## CI failure investigation verdict\n\n"
        f"<!-- ci-failure-investigation:run={run_id} attempt={attempt} head={head} -->\n"
        f"{causal_marker}"
        f"<!-- ci-verdict: {compact} -->\n\n"
        f"{report.rstrip()}\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--verdict", type=Path)
    parser.add_argument(
        "--investigation-packet",
        type=Path,
        help="PM investigation packet to materialize into the canonical verdict",
    )
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--capture-success",
        action="store_true",
        help="adopt one validated successful full capture run into the capture verdict record",
    )
    parser.add_argument(
        "--dispatch-receipt",
        type=Path,
        help="exact wrapper dispatch receipt that binds the capture control SHA",
    )
    parser.add_argument("--capture-profile", default="full")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and print the capture verdict without touching PR comments",
    )
    parser.add_argument("--repo", default="heydonna-app/heydonna-app")
    parser.add_argument(
        "--quarantine-store",
        type=Path,
        default=Path("/tmp/heydonna-ci-flake-quarantine.json"),
    )
    args = parser.parse_args()

    run = gh_json(["api", f"repos/{args.repo}/actions/runs/{args.run_id}"])
    pull = gh_json(["api", f"repos/{args.repo}/pulls/{args.pr}"])
    if args.capture_success:
        if args.verdict is not None or args.investigation_packet is not None:
            parser.error("capture-success cannot be combined with a verdict packet")
        if args.dispatch_receipt is None:
            parser.error("capture-success requires --dispatch-receipt")
        run_view = gh_json(
            [
                "run",
                "view",
                str(args.run_id),
                "--repo",
                args.repo,
                "--json",
                "databaseId,workflowName,displayTitle,event,headBranch,headSha,status,conclusion,attempt,jobs",
            ]
        )
        run_view["pull_requests"] = run.get("pull_requests")
        run_view["repository"] = (run.get("repository") or {}).get("full_name")
        dispatch_receipt = json.loads(
            args.dispatch_receipt.read_text(encoding="utf-8")
        )
        verdict = capture_success_verdict(
            run=run_view,
            pull=pull,
            dispatch_receipt=dispatch_receipt,
            pr=args.pr,
            run_id=args.run_id,
            capture_profile=args.capture_profile,
            repo=args.repo,
        )
        args.verdict = args.dispatch_receipt.with_name(
            f"ci-verdict-{args.run_id}.json"
        )
        args.verdict.write_text(
            json.dumps(verdict, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        report = capture_success_report(verdict)
    elif (args.verdict is None) == (args.investigation_packet is None):
        parser.error("provide exactly one of --verdict or --investigation-packet")
    else:
        if args.report is None:
            parser.error("--report is required for an investigation verdict")
        report = args.report.read_text(encoding="utf-8")
        if not report.strip():
            raise ValueError("investigation report is missing or empty")
    if args.investigation_packet is not None:
        packet = json.loads(args.investigation_packet.read_text(encoding="utf-8"))
        verdict = verdict_from_investigation(
            packet, pr=args.pr, run_id=args.run_id, run=run, pull=pull
        )
        args.verdict = args.investigation_packet.with_name(
            f"ci-verdict-{args.run_id}.json"
        )
        args.verdict.write_text(
            json.dumps(verdict, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    else:
        verdict = json.loads(args.verdict.read_text(encoding="utf-8"))
    attempt, head = validate(verdict, pr=args.pr, run_id=args.run_id, run=run, pull=pull)
    if args.dry_run:
        if not args.capture_success:
            parser.error("--dry-run is supported only with --capture-success")
        print(
            json.dumps(
                {
                    "ok": True,
                    "admission": "capture-success",
                    "pr": args.pr,
                    "run_id": args.run_id,
                    "attempt": attempt,
                    "head": head,
                    "capture_profile": verdict["capture_profile"],
                    "trusted_control_sha": verdict["trusted_control_sha"],
                    "watch_runs": verdict["watch_runs"],
                },
                sort_keys=True,
            )
        )
        return 0
    body = render_comment(report, verdict, args.run_id, attempt, head)
    marker = f"ci-failure-investigation:run={args.run_id} attempt={attempt} head={head}"

    comments = gh_json(
        ["api", f"repos/{args.repo}/issues/{args.pr}/comments?per_page=100"]
    ) or []
    quarantine = (
        {"quarantine_blocked": False, "quarantine_reason": None, "prior_occurrences": 0}
        if args.capture_success
        else quarantine_state(verdict, prior_comments=comments, store_path=args.quarantine_store)
    )
    verdict["quarantine_blocked"] = quarantine["quarantine_blocked"]
    verdict["quarantine_reason"] = quarantine["quarantine_reason"]
    verdict["quarantine_prior_occurrences"] = quarantine["prior_occurrences"]
    existing = next((item for item in reversed(comments) if marker in str(item.get("body") or "")), None)
    if existing:
        result = gh_json(
            [
                "api",
                f"repos/{args.repo}/issues/comments/{existing['id']}",
                "-X",
                "PATCH",
                "--input",
                "-",
            ],
            input_text=json.dumps({"body": body}),
        )
        action = "updated"
    else:
        result = gh_json(
            [
                "api",
                f"repos/{args.repo}/issues/{args.pr}/comments",
                "-X",
                "POST",
                "--input",
                "-",
            ],
            input_text=json.dumps({"body": body}),
        )
        action = "created"

    verdict["pr_comment_status"] = "posted"
    verdict["pr_comment_url"] = result.get("html_url")
    final_body = render_comment(report, verdict, args.run_id, attempt, head)
    result = gh_json(
        [
            "api",
            f"repos/{args.repo}/issues/comments/{result['id']}",
            "-X",
            "PATCH",
            "--input",
            "-",
        ],
        input_text=json.dumps({"body": final_body}),
    )
    args.verdict.write_text(json.dumps(verdict, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    attempt_path = args.verdict.with_name(f"ci-verdict-{args.run_id}-attempt-{attempt}.json")
    if attempt_path != args.verdict:
        attempt_path.write_text(json.dumps(verdict, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    json.dump(
        {
            "ok": True,
            "action": action,
            "pr": args.pr,
            "run_id": args.run_id,
            "attempt": attempt,
            "head": head,
            "url": result.get("html_url"),
        },
        sys.stdout,
        sort_keys=True,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
