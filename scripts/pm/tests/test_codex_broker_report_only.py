"""Fixture proof: the Codex broker class is REPORT-ONLY on every --apply entrypoint."""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[3]
MODULE_PATH = ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "scripts" / "stale-process-cleanup.py"
SPEC = importlib.util.spec_from_file_location("stale_process_cleanup_report_only", MODULE_PATH)
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


# A >6h detached broker whose client disconnected but whose app-server child still runs a local
# tool on stdio/files: no socket client, no network FDs. Age + PPID 1 + ancestry all say "orphan".
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
        raise AssertionError(f"report-only broker class must not call {name}{args!r}")
    return _raise


class CodexBrokerReportOnlyTests(unittest.TestCase):
    def test_candidate_is_reported_but_never_signalled_with_or_without_apply(self):
        for apply in (True, False):
            with patch.object(MODULE, "codex_broker_table", side_effect=table), \
                    patch.object(MODULE.os, "kill", side_effect=forbid("os.kill")), \
                    patch.object(MODULE, "run", side_effect=forbid("run")):
                out = MODULE.codex_broker_orphan_check(apply, 6.0, 3.0)
            self.assertEqual(out["action"], "report_only")
            self.assertEqual([row["pid"] for row in out["found"]], [100])
            self.assertEqual(out["found"][0]["member_pids"], [100, 101, 102])
            self.assertEqual((out["killed"], out["terminated"], out["exiting_pids"]), (0, [], []))

    def test_unreadable_or_incomplete_process_evidence_is_kept(self):
        with patch.object(MODULE, "codex_broker_table", return_value=None), \
                patch.object(MODULE.os, "kill", side_effect=forbid("os.kill")):
            out = MODULE.codex_broker_orphan_check(True, 6.0, 3.0)
        self.assertEqual((out["roots"], out["found"], out["errors"]), (0, [], ["ps_failed"]))
        for malformed in ({}, {100: proc(100, 1, BROKER)}):
            with patch.object(MODULE, "codex_broker_table", return_value=malformed), \
                    patch.object(MODULE.os, "kill", side_effect=forbid("os.kill")):
                MODULE.codex_broker_orphan_check(True, 6.0, 3.0)

    def test_main_apply_entrypoint_signals_nothing_for_brokers(self):
        # Every --apply entrypoint (launchd flags, heartbeat flags, manual) funnels through main().
        for extra in ([], ["--codex-broker-min-hours", "0"], ["--codex-broker-min-hours", "1000000"]):
            with tempfile.TemporaryDirectory() as temp:
                argv = ["x", "--apply", "--attest-slotless-reap", "--output", str(Path(temp) / "p.json"),
                        "--status-output", str(Path(temp) / "s.json"), *extra]
                with patch.object(MODULE, "load_slot_inventory", return_value=({}, "malformed_row:slot=4")), \
                        patch.object(MODULE, "load_history", side_effect=lambda *a, **k: {}), \
                        patch.object(MODULE, "save_history"), \
                        patch.object(MODULE, "runaway_scan", return_value={"class": "runaway_scan", "found": [], "terminated": [], "errors": []}), \
                        patch.object(MODULE, "replayd_check", return_value={"pid": None, "cpu": None, "streak": 0, "action": "none"}), \
                        patch.object(MODULE, "codex_renderer_check", return_value={"class": "codex_renderer", "observed": 0, "over_threshold": 0, "pending": 0, "found": [], "terminated": [], "refused": [], "errors": []}), \
                        patch.object(MODULE, "codex_broker_table", side_effect=table), \
                        patch.object(MODULE.os, "kill", side_effect=forbid("os.kill")), \
                        patch.object(MODULE, "run", side_effect=forbid("run")), \
                        patch.object(sys, "argv", argv), patch.object(sys, "stdout", new_callable=io.StringIO) as out:
                    MODULE.main()
            self.assertIn("CODEX_BROKER_ORPHAN", out.getvalue())
            self.assertIn("action=report_only", out.getvalue())

    def test_no_signalling_code_remains_in_the_broker_section(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        start = source.index("def codex_broker_table(")
        end = source.index("def run_self_test(")
        section = source[start:end]
        for token in ("os.kill", "signal.", "SIGKILL", "SIGTERM", "time.sleep"):
            self.assertNotIn(token, section, token)


if __name__ == "__main__":
    unittest.main()
