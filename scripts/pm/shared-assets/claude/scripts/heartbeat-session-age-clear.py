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
Nothing here gates PM input or tools; a Slack failure is reported and retried
at the next run.

Session age is measured from the effective session start = the first timestamped
event in the pane's newest non-sidechain session JSONL. A `/clear` starts a
fresh JSONL (so its first ts is the clear); `SessionStart:compact` continues the
same JSONL, so age correctly counts from before the compact. Nested
reviewer/subagent transcripts (`isSidechain: true`) are excluded. Numbered-slot
clear-worthy threshold remains 3h; PM threshold is strictly greater than 6h.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import urllib.request
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


def _alert_pm_self_clear(
    session_id: str, age_seconds: float, now: datetime, post=None, state_path: Path | None = None,
) -> tuple[str | None, str | None]:
    """Ask PM, through one Alerts message, to do housekeeping and self-clear.

    Once per session; re-sent no sooner than ALERT_RENOTIFY_SECONDS later. The state is written
    only after an OK receipt, so a failed post is retried at the next run. Fails open.
    """
    post = post or _post_alert
    state_path = state_path or ALERT_STATE
    try:
        saved = json.loads(state_path.read_text())
    except (OSError, ValueError):
        saved = {}
    sent = saved.get("sent_epoch") if isinstance(saved, dict) else None
    if (
        isinstance(saved, dict) and saved.get("session_id") == session_id
        and isinstance(sent, (int, float)) and not isinstance(sent, bool)
        and now.timestamp() - float(sent) < ALERT_RENOTIFY_SECONDS
    ):
        return "alert_already_sent", None
    try:
        delivered = bool(post(_alert_text(age_seconds)))
    except Exception as exc:  # a sender fault never gates PM (rule 50)
        return None, f"alert_post_failed:{type(exc).__name__}"
    if not delivered:
        return None, "alert_post_failed"
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = state_path.with_name(state_path.name + f".{os.getpid()}.new")
        temporary.write_text(json.dumps({"session_id": session_id, "sent_epoch": now.timestamp()}))
        os.replace(temporary, state_path)
    except OSError:
        pass  # message is out; worst case one more message at the next run
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
