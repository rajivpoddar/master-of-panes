#!/bin/bash
#
# UserPromptSubmit hook (PM project): skill-hint router only.
#
# All context surfaces (pm-todo working-memory dump, pending escalations,
# material-event Kanban trigger, PM readiness contract reminder, PM ops sync
# status) were removed 2026-08-15. This hook now emits ONLY skill hints:
#   - production alerts            → Skill(alert-processing)
#   - feedback Diag/Diagnostics    → Skill(alert-processing) Phase 0b subprotocol
#   - Codex bot inline review      → Skill(codex-comment-processing)
#   - CI/E2E terminal events       → Skill(ci-failure-investigation / ci-success-reconciliation)
#   - E2E Capture terminal alerts  → Skill(capture-alert-processing)
#   - PMF survey report            → Skill(survey-report-prompt-miner)
#   - customer reports/recurrence  → Skill(customer-artifact-investigator)
#   - slot→PM wait/assignment nudge → Skill(pm-nudge-processing)
#
# The pm-todo-debt PostToolUse handler in user-global settings is UNTOUCHED.
#
# Active per Rajiv directive 2026-05-08 06:46-07:08 IST thread `1778202996.384899`.

# UserPromptSubmit hooks are advisory context surfaces. They must fail open:
# a closed stdout pipe, missing optional file, or stale helper should never make
# Claude report a hook error or interrupt the PM turn.
set +e
trap '' PIPE
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

LOG="${LOG:-/tmp/hook-debug-pm-context-injector.log}"
exec 2>>"$LOG"

PAYLOAD=$(cat)

# ---------------------------------------------------------------------------
# Parse the prompt JSON once
# ---------------------------------------------------------------------------
PROMPT=$(printf '%s' "$PAYLOAD" | python3 -c '
import json, sys
try:
    data = json.loads(sys.stdin.read())
    p = data.get("prompt") or data.get("message", {}).get("content") or ""
    if isinstance(p, list):
        p = " ".join(c.get("text", "") for c in p if isinstance(c, dict))
    print(p)
except Exception:
    pass
' 2>/dev/null || echo "")

LOWER=$(printf '%s' "$PROMPT" | tr '[:upper:]' '[:lower:]')

CWD=$(printf '%s' "$PAYLOAD" | python3 -c '
import json, sys
try:
    data = json.loads(sys.stdin.read())
    print(data.get("cwd", ""))
except Exception:
    pass
' 2>/dev/null || echo "")

# ---------------------------------------------------------------------------
# Slot-aware gate (mirrors prior hooks)
# ---------------------------------------------------------------------------
# Only run when the cwd looks like the PM project. Slots have their own
# checkouts at heydonna-app-300N and don't own the PM pm-todo.md / debt counter.
case "$CWD" in
  */heydonna-app-300[0-9])
    exit 0
    ;;
  */heydonna-app|"")
    # PM (or unset cwd, e.g. some hook callers) — proceed
    ;;
  *)
    # Outside heydonna context — exit silent
    exit 0
    ;;
esac

# Pane-aware fallback when TMUX is set (preserves todo-staleness pane gate)
PANE_INDEX=""
if [ -n "$TMUX_PANE" ]; then
  PANE_INDEX=$(tmux display-message -t "$TMUX_PANE" -p '#{pane_index}' 2>/dev/null || echo "")
fi
if [ -n "$PANE_INDEX" ] && [ "$PANE_INDEX" != "0" ]; then
  exit 0
fi

