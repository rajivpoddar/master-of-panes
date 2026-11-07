#!/usr/bin/env python3
"""Read-only hourly issue lifecycle findings; GitHub/MoP retain routine tracking.

Uses the existing Unblockable now section. Never changes labels or invents ACs.
The first run backfills partial merges for 24h; all open issue timelines are
checked regardless of age. A failed collection never advances the cursor.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import urllib.request

REPO = 'Scribie/heydonna-app'
STATE = Path.home() / '.claude/state/issue-lifecycle.json'
PM_OPS = Path.home() / '.claude/scripts/pm-ops.py'
HEADER = '*Unblockable now / issue lifecycle:*'
PARTIAL = re.compile(r'\b(?:part\s+of|refs?)\s+((?:#\d+[ \t,;]*(?:and\s+)?)+)', re.I)
CLOSES = re.compile(r'\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)\s+((?:#\d+[ \t,;]*(?:and\s+)?)+)', re.I)
# Only dependency-gated issues can be auto-detected as unblocked; gate/infra/
# product/external/cto blocks have wake conditions the collector cannot see.
DEPENDENCY = 'pm-blocked:dependency'
NON_DEPENDENCY = {'pm-blocked:pm-gate', 'pm-blocked:infra', 'pm-blocked:product',
                  'pm-blocked:external', 'pm-blocked:cto'}
# Reasons a live read can prove cleared. partial_merge comes from the rolling
# merged-PR window, so its absence in a later run proves nothing: never auto-resolved.
LIVE_CHECKABLE = {'unowned_lifecycle', 'done_open', 'merged_closes_open', 'blockers_resolved'}
WINDOW_ONLY = {'partial_merge'}
LIFECYCLE_LABELS = {'status:in-review', 'status:in-progress'}
RECHECK_CAP = 50
BLOCKERS = re.compile(r'(?im)^[^\n]*?\b(?:blockers?\**\s*:|blocked\s+(?:by|on)|depends\s+on|after|waits?\s+on|until|unblock[_ ]condition\s*[:=])\**\s+([^\n]+)')
SOURCE = '''nodes { ... on CrossReferencedEvent { source { ... on PullRequest {
 number state body title headRefName mergedAt
 closingIssuesReferences(first:100) { nodes { number } pageInfo { hasNextPage } }
} } } } pageInfo { hasNextPage endCursor }'''


def gh(*args):
    proc = subprocess.run(['gh', *args], capture_output=True, text=True, timeout=120, check=True)
    return json.loads(proc.stdout)


def graphql(query, variables):
    argv = ['api', 'graphql', '-f', 'query=' + query]
    for name, value in variables.items():
        if value is not None:
            argv += ['-F', f'{name}={value}']
    doc = gh(*argv)
    if doc.get('errors'):
        raise RuntimeError('GitHub lifecycle query failed')
    return doc['data']


def labels(item):
    value = item.get('labels', [])
    if isinstance(value, dict):
        value = value['nodes']
    return {v['name'] if isinstance(v, dict) else v for v in value}


def dependency_gated(labs):
    return DEPENDENCY in labs and not labs & NON_DEPENDENCY


def refs(pattern, text):
    return {int(n) for match in pattern.finditer(text or '') for n in re.findall(r'#(\d+)', match[1])}


def closing(pr):
    connection = pr.get('closingIssuesReferences') or {}
    if connection.get('pageInfo', {}).get('hasNextPage'):
        raise ValueError('incomplete PR closing references')
    return {r['number'] for r in connection.get('nodes', [])} | refs(CLOSES, pr.get('body'))


def referenced(pr, number):
    text = (pr.get('body') or '') + '\n' + (pr.get('title') or '')
    branch = pr.get('headRefName') or (pr.get('head') or {}).get('ref') or ''
    return (number in closing(pr) or bool(re.search(r'#' + str(number) + r'\b', text))
            or bool(re.search(r'(?:^|[/_-])' + str(number) + r'(?:$|[/_-])', branch)))


def detect(issues, open_prs, slots, merged, resolve):
    if slots is None:
        raise ValueError('MoP ownership unavailable')
    owned = {int(s['issue']) for s in slots if s.get('occupied') is True and s.get('issue')}
    partial = {}
    for pr in merged:
        for n in refs(PARTIAL, pr.get('body')):
            partial.setdefault(n, set()).add(pr['number'])
    findings = []
    for item in issues:
        if item.get('state', 'OPEN').upper() != 'OPEN':
            continue
        n, labs = item['number'], labels(item)
        reasons, prs, blockers = set(), set(), set()
        linked = item.get('references', [])
        if labs & {'status:in-review', 'status:in-progress'} and n not in owned:
            if not any(p.get('state', '').upper() == 'OPEN' for p in linked) and not any(referenced(p, n) for p in open_prs):
                reasons.add('unowned_lifecycle')
        if 'status:done' in labs:
            reasons.add('done_open')
        for pr in linked:
            if pr.get('state', '').upper() == 'MERGED' and n in closing(pr):
                reasons.add('merged_closes_open')
                prs.add(pr['number'])
        if dependency_gated(labs):
            body = item.get('body') or ''
            blockers = refs(BLOCKERS, body) - {n}
            # This is a disposition candidate, not permission to unblock:
            # PM must still verify approval/external/runtime wake conditions.
            if blockers and all(resolve(b) is True for b in sorted(blockers)):
                reasons.add('blockers_resolved')
        if n in partial:
            reasons.add('partial_merge')
            prs.update(partial[n])
        if reasons:
            findings.append(dict(issue=n, title=item.get('title', ''), reasons=sorted(reasons),
                                 prs=sorted(prs), blockers=sorted(blockers)))
    return sorted(findings, key=lambda r: r['issue'])


def render(findings, carried=frozenset()):
    kinds = ('unowned_lifecycle', 'done_open', 'merged_closes_open', 'blockers_resolved', 'partial_merge')
    lines = [HEADER, f'• {len(findings)} issues · ' + ' · '.join(
        f'{kind}={sum(kind in r["reasons"] for r in findings)}' for kind in kinds)
        + f' (blockers_resolved new={sum("blockers_resolved" in r["reasons"] and r["issue"] not in carried for r in findings)}, carried={len(carried)})']
    # Carried blockers_resolved rows were already reported; list only new ones.
    shown = [r for r in findings if not (r['issue'] in carried and r['reasons'] == ['blockers_resolved'])]
    for row in shown[:10]:
        evidence = ('; PR ' + ', '.join('#' + str(n) for n in row['prs'])) if row['prs'] else ''
        lines.append(f'• #{row["issue"]}: ' + ', '.join(row['reasons']) + evidence)
    if len(shown) > 10:
        lines.append(f'• +{len(shown) - 10} more; all in the saved lifecycle findings.')
    return lines


def timeline(number, page):
    out = []
    while True:
        out.extend(node['source'] for node in page['nodes'] if (node.get('source') or {}).get('number'))
        if not page['pageInfo']['hasNextPage']:
            return out
        query = '''query($number:Int!, $cursor:String) { repository(owner:"Scribie", name:"heydonna-app") {
 issue(number:$number) { timelineItems(first:100, after:$cursor, itemTypes:[CROSS_REFERENCED_EVENT]) { ''' + SOURCE + ' } } } }'
        page = graphql(query, dict(number=number, cursor=page['pageInfo']['endCursor']))['repository']['issue']['timelineItems']


def blocker_states(numbers):
    """Resolve bounded batches; absent references are unknown, not satisfied."""
    known = dict.fromkeys(numbers)
    valid = sorted(n for n in numbers if 0 < n <= 2147483647)
    for start in range(0, len(valid), 30):
        batch = valid[start:start + 30]
        aliases = {f'b{n}': n for n in batch}
        fields = ' '.join(f'{a}:issueOrPullRequest(number:{n}) {{ __typename ... on Issue {{ state }} ... on PullRequest {{ merged }} }}'
                          for a, n in aliases.items())
        query = 'query { repository(owner:"Scribie",name:"heydonna-app") { ' + fields + ' } }'
        try:
            doc = gh('api', 'graphql', '-f', 'query=' + query)
        except subprocess.CalledProcessError as error:
            # gh exits 1 even for a valid partial response with a missing alias.
            doc = json.loads(error.stdout or '{}')
            if not doc.get('errors'):
                raise
        for error in doc.get('errors', []):
            path = error.get('path', [])
            if (error.get('type') != 'NOT_FOUND' or len(path) != 2
                    or path[0] != 'repository' or path[1] not in aliases):
                raise RuntimeError('GitHub blocker query failed')
        repository = doc['data']['repository']
        for alias, n in aliases.items():
            node = repository[alias]
            if node is not None:
                if node['__typename'] == 'Issue':
                    known[n] = node['state'] == 'CLOSED'
                elif node['__typename'] == 'PullRequest':
                    known[n] = node['merged'] is True
                else:
                    raise ValueError('unexpected blocker type')
    return known


def collect(since):
    issues, cursor = [], None
    while True:
        query = '''query($cursor:String) { repository(owner:"Scribie", name:"heydonna-app") {
 issues(first:30, after:$cursor, states:OPEN) { nodes { number title body state
 labels(first:100) { nodes { name } pageInfo { hasNextPage } }
 timelineItems(first:100, itemTypes:[CROSS_REFERENCED_EVENT]) { ''' + SOURCE + ''' }
 } pageInfo { hasNextPage endCursor } } } }'''
        page = graphql(query, dict(cursor=cursor))['repository']['issues']
        for item in page['nodes']:
            if item['labels']['pageInfo']['hasNextPage']:
                raise ValueError('incomplete issue labels')
            item['references'] = timeline(item['number'], item.pop('timelineItems'))
            issues.append(item)
        if not page['pageInfo']['hasNextPage']:
            break
        cursor = page['pageInfo']['endCursor']
    open_prs = [pr for page in gh('api', '--paginate', '--slurp', f'repos/{REPO}/pulls?state=open&per_page=100') for pr in page]
    with urllib.request.urlopen('http://127.0.0.1:3100/slots', timeout=10) as response:
        slots = json.load(response)['slots']
    merged, cursor = [], None
    while True:
        query = '''query($cursor:String) { repository(owner:"Scribie", name:"heydonna-app") {
 pullRequests(first:100, after:$cursor, states:MERGED, orderBy:{field:UPDATED_AT, direction:DESC}) {
 nodes { number body mergedAt updatedAt } pageInfo { hasNextPage endCursor } } } }'''
        page = graphql(query, dict(cursor=cursor))['repository']['pullRequests']
        merged.extend(pr for pr in page['nodes'] if pr['mergedAt'] >= since)
        if not page['pageInfo']['hasNextPage'] or (page['nodes'] and page['nodes'][-1]['updatedAt'] < since):
            break
        cursor = page['pageInfo']['endCursor']
    known = {i['number']: False for i in issues}

    needed = set()
    for item in issues:
        labs = labels(item)
        if dependency_gated(labs):
            needed.update(refs(BLOCKERS, item.get('body')) - {item['number']})
    known.update(blocker_states(needed - known.keys()))
    return issues, open_prs, slots, merged, known.get


def sync(findings):
    for row in findings:
        subprocess.run([sys.executable, str(PM_OPS), 'obligation-upsert',
            '--kind', 'orphaned_issue', '--severity', 'high', '--horizon', 'hourly',
            '--target-type', 'github_issue', '--target-id', str(row['issue']), '--issue', str(row['issue']),
            '--title', f'Issue #{row["issue"]}: ' + ', '.join(row['reasons']),
            '--action', 'Read issue ACs; choose one: re-queue via ready-pool-contract-repair, close with evidence, unblock, or park with reason. Never invent ACs.',
            '--evidence-json', json.dumps(row)], capture_output=True, text=True, timeout=30, check=True)


def resolve_if_unclaimed(row_id, reason, updated_at=None, evidence_sha256=None):
    """Resolve one exact open orphaned_issue row unless it is claimed or changed.

    pm-ops does the open+unclaimed+fingerprint check and the write in one BEGIN
    IMMEDIATE transaction: a claim, or a canonical upsert (new reasons/evidence)
    landing after our read, is never overwritten on our stale verification.
    Returns 'resolved', 'skipped_claimed', 'skipped_changed' or 'skipped_not_open'.
    """
    argv = [sys.executable, str(PM_OPS), 'obligation-resolve', '--kind', 'orphaned_issue',
            '--id', str(row_id), '--if-unclaimed', '--reason', reason]
    if updated_at is not None:
        argv += ['--expect-updated-at', updated_at]
    if evidence_sha256 is not None:
        argv += ['--expect-evidence-sha256', evidence_sha256]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    if proc.returncode:
        raise RuntimeError(f'obligation-resolve failed for row {row_id}: {proc.stderr.strip()}')
    outcome = (proc.stdout.strip().splitlines() or [''])[-1]
    if outcome not in ('resolved', 'skipped_claimed', 'skipped_changed', 'skipped_not_open'):
        raise RuntimeError(f'obligation-resolve unexpected output for row {row_id}: {outcome!r}')
    return outcome


def ledger_path():
    proc = subprocess.run([sys.executable, str(PM_OPS), 'status', '--format', 'json', '--read-only'],
                          capture_output=True, text=True, timeout=30, check=True)
    return Path(json.loads(proc.stdout)['db']).resolve()


def open_rows():
    """Open issue-keyed orphaned_issue rows: id, issue, owner, suppress_until, reasons."""
    with sqlite3.connect(ledger_path().as_uri() + '?mode=ro', uri=True) as connection:
        rows = connection.execute("SELECT id, issue, owner, suppress_until, evidence_json, updated_at FROM obligations "
                                  "WHERE kind='orphaned_issue' AND status='open' AND issue IS NOT NULL "
                                  "ORDER BY id").fetchall()
    out = []
    for row_id, issue, owner, until, evidence, updated_at in rows:
        fingerprint = hashlib.sha256((evidence or '').encode('utf-8')).hexdigest()
        try:
            reasons = json.loads(evidence or '{}').get('reasons')
        except (ValueError, AttributeError):
            reasons = None
        if not (isinstance(reasons, list) and reasons and all(isinstance(r, str) for r in reasons)):
            reasons = None  # PM-written or unknown evidence: never auto-resolved
        out.append(dict(id=int(row_id), issue=int(issue), owner=owner, suppress_until=until, reasons=reasons,
                        updated_at=updated_at, evidence_sha256=fingerprint))
    return out


def claimed(row, now):
    """Mirror of the pm-ops --if-unclaimed rule; a cheap pre-filter only."""
    if not (row.get('owner') or '').strip() or not (row.get('suppress_until') or '').strip():
        return False
    try:
        until = dt.datetime.fromisoformat(row['suppress_until'].replace('Z', '+00:00'))
        if until.tzinfo is None:
            until = until.replace(tzinfo=dt.timezone.utc)
        return until > now
    except (ValueError, TypeError):
        return True


def live_issue(number):
    """Live issue state; any gh failure or malformed payload raises and the row is kept.

    The REST payload must carry a well-formed state ('open'/'closed') and label
    collection (a list of {name: str}); a missing or malformed label collection
    must never decode to "no labels" and clear a warning. A valid empty list may.
    """
    doc = gh('api', f'repos/{REPO}/issues/{number}')
    if not isinstance(doc, dict) or doc.get('number') != number:
        raise ValueError('unexpected issue payload')
    state = doc.get('state')
    if not isinstance(state, str) or state.lower() not in ('open', 'closed'):
        raise ValueError('malformed issue state')
    raw_labels = doc.get('labels')
    if not isinstance(raw_labels, list) or not all(
            isinstance(v, dict) and isinstance(v.get('name'), str) and v['name'] for v in raw_labels):
        raise ValueError('malformed issue labels')
    raw_assignees = doc.get('assignees', [])
    if not isinstance(raw_assignees, list):
        raise ValueError('malformed issue assignees')
    return dict(state=state.upper(), labels={v['name'] for v in raw_labels},
                assignees=[a.get('login') for a in raw_assignees if isinstance(a, dict) and a.get('login')])


def reason_cleared(reason, number, issue, owned, open_prs, narrowed):
    """True only when the live state proves this finding no longer holds."""
    closed = issue['state'] == 'CLOSED'
    if reason in WINDOW_ONLY:
        return False  # derived from a rolling merged-PR window; absence proves nothing
    if reason == 'unowned_lifecycle':
        # The finding needs a lifecycle label AND no owner; either half gone clears it
        # (PM decision 2026-11-05: lifecycle label gone counts as cleared).
        return (closed or number in owned or any(referenced(p, number) for p in open_prs)
                or not issue['labels'] & LIFECYCLE_LABELS)
    if reason in ('merged_closes_open', 'done_open'):
        return closed
    if reason == 'blockers_resolved':
        return closed or bool(issue['assignees']) or DEPENDENCY not in issue['labels'] or narrowed
    return False  # unknown reason stays open


def recheck(findings, slots, open_prs, previous, cursor=0, write=True, now=None):
    """Live-recheck open orphaned_issue rows the current run no longer reports.

    Rows still reported are current by construction. For the rest, every stored
    reason must be proven cleared by a live read (at most RECHECK_CAP per run,
    rotating from `cursor`); window-only/unknown reasons, claimed rows and
    failed reads are kept. Writes go only through obligation-resolve --if-unclaimed.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    owned = {int(s['issue']) for s in slots if s.get('occupied') is True and s.get('issue')}
    current = {r['issue'] for r in findings}
    narrowed = {r['issue'] for r in previous if 'blockers_resolved' in r.get('reasons', [])} - current
    rows = [r for r in open_rows() if r['issue'] not in current]
    rows = [r for r in rows if r['id'] > cursor] + [r for r in rows if r['id'] <= cursor]
    report = dict(resolved=[], skipped_claimed=[], skipped_changed=[], skipped_not_open=[], kept_window_or_unknown=[],
                  kept_not_cleared=[], skipped_gh_failure=[], checked=0, cursor=cursor)
    for row in rows:
        if row['reasons'] is None or set(row['reasons']) - LIVE_CHECKABLE:
            report['kept_window_or_unknown'].append(row['id'])
            continue
        if claimed(row, now):
            report['skipped_claimed'].append(row['id'])
            continue
        if report['checked'] >= RECHECK_CAP:
            break
        report['checked'] += 1
        report['cursor'] = row['id']
        try:
            issue = live_issue(row['issue'])
        except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError):
            report['skipped_gh_failure'].append(row['id'])
            continue
        if not all(reason_cleared(r, row['issue'], issue, owned, open_prs, row['issue'] in narrowed)
                   for r in row['reasons']):
            report['kept_not_cleared'].append(row['id'])
            continue
        if not write:
            report['resolved'].append(row['id'])
            continue
        reason = 'live_recheck_cleared:' + ','.join(sorted(row['reasons'])) + ':' + issue['state'].lower()
        report[resolve_if_unclaimed(row['id'], reason, row.get('updated_at'), row.get('evidence_sha256'))].append(row['id'])
    else:
        report['cursor'] = 0  # full pass completed; restart from the oldest row
    return report


