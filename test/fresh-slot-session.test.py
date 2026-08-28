#!/usr/bin/env python3
"""Regression tests for fresh numbered-slot Claude session construction."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts/pm/shared-assets/claude/scripts/launch-dev-slot-claude.sh"


class FreshSlotSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "claude-argv.log"
        self.uuid_count = self.root / "uuid-count"
        (self.bin / "uuidgen").write_text(
            "#!/bin/sh\n"
            "n=0; [ -f \"$UUID_COUNT\" ] && n=$(cat \"$UUID_COUNT\")\n"
            "n=$((n+1)); printf '%s' \"$n\" > \"$UUID_COUNT\"\n"
            "case \"$n\" in\n"
            "  1) echo 11111111-1111-4111-8111-111111111111 ;;\n"
            "  2) echo 22222222-2222-4222-8222-222222222222 ;;\n"
            "  *) echo 33333333-3333-4333-8333-333333333333 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        (self.bin / "claude").write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$@\" >> \"$CLAUDE_ARGV_LOG\"\n",
            encoding="utf-8",
        )
        self.sync = self.root / "sync.sh"
        self.sync.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        for path in (self.bin / "uuidgen", self.bin / "claude", self.sync):
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.key = self.root / "spark-key"
        self.key.write_text("test-key\n", encoding="utf-8")
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
            "UUID_COUNT": str(self.uuid_count),
            "CLAUDE_ARGV_LOG": str(self.log),
            "CLAUDE_SLOT_BIN": str(self.bin / "claude"),
            "CLAUDE_SLOT_SKILL_SYNC": str(self.sync),
            "DEV_SLOT_SPARK_API_KEY_FILE": str(self.key),
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_launcher(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(LAUNCHER), "5", *args],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_fresh_launches_get_distinct_ids_without_resume(self) -> None:
        first = self.run_launcher()
        second = self.run_launcher()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        argv = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(argv, [
            "--model", "ornith-1.5-35b-a3b", "--effort", "low",
            "--permission-mode", "bypassPermissions", "--session-id",
            "11111111-1111-4111-8111-111111111111",
            "--model", "ornith-1.5-35b-a3b", "--effort", "low",
            "--permission-mode", "bypassPermissions", "--session-id",
            "22222222-2222-4222-8222-222222222222",
        ])

    def test_explicit_resume_preserves_args_independent_of_order(self) -> None:
        for args in (("--continue", "--session-id", "11111111-1111-4111-8111-111111111111"),
                     ("--session-id", "22222222-2222-4222-8222-222222222222", "--continue")):
            result = self.run_launcher(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log.read_text(encoding="utf-8").splitlines(), [
            "--model", "ornith-1.5-35b-a3b", "--effort", "low",
            "--permission-mode", "bypassPermissions", "--continue",
            "--session-id", "11111111-1111-4111-8111-111111111111",
            "--model", "ornith-1.5-35b-a3b", "--effort", "low",
            "--permission-mode", "bypassPermissions", "--session-id",
            "22222222-2222-4222-8222-222222222222", "--continue",
        ])

    def test_fresh_launch_rejects_fixed_session_id(self) -> None:
        result = self.run_launcher("--session-id", "11111111-1111-4111-8111-111111111111")
        self.assertEqual(result.returncode, 78)
        self.assertIn("caller-supplied session ID", result.stderr)
        self.assertFalse(self.log.exists())

    def test_fresh_launch_rejects_equals_form_fixed_session_id(self) -> None:
        result = self.run_launcher("--session-id=11111111-1111-4111-8111-111111111111")
        self.assertEqual(result.returncode, 78)
        self.assertIn("caller-supplied session ID", result.stderr)
        self.assertFalse(self.log.exists())

    def test_fresh_launch_fails_closed_when_uuid_is_invalid(self) -> None:
        (self.bin / "uuidgen").write_text("#!/bin/sh\necho not-a-uuid\n", encoding="utf-8")
        (self.bin / "uuidgen").chmod((self.bin / "uuidgen").stat().st_mode | stat.S_IXUSR)
        result = self.run_launcher()
        self.assertEqual(result.returncode, 70)
        self.assertIn("session identity could not be created", result.stderr)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
