from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets"
HOOK = SHARED / "claude" / "hooks" / "pm-context-injector.sh"
SOP = SHARED / "claude" / "skills" / "pm-message-to-action" / "SKILL.md"


class PMContextInjectorSOPTests(unittest.TestCase):
    def run_hook(self, payload: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(HOOK)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ, "HOME": tempfile.gettempdir()},
        )

    def test_sop_covers_all_current_mapping_branches(self) -> None:
        sop = SOP.read_text(encoding="utf-8")
        for phrase in (
            "pm-nudge-processing",
            "codex-comment-processing",
            "alert-processing",
            "ci-failure-investigation",
            "ci-success-reconciliation",
            "capture-alert-processing",
            "survey-report-prompt-miner",
            "customer-artifact-investigator",
            "pm-autoscaler-repair",
            "pr-state-sweep",
            "pm-pr-rescue",
            "cleanup-pr",
            "Quoted history",
            "not a new event",
        ):
            self.assertIn(phrase, sop)
        self.assertNotIn("delegation", sop.lower())
        self.assertNotIn("utilization", sop.lower())

    def test_hook_is_content_independent_and_path_only(self) -> None:
        cwd = "/Users/rajiv/Downloads/projects/heydonna-app"
        first = self.run_hook({"cwd": cwd, "prompt": "NUDGE: slot 4; gh pr merge; CI failed"})
        second = self.run_hook({"cwd": cwd, "prompt": "plain unrelated text"})
        self.assertEqual(first.returncode, 0)
        self.assertEqual(second.returncode, 0)
        self.assertEqual(first.stdout, second.stdout)
        self.assertIn("PM_SOP_PATH_REMINDER", first.stdout)
        self.assertIn("pm-message-to-action/SKILL.md", first.stdout)
        self.assertNotIn("pm-nudge-processing", first.stdout)
        self.assertNotIn("ci-failure-investigation", first.stdout)

    def test_non_pm_scope_and_malformed_input_fail_open_silently(self) -> None:
        outside = self.run_hook({"cwd": "/Users/rajiv/Downloads/projects/heydonna-app-3005", "prompt": "NUDGE: slot 5"})
        malformed = subprocess.run(
            ["bash", str(HOOK)],
            input="not-json",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(outside.returncode, 0)
        self.assertEqual(outside.stdout, "")
        self.assertEqual(malformed.returncode, 0)
        self.assertEqual(malformed.stdout, "")

    def test_hook_has_no_classifier_or_side_effect_writer(self) -> None:
        source = HOOK.read_text(encoding="utf-8")
        for forbidden in ("grep", "pm-ops", "obligation-upsert", "mkdir", "Skill(", "REMINDER=", "/tmp/", ">>"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
