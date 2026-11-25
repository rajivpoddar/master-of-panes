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
     module name (classifier rules and configs loaded relative to the script);
  5. every file a PR-triggered workflow READS OUT OF A GIT REVISION, from any directory (CTO REVISE of
     MoP 494a207e, ts 1795587520.404159: ci.yml reads scripts/ci/unit-shards/durations.json from
     WORKFLOW_SHA, a data file that is neither executed nor beside an executed script). A trusted
     read is a `git show|cat-file|archive|ls-tree` command (global options such as `-C dir` or
     `--no-pager` allowed; text inside a quoted string or a comment is not a command), a
     contents-API `contents/PATH?ref=`, or the `--path PATH` of a statement that names a `--ref`.
     The FULL operand is resolved (CTO REVISE of MoP ce4f381b, ts 1795629066.866119: a revision and
     path held together in one shell variable were silently skipped): `REV:PATH` is split after
     every variable in the operand is expanded, so `SPEC="$WORKFLOW_SHA:scripts/x.json"; git show
     "$SPEC"` reads scripts/x.json. A variable takes its value from the nearest earlier literal
     assignment or `for VAR in a b c; do` loop in the SAME step (the value may itself use other
     variables, expanded the same way), else from the step's, job's or workflow's `env:` entry (a
     `${{ github.workflow_sha }}` style expression is a revision, never a path); every expansion is
     an input, and a directory read lists every file in it. A recognized read is UNRESOLVED, and any
     UNRESOLVED read makes the whole run UNKNOWN (exit 1, no stdout, fail closed), when: a variable
     or expression in its path has no literal value (unbound, set by `$(...)`, `read`, `+=`, a
     glob or an env expression); an operand is a bare variable that is not known to be a revision
     (it may hold `REV:PATH`); an operand is a command substitution; `cat-file --batch` takes its
     objects from stdin; the git read sits inside `eval` or `sh -c`; or the path is not a file or
     directory at that commit. A read that cannot leave `.github/` needs no resolution (every
     `.github` file is already an input).
     Not recognized, by definition: files a workflow reads from the PR's own checkout, a command
     name held in a variable, and git commands other than show, cat-file, archive and ls-tree.
Discovery runs at BOTH commits, so a file added or removed by the change is still caught.

