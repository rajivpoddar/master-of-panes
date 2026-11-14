#!/usr/bin/env python3
"""issue-create-owner.py - the single owner of a permit-required `gh issue create`.

Why this exists
---------------
The previous protocol spread one create over several PreToolUse hooks that Claude
Code runs in parallel: pre-issue-create-audit.sh wrote a "pending" file, the
ledger hook validated, and explore-issue-permit-consume-gate.sh consumed the
permit "last". Each hook re-parsed the command and re-read the body file at its
own moment, no hook order is guaranteed, the permit was consumed BEFORE gh ran,
and the rewriting hooks (rtk turns `gh issue create` into `rtk gh issue create`,
rewrite-github-priority-label.py rewrites --label) race each other on
`updatedInput`. A consumed permit with no issue, or an issue with a live permit,
were both reachable.

One process now owns the whole chain, in this order:

  1. buffer    Read the exact --body / --body-file bytes ONCE into memory. That single
               buffer is the only body that is ever validated, digested or sent. It is
               not written anywhere before every gate has passed.
  2. gates     Run the EXISTING gates sequentially on that buffer, which reaches every one
               of them IN MEMORY: pre-issue-create-audit.sh as stage `audit` (base64 in the
               hook's stdin payload) and then the issue-contract-ledger hook(s) (ledger +
               tracker checks; base64 in MOP_ISSUE_CREATE_BODY_B64 with `--body-file -`).
               No gate gets a file, and no file, scratch copy, seal, lock content, in-flight
               record, obligation or draft is written by a gate: in stage `audit` the
               validators suppress their own side effects (Slack cache and fallback log,
               obligation upsert, blocked-issue draft copy) while keeping every decision
               byte-for-byte the one they make for the same bytes in a named body file.
               A rejecting gate therefore writes nothing at all.
  3. bind      Under the ONE shared permit lock (`<permit file>.lock`, the same lock the
               mint, the legacy consume and restore take) re-verify that no create is in
               flight or consumed and that the permit still matches the EXACT raw digest,
               bind source key + invocation id + raw body digest + title digest + argv
               digest, and durably write `<key>.inflight.json` (fsync file and directory)
               BEFORE gh is spawned. The lock is then released: it is never nested and
               never held across gh; the in-flight record, which every other actor checks
               under that same lock, excludes them from then on.
  4. gh        Spawn `gh issue create ... --body-file -` and feed the IDENTICAL buffer on
               stdin. From the in-flight write onward a crash, a timeout or a lost
               response is an UNKNOWN create: reconciliation-only.
  5. verify    Only a zero exit, exactly one canonical issue URL on stdout AND a GitHub
               readback (`gh api repos/<repo>/issues/<n>`) of that issue count: number,
               html_url, repository, not-a-PR, title and body must match what was sent.
               Then, and only then, under the permit lock again, the permit is consumed
               (renamed into the consumed dir with the URL and readback recorded) and the
               in-flight record is removed. Anything else - no URL, a readback that is
               unavailable or mismatches - leaves the in-flight record in place:
               reconciliation-only, never re-mint, never create again.
  6. release   The in-flight record is released ONLY by this process and ONLY when gh
               definitely never started (the spawn itself failed). Nothing else releases
               it: not restore, not a claimed absence, not a retry.

The permit binds the EXACT raw digest: sha256 of the body bytes, trailing newlines and
NUL bytes included (mint it with explore_issue_file_sha256 <body file>).

Raw `gh issue create` for a source that holds a permit, and compound or wrapped
forms of either command, are denied by the PreToolUse hook before any permit write
(see `classify`). ALL compound and wrapped creates are denied upfront, permit or not;
a simple raw create is parsed by `extract` (a byte-preserving parser that knows every
gh flag spelling, -t/-b/-F/--body-file=..., attached values, any quoting) and fails
closed on anything ambiguous or unreadable. The owner command is a plain script path
with no `gh issue` text, and the owner applies the priority-label canonicalisation
itself. The command rewrite handlers are live hooks that run in parallel with the guard
on the SAME payload and are pure functions of it, so the guard DISCOVERS every
PreToolUse Bash handler that can emit updatedInput from the settings files in force
(user, local, managed, every project level from the payload cwd up, enabled plugins) and
DRY-RUNS each one on the owner invocation (`preserve`): the owner command is let through
only when none of them would rewrite, deny or block it. If any would, the guard blocks
BEFORE anything runs or is written and names the handler, so what reaches the shell is
exactly what the agent wrote. No live hook is modified or overwritten for this.

Not covered, on purpose: a hostile process running as the same user. This guards
against accidents, drift and races, not against someone who can edit the state
files directly.

Subcommands
-----------
  create <gh issue create flags>   run the chain (the only agent-facing command)
  classify                         read a PreToolUse JSON payload on stdin and print
                                   {"kind": none|raw_simple|raw_compound|raw_wrapped|
                                            owner_simple|owner_unsafe}
  extract                          (PreToolUse guard only) parse a SIMPLE raw gh issue create from
                                   the payload on stdin: {"ok": true, "title", "has_body",
                                   "body_b64", "body_display"} or {"ok": false, "error_class",
                                   "error"}; ambiguous or unreadable input fails closed
  preserve                         (PreToolUse guard only) discover and dry-run every PreToolUse
                                   Bash handler that can emit updatedInput; print a block
                                   decision unless every one is a no-op on the payload on stdin

Exit codes of `create`
----------------------
  0  filed: URL printed on stdout, readback verified, permit consumed
  2  refused before any state write (gate rejection, bad flags, missing gate)
  3  refused: another create for this source is running or already UNKNOWN/consumed,
     or the permit lock stayed busy
  4  gh could not be started; the in-flight record was released, permit intact
  5  gh was started and the outcome is UNKNOWN (no verified URL, readback unavailable
     or mismatching): reconciliation-only
  6  gh returned a verified URL but the permit bookkeeping failed: reconcile the URL

Environment (all optional; tests point these at temp roots)
-----------------------------------------------------------
  ISSUE_CREATE_OWNER_AUDIT_HOOK      pre-issue-create-audit.sh path
  ISSUE_CREATE_OWNER_LEDGER_HOOKS    os.pathsep list of ledger hook paths (all required)
  ISSUE_CREATE_OWNER_REWRITER_HOOKS  os.pathsep list of hook scripts `preserve` dry-runs INSTEAD of
                                     discovery (test seam)
  ISSUE_CREATE_OWNER_SETTINGS_FILES  os.pathsep list of settings files discovery reads INSTEAD of
                                     the real ones (test seam; plugins are skipped)
  ISSUE_CREATE_OWNER_GH_BIN          gh executable (default: gh)
  ISSUE_CREATE_OWNER_WORK_DIR        post-create hook log directory (written only after a filed create)
  ISSUE_CREATE_OWNER_REPO_RE         allowed `owner/repo` regex for the verified URL
  ISSUE_CREATE_OWNER_GH_TIMEOUT      seconds to wait for gh (default 120)
  ISSUE_CREATE_OWNER_LOCK_WAIT       seconds to wait for the permit lock (default 30)
  ISSUE_CREATE_OWNER_READBACK_DELAY  seconds between readback attempts (default 2, 3 attempts)
  ISSUE_CREATE_OWNER_POST_SWEEP_HOOK post-create sweep hook ('' disables)
  ISSUE_CREATE_OWNER_POST_LINK_HOOK  post-create blocked-by hook ('' disables)
"""
from __future__ import annotations

