#!/usr/bin/env bash
# explore-issue-permit-consume-gate.sh — PreToolUse hook; runs AFTER all other
# blocking hooks in the Bash chain (position after block-invalid-issue-contract-
# ledger.sh in settings.json).
#
# ROOT-CAUSE FIX for the permit-consumed-but-gh-never-ran defect:
#   pre-issue-create-audit.sh (hook N) writes a pending file instead of consuming
#   the permit. Any hook between N and this finalizer can still block; if it does,
#   the actual permit file is untouched and the pending file is left behind (cleaned
#   up on the next attempt). This hook atomically moves the permit to consumed
#   (persist-UNKNOWN-first design) only when ALL blocking hooks have passed.
#
# Preserve-UNKNOWN guarantee: if gh fails AFTER this hook returns 0, the permit
# is in consumed/UNKNOWN state. Recovery requires the manual
# explore_issue_restore_permit path with proven GitHub absence. No unsafe
# auto-restore is wired here: absence of a gh response is not proof of no create.
#
# This hook is a no-op for gh commands that have no pending permit (e.g., internal
# creates with an audit-bypass marker; those never write a pending file).

set -euo pipefail

INPUT=$(cat)
CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)
[ -n "$CMD" ] || exit 0

# Only fire for gh issue create commands.
MATCH_GH_ISSUE_CREATE=""
while IFS= read -r seg; do
  seg=$(printf '%s' "$seg" | sed 's/^[[:space:]]*//')
  case "$seg" in echo\ *|printf\ *|cat\ *|bash\ *|source\ *) continue ;; esac
  if printf '%s' "$seg" | grep -qE '^gh[[:space:]]+issue[[:space:]]+create'; then
    MATCH_GH_ISSUE_CREATE="yes"
    break
  fi
done <<< "$(printf '%s' "$CMD" | tr ';' '\n' | tr '|' '\n' | tr '&' '\n')"
[ -n "$MATCH_GH_ISSUE_CREATE" ] || exit 0

# Source the shared guard library.
# shellcheck disable=SC1090
source "${EXPLORE_ISSUE_LATCH_GUARD:-/Users/rajiv/.claude/hooks/explore-issue-latch-guard.sh}"

# ---------------------------------------------------------------------------
# Extract body and Source: channel:thread_ts
# ---------------------------------------------------------------------------
_extract_body_consume_gate() {
  local cmd="$1" body="" body_file
  body_file=$(printf '%s' "$cmd" | \
    perl -ne 'if (/--body-file[\s=]+(?:"([^"]+)"|'\''([^'\'']+)'\''|(\S+))/) { print $1 || $2 || $3; exit }' \
    2>/dev/null || true)
  if [ -n "$body_file" ] && [ -f "$body_file" ]; then
    body=$(cat "$body_file" 2>/dev/null || true)
  fi
  if [ -z "$body" ]; then
    body=$(printf '%s' "$cmd" | \
      perl -ne 'BEGIN{$/=undef} if (/--body[\s=]+"((?:[^"\\]|\\.)*)"/s) { my $v=$1; $v =~ s/\\"/"/g; print $v; exit }' \
      2>/dev/null || true)
  fi
  printf '%s' "$body"
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    printf '%s' "$1" | sha256sum | awk '{print $1}'
  else
    printf '%s' "$1" | shasum -a 256 | awk '{print $1}'
  fi
}

BODY=$(_extract_body_consume_gate "$CMD")
BODY_OR_CMD="${BODY:-$CMD}"
BODY_SHA=$(sha256_of "$BODY_OR_CMD")

TITLE=""
TITLE=$(printf '%s' "$CMD" | \
  perl -ne 'BEGIN{$/=undef} if (/--title[\s=]+"((?:[^"\\]|\\.)*)"/s) { my $v=$1; $v =~ s/\\"/"/g; print $v; exit }' \
  2>/dev/null || true)
TITLE_DIGEST=$(sha256_of "${TITLE:-}")

# Extract Source: CHANNEL:THREAD_TS (simple grep; full verification happened in
# pre-issue-create-audit.sh; we only need the key to look up the pending file).
SOURCE_LINE=$(printf '%s' "$BODY_OR_CMD" | \
  grep -iE '^[[:space:]]*Source:[[:space:]]*[CD][A-Z0-9]{10}:[0-9]{10}\.[0-9]{6}' \
  | head -1 || true)
CHANNEL=""
THREAD_TS=""
if [ -n "$SOURCE_LINE" ]; then
  CHANNEL=$(printf '%s' "$SOURCE_LINE" | sed -E 's/.*Source:[[:space:]]*//' | cut -d: -f1)
  THREAD_TS=$(printf '%s' "$SOURCE_LINE" | sed -E 's/.*Source:[[:space:]]*//' | cut -d: -f2)
fi
[ -n "$CHANNEL" ] && [ -n "$THREAD_TS" ] || exit 0

export EXPLORE_ISSUE_SOURCE_MESSAGE_TS="${EXPLORE_ISSUE_SOURCE_MESSAGE_TS:-$THREAD_TS}"

# ---------------------------------------------------------------------------
# Atomically consume the pending permit (persist-UNKNOWN-first)
# ---------------------------------------------------------------------------
explore_issue_consume_pending_permit "$CHANNEL" "$THREAD_TS" "$BODY_SHA" "$TITLE_DIGEST" || true
# Whether or not a pending permit existed, allow the tool to run.
# (The pre-issue-create-audit.sh hook already blocked all invalid creates.)
exit 0
