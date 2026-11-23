"""Per-lane stop fence (proxy-fence.py) and its gates: P0 preemption fences a lane (name + run id) before the custody re-check and the SIGTERM,
and a fenced lane cannot START a box lease, a box or a push (CTO C0ALZJHGE49 thread 1795441441.525659, ts 1795441848.817099).

  library : exclusive set (a live fence refuses a second), expiry, clear only by the setter, keyed by name + run id (a relaunch is unfenced)
  check   : rc 75 for a fenced lane (env identity, or the stripped-env ancestry fallback), 0 for a non-lane / another run / an expired fence / any error
  hook    : the lanes' PreToolUse Bash hook blocks (rc 2) git push, gh pr create|merge|edit|ready and pm-admit-ci.sh only for a fenced lane
  gates   : rtb-pool-lease.py fresh acquire2 and borrow, remote-test-box.sh up, remote-browser-box.sh up|once, and the launcher's hook wiring
            (the deployed, non-MoP-tracked scripts are checked where they exist; the structural checks discover every up/once dispatch)
Run: /opt/homebrew/bin/pytest -q -p no:cacheprovider tests/test_proxy_fence.py
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
FENCE_SRC = SCRIPTS / 'proxy-fence.py'
LAUNCHER = (SCRIPTS / 'proxy-agent.sh').read_text()
DEPLOYED = Path.home() / '.claude/scripts'
NOW = 1_795_400_000.0

spec = importlib.util.spec_from_file_location('proxy_fence_under_test', FENCE_SRC)
fence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fence)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A scratch HOME holding proxy-fence.py where the gates look for it, a run registry and a fence dir; every gate sees only this."""
    (tmp_path / '.claude/scripts').mkdir(parents=True)
    (tmp_path / '.claude/scripts/proxy-fence.py').write_text(FENCE_SRC.read_text())
    (tmp_path / 'runs').mkdir()
    monkeypatch.setattr(fence, 'FENCE_DIR', tmp_path / 'fences')
    monkeypatch.setattr(fence, 'RUNS', tmp_path / 'runs')
    return tmp_path


def lane_env(home, name='lane-a', token='tok-a', **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith('PROXY_')}
    env.update(HOME=str(home), PROXY_FENCE_DIR=str(home / 'fences'), PROXY_AGENT_RUN_STATE_DIR=str(home / 'runs'), PROXY_AGENT_NAME=name,
               PROXY_AGENT_RUN_TOKEN=token, **extra)
    return env


def fence_lane(home, name='lane-a', token='tok-a', ttl=600, now=None, by='p0-launch'):
    return fence.set_fence(home / 'fences', name, token, by, ttl, now=now)


# ---- library ----------------------------------------------------------------------------------------------------------------------------

def test_set_is_exclusive_and_a_live_fence_refuses_a_second_setter(home):
    assert fence_lane(home, now=NOW) == (True, 'set')
    assert fence_lane(home, now=NOW + 5, by='another') == (False, 'fence_held')
    assert fence.read_fence(home / 'fences', 'lane-a', 'tok-a', NOW + 5)['by'] == 'p0-launch'


def test_a_fence_expires_by_itself_and_an_expired_one_can_be_replaced(home):
    fence_lane(home, ttl=60, now=NOW)
    assert fence.read_fence(home / 'fences', 'lane-a', 'tok-a', NOW + 59) is not None
    assert fence.read_fence(home / 'fences', 'lane-a', 'tok-a', NOW + 61) is None  # a preemptor that died cannot wedge the lane
    assert fence_lane(home, now=NOW + 61, by='next') == (True, 'set')


def test_the_key_is_lane_name_plus_run_id_so_a_relaunch_is_not_fenced(home):
    fence_lane(home, 'lane-a', 'tok-old', now=NOW)
    assert fence.read_fence(home / 'fences', 'lane-a', 'tok-old', NOW) is not None
    assert fence.read_fence(home / 'fences', 'lane-a', 'tok-new', NOW) is None  # same lane name, new run id
    assert fence.read_fence(home / 'fences', 'lane-b', 'tok-old', NOW) is None


