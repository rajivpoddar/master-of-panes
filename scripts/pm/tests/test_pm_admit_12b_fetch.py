"""Fresh PR objects must be fetched before the existing clean-main-merge carry check."""
import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import patch

P = Path(__file__).parents[1] / 'shared-assets/claude/scripts/ci/pm-admit-12b.py'
spec = importlib.util.spec_from_file_location('admit12b_fetch', P)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def test_new_head_after_last_fetch_keeps_main_merge_only_pass(tmp_path):
    def git(where, *args):
        return subprocess.check_output(['git', '-C', str(where), *args], stderr=subprocess.DEVNULL).decode().strip()

    upstream = tmp_path / 'upstream'
    upstream.mkdir()
    git(upstream, 'init', '-b', 'main')
    git(upstream, 'config', 'user.name', 'Fixture')
    git(upstream, 'config', 'user.email', 'fixture@example.invalid')
    (upstream / 'base').write_text('base\n')
    git(upstream, 'add', '.')
    git(upstream, 'commit', '-m', 'base')
    git(upstream, 'checkout', '-b', 'topic')
    (upstream / 'topic').write_text('reviewed\n')
    git(upstream, 'add', '.')
    git(upstream, 'commit', '-m', 'reviewed')
    reviewed = git(upstream, 'rev-parse', 'HEAD')
    home = tmp_path / 'home'
    checkout = home / 'Downloads/projects/heydonna-app'
    checkout.parent.mkdir(parents=True)
    subprocess.check_call(['git', 'clone', str(upstream), str(checkout)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # The local clone's last fetch predates BOTH the new main commit and PR merge.
    git(upstream, 'checkout', 'main')
    (upstream / 'main-only').write_text('new main\n')
    git(upstream, 'add', '.')
    git(upstream, 'commit', '-m', 'main advanced')
    tip = git(upstream, 'rev-parse', 'HEAD')
    git(upstream, 'checkout', 'topic')
    git(upstream, 'merge', '--no-ff', 'main', '-m', 'merge main')
    head = git(upstream, 'rev-parse', 'HEAD')
    git(upstream, 'update-ref', 'refs/pull/13693/head', head)
    assert subprocess.run(['git', '-C', str(checkout), 'cat-file', '-e', head], capture_output=True).returncode != 0
    comments = [{'body': f'PM 12b: PASS at {reviewed}', 'user': {'login': 'pm'}}]

    def api(repo, endpoint, pages=False):
        if endpoint.startswith('issues/'):
            return comments
        if endpoint == 'commits/main':
            return {'sha': tip}
        if endpoint == f'compare/{tip}...{tip}':
            return {'status': 'identical'}
        raise AssertionError(endpoint)

    with patch.object(gate.Path, 'home', return_value=home), patch.object(gate, 'api', side_effect=api), \
         patch.object(gate, 'authorized_reviewer_login', return_value='pm'), patch.object(gate, 'exempt', return_value=False):
        ok, line = gate.evaluate('owner/repo', 13693, head)
    assert ok and line == f'12B_GATE main-merge-only carry-forward from {reviewed} pr=13693 head={head}'
    assert git(checkout, 'rev-parse', 'origin/main') == tip


def test_fetch_failure_is_explicit_fail_closed():
    comments = [{'body': 'PM 12b: PASS at ' + 'a' * 40, 'user': {'login': 'pm'}}]
    with patch.object(gate, 'api', return_value=comments), patch.object(gate, 'exempt', return_value=False), \
         patch.object(gate, 'authorized_reviewer_login', return_value='pm'), \
         patch.object(gate, 'fetch_review_inputs', return_value=False), \
         patch.object(gate, 'reviewed_12b_verdict', side_effect=AssertionError('must not inspect stale objects')):
        assert gate.evaluate('owner/repo', 13503, 'b' * 40) == (
            False, 'REFUSE: 12b_fetch_failed pr=13503 head=' + 'b' * 40)


def test_missing_reviewed_full_sha_is_fetched_with_bounded_pr_and_main_refs():
    with patch.object(gate, 'resolve_commit', return_value=None), \
         patch.object(gate.subprocess, 'check_output', return_value=b'') as run:
        assert gate.fetch_review_inputs(13503, 'b' * 40, 'a' * 40)
    argv = run.call_args.args[0]
    assert '+refs/pull/13503/head:refs/remotes/origin/pull/13503/head' in argv
    assert '+refs/heads/main:refs/remotes/origin/main' in argv
    assert 'a' * 40 in argv and 'b' * 40 in argv
    assert run.call_args.kwargs['timeout'] == 30
