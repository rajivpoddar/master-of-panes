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
import re
import shlex
import sqlite3
import subprocess
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

ap = argparse.ArgumentParser()
mode = ap.add_mutually_exclusive_group()
mode.add_argument('--dry-run', action='store_true')
mode.add_argument('--complete-on-idle', action='store_true')
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
    with temporary.open('w') as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

# Long-lived PM infrastructure children (MCP servers, caffeinate, zombie/defunct
# helpers) never mean a turn is working. Anything else (Bash shells, python, node
# tool runs, git, gh, npx, test runners) is tool work and blocks the effect phase.
INFRA_PATTERNS = [
    re.compile(r'^(npm exec|npx)\s+\S*mcp\S*(\s|$)'),          # npm exec mcp-server-axiom / @upstash/context7-mcp
    re.compile(r'(^|\s|/)(node\s+)?\S*/node_modules/\.bin/\S*mcp\S*(\s|$)'),
    re.compile(r'^(\S*/)?\S*mcp-server\S*(\s|$)'),
    re.compile(r'^(\S*/)?caffeinate(\s|$)'),
    re.compile(r'^\([^)]*\)$'),                                   # defunct/zombie helper, e.g. "(git)"
]

def classify_children(processes, pids):
    """Return (allowed, blocking). Descendants of an allowlisted MCP root are allowed;
    any non-infra process (or descendant of one) blocks."""
    def infra(p):
        return any(rx.search(processes[p][8].strip()) for rx in INFRA_PATTERNS)
    allowed, blocking = set(), set()
    for p in pids:
        chain, q = [], p
        while q in pids:
            chain.append(q)
            q = int(processes[q][1])
        (allowed if any(infra(c) for c in chain) and infra(chain[-1]) else blocking).add(p)
    return allowed, blocking

def pm_children_report(target='0:0.0'):
    fields = subprocess.run(['tmux', 'display-message', '-t', target, '-p', '#{pane_pid}'],
        capture_output=True, text=True, check=True, timeout=5).stdout.strip()
    listing = subprocess.run(['ps', '-axo', 'pid=,ppid=,uid=,lstart=,command='],
        capture_output=True, text=True, check=True, timeout=5).stdout
    processes = {}
    for line in listing.splitlines():
        row = line.split(None, 8)
        if len(row) == 9 and row[0].isdigit() and row[1].isdigit():
            processes[int(row[0])] = row
    root = int(fields); members, pending = set(), [root]
    while pending:
        pid = pending.pop()
        if pid in members: continue
        members.add(pid)
        pending.extend(p for p, r in processes.items() if int(r[1]) == pid)
    agents = {p for p in members if re.search(r'(^|/)(claude|openclaude)(\s|$)', processes[p][8])}
    allowed, blocking = classify_children(processes, members - {root} - agents)
    fmt = lambda xs: ', '.join(f'{p}:{processes[p][8].strip()[:60]}' for p in sorted(xs)) or '-'
    emit(f'PM_CHILDREN: allowlisted={len(allowed)} [{fmt(allowed)}]')
    emit(f'PM_CHILDREN: would_block={len(blocking)} [{fmt(blocking)}]')

