#!/usr/bin/env python3
"""Focused contract tests for the mop-assign-slot client boundary.

The script is the frozen sanctioned assignment interface, so these tests drive
the real script against a stub MoP HTTP surface and pin the one-JSON-terminal
contract, the non-zero typed refusal, and the deterministic effect id that
makes a retry resume the same durable effect instead of minting a second
assignment.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parents[3]
SCRIPT = ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "scripts" / "mop-assign-slot.py"

TASK_TEXT = "Implement issue 8110: harden the atomic assignment boundary.\n"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D102 - keep the test output quiet
        return

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - stdlib signature
        server = self.server  # type: ignore[assignment]
        server.requests.append(("GET", self.path, None))
        self._send(200, {"slot": 3, "occupied": False, "assignment_epoch": 41})

    def do_POST(self):  # noqa: N802 - stdlib signature
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        server = self.server  # type: ignore[assignment]
        server.requests.append(("POST", self.path, body))
        scripted = server.responses.pop(0) if server.responses else (200, {
            "status": "assigned",
            "slot": 3,
            "assignment_epoch": 42,
            "ownership_receipt": {"issue": 8110},
            "delivery_receipt": {"verified": True},
        })
        self._send(*scripted)


class _Server(ThreadingHTTPServer):
    requests: list
    responses: list


class MopAssignSlotClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _Server(("127.0.0.1", 0), _Handler)
        self.server.requests = []
        self.server.responses = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.temp = tempfile.TemporaryDirectory()
        self.task_file = Path(self.temp.name) / "task.md"
        self.task_file.write_text(TASK_TEXT, encoding="utf-8")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def run_script(self, *extra: str, task_file: Path | None = None):
        command = [
            sys.executable,
            str(SCRIPT),
            "--slot", "3",
            "--class", "new_issue",
            "--repo-id", "github:heydonna-app/heydonna-app",
            "--issue", "8110",
            "--branch", "fix/8110-assignment-boundary",
            "--head", "a" * 40,
            "--work-kind", "implementation",
            "--handoff", "handoff-8110",
            "--task-file", str(task_file or self.task_file),
            *extra,
        ]
        env = {"MOP_PORT": str(self.port), "PATH": "/usr/bin:/bin"}
        return subprocess.run(command, capture_output=True, text=True, env=env, check=False)

    def test_success_emits_one_json_terminal_with_both_receipts(self) -> None:
        completed = self.run_script()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        terminal = json.loads(completed.stdout.strip())
        self.assertEqual(terminal["status"], "assigned")
        self.assertEqual(terminal["slot"], 3)
        self.assertEqual(terminal["assignment_epoch"], 42)
        self.assertEqual(terminal["ownership_receipt"]["issue"], 8110)
        self.assertEqual(terminal["delivery_receipt"]["verified"], True)
        posts = [r for r in self.server.requests if r[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1], "/slots/3/assign-effect")
        self.assertEqual(posts[0][2]["task"], TASK_TEXT)

    def test_typed_refusal_exits_non_zero_and_names_the_sanctioned_path(self) -> None:
        self.server.responses = [(502, {
            "status": "refused",
            "step_failed": "delivery",
            "reason": "session_delivery_unverified",
            "slot_state_after": "occupied=true issue=8110 assignment_epoch=42 task_present=true",
            "sanctioned_path": "mop-assign-slot",
        })]
        completed = self.run_script()
        self.assertEqual(completed.returncode, 2)
        terminal = json.loads(completed.stdout.strip())
        self.assertEqual(terminal["status"], "refused")
        self.assertEqual(terminal["step_failed"], "delivery")
        self.assertEqual(terminal["sanctioned_path"], "mop-assign-slot")
        self.assertIn("task_present=true", terminal["slot_state_after"])

    def test_empty_task_text_refuses_before_any_network_call(self) -> None:
        empty = Path(self.temp.name) / "empty.md"
        empty.write_text("   \n", encoding="utf-8")
        completed = self.run_script(task_file=empty)
        self.assertEqual(completed.returncode, 2)
        terminal = json.loads(completed.stdout.strip())
        self.assertEqual(terminal["reason"], "empty_task_text")
        self.assertEqual(self.server.requests, [])

    def test_unreachable_mop_refuses_typed(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        completed = self.run_script()
        self.assertEqual(completed.returncode, 2)
        terminal = json.loads(completed.stdout.strip())
        self.assertEqual(terminal["reason"], "slot_read_failed")

    def test_effect_id_is_deterministic_so_a_retry_resumes_the_same_effect(self) -> None:
        first = self.run_script()
        second = self.run_script()
        self.assertEqual(first.returncode, 0)
        self.assertEqual(second.returncode, 0)
        posts = [r for r in self.server.requests if r[0] == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0][2]["effect_id"], posts[1][2]["effect_id"])
        self.assertTrue(posts[0][2]["effect_id"].startswith("assign-"))


if __name__ == "__main__":
    unittest.main()
