#!/usr/bin/env python3
"""Deterministic hourly per-PR report; no GitHub or ownership mutations.

Posting is called by slack-send only after an Alerts Hourly parent succeeds.
Gate ages are persisted for the same PR head and gate, independently of ownership.
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
import stat
import subprocess
import sys
import threading
import time
import urllib.request

HOME = Path.home()
STATE = HOME / '.claude/state/pr-status-report.json'
DB = Path(os.environ.get('PM_OPS_DB', str(
    HOME / '.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db')))
REPO = 'heydonna-app/heydonna-app'
RED = {'FAILURE', 'ERROR', 'CANCELLED', 'TIMED_OUT', 'ACTION_REQUIRED', 'STARTUP_FAILURE'}
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

# Rajiv C0ALZJHGE49 thread 1795581894.679329, ts 1795584256.843699: "also add the ci/e2e rounds for each pr in the hourly pr status report".
# CTO REVISE (C0ALZJHGE49 thread 1795586195.864359, ts 1795586272.918649): a round is one CI/E2E ADMISSION (the pm-admit-ci
# `ci-head:<sha>` label event; one admission spawns a ci.yml and an e2e.yml run but is ONE round), the 300 s budget is
# enforced on every GitHub read, and the row shows only the count (no merge-eligibility prompt of any kind).
ROUNDS_BUDGET_S = 300     # one report's total time for rounds lookups; unfinished PRs show "rounds ?"
ROUNDS_WORKERS = 4        # PRs looked up in parallel; the budget is shared
ADMISSION_LABEL = re.compile(r'^ci-head:[0-9a-f]{40}$')  # same shape as pm-admit-cap.HEAD_LABEL


def stamp(value):
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.replace(tzinfo=parsed.tzinfo or dt.timezone.utc)
    except ValueError:
        return None


def gh(*args, timeout=120):
    result = subprocess.run(['gh', *args], capture_output=True, text=True, timeout=timeout, check=True)
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


def process_identity(pid):
    row = subprocess.run(['ps', '-ww', '-p', pid, '-o', 'ppid=,lstart=,command='],
                         capture_output=True, text=True, timeout=5, check=True,
                         env={**os.environ, 'LC_ALL': 'C', 'TZ': 'UTC'}).stdout.split(maxsplit=6)
    return (row[0], dt.datetime.strptime(' '.join(row[1:6]), '%a %b %d %H:%M:%S %Y')
            .replace(tzinfo=dt.timezone.utc), row[6])


def live_launch_header(pid, name, identity):
    # The runner redirects fd 2 to the same launch log for the initial child
    # and every continuation. Name-matched files cannot prove that relationship.
    parent, started, _ = identity
    _, runner_started, runner = process_identity(parent)
    if not re.fullmatch(r'(?:\S*/)?bash\s+.+/proxy-agent-job\.[^/\s]+/run\.sh', runner):
        return None
    opened = subprocess.run(['lsof', '-nP', '-a', '-p', pid, '-d', '2', '-FftDin'],
                            capture_output=True, text=True, timeout=5, check=True).stdout
    fields = {line[0]: line[1:] for line in opened.splitlines() if line}
    if fields.get('p') != pid or fields.get('f') != '2' or fields.get('t') != 'REG':
        return None
    path = Path(fields['n'])
    if not path.is_absolute() or path.suffix != '.log':
        return None
    with open(path, encoding='utf-8', errors='replace',
              opener=lambda p, flags: os.open(p, flags | os.O_NONBLOCK)) as handle:
        file = os.fstat(handle.fileno())
        if (not stat.S_ISREG(file.st_mode) or
                (file.st_dev, file.st_ino) != (int(fields['D'], 16), int(fields['i']))):
            return None
        launch = PROXY_LAUNCH.match(handle.readline(2048))
    if (launch and launch[2] == name and launch[3] != 'UNKNOWN'
            and runner_started <= stamp(launch[1]) <= started and process_identity(pid) == identity):
        return launch, path
    return None


def lane_binding(pid, name, run_dir):
    base = run_dir / name
    try:
        manifest = json.loads((base / 'manifest.json').read_text())
    except (OSError, ValueError):
        manifest = {}
    if not isinstance(manifest, dict) or manifest.get('name') != name:
        manifest = {}
    if manifest and manifest.get('state') not in ('queued', 'running'):
        return None  # never resurrect a completed manifest from the logs
    try:
        identity = process_identity(pid)
        _, started, command = identity
        process = PROXY_PROCESS.match(f'{pid} {command}')
        if not process or process.groups() != (pid, name):
            return None
        manifest_started = stamp(manifest.get('started_at'))
        confirmed = stamp(manifest.get('running_at'))
        if (str(manifest.get('pid')) == pid and manifest_started and confirmed
                and manifest_started.replace(microsecond=0) <= started <= confirmed):
            pr = str(manifest.get('pr', ''))
        else:
            pr = None
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None
    if pr is not None:
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

    try:
        bound = live_launch_header(pid, name, identity)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError, KeyError):
        return None
    if not bound:
        return None
    launch, path = bound
    role, separator, _ = path.name.partition(f'-{name}-')
    role = role if separator else ''
    if path.name == f'{name}.log':
        role = re.sub(r'-\d{8}T\d{6}Z-\d+$', '', name.removeprefix('proxy-'))
    out = manifest.get('out')
    if (isinstance(out, str) and Path(out).name.removesuffix('.md') + '.log' == path.name
            and manifest_started and manifest_started.replace(microsecond=0) <= stamp(launch[1])):
        role = manifest.get('type') or role
    return launch[1], int(launch[3]), role


def live_proxy_lanes(run_dir=None):
    """Only a live, launch-bound Claude process can own an explicit PR."""
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
        binding = lane_binding(pid, name, run_dir or PROXY_RUN_DIR)
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


def count_admissions(events):
    """CI/E2E rounds = distinct admissions: every `labeled` issue event whose label is `ci-head:<40-hex>` (what
    pm-admit-ci.sh writes). A re-admission of the same sha removes and re-adds the label, so it is a fresh event and a
    fresh round; one admission that spawns both a ci.yml and an e2e.yml run is still ONE round. Reruns of an existing
    run, pull_request-triggered runs and every other label are not admissions."""
    return sum(1 for event in events
               if event.get('event') == 'labeled'
               and ADMISSION_LABEL.fullmatch(str((event.get('label') or {}).get('name') or '')))


def fetch_rounds(pr, deadline):
    """Admission rounds for one open PR: {'rounds': int}, or None when the lookup cannot finish inside the shared
    budget (the row then shows "rounds ?", never 0). The remaining budget is the timeout of the GitHub read itself, so
    a stalled `gh` is killed at the deadline instead of being waited for. A pending head that has been admitted but not
    yet tested counts like any other; a PR with no admission yet is a true 0."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    try:
        pages = gh('api', '--paginate', '--slurp', f'repos/{REPO}/issues/{pr["number"]}/events?per_page=100',
                   timeout=remaining)
        return {'rounds': count_admissions(event for page in pages for event in (page or []))}
    except GH_ERRORS as exc:
        print(f'WARN rounds #{pr["number"]}: {type(exc).__name__}: {exc}', file=sys.stderr)
        return None


