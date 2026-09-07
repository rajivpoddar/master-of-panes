#!/usr/bin/env bash
# Fail closed for substantive product/app issue mutations without the
# canonical Issue Contract Ledger. Internal control-plane reimplementation
# issues are not product issues and must not create a second PM obligation.

set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

PARSER="${ISSUE_CONTRACT_LEDGER_HOOK_PARSER:-/Users/rajiv/.claude/scripts/issue-contract-ledger-hook.py}"
VALIDATOR="${ISSUE_CONTRACT_LEDGER_VALIDATOR:-/Users/rajiv/.claude/scripts/validate-issue-contract-ledger.py}"
GH_BIN="${ISSUE_CONTRACT_LEDGER_GH_BIN:-gh}"
INPUT=$(cat 2>/dev/null || echo '{}')
[ "$(printf '%s' "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null || true)" = "Bash" ] || exit 0
CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null || true)
[ -n "$CMD" ] || exit 0

PY_OUT=$(
  CMD_TEXT="$CMD" VALIDATOR="$VALIDATOR" GH_BIN="$GH_BIN" \
    python3 "$PARSER" 2>/dev/null || true
)
[ -n "$PY_OUT" ] || exit 0
[ "$(printf '%s' "$PY_OUT" | jq -r '.block // false')" = true ] || exit 0

TARGET=$(printf '%s' "$PY_OUT" | jq -r '.target // "unknown"')
ERRORS=$(printf '%s' "$PY_OUT" | jq -r '.errors | join(",")')

# The parser's existing control-plane classifier is the authority. Do not
# duplicate its result into a PM obligation and do not require product ICL
# ceremony for an internal reimplementation issue.
if printf '%s' "$ERRORS" | grep -q 'internal_control_plane_issue_forbidden'; then
  exit 0
fi

cat <<EOF
{"decision":"block","message":"BLOCKED: '${TARGET}' requires a valid Issue Contract Ledger (${ERRORS}). Put the complete issue body in a named --body-file before the Bash call, validate it, then run exactly one gh issue create/edit mutation."}
EOF
exit 0
