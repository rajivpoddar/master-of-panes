#!/usr/bin/env python3
"""P0 preemption for proxy-agent.sh: free ONE proxy slot for a P0 launch by stopping the lowest-priority live lane.

Policy (Rajiv C0ALZJHGE49 thread 1795369444.132999, ts 1795372352.279289, 23 Nov: "for P0's, preempt the lowest priority proxy
agent"). proxy-agent.sh calls this just before it starts a P0 lane. Behaviour:

  * Not a P0 launch (no PROXY_AGENT_CAP_OVERRIDE=P0:<issue> and no brief first line `PRIORITY: P0`)  -> rc 0, `PREEMPT not_p0`.
  * The launch's pool is under its cap and the total cap is not reached                              -> rc 0, `PREEMPT not_needed`.
  * Otherwise pick ONE victim among the live registered lanes (the launch's pool when the pool is full, any pool when only the
    total cap is full). Priority comes from the brief's `PRIORITY: Pn` header, else the GitHub P0..P3 label of the issue/PR the
    run registry row carries (the row's issue/pr come from the brief's ISSUE:/PR: header). P3 < P2 < P1: the numerically HIGHEST
    rank is preempted, ties go to the youngest lane (least sunk work). Never preemptible: another P0, an unknown priority,
    pm-admission-reviewer / 12b lanes, a lane mid remote-box lease or mid push, the launcher itself, a lane already being stopped.
  * Victim found -> write ~/.claude/state/proxy-stop-requests/<name> (checkpoint + stop notice), SIGTERM the lane, wait up to
    --wait seconds (default 120) for it to exit, record a pm-ops `proxy_relaunch` obligation, append one line to
    ~/.claude/state/proxy-preemptions.log.                                                                 -> rc 0 (rc 4 if still alive)
  * Nothing preemptible -> log `PREEMPT_REFUSED`, rc 3. proxy-agent.sh still launches the P0 (a P0 is never blocked by a cap).

Never signals anything but the one chosen `claude -p --name <victim>` process. --dry-run selects and prints; it signals, writes,
logs and records nothing. The pm-ops CLI is PROXY_AGENT_PM_OPS_CLI (same contract as proxy-run-state.py), so tests stub it.
"""
import argparse
import datetime
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
RUNS = Path(os.environ.get('PROXY_AGENT_RUN_STATE_DIR', str(HOME / '.local/state/proxy-agent-runs')))
STATE = HOME / '.claude/state'
CAPS_ENV = Path(os.environ.get('PROXY_PREEMPT_CAPS_ENV', str(STATE / 'proxy-caps.env')))
STOP_DIR = Path(os.environ.get('PROXY_PREEMPT_STOP_DIR', str(STATE / 'proxy-stop-requests')))
LOG = Path(os.environ.get('PROXY_PREEMPT_LOG', str(STATE / 'proxy-preemptions.log')))
PM_OPS_CLI = os.environ.get('PROXY_AGENT_PM_OPS_CLI', str(HOME / '.claude/scripts/pm-ops.py'))

# Same roles proxy-cap-count.py counts toward the total cap.
CAPPED = frozenset({'hd-planner', 'hd-builder', 'hd-proof-qa', 'pm-hotfix', 'pm-proof-qa', 'hd-investigator', 'superproofer'})
RANK = {'P0': 0, 'P1': 1, 'P2': 2, 'P3': 3}  # same order as ready_pool_select.PRIORITY_RANK; anything else is unknown
# Pool -> (env vars in precedence order, default). The defaults are proxy-agent.sh's `${VAR:-default}` fallbacks.
POOL_CAPS = {'claude': (('CLAUDE_PROXY_CAP', 'PROXY_AGENT_CLAUDE_CAP'), 5), 'codex': (('CODEX_PROXY_CAP',), 8),
             'grok': (('GROK_PROXY_CAP',), 8), 'opencode': (('OPENCODE_PROXY_CAP',), 2), 'sarvam': (('SARVAM_PROXY_CAP',), 4),
             'spark': (('SPARK_PROXY_CAP', 'QWEN_PROXY_CAP'), 6), 'inkling': (('INKLING_PROXY_CAP',), 8)}
