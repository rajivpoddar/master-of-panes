from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
PUBLISHER = ROOT / "shared-assets" / "claude" / "scripts" / "ci" / "publish-ci-verdict.py"
WRAPPER = ROOT / "shared-assets" / "claude" / "scripts" / "ci" / "rerun-after-local-proof.sh"
SPEC = importlib.util.spec_from_file_location("publish_ci_verdict", PUBLISHER)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


HEAD = "f109414c02cc296510103fe2c090ce964e9b9dfb"
RUN_ID = 33397393224


def investigation_packet() -> dict:
    return {
        "pr": 7591,
        "run_id": RUN_ID,
        "attempt": 1,
        "sha": HEAD,
        "current_for_pr": True,
        "classification": "test_shared_harness_wall_budget",
        "local_repro_result": "passed",
        "requested_owner_action": "rerun-after-proof",
        "pm_transition_recommendation": "one unchanged-head rerun after shared harness diagnosis",
        "blocking_for_merge": True,
        "required_check_failure": True,
        "first_causal_boundary": "shared test-harness instability before product assertions",
        "causal_fingerprint": "shared-harness-failure-run-33397393224",
        "fast_fingerprint": {
            "category": "test-harness",
            "signature": "shared harness wall budget",
        },
    }


def run_snapshot(**overrides: object) -> dict:
    result = {
        "run_attempt": 1,
        "head_sha": HEAD,
        "status": "completed",
        "conclusion": "failure",
        "name": "E2E Smoke Tests",
        "event": "pull_request",
    }
    result.update(overrides)
    return result


