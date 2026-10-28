"""Mixed-state fixtures and the production Slack success callback."""
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import sys
import subprocess
import pytest
from unittest.mock import patch

ASSETS = Path(os.environ.get('MOP_REPORT_PROOF_ASSETS',
    Path(__file__).resolve().parents[1] / 'shared-assets/claude'))
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
    obligations = [dict(pr=n, target_type='github_pr', target_id=str(n), owner='repair-agent', suppress_until='2026-10-27T12:00:00Z', evidence_json=json.dumps({'head': 'head'})) for n in (1, 2, 4, 5)]
    lines, ages = report.render(prs, checks, [dict(slot=2, pr=1, issue=123, occupied=True, head_sha='head')], obligations,
                               {'6': {'gate': 'dual-green/merge ask', 'since': '2026-10-27T05:00:00Z'}}, NOW)
    assert len(lines) == 8
    for line, expected in zip(lines, ['draft-rework', '12b', 'CI running', 'CI red', 'E2E red',
                                     'dual-green/merge ask', 'PARKED', 'ci-head stale']):
        assert expected in line
    assert 'S2' in lines[0]
    assert 'STUCK' in lines[5]
    assert 'resolve conflicts' in lines[7]
    assert ages['6']['since'] == '2026-10-27T05:00:00+00:00'
    assert all('OWNED' in lines[i] for i in (0, 1, 3, 4))
    assert 'WAITING' in lines[2]


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


def test_pending_ci_lane_waits_without_claiming_movement():
    prs, checks = fixture()
    for prior, expected in [({}, 'WAITING'), ({'3': {'gate': 'CI running', 'since': '2026-10-27T05:00:00Z'}}, 'STUCK')]:
        lines, _ = report.render([prs[2]], checks, [], [], prior, NOW)
        assert 'CI (admitted head)' in lines[0]
        assert expected in lines[0]


@pytest.mark.parametrize('slot', [
    dict(slot=2, pr=None, occupied=False, head_sha='head', checkout_branch='feature/live-work'),
    dict(slot=2, pr=99, occupied=True, head_sha='head', checkout_branch='feature/live-work'),
    dict(slot=2, pr=1, occupied=True, head_sha='old', checkout_branch='feature/live-work'),
])
def test_stale_checkout_cannot_own_pr(tmp_path, monkeypatch, capsys, slot):
    prs, checks = fixture()
    prs[0]['headRefName'] = 'feature/live-work'
    monkeypatch.setattr(report, 'STATE', tmp_path / 'state.json')
    monkeypatch.setattr(report, 'collect', lambda: ([prs[0]], checks, [slot], []))
    monkeypatch.setattr(sys, 'argv', ['report'])
    report.main()
    line = capsys.readouterr().out
    assert 'unowned' in line and 'STUCK' in line and 'S2' not in line
    assert 'draft-rework' in line  # draft takes precedence over CI


def test_exact_head_current_slot_is_owned_not_assumed_moving():
    prs, checks = fixture()
    lines, _ = report.render([prs[0]], checks,
                            [dict(slot=1, pr=1, occupied=True, head_sha='head')], [], {}, NOW)
    assert 'S1' in lines[0] and 'OWNED' in lines[0] and 'MOVING' not in lines[0]


@pytest.mark.parametrize('target,expected', [('99', 'unowned'), ('1', 'repair-agent')])
def test_obligation_must_match_pr_target(target, expected):
    prs, checks = fixture()
    rows = [dict(pr=1, target_type='github_pr', target_id=target, owner='repair-agent', suppress_until='2026-10-27T12:00:00Z',
                 evidence_json=json.dumps({'head': 'head'}))]
    lines, _ = report.render([prs[0]], checks, [], rows, {}, NOW)
    assert expected in lines[0] and 'MOVING' not in lines[0]


