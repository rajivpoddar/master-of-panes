import importlib.util
import json
import sqlite3
import pytest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

SOURCE = Path(__file__).resolve().parents[1] / 'shared-assets/claude/scripts/pm/heartbeat/heartbeat-compose.py'
spec = importlib.util.spec_from_file_location('heartbeat_pending_pm', SOURCE)
composer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(composer)
NOW = datetime(2026, 11, 4, 5, tzinfo=timezone.utc)


@pytest.mark.parametrize('state', ['consumed/uncertain', 'UNKNOWN_CONSUMED_NO_RETRY', 'UNKNOWN', 'unknown/consumed'])
def test_ledger_selection_suppression_backlog_and_read_only(tmp_path, state):
    db = tmp_path / 'ops.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE obligations (id integer, status text, owner text, kind text, created_at text, suppress_until text, issue integer, pr integer, required_action text, title text, blocker text, next_review_at text, external_state text)')
        conn.executemany('INSERT INTO obligations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', [
            (1, 'open', 'pm', 'work', '2026-11-04T01:00:00+00:00', None, 123, None, 'do it', None, None, None, None),
            (2, 'open', '', 'backlog', '2026-11-04T02:00:00+00:00', None, None, None, None, None, None, None, None),
            (3, 'open', 'PM', 'work', '2026-11-04T03:00:00+00:00', '2026-11-05T00:00:00+00:00', None, None, 'hidden', None, None, None, None),
            (4, 'open', 'cto', 'work', '2026-11-04T03:00:00+00:00', None, None, None, None, None, None, None, None),
            (5, 'resolved', 'pm', 'work', '2026-11-04T03:00:00+00:00', None, None, None, None, None, None, None, None),
            (6, 'open', '', 'work', '2026-11-04T00:00:00+00:00', None, None, None, 'file issue', None, 'awaiting Rajiv decision', None, None),
            (7, 'open', 'PM', 'work', '2026-11-04T02:00:00+00:00', None, None, None, 'follow up', None, None, '2026-11-05T00:00:00Z', None),
            (8, 'open', 'pm', 'work', '2026-11-04T02:30:00+00:00', None, None, None, 'resend', None, None, None, state),
        ])
    before = db.read_bytes()
    result = composer.pending_obligations(db, NOW)
    assert [r['id'] for r in result['items']] == [6, 1, 7, 8]
    assert result['backlog'] == 1
    block = composer.pending_actions_block(result, {'items': []}, NOW)
    assert block[1].startswith('• #123 | do it')
    assert any('HELD | hold: awaiting Rajiv decision' in line and 'PM triage (unowned)' in line for line in block)
    assert any('HELD | review: 2026-11-05T00:00:00Z' in line and line.endswith('| PM') for line in block)
    assert any(f'HELD | state: {state}' in line for line in block)
    assert 'resend' not in '\n'.join(block)
    assert len(block) == 5
    assert db.read_bytes() == before
    assert 'UNAVAILABLE' in '\n'.join(composer.pending_actions_block(composer.pending_obligations(tmp_path / 'missing', NOW), {'items': []}, NOW))


def test_github_checks_merged_dependency_once_and_reports_failures():
    calls = []
    def run(args, **kwargs):
        assert '--cache' in args
        endpoint = args[-1]
        calls.append(endpoint)
        if '/issues?' in endpoint:
            data = [{'number': 12530, 'body': 'next_action: when PR #12535 merges PM runs workspace creation'},
                    {'number': 12531, 'body': 'next_action: after #12535 merges PM verifies'},
                    {'number': 12532, 'body': 'next_action: when PR #22 merges CTO verifies'}]
        else:
            data = {'state': 'closed', 'pull_request': {'merged_at': '2026-11-04T04:00:00Z'}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(data))
    got = composer.pending_github(run)
    assert len(got['items']) == 2
    assert sum('/issues/12535' in call for call in calls) == 1
    block = '\n'.join(composer.pending_actions_block({'items': [], 'backlog': 0}, got, NOW))
    assert '#12530' in block and 'precondition #12535 merged 1h 0m ago' in block
    def failed(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout='')
    assert 'UNAVAILABLE' in '\n'.join(composer.pending_actions_block({'items': [], 'backlog': 0}, composer.pending_github(failed), NOW))


def test_section_cap_and_mode_placement():
    rows = [dict(id=i, kind='work', created_at='2026-11-04T01:00:00Z', owner='PM', required_action='act') for i in range(10)]
    block = composer.pending_actions_block({'items': rows, 'backlog': 2}, {'items': []}, NOW)
    assert len(block) == 10 and block[-1] == '• +2 more'
    assert 'backlog rows: 2 (not shown)' in block[0]
    report = composer.compose('1h', NOW, None, {}, [], decisions={'items': [], 'stale': 0}, training=('training', []), pending_actions=({'items': rows, 'backlog': 2}, {'items': []}))
    assert report.index('*Decisions pending on Rajiv:*') < report.index('*Pending PM actions:*')
    report3 = composer.compose('3h', NOW, None, {}, [], decisions={'items': [], 'stale': 0})
    assert '*Pending PM actions:*' not in report3


def test_inventory_rows_are_counted_not_listed_as_pending_actions(tmp_path):
    db = tmp_path / 'inv.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE obligations (id integer, status text, owner text, kind text, created_at text, suppress_until text, issue integer, pr integer, required_action text, title text, blocker text, next_review_at text, external_state text)')
        conn.executemany('INSERT INTO obligations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', [
            (1, 'open', 'pm', 'work', '2026-11-04T01:00:00+00:00', None, 123, None, 'do it', None, None, None, None),
            (2, 'open', 'pm', 'ready_pool', '2026-11-04T01:00:00+00:00', None, 200, None, 'assign', None, None, None, None),
            (3, 'open', '', 'ready_pool', '2026-11-04T01:00:00+00:00', None, 201, None, 'assign', None, None, None, None),
            (4, 'open', 'CTO / PM', 'triage_retained_scope', '2026-11-04T01:00:00+00:00', None, 300, None, 'triage', None, None, None, None),
            (5, 'resolved', 'pm', 'ready_pool', '2026-11-04T01:00:00+00:00', None, 202, None, 'assign', None, None, None, None),
        ])
    result = composer.pending_obligations(db, NOW)
    assert [r['id'] for r in result['items']] == [1]
    assert result['inventory'] == {'ready_pool': 2, 'triage_retained_scope': 1}
    block = composer.pending_actions_block(result, {'items': []}, NOW)
    assert block[-1] == '• Inventory: 2 ready-pool, 1 triage (not actions)'
    assert not any('#200' in line or '#300' in line for line in block)
