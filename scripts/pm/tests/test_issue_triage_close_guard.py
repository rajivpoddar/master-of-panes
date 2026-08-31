from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets"
SOP = SHARED / "codex" / "monitors" / "heydonna-issue-triage" / "WAKE_SOP.md"
PROMPT_TEMPLATE = SHARED / "codex" / "automations" / "heydonna-3h-ready-pool-reconciliation" / "prompt.template"


def load_guard() -> dict[str, object]:
    text = SOP.read_text(encoding="utf-8")
    match = re.search(r"```json\n(\{.*?\})\n```", text, flags=re.DOTALL)
    if match is None:
        raise AssertionError("close guard JSON is missing")
    guard = json.loads(match.group(1))
    if guard.get("name") != "customer_verification_reopen_close_guard":
        raise AssertionError("unexpected close guard")
    return guard


def close_decision(issue: dict[str, object], guard: dict[str, object]) -> str:
    """Apply the normative prompt/SOP discriminator to a live-state fixture."""
    if issue.get("state") != "OPEN":
        return "NO_CLOSE_ACTION"
    statuses = set(issue.get("statuses", []))
    protected = set(guard["protected_open_statuses"])
    if statuses & protected and issue.get("customer_verification_wake") is True:
        return "KEEP_OPEN_PRESERVE_STATUS_AND_LABELS"
    if (
        issue.get("explicit_terminal_close_intent") is True
        or issue.get("customer_verification_terminal") is True
        or issue.get("unambiguous_completion") is True
        or issue.get("canonical_duplicate") is True
    ):
        return "CLOSE_IDEMPOTENT"
    return "FAIL_CLOSED_KEEP_OPEN"


class IssueTriagesCloseGuardTests(unittest.TestCase):
    def test_prompt_and_sop_bind_the_same_machine_contract(self) -> None:
        guard = load_guard()
        self.assertTrue(guard["merged_ancestry_is_not_terminal"])
        self.assertTrue(guard["requires_customer_verification_wake"])
        self.assertEqual(guard["ambiguous_state"], "fail_closed_keep_open")
        prompt = PROMPT_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("CONTROL_PLANE_CLOSE_GUARD (mandatory before any CLOSE action)", prompt)
        self.assertIn("later deliberate reopen is authoritative", prompt)
        self.assertIn("Missing, stale, or contradictory lifecycle/wake evidence fails closed", prompt)
        manifest = json.loads((SHARED / "manifest.json").read_text(encoding="utf-8"))
        live_targets = [entry["canonical_target"] for entry in manifest["entries"]]
        self.assertIn("/Users/rajiv/.codex/automations/templates/heydonna-3h-ready-pool-reconciliation.prompt", live_targets)
        self.assertNotIn("/Users/rajiv/.codex/automations/heydonna-3h-ready-pool-reconciliation/automation.toml", live_targets)

    def test_synchronized_reopen_with_merged_ancestor_stays_open(self) -> None:
        guard = load_guard()
        issue = {
            "state": "OPEN",
            "statuses": ["status:in-review"],
            "customer_verification_wake": True,
            "merged_ancestor": True,
        }
        self.assertEqual(close_decision(issue, guard), "KEEP_OPEN_PRESERVE_STATUS_AND_LABELS")

    def test_todo_and_in_progress_customer_wakes_are_protected(self) -> None:
        guard = load_guard()
        for status in ("status:todo", "status:in-progress"):
            with self.subTest(status=status):
                self.assertEqual(
                    close_decision(
                        {"state": "OPEN", "statuses": [status], "customer_verification_wake": True, "merged_ancestor": True},
                        guard,
                    ),
                    "KEEP_OPEN_PRESERVE_STATUS_AND_LABELS",
                )

    def test_explicit_terminal_close_is_idempotent_and_ordinary_completion_survives(self) -> None:
        guard = load_guard()
        terminal = {"state": "OPEN", "statuses": [], "explicit_terminal_close_intent": True, "merged_ancestor": True}
        self.assertEqual(close_decision(terminal, guard), "CLOSE_IDEMPOTENT")
        self.assertEqual(close_decision(terminal, guard), "CLOSE_IDEMPOTENT")
        self.assertEqual(
            close_decision({"state": "OPEN", "statuses": [], "merged_ancestor": True, "unambiguous_completion": True}, guard),
            "CLOSE_IDEMPOTENT",
        )
        self.assertEqual(
            close_decision({"state": "OPEN", "statuses": [], "merged_ancestor": True}, guard),
            "FAIL_CLOSED_KEEP_OPEN",
        )

    def test_missing_or_ambiguous_customer_wake_fails_closed(self) -> None:
        guard = load_guard()
        for issue in (
            {"state": "OPEN", "statuses": ["status:in-review"], "merged_ancestor": True},
            {"state": "OPEN", "statuses": ["status:done"], "merged_ancestor": True, "customer_verification_wake": True},
        ):
            self.assertEqual(close_decision(issue, guard), "FAIL_CLOSED_KEEP_OPEN")


if __name__ == "__main__":
    unittest.main()
