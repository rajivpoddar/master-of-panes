"""P0 preemption for proxy-agent.sh (proxy-preempt.py) and the Claude no-burn-gate contract of the launcher.

UPDATE 23 Nov (Rajiv C0ALZJHGE49 thread 1795437841.011459, ts 1795439359.022079: "drop the P0 override rule. preempt the lowest priority agent and
use that instead."): preemption is the ONLY way a P0 gets a slot at cap: the launcher no longer launches a P0 over a pool cap or past a burn gate. It
calls the helper with --pool-full; rc 0 = slot free, anything else = queued CAPACITY_WAIT. The HELPER defaults ON (PROXY_AGENT_PREEMPT=0 turns it off);
the LAUNCHER defaults OFF (CTO C0ALZJHGE49 thread 1795441441.525659, ts 1795441848.817099: keep the P0 queued until the stop path fences new lease and
external-effect acquisition, then rechecks custody under that fence before signaling) and calls the helper only with PROXY_AGENT_PREEMPT=1.
The fence tests below pin that order: relaunch row, fence (lane name + run id), custody re-check under the fence, then SIGTERM; fence cleared on every path.

Rajiv C0ALZJHGE49 thread 1795369444.132999, ts 1795372352.279289 (23 Nov): "for P0's, preempt the lowest priority proxy agent" and
"drop the P0 gate ... we don't have to track the burn rate for calude proxy accounts".
CTO REVISE C0ALZJHGE49 thread 1795385404.941879, ts 1795385573.907659: the helper is packaged in the release manifest, stays OFF by
default, and before any SIGTERM verifies the victim's current box lease and pending effects with the real lease tooling (never log
heuristics) and persists the relaunch custody first.

Hermetic: lanes are fixture manifests plus fake `ps` text (pid ppid command), the lease ledger is a fake `rtb-pool-lease.py` runner
over a temp state root, signals go to an injected killer, the pm-ops CLI is a stub script (PROXY_AGENT_PM_OPS_CLI contract) and
registries/log/stop files live under tmp_path. Nothing signals a real process, posts to Slack, or touches the real pm-ops DB.
Custody tests assert the PROPERTY (any bound non-free custody row or live box/push descendant refuses the victim; unreadable custody
refuses; the obligation is persisted before the signal), discovered over cases, not a hand-kept list of production sites (rule 60).
Run: /opt/homebrew/bin/pytest -q -p no:cacheprovider tests/test_proxy_preempt.py
"""
import ast
import hashlib
import os
import importlib.util
import inspect
import json
import re
import signal
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location('proxy_preempt_under_test', SCRIPTS / 'proxy-preempt.py')
pp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pp)
LAUNCHER = (SCRIPTS / 'proxy-agent.sh').read_text()
HELPER_SRC = (SCRIPTS / 'proxy-preempt.py').read_text()
MANIFEST = SCRIPTS.parents[1] / 'manifest.json'  # scripts/pm/shared-assets/manifest.json (absent in the installed ~/.claude copy)
REAL_LEASE_CLI = Path.home() / '.claude/scripts/rtb-pool-lease.py'

STUB = '''import json, os, sys
LEDGER, MODE = %r, %r
argv = sys.argv[1:]
open(LEDGER, "a").write(json.dumps(argv) + "\\n")
mode = open(MODE).read().strip() if os.path.exists(MODE) else "ok"
if mode == "fail":
    sys.exit(1)
if argv[0] == "obligation-upsert" and mode == "ok":
    print("42")
'''


class World:
    """Temp registry + fake process table + fake lease ledger + recording signal/clock hooks."""

    def __init__(self, tmp, monkeypatch):
        self.tmp = tmp
        self.ps = []
        self.alive = set()
        self.signals = []
        self.at_signal = []  # how many obligation calls existed when each signal was sent
        self.exit_on_term = True
        self.next_pid = 1000
        self.rtb = tmp / 'rtb'
        (self.rtb / 'pool').mkdir(parents=True)
        self.lists, self.queues, self.pendings = {}, {}, {}  # pool dir name -> output rows of the lease CLI
        self.cli = {}  # (pool dir name, command) -> (rc, stdout) overrides
        self.mutations = {}  # n-th `list` of the main ledger -> callable applied just before that read
        self.snapshots = 0
        (tmp / 'runs').mkdir()
        (tmp / 'caps.env').write_text('')
        self.stub = tmp / 'pm-ops-stub.py'
        self.stub.write_text(STUB % (str(tmp / 'obligations.jsonl'), str(tmp / 'stub-mode')))
        for attr, val in (('RUNS', tmp / 'runs'), ('STOP_DIR', tmp / 'stop'), ('LOG', tmp / 'preemptions.log'), ('FENCE_DIR', tmp / 'fences'),
                          ('CAPS_ENV', tmp / 'caps.env'), ('PM_OPS_CLI', str(self.stub)), ('RTB_ROOT', self.rtb), ('OVERRIDES_LOG', tmp / 'overrides.log')):
            monkeypatch.setattr(pp, attr, val)

    def caps(self, **kv):
        (self.tmp / 'caps.env').write_text(''.join(f'{k}={v}\n' for k, v in kv.items()))

    def stub_mode(self, mode):
        (self.tmp / 'stub-mode').write_text(mode)

    def lane(self, name, model='claude-sonnet-5-5', type='hd-builder', brief='PRIORITY: P2\n', started='2026-11-23T01:00:00Z',
             issue=None, pr=None, log='', jsonl=None, register=True, ppid=1, token='auto'):
        pid = self.next_pid
        self.next_pid += 1
        self.alive.add(pid)
        self.ps.append(f'  {pid} {ppid} /opt/homebrew/bin/claude -p --name {name} --model {model} --max-turns 200 --output-format stream-json')
        if register:
            brief_path = self.tmp / f'{name}.brief'
            brief_path.write_text(brief)
            out = self.tmp / f'{name}.md'
            (self.tmp / f'{name}.log').write_text(log)
            if jsonl is not None:
                (self.tmp / f'{name}.jsonl').write_text('\n'.join(json.dumps(e) for e in jsonl) + '\n')
            m = {'name': name, 'type': type, 'brief': str(brief_path), 'out': str(out), 'started_at': started, 'model': model}
            if token:  # the run id the fence is keyed by (PROXY_AGENT_RUN_TOKEN; proxy-run-state.py start writes it into the registry row)
                m['token'] = f'tok-{name}' if token == 'auto' else token
            if issue:
                m['issue'] = issue
            if pr:
                m['pr'] = pr
            (self.tmp / 'runs' / name).mkdir()
            (self.tmp / 'runs' / name / 'manifest.json').write_text(json.dumps(m))
        return pid

    def child(self, parent, command):
        pid = self.next_pid
        self.next_pid += 1
        self.ps.append(f'  {pid} {parent} {command}')
        return pid

    # ---- the fake lease ledger (what rtb-pool-lease.py list / queue / pending print, and the files beside them) ----
    def pool_dir(self, pool):
        d = self.rtb / pool
        d.mkdir(exist_ok=True)
        return d

    def box(self, box, state, owner, pool='pool', pid=None):
        self.pool_dir(pool)  # a ledger with a box in it exists on disk (the helper only consults ledger dirs that exist)
        self.lists.setdefault(pool, []).append('\t'.join([box, state, owner, '-' if state == 'free' else '30', '-', '-' if state == 'free' else 'P2']))
        if pid is not None:
            (self.pool_dir(pool) / f'{box}.lease').write_text(json.dumps({'owner': owner, 'pid': pid, 'state': state, 'gen': 3}))

    def ticket(self, owner, ticket='abc123def456', pending_box=None, pool='pool', pid=None):
        self.queues.setdefault(pool, []).append(json.dumps({'ticket': ticket, 'owner': owner, 'base': 'P2', 'effective': 'P2', 'age_s': 5,
                                                              'at': 1.0, 'burst': False, 'pending_box': pending_box}))
        if pid is not None:
            (self.pool_dir(pool) / 'queue').mkdir(exist_ok=True)
            (self.pool_dir(pool) / 'queue' / f'{ticket}.json').write_text(json.dumps({'owner': owner, 'pid': pid, 'pending_box': pending_box}))

    def pending(self, slot, state, pid=None, pool='pool'):
        self.pendings.setdefault(pool, []).append(json.dumps({'slot': slot, 'name': f'srv-{slot}', 'state': state, 'pid': pid, 'attempt': 1}))

    def eph(self, name, owner, pid=None, state='active', pool='pool'):
        d = self.pool_dir(pool) / 'eph'
        d.mkdir(exist_ok=True)
        (d / f'{name}.json').write_text(json.dumps({'owner': owner, 'pid': pid, 'name': name, 'state': state, 'burst': False}))

    def lease_runner(self, argv, d):
        cmd = argv[0]
        if cmd == 'list' and d.name == 'pool':
            self.snapshots += 1
            if self.snapshots in self.mutations:
                self.mutations[self.snapshots]()
        if (d.name, cmd) in self.cli:
            return self.cli[(d.name, cmd)]
        table = {'list': self.lists, 'queue': self.queues, 'pending': self.pendings}[cmd]
        return 0, ''.join(row + '\n' for row in table.get(d.name, []))

    def killer(self, pid, sig):
        self.at_signal.append(len(self.obligations()))
        self.signals.append((pid, sig))
        if self.exit_on_term:
            self.alive.discard(pid)

    def run(self, *argv, env=None, now=1_795_400_000.0, enabled='1', killer=None):
        base = ['--launch-name', 'p0-launch', '--wait', '3']
        full_env = {} if enabled is None else {'PROXY_AGENT_PREEMPT': enabled}
        full_env.update(env or {})
        return pp.main([*base, *argv], env=full_env, ps_text=lambda: '\n'.join(self.ps), killer=killer or self.killer,
                       alive=lambda pid: pid in self.alive, sleep=lambda _s: None, now=now, lease_runner=self.lease_runner)

    def obligations(self):
        path = self.tmp / 'obligations.jsonl'
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def log_text(self):
        path = self.tmp / 'preemptions.log'
        return path.read_text() if path.exists() else ''

    def nothing_happened(self):
        return self.signals == [] and self.obligations() == [] and not (self.tmp / 'stop').exists()