class CiVerdictProducerConsumerTests(unittest.TestCase):
    def test_rerun_consumer_has_no_spend_budget_gate_or_counter(self) -> None:
        wrapper = WRAPPER.read_text(encoding="utf-8")
        for retired in (
            "CI_BUDGET",
            "budget_ok(",
            "guarded_first_attempt_budget_override_ok",
            "current_head_bad_run_budget_exceeded",
            "ci_budget_exceeded",
            "max_total_runs",
            "max_current_bad_runs",
            "max_expensive_runs",
            "window-hours",
            "estimated_min_cost",
        ):
            self.assertNotIn(retired, wrapper)
        self.assertIn("current_head_ci_already_running", wrapper)
        self.assertIn("rerun_already_claimed", wrapper)
        self.assertIn("run_head_mismatch", wrapper)
        self.assertIn('args=(run rerun "$run_id" --repo "$REPO")', wrapper)

    def consumer_script(self) -> str:
        wrapper = WRAPPER.read_text(encoding="utf-8")
        start = wrapper.index('  python3 - "$comments_file"')
        start = wrapper.index("<<'PY'\n", start) + len("<<'PY'\n")
        end = wrapper.index("\nPY\n", start)
        return wrapper[start:end]

    def consumer_accepts(self, verdict: dict, *, duplicate: bool = False) -> bool:
        marker = f"ci-failure-investigation:run={RUN_ID} attempt=1 head={HEAD}"
        body = f"{marker}\n<!-- ci-verdict: {json.dumps(verdict, sort_keys=True)} -->"
        comments = [
            {"body": body},
            *([{"body": body}] if duplicate else []),
        ]
        with tempfile.TemporaryDirectory() as directory:
            comments_path = Path(directory) / "comments.json"
            comments_path.write_text(json.dumps(comments), encoding="utf-8")
            process = subprocess.run(
                [
                    "python3", "-", str(comments_path), str(RUN_ID), "1", HEAD,
                    "7591", "", "local", "", "",
                ],
                input=self.consumer_script(),
                text=True,
                capture_output=True,
                check=False,
            )
        return process.returncode == 0

    def test_consumer_requires_current_attempt_test_authorization(self) -> None:
        verdict = {
            "schema_version": 3,
            "run_id": RUN_ID,
            "attempt": 1,
            "run_attempt": 1,
            "pr": 7591,
            "sha": HEAD,
            "current_for_pr": True,
            "classification": "test",
            "requested_owner_action": "rerun-after-proof",
            "local_repro_result": "passed",
            "fast_fingerprint": {"signature": "shared harness"},
            "rerun_authorization": {
                "action": "rerun-after-proof",
                "run_id": str(RUN_ID),
                "attempt": 1,
                "head_sha": HEAD,
                "single_use": True,
            },
        }
        self.assertTrue(self.consumer_accepts(verdict))
        cases = {
            "missing run_attempt": {**verdict, "run_attempt": None},
            "stale run_attempt": {**verdict, "run_attempt": 2},
            "missing authorization": {key: value for key, value in verdict.items() if key != "rerun_authorization"},
            "malformed authorization": {**verdict, "rerun_authorization": "CTO rerun"},
            "wrong authorization head": {
                **verdict,
                "rerun_authorization": {**verdict["rerun_authorization"], "head_sha": "0" * 40},
            },
            "wrong authorization run": {
                **verdict,
                "rerun_authorization": {**verdict["rerun_authorization"], "run_id": "1"},
            },
            "wrong authorization attempt": {
                **verdict,
                "rerun_authorization": {**verdict["rerun_authorization"], "attempt": 2},
            },
            "wrong verdict head": {**verdict, "sha": "0" * 40},
            "wrong verdict run": {**verdict, "run_id": 1},
        }
        for name, invalid in cases.items():
            with self.subTest(name=name):
                self.assertFalse(self.consumer_accepts(invalid))
        self.assertFalse(self.consumer_accepts(verdict, duplicate=True))

    def test_producer_binds_exact_latest_attempt_workflow_and_rerun_action(self) -> None:
        verdict = MODULE.verdict_from_investigation(
            investigation_packet(),
            pr=7591,
            run_id=RUN_ID,
            run=run_snapshot(),
            pull={"head": {"sha": HEAD}},
        )
        self.assertEqual(verdict["run_id"], RUN_ID)
        self.assertEqual(verdict["attempt"], 1)
        self.assertEqual(verdict["run_attempt"], 1)
        self.assertEqual(verdict["pr"], 7591)
        self.assertEqual(verdict["sha"], HEAD)
        self.assertEqual(verdict["workflow"], "E2E Smoke Tests")
        self.assertEqual(verdict["workflow_event"], "pull_request")
        self.assertEqual(verdict["classification"], "test")
        self.assertEqual(verdict["requested_owner_action"], "rerun-after-proof")
        self.assertEqual(
            verdict["rerun_authorization"],
            {
                "action": "rerun-after-proof",
                "run_id": str(RUN_ID),
                "attempt": 1,
                "head_sha": HEAD,
                "single_use": True,
            },
        )
        self.assertEqual(
            MODULE.validate(verdict, pr=7591, run_id=RUN_ID, run=run_snapshot(), pull={"head": {"sha": HEAD}}),
            (1, HEAD),
        )

    def test_missing_or_advanced_attempt_and_workflow_drift_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "run_attempt is missing or malformed"):
            MODULE.verdict_from_investigation(
                investigation_packet(),
                pr=7591,
                run_id=RUN_ID,
                run=run_snapshot(run_attempt=None),
                pull={"head": {"sha": HEAD}},
            )
        with self.assertRaisesRegex(ValueError, "does not match current run attempt"):
            MODULE.verdict_from_investigation(
                investigation_packet(),
                pr=7591,
                run_id=RUN_ID,
                run=run_snapshot(run_attempt=2),
                pull={"head": {"sha": HEAD}},
            )
        with self.assertRaisesRegex(ValueError, "workflow is not label-gated"):
            MODULE.verdict_from_investigation(
                investigation_packet(),
                pr=7591,
                run_id=RUN_ID,
                run=run_snapshot(name="untrusted workflow"),
                pull={"head": {"sha": HEAD}},
            )
        with self.assertRaisesRegex(ValueError, "event is not pull_request"):
            MODULE.verdict_from_investigation(
                investigation_packet(),
                pr=7591,
                run_id=RUN_ID,
                run=run_snapshot(event="workflow_dispatch"),
                pull={"head": {"sha": HEAD}},
            )

    def test_canonical_marker_binds_tuple_and_consumer_refuses_duplicate_marker(self) -> None:
        verdict = MODULE.verdict_from_investigation(
            investigation_packet(),
            pr=7591,
            run_id=RUN_ID,
            run=run_snapshot(),
            pull={"head": {"sha": HEAD}},
        )
        self.assertTrue(self.consumer_accepts(verdict))
        self.assertFalse(self.consumer_accepts(verdict, duplicate=True))


if __name__ == "__main__":
    unittest.main()
