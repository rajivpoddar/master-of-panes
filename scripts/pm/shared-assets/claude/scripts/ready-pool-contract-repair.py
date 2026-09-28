#!/usr/bin/env python3
"""Repair backlog Ready Pool contracts through audited, reviewed promotion."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any


REPO = os.environ.get("GH_REPO", "heydonna-app/heydonna-app")
REPO_ROOT = Path(os.environ.get("HEYDONNA_REPO_ROOT", "/Users/rajiv/Downloads/projects/heydonna-app"))
BACKLOG_TRIAGE = Path(os.environ.get("BACKLOG_TRIAGE", "/Users/rajiv/.claude/scripts/backlog-triage.py"))
PM_OPS = Path(os.environ.get("PM_OPS", "/Users/rajiv/.claude/scripts/pm-ops.py"))
PM_MESSAGE = Path(
    os.environ.get("PM_MESSAGE_SCRIPT", "/Users/rajiv/.claude/scripts/message-pm.sh")
)
CODEX_COMPANION = Path(
    os.environ.get(
        "CODEX_REVIEW_COMPANION",
        "/Users/rajiv/.claude/skills/codex-review-companion/codex-review-companion.mjs",
    )
)
GH = shutil.which("gh") or "/opt/homebrew/bin/gh"
CLAUDE = shutil.which("claude") or "claude"
NODE = shutil.which("node") or "node"
OUT_DIR = Path(os.environ.get("READY_POOL_REPAIR_OUT_DIR", "/tmp"))
ACTIVE_STATE = Path(os.environ.get("READY_POOL_REPAIR_ACTIVE_FILE", "/tmp/pm-ready-pool-contract-repair-active.json"))
LOCK_FILE = Path(os.environ.get("READY_POOL_REPAIR_LOCK_FILE", "/tmp/pm-ready-pool-contract-repair.lock"))
DEBUG_LOG = Path(os.environ.get("READY_POOL_REPAIR_DEBUG_LOG", "/tmp/pm-ready-pool-contract-repair.log"))
REQUIRED_FIELDS = ("priority", "lane", "ac_summary", "claimable_slot_type", "blockers", "work_type")
APPROVE_VERDICTS = {"CONFIRMED", "APPROVE", "VERIFIED"}
ADMISSION_DISPOSITIONS = {
    "ADMIT",
    "REPAIR_CONTRACT",
    "PARK_TRACKING",
    "PARK_DEPENDENCY",
    "DUPLICATE_OR_SUPERSEDED",
    "NEEDS_INVESTIGATION",
    "NEEDS_CTO",
}
DECISION_KINDS = {"cto_review", "product_decision", "directive_clarification"}
TERMINAL_OUTCOMES = {"REPAIRABLE", "PARKED", "NEEDS_CTO", "EXTERNAL_ACTION"}
TERMINAL_STATUSES = {"PROMOTED", "PARKED", "NEEDS_CTO", "EXTERNAL_ACTION", "INVESTIGATION_REQUIRED"}
REPAIR_BUFFER = 3


class RepairError(RuntimeError):
    pass


def now_id() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def read_json(path: Path) -> dict[str, Any]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise RepairError(f"JSON object required: {path}")
    return decoded


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def debug_log(event: str, **fields: Any) -> None:
    """Append best-effort structured diagnostics without changing control flow."""
    payload = {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "pid": os.getpid(),
        "event": event,
        **fields,
    }
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
    except OSError:
        pass


class RepairLease:
    """Fresh, PID-pinned proof that the long repair coordinator is progressing."""

    def __init__(self, *, run_id: str, source: Path, candidates: int, current: int, deficit: int) -> None:
        now = time.time()
        self.state: dict[str, Any] = {
            "schema": "pm-ready-pool-contract-repair-active/v1",
            "status": "running",
            "run_id": run_id,
            "pid": os.getpid(),
            "source_manifest": str(source),
            "started_at_epoch": now,
            "heartbeat_at_epoch": now,
            "candidate_count": candidates,
            "processed_count": 0,
            "promoted_count": 0,
            "parked_count": 0,
            "needs_cto_count": 0,
            "error_count": 0,
            "current_ready_pool_count": current,
            "promotion_deficit": deficit,
            "target": 10,
        }
        self.state_lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.lock_handle: Any = None

    def acquire(self) -> bool:
        """Own the refill coordinator exclusively; stale processes release this automatically."""
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        handle = LOCK_FILE.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} run_id={self.state['run_id']}\n")
        handle.flush()
        self.lock_handle = handle
        return True

    def release(self) -> None:
        if self.lock_handle is None:
            return
        fcntl.flock(self.lock_handle.fileno(), fcntl.LOCK_UN)
        self.lock_handle.close()
        self.lock_handle = None

    def _write(self) -> None:
        ACTIVE_STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = ACTIVE_STATE.with_name(f".{ACTIVE_STATE.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self.state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, ACTIVE_STATE)

    def _heartbeat(self) -> None:
        while not self.stop.wait(30):
            with self.state_lock:
                self.state["heartbeat_at_epoch"] = time.time()
                self._write()

    def start(self) -> None:
        with self.state_lock:
            self._write()
        self.thread.start()

    def update(self, **fields: Any) -> None:
        with self.state_lock:
            self.state.update(fields)
            self.state["heartbeat_at_epoch"] = time.time()
            self._write()

    def finish(self, *, status: str, aggregate: Path) -> None:
        self.stop.set()
        self.thread.join(timeout=2)
        with self.state_lock:
            self.state.update({
                "status": status,
                "aggregate_manifest": str(aggregate),
                "finished_at_epoch": time.time(),
                "heartbeat_at_epoch": time.time(),
            })
            self._write()
        self.release()


def run(cmd: list[str], *, timeout: int = 1200) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True, timeout=timeout)


def notify_pm(message_path: Path, *, issue: int | None, timeout: int) -> str:
    """Deliver a complete operational notice through the supported PM-pane sender."""
    context = f"issue={issue}" if issue is not None else "issue=none"
    try:
        message = message_path.read_bytes()
    except OSError as exc:
        raise RepairError(
            f"ready_pool_notification_failed {context} message_file={message_path} "
            f"reason=message_file_{type(exc).__name__}"
        ) from exc
    if not message:
        raise RepairError(
            f"ready_pool_notification_failed {context} message_file={message_path} reason=empty_message"
        )
    try:
        proc = run([str(PM_MESSAGE), "--file", str(message_path)], timeout=min(max(timeout, 1), 30))
    except (OSError, subprocess.SubprocessError) as exc:
        raise RepairError(
            f"ready_pool_notification_failed {context} message_file={message_path} "
            f"reason=transport_{type(exc).__name__}"
        ) from exc
    match = re.search(r"(?m)^MESSAGE_PM_SENT slot=0 verified=true bytes=(\d+)\s*$", proc.stdout)
    expected_bytes = len(message)
    if proc.returncode != 0 or not match or int(match.group(1)) != expected_bytes:
        detail = (proc.stderr.strip() or proc.stdout.strip() or f"exit={proc.returncode}")[-300:]
        raise RepairError(
            f"ready_pool_notification_failed {context} message_file={message_path} "
            f"expected_bytes={expected_bytes} transport_detail={detail}"
        )
    return f"slot=0 verified=true bytes={expected_bytes}"


def output_path(stdout: str, key: str = "manifest") -> Path:
    match = re.search(rf"(?m)^{re.escape(key)}=(.+)$", stdout)
    if not match:
        raise RepairError(f"missing {key}=... in command output")
    path = Path(match.group(1).strip())
    if not path.is_file():
        raise RepairError(f"reported {key} does not exist: {path}")
    return path


def source_manifest(path: Path) -> tuple[Path, dict[str, Any]]:
    data = read_json(path)
    if data.get("review_result"):
        source = Path(str(data.get("dry_run_manifest") or ""))
        if not source.is_file():
            raise RepairError("review-result source manifest is missing")
        path, data = source, read_json(source)
    if data.get("repo") != REPO or data.get("apply"):
        raise RepairError("source must be a HeyDonna promoter dry-run manifest")
    return path.resolve(), data


def candidate_rows(data: dict[str, Any], issue_filter: set[int] | None) -> list[dict[str, Any]]:
    rows = data.get("needs_ready_pool_metadata")
    if not isinstance(rows, list):
        skipped = data.get("skipped") if isinstance(data.get("skipped"), dict) else {}
        rows = skipped.get("missing_ready_pool_contract")
    result: list[dict[str, Any]] = []
    skipped = data.get("skipped") if isinstance(data.get("skipped"), dict) else {}
    extra_rows: list[Any] = []
    for key in ("priority_label_drift", "handoff_preflight_failed", "status_hygiene"):
        value = skipped.get(key)
        if isinstance(value, list):
            extra_rows.extend(value)
    seen: set[int] = set()
    combined = list(rows if isinstance(rows, list) else []) + extra_rows
    for row in combined:
        if not isinstance(row, dict):
            continue
        try:
            issue = int(row.get("number"))
        except Exception:
            continue
        if issue in seen or (issue_filter and issue not in issue_filter):
            continue
        seen.add(issue)
        result.append(row)
    return result


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def body_hash_matches(body: str, expected: str) -> bool:
    """Accept GitHub/CLI terminal-newline serialization without weakening the body pin."""
    canonical = body.replace("\r\n", "\n").rstrip("\n")
    variants = {
        canonical,
        canonical + "\n",
        canonical.replace("\n", "\r\n") + "\r\n",
    }
    return expected in {sha256_text(value) for value in variants}


def live_issue_body(issue: int) -> str:
    proc = run([GH, "issue", "view", str(issue), "--repo", REPO, "--json", "body", "--jq", ".body"])
    if proc.returncode != 0:
        raise RepairError(f"gh issue view #{issue} failed: {proc.stderr.strip()}")
    return proc.stdout.rstrip("\n")


def strip_ready_pool(body: str) -> str:
    return re.sub(r"(?is)\n*<!--\s*ready-pool:.*?-->\s*", "\n", body or "").strip()


def parse_ready_pool(body: str) -> dict[str, str]:
    blocks = re.findall(r"(?is)<!--\s*ready-pool:(.*?)-->", body or "")
    if len(blocks) != 1:
        raise RepairError(f"proposed body must contain exactly one Ready Pool block; found {len(blocks)}")
    metadata: dict[str, str] = {}
    for raw in blocks[0].splitlines():
        match = re.match(r"\s*([a-zA-Z_][\w-]*)\s*:\s*(.*?)\s*$", raw)
        if match:
            metadata[match.group(1).lower().replace("-", "_")] = match.group(2).strip().strip("`\"'")
    return metadata


def validate_audit_result(result_path: Path, *, issue: int, source: Path) -> dict[str, Any]:
    result = read_json(result_path)
    if result.get("schema") != "pm-ready-pool-contract-repair/v1":
        raise RepairError("invalid auditor schema")
    if int(result.get("issue") or 0) != issue:
        raise RepairError("auditor issue mismatch")
    if Path(str(result.get("source_manifest") or "")).resolve() != source:
        raise RepairError("auditor source manifest mismatch")
    outcome = str(result.get("outcome") or "")
    if outcome == "NEEDS_RAJIV":
        debug_log("legacy_outcome_normalized", issue=issue, result_file=str(result_path), outcome=outcome)
        outcome = "NEEDS_CTO"
        result["outcome"] = outcome
    if outcome not in TERMINAL_OUTCOMES:
        raise RepairError(f"invalid auditor outcome: {outcome or 'missing'}")
    expected = str(result.get("expected_body_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise RepairError("auditor did not pin a valid body SHA-256")
    current = live_issue_body(issue)
    if not body_hash_matches(current, expected):
        raise RepairError("stale_issue_body")
    if outcome == "NEEDS_CTO":
        decision_kind = str(result.get("decision_kind") or "").strip()
        if decision_kind not in DECISION_KINDS:
            raise RepairError("NEEDS_CTO result has invalid decision_kind")
        if not str(result.get("decision_question") or "").strip():
            raise RepairError("NEEDS_CTO result is missing decision_question")
        if not str(result.get("recommended_default") or "").strip():
            raise RepairError("NEEDS_CTO result is missing recommended_default")
        return result
    if outcome == "EXTERNAL_ACTION":
        if not str(result.get("external_owner") or "").strip():
            raise RepairError("EXTERNAL_ACTION result is missing external_owner")
        if not str(result.get("external_action") or "").strip():
            raise RepairError("EXTERNAL_ACTION result is missing external_action")
        if not str(result.get("unblock_condition") or "").strip():
            raise RepairError("EXTERNAL_ACTION result is missing unblock_condition")
        return result
    if outcome != "REPAIRABLE":
        return result

    ready_pool = result.get("ready_pool")
    if not isinstance(ready_pool, dict):
        raise RepairError("REPAIRABLE result is missing ready_pool metadata")
    missing = [field for field in REQUIRED_FIELDS if not str(ready_pool.get(field) or "").strip()]
    if missing:
        raise RepairError("missing Ready Pool fields: " + ",".join(missing))
    if str(ready_pool.get("priority") or "").upper() not in {"P0", "P1", "P2", "P3"}:
        raise RepairError("invalid canonical priority")
    if len(str(ready_pool.get("ac_summary") or "")) > 140:
        raise RepairError("ac_summary exceeds 140 characters")
    if str(ready_pool.get("blockers") or "").strip().lower() not in {"none", "null", "[]"}:
        raise RepairError("REPAIRABLE result contains a blocker")

    proposal_path = Path(str(result.get("proposed_body_file") or ""))
    if not proposal_path.is_file():
        raise RepairError("REPAIRABLE result did not write proposed_body_file")
    proposal = proposal_path.read_text(encoding="utf-8")
    if strip_ready_pool(proposal) != strip_ready_pool(current):
        raise RepairError("proposal changed issue text outside the canonical Ready Pool block")
    parsed = parse_ready_pool(proposal)
    for field in REQUIRED_FIELDS:
        if str(parsed.get(field) or "") != str(ready_pool.get(field) or ""):
            raise RepairError(f"proposal/result Ready Pool mismatch: {field}")
    result["proposed_body_file"] = str(proposal_path.resolve())
    return result


def audit_one(issue: int, source: Path, run_id: str, timeout: int) -> tuple[Path, str]:
    output = OUT_DIR / f"pm-ready-pool-contract-{run_id}-{issue}.json"
    output.unlink(missing_ok=True)
    prompt = f"mode=ready-pool-repair issue={issue} source_manifest={source} output={output}"
    cmd = [
        CLAUDE, "-p", "--agent", "issue-contract-auditor", "--output-format", "json",
        "--no-session-persistence", "--allow-dangerously-skip-permissions",
        "--permission-mode", "bypassPermissions", prompt,
    ]
    proc = subprocess.Popen(cmd, cwd=REPO_ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if output.is_file():
            # The JSON artifact is the terminal contract. Do not wait for an
            # agent that keeps narrating after it has delivered that contract.
            proc.terminate()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
            return output, ""
        returncode = proc.poll()
        if returncode is not None:
            stdout, stderr = proc.communicate()
            if returncode != 0:
                return output, f"auditor_failed:{returncode}:{stderr[-500:].strip()}"
            return output, "auditor_result_missing"
        time.sleep(0.25)
    proc.terminate()
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
    return output, "auditor_timeout"


def arch_review(issue: int, result: dict[str, Any], timeout: int) -> tuple[str, str, str, str, str]:
    focus = (
        "Ready Pool contract admission review. Read the full proposed issue body at "
        f"{result['proposed_body_file']}. Compare it with live issue #{issue} and cited evidence. Confirm only if "
        "priority, production-shaped AC, lane, runtime control point, claimability, and no-blocker claim are grounded; "
        "reject invented metadata, trackers, dependencies, duplicates, or broad work. Return exact "
        "ADMISSION_DISPOSITION and ADMISSION_REASON fields from the Ready Pool admission contract. "
        f"Body pin: {result['expected_body_sha256']}."
    )
    base = [
        NODE, str(CODEX_COMPANION), "--review-type", "arch", "--branch", "main",
        "--issue", str(issue), "--focus-text", focus, "--effort", "max", "--output-format", "json",
    ]
    last_error = ""
    for model in ("gpt-5.6-luna", "gpt-5.5"):
        try:
            proc = run(base + ["--model", model], timeout=timeout)
        except subprocess.TimeoutExpired:
            last_error = f"arch_review_timeout:{model}"
            continue
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            payload = {}
        verdict = str(payload.get("verdict") or "UNKNOWN").upper()
        disposition = str(payload.get("admission_disposition") or "").upper()
        admission_reason = str(payload.get("admission_reason") or "").strip()
        marker = str(payload.get("marker_file") or "")
        if verdict != "UNKNOWN":
            if disposition not in ADMISSION_DISPOSITIONS:
                # Legacy/malformed output fails closed to a PM-owned evidence
                # check. Never infer a human decision from MISDIAGNOSED.
                disposition = "NEEDS_INVESTIGATION"
                admission_reason = admission_reason or "Architecture review omitted the required Ready Pool admission disposition."
            return verdict, disposition, admission_reason, marker, model
        last_error = f"arch_review_failed:{model}:rc={proc.returncode}:{proc.stderr[-500:].strip()}"
    return "ERROR", "NEEDS_INVESTIGATION", last_error, "", ""


def apply_proposed_body(issue: int, result: dict[str, Any]) -> None:
    if not body_hash_matches(live_issue_body(issue), result["expected_body_sha256"]):
        raise RepairError("stale_issue_body_before_apply")
    proc = run([GH, "issue", "edit", str(issue), "--repo", REPO, "--body-file", result["proposed_body_file"]], timeout=120)
    if proc.returncode != 0:
        raise RepairError(f"gh issue edit failed: {proc.stderr.strip()}")
    proposal = Path(result["proposed_body_file"]).read_text(encoding="utf-8").rstrip("\n")
    if live_issue_body(issue) != proposal:
        raise RepairError("live body verification failed after edit")


def promote_exact(issue: int, marker: str, timeout: int) -> Path:
    dry = run(
        [str(BACKLOG_TRIAGE), "promote", "--dry-run", "--force-recheck", "--issues", str(issue)],
        timeout=timeout,
    )
    if dry.returncode != 0:
        raise RepairError(f"targeted promoter dry-run failed: {dry.stderr.strip()}")
    dry_path = output_path(dry.stdout)
    promotable = {
        int(row.get("number")) for row in (read_json(dry_path).get("would_promote") or [])
        if isinstance(row, dict) and row.get("number")
    }
    if issue not in promotable:
        raise RepairError("repaired issue is still not a clean promotion candidate")
    review = run(
        [str(BACKLOG_TRIAGE), "promote", "--review-result", "--dry-run-manifest", str(dry_path),
         "--accepted", str(issue), "--notes", f"Codex arch approval: {marker or 'structured-confirmed-verdict'}"],
        timeout=timeout,
    )
    if review.returncode != 0:
        raise RepairError(f"promotion review-result failed: {review.stderr.strip()}")
    review_path = output_path(review.stdout)
    applied = run(
        [str(BACKLOG_TRIAGE), "promote", "--apply", "--defer-reconcile",
         "--review-result-manifest", str(review_path), "--issues", str(issue)],
        timeout=timeout,
    )
    if applied.returncode != 0:
        raise RepairError(f"promotion apply failed: {applied.stderr.strip()}")
    applied_path = output_path(applied.stdout)
    promoted = read_json(applied_path).get("promoted") or []
    if not any(isinstance(row, dict) and row.get("ok") and int(row.get("issue") or 0) == issue for row in promoted):
        raise RepairError("promotion manifest did not prove the issue was promoted")
    return applied_path


def live_deficit(timeout: int) -> tuple[int, int, Path]:
    proc = run([str(BACKLOG_TRIAGE), "promote", "--dry-run"], timeout=timeout)
    if proc.returncode != 0:
        raise RepairError(f"promoter refresh failed: {proc.stderr.strip()}")
    path = output_path(proc.stdout)
    data = read_json(path)
    return int(data.get("current_ready_pool_count") or 0), int(data.get("promotion_deficit") or 0), path


def live_ready_pool_count(timeout: int) -> int:
    """Count the dispatchable queue without rebuilding the full backlog manifest."""
    proc = run(
        [
            GH,
            "issue",
            "list",
            "--repo",
            REPO,
            "--state",
            "open",
            "--label",
            "status:todo",
            "--limit",
            "100",
            "--json",
            "number",
        ],
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RepairError(f"ready-pool count failed: {proc.stderr.strip()}")
    decoded = json.loads(proc.stdout)
    if not isinstance(decoded, list):
        raise RepairError("ready-pool count did not return a JSON list")
    return len(decoded)


def record_rejections(source: Path, rows: list[dict[str, Any]], timeout: int) -> str:
    if not rows:
        return ""
    enriched: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        issue = int(item.get("issue") or 0)
        if issue:
            proc = run(
                [GH, "issue", "view", str(issue), "--repo", REPO, "--json", "body,labels"],
                timeout=120,
            )
            if proc.returncode == 0:
                snapshot = json.loads(proc.stdout)
                body = str(snapshot.get("body") or "")
                names = sorted(
                    str(label.get("name") or "")
                    for label in snapshot.get("labels") or []
                    if str(label.get("name") or "")
                )
                item["body_sha256"] = sha256_text(body)
                relevant = [
                    name for name in names
                    if name.startswith(("status:", "pm-blocked:", "pm-cleanup:"))
                    or name in {"P0", "P1", "P2", "P3", "epic", "tracking", "pm-nonclaimable:tracking"}
                    or name.startswith("priority:")
                    or name == "high-priority"
                ]
                item["labels_fingerprint"] = sha256_text("\n".join(relevant))
        item.setdefault("dependency_fingerprint", sha256_text(",".join(str(dep) for dep in sorted(item.get("open_dependencies") or []))))
        item.setdefault("rejection_class", str(item.get("reason") or "ARCH_REJECTED").upper())
        enriched.append(item)
    path = OUT_DIR / f"pm-backlog-promoter-rejections-{now_id()}.json"
    path.write_text(json.dumps(enriched, indent=2) + "\n", encoding="utf-8")
    proc = run(
        [str(BACKLOG_TRIAGE), "promote", "--review-result", "--dry-run-manifest", str(source),
         "--rejected-json", str(path), "--notes", "Contract-repair terminal outcomes; retry only after issue change."],
        timeout=timeout,
    )
    return str(output_path(proc.stdout)) if proc.returncode == 0 else ""


def escalate_external_action(
    issue: int,
    result_path: Path,
    result: dict[str, Any],
    timeout: int,
) -> tuple[str, int]:
    """Persist an operational unblock without misclassifying it as a CTO decision."""
    owner = str(result["external_owner"]).strip()
    action = str(result["external_action"]).strip()
    unblock = str(result["unblock_condition"]).strip()
    action_hash = sha256_text(f"{owner}\n{action}\n{unblock}")[:16]
    ensure_label = run(
        [GH, "label", "create", "pm-blocked:external", "--repo", REPO,
         "--color", "B60205", "--description", "External operational prerequisite required", "--force"],
        timeout=120,
    )
    if ensure_label.returncode != 0:
        raise RepairError(f"failed to ensure external-action label: {ensure_label.stderr.strip()}")
    labelled = run(
        [GH, "issue", "edit", str(issue), "--repo", REPO, "--add-label", "pm-blocked:external"],
        timeout=120,
    )
    if labelled.returncode != 0:
        raise RepairError(f"failed to set external-action label: {labelled.stderr.strip()}")
    obligation = run(
        [
            str(PM_OPS), "obligation-upsert",
            "--kind", "ready_pool_external_action",
            "--severity", "action",
            "--target-type", "issue", "--target-id", str(issue), "--issue", str(issue),
            "--owner", "pm",
            "--title", f"Issue #{issue}: external action required from {owner}",
            "--action", f"{action} Unblocked when: {unblock}",
            "--blocker", "pm-blocked:external",
            "--horizon", "hourly",
            "--dedupe-group", f"ready-pool-external-action:{issue}:{action_hash}",
            "--evidence-json", json.dumps({"result_file": str(result_path), "owner": owner, "action": action, "unblock_condition": unblock}, sort_keys=True),
            "--print-id",
        ],
        timeout=timeout,
    )
    if obligation.returncode != 0 or not obligation.stdout.strip().isdigit():
        raise RepairError("failed to persist external-action obligation")
    obligation_id = int(obligation.stdout.strip())
    message_path = OUT_DIR / f"pm-ready-pool-external-{issue}-{action_hash}.txt"
    message_path.write_text(
        f"*Issue #{issue} -> external action required*\nOwner: {owner}\nAction: {action}\n"
        f"Unblocked when: {unblock}\nObligation: `{obligation_id}`",
        encoding="utf-8",
    )
    try:
        receipt = notify_pm(message_path, issue=issue, timeout=timeout)
    except RepairError as exc:
        raise RepairError(f"{exc}; durable_obligation_id={obligation_id}") from exc
    return receipt, obligation_id


def escalate_cto_decision(
    issue: int,
    result_path: Path,
    result: dict[str, Any],
    timeout: int,
) -> tuple[str, int]:
    """Create one durable CTO-decision queue entry and its transition-thread alert."""
    decision_kind = str(result["decision_kind"]).strip()
    question = str(result["decision_question"]).strip()
    recommended_default = str(result["recommended_default"]).strip()
    decision_hash = sha256_text(f"{decision_kind}\n{question}\n{recommended_default}")[:16]
    debug_log(
        "cto_escalation_started",
        issue=issue,
        decision_kind=decision_kind,
        decision_hash=decision_hash,
        result_file=str(result_path),
    )

    labels = run(
        [GH, "issue", "view", str(issue), "--repo", REPO, "--json", "labels", "--jq", ".labels[].name"],
        timeout=120,
    )
    if labels.returncode != 0:
        raise RepairError(f"failed to read issue labels before escalation: {labels.stderr.strip()}")
    live_labels = set(labels.stdout.splitlines())
    label_cmd = [GH, "issue", "edit", str(issue), "--repo", REPO, "--add-label", "pm-blocked:cto"]
    if "pm-blocked:product" in live_labels:
        label_cmd.extend(["--remove-label", "pm-blocked:product"])
    labelled = run(label_cmd, timeout=120)
    if labelled.returncode != 0:
        debug_log("cto_escalation_label_failed", issue=issue, stderr=labelled.stderr[-500:].strip())
        raise RepairError(f"failed to set canonical CTO-decision label: {labelled.stderr.strip()}")
    debug_log(
        "cto_escalation_label_applied",
        issue=issue,
        added="pm-blocked:cto",
        removed="pm-blocked:product" if "pm-blocked:product" in live_labels else "",
    )

    evidence = {
        "decision_kind": decision_kind,
        "decision_question": question,
        "recommended_default": recommended_default,
        "result_file": str(result_path),
        "auditor_evidence": result.get("evidence") or [],
    }
    obligation = run(
        [
            str(PM_OPS),
            "obligation-upsert",
            "--kind", "ready_pool_cto_decision",
            "--severity", "action",
            "--target-type", "issue",
            "--target-id", str(issue),
            "--issue", str(issue),
            "--owner", "pm",
            "--title", f"Issue #{issue}: CTO decision required ({decision_kind})",
            "--action", f"Escalate to CTO: {question} Recommended default: {recommended_default}",
            "--blocker", "pm-blocked:cto",
            "--horizon", "hourly",
            "--dedupe-group", f"ready-pool-cto-decision:{issue}:{decision_hash}",
            "--evidence-json", json.dumps(evidence, sort_keys=True),
            "--print-id",
        ],
        timeout=timeout,
    )
    if obligation.returncode != 0 or not obligation.stdout.strip().isdigit():
        debug_log(
            "cto_escalation_obligation_failed",
            issue=issue,
            stdout=obligation.stdout[-500:].strip(),
            stderr=obligation.stderr[-500:].strip(),
        )
        raise RepairError(
            "failed to persist CTO-decision obligation: "
            + (obligation.stderr.strip() or obligation.stdout.strip() or "missing obligation id")
        )
    obligation_id = int(obligation.stdout.strip())
    debug_log("cto_escalation_obligation_created", issue=issue, obligation_id=obligation_id)

    message_path = OUT_DIR / f"pm-ready-pool-decision-{issue}-{decision_hash}.txt"
    cited = ", ".join(str(item) for item in (result.get("evidence") or [])) or str(result_path)
    message_path.write_text(
        f"*Issue #{issue} -> CTO decision required*\n"
        f"Decision kind: `{decision_kind}`\n"
        f"Question: {question}\n"
        f"PM recommended default: {recommended_default}\n"
        f"Evidence: {cited}\n"
        f"Obligation: `{obligation_id}`\n\n"
        "The issue remains `status:backlog` with `pm-blocked:cto` until the CTO decision is recorded.",
        encoding="utf-8",
    )
    try:
        receipt = notify_pm(message_path, issue=issue, timeout=timeout)
    except RepairError as exc:
        debug_log("cto_escalation_notification_failed", issue=issue, obligation_id=obligation_id, error=str(exc))
        raise RepairError(f"{exc}; durable_obligation_id={obligation_id}") from exc
    debug_log("cto_escalation_complete", issue=issue, obligation_id=obligation_id, notification_receipt=receipt)
    return receipt, obligation_id


def escalate_investigation(
    issue: int,
    result_path: Path,
    detail: str,
    timeout: int,
) -> tuple[str, int]:
    """Persist a PM-owned investigation instead of rerunning the same architecture review."""
    investigation_hash = sha256_text(detail)[:16]
    obligation = run(
        [
            str(PM_OPS), "obligation-upsert",
            "--kind", "ready_pool_investigation",
            "--severity", "action",
            "--target-type", "issue", "--target-id", str(issue), "--issue", str(issue),
            "--owner", "pm",
            "--title", f"Issue #{issue}: Ready Pool investigation required",
            "--action", f"Resolve the architecture review uncertainty before reconsidering promotion: {detail}",
            "--blocker", "architecture-review-needs-investigation",
            "--horizon", "hourly",
            "--dedupe-group", f"ready-pool-investigation:{issue}:{investigation_hash}",
            "--evidence-json", json.dumps({"result_file": str(result_path), "detail": detail}, sort_keys=True),
            "--print-id",
        ],
        timeout=timeout,
    )
    if obligation.returncode != 0 or not obligation.stdout.strip().isdigit():
        raise RepairError("failed to persist Ready Pool investigation obligation")
    obligation_id = int(obligation.stdout.strip())
    message_path = OUT_DIR / f"pm-ready-pool-investigation-{issue}-{investigation_hash}.txt"
    message_path.write_text(
        f"*Issue #{issue} -> Ready Pool investigation required*\n"
        f"Architecture review: `NEEDS_DEEPER_INVESTIGATION`\n"
        f"Required PM action: resolve this uncertainty, update the issue/evidence, then explicitly recheck.\n"
        f"Detail: {detail}\nObligation: `{obligation_id}`",
        encoding="utf-8",
    )
    try:
        receipt = notify_pm(message_path, issue=issue, timeout=timeout)
    except RepairError as exc:
        raise RepairError(f"{exc}; durable_obligation_id={obligation_id}") from exc
    return receipt, obligation_id


def escalate_parked_candidate(
    issue: int,
    result_path: Path,
    classification: str,
    detail: str,
    timeout: int,
) -> str:
    """Make every reviewed non-dispatchable candidate visible to PM."""
    state_hash = sha256_text(f"{classification}\n{detail}")[:16]
    message_path = OUT_DIR / f"pm-ready-pool-parked-{issue}-{state_hash}.txt"
    message_path.write_text(
        f"*Issue #{issue} was not admitted to the Ready Pool*\n"
        f"Classification: `{classification}`\n"
        f"Reason/unblock condition: {detail}\n"
        "The issue remains `status:backlog`; the promoter will reconsider it after the cited evidence changes.",
        encoding="utf-8",
    )
    return notify_pm(message_path, issue=issue, timeout=timeout)


def route_arch_failure(
    issue: int,
    row: dict[str, Any],
    result_path: Path,
    verdict: str,
    disposition: str,
    admission_reason: str,
    marker: str,
    model: str,
    timeout: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Turn non-approval into one durable route, not another generic review cycle."""
    detail = admission_reason or marker or verdict
    common = {
        "issue": issue,
        "result_file": str(result_path),
        "reason": f"codex_arch_{disposition}",
        "review_verdict": verdict,
        "admission_disposition": disposition,
        "admission_reason": detail,
        "review_marker": marker,
        "review_model": model,
    }
    if disposition == "NEEDS_CTO":
        decision = {
            "decision_kind": "directive_clarification",
            "decision_question": f"What product/directive decision should govern Ready Pool admission for issue #{issue}? {detail}",
            "recommended_default": "Keep the issue in backlog until the CTO decision is recorded in its contract.",
            "evidence": [detail],
        }
        notification_receipt, obligation_id = escalate_cto_decision(issue, result_path, decision, timeout)
        terminal = {**common, "status": "NEEDS_CTO", "notification_receipt": notification_receipt, "obligation_id": obligation_id}
        return terminal, rejection_row(issue, row, "NEEDS_CTO", detail)
    if disposition == "NEEDS_INVESTIGATION":
        notification_receipt, obligation_id = escalate_investigation(issue, result_path, detail, timeout)
        terminal = {**common, "status": "INVESTIGATION_REQUIRED", "notification_receipt": notification_receipt, "obligation_id": obligation_id}
        return terminal, rejection_row(issue, row, "NEEDS_INVESTIGATION", detail)
    if disposition == "PARK_TRACKING":
        ensure_tracking_label(issue, timeout)
        notification_receipt = escalate_parked_candidate(issue, result_path, "TRACKING", detail, timeout)
        return {**common, "status": "PARKED", "notification_receipt": notification_receipt}, rejection_row(issue, row, "TRACKING", detail)
    if disposition == "PARK_DEPENDENCY":
        notification_receipt = escalate_parked_candidate(issue, result_path, "DEPENDENCY_WAIT", detail, timeout)
        return {**common, "status": "PARKED", "notification_receipt": notification_receipt}, rejection_row(issue, row, "DEPENDENCY_WAIT", detail)
    if disposition == "DUPLICATE_OR_SUPERSEDED":
        notification_receipt = escalate_parked_candidate(issue, result_path, "DUPLICATE_SUPERSEDED", detail, timeout)
        return {**common, "status": "PARKED", "notification_receipt": notification_receipt}, rejection_row(issue, row, "DUPLICATE_SUPERSEDED", detail)
    if disposition == "REPAIR_CONTRACT":
        notification_receipt = escalate_parked_candidate(issue, result_path, "REPAIRABLE_METADATA", detail, timeout)
        return {**common, "status": "PARKED", "notification_receipt": notification_receipt}, rejection_row(issue, row, "REPAIRABLE_METADATA", detail)

    # Unknown or contradictory output is evidence work, not a CTO decision.
    notification_receipt, obligation_id = escalate_investigation(issue, result_path, detail, timeout)
    terminal = {**common, "status": "INVESTIGATION_REQUIRED", "notification_receipt": notification_receipt, "obligation_id": obligation_id}
    return terminal, rejection_row(issue, row, "NEEDS_INVESTIGATION", detail)


