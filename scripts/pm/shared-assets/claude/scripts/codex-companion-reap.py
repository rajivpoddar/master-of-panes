#!/usr/bin/env python3
"""Ancestry-owned Codex companion cleanup. No age or socket-idleness policy."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import time


def run(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=15)


def companion(command):
    try:
        args = shlex.split(command)
    except ValueError:
        return False
    # Only executable/script positions, never a review prompt mentioning a script.
    for arg in args[:3]:
        if Path(arg).name in ('codex-companion.mjs', 'codex-review-companion.mjs', 'app-server-broker.mjs'):
            return True
    return bool(len(args) == 2 and Path(args[0]).name == 'codex' and args[1:] == ['app-server']) or bool(
        len(args) == 3 and Path(args[0]).name == 'node' and Path(args[1]).name == 'codex' and args[2] == 'app-server')


def ancestry(pid, table, panes):
    seen = set()
    chain = []
    while pid > 1:
        if pid in seen or pid not in table:
            return 'SKIPPED_UNREADABLE', chain
        seen.add(pid)
        chain.append(pid)
        if pid in panes:
            return 'live_pane', chain
        pid = table[pid]['ppid']
    return 'orphan', chain


def select(table, panes, uid, pr_states, protected=()):
    """Pure policy; descendants must independently have readable ancestry."""
    roots = {p for p, r in table.items() if r['uid'] == uid and companion(r['command'])}
    members = set(roots)
    changed = True
    while changed:
        added = {p for p, r in table.items() if r['uid'] == uid and r['ppid'] in members and p not in members}
        changed = bool(added)
        members.update(added)
    result = []
    for pid in sorted(members):
        row = table[pid]
        state, chain = ancestry(pid, table, panes)
        linked = next((pr_states[p] for p in chain if p in pr_states and pr_states[p].get('state') in ('OPEN', 'CLOSED', 'MERGED')), {})
        desktop = any(re.search(r'/Applications/(?:ChatGPT|Codex)\.app/Contents/MacOS/|Codex Framework', table[p]['command']) for p in chain)
        if pid in protected or pid in panes or desktop:
            reason, action = 'protected_session', 'keep'
        elif state == 'SKIPPED_UNREADABLE':
            reason, action = state, 'skip'
        elif linked.get('state') in ('CLOSED', 'MERGED'):
            reason, action = 'closed_pr', 'reap'
        elif state == 'live_pane':
            reason, action = 'live_pane', 'keep'
        else:
            reason, action = 'orphan', 'reap'
        result.append({**row, 'action': action, 'reason': reason, 'linkage': linked, 'ancestry': chain})
    return result


def live_panes(helper):
    slots, error = helper.load_slot_inventory()
    if error:
        raise RuntimeError('slot_inventory:' + error)
    addresses = {s['address'] for s in slots.values()} | {'0:0.0'}
    result = run(['tmux', 'list-panes', '-a', '-F', '#{pane_pid}\t#{session_name}:#{window_index}.#{pane_index}\t#{session_name}\t#{window_name}\t#{pane_dead}'])
    if result.returncode:
        raise RuntimeError('tmux_unreadable')
    panes = {}
    for line in result.stdout.splitlines():
        pid, address, session, window, dead = line.split('\t')
        if dead == '0' and (address in addresses or session == 'proxies' or window.startswith('px-')):
            panes[int(pid)] = address
    return panes


def linkage(row, helper, cache):
    cwd = helper.get_cwd(row['pid'])
    row['cwd'] = cwd
    args = shlex.split(row['command'])
    explicit = None
    for i, arg in enumerate(args):
        if arg in ('--pr', '--pr-number') and i + 1 < len(args) and args[i + 1].isdigit():
            explicit = args[i + 1]
        match = re.fullmatch(r'https://github.com/heydonna-app/heydonna-app/pull/(\d+)', arg)
        if match:
            explicit = match[1]
    branch = next((args[i + 1] for i, arg in enumerate(args[:-1]) if arg == '--branch'), None)
    if cwd and branch is None:
        proc = run(['git', '-C', cwd, 'symbolic-ref', '--quiet', '--short', 'HEAD'])
        branch = proc.stdout.strip() if proc.returncode == 0 else None
    target = explicit or branch
    if not target:
        return {'state': 'UNKNOWN'}
    key = target
    if key not in cache:
        proc = run(['gh', 'pr', 'view', target, '--repo', 'heydonna-app/heydonna-app', '--json', 'number,state,url'])
        cache[key] = json.loads(proc.stdout) if proc.returncode == 0 else {'state': 'UNKNOWN', 'target': target}
    return {**cache[key], 'branch': branch}


def check(helper, apply=False, grace=2):
    failure = {'class': 'codex_broker_cap', 'action': 'SKIPPED_UNREADABLE', 'roots': 0, 'rules': {},
               'before': 0, 'after': None, 'found': [], 'terminated': [], 'refused': [], 'killed': 0}
    table = helper.codex_broker_table()
    if table is None:
        return {**failure, 'errors': ['ps_unreadable']}
    try:
        panes = live_panes(helper)
    except Exception as exc:
        return {**failure, 'errors': [str(exc)]}
    # Protect this executor and all its ancestors, not all PM descendants.
    protected = set()
    cur = os.getpid()
    while cur > 1 and cur in table and cur not in protected:
        protected.add(cur)
        cur = table[cur]['ppid']
    cache, states = {}, {}
    for pid, row in table.items():
        if row['uid'] == os.getuid() and companion(row['command']):
            try:
                states[pid] = linkage(row, helper, cache)
            except Exception:
                states[pid] = {'state': 'UNKNOWN'}
    rows = select(table, panes, os.getuid(), states, protected)
    for row in rows:
        row['cwd'] = row.get('cwd') or helper.get_cwd(row['pid'])
    attempts, refused = [], []
    for sig in (signal.SIGTERM, signal.SIGKILL) if apply else ():
        fresh = helper.codex_broker_table()
        try:
            current_panes = live_panes(helper)
        except Exception:
            refused.append({'reason': 'SKIPPED_UNREADABLE'})
            break
        if fresh is None:
            refused.append({'reason': 'SKIPPED_UNREADABLE'})
            break
        eligible = {r['pid']: r for r in select(fresh, current_panes, os.getuid(), states, protected)}
        for row in reversed(rows):
            pid = row['pid']
            if row['action'] != 'reap' or pid not in fresh:
                continue
            current = fresh[pid]
            if any(current[k] != row[k] for k in ('start_time', 'command', 'uid', 'ppid')) or eligible.get(pid, {}).get('action') != 'reap':
                refused.append({'pid': pid, 'reason': 'identity_or_ownership_changed'})
                continue
            try:
                os.kill(pid, sig)
                attempts.append({'pid': pid, 'signal': sig.name})
            except ProcessLookupError:
                pass
            except PermissionError:
                refused.append({'pid': pid, 'reason': 'permission_denied'})
        if sig == signal.SIGTERM:
            time.sleep(max(0, grace))
    after = helper.codex_broker_table() if apply else table
    terminated = [r for r in rows if r['action'] == 'reap' and after is not None and (r['pid'] not in after or after[r['pid']]['stat'].startswith('Z'))]
    for row in rows:
        row['command'] = helper.sanitize_command(row['command'])
        row.setdefault('cwd', helper.get_cwd(row['pid']) if row['pid'] in table else None)
    return {'class': 'codex_broker_cap', 'action': 'apply' if apply else 'scan', 'roots': len(states),
            'rules': {reason: sum(r['reason'] == reason for r in rows) for reason in {r['reason'] for r in rows}}, 'before': len(rows),
            'kept': sum(r['action'] == 'keep' for r in rows), 'skipped': [r for r in rows if r['action'] == 'skip'],
            'found': rows, 'terminated': terminated, 'killed': len(terminated), 'reaped': len(terminated),
            'after': None if after is None else len(rows) - len(terminated), 'attempts': attempts, 'refused': refused, 'errors': []}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--output', default='/tmp/codex-companion-reap-latest.json')
    opts = parser.parse_args()
    spec = importlib.util.spec_from_file_location('cleanup', Path(__file__).with_name('stale-process-cleanup.py'))
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    receipt = check(helper, opts.apply)
    Path(opts.output).write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps({k: v for k, v in receipt.items() if k not in ('found', 'terminated', 'attempts', 'skipped')}, indent=2))
