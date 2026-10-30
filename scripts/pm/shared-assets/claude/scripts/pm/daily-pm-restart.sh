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

def arm_dev_exit_pending(saved):
    """Rajiv D0AMF0XE6TS ts 1793166992.093769: "the daily restart has to set exit pending on all the dev slots as well".
    Armed on every daily run right after the checkpoint, independent of the PM handoff. Dev slots exit only at
    their own idle safe point (MoP Stop hook / health idle scan; DND skipped). The PM (slot 0) must NOT be
    cycled by this flag: it is replaced only by the effect phase via /slots/0/respawn. MoP's flag is global and
    includes slot 0, so arm ONLY when MoP advertises the dev-only scope; otherwise record the block, never arm."""
    if saved.get('dev_exit_pending_armed') is True:
        emit('DEV_EXIT_PENDING: already armed for ' + day)
        return
    with urllib.request.urlopen('http://127.0.0.1:3100/exit-status', timeout=15) as response:
        status = json.load(response)
    if 'dev_only' not in status:
        saved['dev_exit_pending_armed'] = 'blocked_mop_dev_only_scope_missing'
        atomic(state, json.dumps(saved))
        emit('DEV_EXIT_PENDING_BLOCKED: MoP /exit-pending has no dev_only scope; global flag would also /exit the PM. Needs mop-main-repair change.')
        return
    request = urllib.request.Request('http://127.0.0.1:3100/exit-pending', data=b'{"enabled":true,"dev_only":true}',
        headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(request, timeout=15) as response:
        body = json.load(response)
    if body.get('ok') is not True or (body.get('status') or {}).get('dev_only') is not True:
        raise RuntimeError('MoP refused dev-only safe-point exits')
    saved['dev_exit_pending_armed'] = True
    atomic(state, json.dumps(saved))
    emit('DEV_EXIT_PENDING: armed for all dev slots (idle safe point, DND skipped, PM excluded)')

def resume_on_idle(saved):
    if saved.get('handoff_status') != 'pending':
        emit('NO_PENDING_HANDOFF: nothing to replace')
        return
    requested = saved['request_identity']
    pin = pm_identity(requested['pane'])
    db = Path(os.environ.get('MOP_DB_PATH', str(home / '.local/share/master-of-panes/data/mop.db')))
    with sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True) as connection:
        later_work = connection.execute("SELECT count(*) FROM events WHERE slot=0 AND id>? AND event_type IN ('UserPromptSubmit','pm_status_busy','PreToolUse')", (requested['work'],)).fetchone()[0]
        completions = connection.execute("SELECT id,payload FROM events WHERE slot=0 AND id>? AND event_type='PostToolUse' ORDER BY id", (requested['work'],)).fetchall()
        stopped = connection.execute("SELECT max(id) FROM events WHERE slot=0 AND event_type='Stop'").fetchone()[0]
    completion = json.loads(completions[0][1]) if len(completions) == 1 else {}
    own_return = (len(completions) == 1 and completion.get('tool_name') == 'Bash'
        and 'daily-pm-restart.sh' in str((completion.get('tool_input') or {}).get('command', ''))
        and isinstance(stopped, int) and stopped > completions[0][0])
    soft_boundary = bool(os.environ.get('MOP_PM_SOFT_BOUNDARY_REQUEST'))
    # Completed intervening turns are allowed ONLY under MoP's current,
    # session-bound tool/admission fence. The effect pin below still includes
    # submit/work and is rechecked at every existing irreversible boundary.
    def checkpoint_owner(value):
        result = {k: v for k, v in owner(value).items() if not (soft_boundary and k == 'submit')}
        if soft_boundary:
            # Auto-compaction emits SessionStart with the SAME session id.
            # It is not a successor process; birth/pane/pid remain pinned.
            result['boot'] = json.loads(value['boot'][1]).get('session_id')
        return result
    if checkpoint_owner(pin) != checkpoint_owner(requested) or (not soft_boundary and (later_work or not own_return)):
        saved['handoff_status'] = 'refused_successor_or_work'
        atomic(state, json.dumps(saved))
        emit('REFUSED: checkpoint owner or productive work changed before idle handoff')
        return
    if not saved.get('cleanup'):
        subprocess.run(['python3', str(scripts / 'stale-process-cleanup.py'), '--apply', '--attest-slotless-reap'], check=True, timeout=300)
        saved['cleanup'] = True
        atomic(state, json.dumps(saved))
    if pm_identity(pin['pane']) != pin:
        raise RuntimeError('PM identity/work changed during cleanup; restart deferred')
    # Dev exit-pending is armed at checkpoint time (arm_dev_exit_pending); never re-arm the global
    # flag here, which would also /exit the respawned PM (double restart).
    if pm_identity(pin['pane']) != pin:
        raise RuntimeError('PM identity/work changed during checkpoint/cleanup; restart deferred')
    saved['launch_intent'] = True
    saved['pm_identity'] = pin
    saved['replacement_status'] = 'attempted_uncertain'
    atomic(state, json.dumps(saved))
    emit('CHECKPOINTED: Stop/idle handoff replacing the pinned PM')
    if pm_identity(pin['pane']) != pin:
        raise RuntimeError('PM successor/work after attempted checkpoint; replacement suppressed')
    # Rajiv 2026-10-28 D0AMF0XE6TS ts 1793164925.956469: "pm pane is supposed to restart through MoP".
    # MoP owns the replacement: idle-gated /exit, RESTART_COMMANDS[0] (launch-pm-cliproxy.sh --continue),
    # boot wait, generic "continue". Never tmux respawn-pane the PM directly.
    respawn_body = {'continue_session': True}
    if soft_boundary:
        respawn_body['pm_soft_boundary_request'] = os.environ['MOP_PM_SOFT_BOUNDARY_REQUEST']
    respawn = urllib.request.Request('http://127.0.0.1:3100/slots/0/respawn', data=json.dumps(respawn_body).encode(),
        headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(respawn, timeout=280) as response:
            body = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 409:
            saved['replacement_status'] = 'deferred_mop_409'
            atomic(state, json.dumps(saved))
            emit('DEFERRED: MoP respawn 409 (busy or already in progress): ' + error.read().decode(errors='replace')[:300])
            return
        raise RuntimeError(f'MoP respawn HTTP {error.code}; retain attempted state, no retry')
    if body.get('success') is not True:
        raise RuntimeError('MoP respawn not acknowledged; retain attempted state, no retry')
    # MoP's respawn injects only "continue"; deliver the dated resume prompt through MoP's PM send path.
    send = urllib.request.Request('http://127.0.0.1:3100/slots/0/send',
        data=json.dumps({'command': saved['continuation_prompt']}).encode(),
        headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(send, timeout=60) as response:
            emit('RESUME_PROMPT_SENT: ' + json.dumps(json.load(response))[:200])
    except Exception as error:
        emit('RESUME_PROMPT_SEND_FAILED (new PM still has MEMORY.md resume pointer): ' + str(error)[:200])
    saved['replacement_status'] = 'requested'
    saved['handoff_status'] = 'requested'
    atomic(state, json.dumps(saved))

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
        resume_on_idle(saved)
        return
    if saved and not saved.get('handoff_status'):
        emit('LEGACY_UNCERTAIN: retained pre-handoff state is not permission for another replacement')
        return
    if saved.get('handoff_status'):
        emit('ALREADY_CHECKPOINTED: inspect retained handoff; no new owner or replacement attempt')
        if not args.dry_run:
            arm_dev_exit_pending(saved)
        return
    pin = pm_identity(require_idle=False)
    obligations, slots, prs = snapshot()
    owners = sorted({r['owner'] for r in obligations if r.get('owner')})
    prompt = ('Read ' + str(resume) + '. Resume PM from the canonical ledger. Relaunch the listed background agents only after checking their live state; '
              'do not duplicate completed work. Run Skill(daily-pm-restart) post-restart completion: post one line to Rajiv DM D0AMF0XE6TS.')
    emit(f'{"DRY_RUN" if args.dry_run else "RESTART"}: resume={resume}; open_obligations={len(obligations)}; slots={len(slots)}; open_prs={len(prs)}')
    emit('DEV_EXIT_PENDING (first, after checkpoint, independent of PM handoff): GET /exit-status requires dev_only scope; POST /exit-pending {"enabled":true,"dev_only":true}; idempotent per IST day via state dev_exit_pending_armed; dev slots exit only at idle, DND skipped, PM excluded')
    emit('CLEANUP: ' + str(scripts / 'stale-process-cleanup.py') + ' --apply --attest-slotless-reap')
    emit('PM_RELAUNCH: MoP POST http://127.0.0.1:3100/slots/0/respawn {"continue_session":true} (RESTART_COMMANDS[0]=launch-pm-cliproxy.sh --continue); then POST /slots/0/send resume prompt; 409=deferred')
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
    arm_dev_exit_pending(saved)
    emit('CHECKPOINTED: return from the skill; MoP continues once at the existing debounced Stop/idle boundary')

if args.dry_run:
    perform()
else:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / 'daily-pm-restart.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        perform()
PY
