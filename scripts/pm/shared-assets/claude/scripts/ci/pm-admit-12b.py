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


def authorized_reviewer_login():
    """Use the authenticated gh account that PM's 12b comment is posted as."""
    try:
        out = subprocess.check_output(['gh', 'api', 'user', '--jq', '.login'],
                                      stderr=subprocess.DEVNULL, timeout=15).decode().strip()
        return out
    except (OSError, subprocess.SubprocessError):
        return ''


def resolve_commit(sha):
    """Resolve a marker to one unambiguous local commit, or fail closed."""
    try:
        checkout = Path.home() / 'Downloads/projects/heydonna-app'
        return subprocess.check_output(
            ['git', '-C', str(checkout), 'rev-parse', '--verify', sha + '^{commit}'],
            stderr=subprocess.DEVNULL, timeout=60,
        ).decode().strip().lower()
    except (OSError, subprocess.SubprocessError):
        return None


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


def canonical_markers(comments, reviewer_login):
    """Parse only canonical first-line markers and bind them to the authenticated reviewer."""
    markers = []
    for comment in sorted(comments or [], key=lambda c: (c.get('created_at', ''), c.get('id', 0))):
        lines = (comment.get('body') or '').splitlines()
        first = lines[0] if lines else ''
        first = re.sub(r'^#{1,6} ', '', first)
        match = MARKER.match(first)
        legacy = LEGACY.match(first)
        identity = comment.get('user') or comment.get('author') or {}
        author = identity.get('login', '') if isinstance(identity, dict) else str(identity)
        authorized = bool(reviewer_login and author == reviewer_login)
        if match:
            markers.append((*match.groups(), True, authorized))
        elif legacy:
            old_sha, old_verdict = legacy.groups()
            markers.append((old_verdict, old_sha, False, authorized))
    return markers


def reviewed_12b_verdict(comments, head, reviewer_login, repo, pr):
    """Return latest canonical, authenticated verdict valid for this head.

    PASS may carry only across the same clean main-merge-only transformation
    accepted by admission; other verdicts require the exact reviewed head.
    """
    markers = canonical_markers(comments, reviewer_login)
    if not markers:
        return None
    verdict, sha, canonical, authorized = markers[-1]
    sha = sha.lower()
    if not canonical or not authorized:
        return None
    reviewed = resolve_commit(sha)
    current = resolve_commit(head)
    if not reviewed or not current:
        return None
    if reviewed == current:
        return verdict
    if verdict == 'PASS' and main_merges_only(repo, reviewed, current):
        return verdict
    return None


def evaluate(repo, pr, head):
    last = 'none'
    try:
        comments = api(repo, f'issues/{pr}/comments?per_page=100', True)
        reviewer_login = authorized_reviewer_login()
        markers = canonical_markers(comments, reviewer_login)
        if markers:
            verdict, sha, _, _ = markers[-1]
            last = f'{verdict}@{sha.lower()}'
        try:
            if exempt(repo, pr, head):
                return True, '12B_GATE: exempt (control-plane-only)'
        except Exception:
            pass  # An unavailable classifier never grants an exemption.
        verdict = reviewed_12b_verdict(comments, head, reviewer_login, repo, pr)
        if verdict == 'PASS':
            sha = markers[-1][1].lower()
            if resolve_commit(sha) == resolve_commit(head):
                return True, f'12B_GATE: PASS pr={pr} head={head}'
            return True, f'12B_GATE main-merge-only carry-forward from {sha} pr={pr} head={head}'
    except Exception:
        pass
    return False, f'REFUSE: 12b_missing_or_stale pr={pr} head={head} last_12b={last}'


TEST_PATH = re.compile(r'(^|/)(__tests__|tests?)/|\.(test|spec)\.[A-Za-z]+$|(^|/)test_[^/]+\.py$')
COMPARE_FILE_CAP = 300  # GitHub's compare API stops listing files at 300.
OVERLAP_PATH_LIMIT = 20


