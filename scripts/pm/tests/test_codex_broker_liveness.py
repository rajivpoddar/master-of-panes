"""Fixture proof: the Codex broker reaper signals only trees with positive inactive evidence."""

from __future__ import annotations

import importlib.util
import os
import signal
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[3]
MODULE_PATH = ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "scripts" / "stale-process-cleanup.py"
SPEC = importlib.util.spec_from_file_location("stale_process_cleanup_liveness", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

UID = os.getuid()
BROKER = (
    "/opt/homebrew/Cellar/node/24.9.0_1/bin/node /Users/rajiv/.claude/plugins/cache/openai-codex/"
    "codex/1.0.1/scripts/app-server-broker.mjs serve --endpoint unix:/tmp/mop-fixture-broker.sock --cwd /x"
)
SERVER = "node /opt/homebrew/bin/codex app-server"
SOCK = "/tmp/mop-fixture-broker.sock"


def proc(pid: int, ppid: int, command: str, age_h: float = 20, stat: str = "S") -> dict:
    return {"pid": pid, "ppid": ppid, "uid": UID, "etime": "x", "age_seconds": int(age_h * 3600),
            "stat": stat, "start_time": f"t{pid}", "command": command}


def fd(num: str, ftype: str, name: str, tst: str = "") -> dict:
    return {"fd": num, "type": ftype, "name": name, "tst": tst}


# A detached broker (ppid 1) with one app-server child: stdio pipes and the listening socket only.
IDLE = {100: [fd("0", "PIPE", "->0x1"), fd("3", "unix", SOCK)], 101: [fd("0", "PIPE", "->0x2"), fd("1", "PIPE", "->0x3")]}
CLIENT = {**IDLE, 100: IDLE[100] + [fd("4", "unix", SOCK)]}                       # accepted client connection
STREAM = {**IDLE, 101: IDLE[101] + [fd("7", "IPv4", "10.0.0.1:5->1.2.3.4:443", "ESTABLISHED")]}  # active upstream stream
PEER = {**IDLE, 100: IDLE[100] + [fd("5", "unix", "->0xabc")]}                     # connected non-stdio unix peer


class World:
    def __init__(self, records_by_call):
        self.table = {1: proc(1, 0, "/sbin/launchd", 99), 100: proc(100, 1, BROKER), 101: proc(101, 100, SERVER)}
        self.records_by_call = list(records_by_call)
        self.calls = 0
        self.signals: list[tuple[int, int]] = []

    def snapshot(self):
        return {pid: dict(row) for pid, row in self.table.items()}

    def lsof(self, pids):
        record = self.records_by_call[min(self.calls, len(self.records_by_call) - 1)]
        self.calls += 1
        return None if record is None else {p: record[p] for p in pids if p in record}

    def kill(self, pid, sig):
        self.signals.append((pid, sig))
        if sig == signal.SIGKILL:
            self.table.pop(pid, None)

    def reap(self, holders=frozenset()):
        with patch.object(MODULE, "codex_broker_table", side_effect=self.snapshot), \
                patch.object(MODULE, "codex_lsof_records", side_effect=self.lsof), \
                patch.object(MODULE, "codex_endpoint_holders", return_value=set(holders)), \
                patch.object(MODULE.os, "kill", side_effect=self.kill), \
                patch.object(MODULE.time, "sleep"), \
                patch.object(MODULE, "CODEX_BROKER_POLL_S", 0.0):
            return MODULE.codex_broker_orphan_check(True, 6.0, 0.0)


class CodexBrokerLivenessTests(unittest.TestCase):
    def test_genuinely_dead_orphan_is_selectable_and_reaped(self):
        world = World([IDLE])
        out = world.reap()
        self.assertEqual(out["roots"], 1)
        self.assertEqual({pid for pid, _ in world.signals}, {100, 101})
        self.assertIn((100, signal.SIGKILL), world.signals)
        self.assertEqual(out["kept_liveness"], [])

    def test_report_only_scan_selects_dead_orphan_without_signals(self):
        world = World([IDLE])
        with patch.object(MODULE, "codex_broker_table", side_effect=world.snapshot), \
                patch.object(MODULE, "codex_lsof_records", side_effect=world.lsof), \
                patch.object(MODULE, "codex_endpoint_holders", return_value=set()), \
                patch.object(MODULE.os, "kill", side_effect=world.kill):
            out = MODULE.codex_broker_orphan_check(False, 6.0, 0.0)
        self.assertEqual((out["roots"], world.signals), (1, []))

    def test_old_detached_broker_with_active_socket_client_gets_zero_signals(self):
        world = World([CLIENT])
        out = world.reap()
        self.assertEqual(world.signals, [])
        self.assertEqual(out["roots"], 0)
        self.assertTrue(out["kept_liveness"][0]["reason"].startswith("client_connected"))

    def test_active_stream_unix_peer_or_outside_endpoint_holder_gets_zero_signals(self):
        for name, records, holders in (("tcp stream", STREAM, ()), ("unix peer", PEER, ()), ("outside holder", IDLE, (4242,))):
            world = World([records])
            world.reap(holders)
            self.assertEqual(world.signals, [], name)

    def test_unknown_liveness_gets_zero_signals(self):
        for name, records in (("lsof unavailable", None), ("pid unreadable", {100: IDLE[100]}), ("no fds", {100: [], 101: []})):
            world = World([records])
            out = world.reap()
            self.assertEqual(world.signals, [], name)
            self.assertEqual(out["kept_liveness"][0]["state"], "unknown", name)

    def test_unreadable_endpoint_holders_is_unknown(self):
        world = World([IDLE])
        with patch.object(MODULE, "codex_broker_table", side_effect=world.snapshot), \
                patch.object(MODULE, "codex_lsof_records", side_effect=world.lsof), \
                patch.object(MODULE, "codex_endpoint_holders", return_value=None), \
                patch.object(MODULE.os, "kill", side_effect=world.kill), patch.object(MODULE.time, "sleep"):
            out = MODULE.codex_broker_orphan_check(True, 6.0, 0.0)
        self.assertEqual(world.signals, [])
        self.assertEqual(out["kept_liveness"][0]["reason"], "endpoint_holders_unreadable")

    def test_evidence_rechecked_before_term_and_before_kill(self):
        # Evidence reads: 1 select, 2 pre-TERM revalidation, 3 pre-KILL revalidation.
        before_term = World([IDLE, CLIENT])
        before_term.reap()
        self.assertEqual(before_term.signals, [], "client connects before the first signal: nothing is sent")

        before_kill = World([IDLE, IDLE, CLIENT])
        before_kill.reap()
        self.assertTrue(before_kill.signals)
        self.assertTrue(all(sig == signal.SIGTERM for _, sig in before_kill.signals), "no SIGKILL once a client appears")


if __name__ == "__main__":
    unittest.main()
