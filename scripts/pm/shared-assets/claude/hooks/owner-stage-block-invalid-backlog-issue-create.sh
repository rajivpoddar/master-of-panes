#!/usr/bin/env bash
# OWNER-STAGE COPY of block-invalid-backlog-issue-create.sh (peer filing check, run by issue-create-owner.py).
# Source: heydonna-app/.claude/hooks/block-invalid-backlog-issue-create.sh. The acceptance logic below is that hook's, unchanged.
# The create owner synthesizes ONE gh issue create command and holds the issue body in memory.
# Those peer hooks only recognize a command that begins with gh issue create, so they never saw the
# owner command, and a named body file never exists. This copy runs the same decision for the same
# bytes: when the body-file token is - and MOP_ISSUE_CREATE_STAGE=audit, the body is read from
# MOP_ISSUE_CREATE_BODY_B64 instead of a file. The owner command is parsed as a single segment, so a title or label containing ; | & is not cut.
# Nothing is written to disk in that stage.
#
# Block `gh issue create --label status:backlog` unless the issue body proves it
# is intentionally parked. This is a pre-create guard: it does not silently
# mutate labels after the issue is filed.

set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

INPUT=$(cat 2>/dev/null || echo "{}")

TOOL_NAME=$(printf '%s' "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null || true)
if [ "$TOOL_NAME" != "Bash" ]; then
  exit 0
fi

CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null || true)
if [ -z "$CMD" ]; then
  exit 0
fi

PY_OUT=$(CMD_TEXT="$CMD" python3 - <<'PY' 2>/dev/null || true
import base64
import json
import os
import re
import shlex
import sys

cmd = os.environ.get("CMD_TEXT", "")

# Create owner stage: --body-file - is the in-memory buffer of the create owner, not an unnamed stdin.
owner_stage = os.environ.get("MOP_ISSUE_CREATE_STAGE") == "audit"


def owner_body() -> str:
    raw = os.environ.get("MOP_ISSUE_CREATE_BODY_B64")
    if raw is None:
        return ""
    try:
        return base64.b64decode(raw.encode("ascii"), validate=True).decode("utf-8", "replace")
    except ValueError:
        return ""

def command_segments(text: str) -> list[str]:
    # Owner stage: the command is the ONE synthesized gh issue create, so it is a single segment.
    # Splitting it on ; | & would cut a title or label that contains those characters.
    if owner_stage:
        return [text.strip()] if text.strip() else []
    # Good enough for Claude hook command strings; fail closed later if parsing
    # cannot recover body metadata for a backlog create.
    return [s.strip() for s in re.split(r"[;|&]\s*", text) if s.strip()]

def parse_segment(segment: str) -> dict | None:
    try:
        parts = shlex.split(segment, posix=True)
    except ValueError:
        parts = segment.split()
    if len(parts) < 3 or parts[0:3] != ["gh", "issue", "create"]:
        return None

    labels: list[str] = []
    title = ""
    body = ""
    body_file = ""
    i = 3
    while i < len(parts):
        token = parts[i]
        value = None
        if token in ("--label", "-l") and i + 1 < len(parts):
            value = parts[i + 1]
            i += 2
        elif token.startswith("--label="):
            value = token.split("=", 1)[1]
            i += 1
        elif token == "--title" and i + 1 < len(parts):
            title = parts[i + 1]
            i += 2
            continue
        elif token.startswith("--title="):
            title = token.split("=", 1)[1]
            i += 1
            continue
        elif token == "--body" and i + 1 < len(parts):
            body = parts[i + 1]
            i += 2
            continue
        elif token.startswith("--body="):
            body = token.split("=", 1)[1]
            i += 1
            continue
        elif token == "--body-file" and i + 1 < len(parts):
            body_file = parts[i + 1]
            i += 2
            continue
        elif token.startswith("--body-file="):
            body_file = token.split("=", 1)[1]
            i += 1
            continue
        else:
            i += 1
        if value is not None:
            labels.extend([p.strip() for p in value.split(",") if p.strip()])

    if body_file == "-" and owner_stage:
        body = owner_body()
    elif body_file and body_file != "-":
        try:
            with open(os.path.expanduser(body_file), "r", encoding="utf-8") as f:
                body = f.read()
        except OSError:
            body = body or ""

    return {
        "segment": segment,
        "labels": labels,
        "title": title,
        "body": body,
    }

def has_label(labels: list[str], wanted: str) -> bool:
    return any(label == wanted for label in labels)

def valid_backlog(title: str, body: str) -> tuple[bool, str]:
    text = f"{title}\n{body}"
    lower = text.lower()

    parent = bool(re.search(
        r"(parent:\s*#\d+|blocked\s+on\s*#\d+|blocked-on:\s*#\d+|blocked-by|depends\s+on\s*#\d+|dependency|parent=)",
        lower,
    ))
    unblock = bool(re.search(r"(unblock condition|unblock-condition|unblock\b|unblock=|blocked until|depends on)", lower))
    owner = bool(re.search(r"(owner\s*:|owner=|next reviewer|next_reviewer|pm-held|upstream owner)", lower))
    if parent and unblock and owner:
        return True, "park_metadata"

    investigation_or_tracking = bool(re.search(
        r"(^|\n|\s)(investigation|tracking issue|tracker|umbrella tracker|prd/exploration|exploration only|upstream dependency|blocked-on-upstream)(:|\b)",
        lower,
    ))
    not_dispatch = bool(re.search(
        r"(not implementation|not for slot dispatch|tracking only|investigation[- ]first|blocked-on-upstream|upstream owns|prd-only)",
        lower,
    ))
    if investigation_or_tracking and (not_dispatch or parent or owner):
        return True, "investigation_or_tracking"

    return False, "missing_backlog_intent"

for segment in command_segments(cmd):
    parsed = parse_segment(segment)
    if not parsed:
        continue
    if not has_label(parsed["labels"], "status:backlog"):
        continue
    ok, reason = valid_backlog(parsed["title"], parsed["body"])
    if ok:
        continue
    print(json.dumps({
        "block": True,
        "reason": reason,
        "title": parsed["title"],
        "labels": parsed["labels"],
    }))
    raise SystemExit(0)
PY
)

if [ -z "$PY_OUT" ]; then
  exit 0
fi

BLOCK=$(printf '%s' "$PY_OUT" | jq -r '.block // false' 2>/dev/null || echo false)
if [ "$BLOCK" != "true" ]; then
  exit 0
fi

TITLE=$(printf '%s' "$PY_OUT" | jq -r '.title // "(unknown title)"' 2>/dev/null || echo "(unknown title)")

cat <<EOF
{"decision":"block","message":"BLOCKED: invalid status:backlog issue create for '${TITLE}'. status:backlog is only for investigation/tracking/upstream/umbrella items or explicitly parked work with parent/dependency, unblock condition, and owner/next reviewer in the body. For actionable implementation work, file as status:todo with Ready Pool metadata so slot-dispatch-sweep can assign or queue it. Do not silently convert labels after create."}
EOF
