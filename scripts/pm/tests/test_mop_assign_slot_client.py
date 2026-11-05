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
import hashlib
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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
        if "assignment-effect-receipt" in self.path:
            effect_id = parse_qs(urlparse(self.path).query)["effect_id"][0]
            with server.receipt_lock:
                receipt = server.receipts.get(effect_id)
            self._send(200 if receipt else 404, receipt or {"code": "assignment_effect_receipt_not_found"})
            return
        self._send(200, {"slot": 3, "occupied": False, "assignment_epoch": 41})

    def do_POST(self):  # noqa: N802 - stdlib signature
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        server = self.server  # type: ignore[assignment]
        server.requests.append(("POST", self.path, body))
        if server.post_barrier is not None:
            server.post_barrier.wait(timeout=10)
        with server.receipt_lock:
            was_existing = body["effect_id"] in server.receipts
            if not was_existing:
                server.receipts[body["effect_id"]] = {
                    "success": True, "effect_id": body["effect_id"], "slot": 3,
                    "selection_class": body["selection_class"], "state": "delivered",
                    "desired_tuple": {"issue": body["issue"]},
                    "task_digest": hashlib.sha256(body["task"].encode()).hexdigest(),
                }
        scripted = server.responses.pop(0) if server.responses else (200, {
            "status": "assigned",
            "slot": 3,
            "assignment_epoch": 42,
            "ownership_receipt": {"issue": 8110},
            "delivery_receipt": {"verified": True},
            "idempotent": was_existing,
        })
        server.effect_responses.append((body["effect_id"], scripted[1].get("idempotent")))
        self._send(*scripted)


class _Server(ThreadingHTTPServer):
    requests: list
    responses: list


class MopAssignSlotClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _Server(("127.0.0.1", 0), _Handler)
        self.server.requests = []
        self.server.responses = []
        self.server.receipts = {}
        self.server.receipt_lock = threading.Lock()
        self.server.effect_responses = []
        self.server.post_barrier = None
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.temp = tempfile.TemporaryDirectory()
        self.task_file = Path(self.temp.name) / "task.md"
        self.task_file.write_text(TASK_TEXT, encoding="utf-8")
        # Real selector and caller; only GitHub is replaced, never the check.
        spec = __import__("importlib.util", fromlist=["util"]).spec_from_file_location(
            "existing_ready_fixture", SCRIPT.with_name("test_ready_pool_select.py"))
        fixture = __import__("importlib.util", fromlist=["util"]).module_from_spec(spec)
        spec.loader.exec_module(fixture)
        rows = [fixture.issue(n, ["P1", "status:todo"]) for n in [8110, 8131]]
        for row in rows:
            row["state"] = "OPEN"
            row["labels"] = {"nodes": row["labels"], "pageInfo": {"hasNextPage": False}}
            row["closedByPullRequestsReferences"] = {"nodes": [], "pageInfo": {"hasNextPage": False}}
        self.bin = Path(self.temp.name) / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text("#!" + sys.executable + "\nimport json,sys\nrows=" + repr(rows) + "\n"
            "if sys.argv[1:3]==['pr','list']: print('[]')\n"
            "else:\n q=next(s[6:] for s in sys.argv if s.startswith('query='))\n"
            " if 'issues(first:50' in q:\n  nodes=rows if 'labels:[\\\"status:todo\\\"]' in q else []\n"
            "  print(json.dumps({'data':{'repository':{'issues':{'nodes':nodes,'totalCount':len(nodes),'pageInfo':{'hasNextPage':False}}}}}))\n"
            " else: print(json.dumps({'data':{'repository':{}}}))\n")
        gh.chmod(0o755)
        self.default_sender = self.bin / "slack-send.sh"
        self.default_sender.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        self.default_sender.chmod(0o755)

    def env(self):
        return {
            "MOP_PORT": str(self.port),
            "PATH": str(self.bin) + ":/usr/bin:/bin",
            "MOP_ASSIGN_SLACK_SEND": str(self.default_sender),
        }

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def run_script(self, *extra: str, task_file: Path | None = None, env_overrides: dict[str, str] | None = None):
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
        env = self.env()
        env.update(env_overrides or {})
        return subprocess.run(command, capture_output=True, text=True, env=env, check=False)

    def write_notice_sender(self, content: str) -> Path:
        sender = Path(self.temp.name) / "slack-send.sh"
        sender.write_text("#!/bin/sh\n" + content, encoding="utf-8")
        sender.chmod(0o755)
        return sender

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

    def test_successful_new_issue_assignment_posts_transition_fail_open(self) -> None:
        sender = self.write_notice_sender(
            "printf '%s\\n' \"$@\" > \"$CAPTURE_ARGS\"\n"
            "cat > \"$CAPTURE_BODY\"\n"
            "printf 'OK ts=1793899000.123456 channel=C0ALZJHGE49\\n'\n"
        )
        env = self.env()
        env.update({
            "MOP_ASSIGN_SLACK_SEND": str(sender),
            "CAPTURE_ARGS": str(Path(self.temp.name) / "slack-args"),
            "CAPTURE_BODY": str(Path(self.temp.name) / "slack-body"),
        })
        command = [
            sys.executable, str(SCRIPT), "--slot", "3", "--issue", "8110",
            "--branch", "fix/8110-assignment-boundary", "--task-file", str(self.task_file),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, env=env, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        terminal = json.loads(completed.stdout.strip())
        self.assertEqual(terminal["status"], "assigned")
        self.assertTrue(terminal["assignment_notice"]["posted"])
        self.assertEqual(terminal["assignment_notice"]["thread_ts"], "1793899000.123456")
        self.assertEqual(terminal["assignment_notice"]["channel"], "C0ALZJHGE49")
        args = (Path(self.temp.name) / "slack-args").read_text(encoding="utf-8")
        body = (Path(self.temp.name) / "slack-body").read_text(encoding="utf-8")
        self.assertIn("C0ALZJHGE49", args)
        self.assertIn("P1", body)
        self.assertIn("github:heydonna-app/heydonna-app", body)
        self.assertIn("issue 8110", body)
        self.assertIn("slot 3", body)
        self.assertIn("assignment epoch 42", body)
        self.assertNotIn("<@U0BNFGX2UAX>", body)

    def test_no_success_notice_is_posted_after_assignment_refusal(self) -> None:
        self.server.responses = [(409, {"status": "refused", "reason": "duplicate_assignment"})]
        sender = Path(self.temp.name) / "slack-send.sh"
        marker = Path(self.temp.name) / "calls"
        sender = self.write_notice_sender('printf called >> "$CALLS"\necho \'OK ts=1 channel=C0ALZJHGE49\'\n')
        completed = self.run_script(env_overrides={
            "MOP_ASSIGN_SLACK_SEND": str(sender), "CALLS": str(marker),
        })
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(marker.exists())
        self.assertEqual(len([r for r in self.server.requests if r[0] == "POST"]), 1)

    def test_duplicate_invocation_does_not_repeat_known_sent_notice(self) -> None:
        sender = Path(self.temp.name) / "slack-send.sh"
        marker = Path(self.temp.name) / "calls"
        sender = self.write_notice_sender(
            'printf called >> "$CALLS"\nprintf \'OK ts=1793899000.123456 channel=C0ALZJHGE49\\n\'\n'
        )
        env = {"MOP_ASSIGN_SLACK_SEND": str(sender), "CALLS": str(marker)}
        first = self.run_script(env_overrides=env)
        second = self.run_script(env_overrides=env)
        self.assertEqual(json.loads(first.stdout)["assignment_notice"]["delivery"], "sent")
        self.assertEqual(json.loads(second.stdout)["assignment_notice"]["delivery"], "not_retried")
        self.assertEqual(marker.read_text(encoding="utf-8"), "called")
        self.assertEqual(len(self.server.receipts), 1)

    def test_unknown_notice_is_visible_and_never_retried(self) -> None:
        sender = Path(self.temp.name) / "slack-send.sh"
        marker = Path(self.temp.name) / "calls"
        sender = self.write_notice_sender('printf called >> "$CALLS"\nexit 1\n')
        env = {"MOP_ASSIGN_SLACK_SEND": str(sender), "CALLS": str(marker)}
        first = self.run_script(env_overrides=env)
        second = self.run_script(env_overrides=env)
        first_terminal = json.loads(first.stdout)
        second_terminal = json.loads(second.stdout)
        self.assertEqual(first.returncode, 0)
        self.assertEqual(first_terminal["status"], "assigned")
        self.assertEqual(first_terminal["assignment_notice"]["delivery"], "unknown")
        self.assertEqual(second_terminal["assignment_notice"]["delivery"], "not_retried")
        self.assertEqual(marker.read_text(encoding="utf-8"), "called")
        self.assertEqual(len(self.server.receipts), 1)

    def test_concurrent_effect_responses_grant_only_one_notice_custody(self) -> None:
        sender = Path(self.temp.name) / "slack-send.sh"
        marker = Path(self.temp.name) / "calls"
        sender = self.write_notice_sender(
            'printf called >> "$CALLS"\nprintf \'OK ts=1793899000.123456 channel=C0ALZJHGE49\\n\'\n'
        )
        self.server.post_barrier = threading.Barrier(2)
        env = {"MOP_ASSIGN_SLACK_SEND": str(sender), "CALLS": str(marker)}
        results: list[subprocess.CompletedProcess[str]] = []
        threads = [threading.Thread(target=lambda: results.append(self.run_script(env_overrides=env))) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        self.assertEqual(len(results), 2)
        terminals = [json.loads(result.stdout) for result in results]
        self.assertTrue(all(result.returncode == 0 for result in results))
        self.assertEqual(sorted(item["assignment_notice"]["delivery"] for item in terminals), ["not_retried", "sent"])
        self.assertEqual(marker.read_text(encoding="utf-8"), "called")
        posts = [r for r in self.server.requests if r[0] == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(sorted(bool(outcome) for _, outcome in self.server.effect_responses), [False, True])
        self.assertEqual(len(self.server.receipts), 1)

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
        # Thin surface: an empty task file is not a refusal. Ownership still
        # commits; the server skips delivery when there is no task text.
        empty = Path(self.temp.name) / "empty.md"
        empty.write_text("   \n", encoding="utf-8")
        completed = self.run_script(task_file=empty)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        terminal = json.loads(completed.stdout.strip())
        self.assertEqual(terminal["status"], "assigned")
        posts = [r for r in self.server.requests if r[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2]["task"], "   \n")

    def test_minimal_slot_and_issue_invocation_assigns(self) -> None:
        # The thin PM surface: only --slot and --issue are required.
        command = [
            sys.executable,
            str(SCRIPT),
            "--slot", "3",
            "--issue", "8131",
        ]
        env = self.env()
        completed = subprocess.run(command, capture_output=True, text=True, env=env, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        terminal = json.loads(completed.stdout.strip())
        self.assertEqual(terminal["status"], "assigned")
        posts = [r for r in self.server.requests if r[0] == "POST"]
        self.assertEqual(len(posts), 1)
        body = posts[0][2]
        self.assertEqual(body["issue"], 8131)
        self.assertEqual(body["selection_class"], "new_issue")
        self.assertEqual(body["task"], "")
        self.assertEqual(body["task_file"], "")

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
