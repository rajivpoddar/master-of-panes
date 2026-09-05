#!/usr/bin/env python3
"""Seven-pane session-age materializer and PM obligation producer.

The numbered-slot path remains materialize-only. The PM path uses the existing
`pm-ops.py obligation-upsert` writer so the heartbeat and Stop hook share one
durable obligation instead of delegating to the retired PM Operator binary.

The script never clears a session. Every age-due numbered pane is emitted as
`status: pending, can_clear_now: false, materialized_only: true`; the heartbeat
background agent owns the actual clear via `mop-clear-slot.sh --require-terminal`.
For PM, a session age strictly greater than six hours creates or refreshes one
`pm-self-clear` obligation and leaves it open for the next safe Stop.

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
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MOP_BASE = f"http://127.0.0.1:{os.environ.get('MOP_PORT', '3100')}"
PROJECTS = Path.home() / ".claude" / "projects"
PM_DIR = "-Users-rajiv-Downloads-projects-heydonna-app"
AGE_DUE_SECONDS = 3 * 60 * 60
PM_AGE_DUE_SECONDS = 6 * 60 * 60
PM_TARGET_ID = "pm-dhruva"
PM_OPS = os.environ.get("PM_OPS", "/Users/rajiv/.claude/scripts/pm-ops.py")
LATEST = Path(os.environ.get("SESSION_AGE_CLEAR_LATEST", "/tmp/session-age-clear-latest.json"))


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


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


def _upsert_pm_obligation(session_id: str, start_iso: str, age_seconds: float) -> tuple[str | None, str | None]:
    """Persist one canonical PM obligation, preserving the existing row key."""
    if not Path(PM_OPS).is_file():
        return None, f"pm_ops_unavailable:{PM_OPS}"
    evidence = json.dumps(
        {
            "producer": "heartbeat-session-age-clear",
            "pane": "pm",
            "session_id": session_id,
            "session_started_at": start_iso,
            "age_seconds": round(age_seconds, 3),
            "threshold_seconds": PM_AGE_DUE_SECONDS,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    command = [
        PM_OPS,
        "obligation-upsert",
        "--kind", "pm-self-clear",
        "--severity", "high",
        "--target-type", "session",
        "--target-id", PM_TARGET_ID,
        "--owner", "pm",
        "--title", "PM self-clear context at next safe Stop boundary when session >6h",
        "--action", "At next safe Stop boundary, if PM session age >6h, self-clear context and prove a fresh boundary before resuming work",
        "--horizon", "heartbeat",
        "--dedupe-group", "pm-self-clear:heartbeat:pm",
        "--evidence-json", evidence,
        "--print-id",
    ]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"pm_ops_failed:{type(exc).__name__}"
    if result.returncode != 0:
        return None, f"pm_ops_failed:exit={result.returncode}"
    obligation_id = result.stdout.strip().splitlines()[-1:] or []
    if not obligation_id or not obligation_id[0].isdigit():
        return None, "pm_ops_failed:missing_obligation_id"
    return obligation_id[0], None


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
        numbered = [str(n) for n in range(1, 7)]
        read_errors["_mop"] = "mop_slots_unreachable; using default 1..6 inventory"
    else:
        numbered = sorted({str(s["slot"]) for s in slots if "slot" in s}, key=int)
        if not numbered:
            numbered = [str(n) for n in range(1, 7)]
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
                oid, error = _upsert_pm_obligation(session_id, start_iso, age)
                if error:
                    blocked = True
                    read_errors[pane] = error
                    results[pane] = {
                        "age_due": True,
                        "canonical_obligation": False,
                        "kind": "pm-self-clear",
                        "session_id": session_id,
                        "status": "blocked",
                    }
                    continue
                obligation_ids[pane] = oid
                pending.append(pane)
                results[pane] = {
                    "age_due": True,
                    "canonical_obligation": True,
                    "kind": "pm-self-clear",
                    "materialized_only": True,
                    "obligation_id": int(oid),
                    "pane": pane,
                    "session_id": session_id,
                    "status": "pending_safe_stop",
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
        "execution": "materialize_numbered_and_upsert_pm",
        "generated_at": _iso_now(),
        "inventory": inventory,
        "kind": "session_age_clear",
        "obligation_ids": obligation_ids,
        "pending": list(pending),
        "read_errors": read_errors,
        "results": results,
        "source": "mop_rest+runtime_jsonl+pm-ops.obligation-upsert",
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
