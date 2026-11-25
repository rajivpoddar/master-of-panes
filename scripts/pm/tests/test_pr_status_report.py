"""Mixed-state fixtures and the production Slack success callback."""
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
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
                               {'6': {'head': 'head', 'gate': 'dual-green/merge ask', 'since': '2026-10-27T05:00:00Z'}}, NOW)
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
    assert 'unowned' in lines[0] and lines[0].endswith('· OPEN')


def launch_log(name, pr, time='09:00:00'):
    return f'[2026-10-27T{time}Z] LAUNCH name={name} ISSUE:999 PR:{pr} model_alias=auto MODE:dev\n'


def test_collect_renders_each_process_bound_proxy_and_latest_12b(tmp_path, monkeypatch):
    prs, _ = fixture()
    head = 'a' * 40
    cases = [('pm-admission-reviewer', '12b'), ('hd-proof-qa', 'QA'), ('hd-builder', 'build/rework')]
    selected = prs[:len(cases)]
    processes, by_pr = [], {}
    for pr, (role, stage) in zip(selected, cases):
        number = pr['number']
        lane = f'proxy-{role}-20261027T090000Z-{number}'
        pid = number + 100
        processes.append(f'{pid} /usr/local/bin/claude -p --name {lane} --model auto')
        lane_dir = tmp_path / lane
        lane_dir.mkdir()
        (lane_dir / 'manifest.json').write_text(json.dumps(dict(
            name=lane, pid=pid, state='running', type=role, pr=number,
            started_at='2026-10-27T09:00:00Z', running_at=NOW.isoformat())))
        pr.update(headRefOid=head, isDraft=False, labels=[], comments=[
            {'body': f'## PM 12b: REVISE at {head}', 'createdAt': '2026-10-27T09:10:00Z'},
            {'body': f'PM 12b: PASS at {head}', 'createdAt': '2026-10-27T08:10:00Z'},
        ])
        by_pr[number] = (pr, f'owner={lane} · {stage}')
    db = tmp_path / 'pm.db'
    with sqlite3.connect(db) as con:
        con.execute('CREATE TABLE obligations (id, pr, target_type, target_id, owner, '
                    'suppress_until, kind, evidence_json, status)')
    monkeypatch.setattr(report, 'DB', db)
    monkeypatch.setattr(report, 'HOME', tmp_path)
    monkeypatch.setattr(report, 'PROXY_RUN_DIR', tmp_path)
    commands = dict(row.split(maxsplit=1) for row in processes)
    monkeypatch.setattr(report, 'process_identity',
                        lambda pid: ('9', NOW - dt.timedelta(minutes=30), commands[pid]))
    monkeypatch.setattr(report.urllib.request, 'urlopen', lambda *a, **kw: io.StringIO('{"slots": []}'))
    monkeypatch.setattr(report, 'gh', lambda *args: [[{'number': n} for n in by_pr]]
                        if args[0] == 'api' else by_pr[int(args[2])][0])
    monkeypatch.setattr(report, 'fetch_comments', lambda number: by_pr[number][0]['comments'])
    monkeypatch.setattr(report, 'fetch_checks', lambda *_: [])
    with patch.object(report.subprocess, 'run', return_value=subprocess.CompletedProcess(
            [], 0, '\n'.join(processes), '')) as run:
        inputs = report.collect()
        lines, _ = report.render(*inputs, {str(pr['number']): {'gate': '12b', 'since': '2026-10-26T13:00:00Z'}
                                          for pr in selected}, NOW)
    assert run.call_args.args[0] == ['pgrep', '-fl', 'claude -p --name ']
    assert run.call_count == 1
    assert len(lines) == len(by_pr)
    for line in lines:
        number = int(line.split()[0][1:])
        assert f'· {by_pr[number][1]} · PM 12b: REVISE at {head[:12]}' in line
        assert line.endswith('· OWNED') and 'unowned' not in line


