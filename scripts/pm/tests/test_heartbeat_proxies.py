import importlib.util
from datetime import datetime, timezone
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / 'shared-assets/claude/scripts/pm/heartbeat/heartbeat-compose.py'
spec = importlib.util.spec_from_file_location('heartbeat_proxies', SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_hourly_retains_all_proxies_sorted_and_drops_slot_actions():
    now = datetime(2026, 11, 13, tzinfo=timezone.utc)
    rows = [{'name': f'px-{n}', 'state': 'running', 'model': 'luna' if n % 2 else 'sol',
             'age': n, 'brief_refs': [f'issue:{n}']} for n in range(30)]
    slots = {'slots': [], 'lane_events': [{'slot': 1, 'issue': 99, 'threshold': 'overdue'}]}
    doc = {'rows': rows, 'completions': []}
    report = module.compose('1h', now, None, {}, [{'actions': ['review PR']}],
                            slots=slots, include_slots=True, priority_flags=['priority inversion'],
                            proxies=doc)
    assert '*Slots' not in report and 'priority inversion' not in report and 'overdue slot' not in report
    assert 'review PR' in report
    assert '30 live (luna: 15, sol: 15)' in report
    assert all(f'px-{n} · ' in report for n in range(30))
    assert report.index('px-29 · ') < report.index('px-0 · ')
    assert 'px-29 · luna · running 0h29m · #29' in report
    assert 'issue/PR UNKNOWN' not in report
    assert module.structured_pm_actions([{'actions': ['review PR']}], ['idle-slot'], slots, '1h', True) == ['review PR']


def test_proxy_report_reads_only_pm_obligations(tmp_path):
    import json
    import sqlite3
    now = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
    db = tmp_path / 'ops.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE obligations (kind TEXT, target_id TEXT, status TEXT, issue INTEGER, pr INTEGER, evidence_json TEXT, resolved_at TEXT)')
        conn.execute('INSERT INTO obligations VALUES (?, ?, ?, ?, ?, ?, ?)', ('proxy_run', 'px-first', 'open', 12345, None, json.dumps({'model': 'gpt-6-luna', 'started_at': '2026-11-13T10:48:00Z'}), None))
        conn.execute('INSERT INTO obligations VALUES (?, ?, ?, ?, ?, ?, ?)', ('proxy_run', 'px-missing', 'open', None, None, '{}', None))
        conn.execute('INSERT INTO obligations VALUES (?, ?, ?, ?, ?, ?, ?)', ('proxy_run', 'px-done', 'resolved', None, None, json.dumps({'rc': 1, 'result': 'done'}), '2026-11-13T11:45:00Z'))
        conn.execute('INSERT INTO obligations VALUES (?, ?, ?, ?, ?, ?, ?)', ('proxy_run', 'px-terminal', 'open', None, 54321, json.dumps({'rc': 0}), None))
        conn.execute('INSERT INTO obligations VALUES (?, ?, ?, ?, ?, ?, ?)', ('proxy_run', 'px-ended', 'open', None, None, json.dumps({'finished_at': '2026-11-13T11:59:00Z'}), None))
        conn.execute('INSERT INTO obligations VALUES (?, ?, ?, ?, ?, ?, ?)', ('proxy_run', 'px-unbound-98765', 'open', None, None, json.dumps({'model': 'gpt-6-luna', 'started_at': '2026-11-13T10:48:00Z'}), None))
        conn.execute('INSERT INTO obligations VALUES (?, ?, ?, ?, ?, ?, ?)', ('other', 'px-ignore', 'open', None, None, '{}', None))
    doc = module._read_proxies(now, db, liveness={'px-first': [72], 'px-unbound-98765': [12]})
    text = '\n'.join(module.proxies_block(doc))
    assert 'px-first · gpt-6-luna · running 1h12m · #12345' in text
    assert 'px-missing · ? · ? · elapsed ? · ?' in text
    assert 'px-unbound-98765 · gpt-6-luna · ? · elapsed 1h12m · ?' in text
    assert '#98765' not in text
    assert '1 live' in text and '2 liveness ?' in text
    assert all(name not in text for name in ('px-ignore', 'px-done', 'px-terminal', 'px-ended'))
    assert 'finished' not in text


