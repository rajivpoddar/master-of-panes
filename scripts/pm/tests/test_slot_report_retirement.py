import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
ASSETS = ROOT / "scripts/pm/shared-assets"
RETIRED_REPORT_HOOK = ASSETS / "claude/hooks/block-unverified-codex-pm-report.py"
WRAPPER = pathlib.Path("/Users/rajiv/.claude/hooks/pretooluse-reason-wrapper.sh")


class SlotReportRetirementTest(unittest.TestCase):
    def test_retired_report_hook_is_compatibility_allow_noop(self):
        payload = {
            "cwd": "/Users/rajiv/Downloads/projects/heydonna-app-3001",
            "tool_name": "mcp__plugin_master-of-panes_mop__mop_send_to_slot",
            "tool_input": {"slot": 0, "message": "PR #123 Codex APPROVE"},
        }
        with tempfile.TemporaryDirectory() as directory:
            direct = subprocess.run(
                [sys.executable, str(RETIRED_REPORT_HOOK)],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                cwd=directory,
                check=False,
            )
            wrapped = subprocess.run(
                [str(WRAPPER), sys.executable, str(RETIRED_REPORT_HOOK)],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                cwd=directory,
                check=False,
            )
            self.assertEqual(direct.returncode, 0, direct.stderr)
            self.assertEqual(direct.stdout, "")
            self.assertEqual(wrapped.returncode, 0, wrapped.stderr)
            self.assertEqual(wrapped.stdout, "")
            self.assertEqual(list(pathlib.Path(directory).iterdir()), [])

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
                "/Users/rajiv/.claude/hooks/block-unverified-codex-pm-report.py",
                "/Users/rajiv/.claude/hooks/slot-terminal-message-pm-stop.sh",
                "/Users/rajiv/.claude/agents/codex-plan-reviewer.md",
                "/Users/rajiv/.claude/agents/codex-code-reviewer.md",
                "/Users/rajiv/Downloads/projects/heydonna-app/.claude/rules/99-pm-fable-review-cap.md",
            }
        }
        self.assertEqual(len(selected), 5)
        self.assertEqual(
            selected["/Users/rajiv/.claude/hooks/block-unverified-codex-pm-report.py"]["sha256"],
            hashlib.sha256(RETIRED_REPORT_HOOK.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            selected["/Users/rajiv/.claude/hooks/block-unverified-codex-pm-report.py"]["mode"],
            493,
        )
        self.assertNotIn(
            "claude/scripts/pm/remove-deprecated-codex-pm-report-hook.py",
            [entry["source_path"] for entry in manifest["entries"]],
        )
        self.assertEqual(
            selected["/Users/rajiv/.claude/hooks/slot-terminal-message-pm-stop.sh"]["mode"],
            493,
        )


if __name__ == "__main__":
    unittest.main()
