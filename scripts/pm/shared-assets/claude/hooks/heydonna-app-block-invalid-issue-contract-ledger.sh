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
if printf '%s' "$ERRORS" | grep -q 'internal_control_plane_issue_forbidden' \
   && ! printf '%s' "$ERRORS" | grep -Eq 'body_file_unreadable_at_hook_time|stdin_body_file_requires_named_file|complex_body_requires_named_file'; then
  # The parser may stop at the first internal issue and never inspect a later
  # mutation. Reuse its mutation grammar to require that the exempted command
  # contains exactly one issue create/edit, and do not treat an unreadable
  # body file as proof of an internal issue.
  MUTATION_INFO=$(printf '%s' "$CMD" | python3 -c '
import re, sys
import os
import shlex
pattern = re.compile(r"(?<![A-Za-z0-9_.-])(?:/[^\s;&|()]+/)?gh\s+issue\s+(?:create|edit)\b")
command = sys.stdin.read().replace("\\\n", " ")
try:
    parts = shlex.split(command, posix=True)
except ValueError:
    parts = []
body_files = []
for index, part in enumerate(parts):
    if part == "--body-file" and index + 1 < len(parts):
        body_files.append(parts[index + 1])
    elif part.startswith("--body-file="):
        body_files.append(part.split("=", 1)[1])
unsafe_body = any(
    path == "-"
    or not os.path.isfile(os.path.expanduser(path))
    or not os.access(os.path.expanduser(path), os.R_OK)
    for path in body_files
)
print(len(pattern.findall(command)), "unsafe" if unsafe_body else "safe")
' 2>/dev/null || printf '0')
  MUTATION_COUNT=${MUTATION_INFO%% *}
  BODY_STATUS=${MUTATION_INFO#* }
  if [ "$MUTATION_COUNT" -ne 1 ] || [ "$BODY_STATUS" != "safe" ]; then
    cat <<EOF
{"decision":"block","message":"BLOCKED: internal issue admission requires exactly one gh issue create/edit mutation with a readable named body; compound, ambiguous, or unreadable-body commands remain refused before the internal exemption."}
EOF
    exit 0
  fi
  exit 0
fi

if printf '%s' "$ERRORS" | grep -q 'issue_number_must_be_literal'; then
  cat <<EOF
{"decision":"block","message":"BLOCKED: the issue target '${TARGET}' is not a literal number, so this hook cannot verify it against the live issue before the command runs. Re-run exactly one gh issue edit with the literal issue number (for example 7945). Shell variables and other computed forms cannot be verified pre-execution. No issue was mutated."}
EOF
  exit 0
fi
cat <<EOF
{"decision":"block","message":"BLOCKED: '${TARGET}' requires a valid Issue Contract Ledger (${ERRORS}). Put the complete issue body in a named --body-file before the Bash call, validate it, then run exactly one gh issue create/edit mutation."}
EOF
exit 0
