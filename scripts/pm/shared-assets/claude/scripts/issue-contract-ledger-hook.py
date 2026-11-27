#!/usr/bin/env python3
"""Fail closed on invalid HeyDonna issue creation and mutation."""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import subprocess
import sys

from control_plane_issue_policy import internal_control_plane_reason


COMMAND = os.environ.get("CMD_TEXT", "")
VALIDATOR = os.environ.get("VALIDATOR", "")
GH_BIN = os.environ.get("GH_BIN", "gh")
HOOK_CWD = os.environ.get("HOOK_CWD", "")
HEYDONNA_REPO = "Scribie/heydonna-app"
MUTATION_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:/[^\s;&|()]+/)?gh\s+issue\s+(create|edit)\b"
)


def emit(target: str, *errors: str) -> None:
    print(json.dumps({"block": True, "target": target, "errors": list(errors)}))


def mask_data_spans(text: str) -> str:
    """Blank characters that are data rather than command words.

    Prose carried inside quotes (--action / --external-state payloads, bodies,
    commit messages) and comment text must never be classified as an
    invocation. Length is preserved so regex match spans still index the
    original command.
    """
    out = list(text)
    quote = None
    word_start = True
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if quote:
            out[i] = " "
            if ch == "\\" and quote == '"' and i + 1 < n:
                out[i + 1] = " "
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch == "#" and word_start:
            j = text.find("\n", i)
            if j == -1:
                j = n
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        if ch in "'\"":
            quote = ch
            out[i] = " "
            i += 1
            continue
        word_start = ch in " \t\n;&|()<>"
        i += 1
    return "".join(out)


def tokens(chunk: str) -> list[str]:
    try:
        parsed = shlex.split(chunk, posix=True)
    except ValueError:
        parsed = chunk.split()
    gh_index = next(
        (index for index, value in enumerate(parsed) if os.path.basename(value) == "gh"),
        None,
    )
    if gh_index is None:
        return []
    normalized = parsed[gh_index:]
    normalized[0] = os.path.basename(normalized[0])
    return normalized


def values(parts: list[str], names: set[str]) -> list[str]:
    found: list[str] = []
    for index, token in enumerate(parts):
        if token in names and index + 1 < len(parts):
            found.append(parts[index + 1])
        elif any(token.startswith(name + "=") for name in names):
            found.append(token.split("=", 1)[1])
    return found


def labels_arg(parts: list[str], names: set[str]) -> list[str]:
    labels: list[str] = []
    for value in values(parts, names):
        labels.extend(
            item.strip().strip("()")
            for item in value.split(",")
            if item.strip()
        )
    return labels


def repo_arg(parts: list[str]) -> str | None:
    repos = values(parts, {"--repo", "-R"})
    return repos[-1].strip().removesuffix(".git") if repos else None


