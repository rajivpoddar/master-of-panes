"""Fixture proof of the production lifecycle detector, without GitHub mutations."""
import importlib.util
import datetime as dt
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
    rows = [issue(1, 'pm-blocked:dependency', body='Blockers: #10, #11\nAC: #99'),
            issue(2, 'pm-blocked:dependency', body='Depends on #10 and #12'),
            issue(3, 'pm-blocked:product', body='Need approval; see #10'),
            issue(4, 'pm-blocked:dependency', body='Blockers: #4'),
            issue(5, 'pm-blocked:dependency', body='After #10\nWake: Rajiv approval'),
            issue(6, 'status:todo', body='Blockers: #10'),
            issue(7, 'status:backlog', 'pm-blocked:dependency', body='**Blockers**: #10')]
    # External wakes remain for PM's park/unblock disposition, not auto-mutation.
    assert [r['issue'] for r in m.detect(rows, [], [], [], lambda n: n in (10, 11))] == [1, 5, 7]


def test_partial_links_are_explicit_and_open_only():
    m = load()
    rows = [issue(1), issue(2)]
    merged = [dict(number=10, body='Part of #1, #2\nRefs #1\nCloses #3'),
              dict(number=11, body='Mention #2; not a partial link')]
    assert [(r['issue'], r['prs']) for r in m.detect(rows, [], [], merged, lambda n: False)] == [(1, [10]), (2, [10])]


def test_run_never_upserts_orphaned_issue_rows(tmp_path):
    """Routine issue findings stay in the report/STATE (CTO C0ALZJHGE49 ts 1793883179.092259)."""
    m = load()
    assert not hasattr(m, 'sync')
    findings_rows = [issue(1, 'status:in-review', 'status:done')]
    state = tmp_path / 'state.json'
    with patch.object(m, 'STATE', state), \
         patch.object(m, 'collect', return_value=(findings_rows, [], [], [], lambda n: False)), \
         patch.object(m, 'active_suppressions', return_value=set()), \
         patch.object(m, 'recheck', return_value=dict(cursor=0)), \
         patch.object(m.subprocess, 'run') as run:
        doc = m.run()
    assert run.call_count == 0
    assert [r['issue'] for r in doc['findings']] == [1]
    assert doc['findings'][0]['reasons'] == ['done_open', 'unowned_lifecycle']
    assert any('#1: done_open, unowned_lifecycle' in line for line in doc['lines'])


def test_collection_paginates_timeline_and_refuses_search_truncation():
    m = load()
    page = dict(nodes=[dict(source=dict(number=10, state='OPEN'))],
                pageInfo=dict(hasNextPage=True, endCursor='next'))
    with patch.object(m, 'graphql', return_value={'repository': {'issue': {'timelineItems': {
            'nodes': [dict(source=dict(number=11, state='MERGED'))],
            'pageInfo': dict(hasNextPage=False)}}}}) as query:
        assert [r['number'] for r in m.timeline(1, page)] == [10, 11]
        assert query.call_args.args[1]['cursor'] == 'next'


def test_failed_recheck_does_not_advance_merge_cursor(tmp_path):
    m = load()
    state = tmp_path / 'state.json'
    state.write_text('{"since":"2026-10-26T00:00:00Z"}')
    # Hermetic: never read the live PM ledger (it failed in full-suite runs with
    # "unable to open database file" on origin/main 3352a18 as well).
    with patch.object(m, 'STATE', state), patch.object(m, 'collect', return_value=([], [], [], [], lambda n: False)), \
         patch.object(m, 'active_suppressions', return_value=set()), \
         patch.object(m, 'recheck', side_effect=RuntimeError('ledger unavailable')):
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


# --- live recheck: resolve only proven-cleared, unclaimed rows ----------------

def _row(rid, n, reasons, owner=None, until=None):
    return dict(id=rid, issue=n, owner=owner, suppress_until=until, reasons=reasons)


def _live(state='OPEN', labs=(), assignees=()):
    return dict(state=state, labels=set(labs), assignees=list(assignees))


def _recheck(m, rows, live, findings=(), slots=(), open_prs=(), previous=(), outcome='resolved', **kw):
    calls = []

    def fetch(n):
        value = live[n]
        if isinstance(value, Exception):
            raise value
        return value

    def write(rid, reason, *fingerprint):
        calls.append((rid, reason))
        return outcome
    with patch.object(m, 'open_rows', return_value=rows), patch.object(m, 'live_issue', side_effect=fetch) as gh, \
         patch.object(m, 'resolve_if_unclaimed', side_effect=write):
        report = m.recheck(list(findings), list(slots), list(open_prs), list(previous), **kw)
    return report, calls, gh


