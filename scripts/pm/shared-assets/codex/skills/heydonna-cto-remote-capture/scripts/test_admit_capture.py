#!/usr/bin/env python3
"""Hermetic focused proof for the CTO capture admission edge.

All GitHub access goes through a stub `gh` so nothing is dispatched for real.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE / "admit_capture.py"

PR = 7644
HEAD = "fe3d09da11d9fe16483bd604f47f20bcab20c899"
BRANCH = "fix/7435-ris-commission-fields-reimpl"
SOURCE_RUN = 34617909102
CONTROL = "8213ce0cf83a832e842c714c0d0d6b9ebd8712ca"
CAUSE = "SHARED_STRICT_FIXTURE_CORPUS_MISS_FORMATTED_CONTENT_READINESS_NOT_PR7644_CANDIDATE_CAUSED"

STUB = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
fx = json.load(open(os.environ["HD_GH_FIXTURE"]))
log = os.environ.get("HD_GH_LOG")
if args[:2] == ["pr", "view"]:
    print(json.dumps(fx["pr"])); raise SystemExit(0)
if args[:2] == ["run", "view"]:
    print(json.dumps(fx["source_run"])); raise SystemExit(0)
if args[:2] == ["run", "list"]:
    print(json.dumps(fx["capture_runs"])); raise SystemExit(0)
if args[:2] == ["workflow", "run"]:
    if log:
        open(log, "a").write(" ".join(args) + "\\n")
    raise SystemExit(0)
raise SystemExit(1)
"""


def terminal(**overrides):
    data = {
        "pr_number": PR, "head_sha": HEAD, "head_branch": BRANCH,
        "source_run": SOURCE_RUN, "source_attempt": 1, "control_sha": CONTROL,
        "capture_profile": "auto-process-only", "cause": CAUSE, "fixture_miss": True,
    }
    data.update(overrides)
    return data


def legacy_receipt_only_admission(*, ci_verdict: str) -> str:
    """The retired predicate: capture is admitted only when a CI verdict proves
    'capture-required'. PR7644's exact-head CI is plain SUCCESS, so it refuses."""
    if ci_verdict == "capture_required":
        return "READY_TO_CAPTURE"
    return "REMOTE_CAPTURE_RECOVERY_INVALID/source_e2e_run_does_not_prove_exact_head_fixture_miss"


