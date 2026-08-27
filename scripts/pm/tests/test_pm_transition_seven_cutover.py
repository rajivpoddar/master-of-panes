from __future__ import annotations

import importlib.util
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets"
INSTALLER_PATH = ROOT / "scripts" / "install-release.py"
SPEC = importlib.util.spec_from_file_location("install_release_seven", INSTALLER_PATH)
assert SPEC and SPEC.loader
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)

RETIRED = ("claim-slot", "rebind-slot", "release-slot")
LEGACY = ("slot-ready", "pm-review", "capacity-snapshot", "reconcile-capacity")
RETIRED_UNUSED = ("campaign-status",)


class PmTransitionSevenCutoverTests(unittest.TestCase):
    def test_inventory_retire_only_commands_without_legacy_behavior(self) -> None:
        manifest = INSTALLER._load_shared_manifest(ROOT)
        inventory = manifest["inventory"]["command_inventory"]
        self.assertEqual(manifest["inventory"]["selected_count"], 6)
        by_command = {item["command"]: item for item in inventory}
        self.assertEqual(set(by_command), set(RETIRED + LEGACY + RETIRED_UNUSED))
        for command in RETIRED:
            self.assertTrue(by_command[command]["retired"])
            self.assertEqual(by_command[command]["legacy_callers"], [])
        for command in LEGACY:
            self.assertFalse(by_command[command]["retired"])
            self.assertTrue(by_command[command]["legacy_callers"])
        for command in RETIRED_UNUSED:
            self.assertTrue(by_command[command]["retired"])
            self.assertEqual(by_command[command]["legacy_callers"], [])

    def test_red_broad_guard_would_strand_wrapper_owned_commands(self) -> None:
        old_guard = "slot-ready|pm-review|capacity-snapshot|reconcile-capacity)"
        shell = (SHARED / "claude/scripts/pm-transition.sh").read_text(encoding="utf-8")
        self.assertNotIn(old_guard, shell)
        self.assertIn("slot-ready) cmd_slot_ready", shell)
        self.assertIn("pm-review) family2_pm_review", shell)
        self.assertIn("capacity-snapshot) cmd_capacity_snapshot", shell)
        self.assertIn("reconcile-capacity) cmd_reconcile_capacity", shell)

    def test_retired_commands_refuse_before_any_legacy_effect(self) -> None:
        shell = SHARED / "claude/scripts/pm-transition.sh"
        for command in RETIRED:
            result = subprocess.run(
                ["bash", str(shell), command],
                env={**os.environ, "PM30M_AUTO_RECONCILE": "0"},
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 167, command)
            self.assertIn("reason=pm_operator_required", result.stderr, command)
            self.assertIn("replacement=/Users/rajiv/.claude/scripts/pm-operator.py", result.stderr, command)

    def test_unused_campaign_status_arm_is_removed(self) -> None:
        shell = SHARED / "claude/scripts/pm-transition.sh"
        result = subprocess.run(["bash", str(shell), "campaign-status"], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown command 'campaign-status'", result.stderr)

    def test_operator_delegation_and_unknown_refusal_remain_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "scripts/pm/control_plane"
            package.mkdir(parents=True)
            for init in (root / "scripts/__init__.py", root / "scripts/pm/__init__.py", package / "__init__.py"):
                init.write_text("", encoding="utf-8")
            recorder = root / "calls.jsonl"
            module = (
                "import json, os\n"
                "def main(args):\n"
                "    with open(os.environ['RECORDER'], 'a', encoding='utf-8') as f: f.write(json.dumps(args)+'\\n')\n"
                "    return 0\n"
            )
            (package / "assignment_boundary.py").write_text(module, encoding="utf-8")
            (root / "scripts/pm/capacity-control.py").write_text(
                "import json, os, sys\n"
                "with open(os.environ['RECORDER'], 'a', encoding='utf-8') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n"
                "def main(): return 0\n",
                encoding="utf-8",
            )
            operator = SHARED / "claude/scripts/pm-operator.py"
            env = {**os.environ, "HEYDONNA_CONTROL_PLANE_RELEASE_ROOT": str(root), "RECORDER": str(recorder)}
            for command in RETIRED:
                result = subprocess.run(
                    ["python3", str(operator), command, "--probe", "value"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(result.returncode, 0, command)
            calls = [json.loads(line) for line in recorder.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([call[2] for call in calls], list(RETIRED))
            explicit_state = str(root / "explicit-state.json")
            result = subprocess.run(
                ["python3", str(operator), "claim-slot", "--slot", "1", "--state", explicit_state],
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0)
            explicit_call = json.loads(recorder.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(explicit_call, ["--state", explicit_state, "claim-slot", "--slot", "1"])
            refused = subprocess.run(
                ["python3", str(operator), "not-cut-over"],
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(refused.returncode, 167)
            self.assertEqual(json.loads(refused.stdout)["reason"], "command_not_cut_over")

    def test_operator_direct_mop_fallback_is_stateless_and_complete(self) -> None:
        requests: list[dict[str, object]] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers["Content-Length"] or "0")
                requests.append({
                    "path": self.path,
                    "authority": self.headers.get("x-heydonna-assignment-authority"),
                    "body": json.loads(self.rfile.read(length)),
                })
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"slot": 1, "occupied": True}).encode())

            def log_message(self, *_args: object) -> None:
                return

        with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            with tempfile.TemporaryDirectory() as directory:
                missing_root = Path(directory) / "release"
                env = {
                    **os.environ,
                    "HEYDONNA_CONTROL_PLANE_RELEASE_ROOT": str(missing_root),
                    "MOP_URL": f"http://127.0.0.1:{server.server_address[1]}",
                }
                result = subprocess.run(
                    [
                        "python3", str(SHARED / "claude/scripts/pm-operator.py"), "claim-slot",
                        "--slot", "1", "--expected-epoch", "7", "--repository-id", "heydonna-app/heydonna-app",
                        "--issue", "7518", "--pr", "7518", "--branch", "fix/7518", "--session-id", "session-1",
                        "--head-sha", "a" * 40, "--work-kind", "coding", "--handoff-id", "handoff-1",
                    ], env=env, capture_output=True, text=True, timeout=5,
                )
                result_rebind = subprocess.run(
                    [
                        "python3", str(SHARED / "claude/scripts/pm-operator.py"), "rebind-slot",
                        "--slot", "1", "--expected-epoch", "7", "--repository-id", "heydonna-app/heydonna-app",
                        "--issue", "7518", "--pr", "7518", "--branch", "fix/7518", "--head-sha", "a" * 40,
                        "--work-kind", "coding", "--handoff-id", "handoff-1", "--claimed-at", "2026-08-26T17:00:00Z",
                        "--new-branch", "fix/7518-rebound", "--new-head-sha", "b" * 40,
                    ], env=env, capture_output=True, text=True, timeout=5,
                )
                result_missing_value = subprocess.run(
                    [
                        "python3", str(SHARED / "claude/scripts/pm-operator.py"), "rebind-slot",
                        "--slot", "1", "--expected-epoch", "7", "--repository-id", "heydonna-app/heydonna-app",
                        "--issue", "7518", "--pr", "7518", "--branch", "fix/7518", "--head-sha", "a" * 40,
                        "--work-kind", "coding", "--handoff-id", "handoff-1", "--claimed-at", "2026-08-26T17:00:00Z",
                        "--new-pr",
                    ], env=env, capture_output=True, text=True, timeout=5,
                )
                result_immutable_claimed_at = subprocess.run(
                    [
                        "python3", str(SHARED / "claude/scripts/pm-operator.py"), "rebind-slot",
                        "--slot", "1", "--expected-epoch", "7", "--repository-id", "heydonna-app/heydonna-app",
                        "--issue", "7518", "--pr", "7518", "--branch", "fix/7518", "--head-sha", "a" * 40,
                        "--work-kind", "coding", "--handoff-id", "handoff-1", "--claimed-at", "2026-08-26T17:00:00Z",
                        "--new-claimed-at", "2026-08-26T18:00:00Z",
                    ], env=env, capture_output=True, text=True, timeout=5,
                )
            server.shutdown()
            thread.join(timeout=2)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result_rebind.returncode, 0)
        self.assertEqual(result_missing_value.returncode, 20)
        self.assertEqual(result_immutable_claimed_at.returncode, 20)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["path"], "/slots/1/assign")
        self.assertEqual(requests[0]["authority"], "pm-transition-v1")
        self.assertEqual(requests[0]["body"]["expected_epoch"], 7)
        self.assertEqual(requests[0]["body"]["head_sha"], "a" * 40)
        self.assertEqual(requests[1]["path"], "/slots/1/adopt-issue-claim")
        self.assertEqual(requests[1]["body"]["expected_current_branch"], "fix/7518")
        self.assertEqual(requests[1]["body"]["branch"], "fix/7518-rebound")
        self.assertEqual(requests[1]["body"]["expected_current_head_sha"], "a" * 40)
        self.assertEqual(requests[1]["body"]["head_sha"], "b" * 40)

    def test_scoped_install_preserves_unlisted_files_and_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            shutil.copytree(SHARED, release / "scripts/pm/shared-assets")
            targets = root / "targets"
            keep = targets / "Users/rajiv/.claude/unlisted.txt"
            keep.parent.mkdir(parents=True)
            keep.write_text("preserve\n", encoding="utf-8")
            rollback = root / "rollback"
            with self.assertRaises(INSTALLER.InstallerError):
                INSTALLER.install_shared_assets(release_dir=release, target_root=targets, rollback_bundle=rollback, fail_after=1)
            self.assertEqual(keep.read_text(encoding="utf-8"), "preserve\n")
            for entry in INSTALLER._load_shared_manifest(release)["entries"]:
                target = targets / entry["canonical_target"].lstrip("/")
                self.assertFalse(target.exists() or target.is_symlink())
            self.assertFalse(list(targets.rglob("*.shared.*.tmp")))


if __name__ == "__main__":
    unittest.main()
