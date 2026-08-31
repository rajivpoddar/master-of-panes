#!/usr/bin/env python3
"""Deterministic Sakshi heartbeat for HeyDonna PM ops.

This script owns hard-gate checks that should not depend on an LLM agent:
session age, MoP health/slot state, process sweep, basic PR-label drift, and
queue-motion signals. External checks may be UNKNOWN, but PM/S1-S6 session-age
rows must always be present before a report can claim clean state.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import hashlib
import shutil
import stat
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

if __package__:
    from .runtime_observation import RuntimeObservationAdapter, parse_timestamp
else:  # direct installed-script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from control_plane.runtime_observation import RuntimeObservationAdapter, parse_timestamp


IST = ZoneInfo("Asia/Kolkata")
OMP_SESSIONS_ROOT = Path(
    os.environ.get("HEYDONNA_OMP_SESSIONS_ROOT", str(Path.home() / ".omp/sessions"))
)
PROJECT_ROOT = Path("/Users/rajiv/Downloads/projects/heydonna-app")
AXIOM_SCRIPT = PROJECT_ROOT / "scripts/pm/axiom-activity-report.py"
PM_OPS = Path("/Users/rajiv/.claude/scripts/pm-ops.py")
PM_OPS_DB = Path(
    "/Users/rajiv/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db"
)
PR_STATE_SWEEP = Path(
    os.environ.get(
        "HEYDONNA_PR_STATE_SWEEP",
        "/Users/rajiv/.claude/skills/pr-state-sweep/scripts/sweep.sh",
    )
)
SLACK_SEND = Path("/Users/rajiv/.claude/scripts/slack-send.sh")
READY_POOL_AUTHORITY = [
    "gh", "issue", "list", "--repo", "heydonna-app/heydonna-app", "--state", "open",
    "--label", "status:todo", "--limit", "1000", "--json", "number,title,body,labels",
]
READY_POOL_AUDIT_DIR = Path(
    os.environ.get("READY_POOL_AUDIT_DIR", "/tmp")
)
LEGACY_READY_POOL_COMMAND = "/Users/rajiv/.claude/scripts/backlog-triage.py audit-ready-pool"
SUPPORTED_READY_POOL_COMMAND = "/Users/rajiv/.claude/scripts/sakshi-heartbeat.py --ready-pool-audit"

AXIOM_API_URL = "https://api.axiom.co/v1/datasets/_apl?format=legacy"
AXIOM_DATASET = "heydonna-logs"
AXIOM_PROD_FILTER = (
    "| where ['slot'] == \"prod\" "
    "or ['convex.deployment_type'] == \"prod\" "
    "or (['source'] == \"app\" and ['heydonna_env'] == \"production\")"
)

# Read-only open-PR execution audit.  This deliberately treats labels, holds,
# historical runs, skipped shells, and queued jobs without a runner as no
# execution lane; only exact-head work that is demonstrably executing counts.
OPEN_PR_AUDIT_REPOSITORY = "heydonna-app/heydonna-app"
OPEN_PR_AUDIT_MIN_QUEUED_SECONDS = 15 * 60
OPEN_PR_AUDIT_WORKFLOWS = {"CI", "E2E Smoke Tests"}
OPEN_PR_AUDIT_CAPTURE_WORKFLOW_MARKERS = ("capture", "llm proxy")
OPEN_PR_AUDIT_ACTIVE_WORDS = re.compile(r"\b(repro|reproduction|integration|proof|capture|e2e|ci|test)\b", re.I)
OPEN_PR_MOTION_STATES = (
    "CI_IN_PROGRESS",
    "CAPTURE_IN_PROGRESS",
    "REPRO_OR_PROOF_IN_PROGRESS",
    "REWORK_IN_PROGRESS",
    "REWORK_BLOCKED",
    "DEPENDENCY_BLOCKED",
    "PROCESS_LIMBO",
)
OPEN_PR_CONCRETE_TOKEN = re.compile(r"^[^\s]{2,}$")
OPEN_PR_HEAD = re.compile(r"^[0-9a-f]{40}$")
OPEN_PR_LANES = (
    "CI",
    "capture",
    "repro/proof",
    "rework",
    "rework-blocked",
    "dependency-blocked",
    "true limbo",
)
CONTINUATION_HEAD_KEYS = (
    "head",
    "head_sha",
    "headRefOid",
    "current_head",
    "current_head_sha",
)
CONTINUATION_KIND_LANES = {
    "ci_watch": "CI",
    "pr_admission": "CI",
    "capture_release": "capture",
    "capture_recovery": "capture",
    "pr_qa_pending": "repro/proof",
    "slot_ready_pending": "repro/proof",
    "slot_retask": "rework",
    "slot_rework": "rework",
    "dependency_wait": "dependency-blocked",
    "infra_blocker": "dependency-blocked",
    "rework": "rework-blocked",
    "rework_review": "rework-blocked",
    "ci_rework": "rework-blocked",
    "control_plane_defect": "rework-blocked",
    "followup": "rework-blocked",
}


def _default_heartbeat_skill() -> Path:
    """Resolve the canonical heartbeat skill for launch-prompt generation.

    Prefer the installed skill target (deploy verifies source/install parity)
    so the installed runtime and the generated prompt stay consistent even if
    the live checkout is behind the pushed main. Fall back to the tracked
    source path in a fresh checkout.
    """

    installed = PROJECT_ROOT / ".claude/skills/heartbeat-tasks/SKILL.md"
    if installed.is_file():
        return installed
    return PROJECT_ROOT / "scripts/pm/control-plane/skills/heartbeat-tasks/SKILL.md"


HEARTBEAT_SKILL = Path(
    os.environ.get("HEYDONNA_HEARTBEAT_SKILL") or _default_heartbeat_skill()
)
OUT_JSON = Path("/tmp/sakshi-heartbeat.json")
OUT_TEXT = Path("/tmp/sakshi-heartbeat.txt")

CONTROL_PLANE_HOURS = 3
CONTROL_PLANE_PENDING_LIMIT = 8
CONTROL_PLANE_PATHS = ("scripts/pm", "scripts/ci")

SESSIONS = [
    {"id": "pm", "label": "PM", "pane": 0, "project": "-Users-rajiv-Downloads-projects-heydonna-app"},
    {"id": "1", "label": "S1", "pane": 1, "project": "-Users-rajiv-Downloads-projects-heydonna-app-3001"},
    {"id": "2", "label": "S2", "pane": 2, "project": "-Users-rajiv-Downloads-projects-heydonna-app-3002"},
    {"id": "3", "label": "S3", "pane": 3, "project": "-Users-rajiv-Downloads-projects-heydonna-app-3003"},
    {"id": "4", "label": "S4", "pane": 4, "project": "-Users-rajiv-Downloads-projects-heydonna-app-3004"},
    {"id": "5", "label": "S5", "pane": 5, "project": "-Users-rajiv-Downloads-projects-heydonna-app-3005"},
    {"id": "6", "label": "S6", "pane": 6, "project": "-Users-rajiv-Downloads-projects-heydonna-app-3006"},
]


@dataclass
class CmdResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int


def run_cmd(args: list[str], *, cwd: Path | None = None, timeout: int = 12, input_text: str | None = None, env: dict[str, str] | None = None) -> CmdResult:
    try:
        completed = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
        return CmdResult(completed.returncode == 0, completed.stdout, completed.stderr, completed.returncode)
    except FileNotFoundError as exc:
        return CmdResult(False, "", str(exc), 127)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return CmdResult(False, stdout, f"timeout after {timeout}s\n{stderr}".strip(), 124)


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return "unknown"
    return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")


def fmt_age(seconds: float | int | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def latest_omp_session(slot_id: str) -> tuple[Path, datetime, datetime] | None:
    """Compatibility wrapper over the canonical OMP observation adapter."""

    return RuntimeObservationAdapter(omp_sessions_root=OMP_SESSIONS_ROOT).latest_omp_session(slot_id)


def analyze_session(
    entry: dict[str, Any],
    now_utc: datetime,
    *,
    mop_row: dict[str, Any] | None = None,
    mop_events: dict[str, Any] | None = None,
) -> dict[str, Any]:
    is_omp_session = entry["id"] in {"pm", "1", "2", "3", "4", "5", "6"}
    omp_dir = OMP_SESSIONS_ROOT / f"heydonna-slot{entry['id']}"
    if entry["id"] == "pm":
        omp_dir = OMP_SESSIONS_ROOT / "heydonna-pm"
    configured_source = entry.get("runtime_source")
    if isinstance(configured_source, str):
        runtime_source = configured_source
        runtime_project_dir = None
    else:
        runtime_source, runtime_project_dir = RuntimeObservationAdapter.detect_runtime_identity(
            str(entry["id"])
        )
    adapter = RuntimeObservationAdapter(
        omp_sessions_root=OMP_SESSIONS_ROOT,
        runtime_source=runtime_source,
        runtime_project_dir=runtime_project_dir,
        session_age_activity=True,
    )
    observation = adapter.observe_slot(
        entry["id"],
        mop_row=mop_row if is_omp_session else None,
        mop_events=mop_events,
        now=now_utc,
    )
    result: dict[str, Any] = {
        "id": entry["id"],
        "label": entry["label"],
        "pane": entry["pane"],
        "project_dir": str(Path(observation.session_path).parent) if observation.session_path else str(omp_dir),
        "source": observation.source,
        "jsonl": observation.session_path,
        "present": observation.session_path is not None,
        "first_timestamp": observation.session_start.isoformat() if observation.session_start else None,
        "last_timestamp": observation.latest_record.isoformat() if observation.latest_record else None,
        "last_clear": observation.clear_event_at.isoformat() if observation.clear_event_at else None,
        "omp_session_start": observation.session_start.isoformat() if is_omp_session and observation.session_start else None,
        "omp_latest_record": observation.latest_record.isoformat() if is_omp_session and observation.latest_record else None,
        "last_compact": None,
        "compact_count_last_4h": 0,
        "age_seconds": (now_utc - observation.effective_start).total_seconds() if observation.effective_start else None,
        "age": fmt_age((now_utc - observation.effective_start).total_seconds()) if observation.effective_start else "unknown",
        "effective_start": observation.effective_start.isoformat() if observation.effective_start else None,
        "severity": session_age_severity((now_utc - observation.effective_start).total_seconds()) if observation.effective_start else "unknown",
        "clear_due": observation.clear_due,
        "clear_reason": None,
        "error": observation.error,
        "active_turn_id": observation.active_turn_id,
        "active_turn_state": observation.active_turn_state,
        "active": observation.active,
        "mop_occupied": observation.occupied,
        "mop_idle": observation.idle,
        "checkout_clean": observation.checkout_clean,
        "checkout_head": observation.checkout_head,
        "handoff_ready": observation.handoff_ready,
    }
    return result


def session_age_severity(age_seconds: float | None) -> str:
    if age_seconds is None:
        return "unknown"
    hours = age_seconds / 3600
    if hours > 12:
        return "critical"
    if hours > 6:
        return "warning"
    return "clean"


def http_json(path: str, timeout: float = 3.0) -> dict[str, Any]:
    url = f"http://127.0.0.1:3100{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return {"ok": True, "url": url, "json": json.loads(body)}
    except Exception as exc:
        return {"ok": False, "url": url, "error": str(exc)}


def collect_mop() -> dict[str, Any]:
    return {
        "health": http_json("/health"),
        "ready": http_json("/ready"),
        "slots": http_json("/slots"),
        "recent_cleared": http_json("/events?limit=100&type=slot_cleared"),
        "recent_clear_executed": http_json("/events?limit=100&type=clear_pending_executed"),
        # Retain the queued view for diagnostics, but never use it as a
        # successful clear or effective-start source.
        "recent_clear_pending": http_json("/events?limit=100&type=clear_pending_queued"),
    }


def slot_state_map(mop: dict[str, Any]) -> dict[str, dict[str, Any]]:
    slots_resp = mop.get("slots", {})
    if not slots_resp.get("ok"):
        return {}
    payload = slots_resp.get("json") or {}
    raw_slots = payload.get("slots")
    if not isinstance(raw_slots, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for slot in raw_slots:
        if not isinstance(slot, dict):
            continue
        slot_id = slot.get("id") or slot.get("slot") or slot.get("slotId")
        if slot_id is None:
            continue
        result[str(slot_id)] = slot
    return result


SESSION_CLEAR_THRESHOLD_HOURS = 3


def apply_clear_policy(sessions: list[dict[str, Any]], slots: dict[str, dict[str, Any]]) -> None:
    for row in sessions:
        row["pm_clear_candidate"] = False
        if row.get("clear_due"):
            row["clear_due"] = True
            row["clear_reason"] = (
                f">={SESSION_CLEAR_THRESHOLD_HOURS}h session age; "
                "session-age-clear owns logged MoP clear"
            )
            row["pm_clear_candidate"] = True


def update_omp_effective_starts(
    sessions: list[dict[str, Any]], mop: dict[str, Any], now_utc: datetime
) -> None:
    adapter = RuntimeObservationAdapter(
        omp_sessions_root=OMP_SESSIONS_ROOT,
    )
    successful_clear_events = {
        key: (mop.get(key, {}).get("json") or {})
        for key in ("recent_cleared", "recent_clear_executed")
        if mop.get(key, {}).get("ok")
    }
    for row in sessions:
        if row.get("source") != "omp_top_level":
            continue
        slot = str(row.get("id") or row.get("pane") or "")
        clear = adapter._successful_clear(slot, successful_clear_events)
        if not clear:
            continue
        clear_timestamp, event_type = clear
        session_timestamp = parse_ts(row.get("omp_latest_record")) or parse_ts(row.get("omp_session_start"))
        effective = max((value for value in (session_timestamp, clear_timestamp) if value), default=None)
        row["last_clear"] = clear_timestamp.isoformat()
        row["clear_event_type"] = event_type
        row["effective_start"] = effective.isoformat() if effective else None
        row["age_seconds"] = (now_utc - effective).total_seconds() if effective else None
        row["age"] = fmt_age(row["age_seconds"])
        row["severity"] = session_age_severity(row["age_seconds"])
        row["clear_due"] = RuntimeObservationAdapter.clear_due_for(
            effective,
            occupied=row.get("mop_occupied"),
            idle=row.get("mop_idle"),
            active=row.get("active"),
            is_pm=str(row.get("id") or "") == "pm",
            now=now_utc,
        )


def mark_recent_clear_requests(sessions: list[dict[str, Any]], mop: dict[str, Any], now_utc: datetime) -> None:
    recent_by_slot: dict[str, dict[str, str]] = {}
    cutoff = now_utc - timedelta(hours=4)
    for key in ["recent_cleared", "recent_clear_executed"]:
        response = mop.get(key, {})
        if not response.get("ok"):
            continue
        for event in (response.get("json") or {}).get("events", []):
            if not isinstance(event, dict):
                continue
            ts = parse_ts(event.get("timestamp"))
            if not ts or ts < cutoff:
                continue
            slot = str(event.get("slot"))
            payload = event.get("payload")
            via = ""
            if isinstance(payload, str):
                try:
                    via = str((json.loads(payload) or {}).get("via") or "")
                except json.JSONDecodeError:
                    via = payload
            recent_by_slot[slot] = {
                "event_type": str(event.get("event_type") or key),
                "timestamp": ts.isoformat(),
                "via": via,
            }

    for row in sessions:
        slot = "0" if row["id"] == "pm" else str(row["id"])
        row["clear_already_requested"] = False
        if row.get("clear_due") and slot in recent_by_slot:
            row["clear_already_requested"] = True
            row["clear_recent_event"] = recent_by_slot[slot]
            row["pm_clear_candidate"] = False


def capture_tmux() -> dict[str, Any]:
    panes: dict[str, Any] = {}
    for pane in range(0, 5):
        target = f"0:0.{pane}"
        res = run_cmd(["tmux", "capture-pane", "-t", target, "-p", "-S", "-8"], timeout=4)
        text = res.stdout[-4000:] if res.stdout else ""
        panes[str(pane)] = {
            "target": target,
            "ok": res.ok,
            "error": res.stderr.strip() if not res.ok else None,
            "stuck_on_prompt": "Would you like to proceed?" in text,
            "tail": "\n".join(text.splitlines()[-8:]),
        }
    return panes


def parse_etime_to_seconds(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    days = 0
    if "-" in value:
        day_part, _, value = value.partition("-")
        try:
            days = int(day_part)
        except ValueError:
            return None
    parts = value.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 3:
        hours, minutes, seconds = nums
    elif len(nums) == 2:
        hours, minutes, seconds = 0, nums[0], nums[1]
    else:
        return None
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def process_sweep() -> dict[str, Any]:
    res = run_cmd(["ps", "-axo", "pid=,etime=,command="], timeout=8)
    flagged: list[dict[str, Any]] = []
    if not res.ok:
        return {"ok": False, "error": res.stderr.strip(), "flagged": flagged}
    for line in res.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 2)
        if len(parts) < 3:
            continue
        pid, etime, command = parts
        lower = command.lower()
        if "codex computer use.app" in lower or "skycomputeruseclient" in lower:
            continue
        matches_target = (
            "next-server" in lower
            or "next dev" in lower
            or re.search(r"(^|[/\s])tsc([\s:]|$)", lower) is not None
            or re.search(r"(^|[/\s])vitest([\s:]|$)", lower) is not None
            or re.search(r"(^|[/\s])playwright([\s:]|$)", lower) is not None
            or "google chrome" in lower
            or "chrome helper" in lower
            or "chromium" in lower
        )
        if not matches_target:
            continue
        seconds = parse_etime_to_seconds(etime)
        if seconds is None or seconds < 1800:
            continue
        flagged.append(
            {
                "pid": pid,
                "elapsed_seconds": seconds,
                "elapsed": fmt_age(seconds),
                "command": command[:180],
            }
        )
    return {"ok": True, "flagged": flagged}


def _axiom_token(env_local: Path) -> str | None:
    for key in ("AXIOM_API_TOKEN", "AXIOM_QUERY_TOKEN", "AXIOM_TOKEN"):
        value = os.environ.get(key)
        if value:
            return value
    if env_local.exists():
        values: dict[str, str] = {}
        for line in env_local.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() in {"AXIOM_API_TOKEN", "AXIOM_QUERY_TOKEN", "AXIOM_TOKEN"} and value.strip():
                values[key.strip()] = value.strip()
        for key in ("AXIOM_API_TOKEN", "AXIOM_QUERY_TOKEN", "AXIOM_TOKEN"):
            if values.get(key):
                return values[key]
    return None


def query_save_debug_action_counts(
    token: str | None, start_iso: str, end_iso: str
) -> dict[str, Any]:
    """Fetch full-window counts plus privacy-safe suppression causal state."""

    actions = (
        *SAVE_SUPPRESSION_DEBUG_ACTIONS,
        "autoSaveError",
        "explicitSaveError",
        *_COMMITTED_ADMISSION_ACTIONS,
    )
    apl = (
        f"['{AXIOM_DATASET}'] {AXIOM_PROD_FILTER} "
        f"| where ['action'] in ({', '.join(repr(action) for action in actions)}) "
        "| extend causal = parse_json(['meta']) "
        "| extend releaseVersion = tostring(causal.releaseVersion), "
        "pendingSync = tostring(causal.pendingSync), "
        "pendingSyncCount = tostring(causal.pendingSyncCount), "
        "drainRequested = tostring(causal.drainRequested), "
        "producerOwner = tostring(causal.syncAttemptOwner), "
        "attemptTransport = tostring(causal.syncAttemptTransport), "
        "attemptStage = tostring(causal.syncPhase), "
        "attemptPresent = tostring(isnotempty(causal.syncAttemptId)), "
        "attemptDeadlinePresent = tostring(isnotempty(causal.syncAttemptDeadlineAt)), "
        "attemptDrainRequested = tostring(causal.syncAttemptDrainRequested), "
        "localSequencePresent = tostring(isnotempty(causal.syncLocalSequence)), "
        "admittedVersionPresent = tostring(isnotempty(causal.syncAdmittedVersion)) "
        "| summarize count() by ['action'], releaseVersion, pendingSync, "
        "pendingSyncCount, drainRequested, producerOwner, attemptTransport, "
        "attemptStage, attemptPresent, attemptDeadlinePresent, "
        "attemptDrainRequested, localSequencePresent, admittedVersionPresent"
    )
    query_sha256 = _sha256_text(apl)
    if not token:
        return {"ok": False, "reason": "axiom_token_unavailable", "query_sha256": query_sha256}
    payload = json.dumps({"apl": apl, "startTime": start_iso, "endTime": end_iso}).encode("utf-8")
    request = urllib.request.Request(
        AXIOM_API_URL,
        data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        return {"ok": False, "reason": "query_failed", "error": str(exc)[:240], "query_sha256": query_sha256}
    if not isinstance(raw, dict):
        return {"ok": False, "reason": "malformed_query_response", "query_sha256": query_sha256}
    counts: dict[str, int] = {}
    causal_fields: list[dict[str, Any]] = []
    totals = ((raw.get("buckets") or {}).get("totals") or [])
    for entry in totals:
        if not isinstance(entry, dict):
            continue
        group = entry.get("group") or {}
        action = str(group.get("action") or "")
        aggregations = entry.get("aggregations") or []
        value = next((item.get("value") for item in aggregations if isinstance(item, dict)), 0)
        if action and isinstance(value, (int, float)):
            count = int(value)
            counts[action] = counts.get(action, 0) + count
            if action in SAVE_SUPPRESSION_DEBUG_ACTIONS:
                causal_fields.append(
                    {
                        "action": action,
                        "count": count,
                        "releaseVersion": str(group.get("releaseVersion") or "") or None,
                        "pendingSync": str(group.get("pendingSync") or "") or None,
                        "pendingSyncCount": str(group.get("pendingSyncCount") or "") or None,
                        "drainRequested": str(group.get("drainRequested") or "") or None,
                        "producerOwner": str(group.get("producerOwner") or "") or None,
                        "attemptTransport": str(group.get("attemptTransport") or "") or None,
                        "attemptStage": str(group.get("attemptStage") or "") or None,
                        "attemptPresent": str(group.get("attemptPresent") or "") or None,
                        "attemptDeadlinePresent": str(group.get("attemptDeadlinePresent") or "") or None,
                        "attemptDrainRequested": str(group.get("attemptDrainRequested") or "") or None,
                        "localSequencePresent": str(group.get("localSequencePresent") or "") or None,
                        "admittedVersionPresent": str(group.get("admittedVersionPresent") or "") or None,
                    }
                )
    status = raw.get("status") if isinstance(raw.get("status"), dict) else {}
    return {
        "ok": True,
        "counts": counts,
        "causal_fields": causal_fields[:50],
        "isPartial": raw.get("isPartial", status.get("isPartial")),
        "rowsExamined": raw.get("rowsExamined", status.get("rowsExamined", len(totals))),
        "query_sha256": query_sha256,
    }


def collect_axiom(
    *, window_start: datetime | None = None, window_end: datetime | None = None
) -> dict[str, Any]:
    # Prefer the deploy-managed installed report (home runtime) so heartbeat
    # and deploy share the same digest; fall back to the repo copy.
    script = Path("/Users/rajiv/.claude/scripts/axiom-activity-report.py")
    if not script.exists():
        script = PROJECT_ROOT / "scripts/pm/axiom-activity-report.py"
    if not script.exists():
        return {"ok": False, "status": "UNKNOWN", "error": f"missing {script}"}
    python = "python3"
    for candidate in [PROJECT_ROOT / "venv/bin/python", PROJECT_ROOT / ".venv/bin/python"]:
        if candidate.exists():
            python = str(candidate)
            break
    # The installed report resolves its repo root from its own path
    # (~/.claude/scripts -> home), so the repo .env.local token is not found
    # automatically. Inject the Axiom token from the repo .env.local into the
    # subprocess environment so the installed report can authenticate.
    env = dict(os.environ)
    env_local = PROJECT_ROOT / ".env.local"
    if env_local.exists():
        for line in env_local.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key in {"AXIOM_API_TOKEN", "AXIOM_QUERY_TOKEN", "AXIOM_TOKEN"} and value:
                env.setdefault(key, value)
    end = window_end or datetime.now(timezone.utc)
    start = window_start or (end - timedelta(hours=CONTROL_PLANE_HOURS))
    start_iso = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    script_sha256 = hashlib.sha256(script.read_bytes()).hexdigest()
    action_counts = query_save_debug_action_counts(_axiom_token(env_local), start_iso, end_iso)
    res = run_cmd(
        [python, str(script), "--start", start_iso, "--end", end_iso, "--quiet"],
        cwd=PROJECT_ROOT,
        timeout=120,
        input_text=None,
        env=env,
    )
    if not res.ok:
        return {
            "ok": False,
            "status": "UNKNOWN",
            "python": python,
            "window": {"start_utc": start_iso, "end_utc": end_iso},
            "query": {
                "identity": "axiom-activity-report:query_save_suppression",
                "query_sha256": script_sha256,
            },
            "action_counts": action_counts,
            "error": (res.stderr or res.stdout).strip()[:1000],
        }
    text = res.stdout.strip()
    return {
        "ok": True,
        "status": "AVAILABLE",
        "python": python,
        "summary": "\n".join(text.splitlines()[:120]),
        "window": {"start_utc": start_iso, "end_utc": end_iso},
        "query": {
            "identity": "axiom-activity-report:query_save_suppression",
            "query_sha256": script_sha256,
            "isPartial": None,
            "rowsExamined": None,
        },
        "action_counts": action_counts,
    }


def collect_pr_drift() -> dict[str, Any]:
    res = run_cmd(
        ["gh", "pr", "list", "--state", "open", "--json", "number,title,isDraft,labels,updatedAt", "--limit", "80"],
        cwd=PROJECT_ROOT,
        timeout=20,
    )
    if not res.ok:
        return {"ok": False, "status": "UNKNOWN", "error": res.stderr.strip() or res.stdout.strip()}
    try:
        prs = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        return {"ok": False, "status": "UNKNOWN", "error": str(exc)}

    drift: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    now_utc = datetime.now(timezone.utc)
    for pr in prs:
        reasons = pr_drift_reasons(pr)
        labels = [label.get("name", "") for label in pr.get("labels", []) if isinstance(label, dict)]
        if reasons:
            drift.append({"number": pr.get("number"), "title": pr.get("title"), "reasons": reasons, "labels": labels})

        updated = parse_ts(pr.get("updatedAt"))
        if updated and now_utc - updated > timedelta(hours=24):
            stale.append(
                {
                    "number": pr.get("number"),
                    "title": pr.get("title"),
                    "draft": bool(pr.get("isDraft")),
                    "updated": fmt_dt(updated),
                    "age": fmt_age((now_utc - updated).total_seconds()),
                }
            )
    return {"ok": True, "drift": drift, "stale": stale, "count": len(prs)}


def pr_drift_reasons(pr: dict[str, Any]) -> list[str]:
    """Classify label-drift reasons for one open PR row.

    Intentional DRAFT capture holds are excluded from the
    "pm-blocked without pm-state:blocked-rework" drift rule ONLY when live
    GitHub proves isDraft=true and the state is exactly
    pm-state:pm-review-pending + pm-blocked:capture (PR body/canonical
    block-pr --reason capture gates readiness while the PR is still a draft).
    Arbitrary non-draft or mismatched blockers keep failing closed.
    """

    labels = [label.get("name", "") for label in pr.get("labels", []) if isinstance(label, dict)]
    has_merge_ready = "merge-ready" in labels
    raw_pm_states = [name for name in labels if name.startswith("pm-state:")]
    pm_states = [name for name in raw_pm_states if name != "pm-state:merge-ready"]
    effective_states = list(pm_states)
    if has_merge_ready:
        effective_states.append("merge-ready")
    blockers = [name for name in labels if name.startswith("pm-blocked:") or name.startswith("pm-cleanup:")]
    reasons: list[str] = []
    if "pm-state:merge-ready" in raw_pm_states:
        reasons.append("legacy pm-state:merge-ready; use merge-ready")
    if len(effective_states) == 0:
        reasons.append("missing effective PM state")
    if len(effective_states) > 1:
        reasons.append("multiple effective PM state labels")
    is_draft = bool(pr.get("isDraft"))
    intentional_draft_capture_hold = (
        is_draft
        and pm_states == ["pm-state:pm-review-pending"]
        and blockers == ["pm-blocked:capture"]
    )
    if (
        any(name.startswith("pm-blocked:") for name in blockers)
        and "pm-state:blocked-rework" not in pm_states
        and not intentional_draft_capture_hold
    ):
        reasons.append("pm-blocked without pm-state:blocked-rework")
    if "pm-cleanup:needed" in blockers and "pm-state:merged-cleanup-pending" not in pm_states:
        reasons.append("pm-cleanup without merged-cleanup-pending")
    return reasons


def _audit_gh_json(args: list[str], *, timeout: int = 20) -> tuple[Any | None, str | None]:
    """Read one GitHub JSON endpoint for the open-PR audit.

    The helper is intentionally read-only and returns a typed error instead of
    treating an unavailable endpoint as a healthy execution lane.
    """

    result = run_cmd(["gh", "api", *args], cwd=PROJECT_ROOT, timeout=timeout)
    if not result.ok:
        return None, result.stderr.strip() or result.stdout.strip() or f"gh exit {result.returncode}"
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError as exc:
        return None, f"invalid GitHub JSON: {exc}"


def _job_is_genuinely_executing(job: dict[str, Any]) -> bool:
    """Return true only for a running exact job with a bound runner."""

    status = str(job.get("status") or "").lower()
    if status not in {"in_progress", "running"}:
        return False
    runner_bound = bool(job.get("runner_id") or job.get("runner_name"))
    if not runner_bound:
        return False
    steps = job.get("steps")
    if not isinstance(steps, list) or not steps:
        return False
    return any(
        isinstance(step, dict)
        and str(step.get("status") or "").lower() in {"in_progress", "running"}
        for step in steps
    )


def _run_matches_exact_head(
    run: dict[str, Any], head: str, *, allow_capture_dispatch: bool = False
) -> bool:
    if not isinstance(run, dict) or str(run.get("head_sha") or run.get("headSha") or "") != head:
        return False
    event = str(run.get("event") or "").lower()
    return event == "pull_request" or (allow_capture_dispatch and event == "workflow_dispatch")


def _exact_open_pr_identity(
    pr: dict[str, Any],
) -> tuple[tuple[str, str, str] | None, str | None]:
    """Extract one unambiguous PR number, full head, and branch."""

    if not isinstance(pr, dict):
        return None, "open PR row is not an object"
    number = str(pr.get("number") or "").strip()
    if not number.isdigit():
        return None, "open PR number is missing or malformed"
    head_payload = pr.get("head")
    if head_payload is not None and not isinstance(head_payload, dict):
        return None, "open PR head payload is malformed"
    branch_payload = head_payload or {}

    def _consistent_exact(values: list[Any], pattern: re.Pattern[str], label: str) -> str | None:
        present = [value for value in values if value not in (None, "")]
        if not present or any(not isinstance(value, str) or not pattern.fullmatch(value) for value in present):
            return None
        if len(set(present)) != 1:
            raise ValueError(f"open PR {label} is ambiguous")
        return present[0]

    try:
        head = _consistent_exact(
            [pr.get("head_sha"), pr.get("headRefOid"), branch_payload.get("sha")],
            OPEN_PR_HEAD,
            "head",
        )
        branch = _consistent_exact(
            [pr.get("headRefName"), pr.get("branch"), branch_payload.get("ref")],
            OPEN_PR_CONCRETE_TOKEN,
            "branch",
        )
    except ValueError as exc:
        return None, str(exc)
    if not head:
        return None, "open PR head is missing or not a full 40-character SHA"
    if not branch:
        return None, "open PR branch is missing or malformed"
    return (number, head, branch), None


def _concrete_motion_token(value: Any) -> str | None:
    """Accept only explicit machine-readable owner/wake metadata."""

    if not isinstance(value, str):
        return None
    token = value.strip()
    if not OPEN_PR_CONCRETE_TOKEN.fullmatch(token):
        return None
    if token.lower() in {
        "unknown",
        "none",
        "n/a",
        "cto-owned",
        "relay-only",
        "not-actionable",
    }:
        return None
    return token


def _concrete_motion_text(value: Any) -> str | None:
    """Accept human-readable action text while rejecting placeholder authority."""

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.lower() in {
        "unknown",
        "none",
        "n/a",
        "cto-owned",
        "relay-only",
        "not-actionable",
    }:
        return None
    return text


def _continuation_head(record: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extract one exact head from durable obligation evidence only."""

    raw = record.get("evidence_json")
    if raw in (None, "", "{}"):
        return None, None
    try:
        evidence = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        return None, "durable continuation evidence is malformed"
    if not isinstance(evidence, dict):
        return None, "durable continuation evidence is not an object"
    heads: list[str] = []
    for key in CONTINUATION_HEAD_KEYS:
        if key not in evidence:
            continue
        value = evidence.get(key)
        if not isinstance(value, str) or not OPEN_PR_HEAD.fullmatch(value):
            return None, "durable continuation head is missing or malformed"
        heads.append(value)
    if len(set(heads)) > 1:
        return None, "durable continuation contains conflicting heads"
    return (heads[0] if heads else None), None


