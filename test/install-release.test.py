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


class PluginCacheRootTests(unittest.TestCase):
    def test_refuses_release_root_inside_claude_plugin_cache(self):
        cache_root = module.CLAUDE_PLUGIN_CACHE / "rajiv-plugins" / "master-of-panes" / "releases"
        with self.assertRaisesRegex(module.InstallerError, "plugin cache"):
            module.refuse_claude_plugin_cache(cache_root, label="--release-root")

    def test_refuses_symlink_outside_cache_that_resolves_into_it(self):
        tmp = Path(tempfile.mkdtemp())
        fake_cache = tmp / "plugins" / "cache"
        (fake_cache / "mop" / "releases").mkdir(parents=True)
        outside = tmp / "share"
        outside.mkdir()
        (outside / "releases").symlink_to(fake_cache / "mop" / "releases")
        (outside / "parent-alias").symlink_to(fake_cache / "mop")
        saved = module.CLAUDE_PLUGIN_CACHE
        module.CLAUDE_PLUGIN_CACHE = fake_cache
        try:
            with self.assertRaisesRegex(module.InstallerError, "plugin cache"):
                module.refuse_claude_plugin_cache(outside / "releases", label="--release-root")
            with self.assertRaisesRegex(module.InstallerError, "plugin cache"):
                module.refuse_claude_plugin_cache(outside / "parent-alias" / "current", label="--current")
            module.refuse_claude_plugin_cache(outside / "real-releases", label="--release-root")
        finally:
            module.CLAUDE_PLUGIN_CACHE = saved

    def test_main_refuses_cache_current_before_any_mode_runs(self):
        stable = Path(tempfile.mkdtemp())
        cache_current = module.CLAUDE_PLUGIN_CACHE / "rajiv-plugins" / "master-of-panes" / "current"
        code = module.main([
            "check", "--release-root", str(stable / "releases"), "--current", str(cache_current),
            "--rollback-bundle", str(stable / "rb"),
        ])
        self.assertNotEqual(code, 0)

    def test_allows_mop_owned_root(self):
        module.refuse_claude_plugin_cache(Path.home() / ".local" / "share" / "master-of-panes" / "releases", label="x")