Usage: pm-admit-ci-inputs.py --repo <git dir> --base <older main sha> --main <newer main sha>
Prints the changed trusted CI input paths, one per line (nothing when none changed), exit 0.
Any failed or unreadable git read, or an UNRESOLVED trusted read, exits 1 with no stdout (the reason
goes to stderr), so the caller treats it as UNKNOWN.
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
# The bare-path alternative must not consume an interpreter word: at the start of a line `node scripts/x.mjs`
# would otherwise match `node` there and the script argument would never be seen (multi-line `run: |` blocks).
EXEC_RE = re.compile(
    r"(?:(?<![\w./-])" + INTERP + FLAGS + r"[ \t]+|(?<![\w./-])\./)" + WORKSPACE + PATH
    + r"|(?:^|[;&|(]|\brun:)[ \t]*(?:-[ \t]+)?" + WORKSPACE + r"(?!" + INTERP + r"[ \t])" + PATH,
    re.M,
)
# Trusted reads (rule 5). A resolved operand is a string in which every variable or expression with no literal value is
# a MARK-delimited marker (MARK + payload + MARK); a payload never holds a colon, so the first colon splits REV from PATH.
MARK = "\x00"
MAX_ALTERNATIVES = 256
MAX_DEPTH = 8
GIT_READ_RE = re.compile(
    r"(?<![\w.$-])(?:/[\w./-]*/)?git"
    r"(?:[ \t]+(?:(?:-C|-c|--git-dir|--work-tree|--namespace|--super-prefix|--config-env)[ \t]+\S+|--[a-z][a-z-]*(?:=\S*)?|-[pP]))*"
    r"[ \t]+(show|cat-file|archive|ls-tree)(?![\w-])"
)
# a git read inside `eval "..."` or `sh -c '...'` is a quoted string, so its operands are never resolved: UNRESOLVED
EVAL_GIT_RE = re.compile(r"(?<![\w.$-])(?:eval|(?:ba|z|da|k)?sh[ \t]+-\w*c)(?![\w-])[^\n]*\bgit\b[^\n]*?[ \t](?:show|cat-file|archive|ls-tree)(?![\w-])")
CONTENTS_RE = re.compile(r"/contents/([^\s\"'`?]*)\?[^\s\"']*\bref=")
OPT_PATH_RE = re.compile(r"--path(?:[ \t]+|=)")
REF_STMT_RE = re.compile(r"--ref\b|WORKFLOW_SHA|workflow[_-]sha")
FOR_RE = re.compile(r"\bfor[ \t]+([A-Za-z_]\w*)[ \t]+in[ \t]+([^;\n]*?)[ \t]*(?:;[ \t]*|\n[ \t]*)do\b")
ASSIGN_RE = re.compile(r"(?<![\w$.-])([A-Za-z_]\w*)(\+?)=")
READ_RE = re.compile(r"(?<![\w$.-])read(?![\w-])([^\n;|&<>]*)")
REV_CMD_RE = re.compile(r"[\"']?\$\([ \t]*git[ \t]+(?:-C[ \t]+\S+[ \t]+)?(?:rev-parse|merge-base|rev-list|describe)(?![\w-])")
PIECE_RE = re.compile(r"\$\{\{(.*?)\}\}|\$\{([A-Za-z_]\w*)\}|\$([A-Za-z_]\w*|[0-9@*#?!])")
SIMPLE_VAR_RE = re.compile(r"\$(?:[A-Za-z_]\w*|[0-9@*#?!])")
MARKER_RE = re.compile(MARK + r"([^" + MARK + r"]*)" + MARK)
# payloads that are a revision, never a path: GitHub's sha/ref expressions (and `a || b` of them), the GitHub-provided GITHUB_SHA style
# variables, and a variable set from `$(git rev-parse|merge-base|rev-list|describe ...)`
REV_TERM = r"(?:github\.(?:sha|workflow_sha|base_ref|head_ref|ref|ref_name|event\.(?:before|after|pull_request\.(?:base|head)\.(?:sha|ref)|merge_group\.(?:base|head)_(?:sha|ref)))|GITHUB_(?:SHA|REF|REF_NAME|BASE_REF|HEAD_REF|WORKFLOW_SHA))"
REV_PAYLOAD_RE = re.compile(r"gh=" + REV_TERM + r"(?: \|\| " + REV_TERM + r")*|cmd=rev")
GH_VARS = {"GITHUB_SHA", "GITHUB_REF", "GITHUB_REF_NAME", "GITHUB_BASE_REF", "GITHUB_HEAD_REF", "GITHUB_WORKFLOW_SHA"}
REV_SUFFIX_RE = re.compile(r"(?:\^\{[a-z]*\}|~\d*|\^\d*|@\{[^}]*\})$")
REV_PREFIX_RE = re.compile(r"(?:origin/|refs/(?:heads|tags|remotes|pull)/|remotes/|tags/)")
OPTION_VALUE = {"archive": {"--format", "-o", "--output", "--prefix", "--remote", "--exec"}}
ENV_KEY_RE = re.compile(r"(?m)^([ \t]*)env:[ \t]*$")
ENV_ITEM_RE = re.compile(r"([ \t]+)([A-Za-z_]\w*):[ \t]*(.*?)[ \t]*$")
STEPS_RE = re.compile(r"(?m)^([ \t]*)steps:[ \t]*$")
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


