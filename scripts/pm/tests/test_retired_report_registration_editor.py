import hashlib
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
HELPER = ROOT / "scripts/pm/shared-assets/claude/scripts/pm/remove-deprecated-codex-pm-report-hook.py"
DEPRECATED = (
    "bash /Users/rajiv/.claude/hooks/pretooluse-reason-wrapper.sh "
    "/Users/rajiv/.claude/hooks/block-unverified-codex-pm-report.py"
)


class RetiredReportRegistrationEditorTest(unittest.TestCase):
    def run_editor(self, path, expected, *extra):
        return subprocess.run(
            [sys.executable, str(HELPER), "--settings", str(path), "--expected-sha256", expected, *extra],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_exact_registrations_removed_and_other_values_preserved(self):
        fixture = {
            "hooks": {
                "PreToolUse": [
                    {"hooks": [{"type": "command", "command": DEPRECATED}, {"type": "command", "command": "keep-a"}]},
                    {"hooks": [{"type": "command", "command": DEPRECATED + " --extra"}]},
                ],
                "mcp__plugin_master-of-panes_mop__mop_send_to_slot": [
                    {"hooks": [{"type": "command", "command": DEPRECATED}]}
                ],
            },
            "unrelated": {"credential_placeholder": "opaque", "number": 7},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "settings.json"
            path.write_text(json.dumps(fixture, indent=2) + "\n")
            os.chmod(path, 0o640)
            before = path.read_bytes()
            result = self.run_editor(
                path,
                hashlib.sha256(before).hexdigest(),
                "--expected-mode",
                "640",
                "--expected-uid",
                str(os.stat(path).st_uid),
                "--expected-gid",
                str(os.stat(path).st_gid),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            outcome = json.loads(result.stdout)
            self.assertEqual(outcome, {"status": "CHANGED", "reason": "exact_registrations_removed", "removed": 2})
            after = json.loads(path.read_text())
            commands = [
                hook.get("command")
                for entries in after["hooks"].values()
                for entry in entries
                for hook in entry["hooks"]
            ]
            self.assertNotIn(DEPRECATED, commands)
            self.assertIn(DEPRECATED + " --extra", commands)
            self.assertEqual(after["unrelated"], fixture["unrelated"])
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)

    def test_preimage_or_exact_count_drift_refuses_without_write(self):
        fixture = {"hooks": [{"command": DEPRECATED}]}
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "settings.json"
            path.write_text(json.dumps(fixture) + "\n")
            before = path.read_bytes()
            wrong_hash = self.run_editor(path, "0" * 64)
            self.assertNotEqual(wrong_hash.returncode, 0)
            self.assertEqual(path.read_bytes(), before)
            count_drift = self.run_editor(path, hashlib.sha256(before).hexdigest())
            self.assertNotEqual(count_drift.returncode, 0)
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
