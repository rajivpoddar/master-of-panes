#!/usr/bin/env bash
# PreToolUse Bash hook: label-gated CI/E2E is CTO-owned.
#
# Rajiv directive Ev0C14NF0VED (2026-09-11): PM is blocked only from
# label-gated CI. Ordinary PR/issue label additions, removals, and replacements
# (including pm-blocked:*, merge-ready, status:*, priority:*, and ownership
# labels) are allowed. This hook blocks only the real CI-trigger boundary — the
# pm-state:qa-passed-awaiting-ci label rename/dispatch — and the raw GitHub CI
# dispatch/rerun forms, and it routes PM to request the CTO.

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

PROJECT_CI_DIR="/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/ci"
GH_CMD_RE='([^[:alnum:]_./-]|^)(rtk[[:space:]]+)?((/opt/homebrew/bin/|/usr/local/bin/|/usr/bin/)?gh)[[:space:]]+'
# The approved wrappers perform the live PR-head/proof checks before invoking gh.
if printf '%s' "$CMD" | grep -qE "(^|[;&|[:space:]])(bash[[:space:]]+)?(${PROJECT_CI_DIR}/|(\\./)?\\.claude/scripts/ci/)?(request-label-gated-ci|rerun-after-local-proof|rerun-main-after-local-proof|request-local-gated-capture|request-budgeted-remote-capture)\\.sh([[:space:]]|$)"; then
  exit 0
fi

BLOCK_REASON=""
BLOCK_HINT=""

extract_pr() {
  printf '%s' "$CMD" | sed -nE 's/.*gh[[:space:]]+pr[[:space:]]+edit[[:space:]]+#?([0-9]+).*/\1/p' | head -1
}

if printf '%s' "$CMD" | grep -qE "${GH_CMD_RE}run[[:space:]]+rerun([[:space:]]|$)"; then
  BLOCK_REASON="naked gh run rerun"
fi

if printf '%s' "$CMD" | grep -qE "${GH_CMD_RE}workflow[[:space:]]+run([[:space:]]|$)" &&
   printf '%s' "$CMD" | grep -qE '(^|[/[:space:]'\''"])(ci\.yml|e2e\.yml|CI|E2E Smoke Tests)([[:space:]'\''"]|$)'; then
  BLOCK_REASON="raw CI/E2E workflow dispatch"
fi

if printf '%s' "$CMD" | grep -qE "${GH_CMD_RE}workflow[[:space:]]+run([[:space:]]|$)" &&
   printf '%s' "$CMD" | grep -qE '(^|[/[:space:]'\''"])(e2e-llm-proxy-capture\.yml|E2E LLM Proxy Capture \(manual\))([[:space:]'\''"]|$)'; then
  BLOCK_REASON="raw manual capture workflow dispatch"
fi

if printf '%s' "$CMD" | grep -q 'pm-state:qa-passed-awaiting-ci'; then
  if printf '%s' "$CMD" | grep -qE "${GH_CMD_RE}(pr|issue)[[:space:]]+edit([[:space:]]|$).*--add-label"; then
    BLOCK_REASON="manual qa-passed-awaiting-ci label add"
    PR="$(extract_pr)"
    if [ -n "${PR:-}" ]; then
      BLOCK_HINT="Label-gated CI/E2E is CTO-owned. Request the CTO (Abhijit) to arm exact-head label-gated CI for PR #${PR}; the CTO runs request-label-gated-ci.sh --pr ${PR}."
    else
      BLOCK_HINT="Label-gated CI/E2E is CTO-owned. Request the CTO (Abhijit) to arm it; the CTO runs request-label-gated-ci.sh --pr <PR>."
    fi
  elif printf '%s' "$CMD" | grep -qE "${GH_CMD_RE}api([[:space:]]|$)"; then
    BLOCK_REASON="manual qa-passed-awaiting-ci API mutation"
    BLOCK_HINT="Label-gated CI/E2E is CTO-owned; do not arm it through gh api. Request the CTO (Abhijit) to arm exact-head label-gated CI."
  fi
fi

if printf '%s' "$CMD" | grep -qE '(^|[;&|[:space:]])(/Users/rajiv/\.claude/scripts/)?pm-state-replace\.sh[[:space:]]+[0-9]+[[:space:]]+qa-passed-awaiting-ci([[:space:]]|$)'; then
  BLOCK_REASON="raw qa-passed-awaiting-ci state replacement"
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