def code_mask(text):
    """text with the inside of quoted strings and trailing comments blanked (same length), so a command is only found where the shell
    would run it: `git show` in echo "git show failed" is a message, but `$(git show ...)` inside a double quote is code. Quote state
    resets at each newline, so a stray apostrophe in a comment can only expose more text, never hide a later command."""
    lines = []
    for line in text.split("\n"):
        out, stack, i, n = [], [], 0, len(line)
        while i < n:
            c, top = line[i], (stack[-1] if stack else "")
            if top == "'":
                out.append(c if c == "'" else "\x01")
                if c == "'":
                    stack.pop()
            elif top == '"':
                if c == "\\" and i + 1 < n:
                    out.append("\x01\x01")
                    i += 1
                elif c == '"':
                    out.append(c)
                    stack.pop()
                elif c == "$" and line.startswith("$(", i):
                    out.append("$(")
                    stack.append("(")
                    i += 1
                elif c == "`":
                    out.append(c)
                    stack.append("`")
                else:
                    out.append("\x01")
            elif c == "\\" and i + 1 < n:
                out.append(line[i:i + 2])
                i += 1
            elif c in "'\"":
                out.append(c)
                stack.append(c)
            elif c == "`":
                out.append(c)
                if top == "`":
                    stack.pop()
                else:
                    stack.append("`")
            elif c == "(":
                out.append(c)
                stack.append("(")
            elif c == ")":
                out.append(c)
                if top == "(":
                    stack.pop()
            elif c == "#" and (i == 0 or line[i - 1] in " \t;&|("):
                out.append("\x01" * (n - i))
                break
            else:
                out.append(c)
            i += 1
        lines.append("".join(out))
    return "\n".join(lines)


def skip_parens(text, i):
    """Index just after the parenthesised group whose '(' is the first one at or after i (quotes inside are not interpreted)."""
    depth, j = 0, text.find("(", i)
    if j < 0:
        return len(text)
    while j < len(text):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return len(text)


def dollar(text, i):
    """(piece, end) for the expansion that starts at text[i] == '$'. piece is None when it cannot be resolved statically (command
    substitution, arithmetic, a parameter operator such as ${x:-y} or ${#x}); a `${{ expr }}` or variable reference is kept as is."""
    if text.startswith("${{", i):
        j = text.find("}}", i)
        return (None, len(text)) if j < 0 else (text[i:j + 2], j + 2)
    if text.startswith("$(", i):
        return None, skip_parens(text, i)
    if text.startswith("${", i):
        j = text.find("}", i)
        if j < 0:
            return None, len(text)
        name = text[i + 2:j]
        return ("${" + name + "}" if re.fullmatch(r"[A-Za-z_]\w*", name) else None), j + 1
    m = SIMPLE_VAR_RE.match(text, i)
    return (m.group(0), m.end()) if m else ("$", i + 1)


def shell_word(text, i):
    """(template, end) for the shell word starting at text[i]. Quotes and escapes are removed; a variable reference or `${{ expr }}`
    stays in the template. The template is None when the word is not statically resolvable: a command substitution or backtick, a
    parameter operator, an unquoted glob, or a `$` inside single quotes."""
    n, out, opaque = len(text), [], False
    while i < n and text[i] not in " \t\n;&|<>)":
        c = text[i]
        if c == "$":
            piece, i = dollar(text, i)
            if piece is None:
                opaque = True
            else:
                out.append(piece)
        elif c == "`":
            j = text.find("`", i + 1)
            i, opaque = (n if j < 0 else j + 1), True
        elif c == "\\":
            if i + 1 < n and text[i + 1] != "\n":
                out.append(text[i + 1])
            i += 2
        elif c == "'":
            j = text.find("'", i + 1)
            j = n if j < 0 else j
            opaque = opaque or "$" in text[i + 1:j]
            out.append(text[i + 1:j])
            i = j + 1
        elif c == '"':
            i += 1
            while i < n and text[i] != '"':
                d = text[i]
                if d == "$":
                    piece, i = dollar(text, i)
                    if piece is None:
                        opaque = True
                    else:
                        out.append(piece)
                elif d == "`":
                    j = text.find("`", i + 1)
                    i, opaque = (n if j < 0 else j + 1), True
                elif d == "\\" and i + 1 < n and text[i + 1] in '"\\$`':
                    opaque = opaque or text[i + 1] in "$`"
                    out.append(text[i + 1])
                    i += 2
                else:
                    out.append(d)
                    i += 1
            i += 1
        else:
            opaque = opaque or c in "*?["
            out.append(c)
            i += 1
    return (None if opaque else "".join(out)), min(i, n)