def _load_open_pr_continuations(
    pr_number: str, head: str
) -> tuple[list[dict[str, Any]], str | None]:
    """Read current exact-head continuation rows from the existing PM ledger."""

    if not pr_number.isdigit() or not OPEN_PR_HEAD.fullmatch(head):
        return [], "open-PR identity is not exact"
    if not PM_OPS_DB.is_file():
        return [], f"durable continuation authority unavailable: {PM_OPS_DB}"
    query = (
        "select id,kind,status,pr,issue,slot,owner,title,required_action,blocker,"
        "evidence_json,updated_at,created_at from obligations "
        f"where status='open' and pr={int(pr_number)} order by id desc;"
    )
    result = run_cmd(["sqlite3", "-json", str(PM_OPS_DB), query], timeout=12)
    if not result.ok:
        return [], result.stderr.strip() or result.stdout.strip() or "continuation query failed"
    try:
        rows = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        return [], f"durable continuation JSON is malformed: {exc}"
    if not isinstance(rows, list):
        return [], "durable continuation response is not a list"

    matches: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            return [], "durable continuation row is malformed"
        bound_head, error = _continuation_head(row)
        if error:
            return [], error
        if bound_head != head:
            if bound_head is None:
                return [], "durable continuation has no exact head binding"
            continue
        kind = str(row.get("kind") or "").strip()
        lane = CONTINUATION_KIND_LANES.get(kind)
        if not lane:
            return [], f"unsupported exact-head continuation kind: {kind or 'missing'}"
        row_id = str(row.get("id") or "").strip()
        if not row_id or str(row.get("pr") or "").strip() != pr_number:
            return [], "exact-head continuation has incomplete row identity"
        owner = _concrete_motion_token(row.get("owner"))
        action = _concrete_motion_text(row.get("required_action"))
        blocker = str(row.get("blocker") or "").strip()
        if not owner or not action:
            return [], "exact-head continuation has missing or placeholder owner/action"
        matches.append(
            {
                "id": row_id,
                "kind": kind,
                "lane": lane,
                "owner": owner,
                "next_action": action,
                "wake": action,
                "hold_reason": blocker or "none",
                "head": head,
            }
        )
    signatures = {
        (row["lane"], row["owner"], row["next_action"], row["hold_reason"])
        for row in matches
    }
    if len(signatures) > 1:
        return [], "contradictory exact-head durable continuation records"
    return matches[:1], None


