import importlib.util
import json
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

REPO = m.CI_SELECTION_REPO
PAD = 'x' * 79 + '\n'


def shard_log(notice=None, files=None, notice_after_pad=0):
    """A ~600 KB job log: the notice near the start, the vitest summary near the end.

    notice_after_pad pushes the notice past the head window, like shard 1's printed vitest inventory.
    """
    head = ['2026-11-17T23:40:00.0000000Z ##[group]Run actions/checkout@v4', PAD * 200]
    notice_line = []
    if notice:
        notice_line = [f'2026-11-17T23:40:28.2990151Z ##[notice]unit selection mode={notice[0]} reason={notice[1]}']
    # The workflow script dump also contains the echo text; it must never be read as a notice.
    head.append('2026-11-17T23:40:28.3000000Z \x1b[36;1m  print(f"::notice::unit selection mode={mode} reason={reason}")\x1b[0m')
    middle = PAD * 7000
    tail = []
    if files:
        passed, total = files
        failed = total - passed
        body = f'{failed} failed | {passed} passed' if failed else f'{passed} passed'
        tail.append(f'2026-11-17T23:54:01.7966920Z \x1b[2m Test Files \x1b[22m \x1b[1m\x1b[32m{body}\x1b[39m\x1b[22m\x1b[90m ({total})\x1b[39m')
    tail.append('2026-11-17T23:54:02.0000000Z ##[group]Post job cleanup.')
    if notice_after_pad:
        return '\n'.join(head) + '\n' + PAD * notice_after_pad + '\n'.join(notice_line) + '\n' + middle + '\n'.join(tail) + '\n'
    return '\n'.join(head + notice_line) + '\n' + middle + '\n'.join(tail) + '\n'


class Fake:
    """gh stand-in: run list, run jobs, ranged job logs and check-run annotations."""

    def __init__(self, runs):
        self.runs = runs  # [{id, branch, conclusion, shards: [{n, id, conclusion, log, annotation, log_rc}]}]
        self.calls = []
        self.timeouts = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        self.timeouts.append(kw.get('timeout'))
        assert cmd[0] == 'gh'
        if cmd[1] == 'run' and cmd[2] == 'list':
            return self.ok(json.dumps([{'databaseId': r['id'], 'headBranch': r['branch'], 'conclusion': r['conclusion']}
                                       for r in self.runs]))
        path = cmd[-1]
        if '/jobs?per_page' in path:
            run = next(r for r in self.runs if f"/runs/{r['id']}/jobs" in path)
            jobs = [{'id': s['id'], 'name': f"unit-tests ({s['n']})", 'status': 'completed', 'conclusion': s['conclusion']}
                    for s in run.get('shards', [])]
            jobs += [{'id': 1, 'name': 'typescript', 'status': 'completed', 'conclusion': 'success'},
                     {'id': 2, 'name': 'unit-tests-isolated', 'status': 'completed', 'conclusion': 'success'}]
            return self.ok(json.dumps({'jobs': jobs}))
        shard = next((s for r in self.runs for s in r.get('shards', []) if f"/jobs/{s['id']}/logs" in path
                      or f"/check-runs/{s['id']}/annotations" in path), None)
        if '/annotations' in path:
            notes = [{'annotation_level': 'warning', 'message': 'Node.js 20 is deprecated'}]
            if shard.get('annotation'):
                notes.append({'annotation_level': 'notice', 'message': f"unit selection mode={shard['annotation'][0]} reason={shard['annotation'][1]}"})
            return self.ok(json.dumps(notes))
        if shard.get('log_rc'):
            return subprocess.CompletedProcess(cmd, shard['log_rc'], '', 'gh: HTTP 404')
        span = next(a for a in cmd if a.startswith('Range: bytes=')).split('=')[1]
        start, end = (int(x) for x in span.split('-'))
        log = shard['log']
        body = log[start:end + 1]
        return self.ok(f"HTTP/2.0 206 Partial Content\r\nContent-Range: bytes {start}-{start + len(body) - 1}/{len(log)}\r\n\r\n" + body)

    @staticmethod
    def ok(out):
        return subprocess.CompletedProcess([], 0, out, '')


def full_run(run_id, branch, conclusion='success', files=((513, 513),) * 5, notice=('full', 'merge_parent_mismatch')):
    return {'id': run_id, 'branch': branch, 'conclusion': conclusion, 'shards': [
        {'n': i + 1, 'id': run_id * 10 + i, 'conclusion': 'success', 'log': shard_log(notice, f)} for i, f in enumerate(files)]}


def skipped_run(run_id):
    return {'id': run_id, 'branch': 'dup', 'conclusion': 'skipped', 'shards': []}


