#!/usr/bin/env python3
"""Focused tests for the read-only queue preview helper.

A fake app-server speaks the real JSON-RPC framing so the helper is exercised
end to end through the ordinary shell interface. Every case asserts the outbound
method audit, which is what proves the helper is read-only: only initialize,
initialized and thread/queue/list may ever leave the process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
HELPER = Path(__file__).resolve().parent / "queue_preview.py"
THREAD_ID = "01a09112-a09c-7361-9a2a-0ada6a4e9dfb"
ALLOWLIST = ["initialize", "initialized", "thread/queue/list"]

FAKE_SERVER = """
#!/usr/bin/env python3
import json, os, sys
mode = os.environ.get("FAKE_MODE", "ok")
audit = os.environ.get("FAKE_AUDIT")
for line in sys.stdin:
    try:
        req = json.loads(line)
    except json.JSONDecodeError:
        continue
    method = req.get("method")
    if method and audit:
        with open(audit, "a") as handle:
            handle.write(method + "\\n")
    rid = req.get("id")
    if rid is None:
        continue
    if mode == "exit" and method == "thread/queue/list":
        sys.exit(3)
    if mode == "partial" and method == "thread/queue/list":
        sys.stdout.write('{"jsonrpc":"2.0","id":%d,"result":{"data":[' % rid)
        sys.stdout.flush()
        import time as _t
        _t.sleep(60)
        continue
    if mode == "timeout" and method == "thread/queue/list":
        continue
    if method == "initialize":
        reply = {"jsonrpc": "2.0", "id": rid, "result": {}}
    elif method == "thread/queue/list":
        if mode == "malformed":
            reply = {"jsonrpc": "2.0", "id": rid, "result": {"nope": True}}
        elif mode == "error":
            reply = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unavailable"}}
        else:
            reply = {"jsonrpc": "2.0", "id": rid, "result": json.loads(os.environ["FAKE_RESULT"])}
    else:
        reply = {"jsonrpc": "2.0", "id": rid, "result": {}}
    print(json.dumps(reply), flush=True)
