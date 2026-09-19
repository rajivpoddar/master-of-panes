from __future__ import annotations

import hashlib
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



    # ── attested slot-less reap + truthful outcome accounting ──────────

    def snapshot_line(self, row: dict[str, object]) -> str:
        return f"{row['pid']} 1 {os.getuid()} /opt/agent-browser --remote-debugging-port=9222\n"

    def test_slotless_candidate_requires_attestation_then_reaps(self) -> None:
        result = self.process_rows("/private/tmp/unbound-agent-browser")
        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertIsNone(candidate["associated_slot"])
        self.assertIsNone(candidate["owner_proof"])
        with patch.object(MODULE, "run", return_value=completed(self.snapshot_line(candidate))), patch.object(
            MODULE, "get_cwd", return_value="/private/tmp/unbound-agent-browser"
        ), patch.object(MODULE, "get_start_time", return_value="Mon Sep 7 00:00:00 2026"), patch.object(
            MODULE, "load_slot_inventory", return_value=(inventory(), None)
        ):
            self.assertEqual(MODULE.revalidate_candidate(candidate), (False, "slotless_reap_not_attested"))
            ok, reason = MODULE.revalidate_candidate(candidate, allow_slotless_attested=True)
            self.assertTrue(ok)
            self.assertEqual(reason, "slotless_attested_identity_match")
            with patch.object(MODULE.os, "kill") as kill, patch.object(MODULE, "alive", return_value=False):
                outcomes = MODULE.kill_candidates([candidate], 0, allow_slotless_attested=True)
        self.assertEqual(outcomes[0]["outcome"], "terminated")
        self.assertEqual(outcomes[0]["status"], "terminated")
        attestation = outcomes[0]["attestation"]
        self.assertEqual(attestation["category"], "agent-browser")
        self.assertTrue(attestation["pid_reuse_verified"])
        self.assertEqual(attestation["basis"], "operator_attested_slotless_reap")
        kill.assert_called_once_with(candidate["pid"], MODULE.signal.SIGTERM)

    def test_attestation_does_not_make_held_checkout_orphan_eligible(self) -> None:
        for held_slot in (5, 6):
            with self.subTest(held_slot=held_slot):
                result = self.process_rows(
                    f"{MODULE.slot_root(held_slot)}/.agent-browser", held_slot=held_slot
                )
                self.assertEqual(result["candidates"], [])
                self.assertEqual(result["skipped"][0]["skip_reason"], "held_or_preserved_slot_checkout")
                with patch.object(MODULE.os, "kill") as kill:
                    self.assertEqual(
                        MODULE.kill_candidates(result["candidates"], 0, allow_slotless_attested=True), []
                    )
                    kill.assert_not_called()

    def test_attested_reap_still_refuses_slot_owner_change(self) -> None:
        # Collected while slot 5 was free+idle, so the row carries the positive
        # owner proof; the slot is then taken again before the signal.
        result = self.process_rows(f"{MODULE.slot_root(5)}/.agent-browser")
        row = result["candidates"][0]
        self.assertEqual(row["associated_slot"], 5)
        self.assertEqual(row["owner_proof"], "mop_free_idle_inactive_no_assignment")
        with patch.object(MODULE, "run", return_value=completed(self.snapshot_line(row))), patch.object(
            MODULE, "get_cwd", return_value=f"{MODULE.slot_root(5)}/.agent-browser"
        ), patch.object(MODULE, "get_start_time", return_value=row["start_time"]), patch.object(
            MODULE, "load_slot_inventory", return_value=(inventory(held_slot=5), None)
        ):
            ok, reason = MODULE.revalidate_candidate(row, allow_slotless_attested=True)
            self.assertFalse(ok)
            self.assertEqual(reason, "slot_owner_evidence_changed")
            with patch.object(MODULE.os, "kill") as kill:
                outcomes = MODULE.kill_candidates([row], 0, allow_slotless_attested=True)
                kill.assert_not_called()
        self.assertEqual(outcomes[0]["outcome"], "refused")
        self.assertEqual(outcomes[0]["status"], "identity_or_owner_refused")
        self.assertNotIn("attestation", outcomes[0])

    def test_apply_artifact_separates_terminated_from_refused(self) -> None:
        def candidate(pid: int) -> dict[str, object]:
            return {
                "pid": pid,
                "category": "agent-browser",
                "command_identity": ("agent-browser", "/opt/agent-browser"),
                "age_seconds": 7200,
                "age": "2h 0m",
                "threshold_min": 60,
                "cwd": "/private/tmp/unbound-agent-browser",
                "start_time": "Mon Sep 7 00:00:00 2026",
                "associated_slot": None,
                "owner_proof": None,
                "slot_free_idle": False,
                "eligible": True,
                "skip_reason": None,
            }

        before = {"ok": True, "candidates": [candidate(4101), candidate(4102)], "rows": [], "skipped": []}
        after = {"ok": True, "candidates": [], "rows": [], "skipped": []}

        def revalidate(row: dict[str, object], allow_slotless_attested: bool = False) -> tuple[bool, str]:
            if row["pid"] == 4102:
                return False, "slot_owner_changed"
            return True, "slotless_attested_identity_match"

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "proof.json"
            with patch.object(MODULE, "load_slot_inventory", return_value=(inventory(), None)), patch.object(
                MODULE, "collect_processes", side_effect=[before, after]
            ), patch.object(MODULE, "revalidate_candidate", side_effect=revalidate), patch.object(
                MODULE, "alive", return_value=False
            ), patch.object(MODULE.os, "kill") as kill, patch.object(
                sys, "argv",
                [
                    "stale-process-cleanup.py",
                    "--apply",
                    "--attest-slotless-reap",
                    "--output",
                    str(output),
                ],
            ), patch.object(sys, "stdout", new_callable=io.StringIO) as stdout:
                code = MODULE.main()
            data = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertEqual([item["pid"] for item in data["terminated"]], [4101])
        self.assertEqual([item["pid"] for item in data["refused"]], [4102])
        self.assertEqual(data["refused"][0]["reason"], "slot_owner_changed")
        self.assertTrue(data["terminated"][0]["attestation"]["pid_reuse_verified"])
        self.assertEqual(data["killed"], data["terminated"])
        self.assertEqual(
            data["summary"],
            {
                "after_candidates": 0,
                "already_exited": 0,
                "candidates": 2,
                "errors": 0,
                "refused": 1,
                "slotless_reap_attested": True,
                "terminated": 1,
            },
        )
        self.assertIn("terminated=1 refused=1", stdout.getvalue())
        self.assertIn("killed=1", stdout.getvalue())
        kill.assert_called_once_with(4101, MODULE.signal.SIGTERM)

    def test_attestation_file_gate_refuses_before_enumeration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "proof.json"
            missing = Path(temp) / "no-such-attestation.txt"
            with patch.object(MODULE, "load_slot_inventory", return_value=(inventory(), None)), patch.object(
                MODULE, "collect_processes", side_effect=AssertionError("must refuse before enumeration")
            ), patch.object(
                sys,
                "argv",
                [
                    "stale-process-cleanup.py",
                    "--apply",
                    "--attest-slotless-reap",
                    "--attestation-file",
                    str(missing),
                    "--output",
                    str(output),
                ],
            ), patch.object(sys, "stdout", new_callable=io.StringIO) as stdout:
                code = MODULE.main()
            self.assertEqual(code, 2)
            self.assertIn("attestation_invalid:missing:", stdout.getvalue())
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8"))["before"]["candidates"], []
            )
            # An attestation file without the enabling attestation is refused too.
            with patch.object(MODULE, "load_slot_inventory", return_value=(inventory(), None)), patch.object(
                MODULE, "collect_processes", side_effect=AssertionError("must refuse before enumeration")
            ), patch.object(
                sys,
                "argv",
                ["stale-process-cleanup.py", "--apply", "--attestation-file", str(missing), "--output", str(output)],
            ), patch.object(sys, "stdout", new_callable=io.StringIO) as stdout:
                code = MODULE.main()
            self.assertEqual(code, 2)
            self.assertIn("attestation_file_requires_attest_slotless_reap", stdout.getvalue())

    def test_attestation_file_is_fingerprinted(self) -> None:
        text = "category=agent-browser\nage=2h\npid-reuse=verified\n"
        with tempfile.TemporaryDirectory() as temp:
            attestation = Path(temp) / "attestation.txt"
            attestation.write_text(text, encoding="utf-8")
            metadata, error = MODULE.attestation_metadata(attestation)
        self.assertIsNone(error)
        self.assertEqual(metadata["bytes"], len(text.encode("utf-8")))
        self.assertEqual(metadata["sha256"], hashlib.sha256(text.encode("utf-8")).hexdigest())


if __name__ == "__main__":
    unittest.main()
