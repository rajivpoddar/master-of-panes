#!/usr/bin/env python3
"""Keep the managed review-cap rule aligned with companion cap packets."""

from __future__ import annotations

import hashlib
import json
import pathlib
import stat
import unittest

ROOT = pathlib.Path(__file__).parents[3]
RULE = ROOT / "scripts/pm/shared-assets/claude/rules/99-pm-fable-review-cap.md"
COMPANION = ROOT / "scripts/pm/shared-assets/claude/skills/codex-review-companion/codex-review-companion.mjs"
MANIFEST = ROOT / "scripts/pm/shared-assets/manifest.json"
TARGET = "/Users/rajiv/Downloads/projects/heydonna-app/.claude/rules/99-pm-fable-review-cap.md"
SLOT_HOLD = "Keep the slot held: no release, no reassign, no `pm-blocked:cto`; the slot keeps its ownership, branch, and worktree."


class ReviewCapPolicyTests(unittest.TestCase):
    def test_policy_routes_match_emitted_cap_packet(self):
        policy = RULE.read_text(encoding="utf-8")
        companion = COMPANION.read_text(encoding="utf-8")
        self.assertIn("PLAN_REVIEW_CAP_REACHED", policy)
        self.assertIn("supported_route=cto_plan_adjudication", policy)
        self.assertIn("CODE_REVIEW_CAP_REACHED", policy)
        self.assertIn("supported_route=cto_review_adjudication", policy)
        self.assertIn('"cto_plan_adjudication"', companion)
        self.assertIn('"cto_review_adjudication"', companion)
        self.assertNotIn("pm-kimi3-pr-rescue", policy)
        self.assertNotIn("PM_KIMI3_RESCUE_REQUEST", policy)
        self.assertNotIn("isolation=\"worktree\"", policy)

    def test_both_cap_routes_keep_slot_ownership_held(self):
        policy = " ".join(RULE.read_text(encoding="utf-8").split())
        plan_route = policy.split("When `PLAN_REVIEW_CAP_REACHED` reports", 1)[1].split(
            " When `CODE_REVIEW_CAP_REACHED` reports", 1
        )[0]
        code_route = policy.split("When `CODE_REVIEW_CAP_REACHED` reports", 1)[1].split(
            " For other cap packet types", 1
        )[0]
        self.assertIn(SLOT_HOLD, plan_route)
        self.assertIn(SLOT_HOLD, code_route)
        self.assertEqual(policy.count(SLOT_HOLD), 2)

    def test_manifest_maps_exact_rule_target_and_source_bytes(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        entries = manifest["entries"]
        self.assertEqual(entries, sorted(entries, key=lambda row: row["source_path"]))
        self.assertEqual(manifest["inventory"]["selected_count"], len(entries))
        rows = [row for row in entries if row["source_path"] == "claude/rules/99-pm-fable-review-cap.md"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["canonical_target"], TARGET)
        self.assertEqual(row["mode"], 420)
        self.assertEqual(row["ownership_class"], "shared-claude-pm-review-cap-policy")
        self.assertEqual(row["dependency_status"], "closed")
        self.assertEqual(row["dependencies"], [])
        self.assertEqual(hashlib.sha256(RULE.read_bytes()).hexdigest(), row["sha256"])
        self.assertEqual(stat.S_IMODE(RULE.stat().st_mode), row["mode"])


if __name__ == "__main__":
    unittest.main()
