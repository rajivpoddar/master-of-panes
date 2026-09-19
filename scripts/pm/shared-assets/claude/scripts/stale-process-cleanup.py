#!/usr/bin/env python3
"""Detect and optionally clean stale slot-owned development processes.

This is intentionally narrow. It only considers allowlisted process shapes:
Chrome-for-Testing/agent-browser, Next.js dev/server, Convex dev, and tsc
processes. It writes a proof JSON file on both scan and apply runs.

Two operator-contract points are explicit here:

* A policy-eligible candidate with NO slot association is never reaped
  implicitly. It requires an explicit operator attestation
  (``--attest-slotless-reap``, optionally accompanied by an attestation file
  whose path and digest are recorded). Slot-associated candidates are
  unaffected: they still require the free+idle ownership proof and are still
  refused when their owner changed.
* Kill outcomes are reported by outcome class. Refusals are never written into
  the terminated list, so the artifact and the printed counts cannot claim a
  kill that did not happen. ``killed`` is retained only as an exact alias of
  ``terminated`` for older readers.
"""

from __future__ import annotations

import argparse
import hashlib
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
SLOT_NUMBERS = tuple(range(1, 7))


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


def load_slot_inventory() -> tuple[dict[int, dict[str, Any]], str | None]:
    """Read the complete canonical six-slot inventory before cleanup eligibility.

    A missing, unreadable, malformed, or incomplete inventory is an authority
    failure. Apply mode must refuse before it enumerates kill candidates.
    """
    if not MOP_DB.exists():
        return {}, f"missing:{MOP_DB}"
    try:
        result = run(
            [
                "sqlite3",
                "-json",
                str(MOP_DB),
                "SELECT slot,status,occupied,idle,dnd,activity,issue,task,repository_id,branch,branch_ref,pr,head_sha,work_kind,handoff_id,claimed_at,active_turn_state,active_turn_id,last_activity FROM slots WHERE slot BETWEEN 1 AND 6 ORDER BY slot;",
            ],
            timeout=3,
        )
    except Exception as exc:
        return {}, f"read_failed:{type(exc).__name__}"
    if result.returncode != 0:
        return {}, f"read_failed:{(result.stderr or result.stdout or 'sqlite3 failed').strip()[:200]}"
    try:
        records = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}, "malformed_json"
    if not isinstance(records, list):
        return {}, "malformed_json_shape"
    states: dict[int, dict[str, Any]] = {}
    required_fields = (
        "slot", "status", "occupied", "idle", "dnd", "activity", "issue", "task",
        "repository_id", "branch", "branch_ref", "pr", "head_sha", "work_kind",
        "handoff_id", "claimed_at", "active_turn_state", "active_turn_id", "last_activity",
    )
    for record in records:
        if not isinstance(record, dict) or any(field not in record for field in required_fields):
            return {}, "malformed_row"
        try:
            slot = record["slot"]
            occupied = record["occupied"]
            idle = record["idle"]
            dnd = record["dnd"]
            if any(isinstance(value, bool) or not isinstance(value, int) for value in (slot, occupied, idle, dnd)):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            return {}, "malformed_identity"
        if slot not in SLOT_NUMBERS or slot in states:
            return {}, "invalid_or_duplicate_slot"
        status = record["status"]
        if not isinstance(status, str) or not status:
            return {}, "malformed_status"
        states[slot] = {
            "slot": slot,
            "root": slot_root(slot),
            "status": status,
            "occupied": occupied,
            "idle": idle,
            "dnd": dnd,
            "activity": record["activity"] or None,
            "issue": record["issue"] or None,
            "task": record["task"] or None,
            "repository_id": record["repository_id"] or None,
            "branch": record["branch"] or None,
            "branch_ref": record["branch_ref"] or None,
            "pr": record["pr"] or None,
            "head_sha": record["head_sha"] or None,
            "work_kind": record["work_kind"] or None,
            "handoff_id": record["handoff_id"] or None,
            "claimed_at": record["claimed_at"] or None,
            "active_turn_state": record["active_turn_state"] or None,
            "active_turn_id": record["active_turn_id"] or None,
            "last_activity": record["last_activity"] or None,
            "free_idle": status == "free" and occupied == 0 and idle == 1,
        }
    missing = sorted(set(SLOT_NUMBERS) - set(states))
    if missing:
        return {}, f"incomplete_slots:{','.join(map(str, missing))}"
    return states, None


def slot_states() -> dict[int, dict[str, Any]]:
    """Compatibility view for callers that only need the parsed inventory."""
    return load_slot_inventory()[0]


def under(path: str | None, root: str) -> bool:
    if not path:
        return False
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except Exception:
        return False


def associated_slot(cwd: str | None, states: dict[int, dict[str, Any]]) -> dict[str, Any] | None:
    for slot in SLOT_NUMBERS:
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