def test_proxy_logs_and_completed_markers_cannot_claim_without_live_pr_binding(tmp_path):
    dead = 'proxy-hd-proof-qa-20261027T090000Z-1'
    live = 'proxy-hd-builder-20261027T090000Z-2'
    (tmp_path / f'{dead}.log').write_text(launch_log(dead, 1))
    (tmp_path / f'{live}.log').write_text(launch_log(live, 1))
    (tmp_path / f'hd-builder-{live}-3-20261027T093000Z.log').write_text(
        launch_log(live, 'UNKNOWN', '09:30:00') + 'Later output references PR:1\n')
    with (patch.object(report.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0,
                        f'10 /bin/sh -c claude -p --name {dead}\n11 /bin/claude -p --name {live}\n', '')),
          patch.object(report, 'process_identity', side_effect=[
              ('9', NOW, f'/bin/claude -p --name {live}'), ('1', NOW, '/bin/bash other.sh')])):
        proxies = report.live_proxy_lanes(tmp_path)
    assert proxies == {}
    prs, checks = fixture()
    pr = dict(prs[0], headRefOid='a' * 40, live_proxy=proxies.get(1), comments=[
        {'body': 'PM 12b: PASS at bbbbbbb'},
        {'body': '> PM 12b: BLOCK at aaaaaaa'},
    ])
    lines, _ = report.render([pr], checks, [], [], {}, NOW)
    assert '· unowned · PM 12b: PASS at bbbbbbb (different head)' in lines[0]
    assert lines[0].endswith('· OPEN')


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
    for prior, expected in [({}, 'WAITING'), ({'3': {'head': 'head', 'gate': 'CI running',
                                                    'since': '2026-10-27T05:00:00Z'}}, 'STUCK')]:
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
    assert 'unowned' in line and 'S2' not in line
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
        assert lines[0].endswith('· WAITING' if evidence == 'head' else '· OPEN')
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
        assert [b['text']['text'] for b in blocks][1:] == ['#1 ready · MOVING', '#2 draft · MOVING', report.FOOTER]
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
    assert len(blocks) == len(lines) + 2
    assert [b['text']['text'] for b in blocks][1:] == [*lines, report.FOOTER]


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


@pytest.mark.parametrize('count', [3, 19, 53])
def test_delta_partitions_generated_prs_and_retains_every_baseline_row(count):
    original = [dict(number=n, headRefOid='a' * 40, isDraft=False, labels=[], mergeable='MERGEABLE',
                     comments=[], live_proxy=(f'proxy-build-{n}', 'build/rework')) for n in range(1, count + 1)]
    _, prior = report.render(original, {}, [], [], {}, NOW)
    prior = json.loads(json.dumps(prior))  # compare the same data actually read back next hour
    incoming, checks, obligations, expected = [], {}, [], set()
    for pr in original:
        n = pr['number']
        updated = dict(pr)
        if n % 7 == 0:
            updated['headRefOid'] = 'b' * 40
        elif n % 7 == 1:
            updated['labels'] = [{'name': 'pm-blocked:dependency'}]
        elif n % 7 == 2:
            checks[n] = [dict(head_sha=pr['headRefOid'], id=n, name='e2e', status='completed', conclusion='success')]
        elif n % 7 == 3:
            updated['comments'] = [dict(id=n, body=f'PM 12b: REVISE at {pr["headRefOid"]}')]
        elif n % 7 == 4:
            updated['merge_ask_sent'] = True
        elif n % 7 == 5:
            obligations.append(dict(pr=n, kind='merge_hold', id=n))
        if n % 7 != 6:
            expected.add(n)
        incoming.append(updated)
    lines, baseline = report.render(incoming, checks, [], obligations, prior, NOW + dt.timedelta(hours=1))
    assert {int(line.split()[0][1:]) for line in lines if line.startswith('#')} == expected
    assert all(line.endswith('· OWNED') for line in lines if line.startswith('#'))
    assert set(baseline) == {str(pr['number']) for pr in original}
    if count > len(expected):
        assert lines[-1] == f'{count - len(expected)} unchanged and owned'
    same, _ = report.render(incoming, checks, [], obligations, baseline, NOW + dt.timedelta(hours=2))
    assert same == [f'{count} unchanged and owned']