""".lstrip()


class QueuePreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake = self.root / "fake-codex"
        self.fake.write_text(FAKE_SERVER, encoding="utf-8")
        self.fake.chmod(0o755)
        self.audit = self.root / "audit.txt"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_helper(self, *, result: dict | None = None, mode: str = "ok", extra: list[str] | None = None,
                   thread_id: str = THREAD_ID):
        env = dict(os.environ)
        env.update({"FAKE_MODE": mode, "FAKE_AUDIT": str(self.audit)})
        if result is not None:
            env["FAKE_RESULT"] = json.dumps(result)
        command = [sys.executable, str(HELPER), "--thread-id", thread_id,
                   "--codex-bin", str(self.fake), *[a for a in (extra or [])]]
        done = subprocess.run(command, capture_output=True, text=True, env=env, check=False, timeout=30)
        terminal = json.loads(done.stdout.strip()) if done.stdout.strip() else {}
        return done.returncode, terminal

    def audit_methods(self) -> list[str]:
        if not self.audit.exists():
            return []
        return [m for m in self.audit.read_text(encoding="utf-8").split("\n") if m]

    def test_nonempty_ordered_queue_retains_raw_ids(self) -> None:
        result = {"data": [
            {"id": "sub-1", "clientUserMessageId": "cum-1", "input": [{"type": "text", "text": "first"}]},
            {"id": "sub-2", "clientUserMessageId": "cum-2", "input": [{"type": "text", "text": "second"}]},
        ]}
        code, term = self.run_helper(result=result)
        self.assertEqual(code, 0)
        self.assertEqual(term["status"], "queue_preview_ok")
        self.assertEqual([e["id"] for e in term["entries"]], ["sub-1", "sub-2"], "order preserved")
        self.assertEqual([e["clientUserMessageId"] for e in term["entries"]], ["cum-1", "cum-2"])
        self.assertFalse(term["has_more"])
        self.assertIsNone(term["nextCursor"])

    def test_task_handoff_and_slack_shaped_inputs_are_previewed(self) -> None:
        result = {"data": [
            {"id": "s", "clientUserMessageId": "c",
             "input": [{"type": "text", "text": "<codex_delegation><input>do the thing</input></codex_delegation>"}]},
            {"id": "s2", "clientUserMessageId": "c2",
             "input": [{"type": "text", "text": "Slack mention: <@U123> ship it"}]},
        ]}
        code, term = self.run_helper(result=result)
        self.assertEqual(code, 0)
        self.assertIn("do the thing", term["entries"][0]["preview"])
        self.assertIn("ship it", term["entries"][1]["preview"])

    def test_empty_queue_is_ok_not_an_error(self) -> None:
        code, term = self.run_helper(result={"data": []})
        self.assertEqual(code, 0)
        self.assertEqual(term["status"], "queue_preview_ok")
        self.assertEqual(term["count"], 0)
        self.assertEqual(term["entries"], [])

    def test_pagination_reports_cursor_and_has_more(self) -> None:
        code, term = self.run_helper(result={"data": [{"id": "a", "clientUserMessageId": "b", "input": []}],
                                              "nextCursor": "cur-2"})
        self.assertEqual(code, 0)
        self.assertEqual(term["nextCursor"], "cur-2")
        self.assertTrue(term["has_more"])

    def test_truncation_and_huge_bodies_are_flagged(self) -> None:
        big = "x" * 5000
        code, term = self.run_helper(result={"data": [{"id": "a", "clientUserMessageId": "b",
                                                       "input": [{"type": "text", "text": big}]}]},
                                     extra=["--preview-chars", "50"])
        self.assertEqual(code, 0)
        entry = term["entries"][0]
        self.assertEqual(len(entry["preview"]), 50)
        self.assertTrue(entry["preview_truncated"])
        self.assertEqual(entry["input_chars"], 5000)

    def test_unavailable_timeout_and_malformed_are_typed_never_empty(self) -> None:
        for mode, expected in (("malformed", "queue_preview_malformed"),
                               ("timeout", "queue_preview_timeout"),
                               ("error", "queue_preview_unavailable"),
                               ("exit", "queue_preview_unavailable")):
            code, term = self.run_helper(result={"data": []}, mode=mode, extra=["--timeout-seconds", "3"])
            self.assertNotEqual(code, 0, f"{mode} must not exit 0")
            self.assertEqual(term["status"], expected, mode)
            self.assertNotIn("entries", term, "an error must never be rendered as a queue")
            self.assertIn("error", term)

    def test_invalid_thread_id_is_refused_before_any_rpc(self) -> None:
        code, term = self.run_helper(result={"data": []}, thread_id="not a thread id!")
        self.assertEqual(code, 2)
        self.assertEqual(term["status"], "queue_preview_invalid_args")
        self.assertEqual(term["methods_sent"], [], "no RPC may be sent for invalid args")

    def test_outbound_methods_are_allowlisted_on_every_success_path(self) -> None:
        self.run_helper(result={"data": []})
        methods = self.audit_methods()
        self.assertEqual(methods, ALLOWLIST, f"unexpected outbound methods: {methods}")
        for forbidden in ("thread/queue/add", "turn/start", "thread/resume", "thread/queue/remove"):
            self.assertNotIn(forbidden, methods)


class MalformedAndDeadlineTests(unittest.TestCase):
    """The two runtime defects PR Reviews reproduced on 73161610."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake = self.root / "fake-codex"
        self.fake.write_text(FAKE_SERVER, encoding="utf-8")
        self.fake.chmod(0o755)
        self.audit = self.root / "audit.txt"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_helper(self, *, result=None, mode="ok", extra=None, outer=30):
        env = dict(os.environ)
        env.update({"FAKE_MODE": mode, "FAKE_AUDIT": str(self.audit)})
        if result is not None:
            env["FAKE_RESULT"] = json.dumps(result)
        cmd = [sys.executable, str(HELPER), "--thread-id", THREAD_ID,
               "--codex-bin", str(self.fake), *(extra or [])]
        done = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False, timeout=outer)
        return done.returncode, (json.loads(done.stdout.strip()) if done.stdout.strip() else {})

    def test_all_invalid_entries_are_a_protocol_failure_not_an_empty_success(self) -> None:
        code, term = self.run_helper(result={"data": [123, "x"]})
        self.assertNotEqual(code, 0, "all-invalid entries must not exit 0")
        self.assertEqual(term["status"], "queue_preview_malformed")
        self.assertNotIn("entries", term, "a malformed page must never render as an empty queue")

    def test_mixed_valid_and_invalid_entries_reject_the_whole_page(self) -> None:
        code, term = self.run_helper(result={"data": [{"id": "a", "clientUserMessageId": "b", "input": []}, 7]})
        self.assertNotEqual(code, 0)
        self.assertEqual(term["status"], "queue_preview_malformed")

    def test_genuine_empty_queue_still_succeeds(self) -> None:
        code, term = self.run_helper(result={"data": []})
        self.assertEqual(code, 0)
        self.assertEqual(term["entries"], [])

    def test_partial_line_stall_times_out_under_the_deadline(self) -> None:
        start = time.monotonic()
        code, term = self.run_helper(mode="partial", extra=["--timeout-seconds", "2"], outer=25)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 20, f"must not hang past the deadline (took {elapsed:.1f}s)")
        self.assertEqual(code, 3)
        self.assertEqual(term["status"], "queue_preview_timeout")
        self.assertNotIn("entries", term)

    def test_oversized_body_is_flagged_rather_than_dropped(self) -> None:
        big = "y" * 250_000
        code, term = self.run_helper(result={"data": [{"id": "a", "clientUserMessageId": "b",
                                                       "input": [{"type": "text", "text": big}]}]})
        self.assertEqual(code, 0)
        entry = term["entries"][0]
        self.assertTrue(entry["body_truncated"], ">200k bodies must be flagged")
        self.assertEqual(entry["input_chars"], 250_000)


if __name__ == "__main__":
    unittest.main()
