"""Codex broker cap policy (Rajiv 2026-10-22): --apply signals; ChatGPT exemption and in-flight guard hold."""

from __future__ import annotations

import importlib.util
import os
import signal
import unittest
from pathlib import Path
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


def make_table() -> dict:
    rows = [
        proc(1, 0, "/sbin/launchd", 9999),
        proc(60, 1, "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT", 900),
        proc(100, 1, broker("orphan", "/a")),             # (a) killable
        proc(110, 1, broker("busy", "/b")),               # (a) but in-flight companion review
        proc(120, 60, broker("chatgpt", "/c"), 900),      # ChatGPT-owned: exempt
    ]
    return {r["pid"]: r for r in rows}


class BrokerCapPolicyTests(unittest.TestCase):
    def run_check(self, apply: bool, companions: list[str]):
        killed: list[tuple[int, int]] = []
        with patch.object(MODULE, "codex_broker_table", side_effect=make_table), \
                patch.object(MODULE, "codex_companion_texts", return_value=companions), \
                patch.object(MODULE, "codex_broker_has_client", return_value=True), \
                patch.object(MODULE.os, "kill", side_effect=lambda pid, sig: killed.append((pid, sig))), \
                patch.object(MODULE, "alive", return_value=False), patch.object(MODULE.time, "sleep"):
            out = MODULE.codex_broker_policy_check(apply, 0, 20, 10, 30)
        return out, killed

    def test_apply_kills_orphan_but_spares_chatgpt_and_inflight(self):
        out, killed = self.run_check(True, ["node codex-review-companion.mjs --endpoint unix:/var/folders/x/cxc-busy/broker.sock"])
        self.assertEqual({pid for pid, sig in killed}, {100})
        self.assertEqual(killed[0], (100, signal.SIGTERM))  # TERM first; KILL only if the same identity survives
        self.assertEqual(out["killed"], 1)
        self.assertIn(110, [s["pid"] for s in out["skipped"]])

    def test_scan_only_never_signals(self):
        out, killed = self.run_check(False, [])
        self.assertEqual(killed, [])
        self.assertEqual(out["roots"], 2)  # 100 and 110 eligible; 120 ChatGPT-owned excluded

    def test_unknown_companion_state_fails_closed(self):
        out, killed = self.run_check(True, None)
        self.assertEqual(killed, [])
        self.assertIn("companion_scan_failed", out["errors"])


if __name__ == "__main__":
    unittest.main()
