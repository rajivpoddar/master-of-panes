#!/usr/bin/env python3
"""Trusted shared CI inputs that main changed between two commits (read-only, local git only).

Used by pm-admit-ci.sh's lagging-ref rule (CTO REVISE of MoP 9518bf9d, C0ALZJHGE49 thread
1795567441.556059, ts 1795586027.634509): a PR's test merge built on an older main M is only valid
when main's changes since M did not touch anything that decides WHAT CI runs for the PR. A change
to the shared selector (e.g. scripts/ci/change_scope.py, 73499dfb..6ee67558) leaves the old test
merge running the old selection, even when none of the PR's own files overlap.

The set is DISCOVERED from the repository (rule 60: no hand-kept file list), at both commits:
  1. everything under .github/ (workflows and composite actions);
  2. every repo script a pull_request-triggered workflow executes (directly, or through a local
     composite action or reusable workflow it uses): a script file reached as `<interpreter>
     [flags] path` or `./path`, or a bare path to an executable file at the start of a command;
  3. every file those workflows name that sits in the same directory as such a script (selector
     rules and the E2E selection config passed as arguments);
  4. to a fixpoint, the same-directory siblings that an input names by file name or imports by
     module name (classifier rules and configs loaded relative to the script).
Discovery runs at BOTH commits, so a file added or removed by the change is still caught.

Usage: pm-admit-ci-inputs.py --repo <git dir> --base <older main sha> --main <newer main sha>
Prints the changed trusted CI input paths, one per line (nothing when none changed), exit 0.
Any failed or unreadable git read exits 1 with no stdout, so the caller treats it as UNKNOWN.
"""
import argparse
import posixpath
import re
import subprocess
import sys

MAX_SCAN_BYTES = 4 * 1024 * 1024
SCRIPT_EXT = (".py", ".sh", ".bash", ".mjs", ".cjs", ".js", ".mts", ".cts", ".ts")
INTERP = r"(?:python3?|bash|sh|zsh|node|bun(?:[ \t]+run)?|tsx|npx[ \t]+tsx|uv[ \t]+run)"
FLAGS = r"(?:[ \t]+-{1,2}[A-Za-z0-9_=.,:-]+)*"
PATH_CH = r"[A-Za-z0-9_@.+-]"
WORKSPACE = r"(?:\$\{\{[ \t]*github\.workspace[ \t]*\}\}/|\$\{?GITHUB_WORKSPACE\}?/)?"
PATH = r"(" + PATH_CH + r"+(?:/" + PATH_CH + r"+)*)"
# group 1: after an interpreter (+flags) or ./ ; group 2: a bare path that starts a shell command (start of line, `run:`, `|`, `&&`, `;`, `(`)
EXEC_RE = re.compile(
    r"(?:(?<![\w./-])" + INTERP + FLAGS + r"[ \t]+|(?<![\w./-])\./)" + WORKSPACE + PATH
    + r"|(?:^|[;&|(]|\brun:)[ \t]*(?:-[ \t]+)?" + WORKSPACE + PATH,
    re.M,
)
TOKEN_RE = re.compile(r"(?<![\w./@-])" + WORKSPACE + r"((?:\./)?" + PATH_CH + r"+(?:/" + PATH_CH + r"+)+)")
USES_RE = re.compile(r"(?m)^[ \t]*(?:-[ \t]+)?uses:[ \t]*['\"]?\./(\.github/[A-Za-z0-9_@./-]+?)['\"]?[ \t]*(?:#.*)?$")
PR_TRIGGER_RE = re.compile(r"\bpull_request(?:_target)?\b")
IMPORT_RE = re.compile(r"(?m)^[ \t]*(?:from[ \t]+\.?(\w+)|import[ \t]+([^\n#]+))")


def git(repo, *args, data=None):
    return subprocess.run(["git", "-C", repo, *args], input=data, capture_output=True, check=True).stdout


def ls_tree(repo, rev):
    """{path: (blob oid, mode)} for every regular blob at rev."""
    files = {}
    for rec in git(repo, "ls-tree", "-r", "-z", "--full-tree", rev).split(b"\0"):
        if not rec:
            continue
        meta, _, path = rec.partition(b"\t")
        mode, kind, oid = meta.decode().split(" ")
        if kind == "blob" and mode in ("100644", "100755"):
            files[path.decode("utf-8", "surrogateescape")] = (oid, mode)
    return files


