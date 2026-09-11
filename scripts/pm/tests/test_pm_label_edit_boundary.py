"""Focused proof for the PM label-edit boundary (Rajiv directive Ev0C14NF0VED).

PM is blocked only from label-gated CI; the CI-trigger boundary asks PM to
request the CTO. Every other PR/issue label add, remove, or replace must pass,
and the unrelated non-label guards must stay intact.
"""

from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[3]
HOOK_DIR = ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "hooks"
OPUS = HOOK_DIR / "pm-opus-review-clear-guard.sh"
STATUS = HOOK_DIR / "block-status-todo-without-ready-pool.sh"
CI = HOOK_DIR / "block-ci-rerun-without-local-proof.sh"
MANIFEST = ROOT / "scripts" / "pm" / "shared-assets" / "manifest.json"
INSTALL_RELEASE = ROOT / "scripts" / "install-release.py"


def _run(hook: Path, command: str) -> tuple[bool, str]:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    proc = subprocess.run(
        ["bash", str(hook)], input=payload, text=True, capture_output=True
    )
    stdout = proc.stdout or ""
    combined = stdout + "\n" + (proc.stderr or "")
    blocked = proc.returncode == 2 or "BLOCKED:" in combined
    if '"decision"' in stdout:
        compact = stdout.replace(" ", "").replace("\n", "")
        blocked = blocked or '"decision":"block"' in compact
    return blocked, combined


class PmLabelEditBoundaryTests(unittest.TestCase):
    def test_pm_blocked_label_removal_is_allowed(self) -> None:
        for command in (
            "gh pr edit 123 --remove-label pm-blocked:ci",
            "gh pr edit 123 --remove-label=pm-blocked:capture",
            "gh issue edit 7645 --remove-label pm-blocked:product",
        ):
            blocked, out = _run(OPUS, command)
            self.assertFalse(blocked, f"{command}\n{out}")

    def test_ordinary_label_edits_allowed_across_all_guards(self) -> None:
        commands = (
            "gh pr edit 123 --add-label merge-ready",
            "gh pr edit 123 --remove-label merge-ready",
            "gh issue edit 7645 --add-label status:todo",
            "gh issue edit 7645 --add-label status:in-review",
            "gh issue edit 7645 --add-label priority:high",
            "gh issue edit 7645 --add-label owner:pm",
            "gh pr edit 123 --add-label status:todo,priority:high",
            "gh pr edit 123 --add-label pm-blocked:ci",
        )
        for hook in (OPUS, STATUS, CI):
            for command in commands:
                blocked, out = _run(hook, command)
                self.assertFalse(blocked, f"{hook.name}: {command}\n{out}")

    def test_ci_trigger_denied_with_request_cto_guidance(self) -> None:
        commands = (
            "gh pr edit 123 --add-label pm-state:qa-passed-awaiting-ci",
            "gh pr edit 123 --add-label=pm-state:qa-passed-awaiting-ci",
            "gh run rerun 555",
            "gh workflow run ci.yml",
            "gh api repos/o/r/issues/123/labels -f labels[]=pm-state:qa-passed-awaiting-ci",
        )
        for command in commands:
            blocked, out = _run(CI, command)
            self.assertTrue(blocked, f"{command}\n{out}")
        _, guidance = _run(
            CI, "gh pr edit 123 --add-label pm-state:qa-passed-awaiting-ci"
        )
        self.assertIn("Request the CTO", guidance)

    def test_mixed_trigger_and_ordinary_edit_denied(self) -> None:
        blocked, out = _run(
            CI, "gh pr edit 123 --add-label status:todo,pm-state:qa-passed-awaiting-ci"
        )
        self.assertTrue(blocked, out)

    def test_read_only_and_prose_are_not_blocked(self) -> None:
        commands = (
            "gh pr view 123 --json labels",
            "gh issue view 7645 --json body",
            'echo "remove pm-blocked and arm pm-state:qa-passed-awaiting-ci"',
        )
        for hook in (OPUS, STATUS, CI):
            for command in commands:
                blocked, out = _run(hook, command)
                self.assertFalse(blocked, f"{hook.name}: {command}\n{out}")

    def test_issue_create_schema_guard_intact(self) -> None:
        blocked, out = _run(
            STATUS, "gh issue create --title x --body plain --label status:todo"
        )
        self.assertTrue(blocked, out)

    def test_retired_ceremony_strings_are_gone(self) -> None:
        opus = OPUS.read_text(encoding="utf-8")
        self.assertNotIn("pm-transition.sh", opus)
        self.assertNotIn("PM_CLAUDE_REVIEW: PASS", opus)
        ci = CI.read_text(encoding="utf-8")
        self.assertNotIn("PM-owned wrappers", ci)
        self.assertNotIn("manual pm-blocked:* label edits are no longer allowed", ci)

    def test_manifest_maps_and_source_parity(self) -> None:
        spec = importlib.util.spec_from_file_location("install_release", INSTALL_RELEASE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        manifest = module._load_shared_manifest(ROOT)

        by_source = {entry["source_path"]: entry for entry in manifest["entries"]}
        expected = {
            "claude/hooks/block-ci-rerun-without-local-proof.sh": (
                "/Users/rajiv/.claude/hooks/block-ci-rerun-without-local-proof.sh",
                0o755,
            ),
            "claude/hooks/block-status-todo-without-ready-pool.sh": (
                "/Users/rajiv/Downloads/projects/heydonna-app/.claude/hooks/"
                "block-status-todo-without-ready-pool.sh",
                0o644,
            ),
            "claude/hooks/pm-opus-review-clear-guard.sh": (
                "/Users/rajiv/Downloads/projects/heydonna-app/.claude/hooks/"
                "pm-opus-review-clear-guard.sh",
                0o755,
            ),
        }
        for source, (target, mode) in expected.items():
            entry = by_source[source]
            self.assertEqual(entry["canonical_target"], target)
            path = ROOT / "scripts" / "pm" / "shared-assets" / source
            self.assertTrue(path.is_file())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
            self.assertEqual(
                module.sha256(path),
                entry["sha256"],
                f"manifest digest drift for {source}",
            )


if __name__ == "__main__":
    unittest.main()
