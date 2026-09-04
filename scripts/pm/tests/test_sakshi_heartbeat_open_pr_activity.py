from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from datetime import datetime, timezone
from unittest import mock


ROOT = __import__("pathlib").Path(__file__).parents[1]
SOURCE = ROOT / "shared-assets" / "claude" / "scripts" / "pm" / "control-plane" / "sakshi-heartbeat.py"
SPEC = importlib.util.spec_from_file_location("sakshi_open_pr_activity", SOURCE)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules["sakshi_open_pr_activity"] = MODULE
runtime_observation = types.ModuleType("control_plane.runtime_observation")
runtime_observation.RuntimeObservationAdapter = object
runtime_observation.parse_timestamp = lambda value: value
control_plane = types.ModuleType("control_plane")
control_plane.runtime_observation = runtime_observation
sys.modules["control_plane"] = control_plane
sys.modules["control_plane.runtime_observation"] = runtime_observation
SPEC.loader.exec_module(MODULE)


HEAD = "d7f04ce21a7276c0f01220d349e84c0a4f28db37"
NOW = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)


def open_pr(*, labels: list[str] | None = None, head: str = HEAD) -> dict:
    return {
        "number": 7627,
        "head": {"sha": head, "ref": "fix/7627"},
        "headRefOid": head,
        "headRefName": "fix/7627",
        "labels": [{"name": label} for label in (labels or [])],
        "draft": False,
        "mergeable_state": "behind",
    }


def run(workflow: str, *, status: str = "completed", conclusion: str | None = "success",
        event: str = "pull_request", run_id: int = 1, head: str = HEAD,
        created_at: str = "2026-09-04T00:00:00Z", run_attempt: int | str = 1) -> dict:
    return {
        "id": run_id,
        "head_sha": head,
        "workflowName": workflow,
        "event": event,
        "status": status,
        "conclusion": conclusion,
        "created_at": created_at,
        "run_attempt": run_attempt,
    }


def active_job(job_id: int = 1) -> dict:
    return {
        "id": job_id,
        "status": "in_progress",
        "runner_id": 7,
        "steps": [{"name": "test", "status": "in_progress"}],
    }


