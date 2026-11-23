"""Launcher-level lane state machine (Rajiv D0AMF0XE6TS thread 1795436469.682079, ts 1795438159.967189), against the REAL proxy-agent.sh with a real tmux
window, a fake claude and a recording pm-ops stub. No model, no Slack, no real pm-ops DB, no real run registry (HOME is a temp dir).

  launched lane : proxy_run row goes queued -> running (only once the claude child exists) -> done; the child's stdin carries the RESULT block instruction;
                  a RESULT block in its final message reaches the PROXY_STOP message and the row.
  refused lane  : exit 75 from the launcher writes refused + a proxy_relaunch CAPACITY_WAIT row (brief, type, env, model args) and closes the run row.
  P0 at cap     : no preemptable lane -> refused 75 and queued, never launched over the cap.
Run: /opt/homebrew/bin/pytest -q -p no:cacheprovider tests/test_proxy_lane_launch_states.py
"""
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1]
WRAPPER = SCRIPTS / 'proxy-agent.sh'
STUB = '''import json, os, sys
argv = sys.argv[1:]
open(os.environ['PROXY_TEST_STUB_LOG'], 'a').write(json.dumps(argv) + "\\n")
if '--print-id' in argv:
    print(42)
'''
FAKE_CLAUDE = '''#!/usr/bin/env python3
import json, os, sys, time
prompt = sys.stdin.read()
open(os.path.join(os.environ['HOME'], 'prompt-seen.txt'), 'w').write(prompt)
name = sys.argv[sys.argv.index('--name') + 1]
time.sleep(float(os.environ.get('PROXY_TEST_SLEEP', '2.5')))
block = {'lane': name, 'type': 'test', 'issue': 14461, 'pr': None, 'head': 'b' * 40, 'verdict': 'DONE', 'proof_links': [], 'next_stage': None, 'notes': 'ok'}
text = 'Worked.\\nRESULT_BEGIN\\n' + json.dumps(block) + '\\nRESULT_END\\nDONE: finished\\n'
for event in ({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'working'}]}}, {'type': 'result', 'result': text}):
    print(json.dumps(event), flush=True)
'''


@pytest.fixture
def lab(tmp_path):
    session = 'proxy-states-' + uuid.uuid4().hex[:8]
    home = tmp_path / 'home'
    for d in ('.config/cliproxyapi', '.claude/agents', '.claude/scripts', '.claude/state'):
        (home / d).mkdir(parents=True)
    (home / '.config/cliproxyapi/client.env').write_text('CLIPROXY_API_KEY=test-only\n')
    (home / '.claude/agents/test.md').write_text('---\ntools: Bash\n---\nTest worker.\n')
    (home / '.claude/state/proxy-caps.env').write_text('PROXY_AGENT_TOTAL_CAP=18\n')
    for name in ('proxy-agent-stop.sh', 'proxy-agent-stream.py', 'proxy-run-state.py', 'proxy-window-label.py', 'proxy-agent-notify.py'):
        shutil.copy(SCRIPTS / name, home / '.claude/scripts' / name)
    (home / '.claude/scripts/message-pm.sh').write_text('#!/bin/bash\nprintf "%s\\n" "$2" >> "$HOME/messages"\nprintf "MESSAGE_PM_SENT slot=0 verified=true\\n"\n')
    (home / '.claude/scripts/message-pm.sh').chmod(0o755)
    (tmp_path / 'bin').mkdir()
    (tmp_path / 'bin/claude').write_text(FAKE_CLAUDE)
    (tmp_path / 'bin/claude').chmod(0o755)
    (tmp_path / 'stub.py').write_text(STUB)
    clone = tmp_path / 'clone'
    (clone / '.claude').mkdir(parents=True)
    (clone / '.claude/settings.json').write_text('{}\n')
    (clone / 'CLAUDE.md').write_text('# Test clone\n')
    for cmd in (['init', '-q'], ['add', '.'], ['-c', 'user.name=T', '-c', 'user.email=t@example.invalid', 'commit', '-qm', 'init']):
        subprocess.run(['git', '-C', str(clone), *cmd], check=True, capture_output=True)
    (tmp_path / 'brief.md').write_text('ISSUE: 14461\nPR: 14470\nprint cwd\n')
    env = {**os.environ, 'HOME': str(home), 'PATH': f'{tmp_path / "bin"}:{os.environ["PATH"]}', 'PROXY_AGENT_STUB_GEMINI': '0.93', 'PROXY_AGENT_STUB_GROK': '200',
           'PROXY_AGENT_STATE_DIR': str(tmp_path / 'selector-state'), 'PROXY_AGENT_PM_OPS_CLI': str(tmp_path / 'stub.py'), 'PROXY_TEST_STUB_LOG': str(tmp_path / 'calls.jsonl'),
           'PROXY_AGENT_TMUX_SESSION': session, 'PROXY_AGENT_CHECKOUT': str(clone), 'PROXY_AGENT_STUB_POOL_LIVE': '0', 'PROXY_AGENT_COMBINED_LOG': str(tmp_path / 'all.log'),
           'PROXY_AGENT_OUT_DIR': str(tmp_path / 'out')}
    for k in ('PROXY_AGENT_NAME', 'PROXY_AGENT_CAP_OVERRIDE', 'PROXY_AGENT_DRY_RUN', 'PROXY_AGENT_PREEMPT', 'PROXY_AGENT_RUN_STATE_DIR', 'PROXY_RESUME_ORIGINAL'):
        env.pop(k, None)
    yield tmp_path, home, env
    subprocess.run(['tmux', 'kill-session', '-t', '=' + session], capture_output=True)


