from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import tempfile
import textwrap
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).parents[3] / "scripts" / "pm"
HEARTBEAT = ROOT / "shared-assets" / "claude" / "scripts" / "heartbeat-session-age-clear.py"
STOP_HOOK = ROOT / "shared-assets" / "claude" / "hooks" / "pm-self-clear-stop.sh"
VALIDATOR = ROOT / "shared-assets" / "claude" / "hooks" / "pm-ops-sync-stop-validator.sh"


def load_heartbeat():
    spec = importlib.util.spec_from_file_location("heartbeat_session_age_clear", HEARTBEAT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCHEMA = """
CREATE TABLE obligations (
 id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 status TEXT NOT NULL, kind TEXT NOT NULL, severity TEXT NOT NULL, target_type TEXT,
 target_id TEXT, pr INTEGER, issue INTEGER, slot INTEGER, owner TEXT, title TEXT,
 required_action TEXT, blocker TEXT, evidence_json TEXT NOT NULL DEFAULT '{}',
 resolved_at TEXT, resolution_event_id INTEGER, resolved_reason TEXT, superseded_by INTEGER,
 external_state TEXT, last_verified_at TEXT, horizon TEXT NOT NULL DEFAULT 'hourly',
 next_review_at TEXT, last_surface_at TEXT, suppress_until TEXT, dedupe_group TEXT
)
"""


class PmSelfClearRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.projects = self.root / "projects"
        self.pm_dir = self.projects / "pm"
        self.pm_dir.mkdir(parents=True)
        self.db = self.root / "pm-ops.db"
        with closing(sqlite3.connect(self.db)) as connection:
            connection.executescript(SCHEMA)
            connection.commit()
        self.pm_ops = self.root / "pm-ops.py"
        self.pm_ops.write_text(textwrap.dedent(f"""
            #!/usr/bin/env python3
            import json, os, sqlite3, sys
            db = {str(self.db)!r}
            if sys.argv[1:2] != ["obligation-upsert"]:
                raise SystemExit(2)
            evidence = sys.argv[sys.argv.index("--evidence-json") + 1]
            c = sqlite3.connect(db)
            try:
                row = c.execute("SELECT id FROM obligations WHERE status='open' AND kind='pm-self-clear' AND target_type='session' AND target_id='pm-dhruva'").fetchone()
                if row:
                    oid = row[0]
                    c.execute("UPDATE obligations SET evidence_json=?, updated_at='now' WHERE id=?", (evidence, oid))
                else:
                    c.execute("INSERT INTO obligations(created_at,updated_at,status,kind,severity,target_type,target_id,owner,title,required_action,horizon,evidence_json) VALUES ('now','now','open','pm-self-clear','high','session','pm-dhruva','pm','PM self-clear context','At next safe Stop boundary','heartbeat',?)", (evidence,))
                    oid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
                c.commit()
            finally:
                c.close()
            print(oid)
        """).lstrip())
        self.pm_ops.chmod(0o755)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_session(self, *, age_seconds: float, session_id: str = "pm-session-1") -> None:
        start = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        (self.pm_dir / f"{session_id}.jsonl").write_text(json.dumps({"timestamp": start.isoformat().replace("+00:00", "Z"), "isSidechain": False}) + "\n")

    def run_heartbeat(self, *, age_seconds: float) -> tuple[int, dict]:
        self.write_session(age_seconds=age_seconds)
        module = load_heartbeat()
        module.PROJECTS = self.projects
        module.PM_DIR = "pm"
        module.PM_OPS = str(self.pm_ops)
        module.LATEST = self.root / "latest.json"
        module._mop_slots = lambda: []
        captured: list[str] = []
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = module.main()
        payload = json.loads(output.getvalue())
        return code, payload

    def test_old_disconnected_path_is_replaced_by_one_canonical_upsert(self) -> None:
        code, payload = self.run_heartbeat(age_seconds=6 * 60 * 60 + 1)
        self.assertEqual(code, 0)
        self.assertEqual(payload["results"]["pm"]["status"], "pending_safe_stop")
        self.assertTrue(payload["results"]["pm"]["canonical_obligation"])
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM obligations").fetchone()[0], 1)

    def test_repeated_heartbeat_keeps_one_row_and_preserves_id(self) -> None:
        first_code, first = self.run_heartbeat(age_seconds=6 * 60 * 60 + 30)
        second_code, second = self.run_heartbeat(age_seconds=6 * 60 * 60 + 60)
        self.assertEqual((first_code, second_code), (0, 0))
        self.assertEqual(first["obligation_ids"]["pm"], second["obligation_ids"]["pm"])
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM obligations").fetchone()[0], 1)

    def test_existing_pending_obligation_id_is_reused(self) -> None:
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("INSERT INTO obligations(id,created_at,updated_at,status,kind,severity,target_type,target_id,owner,title,required_action,horizon,evidence_json) VALUES (15606,'old','old','open','pm-self-clear','high','session','pm-dhruva','pm','legacy title','legacy action','heartbeat','{}')")
            connection.commit()
        code, payload = self.run_heartbeat(age_seconds=6 * 60 * 60 + 1)
        self.assertEqual(code, 0)
        self.assertEqual(payload["obligation_ids"]["pm"], "15606")
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM obligations").fetchone()[0], 1)

    def test_exact_threshold_and_younger_session_are_not_due(self) -> None:
        code, payload = self.run_heartbeat(age_seconds=6 * 60 * 60 - 30)
        self.assertEqual(code, 0)
        self.assertEqual(payload["results"]["pm"]["status"], "not_due")
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM obligations").fetchone()[0], 0)

    def test_missing_session_is_not_due_and_is_reported(self) -> None:
        module = load_heartbeat()
        module.PROJECTS = self.projects
        module.PM_DIR = "pm"
        module.PM_OPS = str(self.pm_ops)
        module.LATEST = self.root / "latest.json"
        module._mop_slots = lambda: []
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = module.main()
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["results"]["pm"]["status"], "not_due")
        self.assertIn("no_readable_session_jsonl", payload["read_errors"]["pm"])

    def test_missing_canonical_store_is_truthful(self) -> None:
        self.write_session(age_seconds=6 * 60 * 60 + 1)
        module = load_heartbeat()
        module.PROJECTS = self.projects
        module.PM_DIR = "pm"
        module.PM_OPS = str(self.root / "missing-pm-ops.py")
        module.LATEST = self.root / "latest.json"
        module._mop_slots = lambda: []
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = module.main()
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 20)
        self.assertEqual(payload["results"]["pm"]["status"], "blocked")
        self.assertIn("pm_ops_unavailable", payload["read_errors"]["pm"])

    def make_obligation(self, *, session_id: str = "pm-session-1", age_seconds: float = 7 * 60 * 60) -> None:
        start = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        evidence = json.dumps({
            "producer": "heartbeat-session-age-clear",
            "session_id": session_id,
            "session_started_at": start.isoformat().replace("+00:00", "Z"),
            "age_seconds": age_seconds,
            "threshold_seconds": 6 * 60 * 60,
        })
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute(SCHEMA.replace("CREATE TABLE obligations", "CREATE TABLE IF NOT EXISTS obligations"))
            connection.execute("INSERT INTO obligations(id,created_at,updated_at,status,kind,severity,target_type,target_id,owner,title,required_action,horizon,evidence_json) VALUES (15606,'now','now','open','pm-self-clear','high','session','pm-dhruva','pm','PM self-clear context','At next safe Stop boundary','heartbeat',?)", (evidence,))
            connection.commit()

    def test_stop_consumer_surfaces_same_open_obligation_without_pm_operator(self) -> None:
        self.make_obligation(session_id="current", age_seconds=6 * 60 * 60 + 1)
        transcript = self.pm_dir / "current.jsonl"
        self.write_session(age_seconds=6 * 60 * 60 + 1, session_id="current")
        result = subprocess.run(["python3", str(STOP_HOOK)], input=json.dumps({
            "session_id": "current",
            "transcript_path": str(transcript),
            "stop_hook_active": False,
        }), text=True, capture_output=True, env={**os.environ, "PM_OPS_DB": str(self.db)})
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["decision"], "block")
        self.assertEqual(payload["obligation_id"], 15606)
        self.assertNotIn("pm-operator", result.stdout)

    def test_stop_consumer_defers_old_row_for_fresh_session(self) -> None:
        self.make_obligation(session_id="old-session")
        transcript = self.pm_dir / "fresh-session.jsonl"
        self.write_session(age_seconds=60, session_id="fresh-session")
        result = subprocess.run(["python3", str(STOP_HOOK)], input=json.dumps({
            "session_id": "fresh-session",
            "transcript_path": str(transcript),
            "stop_hook_active": False,
        }), text=True, capture_output=True, env={**os.environ, "PM_OPS_DB": str(self.db)})
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "deferred")
        self.assertEqual(payload["reason"], "current_session_mismatch")

    def test_stop_consumer_defers_unknown_identity_and_recursive_feedback(self) -> None:
        self.make_obligation(session_id="current", age_seconds=7 * 60 * 60)
        unknown = subprocess.run(["python3", str(STOP_HOOK)], input="{}", text=True, capture_output=True, env={**os.environ, "PM_OPS_DB": str(self.db)})
        self.assertEqual(json.loads(unknown.stdout)["reason"], "current_session_identity_unavailable")

        age_unknown = subprocess.run(["python3", str(STOP_HOOK)], input=json.dumps({
            "session_id": "current",
        }), text=True, capture_output=True, env={**os.environ, "PM_OPS_DB": str(self.db)})
        self.assertEqual(json.loads(age_unknown.stdout)["reason"], "current_session_age_unavailable")

        transcript = self.pm_dir / "current.jsonl"
        self.write_session(age_seconds=7 * 60 * 60, session_id="current")
        recursive = subprocess.run(["python3", str(STOP_HOOK)], input=json.dumps({
            "session_id": "current",
            "transcript_path": str(transcript),
            "stop_hook_active": True,
        }), text=True, capture_output=True, env={**os.environ, "PM_OPS_DB": str(self.db)})
        payload = json.loads(recursive.stdout)
        self.assertEqual(payload["status"], "deferred")
        self.assertEqual(payload["reason"], "stop_hook_active")

    def test_stop_consumer_same_session_not_due(self) -> None:
        self.make_obligation(session_id="current", age_seconds=6 * 60 * 60 - 30)
        transcript = self.pm_dir / "current.jsonl"
        self.write_session(age_seconds=6 * 60 * 60 - 30, session_id="current")
        result = subprocess.run(["python3", str(STOP_HOOK)], input=json.dumps({
            "session_id": "current",
            "transcript_path": str(transcript),
            "stop_hook_active": False,
        }), text=True, capture_output=True, env={**os.environ, "PM_OPS_DB": str(self.db)})
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "not_due")
        self.assertEqual(payload["reason"], "current_session_not_due")

    def test_validator_includes_pm_self_clear_in_existing_stop_surface(self) -> None:
        self.make_obligation(session_id="current", age_seconds=7 * 60 * 60)
        transcript = self.pm_dir / "current.jsonl"
        self.write_session(age_seconds=7 * 60 * 60, session_id="current")
        status = self.root / "status.py"
        status.write_text(textwrap.dedent(f"""
            #!/usr/bin/env python3
            import json
            print(json.dumps({{"db": {str(self.db)!r}}}))
        """).lstrip())
        status.chmod(0o755)
        result = subprocess.run(["bash", str(VALIDATOR)], input=json.dumps({
            "session_id": "current",
            "transcript_path": str(transcript),
            "stop_hook_active": False,
        }), text=True, capture_output=True, env={**os.environ, "PM_OPS": str(status)})
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["decision"], "block")
        self.assertIn("obligation:15606", payload["message"])

    def test_validator_defers_old_row_for_fresh_session(self) -> None:
        self.make_obligation(session_id="old-session")
        transcript = self.pm_dir / "fresh-session.jsonl"
        self.write_session(age_seconds=60, session_id="fresh-session")
        status = self.root / "status.py"
        status.write_text(textwrap.dedent(f"""
            #!/usr/bin/env python3
            import json
            print(json.dumps({{"db": {str(self.db)!r}}}))
        """).lstrip())
        status.chmod(0o755)
        result = subprocess.run(["bash", str(VALIDATOR)], input=json.dumps({
            "session_id": "fresh-session",
            "transcript_path": str(transcript),
            "stop_hook_active": False,
        }), text=True, capture_output=True, env={**os.environ, "PM_OPS": str(status)})
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "deferred")
        self.assertIn("current_session_mismatch", payload["reason"])

    def test_validator_defers_recursive_stop_feedback(self) -> None:
        self.make_obligation(session_id="current", age_seconds=7 * 60 * 60)
        transcript = self.pm_dir / "current.jsonl"
        self.write_session(age_seconds=7 * 60 * 60, session_id="current")
        status = self.root / "status.py"
        status.write_text(textwrap.dedent(f"""
            #!/usr/bin/env python3
            import json
            print(json.dumps({{"db": {str(self.db)!r}}}))
        """).lstrip())
        status.chmod(0o755)
        result = subprocess.run(["bash", str(VALIDATOR)], input=json.dumps({
            "session_id": "current",
            "transcript_path": str(transcript),
            "stop_hook_active": True,
        }), text=True, capture_output=True, env={**os.environ, "PM_OPS": str(status)})
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "deferred")
        self.assertIn("stop_hook_active", payload["reason"])


if __name__ == "__main__":
    unittest.main()
