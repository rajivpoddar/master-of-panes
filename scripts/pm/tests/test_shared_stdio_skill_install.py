from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).parents[3]
MODULE_PATH = REPO_ROOT / "scripts" / "install-release.py"
SPEC = importlib.util.spec_from_file_location("install_release_stdio", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


FAKE_CODEX = textwrap.dedent(
    """
    #!/usr/bin/env python3
    import json, os, sys, time
    mode = os.environ.get("FAKE_MODE", "success")
    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        request_id = request.get("id")
        if request_id is None:
            continue
        if method == "initialize":
            response = {"jsonrpc": "2.0", "id": request_id, "result": {}}
        elif method == "thread/queue/add" and mode == "queue-fail":
            response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": "queue unavailable"}}
        elif method == "thread/queue/add" and mode == "uncertain":
            time.sleep(2)
            continue
        elif method == "thread/queue/add":
            response = {"jsonrpc": "2.0", "id": request_id, "result": {"queuedSubmission": {"id": "q-1", "clientUserMessageId": request["params"]["clientUserMessageId"]}}}
        elif method == "thread/queue/start" and mode == "resume":
            response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32600, "message": "resume the thread before starting a queued message"}}
        elif method == "thread/queue/start" and mode == "queued":
            response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": "start unavailable"}}
        else:
            response = {"jsonrpc": "2.0", "id": request_id, "result": {}}
        print(json.dumps(response), flush=True)
    """
).lstrip()


class SharedStdioSkillInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.release = self.root / "release"
        shutil.copytree(REPO_ROOT / "scripts" / "pm" / "shared-assets", self.release / "scripts" / "pm" / "shared-assets")
        self.targets = self.root / "targets"
        self.rollback = self.root / "rollback"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def install(self, *, fail_after: int | None = None) -> dict:
        return MODULE.install_shared_assets(
            release_dir=self.release,
            target_root=self.targets,
            rollback_bundle=self.rollback if fail_after is None else self.root / f"rollback-{fail_after}",
            fail_after=fail_after,
        )

    def test_manifest_is_deterministic_and_source_parity_is_exact(self) -> None:
        manifest = MODULE._load_shared_manifest(self.release)
        self.assertEqual(manifest["entries"], sorted(manifest["entries"], key=lambda item: item["source_path"]))
        self.assertEqual(manifest["inventory"]["selected_count"], 42)
        self.assertEqual(manifest["inventory"]["ambiguous"], [])
        result = self.install()
        self.assertEqual(result["status"], "SHARED_ASSETS_INSTALLED")
        self.assertEqual(MODULE.check_shared_assets(release_dir=self.release, target_root=self.targets)["status"], "SHARED_ASSETS_PASS")
        for entry in manifest["entries"]:
            source = self.release / "scripts" / "pm" / "shared-assets" / entry["source_path"]
            target = self.targets / entry["canonical_target"].lstrip("/")
            self.assertEqual(source.read_bytes(), target.read_bytes())
            self.assertEqual(stat.S_IMODE(source.stat().st_mode), stat.S_IMODE(target.stat().st_mode))

    def test_release_conveyor_matrix_has_guarded_control_plane_refusal(self) -> None:
        shared = self.release / "scripts" / "pm" / "shared-assets"
        contract = (shared / "codex" / "skills" / "_shared" / "release-conveyor-contract.md").read_text(encoding="utf-8")
        skill = (shared / "codex" / "skills" / "heydonna-control-plane-repair" / "SKILL.md").read_text(encoding="utf-8")
        wake_sop = (shared / "codex" / "monitors" / "heydonna-pm-chat" / "WAKE_SOP.md").read_text(encoding="utf-8")
        block = re.search(r"```json\n(\{.*?\})\n```", contract, flags=re.DOTALL)
        self.assertIsNotNone(block)
        matrix = json.loads(block.group(1))
        self.assertEqual(matrix["version"], 2)
        scenarios = matrix["scenarios"]
        self.assertEqual(len(scenarios), 7)
        candidate = scenarios["control_plane_candidate"]
        self.assertEqual(candidate["owner"], "MOP_IMPLEMENTATION_TASK_01a04154")
        self.assertEqual(candidate["action"], "return_candidate_to_cto_inline")
        self.assertEqual(candidate["wake"], "cto_inline_review")
        block = scenarios["control_plane_block"]
        self.assertEqual(block["owner"], "MOP_IMPLEMENTATION_TASK_01a04154")
        self.assertEqual(block["action"], "return_bounded_rework_to_same_mop_task")
        approval = scenarios["control_plane_approval"]
        self.assertEqual(approval["owner"], "CTO_DECISIONS")
        self.assertEqual(approval["action"], "publish_rollout_verify_and_notify_pm")
        self.assertNotIn("at most 15 minutes", contract)
        self.assertIn("PM does not retry", contract)
        self.assertIn("journal", contract)
        self.assertIn("literal pre/post label", contract)
        self.assertIn("candidate packet directly to CTO Decisions", skill)
        self.assertIn("one candidate-only packet for CTO inline review", wake_sop)
        self.assertIn("approval is published and rolled out only by CTO Decisions", wake_sop)
        self.assertNotIn("same execution owner continues through", wake_sop)

    def test_unlisted_file_preserved_and_late_failure_restores_absent_baseline(self) -> None:
        unlisted = self.targets / "Users" / "rajiv" / ".codex" / "unlisted.txt"
        unlisted.parent.mkdir(parents=True)
        unlisted.write_text("keep\n", encoding="utf-8")
        with self.assertRaises(MODULE.InstallerError):
            self.install(fail_after=1)
        self.assertEqual(unlisted.read_text(encoding="utf-8"), "keep\n")
        manifest = MODULE._load_shared_manifest(self.release)
        for entry in manifest["entries"]:
            target = self.targets / entry["canonical_target"].lstrip("/")
            self.assertFalse(target.exists() or target.is_symlink())
        self.assertFalse(list(self.targets.rglob("*.shared.*.tmp")))

    def test_mapped_target_invocation_preserves_delivery_states_without_retry(self) -> None:
        self.install()
        target = self.targets / "Users" / "rajiv" / ".codex" / "skills" / "codex-stdio-send-message" / "scripts" / "send_message.py"
        fake = self.root / "fake-codex"
        fake.write_text(FAKE_CODEX, encoding="utf-8")
        fake.chmod(0o755)
        common = ["python3", str(target), "--thread-id", "thread-1", "--dedup-key", "event-1", "--message", "hello", "--codex-bin", str(fake), "--timeout-seconds", "1"]
        expected = {
            "success": (0, "delivered"),
            "resume": (0, "queued_for_task_consumption"),
            "queued": (5, "queued"),
            "queue-fail": (2, "unavailable"),
            "uncertain": (3, "uncertain"),
        }
        for mode, (code, status) in expected.items():
            result = subprocess.run(common, env={**os.environ, "FAKE_MODE": mode}, capture_output=True, text=True, timeout=4)
            self.assertEqual(result.returncode, code, mode)
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["status"], status, mode)
            if status in {"delivered", "queued_for_task_consumption", "queued"}:
                self.assertEqual(receipt["queuedSubmissionId"], "q-1")


if __name__ == "__main__":
    unittest.main()