def test_queued_ci_is_not_observed_progress(tmp_path, monkeypatch, capsys):
    prs, checks = fixture()
    checks[3][0]['status'] = 'queued'
    monkeypatch.setattr(report, 'STATE', tmp_path / 'state.json')
    monkeypatch.setattr(report, 'collect', lambda: ([prs[2]], checks, [], []))
    monkeypatch.setattr(sys, 'argv', ['report'])
    report.main()
    line = capsys.readouterr().out
    assert 'QUEUED' in line and 'MOVING' not in line


def test_current_head_merge_ask_transfers_owner_to_cto():
    prs, checks = fixture()
    for evidence, expected in [('head', 'CTO (merge ask sent)'), ('old', 'unowned')]:
        rows = [dict(pr=6, kind='pr_merge_ask', evidence_json=json.dumps({'head': evidence}))]
        lines, _ = report.render([prs[5]], checks, [], rows, {}, NOW)
        assert expected in lines[0]
        assert ('WAITING' if evidence == 'head' else 'STUCK') in lines[0]
    prs[5]['merge_ask_sent'] = True
    lines, _ = report.render([prs[5]], checks, [], [], {}, NOW)
    assert 'CTO (merge ask sent)' in lines[0]


def test_post_has_one_pr_per_block_and_explicit_multiline_fallback():
    text = report.chunks(['#1 ready · MOVING', '#2 draft · MOVING'])[0]
    with patch('subprocess.run') as run:
        run.return_value.stdout = 'OK ts=123.4 channel=C0ALZJHGE49'
        assert report.post('C0ALZJHGE49', '123.0', text) == '123.4'
        argv = run.call_args.args[0]
        blocks = json.loads(argv[argv.index('-b') + 1])
        assert [b['text']['text'] for b in blocks][1:] == ['#1 ready · MOVING', '#2 draft · MOVING']
        assert '\n\n#1' in run.call_args.kwargs['input']


def test_lost_slack_response_stops_restart_without_duplicate(tmp_path, monkeypatch):
    prs, checks = fixture()
    state = tmp_path / 'state.json'
    monkeypatch.setattr(report, 'STATE', state)
    monkeypatch.setattr(report, 'collect', lambda: (prs, checks, [], []))
    monkeypatch.setattr(sys, 'argv', ['report', '--thread', '123.0'])
    accepted = []

    def lost_response(*args, **kwargs):
        saved = json.loads(state.read_text())
        assert saved['delivered']['C0ALZJHGE49:123.0']['attempt']['index'] == 0
        accepted.append(kwargs['input'])  # Slack accepted it, response was lost
        raise __import__('subprocess').TimeoutExpired('slack-send', 120)

    with patch('subprocess.run', side_effect=lost_response):
        with pytest.raises(__import__('subprocess').TimeoutExpired):
            report.main()
    # Reload the production entry point, as a real new process would.
    restarted = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(restarted)
    monkeypatch.setattr(restarted, 'STATE', state)
    monkeypatch.setattr(restarted, 'collect', lambda: (prs, checks, [], []))
    with patch('subprocess.run') as send:
        with pytest.raises(RuntimeError, match='UNCERTAIN'):
            restarted.main()
        send.assert_not_called()
    assert len(accepted) == 1


def test_confirmed_parts_reused_after_restart(tmp_path, monkeypatch):
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'delivered': {'C0ALZJHGE49:123.0': {
        'parts': ['123.1'], 'messages': ['already posted', 'remaining'], 'delivery_version': 1}}}))
    monkeypatch.setattr(report, 'STATE', state)
    monkeypatch.setattr(report, 'collect', lambda: ([], {}, [], []))
    monkeypatch.setattr(sys, 'argv', ['report', '--thread', '123.0'])
    with patch('subprocess.run') as send:
        send.return_value.stdout = 'OK ts=123.2'
        report.main()
        assert send.call_count == 1 and send.call_args.kwargs['input'] == 'remaining'
        report.main()
        assert send.call_count == 1
    receipt = json.loads(state.read_text())['delivered']['C0ALZJHGE49:123.0']
    assert receipt['parts'] == ['123.1', '123.2'] and receipt['done']
    assert 'attempt' not in receipt