def test_display_metadata_uses_bound_stream_timestamp_and_prompt(tmp_path, monkeypatch):
    now = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(module, 'PROXY_LOG_ROOT', tmp_path)
    stream = tmp_path / 'pm-hotfix-px-task-55-20261113T104800Z.jsonl'
    stream.write_text('{}\n')
    job = tmp_path / 'proxy-agent-job.test'
    job.mkdir()
    (job / 'prompt0.md').write_text('issue: 12345\nPR: #54321\nprivate text')
    processes = {101: (100, '01:12:00', f'python /x/proxy-agent-stream.py /tmp/out {stream}')}
    value = module._proxy_display_metadata('px-px-task', f'bash {job / "run.sh"}', 100, processes, now)
    assert value == {'runtime_minutes': 72, 'display_refs': ['12345', '54321']}
    assert 'display_refs' not in module._proxy_display_metadata('px-98765', '', 100, {}, now)
    assert 'display_refs' not in module._proxy_display_metadata('px-task', '', 100, {}, now)


def test_live_model_uses_exact_recent_log_or_bound_stream(tmp_path, monkeypatch):
    import json
    import os
    now = datetime(2026, 11, 13, tzinfo=timezone.utc)
    monkeypatch.setattr(module, 'PROXY_LOG_ROOT', tmp_path)
    log = tmp_path / 'px-task.launch.log'
    log.write_text('model=luna id=gpt-6-luna\n')
    os.utime(log, (now.timestamp() - 600, now.timestamp() - 600))
    assert module._live_proxy_model('px-px-task', 100, 10, {}, now) == ('gpt-6-luna', 'launch log')
    # A similarly named proxy and a stale log cannot certify this launch.
    assert module._live_proxy_model('px-task2', 100, 10, {}, now)[0] == 'UNAVAILABLE'
    stream = tmp_path / 'task.jsonl'
    stream.write_text(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'gpt-6.1-sol', 'padding': 'x' * 100000}) + '\n')
    processes = {101: (100, '00:10:00', f'python /x/proxy-agent-stream.py /tmp/out {stream}')}
    assert module._live_proxy_model('px-task', 100, 30, processes, now) == ('gpt-6.1-sol', 'live stream init')
    assert module._live_proxy_model('px-task', 200, 30, processes, now)[0] == 'UNAVAILABLE'
    assert 'no matching launch record' in module._live_proxy_model('px-task', 200, 30, processes, now)[1]


def test_proxy_tmux_ps_fixture(tmp_path):
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    out = tmp_path / 'task.md'
    live = tmp_path / 'task.jsonl'
    live.write_text('event')
    import os
    os.utime(live, (now.timestamp() - 21 * 60, now.timestamp() - 21 * 60))
    tmux = '0\tpx-task\t100\t0\n0\tpx-old\t200\t1\nproxies\tother\t300\t0\n'
    ps = (f'101 100 00:30:00 /usr/bin/claude -p --name proxy-task --model luna --max-turns 200\n'
          f'102 100 00:30:00 python -u /x/proxy-agent-stream.py {out} {live}\n'
          '301 300 00:05:00 /usr/bin/claude -p --name proxy-other --model terra --max-turns 10\n')
    doc = module.proxy_snapshot(tmux, ps, now, [{'name': 'proxy-failed', 'rc': '1', 'result': 'unknown'}])
    assert doc['rows'][0]['state'] == 'STALE'
    assert doc['rows'][0]['idle'] == 21
    assert doc['rows'][1]['state'] == 'dead window'
    assert doc['rows'][2]['model'] == 'terra'
    rendered = '\n'.join(module.proxies_block(doc))
    assert 'proxy-failed' not in rendered and 'finished' not in rendered


def test_proxy_rows_show_runner_metadata_and_exit_result(tmp_path):
    import os
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    job = tmp_path / 'proxy-agent-job.fixture'
    job.mkdir()
    brief = tmp_path / 'brief.md'
    brief.write_text('ISSUE: 123\nPR: 456\n')
    output = tmp_path / 'output.md'
    output.write_text('summary\nRESULT private-secret customer-text\n')
    jsonl = tmp_path / 'output.jsonl'
    jsonl.write_text('{}\n')
    os.utime(jsonl, (now.timestamp() - 420, now.timestamp() - 420))
    runner = job / 'run.sh'
    runner.write_text(f'export SECRET=hidden\nclaude --model gpt-6.1-sol < {brief} 2> /tmp/x.log | python3 -I -u /x/proxy-agent-stream.py {output} {jsonl}\n')
    tmux = f'0\tpx-px-task\t{int(now.timestamp())}\t1\t100\tbash {runner}\n'
    doc = module.build_proxy_snapshot(tmux, '100 1 01:02:03 bash run.sh', now, [])
    rendered = '\n'.join(module.proxies_block(doc))
    assert 'px-task' not in rendered and 'finished' not in rendered
    assert 'RESULT AVAILABLE' not in rendered and 'hidden' not in rendered
    assert 'private-secret' not in rendered and 'customer-text' not in rendered
    assert 'result_line' not in doc['rows'][0]


