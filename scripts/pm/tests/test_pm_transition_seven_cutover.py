from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
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


class PmTransitionSevenCutoverTests(unittest.TestCase):
    def test_inventory_retire_only_commands_without_legacy_behavior(self) -> None:
        manifest = INSTALLER._load_shared_manifest(ROOT)
        inventory = manifest["inventory"]["command_inventory"]
        self.assertEqual(manifest["inventory"]["selected_count"], 6)
        by_command = {item["command"]: item for item in inventory}
        self.assertEqual(set(by_command), set(RETIRED + LEGACY))
        for command in RETIRED:
            self.assertTrue(by_command[command]["retired"])
            self.assertEqual(by_command[command]["legacy_callers"], [])
        for command in LEGACY:
            self.assertFalse(by_command[command]["retired"])
            self.assertTrue(by_command[command]["legacy_callers"])

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
            refused = subprocess.run(
                ["python3", str(operator), "not-cut-over"],
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(refused.returncode, 167)
            self.assertEqual(json.loads(refused.stdout)["reason"], "command_not_cut_over")

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
