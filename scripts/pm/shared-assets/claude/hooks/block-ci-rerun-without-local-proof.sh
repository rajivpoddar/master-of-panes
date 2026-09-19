#!/usr/bin/env bash
# PreToolUse Bash hook: label-gated CI/E2E is CTO-owned.
#
# Rajiv directive Ev0C14NF0VED (2026-09-11): PM is blocked only from
# label-gated CI. Ordinary PR/issue label additions, removals, and replacements
# (including pm-blocked:*, merge-ready, status:*, priority:*, and ownership
# labels) are allowed. This hook blocks only the real CI-trigger boundary — the
# pm-state:qa-passed-awaiting-ci label rename/dispatch — and the raw GitHub CI
# dispatch/rerun forms, and it routes PM to request the CTO.
#
# Precision rule: match the EXECUTED invocation (unquoted command words in
# command position), never free-text prose carried inside quoted arguments,
# heredoc bodies, or comments. Describing a prohibited command in a ledger
# row or a --action string must not trip the guard; running it still does.

set -euo pipefail

INPUT="$(cat 2>/dev/null || echo '{}')"
CMD="$(JSON_INPUT="$INPUT" python3 - <<'PYEOF' 2>/dev/null || true
import json
import os

data = json.loads(os.environ.get("JSON_INPUT", "{}") or "{}")
print(((data.get("tool_input") or {}).get("command")) or "")
PYEOF
)"

[ -n "$CMD" ] || exit 0

BLOCK_REASON="$(HOOK_CMD="$CMD" python3 - <<'PYEOF' 2>/dev/null || true
import os


CI_WORKFLOWS = {"ci.yml", "e2e.yml", "CI", "E2E Smoke Tests"}
CAPTURE_WORKFLOWS = {"e2e-llm-proxy-capture.yml", "E2E LLM Proxy Capture (manual)"}
QA_LABEL = "pm-state:qa-passed-awaiting-ci"
ALLOWLISTED = {
    "request-label-gated-ci.sh",
    "rerun-after-local-proof.sh",
    "rerun-main-after-local-proof.sh",
    "request-local-gated-capture.sh",
    "request-budgeted-remote-capture.sh",
}
SHELL_WRAPPERS = {"sh", "bash", "dash", "zsh"}
LEAD_SKIP = {"sudo", "rtk", "env"}
WF_RUNNERS = {"gh"}


def basename(word):
    return word.rsplit("/", 1)[-1]


def split_segments(text):
    """Split shell text into segments of (word, quoted) tokens.

    Quoted spans, heredoc bodies, and comments are data, never invocations.
    Dollar-paren and backtick spans execute, so their contents are yielded as live segments.
    """
    segments = []
    words = []
    word, quoted = [], False
    i, n = 0, len(text)

    def flush_word():
        nonlocal word, quoted
        if word or quoted:
            words.append(("".join(word), quoted))
        word, quoted = [], False

    def flush_segment():
        flush_word()
        if words:
            segments.append(words[:])
        words.clear()

    while i < n:
        ch = text[i]
        if ch == "#":
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        if ch == "\n":
            flush_segment()
            i += 1
            continue
        if ch in " \t\r":
            flush_word()
            i += 1
            continue
        if ch in ";":
            flush_segment()
            i += 1
            continue
        if ch in "&|()":
            if ch == "&" and text[i:i + 2] == "&&":
                i += 2
            elif ch == "|" and text[i:i + 2] == "||":
                i += 2
            else:
                i += 1
            flush_segment()
            continue
        if ch == "<" and text[i:i + 2] == "<<":
            j = i + 2
            if text[j:j + 1] == "-":
                j += 1
            while j < n and text[j] in " \t":
                j += 1
            quoted_delim = False
            if j < n and text[j] in "'\"":
                quoted_delim = True
                quote = text[j]
                j += 1
                start = j
                while j < n and text[j] != quote:
                    j += 1
                delim = text[start:j]
                j += 1
            else:
                start = j
                while j < n and text[j] not in " \t\r\n;&|()":
                    j += 1
                delim = text[start:j]
            _ = quoted_delim
            k = text.find("\n", j)
            k = n if k == -1 else k + 1
            while k < n:
                m = text.find("\n", k)
                line = text[k:] if m == -1 else text[k:m]
                if line.strip() == delim:
                    k = n if m == -1 else m + 1
                    break
                k = n if m == -1 else k + 1
            flush_word()
            i = k
            continue
        if ch == "'" or ch == '"':
            quote = ch
            i += 1
            while i < n and text[i] != quote:
                if quote == '"' and text[i] == "\\" and i + 1 < n:
                    word.append(text[i + 1])
                    i += 2
                else:
                    word.append(text[i])
                    i += 1
            i += 1
            quoted = True
            continue
        if ch == "\\" and i + 1 < n:
            word.append(text[i + 1])
            i += 2
            continue
        if ch == chr(96):
            j = i + 1
            inner = []
            while j < n and text[j] != chr(96):
                if text[j] == "\\" and j + 1 < n:
                    inner.append(text[j + 1])
                    j += 2
                else:
                    inner.append(text[j])
                    j += 1
            flush_word()
            for seg in split_segments("".join(inner)):
                if seg:
                    segments.append(seg)
            i = j + 1
            continue
        if ch == "$" and text[i:i + 2] == "$" + "(":
            depth = 1
            j = i + 2
            in_q = None
            while j < n and depth:
                c = text[j]
                if in_q:
                    if c == "\\" and in_q == '"':
                        j += 2
                        continue
                    if c == in_q:
                        in_q = None
                elif c in "'\"":
                    in_q = c
                elif c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                j += 1
            flush_word()
            for seg in split_segments(text[i + 2:j - 1]):
                if seg:
                    segments.append(seg)
            i = j
            continue
        word.append(ch)
        i += 1
    flush_segment()
    return [seg for seg in segments if seg]


