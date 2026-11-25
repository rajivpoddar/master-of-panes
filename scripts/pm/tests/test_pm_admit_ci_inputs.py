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


# --- trusted reads from any directory (CTO REVISE #2 of MoP 494a207e, C0ALZJHGE49 thread 1795567441.556059, ts 1795587520.404159) ---
# ci.yml reads scripts/ci/unit-shards/durations.json from WORKFLOW_SHA: a data file in a subdirectory that is neither executed nor
# beside an executed script, so the first discovery missed it. A file a PR workflow reads out of a git revision is a trusted input
# from ANY directory; a read that cannot be resolved to a file is UNKNOWN (exit 1, no output).
TRUSTED_WORKFLOW = """name: trusted
on:
  pull_request:
jobs:
  select:
    runs-on: ubuntu-latest
    steps:
      - name: Prepare selector
        run: |
          prepare_selector() {
            for item in change_scope.py unit-areas.json; do
              git show "$WORKFLOW_SHA:scripts/ci/$item" > "$trusted/$item" || return
            done
            git show "$WORKFLOW_SHA:scripts/ci/unit-shards/durations.json" > "$trusted/durations.json"
          }
      - name: Receipt tooling
        run: |
          for path in scripts/receipts/tool.py config/receipt-rules.json; do
            git show "$WORKFLOW_SHA:$path" > "$tools/$path" || exit 0
          done
      - name: Gate
        run: |
          gh api "/repos/$R/contents/scripts/api/gate.py?ref=${WORKFLOW_SHA}" > "$dest"
          python3 -I "$TOOL" fetch --repo "$R" --ref "$WORKFLOW_SHA" \\
            --path scripts/fetched/selector.json --out "$RUNNER_TEMP/selector.json"
      - name: Materialize
        run: |
          SCRIPT="scripts/pinned/check.sh"
          git show "HEAD:$SCRIPT" > "$SCRIPT"
          git archive "$WORKFLOW_SHA:scripts/bundle" | tar -x -C "$dest"
      - name: Not reads
        run: |
          # git show "$WORKFLOW_SHA:scripts/commented/out.json" is a comment, not a read
          cat docs/notes.md
          node scripts/e2e/emit.mjs \\
            --run-id x
"""
TRUSTED_FILES = {
    **BASE_FILES,
    ".github/workflows/trusted.yml": TRUSTED_WORKFLOW,
    "scripts/ci/unit-areas.json": "{}\n",
    "scripts/ci/unit-shards/durations.json": '{"a": 1}\n',
    "scripts/receipts/tool.py": "print('t')\n",
    "config/receipt-rules.json": "{}\n",
    "scripts/api/gate.py": "print('g')\n",
    "scripts/fetched/selector.json": "{}\n",
    "scripts/pinned/check.sh": "#!/bin/sh\n",
    "scripts/bundle/a.txt": "a\n",
    "scripts/bundle/sub/b.txt": "b\n",
    "scripts/e2e/emit.mjs": "console.log(1)\n",
    "docs/notes.md": "notes\n",
    "data/unreferenced.json": "{}\n",
}


@pytest.fixture
def trusted_repo(tmp_path):
    r = tmp_path / "trusted"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, TRUSTED_FILES)
    return r


def workflow_with(snippet):
    """A pull_request workflow whose one run block holds snippet."""
    body = "".join("          " + line + "\n" for line in snippet.splitlines())
    return "on:\n  pull_request:\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - run: |\n" + body


@pytest.mark.parametrize("path, why", [
    ("scripts/ci/unit-shards/durations.json", "the CTO case: a literal git show of a subdirectory data file at the workflow commit"),
    ("scripts/ci/unit-areas.json", "git show with a path prefix and a for-loop variable"),
    ("scripts/receipts/tool.py", "a for-loop literal list of paths, outside the directory of any executed script"),
    ("config/receipt-rules.json", "the same loop, a different top-level directory"),
    ("scripts/api/gate.py", "a contents-API read at ?ref="),
    ("scripts/fetched/selector.json", "the --path of a statement that names a --ref"),
    ("scripts/pinned/check.sh", "a variable bound by a literal assignment"),
    ("scripts/bundle/sub/b.txt", "a directory read (git archive REV:dir) reads every file in it"),
    ("scripts/e2e/emit.mjs", "an interpreter at the start of a line in a multi-line run block (it used to swallow its script argument)"),
])
def test_a_changed_trusted_read_is_listed_from_any_directory(trusted_repo, path, why):
    rc, out = changed_inputs(trusted_repo, {path: TRUSTED_FILES[path] + "changed\n"})
    assert (rc, out) == (0, [path]), why