def launch(lab, name, *extra, env=None):
    tmp, _home, base = lab
    return subprocess.run(['bash', str(WRAPPER), 'test', str(tmp / 'brief.md'), '--out', str(tmp / f'{name}.md'), *extra],
                          env={**base, 'PROXY_AGENT_NAME': name, **(env or {})}, capture_output=True, text=True, timeout=240)


def rows(tmp, kind, verb='obligation-upsert'):
    path = tmp / 'calls.jsonl'
    out = []
    for argv in ([json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []):
        if argv[0] != verb or argv[argv.index('--kind') + 1] != kind:
            continue
        pairs = {k: v for k, v in zip(argv[1:], argv[2:]) if k.startswith('--')}
        out.append({**pairs, 'evidence': json.loads(pairs['--evidence-json']) if '--evidence-json' in pairs else {}})
    return out


def test_a_launched_lane_goes_queued_running_done_and_its_result_block_reaches_the_row_and_the_stop_message(lab):
    tmp, home, _ = lab
    done = launch(lab, 'proxy-test-states-ok', '--model', 'gpt-6.1-terra')
    assert done.returncode == 0, done.stderr
    states = [r['evidence'].get('state') for r in rows(tmp, 'proxy_run')]
    assert states[0] == 'queued' and 'running' in states and states[-1] == 'done', states
    assert states.index('running') > states.index('queued') and states.index('running') < len(states) - 1
    running = next(r for r in rows(tmp, 'proxy_run') if r['evidence'].get('state') == 'running')
    assert running['evidence']['pid'] > 1 and running['--target-id'] == 'proxy-test-states-ok' and running['--issue'] == '14461'
    final = rows(tmp, 'proxy_run')[-1]['evidence']
    assert final['result_status'] == 'STRUCTURED' and final['head'] == 'b' * 40 and final['result_block']['verdict'] == 'DONE'
    assert final['lease_basis'] == 'launch' and final['lease_ttl_minutes'] == 60
    prompt = (home / 'prompt-seen.txt').read_text()
    assert 'RESULT BLOCK (required)' in prompt and '"lane": "proxy-test-states-ok"' in prompt and 'RESULT_BEGIN' in prompt and 'PROXY RUN CONTRACT' in prompt
    message = (home / 'messages').read_text()
    assert 'result=STRUCTURED verdict=DONE head=' + 'b' * 40 in message and 'RESULT DONE: finished' in message
    assert rows(tmp, 'proxy_relaunch') == [] and len(rows(tmp, 'proxy_run', 'obligation-resolve')) == 1


def test_a_refused_launch_records_refused_and_queues_a_capacity_wait_relaunch_with_its_args(lab):
    tmp, home, _ = lab
    (home / '.claude/state/proxy-no-codex').touch()
    done = launch(lab, 'proxy-test-states-refused', '--model', 'gpt-6.1-terra', env={'CODEX_PROXY_CAP': '8', 'PROXY_AGENT_CAP_OVERRIDE': 'rajiv:1795439359.022079'})
    assert done.returncode == 75 and 'REFUSED primary gpt-6.1-terra (proxy-no-codex)' in done.stderr
    states = [r['evidence'].get('state') for r in rows(tmp, 'proxy_run')]
    assert states == ['queued', 'refused'], states  # never "launched", never running
    (relaunch,) = rows(tmp, 'proxy_relaunch')
    spec = relaunch['evidence']['relaunch']
    assert relaunch['--blocker'].startswith('CAPACITY_WAIT:') and relaunch['--target-id'] == 'proxy-test-states-refused'
    assert spec['type'] == 'test' and spec['brief'] == str(tmp / 'brief.md') and spec['model_args'] == ['--model', 'gpt-6.1-terra']
    assert spec['env']['PROXY_AGENT_CAP_OVERRIDE'] == 'rajiv:1795439359.022079' and spec['env']['CODEX_PROXY_CAP'] == '8'
    assert 'PROXY_AGENT_NAME=proxy-test-states-refused' in spec['cmd'] and '--model gpt-6.1-terra' in spec['cmd'] and str(tmp / 'brief.md') in spec['cmd']
    assert not (home / 'prompt-seen.txt').exists() and len(rows(tmp, 'proxy_run', 'obligation-resolve')) == 1


def test_a_p0_at_a_full_pool_with_nothing_to_preempt_is_queued_and_never_launched_over_the_cap(lab):
    tmp, home, _ = lab
    helper = tmp / 'helper.py'
    helper.write_text('import sys\nprint("PREEMPT_REFUSED reason=nothing_preemptible")\nsys.exit(3)\n')
    done = launch(lab, 'proxy-test-states-p0', '--model', 'gpt-6.2-sol',
                  env={'PROXY_AGENT_CAP_OVERRIDE': 'P0:14461', 'PROXY_AGENT_STUB_POOL_LIVE': '8', 'CODEX_PROXY_CAP': '8', 'PROXY_AGENT_PREEMPT_HELPER': str(helper)})
    assert done.returncode == 75 and 'nothing preemptable' in done.stderr and 'REFUSED primary gpt-6.2-sol (codex pool_cap)' in done.stderr
    assert not (home / 'prompt-seen.txt').exists()  # no lane started anywhere
    (relaunch,) = rows(tmp, 'proxy_relaunch')
    assert relaunch['evidence']['relaunch']['env']['PROXY_AGENT_CAP_OVERRIDE'] == 'P0:14461'  # the re-queued launch is still a P0 (and still gets no bypass)
    assert [r['evidence'].get('state') for r in rows(tmp, 'proxy_run')] == ['queued', 'refused']
