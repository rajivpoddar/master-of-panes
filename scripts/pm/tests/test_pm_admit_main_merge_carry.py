"""Carry-forward checks use real commit trees, without GitHub access."""
import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import patch

P = Path(__file__).parents[1] / 'shared-assets/claude/scripts/ci/pm-admit-12b.py'
spec = importlib.util.spec_from_file_location('admit_merge_carry', P)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()


def history(tmp_path, edit_merge=False, extra_commit=False):
    repo = tmp_path / 'Downloads/projects/heydonna-app'
    repo.mkdir(parents=True)
    git(repo, 'init', '-b', 'main')
    git(repo, 'config', 'user.name', 'Test')
    git(repo, 'config', 'user.email', 'test@example.invalid')
    (repo / 'base').write_text('base\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'base')
    git(repo, 'checkout', '-b', 'pr')
    (repo / 'pr-file').write_text('reviewed\n')
    if edit_merge:
        (repo / 'base').write_text('reviewed change\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'reviewed')
    old = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'checkout', 'main')
    (repo / 'main-file').write_text('main\n')
    if edit_merge:
        (repo / 'base').write_text('main change\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'main change')
    git(repo, 'checkout', 'pr')
    if edit_merge:
        result = subprocess.run(['git', '-C', str(repo), 'merge', '--no-ff', 'main', '-m', 'merge main'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert result.returncode != 0
        (repo / 'base').write_text('conflict resolution changes reviewed file\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '--no-edit')
    else:
        git(repo, 'merge', '--no-ff', 'main', '-m', 'merge main')
    if extra_commit:
        (repo / 'pr-file').write_text('unreviewed\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'extra PR commit')
    head = git(repo, 'rev-parse', 'HEAD')
    with patch.object(gate.Path, 'home', return_value=tmp_path), \
         patch.object(gate, 'api', return_value={'status': 'identical'}):
        return gate.main_merges_only('owner/repo', old, head)


def test_clean_main_merge_carries_review(tmp_path):
    assert history(tmp_path)


def test_merge_with_pr_file_resolution_refuses(tmp_path):
    assert not history(tmp_path, edit_merge=True)


def test_extra_pr_commit_refuses(tmp_path):
    assert not history(tmp_path, extra_commit=True)