def test_a_durations_only_change_is_refused_and_unrelated_drift_passes(trusted_repo):
    # one refusal and one pass, the two outcomes the CTO asked for; the commented-out read in the fixture must not make discovery fail
    refused = changed_inputs(trusted_repo, {"scripts/ci/unit-shards/durations.json": '{"a": 2}\n'})
    assert refused == (0, ["scripts/ci/unit-shards/durations.json"])
    passed = changed_inputs(trusted_repo, {"convex/schema.ts": "export const a = 9\n", "app/page.tsx": "export const y = 1\n",
                                           "docs/notes.md": "changed\n", "data/unreferenced.json": '{"a": 1}\n'})
    assert passed == (0, [])  # negative fixture (rule 60): a file only named (cat docs/notes.md) or never referenced is not an input


def test_a_trusted_read_a_change_adds_is_listed_with_its_file(repo):
    # discovery runs at BOTH commits: the new read and the new file exist only at main
    rc, out = changed_inputs(repo, {
        ".github/workflows/ci.yml": PR_WORKFLOW + '      - run: git show "$WORKFLOW_SHA:scripts/new/data.json" > out\n',
        "scripts/new/data.json": "{}\n",
    })
    assert (rc, out) == (0, [".github/workflows/ci.yml", "scripts/new/data.json"])


def test_a_trusted_file_deleted_while_the_workflow_still_reads_it_is_unknown(trusted_repo):
    assert changed_inputs(trusted_repo, {"scripts/ci/unit-shards/durations.json": None}) == (1, [])


UNRESOLVED_READS = {
    "an unbound variable": 'git show "$WORKFLOW_SHA:$mystery" > out',
    "a for-loop over a command substitution": 'for x in $(ls scripts); do\n  git show "$WORKFLOW_SHA:scripts/$x" > out\ndone',
    "a variable reassigned after its loop": 'for p in scripts/ci/run-shards.sh; do\n  p="$(pick)"\n  git show "$WORKFLOW_SHA:$p" > out\ndone',
    "a non-literal assignment": 'p="$(pick_file)"\ngit show "$WORKFLOW_SHA:$p" > out',
    "a literal path that is not in the tree": 'git show "$WORKFLOW_SHA:scripts/missing.json" > out',
    "a contents read of a missing file": 'gh api "/repos/$R/contents/scripts/gone.json?ref=main" > out',
}


@pytest.mark.parametrize("why", sorted(UNRESOLVED_READS))
def test_an_unresolved_trusted_read_fails_closed(repo, why):
    # "unresolved inputs failing closed": UNKNOWN (exit 1, no stdout), never a guess that nothing changed
    rc, out = changed_inputs(repo, {".github/workflows/unresolved.yml": workflow_with(UNRESOLVED_READS[why]),
                                    "convex/schema.ts": "export const a = 4\n"})
    assert (rc, out) == (1, []), why


def test_a_trusted_read_that_cannot_leave_github_needs_no_resolution(repo):
    # the check-recovery-runner-isolation.yml form: every .github file is an input already, so a loop over a git listing of it is covered
    snippet = ('WORKFLOW_DIR=".github/workflows"\n'
               'for blob in $(git ls-tree --name-only "HEAD:$WORKFLOW_DIR"); do\n'
               '  git show "HEAD:$WORKFLOW_DIR/$blob" > "$WORKFLOW_DIR/$blob"\n'
               'done')
    rc, out = changed_inputs(repo, {".github/workflows/materialize.yml": workflow_with(snippet), "convex/schema.ts": "export const a = 5\n"})
    assert (rc, out) == (0, [".github/workflows/materialize.yml"])