def shell_words(text, i):
    """[(template, start, end)] for the words from text[i] to the end of the simple command (an unquoted ; & | < > ) or a newline)."""
    words, n = [], len(text)
    while True:
        while i < n and text[i] in " \t":
            i += 1
        if i >= n or text[i] in "\n;&|<>)":
            return words
        template, end = shell_word(text, i)
        if end <= i:
            return words
        if not (template is not None and template.isdigit() and text[end:end + 1] in ("<", ">")):  # `2>/dev/null` is a redirect, not an operand
            words.append((template, i, end))
        i = end


def step_spans(text):
    """[(start, end)] offsets of every item of a `steps:` list."""
    spans = []
    for m in STEPS_RE.finditer(text):
        base, item_indent, start, pos = len(m.group(1)), None, None, m.end() + 1
        while pos < len(text):
            eol = text.find("\n", pos)
            eol = len(text) if eol < 0 else eol
            line = text[pos:eol]
            if line.strip():
                indent = len(line) - len(line.lstrip(" \t"))
                item = line.lstrip().startswith("-")
                if item_indent is None:
                    if not item or indent < base:
                        break
                    item_indent = indent
                if indent < item_indent or (indent == item_indent and not item):
                    break
                if indent == item_indent:
                    if start is not None:
                        spans.append((start, pos))
                    start = pos
            pos = eol + 1
        if start is not None:
            spans.append((start, min(pos, len(text))))
    return spans


def yaml_envs(text, steps):
    """[(name, value, step span or None)] for every `env:` mapping: a step's own carries that step, a job's or the workflow's None."""
    found = []
    for m in ENV_KEY_RE.finditer(text):
        base = len(m.group(1))
        span = next(((lo, hi) for lo, hi in steps if lo <= m.start() < hi), None)
        pos = m.end() + 1
        while pos < len(text):
            eol = text.find("\n", pos)
            eol = len(text) if eol < 0 else eol
            line = text[pos:eol]
            if line.strip():
                item = ENV_ITEM_RE.fullmatch(line)
                if item is None or len(item.group(1)) <= base:
                    break
                value = item.group(3)
                if len(value) >= 2 and value[0] in "'\"" and value[-1] == value[0]:
                    value = value[1:-1]
                else:
                    value = re.sub(r"[ \t]+#.*$", "", value)
                if value and value[0] not in "|>":
                    found.append((item.group(2), value, span))
            pos = eol + 1
    return found


