"""Codex broker cap policy is HARD REPORT-ONLY (CTO REVISE 2026-10-22): it never signals under any flag."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).parents[3]
MODULE_PATH = ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "scripts" / "stale-process-cleanup.py"
SPEC = importlib.util.spec_from_file_location("stale_process_cleanup_cap", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

UID = os.getuid()


def broker(sess: str, cwd: str) -> str:
    return ("/opt/homebrew/bin/node /Users/rajiv/.claude/plugins/cache/openai-codex/codex/1.0.1/scripts/app-server-broker.mjs serve "
            f"--endpoint unix:/var/folders/x/cxc-{sess}/broker.sock --cwd {cwd} --pid-file /var/folders/x/cxc-{sess}/broker.pid")


def proc(pid: int, ppid: int, command: str, age_min: int = 120) -> dict:
    return {"pid": pid, "ppid": ppid, "uid": UID, "etime": "x", "age_seconds": age_min * 60,
            "stat": "S", "start_time": f"t{pid}", "command": command}


def forbid(name):
    def _raise(*a, **k):
        raise AssertionError(f"must not call {name}{a!r}")
    return _raise


def make_table() -> dict:
    rows = [
        proc(1, 0, "/sbin/launchd", 9999),
        proc(60, 1, "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT", 900),
        proc(100, 1, broker("orphan", "/a")),                       # selected by (a)
        proc(101, 100, "node /opt/homebrew/bin/codex app-server"),  # its tree
        proc(102, 101, "/bin/sh -c long-running-local-tool"),       # active tool descendant
        proc(110, 1, broker("busy", "/b")),                         # in-flight companion
        proc(120, 60, broker("chatgpt", "/c"), 900),                # ChatGPT-owned
    ]
    return {r["pid"]: r for r in rows}


class BrokerCapReportOnlyTests(unittest.TestCase):
    def check(self, apply, companions, has_client=True):
        with patch.object(MODULE, "codex_broker_table", side_effect=make_table), \
                patch.object(MODULE, "codex_companion_texts", return_value=companions), \
                patch.object(MODULE, "codex_broker_has_client", return_value=has_client), \
                patch.object(MODULE.os, "kill", side_effect=forbid("os.kill")), \
                patch.object(MODULE, "_kill_members", side_effect=forbid("_kill_members")), \
                patch.object(MODULE.time, "sleep", side_effect=forbid("sleep")), \
                patch.object(MODULE, "run", side_effect=forbid("run")):
            return MODULE.codex_broker_policy_check(apply, 0, 0, 0, 0)

    def test_apply_never_signals_for_connected_noncompanion_and_tool_trees(self):
        for client in (True, False, None):
            for comps in ([], ["node codex-review-companion.mjs --endpoint unix:/var/folders/x/cxc-busy/broker.sock"]):
                out = self.check(True, comps, client)
                self.assertEqual(out["action"], "report_only")
                self.assertEqual(out["killed"], 0)
                self.assertEqual(out["terminated"], [])
        out = self.check(True, [], True)  # connected broker (has_client=True) is only reported
        self.assertIn(102, [w["pid"] for w in out["would_kill"]])
        self.assertNotIn(120, [w["pid"] for w in out["would_kill"]])  # ChatGPT exempt
        out = self.check(True, ["node codex-review-companion.mjs --endpoint unix:/var/folders/x/cxc-busy/broker.sock"])
        self.assertNotIn(110, [w["pid"] for w in out["would_kill"]])  # in-flight guard intact

    def test_failed_evidence_cases_never_signal(self):
        for table_ret, comp_ret in ((None, []), (make_table(), None)):
            with patch.object(MODULE, "codex_broker_table", return_value=table_ret), \
                    patch.object(MODULE, "codex_companion_texts", return_value=comp_ret), \
                    patch.object(MODULE.os, "kill", side_effect=forbid("os.kill")), \
                    patch.object(MODULE.time, "sleep", side_effect=forbid("sleep")):
                out = MODULE.codex_broker_policy_check(True, 0, 0, 0, 0)
            self.assertEqual(out["killed"], 0)
            self.assertTrue(out["errors"])

    def test_companion_evidence_fails_closed_on_nonzero_or_empty_ps(self):
        table = {1: proc(1, 0, "/sbin/launchd"), 9: proc(9, 1, "node /x/codex-review-companion.mjs review")}
        for res in (SimpleNamespace(returncode=1, stdout="x"), SimpleNamespace(returncode=0, stdout="  \n")):
            with patch.object(MODULE, "run", return_value=res):
                self.assertIsNone(MODULE.codex_companion_texts(table))

    def test_real_cli_apply_attestation_age0_has_zero_broker_signal_effects(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "p.json"
            argv = ["x", "--apply", "--attest-slotless-reap", "--output", str(output),
                    "--status-output", str(Path(temp) / "s.json"), "--codex-broker-min-hours", "0",
                    "--codex-broker-orphan-min", "0", "--codex-broker-extra-min", "0", "--codex-broker-idle-min", "0"]
            empty = {"ok": True, "rows": [], "candidates": [], "skipped": []}
            with patch.object(MODULE, "load_slot_inventory", return_value=({}, "malformed_row:slot=4")), \
                    patch.object(MODULE, "load_history", side_effect=lambda *a, **k: {}), \
                    patch.object(MODULE, "save_history"), \
                    patch.object(MODULE, "collect_processes", return_value=empty), \
                    patch.object(MODULE, "preview_orphan_scan", return_value={"class": "preview_orphan", "found": [], "terminated": [], "errors": []}), \
                    patch.object(MODULE, "runaway_scan", return_value={"class": "runaway_scan", "found": [], "terminated": [], "errors": []}), \
                    patch.object(MODULE, "replayd_check", return_value={"pid": None, "cpu": None, "streak": 0, "action": "none"}), \
                    patch.object(MODULE, "codex_renderer_check", return_value={"class": "codex_renderer", "observed": 0, "over_threshold": 0, "pending": 0, "found": [], "terminated": [], "refused": [], "errors": []}), \
                    patch.object(MODULE, "codex_broker_orphan_check", return_value={"class": "codex_broker_orphan", "min_hours": 0, "action": "report_only", "roots": 0, "before": 0, "killed": 0, "exiting": 0, "after": 0, "skipped_protected": 0, "skipped_young": 0, "found": [], "skipped": [], "terminated": [], "exiting_pids": [], "refused": [], "errors": []}), \
                    patch.object(MODULE, "codex_broker_table", side_effect=make_table), \
                    patch.object(MODULE, "codex_companion_texts", return_value=[]), \
                    patch.object(MODULE, "codex_broker_has_client", return_value=True), \
                    patch.object(MODULE, "_kill_members", side_effect=forbid("_kill_members")), \
                    patch.object(MODULE.os, "kill", side_effect=forbid("os.kill")), \
                    patch.object(MODULE.time, "sleep", side_effect=forbid("sleep")), \
                    patch.object(MODULE, "run", side_effect=forbid("run")), \
                    patch.object(sys, "argv", argv), patch.object(sys, "stdout", new_callable=io.StringIO) as out:
                MODULE.main()
            self.assertIn("CODEX_BROKER_CAP action=report_only", out.getvalue())
            self.assertEqual(json.loads(output.read_text())["codex_broker_cap"]["killed"], 0)


if __name__ == "__main__":
    unittest.main()
