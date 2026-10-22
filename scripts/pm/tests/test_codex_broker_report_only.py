"""Broker identity/age never prove quiescence: even --apply is report-only."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

ROOT = Path(__file__).parents[3]
MODULE_PATH = ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "scripts" / "stale-process-cleanup.py"
SPEC = importlib.util.spec_from_file_location("stale_process_cleanup_broker", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

UID = os.getuid()
BROKER = (
    "/opt/homebrew/Cellar/node/24.9.0_1/bin/node /Users/rajiv/.claude/plugins/cache/openai-codex/"
    "codex/1.0.1/scripts/app-server-broker.mjs serve --endpoint unix:/tmp/mop-fixture-broker.sock --cwd /x"
)
SERVER = "node /opt/homebrew/bin/codex app-server"
TOOL = "/bin/sh -c long-running-local-tool --no-network"


def proc(pid: int, ppid: int, command: str, age_h: float = 20) -> dict:
    return {"pid": pid, "ppid": ppid, "uid": UID, "etime": "x", "age_seconds": int(age_h * 3600),
            "stat": "S", "start_time": f"t{pid}", "command": command}


# Detached old broker with a live descendant tool: it must never be signalled.
TABLE = {
    1: proc(1, 0, "/sbin/launchd", 99),
    100: proc(100, 1, BROKER),
    101: proc(101, 100, SERVER),
    102: proc(102, 101, TOOL),
}


def table():
    return {pid: dict(row) for pid, row in TABLE.items()}


def forbid(name):
    def _raise(*args, **kwargs):
        raise AssertionError(f"must not call {name}{args!r}")
    return _raise


class CodexBrokerCleanupTests(unittest.TestCase):
    def test_report_mode_reports_candidates_without_any_signal(self):
        with patch.object(MODULE, "codex_broker_table", side_effect=table), \
                patch.object(MODULE.os, "kill", side_effect=forbid("os.kill")), \
                patch.object(MODULE, "run", side_effect=forbid("run")), \
                patch.object(MODULE.time, "sleep", side_effect=forbid("sleep")):
            out = MODULE.codex_broker_orphan_check(False, 6.0, 0)
        self.assertEqual(out["action"], "report_only")
        self.assertEqual([row["pid"] for row in out["found"]], [100])
        self.assertEqual(out["found"][0]["member_pids"], [100, 101, 102])
        self.assertEqual((out["killed"], out["terminated"], out["exiting_pids"]), (0, [], []))

    def test_apply_active_tool_tree_is_hard_report_only(self):
        with patch.object(MODULE, "codex_broker_table", side_effect=table) as snapshots, \
                patch.object(MODULE.os, "kill", side_effect=forbid("os.kill")) as send, \
                patch.object(MODULE, "_kill_members", side_effect=forbid("_kill_members")) as reap, \
                patch.object(MODULE, "run", side_effect=forbid("run")), \
                patch.object(MODULE.time, "sleep", side_effect=forbid("sleep")):
            out = MODULE.codex_broker_orphan_check(True, 6.0, 0)
            repeated = MODULE.codex_broker_orphan_check(True, 6.0, 0)
        send.assert_not_called()
        reap.assert_not_called()
        self.assertEqual(snapshots.call_count, 2)
        self.assertEqual(out, repeated)
        self.assertEqual(out["found"][0]["member_pids"], [100, 101, 102])
        self.assertEqual((out["action"], out["killed"], out["before"], out["after"]),
                         ("report_only", 0, 3, 3))
        self.assertEqual(out["terminated"], [])
        self.assertFalse(out["refused"] or out["errors"])

    def test_unreadable_empty_or_changed_process_evidence_is_kept(self):
        for initial, fresh in ((None, None), ({}, {}), (table(), None)):
            with self.subTest(initial=initial, fresh=fresh), \
                    patch.object(MODULE, "codex_broker_table", side_effect=[initial] + [fresh] * 10), \
                    patch.object(MODULE.os, "kill", side_effect=forbid("os.kill")):
                out = MODULE.codex_broker_orphan_check(True, 6.0, 0)
                self.assertEqual(out["killed"], 0)
                if initial is None:
                    self.assertEqual(out["errors"], ["ps_failed"])
        # Retain the identity/ancestry negative fixtures at their pure verifier;
        # neither verification success nor missing descendants authorizes a signal.
        entry = MODULE.codex_broker_plan(table(), 6 * 3600, UID, 9999)["roots"][0]
        for pid, field, value in ((100, "start_time", "reused"), (100, "ppid", 42),
                                  (101, "start_time", "reused"), (102, "ppid", 42)):
            changed = table()
            changed[pid][field] = value
            with self.subTest(pid=pid, field=field):
                self.assertFalse(MODULE.codex_broker_verify(entry, changed, 6 * 3600, UID, 9999)[0])

    def test_apply_childless_old_broker_is_also_report_only(self):
        with patch.object(MODULE, "codex_broker_table", return_value={100: proc(100, 1, BROKER)}), \
                patch.object(MODULE.os, "kill", side_effect=forbid("os.kill")), \
                patch.object(MODULE, "_kill_members", side_effect=forbid("_kill_members")):
            out = MODULE.codex_broker_orphan_check(True, 0, 0)
        self.assertEqual((out["action"], out["killed"], out["after"]), ("report_only", 0, 1))

    def test_never_signals_live_sessions_young_foreign_exiting_or_standalone_servers(self):
        fixtures = []
        for owner in ("claude", "openclaude", "tmux", "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"):
            live = table()
            live[42] = proc(42, 1, owner)
            live[100]["ppid"] = 42
            fixtures.append(live)
        for field, value in (("age_seconds", 60), ("uid", UID + 1), ("stat", "Z"), ("stat", "E"),
                             ("command", SERVER), ("command", BROKER.replace(" serve ", " inspect "))):
            live = table()
            live[100][field] = value
            fixtures.append(live)
        for live in fixtures:
            with self.subTest(root=live[100]), \
                    patch.object(MODULE, "codex_broker_table", return_value=live), \
                    patch.object(MODULE.os, "kill", side_effect=forbid("os.kill")):
                self.assertEqual(MODULE.codex_broker_orphan_check(True, 6.0, 0)["killed"], 0)

    def test_pid_reuse_after_term_refuses_kill(self):
        live = table()
        entry = MODULE.codex_broker_plan(live, 6 * 3600, UID, 9999)["roots"][0]
        # Exercise retained post-TERM PID-reuse guards with synthetic observations
        # only. The broker reporting path now issues neither TERM nor KILL.
        live[102]["start_time"] = "reused"
        with patch.object(MODULE.os, "kill", side_effect=forbid("os.kill")) as send:
            ok, reason = MODULE.codex_broker_verify(entry, live, 6 * 3600, UID, 9999, TABLE[102], True)
        self.assertFalse(ok)
        self.assertEqual(reason, "member_identity_changed")
        send.assert_not_called()

    def test_main_entrypoint_obeys_apply_and_age_threshold(self):
        for apply, threshold, expected in ((False, 0, []), (True, 0, []), (True, 1000000, [])):
            with self.subTest(apply=apply, threshold=threshold), tempfile.TemporaryDirectory() as temp:
                output = Path(temp) / "p.json"
                argv = ["x", "--attest-slotless-reap", "--output", str(output),
                        "--status-output", str(Path(temp) / "s.json"), "--codex-broker-min-hours", str(threshold)]
                if apply:
                    argv.append("--apply")
                with patch.object(MODULE, "load_slot_inventory", return_value=({}, "malformed_row:slot=4")), \
                        patch.object(MODULE, "load_history", side_effect=lambda *a, **k: {}), \
                        patch.object(MODULE, "save_history"), \
                        patch.object(MODULE, "collect_processes", return_value={"ok": True, "rows": [], "candidates": [], "skipped": []}), \
                        patch.object(MODULE, "preview_orphan_scan", return_value={"class": "preview_orphan", "found": [], "terminated": [], "errors": []}), \
                        patch.object(MODULE, "runaway_scan", return_value={"class": "runaway_scan", "found": [], "terminated": [], "errors": []}), \
                        patch.object(MODULE, "replayd_check", return_value={"pid": None, "cpu": None, "streak": 0, "action": "none"}), \
                        patch.object(MODULE, "codex_renderer_check", return_value={"class": "codex_renderer", "observed": 0, "over_threshold": 0, "pending": 0, "found": [], "terminated": [], "refused": [], "errors": []}), \
                        patch.object(MODULE, "codex_broker_table", side_effect=table), \
                        patch.object(MODULE, "_kill_members", side_effect=forbid("_kill_members")), \
                        patch.object(MODULE.os, "kill") as send, patch.object(MODULE, "alive", return_value=False), \
                        patch.object(MODULE.time, "sleep"), patch.object(MODULE, "run", side_effect=forbid("run")), \
                        patch.object(sys, "argv", argv), patch.object(sys, "stdout", new_callable=io.StringIO) as out:
                    MODULE.main()
                self.assertEqual(send.call_args_list, [call(pid, signal.SIGTERM) for pid in expected])
                self.assertIn("CODEX_BROKER_ORPHAN", out.getvalue())
                self.assertIn("action=report_only", out.getvalue())
                self.assertEqual(json.loads(output.read_text())["codex_broker_orphan"]["killed"], len(expected))


if __name__ == "__main__":
    unittest.main()
