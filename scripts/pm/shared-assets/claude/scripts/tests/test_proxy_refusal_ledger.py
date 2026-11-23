"""A refused launch (rc 75) writes its CAPACITY_WAIT relaunch row and closes its proxy_run row; an unreadable Claude pool count fails CLOSED.

PM add-ons 2026-11-23 (refusals at the Claude pool cap, 20:04-20:10 IST, printed PROXY_AGENT_WARN reason=pm_ops_failed):
  cause   : proxy-run-state.py closed the proxy_run row with `obligation-resolve --kind proxy_run --target-type proxy --target-id <name>` and NO --issue/--pr, but the
            row is stored WITH them (the brief's ISSUE:/PR: header), and pm-ops resolves by an EXACT stored-key match ("obligation-resolve matched no open row ...").
            Every refusal of a brief that names an issue or PR therefore warned and left its proxy_run row open. The relaunch row itself was written, but the
            warning (and the hand-written rows that followed) said otherwise. The strict stub below enforces the same exact-key rule as pm-ops.
  closed  : proxy_agent_pool_live read ps with text=True; a non-UTF-8 / mid-character argv raised UnicodeDecodeError, the count was "unreadable" and
            proxy_agent_pool_blocked failed OPEN for Claude/Grok/Codex ("pool_cap UNAVAILABLE pool=claude"), so Claude lanes launched past a cap of 2.
            Now ps is decoded with errors=replace AND an unreadable Claude count blocks: the picker skips the id, a Claude primary is refused rc 75, and the
            state machine queues the CAPACITY_WAIT relaunch row. Grok/Codex stay fail-open; every role exemption stays.
Real proxy-agent.sh in the lane-launch lab (fake claude, real tmux, strict pm-ops stub); no model, no Slack, no real pm-ops DB.
Run: /opt/homebrew/bin/pytest -q -p no:cacheprovider tests/test_proxy_refusal_ledger.py
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1]
HERE = Path(__file__).parent
LAUNCHER = (SCRIPTS / 'proxy-agent.sh').read_text()

spec = importlib.util.spec_from_file_location('launch_states_for_refusal_ledger', HERE / 'test_proxy_lane_launch_states.py')
launch_states = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch_states)
lab = launch_states.lab  # fixture: temp HOME + fake claude + real tmux; brief.md names ISSUE: 14461 and PR: 14470

STRICT_STUB = '''import json, os, sys
argv = sys.argv[1:]
open(os.environ['PROXY_TEST_STUB_LOG'], 'a').write(json.dumps(argv) + "\\n")
store = os.environ['PROXY_TEST_STORE']
rows = json.load(open(store)) if os.path.exists(store) else []
def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None
key = [opt('--kind'), opt('--target-type'), opt('--target-id'), opt('--issue'), opt('--pr')]
verb = argv[0]
flaky = os.environ.get('PROXY_TEST_FAIL_FIRST_RELAUNCH')
if verb == 'obligation-upsert':
    if flaky and key[0] == 'proxy_relaunch' and not os.path.exists(flaky):
        open(flaky, 'w').write('1')
        print('database is locked', file=sys.stderr)
        sys.exit(1)
    if not any(r['key'] == key and r['status'] == 'open' for r in rows):
        rows.append({'key': key, 'status': 'open'})
    json.dump(rows, open(store, 'w'))
    if '--print-id' in argv:
        print(42)
elif verb == 'obligation-resolve':
    row = next((r for r in rows if r['key'] == key and r['status'] == 'open'), None)
    if row is None:
        print('obligation-resolve matched no open row kind=%s target_type=%s target_id=%s pr=%s issue=%s; key-based resolve requires an exact stored-key match' % (key[0], key[1], key[2], key[4], key[3]), file=sys.stderr)
        sys.exit(1)
    row['status'] = 'resolved'
    json.dump(rows, open(store, 'w'))
'''
COUNT_STUB = 'import os, sys\nprint(os.environ.get("PROXY_TEST_LIVE_" + sys.argv[1].upper(), "0"))\n'


def prepare(lab, role='hd-builder'):
    tmp, home, _env = lab
    (tmp / 'stub.py').write_text(STRICT_STUB)  # the lab's PROXY_AGENT_PM_OPS_CLI
    (home / '.claude/scripts/proxy-cap-count.py').write_text(COUNT_STUB)
    (home / '.claude/agents' / f'{role}.md').write_text('---\ntools: Bash\n---\nTest worker.\n')


def launch(lab, role, name, model, **env):
    tmp, _home, base = lab
    return subprocess.run(['bash', str(SCRIPTS / 'proxy-agent.sh'), role, str(tmp / 'brief.md'), '--out', str(tmp / f'{name}.md'), '--model', model],
                          env={**base, 'PROXY_AGENT_NAME': name, 'PROXY_AGENT_PIN_STRICT': '1', 'PROXY_TEST_STORE': str(tmp / 'store.json'),
                               'PROXY_TEST_LIVE_CLAUDE': '0', 'PROXY_TEST_LIVE_TOTAL': '0', 'PROXY_TEST_SLEEP': '0.3', **env},
                          capture_output=True, text=True, timeout=240)


def store(lab):
    path = lab[0] / 'store.json'
    return [{'kind': r['key'][0], 'target': r['key'][2], 'issue': r['key'][3], 'pr': r['key'][4], 'status': r['status']} for r in json.loads(path.read_text())]


def assert_refusal_is_fully_recorded(lab, done, name):
    """rc 75, no ledger warning, the relaunch row OPEN with the brief's refs, the proxy_run row RESOLVED under the same refs."""
    assert done.returncode == 75, done.stderr
    assert 'pm_ops_failed' not in done.stderr and 'pm_ops_unavailable' not in done.stderr, done.stderr
    rows = [r for r in store(lab) if r['target'] == name]
    assert {(r['kind'], r['status'], r['issue'], r['pr']) for r in rows} == {('proxy_relaunch', 'open', '14461', '14470'), ('proxy_run', 'resolved', '14461', '14470')}, rows
    assert not (lab[1] / 'prompt-seen.txt').exists()  # no lane started


