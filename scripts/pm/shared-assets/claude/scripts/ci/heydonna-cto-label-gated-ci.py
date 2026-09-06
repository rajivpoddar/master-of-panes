#!/usr/bin/env python3
"""CTO-owned, exact-head label-gated CI admission boundary.

The label ``pm-state:qa-passed-awaiting-ci`` is the paid CI/E2E trigger. This
adapter consumes the repository's existing readiness and visual proof gates,
binds the authoritative PR/issue tuple, and performs at most one additive
trigger per exact head. It never retries an uncertain GitHub effect.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
RUN_RE = re.compile(r"^[0-9]+$")
REPO = "heydonna-app/heydonna-app"
# The readiness gate is a shared installed control-plane dependency.  Calling
# the app-checkout sibling lets its resolver select an older visual gate.
READINESS_GATE = Path("/Users/rajiv/.claude/scripts/pr-ci-readiness-gate.py")
READINESS_GATE_SHA256 = "7dceab86419f53dba997c27273b46da0a9d6b4f4cdfbbc471d31d53dcf74c12a"
VISUAL_GATE = Path("/Users/rajiv/.claude/scripts/qa-visual-proof-gate.py")
VISUAL_GATE_SHA256 = "b9bbb10da4adc35c50e1820da1ccb3e87dd4e0468b823783360efe1314e82271"
ISSUE_CONTRACT_VALIDATOR = Path("/Users/rajiv/.claude/scripts/validate-issue-contract-ledger.py")
ISSUE_CONTRACT_VALIDATOR_SHA256 = "3f503e12287ab82d81486f59f1f4acec9c01a6381e39998037c7271f300de4db"
EFFECT_RECEIPT_ROOT = Path("/Users/rajiv/.claude/control-plane/receipts/cto-label-gated-ci")
REENTRY_REASON_PREFIX = "current_head_ci_or_e2e_failed_use_rerun_not_label_trigger"


class Refusal(ValueError):
    pass


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _run(command: list[str], *, input_text: str | None = None, env: dict[str, str] | None = None, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, input=input_text, text=True, capture_output=True, check=False, env=env, timeout=timeout)


def _decode(text: str, label: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise Refusal(f"{label}_malformed") from exc


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _trusted_asset(path: Path, expected: str, label: str) -> None:
    if not path.is_file():
        raise Refusal(f"{label}_unavailable")
    if _sha(path) != expected:
        raise Refusal(f"{label}_provenance_drift")


def _gh_json(args: argparse.Namespace, command: list[str], label: str) -> Any:
    result = _run([args.gh, *command])
    if result.returncode != 0:
        raise Refusal(f"{label}_unavailable")
    return _decode(result.stdout, label)


def _checkout_head(args: argparse.Namespace) -> None:
    result = _run(["git", "-C", str(Path(args.checkout).resolve()), "rev-parse", "--verify", "HEAD"])
    if result.returncode != 0 or result.stdout.strip() != args.head:
        raise Refusal("checkout_head_drift")


def _live_pr(args: argparse.Namespace) -> dict[str, Any]:
    value = _gh_json(args, ["pr", "view", str(args.pr), "--repo", REPO, "--json", "number,headRefOid,state,isDraft,mergeable,mergeStateStatus,headRefName,closingIssuesReferences,labels"], "pr_state")
    if not isinstance(value, dict) or value.get("number") != args.pr:
        raise Refusal("pr_identity_malformed")
    if value.get("headRefOid") != args.head or not HEAD_RE.fullmatch(str(value.get("headRefOid") or "")):
        raise Refusal("head_drift")
    if value.get("state") != "OPEN" or value.get("isDraft") is not False:
        raise Refusal("pr_not_open_ready")
    if value.get("mergeable") != "MERGEABLE" or value.get("mergeStateStatus") not in {"CLEAN", "UNSTABLE"}:
        raise Refusal("pr_not_mergeable")
    refs = value.get("closingIssuesReferences")
    if not isinstance(refs, list):
        raise Refusal("linked_issue_relationship_unreadable")
    matches = [item for item in refs if isinstance(item, dict) and item.get("number") == args.issue]
    if len(matches) != 1 or len(refs) != 1:
        raise Refusal("linked_issue_relationship_ambiguous")
    return value


def _issue_body(args: argparse.Namespace) -> str:
    value = _gh_json(args, ["issue", "view", str(args.issue), "--repo", REPO, "--json", "number,body,state"], "issue_body")
    if not isinstance(value, dict) or value.get("number") != args.issue or not isinstance(value.get("body"), str):
        raise Refusal("issue_binding_malformed")
    if value.get("state") not in {"OPEN", "CLOSED"}:
        raise Refusal("issue_state_malformed")
    return value["body"]


def _clean_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in (
        "HEYDONNA_CHANGE_SCOPE",
        "HEYDONNA_QA_VISUAL_PROOF_GATE",
        "HEYDONNA_ISSUE_CONTRACT_LEDGER_VALIDATOR",
        "REQUIRED_CI_JOBS_FILE",
    ):
        env.pop(name, None)
    return env


def _gate(args: argparse.Namespace, *, reentry: bool) -> dict[str, Any]:
    _trusted_asset(READINESS_GATE, READINESS_GATE_SHA256, "readiness_gate")
    result = _run([sys.executable, str(READINESS_GATE), "--pr", str(args.pr), "--repo", REPO, "--expect-head", args.head, "--source", "cto-direct", "--json"], env=_clean_env(), timeout=120)
    payload = _decode(result.stdout, "readiness_gate")
    if not isinstance(payload, dict):
        raise Refusal("readiness_gate_malformed")
    reported_head = payload.get("headRefOid")
    if reported_head is not None and reported_head != args.head:
        raise Refusal("readiness_head_drift")
    if reported_head is None:
        if result.returncode != 0 or payload.get("ok") is not True:
            reason = payload.get("reason") or payload.get("error") or "readiness_gate_failed"
            raise Refusal(f"readiness_gate_error:{reason}")
        raise Refusal("readiness_gate_output_missing_head")
    artifacts = payload.get("artifacts")
    workflows = artifacts.get("workflows") if isinstance(artifacts, dict) else None
    if not isinstance(workflows, dict) or workflows.get("state") not in {"not_started", "in_progress", "green", "failed", "cancelled", "partial", "unknown"}:
        raise Refusal("workflow_inventory_malformed")
    scope = artifacts.get("change_scope") if isinstance(artifacts, dict) else None
    if not isinstance(scope, dict) or scope.get("head") != args.head:
        raise Refusal("trusted_classifier_missing")
    if scope.get("ci_required") is not True or scope.get("e2e_required") is not True or scope.get("control_plane_only") is not False:
        raise Refusal("required_ci_e2e_missing")
    if reentry:
        reasons = payload.get("reasons")
        if workflows.get("state") != "failed" or not isinstance(reasons, list) or len(reasons) != 1:
            raise Refusal(f"reentry_workflow_state_{workflows.get('state')}")
        reason = str(reasons[0])
        if not reason.startswith(REENTRY_REASON_PREFIX):
            raise Refusal("reentry_reason_not_authorized")
        if "requires_rework" in reason:
            raise Refusal("reentry_current_head_requires_rework")
        latest = artifacts.get("latest_ci_verdict") if isinstance(artifacts, dict) else None
        current_run_id = latest.get("run_id") if isinstance(latest, dict) else None
        if current_run_id is None:
            current_run_id = workflows.get("bad_run_id")
        if str(current_run_id) != str(args.source_run_id) or f"run={args.source_run_id}" not in reason:
            raise Refusal("reentry_source_run_not_current")
    elif result.returncode != 0 or payload.get("ok") is not True:
        reasons = payload.get("reasons") or ["readiness_gate_blocked"]
        raise Refusal("readiness_gate_blocked:" + ",".join(str(reason) for reason in reasons))
    return payload


def _visual_proof(args: argparse.Namespace, issue_body_sha256: str) -> None:
    _trusted_asset(VISUAL_GATE, VISUAL_GATE_SHA256, "visual_gate")
    _trusted_asset(ISSUE_CONTRACT_VALIDATOR, ISSUE_CONTRACT_VALIDATOR_SHA256, "issue_contract_validator")
    result = _run([sys.executable, str(VISUAL_GATE), "--pr", str(args.pr), "--repo", REPO, "--expect-head", args.head, "--json"], env=_clean_env(), timeout=120)
    payload = _decode(result.stdout, "visual_gate")
    if result.returncode != 0 or not isinstance(payload, dict) or payload.get("ok") is not True or payload.get("pr") != args.pr or payload.get("head_sha") != args.head or payload.get("issue_body_sha256") not in {None, issue_body_sha256}:
        raise Refusal("authoritative_visual_proof_missing_or_stale")


def _local_reentry_proof(args: argparse.Namespace) -> None:
    if not (args.source_run_id and args.source_run_attempt and args.causal_log and args.local_log):
        raise Refusal("reentry_proof_required")
    if not RUN_RE.fullmatch(str(args.source_run_id)) or not RUN_RE.fullmatch(str(args.source_run_attempt)):
        raise Refusal("reentry_identity_invalid")
    causal = Path(args.causal_log).resolve()
    local = Path(args.local_log).resolve()
    if not causal.is_file() or not local.is_file() or local == causal:
        raise Refusal("reentry_proof_unavailable")
    causal_text = causal.read_text(encoding="utf-8", errors="replace")
    required = {"PR": str(args.pr), "HEAD": args.head, "SOURCE_RUN_ID": str(args.source_run_id), "SOURCE_RUN_ATTEMPT": str(args.source_run_attempt)}
    if any(not re.search(rf"(?m)^{re.escape(key)}={re.escape(value)}$", causal_text) for key, value in required.items()):
        raise Refusal("causal_identity_mismatch")
    if not re.search(r"(?m)^CAUSAL_CLASS=(PRODUCT_REGRESSION|INFRASTRUCTURE|STRICT_REPLAY_CAPTURE)$", causal_text):
        raise Refusal("causal_class_missing_or_ambiguous")
    local_text = local.read_text(encoding="utf-8", errors="replace")
    for key, value in {"LOCAL_REPRO_PR": str(args.pr), "LOCAL_REPRO_HEAD": args.head, "LOCAL_REPRO_SOURCE_RUN_ID": str(args.source_run_id), "LOCAL_REPRO_SOURCE_RUN_ATTEMPT": str(args.source_run_attempt), "LOCAL_REPRO_RESULT": "PASS", "LOCAL_REPRO_EXIT": "0"}.items():
        if not re.search(rf"(?m)^{re.escape(key)}={re.escape(value)}$", local_text):
            raise Refusal("local_reentry_proof_invalid")


def _source_run(args: argparse.Namespace) -> None:
    value = _gh_json(
        args,
        ["run", "view", str(args.source_run_id), "--repo", REPO, "--json", "databaseId,headSha,event,status,conclusion,workflowName,attempt"],
        "source_run",
    )
    if not isinstance(value, dict):
        raise Refusal("source_run_malformed")
    if (
        str(value.get("databaseId")) != str(args.source_run_id)
        or value.get("headSha") != args.head
        or value.get("event") != "pull_request"
        or value.get("workflowName") not in {"CI", "E2E Smoke Tests"}
        or value.get("status") != "completed"
        or str(value.get("attempt")) != str(args.source_run_attempt)
        or str(value.get("conclusion") or "").lower() not in {"failure", "cancelled", "timed_out", "action_required"}
    ):
        raise Refusal("source_run_not_authoritative")


def _labels(value: dict[str, Any]) -> list[str]:
    labels = value.get("labels")
    if not isinstance(labels, list):
        raise Refusal("labels_malformed")
    names: list[str] = []
    for item in labels:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
            raise Refusal("labels_malformed")
        names.append(item["name"])
    return sorted(set(names))


@contextmanager
def _single_flight(pr: int) -> Iterator[None]:
    lock_path = Path("/tmp") / f"heydonna-cto-label-gated-ci-{pr}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _readback_labels(args: argparse.Namespace) -> list[str]:
    return _labels(_live_pr(args))


def _receipt_path(args: argparse.Namespace) -> Path:
    if not RUN_RE.fullmatch(str(args.pr)) or not HEAD_RE.fullmatch(args.head):
        raise Refusal("effect_identity_invalid")
    return EFFECT_RECEIPT_ROOT / f"{args.pr}-{args.head}.json"


def _load_effect_receipt(args: argparse.Namespace) -> dict[str, Any] | None:
    path = _receipt_path(args)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Refusal("effect_receipt_malformed") from exc
    if (
        not isinstance(value, dict)
        or value.get("repository") != REPO
        or value.get("pr") != args.pr
        or value.get("issue") != args.issue
        or value.get("head") != args.head
        or value.get("state") not in {"effect_started", "unresolved", "completed"}
    ):
        raise Refusal("effect_receipt_identity_mismatch")
    return value


def _save_effect_receipt(args: argparse.Namespace, state: str, **extra: Any) -> None:
    path = _receipt_path(args)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {"repository": REPO, "pr": args.pr, "issue": args.issue, "head": args.head, "state": state, **extra}
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(_json(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _effect(args: argparse.Namespace, before: list[str], expected_body_sha256: str) -> dict[str, Any]:
    with _single_flight(args.pr):
        current_body = _issue_body(args)
        if hashlib.sha256(current_body.encode("utf-8")).hexdigest() != expected_body_sha256:
            raise Refusal("issue_body_changed_before_effect")
        receipt = _load_effect_receipt(args)
        if receipt is not None:
            if receipt["state"] == "completed":
                return {"status": "ALREADY_ADMITTED", "effect": "none", "partial": bool(receipt.get("partial"))}
            observed = _readback_labels(args)
            raise Refusal(
                "effect_unresolved_observed_target" if f"ci-head:{args.head}" in observed
                else "effect_unresolved_no_conclusive_reconciliation"
            )
        current = _labels(_live_pr(args))
        if current != before:
            raise Refusal("labels_changed_before_effect")
        target = f"ci-head:{args.head}"
        stale = [label for label in current if label.startswith("ci-head:") and label != target]
        if target in current:
            return {"status": "ALREADY_ADMITTED", "effect": "none", "partial": bool(stale)}
        if len(stale) > 1:
            raise Refusal("concurrent_wave_detected")
        additions = ["pm-state:qa-passed-awaiting-ci", target]
        _save_effect_receipt(args, "effect_started", before=before, issue_body_sha256=expected_body_sha256)
        post = _run([args.gh, "api", "--method", "POST", f"repos/{REPO}/issues/{args.pr}/labels", "--input", "-"], input_text=_json({"labels": additions}) + "\n")
        after_post = _readback_labels(args)
        if target not in after_post:
            _save_effect_receipt(args, "unresolved", observation="target_absent_after_post")
            raise Refusal("label_trigger_no_effect_no_retry")
        response_lost = post.returncode != 0
        if response_lost:
            _save_effect_receipt(args, "unresolved", observation="target_present_response_lost")
            raise Refusal("label_trigger_response_lost_unresolved")
        for label in stale:
            delete = _run([args.gh, "api", "--method", "DELETE", f"repos/{REPO}/issues/{args.pr}/labels/{label}"])
            after_delete = _readback_labels(args)
            if label in after_delete:
                _save_effect_receipt(args, "unresolved", observation="stale_label_delete_unconfirmed")
                raise Refusal("label_transition_partial_uncertain")
            if delete.returncode != 0:
                _save_effect_receipt(args, "unresolved", observation="stale_label_delete_response_lost")
                raise Refusal("label_transition_applied_response_lost")
        final = _readback_labels(args)
        if target not in final:
            _save_effect_receipt(args, "unresolved", observation="target_missing_final")
            raise Refusal("label_transition_uncertain")
        _save_effect_receipt(args, "completed", partial=False)
        return {"status": "ADMITTED", "effect": "one_ci_trigger_label_transition", "partial": False}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--issue", required=True, type=int)
    parser.add_argument("--head", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--checkout", required=True)
    parser.add_argument("--causal-log")
    parser.add_argument("--local-log")
    parser.add_argument("--source-run-id")
    parser.add_argument("--source-run-attempt")
    parser.add_argument("--gh", default=os.environ.get("GH_BIN", "gh"))
    args = parser.parse_args()
    if not HEAD_RE.fullmatch(args.head):
        raise Refusal("head_invalid")
    reentry = bool(args.source_run_id or args.source_run_attempt or args.causal_log or args.local_log)
    _checkout_head(args)
    pr = _live_pr(args)
    body = _issue_body(args)
    body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
    _visual_proof(args, body_sha256)
    _gate(args, reentry=reentry)
    if reentry:
        _source_run(args)
        _local_reentry_proof(args)
    result = _effect(args, _labels(pr), body_sha256)
    print(_json({"status": result["status"], "pr": args.pr, "issue": args.issue, "head": args.head, "effect": result["effect"], "issue_body_sha256": body_sha256}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, Refusal, subprocess.TimeoutExpired) as exc:
        print(_json({"status": "REFUSED", "reason": str(exc)}), file=sys.stderr)
        raise SystemExit(13)
