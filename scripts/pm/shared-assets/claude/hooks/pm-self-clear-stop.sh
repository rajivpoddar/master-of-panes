#!/usr/bin/env python3
"""Read-only Stop consumer for the canonical PM self-clear obligation."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path


DB = Path(
    os.environ.get(
        "PM_OPS_DB",
        "/Users/rajiv/.claude/projects/"
        "-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db",
    )
)


def main() -> int:
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
        "reason": "pm_self_clear_pending",
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
