#!/usr/bin/env python3
"""Contract tests for the companion broker pre-acquisition freshness gate.

Live-shaped where it matters: several cases stand up a REAL AF_UNIX listener and
speak the broker's real read-only probe (thread/list) at it. No test ever starts
or kills a real broker process - the recycle path is always injected.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).parents[3]
HELPER = ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "scripts" / "codex-companion-broker-freshness.py"
CANARY = "rt.1.CANARY_TOKEN_VALUE_MUST_NEVER_APPEAR"
REFRESH = "2026-09-22T06:35:17Z"


def _load():
    spec = importlib.util.spec_from_file_location("broker_freshness", HELPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["broker_freshness"] = module
    spec.loader.exec_module(module)
    return module


MODULE = _load()


def proc(pid, ppid, start, command):
    return {"pid": pid, "ppid": ppid, "start_epoch": start, "command": command}


def broker_cmd(sock_path: str, cwd: str = "/work/co1") -> str:
    return (
        f"node /x/app-server-broker.mjs serve --endpoint unix:{sock_path} "
        f"--cwd {cwd} --pid-file /tmp/cxc-A/broker.pid"
    )


CHILD_CMD = "node /x/codex app-server --socket /tmp/cxc-A/broker.sock"
UNRELATED_CHILD = "node /x/something-else --flag"


class FakeBroker:
    """A real unix-socket listener that answers like the vendor broker."""

    def __init__(self, reply: dict | None, *, respond: bool = True):
        self.reply = reply
        self.respond = respond
        self.dir = tempfile.TemporaryDirectory()
        self.path = str(pathlib.Path(self.dir.name) / "broker.sock")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        self.sock.listen(4)
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                if not self.respond:
                    time.sleep(0.5)
                    continue
                try:
                    conn.recv(65536)
                except OSError:
                    continue
                try:
                    conn.sendall((json.dumps(self.reply) + "\n").encode())
                except OSError:
                    pass

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass
        self.dir.cleanup()


def auth(home: pathlib.Path, refresh: str = REFRESH, tokens: dict | None = None) -> float:
    (home / "auth.json").write_text(
        json.dumps({"last_refresh": refresh, "tokens": tokens or {"refresh_token": CANARY}}),
        encoding="utf-8",
    )
    return MODULE.read_refresh_epoch(home)[0]


class DecisionPolicyTests(unittest.TestCase):
    def test_fresh_broker_is_a_noop(self):
        v = MODULE.decide(2_000.0, "", proc(10, 1, 2_500.0, broker_cmd("/tmp/x")), MODULE.IDLE, "")
        self.assertEqual(v["action"], MODULE.NOOP)

    def test_stale_idle_is_recycled(self):
        v = MODULE.decide(2_000.0, "", proc(10, 1, 1_000.0, broker_cmd("/tmp/x")), MODULE.IDLE, "")
        self.assertEqual(v["action"], MODULE.RECYCLE)

    def test_stale_active_defers(self):
        v = MODULE.decide(2_000.0, "", proc(10, 1, 1_000.0, broker_cmd("/tmp/x")), MODULE.ACTIVE, "")
        self.assertEqual(v["action"], MODULE.DEFER)

    def test_stale_unknown_defers(self):
        v = MODULE.decide(2_000.0, "", proc(10, 1, 1_000.0, broker_cmd("/tmp/x")), MODULE.UNKNOWN, "probe timeout")
        self.assertEqual(v["action"], MODULE.DEFER)

    def test_missing_refresh_fails_safe(self):
        v = MODULE.decide(None, "auth_refresh_missing_or_unparseable", proc(10, 1, 1_000.0, broker_cmd("/tmp/x")), MODULE.IDLE, "")
        self.assertEqual(v["action"], MODULE.FAIL_SAFE)

    def test_unparseable_start_fails_safe(self):
        v = MODULE.decide(2_000.0, "", proc(10, 1, None, broker_cmd("/tmp/x")), MODULE.IDLE, "")
        self.assertEqual(v["action"], MODULE.FAIL_SAFE)


class LiveProbeTests(unittest.TestCase):
    def test_busy_code_means_active(self):
        broker = FakeBroker({"id": 1, "error": {"code": MODULE.BUSY_RPC_CODE, "message": "busy"}})
        try:
            state, why = MODULE.probe_busy(broker.path)
            self.assertEqual(state, MODULE.ACTIVE)
            self.assertIn("BUSY", why)
        finally:
            broker.close()

    def test_any_other_reply_means_idle(self):
        broker = FakeBroker({"id": 1, "result": {"threads": []}})
        try:
            state, _ = MODULE.probe_busy(broker.path)
            self.assertEqual(state, MODULE.IDLE)
        finally:
            broker.close()

    def test_no_listener_is_idle_not_unknown(self):
        state, why = MODULE.probe_busy("/tmp/definitely-not-a-broker.sock")
        self.assertEqual(state, MODULE.IDLE)
        self.assertIn("no listener", why)

    def test_garbage_reply_is_unknown(self):
        broker = FakeBroker({"unexpected": True})
        try:
            state, _ = MODULE.probe_busy(broker.path)
            self.assertEqual(state, MODULE.UNKNOWN)
        finally:
            broker.close()

    def test_silent_broker_times_out_unknown(self):
        broker = FakeBroker(None, respond=False)
        try:
            state, _ = MODULE.probe_busy(broker.path, timeout=0.4)
            self.assertEqual(state, MODULE.UNKNOWN)
        finally:
            broker.close()


class LiveShapedStaleBrokerTests(unittest.TestCase):
    def test_stale_idle_present_socket_is_recycled(self):
        broker = FakeBroker({"id": 1, "result": {}})
        try:
            with tempfile.TemporaryDirectory() as tmp:
                home = pathlib.Path(tmp)
                refresh = auth(home)
                procs = [proc(100, 1, refresh - 86_400.0, broker_cmd(broker.path)),
                         proc(101, 100, refresh - 86_000.0, CHILD_CMD)]
                plan = MODULE.build_plan(home, "/work/co1", "auto", processes=procs)
                self.assertEqual(plan["brokers"][0]["action"], MODULE.RECYCLE)
                self.assertEqual(plan["brokers"][0]["busy"], MODULE.IDLE)
        finally:
            broker.close()

    def test_stale_active_present_socket_defers(self):
        broker = FakeBroker({"id": 1, "error": {"code": MODULE.BUSY_RPC_CODE, "message": "busy"}})
        try:
            with tempfile.TemporaryDirectory() as tmp:
                home = pathlib.Path(tmp)
                refresh = auth(home)
                procs = [proc(100, 1, refresh - 86_400.0, broker_cmd(broker.path))]
                plan = MODULE.build_plan(home, "/work/co1", "auto", processes=procs)
                self.assertEqual(plan["brokers"][0]["action"], MODULE.DEFER)
                self.assertEqual(plan["brokers"][0]["busy"], MODULE.ACTIVE)
        finally:
            broker.close()


class AcquisitionContractTests(unittest.TestCase):
    def _setup(self, tmp, cmd):
        home = pathlib.Path(tmp)
        refresh = auth(home)
        return home, refresh, [proc(100, 1, refresh - 86_400.0, cmd), proc(101, 100, refresh - 86_000.0, CHILD_CMD)]

    def test_stale_idle_performs_exactly_one_scoped_recycle_then_fresh_is_noop(self):
        broker = FakeBroker({"id": 1, "result": {}})
        try:
            with tempfile.TemporaryDirectory() as tmp:
                home, refresh, procs = self._setup(tmp, broker_cmd(broker.path))
                kills = []
                def killer(pid, children):
                    kills.append((pid, list(children)))
                    return True, "recycled"

                plan = MODULE.ensure_fresh(home, "/work/co1", "auto", processes=procs, killer=killer)
                self.assertEqual(kills, [(100, [101])], "exactly one scoped recycle of parent+child")
                self.assertTrue(plan["recycled"][0]["ok"])

                fresh = [proc(200, 1, refresh + 60.0, broker_cmd(broker.path))]
                second = MODULE.ensure_fresh(home, "/work/co1", "auto", processes=fresh, killer=killer)
                self.assertEqual(second["brokers"][0]["action"], MODULE.NOOP)
                self.assertEqual(len(kills), 1, "a fresh broker is never recycled")
        finally:
            broker.close()

    def test_active_review_never_kills_and_never_proceeds(self):
        broker = FakeBroker({"id": 1, "error": {"code": MODULE.BUSY_RPC_CODE, "message": "busy"}})
        try:
            with tempfile.TemporaryDirectory() as tmp:
                home, _, procs = self._setup(tmp, broker_cmd(broker.path))
                kills = []
                plan = MODULE.ensure_fresh(home, "/work/co1", "auto", processes=procs,
                                           killer=lambda p, c: (kills.append(p), (True, "x"))[1])
                self.assertEqual(kills, [], "an active review is never interrupted")
                self.assertEqual(plan["brokers"][0]["action"], MODULE.DEFER)
        finally:
            broker.close()

    def test_unknown_busy_state_never_kills(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, _, procs = self._setup(tmp, broker_cmd("/tmp/definitely-absent.sock"))
            state, _ = MODULE.probe_busy("/tmp/definitely-absent.sock")
            self.assertEqual(state, MODULE.IDLE)  # no listener == idle, still no kill below
            kills = []
            MODULE.ensure_fresh(home, "/work/co1", "auto", processes=procs,
                                probe=lambda ep: (MODULE.UNKNOWN, "probe failed"),
                                killer=lambda p, c: (kills.append(p), (True, "x"))[1])
            self.assertEqual(kills, [], "unverifiable busy state must not kill")

    def test_review_starting_after_planning_is_not_interrupted(self):
        broker = FakeBroker({"id": 1, "result": {}})
        try:
            with tempfile.TemporaryDirectory() as tmp:
                home, _, procs = self._setup(tmp, broker_cmd(broker.path))
                calls = {"n": 0}
                def probe_two_phase(endpoint):
                    calls["n"] += 1
                    if calls["n"] == 1:
                        return MODULE.IDLE, "idle at plan time"
                    return MODULE.ACTIVE, "a review started after planning"
                kills = []
                plan = MODULE.ensure_fresh(home, "/work/co1", "auto", processes=procs, probe=probe_two_phase,
                                           killer=lambda p, c: (kills.append(p), (True, "x"))[1])
                self.assertEqual(kills, [], "the immediate idle re-check must abort the recycle")
                self.assertEqual(plan["brokers"][0]["action"], MODULE.DEFER)
        finally:
            broker.close()


class TargetingTests(unittest.TestCase):
    def test_cwd_match_is_exact_not_a_prefix(self):
        procs = [proc(100, 1, 1.0, broker_cmd("/tmp/a", "/work/co10")),
                 proc(200, 1, 2.0, broker_cmd("/tmp/b", "/work/co1"))]
        self.assertEqual([p["pid"] for p in MODULE.select_brokers(procs, "/work/co1")], [200])

    def test_children_are_only_the_parents_app_server_children(self):
        procs = [proc(100, 1, 1.0, broker_cmd("/tmp/a")), proc(101, 100, 2.0, CHILD_CMD),
                 proc(102, 100, 3.0, UNRELATED_CHILD), proc(103, 9, 4.0, CHILD_CMD)]
        self.assertEqual([c["pid"] for c in MODULE.select_children(procs, 100)], [101])

    def test_recycle_refuses_unsafe_pids(self):
        ok, detail = MODULE.recycle(1, [])
        self.assertFalse(ok)
        self.assertEqual(detail, "refused_unsafe_pid")


class SecretHygieneTests(unittest.TestCase):
    def test_read_refresh_epoch_never_reads_token_material(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            epoch = auth(home)
            self.assertIsNotNone(epoch)
            self.assertNotIn(CANARY, repr(epoch))

    def test_missing_and_unparseable_auth_are_typed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            self.assertTrue(MODULE.read_refresh_epoch(home)[1].startswith("auth_store_unreadable"))
            (home / "auth.json").write_text(json.dumps({"last_refresh": "nope"}), encoding="utf-8")
            self.assertEqual(MODULE.read_refresh_epoch(home)[1], "auth_refresh_missing_or_unparseable")

    def test_cli_emits_no_secret_material(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            auth(home)
            result = subprocess.run(
                [sys.executable, str(HELPER), "--home", str(home), "--cwd", "/work/absent", "--json"],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            blob = result.stdout + result.stderr
            self.assertNotIn(CANARY, blob)
            self.assertNotIn("refresh_token", blob)
            self.assertIn("noop", blob)


class PreSignalRevalidationTests(unittest.TestCase):
    """The planning snapshot is never trusted for a signal."""

    def _home(self, tmp):
        home = pathlib.Path(tmp)
        return home, auth(home)

    def test_parent_disappearance_is_treated_as_already_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, refresh = self._home(tmp)
            plan_procs = [proc(100, 1, refresh - 100.0, broker_cmd("/tmp/x"))]
            fresh_procs = []
            calls = {"n": 0}
            def provider():
                calls["n"] += 1
                return plan_procs if calls["n"] == 1 else fresh_procs
            kills = []
            plan = MODULE.ensure_fresh(
                home, "/work/co1", "auto",
                process_provider=provider,
                probe=lambda ep: (MODULE.IDLE, "idle"),
                killer=lambda p, c: (kills.append(p), (True, "x"))[1],
            )
            self.assertEqual(plan["brokers"][0]["action"], MODULE.RECYCLE)
            self.assertIn("already absent", plan["brokers"][0]["reason"])

    def test_pid_reuse_or_command_drift_is_fail_safe_with_no_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, refresh = self._home(tmp)
            planned = [proc(100, 1, refresh - 100.0, broker_cmd("/tmp/x"))]
            reused = [proc(100, 1, refresh + 5.0, "node /somewhere/else --cwd /work/co1")]
            calls = {"n": 0}
            def provider():
                calls["n"] += 1
                return planned if calls["n"] == 1 else reused
            kills = []
            plan = MODULE.ensure_fresh(home, "/work/co1", "auto", process_provider=provider,
                                       probe=lambda ep: (MODULE.IDLE, "idle"),
                                       killer=lambda p, c: (kills.append(p), (True, "x"))[1])
            self.assertEqual(kills, [], "drifted identity must never be signalled")
            self.assertEqual(plan["brokers"][0]["action"], MODULE.FAIL_SAFE)

    def test_child_set_drift_uses_the_fresh_child_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, refresh = self._home(tmp)
            planned = [proc(100, 1, refresh - 100.0, broker_cmd("/tmp/x")), proc(101, 100, 1.0, CHILD_CMD)]
            fresh = planned + [proc(104, 100, 2.0, CHILD_CMD)]
            calls = {"n": 0}
            def provider():
                calls["n"] += 1
                return planned if calls["n"] == 1 else fresh
            kills = []
            MODULE.ensure_fresh(home, "/work/co1", "auto", process_provider=provider,
                                probe=lambda ep: (MODULE.IDLE, "idle"),
                                killer=lambda p, c: (kills.append((p, list(c))), (True, "x"))[1])
            self.assertEqual(kills, [(100, [101, 104])], "children are recomputed from the fresh snapshot")


class RecycleVerificationTests(unittest.TestCase):
    def test_survivor_after_sigkill_is_not_success(self):
        ok, detail = MODULE.recycle(4242, [4243], kill=lambda p, s: None, alive=lambda p: True,
                                    sleep=lambda s: None, grace_seconds=0.01)
        self.assertFalse(ok)
        self.assertIn("survivor_after_sigkill", detail)

    def test_verified_gone_is_success(self):
        ok, detail = MODULE.recycle(4242, [4243], kill=lambda p, s: None, alive=lambda p: False,
                                    sleep=lambda s: None, grace_seconds=0.01)
        self.assertTrue(ok)
        self.assertIn("recycled", detail)

    def test_permission_failure_is_typed(self):
        def deny(pid, sig):
            raise PermissionError(1, "denied")
        ok, detail = MODULE.recycle(4242, [], kill=deny, alive=lambda p: True,
                                    sleep=lambda s: None, grace_seconds=0.01)
        self.assertFalse(ok)
        self.assertIn("permission_denied", detail)

    def test_failed_recycle_never_permits_acquisition(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            refresh = auth(home)
            procs = [proc(100, 1, refresh - 100.0, broker_cmd("/tmp/x")), proc(101, 100, 1.0, CHILD_CMD)]
            plan = MODULE.ensure_fresh(home, "/work/co1", "auto", processes=procs,
                                       probe=lambda ep: (MODULE.IDLE, "idle"),
                                       killer=lambda p, c: (False, "survivor_after_sigkill:pid=100"))
            entry = plan["brokers"][0]
            self.assertEqual(entry["action"], MODULE.FAIL_SAFE)
            self.assertIn("do not acquire", entry["reason"])


if __name__ == "__main__":
    unittest.main()
