"""Focused proof for the Modal auth-plane DEADLINE_EXCEEDED alert signature.

Drives the sanitized attempt-1 log shape end to end: log extraction must
emit exactly one Modal auth-plane failure annotation, and the classifier
must return modal-auth-plane-transient. Negatives pin the neighboring arms
and the window scoping. Hermetic: no network, no live state.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "shared-assets" / "claude" / "scripts" / "ci" / "ci-fast-triage.py"
SPEC = importlib.util.spec_from_file_location("ci_fast_triage_modal_deadline", SOURCE)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

Annotation = MODULE.Annotation


ATTEMPT1_LOG = """2026-09-21T06:00:01Z ##[group]Run PR-mode setup (pooled Convex branch deploy + seed + Modal + env)
2026-09-21T06:00:02Z Provisioning Modal function for capture seed...
2026-09-21T06:00:03Z modal-client: Authorization check failed for function call
2026-09-21T06:00:04Z status = StatusCode.DEADLINE_EXCEEDED
2026-09-21T06:00:05Z details = "Deadline Exceeded"
2026-09-21T06:00:06Z grpc.RpcError: <_InactiveRpcError of RPC that terminated with (StatusCode.DEADLINE_EXCEEDED)>
2026-09-21T06:00:07Z ##[error]Process completed with exit code 1.
"""


def modal_annotations(log_text):
    return [
        annotation
        for annotation in MODULE.extract_log_annotations(log_text)
        if "Modal auth-plane" in annotation.message
    ]


def classify(log_text):
    annotations = modal_annotations(log_text)
    jobs = [{"name": "E2E Smoke Tests", "conclusion": "failure"}]
    return MODULE.category_for(annotations, jobs)


class ModalDeadlineSignatureTests(unittest.TestCase):
    def test_attempt1_shape_emits_one_modal_annotation(self) -> None:
        found = modal_annotations(ATTEMPT1_LOG)
        self.assertEqual(len(found), 1)
        self.assertIn("Authorization check failed", found[0].message)
        self.assertIn("DEADLINE_EXCEEDED", found[0].message)

    def test_attempt1_shape_classifies_modal_transient(self) -> None:
        self.assertEqual(classify(ATTEMPT1_LOG), "modal-auth-plane-transient")

    def test_direct_annotation_with_same_tokens_classifies(self) -> None:
        annotation = Annotation(
            "", None, "failure", "",
            "PR-mode setup failed: Modal Authorization check failed; status = StatusCode.DEADLINE_EXCEEDED",
            "",
        )
        jobs = [{"name": "E2E Smoke Tests", "conclusion": "failure"}]
        self.assertEqual(
            MODULE.category_for([annotation], jobs), "modal-auth-plane-transient"
        )

    def test_local_command_is_not_applicable(self) -> None:
        self.assertEqual(
            MODULE.local_command("modal-auth-plane-transient", []),
            "not applicable (Modal auth-plane transport transient; workflow auto-retry owns recovery)",
        )

    def test_generic_5xx_is_not_modal(self) -> None:
        log = "2026-09-21T06:00:01Z Error: HTTP 500 Internal Server Error from api.example.test hook\n"
        self.assertEqual(modal_annotations(log), [])
        self.assertEqual(
            MODULE.category_for(MODULE.extract_log_annotations(log), []), "unknown"
        )

    def test_unrelated_deadline_without_auth_failure_is_not_modal(self) -> None:
        log = "2026-09-21T06:00:01Z TimeoutError: Deadline exceeded while waiting for Modal image manifest\n"
        self.assertEqual(modal_annotations(log), [])
        annotation = Annotation(
            "", None, "failure", "",
            "TimeoutError: Deadline exceeded while waiting for Modal image manifest",
            "",
        )
        self.assertEqual(
            MODULE.category_for([annotation], []), "timeout-or-wall-budget"
        )

    def test_product_spec_failure_is_not_modal(self) -> None:
        annotation = Annotation(
            "", None, "failure", "",
            "AssertionError: transcript paragraph count mismatch (expected 12, got 9)",
            "",
        )
        self.assertEqual(MODULE.category_for([annotation], []), "test-failure")

    def test_fixture_miss_arm_unchanged(self) -> None:
        annotation = Annotation(
            "", None, "failure", "",
            "llm_proxy_cache_miss section_type=index hash=" + "f" * 40,
            "",
        )
        self.assertEqual(MODULE.category_for([annotation], []), "capture-or-fixture")

    def test_auth_deadline_without_modal_context_refused(self) -> None:
        log = "Authorization check failed\nstatus = StatusCode.DEADLINE_EXCEEDED\n"
        self.assertEqual(modal_annotations(log), [])
        annotation = Annotation("", None, "failure", "", log, "")
        self.assertNotEqual(
            MODULE.category_for([annotation], []), "modal-auth-plane-transient"
        )

    def test_distant_deadline_does_not_pair_across_windows(self) -> None:
        filler = "".join(f"2026-09-21T06:00:{10 + i:02d}Z filler line {i}\n" for i in range(12))
        log = "modal-client: Authorization check failed\n" + filler + 'status = StatusCode.DEADLINE_EXCEEDED\n'
        self.assertEqual(modal_annotations(log), [])


if __name__ == "__main__":
    unittest.main()
