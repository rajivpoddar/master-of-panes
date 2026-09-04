#!/usr/bin/env python3
"""Contract checks for retiring CI/E2E spend-budget admission machinery."""

from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[3]
ASSETS = ROOT / "scripts" / "pm" / "shared-assets"
READINESS = ASSETS / "claude" / "scripts" / "ci" / "pr-ci-readiness-gate.py"
RERUN = ASSETS / "claude" / "scripts" / "ci" / "rerun-after-local-proof.sh"
HOURLY = ASSETS / "claude" / "scripts" / "hourly-ops-review-prompt.txt"
MANIFEST = ASSETS / "manifest.json"


class CiBudgetRetirementTests(unittest.TestCase):
    def test_non_s5_admission_surfaces_have_no_budget_authority(self) -> None:
        readiness = READINESS.read_text(encoding="utf-8")
        rerun = RERUN.read_text(encoding="utf-8")
        hourly = HOURLY.read_text(encoding="utf-8")
        forbidden = (
            "pr-ci-budget.py",
            "CI_BUDGET",
            "BUDGET_SUBPROCESS",
            "ci_budget",
            "PR_CI_BUDGET_EXCEEDED",
            "current_head_bad_run_budget_exceeded",
            "max_total_runs",
            "max_expensive_runs",
            "estimated_min_cost_budget_exceeded",
        )
        for surface in (readiness, rerun, hourly):
            for token in forbidden:
                self.assertNotIn(token, surface)

    def test_safety_guards_remain_and_s5_capture_boundary_is_not_claimed(self) -> None:
        rerun = RERUN.read_text(encoding="utf-8")
        for token in ("exact", "head", "single", "flight", "cleanup"):
            self.assertIn(token, rerun.lower())
        # The capture caller is intentionally S5-owned and is not part of this
        # shared-asset manifest; this test prevents accidentally adding it here.
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        sources = {entry["source_path"] for entry in manifest["entries"]}
        self.assertNotIn("claude/scripts/ci/pr-ci-budget.py", sources)
        readiness_entry = next(
            entry for entry in manifest["entries"]
            if entry["source_path"] == "claude/scripts/ci/pr-ci-readiness-gate.py"
        )
        self.assertIn(
            "/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/pr-ci-readiness-gate.py",
            readiness_entry["additional_targets"],
        )

    def test_hourly_prompt_uses_safety_and_causal_checks_only(self) -> None:
        hourly = HOURLY.read_text(encoding="utf-8")
        self.assertIn("exact-head", hourly)
        self.assertIn("causal-classification", hourly)
        self.assertNotIn("48h", hourly)


if __name__ == "__main__":
    unittest.main()
