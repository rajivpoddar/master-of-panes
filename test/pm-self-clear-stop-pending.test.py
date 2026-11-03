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
    def run_hook(self, failed=False, idle=False):
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
                db.execute("INSERT INTO config VALUES ('clear_pending_0', ?)", ("false" if failed or idle else "true",))
                db.execute("INSERT INTO config VALUES ('pm_clear_delivery_state', ?)", ("failed" if failed else "failed_open" if idle else "deferred_busy",))
            result = subprocess.run([sys.executable, str(HOOK)], input=json.dumps({
                "session_id": "pm", "transcript_path": str(transcript)}), text=True,
                capture_output=True, env={**os.environ, "PM_OPS_DB": str(ops), "MOP_DB_PATH": str(mop)}, check=True)
            output = json.loads(result.stdout)
            return output

    def test_pending_latch_does_not_require_a_fresh_marker(self):
        output = self.run_hook()
        self.assertNotEqual(output.get("decision"), "block")
        self.assertEqual(output["reason"], "mop_clear_pending_awaiting_stop")

    def test_terminal_failure_reports_rerequest_without_blocking(self):
        output = self.run_hook(failed=True)
        self.assertNotEqual(output.get("decision"), "block")
        self.assertEqual(output["reason"], "mop_clear_delivery_failed")
        self.assertEqual(output["message"], "MoP delivery failed, re-request")

    def test_due_row_with_no_pending_clear_never_blocks(self):
        # Rajiv D0AMF0XE6TS ts 1793732417.459339: MoP owns the clear via
        # pm_save_handoff; the 20-minute pm_self_clear_pending block is gone.
        output = self.run_hook(idle=True)
        self.assertNotEqual(output.get("decision"), "block")
        self.assertEqual(output["reason"], "superseded_by_pm_save_handoff")


if __name__ == "__main__":
    unittest.main()
