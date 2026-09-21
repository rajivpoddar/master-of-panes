#!/usr/bin/env python3
"""Contract tests for the heartbeat-tasks dev-slot clear removal.

The 3h heartbeat must never clear a numbered slot. Dev-slot clearing belongs to
the new-issue assignment boundary (the atomic `mop-assign-slot` operation), and
only the PM session keeps a cadence clear path.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets"
SKILL_PATH = SHARED / "claude" / "skills" / "heartbeat-tasks" / "SKILL.md"
MANIFEST_PATH = SHARED / "manifest.json"
RESOLVER_PATH = SHARED / "claude" / "scripts" / "pm" / "control-plane" / "sakshi-heartbeat.py"

TARGET = "/Users/rajiv/Downloads/projects/heydonna-app/.claude/skills/heartbeat-tasks/SKILL.md"


class HeartbeatTasksDevSlotClearRemovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = SKILL_PATH.read_text(encoding="utf-8")
        self.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def test_dev_slot_clearing_instructions_are_gone(self) -> None:
        for banned in (
            "--require-terminal",
            "dev-slots-first",
            "idle/free dev slot \u2192 heartbeat clears",
            "creates/updates\n  `SESSION_AGE_CLEAR_PENDING slot:N`",
        ):
            self.assertNotIn(banned, self.skill, f"dev-slot clearing text survived: {banned!r}")
        self.assertNotIn("idle-slot clears", self.skill)

    def test_no_instruction_produces_a_dev_slot_pending_row(self) -> None:
        # The token may appear only inside an explicit prohibition; never as an
        # instruction to create or act on a per-slot pending row.
        for match in re.finditer(r".{0,120}SESSION_AGE_CLEAR_PENDING slot:N.{0,60}", self.skill, re.DOTALL):
            window = match.group(0).lower()
            self.assertTrue(
                ("must not" in window) or ("do not" in window) or ("not be" in window) or ("exists for a dev slot" in window),
                f"non-prohibitive dev-slot pending-row text: {match.group(0)!r}",
            )

    def test_dev_slots_are_cleared_only_at_the_assignment_boundary(self) -> None:
        self.assertIn("new-issue assignment boundary", self.skill)
        self.assertIn("mop-assign-slot", self.skill)
        self.assertIn("NEVER cleared on a cadence", self.skill)

    def test_pm_self_clear_path_is_preserved(self) -> None:
        self.assertIn("SESSION_AGE_CLEAR_PENDING PM", self.skill)
        self.assertIn("mop-clear-slot.sh pm", self.skill)
        self.assertIn("pm-self-clear-stop", self.skill)
        self.assertIn("Due PM becomes a PM pending-clear row", self.skill)

    def test_three_hour_cadence_still_invokes_the_skill(self) -> None:
        cadence = (ROOT / "src" / "pmCadence.ts").read_text(encoding="utf-8")
        self.assertIn("Invoke Skill(heartbeat-tasks)", cadence)

    def test_canonical_source_is_manifest_mapped_to_the_resolver_target(self) -> None:
        rows = [r for r in self.manifest["entries"] if r["source_path"] == "claude/skills/heartbeat-tasks/SKILL.md"]
        self.assertEqual(len(rows), 1, "exactly one canonical heartbeat-tasks row")
        row = rows[0]
        self.assertEqual(row["canonical_target"], TARGET)
        self.assertEqual(row["mode"], 420)
        self.assertEqual(row["dependency_status"], "closed")
        self.assertEqual(row["dependencies"], [])
        self.assertEqual(
            row["sha256"],
            hashlib.sha256(SKILL_PATH.read_bytes()).hexdigest(),
            "manifest digest must match the canonical source bytes",
        )

    def test_resolver_prefers_exactly_this_target(self) -> None:
        resolver = RESOLVER_PATH.read_text(encoding="utf-8")
        self.assertIn(
            'PROJECT_ROOT / ".claude/skills/heartbeat-tasks/SKILL.md"',
            resolver,
            "the resolver's preferred path must be the manifest target",
        )
        self.assertIn('PROJECT_ROOT = Path("/Users/rajiv/Downloads/projects/heydonna-app")', resolver)
        self.assertEqual(TARGET, "/Users/rajiv/Downloads/projects/heydonna-app/.claude/skills/heartbeat-tasks/SKILL.md")

    def test_manifest_count_and_order_are_deterministic(self) -> None:
        self.assertEqual(
            self.manifest["entries"],
            sorted(self.manifest["entries"], key=lambda item: item["source_path"]),
        )
        self.assertEqual(self.manifest["inventory"]["selected_count"], len(self.manifest["entries"]))
        self.assertEqual(self.manifest["inventory"]["ambiguous"], [])


if __name__ == "__main__":
    unittest.main()