def test_proxy_unknown_metadata_stays_unknown():
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    doc = module.build_proxy_snapshot(f'0\tpx-task\t{int(now.timestamp())}\t0\t100\n', '', now, [])
    rendered = '\n'.join(module.proxies_block(doc))
    assert 'issue/PR UNKNOWN' not in rendered
    assert 'px-task · UNAVAILABLE (no matching launch record or stream model) · running ?' in rendered


def test_proxy_finished_result_and_custody_fallback(tmp_path):
    import json
    import sqlite3
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    db = tmp_path / 'ops.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE events (id INTEGER, source TEXT, target_type TEXT, issue INTEGER, payload_json TEXT, ts TEXT)')
        conn.execute('INSERT INTO events VALUES (1, ?, ?, ?, ?, ?)', ('proxy-agent', 'proxy_assignment', 12, json.dumps({'proxy': 'px-task', 'ref': 'pr:34', 'brief_sha256': 'digest'}), now.isoformat()))
    rows = [{'name': 'px-px-task', 'state': 'running', 'age': None}]
    module.proxy_work_history(rows, now, db)
    assert 'work_ref' not in rows[0]  # Display name alone is not current custody.
    doc = {'rows': rows, 'completions': [{'name': 'px-task', 'rc': '0', 'result': 'RESULT done'}]}
    rendered = '\n'.join(module.proxies_block(doc))
    assert 'issue/PR UNKNOWN' not in rendered and 'RESULT UNKNOWN' not in rendered
    assert 'RESULT done' not in rendered


# Authored producer/reader regression definitions. Standing directive: NOT_RUN.
def test_native_peer_and_current_brief_bind_double_prefixed_window(tmp_path, monkeypatch):
    import hashlib
    import json
    import sqlite3
    now = datetime(2026, 11, 11, 14, tzinfo=timezone.utc)
    born = now.timestamp() - 10
    job = tmp_path / 'proxy-agent-job.bound'
    job.mkdir()
    brief = tmp_path / 'brief.md'
    brief.write_text('MODE: REWORK\nPR: 13533\n')
    digest = hashlib.sha256(brief.read_bytes()).hexdigest()
    runner = job / 'run.sh'
    runner.write_text(f'claude --name px-13533-sol --model gpt-6.1-sol < {brief} 2> /tmp/x.log | python3 -I -u /x/proxy-agent-stream.py /tmp/out /tmp/out.jsonl\n')
    original = module._proxy_runner_metadata
    def metadata(*args):
        value = original(*args)
        value['launch_birth'] = born  # Mock only the retained filesystem birth identity.
        return value
    monkeypatch.setattr(module, '_proxy_runner_metadata', metadata)
    tmux = f'0\tpx-px-13533-sol\t{int(now.timestamp())}\t0\t100\tbash {runner}\n'
    ps = '100 1 00:00:10 bash run.sh\n101 100 00:00:09 /usr/bin/claude --name px-13533-sol --model gpt-6.1-sol\n'
    db = tmp_path / 'ops.db'
    payload = {'proxy': 'px-13533-sol', 'ref': 'pr:13533', 'brief_sha256': digest,
               'head_sha': 'a' * 40, 'work_started_at': '2026-11-11T10:00:00Z',
               'stage_started_at': '2026-11-11T13:00:00Z'}
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE events (id INTEGER, source TEXT, target_type TEXT, issue INTEGER, payload_json TEXT, ts TEXT)')
        conn.execute('INSERT INTO events VALUES (1, ?, ?, ?, ?, ?)', ('proxy-agent', 'proxy_assignment', 13533, json.dumps(payload), now.isoformat()))
    rows = module.build_proxy_snapshot(tmux, ps, now, [])['rows']
    assert rows[0]['current_peer'] == 'px-13533-sol'
    module.proxy_work_history(rows, now, db)
    assert rows[0]['work_ref'] == 'pr:13533'
    assert rows[0]['work_started_at_minutes'] == 240
    for bad in ('brief', 'ref', 'old-launch', 'missing-peer', 'same-second'):
        fresh = module.build_proxy_snapshot(tmux, ps, now, [])['rows']
        changed = dict(payload)
        stamp = now.isoformat()
        if bad == 'brief': changed['brief_sha256'] = 'foreign'
        if bad == 'ref': changed['ref'] = 'pr:999'
        if bad == 'old-launch': stamp = '2026-11-11T12:00:00Z'
        if bad == 'missing-peer': fresh[0].pop('current_peer')
        if bad == 'same-second': fresh[0]['launch_birth'] = now.timestamp() + .1
        with sqlite3.connect(db) as conn:
            conn.execute('UPDATE events SET payload_json=?, ts=?', (json.dumps(changed), stamp))
        module.proxy_work_history(fresh, now, db)
        assert 'work_ref' not in fresh[0]
        assert 'work_head' not in fresh[0] and 'stage_started_at_minutes' not in fresh[0]
        rendered = '\n'.join(module.proxies_block({'rows': fresh, 'completions': []}))
        assert 'running ' in rendered and 'this stage UNKNOWN' not in rendered


