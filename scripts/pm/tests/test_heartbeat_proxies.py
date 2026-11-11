import importlib.util
from datetime import datetime, timezone
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / 'shared-assets/claude/scripts/pm/heartbeat/heartbeat-compose.py'
spec = importlib.util.spec_from_file_location('heartbeat_proxies', SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


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
    assert 'best effort' in rendered and 'proxy-failed' in rendered and 'rc=1' in rendered


def test_proxy_rows_show_runner_metadata_and_exit_result(tmp_path):
    import os
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    job = tmp_path / 'proxy-agent-job.fixture'
    job.mkdir()
    brief = tmp_path / 'brief.md'
    brief.write_text('ISSUE: 123\nPR: 456\n')
    output = tmp_path / 'output.md'
    output.write_text('summary\nRESULT complete\n')
    jsonl = tmp_path / 'output.jsonl'
    jsonl.write_text('{}\n')
    os.utime(jsonl, (now.timestamp() - 420, now.timestamp() - 420))
    runner = job / 'run.sh'
    runner.write_text(f'export SECRET=hidden\nclaude --model gpt-6.1-sol < {brief} 2> /tmp/x.log | python3 -I -u /x/proxy-agent-stream.py {output} {jsonl}\n')
    tmux = f'0\tpx-px-task\t{int(now.timestamp())}\t1\t100\tbash {runner}\n'
    doc = module.build_proxy_snapshot(tmux, '100 1 01:02:03 bash run.sh', now, [])
    rendered = '\n'.join(module.proxies_block(doc))
    assert 'px-task' in rendered and 'px-px-task' not in rendered
    assert 'gpt-6.1-sol' in rendered and 'issue:123' in rendered and 'pr:456' in rendered
    assert 'process age 62m' in rendered and 'output age 7m' in rendered
    assert 'RESULT complete' in rendered and 'hidden' not in rendered


def test_proxy_unknown_metadata_stays_unknown():
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    doc = module.build_proxy_snapshot(f'0\tpx-task\t{int(now.timestamp())}\t0\t100\n', '', now, [])
    rendered = '\n'.join(module.proxies_block(doc))
    assert 'issue/PR UNKNOWN' in rendered and 'process age unknown' in rendered
    assert 'output age UNKNOWN' in rendered and 'unknown px-task' in rendered


def test_proxy_finished_result_and_custody_fallback(tmp_path):
    import json
    import sqlite3
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    db = tmp_path / 'ops.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE events (id INTEGER, source TEXT, target_type TEXT, issue INTEGER, payload_json TEXT)')
        conn.execute('INSERT INTO events VALUES (1, ?, ?, ?, ?)', ('proxy-agent', 'proxy_assignment', 12, json.dumps({'proxy': 'px-task', 'ref': 'pr:34'})))
    rows = [{'name': 'px-px-task', 'state': 'running', 'age': None}]
    module.proxy_work_history(rows, now, db)
    assert rows[0]['work_ref'] == 'pr:34'
    doc = {'rows': rows, 'completions': [{'name': 'px-task', 'rc': '0', 'result': 'RESULT done'}]}
    rendered = '\n'.join(module.proxies_block(doc))
    assert 'pr:34' in rendered and 'RESULT done' in rendered


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