def _continuation_motion_metadata(
    records: list[dict[str, Any]]
) -> dict[str, str] | None:
    """Normalize one already exact-head-bound durable continuation."""

    if not records:
        return None
    if any(not isinstance(record, dict) for record in records):
        return None
    signatures = {
        (
            record.get("lane"),
            record.get("owner"),
            record.get("next_action"),
            record.get("hold_reason"),
        )
        for record in records
    }
    if len(signatures) != 1:
        return None
    record = records[0]
    required = ("id", "lane", "kind", "head", "owner", "wake", "next_action", "hold_reason")
    if any(not isinstance(record.get(key), str) or not record.get(key, "").strip() for key in required):
        return None
    if not OPEN_PR_HEAD.fullmatch(record["head"]):
        return None
    if _concrete_motion_token(record["owner"]) is None:
        return None
    if _concrete_motion_text(record["wake"]) is None or _concrete_motion_text(record["next_action"]) is None:
        return None
    if record.get("next_owner") != record["owner"] or _concrete_motion_token(record.get("next_owner")) is None:
        return None
    lane = record["lane"]
    state_by_lane = {
        "CI": "CI_IN_PROGRESS",
        "capture": "CAPTURE_IN_PROGRESS",
        "repro/proof": "REPRO_OR_PROOF_IN_PROGRESS",
        "rework": "REWORK_IN_PROGRESS",
        "rework-blocked": "REWORK_BLOCKED",
        "dependency-blocked": "DEPENDENCY_BLOCKED",
    }
    if lane not in state_by_lane:
        return None
    # A durable obligation proves ownership and the next wake, not execution.
    # In particular, it must never manufacture CI/Capture progress without a
    # bound runner and active step from the workflow reader above.
    progress_lane = lane in {"CI", "capture"}
    output_lane = "true limbo" if progress_lane else lane
    output_state = "PROCESS_LIMBO" if progress_lane else state_by_lane[lane]
    hold_reason = record["hold_reason"]
    if progress_lane and hold_reason == "none":
        hold_reason = f"durable {record['kind']} exists without executing exact-head evidence"
    return {
        "lane": output_lane,
        "motion_state": output_state,
        "owner": record["owner"],
        "wake": record["wake"],
        "next_action": record["next_action"],
        "next_boundary": record["next_action"],
        "next_owner": record["owner"],
        "hold_reason": hold_reason,
        "blocker_class": hold_reason if hold_reason != "none" else record["kind"],
        "owner_source": "pm-ops.obligations",
        "workflow_motion": "none",
    }


def _numbered_motion_kind(slot: dict[str, Any]) -> str | None:
    """Classify an active exact-head numbered lane without trusting labels."""

    work_text = " ".join(
        str(slot.get(key) or "") for key in ("task", "title", "work_kind", "activity")
    )
    if re.search(r"\b(rework|implementation|repair|fix|hotfix|code)\b", work_text, re.I):
        return "rework"
    if not OPEN_PR_AUDIT_ACTIVE_WORDS.search(work_text):
        return None
    if re.search(r"\b(repro|reproduction|integration|proof|capture|e2e|ci|test)\b", work_text, re.I):
        return "repro"
    return None


def _motion_result(
    *,
    number: str,
    branch: str,
    head: str,
    motion_state: str,
    lanes: dict[str, bool],
    reasons: list[str],
    owner: str = "unowned",
    next_boundary: str = "none",
    wake: str = "none",
    last_exact: dict[str, Any] | None = None,
    status: str | None = None,
    lane: str | None = None,
    workflow_motion: str = "none",
    owner_source: str = "none",
    hold_reason: str = "none",
    next_action: str | None = None,
    next_owner: str | None = None,
) -> dict[str, Any]:
    """Return one normalized state while retaining the legacy audit fields."""

    if motion_state not in OPEN_PR_MOTION_STATES:
        raise ValueError(f"unsupported open-PR motion state: {motion_state}")
    resolved_lane = lane or {
        "CI_IN_PROGRESS": "CI",
        "CAPTURE_IN_PROGRESS": "capture",
        "REPRO_OR_PROOF_IN_PROGRESS": "repro/proof",
        "REWORK_IN_PROGRESS": "rework",
        "REWORK_BLOCKED": "rework-blocked",
        "DEPENDENCY_BLOCKED": "dependency-blocked",
        "PROCESS_LIMBO": "true limbo",
    }[motion_state]
    if resolved_lane not in OPEN_PR_LANES:
        raise ValueError(f"unsupported open-PR lane: {resolved_lane}")
    return {
        "pr": number,
        "branch": branch,
        "head": head,
        "gap": motion_state == "PROCESS_LIMBO",
        "status": status or ("active" if motion_state.endswith("_IN_PROGRESS") else "gap"),
        "motion_state": motion_state,
        "lanes": lanes,
        "reasons": list(dict.fromkeys(reasons)),
        "owner": owner,
        "lane": resolved_lane,
        "workflow_motion": workflow_motion,
        "owner_source": owner_source,
        "hold_reason": hold_reason,
        "next_action": next_action or next_boundary,
        "next_owner": next_owner or owner,
        "next_boundary": next_boundary,
        "wake": wake,
        "last_exact": last_exact,
    }


