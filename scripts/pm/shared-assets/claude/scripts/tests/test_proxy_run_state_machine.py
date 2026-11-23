"""Lane lease + launch state machine (Rajiv D0AMF0XE6TS thread 1795436469.682079, ts 1795438159.967189): proxy-run-state.py.

queued -> running (only after the child claude process is confirmed) -> done | refused | failed, all written to the pm-ops proxy_run row keyed by
lane name; a refusal (rc 75) upserts a proxy_relaunch CAPACITY_WAIT row carrying the brief, type, env overrides and model args.
Hermetic: the pm-ops CLI is a recording stub (PROXY_AGENT_PM_OPS_CLI contract), the run registry is a temp dir, the "lane" is a throwaway process.
Run: /opt/homebrew/bin/pytest -q -p no:cacheprovider tests/test_proxy_run_state_machine.py
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HELPER = Path(__file__).parents[1] / 'proxy-run-state.py'
STUB = '''import json, os, sys
argv = sys.argv[1:]
open(os.environ['STUB_LOG'], 'a').write(json.dumps(argv) + "\\n")
if '--kind' in argv and argv[argv.index('--kind') + 1] == os.environ.get('STUB_FAIL_KIND'):
    sys.exit(1)
if '--print-id' in argv:
    print(42)
'''
NAME = 'proxy-hd-builder-14461t'
TOKEN = 'tok-1'


class Lab:
    def __init__(self, tmp):
        self.tmp = tmp
        (tmp / 'stub.py').write_text(STUB)
        self.log = tmp / 'calls.jsonl'
        self.repo = tmp / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.brief = tmp / 'brief.md'
        self.brief.write_text('ISSUE: 14461\nPR: 14470\nImplement it.\n')
        self.out = tmp / 'out.md'
        self.env = {**os.environ, 'PROXY_AGENT_RUN_STATE_DIR': str(tmp / 'runs'), 'PROXY_AGENT_PM_OPS_CLI': str(tmp / 'stub.py'), 'STUB_LOG': str(self.log)}
        for k in ('PROXY_AGENT_CAP_OVERRIDE', 'PROXY_AGENT_RUN_TOKEN', 'PROXY_AGENT_NAME', 'STUB_FAIL_KIND'):
            self.env.pop(k, None)

    def run(self, *args, env=None, check=True):
        done = subprocess.run([sys.executable, '-I', str(HELPER), *args], env={**self.env, **(env or {})}, capture_output=True, text=True, cwd=str(self.tmp))
        if check:
            assert done.returncode == 0, done.stderr
        return done

    def calls(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []

    def rows(self, kind, verb='obligation-upsert'):
        out = []
        for argv in self.calls():
            if argv[0] != verb or argv[argv.index('--kind') + 1] != kind:
                continue
            pairs = {k: v for k, v in zip(argv[1:], argv[2:]) if k.startswith('--')}
            ev = json.loads(pairs['--evidence-json']) if '--evidence-json' in pairs else {}
            out.append({**pairs, 'evidence': ev, 'argv': argv})
        return out

    def last_run(self):
        return self.rows('proxy_run')[-1]

    def acquire(self):
        self.run('acquire', '--name', NAME, '--token', TOKEN)

    def queue(self, *orig, env=None):
        self.run('queue', '--name', NAME, '--token', TOKEN, '--type', 'hd-builder', '--brief', str(self.brief), *[f'--orig-arg={x}' for x in orig], env=env)

    def start(self):
        self.run('start', '--name', NAME, '--token', TOKEN, '--type', 'hd-builder', '--brief', str(self.brief), '--cwd', str(self.repo), '--out', str(self.out),
                 '--model', 'claude-sonnet-5-5', '--pm-role', '0')

    def manifest(self):
        return json.loads((self.tmp / 'runs' / NAME / 'manifest.json').read_text())


@pytest.fixture
def lab(tmp_path):
    return Lab(tmp_path)


def lane_process(tmp_path, name=NAME, seconds=30):
    script = tmp_path / 'bin' / 'claude'
    script.parent.mkdir(exist_ok=True)
    script.write_text(f'import time\ntime.sleep({seconds})\n')
    return subprocess.Popen([sys.executable, str(script), '-p', '--name', name, '--model', 'claude-sonnet-5-5'])


def test_queue_is_the_first_ledger_write_and_stores_the_launch_spec(lab):
    lab.acquire()
    lab.queue('hd-builder', str(lab.brief), '--model', 'claude-sonnet-5-5', env={'PROXY_AGENT_CAP_OVERRIDE': 'P0:77', 'PROXY_AGENT_RUN_TOKEN': 'SECRETTOKEN123',
                                                                                'PROXY_AGENT_NAME': 'x', 'OPENCODE_PROXY_CAP': '3', 'GH_TOKEN': 'ghp_nope'})
    row = lab.last_run()
    assert row['--kind'] == 'proxy_run' and row['--target-id'] == NAME and row['--issue'] == '14461' and row['--pr'] == '14470'
    assert row['evidence']['state'] == 'queued' and row['evidence']['type'] == 'hd-builder' and '--replace-evidence' in row['argv']
    spec = json.loads((lab.tmp / 'runs' / NAME / 'active' / 'launch.json').read_text())
    assert spec['argv'] == ['hd-builder', str(lab.brief), '--model', 'claude-sonnet-5-5'] and spec['model_args'] == ['--model', 'claude-sonnet-5-5']
    assert spec['env']['PROXY_AGENT_CAP_OVERRIDE'] == 'P0:77' and spec['env']['OPENCODE_PROXY_CAP'] == '3'  # a re-queued P0 is still a P0
    flat = json.dumps(spec)
    assert 'SECRETTOKEN123' not in flat and 'ghp_nope' not in flat and 'PROXY_AGENT_RUN_TOKEN' not in flat and "PROXY_AGENT_NAME=x" not in flat
    assert spec['cmd'].startswith('env ') and 'PROXY_AGENT_NAME=' + NAME in spec['cmd'] and '--model claude-sonnet-5-5' in spec['cmd']


def test_start_grants_the_lease_and_stays_queued_until_the_child_is_confirmed(lab):
    lab.acquire()
    lab.queue('hd-builder', str(lab.brief))
    lab.start()
    ev = lab.last_run()['evidence']
    assert ev['state'] == 'queued' and ev['lease_ttl_minutes'] == 60 and ev['lease_basis'] == 'launch'
    assert ev['lease_started_at'] == ev['lease_renewed_at'] == ev['started_at']


def test_running_is_recorded_only_after_the_child_process_exists(lab, tmp_path):
    lab.acquire()
    lab.start()
    gone = lab.run('running', '--name', NAME, '--token', TOKEN, '--wait-s', '0', check=False)
    assert gone.returncode == 3 and 'child_not_confirmed' in gone.stderr
    assert lab.last_run()['evidence']['state'] == 'queued' and lab.manifest().get('state') == 'queued'  # nothing claimed without a process
    lane = lane_process(tmp_path)
    try:
        lab.run('running', '--name', NAME, '--token', TOKEN, '--wait-s', '10')
    finally:
        lane.kill()
        lane.wait()
    ev = lab.last_run()['evidence']
    assert ev['state'] == 'running' and ev['pid'] == lane.pid and ev['running_at']


def test_a_late_running_confirmation_never_undoes_a_finished_run(lab, tmp_path):
    lab.acquire()
    lab.start()
    lab.run('finish', '--name', NAME, '--token', TOKEN, '--rc', '0', '--reason', 'result', '--result', 'RESULT DONE: ok')
    lane = lane_process(tmp_path)
    try:
        late = lab.run('running', '--name', NAME, '--token', TOKEN, '--wait-s', '5', check=False)
    finally:
        lane.kill()
        lane.wait()
    assert late.returncode in (0, 2) and lab.last_run()['evidence']['state'] == 'done'


def test_finish_with_a_result_is_done_and_resolves_the_row(lab):
    lab.acquire()
    lab.start()
    block = {'lane': NAME, 'type': 'hd-builder', 'issue': 14461, 'pr': 14470, 'head': 'a' * 40, 'verdict': 'HANDOFF', 'proof_links': [], 'next_stage': 'hd-proof-qa', 'notes': 'x'}
    lab.run('finish', '--name', NAME, '--token', TOKEN, '--rc', '0', '--reason', 'result', '--result', 'RESULT HANDOFF: next', '--result-status', 'STRUCTURED',
            '--result-json', json.dumps(block), '--head', 'a' * 40)
    ev = lab.last_run()['evidence']
    assert ev['state'] == 'done' and ev['result_status'] == 'STRUCTURED' and ev['result_block'] == block and ev['head'] == 'a' * 40
    (resolve,) = lab.rows('proxy_run', 'obligation-resolve') or [None]
    assert resolve is not None and resolve['--target-id'] == NAME


def test_finish_without_a_result_is_failed_and_stays_open_for_pm(lab):
    lab.acquire()
    lab.start()
    lab.run('finish', '--name', NAME, '--token', TOKEN, '--rc', '143', '--reason', 'failed', '--result', 'RESULT UNSTRUCTURED head=UNKNOWN reason=no_result_block',
            '--result-status', 'UNSTRUCTURED')
    ev = lab.last_run()['evidence']
    assert ev['state'] == 'failed' and ev['result_status'] == 'UNSTRUCTURED' and ev['rc'] == '143'
    assert lab.rows('proxy_run', 'obligation-resolve') == []


def test_a_launch_refused_with_rc_75_queues_a_capacity_wait_relaunch_and_closes_the_run_row(lab):
    lab.acquire()
    lab.queue('hd-builder', str(lab.brief), '--model', 'gpt-6.1-terra', env={'PROXY_AGENT_CAP_OVERRIDE': 'P0:77'})
    lab.run('abandon', '--name', NAME, '--token', TOKEN, '--rc', '75')
    refused = lab.last_run()['evidence']
    assert refused['state'] == 'refused' and refused['rc'] == '75' and refused['reason'] == 'refused'
    (relaunch,) = lab.rows('proxy_relaunch')
    assert relaunch['--target-id'] == NAME and relaunch['--blocker'].startswith('CAPACITY_WAIT: launch refused rc=75')
    assert relaunch['--issue'] == '14461' and relaunch['--owner'] == 'pm'
    spec = relaunch['evidence']['relaunch']
    assert spec['state'] == 'CAPACITY_WAIT' and spec['type'] == 'hd-builder' and spec['brief'] == str(lab.brief)
    assert spec['model_args'] == ['--model', 'gpt-6.1-terra'] and spec['env']['PROXY_AGENT_CAP_OVERRIDE'] == 'P0:77'
    assert spec['cmd'].endswith(f"hd-builder {lab.brief} --model gpt-6.1-terra") and 'PROXY_AGENT_NAME=' + NAME in spec['cmd']
    (resolve,) = lab.rows('proxy_run', 'obligation-resolve')
    assert resolve['--target-id'] == NAME and 'relaunch queued' in resolve['--reason']
    order = [(a[0], a[a.index('--kind') + 1]) for a in lab.calls()]
    assert order.index(('obligation-upsert', 'proxy_relaunch')) < order.index(('obligation-resolve', 'proxy_run'))  # custody persisted before the close


def test_the_run_row_stays_open_when_the_relaunch_row_could_not_be_written(lab):
    lab.acquire()
    lab.queue('hd-builder', str(lab.brief))
    lab.run('abandon', '--name', NAME, '--token', TOKEN, '--rc', '75', env={'STUB_FAIL_KIND': 'proxy_relaunch'})
    assert lab.last_run()['evidence']['state'] == 'refused' and lab.rows('proxy_run', 'obligation-resolve') == []  # PM still sees it


@pytest.mark.parametrize('rc', ['2', '1', '125'])
def test_any_other_launcher_exit_is_failed_and_queues_no_relaunch(lab, rc):
    lab.acquire()
    lab.queue('hd-builder', str(lab.brief))
    lab.run('abandon', '--name', NAME, '--token', TOKEN, '--rc', rc)
    ev = lab.last_run()['evidence']
    assert ev['state'] == 'failed' and ev['reason'] == 'launch_failed' and ev['exit_rc'] == rc
    assert lab.rows('proxy_relaunch') == []


def test_abandon_without_rc_or_launch_spec_keeps_the_legacy_behaviour(lab):
    lab.acquire()
    lab.start()
    lab.run('abandon', '--name', NAME, '--token', TOKEN)  # an old launcher: no --rc, no queue step
    ev = lab.last_run()['evidence']
    assert ev['rc'] == 'UNAVAILABLE' and ev['reason'] == 'launch_failed' and ev['state'] == 'failed' and lab.rows('proxy_relaunch') == []


def test_a_continuation_refused_after_the_run_started_is_resumable_not_done(lab):
    lab.acquire()
    lab.queue('hd-builder', str(lab.brief), '--model', 'claude-sonnet-5-5')
    lab.start()
    lab.run('finish', '--name', NAME, '--token', TOKEN, '--rc', '75', '--reason', 'unavailable', '--result', 'RESULT UNSTRUCTURED')
    assert lab.last_run()['evidence']['state'] == 'refused'
    (relaunch,) = lab.rows('proxy_relaunch')
    assert relaunch['evidence']['relaunch']['cmd'].endswith(f'--resume {NAME} --model claude-sonnet-5-5')
    assert 'resume queued' in lab.rows('proxy_run', 'obligation-resolve')[0]['--reason']


def test_every_ledger_write_is_fail_open_when_the_ledger_is_down(lab):
    lab.acquire()
    lab.env['PROXY_AGENT_PM_OPS_CLI'] = str(lab.tmp / 'missing.py')
    assert lab.run('queue', '--name', NAME, '--token', TOKEN, '--type', 'hd-builder', '--brief', str(lab.brief)).returncode == 0
    assert (lab.tmp / 'runs' / NAME / 'active' / 'launch.json').exists()
