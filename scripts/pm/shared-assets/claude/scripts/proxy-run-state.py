#!/usr/bin/env python3
"""Private proxy run registry and pm-ops lifecycle; never contacts MoP."""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import sys
import time
import uuid
import fcntl

ROOT = Path(os.environ.get('PROXY_AGENT_RUN_STATE_DIR', str(Path.home() / '.local/state/proxy-agent-runs')))
CLI = os.environ.get('PROXY_AGENT_PM_OPS_CLI', str(Path.home() / '.claude/scripts/pm-ops.py'))
# Lane lease + launch state machine (Rajiv D0AMF0XE6TS thread 1795436469.682079, ts 1795438159.967189).
# One proxy_run row per lane name carries evidence.state: queued -> running -> done | refused | failed.
# The launcher writes every transition; PM never hand-writes "launched" rows. A lease is granted at launch and renewed ONLY by
# objective progress (pushed commit / lane PR comment / verified receipt), which proxy-lease-check.py reads from GitHub, never from logs.
LEASE_TTL_MIN = 60
QUEUE_SUPPRESS_MIN = 15
# A relaunch must reproduce the launch; per-run identity, credentials and test-only plumbing never travel.
ENV_KEEP = re.compile(r'^(?:PROXY_|[A-Z]+_PROXY_CAP$|LUNA_)')
ENV_DROP = re.compile(r'(?i)token|secret|password|api.?key|credential|^PROXY_AGENT_(?:RUN_NAME|NAME|DRY_RUN|PM_OPS_CLI|RUN_STATE_DIR)$|^PROXY_RESUME_ORIGINAL$|^PROXY_AGENT_STUB_')
LANE_PS = re.compile(r'(?:^|\s)\S*/claude\s+-p\s+--name\s+([A-Za-z0-9._-]+)\s+--model\s+(\S+)')

def name_ok(name):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', name) or name in ('.', '..'):
        raise ValueError('invalid_name')

def slot(name):
    name_ok(name)
    return ROOT / name

def atomic(path, text):
    temp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    with temp.open('x', encoding='utf-8') as f:
        os.chmod(temp, 0o600)
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)

class state_lock:
    """Serialises the manifest + ledger writes of finish and the running confirmation so a late confirmation can never undo a finish."""
    def __init__(self, base):
        self.path = base / '.state.lock'
    def __enter__(self):
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)

def require_lock(base, token):
    if (base / 'active' / 'token').read_text() != token:
        raise ValueError('lock_not_owned')

def unlock(base, token):
    require_lock(base, token)
    (base / 'history').mkdir(mode=0o700, exist_ok=True)
    os.rename(base / 'active', base / 'history' / ('lock-' + token))

def ledger(args):
    try:
        p = subprocess.run([sys.executable, CLI, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        if p.returncode:
            # The CLI's own refusal text names the cause (23 Nov: "obligation-resolve matched no open row ... exact stored-key match"); keep one line of it.
            detail = ' '.join(p.stderr.decode('utf-8', 'replace').split())[:200]
            print('PROXY_AGENT_WARN reason=pm_ops_failed verb=' + (args[0] if args else '?') + (' detail=' + detail if detail else ''), file=sys.stderr)
            return False
        return True
    except (OSError, subprocess.TimeoutExpired):
        print('PROXY_AGENT_WARN reason=pm_ops_unavailable', file=sys.stderr)
        return False

def quota_text(text):
    for line in text.splitlines()[-100:]:
        if re.search(r'\b(?:402|429)\b|auth_unavailable|\bauth(?:entication)?[ _-]+unavailable\b', line, re.I):
            return True
        shortage = re.search(r'(?:quota|balance|credits?).{0,40}(?:exceed|exhaust|deplet|empty|run(?:s|ning)? out|insufficient|unavailable|error)|(?:insufficient|low|exhausted|depleted|no)\s+(?:quota|balance|credits?)', line, re.I)
        if shortage and (re.search(r'\b(?:error|fail(?:ed|ure)?|denied|provider|api|blocked)\b', line, re.I)
                         or re.match(r'^\s*(?:quota|balance|credits?|insufficient|low|exhausted|depleted)\b', line, re.I)):
            return True
    return False

def worktree_identity(cwd):
    gitdir = Path(subprocess.check_output(['git', '-C', str(cwd), 'rev-parse', '--absolute-git-dir'], stderr=subprocess.DEVNULL, text=True, timeout=10).strip()).resolve(strict=True)
    st = gitdir.stat()
    return [str(gitdir), st.st_dev, st.st_ino]

def brief_refs(text, name=''):
    # Issue/PR the proxy works on: ONLY the brief's explicit ISSUE:/PR: header (case-insensitive); otherwise none (renders '?').
    # Rajiv C0ALZJHGE49 ts 1794538260.553889: PM tracks proxies directly in pm-ops, so the row carries issue/PR.
    refs = {}
    for key in ('issue', 'pr'):
        m = re.search(r'^\s*' + key + r'\s*:\s*#?(\d{3,6})\b', text, re.I | re.M)
        if m: refs[key] = m.group(1)
    # No name-based fallback (CTO review-mop-live-proxies 9b9a1b31): explicit ISSUE:/PR: header only, else unknown.
    return refs

def int_or(value, default):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default

def iso_or(*candidates):
    # First candidate that parses as an ISO timestamp; the last candidate is the always-valid fallback.
    for value in candidates[:-1]:
        try:
            datetime.datetime.fromisoformat(str(value))
            return str(value)
        except (TypeError, ValueError):
            continue
    return candidates[-1]

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def in_minutes(minutes):
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=minutes)).isoformat()

