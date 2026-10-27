"""Mixed-state fixtures and the production Slack success callback."""
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

ASSETS = Path(__file__).resolve().parents[1] / 'shared-assets/claude'
SCRIPT = ASSETS / 'scripts/pm/heartbeat/pr-status-report.py'
spec = importlib.util.spec_from_file_location('pr_status_report', SCRIPT)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)
NOW = dt.datetime(2026, 10, 27, 10, tzinfo=dt.timezone.utc)


def fixture():
    prs, checks = [], {}
    # draft, not admitted, pending, CI red, E2E red, green, blocked, stale label
    for number in range(1, 9):
        labels = [] if number == 2 else [{'name': 'ci-head:head'}]
        if number == 7:
            labels.append({'name': 'pm-blocked:dependency'})
        if number == 8:
            labels = [{'name': 'ci-head:old'}]
        prs.append(dict(number=number, headRefOid='head', isDraft=number == 1,
                        labels=labels, mergeable='CONFLICTING' if number == 8 else 'MERGEABLE'))
        checks[number] = [dict(id=1, name='test', head_sha='head', status='completed', conclusion='success'),
                          dict(id=2, name='e2e', head_sha='head', status='completed', conclusion='success')]
    checks[3][0].update(status='in_progress', conclusion=None)
    checks[4][0]['conclusion'] = 'failure'
    checks[5][1]['conclusion'] = 'failure'
    return prs, checks


def test_mixed_fixture():
    prs, checks = fixture()
    obligations = [dict(pr=n, owner='repair-agent', suppress_until='2026-10-27T12:00:00Z') for n in range(1, 9)]
    lines, ages = report.render(prs, checks, [dict(slot=2, pr=1, issue=123)], obligations,
                               {'6': {'gate': 'dual-green/merge ask', 'since': '2026-10-27T05:00:00Z'}}, NOW)
    assert len(lines) == 8
    for line, expected in zip(lines, ['draft-rework', '12b', 'CI running', 'CI red', 'E2E red',
                                     'dual-green/merge ask', 'PARKED', 'ci-head stale']):
        assert expected in line
    assert 'S2' in lines[0]
    assert 'STUCK' in lines[5]
    assert 'resolve conflicts' in lines[7]
    assert ages['6']['since'] == '2026-10-27T05:00:00+00:00'
    assert all('MOVING' in line for line in lines[:5])


def test_unowned_expired_and_wrong_head():
    prs, checks = fixture()
    checks[6][1]['head_sha'] = 'old'
    checks[6].append(dict(id=0, name='test', head_sha='head', status='completed', conclusion='failure'))
    lines, _ = report.render([prs[5]], checks, [], [dict(pr=6, owner='expired', suppress_until='2026-10-26T00:00:00Z')], {}, NOW)
    assert 'CI running' in lines[0] and 'E2E MISSING' in lines[0]
    assert 'unowned' in lines[0] and 'STUCK' in lines[0]


def test_split_preserves_every_line():
    lines = [f'#{n} ready · gate · owner · next · MOVING' for n in range(500)]
    parts = report.chunks(lines, limit=1500)
    assert len(parts) > 1
    actual = [line for part in parts for line in part.splitlines() if line.startswith('#')]
    assert actual == lines
    assert all(len(part) <= 1500 for part in parts)


def test_real_sender_callback_only_hourly_alert_parent():
    sender = (ASSETS / 'skills/slack-message/scripts/slack-send.sh').read_text()
    callback = sender.split('SLACK_SEND_PAYLOAD="$PAYLOAD" python3 -c "', 1)[1].split('" <<< "$RESULT"', 1)[0]
    callback = callback.replace('\\"', '"')
    for identity, text, thread, success, expected in [
        ('alerts', '*Hourly — 10:00 IST*', None, True, True),
        ('alerts', '<@U0BNFGX2UAX> *Hourly — 10:00 IST*', None, True, True),
        ('alerts', '*Hourly — 10:00 IST*', '123.1', True, False),
        ('alerts', '*Heartbeat — 10:00 IST*', None, True, False),
        ('pm', '*Hourly — 10:00 IST*', None, True, False),
        ('alerts', '*Hourly — 10:00 IST*', None, False, False),
    ]:
        payload = dict(text=text, thread_ts=thread)
        result = dict(ok=success, ts='123.456', channel='C0ALZJHGE49')
        with patch.dict(os.environ, {'SLACK_SEND_AS': identity, 'SLACK_SEND_PAYLOAD': json.dumps(payload)}), \
                patch('sys.stdin', __import__('io').StringIO(json.dumps(result))), \
                patch('subprocess.run') as run:
            run.return_value.returncode = 0
            try:
                exec(compile(callback, '<production-slack-callback>', 'exec'), {})
            except SystemExit:
                assert not success
            assert run.called == expected
            if expected:
                assert run.call_args.args[0][-2:] == ['--thread', '123.456']