def collect_processes(
    agent_browser_min: int,
    nextjs_min: int,
    dev_tool_min: int,
    slot_inventory: tuple[dict[int, dict[str, Any]], str | None] | None = None,
) -> dict[str, Any]:
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
    states, inventory_error = slot_inventory or load_slot_inventory()
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
        known_checkout = slot_number is not None
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
        if known_checkout and inventory_error:
            eligible = False
            skip_reason = f"slot_inventory_unavailable:{inventory_error}"
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
        # A known slot checkout is preserved capacity, not an unbound process
        # pool.  Keep this explicit final guard so a held/occupied slot cannot
        # fall through merely because the process is old or orphaned.
        if known_checkout and inventory_error:
            eligible = False
            skip_reason = f"slot_inventory_unavailable:{inventory_error}"
        elif known_checkout and slot_state and not slot_free_idle:
            eligible = False
            skip_reason = "held_or_preserved_slot_checkout"
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
            "slot_inventory_available": inventory_error is None,
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
        "slot_inventory_available": inventory_error is None,
        "slot_inventory_error": inventory_error,
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


def revalidate_candidate(row: dict[str, Any], allow_slotless_attested: bool = False) -> tuple[bool, str]:
    """Recheck process and slot ownership immediately before each signal.

    Process identity is always required, including the kernel-reported start
    time that fences PID reuse.  A slot-associated candidate must still match
    its slot row and its positive free+idle ownership proof.  A slot-less
    candidate is reaped only under the explicit operator attestation, and never
    once its cwd has drifted into a slot checkout.
    """
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
    states, inventory_error = load_slot_inventory()
    if inventory_error:
        return False, f"slot_inventory_unavailable:{inventory_error}"
    slot_state = associated_slot(current.get("cwd"), states)
    if row.get("associated_slot") is None:
        if slot_state is not None:
            return False, "slot_owner_changed"
        if row.get("owner_proof") is not None:
            return False, "slot_owner_evidence_changed"
        if not allow_slotless_attested:
            return False, "slotless_reap_not_attested"
        return True, "slotless_attested_identity_match"
    if not slot_state or slot_state.get("slot") != row.get("associated_slot"):
        return False, "slot_owner_changed"
    owner_proof = stale_owner_evidence(slot_state)
    if owner_proof != row.get("owner_proof"):
        return False, "slot_owner_evidence_changed"
    return True, "identity_and_owner_match"