def test_twenty_two_rows_preserve_block_layout():
    prs, checks = fixture()
    prs = [dict(prs[i % 8], number=i + 1) for i in range(22)]
    lines, _ = report.render(prs, checks, [], [], {}, NOW)
    assert len(lines) == 22
    text = report.chunks(lines)[0]
    with patch('subprocess.run') as send:
        send.return_value.stdout = 'OK ts=123.4'
        report.post('C0ALZJHGE49', '123.0', text)
    argv = send.call_args.args[0]
    blocks = json.loads(argv[argv.index('-b') + 1])
    assert len(blocks) == 23
    assert [b['text']['text'] for b in blocks][1:] == lines


def entry_fixture(tmp_path):
    """Actual reporter main and Bash sender; only external boundaries are synthetic."""
    home = tmp_path / 'home'
    scripts = home / '.claude/scripts'
    scripts.mkdir(parents=True)
    environment = tmp_path / 'fixture.env'
    environment.write_text('SLACK_BOT_TOKEN=fixture-only\nSLACK_ALERTS_BOT_TOKEN=fixture-only\n')
    sender = (ASSETS / 'skills/slack-message/scripts/slack-send.sh').read_text()
    # Redirect its one hardcoded credential dependency, never read production env.
    old = 'PM_ENV="/Users/rajiv/Downloads/projects/heydonna-app/.env.local"'
    assert sender.count(old) == 1
    actual = tmp_path / 'actual-slack-send.sh'
    actual.write_text(sender.replace(old, f'PM_ENV="{environment}"'))
    (scripts / 'slack-send.sh').write_text(f'''#!/bin/bash
if [ "${{CASE_FAULT:-}}" = stdout-loss ] && [ -f "$CASE_LOG" ]; then
  exec bash '{actual}' "$@" >/dev/null
fi
exec bash '{actual}' "$@"
''')
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    curl = binaries / 'curl'
    curl.write_text(f'''#!{sys.executable}
import json,os,sys
from pathlib import Path
assert sys.argv[-3] == '-d' or '-d' in sys.argv
assert 'https://slack.com/api/chat.postMessage' in sys.argv
p=json.loads(sys.argv[sys.argv.index('-d')+1])
log=Path(os.environ['CASE_LOG'])
rows=log.read_text().splitlines() if log.exists() else []
state=json.loads((Path.home()/'.claude/state/pr-status-report.json').read_text())
receipt=state['delivered']['C_TEST:123.0']
index=len(rows)
attempt=receipt.get('attempt',{{}})
with log.open('a') as f:
 f.write(json.dumps({{'payload':p,'attempt_before_send':'attempted' if attempt.get('index')==index else None}})+'\\n');f.flush();os.fsync(f.fileno())
print(json.dumps({{'ok':True,'ts':f'321.{{len(rows)+1:06d}}','channel':p['channel']}}))
''')
    curl.chmod(0o755)
    data = tmp_path / 'input.json'
    prs = [dict(number=n, headRefOid='head', headRefName=f'pr-{n}', isDraft=False,
                labels=[], mergeable='MERGEABLE') for n in range(1, 42)]
    data.write_text(json.dumps(prs))
    driver = tmp_path / 'reporter-entry.py'
    driver.write_text('''import importlib.util,json,os,sys
from pathlib import Path
s=importlib.util.spec_from_file_location('entry_report',os.environ['CASE_REPORT'])
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
m.collect=lambda:(json.loads(Path(os.environ['CASE_INPUT']).read_text()),{},[],[])
real=m.post
calls=0
def post(*args):
 global calls
 result=real(*args);calls+=1
 if os.environ.get('CASE_FAULT')=='crash' and calls==2: os._exit(87)
 return result
m.post=post
m.main()
''')
    log = tmp_path / 'accepted.jsonl'
    env = {**os.environ, 'HOME': str(home), 'PATH': str(binaries) + os.pathsep + os.environ['PATH'],
           'CASE_INPUT': str(data), 'CASE_REPORT': str(SCRIPT), 'CASE_LOG': str(log),
           'BRIDGE_DB': str(tmp_path / 'never-production.db')}
    def invoke(fault=''):
        return subprocess.run([sys.executable, str(driver), '--channel', 'C_TEST', '--thread', '123.0'],
                              cwd=tmp_path, env={**env, 'CASE_FAULT': fault},
                              text=True, capture_output=True, timeout=20)
    return invoke, log, home / '.claude/state/pr-status-report.json'


