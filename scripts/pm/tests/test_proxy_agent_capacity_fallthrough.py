"""Launcher contract with a disposable home, loopback model probe and mock PM/Slack CLIs."""
import contextlib
import http.server
import json
import os
from pathlib import Path
import subprocess
import threading

import pytest

SCRIPTS = Path(__file__).parents[1] / 'shared-assets/claude/scripts'


def executable(path, body):
    path.write_text(body)
    path.chmod(0o700)


@contextlib.contextmanager
def model_probe():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{}')

        def log_message(self, *_args):
            pass

    with http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f'http://127.0.0.1:{server.server_port}'
        finally:
            server.shutdown()
            thread.join()


@pytest.fixture
def launch(tmp_path):
    scripts = tmp_path / '.claude/scripts'
    scripts.mkdir(parents=True)
    state = scripts.parent / 'state'
    state.mkdir()
    (scripts / 'proxy-run-state.py').symlink_to(SCRIPTS / 'proxy-run-state.py')
    (scripts / 'pm-ops.py').write_text('''import json, os, sys
from pathlib import Path
with (Path(os.environ['HOME']) / 'ledger.jsonl').open('a') as f:
    print(json.dumps(sys.argv[1:]), file=f)
if '--print-id' in sys.argv:
    print(47123)
''')
    executable(scripts / 'slack-send.sh', '''#!/bin/bash
[[ "$*" == '-c D0AMF0XE6TS -f' ]] || exit 2
printf 'DM_MOCK\\n' >> "$HOME/dm.log"
/bin/cat >> "$HOME/dm.log"
printf "OK ts=123.456 channel=D0AMF0XE6TS\\n"
''')
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    executable(bindir / 'memory_pressure', '#!/bin/bash\nprintf "System-wide memory free percentage: 60%%\\n"\n')
    executable(bindir / 'sysctl', '#!/bin/bash\nprintf "used = 0M\\n"\n')
    executable(bindir / 'df', '#!/bin/bash\nprintf "Filesystem 1024-blocks Used Available Capacity Mounted\\nfixture 100000000 1 99999999 1%% /\\n"\n')
    brief = tmp_path / 'echo brief.md'
    brief.write_text('ISSUE: #15001 — echo ECHO.\n')

    def run(model, *, dry=False, pm_live=1, typ='hd-builder', **overrides):
        (state / 'pm-subagents.json').write_text(json.dumps({'agents': [{'status': 'running'}] * pm_live}))
        with model_probe() as base:
            env = dict(HOME=str(tmp_path), PATH=f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                       PROXY_AGENT_SANDBOX='0', PROXY_AGENT_NAME='proxy-fixture', PROXY_AGENT_STUB_POOL_LIVE='1',
                       PROXY_AGENT_OUT_DIR=str(tmp_path / 'out'), PROXY_BURN_GATE='shadow', PM_SUBAGENT_CAP='1',
                       CLAUDE_PROXY_CAP='1', ULTRA_PROXY_CAP='1', HY4_PROXY_CAP='1', CODEX_PROXY_CAP='1',
                       CLIPROXY_API_KEY='fixture-only', PROXY_AGENT_GROK_BASE=base)
            if dry:
                env['PROXY_AGENT_DRY_RUN'] = '1'
            env.update(overrides)
            return subprocess.run(['bash', str(SCRIPTS / 'proxy-agent.sh'), typ, str(brief), '--model', model],
                                  cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    return run


def test_capped_pin_uses_next_eligible_pool(launch):
    run = launch('claude-sonnet-5-5', dry=True, ULTRA_PROXY_CAP='2')
    assert run.returncode == 0, run.stderr
    assert 'POOL_FALLTHROUGH model=claude-sonnet-5-5 tier=MID' in run.stderr
    assert 'tier=MID model=ultra/claude-sonnet-5-5' in run.stderr


def test_all_full_offers_pm_without_queuing(launch, tmp_path):
    run = launch('claude-sonnet-5-5', pm_live=0)
    assert run.returncode == 75, run.stderr
    assert 'IN_PROCESS_AVAILABLE live=0 cap=1' in run.stderr
    assert 'agent_launch_queued' not in (tmp_path / 'ledger.jsonl').read_text()
    assert 'proxy_relaunch' not in (tmp_path / 'ledger.jsonl').read_text()
    assert not (tmp_path / 'dm.log').exists()


def test_all_full_queues_exact_launch_and_dedupes_dm(launch, tmp_path):
    for status in ('sent', 'deduped'):
        run = launch('claude-sonnet-5-5')
        assert run.returncode == 74, run.stderr
        assert f'QUEUED_AWAITING_OVERRIDE lane=proxy-fixture obligation=47123 dm={status}' in run.stderr
    rows = [json.loads(row) for row in (tmp_path / 'ledger.jsonl').read_text().splitlines()]
    queued = [row for row in rows if 'agent_launch_queued' in row]
    assert len(queued) == 2
    assert queued[0][queued[0].index('--target-id') + 1] == '15001'
    action = queued[0][queued[0].index('--action') + 1]
    assert 'echo brief.md\'' in action and '--model claude-sonnet-5-5' in action
    dm = (tmp_path / 'dm.log').read_text()
    assert dm.count('DM_MOCK') == 1
    assert 'proxy-fixture (hd-builder), ISSUE #15001' in dm
    assert 'reply rajiv-override proxy-fixture' in dm
    assert 'claude=1/1' in dm and 'ultra=1/1' in dm and 'codex=1/1' in dm


@pytest.mark.parametrize('typ', ['hd-proof-qa', 'pm-proof-qa', 'disk-cleanup'])
def test_proxy_only_never_offered_pm_even_with_free_slots(launch, typ):
    run = launch('claude-sonnet-5-5', pm_live=0, typ=typ)
    assert run.returncode == 74, run.stderr
    assert 'IN_PROCESS_AVAILABLE' not in run.stderr
    assert 'QUEUED_AWAITING_OVERRIDE' in run.stderr


def test_strict_pin_does_not_switch_to_available_alternative(launch):
    run = launch('claude-sonnet-5-5', dry=True, ULTRA_PROXY_CAP='2', PROXY_AGENT_PIN_STRICT='1')
    assert run.returncode == 74, run.stderr
    assert 'POOL_FALLTHROUGH' not in run.stderr
    assert 'obligation=preview dm=mocked' in run.stderr
