"""P0 preemption for proxy-agent.sh (proxy-preempt.py) and the Claude no-burn-gate contract of the launcher.

Rajiv C0ALZJHGE49 thread 1795369444.132999, ts 1795372352.279289 (23 Nov): "for P0's, preempt the lowest priority proxy agent" and
"drop the P0 gate ... we don't have to track the burn rate for calude proxy accounts".

Hermetic: lanes are fixture manifests plus fake `ps` text, signals go to an injected killer, the pm-ops CLI is a stub script
(PROXY_AGENT_PM_OPS_CLI contract), registries/log/stop files live under tmp_path. Nothing signals a real process, posts to Slack,
or touches the real pm-ops DB. Run: /opt/homebrew/bin/pytest -q -p no:cacheprovider tests/test_proxy_preempt.py
"""
import importlib.util
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


class World:
    """Temp registry + fake process table + recording signal/clock hooks."""

    def __init__(self, tmp, monkeypatch):
        self.tmp = tmp
        self.ps = []
        self.alive = set()
        self.signals = []
        self.exit_on_term = True
        self.next_pid = 1000
        (tmp / 'runs').mkdir()
        (tmp / 'caps.env').write_text('')
        self.stub = tmp / 'pm-ops-stub.py'
        self.stub.write_text('import json, sys\nopen(%r, "a").write(json.dumps(sys.argv[1:]) + "\\n")\n' % str(tmp / 'obligations.jsonl'))
        for attr, val in (('RUNS', tmp / 'runs'), ('STOP_DIR', tmp / 'stop'), ('LOG', tmp / 'preemptions.log'),
                          ('CAPS_ENV', tmp / 'caps.env'), ('PM_OPS_CLI', str(self.stub))):
            monkeypatch.setattr(pp, attr, val)

    def caps(self, **kv):
        (self.tmp / 'caps.env').write_text(''.join(f'{k}={v}\n' for k, v in kv.items()))

    def lane(self, name, model='claude-sonnet-5-5', type='hd-builder', brief='PRIORITY: P2\n', started='2026-11-23T01:00:00Z',
             issue=None, pr=None, log='', jsonl=None, register=True):
        pid = self.next_pid
        self.next_pid += 1
        self.alive.add(pid)
        self.ps.append(f'  {pid} /opt/homebrew/bin/claude -p --name {name} --model {model} --max-turns 200 --output-format stream-json')
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

    def killer(self, pid, sig):
        self.signals.append((pid, sig))
        if self.exit_on_term:
            self.alive.discard(pid)

    def run(self, *argv, env=None, now=1_795_400_000.0):
        base = ['--launch-name', 'p0-launch', '--wait', '3']
        return pp.main([*base, *argv], env={} if env is None else env, ps_text='\n'.join(self.ps), killer=self.killer,
                       alive=lambda pid: pid in self.alive, sleep=lambda _s: None, now=now)

    def obligations(self):
        path = self.tmp / 'obligations.jsonl'
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def log_text(self):
        path = self.tmp / 'preemptions.log'
        return path.read_text() if path.exists() else ''


@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


P0 = ('--model', 'claude-opus-5-5', '--override', 'P0:14600')


def fill_claude_pool(w, cap=3):
    w.caps(CLAUDE_PROXY_CAP=cap)


def test_not_a_p0_launch_does_nothing(w, capsys):
    w.lane('a', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 1)
    assert w.run('--model', 'claude-opus-5-5') == 0
    assert 'PREEMPT not_p0' in capsys.readouterr().out
    assert w.signals == [] and w.log_text() == '' and w.obligations() == []


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
    assert 'for=p0-launch p0=14600 pool=claude:2/2 outcome=exited waited=0s obligation=recorded' in line
    (args,) = w.obligations()
    assert args[:1] == ['obligation-upsert']
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
    ('box_lease', {'brief': 'PRIORITY: P3\n', 'log': 'booting\nremote-test-box.sh run --spec x\n'}),
    ('box_lease', {'brief': 'PRIORITY: P3\n', 'jsonl': [
        {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 't1', 'input': {'command': 'bash remote-qa-runner spec a'}}]}}]}),
    ('push', {'brief': 'PRIORITY: P3\n', 'log': 'tests ok\ngit push origin HEAD\n'}),
    ('push', {'brief': 'PRIORITY: P3\n', 'jsonl': [
        {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 't9', 'input': {'command': 'git -C /x push origin HEAD'}}]}}]}),
])
def test_never_preempts_these_lanes_and_refuses_when_nothing_is_left(w, capsys, why, kwargs):
    w.lane('only', **kwargs)
    fill_claude_pool(w, 1)
    assert w.run(*P0) == 3
    out = capsys.readouterr().out
    assert 'PREEMPT_REFUSED reason=nothing_preemptible' in out and f'only:{why}' in out
    assert w.signals == [] and w.obligations() == [] and not (w.tmp / 'stop').exists()
    assert 'PREEMPT_REFUSED' in w.log_text() and f'only:{why}' in w.log_text()


