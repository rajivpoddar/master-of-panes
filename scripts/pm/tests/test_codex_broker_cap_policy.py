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


def test_live_pane_is_additional_keep_even_with_closed_or_merged_pr():
    table = {10: row(10, 1, '/bin/zsh'), 20: row(20, 10)}
    for state in ('CLOSED', 'MERGED'):
        result = M.cp_select(table, {10: '0:0.1'}, UID, {20: {'state': state}})
        assert (result[0]['action'], result[0]['reason']) == ('keep', 'live_pane')


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
    snapshots = iter([table, table, table, table, table, {}])
    with patch.object(M, 'cp_live_panes', return_value={}), patch.object(M, 'cp_linkage', return_value={'state': 'NONE', 'readable': True, 'cwd': '/x'}), \
         patch.object(M.os, 'kill') as send, patch.object(M.time, 'sleep'):
        out = M.cp_check(helper(lambda: next(snapshots)), True, 0)
    assert [c.args[1] for c in send.call_args_list] == [M.signal.SIGTERM, M.signal.SIGKILL]
    assert out['reaped'] == 1
    assert out['found'][0]['cwd'] == '/x'


def test_pid_reuse_and_fresh_unreadable_never_signalled():
    table = {20: row(20, 1)}
    for fresh in (None, {20: {**row(20, 1), 'start_time': 'reused'}}, {20: row(20, 19)}):
        with patch.object(M, 'cp_live_panes', return_value={}), patch.object(M, 'cp_linkage', return_value={'state': 'NONE', 'readable': True, 'cwd': '/x'}), \
             patch.object(M.os, 'kill') as send, patch.object(M.time, 'sleep'):
            M.cp_check(helper(iter([table, fresh, fresh, fresh]).__next__), True, 0)
        send.assert_not_called()


def test_scan_never_signals():
    with patch.object(M, 'cp_live_panes', return_value={}), patch.object(M, 'cp_linkage', return_value={'state': 'NONE', 'readable': True, 'cwd': '/x'}), \
         patch.object(M.os, 'kill') as send:
        assert M.cp_check(helper(lambda: {20: row(20, 1)}))['action'] == 'scan'
    send.assert_not_called()


def test_recorded_pid1_orphan_open_13326_kept():
    # Sanitized identities from /private/tmp/mop-companion-first-live.json.
    parents = {25874: 1, 25875: 25874, 25876: 25875, 26176: 25876,
               63705: 25876, 66536: 25876, 73938: 25876}
    table = {p: row(p, parent, 'node /x/codex-companion.mjs review' if p == 25874 else 'tool')
             for p, parent in parents.items()}
    rows = M.cp_select(table, {}, UID, {25874: {'state': 'OPEN', 'number': 13326}})
    assert len(rows) == 7
    assert all((r['action'], r['reason']) == ('keep', 'open_pr') for r in rows)


def test_conflicting_ancestor_open_closed_and_unknown_links_keep():
    table = {20: row(20, 1), 21: row(21, 20)}
    for other in ('CLOSED', 'MERGED', 'UNKNOWN'):
        selected = M.cp_select(table, {}, UID, {20: {'state': 'OPEN'}, 21: {'state': other}})
        assert all(r['action'] == 'keep' for r in selected)
    # Affirmative OPEN also survives unreadable ancestry.
    assert M.cp_select({20: row(20, 19)}, {}, UID, {20: {'state': 'OPEN'}})[0]['action'] == 'keep'


def test_multiple_explicit_links_preserve_any_open_despite_closed_or_probe_failure():
    def run(args):
        if args[0] == 'git':
            raise TimeoutError('unreadable ownership')
        if args[3] == '123':
            return SimpleNamespace(returncode=0, stdout='{"number":123,"state":"OPEN"}')
        return SimpleNamespace(returncode=0, stdout='{"number":124,"state":"CLOSED"}')
    with patch.object(M, 'cp_run', side_effect=run):
        link = M.cp_linkage(row(20, 1, 'node /x/codex-companion.mjs --pr 123 --pr 124'), helper(None), {})
    assert link['state'] == 'OPEN'
    assert M.cp_select({20: row(20, 1)}, {}, UID, {20: link})[0]['action'] == 'keep'