def evaluate_open_pr_activity(
    pr: dict[str, Any],
    runs: list[dict[str, Any]],
    jobs_by_run: dict[str, list[dict[str, Any]]],
    slots: dict[str, dict[str, Any]],
    *,
    now_utc: datetime,
    continuation_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evaluate execution lanes for one open PR using exact-head evidence.

    This pure evaluator is the safety boundary used by both the live reader
    and focused production-shaped tests.  It never infers activity from
    labels, holds, historical success, or an idle slot claim.
    """

    continuation_records = continuation_records or []
    number = str(pr.get("number") or "")
    head = str(pr.get("headRefOid") or pr.get("head_sha") or "")
    branch = str(pr.get("headRefName") or pr.get("branch") or "")
    if not number or not re.fullmatch(r"[0-9a-f]{40}", head) or not branch:
        return _motion_result(
            number=number or "?",
            branch=branch,
            head=head,
            motion_state="PROCESS_LIMBO",
            status="unknown",
            lanes={"capture": False, "ci_e2e": False, "numbered_reproduction": False},
            reasons=["missing exact open-PR head or branch binding"],
            next_boundary="re-read open PR metadata with an exact 40-character head",
            wake="re-read exact-head open-PR metadata",
        )

    # The live reader supplies records only after the durable join has bound
    # them to this head. Keep the evaluator equally safe for callers/tests
    # that pass normalized records directly: stale records never become a
    # current owner, while malformed exact-head records are rejected by the
    # authoritative reader before this point.
    continuation_records = [
        record
        for record in continuation_records
        if isinstance(record, dict) and record.get("head") == head
    ]

    capture = False
    ci_e2e = False
    active_workflow_motion: list[str] = []
    last_exact: dict[str, Any] | None = None
    for run in runs:
        workflow = str(run.get("workflowName") or run.get("name") or "")
        is_capture_workflow = any(marker in workflow.lower() for marker in OPEN_PR_AUDIT_CAPTURE_WORKFLOW_MARKERS)
        if not _run_matches_exact_head(run, head, allow_capture_dispatch=is_capture_workflow):
            continue
        run_id = str(run.get("databaseId") or run.get("id") or "")
        jobs = jobs_by_run.get(run_id, [])
        executing = any(_job_is_genuinely_executing(job) for job in jobs)
        if executing and workflow in OPEN_PR_AUDIT_WORKFLOWS:
            ci_e2e = True
            active_workflow_motion.append(workflow)
        if executing and is_capture_workflow:
            capture = True
            active_workflow_motion.append(workflow)
        if jobs:
            latest = dict(run)
            latest_job = max(
                (job for job in jobs if isinstance(job, dict)),
                key=lambda job: str(job.get("started_at") or job.get("completed_at") or ""),
                default={},
            )
            if latest_job:
                latest["job_id"] = latest_job.get("id") or latest_job.get("databaseId")
            created = parse_ts(run.get("created_at") or run.get("createdAt"))
            if last_exact is None or (
                created and parse_ts(last_exact.get("created_at") or last_exact.get("createdAt"))
                and created > parse_ts(last_exact.get("created_at") or last_exact.get("createdAt"))
            ):
                last_exact = latest

    numbered_kind: str | None = None
    owner = "unowned"
    active_numbered_owners: list[tuple[str | None, str]] = []
    for slot_id, slot in slots.items():
        if not isinstance(slot, dict):
            continue
        slot_pr = str(slot.get("pr") or slot.get("pull_request") or "")
        slot_head = str(slot.get("head_sha") or slot.get("headSha") or "")
        if slot_pr == number and slot_head == head:
            owner = str(slot.get("owner") or slot.get("name") or f"S{slot_id}")
        active_state = str(slot.get("active_turn_state") or slot.get("state") or "").lower()
        active = bool(slot.get("occupied")) and (
            active_state in {"active", "running", "working", "in_progress"}
            and bool(str(slot.get("active_turn_id") or "").strip())
        )
        if active and slot_pr == number and slot_head == head:
            kind = _numbered_motion_kind(slot)
            active_numbered_owners.append((kind, owner))

    if len(active_numbered_owners) == 1:
        numbered_kind, owner = active_numbered_owners[0]
    elif len(active_numbered_owners) > 1:
        numbered_kind = "ambiguous"
        owner = ",".join(sorted({active_owner for _, active_owner in active_numbered_owners}))

    lanes = {
        "capture": capture,
        "ci_e2e": ci_e2e,
        "numbered_reproduction": numbered_kind == "repro",
    }
    active_states = [
        state
        for state, enabled in (
            ("CAPTURE_IN_PROGRESS", capture),
            ("CI_IN_PROGRESS", ci_e2e),
            ("REPRO_OR_PROOF_IN_PROGRESS", numbered_kind == "repro"),
            ("REWORK_IN_PROGRESS", numbered_kind == "rework"),
        )
        if enabled
    ]
    if numbered_kind == "ambiguous" or len(active_states) > 1:
        return _motion_result(
            number=number,
            branch=branch,
            head=head,
            motion_state="PROCESS_LIMBO",
            lanes={key: bool(value) for key, value in lanes.items()},
            reasons=["multiple incompatible exact-head active lanes"],
            owner=owner,
            owner_source="workflow+slot",
            workflow_motion=",".join(dict.fromkeys(active_workflow_motion)) or "ambiguous",
            hold_reason="multiple incompatible exact-head active lanes",
            next_action="reconcile one exact-head active lane",
            next_owner="CTO",
            next_boundary="reconcile one exact-head active lane",
            wake="re-read exact-head workflow and slot evidence",
            last_exact=last_exact,
        )
    if len(active_states) == 1:
        return _motion_result(
            number=number,
            branch=branch,
            head=head,
            motion_state=active_states[0],
            lanes={key: bool(value) for key, value in lanes.items()},
            reasons=[],
            owner=owner,
            owner_source="slot" if numbered_kind else "workflow",
            workflow_motion=",".join(dict.fromkeys(active_workflow_motion)) or "slot-only",
            next_action="await the exact-head lane terminal",
            next_owner="CTO" if numbered_kind is None else owner,
            last_exact=last_exact,
        )

    blocked = _continuation_motion_metadata(continuation_records)
    if blocked:
        return _motion_result(
            number=number,
            branch=branch,
            head=head,
            motion_state=blocked["motion_state"],
            lanes={key: bool(value) for key, value in lanes.items()},
            reasons=[f"explicit blocker={blocked['blocker_class']}"],
            owner=blocked["owner"],
            next_boundary=blocked["next_boundary"],
            wake=blocked["wake"],
            lane=blocked["lane"],
            owner_source=blocked["owner_source"],
            workflow_motion=blocked["workflow_motion"],
            hold_reason=blocked["hold_reason"],
            next_action=blocked["next_action"],
            next_owner=blocked["next_owner"],
            last_exact=last_exact,
            status="blocked",
        )

    reasons = ["no genuinely executing exact-head lane"]
    for run in runs:
        workflow = str(run.get("workflowName") or run.get("name") or "")
        is_capture_workflow = any(marker in workflow.lower() for marker in OPEN_PR_AUDIT_CAPTURE_WORKFLOW_MARKERS)
        if not _run_matches_exact_head(run, head, allow_capture_dispatch=is_capture_workflow):
            continue
        run_status = str(run.get("status") or "").lower()
        conclusion = str(run.get("conclusion") or "").lower()
        run_id = str(run.get("databaseId") or run.get("id") or "")
        jobs = jobs_by_run.get(run_id, [])
        if run_status in {"queued", "requested", "waiting"} and not any(_job_is_genuinely_executing(job) for job in jobs):
            created = parse_ts(run.get("created_at") or run.get("createdAt"))
            if created and (now_utc - created).total_seconds() >= OPEN_PR_AUDIT_MIN_QUEUED_SECONDS:
                reasons.append("queued exact-head run has no bound executing runner for >=15m")
        if conclusion in {"skipped", "cancelled", "failure", "timed_out"}:
            reasons.append(f"historical/non-executing exact-head run ignored ({conclusion or run_status})")
    reasons = list(dict.fromkeys(reasons))
    return _motion_result(
        number=number,
        branch=branch,
        head=head,
        motion_state="PROCESS_LIMBO",
        lanes={key: bool(value) for key, value in lanes.items()},
        reasons=reasons,
        owner=owner,
        lane="true limbo",
        owner_source="none",
        workflow_motion="none",
        hold_reason="no exact durable continuation or active workflow/slot",
        next_action="CTO consumes this exact-head limbo row",
        next_owner="CTO",
        next_boundary="start one supported exact-head execution lane after CTO review",
        wake="CTO/PM consumes this exact-head limbo row",
        last_exact=last_exact,
    )


def collect_open_pr_activity_audit(slots: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Read-only exact-head activity audit for every open PR."""

    prs, error = _audit_gh_json(
        [
            f"repos/{OPEN_PR_AUDIT_REPOSITORY}/pulls",
            "--method", "GET", "-f", "state=open", "-f", "per_page=100",
        ]
    )
    if error or not isinstance(prs, list):
        return {
            "ok": False,
            "status": "unknown",
            "error": error or "open PR response is not a list",
            "gaps": [],
            "open_pr_activity_gaps": None,
            "counts": {"capture": 0, "ci_e2e": 0, "numbered_reproduction": 0},
            "motion_states": {state: 0 for state in OPEN_PR_MOTION_STATES},
        }

    rows: list[dict[str, Any]] = []
    counts = {"capture": 0, "ci_e2e": 0, "numbered_reproduction": 0}
    motion_states = {state: 0 for state in OPEN_PR_MOTION_STATES}
    now_utc = datetime.now(timezone.utc)
    for pr in prs:
        if not isinstance(pr, dict):
            row = {
                "pr": "?",
                "gap": True,
                "status": "unknown",
                "motion_state": "PROCESS_LIMBO",
                "reasons": ["malformed open PR row"],
                "missing_predicates": ["open PR identity"],
                "next_boundary": "re-read open PR metadata",
                "wake": "re-read open PR metadata",
                "owner": "unowned",
                "lane": "true limbo",
                "workflow_motion": "none",
                "owner_source": "none",
                "hold_reason": "malformed open PR row",
                "next_action": "re-read open PR metadata",
                "next_owner": "CTO",
            }
            rows.append(row)
            motion_states["PROCESS_LIMBO"] += 1
            continue
        identity, identity_error = _exact_open_pr_identity(pr)
        if identity_error:
            row = {
                "pr": str(pr.get("number") or "?") if isinstance(pr, dict) else "?",
                "branch": "",
                "head": "",
                "gap": True,
                "status": "unknown",
                "motion_state": "PROCESS_LIMBO",
                "lanes": {"capture": False, "ci_e2e": False, "numbered_reproduction": False},
                "reasons": [identity_error],
                "missing_predicates": [identity_error],
                "next_boundary": "re-read unambiguous open PR metadata",
                "wake": "re-read unambiguous open PR metadata",
                "owner": "unowned",
                "lane": "true limbo",
                "workflow_motion": "none",
                "owner_source": "none",
                "hold_reason": identity_error,
                "next_action": "re-read unambiguous open PR metadata",
                "next_owner": "CTO",
            }
            rows.append(row)
            motion_states["PROCESS_LIMBO"] += 1
            continue
        assert identity is not None
        number, head, branch = identity
        runs, run_error = _audit_gh_json(
            [
            f"repos/{OPEN_PR_AUDIT_REPOSITORY}/actions/runs",
                "--method", "GET", "-f", "per_page=100",
            ]
        )
        if run_error or not isinstance(runs, dict) or not isinstance(runs.get("workflow_runs"), list):
            row = {
                "pr": number or "?",
                "branch": branch,
                "head": head,
                "gap": True,
                "status": "unknown",
                "motion_state": "PROCESS_LIMBO",
                "reasons": ["exact-head workflow run read unavailable"],
                "missing_predicates": ["workflow run evidence"],
                "next_boundary": "re-read exact-head workflow evidence",
                "wake": "re-read exact-head workflow evidence",
                "owner": "unowned",
                "lane": "true limbo",
                "workflow_motion": "none",
                "owner_source": "none",
                "hold_reason": "exact-head workflow run read unavailable",
                "next_action": "re-read exact-head workflow evidence",
                "next_owner": "CTO",
            }
            rows.append(row)
            motion_states["PROCESS_LIMBO"] += 1
            continue
        exact_runs = [run for run in runs["workflow_runs"] if isinstance(run, dict) and str(run.get("head_sha") or "") == head]
        jobs_by_run: dict[str, list[dict[str, Any]]] = {}
        jobs_errors: list[str] = []
        for run in exact_runs:
            run_id = str(run.get("id") or "")
            if not run_id:
                continue
            jobs, jobs_error = _audit_gh_json([f"repos/{OPEN_PR_AUDIT_REPOSITORY}/actions/runs/{run_id}/jobs", "--method", "GET", "-f", "per_page=100"])
            if jobs_error or not isinstance(jobs, dict) or not isinstance(jobs.get("jobs"), list):
                jobs_by_run[run_id] = []
                jobs_errors.append(f"run {run_id}: {jobs_error or 'jobs response is not a list'}")
            else:
                jobs_by_run[run_id] = [job for job in jobs["jobs"] if isinstance(job, dict)]
        if jobs_errors:
            rows.append({
                "pr": number or "?",
                "branch": branch,
                "head": head,
                "gap": True,
                "status": "unknown",
                "motion_state": "PROCESS_LIMBO",
                "lanes": {"capture": False, "ci_e2e": False, "numbered_reproduction": False},
                "reasons": ["exact-head job evidence unavailable: " + "; ".join(jobs_errors)],
                "missing_predicates": ["workflow job evidence"],
                "next_boundary": "re-read exact-head workflow job evidence",
                "wake": "re-read exact-head workflow job evidence",
                "owner": "unowned",
                "lane": "true limbo",
                "workflow_motion": "none",
                "owner_source": "none",
                "hold_reason": "exact-head job evidence unavailable",
                "next_action": "re-read exact-head workflow job evidence",
                "next_owner": "CTO",
            })
            motion_states["PROCESS_LIMBO"] += 1
            continue
        continuations, continuation_error = _load_open_pr_continuations(number, head)
        if continuation_error:
            return {
                "ok": False,
                "status": "unknown",
                "error": f"PR #{number}: {continuation_error}",
                "open_pr_count": len(prs),
                "open_pr_activity_gaps": None,
                "gaps": [],
                "rows": [],
                "counts": counts,
                "motion_states": motion_states,
            }
        normalized_pr = {**pr, "head_sha": head, "headRefName": branch}
        row = evaluate_open_pr_activity(
            normalized_pr,
            exact_runs,
            jobs_by_run,
            slots,
            now_utc=now_utc,
            continuation_records=continuations,
        )
        for lane, enabled in row.get("lanes", {}).items():
            if enabled:
                counts[lane] += 1
        motion_state = row.get("motion_state")
        if motion_state in motion_states:
            motion_states[motion_state] += 1
        if motion_state == "PROCESS_LIMBO":
            row["missing_predicates"] = list(row.get("reasons") or [])
        rows.append(row)
    if len(rows) != len(prs):
        return {
            "ok": False,
            "status": "unknown",
            "error": "open PR audit row-count mismatch",
            "open_pr_count": len(prs),
            "open_pr_activity_gaps": None,
            "gaps": [],
            "rows": rows,
            "counts": counts,
            "motion_states": motion_states,
        }
    identities = []
    for pr in prs:
        identity, identity_error = _exact_open_pr_identity(pr) if isinstance(pr, dict) else (None, "malformed")
        if identity_error or identity is None:
            continue
        identities.append(identity[0])
    if len(identities) != len(set(identities)):
        return {
            "ok": False,
            "status": "unknown",
            "error": "ambiguous duplicate open PR identity",
            "open_pr_count": len(prs),
            "open_pr_activity_gaps": None,
            "gaps": [],
            "rows": rows,
            "counts": counts,
            "motion_states": motion_states,
        }
    gaps = [row for row in rows if row.get("gap")]
    return {
        "ok": True,
        "status": "ok",
        "open_pr_count": len(rows),
        "open_pr_activity_gaps": len(gaps),
        "gaps": gaps,
        "rows": rows,
        "counts": counts,
        "motion_states": motion_states,
    }


def _write_ready_pool_audit_artifact(payload: dict[str, Any]) -> str | None:
    """Persist one bounded, deterministic read-only audit artifact."""

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    receipt = _sha256_text(canonical)
    try:
        READY_POOL_AUDIT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = READY_POOL_AUDIT_DIR / f"pm-ready-pool-audit-{receipt[:16]}.json"
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        return str(destination)
    except OSError:
        return None


def _persist_ready_pool_audit(result: dict[str, Any]) -> dict[str, Any]:
    """Attach a stable receipt before writing the bounded audit artifact."""

    result["receipt"] = _sha256_text(json.dumps(result, sort_keys=True, separators=(",", ":")))
    artifact = _write_ready_pool_audit_artifact(result)
    if artifact is None:
        result["ok"] = False
        result["status"] = "unknown"
        result["error"] = "ready-pool audit artifact persistence failed"
        result["artifact"] = None
        result["receipt"] = _sha256_text(json.dumps(result, sort_keys=True, separators=(",", ":")))
    else:
        result["artifact"] = artifact
    return result


READY_POOL_REQUIRED_FIELDS = (
    "priority", "lane", "ac_summary", "claimable_slot_type", "blockers", "work_type"
)
READY_POOL_NON_CLAIMABLE = {
    "none", "no", "no-slot", "no_slot", "not-claimable", "not_claimable",
    "unclaimable", "pm", "pm-direct", "pm_direct", "rajiv", "external",
}


def _ready_pool_contract(body: Any) -> tuple[bool, str]:
    """Validate the body-derived six-field Ready Pool contract read-only."""

    text = str(body or "")
    blocks = re.findall(r"(?is)<!--\s*ready-pool:(.*?)-->", text)
    if not blocks:
        return False, "missing_ready_pool_frontmatter"
    # The supported dispatch consumer resolves the most recent block; older
    # body history is intentionally ignored rather than treated as ambiguity.
    block = blocks[-1].strip()
    metadata: dict[str, str] = {}
    if block.startswith("{"):
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            return False, "invalid_ready_pool_json"
        if not isinstance(parsed, dict):
            return False, "invalid_ready_pool_shape"
        metadata = {
            str(key).strip().lower(): "" if value is None else str(value).strip()
            for key, value in parsed.items()
        }
    else:
        for match in re.finditer(
            r"(?mi)^\s*([A-Za-z_][A-Za-z0-9_ -]*)\s*[:=]\s*(.*?)\s*$", block
        ):
            key = re.sub(r"[\s-]+", "_", match.group(1).strip().lower())
            metadata[key] = match.group(2).strip().strip("`'\"")
        for match in re.finditer(
            r"\b([A-Za-z_][A-Za-z0-9_ -]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s,]+)", block
        ):
            key = re.sub(r"[\s-]+", "_", match.group(1).strip().lower())
            metadata[key] = match.group(2).strip().strip("`'\"")
    missing = [field for field in READY_POOL_REQUIRED_FIELDS if not metadata.get(field)]
    if missing:
        return False, "missing_ready_pool_fields:" + ",".join(missing)
    claimable = metadata["claimable_slot_type"].lower()
    if claimable in READY_POOL_NON_CLAIMABLE:
        return False, f"non_claimable_slot_type:{claimable}"
    return True, ""


def collect_ready_pool_audit() -> dict[str, Any]:
    """Use the supported read-only issue authority, never the retired subcommand."""

    discovered = shutil.which("gh")
    gh_binary: str | None = None
    if discovered:
        try:
            resolved = Path(discovered).resolve(strict=True)
            resolved_stat = resolved.stat()
            if stat.S_ISREG(resolved_stat.st_mode) and os.access(resolved, os.X_OK):
                gh_binary = str(resolved)
        except (OSError, RuntimeError):
            # Dangling links, loops, and inaccessible targets are not a safe
            # authority.  Keep the audit fail-closed before invoking gh.
            gh_binary = None
    if not gh_binary:
        result: dict[str, Any] = {
            "ok": False,
            "status": "unknown",
            "error": "unsupported ready-pool authority executable: gh must resolve to a regular executable",
        }
        return _persist_ready_pool_audit(result)
    command = run_cmd([gh_binary, *READY_POOL_AUTHORITY[1:]], cwd=PROJECT_ROOT, timeout=30)
    if not command.ok:
        result = {
            "ok": False,
            "status": "unknown",
            "error": command.stderr.strip() or command.stdout.strip() or f"authority exit {command.returncode}",
        }
        return _persist_ready_pool_audit(result)
    try:
        report = json.loads(command.stdout)
    except json.JSONDecodeError as exc:
        result = {"ok": False, "status": "unknown", "error": f"invalid ready-pool authority JSON: {exc}"}
        return _persist_ready_pool_audit(result)
    if not isinstance(report, list):
        result = {"ok": False, "status": "unknown", "error": "ready-pool authority returned no issue list"}
        return _persist_ready_pool_audit(result)
    todo_rows: list[dict[str, Any]] = []
    for row in report:
        if not isinstance(row, dict):
            continue
        labels = [
            str(label.get("name") or "")
            for label in (row.get("labels") or [])
            if isinstance(label, dict)
        ]
        if "status:todo" not in labels:
            continue
        valid, reason = _ready_pool_contract(row.get("body"))
        todo_rows.append({
            "number": row.get("number"),
            "ready_pool": valid,
            "contract_reason": reason,
            "status_labels": [label for label in labels if label.startswith("status:")],
            "title": str(row.get("title") or ""),
        })
    gaps = [row for row in todo_rows if row.get("ready_pool") is not True]
    gap_numbers = [str(row.get("number")) for row in gaps if row.get("number") is not None]
    result = {
        "ok": True,
        "status": "repair_required" if gaps else "ok",
        "source": "GitHub issue list status:todo with body contract (read-only)",
        "issue_count": len(report),
        "status_todo_count": len(todo_rows),
        "gaps": gaps,
        "counts": {"status_todo": len(todo_rows), "contract_gaps": len(gaps)},
        "marker": (
            "READY_POOL_CONTRACT_REPAIR_REQUIRED issues=" + ",".join(gap_numbers)
            if gaps else None
        ),
    }
    return _persist_ready_pool_audit(result)


def format_ready_pool_audit(audit: dict[str, Any]) -> list[str]:
    """Render the bounded audit with exceptions first and UNKNOWN on source failure."""

    if not isinstance(audit, dict) or not audit.get("ok"):
        return ["*READY_POOL_AUDIT:* UNKNOWN - " + trim_text((audit or {}).get("error", "audit source unavailable"), 220)]
    gaps = audit.get("gaps") or []
    counts = audit.get("counts") or {}
    if not gaps:
        return [
            "*READY_POOL_AUDIT:* ready_pool_contract_gaps=0; "
            f"status_todo={int(counts.get('status_todo') or 0)}; "
            f"issues={int(audit.get('issue_count') or 0)}."
        ]
    lines = [
        f"*READY_POOL_AUDIT:* READY_POOL_CONTRACT_REPAIR_REQUIRED "
        f"issues={','.join(str(row.get('number')) for row in gaps)} (exceptions first)"
    ]
    for row in gaps:
        lines.append(f"- issue #{row.get('number', '?')} missing dispatchable Ready Pool metadata")
    return lines


def collect_control_plane() -> dict[str, Any]:
    """Read-only control-plane done/pending snapshot for the heartbeat report.

    DONE = control-plane commits merged to origin/main in the previous 3h
    bucket, limited to tracked control-plane paths (scripts/pm and scripts/ci).
    PENDING = the 8 newest OPEN control-plane-family obligations
    (kind LIKE 'control_plane%' OR kind='cto_hold'). This is a status summary
    only: it never resolves, creates, or mutates obligations.
    """

    done_result = run_cmd(
        [
            "git",
            "log",
            "origin/main",
            f"--since={CONTROL_PLANE_HOURS} hours ago",
            "--oneline",
            "--",
            *CONTROL_PLANE_PATHS,
        ],
        cwd=PROJECT_ROOT,
        timeout=12,
    )
    done: list[dict[str, str]] = []
    if done_result.ok:
        for line in done_result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            sha = parts[0] if parts else ""
            if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
                continue
            done.append(
                {
                    "sha": sha,
                    "subject": parts[1].strip() if len(parts) > 1 else "",
                }
            )

    pending: list[dict[str, Any]] = []
    pending_error = ""
    if not PM_OPS_DB.exists():
        pending_status = "unavailable"
        pending_error = f"pm-ops db missing: {PM_OPS_DB}"
    else:
        query = (
            "select id, kind, coalesce(pr,'') pr, coalesce(issue,'') issue, "
            "substr(title,1,70) title, datetime(created_at) created "
            "from obligations where status='open' and "
            "(kind like 'control_plane%' or kind='cto_hold') "
            "order by datetime(created_at) desc limit "
            f"{int(CONTROL_PLANE_PENDING_LIMIT)};"
        )
        pending_result = run_cmd(
            ["sqlite3", "-json", str(PM_OPS_DB), query],
            timeout=12,
        )
        if not pending_result.ok:
            pending_status = "unavailable"
            pending_error = (
                pending_result.stderr.strip() or pending_result.stdout.strip()
            )
        else:
            try:
                parsed = json.loads(pending_result.stdout or "[]")
            except json.JSONDecodeError as exc:
                pending_status = "unavailable"
                pending_error = str(exc)
            else:
                pending_status = "ok"
                for row in parsed:
                    if not isinstance(row, dict):
                        continue
                    pending.append(
                        {
                            "id": str(row.get("id") or ""),
                            "kind": str(row.get("kind") or ""),
                            "pr": str(row.get("pr") or ""),
                            "issue": str(row.get("issue") or ""),
                            "title": str(row.get("title") or ""),
                            "created": str(row.get("created") or ""),
                        }
                    )

    return {
        "status": (
            "ok"
            if done_result.ok and pending_status == "ok"
            else "unavailable"
        ),
        "source": (
            f"git log origin/main --since={CONTROL_PLANE_HOURS}h -- "
            f"{' '.join(CONTROL_PLANE_PATHS)} + pm-ops obligations open "
            "control_plane*/cto_hold newest "
            f"{int(CONTROL_PLANE_PENDING_LIMIT)}"
        ),
        "done": done,
        "done_status": "ok" if done_result.ok else "unavailable",
        "done_error": (
            ""
            if done_result.ok
            else (done_result.stderr.strip() or done_result.stdout.strip())
        ),
        "pending": pending,
        "pending_status": pending_status,
        "pending_error": pending_error,
    }


def collect_queue(slots: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Read queue motion from the authoritative PR/MoP sweep, never prose."""
    free_slots: list[str] = []
    for slot_id, slot in slots.items():
        state = str(slot.get("status") or slot.get("state") or "").lower()
        if state in {"free", "standby", "idle"} or bool(slot.get("idle")):
            free_slots.append(slot_id)

    if not PR_STATE_SWEEP.is_file():
        return {"ok": False, "error": f"missing authoritative sweep {PR_STATE_SWEEP}"}
    sweep = run_cmd(
        ["bash", str(PR_STATE_SWEEP), "--trigger=heartbeat", "--dry-run"],
        cwd=PROJECT_ROOT,
        timeout=30,
    )
    if not sweep.ok:
        return {
            "ok": False,
            "error": (
                "authoritative pr-state/rework-packet/MoP snapshot unavailable: "
                + (sweep.stderr.strip() or sweep.stdout.strip() or f"exit {sweep.returncode}")
            ),
        }

    dispatchable: list[dict[str, Any]] = []
    waiting_no_free_slot: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    now = datetime.now(timezone.utc)
    for line in sweep.stdout.splitlines():
        if "PR_REWORK_DISPATCH_REQUIRED" not in line:
            continue
        pr_match = re.search(r"\bPR#(\d+)\b", line)
        issue_match = re.search(r"\bissue=#(\d+)\b", line)
        slot_match = re.search(r"\bslot:([1-6])\b", line)
        packet_match = re.search(r"\bpacket=(\S+)", line)
        branch_match = re.search(r"\bbranch=(\S+)", line)
        head_match = re.search(r"\bhead=(\S+)", line)
        created_match = re.search(r"\bcreated_at=(\S+)", line)
        reason_match = re.search(r"\breason=(\S+)", line)
        if not pr_match or not packet_match or not reason_match:
            return {"ok": False, "error": "malformed authoritative rework dispatch row"}
        pr = pr_match.group(1)
        packet = packet_match.group(1)
        slot = slot_match.group(1) if slot_match else ""
        key = (pr, slot, packet)
        if key in seen:
            continue
        seen.add(key)
        created_at = created_match.group(1) if created_match else ""
        age_minutes = None
        parsed_created = parse_ts(created_at)
        if parsed_created:
            age_minutes = max(0, int((now - parsed_created).total_seconds() // 60))
        row: dict[str, Any] = {
            "pr": pr,
            "issue": issue_match.group(1) if issue_match else "",
            "slot": slot or None,
            "packet": packet,
            "branch": branch_match.group(1) if branch_match else "",
            "head": head_match.group(1) if head_match else "",
            "reason": reason_match.group(1),
            "created_at": created_at,
            "age_minutes": age_minutes,
            "claim_receipt": False,
            "priority": "high" if age_minutes is not None and age_minutes >= 20 else "normal",
        }
        # The sweep has already joined the authoritative MoP collection with
        # the current PR/packet state. Prefer its explicit free-slot reason
        # over the heartbeat's earlier slot map, which may have gone stale.
        if slot and "free_slot" in row["reason"]:
            if slot not in free_slots:
                free_slots.append(slot)
            dispatchable.append(row)
        else:
            waiting_no_free_slot.append(row)
    return {
        "ok": True,
        "authoritative_source": "pr-state-sweep+rework-packet-ledger+MoP",
        "queued_targeted_count": len(dispatchable),
        "free_or_idle_slots": sorted(free_slots),
        "dispatchable": dispatchable[:20],
        "packet_waiting_no_free_slot": waiting_no_free_slot[:20],
    }


def collect_cc_reports() -> dict[str, Any]:
    reports = sorted(Path("/tmp").glob("claudes-corner-*.md"))
    return {"count": len(reports), "paths": [str(p) for p in reports[:20]]}


def collect_post_issue_latches() -> dict[str, Any]:
    patterns = [
        "/tmp/*post*issue*latch*",
        "/tmp/*pending*issue*",
        "/tmp/mop-*issue*",
    ]
    matches: list[str] = []
    for pattern in patterns:
        matches.extend(str(p) for p in Path("/tmp").glob(Path(pattern).name))
    return {"count": len(sorted(set(matches))), "paths": sorted(set(matches))[:20]}


def summarize_mop(mop: dict[str, Any], slots: dict[str, dict[str, Any]]) -> str:
    health = "ok" if mop.get("health", {}).get("ok") else "UNKNOWN"
    ready = "ok" if mop.get("ready", {}).get("ok") else "UNKNOWN"
    slot_bits = []
    for slot_id in ["1", "2", "3", "4", "5", "6"]:
        slot = slots.get(slot_id, {})
        state = slot.get("status") or slot.get("state") or "unknown"
        idle = " idle" if slot.get("idle") else ""
        task = slot.get("task") or slot.get("title") or ""
        slot_bits.append(f"S{slot_id} {state}{idle}{(': ' + str(task)[:50]) if task else ''}")
    return f"MoP health {health}, ready {ready}. " + "; ".join(slot_bits)


def trim_text(value: Any, limit: int = 88) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def parse_axiom_summary(summary: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {
        "total": None,
        "error": 0,
        "info": None,
        "unlabeled": None,
        "recent_actions": [],
        "last_error_utc": None,
        "exports": None,
        "proofreads": None,
        "formats": None,
        "transcriptions": None,
        "auto_process": None,
        "active_users": None,
        "new_signups": None,
        "save_suppression": None,
    }
    number = r"([0-9][0-9,]*)"
    patterns = {
        "total": rf"^\s*Total:\s*{number}",
        "error": rf"^\s*error:\s*{number}",
        "info": rf"^\s*info:\s*{number}",
        "unlabeled": rf"^\s*\(unlabeled\):\s*{number}",
        "exports": rf"^\s*Exports:\s*{number}",
        "proofreads": rf"^\s*AI Proofreads:\s*{number}",
        "formats": rf"^\s*AI Formats:\s*{number}",
        "transcriptions": rf"^\s*Transcriptions:\s*{number}",
        "active_users": rf"^\s*Unique users with events:\s*{number}",
        "new_signups": rf"^\s*New in period:\s*{number}",
    }
    action_counts: dict[str, int] = {}
    for line in summary.splitlines():
        for key, pattern in patterns.items():
            match = re.search(pattern, line)
            if match:
                parsed[key] = int(match.group(1).replace(",", ""))
        action = re.search(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC\]\s+action=([A-Za-z0-9_:-]+)", line)
        if action:
            if parsed["last_error_utc"] is None:
                parsed["last_error_utc"] = action.group(1)[11:16]
            action_counts[action.group(2)] = action_counts.get(action.group(2), 0) + 1
    if "Auto-Process Runs" in summary and re.search(r"(?m)^\s*\(none\)\s*$", summary):
        parsed["auto_process"] = "none"
    parsed["recent_actions"] = sorted(action_counts.items(), key=lambda item: (-item[1], item[0]))[:3]
    return parsed


def parse_save_suppression_section(summary: str) -> dict[str, Any] | None:
    """Parse the dedicated `Save Suppression` section from the Axiom report.

    Returns None when the section is absent/unparseable (fail soft) or a dict
    with total, by_action, unresolved, and affected. The section is always
    rendered by the report, so absence means the report did not emit it.
    """
    lines = summary.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == "Save Suppression"), None)
    if start is None:
        return None
    total: int | None = None
    by_action: dict[str, int] = {}
    unresolved: dict[str, int] = {}
    affected: list[dict] = []
    for line in lines[start + 1:]:
        stripped = line.strip()
        if not stripped:
            continue
        if set(stripped) <= {"\u2500", "\u2550", "─", "═"}:
            continue
        if "Auto-Process Runs" in line or (not line.startswith(("  ", "\t")) and ":" not in stripped):
            break
        total_match = re.match(r"Total:\s*([0-9,]+)", stripped)
        if total_match:
            total = int(total_match.group(1).replace(",", ""))
            continue
        unresolved_match = re.match(r"Unresolved family:\s*(.+)", stripped)
        if unresolved_match:
            for part in unresolved_match.group(1).split(","):
                if "=" not in part:
                    continue
                action, _, count = part.strip().partition("=")
                unresolved[action.strip()] = int(count.replace(",", ""))
            continue
        affected_match = re.match(
            r"Affected \(top 10 by count\):",
            stripped,
        )
        if affected_match:
            continue
        row_match = re.match(
            r"(\S+)\s+([A-Za-z0-9_]+)\s+branch=(\S+)\s+x([0-9,]+)\s+(.+?)\s*\.\.\s*(.+)$",
            stripped,
        )
        if row_match:
            affected.append(
                {
                    "fileId": row_match.group(1),
                    "action": row_match.group(2),
                    "branch": row_match.group(3),
                    "count": int(row_match.group(4).replace(",", "")),
                    "first": row_match.group(5),
                    "last": row_match.group(6),
                }
            )
            continue
        action_match = re.match(r"([a-z][a-z0-9_]+):\s*([0-9,]+)", stripped)
        if action_match:
            by_action[action_match.group(1)] = int(action_match.group(2).replace(",", ""))
    if total is None:
        return None
    return {
        "total": total,
        "by_action": by_action,
        "unresolved": unresolved,
        "affected": affected,
    }


SAVE_SUPPRESSION_DEBUG_ACTIONS = (
    "save_suppressed_inflight",
    "save_suppressed_sw_lease",
    "save_suppressed_lease_held",
    "save_suppressed_sweep_busy",
    "save_suppressed_sweep_lease",
    "save_escape_unsynced",
    "autoSaveError",
    "explicitSaveError",
    "committed_but_not_durably_admitted",
)

_COMMITTED_ADMISSION_ACTIONS = (
    "committed_but_not_durably_admitted",
    "save_commit_admission_failed",
    "save_commit_not_admitted",
    "save_committed_not_durably_admitted",
)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_report_for_receipt(summary: str) -> str:
    """Remove producer-only wall-clock decoration before hashing evidence."""

    return re.sub(r"(?m)^\s*Generated:\s*.*$", "", summary).strip()


def _safe_affected_lineage(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return opaque, bounded lineage fields without customer content."""

    safe: list[dict[str, Any]] = []
    for row in rows[:10]:
        raw_id = str(row.get("fileId") or "")
        safe.append(
            {
                "file_id_sha256": _sha256_text(raw_id) if raw_id else None,
                "action": str(row.get("action") or ""),
                "branch": str(row.get("branch") or ""),
                "count": int(row.get("count") or 0),
                "first": str(row.get("first") or "") or None,
                "last": str(row.get("last") or "") or None,
            }
        )
    return safe


def build_save_suppression_prod_debug(
    summary: str,
    *,
    window_start: str | None = None,
    window_end: str | None = None,
    query_sha256: str | None = None,
    query_identity: str = "axiom-activity-report:query_save_suppression",
    is_partial: bool | None = None,
    rows_examined: int | None = None,
    full_window_counts: dict[str, Any] | None = None,
    causal_fields: list[dict[str, Any]] | None = None,
    full_window_query_sha256: str | None = None,
    source_status: str = "available",
) -> dict[str, Any]:
    """Build the stable production-debug packet consumed by the CTO wake.

    The Axiom report is the sole source.  Missing/partial report metadata is
    explicit and material; a zero window remains active_debug rather than
    silently resolving the tracked incident.
    """

    parsed = parse_save_suppression_section(summary)
    by_action = (parsed or {}).get("by_action", {}) if parsed else {}
    counts = {name: int(by_action.get(name, 0) or 0) for name in SAVE_SUPPRESSION_DEBUG_ACTIONS}
    if isinstance(full_window_counts, dict):
        for name in SAVE_SUPPRESSION_DEBUG_ACTIONS:
            if name in full_window_counts:
                counts[name] = int(full_window_counts.get(name) or 0)
        counts["committed_but_not_durably_admitted"] = sum(
            int(full_window_counts.get(name) or 0)
            for name in _COMMITTED_ADMISSION_ACTIONS
        )
    # The text report includes the bounded error detail rows.  Preserve only
    # typed action names, never the error payload or email/customer text.
    action_tokens = re.findall(r"(?:action=|[\"']action[\"']\s*:\s*)([A-Za-z0-9_.:-]+)", summary)
    if not isinstance(full_window_counts, dict):
        for action in action_tokens:
            if action in {"autoSaveError", "explicitSaveError"}:
                counts[action] += 1
            if action in _COMMITTED_ADMISSION_ACTIONS:
                counts["committed_but_not_durably_admitted"] += 1

    unresolved = (parsed or {}).get("unresolved", {}) if parsed else {}
    unresolved_total = sum(int(value or 0) for value in unresolved.values())
    source_sha256 = _sha256_text(_canonical_report_for_receipt(summary))
    query_descriptor = json.dumps(
        {"identity": query_identity, "actions": list(SAVE_SUPPRESSION_DEBUG_ACTIONS)},
        sort_keys=True,
        separators=(",", ":"),
    )
    packet: dict[str, Any] = {
        "section": "SAVE_SUPPRESSION_PROD_DEBUG",
        "active_debug": True,
        "material": bool(
            parsed is None
            or source_status != "available"
            or is_partial is True
            or unresolved_total
            or any(counts.values())
        ),
        "source_status": source_status,
        "window": {"start_utc": window_start, "end_utc": window_end},
        "query": {
            "identity": query_identity,
            "query_sha256": full_window_query_sha256 or query_sha256 or _sha256_text(query_descriptor),
            "report_sha256": source_sha256,
            "isPartial": is_partial,
            "rowsExamined": rows_examined,
        },
        "total": int((parsed or {}).get("total") or 0),
        "counts": counts,
        "unresolved_total": unresolved_total,
        "unresolved": dict(sorted(unresolved.items())),
        "affected_lineage": _safe_affected_lineage((parsed or {}).get("affected", [])),
        "new_or_changed_causal_fields": causal_fields[:50] if isinstance(causal_fields, list) else [],
    }
    receipt_payload = json.dumps(packet, sort_keys=True, separators=(",", ":"))
    packet["receipt_sha256"] = _sha256_text(receipt_payload)
    return packet


def collect_save_suppression(summary: str) -> dict[str, Any]:
    """Parse the save-suppression section; returns {ok, parsed, flag}."""
    parsed = parse_save_suppression_section(summary)
    if parsed is None:
        return {"ok": False, "parsed": None, "flag": False, "reason": "section_missing"}
    unresolved_total = sum(parsed.get("unresolved", {}).values())
    # Escalate only on the unresolved family (dirty/inflight/lease loops and
    # save_escape_unsynced). Bounded expected suppression (capability/latch/
    # stale-base/sweep-incompatible/sw_coalesce) must never open an obligation.
    flag = bool(unresolved_total)
    return {"ok": True, "parsed": parsed, "flag": flag}


def upsert_save_suppression_obligation(data: dict[str, Any]) -> str | None:
    """Create/refresh one typed high-priority investigation obligation when a
    customer-affecting save-suppression row persists/recurs. Closure requires
    the tracked family's product fix/canary receipts; a clean window never
    auto-resolves it. Returns the obligation id or None."""
    axiom = data.get("axiom") or {}
    if not axiom.get("ok"):
        return None
    save = collect_save_suppression(str(axiom.get("summary") or ""))
    if not save.get("ok") or not save.get("flag"):
        return None
    parsed = save["parsed"]
    unresolved = parsed.get("unresolved", {})
    affected = parsed.get("affected", [])
    affected_ids = ",".join(sorted({str(r.get("fileId") or "") for r in affected}))[:400]
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    cmd = [
        str(PM_OPS),
        "obligation-upsert",
        "--kind", "save_suppression_investigation",
        "--severity", "high",
        "--target-type", "monitor",
        "--target-id", "save-suppression-family",
        "--owner", "pm",
        "--horizon", "heartbeat",
        "--title", "Editor save-suppression family requires investigation (persisting/recursing rows)",
        "--action", "Investigate the persisted/recursing SyncSaveSuppressed family from the Axiom report; closure requires the tracked product fix/canary receipts, never a clean 3h window.",
        "--blocker", "save_suppression_unresolved",
        "--next-review-at", now,
        "--dedupe-group", "save_suppression_investigation:family",
        "--evidence", f"total={parsed.get('total', 0)}",
        "--evidence", f"unresolved={json.dumps(unresolved, sort_keys=True)}",
        "--evidence", f"affected_ids={affected_ids}",
        "--print-id",
    ]
    res = run_cmd(cmd, timeout=20)
    if not res.ok:
        return None
    return res.stdout.strip().splitlines()[-1] if res.stdout.strip() else None


def format_health(axiom: dict[str, Any]) -> tuple[str, str | None, dict[str, Any]]:
    if not axiom.get("ok"):
        window = axiom.get("window") or {}
        debug = build_save_suppression_prod_debug(
            "",
            window_start=window.get("start_utc"),
            window_end=window.get("end_utc"),
            query_sha256=(axiom.get("query") or {}).get("query_sha256"),
            is_partial=(axiom.get("query") or {}).get("isPartial"),
            rows_examined=(axiom.get("query") or {}).get("rowsExamined"),
            source_status="unavailable",
        )
        return (
            f"*Health:* UNKNOWN - {trim_text(axiom.get('error', 'Axiom unavailable'), 180)}",
            None,
            {
                "ok": False,
                "parsed": None,
                "flag": False,
                "reason": "axiom_unavailable",
                "save_suppression": {
                    "ok": False,
                    "parsed": None,
                    "flag": False,
                    "reason": "axiom_unavailable",
                    "debug": debug,
                },
            },
        )

    parsed = parse_axiom_summary(str(axiom.get("summary") or ""))
    save = collect_save_suppression(str(axiom.get("summary") or ""))
    window = axiom.get("window") or {}
    query = axiom.get("query") or {}
    action_counts = axiom.get("action_counts") or {}
    save["debug"] = build_save_suppression_prod_debug(
        str(axiom.get("summary") or ""),
        window_start=window.get("start_utc"),
        window_end=window.get("end_utc"),
        query_sha256=query.get("query_sha256"),
        query_identity=str(query.get("identity") or "axiom-activity-report:query_save_suppression"),
        is_partial=action_counts.get("isPartial", query.get("isPartial")),
        rows_examined=action_counts.get("rowsExamined", query.get("rowsExamined")),
        full_window_counts=action_counts.get("counts") if action_counts.get("ok") else None,
        causal_fields=action_counts.get("causal_fields") if action_counts.get("ok") else None,
        full_window_query_sha256=action_counts.get("query_sha256"),
        source_status="available" if action_counts.get("ok") else "partial",
    )
    parsed["save_suppression"] = save
    total = parsed.get("total")
    errors = int(parsed.get("error") or 0)
    total_text = f"{total:,}" if isinstance(total, int) else "unknown"
    if errors:
        recent = ", ".join(
            f"{name} x{count}" if count > 1 else name
            for name, count in parsed.get("recent_actions", [])
        )
        recent_text = f" Recent: {recent}." if recent else ""
        last = f" Last error {parsed['last_error_utc']} UTC." if parsed.get("last_error_utc") else ""
        health = f"*Health:* NOT clean - Axiom 3h: {errors:,} errors / {total_text} events.{recent_text}{last}"
    else:
        health = f"*Health:* Clean - Axiom 3h: 0 errors / {total_text} events."

    product_parts: list[str] = []
    product_map = [
        ("exports", "exports"),
        ("proofreads", "proofreads"),
        ("formats", "formats"),
        ("transcriptions", "transcriptions"),
    ]
    for key, label in product_map:
        if isinstance(parsed.get(key), int):
            product_parts.append(f"{parsed[key]} {label}")
    if isinstance(parsed.get("active_users"), int):
        product_parts.insert(0, f"{parsed['active_users']} active users")
    if isinstance(parsed.get("new_signups"), int):
        product_parts.append(f"{parsed['new_signups']} new sign-ups")
    if parsed.get("auto_process") == "none":
        product_parts.append("auto-process none")
    product = "*Product:* " + ", ".join(product_parts) + "." if product_parts else None
    return health, product, parsed


def format_session_age(sessions: list[dict[str, Any]]) -> list[str]:
    flags = [row for row in sessions if row.get("severity") in {"warning", "critical"}]
    unknown = [
        row
        for row in sessions
        if row.get("severity") == "unknown" or not row.get("present")
    ]
    clear_due = [row for row in sessions if row.get("clear_due")]
    already_requested = [row for row in sessions if row.get("clear_already_requested")]
    if unknown:
        unknown_text = ", ".join(
            f"{row['label']} ({row.get('error') or 'age source unavailable'})"
            for row in unknown
        )
        lines = [f"*Session age:* UNKNOWN - {unknown_text}."]
    elif not flags:
        return ["*Session age:* clean - PM/S1-S6 JSONL rows present."]
    else:
        flag_text = ", ".join(f"{row['label']} {row.get('age', 'unknown')} {row.get('severity')}" for row in flags)
        lines = [f"*Session age:* {flag_text}."]
    if clear_due:
        due_text = ", ".join(f"{row['label']} {row.get('age', 'unknown')}" for row in clear_due)
        lines.append(f"*Session-age clear handoff:* {due_text} clear_due=true; hourly ops/session-age-clear owns execution.")
    if already_requested:
        requested_text = ", ".join(
            f"{row['label']} {row.get('clear_recent_event', {}).get('event_type', 'clear_requested')}"
            for row in already_requested
        )
        lines.append(f"*Clear requests already logged:* {requested_text}.")
    lines.append(f"*Session proof:* {OUT_JSON}")
    return lines


def format_slots(data: dict[str, Any]) -> list[str]:
    mop = data.get("mop", {})
    health = "ok" if mop.get("health", {}).get("ok") else "UNKNOWN"
    ready = "ok" if mop.get("ready", {}).get("ok") else "UNKNOWN"
    raw_slots = ((mop.get("slots") or {}).get("json") or {}).get("slots") or []
    slots = [slot for slot in raw_slots if isinstance(slot, dict)]
    occupied = sum(1 for slot in slots if slot.get("occupied"))
    header = f"*Slots:* MoP health {health}, ready {ready}; {occupied}/6 occupied."
    lines = [header]
    for slot_id in ["1", "2", "3", "4", "5", "6"]:
        slot = next((item for item in slots if str(item.get("slot") or item.get("id")) == slot_id), {})
        name = slot.get("name") or f"S{slot_id}"
        issue = f"#{slot['issue']} " if slot.get("issue") else ""
        status = str(slot.get("status") or slot.get("state") or "unknown")
        idle = " idle" if slot.get("idle") else ""
        task = trim_text(slot.get("task"), 96)
        if issue and task.startswith(issue.strip()):
            issue = ""
        detail = f"{issue}{task}".strip() or "no task"
        lines.append(f"- S{slot_id} {name} - {detail} ({status}{idle})")
    return lines


def process_category(command: str) -> str:
    lower = command.lower()
    if "agent-browser" in lower or "chrome for testing" in lower:
        return "agent-browser/chrome"
    if "google chrome" in lower or "chrome helper" in lower or "chromium" in lower:
        return "chrome"
    if "playwright" in lower:
        return "playwright"
    if "vitest" in lower:
        return "vitest"
    if re.search(r"(^|[/\s])tsc([\s:]|$)", lower):
        return "tsc"
    if "next-server" in lower or "next dev" in lower:
        return "next"
    return "other"


def format_sweep(sweep: dict[str, Any]) -> str:
    if not sweep.get("ok"):
        return f"*Sweep:* UNKNOWN - {trim_text(sweep.get('error', 'ps failed'), 180)}"
    flagged = sweep.get("flagged") or []
    if not flagged:
        return "*Sweep:* clean. No next/tsc/vitest/playwright/chrome processes >30m."

    categories: dict[str, dict[str, Any]] = {}
    for proc in flagged:
        category = process_category(str(proc.get("command") or ""))
        bucket = categories.setdefault(category, {"count": 0, "max_seconds": 0})
        bucket["count"] += 1
        bucket["max_seconds"] = max(int(bucket["max_seconds"]), int(proc.get("elapsed_seconds") or 0))
    order = ["next", "tsc", "vitest", "playwright", "agent-browser/chrome", "chrome", "other"]
    bits = []
    for category in order:
        bucket = categories.get(category)
        if not bucket:
            continue
        bits.append(f"{category}={bucket['count']} max {fmt_age(bucket['max_seconds'])}")
    return f"*Sweep:* noisy - {len(flagged)} processes >30m ({'; '.join(bits)}). Details in JSON; heartbeat does not clear/kill."


def format_control_plane(cp: dict[str, Any]) -> list[str]:
    """Always render the *Control plane:* section, including none/unavailable."""

    if not isinstance(cp, dict) or not cp:
        done_status = pending_status = "unavailable"
        done_error = pending_error = "control_plane data missing from heartbeat artifact"
        done = pending = []
    else:
        done_status = cp.get("done_status", "unavailable")
        pending_status = cp.get("pending_status", "unavailable")
        done_error = cp.get("done_error") or ""
        pending_error = cp.get("pending_error") or ""
        done = cp.get("done") or []
        pending = cp.get("pending") or []

    if done_status == "unavailable":
        done_bits = (
            f"unavailable - {trim_text(done_error or 'git log origin/main unavailable', 120)}"
        )
    elif done:
        done_bits = "; ".join(
            (
                (
                    f"{row.get('sha', '?')[:8]} {row.get('subject', '')}".strip()
                    if isinstance(row.get("sha"), str)
                    else "?"
                )
                if isinstance(row, dict)
                else "?"
            )
            for row in done[:12]
        )
    else:
        done_bits = "none"

    if pending_status == "unavailable":
        pending_bits = (
            f"unavailable - {trim_text(pending_error or 'pm-ops db unavailable', 120)}"
        )
    elif pending:
        pending_bits = f"{len(pending)} open; " + "; ".join(
            (
                (
                    f"{row.get('id', '?')} {row.get('kind', '')} "
                    f"#{row.get('pr') or row.get('issue') or '?'} "
                    f"{row.get('title', '')}"
                )
                if isinstance(row, dict)
                else "?"
            ).strip()
            for row in pending[:8]
        )
    else:
        pending_bits = "none"

    return [
        "*Control plane:*",
        f"• Done (3h): {done_bits}",
        f"• Pending: {pending_bits}",
    ]


def format_open_pr_activity_audit(audit: dict[str, Any]) -> list[str]:
    """Render every normalized open-PR row with ownership and motion separate."""

    if not isinstance(audit, dict) or not audit.get("ok"):
        return [
            "*OPEN_PR_ACTIVITY_AUDIT:* UNKNOWN - "
            + trim_text((audit or {}).get("error", "audit source unavailable"), 220)
            + "; action=NOT_CLEAR"
        ]
    counts = audit.get("counts") or {}
    rows = audit.get("rows") or []
    gaps = audit.get("gaps") or []
    motion_states = audit.get("motion_states") or {}
    lines = [
        "*OPEN_PR_ACTIVITY_AUDIT:* "
        f"open_pr_activity_rows={len(rows)}; "
        f"open_prs={int(audit.get('open_pr_count') or 0)}; "
        f"open_pr_activity_gaps={len(gaps)} (exceptions first); "
        f"motion_states={json.dumps(motion_states, sort_keys=True, separators=(',', ':'))}."
    ]
    for row in rows:
        last = row.get("last_exact") or {}
        run_id = last.get("databaseId") or last.get("id") or "none"
        job_id = last.get("job_id") or "none"
        created = parse_ts(last.get("created_at") or last.get("createdAt"))
        age = fmt_age((datetime.now(timezone.utc) - created).total_seconds()) if created else "unknown"
        reasons = "; ".join(str(item) for item in row.get("reasons") or [])
        lines.append(
            f"- PR #{row.get('pr', '?')} {row.get('branch', '?')} head={row.get('head', '?')} "
            f"motion_state={row.get('motion_state', 'PROCESS_LIMBO')} owner={row.get('owner', 'unowned')} "
            f"workflow_motion={row.get('workflow_motion', 'none')}; "
            f"owner_source={row.get('owner_source', 'none')}; "
            f"hold_reason={trim_text(row.get('hold_reason', 'missing'), 220)}; "
            f"next_action={trim_text(row.get('next_action', 'missing'), 180)}; "
            f"next_owner={row.get('next_owner', 'unowned')}; "
            f"wake={trim_text(row.get('wake', 'missing'), 180)}; "
            f"last_exact_run={run_id}/{job_id} age={age}; missing={trim_text(reasons, 220)}; "
            f"next_boundary={trim_text(row.get('next_boundary', 'missing'), 180)}"
        )
    lines.append(
        f"- lane counts: capture={int(counts.get('capture') or 0)}, "
        f"ci_e2e={int(counts.get('ci_e2e') or 0)}, "
        f"numbered_reproduction={int(counts.get('numbered_reproduction') or 0)}, "
        f"numbered_rework={int(counts.get('numbered_rework') or 0)}"
    )
    return lines


def open_pr_activity_action_lines(audit: Any) -> list[str]:
    """Return action lines for UNKNOWN/limbo audit rows before any all-clear."""

    if not isinstance(audit, dict) or not audit.get("ok"):
        return [
            "OPEN_PR_ACTIVITY_AUDIT is UNKNOWN; Actions needed remains NOT_CLEAR until "
            "exact-head motion evidence is re-read."
        ]
    gaps = audit.get("gaps") or []
    if not gaps:
        return []
    actions = [
        f"OPEN_PR_ACTIVITY_AUDIT is NOT_CLEAR: {len(gaps)} open PR(s) are PROCESS_LIMBO; "
        "route each exact-head row through its stated next boundary and wake."
    ]
    for row in gaps:
        actions.append(
            f"PR #{row.get('pr', '?')} branch={row.get('branch', '?')} head={row.get('head', '?')} "
            f"state={row.get('motion_state', 'PROCESS_LIMBO')} owner={row.get('owner', 'unknown')} "
            f"missing={'; '.join(str(item) for item in row.get('reasons') or [])} "
            f"next={row.get('next_boundary', 're-read exact-head evidence')} "
            f"wake={row.get('wake', 're-read exact-head evidence')}"
        )
    return actions


def build_report(data: dict[str, Any]) -> str:
    now_label = data["now_ist"]
    lines: list[str] = [f"*Sakshi Heartbeat - {now_label}*"]

    axiom = data["axiom"]
    health_line, product_line, axiom_parsed = format_health(axiom)
    lines.append(f"\n{health_line}")
    lines.append("")

    save = axiom_parsed.get("save_suppression") or {}
    if save.get("ok"):
        save_parsed = save.get("parsed") or {}
        if save_parsed.get("total"):
            by_action = save_parsed.get("by_action") or {}
            unresolved = save_parsed.get("unresolved") or {}
            bits = []
            for action in ("save_escape_unsynced", "save_suppressed_inflight", "save_suppressed_sw_lease", "save_suppressed_lease_held", "save_suppressed_sweep_busy", "save_suppressed_sweep_lease"):
                if by_action.get(action):
                    bits.append(f"{action}={by_action[action]}")
            extra = ""
            if unresolved:
                extra = f" UNRESOLVED: {'; '.join(f'{k}={v}' for k, v in unresolved.items())}."
            affected = save_parsed.get("affected") or []
            affected_text = ""
            if affected:
                sample = ", ".join(f"{r['fileId']} {r['action']} x{r['count']}" for r in affected[:5])
                affected_text = f" Affected: {sample}."
            lines.append(f"*Save suppression:* {save_parsed['total']} events ({', '.join(bits) or 'bounded expected only'}).{extra}{affected_text}")
        else:
            lines.append("*Save suppression:* clean - no SyncSaveSuppressed rows in 3h.")
    else:
        lines.append(f"*Save suppression:* UNKNOWN - {trim_text(save.get('reason', 'section missing'), 120)}")
    debug_packet = data.get("save_suppression_prod_debug") or save.get("debug")
    if isinstance(debug_packet, dict):
        lines.append(
            "*SAVE_SUPPRESSION_PROD_DEBUG:* "
            + json.dumps(debug_packet, sort_keys=True, separators=(",", ":"))
        )
    lines.append("")

    session_rows = data["sessions"]
    clear_due = [row for row in session_rows if row.get("clear_due")]
    lines.extend(format_session_age(session_rows))

    lines.extend([""] + format_slots(data))
    stuck = [f"pane {pane}" for pane, row in data["tmux"].items() if row.get("stuck_on_prompt")]
    if stuck:
        lines.append(f"*Prompt stalls:* {', '.join(stuck)} stuck on approval prompt.")
    if product_line:
        lines.append("")
        lines.append(product_line)

    lines.append(format_sweep(data["process_sweep"]))

    lines.extend([""] + format_control_plane(data.get("control_plane")))

    lines.extend([""] + format_ready_pool_audit(data.get("ready_pool_audit", {})))

    lines.extend([""] + format_open_pr_activity_audit(data.get("open_pr_activity_audit", {})))

    latches = data["post_issue_latches"]
    lines.append(
        f"*Ops hygiene:* pending issue-create latches {latches['count']}."
    )

    pr = data["pr_drift"]
    if pr.get("ok"):
        drift = pr.get("drift", [])
        stale = pr.get("stale", [])
        if drift:
            drift_bits = [f"#{row['number']} ({', '.join(row['reasons'])})" for row in drift[:8]]
            lines.append(f"\n*PR-state drift:* NOT clean - {'; '.join(drift_bits)}")
        else:
            lines.append("\n*PR-state drift:* none flagged by label drift sweep.")
        if stale:
            stale_bits = [f"#{row['number']} {row['age']}" for row in stale[:5]]
            suffix = f", +{len(stale) - 5} older" if len(stale) > 5 else ""
            lines.append(f"*Stale PRs:* {', '.join(stale_bits)}{suffix}.")
    else:
        lines.append(f"\n*PR-state drift:* UNKNOWN - {pr.get('error', 'gh unavailable')[:240]}")

    queue = data["queue"]
    if queue.get("ok"):
        dispatchable = queue.get("dispatchable", [])
        if dispatchable:
            lines.append(
                "*Queue-motion gate:* NOT_CLEAR - dispatchable queued work for free/idle slots: "
                + ", ".join(
                    f"#{item['pr']} -> S{item['slot']} ({item.get('priority', 'normal')}, packet={item.get('packet', '?')})"
                    for item in dispatchable[:10]
                )
            )
        else:
            free_slots = [f"S{slot}" for slot in queue.get("free_or_idle_slots", [])]
            free_text = ", ".join(free_slots) if free_slots else "none"
            waiting = queue.get("packet_waiting_no_free_slot", [])
            if waiting:
                lines.append(
                    f"*Queue-motion gate:* CLEAR - packet-ready rework is waiting for capacity; "
                    f"free/idle slots {free_text}, authoritative rows {len(waiting)}."
                )
            else:
                lines.append(
                    f"*Queue-motion gate:* CLEAR - free/idle slots {free_text}, "
                    f"authoritative dispatchable rows {queue.get('queued_targeted_count', 0)}."
                )
    else:
        lines.append(f"*Queue-motion gate:* UNKNOWN - {queue.get('error', 'todo unavailable')}")

    actions: list[str] = []
    actions.extend(open_pr_activity_action_lines(data.get("open_pr_activity_audit")))
    for row in clear_due:
        if row.get("clear_already_requested"):
            continue
        target = "pm" if row["id"] == "pm" else row["id"]
        actions.append(
            f"Session-age clear due for {row['label']} {row['age']}: hourly ops should invoke Skill(session-age-clear) for slot \"{target}\" ({row.get('clear_reason')})."
        )
    if pr.get("ok") and pr.get("drift"):
        actions.append("Resolve PM label drift rows before reporting affected PRs as clean.")
    if queue.get("ok") and queue.get("dispatchable"):
        actions.append(
            "Dispatch the exact authoritative packet-ready rework before fresh todo work; "
            "record a claim receipt or a typed failure."
        )
    if not axiom.get("ok"):
        actions.append("Axiom health unavailable; rerun after Python requests/env is available if product health is needed.")
    if not actions:
        actions.append("None.")

    lines.append("\n*Actions needed:*")
    lines.extend(f"- {action}" for action in actions)
    return "\n".join(lines) + "\n"


def validate(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    sessions = data.get("sessions")
    if not isinstance(sessions, list) or len(sessions) != 7:
        errors.append("session table must contain PM plus S1-S6")
        return errors
    labels = {row.get("label") for row in sessions}
    missing = {"PM", "S1", "S2", "S3", "S4", "S5", "S6"} - labels
    if missing:
        errors.append(f"session table missing {', '.join(sorted(missing))}")
    for row in sessions:
        if not row.get("jsonl"):
            errors.append(f"{row.get('label', '?')} missing jsonl")
        if row.get("age_seconds") is None:
            errors.append(f"{row.get('label', '?')} missing age_seconds")

    audit = data.get("open_pr_activity_audit")
    if not isinstance(audit, dict) or not audit.get("ok"):
        errors.append("open_pr_activity_audit must be an authoritative readable audit")
    else:
        rows = audit.get("rows")
        open_pr_count = audit.get("open_pr_count")
        if not isinstance(rows, list):
            errors.append("open_pr_activity_audit.rows must be a list")
        if not isinstance(open_pr_count, int) or open_pr_count < 0:
            errors.append("open_pr_activity_audit.open_pr_count must be a non-negative integer")
        elif isinstance(rows, list) and len(rows) != open_pr_count:
            errors.append("open_pr_activity_audit row-count mismatch")
        gaps = audit.get("gaps")
        gap_count = audit.get("open_pr_activity_gaps")
        if not isinstance(gaps, list) or not isinstance(gap_count, int) or len(gaps) != gap_count:
            errors.append("open_pr_activity_audit gap-count mismatch")
        required = (
            "workflow_motion", "owner_source", "hold_reason",
            "next_action", "next_owner", "wake",
        )
        placeholders = {"", "unknown", "n/a", "cto-owned", "relay-only", "not-actionable"}
        if isinstance(rows, list):
            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    errors.append(f"open_pr_activity_audit.rows[{index}] must be a mapping")
                    continue
                if not isinstance(row.get("head"), str) or not OPEN_PR_HEAD.fullmatch(row["head"]):
                    errors.append(f"open_pr_activity_audit.rows[{index}] head is not a full SHA")
                if not isinstance(row.get("pr"), str) or not row["pr"].isdigit():
                    errors.append(f"open_pr_activity_audit.rows[{index}] PR identity is missing or malformed")
                if not _concrete_motion_text(row.get("branch")):
                    errors.append(f"open_pr_activity_audit.rows[{index}] branch is missing or malformed")
                if row.get("motion_state") not in OPEN_PR_MOTION_STATES:
                    errors.append(f"open_pr_activity_audit.rows[{index}] motion_state is missing or unsupported")
                for field in required:
                    value = row.get(field)
                    if not isinstance(value, str) or not value.strip():
                        errors.append(f"open_pr_activity_audit.rows[{index}] missing {field}")
                    elif value.strip().lower() in placeholders:
                        # `none` is a truthful value only for source/motion;
                        # ownership, action, wake, and hold must be concrete.
                        if field not in {"workflow_motion", "owner_source"} or value.strip().lower() != "none":
                            errors.append(f"open_pr_activity_audit.rows[{index}] placeholder {field}")
                if _concrete_motion_token(row.get("owner")) is None:
                    errors.append(f"open_pr_activity_audit.rows[{index}] owner is missing or placeholder")
                if _concrete_motion_text(row.get("next_action")) is None:
                    errors.append(f"open_pr_activity_audit.rows[{index}] next_action is missing or placeholder")
                if _concrete_motion_token(row.get("next_owner")) is None:
                    errors.append(f"open_pr_activity_audit.rows[{index}] next_owner is missing or placeholder")
                if _concrete_motion_text(row.get("wake")) is None:
                    errors.append(f"open_pr_activity_audit.rows[{index}] wake is missing or placeholder")

    cp = data.get("control_plane")
    if not isinstance(cp, dict):
        errors.append(
            "control plane section data missing (control_plane.done/pending required)"
        )
    else:
        if cp.get("done_status") not in ("ok", "unavailable"):
            errors.append("control_plane.done_status must be ok or unavailable")
        if cp.get("pending_status") not in ("ok", "unavailable"):
            errors.append("control_plane.pending_status must be ok or unavailable")
        for key in ("done", "pending"):
            rows = cp.get(key)
            if not isinstance(rows, list):
                errors.append(f"control_plane.{key} must be a list")
                continue
            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    errors.append(
                        f"control_plane.{key}[{index}] must be a mapping"
                    )
                    continue
                if key == "done" and not isinstance(row.get("sha"), str):
                    errors.append(
                        f"control_plane.done[{index}].sha must be a string"
                    )
    return errors


def launch_prompt() -> str:
    """Emit the canonical heartbeat background-agent prompt from the skill."""

    text = HEARTBEAT_SKILL.read_text(encoding="utf-8")
    # The installed heartbeat skill may still contain the retired PM Operator
    # subcommand.  Keep the prompt source intact while routing this one step
    # through the supported Sakshi adapter, which emits the same artifact and
    # preserves read-only/fail-closed semantics.
    text = text.replace(LEGACY_READY_POOL_COMMAND, SUPPORTED_READY_POOL_COMMAND)
    marker = "Use this EXACT prompt when launching the agent"
    start = text.index(marker)
    fence = text.index("```", start)
    end = text.index("```", fence + 3)
    return text[fence + 3 : end].strip() + "\n"


def send_slack(report: str) -> CmdResult:
    if not SLACK_SEND.exists():
        return CmdResult(False, "", f"missing {SLACK_SEND}", 127)
    return run_cmd(["bash", str(SLACK_SEND), "-f"], timeout=30, input_text=report)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic HeyDonna Sakshi heartbeat")
    parser.add_argument(
        "--launch-prompt",
        action="store_true",
        help="print the canonical heartbeat background-agent prompt from the heartbeat-tasks skill and exit",
    )
    parser.add_argument(
        "--ready-pool-audit",
        action="store_true",
        help="run the supported read-only Ready Pool audit and emit its artifact",
    )
    parser.add_argument("--send-slack", action="store_true", help="send the report to Rajiv via slack-send.sh")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="collect and render the report without persisting obligations or sending Slack",
    )
    parser.add_argument("--json-out", default=str(OUT_JSON), help="JSON output path")
    parser.add_argument("--text-out", default=str(OUT_TEXT), help="Slack text output path")
    args = parser.parse_args()

    if args.dry_run and args.send_slack:
        parser.error("--dry-run cannot be combined with --send-slack")

    if args.ready_pool_audit:
        result = collect_ready_pool_audit()
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("ok") else 2

    if args.launch_prompt:
        try:
            print(launch_prompt(), end="")
        except (OSError, ValueError) as exc:
            print(
                f"[sakshi-heartbeat] launch prompt unavailable: {exc}",
                file=sys.stderr,
            )
            return 2
        return 0

    now_utc = datetime.now(timezone.utc)
    axiom_window_start = now_utc - timedelta(hours=CONTROL_PLANE_HOURS)
    mop = collect_mop()
    slots = slot_state_map(mop)
    mop_events = {
        key: (mop.get(key, {}).get("json") or {})
        for key in ("recent_cleared", "recent_clear_executed")
        if mop.get(key, {}).get("ok")
    }
    sessions = [
        analyze_session(
            entry,
            now_utc,
            mop_row=slots.get(entry["id"]),
            mop_events=mop_events,
        )
        for entry in SESSIONS
    ]
    update_omp_effective_starts(sessions, mop, now_utc)
    apply_clear_policy(sessions, slots)
    mark_recent_clear_requests(sessions, mop, now_utc)

    data: dict[str, Any] = {
        "generated_at": now_utc.isoformat(),
        "now_ist": now_utc.astimezone(IST).strftime("%Y-%m-%d %H:%M IST"),
        "sessions": sessions,
        "mop": mop,
        "mop_summary": summarize_mop(mop, slots),
        "tmux": capture_tmux(),
        "process_sweep": process_sweep(),
        "axiom": collect_axiom(window_start=axiom_window_start, window_end=now_utc),
        "pr_drift": collect_pr_drift(),
        "queue": collect_queue(slots),
        "cc_reports": collect_cc_reports(),
        "post_issue_latches": collect_post_issue_latches(),
        "control_plane": collect_control_plane(),
        "ready_pool_audit": collect_ready_pool_audit(),
        "open_pr_activity_audit": collect_open_pr_activity_audit(slots),
    }
    _, _, axiom_parsed = format_health(data["axiom"])
    data["save_suppression_prod_debug"] = (
        (axiom_parsed.get("save_suppression") or {}).get("debug")
        if isinstance(axiom_parsed, dict)
        else None
    )
    data["validation_errors"] = validate(data)

    report = build_report(data)
    if not args.dry_run:
        obligation_id = upsert_save_suppression_obligation(data)
        if obligation_id:
            data["save_suppression_obligation_id"] = obligation_id

    json_path = Path(args.json_out)
    text_path = Path(args.text_out)
    json_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    text_path.write_text(report, encoding="utf-8")

    if args.send_slack:
        send_result = send_slack(report)
        data["slack_send"] = {
            "ok": send_result.ok,
            "returncode": send_result.returncode,
            "stdout": send_result.stdout,
            "stderr": send_result.stderr,
        }
        json_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if not send_result.ok:
            print(f"[sakshi-heartbeat] Slack send failed: {send_result.stderr or send_result.stdout}", file=sys.stderr)

    print(report, end="")
    if data["validation_errors"]:
        print("[sakshi-heartbeat] validation failed: " + "; ".join(data["validation_errors"]), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
