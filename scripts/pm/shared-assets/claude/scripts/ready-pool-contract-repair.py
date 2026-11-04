#!/usr/bin/env python3
"""Repair backlog Ready Pool contracts.

Default mode is deterministic and standalone (safe in a background agent): it repairs
only from each issue's own existing ACs, reports unresolved contracts explicitly,
and promotes completed contracts in the same run. No architecture-review or nested
auditor prerequisite remains. Named dependencies are preserved for live assignment checks.
"""

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


REPO = os.environ.get("GH_REPO", "Scribie/heydonna-app")
REPO_ROOT = Path(os.environ.get("HEYDONNA_REPO_ROOT", "/Users/rajiv/Downloads/projects/heydonna-app"))
BACKLOG_TRIAGE = Path(os.environ.get("BACKLOG_TRIAGE", "/Users/rajiv/.claude/scripts/backlog-triage.py"))
PM_OPS = Path(os.environ.get("PM_OPS", "/Users/rajiv/.claude/scripts/pm-ops.py"))
# pm-transition-alert.sh is RETIRED (Rajiv 2026-09-21). Alerts post the message file via slack-send.sh.
SLACK_SEND = Path(os.environ.get("PM_SLACK_SEND", "/Users/rajiv/.claude/scripts/slack-send.sh"))
PM_ALERT_CHANNEL = os.environ.get("PM_READY_POOL_ALERT_CHANNEL", "C0ALZJHGE49")
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


def post_alert(args: list[str], *, timeout: int = 1200) -> subprocess.CompletedProcess[str]:
    """Post an alert's --message-file to Slack; stdout carries reply_ts=<ts> on success."""
    msg = Path(args[args.index("--message-file") + 1])
    with msg.open("r", encoding="utf-8") as fh:
        proc = subprocess.run([str(SLACK_SEND), "-c", PM_ALERT_CHANNEL, "-f"], stdin=fh,
                              text=True, capture_output=True, timeout=timeout)
    match = re.search(r"\bOK ts=(\S+)", proc.stdout)
    if proc.returncode == 0 and match:
        return subprocess.CompletedProcess(proc.args, 0, f"reply_ts={match.group(1)}\n", proc.stderr)
    return subprocess.CompletedProcess(proc.args, proc.returncode or 1, proc.stdout, proc.stderr)


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
    if not isinstance(rows, list) or not rows:
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
        if row.get("priority") == "P3" and not row.get("open_dependencies") and int(data.get("current_ready_pool_count") or 0) >= int(data.get("ready_pool_target") or 10):
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


def apply_proposed_body(issue: int, result: dict[str, Any]) -> None:
    if not body_hash_matches(live_issue_body(issue), result["expected_body_sha256"]):
        raise RepairError("stale_issue_body_before_apply")
    proc = run([GH, "issue", "edit", str(issue), "--repo", REPO, "--body-file", result["proposed_body_file"]], timeout=120)
    if proc.returncode != 0:
        raise RepairError(f"gh issue edit failed: {proc.stderr.strip()}")
    proposal = Path(result["proposed_body_file"]).read_text(encoding="utf-8").rstrip("\n")
    if live_issue_body(issue) != proposal:
        raise RepairError("live body verification failed after edit")


