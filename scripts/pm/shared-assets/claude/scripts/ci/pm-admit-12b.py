#!/usr/bin/env python3
"""Read-only, fail-closed 12b admission pre-check. No checkout operations."""
import argparse
import base64
import json
import re
import subprocess
import tempfile
from pathlib import Path

MARKER = re.compile(r"PM 12b(?: delta)?: (PASS|REVISE|BLOCK|FAIL) at ([0-9a-fA-F]{7,40})(?=$|[\s.,;)])")
LEGACY = re.compile(r"PM 12b(?: delta)? at ([0-9a-fA-F]{7,40}): (PASS|REVISE|BLOCK|FAIL)(?=$|[\s.,;)])")


def api(repo, endpoint, pages=False):
    cmd = ['gh', 'api', f'repos/{repo}/{endpoint}']
    if pages:
        cmd += ['--paginate', '--slurp']
    result = json.loads(subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=60))
    return [item for page in result for item in page] if pages else result


def exempt(repo, pr, head):
    # Use the existing shared classifier, not a second path-based policy.
    with tempfile.TemporaryDirectory(prefix='pm-12b-scope-') as tmp:
        for name in ('change_scope.py', 'change-scope-rules.json'):
            data = api(repo, f'contents/scripts/ci/{name}?ref=main')
            Path(tmp, name).write_bytes(base64.b64decode(data['content']))
        out = subprocess.check_output([
            'python3', '-I', str(Path(tmp, 'change_scope.py')), '--rules',
            str(Path(tmp, 'change-scope-rules.json')), '--repo', repo,
            '--pr', str(pr), '--expected-head', head,
        ], stderr=subprocess.DEVNULL, timeout=120)
        scope = json.loads(out)
        return scope.get('control_plane_only') is True and scope.get('product_changed') is False


def evaluate(repo, pr, head):
    last = 'none'
    try:
        comments = api(repo, f'issues/{pr}/comments?per_page=100', True)
        markers = []
        for comment in sorted(comments, key=lambda c: (c.get('created_at', ''), c.get('id', 0))):
            lines = (comment.get('body') or '').splitlines()
            first = lines[0] if lines else ''
            # A markdown heading prefix ("## PM 12b: PASS at <sha>") is the house
            # format for 12b comments and counts as canonical (PM 2026-11-09).
            # Legacy "PM 12b at <sha>: VERDICT" headings still never grant admission.
            match = MARKER.match(re.sub(r'^#{1,6} ', '', first))
            diagnostic = match
            legacy = LEGACY.match(re.sub(r'^#{1,6} ', '', first))
            if diagnostic:
                markers.append((*diagnostic.groups(), bool(match)))
            elif legacy:
                old_sha, old_verdict = legacy.groups()
                markers.append((old_verdict, old_sha, False))
        if markers:
            verdict, sha, canonical = markers[-1]
            sha = sha.lower()
            last = f'{verdict}@{sha}'
        try:
            if exempt(repo, pr, head):
                return True, '12B_GATE: exempt (control-plane-only)'
        except Exception:
            pass  # An unavailable classifier never grants an exemption.
        if markers and canonical and verdict == 'PASS':
            if head.startswith(sha):
                return True, f'12B_GATE: PASS pr={pr} head={head}'
            comparison = api(repo, f'compare/{sha}...{head}?per_page=100')
            commits = comparison['commits']
            # GitHub truncates compare files at 300 and commits at page size.
            if (comparison.get('status') != 'ahead' or
                    comparison.get('total_commits') != len(commits) or
                    len(comparison['files']) >= 300):
                raise ValueError('incomplete comparison')
            own = api(repo, f'pulls/{pr}/commits?per_page=100', True)
            own_shas = {c['sha'] for c in own}
            after = [c for c in commits if c['sha'] in own_shas]
            files = api(repo, f'pulls/{pr}/files?per_page=100', True)
            own_paths = {p for f in files for p in (f['filename'], f.get('previous_filename')) if p}
            changed = {p for f in comparison['files'] for p in (f['filename'], f.get('previous_filename')) if p}
            if (after and after[-1]['sha'] == head and
                    all(len(c['parents']) >= 2 and c['commit']['message'].splitlines()[0].startswith("Merge branch 'main'") for c in after) and
                    not own_paths.intersection(changed)):
                return True, f'12B_GATE: PASS (main-merges-only) pr={pr} head={head}'
    except Exception:
        pass
    return False, f'REFUSE: 12b_missing_or_stale pr={pr} head={head} last_12b={last}'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--pr', required=True, type=int)
    parser.add_argument('--head', required=True)
    args = parser.parse_args()
    ok, line = evaluate(args.repo, args.pr, args.head)
    print(line)
    return 0 if ok else 3


if __name__ == '__main__':
    raise SystemExit(main())
