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


def fetch_review_inputs(pr, head, reviewed=None):
    """Fetch only missing exact proof inputs; local proof never depends on the network."""
    checkout = Path.home() / 'Downloads/projects/heydonna-app'
    refs = []
    for sha in (head, reviewed):
        if sha and not resolve_commit(sha):
            if re.fullmatch(r'[0-9a-fA-F]{40}', sha):
                refs.append(sha)
            else:
                refs.append(f'+refs/pull/{int(pr)}/head:refs/remotes/origin/pull/{int(pr)}/head')
    if not refs:
        return True
    try:
        subprocess.check_output(
            ['git', '-C', str(checkout), 'fetch', '--no-tags', 'origin', *refs],
            stderr=subprocess.DEVNULL, timeout=30)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


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


def main_merges_only(repo, old, head, main_tip=None):
    """Carry a review only across clean main merges with an unchanged PR patch."""
    checkout = Path.home() / 'Downloads/projects/heydonna-app'

    def git(*args):
        return subprocess.check_output(['git', '-C', str(checkout), *args],
                                       stderr=subprocess.DEVNULL, timeout=60).strip()

    main_tip = main_tip or str(api(repo, 'commits/main').get('sha') or '').lower()
    if not re.fullmatch(r'[0-9a-f]{40}', main_tip):
        return False
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
        if api(repo, f'compare/{main}...{main_tip}').get('status') not in ('ahead', 'identical'):
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
        fetch_review_inputs(pr, head, markers[-1][1] if markers else None)
        # Re-evaluate exact proof even after a failed fetch; use the existing
        # missing/stale refusal only when the proof itself remains unavailable.
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


SHA40 = re.compile(r'^[0-9a-f]{40}$')
MAX_PATH_CHARS = 160


def one_line(text, limit=MAX_PATH_CHARS):
    """Render API-supplied text (paths, counts) on exactly one output line.

    A path may legally contain a newline; printed raw it would forge a second
    `12B_*`-looking line. Control characters and Unicode line separators become '?'.
    """
    clean = re.sub(r'[\x00-\x1f\x7f-\x9f\u2028\u2029]', '?', str(text))
    return clean if len(clean) <= limit else clean[:limit - 1] + '\u2026'


def gate_accepted_pass(comments, head, reviewer_login, repo, main_tip=None):
    """The authenticated, canonical, full-SHA PASS the admission gate itself accepts.

    Mirrors `reviewed_12b_verdict`/`evaluate`: the decision marker is the LAST canonical
    marker only (an earlier PASS never rescues a later REVISE), it must be authored by the
    authenticated reviewer, and its SHA must resolve to one local commit that is the
    current head or reaches it through the clean main-merge-only carry. There is no
    fallback to a short or unresolved SHA. Returns ((reviewed_sha, patch_carry), None)
    or (None, reason).
    """
    markers = canonical_markers(comments, reviewer_login)
    if not markers:
        return None, 'no_authorized_12b_marker'
    verdict, sha, canonical, authorized = markers[-1]
    if not canonical or not authorized:
        return None, 'latest_12b_marker_not_canonical_authenticated'
    if verdict != 'PASS':
        return None, f'latest_12b_marker_is_{verdict}'
    reviewed, current = resolve_commit(sha.lower()), resolve_commit(head)
    if not reviewed or not SHA40.match(reviewed) or not current:
        return None, 'reviewed_sha_unresolved'
    if reviewed == current:
        return (reviewed, 'exact'), None
    if main_merges_only(repo, reviewed, current, main_tip=main_tip):
        return (reviewed, 'main_merges_only'), None
    return None, 'patch_carry_unproved'


def overlap_report(repo, pr, head):
    """Advisory overlap lines for the admission output (retro #13357/#13435; CTO C0ALZJHGE49 ts 1794578332.315349).

    Uses the SAME authenticated full-SHA PASS the gate accepted and ONE pinned
    current-main SHA for every compare (a moving `main` ref could answer two
    questions about two different mains). Names the reviewed base, the paths
    changed by BOTH the PR and main since that base, and the proof to re-run.
    Never changes the verdict. A disjoint path set does not prove imports or
    contracts carry, so the report says UNKNOWN there, never CARRY; any gap in
    the evidence (unresolved SHA, unproved patch carry, mismatched merge base,
    truncated file list) is UNKNOWN too.
    """
    try:
        comments = api(repo, f'issues/{pr}/comments?per_page=100', True)
        tip = str((api(repo, 'commits/main') or {}).get('sha') or '').lower()
        if not SHA40.match(tip):
            return ['12B_OVERLAP: UNKNOWN reason=current_main_unresolved']
        accepted, why = gate_accepted_pass(comments, head, authorized_reviewer_login(), repo, main_tip=tip)
        if not accepted:
            return [f'12B_OVERLAP: UNKNOWN reason={why}']
        reviewed, patch_carry = accepted
        main_side = api(repo, f'compare/{reviewed}...{tip}')
        pr_side = api(repo, f'compare/{tip}...{reviewed}')
        base = str((main_side.get('merge_base_commit') or {}).get('sha') or '').lower()
        if not SHA40.match(base) or base != str((pr_side.get('merge_base_commit') or {}).get('sha') or '').lower():
            return ['12B_OVERLAP: UNKNOWN reason=merge_base_unproved']
        main_paths, main_cut = changed_paths(main_side)
        pr_paths, pr_cut = changed_paths(pr_side)
        overlap = sorted(main_paths & pr_paths)
        shown = ','.join(one_line(p) for p in overlap[:OVERLAP_PATH_LIMIT])
        if len(overlap) > OVERLAP_PATH_LIMIT:
            shown += f',+{len(overlap) - OVERLAP_PATH_LIMIT} more'
        stems = {path_stem(p) for p in overlap if not TEST_PATH.search(p)}
        tests = sorted(p for p in pr_paths if TEST_PATH.search(p) and (p in overlap or path_stem(p) in stems))
        if main_cut or pr_cut:
            action, proof = 'UNKNOWN', 'file lists truncated at the API cap; re-prove the affected behavior'
        elif overlap:
            action = 'RE-PROVE'
            proof = ','.join(one_line(p) for p in tests[:OVERLAP_PATH_LIMIT]) if tests else 'UNKNOWN (no owning test matched by name; re-run the PR changed tests)'
        else:
            action = 'UNKNOWN'
            proof = 'dependency carry unproved (no shared path, but imports and contracts are not analyzed); re-run the PR changed tests if main touched code they use'
        return [
            f'12B_OVERLAP: reviewed_head={reviewed} patch_carry={patch_carry} reviewed_base={base} current_head={one_line(head)} current_main={tip} main_commits_since_review={one_line(main_side.get("ahead_by", "UNKNOWN"))}',
            f'12B_OVERLAP: pr_paths={len(pr_paths)} main_paths={len(main_paths)} overlap={len(overlap)} paths={shown or "none"}',
            f'12B_OVERLAP: proof_action={action} dependency_carry=UNPROVED affected_proof={proof} (advisory only; a clean merge is not proof; the admission decision is unchanged)',
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