CAPS_ENV_KEYS = {k for names, _ in POOL_CAPS.values() for k in names}

# The same lane-process shape proxy_agent_pool_live and proxy-cap-count.py match.
LANE = re.compile(r'(?:^|\s)\S*/claude\s+-p\s+--name\s+([A-Za-z0-9._-]+)\s+--model\s+(\S+)')
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z')
OVERRIDE_P0 = re.compile(r'^P0:[0-9]+$')
BRIEF_P0 = re.compile(r'^\s*(?:PRIORITY|priority)\s*:\s*P0(?:$|\s)')
BRIEF_PRIO = re.compile(r'^\s*priority\s*:\s*(P[0-3])\b', re.I | re.M)
ADMISSION = re.compile(r'12b|admission', re.I)
# A lane that has leased a remote box, or is pushing, is never interrupted (a stop would orphan a billed box or half-push a branch).
BOX = re.compile(r'remote-(?:test|browser|qa)-box|remote-qa-runner|remote-test-runner|hcloud\s+server|box[-_ ]lease|lease[-_ ]acquire', re.I)
PUSH = re.compile(r'\bgit\s+(?:-C\s+\S+\s+)?(?:push|ls-remote)\b|\bgh\s+pr\s+(?:create|merge|edit|ready)\b|pm-admit-ci\.sh', re.I)
TAIL_BYTES = 262144


def pool_of(model):
    """Pool of a launch/lane model id (the same cut proxy_agent_pool_live uses: lowercase, drop the first '(' or '[' suffix)."""
    m = re.split(r'[(\[]', str(model).lower(), maxsplit=1)[0].strip()
    if m.startswith('claude-'):
        return 'claude'
    if m.startswith('gpt-'):
        return 'codex'
    if m.startswith('grok'):
        return 'grok'
    if m in ('ornith-1.5-35b-a3b', 'qwen3.8-flash-next'):
        return 'spark'
    if m == 'inkling-2-mini':
        return 'inkling'
    if m in ('hy4-flash-free', 'deepseek-v4.1-flash', 'glm-5.3', 'muse-spark-1.3-contributor'):
        return 'opencode'
    if m in ('sarvam-glm-5.3', 'sarvam-deepseek-v4.1-flash', 'sarvam-105b', 'sarvam-gemma4', 'sarvam-kimi-k3'):
        return 'sarvam'
    return ''


def pool_cap(pool, env):
    """Cap as proxy-agent.sh resolves it: explicit env wins, then proxy-caps.env, then the shell default. None = unreadable."""
    names, default = POOL_CAPS.get(pool, ((), None))
    if default is None:
        return None
    for name in names:
        if re.fullmatch(r'[0-9]+', env.get(name, '')):
            return int(env[name])
    try:
        file_caps = {}
        for line in CAPS_ENV.read_text().splitlines():
            k, _, v = line.partition('=')
            if k in CAPS_ENV_KEYS and re.fullmatch(r'[0-9]+', v.strip()):
                file_caps[k] = int(v.strip())
        for name in names:
            if name in file_caps:
                return file_caps[name]
    except OSError:
        pass
    return default


def read_text(path, limit=TAIL_BYTES):
    try:
        with open(path, 'rb') as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            return fh.read().decode('utf-8', 'replace')
    except (OSError, TypeError, ValueError):
        return ''


