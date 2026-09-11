#!/usr/bin/env bash
#
# Block creation of dispatchable status:todo issues unless the issue body
# carries Ready Pool frontmatter.
#
# Scope (Rajiv directive Ev0C14NF0VED, 2026-09-11): PM is blocked only from
# label-gated CI. Editing the labels of an existing issue — including
# `gh issue edit <N> --add-label status:todo` — is an ordinary label edit and is
# allowed without ceremony. Only the issue-CREATE admission point keeps this
# Ready Pool schema gate. This is intentionally narrower than the old
# block-unrouted-status-todo guard and does not block unrelated PM actions.

set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

INPUT=$(cat 2>/dev/null || echo "{}")

TOOL_NAME=$(printf '%s' "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null || true)
[ "$TOOL_NAME" = "Bash" ] || exit 0

CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null || true)
[ -n "$CMD" ] || exit 0

if printf '%s' "$CMD" | grep -qE 'PM_READY_POOL_SCHEMA_OVERRIDE=PM_OVERRIDE_WITH_PROOF'; then
  exit 0
fi

PY_OUT=$(CMD_TEXT="$CMD" python3 - <<'PY' 2>/dev/null || true
import json
import hashlib
import os
import re
import shlex
import sys
import time

cmd = os.environ.get("CMD_TEXT", "")


def command_token_segments(text: str) -> list[list[str]]:
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        tokens = text.split()

    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in {";", "&", "&&", "||", "|"}:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def labels_from(value: str) -> list[str]:
    return [p.strip() for p in value.split(",") if p.strip()]


def read_body_file(path: str) -> str:
    if not path or path == "-":
        return ""
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def clean_meta_value(value) -> str:
    # \x60 == backtick; a literal backtick here breaks macOS bash 3.2's $() parser
    cleaned = str(value or "").strip().strip("\x60\"'").strip()
    return cleaned.rstrip(";,").strip()


def parse_ready_pool_block(block: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    for line in block.splitlines():
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_ -]*)\s*[:=]\s*(.*?)\s*$", line)
        if not match:
            continue
        key = re.sub(r"[\s-]+", "_", match.group(1).strip().lower())
        meta[key] = clean_meta_value(match.group(2))

    for match in re.finditer(
        r"\b([A-Za-z_][A-Za-z0-9_ -]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s,]+)",
        block,
    ):
        key = re.sub(r"[\s-]+", "_", match.group(1).strip().lower())
        meta[key] = clean_meta_value(match.group(2))
    return meta


def ready_pool_status(body: str) -> tuple[bool, str]:
    m = re.search(r"<!--\s*ready-pool:\s*(.*?)-->", body or "", re.S | re.I)
    if not m:
        return False, "missing_ready_pool_frontmatter"
    block = m.group(1)
    meta = parse_ready_pool_block(block)
    required = ["priority", "lane", "ac_summary", "claimable_slot_type", "blockers", "work_type"]
    missing = [field for field in required if not clean_meta_value(meta.get(field))]
    if missing:
        return False, "missing_ready_pool_fields:" + ",".join(missing)
    claimable = clean_meta_value(meta.get("claimable_slot_type")).lower()
    banned = {
        "none",
        "no",
        "no-slot",
        "no_slot",
        "not-claimable",
        "not_claimable",
        "unclaimable",
        "pm",
        "pm-direct",
        "pm_direct",
        "rajiv",
        "external",
    }
    if claimable in banned:
        return False, f"non_claimable_slot_type:{claimable}"
    return True, ""