@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


P0 = ('--model', 'claude-opus-5-5', '--override', 'P0:14600')


def fill_claude_pool(w, cap=3):
    w.caps(CLAUDE_PROXY_CAP=cap)


# ---- on by default, with an off switch -----------------------------------------------------------------------------------

@pytest.mark.parametrize('enabled', ['0', ''])
def test_a_real_run_is_disabled_only_by_the_off_switch(w, capsys, enabled):
    w.lane('a', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 1)
    assert w.run(*P0, enabled=enabled) == 0
    assert 'PREEMPT disabled' in capsys.readouterr().out
    assert w.nothing_happened() and w.log_text() == '' and w.snapshots == 0  # not even the lease tooling was consulted


def test_a_real_run_preempts_by_default_when_the_env_is_unset(w):
    w.lane('a', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 1)
    assert w.run(*P0, enabled=None) == 0 and len(w.signals) == 1


def test_dry_run_still_plans_while_disabled_because_it_changes_nothing(w, capsys):
    w.lane('lane-p3', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 1)
    assert w.run(*P0, '--dry-run', enabled=None) == 0
    assert 'PREEMPT_PLAN victim=lane-p3' in capsys.readouterr().out and w.nothing_happened()


def test_the_launcher_default_is_off_and_the_helper_is_called_only_at_pool_admission():
    assert '"${PROXY_AGENT_PREEMPT:-0}" == 0' in LAUNCHER and 'PROXY_AGENT_PREEMPT:-1' not in LAUNCHER  # CTO ts 1795441848.817099
    assert LAUNCHER.count('proxy-preempt.py') >= 1 and LAUNCHER.count('--pool-full') >= 1


# ---- selection (priority, scope, never-preempt classes) ---------------------------------------------------------------------

def test_not_a_p0_launch_does_nothing(w, capsys):
    w.lane('a', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 1)
    assert w.run('--model', 'claude-opus-5-5') == 0
    assert 'PREEMPT not_p0' in capsys.readouterr().out
    assert w.nothing_happened() and w.log_text() == ''


def test_p0_with_room_in_pool_and_total_does_nothing(w, capsys):
    w.lane('a', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 3)
    assert w.run(*P0, '--total-cap', '8') == 0
    assert 'PREEMPT not_needed' in capsys.readouterr().out
    assert w.signals == []


def test_brief_first_line_priority_p0_also_triggers(w, tmp_path, capsys):
    w.lane('a', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 1)
    brief = tmp_path / 'launch.brief'
    brief.write_text('PRIORITY: P0\nbody\n')
    assert w.run('--model', 'claude-opus-5-5', '--brief', str(brief)) == 0
    assert 'victim=a' in capsys.readouterr().out and len(w.signals) == 1


def test_preempts_lowest_priority_only_p3_before_p2_before_p1(w, capsys):
    pa = w.lane('lane-p1', brief='PRIORITY: P1\n')
    pb = w.lane('lane-p2', brief='PRIORITY: P2\n')
    pc = w.lane('lane-p3', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 3)
    assert w.run(*P0) == 0
    assert w.signals == [(pc, signal.SIGTERM)]  # exactly one lane, the P3, and nothing else
    assert pa in w.alive and pb in w.alive
    assert 'victim=lane-p3' in capsys.readouterr().out


def test_p1_is_preempted_only_when_nothing_lower_is_live(w):
    pa = w.lane('lane-p1', brief='PRIORITY: P1\n')
    w.lane('lane-p0', brief='PRIORITY: P0\n')
    fill_claude_pool(w, 2)
    assert w.run(*P0) == 0
    assert w.signals == [(pa, signal.SIGTERM)]


def test_tie_goes_to_the_youngest_lane(w):
    w.lane('old', brief='PRIORITY: P3\n', started='2026-11-23T00:10:00Z')
    young = w.lane('young', brief='PRIORITY: P3\n', started='2026-11-23T01:10:00Z')
    fill_claude_pool(w, 2)
    assert w.run(*P0) == 0
    assert w.signals == [(young, signal.SIGTERM)]


def test_victim_stop_file_log_and_relaunch_obligation(w):
    w.lane('lane-p2', brief='PRIORITY: P2\n', issue='14567', pr='14570', type='hd-proof-qa')
    w.lane('other', brief='PRIORITY: P1\n')
    fill_claude_pool(w, 2)
    assert w.run(*P0) == 0
    stop = (w.tmp / 'stop' / 'lane-p2').read_text()
    assert stop.startswith('PREEMPT_STOP name=lane-p2 by=p0-launch p0=14600') and 'Checkpoint now' in stop
    line = w.log_text().strip()
    assert ' PREEMPT victim=lane-p2 type=hd-proof-qa model=claude-sonnet-5-5 priority=P2 source=brief ' in line
    assert 'for=p0-launch p0=14600 pool=claude:2/2 outcome=exited waited=0s obligation=42' in line
    (args,) = w.obligations()
    assert args[0] == 'obligation-upsert' and args[-1] == '--print-id'  # the id is what gates the signal
    pairs = dict(zip(args[1::2], args[2::2]))
    assert pairs['--kind'] == 'proxy_relaunch' and pairs['--target-type'] == 'proxy' and pairs['--target-id'] == 'lane-p2'
    assert pairs['--issue'] == '14567' and pairs['--pr'] == '14570' and pairs['--horizon'] == 'hourly'
    evidence = json.loads(pairs['--evidence-json'])
    assert evidence['preempted'] == 'lane-p2' and evidence['for_launch'] == 'p0-launch' and evidence['p0'] == '14600'
    assert evidence['brief'].endswith('lane-p2.brief')


@pytest.mark.parametrize('why, kwargs', [
    ('p0', {'brief': 'PRIORITY: P0\n'}),
    ('admission_review', {'type': 'pm-admission-reviewer', 'brief': 'PRIORITY: P3\n'}),
    ('admission_review', {'brief': 'PRIORITY: P3\n12b review of #1\n'}),
    ('unknown_priority', {'brief': 'no priority here\n'}),
    ('unregistered', {'register': False}),
])
def test_never_preempts_these_lanes_and_refuses_when_nothing_is_left(w, capsys, why, kwargs):
    w.lane('only', **kwargs)
    fill_claude_pool(w, 1)
    assert w.run(*P0) == 3
    out = capsys.readouterr().out
    assert 'PREEMPT_REFUSED reason=nothing_preemptible' in out and f'only:{why}' in out
    assert w.nothing_happened()
    assert 'PREEMPT_REFUSED' in w.log_text() and f'only:{why}' in w.log_text()


def test_a_lane_whose_pid_cannot_be_read_is_not_a_victim(w, capsys):
    w.lane('only', brief='PRIORITY: P3\n')
    w.ps[-1] = w.ps[-1].split(None, 2)[2]  # drop the `pid ppid` prefix: the lane line no longer says which process to signal
    fill_claude_pool(w, 1)
    assert w.run(*P0) == 3
    assert 'only:no_pid' in capsys.readouterr().out and w.nothing_happened()


@pytest.mark.parametrize('name', ['pm-12b-review-14600', 'x-admission-y'])
def test_a_12b_or_admission_lane_is_recognised_by_name_alone(w, capsys, name):
    w.lane(name, type='hd-builder', brief='PRIORITY: P3\nplain brief\n')
    fill_claude_pool(w, 1)
    assert w.run(*P0) == 3
    assert f'{name}:admission_review' in capsys.readouterr().out and w.signals == []


def test_never_signals_the_launcher_itself(w, capsys):
    w.lane('p0-launch', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 1)
    assert w.run(*P0) == 3
    assert 'p0-launch:launcher' in capsys.readouterr().out and w.signals == []


def test_priority_falls_back_to_the_issue_then_pr_label(w, tmp_path, capsys):
    labels = tmp_path / 'labels.sh'
    labels.write_text('#!/bin/sh\ncase "$1:$2" in issue:101) echo "status:todo,P3";; pr:202) echo "P2";; issue:303) echo "P0,P2";; *) ;; esac\n')
    labels.chmod(0o755)
    a = w.lane('by-issue', brief='no header\n', issue='101')
    w.lane('by-pr', brief='no header\n', pr='202')
    w.lane('best-label-wins', brief='no header\n', issue='303')
    fill_claude_pool(w, 3)
    assert w.run(*P0, env={'PROXY_PREEMPT_LABELS_CMD': str(labels)}) == 0
    assert w.signals == [(a, signal.SIGTERM)]
    assert 'source=issue#101' in capsys.readouterr().out


