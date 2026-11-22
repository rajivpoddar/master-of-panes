"""P0 preemption for proxy-agent.sh (proxy-preempt.py) and the Claude no-burn-gate contract of the launcher.

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
        for attr, val in (('RUNS', tmp / 'runs'), ('STOP_DIR', tmp / 'stop'), ('LOG', tmp / 'preemptions.log'),
                          ('CAPS_ENV', tmp / 'caps.env'), ('PM_OPS_CLI', str(self.stub)), ('RTB_ROOT', self.rtb)):
            monkeypatch.setattr(pp, attr, val)

    def caps(self, **kv):
        (self.tmp / 'caps.env').write_text(''.join(f'{k}={v}\n' for k, v in kv.items()))

    def stub_mode(self, mode):
        (self.tmp / 'stub-mode').write_text(mode)

    def lane(self, name, model='claude-sonnet-5-5', type='hd-builder', brief='PRIORITY: P2\n', started='2026-11-23T01:00:00Z',
             issue=None, pr=None, log='', jsonl=None, register=True, ppid=1):
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

    def run(self, *argv, env=None, now=1_795_400_000.0, enabled='1'):
        base = ['--launch-name', 'p0-launch', '--wait', '3']
        full_env = {} if enabled is None else {'PROXY_AGENT_PREEMPT': enabled}
        full_env.update(env or {})
        return pp.main([*base, *argv], env=full_env, ps_text=lambda: '\n'.join(self.ps), killer=self.killer,
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


# ---- paused by default ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('enabled', [None, '0', ''])
def test_a_real_run_is_disabled_unless_preempt_is_switched_on(w, capsys, enabled):
    w.lane('a', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 1)
    assert w.run(*P0, enabled=enabled) == 0
    assert 'PREEMPT disabled' in capsys.readouterr().out
    assert w.nothing_happened() and w.log_text() == '' and w.snapshots == 0  # not even the lease tooling was consulted


def test_dry_run_still_plans_while_disabled_because_it_changes_nothing(w, capsys):
    w.lane('lane-p3', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 1)
    assert w.run(*P0, '--dry-run', enabled=None) == 0
    assert 'PREEMPT_PLAN victim=lane-p3' in capsys.readouterr().out and w.nothing_happened()


def test_the_launcher_default_is_off_and_this_helper_is_not_the_default_path():
    assert 'if [[ "${PROXY_AGENT_PREEMPT:-0}" != 0 ]]; then' in LAUNCHER
    assert not re.search(r'PROXY_AGENT_PREEMPT:-1|PROXY_AGENT_PREEMPT=1\b', LAUNCHER)


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
        kind = re.match(r'\s*(?:kind = )?"([a-z]*)"', seg).group(1)
        if not kind or ' if ' not in seg:
            continue
        for literal in re.findall(r'"([a-z0-9][a-z0-9.\-]*)"', seg.split(' if ', 1)[1]):
            seen[literal] = kind
            assert pp.pool_of(literal) == kind, f'{literal}: launcher={kind} helper={pp.pool_of(literal)!r}'
    assert {'claude', 'codex', 'grok', 'spark', 'inkling', 'opencode', 'sarvam'} <= set(seen.values())
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
    assert main_src.index('record_obligation(') < main_src.index('holds_for(') < main_src.index('stop_victim(')
    assert main_src.index('custody_snapshot(') < main_src.index('record_obligation(')
    assert 'PROXY_AGENT_PREEMPT' in main_src and main_src.index('PREEMPT disabled') < main_src.index('custody_snapshot(')


# ---- packaging in the release manifest -------------------------------------------------------------------------------------------

@pytest.mark.skipif(not MANIFEST.exists(), reason='release manifest is not beside this copy (installed ~/.claude tree)')
def test_the_helper_and_its_test_are_packaged_in_the_release_manifest_with_current_digests():
    entries = {e['source_path']: e for e in json.loads(MANIFEST.read_text())['entries']}
    for rel in ('claude/scripts/proxy-preempt.py', 'claude/scripts/tests/test_proxy_preempt.py'):
        entry = entries[rel]
        assert entry['sha256'] == hashlib.sha256((MANIFEST.parent / rel).read_bytes()).hexdigest(), f'{rel}: manifest digest is stale'
        assert entry['canonical_target'].endswith('/.claude/scripts/' + rel.split('claude/scripts/', 1)[1])
    assert entries['claude/scripts/proxy-preempt.py']['mode'] == 0o644  # live mode; a mismatch refuses activation ("no pinned old manifest preimage")


# ---- launcher: hook wiring and the Claude no-burn-gate contract (functions/blocks extracted from the deployed proxy-agent.sh) ----

def bash(script, *args, env=None):
    return subprocess.run(['bash', '-c', script, *args], capture_output=True, text=True, env=env, timeout=30)


HOOK = LAUNCHER[LAUNCHER.index('# P0 preemption (Rajiv C0ALZJHGE49 thread 1795369444.132999'):]
HOOK = HOOK[:HOOK.index('ARGS=(-p --name')]


def hook_run(tmp_path, *, override='', brief_first=None, enabled='1', helper_rc=0, name=''):
    """enabled: '1' switches preemption on, 0/'0' sets it off explicitly, None leaves it unset (the launcher's default)."""
    helper = tmp_path / 'helper.py'
    record = tmp_path / 'helper-args.json'
    helper.write_text(f'import json, sys\nopen({str(record)!r}, "w").write(json.dumps(sys.argv[1:]))\nsys.exit({helper_rc})\n')
    brief = tmp_path / 'brief'
    brief.write_text(brief_first if brief_first is not None else 'ordinary brief\n')
    script = ('set -euo pipefail; unset PROXY_AGENT_NAME PROXY_AGENT_CAP_OVERRIDE PROXY_AGENT_PREEMPT; '  # never inherit the real lane's identity
              + (f'PROXY_AGENT_NAME={name}; ' if name else '')
              + 'TYPE=hd-builder; STAMP=s1; LAUNCH_MODEL=claude-opus-5-5; PROXY_AGENT_TOTAL_CAP=8; '
              f'ROLE_BRIEF={brief}; PROXY_AGENT_CAP_OVERRIDE={override!r}; PROXY_AGENT_PREEMPT_HELPER={helper}; '
              + (f'PROXY_AGENT_PREEMPT={enabled}; ' if enabled is not None else '') + HOOK + '\necho REACHED_ARGS')
    done = bash(script)
    called = json.loads(record.read_text()) if record.exists() else None
    return done, called


def test_hook_calls_the_helper_for_a_p0_override_with_the_launch_facts(tmp_path):
    done, called = hook_run(tmp_path, override='P0:14600')
    assert done.returncode == 0 and 'REACHED_ARGS' in done.stdout
    assert called == ['--launch-name', 'proxy-hd-builder-s1', '--model', 'claude-opus-5-5', '--override', 'P0:14600',
                      '--brief', str(tmp_path / 'brief'), '--total-cap', '8']


def test_hook_passes_the_launchers_own_lane_name_so_it_is_never_its_own_victim(tmp_path):
    done, called = hook_run(tmp_path, override='P0:14600', name='proxy-ops-x')
    assert done.returncode == 0 and called[called.index('--launch-name') + 1] == 'proxy-ops-x'


def test_hook_calls_the_helper_for_a_brief_first_line_p0(tmp_path):
    done, called = hook_run(tmp_path, brief_first='PRIORITY: P0\nrest\n')
    assert done.returncode == 0 and called is not None and called[called.index('--override') + 1] == ''


@pytest.mark.parametrize('kwargs', [{}, {'override': 'rajiv:1795.1'}, {'brief_first': 'PRIORITY: P1\n'}, {'brief_first': 'x\nPRIORITY: P0\n'},
                                    {'override': 'P0:9', 'enabled': 0}, {'override': 'P0:9', 'enabled': None},
                                    {'brief_first': 'PRIORITY: P0\n', 'enabled': None}])
def test_hook_skips_non_p0_the_off_switch_and_the_default_off(tmp_path, kwargs):
    done, called = hook_run(tmp_path, **kwargs)
    assert done.returncode == 0 and 'REACHED_ARGS' in done.stdout and called is None  # unset PROXY_AGENT_PREEMPT never reaches the helper


@pytest.mark.parametrize('rc', [3, 4, 1])
def test_hook_never_stops_the_p0_launch_whatever_the_helper_returns(tmp_path, rc):
    done, called = hook_run(tmp_path, override='P0:14600', helper_rc=rc)
    assert done.returncode == 0 and 'REACHED_ARGS' in done.stdout and called is not None  # set -e did not abort the launcher
    assert f'P0 preemption rc={rc} (launching the P0 anyway)' in done.stderr


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
