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

MATCHER="${CI_RERUN_GUARD_MATCHER:-/Users/rajiv/.claude/scripts/ci/ci-rerun-guard-matcher.py}"
MATCHER_ERR="$(mktemp -t ci-rerun-guard.XXXXXX 2>/dev/null || echo "/tmp/ci-rerun-guard.$$.err")"
BLOCK_REASON=""
MATCHER_RC=0
if [ ! -f "$MATCHER" ] || [ ! -r "$MATCHER" ] || [ ! -x "$MATCHER" ]; then
  BLOCK_REASON="ci_rerun_guard_matcher_unavailable"
  MATCHER_RC=127
else
  set +e
  BLOCK_REASON="$(HOOK_CMD="$CMD" python3 "$MATCHER" 2>"$MATCHER_ERR")"
  MATCHER_RC=$?
  set -e
  if [ "$MATCHER_RC" -ne 0 ]; then
    BLOCK_REASON="ci_rerun_guard_matcher_failed"
  fi
fi


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

if [ "$BLOCK_REASON" = "ci_rerun_guard_matcher_unavailable" ] || [ "$BLOCK_REASON" = "ci_rerun_guard_matcher_failed" ]; then
  cat >&2 <<'ERR'
BLOCKED: the CI-rerun guard matcher is unavailable or failed, so this command could not be checked.

The guard fails closed: an absent, unreadable, non-executable, or crashing
matcher cannot be treated as allowed. Restore the canonical matcher
(/Users/rajiv/.claude/scripts/ci/ci-rerun-guard-matcher.py) before retrying.
ERR
  printf 'Blocked command class: %s\n' "$BLOCK_REASON" >&2
  exit 2
fi
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