def promote_exact(issue: int, timeout: int) -> Path:
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
        data = read_json(dry_path)
        if any(int(row.get("number") or 0) == issue for row in data.get("skipped", {}).get("p3_refill_not_needed") or []):
            raise RepairError("p3_refill_not_selected")
        raise RepairError("repaired issue is still not a clean promotion candidate")
    applied = run(
        [str(BACKLOG_TRIAGE), "promote", "--apply", "--defer-reconcile",
         "--issues", str(issue)],
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
            "number,labels",
        ],
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RepairError(f"ready-pool count failed: {proc.stderr.strip()}")
    decoded = json.loads(proc.stdout)
    if not isinstance(decoded, list):
        raise RepairError("ready-pool count did not return a JSON list")
    # Claimable only: no slot:N, pm-blocked:*, pm-nonclaimable*, or open PR.
    pr_proc = run(
        [GH, "pr", "list", "--repo", REPO, "--state", "open", "--limit", "300",
         "--json", "closingIssuesReferences"],
        timeout=timeout,
    )
    pr_issues: set[int] = set()
    if pr_proc.returncode == 0:
        for pr in json.loads(pr_proc.stdout or "[]"):
            for ref in pr.get("closingIssuesReferences") or []:
                if ref.get("number"):
                    pr_issues.add(int(ref["number"]))
    def claimable(row: dict[str, Any]) -> bool:
        if int(row.get("number") or 0) in pr_issues:
            return False
        for lab in row.get("labels") or []:
            name = lab.get("name", "")
            if re.fullmatch(r"slot:\d+", name) or name.startswith(("pm-blocked:", "pm-nonclaimable")):
                return False
        return True
    return sum(1 for row in decoded if claimable(row))


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
    alert = post_alert(
        [ "--event", "external-action-required", "--issue", str(issue),
         "--state", action_hash, "--reason", action, "--proof", str(result_path), "--message-file", str(message_path)],
        timeout=timeout,
    )
    if alert.returncode != 0:
        raise RepairError("failed to post external-action alert")
    match = re.search(r"\breply_ts=([^\s]+)", alert.stdout)
    if not match:
        raise RepairError("external-action alert did not return reply_ts")
    return match.group(1), obligation_id


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
    alert = post_alert(
        [
            "--event", "decision-required",
            "--issue", str(issue),
            "--state", f"{decision_kind}:{decision_hash}",
            "--reason", question,
            "--proof", str(result_path),
            "--message-file", str(message_path),
        ],
        timeout=timeout,
    )
    if alert.returncode != 0:
        debug_log(
            "cto_escalation_alert_failed",
            issue=issue,
            stdout=alert.stdout[-500:].strip(),
            stderr=alert.stderr[-500:].strip(),
        )
        raise RepairError(f"failed to post CTO-decision alert: {alert.stderr.strip() or alert.stdout.strip()}")
    match = re.search(r"\breply_ts=([^\s]+)", alert.stdout)
    if not match:
        debug_log("cto_escalation_alert_missing_ts", issue=issue, stdout=alert.stdout[-500:].strip())
        raise RepairError(f"CTO-decision alert did not return reply_ts: {alert.stdout.strip()}")
    slack_ts = match.group(1)
    debug_log("cto_escalation_complete", issue=issue, obligation_id=obligation_id, slack_ts=slack_ts)
    return slack_ts, obligation_id


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
    alert = post_alert(
        [ "--event", "investigation-required", "--issue", str(issue),
         "--state", investigation_hash, "--reason", detail, "--proof", str(result_path),
         "--message-file", str(message_path)],
        timeout=timeout,
    )
    if alert.returncode != 0:
        raise RepairError("failed to post Ready Pool investigation alert")
    match = re.search(r"\breply_ts=([^\s]+)", alert.stdout)
    if not match:
        raise RepairError("Ready Pool investigation alert did not return reply_ts")
    return match.group(1), obligation_id


