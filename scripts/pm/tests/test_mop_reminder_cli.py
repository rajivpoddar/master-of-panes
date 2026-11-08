from __future__ import annotations

import argparse
import importlib.util
import io
import json
import subprocess
import sys
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).parents[3]
CLI = REPO_ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "scripts" / "mop.py"

_spec = importlib.util.spec_from_file_location("mop_cli_under_test", CLI)
mop = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(mop)

NOW = datetime(2026, 11, 8, 10, 0, 0, tzinfo=timezone.utc)


def add_args(**overrides) -> argparse.Namespace:
    values = dict(
        key="k1", thread="1793953549.727149", text="check", every="30m", until="+12h",
        channel=None, stop_obligation=None, first_at=None, replace=False,
        created_by="cli", source_ref=None,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


class ReminderParsingTest(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(mop.parse_duration_s("10m"), 600)
        self.assertEqual(mop.parse_duration_s("1h"), 3600)
        self.assertEqual(mop.parse_duration_s("2d"), 172800)
        with self.assertRaises(ValueError):
            mop.parse_duration_s("soon")

    def test_relative_and_absolute_times(self):
        text, relative = mop.parse_when("+11m", NOW)
        self.assertTrue(relative)
        self.assertEqual(datetime.fromisoformat(text), NOW + timedelta(minutes=11))
        text, relative = mop.parse_when("2026-11-09T10:00:00+05:30", NOW)
        self.assertFalse(relative)
        self.assertEqual(datetime.fromisoformat(text), datetime(2026, 11, 9, 4, 30, tzinfo=timezone.utc))
        text, _ = mop.parse_when("2026-11-09T04:30:00Z", NOW)
        self.assertEqual(datetime.fromisoformat(text), datetime(2026, 11, 9, 4, 30, tzinfo=timezone.utc))
        with self.assertRaises(ValueError):
            mop.parse_when("tomorrow", NOW)

    def test_add_body_shape(self):
        body = mop.build_reminder_add_body(
            add_args(stop_obligation=42, channel="C0C7U35LBKR", replace=True, first_at="+5m", source_ref="dm"),
            NOW,
        )
        self.assertEqual(body["interval_s"], 1800)
        self.assertEqual(body["thread_ts"], "1793953549.727149")
        self.assertTrue(body["until_relative"])
        self.assertEqual(body["stop_obligation_id"], 42)
        self.assertEqual(body["channel"], "C0C7U35LBKR")
        self.assertTrue(body["replace"])
        self.assertEqual(datetime.fromisoformat(body["first_at"]), NOW + timedelta(minutes=5))
        self.assertEqual(body["source_ref"], "dm")

    def test_add_body_omits_unset_optionals(self):
        body = mop.build_reminder_add_body(add_args(), NOW)
        for key in ("channel", "stop_obligation_id", "first_at", "source_ref"):
            self.assertNotIn(key, body)

    def test_interval_floor_is_enforced_before_any_request(self):
        with self.assertRaises(ValueError):
            mop.build_reminder_add_body(add_args(every="9m"), NOW)


class _Handler(BaseHTTPRequestHandler):
    seen: list = []

    def _reply(self, payload):
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        _Handler.seen.append(("POST", self.path, body))
        self._reply({"success": True, "outcome": "created"})

    def do_GET(self):  # noqa: N802
        _Handler.seen.append(("GET", self.path, None))
        self._reply({"success": True, "reminders": []})

    def log_message(self, *args):  # silence
        return


class ReminderRoutingTest(unittest.TestCase):
    def setUp(self):
        _Handler.seen = []
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.env = {"MOP_PORT": str(self.server.server_address[1])}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def run_cli(self, *argv):
        import os

        old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                code = mop.main(list(argv))
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return code, out.getvalue()

    def test_add_posts_to_alert_reminders(self):
        code, _ = self.run_cli(
            "reminder", "add", "--key", "smoke-1108", "--channel", "C0ALZJHGE49",
            "--thread", "1793953549.727149", "--every", "10m", "--until", "+11m",
            "--text", "MoP recurring reminder smoke test (posts once)",
        )
        self.assertEqual(code, 0)
        method, path, body = _Handler.seen[0]
        self.assertEqual((method, path), ("POST", "/alert-reminders"))
        self.assertEqual(body["key"], "smoke-1108")
        self.assertEqual(body["interval_s"], 600)
        self.assertTrue(body["until_relative"])

    def test_list_and_cancel_routes(self):
        self.assertEqual(self.run_cli("reminder", "list", "--status", "all")[0], 0)
        self.assertEqual(self.run_cli("reminder", "cancel", "--key", "smoke-1108")[0], 0)
        self.assertEqual(_Handler.seen[0][:2], ("GET", "/alert-reminders?status=all"))
        self.assertEqual(_Handler.seen[1][:2], ("POST", "/alert-reminders/smoke-1108/cancel"))

    def test_bad_interval_exits_2_without_calling_mop(self):
        code, out = self.run_cli(
            "reminder", "add", "--key", "k", "--thread", "1.2", "--every", "5m", "--until", "+1h", "--text", "t",
        )
        self.assertEqual(code, 2)
        self.assertIn("at least 10m", out)
        self.assertEqual(_Handler.seen, [])

    def test_help_lists_reminder(self):
        proc = subprocess.run([sys.executable, str(CLI), "reminder", "--help"], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0)
        for sub in ("add", "list", "cancel"):
            self.assertIn(sub, proc.stdout)


if __name__ == "__main__":
    unittest.main()
