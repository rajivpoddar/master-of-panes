#!/usr/bin/env python3
"""P0 preemption for proxy-agent.sh: free ONE proxy slot for a P0 launch by stopping the lowest-priority live lane.

STATUS: PAUSED. proxy-agent.sh calls this only when PROXY_AGENT_PREEMPT is non-zero (default 0), and this helper refuses a real
(non --dry-run) run unless PROXY_AGENT_PREEMPT is non-zero as well. The CTO re-reviews before the default is turned back on
(C0ALZJHGE49 thread 1795385404.941879, ts 1795385573.907659). P0 launches continue without preemption meanwhile.

Policy (Rajiv C0ALZJHGE49 thread 1795369444.132999, ts 1795372352.279289, 23 Nov: "for P0's, preempt the lowest priority proxy
agent"). Behaviour:

  * Not a P0 launch (no PROXY_AGENT_CAP_OVERRIDE=P0:<issue> and no brief first line `PRIORITY: P0`)  -> rc 0, `PREEMPT not_p0`.
  * The launch's pool is under its cap and the total cap is not reached                              -> rc 0, `PREEMPT not_needed`.
  * Otherwise rank the live registered lanes (the launch's pool when the pool is full, any pool when only the total cap is full).
    Priority comes from the brief's `PRIORITY: Pn` header, else the GitHub P0..P3 label of the issue/PR the run registry row
    carries. The numerically HIGHEST rank is preempted first, ties go to the youngest lane. Never preemptible: another P0, an
    unknown priority, pm-admission-reviewer / 12b lanes, the launcher itself, a lane already being stopped, a lane with no pid.
  * CUSTODY (this is the gate; log heuristics are not consulted). Before any signal the target's CURRENT custody is read from the
    real lease tooling (rtb-pool-lease.py `list`, `queue`, `pending` over every pool ledger, plus its eph records) and from the
    live process table. A victim is refused when any non-free box lease, queue ticket, unresolved create or eph record binds to it
    (by lane name, its issue/PR number, or its pid / a descendant's pid), or when a live descendant process is a remote-box tool or
    a push. If the lease tooling or process table cannot be read, nothing is preempted (PREEMPT_REFUSED reason=lease_unverifiable).
  * ORDER for the chosen victim: (1) persist the pm-ops `proxy_relaunch` obligation (--print-id) and require its id, (2) re-read
    custody, (3) write ~/.claude/state/proxy-stop-requests/<name>, (4) SIGTERM, wait up to --wait seconds, (5) log. A failure at (1)
    or (2) signals nothing and withdraws any obligation it created; a failed SIGTERM withdraws it too.
  * Nothing preemptible -> log `PREEMPT_REFUSED`, rc 3. proxy-agent.sh still launches the P0 (a P0 is never blocked by a cap).

Never signals anything but the one chosen `claude -p --name <victim>` process. --dry-run selects and prints: it sends no signal and
writes no stop file, log line or obligation (its lease-CLI reads are the tool's own list/queue/pending reads). The pm-ops CLI is
PROXY_AGENT_PM_OPS_CLI (same contract as proxy-run-state.py) and the lease CLI is PROXY_PREEMPT_LEASE_CLI, so tests stub both.
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
LEASE_CLI = os.environ.get('PROXY_PREEMPT_LEASE_CLI', str(HOME / '.claude/scripts/rtb-pool-lease.py'))
RTB_ROOT = Path(os.environ.get('RTB_STATE') or str(HOME / '.cache/rtb-state'))
POOL_DIRS = ('pool', 'pool-cx33', 'pool-cx43')  # the browser (cx43), test (cx33) and original ledgers rtb-pool-lease.py serves

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
PS_ROW = re.compile(r'\s*(\d+)\s+(\d+)\s+(\S.*)')  # pid ppid command, as `ps -axo pid=,ppid=,command=` prints it
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z')
OVERRIDE_P0 = re.compile(r'^P0:[0-9]+$')
BRIEF_P0 = re.compile(r'^\s*(?:PRIORITY|priority)\s*:\s*P0(?:$|\s)')
BRIEF_PRIO = re.compile(r'^\s*priority\s*:\s*(P[0-3])\b', re.I | re.M)
ADMISSION = re.compile(r'12b|admission', re.I)
# A LIVE descendant process of the target that is a remote-box tool or a push means the lane is inside that effect right now.
# These are matched against process command lines (the process table), never against a lane's log or transcript.
PROC_BOX = re.compile(r'remote-(?:test|browser|qa)-box|remote-qa-runner|remote-test-runner|rtb-pool-lease|hcloud\s+server', re.I)
PROC_PUSH = re.compile(r'\bgit\s+(?:-C\s+\S+\s+)?(?:push|ls-remote)\b|\bgh\s+pr\s+(?:create|merge|edit|ready)\b|pm-admit-ci\.sh', re.I)
TAIL_BYTES = 262144
LEASE_TIMEOUT_S = 30


class LeaseUnverifiable(Exception):
    """The target's lease/pending custody could not be read, so no preemption may proceed."""


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
    """[{name, model, pool, pid}] for every `claude -p --name N --model M` process line (one per line, like the shell count)."""
    lanes = []
    for line in ps_text.splitlines():
        m = LANE.search(line)
        if not m or not NAME.fullmatch(m.group(1)):
            continue
        pid = re.match(r'\s*(\d+)\s', line)
        lanes.append({'name': m.group(1), 'model': m.group(2), 'pool': pool_of(m.group(2)), 'pid': int(pid.group(1)) if pid else None})
    return lanes