class CiSelectionTests(unittest.TestCase):
    def test_merge_parent_mismatch_renders_the_full_line(self):
        fake = Fake([skipped_run(9), full_run(101, 'fix/13701-flood'), skipped_run(8),
                     full_run(102, 'fix/13960-effect-receipt'), full_run(103, 'feat/x')])
        line = m.ci_selection_line(run=fake)
        self.assertTrue(line.startswith('*CI selection:* 101 fix/13701-flood: files 2565/2565; '), line)
        self.assertEqual(line.count('mode=full reason=merge_parent_mismatch ×5'), 3, line)
        self.assertNotIn('narrowed', line)
        self.assertNotIn('dup', line)  # skipped duplicates are not among the three
        self.assertLessEqual(len(line), 299)
        self.assertNotIn('\n', line)

    def test_failed_and_cancelled_shards_are_counted_not_hidden(self):
        run = full_run(201, 'fix/a', 'failure', files=((512, 517), (515, 515), (514, 518), (0, 0), (0, 0)))
        run['shards'][1]['conclusion'] = 'failure'
        for s in run['shards'][3:]:
            s['conclusion'] = 'cancelled'
        line = m.ci_selection_line(run=Fake([run]))
        self.assertIn('files 1541/1550', line)
        self.assertIn('2 cancelled', line)
        self.assertIn('mode=full reason=merge_parent_mismatch ×3', line)

    def test_narrowed_shard_renders_narrowed_with_an_observed_full_baseline(self):
        narrowed = full_run(301, 'feat/narrow', files=((40, 40), (41, 41), (40, 40), (42, 42), (40, 40)),
                            notice=('selected', 'selected'))
        base = full_run(302, 'fix/base')
        line = m.ci_selection_line(run=Fake([narrowed, base]))
        self.assertIn('301 feat/narrow: narrowed: 203 of 2565 files; mode=selected reason=selected ×5', line)
        self.assertIn('302 fix/base: files 2565/2565; mode=full reason=merge_parent_mismatch ×5', line)

    def test_narrowed_without_an_observed_full_run_does_not_invent_the_full_total(self):
        narrowed = full_run(311, 'feat/narrow', files=((40, 40),) * 5, notice=('selected', 'selected'))
        line = m.ci_selection_line(run=Fake([narrowed]))
        self.assertIn('narrowed: 200 files (full n/a)', line)
        self.assertNotIn(' of ', line)

    def test_notice_past_the_head_window_falls_back_to_the_annotation(self):
        run = full_run(401, 'fix/shard1', notice=None)
        run['shards'][0]['log'] = shard_log(('full', 'merge_parent_mismatch'), (513, 513), notice_after_pad=4000)
        run['shards'][0]['annotation'] = ('full', 'merge_parent_mismatch')
        for s in run['shards'][1:]:
            s['log'] = shard_log(('full', 'merge_parent_mismatch'), (513, 513))
        self.assertIsNone(m.parse_unit_selection(run['shards'][0]['log'][:m.CI_LOG_WINDOW]))
        line = m.ci_selection_line(run=Fake([run]))
        self.assertIn('mode=full reason=merge_parent_mismatch ×5', line)
        self.assertNotIn('notice missing', line)

    def test_missing_notice_is_reported_not_assumed_full(self):
        run = full_run(501, 'fix/old-workflow', notice=None)
        line = m.ci_selection_line(run=Fake([run]))
        self.assertIn('notice missing ×5', line)
        self.assertNotIn('mode=full', line)

    def test_workflow_script_echo_is_not_a_notice(self):
        echo = '2026-11-17T23:40:28.3000000Z   print(f"::notice::unit selection mode={mode} reason={reason}")'
        self.assertIsNone(m.parse_unit_selection(echo))
        self.assertEqual(m.parse_unit_selection('t ##[notice]unit selection mode=full reason=selector_failed'),
                         ('full', 'selector_failed'))

    def test_vitest_summary_parser_strips_ansi_and_prefers_the_main_run(self):
        text = ('t \x1b[2m Test Files \x1b[22m \x1b[1m\x1b[31m1 failed\x1b[39m | \x1b[32m512 passed\x1b[39m | 4 skipped (517)\n'
                't  Test Files  1 passed (1)\n')
        self.assertEqual(m.parse_vitest_files(text), (512, 517))
        self.assertEqual(m.parse_vitest_files('t  Test Files  3 passed (3)'), (3, 3))
        self.assertIsNone(m.parse_vitest_files('no summary here'))

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
        self.assertEqual(m.ci_selection_line(run=Fake([skipped_run(1), skipped_run(2)])),
                         '*CI selection:* unavailable (no completed non-skipped pull_request runs in the last 40)')

    def test_unreadable_logs_mark_the_run_unavailable(self):
        bad = full_run(601, 'fix/expired')
        for s in bad['shards']:
            s['log_rc'] = 1
        good = full_run(602, 'fix/good')
        line = m.ci_selection_line(run=Fake([bad, good]))
        self.assertIn('601 fix/expired: unavailable (gh rc=1)', line)
        self.assertIn('602 fix/good: files 2565/2565', line)
        only_bad = m.ci_selection_line(run=Fake([bad]))
        self.assertEqual(only_bad, '*CI selection:* unavailable (gh rc=1)')

    def test_no_unit_shards_and_all_cancelled(self):
        none_ran = {'id': 701, 'branch': 'docs/only', 'conclusion': 'success', 'shards': []}
        cancelled = full_run(702, 'fix/c', 'cancelled')
        for s in cancelled['shards']:
            s['conclusion'] = 'cancelled'
        line = m.ci_selection_line(run=Fake([none_ran, cancelled]))
        self.assertIn('701 docs/only: no unit shards ran', line)
        self.assertIn('702 fix/c: all 5 shards cancelled', line)

    def test_line_stays_under_300_chars_and_keeps_the_notice_groups(self):
        long = 'feature/' + 'very-long-branch-name-' * 6
        runs = [{'id': 40000000000 + i, 'branch': long, 'shards': [
            {'mode': 'full', 'reason': 'merge_parent_mismatch', 'passed': 513, 'total': 513}] * 3 + [
            {'mode': 'selected', 'reason': 'selected', 'passed': 40, 'total': 40}] * 2} for i in range(3)]
        line = m.render_ci_selection(runs)
        self.assertLessEqual(len(line), 299, len(line))
        self.assertIn('narrowed', line)
        self.assertIn('mode=full reason=merge_parent_mismatch ×3', line)
        absurd = [{'id': 1, 'branch': 'b', 'shards': [
            {'mode': 'full', 'reason': 'r' * 120 + str(i), 'passed': 1, 'total': 1}]} for i in range(5)]
        self.assertLessEqual(len(m.render_ci_selection(absurd)), 299)

    def test_branch_names_outlast_the_cancelled_notes_when_the_line_is_tight(self):
        runs = [{'id': 44200000000 + i, 'branch': f'fix/1352{i}-speaker-chunking-regression', 'shards': [
            {'mode': 'full', 'reason': 'merge_parent_mismatch', 'passed': 513, 'total': 513}] * 3 + [
            {'cancelled': True}] * 2} for i in range(3)]
        line = m.render_ci_selection(runs)
        self.assertLessEqual(len(line), 299, len(line))
        self.assertIn('fix/13520-', line)
        self.assertIn('mode=full reason=merge_parent_mismatch ×3', line)

    def test_untrusted_text_cannot_become_a_slack_mention_or_link(self):
        run = full_run(801, 'fix/x<!channel>|<@U0BNFGX2UAX>&amp;', notice=('full', '<!here>'))
        line = m.ci_selection_line(run=Fake([run]))
        for bad in '<>!@&|':
            self.assertNotIn(bad, line)

    def test_every_call_is_a_bounded_read_only_gh_get(self):
        fake = Fake([full_run(901, 'fix/a'), full_run(902, 'fix/b')])
        m.ci_selection_line(run=fake)
        self.assertTrue(fake.calls)
        for cmd in fake.calls:
            self.assertEqual(cmd[0], 'gh')
            self.assertIn(cmd[1], ('run', 'api'))
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
        self.assertEqual(line, '*CI selection:* unavailable (timeout 5s)')


