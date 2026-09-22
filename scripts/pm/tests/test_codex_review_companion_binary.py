#!/usr/bin/env python3
"""Focused proof for the managed codex-review-companion CODEX_BIN default.

Binds the managed source to the live config-compatibility failure: the obsolete
pinned NVM codex (0.144.2) cannot parse the unchanged ~/.codex/config.toml,
while the current canonical binary can.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).parents[3]
SRC = ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "skills" / "codex-review-companion" / "codex-review-companion.mjs"
MANIFEST = ROOT / "scripts" / "pm" / "shared-assets" / "manifest.json"
SOURCE_PATH = "claude/skills/codex-review-companion/codex-review-companion.mjs"
TARGET = "/Users/rajiv/.claude/skills/codex-review-companion/codex-review-companion.mjs"
CANONICAL = "/opt/homebrew/bin/codex"
OBSOLETE = "/Users/rajiv/.nvm/versions/node/v22.13.1/bin/codex"


def managed_default() -> str | None:
    match = re.search(r'process\.env\.CODEX_BIN\s*\|\|\s*"([^"]+)"', SRC.read_text(encoding="utf-8"))
    return match.group(1) if match else None


class ManagedDefaultTests(unittest.TestCase):
    def test_default_is_the_current_canonical_binary(self):
        self.assertEqual(managed_default(), CANONICAL)

    def test_obsolete_pinned_nvm_binary_is_gone_from_the_managed_source(self):
        self.assertNotIn(OBSOLETE, SRC.read_text(encoding="utf-8"))

    def test_explicit_override_is_preserved(self):
        source = SRC.read_text(encoding="utf-8")
        self.assertIn("process.env.CODEX_BIN", source)
        # the override must still win over the default, i.e. env-first form
        self.assertRegex(source, r'process\.env\.CODEX_BIN\s*\|\|')

    def test_manifest_row_targets_the_installed_companion_with_mode_493(self):
        entries = json.loads(MANIFEST.read_text(encoding="utf-8"))["entries"]
        rows = [e for e in entries if e["source_path"] == SOURCE_PATH]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["canonical_target"], TARGET)
        self.assertEqual(row["mode"], 493)
        self.assertEqual(row["dependency_status"], "closed")
        self.assertEqual(row["dependencies"], [])


class LiveConfigCompatibilityTests(unittest.TestCase):
    """RED/GREEN bound to the real, unchanged config.toml."""

    def test_obsolete_binary_cannot_parse_the_live_config(self):
        if not pathlib.Path(OBSOLETE).exists():
            self.skipTest("obsolete binary absent")
        result = subprocess.run([OBSOLETE, "login", "status"], capture_output=True, text=True, timeout=90)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Error loading configuration", result.stderr + result.stdout)

    def test_canonical_binary_parses_the_live_config(self):
        if not pathlib.Path(CANONICAL).exists():
            self.skipTest("canonical binary absent")
        result = subprocess.run([CANONICAL, "login", "status"], capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Logged in", result.stdout + result.stderr)
        # the config itself is not ours to change and must still be present
        self.assertTrue(pathlib.Path("/Users/rajiv/.codex/config.toml").is_file())


if __name__ == "__main__":
    unittest.main()