@pytest.mark.parametrize('fault,code', [('stdout-loss', 1), ('crash', 87)])
def test_actual_entries_accepted_second_part_response_loss_and_restart(tmp_path, fault, code):
    invoke, log, ledger = entry_fixture(tmp_path)
    first = invoke(fault)
    before = json.loads(ledger.read_text())['delivered']['C_TEST:123.0']
    again = invoke()
    rows = [json.loads(x) for x in log.read_text().splitlines()]
    assert len(rows) == 2
    assert first.returncode == code, first.stderr
    # Real sender accepted both parts; only the first has a durable confirmation.
    assert before['parts'] == ['321.000001']
    assert before['attempt']['index'] == 1
    assert again.returncode == 1 and 'UNCERTAIN' in again.stderr
    assert all(r['attempt_before_send'] == 'attempted' for r in rows)
    assert json.loads(ledger.read_text())['delivered']['C_TEST:123.0']['parts'] == before['parts']


def test_actual_entries_confirm_all_rows_and_never_repeat_completed_thread(tmp_path):
    invoke, log, ledger = entry_fixture(tmp_path)
    first = invoke()
    assert first.returncode == 0, first.stderr
    assert invoke().returncode == 0
    rows = [json.loads(x) for x in log.read_text().splitlines()]
    assert len(rows) == 2
    visible = [line for row in rows for line in row['payload']['text'].splitlines() if line.startswith('#')]
    assert [int(line.split()[0][1:]) for line in visible] == list(range(1, 42))
    for row in rows:
        assert row['payload']['thread_ts'] == '123.0'
        assert len([b for b in row['payload']['blocks'] if b['text']['text'].startswith('#')]) in (40, 1)
    saved = json.loads(ledger.read_text())['delivered']['C_TEST:123.0']
    assert saved['done'] and len(saved['parts']) == 2


def test_legacy_incomplete_receipt_retains_confirmed_parts_without_send(tmp_path):
    invoke, log, ledger = entry_fixture(tmp_path)
    ledger.parent.mkdir(parents=True)
    receipt = {'parts': ['321.000001'], 'messages': ['confirmed', 'possibly sent']}
    ledger.write_text(json.dumps({'delivered': {'C_TEST:123.0': receipt}}))
    result = invoke()
    assert result.returncode == 1 and 'UNCERTAIN' in result.stderr
    assert not log.exists()
    assert json.loads(ledger.read_text())['delivered']['C_TEST:123.0']['parts'] == receipt['parts']


def test_legacy_accepted_response_loss_is_not_replayed(tmp_path):
    invoke, log, ledger = entry_fixture(tmp_path)
    assert invoke('stdout-loss').returncode == 1
    # Encode the exact pre-fix receipt shape after a known accepted second part.
    # These are synthetic files; no real receipt or production state is changed.
    saved = json.loads(ledger.read_text())
    receipt = saved['delivered']['C_TEST:123.0']
    receipt.pop('attempt', None)
    receipt.pop('delivery_version', None)
    ledger.write_text(json.dumps(saved))
    restarted = invoke()
    assert len(log.read_text().splitlines()) == 2
    assert restarted.returncode == 1 and 'UNCERTAIN' in restarted.stderr
    assert json.loads(ledger.read_text())['delivered']['C_TEST:123.0']['parts'] == ['321.000001']


def test_native_draft_false_precedes_legacy_true():
    prs, checks = fixture()
    prs[0].update(isDraft=False, draft=True)
    lines, _ = report.render(prs, checks, [], [], {}, NOW)
    assert '#1 ready' in lines[0] and 'draft-rework' not in lines[0]