@pytest.mark.parametrize('case,shown', [
    ('dual-green', True), ('red', True), ('red-owned', False), ('cap', True),
    ('stuck', True), ('stuck-owned', False), ('draft-idle', True),
    ('draft-idle-owned', False), ('draft-recent-comment', True), ('unowned-admission', True),
    ('waiting', False), ('parked', False),
])
def test_unchanged_action_thresholds(case, shown):
    head = 'a' * 40
    pr = dict(number=121, headRefOid=head, isDraft=case.startswith('draft'), labels=[], comments=[])
    runs = []
    if case == 'dual-green' or case.startswith('red'):
        pr['labels'] = [{'name': f'ci-head:{head}'}]
        runs = [dict(head_sha=head, id=i, name=name, status='completed', conclusion=conclusion)
                for i, (name, conclusion) in enumerate((('test', 'success'),
                    ('e2e', 'success' if case == 'dual-green' else 'failure')), 1)]
    if case.endswith('-owned'):
        pr['live_proxy'] = ('proxy-rw-121', 'rework')
    if case == 'cap':
        pr['review_cap'] = '12b'
    if pr['isDraft']:
        pr['tip_at'] = (NOW - dt.timedelta(hours=25)).isoformat()
    if case == 'draft-recent-comment':
        pr['comments'] = [{'created_at': NOW.isoformat(), 'body': 'Working on this'}]
    if case == 'waiting':
        pr['labels'] = [{'name': f'ci-head:{head}'}]
        runs = [dict(head_sha=head, id=1, name='test', status='in_progress')]
    obligations = [dict(pr=pr['number'], kind='merge_hold', id=1)] if case == 'parked' else []
    checks = {pr['number']: runs}
    _, prior = report.render([pr], checks, [], obligations, {}, NOW)
    later = NOW + dt.timedelta(hours=5 if case.startswith('stuck') else 1)
    lines, _ = report.render([pr], checks, [], obligations, prior, later)
    assert lines[0].startswith(f'#{pr["number"]} ') is shown
    assert (lines == ['1 unchanged and owned']) is (not shown and 'live_proxy' in pr)
    if case == 'unowned-admission':
        assert 'unowned' in lines[0] and 'review/admit exact head' in lines[0]


@pytest.mark.parametrize('move', ['none', 'head', 'gate', 'both'])
def test_gate_age_survives_only_the_same_head_and_gate(move):
    pr = dict(number=121, headRefOid='a' * 40, isDraft=False, labels=[], comments=[])
    _, prior = report.render([pr], {}, [], [], {}, NOW)
    if move in ('head', 'both'):
        pr['headRefOid'] = 'b' * 40
    if move in ('gate', 'both'):
        pr['labels'] = [{'name': f'ci-head:{pr["headRefOid"]}'}]
    later = NOW + dt.timedelta(hours=5)
    with patch.object(report, 'pending_ci', return_value=False):
        lines, ages = report.render([pr], {}, [], [], prior, later)
    assert ages[str(pr['number'])]['since'] == (NOW if move == 'none' else later).isoformat()
    assert lines[0].endswith('· STUCK') is (move == 'none')


def test_named_lanes_require_a_live_process_and_explicit_pr(tmp_path):
    names = ['proxy-rw-short', 'px-builder.custom', 'proxy-dead', 'proxy-1234']
    for pid, name in enumerate(names, 10):
        lane = tmp_path / name
        lane.mkdir()
        brief = lane / 'brief.md'
        brief.write_text('PR: #7234\n' if pid == 11 else 'ISSUE: 7234\n')
        manifest = dict(name=name, pid=pid, type='hd-builder', state='running',
                        started_at=NOW.isoformat(), running_at=NOW.isoformat(), brief=str(brief))
        if pid in (10, 12):
            manifest['pr'] = '7233' if pid == 10 else '7235'
        (lane / 'manifest.json').write_text(json.dumps(manifest))
    commands = {str(pid): f'/bin/claude -p --name {names[pid-10]} --model test' for pid in (10, 11, 13)}
    process_rows = '\n'.join(f'{pid} {command}' for pid, command in commands.items())
    process_rows += f'\n12 /bin/sh -c claude -p --name {names[2]}\n'
    with (patch.object(report.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, process_rows)),
          patch.object(report, 'process_identity', side_effect=lambda pid: ('9', NOW, commands.get(pid, 'bash')))):
        owners = report.live_proxy_lanes(tmp_path)
    assert owners == {7233: (names[0], 'build/rework'), 7234: (names[1], 'build/rework')}