import base64
import fcntl
import glob
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

OWNER_NAME = "issue-create-owner.py"
HERE = Path(__file__).resolve().parent
DEFAULT_PROJECT_LEDGER_HOOK = "/Users/rajiv/Downloads/projects/heydonna-app/.claude/hooks/block-invalid-issue-contract-ledger.sh"
DEFAULT_REPO_RE = r"(?:Scribie|heydonna-app)/heydonna-app"

EXIT_FILED = 0
EXIT_REFUSED = 2
EXIT_BUSY = 3
EXIT_RELEASED = 4
EXIT_UNKNOWN = 5
EXIT_BOOKKEEPING = 6


class Refusal(Exception):
    """A refusal that happens BEFORE gh can have started."""

    def __init__(self, reason: str, code: int = EXIT_REFUSED) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def now_stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def say(message: str) -> None:
    print(f"ISSUE_CREATE_OWNER: {message}", file=sys.stderr)


def tail(data: bytes, limit: int = 2000) -> str:
    return data[-limit:].decode("utf-8", "replace")


def atomic_write_json(path: Path, value: Any) -> None:
    """Write JSON durably: temp file in the same directory, fsync, rename, fsync dir."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".tmp.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, str(path))
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(str(path)) as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


# ---------------------------------------------------------------------------
# shell-shape classification (used by the PreToolUse hook)
# ---------------------------------------------------------------------------

_PUNCT = "();<>|&\n"
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WRAPPERS = {
    "env", "time", "nohup", "command", "exec", "sudo", "rtk", "builtin", "nice",
    "stdbuf", "timeout", "xargs", "watch", "caffeinate", "unbuffer", "setsid",
}
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
_COUNT = re.compile(r"^[0-9.]+[smhd]?$")


def tokenize(command: str) -> Optional[List[str]]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=_PUNCT)
    lexer.whitespace = " \t\r"  # newline is an operator, not whitespace
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return None


def _is_operator(token: str) -> bool:
    return token != "" and all(char in _PUNCT for char in token)


def _scan_segment(words: List[str], compound: bool, depth: int, found: Dict[str, list]) -> None:
    index = 0
    wrapped = compound
    while True:
        while index < len(words) and _ASSIGN.match(words[index]):
            index += 1
            wrapped = True
        if index >= len(words):
            return
        head = os.path.basename(words[index])
        if head in _WRAPPERS:
            wrapped = True
            index += 1
            while index < len(words) and (
                words[index].startswith("-") or _ASSIGN.match(words[index]) or _COUNT.match(words[index])
            ):
                index += 1
            continue
        break
    head = os.path.basename(words[index])
    rest = words[index + 1:]
    if head == "gh":
        if rest[:2] == ["issue", "create"]:
            found["creates"].append({"direct": not wrapped, "compound": compound})
        return
    if head == OWNER_NAME:
        found["owners"].append({"direct": not wrapped, "subcommand": rest[0] if rest else ""})
        return
    if head.startswith("python") and rest and os.path.basename(rest[0]) == OWNER_NAME:
        found["owners"].append({"direct": not wrapped, "subcommand": rest[1] if len(rest) > 1 else ""})
        return
    if depth >= 3:
        return
    nested: Optional[str] = None
    if head in _SHELLS:
        for offset, word in enumerate(rest):
            if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", word) and offset + 1 < len(rest):
                nested = rest[offset + 1]
                break
    elif head == "eval":
        nested = " ".join(rest)
    if nested:
        inner = scan(nested, depth + 1)
        for item in inner["creates"]:
            found["creates"].append({"direct": False, "compound": item.get("compound", False)})
        for item in inner["owners"]:
            found["owners"].append({"direct": False, "subcommand": item.get("subcommand", "")})


def scan(command: str, depth: int = 0) -> Dict[str, list]:
    found: Dict[str, list] = {"creates": [], "owners": []}
    text = command.replace("\\\n", " ")
    tokens = tokenize(text)
    if tokens is None:
        # Unbalanced quoting: we cannot prove the shape, so treat any mention as wrapped.
        if re.search(r"gh\s+issue\s+create\b", text):
            found["creates"].append({"direct": False, "compound": False})
        if OWNER_NAME in text:
            found["owners"].append({"direct": False, "subcommand": ""})
        return found
    segments: List[List[str]] = []
    current: List[str] = []
    saw_operator = False
    for token in tokens:
        if _is_operator(token):
            saw_operator = True
            segments.append(current)
            current = []
        else:
            current.append(token)
    segments.append(current)
    segments = [segment for segment in segments if segment]
    compound = saw_operator or len(segments) > 1
    for segment in segments:
        _scan_segment(segment, compound, depth, found)
    return found


def classify(command: str) -> str:
    """none | raw_simple | raw_compound | raw_wrapped | owner_simple | owner_unsafe."""
    if "issue" not in command and OWNER_NAME not in command:
        return "none"
    found = scan(command)
    creates, owners = found["creates"], found["owners"]
    if owners:
        if len(owners) == 1 and owners[0]["direct"] and not creates:
            return "owner_simple"
        return "owner_unsafe"
    if creates:
        if len(creates) == 1 and creates[0]["direct"]:
            return "raw_simple"
        return "raw_compound" if any(item.get("compound") for item in creates) else "raw_wrapped"
    return "none"


# ---------------------------------------------------------------------------
# raw simple create: a byte-preserving parser (used by the PreToolUse guard)
# ---------------------------------------------------------------------------

_GH_VALUE_LONG = {
    "--title": "title", "--body": "body", "--body-file": "body_file", "--label": None, "--assignee": None,
    "--milestone": None, "--project": None, "--repo": None, "--template": None, "--recover": None,
}
_GH_BOOL_LONG = {"--web", "--editor", "--help"}
_GH_VALUE_SHORT = {"t": "title", "b": "body", "F": "body_file", "l": None, "a": None, "m": None,
                   "p": None, "R": None, "T": None}
_GH_BOOL_SHORT = {"w", "e", "h"}


def has_unquoted_expansion(text: str) -> bool:
    """True if the shell would expand something we cannot see (so the real argument is unknowable).

    Quote-aware: single quotes hide everything; inside double quotes only `$...` and backticks expand;
    unquoted text also expands globs and brace lists. Any hit means the hook-time text is not what gh
    will receive, so the caller must fail closed.
    """
    quote = ""
    index, length = 0, len(text)
    while index < length:
        char = text[index]
        if quote == "'":
            if char == "'":
                quote = ""
            index += 1
            continue
        if char == "\\":
            index += 2
            continue
        following = text[index + 1] if index + 1 < length else ""
        if quote == '"':
            if char == '"':
                quote = ""
            elif char == "`":
                return True
            elif char == "$" and following and (following.isalnum() or following in "_{(?!@*#$-"):
                return True
            index += 1
            continue
        if char in "'\"":
            quote = char
        elif char == "`":
            return True
        elif char == "$" and following and (following.isalnum() or following in "_{(?!@*#$-'\""):
            return True
        elif char in "*?[":
            return True
        elif char == "{" and re.match(r"\{[^{}\s]*(?:,|\.\.)[^{}\s]*\}", text[index:]):
            return True
        index += 1
    return False


def _extract_failure(error_class: str, error: str, display: str = "") -> Dict[str, Any]:
    return {"ok": False, "error_class": error_class, "error": error, "body_display": display}


def extract_raw_create(command: str, cwd: str) -> Dict[str, Any]:
    """Parse ONE simple raw `gh issue create` into title + exact body bytes, or fail closed."""
    if classify(command) != "raw_simple":
        return _extract_failure("shape", "not a single simple gh issue create")
    text = command.replace("\\\n", " ")
    if has_unquoted_expansion(text):
        return _extract_failure(
            "expansion",
            "the command contains a shell expansion ($VAR, $(...), backticks, glob or brace list) that makes the real "
            "title/body unknowable at hook time",
        )
    tokens = tokenize(text)
    if tokens is None or len(tokens) < 3:
        return _extract_failure("unparseable", "the command could not be tokenized")
    args = tokens[3:]
    title: Optional[str] = None
    body_inline: Optional[str] = None
    body_file: Optional[str] = None
    index = 0
    while index < len(args):
        arg = args[index]
        index += 1
        field: Optional[str] = None
        value: Optional[str] = None
        if arg == "--":
            return _extract_failure("ambiguous", "positional arguments are not accepted by gh issue create")
        if arg.startswith("--"):
            flag, has_inline, inline_value = arg, False, ""
            if "=" in arg:
                flag, inline_value = arg.split("=", 1)
                has_inline = True
            if flag in _GH_BOOL_LONG:
                if has_inline:
                    return _extract_failure("ambiguous", f"{flag} takes no value")
                continue
            if flag not in _GH_VALUE_LONG:
                return _extract_failure("ambiguous", f"unknown flag {flag}")
            field = _GH_VALUE_LONG[flag]
            if has_inline:
                value = inline_value
            elif index < len(args):
                value = args[index]
                index += 1
            else:
                return _extract_failure("ambiguous", f"{flag} needs a value")
        elif arg.startswith("-") and len(arg) > 1:
            letter = arg[1]
            if letter in _GH_VALUE_SHORT:
                field = _GH_VALUE_SHORT[letter]
                rest = arg[2:]
                if rest:
                    value = rest[1:] if rest.startswith("=") else rest
                elif index < len(args):
                    value = args[index]
                    index += 1
                else:
                    return _extract_failure("ambiguous", f"-{letter} needs a value")
            elif all(char in _GH_BOOL_SHORT for char in arg[1:]):
                continue
            else:
                return _extract_failure("ambiguous", f"unknown flag {arg}")
        else:
            return _extract_failure("ambiguous", f"unexpected positional argument {arg!r}")
        if field == "title":
            if title is not None:
                return _extract_failure("ambiguous", "the title was given more than once")
            title = value
        elif field == "body":
            if body_inline is not None:
                return _extract_failure("ambiguous", "--body/-b was given more than once")
            body_inline = value
        elif field == "body_file":
            if body_file is not None:
                return _extract_failure("ambiguous", "--body-file/-F was given more than once")
            body_file = value
    if body_inline is not None and body_file is not None:
        return _extract_failure("ambiguous", "both a body and a body file were given")
    body: Optional[bytes] = None
    display = ""
    if body_file is not None:
        display = body_file
        if body_file == "-":
            return _extract_failure("unreadable", "--body-file - (stdin) cannot be read at hook time", display)
        file_path = os.path.expanduser(body_file)
        if not os.path.isabs(file_path):
            file_path = os.path.join(cwd, file_path)
        try:
            with open(file_path, "rb") as handle:
                body = handle.read()
        except OSError as exc:
            return _extract_failure("unreadable", f"the body file cannot be read: {exc}", display)
    elif body_inline is not None:
        body = os.fsencode(body_inline)
        display = "(inline --body)"
    return {
        "ok": True,
        "title": title or "",
        "has_body": body is not None,
        "body_b64": base64.b64encode(body).decode("ascii") if body is not None else "",
        "body_display": display,
    }


def cmd_extract() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}
    command = ((payload.get("tool_input") or {}).get("command")) or ""
    cwd = payload.get("cwd") or os.getcwd()
    print(json.dumps(extract_raw_create(command, cwd)))
    return 0


# ---------------------------------------------------------------------------
# rewrite handlers: discovered from the settings in force, then dry-run
# ---------------------------------------------------------------------------

GUARD_FILES = {"issue-create-owner.py", "pre-issue-create-audit.sh"}
_SCRIPT_REF = re.compile(r"(?:~|/)[A-Za-z0-9_./+@-]*\.(?:py|sh|bash|zsh|mjs|js|ts)\b")
_UPDATED_INPUT = re.compile(r"updatedInput|updated_input")


def _script_paths(text: str, plugin_root: str = "") -> List[str]:
    expanded = text.replace("${CLAUDE_PLUGIN_ROOT}", plugin_root).replace("$CLAUDE_PLUGIN_ROOT", plugin_root)
    expanded = os.path.expandvars(expanded)
    return [os.path.expanduser(match) for match in _SCRIPT_REF.findall(expanded)]


def can_emit_updated_input(command: str, plugin_root: str = "") -> bool:
    """Does any script this registered command runs (or directly execs) mention updatedInput?"""
    seen: set = set()
    queue = [(item, 0) for item in _script_paths(command, plugin_root)]
    while queue:
        script, depth = queue.pop(0)
        if script in seen or os.path.basename(script) in GUARD_FILES or not os.path.isfile(script):
            continue
        seen.add(script)
        try:
            with open(script, errors="replace") as handle:
                content = handle.read()
        except OSError:
            continue
        if _UPDATED_INPUT.search(content):
            return True
        if depth < 1:
            queue.extend((item, depth + 1) for item in _script_paths(content, plugin_root))
    return False


def _settings_documents(cwd: str) -> Tuple[List[Tuple[str, Dict[str, Any], str]], List[str]]:
    """(source, document, plugin_root) for every settings file in force, plus problems found reading them."""
    problems: List[str] = []
    documents: List[Tuple[str, Dict[str, Any], str]] = []
    explicit = os.environ.get("ISSUE_CREATE_OWNER_SETTINGS_FILES")
    home = Path.home() / ".claude"
    if explicit is not None:
        candidates = [Path(item) for item in explicit.split(os.pathsep) if item]
    else:
        candidates = [home / "settings.json", home / "settings.local.json",
                      Path("/Library/Application Support/ClaudeCode/managed-settings.json")]
        try:
            current = Path(cwd).resolve()
        except OSError:
            current = Path.cwd()
        for directory in [current, *current.parents]:
            candidates += [directory / ".claude" / "settings.json", directory / ".claude" / "settings.local.json"]
    seen: set = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen or not candidate.is_file():
            continue
        seen.add(key)
        try:
            with open(str(candidate)) as handle:
                document = json.load(handle)
        except (OSError, ValueError) as exc:
            problems.append(f"settings file {candidate} could not be read ({type(exc).__name__})")
            continue
        if isinstance(document, dict):
            documents.append((key, document, ""))
    if explicit is None:
        enabled: Dict[str, Any] = {}
        for _, document, _ in documents:
            plugins = document.get("enabledPlugins")
            if isinstance(plugins, dict):
                enabled.update(plugins)
        for name, on in enabled.items():
            if on is not True:
                continue
            plugin, _, market = str(name).partition("@")
            versions = sorted((home / "plugins" / "cache" / market / plugin).glob("*"))
            if not versions:
                continue
            root = versions[-1]
            hooks_file = root / "hooks" / "hooks.json"
            if hooks_file.is_file():
                try:
                    with open(str(hooks_file)) as handle:
                        document = json.load(handle)
                except (OSError, ValueError) as exc:
                    problems.append(f"plugin hooks {hooks_file} could not be read ({type(exc).__name__})")
                    continue
                if isinstance(document, dict):
                    documents.append((str(hooks_file), document, str(root)))
    return documents, problems


def discover_rewrite_handlers(cwd: str) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Every registered PreToolUse Bash command whose scripts can emit updatedInput: [(label, command)]."""
    documents, problems = _settings_documents(cwd)
    handlers: List[Tuple[str, str]] = []
    seen: set = set()
    for source, document, plugin_root in documents:
        entries = (document.get("hooks") or {}).get("PreToolUse") or []
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            matcher = entry.get("matcher") or ""
            try:
                applies = matcher in ("", "*") or re.fullmatch(matcher, "Bash") is not None
            except re.error:
                applies = matcher == "Bash"
            if not applies:
                continue
            for hook in entry.get("hooks") or []:
                if not isinstance(hook, dict) or hook.get("type", "command") != "command" or not hook.get("command"):
                    continue
                command = str(hook["command"]).replace("${CLAUDE_PLUGIN_ROOT}", plugin_root)
                if command in seen or any(name in command for name in GUARD_FILES):
                    continue
                seen.add(command)
                if can_emit_updated_input(command, plugin_root):
                    refs = _script_paths(command, plugin_root)
                    label = os.path.basename(refs[-1]) if refs else command[:48]
                    handlers.append((label, command))
    return handlers, problems


