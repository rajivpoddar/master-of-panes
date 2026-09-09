import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
ASSETS = ROOT / "scripts/pm/shared-assets"


class SlotReportRetirementTest(unittest.TestCase):
    def test_active_review_consumers_use_supported_pm_delivery(self):
        files = [
            ASSETS / "claude/agents/codex-plan-reviewer.md",
            ASSETS / "claude/agents/codex-code-reviewer.md",
            ASSETS / "claude/rules/99-pm-fable-review-cap.md",
        ]
        for path in files[:2]:
            text = path.read_text()
            self.assertNotIn("slot-report-codex-verdict.sh", text)
            self.assertIn("Skill(message-pm)", text)
            self.assertIn("MARKER_PROVENANCE", text)
            self.assertIn("FINAL_REVIEWER_VERDICT", text)
        rule_text = files[2].read_text()
        self.assertNotIn("slot-report-codex-verdict.sh", rule_text)
        self.assertIn("Skill(message-pm)", rule_text)

    def test_stop_consumer_falls_back_to_normal_pm_status_path(self):
        text = (ASSETS / "claude/hooks/slot-terminal-message-pm-stop.sh").read_text()
        self.assertNotIn("slot-report-codex-verdict.sh", text)
        self.assertNotIn('elif "[SLOT_CODEX_VERDICT" in reason:', text)
        self.assertIn("Skill(message-pm) or direct MoP PM delivery", text)
        self.assertIn("MARKER_PROVENANCE", text)

    def test_manifest_has_exact_selected_mappings(self):
        manifest = json.loads((ASSETS / "manifest.json").read_text())
        selected = {
            entry["canonical_target"]: entry
            for entry in manifest["entries"]
            if entry.get("canonical_target") in {
                "/Users/rajiv/.claude/hooks/slot-terminal-message-pm-stop.sh",
                "/Users/rajiv/.claude/agents/codex-plan-reviewer.md",
                "/Users/rajiv/.claude/agents/codex-code-reviewer.md",
                "/Users/rajiv/Downloads/projects/heydonna-app/.claude/rules/99-pm-fable-review-cap.md",
            }
        }
        self.assertEqual(len(selected), 4)
        self.assertEqual(
            selected["/Users/rajiv/.claude/hooks/slot-terminal-message-pm-stop.sh"]["mode"],
            493,
        )


if __name__ == "__main__":
    unittest.main()