@pytest.mark.parametrize('case,owns', [
    ('reused-name', False), ('recycled-pid', False), ('legacy-live', True),
    ('continuation', True), ('unknown-pr', False),
])
def test_launch_header_fallback_requires_live_process_and_runner_binding(tmp_path, case, owns):
    name, pid, pr = 'proxy-my-rework', '20', 123
    log = tmp_path / f'hd-builder-{name}-11-20261027T090000Z.log'
    log.write_text(launch_log(name, 'UNKNOWN' if case == 'unknown-pr' else pr)
                   + 'Later output references PR:123\n')
    if case in ('continuation', 'recycled-pid'):
        lane_dir = tmp_path / name
        lane_dir.mkdir()
        (lane_dir / 'manifest.json').write_text(json.dumps(dict(
            name=name, pid=int(pid) if case == 'recycled-pid' else 19, pr=pr,
            state='running', started_at='2026-10-27T09:00:00Z', running_at='2026-10-27T09:00:01Z',
            out=str(log.with_suffix('.md')))))
    command = f'/bin/claude -p --name {name}' + (' --resume test-session' if case == 'continuation' else '')
    file = log.stat()
    descriptor = f'p{pid}\nf2\ntREG\nD{file.st_dev:#x}\ni{file.st_ino}\nn{log}\n'

    def run(args, **_):
        executable = args[0]
        if executable == 'pgrep':
            stdout = f'{pid} {command}'
        elif executable == 'ps':
            if args[args.index('-p') + 1] == pid:
                stdout = f'14 Tue Oct 27 09:30:01 2026 {command}'
            else:
                start = '09:30:00' if case == 'recycled-pid' else '09:00:00'
                stdout = f'1 Tue Oct 27 {start} 2026 bash /tmp/proxy-agent-job.abc123/run.sh'
        else:
            assert executable == 'lsof'
            stdout = f'p{pid}\nf2\ntCHR\nn/dev/null\n' if case == 'reused-name' else descriptor
        return subprocess.CompletedProcess(args, 0, stdout)

    with patch.object(report.subprocess, 'run', side_effect=run):
        owners = report.live_proxy_lanes(tmp_path)
    assert owners == ({pr: (name, 'build/rework')} if owns else {})


@pytest.mark.parametrize('initial,retry,expected_calls,field', [
    ('MERGEABLE', None, 1, 'MERGEABLE'), ('UNKNOWN', 'CONFLICTING', 2, 'CONFLICTING'),
    ('UNKNOWN', 'UNKNOWN', 2, None), ('UNKNOWN', 'different-head', 2, None),
])
def test_mergeability_requery_once_and_never_print_unknown(initial, retry, expected_calls, field):
    head = 'a' * 40
    pr = dict(number=721, headRefOid=head, isDraft=False, labels=[], mergeable=initial)
    second = dict(headRefOid='b' * 40 if retry == 'different-head' else head,
                  mergeable='MERGEABLE' if retry == 'different-head' else retry)
    with patch.object(report, 'gh', side_effect=[pr, second]) as query:
        fetched = report.fetch_pr({'number': pr['number']})
    assert query.call_count == expected_calls and fetched.get('mergeable') == field
    lines, _ = report.render([fetched], {}, [], [], {}, NOW)
    assert 'UNKNOWN' not in lines[0]
    assert ('mergeable ' in lines[0]) is bool(field)


def test_disappeared_pr_requires_verified_merge_and_retains_failed_lookup():
    prior = {str(n): {'head': str(n)} for n in range(1, 5)}
    with patch.object(report, 'gh', side_effect=[{'state': 'MERGED', 'mergedAt': NOW.isoformat()},
                                                {'state': 'CLOSED'}, ValueError('unavailable')]):
        merged, unresolved = report.disappeared_prs(prior, [{'number': 4}])
    lines, _ = report.render([], {}, [], [], {}, NOW, merged=merged)
    assert lines == [f'#1 merged · {NOW.isoformat()}'] and unresolved == {'3': prior['3']}