def test_clear_removes_only_the_fence_its_setter_put_up(home):
    fence_lane(home, now=NOW, by='p0-launch')
    assert fence.clear_fence(home / 'fences', 'lane-a', 'tok-a', by='someone-else') is False
    assert fence.read_fence(home / 'fences', 'lane-a', 'tok-a', NOW) is not None
    assert fence.clear_fence(home / 'fences', 'lane-a', 'tok-a', by='p0-launch') is True
    assert fence.read_fence(home / 'fences', 'lane-a', 'tok-a', NOW) is None
    assert fence.clear_fence(home / 'fences', 'lane-a', 'tok-a') is True  # idempotent


@pytest.mark.parametrize('name,token', [('../x', 't'), ('a/b', 't'), ('', 't'), ('a', ''), ('a', 'x/y')])
def test_an_invalid_key_never_touches_the_filesystem(home, name, token):
    assert fence.set_fence(home / 'fences', name, token, 'by') == (False, 'invalid_fence_key')
    assert not (home / 'fences').exists() or list((home / 'fences').iterdir()) == []


# ---- identity + check -----------------------------------------------------------------------------------------------------------------

def run_fence(home, *argv, env=None, stdin=None):
    return subprocess.run([sys.executable, '-I', str(home / '.claude/scripts/proxy-fence.py'), *argv], env=env or lane_env(home), input=stdin,
                          capture_output=True, text=True, timeout=30)


def test_the_fence_refuses_a_new_lease_or_box_for_the_fenced_lane_only(home):
    assert run_fence(home, 'check', '--effect', 'lease').returncode == 0  # not fenced yet
    fence_lane(home, now=None)
    done = run_fence(home, 'check', '--effect', 'lease')
    assert done.returncode == 75 and 'PREEMPT_FENCED lane=lane-a run=tok-a' in done.stderr and 'effect=lease' in done.stderr
    assert run_fence(home, 'check', env=lane_env(home, token='tok-relaunched')).returncode == 0  # a relaunch of the same lane
    assert run_fence(home, 'check', env=lane_env(home, name='lane-b')).returncode == 0
    plain = {k: v for k, v in lane_env(home).items() if k not in ('PROXY_AGENT_NAME', 'PROXY_AGENT_RUN_TOKEN')}
    plain['PROXY_FENCE_PS'] = str(home / 'empty-ps')
    (home / 'empty-ps').write_text('')
    assert run_fence(home, 'check', env=plain).returncode == 0  # PM / any caller that is not a lane


def test_a_stripped_environment_finds_the_lane_through_its_ancestry_and_the_registry_token(home, monkeypatch):
    (home / 'runs/lane-a').mkdir()
    (home / 'runs/lane-a/manifest.json').write_text(json.dumps({'name': 'lane-a', 'token': 'tok-a'}))
    (home / 'ps').write_text('  100 1 /opt/homebrew/bin/claude -p --name lane-a --model claude-sonnet-5-5 --max-turns 9\n  101 100 bash -c ./remote-test-box.sh up\n'
                             '  102 101 python3 rtb-pool-lease.py acquire2\n  200 1 /bin/zsh\n')
    monkeypatch.setenv('PROXY_FENCE_PS', str(home / 'ps'))  # a fixed process table instead of ps
    assert fence.lane_identity({}, pid=102) == ('lane-a', 'tok-a')  # grandchild of the lane's claude process
    assert fence.lane_identity({}, pid=200) == (None, None)  # a shell outside any lane
    (home / 'runs/lane-a/manifest.json').write_text(json.dumps({'name': 'lane-a'}))  # a registry row with no run id cannot be keyed
    assert fence.lane_identity({}, pid=102) == (None, None)


# ---- the PreToolUse hook (push / PR actions) ---------------------------------------------------------------------------------------------

def hook(home, command, env=None):
    return run_fence(home, 'hook', env=env, stdin=json.dumps({'tool_name': 'Bash', 'tool_input': {'command': command}}))


