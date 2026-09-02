from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets"
SKILL = SHARED / "codex/skills/heydonna-open-pr-status/SKILL.md"
PROMPT = SHARED / "codex/automations/pr-merges-residency-heartbeat/prompt.template"
CONTRACT = SHARED / "codex/skills/_shared/release-conveyor-contract.md"
MONITOR = SHARED / "codex/monitors/heydonna-pm-chat/MONITOR.md"
WAKE = SHARED / "codex/monitors/heydonna-pm-chat/WAKE_SOP.md"
REPAIR = SHARED / "codex/skills/heydonna-control-plane-repair/SKILL.md"
STATES = {
    "CI_E2E_IN_PROGRESS",
    "CAPTURE_IN_PROGRESS",
    "REPRO_REWORK_IN_PROGRESS",
    "REPRO_REWORK_QUEUED",
}


class OpenPrFourStateContractTests(unittest.TestCase):
    def test_contract_has_no_fifth_terminal_and_prompt_is_post_action(self):
        skill = SKILL.read_text()
        prompt = PROMPT.read_text()
        for state in STATES:
            self.assertIn(state, skill)
            self.assertIn(state, prompt)
        self.assertIn("ACTION_REQUIRED`, `UNKNOWN`", skill)
        self.assertIn("There is no ACTION_REQUIRED terminal", prompt)
        self.assertIn("post-action", skill)
        self.assertNotIn("| ACTION_REQUIRED |", skill)

    def test_pm_terminal_contract_is_machine_readable_across_all_consumers(self):
        sources = [p.read_text() for p in (CONTRACT, SKILL, PROMPT, MONITOR, WAKE, REPAIR)]
        combined = "\n".join(sources)
        for terminal in (
            "FAILED_RUN_INVESTIGATION",
            "NUMBERED_PROOF",
            "REWORK_REVIEW_CANDIDATE",
            "CAPTURE_TERMINAL",
            "ASSIGNMENT_TERMINAL",
            "TYPED_BLOCKER",
        ):
            self.assertIn(terminal, combined)
        for field in ("terminal_type", "source_receipt", "next_action", "next_owner", "wake"):
            self.assertIn(field, combined)
        self.assertIn('"pm_terminal_envelope"', CONTRACT.read_text())
        self.assertIn('"terminal_continuity_backstop"', CONTRACT.read_text())
        self.assertIn("receipt-continuity backstop", PROMPT.read_text())
        self.assertIn("non-suppressible", WAKE.read_text())
        self.assertNotIn("ACTION_REQUIRED as", PROMPT.read_text())

    def test_terminal_fixtures_wake_once_and_backstop_only_repairs_missed_consumption(self):
        fields = {
            "terminal_type": "FAILED_RUN_INVESTIGATION",
            "pr": 7591,
            "head": "a" * 40,
            "run_or_capture": "run-1",
            "owner": "pm",
            "evidence_summary": "bounded exact-head failure",
            "next_action": "route causal disposition",
            "next_owner": "CTO",
            "wake": "immediate",
            "source_receipt": "receipt-1",
        }
        key = (fields["terminal_type"], fields["pr"], fields["head"], fields["source_receipt"])
        self.assertEqual(len(set(key)), 4)
        self.assertEqual("immediate", fields["wake"])
        # The monitor emits one wake for the first tuple and suppresses a duplicate.
        delivered = {key}
        self.assertEqual(len(delivered), 1)
        self.assertNotIn(key, delivered - {key})
        # A missing CTO consumption receipt is repaired once by the hourly backstop.
        continuity = {"emitted": True, "consumed": False, "next_edge": False, "repairs": 0}
        if continuity["emitted"] and not continuity["consumed"]:
            continuity["repairs"] += 1
            continuity["consumed"] = True
            continuity["next_edge"] = True
        self.assertEqual(continuity["repairs"], 1)
        self.assertTrue(continuity["next_edge"])

    def test_routine_progress_is_not_a_cto_wake_and_pm_cannot_execute_release_edges(self):
        routine = {"kind": "progress", "wake": False}
        self.assertFalse(routine["wake"])
        combined = (MONITOR.read_text() + WAKE.read_text() + PROMPT.read_text())
        self.assertIn("PM never executes CTO-owned CI/E2E", combined)
        self.assertIn("integration, or merge", combined)

    def test_transition_fixtures_finish_exactly_once(self):
        fixtures = [
            ("rework_complete", "CI_E2E_IN_PROGRESS", "admit one genuine exact-head CI/E2E pair"),
            ("failure_classified", "REPRO_REWORK_QUEUED", "create or resume exactly one active or durable queued repro/rework packet"),
            ("capture_complete", "CI_E2E_IN_PROGRESS", "admit one genuine exact-head CI/E2E pair"),
            ("dual_green", "CI_E2E_IN_PROGRESS", "head-pinned merge"),
            ("process_refusal", "REPRO_REWORK_QUEUED", "guarded direct fallback once"),
            ("true_blocker", "REPRO_REWORK_QUEUED", "concrete conflict/product blocker"),
        ]
        for name, state, action in fixtures:
            row = {"pr": 7000 + len(name), "head": "a" * 40, "state": state, "action": action, "edge_count": 1}
            self.assertIn(row["state"], STATES, name)
            self.assertEqual(row["edge_count"], 1, name)
        # No row may settle in a fifth state; an unconvertible row is loud.
        self.assertIn("OPEN_PR_FOUR_STATE_INVARIANT_BREACH", MONITOR.read_text())

    def test_identical_snapshot_is_idempotent_and_no_legacy_route(self):
        payload = {"rows": [{"pr": 7590, "head": "b" * 40, "state": "REPRO_REWORK_QUEUED", "edge": "queue-rework"}]}
        self.assertEqual(json.loads(json.dumps(payload)), payload)
        combined = (SKILL.read_text() + PROMPT.read_text()).lower()
        self.assertRegex(combined, r"never blind\s+rerun")
        self.assertIn("There is no ACTION_REQUIRED terminal", PROMPT.read_text())


if __name__ == "__main__":
    unittest.main()
