#!/usr/bin/env bash
# Block ad-hoc GitHub CI starts/reruns unless they go through the PM wrappers.

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

blocked_reason_from_label() {
  local label="$1"
  case "$label" in
    pm-blocked:ci) printf 'ci' ;;
    pm-blocked:capture) printf 'capture' ;;
    pm-blocked:codex) printf 'codex' ;;
    pm-blocked:rebase) printf 'rebase' ;;
    pm-blocked:dependency) printf 'dependency' ;;
    pm-blocked:product) printf 'product' ;;
    pm-blocked:infra) printf 'infra' ;;
    pm-blocked:pm-gate) printf 'pm-gate' ;;
    *) printf '' ;;
  esac
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
      BLOCK_HINT="Use the authoritative operator workflow for the exact-head slot-ready or pm-review-done action after current-head review PASS."
    else
      BLOCK_HINT="Use the authoritative operator workflow for the exact-head slot-ready or pm-review-done action."
    fi
  elif printf '%s' "$CMD" | grep -qE "${GH_CMD_RE}api([[:space:]]|$)"; then
    BLOCK_REASON="manual qa-passed-awaiting-ci API mutation"
    BLOCK_HINT="Use the authoritative operator workflow for the exact-head slot-ready or pm-review-done action; do not mutate pm-state labels through gh api."
  fi
fi

if printf '%s' "$CMD" | grep -qE '(^|[;&|[:space:]])(/Users/rajiv/\.claude/scripts/)?pm-state-replace\.sh[[:space:]]+[0-9]+[[:space:]]+qa-passed-awaiting-ci([[:space:]]|$)'; then
  BLOCK_REASON="raw qa-passed-awaiting-ci state replacement"
  PR="$(printf '%s' "$CMD" | sed -nE 's/.*pm-state-replace\.sh[[:space:]]+([0-9]+)[[:space:]]+qa-passed-awaiting-ci.*/\1/p' | head -1)"
  if [ -n "${PR:-}" ]; then
    BLOCK_HINT="Use the authoritative operator workflow for the exact-head slot-ready or pm-review-done action after current-head review PASS."
  fi
fi

# pm-blocked:* manual label-edit block REMOVED per Rajiv directive 2026-09-04
# (Slack thread 1788527318.098799): manual `gh pr edit --add-label pm-blocked:*`
# edits are allowed again. The CI-rerun / workflow-dispatch / qa-passed-awaiting-ci
# guards below are intentionally retained.

[ -n "$BLOCK_REASON" ] || exit 0

if [[ "$BLOCK_REASON" == manual\ pm-blocked:* ]]; then
  cat >&2 <<'ERR'
BLOCKED: manual pm-blocked:* label edits are no longer allowed.

Use the authoritative operator workflow so the exact-head safety checks remain
consistent.
ERR
  printf 'Blocked command class: %s\n' "$BLOCK_REASON" >&2
  [ -n "$BLOCK_HINT" ] && printf '%s\n' "$BLOCK_HINT" >&2
  exit 2
fi

cat >&2 <<'ERR'
BLOCKED: raw GitHub CI/state-label command is no longer allowed.

CI is a gate, not the dev-slot test loop. Use the PM-owned wrappers so the
current PR head and current-head local proof are checked before GitHub runners
are spent.

First CI start / Phase A label trigger:

  /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/ci/request-label-gated-ci.sh --pr <PR>

Same-head rerun after failed/cancelled/timed-out CI/E2E:

  /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/ci/rerun-after-local-proof.sh --pr <PR> --run <RUN_ID>

Remote-only capture handling remains in the existing budgeted capture workflow;
raw capture dispatch stays blocked.

Rollback-window local capture handling when REMOTE_CAPTURE_ONLY=0:

  /Users/rajiv/.claude/scripts/capture-local-proof.sh --pr <PR> --head <HEAD> --slot <N> --checkout /Users/rajiv/Downloads/projects/heydonna-app-300<N> [--failed-run <RUN_ID>]
  /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/ci/request-label-gated-ci.sh --pr <PR>

The rerun wrapper requires /tmp/ci-local-preflight-proof-<PR>-<head>.ok or
/tmp/affected-test-proof-<PR>-<head>.ok. For pre-existing/flaky/no-local-equivalent
proofs, the proof must cite a follow-up issue before retrying.
Local capture proof is the fixture refresh. It must merge origin/main first,
populate all touched cache-miss fixtures, and then start normal label-gated CI.
Remote capture is not part of the normal path. Use request-local-gated-capture
only with --force-remote-after-local when Rajiv/CTO explicitly asks for remote
capture, or when current-head local capture failed only because of proven
slot-local provider infra:

  /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/ci/request-local-gated-capture.sh --pr <PR> --proof /tmp/capture-local-proof-<PR>-<HEAD>.fail --force-remote-after-local

The .fail proof must name the current head and include slot_infra_status: fail
plus a whitelisted slot_infra_reason such as slot_vertex_connectivity.
ERR
printf 'Blocked command class: %s\n' "$BLOCK_REASON" >&2
[ -n "$BLOCK_HINT" ] && printf '%s\n' "$BLOCK_HINT" >&2
exit 2