def test_unreadable_label_lookup_means_not_preemptible(w, tmp_path, capsys):
    broken = tmp_path / 'broken.sh'
    broken.write_text('#!/bin/sh\nexit 1\n')
    broken.chmod(0o755)
    w.lane('x', brief='no header\n', issue='5')
    fill_claude_pool(w, 1)
    assert w.run(*P0, env={'PROXY_PREEMPT_LABELS_CMD': str(broken)}) == 3
    assert 'x:unknown_priority' in capsys.readouterr().out and w.signals == []


def test_pool_scoping_other_pools_are_not_victims_when_the_pool_is_full(w):
    w.lane('c1', brief='PRIORITY: P2\n')
    w.lane('c2', brief='PRIORITY: P2\n')
    w.lane('grok-p3', model='grok-4.7', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 2)
    assert w.run(*P0) == 0
    (pid, _sig), = w.signals
    assert pid not in w.alive and 'grok-p3' not in w.log_text()
    assert 'victim=c2' in w.log_text() or 'victim=c1' in w.log_text()


def test_total_cap_full_any_pool_but_only_capped_roles(w):
    w.caps(CLAUDE_PROXY_CAP=9, PROXY_AGENT_TOTAL_CAP=2)
    w.lane('growth-p3', model='grok-4.7', type='growth', brief='PRIORITY: P3\n')  # not a capped role: frees no total slot
    keep = w.lane('builder-p2', model='grok-4.7', type='hd-builder', brief='PRIORITY: P2\n')
    w.lane('qa-p1', model='gpt-6.1-terra', type='hd-proof-qa', brief='PRIORITY: P1\n')
    assert w.run(*P0, '--total-cap', '2') == 0
    assert w.signals == [(keep, signal.SIGTERM)]


def test_codex_pool_at_cap_zero_with_no_live_lane_needs_no_preemption(w, capsys):
    w.caps(CODEX_PROXY_CAP=0)
    w.lane('c', brief='PRIORITY: P3\n')
    assert w.run('--model', 'gpt-6.2-sol', '--override', 'P0:1', '--total-cap', '8') == 0
    assert 'PREEMPT not_needed' in capsys.readouterr().out and w.signals == [] and w.nothing_happened()


def test_still_alive_after_the_wait_is_reported_and_exits_nonzero(w):
    w.exit_on_term = False
    w.lane('stubborn', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 1)
    assert w.run(*P0) == 4
    assert 'outcome=still_alive waited=3s obligation=42' in w.log_text()
    assert len(w.obligations()) == 1  # the relaunch is still owed


def test_dry_run_selects_but_changes_nothing(w, capsys):
    w.lane('lane-p3', brief='PRIORITY: P3\n')
    w.lane('lane-p1', brief='PRIORITY: P1\n')
    fill_claude_pool(w, 2)
    assert w.run(*P0, '--dry-run') == 0
    assert 'PREEMPT_PLAN victim=lane-p3' in capsys.readouterr().out
    assert w.nothing_happened() and w.log_text() == ''


def test_a_victim_that_already_has_a_fresh_stop_request_is_skipped(w, capsys):
    w.lane('stopping', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 1)
    (w.tmp / 'stop').mkdir()
    (w.tmp / 'stop' / 'stopping').write_text('x')
    assert w.run(*P0, now=(w.tmp / 'stop' / 'stopping').stat().st_mtime + 10) == 3
    assert 'stopping:already_stopping' in capsys.readouterr().out


def test_ps_failure_never_blocks_the_p0(w, monkeypatch, capsys):
    def boom(*a, **k):
        raise OSError('no ps')
    monkeypatch.setattr(pp.subprocess, 'check_output', boom)
    rc = pp.main(['--launch-name', 'p0-launch', *P0], env={'PROXY_AGENT_PREEMPT': '1'}, ps_text=None)
    assert rc == 0 and 'UNAVAILABLE reason=ps_failed' in capsys.readouterr().out


def test_pool_of_agrees_with_every_pool_the_launcher_maps_a_model_to():
    """Discovered, not hand-listed (rule 60): parse the launcher's own `kind = ... if ... else ...` chain and check each literal."""
    chain = next((ln for ln in LAUNCHER.splitlines() if ln.lstrip().startswith('kind = "claude" if')), None)
    assert chain, 'launcher pool mapping not found (proxy_agent_pool_live changed shape: update proxy-preempt.py pool_of)'
    seen = {}
    for seg in chain.split(' else '):
        kind = re.match(r'\s*(?:kind = )?"([a-z0-9]*)"', seg).group(1)
        if not kind or ' if ' not in seg:
            continue
        for literal in re.findall(r'"([a-z0-9][a-z0-9.\-]*)"', seg.split(' if ', 1)[1]):
            seen[literal] = kind
            assert pp.pool_of(literal) == kind, f'{literal}: launcher={kind} helper={pp.pool_of(literal)!r}'
    assert {'claude', 'codex', 'grok', 'spark', 'inkling', 'hy4', 'opencode', 'sarvam'} <= set(seen.values())
    assert pp.pool_of('gpt-6.2-sol(xhigh)') == 'codex' and pp.pool_of('unheard-of') == ''
    assert set(pp.POOL_CAPS) == set(seen.values())  # every pool the launcher knows has a cap rule here


# ---- custody: a bound non-free lease/ticket/pending/eph row or a live box/push descendant refuses the victim ---------------------

def _box_by_lane_name(w, pid):
    w.box('hetzner-1', 'leased', 'qa-victim-lane-55')
    return 'box:hetzner-1:leased'


def _box_by_issue_number(w, pid):
    w.box('hetzner-1', 'leased', 'hd-14487-rework')
    return 'box:hetzner-1:leased'


def _box_by_owner_pid_of_a_descendant(w, pid):
    kid = w.child(pid, 'sleep 600')
    w.box('hetzner-1', 'leased', f'runner-{kid}')
    return 'box:hetzner-1:leased'


def _box_by_lease_file_pid(w, pid):
    w.box('hetzner-1', 'leased', 'opaque-owner', pid=pid)
    return 'box:hetzner-1:leased'


def _box_in_another_ledger(w, pid):
    w.box('hetzner-9', 'leased', 'qa-victim-lane-1', pool='pool-cx33')
    return 'box:hetzner-9:leased'


def _ticket_by_lane_name(w, pid):
    w.ticket('victim-lane-qa', ticket='aaaa11112222')
    return 'ticket:aaaa11112222:waiting'


def _ticket_with_a_pending_box(w, pid):
    w.ticket('hd-14487-rework', ticket='bbbb11112222', pending_box='hetzner-4')
    return 'ticket:bbbb11112222:pending_box'


def _ticket_by_queue_file_pid(w, pid):
    w.ticket('opaque-owner', ticket='cccc11112222', pid=pid)
    return 'ticket:cccc11112222:waiting'


def _unresolved_create_by_pid(w, pid):
    kid = w.child(pid, 'python3 remote-create-helper.py')
    w.pending('slot3', 'creating', pid=kid)
    return 'pending:slot3:creating'


def _eph_by_owner(w, pid):
    w.eph('eph-1', 'victim-lane-burst')
    return 'eph:eph-1:active'


def _eph_by_pid(w, pid):
    w.eph('eph-2', 'opaque-owner', pid=pid, state='destroying')
    return 'eph:eph-2:destroying'


def _live_remote_box_descendant(w, pid):
    kid = w.child(pid, 'bash /Users/x/.claude/scripts/remote-test-box.sh run BOX --mop -- npm run test:all')
    return f'process:{kid}:remote_box'


def _live_push_descendant(w, pid):
    kid = w.child(pid, 'git -C /tmp/wt push origin HEAD:main')
    return f'process:{kid}:push'


def _live_pr_create_grandchild(w, pid):
    mid = w.child(pid, 'bash -c wrapper')
    kid = w.child(mid, 'gh pr create --title x')
    return f'process:{kid}:push'


CUSTODY_CASES = [_box_by_lane_name, _box_by_issue_number, _box_by_owner_pid_of_a_descendant, _box_by_lease_file_pid, _box_in_another_ledger,
                 _ticket_by_lane_name, _ticket_with_a_pending_box, _ticket_by_queue_file_pid, _unresolved_create_by_pid, _eph_by_owner,
                 _eph_by_pid, _live_remote_box_descendant, _live_push_descendant, _live_pr_create_grandchild]


@pytest.mark.parametrize('setup', CUSTODY_CASES, ids=lambda f: f.__name__.lstrip('_'))
def test_custody_bound_to_the_only_candidate_refuses_it_with_no_signal_and_no_obligation(w, capsys, setup):
    pid = w.lane('victim-lane', brief='PRIORITY: P3\n', issue='14487')
    expected = setup(w, pid)
    fill_claude_pool(w, 1)
    assert w.run(*P0) == 3
    out = capsys.readouterr().out
    assert 'PREEMPT_REFUSED reason=nothing_preemptible' in out and f'victim-lane:custody:{expected}' in out
    assert w.nothing_happened() and pid in w.alive
    assert f'victim-lane:custody:{expected}' in w.log_text()


@pytest.mark.parametrize('state', ['leased', 'expired', 'broken', 'reserved', 'preempting', 'rebuilding', 'releasing', 'borrowed (test)'])
def test_every_non_free_lease_state_holds_the_bound_victim(w, capsys, state):
    w.lane('victim-lane', brief='PRIORITY: P3\n')
    w.box('hetzner-1', state, 'qa-victim-lane-9')
    fill_claude_pool(w, 1)
    assert w.run(*P0) == 3
    assert f'victim-lane:custody:box:hetzner-1:{state}' in capsys.readouterr().out and w.nothing_happened()


