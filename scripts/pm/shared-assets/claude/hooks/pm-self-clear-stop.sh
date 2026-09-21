#!/usr/bin/env python3
"""Read-only Stop consumer for the canonical PM self-clear obligation."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


DB = Path(
    os.environ.get(
        "PM_OPS_DB",
        "/Users/rajiv/.claude/projects/"
        "-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db",
    )
)
PM_AGE_DUE_SECONDS = 6 * 60 * 60


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _current_session(hook_input: dict) -> tuple[str | None, float | None, str | None]:
    session_id = hook_input.get("session_id")
    transcript_path = hook_input.get("transcript_path")
    if not isinstance(session_id, str) or not session_id.strip():
        return None, None, "current_session_identity_unavailable"
    if not isinstance(transcript_path, str) or not transcript_path:
        return session_id, None, "current_session_age_unavailable"
    started_at = None
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as transcript:
            for raw in transcript:
                try:
                    entry = json.loads(raw)
                except Exception:
                    continue
                if entry.get("isSidechain") is True:
                    continue
                started_at = _parse_ts(entry.get("timestamp"))
                if started_at is not None:
                    break
    except OSError:
        return session_id, None, "current_session_age_unavailable"
    if started_at is None:
        return session_id, None, "current_session_age_unavailable"
    age = (datetime.now(timezone.utc) - started_at).total_seconds()
    if age < 0:
        return session_id, None, "current_session_age_unavailable"
    return session_id, age, None


def _decision_for_row(row: sqlite3.Row, hook_input: dict) -> tuple[str, str]:
    try:
        evidence = json.loads(row["evidence_json"] or "{}")
    except (TypeError, ValueError):
        return "deferred", "pm_self_clear_evidence_unavailable"
    if not isinstance(evidence, dict):
        return "deferred", "pm_self_clear_evidence_unavailable"

    current_session_id, age_seconds, age_reason = _current_session(hook_input)
    recorded_session_id = evidence.get("session_id")
    if not isinstance(recorded_session_id, str) or not recorded_session_id:
        return "deferred", "pm_self_clear_evidence_unavailable"
    if _parse_ts(evidence.get("session_started_at")) is None:
        return "deferred", "pm_self_clear_evidence_unavailable"
    if not current_session_id:
        return "deferred", "current_session_identity_unavailable"
    if recorded_session_id != current_session_id:
        return "deferred", "current_session_mismatch"
    if age_reason or age_seconds is None:
        return "deferred", age_reason or "current_session_age_unavailable"
    if hook_input.get("stop_hook_active") is True:
        return "deferred", "stop_hook_active"
    if age_seconds <= PM_AGE_DUE_SECONDS:
        return "not_due", "current_session_not_due"
    return "block", "pm_self_clear_pending"


def main() -> int:
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
    except (TypeError, ValueError):
        hook_input = {}
    if not isinstance(hook_input, dict):
        hook_input = {}
    if not DB.is_file():
        print(json.dumps({"kind": "pm-self-clear", "status": "deferred", "reason": "pm_ops_unavailable"}))
        return 0
    connection = None
    try:
        connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, title, required_action, evidence_json
            FROM obligations
            WHERE status='open' AND kind='pm-self-clear' AND horizon='heartbeat'
            ORDER BY id ASC
            """
        ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        print(json.dumps({
            "kind": "pm-self-clear",
            "status": "deferred",
            "reason": f"pm_ops_unavailable:{type(exc).__name__}",
        }, sort_keys=True))
        return 0
    finally:
        if connection is not None:
            connection.close()

    if len(rows) != 1:
        status = "not_due" if not rows else "deferred"
        reason = "no_open_canonical_obligation" if not rows else "ambiguous_open_canonical_obligations"
        print(json.dumps({"kind": "pm-self-clear", "status": status, "reason": reason}, sort_keys=True))
        return 0

    row = rows[0]
    decision, decision_reason = _decision_for_row(row, hook_input)
    if decision != "block":
        print(json.dumps({
            "kind": "pm-self-clear",
            "status": decision,
            "reason": decision_reason,
            "obligation_id": int(row["id"]),
        }, sort_keys=True))
        return 0
    action = row["required_action"] or (
        "At next safe Stop boundary, self-clear context and prove a fresh boundary before resuming work"
    )
    message = (
        f"[PM_OPS_ACTION_REQUIRED] obligation:{row['id']} "
        f"{row['title'] or 'PM self-clear context'}. {action}. "
        "Do not auto-clear from age alone."
    )
    print(json.dumps({
        "decision": "block",
        "kind": "pm-self-clear",
        "message": message,
        "obligation_id": int(row["id"]),
        "reason": decision_reason,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