@pytest.mark.parametrize('name', ['pm-12b-review-14600', 'x-admission-y'])
def test_a_12b_or_admission_lane_is_recognised_by_name_alone(w, capsys, name):
    w.lane(name, type='hd-builder', brief='PRIORITY: P3\nplain brief\n')
    fill_claude_pool(w, 1)
    assert w.run(*P0) == 3
    assert f'{name}:admission_review' in capsys.readouterr().out and w.signals == []


def test_a_finished_tool_call_does_not_count_as_mid_push_or_mid_lease(w):
    done = [{'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 't1', 'input': {'command': 'git push origin HEAD'}},
                                                          {'type': 'tool_use', 'id': 't2', 'input': {'command': 'remote-test-box.sh run'}}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't1'}, {'type': 'tool_result', 'tool_use_id': 't2'}]}}]
    pid = w.lane('idle-after-push', brief='PRIORITY: P3\n', log='all done\n', jsonl=done)
    fill_claude_pool(w, 1)
    assert w.run(*P0) == 0
    assert w.signals == [(pid, signal.SIGTERM)]


def test_skips_the_busy_lane_and_takes_the_next_lowest(w):
    w.lane('busy-p3', brief='PRIORITY: P3\n', log='remote-browser-box.sh spec x\n')
    ok = w.lane('free-p2', brief='PRIORITY: P2\n')
    w.lane('p1', brief='PRIORITY: P1\n')
    fill_claude_pool(w, 3)
    assert w.run(*P0) == 0
    assert w.signals == [(ok, signal.SIGTERM)]


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
    assert 'PREEMPT not_needed' in capsys.readouterr().out and w.signals == []


def test_still_alive_after_the_wait_is_reported_and_exits_nonzero(w):
    w.exit_on_term = False
    w.lane('stubborn', brief='PRIORITY: P3\n')
    fill_claude_pool(w, 1)
    assert w.run(*P0) == 4
    assert 'outcome=still_alive waited=3s obligation=recorded' in w.log_text()
    assert len(w.obligations()) == 1  # the relaunch is still owed


def test_dry_run_selects_but_changes_nothing(w, capsys):
    w.lane('lane-p3', brief='PRIORITY: P3\n')
    w.lane('lane-p1', brief='PRIORITY: P1\n')
    fill_claude_pool(w, 2)
    assert w.run(*P0, '--dry-run') == 0
    assert 'PREEMPT_PLAN victim=lane-p3' in capsys.readouterr().out
    assert w.signals == [] and w.log_text() == '' and w.obligations() == [] and not (w.tmp / 'stop').exists()


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
    rc = pp.main(['--launch-name', 'p0-launch', *P0], env={}, ps_text=None)
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


# ---- launcher: hook wiring and the Claude no-burn-gate contract (functions/blocks extracted from the deployed proxy-agent.sh) ----

def bash(script, *args, env=None):
    return subprocess.run(['bash', '-c', script, *args], capture_output=True, text=True, env=env, timeout=30)


HOOK = LAUNCHER[LAUNCHER.index('# P0 preemption (Rajiv C0ALZJHGE49 thread 1795369444.132999'):]
HOOK = HOOK[:HOOK.index('ARGS=(-p --name')]


def hook_run(tmp_path, *, override='', brief_first=None, enabled=None, helper_rc=0, name=''):
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
                                    {'override': 'P0:9', 'enabled': 0}])
def test_hook_skips_non_p0_and_the_off_switch(tmp_path, kwargs):
    done, called = hook_run(tmp_path, **kwargs)
    assert done.returncode == 0 and 'REACHED_ARGS' in done.stdout and called is None


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
    compile((SCRIPTS / 'proxy-preempt.py').read_text(), 'proxy-preempt.py', 'exec')
