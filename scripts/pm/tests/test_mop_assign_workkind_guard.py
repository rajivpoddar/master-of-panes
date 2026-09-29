from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).parents[3]
SCRIPT = REPO_ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "scripts" / "mop-assign-slot.py"
VALID = ("implementation", "rework", "repro", "review")


def load_module():
    spec = importlib.util.spec_from_file_location("mop_assign_slot_guard", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorkKindGuardTest(unittest.TestCase):
    def test_invalid_work_kind_refused_locally_with_zero_http(self):
        module = load_module()
        calls: list = []

        def boom(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("no HTTP must occur for a locally refused work-kind")

        module.http_json = boom  # type: ignore[attr-defined]
        code = module.main(["--slot", "2", "--issue", "7994", "--work-kind", "new_issue"])  # type: ignore[attr-defined]
        # main() returns the emit() exit code; capture stdout via emit is awkward,
        # so re-run through a patched emit to inspect the payload.
        seen: list = []
        module.emit = lambda payload, exit_code: (seen.append((payload, exit_code)), exit_code)[1]  # type: ignore[attr-defined]
        code = module.main(["--slot", "2", "--issue", "7994", "--work-kind", "new_issue"])  # type: ignore[attr-defined]
        self.assertEqual(code, 2)
        self.assertEqual(calls, [])
        payload, _ = seen[-1]
        self.assertEqual(payload["status"], "refused")
        self.assertIn("invalid_work_kind:new_issue", payload["reason"])
        for kind in VALID:
            self.assertIn(kind, payload["reason"])
        self.assertEqual(payload["slot_state_after"], "not_applied")
        self.assertEqual(payload["sanctioned_path"], "mop-assign-slot")

    def test_valid_work_kind_reaches_http(self):
        module = load_module()
        calls: list = []

        timeouts: dict = {}

        def fake_http(method, url, body=None, timeout=60):
            calls.append((method, url))
            timeouts[method] = timeout
            if method == "GET" and url.endswith("/assign-effect/bounds"):
                return 200, {"success": True, "max_clear_wait_ms": 900_000}
            if method == "GET":
                return 200, {"assignment_epoch": 5}
            return 400, {"status": "refused", "reason": "probe", "step_failed": "ownership"}

        module.http_json = fake_http  # type: ignore[attr-defined]
        module.emit = lambda payload, exit_code: exit_code  # type: ignore[attr-defined]
        module.main(["--slot", "2", "--issue", "7994", "--work-kind", "rework", "--handoff", "h1"])  # type: ignore[attr-defined]
        methods = [m for m, _ in calls]
        self.assertIn("GET", methods)
        self.assertIn("POST", methods)
        # The assign-effect POST outlives MoP's bounded idle + clear-ack waits
        # as reported by the server's bounds endpoint, plus a delivery margin.
        self.assertGreater(timeouts["POST"], 900)




class ClientTimeoutFallbackTest(unittest.TestCase):
    def test_timeout_falls_back_to_server_env_bounds(self):
        import os
        module = load_module()
        module.http_json = lambda method, url, body=None, timeout=60: (0, {"error": "down"})  # type: ignore[attr-defined]
        old = {k: os.environ.get(k) for k in ("MOP_ASSIGN_CLEAR_IDLE_TIMEOUT_MS", "MOP_ASSIGN_CLEAR_ACK_TIMEOUT_MS")}
        os.environ["MOP_ASSIGN_CLEAR_IDLE_TIMEOUT_MS"] = "600000"
        os.environ["MOP_ASSIGN_CLEAR_ACK_TIMEOUT_MS"] = "300000"
        try:
            timeout = module.assign_effect_timeout_seconds("http://x")  # type: ignore[attr-defined]
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertGreater(timeout, 600 + 2 * 300)


if __name__ == "__main__":
    unittest.main()