class ComposeWiringTests(unittest.TestCase):
    NOW = datetime(2026, 11, 18, 4, 0, tzinfo=timezone.utc)

    def compose(self, mode, **kw):
        return m.compose(mode, self.NOW, {'top': [], 'all_codes': [], 'total': 0}, {}, [], **kw)

    def test_hourly_renders_the_line_under_remote_boxes_and_3h_does_not(self):
        line = '*CI selection:* 1 fix/a: files 513/513; mode=full reason=merge_parent_mismatch ×5'
        hourly = self.compose('1h', ci_selection=line, remote_boxes='*Remote boxes:* ok')
        rows = hourly.splitlines()
        self.assertIn(line, rows)
        self.assertLess(rows.index('*Remote boxes:* ok'), rows.index(line))
        self.assertNotIn('CI selection', self.compose('3h', ci_selection=line))

    def test_missing_collector_result_says_unavailable_not_none(self):
        hourly = self.compose('1h')
        self.assertIn('*CI selection:* unavailable (collector not run)', hourly.splitlines())

    def test_line_survives_the_row_budget(self):
        line = '*CI selection:* 1 fix/a: files 513/513; mode=full reason=merge_parent_mismatch ×5'
        hourly = self.compose('1h', ci_selection=line, remote_boxes='*Remote boxes:* ok',
                              pace='| a | b |\n| c | d |', memory='*Claude RSS:* 1 GB', opencode='*OpenCode Go:* 0')
        self.assertIn(line, hourly.splitlines())

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
