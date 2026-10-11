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
MOP_DB = Path("/Users/rajiv/.local/share/master-of-panes/data/mop.db")
STATUS_JSON = Path("/tmp/stale-process-cleanup-status.json")
PM_WORKTREE_PREFIX = "/private/tmp/claude-501/wt-"
PM_WORKTREE_MIN_AGE_S = 60 * 60
DUPLICATE_DEV_MIN_AGE_S = 10 * 60
HISTORY_JSON = Path("/Users/rajiv/.claude/state-stale-cleanup-history.json")
AB_IDLE_MIN_DEFAULT = 120  # agent-browser daemon with no CPU progress this long is idle
REPLAYD_CPU_THRESHOLD = 50.0
REPLAYD_STREAK_RESTART = 2
IDLE_ORPHAN_POLICIES = ("idle_agent_browser", "orphan_dev_tool", "dead_slot_session")
PERSIST_HISTORY = False
# No hard-coded slot range (Rajiv 2026-10-07 22:03 IST, DM D0AMF0XE6TS thread
# 1791389413.287779: "also remove remove the hardcoded slot range."). The slot
# set is every MoP ``slots`` row with slot >= 1.


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


def slot_port(slot: int) -> int:
    return 3000 + slot


def slot_root(slot: int, address: str | None = None) -> str:
    """Clone root for a slot: the pane cwd's heydonna-app-NNNN root when tmux
    reports one, else the heydonna-app-<3000+slot> convention."""
    pane = pane_root(address) if address else None
    return pane or str(PROJECT_BASE / f"heydonna-app-{slot_port(slot)}")


def pane_root(address: str) -> str | None:
    try:
        result = run(["tmux", "display-message", "-p", "-t", address, "#{pane_current_path}"], timeout=2)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    path = Path(result.stdout.strip())
    try:
        first = path.relative_to(PROJECT_BASE).parts[0]
    except (ValueError, IndexError):
        return None
    return str(PROJECT_BASE / first) if re.fullmatch(r"heydonna-app-\d+", first) else None