def attestation_metadata(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Fingerprint the operator attestation file recorded with the attested reap.

    The file is provenance for the operator attestation, not a replacement for
    it: ``--attest-slotless-reap`` is what opens the slot-less reap path, and
    the tool still re-verifies process identity and slot association itself
    immediately before every signal.
    """
    if not path.exists():
        return None, f"missing:{path}"
    if not path.is_file():
        return None, f"not_a_regular_file:{path}"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, f"unreadable:{type(exc).__name__}"
    if not raw.strip():
        return None, f"empty:{path}"
    return (
        {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)},
        None,
    )


def kill_candidates(
    candidates: list[dict[str, Any]],
    wait_s: float,
    allow_slotless_attested: bool = False,
) -> list[dict[str, Any]]:
    """Signal eligible candidates, tagging each entry with its outcome class.

    Every entry carries ``outcome``: ``terminated``, ``already_exited``,
    ``refused``, or ``error``.  Callers split on that field, so a refusal can
    never be counted or reported as a kill.
    """
    results: list[dict[str, Any]] = []
    for row in candidates:
        pid = int(row["pid"])
        item: dict[str, Any] = {
            "pid": pid,
            "category": row["category"],
            "associated_slot": row.get("associated_slot"),
            "term_sent": False,
            "kill_sent": False,
            "status": "unknown",
            "outcome": "unknown",
        }
        valid, reason = revalidate_candidate(row, allow_slotless_attested)
        if not valid:
            item["status"] = (
                "attestation_required_refused"
                if reason == "slotless_reap_not_attested"
                else "identity_or_owner_refused"
            )
            item["reason"] = reason
            item["outcome"] = "refused"
            results.append(item)
            continue
        if row.get("associated_slot") is None:
            item["attestation"] = {
                "basis": "operator_attested_slotless_reap",
                "slot_association": None,
                "category": row.get("category"),
                "age_seconds": row.get("age_seconds"),
                "age": row.get("age"),
                "threshold_min": row.get("threshold_min"),
                "pid": pid,
                "start_time": row.get("start_time"),
                "cwd": row.get("cwd"),
                "pid_reuse_verified": bool(row.get("start_time")),
                "verification": reason,
            }
        try:
            os.kill(pid, signal.SIGTERM)
            item["term_sent"] = True
        except ProcessLookupError:
            item["status"] = "already_exited"
            item["outcome"] = "already_exited"
            results.append(item)
            continue
        except PermissionError as exc:
            item["status"] = "permission_denied"
            item["error"] = str(exc)
            item["outcome"] = "error"
            results.append(item)
            continue
        time.sleep(wait_s)
        if not alive(pid):
            item["status"] = "terminated"
            item["outcome"] = "terminated"
            results.append(item)
            continue
        valid, reason = revalidate_candidate(row, allow_slotless_attested)
        if not valid:
            item["status"] = "identity_or_owner_refused_before_kill"
            item["reason"] = reason
            item["outcome"] = "refused"
            results.append(item)
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            item["kill_sent"] = True
        except ProcessLookupError:
            item["status"] = "terminated_after_term"
            item["outcome"] = "terminated"
            results.append(item)
            continue
        except PermissionError as exc:
            item["status"] = "kill_permission_denied"
            item["error"] = str(exc)
            item["outcome"] = "error"
            results.append(item)
            continue
        time.sleep(0.5)
        if alive(pid):
            item["status"] = "still_alive"
            item["outcome"] = "error"
        else:
            item["status"] = "killed"
            item["outcome"] = "terminated"
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
    parser.add_argument(
        "--attest-slotless-reap",
        action="store_true",
        help=(
            "explicit operator attestation: allow reaping policy-eligible candidates with NO slot "
            "association. Slot-associated candidates are unchanged and still require free+idle ownership."
        ),
    )
    parser.add_argument(
        "--attestation-file",
        default=None,
        help="optional operator attestation file; its path, size and sha256 are recorded in the proof",
    )
    args = parser.parse_args()

    attestation: dict[str, Any] | None = None
    attestation_error: str | None = None
    if args.attestation_file:
        if not args.attest_slotless_reap:
            attestation_error = "attestation_file_requires_attest_slotless_reap"
        else:
            attestation, attestation_error = attestation_metadata(Path(args.attestation_file))

    inventory = load_slot_inventory()
    outcomes: list[dict[str, Any]] = []
    if args.apply and inventory[1] is not None:
        before = {
            "ok": False,
            "slot_inventory_available": False,
            "slot_inventory_error": inventory[1],
            "error": f"slot_inventory_unavailable:{inventory[1]}",
            "rows": [],
            "candidates": [],
            "skipped": [],
        }
        after = before
    elif args.apply and attestation_error:
        before = {
            "ok": False,
            "slot_inventory_available": inventory[1] is None,
            "slot_inventory_error": inventory[1],
            "error": f"attestation_invalid:{attestation_error}",
            "rows": [],
            "candidates": [],
            "skipped": [],
        }
        after = before
    else:
        before = collect_processes(args.agent_browser_min, args.nextjs_min, args.dev_tool_min, inventory)
        if args.apply and before.get("ok"):
            outcomes = kill_candidates(
                before.get("candidates", []),
                args.term_wait_s,
                allow_slotless_attested=args.attest_slotless_reap,
            )
        after = collect_processes(args.agent_browser_min, args.nextjs_min, args.dev_tool_min)

    terminated = [item for item in outcomes if item.get("outcome") == "terminated"]
    refused = [item for item in outcomes if item.get("outcome") == "refused"]
    already_exited = [item for item in outcomes if item.get("outcome") == "already_exited"]
    errors = [item for item in outcomes if item.get("outcome") == "error"]
    before_count = len(before.get("candidates", []))
    after_count: int | str = len(after.get("candidates", [])) if after.get("ok") else "unknown"

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "apply" if args.apply else "scan",
        "policy": {
            "agent_browser_min": args.agent_browser_min,
            "nextjs_min": args.nextjs_min,
            "dev_tool_min": args.dev_tool_min,
            "slot_scoped_free_idle_required": ["agent-browser", "nextjs", "convex", "tsc"],
            "allowlist": ["agent-browser", "Chrome for Testing", "Chromium remote-debugging/playwright", "next dev/start", "next-server", "convex dev", "tsc --noEmit"],
            "slotless_reap_requires_operator_attestation": True,
            "outcome_arrays": ["terminated", "refused", "already_exited", "errors"],
            "killed": "deprecated alias of terminated; refusals are never reported as killed",
        },
        "attestation": {
            "slotless_reap_attested": bool(args.attest_slotless_reap),
            "attestation_file": attestation,
            "attestation_error": attestation_error,
        },
        "before": before,
        "outcomes": outcomes,
        "terminated": terminated,
        "refused": refused,
        "already_exited": already_exited,
        "errors": errors,
        "killed": terminated,
        "summary": {
            "candidates": before_count,
            "terminated": len(terminated),
            "refused": len(refused),
            "already_exited": len(already_exited),
            "errors": len(errors),
            "after_candidates": after_count,
            "slotless_reap_attested": bool(args.attest_slotless_reap),
        },
        "after": after,
    }
    Path(args.output).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"STALE_PROCESS_CANDIDATES count={before_count} proof={args.output}")
    for row in before.get("candidates", [])[:20]:
        print(
            f"PROCESS pid={row['pid']} category={row['category']} age={row['age']} "
            f"cwd={row.get('cwd') or 'unknown'} slot={row.get('associated_slot') or 'none'} "
            f"slot_free_idle={row['slot_free_idle']}"
        )
    if args.apply:
        if not before.get("ok"):
            print(f"STALE_PROCESS_CLEANUP refused={before.get('error', 'unknown')} proof={args.output}")
        else:
            print(
                f"STALE_PROCESS_CLEANUP terminated={len(terminated)} refused={len(refused)} "
                f"already_exited={len(already_exited)} errors={len(errors)} "
                f"after_candidates={after_count} killed={len(terminated)} proof={args.output}"
            )
    return 0 if before.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
