from __future__ import annotations

import hashlib
import http.server
import json
import os
from pathlib import Path
import subprocess
import stat
import threading
import unittest


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts/pm/shared-assets/claude/scripts/message-pm.sh"
ASSET_ROOT = ROOT / "scripts/pm/shared-assets"
SKILL_ROOT = ASSET_ROOT / "claude/skills"


class FakeMoP(http.server.BaseHTTPRequestHandler):
    mode = "success"
    requests: list[tuple[str, dict[str, object]]] = []

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        size = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(size)
        payload = json.loads(body.decode("utf-8"))
        self.requests.append((self.path, payload))
        if self.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/redirected")
            self.end_headers()
            return
        if self.mode == "invalid":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"not-json")
            return
        if self.mode == "error":
            self.send_response(503)
            self.end_headers()
            return
        command = payload["command"]
        response = {
            "success": True,
            "slot": 0,
            "verified": True,
            "bytes": len(command.encode("utf-8")),
        }
        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args: object) -> None:
        return


class PMCommunicationRestoreTests(unittest.TestCase):
    def run_sender(self, base_url: str, message: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "MOP_BASE_URL": base_url}
        return subprocess.run(
            [str(SCRIPT), "--", message],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
        )

    def server(self, mode: str) -> http.server.ThreadingHTTPServer:
        FakeMoP.mode = mode
        FakeMoP.requests = []
        return http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeMoP)

    def test_message_sender_posts_one_exact_literal_and_accepts_only_verified_receipt(self) -> None:
        server = self.server("success")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            message = "PM status: café\nready for review"
            result = self.run_sender(f"http://127.0.0.1:{server.server_port}", message)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(FakeMoP.requests), 1)
            path, payload = FakeMoP.requests[0]
            self.assertEqual(path, "/slots/0/send")
            self.assertEqual(payload, {"command": message, "force": True, "source": "message-pm"})
            self.assertIn("bytes=", result.stdout)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_sender_does_not_retry_error_invalid_receipt_or_redirect(self) -> None:
        for mode in ("error", "invalid", "redirect"):
            server = self.server(mode)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                result = self.run_sender(f"http://127.0.0.1:{server.server_port}", "one attempt")
                self.assertNotEqual(result.returncode, 0, mode)
                self.assertEqual(len(FakeMoP.requests), 1, mode)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_sender_rejects_non_loopback_forms_before_network(self) -> None:
        for base in (
            "http://localhost:3100",
            "http://user:pass@127.0.0.1:3100",
            "http://127.0.0.1:3100/path",
            "http://127.0.0.2:3100",
            "https://127.0.0.1:3100",
        ):
            result = self.run_sender(base, "no network")
            self.assertNotEqual(result.returncode, 0, base)

    def test_skill_contracts_are_short_and_use_existing_boundaries(self) -> None:
        message = (SKILL_ROOT / "message-pm/SKILL.md").read_text(encoding="utf-8")
        wait = (SKILL_ROOT / "pm-wait-nudge/SKILL.md").read_text(encoding="utf-8")
        processing = (SKILL_ROOT / "pm-nudge-processing/SKILL.md").read_text(encoding="utf-8")
        direct_assign = (SKILL_ROOT / "direct-assign/SKILL.md").read_text(encoding="utf-8")
        direct_release = (SKILL_ROOT / "direct-release/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("message-pm.sh", message)
        self.assertIn("exactly once", message)
        self.assertIn("LOCAL_CONTINUE", wait)
        self.assertIn("PM_WAIT", wait)
        self.assertIn("message-pm", wait)
        self.assertIn("PM_WAIT_NUDGE_RESULT classification=PM_WAIT", wait)
        self.assertIn("release_required=true", wait)
        self.assertIn("action=RELEASE_REQUIRED", wait)
        self.assertIn("PM_WAIT_NUDGE_RESULT classification=LOCAL_CONTINUE", wait)
        self.assertIn("exactly once", wait)
        self.assertIn("pm-nudge-processing", wait)
        self.assertIn("POST http://127.0.0.1:<MOP_PORT>/slots/{slot}/release", processing)
        self.assertIn("PM_RELEASE_BLOCKED reason=current_state_mismatch", processing)
        self.assertIn("occupied=false", processing)
        self.assertIn("exactly one `POST http://127.0.0.1:<MOP_PORT>/slots/{slot}/assign`", processing)
        self.assertIn("PM_ASSIGNMENT_BLOCKED reason=current_state_mismatch", processing)
        self.assertIn("exactly one `POST http://127.0.0.1:<MOP_PORT>/slots/{slot}/assign`", direct_assign)
        self.assertIn("exactly one empty `POST http://127.0.0.1:<MOP_PORT>/slots/{slot}/release`", direct_release)
        for ceremony in ("pm-transition-v1", "expected_epoch", "effect_id", "request_digest"):
            self.assertNotIn(ceremony, processing)
            self.assertNotIn(ceremony, direct_assign)
            self.assertNotIn(ceremony, direct_release)
        for skill in (wait, processing):
            self.assertNotIn("/pm/nudge/assign", skill)
            self.assertNotIn("pm-transition.sh", skill)
            self.assertNotIn("PM Operator", skill)

    def test_manifest_has_restored_pm_and_direct_lifecycle_assets_and_preserves_protected_scope(self) -> None:
        manifest = json.loads((ROOT / "scripts/pm/shared-assets/manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["inventory"]["selected_count"], 70)
        self.assertEqual(manifest["entries"], sorted(manifest["entries"], key=lambda item: item["source_path"]))
        self.assertEqual(len(manifest["entries"]), 70)
        self.assertEqual(len({entry["canonical_target"] for entry in manifest["entries"]}), 70)
        for entry in manifest["entries"]:
            source = ASSET_ROOT / entry["source_path"]
            self.assertTrue(source.is_file(), entry["source_path"])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), entry["sha256"], entry["source_path"])
            self.assertEqual(stat.S_IMODE(source.stat().st_mode), entry["mode"], entry["source_path"])
        expected = {
            "claude/skills/direct-assign/SKILL.md": ("/Users/rajiv/.claude/skills/direct-assign/SKILL.md", 420),
            "claude/skills/direct-release/SKILL.md": ("/Users/rajiv/.claude/skills/direct-release/SKILL.md", 420),
            "claude/scripts/message-pm.sh": ("/Users/rajiv/.claude/scripts/message-pm.sh", 493),
            "claude/skills/message-pm/SKILL.md": ("/Users/rajiv/.claude/skills/message-pm/SKILL.md", 420),
            "claude/skills/pm-nudge-processing/SKILL.md": ("/Users/rajiv/.claude/skills/pm-nudge-processing/SKILL.md", 420),
            "claude/skills/pm-wait-nudge/SKILL.md": ("/Users/rajiv/.claude/skills/pm-wait-nudge/SKILL.md", 420),
        }
        entries = {entry["source_path"]: entry for entry in manifest["entries"]}
        for source, (target, mode) in expected.items():
            self.assertEqual(entries[source]["canonical_target"], target)
            self.assertEqual(entries[source]["mode"], mode)
            self.assertEqual(
                entries[source]["sha256"],
                hashlib.sha256((ASSET_ROOT / source).read_bytes()).hexdigest(),
            )
        protected = {
            "/Users/rajiv/.claude/skills/pm-code-review/SKILL.md",
            "/Users/rajiv/.codex/skills/heydonna-cto-label-gated-ci/SKILL.md",
            "/Users/rajiv/.codex/skills/heydonna-open-pr-status/SKILL.md",
        }
        self.assertTrue(protected.isdisjoint({entries[source]["canonical_target"] for source in expected}))


if __name__ == "__main__":
    unittest.main()
