#!/usr/bin/env python3
"""Deterministic hourly per-PR report; no GitHub or ownership mutations.

Posting is called by slack-send only after an Alerts Hourly parent succeeds.
Gate ages are persisted independently of PR head/owner changes.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import importlib.util
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
PROXY_LOG_DIR = Path('/tmp/proxy-agent')
PROXY_RUN_DIR = Path(os.environ.get('PROXY_AGENT_RUN_STATE_DIR', str(HOME / '.local/state/proxy-agent-runs')))
PROXY_PROCESS = re.compile(
    r'^\s*(\d+)\s+(?:\S*/)?claude\s+-p\s+--name\s+'
    r'([A-Za-z0-9][A-Za-z0-9._-]{0,79})(?=\s|$)')
PROXY_LAUNCH = re.compile(
    r'^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\] LAUNCH name=(\S+) '
    r'ISSUE:\S+ PR:([1-9]\d*|UNKNOWN)(?=\s|$)')
BRIEF_PR = re.compile(r'^[ \t]*PR[ \t]*:[ \t]*#?([1-9]\d*)\b', re.I | re.M)
FOOTER = 'PM: process with Skill(hourly-per-pr-processing) in this thread.'
GH_ERRORS = (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError)
PROXY_STAGE = {'pm-admission-reviewer': '12b', 'hd-proof-qa': 'QA', 'pm-proof-qa': 'QA',
               'hd-builder': 'build/rework', 'pm-hotfix': 'rework'}

_REVIEW_GATE_SPEC = importlib.util.spec_from_file_location(
    'pm_admit_12b_report', Path(__file__).resolve().parents[2] / 'ci/pm-admit-12b.py')
_REVIEW_GATE = importlib.util.module_from_spec(_REVIEW_GATE_SPEC)
_REVIEW_GATE_SPEC.loader.exec_module(_REVIEW_GATE)
_CAP_SPEC = importlib.util.spec_from_file_location(
    'proxy_review_cap_report', Path(__file__).resolve().parents[2] / 'proxy-review-cap.py')
_CAP = importlib.util.module_from_spec(_CAP_SPEC)
_CAP_SPEC.loader.exec_module(_CAP)

try:  # exact-head copy-only receipts (#13451); a load failure keeps full CI/E2E required, never crashes the report
    _COPY_ONLY_SPEC = importlib.util.spec_from_file_location(
        'copy_only_evidence_report', Path(__file__).resolve().parents[2] / 'ci/copy_only_evidence.py')
    _COPY_ONLY = importlib.util.module_from_spec(_COPY_ONLY_SPEC)
    _COPY_ONLY_SPEC.loader.exec_module(_COPY_ONLY)
except Exception:
    _COPY_ONLY = None


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


def lane_binding(pid, name, log_dir, run_dir):
    base = run_dir / name
    try:
        manifest = json.loads((base / 'manifest.json').read_text())
    except (OSError, ValueError):
        manifest = {}
    if not isinstance(manifest, dict) or manifest.get('name') != name:
        manifest = {}
    if manifest and manifest.get('state') not in ('queued', 'running'):
        return None  # never resurrect a completed manifest from the logs
    if str(manifest.get('pid')) == pid:
        pr = str(manifest.get('pr', ''))
        if not re.fullmatch(r'[1-9]\d*', pr):
            for path in (base / 'active/original-brief.txt', Path(manifest.get('brief') or '/dev/null')):
                try:
                    with path.open(encoding='utf-8', errors='replace') as handle:
                        refs = set(BRIEF_PR.findall(handle.read(200000)))
                except OSError:
                    continue
                if len(refs) == 1:
                    pr = refs.pop()
                break
        if re.fullmatch(r'[1-9]\d*', pr):
            return manifest.get('started_at', ''), int(pr), manifest.get('type', '')

    # Older lanes and same-name continuation PIDs can still be tied to the
    # launch header, never to an incidental PR mention or digits in the name.
    out = manifest.get('out')
    paths = ([Path(out).with_suffix('.log')] if out else
             [log_dir / f'{name}.log', *log_dir.glob(f'*-{name}-*-????????T??????Z.log')])
    launches = []
    for path in paths:
        try:
            with path.open(encoding='utf-8', errors='replace') as handle:
                launch = PROXY_LAUNCH.match(handle.readline(2048))
        except OSError:
            continue
        if launch and launch[2] == name:
            role = manifest.get('type') or path.name.partition(f'-{name}-')[0]
            if path.name == f'{name}.log':
                role = re.sub(r'-\d{8}T\d{6}Z-\d+$', '', name.removeprefix('proxy-'))
            launches.append((launch[1], launch[3], role))
    if launches:
        started_at, pr, role = max(launches)
        if pr != 'UNKNOWN':
            return started_at, int(pr), role
    return None


def live_proxy_lanes(log_dir, run_dir=None):
    """One process snapshot; only a live Claude process can own an explicit PR."""
    try:
        result = subprocess.run(['pgrep', '-fl', 'claude -p --name '],
                                capture_output=True, encoding='utf-8', errors='replace', timeout=5)
        if result.returncode == 1:  # no matches
            return {}
        result.check_returncode()
    except (OSError, subprocess.SubprocessError) as exc:
        print(f'WARN proxy liveness lookup failed: {exc}', file=sys.stderr)
        return {}

    owners = {}
    for line in result.stdout.splitlines():
        process = PROXY_PROCESS.match(line)
        if not process:
            continue
        pid, name = process.groups()
        binding = lane_binding(pid, name, log_dir, run_dir or PROXY_RUN_DIR)
        if binding:
            started_at, pr, role = binding
            entry = (stamp(started_at) or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
                     name, PROXY_STAGE.get(role, role))
            owners[pr] = max(entry, owners.get(pr, entry))
    return {pr: (name, stage) for pr, (_, name, stage) in owners.items()}


def mergeability(pr):
    value = pr.get('mergeable')
    return value if value in MERGEABLE_OK or value == 'CONFLICTING' else None


def fetch_pr(item):
    number = str(item['number'])
    for attempt in range(2):
        try:
            pr = gh('pr', 'view', number, '--repo', REPO, '--json',
                    'number,isDraft,headRefOid,headRefName,labels,mergeable,reviewDecision')
            break
        except GH_ERRORS as exc:
            print(f'WARN pr view #{number} failed (attempt {attempt + 1}): {exc}', file=sys.stderr)
            if attempt == 0:
                time.sleep(3)
    else:
        pr = {'number': item['number'], 'isDraft': bool(item.get('draft')),
              'headRefOid': item['head']['sha'], 'headRefName': item['head']['ref'],
              'labels': [{'name': x['name']} for x in item.get('labels', [])]}
    if pr.get('mergeable') == 'UNKNOWN':
        try:
            retry = gh('pr', 'view', number, '--repo', REPO, '--json', 'headRefOid,mergeable')
            if retry['headRefOid'] == pr['headRefOid'] and mergeability(retry):
                pr['mergeable'] = retry['mergeable']
        except GH_ERRORS as exc:
            print(f'WARN mergeability recheck #{number}: {exc}', file=sys.stderr)
    if not mergeability(pr):
        pr.pop('mergeable', None)
    pr['createdAt'] = item.get('created_at')
    return pr


def fetch_comments(number):
    try:
        # REST IDs match the cap ledger; paginate rather than trusting a slice of GitHub history.
        return [c for page in gh('api', '--paginate', '--slurp',
                f'repos/{REPO}/issues/{number}/comments?per_page=100') for c in page]
    except GH_ERRORS as exc:
        print(f'WARN PR comments #{number}: {exc}', file=sys.stderr)
        return None


def fetch_draft_tip(head):
    try:
        return gh('api', f'repos/{REPO}/commits/{head}')['commit']['committer']['date']
    except GH_ERRORS as exc:
        print(f'WARN draft tip {head[:12]}: {exc}', file=sys.stderr)
        return None


def collect():
    # REST pagination includes drafts and has no fixed PR-count cutoff.
    pages = gh('api', '--paginate', '--slurp', f'repos/{REPO}/pulls?state=open&per_page=100')
    prs, checks = [], {}
    for page in pages:
        for item in page:
            pr = fetch_pr(item)
            pr['comments'] = fetch_comments(pr['number'])
            prs.append(pr)
            # Check-runs endpoint is pinned to the SHA, not branch/latest workflow.
            checks[pr['number']] = fetch_checks(pr['headRefOid'])
    with urllib.request.urlopen('http://127.0.0.1:3100/slots', timeout=10) as response:
        slots = json.load(response)['slots']
    with sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=5) as con:
        con.row_factory = sqlite3.Row
        obligations = [dict(row) for row in con.execute(
            "SELECT id, pr, target_type, target_id, owner, suppress_until, kind, evidence_json FROM obligations "
            "WHERE status='open' AND (target_type='github_pr' OR "
            "kind IN ('pr_red_unowned','pr_admission','pr_merge_ask','merge_hold')) ORDER BY id DESC")]
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
    proxies = live_proxy_lanes(PROXY_LOG_DIR)
    for pr in prs:
        if pr['number'] in proxies:
            pr['live_proxy'] = proxies[pr['number']]
        elif pr['isDraft']:
            pr['tip_at'] = fetch_draft_tip(pr['headRefOid'])
    annotate_review_caps(prs)
    return prs, checks, slots, obligations


def annotate_review_caps(prs):
    ledger = HOME / '.claude/state/proxy-review-cap.sqlite3'
    try:
        saved = {}
        if ledger.exists():
            with sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True, timeout=5) as con:
                saved = {(number, stage): (json.loads(receipts), json.loads(reserved))
                         for number, stage, receipts, reserved in con.execute(
                             'SELECT pr, stage, receipts, reservations FROM rounds')}
        for pr in prs:
            comments = pr.get('comments')
            if comments is None:
                continue  # incomplete history is not a cap verdict
            stage = 'qa' if pr['isDraft'] else '12b'
            head = pr['headRefOid']
            if stage == '12b':
                markers = pm_markers(comments)
                if f'ci-head:{head}' in {label['name'] for label in pr['labels']} or (
                        markers and markers[-1][0] == 'PASS' and head.lower().startswith(markers[-1][1].lower())):
                    continue
            if stage == 'qa' and qa_verdict(pr, head) == 'PASS':
                continue
            receipts, reserved = saved.get((pr['number'], stage), ({}, []))
            current = _CAP.read_receipts(stage, [comments])
            if _CAP.round_counts(stage, {**receipts, **current}, reserved)[0] >= 2:
                pr['review_cap'] = stage
    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
        print(f'WARN review-cap lookup failed: {exc}', file=sys.stderr)


def latest_check_runs(runs, head):
    latest = {}
    for run in runs:
        if run.get('head_sha') != head or run.get('name') not in ('test', 'e2e'):
            continue
        name = run['name']
        if run.get('id', 0) > latest.get(name, {}).get('id', -1):
            latest[name] = run
    return latest


def latest_checks(runs, head):
    return {name: ((run.get('conclusion') or '').upper() if run.get('status') == 'completed'
                   else 'PENDING') for name, run in latest_check_runs(runs, head).items()}


def claim_evidence(row):
    try:
        value = json.loads(row.get('evidence_json') or '{}')
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


MERGEABLE_OK = {'MERGEABLE', 'CLEAN'}
PM_12B_PASS_RE = re.compile(r'PM[ _-]?12b|PM_OPUS_REVIEW:\s*PASS', re.IGNORECASE)
PM_12B_BLOCK_RE = re.compile(r'PM_OPUS_REVIEW:\s*(BLOCK|REVISE|REQUEST_CHANGES)|PM[ _-]?12b[^\n]{0,40}\b(BLOCK|REVISE)\b', re.IGNORECASE)


def latest_12b_verdict(pr, head):
    """Latest PM 12b verdict explicitly bound to the current head."""
    verdict = None
    for comment in pr.get('comments') or []:
        body = comment.get('body') or ''
        if not head or head[:7] not in body:
            continue
        if PM_12B_BLOCK_RE.search(body):
            verdict = 'BLOCK'
        elif PM_12B_PASS_RE.search(body):
            verdict = 'PASS'
    return verdict


def hold_reason(pr, obligations):
    """Shared hold check: pm-blocked:*, pm-state:blocked-*, or an open pr_admission 'HELD' obligation."""
    labels = [item['name'] for item in pr.get('labels', [])]
    held = sorted(x for x in labels if x.startswith('pm-blocked:') or x.startswith('pm-state:blocked-'))
    if held:
        return ','.join(held)
    for row in obligations or []:
        target_pr = row.get('pr')
        if target_pr is None and row.get('target_type') == 'github_pr':
            target_pr = row.get('target_id')
        if str(target_pr) != str(pr['number']):
            continue
        if row.get('kind') == 'merge_hold':
            return f'merge_hold obligation {row.get("id", "open")}'
        if row.get('kind') != 'pr_admission':
            continue
        raw = row.get('evidence_json') or ''
        texts = [raw] + list(claim_evidence(row).keys())
        for text in texts:
            text = text.lstrip('{"\' ')
            if text.startswith('HELD'):
                return re.sub(r'\s+', ' ', text[4:].lstrip(':- ').split('(')[0].split(';')[0]).strip()[:80]
    return None


def e2e_exemption(number, head):
    """(e2e_required, reason) from an exact-head receipt (ci/copy_only_evidence.py, #13451).

    The pinned origin/main classifier plus its copy-only content check run at this exact head; a path-only
    classification never grants copy-only. Missing, stale, malformed or unavailable evidence, or a missing
    helper, keeps full CI/E2E required (fail closed), with the reason shown in the report line.
    """
    if _COPY_ONLY is None:
        return True, 'full-required: copy-only receipt helper unavailable'
    try:
        return _COPY_ONLY.e2e_required_for(number, head, repo=REPO)
    except Exception as exc:  # the helper never raises; this guards an unexpected defect
        return True, f'full-required: receipt check failed ({type(exc).__name__})'


def classifier_e2e_required(number, head):
    """Bool view of e2e_exemption(); fail closed to True."""
    return e2e_exemption(number, head)[0]


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
    e2e_reason = None
    if ci == 'SUCCESS' and e2e in ('MISSING', 'SKIPPED', 'NEUTRAL'):
        if e2e_required is None:
            e2e_required, e2e_reason = e2e_exemption(number, head)
        if not e2e_required:
            e2e = 'NOT_REQUIRED'
    hold = hold_reason(pr, obligations)
    if not hold and latest_12b_verdict(pr, head) == 'BLOCK':
        hold = 'latest-head PM 12b BLOCK'
    mergeable = mergeability(pr)
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
            action = 'resolve conflicts' if mergeable == 'CONFLICTING' else 'check mergeability'
    else:
        gate, action = 'CI running', 'await terminal' if 'PENDING' in (ci, e2e) else 'reconcile missing gates'
    if mergeable == 'CONFLICTING' and not action.startswith('resolve conflicts'):
        action = 'resolve conflicts; ' + action
    return gate, action, {'ci': ci, 'e2e': e2e, 'admitted': admitted, 'e2e_reason': e2e_reason}


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


def comment_time(comment):
    return comment.get('created_at') or comment.get('createdAt') or ''


def qa_markers(comment):
    lines = (comment.get('body') or '').rstrip().splitlines()
    if not lines:
        return ()
    return tuple(line for line in dict.fromkeys((lines[0], lines[-1]))
                 if _CAP.QA.fullmatch(line) or _CAP.QA_PASS.fullmatch(line))


def qa_verdict(pr, head):
    for comment in sorted(pr.get('comments') or [], key=comment_time, reverse=True):
        for marker in reversed(qa_markers(comment)):
            sha = re.search(r'(?:at |head=)([a-fA-F0-9]{7,40})\b', marker)
            if sha and head.lower().startswith(sha[1].lower()):
                return 'PASS' if re.search(r'\bPASS\b|^DONE:', marker) else 'REVISE'
    return None


def pm_markers(comments):
    shaped = [dict(c, created_at=comment_time(c), id=c.get('id', i)) for i, c in enumerate(comments)]
    return [(verdict, sha) for verdict, sha, canonical, _ in
            _REVIEW_GATE.canonical_markers(shaped, None) if canonical]


def report_annotations(pr):
    proxy = pr.get('live_proxy')
    owner = ('owner=' + ' · '.join(filter(None, proxy))) if proxy else None
    markers = pm_markers(pr.get('comments') or [])
    if not markers:
        return owner, ''
    verdict, sha = markers[-1]
    head_note = '' if pr['headRefOid'].lower().startswith(sha.lower()) else ' (different head)'
    return owner, f' · PM 12b: {verdict} at {sha[:12]}{head_note}'


def review_snapshot(pr):
    markers = []
    for comment in pr.get('comments') or []:
        pm = pm_markers([comment])
        qa = qa_markers(comment)
        if pm or qa:
            markers.append([comment.get('id') or comment_time(comment), [list(m) for m in pm], list(qa)])
    return markers


def has_merge_ask(pr, obligations):
    return pr.get('merge_ask_sent', False) or any(
        str(row.get('pr')) == str(pr['number']) and row.get('kind') == 'pr_merge_ask'
        and claim_evidence(row).get('head') == pr['headRefOid'] for row in obligations)


def draft_idle(pr, since_head, now):
    tip = stamp(pr.get('tip_at'))
    if not tip or pr.get('comments') is None:
        return False  # cannot prove idle when a source failed
    activity = [tip, stamp(pr.get('createdAt')), stamp(since_head)]
    activity.extend(stamp(comment_time(c)) for c in pr.get('comments') or [])
    return now - max(d for d in activity if d) > dt.timedelta(hours=24)


def classify(pr, runs, slots, obligations, prior, now):
    number, head = pr['number'], pr['headRefOid']
    labels = [item['name'] for item in pr['labels']]
    admitted = f'ci-head:{head}' in labels
    admission = 'matches' if admitted else ('stale' if any(x.startswith('ci-head:') for x in labels) else 'missing')
    previous = prior.get(str(number), {})
    blocked = hold_reason(pr, obligations)
    if runs is None:
        # An unavailable provider cannot reset the known gate age or invent a red.
        gate = previous.get('gate') if previous.get('head') == head else None
        action, g = 'check GitHub API', {'ci': None, 'e2e': None}
    else:
        gate, action, g = merge_gate(pr, runs, obligations)
    ci, e2e = g['ci'], g['e2e']
    proxy_owner, review = report_annotations(pr)
    owner = proxy_owner
    if owner is None:
        for slot in slots:
            # Checkout branches survive release/reassignment; only MoP's current,
            # occupied PR+head assignment is authority, never leftover git state.
            if slot.get('occupied') is True and slot.get('pr') == number and slot.get('head_sha') == head:
                owner = f'S{slot["slot"]}'
                break
    active_owner = owner is not None
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
    merge_ask = has_merge_ask(pr, obligations)
    if gate == 'dual-green/merge ask' and merge_ask:
        owner, action = owner or 'CTO (merge ask sent)', 'await merge decision'
    since = stamp(previous.get('since')) if previous.get('gate') == gate else None
    since = since or now
    moved_head = previous.get('head') and previous['head'] != head
    since_head = now.isoformat() if moved_head else previous.get('since_head')
    idle = pr['isDraft'] and not proxy_owner and draft_idle(pr, since_head, now)
    stuck = (not active_owner and not blocked and not pr['isDraft'] and gate is not None
             and now - since > dt.timedelta(hours=4))
    red = (ci in RED or e2e in RED) and owner is None
    green = (ci == 'SUCCESS' and e2e in ('SUCCESS', 'NOT_REQUIRED') and not merge_ask
             and not pr['isDraft'] and gate != 'held')
    cap = pr.get('review_cap')
    conflict = mergeability(pr) == 'CONFLICTING' and not active_owner and not blocked
    actionable = stuck or idle or red or green or bool(cap) or conflict

    if proxy_owner:
        movement = 'OWNED'
    elif blocked:
        movement = 'PARKED'
    elif active_owner:
        movement = 'OWNED'
    elif stuck or idle or red:
        movement = 'STUCK'
    elif ci_wait:
        latest = latest_check_runs(runs or [], head)
        movement = ('QUEUED' if any(r.get('status') in ('queued', 'waiting', 'requested', 'pending')
                                    for r in latest.values()) else 'WAITING')
    elif owner:
        movement = 'WAITING'
    else:
        movement = 'MOVING' if moved_head else 'OPEN'

    details = [f'ci-head {admission}']
    if runs is None:
        details.append('checks unavailable (GitHub API error)')
    else:
        e2e_reason = f' ({g["e2e_reason"]})' if g.get('e2e_reason') else ''
        details.extend((f'CI {ci}', f'E2E {e2e}{e2e_reason}'))
    if mergeability(pr):
        details.append(f'mergeable {mergeability(pr)}')
    if cap:
        action = f'{cap} review cap reached; {action}'
    if idle:
        action = f'draft idle >24h; {action}'
    safe_owner = re.sub(r'[\r\n]+', ' ', owner or 'unowned')
    line = (f'#{number} {"draft" if pr["isDraft"] else "ready"} · {gate or "checks unavailable"} '
            f'[{"; ".join(details)}] · {safe_owner}{review} · {action} · {movement}')
    return line, {'gate': gate, 'since': since.isoformat(), 'head': head, 'since_head': since_head}, actionable


def observation(pr, runs, obligations, old):
    head = pr['headRefOid']
    current = {'head': head, 'draft': pr['isDraft'], 'labels': sorted(x['name'] for x in pr['labels']),
               'hold': hold_reason(pr, obligations), 'merge_ask': has_merge_ask(pr, obligations)}
    if pr.get('comments') is not None:
        current['review'] = review_snapshot(pr)
    if 'reviewDecision' in pr:
        current['review_decision'] = pr['reviewDecision']
    checks = dict(old.get('terminal_checks', {})) if old.get('head') == head else {}
    # Ignore nonterminal polling churn. A rerun changes the snapshot once its
    # terminal result is observed, even if the conclusion is unchanged.
    for name, check in latest_check_runs(runs or [], head).items():
        if check.get('status') == 'completed':
            checks[name] = [check.get('id'), check.get('conclusion')]
    if runs is not None or checks:
        current['terminal_checks'] = checks
    return current


def render(prs, checks, slots, obligations, prior, now, *, previous=None, merged=()):
    lines, state, failed, hidden = [], {}, 0, 0
    previous = prior if previous is None else previous
    for pr in sorted(prs, key=lambda item: item['number']):
        key = str(pr['number'])
        runs = checks.get(pr['number'], [])
        failed += runs is None
        line, entry, actionable = classify(pr, runs, slots, obligations, prior, now)
        last = previous.get(key, {})
        old = last.get('observation', {})
        observed = observation(pr, runs, obligations, old)
        # Older installations stored only gate/head. Seed the new fields without
        # listing every existing PR, unless its known head or gate actually moved.
        changed = (not last or last.get('head') not in (None, entry['head']) or
                   (not old and entry['gate'] is not None and last.get('gate') != entry['gate']) or
                   any((k not in old or old[k] != v) for k, v in observed.items() if old))
        state[key] = {**entry, 'observation': {**old, **observed}}
        if changed or actionable:
            lines.append(line)
        else:
            hidden += 1
    for number, merged_at in merged:
        lines.append(f'#{number} merged · {merged_at}')
    if prs and failed * 2 > len(prs):
        lines.insert(0, f'GitHub API degraded: check-runs unavailable for {failed}/{len(prs)} PRs')
    if hidden:
        lines.append(f'{hidden} unchanged and owned')
    return lines or ['No open PRs.'], state


def disappeared_prs(previous, prs):
    """Verify closures of previously seen PRs; absence from the list is not a merge."""
    merged, unresolved = [], {}
    absent = previous.keys() - {str(pr['number']) for pr in prs}
    for number in sorted(absent, key=int):
        try:
            closed = gh('pr', 'view', number, '--repo', REPO, '--json', 'state,mergedAt')
            if closed['state'] == 'MERGED' and closed.get('mergedAt'):
                merged.append((number, closed['mergedAt']))
            elif closed['state'] != 'CLOSED':
                unresolved[number] = previous[number]
        except GH_ERRORS as exc:
            print(f'WARN disappeared PR #{number}: {exc}', file=sys.stderr)
            unresolved[number] = previous[number]
    return merged, unresolved


def chunks(lines, limit=35000):
    # Slack hard limit is 40k; split on complete lines and print the footer only once.
    title = '*Hourly per-PR status (continued)*\n\n'
    out, text, count = [], '*Hourly per-PR status*\n\n', 0
    for line in [*lines, FOOTER]:
        if len(title) + len(line) + 2 > limit:
            raise ValueError('single report line exceeds Slack limit')
        if len(text) + len(line) + 2 > limit or count >= 40:
            out.append(text)
            text, count = title, 0
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


def preview(saved, now):
    prs, checks, slots, obligations = collect()
    gates = saved.get('gates', {})
    previous = saved.get('previous', gates)
    merged, unresolved = disappeared_prs(previous, prs)
    lines, ages = render(prs, checks, slots, obligations, gates, now, previous=previous, merged=merged)
    return lines, ages, {**unresolved, **ages}


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--thread')
    mode.add_argument('--dry-run', action='store_true', help='print the exact Slack body; never post or save ages')
    parser.add_argument('--channel', default='C0ALZJHGE49')
    args = parser.parse_args()
    if not args.thread:
        saved = json.loads(STATE.read_text()) if STATE.exists() else {}
        lines, _, _ = preview(saved, dt.datetime.now(dt.timezone.utc))
        print((''.join(chunks(lines)).rstrip()) if args.dry_run else '\n'.join(lines))
        return
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with STATE.with_suffix('.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        saved = json.loads(STATE.read_text()) if STATE.exists() else {}
        key = f'{args.channel}:{args.thread}'
        delivered = saved.setdefault('delivered', {})
        if args.thread and delivered.get(key, {}).get('done'):
            print('ALREADY_POSTED ' + key)
            return
        receipt = delivered.get(key)
        if receipt and (receipt.get('attempt') is not None or
                (len(receipt['parts']) < len(receipt['messages'])
                 and receipt.get('delivery_version') != 1)):
            # Older receipts cannot prove the next part was never accepted.
            # Keep confirmed parts and stop before even recollecting providers.
            raise RuntimeError('UNCERTAIN Slack part; reconcile the existing thread before retrying: '
                               + key + f'; confirmed_parts={len(receipt["parts"])}')
        now = dt.datetime.now(dt.timezone.utc)
        if receipt is None:
            # Preserve the prior hourly independently of gate ages (which are
            # saved even if delivery fails). Old receipts resume as written.
            saved.setdefault('previous', saved.get('gates', {}))
            lines, ages, snapshot = preview(saved, now)
            saved['gates'] = ages
            receipt = {'parts': [], 'messages': chunks(lines), 'delivery_version': 1,
                       'snapshot': snapshot, 'snapshot_at': now.isoformat()}
            delivered[key] = receipt

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
        for index in range(len(receipt['parts']), len(receipt['messages'])):
            # Save intent durably BEFORE sending. A lost response or crash must
            # never turn an accepted send into a duplicate on the next run.
            receipt['attempt'] = {'index': index, 'started_at': now.isoformat()}
            save()
            receipt['parts'].append(post(args.channel, args.thread, receipt['messages'][index]))
            receipt.pop('attempt')
            save()
        receipt['done'] = True
        snapshot = receipt.pop('snapshot', None)
        snapshot_at = receipt.pop('snapshot_at', '')
        if snapshot is not None and snapshot_at > saved.get('previous_at', ''):
            saved.update(previous=snapshot, previous_at=snapshot_at)
        save()


if __name__ == '__main__':
    main()
