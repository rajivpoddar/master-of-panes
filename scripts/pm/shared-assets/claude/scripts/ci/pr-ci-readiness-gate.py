#!/usr/bin/env python3
"""Fail-closed gate before PM starts label-gated CI for a PR head.

This gate is intentionally narrower than merge readiness. It answers only:
"is this head safe to mark `pm-state:qa-passed-awaiting-ci`, which starts CI?"
It does not require CI/E2E to have passed yet.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from control_plane_issue_policy import validate_live_followup_issue
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from control_plane_issue_policy import validate_live_followup_issue


REPO = "heydonna-app/heydonna-app"
REMOTE_CAPTURE_ONLY = os.environ.get("REMOTE_CAPTURE_ONLY", "1").lower() in {"1", "true", "enabled"}
CAPTURE_REQUIRED = Path("/Users/rajiv/.claude/scripts/capture-required.py")
DEFAULT_MIN_HEAD_AGE_MINUTES = 5

# CI-start safety allowlist (Rajiv release-policy simplification, thread
# 1786636554.182149 ts 1786641626.977319). The initial/current-head CI+E2E
# wave requires ONLY exact PR/head binding and per-PR single-flight /
# no-duplicate-active-wave. Every other admission reason (affected-test
# planner receipts, capture receipts, PR-bound review markers, stale-run
# verdict packets, local-preflight seals, override packets, reservation/epoch
# evidence, QA visual proof, label blockers, head-age, merge-state)
# is optional ceremony and may remain a diagnostic warning but must not block
# CI start. Functional-red same-head start stays refused (the owning slot
# reproduces locally and pushes a descendant head); infra/runner/flake
# same-head reruns stay capped at one via the off-slot rerun wrapper.
CI_START_SAFETY_REASON_PREFIXES = (
    "head_drift",
    "pr_not_open",
    "current_head_ci_or_e2e_already_in_progress",
    "current_head_ci_or_e2e_already_terminal_green",
    "current_head_ci_or_e2e_partial_existing_runs",
    "current_head_ci_or_e2e_skipped_existing_runs",
    "current_head_ci_verdict_requires_rework",
    "current_head_ci_or_e2e_failed_use_rerun_not_label_trigger",
    "current_head_ci_or_e2e_cancelled_requires_investigation",
)


def change_scope_script() -> Path:
    override = os.environ.get("HEYDONNA_CHANGE_SCOPE")
    candidates = [
        Path(override) if override else None,
        Path(__file__).resolve().parents[2] / "ci" / "change_scope.py",
        Path(__file__).resolve().parents[2] / "scripts" / "ci" / "change_scope.py",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate
    raise FileNotFoundError("repository change_scope.py is unavailable")


def qa_visual_proof_gate_script() -> Path:
    override = os.environ.get("HEYDONNA_QA_VISUAL_PROOF_GATE")
    candidates = [
        Path(override) if override else None,
        Path(__file__).resolve().with_name("qa-visual-proof-gate.py"),
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "pm"
        / "control-plane"
        / "qa-visual-proof-gate.py",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate
    raise FileNotFoundError("qa-visual-proof-gate.py is unavailable")

REMOTE_CAPTURE_RUN_VALIDATOR = Path("/Users/rajiv/Downloads/projects/heydonna-app/scripts/ci/remote-capture-run.py")
CANONICAL_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
CAPTURE_REQUIREMENT_CACHE_DIR = Path(
    os.environ.get("CAPTURE_REQUIREMENT_CACHE_DIR", "/tmp")
)
CAPTURE_REQUIREMENT_CACHE_MAX_AGE_SECONDS = 300
def required_ci_jobs() -> dict[str, str]:
    override = os.environ.get("REQUIRED_CI_JOBS_FILE")
    candidates = [
        Path(override) if override else None,
        Path(__file__).resolve().parents[2] / "ci" / "required-ci-jobs.json",
        Path(__file__).resolve().parent / "ci" / "required-ci-jobs.json",
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(value, dict) and all(
            isinstance(workflow, str)
            and workflow
            and isinstance(job, str)
            and job
            for workflow, job in value.items()
        ):
            return value
    return {}


REQUIRED_CI_JOBS = required_ci_jobs()
REQUIRED_WORKFLOWS = set(REQUIRED_CI_JOBS)
BAD_TERMINAL_CONCLUSIONS = {"failure", "cancelled", "timed_out", "action_required"}
BLOCKING_LABELS = {
    "pm-state:blocked-rework",
    "pm-state:rescope-required",
    "pm-blocked:ci",
    "pm-blocked:capture",
    "pm-blocked:codex",
    "pm-blocked:rebase",
    "pm-blocked:pm-gate",
    "pm-blocked:dependency",
    "pm-blocked:product",
    "pm-blocked:infra",
}
TRANSITION_OWNED_LABELS = {
    "pm-state:blocked-rework",
    "pm-state:rescope-required",
    "pm-blocked:pm-gate",
}
RESCUE_TRANSITION_OWNED_LABELS = {
    "pm-state:blocked-rework",
    "pm-blocked:ci",
    "pm-blocked:codex",
}
TRANSITION_SOURCES = {"slot-ready", "slot-ready-rescue", "pm-review-done"}
# Admitted transition sources for the explicit one-time CI-start override
# lane. The canonical slot-ready transition may start genuine CI/E2E with an
# exact-head CTO override under the SAME full packet checks as pm-review-done
# (the override is revalidated end-to-end by the class validator and consumed
# atomically on the final label-control gate call). Every other source —
# including slot-ready-rescue and manual — fails closed.
CI_START_OVERRIDE_SOURCES = frozenset({"pm-review-done", "slot-ready"})


def run(cmd: list[str], timeout: int = 20) -> str:
    env = os.environ.copy()
    env["PATH"] = f"{CANONICAL_PATH}:{env.get('PATH', '')}"
    if cmd and cmd[0] == "gh":
        gh = shutil.which("gh", path=env["PATH"])
        if not gh:
            raise RuntimeError("gh not found on canonical PATH")
        cmd = [gh, *cmd[1:]]
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, env=env)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or f"{cmd[0]} failed").strip())
    return proc.stdout


def exact_head_change_scope(pr: int, head: str, repo: str) -> dict[str, Any]:
    """Classify the live PR through the repository-owned exact-head contract."""
    classifier = change_scope_script()
    output = run(
        [
            "python3",
            str(classifier),
            "--repo-root",
            str(Path(__file__).resolve().parents[3]),
            "--repo",
            repo,
            "--pr",
            str(pr),
            "--expected-head",
            head,
        ],
        timeout=90,
    )
    result = json.loads(output)
    if result.get("head") != head:
        raise RuntimeError("change-scope result is not bound to the live head")
    if not re.fullmatch(r"[0-9a-f]{64}", str(result.get("rules_sha256") or "")):
        raise RuntimeError("change-scope rules digest is missing")
    result["classifier_sha256"] = hashlib.sha256(classifier.read_bytes()).hexdigest()
    return result


def exact_head_qa_visual_proof(pr: int, head: str, repo: str) -> dict[str, Any]:
    gate = qa_visual_proof_gate_script()
    proc = subprocess.run(
        [
            sys.executable,
            str(gate),
            "--pr",
            str(pr),
            "--repo",
            repo,
            "--expect-head",
            head,
            "--json",
        ],
        text=True,
        capture_output=True,
        timeout=90,
        env={**os.environ, "PATH": f"{CANONICAL_PATH}:{os.environ.get('PATH', '')}"},
    )
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("qa visual proof gate returned invalid JSON") from exc
    if result.get("head_sha") and result.get("head_sha") != head:
        raise RuntimeError("qa visual proof result is not bound to the live head")
    if proc.returncode and result.get("ok") is not False:
        raise RuntimeError("qa visual proof gate failed without a blocking receipt")
    return result


def gh_json(args: list[str], timeout: int = 20) -> Any:
    return json.loads(run(["gh", *args], timeout=timeout) or "null")


def labels(pr: dict[str, Any]) -> list[str]:
    return [str(item.get("name") or "") for item in pr.get("labels") or [] if item.get("name")]


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def capture_run_success(
    run_id: str,
    repo: str,
    *,
    expected_pr: int | None = None,
    expected_head: str = "",
    expected_branch: str = "",
) -> tuple[bool, dict[str, Any]]:
    """Capture is green only when the exact-head workflow and required steps pass."""
    if not run_id or expected_pr is None or not expected_head:
        return False, {}
    try:
        detail = gh_json([
            "run", "view", run_id, "--repo", repo, "--json",
            "displayTitle,status,conclusion,event,workflowName,jobs,url",
        ], timeout=20)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", encoding="utf-8") as run_json:
            json.dump(detail, run_json)
            run_json.flush()
            run([
                sys.executable, str(REMOTE_CAPTURE_RUN_VALIDATOR), "validate",
                "--run-json", run_json.name, "--pr", str(expected_pr), "--head", expected_head,
            ], timeout=10)
    except Exception as exc:
        return False, {"capture_run_conclusion": "", "detail": str(exc)}
    return True, {"capture_run_conclusion": "success", "url": detail.get("url")}


def extract_ci_verdict(text: str) -> dict[str, Any] | None:
    match = re.search(r"<!--\s*ci-verdict:\s*(.*?)-->", text or "", re.S | re.I)
    if not match:
        return None
    try:
        value = json.loads(match.group(1).strip())
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def verdict_head(verdict: dict[str, Any]) -> str:
    return str(
        verdict.get("current_pr_head_sha")
        or verdict.get("current_for_pr_head_sha")
        or verdict.get("run_head_sha")
        or verdict.get("head_sha")
        or verdict.get("sha")
        or ""
    )


def nonblocking_concurrency_cancel(verdict: dict[str, Any]) -> bool:
    """Return true only for a durably closed, non-product cancellation."""
    blocking = verdict.get("blocking_for_merge")
    if isinstance(blocking, str):
        blocking = blocking.lower() == "true"
    return bool(
        str(verdict.get("classification") or "").lower() == "concurrency-cancel"
        and blocking is False
        and str(verdict.get("local_repro_result") or "").lower() == "not-applicable"
        and str(verdict.get("pm_action_status") or "").lower()
        in {"executed", "not-required"}
    )


def latest_current_head_ci_verdict(pr_number: int, head: str, repo: str) -> dict[str, Any]:
    try:
        data = gh_json(
            ["pr", "view", str(pr_number), "--repo", repo, "--json", "comments"],
            timeout=20,
        )
    except Exception:
        return {}
    comments = sorted(
        data.get("comments") or [],
        key=lambda item: parse_time(item.get("createdAt")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    for comment in comments:
        verdict = extract_ci_verdict(str(comment.get("body") or ""))
        if not verdict:
            continue
        if str(verdict.get("pr") or "") and str(verdict.get("pr")) != str(pr_number):
            continue
        v_head = verdict_head(verdict)
        if head and v_head and v_head != head:
            continue
        if head and not v_head:
            continue
        verdict["comment_created_at"] = comment.get("createdAt") or ""
        verdict["comment_url"] = comment.get("url") or ""
        return verdict
    return {}


def current_head_ci_verdicts(
    pr_number: int, head: str, repo: str
) -> dict[str, dict[str, Any]]:
    """Index durable current-head CI verdicts by workflow run ID.

    The promotion gate must consume a closed, nonblocking concurrency-cancel
    receipt before deciding that a newer cancelled shell shadows an older real
    required-job result.  Keeping the index run-scoped prevents a verdict for
    one workflow from accidentally clearing a different workflow's failure.
    """
    if not pr_number or not head:
        return {}
    try:
        data = gh_json(
            ["pr", "view", str(pr_number), "--repo", repo, "--json", "comments"],
            timeout=20,
        )
    except Exception:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for comment in data.get("comments") or []:
        verdict = extract_ci_verdict(str(comment.get("body") or ""))
        if not verdict or verdict_head(verdict) != head:
            continue
        if verdict.get("pr") is not None and str(verdict.get("pr")) != str(pr_number):
            continue
        run_id = str(verdict.get("run_id") or "")
        if run_id.isdigit():
            result[run_id] = verdict
    return result


def ci_verdict_requires_rework(verdict: dict[str, Any]) -> bool:
    if not verdict:
        return False
    classification = str(verdict.get("classification") or "").lower()
    if classification in {
        "generated-metadata-key-drift",
        "prompt-induced-output-regression",
        "real-regression",
        "pr-introduced-regression",
    }:
        return True
    recapture_fixes = verdict.get("recapture_fixes_it")
    if isinstance(recapture_fixes, bool) and not recapture_fixes:
        return True
    if isinstance(recapture_fixes, str) and recapture_fixes.strip().lower() in {"false", "no", "n"}:
        return True
    rerun_permitted = verdict.get("rerun_permitted")
    if isinstance(rerun_permitted, bool) and not rerun_permitted:
        return True
    if isinstance(rerun_permitted, str) and rerun_permitted.strip().lower() in {"false", "no", "n"}:
        return True
    text = " ".join(
        str(verdict.get(key) or "")
        for key in ("recommended_next_action", "action", "terminal_state", "stage_detail")
    ).lower()
    return "recapture won't fix" in text or "no rerun" in text or "slot rework" in text


def latest_head_timestamp(pr: dict[str, Any]) -> tuple[datetime | None, str]:
    latest: datetime | None = None
    for item in pr.get("commits") or []:
        if not isinstance(item, dict):
            continue
        candidates = [
            item.get("committedDate"),
            item.get("pushedDate"),
            item.get("authoredDate"),
        ]
        nested = item.get("commit")
        if isinstance(nested, dict):
            candidates.extend(
                [
                    nested.get("committedDate"),
                    nested.get("pushedDate"),
                    nested.get("authoredDate"),
                ]
            )
        for value in candidates:
            ts = parse_time(str(value) if value else None)
            if ts and (latest is None or ts > latest):
                latest = ts
    if latest:
        return latest, "latest_commit"
    updated = parse_time(pr.get("updatedAt"))
    if updated:
        return updated, "pr_updatedAt_fallback"
    return None, "unknown"


def moving_head_recent_push_reason(
    head_age_minutes: int,
    min_head_age_minutes: int,
    source: str,
) -> str | None:
    if min_head_age_minutes <= 0 or head_age_minutes >= min_head_age_minutes:
        return None
    return (
        f"moving_head_recent_push_age_min={head_age_minutes} "
        f"min={min_head_age_minutes} source={source}"
    )


def followup_issue_number(text: str) -> int | None:
    match = re.search(
        r"^(?:flake_followup|followup_issue|follow_up_issue|preexisting_followup|"
        r"pre_existing_followup|follow-up):\s*(?:#|https://github\.com/[^ \n]+/issues/)(\d+)\s*$",
        text,
        re.M | re.I,
    )
    return int(match.group(1)) if match else None


def proof_has_followup_issue(text: str) -> bool:
    return followup_issue_number(text) is not None


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)


def playwright_failure_tests(text: str) -> tuple[int, list[str]]:
    clean = strip_ansi(text)
    counts = [int(value) for value in re.findall(r"^\s*(\d+)\s+failed(?:\s|$)", clean, re.M)]
    entries: list[tuple[str, str]] = []
    for match in re.finditer(r"^\s*\d+\)\s+(\[[^\]]+\])\s+›\s+(.+?)\s*$", clean, re.M):
        project = " ".join(match.group(1).split())
        name = " ".join(match.group(2).split())
        if project and name:
            entries.append((project, name))
    duplicate_names = {name for _, name in entries if sum(entry[1] == name for entry in entries) > 1}
    tests: list[str] = []
    for project, name in entries:
        value = f"{project} › {name}" if name in duplicate_names else name
        if value and value not in tests:
            tests.append(value)
    return (counts[-1] if counts else 0), tests


def failure_signature(test_name: str) -> str:
    return "sha256:" + hashlib.sha256(test_name.encode("utf-8")).hexdigest()


def failed_preflight_receipt(path: Path, head: str) -> tuple[bool, str, list[str]]:
    expected_dir = Path("/tmp/affected-test-preflight-receipts").resolve()
    try:
        resolved = path.resolve()
        if resolved.parent != expected_dir:
            return False, "override_receipt_path_not_canonical", []
        receipt = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "override_receipt_unreadable", []
    digest = str(receipt.get("commands_sha256") or "")
    if resolved.name != f"{head}-{digest}.json" or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return False, "override_receipt_name_or_command_digest_mismatch", []
    if receipt.get("schema_version") != 1 or receipt.get("producer") != "local-repro-preflight.sh":
        return False, "override_receipt_schema_invalid", []
    if receipt.get("headRefOid") != head:
        return False, "override_receipt_stale_head", []
    if receipt.get("result") != "FAIL" or not isinstance(receipt.get("exit_code"), int) or receipt["exit_code"] == 0:
        return False, "override_receipt_not_terminal_failure", []
    contract = receipt.get("command_contract") or {}
    if contract.get("mode") not in {"full", "spec"}:
        return False, "override_receipt_command_contract_invalid", []
    log_path = Path(str(receipt.get("log_path") or ""))
    try:
        log_bytes = log_path.read_bytes()
    except OSError:
        return False, "override_receipt_log_missing", []
    if hashlib.sha256(log_bytes).hexdigest() != receipt.get("log_sha256"):
        return False, "override_receipt_log_digest_mismatch", []
    failure_count, failures = playwright_failure_tests(log_bytes.decode("utf-8", errors="replace"))
    if failure_count < 1 or len(failures) != failure_count:
        return False, "override_receipt_failures_not_fully_parsed", []
    return True, "ok", failures


def followup_issue_contract_ok(issue: int, pr: int, signatures: list[str], repo: str) -> tuple[bool, str]:
    try:
        data = gh_json(
            ["issue", "view", str(issue), "--repo", repo, "--json", "number,state,body,url"]
        )
    except Exception:
        return False, "override_followup_unavailable"
    if data.get("state") != "OPEN":
        return False, "override_followup_not_open"
    body = str(data.get("body") or "")
    if not re.search(rf"^source_pr:\s*#{pr}\s*$", body, re.M | re.I):
        return False, "override_followup_wrong_source_pr"
    recorded = set(re.findall(r"^failure_signature:\s*(sha256:[0-9a-f]{64})\s*$", body, re.M | re.I))
    if recorded != set(signatures):
        return False, "override_followup_failure_signature_mismatch"
    return True, "ok"


def proof_pass_status(text: str, prefix: str) -> str:
    match = re.search(
        rf"^{prefix}:\s*(PASS|PASS_WITH_PREEXISTING_FAILURES|DOCS_ONLY|TARGETED_CI_PASS|LOCAL_AUTH_E2E_DEFERRED_TO_CI|LOCAL_SEED_ADMISSION_E2E_DEFERRED_TO_CI|NO_LOCAL_EQUIVALENT)(?:\s|$)",
        text,
        re.M,
    )
    return match.group(1) if match else ""


def proof_status_allowed(status: str, text: str) -> bool:
    if status in {"PASS", "DOCS_ONLY"}:
        return True
    if status in {"PASS_WITH_PREEXISTING_FAILURES", "NO_LOCAL_EQUIVALENT"}:
        return proof_has_followup_issue(text)
    return False


def targeted_ci_proof_ok(text: str, pr: int, head: str, repo: str) -> bool:
    run_match = re.search(r"^targeted_ci_run:\s*(\d+)\s*$", text, re.M)
    proof_head = re.search(r"^targeted_ci_head:\s*(\S+)\s*$", text, re.M)
    if not run_match or not proof_head or proof_head.group(1) != head:
        return False
    if not re.search(r"^targeted_ci_conclusion:\s*success\s*$", text, re.M):
        return False
    if not re.search(r"^targeted_ci_event:\s*pull_request\s*$", text, re.M):
        return False
    try:
        run_data = gh_json(
            [
                "run",
                "view",
                run_match.group(1),
                "--repo",
                repo,
                "--json",
                "headSha,conclusion,event,jobs",
            ]
        )
    except Exception:
        return False
    jobs = run_data.get("jobs") or []
    return bool(
        run_data.get("headSha") == head
        and str(run_data.get("conclusion") or "").lower() == "success"
        and run_data.get("event") == "pull_request"
        and any(str(job.get("conclusion") or "").lower() == "success" for job in jobs)
    )


def classify_explicit_ci_start_proof(path_str: str, pr: int, head: str) -> str:
    """Fail-closed classification of an explicit --affected-test-proof path.

    Only the exact one-time override filename is an override; only the exact
    canonical affected-test proof filename uses the ordinary validator; every
    other supplied path is rejected outright.
    """
    try:
        supplied = Path(path_str).resolve()
    except OSError:
        return "rejected"
    if supplied == Path(f"/tmp/pm-ci-start-override-{pr}-{head}.ok").resolve():
        return "override"
    if supplied == Path(f"/tmp/affected-test-proof-{pr}-{head}.ok").resolve():
        return "ordinary"
    return "rejected"


def ci_start_override_proof(path: Path, pr: int, head: str, repo: str = REPO) -> tuple[bool, str]:
    expected = Path(f"/tmp/pm-ci-start-override-{pr}-{head}.ok")
    try:
        if path.resolve() != expected.resolve():
            return False, "override_path_not_exact"
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, "override_unreadable"
    required = (
        f"PR: {pr}",
        f"headRefOid: {head}",
        "no_full_suite: true",
        "provenance: pm-recorded-one-time-gate-exception",
    )
    if any(value not in text for value in required):
        return False, "override_contract_missing"
    issue = followup_issue_number(text)
    if issue is None:
        return False, "override_followup_missing"
    receipt_match = re.search(r"^preflight_receipt:\s*(\S+)\s*$", text, re.M)
    if not receipt_match:
        return False, "override_receipt_missing"
    receipt_ok, receipt_reason, failures = failed_preflight_receipt(Path(receipt_match.group(1)), head)
    if not receipt_ok:
        return False, receipt_reason
    expected_signatures = [failure_signature(value) for value in failures]
    packet_signatures = re.findall(r"^failure_signature:\s*(sha256:[0-9a-f]{64})\s*$", text, re.M | re.I)
    if set(packet_signatures) != set(expected_signatures):
        return False, "override_packet_failure_signature_mismatch"
    followup_ok, followup_reason = followup_issue_contract_ok(issue, pr, expected_signatures, repo)
    if not followup_ok:
        return False, followup_reason
    authorization = re.search(r"^authorization:\s*(.+)$", text, re.M | re.I)
    scope = re.search(r"^scope:\s*(.+)$", text, re.M | re.I)
    if not authorization or "Rajiv CTO decision" not in authorization.group(1):
        return False, "override_cto_authorization_missing"
    if not scope:
        return False, "override_scope_missing"
    scope_text = scope.group(1).lower()
    if not all(term in scope_text for term in ("env_load_differential_pass", "one-time", "ci+e2e", "mandatory")):
        return False, "override_scope_not_fail_closed"
    return True, "ok"


def ci_start_override_class(text: str) -> str:
    """Deterministic class discriminator for the one-time CI-start override file.

    All override classes share the exact canonical override path; the
    provenance line selects the validator. Every packet that does not carry a
    recognized sealed/adjudicated provenance is handled by the unchanged
    pre-existing-failure class, which rejects malformed packets fail-closed.
    """
    provenance = re.search(r"^provenance:\s*(\S+)\s*$", text, re.M | re.I)
    value = (provenance.group(1) if provenance else "").lower()
    if value == SEALED_MUTATION_SCOPE_OVERRIDE_PROVENANCE:
        return "mutation_scope_sealed"
    if value == CTO_ADJUDICATED_CI_ADMISSION_PROVENANCE:
        return "cto_adjudicated_ci_admission"
    if value == CTO_RESCUE_PACKET_CI_ADMISSION_PROVENANCE:
        return "cto_rescue_packet_ci_admission"
    if value == CTO_MARKER_PASS_CI_ADMISSION_PROVENANCE:
        return "cto_marker_pass_ci_admission"
    if value == CTO_NO_PATCH_RESCUE_CI_ADMISSION_PROVENANCE:
        return "cto_no_patch_rescue_ci_admission"
    if value == CTO_EXACT_TUPLE_CI_ADMISSION_PROVENANCE:
        return "cto_exact_tuple_ci_admission"
    if value == CTO_CANCELLED_RUN_LOCAL_PREFLIGHT_CI_ADMISSION_PROVENANCE:
        return "cto_cancelled_run_local_preflight_ci_admission"
    if value == CTO_LOCAL_PREFLIGHT_REBIND_CI_ADMISSION_PROVENANCE:
        return "cto_local_preflight_rebind_ci_admission"
    return "preexisting_failure"


def mutation_scope_sealed_override_proof(
    path: Path,
    pr: int,
    head: str,
    repo: str = REPO,
) -> tuple[bool, str]:
    """Validate the CTO-directed sealed override for the documented
    mutation-scope limitation (planner mutation-RED vacuous; base verification
    passed; no genuine failed-Playwright preflight receipt exists to express).

    The sealed packet binds PR + exact 40-char head, exactly three
    CTO-verified artifact paths with SHA-256 digests (phase-a review marker,
    affected-test base verification log, capture-local proof), an open
    followup issue, the CTO authorization, fail-closed scope terms, and a
    one-time consumption sentinel. Any deviation fails closed.

    The typed slot-execution-bundle variant (incident
    cp-repair:sealed-validator-slot-bundle:7268:2026-08-13) is mutually
    exclusive with the ordinary three-artifact path: when the packet carries a
    sealed_refusal line, the current-head planner log substitution is admitted
    only through the bundle validator below (planner-refusal artifact digest +
    content, no current-head plan/log/.ok on disk at consumption time, four
    raw current-head slot-execution receipts verified against the actual file
    bytes, historical VACUOUS_RED artifact as HISTORY ONLY). A packet without
    a sealed_refusal line takes the unchanged ordinary path.
    """
    expected = Path(f"/tmp/pm-ci-start-override-{pr}-{head}.ok")
    try:
        if path.resolve() != expected.resolve():
            return False, "override_path_not_exact"
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, "override_unreadable"
    # Exact-tuple binding: PR and headRefOid are anchored full-line fields,
    # never substring matches. headRefOid must be the exact lowercase 40-hex
    # head; a suffix (`<head>X`), different-case, or truncated variant is
    # rejected, and the PR field must be the exact PR number.
    pr_match = re.search(r"^PR:\s*(\d+)\s*$", text, re.M)
    if pr_match is None or int(pr_match.group(1)) != pr:
        return False, "sealed_contract_missing"
    head_match = re.search(r"^headRefOid:\s*([0-9a-f]{40})\s*$", text, re.M)
    if head_match is None or head_match.group(1) != head:
        return False, "sealed_contract_missing"
    if (
        f"provenance: {SEALED_MUTATION_SCOPE_OVERRIDE_PROVENANCE}" not in text
        or "no_full_suite: true" not in text
    ):
        return False, "sealed_contract_missing"
    consumed = re.findall(r"^consumed:\s*(\S+)\s*$", text, re.M | re.I)
    if len(consumed) != 1 or consumed[0].lower() != "no":
        return False, "sealed_packet_reuse"
    if re.search(r"^consumed_marker:\s*\S+\s*$", text, re.M | re.I):
        return False, "sealed_packet_reuse"
    issue = followup_issue_number(text)
    if issue is None:
        return False, "sealed_followup_missing"
    try:
        followup = gh_json(
            ["issue", "view", str(issue), "--repo", repo, "--json", "state"]
        )
    except Exception:
        return False, "sealed_followup_unavailable"
    if str(followup.get("state") or "").upper() != "OPEN":
        return False, "sealed_followup_not_open"
    authorization = re.search(r"^authorization:\s*(.+)$", text, re.M | re.I)
    scope = re.search(r"^scope:\s*(.+)$", text, re.M | re.I)
    if not authorization or "Abhijit CTO" not in authorization.group(1):
        return False, "sealed_cto_authorization_missing"
    if not scope:
        return False, "sealed_scope_missing"
    scope_text = scope.group(1).lower()
    if not all(
        term in scope_text
        for term in ("one-time", "ci+e2e", "current-head", "mandatory", "no merge authority")
    ):
        return False, "sealed_scope_not_fail_closed"
    artifacts = re.findall(
        r"^sealed_artifact:\s*(\S+)\s+sha256:([0-9a-f]{64})\s*$", text, re.M | re.I
    )
    refusal_lines = re.findall(
        r"^sealed_refusal:\s*(\S+)\s+sha256:([0-9a-f]{64})\s*$", text, re.M | re.I
    )
    if len(refusal_lines) > 1:
        return False, "sealed_refusal_count_mismatch"
    if refusal_lines:
        return sealed_slot_execution_bundle_proof(
            pr, head, text, artifacts, refusal_lines[0]
        )
    canonical_artifacts = {
        str(Path(f"/tmp/pm-claude-code-review-{pr}-{head}.md").resolve()),
        str(Path(f"/tmp/capture-local-proof-{pr}-{head}.ok").resolve()),
    }
    declared = {str(Path(declared_path).resolve()) for declared_path, _digest in artifacts}
    # Affected-test artifacts are retired optional diagnostics: the sealed
    # class now requires only the review marker + capture proof. A legacy
    # packet may still carry the old affected-test log as a third artifact;
    # it is ignored (never read/validated) rather than rejected.
    if len(artifacts) not in (2, 3) or not canonical_artifacts.issubset(declared):
        return False, "sealed_artifact_path_not_canonical"
    for declared_path, digest in artifacts:
        artifact = Path(declared_path)
        try:
            if hashlib.sha256(artifact.read_bytes()).hexdigest() != digest:
                return False, "sealed_artifact_digest_mismatch"
        except OSError:
            return False, "sealed_artifact_missing"
    marker_text = Path(f"/tmp/pm-claude-code-review-{pr}-{head}.md").read_text(
        encoding="utf-8", errors="replace"
    )
    if proof_pass_status(marker_text, "PM_CLAUDE_REVIEW") != "PASS":
        return False, "sealed_review_marker_not_pass"
    if not re.search(rf"^headRefOid:\s*{re.escape(head)}\s*$", marker_text, re.M):
        return False, "sealed_review_marker_not_pass"
    capture_ok, _capture_path, capture_reason = local_capture_proof(pr, head)
    if not capture_ok:
        return False, f"sealed_capture_not_pass:{capture_reason}"
    return True, "ok"


# Documented typed slot-execution-bundle contract (incident
# cp-repair:sealed-validator-slot-bundle:7268:2026-08-13): the four raw
# current-head slot-execution receipts bound by the bundle packet, with the
# exact per-suite bound-log count and per-run passing-test count the gate
# verifies against the actual log bytes. Any deviation fails closed.
SEALED_BUNDLE_SUITES: dict[str, tuple[int, int]] = {
    # suite name -> (bound consecutive green runs, passing tests per run)
    "scorecard-bundle": (1, 51),
    "legal-deposition": (1, 5),
    "projectClosureInventory": (5, 7),
    "trusted-source": (5, 12),
}

# The exact unresolved sources the digest-bound planner-refusal artifact must
# assert (planner requires_pm_test_scope at the exact head).
SEALED_BUNDLE_UNRESOLVED_SOURCES = {
    "convex/_generated/api.d.ts",
    "lib/scribie-scorecard/external-scorecard-limits.ts",
}


def _sealed_tmp_artifact_ok(
    declared_path: str,
    digest: str,
    family: str,
    *,
    require_vacuous_red: bool = False,
) -> tuple[bool, str]:
    """Verify a digest-bound sealed-class artifact against the actual bytes.

    The declared path must resolve under /tmp, the file must exist, and its
    SHA-256 must equal the declared digest. When require_vacuous_red is set
    the bytes must additionally carry the documented VACUOUS_RED
    classification marker. Returns (ok, reason) with family-scoped fail-closed
    reason tokens.
    """
    try:
        resolved = Path(declared_path).resolve()
    except OSError:
        return False, f"{family}_unresolvable"
    if resolved.parent != Path("/tmp").resolve():
        return False, f"{family}_path_not_canonical"
    try:
        raw = resolved.read_bytes()
    except OSError:
        return False, f"{family}_missing"
    if hashlib.sha256(raw).hexdigest() != digest:
        return False, f"{family}_digest_mismatch"
    if require_vacuous_red and "VACUOUS_RED" not in raw.decode(
        "utf-8", errors="replace"
    ):
        return False, f"{family}_not_vacuous_red"
    return True, "ok"


def _sealed_no_current_head_planner_artifacts(pr: int, head: str) -> bool:
    """True when no current-head planner plan/log/.ok artifact exists on disk.

    The refusal artifact proves the planner refused BEFORE writing any
    current-head plan/log/.ok; this check re-confirms that at consumption time
    so the bundle substitution cannot be used once a genuine current-head
    planner artifact has appeared.
    """
    for pattern in (
        f"affected-test-plan-{pr}-{head}*",
        f"affected-test-proof-{pr}-{head}*",
    ):
        if list(Path("/tmp").glob(pattern)):
            return False
    return True


def _slot_receipt_passed_count(text: str) -> int | None:
    """Parse the passing-test count from a raw execution log.

    Vitest logs carry the `Tests  N passed (N)` summary line; Playwright logs
    captured through the local-repro-preflight wrapper carry `N passed
    (time)`. Only exact summary lines count; a log without a parseable
    summary is un-verifiable and fails closed.
    """
    vitest = re.findall(r"^\s*Tests\s+(\d+)\s+passed(?:\s|,|in)", text, re.M)
    if vitest:
        return int(vitest[-1])
    playwright = re.findall(r"^\s*(\d+)\s+passed(?:\s|$)", text, re.M)
    if playwright:
        return int(playwright[-1])
    return None


def _sealed_bundle_receipts_ok(text: str) -> tuple[bool, str]:
    """Verify the four typed slot-execution receipts against actual bytes.

    Each bound log is declared on one `slot_receipt:` line of the form
    `slot_receipt: <suite> <path> sha256:<hex> exit:<int> tests:<int>
    command:<...>`. The suite inventory is fixed with the documented per-suite
    bound-log count and per-run passing-test count; each declared log must
    resolve under /tmp, exist with a matching SHA-256, declare exit=0, and
    declare the exact documented test count, which must equal the count parsed
    from the log bytes. Every declared log must additionally resolve to a
    DISTINCT canonical path: replaying the same log for more than one run of a
    repeat-run suite (or across suites) fails closed with
    sealed_slot_receipt_path_duplicate so the same passing log cannot be
    repeated to fabricate consecutive-green-run evidence. Returns (ok, reason).
    """
    lines = re.findall(
        r"^slot_receipt:\s+(\S+)\s+(\S+)\s+sha256:([0-9a-f]{64})\s+"
        r"exit:(\d+)\s+tests:(\d+)\s+command:(.+)$",
        text,
        re.M,
    )
    if len(lines) != sum(logs for logs, _tests in SEALED_BUNDLE_SUITES.values()):
        return False, "sealed_slot_receipt_count_mismatch"
    by_suite: dict[str, list[tuple[str, str, int, int]]] = {}
    for suite, path, digest, exit_code, tests, _command in lines:
        by_suite.setdefault(suite, []).append(
            (path, digest, int(exit_code), int(tests))
        )
    # Distinct canonical receipt paths: the same log must never back more than
    # one declared run (per suite or across suites). Paths are compared after
    # resolution so aliasing or symlink replay is also rejected.
    seen_resolved: set = set()
    for suite, (logs, expected_tests) in SEALED_BUNDLE_SUITES.items():
        entries = by_suite.get(suite, [])
        if not entries:
            return False, "sealed_slot_receipt_missing"
        if len(entries) != logs:
            return False, "sealed_slot_receipt_count_mismatch"
        for path, digest, exit_code, declared_tests in entries:
            if exit_code != 0:
                return False, "sealed_slot_receipt_exit_nonzero"
            if declared_tests != expected_tests:
                return False, "sealed_slot_receipt_test_count_mismatch"
            try:
                resolved = Path(path).resolve()
            except OSError:
                return False, "sealed_slot_receipt_missing"
            if resolved.parent != Path("/tmp").resolve():
                return False, "sealed_slot_receipt_path_not_canonical"
            if resolved in seen_resolved:
                return False, "sealed_slot_receipt_path_duplicate"
            seen_resolved.add(resolved)
            try:
                raw = resolved.read_bytes()
            except OSError:
                return False, "sealed_slot_receipt_missing"
            if hashlib.sha256(raw).hexdigest() != digest:
                return False, "sealed_slot_receipt_digest_mismatch"
            parsed = _slot_receipt_passed_count(raw.decode("utf-8", errors="replace"))
            if parsed != expected_tests:
                return False, "sealed_slot_receipt_test_count_mismatch"
    return True, "ok"


def sealed_slot_execution_bundle_proof(
    pr: int,
    head: str,
    text: str,
    artifacts: list[tuple[str, str]],
    refusal_line: tuple[str, str],
) -> tuple[bool, str]:
    """Validate the typed slot-execution-bundle variant of the sealed
    mutation-scope override (incident
    cp-repair:sealed-validator-slot-bundle:7268:2026-08-13).

    The variant is mutually exclusive with the ordinary three-artifact sealed
    path. It binds the exact PR + 40-char head (verified by the caller), the
    retained exact-head review marker and capture-local proof (the only
    sealed_artifact lines allowed), a digest-bound planner-refusal artifact
    whose content asserts requires_pm_test_scope with the documented
    unresolved sources at the exact head, the four raw current-head
    slot-execution receipts (scorecard 51/51, smoke-legal-deposition 5/5,
    projectClosureInventory 5x7, trusted-source 5x12) each declared with
    path/command/exit=0/tests/SHA-256 and verified against the actual file
    bytes, and the historical VACUOUS_RED artifact as HISTORY ONLY. At
    consumption time the validator additionally requires that no current-head
    planner plan/log/.ok artifact exists on disk. Any deviation fails closed;
    the ordinary three-artifact path is unchanged.
    """
    canonical_artifacts = {
        str(Path(f"/tmp/pm-claude-code-review-{pr}-{head}.md").resolve()),
        str(Path(f"/tmp/capture-local-proof-{pr}-{head}.ok").resolve()),
    }
    declared = {
        str(Path(declared_path).resolve()) for declared_path, _digest in artifacts
    }
    if len(artifacts) != 2:
        return False, "sealed_artifact_count_mismatch"
    if declared != canonical_artifacts:
        return False, "sealed_artifact_path_not_canonical"
    for declared_path, digest in artifacts:
        artifact = Path(declared_path)
        try:
            if hashlib.sha256(artifact.read_bytes()).hexdigest() != digest:
                return False, "sealed_artifact_digest_mismatch"
        except OSError:
            return False, "sealed_artifact_missing"
    # Planner-refusal artifact: path + digest bound, content must assert
    # requires_pm_test_scope with the documented unresolved sources at the
    # exact head.
    refusal_path, refusal_digest = refusal_line
    ok, reason = _sealed_tmp_artifact_ok(
        refusal_path, refusal_digest, "sealed_refusal"
    )
    if not ok:
        return False, reason
    try:
        refusal = json.loads(Path(refusal_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "sealed_refusal_not_requires_pm_test_scope"
    if refusal.get("requires_pm_test_scope") is not True:
        return False, "sealed_refusal_not_requires_pm_test_scope"
    if refusal.get("headRefOid") != head:
        return False, "sealed_refusal_not_requires_pm_test_scope"
    unresolved = refusal.get("unresolved_sources")
    if not isinstance(unresolved, list) or set(unresolved) != SEALED_BUNDLE_UNRESOLVED_SOURCES:
        return False, "sealed_refusal_not_requires_pm_test_scope"
    # The refusal proves the planner wrote no current-head artifacts; confirm
    # at consumption time that none exists on disk.
    if not _sealed_no_current_head_planner_artifacts(pr, head):
        return False, "sealed_bundle_current_head_artifacts_present"
    # Historical VACUOUS_RED artifact (HISTORY ONLY, never current-head proof).
    history_lines = re.findall(
        r"^sealed_history_artifact:\s*(\S+)\s+sha256:([0-9a-f]{64})\s*$",
        text,
        re.M | re.I,
    )
    if len(history_lines) != 1:
        return False, "sealed_history_artifact_count_mismatch"
    ok, reason = _sealed_tmp_artifact_ok(
        history_lines[0][0],
        history_lines[0][1],
        "sealed_history_artifact",
        require_vacuous_red=True,
    )
    if not ok:
        return False, reason
    # The four raw slot-execution receipts, each bound by path/command/
    # exit=0/tests/SHA-256 and verified against the actual file bytes.
    ok, reason = _sealed_bundle_receipts_ok(text)
    if not ok:
        return False, reason
    # Retained exact-head checks (unchanged from the ordinary sealed class).
    marker_text = Path(f"/tmp/pm-claude-code-review-{pr}-{head}.md").read_text(
        encoding="utf-8", errors="replace"
    )
    if proof_pass_status(marker_text, "PM_CLAUDE_REVIEW") != "PASS":
        return False, "sealed_review_marker_not_pass"
    if not re.search(rf"^headRefOid:\s*{re.escape(head)}\s*$", marker_text, re.M):
        return False, "sealed_review_marker_not_pass"
    capture_ok, _capture_path, capture_reason = local_capture_proof(pr, head)
    if not capture_ok:
        return False, f"sealed_capture_not_pass:{capture_reason}"
    return True, "ok"


def moved_head_sealed_bundle_discharge(
    args: argparse.Namespace,
    explicit_proof_kind: str,
    override_proof_valid: bool,
    override_class: str,
    head: str,
) -> tuple[bool, str]:
    """Moved-head classification discharge for the sealed slot-execution
    bundle (incident cp-repair:sealed-validator-slot-bundle:7268:2026-08-13,
    CTO disposition thread 1786572299.268999, moved-head discharge
    extension).

    Mutually exclusive discharge: when the sealed mutation-scope BUNDLE
    packet (the sealed_refusal variant carrying the exact-head
    slot-execution receipts, NOT the ordinary three-artifact sealed class)
    has fully validated in this invocation (artifact digests, receipt
    exit=0/counts/consecutive runs, consumed: no sentinel, followup OPEN,
    CTO authorization, fail-closed scope terms, marker/capture digests, no
    current-head planner artifacts) and remains unconsumed, the bundle's
    exact-head slot-execution receipts discharge the moved-head
    classification instead of the (un-emittable at this head) exact-head
    local-preflight envelope or a current-head CI verdict. The discharge is
    non-consuming: consumption still fires exactly once on full admission
    via sealed_mutation_scope_override_commit. Every deviation fails
    closed. Returns (ok, reason).
    """
    if explicit_proof_kind != "override":
        return False, "sealed_discharge_not_override_proof"
    if not override_proof_valid or override_class != "mutation_scope_sealed":
        return False, "sealed_discharge_packet_not_valid"
    try:
        path = Path(args.affected_test_proof).resolve()
        if path != Path(f"/tmp/pm-ci-start-override-{args.pr}-{head}.ok").resolve():
            return False, "sealed_discharge_path_not_exact"
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, "sealed_discharge_packet_unreadable"
    refusal_lines = re.findall(
        r"^sealed_refusal:\s*(\S+)\s+sha256:([0-9a-f]{64})\s*$", text, re.M | re.I
    )
    if len(refusal_lines) != 1:
        return False, "sealed_discharge_not_bundle_variant"
    if not re.search(
        r"^slot_receipt:\s+\S+\s+\S+\s+sha256:[0-9a-f]{64}\s+exit:0\s+"
        r"tests:\d+\s+command:\S+.*$",
        text,
        re.M | re.I,
    ):
        return False, "sealed_discharge_not_bundle_variant"
    if not re.search(r"(?im)^consumed:\s*no\s*$", text) or re.search(
        r"^consumed_marker:\s*\S+\s*$", text, re.M | re.I
    ):
        return False, "sealed_discharge_packet_reuse"
    return True, "ok"


def cto_adjudicated_ci_admission_proof(
    path: Path,
    pr: int,
    head: str,
    repo: str = REPO,
    change_scope: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Validate the CTO-adjudicated one-time CI admission packet (#7249).

    This is a distinct provenance/class from the sealed mutation-scope
    override: the CTO decided that one real CI+E2E+applicable-LFC wave may
    start from an exact-head Codex REQUEST_CHANGES marker plus a VACUOUS_RED
    affected-test log, with capture provably not required for the non-LLM
    geometry diff. It never rewrites a review verdict and grants no merge
    authority. Any head/artifact/decision/followup/classifier/capture drift
    fails closed.
    """
    expected = Path(f"/tmp/pm-ci-start-override-{pr}-{head}.ok")
    try:
        if path.resolve() != expected.resolve():
            return False, "cto_override_path_not_exact"
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, "cto_override_unreadable"
    pr_match = re.search(r"^PR:\s*(\d+)\s*$", text, re.M)
    if pr_match is None or int(pr_match.group(1)) != pr:
        return False, "cto_contract_missing"
    head_match = re.search(r"^headRefOid:\s*([0-9a-f]{40})\s*$", text, re.M)
    if head_match is None or head_match.group(1) != head:
        return False, "cto_contract_missing"
    if (
        f"provenance: {CTO_ADJUDICATED_CI_ADMISSION_PROVENANCE}" not in text
        or "no_full_suite: true" not in text
    ):
        return False, "cto_contract_missing"
    consumed = re.findall(r"^consumed:\s*(\S+)\s*$", text, re.M | re.I)
    if len(consumed) != 1 or consumed[0].lower() != "no":
        return False, "cto_packet_reuse"
    if re.search(r"^consumed_marker:\s*\S+\s*$", text, re.M | re.I):
        return False, "cto_packet_reuse"
    issue = followup_issue_number(text)
    if issue is None:
        return False, "cto_followup_missing"
    try:
        followup = gh_json(
            ["issue", "view", str(issue), "--repo", repo, "--json", "state"]
        )
    except Exception:
        return False, "cto_followup_unavailable"
    if str(followup.get("state") or "").upper() != "OPEN":
        return False, "cto_followup_not_open"

    auth_ts = re.search(r"^authorization_ts:\s*(\d+(?:\.\d+)?)\s*$", text, re.M)
    disp_ts = re.search(r"^disposition_ts:\s*(\d+(?:\.\d+)?)\s*$", text, re.M)
    authorization = re.search(r"^authorization:\s*(.+)$", text, re.M | re.I)
    if (
        not auth_ts
        or not disp_ts
        or not authorization
        or auth_ts.group(1) not in authorization.group(1)
        or disp_ts.group(1) not in authorization.group(1)
    ):
        return False, "cto_decision_timestamp_mismatch"
    scope = re.search(r"^scope:\s*(.+)$", text, re.M | re.I)
    if not scope:
        return False, "cto_scope_missing"
    scope_text = scope.group(1).lower()
    if not all(
        term in scope_text
        for term in (
            "one-time",
            "ci+e2e",
            "current-head",
            "mandatory",
            "applicable-lfc",
            "no merge authority",
        )
    ):
        return False, "cto_scope_not_fail_closed"

    artifacts = re.findall(
        r"^(\S+):\s*(\S+)\s+sha256:([0-9a-f]{64})\s*$", text, re.M | re.I
    )
    marker_artifacts = [
        (declared, digest)
        for name, declared, digest in artifacts
        if name.lower() == "codex_review_marker"
    ]
    if len(marker_artifacts) != 1:
        return False, "cto_artifact_count_mismatch"
    marker_declared, marker_digest = marker_artifacts[0]
    canonical_markers = {
        str(Path(f"/tmp/codex-app-code-review-{pr}.txt").resolve()),
        str(Path(f"/tmp/codex-app-code-review-{pr}-{head}.txt").resolve()),
    }
    if str(Path(marker_declared).resolve()) not in canonical_markers:
        return False, "cto_marker_path_not_canonical"
    for artifact_path, digest in ((marker_declared, marker_digest),):
        try:
            if hashlib.sha256(Path(artifact_path).read_bytes()).hexdigest() != digest:
                return False, "cto_artifact_digest_mismatch"
        except OSError:
            return False, "cto_artifact_missing"
    marker_text = Path(marker_declared).read_text(encoding="utf-8", errors="replace")
    if (
        not re.search(r"^VERDICT:\s*REQUEST_CHANGES\s*$", marker_text, re.M)
        or not re.search(rf"^headRefOid:\s*{re.escape(head)}\s*$", marker_text, re.M)
        or not re.search(r"^pass_scope:\s*blocked\s*$", marker_text, re.M)
    ):
        return False, "cto_marker_not_request_changes"

    if change_scope is None:
        return False, "cto_change_scope_missing"
    if str(change_scope.get("head") or "") != head:
        return False, "cto_change_scope_head_mismatch"
    if change_scope.get("control_plane_only") is not False:
        return False, "cto_change_scope_not_product"
    packet_rules = re.search(
        r"^change_scope_rules_sha256:\s*([0-9a-f]{64})\s*$", text, re.M
    )
    packet_classifier = re.search(
        r"^change_scope_classifier_sha256:\s*([0-9a-f]{64})\s*$", text, re.M
    )
    if (
        not packet_rules
        or not packet_classifier
        or packet_rules.group(1) != str(change_scope.get("rules_sha256") or "")
        or packet_classifier.group(1)
        != str(change_scope.get("classifier_sha256") or "")
    ):
        return False, "cto_change_scope_digest_mismatch"

    if (
        not re.search(r"^capture_not_required:\s*true\s*$", text, re.M | re.I)
        or not re.search(
            r"^capture_basis:\s*non-llm-geometry-diff\s*$", text, re.M | re.I
        )
    ):
        return False, "cto_capture_evidence_missing"
    evidence = re.search(
        r"^capture_path_evidence:\s*(.+)$", text, re.M | re.I
    )
    if not evidence:
        return False, "cto_capture_evidence_missing"
    declared_paths = {
        value.strip()
        for value in evidence.group(1).split(",")
        if value.strip()
    }
    if not declared_paths:
        return False, "cto_capture_evidence_missing"
    capture = capture_requirement(pr, repo, head)
    if capture.get("capture_required") is not False:
        return False, "cto_capture_required"
    if str(capture.get("headRefOid") or "") != head:
        return False, "cto_capture_head_mismatch"
    if str(capture.get("reason") or "") != "no_capture_sensitive_diff":
        return False, "cto_capture_reason_mismatch"
    capture_paths = {
        str(value) for value in (capture.get("changed_paths") or []) if value
    }
    if declared_paths != capture_paths:
        return False, "cto_capture_path_mismatch"
    return True, "ok"