def test_free_unrelated_and_rejected_custody_does_not_hold_the_victim(w, capsys):
    pid = w.lane('victim-lane', brief='PRIORITY: P3\n', issue='14487')
    w.box('hetzner-1', 'free', '-')
    w.box('hetzner-2', 'leased', 'someone-else-19999')
    w.ticket('another-lane', ticket='dddd11112222')
    w.pending('slot9', 'rejected', pid=pid)  # the lease tool's own rule: only a rejected create does not block
    w.child(pid, 'sleep 1')
    fill_claude_pool(w, 1)
    assert w.run(*P0) == 0
    assert w.signals == [(pid, signal.SIGTERM)] and 'victim=victim-lane' in capsys.readouterr().out


def test_a_lane_with_custody_is_skipped_and_the_next_lowest_is_taken(w):
    w.lane('held-p3', brief='PRIORITY: P3\n')
    ok = w.lane('free-p2', brief='PRIORITY: P2\n')
    w.lane('p1', brief='PRIORITY: P1\n')
    w.box('hetzner-1', 'leased', 'qa-held-p3-77')
    fill_claude_pool(w, 3)
    assert w.run(*P0) == 0
    assert w.signals == [(ok, signal.SIGTERM)]


def test_logs_and_transcripts_are_not_a_custody_signal_only_the_lease_tooling_and_process_table_are(w):
    done = [{'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 't1', 'input': {'command': 'git push origin HEAD'}}]}}]
    pid = w.lane('log-only', brief='PRIORITY: P3\n', log='remote-test-box.sh run --spec x\ngit push origin HEAD\n', jsonl=done)
    fill_claude_pool(w, 1)
    assert w.run(*P0) == 0  # the real ledgers and live processes show no custody, so the stale log text decides nothing
    assert w.signals == [(pid, signal.SIGTERM)]
    assert not hasattr(pp, 'busy') and 'remote-test-box.sh run' not in inspect.getsource(pp.select)


@pytest.mark.parametrize('override, detail', [
    ({('pool', 'list'): (1, '')}, 'list_rc1:pool'),
    ({('pool', 'queue'): (0, 'not json at all')}, 'queue_unparsable'),
    ({('pool', 'queue'): (3, '')}, 'queue_rc3:pool'),
    ({('pool', 'pending'): (0, 'null')}, 'pending_not_an_object'),
    ({('pool', 'pending'): (2, '')}, 'pending_rc2:pool'),
    ({('pool', 'list'): (0, 'one-column-row')}, 'list_unparsable:pool'),
])
def test_an_unreadable_lease_ledger_refuses_every_candidate(w, capsys, override, detail):
    w.lane('victim-lane', brief='PRIORITY: P3\n')
    w.cli.update(override)
    fill_claude_pool(w, 1)
    assert w.run(*P0) == 3
    assert f'PREEMPT_REFUSED reason=lease_unverifiable detail={detail}' in capsys.readouterr().out
    assert w.nothing_happened() and 'lease_unverifiable' in w.log_text()


def test_an_unreadable_eph_record_refuses(w, capsys):
    w.lane('victim-lane', brief='PRIORITY: P3\n')
    (w.pool_dir('pool') / 'eph').mkdir()
    (w.pool_dir('pool') / 'eph' / 'broken.json').write_text('{not json')
    fill_claude_pool(w, 1)
    assert w.run(*P0) == 3
    assert 'lease_unverifiable detail=eph_unreadable:pool/broken.json' in capsys.readouterr().out and w.nothing_happened()


def test_no_lease_ledger_at_all_refuses(w, monkeypatch, tmp_path, capsys):
    (tmp_path / 'empty-root').mkdir()
    monkeypatch.setattr(pp, 'RTB_ROOT', tmp_path / 'empty-root')
    w.lane('victim-lane', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 1)
    assert w.run(*P0) == 3
    assert 'lease_unverifiable detail=no_lease_ledger' in capsys.readouterr().out and w.nothing_happened()


def test_a_victim_missing_from_the_process_table_refuses(w, capsys):
    pid = w.lane('victim-lane', brief='PRIORITY: P3\n')
    # a pid but no ppid: still a lane line (live_lanes reads the pid) yet not a `pid ppid command` row, so custody cannot be bound to it
    w.ps[-1] = f'  {pid} /opt/homebrew/bin/claude -p --name victim-lane --model claude-sonnet-5-5 --max-turns 200'
    fill_claude_pool(w, 1)
    assert w.run(*P0) == 3
    assert 'lease_unverifiable detail=victim_not_in_process_table' in capsys.readouterr().out and w.nothing_happened()


def test_a_lease_cli_that_cannot_run_is_unverifiable(tmp_path, monkeypatch):
    (tmp_path / 'pool').mkdir()
    monkeypatch.setattr(pp, 'LEASE_CLI', str(tmp_path / 'missing-lease-cli.py'))
    with pytest.raises(pp.LeaseUnverifiable):
        pp.custody_snapshot(tmp_path, pp.run_lease_cli)


@pytest.mark.skipif(not REAL_LEASE_CLI.exists(), reason='deployed rtb-pool-lease.py is not installed here')
def test_the_real_lease_cli_output_parses_on_an_empty_ledger(tmp_path, monkeypatch):
    """Contract check against the deployed tool (an empty ledger has no custody; list/queue/pending all exit 0 with no rows)."""
    (tmp_path / 'pool').mkdir()
    monkeypatch.setattr(pp, 'LEASE_CLI', str(REAL_LEASE_CLI))
    assert pp.custody_snapshot(tmp_path, pp.run_lease_cli) == []


# ---- order: persist custody, re-read custody, then signal ------------------------------------------------------------------------

def test_the_relaunch_obligation_exists_before_the_signal_is_sent(w):
    w.lane('victim-lane', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 1)
    assert w.run(*P0) == 0
    assert w.at_signal == [1]  # exactly the obligation-upsert had been recorded when SIGTERM went out


@pytest.mark.parametrize('mode', ['fail', 'noid'])
def test_no_persisted_obligation_means_no_stop_file_and_no_signal(w, capsys, mode):
    w.stub_mode(mode)  # fail: the pm-ops call errors; noid: it succeeds but returns no obligation id
    w.lane('victim-lane', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 1)
    assert w.run(*P0) == 3
    assert 'PREEMPT_REFUSED reason=custody_not_persisted victim=victim-lane' in capsys.readouterr().out
    assert w.signals == [] and not (w.tmp / 'stop').exists() and 'custody_not_persisted' in w.log_text()


def test_custody_that_appears_after_the_obligation_cancels_the_stop_and_withdraws_it(w, capsys):
    pid = w.lane('victim-lane', brief='PRIORITY: P3\n')
    w.mutations[2] = lambda: w.box('hetzner-1', 'leased', 'qa-victim-lane-12')  # the second custody read sees a new lease
    fill_claude_pool(w, 1)
    assert w.run(*P0) == 3
    assert 'PREEMPT_REFUSED reason=custody_changed victim=victim-lane holds=box:hetzner-1:leased' in capsys.readouterr().out
    assert w.signals == [] and pid in w.alive and not (w.tmp / 'stop').exists()
    upsert, resolve = w.obligations()
    assert upsert[0] == 'obligation-upsert' and resolve[:1] == ['obligation-resolve'] and '42' in resolve and 'proxy_relaunch' in resolve


def test_the_lease_tool_failing_on_the_recheck_cancels_the_stop_and_withdraws_the_obligation(w, capsys):
    w.lane('victim-lane', brief='PRIORITY: P3\n')
    w.mutations[2] = lambda: w.cli.update({('pool', 'list'): (1, '')})
    fill_claude_pool(w, 1)
    assert w.run(*P0) == 3
    assert 'reason=lease_unverifiable detail=list_rc1:pool victim=victim-lane' in capsys.readouterr().out
    assert w.signals == [] and [a[0] for a in w.obligations()] == ['obligation-upsert', 'obligation-resolve']


def test_a_box_tool_or_push_that_starts_before_the_signal_cancels_it(w, capsys):
    pid = w.lane('victim-lane', brief='PRIORITY: P3\n')
    kids = []
    w.mutations[2] = lambda: kids.append(w.child(pid, 'git push origin HEAD'))  # appears between the two process-table reads
    fill_claude_pool(w, 1)
    assert w.run(*P0) == 3
    assert f'reason=custody_changed victim=victim-lane holds=process:{kids[0]}:push' in capsys.readouterr().out
    assert w.signals == [] and [a[0] for a in w.obligations()] == ['obligation-upsert', 'obligation-resolve']


def test_a_signal_that_cannot_be_delivered_withdraws_the_obligation_and_the_stop_file(w):
    def refuse_signal(pid, sig):
        raise PermissionError('not ours')
    w.lane('victim-lane', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 1)
    rc = pp.main(['--launch-name', 'p0-launch', '--wait', '3', *P0], env={'PROXY_AGENT_PREEMPT': '1'}, ps_text=lambda: '\n'.join(w.ps),
                 killer=refuse_signal, alive=lambda pid: True, sleep=lambda _s: None, now=1_795_400_000.0, lease_runner=w.lease_runner)
    assert rc == 4 and 'outcome=signal_failed' in w.log_text()
    assert not (w.tmp / 'stop' / 'victim-lane').exists() and [a[0] for a in w.obligations()] == ['obligation-upsert', 'obligation-resolve']


def test_sigterm_is_delivered_only_by_stop_victim_and_only_after_custody_is_persisted_and_rechecked():
    """Structural: discovered from the source, so a new signalling path or a reordered main fails here (rule 60)."""
    tree = ast.parse(HELPER_SRC)
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    senders = {f.name for f in funcs if any(isinstance(n, ast.Attribute) and n.attr == 'SIGTERM' for n in ast.walk(f))}
    assert senders == {'stop_victim'}
    callers = {f.name for f in funcs if any(isinstance(n, ast.Call) and getattr(n.func, 'id', '') == 'stop_victim' for n in ast.walk(f))}
    assert callers == {'main'}
    main_src = inspect.getsource(pp.main)
    # CTO ts 1795441848.817099: relaunch row, THEN the fence, THEN the custody re-check under the fence, THEN the stop (SIGTERM).
    assert main_src.index('record_obligation(') < main_src.index('set_fence(') < main_src.index('holds_for(') < main_src.index('stop_victim(')
    assert main_src.index('custody_snapshot(') < main_src.index('record_obligation(')
    setters = {f.name for f in funcs if any(isinstance(n, ast.Attribute) and n.attr == 'set_fence' for n in ast.walk(f))}
    assert setters == {'main'}
    # the fence comes down in a finally that wraps BOTH the re-check and the stop, so no refuse/abort/signal-failure/exit path can leave it up
    tries = [n for n in ast.walk(ast.parse(main_src)) if isinstance(n, ast.Try) and n.finalbody]
    assert any('clear_fence' in ast.unparse(n.finalbody) and 'holds_for' in ast.unparse(n.body) and 'stop_victim' in ast.unparse(n.body) for n in tries)
    assert 'PROXY_AGENT_PREEMPT' in main_src and main_src.index('PREEMPT disabled') < main_src.index('custody_snapshot(')


# ---- packaging in the release manifest -------------------------------------------------------------------------------------------

@pytest.mark.skipif(not MANIFEST.exists(), reason='release manifest is not beside this copy (installed ~/.claude tree)')
def test_the_helper_and_its_test_are_packaged_in_the_release_manifest_with_current_digests():
    entries = {e['source_path']: e for e in json.loads(MANIFEST.read_text())['entries']}
    for rel in ('claude/scripts/proxy-preempt.py', 'claude/scripts/tests/test_proxy_preempt.py', 'claude/scripts/proxy-fence.py',
                'claude/scripts/tests/test_proxy_fence.py'):
        entry = entries[rel]
        assert entry['sha256'] == hashlib.sha256((MANIFEST.parent / rel).read_bytes()).hexdigest(), f'{rel}: manifest digest is stale'
        assert entry['canonical_target'].endswith('/.claude/scripts/' + rel.split('claude/scripts/', 1)[1])
    assert entries['claude/scripts/proxy-preempt.py']['mode'] == 0o644  # live mode; a mismatch refuses activation ("no pinned old manifest preimage")


# ---- launcher: hook wiring and the Claude no-burn-gate contract (functions/blocks extracted from the deployed proxy-agent.sh) ----

def bash(script, *args, env=None):
    return subprocess.run(['bash', '-c', script, *args], capture_output=True, text=True, env=env, timeout=30)


# P0 admission wiring: the real functions are extracted BY NAME from the deployed launcher (rule 60: no hand-kept site list) and run in bash with
# stubs for the live pool count and the helper. A P0 gets NO cap bypass: at cap it preempts through proxy-preempt.py --pool-full or it is queued.
def fn(name):
    return re.search(rf'^{name}\(\) \{{.*?^\}}$', LAUNCHER, re.S | re.M).group(0) + '\n'


P0_FUNCS = ''.join(fn(n) for n in ('proxy_agent_router_identity', 'proxy_agent_pool_blocked', 'proxy_agent_is_p0_launch', 'proxy_agent_p0_preempt',
                                    'proxy_agent_cap_exempt'))
P0_STUBS = r'''
proxy_agent_pool_live() { cat "$LIVE_FILE"; }
'''


def p0_pool(tmp_path, *, live=5, cap=5, model='claude-opus-5-5', ref='', brief_first='ordinary brief', helper_rc=0, frees=True, armed=True,
            preempt=1, dry=False, role='hd-builder', cap_name='CLAUDE_PROXY_CAP'):
    """Run proxy_agent_pool_blocked for one model. Returns (BLOCKED|FREE, helper args or None, PA_P0_WAIT, stderr)."""
    helper = tmp_path / 'helper.py'
    record = tmp_path / 'helper-args.json'
    live_file = tmp_path / 'live'
    live_file.write_text(f'{live}\n')
    helper.write_text('import json, os, sys\n'
                      f'open({str(record)!r}, "w").write(json.dumps(sys.argv[1:]))\n'
                      + (f'open({str(live_file)!r}, "w").write("{max(live - 1, 0)}\\n")\n' if frees else '')
                      + f'sys.exit({helper_rc})\n')
    brief = tmp_path / 'brief'
    brief.write_text(brief_first + '\n')
    env = {**os.environ, 'HOME': str(tmp_path), 'LIVE_FILE': str(live_file), cap_name: str(cap)}
    for k in ('PROXY_AGENT_PREEMPT', 'PROXY_AGENT_DRY_RUN', 'PROXY_AGENT_CAP_OVERRIDE', 'PROXY_AGENT_NAME'):
        env.pop(k, None)
    if preempt is not None:
        env['PROXY_AGENT_PREEMPT'] = str(preempt)
    if dry:
        env['PROXY_AGENT_DRY_RUN'] = '1'
    script = (f'set -u; TYPE={role}; ROLE_BRIEF={brief}; PA_P0_REF={ref!r}; PA_P0_WAIT=""; PROXY_AGENT_NAME=lane-under-test; '
              f'PROXY_AGENT_PREEMPT_HELPER={helper}; {P0_STUBS}{P0_FUNCS}\n'
              + 'if ' + ('PA_P0_ARMED=1 ' if armed else '') + f'proxy_agent_pool_blocked {model}; then echo BLOCKED; else echo FREE; fi\n'
              'echo "WAIT=${PA_P0_WAIT}"')
    done = bash(script, env=env)
    out = done.stdout.split()
    assert out, f'harness produced no output: rc={done.returncode} stderr={done.stderr}'
    called = json.loads(record.read_text()) if record.exists() else None
    return out[0], called, next((x[5:] for x in out if x.startswith('WAIT=')), ''), done.stderr


def test_a_p0_at_cap_preempts_in_its_own_pool_and_takes_the_slot(tmp_path):
    state, called, wait, _ = p0_pool(tmp_path, ref='P0:14600')
    assert state == 'FREE' and wait == ''
    assert called == ['--launch-name', 'lane-under-test', '--model', 'claude-opus-5-5', '--pool-full', 'claude', '--override', 'P0:14600',
                      '--brief', str(tmp_path / 'brief')]


def test_a_brief_first_line_p0_preempts_the_same_way(tmp_path):
    state, called, _wait, _ = p0_pool(tmp_path, brief_first='PRIORITY: P0')
    assert state == 'FREE' and called is not None and called[called.index('--override') + 1] == ''


@pytest.mark.parametrize('kwargs', [{}, {'brief_first': 'PRIORITY: P1'}, {'brief_first': 'x\nPRIORITY: P0'}])
def test_a_non_p0_at_cap_is_blocked_and_never_calls_the_helper(tmp_path, kwargs):
    state, called, wait, _ = p0_pool(tmp_path, **kwargs)
    assert state == 'BLOCKED' and called is None and wait == ''


@pytest.mark.parametrize('rc', [3, 4, 1, 2])
def test_a_p0_with_nothing_preemptable_is_queued_never_launched_over_the_cap(tmp_path, rc):
    state, called, wait, err = p0_pool(tmp_path, ref='P0:14600', helper_rc=rc, frees=False)
    assert state == 'BLOCKED' and wait == 'claude' and called is not None
    assert 'nothing preemptable' in err and 'not moved to another pool or tier' in err


def test_a_victim_that_exits_but_leaves_the_pool_full_still_queues_the_p0(tmp_path):
    state, _called, wait, err = p0_pool(tmp_path, ref='P0:14600', live=7, cap=5)  # over cap by two: one stop frees no slot
    assert state == 'BLOCKED' and wait == 'claude' and 'still at cap' in err


def test_the_off_switch_queues_the_p0_without_calling_the_helper(tmp_path):
    state, called, wait, err = p0_pool(tmp_path, ref='P0:14600', preempt=0)
    assert state == 'BLOCKED' and called is None and wait == 'claude' and 'preemption is unavailable' in err


def test_preemption_is_off_by_default_and_on_only_for_the_armed_primary_pick(tmp_path):
    state, called, wait, _ = p0_pool(tmp_path, ref='P0:1', preempt=None)    # PROXY_AGENT_PREEMPT unset = the launcher default = off
    assert state == 'BLOCKED' and called is None and wait == 'claude'
    (tmp_path / 'on').mkdir()
    assert p0_pool(tmp_path / 'on', ref='P0:1')[0] == 'FREE'                # PROXY_AGENT_PREEMPT=1
    (tmp_path / 'unarmed').mkdir()
    state, called, wait, _ = p0_pool(tmp_path / 'unarmed', ref='P0:1', armed=False)  # nested alias picks / availability probes never preempt
    assert state == 'BLOCKED' and called is None and wait == ''


def test_a_pool_that_is_off_cap_zero_is_never_a_preemption_target(tmp_path):
    state, called, _wait, _ = p0_pool(tmp_path, ref='P0:1', live=0, cap=0)
    assert state == 'BLOCKED' and called is None


def test_a_dry_run_passes_dry_run_and_trusts_the_plan_without_recounting(tmp_path):
    state, called, _wait, _ = p0_pool(tmp_path, ref='P0:1', dry=True, frees=False)
    assert state == 'FREE' and called[-1] == '--dry-run'


def test_neither_the_override_nor_a_priority_p0_brief_exempts_a_role_from_a_cap(tmp_path):
    script = (f'set -u; {P0_STUBS}{fn("proxy_agent_cap_exempt")}\n'
              f'ROLE_BRIEF={tmp_path}/b; printf "PRIORITY: P0\\n" > $ROLE_BRIEF; PROXY_AGENT_CAP_OVERRIDE=P0:1\n'
              'proxy_agent_cap_exempt hd-builder && echo EXEMPT || echo NOT_EXEMPT\nproxy_agent_cap_exempt ci-repair-agent && echo EXEMPT || echo NOT_EXEMPT')
    assert bash(script).stdout.split() == ['NOT_EXEMPT', 'EXEMPT']  # only the unrelated role exemption survives


def test_the_p0_override_is_read_once_as_identity_and_removed_from_the_environment():
    block = LAUNCHER[LAUNCHER.index('PA_P0_REF=""; PA_P0_WAIT=""; PA_P0_ARMED=0'):]
    block = block[:block.index('\n', block.index('unset PROXY_AGENT_CAP_OVERRIDE'))]
    for override, kept in (('P0:14600', ''), ('rajiv:1795439359.022079', 'rajiv:1795439359.022079'), ('', '')):
        done = bash(block + '\necho "REF=$PA_P0_REF KEPT=${PROXY_AGENT_CAP_OVERRIDE:-}"', env={**os.environ, 'PROXY_AGENT_CAP_OVERRIDE': override})
        assert done.stdout.strip() == f"REF={override if override.startswith('P0:') else ''} KEPT={kept}", done.stdout


def test_the_deepseek_total_cap_and_burn_gates_no_longer_accept_a_p0_override():
    gates = [ln for ln in LAUNCHER.splitlines() if 'PROXY_AGENT_CAP_OVERRIDE' in ln and '=~' in ln and not ln.lstrip().startswith('#')]
    assert gates and not any('P0:' in ln for ln in gates[1:]), gates  # discovered over every override check (the first is the one-time identity capture)
    assert 'deepseek_requires_rajiv_override' in LAUNCHER and 'deepseek_requires_p0_override' not in LAUNCHER


def test_the_old_post_selection_hook_that_launched_the_p0_over_the_cap_is_gone():
    assert 'launching the P0 anyway' not in LAUNCHER and '"${PROXY_AGENT_PREEMPT:-0}" == 0' in LAUNCHER


def burn_refused(tmp_path, model, gate='enforce'):
    (tmp_path / 'proxy-burn-shadow.py').write_text('import sys\nsys.exit(75)\n')  # a gate that would refuse everything
    funcs = ''.join(re.search(rf'^{n}\(\) \{{.*?^\}}$', LAUNCHER, re.S | re.M).group(0) + '\n'
                    for n in ('proxy_agent_burn_enforced', 'proxy_agent_burn_refused'))
    stubs = ('proxy_agent_is_capped() { return 0; }; proxy_agent_router_identity() { printf "%s" "$1"; }; '
             'proxy_agent_is_opencode_id() { return 1; }; proxy_agent_is_sarvam_id() { return 1; }; ')
    script = f'PROXY_BURN_GATE={gate}; TYPE=hd-builder; {stubs}{funcs}\nproxy_agent_burn_refused "$1"'
    return bash(script, str(tmp_path / 'launcher'), model).returncode


@pytest.mark.parametrize('model', ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5'])
def test_claude_is_never_burn_refused_even_when_the_gate_would_refuse(tmp_path, model):
    assert burn_refused(tmp_path, model) == 1  # 1 = not refused


def test_the_burn_gate_still_refuses_codex_and_is_off_unless_enforced(tmp_path):
    assert burn_refused(tmp_path, 'gpt-6.2-sol') == 0  # refused: Codex keeps its gate
    assert burn_refused(tmp_path, 'grok-4.7') == 1
    assert burn_refused(tmp_path, 'gpt-6.2-sol', gate='shadow') == 1


def test_shadow_script_reports_claude_cap_only_and_allows():
    shadow = (SCRIPTS / 'proxy-burn-shadow.py').read_text()
    assert "pool == 'claude'" in shadow and 'claude_cap_only' in shadow


def test_launcher_and_helper_parse():
    assert bash('bash -n "$1"', 'bash', str(SCRIPTS / 'proxy-agent.sh')).returncode == 0  # $0 is the 2nd word, the file is $1
    compile(HELPER_SRC, 'proxy-preempt.py', 'exec')


# ---- P0 slot mode (--pool-full), relaunch spec, WIP preservation, audit log, last-5-minutes -----------------------------------------

POOL_FULL = ('--model', 'claude-opus-5-5', '--override', 'P0:14600', '--pool-full', 'claude')


def test_pool_full_mode_stops_the_lowest_priority_lane_of_that_pool_and_reports_a_free_slot(w, capsys):
    w.lane('lane-p1', brief='PRIORITY: P1\n')
    w.lane('lane-p2', brief='PRIORITY: P2\n')
    pc = w.lane('lane-p3', brief='PRIORITY: P3\n')
    other = w.lane('grok-p3', model='grok-4.7', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 3)
    assert w.run(*POOL_FULL) == 0
    assert w.signals == [(pc, signal.SIGTERM)] and other in w.alive  # same pool only, lowest priority only
    assert 'PREEMPT victim=lane-p3' in capsys.readouterr().out


@pytest.mark.parametrize('kwargs', [{'brief': 'PRIORITY: P0\n'}, {'type': 'pm-admission-reviewer', 'brief': 'PRIORITY: P3\n'}, {'brief': 'no priority\n'}])
def test_pool_full_mode_with_nothing_preemptable_is_rc_3_with_no_signal_and_no_row(w, capsys, kwargs):
    w.lane('only', **kwargs)
    fill_claude_pool(w, 1)
    assert w.run(*POOL_FULL) == 3
    assert 'PREEMPT_REFUSED reason=nothing_preemptible' in capsys.readouterr().out and w.nothing_happened()


def test_pool_full_mode_never_lets_a_non_p0_or_a_tooling_failure_or_the_off_switch_through(w, monkeypatch, capsys):
    w.lane('a', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 1)
    assert w.run('--model', 'claude-opus-5-5', '--pool-full', 'claude') == 3        # not a P0
    assert w.run(*POOL_FULL, enabled='0') == 3                                       # off switch
    monkeypatch.setattr(pp.subprocess, 'check_output', lambda *a, **k: (_ for _ in ()).throw(OSError('no ps')))
    assert pp.main(['--launch-name', 'p0', *POOL_FULL], env={}, ps_text=None) == 3  # no ps: no bypass, the P0 queues
    assert w.nothing_happened()


def test_pool_full_mode_with_room_in_the_pool_is_already_a_free_slot(w, capsys):
    w.lane('a', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 5)
    assert w.run(*POOL_FULL) == 0 and 'PREEMPT not_needed' in capsys.readouterr().out and w.nothing_happened()


def test_a_lane_inside_the_last_five_minutes_of_its_expected_window_is_never_preempted(w, capsys):
    import datetime as dt
    started = '2026-11-23T01:00:00Z'
    now = 1_795_400_000.0
    began = dt.datetime.fromisoformat(started.replace('Z', '+00:00')).timestamp()
    w.lane('ending', brief='PRIORITY: P3\n', started=started)
    mf = w.tmp / 'runs' / 'ending' / 'manifest.json'
    data = json.loads(mf.read_text())
    data['expected_minutes'] = (now - began + 200) / 60  # 200 s left
    mf.write_text(json.dumps(data))
    fill_claude_pool(w, 1)
    assert w.run(*POOL_FULL, now=now) == 3
    assert 'ending:last_5_min' in capsys.readouterr().out and w.nothing_happened()
    data['expected_minutes'] = (now - began - 60) / 60  # already overdue: preemptable again
    mf.write_text(json.dumps(data))
    assert w.run(*POOL_FULL, now=now) == 0


def test_the_relaunch_row_is_capacity_wait_and_carries_the_victims_launch_spec(w):
    w.lane('lane-p2', brief='PRIORITY: P2\n', issue='14567', type='hd-builder')
    active = w.tmp / 'runs' / 'lane-p2' / 'active'
    active.mkdir()
    spec = {'type': 'hd-builder', 'brief': '/briefs/x.md', 'argv': ['hd-builder', '/briefs/x.md', '--model', 'claude-sonnet-5-5'],
            'env': {'PROXY_AGENT_PM_ROLE': '0'}, 'model_args': ['--model', 'claude-sonnet-5-5'], 'cwd': '/clone',
            'cmd': 'env PROXY_AGENT_PM_ROLE=0 PROXY_AGENT_NAME=lane-p2 /x/proxy-agent.sh hd-builder /briefs/x.md --model claude-sonnet-5-5'}
    (active / 'launch.json').write_text(json.dumps(spec))
    fill_claude_pool(w, 1)
    assert w.run(*POOL_FULL) == 0
    (args,) = w.obligations()
    pairs = dict(zip(args[1::2], args[2::2]))
    assert pairs['--blocker'] == 'CAPACITY_WAIT: preempted for P0 14600; relaunch when its pool has a free slot'
    relaunch = json.loads(pairs['--evidence-json'])['relaunch']
    assert relaunch['state'] == 'CAPACITY_WAIT' and relaunch['cmd'] == spec['cmd'] and relaunch['env'] == spec['env']
    assert relaunch['model_args'] == ['--model', 'claude-sonnet-5-5'] and relaunch['brief'] == '/briefs/x.md'
    assert relaunch['resume_cmd'].endswith('--resume lane-p2 --model claude-sonnet-5-5')


def test_a_victim_without_a_launch_spec_still_gets_a_relaunch_command_from_its_registry_row(w):
    w.lane('old-lane', brief='PRIORITY: P2\n', type='hd-proof-qa')
    fill_claude_pool(w, 1)
    assert w.run(*POOL_FULL) == 0
    relaunch = json.loads(dict(zip(w.obligations()[0][1::2], w.obligations()[0][2::2]))['--evidence-json'])['relaunch']
    assert 'hd-proof-qa' in relaunch['cmd'] and 'old-lane.brief' in relaunch['cmd'] and '--model claude-sonnet-5-5' in relaunch['cmd']


def test_every_preemption_is_logged_to_proxy_cap_overrides_log_only_once_the_lane_has_exited(w):
    w.lane('lane-p3', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 1)
    assert w.run(*POOL_FULL) == 0
    line = (w.tmp / 'overrides.log').read_text().strip()
    assert ' PREEMPTED lane-p3 for P0:14600 pool=claude model=claude-sonnet-5-5 launcher=p0-launch ' in line
    w.exit_on_term = False
    w.lane('stubborn', brief='PRIORITY: P3\n', started='2026-11-23T05:00:00Z')
    assert w.run(*POOL_FULL) == 4
    assert (w.tmp / 'overrides.log').read_text().count('PREEMPTED') == 1  # a lane that did not exit is not logged as preempted


def test_hy4_is_its_own_pool_like_the_launcher_counts_it():
    assert pp.pool_of('hy4-flash-free') == 'hy4' and pp.pool_of('glm-5.3') == 'opencode' and 'hy4' in pp.POOL_CAPS


# WIP preservation: real git, a local bare "origin", a lane whose transcript names its lane-clone checkout.

def git(*a, cwd=None):
    return subprocess.run(['git', '-c', 'user.name=T', '-c', 'user.email=t@example.invalid', *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def lane_with_worktree(w, tmp_path, branch='fix/9-x', commits=1, dirty=False, push_first=True):
    origin = tmp_path / 'origin.git'
    git('init', '-q', '--bare', str(origin))
    wt = tmp_path / 'wt'
    git('init', '-q', '-b', branch, str(wt))
    git('remote', 'add', 'origin', str(origin), cwd=wt)
    (wt / 'f.txt').write_text('base\n')
    git('add', 'f.txt', cwd=wt)
    git('commit', '-q', '-m', 'base', cwd=wt)
    if push_first:
        git('push', '-q', 'origin', f'HEAD:refs/heads/{branch}', cwd=wt)
    for i in range(commits):
        (wt / f'c{i}.txt').write_text(f'{i}\n')
        git('add', '.', cwd=wt)
        git('commit', '-q', '-m', f'wip {i}', cwd=wt)
    if dirty:
        (wt / 'f.txt').write_text('dirty\n')
    event = {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Bash',
                                                           'input': {'command': f'~/.claude/scripts/lane-clone.sh clone {wt} origin/{branch}'}}]}}
    w.lane('wip-lane', brief='PRIORITY: P3\n', jsonl=[event])
    fill_claude_pool(w, 1)
    return origin, wt


def remote_tip(origin, branch):
    return git('--git-dir', str(origin), 'rev-parse', f'refs/heads/{branch}')


def test_clean_unpushed_commits_are_pushed_non_force_after_the_lane_exits(w, tmp_path):
    origin, wt = lane_with_worktree(w, tmp_path, commits=2)
    before = remote_tip(origin, 'fix/9-x')
    assert w.run(*POOL_FULL) == 0
    assert remote_tip(origin, 'fix/9-x') == git('rev-parse', 'HEAD', cwd=wt) != before
    assert 'wip=pushed:' in w.log_text() and f'worktree={wt}' in w.log_text()
    rows = [dict(zip(a[1::2], a[2::2])) for a in w.obligations()]
    assert len(rows) == 2 and json.loads(rows[1]['--evidence-json'])['wip']['status'] == 'pushed'  # the relaunch row learns where the WIP is


@pytest.mark.parametrize('why, kwargs', [('dirty_worktree', {'dirty': True}), ('nothing_unpushed', {'commits': 0})])
def test_a_dirty_or_fully_pushed_worktree_is_left_exactly_as_it_is(w, tmp_path, why, kwargs):
    origin, wt = lane_with_worktree(w, tmp_path, **kwargs)
    before, status = remote_tip(origin, 'fix/9-x'), git('status', '--porcelain', cwd=wt)
    assert w.run(*POOL_FULL) == 0
    assert remote_tip(origin, 'fix/9-x') == before and git('status', '--porcelain', cwd=wt) == status
    assert f'wip=left_intact:{why}' in w.log_text() and len(w.obligations()) == 1


def test_a_protected_branch_and_a_missing_worktree_are_never_pushed(w, tmp_path):
    origin, wt = lane_with_worktree(w, tmp_path, branch='main', commits=1)
    before = remote_tip(origin, 'main')
    assert w.run(*POOL_FULL) == 0
    assert remote_tip(origin, 'main') == before and 'wip=left_intact:protected_or_detached_branch' in w.log_text()


def test_no_worktree_in_the_transcript_leaves_everything_alone(w):
    w.lane('plain', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 1)
    assert w.run(*POOL_FULL) == 0 and 'wip=left_intact:no_worktree_found' in w.log_text()


def test_a_push_the_remote_refuses_is_reported_and_never_forced(w, tmp_path):
    origin, wt = lane_with_worktree(w, tmp_path, commits=1)
    other = tmp_path / 'other'
    git('clone', '-q', '-b', 'fix/9-x', str(origin), str(other))
    (other / 'x.txt').write_text('x\n')
    git('add', '.', cwd=other)
    git('commit', '-q', '-m', 'someone else', cwd=other)
    git('push', '-q', 'origin', 'HEAD:refs/heads/fix/9-x', cwd=other)  # the remote moved: our push is not a fast-forward
    moved = remote_tip(origin, 'fix/9-x')
    assert w.run(*POOL_FULL) == 0
    assert remote_tip(origin, 'fix/9-x') == moved and 'wip=left_intact:' in w.log_text() and 'wip=pushed' not in w.log_text()  # unknown ahead-count: left alone


def test_wip_preservation_never_forces_commits_stashes_or_resets():
    fn_ast = ast.parse(inspect.getsource(pp.preserve_wip)).body[0]
    code = ast.unparse(ast.Module(body=fn_ast.body[1:], type_ignores=[]))  # the docstring is prose, not behaviour
    for banned in ("'--force'", "'-f'", "'--force-with-lease'", "'+HEAD'", "'commit'", "'stash'", "'reset'", "'checkout'", "'add'", "'rebase'", "'merge'"):
        assert banned not in code, banned
    assert "'push', 'origin'" in code


# ---- the picker: a P0 queues instead of falling to another pool or tier ------------------------------------------------------------
# The REAL proxy_agent_select / proxy_agent_pick_tier run with the tiers-test stubs (health probes, cooldowns, burn gate) but the REAL pool_blocked and p0 functions.

_tiers_spec = importlib.util.spec_from_file_location('tiers_for_p0', SCRIPTS / 'tests' / 'test_proxy_agent_tiers.py')
tiers = importlib.util.module_from_spec(_tiers_spec)
_tiers_spec.loader.exec_module(tiers)


def p0_route(tmp_path, role='hd-planner', *, p0=True, live=2, cap=2, helper_rc=3, frees=False, only_pool=None):
    """Route one launch through the real picker. Every pool is at cap (live >= cap) unless only_pool names the single full pool."""
    helper = tmp_path / 'helper.py'
    record = tmp_path / 'helper-calls.jsonl'
    live_file = tmp_path / 'live'
    live_file.write_text(f'{live}\n')
    helper.write_text('import json, sys\n'
                      f'open({str(record)!r}, "a").write(json.dumps(sys.argv[1:]) + "\\n")\n'
                      + (f'open({str(live_file)!r}, "w").write("{max(live - 1, 0)}\\n")\n' if frees else '')
                      + f'sys.exit({helper_rc})\n')
    brief = tmp_path / 'brief.txt'
    brief.write_text('plan it\n')
    extra = ''.join(fn(n) for n in ('proxy_agent_is_p0_launch', 'proxy_agent_p0_preempt'))
    body = ('proxy_agent_pool_live() { if [[ -n "$ONLY_POOL" && "$1" != "$ONLY_POOL" ]]; then echo 0; else cat "$LIVE_FILE"; fi; }\n'
            + extra + tiers.ROUTE_BODY)
    env = tiers.base_env(tmp_path, REQUESTED='auto', ROLE=role, BRIEF=str(brief), RUNG='', UNAVAILABLE='', PROBEFAIL='', LIVE_FILE=str(live_file),
                         ONLY_POOL=only_pool or '', PROXY_AGENT_PREEMPT_HELPER=str(helper), PROXY_AGENT_NAME='p0-lane', PROXY_AGENT_PREEMPT='1',
                         **{k: cap for k in ('CLAUDE_PROXY_CAP', 'CODEX_PROXY_CAP', 'GROK_PROXY_CAP', 'OPENCODE_PROXY_CAP', 'SARVAM_PROXY_CAP')})
    env.pop('PROXY_AGENT_STUB_POOL_LIVE', None)
    if p0:
        env['PA_P0_REF'] = 'P0:77'
    done = subprocess.run(['bash', '-c', tiers.PRELUDE + '\n' + body], env=env, text=True, capture_output=True)
    fields = done.stdout.strip().split('|')
    calls = [json.loads(x) for x in record.read_text().splitlines()] if record.exists() else []
    return fields, calls, done.stderr


def test_a_p0_whose_pool_is_at_cap_with_nothing_to_preempt_is_refused_75_and_never_reaches_sol(tmp_path):
    fields, calls, err = p0_route(tmp_path)
    assert fields[5] == '75' and fields[2] != 'gpt-6.2-sol'  # rc 75, no model: queued, not moved to the last (capped, forbidden) Sol entry
    assert 'REFUSED P0 queued' in err and 'no bypass, no other pool or tier' in err
    assert len(calls) == 1 and calls[0][calls[0].index('--pool-full') + 1] in ('grok', 'claude') and 'gpt-6.2-sol' not in calls[0]  # asked once, for the first full pool


def test_a_p0_that_frees_a_slot_gets_its_pools_normal_model(tmp_path):
    fields, calls, err = p0_route(tmp_path, helper_rc=0, frees=True, live=2, cap=2)
    assert fields[5] == '0' and fields[2] == 'grok-4.7'  # TOP's first entry, in the pool that was just freed
    assert len(calls) == 1 and calls[0][calls[0].index('--model') + 1] == 'grok-4.7' and calls[0][calls[0].index('--pool-full') + 1] == 'grok'


def test_a_p0_whose_first_pool_has_room_never_calls_the_helper(tmp_path):
    fields, calls, _ = p0_route(tmp_path, only_pool='claude', cap=2, live=2)  # grok has 0 live
    assert fields[2] == 'grok-4.7' and calls == []


def test_a_non_p0_at_cap_never_preempts_and_still_skips_to_the_next_model(tmp_path):
    fields, calls, err = p0_route(tmp_path, p0=False, only_pool='grok', cap=2, live=2)
    assert calls == [] and fields[2] != 'grok-4.7' and 'pool_cap skip model=grok-4.7' in err


# ---- fence: the stop path fences the lane (name + run id) BEFORE the custody re-check and the signal, and always clears it ----------------------
# CTO C0ALZJHGE49 thread 1795441441.525659, ts 1795441848.817099: "keep the P0 queued when the pool is full until the stop path fences new lease and
# external-effect acquisition, then rechecks custody under that fence before signaling. Keep the durable relaunch obligation ahead of SIGTERM."

def fence_of(w, name='victim-lane'):
    return w.tmp / 'fences' / f'{name}.tok-{name}.json'


def test_the_lane_is_fenced_during_the_custody_recheck_and_the_signal_and_unfenced_afterwards(w):
    seen = {}
    w.lane('victim-lane', brief='PRIORITY: P3\n')
    path = fence_of(w)

    def at_recheck():  # the second ledger read is the re-check under the fence
        seen['recheck'] = json.loads(path.read_text())['by'] if path.exists() else None
        seen['obligations_then'] = len(w.obligations())

    def killer(pid, sig):
        seen['signal'] = path.exists()
        seen['obligations_at_signal'] = len(w.obligations())
        w.killer(pid, sig)
    w.mutations[2] = at_recheck
    fill_claude_pool(w, 1)
    assert w.run(*P0, killer=killer) == 0
    # relaunch row first, then the fence (set by the launcher p0-launch), custody re-read under it, then SIGTERM with the fence still up
    assert seen == {'recheck': 'p0-launch', 'obligations_then': 1, 'signal': True, 'obligations_at_signal': 1}
    assert w.signals and not path.exists()


@pytest.mark.parametrize('under_fence', ['box lease', 'queue ticket', 'push', 'box tool'])
def test_an_acquisition_or_effect_that_started_before_the_fence_aborts_the_preemption_and_clears_the_fence(w, capsys, under_fence):
    pid = w.lane('victim-lane', brief='PRIORITY: P3\n')
    start = {'box lease': lambda: w.box('hetzner-1', 'leased', 'qa-victim-lane-12'),
             'queue ticket': lambda: w.ticket('qa-victim-lane-12'),
             'push': lambda: w.child(pid, 'git push origin HEAD'),
             'box tool': lambda: w.child(pid, 'bash /Users/rajiv/.claude/scripts/remote-test-box.sh up --pr 9')}[under_fence]
    w.mutations[2] = start  # it appears between the first custody read and the re-check made under the fence
    fill_claude_pool(w, 1)
    assert w.run(*P0) == 3  # the P0 stays queued: rc 3 = nothing preemptable
    assert 'PREEMPT_REFUSED reason=custody_changed victim=victim-lane' in capsys.readouterr().out
    assert w.signals == [] and pid in w.alive and not fence_of(w).exists() and not (w.tmp / 'stop').exists()
    assert [a[0] for a in w.obligations()] == ['obligation-upsert', 'obligation-resolve']  # the relaunch row is withdrawn with the abort


@pytest.mark.parametrize('how', ['exited', 'still_alive', 'signal_failed', 'lease_unverifiable', 'custody_changed'])
def test_the_fence_is_cleared_on_every_path_out_of_the_stop(w, how):
    w.lane('victim-lane', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 1)
    kwargs = {}
    if how == 'still_alive':
        w.exit_on_term = False
    elif how == 'lease_unverifiable':
        w.mutations[2] = lambda: w.cli.update({('pool', 'list'): (1, '')})
    elif how == 'custody_changed':
        w.mutations[2] = lambda: w.box('hetzner-1', 'leased', 'qa-victim-lane-12')
    elif how == 'signal_failed':
        def refuse(pid, sig):
            raise PermissionError('not ours')
        kwargs['killer'] = refuse
    rc = w.run(*P0, **kwargs)
    assert rc == {'exited': 0, 'still_alive': 4, 'signal_failed': 4, 'lease_unverifiable': 3, 'custody_changed': 3}[how]
    assert not fence_of(w).exists()


def test_no_persisted_obligation_means_no_fence_and_no_signal(w):
    w.stub_mode('fail')
    w.lane('victim-lane', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 1)
    assert w.run(*P0) == 3 and w.signals == [] and not (w.tmp / 'fences').exists()


def test_a_dry_run_sets_no_fence(w, capsys):
    w.lane('victim-lane', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 1)
    assert w.run(*P0, '--dry-run') == 0 and 'PREEMPT_PLAN victim=victim-lane' in capsys.readouterr().out
    assert not (w.tmp / 'fences').exists() and w.nothing_happened()


def test_a_lane_already_fenced_by_another_preemption_is_skipped_and_its_fence_is_left_alone(w, capsys):
    w.lane('victim-lane', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 1)
    assert pp._fence_mod().set_fence(w.tmp / 'fences', 'victim-lane', 'tok-victim-lane', 'other-preemptor', 600, 1_795_400_000.0)[0]
    assert w.run(*P0) == 3
    assert 'victim-lane:already_fenced' in capsys.readouterr().out and w.nothing_happened() and fence_of(w).exists()


def test_losing_the_race_for_the_fence_refuses_with_nothing_signalled_and_the_obligation_withdrawn(w, capsys, monkeypatch):
    class Lost:  # another preemptor set the fence between the candidate check and ours
        @staticmethod
        def read_fence(*a, **k):
            return None

        @staticmethod
        def set_fence(*a, **k):
            return False, 'fence_held'

        @staticmethod
        def clear_fence(*a, **k):
            raise AssertionError('a fence this preemptor did not set must never be cleared')
    monkeypatch.setattr(pp, '_fence_mod', lambda: Lost)
    pid = w.lane('victim-lane', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 1)
    assert w.run(*P0) == 3
    assert 'PREEMPT_REFUSED reason=fence_not_set detail=fence_held victim=victim-lane' in capsys.readouterr().out
    assert w.signals == [] and pid in w.alive and not (w.tmp / 'stop').exists()
    assert [a[0] for a in w.obligations()] == ['obligation-upsert', 'obligation-resolve']


def test_without_the_fence_helper_nothing_is_ever_signalled(w, capsys, monkeypatch):
    monkeypatch.setattr(pp, '_fence_mod', lambda: None)
    w.lane('victim-lane', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 1)
    assert w.run(*P0) == 3
    assert 'reason=fence_not_set detail=fence_helper_unavailable' in capsys.readouterr().out and w.signals == []


def test_a_lane_with_no_run_id_cannot_be_fenced_so_it_is_not_a_victim(w, capsys):
    w.lane('victim-lane', brief='PRIORITY: P3\n', token=None)
    fill_claude_pool(w, 1)
    assert w.run(*P0) == 3
    assert 'victim-lane:no_run_id' in capsys.readouterr().out and w.nothing_happened()


def test_the_next_lowest_priority_lane_is_taken_when_the_lowest_has_no_run_id(w):
    w.lane('no-id-p3', brief='PRIORITY: P3\n', token=None)
    pid = w.lane('with-id-p2', brief='PRIORITY: P2\n')
    fill_claude_pool(w, 2)
    assert w.run(*P0) == 0 and [p for p, _ in w.signals] == [pid]
