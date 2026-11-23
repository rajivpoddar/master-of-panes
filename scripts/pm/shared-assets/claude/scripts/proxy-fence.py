#!/usr/bin/env python3
"""Per-lane stop fence for P0 preemption (used by proxy-preempt.py; gated by the box scripts, the lease CLI and the lanes' Bash hook).

CTO ruling (C0ALZJHGE49 thread 1795441441.525659, ts 1795441848.817099): "keep the P0 queued when the pool is full until the stop path fences new
lease and external-effect acquisition, then rechecks custody under that fence before signaling."

A fence is ONE file per lane RUN, keyed by lane name + run id (the registry row's `token`, exported to the lane as PROXY_AGENT_RUN_TOKEN):
  <PROXY_FENCE_DIR>/<name>.<token>.json   {name, token, by, p0, at, expires_at}      default dir ~/.claude/state/proxy-fences
It is created exclusively (os.link of a finished temp file: a reader never sees a half-written fence), expires by itself after a TTL (a preemptor
that dies cannot wedge a lane), and is removed by the preemptor on every path (only by the preemptor that set it). A relaunched lane has a new
run id, so an old fence can never touch it.

What a fence stops: only NEW starts. Nothing already running is touched and release/down/status of an existing lease are never gated.
  check --effect lease|box   remote-test-box.sh up, remote-browser-box.sh up|once, rtb-pool-lease.py fresh acquire2 + borrow  -> rc 75 when fenced
  hook                        the lanes' PreToolUse Bash hook: git push, gh pr create|merge|edit|ready, pm-admit-ci.sh        -> rc 2 (block) when fenced
Lane identity is PROXY_AGENT_NAME + PROXY_AGENT_RUN_TOKEN from the environment; when the environment lacks them the caller's ancestry is walked to
the `claude -p --name N` process and the token is read from that lane's registry row. A caller that is not a lane is never fenced.
Every read error fails OPEN (not fenced): a gate must never break a box or a push for a lane nobody is preempting.

Usage: proxy-fence.py set --name N --token T --by LAUNCHER [--ttl S] [--p0 REF] | clear --name N --token T [--by LAUNCHER]
       proxy-fence.py check [--name N --token T] [--effect E] | hook < PreToolUse-json | status
Env (tests): PROXY_FENCE_DIR, PROXY_AGENT_RUN_STATE_DIR, PROXY_FENCE_PS (file with 'pid ppid command' rows instead of ps).
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
FENCE_DIR = Path(os.environ.get('PROXY_FENCE_DIR', str(HOME / '.claude/state/proxy-fences')))
RUNS = Path(os.environ.get('PROXY_AGENT_RUN_STATE_DIR', str(HOME / '.local/state/proxy-agent-runs')))
TTL_S = 600
FENCED_RC = 75
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z')
TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z')
LANE = re.compile(r'(?:^|\s)\S*/claude\s+-p\s+--name\s+([A-Za-z0-9._-]+)\s+--model\s+(\S+)')
PS_ROW = re.compile(r'\s*(\d+)\s+(\d+)\s+(\S.*)')
# The pushes and PR actions a fenced lane may not START (the same shapes proxy-preempt.py's custody check treats as an effect in flight).
PUSH = re.compile(r'\bgit\s+(?:-C\s+\S+\s+)?push\b|\bgh\s+pr\s+(?:create|merge|edit|ready)\b|pm-admit-ci\.sh', re.I)


def _path(fence_dir, name, token):
    if not NAME.fullmatch(str(name)) or not TOKEN.fullmatch(str(token)):
        raise ValueError('invalid_fence_key')
    return Path(fence_dir) / f'{name}.{token}.json'


def read_fence(fence_dir, name, token, now=None):
    """The live fence of this lane run as a dict, or None (absent, unreadable, malformed or expired)."""
    try:
        data = json.loads(_path(fence_dir, name, token).read_text())
        if not isinstance(data, dict) or float(data['expires_at']) <= (time.time() if now is None else now):
            return None
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None


def set_fence(fence_dir, name, token, by, ttl_s=TTL_S, now=None, p0=''):
    """(ok, detail). Exclusive: a live fence already held (by anyone) -> (False, 'fence_held'). Never raises."""
    try:
        path = _path(fence_dir, name, token)
    except ValueError:
        return False, 'invalid_fence_key'
    now = time.time() if now is None else now
    body = json.dumps({'name': name, 'token': token, 'by': by, 'p0': p0, 'pid': os.getpid(),
                       'at': datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'expires_at': now + ttl_s})
    tmp = Path(fence_dir) / f'.{path.name}.{os.getpid()}.tmp'
    try:
        Path(fence_dir).mkdir(parents=True, exist_ok=True)
        tmp.write_text(body)
    except OSError as exc:
        return False, f'fence_unwritable:{type(exc).__name__}'
    try:
        for _ in range(2):
            try:
                os.link(tmp, path)
                return True, 'set'
            except FileExistsError:
                if read_fence(fence_dir, name, token, now) is not None:
                    return False, 'fence_held'
                try:
                    path.unlink()  # an expired or malformed leftover
                except OSError:
                    pass
            except OSError as exc:
                return False, f'fence_unwritable:{type(exc).__name__}'
        return False, 'fence_held'
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def clear_fence(fence_dir, name, token, by=None):
    """Remove the fence; with by= only a fence that preemptor set (a late clear never removes a successor's fence). True when none is left."""
    try:
        path = _path(fence_dir, name, token)
        if by is not None:
            try:
                if json.loads(path.read_text()).get('by') != by:
                    return False
            except (OSError, ValueError, AttributeError):
                pass
        path.unlink()
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False
    return True


def _ps_rows():
    try:
        fixed = os.environ.get('PROXY_FENCE_PS')
        text = Path(fixed).read_text() if fixed else subprocess.run(['/bin/ps', '-axo', 'pid=,ppid=,command='], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                                                    encoding='utf-8', errors='replace', timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    rows = {}
    for line in text.splitlines():
        m = PS_ROW.match(line)
        if m:
            rows[int(m.group(1))] = (int(m.group(2)), m.group(3))
    return rows


def _manifest_token(name):
    try:
        data = json.loads((RUNS / name / 'manifest.json').read_text())
        return str(data.get('token') or '') if isinstance(data, dict) else ''
    except (OSError, ValueError):
        return ''


def lane_identity(env=None, pid=None):
    """(name, token) of the proxy lane this process belongs to, or (None, None) for a caller that is not a lane."""
    env = os.environ if env is None else env
    name = (env.get('PROXY_AGENT_NAME') or env.get('PROXY_AGENT_RUN_NAME') or '').strip()
    token = (env.get('PROXY_AGENT_RUN_TOKEN') or '').strip()
    if not name:
        rows = _ps_rows()
        cur = os.getpid() if pid is None else pid
        for _ in range(80):  # the stripped-env fallback: walk up to the lane's own claude process
            row = rows.get(cur)
            if row is None:
                break
            m = LANE.search(row[1])
            if m:
                name = m.group(1)
                break
            cur = row[0]
    if name and not token:
        token = _manifest_token(name)
    return (name, token) if name and token else (None, None)


def fenced(env=None, fence_dir=None, name=None, token=None, now=None):
    """The live fence for the caller's lane run (or the given name+token), else None."""
    if not (name and token):
        name, token = lane_identity(env)
    if not (name and token):
        return None
    return read_fence(FENCE_DIR if fence_dir is None else fence_dir, name, token, now)


def _message(fence, what):
    until = datetime.datetime.fromtimestamp(float(fence['expires_at']), datetime.timezone.utc).strftime('%H:%M:%SZ')
    return (f"PREEMPT_FENCED lane={fence.get('name')} run={fence.get('token')} by={fence.get('by')} {what} until<={until}: "
            'this lane is being stopped for a P0, so no new box lease, push or PR action may start. Finish, checkpoint and end the turn; '
            'PM relaunches it from its brief.')


def main(argv=None, env=None, stdin=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('set')
    s.add_argument('--name', required=True)
    s.add_argument('--token', required=True)
    s.add_argument('--by', required=True)
    s.add_argument('--ttl', type=int, default=TTL_S)
    s.add_argument('--p0', default='')
    c = sub.add_parser('clear')
    c.add_argument('--name', required=True)
    c.add_argument('--token', required=True)
    c.add_argument('--by', default=None)
    k = sub.add_parser('check')
    k.add_argument('--name', default='')
    k.add_argument('--token', default='')
    k.add_argument('--effect', default='box')
    sub.add_parser('hook')
    sub.add_parser('status')
    args = ap.parse_args(argv)
    try:
        if args.cmd == 'set':
            ok, detail = set_fence(FENCE_DIR, args.name, args.token, args.by, args.ttl, p0=args.p0)
            print(detail)
            return 0 if ok else 3
        if args.cmd == 'clear':
            return 0 if clear_fence(FENCE_DIR, args.name, args.token, args.by) else 3
        if args.cmd == 'status':
            for p in sorted(FENCE_DIR.glob('*.json')) if FENCE_DIR.is_dir() else []:
                print(p.name, p.read_text().strip())
            return 0
        if args.cmd == 'check':
            fence = fenced(env, name=args.name, token=args.token)
            if fence is None:
                return 0
            print(_message(fence, f'effect={args.effect}'), file=sys.stderr)
            return FENCED_RC
        # hook: PreToolUse Bash. Blocks (rc 2, message on stderr) only a push / PR action by a fenced lane; everything else, and every error, passes.
        data = json.load(sys.stdin if stdin is None else stdin)
        command = (data.get('tool_input') or {}).get('command', '') if isinstance(data, dict) else ''
        if not isinstance(command, str) or not PUSH.search(command):
            return 0
        fence = fenced(env)
        if fence is None:
            return 0
        print(_message(fence, 'effect=push'), file=sys.stderr)
        return 2
    except Exception:  # fail open: the fence is a safety add-on, never a reason to break a lane that is not being preempted
        return 0 if args.cmd in ('check', 'hook') else 2


if __name__ == '__main__':
    sys.exit(main())