def _handler_violation(returncode: int, stdout: bytes) -> Optional[str]:
    if returncode != 0:
        return f"exit {returncode}"
    for line in stdout.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        output = value.get("hookSpecificOutput")
        if isinstance(output, dict) and ("updatedInput" in output or output.get("permissionDecision") in ("deny", "ask")):
            return "would rewrite or deny it"
        if str(value.get("decision", "")).lower() == "block":
            return "would block it"
    return None


def cmd_preserve() -> int:
    """PreToolUse guard: prove every rewrite handler in force is a no-op on this exact payload.

    The handlers run in parallel with the guard on the same payload and are pure functions of it
    (a JSON document in, an optional updatedInput/deny document out), so running them here shows
    exactly what they would do. The set is DISCOVERED from the settings in force, not assumed.
    Any rewrite, deny, failure, or a handler that cannot be run or read means the command might not
    reach the shell as written: block it before anything runs or is written.
    """
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = {}
    cwd = payload.get("cwd") if isinstance(payload, dict) and payload.get("cwd") else os.getcwd()
    run_cwd = cwd if os.path.isdir(cwd) else None
    offenders: List[str] = []
    runs: List[Tuple[str, Any, bool]] = []
    explicit = os.environ.get("ISSUE_CREATE_OWNER_REWRITER_HOOKS")
    if explicit is not None:
        for script in (item for item in explicit.split(os.pathsep) if item):
            argv = ["bash", script] if script.endswith(".sh") else [sys.executable, script]
            runs.append((os.path.basename(script), argv, False))
    else:
        handlers, problems = discover_rewrite_handlers(cwd)
        offenders.extend(problems)
        runs.extend((label, command, True) for label, command in handlers)
    for label, spec, use_shell in runs:
        try:
            proc = subprocess.run(
                spec, shell=use_shell, executable="/bin/bash" if use_shell else None,
                input=raw.encode("utf-8", "surrogateescape"), capture_output=True, timeout=20,
                env=os.environ.copy(), cwd=run_cwd,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            offenders.append(f"{label} (could not be run: {type(exc).__name__})")
            continue
        violation = _handler_violation(proc.returncode, proc.stdout)
        if violation:
            offenders.append(f"{label} ({violation})")
    if offenders:
        print(json.dumps({"decision": "block", "message": (
            "BLOCKED: command rewrite handler(s) " + ", ".join(offenders) + " would not pass this create-owner "
            "invocation through unchanged, so it would not reach the shell as written. Nothing ran and nothing "
            "was written. Remove the text they key on (put long or special body text in a literal --body-file) "
            "and run the owner again."
        )}))
    return 0


def cmd_classify() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}
    command = ((payload.get("tool_input") or {}).get("command")) or ""
    print(json.dumps({"kind": classify(command)}))
    return 0