@pytest.mark.parametrize('command', ['git push origin HEAD', 'cd /tmp/x && git -C /tmp/x push -u origin fix/1-a', 'gh pr create --title t', 'gh pr merge 12 --squash',
                                     'gh pr edit 12 --add-label x', 'gh pr ready 12', '~/.claude/scripts/ci/pm-admit-ci.sh 14470'])
def test_a_fenced_lane_cannot_start_a_push_or_pr_action(home, command):
    assert hook(home, command).returncode == 0  # not fenced: everything passes
    fence_lane(home, now=None)
    done = hook(home, command)
    assert done.returncode == 2 and 'PREEMPT_FENCED' in done.stderr and 'effect=push' in done.stderr and done.stdout == ''


@pytest.mark.parametrize('command', ['git status', 'git log -3', 'gh pr view 12', 'rg push src', 'ls', 'echo hi'])
def test_a_fenced_lane_may_still_do_everything_else(home, command):
    fence_lane(home, now=None)
    assert hook(home, command).returncode == 0


def test_the_hook_fails_open_on_anything_it_cannot_read(home):
    fence_lane(home, now=None)
    assert run_fence(home, 'hook', stdin='not json').returncode == 0
    assert run_fence(home, 'hook', stdin='[]').returncode == 0
    assert run_fence(home, 'hook', stdin=json.dumps({'tool_input': {'command': 7}})).returncode == 0


def test_the_launcher_wires_the_hook_for_bash_and_only_blocks_on_rc_2(home):
    line = next(ln for ln in LAUNCHER.splitlines() if ln.startswith('_fence_cmd = '))
    ns = {'_fence': str(home / '.claude/scripts/proxy-fence.py')}
    exec(line, ns)  # the exact command string the launcher writes into every lane's settings
    assert 'matcher": "Bash"' in LAUNCHER[LAUNCHER.index(line):LAUNCHER.index(line) + 400] and 'proxy-fence.py' in LAUNCHER
    command = ns['_fence_cmd']
    payload = json.dumps({'tool_input': {'command': 'git push origin HEAD'}})

    def go(env, text=payload):
        return subprocess.run(['bash', '-c', command], env=env, input=text, capture_output=True, text=True, timeout=30)
    assert go(lane_env(home)).returncode == 0
    fence_lane(home, now=None)
    assert go(lane_env(home)).returncode == 2
    assert go(lane_env(home), json.dumps({'tool_input': {'command': 'git status'}})).returncode == 0
    (home / '.claude/scripts/proxy-fence.py').unlink()
    assert go(lane_env(home)).returncode == 0  # script missing: fail open


# ---- the lease CLI + the box scripts (deployed, not MoP-tracked: checked where they exist) --------------------------------------------------------

LEASE_CLI = DEPLOYED / 'rtb-pool-lease.py'
RTB = DEPLOYED / 'remote-test-box.sh'
RBB = DEPLOYED / 'remote-browser-box.sh'


def lease(home, *argv, env=None):
    pool = home / 'pool'
    e = {**(env or lane_env(home)), 'RTB_POOL_DIR': str(pool), 'RTB_STATE': str(home / 'rtb-state')}
    return subprocess.run([sys.executable, str(LEASE_CLI), *argv], env=e, capture_output=True, text=True, timeout=60)


def tickets(home):
    return sorted(p.name for p in (home / 'pool/queue').glob('*.json')) if (home / 'pool/queue').is_dir() else []


@pytest.mark.skipif(not LEASE_CLI.exists(), reason='rtb-pool-lease.py is not installed beside this copy')
def test_the_lease_cli_refuses_a_fresh_acquire_and_a_borrow_for_a_fenced_lane_and_writes_nothing(home):
    fence_lane(home, now=None)
    done = lease(home, 'acquire2', 'qa-lane-a-1', '600', '1', '-', str(os.getpid()), '-')
    assert done.returncode == 75 and 'PREEMPT_FENCED' in done.stdout and 'PREEMPT_FENCED' in done.stderr
    assert tickets(home) == [] and not list((home / 'pool').glob('*.lease'))
    borrow = lease(home, 'borrow', 'qa-lane-a-1', '600', '1', '-', str(os.getpid()), 'tkt', str(home / 'browser'))
    assert borrow.returncode == 7 and 'NO_BORROW preempt_fence' in borrow.stdout


