"""deepseek-direct-gate.py: the ONE admission boundary for paid deepseek-direct lanes.
CTO REVISE of 248ae25b (C0ALZJHGE49 thread 1795597777.761599, ts 1795600640.261439) and of a5c85804 (ts 1795603176.516159).

  scope      : no token, a malformed token or another run's token is refused (rc 2); only bench:<run_id> of the state file is admitted.
  stop       : a start and a continuation are both refused while the state file says stop; the flag is read at that moment.
  reservation: every segment reserves grant + one worst-case request at admission, so 24.70 spent with no live lane admits NOTHING, and 24.00 spent admits a
               lane with a smaller grant; the grant becomes the claude --max-budget-usd (cumulative for a resumed session).
  held       : a started lane that ends with no readable result keeps its reservation HELD (never settled at 0, never at an estimate) until reconcile;
               a lane that never reached its claude spawn is released.
  cap        : the cap is re-read from proxy-caps.env on every admission; parallel starts cannot exceed it (flock + reservations).
Each case drives the REAL script in a temp state dir (PROXY_AGENT_DSD_STATE_DIR), with the launcher's own process-table stub (PROXY_AGENT_STUB_POOL_LIVE).
Run: /opt/homebrew/bin/pytest -q -p no:cacheprovider tests/test_deepseek_direct_gate.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

GATE = Path(__file__).parents[1] / 'deepseek-direct-gate.py'
FLASH = 'deepseek-direct/deepseek-flash'
USAGE = 'PROXY_AGENT_USAGE model=%s results=1 input_tokens=100000 output_tokens=30000 cache_read_tokens=6000000 cache_creation_tokens=0 check=strict\n' % FLASH
USAGE_USD = 100000 * 0.30 / 1e6 + 6000000 * 0.006 / 1e6 + 30000 * 1.20 / 1e6  # the published peak rates: miss, cache hit, output (docstring of the gate)
RESULT = json.dumps({'duration_api_ms': 1, 'usage': {'input_tokens': 100000, 'output_tokens': 30000, 'cache_read_input_tokens': 6000000, 'cache_creation_input_tokens': 0},
                     'total_cost_usd': 4.38, 'type': 'result'}) + '\n'  # a real lane's shape: Claude Code priced it 4.38 for 0.102 of DeepSeek usage
ASSISTANT = json.dumps({'type': 'assistant', 'message': {'id': 'm1', 'usage': {'input_tokens': 90000, 'output_tokens': 0}, 'content': [{'type': 'text', 'text': 'working'}]}}) + '\n'

_spec = importlib.util.spec_from_file_location('dsd_gate_under_test', GATE)
G = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(G)
WORST = G.worst_request_usd(FLASH)
RESERVE = G.segment_max(FLASH) + WORST  # one full segment at budget 25: 1.00 + one worst-case request


@pytest.fixture
def lab(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    env = {**os.environ, 'PROXY_AGENT_DSD_STATE_DIR': str(state), 'PROXY_AGENT_STUB_POOL_LIVE': '0', 'PROXY_AGENT_DEEPSEEK_DIRECT_SCOPE': 'bench:run-1'}
    for k in ('DEEPSEEK_DIRECT_PROXY_CAP', 'PROXY_AGENT_DSD_SEGMENT_MAX_USD', 'PROXY_AGENT_DSD_MIN_SEGMENT_USD'):
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


def ledger_of(lab):
    return json.loads((lab[1] / 'deepseek-direct-lanes.json').read_text())


def admit(lab, name, *extra, env=None):
    grant = [] if ('--dry-run' in extra or '--grant-file' in extra) else ['--grant-file', str(lab[0] / ('grant-' + name))]  # the launcher always passes one
    return gate(lab, 'admit', '--name', name, '--model', FLASH, *extra, *grant, env=env)


def fake_launcher(tmp):
    """A live process whose command line names proxy-agent.sh, standing in for a lane's launcher."""
    script = tmp / 'proxy-agent.sh'
    script.write_text('#!/bin/bash\nsleep 60\n')
    return subprocess.Popen(['bash', str(script)])