class Scope:
    """A workflow file's variables: what literal values a variable can have where a trusted read sits (its own step, else env:)."""

    def __init__(self, text):
        self.text = text
        self.mask = code_mask(text)  # bindings are only found where the shell would run them, never inside a quoted string
        self.steps = step_spans(text)
        self.envs = yaml_envs(text, self.steps)

    def span(self, pos):
        return next(((lo, hi) for lo, hi in self.steps if lo <= pos < hi), None)

    def expand(self, template, pos, depth=0):
        """Every string `template` can expand to at pos. A piece with no literal value is a MARK marker (payload: the variable name,
        `gh=<expression>`, `cmd=rev` for a revision from git, or `opaque`)."""
        out, i = [""], 0
        for m in PIECE_RE.finditer(template):
            if m.group(1) is not None:
                alts = [MARK + "gh=" + re.sub(r"\s+", " ", m.group(1).strip()).replace(":", "~") + MARK]
            else:
                alts = self.bind(m.group(2) or m.group(3), pos, depth)
            out = [o + template[i:m.start()] + a for o in out for a in alts]
            if len(out) > MAX_ALTERNATIVES:
                return [MARK + "many" + MARK]
            i = m.end()
        return [o + template[i:] for o in out]

    def bind(self, name, pos, depth):
        """The values VAR can hold at pos: the nearest earlier binding in the same step (`for VAR in ...; do`, `VAR=word`; a `read`
        or `+=` is opaque), else its env: entry, else a GitHub-provided variable, else an unbound marker."""
        opaque = [MARK + "opaque" + MARK]
        if depth > MAX_DEPTH:
            return opaque
        span = self.span(pos)
        lo = span[0] if span else 0
        best = None  # (offset, kind, match)
        for kind, matches in (("for", FOR_RE.finditer(self.mask, lo, pos)), ("assign", ASSIGN_RE.finditer(self.mask, lo, pos)),
                              ("read", READ_RE.finditer(self.mask, lo, pos))):
            for m in matches:
                named = name in re.findall(r"[A-Za-z_]\w*", m.group(1)) if kind == "read" else m.group(1) == name
                if named and (best is None or m.start() > best[0]):
                    best = (m.start(), kind, m)
        if best is None:
            return self.env_values(name, span, depth)
        _, kind, m = best
        if kind == "read" or (kind == "assign" and m.group(2)):
            return opaque
        if kind == "for":
            alts = []
            for template, _, _ in shell_words(self.text[m.start(2):m.end(2)], 0):
                alts += opaque if template is None else self.expand(template, m.start(), depth + 1)
            return alts or opaque
        template, _ = shell_word(self.text, m.end())  # the same offsets in the unmasked text
        if template is None:
            return [MARK + "cmd=rev" + MARK] if REV_CMD_RE.match(self.text, m.end()) else opaque
        return self.expand(template, m.start(), depth + 1)

    def env_values(self, name, span, depth):
        own = [v for n, v, sp in self.envs if n == name and sp is not None and sp == span]
        values = own or [v for n, v, sp in self.envs if n == name and sp is None]
        if not values:
            return [MARK + ("gh=" + name if name in GH_VARS else name) + MARK]
        return [alt for v in values for alt in self.expand(v, 0, depth + 1)]


def is_revision(operand):
    """An operand with an unresolved piece that is nevertheless a revision, not a REV:PATH: a rev suffix (^{commit}, ~1, @{1}), a
    ref-name prefix, or only revision-valued variables and expressions."""
    payloads = MARKER_RE.findall(operand)
    return bool(REV_SUFFIX_RE.search(operand) or REV_PREFIX_RE.match(operand)
                or (payloads and all(REV_PAYLOAD_RE.fullmatch(p) for p in payloads)))


