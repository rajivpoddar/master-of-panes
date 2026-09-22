#!/usr/bin/env python3
"""Contract tests for the companion broker pre-acquisition freshness gate.

Synthetic epochs only: nothing here starts, kills, inspects or recycles a real
broker, and no test reads or asserts on token material.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).parents[3]
HELPER = ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "scripts" / "codex-companion-broker-freshness.py"
CANARY = "rt.1.CANARY_TOKEN_VALUE_MUST_NEVER_APPEAR"

BROKER_CMD = (
    "node /x/app-server-broker.mjs serve --endpoint unix:/tmp/cxc-A/broker.sock "
    "--cwd /work/co1 --pid-file /tmp/cxc-A/broker.pid"
)
OTHER_CMD = (
    "node /x/app-server-broker.mjs serve --endpoint unix:/tmp/cxc-B/broker.sock "
    "--cwd /work/co2 --pid-file /tmp/cxc-B/broker.pid"
)
CHILD_CMD = "node /x/codex app-server --socket /tmp/cxc-A/broker.sock"
UNRELATED_CHILD = "node /x/something-else --flag"


def _load():
    spec = importlib.util.spec_from_file_location("broker_freshness", HELPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["broker_freshness"] = module
    spec.loader.exec_module(module)
    return module


MODULE = _load()


def proc(pid: int, ppid: int, start: float | None, command: str) -> dict:
    return {"pid": pid, "ppid": ppid, "start_epoch": start, "command": command}


class DecisionPolicyTests(unittest.TestCase):
    def test_fresh_broker_is_a_noop(self) -> None:
        verdict = MODULE.decide(2_000.0, "", proc(10, 1, 2_500.0, BROKER_CMD), "unknown", "")
        self.assertEqual(verdict["action"], MODULE.NOOP)

    def test_stale_idle_broker_is_recycled(self) -> None:
        verdict = MODULE.decide(2_000.0, "", proc(10, 1, 1_000.0, BROKER_CMD), "idle", "verified")
        self.assertEqual(verdict["action"], MODULE.RECYCLE)

    def test_stale_busy_broker_defers_without_interruption(self) -> None:
        verdict = MODULE.decide(2_000.0, "", proc(10, 1, 1_000.0, BROKER_CMD), "active", "in flight")
        self.assertEqual(verdict["action"], MODULE.DEFER)

    def test_stale_broker_with_unknown_busy_state_defers(self) -> None:
        verdict = MODULE.decide(2_000.0, "", proc(10, 1, 1_000.0, BROKER_CMD), "unknown", "socket present")
        self.assertEqual(verdict["action"], MODULE.DEFER)

    def test_missing_refresh_fails_safe(self) -> None:
        verdict = MODULE.decide(None, "auth_refresh_missing_or_unparseable", proc(10, 1, 1_000.0, BROKER_CMD), "idle", "")
        self.assertEqual(verdict["action"], MODULE.FAIL_SAFE)
        self.assertIn("auth_refresh", verdict["reason"])

    def test_unparseable_broker_start_fails_safe(self) -> None:
        verdict = MODULE.decide(2_000.0, "", proc(10, 1, None, BROKER_CMD), "idle", "")
        self.assertEqual(verdict["action"], MODULE.FAIL_SAFE)

    def test_no_broker_for_checkout_is_a_noop(self) -> None:
        verdict = MODULE.decide(2_000.0, "", None, "unknown", "")
        self.assertEqual(verdict["action"], MODULE.NOOP)


class ProcessTargetingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.processes = [
            proc(100, 1, 1_000.0, BROKER_CMD),
            proc(200, 1, 1_500.0, OTHER_CMD),
            proc(101, 100, 1_010.0, CHILD_CMD),
            proc(102, 100, 1_011.0, UNRELATED_CHILD),
            proc(103, 9, 1_012.0, CHILD_CMD),
        ]

    def test_selection_is_scoped_to_the_checkout(self) -> None:
        picked = MODULE.select_brokers(self.processes, "/work/co1")
        self.assertEqual([p["pid"] for p in picked], [100])

    def test_children_are_only_the_parents_app_server_children(self) -> None:
        children = MODULE.select_children(self.processes, 100)
        self.assertEqual([c["pid"] for c in children], [101])

    def test_recycle_refuses_unsafe_pids(self) -> None:
        ok, detail = MODULE.recycle(1, [])
        self.assertFalse(ok)
        self.assertEqual(detail, "refused_unsafe_pid")


class FreshnessReadingTests(unittest.TestCase):
    def test_reads_only_last_refresh_and_never_token_material(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            (home / "auth.json").write_text(json.dumps({
                "auth_mode": "chatgpt",
                "last_refresh": "2026-09-22T06:35:17.934139Z",
                "tokens": {"refresh_token": CANARY, "account_id": "acct"},
            }), encoding="utf-8")
            epoch, error = MODULE.read_refresh_epoch(home)
            self.assertEqual(error, "")
            self.assertIsNotNone(epoch)
            self.assertNotIn(CANARY, repr((epoch, error)))

    def test_missing_file_is_a_typed_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            epoch, error = MODULE.read_refresh_epoch(pathlib.Path(tmp))
            self.assertIsNone(epoch)
            self.assertTrue(error.startswith("auth_store_unreadable"))

    def test_unparseable_refresh_is_a_typed_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            (home / "auth.json").write_text(json.dumps({"last_refresh": "not-a-timestamp"}), encoding="utf-8")
            epoch, error = MODULE.read_refresh_epoch(home)
            self.assertIsNone(epoch)
            self.assertEqual(error, "auth_refresh_missing_or_unparseable")

    def test_iso_parsing(self) -> None:
        self.assertEqual(MODULE.parse_iso_epoch(""), None)
        self.assertGreater(MODULE.parse_iso_epoch("2026-09-22T06:35:17.934139Z"), 0)


class BuildPlanTests(unittest.TestCase):
    def test_plan_reports_recycle_for_stale_idle_and_is_hermetic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            (home / "auth.json").write_text(json.dumps({"last_refresh": "2026-09-22T06:35:17Z"}), encoding="utf-8")
            refresh = MODULE.read_refresh_epoch(home)[0]
            processes = [proc(100, 1, refresh - 86_400.0, BROKER_CMD), proc(101, 100, refresh - 86_000.0, CHILD_CMD)]
            plan = MODULE.build_plan(home, "/work/co1", "idle", processes=processes)
            entry = plan["brokers"][0]
            self.assertEqual(entry["action"], MODULE.RECYCLE)
            self.assertEqual(entry["children"], [101])

    def test_plan_auto_treats_an_absent_socket_as_idle(self) -> None:
        # auto mode: no socket means the broker is not serving, so a stale broker
        # is safe to recycle. This is idle evidence, not a guess.
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            (home / "auth.json").write_text(json.dumps({"last_refresh": "2026-09-22T06:35:17Z"}), encoding="utf-8")
            refresh = MODULE.read_refresh_epoch(home)[0]
            processes = [proc(100, 1, refresh - 86_400.0, BROKER_CMD)]
            plan = MODULE.build_plan(home, "/work/co1", "auto", processes=processes)
            self.assertEqual(plan["brokers"][0]["action"], MODULE.RECYCLE)
            self.assertEqual(plan["brokers"][0]["busy"], "idle")

    def test_plan_defers_when_the_socket_exists_but_busy_is_unverifiable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            (home / "auth.json").write_text(json.dumps({"last_refresh": "2026-09-22T06:35:17Z"}), encoding="utf-8")
            sock = home / "broker.sock"
            sock.write_text("", encoding="utf-8")
            refresh = MODULE.read_refresh_epoch(home)[0]
            command = (
                "node /x/app-server-broker.mjs serve --endpoint unix:" + str(sock) +
                " --cwd /work/co1 --pid-file /tmp/cxc-A/broker.pid"
            )
            processes = [proc(100, 1, refresh - 86_400.0, command)]
            plan = MODULE.build_plan(home, "/work/co1", "auto", processes=processes)
            self.assertEqual(plan["brokers"][0]["action"], MODULE.DEFER)
            self.assertEqual(plan["brokers"][0]["busy"], "unknown")


class CliTests(unittest.TestCase):
    def test_cli_outputs_no_secret_material_and_exits_zero_without_a_broker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            (home / "auth.json").write_text(json.dumps({
                "last_refresh": "2026-09-22T06:35:17Z",
                "tokens": {"refresh_token": CANARY},
            }), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(HELPER), "--home", str(home), "--cwd", "/work/absent-checkout", "--json"],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            blob = result.stdout + result.stderr
            self.assertNotIn(CANARY, blob)
            self.assertNotIn("refresh_token", blob)
            self.assertIn("noop", blob)


if __name__ == "__main__":
    unittest.main()
