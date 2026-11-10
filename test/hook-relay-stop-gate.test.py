"""Gated slot Stop: the plugin relay must not POST; the allowed relay must."""
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "hook-relay.sh")
CWD = "/Users/rajiv/Downloads/projects/heydonna-app-3001"


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(n) or b"{}")
        self.server.hits.append(body)
        raw = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        return


class HookRelayStopGateTest(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.hits = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def run_hook(self, hook, env_extra):
        env = os.environ.copy()
        env["MOP_HOOK_BASE"] = self.base
        env.pop("MOP_SLOT_STOP_ALLOWED", None)
        env.pop("MOP_STOP_MARK_ALLOWED", None)
        env.pop("HEYDONNA_SLOT_STOP_GATED", None)
        env.update(env_extra)
        return subprocess.run(
            ["bash", SCRIPT, hook],
            input=json.dumps({"cwd": CWD}).encode(),
            env=env,
            capture_output=True,
            check=False,
        )

    def test_gated_stop_skips_post(self):
        result = self.run_hook("Stop", {"HEYDONNA_SLOT_STOP_GATED": "1"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.server.hits, [])

    def test_ungated_stop_posts_without_allowed_mark(self):
        result = self.run_hook("Stop", {})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(self.server.hits), 1)
        self.assertEqual(self.server.hits[0]["hook_event_name"], "Stop")
        self.assertNotIn("mop_stop_allowed", self.server.hits[0])

    def test_user_prompt_submit_still_posts_when_stop_is_gated(self):
        result = self.run_hook("UserPromptSubmit", {"HEYDONNA_SLOT_STOP_GATED": "1"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(self.server.hits), 1)
        self.assertEqual(self.server.hits[0]["hook_event_name"], "UserPromptSubmit")

    def test_explicit_allowed_stop_posts_the_mark(self):
        result = self.run_hook("Stop", {"HEYDONNA_SLOT_STOP_GATED": "1", "MOP_SLOT_STOP_ALLOWED": "1"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(self.server.hits), 1)
        self.assertEqual(self.server.hits[0]["mop_stop_allowed"], 1)
        self.assertEqual(self.server.hits[0]["hook_event_name"], "Stop")


if __name__ == "__main__":
    unittest.main()