def lane_files(tmp, name, stream='', log=''):
    jsonl, logf = tmp / (name + '.jsonl'), tmp / (name + '.log')
    jsonl.write_text(stream)
    logf.write_text(log)
    return ['--jsonl', str(jsonl), '--log', str(logf)]


def kill(proc):
    proc.kill()
    proc.wait()
    time.sleep(0.2)


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
        files = lane_files(lab[0], 'L1', RESULT)  # one finished segment: a continuation is possible
        assert admit(lab, 'L1', '--pid', str(launcher.pid), *files).returncode == 0
        gate(lab, 'set-stop', 'true', check=0)
        cont = admit(lab, 'L1', '--continue', '--pid', str(launcher.pid), *files)
        assert cont.returncode == 2 and 'reason=deepseek_direct_stop' in cont.stdout and 'kind=continue' in cont.stdout
        assert admit(lab, 'L2', '--dry-run').returncode == 2
        gate(lab, 'set-stop', 'false', check=0)
        assert admit(lab, 'L1', '--continue', '--pid', str(launcher.pid), *files).returncode == 0
    finally:
        launcher.kill()


def test_a_continuation_sees_a_cap_lowered_after_the_lane_started(lab):
    init(lab)
    procs = [fake_launcher(lab[0]) for _ in range(3)]
    try:
        files = [lane_files(lab[0], 'L%d' % i, RESULT) for i in range(3)]
        for i, p in enumerate(procs):
            assert admit(lab, 'L%d' % i, '--pid', str(p.pid), *files[i]).returncode == 0
        (lab[1] / 'proxy-caps.env').write_text('DEEPSEEK_DIRECT_PROXY_CAP=2\n')  # edited while the lanes run: 2 others already fill it
        cont = admit(lab, 'L0', '--continue', '--pid', str(procs[0].pid), *files[0])
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
    assert ledger_of(lab)['lanes'] == {}  # every hold was released


def test_a_start_near_the_ceiling_is_refused_and_a_start_just_below_it_gets_a_smaller_enforced_grant(lab):
    """CTO a5c85804: 24.70 settled and no live lane used to admit a lane at 24.95 that could spend more than 0.30 before the gate ran again."""
    init(lab, budget=25.0, spent=24.70)
    refused = admit(lab, 'L1')
    assert refused.returncode == 2 and 'reason=deepseek_direct_budget' in refused.stdout and 'headroom 0.3000' in refused.stdout, refused.stdout
    assert ledger_of(lab)['lanes'] == {}  # nothing was reserved
    init_state = lab[1] / 'deepseek-direct-bench.json'
    init_state.write_text(json.dumps({'run_id': 'run-1', 'budget_usd': 25.0, 'spent_usd': 24.00, 'stop': False}))
    launcher = fake_launcher(lab[0])
    try:
        grant_file = lab[0] / 'grant'
        ok = admit(lab, 'L1', '--pid', str(launcher.pid), '--grant-file', str(grant_file))
        assert ok.returncode == 0, ok.stdout
        grant = G.floor4(1.0 - WORST)  # headroom 1.00 minus the worst-case request the reservation also holds
        assert 'grant_usd=%.4f' % grant in ok.stdout and 'reserve_usd=%.4f' % (grant + WORST) in ok.stdout
        assert float(grant_file.read_text()) == G.floor4(grant * G.cc_per_usd(FLASH))  # the cap the launcher passes to claude, in Claude Code dollars
        second = admit(lab, 'L2', '--dry-run')  # L1's whole reserve is held: nothing is left for a second lane
        assert second.returncode == 2 and 'reason=deepseek_direct_budget' in second.stdout
    finally:
        launcher.kill()


