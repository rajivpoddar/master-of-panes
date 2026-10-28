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
import sys
import time
import urllib.request

HOME = Path.home()
STATE = HOME / '.claude/state/pr-status-report.json'
DB = Path(os.environ.get('PM_OPS_DB', str(
    HOME / '.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db')))
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


def fetch_checks(head, retry_delay=3):
    """Per-PR check-runs; retry once, then None (unavailable) instead of aborting."""
    for attempt in range(2):
        try:
            runs = gh('api', '--paginate', '--slurp',
                      f'repos/{REPO}/commits/{head}/check-runs?per_page=100&filter=latest')
            return [r for p in runs for r in p['check_runs']]
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as exc:
            print(f'WARN check-runs fetch failed for {head[:12]} (attempt {attempt + 1}): {exc}', file=sys.stderr)
            if attempt == 0:
                time.sleep(retry_delay)
    return None


def collect():
    # REST pagination includes drafts and has no fixed PR-count cutoff.
    pages = gh('api', '--paginate', '--slurp', f'repos/{REPO}/pulls?state=open&per_page=100')
    prs = []
    checks = {}
    for page in pages:
        for item in page:
            pr = None
            for attempt in range(2):
                try:
                    pr = gh('pr', 'view', str(item['number']), '--repo', REPO, '--json',
                            'number,isDraft,headRefOid,headRefName,labels,mergeable')
                    break
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as exc:
                    print(f'WARN pr view #{item["number"]} failed (attempt {attempt + 1}): {exc}', file=sys.stderr)
                    if attempt == 0:
                        time.sleep(3)
            if pr is None:
                # Fall back to the REST list row; mergeable is unknown.
                pr = {'number': item['number'], 'isDraft': bool(item.get('draft')),
                      'headRefOid': item['head']['sha'], 'headRefName': item['head']['ref'],
                      'labels': [{'name': x['name']} for x in item.get('labels', [])],
                      'mergeable': 'UNKNOWN'}
            prs.append(pr)
            # Check-runs endpoint is pinned to the SHA, not branch/latest workflow.
            checks[pr['number']] = fetch_checks(pr['headRefOid'])
    with urllib.request.urlopen('http://127.0.0.1:3100/slots', timeout=10) as response:
        slots = json.load(response)['slots']
    for slot in slots:
        checkout = HOME / f'Downloads/projects/heydonna-app-{3000 + int(slot["slot"])}'
        branch = subprocess.run(['git', '-C', str(checkout), 'branch', '--show-current'],
                                capture_output=True, text=True, timeout=10)
        slot['checkout_branch'] = branch.stdout.strip() if branch.returncode == 0 else None
    with sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=5) as con:
        con.row_factory = sqlite3.Row
        obligations = [dict(row) for row in con.execute(
            "SELECT id, pr, target_type, target_id, owner, suppress_until, kind, evidence_json FROM obligations "
            "WHERE status='open' AND (target_type='github_pr' OR "
            "kind IN ('pr_red_unowned','pr_admission','pr_merge_ask')) ORDER BY id DESC")]
    bridge = HOME / 'Downloads/projects/tmux-slack-bridge/bridge.db'
    if bridge.exists():
        with sqlite3.connect(f'file:{bridge}?mode=ro', uri=True, timeout=5) as con:
            messages = [row[0] for row in con.execute(
                "SELECT body FROM messages WHERE direction='outbound' AND "
                "channel_id='C0ALZJHGE49' AND lower(body) LIKE '%merge ask%' "
                "ORDER BY ts DESC LIMIT 2000")]
        for pr in prs:
            pr['merge_ask_sent'] = any(
                re.search(r'#' + str(pr['number']) + r'\b', body)
                and pr['headRefOid'][:12] in body for body in messages)
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


def claim_evidence(row):
    try:
        value = json.loads(row.get('evidence_json') or '{}')
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