def read_json(path):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}

def model_args(argv):
    out = []
    for i, tok in enumerate(argv):
        if tok in ('--model', '--rung') and i + 1 < len(argv):
            out += [tok, argv[i + 1]]
    return out

def launch_spec(name, typ, brief, argv):
    """Everything proxy-agent.sh needs to re-run this launch: brief path, type, env overrides, model args, exact command."""
    env = {k: v for k, v in sorted(os.environ.items()) if ENV_KEEP.match(k) and not ENV_DROP.search(k) and len(v) <= 300}
    launcher = os.environ.get('PROXY_AGENT_LAUNCHER') or str(Path.home() / '.claude/scripts/proxy-agent.sh')
    words = [k + '=' + shlex.quote(v) for k, v in env.items()] + ['PROXY_AGENT_NAME=' + shlex.quote(name), shlex.quote(launcher)]
    words += [shlex.quote(x) for x in argv]
    return {'name': name, 'type': typ, 'brief': brief, 'argv': list(argv), 'env': env, 'model_args': model_args(argv),
            'cwd': os.getcwd(), 'cmd': 'env ' + ' '.join(words)}

def lane_pid(name):
    """Pid of the live `claude -p --name <name>` child (the same process shape the cap counters match), else None."""
    try:
        # encoding/errors pinned: a lane command line carries brief text (non-ASCII, or cut mid-character by ps), and a decode error here
        # surfaced as PROXY_AGENT_STATE_UNAVAILABLE reason=UnicodeDecodeError so the lane never reached `running`.
        out = subprocess.run(['/bin/ps', '-axww', '-o', 'pid=,command='], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             encoding='utf-8', errors='replace', timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        m = LANE_PS.search(line)
        if m and m.group(1) == name:
            pid = re.match(r'\s*(\d+)\s', line)
            return int(pid.group(1)) if pid else None
    return None

def ref_args(data):
    """--issue/--pr exactly as the proxy_run row was stored: pm-ops resolves by an EXACT key match, so a resolve without them cannot find a row that carries
    them (23 Nov: every refusal of an ISSUE:/PR: brief left its proxy_run row open and warned pm_ops_failed)."""
    extra = []
    if data.get('issue'): extra += ['--issue', str(data['issue'])]
    if data.get('pr'): extra += ['--pr', str(data['pr'])]
    return extra

def relaunch_row(name, spec, data, why, cmd='', low=''):
    """Persist the pm-ops proxy_relaunch CAPACITY_WAIT row for a refused launch. True only when the ledger accepted it."""
    extra = []
    for key in ('issue', 'pr'):
        value = str(data.get(key) or spec.get(key) or '')
        if value.isdigit(): extra += ['--' + key, value]
    cmd = cmd or spec.get('cmd') or ''
    relaunch = {'state': 'CAPACITY_WAIT', 'name': name, 'type': spec.get('type') or data.get('type') or '', 'brief': spec.get('brief') or data.get('brief') or '',
                'argv': spec.get('argv') or [], 'env': spec.get('env') or {}, 'model_args': spec.get('model_args') or [], 'cwd': spec.get('cwd') or '', 'cmd': cmd}
    if low: relaunch['reason'] = 'disk_low'
    evidence = {'relaunch': relaunch, 'refused_at': data.get('finished_at') or now_iso(), 'refused_rc': data.get('exit_rc') or data.get('rc') or '',
                'reason': why, 'model': data.get('model') or ''}
    if low: evidence['disk_low'] = True
    return ledger(['obligation-upsert', '--kind', 'proxy_relaunch', '--severity', 'high', '--target-type', 'proxy', '--target-id', name,
                   '--owner', 'pm', '--horizon', 'hourly', '--title', 'Relaunch refused proxy ' + name,
                   '--action', 'Relaunch ' + name + ' (' + relaunch['type'] + ') when capacity frees: ' + cmd,
                   '--blocker', 'CAPACITY_WAIT: ' + why, *extra, '--evidence-json', json.dumps(evidence)])

def upsert(name, data, suppress_until, replace=False):
    extra = ref_args(data)
    if replace: extra.append('--replace-evidence')
    return ledger(['obligation-upsert', '--kind', 'proxy_run', '--target-type', 'proxy', '--target-id', name,
                   '--owner', name, '--horizon', 'heartbeat', '--title', 'Proxy run ' + name,
                   '--action', 'Monitor proxy run and act on its result', '--suppress-until', suppress_until,
                   *extra, '--evidence-json', json.dumps(data)])

def capacity_wait(spec, pools, base, ledger_cli, slack_cli):
    """Persist custody before a bounded, de-duplicated override request."""
    name, typ = spec['name'], spec['type']
    refs = [(key, spec[key]) for key in ('issue', 'pr') if spec.get(key)]
    kind, target = next(((key, ref) for key, ref in refs if key == 'pr'), refs[0] if refs else ('proxy', name))
    target_type = 'github_' + kind if refs else 'proxy'
    target_label = ', '.join(f'{key.upper()} #{ref}' for key, ref in refs) or 'issue/PR not specified'
    action = 'cd ' + shlex.quote(spec['cwd']) + ' && ' + spec['cmd']
    evidence = {'lane': name, 'type': typ, 'pools_tried': pools, 'state': 'QUEUED_AWAITING_OVERRIDE'}
    args = ['obligation-upsert', '--kind', 'agent_launch_queued', '--horizon', 'hourly',
            '--target-type', target_type, '--target-id', target, *ref_args(spec), '--owner', 'pm', '--dedupe-group', name,
            '--title', f'Launch queued: {name} ({typ})', '--action', action,
            '--blocker', 'All permitted proxy routes unavailable; no eligible PM subagent slot',
            '--next-review-at', in_minutes(15), '--evidence-json', json.dumps(evidence), '--print-id']
    result = subprocess.run([sys.executable, str(ledger_cli), *args], capture_output=True, text=True, timeout=20)
    obligation = result.stdout.strip()
    if result.returncode or not re.fullmatch(r'[0-9]+', obligation):
        raise ValueError('agent_launch_queued_upsert_failed')
    message = (f'Proxy launch queued: {name} ({typ}), {target_label}.\n'
               f'Proxy pools tried (live/cap): {pools or "none"}. No eligible PM subagent slot.\n'
               f'To approve a relaunch, reply rajiv-override {name}. PM relaunches with '
               'PROXY_AGENT_CAP_OVERRIDE=rajiv:<timestamp of your reply>. Existing hard pool limits still apply.')
    dm = 'unconfirmed'
    try:
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        with state_lock(base):
            atomic(base / 'queued-obligation-id', obligation)
            stamp = base / 'override-request-at'
            if stamp.exists() and time.time() - float(stamp.read_text()) < 1800:
                dm = 'deduped'
            else:
                # Claim before sending: Slack can accept the request even if its response is lost.
                atomic(stamp, str(time.time()))
                with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as brief:
                    brief.write(message)
                    brief.seek(0)
                    result = subprocess.run(['/bin/bash', str(slack_cli), '-c', 'D0AMF0XE6TS', '-f'],
                                            stdin=brief, capture_output=True, text=True, timeout=25)
                dm = 'sent' if result.returncode == 0 and re.search(r'^OK ts=[0-9]+\.[0-9]+ channel=D0AMF0XE6TS$', result.stdout, re.M) else 'unconfirmed'
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass  # The durable queue is already owned; never claim a delivered DM without a receipt.
    print(f'QUEUED_AWAITING_OVERRIDE lane={name} obligation={obligation} dm={dm}')
    return obligation


def spec_from_brief(a):
    raw_brief = Path(a.brief)
    try:
        text = raw_brief.read_text(errors='replace')
        brief_path = str(raw_brief.resolve())
    except OSError:
        text, brief_path = '', a.brief
    spec = launch_spec(a.name, a.type, brief_path, a.orig_arg)
    spec.update(brief_refs(text, a.name))
    return spec


def main():
    p = argparse.ArgumentParser()
    p.add_argument('cmd', choices=['acquire', 'resume', 'queue', 'start', 'running', 'finish', 'abandon', 'disable-ernie', 'quota', 'capacity-wait'])
    p.add_argument('--name', default='')
    p.add_argument('--pools', default='')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--token', default='')
    p.add_argument('--type', default='')
    p.add_argument('--brief', default='')
    p.add_argument('--cwd', default='')
    p.add_argument('--out', default='')
    p.add_argument('--log', default='')
    p.add_argument('--jsonl', default='')
    p.add_argument('--model', default='')
    p.add_argument('--rc', default='')
    p.add_argument('--reason', default='')
    p.add_argument('--result', default='RESULT UNSTRUCTURED reason=no_final_result_line')
    # Structured exit result (proxy-agent-notify.py result): validated block JSON, STRUCTURED|UNSTRUCTURED, 40-char head.
    p.add_argument('--result-json', default='')
    p.add_argument('--result-status', default='')
    p.add_argument('--head', default='')
    # queue: the launcher's original argv (one --orig-arg=VALUE per word) so a refusal can be re-queued exactly; running: poll window.
    p.add_argument('--orig-arg', action='append', default=[])
    # abandon: the launcher refused this launch because free space on / was below the start threshold (reason=disk_low).
    p.add_argument('--disk-low', default='0')
    p.add_argument('--in-process', default='0')
    p.add_argument('--wait-s', default='0')
    p.add_argument('--pm-role', default='0')
    # Escalation lineage (Rajiv D0AMF0XE6TS ts 1794641763.847759): role + rung + inherited lane clock per run.
    p.add_argument('--role', default='')
    p.add_argument('--rung', default='')
    p.add_argument('--lane-started-at', default='')
    p.add_argument('--predecessor', default='')
    p.add_argument('--predecessor-out', default='')
    p.add_argument('--inherited-head', default='')
    p.add_argument('--escalations', default='')
    a = p.parse_args()
    if a.cmd == 'quota':
        last_event = None
        try:
            with open(a.jsonl, encoding='utf-8', errors='replace') as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict) and event.get('type') in ('result', 'error'):
                        last_event = event
        except (OSError, ValueError):
            pass
        if last_event is not None:
            text = str(last_event.get('result') or '')
            if last_event.get('type') == 'error' or last_event.get('is_error') or str(last_event.get('subtype', '')).startswith('error'):
                text += '\n' + json.dumps(last_event.get('errors') or last_event.get('error') or '')
            if quota_text(text[-65536:]):
                return
            # A successful structured final result supersedes earlier log errors.
            if last_event.get('type') == 'result' and not last_event.get('is_error') and not str(last_event.get('subtype', '')).startswith('error'):
                raise SystemExit(1)
        try:
            text = Path(a.out).read_text(errors='replace')[-65536:]
        except (OSError, ValueError):
            text = ''
        if quota_text(text):
            return
        if any(re.match(r'^\s*\**\s*(?:DONE|BLOCKED|VERDICT|RESULT)\s*:', line, re.I) for line in text.splitlines()[-30:]):
            raise SystemExit(1)
        try:
            if quota_text(Path(a.log).read_text(errors='replace')[-65536:]):
                return
        except (OSError, ValueError):
            pass
        raise SystemExit(1)
    if a.cmd == 'disable-ernie':
        path = Path(os.environ.get('PROXY_AGENT_STATE_DIR', str(Path.home() / '.cache/proxy-agent'))) / 'quota.json'
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict): raise ValueError('invalid_quota_cache')
        except FileNotFoundError:
            data = {}
        data.setdefault('unusable_models', {})['ernie-flash'] = 'image_input_unsupported'
        atomic(path, json.dumps(data) + '\n')
        return
    base = slot(a.name)
    if a.cmd == 'capacity-wait':
        if a.dry_run:
            print(f'QUEUED_AWAITING_OVERRIDE lane={a.name} obligation=preview dm=mocked pools={a.pools}')
            return
        require_lock(base, a.token)
        spec = read_json(base / 'active' / 'launch.json') or spec_from_brief(a)
        spec['type'] = a.type  # Report the resolved type while preserving the caller's exact argv.
        capacity_wait(spec, ' '.join(dict.fromkeys(a.pools.split())), base, CLI, Path.home() / '.claude/scripts/slack-send.sh')
        return
    if a.cmd == 'acquire':
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        (base / 'active').mkdir(mode=0o700)
        atomic(base / 'active' / 'token', a.token)
        return
    if a.cmd == 'running':
        # Confirm the child claude process, then record running. Runs detached from the runner, bounded by --wait-s; never blocks a lane.
        try:
            deadline = time.time() + max(0.0, float(a.wait_s or 0))
        except ValueError:
            deadline = time.time()
        pid = lane_pid(a.name)
        while pid is None and time.time() < deadline:
            time.sleep(0.2)
            pid = lane_pid(a.name)
        if pid is None:
            print('PROXY_AGENT_WARN reason=child_not_confirmed', file=sys.stderr)
            raise SystemExit(3)
        with state_lock(base):
            try:
                owned = (base / 'active' / 'token').read_text() == a.token
            except OSError:
                owned = False  # already finished and unlocked: nothing to confirm
            data = read_json(base / 'manifest.json')
            if not owned or data.get('token') != a.token or data.get('finished_at'):
                return
            data.update(state='running', running_at=now_iso(), pid=pid)
            atomic(base / 'manifest.json', json.dumps(data) + '\n')
            upsert(a.name, data, data.get('suppress_until') or in_minutes(LEASE_TTL_MIN))
            try:
                queued_id = (base / 'queued-obligation-id').read_text().strip()
            except OSError:
                queued_id = ''
            if queued_id.isdigit() and ledger(['obligation-resolve', '--kind', 'agent_launch_queued', '--id', queued_id,
                                               '--reason', f'Launcher confirmed running proxy {a.name} (pid={pid})']):
                atomic(base / 'queued-obligation-id', '')
        return
    require_lock(base, a.token)
    if a.cmd == 'queue':
        # First ledger write of a launch, before model selection: a refusal (rc 75) therefore always has a row to close and a spec to re-queue.
        spec = spec_from_brief(a)
        spec['queued_at'] = now_iso()
        atomic(base / 'active' / 'launch.json', json.dumps(spec) + '\n')
        data = {'name': a.name, 'type': a.type, 'role': a.type, 'brief': brief_path, 'state': 'queued', 'queued_at': spec['queued_at'], 'token': a.token}
        data.update(brief_refs(text, a.name))
        upsert(a.name, data, in_minutes(QUEUE_SUPPRESS_MIN), replace=True)
        return
    if a.cmd == 'abandon':
        # Launcher exit before the child started. rc 75 = refusal (pool cap, burn gate, no usable model): queue a CAPACITY_WAIT relaunch row.
        data = read_json(base / 'manifest.json')
        spec = read_json(base / 'active' / 'launch.json')
        owned = data.get('token') == a.token
        if owned or spec:
            now = now_iso()
            queued = a.rc == '74'
            in_process = a.rc == '75' and a.in_process == '1'
            refused = a.rc in ('74', '75')
            if not owned:
                data = {'name': a.name, 'type': spec.get('type', ''), 'role': spec.get('type', ''), 'brief': spec.get('brief', ''),
                        'token': a.token, 'queued_at': spec.get('queued_at', now)}
                for key in ('issue', 'pr'):
                    if spec.get(key): data[key] = spec[key]
            data.update(rc=a.rc if refused else 'UNAVAILABLE', reason='queued_awaiting_override' if queued else 'in_process_available' if in_process else 'refused' if refused else 'launch_failed',
                        state='refused' if refused else 'failed', exit_rc=a.rc, finished_at=now)
            if owned:
                atomic(base / 'manifest.json', json.dumps(data) + '\n')
            upsert(a.name, data, now)
            # --disk-low names the refusal so the relaunch row says what actually blocked the launch (Rajiv C0ALZJHGE49 thread
            # 1795448835.183169); the state and kind stay CAPACITY_WAIT / proxy_relaunch so every existing reader is unchanged.
            low = 'disk_low' if a.disk_low == '1' else ''
            why = ('launch refused rc=75: disk_low (free space on / below the start threshold; nothing deleted; '
                   'free space, then relaunch - see launcher stderr)') if low else \
                  'launch refused rc=75 (pool cap, burn gate or no usable model; see launcher stderr)'
            if queued or in_process:
                ledger(['obligation-resolve', '--kind', 'proxy_run', '--target-type', 'proxy', '--target-id', a.name, *ref_args(data),
                        '--reason', 'Queued as agent_launch_queued; awaiting override' if queued else 'IN_PROCESS_AVAILABLE; PM owns next launch',
                        '--external-state', json.dumps({'state': 'refused', 'rc': a.rc, 'reason': data['reason']})])
            elif refused and (relaunch_row(a.name, spec, data, why, low=low) or relaunch_row(a.name, spec, data, why, low=low)):  # one retry: the row is the only record of the refusal
                ledger(['obligation-resolve', '--kind', 'proxy_run', '--target-type', 'proxy', '--target-id', a.name, *ref_args(data),
                        '--reason', 'refused rc=75: relaunch queued as proxy_relaunch CAPACITY_WAIT' + (' (disk_low)' if low else ''),
                        '--external-state', json.dumps({'state': 'refused', 'rc': '75', **({'reason': 'disk_low'} if low else {})})])
        unlock(base, a.token)
        return
    if a.cmd == 'resume':
        data = json.loads((base / 'manifest.json').read_text())
        cwd = Path(data['cwd'])
        st = cwd.stat()
        if not cwd.is_dir() or str(cwd.resolve()) != data['cwd'] or [st.st_dev, st.st_ino] != data['cwd_identity'] or worktree_identity(cwd) != data['git_identity']:
            raise ValueError('original_worktree_changed')
        original = Path(data['original_brief']).read_text()
        progress = data['out']
        brief = base / 'active' / 'resume-brief.txt'
        atomic(brief, 'Continue from the existing worktree state. Read the progress file first: ' + progress + '\nIf the progress file is unavailable, inspect the worktree and report that fact.\n\n--- Original brief ---\n' + original)
        for key, value in [('TYPE', data['type']), ('CWD', data['cwd']), ('PROMPT_FILE', str(brief)), ('PROXY_RESUME_ORIGINAL', data['original_brief']), ('PM_ROLE', data['pm_role'])]:
            print(key + '=' + shlex.quote(value))
        return
    if a.cmd == 'start':
        cwd = Path(a.cwd).resolve(strict=True)
        st = cwd.stat()
        if not cwd.is_dir(): raise ValueError('worktree_missing')
        previous = {}
        if os.environ.get('PROXY_RESUME_ORIGINAL'):
            previous = json.loads((base / 'manifest.json').read_text())
            if (str(cwd) != previous['cwd'] or [st.st_dev, st.st_ino] != previous['cwd_identity']
                    or worktree_identity(cwd) != previous['git_identity']):
                raise ValueError('original_worktree_changed')
        brief = Path(os.environ.get('PROXY_RESUME_ORIGINAL') or a.brief).resolve(strict=True)
        if not brief.is_file(): raise ValueError('brief_missing')
        original = base / 'active' / 'original-brief.txt'
        brief_text = brief.read_text()
        atomic(original, brief_text)
        expected = re.search(r'^\s*EXPECTED_MIN\s*:\s*(\d+)\s*$', brief_text, re.I | re.M)
        minutes = int(expected.group(1)) if expected else 60
        if not 1 <= minutes <= 10080:
            minutes = 60
        # The brief moves with the lock into history; point to its stable eventual path.
        saved = base / 'history' / ('lock-' + a.token) / 'original-brief.txt'
        data = {'name': a.name, 'type': a.type, 'pm_role': a.pm_role, 'brief': a.brief, 'original_brief': str(saved),
                'cwd': str(cwd), 'cwd_identity': [st.st_dev, st.st_ino], 'git_identity': worktree_identity(cwd), 'out': a.out,
                'model': a.model, 'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'token': a.token}
        deadline = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=minutes)).isoformat()
        data['expected_minutes'] = minutes
        # role/rung/lane_started_at/escalations are read by proxy-escalate.py at stop time.
        # A resume keeps the previous run's values when the launcher passes none (lane clock never resets).
        data['role'] = a.role or previous.get('role') or a.type
        data['rung'] = a.rung if a.rung in ('1', '2', '3') else str(previous.get('rung') or '')
        data['lane_started_at'] = iso_or(a.lane_started_at, previous.get('lane_started_at'), data['started_at'])
        data['predecessor'] = a.predecessor or previous.get('predecessor') or ''
        data['predecessor_out'] = a.predecessor_out or previous.get('predecessor_out') or ''
        data['inherited_head'] = a.inherited_head or previous.get('inherited_head') or ''
        data['escalations'] = int_or(a.escalations, int_or(previous.get('escalations'), 0))
        data.update(brief_refs(brief_text, a.name))
        data['suppress_until'] = deadline
        # Lane state machine + lease: queued until the child process is confirmed (running); lease renewed only by objective progress.
        data.update(state='queued', lease_ttl_minutes=LEASE_TTL_MIN, lease_started_at=data['started_at'],
                    lease_renewed_at=data['started_at'], lease_basis='launch')
        if not upsert(a.name, data, deadline):
            raise ValueError('pm_ops_upsert_failed')
        atomic(base / 'manifest.json', json.dumps(data) + '\n')
        return
    if a.cmd == 'finish':
        with state_lock(base):
            data = json.loads((base / 'manifest.json').read_text())
            if data.get('token') != a.token: raise ValueError('manifest_not_owned')
            now = now_iso()
            terminal = a.reason in ('done', 'blocked', 'verdict', 'result')
            # rc 75 = a continuation was refused for capacity (run.sh capacity-stop): the lane is resumable, not done.
            queued = a.rc == '74'
            in_process = a.rc == '75' and a.in_process == '1'
            refused = a.rc in ('74', '75') and not terminal
            state = 'refused' if refused else ('done' if terminal else 'failed')
            data.update(rc=a.rc, reason=a.reason, result=a.result, finished_at=now, state=state)
            if a.result_status: data['result_status'] = a.result_status
            if a.head: data['head'] = a.head
            if a.result_json:
                try:
                    block = json.loads(a.result_json)
                except ValueError:
                    block = None
                if isinstance(block, dict): data['result_block'] = block
            atomic(base / 'manifest.json', json.dumps(data) + '\n')
            updated = upsert(a.name, data, data['suppress_until'] if terminal else now)
            if updated and terminal:
                ledger(['obligation-resolve', '--kind', 'proxy_run', '--target-type', 'proxy', '--target-id', a.name, *ref_args(data),
                        '--reason', a.result, '--external-state', json.dumps({'rc': a.rc, 'reason': a.reason, 'out': data['out']})])
            if updated and refused:
                spec = read_json(base / 'active' / 'launch.json')
                cmd = ''
                if data.get('model'):
                    cmd = 'env PROXY_AGENT_NAME=' + shlex.quote(a.name) + ' ' + shlex.quote(os.environ.get('PROXY_AGENT_LAUNCHER') or str(Path.home() / '.claude/scripts/proxy-agent.sh')) \
                        + ' --resume ' + shlex.quote(a.name) + ' --model ' + shlex.quote(str(data['model']))
                if relaunch_row(a.name, spec, data, 'continuation refused rc=75 (pool cap or capacity stop); lane is resumable', cmd):
                    ledger(['obligation-resolve', '--kind', 'proxy_run', '--target-type', 'proxy', '--target-id', a.name, *ref_args(data),
                            '--reason', 'refused rc=75: resume queued as proxy_relaunch CAPACITY_WAIT',
                            '--external-state', json.dumps({'state': 'refused', 'rc': '75', 'out': data['out']})])
        unlock(base, a.token)

if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as e:
        known = {'invalid_name', 'lock_not_owned', 'original_worktree_changed', 'worktree_missing', 'brief_missing', 'manifest_not_owned', 'pm_ops_upsert_failed'}
        reason = str(e) if str(e) in known else type(e).__name__
        print('PROXY_AGENT_STATE_UNAVAILABLE reason=' + reason, file=sys.stderr)
        sys.exit(2)