# ---------------------------------------------------------------------------
# flag parsing (strict whitelist: nothing may change what gh would create)
# ---------------------------------------------------------------------------

_FLAGS = {
    "--title": "title", "-t": "title",
    "--body": "body", "-b": "body",
    "--body-file": "body_file", "-F": "body_file",
    "--label": "label", "-l": "label",
    "--assignee": "assignee", "-a": "assignee",
    "--milestone": "milestone", "-m": "milestone",
    "--project": "project", "-p": "project",
    "--repo": "repo", "-R": "repo",
}
_LONG = {
    "label": "--label", "assignee": "--assignee", "milestone": "--milestone",
    "project": "--project", "repo": "--repo",
}
_PRIORITY_ALIAS = re.compile(r"(?i)(?<![A-Za-z0-9_-])priority:p([0-3])(?![A-Za-z0-9_-])")


class Spec:
    def __init__(self) -> None:
        self.title: str = ""
        self.body: bytes = b""
        self.body_display: str = ""
        self.extras: List[Tuple[str, str]] = []
        self.repo: str = ""


def parse_spec(args: List[str]) -> Spec:
    title: Optional[str] = None
    body_inline: Optional[str] = None
    body_file: Optional[str] = None
    extras: List[Tuple[str, str]] = []
    index = 0
    while index < len(args):
        arg = args[index]
        value: Optional[str] = None
        flag = arg
        if arg.startswith("--") and "=" in arg:
            flag, value = arg.split("=", 1)
        name = _FLAGS.get(flag)
        if name is None:
            raise Refusal(
                f"unsupported argument {arg!r}. The owner accepts only --title/-t, --body/-b, --body-file/-F, "
                "--label/-l, --assignee/-a, --milestone/-m, --project/-p and --repo/-R; use gh directly for "
                "anything else that needs no permit."
            )
        if value is None:
            index += 1
            if index >= len(args):
                raise Refusal(f"{flag} needs a value")
            value = args[index]
        index += 1
        if name == "title":
            if title is not None:
                raise Refusal("--title was given twice")
            title = value
        elif name == "body":
            if body_inline is not None:
                raise Refusal("--body was given twice")
            body_inline = value
        elif name == "body_file":
            if body_file is not None:
                raise Refusal("--body-file was given twice")
            body_file = value
        else:
            if name == "label":
                value = _PRIORITY_ALIAS.sub(lambda match: f"P{match.group(1)}", value)
            extras.append((_LONG[name], value))
    if not title:
        raise Refusal("--title is required")
    if body_inline is not None and body_file is not None:
        raise Refusal("give exactly one of --body or --body-file, not both")
    spec = Spec()
    spec.title = title
    spec.extras = extras
    spec.repo = next((value for flag, value in extras if flag == "--repo"), "")
    if body_file is not None:
        if body_file == "-":
            raise Refusal("--body-file - (stdin) is not accepted: the owner needs a named file it can capture once")
        path = Path(os.path.expanduser(body_file))
        try:
            spec.body = path.read_bytes()
        except OSError as exc:
            raise Refusal(f"--body-file {body_file!r} cannot be read: {exc}")
        spec.body_display = str(path if path.is_absolute() else Path(os.getcwd()) / path)
    elif body_inline is not None:
        spec.body = os.fsencode(body_inline)
    else:
        raise Refusal("one of --body or --body-file is required")
    if not spec.body.strip():
        raise Refusal("the issue body is empty")
    return spec


