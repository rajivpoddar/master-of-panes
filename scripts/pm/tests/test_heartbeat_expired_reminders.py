#!/usr/bin/env python3
"""Hourly report: expired-reminder line (CTO ask C0ALZJHGE49 thread 1794990919.983979, ts 1794991008.258509 / 1794991173.488999).

Source: MoP 3f4be39 `GET /alert-reminders?expired_since=48h`. Each row shows the key, the time the scheduler MARKED it
expired (`updated_at`, IST) and "no stop obligation". "none" (empty successful list) is distinct from "listing
unavailable" (failed or malformed read).
"""
import importlib.util
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / 'shared-assets/claude/scripts/pm/heartbeat/heartbeat-compose.py'
spec = importlib.util.spec_from_file_location('heartbeat_expired_reminders', PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

LABEL = '*Expired reminders (48h):*'


def row(key, updated_at, until_at=None, stop_obligation_id=None):
    return {'key': key, 'status': 'expired', 'stop_reason': 'until_passed', 'stop_obligation_id': stop_obligation_id,
            'until_at': until_at, 'updated_at': updated_at}


def ok(*rows):
    return {'success': True, 'expired_since_ms': 172800000, 'reminders': list(rows), 'scheduler': {}}


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


def read_with(body):
    with patch('urllib.request.urlopen', lambda url, timeout=0: FakeResponse(body)):
        return m.expired_reminders_line()


class RenderTests(unittest.TestCase):
    def test_empty_successful_list_says_none(self):
        self.assertEqual(m.render_expired_reminders(ok()), f'{LABEL} none')

    def test_rows_show_key_expired_at_in_ist_and_no_stop_obligation(self):
        line = m.render_expired_reminders(ok(
            row('practice-pilot-hourly-dev', '2026-11-17T07:22:24.821Z', '2026-11-17T07:22:09.792Z'),
            row('p1-13062-hourly', '2026-11-16T13:55:09.438Z', '2026-11-16T14:53:34.000Z')))
        self.assertTrue(line.startswith(LABEL))
        self.assertIn('practice-pilot-hourly-dev expired Tue 12:52 IST, no stop obligation', line)
        self.assertIn('p1-13062-hourly expired Mon 19:25 IST, no stop obligation', line)

    def test_time_is_when_the_scheduler_marked_it_expired_not_the_scheduled_cutoff(self):
        # A delayed tick: until_at 14:53:34Z (20:23 IST) is later than the mark at 13:55:09Z (19:25 IST) here.
        line = m.render_expired_reminders(ok(row('k', '2026-11-16T13:55:09.438Z', '2026-11-16T14:53:34.000Z')))
        self.assertIn('19:25 IST', line)
        self.assertNotIn('20:23', line)

    def test_unparseable_updated_at_is_labelled_not_guessed(self):
        line = m.render_expired_reminders(ok(row('k', 'not-a-time'), row('j', None)))
        self.assertIn('k expired at an unknown time, no stop obligation', line)
        self.assertIn('j expired at an unknown time, no stop obligation', line)

    def test_key_cannot_inject_mentions_or_markup(self):
        line = m.render_expired_reminders(ok(row('<@U0BNFGX2UAX> `x` *b*', '2026-11-17T07:22:24Z')))
        for bad in ('@', '<', '>', '`', '*b*'):
            self.assertNotIn(bad, line.split(LABEL, 1)[1])

    def test_many_rows_are_capped_with_a_count(self):
        rows = [row(f'k{i}', '2026-11-17T07:22:24Z') for i in range(m.MAX_EXPIRED_REMINDERS + 2)]
        line = m.render_expired_reminders(ok(*rows))
        self.assertIn('; +2 more', line)
        self.assertEqual(line.count('no stop obligation'), m.MAX_EXPIRED_REMINDERS)

    def test_failed_or_malformed_documents_say_listing_unavailable_never_none(self):
        bad_docs = [
            None,
            {},
            {'success': False, 'error': 'invalid expired_since'},
            {'success': True},
            {'success': True, 'reminders': 'nope'},
            {'success': 'yes', 'reminders': []},
            {'success': True, 'reminders': [None]},
            {'success': True, 'reminders': [{'updated_at': '2026-11-17T07:22:24Z'}]},
            {'success': True, 'reminders': [row('', '2026-11-17T07:22:24Z')]},
            [],
            'ok',
        ]
        for doc in bad_docs:
            with self.subTest(doc=doc):
                line = m.render_expired_reminders(doc)
                self.assertEqual(line, f'{LABEL} listing unavailable')
                self.assertNotIn('none', line)

    def test_row_that_already_has_a_stop_obligation_is_never_labelled_no_stop_obligation(self):
        line = m.render_expired_reminders(ok(row('k', '2026-11-17T07:22:24Z', stop_obligation_id='ob-1')))
        self.assertNotIn('no stop obligation', line)
        self.assertIn('listing unavailable', line)


class CollectorTests(unittest.TestCase):
    def test_reads_rows_over_http(self):
        body = json.dumps(ok(row('p1-13062-hourly', '2026-11-16T13:55:09.438Z'))).encode()
        self.assertIn('p1-13062-hourly expired Mon 19:25 IST, no stop obligation', read_with(body))

    def test_empty_list_over_http_says_none(self):
        self.assertEqual(read_with(json.dumps(ok()).encode()), f'{LABEL} none')

    def test_unreadable_bodies_say_listing_unavailable(self):
        for body in (b'', b'not json', b'{"success": false, "error": "bad"}', b'[]'):
            with self.subTest(body=body):
                self.assertEqual(read_with(body), f'{LABEL} listing unavailable')

    def test_connection_errors_and_timeouts_say_listing_unavailable_and_never_raise(self):
        for exc in (ConnectionRefusedError('down'), TimeoutError('timed out'), OSError('x'), ValueError('y')):
            with self.subTest(exc=type(exc).__name__):
                def boom(url, timeout=0, exc=exc):
                    raise exc
                with patch('urllib.request.urlopen', boom):
                    self.assertEqual(m.expired_reminders_line(), f'{LABEL} listing unavailable')

    def test_it_is_a_get_of_the_48h_window_with_the_short_timeout(self):
        seen = {}

        def fake(url, timeout=0):
            seen.update(url=url, timeout=timeout)
            return FakeResponse(json.dumps(ok()).encode())
        with patch('urllib.request.urlopen', fake):
            m.expired_reminders_line()
        self.assertEqual(seen['url'], 'http://127.0.0.1:3100/alert-reminders?expired_since=48h')
        self.assertEqual(seen['timeout'], m.COLLECTOR_TIMEOUTS['reminders'])


class ComposeWiringTests(unittest.TestCase):
    NOW = datetime(2026, 11, 18, 4, 0, tzinfo=timezone.utc)
    LINE = f'{LABEL} p1-13062-hourly expired Mon 19:25 IST, no stop obligation'

    def compose(self, mode, **kw):
        return m.compose(mode, self.NOW, {'top': [], 'all_codes': [], 'total': 0}, {}, [], **kw)

    def test_hourly_renders_the_line_under_ci_selection_and_3h_does_not(self):
        ci = '*CI selection:* ok'
        rows = self.compose('1h', expired_reminders=self.LINE, ci_selection=ci).splitlines()
        self.assertIn(self.LINE, rows)
        self.assertLess(rows.index(ci), rows.index(self.LINE))
        self.assertNotIn('Expired reminders', self.compose('3h', expired_reminders=self.LINE))

    def test_missing_collector_result_says_listing_unavailable_not_none(self):
        rows = self.compose('1h').splitlines()
        self.assertIn(f'{LABEL} listing unavailable (no result)', rows)
        self.assertFalse(any(r.startswith(LABEL) and r.endswith(' none') for r in rows))

    def test_line_survives_the_row_budget(self):
        hourly = self.compose('1h', expired_reminders=self.LINE, ci_selection='*CI selection:* ok',
                              remote_boxes='*Remote boxes:* ok', pace='| a | b |\n| c | d |',
                              memory='*Claude RSS:* 1 GB', opencode='*OpenCode Go:* 0')
        self.assertIn(self.LINE, hourly.splitlines())

    def test_collector_is_registered_hourly_and_never_served_from_cache(self):
        args = SimpleNamespace(slots_json=None, decisions_json='d', agents_json='a', axiom_json='x', prs_json='p',
                               active_json='ac', map_json='mp', no_active_users=False, mode='1h', sample_usage_claim=None)
        seen = {}
        with patch.object(m, 'parallel_collect', lambda jobs, **kw: seen.update(jobs) or ({}, {})):
            m.collect_hourly(args, 'python3', '1', self.NOW, [])
        self.assertIn('reminders', seen)
        unavailable = f'{LABEL} listing unavailable'
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / 'reminders.json').write_text(json.dumps({'at': time.time(), 'value': f'{LABEL} none'}))
            results, stale = m.parallel_collect({'reminders': lambda: unavailable}, wall_cap=5, cache_dir=cache)
            self.assertEqual(results['reminders'], unavailable)
            self.assertEqual(stale, {})

            def raises():
                raise TimeoutError('timed out')
            results, stale = m.parallel_collect({'reminders': raises}, wall_cap=5, cache_dir=cache)
            self.assertIsNone(results['reminders'])  # compose renders "listing unavailable (no result)", never the cached "none"
            self.assertEqual(stale, {})


if __name__ == '__main__':
    unittest.main()