def test_the_reservations_of_live_lanes_count_against_the_budget(lab):
    init(lab, budget=3.0)
    procs = [fake_launcher(lab[0]) for _ in range(3)]
    try:
        assert admit(lab, 'A', '--pid', str(procs[0].pid)).returncode == 0  # holds 1.00 + worst
        assert admit(lab, 'B', '--pid', str(procs[1].pid)).returncode == 0
        refused = admit(lab, 'C', '--pid', str(procs[2].pid))  # 3.00 - 2 x 1.353 = 0.294 < worst + the minimum segment
        assert refused.returncode == 2 and 'reason=deepseek_direct_budget' in refused.stdout, refused.stdout
    finally:
        for p in procs:
            p.kill()


def test_a_continuation_cap_is_the_sessions_cumulative_claude_code_cost_plus_the_grant(lab):
    init(lab)
    launcher = fake_launcher(lab[0])
    try:
        files = lane_files(lab[0], 'L1', RESULT)  # the finished segment: Claude Code total_cost_usd 4.38 (cumulative for the session)
        assert admit(lab, 'L1', '--pid', str(launcher.pid), *files).returncode == 0
        grant_file = lab[0] / 'grant'
        cont = admit(lab, 'L1', '--continue', '--pid', str(launcher.pid), *files, '--grant-file', str(grant_file))
        assert cont.returncode == 0 and 'cum_cc_usd=4.3800' in cont.stdout and 'segment=2' in cont.stdout, cont.stdout
        grant = G.floor4(min(G.segment_max(FLASH), 25.0 - USAGE_USD - WORST))
        assert float(grant_file.read_text()) == G.floor4(4.38 + grant * G.cc_per_usd(FLASH))
    finally:
        launcher.kill()


def test_a_continuation_of_a_lane_whose_previous_segment_has_no_result_is_refused(lab):
    init(lab)
    launcher = fake_launcher(lab[0])
    try:
        files = lane_files(lab[0], 'L1', ASSISTANT)  # the segment was killed: requests were made, no result event
        assert admit(lab, 'L1', '--pid', str(launcher.pid), *files).returncode == 0
        cont = admit(lab, 'L1', '--continue', '--pid', str(launcher.pid), *files)
        assert cont.returncode == 2 and 'reason=deepseek_direct_segment_unreconciled' in cont.stdout, cont.stdout
    finally:
        launcher.kill()


def test_a_killed_lane_with_no_readable_usage_keeps_its_reservation_held_never_settled_at_zero(lab):
    """CTO a5c85804: a killed lane with no readable usage used to settle at 0 and release its reservation."""
    init(lab, budget=25.0)
    launcher = fake_launcher(lab[0])
    files = lane_files(lab[0], 'L1', ASSISTANT, 'PROXY_AGENT_USAGE model=%s unavailable (no result event with usage in the stream)\n' % FLASH)
    assert admit(lab, 'L1', '--pid', str(launcher.pid), *files).returncode == 0
    gate(lab, 'started', '--name', 'L1', check=0)
    kill(launcher)
    status = json.loads(gate(lab, 'status', check=0).stdout)  # the next look at the ledger closes the dead lane
    assert state_of(lab)['spent_usd'] == 0.0, 'a lane with unknown usage must not be settled at 0'
    held = status['held']['L1']
    assert abs(held['usd'] - RESERVE) < 1e-4 and held['unknown_segments'] == 1 and status['live_lanes'] == 0
    assert abs(status['exposure_usd'] - RESERVE) < 1e-4  # still counted against the budget
    # ...so it still blocks admission when it is most of what is left.
    (lab[1] / 'deepseek-direct-bench.json').write_text(json.dumps({'run_id': 'run-1', 'budget_usd': 1.6, 'spent_usd': 0.0, 'stop': False}))
    blocked = admit(lab, 'L2', '--dry-run')
    assert blocked.returncode == 2 and 'reason=deepseek_direct_budget' in blocked.stdout and 'held %.4f' % RESERVE in blocked.stdout, blocked.stdout
    # reconcile with evidence releases it
    done = gate(lab, 'reconcile', '--name', 'L1', '--usd', '0.21', '--evidence', 'balance_delta', check=0)
    assert json.loads(done.stdout)['reconciled'][0]['charged_usd'] == 0.21 and state_of(lab)['spent_usd'] == 0.21
    assert ledger_of(lab)['held'] == {}
    assert admit(lab, 'L2', '--dry-run').returncode == 0


