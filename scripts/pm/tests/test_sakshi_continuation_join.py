from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "shared-assets" / "claude" / "scripts" / "pm" / "control-plane" / "sakshi-heartbeat.py"
SPEC = importlib.util.spec_from_file_location("sakshi_heartbeat", SOURCE)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules["sakshi_heartbeat"] = MODULE
runtime_observation = types.ModuleType("control_plane.runtime_observation")
runtime_observation.RuntimeObservationAdapter = object
runtime_observation.parse_timestamp = lambda value: value
control_plane = types.ModuleType("control_plane")
control_plane.runtime_observation = runtime_observation
sys.modules["control_plane"] = control_plane
sys.modules["control_plane.runtime_observation"] = runtime_observation
SPEC.loader.exec_module(MODULE)


HEAD = "f109414c02cc296510103fe2c090ce964e9b9dfb"
NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def pr() -> dict:
    return {
        "number": 7591,
        "head": {"sha": HEAD, "ref": "fix/7591"},
        "headRefOid": HEAD,
        "headRefName": "fix/7591",
    }


def running_run(workflow: str, event: str = "pull_request") -> tuple[list[dict], dict[str, list[dict]]]:
    run = {
        "id": 33397393224,
        "head_sha": HEAD,
        "workflowName": workflow,
        "event": event,
        "status": "in_progress",
        "conclusion": None,
        "created_at": "2026-09-01T00:00:00Z",
        "run_attempt": 1,
    }
    job = {
        "id": 991,
        "status": "in_progress",
        "runner_id": 22,
        "steps": [{"name": "test", "status": "in_progress"}],
    }
    return [run], {"33397393224": [job]}


def completed_run(
    workflow: str,
    *,
    run_id: int = 33397393224,
    conclusion: str = "success",
    head: str = HEAD,
    event: str = "pull_request",
) -> dict:
    return {
        "id": run_id,
        "head_sha": head,
        "workflowName": workflow,
        "event": event,
        "status": "completed",
        "conclusion": conclusion,
        "created_at": "2026-09-01T00:00:00Z",
        "run_attempt": 1,
    }


def collect_audit(
    test: unittest.TestCase,
    *,
    runs: list[dict] | None = None,
    jobs: dict[str, list[dict]] | None = None,
    slots: dict[str, dict] | None = None,
    records: list[dict] | None = None,
    continuation_error: str | None = None,
    pr_payload: dict | None = None,
    queue: dict | None = None,
) -> dict:
    runs = runs or []
    jobs = jobs or {}
    pr_payload = pr_payload or pr()

    def fake_audit(args):
        if any("/pulls" in value for value in args):
            return ([pr_payload], None)
        if any("/actions/runs" in value for value in args) and not any("/jobs" in value for value in args):
            return ({"workflow_runs": runs}, None)
        jobs_endpoint = next(value for value in args if "/jobs" in value)
        run_id = jobs_endpoint.rsplit("/", 2)[-2]
        return ({"jobs": jobs.get(run_id, [])}, None)

    with mock.patch.object(MODULE, "_audit_gh_json", side_effect=fake_audit), mock.patch.object(
        MODULE,
        "_load_open_pr_continuations",
        return_value=(records or [], continuation_error),
    ):
        return MODULE.collect_open_pr_activity_audit(slots or {}, queue)


def continuation(kind: str, owner: str = "cto", *, blocker: str = "hold") -> dict:
    lane = MODULE.CONTINUATION_KIND_LANES[kind]
    return {
        "id": "15912",
        "kind": kind,
        "owner": owner,
        "required_action": "consume the exact-head continuation at the next safe boundary",
        "blocker": blocker,
        "evidence_json": json.dumps({"head": HEAD}),
        "lane": lane,
        "next_action": "consume the exact-head continuation at the next safe boundary",
        "wake": "consume the exact-head continuation at the next safe boundary",
        "hold_reason": blocker,
        "next_owner": owner,
        "owner_source": "pm-ops.obligations",
        "workflow_motion": "durable:" + kind,
        "blocker_class": blocker,
        "head": HEAD,
    }