def test_cap_uses_distinct_receipts_and_ignores_completed_qa(monkeypatch, tmp_path):
    monkeypatch.setattr(report, 'HOME', tmp_path)
    head = 'a' * 40
    qa_receipt = f'hd-proof-qa: REVISE at {head}'
    token = 'a' * 32
    draft = dict(number=1, headRefOid=head, isDraft=True, labels=[], comments=[
        {'id': n, 'body': f'{qa_receipt}\nREVIEW_ROUND_RECEIPT: {token}'} for n in (1, 2)])
    report.annotate_review_caps([draft])
    assert 'review_cap' not in draft
    draft['comments'].append({'id': 3, 'body': qa_receipt})
    report.annotate_review_caps([draft])
    assert draft.pop('review_cap') == 'qa'
    draft['comments'].append({'id': 4, 'created_at': NOW.isoformat(), 'body': f'hd-proof-qa: PASS at {head}'})
    report.annotate_review_caps([draft])
    assert 'review_cap' not in draft


def test_dry_run_keeps_previous_and_ends_with_pm_instruction(tmp_path, monkeypatch, capsys):
    state = tmp_path / 'state.json'
    state.write_text('{"gates": {}, "delivered": {}}')
    before = state.read_bytes()
    monkeypatch.setattr(report, 'STATE', state)
    monkeypatch.setattr(report, 'collect', lambda: ([], {}, [], []))
    monkeypatch.setattr(sys, 'argv', ['report', '--dry-run'])
    with patch.object(report, 'post') as post:
        report.main()
    post.assert_not_called()
    assert state.read_bytes() == before and not state.with_suffix('.lock').exists()
    assert capsys.readouterr().out.splitlines()[-1] == report.FOOTER


def test_native_draft_false_precedes_legacy_true():
    prs, checks = fixture()
    prs[0].update(isDraft=False, draft=True)
    lines, _ = report.render(prs, checks, [], [], {}, NOW)
    assert '#1 ready' in lines[0] and 'draft-rework' not in lines[0]


# CI/E2E admission rounds per row (Rajiv C0ALZJHGE49 thread 1795581894.679329, ts 1795584256.843699).
RUN_START = dt.datetime(2026, 11, 24, 10, tzinfo=dt.timezone.utc)
SHA_A = 'a' * 40


def rounds_run(run_id, workflow, *, attempt=1, conclusion='success', seconds=3600, numbers=(5,)):
    return {'id': run_id, 'event': 'pull_request', 'path': f'.github/workflows/{workflow}', 'status': 'completed',
            'conclusion': conclusion, 'run_attempt': attempt, 'head_sha': SHA_A, 'head_branch': 'feat',
            'created_at': RUN_START.isoformat(), 'run_started_at': RUN_START.isoformat(),
            'updated_at': (RUN_START + dt.timedelta(seconds=seconds)).isoformat(),
            'pull_requests': [{'number': n} for n in numbers]}


def verdict(run_id, classification, attempt=1):
    return {'id': run_id, 'created_at': RUN_START.isoformat(), 'body': (
        f'<!-- ci-verdict: {json.dumps({"run_id": run_id, "classification": classification})} -->\n'
        f'ci-failure-investigation:run={run_id} attempt={attempt} head={SHA_A}')}


def rounds_pr(comments=None):
    return {'number': 5, 'headRefName': 'feat', 'headRefOid': SHA_A, 'createdAt': '2026-11-24T09:00:00Z',
            'comments': comments or []}


def fake_items(ci_runs, e2e_runs, jobs=None):
    def fake(path, key, fields):
        if key == 'workflow_runs':
            return {'ci.yml': ci_runs, 'e2e.yml': e2e_runs}[path.split('/workflows/')[1].split('/')[0]]
        return (jobs or {})[int(path.split('/runs/')[1].split('/')[0])]
    return fake


def fetch_rounds(monkeypatch, pr, ci_runs, e2e_runs, jobs=None):
    monkeypatch.setattr(report, 'gh_items', fake_items(ci_runs, e2e_runs, jobs))
    return report.fetch_rounds(pr, RUN_START, report.time.monotonic() + 100)


