#!/usr/bin/env python3
"""Detect and optionally clean stale slot-owned development processes.

This is intentionally narrow. It only considers allowlisted process shapes:
Chrome-for-Testing/agent-browser, Next.js dev/server, Convex dev, and tsc
processes. It writes a proof JSON file on both scan and apply runs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


OUT_JSON = Path("/tmp/stale-process-cleanup-latest.json")
PROJECT_BASE = Path("/Users/rajiv/Downloads/projects")
MOP_DB = Path("/Users/rajiv/.claude/plugins/cache/rajiv-plugins/master-of-panes/1.0.0/data/mop.db")


def run(args: list[str], timeout: int = 5) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout)


def parse_etime(value: str) -> int | None:
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
        if len(parts) == 3:
            hours, minutes, seconds = [int(part) for part in parts]
        elif len(parts) == 2:
            hours = 0
            minutes, seconds = [int(part) for part in parts]
        else:
            return None
    except ValueError:
        return None
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def fmt_age(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    days, rem = divmod(max(0, seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def get_cwd(pid: int) -> str | None:
    try:
        result = run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], timeout=2)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("n/"):
            return line[1:]
    return None


def get_start_time(pid: int) -> str | None:
    """Return the kernel-reported process start identity, or None if unavailable."""
    try:
        result = run(["ps", "-p", str(pid), "-o", "lstart="], timeout=2)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def slot_root(slot: int) -> str:
    return str(PROJECT_BASE / f"heydonna-app-300{slot}")


def slot_states() -> dict[int, dict[str, Any]]:
    if not MOP_DB.exists():
        return {}
    try:
        result = run(
            [
                "sqlite3",
                str(MOP_DB),
                "SELECT slot,status,occupied,idle,dnd,activity,issue,task,repository_id,branch,branch_ref,pr,head_sha,work_kind,handoff_id,claimed_at,active_turn_state,active_turn_id,last_activity FROM slots WHERE slot BETWEEN 1 AND 4;",
            ],
            timeout=3,
        )
    except Exception:
        return {}
    if result.returncode != 0:
        return {}
    states: dict[int, dict[str, Any]] = {}
    for raw in result.stdout.splitlines():
        parts = raw.split("|")
        if len(parts) < 19:
            continue
        try:
            slot = int(parts[0])
            occupied = int(parts[2])
            idle = int(parts[3])
            dnd = int(parts[4])
        except ValueError:
            continue
        if slot not in {1, 2, 3, 4}:
            continue
        status = parts[1]
        states[slot] = {
            "slot": slot,
            "root": slot_root(slot),
            "status": status,
            "occupied": occupied,
            "idle": idle,
            "dnd": dnd,
            "activity": parts[5] or None,
            "issue": parts[6] or None,
            "task": parts[7] or None,
            "repository_id": parts[8] or None,
            "branch": parts[9] or None,
            "branch_ref": parts[10] or None,
            "pr": parts[11] or None,
            "head_sha": parts[12] or None,
            "work_kind": parts[13] or None,
            "handoff_id": parts[14] or None,
            "claimed_at": parts[15] or None,
            "active_turn_state": parts[16] or None,
            "active_turn_id": parts[17] or None,
            "last_activity": parts[18] or None,
            "free_idle": status == "free" and occupied == 0 and idle == 1,
        }
    return states


def under(path: str | None, root: str) -> bool:
    if not path:
        return False
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except Exception:
        return False


def associated_slot(cwd: str | None, states: dict[int, dict[str, Any]]) -> dict[str, Any] | None:
    for slot in range(1, 5):
        root = slot_root(slot)
        if under(cwd, root):
            return states.get(
                slot,
                {
                    "slot": slot,
                    "root": root,
                    "status": None,
                    "occupied": None,
                    "idle": None,
                    "dnd": None,
                    "activity": None,
                    "issue": None,
                    "task": None,
                    "repository_id": None,
                    "branch": None,
                    "branch_ref": None,
                    "pr": None,
                    "head_sha": None,
                    "work_kind": None,
                    "handoff_id": None,
                    "claimed_at": None,
                    "active_turn_state": None,
                    "active_turn_id": None,
                    "last_activity": None,
                    "free_idle": False,
                    "state_missing": True,
                },
            )
    return None


def stale_owner_evidence(slot_state: dict[str, Any] | None) -> str | None:
    """Return positive existing MoP evidence that a slot has no live owner.

    Age is never ownership evidence.  Only the complete, explicit free/idle
    state with no owner/dependency fields can authorize cleanup of a
    slot-scoped backend.  Missing, held, DND, active, or partially populated
    state remains fail-closed.
    """
    if not slot_state or slot_state.get("state_missing"):
        return None
    if (
        slot_state.get("status") != "free"
        or slot_state.get("occupied") != 0
        or slot_state.get("idle") != 1
        or slot_state.get("dnd") != 0
        or slot_state.get("active_turn_state") != "inactive"
        or slot_state.get("active_turn_id") is not None
    ):
        return None
    if any(
        slot_state.get(key)
        for key in (
            "activity",
            "issue",
            "task",
            "repository_id",
            "branch",
            "branch_ref",
            "pr",
            "head_sha",
            "work_kind",
            "handoff_id",
            "claimed_at",
        )
    ):
        return None
    return "mop_free_idle_inactive_no_assignment"


def classify(command: str) -> str | None:
    lower = command.lower()
    if "skycomputeruseclient" in lower:
        return None
    if "chrome for testing" in lower or re.search(r"(^|/|\s)agent-browser([-\s/]|$)", lower):
        return "agent-browser"
    if "chromium" in lower and ("--remote-debugging" in lower or "playwright" in lower):
        return "agent-browser"
    if lower.startswith("next-server") or re.search(
        r"(^|[/\s])node\s+.*node_modules/(?:\.bin/next|next/(?:dist/)?bin/next)\s+(dev|start)(\s|$)",
        lower,
    ):
        return "nextjs"
    if "convex" in lower and re.search(r"(^|[/\s])convex\s+dev(\s|$)|\bconvex\b.*\bdev\b", lower):
        return "convex"
    if re.search(r"(^|[/\s])tsc(\s|$)|typescript/bin/tsc", lower) and "--noemit" in lower:
        return "tsc"
    return None


def command_identity(command: str) -> tuple[str, str] | None:
    """Return the strict allowlisted executable identity for a process shape."""
    category = classify(command)
    if category is None:
        return None
    lower = command.lower()
    if category == "convex":
        match = re.search(r"(?<!\S)(\S*/?convex)\s+dev(?:\s|$)", lower)
        return (category, match.group(1)) if match else None
    if category == "nextjs":
        match = re.search(
            r"(?<!\S)(\S*(?:next-server|node_modules/(?:\.bin/next|next/(?:dist/)?bin/next)))\s+(?:dev|start)(?:\s|$)",
            lower,
        )
        return (category, match.group(1)) if match else None
    if category == "tsc":
        match = re.search(r"(?<!\S)(\S*(?:tsc|typescript/bin/tsc))(?:\s|$)", lower)
        return (category, match.group(1)) if match and "--noemit" in lower else None
    if "chrome for testing" in lower:
        return (category, "chrome-for-testing")
    match = re.search(r"(?<!\S)(\S*agent-browser\S*|\S*chromium\S*)(?:\s|$)", lower)
    return (category, match.group(1)) if match else None


def sanitize_command(command: str) -> str:
    """Keep bounded process-shape context without exposing credential values."""
    bounded = command[:500]
    return re.sub(
        r"(?i)(--?(?:token|secret|password|authorization|api[-_]?key)(?:=|\s+))[^\s]+",
        r"\1<redacted>",
        bounded,
    )


def collect_processes(agent_browser_min: int, nextjs_min: int, dev_tool_min: int) -> dict[str, Any]:
    result = run(["ps", "-axo", "pid=,ppid=,uid=,etime=,command="], timeout=8)
    rows: list[dict[str, Any]] = []
    if result.returncode != 0:
        return {
            "ok": False,
            "error": result.stderr or result.stdout or "ps failed",
            "rows": [],
            "candidates": [],
            "skipped": [],
        }

    current_uid = os.getuid()
    states = slot_states()
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 4)
        if len(parts) < 5:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            uid = int(parts[2])
        except ValueError:
            continue
        if uid != current_uid or pid == os.getpid():
            continue
        age_seconds = parse_etime(parts[3])
        command = parts[4]
        category = classify(command)
        identity = command_identity(command)
        if not category or identity is None or age_seconds is None:
            continue
        cwd = get_cwd(pid)
        start_time = get_start_time(pid)
        slot_state = associated_slot(cwd, states)
        slot_number = slot_state["slot"] if slot_state else None
        slot_free_idle = bool(slot_state and slot_state["free_idle"])
        owner_proof = stale_owner_evidence(slot_state)
        if category == "agent-browser":
            threshold_min = agent_browser_min
        elif category == "nextjs":
            threshold_min = nextjs_min
        else:
            threshold_min = dev_tool_min
        skip_reason = None
        eligible = True
        if age_seconds < threshold_min * 60:
            eligible = False
            skip_reason = f"age below {threshold_min}m"
        if category in {"agent-browser", "nextjs", "convex", "tsc"} and slot_state and not slot_free_idle:
            eligible = False
            skip_reason = "associated slot is not free+idle"
        if category in {"nextjs", "convex", "tsc"} and not slot_state:
            eligible = False
            skip_reason = "no associated slot cwd"
        if category in {"nextjs", "convex", "tsc"} and slot_state and not owner_proof:
            eligible = False
            skip_reason = "no positive stale-owner evidence"
        if category == "agent-browser" and slot_state and not owner_proof:
            eligible = False
            skip_reason = "no positive stale-owner evidence"
        if not start_time:
            eligible = False
            skip_reason = "process start identity unavailable"
        if identity is None:
            eligible = False
            skip_reason = "process shape unavailable"
        if category == "agent-browser" and "google chrome.app/contents/macos/google chrome" in command.lower():
            eligible = False
            skip_reason = "regular Google Chrome excluded"
        row = {
            "pid": pid,
            "ppid": ppid,
            "uid": uid,
            "category": category,
            "command_identity": identity,
            "age_seconds": age_seconds,
            "age": fmt_age(age_seconds),
            "threshold_min": threshold_min,
            "cwd": cwd,
            "start_time": start_time,
            "associated_slot": slot_number,
            "slot_status": slot_state["status"] if slot_state else None,
            "slot_occupied": slot_state["occupied"] if slot_state else None,
            "slot_idle": slot_state["idle"] if slot_state else None,
            "slot_activity": slot_state["activity"] if slot_state else None,
            "slot_state_missing": bool(slot_state and slot_state.get("state_missing")),
            "slot_free_idle": slot_free_idle,
            "owner_proof": owner_proof,
            "eligible": eligible,
            "skip_reason": skip_reason,
            "command": sanitize_command(command),
        }
        rows.append(row)

    candidates = [row for row in rows if row["eligible"]]
    skipped = [row for row in rows if not row["eligible"]]
    return {
        "ok": True,
        "slot_states": states,
        "rows": rows,
        "candidates": candidates,
        "skipped": skipped,
    }


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _process_snapshot(pid: int) -> dict[str, Any] | None:
    """Read the identity fields used to fence a destructive signal."""
    try:
        result = run(["ps", "-p", str(pid), "-o", "pid=,ppid=,uid=,command="], timeout=2)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    parts = result.stdout.strip().split(None, 3)
    if len(parts) < 4:
        return None
    try:
        current_pid, ppid, uid = (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None
    return {
        "pid": current_pid,
        "ppid": ppid,
        "uid": uid,
        "command": parts[3],
        "category": classify(parts[3]),
        "command_identity": command_identity(parts[3]),
        "start_time": get_start_time(pid),
        "cwd": get_cwd(pid),
    }


def revalidate_candidate(row: dict[str, Any]) -> tuple[bool, str]:
    """Recheck process and slot ownership immediately before each signal."""
    pid = int(row["pid"])
    if not row.get("start_time"):
        return False, "process_start_identity_missing"
    current = _process_snapshot(pid)
    if not current:
        return False, "process_identity_unavailable"
    if current.get("category") != row.get("category"):
        return False, "process_category_changed"
    if current.get("command_identity") != row.get("command_identity"):
        return False, "process_executable_identity_changed"
    if current.get("command_identity") is None:
        return False, "process_shape_unavailable"
    for key in ("pid", "ppid", "uid", "start_time", "cwd"):
        if current.get(key) != row.get(key):
            return False, f"process_identity_changed:{key}"
    states = slot_states()
    slot_state = associated_slot(current.get("cwd"), states)
    if not slot_state or slot_state.get("slot") != row.get("associated_slot"):
        return False, "slot_owner_changed"
    owner_proof = stale_owner_evidence(slot_state)
    if owner_proof != row.get("owner_proof"):
        return False, "slot_owner_evidence_changed"
    return True, "identity_and_owner_match"


def kill_candidates(candidates: list[dict[str, Any]], wait_s: float) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for row in candidates:
        pid = int(row["pid"])
        item = {
            "pid": pid,
            "category": row["category"],
            "term_sent": False,
            "kill_sent": False,
            "status": "unknown",
        }
        valid, reason = revalidate_candidate(row)
        if not valid:
            item["status"] = "identity_or_owner_refused"
            item["reason"] = reason
            results.append(item)
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            item["term_sent"] = True
        except ProcessLookupError:
            item["status"] = "already_exited"
            results.append(item)
            continue
        except PermissionError as exc:
            item["status"] = "permission_denied"
            item["error"] = str(exc)
            results.append(item)
            continue
        time.sleep(wait_s)
        if not alive(pid):
            item["status"] = "terminated"
            results.append(item)
            continue
        valid, reason = revalidate_candidate(row)
        if not valid:
            item["status"] = "identity_or_owner_refused_before_kill"
            item["reason"] = reason
            results.append(item)
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            item["kill_sent"] = True
        except ProcessLookupError:
            item["status"] = "terminated_after_term"
            results.append(item)
            continue
        except PermissionError as exc:
            item["status"] = "kill_permission_denied"
            item["error"] = str(exc)
            results.append(item)
            continue
        time.sleep(0.5)
        item["status"] = "killed" if not alive(pid) else "still_alive"
        results.append(item)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan/apply stale allowlisted process cleanup")
    parser.add_argument("--apply", action="store_true", help="send TERM then KILL to eligible allowlisted PIDs")
    parser.add_argument("--output", default=str(OUT_JSON), help="proof JSON output path")
    parser.add_argument("--agent-browser-min", type=int, default=60)
    parser.add_argument("--nextjs-min", type=int, default=360)
    parser.add_argument("--dev-tool-min", type=int, default=30)
    parser.add_argument("--term-wait-s", type=float, default=3.0)
    args = parser.parse_args()

    before = collect_processes(args.agent_browser_min, args.nextjs_min, args.dev_tool_min)
    killed: list[dict[str, Any]] = []
    if args.apply and before.get("ok"):
        killed = kill_candidates(before.get("candidates", []), args.term_wait_s)
    after = collect_processes(args.agent_browser_min, args.nextjs_min, args.dev_tool_min)

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "apply" if args.apply else "scan",
        "policy": {
            "agent_browser_min": args.agent_browser_min,
            "nextjs_min": args.nextjs_min,
            "dev_tool_min": args.dev_tool_min,
            "slot_scoped_free_idle_required": ["agent-browser", "nextjs", "convex", "tsc"],
            "allowlist": ["agent-browser", "Chrome for Testing", "Chromium remote-debugging/playwright", "next dev/start", "next-server", "convex dev", "tsc --noEmit"],
        },
        "before": before,
        "killed": killed,
        "after": after,
    }
    Path(args.output).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    before_count = len(before.get("candidates", []))
    after_count = len(after.get("candidates", [])) if after.get("ok") else "unknown"
    print(f"STALE_PROCESS_CANDIDATES count={before_count} proof={args.output}")
    for row in before.get("candidates", [])[:20]:
        print(
            f"PROCESS pid={row['pid']} category={row['category']} age={row['age']} "
            f"cwd={row.get('cwd') or 'unknown'} slot={row.get('associated_slot') or 'none'} "
            f"slot_free_idle={row['slot_free_idle']}"
        )
    if args.apply:
        print(f"STALE_PROCESS_CLEANUP killed={len(killed)} after_candidates={after_count} proof={args.output}")
    return 0 if before.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