def write_debug(parsed: dict, reason: str) -> str:
    body = parsed.get("body") or ""
    digest = hashlib.sha1(
        f"{parsed.get('op')}|{parsed.get('target')}|{reason}|{body[:500]}".encode("utf-8", errors="replace")
    ).hexdigest()[:12]
    path = f"/tmp/ready-pool-schema-block-{int(time.time())}-{digest}.json"
    payload = {
        "schema_version": 1,
        "source": "block-status-todo-without-ready-pool",
        "op": parsed.get("op"),
        "target": parsed.get("target"),
        "reason": reason,
        "fetch_error": parsed.get("fetch_error") or "",
        "body_len": len(body),
        "body_preview": body[:600],
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
        return path
    except OSError:
        return ""


def parse_create(parts: list[str]) -> dict | None:
    if parts[:3] != ["gh", "issue", "create"]:
        return None
    labels: list[str] = []
    title = ""
    body = ""
    i = 3
    while i < len(parts):
        token = parts[i]
        if token in ("--label", "-l") and i + 1 < len(parts):
            labels.extend(labels_from(parts[i + 1]))
            i += 2
        elif token.startswith("--label="):
            labels.extend(labels_from(token.split("=", 1)[1]))
            i += 1
        elif token == "--title" and i + 1 < len(parts):
            title = parts[i + 1]
            i += 2
        elif token.startswith("--title="):
            title = token.split("=", 1)[1]
            i += 1
        elif token == "--body" and i + 1 < len(parts):
            body = parts[i + 1]
            i += 2
        elif token.startswith("--body="):
            body = token.split("=", 1)[1]
            i += 1
        elif token == "--body-file" and i + 1 < len(parts):
            body = read_body_file(parts[i + 1])
            i += 2
        elif token.startswith("--body-file="):
            body = read_body_file(token.split("=", 1)[1])
            i += 1
        else:
            i += 1
    if "status:todo" not in labels:
        return None
    return {"op": "create", "target": title or "(new issue)", "body": body, "fetch_error": ""}


for parts in command_token_segments(cmd):
    parsed = parse_create(parts)
    if not parsed:
        continue
    ok, reason = ready_pool_status(parsed["body"])
    if ok:
        continue
    debug_path = write_debug(parsed, reason)
    print(json.dumps({
        "block": True,
        "op": parsed["op"],
        "target": parsed["target"],
        "fetch_error": parsed["fetch_error"],
        "reason": reason,
        "debug_path": debug_path,
    }))
    raise SystemExit(0)
PY
)

[ -n "$PY_OUT" ] || exit 0
BLOCK=$(printf '%s' "$PY_OUT" | jq -r '.block // false' 2>/dev/null || echo false)
[ "$BLOCK" = "true" ] || exit 0

TARGET=$(printf '%s' "$PY_OUT" | jq -r '.target // "unknown"' 2>/dev/null || echo unknown)
OP=$(printf '%s' "$PY_OUT" | jq -r '.op // "issue mutation"' 2>/dev/null || echo "issue mutation")
FETCH_ERROR=$(printf '%s' "$PY_OUT" | jq -r '.fetch_error // empty' 2>/dev/null || true)
REASON=$(printf '%s' "$PY_OUT" | jq -r '.reason // "invalid_ready_pool_frontmatter"' 2>/dev/null || echo invalid_ready_pool_frontmatter)
DEBUG_PATH=$(printf '%s' "$PY_OUT" | jq -r '.debug_path // empty' 2>/dev/null || true)

extra=""
[ -n "$FETCH_ERROR" ] && extra=" Current issue body could not be verified (${FETCH_ERROR}), so this fails closed."
[ -n "$DEBUG_PATH" ] && extra="${extra} Debug: ${DEBUG_PATH}."

cat <<EOF
{"decision":"block","message":"BLOCKED: ${OP} would create '${TARGET}' as status:todo without dispatchable Ready Pool frontmatter (${REASON}). Add <!-- ready-pool: priority, lane, ac_summary, claimable_slot_type, blockers, work_type --> with a claimable slot type before creating a dispatchable issue, or use PM_READY_POOL_SCHEMA_OVERRIDE=PM_OVERRIDE_WITH_PROOF with explicit routing proof.${extra}"}
EOF