def load_slot_inventory() -> tuple[dict[int, dict[str, Any]], str | None]:
    """Read the MoP slot inventory (every row with slot >= 1) before cleanup.

    A missing, unreadable or malformed inventory, or any malformed slot row, is
    an authority failure. Apply mode must refuse before it enumerates kill
    candidates.
    """
    if not MOP_DB.exists():
        return {}, f"missing:{MOP_DB}"
    try:
        result = run(
            [
                "sqlite3",
                "-json",
                str(MOP_DB),
                "SELECT slot,address,status,occupied,idle,dnd,activity,issue,task,repository_id,branch,branch_ref,pr,head_sha,work_kind,handoff_id,claimed_at,active_turn_state,active_turn_id,last_activity FROM slots WHERE slot >= 1 ORDER BY slot;",
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
        "slot", "address", "status", "occupied", "idle", "dnd", "activity", "issue", "task",
        "repository_id", "branch", "branch_ref", "pr", "head_sha", "work_kind",
        "handoff_id", "claimed_at", "active_turn_state", "active_turn_id", "last_activity",
    )
    for record in records:
        if not isinstance(record, dict) or any(field not in record for field in required_fields):
            ident = record.get("slot") if isinstance(record, dict) else None
            return {}, f"malformed_row:slot={ident}"
        try:
            slot = record["slot"]
            occupied = record["occupied"]
            idle = record["idle"]
            dnd = record["dnd"]
            if any(isinstance(value, bool) or not isinstance(value, int) for value in (slot, occupied, idle, dnd)):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            return {}, f"malformed_identity:slot={record.get('slot')}"
        if slot < 1 or slot in states:
            return {}, f"invalid_or_duplicate_slot:{slot}"
        status = record["status"]
        if not isinstance(status, str) or not status:
            return {}, f"malformed_status:slot={slot}"
        address = record["address"]
        if not isinstance(address, str) or not address:
            return {}, f"malformed_address:slot={slot}"
        states[slot] = {
            "slot": slot,
            "address": address,
            "root": slot_root(slot, address),
            "port": slot_port(slot),
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
    if not states:
        return {}, "no_slots"
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
    for state in states.values():
        if under(cwd, state["root"]):
            return state
    # A slot-convention clone with no readable MoP row is still a slot
    # checkout: keep it fail-closed rather than treating it as slot-less.
    match = re.match(re.escape(str(PROJECT_BASE)) + r"/heydonna-app-(\d+)(?:/|$)", str(cwd or ""))
    if match and int(match.group(1)) > 3000:
        slot = int(match.group(1)) - 3000
        return {
            "slot": slot,
            "root": str(PROJECT_BASE / f"heydonna-app-{match.group(1)}"),
            "port": slot_port(slot),
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
        }
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
    if "chrome-headless-shell" in lower or "ms-playwright" in lower or re.search(
        r"(^|[/\s])playwright(?:/cli\.js|/lib/\S+)?\s+(test|run-server|test-server)(\s|$)|playwright/lib/(?:cli|common)", lower
    ):
        return "playwright"
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
    if re.search(r"\(vitest(?: \d+)?\)|(^|[/\s])vitest(\s|$)|node_modules/vitest/", lower):
        return "vitest"
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
        if lower.startswith("next-server"):
            return (category, "next-server")
        match = re.search(
            r"(?<!\S)(\S*(?:next-server|node_modules/(?:\.bin/next|next/(?:dist/)?bin/next)))\s+(?:dev|start)(?:\s|$)",
            lower,
        )
        return (category, match.group(1)) if match else None
    if category == "tsc":
        match = re.search(r"(?<!\S)(\S*(?:tsc|typescript/bin/tsc))(?:\s|$)", lower)
        return (category, match.group(1)) if match and "--noemit" in lower else None
    if category == "vitest":
        return (category, "vitest")
    if category == "playwright":
        return (category, "playwright")
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


def listen_ports(pid: int) -> list[int]:
    """TCP ports the process is LISTENing on (empty when none or unreadable)."""
    try:
        result = run(["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN", "-Fn"], timeout=2)
    except Exception:
        return []
    ports: set[int] = set()
    for line in result.stdout.splitlines():
        match = re.match(r"n.*:(\d+)$", line)
        if match:
            ports.add(int(match.group(1)))
    return sorted(ports)


def listen_ports_strict(pid: int) -> list[int] | None:
    """LISTEN ports for a signal decision; None when the read fails or times out.

    lsof exits 1 with no output when the process has no matching sockets; any
    exception, other exit code, or stderr is unreadable and must fail closed.
    """
    try:
        result = run(["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN", "-Fn"], timeout=2)
    except Exception:
        return None
    if (result.stderr or "").strip():
        return None
    if result.returncode not in (0, 1) or (result.returncode == 1 and (result.stdout or "").strip()):
        return None
    ports: set[int] = set()
    for line in (result.stdout or "").splitlines():
        match = re.match(r"n.*:(\d+)$", line)
        if match:
            ports.add(int(match.group(1)))
    return sorted(ports)


def live_children_map() -> dict[int, list[int]] | None:
    """Current {ppid: [pid]} from the process table; None when unreadable."""
    try:
        result = run(["ps", "-axo", "pid=,ppid="], timeout=5)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    children: dict[int, list[int]] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
    return children


def duplicate_dev_server_reasons(rows: list[dict[str, Any]]) -> dict[int, str]:
    """Return {pid: reason} for Next.js dev-server trees to reap in slot clones.

    A tree is a top-most nextjs process plus its nextjs descendants (for
    example ``node .../next dev`` and its ``next-server`` child). Per slot clone:
      (a) when one tree listens on the slot PORT (3000+N), every other tree in
          that clone is a duplicate;
      (b) a tree listening only on ports other than the slot PORT (a stray
          :3000) is reaped even without a keeper.
    A tree younger than DUPLICATE_DEV_MIN_AGE_S is never flagged (a live turn
    may have just started it; a live Claude turn's active command is not
    reliably detectable from the process table, so the age floor is the guard).
    """
    nextjs = {row["pid"]: row for row in rows if row["category"] == "nextjs" and row.get("associated_slot")}
    children: dict[int, list[int]] = {}
    for pid, row in nextjs.items():
        children.setdefault(row["ppid"], []).append(pid)
    trees: dict[int, list[dict[str, Any]]] = {}
    for pid, row in nextjs.items():
        if row["ppid"] in nextjs:
            continue
        members, stack = [], [pid]
        while stack:
            current = stack.pop()
            members.append(nextjs[current])
            stack.extend(children.get(current, []))
        trees[pid] = members
    by_slot: dict[int, list[tuple[int, list[dict[str, Any]], set[int]]]] = {}
    for root, members in trees.items():
        ports = {port for member in members for port in member.get("listen_ports") or []}
        by_slot.setdefault(nextjs[root]["associated_slot"], []).append((root, members, ports))
    reasons: dict[int, str] = {}
    for slot, slot_trees in by_slot.items():
        port = slot_port(slot)
        keepers = [root for root, _members, ports in slot_trees if port in ports]
        # Keep the oldest tree when the OS reports more than one on the PORT.
        keeper = max(keepers, key=lambda r: nextjs[r]["age_seconds"]) if keepers else None
        for root, members, ports in slot_trees:
            if root == keeper:
                continue
            if min(m["age_seconds"] for m in members) < DUPLICATE_DEV_MIN_AGE_S:
                continue
            if port in ports:
                reason = f"duplicate_dev_server:slot={slot}:second_tree_on_port_{port}:keeper_pid={keeper}"
            elif ports:
                reason = f"wrong_port_dev_server:slot={slot}:listening={','.join(map(str, sorted(ports)))}:slot_port={port}"
            elif keeper is not None:
                reason = f"duplicate_dev_server:slot={slot}:not_listening:keeper_pid={keeper}"
            else:
                continue
            for member in members:
                reasons[member["pid"]] = reason
    return reasons


def parse_cpu_time(value: str) -> int | None:
    """ps TIME ([H:]MM:SS.cc) to centiseconds."""
    try:
        parts = value.strip().split(":")
        secs = float(parts[-1])
        mins = int(parts[-2]) if len(parts) >= 2 else 0
        hours = int(parts[-3]) if len(parts) >= 3 else 0
        return int(round((hours * 3600 + mins * 60 + secs) * 100))
    except (ValueError, IndexError):
        return None


def cpu_times() -> dict[int, int]:
    """{pid: cumulative CPU centiseconds} for all processes."""
    try:
        result = run(["ps", "-axo", "pid=,time="], timeout=5)
    except Exception:
        return {}
    out: dict[int, int] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        cs = parse_cpu_time(parts[1])
        if cs is not None:
            out[pid] = cs
    return out


def is_agent_browser_daemon(command: str) -> bool:
    lower = command.lower()
    if "chrome for testing" in lower or "chromium" in lower:
        return False
    return "agent-browser-darwin" in lower or "agent-browser-linux" in lower or "/agent-browser/bin/" in lower


def load_history(path: Path = HISTORY_JSON) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except Exception:
        return {}


def save_history(doc: dict[str, Any], path: Path = HISTORY_JSON) -> None:
    try:
        path.write_text(json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        pass


def idle_seconds_for_daemons(
    daemons: dict[str, int], history: dict[str, Any], now: float
) -> dict[str, int]:
    """Idle seconds per daemon key ``pid:start`` = time since its cumulative CPU last changed.

    The daemon only burns CPU while it serves commands, so a flat CPU counter is
    the idle signal. History is mutated in place (new keys start idle at 0;
    dead keys are pruned).
    """
    tracked = history.setdefault("daemons", {})
    for key in [k for k in tracked if k not in daemons]:
        del tracked[key]
    idle: dict[str, int] = {}
    for key, cpu in daemons.items():
        entry = tracked.get(key)
        if not isinstance(entry, dict) or entry.get("cpu") != cpu:
            tracked[key] = {"cpu": cpu, "changed": now}
            idle[key] = 0
        else:
            idle[key] = int(max(0, now - float(entry.get("changed", now))))
    return idle


def apply_idle_orphan_policy(
    rows: list[dict[str, Any]],
    idle_by_pid: dict[int, int],
    ab_idle_min: int,
    dev_tool_min: int,
    dead_session_min: int = 60,
) -> None:
    """Mark agent-browser idle-daemon trees and orphaned vitest/tsc trees eligible.

    * idle agent-browser: a daemon whose CPU counter has been flat for
      ab_idle_min, plus its Chrome tree (descendants, and reparented Chrome for
      Testing helpers in the same cwd started within 5 min of the daemon). This
      applies even when the owning slot is active: the daemon restarts on the
      next agent-browser command, an idle one only wastes CPU/RAM.
    * orphan dev tool: vitest/tsc whose parent is init (parent shell/session is
      dead), older than dev_tool_min, plus their descendants. A live parent
      (a session, npm, a shell) always protects the process.
    """
    by_pid = {r["pid"]: r for r in rows}
    kids: dict[int, list[int]] = {}
    for r in rows:
        kids.setdefault(r["ppid"], []).append(r["pid"])

    def mark(root: dict[str, Any], members: list[dict[str, Any]], policy: str, reason: str) -> None:
        for m in members:
            if not m.get("start_time") or m.get("command_identity") is None:
                continue
            if m["category"] == "agent-browser" and "google chrome.app/contents/macos/google chrome" in m["command"].lower():
                continue
            m.update(eligible=True, skip_reason=None, policy=policy, reap_reason=reason,
                     tree_root=root["pid"])

    def descendants(pid: int) -> list[dict[str, Any]]:
        out, stack, seen = [], list(kids.get(pid, [])), set()
        while stack:
            cur = stack.pop()
            if cur in seen or cur not in by_pid:
                continue
            seen.add(cur)
            out.append(by_pid[cur])
            stack.extend(kids.get(cur, []))
        return out

    for r in rows:
        if r.get("is_daemon") and r["pid"] in idle_by_pid and idle_by_pid[r["pid"]] >= ab_idle_min * 60 \
                and r["age_seconds"] >= ab_idle_min * 60:
            members = [r] + descendants(r["pid"])
            member_pids = {m["pid"] for m in members}
            for o in rows:
                if (o["pid"] not in member_pids and o["category"] == "agent-browser" and o["ppid"] == 1
                        and not o.get("is_daemon") and o["cwd"] == r["cwd"]
                        and abs(o["age_seconds"] - r["age_seconds"]) <= 300
                        and "chrome for testing" in o["command"].lower()):
                    members.append(o)
            mark(r, members, "idle_agent_browser",
                 f"idle_agent_browser_daemon:cpu_flat={idle_by_pid[r['pid']] // 60}m")
    for r in rows:
        if r["category"] == "playwright" and r["ppid"] == 1 and r["age_seconds"] >= PLAYWRIGHT_ORPHAN_MIN * 60:
            members = [r] + [d for d in descendants(r["pid"]) if d["category"] == "playwright"]
            mark(r, members, "orphan_dev_tool", f"orphan_playwright:ppid=1:age={r['age']}")
    for r in rows:
        if (r["category"] in ("nextjs", "convex", "tsc") and r.get("associated_slot") and not r.get("eligible")
                and not r.get("slot_free_idle") and r["age_seconds"] >= dead_session_min * 60
                and not slot_has_live_session(r["associated_slot"])):
            mark(r, [r], "dead_slot_session", f"no_live_session_in_slot_{r['associated_slot']}:age={r['age']}")
    for r in rows:
        if r["category"] in ("vitest", "tsc") and r["ppid"] == 1 and r["age_seconds"] >= dev_tool_min * 60:
            members = [r] + [d for d in descendants(r["pid"]) if d["category"] in ("vitest", "tsc")]
            mark(r, members, "orphan_dev_tool", f"orphan_{r['category']}:ppid=1:age={r['age']}")


def replayd_streak_update(history: dict[str, Any], pid: int | None, start: str | None, cpu: float | None) -> int:
    """Consecutive-run high-CPU counter for replayd; resets on low CPU or a new pid."""
    entry = history.setdefault("replayd", {})
    if pid is None or cpu is None:
        entry.clear()
        return 0
    key = f"{pid}:{start}"
    if entry.get("key") != key:
        entry.clear()
        entry["key"] = key
        entry["streak"] = 0
    entry["streak"] = int(entry.get("streak", 0)) + 1 if cpu > REPLAYD_CPU_THRESHOLD else 0
    return entry["streak"]


def find_replayd() -> tuple[int | None, str | None, float | None]:
    """User-level replayd (pid, lstart, %cpu); never a different user's or any other process."""
    try:
        result = run(["ps", "-axo", "pid=,uid=,%cpu=,command="], timeout=5)
    except Exception:
        return None, None, None
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4 or parts[3].strip() != "/usr/libexec/replayd":
            continue
        try:
            pid, uid, cpu = int(parts[0]), int(parts[1]), float(parts[2])
        except ValueError:
            continue
        if uid == os.getuid():
            return pid, get_start_time(pid), cpu
    return None, None, None


def replayd_check(history: dict[str, Any], apply: bool) -> dict[str, Any]:
    pid, start, cpu = find_replayd()
    if not apply:
        return {"pid": pid, "cpu": cpu, "action": "scan_only"}
    streak = replayd_streak_update(history, pid, start, cpu)
    info: dict[str, Any] = {"pid": pid, "cpu": cpu, "streak": streak, "action": "none"}
    if pid is not None and streak >= REPLAYD_STREAK_RESTART:
        snap = _process_snapshot(pid)
        if snap and snap["command"].strip() == "/usr/libexec/replayd" and snap["uid"] == os.getuid() \
                and snap["start_time"] == start:
            try:
                os.kill(pid, signal.SIGTERM)
                info["action"] = "restarted_sigterm"
                time.sleep(2)
                if alive(pid) and _process_snapshot(pid) and _process_snapshot(pid)["start_time"] == start:
                    os.kill(pid, signal.SIGKILL)
                    info["action"] = "restarted_sigkill"
                history["replayd"] = {}
            except (ProcessLookupError, PermissionError) as exc:
                info["action"] = f"restart_failed:{type(exc).__name__}"
        else:
            info["action"] = "restart_refused_identity"
    return info


def collect_processes(
    agent_browser_min: int,
    nextjs_min: int,
    dev_tool_min: int,
    slot_inventory: tuple[dict[int, dict[str, Any]], str | None] | None = None,
    ab_idle_min: int = AB_IDLE_MIN_DEFAULT,
    dev_tool_min_orphan: int | None = None,
) -> dict[str, Any]:
    if dev_tool_min_orphan is None:
        dev_tool_min_orphan = dev_tool_min
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
    cpu_map = cpu_times()
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
        if category == "vitest":
            # Only an orphaned vitest tree is ever eligible (apply_idle_orphan_policy).
            eligible = False
            skip_reason = "vitest_parent_alive_or_young"
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
        if cwd and cwd.startswith(PM_WORKTREE_PREFIX) and age_seconds < PM_WORKTREE_MIN_AGE_S:
            eligible = False
            skip_reason = "pm_worktree_younger_than_60m"
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
            "policy": "stale_owner" if eligible else None,
            "listen_ports": listen_ports(pid) if category == "nextjs" and slot_number else [],
            "is_daemon": category == "agent-browser" and is_agent_browser_daemon(command),
            "cpu_centis": cpu_map.get(pid),
        }
        rows.append(row)

    daemons = {
        f"{r['pid']}:{r['start_time']}": r["cpu_centis"]
        for r in rows if r["is_daemon"] and r["cpu_centis"] is not None and r["start_time"]
    }
    history = load_history()
    idle_by_key = idle_seconds_for_daemons(daemons, history, time.time())
    if PERSIST_HISTORY:
        history["updated"] = datetime.now(timezone.utc).isoformat()
        save_history(history)
    idle_by_pid = {
        r["pid"]: idle_by_key.get(f"{r['pid']}:{r['start_time']}", 0) for r in rows if r["is_daemon"]
    }
    for r in rows:
        if r["is_daemon"]:
            r["idle_seconds"] = idle_by_pid[r["pid"]]
    apply_idle_orphan_policy(rows, idle_by_pid, ab_idle_min, dev_tool_min_orphan, nextjs_min)

    report_only: list[dict[str, Any]] = []
    if not inventory_error:
        duplicate_reasons = duplicate_dev_server_reasons(rows)
        for row in rows:
            reason = duplicate_reasons.get(row["pid"])
            if not reason:
                continue
            if row["eligible"]:
                if row.get("policy") not in IDLE_ORPHAN_POLICIES:
                    row["reap_reason"] = reason
                continue
            # Duplicate/wrong-port status never overrides the ownership hold.
            # Age does not prove non-use: unless the slot is proven free+idle
            # with no assignment, the tree is retained and only reported.
            proven_free = bool(
                row["start_time"]
                and row["slot_free_idle"]
                and row["owner_proof"]
                and not row["slot_state_missing"]
                and row["skip_reason"] != "pm_worktree_younger_than_60m"
            )
            if proven_free:
                row.update(eligible=True, policy="duplicate_dev_server", reap_reason=reason, skip_reason=None)
            else:
                row["report_only_reason"] = reason
                report_only.append(
                    {"pid": row["pid"], "associated_slot": row["associated_slot"], "reason": reason,
                     "hold": row["skip_reason"] or "slot_not_proven_free_idle"}
                )

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
        "report_only": report_only,
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


_LIVE_OWNER_RE = re.compile(r"claude|codex|master-of-panes|mop|slack-bridge|cto-bridge|tmux", re.I)


PLAYWRIGHT_ORPHAN_MIN = 60
_SESSION_RE = re.compile(r"(^|/)(claude|openclaude)(\s|$)|codex-darwin|/bin/codex(\s|$)")


def slot_has_live_session(slot: int) -> bool:
    """True if any claude/openclaude/codex process has its cwd in the slot checkout.
    Fails closed (True) when the process list or a cwd cannot be read."""
    root = slot_root(slot)
    try:
        out = run(["ps", "-axo", "pid=,uid=,command="], timeout=5).stdout
    except Exception:
        return True
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3 or not _SESSION_RE.search(parts[2].split(" --")[0]):
            continue
        if int(parts[1]) != os.getuid():
            continue
        cwd = get_cwd(int(parts[0]))
        if cwd and under(cwd, root):
            return True
    return False


def _owner_dead(pid: int) -> bool:
    """True when no ancestor of pid is a live agent/session/service process."""
    seen = set()
    cur = pid
    while cur > 1 and cur not in seen:
        seen.add(cur)
        try:
            out = run(["ps", "-p", str(cur), "-o", "ppid=,command="], timeout=2).stdout.strip()
        except Exception:
            return False
        if not out:
            break
        ppid_s, _, cmd = out.partition(" ")
        if cur != pid and _LIVE_OWNER_RE.search(cmd) and "agent-browser" not in cmd.lower():
            return False
        try:
            cur = int(ppid_s)
        except ValueError:
            return False
    return True


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
    duplicate = row.get("policy") == "duplicate_dev_server"
    idle_orphan = row.get("policy") in IDLE_ORPHAN_POLICIES
    for key in ("pid", "ppid", "uid", "start_time", "cwd"):
        # A tree member reparents to launchd once its parent is reaped.
        if idle_orphan and key == "ppid" and current.get(key) == 1:
            continue
        # Reaping a duplicate tree root first reparents its children to
        # launchd; that is our own effect, not an identity change.
        if duplicate and key == "ppid" and current.get(key) == 1:
            continue
        if current.get(key) != row.get(key):
            return False, f"process_identity_changed:{key}"
    if idle_orphan:
        # Does not depend on slot ownership: idleness / orphanhood is the proof.
        if row["policy"] == "idle_agent_browser" and row.get("is_daemon"):
            now_cpu = cpu_times().get(pid)
            if now_cpu is None or now_cpu != row.get("cpu_centis"):
                return False, "daemon_no_longer_idle"
        if row["policy"] == "dead_slot_session" and slot_has_live_session(int(row["associated_slot"])):
            return False, "slot_session_alive"
        if row["policy"] == "orphan_dev_tool" and row.get("tree_root") == pid and current.get("ppid") != 1:
            return False, "orphan_gained_parent"
        return True, f"{row['policy']}_identity_match"
    states, inventory_error = load_slot_inventory()
    if inventory_error:
        return False, f"slot_inventory_unavailable:{inventory_error}"
    slot_state = associated_slot(current.get("cwd"), states)
    if duplicate:
        if not slot_state or slot_state.get("slot") != row.get("associated_slot"):
            return False, "slot_owner_changed"
        if not stale_owner_evidence(slot_state):
            return False, "slot_owner_evidence_changed"
        ports = listen_ports_strict(pid)
        if ports is None:
            return False, "listener_read_failed"
        if slot_port(int(row["associated_slot"])) in ports:
            return False, "duplicate_now_owns_slot_port"
        return True, "duplicate_dev_server_identity_match"
    if row.get("associated_slot") is None:
        if slot_state is not None:
            return False, "slot_owner_changed"
        if row.get("owner_proof") is not None:
            return False, "slot_owner_evidence_changed"
        if not allow_slotless_attested:
            return False, "slotless_reap_not_attested"
        # Owner must be dead: no live claude/codex/agent/bridge/MoP ancestor.
        if not _owner_dead(pid):
            return False, "slotless_owner_alive"
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


def candidate_trees(candidates: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group candidates into trees (a candidate whose parent is a candidate joins it)."""
    by_pid = {int(row["pid"]): row for row in candidates}
    def root_of(pid: int) -> int:
        seen = set()
        while by_pid[pid].get("ppid") in by_pid and pid not in seen:
            seen.add(pid)
            pid = int(by_pid[pid]["ppid"])
        return pid
    trees: dict[int, list[dict[str, Any]]] = {}
    for row in candidates:
        trees.setdefault(root_of(int(row["pid"])), []).append(row)
    return list(trees.values())


def revalidate_tree(
    members: list[dict[str, Any]], allow_slotless_attested: bool = False
) -> tuple[bool, str, int | None, str | None]:
    """Fail-closed check of a whole selected tree before its FIRST signal.

    Every candidate member must still match its identity (incl. start time)
    and ownership. Every PID in the live tree (members plus their current
    descendants) must have a readable listener set, and none may listen on
    the slot's canonical port. Any failure skips the whole tree.
    """
    for row in members:
        valid, reason = revalidate_candidate(row, allow_slotless_attested)
        if not valid:
            return False, f"tree_member_refused:pid={row['pid']}:{reason}", int(row["pid"]), reason
    children = live_children_map()
    if children is None:
        return False, "tree_process_table_unreadable", None, None
    tree_pids: set[int] = set()
    stack = [int(row["pid"]) for row in members]
    while stack:
        pid = stack.pop()
        if pid in tree_pids:
            continue
        tree_pids.add(pid)
        stack.extend(children.get(pid, []))
    slots = {row.get("associated_slot") for row in members if row.get("associated_slot") is not None}
    canonical = {slot_port(int(slot)) for slot in slots}
    for pid in sorted(tree_pids):
        ports = listen_ports_strict(pid)
        if ports is None:
            return False, f"tree_listener_read_failed:pid={pid}", None, None
        hit = canonical.intersection(ports)
        if hit:
            return False, f"tree_contains_canonical_listener:pid={pid}:port={min(hit)}", None, None
    return True, "tree_identity_owner_listener_ok", None, None


def kill_candidates(
    candidates: list[dict[str, Any]],
    wait_s: float,
    allow_slotless_attested: bool = False,
) -> list[dict[str, Any]]:
    """Revalidate each candidate tree as a whole, then signal its members."""
    results: list[dict[str, Any]] = []
    for members in candidate_trees(candidates):
        ok, reason, bad_pid, bad_reason = revalidate_tree(members, allow_slotless_attested)
        if not ok:
            for row in members:
                status, why = "tree_refused", reason
                if int(row["pid"]) == bad_pid:
                    why = bad_reason
                    status = (
                        "attestation_required_refused"
                        if bad_reason == "slotless_reap_not_attested"
                        else "identity_or_owner_refused"
                    )
                results.append({
                    "pid": int(row["pid"]), "category": row["category"],
                    "associated_slot": row.get("associated_slot"), "term_sent": False, "kill_sent": False,
                    "status": status, "reason": why, "tree_reason": reason, "outcome": "refused",
                })
            continue
        results.extend(_kill_members(members, wait_s, allow_slotless_attested))
    return results


def _kill_members(
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
        if row.get("associated_slot") is None and row.get("policy") not in IDLE_ORPHAN_POLICIES:
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


def write_status(
    path: Path,
    *,
    mode: str,
    refused: bool,
    reason: str | None,
    summary: dict[str, Any] | None,
    report_only: list[dict[str, Any]] | None = None,
    tree_refused: list[dict[str, Any]] | None = None,
) -> None:
    """Status line the hourly heartbeat reads (heartbeat-compose.py)."""
    doc = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "refused": refused,
        "error": bool(summary and summary.get("errors")),
        "reason": reason,
        "summary": summary,
        # Retained duplicate/wrong-port trees on slots not proven free+idle.
        "report_only": report_only or [],
        # Trees skipped by the whole-tree fail-closed check before any signal.
        "tree_refused": tree_refused or [],
    }
    try:
        path.write_text(json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        pass


RUNAWAY_MIN_AGE_S = 5 * 60
RUNAWAY_TOOLS = {"grep", "ggrep", "egrep", "fgrep", "rg", "ripgrep", "ugrep", "find", "bfs", "fd", "fdfind"}
RUNAWAY_NEVER = {"git", "node", "tsc", "vitest", "npm", "npx", "bun", "python", "python3", "next", "convex"}
RUNAWAY_HOME = "/Users/rajiv"


def runaway_classify(command: str, age_seconds: int | None) -> str | None:
    """Return a reason when `command` is a recursive grep/rg/find/fd under $HOME running > 5 min."""
    import shlex
    if age_seconds is None or age_seconds < RUNAWAY_MIN_AGE_S:
        return None
    try:
        toks = shlex.split(command)
    except ValueError:
        toks = command.split()
    if not toks:
        return None
    name = os.path.basename(toks[0])
    if name in RUNAWAY_NEVER or name not in RUNAWAY_TOOLS:
        return None
    args = toks[1:]
    if name in {"grep", "ggrep", "egrep", "fgrep"}:
        recursive = any(a in ("--recursive", "--dereference-recursive") or
                        (a.startswith("-") and not a.startswith("--") and re.search("[rR]", a)) for a in args)
    else:
        recursive = True  # rg/find/fd recurse by default
    if not recursive:
        return None
    if not any(a == RUNAWAY_HOME or a.startswith(RUNAWAY_HOME + "/") or a == "~" or a.startswith("~/")
               or a.startswith("$HOME") for a in args):
        return None
    return f"{name}_recursive_under_home_over_{RUNAWAY_MIN_AGE_S // 60}m"


def runaway_scan(apply: bool, term_wait_s: float = 3.0) -> dict[str, Any]:
    """Independent of slot ownership: kill runaway recursive scans (SIGTERM then SIGKILL)."""
    out: dict[str, Any] = {"class": "runaway_scan", "found": [], "terminated": [], "errors": []}
    try:
        ps = run(["ps", "-axo", "pid=,ppid=,etime=,command="], timeout=10).stdout
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"ps_failed:{type(exc).__name__}")
        return out
    me = os.getpid()
    for line in ps.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4 or not parts[0].isdigit() or int(parts[0]) == me:
            continue
        pid, command = int(parts[0]), parts[3]
        reason = runaway_classify(command, parse_etime(parts[2]))
        if not reason:
            continue
        rec = {"pid": pid, "ppid": int(parts[1]), "etime": parts[2], "command": command[:300],
               "reason": reason, "term_sent": False, "kill_sent": False}
        out["found"].append(rec)
        if not apply:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            rec["term_sent"] = True
            deadline = time.time() + term_wait_s
            while time.time() < deadline and alive(pid):
                time.sleep(0.2)
            if alive(pid):
                os.kill(pid, signal.SIGKILL)
                rec["kill_sent"] = True
            out["terminated"].append(rec)
        except ProcessLookupError:
            rec["already_exited"] = True
        except OSError as exc:
            out["errors"].append(f"{pid}:{exc}")
    return out


def run_self_test() -> int:
    """Pure-function policy tests (no process is touched)."""
    def row(pid, ppid, cat, age_min, cwd="/x", daemon=False, cmd=None, cpu=100):
        return {"pid": pid, "ppid": ppid, "category": cat, "age_seconds": age_min * 60, "age": fmt_age(age_min * 60),
                "cwd": cwd, "start_time": "t", "command": cmd or cat, "command_identity": (cat, cat),
                "eligible": False, "skip_reason": "held_or_preserved_slot_checkout", "policy": None,
                "is_daemon": daemon, "cpu_centis": cpu, "associated_slot": 7}

    # classification
    assert classify("node (vitest 1)") == "vitest"
    assert classify("node (vitest)") == "vitest"
    assert classify("npm exec vitest run foo.test.ts") == "vitest"
    assert classify("node /a/node_modules/vitest/dist/cli.js run") == "vitest"
    assert classify("node /a/node_modules/.bin/tsc --noEmit") == "tsc"
    assert classify("/usr/libexec/replayd") is None
    assert is_agent_browser_daemon("/x/lib/node_modules/agent-browser/bin/agent-browser-darwin-arm64")
    assert not is_agent_browser_daemon("/Users/x/.agent-browser/browsers/c/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")
    assert parse_cpu_time("0:07.27") == 727 and parse_cpu_time("1:02:03.50") == 372350

    # idle history
    hist: dict[str, Any] = {}
    assert idle_seconds_for_daemons({"1:a": 5}, hist, 1000) == {"1:a": 0}
    assert idle_seconds_for_daemons({"1:a": 5}, hist, 1000 + 7300) == {"1:a": 7300}
    assert idle_seconds_for_daemons({"1:a": 6}, hist, 1000 + 7400) == {"1:a": 0}  # CPU moved: active again
    idle_seconds_for_daemons({}, hist, 9999)
    assert hist["daemons"] == {}

    # idle daemon tree is reaped even under an active slot; busy/young daemon is kept
    chrome_main = row(11, 10, "agent-browser", 300, cmd="Google Chrome for Testing --remote-debugging-port=0")
    helper = row(12, 1, "agent-browser", 300, cmd="Google Chrome for Testing Helper")
    other_cwd_helper = row(13, 1, "agent-browser", 300, cwd="/other", cmd="Google Chrome for Testing Helper")
    d_idle = row(10, 1, "agent-browser", 300, daemon=True)
    rows = [d_idle, chrome_main, helper, other_cwd_helper]
    apply_idle_orphan_policy(rows, {10: 7300}, 120, 30)
    assert [r["eligible"] for r in rows] == [True, True, True, False], rows
    assert d_idle["policy"] == "idle_agent_browser"
    d_busy = row(20, 1, "agent-browser", 300, daemon=True)
    c_busy = row(21, 20, "agent-browser", 300)
    apply_idle_orphan_policy([d_busy, c_busy], {20: 600}, 120, 30)
    assert not d_busy["eligible"] and not c_busy["eligible"]

    # orphan vitest/tsc: ppid 1 + age; live parent or young never
    v_root = row(30, 1, "vitest", 45)
    v_kid = row(31, 30, "vitest", 45)
    t_orphan = row(32, 1, "tsc", 90)
    t_live = row(33, 999, "tsc", 90)
    v_young = row(34, 1, "vitest", 10)
    rows = [v_root, v_kid, t_orphan, t_live, v_young]
    apply_idle_orphan_policy(rows, {}, 120, 30)
    assert [r["eligible"] for r in rows] == [True, True, True, False, False], rows

    # replayd streak: 2 consecutive high runs, reset on low CPU or new pid
    h: dict[str, Any] = {}
    assert replayd_streak_update(h, 5, "s", 80.0) == 1
    assert replayd_streak_update(h, 5, "s", 80.0) == 2
    assert replayd_streak_update(h, 5, "s", 10.0) == 0
    assert replayd_streak_update(h, 5, "s", 80.0) == 1
    assert replayd_streak_update(h, 6, "s2", 80.0) == 1
    # runaway_scan classification
    g = "grep -R -l -E 'a|b' /Users/rajiv/Downloads/projects"
    assert runaway_classify(g, 400)
    assert not runaway_classify(g, 100)  # too young
    assert runaway_classify("rg foo /Users/rajiv/Downloads", 900)
    assert runaway_classify("find /Users/rajiv -name x", 900)
    assert not runaway_classify("grep foo /Users/rajiv/file.txt", 900)  # not recursive
    assert not runaway_classify("grep -r foo /tmp/x", 900)  # root not under HOME
    for never in ("git grep -r foo /Users/rajiv/x", "node /Users/rajiv/a.js", "tsc --noEmit /Users/rajiv/x",
                  "vitest run /Users/rajiv/x"):
        assert not runaway_classify(never, 9000), never
    print("SELF_TEST ok")
    return 0


def main() -> int:
    try:
        return _main()
    except Exception as exc:
        write_status(STATUS_JSON, mode="unknown", refused=True, reason=f"crash:{type(exc).__name__}:{str(exc)[:200]}", summary=None)
        raise


def _main() -> int:
    parser = argparse.ArgumentParser(description="Scan/apply stale allowlisted process cleanup")
    parser.add_argument("--apply", action="store_true", help="send TERM then KILL to eligible allowlisted PIDs")
    parser.add_argument("--output", default=str(OUT_JSON), help="proof JSON output path")
    parser.add_argument("--agent-browser-min", type=int, default=60)
    parser.add_argument("--nextjs-min", type=int, default=60)
    parser.add_argument("--dev-tool-min", type=int, default=30)
    parser.add_argument("--ab-idle-min", type=int, default=AB_IDLE_MIN_DEFAULT,
                        help="reap an agent-browser daemon (+Chrome tree) whose CPU counter has been flat this long")
    parser.add_argument("--self-test", action="store_true", help="run built-in policy tests and exit")
    parser.add_argument("--term-wait-s", type=float, default=3.0)
    parser.add_argument("--status-output", default=str(STATUS_JSON), help="heartbeat status JSON path")
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
    if args.self_test:
        return run_self_test()
    global PERSIST_HISTORY
    PERSIST_HISTORY = bool(args.apply)

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
        before = collect_processes(args.agent_browser_min, args.nextjs_min, args.dev_tool_min, inventory, args.ab_idle_min)
        if args.apply and before.get("ok"):
            outcomes = kill_candidates(
                before.get("candidates", []),
                args.term_wait_s,
                allow_slotless_attested=args.attest_slotless_reap,
            )
        after = collect_processes(args.agent_browser_min, args.nextjs_min, args.dev_tool_min, None, args.ab_idle_min)

    runaway = runaway_scan(args.apply, args.term_wait_s)
    history = load_history()
    replayd = replayd_check(history, args.apply)
    if args.apply:
        save_history(history)
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
            "slot_set": "MoP slots rows with slot >= 1 (no hard-coded range)",
            "duplicate_dev_server": "per slot clone keep the tree on PORT 3000+N; reap other trees and wrong-port trees older than 10m only when the slot is proven free+idle; otherwise report-only. Whole tree revalidated (identity, owner, readable listeners, no canonical listener) before its first signal",
            "ab_idle_min": args.ab_idle_min,
            "idle_agent_browser": "daemon CPU counter flat >= ab_idle_min (history in state-stale-cleanup-history.json, written on apply runs) -> reap daemon + Chrome tree even when the slot is active",
            "dead_slot_session": "nextjs/convex/tsc in a HELD slot checkout older than nextjs_min when no claude/openclaude/codex process has its cwd in that checkout (revalidated before signal)",
            "orphan_playwright": "playwright runner / chrome-headless-shell / ms-playwright chromium with ppid 1 older than 60m (+descendants)",
            "orphan_dev_tool": "vitest/tsc with ppid 1 older than dev_tool_min (+descendants); a live parent protects",
            "replayd": "user-level /usr/libexec/replayd SIGTERM (launchd restarts it) after >50% CPU on 2 consecutive apply runs",
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
        "report_only": before.get("report_only", []),
        "replayd": replayd,
        "runaway_scan": runaway,
        "after": after,
    }
    Path(args.output).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    refusal = None if before.get("ok") else str(before.get("error", "unknown"))
    write_status(
        Path(args.status_output),
        mode=data["mode"],
        refused=refusal is not None,
        reason=refusal or (f"{len(errors)} kill errors" if errors else None),
        summary=data["summary"],
        report_only=data["report_only"],
        tree_refused=[
            {"pid": item["pid"], "associated_slot": item.get("associated_slot"), "reason": item.get("tree_reason") or item.get("reason")}
            for item in refused if item.get("tree_reason")
        ],
    )

    print(f"STALE_PROCESS_CANDIDATES count={before_count} proof={args.output}")
    if refusal:
        print(f"STALE_PROCESS_CLEANUP_REFUSED reason={refusal}")
    for row in before.get("candidates", [])[:50]:
        print(
            f"PROCESS pid={row['pid']} category={row['category']} age={row['age']} "
            f"cwd={row.get('cwd') or 'unknown'} slot={row.get('associated_slot') or 'none'} "
            f"slot_free_idle={row['slot_free_idle']} policy={row.get('policy')} "
            f"reason={row.get('reap_reason') or row.get('owner_proof') or 'stale_owner'}"
        )
    print(f"REPLAYD pid={replayd.get('pid')} cpu={replayd.get('cpu')} streak={replayd.get('streak')} action={replayd['action']}")
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
