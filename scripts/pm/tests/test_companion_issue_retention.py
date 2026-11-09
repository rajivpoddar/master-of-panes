"""OPEN issue/PR retention and required-ancestor containment (1794220457.409539)."""
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PATH = Path(__file__).parents[1] / 'shared-assets/claude/scripts/stale-process-cleanup.py'
SPEC = importlib.util.spec_from_file_location('issue_retention', PATH)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)
UID = os.getuid()


def row(pid=20, parent=1, command='node /x/codex-companion.mjs review'):
    return dict(pid=pid, ppid=parent, command=command, uid=UID, stat='S', start_time=str(pid))


def linkage(command, issue='OPEN', pr='CLOSED', branch='fix/123-work'):
    def run(args):
        if args[0] == 'git':
            return SimpleNamespace(returncode=0, stdout=branch)
        if args[1] == 'issue':
            if issue is None:
                return SimpleNamespace(returncode=1, stdout='rate limited')
            return SimpleNamespace(returncode=0, stdout='{"number":123,"state":"' + issue + '"}')
        return SimpleNamespace(returncode=0, stdout='[{"number":456,"state":"' + pr + '"}]')
    with patch.object(M, 'cp_run', side_effect=run):
        return M.cp_linkage(row(command=command), SimpleNamespace(get_cwd=lambda _: '/x'), {})


def test_orphan_open_issue_only_and_all_linkage_sources():
    for evidence in ('--branch fix/123-work', '--branch feat/123-work', '--branch issue-123-work',
                     '--plan-file docs/plans/issue-123-work.md', '--issue 123',
                     '--marker-file /tmp/codex-app-plan-review-123.txt', '--cwd /tmp/issue-123-work'):
        link = linkage('node /x/codex-companion.mjs ' + evidence, pr='CLOSED', branch='work')
        selected = M.cp_select({20: row()}, {}, UID, {20: link})
        assert selected[0]['action'] == 'keep'
        assert selected[0]['reason'] == 'open_issue'


def test_open_issue_closed_pr_and_reverse_and_all_closed():
    for issue, pr, action in (('OPEN', 'CLOSED', 'keep'), ('CLOSED', 'OPEN', 'keep'),
                              ('CLOSED', 'MERGED', 'reap')):
        link = linkage('node /x/codex-companion.mjs review', issue, pr)
        assert M.cp_select({20: row()}, {}, UID, {20: link})[0]['action'] == action


def test_unreadable_github_refuses_but_affirmative_open_wins():
    for pr, action in (('CLOSED', 'skip'), ('OPEN', 'keep')):
        link = linkage('node /x/codex-companion.mjs review', None, pr)
        assert M.cp_select({20: row()}, {}, UID, {20: link})[0]['action'] == action


def test_required_ancestors_retained_for_open_or_unknown_child_and_reverse():
    table = {20: row(command='node /x/app-server-broker.mjs'),
             21: row(21, 20, 'backend'), 22: row(22, 21)}
    for child_state, expected in (('OPEN', 'keep'), ('UNKNOWN', 'skip')):
        states = {20: {'state': 'CLOSED', 'readable': True},
                  22: {'state': child_state, 'kind': 'issue', 'readable': child_state == 'OPEN'}}
        selected = M.cp_select(table, {}, UID, states)
        assert all(r['action'] == expected for r in selected)
        helper = SimpleNamespace(codex_broker_table=lambda: table, get_cwd=lambda _: '/x', sanitize_command=lambda s: s)
        with patch.object(M, 'cp_live_panes', return_value={}), \
             patch.object(M, 'cp_linkage', side_effect=lambda r, *_: states[r['pid']]), \
             patch.object(M.os, 'kill') as send:
            M.cp_check(helper, apply=True)
        send.assert_not_called()
    reverse = {20: {'state': 'OPEN', 'kind': 'issue'}, 22: {'state': 'CLOSED', 'readable': True}}
    assert all(r['action'] == 'keep' for r in M.cp_select(table, {}, UID, reverse))
