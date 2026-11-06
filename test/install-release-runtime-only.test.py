"""Exit repair's guarded runtime-only boundary. Private files/fake service only."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("runtime_installer", Path(__file__).parents[1] / "scripts/install-release.py")
assert SPEC and SPEC.loader
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class RuntimeOnlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old = self.root / ("a" * 40)
        self.new = self.root / ("b" * 40)
        self.targets = self.root / "live"
        self.current = self.root / "current"
        self.old.mkdir(); self.new.mkdir(); self.targets.mkdir()
        self.current.symlink_to(self.old)
        self.source = "claude/scripts/pm/heartbeat/heartbeat-compose.py"
        self.name = "/Users/rajiv/.claude/scripts/pm/heartbeat/heartbeat-compose.py"
        self.target = self.targets / self.name.lstrip("/")
        self.target.parent.mkdir(parents=True)
        self.target.write_text("newer already approved live composer\n"); self.target.chmod(0o755)
        content = b"old embedded composer; must never replace live\n"
        manifest = {"schema": "mop_shared_operational_assets", "version": 1, "entries": [{
            "source_path": self.source, "canonical_target": self.name,
            "sha256": hashlib.sha256(content).hexdigest(), "mode": 0o755,
            "dependencies": [], "dependency_status": "closed",
        }]}
        for release in (self.old, self.new):
            path = release / "scripts/pm/shared-assets" / self.source
            path.parent.mkdir(parents=True); path.write_bytes(content); path.chmod(0o755)
            meta = release / m.SHARED_ASSET_MANIFEST
            meta.write_text(json.dumps(manifest))
            server = release / "dist/server.js"; server.parent.mkdir(); server.write_text("server\n"); server.chmod(0o644)
            helper = release / "scripts/release-slot-reset-and-ack.py"; helper.write_text("helper\n"); helper.chmod(0o755)
        self.preimage_bundle = self.root / "private-preimages"
        record = m._capture_runtime_preimages(self.new, self.old, self.targets, self.preimage_bundle)
        m._write_json(self.new / m.RUNTIME_ONLY_RECORD, record)
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def activate(self, health=None, stop=None):
        return m.activate(release_dir=self.new, current=self.current, expected_old=self.old,
                          delete_targets=[], rollback_bundle=self.root / "activation-backup",
                          stop=stop or (lambda: self.calls.append("stop")),
                          restart=lambda: self.calls.append("restart"),
                          health=health or (lambda: {"status": 200}), canary=lambda: {"slots": []},
                          install_shared=False,
                          shared_preimage_check=lambda: m._verify_runtime_preimages(self.new, self.old, self.targets))

    def test_real_activation_preserves_newer_shared_bytes_and_exact_private_backup(self):
        before = m._runtime_record(self.target)
        result = self.activate()
        self.assertEqual(result["status"], "ACTIVATED")
        self.assertEqual(result["shared_assets"], [])
        self.assertEqual(self.current.resolve(), self.new)
        self.assertEqual(self.calls, ["stop", "restart"])
        self.assertEqual(m._runtime_record(self.target), before)
        self.assertEqual((self.preimage_bundle / "payload/0000").read_bytes(), self.target.read_bytes())
        self.assertEqual((self.preimage_bundle / "payload/0000").stat().st_mode & 0o777, 0o755)

    def test_changed_bytes_refuse_before_service_or_pointer_effect(self):
        self.target.write_text("new legitimate live edit\n")
        with self.assertRaisesRegex(m.InstallerError, "before switch"):
            self.activate()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.current.resolve(), self.old)
        self.assertEqual(self.target.read_text(), "new legitimate live edit\n")

    def test_changed_mode_and_lost_backup_refuse(self):
        self.target.chmod(0o644)
        with self.assertRaisesRegex(m.InstallerError, "preimage changed"):
            m._verify_runtime_preimages(self.new, self.old, self.targets)
        self.target.chmod(0o755)
        (self.preimage_bundle / "payload/0000").write_text("corrupt backup\n")
        with self.assertRaisesRegex(m.InstallerError, "rollback payload mismatch"):
            m._verify_runtime_preimages(self.new, self.old, self.targets)

    def test_drift_after_shutdown_recovers_prior_service_without_overwriting_newer_asset(self):
        def stop():
            self.calls.append("stop")
            self.target.write_text("external newer edit during shutdown\n")
        with self.assertRaisesRegex(m.InstallerError, "baseline restored"):
            self.activate(stop=stop)
        self.assertEqual(self.current.resolve(), self.old)
        self.assertEqual(self.calls, ["stop", "restart"])
        self.assertEqual(self.target.read_text(), "external newer edit during shutdown\n")

    def test_failed_runtime_readiness_restores_pointer_without_restoring_shared_files(self):
        def fail_health():
            if self.current.resolve() == self.new:
                raise RuntimeError("candidate health failed")
            return {"status": 200}
        before = m._runtime_record(self.target)
        with self.assertRaisesRegex(m.InstallerError, "baseline restored"):
            self.activate(health=fail_health)
        self.assertEqual(self.current.resolve(), self.old)
        self.assertEqual(self.calls, ["stop", "restart", "stop", "restart"])
        self.assertEqual(m._runtime_record(self.target), before)

    def test_arbitrary_runtime_change_and_wrong_active_baseline_are_refused(self):
        fake = type("Result", (), {"stdout": "src/db.ts\n"})()
        with patch.object(m, "_run", return_value=fake):
            with self.assertRaisesRegex(m.InstallerError, "exceeds"):
                m._assert_runtime_scope(self.new, "b" * 40, "a" * 40, self.current, self.old)
        with self.assertRaisesRegex(m.InstallerError, "exact current"):
            m._assert_runtime_scope(self.new, "b" * 40, "c" * 40, self.current, self.old)

    def test_runtime_only_cli_rejects_shared_install_and_overwrite_before_effects(self):
        for mode, extra in [("shared-install", []), ("activate", ["--accept-overwrite", self.name])]:
            self.assertEqual(m.main([mode, "--runtime-only", "--release-root", str(self.root),
                                     "--rollback-bundle", str(self.root / "unused"), *extra]), 2)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
