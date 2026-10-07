"""Dynamic MoP slot set, duplicate dev-server policy and heartbeat status file.

Rajiv 2026-10-07 22:03 IST (DM D0AMF0XE6TS thread 1791389413.287779):
"do it. also remove remove the hardcoded slot range."
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[3]
ASSETS = ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "scripts"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = load("stale_process_cleanup_dyn", ASSETS / "stale-process-cleanup.py")
COMPOSE = load("heartbeat_compose_dyn", ASSETS / "pm" / "heartbeat" / "heartbeat-compose.py")
UID = os.getuid()
START = "Mon Oct 7 00:00:00 2026"


def completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["fixture"], returncode, stdout, "")


def record(slot: int, **overrides):
    row = {
        "slot": slot, "address": f"0:0.{slot}", "status": "active", "occupied": 1, "idle": 0, "dnd": 0,
        "activity": "working", "issue": 100 + slot, "task": "t", "repository_id": "r", "branch": "b",
        "branch_ref": "b", "pr": None, "head_sha": None, "work_kind": "new_issue", "handoff_id": "h",
        "claimed_at": "x", "active_turn_state": "active", "active_turn_id": "turn", "last_activity": "x",
    }
    row.update(overrides)
    return row


def read_inventory(records):
    with tempfile.TemporaryDirectory() as temp:
        db = Path(temp) / "mop.db"
        db.touch()
        with patch.object(MODULE, "MOP_DB", db), patch.object(MODULE, "pane_root", return_value=None), patch.object(
            MODULE, "run", return_value=completed(json.dumps(records))
        ) as run:
            states, error = MODULE.load_slot_inventory()
            sql = run.call_args_list[0].args[0][-1]
    return states, error, sql


class DynamicSlotSetTests(unittest.TestCase):
    def test_nine_slots_from_mop_are_not_refused(self) -> None:
        states, error, sql = read_inventory([record(n) for n in range(1, 10)])
        self.assertIsNone(error)
        self.assertEqual(sorted(states), list(range(1, 10)))
        self.assertEqual(states[9]["root"], "/Users/rajiv/Downloads/projects/heydonna-app-3009")
        self.assertEqual(states[9]["port"], 3009)
        self.assertNotIn("BETWEEN", sql)
        self.assertIn("slot >= 1", sql)
        self.assertFalse(hasattr(MODULE, "SLOT_NUMBERS"))

    def test_gap_in_slot_numbers_is_not_incomplete(self) -> None:
        states, error, _ = read_inventory([record(1), record(3)])
        self.assertIsNone(error)
        self.assertEqual(sorted(states), [1, 3])

    def test_malformed_row_is_refused_with_slot_named(self) -> None:
        bad = record(4)
        del bad["address"]
        _, error, _ = read_inventory([record(1), bad])
        self.assertEqual(error, "malformed_row:slot=4")

    def test_pane_cwd_root_wins_over_convention(self) -> None:
        with patch.object(MODULE, "run", return_value=completed("/Users/rajiv/Downloads/projects/heydonna-app-3011/app\n")):
            self.assertEqual(MODULE.slot_root(9, "0:0.9"), "/Users/rajiv/Downloads/projects/heydonna-app-3011")
        with patch.object(MODULE, "run", return_value=completed("/private/tmp/elsewhere\n")):
            self.assertEqual(MODULE.slot_root(9, "0:0.9"), "/Users/rajiv/Downloads/projects/heydonna-app-3009")


class DuplicateDevServerTests(unittest.TestCase):
    """Slot 2 clone, slot busy (active turn): duplicates are still reaped."""

    CLONE = "/Users/rajiv/Downloads/projects/heydonna-app-3002"

    def collect(self, procs, ports):
        ps = "".join(f"{pid} {ppid} {UID} {etime} {cmd}\n" for pid, ppid, etime, cmd in procs)

        def fake_run(args, timeout=5):
            if args[0] == "ps":
                return completed(ps)
            if args[0] == "lsof" and "-iTCP" in args:
                pid = int(args[args.index("-p") + 1])
                return completed("".join(f"n*:{p}\n" for p in ports.get(pid, [])))
            return completed("", 1)

        states = {2: {**record(2), "root": self.CLONE, "port": 3002, "free_idle": False}}
        with patch.object(MODULE, "run", side_effect=fake_run), patch.object(
            MODULE, "get_cwd", return_value=self.CLONE
        ), patch.object(MODULE, "get_start_time", return_value=START):
            return MODULE.collect_processes(60, 360, 30, (states, None))

    def tree(self, root, etime):
        node = f"node {self.CLONE}/node_modules/.bin/next dev"
        return [(root, 900, etime, node), (root + 1, root, etime, "next-server (v15.5.26)")]

    def test_second_tree_in_clone_is_flagged_and_slot_server_is_not(self) -> None:
        result = self.collect(self.tree(100, "02:00:00") + self.tree(200, "30:00"), {101: [3002], 201: [3000]})
        flagged = {row["pid"]: row for row in result["candidates"]}
        self.assertEqual(set(flagged), {200, 201})
        self.assertEqual(flagged[200]["policy"], "duplicate_dev_server")
        self.assertIn("wrong_port_dev_server", flagged[201]["reap_reason"])
        self.assertNotIn(100, flagged)
        self.assertNotIn(101, flagged)

    def test_duplicate_not_listening_is_flagged_when_keeper_exists(self) -> None:
        result = self.collect(self.tree(100, "02:00:00") + self.tree(200, "30:00"), {101: [3002]})
        self.assertEqual({row["pid"] for row in result["candidates"]}, {200, 201})
        self.assertIn("keeper_pid=100", result["candidates"][0]["reap_reason"])

    def test_single_slot_server_is_not_flagged(self) -> None:
        result = self.collect(self.tree(100, "02:00:00"), {101: [3002]})
        self.assertEqual(result["candidates"], [])

    def test_young_duplicate_is_protected_by_ten_minute_floor(self) -> None:
        result = self.collect(self.tree(100, "02:00:00") + self.tree(200, "05:00"), {101: [3002], 201: [3000]})
        self.assertEqual(result["candidates"], [])

    def test_revalidation_refuses_when_duplicate_now_owns_slot_port(self) -> None:
        row = {"pid": 201, "ppid": 200, "uid": UID, "category": "nextjs", "command_identity": ("nextjs", "next-server"),
               "start_time": START, "cwd": self.CLONE, "associated_slot": 2, "policy": "duplicate_dev_server"}
        snap = {**row}
        states = {2: {**record(2), "root": self.CLONE, "port": 3002}}
        with patch.object(MODULE, "_process_snapshot", return_value={**snap, "ppid": 1}), patch.object(
            MODULE, "load_slot_inventory", return_value=(states, None)
        ), patch.object(MODULE, "listen_ports", return_value=[3002]):
            self.assertEqual(MODULE.revalidate_candidate(row), (False, "duplicate_now_owns_slot_port"))
        with patch.object(MODULE, "_process_snapshot", return_value={**snap, "ppid": 1}), patch.object(
            MODULE, "load_slot_inventory", return_value=(states, None)
        ), patch.object(MODULE, "listen_ports", return_value=[3000]):
            self.assertEqual(MODULE.revalidate_candidate(row), (True, "duplicate_dev_server_identity_match"))

    def test_young_pm_worktree_is_never_eligible(self) -> None:
        ps = f"300 1 {UID} 45:00 /opt/agent-browser --remote-debugging-port=9222\n"
        with patch.object(MODULE, "run", return_value=completed(ps)), patch.object(
            MODULE, "get_cwd", return_value="/private/tmp/claude-501/wt-x"
        ), patch.object(MODULE, "get_start_time", return_value=START):
            result = MODULE.collect_processes(30, 360, 30, ({1: {**record(1), "root": "/nope", "port": 3001, "free_idle": False}}, None))
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["skipped"][0]["skip_reason"], "pm_worktree_younger_than_60m")


class StatusFileTests(unittest.TestCase):
    def test_refusal_writes_status_file_and_heartbeat_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            status = Path(temp) / "status.json"
            argv = ["x", "--apply", "--output", str(Path(temp) / "proof.json"), "--status-output", str(status)]
            with patch.object(MODULE, "load_slot_inventory", return_value=({}, "malformed_row:slot=4")), patch.object(
                sys, "argv", argv
            ), patch.object(sys, "stdout", new_callable=io.StringIO) as out:
                self.assertEqual(MODULE.main(), 2)
            doc = json.loads(status.read_text())
            self.assertTrue(doc["refused"])
            self.assertEqual(doc["reason"], "slot_inventory_unavailable:malformed_row:slot=4")
            self.assertIn("timestamp", doc)
            self.assertIn("STALE_PROCESS_CLEANUP_REFUSED", out.getvalue())
            alert = COMPOSE.cleanup_status_alert(status)
            self.assertEqual(alert, "stale-process-cleanup REFUSED: slot_inventory_unavailable:malformed_row:slot=4")
            post = COMPOSE.compose("1h", COMPOSE.datetime.now(COMPOSE.timezone.utc), None, {}, [], include_active=False,
                                   cleanup_alert=alert)
            self.assertIn("stale-process-cleanup REFUSED: slot_inventory_unavailable", post)

    def test_clean_run_has_no_alert(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            status = Path(temp) / "status.json"
            status.write_text(json.dumps({"refused": False, "error": False, "reason": None}))
            self.assertIsNone(COMPOSE.cleanup_status_alert(status))
            self.assertIsNone(COMPOSE.cleanup_status_alert(Path(temp) / "missing.json"))


if __name__ == "__main__":
    unittest.main()