class SakshiContinuationJoinTests(unittest.TestCase):
    def evaluate(self, runs=None, jobs=None, slots=None, records=None):
        return MODULE.evaluate_open_pr_activity(
            pr(),
            runs or [],
            jobs or {},
            slots or {},
            now_utc=NOW,
            continuation_records=records or [],
        )

    def test_source_joins_durable_records_instead_of_pr_synthetic_owner_fields(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("_blocked_motion_metadata", source)
        self.assertIn("_load_open_pr_continuations", source)
        self.assertIn("pm-ops.obligations", source)
        row = self.evaluate(records=[continuation("dependency_wait")])
        self.assertEqual(row["lane"], "dependency-blocked")
        self.assertEqual(row["owner"], "cto")
        self.assertEqual(row["owner_source"], "pm-ops.obligations")
        self.assertEqual(row["next_owner"], "cto")
        self.assertIn("exact-head continuation", row["next_action"])

    def test_pr_identity_rejects_ambiguous_or_partial_heads(self) -> None:
        identity, error = MODULE._exact_open_pr_identity({
            "number": 7591,
            "head": {"sha": "0" * 40, "ref": "fix/7591"},
            "headRefOid": HEAD,
            "headRefName": "fix/7591",
        })
        self.assertIsNone(identity)
        self.assertIn("ambiguous", error or "")
        identity, error = MODULE._exact_open_pr_identity({
            "number": 7591,
            "head": {"ref": "fix/7591"},
            "headRefName": "fix/7591",
        })
        self.assertIsNone(identity)
        self.assertIn("full 40-character", error or "")

    def test_collection_marks_missing_identity_not_clear(self) -> None:
        with mock.patch.object(
            MODULE,
            "_audit_gh_json",
            return_value=([{"number": 7591, "headRefName": "fix/7591"}], None),
        ):
            audit = MODULE.collect_open_pr_activity_audit({})
        self.assertTrue(audit["ok"])
        self.assertEqual(audit["open_pr_activity_gaps"], 0)
        self.assertEqual(audit["gaps"], [])
        self.assertTrue(audit["rows"][0]["verification_limited"])
        self.assertFalse(audit["rows"][0]["unbound"])
        self.assertEqual(audit["rows"][0]["motion_state"], "UNKNOWN")
        self.assertEqual(audit["rows"][0]["lane"], "unknown")
        self.assertIn("missing", audit["rows"][0]["hold_reason"])

    def test_binding_outcome_flags_only_readable_absence_of_ci_and_slot(self) -> None:
        stale_head = "a" * 40
        queued_ci = {
            **completed_run("CI", run_id=401),
            "status": "queued",
            "conclusion": None,
        }
        active_ci, active_jobs = running_run("CI")
        active_slot = {
            "pr": str(pr()["number"]),
            "head_sha": HEAD,
            "occupied": True,
            "active_turn_state": "active",
            "active_turn_id": "turn-1",
            "owner": "S2",
            "task": "production-shaped repro",
        }
        cases = [
            ("queued current-head CI", [queued_ci], {}, {}, [], None, "ci_bound", False),
            ("active numbered exact-head slot", [], {}, {"2": active_slot}, [], None, "slot_bound", False),
            ("named queued slot", [], {}, {}, [continuation("slot_rework", "slot-owner")], None, "slot_bound", False),
            (
                "stale, skipped, and dummy runs",
                [
                    completed_run("CI", run_id=402, head=stale_head),
                    {**completed_run("CI", run_id=403), "conclusion": "skipped"},
                    completed_run("test", run_id=404),
                ],
                {}, {}, [], None, "unbound", True,
            ),
            ("neither binding", [], {}, {}, [], None, "unbound", True),
            (
                "malformed ledger with valid CI and slot",
                active_ci,
                active_jobs,
                {"2": active_slot},
                [],
                "row: durable continuation has no exact head binding",
                "ci_and_slot_bound",
                False,
            ),
        ]
        for name, runs, jobs, slots, records, continuation_error, expected_status, expected_unbound in cases:
            with self.subTest(name=name):
                audit = collect_audit(
                    self,
                    runs=runs,
                    jobs=jobs,
                    slots=slots,
                    records=records,
                    continuation_error=continuation_error,
                )
                self.assertTrue(audit["ok"])
                row = audit["rows"][0]
                self.assertEqual(row["binding_status"], expected_status)
                self.assertEqual(row["unbound"], expected_unbound)
                self.assertEqual(audit["open_pr_activity_gaps"], int(expected_unbound))
                self.assertEqual(len(audit["gaps"]), int(expected_unbound))
                if expected_unbound:
                    self.assertEqual(row["binding_missing"], ["ci", "slot"])
                    self.assertIn("no authoritative current-head CI or executable slot binding", row["reasons"])
                    self.assertTrue(MODULE.open_pr_activity_action_lines(audit))
                else:
                    self.assertFalse(MODULE.open_pr_activity_action_lines(audit))

        malformed_bound = collect_audit(
            self,
            runs=active_ci,
            jobs=active_jobs,
            slots={"2": active_slot},
            continuation_error="row: durable continuation has no exact head binding",
        )
        self.assertEqual(len(malformed_bound["verification_limitations"]), 1)
        self.assertEqual(malformed_bound["gaps"], [])

    def test_exact_packet_task_identity_binds_null_structured_slot_fields(self) -> None:
        slot = {
            "slot": 1,
            "name": "Rohini",
            "occupied": True,
            "active_turn_state": "inactive",
            "task": (
                "REPRO — Packet 635 (#7591 / issue #7435, accepted next step).\n"
                f"Exact head {HEAD}.\n"
                "Precheck before the single run."
            ),
            "pr": None,
            "head_sha": None,
        }
        audit = collect_audit(self, slots={"1": slot})
        row = audit["rows"][0]
        self.assertEqual(row["binding_status"], "slot_bound")
        self.assertFalse(row["unbound"])
        self.assertIn("slot:S1:Rohini:packet=635", row["binding_evidence"])
        self.assertEqual(MODULE.open_pr_activity_action_lines(audit), [])
        malformed_sibling = collect_audit(
            self,
            slots={"1": slot},
            continuation_error="row: durable continuation has no exact head binding",
        )
        self.assertEqual(malformed_sibling["rows"][0]["binding_status"], "slot_bound")
        self.assertFalse(malformed_sibling["rows"][0]["unbound"])

    def test_exact_named_queue_packet_binds_and_stale_packet_does_not(self) -> None:
        queue = {
            "packet_waiting_no_free_slot": [{
                "pr": "7591", "head": HEAD, "slot": "4", "packet": "637",
                "branch": "fix/7591", "reason": "capacity",
            }],
        }
        bound = collect_audit(self, queue=queue)
        self.assertEqual(bound["rows"][0]["binding_status"], "slot_bound")
        self.assertIn("slot-queue:S4:S4:packet=637", bound["rows"][0]["binding_evidence"])

        stale = collect_audit(self, queue={
            "packet_waiting_no_free_slot": [{
                "pr": "7591", "head": "a" * 40, "slot": "4", "packet": "637",
                "reason": "capacity",
            }],
        })
        self.assertEqual(stale["rows"][0]["binding_status"], "unbound")
        self.assertTrue(stale["rows"][0]["unbound"])

    def test_issue_only_prompt_and_prose_head_do_not_create_slot_binding(self) -> None:
        slot = {
            "slot": 2,
            "name": "Hasta",
            "occupied": True,
            "task": f"Continue PR #7591 at head {HEAD}; no packet was accepted.",
            "pr": None,
            "head_sha": None,
        }
        audit = collect_audit(self, slots={"2": slot})
        self.assertEqual(audit["rows"][0]["binding_status"], "unbound")
        self.assertTrue(audit["rows"][0]["unbound"])

    def test_conflicting_structured_slot_identity_is_nonbinding_and_actionable(self) -> None:
        current_head = "a" * 40
        stale_head = "b" * 40
        slot = {
            "slot": 1,
            "name": "Rohini",
            "occupied": True,
            "active_turn_state": "inactive",
            "pr": "7629",
            "head_sha": current_head,
            "task": (
                "REPRO — Packet 635 (#7591 / issue #7435, retained packet).\n"
                f"Exact head {stale_head}."
            ),
        }
        audit = collect_audit(
            self,
            slots={"1": slot},
            pr_payload={
                "number": 7629,
                "head": {"sha": current_head, "ref": "fix/7629"},
                "headRefOid": current_head,
                "headRefName": "fix/7629",
            },
        )
        row = audit["rows"][0]
        self.assertEqual(row["binding_status"], "unbound")
        self.assertTrue(row["unbound"])
        self.assertTrue(row["verification_limited"])
        self.assertTrue(any("conflicts" in item for item in row["binding_limitations"]))
        actions = MODULE.open_pr_activity_action_lines(audit)
        self.assertTrue(actions)
        self.assertIn("PR #7629", actions[1])

    def test_absent_structured_identity_still_uses_rigid_packet_fallback(self) -> None:
        slot = {
            "slot": 1,
            "name": "Rohini",
            "occupied": True,
            "active_turn_state": "inactive",
            "pr": None,
            "head_sha": None,
            "task": f"REPRO — Packet 635 (#7591 / issue #7435). Exact head {HEAD}.",
        }
        audit = collect_audit(self, slots={"1": slot})
        self.assertEqual(audit["rows"][0]["binding_status"], "slot_bound")
        self.assertFalse(audit["rows"][0]["unbound"])

    def test_active_exact_packet_precedes_malformed_ledger_and_counts_motion(self) -> None:
        slot = {
            "slot": 1,
            "name": "Rohini",
            "occupied": True,
            "active_turn_state": "active",
            "active_turn_id": "turn-636",
            "pr": None,
            "head_sha": None,
            "task": f"REPRO — Packet 636 (#7591 / issue #344). Exact head {HEAD}.",
        }
        audit = collect_audit(
            self,
            slots={"1": slot},
            continuation_error="row: durable continuation has no exact head binding",
        )
        row = audit["rows"][0]
        self.assertEqual(row["motion_state"], "REPRO_REWORK_IN_PROGRESS")
        self.assertEqual(row["owner"], "Rohini")
        self.assertEqual(row["next_action"], "await the exact-head numbered packet terminal")
        self.assertEqual(audit["counts"]["numbered_reproduction"], 1)
        self.assertEqual(audit["motion_states"]["REPRO_REWORK_IN_PROGRESS"], 1)
        self.assertEqual(MODULE.open_pr_activity_action_lines(audit), [])
        self.assertTrue(any("durable continuation has no exact head binding" in reason for reason in row["reasons"]))

    def test_held_exact_packet_precedes_ledger_action_without_claiming_execution(self) -> None:
        slot = {
            "slot": 1,
            "name": "Rohini",
            "occupied": True,
            "active_turn_state": "inactive",
            "pr": None,
            "head_sha": None,
            "task": f"REPRO — Packet 636 (#7591 / issue #344). Exact head {HEAD}.",
        }
        audit = collect_audit(
            self,
            slots={"1": slot},
            continuation_error="row: durable continuation has no exact head binding",
        )
        row = audit["rows"][0]
        self.assertEqual(row["motion_state"], "REPRO_REWORK_QUEUED")
        self.assertEqual(row["owner"], "Rohini")
        self.assertIn("packet 636", row["next_action"])
        self.assertEqual(audit["counts"]["numbered_reproduction"], 0)
        self.assertEqual(audit["motion_states"]["REPRO_REWORK_QUEUED"], 1)
        self.assertEqual(MODULE.open_pr_activity_action_lines(audit), [])

    def test_exact_packet_survives_ambiguous_ci_chronology(self) -> None:
        runs = [
            completed_run("CI", run_id=601),
            completed_run("CI", run_id=602),
        ]
        slot = {
            "slot": 1,
            "name": "Rohini",
            "occupied": True,
            "active_turn_state": "active",
            "active_turn_id": "turn-636",
            "pr": None,
            "head_sha": None,
            "task": f"REPRO — Packet 636 (#7591 / issue #344). Exact head {HEAD}.",
        }
        audit = collect_audit(self, runs=runs, slots={"1": slot})
        row = audit["rows"][0]
        self.assertEqual(row["motion_state"], "REPRO_REWORK_IN_PROGRESS")
        self.assertEqual(row["binding_status"], "slot_bound")
        self.assertEqual(audit["counts"]["numbered_reproduction"], 1)
        self.assertTrue(any("chronology" in reason for reason in row["reasons"]))

    def test_ambiguous_ci_chronology_without_packet_stays_unknown(self) -> None:
        audit = collect_audit(
            self,
            runs=[
                completed_run("CI", run_id=601),
                completed_run("CI", run_id=602),
            ],
        )
        row = audit["rows"][0]
        self.assertEqual(row["motion_state"], "UNKNOWN")
        self.assertEqual(row["binding_status"], "unknown")
        self.assertFalse(row["unbound"])
        self.assertTrue(row["verification_limited"])
        self.assertEqual(MODULE.open_pr_activity_action_lines(audit), [])

    def test_terminal_or_missing_required_ci_is_actionable_in_report(self) -> None:
        cases = [
            ("exact-head failed CI", [completed_run("CI", run_id=501, conclusion="failure")]),
            ("exact-head cancelled CI", [completed_run("CI", run_id=502, conclusion="cancelled")]),
            ("green CI without required E2E", [completed_run("CI", run_id=503)]),
        ]
        for name, runs in cases:
            with self.subTest(name=name):
                audit = collect_audit(self, runs=runs)
                self.assertTrue(audit["ok"])
                row = audit["rows"][0]
                self.assertEqual(row["motion_state"], "PROCESS_LIMBO")
                self.assertEqual(row["binding_status"], "unbound")
                self.assertTrue(row["unbound"])
                self.assertEqual(audit["open_pr_activity_gaps"], 1)
                self.assertEqual(len(audit["gaps"]), 1)
                actions = MODULE.open_pr_activity_action_lines(audit)
                self.assertTrue(actions)
                self.assertIn("neither authoritative current-head CI nor executable slot binding", actions[0])

    def test_dual_green_and_queued_required_ci_are_reported_as_bound(self) -> None:
        dual_green = collect_audit(
            self,
            runs=[
                completed_run("CI", run_id=504),
                completed_run("E2E Smoke Tests", run_id=505),
            ],
            pr_payload={**pr(), "labels": [{"name": "merge-ready"}]},
        )
        self.assertEqual(dual_green["rows"][0]["motion_state"], "MERGE_READY")
        self.assertEqual(dual_green["rows"][0]["binding_status"], "ci_bound")
        self.assertFalse(dual_green["rows"][0]["unbound"])
        self.assertEqual(dual_green["gaps"], [])
        self.assertEqual(MODULE.open_pr_activity_action_lines(dual_green), [])

        queued = {
            **completed_run("CI", run_id=506),
            "status": "queued",
            "conclusion": None,
        }
        queued_audit = collect_audit(self, runs=[queued])
        self.assertEqual(queued_audit["rows"][0]["binding_status"], "ci_bound")
        self.assertFalse(queued_audit["rows"][0]["unbound"])
        self.assertEqual(MODULE.open_pr_activity_action_lines(queued_audit), [])

    def test_collection_refuses_duplicate_open_pr_identity(self) -> None:
        with mock.patch.object(
            MODULE,
            "_audit_gh_json",
            side_effect=[([pr(), pr()], None), ({"workflow_runs": []}, None), ({"workflow_runs": []}, None)],
        ), mock.patch.object(MODULE, "_load_open_pr_continuations", return_value=([], None)):
            audit = MODULE.collect_open_pr_activity_audit({})
        self.assertFalse(audit["ok"])
        self.assertIn("ambiguous duplicate", audit["error"])

    def test_headless_continuation_is_row_local_and_preserves_other_open_prs(self) -> None:
        other_head = "a" * 40
        malformed_pr = {
            "number": 7594,
            "head": {"sha": other_head, "ref": "fix/7594"},
            "headRefOid": other_head,
            "headRefName": "fix/7594",
        }
        with mock.patch.object(
            MODULE,
            "_audit_gh_json",
            side_effect=[
                ([malformed_pr, pr()], None),
                ({"workflow_runs": []}, None),
                ({"workflow_runs": []}, None),
            ],
        ), mock.patch.object(
            MODULE,
            "_load_open_pr_continuations",
            side_effect=[
                ([], "row: durable continuation has no exact head binding"),
                ([continuation("dependency_wait")], None),
            ],
        ):
            audit = MODULE.collect_open_pr_activity_audit({})
        self.assertTrue(audit["ok"])
        self.assertEqual(audit["open_pr_count"], 2)
        self.assertEqual(len(audit["rows"]), 2)
        rows = {row["pr"]: row for row in audit["rows"]}
        malformed = rows["7594"]
        self.assertEqual(malformed["motion_state"], "UNKNOWN")
        self.assertEqual(malformed["lane"], "unknown")
        self.assertEqual(malformed["owner"], "CTO")
        self.assertEqual(malformed["next_owner"], "CTO")
        self.assertEqual(malformed["workflow_motion"], "none")
        self.assertIn("no exact head binding", malformed["hold_reason"])
        self.assertIn("reconcile", malformed["next_action"])
        self.assertIn("ledger repair", malformed["wake"])
        self.assertEqual(rows["7591"]["lane"], "dependency-blocked")
        sessions = [
            {"label": label, "jsonl": "/tmp/session", "age_seconds": 1}
            for label in ("PM", "S1", "S2", "S3", "S4", "S5", "S6")
        ]
        self.assertEqual(
            MODULE.validate({
                "sessions": sessions,
                "control_plane": {"done_status": "ok", "pending_status": "ok", "done": [], "pending": []},
                "open_pr_activity_audit": audit,
            }),
            [],
        )

    def test_headless_newest_sibling_does_not_hide_valid_exact_head_continuation(self) -> None:
        headless = {
            "id": "15931",
            "kind": "slot_assignment",
            "pr": "7591",
            "issue": "7554",
            "slot": "4",
            "owner": "cto",
            "required_action": "assign the exact-head repro packet",
            "blocker": "",
            "evidence_json": "{}",
        }
        exact = continuation("ci_watch")
        exact["id"] = "15905"
        exact["pr"] = "7591"
        with mock.patch.object(
            MODULE,
            "run_cmd",
            return_value=MODULE.CmdResult(True, json.dumps([headless, exact]), "", 0),
        ):
            records, error = MODULE._load_open_pr_continuations("7591", HEAD)
        self.assertIsNone(error)
        self.assertEqual([record["id"] for record in records], ["15905"])

    def test_headless_sibling_cannot_hide_executing_exact_head_lane(self) -> None:
        headless_error = "row: durable continuation has no exact head binding"
        runs, jobs = running_run("CI")
        with mock.patch.object(
            MODULE,
            "_audit_gh_json",
            side_effect=[
                ([pr()], None),
                ({"workflow_runs": runs}, None),
                ({"jobs": jobs["33397393224"]}, None),
            ],
        ), mock.patch.object(
            MODULE,
            "_load_open_pr_continuations",
            return_value=([], headless_error),
        ):
            audit = MODULE.collect_open_pr_activity_audit({})
        self.assertTrue(audit["ok"])
        self.assertEqual(audit["rows"][0]["motion_state"], "CI_E2E_IN_PROGRESS")
        self.assertEqual(audit["rows"][0]["workflow_motion"], "CI:active")

    def test_unsupported_legacy_sibling_cannot_hide_exact_head_merge_ready(self) -> None:
        # This is the production writer's current unsupported kind; the row
        # is malformed for the continuation consumer but not authority over
        # exact-head GitHub evidence.
        headless_error = "row: unsupported exact-head continuation kind: candidate_rework"
        runs = [
            {
                "id": 333,
                "head_sha": HEAD,
                "workflowName": workflow,
                "event": "pull_request",
                "status": "completed",
                "conclusion": "success",
                "created_at": "2026-09-01T00:00:00Z",
                "run_attempt": 1,
            }
            for workflow in ("CI", "E2E Smoke Tests")
        ]
        with mock.patch.object(
            MODULE,
            "_audit_gh_json",
            side_effect=[
                ([{**pr(), "labels": [{"name": "merge-ready"}]}], None),
                ({"workflow_runs": runs}, None),
                ({"jobs": []}, None),
                ({"jobs": []}, None),
            ],
        ), mock.patch.object(
            MODULE,
            "_load_open_pr_continuations",
            return_value=([], headless_error),
        ):
            audit = MODULE.collect_open_pr_activity_audit({})
        self.assertTrue(audit["ok"])
        self.assertEqual(audit["rows"][0]["motion_state"], "MERGE_READY")
        self.assertEqual(audit["rows"][0]["lane"], "merge-ready")

    def test_contradictory_exact_head_authority_remains_unknown(self) -> None:
        runs = [
            {
                "id": 334,
                "head_sha": HEAD,
                "workflowName": workflow,
                "event": "pull_request",
                "status": "completed",
                "conclusion": "success",
                "created_at": "2026-09-01T00:00:00Z",
                "run_attempt": 1,
            }
            for workflow in ("CI", "E2E Smoke Tests")
        ]
        contradiction = "row: contradictory exact-head durable continuation records"
        with mock.patch.object(
            MODULE,
            "_audit_gh_json",
            side_effect=[
                ([{**pr(), "labels": [{"name": "merge-ready"}]}], None),
                ({"workflow_runs": runs}, None),
                ({"jobs": []}, None),
                ({"jobs": []}, None),
            ],
        ), mock.patch.object(
            MODULE,
            "_load_open_pr_continuations",
            return_value=([], contradiction),
        ):
            audit = MODULE.collect_open_pr_activity_audit({})
        self.assertTrue(audit["ok"])
        self.assertEqual(audit["rows"][0]["motion_state"], "UNKNOWN")
        self.assertIn("contradictory", audit["rows"][0]["hold_reason"])

    def test_unreadable_ledger_remains_an_audit_wide_refusal(self) -> None:
        with mock.patch.object(
            MODULE,
            "_audit_gh_json",
            side_effect=[([pr()], None), ({"workflow_runs": []}, None)],
        ), mock.patch.object(
            MODULE,
            "_load_open_pr_continuations",
            return_value=([], "authority: durable continuation authority unavailable"),
        ):
            audit = MODULE.collect_open_pr_activity_audit({})
        self.assertFalse(audit["ok"])
        self.assertEqual(audit["rows"], [])
        self.assertIn("durable continuation authority unavailable", audit["error"])

    def test_each_live_fact_has_one_normalized_lane_and_motion_fields(self) -> None:
        runs, jobs = running_run("CI")
        rows = [
            self.evaluate(runs=runs, jobs=jobs),
            self.evaluate(*running_run("E2E LLM Proxy Capture (manual)")),
            self.evaluate(
                slots={"2": {"pr": "7591", "head_sha": HEAD, "occupied": True,
                              "active_turn_state": "active", "active_turn_id": "turn-1",
                              "task": "production-shaped proof"}}
            ),
            self.evaluate(
                slots={"2": {"pr": "7591", "head_sha": HEAD, "occupied": True,
                              "active_turn_state": "active", "active_turn_id": "turn-1",
                              "task": "implementation fix"}}
            ),
            self.evaluate(records=[continuation("rework")]),
            self.evaluate(records=[continuation("dependency_wait")]),
            self.evaluate(),
        ]
        self.assertEqual(
            [row["lane"] for row in rows],
            ["CI", "capture", "repro/rework", "repro/rework", "rework-blocked", "dependency-blocked", "true limbo"],
        )
        for row in rows:
            for field in ("workflow_motion", "owner_source", "hold_reason", "next_action", "next_owner", "wake"):
                self.assertTrue(row[field], field)

    def test_durable_ci_or_capture_ownership_does_not_claim_workflow_motion(self) -> None:
        for kind in ("ci_watch", "capture_release"):
            row = self.evaluate(records=[continuation(kind)])
            self.assertEqual(row["motion_state"], "PROCESS_LIMBO")
            self.assertEqual(row["lane"], "true limbo")
            self.assertEqual(row["workflow_motion"], "none")
            self.assertEqual(row["owner_source"], "pm-ops.obligations")
            self.assertEqual(row["owner"], "cto")

    def test_formatter_renders_every_mixed_open_pr_row_and_all_contract_fields(self) -> None:
        rows = []
        for index, state in enumerate(("CI_IN_PROGRESS", "PROCESS_LIMBO", "DEPENDENCY_BLOCKED"), 1):
            rows.append({
                "pr": str(7590 + index),
                "branch": f"fix/{7590 + index}",
                "head": f"{index:x}" * 40,
                "motion_state": state,
                "owner": "workflow" if index == 1 else "cto",
                "workflow_motion": "CI" if index == 1 else "none",
                "owner_source": "workflow" if index == 1 else "pm-ops.obligations",
                "hold_reason": "none" if index == 1 else "waiting for CTO boundary",
                "next_action": "await exact-head terminal" if index == 1 else "consume exact-head wake",
                "next_owner": "cto",
                "wake": "await exact-head terminal" if index == 1 else "consume exact-head wake",
                "next_boundary": "exact-head terminal",
                "reasons": [],
                "last_exact": {},
            })
        lines = MODULE.format_open_pr_activity_audit({
            "ok": True,
            "open_pr_count": 3,
            "gaps": [rows[1], rows[2]],
            "rows": rows,
            "counts": {"capture": 0, "ci_e2e": 1, "numbered_reproduction": 0},
            "motion_states": {"CI_IN_PROGRESS": 1, "PROCESS_LIMBO": 1, "DEPENDENCY_BLOCKED": 1},
        })
        self.assertIn("open_pr_activity_rows=3", lines[0])
        self.assertIn("open_prs=3", lines[0])
        rendered = [line for line in lines if line.startswith("- PR #")]
        self.assertEqual(len(rendered), 3)
        for line in rendered:
            for field in ("workflow_motion=", "owner_source=", "hold_reason=", "next_action=", "next_owner=", "wake="):
                self.assertIn(field, line)
            self.assertRegex(line, r"head=[0-9a-f]{40}")

    def test_runtime_validate_rejects_pr_audit_row_mismatch_and_placeholders(self) -> None:
        sessions = [{"label": label, "jsonl": "/tmp/session", "age_seconds": 1}
                    for label in ("PM", "S1", "S2", "S3", "S4", "S5", "S6")]
        audit = {
            "ok": True,
            "open_pr_count": 2,
            "open_pr_activity_gaps": 0,
            "gaps": [],
            "rows": [{
                "pr": "7591", "branch": "fix/7591", "head": HEAD,
                "motion_state": "PROCESS_LIMBO",
                "owner": "unknown", "workflow_motion": "none", "owner_source": "none",
                "hold_reason": "none", "next_action": "none", "next_owner": "unknown", "wake": "none",
            }],
        }
        errors = MODULE.validate({
            "sessions": sessions,
            "control_plane": {"done_status": "ok", "pending_status": "ok", "done": [], "pending": []},
            "open_pr_activity_audit": audit,
        })
        self.assertIn("open_pr_activity_audit row-count mismatch", errors)
        self.assertTrue(any("placeholder" in error for error in errors))

    def test_runtime_validate_keeps_truthful_bound_lane_without_wake_nonfatal(self) -> None:
        sessions = [{"label": label, "jsonl": "/tmp/session", "age_seconds": 1}
                    for label in ("PM", "S1", "S2", "S3", "S4", "S5", "S6")]
        active_ci_row = {
            "pr": "7647",
            "branch": "codex/7645-certificate-retry-mitigation",
            "head": "cf7c6911cf2ceaf8e82375fca6d487ca1ef0a11b",
            "motion_state": "CI_E2E_IN_PROGRESS",
            "binding_status": "ci_bound",
            "binding_evidence": ["CI:CI:active", "CI:E2E Smoke Tests:queued"],
            "binding_limitations": [],
            "binding_missing": [],
            "verification_limited": True,
            "unbound": False,
            "owner": "unowned",
            "workflow_motion": "E2E Smoke Tests:queued,CI:active",
            "owner_source": "workflow",
            "hold_reason": "none",
            "next_action": "await the exact-head lane terminal",
            "next_owner": "CTO",
            "wake": "none",
            "next_boundary": "none",
            "reasons": [],
            "last_exact": {},
        }
        malformed_row = {
            **active_ci_row,
            "pr": "7648",
            "branch": "feature/7645-reactive-disable-editor-lease",
            "head": "74c860252534898453b742ad48e61ee766743d81",
            "motion_state": "UNKNOWN",
            "binding_status": "unknown",
            "binding_evidence": [],
            "binding_limitations": ["durable continuation has no exact head binding"],
            "verification_limited": True,
            "owner": "CTO",
            "workflow_motion": "none",
            "owner_source": "pm-ops.obligations malformed",
            "hold_reason": "malformed durable continuation: durable continuation has no exact head binding",
            "next_action": "repair or reconcile the exact-head durable continuation record",
            "next_owner": "CTO",
            "wake": "CTO consumes this exact-head ledger repair row",
            "next_boundary": "repair or reconcile the exact-head durable continuation record",
            "reasons": ["malformed durable continuation: durable continuation has no exact head binding"],
        }
        audit = {
            "ok": True,
            "open_pr_count": 2,
            "open_pr_activity_gaps": 0,
            "gaps": [],
            "rows": [active_ci_row, malformed_row],
        }
        data = {
            "sessions": sessions,
            "control_plane": {"done_status": "ok", "pending_status": "ok", "done": [], "pending": []},
            "open_pr_activity_audit": audit,
        }
        self.assertEqual(MODULE.validate(data), [])
        self.assertEqual(MODULE.open_pr_activity_action_lines(audit), [])

        unknown_without_wake = {**malformed_row, "wake": "none"}
        invalid = {**data, "open_pr_activity_audit": {**audit, "rows": [unknown_without_wake, active_ci_row]}}
        self.assertTrue(any("wake is missing or placeholder" in error for error in MODULE.validate(invalid)))

    def test_stale_or_conflicting_durable_records_do_not_become_current_owner(self) -> None:
        stale = continuation("rework")
        stale["evidence_json"] = json.dumps({"head": "0" * 40})
        stale["head"] = "0" * 40
        row = self.evaluate(records=[stale])
        self.assertEqual(row["lane"], "true limbo")
        self.assertEqual(row["owner_source"], "none")
        conflicting = [continuation("rework", "pm"), continuation("dependency_wait", "cto")]
        self.assertEqual(MODULE._continuation_motion_metadata(conflicting), None)
        self.assertIsNone(MODULE._continuation_motion_metadata([{"head": HEAD}]))

    def test_quiet_terminal_red_has_no_current_motion(self) -> None:
        runs = [{
            "id": 33397393224,
            "head_sha": HEAD,
            "workflowName": "E2E Smoke Tests",
            "event": "pull_request",
            "status": "completed",
            "conclusion": "failure",
            "created_at": "2026-09-01T00:00:00Z",
            "run_attempt": 1,
        }]
        row = self.evaluate(runs=runs, jobs={"33397393224": []})
        self.assertEqual(row["lane"], "true limbo")
        self.assertEqual(row["workflow_motion"], "none")
        self.assertEqual(row["owner_source"], "none")

    def test_offslot_release_owner_is_joined_from_durable_authority(self) -> None:
        row = self.evaluate(records=[continuation("slot_retask", "release-owner")])
        self.assertEqual(row["lane"], "rework")
        self.assertEqual(row["owner"], "release-owner")
        self.assertEqual(row["owner_source"], "pm-ops.obligations")

    def test_authority_reader_returns_only_exact_head_and_refuses_conflicting_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "pm-ops.db"
            conn = sqlite3.connect(db)
            conn.execute("create table obligations (id integer, kind text, status text, pr integer, issue integer, slot integer, owner text, title text, required_action text, blocker text, evidence_json text, updated_at text, created_at text)")
            conn.execute(
                "insert into obligations values (1,'ci_watch','open',7591,null,null,'cto','t','await','hold',?,null,null)",
                (json.dumps({"head": HEAD}),),
            )
            conn.commit()
            original = MODULE.PM_OPS_DB
            MODULE.PM_OPS_DB = db
            try:
                records, error = MODULE._load_open_pr_continuations("7591", HEAD)
                self.assertIsNone(error)
                self.assertEqual(records[0]["lane"], "CI")
                self.assertEqual(records[0]["next_owner"], "cto")
                conn.execute(
                    "insert into obligations values (4,'ci_watch','open',7591,null,null,'unknown','t','await','hold',?,null,null)",
                    (json.dumps({"head": HEAD}),),
                )
                conn.commit()
                records, error = MODULE._load_open_pr_continuations("7591", HEAD)
                self.assertIsNone(error)
                self.assertEqual(records[0]["lane"], "CI")
                conn.execute("delete from obligations where id=4")
                conn.commit()
                conn.execute("delete from obligations where id=1")
                conn.commit()
                conn.execute(
                    "insert into obligations values (4,'ci_watch','open',7591,null,null,'unknown','t','await','hold',?,null,null)",
                    (json.dumps({"head": HEAD}),),
                )
                conn.commit()
                records, error = MODULE._load_open_pr_continuations("7591", HEAD)
                self.assertEqual(records, [])
                self.assertIn("placeholder", error or "")
                conn.execute("delete from obligations where id=4")
                conn.commit()
                conn.execute(
                    "insert into obligations values (3,'ci_watch','open',7591,null,null,'cto','t','await','hold','{}',null,null)"
                )
                conn.commit()
                records, error = MODULE._load_open_pr_continuations("7591", HEAD)
                self.assertEqual(records, [])
                self.assertIn("exact head", error or "")
                conn.execute("delete from obligations where id=3")
                conn.commit()
                conn.execute(
                    "insert into obligations values (1,'ci_watch','open',7591,null,null,'cto','t','await','hold',?,null,null)",
                    (json.dumps({"head": HEAD}),),
                )
                conn.commit()
                conn.execute(
                    "insert into obligations values (2,'dependency_wait','open',7591,null,null,'pm','t','await','hold',?,null,null)",
                    (json.dumps({"head": HEAD}),),
                )
                conn.commit()
                records, error = MODULE._load_open_pr_continuations("7591", HEAD)
                self.assertEqual(records, [])
                self.assertIn("contradictory", error or "")
            finally:
                MODULE.PM_OPS_DB = original
                conn.close()

    def test_production_loaded_record_survives_evaluator_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "pm-ops.db"
            conn = sqlite3.connect(db)
            conn.execute("create table obligations (id integer, kind text, status text, pr integer, issue integer, slot integer, owner text, title text, required_action text, blocker text, evidence_json text, updated_at text, created_at text)")
            conn.execute(
                "insert into obligations values (9,'dependency_wait','open',7591,null,null,'cto','dependency hold','consume the exact-head continuation at the next safe boundary','dependency is holding the PR',?,null,null)",
                (json.dumps({"head": HEAD}),),
            )
            conn.commit()
            original = MODULE.PM_OPS_DB
            MODULE.PM_OPS_DB = db
            try:
                records, error = MODULE._load_open_pr_continuations("7591", HEAD)
                self.assertIsNone(error)
                self.assertEqual(len(records), 1)
                record = records[0]
                self.assertEqual(record["owner"], "cto")
                self.assertEqual(record["next_owner"], "cto")
                self.assertTrue(record["next_action"])
                self.assertTrue(record["wake"])
                self.assertTrue(record["hold_reason"])
                row = MODULE.evaluate_open_pr_activity(
                    pr(), [], {}, {}, now_utc=NOW, continuation_records=records
                )
                self.assertEqual(row["lane"], "dependency-blocked")
                self.assertEqual(row["owner"], "cto")
                self.assertEqual(row["owner_source"], "pm-ops.obligations")
                self.assertEqual(row["next_owner"], "cto")
                self.assertTrue(row["next_action"])
                self.assertTrue(row["wake"])
                self.assertTrue(row["hold_reason"])
                self.assertEqual(row["workflow_motion"], "none")
            finally:
                MODULE.PM_OPS_DB = original
                conn.close()


if __name__ == "__main__":
    unittest.main()
