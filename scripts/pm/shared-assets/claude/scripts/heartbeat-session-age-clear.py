#!/usr/bin/env python3
"""Seven-pane session-age materializer and PM obligation producer.

The numbered-slot path remains materialize-only. The PM path uses the existing
`pm-ops.py obligation-upsert` writer so the heartbeat and Stop hook share one
durable obligation instead of delegating to the retired PM Operator binary.

The script never clears a session, and no caller should clear a numbered pane on
a cadence. Every age-due numbered pane is emitted as `status: pending,
can_clear_now: false, materialized_only: true` as an observation only; dev-slot
clearing belongs to the PM assignment boundary (`mop-assign-slot --class
new_issue`) or a distinct explicit operator clear. The PM pane path below is
unchanged.
For PM, a session age strictly greater than six hours posts ONE HeyDonna Alerts
message in #heydonna-dev asking PM to do clear housekeeping (handoff, resume
prompt, continuation note) and then self-clear (Rajiv D0AMF0XE6TS thread
1794921293.133799, ts 1794923900.734289: "easiest trigger alerts message to dev
immediately, PM self-clear same way"). This replaces the earlier automatic
POST /slots/0/clear (source=session_age_policy): the script no longer asks MoP
for the clear itself. PM's own clear request still goes through MoP's existing
pm_save_handoff flow, which injects /clear at the next PM Stop after the save
(Rajiv D0AMF0XE6TS ts 1793732417.459339). The message is sent once per PM
session and re-sent no sooner than six hours later while that session lives.
Send custody (CTO REVISE of da450c5, C0ALZJHGE49 thread 1794923203.112819, ts
1794925523.128429): the guarded send intent is persisted in the existing
session/window state BEFORE every dispatch. An issued send that is not confirmed
by an OK receipt (timeout, lost response, state-write failure afterwards) stays
consumed/UNKNOWN under that session/window key and is never resent
automatically; only a later policy-due reminder (the next six-hour window) can
send again. If the intent cannot be persisted the alert is skipped and the run
fails open. Nothing here gates PM input or tools.

Session age is measured from the effective session start = the first timestamped
event in the pane's newest non-sidechain session JSONL. A `/clear` starts a
fresh JSONL (so its first ts is the clear); `SessionStart:compact` continues the
same JSONL, so age correctly counts from before the compact. Nested
reviewer/subagent transcripts (`isSidechain: true`) are excluded. Numbered-slot
clear-worthy threshold remains 3h; PM threshold is strictly greater than 6h.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

MOP_BASE = f"http://127.0.0.1:{os.environ.get('MOP_PORT', '3100')}"
PROJECTS = Path.home() / ".claude" / "projects"
PM_DIR = "-Users-rajiv-Downloads-projects-heydonna-app"
AGE_DUE_SECONDS = 3 * 60 * 60
PM_AGE_DUE_SECONDS = 6 * 60 * 60
LATEST = Path(os.environ.get("SESSION_AGE_CLEAR_LATEST", "/tmp/session-age-clear-latest.json"))
ALERT_CHANNEL = "C0ALZJHGE49"  # #heydonna-dev
ALERT_RENOTIFY_SECONDS = 6 * 60 * 60
ALERT_STATE = Path(os.environ.get(
    "PM_SESSION_AGE_ALERT_STATE", str(Path.home() / ".claude" / "state" / "pm-session-age-alert.json")))
PM_CLEAR_COMMAND = "bash /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/mop-clear-slot.sh pm"


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _mcp_children_running() -> int | None:
    """Count live MoP MCP child processes (any master-of-panes dist/mcp.js).

    Rajiv directive 2026-09-23: the MCP is retired in favor of REST; the
    directive is complete when this reaches 0. Fail-open: None on any
    collection error, never blocks the session-age materialization.
    """
    try:
        proc = subprocess.run(
            ["ps", "-ax", "-o", "command="],
            check=False, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    count = 0
    for line in proc.stdout.splitlines():
        if "master-of-panes" in line and "dist/mcp.js" in line:
            count += 1
    return count


def _mop_slots() -> list | None:
    """GET /slots — live MoP slot inventory. None on any failure (fail-soft)."""
    try:
        with urllib.request.urlopen(f"{MOP_BASE}/slots", timeout=4) as resp:
            return json.loads(resp.read().decode()).get("slots", [])
    except Exception:
        return None


def _pane_dir(pane: str) -> Path:
    if pane == "pm":
        return PROJECTS / PM_DIR
    return PROJECTS / f"{PM_DIR}-300{pane}"


def _alert_text(age_seconds: float) -> str:
    """The one HeyDonna Alerts message for a stale PM session. No backticks (slack-send rule)."""
    hours = age_seconds / 3600
    return (
        f"*PM session is {hours:.1f}h old (limit {PM_AGE_DUE_SECONDS // 3600}h).* "
        "PM: do clear housekeeping now, then self-clear. "
        f"Run the MoP-logged PM clear: {PM_CLEAR_COMMAND}. That opens MoP's save-handoff obligation: "
        "update the handoff (memory/MEMORY-session-handoff-current.md), the resume prompt and the "
        "continuation note (data/pm-continuation-note.md), then end the turn. MoP injects /clear at "
        "your next Stop after the save. If a clear is already pending or uncertain, do not request "
        "another. Nothing is blocked meanwhile."
    )


def _post_alert(text: str, channel: str = ALERT_CHANNEL) -> bool:
    """Post as the HeyDonna Alerts bot (SLACK_SEND_AS=alerts), body on stdin. True only on an OK receipt."""
    sender = os.environ.get("PM_ALERT_SLACK_SEND") or str(Path.home() / ".claude" / "scripts" / "slack-send.sh")
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


def _read_alert_state(state_path: Path) -> dict | None:
    """The saved session/window record. {} when absent or unparsable; None when the file exists but cannot be read."""
    try:
        saved = json.loads(state_path.read_text())
    except FileNotFoundError:
        return {}
    except ValueError:
        return {}
    except OSError:
        return None  # unknown is not absence: the window cannot be proven, so nothing is dispatched
    return saved if isinstance(saved, dict) else {}


def _write_alert_state(state_path: Path, record: dict) -> None:
    """Durable atomic write of the session/window record. Raises OSError."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_name(state_path.name + f".{os.getpid()}.new")
    try:
        with temporary.open("w") as handle:
            handle.write(json.dumps(record, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, state_path)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _alert_window_send(saved: dict, session_id: str, now: datetime) -> str | None:
    """The send state of the live window for this session: "posted", "issued", or None when no window holds.

    "issued" means a send was dispatched (or was about to be) and no OK receipt confirmed it: consumed/UNKNOWN.
    A record written before the intent field existed (da450c5) was written only after an OK receipt.
    """
    sent = saved.get("sent_epoch")
    if (
        saved.get("session_id") == session_id
        and isinstance(sent, (int, float)) and not isinstance(sent, bool)
        and now.timestamp() - float(sent) < ALERT_RENOTIFY_SECONDS
    ):
        return "issued" if saved.get("send") == "issued" else "posted"
    return None


def _alert_pm_self_clear(
    session_id: str, age_seconds: float, now: datetime, post=None, state_path: Path | None = None,
) -> tuple[str | None, str | None]:
    """Ask PM, through one Alerts message, to do housekeeping and self-clear.

    Once per session; a policy-due reminder is sent no sooner than ALERT_RENOTIFY_SECONDS later.
    The guarded send intent (session_id, window start, send=issued) is persisted BEFORE the dispatch,
    under a non-blocking lock so two runs cannot both claim the window. An issued send that is not
    confirmed by an OK receipt (post False or raising, or the write of the confirmation failing) stays
    consumed/UNKNOWN under this session/window key and is never resent automatically. If the intent
    cannot be persisted (state unreadable or unwritable, lock contended) nothing is dispatched and the
    caller reports it. Fails open: nothing here gates PM.
    Returns (status, error): status is alert_posted, alert_already_sent or alert_send_unconfirmed.
    """
    post = post or _post_alert
    state_path = state_path or ALERT_STATE
    intent = {
        "attempt": uuid.uuid4().hex,
        "send": "issued",
        "sent_epoch": now.timestamp(),
        "session_id": session_id,
    }
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with state_path.with_name(state_path.name + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            saved = _read_alert_state(state_path)
            if saved is None:
                return None, "alert_intent_not_persisted"
            held = _alert_window_send(saved, session_id, now)
            if held == "posted":
                return "alert_already_sent", None
            if held == "issued":
                return "alert_send_unconfirmed", None
            _write_alert_state(state_path, intent)
    except OSError:  # includes a contended lock: nothing was dispatched, the next run retries
        return None, "alert_intent_not_persisted"
    try:
        delivered = bool(post(_alert_text(age_seconds)))
    except Exception:  # a sender fault never gates PM (rule 50); the issued send stays consumed/UNKNOWN
        return "alert_send_unconfirmed", None
    if not delivered:
        return "alert_send_unconfirmed", None
    try:
        _write_alert_state(state_path, {**intent, "send": "posted"})
    except OSError:
        pass  # the persisted intent already consumes this window; the message is out and is not resent
    return "alert_posted", None


def _newest_main_session(dir_path: Path) -> tuple[str | None, str | None]:
    """Return (session_id, effective_start_iso) for the pane's newest
    non-sidechain session JSONL, or (None, None) if none is readable."""
    try:
        files = sorted(
            dir_path.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None, None
    for f in files:
        try:
            with f.open() as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if rec.get("isSidechain") is True:
                        continue
                    ts = rec.get("timestamp")
                    if ts:
                        return f.stem, ts  # first non-sidechain ts wins
        except OSError:
            continue
    return None, None


def _obligation_id(pane: str, session_id: str) -> str:
    digest = hashlib.sha256(
        f"session-age-clear:{pane}:{session_id}".encode()
    ).hexdigest()
    return f"session-age-clear:{pane}:{digest}"


def main() -> int:
    now = datetime.now(timezone.utc)
    slots = _mop_slots()
    read_errors: dict[str, str] = {}

    if slots is None:
        numbered = [str(n) for n in range(1, 9)]
        read_errors["_mop"] = "mop_slots_unreachable; using default 1..8 inventory"
    else:
        numbered = sorted({str(s["slot"]) for s in slots if "slot" in s}, key=int)
        if not numbered:
            numbered = [str(n) for n in range(1, 9)]
    inventory = ["pm"] + numbered

    results: dict[str, dict] = {}
    obligation_ids: dict[str, str] = {}
    pending: list[str] = []
    blocked = False

    for pane in inventory:
        pane_dir = _pane_dir(pane)
        session_id, start_iso = _newest_main_session(pane_dir)
        start = _parse_ts(start_iso) if start_iso else None
        if session_id is None or start is None:
            read_errors[pane] = (
                f"no_readable_session_jsonl in {pane_dir.name}"
                if session_id is None
                else f"bad_timestamp {start_iso}"
            )
            # Unknown age -> safe default not_due (never triggers a clear).
            results[pane] = {
                "age_due": False,
                "session_id": session_id,
                "status": "not_due",
            }
            continue

        age = (now - start).total_seconds()
        threshold = PM_AGE_DUE_SECONDS if pane == "pm" else AGE_DUE_SECONDS
        due = age > threshold if pane == "pm" else age >= threshold
        if due:
            if pane == "pm":
                alert_status, error = _alert_pm_self_clear(session_id, age, now)
                if error:
                    blocked = True
                    read_errors[pane] = error
                    results[pane] = {
                        "age_due": True,
                        "kind": "pm_self_clear_alert",
                        "session_id": session_id,
                        "status": "blocked",
                    }
                    continue
                pending.append(pane)
                results[pane] = {
                    "age_due": True,
                    "kind": "pm_self_clear_alert",
                    "materialized_only": True,
                    "alert_status": alert_status,
                    "pane": pane,
                    "session_id": session_id,
                    "status": "pending_self_clear_alert",
                }
                continue
            oid = _obligation_id(pane, session_id)
            obligation_ids[pane] = oid
            pending.append(pane)
            results[pane] = {
                "age_due": True,
                "can_clear_now": False,
                "kind": "session_age_clear",
                "materialized_only": True,
                "obligation_id": oid,
                "pane": pane,
                "session_id": session_id,
                "status": "pending",
            }
        else:
            results[pane] = {
                "age_due": False,
                "session_id": session_id,
                "status": "not_due",
            }

    out = {
        "due_before": list(pending),
        "execution": "materialize_numbered_and_alert_pm_self_clear",
        "mcp_children_running": _mcp_children_running(),
        "generated_at": _iso_now(),
        "inventory": inventory,
        "kind": "session_age_clear",
        "obligation_ids": obligation_ids,
        "pending": list(pending),
        "read_errors": read_errors,
        "results": results,
        "source": "mop_rest+runtime_jsonl+heydonna_alerts_pm_self_clear",
    }
    payload = json.dumps(out, sort_keys=True, indent=2)
    try:
        LATEST.write_text(payload)
    except OSError as exc:
        print(
            json.dumps(
                {
                    "kind": "session_age_clear",
                    "status": "blocked",
                    "reason": f"latest_write_failed:{exc}",
                },
                sort_keys=True,
            )
        )
        return 20
    print(payload)
    return 20 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