def pm_identity(target='0:0.0', require_idle=True):
    """Read-only PM pin: immutable pane/session, process birth and monotonic hook work."""
    with urllib.request.urlopen('http://127.0.0.1:3100/pm-status', timeout=15) as response:
        status = json.load(response)
    boundary_request = os.environ.get('MOP_PM_SOFT_BOUNDARY_REQUEST')
    proof = status.get('soft_boundary') or {}
    soft_boundary = (bool(boundary_request) and proof.get('request') == boundary_request
        and proof.get('held') is True and proof.get('stopped') is True
        and proof.get('admission_fenced') is True and proof.get('cli_queue_empty') is True
        and proof.get('tools_in_flight') == 0)
    if boundary_request and not soft_boundary:
        raise RuntimeError('PM soft-boundary admission fence is no longer authoritative')
    if require_idle and (status.get('pm_busy') is not False
            or (status.get('queue_count') != 0 and not soft_boundary)):
        raise RuntimeError('PM busy, queued, or activity unknown; restart deferred')
    fields = subprocess.run(['tmux', 'display-message', '-t', target, '-p',
        '#{pane_id}|#{session_id}|#{pane_pid}|#{pane_current_path}|#{pane_current_command}'],
        capture_output=True, text=True, check=True, timeout=5).stdout.strip().split('|')
    if (len(fields) != 5 or not re.fullmatch(r'%\d+', fields[0])
            or not re.fullmatch(r'\$\d+', fields[1]) or not fields[2].isdigit()
            or fields[3] != str(home / 'Downloads/projects/heydonna-app')
            or (fields[4] not in ('claude', 'openclaude') and not re.fullmatch(r'\d+\.\d+\.\d+', fields[4]))):
        raise RuntimeError('PM pane/session/checkout identity unknown')
    listing = subprocess.run(['ps', '-axo', 'pid=,ppid=,uid=,lstart=,command='],
        capture_output=True, text=True, check=True, timeout=5).stdout
    processes = {}
    for line in listing.splitlines():
        row = line.split(None,8)
        if len(row) != 9 or not all(v.isdigit() for v in row[:3]):
            raise RuntimeError('PM process table unreadable')
        # macOS /bin/ps is setuid root: this script's own direct ps probe shows
        # as a uid-0 descendant of the PM when run from its Bash tool. Skip only
        # that probe (our direct child running exactly this ps); any other
        # non-owner process still refuses.
        if int(row[1]) == os.getpid() and row[8].startswith('ps -axo pid=,ppid=,uid=,lstart=,command='):
            continue
        processes[int(row[0])] = row
    members, pending = set(), [int(fields[2])]
    while pending:
        pid = pending.pop()
        if pid in members: raise RuntimeError('PM process ancestry cycle')
        members.add(pid)
        pending.extend(p for p,r in processes.items() if int(r[1]) == pid)
    if not members <= processes.keys() or any(int(processes[p][2]) != os.getuid() for p in members):
        raise RuntimeError('PM process ownership unknown')
    agents = [p for p in members if re.search(r'(^|/)(claude|openclaude)(\s|$)',processes[p][8])]
    allowed, blocking = classify_children(processes, members - {int(fields[2]), *agents})
    if len(agents) != 1 or (require_idle and blocking):
        raise RuntimeError('PM active descendant work or foreground identity unknown: '
            + '; '.join(f'{p} {processes[p][8][:80]}' for p in sorted(blocking))[:400])
    # Checkpointing is effect-free and may run in this Claude's Bash/Python
    # subtree. Pin only its owner; the idle effect phase still rejects ALL tools.
    birth = [processes[p] for p in sorted({int(fields[2]), *agents})]
    capture = subprocess.run(['tmux', 'capture-pane', '-p', '-t', fields[0], '-S', '-15'],
        capture_output=True, text=True, check=True, timeout=5).stdout
    prompts = [line.strip() for line in capture.splitlines() if line.strip().startswith('❯')]
    if require_idle and (not prompts or prompts[-1] != '❯'
            or re.search(r'press\s+up\s+to\s+edit\s+queued\s+messages|ctrl\+x\s+ctrl\+s\s+to\s+send\s+now', capture, re.I)):
        raise RuntimeError('PM composer not proven empty')
    db = Path(os.environ.get('MOP_DB_PATH', str(home / '.local/share/master-of-panes/data/mop.db')))
    with sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True) as connection:
        boot = connection.execute("SELECT id,payload FROM events WHERE slot=0 AND event_type='SessionStart' ORDER BY id DESC LIMIT 1").fetchone()
        work = connection.execute("SELECT max(id) FROM events WHERE slot=0 AND event_type IN ('UserPromptSubmit','pm_status_busy','PreToolUse','PostToolUse','Stop')").fetchone()[0]
        submit = connection.execute("SELECT max(id) FROM events WHERE slot=0 AND event_type IN ('UserPromptSubmit','pm_status_busy')").fetchone()[0]
    if not boot or not isinstance(work, int) or not isinstance(submit, int):
        raise RuntimeError('PM boot/work history unavailable')
    if soft_boundary and (proof.get('boot_id') != boot[0]
            or proof.get('session_id') != json.loads(boot[1]).get('session_id')):
        raise RuntimeError('PM soft-boundary session changed')
    return {'pane': fields[0], 'session': fields[1], 'pid': fields[2], 'birth': birth,
            'checkout': fields[3], 'command': fields[4], 'boot': list(boot), 'work': work, 'submit': submit}

def owner(pin):
    return {k:v for k,v in pin.items() if k != 'work'}

