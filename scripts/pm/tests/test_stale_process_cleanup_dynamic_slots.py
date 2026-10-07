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


FREE = dict(status="free", occupied=0, idle=1, activity=None, issue=None, task=None, repository_id=None, branch=None,
            branch_ref=None, work_kind=None, handoff_id=None, claimed_at=None, active_turn_state="inactive",
            active_turn_id=None)


class DuplicateDevServerTests(unittest.TestCase):
    """Slot 2 clone. Duplicates are reaped only on a proven free+idle slot;
    otherwise they are report-only (CTO REVISE 1791393355.881389)."""

    CLONE = "/Users/rajiv/Downloads/projects/heydonna-app-3002"

    def state(self, free: bool, **overrides):
        base = {**record(2), **(FREE if free else {}), **overrides}
        st = {**base, "root": self.CLONE, "port": 3002}
        st["free_idle"] = st["status"] == "free" and st["occupied"] == 0 and st["idle"] == 1
        return {2: st}

    def collect(self, procs, ports, states=None):
        ps = "".join(f"{pid} {ppid} {UID} {etime} {cmd}\n" for pid, ppid, etime, cmd in procs)

        def fake_run(args, timeout=5):
            if args[0] == "ps":
                return completed(ps)
            if args[0] == "lsof" and "-iTCP" in args:
                pid = int(args[args.index("-p") + 1])
                return completed("".join(f"n*:{p}\n" for p in ports.get(pid, [])))
            return completed("", 1)

        states = self.state(True) if states is None else states
        with patch.object(MODULE, "run", side_effect=fake_run), patch.object(
            MODULE, "get_cwd", return_value=self.CLONE
        ), patch.object(MODULE, "get_start_time", return_value=START):
            return MODULE.collect_processes(60, 360, 30, (states, None))

    def tree(self, root, etime):
        node = f"node {self.CLONE}/node_modules/.bin/next dev"
        return [(root, 900, etime, node), (root + 1, root, etime, "next-server (v15.5.26)")]

    def test_second_tree_in_free_clone_is_flagged_and_slot_server_is_not(self) -> None:
        result = self.collect(self.tree(100, "02:00:00") + self.tree(200, "30:00"), {101: [3002], 201: [3000]})
        flagged = {row["pid"]: row for row in result["candidates"]}
        self.assertEqual(set(flagged), {200, 201})
        self.assertEqual(flagged[200]["policy"], "duplicate_dev_server")
        self.assertIn("wrong_port_dev_server", flagged[201]["reap_reason"])

    def test_duplicate_not_listening_is_flagged_when_keeper_exists(self) -> None:
        result = self.collect(self.tree(100, "02:00:00") + self.tree(200, "30:00"), {101: [3002]})
        self.assertEqual({row["pid"] for row in result["candidates"]}, {200, 201})
        self.assertIn("keeper_pid=100", result["candidates"][0]["reap_reason"])

    def test_busy_unknown_and_missing_row_duplicates_are_report_only(self) -> None:
        cases = {
            "busy": self.state(False),
            "unknown": self.state(False, status="UNKNOWN", occupied=0, idle=0, active_turn_state="unknown"),
            "missing_row": {},
        }
        for name, states in cases.items():
            with self.subTest(name):
                result = self.collect(self.tree(100, "02:00:00") + self.tree(200, "30:00"), {101: [3002], 201: [3000]}, states)
                self.assertEqual(result["candidates"], [])
                self.assertEqual({item["pid"] for item in result["report_only"]}, {200, 201})
                self.assertTrue(all(item["associated_slot"] == 2 for item in result["report_only"]))

    def test_single_slot_server_is_not_flagged(self) -> None:
        result = self.collect(self.tree(100, "02:00:00"), {101: [3002]})
        self.assertEqual(result["candidates"], [])

    def test_young_duplicate_is_protected_by_ten_minute_floor(self) -> None:
        result = self.collect(self.tree(100, "02:00:00") + self.tree(200, "05:00"), {101: [3002], 201: [3000]})
        self.assertEqual(result["candidates"], [])

    def row(self, pid, ppid, policy="duplicate_dev_server"):
        return {"pid": pid, "ppid": ppid, "uid": UID, "category": "nextjs",
                "command_identity": ("nextjs", "next-server" if ppid != 900 else f"{self.CLONE}/node_modules/.bin/next"),
                "start_time": START, "cwd": self.CLONE, "associated_slot": 2, "policy": policy,
                "owner_proof": "mop_free_idle_inactive_no_assignment"}

    def kill(self, members, live_children, ports, lsof_timeout=()):
        def fake_run(args, timeout=5):
            if args[:2] == ["ps", "-axo"]:
                return completed("".join(f"{c} {p}\n" for p, cs in live_children.items() for c in cs))
            if args[0] == "lsof" and "-iTCP" in args:
                pid = int(args[args.index("-p") + 1])
                if pid in lsof_timeout:
                    raise subprocess.TimeoutExpired(args, timeout)
                out = "".join(f"n*:{p}\n" for p in ports.get(pid, []))
                return completed(out, 0 if out else 1)
            return completed("", 1)

        by_pid = {m["pid"]: m for m in members}
        with patch.object(MODULE, "run", side_effect=fake_run), patch.object(
            MODULE, "_process_snapshot", side_effect=lambda pid: dict(by_pid[pid])
        ), patch.object(MODULE, "load_slot_inventory", return_value=(self.state(True), None)), patch.object(
            MODULE, "alive", return_value=False
        ), patch.object(MODULE, "time"), patch.object(MODULE.os, "kill") as kill:
            outcomes = MODULE.kill_candidates(members, 0)
        return outcomes, kill

    def test_parent_with_canonical_child_is_preserved(self) -> None:
        outcomes, kill = self.kill([self.row(200, 900)], {200: [201]}, {201: [3002]})
        kill.assert_not_called()
        self.assertEqual(outcomes[0]["outcome"], "refused")

    def test_lsof_timeout_preserves_tree(self) -> None:
        members = [self.row(200, 900), self.row(201, 200)]
        outcomes, kill = self.kill(members, {200: [201]}, {}, lsof_timeout=(201,))
        kill.assert_not_called()
        self.assertEqual({o["outcome"] for o in outcomes}, {"refused"})

    def test_revalidation_refuses_on_unreadable_listener(self) -> None:
        outcomes, kill = self.kill([self.row(201, 1)], {}, {}, lsof_timeout=(201,))
        kill.assert_not_called()
        self.assertEqual(outcomes[0]["outcome"], "refused")

    def test_proven_orphan_tree_is_still_terminated(self) -> None:
        members = [self.row(200, 900), self.row(201, 200)]
        outcomes, kill = self.kill(members, {200: [201]}, {201: [3000]})
        self.assertEqual(kill.call_count, 2)
        self.assertEqual({o["outcome"] for o in outcomes}, {"terminated"})

    def test_revalidation_refuses_when_duplicate_now_owns_slot_port(self) -> None:
        row = self.row(201, 200)
        states = self.state(True)
        for ports, expected in (([3002], (False, "duplicate_now_owns_slot_port")),
                                ([3000], (True, "duplicate_dev_server_identity_match"))):
            out = "".join(f"n*:{p}\n" for p in ports)
            with patch.object(MODULE, "_process_snapshot", return_value={**row, "ppid": 1}), patch.object(
                MODULE, "load_slot_inventory", return_value=(states, None)
            ), patch.object(MODULE, "run", return_value=completed(out)):
                self.assertEqual(MODULE.revalidate_candidate(row), expected)

    def test_revalidation_refuses_duplicate_when_slot_became_busy(self) -> None:
        row = self.row(201, 200)
        with patch.object(MODULE, "_process_snapshot", return_value=dict(row)), patch.object(
            MODULE, "load_slot_inventory", return_value=(self.state(False), None)
        ), patch.object(MODULE, "run", return_value=completed("n*:3000\n")):
            self.assertEqual(MODULE.revalidate_candidate(row), (False, "slot_owner_evidence_changed"))

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