def test_recheck_resolves_live_cleared_rows():
    m = load()
    rows = [_row(1, 10, ['merged_closes_open']), _row(2, 20, ['done_open']),
            _row(3, 30, ['unowned_lifecycle']), _row(4, 40, ['unowned_lifecycle']),
            _row(5, 50, ['blockers_resolved']), _row(6, 60, ['blockers_resolved'])]
    live = {10: _live('CLOSED'), 20: _live('CLOSED'), 30: _live(labs={'status:in-progress'}),
            40: _live(labs={'status:in-review'}), 50: _live(labs={'status:todo'}),
            60: _live(labs={'pm-blocked:dependency'}, assignees=['someone'])}
    report, calls, _ = _recheck(m, rows, live, slots=[dict(occupied=True, issue=30)],
                                open_prs=[dict(number=99, body='Closes #40', title='', head={'ref': 'x'})])
    assert report['resolved'] == [1, 2, 3, 4, 5, 6]
    assert [c[0] for c in calls] == [1, 2, 3, 4, 5, 6]
    assert calls[0][1] == 'live_recheck_cleared:merged_closes_open:closed'


def test_recheck_keeps_open_rows_whose_condition_still_holds():
    m = load()
    rows = [_row(1, 10, ['done_open']), _row(2, 20, ['merged_closes_open']),
            _row(3, 30, ['unowned_lifecycle']), _row(4, 40, ['blockers_resolved'])]
    live = {10: _live(labs={'status:done'}), 20: _live(), 30: _live(labs={'status:in-progress'}),
            40: _live(labs={'pm-blocked:dependency'})}
    report, calls, _ = _recheck(m, rows, live)
    assert calls == [] and report['kept_not_cleared'] == [1, 2, 3, 4]


def test_recheck_never_resolves_window_only_mixed_or_unknown_rows():
    m = load()
    rows = [_row(1, 10, ['partial_merge']), _row(2, 20, ['done_open', 'partial_merge']),
            _row(3, 30, None), _row(4, 40, ['something_new'])]
    live = {n: _live('CLOSED') for n in (10, 20, 30, 40)}
    report, calls, gh = _recheck(m, rows, live)
    assert calls == [] and gh.call_count == 0
    assert report['kept_window_or_unknown'] == [1, 2, 3, 4]


def test_recheck_skips_claimed_rows_without_reading_or_writing():
    m = load()
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
    past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
    rows = [_row(1, 10, ['done_open'], 'agent', future), _row(2, 20, ['done_open'], 'agent', past),
            _row(3, 30, ['done_open'], 'PM', None)]
    report, calls, gh = _recheck(m, rows, {10: _live('CLOSED'), 20: _live('CLOSED'), 30: _live('CLOSED')})
    assert report['skipped_claimed'] == [1]
    assert [c[0] for c in calls] == [2, 3]
    assert [c.args[0] for c in gh.call_args_list] == [20, 30]


def test_recheck_race_claim_after_read_is_reported_skipped():
    m = load()
    report, calls, _ = _recheck(m, [_row(1, 10, ['done_open'])], {10: _live('CLOSED')}, outcome='skipped_claimed')
    assert calls and report['skipped_claimed'] == [1] and report['resolved'] == []


def test_recheck_gh_failure_skips_row_and_continues():
    m = load()
    rows = [_row(1, 10, ['done_open']), _row(2, 20, ['done_open'])]
    live = {10: m.subprocess.CalledProcessError(1, ['gh']), 20: _live('CLOSED')}
    report, calls, _ = _recheck(m, rows, live)
    assert report['skipped_gh_failure'] == [1] and [c[0] for c in calls] == [2]


def test_recheck_ignores_rows_still_reported_and_dry_run_never_writes():
    m = load()
    rows = [_row(1, 10, ['done_open']), _row(2, 20, ['done_open'])]
    report, calls, gh = _recheck(m, rows, {20: _live('CLOSED')}, findings=[dict(issue=10, reasons=['done_open'])],
                                 write=False)
    assert calls == [] and report['resolved'] == [2] and [c.args[0] for c in gh.call_args_list] == [20]


def test_recheck_preserves_narrowed_blockers_resolved_for_pure_rows():
    m = load()
    rows = [_row(1, 10, ['blockers_resolved'])]
    report, calls, _ = _recheck(m, rows, {10: _live(labs={'pm-blocked:dependency'})},
                                previous=[dict(issue=10, reasons=['blockers_resolved'])])
    assert report['resolved'] == [1]


def test_recheck_caps_live_reads_and_rotates_cursor():
    m = load()
    rows = [_row(i, 1000 + i, ['done_open']) for i in range(1, 61)]
    live = {1000 + i: _live() for i in range(1, 61)}
    first, _, gh = _recheck(m, rows, live)
    assert gh.call_count == m.RECHECK_CAP == 50 and first['cursor'] == 50
    second, _, gh2 = _recheck(m, rows, live, cursor=first['cursor'])
    assert [c.args[0] for c in gh2.call_args_list][:10] == list(range(1051, 1061))
    assert second['cursor'] == 40


