from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).parents[3]
MANIFEST_PATH = REPO_ROOT / "scripts" / "pm" / "shared-assets" / "manifest.json"
CLI = REPO_ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "scripts" / "mop.py"
WRAPPER = REPO_ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "scripts" / "mop"
HOOK = REPO_ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "hooks" / "block-raw-clear-outside-mop.sh"

SKILL_FILES = [
    "scripts/pm/shared-assets/claude/skills/direct-assign/SKILL.md",
    "scripts/pm/shared-assets/claude/skills/direct-release/SKILL.md",
    "scripts/pm/shared-assets/claude/skills/cleanup-pr/SKILL.md",
    "scripts/pm/shared-assets/claude/skills/session-age-clear/SKILL.md",
    "scripts/pm/shared-assets/claude/skills/heartbeat-tasks/SKILL.md",
    "scripts/pm/shared-assets/claude/skills/todo-prioritize/SKILL.md",
]

MCP_TOOLS = [
    "mop_send_to_slot", "mop_release_slot", "mop_clear_slot", "mop_approve_plan",
    "mop_slot_status", "mop_all_slots", "mop_slot_history", "mop_recent_activity",
    "mop_capture_output", "mop_stream_slot", "mop_set_dnd", "mop_set_exit_pending",
    "mop_exit_status", "mop_clear_all_slots", "mop_respawn_slot", "mop_ops_audit",
    "mop_pm_cadence", "mcp__plugin_master-of-panes",
]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MopCliContractTest(unittest.TestCase):
    def test_manifest_rows_match_blobs(self):
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        rows = {r["source_path"]: r for r in manifest["entries"]}
        for source, target in [
            ("claude/scripts/mop.py", "/Users/rajiv/.claude/scripts/mop.py"),
            ("claude/scripts/mop", "/Users/rajiv/.claude/scripts/mop"),
        ]:
            row = rows[source]
            self.assertEqual(row["canonical_target"], target, source)
            self.assertEqual(row["sha256"], digest(REPO_ROOT / "scripts" / "pm" / "shared-assets" / source), source)
            self.assertEqual(row["mode"], 493, source)
        self.assertEqual(manifest["inventory"]["selected_count"], len(manifest["entries"]))

    def test_cli_help_lists_rest_parity_subcommands(self):
        proc = subprocess.run([sys.executable, str(CLI), "--help"], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0)
        for sub in ["status", "send", "release", "clear", "dnd", "exit-pending",
                    "exit-status", "capture", "approve-plan", "respawn"]:
            self.assertIn(sub, proc.stdout, sub)

    def test_hook_matches_cli_and_parses(self):
        text = HOOK.read_text(encoding="utf-8")
        self.assertIn("mcp__plugin_master-of-panes_mop__mop_send_to_slot", text)
        self.assertIn("mop", text)
        proc = subprocess.run(["bash", "-n", str(HOOK)], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_skills_name_cli_not_mcp_tools(self):
        for rel in SKILL_FILES:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            for tool in MCP_TOOLS:
                self.assertNotIn(tool, text, f"{rel} references {tool}")


if __name__ == "__main__":
    unittest.main()