class SakshiOpenPrActivityTests(unittest.TestCase):
    def evaluate(self, pr=None, runs=None, jobs=None, slots=None, records=None):
        return MODULE.evaluate_open_pr_activity(
            pr or open_pr(), runs or [], jobs or {}, slots or {},
            now_utc=NOW, continuation_records=records or [],
        )

    def test_head_scoped_query_retrieves_old_exact_run_beyond_global_window(self):
        exact = run("CI", run_id=762701)
        unrelated = [run("CI", run_id=index, head="a" * 40) for index in range(100)]
        calls = []

        def gh(args):
            calls.append(args)
            if args[0].endswith("/pulls"):
                return [open_pr()], None
            if args[0].endswith("/actions/runs"):
                return {"workflow_runs": [exact, *unrelated]}, None
            if args[0].endswith("/jobs"):
                return {"jobs": []}, None
            raise AssertionError(args)

        with mock.patch.object(MODULE, "_audit_gh_json", side_effect=gh), \
                mock.patch.object(MODULE, "_load_open_pr_continuations", return_value=([], None)):
            audit = MODULE.collect_open_pr_activity_audit({})
        self.assertTrue(audit["ok"])
        self.assertEqual(audit["rows"][0]["motion_state"], "PROCESS_LIMBO")
        run_call = next(args for args in calls if args[0].endswith("/actions/runs"))
        self.assertIn("-f", run_call)
        self.assertIn(f"head_sha={HEAD}", run_call)

    def test_merge_ready_uses_exact_green_pair_even_behind_current_main(self):
        runs = [run("CI", run_id=10), run("E2E Smoke Tests", run_id=11)]
        row = self.evaluate(pr=open_pr(labels=["merge-ready"]), runs=runs,
                            jobs={"10": [], "11": []})
        self.assertEqual(row["motion_state"], "MERGE_READY")
        self.assertFalse(row["gap"])
        self.assertIn("CI:green", row["workflow_motion"])
        self.assertIn("E2E Smoke Tests:green", row["workflow_motion"])

    def test_newer_terminal_failure_or_cancellation_overrides_older_green(self):
        for conclusion in ("failure", "cancelled", "timed_out"):
            runs = [
                run("CI", run_id=100, created_at="2026-09-04T00:00:00Z"),
                run("CI", run_id=101, conclusion=conclusion, created_at="2026-09-04T01:00:00Z"),
                run("E2E Smoke Tests", run_id=102, created_at="2026-09-04T00:00:00Z"),
                run("E2E Smoke Tests", run_id=103, conclusion=conclusion, created_at="2026-09-04T01:00:00Z"),
            ]
            row = self.evaluate(pr=open_pr(labels=["merge-ready"]), runs=runs,
                                jobs={str(item["id"]): [] for item in runs})
            self.assertEqual(row["motion_state"], "PROCESS_LIMBO")
            self.assertTrue(row["gap"])
            self.assertIn("latest exact-head run is terminal non-success", " ".join(row["reasons"]))

    def test_latest_successful_attempt_is_authoritative_for_same_run(self):
        runs = [
            run("CI", run_id=110, run_attempt=1, conclusion="success"),
            run("CI", run_id=110, run_attempt=2, conclusion="failure"),
            run("E2E Smoke Tests", run_id=111),
        ]
        row = self.evaluate(pr=open_pr(labels=["merge-ready"]), runs=runs,
                            jobs={"110": [], "111": []})
        self.assertEqual(row["motion_state"], "PROCESS_LIMBO")
        self.assertTrue(row["gap"])

    def test_latest_attempt_success_keeps_merge_ready(self):
        runs = [
            run("CI", run_id=120, run_attempt=1, conclusion="failure"),
            run("CI", run_id=120, run_attempt=2, conclusion="success"),
            run("E2E Smoke Tests", run_id=121),
        ]
        row = self.evaluate(pr=open_pr(labels=["merge-ready"]), runs=runs,
                            jobs={"120": [], "121": []})
        self.assertEqual(row["motion_state"], "MERGE_READY")

    def test_malformed_latest_run_ordering_is_unknown(self):
        malformed = run("CI", run_id=130)
        malformed.pop("created_at")
        row = self.evaluate(runs=[malformed, run("E2E Smoke Tests", run_id=131)])
        self.assertEqual(row["motion_state"], "UNKNOWN")
        self.assertTrue(row["gap"])

    def test_active_and_queued_ci_are_non_limbo_and_distinguish_phase(self):
        active = run("CI", status="in_progress", conclusion=None, run_id=20)
        queued = run("E2E Smoke Tests", status="queued", conclusion=None, run_id=21)
        row = self.evaluate(runs=[active, queued], jobs={"20": [active_job()], "21": []})
        self.assertEqual(row["motion_state"], "CI_E2E_IN_PROGRESS")
        self.assertFalse(row["gap"])
        self.assertIn("CI:active", row["workflow_motion"])
        self.assertIn("E2E Smoke Tests:queued", row["workflow_motion"])

    def test_capture_and_exact_slot_repro_or_rework_are_non_limbo(self):
        capture = run("E2E LLM Proxy Capture (manual)", status="in_progress",
                      conclusion=None, event="workflow_dispatch", run_id=30)
        row = self.evaluate(runs=[capture], jobs={"30": [active_job()]})
        self.assertEqual(row["motion_state"], "CAPTURE_IN_PROGRESS")
        for task in ("production-shaped reproduction", "implementation fix"):
            row = self.evaluate(slots={"2": {
                "pr": "7627", "head_sha": HEAD, "occupied": True,
                "active_turn_state": "active", "active_turn_id": "turn-1", "task": task,
            }})
            self.assertEqual(row["motion_state"], "REPRO_REWORK_IN_PROGRESS")
            self.assertFalse(row["gap"])

    def test_queued_repro_rework_is_distinct_from_owned_wait_and_parked_proof(self):
        def record(kind: str, lane: str) -> dict:
            return {
                "id": "queue-1", "kind": kind, "lane": lane, "owner": "cto",
                "next_action": "consume the exact-head queue", "wake": "consume the exact-head queue",
                "hold_reason": "waiting", "next_owner": "cto", "head": HEAD,
            }

        row = self.evaluate(records=[record("slot_rework", "rework")])
        self.assertEqual(row["motion_state"], "REPRO_REWORK_QUEUED")
        self.assertFalse(row["gap"])
        row = self.evaluate(records=[record("dependency_wait", "dependency-blocked")])
        self.assertEqual(row["motion_state"], "PROCESS_LIMBO")
        self.assertTrue(row["gap"])

    def test_stale_skipped_and_historical_evidence_remain_limbo(self):
        stale_slot = {"2": {"pr": "7627", "head_sha": "a" * 40, "occupied": True,
                             "active_turn_state": "active", "active_turn_id": "turn-1",
                             "task": "production-shaped reproduction"}}
        skipped = run("CI", conclusion="skipped", run_id=40)
        row = self.evaluate(runs=[skipped], jobs={"40": []}, slots=stale_slot)
        self.assertEqual(row["motion_state"], "PROCESS_LIMBO")
        self.assertTrue(row["gap"])

    def test_unreadable_or_ambiguous_authority_is_unknown(self):
        self.assertEqual(self.evaluate(pr=open_pr(head="not-a-head"))["motion_state"], "UNKNOWN")
        first = run("CI", status="in_progress", conclusion=None, run_id=50)
        second = run("CI", status="queued", conclusion=None, run_id=51)
        row = self.evaluate(runs=[first, second], jobs={"50": [active_job(50)], "51": []})
        self.assertEqual(row["motion_state"], "UNKNOWN")
        self.assertTrue(row["gap"])
        self.assertEqual(self.evaluate(pr={**open_pr(), "labels": [{"name": ""}]} )["motion_state"], "UNKNOWN")

    def test_unavailable_workflow_authority_is_unknown_not_limbo(self):
        with mock.patch.object(
            MODULE,
            "_audit_gh_json",
            side_effect=[([open_pr()], None), (None, "GitHub unavailable")],
        ):
            audit = MODULE.collect_open_pr_activity_audit({})
        self.assertTrue(audit["ok"])
        self.assertEqual(audit["rows"][0]["motion_state"], "UNKNOWN")
        self.assertTrue(audit["rows"][0]["gap"])


if __name__ == "__main__":
    unittest.main()
