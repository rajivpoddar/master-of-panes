"""lane-worktree.sh contract (added to the repo; not executed on the host).

CTO REVISE on f203baec (C0ALZJHGE49 thread 1795015806.949919, ts 1795015902.846089): the proxy Bash fence must
not exclude raw "git worktree *" or "ssh *". Only this helper runs outside the sandbox, so it must confine every
add/remove to the calling lane's own /private/tmp/<lane>-* worktrees, suppress hooks and never remove an
unregistered path. Fixtures are disposable repos under /private/tmp.
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

HELPER = Path(__file__).parents[1] / "shared-assets/claude/scripts/lane-worktree.sh"


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True, capture_output=True)


def _repo():
    root = Path(tempfile.mkdtemp(prefix="lwt-fixture-", dir="/private/tmp"))
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", ".")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    hook = hooks / "post-checkout"
    hook.write_text("#!/bin/sh\ntouch %s/hook-ran\n" % root)
    hook.chmod(0o755)
    return root, repo


def _run(repo, lane, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["PROXY_AGENT_NAME"] = lane
    return subprocess.run(["bash", str(HELPER), *args], cwd=repo, env=env, capture_output=True, text=True)


def _cleanup(root, *worktrees):
    for w in worktrees:
        shutil.rmtree(w, ignore_errors=True)
    shutil.rmtree(root, ignore_errors=True)


def test_add_remove_inside_own_prefix_suppresses_hooks():
    root, repo = _repo()
    wt = "/private/tmp/%s-a-wt" % root.name
    try:
        added = _run(repo, root.name + "-a", "add", wt)
        assert added.returncode == 0 and "OK action=add" in added.stdout
        assert not (root / "hook-ran").exists()
        removed = _run(repo, root.name + "-a", "remove", wt)
        assert removed.returncode == 0 and "OK action=remove" in removed.stdout
        assert not Path(wt).exists()
    finally:
        _cleanup(root, wt)


def test_cross_lane_target_is_refused_and_left_intact():
    root, repo = _repo()
    other = "/private/tmp/%s-b-wt" % root.name
    try:
        assert _run(repo, root.name + "-b", "add", other).returncode == 0
        refused = _run(repo, root.name + "-a", "remove", other, "--force")
        assert refused.returncode == 2 and "REFUSED" in refused.stderr
        assert Path(other).is_dir()
        assert _run(repo, root.name + "-a", "add", other + "-2").returncode == 2
    finally:
        _cleanup(root, other)


def test_unregistered_symlink_and_non_tmp_paths_are_refused():
    root, repo = _repo()
    plain = Path("/private/tmp/%s-a-plain" % root.name)
    link = Path("/private/tmp/%s-a-link" % root.name)
    try:
        plain.mkdir()
        (plain / "keep").write_text("x")
        refused = _run(repo, root.name + "-a", "remove", str(plain), "--force")
        assert refused.returncode == 2 and "not_registered" in refused.stderr
        assert (plain / "keep").exists()
        link.symlink_to(plain)
        assert _run(repo, root.name + "-a", "remove", str(link), "--force").returncode == 2
        assert _run(repo, root.name + "-a", "add", str(Path.home() / (root.name + "-a-x"))).returncode == 2
        assert _run(repo, "", "add", str(plain) + "-new").returncode == 2
    finally:
        link.unlink(missing_ok=True)
        _cleanup(root, plain)
