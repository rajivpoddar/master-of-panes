from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "scripts" / "pm" / "shared-assets"
PM_ROOT_RULE = Path("/Users/rajiv/Downloads/projects/heydonna-app/.claude/rules/20-buddhi-pm.md")

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

CTO_ADMISSION_SKILL = "codex/skills/heydonna-cto-label-gated-ci/SKILL.md"

CHANGED_ASSETS = PM_SKILLS + CTO_SKILLS + (
    CTO_ADMISSION_SKILL,
    "codex/skills/heydonna-slack-postback/SKILL.md",
    "codex/skills/_shared/release-conveyor-contract.md",
)


class RajivDecisionPolicyTest(unittest.TestCase):
    def test_routine_pm_execution_has_no_approval_hop(self) -> None:
        for relative in PM_SKILLS:
            text = (ASSETS / relative).read_text(encoding="utf-8")
            normalized = " ".join(text.split())
            with self.subTest(relative=relative):
                self.assertIn("CTO", text)
                self.assertIn("routine", normalized)
                self.assertTrue(
                    "genuine decisions to cto" in normalized.lower()
                    or "routes to cto" in normalized.lower()
                )
                self.assertIn("shared release-conveyor decision boundary", normalized)
                self.assertNotIn("Rajiv decision gate", text)
                self.assertNotIn("waits for explicit approval", normalized)

    def test_cto_owns_routine_repairs_and_escalates_only_reserved_decisions(self) -> None:
        for relative in CTO_SKILLS:
            text = (ASSETS / relative).read_text(encoding="utf-8")
            normalized = " ".join(text.split()).lower()
            with self.subTest(relative=relative):
                self.assertIn("routine", normalized)
                self.assertIn("shared release-conveyor decision boundary", normalized)
                self.assertNotIn("Rajiv product/process decision gate", text)
                self.assertNotIn("resume only after explicit approval", normalized)

    def test_routine_and_reserved_scenarios_are_distinct(self) -> None:
        contract = " ".join(
            (ASSETS / "codex/skills/_shared/release-conveyor-contract.md")
            .read_text(encoding="utf-8")
            .lower()
            .split()
        )
        self.assertIn("reviewed publication", contract)
        self.assertIn("red required-workflow merge", contract)
        self.assertIn("destructive/high-risk", contract)
        self.assertIn("routine scheduling", contract)
        self.assertIn("choosing among eligible actions is not a process-policy change", contract)
        self.assertNotIn("stop before mutation and dm rajiv", contract)

    def test_cto_admission_skill_has_no_obsolete_approval_gate(self) -> None:
        text = (ASSETS / CTO_ADMISSION_SKILL).read_text(encoding="utf-8")
        self.assertIn("Only CTO/PR Merges may use this prompt", text)
        self.assertNotIn("Rajiv product/process decision gate", text)
        self.assertNotIn("wait for explicit approval", " ".join(text.split()).lower())

    def test_pm_root_rule_has_one_review_boundary(self) -> None:
        if not PM_ROOT_RULE.exists():
            self.skipTest("installed PM root rule is outside this repository")
        normalized = " ".join(PM_ROOT_RULE.read_text(encoding="utf-8").split()).lower()
        self.assertNotIn("after any rework commit", normalized)
        self.assertNotIn("use sonnet first", normalized)
        self.assertNotIn("r2/r3", normalized)
        self.assertIn("one functionality-first independent review", normalized)
        self.assertIn("unchanged-code", normalized)

    def test_slack_skill_routes_only_material_decisions(self) -> None:
        text = (ASSETS / "codex/skills/heydonna-slack-postback/SKILL.md").read_text(
            encoding="utf-8"
        )
        normalized = " ".join(text.split()).lower()
        self.assertIn("customer/product", normalized)
        self.assertIn("D0BPG55FG72", text)
        self.assertIn("PM sends", text)
        self.assertIn("routine execution under approved rules does not require a rajiv approval hop", normalized)
        self.assertIn("shared release-conveyor decision boundary", normalized)
        self.assertNotIn("a process decision is any", normalized)
        self.assertNotIn("before explicit rajiv approval", normalized)

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
