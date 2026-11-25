"""deepseek-direct-gate.py: the ONE admission boundary for paid deepseek-direct lanes (CTO REVISE of 248ae25b, C0ALZJHGE49 thread 1795597777.761599, ts 1795600640.261439).

  scope     : no token, a malformed token or another run's token is refused (rc 2); only bench:<run_id> of the state file is admitted.
  stop      : a start and a continuation are both refused while the state file says stop; the flag is read at that moment.
  budget    : spent_usd + the in-flight spend of the running lanes (each at least the floor) reaching budget_usd refuses.
  cap       : the cap is re-read from proxy-caps.env on every admission (a continuation sees a cap lowered after the lane started); parallel starts
              cannot exceed it (flock + reservations); a reservation counts before the claude process exists.
  settle    : the PROXY_AGENT_USAGE line is priced at the published peak rates (input miss, cache read, output) and added to spent_usd once.
  reconcile : a lane that ends without settling is settled from its stream by the next admission.
Each case drives the REAL script in a temp state dir (PROXY_AGENT_DSD_STATE_DIR), with the launcher's own process-table stub (PROXY_AGENT_STUB_POOL_LIVE).
Run: /opt/homebrew/bin/pytest -q -p no:cacheprovider tests/test_deepseek_direct_gate.py
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

GATE = Path(__file__).parents[1] / 'deepseek-direct-gate.py'
FLASH = 'deepseek-direct/deepseek-flash'
USAGE = 'PROXY_AGENT_USAGE model=%s results=1 input_tokens=100000 output_tokens=30000 cache_read_tokens=6000000 cache_creation_tokens=0\n' % FLASH
USAGE_USD = 100000 * 0.30 / 1e6 + 6000000 * 0.006 / 1e6 + 30000 * 1.20 / 1e6  # the published peak rates: miss, cache hit, output (docstring of the gate)


@pytest.fixture
def lab(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    env = {**os.environ, 'PROXY_AGENT_DSD_STATE_DIR': str(state), 'PROXY_AGENT_STUB_POOL_LIVE': '0', 'PROXY_AGENT_DEEPSEEK_DIRECT_SCOPE': 'bench:run-1'}
    for k in ('DEEPSEEK_DIRECT_PROXY_CAP', 'PROXY_AGENT_DSD_INFLIGHT_FLOOR_USD'):
        env.pop(k, None)
    return tmp_path, state, env


def gate(lab, *args, env=None, check=None):
    _tmp, _state, base = lab
    done = subprocess.run([sys.executable, '-I', str(GATE), *args], env={**base, **(env or {})}, capture_output=True, text=True, timeout=120)
    if check is not None:
        assert done.returncode == check, done.stdout + done.stderr
    return done


def init(lab, budget=25.0, spent=0.0):
    gate(lab, 'init-state', '--run-id', 'run-1', '--budget-usd', str(budget), '--spent-usd', str(spent), check=0)


def state_of(lab):
    return json.loads((lab[1] / 'deepseek-direct-bench.json').read_text())


def admit(lab, name, *extra, env=None):
    return gate(lab, 'admit', '--name', name, '--model', FLASH, *extra, env=env)


def fake_launcher(tmp):
    """A live process whose command line names proxy-agent.sh, standing in for a lane's launcher."""
    script = tmp / 'proxy-agent.sh'
    script.write_text('#!/bin/bash\nsleep 60\n')
    return subprocess.Popen(['bash', str(script)])


def test_no_token_a_malformed_token_and_another_runs_token_are_refused(lab):
    init(lab)
    for token, why in (('', 'scope'), ('run-1', 'scope'), ('bench:other-run', 'scope')):
        done = admit(lab, 'a', env={'PROXY_AGENT_DEEPSEEK_DIRECT_SCOPE': token})
        assert done.returncode == 2 and 'reason=deepseek_direct_' + why in done.stdout, (token, done.stdout)
    assert admit(lab, 'a', '--dry-run').returncode == 0  # the matching bench token


def test_a_missing_or_invalid_state_file_fails_closed(lab):
    assert admit(lab, 'a').returncode == 2 and 'reason=deepseek_direct_state' in admit(lab, 'a').stdout
    (lab[1] / 'deepseek-direct-bench.json').write_text('{"run_id": "run-1", "budget_usd": "lots", "spent_usd": 0, "stop": false}')
    assert admit(lab, 'a').returncode == 2


def test_a_start_and_a_continuation_both_read_the_stop_flag_now(lab):
    init(lab)
    launcher = fake_launcher(lab[0])
    try:
        assert admit(lab, 'L1', '--pid', str(launcher.pid)).returncode == 0
        gate(lab, 'set-stop', 'true', check=0)
        cont = admit(lab, 'L1', '--continue', '--pid', str(launcher.pid))
        assert cont.returncode == 2 and 'reason=deepseek_direct_stop' in cont.stdout and 'kind=continue' in cont.stdout
        assert admit(lab, 'L2', '--dry-run').returncode == 2
        gate(lab, 'set-stop', 'false', check=0)
        assert admit(lab, 'L1', '--continue', '--pid', str(launcher.pid)).returncode == 0
    finally:
        launcher.kill()


def test_a_continuation_sees_a_cap_lowered_after_the_lane_started(lab):
    init(lab)
    procs = [fake_launcher(lab[0]) for _ in range(3)]
    try:
        for i, p in enumerate(procs):
            assert admit(lab, 'L%d' % i, '--pid', str(p.pid)).returncode == 0
        (lab[1] / 'proxy-caps.env').write_text('DEEPSEEK_DIRECT_PROXY_CAP=2\n')  # edited while the lanes run: 2 others already fill it
        cont = admit(lab, 'L0', '--continue', '--pid', str(procs[0].pid))
        assert cont.returncode == 75 and 'reason=deepseek_direct_cap' in cont.stdout and '(2 live >= 2)' in cont.stdout
    finally:
        for p in procs:
            p.kill()