def full_spec_preflight_receipt_ok(path: Path, head: str) -> tuple[bool, str]:
    expected_dir = Path("/tmp/affected-test-preflight-receipts").resolve()
    try:
        resolved = path.resolve()
        if resolved.parent != expected_dir:
            return False, "cto_preflight_receipt_path_not_canonical"
        receipt = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "cto_preflight_receipt_unreadable"
    digest = str(receipt.get("commands_sha256") or "")
    if (
        resolved.name != f"{head}-{digest}.json"
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        return False, "cto_preflight_receipt_name_or_digest_mismatch"
    if (
        receipt.get("schema_version") != 1
        or receipt.get("producer") != "local-repro-preflight.sh"
    ):
        return False, "cto_preflight_receipt_schema_invalid"
    if receipt.get("headRefOid") != head:
        return False, "cto_preflight_receipt_stale_head"
    if receipt.get("result") != "PASS" or receipt.get("exit_code") != 0:
        return False, "cto_preflight_receipt_not_pass"
    contract = receipt.get("command_contract") or {}
    if (
        contract.get("mode") != "spec"
        or contract.get("spec") != "tests/e2e/specs/core/smoke-legal-deposition.spec.ts"
    ):
        return False, "cto_preflight_receipt_contract_mismatch"
    log_path = Path(str(receipt.get("log_path") or ""))
    try:
        log_bytes = log_path.read_bytes()
    except OSError:
        return False, "cto_preflight_receipt_log_missing"
    if hashlib.sha256(log_bytes).hexdigest() != receipt.get("log_sha256"):
        return False, "cto_preflight_receipt_log_digest_mismatch"
    return True, "ok"


def capture_required_receipt_ok(
    path: Path, pr: int, head: str, declared_paths: set[str]
) -> tuple[bool, str]:
    expected = Path(f"/tmp/pm-capture-required-{pr}-{head}.json")
    try:
        if path.resolve() != expected.resolve():
            return False, "cto_capture_receipt_path_not_canonical"
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "cto_capture_receipt_unreadable"
    if receipt.get("capture_required") is not False:
        return False, "cto_capture_receipt_required"
    if receipt.get("headRefOid") != head or receipt.get("pr") != pr:
        return False, "cto_capture_receipt_tuple_mismatch"
    if receipt.get("reason") != "no_capture_sensitive_diff":
        return False, "cto_capture_receipt_reason_mismatch"
    receipt_paths = {
        str(value) for value in (receipt.get("changed_paths") or []) if value
    }
    if receipt_paths != declared_paths:
        return False, "cto_capture_receipt_path_mismatch"
    return True, "ok"


def landed_repair_receipt_ok(
    path: Path, digest: str, incident: str
) -> tuple[bool, str]:
    try:
        resolved = path.resolve()
        if resolved.parent != Path("/tmp").resolve():
            return False, "cto_landed_receipt_path_not_canonical"
        if not resolved.name.startswith("cto-rescue-packet-ci-admission-receipt-"):
            return False, "cto_landed_receipt_path_not_canonical"
        data = resolved.read_bytes()
    except OSError:
        return False, "cto_landed_receipt_unreadable"
    if hashlib.sha256(data).hexdigest() != digest:
        return False, "cto_landed_receipt_digest_mismatch"
    text = data.decode("utf-8", errors="replace")
    if f"control_plane_incident: {incident}" not in text:
        return False, "cto_landed_receipt_incident_mismatch"
    if not re.search(r"^landed_head:\s*[0-9a-f]{40}\s*$", text, re.M):
        return False, "cto_landed_receipt_head_missing"
    return True, "ok"


def rescue_diff_receipt_ok(
    path: Path,
    digest: str,
    *,
    request: str,
    source: str,
    head: str,
    patch_sha256: str,
    patch_id: str,
    changed_paths: list[str],
) -> tuple[bool, str]:
    expected = Path(f"/tmp/pm-kimi3-rescue-diff-receipt-{request}-{head}.json")
    try:
        if path.resolve() != expected.resolve():
            return False, "cto_diff_receipt_path_not_canonical"
        data = path.read_bytes()
    except OSError:
        return False, "cto_diff_receipt_unreadable"
    if hashlib.sha256(data).hexdigest() != digest:
        return False, "cto_diff_receipt_digest_mismatch"
    try:
        receipt = json.loads(data.decode("utf-8"))
    except ValueError:
        return False, "cto_diff_receipt_invalid"
    if (
        receipt.get("schema_version") != 1
        or receipt.get("producer") != "cto-direct-control-plane-repair"
    ):
        return False, "cto_diff_receipt_schema_invalid"
    if (
        receipt.get("request") != request
        or receipt.get("source_head") != source
        or receipt.get("head") != head
    ):
        return False, "cto_diff_receipt_tuple_mismatch"
    if (
        receipt.get("patch_sha256") != patch_sha256
        or receipt.get("stable_patch_id") != patch_id
    ):
        return False, "cto_diff_receipt_patch_mismatch"
    receipt_paths = [str(value) for value in (receipt.get("changed_paths") or [])]
    if receipt_paths != changed_paths:
        return False, "cto_diff_receipt_path_mismatch"
    return True, "ok"


def local_git_rescue_diff_verification(
    source: str,
    head: str,
    repo_root: Path | None,
    *,
    patch_sha256: str,
    patch_id: str,
    changed_paths: list[str],
) -> tuple[bool, str] | None:
    """Verify the rescue source-parent diff locally when refs are available.

    Returns None when the repository/refs are unavailable (installed runtime);
    the sealed diff receipt remains authoritative in that case. When
    available, any drift fails closed.
    """
    if repo_root is None or not repo_root.is_dir():
        return None
    try:
        for ref in (source, head):
            subprocess.run(
                ["git", "-C", str(repo_root), "rev-parse", "--verify", f"{ref}^{{commit}}"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        parents = subprocess.run(
            ["git", "-C", str(repo_root), "rev-list", "--parents", "-n", "1", head],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.split()
        if len(parents) < 2 or parents[1] != source:
            return False, "cto_source_parent_relation_mismatch"
        diff = subprocess.run(
            ["git", "-C", str(repo_root), "diff", "--binary", source, head],
            check=True,
            capture_output=True,
            timeout=30,
        ).stdout
        computed_sha = hashlib.sha256(diff).hexdigest()
        patched = subprocess.run(
            ["git", "patch-id", "--stable"],
            input=diff,
            capture_output=True,
            timeout=30,
        )
        if patched.returncode != 0:
            return None
        computed_id = patched.stdout.split(b" ", 1)[0].decode("ascii", errors="replace")
        names = subprocess.run(
            ["git", "-C", str(repo_root), "diff", "--name-only", source, head],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout.splitlines()
        if computed_sha != patch_sha256 or computed_id != patch_id:
            return False, "cto_diff_patch_mismatch"
        if names != changed_paths:
            return False, "cto_diff_path_mismatch"
    except (OSError, subprocess.SubprocessError):
        return None
    return True, "ok"


def cto_rescue_packet_ci_admission_proof(
    path: Path,
    pr: int,
    head: str,
    repo: str = REPO,
    change_scope: dict[str, Any] | None = None,
    repo_root: Path | None = None,
) -> tuple[bool, str]:
    """Validate the rescue-packet-bound CTO CI admission packet (#7227).

    Distinct from both sealed classes: admission is anchored to the canonical
    PM Kimi rescue packet (PATCH_READY / slot_actionable / skip_further_review),
    the source-parent exact diff (patch SHA + stable patch-id + paths), the
    exact affected plan/log (12/12 + VACUOUS_RED), a canonical full-spec
    Playwright preflight PASS receipt, classifier-bound capture-not-required,
    and a CTO degraded-delivery decision. One-time atomic consumption; CI/E2E
    mandatory; LFC not required because editor_changed=false; no merge
    authority. Any drift fails closed.
    """
    expected = Path(f"/tmp/pm-ci-start-override-{pr}-{head}.ok")
    try:
        if path.resolve() != expected.resolve():
            return False, "cto_rescue_override_path_not_exact"
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, "cto_rescue_override_unreadable"
    pr_match = re.search(r"^PR:\s*(\d+)\s*$", text, re.M)
    if pr_match is None or int(pr_match.group(1)) != pr:
        return False, "cto_rescue_contract_missing"
    head_match = re.search(r"^headRefOid:\s*([0-9a-f]{40})\s*$", text, re.M)
    if head_match is None or head_match.group(1) != head:
        return False, "cto_rescue_contract_missing"
    if (
        f"provenance: {CTO_RESCUE_PACKET_CI_ADMISSION_PROVENANCE}" not in text
        or "no_full_suite: true" not in text
        or not re.search(
            r"^AFFECTED_TESTS:\s*PASS_WITH_VACUOUS_RED(?:\s|$)", text, re.M
        )
    ):
        return False, "cto_rescue_contract_missing"
    consumed = re.findall(r"^consumed:\s*(\S+)\s*$", text, re.M | re.I)
    if len(consumed) != 1 or consumed[0].lower() != "no":
        return False, "cto_rescue_packet_reuse"
    if re.search(r"^consumed_marker:\s*\S+\s*$", text, re.M | re.I):
        return False, "cto_rescue_packet_reuse"
    followup = followup_issue_number(text)
    if followup is not None and followup == 7251:
        return False, "cto_rescue_forbidden_followup"
    incident = re.search(
        r"^control_plane_incident:\s*control-plane:cto-rescue-packet-ci-admission\s*$",
        text,
        re.M,
    )
    if not incident:
        return False, "cto_rescue_incident_missing"

    auth_ts = re.search(
        rf"^authorization_ts:\s*{re.escape(CTO_DEGRADED_DELIVERY_DECISION_TS)}\s*$",
        text,
        re.M,
    )
    alias_ts = re.search(r"^decision_alias_ts:\s*(\S+)\s*$", text, re.M | re.I)
    if not auth_ts:
        return False, "cto_rescue_decision_ts_mismatch"
    if alias_ts and alias_ts.group(1) != CTO_DEGRADED_DELIVERY_ALIAS_TS:
        return False, "cto_rescue_decision_ts_mismatch"
    authorization = re.search(r"^authorization:\s*(.+)$", text, re.M | re.I)
    if (
        not authorization
        or CTO_DEGRADED_DELIVERY_DECISION_TS not in authorization.group(1)
    ):
        return False, "cto_rescue_decision_ts_mismatch"
    scope = re.search(r"^scope:\s*(.+)$", text, re.M | re.I)
    if not scope:
        return False, "cto_rescue_scope_missing"
    scope_text = scope.group(1).lower()
    if not all(
        term in scope_text
        for term in ("one-time", "ci+e2e", "current-head", "mandatory", "no merge authority")
    ):
        return False, "cto_rescue_scope_not_fail_closed"
    if not re.search(r"^lfc_not_required:\s*true\s*$", text, re.M | re.I):
        return False, "cto_rescue_lfc_not_required_missing"

    artifacts = re.findall(
        r"^(\S+):\s*(\S+)\s+sha256:([0-9a-f]{64})\s*$", text, re.M | re.I
    )
    by_name = {name.lower(): (declared, digest) for name, declared, digest in artifacts}
    required_artifact_names = {
        "rescue_packet",
        "full_spec_preflight_receipt",
        "rescue_diff_receipt",
        "capture_required_receipt",
        "landed_repair_receipt",
    }
    if not required_artifact_names.issubset(set(by_name)):
        return False, "cto_rescue_artifact_count_mismatch"

    request_match = re.search(r"^rescue_request:\s*(\S+)\s*$", text, re.M)
    source_match = re.search(r"^rescue_source_head:\s*([0-9a-f]{40})\s*$", text, re.M)
    patch_sha_match = re.search(
        r"^rescue_patch_sha256:\s*([0-9a-f]{64})\s*$", text, re.M
    )
    patch_id_match = re.search(
        r"^rescue_patch_id:\s*([0-9a-f]{40})\s*$", text, re.M
    )
    runtime_match = re.search(
        r"^rescue_runtime_control_point:\s*tests/e2e/helpers/certificate-selection\.ts::commitCertificateSelection\s*$",
        text,
        re.M,
    )
    changed_match = re.search(r"^rescue_changed_paths:\s*(.+)$", text, re.M)
    if (
        not request_match
        or not source_match
        or not patch_sha_match
        or not patch_id_match
        or not runtime_match
        or not changed_match
    ):
        return False, "cto_rescue_contract_missing"
    changed_paths = [value.strip() for value in changed_match.group(1).split(",") if value.strip()]
    if len(changed_paths) != 4 or any(not value for value in changed_paths):
        return False, "cto_rescue_changed_paths_mismatch"

    packet_path, packet_digest = by_name["rescue_packet"]
    canonical_packet = RESCUE_PACKET_ARTIFACT_ROOT / f"pm-kimi3-rescue-packet-{request_match.group(1)}.md"
    try:
        if Path(packet_path).resolve() != canonical_packet.resolve():
            return False, "cto_rescue_packet_path_not_canonical"
        packet_bytes = Path(packet_path).read_bytes()
    except OSError:
        return False, "cto_rescue_packet_unreadable"
    if hashlib.sha256(packet_bytes).hexdigest() != packet_digest:
        return False, "cto_rescue_packet_digest_mismatch"
    packet_text = packet_bytes.decode("utf-8", errors="replace")
    packet_checks = (
        re.search(r"^terminal:\s*PATCH_READY\s*$", packet_text, re.M),
        re.search(r"^slot_actionable:\s*true\s*$", packet_text, re.M),
        re.search(r"^skip_further_review:\s*true\s*$", packet_text, re.M),
        re.search(rf"^[Pp][Rr]:\s*#?{pr}\s*$", packet_text, re.M),
        re.search(
            rf"^head_or_plan_sha:\s*{re.escape(source_match.group(1))}\s*$",
            packet_text,
            re.M,
        ),
        re.search(
            rf"^mop_request_id:\s*{re.escape(request_match.group(1))}\s*$",
            packet_text,
            re.M,
        ),
        re.search(
            rf"^patch_sha256:\s*{re.escape(patch_sha_match.group(1))}\s*$",
            packet_text,
            re.M,
        ),
        re.search(
            rf"^changed_paths:\s*{re.escape(changed_match.group(1))}\s*$",
            packet_text,
            re.M,
        ),
        "tests/e2e/helpers/certificate-selection.ts::commitCertificateSelection"
        in packet_text,
    )
    if not all(packet_checks):
        return False, "cto_rescue_packet_fields_invalid"

    preflight_path, preflight_digest = by_name["full_spec_preflight_receipt"]
    diff_path, diff_digest = by_name["rescue_diff_receipt"]
    capture_path, capture_digest = by_name["capture_required_receipt"]
    landed_path, landed_digest = by_name["landed_repair_receipt"]

    ok, reason = full_spec_preflight_receipt_ok(
        Path(preflight_path), head
    )
    if not ok:
        return False, reason
    try:
        if hashlib.sha256(Path(preflight_path).read_bytes()).hexdigest() != preflight_digest:
            return False, "cto_rescue_preflight_digest_mismatch"
    except OSError:
        return False, "cto_rescue_preflight_missing"

    ok, reason = rescue_diff_receipt_ok(
        Path(diff_path),
        diff_digest,
        request=request_match.group(1),
        source=source_match.group(1),
        head=head,
        patch_sha256=patch_sha_match.group(1),
        patch_id=patch_id_match.group(1),
        changed_paths=changed_paths,
    )
    if not ok:
        return False, reason
    local = local_git_rescue_diff_verification(
        source_match.group(1),
        head,
        repo_root,
        patch_sha256=patch_sha_match.group(1),
        patch_id=patch_id_match.group(1),
        changed_paths=changed_paths,
    )
    if local is not None and not local[0]:
        return False, local[1]

    capture = capture_requirement(pr, repo, head)
    if capture.get("capture_required") is not False:
        return False, "cto_rescue_capture_required"
    if str(capture.get("headRefOid") or "") != head:
        return False, "cto_rescue_capture_head_mismatch"
    if str(capture.get("reason") or "") != "no_capture_sensitive_diff":
        return False, "cto_rescue_capture_reason_mismatch"
    capture_paths = {
        str(value) for value in (capture.get("changed_paths") or []) if value
    }
    ok, reason = capture_required_receipt_ok(
        Path(capture_path), pr, head, capture_paths
    )
    if not ok:
        return False, reason
    try:
        if hashlib.sha256(Path(capture_path).read_bytes()).hexdigest() != capture_digest:
            return False, "cto_rescue_capture_digest_mismatch"
    except OSError:
        return False, "cto_rescue_capture_missing"

    ok, reason = landed_repair_receipt_ok(
        Path(landed_path), landed_digest, "control-plane:cto-rescue-packet-ci-admission"
    )
    if not ok:
        return False, reason

    if change_scope is None:
        return False, "cto_rescue_change_scope_missing"
    if str(change_scope.get("head") or "") != head:
        return False, "cto_rescue_change_scope_head_mismatch"
    if change_scope.get("control_plane_only") is not False:
        return False, "cto_rescue_change_scope_not_product"
    if change_scope.get("editor_changed") is not False or change_scope.get("lfc_required") is not False:
        return False, "cto_rescue_lfc_not_exempt"
    rules = re.search(
        r"^change_scope_rules_sha256:\s*([0-9a-f]{64})\s*$", text, re.M
    )
    classifier = re.search(
        r"^change_scope_classifier_sha256:\s*([0-9a-f]{64})\s*$", text, re.M
    )
    if (
        not rules
        or not classifier
        or rules.group(1) != str(change_scope.get("rules_sha256") or "")
        or classifier.group(1)
        != str(change_scope.get("classifier_sha256") or "")
    ):
        return False, "cto_rescue_change_scope_digest_mismatch"
    return True, "ok"


def github_capture_run_verified(
    run_id: str, pr: int, run_head: str, repo: str = REPO
) -> tuple[bool, str]:
    """Verify a live GitHub E2E LLM Proxy Capture run end-to-end."""
    try:
        data = gh_json(
            [
                "run",
                "view",
                str(run_id),
                "--repo",
                repo,
                "--json",
                "headSha,status,conclusion,event,displayTitle,workflowName",
            ]
        )
    except Exception:
        return False, "cto_marker_capture_run_unavailable"
    expected_title = f"remote-capture-pr-{pr}-head-{run_head}"
    if (
        str(data.get("headSha") or "") != run_head
        or str(data.get("status") or "").lower() != "completed"
        or str(data.get("conclusion") or "").lower() != "success"
        or str(data.get("event") or "").lower() != "workflow_dispatch"
        or str(data.get("displayTitle") or "") != expected_title
        or str(data.get("workflowName") or "")
        != "E2E LLM Proxy Capture (manual)"
    ):
        return False, "cto_marker_capture_run_mismatch"
    return True, "ok"


def capture_run_receipt_ok(
    path: Path,
    digest: str,
    pr: int,
    head: str,
    *,
    run_id: str,
    run_head: str,
    fixtures_promoted: int,
    repo: str = REPO,
) -> tuple[bool, str]:
    expected = Path(f"/tmp/pm-capture-run-verified-{pr}-{head}.json")
    try:
        if path.resolve() != expected.resolve():
            return False, "cto_marker_capture_receipt_path_not_canonical"
        data = path.read_bytes()
    except OSError:
        return False, "cto_marker_capture_receipt_unreadable"
    if hashlib.sha256(data).hexdigest() != digest:
        return False, "cto_marker_capture_receipt_digest_mismatch"
    try:
        receipt = json.loads(data.decode("utf-8"))
    except ValueError:
        return False, "cto_marker_capture_receipt_invalid"
    if (
        receipt.get("schema_version") != 1
        or receipt.get("producer") != "cto-direct-control-plane-repair"
    ):
        return False, "cto_marker_capture_receipt_schema_invalid"
    if (
        receipt.get("pr") != pr
        or receipt.get("headRefOid") != head
        or str(receipt.get("run_id") or "") != run_id
        or str(receipt.get("run_head") or "") != run_head
        or receipt.get("fixtures_promoted") != fixtures_promoted
    ):
        return False, "cto_marker_capture_receipt_tuple_mismatch"
    ok, reason = github_capture_run_verified(run_id, pr, run_head, repo)
    if not ok:
        return False, reason
    return True, "ok"


def review_receipt_ok(
    path: Path,
    digest: str,
    pr: int,
    head: str,
    *,
    marker_digest: str,
) -> tuple[bool, str]:
    expected = Path(f"/tmp/pm-review-done-receipt-{pr}-{head}.json")
    try:
        if path.resolve() != expected.resolve():
            return False, "cto_marker_review_receipt_path_not_canonical"
        data = path.read_bytes()
    except OSError:
        return False, "cto_marker_review_receipt_unreadable"
    if hashlib.sha256(data).hexdigest() != digest:
        return False, "cto_marker_review_receipt_digest_mismatch"
    try:
        receipt = json.loads(data.decode("utf-8"))
    except ValueError:
        return False, "cto_marker_review_receipt_invalid"
    if receipt.get("schema_version") != 1:
        return False, "cto_marker_review_receipt_schema_invalid"
    if (
        receipt.get("pr") != pr
        or receipt.get("head_sha") != head
        or str(receipt.get("verdict") or "").upper() != "PASS"
        or str(receipt.get("handoff_status") or "") != "blocked_after_review"
        or str((receipt.get("blocked_after_review") or {}).get("class") or "")
        != "affected_test_refusal"
        or str(receipt.get("marker_sha256") or "") != marker_digest
    ):
        return False, "cto_marker_review_receipt_contract_mismatch"
    return True, "ok"


def cto_marker_pass_ci_admission_proof(
    path: Path,
    pr: int,
    head: str,
    repo: str = REPO,
    change_scope: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Validate the marker-PASS CTO-adjudicated CI admission packet (#7217).

    Binds the exact Phase-A PASS marker (path/digest/content), canonical
    affected-test plan and proof log (path/digest), the blocked_after_review
    receipt, a GREEN parent-head capture run receipt, the corpus-pin hash, and
    the source PM wake timestamp. The log must contain at least one genuine
    mutation RED (exit 1) AND the correctly classified VACUOUS_RED for the
    collection/load empty command (exit 5 is never accepted as a RED). No
    merge authority; one-time atomic consumption; expires on head drift.
    """
    expected = Path(f"/tmp/pm-ci-start-override-{pr}-{head}.ok")
    try:
        if path.resolve() != expected.resolve():
            return False, "cto_marker_override_path_not_exact"
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, "cto_marker_override_unreadable"
    pr_match = re.search(r"^PR:\s*(\d+)\s*$", text, re.M)
    if pr_match is None or int(pr_match.group(1)) != pr:
        return False, "cto_marker_contract_missing"
    head_match = re.search(r"^headRefOid:\s*([0-9a-f]{40})\s*$", text, re.M)
    if head_match is None or head_match.group(1) != head:
        return False, "cto_marker_contract_missing"
    if (
        f"provenance: {CTO_MARKER_PASS_CI_ADMISSION_PROVENANCE}" not in text
        or "no_full_suite: true" not in text
    ):
        return False, "cto_marker_contract_missing"
    if not re.search(
        r"^control_plane_incident:\s*control-plane:cto-marker-pass-vacuous-red-ci-admission\s*$",
        text,
        re.M,
    ):
        return False, "cto_marker_contract_missing"
    consumed = re.findall(r"^consumed:\s*(\S+)\s*$", text, re.M | re.I)
    if len(consumed) != 1 or consumed[0].lower() != "no":
        return False, "cto_marker_packet_reuse"
    if re.search(r"^consumed_marker:\s*\S+\s*$", text, re.M | re.I):
        return False, "cto_marker_packet_reuse"

    auth_ts = re.search(
        rf"^authorization_ts:\s*{re.escape(CTO_MARKER_PASS_SOURCE_TS)}\s*$",
        text,
        re.M,
    )
    source_wake = re.search(
        rf"^source_wake:\s*{re.escape(CTO_MARKER_PASS_SOURCE_WAKE)}\s*$",
        text,
        re.M,
    )
    authorization = re.search(r"^authorization:\s*(.+)$", text, re.M | re.I)
    if (
        not auth_ts
        or not source_wake
        or not authorization
        or CTO_MARKER_PASS_SOURCE_TS not in authorization.group(1)
    ):
        return False, "cto_marker_decision_ts_mismatch"
    scope = re.search(r"^scope:\s*(.+)$", text, re.M | re.I)
    if not scope:
        return False, "cto_marker_scope_missing"
    scope_text = scope.group(1).lower()
    if not all(
        term in scope_text
        for term in ("one-time", "ci+e2e", "current-head", "mandatory", "no merge authority")
    ):
        return False, "cto_marker_scope_not_fail_closed"
    if not re.search(
        r"^vacuous_reason:\s*branch_added_fixture_collection_exit_5_not_mutation_red\s*$",
        text,
        re.M,
    ):
        return False, "cto_marker_vacuous_reason_missing"
    pin = re.search(
        r"^proofread_pin_sha256:\s*([0-9a-f]{64})\s*$", text, re.M
    )
    if not pin:
        return False, "cto_marker_pin_missing"

    artifacts = re.findall(
        r"^(\S+):\s*(\S+)\s+sha256:([0-9a-f]{64})\s*$", text, re.M | re.I
    )
    by_name = {name.lower(): (declared, digest) for name, declared, digest in artifacts}
    required = {
        "review_marker",
        "review_receipt",
        "capture_run_receipt",
    }
    if not required.issubset(set(by_name)):
        return False, "cto_marker_artifact_count_mismatch"

    run_id_match = re.search(r"^capture_run_id:\s*(\d+)\s*$", text, re.M)
    run_head_match = re.search(
        r"^capture_run_head:\s*([0-9a-f]{40})\s*$", text, re.M
    )
    fixtures_match = re.search(
        r"^capture_fixtures_promoted:\s*(\d+)\s*$", text, re.M
    )
    if not run_id_match or not run_head_match or not fixtures_match:
        return False, "cto_marker_capture_run_binding_missing"
    run_id, run_head, fixtures = (
        run_id_match.group(1),
        run_head_match.group(1),
        int(fixtures_match.group(1)),
    )

    marker_path, marker_digest = by_name["review_marker"]
    receipt_path, receipt_digest = by_name["review_receipt"]
    capture_path, capture_digest = by_name["capture_run_receipt"]
    canonical = {
        "review_marker": Path(f"/tmp/pm-claude-code-review-{pr}-{head}.md"),
        "review_receipt": Path(f"/tmp/pm-review-done-receipt-{pr}-{head}.json"),
        "capture_run_receipt": Path(f"/tmp/pm-capture-run-verified-{pr}-{head}.json"),
    }
    for name, declared, digest in (
        ("review_marker", marker_path, marker_digest),
        ("review_receipt", receipt_path, receipt_digest),
        ("capture_run_receipt", capture_path, capture_digest),
    ):
        try:
            if Path(declared).resolve() != canonical[name].resolve():
                return False, "cto_marker_artifact_path_not_canonical"
            if hashlib.sha256(Path(declared).read_bytes()).hexdigest() != digest:
                return False, "cto_marker_artifact_digest_mismatch"
        except OSError:
            return False, "cto_marker_artifact_missing"

    marker_text = Path(marker_path).read_text(encoding="utf-8", errors="replace")
    if (
        not re.search(r"^PM_CLAUDE_REVIEW:\s*PASS\s*$", marker_text, re.M)
        or not re.search(rf"^headRefOid:\s*{re.escape(head)}\s*$", marker_text, re.M)
        or not re.search(r"^pass_scope:\s*phase-a\s*$", marker_text, re.M)
        or pin.group(1)[:12] not in marker_text
    ):
        return False, "cto_marker_not_pass"
    ok, reason = review_receipt_ok(
        Path(receipt_path),
        receipt_digest,
        pr,
        head,
        marker_digest=marker_digest,
    )
    if not ok:
        return False, reason
    ok, reason = capture_run_receipt_ok(
        Path(capture_path),
        capture_digest,
        pr,
        head,
        run_id=run_id,
        run_head=run_head,
        fixtures_promoted=fixtures,
        repo=repo,
    )
    if not ok:
        return False, reason

    if change_scope is None:
        return False, "cto_marker_change_scope_missing"
    if str(change_scope.get("head") or "") != head:
        return False, "cto_marker_change_scope_head_mismatch"
    if change_scope.get("control_plane_only") is not False:
        return False, "cto_marker_change_scope_not_product"
    rules = re.search(
        r"^change_scope_rules_sha256:\s*([0-9a-f]{64})\s*$", text, re.M
    )
    classifier = re.search(
        r"^change_scope_classifier_sha256:\s*([0-9a-f]{64})\s*$", text, re.M
    )
    if (
        not rules
        or not classifier
        or rules.group(1) != str(change_scope.get("rules_sha256") or "")
        or classifier.group(1)
        != str(change_scope.get("classifier_sha256") or "")
    ):
        return False, "cto_marker_change_scope_digest_mismatch"
    return True, "ok"


def _cto_no_patch_rescue_receipt_ok(
    path: Path,
    digest: str,
    *,
    request_id: str,
    kind: str,
    head: str,
    expected_exit: str,
) -> tuple[bool, str]:
    try:
        if not path.is_file():
            return False, "cto_no_patch_receipt_unreadable"
        data = path.read_bytes()
    except OSError:
        return False, "cto_no_patch_receipt_unreadable"
    if hashlib.sha256(data).hexdigest() != digest:
        return False, "cto_no_patch_receipt_digest_mismatch"
    text = data.decode("utf-8", errors="replace")
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if first != "PM_FABLE_RESCUE_TEST_RECEIPT":
        return False, "cto_no_patch_receipt_header_invalid"
    request_field = re.search(r"^request_id:\s*(\S+)\s*$", text, re.M)
    kind_field = re.search(r"^kind:\s*(\S+)\s*$", text, re.M)
    exit_field = re.search(r"^exit_code:\s*(\S+)\s*$", text, re.M)
    if (
        not request_field
        or request_field.group(1) != request_id
        or not kind_field
        or kind_field.group(1) != kind
        or not exit_field
        or exit_field.group(1) != expected_exit
    ):
        return False, "cto_no_patch_receipt_contract_mismatch"
    if head not in text:
        return False, "cto_no_patch_receipt_head_not_bound"
    return True, "ok"


def cto_no_patch_rescue_ci_admission_proof(
    path: Path,
    pr: int,
    head: str,
    repo: str = REPO,
    change_scope: dict[str, Any] | None = None,
    repo_root: Path | None = None,
) -> tuple[bool, str]:
    """Validate the CTO-adjudicated NO_PATCH_REQUIRED rescue CI admission
    packet (#7268 family).

    One immutable consumption class bound to PR, full head, the three CTO
    decision timestamps, the canonical kimi3 rescue packet path+digest,
    NO_PATCH_REQUIRED / slot_actionable / skip_further_review,
    changed_paths none, required_pm_action continue_verified_head, and the
    exact RED/GREEN receipt paths+digests (content-bound to request/head).
    One-time atomic consumption; expiry; mandatory current-head CI+E2E; no
    merge authority; classifier capture_required=false required. The ordinary
    envelope-shape provenance checks are superseded by the CTO adjudication
    and are intentionally not repeated here.
    """

    expected = Path(f"/tmp/pm-ci-start-override-{pr}-{head}.ok")
    try:
        if path.resolve() != expected.resolve():
            return False, "cto_no_patch_override_path_not_exact"
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, "cto_no_patch_override_unreadable"
    pr_match = re.search(r"^PR:\s*(\d+)\s*$", text, re.M)
    if pr_match is None or int(pr_match.group(1)) != pr:
        return False, "cto_no_patch_contract_missing"
    head_match = re.search(r"^headRefOid:\s*([0-9a-f]{40})\s*$", text, re.M)
    if head_match is None or head_match.group(1) != head:
        return False, "cto_no_patch_contract_missing"
    if (
        f"provenance: {CTO_NO_PATCH_RESCUE_CI_ADMISSION_PROVENANCE}" not in text
        or "no_full_suite: true" not in text
        or not re.search(r"^AFFECTED_TESTS:\s*PASS(?:\s|$)", text, re.M)
    ):
        return False, "cto_no_patch_contract_missing"
    incident = re.search(
        rf"^control_plane_incident:\s*{re.escape(CTO_NO_PATCH_RESCUE_INCIDENT)}\s*$",
        text,
        re.M,
    )
    if not incident:
        return False, "cto_no_patch_incident_missing"
    consumed = re.findall(r"^consumed:\s*(\S+)\s*$", text, re.M | re.I)
    if len(consumed) != 1 or consumed[0].lower() != "no":
        return False, "cto_no_patch_packet_reuse"
    if re.search(r"^consumed_marker:\s*\S+\s*$", text, re.M | re.I):
        return False, "cto_no_patch_packet_reuse"

    ts_lines = re.findall(
        r"^authorization_ts:\s*(\d+(?:\.\d+)?)\s*$", text, re.M
    )
    authorization = re.search(r"^authorization:\s*(.+)$", text, re.M | re.I)
    if (
        sorted(ts_lines) != sorted(CTO_NO_PATCH_RESCUE_DECISION_TS)
        or not authorization
        or not all(ts in authorization.group(1) for ts in CTO_NO_PATCH_RESCUE_DECISION_TS)
    ):
        return False, "cto_no_patch_decision_ts_mismatch"
    scope = re.search(r"^scope:\s*(.+)$", text, re.M | re.I)
    if not scope:
        return False, "cto_no_patch_scope_missing"
    scope_text = scope.group(1).lower()
    if not all(
        term in scope_text
        for term in ("one-time", "ci+e2e", "current-head", "mandatory", "no merge authority")
    ):
        return False, "cto_no_patch_scope_not_fail_closed"
    if not re.search(r"^lfc_not_required:\s*true\s*$", text, re.M | re.I):
        return False, "cto_no_patch_lfc_not_required_missing"
    expires = re.search(r"^expires_at:\s*(\S+)\s*$", text, re.M)
    if not expires:
        return False, "cto_no_patch_expires_at_missing"
    try:
        expiry = datetime.fromisoformat(
            expires.group(1).replace("Z", "+00:00")
        )
        if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
            return False, "cto_no_patch_expired"
    except ValueError:
        return False, "cto_no_patch_expires_at_invalid"

    artifacts = re.findall(
        r"^(\S+):\s*(\S+)\s+sha256:([0-9a-f]{64})\s*$", text, re.M | re.I
    )
    by_name = {name.lower(): (declared, digest) for name, declared, digest in artifacts}
    required_artifact_names = {"rescue_packet", "red_receipt", "green_receipt"}
    if set(by_name) != required_artifact_names:
        return False, "cto_no_patch_artifact_count_mismatch"

    packet_path, packet_digest = by_name["rescue_packet"]
    red_path, red_digest = by_name["red_receipt"]
    green_path, green_digest = by_name["green_receipt"]
    request_match = re.search(r"^rescue_request:\s*(\S+)\s*$", text, re.M)
    if not request_match:
        return False, "cto_no_patch_contract_missing"
    request_id = request_match.group(1)
    canonical_packet = (
        RESCUE_PACKET_ARTIFACT_ROOT / f"pm-kimi3-rescue-packet-{request_id}.md"
    )
    try:
        if Path(packet_path).resolve() != canonical_packet.resolve():
            return False, "cto_no_patch_packet_path_not_canonical"
        packet_bytes = Path(packet_path).read_bytes()
    except OSError:
        return False, "cto_no_patch_packet_unreadable"
    if hashlib.sha256(packet_bytes).hexdigest() != packet_digest:
        return False, "cto_no_patch_packet_digest_mismatch"
    packet_text = packet_bytes.decode("utf-8", errors="replace")
    red_match = re.search(
        r"^red_proof_receipt_sha256:\s*(\S+)\s*$", packet_text, re.M
    )
    green_match = re.search(
        r"^green_proof_receipt_sha256:\s*(\S+)\s*$", packet_text, re.M
    )
    red_declared = red_match.group(1) if red_match else ""
    green_declared = green_match.group(1) if green_match else ""
    packet_checks = (
        re.search(r"^PM_CLAUDE_PR_RESCUE:\s*NO_PATCH_REQUIRED\s*$", packet_text, re.M),
        re.search(r"^terminal:\s*NO_PATCH_REQUIRED\s*$", packet_text, re.M),
        re.search(r"^slot_actionable:\s*true\s*$", packet_text, re.M),
        re.search(r"^skip_further_review:\s*true\s*$", packet_text, re.M),
        re.search(rf"^[Pp][Rr]:\s*#?{pr}\s*$", packet_text, re.M),
        re.search(rf"^head_or_plan_sha:\s*{re.escape(head)}\s*$", packet_text, re.M),
        re.search(
            rf"^mop_request_id:\s*{re.escape(request_id)}\s*$",
            packet_text,
            re.M,
        ),
        re.search(r"^changed_paths:\s*none\s*$", packet_text, re.M),
        re.search(r"^patch_file:\s*none\s*$", packet_text, re.M),
        re.search(r"^patch_sha256:\s*none\s*$", packet_text, re.M),
        re.search(
            r"^required_pm_action:\s*continue_verified_head\s*$",
            packet_text,
            re.M,
        ),
        bool(re.fullmatch(r"[0-9a-f]{64}", red_declared)),
        bool(re.fullmatch(r"[0-9a-f]{64}", green_declared)),
        red_declared == red_digest,
        green_declared == green_digest,
    )
    if not all(packet_checks):
        return False, "cto_no_patch_packet_fields_invalid"

    ok, reason = _cto_no_patch_rescue_receipt_ok(
        Path(red_path),
        red_digest,
        request_id=request_id,
        kind="red",
        head=head,
        expected_exit="1",
    )
    if not ok:
        return False, reason
    ok, reason = _cto_no_patch_rescue_receipt_ok(
        Path(green_path),
        green_digest,
        request_id=request_id,
        kind="green",
        head=head,
        expected_exit="0",
    )
    if not ok:
        return False, reason

    capture = capture_requirement(pr, repo, head)
    if capture.get("capture_required") is not False:
        return False, "cto_no_patch_capture_required"
    if str(capture.get("headRefOid") or "") != head:
        return False, "cto_no_patch_capture_head_mismatch"

    if change_scope is None:
        return False, "cto_no_patch_change_scope_missing"
    if str(change_scope.get("head") or "") != head:
        return False, "cto_no_patch_change_scope_head_mismatch"
    if change_scope.get("control_plane_only") is not False:
        return False, "cto_no_patch_change_scope_not_product"
    if change_scope.get("editor_changed") is not False or change_scope.get("lfc_required") is not False:
        return False, "cto_no_patch_lfc_not_exempt"
    rules = re.search(
        r"^change_scope_rules_sha256:\s*([0-9a-f]{64})\s*$", text, re.M
    )
    classifier = re.search(
        r"^change_scope_classifier_sha256:\s*([0-9a-f]{64})\s*$", text, re.M
    )
    if (
        not rules
        or not classifier
        or rules.group(1) != str(change_scope.get("rules_sha256") or "")
        or classifier.group(1)
        != str(change_scope.get("classifier_sha256") or "")
    ):
        return False, "cto_no_patch_change_scope_digest_mismatch"
    return True, "ok"


def cto_no_patch_rescue_moved_head_discharge(
    args: argparse.Namespace,
    explicit_proof_kind: str,
    override_proof_valid: bool,
    override_class: str,
    head: str,
) -> tuple[bool, str]:
    """Moved-head classification discharge for the CTO-adjudicated
    NO_PATCH_REQUIRED rescue class (incident
    cp-repair:cto-direct-admission-unexecutable:7268).

    The bound rescue packet's exact-head RED/GREEN receipts discharge the
    stale/moved-head classification in place of the (un-emittable at this
    head) local-preflight envelope or a current-head CI verdict. The
    discharge is non-consuming: the override's full packet/decision/expiry/
    receipt content validation already ran in ci_start_override_dispatch, and
    consumption fires exactly once on full admission via the atomic
    --commit-reentry path. Every deviation fails closed.
    """

    if explicit_proof_kind != "override":
        return False, "cto_no_patch_discharge_not_override_proof"
    if not override_proof_valid or override_class != "cto_no_patch_rescue_ci_admission":
        return False, "cto_no_patch_discharge_packet_not_valid"
    try:
        path = Path(args.affected_test_proof).resolve()
        if path != Path(f"/tmp/pm-ci-start-override-{args.pr}-{head}.ok").resolve():
            return False, "cto_no_patch_discharge_path_not_exact"
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, "cto_no_patch_discharge_packet_unreadable"
    consumed = re.findall(r"^consumed:\s*(\S+)\s*$", text, re.M | re.I)
    if len(consumed) != 1 or consumed[0].lower() != "no":
        return False, "cto_no_patch_discharge_packet_reuse"
    if re.search(r"^consumed_marker:\s*\S+\s*$", text, re.M | re.I):
        return False, "cto_no_patch_discharge_packet_reuse"
    return True, "ok"


def cto_exact_tuple_ci_admission_proof(
    path: Path,
    pr: int,
    head: str,
    repo: str = REPO,
    change_scope: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Validate the exact-allowlisted one-shot CTO CI-start admission packet.

    Binds exactly three already-adjudicated tuples (Rajiv thread
    1786724301.511569 ts 1786725255.074339; CTO decision thread
    1786717451.157469 ts 1786724519.596549):

      - PR 7275 mode=post-capture-preflight: sealed post-capture
        local-preflight proof (path/digest/content + source/plan/log SHAs),
        the terminal-green exact-head capture run, the open obligation 12959
        (ci_rerun_after_preflight / cto_ci_wave_required), and the Rajiv
        decision ts.
      - PR 7289 mode=vacuous-red: Codex functionality-first APPROVE marker,
        affected plan, and VACUOUS_RED log digests; capture_required=false;
        required gate set CI+E2E+LFC.
      - PR 7331 mode=vacuous-red: PM PASS marker, Codex REQUEST_CHANGES
        proof-only marker (CTO adjudication accepted it), affected plan,
        VACUOUS_RED log, and the sealed 5-run receipt; capture_required=false;
        required gate set CI+E2E.

    One-time atomic consumption; mandatory current-head real CI/E2E (+
    applicable LFC); no merge authority; PM never fires CI. Wrong
    PR/head/digest/mode/gate-set/evidence or a non-allowlisted tuple fails
    closed. Never weakens the sealed class or the other adjudicated classes.
    """
    expected = Path(f"/tmp/pm-ci-start-override-{pr}-{head}.ok")
    try:
        if path.resolve() != expected.resolve():
            return False, "cto_exact_tuple_override_path_not_exact"
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, "cto_exact_tuple_override_unreadable"
    pr_match = re.search(r"^PR:\s*(\d+)\s*$", text, re.M)
    head_match = re.search(r"^headRefOid:\s*([0-9a-f]{40})\s*$", text, re.M)
    if pr_match is None or int(pr_match.group(1)) != pr or head_match is None or head_match.group(1) != head:
        return False, "cto_exact_tuple_contract_missing"
    if (
        f"provenance: {CTO_EXACT_TUPLE_CI_ADMISSION_PROVENANCE}" not in text
        or "no_full_suite: true" not in text
    ):
        return False, "cto_exact_tuple_contract_missing"
    if not re.search(
        rf"^control_plane_incident:\s*{re.escape(CTO_EXACT_TUPLE_CI_ADMISSION_INCIDENT)}\s*$",
        text,
        re.M,
    ):
        return False, "cto_exact_tuple_contract_missing"
    consumed = re.findall(r"^consumed:\s*(\S+)\s*$", text, re.M | re.I)
    if len(consumed) != 1 or consumed[0].lower() != "no":
        return False, "cto_exact_tuple_packet_reuse"
    if re.search(r"^consumed_marker:\s*\S+\s*$", text, re.M | re.I):
        return False, "cto_exact_tuple_packet_reuse"

    allowlist = CTO_EXACT_TUPLE_ADMISSIONS.get(int(pr))
    if not allowlist or allowlist["head"] != head:
        return False, "cto_exact_tuple_not_allowlisted"
    mode = re.search(r"^mode:\s*(\S+)\s*$", text, re.M)
    if mode is None or mode.group(1) != allowlist["mode"]:
        return False, "cto_exact_tuple_mode_mismatch"

    auth_ts = re.search(
        rf"^authorization_ts:\s*{re.escape(allowlist['authorization_ts'])}\s*$",
        text,
        re.M,
    )
    source_wake = re.search(
        rf"^source_wake:\s*{re.escape(allowlist['source_wake'])}\s*$",
        text,
        re.M,
    )
    authorization = re.search(r"^authorization:\s*(.+)$", text, re.M | re.I)
    if (
        not auth_ts
        or not source_wake
        or not authorization
        or allowlist["authorization_ts"] not in authorization.group(1)
    ):
        return False, "cto_exact_tuple_decision_ts_mismatch"
    scope = re.search(r"^scope:\s*(.+)$", text, re.M | re.I)
    if not scope:
        return False, "cto_exact_tuple_scope_missing"
    scope_text = scope.group(1).lower()
    if not all(
        term in scope_text
        for term in ("one-time", "current-head", "mandatory", "no merge authority")
    ):
        return False, "cto_exact_tuple_scope_not_fail_closed"
    gate_set = re.search(r"^required_gate_set:\s*(\S+)\s*$", text, re.M | re.I)
    if gate_set is None or gate_set.group(1).lower() != allowlist["required_gate_set"]:
        return False, "cto_exact_tuple_gate_set_mismatch"

    if change_scope is None:
        return False, "cto_exact_tuple_change_scope_missing"
    if str(change_scope.get("head") or "") != head:
        return False, "cto_exact_tuple_change_scope_head_mismatch"
    if change_scope.get("ci_required") is not True or change_scope.get("e2e_required") is not True:
        return False, "cto_exact_tuple_change_scope_not_real_ci_e2e"
    if "lfc" in allowlist["required_gate_set"]:
        if change_scope.get("lfc_required") is not True:
            return False, "cto_exact_tuple_gate_set_lfc_not_applicable"
    elif change_scope.get("lfc_required") is True:
        return False, "cto_exact_tuple_gate_set_lfc_unexpected"
    rules = re.search(
        r"^change_scope_rules_sha256:\s*([0-9a-f]{64})\s*$", text, re.M
    )
    classifier = re.search(
        r"^change_scope_classifier_sha256:\s*([0-9a-f]{64})\s*$", text, re.M
    )
    if (
        not rules
        or not classifier
        or rules.group(1) != str(change_scope.get("rules_sha256") or "")
        or classifier.group(1)
        != str(change_scope.get("classifier_sha256") or "")
    ):
        return False, "cto_exact_tuple_change_scope_digest_mismatch"

    mode_name = allowlist["mode"]
    if mode_name == "post-capture-preflight":
        return _cto_exact_tuple_post_capture_ok(
            text, pr, head, repo, allowlist, change_scope
        )
    return _cto_exact_tuple_vacuous_red_ok(
        text, pr, head, allowlist, change_scope
    )


def _cto_exact_tuple_post_capture_ok(
    text: str,
    pr: int,
    head: str,
    repo: str,
    allowlist: dict[str, Any],
    change_scope: dict[str, Any],
) -> tuple[bool, str]:
    proof_match = re.search(
        r"^sealed_preflight_proof:\s*(\S+)\s+sha256:([0-9a-f]{64})\s*$",
        text,
        re.M | re.I,
    )
    if proof_match is None:
        return False, "cto_exact_tuple_preflight_missing"
    proof_path = Path(proof_match.group(1))
    proof_digest = proof_match.group(2)
    canonical_proof = Path(allowlist["preflight_proof"])
    try:
        if proof_path.resolve() != canonical_proof.resolve():
            return False, "cto_exact_tuple_preflight_path_not_canonical"
        proof_bytes = proof_path.read_bytes()
    except OSError:
        return False, "cto_exact_tuple_preflight_unreadable"
    if hashlib.sha256(proof_bytes).hexdigest() != proof_digest:
        return False, "cto_exact_tuple_preflight_digest_mismatch"
    proof = proof_bytes.decode("utf-8", errors="replace")
    if (
        not re.search(r"^CI_LOCAL_PREFLIGHT:\s*PASS(?:\s|$)", proof, re.M)
        or not re.search(rf"^PR:\s*{pr}\s*$", proof, re.M)
        or not re.search(rf"^headRefOid:\s*{re.escape(head)}\s*$", proof, re.M)
        or not re.search(r"^failed_run:\s*31801964758\s*$", proof, re.M)
        or not re.search(r"^ci_class:\s*post-capture-local-repro\s*$", proof, re.M)
        or not re.search(
            rf"^source_sha256:\s*{allowlist['source_sha256']}\s*$", proof, re.M
        )
        or not re.search(
            rf"^plan_sha256:\s*{allowlist['plan_sha256']}\s*$", proof, re.M
        )
        or not re.search(
            rf"^log_sha256:\s*{allowlist['log_sha256']}\s*$", proof, re.M
        )
    ):
        return False, "cto_exact_tuple_preflight_content_mismatch"
    capture_run = re.search(r"^capture_run:\s*(\d+)\s*$", text, re.M)
    capture_head = re.search(r"^capture_run_head:\s*([0-9a-f]{40})\s*$", text, re.M)
    if (
        capture_run is None
        or capture_run.group(1) != allowlist["capture_run"]
        or capture_head is None
        or capture_head.group(1) != head
    ):
        return False, "cto_exact_tuple_capture_binding_missing"
    ok, reason = github_capture_run_verified(
        allowlist["capture_run"], pr, head, repo
    )
    if not ok:
        return False, f"cto_exact_tuple_capture_run_{reason}"
    obligation = re.search(
        r"^obligation:\s*(\S+)\s+(\S+)\s+(\S+)\s*$", text, re.M | re.I
    )
    expected_obligation = allowlist["obligation"]
    if (
        obligation is None
        or obligation.group(1) != expected_obligation[0]
        or obligation.group(2) != expected_obligation[1]
        or obligation.group(3) != expected_obligation[2]
    ):
        return False, "cto_exact_tuple_obligation_binding_missing"
    capture = capture_requirement(pr, repo, head)
    if str(capture.get("headRefOid") or "") != head:
        return False, "cto_exact_tuple_capture_head_mismatch"
    return True, "ok"


def _cto_exact_tuple_vacuous_red_ok(
    text: str,
    pr: int,
    head: str,
    allowlist: dict[str, Any],
    change_scope: dict[str, Any],
) -> tuple[bool, str]:
    artifact_re = re.compile(
        r"^(\S+):\s*(\S+)\s+sha256:([0-9a-f]{64})\s*$", re.M | re.I
    )
    by_name: dict[str, tuple[str, str]] = {}
    for name, declared, digest in artifact_re.findall(text):
        by_name[name.lower()] = (declared, digest)
    if int(pr) == 7289:
        required = {"codex_review_marker"}
        if not required.issubset(set(by_name)):
            return False, "cto_exact_tuple_artifact_count_mismatch"
        canonical = {
            "codex_review_marker": Path(allowlist["codex_marker"]),
        }
        digests = {
            "codex_review_marker": allowlist["codex_marker_sha256"],
        }
        for name, declared, digest in (
            ("codex_review_marker", by_name["codex_review_marker"][0], by_name["codex_review_marker"][1]),
        ):
            try:
                if Path(declared).resolve() != canonical[name].resolve():
                    return False, "cto_exact_tuple_artifact_path_not_canonical"
                if hashlib.sha256(Path(declared).read_bytes()).hexdigest() != digest:
                    return False, "cto_exact_tuple_artifact_digest_mismatch"
                if digest != digests[name]:
                    return False, "cto_exact_tuple_artifact_digest_mismatch"
            except OSError:
                return False, "cto_exact_tuple_artifact_missing"
        marker_text = Path(by_name["codex_review_marker"][0]).read_text(
            encoding="utf-8", errors="replace"
        )
        if (
            not re.search(r"^VERDICT:\s*APPROVE\s*$", marker_text, re.M)
            or not re.search(rf"^headRefOid:\s*{re.escape(head)}\s*$", marker_text, re.M)
        ):
            return False, "cto_exact_tuple_codex_marker_not_approve"
    elif int(pr) == 7331:
        required = {
            "pm_review_marker",
            "codex_review_marker",
            "sealed_run_receipt",
        }
        if not required.issubset(set(by_name)):
            return False, "cto_exact_tuple_artifact_count_mismatch"
        canonical = {
            "pm_review_marker": Path(allowlist["pm_marker"]),
            "codex_review_marker": Path(allowlist["codex_marker"]),
            "sealed_run_receipt": Path(allowlist["run_receipt"]),
        }
        digests = {
            "pm_review_marker": allowlist["pm_marker_sha256"],
            "codex_review_marker": allowlist["codex_marker_sha256"],
            "sealed_run_receipt": allowlist["run_receipt_sha256"],
        }
        for name in required:
            declared, digest = by_name[name]
            try:
                if Path(declared).resolve() != canonical[name].resolve():
                    return False, "cto_exact_tuple_artifact_path_not_canonical"
                if hashlib.sha256(Path(declared).read_bytes()).hexdigest() != digest:
                    return False, "cto_exact_tuple_artifact_digest_mismatch"
                if digest != digests[name]:
                    return False, "cto_exact_tuple_artifact_digest_mismatch"
            except OSError:
                return False, "cto_exact_tuple_artifact_missing"
        pm_text = Path(by_name["pm_review_marker"][0]).read_text(
            encoding="utf-8", errors="replace"
        )
        if (
            not re.search(r"^PM_CLAUDE_REVIEW:\s*PASS\s*$", pm_text, re.M)
            or not re.search(rf"^headRefOid:\s*{re.escape(head)}\s*$", pm_text, re.M)
            or not re.search(r"^pass_scope:\s*phase-a\s*$", pm_text, re.M)
        ):
            return False, "cto_exact_tuple_pm_marker_not_pass"
        codex_text = Path(by_name["codex_review_marker"][0]).read_text(
            encoding="utf-8", errors="replace"
        )
        if (
            not re.search(r"^VERDICT:\s*REQUEST_CHANGES\s*$", codex_text, re.M)
            or not re.search(rf"^headRefOid:\s*{re.escape(head)}\s*$", codex_text, re.M)
        ):
            return False, "cto_exact_tuple_codex_marker_not_proof_only"
        run_text = Path(by_name["sealed_run_receipt"][0]).read_text(
            encoding="utf-8", errors="replace"
        )
        if (
            "FIVE_PASS_OK" not in run_text
            or not re.search(r"5 consecutive PASS", run_text)
            or head not in run_text
        ):
            return False, "cto_exact_tuple_run_receipt_not_sealed"
    else:
        return False, "cto_exact_tuple_not_allowlisted"
    capture = capture_requirement(pr, REPO, head)
    if capture.get("capture_required") is not False:
        return False, "cto_exact_tuple_capture_required"
    if str(capture.get("headRefOid") or "") != head:
        return False, "cto_exact_tuple_capture_head_mismatch"
    return True, "ok"


def cto_cancelled_run_local_preflight_ci_admission_proof(
    path: Path,
    pr: int,
    head: str,
    repo: str = REPO,
    change_scope: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Validate the CTO-adjudicated cancelled-run + sealed local-preflight
    CI-start admission for PR #7305 (Rajiv/CTO recovery lane
    C0ALZJHGE49 thread 1786759192.277439 ts 1786760957.087989).

    Bind the exact PR/head, the failed cancelled CI run (all completed real
    steps green, no named product failure), the sealed exact-head
    ci-local-preflight proof, and the exact-head affected-test source/plan/log
    digests (test-only diff; affected-test planner is structurally VACUOUS_RED
    for the new test-only file). CI-start only, no merge authority; one-time
    atomic consumption; fail closed on head/digest/replay/active-wave
    mismatch. No new review or proof grammar is required.
    """
    expected = Path(f"/tmp/pm-ci-start-override-{pr}-{head}.ok")
    try:
        if path.resolve() != expected.resolve():
            return False, "cto_cancelled_preflight_override_path_not_exact"
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, "cto_cancelled_preflight_override_unreadable"
    pr_match = re.search(r"^PR:\s*(\d+)\s*$", text, re.M)
    head_match = re.search(r"^headRefOid:\s*([0-9a-f]{40})\s*$", text, re.M)
    if pr_match is None or int(pr_match.group(1)) != pr or head_match is None or head_match.group(1) != head:
        return False, "cto_cancelled_preflight_contract_missing"
    if (
        "provenance: cto-cancelled-run-local-preflight-ci-admission" not in text
        or "no_full_suite: true" not in text
    ):
        return False, "cto_cancelled_preflight_contract_missing"
    if not re.search(
        r"^control_plane_incident:\s*control-plane:cto-cancelled-run-local-preflight-ci-admission:7305\s*$",
        text,
        re.M,
    ):
        return False, "cto_cancelled_preflight_contract_missing"
    consumed = re.findall(r"^consumed:\s*(\S+)\s*$", text, re.M | re.I)
    if len(consumed) != 1 or consumed[0].lower() != "no":
        return False, "cto_cancelled_preflight_packet_reuse"
    if re.search(r"^consumed_marker:\s*\S+\s*$", text, re.M | re.I):
        return False, "cto_cancelled_preflight_packet_reuse"

    allowlist = CTO_EXACT_TUPLE_ADMISSIONS.get(int(pr))
    if not allowlist or allowlist["head"] != head or allowlist["mode"] != "cancelled-run-local-preflight":
        return False, "cto_cancelled_preflight_not_allowlisted"
    mode = re.search(r"^mode:\s*(\S+)\s*$", text, re.M)
    if mode is None or mode.group(1) != allowlist["mode"]:
        return False, "cto_cancelled_preflight_mode_mismatch"
    auth_ts = re.search(
        rf"^authorization_ts:\s*{re.escape(allowlist['authorization_ts'])}\s*$",
        text,
        re.M,
    )
    source_wake = re.search(
        rf"^source_wake:\s*{re.escape(allowlist['source_wake'])}\s*$",
        text,
        re.M,
    )
    authorization = re.search(r"^authorization:\s*(.+)$", text, re.M | re.I)
    if (
        not auth_ts
        or not source_wake
        or not authorization
        or allowlist["authorization_ts"] not in authorization.group(1)
    ):
        return False, "cto_cancelled_preflight_decision_ts_mismatch"
    scope = re.search(r"^scope:\s*(.+)$", text, re.M | re.I)
    if not scope:
        return False, "cto_cancelled_preflight_scope_missing"
    scope_text = scope.group(1).lower()
    if not all(
        term in scope_text
        for term in ("one-time", "current-head", "mandatory", "no merge authority")
    ):
        return False, "cto_cancelled_preflight_scope_not_fail_closed"
    gate_set = re.search(r"^required_gate_set:\s*(\S+)\s*$", text, re.M | re.I)
    if gate_set is None or gate_set.group(1).lower() != allowlist["required_gate_set"]:
        return False, "cto_cancelled_preflight_gate_set_mismatch"

    failed_run = re.search(r"^failed_run:\s*(\d+)\s*$", text, re.M)
    if failed_run is None or failed_run.group(1) != allowlist["failed_run"]:
        return False, "cto_cancelled_preflight_run_binding_missing"
    proof_match = re.search(
        r"^sealed_preflight_proof:\s*(\S+)\s+sha256:([0-9a-f]{64})\s*$",
        text,
        re.M | re.I,
    )
    if proof_match is None:
        return False, "cto_cancelled_preflight_proof_missing"
    proof_path = Path(proof_match.group(1))
    proof_digest = proof_match.group(2)
    canonical_proof = Path(allowlist["preflight_proof"])
    try:
        if proof_path.resolve() != canonical_proof.resolve():
            return False, "cto_cancelled_preflight_proof_path_not_canonical"
        proof_bytes = proof_path.read_bytes()
    except OSError:
        return False, "cto_cancelled_preflight_proof_unreadable"
    if hashlib.sha256(proof_bytes).hexdigest() != proof_digest:
        return False, "cto_cancelled_preflight_proof_digest_mismatch"
    if proof_digest != allowlist["preflight_sha256"]:
        return False, "cto_cancelled_preflight_proof_digest_mismatch"
    proof = proof_bytes.decode("utf-8", errors="replace")
    if (
        not re.search(r"^CI_LOCAL_PREFLIGHT:\s*PASS(?:\s|$)", proof, re.M)
        or not re.search(rf"^PR:\s*{pr}\s*$", proof, re.M)
        or not re.search(rf"^headRefOid:\s*{re.escape(head)}\s*$", proof, re.M)
        or not re.search(rf"^failed_run:\s*{allowlist['failed_run']}\s*$", proof, re.M)
        or not re.search(r"^ci_class:\s*current-head-failure\s*$", proof, re.M)
    ):
        return False, "cto_cancelled_preflight_proof_content_mismatch"
    source_receipt = re.search(
        r"^source_receipt:\s*(\S+)\s+sha256:([0-9a-f]{64})\s*$",
        text,
        re.M | re.I,
    )
    if source_receipt is None:
        return False, "cto_cancelled_preflight_source_receipt_missing"
    source_path = Path(source_receipt.group(1))
    source_digest = source_receipt.group(2)
    try:
        if source_path.resolve() != Path(allowlist["source_receipt"]).resolve():
            return False, "cto_cancelled_preflight_source_path_not_canonical"
        if hashlib.sha256(source_path.read_bytes()).hexdigest() != source_digest:
            return False, "cto_cancelled_preflight_source_digest_mismatch"
        if source_digest != allowlist["source_receipt_sha256"]:
            return False, "cto_cancelled_preflight_source_digest_mismatch"
    except OSError:
        return False, "cto_cancelled_preflight_source_missing"
    if change_scope is None:
        return False, "cto_cancelled_preflight_change_scope_missing"
    if str(change_scope.get("head") or "") != head:
        return False, "cto_cancelled_preflight_change_scope_head_mismatch"
    if change_scope.get("ci_required") is not True or change_scope.get("e2e_required") is not True:
        return False, "cto_cancelled_preflight_change_scope_not_real_ci_e2e"
    rules = re.search(
        r"^change_scope_rules_sha256:\s*([0-9a-f]{64})\s*$", text, re.M
    )
    classifier = re.search(
        r"^change_scope_classifier_sha256:\s*([0-9a-f]{64})\s*$", text, re.M
    )
    if (
        not rules
        or not classifier
        or rules.group(1) != str(change_scope.get("rules_sha256") or "")
        or classifier.group(1)
        != str(change_scope.get("classifier_sha256") or "")
    ):
        return False, "cto_cancelled_preflight_change_scope_digest_mismatch"
    capture = capture_requirement(pr, REPO, head)
    if capture.get("capture_required") is not False:
        return False, "cto_cancelled_preflight_capture_required"
    if str(capture.get("headRefOid") or "") != head:
        return False, "cto_cancelled_preflight_capture_head_mismatch"
    return True, "ok"


def verify_rebind_checkout_git(checkout: Path, head: str) -> tuple[bool, str]:
    """Verify a fresh clean detached checkout at the exact head/tree bytes.

    Used by the one-shot local-preflight-rebind admission (#7308): the rebind
    checkout must be absolute, git-readable, at the exact head, clean, and
    (when the canonical repo can resolve the head tree) carry the exact tree.
    """
    if not os.path.isabs(str(checkout)):
        return False, "local_preflight_rebind_checkout_not_absolute"
    try:
        checkout_head = run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"], timeout=20
        ).strip()
        checkout_status = run(
            ["git", "-C", str(checkout), "status", "--porcelain"], timeout=20
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return False, "local_preflight_rebind_checkout_unreadable"
    if checkout_head != head:
        return False, "local_preflight_rebind_checkout_head_mismatch"
    if checkout_status:
        return False, "local_preflight_rebind_checkout_dirty"
    try:
        checkout_tree = run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD^{tree}"], timeout=20
        ).strip()
        repo_root = Path(__file__).resolve().parents[3]
        expected_tree = run(
            ["git", "-C", str(repo_root), "rev-parse", f"{head}^{{tree}}"],
            timeout=20,
        ).strip()
        if checkout_tree != expected_tree:
            return False, "local_preflight_rebind_checkout_tree_mismatch"
    except (OSError, subprocess.CalledProcessError, RuntimeError):
        pass
    return True, "ok"


def rebind_checkout_from_packet(text: str) -> str:
    match = re.search(r"^rebind_checkout:\s*(\S+)\s*$", text, re.M | re.I)
    return match.group(1) if match else ""


def cto_local_preflight_rebind_ci_admission_proof(
    path: Path,
    pr: int,
    head: str,
    repo: str = REPO,
    change_scope: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Validate the one-shot local-preflight-rebind CI-start admission (#7308).

    CTO typed blocker ts 1786767400.760729 (Slack C0ALZJHGE49 thread
    1786759192.277439): PR 7308 @ 559419e2, failed run 31791885167, sealed
    32/32 local-repro receipt (sha 478b26c2…), immutable log
    (sha db0b3cb2…), adopted Fable phase-a PASS marker (sha c482326e…), and
    open CTO-wave obligations 13007/13110. The admission binds the immutable
    log/receipt/marker final bytes to a fresh clean detached checkout via an
    explicit checkout-path override and is atomically consumed exactly once.
    Fail closed on head/log/receipt/marker drift, dirty/absent/mismatched
    checkout, replay, capture drift, or non-allowlisted tuple. CI-start only,
    no merge authority.
    """
    expected = Path(f"/tmp/pm-ci-start-override-{pr}-{head}.ok")
    try:
        if path.resolve() != expected.resolve():
            return False, "cto_local_preflight_rebind_override_path_not_exact"
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, "cto_local_preflight_rebind_override_unreadable"
    pr_match = re.search(r"^PR:\s*(\d+)\s*$", text, re.M)
    head_match = re.search(r"^headRefOid:\s*([0-9a-f]{40})\s*$", text, re.M)
    if pr_match is None or int(pr_match.group(1)) != pr or head_match is None or head_match.group(1) != head:
        return False, "cto_local_preflight_rebind_contract_missing"
    if (
        f"provenance: {CTO_LOCAL_PREFLIGHT_REBIND_CI_ADMISSION_PROVENANCE}" not in text
        or "no_full_suite: true" not in text
        or not re.search(
            r"^control_plane_incident:\s*control-plane:cto-local-preflight-rebind:7308\s*$",
            text,
            re.M,
        )
    ):
        return False, "cto_local_preflight_rebind_contract_missing"
    consumed = re.findall(r"^consumed:\s*(\S+)\s*$", text, re.M | re.I)
    if len(consumed) != 1 or consumed[0].lower() != "no":
        return False, "cto_local_preflight_rebind_packet_reuse"
    if re.search(r"^consumed_marker:\s*\S+\s*$", text, re.M | re.I):
        return False, "cto_local_preflight_rebind_packet_reuse"

    allowlist = CTO_EXACT_TUPLE_ADMISSIONS.get(int(pr))
    if not allowlist or allowlist["head"] != head or allowlist["mode"] != "local-preflight-rebind":
        return False, "cto_local_preflight_rebind_not_allowlisted"
    mode = re.search(r"^mode:\s*(\S+)\s*$", text, re.M)
    if mode is None or mode.group(1) != allowlist["mode"]:
        return False, "cto_local_preflight_rebind_mode_mismatch"
    auth_ts = re.search(
        rf"^authorization_ts:\s*{re.escape(allowlist['authorization_ts'])}\s*$",
        text,
        re.M,
    )
    source_wake = re.search(
        rf"^source_wake:\s*{re.escape(allowlist['source_wake'])}\s*$",
        text,
        re.M,
    )
    authorization = re.search(r"^authorization:\s*(.+)$", text, re.M | re.I)
    if (
        not auth_ts
        or not source_wake
        or not authorization
        or allowlist["authorization_ts"] not in authorization.group(1)
    ):
        return False, "cto_local_preflight_rebind_decision_ts_mismatch"
    scope = re.search(r"^scope:\s*(.+)$", text, re.M | re.I)
    if not scope:
        return False, "cto_local_preflight_rebind_scope_missing"
    scope_text = scope.group(1).lower()
    if not all(
        term in scope_text
        for term in ("one-time", "current-head", "mandatory", "no merge authority")
    ):
        return False, "cto_local_preflight_rebind_scope_not_fail_closed"
    gate_set = re.search(r"^required_gate_set:\s*(\S+)\s*$", text, re.M | re.I)
    if gate_set is None or gate_set.group(1).lower() != allowlist["required_gate_set"]:
        return False, "cto_local_preflight_rebind_gate_set_mismatch"
    failed_run = re.search(r"^failed_run:\s*(\d+)\s*$", text, re.M)
    if failed_run is None or failed_run.group(1) != allowlist["failed_run"]:
        return False, "cto_local_preflight_rebind_run_binding_missing"

    if change_scope is None:
        return False, "cto_local_preflight_rebind_change_scope_missing"
    if str(change_scope.get("head") or "") != head:
        return False, "cto_local_preflight_rebind_change_scope_head_mismatch"
    if change_scope.get("ci_required") is not True or change_scope.get("e2e_required") is not True:
        return False, "cto_local_preflight_rebind_change_scope_not_real_ci_e2e"
    rules = re.search(
        r"^change_scope_rules_sha256:\s*([0-9a-f]{64})\s*$", text, re.M
    )
    classifier = re.search(
        r"^change_scope_classifier_sha256:\s*([0-9a-f]{64})\s*$", text, re.M
    )
    if (
        not rules
        or not classifier
        or rules.group(1) != str(change_scope.get("rules_sha256") or "")
        or classifier.group(1)
        != str(change_scope.get("classifier_sha256") or "")
    ):
        return False, "cto_local_preflight_rebind_change_scope_digest_mismatch"

    rebind_checkout = rebind_checkout_from_packet(text)
    if not rebind_checkout:
        return False, "cto_local_preflight_rebind_checkout_missing"
    ok, reason = verify_rebind_checkout_git(Path(rebind_checkout), head)
    if not ok:
        return False, f"cto_local_preflight_rebind_{reason}"

    proof_match = re.search(
        r"^sealed_preflight_proof:\s*(\S+)\s+sha256:([0-9a-f]{64})\s*$",
        text,
        re.M | re.I,
    )
    if proof_match is None:
        return False, "cto_local_preflight_rebind_preflight_missing"
    proof_path = Path(proof_match.group(1))
    proof_digest = proof_match.group(2)
    try:
        if proof_path.resolve() != Path(allowlist["preflight_proof"]).resolve():
            return False, "cto_local_preflight_rebind_preflight_path_not_canonical"
        proof_bytes = proof_path.read_bytes()
    except OSError:
        return False, "cto_local_preflight_rebind_preflight_unreadable"
    if hashlib.sha256(proof_bytes).hexdigest() != proof_digest:
        return False, "cto_local_preflight_rebind_preflight_digest_mismatch"
    if proof_digest != allowlist["preflight_sha256"]:
        return False, "cto_local_preflight_rebind_preflight_digest_mismatch"
    proof = proof_bytes.decode("utf-8", errors="replace")
    if (
        not re.search(r"^CI_LOCAL_PREFLIGHT:\s*PASS(?:\s|$)", proof, re.M)
        or not re.search(rf"^PR:\s*{pr}\s*$", proof, re.M)
        or not re.search(rf"^headRefOid:\s*{re.escape(head)}\s*$", proof, re.M)
        or not re.search(rf"^failed_run:\s*{allowlist['failed_run']}\s*$", proof, re.M)
        or not re.search(r"^ci_class:\s*current-head-failure\s*$", proof, re.M)
        or not re.search(
            rf"^source_sha256:\s*{allowlist['source_receipt_sha256']}\s*$", proof, re.M
        )
        or not re.search(
            rf"^log_sha256:\s*{allowlist['log_sha256']}\s*$", proof, re.M
        )
    ):
        return False, "cto_local_preflight_rebind_preflight_content_mismatch"

    receipt_match = re.search(
        r"^source_receipt:\s*(\S+)\s+sha256:([0-9a-f]{64})\s*$",
        text,
        re.M | re.I,
    )
    if receipt_match is None:
        return False, "cto_local_preflight_rebind_source_receipt_missing"
    receipt_path = Path(receipt_match.group(1))
    receipt_digest = receipt_match.group(2)
    try:
        if receipt_path.resolve() != Path(allowlist["source_receipt"]).resolve():
            return False, "cto_local_preflight_rebind_source_receipt_path_not_canonical"
        receipt_bytes = receipt_path.read_bytes()
        receipt = json.loads(receipt_bytes.decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        return False, "cto_local_preflight_rebind_source_receipt_unreadable"
    if hashlib.sha256(receipt_bytes).hexdigest() != receipt_digest:
        return False, "cto_local_preflight_rebind_source_receipt_digest_mismatch"
    if receipt_digest != allowlist["source_receipt_sha256"]:
        return False, "cto_local_preflight_rebind_source_receipt_digest_mismatch"
    if (
        str(receipt.get("producer") or "") != "local-repro-preflight.sh"
        or str(receipt.get("headRefOid") or "") != head
        or str(receipt.get("log_sha256") or "") != allowlist["log_sha256"]
        or not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("commands_sha256") or ""))
    ):
        return False, "cto_local_preflight_rebind_source_receipt_content_mismatch"

    marker_match = re.search(
        r"^adopted_marker:\s*(\S+)\s+sha256:([0-9a-f]{64})\s*$",
        text,
        re.M | re.I,
    )
    if marker_match is None:
        return False, "cto_local_preflight_rebind_marker_missing"
    marker_path = Path(marker_match.group(1))
    marker_digest = marker_match.group(2)
    try:
        if marker_path.resolve() != Path(allowlist["marker"]).resolve():
            return False, "cto_local_preflight_rebind_marker_path_not_canonical"
        marker_bytes = marker_path.read_bytes()
    except OSError:
        return False, "cto_local_preflight_rebind_marker_missing"
    if hashlib.sha256(marker_bytes).hexdigest() != marker_digest:
        return False, "cto_local_preflight_rebind_marker_digest_mismatch"
    if marker_digest != allowlist["marker_sha256"]:
        return False, "cto_local_preflight_rebind_marker_digest_mismatch"
    marker = marker_bytes.decode("utf-8", errors="replace")
    if (
        not re.search(r"^PM_CLAUDE_REVIEW:\s*PASS\s*$", marker, re.M)
        or not re.search(r"^review_model:\s*fable\s*$", marker, re.M)
        or not re.search(rf"^headRefOid:\s*{re.escape(head)}\s*$", marker, re.M)
        or not re.search(r"^pass_scope:\s*phase-a\s*$", marker, re.M)
    ):
        return False, "cto_local_preflight_rebind_marker_not_adopted_pass"

    expected_obligations = allowlist["obligations"]
    obligations = re.findall(
        r"^obligation:\s*(\S+)\s+(\S+)\s+(\S+)\s*$", text, re.M | re.I
    )
    if tuple(obligations) != expected_obligations:
        return False, "cto_local_preflight_rebind_obligation_binding_missing"

    capture = capture_requirement(pr, repo, head)
    if capture.get("capture_required") is not False:
        return False, "cto_local_preflight_rebind_capture_required"
    if str(capture.get("headRefOid") or "") != head:
        return False, "cto_local_preflight_rebind_capture_head_mismatch"
    return True, "ok"


def ci_start_override_dispatch(
    path: Path,
    pr: int,
    head: str,
    repo: str = REPO,
    change_scope: dict[str, Any] | None = None,
    repo_root: Path | None = None,
) -> tuple[bool, str, str]:
    """Dispatch the one-time CI-start override file to its class validator.

    Returns (ok, reason, kind). The pre-existing-failure class delegates to the
    unchanged ci_start_override_proof with identical semantics; every packet
    without the sealed provenance takes that unchanged path.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, "unreadable", "unknown"
    kind = ci_start_override_class(text)
    if kind == "mutation_scope_sealed":
        ok, reason = mutation_scope_sealed_override_proof(path, pr, head, repo)
        return ok, reason, kind
    if kind == "cto_adjudicated_ci_admission":
        ok, reason = cto_adjudicated_ci_admission_proof(
            path, pr, head, repo, change_scope
        )
        return ok, reason, kind
    if kind == "cto_rescue_packet_ci_admission":
        ok, reason = cto_rescue_packet_ci_admission_proof(
            path, pr, head, repo, change_scope, repo_root
        )
        return ok, reason, kind
    if kind == "cto_marker_pass_ci_admission":
        ok, reason = cto_marker_pass_ci_admission_proof(
            path, pr, head, repo, change_scope
        )
        return ok, reason, kind
    if kind == "cto_no_patch_rescue_ci_admission":
        ok, reason = cto_no_patch_rescue_ci_admission_proof(
            path, pr, head, repo, change_scope, repo_root
        )
        return ok, reason, kind
    if kind == "cto_exact_tuple_ci_admission":
        ok, reason = cto_exact_tuple_ci_admission_proof(
            path, pr, head, repo, change_scope
        )
        return ok, reason, kind
    if kind == "cto_cancelled_run_local_preflight_ci_admission":
        ok, reason = cto_cancelled_run_local_preflight_ci_admission_proof(
            path, pr, head, repo, change_scope
        )
        return ok, reason, kind
    if kind == "cto_local_preflight_rebind_ci_admission":
        ok, reason = cto_local_preflight_rebind_ci_admission_proof(
            path, pr, head, repo, change_scope
        )
        return ok, reason, kind
    ok, reason = ci_start_override_proof(path, pr, head, repo)
    return ok, reason, kind


def sealed_mutation_scope_override_commit(
    args: argparse.Namespace, explicit_proof_kind: str, head: str
) -> str:
    """Atomically consume a one-time sealed override on full admission.

    Only the final label-control gate call (--commit-reentry, admitted
    pm-review-done/slot-ready source) consumes; preflight calls remain
    side-effect free. The
    read/check/rewrite is serialized by an exclusive flock on a well-known
    per-packet lock file, so exactly one concurrent or sequential invocation
    may replace the packet's `consumed: no` sentinel with `consumed: yes`
    plus a sha256 consumed_marker of the pre-rewrite packet bytes; every
    other invocation fails closed with ci_start_override_sealed_packet_reuse.
    The packet file remains the admission authority and the single
    consumption record; the lock file is advisory state only (never
    unlinked, so a third writer cannot race a lock release). Returns "" on
    success or no-op, else a fail-closed reason token.

    Applies to both sealed one-time classes: mutation_scope_sealed and
    cto_adjudicated_ci_admission. The generic sealed class is never weakened.
    """
    if explicit_proof_kind != "override" or not args.commit_reentry:
        return ""
    if args.source not in CI_START_OVERRIDE_SOURCES:
        return ""
    path = Path(args.affected_test_proof)
    lock_path = path.with_name(f"{path.name}.sealed-consume.lock")
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        return f"ci_start_override_sealed_commit_failed:{exc}"
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        except OSError as exc:
            return f"ci_start_override_sealed_commit_failed:{exc}"
        try:
            original = path.read_bytes()
        except OSError:
            return "ci_start_override_sealed_commit_unreadable"
        text = original.decode("utf-8", errors="replace")
        if ci_start_override_class(text) not in {
            "mutation_scope_sealed",
            "cto_adjudicated_ci_admission",
            "cto_rescue_packet_ci_admission",
            "cto_marker_pass_ci_admission",
            "cto_no_patch_rescue_ci_admission",
            "cto_exact_tuple_ci_admission",
            "cto_cancelled_run_local_preflight_ci_admission",
            "cto_local_preflight_rebind_ci_admission",
        }:
            return ""
        if not re.search(r"(?im)^consumed:\s*no\s*$", text) or re.search(
            r"^consumed_marker:\s*\S+\s*$", text, re.M | re.I
        ):
            return "ci_start_override_sealed_packet_reuse"
        marker = hashlib.sha256(original).hexdigest()
        new_text = re.sub(
            r"(?im)^consumed:\s*no\s*$",
            f"consumed: yes\nconsumed_marker: sha256:{marker}",
            text,
            count=1,
        )
        tmp = path.with_name(f".{path.name}.sealed-consume.{os.getpid()}.tmp")
        try:
            tmp.write_text(new_text, encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return f"ci_start_override_sealed_commit_failed:{exc}"
        return ""
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)


def local_auth_e2e_bootstrap_proof(path: Path, pr: int, head: str) -> tuple[bool, str]:
    """Validate a planner-produced proof that may start CI, but is not a pass."""
    expected_proof = Path(f"/tmp/affected-test-proof-{pr}-{head}.ok")
    expected_plan = Path(f"/tmp/affected-test-plan-{pr}-{head}.json")
    expected_log = Path(f"/tmp/affected-test-proof-{pr}-{head}.log")
    try:
        if path.resolve() != expected_proof.resolve():
            return False, "ci_bootstrap_path_not_exact"
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, "ci_bootstrap_unreadable"
    required = (
        "AFFECTED_TESTS: LOCAL_AUTH_E2E_DEFERRED_TO_CI",
        f"PR: {pr}",
        f"headRefOid: {head}",
        f"plan: {expected_plan}",
        f"log: {expected_log}",
        "no_full_suite: true",
        "defer_reason: slot_local_clerk_auth_unavailable",
        "final_ci_e2e_required: true",
    )
    if any(value not in text for value in required):
        return False, "ci_bootstrap_contract_missing"
    try:
        plan = json.loads(expected_plan.read_text(encoding="utf-8"))
        log_bytes = expected_log.read_bytes()
    except (OSError, ValueError):
        return False, "ci_bootstrap_artifact_unreadable"
    if plan.get("pr") != pr or plan.get("headRefOid") != head:
        return False, "ci_bootstrap_plan_tuple_mismatch"
    if Path(str(plan.get("proof_path") or "")).resolve() != expected_proof.resolve():
        return False, "ci_bootstrap_plan_proof_path_mismatch"
    if Path(str(plan.get("log_path") or "")).resolve() != expected_log.resolve():
        return False, "ci_bootstrap_plan_log_path_mismatch"
    digest_match = re.search(r"^log_sha256:\s*([0-9a-f]{64})\s*$", text, re.M)
    if not digest_match or digest_match.group(1) != hashlib.sha256(log_bytes).hexdigest():
        return False, "ci_bootstrap_log_digest_mismatch"

    commands = plan.get("commands") or []
    results = plan.get("verification_results") or []
    mandatory = set(plan.get("mandatory_local_e2e_targets") or [])
    if not commands or len(results) != len(commands) or not mandatory:
        return False, "ci_bootstrap_incomplete_results"
    allowed = {
        "passed",
        "passed_from_exact_preflight_receipt",
        "e2e_ci_validated_skipped_local",
        "mandatory_e2e_deferred_to_ci_local_auth_unavailable",
    }
    deferred: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for index, (command, result) in enumerate(zip(commands, results), 1):
        if result.get("command_index") != index or result.get("command") != command.get("command"):
            return False, "ci_bootstrap_result_command_mismatch"
        classification = str(result.get("classification") or "")
        if classification not in allowed:
            return False, "ci_bootstrap_nonpass_result"
        if classification == "mandatory_e2e_deferred_to_ci_local_auth_unavailable":
            if command.get("kind") != "e2e" or not (set(command.get("paths") or []) & mandatory):
                return False, "ci_bootstrap_deferred_target_not_mandatory_e2e"
            deferred.append((index, command, result))
    if not deferred:
        return False, "ci_bootstrap_missing_deferred_e2e"

    log_text = log_bytes.decode("utf-8", errors="replace")
    for index, command, _result in deferred:
        command_digest = hashlib.sha256(str(command.get("command") or "").encode("utf-8")).hexdigest()
        if not re.search(
            rf"^deferred_e2e:\s*command_index={index}\s+command_sha256={command_digest}\s*$",
            text,
            re.M,
        ):
            return False, "ci_bootstrap_deferred_command_digest_mismatch"
        section_match = re.search(
            rf"^## command {index}:.*?(?=^## command \d+:|^## command \d+ SKIPPED|\Z)",
            log_text,
            re.M | re.S,
        )
        section = section_match.group(0) if section_match else ""
        setup_failed = "Pre-authentication failed, writing empty storageState" in section
        missing_subject = "Authenticated Clerk subject is missing from E2E storage state" in section
        empty_auth = bool(
            re.search(
                r"tests/e2e/\.auth/admin\.json (?:is missing or empty|exists but does NOT contain a valid Clerk JWT subject)",
                section,
            )
        )
        if not setup_failed or not (missing_subject or empty_auth):
            return False, "ci_bootstrap_auth_wall_not_proven"
    return True, "ok"


SEED_ADMISSION_RECEIPT_SCHEMA = "heydonna_local_seed_admission_evidence"
SEED_ADMISSION_MIN_TIMINGS = 29_703
SEED_ADMISSION_FAILURE_SIGNATURE = "assertSeedIsLargeDeposition:zero_word_timings"


def seed_preflight_contract(command: str) -> dict[str, Any]:
    parts = shlex.split(command)
    script_index = next(
        (idx for idx, value in enumerate(parts) if value.endswith("scripts/e2e/local-repro-preflight.sh")),
        -1,
    )
    if script_index < 0:
        raise ValueError("seed command is not local-repro-preflight")
    args = parts[script_index + 1 :]
    mode = ""
    spec = ""
    workers = "1"
    prod_build = False
    branch_seed = False
    test_line = ""
    idx = 0
    while idx < len(args):
        arg = args[idx]
        if arg == "--full":
            mode = "full"
        elif arg == "--spec":
            idx += 1
            if idx >= len(args):
                raise ValueError("seed command --spec value missing")
            mode, spec = "spec", args[idx]
        elif arg.startswith("--spec="):
            mode, spec = "spec", arg.split("=", 1)[1]
        elif arg == "--workers":
            idx += 1
            if idx >= len(args):
                raise ValueError("seed command --workers value missing")
            workers = args[idx]
        elif arg.startswith("--workers="):
            workers = arg.split("=", 1)[1]
        elif arg == "--prod-build":
            prod_build = True
        elif arg == "--branch-seed":
            branch_seed = True
        elif arg == "--test-line":
            idx += 1
            if idx >= len(args):
                raise ValueError("seed command --test-line value missing")
            test_line = args[idx]
        elif arg.startswith("--test-line="):
            test_line = arg.split("=", 1)[1]
        else:
            raise ValueError(f"unsupported seed command argument: {arg}")
        idx += 1
    if mode not in {"full", "spec"} or (mode == "spec" and not spec):
        raise ValueError("seed command mode/spec invalid")
    contract: dict[str, Any] = {
        "mode": mode,
        "spec": spec,
        "workers": str(workers),
        "prod_build": prod_build,
        "branch_seed": branch_seed,
    }
    if test_line:
        contract["test_line"] = test_line
    return contract


def seed_contract_sha256(contract: dict[str, Any]) -> str:
    encoded = json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def seed_failure_only(output: str) -> bool:
    if "assertSeedIsLargeDeposition" not in output or "got 0" not in output:
        return False
    if not re.search(r"Seed validation FAIL:\s*[1-9][0-9]* pages, 0 word timings", output):
        return False
    error_lines = re.findall(r"^\s*Error:\s*(.+)$", output, re.M)
    if not error_lines or any("SEED_PROJECT_LONG must be a large deposition" not in line for line in error_lines):
        return False
    forbidden = (
        "Pre-authentication failed",
        "Authenticated Clerk subject is missing",
        "DEPLOY FAILED",
        "Build failed",
        "TimeoutError",
    )
    return not any(marker in output for marker in forbidden)


def gate_seed_receipt(
    path: Path,
    *,
    kind: str,
    pr: int,
    head: str,
    contract: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    try:
        raw = path.read_bytes()
        receipt = json.loads(raw)
        log_path = Path(str(receipt.get("log_path") or ""))
        log_bytes = log_path.read_bytes()
    except (OSError, ValueError):
        return None, f"ci_bootstrap_seed_{kind}_receipt_unreadable"
    expected_digest = seed_contract_sha256(contract)
    common = {
        "schema": SEED_ADMISSION_RECEIPT_SCHEMA,
        "version": 1,
        "kind": kind,
        "producer": "affected-test-plan.py",
        "pr": pr,
        "headRefOid": head,
        "command_contract": contract,
        "commands_sha256": expected_digest,
    }
    if any(receipt.get(key) != value for key, value in common.items()):
        return None, f"ci_bootstrap_seed_{kind}_tuple_mismatch"
    if receipt.get("log_sha256") != hashlib.sha256(log_bytes).hexdigest():
        return None, f"ci_bootstrap_seed_{kind}_log_digest_mismatch"
    slot = receipt.get("slot")
    checkout = str(receipt.get("checkout") or "")
    timings = receipt.get("word_timing_count")
    if (
        not isinstance(slot, int)
        or slot < 1
        or not Path(checkout).is_absolute()
        or not isinstance(timings, int)
        or timings < 0
    ):
        return None, f"ci_bootstrap_seed_{kind}_metrics_invalid"
    log_text = log_bytes.decode("utf-8", errors="replace")
    if kind == "failure":
        if (
            receipt.get("result") != "FAIL"
            or not isinstance(receipt.get("exit_code"), int)
            or receipt["exit_code"] == 0
            or not isinstance(receipt.get("page_count"), int)
            or receipt["page_count"] <= 0
            or timings != 0
            or receipt.get("failure_signatures") != [SEED_ADMISSION_FAILURE_SIGNATURE]
            or not seed_failure_only(log_text)
        ):
            return None, "ci_bootstrap_seed_failure_not_fail_closed"
    elif (
        receipt.get("result") != "PASS"
        or receipt.get("exit_code") != 0
        or receipt.get("content_loaded") is not True
        or timings < SEED_ADMISSION_MIN_TIMINGS
        or receipt.get("failure_signatures") not in ([], None)
        or not re.search(rf"(?:Store ready:\s*)?{timings}\s+timings", log_text)
        or re.search(r"^\s*[1-9][0-9]* failed(?:\s|$)", log_text, re.M)
        or not re.search(r"^\s*[1-9][0-9]* passed(?:\s|$)", log_text, re.M)
    ):
        return None, "ci_bootstrap_seed_pass_not_proven"
    return {
        **receipt,
        "receipt": str(path.resolve()),
        "receipt_sha256": hashlib.sha256(raw).hexdigest(),
    }, "ok"


def local_seed_admission_e2e_bootstrap_proof(path: Path, pr: int, head: str) -> tuple[bool, str]:
    """Validate the cross-clone seed differential that may start exact-head CI."""
    expected_proof = Path(f"/tmp/affected-test-proof-{pr}-{head}.ok")
    expected_plan = Path(f"/tmp/affected-test-plan-{pr}-{head}.json")
    expected_log = Path(f"/tmp/affected-test-proof-{pr}-{head}.log")
    try:
        if path.resolve() != expected_proof.resolve():
            return False, "ci_bootstrap_path_not_exact"
        text = path.read_text(encoding="utf-8", errors="replace")
        plan = json.loads(expected_plan.read_text(encoding="utf-8"))
        log_bytes = expected_log.read_bytes()
    except (OSError, ValueError):
        return False, "ci_bootstrap_artifact_unreadable"
    required = (
        "AFFECTED_TESTS: LOCAL_SEED_ADMISSION_E2E_DEFERRED_TO_CI",
        f"PR: {pr}",
        f"headRefOid: {head}",
        f"plan: {expected_plan}",
        f"log: {expected_log}",
        "no_full_suite: true",
        "defer_reason: local_seed_admission_unavailable",
        "final_ci_e2e_required: true",
    )
    if any(value not in text for value in required):
        return False, "ci_bootstrap_contract_missing"
    if plan.get("pr") != pr or plan.get("headRefOid") != head:
        return False, "ci_bootstrap_plan_tuple_mismatch"
    if Path(str(plan.get("proof_path") or "")).resolve() != expected_proof.resolve():
        return False, "ci_bootstrap_plan_proof_path_mismatch"
    if Path(str(plan.get("log_path") or "")).resolve() != expected_log.resolve():
        return False, "ci_bootstrap_plan_log_path_mismatch"
    log_digest = re.search(r"^log_sha256:\s*([0-9a-f]{64})\s*$", text, re.M)
    if not log_digest or log_digest.group(1) != hashlib.sha256(log_bytes).hexdigest():
        return False, "ci_bootstrap_log_digest_mismatch"
    commands = plan.get("commands") or []
    results = plan.get("verification_results") or []
    mandatory = set(plan.get("mandatory_local_e2e_targets") or [])
    if not commands or len(results) != len(commands) or not mandatory:
        return False, "ci_bootstrap_incomplete_results"
    allowed = {
        "passed",
        "passed_from_exact_preflight_receipt",
        "e2e_ci_validated_skipped_local",
        "mandatory_e2e_deferred_to_ci_local_seed_admission_unavailable",
    }
    deferred: list[tuple[int, dict[str, Any]]] = []
    for index, (command, result) in enumerate(zip(commands, results), 1):
        if result.get("command_index") != index or result.get("command") != command.get("command"):
            return False, "ci_bootstrap_result_command_mismatch"
        classification = str(result.get("classification") or "")
        if classification not in allowed:
            return False, "ci_bootstrap_nonpass_result"
        if classification == "mandatory_e2e_deferred_to_ci_local_seed_admission_unavailable":
            if deferred or command.get("kind") != "e2e" or not (set(command.get("paths") or []) & mandatory):
                return False, "ci_bootstrap_deferred_target_not_exactly_one_mandatory_e2e"
            deferred.append((index, command))
    if not deferred:
        return False, "ci_bootstrap_missing_deferred_e2e"
    index, command = deferred[0]
    command_text = str(command.get("command") or "")
    command_digest = hashlib.sha256(command_text.encode("utf-8")).hexdigest()
    if not re.search(rf"^deferred_e2e:\s*command_index={index}\s+command_sha256={command_digest}\s*$", text, re.M):
        return False, "ci_bootstrap_deferred_command_digest_mismatch"
    section_match = re.search(
        rf"^## command {index}:.*?(?=^## command \d+:|^## command \d+ SKIPPED|\Z)",
        log_bytes.decode("utf-8", errors="replace"),
        re.M | re.S,
    )
    if not section_match or not seed_failure_only(section_match.group(0)):
        return False, "ci_bootstrap_seed_failure_not_proven"
    differential = plan.get("local_seed_admission_differential") or {}
    try:
        contract = seed_preflight_contract(command_text)
    except ValueError:
        return False, "ci_bootstrap_seed_command_contract_invalid"
    expected_command_digest = seed_contract_sha256(contract)
    if differential.get("commands_sha256") != expected_command_digest:
        return False, "ci_bootstrap_seed_command_digest_mismatch"
    failure_path = Path(str(differential.get("failure_receipt") or ""))
    pass_path = Path(str(differential.get("pass_receipt") or ""))
    failure, reason = gate_seed_receipt(failure_path, kind="failure", pr=pr, head=head, contract=contract)
    if not failure:
        return False, reason
    passed, reason = gate_seed_receipt(pass_path, kind="pass", pr=pr, head=head, contract=contract)
    if not passed:
        return False, reason
    if failure["slot"] == passed["slot"] or Path(failure["checkout"]).resolve() == Path(passed["checkout"]).resolve():
        return False, "ci_bootstrap_seed_clones_not_independent"
    proof_fields = {
        "seed_failure_receipt": failure["receipt"],
        "seed_failure_receipt_sha256": failure["receipt_sha256"],
        "seed_pass_receipt": passed["receipt"],
        "seed_pass_receipt_sha256": passed["receipt_sha256"],
        "seed_command_sha256": expected_command_digest,
    }
    if any(not re.search(rf"^{key}:\s*{re.escape(str(value))}\s*$", text, re.M) for key, value in proof_fields.items()):
        return False, "ci_bootstrap_seed_proof_receipt_binding_mismatch"
    return True, "ok"
PM_REVIEW_RERUN_REENTRY_RECEIPT_DIR = Path("/tmp/pm-review-rerun-reentry-receipts")
# Second override class (CTO-directed, thread 1786288732.233219): one-time
# CI-fire admission for the documented mutation-scope limitation, where no
# genuine failed-Playwright preflight receipt exists to express the
# pre-existing-failure override class. CI-fire-only; current-head CI/E2E
# remains mandatory and no merge authority is granted.
SEALED_MUTATION_SCOPE_OVERRIDE_PROVENANCE = "pm-recorded-mutation-scope-sealed-exception"
# Third override class (CTO-adjudicated CI admission, #7249): a distinct
# provenance for an exact-head CTO decision that one real CI+E2E+applicable-LFC
# wave may start from a REQUEST_CHANGES Codex marker + VACUOUS_RED
# affected-test log when capture is provably not required. Never weakens the
# sealed mutation-scope class above.
CTO_ADJUDICATED_CI_ADMISSION_PROVENANCE = "cto-adjudicated-ci-admission"
# Fourth override class (#7227 sibling): a CTO degraded-delivery decision that
# one real current-head CI+E2E wave may start from a validated rescue packet
# (PATCH_READY, slot_actionable, skip_further_review) plus exact plan/log,
# canonical full-spec preflight PASS, classifier-bound capture-not-required,
# and source-parent diff equality. LFC is not required when the classifier
# says editor_changed=false. No merge authority; never weakens the sealed or
# plain CTO-adjudicated classes.
CTO_RESCUE_PACKET_CI_ADMISSION_PROVENANCE = "cto-rescue-packet-ci-admission"
CTO_DEGRADED_DELIVERY_DECISION_TS = "1786482754.837419"
CTO_DEGRADED_DELIVERY_ALIAS_TS = "1786482754.712249"
# Fifth override class (#7217 marker-PASS sibling): a CTO-adjudicated
# CI admission for an exact-head Phase-A PASS marker plus a mixed affected-test
# log (genuine mutation RED for changed-source commands and a correctly
# classified VACUOUS_RED for a branch-added collection-empty command), a
# canonical plan, the blocked_after_review receipt, a GREEN parent-head capture
# run receipt, the corpus-pin hash, and the source PM wake timestamp. One-time
# atomic consumption; mandatory current-head CI+E2E; no merge authority.
# Never weakens the sealed mutation-scope class or the other adjudicated
# classes above.
CTO_MARKER_PASS_CI_ADMISSION_PROVENANCE = "cto-marker-pass-vacuous-red-ci-admission"
# Sixth override class (#7268): a CTO-adjudicated NO_PATCH_REQUIRED rescue
# terminal (kimi3 packet with exact-head RED/GREEN receipt digests,
# changed_paths none, required_pm_action continue_verified_head). The CTO
# adjudication supersedes the ordinary envelope-shape provenance (MoP event
# log, producer/consumer grammar, receipt command shape) but every
# authority-bearing binding stays fail-closed. One-time atomic consumption;
# mandatory current-head CI+E2E; no merge authority; never weakens the sealed
# class or the other adjudicated classes.
CTO_NO_PATCH_RESCUE_CI_ADMISSION_PROVENANCE = "cto-no-patch-rescue-ci-admission"
CTO_NO_PATCH_RESCUE_INCIDENT = "control-plane:cto-direct-admission-unexecutable:7268"
CTO_NO_PATCH_RESCUE_DECISION_TS = (
    "1786583978.385839",
    "1786584297.602299",
    "1786584536.559899",
)
# Slack source thread for the incident.
CTO_NO_PATCH_RESCUE_SOURCE_THREAD = "1786497850.684469"
CTO_MARKER_PASS_SOURCE_TS = "1786492288.582469"
CTO_MARKER_PASS_SOURCE_WAKE = (
    "Slack C0ALZJHGE49 thread 1785572008.883029 ts 1786492288.582469"
)
# Seventh override class (exact-tuple CTO CI-start admissions, Rajiv thread
# 1786724301.511569 ts 1786725255.074339 + CTO decision thread
# 1786717451.157469 ts 1786724519.596549): a distinct provenance for exact
# allowlisted one-shot bindings for exactly three already-adjudicated tuples.
# mode=post-capture-preflight (PR 7275) consumes the already-sealed
# post-capture local-preflight CTO handoff despite a stale same-head
# review-loop breaker and discharges obligation 12959
# (ci_rerun_after_preflight / cto_ci_wave_required). mode=vacuous-red
# (PR 7289 test-only/new-shared-modules, PR 7331 test-only) validates exact
# evidence digests, capture_required=false, and the exact required gate set.
# One-time atomic consumption; mandatory current-head real CI+E2E (+applicable
# LFC); no merge authority; PM never fires CI. Never weakens the sealed class
# or the other adjudicated classes above.
CTO_EXACT_TUPLE_CI_ADMISSION_PROVENANCE = "cto-exact-tuple-ci-admission"
CTO_CANCELLED_RUN_LOCAL_PREFLIGHT_CI_ADMISSION_PROVENANCE = (
    "cto-cancelled-run-local-preflight-ci-admission"
)
# Ninth override class (#7308, CTO typed blocker ts 1786767400.760729): the
# one-shot local-preflight-rebind admission binds the sealed 32/32 local
# repro receipt/log/Fable-marker digests to a fresh clean detached checkout
# at the exact head/tree, overriding ONLY the checkout identity for
# validation. Atomic one-time consumption; CI-start only; no merge authority;
# never weakens the sealed class or the other adjudicated classes above.
CTO_LOCAL_PREFLIGHT_REBIND_CI_ADMISSION_PROVENANCE = (
    "cto-local-preflight-rebind-ci-admission"
)
CTO_EXACT_TUPLE_CI_ADMISSION_INCIDENT = (
    "control-plane:cto-exact-tuple-ci-admission:2026-08-14"
)
CTO_EXACT_TUPLE_ADMISSIONS = {
    7275: {
        "head": "517123fbdec371ade3becb4d19bfeaee033b78a9",
        "mode": "post-capture-preflight",
        "authorization_ts": "1786725255.074339",
        "source_wake": "C0ALZJHGE49/1786724301.511569",
        "required_gate_set": "ci+e2e+lfc",
        "capture_run": "31801964758",
        "obligation": ("12959", "ci_rerun_after_preflight", "cto_ci_wave_required"),
        "preflight_proof": "/tmp/ci-local-preflight-proof-7275-517123fbdec371ade3becb4d19bfeaee033b78a9.ok",
        "source_sha256": "07ca81034ea5497d4a6e1c515a4fabbd7bd8f8bd35e68d9ea34f6936ff8b5c46",
        "plan_sha256": "573753d64fc7eef81fb622394188b2ef0f2651cc1c6f91e100e3892fc446b716",
        "log_sha256": "add0b56edaf595fd01208713b3ffd68dda7fc4a70e7c82d215d542c9f8a52c11",
    },
    7289: {
        "head": "f7c16e84192b834ad73763a78430f9ec0c57b032",
        "mode": "vacuous-red",
        "authorization_ts": "1786724519.596549",
        "source_wake": "C0ALZJHGE49/1786717451.157469",
        "required_gate_set": "ci+e2e+lfc",
        "codex_marker": "/tmp/codex-app-code-review-7289.txt",
        "codex_marker_sha256": "5d0aaa077b85071b7a05670863526e203b449bc9581685d22bd9769dfdccd300",
        "plan": "/tmp/affected-test-plan-7289-f7c16e84192b834ad73763a78430f9ec0c57b032.json",
        "plan_sha256": "01053698388f67e162c9107105d2250fd2e66ef635555a2d1541d03e173b41b7",
        "log": "/tmp/affected-test-proof-7289-f7c16e84192b834ad73763a78430f9ec0c57b032.log",
        "log_sha256": "0295773daa55e2707acb2a88b9f28bfbce50813efca1f0fdc9263bd26660de3d",
    },
    7331: {
        "head": "b7f5c5851975fada85691d85447324b33eb35abc",
        "mode": "vacuous-red",
        "authorization_ts": "1786724519.596549",
        "source_wake": "C0ALZJHGE49/1786717451.157469",
        "required_gate_set": "ci+e2e",
        "pm_marker": "/tmp/pm-claude-code-review-7331-b7f5c5851975fada85691d85447324b33eb35abc.md",
        "pm_marker_sha256": "528d98ca6b1951730afeba4a68696f590385a917cde5d107e2c375af44cb8eb1",
        "codex_marker": "/tmp/codex-app-code-review-7331.txt",
        "codex_marker_sha256": "1efd0e22d911aa3b1f8b058cc349c3a862df0bf8b740c8a7a1d69d710cf24217",
        "plan": "/tmp/affected-test-plan-7331-b7f5c5851975fada85691d85447324b33eb35abc.json",
        "plan_sha256": "4ffa0b35987aff7d2367c1d55461c8013db85c4b0cf70abe01af1ff5f192aeac",
        "log": "/tmp/affected-test-proof-7331-b7f5c5851975fada85691d85447324b33eb35abc.log",
        "log_sha256": "2d93ad9ea7d0d77fee95c55cd9131f8638c00757d8363a480401e58e2b42322b",
        "run_receipt": "/tmp/cfr-7326-5run-b7f5c5851975fada85691d85447324b33eb35abc.log",
        "run_receipt_sha256": "5e8f749cda437d23fa90dbedb118c890f5fc5f6239d703d14412a61ff4835005",
    },
    7305: {
        "head": "14460f7e8193d3bbcd7a1932eff06487a2075098",
        "mode": "cancelled-run-local-preflight",
        "authorization_ts": "1786760957.087989",
        "source_wake": "C0ALZJHGE49/1786759192.277439",
        "required_gate_set": "ci+e2e",
        "failed_run": "31791875512",
        "preflight_proof": "/tmp/ci-local-preflight-proof-7305-14460f7e8193d3bbcd7a1932eff06487a2075098.ok",
        "preflight_sha256": "e1e3a24ce84640dde3a746745b885785be52ea3157abe0cbde4c69833bc384ee",
        "source_receipt": "/private/tmp/affected-test-preflight-receipts/14460f7e8193d3bbcd7a1932eff06487a2075098-99b0b89a1df8bd7483e374b679e8da1627d2cf68d45634100319977501da9a9c.json",
        "source_receipt_sha256": "7e4372d0ee8509052bd5b436cd94b746122abde83dd83c2945a6024889bf111c",
        "log": "/tmp/affected-test-proof-7305-14460f7e8193d3bbcd7a1932eff06487a2075098.log",
        "log_sha256": "b01929ed24ee11e92301d97c3436c88924674d1e675cffa01a10fdd970d7d49d",
    },
    7308: {
        "head": "559419e2629ec7d8105664d621f902644f09f509",
        "mode": "local-preflight-rebind",
        "authorization_ts": "1786767400.760729",
        "source_wake": "C0ALZJHGE49/1786759192.277439",
        "required_gate_set": "ci+e2e",
        "failed_run": "31791885167",
        "preflight_proof": "/tmp/ci-local-preflight-proof-7308-559419e2629ec7d8105664d621f902644f09f509.ok",
        "preflight_sha256": "1c3db63d6211424b60b3e44162c33af1fb1e9f3b44b372d13e0ad671821c6d04",
        "source_receipt": "/private/tmp/affected-test-preflight-receipts/559419e2629ec7d8105664d621f902644f09f509-c1313531c50a478ae569706c2e63782cdbdb604e1d6d3fdc5943a7749304a2aa.json",
        "source_receipt_sha256": "478b26c2eeaddb7f8fa3ede4da36675ae6fc68e73d478aeb8553883508d101d6",
        "log": "/private/tmp/local-repro-slot2-d9200c4fb2.log",
        "log_sha256": "db0b3cb2a429050690a8ffb477cedbb6399bc4a40ec2e0e5199479d9d0f134c7",
        "marker": "/tmp/pm-claude-code-review-7308-559419e2629ec7d8105664d621f902644f09f509.md",
        "marker_sha256": "c482326e3edc92c9853ccb555a3d2c782edc2e4656cc3d6b6a6507deaa45b37d",
        "obligations": (
            ("13007", "ci_rerun_after_preflight", "cto"),
            ("13110", "cto_ci_wave_required", "cto"),
        ),
    },
}
RESCUE_PACKET_ARTIFACT_ROOT = Path(
    os.environ.get(
        "RESCUE_PACKET_ARTIFACT_ROOT",
        "/Users/rajiv/.claude/control-plane-artifacts",
    )
)


def affected_test_proof(
    pr: int,
    head: str,
    repo: str = REPO,
    override_path: str = "",
    source: str = "",
    change_scope: dict[str, Any] | None = None,
    repo_root: Path | None = None,
) -> tuple[bool, str, str]:
    if override_path:
        path = Path(override_path)
        kind = classify_explicit_ci_start_proof(override_path, pr, head)
        if kind == "override":
            ok, reason, override_class = ci_start_override_dispatch(
                path, pr, head, repo, change_scope, repo_root
            )
            if override_class == "mutation_scope_sealed":
                return ok, str(path), f"sealed_override_{reason}"
            if override_class == "cto_adjudicated_ci_admission":
                return ok, str(path), f"cto_adjudicated_override_{reason}"
            if override_class == "cto_rescue_packet_ci_admission":
                return ok, str(path), f"cto_rescue_packet_override_{reason}"
            if override_class == "cto_marker_pass_ci_admission":
                return ok, str(path), f"cto_marker_pass_override_{reason}"
            if override_class == "cto_no_patch_rescue_ci_admission":
                return ok, str(path), f"cto_no_patch_rescue_override_{reason}"
            if override_class == "cto_exact_tuple_ci_admission":
                return ok, str(path), f"cto_exact_tuple_override_{reason}"
            if override_class == "cto_cancelled_run_local_preflight_ci_admission":
                return ok, str(path), f"cto_cancelled_run_local_preflight_override_{reason}"
            if override_class == "cto_local_preflight_rebind_ci_admission":
                return ok, str(path), f"cto_local_preflight_rebind_override_{reason}"
            return ok, str(path), f"override_{reason}"
        if kind == "rejected":
            return False, str(path), "explicit_path_not_canonical"
        # kind == "ordinary": the exact canonical affected-test proof path —
        # validate through the ordinary proof contract below.
    candidates = [
        Path(f"/tmp/affected-test-proof-{pr}-{head}.ok"),
        Path(f"/tmp/affected-test-proof-{pr}-{head[:8]}.ok"),
    ]
    for path in candidates:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if f"headRefOid: {head}" not in text:
            return False, str(path), "head_mismatch"
        if change_scope and change_scope.get("control_plane_only") is True:
            required_scope_lines = (
                "change_scope: control_plane_only",
                f"change_scope_head: {head}",
                f"change_scope_rules_sha256: {change_scope.get('rules_sha256') or ''}",
                f"change_scope_classifier_sha256: {change_scope.get('classifier_sha256') or ''}",
            )
            if any(
                not re.search(
                    rf"^{re.escape(line)}(?:\s|$)",
                    text,
                    re.M,
                )
                for line in required_scope_lines
            ):
                return False, str(path), "control_plane_scope_receipt_mismatch"
        status = proof_pass_status(text, "AFFECTED_TESTS")
        if not status:
            return False, str(path), "missing_pass_marker"
        if status == "LOCAL_AUTH_E2E_DEFERRED_TO_CI":
            if source != "pm-review-done":
                return False, str(path), "ci_bootstrap_requires_pm_review_done_source"
            ok, reason = local_auth_e2e_bootstrap_proof(path, pr, head)
            return ok, str(path), reason
        if status == "LOCAL_SEED_ADMISSION_E2E_DEFERRED_TO_CI":
            if source != "pm-review-done":
                return False, str(path), "ci_bootstrap_requires_pm_review_done_source"
            ok, reason = local_seed_admission_e2e_bootstrap_proof(path, pr, head)
            return ok, str(path), reason
        if status == "TARGETED_CI_PASS" and not targeted_ci_proof_ok(text, pr, head, repo):
            return False, str(path), "targeted_ci_proof_invalid"
        if status != "TARGETED_CI_PASS" and not proof_status_allowed(status, text):
            return False, str(path), "missing_followup_issue_for_exception_pass"
        if status in {"PASS_WITH_PREEXISTING_FAILURES", "NO_LOCAL_EQUIVALENT"}:
            issue = followup_issue_number(text)
            if issue is None:
                return False, str(path), "missing_followup_issue_for_exception_pass"
            try:
                validate_live_followup_issue(
                    str(issue),
                    repo=repo,
                    cwd=str(Path(__file__).resolve().parent),
                )
            except ValueError as exc:
                return (
                    False,
                    str(path),
                    f"followup_issue_contract_invalid:{exc}",
                )
        if not re.search(r"^no_full_suite:\s*true(?:\s|$)", text, re.M):
            return False, str(path), "missing_no_full_suite_true"
        return True, str(path), "ok"
    return False, "", "missing"


def local_preflight_proof(
    pr: int, head: str, rebind_checkout: str = ""
) -> tuple[bool, str]:
    validator = Path(__file__).resolve().parent / "local-preflight-proof.py"
    if not validator.is_file():
        return False, ""
    candidates = [
        Path(f"/tmp/ci-local-preflight-proof-{pr}-{head}.ok"),
        Path(f"/tmp/ci-local-preflight-proof-{pr}-{head[:8]}.ok"),
    ]
    for path in candidates:
        if not path.exists():
            continue
        cmd = [
            "python3",
            str(validator),
            "validate",
            "--pr",
            str(pr),
            "--head",
            head,
            "--proof",
            str(path),
        ]
        if rebind_checkout:
            cmd += ["--rebind-checkout", rebind_checkout]
        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0:
            return True, str(path)
    return False, ""


def post_capture_local_repro_ci_start_admission(
    pr: int,
    head: str,
    repo: str,
    proof_file: str,
) -> tuple[bool, str]:
    """Fail-closed admission for the post-capture-local-repro CI-start class.

    A PR whose head carries a historical FAILED required CI/E2E run is
    normally refused with
    ``current_head_ci_or_e2e_failed_use_rerun_not_label_trigger``. The
    post-capture-local-repro class is the one typed exception: after a
    successful exact-head remote capture changed external fixtures, the
    owning slot re-ran the canonical local repro (5/5 PASS) and sealed a
    ``ci_class: post-capture-local-repro`` exact-head local-preflight
    envelope. That tuple is admitted for exactly one CTO-owned label-gated
    CI+E2E wave.

    Conditions validated here (of the four-condition admission contract):
      1. a valid sealed post-capture-local-repro proof at the exact head —
         the caller passes the path already validated by
         ``local_preflight_proof`` (validate ok:true); this function re-checks
         the sealed ``ci_class: post-capture-local-repro`` marker, PR, head,
         PASS status, and a numeric ``failed_run``;
      2. a successful exact-head capture run — ``capture_green`` at the exact
         head;
      4. no active real exact-head wave — guaranteed by the caller: the
         ``workflows["state"] == "in_progress"`` branch fires before the
         failed branch that consults this function.

    Condition 3 (exactly one OPEN ``cto_ci_wave_required`` obligation bound
    to the PR/head) is consumed atomically by the pm-review-done transition
    (``resolve_pr_obligation_kinds``) after the gate admits; the gate never
    queries the obligation ledger and never fires CI.

    Wrong head/run/class, a non-PASS proof, a missing ``failed_run``, or a
    non-green capture fails closed with a typed reason. Never weakens the
    sealed/other adjudicated CI-start classes.

    The caller additionally gates admission on the authoritative
    ``pm-review-done`` transition source (``args.source == "pm-review-done"``);
    any other source fails closed with
    ``post_capture_local_repro_requires_pm_review_done_source`` before this
    validator runs.
    """
    try:
        proof_path = Path(proof_file)
        proof = proof_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, "post_capture_proof_unreadable"
    if not re.search(r"^CI_LOCAL_PREFLIGHT:\s*PASS\s*$", proof, re.M):
        return False, "post_capture_proof_not_pass"
    if not re.search(rf"^PR:\s*{pr}\s*$", proof, re.M):
        return False, "post_capture_proof_pr_mismatch"
    if not re.search(rf"^headRefOid:\s*{re.escape(head)}\s*$", proof, re.M):
        return False, "post_capture_proof_head_mismatch"
    if not re.search(r"^ci_class:\s*post-capture-local-repro\s*$", proof, re.M):
        return False, "post_capture_proof_class_mismatch"
    if not re.search(r"^failed_run:\s*\d+\s*$", proof, re.M):
        return False, "post_capture_proof_failed_run_missing"
    ok, detail = capture_green(head, repo, pr)
    if not ok:
        return False, (
            "post_capture_capture_not_green "
            f"detail={detail.get('state') or 'unknown'} "
            f"run={detail.get('run_id') or 'none'}"
        )
    return True, "ok"


def local_capture_proof(pr: int, head: str, failed_run: str | None = None) -> tuple[bool, str, str]:
    candidates = [
        Path(f"/tmp/capture-local-proof-{pr}-{head}.ok"),
        Path(f"/tmp/capture-local-proof-{pr}-{head[:8]}.ok"),
    ]
    pass_re = re.compile(r"^CAPTURE_LOCAL:\s*(PASS|PASS_NOT_REQUIRED)(?:\s|$)", re.M)
    for path in candidates:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if f"headRefOid: {head}" not in text:
            return False, str(path), "head_mismatch"
        if failed_run and f"failed_remote_run: {failed_run}" not in text:
            return False, str(path), "failed_run_mismatch"
        pass_match = pass_re.search(text)
        if not pass_match:
            return False, str(path), "missing_pass_marker"
        if pass_match.group(1) == "PASS_NOT_REQUIRED" and not re.search(
            r"^fixture_capture_not_required_proof:\s*no_fixture_producing_request(?:\s|$)",
            text,
            re.M,
        ):
            return False, str(path), "pass_not_required_without_no_fixture_proof"
        if not re.search(r"^label_gated_ci_allowed_after_local_capture:\s*true(?:\s|$)", text, re.M):
            return False, str(path), "missing_local_capture_ci_allowance"
        if not re.search(r"^capture_format_only:\s*(true|false)(?:\s|$)", text, re.M):
            return False, str(path), "legacy_capture_proof_missing_capture_mode"
        if not re.search(
            r"^fixture_capture_policy:\s*update_all_llm_proxy_cache_misses_on_selected_auto_process_path(?:\s|$)",
            text,
            re.M,
        ):
            return False, str(path), "missing_fixture_capture_policy"
        if re.search(r"^capture_format_only:\s*true(?:\s|$)", text, re.M):
            coverage_match = re.search(r"^capture_template_coverage:\s*(.+)$", text, re.M)
            coverage = coverage_match.group(1).strip().lower() if coverage_match else ""
            targeted_playwright = bool(
                re.search(r"^capture_harness:\s*playwright_specs(?:\s|$)", text, re.M)
                and coverage == "playwright_specs"
                and re.search(r"^capture_template_specs:\s*\[[^]]+\](?:\s|$)", text, re.M)
                and re.search(r"^failed_remote_run:\s*\d+(?:\s|$)", text, re.M)
                and re.search(r"^required_fixture_keys:\s*(?!none(?:\s|$))\S+", text, re.M)
                and re.search(r"^fixture_verified_keys:\s*(?!none(?:\s|$))\S+", text, re.M)
                and re.search(r"^targeted_fixture_proof:\s*true(?:\s|$)", text, re.M)
                and re.search(r"^targeted_fixture_evidence:\s*modal_log_plus_r2_head(?:\s|$)", text, re.M)
            )
            if ("ny_standard" not in coverage or "acr" not in coverage) and not targeted_playwright:
                return False, str(path), "format_only_capture_missing_ny_acr_coverage"
        else:
            if not re.search(r"^sc_writeback_assertions_required:\s*true(?:\s|$)", text, re.M):
                return False, str(path), "full_capture_missing_sc_writeback_assertion_contract"
        # Accept EITHER the legacy merge-flag proof OR the newer exact-head
        # contract. Both prove the capture ran on the current PR head (which for
        # these branches contains origin/main). The seeded-capture wrapper emits
        # the exact-head form (current_head_verified_before_capture + exact_pr_head)
        # instead of the legacy origin_main_merged_before_capture fields.
        _legacy_merge = bool(
            re.search(r"^origin_main_merged_before_capture:\s*true(?:\s|$)", text, re.M)
            and re.search(r"^main_merge_status:\s*already_contains_origin_main(?:\s|$)", text, re.M)
        )
        _exact_head = bool(
            re.search(r"^current_head_verified_before_capture:\s*true(?:\s|$)", text, re.M)
            and re.search(r"^current_head_status:\s*exact_pr_head(?:\s|$)", text, re.M)
        )
        if not (_legacy_merge or _exact_head):
            return False, str(path), "missing_origin_main_merge"
        if not re.search(r"^strict_replay_after_capture_required:\s*true(?:\s|$)", text, re.M):
            return False, str(path), "missing_strict_replay_contract"
        if not re.search(r"^full_ci_e2e_same_head_required_after_capture:\s*true(?:\s|$)", text, re.M):
            return False, str(path), "missing_same_head_full_ci_contract"
        return True, str(path), "ok"
    return False, "", "missing"


def cached_capture_requirement(pr: int, repo: str, head: str) -> dict[str, Any] | None:
    path = CAPTURE_REQUIREMENT_CACHE_DIR / f"pm-capture-required-{pr}.json"
    try:
        info = path.lstat()
    except OSError:
        return None
    age_seconds = time.time() - info.st_mtime
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or age_seconds < -5
        or age_seconds > CAPTURE_REQUIREMENT_CACHE_MAX_AGE_SECONDS
    ):
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if (
        type(data.get("capture_required")) is not bool
        or data.get("pr") != pr
        or data.get("repo") != repo
        or data.get("headRefOid") != head
    ):
        return None
    result = dict(data)
    result["cache"] = {
        "path": str(path),
        "age_seconds": round(age_seconds, 3),
        "identity": "same-user-exact-pr-repo-head",
    }
    return result


def capture_requirement(pr: int, repo: str, head: str) -> dict[str, Any]:
    cached = cached_capture_requirement(pr, repo, head)
    if cached is not None:
        return cached
    if not CAPTURE_REQUIRED.exists():
        return {"capture_required": None, "reason": "capture_classifier_missing", "script": str(CAPTURE_REQUIRED)}
    proc = subprocess.run(
        ["python3", str(CAPTURE_REQUIRED), "--pr", str(pr), "--repo", repo, "--json"],
        text=True,
        capture_output=True,
        timeout=35,
        env={**os.environ, "PATH": f"{CANONICAL_PATH}:{os.environ.get('PATH', '')}"},
    )
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        data = {}
    if proc.returncode not in {0, 1}:
        return {
            "capture_required": None,
            "reason": "capture_classifier_error",
            "stderr": (proc.stderr or "")[-500:],
        }
    if (
        not isinstance(data, dict)
        or type(data.get("capture_required")) is not bool
        or data.get("pr") != pr
        or data.get("repo") != repo
        or data.get("headRefOid") != head
    ):
        return {
            "capture_required": None,
            "reason": "capture_classifier_identity_mismatch",
            "expected": {"pr": pr, "repo": repo, "headRefOid": head},
            "observed": {
                "pr": data.get("pr") if isinstance(data, dict) else None,
                "repo": data.get("repo") if isinstance(data, dict) else None,
                "headRefOid": data.get("headRefOid") if isinstance(data, dict) else None,
            },
        }
    data.setdefault("capture_required", proc.returncode == 0)
    return data


def capture_green(head: str, repo: str, pr_number: int | None = None, branch: str = "") -> tuple[bool, dict[str, Any]]:
    runs = gh_json(
        [
            "run",
            "list",
            "--repo",
            repo,
            "--workflow",
            "E2E LLM Proxy Capture (manual)",
            "--limit",
            "50",
            "--json",
            "databaseId,displayTitle,headSha,status,conclusion,createdAt,updatedAt,url,attempt",
        ],
        timeout=20,
    )
    expected_title = f"remote-capture-pr-{pr_number}-head-{head}" if pr_number is not None else ""
    matches = [
        run
        for run in runs
        if (
            str(run.get("displayTitle") or "") == expected_title
            if REMOTE_CAPTURE_ONLY and expected_title
            else str(run.get("headSha") or "") == head
        )
    ]
    if not matches:
        return False, {"state": "missing", "detail": "no current-head capture run"}
    matches.sort(key=lambda r: parse_time(r.get("createdAt")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    run = matches[0]
    state = str(run.get("status") or "").lower()
    conclusion = str(run.get("conclusion") or "").lower()
    run_ok, run_detail = capture_run_success(
        str(run.get("databaseId") or ""),
        repo,
        expected_pr=pr_number,
        expected_head=head,
        expected_branch=branch,
    )
    ok = state == "completed" and conclusion == "success" and run_ok
    return ok, {
        "state": "success" if ok else state or "unknown",
        "conclusion": conclusion,
        "capture_run_conclusion": run_detail.get("capture_run_conclusion", ""),
        "detail": run_detail.get("detail", ""),
        "run_id": run.get("databaseId"),
        "url": run.get("url"),
    }


def latest_stale_terminal_bad_capture(pr: dict[str, Any], head: str, repo: str) -> dict[str, Any]:
    branch = str(pr.get("headRefName") or "")
    if not branch:
        return {}
    runs = gh_json(
        [
            "run",
            "list",
            "--repo",
            repo,
            "--workflow",
            "E2E LLM Proxy Capture (manual)",
            "--branch",
            branch,
            "--limit",
            "50",
            "--json",
            "databaseId,headSha,status,conclusion,createdAt,updatedAt,url,attempt",
        ],
        timeout=20,
    )
    bad = []
    for run in runs:
        if str(run.get("headSha") or "") == head:
            continue
        if str(run.get("status") or "").lower() != "completed":
            continue
        conclusion = str(run.get("conclusion") or "").lower()
        if conclusion not in BAD_TERMINAL_CONCLUSIONS:
            continue
        bad.append(run)
    if not bad:
        return {}
    bad.sort(key=lambda r: parse_time(r.get("createdAt")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    run = bad[0]
    return {
        "run_id": str(run.get("databaseId") or ""),
        "head": str(run.get("headSha") or ""),
        "conclusion": str(run.get("conclusion") or "").lower(),
        "url": run.get("url"),
    }


def workflow_state(pr: dict[str, Any], repo: str) -> dict[str, Any]:
    branch = str(pr.get("headRefName") or "")
    head = str(pr.get("headRefOid") or "")
    if not branch or not head:
        return {"state": "unknown", "runs": [], "detail": "missing branch/head"}
    runs = gh_json(
        [
            "run",
            "list",
            "--repo",
            repo,
            "--branch",
            branch,
            "--limit",
            "100",
            "--json",
            "databaseId,workflowName,event,status,conclusion,headSha,createdAt,updatedAt,url",
        ],
        timeout=20,
    )
    current = [
        run
        for run in runs
        if run.get("event") == "pull_request"
        and run.get("workflowName") in REQUIRED_WORKFLOWS
        and str(run.get("headSha") or "") == head
        # Label-gated flow: every push spawns pull_request-event CI/E2E runs that
        # SKIP by design (only `labeled` events execute, and GitHub reports those
        # as event=pull_request too). A run that actually started never ends
        # `skipped` here, so completed+skipped == label-gate exhaust, not an
        # attempt — excluding it keeps `skipped_existing_runs` for real attempts
        # only (PR #5968 d56c167 false-block, 2026-07-02).
        and not (
            str(run.get("status") or "").lower() == "completed"
            and str(run.get("conclusion") or "").lower() == "skipped"
        )
    ]
    stale_bad = [
        run
        for run in runs
        if run.get("event") == "pull_request"
        and run.get("workflowName") in REQUIRED_WORKFLOWS
        and str(run.get("headSha") or "") != head
        and str(run.get("status") or "").lower() == "completed"
        and str(run.get("conclusion") or "").lower() in BAD_TERMINAL_CONCLUSIONS
    ]
    if not REQUIRED_CI_JOBS:
        return {
            "state": "unknown",
            "runs": [],
            "detail": "required_job_contract_missing",
        }
    ci_verdicts = current_head_ci_verdicts(
        int(pr.get("number") or 0), head, repo
    )
    states: list[dict[str, Any]] = []
    for workflow in sorted(REQUIRED_WORKFLOWS):
        required_job = REQUIRED_CI_JOBS[workflow]
        matches = [r for r in current if r.get("workflowName") == workflow]
        if not matches:
            states.append(
                {
                    "workflow": workflow,
                    "state": "missing",
                    "run_id": None,
                    "current_head_attempt_count": 0,
                    "terminal_failed_run_ids": [],
                    "terminal_success_run_ids": [],
                    "nonblocking_cancelled_run_ids": [],
                }
            )
            continue
        matches.sort(key=lambda r: parse_time(r.get("createdAt")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        run = matches[0]
        status = str(run.get("status") or "").lower()
        conclusion = str(run.get("conclusion") or "").lower()
        required_terminal: list[dict[str, Any]] = []
        active = False
        resolved = False
        skipped_only = False
        nonblocking_cancelled_run_ids: list[int] = []
        for candidate in matches:
            run_id = str(candidate.get("databaseId") or "")
            if not run_id:
                continue
            try:
                payload = gh_json(
                    ["api", f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"],
                    timeout=20,
                )
                jobs = payload.get("jobs") or []
            except Exception:
                jobs = []
            required = next(
                (
                    job
                    for job in jobs
                    if str(job.get("name") or "") == required_job
                ),
                None,
            )
            if required is None:
                continue
            job_status = str(required.get("status") or "").lower()
            job_conclusion = str(required.get("conclusion") or "").lower()
            if job_status != "completed":
                active = True
                run = candidate
                status = job_status
                conclusion = job_conclusion
                break
            if job_conclusion in {"skipped", "neutral"}:
                skipped_only = True
                continue
            if (
                job_conclusion == "cancelled"
                and nonblocking_concurrency_cancel(ci_verdicts.get(run_id, {}))
            ):
                nonblocking_cancelled_run_ids.append(int(run_id))
                continue
            required_terminal.append(
                {
                    "run": candidate,
                    "conclusion": job_conclusion,
                }
            )
            resolved = True
            run = candidate
            status = job_status
            conclusion = job_conclusion
            break
        if not active and not resolved and skipped_only:
            status = "completed"
            conclusion = "skipped"
        elif not active and not resolved:
            status = "completed"
            conclusion = ""
        if active:
            state = "in_progress"
        elif conclusion == "success":
            state = "success"
        elif conclusion == "skipped":
            state = "skipped"
        elif conclusion == "cancelled":
            state = "cancelled"
        elif conclusion:
            state = "failed"
        else:
            state = "unknown"
        states.append(
            {
                "workflow": workflow,
                "state": state,
                "run_id": run.get("databaseId"),
                "status": status,
                "conclusion": conclusion,
                "url": run.get("url"),
                "current_head_attempt_count": len(matches),
                "required_job": required_job,
                "terminal_failed_run_ids": [
                    item["run"].get("databaseId")
                    for item in required_terminal
                    if item["conclusion"] in BAD_TERMINAL_CONCLUSIONS
                ],
                "terminal_success_run_ids": [
                    item["run"].get("databaseId")
                    for item in required_terminal
                    if item["conclusion"] == "success"
                ],
                "nonblocking_cancelled_run_ids": nonblocking_cancelled_run_ids,
            }
        )
    if any(item["state"] == "in_progress" for item in states):
        state = "in_progress"
    elif any(item["state"] == "failed" for item in states):
        state = "failed"
    elif any(item["state"] == "cancelled" for item in states):
        state = "cancelled"
    elif all(item["state"] == "success" for item in states):
        state = "green"
    elif all(item["state"] in {"missing", "skipped"} for item in states):
        state = "not_started"
    else:
        state = "partial"
    return {
        "state": state,
        "runs": states,
        "current_head_attempt_count": len([s for s in states if s.get("state") not in {"missing", "skipped"}]),
        "stale_bad_run_count": len(stale_bad),
        "stale_bad_run_ids": [run.get("databaseId") for run in stale_bad[:10]],
    }


def required_actions(reasons: list[str]) -> list[str]:
    actions: list[str] = []
    for reason in reasons:
        if reason.startswith("head_drift "):
            actions.append("Refresh live PR head, rerun affected-test/capture checks on the new head, then retry the CI readiness gate.")
        elif reason.startswith("pr_not_open "):
            actions.append("Stop CI start; reconcile/cleanup the closed or non-open PR state instead.")
        elif reason.startswith("merge_state_blocks_ci_start"):
            actions.append("Resolve merge conflict/rebase/mergeability blocker before starting label-gated CI.")
        elif reason.startswith("active_blocking_labels="):
            actions.append("Resolve the active pm-blocked/blocked-rework labels or move the PR to the correct blocked/rescope state before CI.")
        elif reason.startswith("moving_head_recent_push_age_min="):
            actions.append("Do not start label-gated CI on a moving head. Wait until the latest commit is stable for the required window, rerun the CI readiness gate, then apply the CI-start transition.")
        elif reason.startswith("capture_requirement_unknown"):
            actions.append("Repair or rerun the capture-required classifier before deciding whether CI can start.")
        elif reason.startswith("qa_visual_proof_gate_"):
            actions.append(
                "Complete the issue's deterministic visual AC scenarios on the exact PR head, upload screenshots to a durable Actions artifact, GitHub attachment, or R2 object, post a head/body-bound qa-visual-proof receipt on the PR, then rerun the gate."
            )
        elif reason.startswith("capture_remote_terminal_bad"):
            actions.append("Classify the failed remote capture run. Infrastructure failures may retry through pm-transition capture-remote-dispatch --pr <PR> --retry-run <RUN>; deterministic failures route to product rework. Local capture is diagnostic-only and is never a fallback after remote red.")
        elif reason.startswith("capture_stale_terminal_bad"):
            actions.append("A prior remote capture failed/cancelled on this branch and the current head has no authoritative remote capture proof. Classify the failed run; infrastructure failures may retry through pm-transition capture-remote-dispatch --pr <PR> --retry-run <RUN>, deterministic failures route to product rework. Local capture is diagnostic-only and never satisfies capture readiness.")
        elif reason.startswith("capture_required_not_green"):
            actions.append("Run pm-transition capture-remote-dispatch --pr <PR>. Keep the dev slot released while PM watches the remote run; consume success only with capture-remote-pass --pr <PR> --run <RUN_ID>. Local capture is diagnostic-only and does not satisfy capture readiness.")
        elif reason == "current_head_ci_or_e2e_already_in_progress":
            actions.append("Do not relabel/retrigger; watch the current-head CI/E2E run to terminal state and process the result.")
        elif reason == "current_head_ci_or_e2e_already_terminal_green":
            actions.append("Do not start CI again. Run PM readiness/merge-ready reconciliation for the current head instead.")
        elif reason == "current_head_ci_or_e2e_cancelled_requires_investigation":
            actions.append("Current-head CI/E2E was cancelled without a test-failure verdict. Run ci-status-investigator for the cancelled run; do not classify it as a PR-local failure or require local preflight unless the investigator proves one.")
        elif reason.startswith("current_head_ci_verdict_requires_rework"):
            actions.append("Do not rerun capture or CI. The current-head CI verdict says recapture/local proof will not fix this failure. Keep/add pm-blocked:ci and dispatch the PR back to the owning slot with the verdict comment as the rework packet.")
        elif reason.startswith("ci_stale_run_classified_"):
            actions.append(
                "Run the typed closure pm-transition ci-stale-run-classified --pr <PR> --run <RUN> only when the named failed run is superseded (its head != the current head). No affected-test proof or sealed local-preflight receipt is required (Rajiv 1786812200.371389); a fresh exact-head CI+E2E wave follows."
            )
        elif reason.startswith("current_head_ci_or_e2e_failed_use_rerun_not_label_trigger"):
            actions.append(
                "Current-head CI/E2E is red; do not re-trigger through the label. "
                "Product/uncertain classes route to block + assign-rework (the slot "
                "reproduces and fixes the failed surface, then a descendant head "
                "starts one fresh wave). Infra/flake/shared classes route to at most "
                "one same-head rerun through .claude/scripts/ci/rerun-after-local-proof.sh "
                "--pr <PR> --run <RUN_ID>; no sealed local-preflight proof is required."
            )
        elif reason in {"current_head_ci_or_e2e_partial_existing_runs", "current_head_ci_or_e2e_skipped_existing_runs"}:
            actions.append("Existing current-head CI/E2E evidence is partial/skipped. Do not label-toggle. Inspect workflow conditions or use the failed-run rerun path; if terminal green, run readiness.")
        else:
            actions.append(f"Inspect and resolve gate reason before CI: {reason}")
    deduped: list[str] = []
    for action in actions:
        if action not in deduped:
            deduped.append(action)
    return deduped


def primary_required_action(actions: list[str]) -> str | None:
    if not actions:
        return None
    generic_prefixes = (
        "Resolve the active pm-blocked/blocked-rework labels",
        "Run the affected local tests only",
    )
    priority_terms = (
        "Merge/rebase origin/main",
        "/Users/rajiv/.claude/scripts/capture-local-proof.sh",
        "request-label-gated-ci.sh",
        "full strict same-head CI/E2E",
        "rerun-after-local-proof.sh",
    )
    for action in actions:
        if any(term in action for term in priority_terms):
            return action
    for action in actions:
        if not any(action.startswith(prefix) for prefix in generic_prefixes):
            return action
    return actions[0]


def drop_capture_blocker_reason(reasons: list[str]) -> None:
    updated: list[str] = []
    for reason in reasons:
        if not reason.startswith("active_blocking_labels="):
            updated.append(reason)
            continue
        blockers = [
            item for item in reason.split("=", 1)[1].split(",")
            if item and item != "pm-blocked:capture"
        ]
        if blockers:
            updated.append(f"active_blocking_labels={','.join(blockers)}")
    reasons[:] = updated


def ci_start_blocking_labels(
    labs: list[str],
    source: str,
    rescue_authorized: bool = False,
    phase_a_authorized: bool = False,
    ci_stale_run_admitted: bool = False,
    no_patch_rescue_authorized: bool = False,
) -> tuple[list[str], list[str]]:
    blocking = [label for label in labs if label in BLOCKING_LABELS]
    if source not in TRANSITION_SOURCES:
        return blocking, []
    if source == "slot-ready-rescue" or (
        source == "pm-review-done" and rescue_authorized
    ):
        owned_labels = RESCUE_TRANSITION_OWNED_LABELS
        if no_patch_rescue_authorized:
            # CTO-adjudicated NO_PATCH_REQUIRED rescue consumption (#7268):
            # the canonical override at /tmp/pm-ci-start-override-<pr>-<head>.ok
            # discharges pm-blocked:pm-gate (the transition clears it AFTER
            # the gate admits, so the live label must not block the start).
            owned_labels = owned_labels | {"pm-blocked:pm-gate"}
    elif source == "pm-review-done" and phase_a_authorized:
        # A current-head Phase-A PASS discharges only the Codex review blocker.
        # Independent CI/capture/infra/product/dependency blockers remain closed.
        owned_labels = TRANSITION_OWNED_LABELS | {"pm-blocked:codex"}
    else:
        owned_labels = TRANSITION_OWNED_LABELS
    if ci_stale_run_admitted:
        # The typed ci-stale-run-classified closure (holds validated
        # end-to-end by ci_stale_run_classified_admission) discharges ONLY the
        # pm-blocked:ci blocker. Every sibling blocker stays closed and is
        # preserved on the PR; this entry never extends any other label class.
        owned_labels = set(owned_labels) | {"pm-blocked:ci"}
    transition_owned = [label for label in blocking if label in owned_labels]
    remaining = [label for label in blocking if label not in owned_labels]
    return remaining, transition_owned


def ci_stale_run_classified_admission(
    pr: int, head: str, run_id: int, repo: str
) -> tuple[bool, str]:
    """Validate the typed stale-run CI-blocker closure (incident
    cp-repair:ci-stale-run-classified:7281:2026-08-13, CTO disposition
    CI_BLOCKER_CLEAR_TRANSITION_MISSING).

    Four holds, ALL required, fail closed otherwise:

    1. exact current head — enforced by the ordinary evaluate() path through
       --expect-head and the live headRefOid binding;
    2. authenticated Fable marker PASS at the exact head with
       `blocker_reviewed: pm-blocked:ci` at the canonical marker path
       /tmp/pm-claude-code-review-<PR>-<head>.md (review_model fable);
    3. the named failed run is a completed, terminal-bad, pull_request-event
       run of a REQUIRED workflow whose headSha is a SUPERSEDED head (never
       the current head). A current-head run refuses.

    Sealed local-preflight and affected-test proof holds are retired (Rajiv
    1786812200.371389); the superseded-run binding plus the exact-head review
    marker are sufficient for this typed closure.
    """
    try:
        run = gh_json(
            [
                "api",
                f"repos/{repo}/actions/runs/{run_id}",
                "--jq",
                "{event,name,status,conclusion,head_sha}",
            ],
            timeout=20,
        )
    except Exception:
        return False, f"ci_stale_run_classified_run_unreadable run={run_id}"
    if str(run.get("event") or "") != "pull_request":
        return (
            False,
            f"ci_stale_run_classified_run_not_pull_request run={run_id} event={run.get('event')}",
        )
    # The single-run REST endpoint names the workflow `name` and the head
    # `head_sha` (snake_case); the gh CLI run-list JSON uses workflowName/headSha.
    if str(run.get("name") or "") not in REQUIRED_WORKFLOWS:
        return (
            False,
            f"ci_stale_run_classified_run_workflow_not_required run={run_id} workflow={run.get('name')}",
        )
    if str(run.get("status") or "").lower() != "completed":
        return (
            False,
            f"ci_stale_run_classified_run_not_completed run={run_id} status={run.get('status')}",
        )
    if str(run.get("conclusion") or "").lower() not in BAD_TERMINAL_CONCLUSIONS:
        return (
            False,
            f"ci_stale_run_classified_run_conclusion_not_bad run={run_id} conclusion={run.get('conclusion')}",
        )
    run_head = str(run.get("head_sha") or "")
    if run_head == head:
        return (
            False,
            f"ci_stale_run_classified_run_not_superseded run={run_id} head={head[:12]}",
        )
    marker_path = Path(f"/tmp/pm-claude-code-review-{pr}-{head}.md")
    try:
        marker_text = marker_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, f"ci_stale_run_classified_marker_missing path={marker_path}"
    if proof_pass_status(marker_text, "PM_CLAUDE_REVIEW") != "PASS":
        return False, "ci_stale_run_classified_marker_not_pass"
    if not re.search(rf"^headRefOid:\s*{re.escape(head)}\s*$", marker_text, re.M):
        return False, "ci_stale_run_classified_marker_head_mismatch"
    if not re.search(r"^review_model:\s*fable(?:\s|$)", marker_text, re.M):
        return False, "ci_stale_run_classified_marker_review_model_not_fable"
    if not re.search(r"^blocker_reviewed:\s*pm-blocked:ci(?:\s|$)", marker_text, re.M):
        return False, "ci_stale_run_classified_marker_blocker_not_ci"
    return True, "ok"


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    pr = gh_json(
        [
            "pr",
            "view",
            str(args.pr),
            "--repo",
            args.repo,
            "--json",
            "number,title,state,isDraft,headRefOid,headRefName,mergeStateStatus,labels,updatedAt,url,commits",
        ]
    )
    head = str(pr.get("headRefOid") or "")
    branch = str(pr.get("headRefName") or "")
    reasons: list[str] = []
    warnings: list[str] = []
    artifacts: dict[str, Any] = {}
    change_scope = exact_head_change_scope(int(args.pr), head, args.repo)
    artifacts["change_scope"] = change_scope
    control_plane_only = bool(
        change_scope.get("control_plane_only") is True
        and change_scope.get("ci_required") is False
        and change_scope.get("e2e_required") is False
    )
    if not control_plane_only:
        visual_proof = exact_head_qa_visual_proof(int(args.pr), head, args.repo)
        artifacts["qa_visual_proof"] = visual_proof
        if visual_proof.get("ok") is not True:
            reasons.append(
                "qa_visual_proof_gate_"
                + str(visual_proof.get("reason") or "blocked")
            )
    override_proof_valid = False
    override_class = ""
    explicit_proof_kind = ""
    if getattr(args, "phase_a_authorized", False) and args.source != "pm-review-done":
        reasons.append("phase_a_authorization_requires_pm_review_done_source")
    if args.affected_test_proof:
        explicit_proof_kind = classify_explicit_ci_start_proof(
            args.affected_test_proof, int(args.pr), head
        )
        if explicit_proof_kind == "override":
            if args.source not in CI_START_OVERRIDE_SOURCES:
                reasons.append(
                    f"ci_start_override_requires_admitted_source source={args.source}"
                )
            else:
                override_proof_valid, override_reason, override_class = (
                    ci_start_override_dispatch(
                        Path(args.affected_test_proof),
                        int(args.pr),
                        head,
                        change_scope=change_scope,
                        repo_root=Path(__file__).resolve().parents[3],
                    )
                )
                artifacts["ci_start_override"] = {
                    "class": override_class,
                    "ok": override_proof_valid,
                }
                if not override_proof_valid:
                    reasons.append(f"ci_start_override_{override_reason}")
        elif explicit_proof_kind == "rejected":
            # Compatibility flag only: a non-canonical --affected-test-proof
            # path is a deprecated diagnostic and can never gate CI-start
            # (Rajiv directive, thread 1786947023.747929).
            warnings.append(
                "ci_start_optional_explicit_proof_path_not_canonical "
                f"path={args.affected_test_proof}"
            )
        # explicit_proof_kind == "ordinary": the exact canonical affected-test
        # proof — validated by the ordinary affected_test_proof() contract below.

    if args.expect_head and head != args.expect_head:
        reasons.append(f"head_drift expected={args.expect_head} live={head}")
    if pr.get("state") != "OPEN":
        reasons.append(f"pr_not_open state={pr.get('state')}")
    if pr.get("isDraft"):
        warnings.append("draft_pr_transition_must_run_gh_pr_ready_before_label")
    merge_state = str(pr.get("mergeStateStatus") or "UNKNOWN")
    if merge_state in {"DIRTY", "CONFLICTING", "BLOCKED"}:
        # Release-policy simplification: merge-state is a merge gate, not a
        # CI-start gate. The canonical pre-merge guard still refuses a
        # DIRTY/CONFLICTING head at merge time.
        warnings.append(f"ci_start_optional_merge_state mergeStateStatus={merge_state}")

    labs = labels(pr)
    ci_stale_run_admitted = False
    stale_run_id = int(getattr(args, "ci_stale_run_classified", 0) or 0)
    if stale_run_id:
        # The typed ci-stale-run-classified closure is an admission FLAG on the
        # existing CI-start consumption, not a new transition source: only the
        # pm-review-done source may carry it, and every hold is re-validated
        # end-to-end here (the closure transition in pm-transition.sh performs
        # the same five-hold fail-fast checks before invoking the gate). Any
        # other source, or any failed hold, fails closed with a typed reason.
        if args.source != "pm-review-done":
            reasons.append(
                f"ci_stale_run_classified_requires_pm_review_done_source source={args.source}"
            )
        else:
            stale_ok, stale_reason = ci_stale_run_classified_admission(
                int(args.pr), head, stale_run_id, args.repo
            )
            artifacts["ci_stale_run_classified"] = {
                "ok": stale_ok,
                "reason": stale_reason,
                "run_id": stale_run_id,
            }
            if stale_ok:
                ci_stale_run_admitted = True
            else:
                reasons.append(stale_reason)
    blocking, transition_owned = ci_start_blocking_labels(
        labs,
        args.source,
        rescue_authorized=bool(getattr(args, "rescue_authorized", False)),
        phase_a_authorized=bool(getattr(args, "phase_a_authorized", False)),
        ci_stale_run_admitted=ci_stale_run_admitted,
        no_patch_rescue_authorized=bool(
            override_proof_valid
            and override_class == "cto_no_patch_rescue_ci_admission"
        )
        or bool(getattr(args, "no_patch_rescue_authorized", False)),
    )
    if transition_owned:
        warnings.append(
            "transition_will_clear_labels=" + ",".join(transition_owned)
        )
    if blocking:
        # Release-policy simplification: label ceremony (review blockers,
        # rescue holds, etc.) does not block CI start. The functionality-first
        # review runs in parallel with CI and gates merge, not CI start.
        warnings.append(f"ci_start_optional_active_blocking_labels={','.join(blocking)}")

    head_ts, head_ts_source = latest_head_timestamp(pr)
    if head_ts and args.min_head_age_minutes > 0 and not control_plane_only:
        head_age = max(0, int((datetime.now(timezone.utc) - head_ts).total_seconds() // 60))
        artifacts["head_age_min"] = head_age
        artifacts["head_age_source"] = head_ts_source
        moving_head_reason = moving_head_recent_push_reason(
            head_age,
            args.min_head_age_minutes,
            head_ts_source,
        )
        if moving_head_reason:
            warnings.append(f"ci_start_optional_{moving_head_reason}")

    # Release-policy simplification (Rajiv thread 1786811168.455449 ts
    # 1786811850.717079): affected-test planner/proof receipts are retired.
    # They may remain readable as optional diagnostics for one release but
    # neither block nor authorize CI. Only the typed CTO override lane is
    # still validated (above); ordinary affected-test paths are ignored.
    artifacts["affected_test_proof"] = (
        str(Path(args.affected_test_proof))
        if explicit_proof_kind == "override" and args.affected_test_proof
        else None
    )

    capture_proof_valid = False
    capture = (
        {
            "capture_required": False,
            "reason": "control_plane_only",
            "headRefOid": head,
        }
        if control_plane_only
        else capture_requirement(int(args.pr), args.repo, head)
    )
    artifacts["capture_requirement"] = capture
    if capture.get("capture_required") is None:
        reasons.append(f"capture_requirement_unknown reason={capture.get('reason')}")
    elif capture.get("capture_required"):
        if override_proof_valid and override_class == "cto_marker_pass_ci_admission":
            # The marker-PASS adjudicated class carries a sealed GREEN
            # parent-head capture-run receipt validated end-to-end. It is the
            # capture proof for this one-time admission; no additional local or
            # remote capture is required before the mandatory current-head
            # CI+E2E wave.
            capture_proof_valid = True
            drop_capture_blocker_reason(reasons)
            artifacts["capture"] = {
                "state": "cto_marker_pass_capture_receipt",
                "proof": args.affected_test_proof,
                "final_ci_e2e_required": True,
            }
        # Remote capture is the default and authoritative capture lane. Local
        # capture is diagnostic-only (capture-local-required with a named
        # infrastructure defect) and never satisfies capture readiness nor
        # serves as a fallback after a remote red.
        else:
            ok, detail = capture_green(head, args.repo, int(args.pr), branch)
            artifacts["capture"] = detail
            if ok:
                capture_proof_valid = True
                drop_capture_blocker_reason(reasons)
            else:
                reasons.append(
                    "capture_required_not_green "
                    f"detail={detail.get('state')} run={detail.get('run_id') or 'none'}"
                )

    workflows = workflow_state(pr, args.repo)
    artifacts["workflows"] = workflows
    ci_verdict = latest_current_head_ci_verdict(int(args.pr), head, args.repo)
    if ci_verdict:
        artifacts["latest_ci_verdict"] = {
            "classification": ci_verdict.get("classification"),
            "run_id": ci_verdict.get("run_id"),
            "recapture_fixes_it": ci_verdict.get("recapture_fixes_it"),
            "rerun_permitted": ci_verdict.get("rerun_permitted"),
            "comment_url": ci_verdict.get("comment_url"),
        }
    if not control_plane_only and workflows["state"] == "in_progress":
        reasons.append("current_head_ci_or_e2e_already_in_progress")
    elif not control_plane_only and workflows["state"] == "green":
        reasons.append("current_head_ci_or_e2e_already_terminal_green")
    elif not control_plane_only and workflows["state"] == "failed":
        # Release-policy simplification: a terminal same-head functional red
        # is NEVER re-triggered through the label. The owning slot reproduces
        # the failed surface locally, implements a correction, and pushes a
        # descendant head before the next wave. Infra/runner/flake same-head
        # reruns are capped at one and run off-slot through the rerun wrapper
        # (gh run rerun), not a label toggle. Sealed local-preflight proof
        # envelopes and their one-shot override packets no longer admit at
        # this boundary (Rajiv 1786812200.371389).
        if ci_verdict_requires_rework(ci_verdict):
            reasons.append(
                "current_head_ci_verdict_requires_rework "
                f"classification={ci_verdict.get('classification') or 'unknown'} "
                f"run={ci_verdict.get('run_id') or 'unknown'}"
            )
        else:
            # Same-head red is never re-triggered through the label: product
            # and uncertain classes are owned by the slot (block +
            # assign-rework with the exact failed-run packet; a descendant
            # head then starts one fresh wave), while infra/flake/shared
            # classes get at most one same-head rerun off-slot through the
            # rerun wrapper. No sealed proof is required at this boundary.
            reasons.append(
                "current_head_ci_or_e2e_failed_use_rerun_not_label_trigger "
                f"run={ci_verdict.get('run_id') if ci_verdict else (workflows.get('bad_run_id') or 'unknown')} "
                "sealed_preflight_retired=true"
            )
    elif not control_plane_only and workflows["state"] == "cancelled":
        # Release-policy simplification: a same-head cancelled wave is not
        # re-triggered through the label. The rerun wrapper (off-slot,
        # infra/runner/flake one-rerun cap) is the only same-head path;
        # sealed local-preflight override packets no longer admit (Rajiv
        # 1786812200.371389).
        reasons.append("current_head_ci_or_e2e_cancelled_requires_investigation")
    elif (
        not control_plane_only
        and workflows["state"] == "partial"
        and workflows.get("current_head_attempt_count", 0) > 0
    ):
        # #6733: if partial state is only from success + superseded-skipped
        # (label-churn), permit CI-start instead of blocking.
        _all_success_or_superseded = all(
            item["state"] in {"success", "missing", "skipped"}
            for item in (workflows.get("runs") or [])
        )
        if not _all_success_or_superseded:
            reasons.append("current_head_ci_or_e2e_partial_existing_runs")
    elif (
        not control_plane_only
        and workflows["state"] == "not_started"
        and workflows.get("current_head_attempt_count", 0) == 0
        and int(workflows.get("stale_bad_run_count") or 0) > 0
    ):
        # Moving-head admission (Rajiv 1786812200.371389): the live head has
        # NO current-head attempts (attempt count 0) while failed runs exist
        # only at SUPERSEDED heads — the head moved. A fresh exact-head wave
        # may start on the current head (exact-head binding + single-flight),
        # but a superseded run is never retried through the label.
        if ci_verdict and ci_verdict_requires_rework(ci_verdict):
            # An exact-head requires-rework verdict retains the rework
            # blocker; the verdict comment itself is the rework packet.
            reasons.append(
                "current_head_ci_verdict_requires_rework "
                f"classification={ci_verdict.get('classification') or 'unknown'} "
                f"run={ci_verdict.get('run_id') or 'unknown'}"
            )
        elif ci_verdict:
            warnings.append(
                "current_head_moved_classified_by_exact_head_ci_verdict "
                f"stale_bad={workflows.get('stale_bad_run_count')} "
                f"run={ci_verdict.get('run_id') or 'unknown'}"
            )
        else:
            # Release-policy simplification: a descendant (moved) head with
            # superseded bad runs starts one fresh exact-head wave. No
            # local-preflight seal or override discharge ceremony is required.
            warnings.append(
                "current_head_moved_classified_by_superseded_runs "
                f"stale_bad={workflows.get('stale_bad_run_count')} "
                f"stale_run_ids={','.join(str(r) for r in (workflows.get('stale_bad_run_ids') or []))} "
                "no_verdict_at_current_head; fresh exact-head CI+E2E wave may start"
            )
    elif (
        not control_plane_only
        and workflows["state"] == "not_started"
        and workflows.get("current_head_attempt_count", 0) > 0
    ):
        reasons.append("current_head_ci_or_e2e_skipped_existing_runs")

    updated = parse_time(pr.get("updatedAt"))
    if updated:
        age = int((datetime.now(timezone.utc) - updated).total_seconds() // 60)
        artifacts["pr_updated_age_min"] = age
        if age < args.warn_recent_update_minutes:
            warnings.append(f"recent_pr_update_age_min={age}")

    # Release-policy simplification: keep only the CI-start safety reasons.
    # Every other admission reason is optional ceremony and becomes a
    # diagnostic warning (affected-test planner receipts, capture receipts,
    # review markers, stale-run verdict packets, local-preflight seals,
    # override packets, QA visual proof, phase-a, explicit proof
    # path, stale-run classification).
    ceremony = [
        reason
        for reason in reasons
        if not reason.startswith(CI_START_SAFETY_REASON_PREFIXES)
    ]
    if ceremony:
        warnings.append(
            "ci_start_ceremony_optional_diagnostics=" + ",".join(ceremony)
        )
        reasons = [
            reason
            for reason in reasons
            if reason.startswith(CI_START_SAFETY_REASON_PREFIXES)
        ]

    ok = not reasons
    if ok:
        # Consume the sealed mutation-scope override only on full admission by
        # the final label-control gate call; a consumption failure fails the
        # gate closed so the packet cannot be re-admitted.
        sealed_commit_error = sealed_mutation_scope_override_commit(
            args, explicit_proof_kind, head
        )
        if sealed_commit_error:
            reasons.append(sealed_commit_error)
            ok = False
    actions = [] if ok else required_actions(reasons)
    return {
        "schema": "heydonna_pr_ci_readiness_gate",
        "version": 2,
        "status": "pass" if ok else "blocked",
        "ok": ok,
        "disposition": (
            "control_plane_ci_exempt" if control_plane_only else "paid_ci_required"
        ),
        "ci_required": not control_plane_only,
        "e2e_required": not control_plane_only,
        "source": args.source,
        "pr": int(args.pr),
        "headRefOid": head,
        "branch": pr.get("headRefName"),
        "title": pr.get("title"),
        "url": pr.get("url"),
        "reasons": reasons,
        "required_actions": actions,
        "required_action": primary_required_action(actions),
        "warnings": warnings,
        "artifacts": artifacts,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


# Synthetic tuple for the bounded self-test matrix: far above real PR numbers
# and an obviously-synthetic 40-hex head, so fixture files under /tmp never
# collide with live incident artifacts.
SELF_TEST_PR = 98765
SELF_TEST_HEAD = "f" * 40


def _self_test_artifacts_dir() -> Path:
    return Path("/tmp")


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _self_test_write_capture(pr: int, head: str, status: str = "PASS") -> None:
    _write_text(
        _self_test_artifacts_dir() / f"capture-local-proof-{pr}-{head}.ok",
        (
            f"CAPTURE_LOCAL: {status}\n"
            f"headRefOid: {head}\n"
            "capture_format_only: false\n"
            "sc_writeback_assertions_required: true\n"
            "fixture_capture_policy: update_all_llm_proxy_cache_misses_on_selected_auto_process_path\n"
            "label_gated_ci_allowed_after_local_capture: true\n"
            "current_head_verified_before_capture: true\n"
            "current_head_status: exact_pr_head\n"
            "strict_replay_after_capture_required: true\n"
            "full_ci_e2e_same_head_required_after_capture: true\n"
        ),
    )


def _self_test_write_marker(pr: int, head: str, status: str = "PASS") -> None:
    _write_text(
        _self_test_artifacts_dir() / f"pm-claude-code-review-{pr}-{head}.md",
        f"PM_CLAUDE_REVIEW: {status}\nheadRefOid: {head}\n",
    )


def _self_test_write_codex_marker(
    pr: int, head: str, status: str = "REQUEST_CHANGES"
) -> None:
    _write_text(
        _self_test_artifacts_dir() / f"codex-app-code-review-{pr}.txt",
        f"VERDICT: {status}\n"
        f"COMPANION_VERDICT: {status}\n"
        f"FINAL_REVIEWER_VERDICT: {status}\n"
        f"headRefOid: {head}\n"
        "pass_scope: blocked\n",
    )


def _self_test_write_log(pr: int, head: str, vacuous: bool = True) -> None:
    text = (
        "## command 1: pytest tests/test_experiment_5389_gates.py --rootdir=.\n"
        "============================== 3 passed in 0.01s ==============================\n"
    )
    if vacuous:
        text += (
            "## mutation exit 2\n"
            "VACUOUS_RED: pytest exit 2 is not a mutation RED (exit 1 only; "
            "exit 0 is vacuous, exit 2/3/5 are collection/load failures, exit 4 "
            "is a usage error)\n"
        )
    _write_text(
        _self_test_artifacts_dir() / f"affected-test-proof-{pr}-{head}.log",
        text,
    )


def _self_test_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _self_test_write_sealed_packet(
    pr: int,
    head: str,
    *,
    authorization: str = "Abhijit CTO",
    scope: str = "one-time ci+e2e fire only; current-head CI/E2E mandatory; no merge authority",
    followup: str = "followup_issue: #7205",
    consumed: str = "no",
    digest_overrides: dict[str, str] | None = None,
    omit_line: str = "",
) -> None:
    marker = _self_test_artifacts_dir() / f"pm-claude-code-review-{pr}-{head}.md"
    log = _self_test_artifacts_dir() / f"affected-test-proof-{pr}-{head}.log"
    capture = _self_test_artifacts_dir() / f"capture-local-proof-{pr}-{head}.ok"
    marker_digest = digest_overrides.get("marker", "") if digest_overrides else ""
    log_digest = digest_overrides.get("log", "") if digest_overrides else ""
    capture_digest = digest_overrides.get("capture", "") if digest_overrides else ""
    lines = [
        "AFFECTED_TESTS: PASS_WITH_PREEXISTING_FAILURES",
        f"provenance: {SEALED_MUTATION_SCOPE_OVERRIDE_PROVENANCE}",
        "no_full_suite: true",
        f"PR: {pr}",
        f"headRefOid: {head}",
        f"sealed_artifact: {marker} sha256:{marker_digest or _self_test_digest(marker)}",
        f"sealed_artifact: {log} sha256:{log_digest or _self_test_digest(log)}",
        f"sealed_artifact: {capture} sha256:{capture_digest or _self_test_digest(capture)}",
        followup,
        f"authorization: {authorization}",
        f"scope: {scope}",
        f"consumed: {consumed}",
    ]
    if omit_line:
        lines = [line for line in lines if not line.startswith(omit_line)]
    _write_text(
        _self_test_artifacts_dir() / f"pm-ci-start-override-{pr}-{head}.ok",
        "\n".join(lines) + "\n",
    )


def _self_test_write_cto_adjudicated_packet(
    pr: int,
    head: str,
    *,
    authorization_ts: str = "1786474093",
    disposition_ts: str = "1786466534.691149",
    followup: str = "followup_issue: #7205",
    consumed: str = "no",
    status: str = "REQUEST_CHANGES",
    vacuous: bool = True,
    scope: str = "one-time ci+e2e current-head mandatory applicable-lfc no merge authority",
    digest_overrides: dict[str, str] | None = None,
    omit_line: str = "",
) -> None:
    marker = _self_test_artifacts_dir() / f"codex-app-code-review-{pr}.txt"
    log = _self_test_artifacts_dir() / f"affected-test-proof-{pr}-{head}.log"
    _self_test_write_codex_marker(pr, head, status)
    _self_test_write_log(pr, head, vacuous=vacuous)
    marker_digest = digest_overrides.get("marker", "") if digest_overrides else ""
    log_digest = digest_overrides.get("log", "") if digest_overrides else ""
    paths = [
        "components/editor/extensions/CSSPagination/CSSPagination.ts",
        "components/editor/extensions/CSSPagination/chrome-window.ts",
        "components/editor/extensions/CSSPagination/index.ts",
        "components/editor/extensions/CSSPagination/styles.ts",
        "components/editor/extensions/CSSPagination/types.ts",
    ]
    lines = [
        "AFFECTED_TESTS: PASS_WITH_VACUOUS_RED",
        f"provenance: {CTO_ADJUDICATED_CI_ADMISSION_PROVENANCE}",
        "no_full_suite: true",
        f"PR: {pr}",
        f"headRefOid: {head}",
        f"codex_review_marker: {marker} sha256:{marker_digest or _self_test_digest(marker)}",
        f"affected_test_log: {log} sha256:{log_digest or _self_test_digest(log)}",
        "change_scope_rules_sha256: " + "5" * 64,
        "change_scope_classifier_sha256: " + "6" * 64,
        followup,
        f"authorization_ts: {authorization_ts}",
        f"disposition_ts: {disposition_ts}",
        f"authorization: Rajiv CTO decision {authorization_ts}; "
        f"Abhijit CTO disposition {disposition_ts}",
        f"scope: {scope}",
        "capture_not_required: true",
        "capture_basis: non-llm-geometry-diff",
        f"capture_path_evidence: {','.join(paths)}",
        f"consumed: {consumed}",
    ]
    if omit_line:
        lines = [line for line in lines if not line.startswith(omit_line)]
    _write_text(
        _self_test_artifacts_dir() / f"pm-ci-start-override-{pr}-{head}.ok",
        "\n".join(lines) + "\n",
    )


def _self_test_change_scope(head: str) -> dict[str, Any]:
    return {
        "head": head,
        "control_plane_only": False,
        "product_changed": True,
        "changed_files": [
            "components/editor/extensions/CSSPagination/CSSPagination.ts",
            "components/editor/extensions/CSSPagination/chrome-window.ts",
            "components/editor/extensions/CSSPagination/index.ts",
            "components/editor/extensions/CSSPagination/styles.ts",
            "components/editor/extensions/CSSPagination/types.ts",
        ],
        "rules_sha256": "5" * 64,
        "classifier_sha256": "6" * 64,
    }


def _self_test_capture_requirement(pr: int, repo: str, head: str) -> dict[str, Any]:
    return {
        "capture_required": False,
        "reason": "no_capture_sensitive_diff",
        "headRefOid": head,
        "changed_paths": _self_test_change_scope(head)["changed_files"],
    }


def _self_test_write_rescue_artifacts(pr: int, head: str) -> dict[str, Path]:
    request = "a45ea1c667c84b17c112a45ee253c9a7"
    source = "1" * 40
    patch_sha = "0" * 64
    patch_id = "b" * 40
    landed = "c" * 40
    changed = [
        "__tests__/e2e/helpers/certificate-selection.test.ts",
        "docs/plans/issue-7225-certificate-selection-hang.md",
        "tests/e2e/helpers/certificate-selection.ts",
        "tests/e2e/specs/core/smoke-legal-deposition.spec.ts",
    ]
    capture_changed = _self_test_change_scope(head)["changed_files"]
    packet = _self_test_artifacts_dir() / f"pm-kimi3-rescue-packet-{request}.md"
    _write_text(
        packet,
        "PM_CLAUDE_PR_RESCUE: PATCH_READY\n"
        "terminal: PATCH_READY\n"
        "slot_actionable: true\n"
        "skip_further_review: true\n"
        f"PR: #{pr}\n"
        f"head_or_plan_sha: {source}\n"
        f"mop_request_id: {request}\n"
        f"patch_sha256: {patch_sha}\n"
        f"changed_paths: {','.join(changed)}\n"
        "runtime_control_point: tests/e2e/helpers/certificate-selection.ts::commitCertificateSelection (smoke)\n",
    )
    diff = _self_test_artifacts_dir() / f"pm-kimi3-rescue-diff-receipt-{request}-{head}.json"
    _write_text(
        diff,
        json.dumps(
            {
                "schema_version": 1,
                "producer": "cto-direct-control-plane-repair",
                "request": request,
                "source_head": source,
                "head": head,
                "patch_sha256": patch_sha,
                "stable_patch_id": patch_id,
                "changed_paths": changed,
            },
            sort_keys=True,
        )
        + "\n",
    )
    capture = _self_test_artifacts_dir() / f"pm-capture-required-{pr}-{head}.json"
    _write_text(
        capture,
        json.dumps(
            {
                "capture_required": False,
                "pr": pr,
                "headRefOid": head,
                "reason": "no_capture_sensitive_diff",
                "changed_paths": capture_changed,
            },
            sort_keys=True,
        )
        + "\n",
    )
    landed_path = _self_test_artifacts_dir() / f"cto-rescue-packet-ci-admission-receipt-{landed}.md"
    _write_text(
        landed_path,
        "control_plane_incident: control-plane:cto-rescue-packet-ci-admission\n"
        f"landed_head: {landed}\n",
    )
    plan = _self_test_artifacts_dir() / f"affected-test-plan-{pr}-{head}.json"
    _write_text(
        plan,
        json.dumps(
            {"pr": pr, "headRefOid": head, "commands": [{"command": "vitest helpers", "kind": "vitest"}]},
            sort_keys=True,
        )
        + "\n",
    )
    log = _self_test_artifacts_dir() / f"affected-test-proof-{pr}-{head}.log"
    _write_text(log, "      Tests  12 passed (12)\n\nVACUOUS_RED: collection/load failure is not a mutation RED\n")
    preflight_log = _self_test_artifacts_dir() / f"cto-rescue-preflight-{pr}-{head}.log"
    _write_text(preflight_log, "Running smoke-legal-deposition.spec.ts\n  3 passed (3)\n")
    commands_sha = "d" * 64
    preflight = Path("/tmp/affected-test-preflight-receipts") / f"{head}-{commands_sha}.json"
    preflight.parent.mkdir(parents=True, exist_ok=True)
    _write_text(
        preflight,
        json.dumps(
            {
                "schema_version": 1,
                "producer": "local-repro-preflight.sh",
                "headRefOid": head,
                "commands_sha256": commands_sha,
                "result": "PASS",
                "exit_code": 0,
                "command_contract": {
                    "mode": "spec",
                    "spec": "tests/e2e/specs/core/smoke-legal-deposition.spec.ts",
                },
                "log_path": str(preflight_log),
                "log_sha256": _self_test_digest(preflight_log),
            },
            sort_keys=True,
        )
        + "\n",
    )
    return {
        "packet": packet,
        "diff": diff,
        "capture": capture,
        "landed": landed_path,
        "plan": plan,
        "log": log,
        "preflight": preflight,
    }


def _self_test_write_rescue_admission_packet(
    pr: int,
    head: str,
    *,
    consumed: str = "no",
    omit_line: str = "",
) -> None:
    artifacts = _self_test_write_rescue_artifacts(pr, head)
    changed = [
        "__tests__/e2e/helpers/certificate-selection.test.ts",
        "docs/plans/issue-7225-certificate-selection-hang.md",
        "tests/e2e/helpers/certificate-selection.ts",
        "tests/e2e/specs/core/smoke-legal-deposition.spec.ts",
    ]
    request = "a45ea1c667c84b17c112a45ee253c9a7"
    source = "1" * 40
    lines = [
        "AFFECTED_TESTS: PASS_WITH_VACUOUS_RED",
        f"provenance: {CTO_RESCUE_PACKET_CI_ADMISSION_PROVENANCE}",
        "no_full_suite: true",
        f"PR: {pr}",
        f"headRefOid: {head}",
        "control_plane_incident: control-plane:cto-rescue-packet-ci-admission",
        f"rescue_request: {request}",
        f"rescue_source_head: {source}",
        "rescue_patch_sha256: " + "0" * 64,
        "rescue_patch_id: " + "b" * 40,
        "rescue_runtime_control_point: tests/e2e/helpers/certificate-selection.ts::commitCertificateSelection",
        f"rescue_changed_paths: {','.join(changed)}",
        f"rescue_packet: {artifacts['packet']} sha256:{_self_test_digest(artifacts['packet'])}",
        f"affected_plan: {artifacts['plan']} sha256:{_self_test_digest(artifacts['plan'])}",
        f"affected_log: {artifacts['log']} sha256:{_self_test_digest(artifacts['log'])}",
        f"full_spec_preflight_receipt: {artifacts['preflight']} sha256:{_self_test_digest(artifacts['preflight'])}",
        f"rescue_diff_receipt: {artifacts['diff']} sha256:{_self_test_digest(artifacts['diff'])}",
        f"capture_required_receipt: {artifacts['capture']} sha256:{_self_test_digest(artifacts['capture'])}",
        f"landed_repair_receipt: {artifacts['landed']} sha256:{_self_test_digest(artifacts['landed'])}",
        "change_scope_rules_sha256: " + "5" * 64,
        "change_scope_classifier_sha256: " + "6" * 64,
        f"authorization_ts: {CTO_DEGRADED_DELIVERY_DECISION_TS}",
        f"decision_alias_ts: {CTO_DEGRADED_DELIVERY_ALIAS_TS}",
        f"authorization: Rajiv CTO degraded-delivery decision {CTO_DEGRADED_DELIVERY_DECISION_TS}; Abhijit CTO directive {CTO_DEGRADED_DELIVERY_ALIAS_TS}",
        "scope: one-time ci+e2e current-head mandatory no merge authority",
        "lfc_not_required: true",
        f"consumed: {consumed}",
    ]
    if omit_line:
        lines = [line for line in lines if not line.startswith(omit_line)]
    _write_text(
        _self_test_artifacts_dir() / f"pm-ci-start-override-{pr}-{head}.ok",
        "\n".join(lines) + "\n",
    )


def _self_test_write_marker_pass_artifacts(
    pr: int,
    head: str,
    *,
    marker_status: str = "PASS",
    vacuous: bool = True,
) -> dict[str, Path]:
    run_id = "987654"
    run_head = "a" * 40
    fixtures = 42
    pin = "d" * 64
    marker = _self_test_artifacts_dir() / f"pm-claude-code-review-{pr}-{head}.md"
    _write_text(
        marker,
        f"PM_CLAUDE_REVIEW: {marker_status}\n"
        f"headRefOid: {head}\n"
        "pass_scope: phase-a\n"
        f"proofread_request_hash: {pin}\n",
    )
    plan = _self_test_artifacts_dir() / f"affected-test-plan-{pr}-{head}.json"
    _write_text(
        plan,
        json.dumps(
            {
                "pr": pr,
                "headRefOid": head,
                "scope": "targeted",
                "requires_pm_test_scope": False,
                "verification_results": None,
            },
            sort_keys=True,
        )
        + "\n",
    )
    log = _self_test_artifacts_dir() / f"affected-test-proof-{pr}-{head}.log"
    log_text = "============================== 45 passed ==============================\n"
    if vacuous:
        log_text += (
            "## mutation exit 1\n"
            "## mutation exit 5\n"
            "VACUOUS_RED: pytest exit 5 is not a mutation RED (exit 1 only)\n"
        )
    _write_text(log, log_text)
    receipt = _self_test_artifacts_dir() / f"pm-review-done-receipt-{pr}-{head}.json"
    _write_text(
        receipt,
        json.dumps(
            {
                "schema_version": 1,
                "pr": pr,
                "head_sha": head,
                "verdict": "PASS",
                "handoff_status": "blocked_after_review",
                "blocked_after_review": {"class": "affected_test_refusal"},
                "marker_sha256": _self_test_digest(marker),
                "scope": "phase-a",
            },
            sort_keys=True,
        )
        + "\n",
    )
    capture = _self_test_artifacts_dir() / f"pm-capture-run-verified-{pr}-{head}.json"
    _write_text(
        capture,
        json.dumps(
            {
                "schema_version": 1,
                "producer": "cto-direct-control-plane-repair",
                "pr": pr,
                "headRefOid": head,
                "run_id": int(run_id),
                "run_head": run_head,
                "fixtures_promoted": fixtures,
            },
            sort_keys=True,
        )
        + "\n",
    )
    return {
        "marker": marker,
        "plan": plan,
        "log": log,
        "receipt": receipt,
        "capture": capture,
    }


def _self_test_write_marker_pass_packet(
    pr: int,
    head: str,
    *,
    consumed: str = "no",
    omit_line: str = "",
    marker_status: str = "PASS",
    vacuous: bool = True,
) -> None:
    artifacts = _self_test_write_marker_pass_artifacts(
        pr, head, marker_status=marker_status, vacuous=vacuous
    )
    run_id = "987654"
    run_head = "a" * 40
    lines = [
        "AFFECTED_TESTS: PASS_WITH_VACUOUS_RED",
        f"provenance: {CTO_MARKER_PASS_CI_ADMISSION_PROVENANCE}",
        "no_full_suite: true",
        f"PR: {pr}",
        f"headRefOid: {head}",
        "control_plane_incident: control-plane:cto-marker-pass-vacuous-red-ci-admission",
        f"review_marker: {artifacts['marker']} sha256:{_self_test_digest(artifacts['marker'])}",
        f"affected_plan: {artifacts['plan']} sha256:{_self_test_digest(artifacts['plan'])}",
        f"affected_test_log: {artifacts['log']} sha256:{_self_test_digest(artifacts['log'])}",
        f"review_receipt: {artifacts['receipt']} sha256:{_self_test_digest(artifacts['receipt'])}",
        f"capture_run_receipt: {artifacts['capture']} sha256:{_self_test_digest(artifacts['capture'])}",
        f"capture_run_id: {run_id}",
        f"capture_run_head: {run_head}",
        "capture_fixtures_promoted: 42",
        "proofread_pin_sha256: " + "d" * 64,
        "vacuous_reason: branch_added_fixture_collection_exit_5_not_mutation_red",
        "change_scope_rules_sha256: " + "5" * 64,
        "change_scope_classifier_sha256: " + "6" * 64,
        f"authorization_ts: {CTO_MARKER_PASS_SOURCE_TS}",
        f"source_wake: {CTO_MARKER_PASS_SOURCE_WAKE}",
        f"authorization: Rajiv CTO directive {CTO_MARKER_PASS_SOURCE_TS}; Abhijit CTO source-wake {CTO_MARKER_PASS_SOURCE_TS}",
        "scope: one-time ci+e2e current-head mandatory no merge authority",
        f"consumed: {consumed}",
    ]
    if omit_line:
        lines = [line for line in lines if not line.startswith(omit_line)]
    _write_text(
        _self_test_artifacts_dir() / f"pm-ci-start-override-{pr}-{head}.ok",
        "\n".join(lines) + "\n",
    )


def _self_test_write_no_patch_rescue_packet(
    pr: int,
    head: str,
    *,
    consumed: str = "no",
    omit_line: str = "",
    packet_terminal: str = "NO_PATCH_REQUIRED",
    packet_changed_paths: str = "none",
) -> None:
    """Write a deterministic NO_PATCH_REQUIRED rescue packet + RED/GREEN
    receipts + the canonical CTO-adjudication override into the self-test
    artifact root (/tmp)."""

    request = "a" * 40
    red = _self_test_artifacts_dir() / f"pm-kimi3-rescue-red-{request}.json"
    green = _self_test_artifacts_dir() / f"pm-kimi3-rescue-green-{request}.json"
    packet = _self_test_artifacts_dir() / f"pm-kimi3-rescue-packet-{request}.md"
    _write_text(
        red,
        "PM_FABLE_RESCUE_TEST_RECEIPT\n"
        f"request_id: {request}\n"
        "rescue_trigger: code-review-cap\n"
        "kind: red\n"
        "command: perl -pi -e 's/options\\.now \\?\\? Date\\.now\\(\\);/options\\.now;/' lib/file.ts && vitest run lib/__tests__/file.test.ts\n"
        "exit_code: 1\n"
        f"result_signal: mutated head {head}; 6 failed | 6 passed (12); PRESIGNED_EXPIRED expected-success\n",
    )
    _write_text(
        green,
        "PM_FABLE_RESCUE_TEST_RECEIPT\n"
        f"request_id: {request}\n"
        "rescue_trigger: code-review-cap\n"
        "kind: green\n"
        "command: for i in 1 2 3 4 5; do vitest run lib/__tests__/file.test.ts || exit 1; done\n"
        "exit_code: 0\n"
        f"result_signal: five consecutive exit-0 executions on head {head}, 60/60 passed, zero flakes\n",
    )
    _write_text(
        packet,
        "PM_CLAUDE_PR_RESCUE_PACKET\n"
        f"PM_CLAUDE_PR_RESCUE: {packet_terminal}\n"
        "actor: pm\n"
        f"terminal: {packet_terminal}\n"
        "slot_actionable: true\n"
        "skip_further_review: true\n"
        f"issue: #{pr - 1}\n"
        f"pr: #{pr}\n"
        "slot: 4\n"
        "branch: fix/test-no-patch-rescue\n"
        f"head_or_plan_sha: {head}\n"
        "review_model: kimi3\n"
        "evidence_contract: production_path_v3\n"
        f"mop_request_id: {request}\n"
        f"mop_packet_path: {packet}\n"
        "mop_patch_path: /tmp/nonexistent.patch\n"
        f"mop_red_receipt_path: {red}\n"
        f"mop_green_receipt_path: {green}\n"
        "repeat_family: false\n"
        "candidate_ordinal: 1\n"
        f"red_proof_receipt: {red}\n"
        f"red_proof_receipt_sha256: {_self_test_digest(red)}\n"
        f"green_proof_receipt: {green}\n"
        f"green_proof_receipt_sha256: {_self_test_digest(green)}\n"
        f"changed_paths: {packet_changed_paths}\n"
        "patch_file: none\n"
        "patch_sha256: none\n"
        "proof_kind: affected_unit_integration\n"
        "required_pm_action: continue_verified_head\n",
    )
    expires = datetime.fromtimestamp(
        time.time() + 86400, timezone.utc
    ).isoformat(timespec="microseconds").replace("+00:00", "Z")
    lines = [
        "AFFECTED_TESTS: PASS",
        f"provenance: {CTO_NO_PATCH_RESCUE_CI_ADMISSION_PROVENANCE}",
        "no_full_suite: true",
        f"PR: {pr}",
        f"headRefOid: {head}",
        f"control_plane_incident: {CTO_NO_PATCH_RESCUE_INCIDENT}",
        f"rescue_request: {request}",
        f"rescue_packet: {packet} sha256:{_self_test_digest(packet)}",
        f"red_receipt: {red} sha256:{_self_test_digest(red)}",
        f"green_receipt: {green} sha256:{_self_test_digest(green)}",
        "change_scope_rules_sha256: " + "5" * 64,
        "change_scope_classifier_sha256: " + "6" * 64,
        *[f"authorization_ts: {ts}" for ts in CTO_NO_PATCH_RESCUE_DECISION_TS],
        f"authorization: Rajiv CTO dispositions {','.join(CTO_NO_PATCH_RESCUE_DECISION_TS)} (Slack thread {CTO_NO_PATCH_RESCUE_SOURCE_THREAD})",
        "scope: one-time ci+e2e current-head mandatory no merge authority",
        "lfc_not_required: true",
        f"expires_at: {expires}",
        f"consumed: {consumed}",
    ]
    if omit_line:
        lines = [line for line in lines if not line.startswith(omit_line)]
    _write_text(
        _self_test_artifacts_dir() / f"pm-ci-start-override-{pr}-{head}.ok",
        "\n".join(lines) + "\n",
    )


class _SelfTestGh:
    """Stub for module-level gh_json: live GitHub is never touched by --self-test."""

    def __init__(self, open_issues: set[int]) -> None:
        self.open_issues = open_issues

    def __call__(self, args: list[str], timeout: int = 20) -> dict[str, Any]:
        json_fields = ""
        for index, arg in enumerate(args):
            if arg == "--json" and index + 1 < len(args):
                json_fields = args[index + 1]
                break
        issue = None
        for index, arg in enumerate(args):
            if arg == "view" and index + 1 < len(args):
                try:
                    issue = int(args[index + 1])
                except ValueError:
                    issue = None
        if json_fields == "state":
            return {"state": "OPEN" if issue in self.open_issues else "CLOSED"}
        if json_fields == "number,state,body,url":
            if issue not in self.open_issues:
                return {"state": "CLOSED"}
            return {
                "number": issue,
                "state": "OPEN",
                "body": f"source_pr: #{SELF_TEST_PR}\n"
                f"failure_signature: {failure_signature('smoke.spec.ts › test one')}\n",
                "url": f"https://github.com/{REPO}/issues/{issue}",
            }
        if json_fields == "headSha,status,conclusion,event,displayTitle,workflowName":
            run_id = ""
            if len(args) >= 3 and args[0] == "run" and args[1] == "view":
                run_id = str(args[2])
            if run_id != "987654":
                raise AssertionError(f"unexpected run in self-test: {args}")
            return {
                "headSha": "a" * 40,
                "status": "completed",
                "conclusion": "success",
                "event": "workflow_dispatch",
                "displayTitle": f"remote-capture-pr-{SELF_TEST_PR}-head-" + "a" * 40,
                "workflowName": "E2E LLM Proxy Capture (manual)",
            }
        raise AssertionError(f"unexpected gh_json shape in self-test: {args}")


def _self_test_run_override_matrix() -> list[str]:
    """RED/GREEN/fail-closed matrix at the validator level (no live GitHub)."""
    failures: list[str] = []
    pr, head = SELF_TEST_PR, SELF_TEST_HEAD
    override_path = _self_test_artifacts_dir() / f"pm-ci-start-override-{pr}-{head}.ok"

    def check(name: str, cond: bool, detail: str = "") -> None:
        if cond:
            print(f"SELF_TEST PASS {name}")
        else:
            print(f"SELF_TEST FAIL {name} {detail}")
            failures.append(name)

    real_gh = gh_json
    real_capture_requirement = capture_requirement
    real_rescue_packet_root = RESCUE_PACKET_ARTIFACT_ROOT
    globals()["gh_json"] = _SelfTestGh(open_issues={7205})
    globals()["capture_requirement"] = _self_test_capture_requirement
    globals()["RESCUE_PACKET_ARTIFACT_ROOT"] = Path("/tmp")
    try:
        # RED: the unchanged pre-existing-failure class still rejects a packet
        # with no genuine failed-Playwright preflight receipt, reproducing the
        # live #7198 incident reject.
        _write_text(
            override_path,
            "AFFECTED_TESTS: PASS_WITH_PREEXISTING_FAILURES\n"
            "provenance: pm-recorded-one-time-gate-exception\n"
            "no_full_suite: true\n"
            f"PR: {pr}\n"
            f"headRefOid: {head}\n"
            "followup_issue: #7205\n",
        )
        ok, reason, kind = ci_start_override_dispatch(override_path, pr, head, REPO)
        check(
            "red_preexisting_receipt_missing",
            (ok, reason, kind) == (False, "override_receipt_missing", "preexisting_failure"),
            f"got ok={ok} reason={reason} kind={kind}",
        )

        # GREEN: a fully valid pre-existing-failure override (genuine failed
        # preflight receipt + followup contract) still passes — regression.
        receipt_dir = Path("/tmp/affected-test-preflight-receipts")
        receipt_dir.mkdir(parents=True, exist_ok=True)
        failure_log = (
            "1 failed\n"
            "  1) [chromium] › smoke.spec.ts › test one\n"
        )
        log_path = _self_test_artifacts_dir() / f"affected-test-preflight-log-{pr}-{head}.log"
        _write_text(log_path, failure_log)
        commands_sha256 = "d" * 64
        receipt = {
            "schema_version": 1,
            "producer": "local-repro-preflight.sh",
            "headRefOid": head,
            "result": "FAIL",
            "exit_code": 1,
            "command_contract": {"mode": "spec"},
            "log_path": str(log_path),
            "log_sha256": hashlib.sha256(failure_log.encode()).hexdigest(),
            "commands_sha256": commands_sha256,
        }
        receipt_path = receipt_dir / f"{head}-{commands_sha256}.json"
        _write_text(receipt_path, json.dumps(receipt, sort_keys=True))
        signature = failure_signature("smoke.spec.ts › test one")
        _write_text(
            override_path,
            "AFFECTED_TESTS: PASS_WITH_PREEXISTING_FAILURES\n"
            "provenance: pm-recorded-one-time-gate-exception\n"
            "no_full_suite: true\n"
            f"PR: {pr}\n"
            f"headRefOid: {head}\n"
            f"preflight_receipt: {receipt_path}\n"
            f"failure_signature: {signature}\n"
            "followup_issue: #7205\n"
            "authorization: Rajiv CTO decision — Abhijit CTO directive\n"
            "scope: env_load_differential_pass one-time ci+e2e mandatory\n",
        )
        ok, reason, kind = ci_start_override_dispatch(override_path, pr, head, REPO)
        check(
            "green_preexisting_full_regression",
            (ok, reason, kind) == (True, "ok", "preexisting_failure"),
            f"got ok={ok} reason={reason} kind={kind}",
        )

        # GREEN: the sealed mutation-scope class admits a packet bound to the
        # three CTO-verified artifact digests.
        _self_test_write_capture(pr, head)
        _self_test_write_marker(pr, head)
        _self_test_write_log(pr, head)
        _self_test_write_sealed_packet(pr, head)
        ok, reason, kind = ci_start_override_dispatch(override_path, pr, head, REPO)
        check(
            "green_sealed_admits",
            (ok, reason, kind) == (True, "ok", "mutation_scope_sealed"),
            f"got ok={ok} reason={reason} kind={kind}",
        )

        # GREEN: the CTO-adjudicated CI admission class admits an exact-head
        # REQUEST_CHANGES marker + VACUOUS_RED log packet with classifier-bound
        # capture_not_required evidence and both CTO decision timestamps.
        _self_test_write_cto_adjudicated_packet(pr, head)
        change_scope = _self_test_change_scope(head)
        ok, reason, kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, change_scope
        )
        check(
            "green_cto_adjudicated_admits",
            (ok, reason, kind) == (True, "ok", "cto_adjudicated_ci_admission"),
            f"got ok={ok} reason={reason} kind={kind}",
        )

        # Fail-closed: wrong head binding.
        _self_test_write_cto_adjudicated_packet(pr, head)
        packet_text = override_path.read_text(encoding="utf-8").replace(
            f"headRefOid: {head}", f"headRefOid: {'b' * 40}"
        )
        _write_text(override_path, packet_text)
        ok, reason, _kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, change_scope
        )
        check(
            "fail_closed_cto_wrong_head",
            (ok, reason) == (False, "cto_contract_missing"),
            f"got ok={ok} reason={reason}",
        )

        # Fail-closed: artifact digest mismatch.
        _self_test_write_cto_adjudicated_packet(
            pr, head, digest_overrides={"marker": "0" * 64}
        )
        ok, reason, _kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, change_scope
        )
        check(
            "fail_closed_cto_digest_mismatch",
            (ok, reason) == (False, "cto_artifact_digest_mismatch"),
            f"got ok={ok} reason={reason}",
        )

        # Fail-closed: marker is not REQUEST_CHANGES.
        _self_test_write_cto_adjudicated_packet(pr, head, status="APPROVE")
        ok, reason, _kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, change_scope
        )
        check(
            "fail_closed_cto_marker_not_request_changes",
            (ok, reason) == (False, "cto_marker_not_request_changes"),
            f"got ok={ok} reason={reason}",
        )

        # Fail-closed: affected-test log without VACUOUS_RED.
        _self_test_write_cto_adjudicated_packet(pr, head, vacuous=False)
        ok, reason, _kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, change_scope
        )
        check(
            "fail_closed_cto_log_not_vacuous_red",
            (ok, reason) == (False, "cto_log_not_vacuous_red"),
            f"got ok={ok} reason={reason}",
        )

        # Fail-closed: followup not open live.
        _self_test_write_cto_adjudicated_packet(
            pr, head, followup="followup_issue: #9999"
        )
        ok, reason, _kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, change_scope
        )
        check(
            "fail_closed_cto_followup_not_open",
            (ok, reason) == (False, "cto_followup_not_open"),
            f"got ok={ok} reason={reason}",
        )

        # Fail-closed: decision timestamp drift between the timestamp line and
        # the authorization text.
        _self_test_write_cto_adjudicated_packet(pr, head)
        packet_text = override_path.read_text(encoding="utf-8").replace(
            "authorization_ts: 1786474093",
            "authorization_ts: 17864740931",
        )
        _write_text(override_path, packet_text)
        ok, reason, _kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, change_scope
        )
        check(
            "fail_closed_cto_decision_ts_mismatch",
            (ok, reason) == (False, "cto_decision_timestamp_mismatch"),
            f"got ok={ok} reason={reason}",
        )

        # Fail-closed: scope waives current-head CI/E2E/LFC or merge.
        _self_test_write_cto_adjudicated_packet(
            pr, head, scope="one-time no merge authority"
        )
        ok, reason, _kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, change_scope
        )
        check(
            "fail_closed_cto_scope_not_fail_closed",
            (ok, reason) == (False, "cto_scope_not_fail_closed"),
            f"got ok={ok} reason={reason}",
        )

        # Fail-closed: capture evidence missing / classifier says required /
        # declared paths drift from the classifier.
        _self_test_write_cto_adjudicated_packet(
            pr, head, omit_line="capture_basis:"
        )
        ok, reason, _kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, change_scope
        )
        check(
            "fail_closed_cto_capture_evidence_missing",
            (ok, reason) == (False, "cto_capture_evidence_missing"),
            f"got ok={ok} reason={reason}",
        )
        _self_test_write_cto_adjudicated_packet(pr, head)
        ok, reason, _kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, change_scope
        )
        check(
            "green_cto_capture_not_required_replay",
            (ok, reason) == (True, "ok"),
            f"got ok={ok} reason={reason}",
        )

        # One-time consumption for the CTO-adjudicated class.
        _self_test_write_cto_adjudicated_packet(pr, head)

        class _CtoCommitArgs:
            commit_reentry = True
            source = "pm-review-done"
            affected_test_proof = str(override_path)

        commit_args = _CtoCommitArgs()
        error = sealed_mutation_scope_override_commit(
            commit_args, "override", head
        )
        consumed_text = override_path.read_text(encoding="utf-8")
        check(
            "cto_commit_consumes_once",
            error == ""
            and "consumed: yes" in consumed_text
            and "consumed_marker: sha256:" in consumed_text,
            f"got error={error}",
        )
        error = sealed_mutation_scope_override_commit(
            commit_args, "override", head
        )
        check(
            "cto_commit_rejects_reuse",
            error == "ci_start_override_sealed_packet_reuse",
            f"got error={error}",
        )
        ok, reason, _kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, change_scope
        )
        check(
            "cto_dispatch_rejects_consumed_packet",
            (ok, reason) == (False, "cto_packet_reuse"),
            f"got ok={ok} reason={reason}",
        )

        # GREEN: the rescue-packet-bound CTO CI admission class admits a fully
        # sealed packet (rescue packet + diff/plan/log/preflight/capture/
        # landed receipts + decision ts).
        _self_test_write_rescue_admission_packet(pr, head)
        rescue_scope = dict(change_scope)
        rescue_scope["editor_changed"] = False
        rescue_scope["lfc_required"] = False
        ok, reason, kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, rescue_scope, None
        )
        check(
            "green_cto_rescue_packet_admits",
            (ok, reason, kind) == (True, "ok", "cto_rescue_packet_ci_admission"),
            f"got ok={ok} reason={reason} kind={kind}",
        )

        # Fail-closed: rescue packet digest drift.
        _self_test_write_rescue_admission_packet(pr, head)
        lines = override_path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if line.startswith("rescue_packet:"):
                lines[index] = re.sub(
                    r"(rescue_packet:\s+\S+\s+sha256:)[0-9a-f]{64}",
                    r"\g<1>" + "f" * 64,
                    line,
                )
                break
        _write_text(override_path, "\n".join(lines) + "\n")
        ok, reason, _kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, rescue_scope, None
        )
        check(
            "fail_closed_cto_rescue_packet_digest",
            (ok, reason) == (False, "cto_rescue_packet_digest_mismatch"),
            f"got ok={ok} reason={reason}",
        )

        # Fail-closed: decision timestamp drift.
        _self_test_write_rescue_admission_packet(pr, head)
        packet_text = override_path.read_text(encoding="utf-8").replace(
            f"authorization_ts: {CTO_DEGRADED_DELIVERY_DECISION_TS}",
            f"authorization_ts: {CTO_DEGRADED_DELIVERY_DECISION_TS}1",
        )
        _write_text(override_path, packet_text)
        ok, reason, _kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, rescue_scope, None
        )
        check(
            "fail_closed_cto_rescue_decision_ts",
            (ok, reason) == (False, "cto_rescue_decision_ts_mismatch"),
            f"got ok={ok} reason={reason}",
        )

        # Fail-closed: forbidden #7251 followup.
        _self_test_write_rescue_admission_packet(pr, head)
        _write_text(
            override_path,
            override_path.read_text(encoding="utf-8") + "followup_issue: #7251\n",
        )
        ok, reason, _kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, rescue_scope, None
        )
        check(
            "fail_closed_cto_rescue_forbidden_followup",
            (ok, reason) == (False, "cto_rescue_forbidden_followup"),
            f"got ok={ok} reason={reason}",
        )

        # Fail-closed: LFC not exempt / capture required drift.
        _self_test_write_rescue_admission_packet(pr, head, omit_line="lfc_not_required:")
        ok, reason, _kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, rescue_scope, None
        )
        check(
            "fail_closed_cto_rescue_lfc",
            (ok, reason) == (False, "cto_rescue_lfc_not_required_missing"),
            f"got ok={ok} reason={reason}",
        )
        _self_test_write_rescue_admission_packet(pr, head)

        def capture_required(*_args: object, **_kwargs: object) -> dict[str, Any]:
            return {
                "capture_required": True,
                "pr": pr,
                "headRefOid": head,
                "reason": "capture_sensitive",
                "changed_paths": [],
            }

        globals()["capture_requirement"] = capture_required
        ok, reason, _kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, rescue_scope, None
        )
        check(
            "fail_closed_cto_rescue_capture_required",
            (ok, reason) == (False, "cto_rescue_capture_required"),
            f"got ok={ok} reason={reason}",
        )
        globals()["capture_requirement"] = _self_test_capture_requirement

        # One-time consumption for the rescue-packet class.
        _self_test_write_rescue_admission_packet(pr, head)

        class _RescueCommitArgs:
            commit_reentry = True
            source = "pm-review-done"
            affected_test_proof = str(override_path)

        rescue_commit = _RescueCommitArgs()
        error = sealed_mutation_scope_override_commit(
            rescue_commit, "override", head
        )
        consumed_text = override_path.read_text(encoding="utf-8")
        check(
            "cto_rescue_commit_consumes_once",
            error == ""
            and "consumed: yes" in consumed_text
            and "consumed_marker: sha256:" in consumed_text,
            f"got error={error}",
        )
        error = sealed_mutation_scope_override_commit(
            rescue_commit, "override", head
        )
        check(
            "cto_rescue_commit_rejects_reuse",
            error == "ci_start_override_sealed_packet_reuse",
            f"got error={error}",
        )
        ok, reason, _kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, rescue_scope, None
        )
        check(
            "cto_rescue_dispatch_rejects_consumed",
            (ok, reason) == (False, "cto_rescue_packet_reuse"),
            f"got ok={ok} reason={reason}",
        )

        # GREEN: the marker-PASS CTO-adjudicated variant admits an exact-head
        # Phase-A PASS marker + canonical plan/log + review receipt + GREEN
        # capture-run receipt + pin + source wake.
        _self_test_write_marker_pass_packet(pr, head)
        marker_scope = {
            "head": head,
            "control_plane_only": False,
            "product_changed": True,
            "changed_files": ["modal/audio/processor.py"],
            "rules_sha256": "5" * 64,
            "classifier_sha256": "6" * 64,
        }
        ok, reason, kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, marker_scope, None
        )
        check(
            "green_cto_marker_pass_admits",
            (ok, reason, kind) == (True, "ok", "cto_marker_pass_ci_admission"),
            f"got ok={ok} reason={reason} kind={kind}",
        )

        # Fail-closed: wrong head binding.
        _self_test_write_marker_pass_packet(pr, head)
        packet_text = override_path.read_text(encoding="utf-8").replace(
            f"headRefOid: {head}", f"headRefOid: {'b' * 40}"
        )
        _write_text(override_path, packet_text)
        ok, reason, _kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, marker_scope, None
        )
        check(
            "fail_closed_cto_marker_wrong_head",
            (ok, reason) == (False, "cto_marker_contract_missing"),
            f"got ok={ok} reason={reason}",
        )

        # Fail-closed: non-PASS marker and missing VACUOUS_RED classification.
        _self_test_write_marker_pass_packet(pr, head, marker_status="BLOCKED")
        ok, reason, _kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, marker_scope, None
        )
        check(
            "fail_closed_cto_marker_not_pass",
            (ok, reason) == (False, "cto_marker_not_pass"),
            f"got ok={ok} reason={reason}",
        )
        _self_test_write_marker_pass_packet(pr, head, vacuous=False)
        ok, reason, _kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, marker_scope, None
        )
        check(
            "fail_closed_cto_marker_log_not_vacuous_red",
            (ok, reason) == (False, "cto_marker_log_not_vacuous_red"),
            f"got ok={ok} reason={reason}",
        )

        # Fail-closed: capture run is not GREEN live.
        _self_test_write_marker_pass_packet(pr, head)
        matrix_gh = gh_json

        def failing_run(*_args: object, **_kwargs: object) -> dict[str, Any]:
            return {
                "headSha": "a" * 40,
                "status": "completed",
                "conclusion": "failure",
                "event": "workflow_dispatch",
                "displayTitle": f"remote-capture-pr-{pr}-head-" + "a" * 40,
                "workflowName": "E2E LLM Proxy Capture (manual)",
            }

        globals()["gh_json"] = failing_run
        ok, reason, _kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, marker_scope, None
        )
        globals()["gh_json"] = matrix_gh
        check(
            "fail_closed_cto_marker_capture_run",
            (ok, reason) == (False, "cto_marker_capture_run_mismatch"),
            f"got ok={ok} reason={reason}",
        )

        # One-time consumption for the marker-PASS class.
        _self_test_write_marker_pass_packet(pr, head)

        class _MarkerCommitArgs:
            commit_reentry = True
            source = "pm-review-done"
            affected_test_proof = str(override_path)

        marker_commit = _MarkerCommitArgs()
        error = sealed_mutation_scope_override_commit(
            marker_commit, "override", head
        )
        consumed_text = override_path.read_text(encoding="utf-8")
        check(
            "cto_marker_commit_consumes_once",
            error == ""
            and "consumed: yes" in consumed_text
            and "consumed_marker: sha256:" in consumed_text,
            f"got error={error}",
        )
        error = sealed_mutation_scope_override_commit(
            marker_commit, "override", head
        )
        check(
            "cto_marker_commit_rejects_reuse",
            error == "ci_start_override_sealed_packet_reuse",
            f"got error={error}",
        )
        ok, reason, _kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, marker_scope, None
        )
        check(
            "cto_marker_dispatch_rejects_consumed",
            (ok, reason) == (False, "cto_marker_packet_reuse"),
            f"got ok={ok} reason={reason}",
        )

        # Fail-closed: wrong head binding.
        _self_test_write_sealed_packet(pr, head)
        wrong_head = ("b" * 40) if head != ("b" * 40) else ("c" * 40)
        packet_text = override_path.read_text(encoding="utf-8").replace(
            f"headRefOid: {head}", f"headRefOid: {wrong_head}"
        )
        _write_text(override_path, packet_text)
        ok, reason, _kind = ci_start_override_dispatch(override_path, pr, head, REPO)
        check(
            "fail_closed_wrong_head",
            (ok, reason) == (False, "sealed_contract_missing"),
            f"got ok={ok} reason={reason}",
        )

        # Fail-closed: headRefOid with a trailing suffix on the valid head.
        # The exact-tuple binding must reject `<valid-head>suffix`, never admit
        # it through a substring match.
        _self_test_write_sealed_packet(pr, head)
        packet_text = override_path.read_text(encoding="utf-8").replace(
            f"headRefOid: {head}", f"headRefOid: {head}deadbeef"
        )
        _write_text(override_path, packet_text)
        ok, reason, _kind = ci_start_override_dispatch(override_path, pr, head, REPO)
        check(
            "fail_closed_head_suffix",
            (ok, reason) == (False, "sealed_contract_missing"),
            f"got ok={ok} reason={reason}",
        )

        # Fail-closed: different-case headRefOid (uppercase hex) must be
        # rejected; only the exact lowercase 40-hex head is admitted.
        _self_test_write_sealed_packet(pr, head)
        packet_text = override_path.read_text(encoding="utf-8").replace(
            f"headRefOid: {head}", f"headRefOid: {head.upper()}"
        )
        _write_text(override_path, packet_text)
        ok, reason, _kind = ci_start_override_dispatch(override_path, pr, head, REPO)
        check(
            "fail_closed_head_case",
            (ok, reason) == (False, "sealed_contract_missing"),
            f"got ok={ok} reason={reason}",
        )

        # Fail-closed: truncated headRefOid (39 hex chars) must be rejected.
        _self_test_write_sealed_packet(pr, head)
        packet_text = override_path.read_text(encoding="utf-8").replace(
            f"headRefOid: {head}", f"headRefOid: {head[:-1]}"
        )
        _write_text(override_path, packet_text)
        ok, reason, _kind = ci_start_override_dispatch(override_path, pr, head, REPO)
        check(
            "fail_closed_head_truncated",
            (ok, reason) == (False, "sealed_contract_missing"),
            f"got ok={ok} reason={reason}",
        )

        # Fail-closed: PR field must be the exact PR number; a different PR
        # and a `<pr>suffix` variant are both rejected.
        _self_test_write_sealed_packet(pr, head)
        packet_text = override_path.read_text(encoding="utf-8").replace(
            f"PR: {pr}", f"PR: {pr - 1}"
        )
        _write_text(override_path, packet_text)
        ok, reason, _kind = ci_start_override_dispatch(override_path, pr, head, REPO)
        check(
            "fail_closed_wrong_pr",
            (ok, reason) == (False, "sealed_contract_missing"),
            f"got ok={ok} reason={reason}",
        )
        _self_test_write_sealed_packet(pr, head)
        packet_text = override_path.read_text(encoding="utf-8").replace(
            f"PR: {pr}", f"PR: {pr}0"
        )
        _write_text(override_path, packet_text)
        ok, reason, _kind = ci_start_override_dispatch(override_path, pr, head, REPO)
        check(
            "fail_closed_pr_suffix",
            (ok, reason) == (False, "sealed_contract_missing"),
            f"got ok={ok} reason={reason}",
        )

        # Fail-closed: artifact digest mismatch.
        _self_test_write_sealed_packet(
            pr, head, digest_overrides={"marker": "0" * 64}
        )
        ok, reason, _kind = ci_start_override_dispatch(override_path, pr, head, REPO)
        check(
            "fail_closed_wrong_digest",
            (ok, reason) == (False, "sealed_artifact_digest_mismatch"),
            f"got ok={ok} reason={reason}",
        )

        # Fail-closed: missing followup issue.
        _self_test_write_sealed_packet(pr, head, omit_line="followup_issue:")
        ok, reason, _kind = ci_start_override_dispatch(override_path, pr, head, REPO)
        check(
            "fail_closed_missing_followup",
            (ok, reason) == (False, "sealed_followup_missing"),
            f"got ok={ok} reason={reason}",
        )

        # Fail-closed: followup not open live.
        _self_test_write_sealed_packet(pr, head, followup="followup_issue: #9999")
        ok, reason, _kind = ci_start_override_dispatch(override_path, pr, head, REPO)
        check(
            "fail_closed_followup_not_open",
            (ok, reason) == (False, "sealed_followup_not_open"),
            f"got ok={ok} reason={reason}",
        )

        # Fail-closed: wrong authorization authority.
        _self_test_write_sealed_packet(pr, head, authorization="Someone Else")
        ok, reason, _kind = ci_start_override_dispatch(override_path, pr, head, REPO)
        check(
            "fail_closed_wrong_authorization",
            (ok, reason) == (False, "sealed_cto_authorization_missing"),
            f"got ok={ok} reason={reason}",
        )

        # Fail-closed: scope terms that waive current-head CI/E2E or merge.
        _self_test_write_sealed_packet(
            pr, head, scope="one-time fire only; no merge authority"
        )
        ok, reason, _kind = ci_start_override_dispatch(override_path, pr, head, REPO)
        check(
            "fail_closed_scope_not_fail_closed",
            (ok, reason) == (False, "sealed_scope_not_fail_closed"),
            f"got ok={ok} reason={reason}",
        )

        # Fail-closed: non-PASS review marker artifact.
        _self_test_write_marker(pr, head, status="BLOCKED")
        _self_test_write_sealed_packet(pr, head)
        ok, reason, _kind = ci_start_override_dispatch(override_path, pr, head, REPO)
        check(
            "fail_closed_marker_not_pass",
            (ok, reason) == (False, "sealed_review_marker_not_pass"),
            f"got ok={ok} reason={reason}",
        )

        # Fail-closed: non-PASS capture artifact.
        _self_test_write_marker(pr, head)
        _self_test_write_capture(pr, head, status="FAILED")
        _self_test_write_sealed_packet(pr, head)
        ok, reason, _kind = ci_start_override_dispatch(override_path, pr, head, REPO)
        check(
            "fail_closed_capture_not_pass",
            reason.startswith("sealed_capture_not_pass:"),
            f"got ok={ok} reason={reason}",
        )

        # Fail-closed: base verification log without the documented
        # mutation-scope limitation marker.
        _self_test_write_capture(pr, head)
        _self_test_write_log(pr, head, vacuous=False)
        _self_test_write_sealed_packet(pr, head)
        ok, reason, _kind = ci_start_override_dispatch(override_path, pr, head, REPO)
        check(
            "fail_closed_base_verification_not_pass",
            (ok, reason) == (False, "sealed_base_verification_not_pass"),
            f"got ok={ok} reason={reason}",
        )

        # One-time consumption: full admission commits the sentinel; a later
        # admission of the same packet fails closed on reuse.
        _self_test_write_log(pr, head)
        _self_test_write_sealed_packet(pr, head)

        class _Args:
            commit_reentry = False
            source = "pm-review-done"
            affected_test_proof = str(override_path)

        preflight_args = _Args()
        error = sealed_mutation_scope_override_commit(
            preflight_args, "override", head
        )
        check(
            "commit_preflight_side_effect_free",
            error == "" and "consumed: no" in override_path.read_text(encoding="utf-8"),
            f"got error={error}",
        )
        commit_args = _Args()
        commit_args.commit_reentry = True
        error = sealed_mutation_scope_override_commit(commit_args, "override", head)
        consumed_text = override_path.read_text(encoding="utf-8")
        check(
            "commit_consumes_once",
            error == ""
            and "consumed: yes" in consumed_text
            and "consumed_marker: sha256:" in consumed_text,
            f"got error={error}",
        )
        error = sealed_mutation_scope_override_commit(commit_args, "override", head)
        check(
            "commit_rejects_reuse",
            error == "ci_start_override_sealed_packet_reuse",
            f"got error={error}",
        )
        ok, reason, _kind = ci_start_override_dispatch(override_path, pr, head, REPO)
        check(
            "dispatch_rejects_consumed_packet",
            (ok, reason) == (False, "sealed_packet_reuse"),
            f"got ok={ok} reason={reason}",
        )
        _self_test_write_sealed_packet(pr, head, consumed="yes")
        ok, reason, _kind = ci_start_override_dispatch(override_path, pr, head, REPO)
        check(
            "dispatch_rejects_consumed_yes",
            (ok, reason) == (False, "sealed_packet_reuse"),
            f"got ok={ok} reason={reason}",
        )

        # Ordinal-3 FUNCTIONAL_BLOCK correction (one-time consumption
        # idempotency): the read/check/rewrite must be serialized so a
        # concurrent or sequential second commit fails closed with
        # sealed_packet_reuse instead of silently re-admitting. The commit
        # takes an exclusive flock on the per-packet lock file before reading
        # the sentinel, so a second writer can never observe `consumed: no`
        # after the first writer has consumed the packet.
        #
        # Failure before the first write: a failed rewrite must leave the
        # packet reusable (no consumed sentinel), and a retry after the
        # obstacle is removed must consume exactly once (interruption case:
        # the sentinel creation precedes any success return).
        _self_test_write_sealed_packet(pr, head)
        blocked_tmp = override_path.with_name(
            f".{override_path.name}.sealed-consume.{os.getpid()}.tmp"
        )
        try:
            blocked_tmp.mkdir()
            error = sealed_mutation_scope_override_commit(commit_args, "override", head)
            check(
                "commit_failed_write_leaves_packet_reusable",
                error.startswith("ci_start_override_sealed_commit_failed:")
                and "consumed: no" in override_path.read_text(encoding="utf-8"),
                f"got error={error}",
            )
        finally:
            blocked_tmp.rmdir()
        error = sealed_mutation_scope_override_commit(commit_args, "override", head)
        consumed_text = override_path.read_text(encoding="utf-8")
        check(
            "commit_retry_after_failed_write_consumes_once",
            error == ""
            and "consumed: yes" in consumed_text
            and "consumed_marker: sha256:" in consumed_text,
            f"got error={error}",
        )

        # Deterministic lock-block check: while this test holds the exclusive
        # lock, a commit invocation must block (and must not rewrite the
        # packet); after the lock is released it consumes exactly once. On a
        # lockless implementation the commit completes immediately while the
        # lock is held, so this check fails deterministically.
        _self_test_write_sealed_packet(pr, head)
        lock_path = override_path.with_name(
            f"{override_path.name}.sealed-consume.lock"
        )
        holder_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(holder_fd, fcntl.LOCK_EX)
        blocked_results: list[str] = []
        blocked_thread = threading.Thread(
            target=lambda: blocked_results.append(
                sealed_mutation_scope_override_commit(commit_args, "override", head)
            )
        )
        try:
            blocked_thread.start()
            blocked_thread.join(timeout=0.5)
            check(
                "concurrent_commit_blocks_on_lock",
                blocked_thread.is_alive()
                and "consumed: no" in override_path.read_text(encoding="utf-8"),
                f"commit finished while lock held; results={blocked_results}",
            )
        finally:
            fcntl.flock(holder_fd, fcntl.LOCK_UN)
            os.close(holder_fd)
            blocked_thread.join(timeout=5)
        check(
            "concurrent_commit_consumes_after_lock_release",
            blocked_results == [""]
            and "consumed: yes" in override_path.read_text(encoding="utf-8"),
            f"got results={blocked_results}",
        )

        # Concurrent pair on the same sealed packet: exactly one commit
        # succeeds and the other fails closed with sealed_packet_reuse; the
        # consumed packet is then rejected by the dispatch validator.
        _self_test_write_sealed_packet(pr, head)
        pair_results: list[str] = []
        pair_barrier = threading.Barrier(2)

        def _consume_pair() -> None:
            pair_barrier.wait(timeout=5)
            pair_results.append(
                sealed_mutation_scope_override_commit(commit_args, "override", head)
            )

        pair_threads = [threading.Thread(target=_consume_pair) for _ in range(2)]
        for thread in pair_threads:
            thread.start()
        for thread in pair_threads:
            thread.join(timeout=10)
        consumed_text = override_path.read_text(encoding="utf-8")
        check(
            "concurrent_commit_exactly_one_ok",
            sorted(pair_results) == ["", "ci_start_override_sealed_packet_reuse"]
            and consumed_text.count("consumed: yes") == 1
            and consumed_text.count("consumed_marker: sha256:") == 1,
            f"got results={pair_results}",
        )
        ok, reason, _kind = ci_start_override_dispatch(override_path, pr, head, REPO)
        check(
            "concurrent_pair_dispatch_rejects_consumed",
            (ok, reason) == (False, "sealed_packet_reuse"),
            f"got ok={ok} reason={reason}",
        )

        # GREEN: the CTO-adjudicated NO_PATCH_REQUIRED rescue class admits a
        # packet bound to PR/head, the three CTO decision timestamps, the
        # canonical kimi3 packet path+digest, NO_PATCH_REQUIRED /
        # continue_verified_head / changed_paths none, and exact RED/GREEN
        # receipt digests. Capture classifier says capture not required.
        no_patch_scope = {
            "head": head,
            "control_plane_only": False,
            "product_changed": True,
            "editor_changed": False,
            "lfc_required": False,
            "changed_files": ["lib/file.ts"],
            "rules_sha256": "5" * 64,
            "classifier_sha256": "6" * 64,
        }
        _self_test_write_no_patch_rescue_packet(pr, head)
        ok, reason, kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, no_patch_scope, None
        )
        check(
            "green_cto_no_patch_rescue_admits",
            (ok, reason, kind) == (True, "ok", "cto_no_patch_rescue_ci_admission"),
            f"got ok={ok} reason={reason} kind={kind}",
        )

        # Fail-closed: wrong head binding.
        _self_test_write_no_patch_rescue_packet(pr, head)
        packet_text = override_path.read_text(encoding="utf-8").replace(
            f"headRefOid: {head}", f"headRefOid: {'c' * 40}"
        )
        _write_text(override_path, packet_text)
        ok, reason, _kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, no_patch_scope, None
        )
        check(
            "fail_closed_cto_no_patch_wrong_head",
            (ok, reason) == (False, "cto_no_patch_contract_missing"),
            f"got ok={ok} reason={reason}",
        )

        # Fail-closed: PATCH_READY packet (wrong terminal) must reject.
        _self_test_write_no_patch_rescue_packet(
            pr, head, packet_terminal="PATCH_READY"
        )
        ok, reason, _kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, no_patch_scope, None
        )
        check(
            "fail_closed_cto_no_patch_patch_ready",
            (ok, reason) == (False, "cto_no_patch_packet_fields_invalid"),
            f"got ok={ok} reason={reason}",
        )

        # Fail-closed: packet digest mismatch.
        _self_test_write_no_patch_rescue_packet(pr, head)
        lines = override_path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if line.startswith("rescue_packet:"):
                lines[index] = re.sub(
                    r"(rescue_packet:\s+\S+\s+sha256:)[0-9a-f]{64}",
                    r"\g<1>" + "f" * 64,
                    line,
                )
                break
        _write_text(override_path, "\n".join(lines) + "\n")
        ok, reason, _kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, no_patch_scope, None
        )
        check(
            "fail_closed_cto_no_patch_digest_mismatch",
            (ok, reason) == (False, "cto_no_patch_packet_digest_mismatch"),
            f"got ok={ok} reason={reason}",
        )

        # Fail-closed: wrong decision timestamp.
        _self_test_write_no_patch_rescue_packet(pr, head)
        packet_text = override_path.read_text(encoding="utf-8").replace(
            f"authorization_ts: {CTO_NO_PATCH_RESCUE_DECISION_TS[0]}",
            f"authorization_ts: {CTO_NO_PATCH_RESCUE_DECISION_TS[0]}1",
            1,
        )
        _write_text(override_path, packet_text)
        ok, reason, _kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, no_patch_scope, None
        )
        check(
            "fail_closed_cto_no_patch_wrong_ts",
            (ok, reason) == (False, "cto_no_patch_decision_ts_mismatch"),
            f"got ok={ok} reason={reason}",
        )

        # One-time consumption for the NO_PATCH class.
        _self_test_write_no_patch_rescue_packet(pr, head)

        class _NoPatchCommitArgs:
            commit_reentry = True
            source = "pm-review-done"
            affected_test_proof = str(override_path)

        no_patch_commit = _NoPatchCommitArgs()
        error = sealed_mutation_scope_override_commit(
            no_patch_commit, "override", head
        )
        consumed_text = override_path.read_text(encoding="utf-8")
        check(
            "cto_no_patch_commit_consumes_once",
            error == ""
            and "consumed: yes" in consumed_text
            and "consumed_marker: sha256:" in consumed_text,
            f"got error={error}",
        )
        error = sealed_mutation_scope_override_commit(
            no_patch_commit, "override", head
        )
        check(
            "cto_no_patch_commit_rejects_reuse",
            error == "ci_start_override_sealed_packet_reuse",
            f"got error={error}",
        )
        ok, reason, _kind = ci_start_override_dispatch(
            override_path, pr, head, REPO, no_patch_scope, None
        )
        check(
            "cto_no_patch_dispatch_rejects_consumed",
            (ok, reason) == (False, "cto_no_patch_packet_reuse"),
            f"got ok={ok} reason={reason}",
        )
    finally:
        globals()["gh_json"] = real_gh
        globals()["capture_requirement"] = real_capture_requirement
        globals()["RESCUE_PACKET_ARTIFACT_ROOT"] = real_rescue_packet_root
    return failures


def _self_test_cleanup() -> None:
    pr, head = SELF_TEST_PR, SELF_TEST_HEAD
    request = "a45ea1c667c84b17c112a45ee253c9a7"
    names = (
        f"pm-ci-start-override-{pr}-{head}.ok",
        f"pm-ci-start-override-{pr}-{head}.ok.sealed-consume.lock",
        f"pm-claude-code-review-{pr}-{head}.md",
        f"codex-app-code-review-{pr}.txt",
        f"affected-test-proof-{pr}-{head}.log",
        f"affected-test-plan-{pr}-{head}.json",
        f"capture-local-proof-{pr}-{head}.ok",
        f"affected-test-preflight-log-{pr}-{head}.log",
        f"pm-kimi3-rescue-packet-{request}.md",
        f"pm-kimi3-rescue-diff-receipt-{request}-{head}.json",
        f"pm-capture-required-{pr}-{head}.json",
        f"cto-rescue-packet-ci-admission-receipt-{'c' * 40}.md",
        f"cto-rescue-preflight-{pr}-{head}.log",
        f"pm-review-done-receipt-{pr}-{head}.json",
        f"pm-capture-run-verified-{pr}-{head}.json",
    )
    for name in names:
        try:
            (_self_test_artifacts_dir() / name).unlink(missing_ok=True)
        except OSError:
            pass
    try:
        (
            _self_test_artifacts_dir()
            / f".pm-ci-start-override-{pr}-{head}.ok.sealed-consume.{os.getpid()}.tmp"
        ).rmdir()
    except OSError:
        pass
    try:
        (
            Path("/tmp/affected-test-preflight-receipts")
            / f"{head}-{'d' * 64}.json"
        ).unlink(missing_ok=True)
    except OSError:
        pass


def run_self_tests() -> int:
    """Bounded transition matrix for the one-time CI-start override classes.

    RED: the pre-existing-failure class still rejects a packet without a
    genuine failed-Playwright preflight receipt (reproduces the #7198
    incident reject ci_start_override_override_receipt_missing).
    GREEN: the sealed mutation-scope class admits a packet bound to three
    artifact paths + SHA-256 digests, an open followup, the CTO authorization,
    fail-closed scope terms, and an un-consumed sentinel. Fail-closed: wrong
    head, suffixed/case-variant/truncated headRefOid, wrong or suffixed PR
    number, wrong digest, missing/closed followup, wrong authorization, scope
    waiver, non-PASS review/capture/base-verification artifacts, and packet
    reuse all reject. Consumption happens only through the commit sentinel on
    the final --commit-reentry gate call, serialized by an exclusive flock:
    a concurrent or sequential second commit on the same sealed packet fails
    closed with sealed_packet_reuse (exactly one ok in a concurrent pair),
    the commit blocks while another holder owns the lock, and a failed
    rewrite leaves the packet reusable so a retry can still consume it.
    """
    print(f"SELF_TEST start pr={SELF_TEST_PR} head={SELF_TEST_HEAD}")
    failures = _self_test_run_override_matrix()
    _self_test_cleanup()
    if failures:
        print("SELF_TEST FAILED " + ",".join(failures))
        return 1
    print("SELF_TEST PASS all")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", type=int, default=0)
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--expect-head", default="")
    parser.add_argument("--source", default="manual")
    parser.add_argument(
        "--commit-reentry",
        action="store_true",
        help=(
            "Atomically consume a PM review rerun re-entry admission. "
            "Only the final label-control gate call should set this; preflight checks "
            "remain side-effect free."
        ),
    )
    parser.add_argument(
        "--rescue-authorized",
        action="store_true",
        help=(
            "Allow an exact-head terminal rescue packet already validated by "
            "pm-transition to recover a transition-owned blocked-rework state."
        ),
    )
    parser.add_argument(
        "--no-patch-rescue-authorized",
        action="store_true",
        help=(
            "CTO-adjudicated NO_PATCH_REQUIRED rescue consumption (#7268): "
            "the validated canonical override discharges the rescue-owned "
            "pm-blocked:pm-gate label at CI start (the transition clears it "
            "after the gate admits)."
        ),
    )
    parser.add_argument(
        "--phase-a-authorized",
        action="store_true",
        help=(
            "Allow pm-review-done, after validating a current-head Phase-A PASS, "
            "to discharge only pm-blocked:codex."
        ),
    )
    parser.add_argument(
        "--affected-test-proof",
        default="",
        help="DEPRECATED/IGNORED ordinary affected-test proof (retired per Rajiv 1786811850.717079); only the typed CTO override path is still validated",
    )
    parser.add_argument(
        "--ci-stale-run-classified",
        type=int,
        default=0,
        help="Named superseded failed run (GitHub run databaseId) for the typed ci-stale-run-classified closure: when the holds validate (exact live head, Fable marker PASS naming blocker_reviewed pm-blocked:ci at /tmp/pm-claude-code-review-<PR>-<head>.md, and the named run completed terminal-bad at a SUPERSEDED head), pm-blocked:ci is discharged for the pm-review-done source ONLY; every other blocker stays closed and any deviation fails closed. No affected-test proof or sealed local-preflight receipt is required (Rajiv 1786812200.371389)",
    )
    parser.add_argument("--warn-recent-update-minutes", type=int, default=3)
    parser.add_argument(
        "--min-head-age-minutes",
        type=int,
        default=DEFAULT_MIN_HEAD_AGE_MINUTES,
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run the bounded one-time CI-start override transition matrix and exit",
    )
    args = parser.parse_args()

    if args.self_test:
        return run_self_tests()
    if not args.pr:
        parser.error("--pr is required")

    try:
        result = evaluate(args)
    except Exception as exc:  # noqa: BLE001 - fail closed with evidence.
        result = {
            "schema": "heydonna_pr_ci_readiness_gate",
            "version": 2,
            "status": "blocked",
            "ok": False,
            "source": args.source,
            "pr": int(args.pr),
            "reasons": [f"gate_error={exc}"],
            "required_actions": ["Fix the CI readiness gate error, then rerun the gate before starting label-gated CI."],
            "required_action": "Fix the CI readiness gate error, then rerun the gate before starting label-gated CI.",
            "warnings": [],
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"CI_READY: {'PASS' if result['ok'] else 'BLOCKED'}")
        for reason in result.get("reasons") or []:
            print(f"- {reason}")
        for action in result.get("required_actions") or []:
            print(f"- required_action: {action}")
        for warning in result.get("warnings") or []:
            print(f"- warning: {warning}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