def test_reconcile_can_charge_the_reservation_in_full_for_one_lane_or_all(lab):
    init(lab)
    procs = [fake_launcher(lab[0]) for _ in range(2)]
    for i, p in enumerate(procs):
        assert admit(lab, 'L%d' % i, '--pid', str(p.pid), *lane_files(lab[0], 'L%d' % i, ASSISTANT)).returncode == 0
        gate(lab, 'started', '--name', 'L%d' % i, check=0)
        kill(p)
    gate(lab, 'status', check=0)
    assert set(ledger_of(lab)['held']) == {'L0', 'L1'}
    assert gate(lab, 'reconcile', '--all').returncode == 2  # --all alone is refused: it needs an explicit charge basis
    one = json.loads(gate(lab, 'reconcile', '--name', 'L0', '--charge-reservation', check=0).stdout)
    assert abs(one['reconciled'][0]['charged_usd'] - RESERVE) < 1e-4
    rest = json.loads(gate(lab, 'reconcile', '--all', '--charge-reservation', check=0).stdout)
    assert [r['name'] for r in rest['reconciled']] == ['L1'] and abs(state_of(lab)['spent_usd'] - 2 * RESERVE) < 1e-4 and rest['held_usd'] == 0


# CTO REVISE of 56d3ff19 (ts 1795605169.603049): ONE strict usage check for settlement and continuation. Empty, malformed or otherwise unverified usage retains the reservation.
OLD_SUMMARY = 'results=1 input_tokens=0 output_tokens=0 cache_read_tokens=0 cache_creation_tokens=0'  # what the pre-fix launcher printed for an empty or malformed usage object
UNVERIFIED = {
    'empty object': {},
    'input_tokens malformed, alone': {'input_tokens': 'lots'},
    'input_tokens malformed, output valid': {'input_tokens': 'lots', 'output_tokens': 5},
    'input_tokens is a bool': {'input_tokens': True, 'output_tokens': 5},
    'input_tokens is a float': {'input_tokens': 1000.5, 'output_tokens': 5},
    'input_tokens negative': {'input_tokens': -1, 'output_tokens': 5},
    'output_tokens missing': {'input_tokens': 1000},
    'cache count malformed': {'input_tokens': 1000, 'output_tokens': 5, 'cache_read_input_tokens': 'x'},
    'cache count null': {'input_tokens': 1000, 'output_tokens': 5, 'cache_creation_input_tokens': None},
    'zero prompt': {'input_tokens': 0, 'output_tokens': 0},
    'not an object': 'lots',
}


@pytest.mark.parametrize('usage', list(UNVERIFIED.values()), ids=list(UNVERIFIED))
def test_strict_usage_rejects_every_unverified_shape(usage):
    assert G.strict_usage(usage) is None


def test_strict_usage_accepts_verified_positive_usage_and_defaults_absent_cache_counts():
    assert G.strict_usage({'input_tokens': 1000, 'output_tokens': 200}) == {'input_tokens': 1000, 'output_tokens': 200, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}
    assert G.strict_usage({'input_tokens': 0, 'output_tokens': 0, 'cache_read_input_tokens': 50000})['cache_read_input_tokens'] == 50000


