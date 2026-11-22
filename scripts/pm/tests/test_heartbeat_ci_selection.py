"""CI selection line: coverage bound to run id + attempt + head, no borrowed baseline, markers survive compaction.

CTO REVISE 2026-11-18 (C0ALZJHGE49 thread 1794296562.075779, ts 1794968959.921749). Each fix has tests that
fail when it is reverted:
  1 (attempt/head binding, expected vs observed shards, UNKNOWN): test_complete_attempt_is_bound_to_run_attempt_and_head,
    test_rerun_attempt_reads_that_attempt_and_missing_shards_are_partial, test_job_of_another_attempt_or_head_is_coverage_unknown,
    test_unknown_expected_shard_set_is_coverage_unknown. The old collector read the unversioned run jobs and printed files.
  2 (no borrowed baseline, no largest-summary): test_selected_run_never_borrows_another_runs_total,
    test_vitest_summary_is_never_read. The old line printed "narrowed: X of Y files" from another run and took the
    largest "Test Files" summary in the window.
  3 (compaction keeps the marker): test_compaction_keeps_the_partial_marker_and_carries_no_cross_run_ratio,
    test_only_complete_entries_are_omitted. The old compaction dropped the cancelled notes before branch names
    and had no per-run PARTIAL / UNKNOWN marker at all.
"""
import importlib.util
import json
import re
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / 'shared-assets/claude/scripts/pm/heartbeat/heartbeat-compose.py'
spec = importlib.util.spec_from_file_location('heartbeat_ci_selection', PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

PAD = 'x' * 79 + '\n'
WORKFLOW = ('jobs:\n  unit-tests:\n    strategy:\n      fail-fast: false\n      matrix:\n        shard: [1, 2, 3, 4, 5]\n'
            '  unit-tests-isolated:\n    steps: []\n')
LONG = 'feature/' + 'very-long-branch-name-' * 4


def head_of(run_id):
    return (f'{run_id:x}' * 40)[:40]


def shard_log(shard, mode='full', reason='merge_parent_mismatch', files=513, selected=None, configured=None, of=5,
              row=True, vitest_summary=None):
    """A ~400 KB job log: the executed-unit receipt and the selection row sit in the tail window."""
    lines = ['2026-11-17T23:40:00.0000000Z ##[group]Run actions/checkout@v4', PAD * 5000]
    if of:
        receipt = {'head': 'c' * 40, 'shard': shard, 'of': of, 'runner': 'r', 'shared': None}
        lines.append('2026-11-17T23:50:00.0000000Z ' + json.dumps(receipt, separators=(',', ':')))
    if row:
        payload = {'assigned_count': None, 'configured_inventory_count': configured, 'configured_mode': 'enforce_full',
                   'effective_mode': mode, 'executed_files': files, 'executed_tests': files * 20,
                   'full_reason': reason if mode == 'full' else None, 'selected_inventory_count': selected,
                   'selector_mode': mode, 'shard': str(shard)}
        lines.append('2026-11-17T23:50:01.0000000Z functional selection report '
                     + json.dumps(payload, separators=(',', ':'), sort_keys=True))
    if vitest_summary:
        lines.append('2026-11-17T23:50:02.0000000Z \x1b[2m Test Files \x1b[22m ' + vitest_summary)
    lines.append('2026-11-17T23:54:02.0000000Z ##[group]Post job cleanup.')
    return '\n'.join(lines) + '\n'


def leg(run_id, n, conclusion='success', **log_kw):
    return {'n': n, 'id': run_id * 10 + n, 'conclusion': conclusion, 'log': shard_log(n, **log_kw)}


def run_of(run_id, branch, shards, attempt=1, conclusion='success'):
    return {'id': run_id, 'branch': branch, 'attempt': attempt, 'conclusion': conclusion, 'shards': shards}


def full_run(run_id, branch, attempt=1, files=(513, 514, 515, 516, 517), **kw):
    return run_of(run_id, branch, [leg(run_id, i + 1, files=f) for i, f in enumerate(files)], attempt, **kw)


def seg(run_id, attempt, branch, body):
    return f'{run_id}#{attempt}/{head_of(run_id)[:8]} {branch}: {body}'


def gate_job(run_id, attempt, skipped, **over):
    """ci.yml's `test` job. A pull_request run started by any label but ci-head:<sha> skips the gate step."""
    job = {'id': 9, 'name': 'test', 'status': 'completed', 'conclusion': 'success',
           'run_id': run_id, 'run_attempt': attempt, 'head_sha': head_of(run_id),
           'steps': [{'name': 'Set up job', 'conclusion': 'success'},
                     {'name': 'Require all CI phases', 'conclusion': 'skipped' if skipped else 'success'}]}
    return {**job, **over}


def noop_run(run_id, branch, attempt=1):
    return dict(run_of(run_id, branch, [], attempt), noop=True)


def admitted_run(run_id, branch, **kw):
    return dict(full_run(run_id, branch, **kw), noop=False)


class Fake:
    """gh stand-in: run list, ci.yml at a ref, per-attempt jobs and ranged job logs."""

    def __init__(self, runs, workflow=WORKFLOW):
        self.runs, self.workflow = runs, workflow
        self.calls, self.timeouts, self.job_reads = [], [], []

    @staticmethod
    def ok(out):
        return subprocess.CompletedProcess([], 0, out, '')

    @staticmethod
    def jobs(run):
        jobs = [{'id': s['id'], 'name': f"unit-tests ({s['n']})", 'status': 'completed', 'conclusion': s['conclusion'],
                 'run_id': run['id'], 'run_attempt': run.get('job_attempt', run['attempt']),
                 'head_sha': run.get('job_head', head_of(run['id']))} for s in run['shards']]
        jobs += [{'id': 1, 'name': 'typescript', 'status': 'completed', 'conclusion': 'success'},
                 {'id': 2, 'name': 'unit-tests-isolated', 'status': 'completed', 'conclusion': 'success'}]
        if 'noop' in run:  # opt-in: ci.yml's `test` job, whose "Require all CI phases" step is skipped on a noop run
            jobs.append(gate_job(run['id'], run['attempt'], skipped=run['noop']))
            if run['noop']:
                jobs.append({'id': 3, 'name': 'unit-tests', 'status': 'completed', 'conclusion': 'skipped',
                             'run_id': run['id'], 'run_attempt': run['attempt'], 'head_sha': head_of(run['id'])})
        return jobs

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        self.timeouts.append(kw.get('timeout'))
        assert cmd[0] == 'gh'
        if cmd[1] == 'run' and cmd[2] == 'list':
            return self.ok(json.dumps([{'databaseId': r['id'], 'headBranch': r['branch'], 'headSha': head_of(r['id']),
                                        'attempt': r['attempt'], 'conclusion': r['conclusion']} for r in self.runs]))
        path = cmd[-1]
        if '/contents/.github/workflows/ci.yml?ref=' in path:
            if self.workflow is None:
                return subprocess.CompletedProcess(cmd, 1, '', 'gh: HTTP 404')
            return self.ok(self.workflow)
        found = re.search(r'/runs/(\d+)/attempts/(\d+)/jobs\?per_page=100$', path)
        if found:
            run = next(r for r in self.runs if str(r['id']) == found.group(1))
            self.job_reads.append((run['id'], int(found.group(2))))
            jobs = self.jobs(run)
            return self.ok(json.dumps({'total_count': len(jobs), 'jobs': jobs}))
        shard = next(s for r in self.runs for s in r['shards'] if path.endswith(f"/actions/jobs/{s['id']}/logs"))
        if shard.get('log_rc'):
            return subprocess.CompletedProcess(cmd, shard['log_rc'], '', 'gh: HTTP 404')
        span = next(a for a in cmd if a.startswith('Range: bytes=')).split('=')[1]
        start, end = (int(x) for x in span.split('-'))
        log = shard['log']
        body = log[start:end + 1]
        return self.ok(f"HTTP/2.0 206 Partial Content\r\nContent-Range: bytes {start}-{start + len(body) - 1}/{len(log)}\r\n\r\n" + body)


def evidence(run_id, ok=5, attempt=1):
    """Collected evidence for the pure renderer: ok shards with rows, the rest cancelled (or absent)."""
    shards = {n: {'state': 'ok', 'failed': False,
                  'row': {'shard': n, 'mode': 'full', 'reason': 'merge_parent_mismatch', 'files': 513, 'tests': 1,
                          'selected': None, 'configured': None, 'of': 5}} for n in range(1, ok + 1)}
    for n in range(ok + 1, 6):
        shards[n] = {'state': 'cancelled'}
    return {'id': run_id, 'attempt': attempt, 'head': head_of(run_id), 'branch': 'fix/x', 'matrix': {1, 2, 3, 4, 5},
            'shards': shards}


class CiSelectionTests(unittest.TestCase):
    def test_complete_attempt_is_bound_to_run_attempt_and_head(self):
        fake = Fake([full_run(101, 'fix/13701-flood')])
        line = m.ci_selection_line(run=fake)
        self.assertEqual(line, '*CI selection:* ' + seg(101, 1, 'fix/13701-flood',
                                                        '5/5 shards ran 2575 files; full:merge_parent_mismatch ×5'))
        self.assertEqual(fake.job_reads, [(101, 1)])
        self.assertLessEqual(len(line), 299)

    def test_rerun_attempt_reads_that_attempt_and_missing_shards_are_partial(self):
        rerun = full_run(202, 'fix/rerun', attempt=2)
        rerun['shards'] = rerun['shards'][:3]  # the attempt lists only 3 of the 5 matrix legs
        fake = Fake([rerun])
        line = m.ci_selection_line(run=fake)
        self.assertEqual(fake.job_reads, [(202, 2)])
        self.assertEqual(line, '*CI selection:* ' + seg(202, 2, 'fix/rerun',
                                                        'PARTIAL 3/5 shards (2 missing) ran 1542 files in them; full:merge_parent_mismatch ×3'))

    def test_job_of_another_attempt_or_head_is_coverage_unknown(self):
        stale = full_run(301, 'fix/a', attempt=2)
        stale['job_attempt'] = 1
        foreign = full_run(302, 'fix/b')
        foreign['job_head'] = 'f' * 40
        line = m.ci_selection_line(run=Fake([stale, foreign]))
        self.assertIn(seg(301, 2, 'fix/a', 'coverage UNKNOWN (job is not of this run attempt and head)'), line)
        self.assertIn(seg(302, 1, 'fix/b', 'coverage UNKNOWN (job is not of this run attempt and head)'), line)
        self.assertNotIn('shards', line)

    def test_noop_runs_are_labelled_never_quoted_as_partial_shards(self):
        """A label-triggered run that was never admitted skips every lane. It must not read as "PARTIAL 0/5 shards"."""
        fake = Fake([noop_run(501, 'fix/n1'), admitted_run(502, 'fix/a'), noop_run(503, 'fix/n2')])
        line = m.ci_selection_line(run=fake)
        full = '5/5 shards ran 2575 files; full:merge_parent_mismatch ×5'
        self.assertEqual(line, '*CI selection:* ' + seg(502, 1, 'fix/a', full) + ' | noop (not admitted) ×2')
        self.assertNotIn('PARTIAL', line)
        self.assertNotIn('501#', line)
        self.assertNotIn('503#', line)

    def test_noop_runs_do_not_use_up_the_admitted_run_slots(self):
        wanted = m.CI_SELECTION_RUNS
        runs = [noop_run(600, 'fix/n0'), noop_run(601, 'fix/n1')]
        runs += [admitted_run(610 + i, f'fix/a{i}') for i in range(wanted + 1)]
        fake = Fake(runs)
        line = m.ci_selection_line(run=fake)
        for i in range(wanted):
            self.assertIn(f'{610 + i}#1/', line)
        self.assertNotIn(f'{610 + wanted}#1/', line)  # one admitted run past the quota is never read
        self.assertIn('noop (not admitted) ×2', line)
        self.assertNotIn(f'{610 + wanted}', ' '.join(str(rid) for rid, _ in fake.job_reads))
        self.assertLessEqual(len(line), 299)

    def test_only_noop_runs_say_so_instead_of_showing_shards(self):
        line = m.ci_selection_line(run=Fake([noop_run(701, 'fix/a'), noop_run(702, 'fix/b')]))
        self.assertEqual(line, '*CI selection:* no admitted ci-head run found; noop (not admitted) ×2')

    def test_noop_signal_is_the_test_jobs_gate_step_and_nothing_looser(self):
        def entry():
            return {'id': 801, 'attempt': 1, 'head': head_of(801), 'branch': 'fix/x'}

        def classify(job):
            e = entry()
            m.ci_classify_jobs(e, [job] if job else [])
            return bool(e.get('noop'))
        self.assertTrue(classify(gate_job(801, 1, skipped=True)))
        self.assertFalse(classify(gate_job(801, 1, skipped=False)))
        self.assertFalse(classify(None))  # no `test` job: not a noop
        self.assertFalse(classify(gate_job(801, 1, skipped=True, status='in_progress')))  # not finished: not a noop
        self.assertFalse(classify(gate_job(801, 1, skipped=True, steps=None)))
        self.assertFalse(classify(gate_job(801, 1, skipped=True, steps=[{'name': 'Set up job', 'conclusion': 'skipped'}])))
        self.assertFalse(classify(gate_job(801, 1, skipped=True, name='classify-change-scope')))  # scope job is not the signal
        with self.assertRaises(m.CiSelectionError):  # a job of another attempt can not mark this run a noop
            classify(gate_job(801, 1, skipped=True, run_attempt=2))

    def test_unknown_expected_shard_set_is_coverage_unknown(self):
        cancelled = full_run(401, 'fix/c')
        for s in cancelled['shards']:
            s['conclusion'] = 'cancelled'
        line = m.ci_selection_line(run=Fake([cancelled], workflow=None))  # no matrix, no receipt of an executed shard
        self.assertIn(seg(401, 1, 'fix/c', 'coverage UNKNOWN (expected shards unknown)'), line)
        disagree = run_of(402, 'fix/d', [leg(402, i + 1, of=4) for i in range(5)])  # receipts say 4, the matrix says 5
        line = m.ci_selection_line(run=Fake([disagree]))
        self.assertIn(seg(402, 1, 'fix/d', 'coverage UNKNOWN (matrix and receipt shard sets differ)'), line)
        line = m.ci_selection_line(run=Fake([full_run(403, 'fix/e')], workflow=None))  # receipts alone establish it
        self.assertIn('403#1/', line)
        self.assertIn(': 5/5 shards ran 2575 files', line)
        self.assertEqual(m.parse_unit_matrix(WORKFLOW), {1, 2, 3, 4, 5})
        self.assertIsNone(m.parse_unit_matrix('jobs:\n  other:\n    x: 1\n'))

    def test_selected_run_never_borrows_another_runs_total(self):
        narrowed = run_of(501, 'feat/narrow', [leg(501, i + 1, mode='selected', files=40, selected=200, configured=3000)
                                               for i in range(5)])
        line = m.ci_selection_line(run=Fake([narrowed, full_run(502, 'fix/base')]))
        first = line.split(' | ')[0]
        self.assertNotIn('narrowed', line)
        self.assertIn('5/5 shards ran 200 files; selected ×5; selected 200 of 3000 files', first)  # its own rows only
        self.assertNotIn('2575', first)
        bare = run_of(511, 'feat/bare', [leg(511, i + 1, mode='selected', files=40) for i in range(5)])
        line = m.ci_selection_line(run=Fake([bare, full_run(512, 'fix/base')]))
        first = line.split(' | ')[0]
        self.assertIn('selected counts UNKNOWN', first)
        self.assertNotIn(' of ', first)
        self.assertNotIn('2575', first)

    def test_mixed_modes_stay_mixed(self):
        shards = [leg(601, i + 1, mode='selected', files=40, selected=120, configured=2575) for i in range(3)]
        shards += [leg(601, i + 1, files=513) for i in (3, 4)]
        line = m.ci_selection_line(run=Fake([run_of(601, 'feat/mixed', shards)]))
        self.assertIn('mixed selected ×3, full:merge_parent_mismatch ×2', line)
        self.assertIn('selected 120 of 2575 files', line)

    def test_vitest_summary_is_never_read(self):
        run = full_run(701, 'fix/s1')
        run['shards'][0]['log'] = shard_log(1, files=513, vitest_summary='1 passed (1)')  # shard 1's NYSD summary
        run['shards'][1]['log'] = shard_log(2, files=514, row=False, vitest_summary='514 passed (514)')
        line = m.ci_selection_line(run=Fake([run]))
        self.assertEqual(line, '*CI selection:* ' + seg(701, 1, 'fix/s1',
                                                        'PARTIAL 4/5 shards (1 unreadable) ran 2061 files in them; full:merge_parent_mismatch ×4'))

    def test_compaction_keeps_the_partial_marker_and_carries_no_cross_run_ratio(self):
        complete = full_run(801, LONG)
        rerun = full_run(802, LONG, attempt=2)
        rerun['shards'] = rerun['shards'][:3]  # shards 4 and 5 are missing from the rerun attempt
        rerun['shards'][2]['conclusion'] = 'cancelled'
        line = m.ci_selection_line(run=Fake([complete, rerun, full_run(803, LONG)]))
        self.assertLessEqual(len(line), 299, len(line))
        self.assertNotIn('merge_parent_mismatch', line)  # the verbose reasons went first
        self.assertIn(f'802#2/{head_of(802)[:8]}', line)
        self.assertIn('PARTIAL 2/5 shards (1 cancelled, 2 missing)', line)
        self.assertEqual(line.count('5/5 shards'), 2)
        self.assertIsNone(re.search(r'\d+ of \d+|\d+/\d+ files', line), line)

    def test_only_complete_entries_are_omitted(self):
        runs = [evidence(44200000000 + i, ok=3 if i in (3, 6) else 5) for i in range(7)]
        runs.append({'id': 44200000007, 'attempt': None, 'head': None, 'branch': 'b', 'error': 'run attempt or head unknown'})
        line = m.render_ci_selection(runs)
        self.assertLessEqual(len(line), 299, len(line))
        self.assertEqual(line.count('PARTIAL 3/5 shards'), 2)
        self.assertIn('44200000007#?: coverage UNKNOWN', line)
        self.assertIn('+5 omitted', line)
        self.assertNotIn('44200000000#', line)

    def test_listing_errors_are_unavailable_never_none(self):
        def rc1(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 1, '', 'gh: boom')

        def slow(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 25)

        def bad_json(cmd, **kw):
            return Fake.ok('not json')

        def missing(cmd, **kw):
            raise FileNotFoundError('gh')
        for runner, reason in ((rc1, 'gh rc=1'), (slow, 'timeout 60s'), (bad_json, 'invalid GitHub response'),
                               (missing, 'FileNotFoundError')):
            line = m.ci_selection_line(run=runner)
            self.assertEqual(line, f'*CI selection:* unavailable ({reason})')
            self.assertNotIn('none', line.lower())
        skipped = [run_of(1, 'dup', [], conclusion='skipped'), run_of(2, 'dup', [], conclusion='skipped')]
        self.assertEqual(m.ci_selection_line(run=Fake(skipped)),
                         f'*CI selection:* unavailable (no completed non-skipped pull_request runs in the last {m.CI_SELECTION_SCAN})')

    def test_unreadable_logs_make_the_shards_unreadable_not_the_line_empty(self):
        bad = run_of(601, 'fix/expired', [dict(leg(601, i + 1), log_rc=1) for i in range(5)])
        line = m.ci_selection_line(run=Fake([bad, full_run(602, 'fix/good')]))
        self.assertIn(seg(601, 1, 'fix/expired', 'PARTIAL 0/5 shards (5 unreadable)'), line)
        self.assertIn('602#1/', line)

    def test_untrusted_text_cannot_become_a_slack_mention_or_link(self):
        run = run_of(901, 'fix/x<!channel>|<@U0BNFGX2UAX>&amp;', [leg(901, i + 1, reason='<!here>') for i in range(5)])
        line = m.ci_selection_line(run=Fake([run]))
        for bad in '<>!@&|':
            self.assertNotIn(bad, line)

    def test_every_call_is_a_bounded_read_only_gh_get(self):
        fake = Fake([full_run(951, 'fix/a'), full_run(952, 'fix/b')])
        m.ci_selection_line(run=fake)
        self.assertTrue(fake.calls)
        for cmd in fake.calls:
            self.assertEqual(cmd[0], 'gh')
            self.assertTrue(cmd[1] == 'run' and cmd[2] == 'list' or cmd[1] == 'api')
            self.assertFalse({'-X', '--method', '-f', '-F', '--input', 'POST', 'PATCH', 'DELETE', 'PUT'} & set(cmd), cmd)
        self.assertTrue(all(t is not None and t <= 25 for t in fake.timeouts), fake.timeouts)
        self.assertLessEqual(m.COLLECTOR_TIMEOUTS['ci-selection'], 60)

    def test_expired_deadline_makes_no_call(self):
        fake = Fake([full_run(1001, 'fix/a')])
        self.assertEqual(m.ci_selection_line(run=fake, timeout=0), '*CI selection:* unavailable (timeout 0s)')
        self.assertEqual(fake.calls, [])

    def test_slow_shard_logs_do_not_exceed_the_deadline(self):
        base = Fake([full_run(1101, 'fix/a')])

        def stall_on_logs(cmd, **kw):
            if '-i' in cmd:
                time.sleep(2)
                raise subprocess.TimeoutExpired(cmd, kw['timeout'])
            return base(cmd, **kw)
        started = time.monotonic()
        line = m.ci_selection_line(run=stall_on_logs, timeout=5)
        self.assertLess(time.monotonic() - started, 12)
        self.assertIn('PARTIAL 0/5 shards (5 unreadable)', line)


class ComposeWiringTests(unittest.TestCase):
    NOW = datetime(2026, 11, 18, 4, 0, tzinfo=timezone.utc)
    LINE = '*CI selection:* 1#1/11111111 fix/a: 5/5 shards ran 2565 files; full:merge_parent_mismatch ×5'

    def compose(self, mode, **kw):
        return m.compose(mode, self.NOW, {'top': [], 'all_codes': [], 'total': 0}, {}, [], **kw)

    def test_hourly_renders_the_line_under_remote_boxes_and_3h_does_not(self):
        hourly = self.compose('1h', ci_selection=self.LINE, remote_boxes='*Remote boxes:* ok')
        rows = hourly.splitlines()
        self.assertIn(self.LINE, rows)
        self.assertLess(rows.index('*Remote boxes:* ok'), rows.index(self.LINE))
        self.assertNotIn('CI selection', self.compose('3h', ci_selection=self.LINE))

    def test_missing_collector_result_says_unavailable_not_none(self):
        hourly = self.compose('1h')
        self.assertIn('*CI selection:* unavailable (collector not run)', hourly.splitlines())

    def test_line_survives_the_row_budget(self):
        hourly = self.compose('1h', ci_selection=self.LINE, remote_boxes='*Remote boxes:* ok',
                              pace='| a | b |\n| c | d |', memory='*Claude RSS:* 1 GB', opencode='*OpenCode Go:* 0')
        self.assertIn(self.LINE, hourly.splitlines())

    def test_collector_is_registered_hourly_and_never_served_from_cache(self):
        args = SimpleNamespace(slots_json=None, decisions_json='d', agents_json='a', axiom_json='x', prs_json='p',
                               active_json='ac', map_json='mp', no_active_users=False, mode='1h', sample_usage_claim=None)
        seen = {}
        with patch.object(m, 'parallel_collect', lambda jobs, **kw: seen.update(jobs) or ({}, {})):
            m.collect_hourly(args, 'python3', '1', self.NOW, [])
        self.assertIn('ci-selection', seen)
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / 'ci-selection.json').write_text(json.dumps({'at': time.time(), 'value': '*CI selection:* old'}))
            unavailable = '*CI selection:* unavailable (gh rc=1)'
            results, stale = m.parallel_collect({'ci-selection': lambda: unavailable}, wall_cap=5, cache_dir=cache)
        self.assertEqual(results['ci-selection'], unavailable)
        self.assertEqual(stale, {})


if __name__ == '__main__':
    unittest.main()