MERGEABLE_OK = {'MERGEABLE', 'CLEAN'}


def hold_reason(pr, obligations):
    """Shared hold check: pm-blocked:*, pm-state:blocked-*, or an open pr_admission 'HELD' obligation."""
    labels = [item['name'] for item in pr['labels']]
    held = sorted(x for x in labels if x.startswith('pm-blocked:') or x.startswith('pm-state:blocked-'))
    if held:
        return ','.join(held)
    for row in obligations or []:
        if row.get('pr') != pr['number'] or row.get('kind') != 'pr_admission':
            continue
        raw = row.get('evidence_json') or ''
        texts = [raw] + list(claim_evidence(row).keys())
        for text in texts:
            text = text.lstrip('{"\' ')
            if text.startswith('HELD'):
                return re.sub(r'\s+', ' ', text[4:].lstrip(':- ').split('(')[0].split(';')[0]).strip()[:80]
    return None


def classifier_e2e_required(number, head):
    """change_scope.py classifier (main copy); fail closed to True."""
    script = HOME / 'Downloads/projects/heydonna-app/scripts/ci/change_scope.py'
    try:
        out = subprocess.run([sys.executable, str(script), '--repo', REPO, '--pr', str(number),
                              '--expected-head', head], capture_output=True, text=True, timeout=90,
                             cwd=script.parent.parent.parent, check=True).stdout
        return bool(json.loads(out).get('e2e_required', True))
    except Exception:
        return True


def merge_gate(pr, runs, obligations, e2e_required=None):
    """Single merge-ask gate (shared by tracked-features). Returns (gate, action, checks).

    Merge ask only when: non-draft, no hold, live head == ci-head label, CI SUCCESS,
    E2E SUCCESS (or not required by the classifier), mergeable MERGEABLE/CLEAN.
    """
    number, head = pr['number'], pr['headRefOid']
    labels = [item['name'] for item in pr['labels']]
    admitted = f'ci-head:{head}' in labels
    checks = latest_checks(runs or [], head)
    ci, e2e = checks.get('test', 'MISSING'), checks.get('e2e', 'MISSING')
    if ci == 'SUCCESS' and e2e in ('MISSING', 'SKIPPED', 'NEUTRAL'):
        if e2e_required is None:
            e2e_required = classifier_e2e_required(number, head)
        if not e2e_required:
            e2e = 'NOT_REQUIRED'
    hold = hold_reason(pr, obligations)
    mergeable = pr.get('mergeable') or 'UNKNOWN'
    if pr['isDraft']:
        gate, action = 'draft-rework', 'finish rework'
    elif hold:
        gate, action = 'held', f'held: {hold}'
    elif not admitted:
        gate, action = '12b', 'review/admit exact head'
    elif ci in RED or e2e in RED:
        gate = 'CI red' if ci in RED else 'E2E red'
        action = 'repair failing run'
    elif ci == 'SUCCESS' and e2e in ('SUCCESS', 'NOT_REQUIRED'):
        if mergeable in MERGEABLE_OK:
            gate, action = 'dual-green/merge ask', 'request exact-head merge'
        else:
            gate = 'green/not mergeable'
            action = 'resolve conflicts' if mergeable == 'CONFLICTING' else f'reconcile mergeable {mergeable}'
    else:
        gate, action = 'CI running', 'await terminal' if 'PENDING' in (ci, e2e) else 'reconcile missing gates'
    if mergeable == 'CONFLICTING' and not action.startswith('resolve conflicts'):
        action = 'resolve conflicts; ' + action
    return gate, action, {'ci': ci, 'e2e': e2e, 'admitted': admitted}


def pending_ci(head):
    """Workflow status covers queued runs before their test check is created."""
    try:
        runs = gh('run', 'list', '--repo', REPO, '--workflow', 'ci.yml',
                  '--commit', head, '--json', 'headSha,status')
        return any(run.get('headSha') == head and run.get('status') in ('queued', 'in_progress')
                   for run in runs)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError, TypeError) as exc:
        print(f'WARN CI runs fetch failed for {head[:12]}: {exc}', file=sys.stderr)
        return False