def test_resolve_if_unclaimed_end_to_end_against_real_writer(tmp_path):
    m = load()
    legacy = ROOT / 'shared-assets/claude/scripts/pm-ops-legacy.py'
    db = str(tmp_path / 'pm.db')
    env = dict(__import__('os').environ, PM_OPS_DB=db)
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
    ids = []
    for n, extra in ((1, []), (2, ['--owner', 'agent', '--suppress-until', future])):
        out = m.subprocess.run([m.sys.executable, str(legacy), 'obligation-upsert', '--kind', 'orphaned_issue',
                                '--target-type', 'github_issue', '--target-id', str(n), '--issue', str(n),
                                '--title', 't', '--print-id', *extra], env=env, capture_output=True, text=True)
        ids.append(int(out.stdout.strip()))
    with patch.object(m, 'PM_OPS', legacy), patch.dict('os.environ', {'PM_OPS_DB': db}):
        assert m.resolve_if_unclaimed(ids[0], 'live_recheck_cleared:done_open:closed') == 'resolved'
        assert m.resolve_if_unclaimed(ids[1], 'live_recheck_cleared:done_open:closed') == 'skipped_claimed'
        assert m.resolve_if_unclaimed(ids[0], 'again') == 'skipped_not_open'
    con = __import__('sqlite3').connect(db)
    assert [r[0] for r in con.execute('SELECT status FROM obligations ORDER BY id')] == ['resolved', 'open']
    con.close()


def test_recheck_unowned_lifecycle_clears_when_lifecycle_label_gone():
    m = load()
    rows = [_row(1, 10, ['unowned_lifecycle']), _row(2, 20, ['unowned_lifecycle']),
            _row(3, 30, ['unowned_lifecycle', 'partial_merge'])]
    live = {10: _live(labs={'status:backlog', 'P1'}), 20: _live(labs={'status:in-review'}),
            30: _live(labs={'status:backlog'})}
    report, calls, _ = _recheck(m, rows, live)
    assert report['resolved'] == [1]
    assert report['kept_not_cleared'] == [2]
    assert report['kept_window_or_unknown'] == [3]
    assert calls[0][1] == 'live_recheck_cleared:unowned_lifecycle:open'


def test_recheck_passes_row_fingerprint_and_reports_changed():
    m = load()
    row = _row(1, 10, ['done_open'])
    row.update(updated_at='2026-11-05T01:00:00Z', evidence_sha256='ab' * 32)
    seen = []

    def write(rid, reason, updated_at=None, evidence_sha256=None):
        seen.append((updated_at, evidence_sha256))
        return 'skipped_changed'
    with patch.object(m, 'open_rows', return_value=[row]), patch.object(m, 'live_issue', return_value=_live('CLOSED')), \
         patch.object(m, 'resolve_if_unclaimed', side_effect=write):
        report = m.recheck([], [], [], [])
    assert seen == [('2026-11-05T01:00:00Z', 'ab' * 32)]
    assert report['skipped_changed'] == [1] and report['resolved'] == []


def test_upsert_during_live_read_survives_real_writer(tmp_path):
    """Hold the live read, canonically upsert new reasons, release: the newer row survives."""
    m = load()
    legacy = ROOT / 'shared-assets/claude/scripts/pm-ops-legacy.py'
    db = str(tmp_path / 'race.db')
    env = dict(__import__('os').environ, PM_OPS_DB=db)

    def upsert(n, reasons):
        m.subprocess.run([m.sys.executable, str(legacy), 'obligation-upsert', '--kind', 'orphaned_issue',
                          '--target-type', 'github_issue', '--target-id', str(n), '--issue', str(n), '--title', 't',
                          '--evidence-json', json.dumps(dict(issue=n, reasons=reasons))],
                         env=env, capture_output=True, text=True, check=False)
    upsert(10, ['done_open'])          # stale-able row: will be "cleared" by the live read
    upsert(20, ['done_open'])          # unchanged control: resolves normally

    def held_live_read(n):
        if n == 10:                    # while the read is "in flight", a fresh finding lands
            upsert(10, ['done_open', 'partial_merge'])
        return _live('CLOSED')
    with patch.object(m, 'PM_OPS', legacy), patch.dict('os.environ', {'PM_OPS_DB': db}), \
         patch.object(m, 'ledger_path', return_value=Path(db)), patch.object(m, 'live_issue', side_effect=held_live_read):
        report = m.recheck([], [], [], [])
    assert report['skipped_changed'] == [1] and report['resolved'] == [2]
    con = __import__('sqlite3').connect(db)
    rows = con.execute('SELECT issue, status, evidence_json FROM obligations ORDER BY id').fetchall()
    con.close()
    assert rows[0][1] == 'open' and 'partial_merge' in rows[0][2]
    assert rows[1][1] == 'resolved'