def active_suppressions(now):
    """Read PM's authoritative dispositions; never renew or overwrite them."""
    with sqlite3.connect(ledger_path().as_uri() + '?mode=ro', uri=True) as connection:
        rows = connection.execute("SELECT issue, suppress_until FROM obligations "
                                  "WHERE kind='orphaned_issue' AND status='open' "
                                  "AND owner IS NOT NULL AND owner != '' AND suppress_until IS NOT NULL")
        suppressed = set()
        for issue, until in rows:
            try:
                expires = dt.datetime.fromisoformat(until.replace('Z', '+00:00'))
                if expires > now:
                    suppressed.add(int(issue))
            except (ValueError, TypeError):
                continue  # Invalid/naive timestamps must not hide work.
    return suppressed


def run(dry_run=False):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with STATE.with_suffix('.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        now = dt.datetime.now(dt.timezone.utc)
        saved = json.loads(STATE.read_text()) if STATE.exists() else {}
        since = saved.get('since') or (now - dt.timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%SZ')
        collected = collect(since)
        findings = detect(*collected)
        _issues, open_prs, slots = collected[0], collected[1], collected[2]
        resolved = {r['issue'] for r in findings if 'blockers_resolved' in r['reasons']}
        reported = set(saved.get('reported_blockers_resolved', []))
        suppressed = active_suppressions(now)
        actionable = [r for r in findings if r['issue'] not in suppressed]
        carried = resolved & reported & {r['issue'] for r in actionable}
        lines = render(actionable, carried)
        if len(actionable) != len(findings):
            lines.append(f'• {len(findings) - len(actionable)} dispositioned issues suppressed until their recorded review/wake; not actions.')
        if dry_run:
            return dict(dry_run=True, findings=actionable, lines=lines, would_resolve=[])
        # Routine lifecycle findings live in this report and GitHub. Neither
        # create mirrors nor resolve an explicitly tracked issue commitment.
        doc = dict(since=now.strftime('%Y-%m-%dT%H:%M:%SZ'), findings=actionable, lines=lines,
                   scanned_since=since, reported_blockers_resolved=sorted(resolved))
        temporary = STATE.with_suffix('.new')
        temporary.write_text(json.dumps(doc, indent=2) + '\n')
        temporary.replace(STATE)
        return doc


if __name__ == '__main__':
    try:
        print(json.dumps(run(dry_run='--dry-run' in sys.argv)))
    except subprocess.CalledProcessError as error:
        # Do not emit command arguments or raw stderr, which may contain secrets.
        stderr = error.stderr or ''
        status = re.search(r'HTTP\s+\d{3}', stderr, re.I)
        reasons = ('rate limit', 'timeout', 'timed out', 'connection refused',
                   'connection reset', 'not found', 'authentication', 'permission denied')
        reason = next((value for value in reasons if value in stderr.lower()), 'GitHub command failed')
        print(f'{reason}' + (f' ({status.group(0)})' if status else ''), file=sys.stderr)
        sys.exit(error.returncode or 1)
