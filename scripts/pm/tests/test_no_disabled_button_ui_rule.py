#!/usr/bin/env python3
"""Focused proof for the Rajiv 2026-09-26 no-disabled-button product UI rule.

Asserts the verbatim rule reaches every planner/reviewer surface through the
canonical shared-asset mapping, including the Codex review companion prompts
resolved exactly as codex-review-companion.mjs loadPromptTemplate resolves
them, and that each surface names the gate so a reviewer can cite it.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import stat
import subprocess
import unittest

ROOT = pathlib.Path(__file__).parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets"
MANIFEST = SHARED / "manifest.json"

RULE = (
    "UI RULE (Rajiv 2026-09-26): Do not gate actions with disabled buttons. Buttons stay "
    "enabled; on click, validate and render an error state on the offending control "
    "(checkbox/field error styling + inline message) and do not proceed. The only allowed "
    "disabled state is the action's own in-flight/double-submit guard. Plans and reviews must "
    "call out any new `disabled=` on a button that encodes a precondition and require the "
    "error-state pattern instead."
)
RULE_FILE = "claude/user-rules/33-heydonna-ui-product-rules.md"
RULE_TARGET = "/Users/rajiv/.claude/rules/33-heydonna-ui-product-rules.md"

# source_path -> (canonical_target, gate phrase the surface must name)
SURFACES = {
    "claude/agents/plan-agent.md":
        ("/Users/rajiv/.claude/agents/plan-agent.md", "No-disabled-button Planning Gate"),
    "claude/agents/codex-plan-reviewer.md":
        ("/Users/rajiv/.claude/agents/codex-plan-reviewer.md", "No-disabled-button gate"),
    "claude/agents/codex-code-reviewer.md":
        ("/Users/rajiv/.claude/agents/codex-code-reviewer.md", "No-disabled-button gate"),
    "claude/project-agents/pm-plan-reviewer.md":
        ("/Users/rajiv/Downloads/projects/heydonna-app/.claude/agents/pm-plan-reviewer.md",
         "No-disabled-button gate"),
    "claude/project-agents/pm-code-reviewer.md":
        ("/Users/rajiv/Downloads/projects/heydonna-app/.claude/agents/pm-code-reviewer.md",
         "No-disabled-button gate"),
}


def companion_prompt(review_type: str, rework: bool) -> pathlib.Path:
    """Mirror codex-review-companion.mjs loadPromptTemplate resolution."""
    name = "prompt-rework.txt" if rework else "prompt.txt"
    return SHARED / "claude" / "skills" / f"codex-app-{review_type}-review" / "templates" / name


COMPANION_MODES = [(rt, rw) for rt in ("plan", "code") for rw in (False, True)]


def entries() -> dict[str, dict]:
    return {e["source_path"]: e for e in json.loads(MANIFEST.read_text(encoding="utf-8"))["entries"]}


class RuleFileTests(unittest.TestCase):
    def test_rule_file_is_managed_and_auto_loaded_for_every_checkout(self):
        # ~/.claude/rules/ is loaded by Claude Code in every project, so the PM
        # checkout and all numbered-slot checkouts receive the rule.
        row = entries()[RULE_FILE]
        self.assertEqual(row["canonical_target"], RULE_TARGET)
        body = (SHARED / RULE_FILE).read_text(encoding="utf-8")
        self.assertIn(RULE, body)
        self.assertIn("disabled={!agreed}", body)
        self.assertIn("aria-invalid", body)
        self.assertIn("disabled={isSubmitting}", body)


class AgentSurfaceTests(unittest.TestCase):
    def test_every_planner_and_reviewer_carries_the_verbatim_rule_and_gate(self):
        rows = entries()
        for src, (target, gate) in SURFACES.items():
            with self.subTest(surface=src):
                self.assertEqual(rows[src]["canonical_target"], target)
                body = (SHARED / src).read_text(encoding="utf-8")
                self.assertIn(RULE, body)
                self.assertIn(gate, body)
                self.assertIn("33-heydonna-ui-product-rules.md", body)

    def test_reviewers_request_changes_and_allow_the_in_flight_guard(self):
        for src in ("claude/agents/codex-plan-reviewer.md", "claude/agents/codex-code-reviewer.md"):
            body = (SHARED / src).read_text(encoding="utf-8")
            self.assertIn("REQUEST_CHANGES and name this gate", body)
            self.assertIn("disabled={isSubmitting}", body)
        for src in ("claude/project-agents/pm-plan-reviewer.md", "claude/project-agents/pm-code-reviewer.md"):
            body = (SHARED / src).read_text(encoding="utf-8")
            self.assertIn("BLOCKED", body)
            self.assertIn("REQUEST_CHANGES", body)

    def test_code_reviewer_gate_sits_after_the_ui_chrome_gate(self):
        body = (SHARED / "claude/agents/codex-code-reviewer.md").read_text(encoding="utf-8")
        chrome = body.index("## UI chrome / layout invariant gate")
        gate = body.index("## No-disabled-button gate")
        convex = body.index("## Convex metadata-only transcript artifact boundary gate")
        self.assertLess(chrome, gate)
        self.assertLess(gate, convex)


class CompanionPromptTests(unittest.TestCase):
    def test_every_companion_mode_carries_the_rule_and_named_gate(self):
        for rt, rw in COMPANION_MODES:
            with self.subTest(review=rt, rework=rw):
                path = companion_prompt(rt, rw)
                self.assertTrue(path.is_file(), f"loader would hard-fail: {path}")
                body = path.read_text(encoding="utf-8")
                self.assertEqual(body.count("<!-- NO_DISABLED_BUTTON_UI_RULE_V1 -->"), 1)
                self.assertIn(RULE, body)
                self.assertIn("REQUEST_CHANGES and name `NO-DISABLED-BUTTON GATE`", body)
                self.assertIn("aria-invalid", body)
                self.assertIn("disabled={isSubmitting}", body)

    def test_plan_prompts_target_plans_and_code_prompts_target_diffs(self):
        for rw in (False, True):
            plan = companion_prompt("plan", rw).read_text(encoding="utf-8")
            code = companion_prompt("code", rw).read_text(encoding="utf-8")
            self.assertIn("when the plan or an AC specifies", plan)
            self.assertIn("when the diff adds", code)

    def test_parent_prompts_lack_the_rule_red_witness(self):
        parent = subprocess.run(["git", "rev-parse", "HEAD^"], capture_output=True, text=True,
                                cwd=str(ROOT)).stdout.strip()
        for rt, rw in COMPANION_MODES:
            rel = companion_prompt(rt, rw).relative_to(ROOT).as_posix()
            shown = subprocess.run(["git", "show", f"{parent}:{rel}"], capture_output=True, text=True,
                                   cwd=str(ROOT))
            self.assertEqual(shown.returncode, 0, rel)
            self.assertNotIn("NO_DISABLED_BUTTON_UI_RULE_V1", shown.stdout, rel)


class ManifestParityTests(unittest.TestCase):
    def test_every_rule_surface_row_matches_its_source_bytes_and_mode(self):
        rows = entries()
        paths = [RULE_FILE, *SURFACES] + [
            companion_prompt(rt, rw).relative_to(SHARED).as_posix() for rt, rw in COMPANION_MODES
        ]
        for src in paths:
            with self.subTest(source=src):
                row = rows[src]
                f = SHARED / src
                self.assertEqual(hashlib.sha256(f.read_bytes()).hexdigest(), row["sha256"])
                self.assertEqual(stat.S_IMODE(f.stat().st_mode), row["mode"])
                self.assertEqual(row["mode"], 420)
                self.assertEqual((row["dependency_status"], row["dependencies"]), ("closed", []))


if __name__ == "__main__":
    unittest.main()