def test_parallel_starts_cannot_exceed_the_cap(lab):
    init(lab)
    env = {'DEEPSEEK_DIRECT_PROXY_CAP': '8'}
    procs = [subprocess.Popen([sys.executable, '-I', str(GATE), 'admit', '--name', 'n%d' % i, '--model', FLASH, '--dry-run', '--hold', '4'],
                              env={**lab[2], **env}, stdout=subprocess.PIPE, text=True) for i in range(10)]
    outs = [(p.communicate()[0], p.returncode) for p in procs]
    assert sorted(rc for _o, rc in outs) == [0] * 8 + [75] * 2, outs
    assert json.loads((lab[1] / 'deepseek-direct-lanes.json').read_text())['lanes'] == {}  # every hold was released


def test_the_budget_counts_spent_plus_the_in_flight_floor_of_every_running_lane(lab):
    init(lab, budget=1.0, spent=0.4)
    procs = [fake_launcher(lab[0]) for _ in range(3)]
    try:
        assert admit(lab, 'L0', '--pid', str(procs[0].pid)).returncode == 0  # 0.40 + 0.25 floor < 1.00
        assert admit(lab, 'L1', '--pid', str(procs[1].pid)).returncode == 0  # 0.40 + 2 * 0.25 < 1.00
        refused = admit(lab, 'L2', '--pid', str(procs[2].pid))  # 0.40 + 3 * 0.25 >= 1.00
        assert refused.returncode == 2 and 'reason=deepseek_direct_budget' in refused.stdout, refused.stdout
    finally:
        for p in procs:
            p.kill()


def test_settle_prices_the_usage_line_and_frees_the_reservation(lab):
    init(lab)
    launcher = fake_launcher(lab[0])
    try:
        logf = lab[0] / 'lane.log'
        logf.write_text(USAGE)
        assert admit(lab, 'L1', '--pid', str(launcher.pid), '--log', str(logf)).returncode == 0
        gate(lab, 'settle', '--name', 'L1', '--model', FLASH, '--log', str(logf), check=0)
        assert abs(state_of(lab)['spent_usd'] - USAGE_USD) < 1e-5  # the launcher calls settle once per lane, at its end
        assert json.loads((lab[1] / 'deepseek-direct-lanes.json').read_text())['lanes'] == {}
    finally:
        launcher.kill()


def test_a_result_event_that_does_not_start_with_its_type_is_priced_and_a_dead_lane_is_reconciled(lab):
    init(lab)
    launcher = fake_launcher(lab[0])
    jsonl = lab[0] / 'lane.jsonl'
    result = {'duration_api_ms': 1, 'usage': {'input_tokens': 100000, 'output_tokens': 30000, 'cache_read_input_tokens': 6000000, 'cache_creation_input_tokens': 0}, 'type': 'result'}
    jsonl.write_text(json.dumps(result) + '\n')
    assert admit(lab, 'L1', '--pid', str(launcher.pid), '--jsonl', str(jsonl)).returncode == 0
    launcher.kill()
    launcher.wait()
    time.sleep(0.2)
    assert admit(lab, 'L2', '--dry-run').returncode == 0  # the next admission settles L1 from its stream
    assert abs(state_of(lab)['spent_usd'] - USAGE_USD) < 1e-5
    assert 'L1' not in json.loads((lab[1] / 'deepseek-direct-lanes.json').read_text())['lanes']


def test_init_state_seeds_ended_lanes_exact_first_and_adopts_the_running_one_once(lab):
    tmp = lab[0]
    exact_log = tmp / 'claude-ended-exact.log'
    exact_log.write_text('[2026-11-25T09:56:35Z] LAUNCH name=ended-exact ISSUE:UNKNOWN model_alias=x model_id=' + FLASH + '\n' + USAGE)
    (tmp / 'claude-ended-exact.jsonl').write_text('')
    (tmp / 'claude-running-one.log').write_text('[2026-11-25T09:56:35Z] LAUNCH name=running-one ISSUE:UNKNOWN model_alias=x model_id=' + FLASH + '\n')
    (tmp / 'claude-running-one.jsonl').write_text('')
    fake = tmp / 'bin' / 'claude'
    fake.parent.mkdir()
    fake.write_text('#!/bin/bash\nsleep 60\n')
    fake.chmod(0o755)
    proc = subprocess.Popen([str(fake), '-p', '--name', 'running-one', '--model', FLASH])  # what the process table shows for a live lane
    try:
        time.sleep(0.3)
        done = gate(lab, 'init-state', '--run-id', 'run-1', '--budget-usd', '25', '--adopt-running', '--ended-jsonl', str(tmp / 'claude-running-one.jsonl'),
                    '--ended-jsonl', str(tmp / 'claude-ended-exact.jsonl'), env={'PROXY_AGENT_STUB_POOL_LIVE': ''}, check=0)
        report = json.loads(done.stdout)
        assert report['skipped_still_running'] == ['running-one'] and [s['name'] for s in report['adopted_running']] == ['running-one']
        assert [s['name'] for s in report['ended_seeded']] == ['ended-exact'] and report['ended_seeded'][0]['source'] == 'init_usage_line'
        assert abs(state_of(lab)['spent_usd'] - USAGE_USD) < 1e-5  # the ended lane's exact usage only; the running lane has a baseline of ~0 and counts from here
    finally:
        proc.kill()
