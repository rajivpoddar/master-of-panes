#!/usr/bin/env python3
"""proxy-review-cap.py `reserve` is refused unless a launch path calls it (phantom round on #15208, 28 Nov)."""

from __future__ import annotations

import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).parents[3]
SCRIPTS = ROOT / "scripts/pm/shared-assets/claude/scripts"
HELPER = SCRIPTS / "proxy-review-cap.py"
LAUNCHER = SCRIPTS / "proxy-agent.sh"
CALLER_ENV = "PROXY_REVIEW_CAP_CALLER"
INPROCESS_ENV = "PROXY_REVIEW_CAP_INPROCESS"


class ReserveGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name) / "home"
        (self.home / ".claude/state").mkdir(parents=True)
        self.bin = pathlib.Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        self.comments = pathlib.Path(self.tmp.name) / "comments.json"
        self.set_comments([[]])
        gh = self.bin / "gh"
        gh.write_text(f"#!/bin/sh\nexec /bin/cat '{self.comments}'\n")
        gh.chmod(0o755)
        self.brief = pathlib.Path(self.tmp.name) / "brief"
        self.brief.write_text("PR: 321\n")
        self.db = self.home / ".claude/state/proxy-review-cap.sqlite3"

    def set_comments(self, pages):
        self.comments.write_text(json.dumps(pages))

    def run_helper(self, mode, **extra):
        env = {k: v for k, v in os.environ.items() if k not in (CALLER_ENV, INPROCESS_ENV)}
        env.update(HOME=str(self.home), PATH=f"{self.bin}:{env.get('PATH', '')}", **extra)
        return subprocess.run([sys.executable, "-I", str(HELPER), mode, "pm-admission-reviewer", str(self.brief)],
                              env=env, capture_output=True, text=True, timeout=30)

    def reservations(self):
        if not self.db.exists():
            return []
        with sqlite3.connect(self.db) as conn:
            row = conn.execute("SELECT reservations FROM rounds WHERE pr=321 AND stage='12b'").fetchone()
        return json.loads(row[0]) if row else []

    def test_bare_reserve_is_refused_and_writes_nothing(self):
        result = self.run_helper("reserve")
        self.assertEqual(result.returncode, 76)
        self.assertIn("MANUAL_RESERVE_FOR_PROXY_LAUNCH", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.db.exists())

    def test_wrong_flag_values_are_refused(self):
        for extra in ({INPROCESS_ENV: "0"}, {INPROCESS_ENV: "true"}, {CALLER_ENV: "pm"}):
            result = self.run_helper("reserve", **extra)
            self.assertEqual(result.returncode, 76, extra)
            self.assertIn("MANUAL_RESERVE_FOR_PROXY_LAUNCH", result.stderr)
        self.assertEqual(self.reservations(), [])

    def test_proxy_agent_path_reserves(self):
        result = self.run_helper("reserve", **{CALLER_ENV: "proxy-agent"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("REVIEW_CAP_OK stage=12b pr=321 revise_rounds=0", result.stderr)
        self.assertEqual(self.reservations(), [result.stdout.strip()])

    def test_inprocess_path_reserves(self):
        result = self.run_helper("reserve", **{INPROCESS_ENV: "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout.strip(), r"^[0-9a-f]{32}$")
        self.assertEqual(self.reservations(), [result.stdout.strip()])

    def test_check_is_read_only_and_unchanged(self):
        result = self.run_helper("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("REVIEW_CAP_OK stage=12b pr=321 revise_rounds=0 receipts=0 reservations=0 round=1", result.stderr)
        self.assertFalse(self.db.exists())

    def test_cap_counting_unchanged(self):
        verdict = "PM 12b: REVISE at abcdef1\nbehavior: finding"
        self.set_comments([[{"id": 1, "body": verdict}, {"id": 2, "body": verdict}]])
        for mode, extra in (("check", {}), ("reserve", {CALLER_ENV: "proxy-agent"}), ("reserve", {INPROCESS_ENV: "1"})):
            result = self.run_helper(mode, **extra)
            self.assertEqual(result.returncode, 76, (mode, result.stderr))
            self.assertIn("REVIEW_CAP_2 stage=12b pr=321 revise_rounds=2", result.stderr)
        self.assertEqual(self.reservations(), [])

    def test_launcher_marks_its_own_helper_call(self):
        launcher = LAUNCHER.read_text(encoding="utf-8")
        self.assertRegex(launcher, re.compile(
            r'PROXY_REVIEW_CAP_CALLER=proxy-agent python3 -I "\$REVIEW_CAP_HELPER" "\$1" "\$TYPE" "\$ROLE_BRIEF"'))


if __name__ == "__main__":
    unittest.main()