def classify(pr, runs, slots, obligations, prior, now):
    number, head = pr['number'], pr['headRefOid']
    labels = [item['name'] for item in pr['labels']]
    admitted = f'ci-head:{head}' in labels
    admission = 'matches' if admitted else ('stale' if any(x.startswith('ci-head:') for x in labels) else 'missing')
    gate, action, g = merge_gate(pr, runs, obligations)
    ci, e2e = g['ci'], g['e2e']
    blocked = hold_reason(pr, obligations)
    owner = None
    for slot in slots:
        # Checkout branches survive release/reassignment; only MoP's current,
        # occupied PR+head assignment is authority, never leftover git state.
        if slot.get('occupied') is True and slot.get('pr') == number and slot.get('head_sha') == head:
            owner = f'S{slot["slot"]}'
            break
    active_owner = False
    if owner is None:
        for row in obligations:
            until = stamp(row.get('suppress_until'))
            if (row.get('target_type') == 'github_pr' and str(row.get('target_id')) == str(number)
                    and row.get('owner') and until and until > now):
                owner = f'owner: {row["owner"]} (until {until.astimezone().strftime("%H:%M")})'
                active_owner = True
                break
    workflow_wait = (gate == 'CI running' and admitted and action == 'reconcile missing gates'
                     and pending_ci(head))
    ci_wait = gate == 'CI running' and admitted and ('PENDING' in (ci, e2e) or workflow_wait)
    if ci_wait:
        action = 'await terminal'
        if owner is None:
            owner = f'CI (admitted {head[:12]})'
    merge_ask = pr.get('merge_ask_sent', False)
    for row in obligations:
        if row.get('pr') == number and row.get('kind') == 'pr_merge_ask':
            evidence = claim_evidence(row)
            merge_ask = merge_ask or evidence.get('head') == head
    if gate == 'dual-green/merge ask' and merge_ask:
        owner, action = 'CTO (merge ask sent)', 'await merge decision'
    previous = prior.get(str(number), {})
    since = stamp(previous.get('since')) if previous.get('gate') == gate else None
    since = since or now
    movement = 'STUCK' if owner is None or (now - since).total_seconds() > 4 * 3600 else 'OWNED'
    if movement != 'STUCK':
        if ci_wait:
            latest = {}
            for run in runs:
                if run.get('head_sha') == head and run.get('name') in ('test', 'e2e'):
                    name = run['name']
                    if run.get('id', 0) > latest.get(name, {}).get('id', -1):
                        latest[name] = run
            movement = ('QUEUED' if any(r.get('status') in ('queued', 'waiting', 'requested', 'pending')
                                        for r in latest.values()) else 'WAITING')
        elif gate == 'dual-green/merge ask' and merge_ask:
            movement = 'WAITING'
        elif previous.get('head') and previous['head'] != head:
            movement = 'MOVING'  # observed new pushed head, not merely a claim

    if active_owner:
        movement = 'OWNED'
    if workflow_wait:
        movement = 'WAITING'
    if blocked:
        movement = 'PARKED'
    safe_owner = re.sub(r'[\r\n]+', ' ', owner or 'unowned')
    line = (f'#{number} {"draft" if pr["isDraft"] else "ready"} · {gate} '
            f'[ci-head {admission}; CI {ci}; E2E {e2e}; mergeable {pr.get("mergeable", "UNKNOWN")}] '
            f'· {safe_owner} · {action} · {movement}')
    return line, {'gate': gate, 'since': since.isoformat(), 'head': head}