@pytest.mark.skipif(not LEASE_CLI.exists(), reason='rtb-pool-lease.py is not installed beside this copy')
def test_the_lease_cli_still_serves_an_unfenced_lane_and_a_fenced_lanes_existing_ticket(home):
    free = lease(home, 'acquire2', 'qa-lane-a-1', '600', '1', '-', str(os.getpid()), '-')
    assert free.returncode != 75 and 'PREEMPT_FENCED' not in free.stdout + free.stderr
    fence_lane(home, now=None)
    polling = lease(home, 'acquire2', 'qa-lane-a-1', '600', '1', '-', str(os.getpid()), 'abc123def456')  # already holds a ticket: custody the stop path sees
    assert polling.returncode != 75 and 'PREEMPT_FENCED' not in polling.stdout + polling.stderr
    assert lease(home, 'list').returncode == 0 and lease(home, 'queue').returncode == 0  # the stop path's own custody reads are never gated


def gate_fn(path):
    m = re.search(r'^fence_gate\(\) \{.*\}$', path.read_text(), re.M)
    assert m, f'{path.name} has no fence_gate'
    return m.group(0)


@pytest.mark.skipif(not (RTB.exists() and RBB.exists()), reason='box scripts are not installed beside this copy')
@pytest.mark.parametrize('script', [RTB, RBB], ids=['remote-test-box', 'remote-browser-box'])
def test_the_box_scripts_gate_the_fenced_lane_and_fail_open_otherwise(home, script):
    body = gate_fn(script)

    def go(env):
        return subprocess.run(['bash', '-c', body + '\nfence_gate lease\necho PROCEEDED'], env=env, capture_output=True, text=True, timeout=30)
    assert 'PROCEEDED' in go(lane_env(home)).stdout
    fence_lane(home, now=None)
    blocked = go(lane_env(home))
    assert blocked.returncode == 75 and 'PROCEEDED' not in blocked.stdout and 'PREEMPT_FENCED' in blocked.stderr
    assert 'PROCEEDED' in go(lane_env(home, token='tok-relaunched')).stdout
    (home / '.claude/scripts/proxy-fence.py').write_text('import sys\nsys.exit(1)\n')  # a crashing helper never blocks (only its exact rc 75 does)
    assert 'PROCEEDED' in go(lane_env(home)).stdout
    (home / '.claude/scripts/proxy-fence.py').unlink()
    assert 'PROCEEDED' in go(lane_env(home)).stdout


@pytest.mark.skipif(not (RTB.exists() and RBB.exists()), reason='box scripts are not installed beside this copy')
@pytest.mark.parametrize('script', [RTB, RBB], ids=['remote-test-box', 'remote-browser-box'])
def test_every_up_and_once_dispatch_runs_the_fence_gate_before_it_starts(script):
    """Discovered over the dispatch table: any up/once entry that reaches cmd_up/cmd_once must gate first (no hand-kept list of entry points)."""
    src = script.read_text()
    entries = re.findall(r'\b(up|once)\) ((?:(?!;;)[^\n])*?)cmd_(?:up|once)\b', src)
    assert entries, f'{script.name}: no up/once dispatch found'
    assert all('fence_gate' in prefix for _name, prefix in entries), entries

@pytest.mark.skipif(not RTB.exists(), reason='remote-test-box.sh is not installed beside this copy')
def test_remote_test_box_gates_up_before_the_mac_first_provider_dispatch_that_reserves_capacity():
    """mac_up (the default provider) reserves Mac capacity before the later up) dispatch runs, so the first fence_gate call must precede it."""
    src = RTB.read_text()
    calls = [m.start() for m in re.finditer(r'fence_gate lease', src)]
    assert calls and calls[0] < src.index('mac_up "$@"') and calls[0] < src.index('cmd_up "$@"')