def test_the_usage_line_parser_trusts_only_strict_lines_with_a_positive_prompt(tmp_path):
    logf = tmp_path / 'l.log'
    for text, ok in ((USAGE, True), ('PROXY_AGENT_USAGE model=%s %s\n' % (FLASH, OLD_SUMMARY), False),  # an all-zero summary
                     (USAGE.replace(' check=strict', ''), False),  # a line from a launcher that counted any usage object
                     (USAGE.replace('results=1', 'results=0'), False), ('PROXY_AGENT_USAGE model=%s unavailable\n' % FLASH, False)):
        logf.write_text(text)
        assert (G.usage_from_log(str(logf)) is not None) == ok, text


@pytest.mark.parametrize('summary', [None, OLD_SUMMARY, OLD_SUMMARY + ' check=strict'], ids=['stream only', 'old launcher summary', 'zero summary even if tagged'])
@pytest.mark.parametrize('usage', list(UNVERIFIED.values()), ids=list(UNVERIFIED))
def test_unverified_usage_retains_the_reservation_at_settlement_and_refuses_a_continuation(lab, usage, summary):
    """The same strict check on both paths. Settlement: spent_usd stays 0 and the whole reserve stays held. Continuation: refused (segment_unreconciled), never admitted."""
    result = json.dumps({'usage': usage, 'total_cost_usd': 4.38, 'type': 'result'}) + '\n'
    log = 'PROXY_AGENT_USAGE model=%s %s\n' % (FLASH, summary) if summary else 'PROXY_AGENT_USAGE model=%s unavailable\n' % FLASH
    for path in ('continue', 'settle'):
        sub = lab[0] / path
        sub.mkdir()
        state2 = lab[1] / path
        state2.mkdir()
        lab2 = (sub, state2, {**lab[2], 'PROXY_AGENT_DSD_STATE_DIR': str(state2)})  # each path gets its own state dir
        init(lab2)
        launcher = fake_launcher(sub)
        try:
            files = lane_files(sub, 'L1', result, log)
            assert admit(lab2, 'L1', '--pid', str(launcher.pid), *files).returncode == 0
            gate(lab2, 'started', '--name', 'L1', check=0)
            if path == 'continue':
                cont = admit(lab2, 'L1', '--continue', '--pid', str(launcher.pid), *files)
                assert cont.returncode == 2 and 'reason=deepseek_direct_segment_unreconciled' in cont.stdout, cont.stdout
            else:
                gate(lab2, 'settle', '--name', 'L1', '--model', FLASH, '--log', files[3], check=0)
                assert state_of(lab2)['spent_usd'] == 0.0
                assert abs(ledger_of(lab2)['held']['L1']['usd'] - RESERVE) < 1e-4
        finally:
            launcher.kill()


def test_a_result_event_without_usage_is_unreadable_not_zero(lab):
    init(lab)
    launcher = fake_launcher(lab[0])
    files = lane_files(lab[0], 'L1', json.dumps({'type': 'result', 'result': 'done'}) + '\n', 'PROXY_AGENT_USAGE model=%s unavailable\n' % FLASH)
    assert admit(lab, 'L1', '--pid', str(launcher.pid), *files).returncode == 0
    gate(lab, 'started', '--name', 'L1', check=0)
    gate(lab, 'settle', '--name', 'L1', '--model', FLASH, '--log', files[3], check=0)
    assert state_of(lab)['spent_usd'] == 0.0 and list(ledger_of(lab)['held']) == ['L1']
    launcher.kill()


def test_a_segment_that_ended_on_the_budget_stop_holds_the_unreported_crossing_request(lab):
    """Measured with the real claude: error_max_budget_usd reports usage for the requests BEFORE the crossing one only (error_max_turns reports all)."""
    init(lab)
    launcher = fake_launcher(lab[0])
    capped = RESULT.replace('"type": "result"', '"subtype": "error_max_budget_usd", "type": "result"')
    files = lane_files(lab[0], 'L1', capped, USAGE)
    assert admit(lab, 'L1', '--pid', str(launcher.pid), *files).returncode == 0
    gate(lab, 'started', '--name', 'L1', check=0)
    gate(lab, 'settle', '--name', 'L1', '--model', FLASH, '--log', files[3], check=0)
    assert abs(state_of(lab)['spent_usd'] - USAGE_USD) < 1e-5  # what the result reports is spent
    held = ledger_of(lab)['held']['L1']
    assert abs(held['usd'] - WORST) < 1e-4 and held['capped_segments'] == 1  # the crossing request: one worst-case request, until reconcile
    assert 'cost_basis_violation' not in ledger_of(lab)  # the ratio check skips a segment whose usage is incomplete
    launcher.kill()