class LiveDriftGateTests(unittest.TestCase):
    """Rajiv 2026-10-07 (DM 1791376793.611059) + CTO REVISE ts 1791382000.456239:
    installs and sync-back must never overwrite fresh bytes. Driven through the CLI on temp dirs."""

    T1 = "/opt/claude/scripts/guard.sh"
    T2 = "/opt/claude/scripts/zz-other.sh"
    ALIAS = "/opt/claude/alias/guard.sh"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.target_root = self.root / "deployed"
        self.releases = self.root / "releases"
        self.current = self.root / "current"
        self.active = self._release("active", {"guard.sh": "v1\n", "zz-other.sh": "o1\n"})
        os.symlink(self.active, self.current)
        self.repo = self._release("repo", {"guard.sh": "v1\n", "zz-other.sh": "o1\n"})
        for target, body in ((self.T1, "v1\n"), (self.ALIAS, "v1\n"), (self.T2, "o1\n")):
            path = self.dep(target)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
            path.chmod(0o755)
        self.bundles = 0

    def tearDown(self) -> None:
        self.temp.cleanup()

    def dep(self, target: str) -> Path:
        return self.target_root / target.lstrip("/")

    def src(self, release: Path, name: str) -> Path:
        return release / Path(module.SHARED_ASSET_MANIFEST).parent / "scripts" / name

    def _release(self, name: str, bodies: dict) -> Path:
        root = self.releases / name
        (root / Path(module.SHARED_ASSET_MANIFEST).parent / "scripts").mkdir(parents=True)
        entries = []
        for file_name, body in sorted(bodies.items()):
            source = self.src(root, file_name)
            source.write_text(body)
            source.chmod(0o755)
            entry = {
                "canonical_target": f"/opt/claude/scripts/{file_name}",
                "dependencies": [],
                "dependency_status": "closed",
                "mode": 0o755,
                "sha256": module.sha256(source),
                "source_path": f"scripts/{file_name}",
            }
            if file_name == "guard.sh":
                entry["additional_targets"] = [self.ALIAS]
            entries.append(entry)
        manifest = {
            "schema": "mop_shared_operational_assets",
            "version": 1,
            "inventory": {"selected_count": len(entries)},
            "entries": entries,
        }
        (root / module.SHARED_ASSET_MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        return root

    def cli(self, mode: str, *extra: str, candidate: Path | None = None) -> int:
        self.bundles += 1
        argv = [mode, "--release-root", str(self.releases), "--current", str(self.current),
                "--shared-assets-root", str(self.target_root)]
        if candidate is not None:
            argv += ["--candidate", candidate.name]
        if mode != "drift-check":
            argv += ["--rollback-bundle", str(self.root / f"rb{self.bundles}")]
        return module.main([*argv, *extra])

    def snapshot(self) -> dict:
        return {str(p): p.read_bytes() for base in (self.target_root, self.repo) for p in sorted(base.rglob("*")) if p.is_file()}

    # --- positive / no-op ---
    def test_no_drift_installs_candidate(self) -> None:
        cand = self._release("cand", {"guard.sh": "v2\n", "zz-other.sh": "o1\n"})
        self.assertEqual(self.cli("drift-check", "--repo", str(self.repo)), 0)
        self.assertEqual(self.cli("shared-install", candidate=cand), 0)
        self.assertEqual(self.dep(self.T1).read_text(), "v2\n")
        self.assertEqual(self.dep(self.ALIAS).read_text(), "v2\n")

    def test_drift_check_is_read_only(self) -> None:
        self.dep(self.T1).write_text("live\n")
        before = self.snapshot()
        self.assertEqual(self.cli("drift-check", "--repo", str(self.repo)), 3)
        self.assertEqual(self.snapshot(), before)

    # --- refuse + exact accept + second-target protection ---
    def test_live_edit_refuses_and_accept_must_name_each_target(self) -> None:
        cand = self._release("cand", {"guard.sh": "v2\n", "zz-other.sh": "o2\n"})
        self.dep(self.T1).write_text("live1\n")
        self.dep(self.T2).write_text("live2\n")
        self.assertEqual(self.cli("shared-install", candidate=cand), 2)
        self.assertEqual(self.cli("shared-install", "--accept-overwrite", self.T1, candidate=cand), 2)
        self.assertEqual(self.dep(self.T1).read_text(), "live1\n")
        self.assertEqual(self.dep(self.T2).read_text(), "live2\n")
        self.assertEqual(
            self.cli("shared-install", "--accept-overwrite", self.T1, "--accept-overwrite", self.T2, candidate=cand), 0
        )
        self.assertEqual(self.dep(self.T2).read_text(), "o2\n")

    # --- gap 1: late edit at the replacement boundary ---
    def test_late_target_edit_keeps_fresh_bytes_and_restores_only_what_was_written(self) -> None:
        cand = self._release("cand", {"guard.sh": "v2\n", "zz-other.sh": "o2\n"})
        original = module._before_replace_hook

        def late_edit(target: Path) -> None:
            if target == self.dep(self.T2):
                target.write_text("fresh\n")

        module._before_replace_hook = late_edit
        try:
            self.assertEqual(self.cli("shared-install", candidate=cand), 2)
        finally:
            module._before_replace_hook = original
        self.assertEqual(self.dep(self.T2).read_text(), "fresh\n")
        self.assertEqual(self.dep(self.T1).read_text(), "v1\n")
        self.assertEqual(self.dep(self.ALIAS).read_text(), "v1\n")

    # --- gap 2: unknown baseline ---
    def test_unknown_baseline_refuses_existing_nonmatching_target(self) -> None:
        cand = self._release("cand", {"guard.sh": "v2\n", "zz-other.sh": "o1\n"})
        self.current.unlink()
        os.symlink(self.root / "releases" / "gone", self.current)
        self.assertEqual(self.cli("drift-check"), 2)
        self.assertEqual(self.cli("shared-install", candidate=cand), 2)
        self.assertEqual(self.dep(self.T1).read_text(), "v1\n")
        # absent first-install targets + candidate-equal files are fine; explicit accepts are exact
        self.dep(self.ALIAS).unlink()
        self.assertEqual(self.cli("shared-install", "--accept-overwrite", self.T1, candidate=cand), 0)
        self.assertEqual(self.dep(self.T1).read_text(), "v2\n")
        self.assertEqual(self.dep(self.ALIAS).read_text(), "v2\n")

    # --- gap 3: sync-back ---
    def test_sync_drift_metadata_then_install_keeps_edit_and_unrelated_dirt(self) -> None:
        self.dep(self.T1).write_text("live\n")
        unrelated = self.repo / "notes.txt"
        unrelated.write_text("dirty\n")
        self.assertEqual(self.cli("drift-check", "--sync-drift", "--repo", str(self.repo)), 0)
        source = self.src(self.repo, "guard.sh")
        self.assertEqual(source.read_text(), "live\n")
        self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o755)
        manifest = json.loads((self.repo / module.SHARED_ASSET_MANIFEST).read_text())
        self.assertEqual(manifest["entries"][0]["sha256"], module.sha256(self.dep(self.T1)))
        self.assertEqual(unrelated.read_text(), "dirty\n")
        module._load_shared_manifest(self.repo)
        self.assertEqual(self.cli("drift-check", "--repo", str(self.repo)), 0)
        self.assertEqual(self.cli("shared-install", candidate=self.repo), 0)
        self.assertEqual(self.dep(self.T1).read_text(), "live\n")
        self.assertEqual(self.dep(self.ALIAS).read_text(), "live\n")

    def test_sync_refuses_receiver_edit_before_any_write(self) -> None:
        self.dep(self.T1).write_text("live\n")
        self.dep(self.T2).write_text("live2\n")
        self.src(self.repo, "zz-other.sh").write_text("uncommitted\n")
        before = self.snapshot()
        self.assertEqual(self.cli("drift-check", "--sync-drift", "--repo", str(self.repo)), 2)
        self.assertEqual(self.snapshot(), before)

    def test_sync_refuses_alias_conflict_before_any_write(self) -> None:
        self.dep(self.T2).write_text("live2\n")
        self.dep(self.T1).write_text("liveA\n")
        self.dep(self.ALIAS).write_text("liveB\n")
        before = self.snapshot()
        self.assertEqual(self.cli("drift-check", "--sync-drift", "--repo", str(self.repo)), 2)
        self.assertEqual(self.snapshot(), before)


    def _sync_with_hook(self, hook) -> int:
        original = getattr(module, "_before_sync_write_hook", None)
        module._before_sync_write_hook = hook
        try:
            return self.cli("drift-check", "--sync-drift", "--repo", str(self.repo))
        finally:
            if original is None:
                del module._before_sync_write_hook
            else:
                module._before_sync_write_hook = original

    def test_sync_refuses_receiver_edit_made_during_sync(self) -> None:
        self.dep(self.T1).write_text("live\n")
        receiver = self.src(self.repo, "guard.sh")

        def edit_receiver(path: Path) -> None:
            if path == receiver:
                receiver.write_text("fresh source\n")

        self.assertEqual(self._sync_with_hook(edit_receiver), 2)
        self.assertEqual(receiver.read_text(), "fresh source\n")

    def test_sync_fails_when_source_manifest_and_deployed_disagree(self) -> None:
        self.dep(self.T1).write_text("live\n")
        deployed = self.dep(self.T1)

        def edit_deployed(path: Path) -> None:
            if path.name == "guard.sh":
                deployed.write_text("later live\n")

        self.assertEqual(self._sync_with_hook(edit_deployed), 2)
        self.assertEqual(deployed.read_text(), "later live\n")


if __name__ == "__main__":
    unittest.main()
