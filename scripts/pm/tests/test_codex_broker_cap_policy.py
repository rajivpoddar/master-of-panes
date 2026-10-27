"""Strict broker cleanup preserves live clients, requests and companions."""

from __future__ import annotations

import importlib.util
import copy
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
    def test_pure_policy_selftest_has_no_real_signal(self):
        with patch.object(MODULE.os, 'kill', side_effect=forbid('real signal')):
            self.assertEqual(MODULE.run_self_test(),0)
    def test_each_term_and_kill_rechecks_full_group_and_activity(self):
        table = {100: proc(100, 1, broker('safe','/a')),
                 101: proc(101, 100, 'node /opt/homebrew/bin/codex app-server')}
        root = MODULE.codex_broker_policy_plan(table, UID, -1, [], lambda *a: False, 0, 0, 0)['roots'][0]
        for at in ('before_term', 'between_terms', 'before_kill'):
            for drift in ('reparent', 'protected', 'new_foreign', 'tool', 'client', 'inflight', 'cwd', 'unknown'):
                with self.subTest(at=at, drift=drift):
                    state = copy.deepcopy(table)
                    sent, stepped = [], [False]
                    def change():
                        stepped[0] = True
                        if drift == 'reparent': state[101]['ppid'] = 1
                        if drift == 'protected': state[101]['command'] = 'claude active-work'
                        if drift in ('new_foreign','tool'):
                            state[102] = proc(102,100,'long-running-tool')
                            if drift == 'new_foreign': state[102]['uid'] = UID + 1
                    if at == 'before_term': change()
                    def send(pid, sig):
                        sent.append((pid,sig))
                        if at == 'between_terms' and len(sent) == 1: change()
                    result = MODULE.codex_broker_strict_kill(root, 0,
                        table_fn=lambda: None if stepped[0] and drift == 'unknown' else state,
                        has_client=lambda *a: stepped[0] and drift == 'client',
                        companion_fn=lambda t: [broker('safe','/a')] if stepped[0] and drift == 'inflight' else [],
                        companion_cwd_fn=lambda t: ['/a'] if stepped[0] and drift == 'cwd' else [],
                        send=send, sleep=lambda _: change() if at == 'before_kill' else None)
                    self.assertTrue(result['outcome'].startswith('refused_'), result)
                    self.assertEqual(len(sent), {'before_term':0,'between_terms':1,'before_kill':2}[at])
                    self.assertEqual(result['sigkill'], [])
                    self.assertEqual(result['signalled'], len(sent))

    def test_safe_known_group_control_and_unknown_post_read(self):
        for unknown in (False, True):
            table = {100: proc(100,1,broker('safe','/a'))}
            root = MODULE.codex_broker_policy_plan(table,UID,-1,[],lambda *a:False,0,0,0)['roots'][0]
            calls = [0]
            sent = []
            def scan():
                calls[0] += 1
                return (None if unknown else {}) if calls[0] == 3 else table
            out = MODULE.codex_broker_strict_kill(root,0,table_fn=scan,has_client=lambda *a:False,
                companion_fn=lambda t:[],companion_cwd_fn=lambda t:[],send=lambda *a:sent.append(a),sleep=lambda _:None)
            self.assertEqual(out['outcome'], 'refused_post_read_unknown' if unknown else 'terminated')
            self.assertEqual(len(sent),2)

    def check(self, apply, companions, has_client=True):
        with patch.object(MODULE, "codex_broker_table", side_effect=make_table), \
                patch.object(MODULE, "codex_companion_texts", return_value=companions), \
                patch.object(MODULE, "codex_broker_has_client", return_value=has_client), \
                patch.object(MODULE.os, "kill", side_effect=forbid("os.kill")), \
                patch.object(MODULE, "_kill_members", side_effect=forbid("_kill_members")), \
                patch.object(MODULE.time, "sleep", side_effect=forbid("sleep")), \
                patch.object(MODULE, "run", side_effect=forbid("run")):
            return MODULE.codex_broker_policy_check(apply, 0, 0, 0, 0)

    def test_strict_guards_independently_refuse(self):
        for guard in ("cwd", "inflight", "client", "young"):
            with self.subTest(guard=guard):
                table = {100: proc(100, 1, broker("orphan", "/a"), 119 if guard == "young" else 120)}
                with patch.object(MODULE, "codex_broker_table", return_value=table), \
                     patch.object(MODULE, "codex_companion_texts", return_value=[broker("orphan", "/a")] if guard == "inflight" else []), \
                     patch.object(MODULE, "codex_companion_cwds", return_value=["/a"] if guard == "cwd" else []), \
                     patch.object(MODULE, "codex_broker_has_client", return_value=guard == "client"), \
                     patch.object(MODULE, "codex_broker_strict_kill", side_effect=forbid("signal")):
                    out = MODULE.codex_broker_policy_check(True, 0, 0, 0, 0)
                self.assertEqual(out["action"], "strict_kill")
                self.assertEqual(out["would_kill"], [])
                self.assertEqual(out["killed"], 0)

    def test_strict_rechecks_before_signalling(self):
        table = {100: proc(100, 1, broker("orphan", "/a"))}
        root = MODULE.codex_broker_policy_plan(table, UID, -1, [], lambda *a: False, 0, 0, 0)["roots"][0]
        for guard in ("identity", "inflight", "cwd", "client"):
            with self.subTest(guard=guard):
                result = MODULE.codex_broker_strict_kill(
                    root, 0, table_fn=lambda: {} if guard == "identity" else table,
                    has_client=lambda *a: guard == "client",
                    companion_fn=lambda t: [broker("orphan", "/a")] if guard == "inflight" else [],
                    companion_cwd_fn=lambda t: ["/a"] if guard == "cwd" else [],
                    send=forbid("signal"), sleep=forbid("sleep"))
                self.assertTrue(result["outcome"].startswith("refused_"))

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
                    patch.object(MODULE, "codex_broker_table", side_effect=lambda: {p:r for p,r in make_table().items() if p in (100,101,102)}), \
                    patch.object(MODULE, "codex_companion_texts", return_value=[]), \
                    patch.object(MODULE, "codex_broker_has_client", return_value=False), \
                    patch.object(MODULE, "_kill_members", side_effect=forbid("_kill_members")), \
                    patch.object(MODULE.os, "kill", side_effect=forbid("os.kill")), \
                    patch.object(MODULE.time, "sleep", side_effect=forbid("sleep")), \
                    patch.object(MODULE, "run", side_effect=forbid("run")), \
                    patch.object(sys, "argv", argv), patch.object(sys, "stdout", new_callable=io.StringIO) as out:
                MODULE.main()
            self.assertIn("CODEX_BROKER_CAP action=strict_kill", out.getvalue())
            self.assertEqual(json.loads(output.read_text())["codex_broker_cap"]["killed"], 0)


if __name__ == "__main__":
    unittest.main()


def test_eligible_broker_still_requires_fresh_signal_time_evidence():
    """Retains e148's eligible-root control; unknown fresh evidence cannot lift containment."""
    table = {100: proc(100, 1, broker("orphan", "/a"))}
    with patch.object(MODULE, "codex_broker_table", side_effect=[table, None]), \
            patch.object(MODULE, "codex_companion_texts", return_value=[]), \
            patch.object(MODULE, "codex_companion_cwds", return_value=[]), \
            patch.object(MODULE, "codex_broker_has_client", return_value=False), \
            patch.object(MODULE.os, "kill", side_effect=forbid("signal without fresh evidence")):
        result = MODULE.codex_broker_policy_check(True, 0, 0, 0, 0)
    assert result['action'] == 'strict_kill' and result['killed'] == 0
    assert result['would_kill']
    assert result['refused'][0]['outcome'] == 'refused_ps_failed'
