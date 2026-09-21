"""Survival regression for the installed-only llm_proxy_miss_strict token.

App commit 61133b883 added llm_proxy_miss_strict to the capture-or-fixture
arm directly on the installed mirror; the canonical MoP source must carry
the same token so a scoped install cannot regress the live Slack alert
classifier to unknown. Mirrors the app-side expectations byte-for-behavior.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "shared-assets" / "claude" / "scripts" / "ci" / "ci-fast-triage.py"
SPEC = importlib.util.spec_from_file_location("ci_fast_triage_miss_strict", SOURCE)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

Annotation = MODULE.Annotation


class MissStrictSurvivalTests(unittest.TestCase):
    def test_strict_fixture_miss_routes_capture_or_fixture(self) -> None:
        annotation = Annotation(
            "tests/e2e/specs/core/critical/auto-process-regression.spec.ts",
            1, "failure", "",
            "Error: Test5 formatter baseline missing "
            "(llm_proxy_miss_strict section_type=index hash=fbed56f92be4)",
            "",
        )
        self.assertEqual(
            MODULE.category_for([annotation], []), "capture-or-fixture"
        )

    def test_strict_fixture_miss_match_is_case_insensitive(self) -> None:
        annotation = Annotation(
            "", 1, "failure", "",
            "LLM_PROXY_MISS_STRICT section_type=index hash=FBED56F92BE4",
            "",
        )
        self.assertEqual(
            MODULE.category_for([annotation], []), "capture-or-fixture"
        )

    def test_capture_or_fixture_repro_is_preflight_not_grep_narrowed(self) -> None:
        annotation = Annotation(
            "tests/e2e/specs/core/critical/auto-process-regression.spec.ts",
            1, "failure", "",
            "Error: Test5 formatter baseline missing "
            "(llm_proxy_miss_strict section_type=index)",
            "",
        )
        command = MODULE.local_command("capture-or-fixture", [annotation])
        self.assertIsNotNone(command)
        assert command is not None
        self.assertIn("local-repro-preflight.sh", command)
        self.assertNotIn("--grep", command)


if __name__ == "__main__":
    unittest.main()