def trusted_reads(wf, body, tree):
    """(paths, problems): every file the workflow text reads out of a git revision, with the whole operand resolved (see the module
    docstring, rule 5). A read of a directory is a read of every file in it. A recognized read that cannot be resolved is a problem
    (UNRESOLVED), except a path that cannot leave .github/ (every .github file is an input already)."""
    joined = re.sub(r"(?m)^[ \t]*#.*$", "", re.sub(r"\\[ \t]*\r?\n[ \t]*", " ", body))
    scope = Scope(joined)
    paths, problems = set(), []

    def path_read(shown, resolved):
        if MARK in resolved:
            if not _norm(resolved[:resolved.index(MARK)]).startswith(".github/"):
                problems.append(f"{wf}: '{shown}' has a variable or expression with no literal value in its path")
            return
        v = _norm(resolved).rstrip("/")
        below = [t for t in tree if t.startswith(v + "/")] if v else []
        if v in tree and tree[v][1] in ("100644", "100755"):
            paths.add(v)
        elif below:  # a directory: every file in it is read
            paths.update(below)
        else:
            problems.append(f"{wf}: '{v}' (from '{shown}') is not a file or directory at this commit")

    def object_read(shown, template, pos):  # a git object operand: REV:PATH reads PATH, a bare revision reads no file
        if template is None:
            problems.append(f"{wf}: '{shown}' is a command substitution, so the object it names cannot be resolved")
            return
        for operand in scope.expand(template, pos):
            colon = operand.find(":")
            if colon >= 0:
                path_read(shown, operand[colon + 1:])
            elif MARK in operand and not is_revision(operand) and not _norm(operand[:operand.index(MARK)]).startswith(".github/"):
                problems.append(f"{wf}: '{shown}' is a variable with no literal value; it may hold a REV:PATH")

    def pathspec_read(shown, template, pos):
        if template is None:
            problems.append(f"{wf}: '{shown}' is a command substitution, so the path it names cannot be resolved")
            return
        for resolved in scope.expand(template, pos):
            path_read(shown, resolved)

    masked = scope.mask
    for g in GIT_READ_RE.finditer(masked):  # only where the shell would run it, never inside a quoted string
        sub, objects, specs, ddash, first, skip = g.group(1), [], [], False, True, False
        for template, a, b in shell_words(joined, g.end()):
            if skip:
                skip = False
            elif template == "--" and not ddash:
                ddash = True
            elif template is not None and template.startswith("-") and not ddash:
                skip = template in OPTION_VALUE.get(sub, ())
                if sub == "cat-file" and template.startswith("--batch"):
                    problems.append(f"{wf}: 'git cat-file {template}' reads its objects from stdin, so they cannot be resolved")
            else:
                (specs if ddash or (sub in ("archive", "ls-tree") and not first) else objects).append((joined[a:b], template, a))
                first = False
        for shown, template, pos in objects:
            object_read(shown, template, pos)
        for shown, template, pos in specs:
            pathspec_read(shown, template, pos)
    for m in EVAL_GIT_RE.finditer(joined):
        if masked[m.start()] == joined[m.start()]:  # the eval / sh -c itself is code (its quoted argument is what holds the git read)
            problems.append(f"{wf}: a git read inside eval or sh -c ('{joined[m.start():m.start() + 50]}') cannot be resolved")
    for m in CONTENTS_RE.finditer(joined):
        pathspec_read(m.group(1), None if re.search(r"\$\(|`|[*\[]", m.group(1)) else m.group(1), m.start(1))
    for stmt in re.finditer(r"[^\n]+", joined):
        if REF_STMT_RE.search(stmt.group(0)):
            for m in OPT_PATH_RE.finditer(stmt.group(0)):
                start = stmt.start() + m.end()
                template, end = shell_word(joined, start)
                pathspec_read(joined[start:end], template, start)
    return paths, problems


def discover(repo, rev):
    """(trusted CI input paths, unresolved trusted reads) at rev (see module docstring)."""
    tree = ls_tree(repo, rev)
    github = {p for p in tree if p.startswith(".github/")}
    text = read_blobs(repo, [tree[p][0] for p in github])
    wf_text = {p: text[tree[p][0]] for p in github}
    scope_paths = sorted(pr_scope(github, wf_text))
    scoped = [wf_text[p] for p in scope_paths]
    problems, trusted = [], set()
    for p in scope_paths:
        got, bad = trusted_reads(p, wf_text[p], tree)
        trusted |= got
        problems += bad

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
    return inputs | trusted, problems


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
    inputs, problems = set(), []
    for rev in (a.base, a.main):
        found, bad = discover(a.repo, rev)
        inputs |= found
        problems += [f"{rev[:8]}: {b}" for b in bad]
    if problems:  # an unresolved trusted read could hide a changed input: UNKNOWN, fail closed
        for b in sorted(set(problems)):
            print(f"pm-admit-ci-inputs: unresolved trusted read: {b}", file=sys.stderr)
        return 1
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
