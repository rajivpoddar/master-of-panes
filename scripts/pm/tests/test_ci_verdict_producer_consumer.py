from __future__ import annotations

import importlib.util
import re
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
        comment = MODULE.render_comment("shared harness report", verdict, RUN_ID, 1, HEAD)
        self.assertIn(f"ci-failure-investigation:run={RUN_ID} attempt=1 head={HEAD}", comment)
        wrapper = WRAPPER.read_text(encoding="utf-8")
        self.assertIn("run_attempt_missing_or_malformed", wrapper)
        self.assertIn("matching_comments = [", wrapper)
        self.assertIn("if len(matching_comments) > 1:", wrapper)
        self.assertIn('if "run_attempt" in verdict', wrapper)
        self.assertIn('authorization.get("action") != "rerun-after-proof"', wrapper)
        self.assertNotRegex(wrapper, r"run_attempt=1")


if __name__ == "__main__":
    unittest.main()
