"""Exercise the admission merge decision with real Git history and no GitHub writes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).parents[1] / "shared-assets/claude/scripts/ci/pm-admit-ci.sh"


@pytest.mark.parametrize("case", [
    "disjoint", "overlap", "local_conflict", "github_conflict", "no_update", "update_pending",
])
def test_admission_only_requests_required_main_merges(tmp_path, case):
    origin = tmp_path / "origin"
    origin.mkdir()
    real_git = shutil.which("git")

    def git(*args):
        return subprocess.check_output([real_git, *args], cwd=origin, text=True).strip()

    git("init", "-q", "-b", "main")
    for key, value in [("user.name", "Fixture"), ("user.email", "fixture@example.invalid"),
                       ("commit.gpgsign", "false")]:
        git("config", key, value)
    shared = origin / "shared file.txt"
    lines = [f"line {i}\n" for i in range(12)]
    shared.write_text("".join(lines))
    git("add", ".")
    git("commit", "-qm", "base")
    git("switch", "-qc", "topic")
    shared.write_text("topic\n" + "".join(lines[1:]))
    git("commit", "-qam", "topic edit")
    head = git("rev-parse", "HEAD")
    git("update-ref", "refs/pull/123/head", head)
    git("switch", "-q", "main")
    if case in {"disjoint", "github_conflict"}:
        (origin / "main.txt").write_text("main only\n")
    elif case == "local_conflict":
        shared.write_text("main\n" + "".join(lines[1:]))
    else:
        shared.write_text("".join(lines[:-1]) + "main\n")
    git("add", ".")
    git("commit", "-qm", "main edit")
    merged = ""
    if case == "overlap":
        git("switch", "-q", "topic")
        git("merge", "-q", "--no-ff", "--no-edit", "main")
        merged = git("rev-parse", "HEAD")
        git("switch", "-q", "main")

    checkout = tmp_path / "checkout"
    git("clone", "-q", str(origin), str(checkout))
    wrapper = tmp_path / SCRIPT.name
    shutil.copyfile(SCRIPT, wrapper)
    (tmp_path / "pm-admit-12b.py").write_text(
        'import os, sys\nwith open(os.environ["CALLS"], "a") as log:\n'
        '    if "--overlap-only" not in sys.argv: print("12b " + sys.argv[-1], file=log)\n'
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    def stub(name, script):
        path = bin_dir / name
        path.write_text(script)
        path.chmod(0o755)

    stub("git", '#!/bin/bash\nexec "$REAL_GIT" -C "$CHECKOUT" "${@:3}"\n')
    stub("sleep", "#!/bin/sh\nexit 0\n")
    stub("gh", f"#!{sys.executable}\n" + '''
import json, os, subprocess, sys
args = sys.argv[1:]
case = os.environ["CASE"]
with open(os.environ["CALLS"], "a") as log:
    print(json.dumps(args), file=log)
git = [os.environ["REAL_GIT"], "-C", os.environ["ORIGIN"]]
if args[:2] == ["pr", "view"]:
    field = args[args.index("--json") + 1]
    head = subprocess.check_output(git + ["rev-parse", "refs/pull/123/head"], text=True).strip()
    # The CLI owns MERGEABLE; derive GitHub's verdict from the fixture case instead.
    mergeable = "CONFLICTING" if case == "github_conflict" else "MERGEABLE"
    print({"headRefOid": head, "headRefOid,mergeable": f"{head} {mergeable}",
           "state": "OPEN", "closingIssuesReferences": "42", "baseRefName": "main",
           "labels": '{"labels":[]}', "labels,closingIssuesReferences": '{"labels":[{"name":"P2"}]}'}[field])
elif args[:3] == ["api", "-X", "PUT"]:
    if case == "update_pending":
        sys.exit(0)  # Accepted, but no new head is visible yet.
    if not os.environ["MERGED"]:
        sys.exit(1)  # GitHub cannot resolve the conflict itself.
    subprocess.run(git + ["update-ref", "refs/pull/123/head", os.environ["MERGED"]], check=True)
elif args[:2] not in (["label", "create"], ["pr", "edit"]):
    sys.exit(1)
''')
    (tmp_path / ".claude/logs").mkdir(parents=True)
    calls = tmp_path / "calls"
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", HOME=str(tmp_path),
               TMPDIR=str(tmp_path), CALLS=str(calls), REAL_GIT=real_git, CHECKOUT=str(checkout),
               ORIGIN=str(origin), MERGED=merged, CASE=case, PM_ADMIT_RETRY_SLEEP="0")
    flags = ["--no-update"] if case == "no_update" else []
    result = subprocess.run(["bash", str(wrapper), "123", *flags], env=env,
                            capture_output=True, text=True, timeout=30)
    log = calls.read_text().splitlines()
    requests = [json.loads(line) for line in log if "update-branch" in line]
    lines = result.stdout.splitlines()
    output = result.stdout + result.stderr

    if case == "disjoint":
        assert requests == []
        assert "MAIN_MERGE: skipped reason=no_conflict_no_overlap" in lines, output
        assert result.returncode == 0 and f"ADMITTED #123 at {head}" in lines
    elif case == "no_update":
        assert requests == []
        assert result.returncode == 2, output
        assert "REFUSE: --no-update cannot admit this head; main integration required reason=overlap files=1" in lines
        assert not any(line.startswith("ADMITTED ") for line in lines)
        gh_calls = [json.loads(line) for line in log if line.startswith("[")]
        assert not any(call[:2] in (["label", "create"], ["pr", "edit"]) for call in gh_calls)
    else:
        assert requests == [["api", "-X", "PUT", "repos/Scribie/heydonna-app/pulls/123/update-branch",
                             "-f", f"expected_head_sha={head}"]], output
        if case == "overlap":
            assert "MAIN_MERGE: performed reason=overlap files=1" in lines
            label_write = next(i for i, line in enumerate(log) if f"ci-head:{merged}" in line
                               and "--add-label" in line)
            assert log.index(f"12b {merged}") < label_write
            assert result.returncode == 0 and f"ADMITTED #123 at {merged}" in lines
        else:
            reason = ("required main merge not confirmed; retry admission or route to slot"
                      if case == "update_pending" else "conflict with main; route to slot")
            assert result.returncode == 2 and f"REFUSE: {reason}" in lines
            assert not any("MAIN_MERGE: performed" in line or "ADMITTED" in line for line in lines)
            assert not any("ci-head:" in call for call in log)
