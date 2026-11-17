"""Session-age PM self-clear is triggered by ONE HeyDonna Alerts message.

Rajiv, DM D0AMF0XE6TS thread 1793731816.594829, ts 1793732417.459339: "add the obligation to
save memory and then automatically inject clear at next stop" (MoP's save-handoff flow after
PM's own clear request is unchanged).
Superseded trigger, Rajiv DM D0AMF0XE6TS thread 1794921293.133799, ts 1794923900.734289:
"easiest trigger alerts message to dev immediately, PM self-clear same way, remove 4am restart".
The script no longer calls POST /slots/0/clear itself. All Slack and file effects are faked.

Alert send custody, CTO REVISE of da450c5 (C0ALZJHGE49 thread 1794923203.112819, ts 1794925523.128429): the
guarded send intent is persisted in the session/window state BEFORE every dispatch. An unconfirmed issued send
(timeout, lost receipt, a failed write afterwards) stays consumed/UNKNOWN under the session/window key and is
never resent automatically; only the next policy-due six-hour window can send again. If the intent cannot be
persisted the alert is skipped. Added, NOT executed (MoP changes need no local proof).
"""
from __future__ import annotations

import contextlib
import fcntl
import importlib.util
import io
import json
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

    def test_accepted_send_with_a_lost_receipt_stays_consumed_across_a_second_run_and_a_restart(self):
        module = load_module()
        posts: list[str] = []

        def lost_receipt(text):
            posts.append(text)
            return False  # Slack accepted the message; the receipt never arrived

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "alert.json"
            first = module._alert_pm_self_clear("sess-1", 7 * 3600, NOW, post=lost_receipt, state_path=state)
            record = json.loads(state.read_text())
            restarted = load_module()  # a restart keeps only the state file
            again = restarted._alert_pm_self_clear(
                "sess-1", 7.5 * 3600, NOW + timedelta(minutes=30), post=lost_receipt, state_path=state)
            last_second = restarted._alert_pm_self_clear(
                "sess-1", 12.9 * 3600, NOW + timedelta(hours=6) - timedelta(seconds=1),
                post=lost_receipt, state_path=state)
            self.assertEqual(len(posts), 1, "an unconfirmed issued send is never resent inside the window")
            due = restarted._alert_pm_self_clear(
                "sess-1", 13 * 3600, NOW + timedelta(hours=6), post=lost_receipt, state_path=state)
        self.assertEqual(first, ("alert_send_unconfirmed", None))
        self.assertEqual(record["send"], "issued", "UNKNOWN stays consumed; it is never recorded as unsent")
        self.assertEqual(record["session_id"], "sess-1")
        self.assertEqual(again, ("alert_send_unconfirmed", None))
        self.assertEqual(last_second, ("alert_send_unconfirmed", None))
        self.assertEqual(due, ("alert_send_unconfirmed", None), "the next policy-due window is a separate reminder")
        self.assertEqual(len(posts), 2)

    def test_raising_sender_is_unconfirmed_and_consumed(self):
        module = load_module()
        calls: list[int] = []

        def boom(_text):
            calls.append(1)
            raise TimeoutError("sender timed out after dispatch")

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "alert.json"
            first = module._alert_pm_self_clear("sess-1", 7 * 3600, NOW, post=boom, state_path=state)
            again = module._alert_pm_self_clear(
                "sess-1", 8 * 3600, NOW + timedelta(hours=1), post=boom, state_path=state)
            self.assertEqual(json.loads(state.read_text())["send"], "issued")
        self.assertEqual(first, ("alert_send_unconfirmed", None))
        self.assertEqual(again, ("alert_send_unconfirmed", None))
        self.assertEqual(len(calls), 1)

    def test_intent_is_on_disk_before_the_dispatch(self):
        module = load_module()
        seen: list[dict] = []
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "alert.json"

            def post(_text):
                seen.append(json.loads(state.read_text()))
                return True

            status = module._alert_pm_self_clear("sess-1", 7 * 3600, NOW, post=post, state_path=state)
            confirmed = json.loads(state.read_text())
        self.assertEqual(status, ("alert_posted", None))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["send"], "issued")
        self.assertEqual(seen[0]["session_id"], "sess-1")
        self.assertEqual(seen[0]["sent_epoch"], NOW.timestamp())
        self.assertTrue(seen[0]["attempt"])
        self.assertEqual(confirmed["send"], "posted")
        self.assertEqual(confirmed["attempt"], seen[0]["attempt"])

    def test_confirmation_write_failure_after_an_ok_receipt_does_not_repeat(self):
        module = load_module()
        posts: list[str] = []
        real_write = module._write_alert_state

        def write_then_fail(path, record):
            if posts:
                raise OSError("disk full")
            real_write(path, record)

        module._write_alert_state = write_then_fail
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "alert.json"
            first = module._alert_pm_self_clear(
                "sess-1", 7 * 3600, NOW, post=lambda t: posts.append(t) or True, state_path=state)
            persisted = json.loads(state.read_text())
            again = module._alert_pm_self_clear(
                "sess-1", 8 * 3600, NOW + timedelta(hours=1), post=lambda t: posts.append(t) or True, state_path=state)
        self.assertEqual(first, ("alert_posted", None))
        self.assertEqual(persisted["send"], "issued", "the persisted intent still holds the window")
        self.assertEqual(again, ("alert_send_unconfirmed", None))
        self.assertEqual(len(posts), 1)

    def test_unpersistable_intent_skips_the_alert_and_reports_it(self):
        module = load_module()
        posts: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            blocker = Path(directory) / "not-a-directory"
            blocker.write_text("x")
            under_a_file = module._alert_pm_self_clear(
                "sess-1", 7 * 3600, NOW, post=lambda t: posts.append(t) or True, state_path=blocker / "alert.json")
            unreadable = Path(directory) / "alert.json"
            unreadable.mkdir()  # exists but cannot be read as a file: the window cannot be proven
            unknown = module._alert_pm_self_clear(
                "sess-1", 7 * 3600, NOW, post=lambda t: posts.append(t) or True, state_path=unreadable)
        self.assertEqual(under_a_file, (None, "alert_intent_not_persisted"))
        self.assertEqual(unknown, (None, "alert_intent_not_persisted"))
        self.assertEqual(posts, [], "no intent on disk means nothing is dispatched")

    def test_a_run_that_cannot_claim_the_window_dispatches_nothing(self):
        module = load_module()
        posts: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "alert.json"
            with state.with_name("alert.json.lock").open("a") as held:
                fcntl.flock(held, fcntl.LOCK_EX)  # another run is claiming the same window
                status = module._alert_pm_self_clear(
                    "sess-1", 7 * 3600, NOW, post=lambda t: posts.append(t) or True, state_path=state)
        self.assertEqual(status, (None, "alert_intent_not_persisted"))
        self.assertEqual(posts, [])
        self.assertFalse(state.exists())

    def test_record_written_before_the_intent_field_existed_counts_as_posted(self):
        module = load_module()
        posts: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "alert.json"
            state.write_text(json.dumps({"session_id": "sess-1", "sent_epoch": NOW.timestamp()}))
            status = module._alert_pm_self_clear(
                "sess-1", 8 * 3600, NOW + timedelta(hours=1), post=lambda t: posts.append(t) or True, state_path=state)
        self.assertEqual(status, ("alert_already_sent", None))
        self.assertEqual(posts, [])

    def test_main_reports_an_unconfirmed_send_without_blocking_and_an_unpersistable_intent_as_blocked(self):
        module = load_module()
        posts: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pm_dir = root / "pm-sessions"
            pm_dir.mkdir()
            started = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
            (pm_dir / "session-1.jsonl").write_text(json.dumps({"timestamp": started}) + "\n")
            module._mop_slots = lambda: []
            module._pane_dir = lambda pane: pm_dir if pane == "pm" else root / f"absent-{pane}"
            module._mcp_children_running = lambda: 0
            module.LATEST = root / "latest.json"
            module.ALERT_STATE = root / "alert.json"
            module._post_alert = lambda text: posts.append(text) or False  # accepted, receipt lost

            def run_main() -> tuple[int, dict]:
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    code = module.main()
                return code, json.loads(buffer.getvalue())

            code, out = run_main()
            self.assertEqual(code, 0, "an UNKNOWN send is reported, not a blocked run")
            self.assertEqual(out["results"]["pm"]["status"], "pending_self_clear_alert")
            self.assertEqual(out["results"]["pm"]["alert_status"], "alert_send_unconfirmed")
            code, out = run_main()
            self.assertEqual(out["results"]["pm"]["alert_status"], "alert_send_unconfirmed")
            self.assertEqual(len(posts), 1, "the second run must not dispatch again")

            blocker = root / "not-a-directory"
            blocker.write_text("x")
            module.ALERT_STATE = blocker / "alert.json"
            code, out = run_main()
            self.assertEqual(code, 20)
            self.assertEqual(out["results"]["pm"]["status"], "blocked")
            self.assertEqual(out["read_errors"]["pm"], "alert_intent_not_persisted")
            self.assertEqual(len(posts), 1)

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
