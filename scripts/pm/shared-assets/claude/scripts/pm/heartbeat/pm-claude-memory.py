#!/usr/bin/env python3
"""PM Claude RSS line for the hourly, and a once-per-6h PM-only exit-pending arm.

Rajiv DM D0AMF0XE6TS thread 1794921293.133799, ts 1794921896.970909:
"track the claude process memory usage in hourlies and set exit pending automatically".
Scope revision, same thread, ts 1794923900.734289: the easiest trigger is an alerts
message to dev immediately; the 04:00 restart is retired. At PM RSS >= 2GB this
posts ONE HeyDonna Alerts message in #heydonna-dev asking PM to do restart
housekeeping (handoff, resume prompt, continuation note) and then restart.

The arm is the existing POST /exit-pending path (pm_only, continuation prompt,
Stop-hook pm_save_handoff, /exit at the next Stop). with_devs is false: a memory
restart must not cycle dev slots (2026-11-09 global exit-pending restarted a slot
mid-task). The arm is CONDITIONAL at the MoP server boundary (condition=pm_rss):
the server checks for existing owner state and arms in one synchronous step, and
answers 409 owner_state_preserved with zero mutation when any owner exists. The
old GET-then-POST here was a race. The message follows the threshold crossing, not
the arm outcome: armed, uncertain, already pending or preserved all post the same
single message (its text says which), under the unchanged 6h throttle.

Unavailable is never reported as none: when the process snapshot cannot be read
the px part says unavailable.

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
ARM_CONDITION = "pm_rss"
ALERT_CHANNEL = "C0ALZJHGE49"  # #heydonna-dev
SLACK_SEND = Path.home() / ".claude/scripts/slack-send.sh"
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
    "armed_alerted": "; exit-pending armed; alert posted",
    "armed_unalerted": "; exit-pending armed; alert unsent",
    "cooldown": "; exit-pending cooldown",
    "already_pending": "; exit-pending already pending",
    "already_pending_alerted": "; exit-pending already pending; alert posted",
    "already_pending_unalerted": "; exit-pending already pending; alert unsent",
    "preserved": "; exit-pending not armed (existing owner state preserved)",
    "preserved_alerted": "; exit-pending not armed (existing owner state preserved); alert posted",
    "preserved_unalerted": "; exit-pending not armed (existing owner state preserved); alert unsent",
    "unreachable": "; exit-pending not armed (MoP unreachable)",
    "uncertain": "; exit-pending uncertain (fail open)",
    "uncertain_alerted": "; exit-pending uncertain (fail open); alert posted",
    "uncertain_unalerted": "; exit-pending uncertain (fail open); alert unsent",
    "refused": "; exit-pending refused (fail open)",
}


class OwnerPreserved(RuntimeError):
    """The MoP server refused the conditional arm because an owner already exists (409, zero effect)."""


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


def format_line(pm: tuple[int, int] | None, px: list[tuple[str, int, int]] | None, note: str) -> str:
    """px is None when the process snapshot was unavailable: that is never reported as none."""
    if pm is None:
        pm_text = "PM unknown"
    else:
        pid, rss = pm
        pm_text = f"PM {format_gib(rss)} pid {pid}"
    if px is None:
        px_text = "unavailable"
    elif not px:
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
    try:
        body = _http_json(EXIT_URL, payload)
    except urllib.error.HTTPError as error:
        # 409 owner_state_preserved is the server's zero-effect answer: an owner already exists.
        if error.code == 409:
            try:
                reason = json.load(error).get("reason")
            except (OSError, ValueError, AttributeError):
                reason = None
            if reason == "owner_state_preserved":
                raise OwnerPreserved(reason) from error
        raise
    status = body.get("status") or {}
    if body.get("ok") is not True or status.get("pm_only") is not True or status.get("with_devs") is True:
        raise RuntimeError("MoP refused PM-only exit-pending")
    return body


def live_snapshot(pane: str = PM_PANE) -> dict:
    """pm and px are None when that half could not be read; unavailable is never reported as none."""
    try:
        listing = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,rss=,command="],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        procs = parse_ps_table(listing)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"pm": None, "px": None}
    if not procs:
        return {"pm": None, "px": None}
    px = px_over(procs)
    try:
        pane_pid = subprocess.run(
            ["tmux", "display-message", "-t", pane, "-p", "#{pane_pid}"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        pm = nearest_claude(int(pane_pid), procs) if pane_pid.isdigit() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        pm = None
    return {"pm": pm, "px": px}


def alert_text(outcome: str, rss_bytes: int, threshold: int) -> str:
    """The one HeyDonna Alerts message. No backticks (slack-send rule), no tracker numbers."""
    state = {
        "armed": (
            "MoP armed a PM-only exit-pending (dev slots are not cycled): it sends /exit at your next Stop "
            "after the save, relaunches PM fresh and injects the continuation."
        ),
        "uncertain": (
            "The PM-only exit-pending arm outcome is uncertain; confirm with GET /exit-status. When armed, "
            "MoP sends /exit at your next Stop after the save."
        ),
        "preserved": (
            "An existing exit or clear is already in progress and MoP preserved it (nothing new was armed); "
            "complete that restart."
        ),
        "already_pending": "An exit-pending episode is already pending (nothing new was armed); complete that restart.",
    }[outcome]
    return (
        f"*PM Claude RSS {format_gib(rss_bytes)} reached the {format_gib(threshold)} limit.* "
        "PM: do restart housekeeping now, then restart. "
        "1) Save the handoff (memory/MEMORY-session-handoff-current.md), the resume prompt and the "
        "continuation note (data/pm-continuation-note.md). 2) End the turn. "
        f"{state} Nothing is blocked meanwhile."
    )


def post_alert(text: str, channel: str = ALERT_CHANNEL, sender: str | None = None) -> bool:
    """Post as the HeyDonna Alerts bot (SLACK_SEND_AS=alerts), body on stdin. True only on an OK receipt."""
    sender = sender or os.environ.get("PM_ALERT_SLACK_SEND") or str(SLACK_SEND)
    try:
        done = subprocess.run(
            [sender, "-c", channel, "-f"], input=text, capture_output=True, text=True,
            timeout=20, check=False, env={**os.environ, "SLACK_SEND_AS": "alerts"},
        )
    except (OSError, subprocess.SubprocessError):
        return False
    receipt = next((line for line in done.stdout.splitlines() if line.startswith("OK ts=")), "")
    fields = dict(part.split("=", 1) for part in receipt.split()[1:] if "=" in part)
    return (
        done.returncode == 0
        and re.fullmatch(r"[0-9]+\.[0-9]+", fields.get("ts", "")) is not None
        and fields.get("channel") == channel
    )


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
    notifier=None,
) -> str:
    """Decide once per cooldown window: attempt the arm AND post the one Alerts message.

    The arm is conditional at the server (pm_rss): a 409 owner_state_preserved means an owner
    already exists and nothing was changed. Whatever the arm's outcome (armed, uncertain, an
    already-pending episode, or a preserved owner), the PM is asked once to do restart
    housekeeping, and the 6h throttle starts, so there is never more than one message per window.
    MoP unreachable posts nothing and starts nothing (retried at the next hourly). Intent is
    persisted before the POST. notifier posts the message; None (the default) posts nothing.
    """
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
    attempted = {"last_attempt_epoch": now_epoch, "last_rss_bytes": rss_bytes, "status": "attempted"}

    def settle(outcome: str) -> str:
        attempted["status"] = outcome
        alerted = outcome
        if notifier is not None:
            try:
                sent = bool(notifier(alert_text(outcome, rss_bytes, threshold)))
            except Exception:  # fail open: a sender fault never gates PM or the hourly
                sent = False
            attempted["alert"] = "posted" if sent else "unsent"
            alerted = f"{outcome}_{'alerted' if sent else 'unalerted'}"
            _log(log_path, f"alert_{'posted' if sent else 'unsent'} rss={rss_bytes}")
        try:
            _atomic(state_path, attempted)
        except OSError:
            pass
        _log(log_path, f"{outcome} rss={rss_bytes}")
        return alerted

    if isinstance(status, dict) and status.get("pending") is True:
        return settle("already_pending")
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
        "condition": ARM_CONDITION,
        "rss_bytes": rss_bytes,
        "threshold_bytes": threshold,
    }
    try:
        opener(payload)
    except OwnerPreserved:
        return settle("preserved")  # zero effect at the server: the existing owner is untouched
    except (OSError, urllib.error.URLError, TimeoutError, ValueError, RuntimeError, json.JSONDecodeError):
        return settle("uncertain")
    return settle("armed")


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
    notifier=None,
) -> str:
    """One hourly line. arm=False never calls MoP (dry-run / unclaimed compose).

    The Alerts message follows the LIVE arm only: with the real opener (no injected opener) it
    defaults to post_alert; any injected opener (a test or dry harness) posts nothing unless a
    notifier is also injected, so nothing here can reach Slack by accident.
    """
    now = now or datetime.now(timezone.utc)
    snap = (collect or live_snapshot)()
    pm = snap.get("pm") if isinstance(snap, dict) else None
    px = snap.get("px") if isinstance(snap, dict) else None
    if not isinstance(px, list):
        px = None  # an unreadable snapshot is unavailable, never an empty list
    if pm is not None and (not isinstance(pm, tuple) or len(pm) != 2):
        pm = None
    notify = notifier if notifier is not None else (post_alert if opener is None else None)
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
        log_path=log_path, notifier=notify,
    )
    return format_line(pm, px, note)