def escalate_parked_candidate(
    issue: int,
    result_path: Path,
    classification: str,
    detail: str,
    timeout: int,
) -> str:
    """Make every reviewed non-dispatchable candidate visible in its transition thread."""
    state_hash = sha256_text(f"{classification}\n{detail}")[:16]
    message_path = OUT_DIR / f"pm-ready-pool-parked-{issue}-{state_hash}.txt"
    message_path.write_text(
        f"*Issue #{issue} was not admitted to the Ready Pool*\n"
        f"Classification: `{classification}`\n"
        f"Reason/unblock condition: {detail}\n"
        "The issue remains `status:backlog`; the promoter will reconsider it after the cited evidence changes.",
        encoding="utf-8",
    )
    alert = post_alert(
        [ "--event", "ready-pool-candidate-parked", "--issue", str(issue),
         "--state", f"{classification}:{state_hash}", "--reason", detail, "--proof", str(result_path),
         "--message-file", str(message_path)],
        timeout=timeout,
    )
    if alert.returncode != 0:
        debug_log(
            "parked_candidate_alert_failed",
            issue=issue,
            returncode=alert.returncode,
            stderr=alert.stderr[-500:].strip(),
        )
        return ""
    match = re.search(r"\breply_ts=([^\s]+)", alert.stdout)
    if not match:
        debug_log(
            "parked_candidate_alert_missing_reply_ts",
            issue=issue,
            stdout=alert.stdout[-500:].strip(),
        )
        return ""
    return match.group(1)


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
        slack_ts, obligation_id = escalate_cto_decision(issue, result_path, decision, timeout)
        terminal = {**common, "status": "NEEDS_CTO", "slack_ts": slack_ts, "obligation_id": obligation_id}
        return terminal, rejection_row(issue, row, "NEEDS_CTO", detail)
    if disposition == "NEEDS_INVESTIGATION":
        slack_ts, obligation_id = escalate_investigation(issue, result_path, detail, timeout)
        terminal = {**common, "status": "INVESTIGATION_REQUIRED", "slack_ts": slack_ts, "obligation_id": obligation_id}
        return terminal, rejection_row(issue, row, "NEEDS_INVESTIGATION", detail)
    if disposition == "PARK_TRACKING":
        ensure_tracking_label(issue, timeout)
        slack_ts = escalate_parked_candidate(issue, result_path, "TRACKING", detail, timeout)
        return {**common, "status": "PARKED", "slack_ts": slack_ts}, rejection_row(issue, row, "TRACKING", detail)
    if disposition == "PARK_DEPENDENCY":
        slack_ts = escalate_parked_candidate(issue, result_path, "DEPENDENCY_WAIT", detail, timeout)
        return {**common, "status": "PARKED", "slack_ts": slack_ts}, rejection_row(issue, row, "DEPENDENCY_WAIT", detail)
    if disposition == "DUPLICATE_OR_SUPERSEDED":
        slack_ts = escalate_parked_candidate(issue, result_path, "DUPLICATE_SUPERSEDED", detail, timeout)
        return {**common, "status": "PARKED", "slack_ts": slack_ts}, rejection_row(issue, row, "DUPLICATE_SUPERSEDED", detail)
    if disposition == "REPAIR_CONTRACT":
        slack_ts = escalate_parked_candidate(issue, result_path, "REPAIRABLE_METADATA", detail, timeout)
        return {**common, "status": "PARKED", "slack_ts": slack_ts}, rejection_row(issue, row, "REPAIRABLE_METADATA", detail)

    # Unknown or contradictory output is evidence work, not a CTO decision.
    slack_ts, obligation_id = escalate_investigation(issue, result_path, detail, timeout)
    terminal = {**common, "status": "INVESTIGATION_REQUIRED", "slack_ts": slack_ts, "obligation_id": obligation_id}
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
    proc = post_alert(
        [ "--event", "ready-pool-exhausted", "--state", f"{current}-of-{target}",
         "--reason", f"final_pool={current} target={target}", "--message-file", str(message)],
        timeout=timeout,
    )
    match = re.search(r"\breply_ts=([^\s]+)", proc.stdout)
    return match.group(1) if proc.returncode == 0 and match else ""


# --------------------------------------------------------------------------
# Deterministic, standalone repair (no nested subagents; Rajiv 2026-10-03).
# Repairs a Ready Pool contract only from the issue's own existing ACs/body.
# --------------------------------------------------------------------------
ICL_VALIDATOR = Path(os.environ.get("HEYDONNA_ICL_VALIDATOR", "/Users/rajiv/.claude/scripts/validate-issue-contract-ledger.py"))
NEEDS_CONTRACT_LABEL = "needs-contract"
REPAIR_MARK = "<!-- ready-pool-repair: deterministic; derived only from existing ACs; no AC added, removed or reworded -->"
_AC_HEADING = re.compile(
    r"(?im)^(#{1,4})\s*(?:acceptance criteria|acceptance criterion|acs?)(?:\s*\([^\n)]*\))?\s*:?[ \t]*$"
)
_AC_ITEM = re.compile(r"^(\s{0,3})(?:[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+)(\S.*)$")
_VISUAL_WORDS = re.compile(r"\b(screenshot|visual|ui|layout|render(?:s|ed|ing)?|button|modal|dialog|panel|toolbar|banner|toast|css|pixel|badge|dropdown|menu|icon|footer|header)\b", re.I)
_PROOF_WORDS = (
    ("e2e", re.compile(r"\b(e2e|playwright|end-to-end)\b", re.I)),
    ("integration", re.compile(r"\b(integration)\b", re.I)),
    ("artifact", re.compile(r"\b(artifact|docx|ooxml|r2|export)\b", re.I)),
    ("telemetry", re.compile(r"\b(telemetry|axiom|langsmith|metric)\b", re.I)),
)


