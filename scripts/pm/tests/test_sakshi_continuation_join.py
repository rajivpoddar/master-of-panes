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
        self.assertEqual(audit["open_pr_activity_gaps"], 1)
        self.assertEqual(audit["rows"][0]["motion_state"], "UNKNOWN")
        self.assertEqual(audit["rows"][0]["lane"], "unknown")
        self.assertIn("missing", audit["rows"][0]["hold_reason"])

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
