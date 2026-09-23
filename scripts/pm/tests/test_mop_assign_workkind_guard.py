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

        def fake_http(method, url, body=None):
            calls.append((method, url))
            if method == "GET":
                return 200, {"assignment_epoch": 5}
            return 400, {"status": "refused", "reason": "probe", "step_failed": "ownership"}

        module.http_json = fake_http  # type: ignore[attr-defined]
        module.emit = lambda payload, exit_code: exit_code  # type: ignore[attr-defined]
        module.main(["--slot", "2", "--issue", "7994", "--work-kind", "rework", "--handoff", "h1"])  # type: ignore[attr-defined]
        methods = [m for m, _ in calls]
        self.assertIn("GET", methods)
        self.assertIn("POST", methods)


if __name__ == "__main__":
    unittest.main()