def read_blobs(repo, oids):
    """{oid: text} for the oids, from one git cat-file --batch process."""
    oids = sorted(set(oids))
    if not oids:
        return {}
    raw = git(repo, "cat-file", "--batch", data=("\n".join(oids) + "\n").encode())
    texts, pos = {}, 0
    for oid in oids:
        end = raw.index(b"\n", pos)
        header = raw[pos:end].split(b" ")
        if len(header) != 3 or header[1] != b"blob" or header[0].decode() != oid:
            raise RuntimeError(f"unreadable blob {oid}")
        size = int(header[2])
        texts[oid] = raw[end + 1:end + 1 + size].decode("utf-8", "replace") if size <= MAX_SCAN_BYTES else ""
        pos = end + 1 + size + 1
    return texts


def _norm(token):
    return token[2:] if token.startswith("./") else token


def pr_scope(github, wf_text):
    """The .github files that run for a pull request: PR-triggered workflows plus every local action or reusable workflow they use."""
    scope = {
        p for p in github
        if p.startswith(".github/workflows/") and p.endswith((".yml", ".yaml"))
        and PR_TRIGGER_RE.search(re.split(r"(?m)^jobs:", wf_text[p], maxsplit=1)[0])
    }
    stack = sorted(scope)
    while stack:
        for ref in USES_RE.findall(wf_text[stack.pop()]):
            for cand in (ref, ref + "/action.yml", ref + "/action.yaml"):
                if cand in github and cand not in scope:
                    scope.add(cand)
                    stack.append(cand)
    return scope


def discover(repo, rev):
    """The trusted CI input paths at rev (see module docstring)."""
    tree = ls_tree(repo, rev)
    github = {p for p in tree if p.startswith(".github/")}
    text = read_blobs(repo, [tree[p][0] for p in github])
    wf_text = {p: text[tree[p][0]] for p in github}
    scoped = [wf_text[p] for p in sorted(pr_scope(github, wf_text))]

    executed = set()
    for body in scoped:
        for m in EXEC_RE.finditer(body):
            if m.group(1):
                t = _norm(m.group(1))
                if t in tree and t.endswith(SCRIPT_EXT):
                    executed.add(t)
            else:
                t = _norm(m.group(2))
                if t in tree and tree[t][1] == "100755":
                    executed.add(t)
    executed = {t for t in executed if not t.startswith(".github/")}
    homes = {posixpath.dirname(p) for p in executed}
    inputs = set(github) | executed
    # files a PR workflow names (any position) that live beside an executed script: selector rules and E2E selection config
    for body in scoped:
        for m in TOKEN_RE.finditer(body):
            t = _norm(m.group(1))
            if t in tree and posixpath.dirname(t) in homes:
                inputs.add(t)

    by_dir = {}
    for p in tree:
        by_dir.setdefault(posixpath.dirname(p), []).append(p)
    scanned, level = set(), sorted(p for p in inputs if not p.startswith(".github/"))
    while level:  # breadth-first: one batched blob read per level, one token set per source
        scanned.update(level)
        bodies = read_blobs(repo, [tree[p][0] for p in level])
        found = []
        for src in level:
            body = bodies[tree[src][0]]
            words = set(re.findall(r"[A-Za-z0-9_@.+-]+", body))
            words |= {w.rstrip(".") for w in words if w.endswith(".")}
            modules = set()
            for frm, imp in IMPORT_RE.findall(body):
                modules.add(frm)
                modules.update(re.findall(r"\w+", imp))
            for sib in by_dir.get(posixpath.dirname(src), []):
                if sib in inputs:
                    continue
                name = posixpath.basename(sib)
                if name in words or (name.endswith(".py") and name[:-3] in modules):
                    inputs.add(sib)
                    found.append(sib)
        level = sorted(found)
    return inputs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--main", required=True)
    a = ap.parse_args()
    for rev in (a.base, a.main):
        if not re.fullmatch(r"[0-9a-f]{40}", rev):
            print(f"not a full sha: {rev}", file=sys.stderr)
            return 1
    changed = {
        p.decode("utf-8", "surrogateescape")
        for p in git(a.repo, "diff", "--no-renames", "--name-only", "-z", a.base, a.main, "--").split(b"\0")
        if p
    }
    if not changed:
        return 0
    inputs = discover(a.repo, a.base) | discover(a.repo, a.main)
    hit = sorted(changed & inputs)
    if hit:
        sys.stdout.write("\n".join(hit) + "\n")
    print(f"ci_inputs={len(inputs)} changed={len(changed)} changed_inputs={len(hit)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # any unreadable git state is UNKNOWN to the caller (fail closed)
        print(f"pm-admit-ci-inputs: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