# ---------------------------------------------------------------------------
# gate stages (existing hooks, run sequentially on the captured bytes)
# ---------------------------------------------------------------------------

def hook_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", "/usr/bin:/bin")
    if extra:
        env.update(extra)
    return env


def run_hook(script: str, payload: Dict[str, Any], extra_env: Dict[str, str], timeout: int = 120) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["bash", script],
            input=json.dumps(payload).encode("utf-8", "surrogateescape"),
            capture_output=True,
            env=hook_env(extra_env),
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")


def block_message(stdout: str) -> Optional[str]:
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and str(value.get("decision", "")).lower() == "block":
            return str(value.get("message") or value.get("reason") or "blocked")
    return None


def parse_marker(stdout: str) -> Optional[Dict[str, Any]]:
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        marker = value.get("mop_issue_create_audit") if isinstance(value, dict) else None
        if isinstance(marker, dict) and marker.get("permit") == "valid":
            return marker
    return None


def ledger_hooks() -> List[str]:
    configured = os.environ.get("ISSUE_CREATE_OWNER_LEDGER_HOOKS")
    if configured is not None:
        return [item for item in configured.split(os.pathsep) if item]
    hooks = [str(HERE / "block-invalid-issue-contract-ledger.sh")]
    if os.path.isfile(DEFAULT_PROJECT_LEDGER_HOOK):
        hooks.append(DEFAULT_PROJECT_LEDGER_HOOK)
    return hooks


# ---------------------------------------------------------------------------
# permit / lock / in-flight bookkeeping
# ---------------------------------------------------------------------------

def permit_matches(permit: Dict[str, Any], marker: Dict[str, Any]) -> bool:
    message = permit.get("source_message_ts", "")
    return (
        permit.get("channel") == marker["channel"]
        and permit.get("thread_ts") == marker["thread_ts"]
        and (not message or message == marker["source_message_ts"])
        and permit.get("body_sha256") == marker["body_sha256"]
        and permit.get("title_digest") == marker["title_digest"]
    )


class LockBusy(Exception):
    """The permit lock stayed busy for the whole bounded wait."""


