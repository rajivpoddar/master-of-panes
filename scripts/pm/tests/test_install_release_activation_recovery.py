"""Hermetic proof for exception-safe activation recovery.

Fake pointer/restart/health, no live service, slot, or release effect.
"""
from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[3]
SPEC = importlib.util.spec_from_file_location("install_release_recovery", ROOT / "scripts" / "install-release.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.old = base / "old"
        self.new = base / "new"
        self.old.mkdir()
        self.new.mkdir()
        self.current = base / "current"
        os.symlink(self.old, self.current)
        self.bundle = base / "rollback"
        MODULE._verify_required_runtime_files = lambda *a, **k: None  # type: ignore[assignment]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def activate(self, restart, health, canary=None):
        return MODULE.activate(
            release_dir=self.new, current=self.current, expected_old=self.old,
            delete_targets=[], rollback_bundle=self.bundle,
            restart=restart, health=health, canary=canary or (lambda: {"status": 200}),
        )

    def test_normal_activation_unchanged(self) -> None:
        result = self.activate(lambda: None, lambda: {"status": 200})
        self.assertEqual(result["status"], "ACTIVATED")
        self.assertEqual(self.current.resolve(), self.new.resolve())

    def test_pre_switch_refusal_never_restarts(self) -> None:
        calls = []
        self.current.unlink()
        os.symlink(self.new, self.current)  # drift: current no longer selects old
        with self.assertRaises(MODULE.InstallerError) as ctx:
            self.activate(lambda: calls.append("restart"), lambda: {"status": 200})
        self.assertIn("activation refused before switch", str(ctx.exception))
        self.assertEqual(calls, [])

    def test_activation_restart_fails_then_bounded_recovery_succeeds(self) -> None:
        state = {"restarts": 0, "health": 0}

        def restart():
            state["restarts"] += 1
            if state["restarts"] in (1, 2):  # activation attempt + first rollback attempt
                raise RuntimeError("restart boom")

        def health():
            state["health"] += 1
            return {"status": 200}

        with self.assertRaises(MODULE.InstallerError) as ctx:
            self.activate(restart, health)
        message = str(ctx.exception)
        self.assertIn("activation failed and baseline restored", message)
        self.assertIn("RuntimeError: restart boom", message)
        self.assertIn("prior release healthy after restart attempt 2", message)
        self.assertEqual(self.current.resolve(), self.old.resolve(), "baseline pointer restored")
        self.assertEqual(state["restarts"], 3)  # 1 activation + 2 bounded recovery attempts

    def test_irrecoverable_restart_reports_recovery_failed(self) -> None:
        def restart():
            raise RuntimeError("restart boom")

        with self.assertRaises(MODULE.InstallerError) as ctx:
            self.activate(restart, lambda: {"status": 200})
        message = str(ctx.exception)
        self.assertTrue(message.startswith("RECOVERY_FAILED"), message)
        self.assertIn("RuntimeError: restart boom", message)
        self.assertIn("activation_error", message)
        self.assertIn("rollback_restart_errors", message)
        self.assertIn(str(self.current), message)
        self.assertEqual(self.current.resolve(), self.old.resolve(), "pointer restored even on recovery failure")

    def test_rollback_substep_failure_does_not_skip_service_restoration(self) -> None:
        state = {"restarts": 0}

        def restart():
            state["restarts"] += 1
            if state["restarts"] == 1:
                raise RuntimeError("activation restart boom")

        def broken_restore(*_a, **_k):
            raise RuntimeError("asset restore boom")

        original_restore = MODULE.restore_rollback_bundle
        MODULE.restore_rollback_bundle = broken_restore  # type: ignore[assignment]
        self.addCleanup(setattr, MODULE, "restore_rollback_bundle", original_restore)
        with self.assertRaises(MODULE.InstallerError) as ctx:
            self.activate(restart, lambda: {"status": 200})
        message = str(ctx.exception)
        self.assertIn("asset restore boom", message)
        self.assertIn("prior release healthy after restart attempt", message)
        self.assertEqual(self.current.resolve(), self.old.resolve())

    def test_pointer_restore_failure_reports_service_healthy_on_pointer_release(self) -> None:
        calls = {"switches": 0, "restarts": 0}
        original_switch = MODULE.atomic_switch

        def flaky_switch(current, release_dir, expected_old):
            if calls["switches"] == 0:
                calls["switches"] = 1
                return original_switch(current, release_dir, expected_old)
            raise RuntimeError("rollback switch boom")

        MODULE.atomic_switch = flaky_switch  # type: ignore[assignment]
        self.addCleanup(setattr, MODULE, "atomic_switch", original_switch)

        def restart():
            calls["restarts"] += 1
            if calls["restarts"] == 1:
                raise RuntimeError("activation restart boom")

        with self.assertRaises(MODULE.InstallerError) as ctx:
            self.activate(restart, lambda: {"status": 200})
        message = str(ctx.exception)
        self.assertNotIn("baseline restored", message)
        self.assertNotIn("prior release healthy", message)
        self.assertIn("POINTER_RESTORE_FAILED", message)
        self.assertIn("rollback switch boom", message)
        self.assertIn(str(self.new.resolve()), message, "fresh pointer target must be reported")
        self.assertEqual(self.current.resolve(), self.new.resolve(), "pointer still selects the refused release")


if __name__ == "__main__":
    unittest.main()
