#!/usr/bin/env python3
"""Hourly issue lifecycle findings; GitHub/MoP are read-only, pm-ops owns writes.

Uses the existing Unblockable now section. Never changes labels or invents ACs.
The first run backfills partial merges for 24h; all open issue timelines are
checked regardless of age. A failed collection/upsert never advances the cursor.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

REPO = 'heydonna-app/heydonna-app'
STATE = Path.home() / '.claude/state/issue-lifecycle.json'
PM_OPS = Path.home() / '.claude/scripts/pm-ops.py'
HEADER = '*Unblockable now / issue lifecycle:*'
PARTIAL = re.compile(r'\b(?:part\s+of|refs?)\s+((?:#\d+[ \t,;]*(?:and\s+)?)+)', re.I)
CLOSES = re.compile(r'\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)\s+((?:#\d+[ \t,;]*(?:and\s+)?)+)', re.I)
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
        if labs & {'status:blocked', 'status:backlog'} or any(l.startswith('pm-blocked:') for l in labs):
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


def render(findings):
    kinds = ('unowned_lifecycle', 'done_open', 'merged_closes_open', 'blockers_resolved', 'partial_merge')
    lines = [HEADER, f'• {len(findings)} issues · ' + ' · '.join(
        f'{kind}={sum(kind in r["reasons"] for r in findings)}' for kind in kinds)]
    for row in findings[:10]:
        evidence = ('; PR ' + ', '.join('#' + str(n) for n in row['prs'])) if row['prs'] else ''
        lines.append(f'• #{row["issue"]}: ' + ', '.join(row['reasons']) + evidence)
    if len(findings) > 10:
        lines.append(f'• +{len(findings) - 10} more; all in orphaned_issue obligations.')
    return lines


def timeline(number, page):
    out = []
    while True:
        out.extend(node['source'] for node in page['nodes'] if (node.get('source') or {}).get('number'))
        if not page['pageInfo']['hasNextPage']:
            return out
        query = '''query($number:Int!, $cursor:String) { repository(owner:"heydonna-app", name:"heydonna-app") {
 issue(number:$number) { timelineItems(first:100, after:$cursor, itemTypes:[CROSS_REFERENCED_EVENT]) { ''' + SOURCE + ' } } } }'
        page = graphql(query, dict(number=number, cursor=page['pageInfo']['endCursor']))['repository']['issue']['timelineItems']


def collect(since):
    issues, cursor = [], None
    while True:
        query = '''query($cursor:String) { repository(owner:"heydonna-app", name:"heydonna-app") {
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
        query = '''query($cursor:String) { repository(owner:"heydonna-app", name:"heydonna-app") {
 pullRequests(first:100, after:$cursor, states:MERGED, orderBy:{field:UPDATED_AT, direction:DESC}) {
 nodes { number body mergedAt updatedAt } pageInfo { hasNextPage endCursor } } } }'''
        page = graphql(query, dict(cursor=cursor))['repository']['pullRequests']
        merged.extend(pr for pr in page['nodes'] if pr['mergedAt'] >= since)
        if not page['pageInfo']['hasNextPage'] or (page['nodes'] and page['nodes'][-1]['updatedAt'] < since):
            break
        cursor = page['pageInfo']['endCursor']
    known = {i['number']: False for i in issues}

    def resolve(n):
        if n not in known:
            doc = gh('api', f'repos/{REPO}/issues/{n}')
            if doc.get('pull_request'):
                pr = gh('api', f'repos/{REPO}/pulls/{n}')
                known[n] = bool(pr.get('merged_at'))
            else:
                known[n] = doc.get('state') == 'closed'
        return known[n]
    return issues, open_prs, slots, merged, resolve


def sync(findings):
    for row in findings:
        subprocess.run([sys.executable, str(PM_OPS), 'obligation-upsert',
            '--kind', 'orphaned_issue', '--severity', 'high', '--horizon', 'hourly',
            '--target-type', 'github_issue', '--target-id', str(row['issue']), '--issue', str(row['issue']),
            '--title', f'Issue #{row["issue"]}: ' + ', '.join(row['reasons']),
            '--action', 'Read issue ACs; choose one: re-queue via ready-pool-contract-repair, close with evidence, unblock, or park with reason. Never invent ACs.',
            '--evidence-json', json.dumps(row)], capture_output=True, text=True, timeout=30, check=True)


def run():
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with STATE.with_suffix('.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        now = dt.datetime.now(dt.timezone.utc)
        saved = json.loads(STATE.read_text()) if STATE.exists() else {}
        since = saved.get('since') or (now - dt.timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%SZ')
        findings = detect(*collect(since))
        sync(findings)
        doc = dict(since=now.strftime('%Y-%m-%dT%H:%M:%SZ'), findings=findings, lines=render(findings), scanned_since=since)
        temporary = STATE.with_suffix('.new')
        temporary.write_text(json.dumps(doc, indent=2) + '\n')
        temporary.replace(STATE)
        return doc


if __name__ == '__main__':
    print(json.dumps(run()))
