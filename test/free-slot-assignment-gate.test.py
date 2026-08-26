#!/usr/bin/env python3
"""Focused contract tests for the release-owned free-slot gate."""

from __future__ import annotations

import importlib.util
import stat
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "mop_free_slot_assignment_gate", ROOT / "scripts" / "ready-pool-assignment-gate.py"
)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def pr_row(*, head: str, number: int = 7516, branch: str = "fix/7516") -> dict[str, object]:
    return {
        "number": number,
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "headRefName": branch,
        "headRefOid": head,
        "updatedAt": "2026-08-26T17:00:00Z",
        "labels": [],
    }


class FreeSlotAssignmentGateTests(unittest.TestCase):
    def test_ready_pool_candidates_reject_blocking_labels_and_invalid_rows(self) -> None:
        original_cache = gate.TODO_SCAN_CACHE
        original_run = gate.run_gh_json
        with tempfile.TemporaryDirectory() as directory:
            gate.TODO_SCAN_CACHE = Path(directory) / "todo-cache.json"
            gate.run_gh_json = lambda *_args, **_kwargs: [
                {"number": 7517, "title": "blocked", "labels": [{"name": "status:todo"}, {"name": "pm-blocked"}]},
                {"number": 7518, "title": "dependency blocked", "labels": [{"name": "status:todo"}, {"name": "dependency-blocked"}]},
                {"number": 7519, "title": "ready", "labels": [{"name": "status:todo"}, {"name": "P2"}]},
                {"number": 7520, "title": "", "labels": [{"name": "status:todo"}]},
            ]
            try:
                candidates, error = gate.github_todo_candidates(set())
            finally:
                gate.TODO_SCAN_CACHE = original_cache
                gate.run_gh_json = original_run
        self.assertIsNone(error)
        self.assertEqual([candidate["issue"] for candidate in candidates], [7519])

    def test_exact_head_obligation_joins_live_pr_identity(self) -> None:
        head = "a" * 40
        boundaries = [{
            "obligation_id": 12,
            "pr": 7516,
            "issue": 7516,
            "head_sha": head,
            "packet_id": "packet-7516",
            "handoff_id": "handoff-7516",
            "wake_condition": "e2e",
            "spec": "e2e",
            "dedup": "dedup-7516",
        }]
        self.assertEqual(
            len(gate.build_authoritative_slot_e2e_candidates([pr_row(head=head)], boundaries)),
            1,
        )
        self.assertEqual(
            gate.build_authoritative_slot_e2e_candidates([pr_row(head="b" * 40)], boundaries),
            [],
        )

    def test_existing_pr_boundary_outranks_ready_pool_todo(self) -> None:
        result = gate.inspect_gate(
            1,
            set(),
            set(),
            [],
            [{"issue": 7517, "priority": 3, "updated_at": "2026-08-26T17:00:00Z"}],
            [{"obligation_id": 12, "pr": 7516, "issue": 7516, "packet_id": "p"}],
        )
        self.assertTrue(result["allowed"])
        self.assertEqual(result["recommendation_kind"], "slot_e2e")
        self.assertEqual(result["recommended_pr"], 7516)

    def test_empty_authoritative_audit_selects_unblocked_todo(self) -> None:
        result = gate.inspect_gate(
            1,
            set(),
            set(),
            [],
            [{"issue": 7518, "priority": 3, "updated_at": "2026-08-26T17:00:00Z"}],
            [],
        )
        self.assertTrue(result["allowed"])
        self.assertEqual(result["recommendation_kind"], "todo")
        self.assertEqual(result["recommended_issue"], 7518)

    def test_dependency_blocked_todo_is_excluded(self) -> None:
        result = gate.inspect_gate(
            1,
            set(),
            {7517},
            [],
            [{"issue": 7517, "priority": 3, "updated_at": "2026-08-26T17:00:00Z"}],
            [],
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "no_assignable_obligation")

    def test_occupied_pr_exclusion_is_authoritative(self) -> None:
        result = gate.inspect_gate(
            1,
            {7516},
            set(),
            [],
            [],
            [{"obligation_id": 12, "pr": 7516, "issue": 7516, "packet_id": "p"}],
        )
        self.assertFalse(result["allowed"])

    def test_unavailable_authoritative_source_fails_closed(self) -> None:
        result = gate.inspect_gate(1, set(), set(), [], [], [], "authoritative_boundary_unavailable:missing_db")
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "authoritative_boundary_audit_incomplete")
        self.assertEqual(result["boundary_audit_reason"], "authoritative_boundary_unavailable:missing_db")

    def test_missing_obligation_database_is_typed_and_read_only(self) -> None:
        original = gate.PM_OPS_DB
        with tempfile.TemporaryDirectory() as directory:
            gate.PM_OPS_DB = Path(directory) / "missing.db"
            boundaries, error = gate._read_authoritative_slot_boundaries()
        gate.PM_OPS_DB = original
        self.assertEqual(boundaries, [])
        self.assertIn("authoritative_boundary_unavailable:missing_db", error or "")

    def test_release_owned_path_and_executable_mode(self) -> None:
        path = ROOT / "scripts" / "ready-pool-assignment-gate.py"
        self.assertTrue(path.is_file())
        self.assertTrue(stat.S_IMODE(path.stat().st_mode) & 0o111)


if __name__ == "__main__":
    unittest.main()
