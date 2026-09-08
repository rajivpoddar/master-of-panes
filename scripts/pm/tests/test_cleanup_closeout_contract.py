from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets"
SWEEP = SHARED / "claude" / "skills" / "pr-state-sweep" / "scripts" / "sweep.sh"
WRITER = SHARED / "claude" / "scripts" / "pm-cleanup-pr.py"
RUNTIME_OBSERVATION = Path("/Users/rajiv/.claude/control_plane/runtime_observation.py")
OLD_SWEEP = Path("/Users/rajiv/.claude/skills/pr-state-sweep/scripts/sweep.sh")

PR = 7655
HEAD = "a" * 40
MERGE_COMMIT = "b" * 40
MERGED_PR = {
    "number": PR,
    "title": "cleanup fixture",
    "labels": [
        {"name": "pm-state:merged-cleanup-pending"},
        {"name": "pm-cleanup:needed"},
    ],
    "mergedAt": "2026-09-01T00:00:00Z",
    "headRefOid": HEAD,
    "url": f"https://github.com/heydonna-app/heydonna-app/pull/{PR}",
}


def load_writer():
    spec = importlib.util.spec_from_file_location("canonical_pm_cleanup", WRITER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeCleanupExternal:
    def __init__(self) -> None:
        self.labels = ["pm-state:merged-cleanup-pending", "pm-cleanup:needed"]

    def read_pr(self, request):
        return {
            "number": request["pr"],
            "state": "MERGED",
            "mergeCommit": {"oid": request["merge_commit"]},
            "headRefOid": request["head"],
            "closingIssuesReferences": [],
            "labels": list(self.labels),
        }

    def add_pr_label(self, _request, label):
        if label not in self.labels:
            self.labels.append(label)

    def remove_pr_label(self, _request, label):
        self.labels = [value for value in self.labels if value != label]


class CleanupCloseoutContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.receipt = self.root / "pm-cleanup-receipts.json"
        self.pm_ops_db = self.root / "empty.db"
        self.required_jobs = self.root / "required-ci-jobs.json"
        self.required_jobs.write_text("{}\n", encoding="utf-8")
        self.sentinel = self.root / "sentinel.json"
        self.sentinel.write_text("{}\n", encoding="utf-8")

        gh = self.bin / "gh"
        gh.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "state = sys.argv[sys.argv.index('--state') + 1]\n"
            f"print(json.dumps([] if state == 'open' else [{json.dumps(MERGED_PR)}]))\n",
            encoding="utf-8",
        )
        gh.chmod(0o755)
        curl = self.bin / "curl"
        curl.write_text(
            "#!/usr/bin/env python3\n"
            "print('{\"slots\": []}')\n",
            encoding="utf-8",
        )
        curl.chmod(0o755)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def sweep(self, script: Path) -> str:
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{self.bin}:{env['PATH']}",
                "GH_REPO": "heydonna-app/heydonna-app",
                "PR_SWEEP_REPO_ROOT": str(self.root / "missing-repo"),
                "PM_OPS_DB": str(self.pm_ops_db),
                "MOP_CLEANUP_RECEIPT_PATH": str(self.receipt),
                "PR_SWEEP_RUNTIME_OBSERVATION": str(RUNTIME_OBSERVATION),
                "REQUIRED_CI_JOBS_FILE": str(self.required_jobs),
                "SENTINEL": str(self.sentinel),
                "PR_STATE_SWEEP_SENTINEL": str(self.sentinel),
                "REWORK_PACKET_LEDGER": str(self.root / "missing-ledger.py"),
                "PR_SWEEP_WRITE_SENTINEL": "0",
            }
        )
        completed = subprocess.run(
            ["bash", str(script), "--trigger=manual", "--dry-run"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        return completed.stdout

    def write_status(self, status: str) -> None:
        data = json.loads(self.receipt.read_text(encoding="utf-8"))
        self.assertEqual(len(data), 1)
        next(iter(data.values()))["status"] = status
        self.receipt.write_text(json.dumps(data) + "\n", encoding="utf-8")

    def test_installed_baseline_false_miss_and_current_journal_silence(self) -> None:
        baseline = self.sweep(OLD_SWEEP)
        self.assertIn("PR_CLEANUP_CLOSEOUT_REQUIRED PR#7655", baseline)
        self.assertIn("merged_cleanup_pending_without_start_over_15m", baseline)

        writer = load_writer()
        request = {
            "repository": "heydonna-app/heydonna-app",
            "pr": PR,
            "cleanup_mode": "merged_pr_issue_less",
            "head": HEAD,
            "merge_commit": MERGE_COMMIT,
        }
        external = FakeCleanupExternal()
        result = writer.run(request, receipt_path=self.receipt, external=external)
        replay = writer.run(request, receipt_path=self.receipt, external=external)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(replay["idempotent"])
        journal = json.loads(self.receipt.read_text(encoding="utf-8"))
        self.assertEqual(len(journal), 1)

        corrected = self.sweep(SWEEP)
        self.assertNotIn("PR_CLEANUP_CLOSEOUT_REQUIRED PR#7655", corrected)

        self.write_status("processing")
        in_progress = self.sweep(SWEEP)
        self.assertNotIn("PR_CLEANUP_CLOSEOUT_REQUIRED PR#7655", in_progress)

    def test_uncertain_and_missing_current_head_remain_fail_closed(self) -> None:
        writer = load_writer()
        writer.run(
            {
                "repository": "heydonna-app/heydonna-app",
                "pr": PR,
                "cleanup_mode": "merged_pr_issue_less",
                "head": HEAD,
                "merge_commit": MERGE_COMMIT,
            },
            receipt_path=self.receipt,
            external=FakeCleanupExternal(),
        )
        self.write_status("ambiguous")
        uncertain = self.sweep(SWEEP)
        self.assertIn("reason=cleanup_current_head_uncertain", uncertain)
        self.assertNotIn("merged_cleanup_pending_without_start_over_15m", uncertain)

        self.receipt.unlink()
        missing = self.sweep(SWEEP)
        self.assertIn("PR_CLEANUP_CLOSEOUT_REQUIRED PR#7655", missing)
        self.assertIn("merged_cleanup_pending_without_start_over_15m", missing)

    def test_manifest_maps_both_current_cleanup_boundaries(self) -> None:
        manifest = json.loads((SHARED / "manifest.json").read_text(encoding="utf-8"))
        entries = {entry["source_path"]: entry for entry in manifest["entries"]}
        self.assertEqual(
            entries["claude/scripts/pm-cleanup-pr.py"]["sha256"],
            __import__("hashlib").sha256(WRITER.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            entries["claude/skills/pr-state-sweep/scripts/sweep.sh"]["sha256"],
            __import__("hashlib").sha256(SWEEP.read_bytes()).hexdigest(),
        )
        for source_path in entries:
            if source_path in {
                "claude/scripts/pm-cleanup-pr.py",
                "claude/skills/pr-state-sweep/scripts/sweep.sh",
            }:
                source = SHARED / source_path
                self.assertTrue(source.is_file() and not source.is_symlink())
                self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o755)


if __name__ == "__main__":
    unittest.main()
