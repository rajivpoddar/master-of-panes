"""Session-age PM clear routes through MoP's pm_save_handoff flow.

Rajiv, DM D0AMF0XE6TS thread 1793731816.594829, ts 1793732417.459339:
"add the obligation to save memory and then automatically inject clear at next stop".
"""
from __future__ import annotations

import importlib.util
import io
import json
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "shared-assets" / "claude" / "scripts" / "heartbeat-session-age-clear.py"


def load_module():
    spec = importlib.util.spec_from_file_location("heartbeat_session_age_clear_handoff", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HeartbeatPmHandoffClearTest(unittest.TestCase):
    def test_due_pm_asks_mop_for_the_save_handoff_clear(self):
        module = load_module()
        seen = {}

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=0):
            seen["url"] = request.full_url
            seen["method"] = request.get_method()
            seen["body"] = json.loads(request.data.decode())
            return Response(json.dumps({"results": [{"slot": 0, "status": "deferred (pm_save_handoff obligation opened)"}]}).encode())

        module.urllib.request.urlopen = fake_urlopen
        status, error = module._request_mop_handoff_clear("sess-1")
        self.assertIsNone(error)
        self.assertIn("pm_save_handoff", status)
        self.assertTrue(seen["url"].endswith("/slots/0/clear"))
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["body"], {"source": "session_age_policy", "session_id": "sess-1"})

    def test_mop_unreachable_fails_open(self):
        module = load_module()

        def boom(*args, **kwargs):
            raise OSError("connection refused")

        module.urllib.request.urlopen = boom
        status, error = module._request_mop_handoff_clear("sess-1")
        self.assertIsNone(status)
        self.assertTrue(error.startswith("mop_unreachable"))

    def test_no_pm_self_clear_obligation_is_produced(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn('"--kind", "pm-self-clear"', text)
        self.assertNotIn("UserPromptSubmit", text)


if __name__ == "__main__":
    unittest.main()
