from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL = REPO_ROOT / "scripts" / "pm" / "shared-assets" / "codex" / "skills" / "explore-issue" / "SKILL.md"
MANIFEST = REPO_ROOT / "scripts" / "pm" / "shared-assets" / "manifest.json"


class ExploreIssueObservationPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = SKILL.read_text(encoding="utf-8")
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def test_confirmed_unknown_observation_is_investigation_ready(self) -> None:
        self.assertIn("A confirmed customer or Abi symptom", self.text)
        self.assertIn("enough to file an `INVESTIGATION` issue", self.text)
        self.assertIn("`UNKNOWN` or a clearly labelled `HYPOTHESIS`", self.text)
        self.assertIn("evidence limits", self.text)

    def test_unconfirmed_and_unproven_cause_fail_closed(self) -> None:
        self.assertIn("An unconfirmed report remains unfiled", self.text)
        self.assertIn("Only causal proof may promote", self.text)
        self.assertIn("or if the observation is UNCONFIRMED: do not file", self.text)

    def test_duplicate_and_external_evidence_actions_remain_bounded(self) -> None:
        self.assertRegex(self.text, r"A duplicate\s+reconciles to the existing issue")
        self.assertIn("Never acquire a customer query, artifact download,", self.text)
        self.assertIn("such work requires its own explicit authority", self.text)

    def test_codex_review_branch_distinguishes_investigation_from_fix(self) -> None:
        self.assertRegex(self.text, r"Codex review is not an evidence-acquisition\s+gate")
        self.assertIn("NEEDS_DEEPER_INVESTIGATION and the reported observation is CONFIRMED", self.text)
        self.assertIn("proceed to Phase 3 as `INVESTIGATION`", self.text)
        self.assertIn("If MISDIAGNOSED", self.text)

    def test_source_manifest_mapping_is_exact(self) -> None:
        relative = "codex/skills/explore-issue/SKILL.md"
        entries = [entry for entry in self.manifest["entries"] if entry["source_path"] == relative]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["canonical_target"], "/Users/rajiv/.claude/skills/explore-issue/SKILL.md")
        self.assertEqual(entry["mode"], 0o644)
        digest = hashlib.sha256(SKILL.read_bytes()).hexdigest()
        self.assertEqual(entry["sha256"], digest)
        self.assertEqual(self.manifest["inventory"]["selected_count"], len(self.manifest["entries"]))


if __name__ == "__main__":
    unittest.main()
