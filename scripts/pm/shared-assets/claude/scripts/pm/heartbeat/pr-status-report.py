#!/usr/bin/env python3
"""Deterministic hourly per-PR report; no GitHub or ownership mutations.

Posting is called by slack-send only after an Alerts Hourly parent succeeds.
Gate ages are persisted independently of PR head/owner changes.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import urllib.request

HOME = Path.home()
STATE = HOME / '.claude/state/pr-status-report.json'
DB = HOME / '.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db'
REPO = 'heydonna-app/heydonna-app'
RED = {'FAILURE', 'ERROR', 'CANCELLED', 'TIMED_OUT', 'ACTION_REQUIRED', 'STARTUP_FAILURE'}


def stamp(value):
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.replace(tzinfo=parsed.tzinfo or dt.timezone.utc)
    except ValueError:
        return None


def gh(*args):
    result = subprocess.run(['gh', *args], capture_output=True, text=True, timeout=120, check=True)
    return json.loads(result.stdout)


def collect():
    # REST pagination includes drafts and has no fixed PR-count cutoff.
    pages = gh('api', '--paginate', '--slurp', f'repos/{REPO}/pulls?state=open&per_page=100')
    prs = []
    checks = {}
    for page in pages:
        for item in page:
            pr = gh('pr', 'view', str(item['number']), '--repo', REPO, '--json',
                    'number,isDraft,headRefOid,labels,mergeable')
            prs.append(pr)
            # Check-runs endpoint is pinned to the SHA, not branch/latest workflow.
            runs = gh('api', '--paginate', '--slurp',
                      f'repos/{REPO}/commits/{pr["headRefOid"]}/check-runs?per_page=100&filter=latest')
            checks[pr['number']] = [r for p in runs for r in p['check_runs']]
    with urllib.request.urlopen('http://127.0.0.1:3100/slots', timeout=10) as response:
        slots = json.load(response)['slots']
    with sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=5) as con:
        con.row_factory = sqlite3.Row
        obligations = [dict(row) for row in con.execute(
            "SELECT id, pr, owner, suppress_until, kind FROM obligations "
            "WHERE status='open' AND kind IN ('pr_red_unowned','pr_admission') ORDER BY id DESC")]
    return prs, checks, slots, obligations


def latest_checks(runs, head):
    latest = {}
    for run in runs:
        if run.get('head_sha') != head or run.get('name') not in ('test', 'e2e'):
            continue
        name = run['name']
        if run.get('id', 0) > latest.get(name, {}).get('id', -1):
            latest[name] = run
    return {name: ((run.get('conclusion') or '').upper() if run.get('status') == 'completed'
                   else 'PENDING') for name, run in latest.items()}


def classify(pr, runs, slots, obligations, prior, now):
    number, head = pr['number'], pr['headRefOid']
    labels = [item['name'] for item in pr['labels']]
    blocked = sorted(label for label in labels if label.startswith('pm-blocked:'))
    admitted = f'ci-head:{head}' in labels
    admission = 'matches' if admitted else ('stale' if any(x.startswith('ci-head:') for x in labels) else 'missing')
    checks = latest_checks(runs, head)
    ci, e2e = checks.get('test', 'MISSING'), checks.get('e2e', 'MISSING')
    if pr['isDraft']:
        gate, action = 'draft-rework', 'finish rework'
    elif not admitted:
        gate, action = '12b', 'review/admit exact head'
    elif ci in RED or e2e in RED:
        gate = 'CI red' if ci in RED else 'E2E red'
        action = 'repair failing run'
    elif ci == e2e == 'SUCCESS':
        gate, action = 'dual-green/merge ask', 'request exact-head merge'
    else:
        gate, action = 'CI running', 'await terminal' if 'PENDING' in (ci, e2e) else 'reconcile missing gates'
    if pr.get('mergeable') == 'CONFLICTING':
        action = 'resolve conflicts; ' + action
    owner = None
    for slot in slots:
        if slot.get('pr') == number and slot.get('issue') is not None:
            owner = f'S{slot["slot"]}'
            break
    if owner is None:
        for row in obligations:
            until = stamp(row.get('suppress_until'))
            if row.get('pr') == number and row.get('owner') and until and until > now:
                owner = f'{row["owner"]} (until {row["suppress_until"]})'
                break
    previous = prior.get(str(number), {})
    since = stamp(previous.get('since')) if previous.get('gate') == gate else None
    since = since or now
    movement = 'STUCK' if owner is None or (now - since).total_seconds() > 4 * 3600 else 'MOVING'
    if blocked:
        movement = 'PARKED'
        action = 'resolve ' + ','.join(blocked)
    safe_owner = re.sub(r'[\r\n]+', ' ', owner or 'unowned')
    line = (f'#{number} {"draft" if pr["isDraft"] else "ready"} · {gate} '
            f'[ci-head {admission}; CI {ci}; E2E {e2e}; mergeable {pr.get("mergeable", "UNKNOWN")}] '
            f'· {safe_owner} · {action} · {movement}')
    return line, {'gate': gate, 'since': since.isoformat()}


def render(prs, checks, slots, obligations, prior, now):
    lines, state = [], {}
    for pr in sorted(prs, key=lambda item: item['number']):
        line, entry = classify(pr, checks.get(pr['number'], []), slots, obligations, prior, now)
        lines.append(line)
        state[str(pr['number'])] = entry
    return lines or ['No open PRs.'], state


def chunks(lines, limit=35000):
    # Slack hard limit is 40k; split on complete PR lines, never omit a row.
    out, text = [], '*Hourly per-PR status*\n'
    for line in lines:
        if len(line) + 30 > limit:
            raise ValueError('single PR line exceeds Slack limit')
        if len(text) + len(line) + 1 > limit:
            out.append(text)
            text = '*Hourly per-PR status (continued)*\n'
        text += line + '\n'
    return out + [text]


def post(channel, thread, text):
    result = subprocess.run(['bash', str(HOME / '.claude/scripts/slack-send.sh'),
                             '-c', channel, '-t', thread, '-f'], input=text,
                            text=True, capture_output=True, timeout=120,
                            env={**os.environ, 'SLACK_SEND_AS': 'alerts'}, check=True)
    match = re.search(r'OK ts=([0-9.]+)', result.stdout)
    if not match:
        raise RuntimeError('Slack delivery did not return a timestamp')
    print(result.stdout.strip())
    return match.group(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--thread')
    parser.add_argument('--channel', default='C0ALZJHGE49')
    args = parser.parse_args()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with STATE.with_suffix('.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        saved = json.loads(STATE.read_text()) if STATE.exists() else {}
        key = f'{args.channel}:{args.thread}'
        delivered = saved.setdefault('delivered', {})
        if args.thread and delivered.get(key, {}).get('done'):
            print('ALREADY_POSTED ' + key)
            return
        prs, checks, slots, obligations = collect()
        now = dt.datetime.now(dt.timezone.utc)
        lines, ages = render(prs, checks, slots, obligations, saved.get('gates', {}), now)
        saved['gates'] = ages
        # Save ages even if delivery fails; retries cannot reset the STUCK clock.
        def save():
            temporary = STATE.with_suffix('.new')
            temporary.write_text(json.dumps(saved, indent=2) + '\n')
            temporary.replace(STATE)
        save()
        if not args.thread:
            print('\n'.join(lines))
            return
        receipt = delivered.setdefault(key, {'parts': [], 'messages': chunks(lines)})
        save()
        for text in receipt['messages'][len(receipt['parts']):]:
            receipt['parts'].append(post(args.channel, args.thread, text))
            save()
        receipt['done'] = True
        save()


if __name__ == '__main__':
    main()