def test_a_claude_pool_cap_refusal_writes_the_relaunch_row_and_closes_the_run_row_without_a_ledger_warning(lab):
    prepare(lab)
    done = launch(lab, 'hd-builder', 'proxy-test-claude-cap', 'claude-sonnet-5-5', PROXY_TEST_LIVE_CLAUDE='2', PROXY_AGENT_CLAUDE_CAP='2')
    assert 'PROXY_AGENT_REFUSED reason=claude_cap' in done.stderr, done.stderr
    assert_refusal_is_fully_recorded(lab, done, 'proxy-test-claude-cap')


def test_a_codex_pool_cap_refusal_is_recorded_the_same_way(lab):
    prepare(lab, 'test')
    done = launch(lab, 'test', 'proxy-test-codex-cap', 'gpt-6.1-terra', PROXY_AGENT_STUB_POOL_LIVE='8', CODEX_PROXY_CAP='8')
    assert 'REFUSED primary gpt-6.1-terra (codex pool_cap)' in done.stderr, done.stderr
    assert_refusal_is_fully_recorded(lab, done, 'proxy-test-codex-cap')


def test_the_relaunch_write_is_retried_once_because_it_is_the_only_record_of_the_refusal(lab):
    prepare(lab)
    done = launch(lab, 'hd-builder', 'proxy-test-flaky', 'claude-sonnet-5-5', PROXY_TEST_LIVE_CLAUDE='2', PROXY_AGENT_CLAUDE_CAP='2',
                  PROXY_TEST_FAIL_FIRST_RELAUNCH=str(lab[0] / 'failed-once'))
    assert (lab[0] / 'failed-once').exists() and 'verb=obligation-upsert detail=database is locked' in done.stderr  # the first write failed, with its cause shown
    rows = [r for r in store(lab) if r['target'] == 'proxy-test-flaky']
    assert ('proxy_relaunch', 'open') in {(r['kind'], r['status']) for r in rows} and ('proxy_run', 'resolved') in {(r['kind'], r['status']) for r in rows}


def test_a_ledger_refusal_shows_which_call_failed_and_why(lab):
    """The old warning was a bare 'pm_ops_failed': the cause (the pm-ops CLI's own refusal) is now kept, one line."""
    tmp, home, base = lab
    prepare(lab)
    env = {**base, 'PROXY_TEST_STORE': str(tmp / 'store.json'), 'PROXY_AGENT_RUN_STATE_DIR': str(tmp / 'runs'), 'PROXY_AGENT_PM_OPS_CLI': str(tmp / 'stub.py')}
    helper = SCRIPTS / 'proxy-run-state.py'
    run = lambda *a: subprocess.run([sys.executable, '-I', str(helper), *a], env=env, capture_output=True, text=True, timeout=60)
    run('acquire', '--name', 'proxy-test-detail', '--token', 't1')
    queued = run('queue', '--name', 'proxy-test-detail', '--token', 't1', '--type', 'hd-builder', '--brief', str(tmp / 'brief.md'))
    assert queued.returncode == 0 and 'pm_ops_failed' not in queued.stderr
    # close a row that was stored with refs but WITHOUT passing them: the pm-ops CLI refuses and the warning carries its message
    bare = subprocess.run([sys.executable, str(tmp / 'stub.py'), 'obligation-resolve', '--kind', 'proxy_run', '--target-type', 'proxy', '--target-id', 'proxy-test-detail'],
                          env=env, capture_output=True, text=True)
    assert bare.returncode == 1 and 'exact stored-key match' in bare.stderr