def test_a_lane_that_never_reached_its_claude_spawn_is_released_not_held(lab):
    init(lab)
    launcher = fake_launcher(lab[0])
    assert admit(lab, 'L1', '--pid', str(launcher.pid), *lane_files(lab[0], 'L1')).returncode == 0  # admitted, but no started mark: the runner died first
    kill(launcher)
    status = json.loads(gate(lab, 'status', check=0).stdout)
    assert status['held'] == {} and status['live_lanes'] == 0 and state_of(lab)['spent_usd'] == 0.0


def test_a_runner_that_cannot_receive_the_segment_cap_is_refused(lab):
    init(lab)
    launcher = fake_launcher(lab[0])
    try:
        done = gate(lab, 'admit', '--name', 'L1', '--model', FLASH, '--pid', str(launcher.pid))  # no --grant-file: a runner baked before the cap existed
        assert done.returncode == 2 and 'reason=deepseek_direct_no_grant_channel' in done.stdout and ledger_of(lab)['lanes'] == {}
        assert gate(lab, 'admit', '--name', 'L1', '--model', FLASH, '--dry-run').returncode == 0  # a preview needs no channel
    finally:
        launcher.kill()


def test_a_lane_admitted_by_the_previous_gate_version_that_dies_with_no_result_is_held_not_released(lab):
    init(lab)
    launcher = fake_launcher(lab[0])
    legacy = {'name': 'old', 'model': FLASH, 'owner': 'launcher', 'pid': launcher.pid, 'jsonl': None, 'log': None, 'baseline_usd': 0.0, 'missing_since': None,
              'acc': {'off': 0, 'last_mid': None, 'a_in': 0, 'a_out': 0, 'a_cr': 0, 'a_cc': 0, 'r_in': 0, 'r_out': 0, 'r_cr': 0, 'r_cc': 0}, 'acc_c': {'off': 0}}
    (lab[1] / 'deepseek-direct-lanes.json').write_text(json.dumps({'version': 1, 'lanes': {'old': legacy}, 'settled': []}))  # the a5c85804 ledger shape
    kill(launcher)
    status = json.loads(gate(lab, 'status', check=0).stdout)
    assert list(status['held']) == ['old'] and state_of(lab)['spent_usd'] == 0.0 and status['held']['old']['usd'] > 0


def test_started_without_a_reservation_fails_closed(lab):
    init(lab)
    done = gate(lab, 'started', '--name', 'nobody')
    assert done.returncode == 2 and 'reason=deepseek_direct_no_reservation' in done.stdout


def test_settle_prices_the_usage_line_and_frees_the_reservation(lab):
    init(lab)
    launcher = fake_launcher(lab[0])
    try:
        files = lane_files(lab[0], 'L1', '', USAGE)
        assert admit(lab, 'L1', '--pid', str(launcher.pid), *files).returncode == 0
        gate(lab, 'started', '--name', 'L1', check=0)
        gate(lab, 'settle', '--name', 'L1', '--model', FLASH, '--log', files[3], check=0)
        assert abs(state_of(lab)['spent_usd'] - USAGE_USD) < 1e-5  # the launcher calls settle once per lane, at its end
        assert ledger_of(lab)['lanes'] == {} and ledger_of(lab)['held'] == {}
    finally:
        launcher.kill()