def current_repo() -> str:
    if not HOOK_CWD:
        return ""
    try:
        remote = subprocess.check_output(
            ["git", "-C", HOOK_CWD, "config", "--get", "remote.origin.url"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip()
    except Exception:
        return ""
    match = re.search(r"(?:github\.com[:/])([^/\s]+/[^/\s]+?)(?:\.git)?$", remote)
    return match.group(1).removesuffix(".git") if match else ""


def targets_heydonna(parts: list[str]) -> bool:
    explicit = repo_arg(parts)
    if explicit is not None:
        return explicit.lower() == HEYDONNA_REPO.lower()
    return current_repo().lower() == HEYDONNA_REPO.lower()


def shell_commands(command: str) -> list[list[str]]:
    """Return command-position token groups without inspecting quoted payloads."""
    try:
        lexer = shlex.shlex(
            command,
            posix=True,
            punctuation_chars=";&|()`\n",
        )
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        parsed = list(lexer)
    except ValueError:
        return []

    separators = {
        "\n",
        ";",
        ";;",
        "&",
        "&&",
        "|",
        "||",
        "(",
        ")",
        "`",
    }
    commands: list[list[str]] = []
    current: list[str] = []
    for token in parsed:
        normalized_punctuation = token.replace("\n", "")
        is_newline_punctuation = (
            "\n" in token
            and (
                not normalized_punctuation
                or set(normalized_punctuation) <= set(";&|()`")
            )
        )
        if token in separators or is_newline_punctuation:
            if current:
                commands.append(current)
                current = []
            continue
        current.append(token)
    if current:
        commands.append(current)
    return commands


def command_tokens(parts: list[str]) -> list[str]:
    """Normalize a simple command while preserving command-position semantics."""
    index = 0
    while index < len(parts) and re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*=.*",
        parts[index],
    ):
        index += 1

    while index < len(parts):
        wrapper = os.path.basename(parts[index])
        if wrapper not in {
            "!",
            "command",
            "env",
            "nice",
            "nohup",
            "stdbuf",
            "sudo",
            "time",
            "timeout",
            "xargs",
        }:
            break
        index += 1
        if wrapper == "env":
            while index < len(parts) and (
                parts[index].startswith("-")
                or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", parts[index])
            ):
                index += 1
        elif wrapper == "sudo":
            while index < len(parts) and parts[index].startswith("-"):
                index += 1
        elif wrapper == "timeout":
            option_values = {"-k", "--kill-after", "-s", "--signal"}
            while index < len(parts) and parts[index].startswith("-"):
                option = parts[index].split("=", 1)[0]
                index += 1
                if (
                    option in option_values
                    and "=" not in parts[index - 1]
                    and index < len(parts)
                ):
                    index += 1
            if index < len(parts):
                index += 1
        elif wrapper == "xargs":
            option_values = {
                "-a",
                "--arg-file",
                "-d",
                "--delimiter",
                "-E",
                "--eof",
                "-I",
                "--replace",
                "-L",
                "--max-lines",
                "-n",
                "--max-args",
                "-P",
                "--max-procs",
                "-s",
                "--max-chars",
            }
            while index < len(parts) and parts[index].startswith("-"):
                option = parts[index].split("=", 1)[0]
                attached_short_value = bool(
                    re.match(r"^-[adEILnPs].+", parts[index])
                )
                index += 1
                if (
                    option in option_values
                    and "=" not in parts[index - 1]
                    and not attached_short_value
                    and index < len(parts)
                ):
                    index += 1
        elif wrapper == "nice":
            if index < len(parts) and parts[index] in {"-n", "--adjustment"}:
                index += 2
            elif index < len(parts) and (
                re.match(r"^-\d+$", parts[index])
                or parts[index].startswith("--adjustment=")
            ):
                index += 1
        elif wrapper == "stdbuf":
            while index < len(parts) and parts[index].startswith("-"):
                option = parts[index].split("=", 1)[0]
                attached_short_value = bool(re.match(r"^-[ioe].+", parts[index]))
                index += 1
                if (
                    option in {"-i", "--input", "-o", "--output", "-e", "--error"}
                    and "=" not in parts[index - 1]
                    and not attached_short_value
                    and index < len(parts)
                ):
                    index += 1
        elif wrapper == "time":
            while index < len(parts) and parts[index].startswith("-"):
                option = parts[index].split("=", 1)[0]
                index += 1
                if (
                    option in {"-f", "--format", "-o", "--output"}
                    and "=" not in parts[index - 1]
                    and index < len(parts)
                ):
                    index += 1
        elif wrapper == "nohup":
            if index < len(parts) and parts[index] == "--":
                index += 1

    if index >= len(parts) or os.path.basename(parts[index]) != "gh":
        return []
    normalized = parts[index:]
    normalized[0] = "gh"
    return normalized


def raw_issue_create_targets_heydonna(command: str) -> bool:
    for shell_command in shell_commands(command):
        parts = command_tokens(shell_command)
        if len(parts) < 2 or parts[1] not in {"api", "graphql"}:
            continue
        rendered = " ".join(parts)
        api_endpoint = re.search(
            r"repos/([^/\s\"']+)/([^/\s\"']+)/issues(?:[?\s\"']|$)",
            rendered,
        )
        if api_endpoint:
            repo = f"{api_endpoint.group(1)}/{api_endpoint.group(2)}"
            if repo.lower() != HEYDONNA_REPO.lower():
                continue
            if (
                re.search(
                    r"(?:^|\s)(?:-X|--method)(?:=|\s+)POST(?:\s|$)",
                    rendered,
                    re.I,
                )
                or re.search(
                    r"(?:^|\s)(?:-f|-F|--field|--raw-field)(?:=|\s+)",
                    rendered,
                )
            ):
                return True
            continue
        if (
            targets_heydonna(parts)
            and re.search(r"\bcreateIssue\b", rendered)
            and re.search(r"\bmutation\b", rendered)
        ):
            return True
    return False


def owner_buffer() -> str:
    """The create owner's single buffer (stage audit), delivered IN MEMORY.

    issue-create-owner.py puts the exact bytes it will send to gh into the environment of this
    hook as base64 (MOP_ISSUE_CREATE_BODY_B64) and passes `--body-file -`. Nothing is read from or
    written to disk. The text is decoded exactly like a file read would be: strict UTF-8 (a bad
    byte raises, which blocks the command just as an undecodable file does) and universal
    newlines, so every decision matches the one made for the same bytes in a named body file.
    """
    text = base64.b64decode(os.environ.get("MOP_ISSUE_CREATE_BODY_B64", "")).decode("utf-8")
    return text.replace("\r\n", "\n").replace("\r", "\n")


EDIT_VALUE_FLAGS = {
    "--add-assignee", "--add-blocked-by", "--add-blocking", "--add-label", "--add-project",
    "--add-sub-issue", "--body", "--body-file", "--milestone", "--parent", "--remove-assignee",
    "--remove-blocked-by", "--remove-blocking", "--remove-label", "--remove-project",
    "--remove-sub-issue", "--repo", "--title", "--type", "-b", "-F", "-m", "-R", "-t",
}
EDIT_BOOL_FLAGS = {"--remove-milestone", "--remove-parent", "--remove-type"}


BODY_FLAGS = {"--body", "--body-file", "-b", "-F"}
NON_BODY_VALUE_FLAGS = EDIT_VALUE_FLAGS - BODY_FLAGS
# CTO REVISE of acc2caaa (C0ALZJHGE49 thread 1795753187.323529, ts 1795753582.188919):
# the hook tokenizes with shlex, bash expands afterwards, so any expansion or control
# character can change the argv gh receives after the check. A body write is admitted
# only when the command text contains none of these, which makes shlex.split(command)
# the exact argv gh receives.
BODY_WRITE_FORBIDDEN = set("$`*?[{~;|&<>()\n\\")
EDIT_SEPARATOR_RE = re.compile(r"\|\||&&|[;|()`\n]|(?<![<>])&(?!>)")
INDIRECT_GH_RE = re.compile(r"(?<![A-Za-z0-9_.-])gh\s+(?:issue\s+edit|api)\b")
INDIRECT_BODY_RE = re.compile(r"--body|--input|(?<!\S)-[bF]|\bbody\s*[=:]|\bupdateIssue\b")
INTERPRETER_RE = re.compile(r"(?<![A-Za-z0-9_./-])(?:/\S*/)?(?:eval|sh|bash|zsh|dash|ksh|fish|source)(?=\s|$)")


def raw_words(segment: str) -> list[str]:
    """Split on unquoted whitespace, keeping quotes so expansion stays visible."""
    words: list[str] = []
    current = ""
    quote = None
    index = 0
    while index < len(segment):
        ch = segment[index]
        if quote:
            current += ch
            if ch == "\\" and quote == '"' and index + 1 < len(segment):
                current += segment[index + 1]
                index += 2
                continue
            if ch == quote:
                quote = None
        elif ch in " \t\r":
            if current:
                words.append(current)
                current = ""
        else:
            if ch in "'\"":
                quote = ch
            current += ch
        index += 1
    if current:
        words.append(current)
    return words


def word_expansion(word: str) -> str:
    """'none', 'quoted' ($ or ` only inside double quotes: stays one word) or 'unsafe'."""
    quote = None
    kind = "none"
    index = 0
    while index < len(word):
        ch = word[index]
        if quote == "'":
            if ch == "'":
                quote = None
        elif quote == '"':
            if ch == '"':
                quote = None
            elif ch == "\\":
                index += 1
            elif ch in "$`":
                kind = "quoted"
        elif ch in "'\"":
            quote = ch
        elif ch in "$`*?[{~\\":
            return "unsafe"
        index += 1
    return kind


def segment_may_hide_body(segment: str) -> bool:
    """True when expansion in this gh issue edit could add a body flag or a second issue.

    A double-quoted expansion bound as the value of a known non-body value flag
    (``--add-label "$L"`` or ``--add-label="$L"``) stays one value; anything else
    (an unquoted or positional expansion, a glob, a tilde) can change argv.
    """
    words = raw_words(segment)
    # words[3] (the issue) keeps its own issue_number_must_be_literal refusal.
    for index, word in enumerate(words[4:], start=4):
        kind = word_expansion(word)
        if kind == "none":
            continue
        if kind == "quoted":
            previous = words[index - 1] if index > 4 else ""
            if word_expansion(previous) == "none" and previous.strip("'\"") in NON_BODY_VALUE_FLAGS:
                continue
            prefix = re.split(r"['\"]", word, maxsplit=1)[0]
            if prefix.endswith("=") and prefix[:-1] in NON_BODY_VALUE_FLAGS:
                continue
        return True
    return False


def literal_body_write_problem(command: str) -> str | None:
    """Admit only `gh issue edit <N> --flag value ...`: one literal simple invocation."""
    bad = sorted(BODY_WRITE_FORBIDDEN & set(command))
    if bad:
        shown = ", ".join("newline" if ch == "\n" else ch for ch in bad)
        return f"shell metacharacters or expansion in the command ({shown})"
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return "command is not one parseable literal invocation"
    if len(argv) < 4 or argv[1:3] != ["issue", "edit"] or argv[0] not in {"gh"} and not (
        os.path.isabs(argv[0]) and os.path.basename(argv[0]) == "gh"
    ):
        return "command must start with gh issue edit (no env assignments, wrappers or aliases)"
    if not argv[3].isdigit():
        return "the issue must be one literal number right after gh issue edit"
    body_flags_seen = 0
    args = argv[4:]
    index = 0
    while index < len(args):
        token = args[index]
        option, sep, value = token.partition("=")
        if option in EDIT_BOOL_FLAGS and not sep:
            index += 1
            continue
        if option in EDIT_VALUE_FLAGS and (sep or index + 1 < len(args)):
            if not sep:
                index += 1
                value = args[index]
            if option.startswith("-") and not option.startswith("--") and sep:
                return f"attached short option {token!r}; use {option} <value>"
            if not value or value.startswith("-"):
                return f"option {option} has no literal value bound to it"
            body_flags_seen += option in BODY_FLAGS
            index += 1
            continue
        if token == "--":
            return "'--' ends option parsing; refusing"
        if token.startswith("-") and not token.startswith("--") and token[:2] in EDIT_VALUE_FLAGS:
            return f"attached short option {token!r}; use {token[:2]} <value>"
        if token.startswith("-"):
            return f"unknown option {token!r}"
        return f"extra positional {token!r}: exactly one literal issue number is allowed"
    if body_flags_seen != 1:
        return "exactly one --body or --body-file is allowed"
    return None


def api_issue_body_write(command: str) -> bool:
    """gh api / graphql writes of an issue body (REST PATCH issues/N, updateIssue)."""
    for shell_command in shell_commands(command):
        parts = command_tokens(shell_command)
        if len(parts) < 2 or parts[1] != "api":
            continue
        rendered = " ".join(parts)
        if re.search(r"\bupdateIssue\b", rendered) and re.search(r"\bbody\b", rendered):
            return True
        body_field = re.search(
            r"(?:^|\s)(?:-f|-F|--field|--raw-field)(?:=|\s+)body(?:=|\[|\s|$)|(?:^|\s)--input(?:=|\s|$)",
            rendered,
        )
        if not body_field:
            continue
        if re.search(r"[$`]", rendered):
            return True
        for owner, repo in re.findall(
            r"(?:^|\s)/?repos/([^/\s]+)/([^/\s]+)/issues/[^/\s?]+/?(?:\?\S*)?(?=\s|$)", rendered
        ):
            if "{" in owner or "{" in repo:
                if current_repo().lower() in {HEYDONNA_REPO.lower(), ""}:
                    return True
            elif f"{owner}/{repo}".lower() == HEYDONNA_REPO.lower():
                return True
    return False


def command_substitutions(command: str) -> list[str]:
    """Bodies of $(...) and `...` spans; `$(cat <<EOF ...)` text payloads are data."""
    spans: list[str] = []
    index = 0
    while index < len(command):
        if command.startswith("$(", index):
            depth, end = 1, index + 2
            while end < len(command) and depth:
                depth += {"(": 1, ")": -1}.get(command[end], 0)
                end += 1
            spans.append(command[index + 2 : end - 1 if not depth else end])
            index += 2
            continue
        if command[index] == "`":
            end = command.find("`", index + 1)
            end = len(command) if end == -1 else end
            spans.append(command[index + 1 : end])
            index = end + 1
            continue
        index += 1
    return [span for span in spans if not re.match(r"\s*cat\s+<<", span)]


def body_write_refusal(normalized: str, code_only: str) -> tuple[str, str] | None:
    """Refuse any issue-body write that is not one literal, single-target gh issue edit."""
    if api_issue_body_write(normalized):
        return ("raw GitHub API issue edit", "raw API issue body write; use one literal gh issue edit")
    indirect_payloads = command_substitutions(normalized)
    if INTERPRETER_RE.search(code_only):
        indirect_payloads.append(normalized)
    if any(INDIRECT_GH_RE.search(p) and INDIRECT_BODY_RE.search(p) for p in indirect_payloads):
        return ("indirect issue edit", "gh issue edit/api run through a shell, eval or command substitution")
    edit_matches = [m for m in MUTATION_RE.finditer(code_only) if m.group(1) == "edit"]
    parsed_edits = [
        parts
        for shell_command in shell_commands(normalized)
        if (parts := command_tokens(shell_command))[:3] == ["gh", "issue", "edit"]
    ]
    writes_body = False
    if len(parsed_edits) > len(edit_matches):
        # gh issue edit hidden from the command-word scan (quoted or escaped gh).
        writes_body = any(any(body_flags(parts)) for parts in parsed_edits) or bool(
            re.search(r"[$`]", normalized)
        )
    first_target = ""
    for match in edit_matches:
        separator = EDIT_SEPARATOR_RE.search(code_only, match.end())
        segment = normalized[match.start() : separator.start() if separator else len(normalized)]
        parts = tokens(segment)
        first_target = first_target or (parts[3] if len(parts) > 3 else "")
        if any(body_flags(parts)) or segment_may_hide_body(segment):
            writes_body = True
    if not writes_body:
        return None
    problem = literal_body_write_problem(normalized.strip())
    if problem:
        return (first_target.strip("'\"") or "unknown", problem)
    return None


def body_flags(parts: list[str]) -> tuple[list[str], list[str]]:
    inline = values(parts, {"--body", "-b"})
    files = values(parts, {"--body-file", "-F"})
    inline.extend(t[2:] for t in parts if t.startswith("-b") and len(t) > 2 and t[2] != "=")
    files.extend(t[2:] for t in parts if t.startswith("-F") and len(t) > 2 and t[2] != "=")
    return inline, files


def body_arg(parts: list[str]) -> str | None:
    inline, files = body_flags(parts)
    if inline:
        body = inline[-1]
        if "$(" in body or "<<" in body:
            return "__ICL_COMPLEX_BODY_UNSUPPORTED__"
        return body
    if not files:
        return None
    path = files[-1]
    if path == "-":
        if os.environ.get("MOP_ISSUE_CREATE_STAGE") == "audit":
            if "MOP_ISSUE_CREATE_BODY_B64" not in os.environ:
                return "__ICL_BODY_FILE_UNREADABLE__"
            return owner_buffer()
        return "__ICL_STDIN_UNSUPPORTED__"
    try:
        with open(os.path.expanduser(path), encoding="utf-8", newline="") as handle:
            return handle.read()
    except (OSError, UnicodeError):
        return "__ICL_BODY_FILE_UNREADABLE__"


def header_span(body: str) -> tuple[int, int] | None:
    """Find one unambiguous, standalone Ready Pool comment (not a fenced example)."""
    openings = list(re.finditer(r"<!--\s*ready-pool\b(?!-repair\b)", body, re.I))
    if not openings:
        return None
    opening = openings[0].start()
    block = re.compile(r"^<!--[ \t]*ready-pool:[ \t]*\r?\n(.*?)^-->(?=\r?$)", re.I | re.M | re.S)
    header = block.match(body, opening)
    prefix = body[:opening]
    if (len(openings) != 1 or not header or "<!--" in header[1] or "-->" in header[1]
            or prefix.rfind("<!--") > prefix.rfind("-->")):
        raise ValueError("ready_pool_header_unparseable")
    fence = ""
    for line in prefix.splitlines():
        mark = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if not mark:
            continue
        if not fence:
            fence = mark[1]
        elif mark[1].startswith(fence) and not mark[2].strip():
            fence = ""
    if fence:
        raise ValueError("ready_pool_header_unparseable")
    keys: set[str] = set()
    for line in header[1].splitlines():
        field = re.fullmatch(r"([A-Za-z_][\w-]*):.*", line)
        if field:
            key = field[1].lower().replace("-", "_")
            if key in keys:
                raise ValueError("ready_pool_header_unparseable")
            keys.add(key)
        elif line.strip() and not (keys and line.startswith((" ", "\t"))):
            raise ValueError("ready_pool_header_unparseable")
    if not keys:
        raise ValueError("ready_pool_header_unparseable")
    return header.span()


def check_header_only_edit(old: str, new: str) -> None:
    # Never normalize Markdown or compare stripped bodies: whitespace is content.
    before, after = header_span(old), header_span(new)
    if old == new:
        return
    if after is not None:
        start, end = after
        if before is not None:
            if old[:before[0]] == new[:start] and old[before[1]:] == new[end:]:
                return
        else:
            # Insertion may introduce blank lines to isolate a new comment, but
            # dropping that span and its new separators must recover every byte.
            left, right = new[:start], new[end:]
            for left_sep in ("", "\n", "\n\n", "\r\n", "\r\n\r\n"):
                for right_sep in ("", "\n", "\n\n", "\r\n", "\r\n\r\n"):
                    if left.endswith(left_sep) and right.startswith(right_sep):
                        if left[:len(left) - len(left_sep)] + right[len(right_sep):] == old:
                            return
    raise ValueError("ready_pool_edit_changes_existing_issue_text")


def label_hygiene_edit(parts: list[str]) -> bool:
    names = labels_arg(parts, {"--add-label", "--remove-label"})
    return (any(name.startswith(("status:", "sub:")) for name in names)
            or os.environ.get("PROXY_AGENT_NAME", "").startswith("proxy-routine-")
            or os.environ.get("PROXY_AGENT_TYPE") in {"pm-triage-agent", "pm-backlog-promoter"})


def validate(body: str) -> dict[str, object]:
    special = {
        "__ICL_COMPLEX_BODY_UNSUPPORTED__": "complex_body_requires_named_file",
        "__ICL_STDIN_UNSUPPORTED__": "stdin_body_file_requires_named_file",
        "__ICL_BODY_FILE_UNREADABLE__": "body_file_unreadable_at_hook_time",
    }
    if body in special:
        return {"ok": False, "errors": [special[body]]}
    proc = subprocess.run(
        [sys.executable, VALIDATOR, "--json"],
        input=body,
        text=True,
        capture_output=True,
        check=False,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "errors": ["validator_failed"]}


def live_issue(target: str) -> dict[str, object] | None:
    match = re.search(r"(\d+)(?:$|[/?#])", target)
    if not match:
        return None
    try:
        raw = subprocess.check_output(
            [
                GH_BIN,
                "issue",
                "view",
                match.group(1),
                "--repo",
                "Scribie/heydonna-app",
                "--json",
                "body,labels",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
        return json.loads(raw)
    except Exception:
        return None


def main() -> int:
    normalized = COMMAND.replace("\\\n", " ")
    code_only = mask_data_spans(normalized)
    refusal = body_write_refusal(normalized, code_only)
    if refusal:
        emit(refusal[0], "issue_body_write_refused", refusal[1])
        return 0
    matches = list(MUTATION_RE.finditer(code_only))
    if not matches:
        if raw_issue_create_targets_heydonna(code_only):
            emit("raw GitHub API issue creation", "unsupported_raw_api_issue_create")
        return 0

    chunks = [
        normalized[
            match.start() : (
                matches[index + 1].start()
                if index + 1 < len(matches)
                else len(normalized)
            )
        ]
        for index, match in enumerate(matches)
    ]
    parsed = [tokens(chunk) for chunk in chunks]
    relevant = [parts for parts in parsed if parts and targets_heydonna(parts)]

    # The hook sees pre-command state. A later mutation cannot rely on an
    # earlier body edit inside the same Bash call.
    if any(parts[:3] == ["gh", "issue", "edit"] for parts in relevant) and len(relevant) > 1:
        emit(
            "compound HeyDonna issue mutation",
            "issue_mutation_requires_single_command",
        )
        return 0

    for parts in relevant:
        guarded = False
        if parts[:3] == ["gh", "issue", "create"]:
            body = body_arg(parts) or ""
            target = (values(parts, {"--title"}) or ["(new issue)"])[-1]
            control_plane_reason = internal_control_plane_reason(target, body)
            if control_plane_reason:
                emit(
                    target,
                    "internal_control_plane_issue_forbidden",
                    control_plane_reason,
                )
                return 0
        elif len(parts) >= 4 and parts[:3] == ["gh", "issue", "edit"]:
            target = parts[3]
            literal = target[1:-1] if len(target) >= 2 and target[0] == target[-1] and target[0] in "'\"" else target
            if not literal.isdigit():
                # A shell variable or other non-literal form cannot be
                # verified against the live issue before the command runs.
                emit(literal, "issue_number_must_be_literal")
                return 0
            target = literal
            guarded = label_hygiene_edit(parts)
            inline, files = body_flags(parts)
            if guarded and (inline or len(files) > 1 or (files and
                    (not os.path.isabs(files[0]) or re.search(r"[$`*?\[\n]", files[0])))):
                emit(target, "label_hygiene_requires_one_literal_absolute_body_file")
                return 0
            new_body = body_arg(parts)
            live = live_issue(target)
            if live is None:
                emit(target, "label_hygiene_skip_issue_unavailable" if guarded else "could_not_verify_live_issue")
                return 0
            old = live.get("body")
            if guarded:
                if not isinstance(old, str):
                    emit(target, "ready_pool_header_unparseable")
                    return 0
                try:
                    check_header_only_edit(old, old if new_body is None else new_body)
                except ValueError as exc:
                    emit(target, str(exc))
                    return 0
            body = new_body if new_body is not None else str(old or "")
        else:
            continue

        result = validate(body)
        if not result.get("ok"):
            errors = result.get("errors", [])
            if guarded:
                errors = ["label_hygiene_skip_invalid_ledger", *errors]
            emit(target, *(str(error) for error in errors))
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
