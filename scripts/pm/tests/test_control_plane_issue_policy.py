"""Regression coverage for the issue-create internal-surface classifier."""

from __future__ import annotations

import sys
from pathlib import Path


POLICY_DIR = Path(__file__).parents[1] / "shared-assets" / "claude" / "scripts"
sys.path.insert(0, str(POLICY_DIR))

from control_plane_issue_policy import (  # noqa: E402
    INTERNAL_FOLLOWUP_MARKER,
    internal_control_plane_reason,
)


SUPPORT_CHAT_TITLE = "Support chat refresh docs add marketing blog content persist"
SUPPORT_CHAT_BODY = f"""{INTERNAL_FOLLOWUP_MARKER}

## Runtime control point

`scripts/generate-docs-context.mjs` refreshes the support-chat context, and
`app/api/chat/support/route.ts` awaits the chat-support log write.

## Validation

Focused Vitest for the generator and route log write; typecheck; lint; affected-test plan.
"""


def test_product_support_chat_issue_is_not_misclassified_from_affected_test_plan_prose():
    assert internal_control_plane_reason(SUPPORT_CHAT_TITLE, SUPPORT_CHAT_BODY) is None


def test_pm_operator_issue_remains_classified_as_internal_control_plane_work():
    body = (
        f"{INTERNAL_FOLLOWUP_MARKER}\n\n"
        "Runtime control point: scripts/pm/shared-assets/claude/scripts/"
        "control_plane_issue_policy.py."
    )

    assert (
        internal_control_plane_reason("Fix PM Operator issue classification", body)
        == "internal_followup_targets_control_plane_surface"
    )


def test_affected_test_plan_script_symbol_remains_a_control_plane_surface():
    body = f"{INTERNAL_FOLLOWUP_MARKER}\n\nAffected test entry point: affected-test-plan.py."

    assert (
        internal_control_plane_reason("Fix affected test planner", body)
        == "internal_followup_targets_control_plane_surface"
    )