class PermitLock:
    """The ONE permit lock: `<permit file>.lock`.

    It is the same lock file explore_issue_atomic_write_json (mint), the legacy
    explore_issue_consume_permit and explore_issue_restore_permit take. Waits are bounded,
    the lock is never nested and never held across gh, so none of them can deadlock on it.
    """

    def __init__(self, permit_file: str, wait: float) -> None:
        self.path = permit_file + ".lock"
        self.wait = wait
        self.handle: Any = None

    def __enter__(self) -> "PermitLock":
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.handle = open(self.path, "a+")
        deadline = time.monotonic() + self.wait
        while True:
            try:
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    raise LockBusy(self.path)
                time.sleep(0.05)

    def bind(self, value: Dict[str, Any]) -> None:
        """Record the binding (source / invocation / digests) in the lock file, under the lock."""
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(json.dumps(value, sort_keys=True) + "\n")
        self.handle.flush()

    def __exit__(self, *exc_info: Any) -> bool:
        if self.handle is not None:
            try:
                fcntl.flock(self.handle, fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle = None
        return False


def consume_under_lock(marker: Dict[str, Any], record: Dict[str, Any], url: str,
                       inflight_path: Path, wait: float) -> str:
    """Consume the permit under the shared permit lock. Returns "ok" or why it did not."""
    permit_path = Path(marker["permit_file"])
    consumed_dir = Path(marker["consumed_dir"])
    consumed_dir.mkdir(parents=True, exist_ok=True)
    with PermitLock(marker["permit_file"], wait):
        on_disk = read_json(inflight_path)
        if (on_disk is None or on_disk.get("invocation") != record["invocation"]
                or on_disk.get("body_sha256") != record["body_sha256"]):
            return "the in-flight record no longer matches this invocation"
        permit = read_json(permit_path)
        if permit is None or not permit_matches(permit, marker):
            return "the permit changed or vanished"
        permit.update({
            "consumed_by": "issue-create-owner",
            "consumed_at": now_iso(),
            "invocation": record["invocation"],
            "issue_url": url,
            "issue_number": int(url.rsplit("/", 1)[1]),
            "readback": record.get("readback", ""),
        })
        target = consumed_dir / f"{marker['key']}.permit.consumed.{now_stamp()}.json"
        atomic_write_json(target, permit)  # durable consumed record first
        os.unlink(str(permit_path))
        try:
            os.unlink(str(inflight_path))
        except OSError:
            pass
    return "ok"


def release_inflight(inflight_path: Path, invocation: str, permit_file: str, wait: float) -> bool:
    """Drop OUR in-flight record, under the permit lock. Only called when gh definitely never started."""
    try:
        with PermitLock(permit_file, wait):
            record = read_json(inflight_path)
            if record is None or record.get("invocation") != invocation or record.get("state") != "IN_FLIGHT":
                return False
            os.unlink(str(inflight_path))
            return True
    except (LockBusy, OSError):
        return False


def verified_url(rc: int, stdout: bytes, spec: Spec) -> Optional[str]:
    """Exactly one canonical issue URL on stdout and a zero exit; otherwise None."""
    if rc != 0:
        return None
    repo_re = os.environ.get("ISSUE_CREATE_OWNER_REPO_RE", DEFAULT_REPO_RE)
    pattern = re.compile(r"^https://github\.com/(" + repo_re + r")/issues/([0-9]+)$")
    lines = [line.strip() for line in stdout.decode("utf-8", "replace").splitlines() if line.strip()]
    if not lines:
        return None
    matches = [pattern.match(line) for line in lines]
    urls = {line for line, match in zip(lines, matches) if match}
    last = pattern.match(lines[-1])
    if len(urls) != 1 or last is None:
        return None
    if spec.repo and last.group(1).lower() != spec.repo.lower().rsplit("github.com/", 1)[-1].strip("/"):
        return None
    return lines[-1]


def _normalise_text(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def compare_issue(data: Dict[str, Any], url: str, spec: Spec, body: bytes) -> Tuple[bool, str]:
    """Does the GitHub issue read back with the identity we just sent?"""
    match = re.match(r"^https://github\.com/([^/]+/[^/]+)/issues/([0-9]+)$", url)
    if match is None:
        return False, "mismatch: unparseable url"
    repo, number = match.group(1), int(match.group(2))
    try:
        got_number = int(data.get("number", -1))
    except (TypeError, ValueError):
        got_number = -1
    if got_number != number:
        return False, f"mismatch: number {got_number} != {number}"
    if str(data.get("html_url", "")).lower() != url.lower():
        return False, f"mismatch: html_url {data.get('html_url')!r}"
    if "pull_request" in data:
        return False, "mismatch: the number is a pull request"
    repository_url = str(data.get("repository_url", ""))
    if repository_url and not repository_url.lower().endswith("/repos/" + repo.lower()):
        return False, f"mismatch: repository_url {repository_url!r}"
    if spec.repo and spec.repo.lower().rsplit("github.com/", 1)[-1].strip("/") != repo.lower():
        return False, f"mismatch: requested repo {spec.repo!r} != {repo}"
    if str(data.get("title", "")).strip() != spec.title.strip():
        return False, "mismatch: title"
    if _normalise_text(str(data.get("body") or "")) != _normalise_text(body.decode("utf-8", "replace")):
        return False, "mismatch: body"
    return True, "verified"


def readback_issue(gh_bin: str, url: str, spec: Spec, body: bytes) -> Tuple[bool, str]:
    """GET the issue (bounded retries for read-after-write lag). Unavailable or mismatching => not verified."""
    match = re.match(r"^https://github\.com/([^/]+/[^/]+)/issues/([0-9]+)$", url)
    if match is None:
        return False, "mismatch: unparseable url"
    repo, number = match.group(1), match.group(2)
    delay = float(os.environ.get("ISSUE_CREATE_OWNER_READBACK_DELAY", "2"))
    detail = "unavailable"
    for attempt in range(3):
        try:
            proc = subprocess.run(
                [gh_bin, "api", f"repos/{repo}/issues/{number}"],
                stdin=subprocess.DEVNULL, capture_output=True, env=hook_env(), timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            detail = f"unavailable: {type(exc).__name__}"
        else:
            if proc.returncode == 0:
                try:
                    data = json.loads(proc.stdout.decode("utf-8", "replace"))
                except ValueError:
                    data = None
                if isinstance(data, dict):
                    return compare_issue(data, url, spec, body)
                detail = "unavailable: unparseable response"
            else:
                detail = "unavailable: " + tail(proc.stderr, 200).strip()
        if attempt < 2 and delay > 0:
            time.sleep(delay)
    return False, detail


def work_dir() -> Path:
    """Post-create hook logs only; nothing is written here unless a create was filed."""
    return Path(os.environ.get("ISSUE_CREATE_OWNER_WORK_DIR", "/tmp/heydonna-issue-create-owner"))


def start_post_hooks(synthetic_command: str, stdout_text: str, cwd: str, work: Path, invocation: str) -> Optional[Path]:
    """Run the existing PostToolUse issue hooks (they key on the `gh issue create` text, which the
    owner command does not contain). Detached and best effort; never affects the create outcome."""
    payload = json.dumps({
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "cwd": cwd,
        "tool_input": {"command": synthetic_command},
        "tool_response": {"stdout": stdout_text, "stderr": "", "exit_code": 0},
    }).encode("utf-8", "surrogateescape")
    home_hooks = Path.home() / ".claude" / "hooks"
    candidates = (
        ("bash", os.environ.get("ISSUE_CREATE_OWNER_POST_SWEEP_HOOK", str(home_hooks / "post-issue-create-sweep.sh"))),
        ("python3", os.environ.get("ISSUE_CREATE_OWNER_POST_LINK_HOOK", str(home_hooks / "post-issue-blocked-by-link.py"))),
    )
    log_path = work / f"post-{invocation}.log"
    try:
        work.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    started = False
    for interpreter, script in candidates:
        if not script or not os.path.isfile(script):
            continue
        try:
            with open(str(log_path), "ab") as log:
                child = subprocess.Popen(
                    [interpreter, script], stdin=subprocess.PIPE, stdout=log, stderr=log,
                    env=hook_env(), start_new_session=True,
                )
            assert child.stdin is not None
            child.stdin.write(payload)
            child.stdin.close()
            started = True
        except (OSError, ValueError):
            continue
    return log_path if started else None


# ---------------------------------------------------------------------------
# the chain
# ---------------------------------------------------------------------------

def refuse(reason: str, code: int = EXIT_REFUSED, label: str = "REFUSED NO_GH_START") -> int:
    say(f"{label} {reason}")
    return code


def run_ledger_stage(spec: Spec, cwd: str, command_words: List[str]) -> None:
    """Run the ledger/tracker gates on the SAME bytes, in memory.

    The buffer reaches each gate as base64 in MOP_ISSUE_CREATE_BODY_B64 (the environment of that
    one process) with `--body-file -` in the synthesized command and MOP_ISSUE_CREATE_STAGE=audit.
    The validators read it instead of a file and suppress their side effects in that stage (no
    blocked-issue draft copy, no obligation upsert), keeping every decision identical to the one
    they make for the same bytes in a named body file. Nothing touches the disk.
    """
    hooks = ledger_hooks()
    for script in hooks:
        if not os.path.isfile(script):
            raise Refusal(f"a required ledger/tracker gate is missing: {script}")
    payload = {
        "hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": cwd,
        "tool_input": {"command": shlex.join(command_words + ["--body-file", "-"])},
    }
    env = {
        "MOP_ISSUE_CREATE_STAGE": "audit",
        "MOP_ISSUE_CREATE_BODY_B64": base64.b64encode(spec.body).decode("ascii"),
    }
    for script in hooks:
        rc, out, err = run_hook(script, payload, env)
        if rc != 0:
            raise Refusal(f"ledger gate {script} failed (rc={rc}): {err.strip() or out.strip()}")
        blocked = block_message(out)
        if blocked:
            raise Refusal(f"ledger/tracker gate rejected the create: {blocked}")


def run_chain(args: List[str]) -> int:
    spec = parse_spec(args)  # reads --body-file into memory; nothing is written
    cwd = os.getcwd()
    invocation = uuid.uuid4().hex
    lock_wait = float(os.environ.get("ISSUE_CREATE_OWNER_LOCK_WAIT", "30"))

    # 1. The ONE buffer: spec.body. Exact raw digest (no command substitution, nothing stripped).
    raw_sha = sha256_hex(spec.body)
    title_digest = sha256_hex(spec.title.encode("utf-8", "surrogateescape"))
    display = spec.body_display or "(inline --body)"

    gh_bin = os.environ.get("ISSUE_CREATE_OWNER_GH_BIN", "gh")
    gh_args = ["issue", "create", "--title", spec.title, "--body-file", "-"]
    for flag, value in spec.extras:
        gh_args += [flag, value]
    argv_digest = sha256_hex(json.dumps(gh_args).encode("utf-8", "surrogateescape"))
    command_words = ["gh", "issue", "create", "--title", spec.title] + [item for pair in spec.extras for item in pair]
    command_text = shlex.join(command_words + ["--body-file", "-"])

    # 2a. gate: source / verdict / permit on the buffer (arrives on the hook's stdin, no file).
    audit_script = os.environ.get("ISSUE_CREATE_OWNER_AUDIT_HOOK") or str(HERE / "pre-issue-create-audit.sh")
    if not os.path.isfile(audit_script):
        raise Refusal(f"the pre-issue-create audit gate is missing: {audit_script}")
    audit_payload = {
        "hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": cwd,
        "tool_input": {"command": command_text},
        "mop_issue_create": {
            "title": spec.title,
            "body_b64": base64.b64encode(spec.body).decode("ascii"),
            "body_display": display,
        },
    }
    audit_env = {"MOP_ISSUE_CREATE_STAGE": "audit"}
    latch = HERE / "explore-issue-latch-guard.sh"
    if "EXPLORE_ISSUE_LATCH_GUARD" not in os.environ and latch.is_file():
        audit_env["EXPLORE_ISSUE_LATCH_GUARD"] = str(latch)
    rc, out, err = run_hook(audit_script, audit_payload, audit_env)
    if rc != 0:
        raise Refusal(f"the audit gate failed (rc={rc}): {err.strip() or out.strip()}")
    blocked = block_message(out)
    if blocked:
        raise Refusal(f"audit gate rejected the create: {blocked}")
    marker = parse_marker(out)
    if marker is None:
        raise Refusal(
            "the audit gate did not find a valid READY_TO_FILE permit bound to this exact body and title. The "
            "owner only runs permit-required creates; a create that needs no permit uses gh issue create directly."
        )
    for field in ("key", "channel", "thread_ts", "source_message_ts", "body_sha256", "title_digest",
                  "permit_file", "inflight_dir", "consumed_dir"):
        if not marker.get(field):
            raise Refusal(f"the audit gate marker is missing {field}")
    if marker["body_sha256"] != raw_sha or marker["title_digest"] != title_digest:
        raise Refusal("the audit gate saw different bytes than the owner holds; nothing was written")

    # 2b. gate: ledger + tracker validators on the same bytes.
    run_ledger_stage(spec, cwd, command_words)

    # 3. bind: check + binding + IN_FLIGHT under the shared permit lock, then release it.
    key = marker["key"]
    permit_file = marker["permit_file"]
    inflight_path = Path(marker["inflight_dir"]) / f"{key}.inflight.json"
    record: Dict[str, Any] = {
        "schema": 2, "state": "IN_FLIGHT", "key": key, "invocation": invocation, "pid": os.getpid(),
        "channel": marker["channel"], "thread_ts": marker["thread_ts"],
        "source_message_ts": marker["source_message_ts"],
        "body_sha256": raw_sha, "title_digest": title_digest, "argv_digest": argv_digest,
        "permit_file": permit_file, "started_at": now_iso(),
    }
    try:
        with PermitLock(permit_file, lock_wait) as lock:
            consumed = glob.glob(os.path.join(marker["consumed_dir"], f"{key}.permit.consumed.*.json"))
            if consumed or inflight_path.exists():
                return refuse(
                    "this source already has a consumed or in-flight create (result UNKNOWN). RECONCILIATION-ONLY: "
                    "search GitHub for the canonical Source permalink and resolve the same obligation; never re-mint "
                    "or create again.",
                    EXIT_BUSY,
                    "REFUSED NO_GH_START",
                )
            permit = read_json(Path(permit_file))
            if permit is None or not permit_matches(permit, marker):
                raise Refusal("the permit changed or vanished after the gates ran; nothing was written")
            lock.bind(record)
            # 4. durable in-flight BEFORE gh. A failure to persist it means gh has not started.
            try:
                atomic_write_json(inflight_path, record)
            except OSError as exc:
                try:
                    os.unlink(str(inflight_path))
                except OSError:
                    pass
                return refuse(f"the durable in-flight record could not be written ({exc}); gh was not started",
                              EXIT_REFUSED, "REFUSED NO_GH_START")
    except LockBusy:
        return refuse(
            f"the permit lock for source {marker['channel']}:{marker['thread_ts']} stayed busy for {lock_wait:g}s; "
            "nothing was started by this invocation. Run it again.",
            EXIT_BUSY,
            "REFUSED NO_GH_START",
        )
    except OSError as exc:
        raise Refusal(f"the permit lock could not be used: {exc}")

    # 5. gh, fed the identical buffer on stdin. No lock is held while it runs.
    timeout = int(os.environ.get("ISSUE_CREATE_OWNER_GH_TIMEOUT", "120"))
    try:
        child = subprocess.Popen(
            [gh_bin] + gh_args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=hook_env(),
        )
    except OSError as exc:
        # 6. definite no-start: the only release.
        released = release_inflight(inflight_path, invocation, permit_file, lock_wait)
        return refuse(
            f"gh could not be started ({exc}); in-flight record {'released' if released else 'NOT released'}, "
            "permit intact",
            EXIT_RELEASED,
            "NO_GH_START",
        )
    timed_out = False
    try:
        out_bytes, err_bytes = child.communicate(input=spec.body, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        child.kill()
        out_bytes, err_bytes = child.communicate()
    gh_rc = child.returncode

    record.update({
        "state": "RETURNED", "gh_rc": gh_rc, "timed_out": timed_out, "finished_at": now_iso(),
        "stdout_tail": tail(out_bytes), "stderr_tail": tail(err_bytes),
    })
    candidate = None if timed_out else verified_url(gh_rc, out_bytes, spec)
    url: Optional[str] = None
    readback = "not attempted"
    if candidate:
        # The URL gh printed is only a claim: read the issue back from GitHub and check its identity.
        record["issue_url_candidate"] = candidate
        readback_ok, readback = readback_issue(gh_bin, candidate, spec, spec.body)
        if readback_ok:
            url = candidate
            record["issue_url"] = url
    record["readback"] = readback
    try:
        atomic_write_json(inflight_path, record)
    except OSError:
        pass

    if err_bytes:
        sys.stderr.write(err_bytes.decode("utf-8", "replace"))
    if url is None:
        if timed_out:
            reason = "gh timed out"
        elif candidate:
            reason = f"gh printed {candidate} but the GitHub readback did not verify it ({readback})"
        else:
            reason = f"gh exited {gh_rc} without exactly one verified issue URL"
        return refuse(
            f"{reason}. UNKNOWN create: RECONCILIATION-ONLY. The in-flight record for {key} stays in place and is "
            "never released: search GitHub for the canonical Source permalink and resolve the same obligation. "
            "Do not re-mint, restore or create again.",
            EXIT_UNKNOWN,
            "UNKNOWN",
        )

    # 7. verified URL: consume the permit (under the permit lock), which also drops the in-flight record.
    try:
        outcome = consume_under_lock(marker, record, url, inflight_path, lock_wait)
    except (LockBusy, OSError) as exc:
        outcome = f"permit bookkeeping error: {type(exc).__name__}: {exc}"
    if outcome != "ok":
        return refuse(
            f"filed {url} but the permit could not be consumed ({outcome}). The in-flight record is kept so the "
            "source stays reconciliation-only; resolve the obligation with this URL.",
            EXIT_BOOKKEEPING,
            "FILED_UNBOOKED",
        )
    sys.stdout.write(out_bytes.decode("utf-8", "replace"))
    if not out_bytes.endswith(b"\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()
    say(f"FILED {url} key={key} invocation={invocation}")
    log_path = start_post_hooks(command_text, out_bytes.decode("utf-8", "replace"), cwd, work_dir(), invocation)
    if log_path:
        say(f"post-create sweep and blocked-by hooks started in the background; log={log_path}")
    return EXIT_FILED


def cmd_create(args: List[str]) -> int:
    try:
        return run_chain(args)
    except Refusal as refusal:
        return refuse(refusal.reason, refusal.code)


def main(argv: List[str]) -> int:
    if len(argv) >= 2 and argv[1] == "classify":
        return cmd_classify()
    if len(argv) >= 2 and argv[1] == "extract":
        return cmd_extract()
    if len(argv) >= 2 and argv[1] == "preserve":
        return cmd_preserve()
    if len(argv) >= 2 and argv[1] == "create":
        return cmd_create(argv[2:])
    print(__doc__.split("Subcommands", 1)[1].split("Exit codes", 1)[0].strip(), file=sys.stderr)
    return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main(sys.argv))