def test_reparented_named_proxy_and_unverified_cases(tmp_path):
    now = datetime(2026, 11, 11, 14, tzinfo=timezone.utc)
    job = tmp_path / 'proxy-agent-job.reparented'
    job.mkdir()
    runner = job / 'run.sh'
    runner.write_text('claude --name px-owned --model luna\n')
    window = f'0\tpx-owned\t{int(now.timestamp())}\t{{dead}}\t100\tbash {runner}\n'
    process = '100 1 00:00:10 bash run.sh\n101 1 00:00:09 /usr/bin/claude --name px-owned --model luna\n'
    def row(dead, ps):
        return module.build_proxy_snapshot(window.format(dead=dead), ps, now, [])['rows'][0]
    assert row(0, process)['current_peer'] == 'px-owned'
    assert 'current_peer' not in row(1, process)
    assert 'current_peer' not in row(0, '100 1 00:00:10 bash run.sh\n')
    assert 'current_peer' not in row(0, process + '102 1 00:00:08 /usr/bin/claude --name px-owned\n')


def test_proxy_output_age_future_and_invalid_is_unknown(tmp_path, monkeypatch):
    from types import SimpleNamespace
    now = datetime(2026, 11, 11, 14, tzinfo=timezone.utc)
    job = tmp_path / 'proxy-agent-job.age'
    job.mkdir()
    brief = tmp_path / 'brief.md'
    brief.write_text('PR: 34\n')
    jsonl = tmp_path / 'output.jsonl'
    runner = job / 'run.sh'
    runner.write_text(f'claude --name proxy-age < {brief} 2> /tmp/x | python3 -I -u /x/proxy-agent-stream.py /tmp/out {jsonl}\n')
    original_stat = Path.stat
    for mtime in (now.timestamp() + 60, float('nan'), float('inf'), -1):
        def stat(path, *args, **kwargs):
            return SimpleNamespace(st_mtime=mtime) if path == jsonl else original_stat(path, *args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(Path, 'stat', stat)
            metadata = module._proxy_runner_metadata(f'bash {runner}', now, False)
        assert 'output_age' not in metadata
        doc = {'rows': [{'name': 'px-age', 'state': 'running', 'age': 2, **metadata}], 'completions': []}
        assert 'output age UNKNOWN' not in '\n'.join(module.proxies_block(doc))


def test_proxy_completion_result_prose_stays_private(tmp_path, monkeypatch):
    import json
    import sqlite3
    now = datetime(2026, 11, 11, 14, tzinfo=timezone.utc)
    db = tmp_path / '.local/share/master-of-panes/data/mop.db'
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE events (id INTEGER, event_type TEXT, timestamp TEXT, payload TEXT)')
        conn.execute('INSERT INTO events VALUES (1, ?, ?, ?)', ('send_command', now.isoformat(), json.dumps({'command': 'PROXY_STOP name=proxy-test rc=1 out=/private RESULT private-secret customer-text'})))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    completed, _ = module._proxy_completions(now)
    rendered = '\n'.join(module.proxies_block({'rows': [], 'completions': completed}))
    assert 'rc=1' not in rendered and 'RESULT AVAILABLE' not in rendered
    assert 'private-secret' not in rendered and 'customer-text' not in rendered
    assert 'private-secret' not in repr(completed)


def test_proxy_collector_budget_bounds_slow_ps_and_sqlite(monkeypatch):
    import threading
    import time
    from types import SimpleNamespace

    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    monkeypatch.setitem(module.COLLECTOR_TIMEOUTS, 'proxies', 0.05)
    for slow in ('ps', 'sqlite'):
        release = threading.Event()
        def run(args, _slow=slow, _release=release, **kwargs):
            if _slow == 'ps' and args[0] == 'ps':
                _release.wait(2)
            return SimpleNamespace(stdout='')
        def connect(*args, _slow=slow, _release=release, **kwargs):
            if _slow == 'sqlite':
                _release.wait(2)
            raise module.sqlite3.OperationalError('stubbed')
        monkeypatch.setattr(module.subprocess, 'run', run)
        monkeypatch.setattr(module.sqlite3, 'connect', connect)
        start = time.monotonic()
        try:
            doc = module.read_proxies(now)
            assert time.monotonic() - start < 0.5
            assert module.proxies_block(doc) == ['*Proxies:* unavailable (collector budget)']
        finally:
            release.set()