def changed_paths(payload):
    """Changed paths (renames included) of a compare payload, plus whether the list may be truncated."""
    files = payload.get('files') or []
    paths = set()
    for item in files:
        for key in ('filename', 'previous_filename'):
            if item.get(key):
                paths.add(item[key])
    return paths, len(files) >= COMPARE_FILE_CAP


def path_stem(path):
    name = re.sub(r'^test_', '', path.rsplit('/', 1)[-1])
    return re.split(r'\.(?:test|spec)\.|\.', name)[0]


def overlap_report(repo, pr, head):
    """Advisory overlap lines for the admission output (retro #13357/#13435; CTO C0ALZJHGE49 ts 1794578332.315349).

    Names the reviewed base, current main, the paths changed by BOTH the PR and
    main since that base, and the proof to re-run. Never changes the verdict:
    a clean file merge is not proof, so overlap means re-prove, no overlap means
    the reviewed proof carries (paths only; imports are not analyzed).
    """
    try:
        usable = [m for m in canonical_markers(api(repo, f'issues/{pr}/comments?per_page=100', True),
                                               authorized_reviewer_login()) if m[2] and m[3]]
        if not usable:
            return ['12B_OVERLAP: UNKNOWN reason=no_authorized_12b_marker']
        reviewed = resolve_commit(usable[-1][1]) or usable[-1][1].lower()
        main_side = api(repo, f'compare/{reviewed}...main')
        base = (main_side.get('merge_base_commit') or {}).get('sha') or 'UNKNOWN'
        tip = api(repo, 'commits/main').get('sha') or 'UNKNOWN'
        main_paths, main_cut = changed_paths(main_side)
        pr_paths, pr_cut = changed_paths(api(repo, f'compare/main...{reviewed}'))
        overlap = sorted(main_paths & pr_paths)
        shown = ','.join(overlap[:OVERLAP_PATH_LIMIT]) + (f',+{len(overlap) - OVERLAP_PATH_LIMIT} more' if len(overlap) > OVERLAP_PATH_LIMIT else '')
        stems = {path_stem(p) for p in overlap if not TEST_PATH.search(p)}
        tests = sorted(p for p in pr_paths if TEST_PATH.search(p) and (p in overlap or path_stem(p) in stems))
        if main_cut or pr_cut:
            action, proof = 'UNKNOWN', 'file lists truncated at the API cap; re-prove the affected behavior'
        elif overlap:
            action = 'RE-PROVE'
            proof = ','.join(tests[:OVERLAP_PATH_LIMIT]) if tests else 'UNKNOWN (no owning test matched by name; re-run the PR changed tests)'
        else:
            action, proof = 'CARRY', 'none (no path changed by both the PR and main since the reviewed base)'
        return [
            f'12B_OVERLAP: reviewed_head={reviewed} reviewed_base={base} current_head={head} current_main={tip} main_commits_since_review={main_side.get("ahead_by", "UNKNOWN")}',
            f'12B_OVERLAP: pr_paths={len(pr_paths)} main_paths={len(main_paths)} overlap={len(overlap)} paths={shown or "none"}',
            f'12B_OVERLAP: proof_action={action} affected_proof={proof} (advisory only; a clean merge is not proof; the admission decision is unchanged)',
        ]
    except Exception as exc:
        return [f'12B_OVERLAP: UNKNOWN reason={type(exc).__name__}']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--pr', required=True, type=int)
    parser.add_argument('--head', required=True)
    parser.add_argument('--overlap-only', action='store_true',
                        help='print only the advisory overlap report (called by pm-admit-ci.sh); no verdict')
    args = parser.parse_args()
    if args.overlap_only:
        for line in overlap_report(args.repo, args.pr, args.head):
            print(line)
        return 0
    ok, line = evaluate(args.repo, args.pr, args.head)
    print(line)
    return 0 if ok else 3


if __name__ == '__main__':
    raise SystemExit(main())