def live_lanes(ps_text):
    """[{name, model, pool, pid, line}] for every `claude -p --name N --model M` process line (one per line, like the shell count)."""
    lanes = []
    for line in ps_text.splitlines():
        m = LANE.search(line)
        if not m or not NAME.fullmatch(m.group(1)):
            continue
        pid = re.match(r'\s*(\d+)\s', line)
        lanes.append({'name': m.group(1), 'model': m.group(2), 'pool': pool_of(m.group(2)), 'pid': int(pid.group(1)) if pid else None})
    return lanes


def manifest_of(name):
    try:
        data = json.loads((RUNS / name / 'manifest.json').read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def brief_text(manifest):
    for key in ('brief', 'original_brief'):
        text = read_text(manifest.get(key), 16384) if manifest.get(key) else ''
        if text:
            return text
    return ''


def label_priority(kind, number, labels_cmd):
    """Best (lowest-rank) P0..P3 label of an issue (kind 'issue') or PR (kind 'pr'), or None when unreadable.
    labels_cmd: a command that is run as `<cmd> <kind> <number>` and prints the labels (tests; default is gh in the cwd's repo)."""
    try:
        if labels_cmd:
            out = subprocess.run([*labels_cmd.split(), kind, str(number)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 text=True, timeout=20)
        else:
            out = subprocess.run(['gh', 'pr' if kind == 'pr' else 'issue', 'view', str(number), '--json', 'labels', '--jq',
                                  '.labels|map(.name)|join(",")'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 text=True, timeout=20)
        if out.returncode:
            return None
    except (OSError, subprocess.SubprocessError):
        return None
    found = [p for p in re.split(r'[\s,]+', out.stdout.strip()) if p in RANK]
    return min(found, key=RANK.get) if found else None


def lane_priority(manifest, labels_cmd):
    """(priority or None, source). The brief header wins; then the registry row's issue, then its PR."""
    m = BRIEF_PRIO.search(brief_text(manifest)[:4096])
    if m:
        return m.group(1).upper(), 'brief'
    for key in ('issue', 'pr'):
        if str(manifest.get(key) or '').isdigit():
            p = label_priority(key, manifest[key], labels_cmd)
            if p:
                return p, f'{key}#{manifest[key]}'
    return None, 'none'


def busy(manifest):
    """'box_lease' / 'push' when the lane's last log lines or an unanswered / open tool call show it mid remote-box lease or push."""
    out = str(manifest.get('out') or '')
    base = out[:-3] if out.endswith('.md') else out
    last_lines = [ln for ln in read_text(base + '.log', 8192).splitlines() if ln.strip()][-3:]
    tail = '\n'.join(last_lines)
    pending = {}
    for raw in read_text(base + '.jsonl').splitlines():
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        content = (ev.get('message') or {}).get('content') if isinstance(ev, dict) else None
        for part in content if isinstance(content, list) else []:
            if not isinstance(part, dict):
                continue
            if part.get('type') == 'tool_use':
                cmd = str((part.get('input') or {}).get('command') or '')
                pending[part.get('id')] = cmd  # unanswered until its tool_result arrives: the lane is inside that command right now
            elif part.get('type') == 'tool_result':
                pending.pop(part.get('tool_use_id'), None)
    inflight = '\n'.join(pending.values())
    if BOX.search(tail) or BOX.search(inflight):
        return 'box_lease'
    if PUSH.search(tail) or PUSH.search(inflight):
        return 'push'
    return ''


def select(lanes, launch_name, scope_pool, labels_cmd, now, capped_only=False):
    """(victim or None, skipped). skipped = [(name, reason)]. Candidates are registered, live, in scope, de-duplicated by name.
    capped_only: only the total cap is full, so a victim must be a capped role (stopping any other lane frees no total slot)."""
    skipped, cands, seen = [], [], set()
    for lane in lanes:
        name = lane['name']
        if name in seen:
            continue
        seen.add(name)
        if name == launch_name:
            skipped.append((name, 'launcher'))
            continue
        if scope_pool and lane['pool'] != scope_pool:
            continue
        manifest = manifest_of(name)
        if manifest is None:
            skipped.append((name, 'unregistered'))
            continue
        rtype = str(manifest.get('type') or '')
        if capped_only and rtype not in CAPPED:
            skipped.append((name, 'not_capped_role'))
            continue
        head = brief_text(manifest)[:600]
        if rtype == 'pm-admission-reviewer' or ADMISSION.search(name) or re.search(r'12b|pm-admission', head, re.I):
            skipped.append((name, 'admission_review'))
            continue
        prio, source = lane_priority(manifest, labels_cmd)
        if prio is None:
            skipped.append((name, 'unknown_priority'))
            continue
        if prio == 'P0':
            skipped.append((name, 'p0'))
            continue
        try:
            fresh = now - (STOP_DIR / name).stat().st_mtime < 300
        except OSError:
            fresh = False
        if fresh:
            skipped.append((name, 'already_stopping'))
            continue
        why = busy(manifest)
        if why:
            skipped.append((name, why))
            continue
        cands.append({**lane, 'type': rtype, 'priority': prio, 'source': source, 'manifest': manifest,
                      'started': str(manifest.get('started_at') or '')})
    if not cands:
        return None, skipped
    worst = max(RANK[c['priority']] for c in cands)
    pool = sorted((c for c in cands if RANK[c['priority']] == worst), key=lambda c: (c['started'], c['name']), reverse=True)
    return pool[0], skipped


def iso(now):
    return datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def log_line(text):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, 'a') as fh:
        fh.write(text + '\n')


def record_obligation(victim, launcher, p0, now):
    mf = victim['manifest']
    extra = []
    if str(mf.get('issue') or '').isdigit():
        extra += ['--issue', str(mf['issue'])]
    if str(mf.get('pr') or '').isdigit():
        extra += ['--pr', str(mf['pr'])]
    evidence = {'preempted': victim['name'], 'type': victim['type'], 'model': victim['model'], 'priority': victim['priority'],
                'brief': mf.get('brief') or mf.get('original_brief'), 'out': mf.get('out'), 'cwd': mf.get('cwd'),
                'for_launch': launcher, 'p0': p0, 'at': iso(now)}
    args = ['obligation-upsert', '--kind', 'proxy_relaunch', '--severity', 'high', '--target-type', 'proxy',
            '--target-id', victim['name'], '--owner', 'pm', '--horizon', 'hourly',
            '--title', 'Relaunch preempted proxy ' + victim['name'],
            '--action', f"Relaunch {victim['name']} ({victim['type']}, {victim['priority']}) from its brief after P0 {p0} "
                        'is running: it was stopped for P0 preemption', *extra, '--evidence-json', json.dumps(evidence)]
    try:
        done = subprocess.run([sys.executable, PM_OPS_CLI, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        return done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def stop_victim(victim, launcher, p0, wait_s, now, killer, alive, sleep):
    """Write the stop request, SIGTERM the lane, wait. Returns (exited, waited_seconds)."""
    STOP_DIR.mkdir(parents=True, exist_ok=True)
    note = (f"PREEMPT_STOP name={victim['name']} by={launcher} p0={p0} at={iso(now)}\n"
            'Checkpoint now: commit and push nothing new, state your resume point in your final line, and end the turn.\n'
            'PM records a relaunch obligation; your brief and out file stay as they are.\n')
    tmp = STOP_DIR / f".{victim['name']}.tmp"
    tmp.write_text(note)
    os.replace(tmp, STOP_DIR / victim['name'])
    waited = 0
    if victim['pid'] is None:
        return False, waited
    try:
        killer(victim['pid'], signal.SIGTERM)
    except ProcessLookupError:
        return True, waited
    except OSError:
        return False, waited
    while waited < wait_s:
        if not alive(victim['pid']):
            return True, waited
        sleep(1)
        waited += 1
    return (not alive(victim['pid'])), waited


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def main(argv=None, env=None, ps_text=None, killer=os.kill, alive=_alive, sleep=time.sleep, now=None):
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--launch-name', default='')
    ap.add_argument('--model', required=True, help='model id of the lane about to launch')
    ap.add_argument('--override', default='', help='PROXY_AGENT_CAP_OVERRIDE of the launch')
    ap.add_argument('--brief', default='', help='ROLE_BRIEF of the launch (a first line `PRIORITY: P0` also marks a P0)')
    ap.add_argument('--total-cap', default='', help='PROXY_AGENT_TOTAL_CAP in force for the launch')
    ap.add_argument('--wait', type=int, default=int(env.get('PROXY_PREEMPT_WAIT_S', '120') or 120))
    ap.add_argument('--dry-run', '--dry', action='store_true')
    args = ap.parse_args(argv)
    now = time.time() if now is None else now

    first = ''
    if args.brief:
        try:
            with open(args.brief) as fh:
                first = fh.readline()
        except OSError:
            first = ''
    p0_issue = args.override.split(':', 1)[1] if OVERRIDE_P0.match(args.override) else ''
    if not (p0_issue or BRIEF_P0.match(first)):
        print('PREEMPT not_p0')
        return 0
    p0 = p0_issue or 'brief'

    if ps_text is None:
        try:
            ps_text = subprocess.check_output(['/bin/ps', '-axo', 'pid=,command='], text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            print('PREEMPT UNAVAILABLE reason=ps_failed')
            return 0  # never block a P0 on a tooling failure; the cap exemption already lets it through
    lanes = live_lanes(ps_text)
    pool = pool_of(args.model)
    cap = pool_cap(pool, env) if pool else None
    live_pool = sum(1 for lane in lanes if lane['pool'] == pool) if pool else 0
    pool_full = cap is not None and live_pool > 0 and live_pool >= cap
    total_cap = int(args.total_cap) if args.total_cap.isdigit() else None
    names = {lane['name'] for lane in lanes}
    live_total = sum(1 for n in names if (manifest_of(n) or {}).get('type') in CAPPED)
    total_full = total_cap is not None and live_total >= total_cap
    if not (pool_full or total_full):
        print(f'PREEMPT not_needed pool={pool or "unknown"} live={live_pool} cap={cap} total_live={live_total} total_cap={total_cap}')
        return 0

    labels_cmd = env.get('PROXY_PREEMPT_LABELS_CMD', '')
    victim, skipped = select(lanes, args.launch_name, pool if pool_full else '', labels_cmd, now, capped_only=not pool_full)
    why = f'pool={pool}:{live_pool}/{cap}' if pool_full else f'total={live_total}/{total_cap}'
    skips = ','.join(f'{n}:{r}' for n, r in skipped) or 'none'
    if victim is None:
        line = f'{iso(now)} PREEMPT_REFUSED reason=nothing_preemptible for={args.launch_name or "?"} p0={p0} {why} skipped={skips}'
        if not args.dry_run:
            log_line(line)
        print(line.split(' ', 1)[1])
        return 3
    desc = (f"victim={victim['name']} type={victim['type']} model={victim['model']} priority={victim['priority']} "
            f"source={victim['source']} for={args.launch_name or '?'} p0={p0} {why}")
    if args.dry_run:
        print(f'PREEMPT_PLAN {desc}')
        return 0
    exited, waited = stop_victim(victim, args.launch_name or '?', p0, max(0, args.wait), now, killer, alive, sleep)
    recorded = record_obligation(victim, args.launch_name or '?', p0, now)
    outcome = 'exited' if exited else 'still_alive'
    line = f'{iso(now)} PREEMPT {desc} outcome={outcome} waited={waited}s obligation={"recorded" if recorded else "failed"}'
    log_line(line)
    print(line.split(' ', 1)[1])
    return 0 if exited else 4


if __name__ == '__main__':
    sys.exit(main())