def test_every_proxy_run_resolve_carries_the_refs_the_row_was_stored_with():
    """Structural, over every obligation-resolve of a proxy_run row in the helper (no hand-kept list): each passes the stored issue/pr key."""
    src = (SCRIPTS / 'proxy-run-state.py').read_text()
    calls = re.findall(r"ledger\(\['obligation-resolve', '--kind', 'proxy_run'[^\n]*", src)
    assert len(calls) >= 3 and all('*ref_args(data)' in c for c in calls), calls
    assert 'extra = ref_args(data)' in src  # the upsert builds its key from the same helper


# ---- unreadable Claude count fails closed ---------------------------------------------------------------------------------------------

def fn(name):
    return re.search(rf'^{name}\(\) \{{.*?^\}}$', LAUNCHER, re.S | re.M).group(0) + '\n'


FUNCS = ''.join(fn(n) for n in ('proxy_agent_router_identity', 'proxy_agent_pool_blocked', 'proxy_agent_cap_exempt', 'proxy_agent_p0_preempt', 'proxy_agent_is_p0_launch'))


def blocked(model, role='hd-builder', live='unreadable'):
    script = (f'set -u; TYPE={role}; ROLE_BRIEF=/nonexistent; PA_P0_REF=""; PA_P0_ARMED=0; PA_P0_WAIT=""\n'
              'proxy_agent_pool_live() { if [[ "$LIVE" == unreadable ]]; then return 1; fi; printf "%s\\n" "$LIVE"; }\n' + FUNCS +
              f'if proxy_agent_pool_blocked {model}; then echo BLOCKED; else echo FREE; fi')
    env = {**os.environ, 'LIVE': live, 'CLAUDE_PROXY_CAP': '2', 'CODEX_PROXY_CAP': '2', 'GROK_PROXY_CAP': '2', 'OPENCODE_PROXY_CAP': '2'}
    done = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True, timeout=30)
    return done.stdout.split()[-1], done.stderr


@pytest.mark.parametrize('model', ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5', 'claude-fable-5-2'])
def test_an_unreadable_claude_count_blocks_every_claude_id(model):
    state, err = blocked(model)
    assert state == 'BLOCKED' and 'pool_cap UNAVAILABLE pool=claude' in err and 'fail-closed pool=claude' in err


def test_a_readable_count_still_decides_and_the_other_pools_keep_their_behaviour():
    assert blocked('claude-opus-5-5', live='1')[0] == 'FREE' and blocked('claude-opus-5-5', live='2')[0] == 'BLOCKED'
    assert blocked('gpt-6.2-sol')[0] == 'FREE' and blocked('grok-4.7')[0] == 'FREE'  # Grok/Codex: unchanged fail-open
    assert blocked('deepseek-v4.1-flash')[0] == 'BLOCKED'  # OpenCode: already fail-closed


@pytest.mark.parametrize('role', ['pm-admission-reviewer', 'ci-repair-agent', 'growth'])
def test_a_cap_exempt_role_is_never_blocked_by_an_unreadable_count(role):
    assert blocked('claude-opus-5-5', role=role)[0] == 'FREE'


def test_a_claude_primary_with_an_unreadable_count_is_refused_75_and_queued_with_its_launch_args(lab):
    prepare(lab)
    done = launch(lab, 'hd-builder', 'proxy-test-unreadable', 'claude-sonnet-5-5', PROXY_AGENT_STUB_POOL_LIVE='unreadable', PROXY_AGENT_CLAUDE_CAP='5')
    assert 'pool_cap fail-closed pool=claude' in done.stderr and 'REFUSED primary claude-sonnet-5-5' in done.stderr, done.stderr
    assert_refusal_is_fully_recorded(lab, done, 'proxy-test-unreadable')
    (relaunch,) = [json.loads(x) for x in (lab[0] / 'calls.jsonl').read_text().splitlines() if '"proxy_relaunch"' in x]
    spec = json.loads(relaunch[relaunch.index('--evidence-json') + 1])['relaunch']
    assert spec['type'] == 'hd-builder' and spec['model_args'] == ['--model', 'claude-sonnet-5-5']


def test_the_pool_count_decodes_ps_with_errors_replace():
    """A non-UTF-8 argv must not make the count unreadable: the cause of 'pool_cap UNAVAILABLE'."""
    body = fn('proxy_agent_pool_live')
    call = next(ln for ln in body.splitlines() if 'check_output(' in ln and '/bin/ps' in ln)
    assert 'encoding="utf-8"' in call and 'errors="replace"' in call and 'text=True' not in call, call