def annotate_rounds(prs, budget=None, lookup=None):
    """Fill pr['rounds'] for every PR within ONE wall-clock budget. Workers are daemon threads and the composer joins
    them only until the deadline, so a lookup that ignores its own timeout still cannot hold the report past the
    budget: every PR without a finished result renders "rounds ?"."""
    budget = ROUNDS_BUDGET_S if budget is None else budget
    lookup = lookup or fetch_rounds
    deadline = time.monotonic() + budget
    pending, results, lock = list(prs), {}, threading.Lock()

    def worker():
        while time.monotonic() < deadline:
            with lock:
                if not pending:
                    return
                pr = pending.pop(0)
            try:
                result = lookup(pr, deadline)
            except Exception as exc:  # a lookup bug must never take the whole report down: this row shows "rounds ?"
                print(f'WARN rounds #{pr.get("number")}: {type(exc).__name__}: {exc}', file=sys.stderr)
                result = None
            with lock:
                results[pr['number']] = result

    threads = [threading.Thread(target=worker, daemon=True, name=f'rounds-{i}')
               for i in range(max(1, min(ROUNDS_WORKERS, len(pending))))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    with lock:  # snapshot: a straggler finishing after the deadline cannot change what this report renders
        finished = dict(results)
    for pr in prs:
        pr['rounds'] = finished.get(pr['number'])


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
    proxies = live_proxy_lanes()
    for pr in prs:
        if pr['number'] in proxies:
            pr['live_proxy'] = proxies[pr['number']]
        elif pr['isDraft']:
            pr['tip_at'] = fetch_draft_tip(pr['headRefOid'])
    annotate_review_caps(prs)
    annotate_rounds(prs)
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
    awaiting_merge = gate == 'dual-green/merge ask' and merge_ask
    if awaiting_merge:
        owner, action = owner or 'CTO (merge ask sent)', 'await merge decision'
    same_head_and_gate = previous.get('head') == head and previous.get('gate') == gate
    since = stamp(previous.get('since')) if same_head_and_gate else None
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
    needs_owner = not (active_owner or blocked or gate == 'held' or ci_wait or awaiting_merge)
    rounds = pr.get('rounds')  # None = lookup unfinished: shown as "rounds ?", never 0. Display only; it gates nothing.
    actionable = stuck or idle or red or green or bool(cap) or conflict or needs_owner

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
    details.append(f'rounds {rounds["rounds"] if rounds else "?"}')
    if cap:
        action = f'{cap} review cap reached; {action}'
    if idle:
        action = f'draft idle >24h; {action}'
    safe_owner = re.sub(r'[\r\n]+', ' ', owner or 'unowned')
    line = (f'#{number} {"draft" if pr["isDraft"] else "ready"} · {gate or "checks unavailable"} '
            f'[{"; ".join(details)}] · {safe_owner}{review} · {action} · {movement}')
    return line, {'gate': gate, 'since': since.isoformat(), 'head': head, 'since_head': since_head}, actionable, active_owner


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
    lines, state, failed, hidden_owned = [], {}, 0, 0
    previous = prior if previous is None else previous
    for pr in sorted(prs, key=lambda item: item['number']):
        key = str(pr['number'])
        runs = checks.get(pr['number'], [])
        failed += runs is None
        line, entry, actionable, active_owner = classify(pr, runs, slots, obligations, prior, now)
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
        elif active_owner:
            hidden_owned += 1
    for number, merged_at in merged:
        lines.append(f'#{number} merged · {merged_at}')
    if prs and failed * 2 > len(prs):
        lines.insert(0, f'GitHub API degraded: check-runs unavailable for {failed}/{len(prs)} PRs')
    if hidden_owned:
        lines.append(f'{hidden_owned} unchanged and owned')
    return lines or ['No PR changes or actions.' if prs else 'No open PRs.'], state


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
