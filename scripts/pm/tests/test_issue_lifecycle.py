"""Fixture proof of the production lifecycle detector, without GitHub mutations."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'shared-assets/claude/scripts/pm/heartbeat/issue-lifecycle.py'


def load():
    spec = importlib.util.spec_from_file_location('issue_lifecycle', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def issue(n, *labels, body='', timeline=None):
    return dict(number=n, title=f'Fixture {n}', state='OPEN', labels=[{'name': x} for x in labels],
                body=body, references=timeline or [])


def test_all_classes_and_issue_dedupe():
    m = load()
    rows = [issue(1, 'status:in-review', 'slot:2'), issue(2, 'status:done'),
            issue(3, 'status:todo', timeline=[dict(number=30, state='MERGED', body='Closes #3')]),
            issue(4, 'pm-blocked:dependency', body='Blocked by #40 and #41'),
            issue(5, 'status:in-progress')]
    findings = m.detect(rows, [], [], [dict(number=50, body='Part of #5\nRefs #5')],
                        lambda n: n in (40, 41))
    assert {r['issue']: r['reasons'] for r in findings} == {
        1: ['unowned_lifecycle'], 2: ['done_open'], 3: ['merged_closes_open'],
        4: ['blockers_resolved'], 5: ['partial_merge', 'unowned_lifecycle']}
    assert findings[-1]['prs'] == [50]
    rendered = '\n'.join(m.render(findings))
    assert rendered.count('*Unblockable now / issue lifecycle:*') == 1
    assert '5 issues' in rendered and 'unowned_lifecycle=2' in rendered


def test_ownership_uses_live_mop_not_slot_label_and_all_pr_reference_paths():
    m = load()
    rows = [issue(n, 'status:in-progress') for n in range(1, 9)]
    rows[4]['references'] = [dict(number=90, state='OPEN')]
    prs = [dict(number=10, body='Fix #1', title='', head={'ref': 'x'}),
           dict(number=11, body='', title='Fix #2', head={'ref': 'x'}),
           dict(number=12, body='', title='', head={'ref': 'fix/3-thing'}),
           dict(number=13, closingIssuesReferences={'nodes': [{'number': 4}]})]
    slots = [dict(slot=2, occupied=True, issue=6), dict(slot=3, occupied=False, issue=7),
             dict(slot=4, occupied=True, issue=8, pr=80)]
    assert [r['issue'] for r in m.detect(rows, prs, slots, [], lambda n: False)] == [7]
    with pytest.raises(ValueError, match='MoP'):
        m.detect(rows, prs, None, [], lambda n: False)


def test_blocker_boundaries_and_unknowns():
    m = load()
    rows = [issue(1, 'status:blocked', body='Blockers: #10, #11\nAC: #99'),
            issue(2, 'status:blocked', body='Depends on #10 and #12'),
            issue(3, 'pm-blocked:product', body='Need approval; see #10'),
            issue(4, 'status:blocked', body='Blockers: #4'),
            issue(5, 'status:blocked', body='After #10\nWake: Rajiv approval'),
            issue(6, 'status:todo', body='Blockers: #10'),
            issue(7, 'status:backlog', body='**Blockers**: #10')]
    # External wakes remain for PM's park/unblock disposition, not auto-mutation.
    assert [r['issue'] for r in m.detect(rows, [], [], [], lambda n: n in (10, 11))] == [1, 5, 7]


def test_partial_links_are_explicit_and_open_only():
    m = load()
    rows = [issue(1), issue(2)]
    merged = [dict(number=10, body='Part of #1, #2\nRefs #1\nCloses #3'),
              dict(number=11, body='Mention #2; not a partial link')]
    assert [(r['issue'], r['prs']) for r in m.detect(rows, [], [], merged, lambda n: False)] == [(1, [10]), (2, [10])]


def test_sync_one_obligation_per_issue_and_failure_is_not_silent():
    m = load()
    findings = m.detect([issue(1, 'status:in-review', 'status:done')], [], [], [], lambda n: False)
    with patch.object(m.subprocess, 'run') as run:
        m.sync(findings)
        assert run.call_count == 1
        argv = run.call_args.args[0]
        assert argv[argv.index('--kind') + 1] == 'orphaned_issue'
        assert argv[argv.index('--target-id') + 1] == '1'
        assert argv[argv.index('--issue') + 1] == '1'
        assert json.loads(argv[argv.index('--evidence-json') + 1])['reasons'] == ['done_open', 'unowned_lifecycle']
        assert run.call_args.kwargs['check'] is True


def test_collection_paginates_timeline_and_refuses_search_truncation():
    m = load()
    page = dict(nodes=[dict(source=dict(number=10, state='OPEN'))],
                pageInfo=dict(hasNextPage=True, endCursor='next'))
    with patch.object(m, 'graphql', return_value={'repository': {'issue': {'timelineItems': {
            'nodes': [dict(source=dict(number=11, state='MERGED'))],
            'pageInfo': dict(hasNextPage=False)}}}}) as query:
        assert [r['number'] for r in m.timeline(1, page)] == [10, 11]
        assert query.call_args.args[1]['cursor'] == 'next'


def test_failed_sync_does_not_advance_merge_cursor(tmp_path):
    m = load()
    state = tmp_path / 'state.json'
    state.write_text('{"since":"2026-10-26T00:00:00Z"}')
    with patch.object(m, 'STATE', state), patch.object(m, 'collect', return_value=([], [], [], [], lambda n: False)), \
         patch.object(m, 'sync', side_effect=RuntimeError('ledger unavailable')):
        with pytest.raises(RuntimeError):
            m.run()
    assert json.loads(state.read_text())['since'] == '2026-10-26T00:00:00Z'


def test_blocker_states_batches_reads_and_keeps_invalid_or_missing_unknown():
    m = load()
    response = {'data': {'repository': {
        'b10': {'__typename': 'Issue', 'state': 'CLOSED'},
        'b11': {'__typename': 'PullRequest', 'merged': False}, 'b12': None}},
        'errors': [{'type': 'NOT_FOUND', 'path': ['repository', 'b12']}]}
    with patch.object(m, 'gh', return_value=response) as query:
        assert m.blocker_states({10, 11, 12, 39189729524}) == {10: True, 11: False, 12: None, 39189729524: None}
        assert query.call_count == 1
        assert '39189729524' not in query.call_args.args[-1]
    with patch.object(m, 'gh', side_effect=m.subprocess.CalledProcessError(1, ['gh'], output=json.dumps(response))):
        assert m.blocker_states({10, 11, 12})[10] is True
    with patch.object(m, 'gh', return_value={'errors': [{'type': 'RATE_LIMITED'}]}):
        with pytest.raises(RuntimeError):
            m.blocker_states({10})
