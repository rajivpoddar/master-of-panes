#!/usr/bin/env python3
"""PM Claude RSS line for the hourly, and a once-per-6h PM-only exit-pending arm.

Rajiv DM D0AMF0XE6TS thread 1794921293.133799, ts 1794921896.970909:
"track the claude process memory usage in hourlies and set exit pending automatically".

The arm is the existing POST /exit-pending path daily-pm-restart uses
(pm_only, continuation prompt, Stop-hook pm_save_handoff, /exit at the next
Stop). with_devs is false: a memory restart must not cycle dev slots
(2026-11-09 global exit-pending restarted a slot mid-task). PM-only is already
supported, so this file does not change MoP server code.

Fail open. Never gates PM input, never touches hooks or settings.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PM_PANE = "0:0.0"
THRESHOLD_BYTES = 2 * 1024 * 1024 * 1024
PX_FLOOR_BYTES = (3 * 1024 * 1024 * 1024) // 2
COOLDOWN_S = 6 * 3600
STATE_NAME = "pm-claude-memory-exit.json"
LOG_NAME = "pm-claude-memory-exit.log"
EXIT_URL = "http://127.0.0.1:3100/exit-pending"
STATUS_URL = "http://127.0.0.1:3100/exit-status"
CLAUDE_RE = re.compile(r"(^|/)(claude|openclaude)(\s|$)")
PX_NAME_RE = re.compile(r"(?:^|\s)--name(?:=|\s+)(px-[\w.-]+)(?=\s|$)")
CONTINUATION = (
    "PM Claude RSS crossed the memory threshold and MoP armed exit-pending. "
    "Read memory/MEMORY-session-handoff-current.md and data/pm-continuation-note.md. "
    "Resume from the canonical ledger. Reconcile live background agents before relaunching any; "
    "do not duplicate running work."
)

NOTES = {
    "below": "",
    "unknown": "",
    "would_arm": "; exit-pending dry-run would arm",
    "armed": "; exit-pending armed",
    "cooldown": "; exit-pending cooldown",
    "already_pending": "; exit-pending already pending",
    "unreachable": "; exit-pending not armed (MoP unreachable)",
    "uncertain": "; exit-pending uncertain (fail open)",
    "refused": "; exit-pending refused (fail open)",
}


def threshold_bytes(env: dict | None = None) -> int:
    raw = (env if env is not None else os.environ).get("MOP_PM_RSS_EXIT_BYTES")
    if raw is None or raw == "":
        return THRESHOLD_BYTES
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return THRESHOLD_BYTES
    return value if value > 0 else THRESHOLD_BYTES


def format_gib(n: int) -> str:
    return f"{n / (1024 ** 3):.2f}GB"


def parse_ps_table(text: str) -> dict[int, tuple[int, int, str]]:
    """pid -> (ppid, rss_kib, command) from `ps -axo pid=,ppid=,rss=,command=`."""
    rows: dict[int, tuple[int, int, str]] = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) != 4:
            continue
        pid, ppid, rss, command = parts
        if pid.isdigit() and ppid.isdigit() and rss.isdigit():
            rows[int(pid)] = (int(ppid), int(rss), command)
    return rows


def _is_claude(command: str) -> bool:
    token = command.split(None, 1)[0] if command else ""
    return CLAUDE_RE.search(token) is not None


def nearest_claude(root: int, procs: dict[int, tuple[int, int, str]]) -> tuple[int, int] | None:
    """(pid, rss_bytes) of the claude closest to the pane root, not a nested subagent."""
    children: dict[int, list[int]] = {}
    for pid, (ppid, _rss, _cmd) in procs.items():
        children.setdefault(ppid, []).append(pid)
    pending = [root]
    seen: set[int] = set()
    while pending:
        pid = pending.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        row = procs.get(pid)
        if row and _is_claude(row[2]):
            return pid, row[1] * 1024
        pending.extend(children.get(pid, []))
    return None


def px_over(procs: dict[int, tuple[int, int, str]], floor_bytes: int = PX_FLOOR_BYTES) -> list[tuple[str, int, int]]:
    """(name, pid, rss_bytes) for px-* claude processes at or above the floor, largest first."""
    found: list[tuple[str, int, int]] = []
    for pid, (_ppid, rss_kib, command) in procs.items():
        if not _is_claude(command):
            continue
        match = PX_NAME_RE.search(command)
        if not match:
            continue
        rss = rss_kib * 1024
        if rss >= floor_bytes:
            found.append((match.group(1), pid, rss))
    found.sort(key=lambda row: (-row[2], row[0], row[1]))
    return found


def format_line(pm: tuple[int, int] | None, px: list[tuple[str, int, int]], note: str) -> str:
    if pm is None:
        pm_text = "PM unknown"
    else:
        pid, rss = pm
        pm_text = f"PM {format_gib(rss)} pid {pid}"
    if not px:
        px_text = "none"
    else:
        shown = px[:8]
        parts = [f"{name} {format_gib(rss)} pid {pid}" for name, pid, rss in shown]
        extra = len(px) - len(shown)
        px_text = ", ".join(parts) + (f", +{extra} more" if extra else "")
    return f"*Claude RSS:* {pm_text}; px over 1.5GB: {px_text}{NOTES[note]}"


def decide(rss_bytes: int | None, now_epoch: float, state: dict, threshold: int, cooldown_s: int = COOLDOWN_S) -> str:
    if rss_bytes is None:
        return "unknown"
    if rss_bytes < threshold:
        return "below"
    last = state.get("last_attempt_epoch")
    if isinstance(last, (int, float)) and not isinstance(last, bool) and now_epoch - float(last) < cooldown_s:
        return "cooldown"
    return "arm"


def _load_state(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.new")
    temporary.write_text(json.dumps(payload))
    os.replace(temporary, path)


def _log(path: Path, message: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            handle.write(datetime.now(timezone.utc).isoformat() + " " + message + "\n")
    except OSError:
        return


def _http_json(url: str, payload: dict | None = None, timeout: int = 15) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.load(response)
    if not isinstance(body, dict):
        raise RuntimeError("MoP response was not an object")
    return body


def live_status() -> dict:
    return _http_json(STATUS_URL)


def live_arm(payload: dict) -> dict:
    body = _http_json(EXIT_URL, payload)
    status = body.get("status") or {}
    if body.get("ok") is not True or status.get("pm_only") is not True or status.get("with_devs") is True:
        raise RuntimeError("MoP refused PM-only exit-pending")
    return body


def live_snapshot(pane: str = PM_PANE) -> dict:
    try:
        pane_pid = subprocess.run(
            ["tmux", "display-message", "-t", pane, "-p", "#{pane_pid}"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        if not pane_pid.isdigit():
            return {"pm": None, "px": []}
        listing = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,rss=,command="],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        procs = parse_ps_table(listing)
        return {"pm": nearest_claude(int(pane_pid), procs), "px": px_over(procs)}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"pm": None, "px": []}


def arm_if_needed(
    *,
    rss_bytes: int,
    now_epoch: float,
    state_path: Path,
    threshold: int,
    cooldown_s: int = COOLDOWN_S,
    status_get=live_status,
    opener=live_arm,
    log_path: Path | None = None,
) -> str:
    """Arm at most once per cooldown. Intent is persisted before the POST."""
    state = _load_state(state_path)
    decision = decide(rss_bytes, now_epoch, state, threshold, cooldown_s)
    if decision != "arm":
        return decision
    log_path = log_path or state_path.with_name(LOG_NAME)
    try:
        status = status_get()
    except (OSError, urllib.error.URLError, TimeoutError, ValueError, RuntimeError, json.JSONDecodeError):
        _log(log_path, f"unreachable rss={rss_bytes}")
        return "unreachable"
    if isinstance(status, dict) and status.get("pending") is True:
        _log(log_path, f"already_pending rss={rss_bytes}")
        return "already_pending"
    attempted = {"last_attempt_epoch": now_epoch, "last_rss_bytes": rss_bytes, "status": "attempted"}
    try:
        _atomic(state_path, attempted)
    except OSError:
        _log(log_path, f"state_unwritable rss={rss_bytes}")
        return "uncertain"
    payload = {
        "enabled": True,
        "pm_only": True,
        "with_devs": False,
        "continuation_prompt": CONTINUATION,
        "reason": "pm claude rss threshold",
    }
    try:
        opener(payload)
    except (OSError, urllib.error.URLError, TimeoutError, ValueError, RuntimeError, json.JSONDecodeError):
        attempted["status"] = "uncertain"
        try:
            _atomic(state_path, attempted)
        except OSError:
            pass
        _log(log_path, f"uncertain rss={rss_bytes}")
        return "uncertain"
    attempted["status"] = "armed"
    try:
        _atomic(state_path, attempted)
    except OSError:
        pass
    _log(log_path, f"armed rss={rss_bytes}")
    return "armed"


def hourly_line(
    *,
    arm: bool,
    now: datetime | None = None,
    collect=None,
    state_path: Path | None = None,
    status_get=None,
    opener=None,
    env: dict | None = None,
    log_path: Path | None = None,
    cooldown_s: int = COOLDOWN_S,
) -> str:
    """One hourly line. arm=False never calls MoP (dry-run / unclaimed compose)."""
    now = now or datetime.now(timezone.utc)
    snap = (collect or live_snapshot)()
    pm = snap.get("pm") if isinstance(snap, dict) else None
    px = snap.get("px") if isinstance(snap, dict) and isinstance(snap.get("px"), list) else []
    if pm is not None and (not isinstance(pm, tuple) or len(pm) != 2):
        pm = None
    rss = pm[1] if pm else None
    threshold = threshold_bytes(env)
    state_path = state_path or Path.home() / ".claude/state" / STATE_NAME
    if not arm:
        decision = decide(rss, now.timestamp(), _load_state(state_path), threshold, cooldown_s)
        note = "would_arm" if decision == "arm" else decision
        return format_line(pm, px, note)
    if rss is None:
        return format_line(None, px, "unknown")
    note = arm_if_needed(
        rss_bytes=rss, now_epoch=now.timestamp(), state_path=state_path, threshold=threshold,
        cooldown_s=cooldown_s, status_get=status_get or live_status, opener=opener or live_arm,
        log_path=log_path,
    )
    return format_line(pm, px, note)
