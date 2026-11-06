"""Actual activation/one-file writers with isolated assets and stubbed launchd."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("cutover_installer", Path(__file__).parents[1] / "scripts/install-release.py")
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class StoppedCutoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.releases = self.root / "releases"
        self.current = self.root / "current"
        self.targets = self.root / "installed"
        self.target = "/opt/pm/mop-assign-slot.py"
        self.helper = self.targets / self.target.lstrip("/")
        self.helper.parent.mkdir(parents=True)
        self.helper.write_text("old client\n")
        self.helper.chmod(0o755)
        self.old = self.release("old", "old client\n")
        self.new = self.release("new", "new client\n")
        self.current.symlink_to(self.old)
        self.owned = True
        self.events = []
        self.start_error = None
        self.fail_health = False

    def release(self, name, body):
        root = self.releases / name
        for relative in ("dist/server.js", "scripts/release-slot-reset-and-ack.py"):
            p = root / relative
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("fixture\n")
            p.chmod(0o644 if relative.endswith(".js") else 0o755)
        source = root / "scripts/pm/shared-assets/claude/scripts/mop-assign-slot.py"
        source.parent.mkdir(parents=True)
        source.write_text(body)
        source.chmod(0o755)
        manifest = {"schema": "mop_shared_operational_assets", "version": 1,
                    "inventory": {"selected_count": 1}, "entries": [{
                        "canonical_target": self.target, "dependencies": [], "dependency_status": "closed",
                        "mode": 0o755, "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        "source_path": "claude/scripts/mop-assign-slot.py"}]}
        (root / m.SHARED_ASSET_MANIFEST).write_text(json.dumps(manifest))
        return root

    def transport(self, argv, **kwargs):
        self.assertNotIn("kickstart", argv)
        if argv[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(argv, 0 if self.owned else 113,
                                               "pid = 1234\n" if self.owned else "",
                                               "" if self.owned else "Could not find service")
        if argv[0] == "lsof":
            return subprocess.CompletedProcess(argv, 0 if self.owned else 1,
                                               "p1234\n" if self.owned else "", "")
        if argv[:2] == ["launchctl", "bootout"]:
            self.events.append("bootout")
            self.owned = False
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:2] == ["launchctl", "bootstrap"]:
            self.assertFalse(self.owned, "never start a second owner")
            selected = self.current.resolve().name
            self.assertEqual(self.helper.read_text(), selected + " client\n", "pointer/assets paired BEFORE bootstrap")
            self.events.append("bootstrap:" + selected)
            if self.start_error == "refused" and selected == "new":
                return subprocess.CompletedProcess(argv, 5, "", "EIO without owner")
            self.owned = True
            if self.start_error == "timeout":
                raise subprocess.TimeoutExpired(argv, 20)
            return subprocess.CompletedProcess(argv, 5 if self.start_error == "EIO" else 0, "", "EIO")
        self.fail("unexpected transport: " + repr(argv))

    def replace(self, target):
        self.assertFalse(self.owned, "client replacement requires service stopped")
        self.assertEqual(self.current.resolve(), self.new)
        self.events.append("replace")

    def health(self, *args, **kwargs):
        if self.fail_health and self.current.resolve() == self.new:
            raise m.InstallerError("candidate health failed")
        self.assertTrue(self.owned)
        return {"status": 200}

    def activate(self, hook=None):
        with patch.object(m.subprocess, "run", side_effect=self.transport), \
             patch.object(m, "verify_staged"), \
             patch.object(m, "_bound_health", side_effect=self.health), \
             patch.object(m, "_default_canary", return_value={"slots": []}), \
             patch.object(m, "_before_replace_hook", side_effect=hook or self.replace), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return m.main(["activate", "--repo", str(self.new), "--candidate", "new",
                           "--release-root", str(self.releases), "--current", str(self.current),
                           "--expected-old", str(self.old), "--inventory", str(self.root / "inventory.json"),
                           "--rollback-bundle", str(self.root / "rollback"),
                           "--installed-root", str(self.targets),
                           "--shared-assets-root", str(self.targets)])

    def set_inventory(self):
        (self.root / "inventory.json").write_text(json.dumps({"items": []}))

    def test_false_drift_after_pointer_movement_then_guarded_cutover(self):
        # Reproduce the old two-command failure, with the legitimate old helper.
        m.atomic_switch(self.current, self.new, self.old)
        with self.assertRaises(m.InstallerError):
            m.assert_no_live_drift(active_release=self.current.resolve(), candidate_root=self.new, target_root=self.targets)
        m.atomic_switch(self.current, self.old, self.new)
        self.set_inventory()
        self.assertEqual(self.activate(), 0)
        self.assertEqual(self.events, ["bootout", "replace", "bootstrap:new"])
        self.assertEqual(self.current.resolve(), self.new)
        plan = json.loads((self.root / "rollback/CUTOVER.json").read_text())
        self.assertEqual(plan["old_release"], str(self.old))
        self.assertEqual(plan["old_manifest_sha256"], m.sha256(self.old / m.SHARED_ASSET_MANIFEST))
        self.assertEqual(plan["preimages"][0]["sha256"], hashlib.sha256(b"old client\n").hexdigest())
        backup = json.loads((self.root / "rollback/shared-preimages/ROLLBACK_MANIFEST.json").read_text())
        self.assertEqual(backup["entries"][0]["mode"], 0o755)

    def test_pre_shutdown_drift_refuses_without_service_effect(self):
        self.set_inventory()
        original = m.create_rollback_bundle
        def capture(targets, bundle):
            result = original(targets, bundle)
            if bundle.name == "shared-preimages":
                self.helper.write_text("fresh user edit\n")
            return result
        with patch.object(m, "create_rollback_bundle", side_effect=capture):
            self.assertEqual(self.activate(), 2)
        self.assertEqual(self.events, [])
        self.assertTrue(self.owned)
        self.assertEqual(self.current.resolve(), self.old)
        self.assertEqual(self.helper.read_text(), "fresh user edit\n")

    def test_last_replacement_drift_preserved_and_no_mixed_recovery_start(self):
        self.set_inventory()
        def race(target):
            self.replace(target)
            self.helper.write_text("fresh after capture\n")
        self.assertEqual(self.activate(race), 2)
        self.assertEqual(self.helper.read_text(), "fresh after capture\n")
        self.assertFalse(self.owned)
        self.assertFalse(any(x.startswith("bootstrap") for x in self.events))

    def test_start_failure_restores_both_before_old_bootstrap(self):
        self.set_inventory()
        self.start_error = "refused"
        self.assertEqual(self.activate(), 2)
        self.assertEqual(self.events, ["bootout", "replace", "bootstrap:new", "bootstrap:old"])
        self.assertEqual(self.current.resolve(), self.old)
        self.assertEqual(self.helper.read_text(), "old client\n")
        self.assertTrue(self.owned)

    def test_unhealthy_new_owner_stopped_before_paired_recovery(self):
        self.set_inventory()
        self.fail_health = True
        self.assertEqual(self.activate(), 2)
        self.assertEqual(self.events, ["bootout", "replace", "bootstrap:new", "bootout", "bootstrap:old"])
        self.assertEqual(self.current.resolve(), self.old)

    def test_bootstrap_EIO_with_actual_owner_is_not_repeated(self):
        self.set_inventory()
        self.start_error = "EIO"
        self.assertEqual(self.activate(), 0)
        self.assertEqual(self.events.count("bootstrap:new"), 1)

    def test_bootstrap_response_loss_reconciles_owner_without_resend(self):
        self.set_inventory()
        self.start_error = "timeout"
        self.assertEqual(self.activate(), 0)
        self.assertEqual(self.events.count("bootstrap:new"), 1)

    def test_candidate_equal_installed_file_is_not_an_old_preimage(self):
        self.set_inventory()
        self.helper.write_text("new client\n")
        self.assertEqual(self.activate(), 2)
        self.assertEqual(self.events, [])
        self.assertTrue(self.owned)
        self.assertEqual(self.current.resolve(), self.old)

    def test_backup_bytes_are_verified_before_shutdown(self):
        self.set_inventory()
        original = m._copy_payload
        def corrupt(source, destination):
            original(source, destination)
            if "shared-preimages" in str(destination):
                destination.write_text("corrupt backup\n")
        with patch.object(m, "_copy_payload", side_effect=corrupt):
            self.assertEqual(self.activate(), 2)
        self.assertEqual(self.events, [])
        self.assertTrue(self.owned)
        self.assertEqual(self.current.resolve(), self.old)
        self.assertEqual(self.helper.read_text(), "old client\n")


if __name__ == "__main__":
    unittest.main()
