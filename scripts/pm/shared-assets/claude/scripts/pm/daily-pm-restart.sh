#!/usr/bin/env bash
# Daily safe-point restart; state is checkpointed before replacing the PM pane.
set -euo pipefail
exec python3 - "$@" <<'PY'
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import urllib.request
from zoneinfo import ZoneInfo

ap = argparse.ArgumentParser()
ap.add_argument('--dry-run', action='store_true')
args = ap.parse_args()
home = Path.home()
day = dt.datetime.now(ZoneInfo('Asia/Kolkata')).strftime('%Y-%m-%d')
memory = home / '.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/memory'
state_dir = home / '.claude/state'
resume = memory / f'MEMORY-pm-resume-prompt-{day}-0400.md'
state = state_dir / f'daily-pm-restart-{day}.json'
log = Path(f'/tmp/daily-pm-restart-{day}.log')
scripts = home / '.claude/scripts'

def emit(message):
    print(message, flush=True)
    with log.open('a') as output:
        output.write(dt.datetime.now(dt.timezone.utc).isoformat() + ' ' + message + '\n')

def atomic(path, text):
    temporary = path.with_name(path.name + f'.{os.getpid()}.new')
    temporary.write_text(text)
    temporary.replace(path)

def snapshot():
    db = memory.parent / 'state/pm-ops.db'
    connection = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        obligations = [dict(row) for row in connection.execute(
            'SELECT id,kind,owner,title,required_action,blocker,issue,pr,slot FROM obligations WHERE status=? ORDER BY id', ('open',))]
    finally:
        connection.close()
    with urllib.request.urlopen('http://127.0.0.1:3100/slots', timeout=15) as response:
        slots = json.load(response)['slots']
    prs = json.loads(subprocess.run(['gh', 'pr', 'list', '--repo', 'heydonna-app/heydonna-app', '--state', 'open',
        '--limit', '1000', '--json', 'number,title,headRefName,headRefOid,isDraft,url'],
        capture_output=True, text=True, check=True, timeout=60).stdout)
    if len(prs) >= 1000:
        raise RuntimeError('PR snapshot may be truncated; refusing restart')
    return obligations, slots, prs

def perform():
    saved = json.loads(state.read_text()) if state.exists() else {}
    if saved.get('launch_intent'):
        emit('ALREADY_REQUESTED: PM restart was checkpointed for ' + day + '; inspect pane before any manual retry')
        return
    obligations, slots, prs = snapshot()
    owners = sorted({r['owner'] for r in obligations if r.get('owner')})
    prompt = ('Read ' + str(resume) + '. Resume PM from the canonical ledger. Relaunch the listed background agents only after checking their live state; '
              'do not duplicate completed work. Run Skill(daily-pm-restart) post-restart completion: post one line to Rajiv DM D0AMF0XE6TS.')
    launch = [str(scripts / 'launch-pm-cliproxy.sh'), prompt]
    emit(f'{"DRY_RUN" if args.dry_run else "RESTART"}: resume={resume}; open_obligations={len(obligations)}; slots={len(slots)}; open_prs={len(prs)}')
    emit('CLEANUP: ' + str(scripts / 'stale-process-cleanup.py') + ' --apply --attest-slotless-reap')
    emit('SAFE_POINT: POST /exit-pending {"enabled":true} for every dev slot; never interrupt a dev turn')
    emit('PM_RELAUNCH: tmux respawn-pane -k -t 0:0.0 ' + shlex.join(launch))
    if args.dry_run:
        return
    text = ('---\nname: pm-resume-prompt-' + day + '-0400\ndescription: Daily 04:00 IST PM continuation; live snapshots and agent owners.\n---\n\n'
        '# PM RESUME PROMPT: ' + day + ' 04:00 IST\n\n'
        'Read MEMORY-session-handoff-current.md too. pm-ops.db is authoritative; snapshots are not permission to mutate ownership. '
        'Reconcile live state before resuming work. Never assume a background agent finished.\n\n'
        '## Background agent owners to reconcile and relaunch\n' + '\n'.join('- ' + owner for owner in owners) + '\n\n'
        'Relaunch only actual stopped background agents with still-open work; PM, CTO, Rajiv and slots are owners, not agent launch names. '
        'Use each obligation required_action and evidence for the exact task, not an invented assignment.\n\n'
        '## Open obligations\n```json\n' + json.dumps(obligations, indent=2) + '\n```\n\n'
        '## MoP slots (preserve issues and assignment epochs)\n```json\n' + json.dumps(slots, indent=2) + '\n```\n\n'
        '## Open PRs\n```json\n' + json.dumps(prs, indent=2) + '\n```\n\n'
        '## Completion\nReconcile/relaunch stopped agents, then post one line to Rajiv DM D0AMF0XE6TS. Dev slots exit only at their safe point.\n')
    atomic(resume, text)
    index = memory / 'MEMORY.md'
    lines = index.read_text().splitlines()
    pointer = f'- [RESUME PROMPT {day} 04:00]({resume.name}) — daily PM restart; reconcile and relaunch open agent work.'
    matches = [i for i, line in enumerate(lines) if line.startswith('- [RESUME PROMPT ')]
    if len(matches) != 1:
        raise RuntimeError('Expected exactly one MEMORY.md resume pointer; restart refused')
    lines[matches[0]] = pointer
    atomic(index, '\n'.join(lines) + '\n')
    if not saved.get('cleanup'):
        subprocess.run(['python3', str(scripts / 'stale-process-cleanup.py'), '--apply', '--attest-slotless-reap'], check=True, timeout=300)
        saved['cleanup'] = True
        atomic(state, json.dumps(saved))
    # Arm safe-point exits before replacing the invoking PM process.
    request = urllib.request.Request('http://127.0.0.1:3100/exit-pending', data=b'{"enabled":true}',
        headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(request, timeout=15) as response:
        if json.load(response).get('ok') is not True:
            raise RuntimeError('MoP refused safe-point exits')
    saved['launch_intent'] = True
    atomic(state, json.dumps(saved))
    emit('CHECKPOINTED: resume written, pointer updated, cleanup complete, safe-point exits armed; replacing PM')
    try:
        subprocess.run(['tmux', 'respawn-pane', '-k', '-t', '0:0.0', shlex.join(launch)], check=True)
    except Exception:
        saved['launch_intent'] = False
        atomic(state, json.dumps(saved))
        raise

if args.dry_run:
    perform()
else:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / 'daily-pm-restart.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        perform()
PY
