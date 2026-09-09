from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).parents[3]
MODULE_PATH = ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "scripts" / "stale-process-cleanup.py"
SPEC = importlib.util.spec_from_file_location("stale_process_cleanup", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["fixture"], returncode, stdout, stderr)


def inventory(*, held_slot: int | None = None) -> dict[int, dict[str, object]]:
    result: dict[int, dict[str, object]] = {}
    for slot in MODULE.SLOT_NUMBERS:
        held = slot == held_slot
        result[slot] = {
            "slot": slot,
            "root": MODULE.slot_root(slot),
            "status": "active" if held else "free",
            "occupied": 1 if held else 0,
            "idle": 0 if held else 1,
            "dnd": 0,
            "activity": None,
            "issue": "681" if held else None,
            "task": "held-cutover" if held else None,
            "repository_id": "rajivpoddar/heydonna-app" if held else None,
            "branch": "held" if held else None,
            "branch_ref": "held" if held else None,
            "pr": "7655" if held else None,
            "head_sha": "abc" if held else None,
            "work_kind": "repro" if held else None,
            "handoff_id": "hold-1" if held else None,
            "claimed_at": "2026-09-07T00:00:00Z" if held else None,
            "active_turn_state": "active" if held else "inactive",
            "active_turn_id": "turn-1" if held else None,
            "last_activity": "2026-09-07T00:00:00Z",
            "free_idle": not held,
        }
    return result


class StaleProcessCleanupTests(unittest.TestCase):
    def test_inventory_parses_structured_multiline_slot_rows(self) -> None:
        records = []
        for slot in MODULE.SLOT_NUMBERS:
            held = slot == 6
            records.append(
                {
                    "slot": slot,
                    "status": "active" if held else "free",
                    "occupied": 1 if held else 0,
                    "idle": 0 if held else 1,
                    "dnd": 0,
                    "activity": None,
                    "issue": "681" if held else None,
                    "task": "held line 1\nheld line 2" if held else None,
                    "repository_id": None,
                    "branch": None,
                    "branch_ref": None,
                    "pr": None,
                    "head_sha": None,
                    "work_kind": None,
                    "handoff_id": None,
                    "claimed_at": None,
                    "active_turn_state": "active" if held else "inactive",
                    "active_turn_id": "turn-6" if held else None,
                    "last_activity": None,
                }
            )
        with tempfile.TemporaryDirectory() as temp:
            original_db = MODULE.MOP_DB
            MODULE.MOP_DB = Path(temp) / "mop.db"
            MODULE.MOP_DB.touch()
            try:
                with patch.object(MODULE, "run", return_value=completed(json.dumps(records))):
                    states, error = MODULE.load_slot_inventory()
            finally:
                MODULE.MOP_DB = original_db
        self.assertIsNone(error)
        self.assertEqual(len(states), 6)
        self.assertEqual(states[6]["task"], "held line 1\nheld line 2")

    def process_rows(self, cwd: str, *, held_slot: int = 6) -> dict[str, object]:
        command = "/opt/agent-browser --remote-debugging-port=9222"
        ps = f"{os.getpid() + 1000} 1 {os.getuid()} 2-00:00:00 {command}\n"
        states = inventory(held_slot=held_slot)
        with patch.object(MODULE, "run", return_value=completed(ps)), patch.object(
            MODULE, "get_cwd", return_value=cwd
        ), patch.object(MODULE, "get_start_time", return_value="Mon Sep 7 00:00:00 2026"):
            return MODULE.collect_processes(60, 360, 30, (states, None))

    def test_held_checkout_orphan_is_refused_before_kill_boundary(self) -> None:
        for held_slot in (5, 6):
            with self.subTest(held_slot=held_slot):
                result = self.process_rows(
                    f"/Users/rajiv/Downloads/projects/heydonna-app-300{held_slot}/.agent-browser",
                    held_slot=held_slot,
                )
                self.assertEqual(result["candidates"], [])
                self.assertEqual(len(result["skipped"]), 1)
                self.assertEqual(result["skipped"][0]["skip_reason"], "held_or_preserved_slot_checkout")
                with patch.object(MODULE.os, "kill") as kill:
                    self.assertEqual(MODULE.kill_candidates(result["candidates"], 0), [])
                    kill.assert_not_called()

    def test_unbound_stale_process_outside_slot_checkout_remains_eligible(self) -> None:
        result = self.process_rows("/private/tmp/unbound-agent-browser")
        self.assertEqual(len(result["candidates"]), 1)
        with patch.object(MODULE, "revalidate_candidate", return_value=(True, "identity_and_owner_match")), patch.object(
            MODULE, "alive", return_value=False
        ), patch.object(MODULE.os, "kill") as kill:
            killed = MODULE.kill_candidates(result["candidates"], 0)
        self.assertEqual(killed[0]["status"], "terminated")
        kill.assert_called_once_with(result["candidates"][0]["pid"], MODULE.signal.SIGTERM)

    def test_owner_identity_drift_refuses_without_kill(self) -> None:
        result = self.process_rows("/private/tmp/unbound-agent-browser")
        candidate = result["candidates"]
        with patch.object(MODULE, "revalidate_candidate", return_value=(False, "process_identity_changed:cwd")), patch.object(
            MODULE.os, "kill"
        ) as kill:
            refused = MODULE.kill_candidates(candidate, 0)
        self.assertEqual(refused[0]["status"], "identity_or_owner_refused")
        self.assertEqual(refused[0]["reason"], "process_identity_changed:cwd")
        kill.assert_not_called()

    def test_apply_refuses_before_process_enumeration_when_inventory_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "proof.json"
            with patch.object(MODULE, "load_slot_inventory", return_value=({}, "read_failed:fixture")), patch.object(
                MODULE, "collect_processes", side_effect=AssertionError("must refuse before enumeration")
            ), patch.object(sys, "argv", ["stale-process-cleanup.py", "--apply", "--output", str(output)]), patch.object(
                sys, "stdout", new_callable=io.StringIO
            ) as stdout:
                code = MODULE.main()
            self.assertEqual(code, 2)
            self.assertIn("slot_inventory_unavailable:read_failed:fixture", stdout.getvalue())
            self.assertEqual(__import__("json").loads(output.read_text(encoding="utf-8"))["before"]["candidates"], [])


if __name__ == "__main__":
    unittest.main()