# Shape 4 EARLY-EXIT: slot->PM nudge skill hint (Rajiv directive 2026-08-12)
# ---------------------------------------------------------------------------
# Slot->PM nudges ("slot N (Name): ...") MUST surface the pm-nudge-processing
# hint immediately. Detect nudges here and exit after emitting the hint.
MOP_NUDGE_FIRST_LINE=$(printf '%s' "$PROMPT" | python3 -c '
import sys
lines = sys.stdin.read().splitlines()
if lines and lines[0].lower().startswith("# mop "):
    print(lines[1] if len(lines) > 1 else "")
else:
    print(lines[0] if lines else "")
' 2>/dev/null || true)
if printf '%s' "$MOP_NUDGE_FIRST_LINE" | grep -qE '^(ESCALATION:[[:space:]]*)?((slot[[:space:]]+[0-9]+[[:space:]]+\([^)]*\):[[:space:]]*)|(\[[A-Z_]+[[:space:]]*\|[^]]*\][[:space:]]*))?NUDGE:[[:space:]]*(\[[A-Z_]+[[:space:]]*\|[^]]*\][[:space:]]*)?'; then
  MOP_NUDGE_SLOT=$(printf '%s' "$MOP_NUDGE_FIRST_LINE" | python3 -c '
import re, sys
t = sys.stdin.read()
m = re.search(r"\bslot\s+([0-9]+)", t, re.IGNORECASE)
print(m.group(1) if m else "")
' 2>/dev/null || true)
  if [ -z "$MOP_NUDGE_SLOT" ]; then
    exit 0
  fi
  TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  LOG_DIR="${HOME}/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/skill-hint-log"
  mkdir -p "$LOG_DIR" 2>/dev/null || true
  SNIPPET=$(printf '%s' "$MOP_NUDGE_FIRST_LINE" | head -c 200 | tr '\n' ' ')
  printf '%s\t%s\t%s\t%s\n' "$TS" "MOP_SLOT_NOTIFICATION" "slot-nudge:slot=${MOP_NUDGE_SLOT}" "$SNIPPET" \
    >> "$LOG_DIR/$(date -u +%Y-%m-%d).tsv" 2>/dev/null || true
  cat <<EOF

[MoP_SLOT_NOTIFICATION] MoP slot-nudge for slot ${MOP_NUDGE_SLOT} — invoke Skill(pm-nudge-processing) with arg ${MOP_NUDGE_SLOT}.
EOF
  exit 0
fi
PM_OPS="${PM_OPS:-/Users/rajiv/.claude/scripts/pm-ops.py}"
GH_CLI="${GH_CLI:-gh}"

# CI terminal obligations may be consumed after the alert has aged.  Bind
# their evidence to the exact head carried by the original event packet; a
# missing or conflicting head remains diagnostic-only and can never become
# valid current-head activity.
CI_EVENT_HEAD=$(printf '%s' "$PROMPT" | python3 -c '
import re, sys
t = sys.stdin.read()
values = []
for match in re.finditer(r"\b(?:head|head_sha|headRefOid|current_head|current_head_sha)\s*[\"]?\s*[:=]\s*[\"]?([0-9a-f]{40})\b", t, re.I):
    values.append(match.group(1).lower())
print(values[0] if values and len(set(values)) == 1 else "")
' 2>/dev/null || true)
if [[ "$CI_EVENT_HEAD" =~ ^[0-9a-f]{40}$ ]]; then
  CI_HEAD_EVIDENCE=(--evidence "head_sha=$CI_EVENT_HEAD")
else
  CI_HEAD_EVIDENCE=(--evidence "head_status=missing_or_conflicting")
fi

record_p0_escalation_obligation() {
  [ -x "$PM_OPS" ] || return 0
  # p0-escalation-admission: reject non-Slack envelopes
  case "$PROMPT" in
    '<task-notification>'*|'<retrieval_status>'*)
      printf '%s
' "P0_ESCALATION_ADMISSION_REJECTED reason=non_slack_envelope" >>"$LOG"
      return 0
      ;;
  esac

  local p0_pattern='(^|[^a-z0-9])(p0|sev0)([^a-z0-9]|$)|severity[[:space:]:=-]*p0|priority[[:space:]:=-]*p0|transcription_both_providers_failed|both providers failed|whisperx[^[:cntrl:]]*assemblyai[^[:cntrl:]]*failed|assemblyai[^[:cntrl:]]*whisperx[^[:cntrl:]]*failed|fallback-dispatch[^[:cntrl:]]*401|stuck job|auto-failed|stuck transcript'
  if ! printf '%s' "$LOWER" | grep -qE "$p0_pattern"; then
    return 0
  fi

  local channel thread target target_label next_review excerpt
  channel=$(printf '%s' "$PROMPT" | sed -nE 's/^# slack-channel ([A-Z0-9]+).*/\1/p' | head -1)
  thread=$(printf '%s' "$PROMPT" | sed -nE 's/.* in thread ([0-9]+\.[0-9]+).*/\1/p' | head -1)
  # p0-escalation-admission: require canonical Slack coordinates
  if [ -z "$channel" ] || [ -z "$thread" ]; then
    printf '%s
' "P0_ESCALATION_ADMISSION_REJECTED reason=missing_slack_coordinates channel=${channel:-unknown} thread_ts=${thread:-unknown}" >>"$LOG"
    return 0
  fi
  target="$thread"
  target_label="$thread"
  next_review=$(python3 - <<'PY' 2>/dev/null
from datetime import datetime, timedelta, timezone
print((datetime.now(timezone.utc) + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"))
PY
)
  excerpt=$(printf '%s' "$PROMPT" | tr '\n' ' ' | cut -c1-220)

  python3 "$PM_OPS" obligation-upsert \
    --kind p0_escalation \
    --severity critical \
    --target-type alert-thread \
    --target-id "$target" \
    --owner pm \
    --horizon hourly \
    --next-review-at "$next_review" \
    --dedupe-group "p0-escalation:${channel:-unknown}:${target}" \
    --title "P0 escalation call due for alert thread ${target_label}" \
    --action "Invoke Skill(alert-processing); check alert thread ${target_label} for Rajiv reply or terminal recovery proof. If neither exists after 30m, run scripts/escalation-call.sh, then reply with Twilio call SID/status, owner, and next action." \
    --evidence "source=pm-context-injector" \
    --evidence "channel=${channel:-unknown}" \
    --evidence "thread_ts=${thread:-unknown}" \
    --evidence "prompt_excerpt=${excerpt}" \
    >/dev/null 2>>"$LOG" || true
}

record_survey_prompt_mining_obligation() {
  [ -x "$PM_OPS" ] || return 0

  local channel thread raw_permalink_ts target target_label next_review excerpt secs micros
  channel=$(printf '%s' "$PROMPT" | sed -nE 's/^# slack-channel ([A-Z0-9]+).*/\1/p' | head -1)
  thread=$(printf '%s' "$PROMPT" | sed -nE 's/.*\bin thread ([0-9]+\.[0-9]+).*/\1/p' | head -1)
  if [ -z "$thread" ] && [ -n "${PMF_SURVEY_ALERT_THREAD_TS:-}" ]; then
    thread="$PMF_SURVEY_ALERT_THREAD_TS"
  fi
  if [ -z "$thread" ]; then
    raw_permalink_ts=$(printf '%s' "$PROMPT" | grep -oE '/p[0-9]{16}' | head -1 | sed 's#^/p##')
    if [ -n "$raw_permalink_ts" ]; then
      secs="${raw_permalink_ts:0:10}"
      micros="${raw_permalink_ts:10:6}"
      thread="${secs}.${micros}"
    fi
  fi

  if [ -n "$thread" ]; then
    target="$thread"
    target_label="$thread"
  else
    target="prompt-$(printf '%s' "$PROMPT" | shasum -a 256 2>/dev/null | awk '{print substr($1,1,16)}')"
    target_label="$target"
  fi

  next_review=$(python3 - <<'PY' 2>/dev/null
from datetime import datetime, timedelta, timezone
print((datetime.now(timezone.utc) + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"))
PY
)
  excerpt=$(printf '%s' "$PROMPT" | tr '\n' ' ' | cut -c1-220)

  python3 "$PM_OPS" obligation-upsert \
    --kind survey_prompt_mining \
    --severity high \
    --target-type alert-thread \
    --target-id "$target" \
    --owner pm \
    --horizon hourly \
    --next-review-at "$next_review" \
    --dedupe-group "survey-prompt-mining:${channel:-unknown}:${target}" \
    --title "PMF survey prompt-mining closure required for alert thread ${target_label}" \
    --action "Invoke Skill(survey-report-prompt-miner); immediately reply in the alert thread with status:, owner/assignee:, and next action:, then close the same thread when the agent returns PACKET_READY, NEEDS_IDENTIFIER, or NO_PROMPT_ACTION." \
    --evidence "source=pm-context-injector" \
    --evidence "channel=${channel:-unknown}" \
    --evidence "thread_ts=${thread:-unknown}" \
    --evidence "prompt_excerpt=${excerpt}" \
    >/dev/null 2>>"$LOG" || true
}

record_customer_recurrence_obligation() {
  [ -x "$PM_OPS" ] || return 0

  local channel thread next_review excerpt
  channel=$(printf '%s' "$PROMPT" | sed -nE 's/^# slack-channel ([A-Z0-9]+).*/\1/p' | head -1)
  thread=$(printf '%s' "$PROMPT" | grep -oE 'in thread [0-9]+\.[0-9]+' | head -1 | grep -oE '[0-9]+\.[0-9]+')
  # Fail closed: the obligation is keyed by channel/thread/message; without
  # canonical Slack coordinates no obligation is written (the holding reply is
  # still emitted by the caller; the next UserPromptSubmit retries idempotently).
  if [ -z "$channel" ] || [ -z "$thread" ]; then
    printf '%s
' "CUSTOMER_RECURRENCE_OBLIGATION_SKIPPED reason=missing_slack_coordinates channel=${channel:-unknown} thread_ts=${thread:-unknown}" >>"$LOG"
    return 0
  fi

  next_review=$(python3 - <<'PY' 2>/dev/null
from datetime import datetime, timedelta, timezone
print((datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
PY
)
  excerpt=$(printf '%s' "$PROMPT" | tr '\n' ' ' | cut -c1-220)

  # Idempotent: obligation-upsert matches the open (kind, target_type,
  # target_id) row and updates it; repeated delivery never duplicates.
  python3 "$PM_OPS" obligation-upsert \
    --kind customer_post_fix_recurrence \
    --severity urgent \
    --target-type alert-thread \
    --target-id "$thread" \
    --owner pm \
    --horizon hourly \
    --next-review-at "$next_review" \
    --dedupe-group "customer-post-fix-recurrence:${channel}:${thread}" \
    --title "Post-fix recurrence reported by customer in thread ${thread}" \
    --action "Holding reply sent first; launch customer-artifact-investigator read-only with channel/thread/message/customer/artifact context; verify deployment-bound telemetry before any fix-holding claim; close on investigator report." \
    --evidence "source=pm-context-injector" \
    --evidence "channel=${channel}" \
    --evidence "thread_ts=${thread}" \
    --evidence "prompt_excerpt=${excerpt}" \
    >/dev/null 2>>"$LOG" || true
}
# ---------------------------------------------------------------------------
# Section 3: Skill-hint router (one reminder max)
# ---------------------------------------------------------------------------
if [ -z "$PROMPT" ]; then
  exit 0
fi

PROJECT_ID_PATTERN='(^|[^a-z0-9])j[a-z0-9]{15,}'
# Internal-author classifier for the Trigger D guard (never trigger on an
# internal PM/slot/CTO post). Extended 2026-08-11 (CUSTOMER_POST_FIX_RECURRENCE
# repair): production slack-bridge relays on #heydonna-dev carry the CTO as
# "Abhijit CTO" and the alerts bot as "HeyDonna Alerts"; both are internal
# machinery surfaces and must never fire the customer-recurrence marker.
# Slot posts relay through the MoP bot identity. Customer-collision-prone
# names (e.g. Rohini/Ashwini/Chitra) are intentionally NOT added.
INTERNAL_NAME_REGEX='Rajiv|Dhruva|HeyDonna PM|HeyDonna Alerts|Codex|claude|Abhijit|MoP'
# Bounded post-fix recurrence/error phrases from an external customer author
# (CTO directive 2026-08-11, thread 1786425139.488049 — CUSTOMER_POST_FIX_RECURRENCE).
# Word-boundary anchored like PROJECT_ID_PATTERN: "persisted", "still happening",
# "happened again", "keeps showing", "came back", "not fixed".
RECURRENCE_PATTERN='(^|[^a-z0-9])(happened again|still happening|persisted|keeps showing|came back|not fixed)([^a-z0-9]|$)'

# customer-report-detector.sh). Slack message header format:
#   # slack-channel <CHANNEL> in thread <TS> | <Author> | <time>
LAST_AUTHOR=$(printf '%s' "$PROMPT" \
  | grep -E '^# slack-channel ' 2>/dev/null \
  | tail -1 \
  | awk -F '\\|' '{ gsub(/^[ \t]+|[ \t]+$/, "", $2); print $2 }')

INTERNAL_AUTHOR=""
if [ -n "$LAST_AUTHOR" ] && printf '%s' "$LAST_AUTHOR" | grep -qiE "$INTERNAL_NAME_REGEX"; then
  INTERNAL_AUTHOR="yes"
fi

REMINDER=""
REASON=""

PR_MERGED_NUM=""

# ---- Trigger A: Codex bot inline review ----
# Codex inline reviews land as PR review comments (`bot:codex` author).
# Patterns: bot:codex, "Codex Review", "Codex Inline", "Codex P0", "Codex P1",
# discussion_r<10+ digits> permalink (GitHub inline comment format).
if [ -z "$REMINDER" ]; then
  if printf '%s' "$LOWER" | grep -qE 'bot:codex|codex review|codex inline|codex p0|codex p1' \
     || printf '%s' "$PROMPT" | grep -qE 'discussion_r[0-9]{10,}'; then
    REMINDER="CODEX_BOT_COMMENT_DETECTED"
    REASON="Codex bot inline review comment pattern"
  fi
fi

# ---- Trigger B: feedback Diag/Diagnostics submission (Phase 0b) ----
# B0AHQ6BK7F1 = HeyDonna Alerts feedback bot in #heydonna-feedback. When the
# alert title contains "Diag" or "Diagnostics" it's a customer-submitted
# perfSummary — alert-processing skill Phase 0b subprotocol.
if [ -z "$REMINDER" ]; then
  if printf '%s' "$PROMPT" | grep -qE 'B0AHQ6BK7F1' \
     && printf '%s' "$LOWER" | grep -qE '(^|[^a-z])diag(nostics?)?([^a-z]|$)'; then
    REMINDER="ALERT_DETECTED_DIAG"
    REASON="Diag/Diagnostics feedback submission (alert-processing Phase 0b)"
  fi
fi

# ---- Trigger Bc: CI/E2E failure — routes to ci-failure-investigation skill ----
# GitHub bot shapes (post-2026-08 drift): `:x: *CI failed*`,
# `:x: *E2E Smoke Tests failed*`, and `:x: *CI + E2E* failed`. E2E Large File
# Correctness (`E2E Large File Correctness failure`) is intentionally excluded:
# LFC is an optional lane and requires no investigation.
# More specific than the general production-alert trigger (Trigger C). CI/E2E
# failures need the ci-failure-investigation SOP (Modal-logs-cache pull per
# `feedback_e2e_fail_investigation_modal_logs_cache_mandatory`, classify,
# rework-dispatch, then Step 7 verdict reply to alert thread per Rajiv
# directive 2026-05-11 22:06 IST). Fires BEFORE Trigger C so CI patterns take
# precedence. Parses run_id + alert_thread_ts + pr for the skill ARGUMENTS.
CI_RUN_ID=""
CI_ALERT_THREAD_TS=""
CI_PR=""
if [ -z "$REMINDER" ]; then
  if printf '%s' "$LOWER" | grep -qE 'ci \+ e2e\*?[[:space:]]+failed|ci failed|e2e smoke tests failed|ci: failure|e2e: failure|e2e: cancelled.*timed_out|workflow.*failed.*pull[/_-]request|gh run view --log-failed'; then
    REMINDER="CI_FAILURE_DETECTED"
    REASON="CI/E2E failure alert — ci-failure-investigation SOP gate"

    # Parse run_id from the alert URL: actions/runs/<NUMERIC>
    CI_RUN_ID=$(printf '%s' "$PROMPT" \
      | grep -oE 'actions/runs/[0-9]+' \
      | head -1 \
      | grep -oE '[0-9]+')

    # Parse alert_thread_ts from the slack-bridge header:
    #   # slack-channel <CH> in thread <TS> | <Author> | <time>
    CI_ALERT_THREAD_TS=$(printf '%s' "$PROMPT" \
      | grep -oE 'in thread [0-9]+\.[0-9]+' \
      | head -1 \
      | grep -oE '[0-9]+\.[0-9]+')

    # Parse PR from the PR URL: pull/<N>
    CI_PR=$(printf '%s' "$PROMPT" \
      | grep -oE 'pull/[0-9]+' \
      | head -1 \
      | grep -oE '[0-9]+')
  fi
fi

# ---- Trigger Bd: CI/E2E success — routes to readiness promotion ----
# Symmetric with Trigger Bc above. A green CI/E2E alert is a terminal event:
# PRs already in qa-passed-awaiting-ci must run Phase B readiness promotion;
# PRs still carrying pm-blocked:ci must clear stale CI state only after
# latest-head proof.
# This fires BEFORE generic HeyDonna Alerts routing so success alerts cannot be
# swallowed by alert-processing.
CI_SUCCESS_RUN_ID=""
CI_SUCCESS_ALERT_THREAD_TS=""
CI_SUCCESS_PR=""
if [ -z "$REMINDER" ]; then
  if printf '%s' "$LOWER" | grep -qE 'ci \+ e2e success on|ci: success|e2e: success|workflow.*(succeeded|success).*pull[/_-]request|white_check_mark.*ci \+ e2e'; then
    REMINDER="CI_SUCCESS_DETECTED"
    REASON="CI/E2E success alert — ci-success-reconciliation skill gate"

    # Parse run_id from the alert URL: actions/runs/<NUMERIC>
    CI_SUCCESS_RUN_ID=$(printf '%s' "$PROMPT" \
      | grep -oE 'actions/runs/[0-9]+' \
      | head -1 \
      | grep -oE '[0-9]+')

    # Parse alert_thread_ts from the slack-bridge header:
    #   # slack-channel <CH> in thread <TS> | <Author> | <time>
    CI_SUCCESS_ALERT_THREAD_TS=$(printf '%s' "$PROMPT" \
      | grep -oE 'in thread [0-9]+\.[0-9]+' \
      | head -1 \
      | grep -oE '[0-9]+\.[0-9]+')

    # Parse PR from the PR URL: pull/<N>
    CI_SUCCESS_PR=$(printf '%s' "$PROMPT" \
      | grep -oE 'pull/[0-9]+' \
      | head -1 \
      | grep -oE '[0-9]+')
  fi
fi

# ---- Trigger Be: E2E Capture terminal alert — route to capture-alert-processing ----
# Capture alerts are not normal CI + E2E terminal alerts and not production
# pipeline alerts. They are PM-owned terminal control-plane events. PM consumes
# them through the typed remote-capture transition, then relays the exact
# terminal result to CTO in the originating alert thread. This is not a
# numbered-slot relay and never triggers a second workflow action.
CAPTURE_RUN_ID=""
CAPTURE_ALERT_THREAD_TS=""
CAPTURE_PR=""
CAPTURE_BRANCH=""
CAPTURE_HEAD=""
CAPTURE_VERDICT=""
if [ -z "$REMINDER" ]; then
  if printf '%s' "$LOWER" | grep -qE 'e2e capture' \
     && printf '%s' "$LOWER" | grep -qE 'success|failure|failed|cancelled|canceled'; then
    REMINDER="CAPTURE_ALERT_DETECTED"
    REASON="E2E Capture terminal alert — consume typed remote-capture transition"

    CAPTURE_RUN_ID=$(printf '%s' "$PROMPT" \
      | grep -oE 'actions/runs/[0-9]+' \
      | head -1 \
      | grep -oE '[0-9]+')

    CAPTURE_ALERT_THREAD_TS=$(printf '%s' "$PROMPT" \
      | grep -oE 'in thread [0-9]+\.[0-9]+' \
      | head -1 \
      | grep -oE '[0-9]+\.[0-9]+')

    CAPTURE_PR=$(printf '%s' "$PROMPT" \
      | grep -oE 'pull/[0-9]+|PR #[0-9]+' \
      | head -1 \
      | grep -oE '[0-9]+')

    CAPTURE_HEAD=$(printf '%s' "$PROMPT" | python3 -c '
import re, sys
t = sys.stdin.read()
m = re.search(r"\bhead(?:_sha| sha|sha)?\s*[:=]?\s*`?([0-9a-f]{40})\b", t, re.I)
print(m.group(1).lower() if m else "")
' 2>/dev/null || true)

    CAPTURE_VERDICT=$(printf '%s' "$PROMPT" | python3 -c '
import re, sys
t = sys.stdin.read()
m = re.search(r"\*E2E Capture\*\s+(success|failure|failed|cancelled|canceled)\b", t, re.I)
if not m:
    m = re.search(r"\*E2E Capture\*.*?\*\s*(success|failure|failed|cancelled|canceled)\s*\*", t, re.I | re.S)
v = (m.group(1).lower() if m else "")
if v == "failed":
    v = "failure"
if v == "canceled":
    v = "cancelled"
print(v)
' 2>/dev/null || true)

    CAPTURE_BRANCH=$(printf '%s' "$PROMPT" | python3 -c '
import re, sys
t = sys.stdin.read()
m = re.search(r"\*E2E Capture\*\s+(?:success|failure|failed|cancelled|canceled)\s+on\s+`([^`]+)`", t, re.I)
if not m:
    m = re.search(r"\*Branch:\*\s*`([^`]+)`", t, re.I)
if not m:
    m = re.search(r"\*E2E Capture\*\s+(.+?)\s+[—-]\s+\*\s*(?:success|failure|failed|cancelled|canceled)\s*\*", t, re.I)
print((m.group(1).strip() if m else ""))
' 2>/dev/null || true)
  fi
fi

# ---- Trigger Bf: PMF survey report — route to survey-report-prompt-miner ----
# PMF survey alerts are posted by HeyDonna Alerts in #heydonna-alerts, so the
# generic production-alert matcher would otherwise swallow them. They are
# customer-artifact/prompt-mining work: resolve the associated file through
# Axiom, diff human-edited proceedings vs auto-processed proceedings, and
# generate proofreading / SC / formatting examples via the dedicated agent.
PMF_SURVEY_ALERT_THREAD_TS=""
if [ -z "$REMINDER" ]; then
  if printf '%s' "$LOWER" | grep -qE 'new pmf survey response|pmf survey response|post_export_heavy_edit|survey response' \
     && printf '%s' "$PROMPT" | grep -qE 'C0AEY9CEC4D|HeyDonna Alerts|B0AHQ6BK7F1'; then
    REMINDER="PMF_SURVEY_DETECTED"
    REASON="PMF survey report — survey-report-prompt-miner"

    PMF_SURVEY_ALERT_THREAD_TS=$(printf '%s' "$PROMPT" \
      | grep -oE 'in thread [0-9]+\.[0-9]+' \
      | head -1 \
      | grep -oE '[0-9]+\.[0-9]+')
  fi
fi

# ---- Trigger Bg: CI runner pool stall — route to Hetzner autoscaler repair ----
# HeyDonna Alerts emits `:rotating_light: *CI runner pool stall (job-level)*`
# when ephemeral Hetzner CI runners are absent or queued beyond the stall
# threshold. This is CI capacity/autoscaler (CP #15), not a customer pipeline
# alert; route to the PM autoscaler repair lane rather than alert-processing.
if [ -z "$REMINDER" ]; then
  if printf '%s' "$LOWER" | grep -qE 'ci runner pool stall|runner pool stall'; then
    REMINDER="RUNNER_POOL_STALL_DETECTED"
    REASON="CI runner pool stall — Hetzner autoscaler capacity (CP #15)"
  fi
fi

# ---- Trigger Bh: CTO PR sweep directives -> pr-state-sweep ----
# The CTO posts sweep/intake/merge-ready action items to PM in #heydonna-dev.
# These are the pre-dispatch reconciler inputs, not customer alerts or slot
# work; route them to the canonical pr-state-sweep skill.
if [ -z "$REMINDER" ]; then
  if printf '%s' "$LOWER" | grep -qE 'cto decisions intake|cto intake|merge[- ]ready report|five[- ]lane|pr[- ]state sweep|pr sweep'; then
    REMINDER="PR_SWEEP_DETECTED"
    REASON="CTO PR sweep / intake directive — pr-state-sweep reconciler"
  fi
fi

# ---- Trigger Bi: slot-requested PR rescue -> pm-pr-rescue ----
# Slots request review-cap/PR rescue in their `slot N (Name): ...` messages
# (rescue terminal, plan-cap rescue, 3-cycle cap, review cap). Route those to
# the PM-owned rescue lane rather than leaving them to fall through silently.
if [ -z "$REMINDER" ]; then
  if printf '%s' "$LOWER" | grep -qE '^(escalation:[[:space:]]*)?slot[[:space:]]+[0-9]+[[:space:]]+\(' \
     && printf '%s' "$LOWER" | grep -qE 'rescue|review[- ]cap|plan[- ]cap|3[- ]cycle cap'; then
    REMINDER="PR_RESCUE_REQUESTED"
    REASON="slot-requested PR/review-cap rescue — pm-pr-rescue lane"
  fi
fi

# ---- Trigger C: production alerts (auto-process / pipeline / validator) ----
# HeyDonna Alerts bot text in #heydonna-alerts (C0AEY9CEC4D, bot B0AHQ6BK7F1)
# OR phrases that name a stuck pipeline / pipeline error / auto-process /
# validator gate / format failure / processing failure / stuck transcript.
# CI/E2E failures are handled by Trigger Bc above (more specific class).
#
# Pattern broadening per Rajiv CTO directive 2026-05-26 11:47 IST thread
# `1779773350.706679` reply `1779776327.681149` (ITEM 4): include the full
# auto-process / pipeline / validator surface so customer-facing pipeline
# alerts route to alert-processing (NOT ci-failure-investigation).
if [ -z "$REMINDER" ]; then
  if printf '%s' "$PROMPT" | grep -qE 'C0AEY9CEC4D|HeyDonna Alerts' \
     || printf '%s' "$LOWER" | grep -qE 'stuck job|auto-failed|stale auto-process|processing_failure|auto[ -]?process|pipeline failure|validator_gate|format_validator|processing_failed|stuck transcript|jobid|transcriptid'; then
    REMINDER="ALERT_DETECTED"
    REASON="production alert pattern (HeyDonna Alerts / auto-process / pipeline / validator-gate / stuck-transcript / processing-failed)"
  fi
fi

# ---- Trigger D: customer report (existing customer-report-detector logic) ----
if [ -z "$REMINDER" ] && [ -z "$INTERNAL_AUTHOR" ]; then
  CR_TRIG=""
  CR_REASON=""

  if printf '%s' "$PROMPT" | grep -qE 'C0A56RX6FNW'; then
    CR_TRIG="yes"
    CR_REASON="heydonna-feedback channel ID (C0A56RX6FNW) detected"
  fi

  if [ -z "$CR_TRIG" ] && printf '%s' "$PROMPT" | grep -qE '^# slack-channel|^> .*slack-channel'; then
    if printf '%s' "$LOWER" | grep -qE "$PROJECT_ID_PATTERN|abilaasha|customer|user|diagnostic"; then
      CR_TRIG="yes"
      CR_REASON="quoted slack-channel block with customer-issue markers"
    fi
  fi

  if [ -z "$CR_TRIG" ] && printf '%s' "$LOWER" | grep -qE '(^|[^a-z])abilaasha([^a-z]|$)|(^|[^a-z])abi([^a-z]|$)'; then
    if printf '%s' "$LOWER" | grep -qE "$PROJECT_ID_PATTERN"; then
      CR_TRIG="yes"
      CR_REASON="Abilaasha/abi mention with project-id pattern"
    fi
  fi

  if [ -z "$CR_TRIG" ] && printf '%s' "$LOWER" | grep -qE 'customer reported|user reported|customer report|user report|reported by'; then
    if printf '%s' "$LOWER" | grep -qE "$PROJECT_ID_PATTERN"; then
      CR_TRIG="yes"
      CR_REASON="customer/user report phrase with project-id pattern"
    fi
  fi

  if [ -z "$CR_TRIG" ] && printf '%s' "$LOWER" | grep -qE '(^|[^a-z])diagnostics?([^a-z]|$)'; then
    if printf '%s' "$LOWER" | grep -qE "$PROJECT_ID_PATTERN"; then
      CR_TRIG="yes"
      CR_REASON="diagnostic upload reference with project-id pattern"
    fi
  fi

  # Post-fix recurrence recognition (CTO directive 2026-08-11, thread
  # 1786425139.488049): bounded recurrence/error phrases from a non-internal
  # Slack author are a customer-recurrence signal even without a project-id.
  # Requires the slack-channel relay header with an external author
  # (LAST_AUTHOR non-empty); ordinary prompts stay silent (fail-closed).
  if [ -z "$CR_TRIG" ] && [ -n "$LAST_AUTHOR" ]; then
    if printf '%s' "$LOWER" | grep -qE "$RECURRENCE_PATTERN"; then
      CR_TRIG="recurrence"
      CR_REASON="external-customer post-fix recurrence phrase"
    fi
  fi

  if [ -n "$CR_TRIG" ]; then
    if [ "$CR_TRIG" = "recurrence" ]; then
      REMINDER="CUSTOMER_POST_FIX_RECURRENCE"
    else
      REMINDER="CUSTOMER_REPORT_DETECTED"
    fi
    REASON="$CR_REASON"
  fi
fi

# ---- Trigger E: PR-merge notification (lowest precedence) ----
# Patterns: GitHub Slack integration "merged into main" / "Pull request #N merged"
# / "squash merged" / "squashed and merged"; PM's own `gh pr merge` success
# stdout (https://github.com/.*/pull/N); ":tada:" emoji from the merge bot.
# Extract PR number for the reminder body.
if [ -z "$REMINDER" ]; then
  PR_MERGE_TRIG=""
  PR_MERGE_REASON=""

  if printf '%s' "$LOWER" | grep -qE 'merged into main|merged to.*main|squash[- ]merged|squashed and merged'; then
    PR_MERGE_TRIG="yes"
    PR_MERGE_REASON="GitHub merge notification phrase"
  fi

  if [ -z "$PR_MERGE_TRIG" ] && printf '%s' "$PROMPT" | grep -qE 'Pull request #[0-9]+ merged'; then
    PR_MERGE_TRIG="yes"
    PR_MERGE_REASON="Pull request #N merged"
  fi

  # PM's own merge: gh pr merge success returns the PR URL. Look for a
  # github.com pull URL combined with merge wording.
  if [ -z "$PR_MERGE_TRIG" ] && printf '%s' "$PROMPT" | grep -qE 'https://github\.com/[^[:space:]]+/pull/[0-9]+'; then
    if printf '%s' "$LOWER" | grep -qE 'gh pr merge|pr merged|merged pr|merge complete|merged successfully'; then
      PR_MERGE_TRIG="yes"
      PR_MERGE_REASON="gh pr merge success output"
    fi
  fi

  if [ -z "$PR_MERGE_TRIG" ] && printf '%s' "$PROMPT" | grep -qE ':tada:' \
     && printf '%s' "$LOWER" | grep -qE 'merged|pull request #[0-9]+'; then
    PR_MERGE_TRIG="yes"
    PR_MERGE_REASON="GitHub merge bot :tada: with merge wording"
  fi

  if [ -n "$PR_MERGE_TRIG" ]; then
    # Extract PR number — try multiple patterns, take the first.
    PR_MERGED_NUM=$(printf '%s' "$PROMPT" \
      | grep -oE 'Pull request #[0-9]+|/pull/[0-9]+|PR #[0-9]+|pr #[0-9]+|#[0-9]+' \
      | head -1 \
      | grep -oE '[0-9]+')
    REMINDER="PR_MERGED_DETECTED"
    REASON="$PR_MERGE_REASON"
  fi
fi

# ---------------------------------------------------------------------------
# Emit one reminder (if any)
# ---------------------------------------------------------------------------
if [ -n "$REMINDER" ]; then
  TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  LOG_DIR="${HOME}/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/skill-hint-log"
  mkdir -p "$LOG_DIR" 2>/dev/null
  SNIPPET=$(printf '%s' "$PROMPT" | head -c 200 | tr '\n' ' ')
  printf '%s\t%s\t%s\t%s\n' "$TS" "$REMINDER" "$REASON" "$SNIPPET" \
    >> "$LOG_DIR/$(date -u +%Y-%m-%d).tsv"

  case "$REMINDER" in
    CODEX_BOT_COMMENT_DETECTED)
      cat <<EOF

[CODEX_BOT_COMMENT_DETECTED] This message references a Codex bot inline review comment.
Trigger: ${REASON}.

Invoke Skill(codex-comment-processing) with the comment URL / discussion_r ID as ARGUMENTS.
EOF
      ;;
    ALERT_DETECTED_DIAG)
      cat <<EOF

[ALERT_DETECTED] (Diag/Diagnostics submission)

Trigger: ${REASON}.

Invoke Skill(alert-processing) (Phase 0b Diag Mode subprotocol).
EOF
      ;;
    RUNNER_POOL_STALL_DETECTED)
      cat <<EOF

[RUNNER_POOL_STALL_DETECTED] CI runner pool stall detected.

Trigger: ${REASON}.

Invoke Skill(pm-autoscaler-repair); the full alert payload is in this prompt.
EOF
      ;;
    PR_SWEEP_DETECTED)
      cat <<EOF

[PR_SWEEP_DETECTED] CTO PR sweep / intake directive received.

Trigger: ${REASON}.

Invoke Skill(pr-state-sweep) with the full CTO directive as context.
EOF
      ;;
    PR_RESCUE_REQUESTED)
      cat <<EOF

[PR_RESCUE_REQUESTED] A slot requested a PR/review-cap rescue.

Trigger: ${REASON}.

Invoke Skill(pm-pr-rescue) with the PR/issue number and exact head as ARGUMENTS; the rescue is off-slot and must not hold a numbered slot.
EOF
      ;;
    ALERT_DETECTED)
      record_p0_escalation_obligation
      cat <<EOF

[ALERT_DETECTED] This message appears to be a production alert.

Trigger: ${REASON}.

Invoke Skill(alert-processing); all P0/SEV0 incidents require a durable p0_escalation obligation.
EOF
      ;;
	    CI_FAILURE_DETECTED)
	      RUN_LABEL="${CI_RUN_ID:-<unknown>}"
	      TS_LABEL="${CI_ALERT_THREAD_TS:-<unknown>}"
	      PR_LABEL_CI="${CI_PR:-<unknown>}"
		      # ---------------------------------------------------------------------
	      # Stop-hook supersession gate (Phase 3 of slot-claim spec — 2026-05-24)
      # ---------------------------------------------------------------------
      # Suppress CI failure reminders that are newer-state superseded. The
      # context (Slack alert) may be 30+ min old, but the PR may have moved
      # to a new head SHA AND a newer CI run is in-flight. Resurfacing the
      # old run as "blocker" yields a ghost reminder.
      #
	      # Resource-state check: a main/push terminal may be superseded only by
	      # a newer terminal from the same workflow on main/push. PR events,
	      # branch runs, skipped/cancelled shells, and stale heads are not
	      # authorizing replacements for a main failure.
	      SUPERSEDED=""
      if [ -n "${CI_PR}" ] && [ -n "${CI_RUN_ID}" ]; then
	        ORIGINAL_RUN_JSON=$(timeout 5 "$GH_CLI" run view "${CI_RUN_ID}" --json workflowName,event,headBranch,headSha,createdAt,status,conclusion 2>/dev/null)
	        if jq -e '(.workflowName|type) == "string" and (.workflowName|length) > 0 and .event == "push" and .headBranch == "main" and (.headSha|type) == "string" and (.headSha|test("^[0-9a-f]{40}$")) and (.createdAt|type) == "string"' >/dev/null 2>&1 <<<"$ORIGINAL_RUN_JSON"; then
	          WORKFLOW_NAME=$(jq -r '.workflowName' <<<"$ORIGINAL_RUN_JSON")
	          ORIGINAL_HEAD=$(jq -r '.headSha' <<<"$ORIGINAL_RUN_JSON")
	          ORIGINAL_CREATED=$(jq -r '.createdAt' <<<"$ORIGINAL_RUN_JSON")
	          WORKFLOW_RUNS=$(timeout 5 "$GH_CLI" run list --workflow "$WORKFLOW_NAME" --limit 100 --json databaseId,status,conclusion,event,headBranch,headSha,createdAt 2>/dev/null)
	          AUTHORITATIVE_REPLACEMENT=$(jq -c --arg run "${CI_RUN_ID}" --arg head "$ORIGINAL_HEAD" --arg created "$ORIGINAL_CREATED" '
            [ .[] | select(
              ((.databaseId|tostring) != $run)
              and .status == "completed"
              and (.conclusion != null)
              and (.conclusion != "skipped")
              and (.conclusion != "cancelled")
              and .event == "push"
              and .headBranch == "main"
              and (.headSha|type) == "string"
              and (.headSha|test("^[0-9a-f]{40}$"))
              and .headSha != $head
              and .createdAt > $created
            ) ] | sort_by(.createdAt) | last // empty
          ' <<<"$WORKFLOW_RUNS" 2>/dev/null)
          if [ -n "$AUTHORITATIVE_REPLACEMENT" ]; then
            SUPERSEDED="yes"
            LATEST_RUN_ID=$(jq -r '.databaseId' <<<"$AUTHORITATIVE_REPLACEMENT")
            LATEST_STATUS=$(jq -r '.status + "/" + .conclusion' <<<"$AUTHORITATIVE_REPLACEMENT")
            LATEST_HEAD=$(jq -r '.headSha' <<<"$AUTHORITATIVE_REPLACEMENT")
          fi
        fi
      fi

      if [ -n "$SUPERSEDED" ]; then
        # Log + suppress
        TS_SUPPRESS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
        SUPPRESS_LOG="${HOME}/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/skill-hint-log/$(date -u +%Y-%m-%d)-suppressed.tsv"
        mkdir -p "$(dirname "$SUPPRESS_LOG")" 2>/dev/null
        printf '%s\t%s\t%s\tlatest=%s\n' "$TS_SUPPRESS" "CI_FAILURE_SUPERSEDED" "run=${CI_RUN_ID} pr=${CI_PR}" "${LATEST_RUN_ID:-unknown}" >> "$SUPPRESS_LOG"
        cat <<EOF

[CI_FAILURE_SUPERSEDED] Stale CI failure reminder suppressed.

Original run ID: ${CI_RUN_ID} for PR #${CI_PR}.
Validated replacement run: ${LATEST_RUN_ID:-<unknown>} (${LATEST_STATUS:-<unknown>}, head ${LATEST_HEAD:-<unknown>}).

The PR has moved on. Do NOT chase this stale failure. If you want to verify the
latest CI state, run \`gh pr view ${CI_PR} --json statusCheckRollup\`.

(Phase 3 stop-hook supersession gate — slot-claim spec 2026-05-24.)
EOF
	      else
	        if [ -n "${CI_PR:-}" ]; then
	          CI_RECONCILE_SENTINEL="/tmp/pm-required-ci-reconcile-${CI_PR}.json"
	          CI_EVENT_KIND="failure" CI_EVENT_PR="$CI_PR" CI_EVENT_RUN="$RUN_LABEL" CI_EVENT_THREAD="$TS_LABEL" CI_EVENT_REASON="$REASON" CI_EVENT_SENTINEL="$CI_RECONCILE_SENTINEL" python3 - <<'PYEOF' 2>/dev/null || true
import datetime
import json
import os

now = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
path = os.environ["CI_EVENT_SENTINEL"]
payload = {
    "schema_version": 1,
    "source": "pm-context-injector",
    "status": "pending",
    "event": os.environ.get("CI_EVENT_KIND"),
    "pr": os.environ.get("CI_EVENT_PR"),
    "run_id": os.environ.get("CI_EVENT_RUN"),
    "alert_thread_ts": os.environ.get("CI_EVENT_THREAD"),
    "reason": os.environ.get("CI_EVENT_REASON"),
    "created_at": now,
    "updated_at": now,
}
tmp = f"{path}.{os.getpid()}.tmp"
with open(tmp, "w") as f:
    json.dump(payload, f)
os.replace(tmp, path)
PYEOF
		          printf '%s\tCI_TERMINAL\tpr=%s event=failure run=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$CI_PR" "$RUN_LABEL" >> /tmp/kanban-pending.flag 2>/dev/null || true
		          if [ -x "$PM_OPS" ]; then
		            python3 "$PM_OPS" record --source pm-context-injector --event ci_terminal_failure --target-type pr --target-id "$CI_PR" --pr "$CI_PR" --payload "run=$RUN_LABEL" --payload "sentinel=$CI_RECONCILE_SENTINEL" --dedupe >/dev/null 2>&1 || true
		            python3 "$PM_OPS" obligation-upsert --kind ci_reconcile --severity high --target-type pr --target-id "$CI_PR" --pr "$CI_PR" --owner pm --title "CI terminal failure reconcile required for PR #${CI_PR}" --action "Run ci-failure-investigation or reconcile live PR state, then demote/rerun/dispatch/block with proof." --evidence "run=$RUN_LABEL" --evidence "sentinel=$CI_RECONCILE_SENTINEL" "${CI_HEAD_EVIDENCE[@]}" >/dev/null 2>&1 || true
		          fi
		        fi
	        cat <<EOF

[CI_FAILURE_DETECTED] CI/E2E failure: run_id=${RUN_LABEL} alert_thread_ts=${TS_LABEL} pr=${PR_LABEL_CI}.

Trigger: ${REASON}.

Invoke Skill(ci-failure-investigation) with ARGUMENTS: run_id=${RUN_LABEL} alert_thread_ts=${TS_LABEL} pr=${PR_LABEL_CI}.
EOF
      fi
      ;;
	    CI_SUCCESS_DETECTED)
	      # CI_SUCCESS_RECONCILIATION_SKILL_V1
	      RUN_LABEL="${CI_SUCCESS_RUN_ID:-<unknown>}"
	      TS_LABEL="${CI_SUCCESS_ALERT_THREAD_TS:-<unknown>}"
	      PR_LABEL_CI="${CI_SUCCESS_PR:-<unknown>}"
	      if [ -n "${CI_SUCCESS_PR:-}" ]; then
	        CI_RECONCILE_SENTINEL="/tmp/pm-required-ci-reconcile-${CI_SUCCESS_PR}.json"
	        CI_EVENT_KIND="success" CI_EVENT_PR="$CI_SUCCESS_PR" CI_EVENT_RUN="$RUN_LABEL" CI_EVENT_THREAD="$TS_LABEL" CI_EVENT_REASON="$REASON" CI_EVENT_SENTINEL="$CI_RECONCILE_SENTINEL" python3 - <<'PYEOF' 2>/dev/null || true
import datetime
import json
import os

now = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
path = os.environ["CI_EVENT_SENTINEL"]
payload = {
    "schema_version": 1,
    "source": "pm-context-injector",
    "status": "pending",
    "event": os.environ.get("CI_EVENT_KIND"),
    "pr": os.environ.get("CI_EVENT_PR"),
    "run_id": os.environ.get("CI_EVENT_RUN"),
    "alert_thread_ts": os.environ.get("CI_EVENT_THREAD"),
    "reason": os.environ.get("CI_EVENT_REASON"),
    "created_at": now,
    "updated_at": now,
}
tmp = f"{path}.{os.getpid()}.tmp"
with open(tmp, "w") as f:
    json.dump(payload, f)
os.replace(tmp, path)
PYEOF
		        printf '%s\tCI_TERMINAL\tpr=%s event=success run=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$CI_SUCCESS_PR" "$RUN_LABEL" >> /tmp/kanban-pending.flag 2>/dev/null || true
		        if [ -x "$PM_OPS" ]; then
		          python3 "$PM_OPS" record --source pm-context-injector --event ci_terminal_success --target-type pr --target-id "$CI_SUCCESS_PR" --pr "$CI_SUCCESS_PR" --payload "run=$RUN_LABEL" --payload "sentinel=$CI_RECONCILE_SENTINEL" --dedupe >/dev/null 2>&1 || true
		          python3 "$PM_OPS" obligation-upsert --kind ci_reconcile --severity high --target-type pr --target-id "$CI_SUCCESS_PR" --pr "$CI_SUCCESS_PR" --owner pm --title "CI terminal success reconcile required for PR #${CI_SUCCESS_PR}" --action "Promote, clear stale CI blocker, or write live blocker proof based on current PR labels/checks." --evidence "run=$RUN_LABEL" --evidence "sentinel=$CI_RECONCILE_SENTINEL" "${CI_HEAD_EVIDENCE[@]}" >/dev/null 2>&1 || true
		        fi
		      fi
	      cat <<EOF

[CI_SUCCESS_DETECTED] CI/E2E success: run_id=${RUN_LABEL} alert_thread_ts=${TS_LABEL} pr=${PR_LABEL_CI}.

Trigger: ${REASON}.

The next tool call MUST be Skill(ci-success-reconciliation) with ARGUMENTS:
pr=${PR_LABEL_CI} run_id=${RUN_LABEL} alert_thread_ts=${TS_LABEL}.
This is a terminal CI event. The skill owns exact-head readiness and the typed
terminal transition; do not treat this as a generic production alert:

1. Query live PR state:
   \`gh pr view ${PR_LABEL_CI} --json labels,isDraft,mergeStateStatus,mergeable,statusCheckRollup,headRefOid\`
2. If the PR is \`pm-state:qa-passed-awaiting-ci\`, run project-local
   Skill(pm-readiness-contract) only. Do not run a second PM Claude code review
   merely to mark merge-ready; cite the existing current-head Phase A marker if
   the PR had rework. Promote only after READY_PACKET: PASS and latest-head
   checks, mergeability, Codex threads, and capture state all pass.
3. If the PR is \`pm-state:blocked-rework\` with \`pm-blocked:ci\`, prove this
   green run is on the latest head, remove the stale CI blocker, then run the
   same Phase B readiness path if no other blocker remains.
4. If state, head, draft status, review threads, or mergeability disagree,
   report \`STATE_MISMATCH\` and leave an explicit blocker instead of silently
   keeping the PR in CI limbo.
5. After any label/state mutation, write \`/tmp/kanban-pending.flag\` with a
   \`CI_SUCCESS\` / \`CI_TERMINAL\` material event.

# CI_SUCCESS_CTO_RELAY_V1
Once the exact head is green and ready, CTO/PR-merges/rescue ownership requires
CI_SUCCESS_CTO_RELAY_REQUIRED with the exact PR/head/CI/E2E tuple, mention
<@U0BNFGX2UAX> in this source thread, then hand off—not
CI_SUCCESS_SUPERSEDED. Only true head drift is superseded; real product,
capture, review-thread, or readiness blockers remain typed blockers.
EOF
	      ;;
    CAPTURE_ALERT_DETECTED)
      RUN_LABEL="${CAPTURE_RUN_ID:-<unknown>}"
      TS_LABEL="${CAPTURE_ALERT_THREAD_TS:-<unknown>}"
      PR_LABEL_CAPTURE="${CAPTURE_PR:-<unknown>}"
      BRANCH_LABEL_CAPTURE="${CAPTURE_BRANCH:-<unknown>}"
      VERDICT_LABEL_CAPTURE="${CAPTURE_VERDICT:-<unknown>}"
      CAPTURE_SENTINEL="/tmp/pm-required-capture-alert-${RUN_LABEL}.json"
      CAPTURE_EVENT_RUN="$RUN_LABEL" CAPTURE_EVENT_THREAD="$TS_LABEL" CAPTURE_EVENT_PR="$PR_LABEL_CAPTURE" CAPTURE_EVENT_BRANCH="$BRANCH_LABEL_CAPTURE" CAPTURE_EVENT_HEAD="$CAPTURE_HEAD" CAPTURE_EVENT_VERDICT="$VERDICT_LABEL_CAPTURE" CAPTURE_EVENT_REASON="$REASON" CAPTURE_EVENT_SENTINEL="$CAPTURE_SENTINEL" python3 - <<'PYEOF' 2>/dev/null || true
import datetime
import json
import os

now = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
path = os.environ["CAPTURE_EVENT_SENTINEL"]
payload = {
    "schema_version": 1,
    "source": "pm-context-injector",
    "status": "pending",
    "event": "capture_terminal",
    "run_id": os.environ.get("CAPTURE_EVENT_RUN"),
    "alert_thread_ts": os.environ.get("CAPTURE_EVENT_THREAD"),
    "pr": os.environ.get("CAPTURE_EVENT_PR"),
    "branch": os.environ.get("CAPTURE_EVENT_BRANCH"),
    "head": os.environ.get("CAPTURE_EVENT_HEAD"),
    "verdict": os.environ.get("CAPTURE_EVENT_VERDICT"),
    "reason": os.environ.get("CAPTURE_EVENT_REASON"),
    "created_at": now,
    "updated_at": now,
}
tmp = f"{path}.{os.getpid()}.tmp"
with open(tmp, "w") as f:
    json.dump(payload, f)
os.replace(tmp, path)
PYEOF
      printf '%s\tCAPTURE_TERMINAL\tpr=%s verdict=%s run=%s branch=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$PR_LABEL_CAPTURE" "$VERDICT_LABEL_CAPTURE" "$RUN_LABEL" "$BRANCH_LABEL_CAPTURE" >> /tmp/kanban-pending.flag 2>/dev/null || true
      if [ -x "$PM_OPS" ]; then
        TARGET_TYPE="run"
        TARGET_ID="$RUN_LABEL"
        PM_OPS_PR_ARGS=()
        case "$PR_LABEL_CAPTURE" in
          [0-9]*) TARGET_TYPE="pr"; TARGET_ID="$PR_LABEL_CAPTURE"; PM_OPS_PR_ARGS=(--pr "$PR_LABEL_CAPTURE") ;;
        esac
        python3 "$PM_OPS" record --source pm-context-injector --event capture_terminal --target-type "$TARGET_TYPE" --target-id "$TARGET_ID" ${PM_OPS_PR_ARGS[@]+"${PM_OPS_PR_ARGS[@]}"} --payload "run=$RUN_LABEL" --payload "branch=$BRANCH_LABEL_CAPTURE" --payload "verdict=$VERDICT_LABEL_CAPTURE" --payload "sentinel=$CAPTURE_SENTINEL" --dedupe >/dev/null 2>&1 || true
        python3 "$PM_OPS" obligation-upsert --kind capture_alert --severity high --target-type "$TARGET_TYPE" --target-id "$TARGET_ID" ${PM_OPS_PR_ARGS[@]+"${PM_OPS_PR_ARGS[@]}"} --owner pm --title "Capture transition required for ${TARGET_TYPE} ${TARGET_ID}" --action "Invoke capture-alert-processing; verify the live exact-head run, execute capture-remote-pass or capture-remote-fail, then relay the typed result with the exact PR/run/head/verdict once in the originating Slack thread to <@U0BNFGX2UAX>. Read the thread first and skip the relay if the same relay key is already present." --evidence "run=$RUN_LABEL" --evidence "branch=$BRANCH_LABEL_CAPTURE" --evidence "head=$CAPTURE_HEAD" --evidence "verdict=$VERDICT_LABEL_CAPTURE" --evidence "alert_thread_ts=$TS_LABEL" --evidence "sentinel=$CAPTURE_SENTINEL" >/dev/null 2>&1 || true
      fi
      cat <<EOF

[CAPTURE_ALERT_DETECTED] E2E Capture terminal event: verdict=${VERDICT_LABEL_CAPTURE} run_id=${RUN_LABEL} branch=${BRANCH_LABEL_CAPTURE} pr=${PR_LABEL_CAPTURE} head=${CAPTURE_HEAD:-unknown} alert_thread_ts=${TS_LABEL}.

Trigger: ${REASON}.

Invoke Skill(capture-alert-processing) with ARGUMENTS: run_id=${RUN_LABEL} branch=${BRANCH_LABEL_CAPTURE} pr=${PR_LABEL_CAPTURE} verdict=${VERDICT_LABEL_CAPTURE} alert_thread_ts=${TS_LABEL}.

[CAPTURE_CTO_RELAY_REQUIRED] After the typed capture-remote-pass or capture-remote-fail transition, read the original alert thread and relay the exact live PR/head/run/verdict and the typed transition result (next action or blocker) once to <@U0BNFGX2UAX> in thread ${TS_LABEL}. Use relay_key=capture-terminal:${RUN_LABEL}:${TS_LABEL}; if that key is already present, do not post a duplicate. This is a CTO relay only, not a slot, workflow, label, or MoP action.
EOF
      ;;
    PMF_SURVEY_DETECTED)
      TS_LABEL="${PMF_SURVEY_ALERT_THREAD_TS:-<unknown>}"
      record_survey_prompt_mining_obligation
      cat <<EOF

[PMF_SURVEY_DETECTED] PMF survey report detected: alert_thread_ts=${TS_LABEL}.

Trigger: ${REASON}.

Invoke Skill(survey-report-prompt-miner) with the Slack thread/report text.
EOF
      ;;
	    PR_MERGED_DETECTED)
	      PR_LABEL="${PR_MERGED_NUM:-<unknown>}"
	      if [ -n "${PR_MERGED_NUM:-}" ]; then
	        CLEANUP_SENTINEL="/tmp/pm-required-cleanup-pr-${PR_MERGED_NUM}.json"
	        CLEANUP_PR="$PR_MERGED_NUM" CLEANUP_REASON="$REASON" CLEANUP_SENTINEL="$CLEANUP_SENTINEL" python3 - <<'PYEOF' 2>/dev/null || true
import datetime
import json
import os

now = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
path = os.environ["CLEANUP_SENTINEL"]
payload = {
    "schema_version": 1,
    "source": "pm-context-injector",
    "status": "pending",
    "pr": os.environ.get("CLEANUP_PR"),
    "reason": os.environ.get("CLEANUP_REASON"),
    "created_at": now,
    "updated_at": now,
}
tmp = f"{path}.{os.getpid()}.tmp"
with open(tmp, "w") as f:
    json.dump(payload, f)
os.replace(tmp, path)
PYEOF
		        printf '%s\tPR_MERGED\tpr=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$PR_MERGED_NUM" >> /tmp/kanban-pending.flag 2>/dev/null || true
		        if [ -x "$PM_OPS" ]; then
		          python3 "$PM_OPS" record --source pm-context-injector --event pr_merged_detected --target-type pr --target-id "$PR_MERGED_NUM" --pr "$PR_MERGED_NUM" --payload "sentinel=$CLEANUP_SENTINEL" --dedupe >/dev/null 2>&1 || true
		          python3 "$PM_OPS" obligation-upsert --kind cleanup_pr --severity high --target-type pr --target-id "$PR_MERGED_NUM" --pr "$PR_MERGED_NUM" --owner pm --title "Post-merge cleanup required for PR #${PR_MERGED_NUM}" --action "Invoke/resume cleanup-pr until pm-state:closed-clean is live." --evidence "sentinel=$CLEANUP_SENTINEL" >/dev/null 2>&1 || true
		        fi
		      fi
	      cat <<EOF

[PR_MERGED_DETECTED] PR #${PR_LABEL} merge detected.

Trigger: ${REASON}.

Invoke Skill(cleanup-pr) with the PR number as ARGUMENTS.
EOF
      ;;
    CUSTOMER_POST_FIX_RECURRENCE)
      RECURRENCE_CHANNEL=$(printf '%s' "$PROMPT" | sed -nE 's/^# slack-channel ([A-Z0-9]+).*/\1/p' | head -1)
      RECURRENCE_THREAD=$(printf '%s' "$PROMPT" | grep -oE 'in thread [0-9]+\.[0-9]+' | head -1 | grep -oE '[0-9]+\.[0-9]+')
      record_customer_recurrence_obligation
      cat <<EOF

[CUSTOMER_POST_FIX_RECURRENCE] External-customer post-fix recurrence signal.

Trigger: ${REASON}.

Author: ${LAST_AUTHOR:-<unknown>} · channel: ${RECURRENCE_CHANNEL:-<unknown>} · thread/message: ${RECURRENCE_THREAD:-<unknown>}

Invoke Skill(customer-artifact-investigator) with this prompt's channel/thread/message context as ARGUMENTS.
EOF
      ;;
    CUSTOMER_REPORT_DETECTED)
      cat <<EOF

[CUSTOMER_REPORT_DETECTED] This message appears to be a customer-reported issue.

Trigger: ${REASON}.

Invoke Skill(customer-artifact-investigator) with the customer report as ARGUMENTS.
EOF
      ;;
  esac
fi

# ---------------------------------------------------------------------------
exit 0
