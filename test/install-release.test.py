from __future__ import annotations

import importlib.util
import hashlib
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
        self.shared_release_count = 0

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

    def _write_shared_manifest_release(self, *, entries):
        release = self.root / f"shared-release-{self.shared_release_count}"
        self.shared_release_count += 1
        shared_root = release / "scripts" / "pm" / "shared-assets"
        shared_root.mkdir(parents=True, exist_ok=True)
        for entry in entries:
            source = shared_root / entry["source_path"]
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(entry["payload"])
            source.chmod(entry.get("mode", 0o644))
        manifest_entries = []
        for entry in entries:
            manifest_entry = {
                key: entry[key]
                for key in ("source_path", "canonical_target", "additional_targets", "sha256", "mode")
                if key in entry
            }
            manifest_entry.update(
                {
                    "ownership_class": "test-shared-asset",
                    "dependency_status": "closed",
                    "dependencies": [],
                }
            )
            manifest_entries.append(manifest_entry)
        manifest = {
            "schema": "mop_shared_operational_assets",
            "version": 1,
            "owner": "master-of-panes",
            "inventory": {
                "selected_count": len(manifest_entries),
            },
            "rollback_compatibility": [],
            "entries": sorted(manifest_entries, key=lambda item: item["source_path"]),
        }
        (shared_root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        return release

    def _shared_entry(self, source_path, canonical_target, payload, *, additional_targets=None):
        return {
            "source_path": source_path,
            "canonical_target": canonical_target,
            "additional_targets": additional_targets or [],
            "sha256": hashlib.sha256(payload).hexdigest(),
            "mode": 0o644,
            "payload": payload,
        }

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

    def test_manifest_packages_full_agent_baselines_and_all_rule_targets(self):
        repo = Path(__file__).parents[1]
        result = module.install_shared_assets(
            release_dir=repo,
            target_root=self.root / "fake-target-root",
            rollback_bundle=self.root / "shared-rollback",
        )
        self.assertEqual(result["status"], "SHARED_ASSETS_INSTALLED")
        expected_agents = {
            "claude/agents/plan-agent.md": "123608d3f98d8ca0cfb0bf6c9b96c254b4b92b57dc7b20a85eb838534d761ba8",
            "claude/agents/feature-dev-code-architect.md": "dd5630107d9828eba42cbeba4cda7ecf46f5342480b1cf9e058b7e2ea6788d4f",
            "claude/agents/codex-plan-reviewer.md": "3b459554ad2d540795f09325d32afb7311d91ab68a90ca1075e962f954c31783",
            "claude/agents/codex-code-reviewer.md": "ffcf43eaaad84fb16e1a9cb35b645b0e55c4a092f6d83eccc7f03c563b7ba612",
        }
        manifest = module._load_shared_manifest(repo)
        expected_target_count = sum(len(module._shared_entry_targets(entry)) for entry in manifest["entries"])
        self.assertEqual(result["count"], expected_target_count)
        rule = next(entry for entry in manifest["entries"] if entry["source_path"] == "claude/rules/32-canonical-capture-contract.md")
        self.assertEqual(len(module._shared_entry_targets(rule)), 7)
        for entry in manifest["entries"]:
            source = repo / "scripts" / "pm" / "shared-assets" / entry["source_path"]
            for target_name in module._shared_entry_targets(entry):
                target = module._shared_target_path(target_name, self.root / "fake-target-root")
                self.assertEqual(target.read_bytes(), source.read_bytes())
                self.assertEqual(module.sha256(target), entry["sha256"])
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), entry["mode"])
        for source_path, digest in expected_agents.items():
            source = repo / "scripts" / "pm" / "shared-assets" / source_path
            self.assertEqual(module.sha256(source), digest)

        checked = module.check_shared_assets(release_dir=repo, target_root=self.root / "fake-target-root")
        self.assertEqual(checked["status"], "SHARED_ASSETS_PASS")
        self.assertEqual(checked["count"], expected_target_count)

    def test_shared_asset_failure_restores_primary_and_additional_preimages(self):
        repo = Path(__file__).parents[1]
        manifest = module._load_shared_manifest(repo)
        target_root = self.root / "failure-target-root"
        old = b"preimage\n"
        target_paths = []
        for entry in manifest["entries"]:
            for target_name in module._shared_entry_targets(entry):
                target = module._shared_target_path(target_name, target_root)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(old)
                target.chmod(entry["mode"])
                target_paths.append(target)
        with self.assertRaisesRegex(module.InstallerError, "baseline restored"):
            module.install_shared_assets(
                release_dir=repo,
                target_root=target_root,
                rollback_bundle=self.root / "failure-rollback",
                fail_after=3,
            )
        self.assertTrue(target_paths)
        for target in target_paths:
            self.assertEqual(target.read_bytes(), old)

    def test_duplicate_or_broad_additional_target_refuses_before_writes(self):
        payload = b"rule\n"
        cases = [
            ["/Users/rajiv/.claude/test-rule.md"],
            ["/"],
        ]
        for additional_targets in cases:
            with self.subTest(additional_targets=additional_targets):
                entry = self._shared_entry(
                    "claude/rules/test-rule.md",
                    "/Users/rajiv/.claude/test-rule.md",
                    payload,
                    additional_targets=additional_targets,
                )
                release = self._write_shared_manifest_release(entries=[entry])
                target_root = self.root / ("malformed-" + str(len(additional_targets)))
                with self.assertRaises(module.InstallerError):
                    module.install_shared_assets(
                        release_dir=release,
                        target_root=target_root,
                        rollback_bundle=self.root / "malformed-rollback",
                    )
                self.assertFalse(target_root.exists())

    def test_cross_entry_primary_additional_collision_refuses_before_writes(self):
        first = self._shared_entry(
            "claude/rules/first.md",
            "/Users/rajiv/.claude/first.md",
            b"first\n",
            additional_targets=["/Users/rajiv/.claude/second.md"],
        )
        second = self._shared_entry(
            "claude/rules/second.md",
            "/Users/rajiv/.claude/second.md",
            b"second\n",
        )
        release = self._write_shared_manifest_release(entries=[first, second])
        target_root = self.root / "collision-target-root"
        with self.assertRaisesRegex(module.InstallerError, "duplicate shared asset target"):
            module.install_shared_assets(
                release_dir=release,
                target_root=target_root,
                rollback_bundle=self.root / "collision-rollback",
            )
        self.assertFalse(target_root.exists())


if __name__ == "__main__":
    unittest.main()
