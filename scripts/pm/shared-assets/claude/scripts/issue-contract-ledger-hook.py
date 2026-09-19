#!/usr/bin/env python3
"""Fail closed on invalid HeyDonna issue creation and mutation."""

from __future__ import annotations

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
HEYDONNA_REPO = "heydonna-app/heydonna-app"
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
        return explicit.lower() == HEYDONNA_REPO
    return current_repo().lower() == HEYDONNA_REPO


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
            if repo.lower() != HEYDONNA_REPO:
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


def body_arg(parts: list[str]) -> str | None:
    inline = values(parts, {"--body"})
    if inline:
        body = inline[-1]
        if "$(" in body or "<<" in body:
            return "__ICL_COMPLEX_BODY_UNSUPPORTED__"
        return body
    files = values(parts, {"--body-file"})
    if not files:
        return None
    path = files[-1]
    if path == "-":
        return "__ICL_STDIN_UNSUPPORTED__"
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return "__ICL_BODY_FILE_UNREADABLE__"


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
                "heydonna-app/heydonna-app",
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
            new_body = body_arg(parts)
            live = live_issue(target)
            if live is None:
                emit(target, "could_not_verify_live_issue")
                return 0
            body = new_body if new_body is not None else str(live.get("body") or "")
        else:
            continue

        result = validate(body)
        if not result.get("ok"):
            errors = result.get("errors", [])
            emit(target, *(str(error) for error in errors))
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
