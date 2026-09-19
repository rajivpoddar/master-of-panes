#!/usr/bin/env bash
# Fail closed when any HeyDonna issue is created or mutated without the
# canonical Issue Contract Ledger.

set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

PARSER="${ISSUE_CONTRACT_LEDGER_HOOK_PARSER:-/Users/rajiv/.claude/scripts/issue-contract-ledger-hook.py}"
VALIDATOR="${ISSUE_CONTRACT_LEDGER_VALIDATOR:-/Users/rajiv/.claude/scripts/validate-issue-contract-ledger.py}"
GH_BIN="${ISSUE_CONTRACT_LEDGER_GH_BIN:-gh}"
TEMPLATE="${ISSUE_CONTRACT_LEDGER_TEMPLATE:-/Users/rajiv/.claude/templates/issue-contract-ledger.md}"
INPUT=$(cat 2>/dev/null || echo '{}')
if ! TOOL_NAME=$(printf '%s' "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null); then
  printf '%s\n' '{"decision":"block","message":"BLOCKED: the issue-contract-ledger input could not be classified (jq extraction failed or the input was malformed); no issue was mutated."}'
  exit 0
fi
[ "$TOOL_NAME" = "Bash" ] || exit 0
if ! CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null); then
  printf '%s\n' '{"decision":"block","message":"BLOCKED: the issue-contract-ledger input could not be classified (jq extraction failed or the input was malformed); no issue was mutated."}'
  exit 0
fi
CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null || true)
[ -n "$CMD" ] || exit 0

set +e
PY_OUT=$(
  CMD_TEXT="$CMD" HOOK_CWD="$CWD" VALIDATOR="$VALIDATOR" GH_BIN="$GH_BIN" \
    python3 "$PARSER" 2>/dev/null
)
PARSER_RC=$?
set -e
if [ "$PARSER_RC" -ne 0 ]; then
  printf '%s\n' '{"decision":"block","message":"BLOCKED: the issue-contract-ledger parser failed, so this command could not be checked; no issue was mutated."}'
  exit 0
fi

[ -n "$PY_OUT" ] || exit 0
[ "$(printf '%s' "$PY_OUT" | jq -r '.block // false')" = true ] || exit 0
TARGET=$(printf '%s' "$PY_OUT" | jq -r '.target // "unknown"')
ERRORS=$(printf '%s' "$PY_OUT" | jq -r '.errors | join(",")')
if printf '%s' "$ERRORS" | grep -q 'internal_control_plane_issue_forbidden'; then
  # Rajiv directive 2026-08-12: a blocked internal control-plane issue must
  # not be silently dropped. Preserve the draft and create a high hourly
  # control_plane_defect obligation (pm_stop_actionable=1) so the PM Stop
  # validator blocks until PM posts the report to the CTO thread and resolves
  # the obligation. Fail open: a ledger/artifact write failure never weakens
  # the block.
  CP_ISSUE_ARTIFACTS="${BLOCKED_CP_ISSUE_ARTIFACTS:-$HOME/.claude/control-plane-artifacts/blocked-issues}"
  PM_NUDGE_OPS="${PM_OPS:-$HOME/.claude/scripts/pm-ops.py}"
  BODY_FILE=$(printf '%s' "$CMD" | sed -nE 's/.*--body-file[ =]+([^ ]+).*/\1/p' | head -1)
  DURABLE_BODY=""
  COPY_OK=0
  OBLIGATION_LINE=""
  if [ -n "$BODY_FILE" ] && [ -f "$BODY_FILE" ]; then
    mkdir -p "$CP_ISSUE_ARTIFACTS" 2>/dev/null || true
    SLUG=$(printf '%s' "$TARGET" | tr -cs '[:alnum:]' '-' | cut -c1-60)
    BODY_HASH=$(printf '%s' "$BODY_FILE" | shasum -a 256 2>/dev/null | awk '{print substr($1,1,16)}')
    DURABLE_BODY="$CP_ISSUE_ARTIFACTS/${BODY_HASH}-${SLUG}.md"
    if cp "$BODY_FILE" "$DURABLE_BODY" 2>/dev/null; then
      COPY_OK=1
    fi
  fi
  BODY_REF="$DURABLE_BODY"
  if [ "$COPY_OK" = 1 ] && [ -x "$PM_NUDGE_OPS" ]; then
    OBLIGATION_ID=$(TARGET_TEXT="$TARGET" BODY_REF="$BODY_REF" \
      python3 "$PM_NUDGE_OPS" obligation-upsert \
        --kind control_plane_defect \
        --severity high \
        --horizon hourly \
        --target-type blocked-issue \
        --target-id "$BODY_REF" \
        --owner pm \
        --title "Internal control-plane issue blocked — report to CTO: ${TARGET}" \
        --action "Post the gap report to the CTO in #heydonna-dev (C0ALZJHGE49) with exact failure evidence, affected PR/head, runtime control point, blocked consequence, and the preserved draft at ${BODY_REF}; then resolve with: python3 ~/.claude/scripts/pm-ops.py obligation-resolve --kind control_plane_defect --target-type blocked-issue --target-id ${BODY_REF}" \
        --blocker "${TARGET}" \
        --evidence pm_stop_actionable=1 \
        --evidence "body_file=${BODY_REF}" \
        --evidence "target=${TARGET}" \
        --dedupe-group "blocked-cp-issue:${BODY_REF}" \
        2>/dev/null || true)
    if [ -n "$OBLIGATION_ID" ] && printf '%s' "$OBLIGATION_ID" | grep -qE '^[0-9]+$'; then
      OBLIGATION_LINE=" High-priority PM obligation ${OBLIGATION_ID} created (pm_stop_actionable=1); resolve it only after posting the CTO report."
    fi
  fi
  if [ "$COPY_OK" = 1 ]; then
    PRESERVED_LINE=" Preserved draft: ${DURABLE_BODY}."
  else
    PRESERVED_LINE=""
  fi
  cat <<EOF
{"decision":"block","message":"BLOCKED: '${TARGET}' is internal HeyDonna control-plane work (${ERRORS}) and must not be filed as a GitHub issue. Report it to the CTO in the authoritative #heydonna-dev operational thread with the exact failure evidence, affected PR/head, runtime control point, and blocked consequence.${OBLIGATION_LINE}${PRESERVED_LINE} Keep the affected PR blocked until the control-plane repair is verified. Do not use the internal-followup bypass for control-plane work."}
EOF
  exit 0
fi
if printf '%s' "$ERRORS" | grep -q 'issue_number_must_be_literal'; then
  cat <<EOF
{"decision":"block","message":"BLOCKED: the issue target '${TARGET}' is not a literal number, so this hook cannot verify it against the live issue before the command runs. Re-run exactly one gh issue edit with the literal issue number (for example 7945). Shell variables, command substitution, and other computed forms cannot be verified pre-execution. No issue was mutated."}
EOF
  exit 0
fi
cat <<EOF
{"decision":"block","message":"BLOCKED: HeyDonna issue '${TARGET}' requires a valid Issue Contract Ledger (${ERRORS}). Start from the canonical template: ${TEMPLATE}. In a separate Write/Edit step, copy and fill every TBD into a named body file; the completed file must already exist before the Bash call because this hook runs before Bash. Then validate it with: python3 /Users/rajiv/.claude/scripts/validate-issue-contract-ledger.py --body-file <completed-body-file> --json. Finally run exactly one gh issue create/edit mutation using --body-file <completed-body-file>. Do not create the body file with a heredoc in the same Bash call."}
EOF