def test_live_issue_requires_well_formed_state_and_labels():
    m = load()
    good = dict(number=5, state='open', labels=[], assignees=[])
    with patch.object(m, 'gh', return_value=good):
        assert m.live_issue(5) == dict(state='OPEN', labels=set(), assignees=[])
    for bad in (dict(number=5, state='open'), dict(number=5, state='open', labels=''),
                dict(number=5, state='open', labels=None), dict(number=5, state='open', labels=[{'nope': 1}]),
                dict(number=5, state='open', labels=['status:in-progress']), dict(number=5, labels=[]),
                dict(number=5, state='weird', labels=[]), dict(number=6, state='open', labels=[]),
                dict(number=5, state='open', labels=[], assignees='x')):
        with patch.object(m, 'gh', return_value=bad), pytest.raises(ValueError):
            m.live_issue(5)


def test_recheck_packet_through_real_live_issue_and_writer(tmp_path):
    """CTO packet: changed reasons/head during the held read, valid-empty vs missing/malformed labels,
    unchanged resolution, and claim / failed-read / mixed-partial preservation, all through the real
    live_issue decoder and the real pm-ops writer."""
    m = load()
    legacy = ROOT / 'shared-assets/claude/scripts/pm-ops-legacy.py'
    db = str(tmp_path / 'packet.db')
    env = dict(__import__('os').environ, PM_OPS_DB=db)
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')

    def upsert(n, reasons, *extra):
        m.subprocess.run([m.sys.executable, str(legacy), 'obligation-upsert', '--kind', 'orphaned_issue',
                          '--target-type', 'github_issue', '--target-id', str(n), '--issue', str(n), '--title', 't',
                          '--evidence-json', json.dumps(dict(issue=n, reasons=reasons)), *extra],
                         env=env, capture_output=True, text=True, check=False)
    plan = {  # issue -> (stored reasons, upsert extras, gh payload or exception)
        101: (['unowned_lifecycle'], [], dict(number=101, state='open', labels=[], assignees=[])),      # valid empty
        102: (['unowned_lifecycle'], [], dict(number=102, state='open', assignees=[])),                 # labels missing
        103: (['unowned_lifecycle'], [], dict(number=103, state='open', labels='', assignees=[])),      # labels ""
        104: (['unowned_lifecycle'], [], dict(number=104, state='open', labels=[{'x': 1}], assignees=[])),  # malformed
        105: (['done_open'], [], dict(number=105, state='closed', labels=[], assignees=[])),            # changed mid-read
        106: (['done_open'], [], dict(number=106, state='closed', labels=[], assignees=[])),            # unchanged
        107: (['done_open'], ['--owner', 'agent', '--suppress-until', future],
              dict(number=107, state='closed', labels=[], assignees=[])),                               # claimed
        108: (['done_open'], [], m.subprocess.CalledProcessError(1, ['gh'])),                           # failed read
        109: (['done_open', 'partial_merge'], [], dict(number=109, state='closed', labels=[], assignees=[])),  # mixed
        110: (['unowned_lifecycle'], [], dict(number=110, state='open', labels=[{'name': 'status:in-progress'}],
                                              assignees=[])),                                           # still holds
    }
    for n, (reasons, extra, _) in plan.items():
        upsert(n, reasons, *extra)

    def fake_gh(*args):
        n = int(args[-1].rsplit('/', 1)[-1])
        payload = plan[n][2]
        if isinstance(payload, Exception):
            raise payload
        if n == 105:   # held read: a fresh finding is canonically upserted before the read returns
            upsert(105, ['done_open', 'partial_merge'])
        return payload
    with patch.object(m, 'PM_OPS', legacy), patch.dict('os.environ', {'PM_OPS_DB': db}), \
         patch.object(m, 'ledger_path', return_value=Path(db)), patch.object(m, 'gh', side_effect=fake_gh):
        report = m.recheck([], [], [], [])
    con = __import__('sqlite3').connect(db)
    status = {issue: (st, ev) for issue, st, ev in con.execute('SELECT issue, status, evidence_json FROM obligations')}
    con.close()
    ids = {n: i for i, n in enumerate(sorted(plan), start=1)}
    assert set(report['resolved']) == {ids[101], ids[106]}
    assert set(report['skipped_gh_failure']) == {ids[102], ids[103], ids[104], ids[108]}
    assert report['skipped_changed'] == [ids[105]]
    assert report['skipped_claimed'] == [ids[107]]
    assert report['kept_window_or_unknown'] == [ids[109]]
    assert report['kept_not_cleared'] == [ids[110]]
    assert {n for n, (st, _) in status.items() if st == 'resolved'} == {101, 106}
    assert 'partial_merge' in status[105][1]