def process_tree(ps_text):
    """{pid: (ppid, command)} from `pid ppid command` rows. Rows without a ppid are ignored (so a victim missing here is unverifiable)."""
    tree = {}
    for line in ps_text.splitlines():
        m = PS_ROW.match(line)
        if m:
            tree[int(m.group(1))] = (int(m.group(2)), m.group(3))
    return tree


def descendants(tree, pid):
    """Every live descendant pid of `pid` (children, grandchildren, ...), excluding `pid` itself."""
    kids = {}
    for p, (pp, _cmd) in tree.items():
        kids.setdefault(pp, []).append(p)
    out, todo = [], list(kids.get(pid, []))
    while todo:
        p = todo.pop()
        if p in out or p == pid:
            continue
        out.append(p)
        todo.extend(kids.get(p, []))
    return out


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


# ---- custody: the real lease tooling, never log heuristics ------------------------------------------------------------------

def run_lease_cli(argv, pool_dir):
    """(rc, stdout) of `rtb-pool-lease.py <argv>` against ONE ledger dir (RTB_POOL_DIR). Spawn failure / timeout -> unverifiable."""
    child_env = {**os.environ, 'RTB_POOL_DIR': str(pool_dir)}
    try:
        done = subprocess.run([sys.executable, LEASE_CLI, *argv], env=child_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=LEASE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LeaseUnverifiable(f'lease_cli_{type(exc).__name__}')
    return done.returncode, done.stdout


def _pid_of(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def _json_file(path):
    """dict, or None when the file is missing / unreadable / not an object."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _json_lines(text, what):
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            raise LeaseUnverifiable(f'{what}_unparsable')
        if not isinstance(row, dict):
            raise LeaseUnverifiable(f'{what}_not_an_object')
        rows.append(row)
    return rows


def custody_snapshot(root, runner):
    """Every non-free custody row across the lease ledgers: [{kind, id, owner, state, pids}].
    kinds: box (a lease in any state but free), ticket (queue ticket), pending (unresolved create), eph (ephemeral/burst record).
    Raises LeaseUnverifiable when any ledger or any CLI call cannot be read, so the caller fails closed."""
    dirs = [Path(root) / name for name in POOL_DIRS if (Path(root) / name).is_dir()]
    if not dirs:
        raise LeaseUnverifiable('no_lease_ledger')
    rows = []
    for d in dirs:
        rc, out = runner(['list'], d)
        if rc != 0:
            raise LeaseUnverifiable(f'list_rc{rc}:{d.name}')
        for line in out.splitlines():
            if not line.strip():
                continue
            fields = line.split('\t')
            if len(fields) < 3:
                raise LeaseUnverifiable(f'list_unparsable:{d.name}')
            if fields[0].startswith('queue:') or fields[1] == 'free':
                continue  # tickets come from `queue` below; a free box has no custody
            lease = _json_file(d / f'{fields[0]}.lease') or {}
            rows.append({'kind': 'box', 'id': fields[0], 'owner': fields[2], 'state': fields[1], 'pids': [p for p in [_pid_of(lease.get('pid'))] if p]})
        rc, out = runner(['queue'], d)
        if rc != 0:
            raise LeaseUnverifiable(f'queue_rc{rc}:{d.name}')
        for t in _json_lines(out, 'queue'):
            ticket = str(t.get('ticket') or '')
            file = _json_file(d / 'queue' / f'{ticket}.json') or {}
            rows.append({'kind': 'ticket', 'id': ticket, 'owner': str(t.get('owner') or ''), 'state': 'pending_box' if t.get('pending_box') else 'waiting',
                         'pids': [p for p in [_pid_of(file.get('pid'))] if p]})
        rc, out = runner(['pending'], d)
        if rc != 0:
            raise LeaseUnverifiable(f'pending_rc{rc}:{d.name}')
        for r in _json_lines(out, 'pending'):
            if str(r.get('state') or '') == 'rejected':
                continue  # the lease tool's own rule: only a rejected create does not block
            rows.append({'kind': 'pending', 'id': str(r.get('slot') or r.get('name') or '?'), 'owner': '', 'state': str(r.get('state') or 'unresolved'),
                         'pids': [p for p in [_pid_of(r.get('pid'))] if p]})
        eph = d / 'eph'
        if eph.is_dir():
            try:
                names = sorted(n for n in os.listdir(eph) if n.endswith('.json'))
            except OSError:
                raise LeaseUnverifiable(f'eph_unreadable:{d.name}')
            for n in names:
                rec = _json_file(eph / n)
                if rec is None:
                    raise LeaseUnverifiable(f'eph_unreadable:{d.name}/{n}')
                rows.append({'kind': 'eph', 'id': str(rec.get('name') or n[:-5]), 'owner': str(rec.get('owner') or ''), 'state': str(rec.get('state') or 'recorded'),
                             'pids': [p for p in [_pid_of(rec.get('pid'))] if p]})
    return rows


def holds_for(victim, rows, tree):
    """Why the victim may NOT be signalled: custody rows bound to it plus live remote-box / push descendants. [] = free to stop.
    Binding is by lane name, the registry row's issue/PR number (owner tokens like `hd-14487-rework`), or pid ancestry (the victim's
    pid, a descendant's pid, or the pid an owner string ends with). Owners are free-form, so this fails toward refusing."""
    pid = victim['pid']
    if pid not in tree:
        raise LeaseUnverifiable('victim_not_in_process_table')
    family = {pid, *descendants(tree, pid)}
    manifest = victim.get('manifest') or {}
    tokens = [re.compile(r'(?<![A-Za-z0-9])' + re.escape(victim['name']) + r'(?![A-Za-z0-9])', re.I)]
    for key in ('issue', 'pr'):
        number = str(manifest.get(key) or '')
        if number.isdigit() and len(number) >= 3:
            tokens.append(re.compile(r'(?<![A-Za-z0-9])' + number + r'(?![A-Za-z0-9])'))
    holds = []
    for row in rows:
        owner_pid = re.search(r'-(\d{2,})$', row['owner'])
        pids = set(row['pids']) | ({int(owner_pid.group(1))} if owner_pid else set())
        if any(t.search(row['owner']) for t in tokens) or (pids & family):
            holds.append(f"{row['kind']}:{row['id']}:{row['state']}")
    for p in sorted(family - {pid}):
        cmd = tree[p][1]
        if PROC_BOX.search(cmd):
            holds.append(f'process:{p}:remote_box')
        elif PROC_PUSH.search(cmd):
            holds.append(f'process:{p}:push')
    return holds


def candidates(lanes, launch_name, scope_pool, labels_cmd, now, capped_only=False):
    """(ordered victim candidates, skipped). skipped = [(name, reason)]. Candidates are registered, live, in scope, de-duplicated
    by name, ordered worst priority first then youngest first. capped_only: only the total cap is full, so a victim must be a
    capped role (stopping any other lane frees no total slot). Custody is NOT decided here: see holds_for."""
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
        if lane['pid'] is None:
            skipped.append((name, 'no_pid'))
            continue
        try:
            fresh = now - (STOP_DIR / name).stat().st_mtime < 300
        except OSError:
            fresh = False
        if fresh:
            skipped.append((name, 'already_stopping'))
            continue
        cands.append({**lane, 'type': rtype, 'priority': prio, 'source': source, 'manifest': manifest,
                      'started': str(manifest.get('started_at') or '')})
    cands.sort(key=lambda c: (RANK[c['priority']], c['started'], c['name']), reverse=True)
    return cands, skipped


def select(cands, skipped, rows, tree):
    """First candidate with no custody holds; every held candidate is added to skipped as `custody:<holds>`."""
    for cand in cands:
        holds = holds_for(cand, rows, tree)
        if not holds:
            return cand
        skipped.append((cand['name'], 'custody:' + '+'.join(holds)))
    return None


def iso(now):
    return datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def log_line(text):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, 'a') as fh:
        fh.write(text + '\n')


def record_obligation(victim, launcher, p0, now):
    """Persist the pm-ops `proxy_relaunch` obligation BEFORE any signal. Returns its integer id, or None when it was not persisted."""
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
                        'is running: it was stopped for P0 preemption', *extra, '--evidence-json', json.dumps(evidence), '--print-id']
    try:
        done = subprocess.run([sys.executable, PM_OPS_CLI, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    for line in reversed(done.stdout.splitlines()):
        if line.strip().isdigit():
            return int(line.strip())
    return None


def withdraw_obligation(oid, reason):
    """Best effort: close an obligation that was persisted for a stop that never happened."""
    try:
        done = subprocess.run([sys.executable, PM_OPS_CLI, 'obligation-resolve', '--kind', 'proxy_relaunch', '--id', str(oid),
                               '--reason', reason], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        return done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def stop_victim(victim, launcher, p0, wait_s, now, killer, alive, sleep):
    """Write the stop request, SIGTERM the lane, wait. Returns (outcome, waited_seconds); outcome is exited / still_alive / signal_failed."""
    STOP_DIR.mkdir(parents=True, exist_ok=True)
    note = (f"PREEMPT_STOP name={victim['name']} by={launcher} p0={p0} at={iso(now)}\n"
            'Checkpoint now: commit and push nothing new, state your resume point in your final line, and end the turn.\n'
            'PM records a relaunch obligation; your brief and out file stay as they are.\n')
    tmp = STOP_DIR / f".{victim['name']}.tmp"
    tmp.write_text(note)
    os.replace(tmp, STOP_DIR / victim['name'])
    waited = 0
    try:
        killer(victim['pid'], signal.SIGTERM)
    except ProcessLookupError:
        return 'exited', waited
    except OSError:
        try:
            (STOP_DIR / victim['name']).unlink()
        except OSError:
            pass
        return 'signal_failed', waited
    while waited < wait_s:
        if not alive(victim['pid']):
            return 'exited', waited
        sleep(1)
        waited += 1
    return ('exited' if not alive(victim['pid']) else 'still_alive'), waited


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _read_ps():
    try:
        return subprocess.check_output(['/bin/ps', '-axo', 'pid=,ppid=,command='], text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None


def main(argv=None, env=None, ps_text=None, killer=os.kill, alive=_alive, sleep=time.sleep, now=None, lease_runner=None):
    env = os.environ if env is None else env
    lease_runner = lease_runner or run_lease_cli
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

    if not args.dry_run and str(env.get('PROXY_AGENT_PREEMPT', '0')).strip() in ('', '0'):
        print('PREEMPT disabled (PROXY_AGENT_PREEMPT is 0; the CTO re-reviews before it is turned on)')
        return 0

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

    def get_ps():  # ps_text: a fixed string, or a callable re-read before each custody check (tests); None = the real ps
        if callable(ps_text):
            return ps_text()
        return ps_text if ps_text is not None else _read_ps()

    table = get_ps()
    if table is None:
        print('PREEMPT UNAVAILABLE reason=ps_failed')
        return 0  # never block a P0 on a tooling failure; the cap exemption already lets it through
    lanes = live_lanes(table)
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
    launcher = args.launch_name or '?'
    why = f'pool={pool}:{live_pool}/{cap}' if pool_full else f'total={live_total}/{total_cap}'

    def refuse(reason, skipped=()):
        skips = ','.join(f'{n}:{r}' for n, r in skipped) or 'none'
        line = f'{iso(now)} PREEMPT_REFUSED reason={reason} for={launcher} p0={p0} {why} skipped={skips}'
        if not args.dry_run:
            log_line(line)
        print(line.split(' ', 1)[1])
        return 3

    cands, skipped = candidates(lanes, args.launch_name, pool if pool_full else '', labels_cmd, now, capped_only=not pool_full)
    if not cands:
        return refuse('nothing_preemptible', skipped)
    try:
        rows = custody_snapshot(RTB_ROOT, lease_runner)
        victim = select(cands, skipped, rows, process_tree(table))
    except LeaseUnverifiable as exc:
        return refuse(f'lease_unverifiable detail={exc}', skipped)
    if victim is None:
        return refuse('nothing_preemptible', skipped)
    desc = (f"victim={victim['name']} type={victim['type']} model={victim['model']} priority={victim['priority']} "
            f"source={victim['source']} for={launcher} p0={p0} {why}")
    if args.dry_run:
        print(f'PREEMPT_PLAN {desc}')
        return 0

    # (1) custody first: nothing is signalled unless the relaunch obligation is persisted and has an id.
    oid = record_obligation(victim, launcher, p0, now)
    if oid is None:
        return refuse(f"custody_not_persisted victim={victim['name']}", skipped)
    # (2) re-read custody and the process table now that the obligation exists; anything that changed in between wins.
    try:
        fresh_table = get_ps()
        if fresh_table is None:
            raise LeaseUnverifiable('ps_failed')
        holds = holds_for(victim, custody_snapshot(RTB_ROOT, lease_runner), process_tree(fresh_table))
    except LeaseUnverifiable as exc:
        withdraw_obligation(oid, f'preempt withdrawn before any signal: lease_unverifiable {exc}')
        return refuse(f"lease_unverifiable detail={exc} victim={victim['name']}", skipped)
    if holds:
        withdraw_obligation(oid, 'preempt withdrawn before any signal: custody appeared ' + '+'.join(holds))
        return refuse(f"custody_changed victim={victim['name']} holds={'+'.join(holds)}", skipped)
    # (3)-(4) stop request, SIGTERM, wait.
    outcome, waited = stop_victim(victim, launcher, p0, max(0, args.wait), now, killer, alive, sleep)
    if outcome == 'signal_failed':
        withdraw_obligation(oid, 'preempt withdrawn: SIGTERM could not be delivered, the lane was not stopped')
    line = f'{iso(now)} PREEMPT {desc} outcome={outcome} waited={waited}s obligation={oid}'
    log_line(line)
    print(line.split(' ', 1)[1])
    return 0 if outcome == 'exited' else 4


if __name__ == '__main__':
    sys.exit(main())