def test_identity_change_during_pane_probe_refuses_both_signals():
    original = {20: row(20, 1)}
    current = dict(original)
    calls = 0
    def panes(_):
        nonlocal calls
        calls += 1
        if calls > 1:
            current[20] = {**original[20], 'start_time': 'reused-during-tmux'}
        return {}
    with patch.object(M, 'cp_live_panes', side_effect=panes), \
         patch.object(M, 'cp_linkage', return_value={'state': 'NONE', 'readable': True, 'cwd': '/x'}), \
         patch.object(M.os, 'kill') as send, patch.object(M.time, 'sleep'):
        out = M.cp_check(helper(lambda: dict(current)), True, 0)
    send.assert_not_called()
    assert all(r['reason'] == 'identity_or_ownership_changed' for r in out['refused'])


def test_ownership_change_during_pane_probe_refuses():
    table = {20: row(20, 1)}
    state = {'state': 'NONE', 'readable': True, 'cwd': '/x'}
    calls = 0
    def panes(_):
        nonlocal calls
        calls += 1
        if calls > 1:
            state['state'] = 'OPEN'
        return {}
    with patch.object(M, 'cp_live_panes', side_effect=panes), \
         patch.object(M, 'cp_linkage', side_effect=lambda *a: dict(state)), \
         patch.object(M.os, 'kill') as send, patch.object(M.time, 'sleep'):
        out = M.cp_check(helper(lambda: table), True, 0)
    send.assert_not_called()
    assert all(r['reason'] == 'identity_or_ownership_changed' for r in out['refused'])


def test_unreadable_current_ownership_refuses():
    table = {20: row(20, 1)}
    with patch.object(M, 'cp_live_panes', return_value={}), \
         patch.object(M, 'cp_linkage', return_value={'state': 'UNKNOWN', 'readable': False}), \
         patch.object(M.os, 'kill') as send, patch.object(M.time, 'sleep'):
        out = M.cp_check(helper(lambda: table), True, 0)
    send.assert_not_called()
    assert all(r['reason'] == 'SKIPPED_UNREADABLE' for r in out['refused'])


def test_pr_probe_identity_change_checked_after_lookup_and_before_signal():
    table = {20: row(20, 1)}
    current = dict(table)
    calls = 0
    def linkage(*_):
        nonlocal calls
        calls += 1
        if calls > 1:
            current[20] = {**table[20], 'command': 'replacement'}
        return {'state': 'NONE', 'readable': True, 'cwd': '/x'}
    with patch.object(M, 'cp_live_panes', return_value={}), patch.object(M, 'cp_linkage', side_effect=linkage), \
         patch.object(M.os, 'kill') as send, patch.object(M.time, 'sleep'):
        M.cp_check(helper(lambda: dict(current)), True, 0)
    send.assert_not_called()


def test_each_individual_signal_rechecks_original_tree():
    table = {20: row(20, 1), 21: row(21, 20, 'tool')}
    current = dict(table)
    def sent(pid, sig):
        current[20] = {**table[20], 'command': 'new-owner'}
    with patch.object(M, 'cp_live_panes', return_value={}), \
         patch.object(M, 'cp_linkage', return_value={'state': 'NONE', 'readable': True, 'cwd': '/x'}), \
         patch.object(M.os, 'kill', side_effect=sent) as send, patch.object(M.time, 'sleep'):
        out = M.cp_check(helper(lambda: dict(current)), True, 0)
    assert [(c.args[0], c.args[1]) for c in send.call_args_list] == [(21, M.signal.SIGTERM)]
    assert len(out['refused']) == 3


def test_changed_ancestor_identity_with_same_pid_chain_refuses():
    table = {10: row(10, 1, 'shell'), 20: row(20, 10)}
    changed = {**table, 10: {**table[10], 'start_time': 'replaced-ancestor'}}
    snapshots = iter([table, changed, changed, changed])
    with patch.object(M, 'cp_live_panes', return_value={}), \
         patch.object(M, 'cp_linkage', return_value={'state': 'NONE', 'readable': True, 'cwd': '/x'}), \
         patch.object(M.os, 'kill') as send, patch.object(M.time, 'sleep'):
        out = M.cp_check(helper(lambda: next(snapshots)), True, 0)
    send.assert_not_called()
    assert all(r['reason'] == 'identity_or_ownership_changed' for r in out['refused'])