def test_rounds_sum_attempts_across_heads_and_skip_noop_label_runs(monkeypatch):
    quick = [{'conclusion': 'success', 'started_at': RUN_START.isoformat(),
              'completed_at': (RUN_START + dt.timedelta(seconds=5)).isoformat()}]
    ci = [rounds_run(1, 'ci.yml'), rounds_run(2, 'ci.yml', attempt=2), rounds_run(3, 'ci.yml', seconds=30),
          rounds_run(4, 'ci.yml', numbers=(9,)), dict(rounds_run(5, 'ci.yml'), conclusion='skipped')]
    e2e = [rounds_run(6, 'e2e.yml')]
    got = fetch_rounds(monkeypatch, rounds_pr(), ci, e2e, jobs={3: quick})
    assert got == {'rounds': 1 + 2 + 1, 'due': False}  # run 3 is a gate-only no-op, 4 is another PR, 5 is skipped


@pytest.mark.parametrize('comments,runs,due', [
    ([verdict(11, 'flaky test'), verdict(12, 'infra: runner OOM')],
     [rounds_run(11, 'ci.yml', conclusion='failure'), rounds_run(12, 'e2e.yml', conclusion='failure')], True),
    ([verdict(11, 'flaky test'), verdict(12, 'PR-caused regression')],
     [rounds_run(11, 'ci.yml', conclusion='failure'), rounds_run(12, 'e2e.yml', conclusion='failure')], False),
    ([verdict(11, 'flaky test')],  # newest red has no verdict: not exempt
     [rounds_run(11, 'ci.yml', conclusion='failure'), rounds_run(12, 'e2e.yml', conclusion='failure')], False),
    ([verdict(11, 'flaky test')],  # only one red: below two
     [rounds_run(11, 'ci.yml', conclusion='failure'), rounds_run(12, 'e2e.yml')], False),
])
def test_admin_merge_due_needs_three_rounds_and_last_two_reds_exempt(monkeypatch, comments, runs, due):
    ci = [r for r in runs if 'ci.yml' in r['path']] + [rounds_run(13, 'ci.yml')]
    e2e = [r for r in runs if 'e2e.yml' in r['path']]
    assert fetch_rounds(monkeypatch, rounds_pr(comments), ci, e2e)['due'] is due


def test_two_rounds_are_never_due_even_when_both_reds_are_exempt(monkeypatch):
    comments = [verdict(11, 'flaky'), verdict(12, 'infra')]
    got = fetch_rounds(monkeypatch, rounds_pr(comments), [rounds_run(11, 'ci.yml', conclusion='failure')],
                       [rounds_run(12, 'e2e.yml', conclusion='failure')])
    assert got == {'rounds': 2, 'due': False}


def test_rounds_failure_is_unknown_never_zero(monkeypatch):
    def boom(*args, **kwargs):
        raise subprocess.CalledProcessError(1, 'gh')
    monkeypatch.setattr(report, 'gh_items', boom)
    assert report.fetch_rounds(rounds_pr(), RUN_START, report.time.monotonic() + 100) is None
    assert report.fetch_rounds(rounds_pr(), RUN_START, report.time.monotonic() - 1) is None  # budget spent
    prs, checks = fixture()
    prs[3]['rounds'] = None
    line = report.render([prs[3]], checks, [], [], {}, NOW)[0][0]
    assert 'rounds ?' in line and 'rounds 0' not in line


def test_row_shows_rounds_token_and_due_marker_only_while_the_head_has_a_red():
    prs, checks = fixture()  # prs[3] = CI red, prs[5] = dual-green
    prs[3]['rounds'] = {'rounds': 4, 'due': False}
    plain = report.render([prs[3]], checks, [], [], {}, NOW)[0][0]
    assert '; rounds 4]' in plain and 'admin-merge' not in plain
    prs[3]['rounds'] = {'rounds': 5, 'due': True}
    assert '; rounds 5 → admin-merge ask due]' in report.render([prs[3]], checks, [], [], {}, NOW)[0][0]
    prs[5]['rounds'] = {'rounds': 5, 'due': True}  # a dual-green head merges normally: no ask
    green = report.render([prs[5]], checks, [], [], {}, NOW)[0][0]
    assert '; rounds 5]' in green and 'admin-merge' not in green