def test_a_lane_killed_after_its_result_event_is_settled_exactly_from_the_stream(lab):
    init(lab)
    launcher = fake_launcher(lab[0])
    files = lane_files(lab[0], 'L1', RESULT)  # the result event does not start with its type: the keys come alphabetically
    assert admit(lab, 'L1', '--pid', str(launcher.pid), *files).returncode == 0
    gate(lab, 'started', '--name', 'L1', check=0)
    kill(launcher)
    status = json.loads(gate(lab, 'status', check=0).stdout)
    assert abs(state_of(lab)['spent_usd'] - USAGE_USD) < 1e-5 and status['held'] == {} and status['live_lanes'] == 0


def test_a_lane_priced_below_the_assumed_claude_code_floor_stops_admission_until_cleared(lab):
    init(lab)
    cheap = RESULT.replace('4.38', '0.12')  # Claude Code total 0.12 for 0.102 of DeepSeek usage: a ratio of ~1.2, far below k
    launcher = fake_launcher(lab[0])
    files = lane_files(lab[0], 'L1', cheap, USAGE)
    assert admit(lab, 'L1', '--pid', str(launcher.pid), *files).returncode == 0
    gate(lab, 'started', '--name', 'L1', check=0)
    gate(lab, 'settle', '--name', 'L1', '--model', FLASH, '--log', files[3], check=0)
    assert 'cost_basis_violation' in ledger_of(lab)
    blocked = admit(lab, 'L2', '--dry-run')
    assert blocked.returncode == 2 and 'reason=deepseek_direct_cost_basis' in blocked.stdout
    gate(lab, 'reconcile', '--clear-cost-basis', check=0)
    assert admit(lab, 'L2', '--dry-run').returncode == 0
    launcher.kill()


def test_init_state_seeds_exact_ended_lanes_holds_unreadable_ones_and_adopts_the_running_one_once(lab):
    tmp = lab[0]
    launch_line = '[2026-11-25T09:56:35Z] LAUNCH name=%s ISSUE:UNKNOWN model_alias=x model_id=' + FLASH + '\n'
    (tmp / 'claude-ended-exact.log').write_text(launch_line % 'ended-exact' + USAGE)
    (tmp / 'claude-ended-exact.jsonl').write_text(RESULT)
    (tmp / 'claude-ended-dead.log').write_text(launch_line % 'ended-dead' + 'PROXY_AGENT_USAGE model=%s unavailable\n' % FLASH)
    (tmp / 'claude-ended-dead.jsonl').write_text(ASSISTANT)
    (tmp / 'claude-running-one.log').write_text(launch_line % 'running-one')
    (tmp / 'claude-running-one.jsonl').write_text('')
    fake = tmp / 'bin' / 'claude'
    fake.parent.mkdir()
    fake.write_text('#!/bin/bash\nsleep 60\n')
    fake.chmod(0o755)
    proc = subprocess.Popen([str(fake), '-p', '--name', 'running-one', '--model', FLASH])  # what the process table shows for a live lane
    try:
        time.sleep(0.3)
        done = gate(lab, 'init-state', '--run-id', 'run-1', '--budget-usd', '25', '--adopt-running', '--ended-jsonl', str(tmp / 'claude-running-one.jsonl'),
                    '--ended-jsonl', str(tmp / 'claude-ended-dead.jsonl'), '--ended-jsonl', str(tmp / 'claude-ended-exact.jsonl'), env={'PROXY_AGENT_STUB_POOL_LIVE': ''}, check=0)
        report = json.loads(done.stdout)
        # (the real process table is read here, so unrelated live deepseek-direct lanes may be listed too: assert on ours)
        assert 'running-one' in report['skipped_still_running'] and 'running-one' in [s['name'] for s in report['adopted_running']]
        assert abs(state_of(lab)['spent_usd'] - USAGE_USD) < 1e-5  # only the exact lane is spent; the unreadable one is HELD, the running one is not counted yet
        assert list(report['held']) == ['ended-dead']
    finally:
        proc.kill()