def render(prs, checks, slots, obligations, prior, now):
    lines, state, failed = [], {}, 0
    for pr in sorted(prs, key=lambda item: item['number']):
        runs = checks.get(pr['number'], [])
        if runs is None:
            failed += 1
            labels = [item['name'] for item in pr['labels']]
            lines.append(f'#{pr["number"]} {"draft" if pr["isDraft"] else "ready"} · '
                         f'checks: unavailable (GitHub API error) · head {pr["headRefOid"][:12]} '
                         f'· mergeable {pr.get("mergeable", "UNKNOWN")}'
                         + (' · ' + ','.join(sorted(x for x in labels if x.startswith('pm-blocked:')))
                            if any(x.startswith('pm-blocked:') for x in labels) else ''))
            if str(pr['number']) in prior:
                state[str(pr['number'])] = prior[str(pr['number'])]  # keep gate age
            continue
        line, entry = classify(pr, runs, slots, obligations, prior, now)
        lines.append(line)
        state[str(pr['number'])] = entry
    if prs and failed * 2 > len(prs):
        lines.insert(0, f'GitHub API degraded: check-runs unavailable for {failed}/{len(prs)} PRs')
    return lines or ['No open PRs.'], state


def chunks(lines, limit=35000):
    # Slack hard limit is 40k; split on complete PR lines, never omit a row.
    out, text, count = [], '*Hourly per-PR status*\n\n', 0
    for line in lines:
        if len(line) + 30 > limit:
            raise ValueError('single PR line exceeds Slack limit')
        if len(text) + len(line) + 2 > limit or count >= 40:
            out.append(text)
            text, count = '*Hourly per-PR status (continued)*\n\n', 0
        text += line + '\n\n'
        count += 1
    return out + [text]


def post(channel, thread, text):
    # Separate Block Kit sections guarantee one PR per visible row; provide the
    # same explicit fallback body for bridge consumers and notifications.
    blocks = [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': line}}
              for line in text.splitlines() if line]
    result = subprocess.run(['bash', str(HOME / '.claude/scripts/slack-send.sh'),
                             '-c', channel, '-t', thread, '-b', json.dumps(blocks), '-f'], input=text,
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
        receipt = delivered.get(key) if args.thread else None
        if receipt and (receipt.get('attempt') is not None or
                (len(receipt['parts']) < len(receipt['messages'])
                 and receipt.get('delivery_version') != 1)):
            # Older receipts cannot prove the next part was never accepted.
            # Keep confirmed parts and stop before even recollecting providers.
            raise RuntimeError('UNCERTAIN Slack part; reconcile the existing thread before retrying: '
                               + key + f'; confirmed_parts={len(receipt["parts"])}')
        prs, checks, slots, obligations = collect()
        now = dt.datetime.now(dt.timezone.utc)
        lines, ages = render(prs, checks, slots, obligations, saved.get('gates', {}), now)
        saved['gates'] = ages
        # Save ages even if delivery fails; retries cannot reset the STUCK clock.
        def save():
            temporary = STATE.with_suffix('.new')
            with temporary.open('w') as handle:
                handle.write(json.dumps(saved, indent=2) + '\n')
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(STATE)
            directory = os.open(STATE.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        save()
        if not args.thread:
            print('\n'.join(lines))
            return
        receipt = delivered.setdefault(key, {'parts': [], 'messages': chunks(lines), 'delivery_version': 1})
        save()
        if receipt.get('attempt') is not None:
            raise RuntimeError('UNCERTAIN Slack part; reconcile the existing thread before retrying: '
                               + json.dumps(receipt['attempt']))
        for index in range(len(receipt['parts']), len(receipt['messages'])):
            # Save intent durably BEFORE sending. A lost response or crash must
            # never turn an accepted send into a duplicate on the next run.
            receipt['attempt'] = {'index': index, 'started_at': now.isoformat()}
            save()
            receipt['parts'].append(post(args.channel, args.thread, receipt['messages'][index]))
            receipt.pop('attempt')
            save()
        receipt['done'] = True
        save()


if __name__ == '__main__':
    main()