def _triage():
    import importlib.util
    spec = importlib.util.spec_from_file_location("backlog_triage_mod", str(BACKLOG_TRIAGE))
    if spec is None or spec.loader is None:
        raise RepairError(f"cannot load {BACKLOG_TRIAGE}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("backlog_triage_mod", mod)
    spec.loader.exec_module(mod)
    return mod


def extract_acs(body: str) -> list[str]:
    """Return the issue's own top-level AC items, verbatim (never invented)."""
    heading = _AC_HEADING.search(body or "")
    if not heading:
        return []
    level = len(heading.group(1))
    items: list[str] = []
    item_indent: int | None = None
    for line in (body or "")[heading.end():].splitlines():
        h = re.match(r"^(#{1,6})\s+\S", line)
        if h and len(h.group(1)) <= level:
            break
        m = _AC_ITEM.match(line)
        if m:
            indent = len(m.group(1))
            if item_indent is None:
                item_indent = indent
            if indent != item_indent:
                continue
            text = re.sub(r"\s+", " ", m.group(2)).strip()
            if len(re.sub(r"[^A-Za-z0-9]", "", text)) >= 12:
                items.append(text)
    return items


def ledger_errors(body: str, *, require_qa_proof: bool = True) -> list[str]:
    cmd = [sys.executable, str(ICL_VALIDATOR), "--json"] + (["--require-qa-proof"] if require_qa_proof else [])
    proc = subprocess.run(cmd, input=body or "", text=True, capture_output=True, check=False)
    try:
        return [str(e) for e in json.loads(proc.stdout).get("errors", [])]
    except json.JSONDecodeError:
        return ["icl_validator_failed"]


def _first_clause(text: str, limit: int = 240) -> str:
    text = re.sub(r"`", "", text).strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _ac_text_for_ledger(acs: list[str], limit: int = 600) -> str:
    joined = " ; ".join(_first_clause(a, 160) for a in acs[:4])
    if len(acs) > 4:
        joined += f" ; (+{len(acs) - 4} more, see Acceptance criteria)"
    return joined[:limit]


def build_ledger(acs: list[str]) -> str:
    rows = [
        ("Positive AC", _ac_text_for_ledger(acs)),
        ("Negative AC", "None of the acceptance criteria above may be weakened, skipped or waived to pass; behavior outside the stated scope must not regress."),
        ("Forbidden implementation", "Skipping/xfail-ing tests, raising timeouts or loosening assertions to satisfy an AC, and any approach that does not exercise the AC at its real runtime control point."),
        ("Determinism", "As stated in each acceptance criterion; behavior that depends on a model/provider is called out in that AC."),
        ("Required proof", "The proof each acceptance criterion names (tests/artifacts), run at the real runtime control point; failing test first for bug fixes."),
    ]
    out = ["## Issue Contract Ledger", "", "| Contract row | Requirement |", "| --- | --- |"]
    out += [f"| {k} | {v.replace('|', '/')} |" for k, v in rows]
    return "\n".join(out) + "\n"


def annotate_ac_proof(body: str) -> str:
    """Add missing QA-proof sub-fields under existing `AC-n:` lines only; AC text is untouched."""
    heading = _AC_HEADING.search(body or "")
    if not heading:
        return body
    level = len(heading.group(1))
    lines = body.splitlines()
    start = body[: heading.end()].count("\n") + 1
    end = len(lines)
    for i in range(start, len(lines)):
        h = re.match(r"^(#{1,6})\s+\S", lines[i])
        if h and len(h.group(1)) <= level:
            end = i
            break
    ac_line = re.compile(r"^\s*(?:[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+)(AC-[A-Za-z0-9_-]+)\s*:\s*(\S.*)$", re.I)
    field_line = re.compile(r"^\s+(?:[-*+]\s+)?(Surface|QA reachability|Required proof|QA scenario|Screenshot exemption)\s*:\s*\S", re.I)
    out = lines[:start]
    i = start
    while i < end:
        m = ac_line.match(lines[i])
        if not m:
            out.append(lines[i]); i += 1; continue
        j = i + 1
        block: list[str] = []
        while j < end and not ac_line.match(lines[j]) and (field_line.match(lines[j]) or not _AC_ITEM.match(lines[j])):
            block.append(lines[j]); j += 1
        trail: list[str] = []
        while block and not block[-1].strip():
            trail.insert(0, block.pop())
        have = {field_line.match(b).group(1).lower() for b in block if field_line.match(b)}
        text = m.group(2)
        visual = bool(_VISUAL_WORDS.search(text))
        proofs = [name for name, rx in _PROOF_WORDS if rx.search(text)] or ["unit"]
        if visual and "screenshot" not in proofs:
            proofs.insert(0, "screenshot")
        add = []
        if "surface" not in have:
            add.append(f"   - Surface: {'visual' if visual else 'nonvisual'}")
        if "qa reachability" not in have:
            add.append("   - QA reachability: deterministic")
        if "required proof" not in have:
            add.append(f"   - Required proof: {', '.join(proofs)}")
        if visual and "qa scenario" not in have:
            add.append(f"   - QA scenario: Reach the state described by this AC in the UI and capture it: {_first_clause(text, 160)}")
        out.append(lines[i]); out += block; out += add; out += trail
        i = j
    out += lines[end:]
    return "\n".join(out) + ("\n" if body.endswith("\n") else "")


def _derive_work_type(labels: set[str], title: str) -> str:
    t = title.lower()
    if t.startswith(("test", "flake")) or "e2e" in t:
        return "app-test"
    if "bug" in labels or t.startswith("fix"):
        return "bug"
    return "app-code"


def _derive_lane(labels: set[str], title: str) -> str:
    m = re.match(r"^\s*\w+\(([a-z0-9_-]+)\)", title or "", re.I)
    if m:
        return m.group(1).lower()
    for name in sorted(labels):
        if name.startswith(("area:", "lane:")):
            return name.split(":", 1)[1]
    return "general"


def _emit_block(meta: dict[str, str]) -> str:
    order = ["priority", "lane", "ac_summary", "claimable_slot_type", "blockers", "work_type", "branch_slug"]
    keys = order + [k for k in meta if k not in order]
    return "<!-- ready-pool:\n" + "\n".join(f"{k}: {meta[k]}" for k in keys if k in meta) + "\n-->"


def repair_contract(issue: dict[str, Any], *, fallback_priority: str = "") -> dict[str, Any]:
    """Pure deterministic repair of one issue dict {number,title,labels,body,state}.

    Returns {status, body, reasons, add_label, ledger_errors}. status is one of
    SKIPPED, ALREADY_COMPLETE, REPAIRED, NEEDS_CONTRACT, BLOCKED_CONTRACT.
    """
    bt = _triage()
    body = str(issue.get("body") or "")
    title = str(issue.get("title") or "")
    names = {str(l.get("name") if isinstance(l, dict) else l) for l in issue.get("labels") or []}
    res: dict[str, Any] = {"status": "", "body": body, "reasons": [], "add_label": [], "ledger_errors": []}
    if str(issue.get("state") or "OPEN").upper() != "OPEN":
        res.update(status="SKIPPED", reasons=["not_open"]); return res
    prio = next((p for p in ("P0", "P1", "P2", "P3") if p in names), "")
    if not prio:
        prio = fallback_priority.upper() if fallback_priority.upper() in {"P0", "P1", "P2", "P3"} else ""
        if prio:
            res["add_label"].append(prio)
    if not prio:
        res.update(status="NEEDS_CONTRACT", reasons=["no_priority"]); return res

    acs = extract_acs(body)
    if not acs:
        res.update(status="NEEDS_CONTRACT", reasons=["no_usable_acs"]); return res
    if len(acs) > 3:
        res.update(status="NEEDS_CONTRACT", reasons=["split_required_more_than_three_acs"]); return res

    blocks = bt.ready_pool_blocks(body)
    meta = bt.parse_ready_pool_block(blocks[-1]) if blocks else {}
    if meta and (bt.is_non_dispatchable_contract({"labels": [{"name": n} for n in names - {"pm-blocked:dependency"}], "title": title, "body": body}, meta)
                 or (not bt.metadata_blockers_clear(meta) and not bt.has_dependency_contract(meta))):
        res.update(status="BLOCKED_CONTRACT", reasons=["contract_declares_blocked_or_non_dispatchable"]); return res

    new = dict(meta)
    if str(new.get("priority", "")).upper() != prio:
        new["priority"] = prio
    if not new.get("lane"):
        new["lane"] = _derive_lane(names, title)
    if not new.get("ac_summary") or new.get("ac_summary") == title:
        new["ac_summary"] = _first_clause(acs[0], 140).replace("\n", " ")
    if not new.get("claimable_slot_type"):
        new["claimable_slot_type"] = "dev"
    if "blockers" not in new:
        declared = bt.extract_deps_from_lines(body) | bt.extract_deps_from_title(title)
        if declared:
            new["blockers"] = "[" + ", ".join(f"#{d}" for d in sorted(declared)) + "]"
        else:
            new["blockers"] = "none"
    if not new.get("work_type"):
        new["work_type"] = _derive_work_type(names, title)
    try:
        bt.validate_branch_slug(new.get("branch_slug"))
    except ValueError:
        try:
            new["branch_slug"] = bt.concise_branch_slug(title)
        except ValueError:
            res.update(status="NEEDS_CONTRACT", reasons=["no_branch_slug_source"]); return res

    out = body
    if new != meta or not blocks:
        block = _emit_block(new)
        if blocks:
            last = list(re.finditer(r"<!--\s*ready-pool:.*?-->", out, re.S | re.I))[-1]
            out = out[: last.start()] + block + out[last.end():]
        else:
            out = out.rstrip("\n") + "\n\n" + block + "\n"
    errors = ledger_errors(out)
    if "missing_issue_contract_ledger" in errors:
        out = out.rstrip("\n") + "\n\n" + build_ledger(acs)
        errors = ledger_errors(out)
    elif any(e.startswith(("missing_icl_row", "placeholder_icl_row")) for e in errors):
        res.update(status="NEEDS_CONTRACT", reasons=["partial_ledger_not_rewritten:" + ",".join(errors)], ledger_errors=errors); return res
    if any(e.startswith("missing_ac_field") for e in errors):
        res.update(status="NEEDS_CONTRACT", reasons=["missing_explicit_qa_proof_contract"], ledger_errors=errors); return res
    if errors:
        reason = ("acs_lack_ac_ids_and_qa_proof_fields_not_rewritten" if errors == ["missing_ac_proof_contract"]
                  else "ledger_invalid_after_repair")
        res.update(status="NEEDS_CONTRACT", reasons=[reason], ledger_errors=errors); return res
    if out == body:
        res.update(status="ALREADY_COMPLETE"); return res
    if REPAIR_MARK not in out:
        out = out.rstrip("\n") + "\n\n" + REPAIR_MARK + "\n"
    res.update(status="REPAIRED", body=out)
    return res


def _gh_json_issue(number: int) -> dict[str, Any]:
    proc = run([GH, "issue", "view", str(number), "--repo", REPO, "--json", "number,title,labels,body,state"])
    if proc.returncode != 0:
        raise RepairError(f"gh issue view #{number} failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def run_deterministic(source: Path, data: dict[str, Any], issue_filter: set[int] | None, apply: bool) -> int:
    run_id = now_id()
    rows = candidate_rows(data, issue_filter)
    summary: dict[str, list[Any]] = {k: [] for k in ("repaired", "already_complete", "promoted", "needs_contract", "blocked_contract", "skipped", "failed")}
    for row in rows:
        n = int(row["number"])
        try:
            issue = _gh_json_issue(n)
            res = repair_contract(issue, fallback_priority=str(row.get("priority") or ""))
        except (RepairError, OSError, json.JSONDecodeError) as exc:
            summary["failed"].append({"issue": n, "error": str(exc)[:200]}); continue
        st = res["status"]
        key = {"REPAIRED": "repaired", "ALREADY_COMPLETE": "already_complete", "NEEDS_CONTRACT": "needs_contract",
               "BLOCKED_CONTRACT": "blocked_contract"}.get(st, "skipped")
        entry = {"issue": n, "reasons": res["reasons"]}
        if apply and st == "REPAIRED":
            path = OUT_DIR / f"ready-pool-repair-{n}-{run_id}.md"
            path.write_text(res["body"], encoding="utf-8")
            if ledger_errors(path.read_text(encoding="utf-8")):
                summary["failed"].append({"issue": n, "error": "validator rejected final body"}); continue
            if not body_hash_matches(live_issue_body(n), sha256_text(issue.get("body") or "")):
                summary["failed"].append({"issue": n, "error": "stale_issue_body_before_apply"}); continue
            proc = run([GH, "issue", "edit", str(n), "--repo", REPO, "--body-file", str(path)])
            if proc.returncode != 0:
                summary["failed"].append({"issue": n, "error": proc.stderr.strip()[:200]}); continue
            if live_issue_body(n) != res["body"].rstrip("\n"):
                summary["failed"].append({"issue": n, "error": "live_body_verification_failed"}); continue
            for label in res["add_label"]:
                proc = run([GH, "issue", "edit", str(n), "--repo", REPO, "--add-label", label])
                if proc.returncode != 0:
                    summary["failed"].append({"issue": n, "error": "priority_label_edit_failed"})
                    break
            else:
                entry["body_file"] = str(path)
            if "body_file" not in entry:
                continue
        if apply and st in {"REPAIRED", "ALREADY_COMPLETE"}:
            try:
                entry["promotion_manifest"] = str(promote_exact(n, 1800))
                summary["promoted"].append({**entry, "status": "PROMOTED"})
            except (RepairError, OSError, subprocess.TimeoutExpired) as exc:
                if str(exc) == "p3_refill_not_selected":
                    summary["skipped"].append({"issue": n, "reasons": ["P3 remains parked until selected for a claimable deficit after higher priorities."]})
                else:
                    summary["failed"].append({"issue": n, "error": str(exc)[:200]})
        summary[key].append(entry)
    manifest = OUT_DIR / f"pm-ready-pool-contract-repair-deterministic-{run_id}.json"
    final_scan = None
    remaining = None
    if apply:
        try:
            _current, _deficit, final_scan = live_deficit(1800)
            fresh = read_json(final_scan)
            remaining = len(fresh.get("would_promote") or []) + len(fresh.get("needs_ready_pool_metadata") or [])
        except (RepairError, OSError, subprocess.TimeoutExpired) as exc:
            summary["failed"].append({"issue": 0, "error": "final_scan_failed:" + str(exc)[:180]})
    complete = apply and remaining == 0 and not any(summary[k] for k in ("failed", "needs_contract", "blocked_contract"))
    manifest.write_text(json.dumps({"schema": "pm-ready-pool-contract-repair-deterministic/v1", "apply": apply,
                                    "status": "complete" if complete else "action_required",
                                    "final_scan_manifest": str(final_scan) if final_scan else None,
                                    "source_manifest": str(source), **summary}, indent=2), encoding="utf-8")
    print("READY_POOL_CONTRACT_REPAIR_DETERMINISTIC " + ("APPLIED" if apply else "DRY_RUN"))
    print(f"manifest={manifest}")
    for k, v in summary.items():
        print(f"{k}={len(v)} " + " ".join(f"#{e['issue']}" for e in v))
    return 2 if summary["failed"] else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--issues", nargs="*", type=int)
    parser.add_argument("--max-concurrency", type=int, default=3, help=argparse.SUPPRESS)
    parser.add_argument("--auditor-timeout", type=int, default=1800, help=argparse.SUPPRESS)
    parser.add_argument("--command-timeout", type=int, default=1800)
    parser.add_argument("--use-auditors", action="store_true", help="deprecated; uses deterministic repair without nested agents or architecture review")
    parser.add_argument("--finalize-aggregate", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--slack-ts", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.finalize_aggregate:
        if not args.slack_ts:
            parser.error("--finalize-aggregate requires --slack-ts")
        return finalize_aggregate(args.finalize_aggregate, args.slack_ts)
    if not args.source_manifest:
        parser.error("--source-manifest is required")
    source, data = source_manifest(args.source_manifest)
    if not args.apply:
        return run_deterministic(source, data, set(args.issues or []) or None, False)
    lease = RepairLease(run_id=now_id(), source=source, candidates=len(candidate_rows(data, set(args.issues or []) or None)),
                        current=int(data.get("current_ready_pool_count") or 0), deficit=int(data.get("promotion_deficit") or 0))
    if not lease.acquire():
        print("READY_POOL_CONTRACT_REPAIR_ALREADY_RUNNING")
        return 2
    lease.start()
    try:
        code = run_deterministic(source, data, set(args.issues or []) or None, True)
        return code
    finally:
        lease.finish(status="stopped", aggregate=source)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (RepairError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"READY_POOL_CONTRACT_REPAIR_DEGRADED\nerror={exc}", file=sys.stderr)
        raise SystemExit(2)