def bare(words):
    """Strip leading assignments and sudo/rtk/env wrappers from a segment."""
    items = list(words)
    while items:
        text, is_quoted = items[0]
        if is_quoted:
            break
        name = text
        if "=" in name and name.split("=", 1)[0].replace("_", "").isalnum() and name[0].isalpha():
            items.pop(0)
            continue
        if name in LEAD_SKIP:
            items.pop(0)
            continue
        break
    return items


def invoked_value(words, flag):
    for index, (text, is_quoted) in enumerate(words):
        if not is_quoted and text == flag and index + 1 < len(words):
            return words[index + 1][0]
    return None


def classify_segment(words):
    core = bare(words)
    if not core:
        return ""
    head, head_quoted = core[0]
    if head_quoted:
        return ""
    name = basename(head)
    if name in ALLOWLISTED:
        return "ALLOWLIST"
    if name in SHELL_WRAPPERS:
        for index, (text, is_quoted) in enumerate(core):
            if not is_quoted and text == "-c" and index + 1 < len(core):
                nested = classify_command(core[index + 1][0])
                if nested and nested != "ALLOWLIST":
                    return nested
        return ""
    if name not in WF_RUNNERS:
        if name == "pm-state-replace.sh":
            rest = [text for text, is_quoted in core[1:] if not is_quoted]
            if len(rest) >= 2 and rest[0].isdigit() and rest[1] == QA_LABEL:
                return "raw qa-passed-awaiting-ci state replacement"
        return ""
    rest = [text for text, is_quoted in core[1:] if not is_quoted]
    if rest[:2] == ["run", "rerun"]:
        return "naked gh run rerun"
    if rest[:2] == ["workflow", "run"]:
        for token in rest[2:]:
            if token.startswith("-"):
                continue
            if basename(token) in CI_WORKFLOWS or token in CI_WORKFLOWS:
                return "raw CI/E2E workflow dispatch"
            if basename(token) in CAPTURE_WORKFLOWS or token in CAPTURE_WORKFLOWS:
                return "raw manual capture workflow dispatch"
        return ""
    if len(rest) >= 2 and rest[0] in ("pr", "issue") and "edit" in rest[1:]:
        value = invoked_value(core, "--add-label")
        if value == QA_LABEL:
            return "manual qa-passed-awaiting-ci label add"
        return ""
    if rest[:1] == ["api"]:
        if any(QA_LABEL in text for text, _ in core):
            return "manual qa-passed-awaiting-ci API mutation"
    return ""


def classify_command(text):
    allowlisted = False
    for segment in split_segments(text):
        reason = classify_segment(segment)
        if reason == "ALLOWLIST":
            allowlisted = True
        elif reason:
            return reason
    return "" if not allowlisted else ""


command = os.environ.get("HOOK_CMD", "")
print(classify_command(command))
PYEOF
)"

BLOCK_HINT=""

extract_pr() {
  printf '%s' "$CMD" | sed -nE 's/.*gh[[:space:]]+pr[[:space:]]+edit[[:space:]]+#?([0-9]+).*/\1/p' | head -1
}

if [ "$BLOCK_REASON" = "manual qa-passed-awaiting-ci label add" ]; then
  PR="$(extract_pr)"
  if [ -n "${PR:-}" ]; then
    BLOCK_HINT="Label-gated CI/E2E is CTO-owned. Request the CTO (Abhijit) to arm exact-head label-gated CI for PR #${PR}; the CTO runs request-label-gated-ci.sh --pr ${PR}."
  else
    BLOCK_HINT="Label-gated CI/E2E is CTO-owned. Request the CTO (Abhijit) to arm it; the CTO runs request-label-gated-ci.sh --pr <PR>."
  fi
elif [ "$BLOCK_REASON" = "manual qa-passed-awaiting-ci API mutation" ]; then
  BLOCK_HINT="Label-gated CI/E2E is CTO-owned; do not arm it through gh api. Request the CTO (Abhijit) to arm exact-head label-gated CI."
elif [ "$BLOCK_REASON" = "raw qa-passed-awaiting-ci state replacement" ]; then
  PR="$(printf '%s' "$CMD" | sed -nE 's/.*pm-state-replace\.sh[[:space:]]+([0-9]+)[[:space:]]+qa-passed-awaiting-ci.*/\1/p' | head -1)"
  if [ -n "${PR:-}" ]; then
    BLOCK_HINT="Label-gated CI/E2E is CTO-owned. Request the CTO (Abhijit) to arm exact-head label-gated CI for PR #${PR}."
  fi
fi

[ -n "$BLOCK_REASON" ] || exit 0

cat >&2 <<'ERR'
BLOCKED: label-gated CI/E2E is CTO-owned; PM cannot arm or rerun GitHub CI directly.

CI is a gate, not the dev-slot test loop. Arming label-gated CI/E2E is a
CTO-owned trigger. Request the CTO (Abhijit) to arm exact-head label-gated CI
for the PR; the CTO runs:

  /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/ci/request-label-gated-ci.sh --pr <PR>

Ordinary PR/issue label additions, removals, and replacements remain allowed,
including pm-blocked:*, merge-ready, status:*, priority:*, and ownership labels.
Blocked here are only `gh run rerun`, `gh workflow run` for CI/E2E, manual
`pm-state:qa-passed-awaiting-ci` label edits, and direct `gh api` mutations of
that CI-trigger label. Raw capture-workflow dispatch stays blocked.
ERR
printf 'Blocked command class: %s\n' "$BLOCK_REASON" >&2
[ -n "$BLOCK_HINT" ] && printf '%s\n' "$BLOCK_HINT" >&2
exit 2