def arm_pm_exit_pending(saved):
    """Rajiv 1793501776.726249, 1793502261.099499, 1793502605.714159:
    PM exits at its next Stop, resumes its saved prompt, THEN MoP arms dev exits.
    Rajiv 1793752982.460979 ("implement the pm exit pending the same way ... stop
    all subagents after save handoff"): MoP opens pm_save_handoff (variant exit);
    /exit waits for the saved handoff files AND POST /exit-pending/subagents-stopped.
    No maintenance, input gate, idle polling or tracked-tool accounting."""
    if saved.get('pm_exit_pending_armed') is True:
        emit('PM_EXIT_PENDING: already armed for ' + day)
        return
    with urllib.request.urlopen('http://127.0.0.1:3100/exit-status', timeout=15) as response:
        status = json.load(response)
    if 'pm_only' not in status or 'dev_only' not in status:
        saved['pm_exit_pending_armed'] = 'blocked_mop_pm_only_scope_missing'
        atomic(state, json.dumps(saved))
        emit('PM_EXIT_PENDING_BLOCKED: MoP lacks PM-first exit-pending support; no global or dev-only fallback')
        return
    # Persist intent before HTTP: an accepted request with lost ACK must not
    # reset the PM cycle flag and cause another restart on a repeated skill.
    saved['launch_intent'] = True
    saved['replacement_status'] = 'attempted_uncertain'
    saved['handoff_status'] = 'armed'
    atomic(state, json.dumps(saved))
    request = urllib.request.Request('http://127.0.0.1:3100/exit-pending',
        data=json.dumps({'enabled': True, 'pm_only': True, 'continuation_prompt': saved['continuation_prompt']}).encode(),
        headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(request, timeout=15) as response:
        body = json.load(response)
    if body.get('ok') is not True or (body.get('status') or {}).get('pm_only') is not True:
        raise RuntimeError('MoP refused PM-only safe-point exit; retain intent, no automatic retry')
    saved['pm_exit_pending_armed'] = True
    saved['replacement_status'] = 'exit_pending_armed'
    atomic(state, json.dumps(saved))
    emit('PM_EXIT_PENDING: MoP opened pm_save_handoff (variant exit). PM saves MEMORY-session-handoff-current.md + data/pm-continuation-note.md, '
         'stops ALL background agents with TaskStop, then POST /exit-pending/subagents-stopped; MoP sends /exit at the next Stop after both. '
         'Watchdog resumes saved prompt, THEN arms dev-only exits (next Stop, DND skipped)')

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
    if args.dry_run and saved:
        emit('DRY_RUN_NOTE: retained state ' + str(saved.get('handoff_status') or saved.get('replacement_status')) + '; showing plan only')
        saved = {}
    if saved.get('launch_intent'):
        emit('ALREADY_REQUESTED: PM restart was checkpointed for ' + day + '; inspect pane before any manual retry')
        return
    if args.complete_on_idle:
        emit('NO_IDLE_REPLACEMENT: PM-first exit-pending is owned by the Stop hook and watchdog')
        return
    if saved and not saved.get('handoff_status'):
        emit('LEGACY_UNCERTAIN: retained pre-handoff state is not permission for another replacement')
        return
    if saved.get('handoff_status'):
        emit('ALREADY_CHECKPOINTED: inspect retained handoff; no new owner or replacement attempt')
        if not args.dry_run:
            arm_pm_exit_pending(saved)
        return
    pin = pm_identity(require_idle=False)
    obligations, slots, prs = snapshot()
    owners = sorted({r['owner'] for r in obligations if r.get('owner')})
    prompt = ('Read ' + str(resume) + '. Resume PM from the canonical ledger. Relaunch the listed background agents only after checking their live state; '
              'do not duplicate completed work. Run Skill(daily-pm-restart) post-restart completion: post one line to Rajiv DM D0AMF0XE6TS.')
    emit(f'{"DRY_RUN" if args.dry_run else "RESTART"}: resume={resume}; open_obligations={len(obligations)}; slots={len(slots)}; open_prs={len(prs)}')
    emit('PM_RELAUNCH: POST /exit-pending pm_only with saved continuation; MoP opens pm_save_handoff exit; PM saves handoff, TaskStops all agents, '
         'acks POST /exit-pending/subagents-stopped; /exit at the next Stop after save+ack; watchdog uses RESTART_COMMANDS[0]')
    emit('DEV_EXIT_PENDING: only AFTER PM boot and continuation delivery; dev_only, next Stop, DND skipped')
    if args.dry_run:
        pm_children_report()
        return
    if owner(pm_identity(pin['pane'], require_idle=False)) != owner(pin):
        raise RuntimeError('PM owner changed before writing the checkpoint')
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
    if owner(pm_identity(pin['pane'], require_idle=False)) != owner(pin):
        raise RuntimeError('PM owner changed while checkpointing')
    saved['request_identity'] = pin
    saved['requested_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    saved['continuation_prompt'] = prompt
    saved['handoff_status'] = 'pending'
    atomic(state, json.dumps(saved))
    arm_pm_exit_pending(saved)
    emit('CHECKPOINTED: PM remains active (nothing gated); MoP sends /exit once at the next Stop after the handoff save + subagents-stopped ack')

if args.dry_run:
    perform()
else:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / 'daily-pm-restart.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        perform()
PY
