"""Pending MoP delivery must never be blocked by the PM obligation consumer."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HOOK = Path(__file__).resolve().parents[1] / "scripts/pm/shared-assets/claude/hooks/pm-self-clear-stop.sh"


class PendingClearStopTest(unittest.TestCase):
    def test_pending_latch_does_not_require_a_fresh_marker(self):
        with tempfile.TemporaryDirectory(prefix="mop-clear-hook-") as directory:
            root = Path(directory)
            ops = root / "pm-ops.db"
            mop = root / "mop.db"
            transcript = root / "transcript.jsonl"
            old = (datetime.now(timezone.utc) - timedelta(hours=8)).isoformat()
            transcript.write_text(json.dumps({"timestamp": old}) + "\n")
            with sqlite3.connect(ops) as db:
                db.execute("CREATE TABLE obligations (id INTEGER, title TEXT, required_action TEXT, evidence_json TEXT, status TEXT, kind TEXT, horizon TEXT)")
                db.execute("INSERT INTO obligations VALUES (1, 'clear', 'request clear', ?, 'open', 'pm-self-clear', 'heartbeat')",
                           (json.dumps({"session_id": "pm", "session_started_at": old}),))
            with sqlite3.connect(mop) as db:
                db.execute("CREATE TABLE config (key TEXT, value TEXT)")
                db.execute("INSERT INTO config VALUES ('clear_pending_0', 'true')")
            result = subprocess.run([sys.executable, str(HOOK)], input=json.dumps({
                "session_id": "pm", "transcript_path": str(transcript)}), text=True,
                capture_output=True, env={**os.environ, "PM_OPS_DB": str(ops), "MOP_DB_PATH": str(mop)}, check=True)
            output = json.loads(result.stdout)
            self.assertNotEqual(output.get("decision"), "block")
            self.assertEqual(output["reason"], "mop_clear_pending_awaiting_stop")


if __name__ == "__main__":
    unittest.main()
