"""Session-age PM self-clear is triggered by ONE HeyDonna Alerts message.

Rajiv, DM D0AMF0XE6TS thread 1793731816.594829, ts 1793732417.459339: "add the obligation to
save memory and then automatically inject clear at next stop" (MoP's save-handoff flow after
PM's own clear request is unchanged).
Superseded trigger, Rajiv DM D0AMF0XE6TS thread 1794921293.133799, ts 1794923900.734289:
"easiest trigger alerts message to dev immediately, PM self-clear same way, remove 4am restart".
The script no longer calls POST /slots/0/clear itself. All Slack and file effects are faked.
"""
from __future__ import annotations

import importlib.util
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "shared-assets" / "claude" / "scripts" / "heartbeat-session-age-clear.py"
NOW = datetime(2026, 11, 17, 14, 0, tzinfo=timezone.utc)


def load_module():
    spec = importlib.util.spec_from_file_location("heartbeat_session_age_clear_handoff", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HeartbeatPmSelfClearAlertTest(unittest.TestCase):
    def test_due_pm_posts_one_alert_once_per_session(self):
        module = load_module()
        posts: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "alert.json"
            first = module._alert_pm_self_clear("sess-1", 7 * 3600, NOW, post=lambda t: posts.append(t) or True, state_path=state)
            again = module._alert_pm_self_clear("sess-1", 7.5 * 3600, NOW + timedelta(hours=1),
                                                post=lambda t: posts.append(t) or True, state_path=state)
            later = module._alert_pm_self_clear("sess-1", 13 * 3600, NOW + timedelta(hours=6),
                                                post=lambda t: posts.append(t) or True, state_path=state)
            fresh = module._alert_pm_self_clear("sess-2", 7 * 3600, NOW + timedelta(hours=7),
                                                post=lambda t: posts.append(t) or True, state_path=state)
        self.assertEqual(first, ("alert_posted", None))
        self.assertEqual(again, ("alert_already_sent", None))
        self.assertEqual(later, ("alert_posted", None), "re-sent no sooner than six hours later")
        self.assertEqual(fresh, ("alert_posted", None), "a new PM session is a new key")
        self.assertEqual(len(posts), 3)

    def test_alert_text_asks_for_housekeeping_then_self_clear_without_gating_or_backticks(self):
        module = load_module()
        text = module._alert_text(7 * 3600)
        for needle in ("7.0h old", "housekeeping", "self-clear", "resume prompt", "continuation note",
                       "MEMORY-session-handoff-current.md", "mop-clear-slot.sh pm", "Nothing is blocked meanwhile",
                       "do not request another"):
            self.assertIn(needle, text)
        self.assertNotIn("`", text)

    def test_failed_post_is_reported_and_retried_next_run(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "alert.json"
            status, error = module._alert_pm_self_clear("sess-1", 7 * 3600, NOW, post=lambda _t: False, state_path=state)
            self.assertIsNone(status)
            self.assertEqual(error, "alert_post_failed")
            self.assertFalse(state.exists(), "state is written only after an OK receipt")

            def boom(_text):
                raise RuntimeError("slack down")

            status, error = module._alert_pm_self_clear("sess-1", 7 * 3600, NOW, post=boom, state_path=state)
            self.assertIsNone(status)
            self.assertTrue(error.startswith("alert_post_failed:"))
            self.assertFalse(state.exists())

    def test_post_alert_posts_as_the_alerts_bot_with_the_body_on_stdin(self):
        module = load_module()
        seen = {}

        def fake_run(args, **kwargs):
            seen.update(args=args, stdin=kwargs["input"], env=kwargs["env"])
            return types.SimpleNamespace(returncode=0, stdout="OK ts=1794924000.100000 channel=C0ALZJHGE49\n")

        module.subprocess.run = fake_run
        self.assertTrue(module._post_alert("hello"))
        self.assertEqual(seen["args"][-3:], ["-c", "C0ALZJHGE49", "-f"])
        self.assertEqual(seen["stdin"], "hello")
        self.assertEqual(seen["env"]["SLACK_SEND_AS"], "alerts")

    def test_script_no_longer_asks_mop_for_the_clear_and_stays_inside_rule_50(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("_request_mop_handoff_clear", text)
        self.assertNotIn("/slots/0/clear", text.split('"""', 2)[2], "no POST /slots/0/clear call remains in code")
        self.assertNotIn('"--kind", "pm-self-clear"', text)
        self.assertNotIn("UserPromptSubmit", text)


if __name__ == "__main__":
    unittest.main()
