from __future__ import annotations

import importlib.util
import json
import os
import stat
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


if __name__ == "__main__":
    unittest.main()
