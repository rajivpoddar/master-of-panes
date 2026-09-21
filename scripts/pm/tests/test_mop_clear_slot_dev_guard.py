#!/usr/bin/env python3
"""Contract tests for the mop-clear-slot dev-slot refusal guard.

S1-S6 are cleared only at the new-issue assignment boundary. The wrapper must
refuse a dev-slot clear by default, must not accept --require-terminal as an
acknowledgement, must proceed for a genuine operator clear carrying the explicit
acknowledgement flag, and must leave PM self-clear untouched.

The guard runs BEFORE the MoP health probe, so a refusal is observable with no
server running. A path that clears the guard then fails at the unreachable-MoP
check with exit 30, which is what distinguishes "allowed through" from "refused".
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets"
SCRIPT = SHARED / "claude" / "scripts" / "mop-clear-slot.sh"
SKILL = SHARED / "claude" / "skills" / "heartbeat-tasks" / "SKILL.md"
MANIFEST = SHARED / "manifest.json"
RAW_CLEAR = SHARED / "claude" / "hooks" / "block-raw-clear-outside-mop.sh"

GUARD_FLAG = "--operator-confirm-dev-slot-clear"
DEAD_URL = "http://127.0.0.1:9"  # discard port: the health probe always fails


# The script resolves DB_PATH from MOP_DB_PATH, else from $HOME. Both are pinned
# to a throwaway directory so the pm / 0 / --repair-* cases can never reach the
# LIVE control-plane DB: repair_stale_pm_pending conditionally WRITES there, so a
# harness that inherits the default environment is a data-integrity hazard, not a
# style point.
ISOLATED_HOME = Path(tempfile.mkdtemp(prefix="mop-clear-guard-home-"))


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True, text=True, check=False,
        env={
            "MOP_BASE_URL": DEAD_URL,
            "PATH": "/usr/bin:/bin",
            "HOME": str(ISOLATED_HOME),                          # never the operator's HOME
            "MOP_DB_PATH": str(ISOLATED_HOME / "mop.db"),        # never the live MoP DB
        },
    )


class MopClearSlotDevGuardTests(unittest.TestCase):
    def test_dev_slot_clear_is_refused_by_default(self) -> None:
        for target in ("2", "6", "all"):
            completed = run(target)
            self.assertEqual(completed.returncode, 2, f"{target} must refuse")
            self.assertIn("REFUSED", completed.stderr)
            self.assertIn("mop-assign-slot", completed.stderr, "the refusal must name the sanctioned path")

    def test_require_terminal_does_not_bypass_the_refusal(self) -> None:
        completed = run("--require-terminal", "3")
        self.assertEqual(completed.returncode, 2)
        self.assertIn("REFUSED", completed.stderr)
        self.assertIn("--require-terminal does NOT satisfy", completed.stderr)

    def test_explicit_operator_acknowledgement_passes_the_guard(self) -> None:
        completed = run(GUARD_FLAG, "4")
        self.assertNotIn("REFUSED", completed.stderr, "the acknowledgement must clear the guard")
        self.assertEqual(completed.returncode, 30, "expected to reach and fail the MoP health probe")

    def test_pm_self_clear_is_unaffected(self) -> None:
        for target in ("pm", "0"):
            completed = run(target)
            self.assertNotIn("REFUSED", completed.stderr)
            self.assertEqual(completed.returncode, 30, f"{target} must reach the health probe")

    def test_repair_only_still_works(self) -> None:
        completed = run("--repair-stale-pm-pending-only")
        self.assertNotIn("REFUSED", completed.stderr)

    def test_header_fence_documents_the_operator_only_contract(self) -> None:
        head = SCRIPT.read_text(encoding="utf-8").splitlines()[:20]
        blob = "\n".join(head)
        self.assertIn("OPERATOR-ONLY FOR DEV SLOTS", blob)
        self.assertIn(GUARD_FLAG, blob)
        self.assertIn("mop-assign-slot", blob)

    def test_heartbeat_skill_no_longer_emits_a_dev_slot_clear_or_the_ack(self) -> None:
        skill = SKILL.read_text(encoding="utf-8")
        self.assertNotIn("--require-terminal", skill)
        self.assertNotIn(GUARD_FLAG, skill)
        self.assertNotIn("mop-clear-slot.sh N", skill)
        self.assertIn("mop-clear-slot.sh pm", skill, "the PM self-clear invocation must survive")

    def test_manifest_row_is_mapped_and_current(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        rows = [r for r in manifest["entries"] if r["source_path"] == "claude/scripts/mop-clear-slot.sh"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(
            row["canonical_target"],
            "/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/mop-clear-slot.sh",
        )
        self.assertEqual(row["mode"], 493)
        self.assertEqual(row["dependencies"], [])
        self.assertEqual(row["dependency_status"], "closed")
        self.assertEqual(row["sha256"], hashlib.sha256(SCRIPT.read_bytes()).hexdigest())
        self.assertEqual(manifest["entries"], sorted(manifest["entries"], key=lambda i: i["source_path"]))
        self.assertEqual(manifest["inventory"]["selected_count"], len(manifest["entries"]))


class RawClearHookMessageTests(unittest.TestCase):
    """The /clear-injection hook must not send S1-S6 callers to a bare dev-slot clear."""

    def test_bare_dev_slot_clear_is_no_longer_recommended(self) -> None:
        hook = RAW_CLEAR.read_text(encoding="utf-8")
        self.assertNotIn("mop-clear-slot.sh N.", hook, "the bare dev-slot clear must not be the sanctioned route")

    def test_sanctioned_paths_are_named(self) -> None:
        hook = RAW_CLEAR.read_text(encoding="utf-8")
        self.assertIn("mop-assign-slot", hook, "assignment-boundary clearing must be named")
        self.assertIn("--operator-confirm-dev-slot-clear", hook, "the operator acknowledgement must be named")
        self.assertIn("mop-clear-slot.sh pm", hook, "PM self-clear must stay documented")

    def test_hook_still_blocks_the_injection_class_only(self) -> None:
        hook = RAW_CLEAR.read_text(encoding="utf-8")
        self.assertIn("mcp__plugin_master-of-panes_mop__mop_send_to_slot", hook)
        self.assertIn("tmux[[:space:]]+send-keys", hook)
        # it must not start policing assignment ownership - that is the separate Slice-B hook
        self.assertNotIn("mop-assign-slot.py --slot", hook.split("json_block")[0])

    def test_manifest_row_is_mapped_and_current(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        rows = [r for r in manifest["entries"] if r["source_path"] == "claude/hooks/block-raw-clear-outside-mop.sh"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(
            row["canonical_target"],
            "/Users/rajiv/Downloads/projects/heydonna-app/.claude/hooks/block-raw-clear-outside-mop.sh",
        )
        self.assertEqual(row["mode"], 493)
        self.assertEqual(row["dependency_status"], "closed")
        self.assertEqual(row["sha256"], hashlib.sha256(RAW_CLEAR.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
