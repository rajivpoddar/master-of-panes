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


def main_merges_only(repo, old, head):
    """Carry a review only across clean main merges with an unchanged PR patch."""
    checkout = Path.home() / 'Downloads/projects/heydonna-app'

    def git(*args):
        return subprocess.check_output(['git', '-C', str(checkout), *args],
                                       stderr=subprocess.DEVNULL, timeout=60).strip()

    old = git('rev-parse', '--verify', old + '^{commit}').decode()
    head = git('rev-parse', '--verify', head + '^{commit}').decode()
    git('merge-base', '--is-ancestor', old, head)
    chain = git('rev-list', '--first-parent', '--reverse', f'{old}..{head}').decode().splitlines()
    if not chain or chain[-1] != head:
        return False
    previous = old
    for commit in chain:
        parents = git('show', '-s', '--format=%P', commit).decode().split()
        if len(parents) != 2 or parents[0] != previous:
            return False
        main = parents[1]
        if api(repo, f'compare/{main}...main').get('status') not in ('ahead', 'identical'):
            return False
        # A conflicted merge or any manual tree edit is never carried forward.
        tree = git('merge-tree', '--write-tree', previous, main)
        if tree != git('rev-parse', commit + '^{tree}'):
            return False
        base = git('merge-base', previous, main).decode()
        def patch(left, right):
            raw = git('diff', '--binary', '--no-ext-diff', '--no-renames', '-U0', left, right)
            # Main may shift line numbers and change blob IDs without changing the
            # reviewed edits. Keep every changed byte and file/mode header.
            lines = []
            for line in raw.splitlines(keepends=True):
                if line.startswith(b'index '):
                    continue
                if line.startswith(b'@@ '):
                    line = b'@@\n'
                lines.append(line)
            return b''.join(lines)
        if patch(base, previous) != patch(main, commit):
            return False
        previous = commit
    return True


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
            if main_merges_only(repo, sha, head):
                return True, f'12B_GATE main-merge-only carry-forward from {sha} pr={pr} head={head}'
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
