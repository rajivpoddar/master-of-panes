from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "install-release.py"
SPEC = importlib.util.spec_from_file_location("install_release", MODULE_PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class ReleaseInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old = self.root / "releases" / "old"
        self.new = self.root / "releases" / "new"
        self.current = self.root / "current"
        self.old.mkdir(parents=True)
        self.new.mkdir(parents=True)
        (self.old / "server.js").write_text("old\n")
        (self.new / "server.js").write_text("new\n")
        for release in (self.old, self.new):
            server = release / "dist" / "server.js"
            server.parent.mkdir(parents=True)
            server.write_text("server\n")
            server.chmod(0o644)
            helper = release / "scripts" / "release-slot-reset-and-ack.py"
            helper.parent.mkdir(parents=True)
            helper.write_text("#!/usr/bin/env python3\n")
            helper.chmod(0o755)
        self.current.parent.mkdir(exist_ok=True)
        os.symlink(self.old, self.current)
        self.delete_file = self.root / "legacy" / "planner.py"
        self.delete_file.parent.mkdir()
        self.delete_file.write_text("legacy\n")
        self.delete_file.chmod(0o755)
        self.delete_link = self.root / "legacy" / "planner-link"
        os.symlink(self.delete_file, self.delete_link)
        self.bundle = self.root / "rollback"
        self.restart_count = 0

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_activation(self, *, health=None, canary=None):
        def restart():
            self.restart_count += 1

        return module.activate(
            release_dir=self.new,
            current=self.current,
            expected_old=self.old,
            delete_targets=[self.delete_file, self.delete_link],
            rollback_bundle=self.bundle,
            restart=restart,
            health=health or (lambda: {"status": 200}),
            canary=canary or (lambda: {"slots": []}),
        )

    def test_atomic_switch_delete_after_readiness_and_check(self):
        def canary():
            self.assertTrue(self.delete_file.exists())
            self.assertTrue(self.delete_link.is_symlink())
            return {"slots": []}

        result = self.run_activation(canary=canary)
        self.assertEqual(result["status"], "ACTIVATED")
        self.assertEqual(self.current.resolve(), self.new.resolve())
        self.assertFalse(self.delete_file.exists())
        self.assertFalse(self.delete_link.exists() or self.delete_link.is_symlink())
        self.assertEqual(self.restart_count, 1)
        checked = module.check_install(
            release_dir=self.new,
            current=self.current,
            delete_targets=[self.delete_file, self.delete_link],
            keep_targets=[self.new / "server.js"],
            rollback_bundle=self.bundle,
        )
        self.assertEqual(checked["status"], "PASS")
        self.assertTrue(self.old.joinpath("server.js").exists())

    def test_repeated_check_is_idempotent_after_activation(self):
        self.run_activation()
        first = module.check_install(
            release_dir=self.new,
            current=self.current,
            delete_targets=[self.delete_file, self.delete_link],
            keep_targets=[self.new / "server.js"],
            rollback_bundle=self.bundle,
        )
        second = module.check_install(
            release_dir=self.new,
            current=self.current,
            delete_targets=[self.delete_file, self.delete_link],
            keep_targets=[self.new / "server.js"],
            rollback_bundle=self.bundle,
        )
        self.assertEqual(first, second)

    def test_late_health_failure_restores_pointer_bytes_mode_and_link(self):
        old_mode = stat.S_IMODE(self.delete_file.stat().st_mode)
        old_link = os.readlink(self.delete_link)
        health_calls = 0

        def health():
            nonlocal health_calls
            health_calls += 1
            if health_calls == 1:
                raise RuntimeError("health down")
            return {"status": 200}

        with self.assertRaises(module.InstallerError):
            self.run_activation(health=health)
        self.assertEqual(self.current.resolve(), self.old.resolve())
        self.assertEqual(self.delete_file.read_text(), "legacy\n")
        self.assertEqual(stat.S_IMODE(self.delete_file.stat().st_mode), old_mode)
        self.assertEqual(os.readlink(self.delete_link), old_link)
        self.assertEqual(self.restart_count, 2)

    def test_canary_failure_restores_everything(self):
        with self.assertRaises(module.InstallerError):
            self.run_activation(canary=lambda: (_ for _ in ()).throw(RuntimeError("canary down")))
        self.assertEqual(self.current.resolve(), self.old.resolve())
        self.assertTrue(self.delete_file.exists())

    def test_delete_target_drift_refuses_cleanup_and_restores(self):
        def canary():
            self.delete_file.write_text("changed after capture\n")
            return {"slots": []}

        with self.assertRaises(module.InstallerError):
            self.run_activation(canary=canary)
        self.assertEqual(self.current.resolve(), self.old.resolve())
        self.assertEqual(self.delete_file.read_text(), "legacy\n")

    def test_traversal_and_directory_deletion_refused(self):
        with self.assertRaises(module.InstallerError):
            module._safe_relative("../../etc/passwd")
        directory = self.root / "directory"
        directory.mkdir()
        with self.assertRaises(module.InstallerError):
            module.create_rollback_bundle([directory], self.root / "bad-bundle")

    def test_rollback_bundle_is_owner_only_and_records_absence(self):
        absent = self.root / "legacy" / "absent"
        manifest = module.create_rollback_bundle([absent], self.bundle)
        self.assertFalse(manifest["entries"][0]["present"])
        self.assertEqual(stat.S_IMODE(self.bundle.stat().st_mode), 0o700)

    def test_activation_refuses_release_missing_required_helper_before_switch(self):
        helper = self.new / "scripts" / "release-slot-reset-and-ack.py"
        helper.unlink()
        with self.assertRaisesRegex(module.InstallerError, "required runtime file"):
            self.run_activation()
        self.assertEqual(self.current.resolve(), self.old.resolve())
        self.assertEqual(self.restart_count, 0)

    def test_activation_refuses_release_missing_server_before_switch(self):
        (self.new / "dist" / "server.js").unlink()
        with self.assertRaisesRegex(module.InstallerError, "required runtime file"):
            self.run_activation()
        self.assertEqual(self.current.resolve(), self.old.resolve())
        self.assertEqual(self.restart_count, 0)

    def test_atomic_switch_refuses_dangling_expected_release(self):
        missing = self.root / "releases" / "missing"
        dangling = self.root / "dangling-current"
        os.symlink(missing, dangling)
        with self.assertRaisesRegex(module.InstallerError, "current release"):
            module.atomic_switch(dangling, self.new, missing)
        self.assertEqual(os.readlink(dangling), str(missing))


class SharedAssetInstallCliTests(unittest.TestCase):
    SELECTED = "/Users/rajiv/.claude/hooks/validate-codex-review-agent-result.py"
    ALIAS = "/Users/rajiv/.claude/hooks/validate-codex-review-agent-result-alias.py"
    OTHER = "/Users/rajiv/.claude/rules/unrelated.md"
    COMPAT = "/Users/rajiv/.claude/hooks/legacy-validator.py"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.release_root = self.root / "releases"
        self.release = self.release_root / "candidate"
        self.shared = self.release / "scripts" / "pm" / "shared-assets"
        self.sources = self.shared / "claude" / "hooks"
        self.sources.mkdir(parents=True)
        self.target_root = self.root / "installed"
        self.script = MODULE_PATH
        self.selected_bytes = b"#!/usr/bin/env python3\nselected candidate\n"
        self.other_bytes = b"unrelated candidate\n"
        self.compat_bytes = b"compatibility backup source\n"
        sources = [
            ("claude/hooks/validator.py", self.selected_bytes, 0o755, self.SELECTED, [self.ALIAS]),
            ("claude/hooks/unrelated.md", self.other_bytes, 0o640, self.OTHER, []),
        ]
        entries = []
        for source, content, mode, target, aliases in sources:
            source_path = self.shared / source
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_bytes(content)
            source_path.chmod(mode)
            entries.append(
                {
                    "canonical_target": target,
                    "additional_targets": aliases,
                    "dependencies": [],
                    "dependency_status": "closed",
                    "mode": mode,
                    "ownership_class": "shared-asset-install-cli-test",
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "source_path": source,
                }
            )
        compatibility_source = self.shared / "claude" / "hooks" / "compat.py"
        compatibility_source.write_bytes(self.compat_bytes)
        compatibility_source.chmod(0o600)
        manifest = {
            "schema": "mop_shared_operational_assets",
            "version": 1,
            "entries": sorted(entries, key=lambda entry: entry["source_path"]),
            "inventory": {"selected_count": len(entries)},
            "rollback_compatibility": [
                {
                    "canonical_target": self.COMPAT,
                    "mode": 0o600,
                    "preimage_modes": [0o600],
                    "sha256": hashlib.sha256(self.compat_bytes).hexdigest(),
                    "source_path": "claude/hooks/compat.py",
                }
            ],
        }
        manifest_path = self.shared / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def installed_path(self, declared_target: str) -> Path:
        return self.target_root / declared_target.lstrip("/")

    def seed_targets(self) -> dict[str, tuple[bytes, int]]:
        baselines = {
            self.SELECTED: (b"old selected bytes\n", 0o600),
            self.ALIAS: (b"old alias bytes\n", 0o604),
            self.OTHER: (b"old unrelated bytes\n", 0o640),
            self.COMPAT: (b"old compatibility target\n", 0o644),
        }
        for target, (content, mode) in baselines.items():
            path = self.installed_path(target)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(mode)
        return baselines

    def run_cli(self, mode: str, *, target: str | None = None, rollback: Path | None = None, env=None):
        command = [
            sys.executable,
            str(self.script),
            mode,
            "--release-root",
            str(self.release_root),
            "--candidate",
            "candidate",
            "--rollback-bundle",
            str(rollback or (self.root / "rollback")),
            "--shared-assets-root",
            str(self.target_root),
        ]
        if target is not None:
            command.extend(["--shared-target", target])
        return subprocess.run(command, text=True, capture_output=True, env=env)

    def file_state(self, target: str) -> tuple[bytes, int]:
        path = self.installed_path(target)
        return path.read_bytes(), stat.S_IMODE(path.stat().st_mode)

    def test_cli_installs_and_checks_only_exact_selected_destination(self):
        baseline = self.seed_targets()
        rollback = self.root / "selected-rollback"
        installed = self.run_cli("shared-install", target=self.SELECTED, rollback=rollback)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        result = json.loads(installed.stdout)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["targets"], [str(self.installed_path(self.SELECTED))])
        self.assertEqual(self.file_state(self.SELECTED), (self.selected_bytes, 0o755))
        for target in (self.ALIAS, self.OTHER, self.COMPAT):
            self.assertEqual(self.file_state(target), baseline[target])

        checked = self.run_cli("shared-check", target=self.SELECTED, rollback=rollback)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        check_result = json.loads(checked.stdout)
        self.assertEqual(check_result["count"], 1)
        self.assertEqual(check_result["targets"], [str(self.installed_path(self.SELECTED))])

        rollback_manifest = json.loads((rollback / "ROLLBACK_MANIFEST.json").read_text())
        self.assertEqual([entry["path"] for entry in rollback_manifest["entries"]], [str(self.installed_path(self.SELECTED))])
        self.assertEqual(rollback_manifest.get("compatibility_entries", []), [])
        self.assertFalse((rollback / "compatibility").exists())

        self.target_root = self.root / "alias-installed"
        alias_baseline = self.seed_targets()
        alias_rollback = self.root / "alias-rollback"
        alias_install = self.run_cli("shared-install", target=self.ALIAS, rollback=alias_rollback)
        self.assertEqual(alias_install.returncode, 0, alias_install.stderr)
        self.assertEqual(json.loads(alias_install.stdout)["count"], 1)
        self.assertEqual(self.file_state(self.ALIAS), (self.selected_bytes, 0o755))
        for target in (self.SELECTED, self.OTHER, self.COMPAT):
            self.assertEqual(self.file_state(target), alias_baseline[target])

    def test_cli_rejects_empty_unknown_and_relative_selection_before_effects(self):
        baseline = self.seed_targets()
        invalid_targets = ("", "/Users/rajiv/.claude/hooks/not-declared.py", "relative/target.py")
        for index, target in enumerate(invalid_targets):
            with self.subTest(target=target):
                rollback = self.root / f"invalid-rollback-{index}"
                result = self.run_cli("shared-install", target=target, rollback=rollback)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("shared target selection", result.stderr)
                self.assertFalse(rollback.exists())
                for observed, expected in baseline.items():
                    self.assertEqual(self.file_state(observed), expected)

                checked = self.run_cli("shared-check", target=target, rollback=rollback)
                self.assertNotEqual(checked.returncode, 0)
                self.assertIn("shared target selection", checked.stderr)
                for observed, expected in baseline.items():
                    self.assertEqual(self.file_state(observed), expected)

    def test_cli_failure_after_selected_replace_restores_only_selected_preimage(self):
        baseline = self.seed_targets()
        rollback = self.root / "failure-rollback"
        target_parent = self.installed_path(self.SELECTED).parent
        injector = self.root / "injector"
        injector.mkdir()
        (injector / "sitecustomize.py").write_text(
            "import os, stat\n"
            "_path = os.environ['MOP_TEST_FAIL_FSYNC_DIRECTORY']\n"
            "_target = os.stat(_path)\n"
            "_real_fsync = os.fsync\n"
            "_failed = False\n"
            "def _fsync(fd):\n"
            "    global _failed\n"
            "    observed = os.fstat(fd)\n"
            "    if not _failed and stat.S_ISDIR(observed.st_mode) and (observed.st_dev, observed.st_ino) == (_target.st_dev, _target.st_ino):\n"
            "        _failed = True\n"
            "        raise OSError('injected selected-target directory sync failure')\n"
            "    return _real_fsync(fd)\n"
            "os.fsync = _fsync\n",
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["MOP_TEST_FAIL_FSYNC_DIRECTORY"] = str(target_parent)
        existing_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(injector) + (os.pathsep + existing_pythonpath if existing_pythonpath else "")

        result = self.run_cli("shared-install", target=self.SELECTED, rollback=rollback, env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("baseline restored", result.stderr)
        self.assertIn("injected selected-target directory sync failure", result.stderr)
        self.assertEqual(self.file_state(self.SELECTED), baseline[self.SELECTED])
        for target in (self.ALIAS, self.OTHER, self.COMPAT):
            self.assertEqual(self.file_state(target), baseline[target])
        rollback_manifest = json.loads((rollback / "ROLLBACK_MANIFEST.json").read_text())
        self.assertEqual([entry["path"] for entry in rollback_manifest["entries"]], [str(self.installed_path(self.SELECTED))])
        self.assertEqual(rollback_manifest["entries"][0]["sha256"], hashlib.sha256(baseline[self.SELECTED][0]).hexdigest())
        self.assertEqual(rollback_manifest["entries"][0]["mode"], baseline[self.SELECTED][1])
        self.assertEqual(rollback_manifest.get("compatibility_entries", []), [])

    def test_cli_without_selector_keeps_all_asset_and_compatibility_behavior(self):
        result = self.run_cli("shared-install", rollback=self.root / "all-rollback")
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = json.loads(result.stdout)
        expected_targets = sorted(str(self.installed_path(target)) for target in (self.SELECTED, self.ALIAS, self.OTHER))
        self.assertEqual(installed["count"], 3)
        self.assertEqual(sorted(installed["targets"]), expected_targets)
        self.assertEqual(self.file_state(self.SELECTED), (self.selected_bytes, 0o755))
        self.assertEqual(self.file_state(self.ALIAS), (self.selected_bytes, 0o755))
        self.assertEqual(self.file_state(self.OTHER), (self.other_bytes, 0o640))

        checked = self.run_cli("shared-check", rollback=self.root / "all-rollback")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        check_result = json.loads(checked.stdout)
        self.assertEqual(check_result["count"], 3)
        rollback_manifest = json.loads((self.root / "all-rollback" / "ROLLBACK_MANIFEST.json").read_text())
        self.assertEqual(len(rollback_manifest["compatibility_entries"]), 1)


if __name__ == "__main__":
    unittest.main()
