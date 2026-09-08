from __future__ import annotations

import json
import contextlib
import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets"
HOOK = SHARED / "claude" / "hooks" / "pm-context-injector.sh"
SOP = SHARED / "claude" / "skills" / "pm-message-to-action" / "SKILL.md"
INSTALLED_CI_SUCCESS = Path("/Users/rajiv/.claude/scripts/ci-success-reconciliation.py")


class PMContextInjectorSOPTests(unittest.TestCase):
    def run_hook(self, payload: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(HOOK)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ, "HOME": tempfile.gettempdir()},
        )

    def test_sop_covers_all_current_mapping_branches(self) -> None:
        sop = SOP.read_text(encoding="utf-8")
        for phrase in (
            "pm-nudge-processing",
            "codex-comment-processing",
            "alert-processing",
            "ci-failure-investigation",
            "ci-success-reconciliation",
            "capture-alert-processing",
            "survey-report-prompt-miner",
            "customer-artifact-investigator",
            "pm-autoscaler-repair",
            "pr-state-sweep",
            "pm-pr-rescue",
            "cleanup-pr",
            "Quoted history",
            "not a new event",
        ):
            self.assertIn(phrase, sop)
        self.assertNotIn("delegation", sop.lower())
        self.assertNotIn("utilization", sop.lower())

    def test_hook_is_content_independent_and_path_only(self) -> None:
        cwd = "/Users/rajiv/Downloads/projects/heydonna-app"
        first = self.run_hook({"cwd": cwd, "prompt": "NUDGE: slot 4; gh pr merge; CI failed"})
        second = self.run_hook({"cwd": cwd, "prompt": "plain unrelated text"})
        self.assertEqual(first.returncode, 0)
        self.assertEqual(second.returncode, 0)
        self.assertEqual(first.stdout, second.stdout)
        self.assertIn("PM_SOP_PATH_REMINDER", first.stdout)
        self.assertIn("pm-message-to-action/SKILL.md", first.stdout)
        self.assertNotIn("pm-nudge-processing", first.stdout)
        self.assertNotIn("ci-failure-investigation", first.stdout)

    def test_non_pm_scope_and_malformed_input_fail_open_silently(self) -> None:
        outside = self.run_hook({"cwd": "/Users/rajiv/Downloads/projects/heydonna-app-3005", "prompt": "NUDGE: slot 5"})
        malformed = subprocess.run(
            ["bash", str(HOOK)],
            input="not-json",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(outside.returncode, 0)
        self.assertEqual(outside.stdout, "")
        self.assertEqual(malformed.returncode, 0)
        self.assertEqual(malformed.stdout, "")

    def test_hook_has_no_classifier_or_side_effect_writer(self) -> None:
        source = HOOK.read_text(encoding="utf-8")
        for forbidden in ("grep", "pm-ops", "obligation-upsert", "mkdir", "Skill(", "REMINDER=", "/tmp/", ">>"):
            self.assertNotIn(forbidden, source)

    def test_existing_ci_success_materializer_initializes_then_claims_without_sentinel(self) -> None:
        self.assertTrue(INSTALLED_CI_SUCCESS.is_file())
        spec = importlib.util.spec_from_file_location("installed_ci_success", INSTALLED_CI_SUCCESS)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        head = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            args = type("Args", (), {
                "sentinel_dir": directory,
                "guard": "/unused/guard",
                "guard_cwd": directory,
                "gh_bin": "gh",
                "repo": "heydonna-app/heydonna-app",
                "pr": 7655,
                "max_checks": 1,
                "marker_dir": directory,
                "pm_ops": "/unused/pm-ops",
                "merge_ready_alert": "/unused/alert",
            })()
            module.open_candidates = lambda _gh, _repo: [{
                "number": args.pr,
                "headRefOid": head,
                "labels": [{"name": "pm-state:pm-review-pending"}],
                "statusCheckRollup": [],
            }]
            module.exact_green = lambda _guard, _pr, cwd: (head, 123, 456, "exact-head-green")
            module.upsert_ci_reconcile_obligation = lambda *_args, **_kwargs: None
            module.promote_to_merge_ready = lambda *_args, **_kwargs: (False, "not a promotion fixture")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(module.materialize(args), 0)
            sentinel = Path(directory) / "pm-required-ci-reconcile-7655.json"
            self.assertTrue(sentinel.is_file())
            materialized = json.loads(sentinel.read_text(encoding="utf-8"))
            self.assertEqual(materialized["status"], "pending")
            self.assertEqual(materialized["head_sha"], head)
            self.assertEqual(materialized["ci_run_id"], "123")
            self.assertEqual(materialized["e2e_run_id"], "456")

            module.current_head = lambda _gh, _repo, _pr: head
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(module.claim(args), 0)
            claimed = json.loads(sentinel.read_text(encoding="utf-8"))
            self.assertEqual(claimed["status"], "in_progress")
            self.assertEqual(claimed["head_sha"], head)

    def test_existing_ci_success_materializer_duplicate_and_head_mismatch_controls(self) -> None:
        self.assertTrue(INSTALLED_CI_SUCCESS.is_file())
        spec = importlib.util.spec_from_file_location("installed_ci_success_controls", INSTALLED_CI_SUCCESS)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        head = "b" * 40
        with tempfile.TemporaryDirectory() as directory:
            args = type("Args", (), {
                "sentinel_dir": directory,
                "guard": "/unused/guard",
                "guard_cwd": directory,
                "gh_bin": "gh",
                "repo": "heydonna-app/heydonna-app",
                "pr": 7655,
                "max_checks": 1,
                "marker_dir": directory,
                "pm_ops": "/unused/pm-ops",
                "merge_ready_alert": "/unused/alert",
            })()
            sentinel = Path(directory) / "pm-required-ci-reconcile-7655.json"
            sentinel.write_text(json.dumps({"status": "pending", "head_sha": head}) + "\n", encoding="utf-8")
            module.open_candidates = lambda _gh, _repo: [{"number": args.pr, "headRefOid": head, "labels": [], "statusCheckRollup": []}]
            module.upsert_ci_reconcile_obligation = lambda *_args, **_kwargs: None
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(module.materialize(args), 0)
            self.assertEqual(json.loads(sentinel.read_text(encoding="utf-8"))["status"], "pending")
            module.current_head = lambda _gh, _repo, _pr: "c" * 40
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(module.claim(args), 0)
            mismatch = json.loads(sentinel.read_text(encoding="utf-8"))
            self.assertEqual(mismatch["status"], "superseded")
            self.assertEqual(mismatch["resolution"], "head_drift")


if __name__ == "__main__":
    unittest.main()
