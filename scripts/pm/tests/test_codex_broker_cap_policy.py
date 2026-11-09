"""Rajiv 1794219276.746709 / PM revision: ancestry, not age, owns companions.

Obligation 31735: replace stale strict_kill/idle sampling expectations. Legacy
report-only orphan scanner remains separately covered; cap entrypoint now reaps.
"""
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PATH = Path(__file__).parents[1] / 'shared-assets/claude/scripts/stale-process-cleanup.py'
SPEC = importlib.util.spec_from_file_location('reaper', PATH)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)
UID = os.getuid()


def row(pid, parent, cmd='node /x/codex-companion.mjs review'):
    return dict(pid=pid, ppid=parent, command=cmd, uid=UID, stat='S', start_time=str(pid))


def test_live_pane_and_open_pr_kept():
    table = {10: row(10, 1, '/bin/zsh'), 20: row(20, 10)}
    for linkage in ({}, {20: {'state': 'OPEN', 'number': 123}}):
        assert M.cp_select(table, {10: '0:0.1'}, UID, linkage)[0]['action'] == 'keep'


def test_closed_or_merged_pr_overrides_live_pane():
    table = {10: row(10, 1, '/bin/zsh'), 20: row(20, 10)}
    for state in ('CLOSED', 'MERGED'):
        result = M.cp_select(table, {10: '0:0.1'}, UID, {20: {'state': state}})
        assert (result[0]['action'], result[0]['reason']) == ('reap', 'closed_pr')


def test_orphans_including_unresolvable_linkage_reaped_without_age_gate():
    assert M.cp_select({20: row(20, 1)}, {}, UID, {})[0]['reason'] == 'orphan'


def test_unreadable_or_cyclic_ancestry_skipped_even_for_closed_pr():
    for table in ({20: row(20, 19)}, {20: row(20, 21), 21: row(21, 20, 'tool')}):
        assert all(r['reason'] == 'SKIPPED_UNREADABLE' for r in M.cp_select(table, {}, UID, {20: {'state': 'CLOSED'}}))


def test_noncompanions_and_prompt_mentions_untouched():
    table = {10: row(10, 1, 'claude --prompt "node /x/codex-companion.mjs"'), 11: row(11, 1, 'node /x/server.js')}
    assert M.cp_select(table, {}, UID, {}) == []


def test_children_and_foreign_uid_and_self_protection():
    table = {20: row(20, 1), 21: row(21, 20, 'tool'), 22: {**row(22, 20, 'tool'), 'uid': UID + 1}}
    assert [r['pid'] for r in M.cp_select(table, {}, UID, {})] == [20, 21]
    assert M.cp_select(table, {}, UID, {}, {20})[0]['action'] == 'keep'


def helper(table_fn):
    return SimpleNamespace(codex_broker_table=table_fn, get_cwd=lambda p: '/x', sanitize_command=lambda s: s)


def test_apply_terms_then_kills_and_logs_verified_deaths():
    table = {20: row(20, 1)}
    snapshots = iter([table, table, table, {}])
    with patch.object(M, 'cp_live_panes', return_value={}), patch.object(M, 'cp_linkage', return_value={}), \
         patch.object(M.os, 'kill') as send, patch.object(M.time, 'sleep'):
        out = M.cp_check(helper(lambda: next(snapshots)), True, 0)
    assert [c.args[1] for c in send.call_args_list] == [M.signal.SIGTERM, M.signal.SIGKILL]
    assert out['reaped'] == 1
    assert out['found'][0]['cwd'] == '/x'


def test_pid_reuse_and_fresh_unreadable_never_signalled():
    table = {20: row(20, 1)}
    for fresh in (None, {20: {**row(20, 1), 'start_time': 'reused'}}, {20: row(20, 19)}):
        with patch.object(M, 'cp_live_panes', return_value={}), patch.object(M, 'cp_linkage', return_value={}), \
             patch.object(M.os, 'kill') as send, patch.object(M.time, 'sleep'):
            M.cp_check(helper(iter([table, fresh, fresh, fresh]).__next__), True, 0)
        send.assert_not_called()


def test_scan_never_signals():
    with patch.object(M, 'cp_live_panes', return_value={}), patch.object(M, 'cp_linkage', return_value={}), \
         patch.object(M.os, 'kill') as send:
        assert M.cp_check(helper(lambda: {20: row(20, 1)}))['action'] == 'scan'
    send.assert_not_called()
