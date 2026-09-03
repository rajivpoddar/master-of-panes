from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.server
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets"
RUNTIME = SHARED / "claude" / "control_plane" / "runtime_observation.py"
CLEAR = SHARED / "claude" / "scripts" / "heartbeat-session-age-clear.py"
SAKSHI = SHARED / "claude" / "scripts" / "pm" / "control-plane" / "sakshi-heartbeat.py"
MANIFEST = SHARED / "manifest.json"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class RuntimeObservationRestoreTests(unittest.TestCase):
    def test_manifest_maps_repository_owned_pm_operator_free_runtime_and_client(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        entries = {item["source_path"]: item for item in manifest["entries"]}
        expected = {
            "claude/control_plane/runtime_observation.py": (
                "/Users/rajiv/.claude/control_plane/runtime_observation.py",
                0o644,
            ),
            "claude/scripts/heartbeat-session-age-clear.py": (
                "/Users/rajiv/.claude/scripts/heartbeat-session-age-clear.py",
                0o755,
            ),
            "claude/scripts/pm/control-plane/sakshi-heartbeat.py": (
                "/Users/rajiv/.claude/scripts/sakshi-heartbeat.py",
                0o755,
            ),
        }
        for source_path, (target, mode) in expected.items():
            source = SHARED / source_path
            entry = entries[source_path]
            self.assertEqual(entry["canonical_target"], target)
            self.assertEqual(entry["mode"], mode)
            self.assertEqual(entry["dependency_status"], "closed")
            self.assertEqual(entry["dependencies"], [])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), entry["sha256"])
        runtime_text = RUNTIME.read_text(encoding="utf-8")
        client_text = CLEAR.read_text(encoding="utf-8")
        self.assertNotIn("pm_operator", runtime_text)
        self.assertNotIn("pm_operator", client_text)
        self.assertNotIn("pm-operator", client_text)
        self.assertIn("/Users/rajiv/.claude/scripts/heartbeat-session-age-clear.py", SAKSHI.read_text(encoding="utf-8"))

    def test_pm_and_six_slot_rows_are_fresh_and_missing_evidence_degrades(self) -> None:
        runtime = load_module("runtime_observation_restore", RUNTIME)
        now = runtime.parse_timestamp("2026-09-03T07:00:00Z")
        assert now is not None
        with tempfile.TemporaryDirectory() as temp:
            sessions = Path(temp) / "claude-projects"
            rows = {}
            for label in ["pm", "1", "2", "3", "4", "5", "6"]:
                project = Path(f"/fixture/heydonna-{label}")
                directory = sessions / runtime.RuntimeObservationAdapter._claude_project_name(project)
                directory.mkdir(parents=True)
                (directory / f"{label}.jsonl").write_text(
                    json.dumps(
                        {
                            "type": "session",
                            "sessionId": f"session-{label}",
                            "timestamp": "2026-09-02T00:00:00Z",
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                adapter = runtime.RuntimeObservationAdapter(
                    claude_projects_root=sessions,
                    runtime_source="claude",
                    runtime_project_dir=project,
                )
                row = adapter.observe_slot(
                    label,
                    mop_row={
                        "occupied": False,
                        "idle": True,
                        "dnd": False,
                        "active_turn_id": None,
                        "active_turn_state": "inactive",
                        "session_id": f"session-{label}",
                        "session_started_at": "2026-09-02T00:00:00Z",
                    },
                    now=now,
                )
                rows[label] = row
            self.assertEqual(set(rows), {"pm", "1", "2", "3", "4", "5", "6"})
            for label, row in rows.items():
                self.assertEqual(row.session_id, f"session-{label}")
                self.assertIsNone(row.error)
                self.assertFalse(row.active)
                self.assertTrue(row.idle)
                self.assertTrue(row.clear_due)
            missing = runtime.RuntimeObservationAdapter(
                claude_projects_root=Path(temp) / "missing",
                runtime_source="claude",
                runtime_project_dir=Path("/fixture/missing"),
            ).observe_slot("1", mop_row={"occupied": False}, now=now)
            self.assertIsNotNone(missing.error)
            self.assertIsNone(missing.active)
            self.assertFalse(missing.clear_due)

    def test_omp_s5_and_s6_are_observed_and_clear_due_matches_helper_state(self) -> None:
        runtime = load_module("runtime_observation_omp_restore", RUNTIME)
        now = runtime.parse_timestamp("2026-09-03T07:00:00Z")
        assert now is not None
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for slot in ("5", "6"):
                directory = root / f"heydonna-slot{slot}"
                directory.mkdir()
                (directory / f"session-{slot}.jsonl").write_text(
                    json.dumps({
                        "type": "session",
                        "sessionId": f"omp-session-{slot}",
                        "timestamp": "2026-09-02T00:00:00Z",
                    }) + "\n",
                    encoding="utf-8",
                )
            adapter = runtime.RuntimeObservationAdapter(
                omp_sessions_root=root,
                runtime_source="omp",
            )
            sys.path.insert(0, str(RUNTIME.parent.parent))
            try:
                sakshi = load_module("sakshi_omp_restore", SAKSHI)
            finally:
                sys.path.pop(0)
            for slot in ("5", "6"):
                row = adapter.observe_slot(
                    slot,
                    mop_row={
                        "occupied": False,
                        "idle": True,
                        "dnd": False,
                        "active_turn_id": None,
                        "active_turn_state": "inactive",
                        "session_id": f"db-session-{slot}",
                        "session_started_at": "2026-09-02T01:00:00Z",
                    },
                    now=now,
                )
                self.assertEqual(row.session_id, f"db-session-{slot}")
                self.assertEqual(row.runtime_session_id, f"omp-session-{slot}")
                self.assertEqual(row.session_started_at.isoformat(), "2026-09-02T01:00:00+00:00")
                self.assertEqual(row.source, "omp_top_level")
                self.assertFalse(row.active)
                self.assertTrue(row.idle)
                self.assertTrue(row.clear_due)

                with mock.patch.object(sakshi, "OMP_SESSIONS_ROOT", root):
                    sakshi_row = sakshi.analyze_session(
                        {"id": slot, "label": f"S{slot}", "pane": int(slot), "runtime_source": "omp"},
                        now,
                        mop_row={
                            "occupied": False,
                            "idle": True,
                            "dnd": False,
                            "active_turn_id": None,
                            "active_turn_state": "inactive",
                            "session_id": f"db-session-{slot}",
                            "session_started_at": "2026-09-02T01:00:00Z",
                        },
                    )
                self.assertTrue(sakshi_row["clear_due"])
                self.assertEqual(sakshi_row["session_id"], f"db-session-{slot}")
                self.assertEqual(sakshi_row["session_started_at"], "2026-09-02T01:00:00+00:00")

                # The fields Sakshi emits for clear_due are the same exact
                # free/idle tuple consumed by the one-shot helper fence.
                helper_args = argparse.Namespace(
                    expected_epoch=0,
                    expected_session_id=row.session_id,
                    expected_session_started_at=row.session_started_at.isoformat() if row.session_started_at else "",
                )
                helper_observation = {
                    "assignment_epoch": row.assignment_epoch or 0,
                    "session_id": sakshi_row["session_id"],
                    "session_started_at": sakshi_row["session_started_at"],
                    "occupied": row.occupied,
                    "dnd": row.dnd,
                    "idle": row.idle,
                    "active_turn_id": row.active_turn_id,
                    "active_turn_state": row.active_turn_state,
                }
                self.assertEqual(adapter.clear_due_for(
                    row.effective_start,
                    occupied=helper_observation["occupied"],
                    idle=helper_observation["idle"],
                    active=row.active,
                    session_id=row.session_id,
                    session_started_at=row.session_started_at,
                    now=now,
                ), True)
                client = load_module("heartbeat_session_age_clear_omp", CLEAR)
                self.assertEqual(client._fence(helper_args, helper_observation), (True, None))

    def test_session_age_dry_run_uses_authenticated_stub_and_has_zero_effect(self) -> None:
        client = load_module("heartbeat_session_age_clear_restore", CLEAR)
        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(
                    {
                        "path": self.path,
                        "authority": self.headers.get("x-heydonna-direct-client"),
                        "capability": self.headers.get("x-mop-capability"),
                    }
                )
                body = json.dumps(
                    {
                        "assignment_epoch": 7,
                        "session_id": "session-2",
                        "session_started_at": "2026-09-02T00:00:00+00:00",
                        "occupied": False,
                        "dnd": False,
                        "idle": True,
                        "active_turn_id": None,
                        "active_turn_state": "inactive",
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            argv = [
                str(CLEAR),
                "--slot", "2",
                "--expected-epoch", "7",
                "--expected-session-id", "session-2",
                "--expected-session-started-at", "2026-09-02T00:00:00+00:00",
                "--expected-age-seconds", str(7 * 60 * 60),
                "--checkout-path", "/fixture/checkout",
                "--checkout-branch", "main",
                "--checkout-head", "a" * 40,
                "--request-token", "request-2",
                "--base-url", f"http://127.0.0.1:{server.server_port}",
                "--dry-run",
            ]
            output = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"MOP_LOCAL_CAPABILITY": "c" * 64}, clear=False),
                mock.patch.object(client, "_checkout_observation", return_value=(True, {"checkout_clean": True, "unpushed_commits": []})),
                mock.patch.object(sys, "argv", argv),
                contextlib.redirect_stdout(output),
            ):
                code = client.main()
            self.assertEqual(code, 0)
            result = json.loads(output.getvalue())
            self.assertTrue(result["success"])
            self.assertFalse(result["effect"])
            self.assertEqual(result["mode"], "dry-run")
            self.assertEqual(requests, [{
                "path": "/slots/2",
                "authority": "mop-release-assign-v1",
                "capability": "c" * 64,
            }])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_client_fails_closed_before_network_for_missing_capability_or_non_loopback(self) -> None:
        client = load_module("heartbeat_session_age_clear_negative", CLEAR)
        common = [
            str(CLEAR),
            "--slot", "1",
            "--expected-epoch", "1",
            "--expected-session-id", "s",
            "--expected-session-started-at", "2026-09-02T00:00:00+00:00",
            "--expected-age-seconds", "21601",
            "--checkout-path", "/fixture/checkout",
            "--checkout-branch", "main",
            "--checkout-head", "b" * 40,
            "--request-token", "request-1",
            "--dry-run",
        ]
        for base_url, env in [
            ("http://127.0.0.1:3100", {"MOP_LOCAL_CAPABILITY": ""}),
            ("http://localhost:3100", {"MOP_LOCAL_CAPABILITY": "d" * 64}),
            ("http://user:pass@127.0.0.1:3100", {"MOP_LOCAL_CAPABILITY": "d" * 64}),
            ("http://127.0.0.1:3100/path", {"MOP_LOCAL_CAPABILITY": "d" * 64}),
            ("http://127.0.0.1:3100?redirect=1", {"MOP_LOCAL_CAPABILITY": "d" * 64}),
        ]:
            with (
                mock.patch.dict(os.environ, env, clear=False),
                mock.patch.object(sys, "argv", common + ["--base-url", base_url]),
            ):
                self.assertEqual(client.main(), 2, base_url)


if __name__ == "__main__":
    unittest.main()
