#!/usr/bin/env python3
"""Read-only, fail-closed 12b admission pre-check. No checkout operations."""
import argparse
import base64
import json
import os
from datetime import datetime, timezone
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


# Fixed rollout deadline: reinstall/restart must never renew the 48-hour grace.
PROVENANCE_DEADLINE = datetime(2026, 11, 11, 18, 40, 9, tzinfo=timezone.utc)


def transition_open():
    return (os.environ.get('PM_ADMIT_12B_STRICT', '') != '1' and
            datetime.now(timezone.utc) < PROVENANCE_DEADLINE)


def read_review(pr):
    """Local reviewer receipt, not GitHub prose. Invalid receipts grant nothing."""
    try:
        path = Path(f'/tmp/pm-admission-review-{pr}.txt')
        if path.is_symlink() or path.stat().st_uid != os.getuid():
            return None
        fields = {}
        for line in path.read_text().splitlines():
            key, value = line.split(':', 1)
            if key in fields:
                return None
            fields[key] = value.strip()
        stamp = datetime.fromisoformat(fields['TIMESTAMP'].replace('Z', '+00:00'))
        if (fields['PR'] != str(pr) or fields['REVIEWER'] != 'pm-admission-reviewer' or
                fields['VERDICT'] != 'PASS' or
                not re.fullmatch('[0-9a-f]{40}', fields['HEAD_SHA']) or
                stamp.tzinfo is None or stamp > datetime.now(timezone.utc)):
            return None
        return fields
    except (OSError, ValueError, KeyError):
        return None


def test_file(path):
    # Only executable test sources, not fixtures/config/helpers in a tests directory.
    return bool(re.search(r'(^|/)(test_[^/]+\.py|[^/]+[.-](test|spec)\.(py|[cm]?[jt]sx?)|test-[^/]+\.sh)$', path))


def verify_history(repo, pr, reviewed, head, mechanical):
    """Inspect the complete first-parent chain in an isolated bare object store.

    Main merge labels are not proof: the second parent must be on main and the
    actual tree must equal Git's clean merge-tree result (no hidden resolutions).
    """
    if not re.fullmatch('[0-9a-f]{40}', head):
        return False
    if reviewed == head:
        return True
    with tempfile.TemporaryDirectory(prefix='pm-12b-history-') as tmp:
        def git(*args):
            return subprocess.check_output(['git', '-C', tmp, *args],
                                           stderr=subprocess.DEVNULL, timeout=120).decode().strip()
        git('init', '--bare', '--quiet')
        git('fetch', '--quiet', '--no-tags', f'https://github.com/{repo}.git',
            reviewed, head, 'refs/heads/main:refs/heads/main')
        git('merge-base', '--is-ancestor', reviewed, head)
        base = git('merge-base', reviewed, 'refs/heads/main')
        own_paths = set(git('diff', '--name-only', '--no-renames', base, reviewed).splitlines())
        files = api(repo, f'pulls/{pr}/files?per_page=100', True)
        own_paths.update(p for f in files for p in (f['filename'], f.get('previous_filename')) if p)
        chain = git('rev-list', '--first-parent', '--reverse', f'{reviewed}..{head}').splitlines()
        previous = reviewed
        for sha in chain:
            parents = git('show', '-s', '--format=%P', sha).split()
            if not parents or parents[0] != previous:
                return False
            paths = set(git('diff', '--name-only', '--no-renames', previous, sha).splitlines())
            if len(parents) == 2:
                subject = git('show', '-s', '--format=%s', sha)
                if not re.match(r"^Merge branch 'main'(?: |$)", subject):
                    return False
                git('merge-base', '--is-ancestor', parents[1], 'refs/heads/main')
                clean_tree = git('merge-tree', '--write-tree', previous, parents[1]).splitlines()[0]
                if clean_tree != git('rev-parse', f'{sha}^{{tree}}'):
                    return False
                if not mechanical and own_paths.intersection(paths):
                    return False
            elif len(parents) == 1 and mechanical and paths and all(test_file(p) for p in paths):
                pass
            else:
                return False
            previous = sha
        return bool(chain) and previous == head


def evaluate(repo, pr, head):
    last = 'none'
    try:
        comments = api(repo, f'issues/{pr}/comments?per_page=100', True)
        markers = []
        last_body = ''
        for comment in sorted(comments, key=lambda c: (c.get('created_at', ''), c.get('id', 0))):
            lines = (comment.get('body') or '').splitlines()
            first = lines[0] if lines else ''
            # A markdown heading prefix ("## PM 12b: PASS at <sha>") is the house
            # format for 12b comments and counts as canonical (PM 2026-11-09).
            # Legacy "PM 12b at <sha>: VERDICT" headings still never grant admission.
            match = MARKER.match(re.sub(r'^#{1,6} ', '', first))
            diagnostic = match
            legacy = LEGACY.match(re.sub(r'^#{1,6} ', '', first))
            if diagnostic or legacy:
                last_body = comment.get('body') or ''
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
        review = read_review(pr)
        last_marker = review['HEAD_SHA'] if review else 'none'
        # A later negative/legacy verdict cannot be bypassed by an old PASS receipt.
        if not markers or (canonical and verdict == 'PASS'):
            mechanical = bool(markers and head.startswith(sha) and
                              '(mechanical)' in last_body)
            if review:
                try:
                    if verify_history(repo, pr, last_marker, head, mechanical):
                        return True, f'12B_GATE: PASS (marker-backed) pr={pr} head={head}'
                except Exception:
                    pass  # Missing objects, conflicts and git failures never grant proof.
            if not transition_open():
                return False, f'REFUSE: 12b_provenance_missing pr={pr} head={head} last_marker={last_marker}'
        if markers and canonical and verdict == 'PASS':
            warning = '12B_GATE: WARN provenance_unverified\n'
            if head.startswith(sha):
                return True, warning + f'12B_GATE: PASS pr={pr} head={head}'
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
                return True, warning + f'12B_GATE: PASS (main-merges-only) pr={pr} head={head}'
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
