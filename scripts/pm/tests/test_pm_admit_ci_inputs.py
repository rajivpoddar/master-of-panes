"""pm-admit-ci-inputs.py: which trusted shared CI inputs did main change between two commits (real temp git repos)."""
import subprocess
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).parents[1] / "shared-assets/claude/scripts/ci/pm-admit-ci-inputs.py"

PR_WORKFLOW = """name: ci
on:
  pull_request:
jobs:
  scope:
    uses: ./.github/workflows/reusable.yml
  select:
    runs-on: ubuntu-latest
    steps:
      - uses: ./.github/actions/setup
      - run: python3 scripts/ci/change_scope.py --rules scripts/ci/change-scope-rules.json
      - run: bash scripts/ci/run-shards.sh
"""
REUSABLE = """on: workflow_call
jobs:
  j:
    runs-on: ubuntu-latest
    steps:
      - run: python3 scripts/ci/reusable_tool.py
"""
SCHEDULE_ONLY = """on:
  schedule:
    - cron: '0 1 * * *'
jobs:
  nightly:
    runs-on: ubuntu-latest
    steps:
      - run: python3 scripts/nightly_only.py
"""
BASE_FILES = {
    ".github/workflows/ci.yml": PR_WORKFLOW,
    ".github/workflows/reusable.yml": REUSABLE,
    ".github/workflows/nightly.yml": SCHEDULE_ONLY,
    ".github/actions/setup/action.yml": "runs:\n  using: composite\n  steps: []\n",
    "scripts/ci/change_scope.py": "import classifier\nRULES = 'change-scope-rules.json'\n",
    "scripts/ci/classifier.py": "X = 1\n",
    "scripts/ci/change-scope-rules.json": "{}\n",
    "scripts/ci/run-shards.sh": "#!/bin/sh\necho shards\n",
    "scripts/ci/reusable_tool.py": "print('r')\n",
    "scripts/ci/orphan.py": "print('no workflow references me')\n",
    "scripts/nightly_only.py": "print('n')\n",
    "convex/schema.ts": "export const a = 1\n",
    "app/page.tsx": "export default function P() {}\n",
}


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, changes):
    """changes: {path: text or None (delete)}; returns the new commit sha."""
    for rel, text in changes.items():
        path = repo / rel
        if text is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "c", "--allow-empty")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, BASE_FILES)
    return r


def run(repo, base, main):
    p = subprocess.run([sys.executable, "-I", str(HELPER), "--repo", str(repo), "--base", base, "--main", main],
                       capture_output=True, text=True)
    return p.returncode, p.stdout.split()


def changed_inputs(repo, changes):
    base = git(repo, "rev-parse", "HEAD")
    return run(repo, base, commit(repo, changes))


@pytest.mark.parametrize("path, why", [
    ("scripts/ci/change_scope.py", "the shared selector a workflow executes"),
    ("scripts/ci/change-scope-rules.json", "the rules file the workflow passes to it"),
    ("scripts/ci/classifier.py", "a module the selector imports"),
    ("scripts/ci/run-shards.sh", "a script the workflow runs"),
    ("scripts/ci/reusable_tool.py", "a script run by a reusable workflow the PR workflow uses"),
    (".github/workflows/ci.yml", "a workflow"),
    (".github/actions/setup/action.yml", "a local composite action"),
])
def test_changed_trusted_input_is_listed_by_path(repo, path, why):
    rc, out = changed_inputs(repo, {path: BASE_FILES[path] + "# changed\n"})
    assert (rc, out) == (0, [path]), why


def test_unrelated_product_drift_is_not_listed(repo):
    rc, out = changed_inputs(repo, {"convex/schema.ts": "export const a = 2\n", "app/page.tsx": "export const x = 1\n",
                                    "docs/readme.md": "hi\n"})
    assert (rc, out) == (0, [])


def test_only_the_selector_in_a_mixed_drift_is_named(repo):
    rc, out = changed_inputs(repo, {"scripts/ci/change_scope.py": "import classifier\nRULES = 'x'\n",
                                    "convex/schema.ts": "export const a = 3\n"})
    assert (rc, out) == (0, ["scripts/ci/change_scope.py"])


@pytest.mark.parametrize("path", ["scripts/ci/orphan.py", "scripts/nightly_only.py"])
def test_files_no_pull_request_workflow_executes_are_not_inputs(repo, path):
    # negative fixture (rule 60): discovery follows references, so an unreferenced script, or one only a schedule workflow runs, is not an input
    rc, out = changed_inputs(repo, {path: "print('changed')\n"})
    assert (rc, out) == (0, [])


def test_a_script_a_change_adds_is_listed(repo):
    # discovery runs at BOTH commits: the new workflow reference and the new script both exist only at main
    rc, out = changed_inputs(repo, {
        "scripts/ci/new_gate.py": "print('g')\n",
        ".github/workflows/ci.yml": PR_WORKFLOW + "      - run: python3 scripts/ci/new_gate.py\n",
    })
    assert (rc, out) == (0, [".github/workflows/ci.yml", "scripts/ci/new_gate.py"])


def test_a_script_a_change_deletes_is_listed(repo):
    # ... and at base: the deleted selector existed only there
    rc, out = changed_inputs(repo, {"scripts/ci/run-shards.sh": None})
    assert (rc, out) == (0, ["scripts/ci/run-shards.sh"])


def test_empty_range_lists_nothing(repo):
    head = git(repo, "rev-parse", "HEAD")
    assert run(repo, head, head) == (0, [])


@pytest.mark.parametrize("bad", ["HEAD", "abc123", "z" * 40, ""])
def test_a_non_sha_argument_fails_closed_with_no_output(repo, bad):
    head = git(repo, "rev-parse", "HEAD")
    rc, out = run(repo, bad, head)
    assert rc == 1 and out == []


def test_an_unreadable_repo_or_unknown_commit_fails_closed_with_no_output(repo, tmp_path):
    head = git(repo, "rev-parse", "HEAD")
    assert run(tmp_path / "missing", head, head) == (1, [])
    assert run(repo, "0" * 40, head) == (1, [])
