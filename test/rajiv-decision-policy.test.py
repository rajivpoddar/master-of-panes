from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "scripts" / "pm" / "shared-assets"

PM_SKILLS = (
    "claude/skills/direct-assign/SKILL.md",
    "claude/skills/direct-release/SKILL.md",
    "claude/skills/pm-nudge-processing/SKILL.md",
    "claude/skills/pm-wait-nudge/SKILL.md",
)

CTO_SKILLS = (
    "codex/skills/heydonna-control-plane-repair/SKILL.md",
    "codex/skills/heydonna-open-pr-status/SKILL.md",
)

CHANGED_ASSETS = PM_SKILLS + CTO_SKILLS + (
    "codex/skills/heydonna-slack-postback/SKILL.md",
)


class RajivDecisionPolicyTest(unittest.TestCase):
    def test_pm_decisions_route_to_cto_without_mutation(self) -> None:
        for relative in PM_SKILLS:
            text = (ASSETS / relative).read_text(encoding="utf-8")
            normalized = " ".join(text.split())
            with self.subTest(relative=relative):
                self.assertIn("Rajiv decision gate", text)
                self.assertIn("#heydonna-dev", text)
                self.assertIn("CTO", text)
                self.assertRegex(normalized, r"CTO (?:must|alone) DM(?:s)? Rajiv")
                self.assertRegex(
                    normalized,
                    r"stop(?:s)? (?:before (?:the POST|mutation|assigning)|without assigning)",
                )

    def test_cto_decisions_dm_rajiv_before_mutation(self) -> None:
        for relative in CTO_SKILLS:
            text = (ASSETS / relative).read_text(encoding="utf-8")
            normalized = " ".join(text.split()).lower()
            with self.subTest(relative=relative):
                self.assertIn("Rajiv product/process decision gate", text)
                self.assertIn("D0BPG55FG72", text)
                self.assertIn("stop before mutation", normalized)
                self.assertIn("explicit approval", normalized)

    def test_slack_skill_covers_product_and_process_decisions(self) -> None:
        text = (ASSETS / "codex/skills/heydonna-slack-postback/SKILL.md").read_text(
            encoding="utf-8"
        )
        normalized = " ".join(text.split())
        self.assertIn("CTO product and process decisions", text)
        self.assertIn("D0BPG55FG72", text)
        self.assertIn("PM sends", text)
        self.assertIn("CTO alone sends Rajiv the DM", normalized)
        self.assertIn("before explicit Rajiv approval", normalized)

    def test_manifest_digests_match_changed_assets(self) -> None:
        manifest = json.loads((ASSETS / "manifest.json").read_text(encoding="utf-8"))
        entries = {entry["source_path"]: entry for entry in manifest["entries"]}
        for relative in CHANGED_ASSETS:
            data = (ASSETS / relative).read_bytes()
            with self.subTest(relative=relative):
                self.assertIn(relative, entries)
                self.assertEqual(hashlib.sha256(data).hexdigest(), entries[relative]["sha256"])


if __name__ == "__main__":
    unittest.main()
