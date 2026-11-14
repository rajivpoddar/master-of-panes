#!/usr/bin/env bash
# OWNER-STAGE COPY of block-missing-directive-contract-issue-create.sh (peer filing check, run by issue-create-owner.py).
# Source: heydonna-app/.claude/hooks/block-missing-directive-contract-issue-create.sh. The acceptance logic below is that hook's, unchanged.
# The create owner synthesizes ONE gh issue create command and holds the issue body in memory.
# Those peer hooks only recognize a command that begins with gh issue create, so they never saw the
# owner command, and a named body file never exists. This copy runs the same decision for the same
# bytes: when the body-file token is - and MOP_ISSUE_CREATE_STAGE=audit, the body is read from
# MOP_ISSUE_CREATE_BODY_B64 instead of a file. The owner command is parsed as a single segment, so a title or label containing ; | & is not cut.
# Nothing is written to disk in that stage.
#
# Block directive/customer/pipeline-looking `gh issue create` calls unless the
# body includes the Directive Contract section. This catches issue-intake drift:
# quoting Rajiv is not enough; the issue must preserve intended capability,
# runtime control point, and prod-shaped AC before dispatch.

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


def looks_directive_scoped(title: str, body: str, labels: list[str]) -> bool:
    text = f"{title}\n{body}\n{' '.join(labels)}"
    lower = text.lower()

    if "## directive contract" in lower:
        return False

    strong_markers = [
        "rajiv directive",
        "original directive",
        "rajiv:",
        "rajiv says",
        "rajiv said",
        "source thread",
        "slack thread",
        "thread_ts",
        "customer report",
        "customer-visible",
        "explore-issue",
    ]
    if any(marker in lower for marker in strong_markers):
        return True

    pipeline_markers = [
        "pipeline",
        "modal",
        "format",
        "proofread",
        "speaker correction",
        "validator",
        "bon",
        "retry",
        "fallback",
        "watchdog",
        "r2",
        "langsmith",
        "axiom",
        "transcript",
        "docx",
    ]
    action_markers = [
        "tunable",
        "threshold",
        "gate",
        "control point",
        "state transition",
        "recover",
        "stall",
        "timeout",
        "degraded",
        "exhaust",
        "hard reject",
        "soft warn",
    ]
    if any(p in lower for p in pipeline_markers) and any(a in lower for a in action_markers):
        return True

    return False


for segment in command_segments(cmd):
    parsed = parse_segment(segment)
    if not parsed:
        continue
    if not looks_directive_scoped(parsed["title"], parsed["body"], parsed["labels"]):
        continue
    print(json.dumps({
        "block": True,
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
{"decision":"block","message":"BLOCKED: issue create for '${TITLE}' looks Rajiv/customer/pipeline directive-scoped but is missing '## Directive Contract'. Add Original directive, Intended capability, Prod failure mode, Runtime control point, Tunable vs behavior, Directive match check, and prod-shaped AC before filing. This prevents implementing a narrower issue than Rajiv asked for."}
EOF
