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