class CaptureAdmissionProof(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.stub = self.dir / "gh"
        self.stub.write_text(STUB)
        self.stub.chmod(0o755)
        self.log = self.dir / "gh.log"
        self.fixture = self.dir / "fixture.json"

    def tearDown(self):
        self.tmp.cleanup()

    def fixture_with(self, *, pr=None, source_run=None, capture_runs=None):
        self.fixture.write_text(json.dumps({
            "pr": pr if pr is not None else {"state": "OPEN", "headRefOid": HEAD, "headRefName": BRANCH},
            "source_run": source_run if source_run is not None else {"headSha": HEAD, "attempt": 1},
            "capture_runs": capture_runs or [],
        }))

    def run_tool(self, term, *, apply=False):
        path = self.dir / "terminal.json"
        path.write_text(json.dumps(term))
        env = dict(os.environ, HD_GH_BIN=str(self.stub), HD_GH_FIXTURE=str(self.fixture),
                   HD_GH_LOG=str(self.log))
        argv = [sys.executable, str(TOOL), "--terminal", str(path)]
        if apply:
            argv.append("--apply")
        proc = subprocess.run(argv, capture_output=True, text=True, env=env, check=False)
        return proc.returncode, proc.stdout.strip()

    # ---- RED: the retired receipt-only predicate refuses this exact tuple ----
    def test_red_obsolete_predicate_refuses_the_verified_tuple(self):
        self.assertEqual(
            "REMOTE_CAPTURE_RECOVERY_INVALID/source_e2e_run_does_not_prove_exact_head_fixture_miss",
            legacy_receipt_only_admission(ci_verdict="success"),
        )

    # ---- GREEN: the replacement admits it, read-only ----
    def test_green_dry_run_is_ready_to_capture_and_read_only(self):
        self.fixture_with()
        rc, out = self.run_tool(terminal())
        self.assertEqual(0, rc, out)
        self.assertEqual("READY_TO_CAPTURE", json.loads(out)["status"])
        self.assertFalse(self.log.exists(), "dry-run must not dispatch anything")

    # ---- negatives ----
    def test_refuses_head_drift(self):
        self.fixture_with(pr={"state": "OPEN", "headRefOid": "0" * 40, "headRefName": BRANCH})
        rc, out = self.run_tool(terminal())
        self.assertEqual(2, rc)
        self.assertEqual("head_drift", json.loads(out)["reason"])

    def test_refuses_source_run_head_mismatch(self):
        self.fixture_with(source_run={"headSha": "1" * 40, "attempt": 1})
        rc, out = self.run_tool(terminal())
        self.assertEqual(2, rc)
        self.assertEqual("source_run_head_mismatch", json.loads(out)["reason"])

    def test_refuses_non_fixture_evidence(self):
        self.fixture_with()
        rc, out = self.run_tool(terminal(fixture_miss=False))
        self.assertEqual(2, rc)
        self.assertEqual("non_fixture_evidence", json.loads(out)["reason"])

    def test_refuses_duplicate_active_capture(self):
        self.fixture_with(capture_runs=[{"databaseId": 1, "headSha": HEAD,
                                         "status": "in_progress", "conclusion": None}])
        rc, out = self.run_tool(terminal())
        self.assertEqual(2, rc)
        self.assertEqual("duplicate_active_capture", json.loads(out)["reason"])

    def test_refuses_duplicate_successful_capture(self):
        self.fixture_with(capture_runs=[{"databaseId": 2, "headSha": HEAD,
                                         "status": "completed", "conclusion": "success"}])
        rc, out = self.run_tool(terminal())
        self.assertEqual(2, rc)
        self.assertEqual("duplicate_successful_capture", json.loads(out)["reason"])

    # ---- armed path: exactly one canonical launch, then idempotent ----
    def test_armed_dispatch_is_single_and_idempotent(self):
        self.fixture_with()
        rc, out = self.run_tool(terminal(), apply=True)
        self.assertEqual(0, rc, out)
        payload = json.loads(out)
        self.assertEqual("DISPATCHED", payload["status"])
        self.assertTrue(payload["argv"].count("workflow") >= 1)
        self.assertIn("E2E LLM Proxy Capture (manual)", payload["argv"])
        self.assertIn(f"head_sha={HEAD}", payload["argv"])
        self.assertIn("capture_profile=auto-process-only", payload["argv"])
        launches = self.log.read_text().strip().splitlines()
        self.assertEqual(1, len(launches), f"expected exactly one launch, got {launches}")

        # After launch the duplicate fence must stop a second dispatch.
        self.fixture_with(capture_runs=[{"databaseId": 3, "headSha": HEAD,
                                         "status": "queued", "conclusion": None}])
        rc2, out2 = self.run_tool(terminal(), apply=True)
        self.assertEqual(2, rc2)
        self.assertEqual("duplicate_active_capture", json.loads(out2)["reason"])
        self.assertEqual(1, len(self.log.read_text().strip().splitlines()))

    # ---- no legacy dependency in the tool source ----
    def test_no_pm_state_or_legacy_receipt_dependency(self):
        # Strip docstrings and comments first: prose may NAME the retired paths
        # (explaining why they are not used); only real code dependencies count.
        import ast
        source = TOOL.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                body = getattr(node, "body", [])
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    node.body = body[1:] or [ast.Pass()]
        src = ast.unparse(tree)
        for forbidden in ("pm-ops", "request-budgeted-remote-capture", "capture-required",
                          "pm-state", "retry-budget", "affected-test"):
            self.assertNotIn(forbidden, src, f"tool must not depend on {forbidden}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
