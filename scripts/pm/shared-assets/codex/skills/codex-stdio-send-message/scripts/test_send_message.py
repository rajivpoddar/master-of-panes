#!/usr/bin/env python3

import contextlib
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).with_name("send_message.py")
SPEC = importlib.util.spec_from_file_location("codex_stdio_send_message", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


FAKE_APP_SERVER = textwrap.dedent(
    """
    #!/usr/bin/env python3
    import json
    import os
    import sys

    mode = os.environ["FAKE_MODE"]
    log_path = os.environ["FAKE_LOG"]
    with open(log_path, "w", encoding="utf-8") as log:
        for line in sys.stdin:
            request = json.loads(line)
            method = request.get("method")
            if method:
                log.write(method + "\\n")
                log.flush()
            request_id = request.get("id")
            if request_id is None:
                continue
            if method == "initialize":
                response = {"jsonrpc": "2.0", "id": request_id, "result": {}}
            elif method == "thread/queue/add":
                if mode == "queue-fail":
                    response = {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32000, "message": "queue unavailable"},
                    }
                elif mode == "uncertain":
                    continue
                else:
                    response = {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "queuedSubmission": {
                                "id": "queued-1",
                                "clientUserMessageId": request["params"]["clientUserMessageId"],
                            }
                        },
                    }
            else:
                response = {"jsonrpc": "2.0", "id": request_id, "result": {}}
            print(json.dumps(response), flush=True)
    """
).lstrip()


class QueueOnlyBoundaryTests(unittest.TestCase):
    def run_helper(self, mode: str) -> tuple[int, dict, list[str]]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "fake-codex"
            fake_bin.write_text(FAKE_APP_SERVER, encoding="utf-8")
            fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IXUSR)
            log_path = root / "methods.log"
            output = io.StringIO()
            argv = [
                "send_message.py",
                "--thread-id",
                "thread-1",
                "--dedup-key",
                "event-1",
                "--message",
                "hello",
                "--codex-bin",
                str(fake_bin),
                "--timeout-seconds",
                "1",
            ]
            with patch.dict(
                os.environ,
                {"FAKE_MODE": mode, "FAKE_LOG": str(log_path)},
                clear=False,
            ), patch.object(sys, "argv", argv), contextlib.redirect_stdout(output):
                return_code = MODULE.main()
            receipt = json.loads(output.getvalue())
            methods = log_path.read_text(encoding="utf-8").splitlines()
            return return_code, receipt, methods

    def test_queue_success_is_durable_without_start_request(self) -> None:
        return_code, receipt, methods = self.run_helper("success")

        self.assertEqual(return_code, 0)
        self.assertEqual(receipt["status"], "queued_for_task_consumption")
        self.assertTrue(receipt["queueAccepted"])
        self.assertEqual(receipt["executionOwnership"], "queued")
        self.assertNotIn("startAccepted", receipt)
        self.assertEqual(receipt["queuedSubmissionId"], "queued-1")
        self.assertEqual(
            receipt["clientUserMessageId"],
            MODULE.stable_client_user_message_id("event-1"),
        )
        self.assertEqual(
            methods,
            ["initialize", "initialized", "thread/queue/add"],
        )

    def test_queue_add_failure_has_no_execution_ownership(self) -> None:
        return_code, receipt, methods = self.run_helper("queue-fail")

        self.assertEqual(return_code, 2)
        self.assertEqual(receipt["status"], "unavailable")
        self.assertNotIn("thread/queue/start", methods)

    def test_queue_response_timeout_is_uncertain_without_start_request(self) -> None:
        return_code, receipt, methods = self.run_helper("uncertain")

        self.assertEqual(return_code, 3)
        self.assertEqual(receipt["status"], "uncertain")
        self.assertEqual(methods, ["initialize", "initialized", "thread/queue/add"])


if __name__ == "__main__":
    unittest.main()