def finalize_aggregate(path: Path, slack_ts: str) -> int:
    data = read_json(path)
    if data.get("schema") != "pm-ready-pool-contract-fanout/v1":
        raise RepairError("invalid aggregate schema")
    for row in data.get("results") or []:
        if isinstance(row, dict) and row.get("status") in {"NEEDS_CTO_PENDING_SLACK", "NEEDS_RAJIV_PENDING_SLACK"}:
            row["status"] = "NEEDS_CTO"
            row["slack_ts"] = slack_ts
    nonterminal = [row for row in data.get("results") or [] if not isinstance(row, dict) or row.get("status") not in TERMINAL_STATUSES]
    data["status"] = "complete" if not nonterminal else "incomplete"
    data["needs_cto_slack_ts"] = slack_ts
    data.pop("needs_rajiv_slack_ts", None)
    write_json(path, data)
    debug_log("aggregate_finalized", manifest=str(path), status=data["status"], slack_ts=slack_ts)
    print(f"READY_POOL_REPAIR_AGGREGATE_FINALIZED\nmanifest={path}\nstatus={data['status']}")
    return 0 if not nonterminal else 2


def ensure_tracking_label(issue: int, timeout: int) -> None:
    run(
        [GH, "label", "create", "pm-nonclaimable:tracking", "--repo", REPO,
         "--color", "6E7781", "--description", "Tracking or umbrella issue; never dispatch to a slot", "--force"],
        timeout=120,
    )
    proc = run(
        [GH, "issue", "edit", str(issue), "--repo", REPO, "--add-label", "pm-nonclaimable:tracking"],
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RepairError(f"failed to label tracking issue #{issue}: {proc.stderr.strip()}")


def apply_deterministic_repairs(
    data: dict[str, Any],
    timeout: int,
    *,
    limit: int,
) -> dict[str, list[int]]:
    """Repair only deficit-adjacent priority drift; broad hygiene belongs to slower horizons."""
    repaired: dict[str, list[int]] = {"priority": [], "failed": []}
    skipped = data.get("skipped") if isinstance(data.get("skipped"), dict) else {}
    priority_rows = list(skipped.get("priority_label_drift") or [])
    priority_rows.extend(
        row for row in data.get("needs_ready_pool_metadata") or []
        if isinstance(row, dict) and "priority" in str(row.get("reason") or "")
    )
    seen: set[int] = set()
    attempts = 0
    for row in priority_rows:
        if attempts >= max(0, limit):
            break
        if not isinstance(row, dict):
            continue
        issue = int(row["number"])
        if issue in seen:
            continue
        seen.add(issue)
        attempts += 1
        canonical = str(row.get("priority") or "").upper()
        if canonical not in {"P0", "P1", "P2", "P3"}:
            continue
        names = [str(name) for name in row.get("labels") or []]
        remove = [
            name for name in names
            if name != canonical and (
                name in {"P0", "P1", "P2", "P3"}
                or name == "high-priority"
                or name.startswith("priority:")
            )
        ]
        cmd = [GH, "issue", "edit", str(issue), "--repo", REPO, "--add-label", canonical]
        if remove:
            cmd.extend(["--remove-label", ",".join(remove)])
        proc = run(cmd, timeout=timeout)
        if proc.returncode != 0:
            repaired["failed"].append(issue)
            debug_log("priority_repair_failed", issue=issue, stderr=proc.stderr[-500:].strip())
            continue
        repaired["priority"].append(issue)
    return repaired


def start_auditor(issue: int, source: Path, run_id: str) -> dict[str, Any]:
    output = OUT_DIR / f"pm-ready-pool-contract-{run_id}-{issue}.json"
    output.unlink(missing_ok=True)
    prompt = f"mode=ready-pool-repair issue={issue} source_manifest={source} output={output}"
    cmd = [
        CLAUDE, "-p", "--agent", "issue-contract-auditor", "--output-format", "json",
        "--no-session-persistence", "--allow-dangerously-skip-permissions",
        "--permission-mode", "bypassPermissions", prompt,
    ]
    proc = subprocess.Popen(cmd, cwd=REPO_ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return {"issue": issue, "source": source, "output": output, "proc": proc, "started": time.monotonic()}


def stop_auditor(task: dict[str, Any]) -> tuple[str, str]:
    proc: subprocess.Popen[str] = task["proc"]
    if proc.poll() is None:
        proc.terminate()
    try:
        return proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.communicate()


def clean_candidate_result(row: dict[str, Any]) -> dict[str, Any]:
    issue = int(row["number"])
    body = live_issue_body(issue)
    body_file = OUT_DIR / f"pm-ready-pool-clean-{issue}-{sha256_text(body)[:12]}.md"
    body_file.write_text(body, encoding="utf-8")
    return {
        "expected_body_sha256": sha256_text(body),
        "proposed_body_file": str(body_file),
    }


def rejection_row(issue: int, row: dict[str, Any], rejection_class: str, detail: str) -> dict[str, Any]:
    return {
        "issue": issue,
        "queue": row.get("reason") or "ready_pool",
        "reason": rejection_class,
        "rejection_class": rejection_class,
        "detail": detail,
        "body_sha256": row.get("body_sha256") or "",
        "labels_fingerprint": row.get("labels_fingerprint") or "",
        "open_dependencies": row.get("open_dependencies") or [],
        "dependency_fingerprint": sha256_text(",".join(str(dep) for dep in sorted(row.get("open_dependencies") or []))),
    }


def post_exhaustion_summary(data: dict[str, Any], terminal: list[dict[str, Any]], timeout: int) -> str:
    current = int(data.get("current_ready_pool_count") or 0)
    target = int(data.get("ready_pool_target") or 10)
    skipped = data.get("skipped") if isinstance(data.get("skipped"), dict) else {}
    counts = {
        "promoted": sum(row.get("status") == "PROMOTED" for row in terminal),
        "repairable": len(data.get("needs_ready_pool_metadata") or []),
        "dependency_waits": len(skipped.get("open_dependency") or []),
        "tracking": len(skipped.get("non_dispatchable") or []),
        "external_actions": sum(row.get("status") == "EXTERNAL_ACTION" for row in terminal),
        "cto_decisions": sum(row.get("status") == "NEEDS_CTO" for row in terminal),
    }
    blocked = [
        f"#{int(row.get('issue') or 0)} {row.get('status')}: {str(row.get('reason') or 'no reason recorded')[:240]}"
        for row in terminal
        if int(row.get("issue") or 0) and row.get("status") != "PROMOTED"
    ]
    blocked_text = "\n".join(blocked) if blocked else "none"
    message = OUT_DIR / f"pm-ready-pool-exhausted-{now_id()}.txt"
    message.write_text(
        "*Ready Pool refill exhausted below target*\n"
        f"Final pool: {current}/{target}\nPromoted this run: {counts['promoted']}\n"
        f"Repairable now: {counts['repairable']}\nDependency waits: {counts['dependency_waits']}\n"
        f"Tracking/nonclaimable: {counts['tracking']}\nExternal actions: {counts['external_actions']}\n"
        f"CTO decisions: {counts['cto_decisions']}\n"
        f"Reviewed but not dispatchable:\n{blocked_text}\n"
        f"Manifest: `{data.get('_manifest_path', 'unknown')}`",
        encoding="utf-8",
    )
    return notify_pm(message, issue=None, timeout=timeout)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--issues", nargs="*", type=int)
    parser.add_argument("--max-concurrency", type=int, default=3)
    parser.add_argument("--auditor-timeout", type=int, default=1800)
    parser.add_argument("--command-timeout", type=int, default=1800)
    parser.add_argument("--finalize-aggregate", type=Path)
    parser.add_argument("--slack-ts")
    args = parser.parse_args(argv)
    if args.finalize_aggregate:
        if not args.slack_ts:
            parser.error("--finalize-aggregate requires --slack-ts")
        return finalize_aggregate(args.finalize_aggregate, args.slack_ts)
    if not args.source_manifest:
        parser.error("--source-manifest is required")

    source, source_data = source_manifest(args.source_manifest)
    rows = candidate_rows(source_data, set(args.issues or []) or None)
    current = int(source_data.get("current_ready_pool_count") or 0)
    deficit = int(source_data.get("promotion_deficit") or max(0, 10 - current))
    if not args.apply:
        preview_count = min(len(rows), max(0, deficit) + 3, 12)
        print("READY_POOL_CONTRACT_REPAIR_DRY_RUN")
        print(f"source_manifest={source}\ncurrent={current} deficit={deficit} candidates={len(rows)} preview_count={preview_count}")
        print("preview_issues=" + " ".join(str(row["number"]) for row in rows[:preview_count]))
        print("scan_policy=stream up to 3 auditors over one pinned candidate queue until target or exhaustion")
        print(f"next=python3 {Path(__file__).resolve()} --source-manifest {source} --apply")
        return 0

    run_id = now_id()
    terminal: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    processed: set[tuple[int, str]] = set()
    active: dict[int, dict[str, Any]] = {}
    stale_retries: dict[int, int] = {}
    hygiene_applied = False
    target_reached = deficit <= 0
    exhausted = False
    max_concurrency = max(1, min(3, args.max_concurrency))
    issue_filter = set(args.issues or []) or None
    lease = RepairLease(run_id=run_id, source=source, candidates=len(rows), current=current, deficit=deficit)
    if not lease.acquire():
        debug_log("repair_skipped_active_owner", run_id=run_id, lock_file=str(LOCK_FILE))
        print("READY_POOL_CONTRACT_REPAIR_ALREADY_RUNNING")
        print(f"lock_file={LOCK_FILE}")
        print(f"active_state={ACTIVE_STATE}")
        return 0
    lease.start()
    debug_log("repair_started", run_id=run_id, source_manifest=str(source), candidate_count=len(rows), current_ready_pool_count=current, promotion_deficit=deficit)

    latest_source = source
    latest_data = source_data
    current = live_ready_pool_count(args.command_timeout)
    remaining = max(0, 10 - current)
    while not target_reached and not exhausted:
        try:
            target_reached = remaining <= 0
            if target_reached:
                break

            if not hygiene_applied:
                repairs = apply_deterministic_repairs(
                    latest_data,
                    args.command_timeout,
                    limit=max(0, remaining) + REPAIR_BUFFER,
                )
                hygiene_applied = True
                if repairs["priority"] or repairs["failed"]:
                    debug_log("deterministic_repairs_applied", run_id=run_id, **repairs)

            # Clean reviewed candidates always precede contract-repair work.
            clean_rows = [row for row in latest_data.get("would_promote") or [] if isinstance(row, dict)]
            if issue_filter:
                clean_rows = [row for row in clean_rows if int(row.get("number") or 0) in issue_filter]
            clean = next(
                (row for row in clean_rows if (int(row["number"]), str(row.get("body_sha256") or "")) not in processed),
                None,
            )
            if clean is not None:
                issue = int(clean["number"])
                key = (issue, str(clean.get("body_sha256") or ""))
                processed.add(key)
                result = clean_candidate_result(clean)
                verdict, disposition, admission_reason, marker, model = arch_review(issue, result, args.command_timeout)
                if verdict in APPROVE_VERDICTS and disposition == "ADMIT":
                    promotion_manifest = promote_exact(issue, marker, args.command_timeout)
                    terminal.append({"issue": issue, "status": "PROMOTED", "result_file": result["proposed_body_file"], "promotion_manifest": str(promotion_manifest), "review_marker": marker, "review_model": model, "review_verdict": verdict, "admission_disposition": disposition, "admission_reason": admission_reason})
                else:
                    routed, rejection = route_arch_failure(
                        issue,
                        clean,
                        Path(result["proposed_body_file"]),
                        verdict,
                        disposition,
                        admission_reason,
                        marker,
                        model,
                        args.command_timeout,
                    )
                    terminal.append(routed)
                    rejected.append(rejection)
                debug_log("clean_candidate_terminal", run_id=run_id, issue=issue, verdict=verdict, admission_disposition=disposition)
                if terminal[-1]["status"] == "PROMOTED":
                    current = live_ready_pool_count(args.command_timeout)
                    remaining = max(0, 10 - current)
                lease.update(
                    processed_count=len(terminal),
                    promoted_count=sum(row.get("status") == "PROMOTED" for row in terminal),
                    parked_count=sum(row.get("status") == "PARKED" for row in terminal),
                    needs_cto_count=sum(row.get("status") == "NEEDS_CTO" for row in terminal),
                    investigation_required_count=sum(row.get("status") == "INVESTIGATION_REQUIRED" for row in terminal),
                    error_count=sum(row.get("status") == "ERROR" for row in terminal),
                    current_ready_pool_count=current,
                    promotion_deficit=remaining,
                )
                continue

            repair_rows = [
                row for row in candidate_rows(latest_data, issue_filter)
                if (int(row["number"]), str(row.get("body_sha256") or "")) not in processed
                and int(row["number"]) not in active
            ]
            while repair_rows and len(active) < max_concurrency:
                row = repair_rows.pop(0)
                issue = int(row["number"])
                task = start_auditor(issue, latest_source, run_id)
                task["row"] = row
                active[issue] = task
                debug_log("auditor_started", run_id=run_id, issue=issue, source_manifest=str(latest_source))

            if not active:
                exhausted = True
                break

            completed_issue = 0
            while not completed_issue:
                for issue, task in list(active.items()):
                    output: Path = task["output"]
                    proc: subprocess.Popen[str] = task["proc"]
                    elapsed = time.monotonic() - float(task["started"])
                    if output.is_file():
                        stop_auditor(task)
                        completed_issue = issue
                        break
                    if proc.poll() is not None:
                        stdout, stderr = proc.communicate()
                        terminal.append({"issue": issue, "status": "ERROR", "result_file": str(output), "reason": f"auditor_failed:{proc.returncode}:{stderr[-500:].strip()}"})
                        processed.add((issue, str(task["row"].get("body_sha256") or "")))
                        active.pop(issue, None)
                        completed_issue = -1
                        break
                    if elapsed >= args.auditor_timeout:
                        stop_auditor(task)
                        terminal.append({"issue": issue, "status": "ERROR", "result_file": str(output), "reason": "auditor_timeout"})
                        processed.add((issue, str(task["row"].get("body_sha256") or "")))
                        active.pop(issue, None)
                        completed_issue = -1
                        break
                if not completed_issue:
                    time.sleep(0.25)
            if completed_issue < 0:
                continue

            task = active.pop(completed_issue)
            row = task["row"]
            result_path: Path = task["output"]
            issue = completed_issue
            processed_key = (issue, str(row.get("body_sha256") or ""))
            processed.add(processed_key)
            try:
                result = validate_audit_result(result_path, issue=issue, source=Path(task["source"]).resolve())
                outcome = result["outcome"]
                if outcome == "PARKED":
                    reason = str(result.get("reason") or "not dispatchable")
                    rejection_class = "TRACKING" if "track" in reason.lower() or "umbrella" in reason.lower() else "PARKED"
                    if rejection_class == "TRACKING":
                        ensure_tracking_label(issue, args.command_timeout)
                    notification_receipt = escalate_parked_candidate(
                        issue,
                        result_path,
                        rejection_class,
                        reason,
                        args.command_timeout,
                    )
                    terminal.append({"issue": issue, "status": "PARKED", "result_file": str(result_path), "reason": reason, "notification_receipt": notification_receipt})
                    rejected.append(rejection_row(issue, row, rejection_class, reason))
                elif outcome == "NEEDS_CTO":
                    notification_receipt, obligation_id = escalate_cto_decision(issue, result_path, result, args.command_timeout)
                    terminal.append({"issue": issue, "status": "NEEDS_CTO", "result_file": str(result_path), "reason": result.get("reason"), "decision_kind": result.get("decision_kind"), "decision_question": result.get("decision_question"), "recommended_default": result.get("recommended_default"), "notification_receipt": notification_receipt, "obligation_id": obligation_id})
                    rejected.append(rejection_row(issue, row, "NEEDS_CTO", str(result.get("decision_question") or result.get("reason") or "decision required")))
                elif outcome == "EXTERNAL_ACTION":
                    notification_receipt, obligation_id = escalate_external_action(issue, result_path, result, args.command_timeout)
                    terminal.append({"issue": issue, "status": "EXTERNAL_ACTION", "result_file": str(result_path), "reason": result.get("reason"), "external_owner": result.get("external_owner"), "external_action": result.get("external_action"), "notification_receipt": notification_receipt, "obligation_id": obligation_id})
                    rejected.append(rejection_row(issue, row, "EXTERNAL_ACTION", str(result.get("external_action") or "external action required")))
                else:
                    verdict, disposition, admission_reason, marker, model = arch_review(issue, result, args.command_timeout)
                    if verdict not in APPROVE_VERDICTS or disposition != "ADMIT":
                        routed, rejection = route_arch_failure(
                            issue,
                            row,
                            result_path,
                            verdict,
                            disposition,
                            admission_reason,
                            marker,
                            model,
                            args.command_timeout,
                        )
                        terminal.append(routed)
                        rejected.append(rejection)
                    else:
                        apply_proposed_body(issue, result)
                        promotion_manifest = promote_exact(issue, marker, args.command_timeout)
                        terminal.append({"issue": issue, "status": "PROMOTED", "result_file": str(result_path), "promotion_manifest": str(promotion_manifest), "review_marker": marker, "review_model": model, "review_verdict": verdict, "admission_disposition": disposition, "admission_reason": admission_reason})
                debug_log("issue_terminal", run_id=run_id, issue=issue, status=terminal[-1]["status"], reason=terminal[-1].get("reason") or "")
                if terminal[-1]["status"] == "PROMOTED":
                    current = live_ready_pool_count(args.command_timeout)
                    remaining = max(0, 10 - current)
            except (RepairError, OSError, ValueError, json.JSONDecodeError) as exc:
                if str(exc) == "stale_issue_body" and stale_retries.get(issue, 0) < 1:
                    stale_retries[issue] = stale_retries.get(issue, 0) + 1
                    processed.discard(processed_key)
                    result_path.unlink(missing_ok=True)
                    debug_log(
                        "stale_issue_body_retry",
                        run_id=run_id,
                        issue=issue,
                        attempt=stale_retries[issue],
                    )
                    continue
                terminal.append({"issue": issue, "status": "ERROR", "result_file": str(result_path), "reason": str(exc)})
                debug_log("issue_terminal", run_id=run_id, issue=issue, status="ERROR", reason=str(exc))

            lease.update(
                processed_count=len(terminal),
                promoted_count=sum(row.get("status") == "PROMOTED" for row in terminal),
                parked_count=sum(row.get("status") == "PARKED" for row in terminal),
                needs_cto_count=sum(row.get("status") == "NEEDS_CTO" for row in terminal),
                investigation_required_count=sum(row.get("status") == "INVESTIGATION_REQUIRED" for row in terminal),
                error_count=sum(row.get("status") == "ERROR" for row in terminal),
                current_ready_pool_count=current,
                promotion_deficit=remaining,
            )
        except (RepairError, OSError, ValueError, json.JSONDecodeError) as exc:
            terminal.append({"issue": 0, "status": "ERROR", "result_file": "", "reason": str(exc)})
            debug_log("pool_reconciliation_failed", run_id=run_id, reason=str(exc))
            break

    for task in active.values():
        stop_auditor(task)

    rejection_manifest = record_rejections(latest_source, rejected, args.command_timeout)
    exhaustion_notification_receipt = ""
    if exhausted and not target_reached:
        try:
            exhaustion_notification_receipt = post_exhaustion_summary(latest_data, terminal, args.command_timeout)
        except RepairError as exc:
            terminal.append({"issue": 0, "status": "ERROR", "result_file": "", "reason": str(exc)})
            debug_log("exhaustion_notification_failed", run_id=run_id, reason=str(exc))
    nonterminal = [row for row in terminal if row.get("status") not in TERMINAL_STATUSES]
    notification_failed = any(
        row.get("status") == "ERROR" and str(row.get("reason") or "").startswith("ready_pool_notification_failed")
        for row in nonterminal
    )
    aggregate = OUT_DIR / f"pm-ready-pool-contract-fanout-{run_id}.json"
    payload = {
        "schema": "pm-ready-pool-contract-fanout/v1",
        "status": "complete" if (target_reached or exhausted) and not nonterminal else "incomplete",
        "source_manifest": str(source), "deficit": deficit, "window_size": len(terminal), "results": terminal,
        "target_reached": target_reached, "candidate_exhausted": exhausted, "review_result_manifest": rejection_manifest,
        "exhaustion_notification_receipt": exhaustion_notification_receipt,
        "next_command": (
            "restore PM message transport, deliver the recorded message_file, then rerun"
            if notification_failed
            else "rerun failed issues" if nonterminal else "none"
        ),
    }
    write_json(aggregate, payload)
    lease.finish(status=payload["status"], aggregate=aggregate)
    debug_log(
        "repair_finished",
        run_id=run_id,
        status=payload["status"],
        aggregate_manifest=str(aggregate),
        target_reached=target_reached,
        candidate_exhausted=exhausted,
    )
    print("READY_POOL_CONTRACT_REPAIR_RESULT")
    print(f"manifest={aggregate}\nstatus={payload['status']} target_reached={target_reached} exhausted={exhausted}")
    for status in ("PROMOTED", "PARKED", "NEEDS_CTO", "INVESTIGATION_REQUIRED", "EXTERNAL_ACTION", "ERROR"):
        print(status.lower() + "=" + ",".join(str(row["issue"]) for row in terminal if row.get("status") == status))
    if payload["next_command"] != "none":
        print(f"next={payload['next_command']}")
    return 0 if payload["status"] == "complete" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (RepairError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"READY_POOL_CONTRACT_REPAIR_DEGRADED\nerror={exc}", file=sys.stderr)
        raise SystemExit(2)
