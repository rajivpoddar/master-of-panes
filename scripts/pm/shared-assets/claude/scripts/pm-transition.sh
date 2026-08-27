#!/usr/bin/env bash
# pm-transition.sh - PM-owned deterministic state transition entry point.

set -u
export PATH="${PATH:-}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
CONTROL_PLANE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Production assignment execution imports only the immutable active release.
# Tests may inject a detached root explicitly; never default to the mutable PM
# checkout.
CONTROL_PLANE_KERNEL_ROOT="${CONTROL_PLANE_KERNEL_ROOT:-${HOME}/.claude/control-plane/current/heydonna}"
CONTROL_PLANE_KERNEL_PYTHON="${CONTROL_PLANE_KERNEL_PYTHON:-$CONTROL_PLANE_KERNEL_ROOT/scripts/pm/control_plane/.venv/bin/python}"
CONTROL_PLANE_KERNEL_DATABASE="${CONTROL_PLANE_KERNEL_DATABASE:-/Users/rajiv/.claude/control-plane-artifacts/kernel/control-plane.sqlite3}"
CONTROL_PLANE_KERNEL_SHADOW_JOURNAL="${CONTROL_PLANE_KERNEL_SHADOW_JOURNAL:-/Users/rajiv/.claude/control-plane-artifacts/kernel-shadow/runtime-shadow.jsonl}"
CONTROL_PLANE_KERNEL_SHADOW_DIAGNOSTIC="${CONTROL_PLANE_KERNEL_SHADOW_DIAGNOSTIC:-/tmp/control-plane-kernel-shadow-observer.log}"
CONTROL_PLANE_KERNEL_SHADOW_TIMEOUT_SECONDS="${CONTROL_PLANE_KERNEL_SHADOW_TIMEOUT_SECONDS:-2}"
KERNEL_RELEASE_BOUNDARY="${KERNEL_RELEASE_BOUNDARY:-/Users/rajiv/.claude/scripts/kernel-release-boundary.py}"
if [ -z "${KERNEL_ASSIGNMENT_BOUNDARY:-}" ]; then
  # A source-root override is test-only. Production release roots do not carry
  # this source wrapper under heydonna/, so they resolve the installed alias.
  _source_assignment_boundary="$CONTROL_PLANE_KERNEL_ROOT/scripts/pm/control-plane/kernel-assignment-boundary.py"
  if [ -f "$_source_assignment_boundary" ]; then
    KERNEL_ASSIGNMENT_BOUNDARY="$_source_assignment_boundary"
  else
    KERNEL_ASSIGNMENT_BOUNDARY="/Users/rajiv/.claude/scripts/kernel-assignment-boundary.py"
  fi
  unset _source_assignment_boundary
fi

# Durable PM capacity flags (Rajiv assignment/fill go-live 2026-07-15) — sourced at the public
# entrypoint so reconcile-capacity is production-live for hourly-ops, not only when flags are
# passed inline. The env file uses :- defaults so an explicit inline override (rollback) still wins.
PM_CAPACITY_CONFIG="${PM_CAPACITY_CONFIG:-/Users/rajiv/.claude/pm-capacity.env}"
[ -f "$PM_CAPACITY_CONFIG" ] && . "$PM_CAPACITY_CONFIG"

REPO="${GH_REPO:-heydonna-app/heydonna-app}"
MOP_BASE="${MOP_BASE:-http://127.0.0.1:3100}"
MOP_PRIMARY_REPOSITORY="${MOP_PRIMARY_REPOSITORY:-heydonna-app/heydonna-app}"
MOP_PRIMARY_REPOSITORY_ID="${MOP_PRIMARY_REPOSITORY_ID:-992731533}"
PM_CLONE_LOCK_DIR="${PM_CLONE_LOCK_DIR:-/tmp}"
PM_OPS="${PM_OPS:-/Users/rajiv/.claude/scripts/pm-ops.py}"
CI_RECONCILE_SENTINEL_DIR="${PM_CI_RECONCILE_SENTINEL_DIR:-/tmp}"
PM30M_CONTROL="${PM30M_CONTROL:-/Users/rajiv/.claude/scripts/pm30m-control.py}"
READY_TUPLE_CHECKER="${READY_TUPLE_CHECKER:-/Users/rajiv/.claude/scripts/slot-ready-tuple-check.py}"
PM_STATE="${PM_STATE:-/Users/rajiv/.claude/scripts/pm-state-replace.sh}"
SLOT_CLAIM="${SLOT_CLAIM:-/Users/rajiv/.claude/skills/slot-claim/scripts/slot-claim.sh}"
SWEEP="${SWEEP:-/Users/rajiv/.claude/skills/slot-dispatch-sweep/scripts/sweep.sh}"
PR_SWEEP="${PR_SWEEP:-/Users/rajiv/.claude/skills/pr-state-sweep/scripts/sweep.sh}"
# Durable locally queried priority-rework index (CTO 2026-08-05 transition
# latency repair, thread 1785921483.708289). The assign hot path reads this
# index instead of invoking the global pr-state-sweep synchronously; a stale
# or missing index fails closed as RECONCILE_REQUIRED and enqueues a
# DEDUPLICATED async sweep request consumed by the next reconcile-capacity.
PRIORITY_REWORK_INDEX="${PM_PRIORITY_REWORK_INDEX:-/tmp/pm-priority-rework-index.json}"
PRIORITY_REWORK_INDEX_TTL_SECONDS="${PM_PRIORITY_REWORK_INDEX_TTL_SECONDS:-1800}"
PRIORITY_REWORK_SWEEP_REQUEST="${PM_PRIORITY_REWORK_SWEEP_REQUEST:-/tmp/pm-priority-rework-sweep-requested.json}"
# Durable post-commit outbox: sweeps/Slack/obligation reconciliation is
# enqueued after the authoritative mutation commits and executed by the next
# reconcile-capacity or the bounded background drain at exit. A failed drain
# leaves the entry durable; it never rolls back the verified transition.
TRANSITION_OUTBOX_DIR="${PM_TRANSITION_OUTBOX_DIR:-/tmp/pm-transition-outbox}"
TRANSITION_OUTBOX_LOG="${PM_TRANSITION_OUTBOX_LOG:-/tmp/pm-transition-outbox-drain.log}"
# Command-scoped immutable snapshot + phase-timing sidecars (snapshot_ms,
# guard_ms, checkout_ms, github_mutation_ms, verify_ms, postcommit_ms).
TRANSITION_SNAPSHOT_DIR="${PM_TRANSITION_SNAPSHOT_DIR:-/tmp/pm-transition-snapshots}"
TRANSITION_TIMINGS_DIR="${PM_TRANSITION_TIMINGS_DIR:-/tmp/pm-transition-timings}"
BACKLOG_TRIAGE="${BACKLOG_TRIAGE:-/Users/rajiv/.claude/scripts/backlog-triage.py}"
ISSUE_CLAIMABILITY="${ISSUE_CLAIMABILITY:-$CONTROL_PLANE_DIR/issue_claimability.py}"
CONTROL_PLANE_ISSUE_POLICY="${CONTROL_PLANE_ISSUE_POLICY:-/Users/rajiv/.claude/scripts/control_plane_issue_policy.py}"
HANDOFF_DELIVER="${HANDOFF_DELIVER:-/Users/rajiv/Downloads/projects/heydonna-app/.claude/skills/handoff/scripts/deliver-handoff.sh}"
REWORK_WORKFLOW_TEMPLATE="${REWORK_WORKFLOW_TEMPLATE:-/Users/rajiv/Downloads/projects/heydonna-app/.claude/skills/handoff/templates/workflow-prefix-rework.md}"
CAMPAIGN_LOCK="${CAMPAIGN_LOCK:-/tmp/pm-current-pr-campaign-lock.json}"
MOP_CLEAR="${MOP_CLEAR:-/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/mop-clear-slot.sh}"
CAPTURE_REQUIRED="${CAPTURE_REQUIRED:-/Users/rajiv/.claude/scripts/capture-required.py}"
CAPTURE_LOCAL_PROOF="${CAPTURE_LOCAL_PROOF:-/Users/rajiv/.claude/scripts/capture-local-proof.sh}"
REMOTE_CAPTURE_WRAPPER="${REMOTE_CAPTURE_WRAPPER:-/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/ci/request-budgeted-remote-capture.sh}"
REMOTE_CAPTURE_RUN_VALIDATOR="${REMOTE_CAPTURE_RUN_VALIDATOR:-/Users/rajiv/Downloads/projects/heydonna-app/scripts/ci/remote-capture-run.py}"
CAPTURE_CAPACITY_RECONCILE_GATE="${CAPTURE_CAPACITY_RECONCILE_GATE:-/Users/rajiv/.claude/scripts/capture-capacity-reconcile-gate.py}"
PM_TRANSITION_SELF="${PM_TRANSITION_SELF:-${BASH_SOURCE[0]}}"
CI_READY_GATE="${CI_READY_GATE:-/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/pr-ci-readiness-gate.py}"
LOCAL_PREFLIGHT_VALIDATOR="${LOCAL_PREFLIGHT_VALIDATOR:-/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/local-preflight-proof.py}"
SLACK_SEND="${SLACK_SEND:-/Users/rajiv/.claude/skills/slack-message/scripts/slack-send.sh}"
TRANSITION_ALERT="${TRANSITION_ALERT:-/Users/rajiv/.claude/scripts/pm-transition-alert.sh}"
RAJIV_DM_CHANNEL="${RAJIV_DM_CHANNEL:-D0AMF0XE6TS}"
MERGE_READY_DM_DIR="${MERGE_READY_DM_DIR:-/tmp/pm-merge-ready-dm}"
MERGE_READY_ALERT_DIR="${MERGE_READY_ALERT_DIR:-/tmp/pm-merge-ready-alert}"
MESSAGE_SLOT="${MESSAGE_SLOT:-/Users/rajiv/.claude/skills/message-slot/scripts/message-slot.sh}"
VERIFICATION_LEASE_DIR="${PM_VERIFICATION_LEASE_DIR:-/tmp/pm-verification-leases}"
REVIEW_BUDGET="${REVIEW_BUDGET:-/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/pr-review-budget.py}"
REVIEW_RESCUE_AUTHORIZER="${REVIEW_RESCUE_AUTHORIZER:-/Users/rajiv/.claude/scripts/review_rescue_authorization.py}"
REVIEW_CAP_DISPATCH_DIR="${PM_REVIEW_CAP_DISPATCH_DIR:-/tmp/pm-review-cap-dispatch}"
PM_RESCUE_GIT_REPO="${PM_RESCUE_GIT_REPO:-/Users/rajiv/Downloads/projects/heydonna-app}"
REWORK_PACKET_LEDGER="${REWORK_PACKET_LEDGER:-/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/rework-packet-ledger.py}"
SLOT_RELEASE_QUARANTINE_DIR="${PM_SLOT_RELEASE_QUARANTINE_DIR:-/tmp/pm-slot-release-quarantine}"
SLOT_RELEASE_QUARANTINE_SECONDS="${PM_SLOT_RELEASE_QUARANTINE_SECONDS:-30}"
SLOT_RELEASE_QUARANTINE_CHECK="${SLOT_RELEASE_QUARANTINE_CHECK:-/Users/rajiv/.claude/scripts/slot-release-quarantine-check.py}"
DEPENDENCY_WATCH_LEDGER="${DEPENDENCY_WATCH_LEDGER:-/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/dependency-watch-ledger.py}"
CAPACITY_CONTROL="${CAPACITY_CONTROL:-/Users/rajiv/.claude/scripts/scope-split-capacity-control.py}"
PR_ISSUE_RESOLVER="${PR_ISSUE_RESOLVER:-/Users/rajiv/Downloads/projects/heydonna-app/scripts/pm/resolve_pr_issue.py}"
[ -f "$PR_ISSUE_RESOLVER" ] || {
  echo "PM_TRANSITION_FAILED exit=70 reason=missing canonical PR issue resolver: $PR_ISSUE_RESOLVER" >&2
  exit 70
}
RESCOPE_CONTRACT_TOOL="${RESCOPE_CONTRACT_TOOL:-/Users/rajiv/.claude/scripts/rescope-contract.py}"
FINAL_PATCH_RECOVERY_TOOL="${FINAL_PATCH_RECOVERY_TOOL:-/Users/rajiv/.claude/scripts/final-patch-recovery.py}"
FINAL_PATCH_RECOVERY_REPO="${FINAL_PATCH_RECOVERY_REPO:-/Users/rajiv/Downloads/projects/heydonna-app}"
CI_MAIN_REFRESH_REARM_HELPER="${CI_MAIN_REFRESH_REARM_HELPER:-/Users/rajiv/.claude/scripts/ci-main-refresh-rearm.py}"
CI_MAIN_REFRESH_REARM_REPO="${CI_MAIN_REFRESH_REARM_REPO:-/Users/rajiv/Downloads/projects/heydonna-app}"
CI_MAIN_REFRESH_REARM_RECEIPT_DIR="${CI_MAIN_REFRESH_REARM_RECEIPT_DIR:-/tmp/pm-ci-main-refresh-rearm}"
PROMOTION_CURRENT_HEAD_CI_GUARD="${PROMOTION_CURRENT_HEAD_CI_GUARD:-/Users/rajiv/Downloads/projects/heydonna-app/scripts/ci/promotion-current-head-ci-guard.sh}"
LEGACY_SWEEP_GATE="${LEGACY_SWEEP_GATE:-/Users/rajiv/Downloads/projects/heydonna-app/scripts/pm/legacy_sweep_gate.py}"
[ -f "$LEGACY_SWEEP_GATE" ] || {
  echo "PM_TRANSITION_FAILED exit=70 reason=missing canonical legacy sweep gate: $LEGACY_SWEEP_GATE" >&2
  exit 70
}

# A successful typed PM mutation should close its rolling obligation from live
# state immediately, not at the next 30-minute sweep. This hook is fail-soft:
# transition success remains authoritative, while reconciliation errors are
# written to a local diagnostic log. Read-only commands and dry-runs never
# mutate the obligation ledger.
PM30M_COMMAND="${1:-}"
PM30M_ORIGINAL_ARGS=("$@")
pm30m_reconcile_after_transition() {
  local rc=$? arg index target_pr="" target_issue="" target_slot=""
  trap - EXIT
  if [ "$rc" -ne 0 ] || [ "${PM30M_AUTO_RECONCILE:-1}" != "1" ] || [ ! -x "$PM30M_CONTROL" ]; then
    exit "$rc"
  fi
  case "$PM30M_COMMAND" in
    capacity-snapshot|campaign-status|assign|assign-rework|assign-repro|assign-review|"") exit "$rc" ;;
  esac
  for arg in "${PM30M_ORIGINAL_ARGS[@]}"; do
    [ "$arg" = "--dry-run" ] && exit "$rc"
  done
  for ((index=0; index<${#PM30M_ORIGINAL_ARGS[@]}; index++)); do
    case "${PM30M_ORIGINAL_ARGS[$index]}" in
      --pr) target_pr="${PM30M_ORIGINAL_ARGS[$((index+1))]:-}" ;;
      --issue) target_issue="${PM30M_ORIGINAL_ARGS[$((index+1))]:-}" ;;
      --slot) target_slot="${PM30M_ORIGINAL_ARGS[$((index+1))]:-}" ;;
    esac
  done
  local -a reconcile_args=(reconcile-live --apply)
  [ -n "$target_pr" ] && reconcile_args+=(--target-pr "$target_pr")
  [ -n "$target_issue" ] && reconcile_args+=(--target-issue "$target_issue")
  [ -n "$target_slot" ] && reconcile_args+=(--target-slot "$target_slot")
  if [ -n "$target_pr" ] || [ -n "$target_issue" ] || [ -n "$target_slot" ]; then
    # Obligation reconciliation is durable post-commit outbox work: enqueue it
    # and launch a bounded background drain. The OK line is never held hostage
    # to Slack/pm-ops round trips, and an interrupted drain leaves the entry
    # durable for the next reconcile-capacity.
    postcommit_enqueue "pm30m-reconcile-${PM30M_COMMAND}-${target_pr:-none}-${target_issue:-none}-${target_slot:-none}" \
      python3 "$PM30M_CONTROL" "${reconcile_args[@]}"
  fi
  if [ "${PM_TRANSITION_DRAIN_ON_EXIT:-1}" = "1" ] \
      && [ -d "$TRANSITION_OUTBOX_DIR" ] && [ -n "$(ls -A "$TRANSITION_OUTBOX_DIR" 2>/dev/null || true)" ]; then
    (
      postcommit_drain 12 || true
    ) >>"$TRANSITION_OUTBOX_LOG" 2>&1 &
    disown 2>/dev/null || true
  fi
  exit "$rc"
}
trap pm30m_reconcile_after_transition EXIT

usage() {
  cat >&2 <<'EOF'
Usage:
  pm-transition.sh assign --slot N --issue N|--pr N --branch NAME --expected-epoch E --repository-id ID --handoff-id ID [--head-sha SHA]
  pm-transition.sh assign-rework --slot N --issue N|--pr N --branch NAME --expected-epoch E --repository-id ID --handoff-id ID [--head-sha SHA]
  pm-transition.sh assign-repro --slot N --issue N|--pr N --branch NAME --expected-epoch E --repository-id ID --handoff-id ID [--head-sha SHA]
  pm-transition.sh assign-review --slot N --issue N|--pr N --branch NAME --expected-epoch E --repository-id ID --handoff-id ID [--head-sha SHA]
  pm-transition.sh reserve-handoff --issue N --slot N [--orphan-pr N]
  pm-transition.sh slot-ready --event /tmp/slot-ready-events/slot-N-pr-P-SHA.json
  pm-transition.sh block-pr --pr N --reason ci|capture|codex|pm-gate|pm-review-wait|rebase|dependency|product|infra|qa|other [--issue N] [--slot N] [--completed-head SHA --expected-epoch E] [--packet FILE] [--loop-class TEXT] [--ci-class TEXT] [--failed-run N] [--failed-suite TEXT] [--local-preflight-proof FILE] [--capture-run N] [--dependency-pr N] [--question TEXT --recommended-default TEXT --source-citation TEXT]
  pm-transition.sh retract-operator-block --pr N --reason product|rebase --expected-head SHA --slot N --expected-epoch E --proof FILE
  pm-transition.sh ci-watch --pr N --classification TEXT --failed-run N --proof FILE [--baseline-run N] [--issue N] [--slot N]
  pm-transition.sh record-rework-packet --pr N --packet FILE [--issue N] [--kind TEXT]
  pm-transition.sh deliver-rework-packet --pr N --slot N [--issue N]
  pm-transition.sh reconcile-rework-obligation --pr N [--issue N]
  pm-transition.sh revoke-rework --pr N --expected-head SHA --reason TEXT --proof FILE [--issue N] [--slot N --expected-epoch E] [--preserve-independent-blockers]
  pm-transition.sh dependency-unblocked --pr N [--issue N] [--proof FILE]
  pm-transition.sh resolve-pm-gate --pr N [--issue N] --proof FILE --reason TEXT
  pm-transition.sh ci-local-preflight-pass --pr N [--proof FILE (deprecated/ignored)] [--failed-run N] [--ci-class TEXT]
  pm-transition.sh capture-local-required --pr N [--issue N] [--slot N] [--failed-run N] [--head SHA] [--reason TEXT]
  pm-transition.sh capture-local-pass --pr N --proof FILE [--failed-run N]
  pm-transition.sh capture-remote-dispatch --pr N [--issue N] [--head SHA] [--retry-run N] [--source-e2e-run N] [--descendant-proof-json PATH] [--profile NAME]
  pm-transition.sh capture-remote-dispatch --key KEY --carrier main --head MAIN_SHA --force-key-capture
  pm-transition.sh capture-remote-dispatch --key KEY --carrier main --head MAIN_SHA --key-miss-proof RUN1,RUN2,RUN3
  pm-transition.sh capture-control-plane-repaired --pr N --run N --repair-commit SHA
  pm-transition.sh capture-remote-pass --pr N --run N
  pm-transition.sh capture-remote-exhaust --pr N --first-run N --second-run N
  pm-transition.sh capture-remote-fail --pr N --run N [--dependency-pr N]
	  pm-transition.sh review-cap-dispatch --pr N|none --head SHA --kind plan|code --marker FILE --checkpoint URL [--issue N] [--slot N] [--reason TEXT]
	  pm-transition.sh cto-rescue-pr --pr N [--issue N] [--reason TEXT] [--pm-rescue-proof FILE]
	  pm-transition.sh cto-rescue-issue --issue N [--slot N] [--branch NAME] [--reason TEXT] [--pm-rescue-proof FILE]
	  pm-transition.sh offslot-rescue-start --repository-id OWNER/REPO --pr N --head-sha SHA --expected-labels-json JSON --rescue-kind implementation/off_slot --rescue-receipt FILE
	  pm-transition.sh rescope-pr --pr N [--issue N] [--reason TEXT] # legacy alias
	  pm-transition.sh rescope-issue --issue N [--slot N] [--branch NAME] [--reason TEXT] # legacy alias
	  pm-transition.sh rescope-decide --pr N --decision resume|final_verified_patch|split_and_reimplement|override_with_evidence|escalate_product_decision [--issue N] [--rationale TEXT] [--proof PATH_OR_TEXT] [--child-plan TEXT] [--question TEXT] [--recommended-default DECISION] [--rescope-contract JSON_FILE | --no-scope-change] [--allow-escalate] [--replace-existing]
	  pm-transition.sh rescope-final-patch-applied --pr N --authorized-head SHA [--applied-head SHA] --proof PATCH --approval JSON [--issue N] [--rationale TEXT]
	  pm-transition.sh rescope-issue-decide --issue N --decision final_verified_patch|split_and_reimplement|override_with_evidence|escalate_product_decision [--rationale TEXT] [--proof PATH_OR_TEXT] [--applied-branch NAME --applied-head SHA --approval PATH_OR_TEXT] [--child-plan TEXT] [--question TEXT] [--recommended-default DECISION] [--rescope-contract JSON_FILE | --no-scope-change] [--allow-escalate] [--replace-existing]
	  pm-transition.sh rescope-split-complete (--issue N | --pr N) --child-issue N [--child-issue N ...]
  pm-transition.sh fabrication-reset --slot N (--pr N [--issue N] | --issue N) --kind KIND --claim TEXT --verified-state TEXT --evidence PATH_OR_TEXT [--handoff FILE] [--branch NAME] [--head SHA] [--pm-packet-error]
  pm-transition.sh pm-review --pr N --scope phase-a [--reason TEXT]
  pm-transition.sh pm-review-done --pr N [--affected-test-proof FILE (deprecated/ignored)] [--capture-proof FILE] [--rescue-proof FILE]
  pm-transition.sh ci-stale-run-classified --pr N --run N [--issue N]
  pm-transition.sh ci-rearm-after-main-refresh --pr N --prior-head SHA [--affected-test-proof FILE (deprecated/ignored)]
  pm-transition.sh write-promotion-proof --pr N --state qa-passed-awaiting-ci|merge-ready --content-file FILE
  pm-transition.sh adopt-issue-tuple --issue N --pr N --slot N --repository-id ID --expected-epoch E --branch NAME --head-sha SHA --work-kind KIND --handoff-id ID --claimed-at ISO --new-branch NAME --new-head-sha SHA
  pm-transition.sh adopt-pr-tuple --pr N --issue N --slot N --repository-id ID --expected-epoch E --branch NAME --head-sha SHA --work-kind KIND --handoff-id ID --claimed-at ISO --new-branch NAME --new-head-sha SHA
  pm-transition.sh accept-ready --pr N [--issue N] [--slot N] [--marker FILE|--rescue-proof FILE] [--review-proof FILE] [--affected-test-proof FILE (deprecated/ignored)] [--affected-test-plan FILE (deprecated/ignored)] [--notes FILE] # Phase A only; exact applied Fable rescue replaces phase-a, never fabricates it
  pm-transition.sh validate-ready-proof --pr N
  pm-transition.sh merge-ready --pr N
  pm-transition.sh park-issue --issue N [--pr N] --slot N --blocker product|dependency|infra|pm-gate --reason TEXT [--expected-epoch N] [--head SHA]
  pm-transition.sh unpark-issue --issue N --blocker product|dependency|infra|pm-gate|cto --proof FILE --reason TEXT
  pm-transition.sh reconcile-stale-slot-owner --issue N --slot N --expected-epoch E
  pm-transition.sh reconcile-closed-slot-owner --issue N --slot N --expected-epoch E
  pm-transition.sh reconcile-stale-pr-owner --pr N --slot N --expected-epoch E
  pm-transition.sh reconcile-stale-github-owner --issue N --slot N --expected-epoch E --expect-labels "a,b,c" --blocker REASON --next-action TEXT [--status status:LABEL] [--deadline ISO-TS]
  pm-transition.sh recover-unpicked-claim --slot N --issue N --expected-epoch E
  pm-transition.sh drain-slot --slot N --expected-epoch E [--dry-run]
  pm-transition.sh capacity-snapshot [--slots 1,2,3,4]
  pm-transition.sh reconcile-capacity [--dry-run] [--slots 1,2,3,4]
  pm-transition.sh campaign-lock --prs N,N[,N...] --reason TEXT [--ttl-min N]
  pm-transition.sh campaign-unlock --reason TEXT
  pm-transition.sh cleanup-start --pr N
  pm-transition.sh cleanup --pr N [--retro-path PATH | --retro-trivial]

  # Receipt-style holds: --reason may carry a hold-evidence=<kind> token
  # naming the demanded exit receipt. Kinds the runtime cannot emit (e.g.
  # production_session_receipt) are REFUSED with a typed
  # receipt_unemittable packet (exit 49) before any mutation.

Only this PM-owned command should mutate GitHub/MoP/Kanban state for these
transitions. Slots emit packets via slot-submit-ready.sh / slot-event.sh.
EOF
}

die() {
  local code="$1"; shift
  echo "PM_TRANSITION_FAILED exit=$code reason=$*" >&2
  exit "$code"
}

need_num() {
  local name="$1" value="$2"
  [[ "$value" =~ ^[0-9]+$ ]] || die 2 "$name must be numeric"
}

record_event() {
  [ -x "$PM_OPS" ] || return 0
  python3 "$PM_OPS" record "$@" >/dev/null 2>&1 || true
}

kernel_shadow_enabled() {
  [ "${PM_CONTROL_PLANE_KERNEL_SHADOW:-0}" = "1" ]
}

kernel_shadow_observe() {
  local transition_type="$1" slot="$2" request_source="$3"
  local before_json="$4" observed_json="$5" intent_json="$6"
  local timeout_seconds="$CONTROL_PLANE_KERNEL_SHADOW_TIMEOUT_SECONDS"
  kernel_shadow_enabled || return 0
  [ -x "$CONTROL_PLANE_KERNEL_PYTHON" ] || {
    printf 'observer_unavailable python=%s source=%s\n' \
      "$CONTROL_PLANE_KERNEL_PYTHON" "$request_source" \
      >>"$CONTROL_PLANE_KERNEL_SHADOW_DIAGNOSTIC" 2>&1 || true
    return 0
  }
  [ -n "$before_json" ] && [ -n "$observed_json" ] && [ -n "$intent_json" ] || {
    printf 'observer_input_missing transition=%s slot=%s source=%s\n' \
      "$transition_type" "$slot" "$request_source" \
      >>"$CONTROL_PLANE_KERNEL_SHADOW_DIAGNOSTIC" 2>&1 || true
    return 0
  }
  [[ "$timeout_seconds" =~ ^[1-9][0-9]*$ ]] || timeout_seconds=2
  PM_KERNEL_SHADOW_BEFORE_JSON="$before_json" \
    PM_KERNEL_SHADOW_OBSERVED_JSON="$observed_json" \
    PM_KERNEL_SHADOW_INTENT_JSON="$intent_json" \
    PYTHONPATH="$CONTROL_PLANE_KERNEL_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    /usr/bin/perl -e 'alarm shift; exec @ARGV' "$timeout_seconds" \
      "$CONTROL_PLANE_KERNEL_PYTHON" \
      -m scripts.pm.control_plane.runtime_shadow \
      --transition-type "$transition_type" \
      --requested-by pm-transition \
      --request-source "$request_source" \
      --observation-source mop \
      --observation-id "mop:slot:${slot}:${request_source}:pid:${BASHPID:-$$}" \
      --observed-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      --journal "$CONTROL_PLANE_KERNEL_SHADOW_JOURNAL" \
      >>"$CONTROL_PLANE_KERNEL_SHADOW_DIAGNOSTIC" 2>&1 || true
  return 0
}

capacity_reconcile_trigger() {
  local reason="$1" slot="${2:-}" quarantine_seconds="${SLOT_RELEASE_QUARANTINE_SECONDS:-30}"
  if [ "${PM_CAPACITY_EVENT_TRIGGERS:-0}" != "1" ]; then
    if [ "$reason" = "slot_released" ] && [[ "$slot" =~ ^[1-4]$ ]]; then
      if ! PM_REQUIRED_SLOT_DISPATCH_SENTINEL="${PM_REQUIRED_SLOT_DISPATCH_SENTINEL:-/tmp/pm-required-slot-dispatch.json}" \
        PM_RELEASED_SLOT="$slot" python3 <<'PY' >/dev/null 2>&1
import json
import os
import os
from datetime import datetime, timezone
from pathlib import Path

path = Path(os.environ["PM_REQUIRED_SLOT_DISPATCH_SENTINEL"])
try:
    data = json.loads(path.read_text()) if path.exists() else {}
except (OSError, ValueError):
    data = {}
now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
slots = {int(value) for value in data.get("slots", []) if str(value).isdigit()}
slots.add(int(os.environ["PM_RELEASED_SLOT"]))
for stale_key in ("resolved_at", "resolution", "resolved_by_trigger"):
    data.pop(stale_key, None)
data.update({
    "schema_version": 1,
    "source": "pm-transition",
    "status": "pending",
    "updated_at": now,
    "reason": "released_slot_requires_capacity_reconcile",
    "required_command": "/Users/rajiv/.claude/scripts/pm-transition.sh reconcile-capacity",
    "slots": sorted(slots),
})
data.setdefault("created_at", now)
tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
tmp.replace(path)
PY
      then
        [ "${PM_CAPACITY_RECONCILE_STRICT:-0}" = "1" ] && return 1
      fi
    fi
    return 0
  fi
  [ -x "$CAPACITY_CONTROL" ] || return 0
  if [[ "$slot" =~ ^[1-4]$ ]]; then
    if [ "$reason" = "slot_released" ]; then
      python3 "$CAPACITY_CONTROL" trigger --reason "$reason" --slots "$slot" \
        --delay-seconds "$((quarantine_seconds + 1))" >/dev/null 2>&1 || true
    else
      python3 "$CAPACITY_CONTROL" trigger --reason "$reason" --slots "$slot" >/dev/null 2>&1 || true
    fi
  else
    python3 "$CAPACITY_CONTROL" trigger --reason "$reason" >/dev/null 2>&1 || true
  fi
}

write_slot_release_quarantine() {
  local slot="$1" pr="${2:-}" issue="${3:-}" branch="${4:-}" reason="${5:-}"
  local path tmp now
  [ -n "$slot" ] || return 0
  mkdir -p "$SLOT_RELEASE_QUARANTINE_DIR" 2>/dev/null || return 0
  path="$SLOT_RELEASE_QUARANTINE_DIR/slot-${slot}.json"
  tmp="${path}.$$"
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  python3 - "$tmp" "$slot" "$pr" "$issue" "$branch" "$reason" "$now" "$SLOT_RELEASE_QUARANTINE_SECONDS" <<'PY' 2>/dev/null || return 0
import json
import sys

path, slot, pr, issue, branch, reason, created_at, ttl = sys.argv[1:9]
payload = {
    "slot": int(slot),
    "pr": pr or None,
    "issue": issue or None,
    "branch": branch or None,
    "reason": reason or None,
    "created_at": created_at,
    "ttl_seconds": int(ttl or 30),
}
with open(path, "w", encoding="utf-8") as f:
    json.dump(payload, f, indent=2, sort_keys=True)
    f.write("\n")
PY
  mv "$tmp" "$path" 2>/dev/null || rm -f "$tmp"
}

assert_slot_assignment_not_quarantined() {
  local slot="$1" pr="${2:-}" issue="${3:-}" branch="${4:-}" out rc
  local -a args=(--slot "$slot")
  [ -x "$SLOT_RELEASE_QUARANTINE_CHECK" ] \
    || die 23 "slot release quarantine checker is not executable: $SLOT_RELEASE_QUARANTINE_CHECK"
  [ -n "$pr" ] && args+=(--pr "$pr")
  [ -n "$issue" ] && args+=(--issue "$issue")
  [ -n "$branch" ] && args+=(--branch "$branch")
  out="$(
    PM_SLOT_RELEASE_QUARANTINE_DIR="$SLOT_RELEASE_QUARANTINE_DIR" \
    PM_SLOT_RELEASE_QUARANTINE_SECONDS="$SLOT_RELEASE_QUARANTINE_SECONDS" \
      "$SLOT_RELEASE_QUARANTINE_CHECK" "${args[@]}" 2>&1
  )"
  rc=$?
  case "$rc" in
    0) return 0 ;;
    22) die 22 "$out" ;;
    *) die 23 "$out" ;;
  esac
}

upsert_obligation() {
  [ -x "$PM_OPS" ] || return 0
  python3 "$PM_OPS" obligation-upsert "$@" >/dev/null 2>&1 || true
}

upsert_obligation_strict() {
  # Fail-closed variant for transitions whose release authority depends on the
  # obligation actually being durably written (for example the
  # dependency-unblocked rework obligation). Unlike upsert_obligation it
  # propagates a missing PM_OPS or a writer failure, so a caller can keep the
  # hold intact and retry idempotently.
  [ -x "$PM_OPS" ] || return 1
  python3 "$PM_OPS" obligation-upsert "$@" >/dev/null 2>&1
}

resolve_target_obligations() {
  [ -x "$PM_OPS" ] || return 0
  python3 "$PM_OPS" obligation-resolve-target "$@" >/dev/null 2>&1 || true
}

resolve_ci_reconcile_sentinel() {
  local pr="$1" reason="$2"
  [ -n "$pr" ] || return 0
  python3 - "$CI_RECONCILE_SENTINEL_DIR" "$pr" "$reason" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

directory, pr, reason = sys.argv[1:4]
path = Path(directory) / f"pm-required-ci-reconcile-{pr}.json"
if not path.is_file():
    raise SystemExit(0)
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    print(f"PM_TRANSITION_WARN invalid_ci_reconcile_sentinel path={path} error={exc}", file=sys.stderr)
    raise SystemExit(1)
if str(data.get("pr") or "") != pr:
    print(f"PM_TRANSITION_WARN ci_reconcile_sentinel_pr_mismatch path={path} expected={pr}", file=sys.stderr)
    raise SystemExit(1)
if data.get("status") in {"resolved", "superseded"}:
    raise SystemExit(0)
data["status"] = "resolved"
data["resolution"] = reason
data["resolved_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(path)
PY
}

resolve_pr_obligation_kinds() {
  local pr="$1" issue="$2" reason="$3" external_state="$4" kind saw_ci_reconcile=0
  [ -n "$pr" ] || return 0
  for kind in "${@:5}"; do
    resolve_target_obligations --kind "$kind" --target-type pr --target-id "$pr" --pr "$pr" --reason "$reason" --external-state "$external_state"
    [ -n "$issue" ] && resolve_target_obligations --kind "$kind" --pr "$pr" --issue "$issue" --reason "$reason" --external-state "$external_state"
    [ "$kind" = "ci_reconcile" ] && saw_ci_reconcile=1
  done
  if [ "$saw_ci_reconcile" -eq 1 ]; then
    resolve_ci_reconcile_sentinel "$pr" "$reason" \
      || echo "PM_TRANSITION_WARN failed_to_resolve_ci_reconcile_sentinel pr=$pr reason=$reason" >&2
  fi
}

# #7132-class terminal-green reconciliation: pm-review-done already_green must
# never return a successful receipt with no wake row. This materializes exactly
# one open severity=high ci_reconcile obligation (owner=pm, immediate
# next_review_at) plus the durable ci_reconcile sentinel the claim/resolve
# machinery binds to, keyed by PR + exact 40-char head. Identical head/event is
# idempotent (an existing pending/in_progress/resolved sentinel at the same
# head is never rewritten; a resolved event is never reopened); a new head is a
# new tuple. Any failure dies so the caller cannot print PM_TRANSITION_OK.
materialize_ci_reconcile_wake() {
  local pr="$1" head="$2" issue="$3" ci_run_id="$4" e2e_run_id="$5" proof="$6"
  local sentinel="" effective_status="" evidence_json="" action="" out=""
  [ -n "$pr" ] || die 1 "ci_reconcile wake requires pr"
  [ -n "$head" ] || die 1 "ci_reconcile wake requires exact 40-char head"
  [ -n "$ci_run_id" ] || die 1 "ci_reconcile wake requires CI run id"
  [ -n "$e2e_run_id" ] || die 1 "ci_reconcile wake requires E2E run id"
  [ -x "$PM_OPS" ] || die 1 "ci_reconcile wake cannot be written: pm-ops missing: $PM_OPS"

  sentinel="$CI_RECONCILE_SENTINEL_DIR/pm-required-ci-reconcile-${pr}.json"
  effective_status="$(python3 - "$sentinel" "$pr" "$head" "$ci_run_id" "$e2e_run_id" "$proof" <<'PY' 2>&1
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

path, pr, head, ci_run_id, e2e_run_id, proof = sys.argv[1:7]
existing = {}
try:
    existing = json.loads(Path(path).read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    existing = {}
if str(existing.get("head_sha") or "") == head:
    status = str(existing.get("status") or "")
    if status in {"pending", "in_progress", "resolved"}:
        print(status)
        raise SystemExit(0)
now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
data = {
    "schema_version": 1,
    "source": "pm-transition pm-review-done already-green",
    "status": "pending",
    "event": "success",
    "pr": pr,
    "run_id": str(e2e_run_id),
    "ci_run_id": str(ci_run_id),
    "e2e_run_id": str(e2e_run_id),
    "head_sha": head,
    "alert_thread_ts": "unknown",
    "reason": "exact-head CI and E2E are green but PM state needs reconciliation",
    "guard_output": proof,
    "created_at": now,
    "updated_at": now,
}
tmp = Path(f"{path}.tmp.{os.getpid()}")
tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(path)
print("pending")
PY
)" || die 1 "cannot materialize ci_reconcile sentinel for PR #$pr head=${head:0:8}"
  if [ "$effective_status" = "resolved" ]; then
    return 0
  fi

  evidence_json="$(python3 - "$pr" "$head" "$ci_run_id" "$e2e_run_id" "$proof" <<'PY' 2>&1
import json
import sys

pr, head, ci_run_id, e2e_run_id, proof = sys.argv[1:6]
print(json.dumps({
    "head_sha": head,
    "ci_run_id": ci_run_id,
    "e2e_run_id": e2e_run_id,
    "pm_stop_actionable": 1,
    "proof": proof,
    "required_skill": "ci-success-reconciliation",
    "source": "pm-transition pm-review-done already-green",
}, sort_keys=True))
PY
)" || die 1 "cannot build ci_reconcile evidence for PR #$pr"
  action="PR #${pr} is terminal green at exact head ${head} (CI run ${ci_run_id} / E2E run ${e2e_run_id}) with PM state still pm-review-pending. Run Skill(ci-success-reconciliation) and pm-readiness-contract now, then execute exactly one typed merge-ready or blocker transition and resolve the CI event with that disposition. Do not rerun CI, recapture, or edit labels directly."
  out="$(python3 "$PM_OPS" obligation-upsert \
    --kind ci_reconcile --severity high \
    --target-type pr --target-id "$pr" --pr "$pr" \
    ${issue:+--issue "$issue"} \
    --owner pm --horizon hourly \
    --next-review-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --dedupe-group "ci_reconcile:${pr}:${head}" \
    --title "PR #${pr} terminal-green pm-review-pending requires reconciliation" \
    --action "$action" \
    --blocker ci_success_pending_reconciliation \
    --evidence-json "$evidence_json" 2>&1)" || {
    die 1 "failed to materialize ci_reconcile obligation for PR #$pr head=${head:0:8}: ${out:-unknown}"
  }
}

utc_plus_minutes() {
  local minutes="${1:-30}"
  python3 - "$minutes" <<'PY' 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ
import sys
from datetime import datetime, timezone, timedelta
minutes = int(sys.argv[1])
print((datetime.now(timezone.utc) + timedelta(minutes=minutes)).replace(microsecond=0).isoformat().replace("+00:00", "Z"))
PY
}

kanban_flag() {
  local event="$1"; shift
  printf '%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$event" "$*" >> /tmp/kanban-pending.flag 2>/dev/null || true
}

BACKLOG_PROMOTER_STATUS="not_checked"
BACKLOG_PROMOTER_SELECTED=0
BACKLOG_PROMOTER_LOG=""
BACKLOG_PROMOTER_SWEEP_LOG=""

transition_alert() {
  [ "${PM_TRANSITION_DISABLE_SLACK_ALERTS:-0}" = "1" ] && return 0
  [ -x "$TRANSITION_ALERT" ] || return 0
  local out rc
  out="$(bash "$TRANSITION_ALERT" "$@" 2>&1)"
  rc=$?
  [ -n "$out" ] && printf '%s\n' "$out" >&2
  return 0
}

notify_merge_ready_dm() {
  local pr="$1" issue="$2" branch="$3" released_slots="$4"
  [ "${PM_TRANSITION_DISABLE_MERGE_READY_DM:-0}" = "1" ] && return 0
  [ -x "$SLACK_SEND" ] || {
    record_event --source pm-transition --event merge_ready_dm_unavailable --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --payload "reason=slack_send_missing" --payload "script=$SLACK_SEND"
    echo "PM_TRANSITION_MERGE_READY_DM status=skipped reason=slack_send_missing pr=$pr script=$SLACK_SEND" >&2
    return 0
  }

  local pr_json title url head short_head merge_state flag tmp msg rc slack_line
  pr_json="$(gh pr view "$pr" --repo "$REPO" --json title,url,headRefOid,mergeStateStatus 2>/dev/null || true)"
  title="$(printf '%s' "$pr_json" | json_field title 2>/dev/null || true)"
  url="$(printf '%s' "$pr_json" | json_field url 2>/dev/null || true)"
  head="$(printf '%s' "$pr_json" | json_field headRefOid 2>/dev/null || true)"
  merge_state="$(printf '%s' "$pr_json" | json_field mergeStateStatus 2>/dev/null || true)"
  short_head="${head:-unknown}"
  short_head="${short_head:0:10}"

  mkdir -p "$MERGE_READY_DM_DIR" 2>/dev/null || true
  flag="$MERGE_READY_DM_DIR/pr-${pr}-${head:-unknown}.sent"
  if [ -f "$flag" ]; then
    record_event --source pm-transition --event merge_ready_dm_skipped --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --payload "reason=dedup" --payload "head=${head:-unknown}"
    echo "PM_TRANSITION_MERGE_READY_DM status=skipped reason=dedup pr=$pr head=${short_head}" >&2
    return 0
  fi

  tmp="$MERGE_READY_DM_DIR/pr-${pr}-${head:-unknown}.send.out"
  msg="$(cat <<EOF
*PR #${pr}${issue:+ (#${issue})} — MERGE READY*
${title:-<title unavailable>}
${url:-https://github.com/${REPO}/pull/${pr}}

State: \`merge-ready\`
Head: \`${short_head}\`${branch:+ on \`$branch\`}
Mergeability: \`${merge_state:-unknown}\`
Released slots: ${released_slots:-none}

PM transition completed. Run the normal latest-head pre-merge guard before merging.
EOF
)"

  if printf '%s\n' "$msg" | bash "$SLACK_SEND" -c "$RAJIV_DM_CHANNEL" -f >"$tmp" 2>&1; then
    slack_line="$(tr '\n' ' ' < "$tmp" 2>/dev/null | sed 's/[[:space:]]\{1,\}/ /g' | head -c 300)"
    {
      printf 'sent_at=%s\n' "$(date -Iseconds)"
      printf 'pr=%s\nissue=%s\nhead=%s\nchannel=%s\n' "$pr" "${issue:-}" "${head:-unknown}" "$RAJIV_DM_CHANNEL"
      printf 'slack_output=%s\n' "$slack_line"
    } > "$flag" 2>/dev/null || true
    record_event --source pm-transition --event merge_ready_dm_sent --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --payload "head=${head:-unknown}" --payload "channel=$RAJIV_DM_CHANNEL" --payload "slack=$slack_line" --dedupe
    echo "PM_TRANSITION_MERGE_READY_DM status=sent pr=$pr head=${short_head} channel=$RAJIV_DM_CHANNEL proof=$flag" >&2
  else
    rc=$?
    slack_line="$(tr '\n' ' ' < "$tmp" 2>/dev/null | sed 's/[[:space:]]\{1,\}/ /g' | head -c 300)"
    record_event --source pm-transition --event merge_ready_dm_failed --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --payload "head=${head:-unknown}" --payload "channel=$RAJIV_DM_CHANNEL" --payload "rc=$rc" --payload "slack=$slack_line"
    echo "PM_TRANSITION_MERGE_READY_DM status=failed rc=$rc pr=$pr head=${short_head} channel=$RAJIV_DM_CHANNEL output=${slack_line:-<empty>}" >&2
  fi
  return 0
}

# Posts the merge-ready notification into the PR's transition thread via the
# existing canonical transition-thread postback (transition_alert ->
# pm-transition-alert.sh) with the CTO at-mention, after the promotion mutation
# and the Rajiv DM. Additive and fail-soft: the promotion stands regardless of
# the post outcome. Idempotent per (pr, head) via the flag file, mirroring the
# merge-ready DM dedup; the alert script's own sent-key dedup backstops the
# same tuple. No post on failed promotion: this is only called after the
# promotion mutation commits and the DM completes.
merge_ready_thread_notify() {
  local pr="$1" issue="$2" branch="$3" head="$4" proof="$5" released_slots="$6"
  [ "${PM_TRANSITION_DISABLE_SLACK_ALERTS:-0}" = "1" ] && return 0
  mkdir -p "$MERGE_READY_ALERT_DIR" 2>/dev/null || true
  local short_head flag pr_json title url merge_state msg tmp out
  short_head="${head:-unknown}"
  short_head="${short_head:0:10}"
  flag="$MERGE_READY_ALERT_DIR/pr-${pr}-${head:-unknown}.sent"
  if [ -f "$flag" ]; then
    record_event --source pm-transition --event merge_ready_thread_alert_skipped --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --payload "reason=dedup" --payload "head=${head:-unknown}"
    echo "PM_TRANSITION_MERGE_READY_ALERT status=skipped reason=dedup pr=$pr head=${short_head}" >&2
    return 0
  fi

  pr_json="$(gh pr view "$pr" --repo "$REPO" --json title,url,headRefOid,mergeStateStatus 2>/dev/null || true)"
  title="$(printf '%s' "$pr_json" | json_field title 2>/dev/null || true)"
  url="$(printf '%s' "$pr_json" | json_field url 2>/dev/null || true)"
  merge_state="$(printf '%s' "$pr_json" | json_field mergeStateStatus 2>/dev/null || true)"

  msg="$(cat <<EOF
*PR #${pr}${issue:+ (#${issue})} — MERGE READY* <@U0BNFGX2UAX>
${title:-<title unavailable>}
${url:-https://github.com/${REPO}/pull/${pr}}

State: \`merge-ready\`
Head: \`${short_head}\`${branch:+ on \`$branch\`}
Mergeability: \`${merge_state:-unknown}\`
Proof: \`${proof:-not recorded}\`
Released slots: ${released_slots:-none}

Merge-ready transition posted to the PR transition thread. Process per the normal latest-head pre-merge guard before merging.
EOF
)"
  tmp="$MERGE_READY_ALERT_DIR/pr-${pr}-${head:-unknown}.msg"
  printf '%s\n' "$msg" >"$tmp" 2>/dev/null || {
    record_event --source pm-transition --event merge_ready_thread_alert_failed --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --payload "head=${head:-unknown}" --payload "reason=message_write_failed" --payload "path=$tmp"
    echo "PM_TRANSITION_MERGE_READY_ALERT status=failed reason=message_write_failed pr=$pr head=${short_head}" >&2
    return 0
  }

  out="$(transition_alert --event external-action-required --pr "$pr" --issue "$issue" --state merge-ready --head "$head" --branch "$branch" --proof "$proof" --reason merge-ready --message-file "$tmp" 2>&1)"
  case "$out" in
    *PM_TRANSITION_ALERT_OK*)
      : >"$flag" 2>/dev/null || true
      record_event --source pm-transition --event merge_ready_thread_alert_sent --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --payload "head=${head:-unknown}" --payload "proof=$proof" --dedupe
      echo "PM_TRANSITION_MERGE_READY_ALERT status=sent pr=$pr head=${short_head}" >&2
      ;;
    *PM_TRANSITION_ALERT_SKIPPED*)
      : >"$flag" 2>/dev/null || true
      record_event --source pm-transition --event merge_ready_thread_alert_skipped --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --payload "reason=alert_dedup" --payload "head=${head:-unknown}"
      echo "PM_TRANSITION_MERGE_READY_ALERT status=skipped reason=alert_dedup pr=$pr head=${short_head}" >&2
      ;;
    *PM_TRANSITION_ALERT_FAILED*)
      record_event --source pm-transition --event merge_ready_thread_alert_failed --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --payload "head=${head:-unknown}" --payload "reason=alert_failed" --payload "output=${out:0:120}"
      echo "PM_TRANSITION_MERGE_READY_ALERT status=failed reason=alert_failed pr=$pr head=${short_head} output=${out:0:120}" >&2
      ;;
    "")
      record_event --source pm-transition --event merge_ready_thread_alert_skipped --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --payload "reason=alert_unavailable" --payload "head=${head:-unknown}"
      echo "PM_TRANSITION_MERGE_READY_ALERT status=skipped reason=alert_unavailable pr=$pr head=${short_head}" >&2
      ;;
    *)
      record_event --source pm-transition --event merge_ready_thread_alert_failed --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --payload "head=${head:-unknown}" --payload "reason=unexpected_output" --payload "output=${out:0:120}"
      echo "PM_TRANSITION_MERGE_READY_ALERT status=failed reason=unexpected_output pr=$pr head=${short_head} output=${out:0:120}" >&2
      ;;
  esac
  return 0
}

transition_packet_upsert() {
  local event_file="$1"
  [ -x "$PM_OPS" ] || die 1 "PM ops DB writer not executable: $PM_OPS"
  python3 "$PM_OPS" transition-packet-upsert \
    --packet-type slot_ready \
    --status pending \
    --event-file "$event_file" \
    >/dev/null || die 1 "failed to persist transition packet in PM ops DB: $event_file"
}

transition_packet_status() {
  local event_file="$1" status="$2" reason="$3"
  [ -x "$PM_OPS" ] || die 1 "PM ops DB writer not executable: $PM_OPS"
  python3 "$PM_OPS" transition-packet-status \
    --packet-type slot_ready \
    --status "$status" \
    --event-file "$event_file" \
    --reason "$reason" \
    >/dev/null || die 1 "failed to mark transition packet $status in PM ops DB: $event_file"
  if [ "$status" = "consumed" ]; then
    capacity_reconcile_trigger slot_ready_consumed "$(event_get "$event_file" slot 2>/dev/null || true)"
  fi
}

event_get() {
  local file="$1" key="$2"
  python3 - "$file" "$key" <<'PY'
import json
import sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
value = data
for part in sys.argv[2].split("."):
    value = value.get(part) if isinstance(value, dict) else None
if isinstance(value, bool):
    print("true" if value else "false")
elif value is None:
    print("")
else:
    print(value)
PY
}

json_field() {
  local key="$1"
  python3 -c '
import json
import sys
data = json.load(sys.stdin)
value = data
for part in sys.argv[1].split("."):
    value = value.get(part) if isinstance(value, dict) else None
if isinstance(value, bool):
    print("true" if value else "false")
elif value is None:
    print("")
else:
    print(value)
' "$key"
}

# `gh pr view` uses GraphQL and can fail when the GraphQL quota is exhausted
# even though the REST core quota is healthy. Keep transitions fail-closed, but
# fall back to an equivalent normalized REST payload for fields REST can prove.
pr_metadata_json() {
  local pr="$1" payload
  # Command-scoped snapshot hook: within a snapshotted command (assign-rework),
  # PR metadata is served from the immutable snapshot — zero extra gh reads.
  if [ -n "${PM_COMMAND_SNAPSHOT:-}" ] && [ -f "$PM_COMMAND_SNAPSHOT" ]; then
    payload="$(command_snapshot_pr_json "$pr")"
    if [ -n "$payload" ]; then
      printf '%s' "$payload"
      return 0
    fi
  fi
  payload="$(gh pr view "$pr" --repo "$REPO" --json state,isDraft,headRefOid,headRefName,baseRefName,title,url,labels,mergeStateStatus,body,closingIssuesReferences,updatedAt 2>/dev/null || true)"
  if [ -n "$payload" ]; then
    printf '%s' "$payload"
    return 0
  fi
  payload="$(gh api "repos/${REPO}/pulls/${pr}" 2>/dev/null || true)"
  [ -n "$payload" ] || return 1
  echo "WARN: PR #${pr} metadata transport=rest (GraphQL unavailable)" >&2
  printf '%s' "$payload" | python3 -c '
import json, sys
p = json.load(sys.stdin)
print(json.dumps({
    "state": str(p.get("state") or "").upper(),
    "isDraft": bool(p.get("draft")),
    "headRefOid": ((p.get("head") or {}).get("sha") or ""),
    "headRefName": ((p.get("head") or {}).get("ref") or ""),
    "baseRefName": ((p.get("base") or {}).get("ref") or ""),
    "title": p.get("title") or "",
    "body": p.get("body") or "",
    "url": p.get("html_url") or "",
    "labels": [{"name": x.get("name", "")} for x in (p.get("labels") or [])],
    "mergeStateStatus": str(p.get("mergeable_state") or "").upper(),
    "closingIssuesReferences": [],
    "updatedAt": p.get("updated_at") or "",
}))'
}

issue_from_pr() {
  local pr="$1" pr_json
  pr_json="$(pr_metadata_json "$pr" || true)"
  printf '%s' "$pr_json" | python3 "$PR_ISSUE_RESOLVER"
}

open_pr_exact_linked_issue() {
  # Return exactly one OPEN PR number exact-linked to an issue, or "0" when
  # none exists. Candidate scan uses the same resolver semantics (branch then
  # prose), and every candidate is confirmed through the canonical PR metadata
  # + PR_ISSUE_RESOLVER (prefers closingIssuesReferences). Fail closed on
  # unreadable PR state, an owned/blocked candidate PR, or multiple exact
  # links (ambiguous tuple).
  local issue="$1" candidates="" confirmed="" pr pr_json resolved pr_state pr_head pr_labels pr_list
  pr_list="$(gh pr list --repo "$REPO" --state open --json number,headRefName,headRefOid,title,body,closingIssuesReferences --limit 200 2>/dev/null)" \
    || return 1
  case "$pr_list" in
    '') return 1 ;;
    '['*) ;;
    *) return 1 ;;
  esac
  candidates="$(printf '%s' "$pr_list" | ISSUE="$issue" python3 -c '
import json
import os
import re
import sys

issue = int(os.environ["ISSUE"])
try:
    prs = json.load(sys.stdin)
except Exception:
    sys.exit(2)
found = []
for p in prs:
    for ref in p.get("closingIssuesReferences") or []:
        number = ref.get("number") if isinstance(ref, dict) else None
        if isinstance(number, int) and number == issue:
            found.append(str(p.get("number")))
            break
    else:
        branch = p.get("headRefName") or ""
        m = re.fullmatch(
            r"(?:.*/)?(?:(?:fix|feat|feature|bug|test|chore|perf|refactor|enhance)/)?"
            r"([0-9]{3,6})(?:[-_/].*)?",
            branch,
        )
        if m and int(m.group(1)) == issue:
            found.append(str(p.get("number")))
            continue
        title = str(p.get("title") or "")
        body = str(p.get("body") or "")
        text = title + "\n" + body
        if re.search(r"#" + str(issue) + r"(?![0-9])", text):
            found.append(str(p.get("number")))
print(",".join(found))
' 2>/dev/null)" || return 1
  case "$candidates" in
    ""|"0"|,*) printf '0'; return 0 ;;
  esac
  local old_ifs="$IFS"
  IFS=','
  for pr in $candidates; do
    IFS="$old_ifs"
    [[ "$pr" =~ ^[0-9]+$ ]] || { IFS="$old_ifs"; return 1; }
    pr_json="$(pr_metadata_json "$pr" || true)"
    [ -n "$pr_json" ] || { IFS="$old_ifs"; return 1; }
    resolved="$(printf '%s' "$pr_json" | python3 "$PR_ISSUE_RESOLVER" 2>/dev/null || true)"
    [ -n "$resolved" ] && [ "$resolved" = "$issue" ] || continue
    pr_state="$(printf '%s' "$pr_json" | json_field state 2>/dev/null || true)"
    [ "$pr_state" = "OPEN" ] || continue
    pr_head="$(printf '%s' "$pr_json" | json_field headRefOid 2>/dev/null || true)"
    [[ "$pr_head" =~ ^[0-9a-fA-F]{40}$ ]] || continue
    pr_labels="$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print(",".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
    if printf '%s\n' "$pr_labels" | tr ',' '\n' | grep -Eq '^(slot:[1-4]|pm-state:|merge-ready)$'; then
      IFS="$old_ifs"
      return 1
    fi
    confirmed="${confirmed:+$confirmed,}$pr"
  done
  IFS="$old_ifs"
  case "$confirmed" in
    "") printf '0' ;;
    *,*) return 1 ;;
    *) printf '%s' "$confirmed" ;;
  esac
}

slot_from_labels() {
  local pr="$1" issue="${2:-}" labels slot
  labels="$(pr_metadata_json "$pr" 2>/dev/null | python3 -c 'import json,sys; print(",".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
  slot="$(printf '%s' "$labels" | grep -oE 'slot:[1-4]' | head -1 | cut -d: -f2)"
  if [ -z "$slot" ] && [ -n "$issue" ]; then
    labels="$(gh issue view "$issue" --repo "$REPO" --json labels --jq '[.labels[].name] | join(",")' 2>/dev/null || true)"
    slot="$(printf '%s' "$labels" | grep -oE 'slot:[1-4]' | head -1 | cut -d: -f2)"
  fi
  printf '%s' "$slot"
}

issue_from_branch_name() {
  local branch="${1:-}"
  printf '%s\n' "$branch" | sed -nE 's#(^|.*/)?(fix|feat|feature|bug|test|chore|perf|refactor|enhance)?/?([0-9]{3,6})([-_/].*)?$#\3#p' | head -1
}

issue_active_for_slot() {
  local issue="$1" slot="$2" issue_json state labels
  [ -n "$issue" ] || return 1
  [[ "$issue" =~ ^[0-9]+$ ]] || return 1
  issue_json="$(gh issue view "$issue" --repo "$REPO" --json state,labels 2>/dev/null || true)"
  [ -n "$issue_json" ] || return 1
  state="$(printf '%s' "$issue_json" | json_field state 2>/dev/null || true)"
  [ "$state" = "OPEN" ] || return 1
  labels="$(printf '%s' "$issue_json" | python3 -c 'import json,sys; print(",".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
  printf '%s\n' "$labels" | tr ',' '\n' | grep -qx "slot:${slot}" || return 1
  printf '%s\n' "$labels" | tr ',' '\n' | grep -Eq '^(status:in-progress|status:in-review|pm-state:blocked-rework|pm-state:pm-review-pending|pm-state:qa-passed-awaiting-ci|pm-state:rescope-required)$' || return 1
  return 0
}

issue_open_active() {
  local issue="$1" issue_json state labels
  [ -n "$issue" ] || return 1
  [[ "$issue" =~ ^[0-9]+$ ]] || return 1
  issue_json="$(gh issue view "$issue" --repo "$REPO" --json state,labels 2>/dev/null || true)"
  [ -n "$issue_json" ] || return 1
  state="$(printf '%s' "$issue_json" | json_field state 2>/dev/null || true)"
  [ "$state" = "OPEN" ] || return 1
  labels="$(printf '%s' "$issue_json" | python3 -c 'import json,sys; print(",".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
  printf '%s\n' "$labels" | tr ',' '\n' | grep -Eq '^(status:in-progress|status:in-review|pm-state:blocked-rework|pm-state:pm-review-pending|pm-state:qa-passed-awaiting-ci|pm-state:rescope-required)$' || return 1
  return 0
}

slot_other_open_owners() {
  local slot="$1" target_issue="${2:-}" issue_json pr_json
  issue_json="$(gh issue list --repo "$REPO" --state open --label "slot:${slot}" --json number,title,labels --limit 100 2>/dev/null || true)"
  pr_json="$(gh pr list --repo "$REPO" --state open --label "slot:${slot}" --json number,title,headRefName,labels --limit 100 2>/dev/null || true)"
  ISSUE_JSON="$issue_json" PR_JSON="$pr_json" TARGET_ISSUE="$target_issue" python3 <<'PY' 2>/dev/null || true
import json
import os
import re

target = str(os.environ.get("TARGET_ISSUE") or "")
owners = []
try:
    issue_rows = json.loads(os.environ.get("ISSUE_JSON") or "[]")
except Exception:
    issue_rows = []
for row in issue_rows:
    num = str(row.get("number") or "")
    if target and num == target:
        continue
    labels = {x.get("name", "") for x in row.get("labels", [])}
    if "status:done" in labels:
        continue
    owners.append(f"issue#{num}")

try:
    pr_rows = json.loads(os.environ.get("PR_JSON") or "[]")
except Exception:
    pr_rows = []
branch_re = re.compile(rf"(^|/){re.escape(target)}($|[-_/])") if target else None
title_re = re.compile(rf"(^|[^0-9])#{re.escape(target)}($|[^0-9])") if target else None
for row in pr_rows:
    branch = row.get("headRefName") or ""
    title = row.get("title") or ""
    if target and ((branch_re and branch_re.search(branch)) or (title_re and title_re.search(title))):
        continue
    owners.append(f"pr#{row.get('number')}:{branch}")
print(",".join(owners))
PY
}

slot_open_owner_issues() {
  local slot="$1" issue_json
  issue_json="$(gh issue list --repo "$REPO" --state open --label "slot:${slot}" --json number,title,labels --limit 100 2>/dev/null || true)"
  ISSUE_JSON="$issue_json" python3 <<'PY' 2>/dev/null || true
import json
import os

try:
    issue_rows = json.loads(os.environ.get("ISSUE_JSON") or "[]")
except Exception:
    issue_rows = []
owners = []
for row in issue_rows:
    labels = {x.get("name", "") for x in row.get("labels", [])}
    if "status:done" in labels:
        continue
    if not labels.intersection({
        "status:in-progress",
        "status:in-review",
        "pm-state:blocked-rework",
        "pm-state:pm-review-pending",
        "pm-state:qa-passed-awaiting-ci",
        "pm-state:rescope-required",
    }):
        continue
    num = row.get("number")
    if num:
        owners.append(str(num))
print(",".join(owners))
PY
}

clear_other_slot_labels() {
  local kind="$1" id="$2" keep_slot="$3" slot
  for slot in 1 2 3 4; do
    [ "$slot" = "$keep_slot" ] && continue
    gh "$kind" edit "$id" --repo "$REPO" --remove-label "slot:${slot}" >/dev/null 2>&1 || true
  done
}

clear_slot_label_verified() {
  local kind="$1" id="$2" slot="$3" phase="$4"
  local attempt labels label_state
  [ -n "$id" ] || return 0
  for attempt in 1 2 3; do
    labels="$(gh "$kind" view "$id" --repo "$REPO" --json labels 2>/dev/null || true)"
    label_state="$(printf '%s' "$labels" | python3 -c '
import json
import sys

slot_label = sys.argv[1]
data = json.load(sys.stdin)
labels = {item.get("name", "") for item in data.get("labels", [])}
print("present" if slot_label in labels else "absent")
' "slot:${slot}" 2>/dev/null || true)"
    if [ "$label_state" = "absent" ]; then
      printf 'PM_TRANSITION_SLOT_LABEL_VERIFIED kind=%s id=%s slot=%s phase=%s attempt=%s\n' \
        "$kind" "$id" "$slot" "$phase" "$attempt" >&2
      return 0
    fi
    gh "$kind" edit "$id" --repo "$REPO" --remove-label "slot:${slot}" >/dev/null 2>&1 || true
    sleep 0.2
  done
  labels="$(gh "$kind" view "$id" --repo "$REPO" --json labels 2>/dev/null || true)"
  label_state="$(printf '%s' "$labels" | python3 -c '
import json
import sys

slot_label = sys.argv[1]
data = json.load(sys.stdin)
labels = {item.get("name", "") for item in data.get("labels", [])}
print("present" if slot_label in labels else "absent")
' "slot:${slot}" 2>/dev/null || true)"
  if [ "$label_state" = "absent" ]; then
    printf 'PM_TRANSITION_SLOT_LABEL_VERIFIED kind=%s id=%s slot=%s phase=%s attempt=final\n' \
      "$kind" "$id" "$slot" "$phase" >&2
    return 0
  fi
  printf 'PM_TRANSITION_SLOT_LABEL_RECONCILE_FAILED kind=%s id=%s slot=%s phase=%s\n' \
    "$kind" "$id" "$slot" "$phase" >&2
  return 15
}

mop_slot_status() {
  local slot="$1"
  curl -sS -m 4 "$MOP_BASE/slots" 2>/dev/null | python3 -c '
import json
import sys
slot = int(sys.argv[1])
try:
    data = json.load(sys.stdin)
except Exception:
    print("unreachable")
    sys.exit(0)
entry = next((s for s in data.get("slots", []) if int(s.get("slot", -1)) == slot), None)
if not entry:
    print("missing")
elif entry.get("occupied"):
    print("occupied:%s:%s" % (entry.get("issue") or "", entry.get("dnd")))
else:
    print("free::%s" % entry.get("dnd"))
' "$slot"
}

mop_slot_epoch() {
  local slot="$1"
  curl -sS -m 4 "$MOP_BASE/slots" 2>/dev/null | python3 -c '
import json
import sys
try:
    slot = int(sys.argv[1])
    data = json.load(sys.stdin)
    entry = next(
        (row for row in data.get("slots", []) if int(row.get("slot", -1)) == slot),
        None,
    )
    value = entry.get("assignment_epoch") if entry else None
except Exception:
    raise SystemExit(1)
if not isinstance(value, int) or value < 0:
    raise SystemExit(1)
print(value)
' "$slot" || return 1
}

mop_slot_turn_state() {
  local slot="$1"
  curl -sS -m 4 "$MOP_BASE/slots" 2>/dev/null | python3 -c '
import json
import sys
try:
    slot = int(sys.argv[1])
    data = json.load(sys.stdin)
    entry = next(
        (row for row in data.get("slots", []) if int(row.get("slot", -1)) == slot),
        None,
    )
    value = entry.get("active_turn_state") if entry else None
except Exception:
    raise SystemExit(1)
if value not in {"active", "inactive", "indeterminate"}:
    raise SystemExit(1)
print(value)
' "$slot" || return 1
}

mop_slot_restorable_free() {
  # A slot whose MoP row carries NO owner (pr and issue both null) and an
  # inactive turn is genuinely free/inactive even when a prior
  # assignment_epoch lingers (the S1 half-released shape: occupied may remain
  # true from the old claim while pr/issue/branch/head were already cleared).
  # DND and any non-inactive turn disqualify.  This classification is used
  # ONLY where a slot is explicitly targeted (capture-local-required --slot)
  # or auto-selected by capture-local-required; ordinary assign / assign-rework
  # gates are untouched.
  local slot="$1" snapshot
  [ -n "$slot" ] || return 1
  snapshot="$(curl -sS -m 4 "$MOP_BASE/slots/${slot}" 2>/dev/null || true)"
  [ -n "$snapshot" ] || return 1
  MOP_RESTORABLE_SNAPSHOT="$snapshot" python3 - "$slot" <<'PY' >/dev/null 2>&1
import json
import os
import sys

slot = int(sys.argv[1])
try:
    row = json.loads(os.environ["MOP_RESTORABLE_SNAPSHOT"])
except (KeyError, ValueError):
    raise SystemExit(1)
if row.get("slot") not in (slot, str(slot)):
    raise SystemExit(1)
if row.get("pr") is not None or row.get("issue") is not None:
    raise SystemExit(1)
if row.get("active_turn_state") != "inactive":
    raise SystemExit(1)
if row.get("dnd") is True:
    raise SystemExit(1)
if row.get("idle") is not True:
    raise SystemExit(1)
raise SystemExit(0)
PY
}

verify_assignment_pickup() {
  local slot="$1" issue="$2" pr="$3" branch="$4" head="$5" epoch="$6" prior_turn_id="${7:-}" accept_existing_turn="${8:-0}"
  local timeout="${PM_ASSIGN_PICKUP_TIMEOUT_SECONDS:-30}"
  local interval="${PM_ASSIGN_PICKUP_POLL_SECONDS:-1}"
  local deadline now snapshot result tuple_state turn_state turn_id
  deadline=$(( $(date +%s) + timeout ))
  while :; do
    snapshot="$(curl -sS -m 4 "$MOP_BASE/slots/${slot}" 2>/dev/null || true)"
    result="$(python3 - "$slot" "$issue" "$pr" "$branch" "$head" "$epoch" "$snapshot" <<'PY' 2>/dev/null || true
import json
import sys
slot, issue, pr, branch, head, epoch, raw = sys.argv[1:]
try:
    row = json.loads(raw)
except Exception:
    print("unreadable\tindeterminate\t")
    raise SystemExit
expected = {
    "slot": int(slot),
    "issue": int(issue),
    "pr": int(pr),
    "branch": branch,
    "head_sha": head,
    "assignment_epoch": int(epoch),
}
matches = row.get("occupied") is True and all(row.get(key) == value for key, value in expected.items())
print("%s\t%s\t%s" % (
    "match" if matches else "mismatch",
    row.get("active_turn_state") or "indeterminate",
    row.get("active_turn_id") or "",
))
PY
)"
    IFS="$(printf '\t')" read -r tuple_state turn_state turn_id <<< "$result"
    if [ "$tuple_state" = "mismatch" ]; then
      printf 'assignment_tuple_drift slot=%s epoch=%s snapshot=%s\n' "$slot" "$epoch" "$snapshot" >&2
      return 2
    fi
    if [ "$tuple_state" = "match" ] && [ "$turn_state" = "active" ] \
      && [ -n "$turn_id" ] \
      && { [ "$accept_existing_turn" = "1" ] || [ "$turn_id" != "$prior_turn_id" ]; }; then
      printf '%s' "$turn_id"
      return 0
    fi
    now="$(date +%s)"
    [ "$now" -lt "$deadline" ] || {
      printf 'assignment_pickup_timeout slot=%s epoch=%s prior_turn_id=%s final=%s\n' \
        "$slot" "$epoch" "${prior_turn_id:-none}" "$snapshot" >&2
      return 1
    }
    sleep "$interval"
  done
}

record_assignment_pickup_failure() {
  local slot="$1" issue="$2" pr="$3" branch="$4" head="$5" epoch="$6" delivery_ack="$7" reason="$8"
  upsert_obligation --kind rework_slot_idle --severity high --target-type pr --target-id "$pr" \
    --pr "$pr" --issue "$issue" --owner pm --horizon hourly --next-review-at "$(utc_plus_minutes 5)" \
    --title "PR #$pr assignment has no verified active turn" \
    --action "Re-read slot $slot tuple at epoch $epoch. If unchanged, redeliver the canonical current-head handoff once and verify a new active_turn_id; if drifted, epoch-rebind first." \
    --blocker "assignment_pickup_unverified" \
    --evidence "slot=$slot issue=$issue pr=$pr branch=$branch head=$head epoch=$epoch delivery_ack=${delivery_ack:-none} reason=$reason"
  record_event --source pm-transition --event assign_rework_pickup_unverified --target-type pr --target-id "$pr" \
    --pr "$pr" --issue "$issue" --slot "$slot" --head-sha "$head" --payload "branch=$branch" \
    --payload "epoch=$epoch" --payload "delivery_ack=${delivery_ack:-none}" --payload "reason=$reason"
  capacity_reconcile_trigger assignment_pickup_unverified "$slot"
}

enqueue_async_pickup_verification() {
  # Decoupled pickup verification (Rajiv directive 2026-08-15): delivery ack is
  # not proof of pickup, but the synchronous wait was blocking the PM thread.
  # Enqueue the same exact-tuple pickup check as a durable background outbox
  # entry so assign/assign-rework return immediately after the delivery ack
  # and the wake resolves the rework_slot_idle obligation once the slot's
  # UserPromptSubmit/agent_start flips the turn active.
  local slot="$1" issue="$2" pr="$3" branch="$4" head="$5" epoch="$6" prior_turn_id="${7:-}" accept_existing_turn="${8:-0}"
  postcommit_enqueue "assign-rework-pr-${pr}-pickup-verify" \
    verify_assignment_pickup_async "$slot" "$issue" "$pr" "$branch" "$head" "$epoch" "$prior_turn_id" "$accept_existing_turn"
}

verify_assignment_pickup_async() {
  local slot="$1" issue="$2" pr="$3" branch="$4" head="$5" epoch="$6" prior_turn_id="${7:-}" accept_existing_turn="${8:-0}"
  local timeout="${PM_ASSIGN_PICKUP_TIMEOUT_SECONDS:-30}"
  local interval="${PM_ASSIGN_PICKUP_POLL_SECONDS:-1}"
  local deadline now snapshot result tuple_state turn_state turn_id pickup_rc
  deadline=$(( $(date +%s) + timeout ))
  while :; do
    snapshot="$(curl -sS -m 4 "$MOP_BASE/slots/${slot}" 2>/dev/null || true)"
    result="$(python3 - "$slot" "$issue" "$pr" "$branch" "$head" "$epoch" "$snapshot" <<'PY' 2>/dev/null || true
import json
import sys
slot, issue, pr, branch, head, epoch, raw = sys.argv[1:]
try:
    row = json.loads(raw)
except Exception:
    print("unreadable\tindeterminate\t")
    raise SystemExit
expected = {
    "slot": int(slot),
    "issue": int(issue),
    "pr": int(pr),
    "branch": branch,
    "head_sha": head,
    "assignment_epoch": int(epoch),
}
matches = row.get("occupied") is True and all(row.get(key) == value for key, value in expected.items())
print("%s\t%s\t%s" % (
    "match" if matches else "mismatch",
    row.get("active_turn_state") or "indeterminate",
    row.get("active_turn_id") or "",
))
PY
)"
    IFS="$(printf '\t')" read -r tuple_state turn_state turn_id <<< "$result"
    if [ "$tuple_state" = "mismatch" ]; then
      printf 'assignment_tuple_drift slot=%s epoch=%s snapshot=%s\n' "$slot" "$epoch" "$snapshot" >&2
      record_assignment_pickup_failure "$slot" "$issue" "$pr" "$branch" "$head" "$epoch" "${ASSIGN_REWORK_DELIVERY_ACK:-none}" "async_tuple_drift"
      return 2
    fi
    if [ "$tuple_state" = "match" ] && [ "$turn_state" = "active" ] \
      && [ -n "$turn_id" ] \
      && { [ "$accept_existing_turn" = "1" ] || [ "$turn_id" != "$prior_turn_id" ]; }; then
      resolve_target_obligations --kind rework_slot_idle --target-type pr --target-id "$pr" --pr "$pr" \
        --reason "assignment_pickup_verified_async" --external-state "slot=$slot epoch=$epoch turn=$turn_id"
      printf 'PM_TRANSITION_PICKUP_VERIFIED_ASYNC slot=%s pr=%s issue=%s epoch=%s turn=%s\n' \
        "$slot" "$pr" "$issue" "$epoch" "$turn_id" >&2
      return 0
    fi
    now="$(date +%s)"
    [ "$now" -lt "$deadline" ] || {
      printf 'assignment_pickup_timeout slot=%s epoch=%s prior_turn_id=%s final=%s\n' \
        "$slot" "$epoch" "${prior_turn_id:-none}" "$snapshot" >&2
      record_assignment_pickup_failure "$slot" "$issue" "$pr" "$branch" "$head" "$epoch" "${ASSIGN_REWORK_DELIVERY_ACK:-none}" "async_timeout"
      return 1
    }
    sleep "$interval"
  done
}

run_with_slot_mutation_lock() {
  # Per-slot mutual exclusion for release_slot and other remaining compatibility
  # mutations. flock
  # is per open-file-description and auto-releases when the subshell exits on
  # ANY path (normal return, die, crash), so a failed mutation can never
  # wedge the slot with a stale lock.  The PM_SLOT_MUTATION_* env flags make
  # the lock reentrant within one mutation window while still blocking
  # concurrent same-slot writers in other processes.
  local slot="$1"; shift
  if [ "${PM_SLOT_MUTATION_LOCK:-0}" = "1" ] \
    && [ "${PM_SLOT_MUTATION_SLOT:-}" = "$slot" ]; then
    "$@"
    return $?
  fi
  local lock="/tmp/pm-transition-slot-mutation-${slot}.lock"
  if command -v flock >/dev/null 2>&1; then
    (
      flock -x 9 || exit 97
      PM_SLOT_MUTATION_LOCK=1 PM_SLOT_MUTATION_SLOT="$slot" "$@"
    ) 9>"$lock"
    local rc=$?
    [ "$rc" = "97" ] && die 1 "slot-mutation lock failed slot=$slot lock=$lock"
    return "$rc"
  fi
  local lockdir="${lock}.dir" waited=0 rc
  remove_stale_lockdir "$lockdir" 300
  while ! mkdir "$lockdir" 2>/dev/null; do
    remove_stale_lockdir "$lockdir" 300
    waited=$((waited + 1))
    [ "$waited" -ge 50 ] && {
      printf 'PM_TRANSITION_WARN slot_mutation_lock_timeout slot=%s\n' "$slot" >&2
      return 1
    }
    sleep 0.1
  done
  # Run in a subshell so die/exit inside the mutation cannot skip the
  # release; the lock dir is removed on every normal/exit path.
  (
    PM_SLOT_MUTATION_LOCK=1 PM_SLOT_MUTATION_SLOT="$slot" "$@"
  )
  rc=$?
  rmdir "$lockdir" 2>/dev/null || true
  return "$rc"
}

mop_epoch_payload() {
  local slot="$1" payload="$2" epoch
  epoch="$(mop_slot_epoch "$slot")" \
    || die 30 "cannot read authoritative assignment_epoch for slot $slot"
  printf '%s' "$payload" | jq --argjson expected_epoch "$epoch" \
    '. + {expected_epoch: $expected_epoch}' \
    || die 30 "failed to bind expected_epoch for slot $slot"
}

mop_authorized_assign_payload() {
  local payload="$1" repository_id="${MOP_REPOSITORY_ID:-}"
  if [ -z "$repository_id" ] && [ "$REPO" = "$MOP_PRIMARY_REPOSITORY" ]; then
    repository_id="$MOP_PRIMARY_REPOSITORY_ID"
  fi
  if ! [[ "$repository_id" =~ ^[1-9][0-9]*$ ]]; then
    printf 'PM_TRANSITION_ASSIGNMENT_REFUSED reason=immutable_repository_id_required repository=%s\n' \
      "$REPO" >&2
    return 1
  fi
  printf '%s' "$payload" | jq --arg repository_id "$repository_id" '
    if has("repository_id")
       and (.repository_id | tostring) != $repository_id
    then
      error("assignment repository_id conflicts with configured authority")
    else
      . + {repository_id: $repository_id}
    end
  ' || {
    printf 'PM_TRANSITION_ASSIGNMENT_REFUSED reason=repository_identity_binding_failed repository=%s repository_id=%s\n' \
      "$REPO" "$repository_id" >&2
    return 1
  }
}

legacy_assignment_writer_disabled() {
  local surface="${1:-unknown}"
  printf 'PM_TRANSITION_ASSIGNMENT_REFUSED surface=%s reason=legacy_shell_writer_disabled use=claim_slot\n' "$surface" >&2
  return 423
}

mop_assign_http() {
  local slot="${1:-unknown}"
  legacy_assignment_writer_disabled "mop-assign-slot-${slot}"
}

mop_slots_healthy() {
  curl -sS -m 4 "$MOP_BASE/slots" 2>/dev/null | python3 -c '
import json
import sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
slots = data.get("slots")
if not isinstance(slots, list) or not slots:
    sys.exit(1)
for slot in slots:
    if isinstance(slot, dict) and any(key in slot for key in ("slot", "id", "number")):
        sys.exit(0)
sys.exit(1)
'
}

slot_clone_lock_path() {
  local slot="$1"
  printf '%s/pm-clone-lock-%s' "$PM_CLONE_LOCK_DIR" "$slot"
}

slot_clone_locked() {
  local slot="$1" lock owner_pid stale
  lock="$(slot_clone_lock_path "$slot")"
  [ -f "$lock" ] || return 1

  owner_pid="$(sed -n 's/^pid: //p' "$lock" 2>/dev/null | head -1)"
  if ! [[ "$owner_pid" =~ ^[1-9][0-9]*$ ]]; then
    # A malformed lease cannot prove that checkout mutation is safe.
    return 0
  fi
  if kill -0 "$owner_pid" 2>/dev/null; then
    return 0
  fi

  stale="${lock}.stale-dead-pid-${owner_pid}-$(date -u +%Y%m%dT%H%M%SZ)"
  if mv "$lock" "$stale" 2>/dev/null; then
    echo "PM_CLONE_LOCK_STALE_RECOVERED slot=$slot pid=$owner_pid evidence=$stale" >&2
    return 1
  fi

  # If another process changed the lease while it was being checked, preserve
  # the fail-closed behavior and let the next transition retry.
  return 0
}

slot_checkout_mutation_lock_acquire() {
  local slot="$1" kind="$2" pr="${3:-}" issue="${4:-}" branch="${5:-}"
  local lock tmp token pid
  lock="$(slot_clone_lock_path "$slot")"
  mkdir -p "$PM_CLONE_LOCK_DIR" || return 1
  if slot_clone_locked "$slot"; then
    return 1
  fi
  pid="${BASHPID:-$$}"
  token="${pid}-${RANDOM}-${RANDOM}"
  tmp="${lock}.candidate.${token}"
  {
    printf 'kind: %s\n' "$kind"
    printf 'pid: %s\n' "$pid"
    printf 'token: %s\n' "$token"
    printf 'pr: %s\n' "$pr"
    printf 'issue: %s\n' "$issue"
    printf 'branch: %s\n' "$branch"
  } >"$tmp" || return 1
  if ! ln "$tmp" "$lock" 2>/dev/null; then
    rm -f "$tmp"
    return 1
  fi
  rm -f "$tmp"
  SLOT_CHECKOUT_MUTATION_LOCK_TOKEN="$token"
  return 0
}

slot_checkout_mutation_lock_release() {
  local slot="$1" lock token
  lock="$(slot_clone_lock_path "$slot")"
  token="${SLOT_CHECKOUT_MUTATION_LOCK_TOKEN:-}"
  [ -n "$token" ] || return 0
  [ "$(sed -n 's/^token: //p' "$lock" 2>/dev/null | head -1)" = "$token" ] || return 1
  rm -f "$lock"
  SLOT_CHECKOUT_MUTATION_LOCK_TOKEN=""
}

active_capture_lock_details() {
  local slot="$1" lock kind pid pr head branch checkout
  lock="$(slot_clone_lock_path "$slot")"
  [ -f "$lock" ] || return 1
  kind="$(sed -n 's/^kind: //p' "$lock" 2>/dev/null | head -1)"
  [ "$kind" = "capture-local-proof" ] || return 1
  pid="$(sed -n 's/^pid: //p' "$lock" 2>/dev/null | head -1)"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  pr="$(sed -n 's/^pr: //p' "$lock" 2>/dev/null | head -1)"
  head="$(sed -n 's/^headRefOid: //p' "$lock" 2>/dev/null | head -1)"
  branch="$(sed -n 's/^branch: //p' "$lock" 2>/dev/null | head -1)"
  checkout="$(sed -n 's/^checkout: //p' "$lock" 2>/dev/null | head -1)"
  printf 'lock=%s pid=%s pr=%s head=%s branch=%s checkout=%s' \
    "$lock" "$pid" "${pr:-unknown}" "${head:-unknown}" "${branch:-unknown}" "${checkout:-unknown}"
}

mop_slot_checkout_mutation_status() {
  local slot="$1" pr="${2:-}" issue="${3:-}" branch="${4:-}" expected_epoch="${5:-}"
  local snapshot
  snapshot="$(curl -sS -m 4 "$MOP_BASE/slots/${slot}" 2>/dev/null || true)"
  python3 - "$slot" "$pr" "$issue" "$branch" "$expected_epoch" "$snapshot" <<'PY' 2>/dev/null || printf 'unknown'
import json
import sys

slot, pr, issue, branch, expected_epoch, raw = sys.argv[1:]
try:
    row = json.loads(raw)
except Exception:
    print("unknown")
    raise SystemExit
if str(row.get("slot") or row.get("id") or row.get("number") or "") != slot:
    print("unknown")
    raise SystemExit
if row.get("occupied") is not True:
    print("free")
    raise SystemExit
expected = {
    "pr": pr,
    "issue": issue,
    "branch": branch,
}
for key, value in expected.items():
    if value and str(row.get(key) or "") != value:
        print("mismatch")
        raise SystemExit
if expected_epoch:
    try:
        if int(row.get("assignment_epoch")) != int(expected_epoch):
            print("epoch-mismatch")
            raise SystemExit
    except (TypeError, ValueError):
        print("epoch-mismatch")
        raise SystemExit
print("match")
PY
}

mop_slot_checkout_issue_only_release_status() {
  # Typed issue-only-release admission (issue-only MoP claim with an open PR
  # on the exact branch). The live occupied MoP tuple must carry pr=null and
  # the exact issue/branch/head/slot/inactive-turn/assignment_epoch of the
  # release target. The open PR is only evidence the branch has a PR; it is
  # never used to fabricate or rebind MoP PR ownership. Fail closed on any
  # non-null different PR, issue/branch/head/epoch drift, active turn, or
  # foreign owner.
  local slot="$1" issue="${2:-}" branch="${3:-}" expected_epoch="${4:-}" expected_head="${5:-}"
  local snapshot
  snapshot="$(curl -sS -m 4 "$MOP_BASE/slots/${slot}" 2>/dev/null || true)"
  python3 - "$slot" "$issue" "$branch" "$expected_epoch" "$expected_head" "$snapshot" <<'PY' 2>/dev/null || printf 'unknown'
import json
import sys

slot, issue, branch, expected_epoch, expected_head, raw = sys.argv[1:]
try:
    row = json.loads(raw)
except Exception:
    print("unknown")
    raise SystemExit
if str(row.get("slot") or row.get("id") or row.get("number") or "") != slot:
    print("unknown")
    raise SystemExit
if row.get("occupied") is not True:
    print("free")
    raise SystemExit
# The live tuple must be a genuine issue-only claim with an EXPLICIT pr=null
# field: an absent `pr` key is an unproven tuple and fails closed (it must
# never be treated as a proven null). A non-null PR owner is a foreign owner
# and fails closed even if it happens to match the resolved open PR number
# (MoP PR ownership must never be rebound here).
if "pr" not in row or row.get("pr") is not None:
    print("pr-owned")
    raise SystemExit
if not issue or str(row.get("issue") or "") != issue:
    print("mismatch")
    raise SystemExit
if not branch or str(row.get("branch") or "") != branch:
    print("mismatch")
    raise SystemExit
# Exact checkout/head binding: MoP head_sha must equal the release target head.
if not expected_head or str(row.get("head_sha") or "") != expected_head:
    print("head-mismatch")
    raise SystemExit
# Inactive turn is required: an active or indeterminate turn means the slot is
# mid-work and must never be released by an issue-only admission.
if str(row.get("active_turn_state") or "") != "inactive":
    print("active-turn")
    raise SystemExit
# Foreign owner guard: a MoP owner identity (agent/session) other than the
# release target refuses.
if row.get("owner") not in (None, ""):
    print("foreign-owner")
    raise SystemExit
if expected_epoch:
    try:
        if int(row.get("assignment_epoch")) != int(expected_epoch):
            print("epoch-mismatch")
            raise SystemExit
    except (TypeError, ValueError):
        print("epoch-mismatch")
        raise SystemExit
print("match")
PY
}

labels_include() {
  local labels="$1" needle="$2"
  printf '%s\n' "$labels" | tr ',' '\n' | grep -qx "$needle"
}

assert_pr_not_dependency_blocked() {
  local pr="$1" labels pr_json
  pr_json="$(pr_metadata_json "$pr" 2>/dev/null || true)"
  [ -n "$pr_json" ] || die 1 "cannot read PR #$pr labels through GraphQL or REST"
  labels="$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print(",".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
  if labels_include "$labels" "pm-blocked:dependency"; then
    die 44 "PR #$pr has an active dependency watch. Do not start or complete PM review; wait for PR_DEPENDENCY_UNBLOCKED_REQUIRED, then run pm-transition dependency-unblocked --pr $pr --proof <proof>. That transition rebases/retargets onto main, verifies the new exact head, and directly fires one label-gated CI+E2E wave; review may run in parallel and gates merge, not CI start."
  fi
}

verification_lease_path() {
  printf '%s/slot-%s.json\n' "$VERIFICATION_LEASE_DIR" "$1"
}

write_verification_lease() {
  local slot="$1" epoch="$2" issue="$3" pr="$4" branch="$5" head="$6" source_packet="$7" initial_handoff="$8" status="${9:-initial_delivery}"
  local path tmp
  mkdir -p "$VERIFICATION_LEASE_DIR" || return 1
  path="$(verification_lease_path "$slot")"
  [ ! -e "$path" ] || return 1
  tmp="${path}.$$"
  python3 - "$tmp" "$path" "$slot" "$epoch" "$issue" "$pr" "$branch" "$head" "$source_packet" "$initial_handoff" "$status" <<'PY' || return 1
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

tmp, path, slot, epoch, issue, pr, branch, head, source_packet, initial_handoff, status = sys.argv[1:12]
packet = json.loads(Path(source_packet).read_text(encoding="utf-8"))
digest = packet.get("commands_sha256")
if not isinstance(digest, str) or len(digest) != 64:
    raise SystemExit("verification packet has no valid commands_sha256")
payload = {
    "schema_version": 1,
    "slot": int(slot),
    "assignment_epoch": int(epoch),
    "issue": int(issue),
    "pr": int(pr),
    "branch": branch,
    "head_sha": head,
    "commands_sha256": digest,
    "source_packet": str(Path(source_packet).resolve()),
    "initial_handoff": str(Path(initial_handoff).resolve()),
    "status": status,
    "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
}
Path(tmp).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(tmp, path)
PY
}

set_verification_lease_status() {
  local slot="$1" epoch="$2" status="$3" path tmp
  path="$(verification_lease_path "$slot")"
  [ -f "$path" ] || return 1
  tmp="${path}.$$"
  python3 - "$path" "$tmp" "$epoch" "$status" <<'PY' || return 1
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

path, tmp, epoch, status = sys.argv[1:5]
data = json.loads(Path(path).read_text(encoding="utf-8"))
if data.get("assignment_epoch") != int(epoch):
    raise SystemExit("verification lease epoch mismatch")
data["status"] = status
data["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
Path(tmp).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(tmp, path)
PY
}

clear_verification_lease() {
  local slot="$1" epoch="$2" path
  path="$(verification_lease_path "$slot")"
  [ -f "$path" ] || return 0
  python3 - "$path" "$epoch" <<'PY'
import json
import sys
from pathlib import Path

path, epoch = Path(sys.argv[1]), int(sys.argv[2])
data = json.loads(path.read_text(encoding="utf-8"))
if data.get("assignment_epoch") != epoch:
    raise SystemExit("verification lease epoch mismatch")
path.unlink()
PY
}

release_reservation_gate() {
  # Pre-mutation reservation predicate for eligible non-DND slot releases.
  # Mirrors the kernel release-canary admission semantics: a release is
  # eligible only when the live MoP slot state reports dnd_active == False
  # ("dnd_active is not False" -> not eligible -> exempt).  Eligible releases
  # require an OPEN first_boundary_reservation obligation row matching the
  # exact first-boundary tuple (slot, assignment_epoch, pr|issue, branch,
  # 40-char head) before ANY checkout/MoP/label/owner mutation; without it the
  # release fails closed with PM_TRANSITION_BLOCKED
  # reason=missing_pre_mutation_reservation and NO surface is mutated.
  # Reservation rows are created by PM through the existing
  # `pm-ops.py obligation-upsert --kind first_boundary_reservation` writer with
  # --evidence assignment_epoch=N --evidence branch=<live branch> --evidence
  # head=<40-char live head>; this predicate only enforces and consumes them.
  # On success the matched row is bound in the RELEASE_RESERVATION_* globals
  # and consumed by release_slot() after the release is persisted.  Callers
  # that validate the gate before parking the checkout (release_target_slots) set
  # PM_RESERVATION_PREVALIDATED=1 so release_slot() revalidates the SAME bound
  # tuple immediately before the mutation instead of re-acquiring the gate.
  local slot="$1" expected_epoch="$2" snapshot verdict live_pr live_issue live_branch live_head
  RELEASE_RESERVATION_ID=""
  RELEASE_RESERVATION_TARGET_TYPE=""
  RELEASE_RESERVATION_TARGET_ID=""
  RELEASE_RESERVATION_PR=""
  RELEASE_RESERVATION_ISSUE=""
  RELEASE_RESERVATION_BRANCH=""
  RELEASE_RESERVATION_HEAD=""
  RELEASE_RESERVATION_EPOCH=""
  snapshot="$(curl -sS -m 4 "$MOP_BASE/slots/${slot}" 2>/dev/null || true)"
  [ -n "$snapshot" ] || {
    printf 'PM_TRANSITION_BLOCKED reason=release_reservation_unreadable slot=%s epoch=%s\n' "$slot" "$expected_epoch" >&2
    return 16
  }
  verdict="$(PM_RELEASE_SNAPSHOT="$snapshot" python3 - "$slot" "$expected_epoch" <<'PY'
import json
import os
import sqlite3
import sys
from pathlib import Path

slot = int(sys.argv[1])
expected_epoch = int(sys.argv[2])
try:
    row = json.loads(os.environ["PM_RELEASE_SNAPSHOT"])
except (KeyError, ValueError):
    print("unreadable")
    raise SystemExit(0)

# Canary admission semantics: only a slot whose live DND state is exactly
# False is eligible for the reservation requirement.  True or missing DND
# state is not eligible and proceeds without a reservation.
if row.get("dnd") is not False:
    print("exempt")
    raise SystemExit(0)

live_epoch = row.get("assignment_epoch")
if not isinstance(live_epoch, int) or live_epoch != expected_epoch:
    print("tuple_drift")
    raise SystemExit(0)

live_pr = row.get("pr")
live_issue = row.get("issue")
if not isinstance(live_pr, int) and not isinstance(live_issue, int):
    print("no_identity")
    raise SystemExit(0)

live_branch = row.get("branch")
live_head = row.get("head_sha")

db_path = os.environ.get("PM_OPS_DB") or str(
    Path.home() / ".claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db"
)
try:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    clauses = [
        "status='open'",
        "kind='first_boundary_reservation'",
        "COALESCE(slot,-1)=?",
        "CAST(json_extract(COALESCE(evidence_json,'{}'), '$.assignment_epoch') AS INTEGER)=?",
        "COALESCE(pr,-1)=COALESCE(?,-1)",
        "COALESCE(issue,-1)=COALESCE(?,-1)",
        "(pr IS NOT NULL OR issue IS NOT NULL)",
    ]
    params = [slot, expected_epoch, live_pr, live_issue]
    # The reservation must be bound to the live branch and 40-char head when
    # the live row carries them; a reservation missing either binding cannot
    # authorize the exact first-boundary tuple.
    if isinstance(live_branch, str) and live_branch:
        clauses.append("json_extract(COALESCE(evidence_json,'{}'), '$.branch')=?")
        params.append(live_branch)
    if isinstance(live_head, str) and len(live_head) == 40:
        clauses.append("json_extract(COALESCE(evidence_json,'{}'), '$.head')=?")
        params.append(live_head)
    found = con.execute(
        "SELECT id, target_type, target_id, pr, issue "
        "FROM obligations WHERE "
        + " AND ".join(clauses)
        + " ORDER BY id DESC LIMIT 1",
        params,
    ).fetchone()
except Exception:
    print("db_unreadable")
    raise SystemExit(0)
if found is None:
    # Distinguishing probe for the incident
    # first-boundary-reservation-evidence-ordering: no OPEN row satisfied the
    # json_extract tuple contract.  Report whether a reservation row is bound
    # to this slot/epoch/pr/issue tuple at all and why it cannot authorize:
    # absent, already consumed, or open with evidence the gate cannot read
    # (mangled writer output such as a JSON-key wrapper).  The release stays
    # blocked either way; the detail makes the operator's fix-once-retry-once
    # SOP machine-checkable.
    probe = con.execute(
        "SELECT id, status, evidence_json FROM obligations "
        "WHERE kind='first_boundary_reservation' "
        "AND COALESCE(slot,-1)=? "
        "AND COALESCE(pr,-1)=COALESCE(?,-1) "
        "AND COALESCE(issue,-1)=COALESCE(?,-1) "
        "ORDER BY id DESC LIMIT 1",
        (slot, live_pr, live_issue),
    ).fetchone()
    if probe is None:
        print("missing %s %s" % (live_pr, live_issue))
        raise SystemExit(0)
    try:
        ev = json.loads(probe["evidence_json"] or "{}")
    except Exception:
        ev = None
    # Mirrors the gate's own json_extract clauses above.
    readable = isinstance(ev, dict) and ev.get("assignment_epoch") == expected_epoch
    if readable and isinstance(live_branch, str) and live_branch:
        readable = ev.get("branch") == live_branch
    if readable and isinstance(live_head, str) and len(live_head) == 40:
        readable = ev.get("head") == live_head
    print("missing %s %s reservation=%s status=%s evidence_readable=%s" % (
        live_pr, live_issue, probe["id"], probe["status"],
        "yes" if readable else "no",
    ))
    raise SystemExit(0)
print("reservation|%s|%s|%s|%s|%s|%s|%s" % (
    found["id"],
    found["target_type"] or "",
    found["target_id"] or "",
    found["pr"] if found["pr"] is not None else "",
    found["issue"] if found["issue"] is not None else "",
    live_branch if isinstance(live_branch, str) else "",
    live_head if isinstance(live_head, str) else "",
))
PY
)"
  case "$verdict" in
    exempt)
      return 0
      ;;
    tuple_drift)
      printf 'PM_TRANSITION_WARN release_slot_deferred slot=%s reason=assignment_epoch_drift expected=%s\n' "$slot" "$expected_epoch" >&2
      return 13
      ;;
    unreadable|db_unreadable)
      printf 'PM_TRANSITION_BLOCKED reason=release_reservation_%s slot=%s epoch=%s\n' "$verdict" "$slot" "$expected_epoch" >&2
      return 16
      ;;
    no_identity)
      printf 'PM_TRANSITION_BLOCKED reason=release_reservation_tuple_missing slot=%s epoch=%s\n' "$slot" "$expected_epoch" >&2
      return 16
      ;;
    missing*)
      live_pr="$(printf '%s' "$verdict" | awk '{print $2}')"
      live_issue="$(printf '%s' "$verdict" | awk '{print $3}')"
      probe_detail="$(printf '%s' "$verdict" | awk '{for(i=4;i<=NF;i++) printf "%s%s", $i, (i<NF?" ":"")}')"
      printf 'PM_TRANSITION_BLOCKED reason=missing_pre_mutation_reservation slot=%s epoch=%s tuple=pr:%s,issue:%s%s\n' \
        "$slot" "$expected_epoch" "${live_pr:-none}" "${live_issue:-none}" \
        "${probe_detail:+ $probe_detail}" >&2
      return 16
      ;;
    reservation*)
      RELEASE_RESERVATION_ID="$(printf '%s' "$verdict" | awk -F'|' '{print $2}')"
      RELEASE_RESERVATION_TARGET_TYPE="$(printf '%s' "$verdict" | awk -F'|' '{print $3}')"
      RELEASE_RESERVATION_TARGET_ID="$(printf '%s' "$verdict" | awk -F'|' '{print $4}')"
      RELEASE_RESERVATION_PR="$(printf '%s' "$verdict" | awk -F'|' '{print $5}')"
      RELEASE_RESERVATION_ISSUE="$(printf '%s' "$verdict" | awk -F'|' '{print $6}')"
      RELEASE_RESERVATION_BRANCH="$(printf '%s' "$verdict" | awk -F'|' '{print $7}')"
      RELEASE_RESERVATION_HEAD="$(printf '%s' "$verdict" | awk -F'|' '{print $8}')"
      RELEASE_RESERVATION_EPOCH="$expected_epoch"
      printf 'PM_TRANSITION_RESERVATION_APPLIED slot=%s epoch=%s tuple=pr:%s,issue:%s reservation=%s\n' \
        "$slot" "$expected_epoch" "${RELEASE_RESERVATION_PR:-none}" "${RELEASE_RESERVATION_ISSUE:-none}" "$RELEASE_RESERVATION_ID" >&2
      return 0
      ;;
    *)
      printf 'PM_TRANSITION_BLOCKED reason=release_reservation_verdict_unreadable slot=%s epoch=%s verdict=%q\n' "$slot" "$expected_epoch" "$verdict" >&2
      return 16
      ;;
  esac
}

release_reservation_revalidate() {
  # Revalidates the SAME first-boundary reservation acquired by
  # release_reservation_gate BEFORE any mutation (release_target_slots) immediately before the MoP
  # release mutation.  It never re-acquires the gate: the bound reservation id
  # must still be OPEN and the live MoP tuple must still equal the bound
  # tuple; any drift fails closed with no surface mutated.
  local slot="$1" expected_epoch="$2" snapshot verdict live_pr live_issue
  snapshot="$(curl -sS -m 4 "$MOP_BASE/slots/${slot}" 2>/dev/null || true)"
  [ -n "$snapshot" ] || {
    printf 'PM_TRANSITION_BLOCKED reason=release_reservation_revalidation_unreadable slot=%s epoch=%s\n' "$slot" "$expected_epoch" >&2
    return 16
  }
  verdict="$(PM_RELEASE_SNAPSHOT="$snapshot" \
    RELEASE_RESERVATION_ID="$RELEASE_RESERVATION_ID" \
    RELEASE_RESERVATION_EPOCH="${RELEASE_RESERVATION_EPOCH:-}" \
    RELEASE_RESERVATION_PR="${RELEASE_RESERVATION_PR:-}" \
    RELEASE_RESERVATION_ISSUE="${RELEASE_RESERVATION_ISSUE:-}" \
    RELEASE_RESERVATION_BRANCH="${RELEASE_RESERVATION_BRANCH:-}" \
    RELEASE_RESERVATION_HEAD="${RELEASE_RESERVATION_HEAD:-}" \
    python3 - "$slot" "$expected_epoch" <<'PY'
import json
import os
import sqlite3
import sys
from pathlib import Path

slot = int(sys.argv[1])
expected_epoch = int(sys.argv[2])
try:
    row = json.loads(os.environ["PM_RELEASE_SNAPSHOT"])
except (KeyError, ValueError):
    print("unreadable")
    raise SystemExit(0)

res_id = os.environ.get("RELEASE_RESERVATION_ID", "")

def eq(bound, live):
    # An empty bound value is a wildcard; otherwise the live value must equal
    # the bound value exactly.
    if bound is None or bound == "":
        return True
    if live is None:
        return False
    return str(bound) == str(live)

if res_id == "":
    # The caller prevalidated an exact fresh issue-only claim tuple that
    # carries no first_boundary_reservation (fresh claims never create one).
    # Re-check the exact bound tuple immediately before the mutation FIRST
    # (epoch, slot, issue, inactive turn); drift refuses regardless of DND
    # state so a slot that became busy/occupied underneath us is never
    # released.  Otherwise prevalidation exempted this release (DND-active):
    # re-check the exemption immediately before the mutation; a slot that
    # became eligible requires the full reservation gate.
    if os.environ.get("PM_RELEASE_PREVALIDATION_MODE") == "review_cap_off_slot_release":
        # Off-slot review-cap release (Rajiv thread 1786794072.170389 ts
        # 1786796329.507399): the dispatch already froze the exact PR-owned
        # tuple, and the caller verified the full live tuple (slot, PR or
        # issue-only identity, branch, 40-char head, inactive turn, DND
        # inactive) before parking.  Re-check that SAME bound tuple
        # immediately before the MoP mutation; any drift or DND/active turn
        # refuses with no surface mutated.
        live_epoch = row.get("assignment_epoch")
        if not isinstance(live_epoch, int) or live_epoch != expected_epoch:
            print("drift")
            raise SystemExit(0)
        if row.get("slot") != slot:
            print("drift")
            raise SystemExit(0)
        live_pr = row.get("pr")
        live_issue = row.get("issue")
        bound_pr = os.environ.get("RELEASE_RESERVATION_PR", "")
        bound_issue = os.environ.get("RELEASE_RESERVATION_ISSUE", "")
        pr_ok = (bound_pr and live_pr is not None and str(live_pr) == bound_pr) or (
            not bound_pr and live_pr in (None, "")
        )
        issue_ok = bool(bound_issue) and live_issue is not None and str(live_issue) == bound_issue
        if not pr_ok or not issue_ok:
            print("drift")
            raise SystemExit(0)
        if not eq(os.environ.get("RELEASE_RESERVATION_BRANCH"), row.get("branch")) \
           or not eq(os.environ.get("RELEASE_RESERVATION_HEAD"), row.get("head_sha")):
            print("drift")
            raise SystemExit(0)
        if str(row.get("active_turn_state") or "").lower() not in {"", "inactive"}:
            print("drift")
            raise SystemExit(0)
        if row.get("dnd") is not False:
            print("drift")
            raise SystemExit(0)
        print("ok")
        raise SystemExit(0)
    if os.environ.get("PM_RELEASE_PREVALIDATION_MODE") == "fresh_claim_rollback":
        live_epoch = row.get("assignment_epoch")
        if not isinstance(live_epoch, int) or live_epoch != expected_epoch:
            print("drift")
            raise SystemExit(0)
        if row.get("slot") != slot:
            print("drift")
            raise SystemExit(0)
        if not eq(os.environ.get("RELEASE_RESERVATION_EPOCH"), str(live_epoch)) \
           or not eq(os.environ.get("RELEASE_RESERVATION_ISSUE"), row.get("issue")):
            print("drift")
            raise SystemExit(0)
        if str(row.get("active_turn_state") or "").lower() not in {"", "inactive"}:
            # An active turn may already be working; never release underneath
            # it even if epoch/issue still match.
            print("drift")
            raise SystemExit(0)
        print("ok")
        raise SystemExit(0)
    if row.get("dnd") is not False:
        print("exempt")
        raise SystemExit(0)
    print("missing %s %s" % (row.get("pr"), row.get("issue")))
    raise SystemExit(0)

live_epoch = row.get("assignment_epoch")
if not isinstance(live_epoch, int) or live_epoch != expected_epoch:
    print("drift")
    raise SystemExit(0)
if not eq(os.environ.get("RELEASE_RESERVATION_EPOCH"), str(live_epoch)) \
   or not eq(os.environ.get("RELEASE_RESERVATION_PR"), row.get("pr")) \
   or not eq(os.environ.get("RELEASE_RESERVATION_ISSUE"), row.get("issue")) \
   or not eq(os.environ.get("RELEASE_RESERVATION_BRANCH"), row.get("branch")) \
   or not eq(os.environ.get("RELEASE_RESERVATION_HEAD"), row.get("head_sha")):
    print("drift")
    raise SystemExit(0)

db_path = os.environ.get("PM_OPS_DB") or str(
    Path.home() / ".claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db"
)
try:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    found = con.execute(
        "SELECT status FROM obligations WHERE id=?", (res_id,)
    ).fetchone()
except Exception:
    print("db_unreadable")
    raise SystemExit(0)
if found is None or found["status"] != "open":
    print("consumed")
    raise SystemExit(0)
print("ok")
PY
)"
  case "$verdict" in
    ok|exempt)
      return 0
      ;;
    missing*)
      live_pr="$(printf '%s' "$verdict" | awk '{print $2}')"
      live_issue="$(printf '%s' "$verdict" | awk '{print $3}')"
      printf 'PM_TRANSITION_BLOCKED reason=missing_pre_mutation_reservation slot=%s epoch=%s tuple=pr:%s,issue:%s\n' \
        "$slot" "$expected_epoch" "${live_pr:-none}" "${live_issue:-none}" >&2
      return 16
      ;;
    drift)
      printf 'PM_TRANSITION_BLOCKED reason=release_reservation_revalidation_drift slot=%s epoch=%s reservation=%s\n' \
        "$slot" "$expected_epoch" "$RELEASE_RESERVATION_ID" >&2
      return 16
      ;;
    consumed)
      printf 'PM_TRANSITION_BLOCKED reason=release_reservation_revalidation_consumed slot=%s epoch=%s reservation=%s\n' \
        "$slot" "$expected_epoch" "$RELEASE_RESERVATION_ID" >&2
      return 16
      ;;
    *)
      printf 'PM_TRANSITION_BLOCKED reason=release_reservation_revalidation_failed slot=%s epoch=%s verdict=%q\n' \
        "$slot" "$expected_epoch" "$verdict" >&2
      return 16
      ;;
  esac
}

release_slot() {
  # Python owns the authoritative release lock, tuple validation, MoP CAS,
  # readback, and durable post-commit outbox. This shell symbol is retained
  # only as a compatibility call-through for existing PM consumers.
  release_slot_mutate "$@"
}

release_slot_mutate() {
  local slot="${1:-}" reason="${2:-}" expected_epoch="${3:-${PM_MUTATION_EXPECTED_EPOCH:-}}"
  local repository_id="${4:-}" issue="${5:-}" pr="${6:-}" branch="${7:-}" head_sha="${8:-}"
  [ -n "$reason" ] || { printf 'PM_TRANSITION_BLOCKED reason=release_slot_requires_reason slot=%s\n' "$slot" >&2; return 2; }
  [[ "$expected_epoch" =~ ^[0-9]+$ ]] || { printf 'PM_TRANSITION_BLOCKED reason=release_slot_requires_expected_epoch slot=%s\n' "$slot" >&2; return 2; }
  local python_bin="${CONTROL_PLANE_KERNEL_PYTHON:-}"
  [ -x "$python_bin" ] || python_bin="$(command -v python3 2>/dev/null || true)"
  [ -x "$python_bin" ] || return 70
  local -a release_args=(
    release-slot --slot "$slot" --expected-epoch "$expected_epoch" --reason "$reason"
    --repository-id "$repository_id" --issue "$issue" --pr "$pr"
    --branch "$branch" --head-sha "$head_sha"
    --mop-url "$MOP_BASE" --database "$CONTROL_PLANE_KERNEL_DATABASE"
    --capacity-control "$CAPACITY_CONTROL"
    --capacity-delay-seconds "$((SLOT_RELEASE_QUARANTINE_SECONDS + 1))"
  )
  local release_result release_rc decision
  if release_result="$("$python_bin" "$KERNEL_ASSIGNMENT_BOUNDARY" "${release_args[@]}")"; then
    release_rc=0
  else
    release_rc=$?
  fi
  if [ "$release_rc" -ne 0 ]; then
    printf '%s\n' "$release_result"
    return "$release_rc"
  fi
  decision="$(printf '%s' "$release_result" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("decision", ""))' 2>/dev/null || true)"
  case "$decision" in
    RELEASE_SLOT_COMMITTED|RELEASE_SLOT_COMMITTED_DEFERRED)
      # Capacity reconciliation is consumed by the Python release boundary.
      # This shell facade must not become a second writer or drain owner.
      :
      ;;
  esac
  printf '%s\n' "$release_result"
}

run_with_assign_rework_lock() {
  local pr="$1"; shift
  local lock="/tmp/pm-transition-assign-rework-pr-${pr}.lock"
  if command -v flock >/dev/null 2>&1; then
    (
      flock -x 9 || exit 97
      "$@"
    ) 9>"$lock"
    local rc=$?
    [ "$rc" = "97" ] && die 1 "assign-rework lock failed pr=$pr lock=$lock"
    return "$rc"
  fi

  local lockdir="${lock}.dir" waited=0 rc
  remove_stale_lockdir "$lockdir" 300
  while ! mkdir "$lockdir" 2>/dev/null; do
    waited=$((waited+1))
    if [ $((waited % 20)) = "0" ]; then
      remove_stale_lockdir "$lockdir" 300
    fi
    [ "$waited" -ge 200 ] && die 1 "assign-rework lock timeout pr=$pr lock=$lockdir"
    sleep 0.05
  done
  (
    lockdir_for_trap="$lockdir"
    trap 'rmdir "$lockdir_for_trap" 2>/dev/null || true' EXIT
    "$@"
  )
  rc=$?
  return "$rc"
}

assert_assign_rework_live_state() {
  local pr="$1" expected_head="$2" context="${3:-mutation}" allow_rescope_verification="${4:-0}"
  local pr_json state current_head labels blocker required_transition
  if [ -n "${PM_COMMAND_SNAPSHOT:-}" ] && [ -f "$PM_COMMAND_SNAPSHOT" ]; then
    # pre_assignment_mutation / preexisting_delivery are the ONE final
    # authoritative reread (single gh tuple read) immediately before the first
    # mutation; every other context reads the immutable command snapshot.
    case "$context" in
      pre_assignment_mutation|preexisting_delivery)
        # The ONE final authoritative reread returns the reread PR JSON so the
        # live-state checks below run against the reread tuple with zero extra
        # gh calls.
        pr_json="$(command_snapshot_reread_compare "$pr")" \
          || die 1 "assign_rework_live_state_unavailable pr=$pr context=$context"
        ;;
      *)
        pr_json="$(command_snapshot_pr_json "$pr")" \
          || die 1 "assign_rework_live_state_unavailable pr=$pr context=$context snapshot=missing"
        ;;
    esac
  else
    pr_json="$(gh pr view "$pr" --repo "$REPO" --json state,headRefOid,labels 2>/dev/null || true)"
    [ -n "$pr_json" ] || die 1 "assign_rework_live_state_unavailable pr=$pr context=$context"
  fi
  state="$(printf '%s' "$pr_json" | json_field state)"
  current_head="$(printf '%s' "$pr_json" | json_field headRefOid)"
  labels="$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print(",".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
  [ "$state" = "OPEN" ] \
    || die 42 "rework_assignment_state_changed pr=$pr context=$context state=$state required=OPEN"
  [ "$current_head" = "$expected_head" ] \
    || die 42 "rework_assignment_head_changed pr=$pr context=$context expected=$expected_head live=${current_head:-unknown}"
  if labels_include "$labels" "pm-state:blocked-rework"; then
    :
  elif [ "$allow_rescope_verification" = "1" ] \
      && labels_include "$labels" "pm-state:rescope-required"; then
    :
  else
    die 42 "rework_assignment_state_changed pr=$pr context=$context head=${current_head:0:10} required=pm-state:blocked-rework labels=${labels:-none}"
  fi
  for blocker in pm-blocked:pm-gate pm-blocked:dependency pm-blocked:cto; do
    labels_include "$labels" "$blocker" || continue
    if [ "$blocker" = "pm-blocked:cto" ] \
        && [ "$allow_rescope_verification" = "1" ] \
        && labels_include "$labels" "pm-state:rescope-required"; then
      continue
    fi
    case "$blocker" in
      pm-blocked:pm-gate) required_transition="resolve-pm-gate_then_reconcile-rework-obligation" ;;
      pm-blocked:dependency) required_transition="dependency-unblocked_then_reconcile-rework-obligation" ;;
      pm-blocked:cto) required_transition="rescope-decide_then_reconcile-rework-obligation" ;;
    esac
    die 42 "rework_assignment_blocked pr=$pr blocker=$blocker head=${current_head:0:10} context=$context required_transition=$required_transition"
  done
}

remove_stale_lockdir() {
  local lockdir="$1" max_age_s="${2:-300}" age_s
  [ -d "$lockdir" ] || return 0
  age_s="$(python3 - "$lockdir" <<'PY' 2>/dev/null || echo 0
import os
import sys
import time
try:
    print(int(time.time() - os.path.getmtime(sys.argv[1])))
except Exception:
    print(0)
PY
)"
  if [ "${age_s:-0}" -ge "$max_age_s" ]; then
    rmdir "$lockdir" 2>/dev/null || true
  fi
}

pending_session_age_clear_for_slot() {
  local slot="$1"
  python3 - "$slot" <<'PY'
import os
import sqlite3
import sys
from pathlib import Path

slot = sys.argv[1]
db_path = os.environ.get("PM_OPS_DB") or str(
    Path.home() / ".claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db"
)
try:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    row = con.execute(
        """
        SELECT id
        FROM obligations
        WHERE status='open'
          AND kind='session_age_clear'
          AND COALESCE(target_type,'')='slot'
          AND COALESCE(target_id,'')=?
          AND COALESCE(slot, ?) = ?
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """,
        (slot, int(slot), int(slot)),
    ).fetchone()
except Exception:
    raise SystemExit(1)
if not row:
    raise SystemExit(1)
print(row["id"])
PY
}

maybe_clear_released_slot() {
  local slot="$1" reason="$2" pr="${3:-}" issue="${4:-}" branch="${5:-}"
  [ "${PM_TRANSITION_DISABLE_RELEASE_CLEAR:-0}" = "1" ] && return 0
  [ -x "$MOP_CLEAR" ] || return 0
  [ -x "$PM_OPS" ] || return 0

  local obligation_id safe_reason stamp tmp proof rc
  obligation_id="$(pending_session_age_clear_for_slot "$slot" 2>/dev/null || true)"
  [ -n "$obligation_id" ] || return 0

  safe_reason="$(printf '%s' "$reason" | tr -cs 'A-Za-z0-9_.-' '_')"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  tmp="/tmp/pm-transition-slot-clear-${slot}-${safe_reason}-${stamp}.out"
  proof="/tmp/pm-transition-slot-clear-${slot}-${safe_reason}-${stamp}.json"

  if bash "$MOP_CLEAR" --source "pm-transition:${reason}" --require-terminal "$slot" >"$tmp" 2>&1; then
    if [ -f /tmp/mop-clear-slot-latest.json ]; then
      cp /tmp/mop-clear-slot-latest.json "$proof" 2>/dev/null || cp "$tmp" "$proof" 2>/dev/null || true
    else
      cp "$tmp" "$proof" 2>/dev/null || true
    fi
    python3 "$PM_OPS" obligation-resolve \
      --kind session_age_clear \
      --target-type slot \
      --target-id "$slot" \
      --reason "pm-transition release clear ($reason)" \
      --external-state "$proof" \
      >/dev/null 2>&1 || true
    python3 "$PM_OPS" sync --write --no-live --reason "pm-transition-release-clear" >/dev/null 2>&1 || true
    record_event --source pm-transition --event session_age_clear_consumed --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --payload "branch=$branch" --payload "reason=$reason" --payload "obligation=$obligation_id" --payload "proof=$proof"
    echo "PM_TRANSITION_SLOT_CLEAR slot=$slot status=executed obligation=$obligation_id reason=$reason proof=$proof" >&2
  else
    rc=$?
    cp "$tmp" "$proof" 2>/dev/null || true
    record_event --source pm-transition --event session_age_clear_release_clear_blocked --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --payload "branch=$branch" --payload "reason=$reason" --payload "obligation=$obligation_id" --payload "rc=$rc" --payload "proof=$proof"
    echo "PM_TRANSITION_SLOT_CLEAR slot=$slot status=blocked rc=$rc obligation=$obligation_id reason=$reason proof=$proof" >&2
  fi
  rm -f "$tmp" 2>/dev/null || true
}

trim_ws() {
  awk '{$1=$1; print}'
}

count_pr_sweep_actionable() {
  local log="$1" count
  [ -r "$log" ] || {
    printf '0\n'
    return 0
  }
  count="$(grep -Ec '^.* PR_[A-Z0-9_]*REQUIRED ' "$log" 2>/dev/null || true)"
  printf '%s\n' "${count:-0}"
}

count_pr_sweep_dispatch_blockers() {
  local log="$1" count
  [ -r "$log" ] || {
    printf '0\n'
    return 0
  }
  count="$(grep -Ec '(^|[[:space:]])PR_(READY_PROMOTION|PM_REVIEW|PM_REVIEW_COMPLETE|PM_REVIEW_CAPTURE_REQUIRED|PM_REVIEW_CAPTURE_COMPLETE|PM_REVIEW_CAPTURE_BYPASS|PM_REVIEW_SLOT_RELEASE|CAPTURE_LOCAL|CAPTURE_LOCAL_PASS_CI|CAPTURE_BEFORE_CI|CAPTURE_COMPLETE|CAPTURE_SLOT_RELEASE|CAPTURE_LABEL_RECONCILE|CAPTURE_FAILED|LOCAL_PREFLIGHT|CI_RERUN_AFTER_PREFLIGHT|CI_VERDICT_REWORK|CI_CLASSIFICATION|CI_WATCH_STUCK|CI_LABEL_RECONCILE|CI_BUDGET_EXCEEDED|REWORK_PACKET|REWORK_DISPATCH|ACTIVE_REWORK_IDLE|REVIEW_CIRCUIT_BREAKER|RESCOPE|CLEANUP_CLOSEOUT)_REQUIRED([[:space:]]|$)' "$log" 2>/dev/null || true)"
  printf '%s\n' "${count:-0}"
}

maybe_run_backlog_promoter_after_sweep() {
  local trigger="$1" sweep_log="$2" dry_run="${3:-0}" pr_sweep_log="${4:-}"
  local threshold="${PM_BACKLOG_PROMOTER_TODO_THRESHOLD:-10}"
  local skip_reason="hourly_ops_reminder_only"
  BACKLOG_PROMOTER_STATUS="hourly_reminder_only"
  BACKLOG_PROMOTER_SELECTED=0
  BACKLOG_PROMOTER_LOG="/tmp/pm-transition-${trigger}-backlog-promoter.log"
  BACKLOG_PROMOTER_SWEEP_LOG="/tmp/pm-transition-${trigger}-backlog-promoter-sweep.log"

  if [ -r "$sweep_log" ] && grep -Eq 'DISPATCH_WEDGE|capacity_repair_required|DISPATCH_SLOT_REPAIR_REQUIRED|SLOT_DRAIN_REQUIRED' "$sweep_log" 2>/dev/null; then
    skip_reason="capacity_repair_required"
    BACKLOG_PROMOTER_STATUS="capacity_repair_required"
  fi

  # Backlog promotion is no longer a side effect of capacity reconciliation.
  # Hourly ops emits a low-pool reminder when clean status:todo work drops below
  # the threshold, and PM explicitly runs Skill(pm-backlog-promoter), which first
  # performs Codex architecture review before applying any promotion.
  echo "BACKLOG_PROMOTER_SKIPPED trigger=$trigger reason=$skip_reason clean_todo_threshold=$threshold sweep_log=$sweep_log pr_sweep_log=${pr_sweep_log:-none} dry_run=$dry_run"
}

run_post_release_sweep() {
  local trigger="${1:-post-release}"
  # Post-release sweeps are durable outbox work: the transition enqueues the
  # sweep and the outbox drain (bounded background drain at exit, or the next
  # reconcile-capacity) executes it. The authoritative mutation is never held
  # hostage to a sweep's wall time.
  if [ "${PM_TRANSITION_NO_SWEEP:-0}" = "1" ]; then
    return 0
  fi
  postcommit_enqueue "post-release-sweep-${trigger}" run_post_release_sweep_locked "$trigger"
}

run_post_release_sweep_locked() {
  local trigger="${1:-post-release}"
  local sweep_log="/tmp/pm-transition-${trigger}-sweep.log"
  if [ "${PM_TRANSITION_NO_SWEEP:-0}" != "1" ] && [ -x "$SWEEP" ]; then
    if python3 "$LEGACY_SWEEP_GATE"; then
      bash "$SWEEP" --trigger="$trigger" >"$sweep_log" 2>&1 || true
    else
      bash "$SWEEP" --trigger="$trigger" --dry-run >"$sweep_log" 2>&1 || true
      record_event --source pm-transition --event legacy_post_release_sweep_shadowed \
        --target-type control-plane --target-id capacity --payload "trigger=$trigger" \
        --payload "assign_fill=${PM_CAPACITY_ASSIGN_FILL:-0}" \
        --payload "fleet_enable=${PM_CAPACITY_FLEET_ENABLE:-0}" --dedupe
    fi
    maybe_run_backlog_promoter_after_sweep "$trigger" "$sweep_log" 0 ""
  fi
}

# ---------------------------------------------------------------------------
# Transition latency repair primitives (CTO 2026-08-05, thread 1785921483.708289)
# ---------------------------------------------------------------------------

now_ms() {
  python3 -c 'import time; print(int(time.time() * 1000))'
}

PHASE_TIMINGS_ENABLED=1
PHASE_ACTIVE=""
PHASE_START_MS=0
PHASE_MS_SNAPSHOT=0
PHASE_MS_GUARD=0
PHASE_MS_CHECKOUT=0
PHASE_MS_GITHUB_MUTATION=0
PHASE_MS_VERIFY=0
PHASE_MS_POSTCOMMIT=0

phase_begin() {
  local name="$1"
  [ "$PHASE_TIMINGS_ENABLED" = "1" ] || return 0
  PHASE_START_MS="$(now_ms)"
  PHASE_ACTIVE="$name"
}

phase_end() {
  local name="$1" end_ms elapsed
  [ "$PHASE_TIMINGS_ENABLED" = "1" ] || return 0
  [ "$PHASE_ACTIVE" = "$name" ] || return 0
  end_ms="$(now_ms)"
  elapsed=$((end_ms - PHASE_START_MS))
  [ "$elapsed" -lt 0 ] && elapsed=0
  case "$name" in
    snapshot) PHASE_MS_SNAPSHOT="$elapsed" ;;
    guard) PHASE_MS_GUARD="$elapsed" ;;
    checkout) PHASE_MS_CHECKOUT="$elapsed" ;;
    github_mutation) PHASE_MS_GITHUB_MUTATION="$elapsed" ;;
    verify) PHASE_MS_VERIFY="$elapsed" ;;
    postcommit) PHASE_MS_POSTCOMMIT="$elapsed" ;;
  esac
  PHASE_ACTIVE=""
}

emit_phase_timings() {
  # Emits the six CTO phase timings and writes a durable sidecar JSON.
  local command="${1:-transition}" path timings
  local snapshot_ms="${PHASE_MS_SNAPSHOT:-0}" guard_ms="${PHASE_MS_GUARD:-0}"
  local checkout_ms="${PHASE_MS_CHECKOUT:-0}" github_mutation_ms="${PHASE_MS_GITHUB_MUTATION:-0}"
  local verify_ms="${PHASE_MS_VERIFY:-0}" postcommit_ms="${PHASE_MS_POSTCOMMIT:-0}"
  timings="snapshot_ms=${snapshot_ms},guard_ms=${guard_ms},checkout_ms=${checkout_ms},github_mutation_ms=${github_mutation_ms},verify_ms=${verify_ms},postcommit_ms=${postcommit_ms}"
  mkdir -p "$TRANSITION_TIMINGS_DIR" 2>/dev/null || true
  path="$TRANSITION_TIMINGS_DIR/${command}-$$-${RANDOM:-0}.json"
  PM_TRANSITION_TIMINGS_PATH="$path" \
    python3 - "$command" "$snapshot_ms" "$guard_ms" "$checkout_ms" "$github_mutation_ms" "$verify_ms" "$postcommit_ms" <<'PY' >/dev/null 2>&1 || true
import json
import os
import sys
from datetime import datetime, timezone

command, snapshot_ms, guard_ms, checkout_ms, github_mutation_ms, verify_ms, postcommit_ms = sys.argv[1:]
payload = {
    "schema_version": 1,
    "command": command,
    "emitted_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "timings_ms": {
        "snapshot_ms": int(snapshot_ms),
        "guard_ms": int(guard_ms),
        "checkout_ms": int(checkout_ms),
        "github_mutation_ms": int(github_mutation_ms),
        "verify_ms": int(verify_ms),
        "postcommit_ms": int(postcommit_ms),
    },
}
path = os.environ["PM_TRANSITION_TIMINGS_PATH"]
tmp = path + ".tmp." + str(os.getpid())
with open(tmp, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
os.replace(tmp, path)
PY
  printf '%s' "$timings"
}

# --- durable priority-rework index -------------------------------------------

priority_rework_index_state() {
  # Prints fresh | missing | stale. A corrupt or unparseable index is stale:
  # the guard fails closed rather than trusting a degraded file.
  local state
  [ -f "$PRIORITY_REWORK_INDEX" ] || {
    printf 'missing\n'
    return 0
  }
  state="$(PM_PRIORITY_REWORK_INDEX_PATH="$PRIORITY_REWORK_INDEX" \
    PM_PRIORITY_REWORK_INDEX_TTL_SECONDS="$PRIORITY_REWORK_INDEX_TTL_SECONDS" \
    python3 - "$PRIORITY_REWORK_INDEX" <<'PY' 2>/dev/null || printf 'stale\n'
import json
import os
import sys
from datetime import datetime, timezone

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    generated = data.get("generated_at")
    if not generated:
        raise ValueError("missing generated_at")
    if data.get("invalidated_at"):
        # A packet publish/revoke, PR-head, or blocking-state change landed
        # after this index was built; the guard must re-read the sweep
        # instead of trusting cached rows. Only the next refresh
        # (priority_rework_index_write) clears the invalidation.
        print("stale")
        raise SystemExit(0)
    age = (
        datetime.now(timezone.utc)
        - datetime.fromisoformat(generated.replace("Z", "+00:00"))
    ).total_seconds()
    ttl = int(os.environ.get("PM_PRIORITY_REWORK_INDEX_TTL_SECONDS") or "1800")
except Exception:
    print("stale")
    raise SystemExit(0)
if 0 <= age <= ttl:
    print("fresh")
else:
    print("stale")
PY
)"
  printf '%s\n' "${state:-stale}"
}

reconcile_pr_sweep_log_fresh() {
  # Returns 0 if the last reconcile-capacity PR sweep log is fresh enough to
  # reuse (bounded read-only cache). The PR sweep is the dominant reconcile
  # latency (full `gh pr list --json statusCheckRollup` + per-PR reads); a
  # short TTL lets repeated reconcile-capacity invocations (e.g. the drain
  # loop plus the fresh-assign guard refresh) share one sweep result instead
  # of re-running the ~20s scan each time. Only dry-run (read-only) sweep
  # logs are reusable: writable sweeps mutate GitHub/PM state and are never
  # cached. Fail closed: a stale/missing/corrupt log forces a live sweep.
  local log="${1:-/tmp/pm-transition-reconcile-capacity-pr-sweep.log}"
  local ttl="${PM_RECONCILE_PR_SWEEP_CACHE_TTL_SECONDS:-120}"
  local now age
  [ "$ttl" -gt 0 ] 2>/dev/null || return 1
  [ -f "$log" ] || return 1
  # A pending sweep request means a transition mutated state after this log
  # was written; the cache must not mask that invalidation.
  [ -f "$PRIORITY_REWORK_SWEEP_REQUEST" ] && return 1
  # The durable index carries the same invalidation: if a packet publish/
  # revoke, PR-head, or blocking-state change landed after the sweep, the
  # cached log is stale and a live sweep is required.
  [ "$(priority_rework_index_state)" = "fresh" ] || return 1
  now="$(date +%s)"
  age=$((now - $(stat -f %m "$log" 2>/dev/null || echo 0)))
  [ "$age" -ge 0 ] && [ "$age" -le "$ttl" ] || return 1
  # The log must carry the terminal sweep contract row; a partial/interrupted
  # sweep (or a log written by a different trigger) is never reused.
  grep -Eq ' PR_SWEEP_(ACTIONABLE|CLEAN) ' "$log" || return 1
  # Only the dry-run sweep log path is eligible. If a writable sweep ran, its
  # log went to the same path but must not be treated as cacheable.
  grep -Eq '\[DRY_RUN\]' "$log" || return 1
  return 0
}

priority_rework_index_write() {
  # $1 = sweep log path; remaining args = summary key=value pairs. Parses the
  # PR_*_REQUIRED row contract (same regex domain as capacity-control.py) and
  # persists a durable JSON index atomically.
  local sweep_log="$1"; shift
  [ -f "$sweep_log" ] || return 1
  local summary="$*"
  PRIORITY_REWORK_INDEX_PATH="$PRIORITY_REWORK_INDEX" \
    python3 - "$sweep_log" "$PRIORITY_REWORK_INDEX_TTL_SECONDS" "$summary" <<'PY'
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

log_path, ttl_s, summary = sys.argv[1:4]
rows = []
with open(log_path, encoding="utf-8", errors="replace") as handle:
    for line in handle:
        if not re.search(r"\bPR_[A-Z0-9_]+_REQUIRED\b", line):
            continue
        row = {
            "kind": None,
            "pr": None,
            "issue": None,
            "slot": None,
            "packet": None,
            "affected_test_plan": None,
            "head": None,
            "created_at": None,
            "detail": line.rstrip("\n"),
        }
        match = re.search(r"\b(PR_[A-Z0-9_]+_REQUIRED)\b", line)
        if match:
            row["kind"] = match.group(1)
        match = re.search(r"\bPR#(\d+)\b", line)
        if match:
            row["pr"] = int(match.group(1))
        match = re.search(r"\bissue=#(\d+)\b", line)
        if match:
            row["issue"] = int(match.group(1))
        match = re.search(r"\bslot:(\d+)\b", line)
        if match:
            row["slot"] = int(match.group(1))
        match = re.search(r"\bpacket=(\S+)", line)
        if match:
            row["packet"] = match.group(1)
        match = re.search(r"\b(?:affected_test_plan|plan)=(\S+)", line)
        if match:
            row["affected_test_plan"] = match.group(1)
        match = re.search(r"\bhead=([0-9a-f]{7,40})\b", line)
        if match:
            row["head"] = match.group(1)
        match = re.search(r"\bcreated_at=(\S+)", line)
        if match:
            row["created_at"] = match.group(1)
        rows.append(row)
payload = {
    "schema_version": 1,
    "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "ttl_seconds": int(ttl_s) if str(ttl_s).isdigit() else 1800,
    "summary": summary,
    "rows": rows,
}
path = Path(os.environ["PRIORITY_REWORK_INDEX_PATH"])
tmp = path.with_name(path.name + ".tmp." + str(os.getpid()))
tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(str(tmp), str(path))
PY
}

priority_rework_index_invalidate() {
  # Marks the durable priority-rework index stale after a packet publish/
  # revoke, PR-head, or blocking-state change so the fresh-assign guard
  # re-reads the sweep (via the next reconcile-capacity refresh) instead of
  # trusting cached rows. schema_version stays 1; "invalidated_at" is
  # additive and the next priority_rework_index_write refresh clears it.
  # Atomic temp + os.replace; missing or unparseable indexes are no-ops
  # (already stale/fail closed). Fail-soft: an unwritable index keeps the
  # TTL backstop and must never fail the transition that mutated the state.
  local reason="${1:-unknown}"
  [ -f "$PRIORITY_REWORK_INDEX" ] || return 0
  PRIORITY_REWORK_INDEX_PATH="$PRIORITY_REWORK_INDEX" \
    python3 - "$reason" <<'PY' 2>/dev/null || true
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

reason = sys.argv[1]
path = Path(os.environ["PRIORITY_REWORK_INDEX_PATH"])
try:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError("unexpected schema_version")
    generated = data.get("generated_at")
    if not generated:
        raise ValueError("missing generated_at")
    datetime.fromisoformat(generated.replace("Z", "+00:00"))
except (OSError, ValueError):
    # Same corrupt-index semantics as the refresh guard: never stamp over an
    # unparseable file; it stays stale for operator inspection.
    raise SystemExit(0)
data["invalidated_at"] = (
    datetime.now(timezone.utc)
    .replace(microsecond=0)
    .isoformat()
    .replace("+00:00", "Z")
)
data["invalidation_reason"] = reason
data["invalidation_version"] = int(data.get("invalidation_version") or 0) + 1
tmp = path.with_name(path.name + ".inv." + str(os.getpid()))
tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(str(tmp), str(path))
PY
  return 0
}

priority_rework_index_sweep_text() {
  # Reconstructs the sweep row lines from the local index so existing parsers
  # (capacity-control fresh-fill-guard, count_pr_sweep_*) run unchanged.
  [ -f "$PRIORITY_REWORK_INDEX" ] || return 0
  PRIORITY_REWORK_INDEX_PATH="$PRIORITY_REWORK_INDEX" python3 -c '
import json
import os
try:
    with open(os.environ["PRIORITY_REWORK_INDEX_PATH"], encoding="utf-8") as handle:
        data = json.load(handle)
except Exception:
    raise SystemExit(0)
for row in data.get("rows") or []:
    detail = row.get("detail")
    if detail:
        print(detail)
' 2>/dev/null || true
}

priority_rework_sweep_request_enqueue() {
  # DEDUPLICATED async sweep request: one pending marker; repeated transitions
  # never stack additional sweep requests.
  local reason="${1:-assign-guard}" issue="${2:-}" slot="${3:-}" now
  [ -n "$PRIORITY_REWORK_SWEEP_REQUEST" ] || return 0
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  PM_PRIORITY_REWORK_SWEEP_REQUEST="$PRIORITY_REWORK_SWEEP_REQUEST" \
    python3 - "$reason" "$issue" "$slot" "$now" <<'PY' >/dev/null 2>&1 || true
import json
import os
import sys
from pathlib import Path

reason, issue, slot, now = sys.argv[1:5]
path = Path(os.environ["PM_PRIORITY_REWORK_SWEEP_REQUEST"])
try:
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
except (OSError, ValueError):
    data = {}
if data.get("status") == "pending":
    sys.exit(0)
data.update({
    "schema_version": 1,
    "status": "pending",
    "reason": reason,
    "issue": int(issue) if str(issue).isdigit() else None,
    "slot": int(slot) if str(slot).isdigit() else None,
    "enqueued_at": now,
})
tmp = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(str(tmp), str(path))
PY
}

priority_rework_sweep_request_consume() {
  local reason
  [ -n "$PRIORITY_REWORK_SWEEP_REQUEST" ] || return 0
  [ -f "$PRIORITY_REWORK_SWEEP_REQUEST" ] || return 0
  reason="$(PM_PRIORITY_REWORK_SWEEP_REQUEST="$PRIORITY_REWORK_SWEEP_REQUEST" python3 -c '
import json
import os
try:
    data = json.load(open(os.environ["PM_PRIORITY_REWORK_SWEEP_REQUEST"], encoding="utf-8"))
    print(data.get("reason") or "unknown")
except Exception:
    print("unknown")
' 2>/dev/null || true)"
  echo "PRIORITY_REWORK_SWEEP_REQUEST_CONSUMED reason=${reason:-unknown} index=$PRIORITY_REWORK_INDEX"
  rm -f "$PRIORITY_REWORK_SWEEP_REQUEST"
}

priority_rework_index_refresh_from_sweep() {
  # Refreshes the durable priority-rework index from a fresh read-only PR sweep
  # so reconcile-capacity self-heals the fresh-assign guard in every engine
  # mode (the legacy reconcile block already refreshes; the V2/shadow branches
  # exec the engine and never did). The sweep stays dry-run: in V2 mode the
  # typed engine owns every mutation, so the shell-side refresh must never
  # trigger a second writable sweep. The index write keeps its atomic
  # temp + os.replace semantics; only priority_rework_index_write writes it.
  local sweep_log="/tmp/pm-transition-reconcile-capacity-pr-sweep.log"
  local sweep_rc=0
  local pr_actionable=0 pr_dispatch_blockers=0
  [ -x "$PR_SWEEP" ] || return 0
  # Fail closed on a corrupt index: never stamp a fresh generated_at over an
  # unparseable file (priority_rework_index_state must keep reporting stale).
  # Missing and TTL-stale indexes are healed; only an unreadable file stays
  # stale so the operator can inspect it before the guard releases.
  if [ -f "$PRIORITY_REWORK_INDEX" ] && ! PRIORITY_REWORK_INDEX_PATH="$PRIORITY_REWORK_INDEX" \
      python3 -c '
import json
import os
import sys
from datetime import datetime, timezone

try:
    with open(os.environ["PRIORITY_REWORK_INDEX_PATH"], encoding="utf-8") as handle:
        data = json.load(handle)
    generated = data.get("generated_at")
    if not generated:
        raise ValueError("missing generated_at")
    datetime.fromisoformat(generated.replace("Z", "+00:00"))
except Exception:
    sys.exit(1)
' 2>/dev/null; then
    echo "PRIORITY_REWORK_INDEX_CORRUPT index=$PRIORITY_REWORK_INDEX state=stale action=no_refresh fail_closed=1" >&2
    return 0
  fi
  # A failed sweep must never stamp a fresh index nor consume the pending
  # sweep request: the fresh-assign guard stays stale/RECONCILE_REQUIRED until
  # a sweep actually succeeds. Success = exit 0 AND a nonempty log carrying
  # the terminal PR_SWEEP_ACTIONABLE / PR_SWEEP_CLEAN row (the sweep contract
  # emits PR_SWEEP_FAILED with exit 2 on any failure).
  PR_SWEEP_WRITE_SENTINEL=1 bash "$PR_SWEEP" --trigger=reconcile-capacity --dry-run >"$sweep_log" 2>&1
  sweep_rc=$?
  if [ "$sweep_rc" -ne 0 ] || [ ! -s "$sweep_log" ] \
      || ! grep -Eq ' PR_SWEEP_(ACTIONABLE|CLEAN) ' "$sweep_log"; then
    echo "PRIORITY_REWORK_INDEX_SWEEP_FAILED rc=$sweep_rc index=$PRIORITY_REWORK_INDEX state=stale action=no_refresh request_consumed=no fail_closed=1" >&2
    return 0
  fi
  pr_actionable="$(count_pr_sweep_actionable "$sweep_log")"
  pr_dispatch_blockers="$(count_pr_sweep_dispatch_blockers "$sweep_log")"
  priority_rework_index_write "$sweep_log" "pr_actionable=${pr_actionable:-0} pr_dispatch_blockers=${pr_dispatch_blockers:-0}"
  priority_rework_sweep_request_consume
}

# --- durable post-commit outbox ----------------------------------------------

postcommit_enqueue() {
  # $1 = unique entry name (dedupe key)  $2 = function to invoke  args...
  local entry_name="$1" func="$2"; shift 2
  local path arg quoted
  [ -n "$entry_name" ] || return 1
  [ -n "$func" ] || return 1
  mkdir -p "$TRANSITION_OUTBOX_DIR" 2>/dev/null || return 1
  path="$TRANSITION_OUTBOX_DIR/${entry_name}.sh"
  [ -f "$path" ] && return 0
  {
    printf '# post-commit outbox entry: %s\n' "$entry_name"
    printf '%s\n' "$func"
    for arg in "$@"; do
      printf '%q\n' "$arg"
    done
  } >"${path}.tmp.$$" 2>/dev/null || return 1
  mv -f "${path}.tmp.$$" "$path" || return 1
  return 0
}

postcommit_drain() {
  # Executes durable post-commit entries fail-soft: a failed entry is retained
  # for the next drainer; success removes the entry. Returns 1 when any entry
  # failed so callers can surface the summary without rolling back anything.
  local limit="${1:-40}" entry funcname invocation drained=0 failed=0 retained=0 line first
  mkdir -p "$TRANSITION_OUTBOX_DIR" 2>/dev/null || true
  for entry in "$TRANSITION_OUTBOX_DIR"/*.sh; do
    [ -f "$entry" ] || continue
    [ "$drained" -lt "$limit" ] || break
    funcname=""
    invocation=""
    first=1
    while IFS= read -r line; do
      case "$line" in \#*) continue ;; esac
      if [ "$first" = "1" ]; then
        funcname="$line"
        first=0
      else
        invocation="${invocation}${invocation:+ }${line}"
      fi
    done <"$entry"
    if [ -z "$funcname" ]; then
      retained=$((retained+1))
      continue
    fi
    if eval "set -- $invocation" && "$funcname" "$@"; then
      rm -f "$entry"
      drained=$((drained+1))
    else
      failed=$((failed+1))
    fi
  done
  retained=$((retained + failed))
  echo "POSTCOMMIT_DRAIN drained=$drained failed=$failed retained=$retained outbox=$TRANSITION_OUTBOX_DIR"
  [ "$failed" -eq 0 ] || return 1
  return 0
}

assignment_outbox_drain() {
  # The Python assignment boundary owns projection and handoff retries. Keep
  # reconcile-capacity as the one existing automatic trigger; no second shell
  # delivery writer or obligation hop is introduced here.
  local python_bin="${CONTROL_PLANE_KERNEL_PYTHON:-}"
  if [ -z "$python_bin" ] || [ ! -x "$python_bin" ]; then
    python_bin="$(command -v python3 2>/dev/null || true)"
  fi
  [ -x "$python_bin" ] || return 1
  [ -f "$CONTROL_PLANE_KERNEL_DATABASE" ] || return 0
  PYTHONPATH="$CONTROL_PLANE_KERNEL_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    MOP_URL="$MOP_BASE" GH_REPO="$REPO" HANDOFF_DELIVER="$HANDOFF_DELIVER" \
    "$python_bin" - <<'PY'
import json
import os
from pathlib import Path

from scripts.pm.control_plane.assignment_boundary import (
    DEFAULT_DATABASE,
    GhAssignmentEffectsAdapter,
    drain_assignment_outbox,
)

database = Path(
    os.environ.get("CONTROL_PLANE_KERNEL_DATABASE") or str(DEFAULT_DATABASE)
)
adapter = GhAssignmentEffectsAdapter(
    os.environ["GH_REPO"],
    os.environ["HANDOFF_DELIVER"],
    mop_url=os.environ["MOP_URL"],
)
print("ASSIGNMENT_OUTBOX_DRAIN " + json.dumps(
    drain_assignment_outbox(database_path=database, effects_adapter=adapter),
    ensure_ascii=True,
    sort_keys=True,
))
PY
}

# --- command-scoped immutable snapshot ---------------------------------------

PM_COMMAND_SNAPSHOT=""
PM_COMMAND_SNAPSHOT_KEY=""

command_snapshot_path() {
  printf '%s/%s.json' "$TRANSITION_SNAPSHOT_DIR" "$1"
}

command_snapshot_begin() {
  # ONE immutable capture per command: a single gh pr view, a single gh issue
  # view (fail-soft), and one MoP slots list read. All pre-mutation guards read
  # this snapshot; exactly one final authoritative reread follows before the
  # first mutation.
  local command="$1"; shift
  local pr="" issue="" slot="" key pr_json="" issue_json="" mop_slots_json=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      --issue) issue="${2:-}"; shift 2 ;;
      --slot) slot="${2:-}"; shift 2 ;;
      *) die 2 "unknown snapshot arg $1" ;;
    esac
  done
  key="${command}"
  [ -n "$pr" ] && key="${key}-pr-${pr}"
  [ -n "$issue" ] && key="${key}-issue-${issue}"
  [ -n "$slot" ] && key="${key}-slot-${slot}"
  mkdir -p "$TRANSITION_SNAPSHOT_DIR" 2>/dev/null || true
  PM_COMMAND_SNAPSHOT="$(command_snapshot_path "$key")"
  PM_COMMAND_SNAPSHOT_KEY="$key"
  if [ -n "$pr" ]; then
    pr_json="$(gh pr view "$pr" --repo "$REPO" --json state,isDraft,headRefName,headRefOid,title,url,labels,mergeable,mergeStateStatus,body,closingIssuesReferences,updatedAt 2>/dev/null || true)"
    [ -n "$pr_json" ] || die 1 "cannot read PR #$pr"
  fi
  if [ -n "$issue" ]; then
    issue_json="$(gh issue view "$issue" --repo "$REPO" --json state,title,body,labels 2>/dev/null || true)"
  fi
  mop_slots_json="$(curl -sS -m 4 "$MOP_BASE/slots" 2>/dev/null || true)"
  COMMAND_SNAPSHOT_PR="$pr_json" COMMAND_SNAPSHOT_ISSUE="$issue_json" \
    COMMAND_SNAPSHOT_MOP="$mop_slots_json" \
    python3 - "$PM_COMMAND_SNAPSHOT" "$command" "$pr" "$issue" "$slot" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

path, command, pr, issue, slot = sys.argv[1:6]


def load(env_name):
    raw = os.environ.get(env_name) or ""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


payload = {
    "schema_version": 1,
    "command": command,
    "pr": int(pr) if pr else None,
    "issue": int(issue) if issue else None,
    "slot": int(slot) if slot else None,
    "captured_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "pr_json": load("COMMAND_SNAPSHOT_PR"),
    "issue_json": load("COMMAND_SNAPSHOT_ISSUE"),
    "mop_slots_json": load("COMMAND_SNAPSHOT_MOP") or {},
}
tmp = path + ".tmp." + str(os.getpid())
with open(tmp, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
os.replace(tmp, path)
PY
}

command_snapshot_pr_json() {
  local pr="${1:-}"
  [ -n "$PM_COMMAND_SNAPSHOT" ] && [ -f "$PM_COMMAND_SNAPSHOT" ] || return 1
  python3 - "$PM_COMMAND_SNAPSHOT" "$pr" <<'PY'
import json
import sys

path, pr = sys.argv[1:3]
try:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
except (OSError, ValueError):
    raise SystemExit(1)
row = data.get("pr_json")
if row is None:
    raise SystemExit(1)
if pr and row.get("number") is not None and str(row.get("number")) != str(pr):
    raise SystemExit(1)
print(json.dumps(row))
PY
}

command_snapshot_issue_json() {
  local issue="${1:-}"
  [ -n "$PM_COMMAND_SNAPSHOT" ] && [ -f "$PM_COMMAND_SNAPSHOT" ] || return 1
  python3 - "$PM_COMMAND_SNAPSHOT" "$issue" <<'PY'
import json
import sys

path, issue = sys.argv[1:3]
try:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
except (OSError, ValueError):
    raise SystemExit(1)
row = data.get("issue_json")
if row is None:
    raise SystemExit(1)
if issue and row.get("number") is not None and str(row.get("number")) != str(issue):
    raise SystemExit(1)
print(json.dumps(row))
PY
}

command_snapshot_mop_slots_json() {
  [ -n "$PM_COMMAND_SNAPSHOT" ] && [ -f "$PM_COMMAND_SNAPSHOT" ] || return 1
  python3 - "$PM_COMMAND_SNAPSHOT" <<'PY' 2>/dev/null || true
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        data = json.load(handle)
    print(json.dumps(data.get("mop_slots_json") or {}))
except Exception:
    raise SystemExit(1)
PY
}

command_snapshot_reread_compare() {
  # The ONE final authoritative reread: a single gh pr view (state/head/labels)
  # compared against the immutable snapshot. Any drift fails closed (exit 42)
  # before the first mutation. The mutation's GitHub dependencies are the PR
  # state/head/labels; the issue tuple is bound in the immutable snapshot and
  # MoP ownership is re-validated live by the mutation-time checks (epoch
  # binding, occupied/free re-checks). On success prints the reread PR JSON.
  local pr="$1" pr_json
  pr_json="$(gh pr view "$pr" --repo "$REPO" --json state,headRefOid,labels 2>/dev/null || true)"
  [ -n "$pr_json" ] || die 1 "snapshot reread cannot read PR #$pr"
  if SNAPSHOT_REREAD_PR="$pr_json" \
    command_snapshot_reread_compare_python "$pr" >/dev/null; then
    printf '%s' "$pr_json"
    return 0
  fi
  die 42 "snapshot_reread_drift pr=$pr"
}

command_snapshot_reread_compare_python() {
  # The reread comparison itself: snapshot vs reread PR tuple.
  SNAPSHOT_REREAD_PR="${SNAPSHOT_REREAD_PR:-}" \
    python3 - "$PM_COMMAND_SNAPSHOT" "${1:-}" <<'PY'
import json
import os
import sys

path, pr = sys.argv[1:3]
try:
    with open(path, encoding="utf-8") as handle:
        snapshot = json.load(handle)
except (OSError, ValueError) as exc:
    print(f"snapshot_reread_unreadable detail={exc}", file=sys.stderr)
    raise SystemExit(42)

pr_json = json.loads(os.environ.get("SNAPSHOT_REREAD_PR") or "null")
if not isinstance(pr_json, dict):
    print("snapshot_reread_unavailable pr_json=missing", file=sys.stderr)
    raise SystemExit(42)
snap_pr = snapshot.get("pr_json") or {}
diffs = []
for field in ("state", "headRefOid"):
    if snap_pr.get(field) != pr_json.get(field):
        diffs.append(f"{field}:{snap_pr.get(field)}->{pr_json.get(field)}")
snap_labels = {x.get("name") for x in snap_pr.get("labels") or [] if isinstance(x, dict)}
live_labels = {x.get("name") for x in pr_json.get("labels") or [] if isinstance(x, dict)}
if snap_labels != live_labels:
    diffs.append("labels")
if diffs:
    print(f"snapshot_reread_drift pr={pr} diffs={','.join(sorted(diffs))}", file=sys.stderr)
    raise SystemExit(42)
print("snapshot_reread_ok")
PY
}

command_snapshot_clear() {
  PM_COMMAND_SNAPSHOT=""
  PM_COMMAND_SNAPSHOT_KEY=""
}

slot_checkout_path() {
  case "$1" in
    1) printf '/Users/rajiv/Downloads/projects/heydonna-app-3001' ;;
    2) printf '/Users/rajiv/Downloads/projects/heydonna-app-3002' ;;
    3) printf '/Users/rajiv/Downloads/projects/heydonna-app-3003' ;;
    4) printf '/Users/rajiv/Downloads/projects/heydonna-app-3004' ;;
    *) return 1 ;;
  esac
}

slot_checkout_branch() {
  local slot="$1" path
  path="$(slot_checkout_path "$slot")" || return 1
  git -C "$path" rev-parse --abbrev-ref HEAD 2>/dev/null || true
}

slot_checkout_head() {
  local slot="$1" path
  path="$(slot_checkout_path "$slot")" || return 1
  git -C "$path" rev-parse HEAD 2>/dev/null || true
}

slot_checkout_matches_target_ref() {
  local slot="$1" branch="$2" expected_head="${3:-}" path current_branch current_head target_head=""
  [ -n "$branch" ] || return 1
  path="$(slot_checkout_path "$slot")" || return 1
  current_branch="$(slot_checkout_branch "$slot")"
  if [ "$current_branch" = "$branch" ]; then
    return 0
  fi
  [ "$current_branch" = "HEAD" ] || return 1
  current_head="$(slot_checkout_head "$slot")"
  [ -n "$current_head" ] || return 1
  if [ -n "$expected_head" ]; then
    [ "$current_head" = "$expected_head" ]
    return
  fi
  target_head="$(git -C "$path" rev-parse "refs/remotes/origin/$branch" 2>/dev/null || true)"
  [ -n "$target_head" ] || target_head="$(git -C "$path" rev-parse "refs/heads/$branch" 2>/dev/null || true)"
  [ -n "$target_head" ] && [ "$current_head" = "$target_head" ]
}

slot_checkout_content_clean() {
  # Content-identity guard for mutation gates (incident 8816).
  #
  # `git status --porcelain` can report CLEAN while tracked blobs on disk
  # differ from HEAD: tools mark generated paths with --skip-worktree /
  # --assume-unchanged, fsmonitor/optional-lock heuristics go stale in
  # mid-edit windows, and smudge/clean filters round-trip bytes. Porcelain
  # alone must never authorize an assignment, release, park, adoption, or
  # repro dispatch.
  #
  # This guard hashes every tracked file's on-disk bytes through the same
  # path-aware clean filters used at commit time and compares the resulting
  # blob against HEAD:<path>. The hash is content-derived, so it cannot be
  # fooled by the stat cache, racy-clean, fsmonitor, assume-unchanged, or
  # skip-worktree bits. Fails closed on any read error; submodules are
  # skipped because they are directory gitlinks, not file blobs.
  #
  # Batched execution (CTO 2026-08-08 thread 1786106923.655619): the original
  # per-file loop spawned ~3 git processes per tracked file (rev-parse +
  # cat-file -t + hash-object), measured ~1.93s per 50 files (~6 min for the
  # ~9k-file inventory). The batch computes the same verdict with three
  # batched git primitives: `git ls-files -z` (index-tracked inventory),
  # `git ls-tree -rz HEAD` (HEAD blob/type inventory), and
  # `git hash-object --stdin-paths` (on-disk blobs; applies the same
  # path-aware clean filters as the per-file --path form). A tracked path
  # containing a newline cannot ride the line-delimited hash-object stdin
  # list, so such inventories fall back to the proven per-file loop. Progress
  # lines go to stderr so a long guard phase never looks dead to the wrapper.
  local slot="$1" path tmp rc
  [ -n "$slot" ] || return 1
  path="$(slot_checkout_path "$slot")" || return 1
  printf 'PM_TRANSITION_CONTENT_GUARD phase=start slot=%s\n' "$slot" >&2
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/pm-content-guard.XXXXXX")" || {
    printf 'PM_TRANSITION_CONTENT_GUARD phase=tmp_failed slot=%s\n' "$slot" >&2
    return 1
  }
  if _pm_content_guard_batch "$slot" "$path" "$tmp"; then
    rc=0
  else
    rc=$?
  fi
  rm -rf -- "$tmp" 2>/dev/null || true
  return "$rc"
}

_pm_content_guard_batch() {
  # Batched verdict engine for slot_checkout_content_clean. Returns 0 when
  # every index-tracked blob path that exists in HEAD has on-disk bytes that
  # clean-rewrite to the HEAD blob; 1 otherwise. Index entries absent from
  # HEAD and gitlink (submodule) paths are excluded from hashing exactly like
  # the per-file loop's rev-parse/type lookup behavior.
  local slot="$1" path="$2" tmp="$3" nfiles t_start t_end
  t_start="$(now_ms)"
  if ! git -C "$path" ls-files -z >"$tmp/files" 2>/dev/null; then
    printf 'PM_TRANSITION_CONTENT_GUARD phase=inventory_failed slot=%s\n' "$slot" >&2
    return 1
  fi
  if ! git -C "$path" ls-tree -rz HEAD >"$tmp/head" 2>/dev/null; then
    printf 'PM_TRANSITION_CONTENT_GUARD phase=head_unreadable slot=%s\n' "$slot" >&2
    return 1
  fi
  # Stage 1: cross-reference index vs HEAD. Emits the blob hash list and the
  # matching HEAD blob map in index order; marks fallback for newline-named
  # paths. Index entries absent from HEAD are skipped, matching the per-file
  # loop exactly: `git rev-parse HEAD:$file` on such a path echoes the
  # argument as its error message, so the base loop's head_blob is non-empty
  # and the subsequent cat-file -t lookup (which also fails) routes the entry
  # to the non-blob `continue` — a staged-but-uncommitted file is never
  # hashed by the base guard either.
  if ! python3 - "$tmp" >"$tmp/hashlist" 2>"$tmp/guard-err" <<'PY'
import os
import sys

tmp = sys.argv[1]
files_raw = open(os.path.join(tmp, "files"), "rb").read()
head_raw = open(os.path.join(tmp, "head"), "rb").read()
index_paths = [p for p in files_raw.split(b"\0") if p]
for p in index_paths:
    if b"\n" in p:
        open(os.path.join(tmp, "fallback"), "w").close()
        sys.exit(0)
head_types = {}
head_blobs = {}
for entry in head_raw.split(b"\0"):
    if not entry:
        continue
    meta, sep, p = entry.partition(b"\t")
    if not sep:
        continue
    _mode, typ, sha = meta.split(b" ", 2)
    if typ == b"tree":
        continue
    head_types[p] = typ
    head_blobs[p] = sha
with open(os.path.join(tmp, "headmap"), "wb") as mf:
    for p in index_paths:
        if p not in head_types or head_types[p] != b"blob":
            continue
        sys.stdout.buffer.write(p + b"\n")
        mf.write(head_blobs[p] + b"\t" + p + b"\n")
sys.exit(0)
PY
  then
    printf 'PM_TRANSITION_CONTENT_GUARD phase=parse_failed slot=%s\n' "$slot" >&2
    return 1
  fi
  if [ -e "$tmp/fallback" ]; then
    printf 'PM_TRANSITION_CONTENT_GUARD phase=fallback_per_file slot=%s\n' "$slot" >&2
    _pm_content_guard_per_file "$slot" "$path"
    return $?
  fi
  if [ -s "$tmp/guard-err" ]; then
    printf 'PM_TRANSITION_CONTENT_GUARD phase=parse_failed slot=%s\n' "$slot" >&2
    return 1
  fi
  nfiles="$(wc -l <"$tmp/hashlist" 2>/dev/null || printf 0)"
  printf 'PM_TRANSITION_CONTENT_GUARD phase=hash files=%s slot=%s\n' "${nfiles:-0}" "$slot" >&2
  if ! git -C "$path" hash-object --stdin-paths <"$tmp/hashlist" >"$tmp/disk" 2>/dev/null; then
    printf 'PM_TRANSITION_CONTENT_GUARD phase=disk_hash_failed slot=%s\n' "$slot" >&2
    return 1
  fi
  # Stage 2: line-for-line disk-hash vs HEAD-blob comparison in index order.
  if ! python3 - "$tmp" 2>>"$tmp/guard-err" <<'PY'
import os
import sys

tmp = sys.argv[1]
with open(os.path.join(tmp, "disk"), "rb") as fh:
    disk = fh.read().split(b"\n")
with open(os.path.join(tmp, "headmap"), "rb") as fh:
    headmap = fh.read().split(b"\n")
if len(headmap) != len(disk):
    print("PM_TRANSITION_CONTENT_GUARD phase=compare_shape_mismatch", file=sys.stderr)
    sys.exit(1)
for i, line in enumerate(headmap):
    if not line:
        continue
    expected, sep, p = line.partition(b"\t")
    actual = disk[i]
    if actual != expected:
        print(
            "PM_TRANSITION_CONTENT_GUARD phase=content_mismatch file=%s"
            % p.decode("utf-8", "replace"),
            file=sys.stderr,
        )
        sys.exit(1)
sys.exit(0)
PY
  then
    printf 'PM_TRANSITION_CONTENT_GUARD phase=verdict_dirty slot=%s\n' "$slot" >&2
    return 1
  fi
  if [ -s "$tmp/guard-err" ]; then
    printf 'PM_TRANSITION_CONTENT_GUARD phase=compare_failed slot=%s\n' "$slot" >&2
    return 1
  fi
  t_end="$(now_ms)"
  printf 'PM_TRANSITION_CONTENT_GUARD phase=done verdict=clean files=%s elapsed_ms=%s slot=%s\n' \
    "${nfiles:-0}" "$((t_end - t_start))" "$slot" >&2
  return 0
}

_pm_content_guard_per_file() {
  # Proven per-file loop retained for inventories whose tracked paths contain
  # newlines (git hash-object --stdin-paths is line-delimited). Verdict
  # semantics are byte-identical to the pre-batching implementation; emits
  # incremental progress so long runs stay observable.
  local slot="$1" path="$2" file head_blob disk_blob head_type n=0
  while IFS= read -r -d '' file; do
    n=$((n + 1))
    if [ $((n % 500)) -eq 0 ]; then
      printf 'PM_TRANSITION_CONTENT_GUARD phase=fallback files=%s slot=%s\n' "$n" "$slot" >&2
    fi
    head_blob="$(git -C "$path" rev-parse "HEAD:$file" 2>/dev/null || true)"
    if [ -z "$head_blob" ]; then
      printf 'PM_TRANSITION_CONTENT_GUARD phase=fallback_fail reason=head_unreadable file=%q slot=%s\n' "$file" "$slot" >&2
      return 1
    fi
    head_type="$(git -C "$path" cat-file -t "$head_blob" 2>/dev/null || true)"
    [ "$head_type" = "blob" ] || continue
    disk_blob="$(git -C "$path" hash-object --path="$file" -- "$file" 2>/dev/null || true)"
    if [ -z "$disk_blob" ] || [ "$disk_blob" != "$head_blob" ]; then
      printf 'PM_TRANSITION_CONTENT_GUARD phase=fallback_fail reason=content_mismatch file=%q slot=%s\n' "$file" "$slot" >&2
      return 1
    fi
  done < <(git -C "$path" ls-files -z)
  printf 'PM_TRANSITION_CONTENT_GUARD phase=done verdict=clean mode=fallback files=%s slot=%s\n' "$n" "$slot" >&2
  return 0
}

slot_checkout_is_target_branch() {
  slot_checkout_matches_target_ref "$1" "$2"
}

prepare_slot_checkout_for_assignment_locked() {
  local slot="$1" pr="$2" issue="$3" branch="$4" head="$5"
  local path current_branch current_head remote_head
  path="$(slot_checkout_path "$slot")" || return 1
  [ -n "$branch" ] && [[ "$head" =~ ^[0-9a-f]{40}$ ]] || return 1
  { [ -z "$(git -C "$path" status --porcelain 2>/dev/null || printf unreadable)" ]; } || {
    record_event --source pm-transition --event slot_assignment_checkout_prepare_failed \
      --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --head-sha "$head" \
      --payload "branch=$branch" --payload "reason=checkout_dirty_or_unreadable" \
      --payload "detail=porcelain_or_content_guard_failed" --payload "action=no_assignment"
    return 1
  }
  if slot_checkout_matches_target_ref "$slot" "$branch" "$head" \
    && [ "$(slot_checkout_head "$slot")" = "$head" ]; then
    # Already on the target ref at the target head: the single authoritative
    # content-guard run validates the exact state the assignment proceeds
    # with. Batching repair 2026-08-08 (thread 1786106923.655619) runs the
    # content guard exactly ONCE per assignment: the pre-switch duplicate was
    # eliminated (the porcelain check plus git switch's own conflict refusal
    # cover the pre-mutation state; a switch that carries masked dirt is
    # caught by the post-switch guard below).
    if ! slot_checkout_content_clean "$slot"; then
      record_event --source pm-transition --event slot_assignment_checkout_prepare_failed \
        --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --head-sha "$head" \
        --payload "branch=$branch" --payload "reason=checkout_dirty_or_unreadable" \
        --payload "detail=porcelain_or_content_guard_failed" --payload "action=no_assignment"
      return 1
    fi
    return 0
  fi
  git -C "$path" fetch --quiet origin "refs/heads/${branch}:refs/remotes/origin/${branch}" >/dev/null 2>&1 || {
    record_event --source pm-transition --event slot_assignment_checkout_prepare_failed \
      --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --head-sha "$head" \
      --payload "branch=$branch" --payload "reason=remote_branch_fetch_failed" --payload "action=no_assignment"
    return 1
  }
  remote_head="$(git -C "$path" rev-parse "refs/remotes/origin/${branch}" 2>/dev/null || true)"
  [ "$remote_head" = "$head" ] || {
    record_event --source pm-transition --event slot_assignment_checkout_prepare_failed \
      --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --head-sha "$head" \
      --payload "branch=$branch" --payload "reason=remote_head_mismatch" --payload "remote_head=${remote_head:-unknown}" --payload "action=no_assignment"
    return 1
  }

  current_branch="$(slot_checkout_branch "$slot")"
  current_head="$(slot_checkout_head "$slot")"
  if git -C "$path" show-ref --verify --quiet "refs/heads/${branch}" \
    && [ "$(git -C "$path" rev-parse "refs/heads/${branch}" 2>/dev/null || true)" = "$head" ]; then
    git -C "$path" switch "$branch" >/dev/null 2>&1 || git -C "$path" switch --detach "$head" >/dev/null 2>&1 || return 1
  else
    git -C "$path" switch --detach "$head" >/dev/null 2>&1 || return 1
  fi
  if ! slot_checkout_matches_target_ref "$slot" "$branch" "$head" \
    || [ "$(slot_checkout_head "$slot")" != "$head" ] \
    || [ -n "$(git -C "$path" status --porcelain 2>/dev/null || printf unreadable)" ] \
    || ! slot_checkout_content_clean "$slot"; then
    record_event --source pm-transition --event slot_assignment_checkout_prepare_failed \
      --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --head-sha "$head" \
      --payload "branch=$branch" --payload "reason=post_switch_exact_head_check_failed" \
      --payload "detail=porcelain_or_content_guard_failed" --payload "action=no_assignment"
    return 1
  fi
  record_event --source pm-transition --event slot_assignment_checkout_prepared \
    --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --head-sha "$head" \
    --payload "branch=$branch" --payload "from_branch=${current_branch:-unknown}" --payload "from_head=${current_head:-unknown}" \
    --payload "to_branch=$(slot_checkout_branch "$slot")" --payload "action=continue_assignment"
}

notify_slot_checkout_parked() {
  local slot="$1" pr="$2" issue="$3" branch="$4" reason="$5" stash_ref="${6:-none}"
  local generated_cleanup="${7:-none}" convex_pids="${8:-none}"
  local msg="/tmp/pm-transition-release-slot-${slot}-${pr:-issue-${issue:-unknown}}.md" delivery="skipped"
  {
    printf 'PM -> slot %s: release cleanup completed.\n\n' "$slot"
    printf -- '- Released PR: #%s\n' "${pr:-none}"
    printf -- '- Released issue: #%s\n' "${issue:-none}"
    printf -- '- Prior branch: `%s`\n' "${branch:-unknown}"
    printf -- '- Reason: `%s`\n' "$reason"
    if [ -n "$convex_pids" ] && [ "$convex_pids" != "none" ]; then
      printf -- '- Stopped slot-scoped `convex dev` watcher PID(s): `%s`\n' "$convex_pids"
    else
      printf -- '- No slot-scoped `convex dev` watcher was running.\n'
    fi
    if [ "$generated_cleanup" = "restored" ]; then
      printf -- '- Restored generated-only drift under `convex/_generated/` from `HEAD`; it was not added to the stash.\n'
    fi
    if [ -n "$stash_ref" ] && [ "$stash_ref" != "none" ]; then
      printf -- '- Preserved worktree changes in stash: `%s`\n' "$stash_ref"
    else
      printf -- '- Worktree was clean; no stash was needed.\n'
    fi
    printf -- '- Checkout is now on latest `main` after `git pull --ff-only origin main`.\n'
    printf -- '- Instruction: remain on `main`; do not restore the stash or switch back to the prior branch unless PM reassigns work through the formal claim/`assign-rework` path.\n'
    printf -- '- Stale-message guard: if MoP already shows a newer assignment when this arrives, ignore this release notice and follow the newer formal handoff.\n'
  } > "$msg"
  if [ -x "$MESSAGE_SLOT" ]; then
    if bash "$MESSAGE_SLOT" "$slot" --file "$msg" --force --from PM >/tmp/pm-transition-release-message-slot-${slot}.log 2>&1; then
      delivery="delivered"
    else
      delivery="failed"
    fi
  fi
  record_event --source pm-transition --event slot_release_cleanup_notified --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --payload "branch=$branch" --payload "reason=$reason" --payload "stash_ref=${stash_ref:-none}" --payload "generated_cleanup=$generated_cleanup" --payload "convex_pids=$convex_pids" --payload "message=$msg" --payload "delivery=$delivery"
  [ "$delivery" != "failed" ] || printf 'PM_TRANSITION_WARN release_cleanup_message_failed slot=%s message=%s\n' "$slot" "$msg" >&2
}

slot_convex_dev_pids() {
  local path="$1" pid command cwd
  while read -r pid command; do
    [ -n "${pid:-}" ] || continue
    case "$command" in
      *convex*dev*) ;;
      *) continue ;;
    esac
    cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)"
    if [[ "$command" == *"$path"* ]] || [ "$cwd" = "$path" ]; then
      printf '%s\n' "$pid"
    fi
  done < <(ps -axo pid=,command= 2>/dev/null)
}

stop_slot_convex_watchers() {
  local slot="$1" path="$2" pr="$3" issue="$4" reason="$5" pids pid attempt remaining
  pids="$(slot_convex_dev_pids "$path" | awk 'NF && !seen[$0]++' | paste -sd, -)"
  [ -n "$pids" ] || { printf 'none'; return 0; }

  for pid in ${pids//,/ }; do
    kill -TERM "$pid" 2>/dev/null || true
  done
  for attempt in 1 2 3 4 5; do
    remaining=""
    for pid in ${pids//,/ }; do
      kill -0 "$pid" 2>/dev/null && remaining="${remaining}${remaining:+,}${pid}"
    done
    [ -z "$remaining" ] && break
    sleep 0.2
  done
  if [ -n "${remaining:-}" ]; then
    for pid in ${remaining//,/ }; do
      kill -KILL "$pid" 2>/dev/null || true
    done
    sleep 0.1
  fi
  remaining=""
  for pid in ${pids//,/ }; do
    kill -0 "$pid" 2>/dev/null && remaining="${remaining}${remaining:+,}${pid}"
  done
  if [ -n "$remaining" ]; then
    record_event --source pm-transition --event slot_convex_watchers_stop_failed --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --payload "reason=$reason" --payload "path=$path" --payload "pids=$pids" --payload "remaining=$remaining"
    kanban_flag PM_TRANSITION "slot_convex_watchers_stop_failed slot=$slot pr=${pr:-none} issue=${issue:-none} reason=$reason pids=$pids remaining=$remaining"
    return 11
  fi
  record_event --source pm-transition --event slot_convex_watchers_stopped --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --payload "reason=$reason" --payload "path=$path" --payload "pids=$pids"
  kanban_flag PM_TRANSITION "slot_convex_watchers_stopped slot=$slot pr=${pr:-none} issue=${issue:-none} reason=$reason pids=$pids"
  printf '%s' "$pids"
}

worktree_dirty_paths() {
  local path="$1"
  {
    git -C "$path" diff --name-only 2>/dev/null || true
    git -C "$path" diff --cached --name-only 2>/dev/null || true
    git -C "$path" ls-files --others --exclude-standard 2>/dev/null || true
  } | awk 'NF && !seen[$0]++'
}

worktree_generated_only_dirty() {
  local path="$1" paths
  paths="$(worktree_dirty_paths "$path")"
  [ -n "$paths" ] || return 1
  ! printf '%s\n' "$paths" | grep -Ev '^convex/_generated/' >/dev/null
}

worktree_unpushed_evidence() {
  local path="$1" current_branch="$2" pr="${3:-}" pr_json live_branch live_head local_head evidence remote_branch_ref
  if [[ "$pr" =~ ^[0-9]+$ ]]; then
    pr_json="$(gh pr view "$pr" --repo "$REPO" --json state,headRefName,headRefOid 2>/dev/null || true)"
    live_branch="$(printf '%s' "$pr_json" | json_field headRefName 2>/dev/null || true)"
    live_head="$(printf '%s' "$pr_json" | json_field headRefOid 2>/dev/null || true)"
    local_head="$(git -C "$path" rev-parse HEAD 2>/dev/null || true)"
    if [ -z "$live_branch" ] || [ -z "$live_head" ] || [ -z "$local_head" ]; then
      printf 'pr_head_authority_unavailable pr=%s branch=%s\n' "$pr" "$current_branch"
      return 0
    fi
    if [ "$local_head" = "$live_head" ]; then
      return 1
    fi
    if [ "$current_branch" != "$live_branch" ]; then
      printf 'pr_branch_mismatch pr=%s checkout=%s live=%s\n' "$pr" "$current_branch" "$live_branch"
      return 0
    fi
    if ! git -C "$path" cat-file -e "${live_head}^{commit}" 2>/dev/null; then
      git -C "$path" fetch --quiet origin "$live_branch" >/dev/null 2>&1 || {
        printf 'pr_head_fetch_failed pr=%s branch=%s live_head=%s\n' "$pr" "$live_branch" "$live_head"
        return 0
      }
    fi
    if git -C "$path" merge-base --is-ancestor "$local_head" "$live_head" >/dev/null 2>&1; then
      return 1
    fi
    printf 'local_head_not_contained_in_live_pr pr=%s local=%s live=%s\n' "$pr" "$local_head" "$live_head"
    return 0
  fi
  local_head="$(git -C "$path" rev-parse HEAD 2>/dev/null || true)"
  remote_branch_ref="refs/remotes/origin/${current_branch}"
  if [ -n "$local_head" ] \
    && git -C "$path" cat-file -e "${remote_branch_ref}^{commit}" 2>/dev/null \
    && git -C "$path" merge-base --is-ancestor "$local_head" "$remote_branch_ref" >/dev/null 2>&1; then
    return 1
  fi
  evidence="$(git -C "$path" log '@{u}..' --oneline 2>/dev/null | head -1)"
  [ -n "$evidence" ] || return 1
  printf '%s\n' "$evidence"
}

park_slot_checkout_to_main() (
  local slot="$1" branch="$2" reason="$3" pr="${4:-}" issue="${5:-}" path current_branch porcelain unpushed unpushed_pr stash_ref="" generated_cleanup="none" convex_pids="none" capture_lease park_mode="main"
  local authority_status expected_epoch="${PM_MUTATION_EXPECTED_EPOCH:-}" allow_free=0
  if capture_lease="$(active_capture_lock_details "$slot")"; then
    record_event --source pm-transition --event slot_checkout_park_deferred_capture_running --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --payload "branch=$branch" --payload "reason=$reason" --payload "$capture_lease" --dedupe
    printf 'PM_TRANSITION_WARN slot_checkout_park_deferred slot=%s reason=capture_local_running %s\n' "$slot" "$capture_lease" >&2
    return 12
  fi
  if ! slot_checkout_mutation_lock_acquire "$slot" park "$pr" "$issue" "$branch"; then
    record_event --source pm-transition --event slot_checkout_park_lock_busy --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --payload "branch=$branch" --payload "reason=$reason" --payload "action=no_checkout_mutation"
    printf 'PM_TRANSITION_WARN slot_checkout_park_blocked slot=%s reason=checkout_mutation_lock_busy\n' "$slot" >&2
    return 12
  fi
  trap 'slot_checkout_mutation_lock_release "$slot" >/dev/null 2>&1 || true' EXIT
  path="$(slot_checkout_path "$slot")" || return 1
  current_branch="$(slot_checkout_branch "$slot")"
  case "$current_branch" in
    "")
      return 0
      ;;
  esac
  if [ "$current_branch" != "main" ] && [ "$current_branch" != "master" ] \
    && [ -n "$branch" ] && ! slot_checkout_matches_target_ref "$slot" "$branch"; then
    return 0
  fi

  case "$reason" in
    drain-slot:free-*) allow_free=1 ;;
  esac
  authority_status="$(mop_slot_checkout_mutation_status "$slot" "$pr" "$issue" "$branch" "$expected_epoch")"
  if [ "$authority_status" != "match" ] && [ -n "$pr" ] && [ -n "$issue" ]; then
    # Typed issue-only-release admission: an open PR resolved from the exact
    # checkout branch must not fabricate MoP PR ownership. If the live MoP
    # tuple is a genuine issue-only claim (pr=null) with the exact
    # issue/branch/head/inactive-turn/assignment_epoch, the park is safe.
    checkout_head="$(slot_checkout_head "$slot" 2>/dev/null || true)"
    if [ -n "$checkout_head" ]; then
      issue_only_status="$(mop_slot_checkout_issue_only_release_status "$slot" "$issue" "$branch" "$expected_epoch" "$checkout_head")"
      if [ "$issue_only_status" = "match" ]; then
        authority_status="issue-only-match"
      fi
    fi
  fi
  if { [ "$allow_free" = "1" ] && [ "$authority_status" != "free" ]; } \
    || { [ "$allow_free" != "1" ] && [ "$authority_status" != "match" ] && [ "$authority_status" != "issue-only-match" ]; }; then
    record_event --source pm-transition --event slot_checkout_park_target_mismatch --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --payload "branch=$branch" --payload "reason=$reason" --payload "expected_epoch=${expected_epoch:-none}" --payload "authority_status=$authority_status" --payload "action=no_checkout_mutation"
    printf 'PM_TRANSITION_WARN slot_checkout_park_blocked slot=%s reason=authoritative_tuple_%s expected_pr=%s expected_issue=%s expected_branch=%s expected_epoch=%s\n' "$slot" "$authority_status" "${pr:-none}" "${issue:-none}" "${branch:-none}" "${expected_epoch:-none}" >&2
    return 13
  fi

  convex_pids="$(stop_slot_convex_watchers "$slot" "$path" "$pr" "$issue" "$reason")" \
    || return 11

  if worktree_generated_only_dirty "$path"; then
    if ! git -C "$path" restore --source=HEAD --staged --worktree -- convex/_generated >/dev/null 2>&1 \
      || ! git -C "$path" clean -fd -- convex/_generated >/dev/null 2>&1; then
      record_event --source pm-transition --event slot_generated_cleanup_failed --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --payload "branch=$current_branch" --payload "reason=$reason" --payload "path=$path"
      return 11
    fi
    generated_cleanup="restored"
    record_event --source pm-transition --event slot_generated_only_drift_restored --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --payload "branch=$current_branch" --payload "reason=$reason" --payload "path=$path"
    kanban_flag PM_TRANSITION "slot_generated_only_drift_restored slot=$slot pr=${pr:-none} issue=${issue:-none} branch=$current_branch reason=$reason"
  fi

  porcelain="$(git -C "$path" status --porcelain 2>/dev/null || true)"
  unpushed_pr="$pr"
  case "$current_branch" in
    main|master) unpushed_pr="" ;;
  esac
  if unpushed="$(worktree_unpushed_evidence "$path" "$current_branch" "$unpushed_pr")"; then
    record_event --source pm-transition --event slot_checkout_park_blocked --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --payload "branch=$current_branch" --payload "reason=$reason" --payload "dirty=$([ -n "$porcelain" ] && printf true || printf false)" --payload "unpushed=true" --payload "unpushed_evidence=$unpushed"
    kanban_flag PM_TRANSITION "slot_checkout_park_blocked slot=$slot pr=${pr:-none} issue=${issue:-none} branch=$current_branch reason=$reason dirty=$([ -n "$porcelain" ] && printf true || printf false) unpushed=$([ -n "$unpushed" ] && printf true || printf false)"
    return 11
  fi

  if [ -n "$porcelain" ] || ! slot_checkout_content_clean "$slot"; then
    if ! git -C "$path" stash push --include-untracked -m "pm-transition release slot-${slot} ${reason} ${branch:-unknown}" >/dev/null 2>&1; then
      record_event --source pm-transition --event slot_checkout_park_failed --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --payload "branch=$current_branch" --payload "reason=$reason" --payload "step=stash_worktree"
      return 11
    fi
    stash_ref="$(git -C "$path" stash list -1 --format='%gd@%H' 2>/dev/null || true)"
    [ -n "$stash_ref" ] || stash_ref="stash-created"
    record_event --source pm-transition --event slot_checkout_changes_stashed --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --payload "branch=$current_branch" --payload "reason=$reason" --payload "stash_ref=$stash_ref"
    kanban_flag PM_TRANSITION "slot_checkout_changes_stashed slot=$slot pr=${pr:-none} issue=${issue:-none} branch=$current_branch reason=$reason stash_ref=$stash_ref"
  fi

  if ! git -C "$path" fetch origin main >/dev/null 2>&1; then
    record_event --source pm-transition --event slot_checkout_park_failed --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --payload "branch=$current_branch" --payload "reason=$reason" --payload "step=fetch_origin_main"
    return 11
  fi
  if git -C "$path" switch main >/dev/null 2>&1 || git -C "$path" checkout main >/dev/null 2>&1; then
    if ! git -C "$path" pull --ff-only origin main >/dev/null 2>&1; then
      park_mode="detached-origin-main"
    fi
  else
    park_mode="detached-origin-main"
  fi
  if [ "$park_mode" = "detached-origin-main" ]; then
    if ! git -C "$path" switch --detach origin/main >/dev/null 2>&1 \
      || [ "$(git -C "$path" rev-parse HEAD 2>/dev/null || true)" != "$(git -C "$path" rev-parse origin/main 2>/dev/null || true)" ]; then
      record_event --source pm-transition --event slot_checkout_park_failed --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --payload "branch=$current_branch" --payload "reason=$reason" --payload "step=park_origin_main" --payload "stash_ref=${stash_ref:-none}"
      return 11
    fi
  fi
  record_event --source pm-transition --event slot_checkout_parked --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --payload "from_branch=$current_branch" --payload "to_branch=$park_mode" --payload "reason=$reason" --payload "stash_ref=${stash_ref:-none}"
  kanban_flag PM_TRANSITION "slot_checkout_parked slot=$slot pr=${pr:-none} issue=${issue:-none} from=$current_branch to=$park_mode reason=$reason stash_ref=${stash_ref:-none}"
  notify_slot_checkout_parked "$slot" "$pr" "$issue" "$branch" "$reason" "${stash_ref:-none}" "$generated_cleanup" "$convex_pids"
  return 0
)

phase_release_hold_reason() {
  # PM-owned review, rescope, capture, dependency, and CI phases never own a
  # dev slot. This helper remains for compatibility with older call sites but
  # intentionally authorizes no hold reason.
  return 1
}

ci_start_release_reason() {
  case "$1" in
    ci-start:*) return 0 ;;
    *) return 1 ;;
  esac
}

phase_release_task() {
  local reason="$1" pr="$2" issue="$3"
  case "$reason" in
    pm-review:*|slot-ready-pm-review)
      printf 'PM review phase PR #%s' "${pr:-unknown}"
      ;;
    rescope-pr)
      printf 'Rescope phase PR #%s' "${pr:-unknown}"
      ;;
    rescope-issue)
      printf 'Rescope phase issue #%s' "${issue:-unknown}"
      ;;
    *)
      printf 'Active PM phase'
      ;;
  esac
}

phase_mark_target_slot() {
  legacy_assignment_writer_disabled "phase-mark-target-slot"
  return 423
}
find_pr_by_branch() {
  local branch="$1"
  [ -n "$branch" ] || return 0
  gh pr list --repo "$REPO" --state open --head "$branch" \
    --json number,title,headRefName,headRefOid,isDraft,labels --limit 1 \
    --jq '.[0] // empty' 2>/dev/null || true
}

matching_mop_slots_for_target() {
  local pr="$1" issue="$2" branch="$3" mop_json=""
  # Command-scoped snapshot hook: ownership reads use the immutable MoP list
  # captured at snapshot time; no repeated mid-transition slot-list reads.
  if [ -n "${PM_COMMAND_SNAPSHOT:-}" ] && [ -f "$PM_COMMAND_SNAPSHOT" ]; then
    mop_json="$(command_snapshot_mop_slots_json)"
  else
    mop_json="$(curl -sS -m 4 "$MOP_BASE/slots" 2>/dev/null || true)"
  fi
  printf '%s' "$mop_json" | python3 -c '
import json
import sys

pr, issue, branch = sys.argv[1:4]
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

for slot in data.get("slots", []):
    if not slot.get("occupied"):
        continue
    sid = slot.get("slot") or slot.get("id") or slot.get("number")
    if sid is None:
        continue
    slot_pr = str(slot.get("pr") or "")
    slot_issue = str(slot.get("issue") or "")
    slot_branch = str(slot.get("branch") or "")
    matches = (
        bool(pr and slot_pr == pr)
        or bool(issue and slot_issue == issue)
        or bool(branch and slot_branch == branch)
    )
    if matches:
        print(int(sid))
' "$pr" "$issue" "$branch"
}

mop_slot_matches_target() {
  local requested_slot="$1" pr="$2" issue="$3" branch="$4" slot
  [ -n "$requested_slot" ] || return 1
  for slot in $(matching_mop_slots_for_target "$pr" "$issue" "$branch"); do
    [ "$slot" = "$requested_slot" ] && return 0
  done
  return 1
}

mop_slot_matches_operator_retraction_target() {
  local requested_slot="$1" pr="$2" issue="$3" branch="$4"
  [ -n "$requested_slot" ] || return 1
  curl -sS -m 4 "$MOP_BASE/slots" 2>/dev/null | python3 -c '
import json
import sys

requested_slot, pr, issue, branch = sys.argv[1:5]
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)

for slot in data.get("slots", []):
    sid = slot.get("slot") or slot.get("id") or slot.get("number")
    if str(sid or "") != requested_slot or not slot.get("occupied"):
        continue
    slot_pr = str(slot.get("pr") or "")
    slot_issue = str(slot.get("issue") or "")
    slot_branch = str(slot.get("branch") or "")
    if (
        slot_issue == issue
        and slot_branch == branch
        and slot_pr in {"", pr}
    ):
        sys.exit(0)
sys.exit(1)
' "$requested_slot" "$pr" "$issue" "$branch"
}

mop_slot_target_status() {
  local requested_slot="$1" pr="$2" issue="$3" branch="$4"
  [ -n "$requested_slot" ] || { printf 'unknown'; return 0; }
  curl -sS -m 4 "$MOP_BASE/slots" 2>/dev/null | python3 -c '
import json
import sys

requested_slot, pr, issue, branch = sys.argv[1:5]
try:
    data = json.load(sys.stdin)
except Exception:
    print("unknown")
    sys.exit(0)

for slot in data.get("slots", []):
    sid = slot.get("slot") or slot.get("id") or slot.get("number")
    if sid is None or str(sid) != str(requested_slot):
        continue
    if not slot.get("occupied"):
        print("free")
        sys.exit(0)
    slot_pr = str(slot.get("pr") or "")
    slot_issue = str(slot.get("issue") or "")
    slot_branch = str(slot.get("branch") or "")
    matches = (
        bool(pr and slot_pr == pr)
        or bool(issue and slot_issue == issue)
        or bool(branch and slot_branch == branch)
    )
    if matches:
        print("match")
    else:
        print("mismatch")
    sys.exit(0)

print("unknown")
' "$requested_slot" "$pr" "$issue" "$branch"
}

owner_slots_except_requested() {
  local owner_slots="$1" requested_slot="$2" slot conflicts=""
  for slot in $owner_slots; do
    [ -n "$slot" ] || continue
    [ "$slot" = "$requested_slot" ] && continue
    conflicts="${conflicts}${conflicts:+,}${slot}"
  done
  printf '%s' "$conflicts"
}

publish_rework_packet() {
  local pr="$1" issue="$2" head="$3" kind="$4" packet="$5"
  [ -x "$REWORK_PACKET_LEDGER" ] \
    || die 43 "rework packet ledger is not executable: $REWORK_PACKET_LEDGER"
  [ -f "$packet" ] || die 43 "rework packet file not found: $packet"
  python3 "$REWORK_PACKET_LEDGER" publish \
    --repo "$REPO" \
    --pr "$pr" \
    --issue "$issue" \
    --head "$head" \
    --kind "$kind" \
    --packet "$packet" || return 1
  # A packet published after the last index build changes what the sweep
  # resolver can recover; the cached index must never admit fresh fill.
  priority_rework_index_invalidate "packet_publish pr=$pr issue=$issue head=${head:0:10}"
}

fetch_durable_rework_packet() {
  local pr="$1" head="$2" output
  [ -x "$REWORK_PACKET_LEDGER" ] || return 1
  output="/tmp/slot-rework-${pr}-pr-comment-${head:0:12}.md"
  python3 "$REWORK_PACKET_LEDGER" fetch \
    --repo "$REPO" \
    --pr "$pr" \
    --head "$head" \
    --output "$output" \
    >/tmp/pm-rework-packet-fetch-${pr}.json 2>/tmp/pm-rework-packet-fetch-${pr}.err \
    || return 1
  [ -s "$output" ] || return 1
  printf '%s\n' "$output"
}

record_rework_packet_delivery() {
  local pr="$1" head="$2" slot="$3" packet="$4" ack="$5" digest
  [ -x "$REWORK_PACKET_LEDGER" ] || return 1
  digest="$(shasum -a 256 "$packet" 2>/dev/null | awk '{print $1}')"
  [ -n "$digest" ] || return 1
  python3 "$REWORK_PACKET_LEDGER" delivery \
    --repo "$REPO" \
    --pr "$pr" \
    --head "$head" \
    --slot "$slot" \
    --packet-id "$digest" \
    --ack "$ack" \
    >/tmp/pm-rework-packet-delivery-${pr}-${slot}.json 2>/tmp/pm-rework-packet-delivery-${pr}-${slot}.err
}

latest_rework_packet_for_pr() {
  local pr="$1" head="${2:-}" durable=""
  [ -n "$pr" ] || return 0
  if [ -n "$head" ]; then
    durable="$(fetch_durable_rework_packet "$pr" "$head" 2>/dev/null || true)"
    if [ -n "$durable" ]; then
      printf '%s\n' "$durable"
      return 0
    fi
  fi
  python3 - "$pr" "$head" <<'PY'
import glob
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

pr, head = sys.argv[1:3]
candidates = []


def add_candidate(raw: str, weight: int) -> None:
    path = Path(raw)
    if not path.is_file():
        return
    if not str(path).startswith(f"/tmp/slot-rework-{pr}-"):
        return
    if head:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        variants = {head, head[:12], head[:10], head[:8]}
        if not any(value and value in text for value in variants):
            return
    candidates.append((weight, path.stat().st_mtime, str(path)))


db = Path(
    os.environ.get(
        "PM_OPS_DB",
        str(Path.home() / ".claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db"),
    )
)
try:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT evidence_json, required_action, title, blocker
        FROM obligations
        WHERE status='open'
          AND pr=?
          AND kind IN ('ci_rework', 'rework', 'blocked_rework', 'codex_rework')
        ORDER BY updated_at DESC, id DESC
        """,
        (int(pr),),
    ).fetchall()
    for row in rows:
        texts = []
        for key in ("evidence_json", "required_action", "title", "blocker"):
            value = row[key]
            if value:
                texts.append(str(value))
        try:
            evidence = json.loads(row["evidence_json"] or "[]")
            if isinstance(evidence, dict):
                texts.extend(str(v) for v in evidence.values())
            elif isinstance(evidence, list):
                texts.extend(str(v) for v in evidence)
        except Exception:
            pass
        for text in texts:
            for match in re.findall(rf"/tmp/slot-rework-{re.escape(pr)}-[A-Za-z0-9_.-]+\.md", text):
                add_candidate(match, 10)
except Exception:
    pass

for path in glob.glob(f"/tmp/slot-rework-{pr}-*.md"):
    add_candidate(path, 0)

if candidates:
    print(sorted(candidates, reverse=True)[0][2])
PY
}

release_target_slots() {
  local pr="$1" issue="$2" branch="$3" reason="$4" extra_slot="${5:-}" keep_slot="${6:-}"
  local slots slot released include_extra_slot extra_slot_status cleanup_msg
  include_extra_slot=""
  if [ -n "$extra_slot" ]; then
    extra_slot_status="$(mop_slot_target_status "$extra_slot" "$pr" "$issue" "$branch")"
    case "$extra_slot_status" in
      match)
        include_extra_slot="$extra_slot"
        ;;
      free|mismatch)
        if { [ -n "$pr" ] && ! clear_slot_label_verified pr "$pr" "$extra_slot" "$reason"; } \
          || { [ -n "$issue" ] && ! clear_slot_label_verified issue "$issue" "$extra_slot" "$reason"; }; then
          record_event --source pm-transition --event slot_release_label_reconcile_failed --target-type slot --target-id "$extra_slot" --pr "$pr" --issue "$issue" --slot "$extra_slot" --payload "branch=$branch" --payload "reason=$reason" --payload "mop_status=$extra_slot_status" --payload "action=typed_reconcile_required"
          kanban_flag PM_TRANSITION "slot_release_label_reconcile_failed slot=$extra_slot pr=${pr:-none} issue=${issue:-none} reason=$reason mop_status=$extra_slot_status action=typed_reconcile_required"
          return 15
        fi
        record_event --source pm-transition --event stale_slot_label_ignored --target-type slot --target-id "$extra_slot" --pr "$pr" --issue "$issue" --slot "$extra_slot" --payload "branch=$branch" --payload "reason=$reason" --payload "mop_status=$extra_slot_status" --payload "action=label_removed_no_release"
        kanban_flag PM_TRANSITION "stale_slot_label_ignored slot=$extra_slot pr=${pr:-none} issue=${issue:-none} reason=$reason mop_status=$extra_slot_status action=label_removed_no_release"
        ;;
      *)
        record_event --source pm-transition --event slot_release_skipped_mop_unknown --target-type slot --target-id "$extra_slot" --pr "$pr" --issue "$issue" --slot "$extra_slot" --payload "branch=$branch" --payload "reason=$reason" --payload "mop_status=${extra_slot_status:-unknown}" --payload "action=no_release"
        kanban_flag PM_TRANSITION "slot_release_skipped_mop_unknown slot=$extra_slot pr=${pr:-none} issue=${issue:-none} reason=$reason action=no_release"
        ;;
    esac
  fi
  slots="$(
    {
      [ -n "$include_extra_slot" ] && printf '%s\n' "$include_extra_slot"
      matching_mop_slots_for_target "$pr" "$issue" "$branch"
    } | awk 'NF && !seen[$0]++'
  )"
  released=""
  # Caller-pinned epoch (release_issue_owner_for_pm_transition) is captured at
  # entry and restored after each slot so multi-slot releases never inherit a
  # sibling slot's pin.
  local entry_epoch="${PM_MUTATION_EXPECTED_EPOCH:-}"
  for slot in $slots; do
    [ -n "$keep_slot" ] && [ "$slot" = "$keep_slot" ] && continue
    local capture_lease
    if capture_lease="$(active_capture_lock_details "$slot")"; then
      record_event --source pm-transition --event slot_release_deferred_capture_running --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --payload "branch=$branch" --payload "reason=$reason" --payload "$capture_lease" --dedupe
      kanban_flag PM_TRANSITION "slot_release_deferred_capture_running slot=$slot pr=${pr:-none} issue=${issue:-none} reason=$reason $capture_lease"
      continue
    fi
    if phase_release_hold_reason "$reason" && slot_checkout_is_target_branch "$slot" "$branch"; then
      record_event --source pm-transition --event slot_release_deferred_phase_hold \
        --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" \
        --payload "branch=$branch" --payload "reason=$reason" \
        --payload "action=defer_until_release_boundary"
      continue
    fi
    # Pre-mutation reservation: acquire and validate the exact first-boundary
    # tuple BEFORE any checkout/MoP/label/owner mutation.  A rejected gate
    # leaves every surface unchanged (the PR-bound split was caused by parking
    # the checkout before the gate).  release_slot() revalidates this SAME
    # bound reservation and never re-acquires the gate.  The expected epoch is
    # the live slot epoch; a caller-pinned epoch (PM_MUTATION_EXPECTED_EPOCH)
    # that drifted from live fails closed before any mutation.
    local gate_epoch live_epoch
    live_epoch="$(mop_slot_epoch "$slot" 2>/dev/null || true)"
    if [ -n "$entry_epoch" ]; then
      if [ -z "$live_epoch" ] || [ "$entry_epoch" != "$live_epoch" ]; then
        record_event --source pm-transition --event slot_release_failed --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --payload "branch=$branch" --payload "reason=$reason" --payload "action=kept_labels" --payload "gate=reservation" --payload "gate_error=assignment_epoch_drift" --payload "planned=$entry_epoch" --payload "live=${live_epoch:-unavailable}"
        kanban_flag PM_TRANSITION "slot_release_failed slot=$slot pr=${pr:-none} issue=${issue:-none} reason=$reason action=kept_labels gate=reservation gate_error=assignment_epoch_drift"
        continue
      fi
    fi
    if [ -z "$live_epoch" ]; then
      record_event --source pm-transition --event slot_release_failed --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --payload "branch=$branch" --payload "reason=$reason" --payload "action=kept_labels" --payload "gate=reservation" --payload "gate_error=assignment_epoch_unavailable"
      kanban_flag PM_TRANSITION "slot_release_failed slot=$slot pr=${pr:-none} issue=${issue:-none} reason=$reason action=kept_labels gate=reservation gate_error=assignment_epoch_unavailable"
      continue
    fi
    gate_epoch="$live_epoch"
    local release_head="" release_head_json=""
    if [ -n "$pr" ]; then
      release_head_json="$(pr_metadata_json "$pr" || true)"
      release_head="$(printf '%s' "$release_head_json" | json_field headRefOid 2>/dev/null || true)"
      if ! [[ "$release_head" =~ ^[0-9a-f]{40}$ ]]; then
        record_event --source pm-transition --event slot_release_failed --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --payload "branch=$branch" --payload "reason=$reason" --payload "gate=release_intent" --payload "gate_error=head_unavailable"
        kanban_flag PM_TRANSITION "slot_release_failed slot=$slot pr=${pr:-none} issue=${issue:-none} reason=$reason gate=release_intent gate_error=head_unavailable"
        PM_MUTATION_EXPECTED_EPOCH="$entry_epoch"
        continue
      fi
    fi
    if ! release_slot "$slot" "$reason" "$gate_epoch" "$MOP_PRIMARY_REPOSITORY" "$issue" "$pr" "$branch" "$release_head"; then
      record_event --source pm-transition --event slot_release_failed --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --payload "branch=$branch" --payload "reason=$reason"
      kanban_flag PM_TRANSITION "slot_release_failed slot=$slot pr=${pr:-none} issue=${issue:-none} reason=$reason action=kept_labels"
      PM_MUTATION_EXPECTED_EPOCH="$entry_epoch"
      continue
    fi
    PM_MUTATION_EXPECTED_EPOCH="$entry_epoch"
    write_slot_release_quarantine "$slot" "$pr" "$issue" "$branch" "$reason"
    record_event --source pm-transition --event slot_released --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --payload "branch=$branch" --payload "reason=$reason"
    released="${released}${released:+ }${slot}"
  done
  printf '%s' "$released"
}

release_issue_owner_for_pm_transition() {
  local issue="$1" slot="${2:-}" branch="${3:-}" reason="$4" captured_epoch="${5:-}" explicit_pr="${6:-}"
  local expected_epoch live_epoch turn_state released_slots pr_json pr linked linked_issue live_pr
  [ -n "$slot" ] || { printf ''; return 0; }

  mop_slots_healthy \
    || die 13 "MoP unavailable before issue-level PM transition release issue=$issue slot=$slot reason=$reason"
  live_epoch="$(mop_slot_epoch "$slot")" \
    || die 30 "cannot read assignment_epoch for slot $slot"
  if [ -n "$captured_epoch" ]; then
    [[ "$captured_epoch" =~ ^[0-9]+$ ]] \
      || die 13 "invalid captured assignment_epoch issue=$issue slot=$slot epoch=$captured_epoch reason=$reason"
    [ "$live_epoch" = "$captured_epoch" ] \
      || die 13 "assignment_epoch drift before issue-level release issue=$issue slot=$slot captured_epoch=$captured_epoch live_epoch=$live_epoch reason=$reason"
    expected_epoch="$captured_epoch"
  else
    expected_epoch="$live_epoch"
  fi
  turn_state="$(mop_slot_turn_state "$slot")" \
    || die 13 "agent-turn authority unavailable for slot $slot"
  [ "$turn_state" = "inactive" ] \
    || die 13 "issue-level PM transition release blocked issue=$issue slot=$slot active_turn_state=$turn_state reason=$reason"

  pr=""
  if [ -n "$explicit_pr" ]; then
    # Typed PR-bound park/release: accept the LIVE PR-bound tuple as given.
    # No claim-rebind and no issue-only pr=none/main reinterpretation.
    need_num pr "$explicit_pr"
    pr="$explicit_pr"
    if [ -n "$branch" ]; then
      pr_json="$(find_pr_by_branch "$branch")"
      if [ -n "$pr_json" ]; then
        linked="$(printf '%s' "$pr_json" | json_field number 2>/dev/null || true)"
        [ "$linked" = "$pr" ] \
          || die 13 "issue-level PM transition branch/PR mismatch issue=$issue pr=$pr branch=$branch resolved_pr=${linked:-none} reason=$reason"
      fi
    fi
  elif [ -n "$branch" ]; then
    pr_json="$(find_pr_by_branch "$branch")"
    if [ -n "$pr_json" ]; then
      pr="$(printf '%s' "$pr_json" | json_field number 2>/dev/null || true)"
      [ -n "$pr" ] \
        || die 13 "issue-level PM transition found unreadable open PR branch=$branch issue=$issue reason=$reason"
      linked_issue="$(issue_from_pr "$pr")"
      if [ -n "$linked_issue" ] && [ "$linked_issue" != "$issue" ]; then
        die 13 "issue-level PM transition linked PR mismatch issue=$issue pr=$pr linked_issue=${linked_issue:-none} branch=$branch reason=$reason"
      fi
    else
      # Fail closed: a live PR-bound claim must never be released through the
      # issue-only pr=none/main projection.  The caller must pass the typed
      # PR-bound tuple (park-issue --pr) instead.
      live_pr="$(curl -sS -m 4 "$MOP_BASE/slots/${slot}" 2>/dev/null | python3 -c 'import json,sys
try:
    v = json.load(sys.stdin).get("pr")
except Exception:
    v = None
print(v if isinstance(v, int) else "")' 2>/dev/null || true)"
      [ -z "$live_pr" ] \
        || die 13 "issue-level PM transition live claim is PR-bound issue=$issue slot=$slot live_pr=$live_pr branch=$branch reason=$reason; pass the typed PR-bound tuple (park-issue --pr) instead of issue-only reinterpretation"
    fi
  fi

  PM_MUTATION_EXPECTED_EPOCH="$expected_epoch"
  released_slots="$(release_target_slots "$pr" "$issue" "$branch" "$reason" "$slot")"
  printf '%s\n' "$released_slots" | tr ' ' '\n' | grep -qx "$slot" \
    || die 13 "issue-level PM transition did not release owner issue=$issue slot=$slot epoch=$expected_epoch reason=$reason"
  assert_no_slot_owner_for_phase "$pr" "$issue" "$branch" "$reason"
  printf '%s' "$released_slots"
}

target_slot_owners_before_ci() {
  local pr="$1" issue="$2" branch="$3" owners="" label slot
  if [ -n "$pr" ]; then
    while IFS= read -r label; do
      [ -n "$label" ] || continue
      owners="${owners}${owners:+ }pr:$label"
    done < <(gh pr view "$pr" --repo "$REPO" --json labels --jq '.labels[].name | select(test("^slot:[1-4]$"))' 2>/dev/null || true)
  fi
  if [ -n "$issue" ]; then
    while IFS= read -r label; do
      [ -n "$label" ] || continue
      owners="${owners}${owners:+ }issue:$label"
    done < <(gh issue view "$issue" --repo "$REPO" --json labels --jq '.labels[].name | select(test("^slot:[1-4]$"))' 2>/dev/null || true)
  fi
  for slot in $(matching_mop_slots_for_target "$pr" "$issue" "$branch"); do
    owners="${owners}${owners:+ }mop:slot:${slot}"
  done
  printf '%s' "$owners"
}

assert_no_slot_owner_for_phase() {
  local pr="$1" issue="$2" branch="$3" phase="$4" owners
  mop_slots_healthy || die 1 "MoP unavailable while verifying slot release phase=$phase pr=${pr:-none} issue=${issue:-none}"
  owners="$(target_slot_owners_before_ci "$pr" "$issue" "$branch")"
  [ -z "$owners" ] || die 1 "slot_owner_present phase=$phase pr=${pr:-none} issue=${issue:-unknown} branch=${branch:-unknown} owners=$owners"
}

assert_no_slot_owner_before_ci() {
  assert_no_slot_owner_for_phase "$1" "$2" "$3" "ci-start"
}

notify_slot_released_for_ci() {
  local slot="$1" pr="$2" issue="$3" branch="$4" reason="$5" msg="/tmp/pm-transition-ci-release-slot-${slot}-pr-${pr}.md"
  [ -n "$slot" ] || return 0
  {
    printf 'PM -> slot %s: PR #%s released for label-gated CI.\n\n' "$slot" "$pr"
    printf -- '- Issue: #%s\n' "${issue:-unknown}"
    printf -- '- Branch: `%s`\n' "${branch:-unknown}"
    printf -- '- Reason: %s\n' "$reason"
    printf -- '- Hold pushes unless PM reassigns rework on this PR.\n'
  } > "$msg"
  if [ -x "$MESSAGE_SLOT" ]; then
    bash "$MESSAGE_SLOT" "$slot" --file "$msg" --force >/dev/null 2>&1 || true
  fi
  record_event --source pm-transition --event slot_notified_released_for_ci --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --payload "branch=$branch" --payload "reason=$reason" --payload "message=$msg" --dedupe
}

release_target_before_ci_start() {
  local pr="$1" issue="$2" branch="$3" reason="$4" explicit_slot="${5:-}" released slot
  released="$(release_target_slots "$pr" "$issue" "$branch" "ci-start:${reason}" "$explicit_slot")"
  for slot in $released; do
    notify_slot_released_for_ci "$slot" "$pr" "$issue" "$branch" "$reason"
  done
  assert_no_slot_owner_before_ci "$pr" "$issue" "$branch"
  record_event --source pm-transition --event slot_released_before_ci_start --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --payload "branch=$branch" --payload "reason=$reason" --payload "released_slots=${released:-none}" --dedupe
  printf '%s' "$released"
}

capture_release_target_before_ci_start() {
  local result_var="$1"
  shift
  local released rc

  released="$(release_target_before_ci_start "$@")"
  rc=$?
  [ "$rc" -eq 0 ] || return "$rc"
  printf -v "$result_var" '%s' "$released"
}


pm_review_meta_file() {
  local pr="$1"
  printf '/tmp/pm-review-pending-%s.json' "$pr"
}

pm_review_marker_path() {
  local pr="$1" head="$2"
  printf '/tmp/pm-claude-code-review-%s-%s.md' "$pr" "$head"
}

family2_pm_review_wait() {
  local pr="$1" issue="$2" slot="$3" head="$4" expected_epoch="$5" marker python_bin output rc
  python_bin="${CONTROL_PLANE_KERNEL_PYTHON:-}"
  [ -x "$python_bin" ] || die 49 "family2_python_boundary_required path=$python_bin"
  marker="${PM_REVIEW_EVIDENCE:-$(pm_review_marker_path "$pr" "$head")}"
  output="$({
    cd "$CONTROL_PLANE_KERNEL_ROOT" || exit 49
    GH_REPO="$REPO" MOP_HOST="$MOP_BASE" \
      CONTROL_PLANE_KERNEL_DATABASE="$CONTROL_PLANE_KERNEL_DATABASE" \
      PYTHONPATH="$CONTROL_PLANE_KERNEL_ROOT" \
      "$python_bin" -m scripts.pm.control_plane.family2_boundary \
        --transition-type pm_review --slot "$slot" --issue "$issue" --pr "$pr" \
        --review-evidence "$marker" --non-ci-hold
  })"
  rc=$?
  printf '%s\n' "$output"
  case "$rc" in
    0) printf 'PM_TRANSITION_OK command=block-pr reason=pm-review-wait family2=pm_review non_ci_hold=1 pr=%s slot=%s epoch=%s head=%s\n' "$pr" "$slot" "$expected_epoch" "$head"; return 0 ;;
    23) printf 'PM_TRANSITION_DEFERRED command=block-pr reason=pm-review-wait family2=pm_review non_ci_hold=1 pr=%s slot=%s epoch=%s head=%s\n' "$pr" "$slot" "$expected_epoch" "$head"; return 23 ;;
    *) die 13 "family2 pm-review-wait refused pr=$pr slot=$slot head=$head" ;;
  esac
}

family2_slot_ready() {
  local event="$1" python_bin output rc
  [ -f "$event" ] || die 2 "slot-ready event file not found: $event"
  python_bin="${CONTROL_PLANE_KERNEL_PYTHON:-}"
  [ -x "$python_bin" ] || die 49 "family2_python_boundary_required path=$python_bin"
  output="$({
    cd "$CONTROL_PLANE_KERNEL_ROOT" || exit 49
    GH_REPO="$REPO" MOP_HOST="$MOP_BASE" \
      CONTROL_PLANE_KERNEL_DATABASE="$CONTROL_PLANE_KERNEL_DATABASE" \
      PYTHONPATH="$CONTROL_PLANE_KERNEL_ROOT" \
      "$python_bin" -m scripts.pm.control_plane.family2_boundary \
        --transition-type slot_ready --event "$event"
  })"
  rc=$?
  printf '%s\n' "$output"
  case "$rc" in
    0) printf 'PM_TRANSITION_OK command=slot-ready family2=slot_ready\n'; return 0 ;;
    23) printf 'PM_TRANSITION_DEFERRED command=slot-ready family2=slot_ready\n'; return 23 ;;
    *) printf 'PM_TRANSITION_BLOCKED command=slot-ready reason=family2_boundary_refused\n' >&2; return 1 ;;
  esac
}

family2_pm_review() {
  local pr="" scope="" python_bin output rc
  local -a extra_args=()
  local -a python_args=(--transition-type pm_review)
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      --scope) scope="${2:-}"; shift 2 ;;
      --reason) shift 2 ;;
      --review-evidence) extra_args+=(--review-evidence "${2:-}"); shift 2 ;;
      *) die 2 "unknown pm-review arg $1" ;;
    esac
  done
  need_num pr "$pr"
  [ "$scope" = "phase-a" ] || die 2 "scope must be phase-a"
  python_args+=(--pr "$pr")
  if [ "${#extra_args[@]}" -gt 0 ]; then
    python_args+=("${extra_args[@]}")
  fi
  python_bin="${CONTROL_PLANE_KERNEL_PYTHON:-}"
  [ -x "$python_bin" ] || die 49 "family2_python_boundary_required path=$python_bin"
  output="$({
    cd "$CONTROL_PLANE_KERNEL_ROOT" || exit 49
    GH_REPO="$REPO" MOP_HOST="$MOP_BASE" \
      CONTROL_PLANE_KERNEL_DATABASE="$CONTROL_PLANE_KERNEL_DATABASE" \
      PYTHONPATH="$CONTROL_PLANE_KERNEL_ROOT" \
      "$python_bin" -m scripts.pm.control_plane.family2_boundary \
        "${python_args[@]}"
  })"
  rc=$?
  printf '%s\n' "$output"
  case "$rc" in
    0) printf 'PM_TRANSITION_OK command=pm-review family2=pm_review pr=%s scope=phase-a\n' "$pr"; return 0 ;;
    23) printf 'PM_TRANSITION_DEFERRED command=pm-review family2=pm_review pr=%s scope=phase-a\n' "$pr"; return 23 ;;
    *) printf 'PM_TRANSITION_BLOCKED command=pm-review reason=family2_boundary_refused pr=%s\n' "$pr" >&2; return 1 ;;
  esac
}

adopt_current_head_review_after_override() {
  local pr="$1" issue="$2" head="$3" branch="$4"
  local marker plan meta
  marker="$(pm_review_marker_path "$pr" "$head")"
  pm_review_marker_ok_for_scope "$pr" "$head" phase-a || return 1
  plan="$(awk -F': ' '/^affected_test_plan:[[:space:]]*/ {print $2; exit}' "$marker" 2>/dev/null || true)"
  [ -n "$plan" ] || return 1
  python3 - "$plan" "$pr" "$head" <<'PY' >/dev/null 2>&1 || return 1
import json
import sys
from pathlib import Path

path, expected_pr, expected_head = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
data = json.loads(path.read_text(encoding="utf-8"))
commands = data.get("commands")
ok = (
    data.get("pr") == expected_pr
    and str(data.get("headRefOid") or "") == expected_head
    and isinstance(commands, list)
    and bool(commands)
    and all(isinstance(item, dict) and str(item.get("command") or "").strip() for item in commands)
)
raise SystemExit(0 if ok else 1)
PY

  meta="$(pm_review_meta_file "$pr")"
  python3 - "$meta" "$pr" "$issue" "$head" "$branch" "$marker" "$plan" <<'PY' || return 1
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

path, pr, issue, head, branch, marker, plan = sys.argv[1:]
data = {
    "schema_version": 1,
    "source": "pm-transition rescope-decide/adopt-current-head-review",
    "status": "pending-validation",
    "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "pr": int(pr),
    "issue": int(issue) if str(issue).isdigit() else None,
    "headRefOid": head,
    "branch": branch,
    "scope": "phase-a",
    "reason": "cto-override-existing-review",
    "released_slots": [],
    "expected_marker": marker,
    "affected_test_plan": plan,
    "affected_test_proof": None,
    "adopted": True,
    "new_review_required": False,
}
target = Path(path)
tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(target)
PY
  printf '%s\t%s\t%s' "$marker" "$plan" "$meta"
}

ci_proof_has_followup_issue() {
  local proof="$1" issue
  issue="$(
    sed -nE \
      's~^(flake_followup|followup_issue|follow_up_issue|preexisting_followup|pre_existing_followup|follow-up):[[:space:]]*(#([0-9]+)|https://github.com/[^[:space:]]+/issues/([0-9]+))[[:space:]]*$~#\3\4~Ip' \
      "$proof" 2>/dev/null \
      | head -1
  )"
  [[ "$issue" =~ ^#[0-9]+$ ]] || return 1
  [ -f "$CONTROL_PLANE_ISSUE_POLICY" ] || return 1
  python3 "$CONTROL_PLANE_ISSUE_POLICY" validate-followup \
    --issue "$issue" \
    --repo "$REPO" \
    >/dev/null 2>&1
}

ci_proof_has_clean_pass() {
  local proof="$1"
  grep -qE '^(CI_LOCAL_PREFLIGHT|AFFECTED_TESTS):[[:space:]]*(PASS|DOCS_ONLY)($|[[:space:]])' "$proof" 2>/dev/null
}

ci_proof_targeted_run_ok() {
  local pr="$1" head="$2" proof="$3" run_id run_json
  grep -qE '^AFFECTED_TESTS:[[:space:]]*TARGETED_CI_PASS($|[[:space:]])' "$proof" 2>/dev/null || return 1
  run_id="$(sed -n 's/^targeted_ci_run:[[:space:]]*\([0-9][0-9]*\)[[:space:]]*$/\1/p' "$proof" | head -1)"
  [[ "$run_id" =~ ^[0-9]+$ ]] || return 1
  grep -Fqx "targeted_ci_head: $head" "$proof" 2>/dev/null || return 1
  grep -Fqx 'targeted_ci_conclusion: success' "$proof" 2>/dev/null || return 1
  grep -Fqx 'targeted_ci_event: pull_request' "$proof" 2>/dev/null || return 1
  run_json="$(gh run view "$run_id" --repo "$REPO" --json headSha,conclusion,event,jobs 2>/dev/null || true)"
  [ -n "$run_json" ] || return 1
  python3 - "$head" "$run_json" <<'PY' >/dev/null 2>&1
import json, sys
head, raw = sys.argv[1:]
data = json.loads(raw)
jobs = data.get("jobs") or []
ok = (
    data.get("headSha") == head
    and str(data.get("conclusion") or "").lower() == "success"
    and data.get("event") == "pull_request"
    and any(str(job.get("conclusion") or "").lower() == "success" for job in jobs)
)
raise SystemExit(0 if ok else 1)
PY
}

ci_proof_has_exception_pass() {
  local proof="$1"
  grep -qE '^(CI_LOCAL_PREFLIGHT|AFFECTED_TESTS):[[:space:]]*(PASS_WITH_PREEXISTING_FAILURES|NO_LOCAL_EQUIVALENT)($|[[:space:]])' "$proof" 2>/dev/null
}

affected_test_proof_ok() {
  local pr="$1" head="$2" proof="${3:-}" candidate
  if [ -z "$proof" ]; then
    for candidate in \
      "/tmp/affected-test-proof-${pr}-${head}.ok" \
      "/tmp/affected-test-proof-${pr}-${head:0:8}.ok"; do
      if [ -f "$candidate" ]; then
        proof="$candidate"
        break
      fi
    done
  fi
  [ -f "$proof" ] || return 1
  # The canonical marker-PASS CTO-adjudicated override packet is a valid
  # current-head proof; the readiness gate performs the full sealed-packet
  # validation (artifact digests, capture-run receipt, one-time consumption).
  if affected_test_marker_pass_override_ok "$pr" "$head" "$proof"; then
    return 0
  fi
  # CTO-adjudicated NO_PATCH_REQUIRED rescue override (same family).
  if affected_test_no_patch_rescue_override_ok "$pr" "$head" "$proof"; then
    return 0
  fi
  # CTO-adjudicated exact-tuple CI-start admission (same family).
  if affected_test_exact_tuple_override_ok "$pr" "$head" "$proof"; then
    return 0
  fi
  # CTO-adjudicated cancelled-run local-preflight CI-start admission (#7305).
  if affected_test_cancelled_run_override_ok "$pr" "$head" "$proof"; then
    return 0
  fi
  # CTO-adjudicated local-preflight-rebind CI-start admission (#7308).
  if affected_test_local_preflight_rebind_override_ok "$pr" "$head" "$proof"; then
    return 0
  fi
  grep -qE '^AFFECTED_TESTS:' "$proof" 2>/dev/null || return 1
  ci_proof_has_clean_pass "$proof" \
    || ci_proof_targeted_run_ok "$pr" "$head" "$proof" \
    || { ci_proof_has_exception_pass "$proof" && ci_proof_has_followup_issue "$proof"; } \
    || return 1
  grep -q "headRefOid: ${head}" "$proof" 2>/dev/null || return 1
  grep -qE '^no_full_suite:[[:space:]]*true($|[[:space:]])' "$proof" 2>/dev/null || return 1
  return 0
}

affected_test_marker_pass_override_ok() {
  local pr="$1" head="$2" proof="${3:-}"
  local expected="/tmp/pm-ci-start-override-${pr}-${head}.ok"
  [ -n "$proof" ] && [ "$proof" = "$expected" ] || return 1
  [ -f "$proof" ] || return 1
  grep -Fqx "headRefOid: $head" "$proof" || return 1
  grep -qE '^provenance:[[:space:]]*cto-marker-pass-vacuous-red-ci-admission($|[[:space:]])' "$proof" || return 1
  grep -qE '^AFFECTED_TESTS:[[:space:]]*PASS_WITH_VACUOUS_RED($|[[:space:]])' "$proof" || return 1
  grep -qE '^no_full_suite:[[:space:]]*true($|[[:space:]])' "$proof" || return 1
  grep -qE '^consumed:[[:space:]]*no($|[[:space:]])' "$proof" || return 1
  return 0
}

affected_test_no_patch_rescue_override_ok() {
  # CTO-adjudicated NO_PATCH_REQUIRED rescue consumption (incident
  # cp-repair:cto-direct-admission-unexecutable:7268): the canonical one-use
  # override at /tmp/pm-ci-start-override-<pr>-<head>.ok is a valid
  # current-head proof for the pm-review-done path. The readiness gate
  # performs the full sealed-packet validation (packet/red/green digests,
  # decision timestamps, expiry, one-time consumption); this helper only
  # pre-identifies the class so the flow routes the override to the gate.
  local pr="$1" head="$2" proof="${3:-}"
  local expected="/tmp/pm-ci-start-override-${pr}-${head}.ok"
  [ -n "$proof" ] && [ "$proof" = "$expected" ] || return 1
  [ -f "$proof" ] || return 1
  grep -Fqx "headRefOid: $head" "$proof" || return 1
  grep -qE '^provenance:[[:space:]]*cto-no-patch-rescue-ci-admission($|[[:space:]])' "$proof" || return 1
  grep -qE '^AFFECTED_TESTS:[[:space:]]*PASS($|[[:space:]])' "$proof" || return 1
  grep -qE '^no_full_suite:[[:space:]]*true($|[[:space:]])' "$proof" || return 1
  grep -qE '^consumed:[[:space:]]*no($|[[:space:]])' "$proof" || return 1
  grep -qE '^rescue_packet:[[:space:]]*.+sha256:[0-9a-f]{64}$' "$proof" || return 1
  return 0
}

affected_test_exact_tuple_override_ok() {
  # CTO-adjudicated exact-tuple CI-start admission (Rajiv thread
  # 1786724301.511569 ts 1786725255.074339 + CTO decision thread
  # 1786717451.157469 ts 1786724519.596549): the canonical one-use override
  # at /tmp/pm-ci-start-override-<pr>-<head>.ok is a valid current-head
  # proof for the pm-review-done path for exactly three allowlisted tuples
  # (7275 post-capture-preflight, 7289/7331 vacuous-red). The readiness gate
  # performs the full sealed-packet validation (artifact digests, capture
  # run, obligation binding, one-time consumption); this helper only
  # pre-identifies the class so the flow routes the override to the gate.
  # The pre-selector MUST enforce the same three-entry allowlist before any
  # loop-blocker/marker bypass is granted: an unallowlisted tuple with this
  # provenance must stay fail-closed at the stale-loop breaker.
  local pr="$1" head="$2" proof="${3:-}"
  local expected="/tmp/pm-ci-start-override-${pr}-${head}.ok"
  [ -n "$proof" ] && [ "$proof" = "$expected" ] || return 1
  [ -f "$proof" ] || return 1
  local expected_mode=""
  case "${pr}:${head}" in
    7275:517123fbdec371ade3becb4d19bfeaee033b78a9) expected_mode="post-capture-preflight" ;;
    7289:f7c16e84192b834ad73763a78430f9ec0c57b032|7331:b7f5c5851975fada85691d85447324b33eb35abc) expected_mode="vacuous-red" ;;
    *) return 1 ;;
  esac
  grep -qE "^mode:[[:space:]]*${expected_mode}($|[[:space:]])" "$proof" || return 1
  grep -Fqx "headRefOid: $head" "$proof" || return 1
  grep -qE '^provenance:[[:space:]]*cto-exact-tuple-ci-admission($|[[:space:]])' "$proof" || return 1
  grep -qE '^AFFECTED_TESTS:[[:space:]]*PASS_WITH_VACUOUS_RED($|[[:space:]])' "$proof" || return 1
  grep -qE '^no_full_suite:[[:space:]]*true($|[[:space:]])' "$proof" || return 1
  grep -qE '^consumed:[[:space:]]*no($|[[:space:]])' "$proof" || return 1
  return 0
}

affected_test_cancelled_run_override_ok() {
  # CTO-adjudicated cancelled-run local-preflight CI-start admission
  # (Rajiv/CTO recovery lane C0ALZJHGE49 thread 1786759192.277439 ts
  # 1786760957.087989): the canonical one-use override at
  # /tmp/pm-ci-start-override-<pr>-<head>.ok is a valid current-head proof
  # for the pm-review-done path for the exact allowlisted #7305 tuple. The
  # readiness gate performs the full sealed-packet validation (failed run,
  # sealed preflight proof, source-receipt/log digests, change-scope digests,
  # one-time atomic consumption); this helper only pre-identifies the class
  # so the flow routes the override to the gate. The pre-selector MUST
  # enforce the same exact-tuple allowlist before any loop-blocker bypass is
  # granted: an unallowlisted tuple with this provenance stays fail-closed at
  # the stale-loop breaker.
  local pr="$1" head="$2" proof="${3:-}"
  local expected="/tmp/pm-ci-start-override-${pr}-${head}.ok"
  [ -n "$proof" ] && [ "$proof" = "$expected" ] || return 1
  [ -f "$proof" ] || return 1
  [ "$pr:$head" = "7305:14460f7e8193d3bbcd7a1932eff06487a2075098" ] || return 1
  grep -qE "^mode:[[:space:]]*cancelled-run-local-preflight($|[[:space:]])" "$proof" || return 1
  grep -Fqx "headRefOid: $head" "$proof" || return 1
  grep -qE '^provenance:[[:space:]]*cto-cancelled-run-local-preflight-ci-admission($|[[:space:]])' "$proof" || return 1
  grep -qE '^AFFECTED_TESTS:[[:space:]]*PASS_WITH_VACUOUS_RED($|[[:space:]])' "$proof" || return 1
  grep -qE '^no_full_suite:[[:space:]]*true($|[[:space:]])' "$proof" || return 1
  grep -qE '^consumed:[[:space:]]*no($|[[:space:]])' "$proof" || return 1
  return 0
}

affected_test_local_preflight_rebind_override_ok() {
  # CTO-adjudicated one-shot local-preflight-rebind CI-start admission
  # (#7308, CTO typed blocker ts 1786767400.760729, thread
  # 1786759192.277439): the canonical one-use override at
  # /tmp/pm-ci-start-override-<pr>-<head>.ok is a valid current-head proof
  # for the pm-review-done path for the exact allowlisted #7308 tuple. The
  # readiness gate performs the full sealed-packet validation (log/receipt/
  # Fable-marker digests, rebind checkout HEAD/tree/clean, open obligations,
  # one-time atomic consumption); this helper only pre-identifies the class so
  # the flow routes the override to the gate. The pre-selector MUST enforce
  # the same exact-tuple allowlist before any loop-blocker bypass is granted:
  # an unallowlisted tuple with this provenance stays fail-closed at the
  # stale-loop breaker.
  local pr="$1" head="$2" proof="${3:-}"
  local expected="/tmp/pm-ci-start-override-${pr}-${head}.ok"
  [ -n "$proof" ] && [ "$proof" = "$expected" ] || return 1
  [ -f "$proof" ] || return 1
  [ "$pr:$head" = "7308:559419e2629ec7d8105664d621f902644f09f509" ] || return 1
  grep -qE "^mode:[[:space:]]*local-preflight-rebind($|[[:space:]])" "$proof" || return 1
  grep -Fqx "headRefOid: $head" "$proof" || return 1
  grep -qE '^provenance:[[:space:]]*cto-local-preflight-rebind-ci-admission($|[[:space:]])' "$proof" || return 1
  grep -qE '^AFFECTED_TESTS:[[:space:]]*PASS($|[[:space:]])' "$proof" || return 1
  grep -qE '^no_full_suite:[[:space:]]*true($|[[:space:]])' "$proof" || return 1
  grep -qE '^rebind_checkout:[[:space:]]*\/' "$proof" || return 1
  grep -qE '^consumed:[[:space:]]*no($|[[:space:]])' "$proof" || return 1
  return 0
}

affected_test_ci_bootstrap_candidate_ok() {
  # This is deliberately weaker than affected_test_proof_ok and is admitted
  # only by pm-review-done. pr-ci-readiness-gate.py performs the full plan,
  # command, log-digest, mandatory-target, auth-wall, and exact-head validation.
  local pr="$1" head="$2" proof="${3:-}" status reason
  local expected="/tmp/affected-test-proof-${pr}-${head}.ok"
  [ -n "$proof" ] || proof="$expected"
  [ "$proof" = "$expected" ] || return 1
  [ -f "$proof" ] || return 1
  status="$(sed -n 's/^AFFECTED_TESTS:[[:space:]]*//p' "$proof" | head -1)"
  reason="$(sed -n 's/^defer_reason:[[:space:]]*//p' "$proof" | head -1)"
  case "$status|$reason" in
    LOCAL_AUTH_E2E_DEFERRED_TO_CI\|slot_local_clerk_auth_unavailable) ;;
    LOCAL_SEED_ADMISSION_E2E_DEFERRED_TO_CI\|local_seed_admission_unavailable) ;;
    *) return 1 ;;
  esac
  grep -Fqx "PR: $pr" "$proof" 2>/dev/null || return 1
  grep -Fqx "headRefOid: $head" "$proof" 2>/dev/null || return 1
  grep -Fqx 'no_full_suite: true' "$proof" 2>/dev/null || return 1
  grep -Fqx 'final_ci_e2e_required: true' "$proof" 2>/dev/null || return 1
}

affected_test_failure_log_for_head() {
  local pr="$1" head="$2" log exit_code
  log="/tmp/affected-test-proof-${pr}-${head}.log"
  [ -f "$log" ] || return 1
  exit_code="$(
    awk '
      /^## exit [0-9]+$/ { code = $3 }
      END {
        if (code != "") {
          print code
        }
      }
    ' "$log"
  )"
  [[ "$exit_code" =~ ^[0-9]+$ ]] || return 1
  [ "$exit_code" -ne 0 ] || return 1
  printf '%s\t%s\n' "$log" "$exit_code"
}

affected_test_proof_for_head() {
  local pr="$1" head="$2" proof="${3:-}" candidate
  if [ -n "$proof" ] && [ -f "$proof" ]; then
    printf '%s' "$proof"
    return 0
  fi
  for candidate in \
    "/tmp/affected-test-proof-${pr}-${head}.ok" \
    "/tmp/affected-test-proof-${pr}-${head:0:8}.ok"; do
    if [ -f "$candidate" ]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

materialize_no_patch_rescue_plan() {
  # Materialize the deterministic exact-head affected-test plan for the
  # CTO-adjudicated NO_PATCH_REQUIRED rescue class from the bound rescue
  # packet's proof_required_after_slot contract. The plan is derived, never
  # invented: packet path+digest and PR/head are re-verified before any write.
  local pr="$1" head="$2" override="$3"
  local plan="/tmp/affected-test-plan-${pr}-${head}.json"
  # Always re-verify the bound rescue packet and re-derive the plan from its
  # CURRENT proof_required_after_slot. A stale plan at the canonical path must
  # never become authoritative: the packet digest, PR, and head are re-checked
  # and the plan is atomically replaced (or the materializer fails closed).
  python3 - "$override" "$pr" "$head" "$plan" <<'PY'
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

override, expected_pr, expected_head, plan_path = sys.argv[1:]
try:
    override_text = Path(override).read_text(encoding="utf-8")
except OSError:
    raise SystemExit(1)
packet_match = re.search(
    r"(?im)^rescue_packet:\s*(\S+)\s+sha256:([0-9a-f]{64})\s*$",
    override_text,
)
if not packet_match:
    raise SystemExit(1)
packet_path, packet_digest = packet_match.group(1), packet_match.group(2)
try:
    packet_bytes = Path(packet_path).read_bytes()
except OSError:
    raise SystemExit(1)
if hashlib.sha256(packet_bytes).hexdigest() != packet_digest:
    raise SystemExit(1)
packet = packet_bytes.decode("utf-8", errors="replace")
pr_field = re.search(r"(?im)^pr:\s*#?(\d+)\s*$", packet)
head_field = re.search(r"(?im)^head_or_plan_sha:\s*([0-9a-f]{40})\s*$", packet)
proof = re.search(r"(?im)^proof_required_after_slot:\s*(.+)$", packet)
if (
    not pr_field
    or int(pr_field.group(1)) != int(expected_pr)
    or not head_field
    or head_field.group(1) != expected_head
    or not proof
    or not proof.group(1).strip()
):
    raise SystemExit(1)
command = proof.group(1).strip()
kind = "vitest" if re.search(r"vitest|\.test\.|\.spec\.", command) else "other"
payload = {
    "schema_version": 1,
    "producer": "pm-transition cto-no-patch-rescue",
    "pr": int(expected_pr),
    "headRefOid": expected_head,
    "scope": "targeted",
    "requires_pm_test_scope": False,
    "verification_results": None,
    "commands": [{"command": command, "kind": kind}],
}
tmp = f"{plan_path}.{Path(plan_path).name}.{os.getpid()}.tmp"
Path(tmp).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
Path(tmp).replace(plan_path)
PY
}

affected_test_plan_for_proof() {
  local pr="$1" head="$2" proof="$3" plan
  [ -n "$pr" ] && [ -n "$head" ] && [ -f "$proof" ] || return 1
  if affected_test_marker_pass_override_ok "$pr" "$head" "$proof"; then
    plan="$(awk -F': ' '/^affected_plan:[[:space:]]*/ {print $2; exit}' "$proof" 2>/dev/null || true)"
    plan="${plan%% sha256:*}"
  elif affected_test_no_patch_rescue_override_ok "$pr" "$head" "$proof"; then
    # CTO-adjudicated NO_PATCH_REQUIRED rescue: materialize the deterministic
    # exact-head plan from the bound rescue packet's proof contract so the
    # transition's canonical plan consumers stay intact. The materializer
    # re-verifies the bound packet digest/PR/head and atomically replaces any
    # stale plan, so an existing plan file can never become authoritative.
    materialize_no_patch_rescue_plan "$pr" "$head" "$proof" || return 1
    plan="/tmp/affected-test-plan-${pr}-${head}.json"
  elif affected_test_exact_tuple_override_ok "$pr" "$head" "$proof"; then
    # CTO-adjudicated exact-tuple CI-start admission: the packet binds the
    # canonical affected-test plan path (vacuous-red modes) or the sealed
    # post-capture preflight proof references it (post-capture-preflight);
    # the plan is a bound diagnostic, never a CI-start prerequisite.
    plan="$(awk -F': ' '/^affected_plan:[[:space:]]*/ {print $2; exit}' "$proof" 2>/dev/null || true)"
    plan="${plan%% sha256:*}"
  elif affected_test_cancelled_run_override_ok "$pr" "$head" "$proof"; then
    # Cancelled-run local-preflight admission (#7305): the packet binds the
    # sealed preflight proof and source-receipt/log digests; the affected-test
    # plan is a bound diagnostic, never a CI-start prerequisite.
    plan="$(awk -F': ' '/^affected_plan:[[:space:]]*/ {print $2; exit}' "$proof" 2>/dev/null || true)"
    plan="${plan%% sha256:*}"
  elif affected_test_local_preflight_rebind_override_ok "$pr" "$head" "$proof"; then
    # Local-preflight-rebind admission (#7308): the packet binds the sealed
    # preflight proof, receipt/log/Fable-marker digests, and the rebind
    # checkout; the affected-test plan is a bound diagnostic, never a
    # CI-start prerequisite.
    plan="$(awk -F': ' '/^affected_plan:[[:space:]]*/ {print $2; exit}' "$proof" 2>/dev/null || true)"
    plan="${plan%% sha256:*}"
  else
    plan="$(awk -F': ' '/^plan:[[:space:]]*/ {print $2; exit}' "$proof" 2>/dev/null || true)"
  fi
  [ -n "$plan" ] && [ -f "$plan" ] || return 1
  python3 - "$plan" "$pr" "$head" <<'PY'
import json
import sys
from pathlib import Path

path, expected_pr, expected_head = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
try:
    packet = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(1)
if packet.get("pr") != expected_pr or str(packet.get("headRefOid") or "") != expected_head:
    raise SystemExit(1)
commands = packet.get("commands")
if not isinstance(commands, list):
    raise SystemExit(1)
if not commands:
    empty_scope_ok = (
        packet.get("scope") == "unit-integration-empty"
        and packet.get("no_affected_unit_tests") is True
        and packet.get("requires_pm_test_scope") is False
        and packet.get("unresolved_sources") == []
        and isinstance(packet.get("changed_files"), list)
        and bool(packet.get("changed_files"))
        and isinstance(packet.get("ignored_non_unit_files"), list)
    )
    if not empty_scope_ok:
        raise SystemExit(1)
print(path)
PY
}

capture_local_proof_ok() {
  local pr="$1" head="$2" proof="$3" failed_run="${4:-}"
  [ -n "$pr" ] || return 1
  [ -n "$head" ] || return 1
  [ -f "$proof" ] || return 1
  grep -q "headRefOid: ${head}" "$proof" 2>/dev/null || return 1
  [ -z "$failed_run" ] || grep -q "failed_remote_run: ${failed_run}" "$proof" 2>/dev/null || return 1
  grep -qE '^CAPTURE_LOCAL:[[:space:]]*(PASS|PASS_NOT_REQUIRED)($|[[:space:]])' "$proof" 2>/dev/null || return 1
  if grep -qE '^CAPTURE_LOCAL:[[:space:]]*PASS_NOT_REQUIRED($|[[:space:]])' "$proof" 2>/dev/null; then
    grep -qE '^fixture_capture_not_required_proof:[[:space:]]*no_fixture_producing_request($|[[:space:]])' "$proof" 2>/dev/null || return 1
  fi
  grep -qE '^label_gated_ci_allowed_after_local_capture:[[:space:]]*true($|[[:space:]])' "$proof" 2>/dev/null || return 1
  grep -qE '^capture_format_only:[[:space:]]*(true|false)($|[[:space:]])' "$proof" 2>/dev/null || return 1
  grep -qE '^fixture_capture_policy:[[:space:]]*update_all_llm_proxy_cache_misses_on_selected_auto_process_path($|[[:space:]])' "$proof" 2>/dev/null || return 1
  # PASS_NOT_REQUIRED = no capture ran (no_capture_sensitive_diff): the template-coverage /
  # sc-writeback content checks only apply when a capture actually executed (CAPTURE_LOCAL: PASS).
  if ! grep -qE '^CAPTURE_LOCAL:[[:space:]]*PASS_NOT_REQUIRED($|[[:space:]])' "$proof" 2>/dev/null; then
    grep -qE '^llm_fixture_r2_bucket:[[:space:]]*heydonna-fixtures($|[[:space:]])' "$proof" 2>/dev/null || return 1
    grep -qE '^fixture_bucket_policy:[[:space:]]*pinned_to_ci_fixture_bucket($|[[:space:]])' "$proof" 2>/dev/null || return 1
    grep -qE '^fixture_verification_status:[[:space:]]*pass($|[[:space:]])' "$proof" 2>/dev/null || return 1
    # #6197: per-slot Clerk/editor-settings clone retired — capture now runs
    # directly as the shared TEST_ADMIN_EMAIL Clerk identity. The removed
    # capture_settings_clone_status / capture_settings_hash /
    # capture_settings_prompt_fields fields are gone; validate the shared
    # capture identity resolved and owns the capture API key instead.
    grep -qE '^capture_identity_user_id:[[:space:]]*[^[:space:]].+' "$proof" 2>/dev/null || return 1
    grep -qE '^capture_identity_email:[[:space:]]*[^[:space:]].+' "$proof" 2>/dev/null || return 1
    grep -qE '^capture_api_key_user_id:[[:space:]]*[^[:space:]].+' "$proof" 2>/dev/null || return 1
    local _capture_identity_user _capture_api_key_user
    _capture_identity_user="$(awk -F': ' '/^capture_identity_user_id:/ {print $2; exit}' "$proof" 2>/dev/null || true)"
    _capture_api_key_user="$(awk -F': ' '/^capture_api_key_user_id:/ {print $2; exit}' "$proof" 2>/dev/null || true)"
    [ -n "$_capture_identity_user" ] && [ "$_capture_identity_user" = "$_capture_api_key_user" ] || return 1
    grep -qE '^fixture_key_parity_status:[[:space:]]*pass($|[[:space:]])' "$proof" 2>/dev/null || return 1
    grep -qE '^fixture_key_parity_basis:[[:space:]]*shared_clerk_identity_plus_verified_current_head_fixture_keys($|[[:space:]])' "$proof" 2>/dev/null || return 1
    # fixture_verified_keys non-empty check REMOVED per Rajiv directive 2026-07-08
    # (DM 1783488105): the R2 fixture-key verification was removed from
    # capture-local-proof.sh — CI strict-replay is the fixture-presence gate — so
    # fixture_verified_keys is always "none". Do not require it here.
    if grep -qE '^capture_format_only:[[:space:]]*true($|[[:space:]])' "$proof" 2>/dev/null; then
      if grep -qE '^capture_template_coverage:.*ny_standard.*acr|^capture_template_coverage:.*acr.*ny_standard' "$proof" 2>/dev/null; then
        :
      else
        # Targeted Playwright is the documented fallback when the normal NY+ACR
        # harness cannot produce the failing request shape. Accept it only when
        # the producer proves the requested key was exercised in this capture and
        # exists in R2; generic Playwright PASS is not sufficient.
        grep -qE '^capture_harness:[[:space:]]*playwright_specs($|[[:space:]])' "$proof" 2>/dev/null || return 1
        grep -qE '^capture_template_coverage:[[:space:]]*playwright_specs($|[[:space:]])' "$proof" 2>/dev/null || return 1
        grep -qE '^capture_template_specs:[[:space:]]*\[[^]]+\]($|[[:space:]])' "$proof" 2>/dev/null || return 1
        grep -qE '^failed_remote_run:[[:space:]]*[0-9]+($|[[:space:]])' "$proof" 2>/dev/null || return 1
        grep -E '^required_fixture_keys:[[:space:]]*[^[:space:]].*' "$proof" 2>/dev/null | grep -vqE '^required_fixture_keys:[[:space:]]*none([[:space:]]|$)' || return 1
        grep -E '^fixture_verified_keys:[[:space:]]*[^[:space:]].*' "$proof" 2>/dev/null | grep -vqE '^fixture_verified_keys:[[:space:]]*none([[:space:]]|$)' || return 1
        grep -qE '^targeted_fixture_proof:[[:space:]]*true($|[[:space:]])' "$proof" 2>/dev/null || return 1
        grep -qE '^targeted_fixture_evidence:[[:space:]]*modal_log_plus_r2_head($|[[:space:]])' "$proof" 2>/dev/null || return 1
      fi
    else
      grep -qE '^sc_writeback_assertions_required:[[:space:]]*true($|[[:space:]])' "$proof" 2>/dev/null || return 1
    fi
  fi
	  if grep -qE '^current_head_verified_before_capture:[[:space:]]*true($|[[:space:]])' "$proof" 2>/dev/null; then
	    grep -qE '^current_head_status:[[:space:]]*exact_pr_head($|[[:space:]])' "$proof" 2>/dev/null || return 1
	  else
	    # Backward-compatible acceptance for proofs produced before the exact-head contract.
	    grep -qE '^origin_main_merged_before_capture:[[:space:]]*true($|[[:space:]])' "$proof" 2>/dev/null || return 1
	    grep -qE '^main_merge_status:[[:space:]]*already_contains_origin_main($|[[:space:]])' "$proof" 2>/dev/null || return 1
	  fi
  return 0
}

capture_local_proof_for_head() {
  local pr="$1" head="$2" candidate
  for candidate in \
    "/tmp/capture-local-proof-${pr}-${head}.ok" \
    "/tmp/capture-local-proof-${pr}-${head:0:8}.ok"; do
    if capture_local_proof_ok "$pr" "$head" "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

pm_review_marker_ok_for_scope() {
  local pr="$1" head="$2" scope="$3" marker age_s marker_class
  marker="$(pm_review_marker_path "$pr" "$head")"
  if [ -f "$marker" ]; then
    age_s=$(python3 -c 'import os,time,sys; print(int(time.time() - os.path.getmtime(sys.argv[1])))' "$marker" 2>/dev/null || echo 999999)
    [ "$age_s" -le 86400 ] || return 1
  else
    return 1
  fi
  grep -q "headRefOid: ${head}" "$marker" 2>/dev/null || return 1
  if grep -qE '^MARKER_PROVENANCE[:=][[:space:]]*codex-review-companion($|[[:space:]])' "$marker" 2>/dev/null; then
    marker_class="codex"
    grep -qE '^FINAL_REVIEWER_VERDICT[:=][[:space:]]*APPROVE($|[[:space:]])' "$marker" 2>/dev/null || return 1
    grep -qE "^(PR|pr)[:=][[:space:]]*#?${pr}($|[[:space:]])" "$marker" 2>/dev/null || return 1
    grep -qE "^(HEAD_SHA|headRefOid)[:=][[:space:]]*${head}($|[[:space:]])" "$marker" 2>/dev/null || return 1
  elif grep -qE '^(PM_CLAUDE_REVIEW|PM_OPUS_REVIEW)[:=][[:space:]]*PASS($|[[:space:]])' "$marker" 2>/dev/null; then
    # Genuine PM Claude / PM Opus PASS marker (Rajiv directive 2026-07-15): accepted alongside
    # authenticated Codex + PM_OVERRIDE markers. The common headRefOid + pass_scope +
    # readiness_ceiling + runtime_control_point checks below still apply. Do not synthesize an override.
    marker_class="pm_claude"
  elif grep -qE '^PM_OVERRIDE:[[:space:]]*APPROVE($|[[:space:]])' "$marker" 2>/dev/null; then
    marker_class="pm_override"
    grep -qE '^override_identity:[[:space:]]*[^[:space:]].+' "$marker" 2>/dev/null || return 1
    grep -qE '^override_timestamp:[[:space:]]*[^[:space:]].+' "$marker" 2>/dev/null || return 1
    grep -qE '^override_source_citation:[[:space:]]*[^[:space:]].+' "$marker" 2>/dev/null || return 1
    grep -qE '^override_rationale:[[:space:]]*[^[:space:]].+' "$marker" 2>/dev/null || return 1
  else
    return 1
  fi
  grep -qE '^runtime_control_point:[[:space:]]*[^[:space:]].+' "$marker" 2>/dev/null || return 1
	  case "$scope" in
	    phase-a)
	      grep -qE '^pass_scope:[[:space:]]*phase-a($|[[:space:]])' "$marker" 2>/dev/null || return 1
	      grep -qE '^readiness_ceiling:[[:space:]]*[^[:space:]].+' "$marker" 2>/dev/null || return 1
	      ;;
	    merge-ready)
	      grep -qE '^pass_scope:[[:space:]]*merge-ready($|[[:space:]])' "$marker" 2>/dev/null || return 1
	      grep -qE '^readiness_ceiling:[[:space:]]*.*merge-ready' "$marker" 2>/dev/null || return 1
	      grep -qE '^branch_freshness:[[:space:]]*[^[:space:]].+' "$marker" 2>/dev/null || return 1
      grep -qE '^unresolved_review_threads:[[:space:]]*[^[:space:]].+' "$marker" 2>/dev/null || return 1
      grep -qE '^product_ac_proof:[[:space:]]*[^[:space:]].+' "$marker" 2>/dev/null || return 1
      ;;
    *) return 1 ;;
  esac
  return 0
}

genuine_codex_marker_ok() {
  local pr="$1" head="$2" marker="$3" slot="${4:-}" epoch="${5:-}"
  [ -f "$marker" ] || return 1
  grep -qE '^MARKER_PROVENANCE[:=][[:space:]]*codex-review-companion($|[[:space:]])' "$marker" 2>/dev/null || return 1
  grep -qE '^TYPE[:=][[:space:]]*code-review($|[[:space:]])' "$marker" 2>/dev/null || return 1
  grep -qE '^FINAL_REVIEWER_VERDICT[:=][[:space:]]*APPROVE($|[[:space:]])' "$marker" 2>/dev/null || return 1
  grep -qE "^PR[:=][[:space:]]*#?${pr}($|[[:space:]])" "$marker" 2>/dev/null || return 1
  grep -qE "^HEAD_SHA[:=][[:space:]]*${head}($|[[:space:]])" "$marker" 2>/dev/null || return 1
}

# Post-PR-create review collapse (Rajiv 1786811168.455449): a genuine
# pre-PR exact-head Codex review APPROVE remains valid after PR creation when
# branch/head bytes are unchanged and the PR/head rebind attestation matches.
# The slot-ready event tuple (issue/pr/head/branch) plus the live PR
# createdAt bound against the marker ISSUE/HEAD_SHA/BRANCH/TIMESTAMP is the
# attestation: the marker must have been written BEFORE the PR existed and
# must not carry a conflicting PR field. PM review is then not mandated; it
# stays reserved for cap/rescue or a genuinely new runtime-risk signal.
pre_pr_review_rebind_ok() {
  local pr="$1" issue="$2" head="$3" branch="$4" proof="${5:-}" pr_created_at="${6:-}"
  [ -f "$proof" ] || return 1
  [ -n "$pr_created_at" ] || return 1
  python3 - "$pr" "$issue" "$head" "$branch" "$proof" "$pr_created_at" <<'PY'
import re
import sys
from datetime import datetime
from pathlib import Path

pr, issue, head, branch, proof, created = sys.argv[1:7]
try:
    text = Path(proof).read_text(encoding="utf-8", errors="replace")
except OSError:
    sys.exit(1)


def field(name: str) -> str:
    match = re.search(rf"^{re.escape(name)}:\s*(.*?)\s*$", text, re.M)
    return match.group(1).strip() if match else ""


if not re.search(r"(?m)^MARKER_PROVENANCE:\s*codex-review-companion\s*$", text):
    sys.exit(1)
if not re.search(r"(?m)^TYPE:\s*code-review\s*$", text):
    sys.exit(1)
if not re.search(r"(?m)^FINAL_REVIEWER_VERDICT:\s*APPROVE\s*$", text):
    sys.exit(1)

marker_issue = field("ISSUE").lstrip("#")
marker_head = field("HEAD_SHA")
marker_branch = field("BRANCH")
marker_pr = field("PR").strip().lower()
if marker_issue != str(issue):
    sys.exit(1)
if marker_head != head:
    sys.exit(1)
if marker_branch != branch:
    sys.exit(1)
# A pre-PR marker must not carry a conflicting PR binding; absent PR/-/none
# is the expected pre-PR shape.
if marker_pr not in ("", "-", "none", "n/a"):
    sys.exit(1)
try:
    marker_epoch = int(field("TIMESTAMP"))
except (TypeError, ValueError):
    sys.exit(1)
try:
    created_epoch = int(
        datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
    )
except Exception:
    sys.exit(1)
if marker_epoch >= created_epoch:
    sys.exit(1)
print(
    f"pre_pr_review_rebind_ok pr={pr} issue={issue} "
    f"head={head[:12]} marker={proof}"
)
PY
}

pm_review_marker_capture_gated() {
  local marker="$1"
  grep -qiE '^readiness_ceiling:[[:space:]]*.*capture-gated|^required_pm_action:[[:space:]]*.*capture.*before.*qa-passed-awaiting-ci' "$marker" 2>/dev/null
}

pm_review_capture_green_ok() {
  local pr="$1" head="$2"
  python3 - "$REPO" "$pr" "$head" "$REMOTE_CAPTURE_RUN_VALIDATOR" <<'PY'
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

repo, pr, head, validator = sys.argv[1:5]
capture_classes = {
    "capture-required",
    "e2e-cache-miss",
    "fixture-stale-or-invalid",
    "fixture-observability-missing",
}

def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None

def extract_ci_verdict(text):
    marker = "<!-- ci-verdict:"
    start = text.find(marker)
    if start < 0:
        return None
    end = text.find("-->", start)
    if end < 0:
        return None
    try:
        return json.loads(text[start + len(marker):end].strip())
    except Exception:
        return None

def verdict_head(verdict):
    return str(
        verdict.get("current_pr_head_sha")
        or verdict.get("current_for_pr_head_sha")
        or verdict.get("run_head_sha")
        or verdict.get("head_sha")
        or verdict.get("sha")
        or ""
    )

try:
    proc = subprocess.run(
        ["gh", "pr", "view", pr, "--repo", repo, "--json", "comments"],
        text=True,
        capture_output=True,
        timeout=8,
    )
    if proc.returncode != 0:
        print("capture_proof_unreadable")
        sys.exit(1)
    comments = json.loads(proc.stdout or "{}").get("comments") or []
except Exception as exc:
    print(f"capture_proof_error={exc}")
    sys.exit(1)

for comment in sorted(
    comments,
    key=lambda c: parse_time(c.get("createdAt")) or datetime.min.replace(tzinfo=timezone.utc),
    reverse=True,
):
    verdict = extract_ci_verdict(comment.get("body") or "")
    if not verdict:
        continue
    classification = str(verdict.get("classification") or "")
    if classification not in capture_classes:
        continue
    v_head = verdict_head(verdict)
    if head and v_head and v_head != head:
        continue
    watch_runs = [str(x) for x in (verdict.get("watch_runs") or []) if str(x)]
    if not watch_runs:
        print(f"capture_verdict_missing_watch_run classification={classification}")
        sys.exit(1)
    run_id = watch_runs[-1]
    try:
        run_proc = subprocess.run(
            ["gh", "run", "view", run_id, "--repo", repo, "--json", "status,conclusion,url,jobs,attempt,displayTitle,event,workflowName"],
            text=True,
            capture_output=True,
            timeout=8,
        )
        if run_proc.returncode != 0:
            print(f"capture_run_unreadable run={run_id}")
            sys.exit(1)
        run = json.loads(run_proc.stdout or "{}")
    except Exception as exc:
        print(f"capture_run_error run={run_id} error={exc}")
        sys.exit(1)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as handle:
        json.dump(run, handle)
        run_json_path = handle.name
    validation = subprocess.run(
        [sys.executable, validator, "validate", "--run-json", run_json_path, "--pr", pr, "--head", head],
        text=True,
        capture_output=True,
        timeout=8,
    )
    os.unlink(run_json_path)
    if validation.returncode == 0:
        print(f"capture_green run={run_id}")
        sys.exit(0)
    print(f"capture_not_green run={run_id} detail={(validation.stderr or validation.stdout).strip()}")
    sys.exit(1)

print("missing_current_head_capture_verdict")
sys.exit(1)
PY
}

pr_requires_fresh_capture_before_ci() {
  local pr="$1" out="/tmp/pm-capture-required-${pr}.json" err="/tmp/pm-capture-required-${pr}.err"
  [ -r "$CAPTURE_REQUIRED" ] || return 1
  python3 "$CAPTURE_REQUIRED" --pr "$pr" --repo "$REPO" --json >"$out" 2>"$err"
}

capture_required_json_says_false() {
  local path="$1"
  [ -r "$path" ] || return 1
  python3 - "$path" <<'PY' >/dev/null 2>&1
import json
import sys
from pathlib import Path

try:
    data = json.loads(Path(sys.argv[1]).read_text())
except Exception:
    sys.exit(1)
sys.exit(0 if data.get("capture_required") is False else 1)
PY
}

clear_stale_capture_blocker_if_not_required() {
  local pr="$1" source="$2" head="${3:-}" out="/tmp/pm-capture-required-${pr}.json" labels=""
  pr_requires_fresh_capture_before_ci "$pr" >/dev/null 2>&1
  if ! capture_required_json_says_false "$out"; then
    return 0
  fi
  labels="$(gh pr view "$pr" --repo "$REPO" --json labels --jq '.labels[].name' 2>/dev/null || true)"
  printf '%s\n' "$labels" | grep -qx 'pm-blocked:capture' || return 0
  gh pr edit "$pr" --repo "$REPO" --remove-label "pm-blocked:capture" >/dev/null 2>&1 || return 0
  record_event --source pm-transition --event stale_capture_blocker_cleared --target-type pr --target-id "$pr" --pr "$pr" --head-sha "$head" --payload "source=$source" --payload "proof=$out" --dedupe
  kanban_flag PM_TRANSITION "stale_capture_blocker_cleared pr=$pr source=$source proof=$out"
}

ci_ready_gate_ok() {
  local pr="$1" head="$2" source="$3" affected_test_proof="${4:-}" rescue_authorized="${5:-0}" phase_a_authorized="${6:-0}" ci_stale_run_classified="${7:-}"
  local out="/tmp/pm-ci-ready-gate-${pr}-${head}.json"
  local err="/tmp/pm-ci-ready-gate-${pr}-${head}.err"
  local -a proof_args=()
  [ -n "$affected_test_proof" ] && proof_args+=(--affected-test-proof "$affected_test_proof")
  [ "$rescue_authorized" = "1" ] && proof_args+=(--rescue-authorized)
  [ "$phase_a_authorized" = "1" ] && proof_args+=(--phase-a-authorized)
  if [ -n "$affected_test_proof" ] \
    && affected_test_no_patch_rescue_override_ok \
      "$pr" "$head" "$affected_test_proof"; then
    proof_args+=(--no-patch-rescue-authorized)
  fi
  [ -n "$ci_stale_run_classified" ] && proof_args+=(--ci-stale-run-classified "$ci_stale_run_classified")
  [ -r "$CI_READY_GATE" ] || {
    printf 'ci_ready_gate_missing script=%s\n' "$CI_READY_GATE" >"$err"
    return 1
  }

  # Exact-input short-TTL cache (latency repair 2026-08-15): the gate is
  # expensive (~20s) and PM re-runs it during rework loops on the same
  # pr/head/proof tuple. Reuse the result within a short TTL only when every
  # input matches; live GitHub drift within 2 minutes does not change the
  # gate's decision contract for the same head/proof pair.
  local cache_dir="${PM_CI_READY_GATE_CACHE_DIR:-/tmp/pm-ci-ready-gate-cache}"
  local ttl="${PM_CI_READY_GATE_CACHE_TTL_SECONDS:-120}"
  local proof_sha="" cache_key cache_meta cache_out now cached_ts cached_rc
  if [ -n "$affected_test_proof" ] && [ -f "$affected_test_proof" ]; then
    proof_sha="$(shasum -a 256 "$affected_test_proof" 2>/dev/null | awk '{print $1}')"
  fi
  cache_key="$(printf '%s|%s|%s|%s|%s|%s|%s|%s' \
    "$pr" "$head" "$source" "$affected_test_proof" "$proof_sha" \
    "$rescue_authorized" "$phase_a_authorized" "${ci_stale_run_classified:-none}" \
    | shasum -a 256 2>/dev/null | awk '{print $1}')"
  cache_meta="$cache_dir/$cache_key.meta"
  cache_out="$cache_dir/$cache_key.out"
  now="$(date +%s)"
  if [ -n "$cache_key" ] && [ -f "$cache_meta" ] && [ -f "$cache_out" ]; then
    cached_ts="$(sed -n 's/^ts=//p' "$cache_meta" 2>/dev/null | head -1)"
    cached_rc="$(sed -n 's/^rc=//p' "$cache_meta" 2>/dev/null | head -1)"
    if [[ "$cached_ts" =~ ^[0-9]+$ ]] && [[ "$cached_rc" =~ ^[0-9]+$ ]] \
        && [ $(( now - cached_ts )) -lt "$ttl" ]; then
      cp "$cache_out" "$out" 2>/dev/null || true
      return "$cached_rc"
    fi
  fi

  python3 "$CI_READY_GATE" \
    --pr "$pr" \
    --repo "$REPO" \
    --expect-head "$head" \
    --source "$source" \
    ${proof_args[@]+"${proof_args[@]}"} \
    --json \
    >"$out" 2>"$err"
  local gate_rc=$?
  if [ -n "$cache_key" ]; then
    mkdir -p "$cache_dir" 2>/dev/null || true
    find "$cache_dir" -name '*.meta' -mmin +60 -delete 2>/dev/null || true
    find "$cache_dir" -name '*.out' -mmin +60 -delete 2>/dev/null || true
    cp "$out" "$cache_out" 2>/dev/null || true
    printf 'ts=%s\nrc=%s\n' "$now" "$gate_rc" >"$cache_meta" 2>/dev/null || true
  fi
  return "$gate_rc"
}

ci_ready_gate_control_plane_exempt() {
  local pr="$1"
  local head="$2"
  local out="/tmp/pm-ci-ready-gate-${pr}-${head}.json"
  [ -r "$out" ] || return 1
  python3 - "$out" "$pr" "$head" <<'PY' >/dev/null 2>&1
import json
import re
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text())
scope = ((data.get("artifacts") or {}).get("change_scope") or {})
ok = (
    data.get("ok") is True
    and data.get("disposition") == "control_plane_ci_exempt"
    and data.get("pr") == int(sys.argv[2])
    and data.get("headRefOid") == sys.argv[3]
    and data.get("ci_required") is False
    and data.get("e2e_required") is False
    and scope.get("head") == sys.argv[3]
    and scope.get("scope") == "control_plane_only"
    and scope.get("control_plane_only") is True
    and scope.get("product_changed") is False
    and re.fullmatch(r"[0-9a-f]{64}", str(scope.get("rules_sha256") or ""))
    and re.fullmatch(r"[0-9a-f]{64}", str(scope.get("classifier_sha256") or ""))
)
raise SystemExit(0 if ok else 1)
PY
}

ci_ready_gate_change_scope_field() {
  local pr="$1"
  local head="$2"
  local field="$3"
  local out="/tmp/pm-ci-ready-gate-${pr}-${head}.json"
  python3 - "$out" "$field" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text())
scope = ((data.get("artifacts") or {}).get("change_scope") or {})
value = scope.get(sys.argv[2], "")
if isinstance(value, bool):
    print(str(value).lower())
elif isinstance(value, (str, int)):
    print(value)
else:
    raise SystemExit(1)
PY
}

# Recovery branch for cmd_pm_review_done: accept an already-green PR whose current
# head ran CI/E2E to terminal success BEFORE a (now-cleared) block, so the CI-start
# readiness gate reports the single reason
# "current_head_ci_or_e2e_already_terminal_green". This is fail-closed: it returns 0
# ONLY when every CTO condition holds, and on success exports the reused CI and E2E
# run IDs in PM_ALREADY_GREEN_CI_RUN_ID / PM_ALREADY_GREEN_E2E_RUN_ID. All prior
# marker/affected-test/capture/head/blocker guards in cmd_pm_review_done still run
# and are unchanged; this branch never starts or reruns CI.
PM_ALREADY_GREEN_CI_RUN_ID=""
PM_ALREADY_GREEN_E2E_RUN_ID=""
pm_review_done_already_green_ok() {
  local pr="$1" head="$2"
  PM_ALREADY_GREEN_CI_RUN_ID=""
  PM_ALREADY_GREEN_E2E_RUN_ID=""
  local gate_json="/tmp/pm-ci-ready-gate-${pr}-${head}.json"
  [ -r "$gate_json" ] || return 1

  # Re-read the live head and confirm it matches the head the gate is bound to.
  local live_head
  live_head="$(gh pr view "$pr" --repo "$REPO" --json headRefOid --jq '.headRefOid' 2>/dev/null || true)"
  [ -n "$live_head" ] || return 1
  [ "$live_head" = "$head" ] || return 1

  # No pm-blocked:* label may remain, and merge state must be CLEAN.
  local labels merge_state
  labels="$(gh pr view "$pr" --repo "$REPO" --json labels --jq '[.labels[].name] | join(",")' 2>/dev/null || true)"
  printf '%s\n' "$labels" | tr ',' '\n' | grep -q '^pm-blocked:' && return 1
  merge_state="$(gh pr view "$pr" --repo "$REPO" --json mergeStateStatus --jq '.mergeStateStatus' 2>/dev/null || true)"
  [ "$merge_state" = "CLEAN" ] || return 1

  # Parse the gate JSON: the head it is bound to must equal the live head, the
  # reasons array must be EXACTLY the single already-green reason, and the
  # workflows evidence must show real current-head CI + E2E Smoke Tests SUCCESS
  # rows (not skipped/pending/failed). On success emit the two reused run IDs.
  local run_ids
  run_ids="$(python3 - "$gate_json" "$head" 2>/dev/null <<'PY'
import json
import sys

gate_path, expect_head = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(gate_path))
except Exception:
    sys.exit(1)

if str(data.get("headRefOid") or "") != expect_head:
    sys.exit(1)

reasons = data.get("reasons")
if reasons != ["current_head_ci_or_e2e_already_terminal_green"]:
    sys.exit(1)

workflows = ((data.get("artifacts") or {}).get("workflows") or {})
if str(workflows.get("state") or "") != "green":
    sys.exit(1)

rows = {}
for row in workflows.get("runs") or []:
    name = str(row.get("workflow") or "")
    state = str(row.get("state") or "")
    conclusion = str(row.get("conclusion") or "").lower()
    run_id = row.get("run_id")
    if state != "success" or conclusion != "success" or not run_id:
        continue
    rows[name] = str(run_id)

ci = rows.get("CI")
e2e = rows.get("E2E Smoke Tests")
if not ci or not e2e:
    sys.exit(1)

print(f"{ci}\t{e2e}")
PY
)"
  [ -n "$run_ids" ] || return 1
  PM_ALREADY_GREEN_CI_RUN_ID="${run_ids%%$'\t'*}"
  PM_ALREADY_GREEN_E2E_RUN_ID="${run_ids##*$'\t'}"
  [ -n "$PM_ALREADY_GREEN_CI_RUN_ID" ] || return 1
  [ -n "$PM_ALREADY_GREEN_E2E_RUN_ID" ] || return 1
  return 0
}

capture_gate_ready_transition() {
  local pr="$1" issue="$2" slot="$3" branch="$4" head="$5" source="$6" event="${7:-}"
  local block_label="pm-blocked:capture" released_slots=""

  bash "$PM_STATE" "$pr" pm-review-pending || die 1 "failed to move PR #$pr to pm-review-pending for capture-before-ci"
  remove_pm_blockers "$pr" "" >/dev/null 2>&1 || true
  gh pr edit "$pr" --repo "$REPO" --add-label "$block_label" >/dev/null || die 1 "failed to add $block_label to PR #$pr"
  if [ -n "$issue" ]; then
    gh issue edit "$issue" --repo "$REPO" --remove-label "status:todo" >/dev/null 2>&1 || true
    gh issue edit "$issue" --repo "$REPO" --remove-label "status:in-progress" >/dev/null 2>&1 || true
    gh issue edit "$issue" --repo "$REPO" --add-label "status:in-review" >/dev/null 2>&1 || true
  fi
  if [ -n "$slot" ]; then
    gh issue edit "$issue" --repo "$REPO" --remove-label "slot:${slot}" >/dev/null 2>&1 || true
    gh pr edit "$pr" --repo "$REPO" --remove-label "slot:${slot}" >/dev/null 2>&1 || true
  fi
  released_slots="$(release_target_slots "$pr" "$issue" "$branch" "capture-before-ci:$source" "$slot")"

  if ! remote_capture_only_enabled; then
    die 12 "REMOTE_CAPTURE_ONLY is disabled; local capture is diagnostic-only and cannot satisfy capture readiness. Re-enable remote capture or use capture-local-required --reason <named-infra-defect> for debugging only."
  fi
  if [ -n "$issue" ]; then
    cmd_capture_remote_dispatch --pr "$pr" --issue "$issue" --head "$head"
  else
    cmd_capture_remote_dispatch --pr "$pr" --head "$head"
  fi
  return 0
}

pm_review_loop_decision_blocker() {
  local pr="$1" head="$2" issue="${3:-}"
  if [ -z "$issue" ]; then issue="$(issue_from_pr "$pr")"; fi
  if [ -f "$REVIEW_BUDGET" ]; then
    local -a budget_args=(--pr "$pr" --head "$head" --write --json --live-pr)
    [ -n "$issue" ] && budget_args+=(--issue "$issue")
    python3 "$REVIEW_BUDGET" "${budget_args[@]}" >/dev/null 2>&1 || true
  fi
  python3 - "$pr" "$head" <<'PY' 2>/dev/null || true
import glob
import json
import re
import sys
from pathlib import Path

pr, live_head = sys.argv[1:3]
decisions = {
    "final_verified_patch",
    "split_and_reimplement",
    "rescope_required",
    "split_pr",
    "send_verified_patch",
    "send_instruction_packet",
    "send_test_harness_packet",
    "override_with_evidence",
    "escalate_product_decision",
}

def field(text: str, name: str) -> str:
    m = re.search(rf"(?im)^\s*{re.escape(name)}\s*:\s*(.+?)\s*$", text)
    return m.group(1).strip() if m else ""

loop_path = Path(f"/tmp/pm-review-loop-{pr}.json")

# A resolved rescope decision is the lifecycle authority for a review cap. It
# must be evaluated before the generated budget JSON, otherwise the same
# companion markers recreate rescue_required and make rescope-decide unable to
# relieve its own gate.
rescope_path = Path(f"/tmp/pm-rescope-pr-{pr}.json")
try:
    rescope = json.loads(rescope_path.read_text(encoding="utf-8"))
except Exception:
    rescope = {}
if (
    rescope
    and str(rescope.get("headRefOid") or "") == live_head
    and str(rescope.get("status") or "") == "resolved"
):
    terminal = str(rescope.get("terminal_decision") or "")
    proof = str(rescope.get("terminal_decision_proof") or "")
    if terminal == "override_with_evidence":
        # Deliberately emit no blocker: PM already adjudicated the repeated
        # companion findings and recorded the evidence that clears runtime risk.
        raise SystemExit(0)
    if terminal == "final_verified_patch":
        recovery = rescope.get("final_patch_recovery")
        if isinstance(recovery, dict):
            applied_head = str(recovery.get("applied_headRefOid") or "")
            recovery_live_head = str(recovery.get("live_headRefOid") or "")
            proof_sha256 = str(recovery.get("proof_sha256") or "")
            approval_sha256 = str(recovery.get("approval_sha256") or "")
            if (
                applied_head == live_head
                and recovery_live_head == live_head
                and re.fullmatch(r"[0-9a-f]{64}", proof_sha256)
                and re.fullmatch(r"[0-9a-f]{64}", approval_sha256)
            ):
                # rescope-final-patch-applied already verified the approved
                # patch and persisted its exact live-head recovery receipt.
                # Once that receipt is complete, ordinary exact-head review may
                # continue; stale or incomplete recovery metadata still blocks.
                raise SystemExit(0)
    if terminal:
        print(
            f"rescope_terminal_decision marker={rescope_path} decision={terminal} "
            f"proof={proof or 'none'} required_pm_action=execute_{terminal}"
        )
        raise SystemExit(0)

# Prefer the PM-authored current-head loop decision over the generic budget
# classification. Non-override decisions remain blockers with their exact next
# action; a PM PASS plus an explicit override is terminal relief.
for raw in sorted(glob.glob(f"/tmp/pm-claude-code-review-{pr}-*.md"), key=lambda p: Path(p).stat().st_mtime, reverse=True):
    path = Path(raw)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        continue
    marker_head = field(text, "headRefOid")
    if marker_head != live_head:
        continue
    decision = field(text, "loop_reduction_decision").split()[0]
    pm_verdict = field(text, "PM_CLAUDE_REVIEW").split()[0].upper()
    if decision == "override_with_evidence" and pm_verdict == "PASS":
        raise SystemExit(0)
    if decision in decisions:
        action = field(text, "required_pm_action") or decision
        print(f"same_head_loop_decision marker={path} decision={decision} required_pm_action={action}")
        raise SystemExit(0)

try:
    budget = json.loads(loop_path.read_text(encoding="utf-8"))
except Exception:
    budget = {}
if budget and str(budget.get("headRefOid") or "") == live_head:
    action = str(budget.get("required_pm_action") or "")
    decision = str(budget.get("decision") or "")
    current_events = [
        event for event in (budget.get("events") or [])
        if str(event.get("headRefOid") or "") == live_head
        and event.get("blocking") is not False
    ]
    current_counts = {}
    current_round_counts = {}
    for event in current_events:
        klass = str(event.get("class") or "").strip()
        if klass:
            current_counts[klass] = current_counts.get(klass, 0) + 1
        round_key = (str(event.get("layer") or ""), str(event.get("review_type") or ""))
        current_round_counts[round_key] = current_round_counts.get(round_key, 0) + 1
    same_class_hot = sorted(klass for klass, count in current_counts.items() if count >= 2)
    explicit_cap = (
        "explicit_cap_marker" in [str(item) for item in (budget.get("cap_reasons") or [])]
        and any(count >= 3 for count in current_round_counts.values())
    )
    if current_events and (same_class_hot or explicit_cap) and (
        action in {"run_pr_rescue", "run_pm_codex_pr_rescue", "escalate_to_cto_and_release_slot"}
        or decision == "rescue_required"
    ):
        total = budget.get("total_events", "unknown")
        hot = ",".join(str(x) for x in same_class_hot) if isinstance(same_class_hot, list) else str(same_class_hot)
        print(
            f"review_budget_decision marker={loop_path} decision={decision or 'rescue_required'} "
            f"required_pm_action={action or 'run_pr_rescue'} total_events={total} same_class_hot={hot or 'none'}"
        )
        raise SystemExit(0)

try:
    data = json.loads(loop_path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(0)
# The budget script (pr-review-budget.py) is the authoritative writer of the
# review-loop decision. Its same-class hot and round-cap counts are
# round-deduplicated: one (layer, review_type, head, mtime) round legitimately
# carries several blocker records, and the legacy event recount below would
# re-block the next review from a within-round multi-record budget. When an
# exact-head schema-v5 budget admits the review (within_budget /
# normal_review_allowed), exit without fallback so the next ordinary review is
# admitted. Stale heads, older schema versions, missing budget files, and
# rescue_required decisions all keep the legacy recount as the fail-closed
# backstop.
if (
    str(data.get("schema") or "") == "heydonna_pr_review_budget"
    and data.get("version") == 5
    and str(data.get("headRefOid") or "") == live_head
    and str(data.get("decision") or "") == "within_budget"
    and str(data.get("required_pm_action") or "") == "normal_review_allowed"
):
    raise SystemExit(0)
events = data.get("events") or []
latest_same_head = [
    e for e in events
    if str(e.get("headRefOid") or "") == live_head
    and e.get("blocking") is not False
]
counts = {}
round_counts = {}
for event in latest_same_head:
    klass = str(event.get("class") or "").strip()
    if klass:
        counts[klass] = counts.get(klass, 0) + 1
    round_key = (str(event.get("layer") or ""), str(event.get("review_type") or ""))
    round_counts[round_key] = round_counts.get(round_key, 0) + 1
hot = [(klass, count) for klass, count in counts.items() if count >= 2]
hard_cap = any(count >= 3 for count in round_counts.values())
if latest_same_head and (hot or hard_cap):
    if not hot:
        hot = [("explicit_cap_marker", max(round_counts.values()))]
    hot.sort(key=lambda item: (-item[1], item[0]))
    klass, count = hot[0]
    print(f"same_head_review_loop class={klass} count={count} marker={loop_path} required_pm_action=run_pr_rescue")
PY
}

cto_review_hold_state() {
  local reason="$1" classes="${2:-none}" current_head_artifacts="${3:-0}"
  case "$reason" in
    code-review-cap|plan-review-cap|review-loop-circuit-breaker|review-cap-assignment-refused)
      if [ "$classes" = "none" ] && [ "$current_head_artifacts" = "0" ]; then
        printf '%s\n' "pm-review-pending"
        return 0
      fi
      ;;
  esac
  printf '%s\n' "rescope-required"
}

review_cap_requires_pm_rescue() {
  case "$1" in
    *review-cap*|review-loop-circuit-breaker|review-cap-assignment-refused)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

require_pm_fable_rescue_failure() {
  local proof="$1" reason="$2" pr="$3" issue="$4" head="$5"
  review_cap_requires_pm_rescue "$reason" || return 0
  [ -n "$proof" ] || die 42 \
    "PM_FABLE_RESCUE_REQUIRED reason=$reason action=run Skill(pm-codex-pr-rescue); CTO escalation is allowed only after one exact-head Fable rescue returns a MoP-validated failure packet"
  [ -f "$proof" ] || die 42 "PM_FABLE_RESCUE_PROOF_MISSING proof=$proof"
  local args=(--mode failure --packet "$proof")
  [ -z "$pr" ] || args+=(--pr "$pr")
  [ -z "$issue" ] || args+=(--issue "$issue")
  [ -z "$head" ] || args+=(--head "$head")
  local result
  if ! result="$(python3 "$REVIEW_RESCUE_AUTHORIZER" "${args[@]}" 2>&1)"; then
    die 42 "PM_FABLE_RESCUE_PROOF_INVALID reason=$reason proof=$proof result=$result"
  fi
  printf 'PM_FABLE_RESCUE_FAILURE_VERIFIED reason=%s proof=%s result=%s\n' \
    "$reason" "$proof" "$result" >&2
}

packet_allows_review_loop_assignment() {
  local packet="${1:-}" pr="${2:-}" head="${3:-}"
  [ -n "$packet" ] || return 1
  [ -f "$packet" ] || return 1
  [ -n "$pr" ] || return 1
  [ -n "$head" ] || return 1
  [ -f "$REVIEW_RESCUE_AUTHORIZER" ] || return 1
  python3 "$REVIEW_RESCUE_AUTHORIZER" --packet "$packet" --pr "$pr" --head "$head" >/dev/null 2>&1
}

rescue_packet_authorizes_final_head() {
  local packet="${1:-}" pr="${2:-}" final_head="${3:-}"
  [ -f "$packet" ] || return 1
  [ -d "$PM_RESCUE_GIT_REPO/.git" ] || return 1
  local parsed terminal source_head patch_file
  parsed="$(
python3 - "$packet" 2>/dev/null <<'PY'
import os
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
def field(name):
    match = re.search(rf"(?m)^{re.escape(name)}:[ \t]*(.+?)\s*$", text)
    return match.group(1).strip() if match else ""

terminal = field("PM_CLAUDE_PR_RESCUE")
if terminal not in {"PATCH_READY", "NO_PATCH_REQUIRED"}:
    raise SystemExit(1)
if field("terminal") != terminal:
    raise SystemExit(1)
if field("skip_further_review") != "true":
    raise SystemExit(1)
source = field("head_or_plan_sha")
patch = field("patch_file")
if not re.fullmatch(r"[0-9a-f]{40}", source):
    raise SystemExit(1)
if terminal == "PATCH_READY":
    artifact_root = Path(
        os.environ.get(
            "PM_FABLE_RESCUE_ARTIFACT_ROOT",
            "/Users/rajiv/.claude/control-plane-artifacts",
        )
    ).expanduser().resolve(strict=False)
    patch_path = Path(patch).expanduser()
    if (
        not patch_path.is_absolute()
        or patch_path.parent.resolve(strict=False) != artifact_root
    ):
        raise SystemExit(1)
if terminal == "NO_PATCH_REQUIRED" and patch != "none":
    raise SystemExit(1)
print(terminal + "\t" + source + "\t" + patch)
PY
)" || return 1
  IFS=$'\t' read -r terminal source_head patch_file <<<"$parsed"
  [ -n "$source_head" ] || return 1
  local authorizer_mode="patch"
  if [ "$terminal" = "NO_PATCH_REQUIRED" ]; then
    # CTO-adjudicated NO_PATCH_REQUIRED rescue consumption (incident
    # cp-repair:cto-direct-admission-unexecutable:7268): the canonical
    # one-use override at /tmp/pm-ci-start-override-<pr>-<head>.ok lets a
    # sound NO_PATCH_REQUIRED terminal cross the transition even though its
    # envelope shape (MoP event provenance, producer/consumer grammar,
    # receipt command shape) is not valid for the ordinary --mode patch gate.
    # The override is written only by the CTO direct repair and is validated
    # end-to-end by --mode cto-adjudicated; ordinary PATCH_READY/failure
    # packets keep the unchanged --mode patch path.
    local adjudication_override="/tmp/pm-ci-start-override-${pr}-${final_head}.ok"
    if [ -f "$adjudication_override" ] \
      && grep -qE '^provenance:[[:space:]]*cto-no-patch-rescue-ci-admission($|[[:space:]])' "$adjudication_override" \
      && grep -qE '^consumed:[[:space:]]*no($|[[:space:]])' "$adjudication_override"; then
      authorizer_mode="cto-adjudicated"
    fi
  fi
  python3 "$REVIEW_RESCUE_AUTHORIZER" \
    --mode "$authorizer_mode" --packet "$packet" --pr "$pr" --head "$source_head" \
    >/dev/null 2>&1 || return 1
  if [ "${PM_RESCUE_SKIP_FETCH:-0}" != "1" ]; then
    git -C "$PM_RESCUE_GIT_REPO" fetch --quiet --no-tags origin "pull/$pr/head" \
      >/dev/null 2>&1 || return 1
  fi
  if [ "$terminal" = "NO_PATCH_REQUIRED" ]; then
    [ "$final_head" = "$source_head" ] || return 1
    printf 'PM_FABLE_RESCUE_FINAL_HEAD_AUTHORIZED pr=%s source=%s final=%s terminal=%s packet=%s\n' \
      "$pr" "$source_head" "$final_head" "$terminal" "$packet" >&2
    return 0
  fi
  [ -f "$patch_file" ] || return 1
  local parent final_tree applied_tree index_file
  parent="$(git -C "$PM_RESCUE_GIT_REPO" rev-parse "${final_head}^" 2>/dev/null)" || return 1
  [ "$parent" = "$source_head" ] || return 1
  final_tree="$(git -C "$PM_RESCUE_GIT_REPO" rev-parse "${final_head}^{tree}" 2>/dev/null)" || return 1
  index_file="$(mktemp "${TMPDIR:-/tmp}/pm-rescue-final-head-index.XXXXXX")" || return 1
  rm -f "$index_file"
  if ! GIT_INDEX_FILE="$index_file" git -C "$PM_RESCUE_GIT_REPO" read-tree "$source_head" \
      >/dev/null 2>&1 \
    || ! GIT_INDEX_FILE="$index_file" git -C "$PM_RESCUE_GIT_REPO" apply --cached "$patch_file" \
      >/dev/null 2>&1; then
    rm -f "$index_file"
    return 1
  fi
  applied_tree="$(GIT_INDEX_FILE="$index_file" git -C "$PM_RESCUE_GIT_REPO" write-tree 2>/dev/null)" || {
    rm -f "$index_file"
    return 1
  }
  rm -f "$index_file"
  [ "$applied_tree" = "$final_tree" ] || return 1
  printf 'PM_FABLE_RESCUE_FINAL_HEAD_AUTHORIZED pr=%s source=%s final=%s terminal=%s packet=%s\n' \
    "$pr" "$source_head" "$final_head" "$terminal" "$packet" >&2
}

packet_supersedes_review_loop() {
  local packet="${1:-}" pr="${2:-}" head="${3:-}"
  [ -n "$packet" ] || return 1
  [ -f "$packet" ] || return 1
  [ -n "$pr" ] || return 1
  [ -n "$head" ] || return 1
  python3 - "$packet" "$pr" "$head" "/tmp/pm-rework-packet-publish-${pr}.json" "/tmp/pm-rework-packet-fetch-${pr}.json" <<'PY' >/dev/null 2>&1
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

packet_path, expected_pr, expected_head = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
metadata_paths = [Path(raw) for raw in sys.argv[4:]]

def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None

try:
    packet_text = packet_path.read_text(encoding="utf-8")
except Exception:
    raise SystemExit(1)
normalized = packet_text.rstrip() + "\n"
packet_id = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
metadata = None
for metadata_path in metadata_paths:
    try:
        candidate = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        continue
    if (
        int(candidate.get("pr") or 0) == expected_pr
        and str(candidate.get("head") or "") == expected_head
        and str(candidate.get("packet_id") or "") == packet_id
    ):
        metadata = candidate
        break
if metadata is None:
    raise SystemExit(1)
packet_time = parse_time(metadata.get("created_at"))
if packet_time is None:
    raise SystemExit(1)
try:
    loop = json.loads(Path(f"/tmp/pm-review-loop-{expected_pr}.json").read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(1)
blocking_times = []
for event in loop.get("events") or []:
    if str(event.get("headRefOid") or "") != expected_head or event.get("blocking") is False:
        continue
    try:
        blocking_times.append(datetime.fromtimestamp(float(event.get("mtime")), timezone.utc))
    except Exception:
        event_time = parse_time(event.get("created_at"))
        if event_time is not None:
            blocking_times.append(event_time)
if blocking_times and packet_time <= max(blocking_times):
    raise SystemExit(1)
PY
}

packet_matches_resolved_final_patch() {
  local packet="${1:-}" pr="${2:-}" head="${3:-}"
  [ -n "$packet" ] || return 1
  [ -f "$packet" ] || return 1
  [ -n "$pr" ] || return 1
  [ -n "$head" ] || return 1
  python3 - "$packet" "$pr" "$head" <<'PY' >/dev/null 2>&1
import json
import sys
from pathlib import Path

packet_path, expected_pr, expected_head = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
marker_path = Path(f"/tmp/pm-rescope-pr-{expected_pr}.json")
try:
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(1)

if marker.get("status") != "resolved":
    raise SystemExit(1)
if marker.get("pr") != expected_pr:
    raise SystemExit(1)
if str(marker.get("headRefOid") or "") != expected_head:
    raise SystemExit(1)
if str(marker.get("terminal_decision") or "").lower() != "final_verified_patch":
    raise SystemExit(1)

proof = str(marker.get("terminal_decision_proof") or "").strip()
if not proof:
    raise SystemExit(1)
try:
    if Path(proof).resolve(strict=True) != packet_path.resolve(strict=True):
        raise SystemExit(1)
except OSError:
    raise SystemExit(1)
PY
}

packet_allows_override_verification_assignment() {
  local packet="${1:-}" pr="${2:-}" head="${3:-}"
  [ -n "$packet" ] || return 1
  [ -f "$packet" ] || return 1
  [ -n "$pr" ] || return 1
  [ -n "$head" ] || return 1
  python3 - "$packet" "$pr" "$head" <<'PY' >/dev/null 2>&1
import hashlib
import json
import sys
from pathlib import Path

packet_path, expected_pr, expected_head = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
marker_path = Path(f"/tmp/pm-rescope-pr-{expected_pr}.json")

try:
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(1)

if marker.get("status") != "resolved":
    raise SystemExit(1)
if str(marker.get("headRefOid") or "") != expected_head:
    raise SystemExit(1)
if str(marker.get("terminal_decision") or "").lower() != "override_with_evidence":
    raise SystemExit(1)

required = {
    "kind": "affected-test-proof",
    "actor": "pm",
    "verification_only": True,
    "no_code_changes": True,
    "no_commit": True,
    "no_push": True,
}
if any(packet.get(key) != value for key, value in required.items()):
    raise SystemExit(1)
if packet.get("pr") != expected_pr or str(packet.get("headRefOid") or "") != expected_head:
    raise SystemExit(1)

plan_raw = str(packet.get("affected_test_plan") or "")
digest = str(packet.get("commands_sha256") or "")
if not plan_raw or not digest:
    raise SystemExit(1)
plan_path = Path(plan_raw)
try:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(1)
if plan.get("pr") != expected_pr or str(plan.get("headRefOid") or "") != expected_head:
    raise SystemExit(1)
commands = plan.get("commands")
if not isinstance(commands, list) or not commands:
    raise SystemExit(1)
for entry in commands:
    if not isinstance(entry, dict) or not str(entry.get("command") or "").strip():
        raise SystemExit(1)
canonical = json.dumps(commands, sort_keys=True, separators=(",", ":")).encode("utf-8")
if hashlib.sha256(canonical).hexdigest() != digest:
    raise SystemExit(1)
PY
}


review_loop_blocker_allows_instruction_delivery() {
  local blocker="${1:-}"
  case "$blocker" in
    *"same_head_loop_decision "*" decision=send_instruction_packet "*) return 0 ;;
    *) return 1 ;;
  esac
}

pm_review_meta_get() {
  local pr="$1" key="$2" file
  file="$(pm_review_meta_file "$pr")"
  [ -f "$file" ] || return 1
  python3 - "$file" "$key" <<'PYSUB'
import json
import sys
from pathlib import Path
try:
    data = json.loads(Path(sys.argv[1]).read_text())
except Exception:
    raise SystemExit(1)
value = data
for part in sys.argv[2].split('.'):
    value = value.get(part) if isinstance(value, dict) else None
if value is None:
    raise SystemExit(1)
print(value)
PYSUB
}

remove_pm_blockers() {
  # Capture is an independent exact-head product-proof gate. Preserve it by
  # default so new callers cannot silently clear it. Callers that immediately
  # restore capture, or post-merge cleanup, must opt out explicitly with "".
  local pr="$1" preserved="${2-pm-blocked:capture}" labels lbl removed=""
  labels="$(gh pr view "$pr" --repo "$REPO" --json labels --jq '.labels[].name | select(startswith("pm-blocked:"))' 2>/dev/null || true)"
  while IFS= read -r lbl; do
    [ -n "$lbl" ] || continue
    case " $preserved " in
      *" $lbl "*) continue ;;
    esac
    gh pr edit "$pr" --repo "$REPO" --remove-label "$lbl" >/dev/null 2>&1 || true
    removed="${removed}${removed:+ }${lbl}"
  done <<EOF
$labels
EOF
  printf '%s' "$removed"
}

resume_product_wait_after_rework_delivery() {
  local pr="$1" issue="$2" slot="$3" head="$4" handoff="$5" product_wait="$6"
  [ "$product_wait" = "1" ] || return 0

  gh pr edit "$pr" --repo "$REPO" --remove-label "pm-blocked:product" >/dev/null \
    || die 1 "failed to clear pm-blocked:product while resuming PR #$pr rework"
  resolve_pr_obligation_kinds "$pr" "$issue" "product_wait_resumed" \
    "slot=$slot head=$head handoff=$handoff" product_decision_wait
  record_event --source pm-transition --event product_wait_resumed \
    --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --slot "$slot" \
    --head-sha "$head" --payload "handoff=$handoff" --dedupe
}

promotion_proof_candidate_ok() {
  local proof="$1" pr="$2" state="$3" head="$4" marker latest_rework_sha
  [ -s "$proof" ] || return 1
  grep -qE "^PR:[[:space:]]*$pr[[:space:]]*$" "$proof" 2>/dev/null || return 1
  grep -qE "^headRefOid:[[:space:]]*$head[[:space:]]*$" "$proof" 2>/dev/null || return 1
  grep -qE '^review_provenance:[[:space:]]*ok($|[[:space:]])' "$proof" 2>/dev/null || return 1

  case "$state" in
    qa-passed-awaiting-ci)
      grep -qE '^CURRENT_HEAD_REVIEW_OK[[:space:]]*$' "$proof" 2>/dev/null || return 1
      ;;
    merge-ready)
      grep -qE '^READY_PACKET:[[:space:]]*PASS($|[[:space:]])' "$proof" 2>/dev/null || return 1
      ;;
    *)
      return 1
      ;;
  esac

  # A rework proof is only replaceable when it carries the corresponding
  # current-head PM review marker or an exact-head terminal Fable rescue
  # packet. This prevents a failed/manual attempt from replacing a valid proof
  # with authority that cannot pass the promotion guard.
  latest_rework_sha="$(awk -F': ' '/^latest_rework_sha:[[:space:]]*/ {print $2; exit}' "$proof" 2>/dev/null || true)"
  if [ -n "$latest_rework_sha" ] && [ "$latest_rework_sha" != "none" ]; then
    if grep -qE '^PM_CLAUDE_REVIEW:[[:space:]]*PASS[[:space:]]*$' "$proof" 2>/dev/null; then
      marker="$(awk -F': ' '/^claude_review_marker:[[:space:]]*/ {print $2; exit}' "$proof" 2>/dev/null || true)"
    elif grep -qE '^PM_FABLE_RESCUE:[[:space:]]*PASS[[:space:]]*$' "$proof" 2>/dev/null; then
      marker="$(awk -F': ' '/^fable_rescue_packet:[[:space:]]*/ {print $2; exit}' "$proof" 2>/dev/null || true)"
    elif grep -qE '^CTO_EXACT_TUPLE_CI_ADMISSION:[[:space:]]*PASS[[:space:]]*$' "$proof" 2>/dev/null; then
      # CTO-adjudicated exact-tuple / cancelled-run CI-start admission: the
      # readiness gate already performed full sealed-packet validation and
      # one-time atomic consumption; the promotion writer only re-binds the
      # canonical override path + PR/head/provenance/mode so a bogus or
      # foreign marker file can never authorize a proof replacement.
      marker="$(awk -F': ' '/^cto_exact_tuple_override:[[:space:]]*/ {print $2; exit}' "$proof" 2>/dev/null || true)"
      [ -n "$marker" ] || return 1
      [ "$marker" = "/tmp/pm-ci-start-override-${pr}-${head}.ok" ] || return 1
      [ -f "$marker" ] || return 1
      grep -Fqx "headRefOid: $head" "$marker" 2>/dev/null || return 1
      grep -qE "^PR:[[:space:]]*${pr}([[:space:]]|$)" "$marker" 2>/dev/null || return 1
      grep -qE '^provenance:[[:space:]]*(cto-exact-tuple-ci-admission|cto-cancelled-run-local-preflight-ci-admission|cto-local-preflight-rebind-ci-admission)($|[[:space:]])' "$marker" 2>/dev/null || return 1
      grep -qE '^mode:[[:space:]]*(post-capture-preflight|vacuous-red|cancelled-run-local-preflight|local-preflight-rebind)($|[[:space:]])' "$marker" 2>/dev/null || return 1
      return 0
    else
      return 1
    fi
    [ -n "$marker" ] && [ -f "$marker" ] || return 1
  fi
  return 0
}

normalized_review_provenance() {
  local review_verdict="${1:-unknown}" pm_accept_ready="${2:-false}" genuine_review_ok="${3:-false}"
  if [ "$pm_accept_ready" = "true" ] || [ "$review_verdict" = "PM_ACCEPT_READY" ]; then
    printf 'ok'
  elif [ "$genuine_review_ok" = "true" ] \
    && { [ "$review_verdict" = "approved:APPROVE" ] || [ "$review_verdict" = "APPROVE" ]; }; then
    printf 'ok'
  else
    printf '%s' "$review_verdict"
  fi
}

slot_ready_post_release_recovery_ok() {
  local event="$1" pr="$2" issue="$3" slot="$4" head="$5" branch="$6" assignment_epoch="$7"
  local live_draft="$8" live_labels="$9" issue_labels="${10}" owners="${11}" mop_json="${12}" packets_json="${13}"
  SLOT_READY_RECOVERY_EVENT="$event" \
  SLOT_READY_RECOVERY_PR="$pr" \
  SLOT_READY_RECOVERY_ISSUE="$issue" \
  SLOT_READY_RECOVERY_SLOT="$slot" \
  SLOT_READY_RECOVERY_HEAD="$head" \
  SLOT_READY_RECOVERY_BRANCH="$branch" \
  SLOT_READY_RECOVERY_EPOCH="$assignment_epoch" \
  SLOT_READY_RECOVERY_DRAFT="$live_draft" \
  SLOT_READY_RECOVERY_PR_LABELS="$live_labels" \
  SLOT_READY_RECOVERY_ISSUE_LABELS="$issue_labels" \
  SLOT_READY_RECOVERY_OWNERS="$owners" \
  SLOT_READY_RECOVERY_MOP="$mop_json" \
  SLOT_READY_RECOVERY_PACKETS="$packets_json" \
  python3 <<'PY'
import json
import os

try:
    slot = int(os.environ["SLOT_READY_RECOVERY_SLOT"])
    issue = int(os.environ["SLOT_READY_RECOVERY_ISSUE"])
    pr = int(os.environ["SLOT_READY_RECOVERY_PR"])
    epoch = int(os.environ["SLOT_READY_RECOVERY_EPOCH"])
    mop = json.loads(os.environ["SLOT_READY_RECOVERY_MOP"])
    packets = json.loads(os.environ["SLOT_READY_RECOVERY_PACKETS"])
except (KeyError, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)

if os.environ.get("SLOT_READY_RECOVERY_DRAFT") != "false":
    raise SystemExit(1)
if os.environ.get("SLOT_READY_RECOVERY_OWNERS", "").strip():
    raise SystemExit(1)

pr_labels = set(os.environ.get("SLOT_READY_RECOVERY_PR_LABELS", "").splitlines())
issue_labels = set(os.environ.get("SLOT_READY_RECOVERY_ISSUE_LABELS", "").splitlines())
if any(label.startswith("slot:") for label in pr_labels | issue_labels):
    raise SystemExit(1)
if "status:in-review" not in issue_labels:
    raise SystemExit(1)

free_same_epoch = (
    mop.get("occupied") is False
    and mop.get("assignment_epoch") == epoch
    and any(mop.get(key) is not None for key in ("issue", "pr", "branch", "head_sha")) is False
)
reassigned_newer_epoch = (
    mop.get("occupied") is True
    and isinstance(mop.get("assignment_epoch"), int)
    and mop.get("assignment_epoch") > epoch
    and (mop.get("issue") is not None or mop.get("pr") is not None)
    and mop.get("issue") != issue
    and mop.get("pr") != pr
    and mop.get("branch") != os.environ["SLOT_READY_RECOVERY_BRANCH"]
)
if (
    mop.get("slot") != slot
    or mop.get("dnd") is not False
    or not (free_same_epoch or reassigned_newer_epoch)
):
    raise SystemExit(1)

event = os.environ["SLOT_READY_RECOVERY_EVENT"]
head = os.environ["SLOT_READY_RECOVERY_HEAD"]
branch = os.environ["SLOT_READY_RECOVERY_BRANCH"]
reason = "refused to replace promotion proof for PR #%d: candidate was invalid or could not be written atomically" % pr
retry_reason = "slot-ready artifact tuple does not match authoritative occupied slot/epoch/issue/PR/branch/turn state (result=mismatch)"

def exact_overwritten_retry_chain(row):
    if row.get("resolution_reason") != retry_reason:
        return False
    rejected_at = row.get("rejected_at")
    updated_at = row.get("updated_at")
    if not rejected_at or not updated_at or rejected_at >= updated_at:
        return False
    try:
        payload = json.loads(row.get("payload_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    return (
        payload.get("event_type") == "slot_ready"
        and payload.get("status") == "pending"
        and payload.get("pr_is_draft") is True
        and payload.get("assignment_epoch") == epoch
    )

matching = [
    row for row in packets
    if row.get("packet_type") == "slot_ready"
    and row.get("status") in ("pending", "rejected")
    and row.get("rejected_at")
    and row.get("event_file") == event
    and row.get("pr") == pr
    and row.get("issue") == issue
    and row.get("slot") == slot
    and row.get("head_sha") == head
    and row.get("branch") == branch
    and (
        row.get("resolution_reason") == reason
        or exact_overwritten_retry_chain(row)
    )
]
raise SystemExit(0 if len(matching) == 1 else 1)
PY
}

write_promotion_proof_atomic() {
  local proof="$1" pr="$2" state="$3" head="$4" content="$5" tmp
  [ -n "$proof" ] && [ -n "$pr" ] && [ -n "$state" ] && [ -n "$head" ] || return 1
  tmp="$(mktemp "${proof}.tmp.XXXXXX" 2>/dev/null)" || return 1
  if ! printf '%s\n' "$content" >"$tmp" || ! promotion_proof_candidate_ok "$tmp" "$pr" "$state" "$head"; then
    rm -f "$tmp"
    return 1
  fi
  if ! mv -f "$tmp" "$proof"; then
    rm -f "$tmp"
    return 1
  fi
  return 0
}

merge_ready_proof_path() {
  local pr="$1"
  printf '/tmp/pm-state-promotion-proof-%s-merge-ready.ok' "$pr"
}

assert_no_unresolved_review_threads() {
  local pr="$1" head="$2" response
  response="$(
    gh api graphql --paginate --slurp \
      -f owner=heydonna-app \
      -f name=heydonna-app \
      -F pr="$pr" \
      -f query='
        query($owner:String!,$name:String!,$pr:Int!,$endCursor:String){
          repository(owner:$owner,name:$name){
            pullRequest(number:$pr){
              headRefOid
              reviewThreads(first:100,after:$endCursor){
                nodes{id isResolved}
                pageInfo{hasNextPage endCursor}
              }
            }
          }
        }' 2>/dev/null
  )" || die 1 "cannot read live review threads for PR #$pr; merge-ready fails closed"

  REVIEW_THREADS_RESPONSE="$response" python3 - "$pr" "$head" <<'PYSUB' \
    || die 1 "PR #$pr has unresolved review threads or invalid live thread evidence; merge-ready fails closed"
import json
import os
import sys

pr = sys.argv[1]
expected_head = sys.argv[2]
try:
    pages = json.loads(os.environ["REVIEW_THREADS_RESPONSE"])
except Exception:
    raise SystemExit(1)
if not isinstance(pages, list) or not pages:
    raise SystemExit(1)

unresolved = []
for page in pages:
    try:
        pull = page["data"]["repository"]["pullRequest"]
        threads = pull["reviewThreads"]
    except (KeyError, TypeError):
        raise SystemExit(1)
    if not isinstance(pull, dict) or pull.get("headRefOid") != expected_head:
        raise SystemExit(1)
    nodes = threads.get("nodes")
    page_info = threads.get("pageInfo")
    if not isinstance(nodes, list) or not isinstance(page_info, dict):
        raise SystemExit(1)
    has_next_page = page_info.get("hasNextPage")
    end_cursor = page_info.get("endCursor")
    if not isinstance(has_next_page, bool):
        raise SystemExit(1)
    if has_next_page and (not isinstance(end_cursor, str) or not end_cursor):
        raise SystemExit(1)
    for node in nodes:
        if (
            not isinstance(node, dict)
            or not isinstance(node.get("id"), str)
            or not node["id"]
            or not isinstance(node.get("isResolved"), bool)
        ):
            raise SystemExit(1)
        if node["isResolved"] is False:
            unresolved.append(node["id"])

last_page_info = pages[-1]["data"]["repository"]["pullRequest"]["reviewThreads"]["pageInfo"]
if last_page_info["hasNextPage"] is not False:
    raise SystemExit(1)
if unresolved:
    print(
        f"unresolved review threads on PR #{pr}: {','.join(unresolved)}",
        file=sys.stderr,
    )
    raise SystemExit(1)
PYSUB
}

merge_ready_proof_ok() {
  local pr="$1" head="$2" proof
  proof="$(merge_ready_proof_path "$pr")"
  [ -f "$proof" ] || return 1
  grep -qE '^READY_PACKET:[[:space:]]*PASS($|[[:space:]])' "$proof" 2>/dev/null || return 1
  # Exact-tuple authorization (reviewer BLOCK exact_tuple_authorization,
  # ordinal-2): the packet's PR identity must equal this transition's PR. A
  # missing or different PR field fails the authoritative merge-ready proof
  # validator, so validate-ready-proof/merge-ready never promote the wrong PR.
  grep -qE "^PR:[[:space:]]*${pr}[[:space:]]*$" "$proof" 2>/dev/null || return 1
  grep -qE "^headRefOid:[[:space:]]*$head[[:space:]]*$" "$proof" 2>/dev/null || return 1
  grep -qE '^review_provenance:[[:space:]]*ok($|[[:space:]])' "$proof" 2>/dev/null || return 1
  return 0
}

merge_ready_proof_normalize_legacy_head() {
  local pr="$1" head="$2" proof tmp
  proof="$(merge_ready_proof_path "$pr")"
  [ -f "$proof" ] || return 0
  grep -qE "^headRefOid:[[:space:]]*$head[[:space:]]*$" "$proof" 2>/dev/null && return 0
  grep -qE '^headRefOid:[[:space:]]*' "$proof" 2>/dev/null && return 0
  grep -qE "^head:[[:space:]]*$head[[:space:]]*$" "$proof" 2>/dev/null || return 0
  grep -qE '^READY_PACKET:[[:space:]]*PASS($|[[:space:]])' "$proof" 2>/dev/null || return 0
  grep -qE '^review_provenance:[[:space:]]*ok($|[[:space:]])' "$proof" 2>/dev/null || return 0
  tmp="${proof}.$$"
  awk -v head="$head" '
    { print }
    /^head:[[:space:]]*/ && !done { print "headRefOid: " head; done=1 }
    END { if (!done) print "headRefOid: " head }
  ' "$proof" > "$tmp" && mv "$tmp" "$proof"
}

merge_ready_proof_error() {
  local pr="$1" head="$2" proof
  proof="$(merge_ready_proof_path "$pr")"
  if [ ! -f "$proof" ]; then
    printf 'missing proof file %s' "$proof"
    return 0
  fi
  if ! grep -qE '^READY_PACKET:[[:space:]]*PASS($|[[:space:]])' "$proof" 2>/dev/null; then
    printf 'proof %s missing READY_PACKET: PASS' "$proof"
    return 0
  fi
  if ! grep -qE "^PR:[[:space:]]*${pr}[[:space:]]*$" "$proof" 2>/dev/null; then
    if grep -qE '^PR:[[:space:]]*[0-9]+([[:space:]]|$)' "$proof" 2>/dev/null; then
      printf 'proof %s carries a different PR identity; expected PR: %s' "$proof" "$pr"
    else
      printf 'proof %s missing PR: %s' "$proof" "$pr"
    fi
    return 0
  fi
  if ! grep -qE "^headRefOid:[[:space:]]*$head[[:space:]]*$" "$proof" 2>/dev/null; then
    if grep -qE '^headRefOid:[[:space:]]*' "$proof" 2>/dev/null; then
      printf 'proof %s has stale headRefOid; expected current headRefOid: %s' "$proof" "$head"
      return 0
    fi
    if grep -qE "^head:[[:space:]]*$head[[:space:]]*$" "$proof" 2>/dev/null; then
      printf 'proof %s uses legacy head: field; expected headRefOid: %s' "$proof" "$head"
    else
      printf 'proof %s missing current headRefOid: %s' "$proof" "$head"
    fi
    return 0
  fi
  if ! grep -qE '^review_provenance:[[:space:]]*ok($|[[:space:]])' "$proof" 2>/dev/null; then
    printf 'proof %s missing review_provenance: ok' "$proof"
    return 0
  fi
  printf 'unknown ready-proof validation failure for %s' "$proof"
}

write_dependency_payload() {
  local pr="$1" issue="$2" branch="$3" head="$4" reason="$5" dependency_ref="${6:-}"
  local dir="/tmp/pm-dependency-watch-acks" payload txt ledger_json comment_url
  local ledger_args=()
  [[ "$dependency_ref" =~ ^[0-9]+$ ]] || die 2 "dependency reference must be numeric"
  [ -n "$head" ] || die 1 "cannot publish dependency proof without current PR head"
  [ -x "$DEPENDENCY_WATCH_LEDGER" ] || die 1 "dependency watch ledger unavailable: $DEPENDENCY_WATCH_LEDGER"
  mkdir -p "$dir" 2>/dev/null || true
  payload="$dir/pr-${pr}-${head:-unknown}-visible-marker.json"
  txt="/tmp/pm-dependency-blocked-${pr}.txt"
  ledger_args=(publish --repo "$REPO" --pr "$pr" --head "$head" --dependency "$dependency_ref" --reason "$reason")
  [ -n "$issue" ] && ledger_args+=(--issue "$issue")
  ledger_json="$(python3 "$DEPENDENCY_WATCH_LEDGER" "${ledger_args[@]}")" \
    || die 1 "failed to publish durable dependency proof for PR #$pr"
  comment_url="$(printf '%s' "$ledger_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("url", ""))' 2>/dev/null || true)"
  [ -n "$comment_url" ] || die 1 "dependency watch ledger returned no PR comment URL for PR #$pr"
  python3 - "$payload" "$pr" "${issue:-}" "${branch:-}" "${head:-}" "$reason" "$txt" "${dependency_ref:-}" "$comment_url" <<'PY'
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

payload, pr, issue, branch, head, reason, txt, dependency_ref, comment_url = sys.argv[1:]
refs = []
txt_path = Path(txt)
if txt_path.exists():
    try:
        text = txt_path.read_text(encoding="utf-8", errors="replace")
        refs = sorted(set(re.findall(r"#(\d{3,6})", text)))
    except Exception:
        refs = []
if dependency_ref:
    refs.append(str(dependency_ref).lstrip("#"))
refs = sorted(set(r for r in refs if r and r != str(pr)))
if refs:
    txt_path.write_text(
        "\n".join([
            "PM_DEPENDENCY_WATCH: active",
            f"pr: {pr}",
            f"issue: {issue or 'unknown'}",
            f"branch: {branch or 'unknown'}",
            f"headRefOid: {head}",
            "blocked_on: " + ",".join(f"#{r}" for r in refs),
            f"reason: {reason}",
            "next_action: after the named dependency merges/closes, retarget/rebase this PR onto main, verify the new exact head, and directly fire one label-gated CI+E2E wave; one functionality-first review may run in parallel and gates merge, not CI start. Planner/capture/marker/preflight ceremony must not block CI start.",
            "",
        ]),
        encoding="utf-8",
    )
Path(payload).write_text(json.dumps({
    "schema_version": 1,
    "status": "watching",
    "source": "pm-transition block-pr --reason dependency",
    "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "pr": int(pr),
    "issue": int(issue) if str(issue).isdigit() else None,
    "branch": branch,
    "head": head,
    "head_short": head[:10],
    "blocker_label": "pm-blocked:dependency",
    "dependency_refs": refs,
    "proof_txt": str(txt_path) if txt_path.exists() else "",
    "comment_url": comment_url,
    "next_action": "after the named dependency merges/closes, retarget/rebase this PR onto main, verify the new exact head, and directly fire one label-gated CI+E2E wave; one functionality-first review may run in parallel and gates merge, not CI start. Planner/capture/marker/preflight ceremony must not block CI start.",
    "reason": reason,
}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(payload)
PY
}

first_free_slot_for_rework() {
  local allowed_slots="$1" mop_json candidates slot
  if [ -n "${PM_COMMAND_SNAPSHOT:-}" ] && [ -f "$PM_COMMAND_SNAPSHOT" ]; then
    mop_json="$(command_snapshot_mop_slots_json)"
  else
    mop_json="$(curl -sS -m 4 "$MOP_BASE/slots" 2>/dev/null || true)"
  fi
  candidates="$(MOP_JSON="$mop_json" python3 - "$allowed_slots" <<'PY'
import json
import os
import sys

allowed = {int(x) for x in sys.argv[1].split() if x.isdigit()}
try:
    data = json.loads(os.environ.get("MOP_JSON") or "{}")
except Exception:
    sys.exit(0)
for entry in data.get("slots", []):
    try:
        slot = int(entry.get("slot"))
    except Exception:
        continue
    if allowed and slot not in allowed:
        continue
    if entry.get("dnd"):
        continue
    if not entry.get("occupied"):
        print(slot)
PY
)"
  while IFS= read -r slot; do
    [[ "$slot" =~ ^[1-4]$ ]] || continue
    if ! slot_clone_locked "$slot"; then
      printf '%s\n' "$slot"
      return 0
    fi
  done <<<"$candidates"
}

first_restorable_free_slot_for_rework() {
  # S1-restore selection for capture-local-required ONLY: a slot with NO owner
  # (pr/issue null), an inactive turn, not DND, and idle is genuinely free even
  # when a prior assignment_epoch lingers with occupied still true.  Ordinary
  # rework sweeps keep the strict first_free_slot_for_rework gate.
  local allowed_slots="$1" mop_json candidates slot
  mop_json="$(curl -sS -m 4 "$MOP_BASE/slots" 2>/dev/null || true)"
  candidates="$(MOP_JSON="$mop_json" python3 - "$allowed_slots" <<'PY'
import json
import os
import sys

allowed = {int(x) for x in sys.argv[1].split() if x.isdigit()}
try:
    data = json.loads(os.environ.get("MOP_JSON") or "{}")
except Exception:
    sys.exit(0)
for entry in data.get("slots", []):
    try:
        slot = int(entry.get("slot"))
    except Exception:
        continue
    if allowed and slot not in allowed:
        continue
    if entry.get("dnd"):
        continue
    if entry.get("occupied") is not True:
        continue
    if entry.get("pr") is not None or entry.get("issue") is not None:
        continue
    if entry.get("active_turn_state") != "inactive":
        continue
    if entry.get("idle") is not True:
        continue
    print(slot)
PY
)"
  while IFS= read -r slot; do
    [[ "$slot" =~ ^[1-4]$ ]] || continue
    if ! slot_clone_locked "$slot"; then
      printf '%s\n' "$slot"
      return 0
    fi
  done <<<"$candidates"
}

auto_assign_rework_from_pr_sweep() {
  local pr_sweep_log="$1" dry_run="$2" slots="$3"
  local candidates line pr issue slot packet out rc
  local assigned=0 deferred=0 errors=0
  candidates="$(grep -E ' PR_REWORK_DISPATCH_REQUIRED PR#[0-9]+' "$pr_sweep_log" 2>/dev/null || true)"
  if [ -z "$candidates" ]; then
    echo "PR_REWORK_ASSIGNMENT_SUMMARY assigned=0 deferred=0 errors=0"
    return 0
  fi
  # Dispatch contract: oldest rework record first (created_at ascending, UTC
  # Z-suffixed so lexicographic sort is chronological), tie-break PR number
  # ascending. The sweep emits rows newest-PR-first; preserving scan order
  # dispatched newer PR #6603 ahead of older queued #6597 (2026-07-19).
  # Rows without created_at sort last. Ordering only — eligibility unchanged.
  candidates="$(printf '%s\n' "$candidates" | awk '
    {
      ca = "9999-12-31T23:59:59Z"; pr = 999999999
      if (match($0, /created_at=[^[:space:]]+/)) ca = substr($0, RSTART + 11, RLENGTH - 11)
      if (match($0, /PR#[0-9]+/)) pr = substr($0, RSTART + 3, RLENGTH - 3) + 0
      printf "%s\t%09d\t%s\n", ca, pr, $0
    }' | LC_ALL=C sort | cut -f3-)"
  if ! mop_slots_healthy; then
    local candidate_count
    candidate_count="$(printf '%s\n' "$candidates" | awk 'NF { c++ } END { print c+0 }')"
    echo "PR_REWORK_ASSIGNMENT_DEFERRED reason=mop_slots_unhealthy count=$candidate_count"
    echo "PR_REWORK_ASSIGNMENT_SUMMARY assigned=0 deferred=$candidate_count errors=0"
    return 0
  fi

  while IFS= read -r line; do
    [ -n "$line" ] || continue
    pr="$(printf '%s
' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
    issue="$(printf '%s
' "$line" | sed -n 's/.*issue=#\([0-9][0-9]*\).*/\1/p')"
    packet="$(printf '%s
' "$line" | sed -n 's/.*packet=\([^ ]*\).*/\1/p')"
    [ -n "$pr" ] || continue

    if [ "$dry_run" = "1" ]; then
      slot="$(printf '%s
' "$line" | sed -n 's/.*slot:\([0-9][0-9]*\).*/\1/p')"
      echo "PR_REWORK_ASSIGNMENT_SKIPPED PR#$pr slot:${slot:-unknown} issue=#${issue:-unknown} reason=dry-run"
      deferred=$((deferred+1))
      continue
    fi

    local pr_json branch head labels owner_slots hint_slot owner_conflicts expected_epoch repository_id
    if [ -z "$issue" ]; then
      issue="$(issue_from_pr "$pr")"
    fi
    pr_json="$(gh pr view "$pr" --repo "$REPO" --json headRefName,headRefOid,labels 2>/dev/null || true)"
    branch="$(printf '%s' "$pr_json" | json_field headRefName 2>/dev/null || true)"
    head="$(printf '%s' "$pr_json" | json_field headRefOid 2>/dev/null || true)"
    labels="$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print(",".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
    if labels_include "$labels" "pm-blocked:capture" || printf '%s\n' "$line" | grep -q 'pm-blocked:capture'; then
      hint_slot="$(printf '%s
' "$line" | sed -n 's/.*slot:\([0-9][0-9]*\).*/\1/p')"
      echo "PR_REWORK_ASSIGNMENT_SKIPPED PR#$pr slot:${hint_slot:-unknown} issue=#${issue:-unknown} reason=capture_blocked"
      deferred=$((deferred+1))
      continue
    fi
    owner_slots="$(matching_mop_slots_for_target "$pr" "$issue" "$branch" | awk 'NF && !seen[$0]++' | tr '\n' ' ' | sed 's/[[:space:]]*$//')"
    if [ -n "$owner_slots" ]; then
      hint_slot="$(printf '%s
' "$line" | sed -n 's/.*slot:\([0-9][0-9]*\).*/\1/p')"
      owner_conflicts="$(owner_slots_except_requested "$owner_slots" "$hint_slot")"
      if [ -n "$hint_slot" ] && [ -z "$owner_conflicts" ]; then
        echo "PR_REWORK_ALREADY_ASSIGNED PR#$pr issue=#${issue:-unknown} branch=${branch:-unknown} owner_slots=$owner_slots requested_slot=$hint_slot action=keep_active"
      else
        echo "PR_REWORK_ALREADY_ASSIGNED PR#$pr issue=#${issue:-unknown} branch=${branch:-unknown} owner_slots=$owner_slots action=do_not_dispatch"
      fi
      deferred=$((deferred+1))
      continue
    fi

    if [ -z "$packet" ] || [ ! -f "$packet" ]; then
      echo "PR_REWORK_ASSIGNMENT_DEFERRED PR#$pr issue=#${issue:-unknown} reason=durable_current_head_packet_missing packet=${packet:-none}"
      deferred=$((deferred+1))
      continue
    fi

    slot="$(first_free_slot_for_rework "$slots")"
    if [ -z "$slot" ]; then
      echo "PR_REWORK_ASSIGNMENT_DEFERRED PR#$pr issue=#${issue:-unknown} reason=no_free_slot"
      deferred=$((deferred+1))
      continue
    fi
    if slot_clone_locked "$slot"; then
      echo "PR_REWORK_ASSIGNMENT_DEFERRED PR#$pr slot:$slot issue=#${issue:-unknown} reason=slot_clone_locked lock=$(slot_clone_lock_path "$slot")"
      deferred=$((deferred+1))
      continue
    fi

    expected_epoch="$(mop_slot_epoch "$slot" 2>/dev/null || true)"
    repository_id="${MOP_REPOSITORY_ID:-$MOP_PRIMARY_REPOSITORY_ID}"
    if [ -z "$branch" ] || ! [[ "$head" =~ ^[0-9a-fA-F]{40}$ ]] || ! [[ "$expected_epoch" =~ ^[0-9]+$ ]] || [ -z "$repository_id" ]; then
      echo "PR_REWORK_ASSIGNMENT_DEFERRED PR#$pr slot:$slot issue=#${issue:-unknown} reason=claim_slot_resolution_required branch=${branch:-missing} head=${head:-missing} epoch=${expected_epoch:-missing} repository_id=${repository_id:-missing} handoff=$packet"
      deferred=$((deferred+1))
      continue
    fi
    local -a claim_args=(--pr "$pr" --slot "$slot" --branch "$branch" --head-sha "$head" --expected-epoch "$expected_epoch" --repository-id "$repository_id" --handoff-id "$packet")
    [ -n "$issue" ] && claim_args+=(--issue "$issue")
    # Internal reconciliation remains a direct Python claim_slot invocation;
    # the public pm-transition assignment command is retired below.
    out="$(claim_slot_compat rework assign-rework "${claim_args[@]}" --task "PR #$pr exact-head rework packet" 2>&1)"
    rc=$?
    printf '%s\n' "$out"
    if [ "$rc" = "0" ]; then
      assigned=$((assigned+1))
    else
      errors=$((errors+1))
      echo "PR_REWORK_ASSIGNMENT_FAILED PR#$pr slot:$slot issue=#${issue:-unknown} exit=$rc"
    fi
  done <<< "$candidates"

  echo "PR_REWORK_ASSIGNMENT_SUMMARY assigned=$assigned deferred=$deferred errors=$errors"
}

pending_ready_event_for_slot_pr() {
  local slot="$1" pr="${2:-}" head="${3:-}"
  python3 - "$slot" "$pr" "$head" <<'PY'
import glob
import json
import os
import sys

slot, pr, head = sys.argv[1:4]
matches = []
event_dir = os.environ.get("SLOT_READY_EVENT_DIR", "/tmp/slot-ready-events")
for path in glob.glob(os.path.join(event_dir, "*.json")):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        continue
    if str(data.get("status") or "") != "pending":
        continue
    if str(data.get("slot") or "") != slot:
        continue
    if pr and str(data.get("pr") or "") != pr:
        continue
    if head and str(data.get("head_sha") or "") != head:
        continue
    matches.append((os.path.getmtime(path), path))
if matches:
    print(sorted(matches)[-1][1])
PY
}

archive_slot_ready_events_for_pr() {
  local pr="$1" reason="$2" keep_event="${3:-}"
  [ -n "$pr" ] || return 0
  python3 - "$pr" "$reason" "$keep_event" <<'PY' 2>/dev/null || true
import glob
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

pr, reason, keep_event = sys.argv[1:4]
now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
for raw in glob.glob("/tmp/slot-ready-events/*.json"):
    path = Path(raw)
    if keep_event and path == Path(keep_event):
        continue
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        continue
    if str(data.get("pr") or "") != str(pr):
        continue
    if str(data.get("status") or "") != "pending":
        continue
    data["status"] = "obsolete"
    data["obsolete_at"] = now
    data["obsolete_reason"] = reason
    rendered = json.dumps(data, indent=2, sort_keys=True) + "\n"
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(rendered, encoding="utf-8")
    tmp.replace(path)
    path.with_suffix(path.suffix + ".obsolete").write_text(rendered, encoding="utf-8")
PY
}

record_review_loop_blocker() {
  # Review-loop accounting is marker-derived. PM state transitions such as
  # block-pr/rescope-decide are control-plane bookkeeping and must not inflate
  # the same-head review circuit breaker.
  return 0
}

assert_no_priority_pr_work_before_fresh_assign() {
  local issue="$1" slot="$2" output rc guard_output guard_rc index_state
  # The durable priority-rework index is the guard's authority. A fresh index
  # is consumed directly. A stale or missing index is repaired ONCE through
  # the canonical read-only sweep (bounded and fail-closed): a successful
  # sweep with a terminal row refreshes the index and the guard re-evaluates;
  # a failed or incomplete sweep keeps the stale index and fails closed as
  # RECONCILE_REQUIRED with a deduplicated async sweep request for the next
  # reconcile-capacity. This preserves the fail-closed contract while letting
  # one verified healthy free slot accept the first queued packet without an
  # external reconcile-capacity round trip. Root cause (Rajiv batch
  # 2026-08-15, thread 1786757064.752059): the assignment-family index
  # invalidated its own candidate during claim - a packet-publish/blocking-
  # state mutation in the same dispatch cycle invalidates the index and then
  # starves the next fresh candidate because the hot path only enqueued an
  # async request instead of re-reading the authoritative sweep.
  index_state="$(priority_rework_index_state)"
  case "$index_state" in
    fresh) ;;
    *)
      priority_rework_index_refresh_from_sweep
      index_state="$(priority_rework_index_state)"
      case "$index_state" in
        fresh) ;;
        *)
          priority_rework_sweep_request_enqueue "assign-guard" "$issue" "$slot"
          die 24 "fresh assignment guard degraded for issue #$issue index=$index_state reason=RECONCILE_REQUIRED; run pm-transition reconcile-capacity to refresh the priority rework index"
          ;;
      esac
      ;;
  esac
  output="$(priority_rework_index_sweep_text)"
  if [ "${PM_MUTATION_CLASS:-}" = "typed_capacity_fill" ] && [ -n "${PM_CAPACITY_REWORK_RESERVED_SLOTS:-}" ]; then
    guard_output="$(printf '%s\n' "$output" | python3 "$CAPACITY_CONTROL" fresh-fill-guard --candidate-slot "$slot" --reserved-slots "$PM_CAPACITY_REWORK_RESERVED_SLOTS" 2>&1)"
  else
    guard_output="$(printf '%s\n' "$output" | python3 "$CAPACITY_CONTROL" fresh-fill-guard 2>&1)"
  fi
  guard_rc=$?
  case "$guard_rc" in
    0) ;;
    24) die 24 "existing unowned PR work precedes fresh issue #$issue assignment guard=${guard_output}; run pm-transition reconcile-capacity first" ;;
    *) die 24 "fresh assignment ownership guard degraded for issue #$issue rc=$guard_rc detail=${guard_output}" ;;
  esac
}

issue_planned_branch() {
  local issue="$1" issue_json="${2:-}"
  if [ -z "$issue_json" ]; then
    issue_json="$(gh issue view "$issue" --repo "$REPO" --json title,body,labels 2>/dev/null)" \
      || return 1
  fi

  ISSUE_PLANNED_BRANCH_JSON="$issue_json" python3 - "$issue" <<'PY'
import json
import os
import re
import sys
import unicodedata

issue = int(sys.argv[1])
try:
    payload = json.loads(os.environ["ISSUE_PLANNED_BRANCH_JSON"])
except (KeyError, TypeError, ValueError) as exc:
    print(f"issue_branch_contract_invalid_json issue={issue} detail={exc}", file=sys.stderr)
    raise SystemExit(1)

body = str(payload.get("body") or "")
title = str(payload.get("title") or "").strip()
labels = {
    str(row.get("name") or "")
    for row in (payload.get("labels") or [])
    if isinstance(row, dict)
}
blocks = re.findall(r"(?is)<!--\s*ready-pool:(.*?)-->", body)

configured = ""
if blocks:
    # Ready Pool consumers treat the last block as the current contract.
    block = blocks[-1].strip()
    if block.startswith("{"):
        try:
            row = json.loads(block)
        except ValueError as exc:
            print(f"issue_branch_contract_invalid_ready_pool_json issue={issue} detail={exc}", file=sys.stderr)
            raise SystemExit(1)
        configured = str(row.get("branch_slug") or "").strip()
    else:
        matches = re.findall(r"(?mi)^\s*branch_slug\s*:\s*([^\s#]+)\s*(?:#.*)?$", block)
        if len(matches) > 1:
            print(f"issue_branch_contract_duplicate_slug issue={issue}", file=sys.stderr)
            raise SystemExit(1)
        configured = matches[0].strip() if matches else ""

if configured:
    slug = configured
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        print(f"issue_branch_contract_invalid_slug issue={issue} slug={slug}", file=sys.stderr)
        raise SystemExit(1)
    if len(slug) > 40:
        print(
            f"issue_branch_contract_slug_too_long issue={issue} length={len(slug)} max=40",
            file=sys.stderr,
        )
        raise SystemExit(1)
else:
    # Legacy Ready Pool issues may already own a slot and branch created by the
    # pre-contract title fallback. Reconstruct that exact branch only for
    # re-entry; fresh assignments still fail closed until promotion writes the
    # concise branch_slug contract.
    slot_labels = sorted(label for label in labels if re.fullmatch(r"slot:[1-4]", label))
    if len(slot_labels) != 1:
        print(f"issue_branch_contract_missing_slug issue={issue}", file=sys.stderr)
        raise SystemExit(1)
    normalized = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    normalized = re.sub(
        r"^(?:fix|feat|test|chore|docs|refactor|perf|ci)(?:\([^)]*\))?\s*:\s*",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(rf"^#?{issue}\b[\s:—–-]*", "", normalized, flags=re.IGNORECASE)
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    slug = slug[:72].rstrip("-")
    if not slug:
        print(f"issue_branch_contract_missing_legacy_title issue={issue}", file=sys.stderr)
        raise SystemExit(1)

branch = f"fix/{issue}-{slug}"
if len(branch) > 120:
    print(f"issue_branch_contract_branch_too_long issue={issue} length={len(branch)}", file=sys.stderr)
    raise SystemExit(1)
print(branch)
PY
}

verify_assign_postcondition() {
  local issue="$1" slot="$2" expected_branch="$3" mop_json issue_json
  mop_json="$(curl -sS -m 4 "$MOP_BASE/slots/${slot}" 2>/dev/null || true)"
  [ -n "$mop_json" ] || {
    printf 'assign_postcondition_unreadable surface=mop slot=%s issue=%s\n' "$slot" "$issue" >&2
    return 1
  }
  issue_json="$(gh issue view "$issue" --repo "$REPO" --json state,labels 2>/dev/null || true)"
  [ -n "$issue_json" ] || {
    printf 'assign_postcondition_unreadable surface=github slot=%s issue=%s\n' "$slot" "$issue" >&2
    return 1
  }

  MOP_SLOT_JSON="$mop_json" ISSUE_STATE_JSON="$issue_json" python3 - "$issue" "$slot" "$expected_branch" <<'PY'
import json
import os
import re
import sys

issue, slot = map(int, sys.argv[1:3])
expected_branch = sys.argv[3]
try:
    mop = json.loads(os.environ["MOP_SLOT_JSON"])
    issue_state = json.loads(os.environ["ISSUE_STATE_JSON"])
except (KeyError, TypeError, ValueError) as exc:
    print(f"assign_postcondition_invalid_json detail={exc}", file=sys.stderr)
    raise SystemExit(1)

labels = {
    str(row.get("name") or "")
    for row in issue_state.get("labels", [])
    if isinstance(row, dict)
}
slot_labels = sorted(label for label in labels if re.fullmatch(r"slot:[1-4]", label))
branch = str(mop.get("branch") or "")
task = str(mop.get("task") or "")
epoch = mop.get("assignment_epoch")
turn_id = str(mop.get("active_turn_id") or "")
turn_state = str(mop.get("active_turn_state") or "")

checks = {
    "mop_occupied": mop.get("occupied") is True,
    "mop_not_dnd": mop.get("dnd") is not True,
    "mop_slot": mop.get("slot") == slot,
    "mop_issue": mop.get("issue") == issue,
    # The claim helper may route an existing PR whose established branch is
    # authoritative. Fresh issue claims are separately bound to
    # PM_EXPECTED_BRANCH; the postcondition must at minimum reject every legacy
    # placeholder tuple.
    "mop_branch": bool(branch) and branch != f"fix/{issue}-pending",
    "mop_task": task.startswith(f"CLAIM #{issue}:"),
    "mop_epoch": isinstance(epoch, int) and epoch >= 0,
    "github_open": issue_state.get("state") == "OPEN",
    "github_in_progress": "status:in-progress" in labels,
    "github_not_todo": "status:todo" not in labels,
    "github_exact_slot": slot_labels == [f"slot:{slot}"],
}
failed = sorted(name for name, passed in checks.items() if not passed)
if failed:
    print(
        "assign_postcondition_failed "
        f"issue={issue} slot={slot} failed={','.join(failed)} "
        f"mop_issue={mop.get('issue')} occupied={mop.get('occupied')} "
        f"branch={branch or 'none'} expected_branch={expected_branch} "
        f"epoch={epoch} turn_state={turn_state or 'none'} "
        f"turn_id={turn_id or 'none'} labels={','.join(sorted(labels)) or 'none'}",
        file=sys.stderr,
    )
    raise SystemExit(1)

print(
    f"assignment_epoch={epoch} active_turn_id={turn_id} "
    f"branch={branch}"
)
PY
}

cmd_reserve_handoff() {
  legacy_assignment_writer_disabled "reserve-handoff"
  return 423
}
reserve_handoff_locked() {
  legacy_assignment_writer_disabled "reserve-handoff"
  return 423
}
cmd_slot_ready() {
  local event=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --event) event="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown slot-ready arg $1" ;;
    esac
  done
  [ -n "$event" ] || die 2 "--event is required"
  family2_slot_ready "$event"
}
cmd_accept_ready() {
  legacy_family2_retired "accept-ready"
  return 423
  local pr="" issue="" slot="" review_proof="" rescue_proof="" affected_test_proof="" affected_test_plan="" notes="" marker_arg=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      --issue) issue="${2:-}"; shift 2 ;;
      --slot) slot="${2:-}"; shift 2 ;;
      --review-proof) review_proof="${2:-}"; shift 2 ;;
      --rescue-proof) rescue_proof="${2:-}"; shift 2 ;;
      --affected-test-proof) affected_test_proof="${2:-}"; shift 2 ;;
      --affected-test-plan) affected_test_plan="${2:-}"; shift 2 ;;
      --notes) notes="${2:-}"; shift 2 ;;
      --marker|--pm-marker) marker_arg="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown accept-ready arg $1" ;;
    esac
  done

  need_num pr "$pr"
  [ -n "$issue" ] || issue="$(issue_from_pr "$pr")"
  [ -n "$issue" ] || die 1 "cannot infer issue for PR #$pr; pass --issue"
  [ -n "$issue" ] && need_num issue "$issue"
  [ -n "$slot" ] || slot="$(slot_from_labels "$pr" "$issue")"

  local pr_json state live_draft head branch title url marker event live_labels live_pm_state owner_slots
  pr_json="$(gh pr view "$pr" --repo "$REPO" --json state,isDraft,headRefOid,headRefName,title,url,labels 2>/dev/null || true)"
  [ -n "$pr_json" ] || die 1 "cannot read PR #$pr"
  state="$(printf '%s' "$pr_json" | json_field state)"
  live_draft="$(printf '%s' "$pr_json" | json_field isDraft)"
  head="$(printf '%s' "$pr_json" | json_field headRefOid)"
  branch="$(printf '%s' "$pr_json" | json_field headRefName)"
  title="$(printf '%s' "$pr_json" | json_field title)"
  url="$(printf '%s' "$pr_json" | json_field url)"
  [ "$state" = "OPEN" ] || die 1 "PR #$pr is not open (state=$state)"
  [ -n "$head" ] || die 1 "PR #$pr missing headRefOid"
  [ -n "$branch" ] || die 1 "PR #$pr missing headRefName"

  live_labels="$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print("\n".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
  live_pm_state="$(printf '%s\n' "$live_labels" | grep '^pm-state:' | head -1 || true)"
  owner_slots="$(matching_mop_slots_for_target "$pr" "$issue" "$branch" | awk 'NF && !seen[$0]++' | tr '\n' ' ' | sed 's/[[:space:]]*$//')"
  if [ "$live_pm_state" = "pm-state:qa-passed-awaiting-ci" ] && [ -z "$owner_slots" ]; then
    die 44 "accept-ready is Phase A and requires a live exact-tuple slot owner. PR #$pr is already slot-free in qa-passed-awaiting-ci at head ${head:0:10}; do not recreate slot ownership. Run project-local pm-readiness-contract. If it returns READY_PACKET: PASS, run pm-transition validate-ready-proof --pr $pr && pm-transition merge-ready --pr $pr."
  fi

  need_num slot "$slot"
  [[ "$slot" =~ ^[1-4]$ ]] || die 2 "slot must be 1..4"

	  marker="$(pm_review_marker_path "$pr" "$head")"
  [ -z "$rescue_proof" ] || [ -z "$marker_arg" ] \
    || die 1 "PM accept-ready accepts exactly one review authority: phase-a marker or Fable rescue packet"
  if [ -n "$rescue_proof" ]; then
    rescue_packet_authorizes_final_head "$rescue_proof" "$pr" "$head" \
      || die 1 "PM accept-ready Fable rescue packet does not authorize exact final head: pr=$pr head=$head packet=$rescue_proof"
    marker="$rescue_proof"
  else
	  if [ -n "$marker_arg" ]; then
	    [ -f "$marker_arg" ] || die 1 "PM accept-ready marker not found: $marker_arg"
    grep -q "headRefOid: ${head}" "$marker_arg" 2>/dev/null || die 1 "PM accept-ready marker is not for current head: marker=$marker_arg head=$head"
    grep -qE '^(PM_CLAUDE_REVIEW|PM_OPUS_REVIEW):[[:space:]]*PASS' "$marker_arg" 2>/dev/null || die 1 "PM accept-ready marker is not PASS: $marker_arg"
    grep -qE '^runtime_control_point:[[:space:]]*[^[:space:]].+' "$marker_arg" 2>/dev/null || die 1 "PM accept-ready marker missing runtime_control_point: $marker_arg"
    grep -qE '^pass_scope:[[:space:]]*phase-a($|[[:space:]])' "$marker_arg" 2>/dev/null || die 1 "PM accept-ready marker missing pass_scope phase-a: $marker_arg"
    grep -qE '^readiness_ceiling:[[:space:]]*[^[:space:]].+' "$marker_arg" 2>/dev/null || die 1 "PM accept-ready marker missing readiness_ceiling: $marker_arg"
	    if [ "$marker_arg" != "$marker" ]; then
	      cp "$marker_arg" "$marker" || die 1 "failed to copy PM accept-ready marker to canonical path $marker"
	    fi
	  fi
    pm_review_marker_ok_for_scope "$pr" "$head" phase-a || die 1 "PM accept-ready requires a current-head PM Claude phase-a PASS marker or an exact applied Fable rescue packet for PR #$pr head=${head:0:8}: $marker"
  fi

  # Release-policy simplification: affected-test planner receipts must not
  # block CI start. They remain optional diagnostics on the promotion proof.
  affected_test_proof="$(affected_test_proof_for_head "$pr" "$head" "$affected_test_proof" || true)"

  if [ -z "$review_proof" ]; then
    if [ -n "$rescue_proof" ]; then
      review_proof="$rescue_proof"
    elif [ -f "/tmp/codex-app-code-review-${pr}.txt" ]; then
      review_proof="/tmp/codex-app-code-review-${pr}.txt"
    else
      review_proof="$marker"
    fi
  fi

  local _ar_epoch
  _ar_epoch="$(accept_ready_existing_claim_epoch "$slot" "$issue" "$pr" "$branch" "$head")" || {
    local _ar_rc=$?
    return "$_ar_rc"
  }
  [[ "$_ar_epoch" =~ ^[0-9]+$ ]] \
    || die 46 "accept-ready slot $slot tuple adoption returned non-numeric assignment_epoch=${_ar_epoch:-empty}"

  mkdir -p /tmp/slot-ready-events || die 1 "failed to create /tmp/slot-ready-events"
  event="/tmp/slot-ready-events/pm-accept-ready-slot-${slot}-pr-${pr}-${head:0:12}.json"
  python3 - "$event" "$pr" "$issue" "$slot" "$head" "$branch" "$url" "$title" "$live_draft" "$marker" "$review_proof" "$rescue_proof" "$affected_test_proof" "$affected_test_plan" "$notes" <<'PYSUB'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

(
    event,
    pr,
    issue,
    slot,
    head,
    branch,
    url,
    title,
    is_draft,
    marker,
    review_proof,
    rescue_proof,
    affected_test_proof,
    affected_test_plan,
    notes,
) = sys.argv[1:]

data = {
    "event_type": "slot_ready",
    "status": "pending",
    "source": "pm-transition accept-ready",
    "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "slot": int(slot),
    "issue": int(issue) if str(issue).isdigit() else None,
    "pr": int(pr),
    "branch": branch,
    "head_sha": head,
    "pr_url": url,
    "pr_title": title,
    "pr_is_draft": is_draft == "true",
    "review_proof": marker,
    "review_verdict": "PM_ACCEPT_READY",
    "pm_accept_ready": True,
    "pm_accept_ready_marker": marker,
    "pm_accept_ready_rescue_proof": rescue_proof or None,
    "accepted_slot_review_proof": review_proof or None,
    "qa_proof": None,
    "affected_test_proof": affected_test_proof or None,
    "affected_test_plan": affected_test_plan or None,
    "notes": notes or None,
}
path = Path(event)
tmp = path.with_name(path.name + ".tmp")
tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(path)
PYSUB

  # accept-ready's PM-constructed slot_ready event omits assignment_epoch,
  # which cmd_slot_ready requires (need_num at ~2980). Inject the authoritative
  # MoP epoch so PM-override advances (split marker / released-slot) are not
  # rejected with "assignment_epoch must be numeric".
  if [[ "$_ar_epoch" =~ ^[0-9]+$ ]]; then
    python3 - "$event" "$_ar_epoch" <<'PYAREPOCH'
import json, sys
ev, epoch = sys.argv[1], sys.argv[2]
d = json.load(open(ev))
d["assignment_epoch"] = int(epoch)
with open(ev, "w") as fh:
    json.dump(d, fh, indent=2, sort_keys=True)
    fh.write("\n")
PYAREPOCH
  fi

  record_event --source pm-transition --event accept_ready --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --slot "$slot" --head-sha "$head" --payload "marker=$marker" --payload "accepted_slot_review_proof=${review_proof:-none}" --payload "event=$event" --dedupe
  legacy_family2_retired "accept-ready"
  return 423
}

blocked_label_for_reason() {
  case "$1" in
    ci) printf 'pm-blocked:ci' ;;
    capture) printf 'pm-blocked:capture' ;;
    codex) printf 'pm-blocked:codex' ;;
    rebase) printf 'pm-blocked:rebase' ;;
    dependency) printf 'pm-blocked:dependency' ;;
    product|rajiv|blocked-on-rajiv) printf 'pm-blocked:product' ;;
    infra) printf 'pm-blocked:infra' ;;
    pm-gate|pm-review-wait|qa|other|*) printf 'pm-blocked:pm-gate' ;;
  esac
}

block_reason_releases_slot() {
  case "$1" in
    product|rajiv|blocked-on-rajiv|dependency|capture|pm-review-wait) return 0 ;;
    *) return 1 ;;
  esac
}

non_slot_rework_checkpoint_label() {
  local labels="$1" label
  for label in \
    cto-rescue:in-progress \
    pm-blocked:cto \
    pm-blocked:product \
    pm-blocked:dependency \
    pm-blocked:capture \
    pm-blocked:infra \
    pm-blocked:pm-gate; do
    if printf '%s\n' "$labels" | grep -qx "$label"; then
      printf '%s\n' "$label"
      return 0
    fi
  done
  return 1
}

reconcile_rework_packet_obligation() {
  local pr="$1" issue="$2" head="$3" packet_id="$4" comment_url="$5" source_packet="$6" labels="$7"
  local checkpoint
  checkpoint="$(non_slot_rework_checkpoint_label "$labels" || true)"
  if [ -n "$checkpoint" ]; then
    resolve_pr_obligation_kinds "$pr" "$issue" "rework_packet_is_checkpoint" \
      "head=$head checkpoint=$checkpoint packet_id=$packet_id" rework
    upsert_obligation --kind rework_checkpoint --severity high \
      --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" \
      --owner pm --horizon hourly \
      --title "PR #$pr has a durable non-slot rework checkpoint" \
      --action "Do not assign PR #$pr while $checkpoint remains. Resolve that blocker through its canonical PM transition, then run pm-transition reconcile-rework-obligation --pr $pr before capacity reconciliation." \
      --blocker "rework_checkpoint:$checkpoint" \
      --evidence "packet_id=$packet_id" --evidence "head=$head" \
      --evidence "comment_url=${comment_url:-unknown}" \
      --evidence "source_packet=$source_packet" \
      --evidence "checkpoint=$checkpoint"
    printf 'checkpoint:%s\n' "$checkpoint"
    return 0
  fi

  resolve_pr_obligation_kinds "$pr" "$issue" "rework_packet_is_assignable" \
    "head=$head packet_id=$packet_id" rework_checkpoint
  upsert_obligation --kind rework --severity high \
    --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" \
    --owner pm --horizon hourly \
    --title "PR #$pr has durable current-head rework packet" \
    --action "Resolve branch/head/epoch/handoff, then use claim_slot via the complete tuple; the legacy partial assign-rework form is disabled." \
    --blocker "rework_packet_ready" \
    --evidence "packet_id=$packet_id" --evidence "head=$head" \
    --evidence "comment_url=${comment_url:-unknown}" \
    --evidence "source_packet=$source_packet"
  capacity_reconcile_trigger rework_packet_ready
  printf 'assignable\n'
}

cmd_record_rework_packet() {
  local pr="" issue="" packet="" kind="rework"
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      --issue) issue="${2:-}"; shift 2 ;;
      --packet) packet="${2:-}"; shift 2 ;;
      --kind) kind="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown record-rework-packet arg $1" ;;
    esac
  done
  need_num pr "$pr"
  [ -f "$packet" ] || die 2 "packet file not found: $packet"
  if [ -z "$issue" ]; then issue="$(issue_from_pr "$pr")"; fi
  need_num issue "$issue"
  local pr_json state head labels result packet_id comment_url obligation
  pr_json="$(gh pr view "$pr" --repo "$REPO" --json state,headRefOid,labels 2>/dev/null || true)"
  [ -n "$pr_json" ] || die 1 "cannot read PR #$pr"
  state="$(printf '%s' "$pr_json" | json_field state)"
  [ "$state" = "OPEN" ] || die 1 "PR #$pr is not open"
  head="$(printf '%s' "$pr_json" | json_field headRefOid)"
  [ -n "$head" ] || die 1 "PR #$pr has no headRefOid"
  labels="$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print("\n".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
  result="$(publish_rework_packet "$pr" "$issue" "$head" "$kind" "$packet")" \
    || die 43 "failed to persist current-head rework packet for PR #$pr"
  packet_id="$(printf '%s' "$result" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("packet_id", ""))' 2>/dev/null || true)"
  comment_url="$(printf '%s' "$result" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("comment_url", ""))' 2>/dev/null || true)"
  [ -n "$packet_id" ] || die 43 "rework packet ledger returned no packet_id for PR #$pr"
  obligation="$(reconcile_rework_packet_obligation "$pr" "$issue" "$head" "$packet_id" "$comment_url" "$packet" "$labels")"
  record_event --source pm-transition --event rework_packet_recorded --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --head-sha "$head" --payload "kind=$kind" --payload "packet_id=$packet_id" --payload "comment_url=${comment_url:-unknown}" --payload "source_packet=$packet" --payload "obligation=$obligation"
  echo "PM_TRANSITION_OK command=record-rework-packet pr=$pr issue=$issue head=${head:0:10} kind=$kind packet_id=$packet_id comment_url=${comment_url:-unknown} obligation=$obligation"
}

cmd_deliver_rework_packet() {
  legacy_assignment_writer_disabled "deliver-rework-packet"
  return 423
}


cmd_reconcile_rework_obligation() {
  local pr="" issue=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      --issue) issue="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown reconcile-rework-obligation arg $1" ;;
    esac
  done
  need_num pr "$pr"
  if [ -z "$issue" ]; then issue="$(issue_from_pr "$pr")"; fi
  need_num issue "$issue"

  local pr_json state head labels packet_file result packet_id comment_url obligation
  pr_json="$(gh pr view "$pr" --repo "$REPO" --json state,headRefOid,labels 2>/dev/null || true)"
  [ -n "$pr_json" ] || die 1 "cannot read PR #$pr"
  state="$(printf '%s' "$pr_json" | json_field state)"
  [ "$state" = "OPEN" ] || die 1 "PR #$pr is not open"
  head="$(printf '%s' "$pr_json" | json_field headRefOid)"
  [ -n "$head" ] || die 1 "PR #$pr has no headRefOid"
  labels="$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print("\n".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
  packet_file="/tmp/pm-rework-obligation-${pr}-${head}.md"
  result="$(python3 "$REWORK_PACKET_LEDGER" fetch --repo "$REPO" --pr "$pr" --head "$head" --output "$packet_file" 2>/dev/null)" \
    || die 43 "PR #$pr has no durable current-head rework packet for head ${head:0:10}"
  [ -s "$packet_file" ] || die 43 "PR #$pr durable current-head rework packet is empty"
  packet_id="$(printf '%s' "$result" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("packet_id", ""))' 2>/dev/null || true)"
  comment_url="$(printf '%s' "$result" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("comment_url", ""))' 2>/dev/null || true)"
  [ -n "$packet_id" ] || die 43 "rework packet ledger returned no packet_id for PR #$pr"
  obligation="$(reconcile_rework_packet_obligation "$pr" "$issue" "$head" "$packet_id" "$comment_url" "$packet_file" "$labels")"
  record_event --source pm-transition --event rework_obligation_reconciled --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --head-sha "$head" --payload "packet_id=$packet_id" --payload "comment_url=${comment_url:-unknown}" --payload "obligation=$obligation"
  echo "PM_TRANSITION_OK command=reconcile-rework-obligation pr=$pr issue=$issue head=${head:0:10} packet_id=$packet_id obligation=$obligation"
}

cmd_revoke_rework() {
  local pr="" issue="" expected_head="" reason="" proof="" slot="" expected_epoch=""
  local preserve_independent_blockers=0
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      --issue) issue="${2:-}"; shift 2 ;;
      --expected-head) expected_head="${2:-}"; shift 2 ;;
      --reason) reason="${2:-}"; shift 2 ;;
      --proof) proof="${2:-}"; shift 2 ;;
      --slot) slot="${2:-}"; shift 2 ;;
      --expected-epoch) expected_epoch="${2:-}"; shift 2 ;;
      --preserve-independent-blockers) preserve_independent_blockers=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown revoke-rework arg $1" ;;
    esac
  done
  need_num pr "$pr"
  [[ "$expected_head" =~ ^[0-9a-f]{40}$ ]] \
    || die 2 "--expected-head must be a full lowercase commit SHA"
  [ -n "$reason" ] || die 2 "--reason is required"
  [ -f "$proof" ] || die 2 "revocation proof file not found: $proof"
  if [ -z "$issue" ]; then issue="$(issue_from_pr "$pr")"; fi
  need_num issue "$issue"
  if [ -n "$slot" ] || [ -n "$expected_epoch" ]; then
    need_num slot "$slot"
    need_num expected-epoch "$expected_epoch"
  fi

  local pr_json state head branch labels blockers disallowed_blockers
  local label_slots owner_slots mop_status current_epoch ledger_result packet_id
  local released_slots release_rc proof_digest post_labels
  local -a event_slot_args=()
  pr_json="$(gh pr view "$pr" --repo "$REPO" --json state,headRefOid,headRefName,labels 2>/dev/null || true)"
  [ -n "$pr_json" ] || die 1 "cannot read PR #$pr"
  state="$(printf '%s' "$pr_json" | json_field state)"
  head="$(printf '%s' "$pr_json" | json_field headRefOid)"
  branch="$(printf '%s' "$pr_json" | json_field headRefName)"
  labels="$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print("\n".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
  [ "$state" = "OPEN" ] || die 1 "PR #$pr is not open"
  [ "$head" = "$expected_head" ] \
    || die 13 "revoke-rework head drift pr=$pr expected=$expected_head live=${head:-missing}"
  [ -n "$branch" ] || die 1 "PR #$pr has no headRefName"

  blockers="$(printf '%s\n' "$labels" | grep '^pm-blocked:' || true)"
  disallowed_blockers="$(printf '%s\n' "$blockers" | grep -Ev '^(pm-blocked:codex|pm-blocked:pm-gate)$' || true)"
  if [ -n "$disallowed_blockers" ] && [ "$preserve_independent_blockers" != "1" ]; then
    die 42 "revoke-rework cannot supersede independent blockers pr=$pr blockers=$(printf '%s' "$disallowed_blockers" | paste -sd, -)"
  fi
  if [ -z "$disallowed_blockers" ] && [ "$preserve_independent_blockers" = "1" ]; then
    die 42 "revoke-rework --preserve-independent-blockers requires a live independent pm-blocked:* label pr=$pr"
  fi

  label_slots="$(
    {
      printf '%s\n' "$labels" | sed -n 's/^slot:\([1-4]\)$/\1/p'
      gh issue view "$issue" --repo "$REPO" --json labels --jq '.labels[].name | select(test("^slot:[1-4]$")) | sub("^slot:";"")' 2>/dev/null || true
    } | awk 'NF && !seen[$0]++'
  )"
  owner_slots="$(matching_mop_slots_for_target "$pr" "$issue" "$branch" | awk 'NF && !seen[$0]++')"
  if [ -n "$label_slots" ] || [ -n "$owner_slots" ]; then
    [ -n "$slot" ] \
      || die 13 "revoke-rework requires --slot and --expected-epoch for an owned or labelled target pr=$pr labels=$(printf '%s' "$label_slots" | paste -sd, -) owners=$(printf '%s' "$owner_slots" | paste -sd, -)"
    if printf '%s\n%s\n' "$label_slots" "$owner_slots" | awk 'NF' | grep -vx "$slot" >/dev/null; then
      die 13 "revoke-rework slot tuple mismatch pr=$pr requested=$slot labels=$(printf '%s' "$label_slots" | paste -sd, -) owners=$(printf '%s' "$owner_slots" | paste -sd, -)"
    fi
    current_epoch="$(mop_slot_epoch "$slot")" \
      || die 13 "revoke-rework cannot read slot epoch slot=$slot pr=$pr"
    [ "$current_epoch" = "$expected_epoch" ] \
      || die 13 "revoke-rework epoch drift slot=$slot pr=$pr expected=$expected_epoch live=$current_epoch"
    mop_status="$(mop_slot_target_status "$slot" "$pr" "$issue" "$branch")"
    case "$mop_status" in
      match|free) ;;
      *) die 13 "revoke-rework slot target mismatch slot=$slot pr=$pr issue=$issue branch=$branch mop_status=${mop_status:-unknown}" ;;
    esac
  elif [ -n "$slot" ]; then
    die 13 "revoke-rework refused unused slot tuple slot=$slot pr=$pr"
  fi

  [ -x "$REWORK_PACKET_LEDGER" ] \
    || die 43 "rework packet ledger is not executable: $REWORK_PACKET_LEDGER"
  ledger_result="$(python3 "$REWORK_PACKET_LEDGER" revoke \
    --repo "$REPO" --pr "$pr" --issue "$issue" --head "$head" \
    --reason "$reason" --proof "$proof")" \
    || die 43 "failed to revoke current-head rework packet for PR #$pr"
  packet_id="$(printf '%s' "$ledger_result" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("packet_id", ""))' 2>/dev/null || true)"
  [ -n "$packet_id" ] || die 43 "rework packet revocation returned no packet_id for PR #$pr"
  priority_rework_index_invalidate "packet_revoke pr=$pr issue=${issue:-unknown} head=${head:0:10}"
  proof_digest="$(shasum -a 256 "$proof" | awk '{print $1}')"

  if [ -n "$slot" ]; then
    PM_MUTATION_EXPECTED_EPOCH="$expected_epoch"
    event_slot_args+=(--slot "$slot")
  fi
  upsert_obligation --kind rework_revoke_release_pending --severity high \
    --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" \
    --owner pm --horizon hourly \
    --title "PR #$pr rework is revoked but slot release must finish" \
    --action "Retry pm-transition revoke-rework with the same exact head, reason, proof, slot, and current expected epoch. The revocation is durable and idempotent; do not re-dispatch the packet." \
    --blocker "rework_revoke_release_pending" \
    --evidence "head=$head" --evidence "packet_id=$packet_id" \
    --evidence "slot=${slot:-none}" --evidence "expected_epoch=${expected_epoch:-none}"
  released_slots="$(release_target_slots "$pr" "$issue" "$branch" "revoke-rework" "$slot")"
  release_rc=$?
  if [ "$release_rc" -ne 0 ]; then
    die 13 "revoke-rework packet revoked but slot release failed pr=$pr slot=${slot:-none} packet_id=$packet_id"
  fi
  assert_no_slot_owner_for_phase "$pr" "$issue" "$branch" "revoke-rework"

  if [ "$preserve_independent_blockers" = "1" ]; then
    post_labels="$(gh pr view "$pr" --repo "$REPO" --json labels --jq '.labels[].name' 2>/dev/null || true)"
    printf '%s\n' "$post_labels" | grep -qx 'pm-state:blocked-rework' \
      || die 15 "revoke-rework lost blocked-rework state while preserving independent blockers pr=$pr"
    while IFS= read -r preserved_blocker; do
      [ -n "$preserved_blocker" ] || continue
      printf '%s\n' "$post_labels" | grep -qx "$preserved_blocker" \
        || die 15 "revoke-rework lost independent blocker pr=$pr blocker=$preserved_blocker"
    done <<< "$disallowed_blockers"
    resolve_pr_obligation_kinds "$pr" "$issue" "rework_revoked_independent_blockers_preserved" \
      "head=$head packet_id=$packet_id proof_sha256=$proof_digest blockers=$(printf '%s' "$disallowed_blockers" | paste -sd, -)" \
      rework rework_checkpoint rework_delivery_pending rework_slot_idle rework_revoke_release_pending
    upsert_obligation --kind blocked_rework --severity high \
      --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" \
      --owner pm --horizon hourly --next-review-at "$(utc_plus_minutes 10)" \
      --title "PR #$pr remains blocked after packet-only rework revocation" \
      --action "Resolve the preserved independent blocker(s) through their canonical typed transition. Do not re-record or re-dispatch the revoked packet." \
      --blocker "independent_blocker_preserved" \
      --evidence "head=$head" --evidence "packet_id=$packet_id" \
      --evidence "blockers=$(printf '%s' "$disallowed_blockers" | paste -sd, -)" \
      --evidence "proof=$proof" --evidence "proof_sha256=$proof_digest"
    record_event --source pm-transition --event rework_revoked \
      --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" \
      "${event_slot_args[@]}" --head-sha "$head" \
      --payload "packet_id=$packet_id" --payload "reason=$reason" \
      --payload "proof=$proof" --payload "proof_sha256=$proof_digest" \
      --payload "expected_epoch=${expected_epoch:-none}" \
      --payload "released_slots=${released_slots:-none}" \
      --payload "preserved_independent_blockers=$(printf '%s' "$disallowed_blockers" | paste -sd, -)"
    kanban_flag PM_TRANSITION "rework_revoked_independent_blockers_preserved pr=$pr issue=$issue head=$head packet_id=$packet_id released_slots=${released_slots:-none}"
    echo "PM_TRANSITION_OK command=revoke-rework pr=$pr issue=$issue head=${head:0:10} packet_id=$packet_id proof_sha256=$proof_digest released_slots=${released_slots:-none} state=blocked-rework next=resolve-independent-blocker preserved_blockers=$(printf '%s' "$disallowed_blockers" | paste -sd, -)"
    return 0
  fi

  bash "$PM_STATE" "$pr" pm-review-pending \
    || die 1 "failed to move revoked PR #$pr to pm-review-pending"
  gh pr edit "$pr" --repo "$REPO" --remove-label "pm-blocked:codex" --remove-label "pm-blocked:pm-gate" >/dev/null 2>&1 || true
  post_labels="$(gh pr view "$pr" --repo "$REPO" --json labels --jq '.labels[].name' 2>/dev/null || true)"
  if printf '%s\n' "$post_labels" | grep -qE '^(pm-blocked:codex|pm-blocked:pm-gate)$'; then
    die 15 "revoke-rework could not clear superseded review blockers pr=$pr"
  fi
  gh issue edit "$issue" --repo "$REPO" --remove-label "status:in-progress" --add-label "status:in-review" >/dev/null 2>&1 || true
  resolve_pr_obligation_kinds "$pr" "$issue" "rework_revoked" \
    "head=$head packet_id=$packet_id proof_sha256=$proof_digest" \
    rework rework_checkpoint rework_delivery_pending blocked_rework rework_slot_idle rework_revoke_release_pending
  upsert_obligation --kind pm_review_pending --severity high \
    --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" \
    --owner pm --horizon hourly --next-review-at "$(utc_plus_minutes 10)" \
    --title "PR #$pr revoked rework must continue through exact-head PM review" \
    --action "Run pm-transition pm-review --pr $pr --scope phase-a --reason rework-revoked, then complete the exact-head affected proof and typed CI handoff. Do not re-record or re-dispatch the revoked packet." \
    --blocker "pm_review_pending" \
    --evidence "head=$head" --evidence "packet_id=$packet_id" \
    --evidence "proof=$proof" --evidence "proof_sha256=$proof_digest"
  record_event --source pm-transition --event rework_revoked \
    --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" \
    "${event_slot_args[@]}" --head-sha "$head" \
    --payload "packet_id=$packet_id" --payload "reason=$reason" \
    --payload "proof=$proof" --payload "proof_sha256=$proof_digest" \
    --payload "expected_epoch=${expected_epoch:-none}" \
    --payload "released_slots=${released_slots:-none}"
  kanban_flag PM_TRANSITION "rework_revoked pr=$pr issue=$issue head=$head packet_id=$packet_id released_slots=${released_slots:-none}"
  echo "PM_TRANSITION_OK command=revoke-rework pr=$pr issue=$issue head=${head:0:10} packet_id=$packet_id proof_sha256=$proof_digest released_slots=${released_slots:-none} state=pm-review-pending next=pm-review"
}

cmd_ci_watch() {
  local pr="" issue="" slot="" classification="" failed_run="" baseline_run="" proof=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      --issue) issue="${2:-}"; shift 2 ;;
      --slot) slot="${2:-}"; shift 2 ;;
      --classification) classification="${2:-}"; shift 2 ;;
      --failed-run) failed_run="${2:-}"; shift 2 ;;
      --baseline-run) baseline_run="${2:-}"; shift 2 ;;
      --proof) proof="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown ci-watch arg $1" ;;
    esac
  done
  need_num pr "$pr"
  need_num failed-run "$failed_run"
  [ -z "$baseline_run" ] || need_num baseline-run "$baseline_run"
  [ -n "$classification" ] || die 2 "--classification is required"
  [ -f "$proof" ] || die 2 "proof file not found: $proof"
  if [ -z "$issue" ]; then issue="$(issue_from_pr "$pr")"; fi
  [ -z "$issue" ] || need_num issue "$issue"
  if [ -z "$slot" ]; then slot="$(slot_from_labels "$pr" "$issue")"; fi
  [ -z "$slot" ] || need_num slot "$slot"

  local pr_json state branch head labels other_states marker released_slots
  pr_json="$(gh pr view "$pr" --repo "$REPO" --json state,headRefName,headRefOid,labels 2>/dev/null || true)"
  [ -n "$pr_json" ] || die 1 "cannot read PR #$pr"
  state="$(printf '%s' "$pr_json" | json_field state)"
  [ "$state" = "OPEN" ] || die 1 "PR #$pr is not open"
  branch="$(printf '%s' "$pr_json" | json_field headRefName)"
  head="$(printf '%s' "$pr_json" | json_field headRefOid)"
  [ -n "$head" ] || die 1 "PR #$pr has no headRefOid"
  labels="$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print("\n".join(x.get("name", "") for x in json.load(sys.stdin).get("labels", [])))')"
  [ "$(printf '%s\n' "$labels" | grep -c '^pm-state:qa-passed-awaiting-ci$' || true)" = "1" ] \
    || die 43 "ci-watch requires exactly pm-state:qa-passed-awaiting-ci on PR #$pr"
  other_states="$(printf '%s\n' "$labels" | grep '^pm-state:' | grep -v '^pm-state:qa-passed-awaiting-ci$' || true)"
  [ -z "$other_states" ] || die 43 "ci-watch refuses contradictory PM state labels on PR #$pr: $other_states"

  python3 - "$proof" "$head" "$failed_run" <<'PY'
import sys
from pathlib import Path

path, head, failed_run = sys.argv[1:]
text = Path(path).read_text(encoding="utf-8", errors="replace")
head_variants = {head, head[:12], head[:10], head[:8]}
if not any(value and value in text for value in head_variants):
    raise SystemExit(f"proof does not name current head {head}")
if failed_run not in text:
    raise SystemExit(f"proof does not name failed run {failed_run}")
PY
  [ "$?" -eq 0 ] || die 43 "ci-watch proof is not current-head/current-run evidence for PR #$pr"

  marker="/tmp/pm-ci-watch-${pr}.json"
  python3 - "$marker" "$pr" "${issue:-}" "$branch" "$head" "$classification" "$failed_run" "$baseline_run" "$proof" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

marker, pr, issue, branch, head, classification, failed_run, baseline_run, proof = sys.argv[1:]
payload = {
    "schema": "pm-ci-watch/v1",
    "status": "active",
    "pr": int(pr),
    "issue": int(issue) if issue else None,
    "branch": branch,
    "headRefOid": head,
    "classification": classification,
    "failed_run": int(failed_run),
    "baseline_run": int(baseline_run) if baseline_run else None,
    "proof": str(Path(proof).resolve()),
    "created_at": datetime.now(timezone.utc).isoformat(),
}
path = Path(marker)
tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(path)
PY

  gh pr edit "$pr" --repo "$REPO" --add-label "pm-blocked:ci" >/dev/null \
    || die 1 "failed to add pm-blocked:ci to PR #$pr"
  archive_slot_ready_events_for_pr "$pr" "ci-watch"
  released_slots="$(release_target_slots "$pr" "$issue" "$branch" "ci-watch" "$slot")"
  resolve_pr_obligation_kinds "$pr" "$issue" "ci_watch_started" "classification=$classification failed_run=$failed_run head=$head marker=$marker" ci_local_preflight ci_rerun_after_preflight ci_reconcile
  upsert_obligation --kind ci_watch --severity high --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --owner pm --horizon hourly --next-review-at "$(utc_plus_minutes 30)" --title "PR #$pr classified same-head CI hold" --action "Watch failed run $failed_run on head $head. Do not toggle qa-passed-awaiting-ci. A new failed run requires fresh classification; terminal green resumes readiness." --blocker "classified_same_head_ci_hold" --evidence "classification=$classification" --evidence "marker=$marker" --evidence "proof=$proof"
  record_event --source pm-transition --event ci_watch_started --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --slot "$slot" --head-sha "$head" --payload "classification=$classification" --payload "failed_run=$failed_run" --payload "baseline_run=${baseline_run:-none}" --payload "marker=$marker" --payload "released_slots=${released_slots:-none}" --dedupe
  kanban_flag PM_TRANSITION "ci_watch pr=$pr failed_run=$failed_run head=${head:0:10} released=${released_slots:-none}"
  [ -n "$released_slots" ] && run_post_release_sweep "ci-watch"
  echo "PM_TRANSITION_OK command=ci-watch pr=$pr state=qa-passed-awaiting-ci label=pm-blocked:ci classification=$classification failed_run=$failed_run marker=$marker released_slots=${released_slots:-none}"
}

validate_split_successor_rework() {
  local pr="$1" successor_issue="$2" head="$3"
  python3 - "$pr" "$successor_issue" "$head" "$RESCOPE_CONTRACT_TOOL" <<'PY'
import importlib.util
import json
import sys
from pathlib import Path

pr = int(sys.argv[1])
successor = int(sys.argv[2])
head = sys.argv[3]
tool_path = Path(sys.argv[4])
marker_path = Path(f"/tmp/pm-rescope-pr-{pr}.json")

def fail(reason: str) -> None:
    print(f"split_successor_rework_invalid reason={reason}", file=sys.stderr)
    raise SystemExit(1)

try:
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
except (OSError, ValueError) as exc:
    fail(f"marker_unreadable marker={marker_path} detail={exc}")

if int(marker.get("pr") or 0) != pr:
    fail("marker_pr_mismatch")
if str(marker.get("headRefOid") or "") != head:
    fail("marker_head_mismatch")
if marker.get("status") != "resolved":
    fail("marker_not_resolved")
if marker.get("terminal_decision") != "split_and_reimplement":
    fail("marker_not_split_and_reimplement")

source_issue = int(marker.get("issue") or 0)
if source_issue <= 0 or source_issue == successor:
    fail("invalid_source_successor_tuple")
rescope = marker.get("rescope_contract")
if not isinstance(rescope, dict):
    fail("marker_rescope_contract_missing")
contract_path = Path(str(rescope.get("source_file") or ""))
recorded_digest = str(rescope.get("digest") or "")
if not contract_path.is_file() or not recorded_digest:
    fail("contract_source_or_digest_missing")
try:
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
except (OSError, ValueError) as exc:
    fail(f"contract_unreadable contract={contract_path} detail={exc}")

if not tool_path.is_file():
    fail(f"contract_tool_missing tool={tool_path}")
spec = importlib.util.spec_from_file_location("pm_rescope_contract", tool_path)
if spec is None or spec.loader is None:
    fail("contract_tool_unloadable")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
try:
    normalized = module.normalize_contract(
        contract,
        issue=source_issue,
        pr=pr,
        decision="split_and_reimplement",
    )
except Exception as exc:
    fail(f"contract_invalid detail={exc}")

if normalized.get("digest") != recorded_digest:
    fail("contract_digest_mismatch")
if normalized.get("rescope_type") != "split":
    fail("contract_not_split")
if successor not in normalized.get("follow_up_issues", []):
    fail("successor_not_authorized")
if str(contract.get("status") or "").lower() != "superseded":
    fail("source_not_superseded")

print(
    "\t".join(
        (
            str(source_issue),
            str(marker_path),
            str(contract_path),
            recorded_digest,
        )
    )
)
PY
}

# ---------------------------------------------------------------------------
# Receipt-emittability admission for receipt-style holds (obligation 11694
# RUN 2, incident control-plane:receipt-emittability-admission). A
# receipt-style hold declares its demanded exit-evidence with a
# machine-observable `hold-evidence=<kind>` token inside --reason. The
# runtime refuses the hold BEFORE any mutation when the demanded receipt
# kind cannot be emitted, writing a typed receipt_unemittable packet
# (decision class, evidence, recommended default, required transition) and
# exiting 49 — the #7229 8.56h impossible-receipt hold shape must never be
# admitted silently.
# ---------------------------------------------------------------------------

receipt_kind_emittable() {
  # Closed registry of receipt kinds the control-plane runtime can emit.
  # Anything else is unemittable and a hold demanding it fails closed.
  case "$1" in
    affected_test_plan|local_preflight|review_cap_dispatch|rescope_contract|split_completion|capture_release|dependency_recovery|cto_rescue_handoff|rework_packet)
      return 0 ;;
    *) return 1 ;;
  esac
}

extract_hold_evidence() {
  local reason="$1"
  if [[ "$reason" =~ (^|[[:space:]])hold-evidence=([a-z0-9_]+)([[:space:]]|$) ]]; then
    printf '%s\n' "${BASH_REMATCH[2]}"
  fi
}

write_receipt_unemittable_packet() {
  local packet_json="$1" packet_md="$2" command="$3" pr="$4" issue="$5" head="$6" kind="$7" reason="$8"
  local generated_at runtime_surfaces
  generated_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  runtime_surfaces="affected_test_plan,local_preflight,review_cap_dispatch,rescope_contract,split_completion,capture_release,dependency_recovery,cto_rescue_handoff,rework_packet"
  COMMAND="$command" PR="$pr" ISSUE="${issue:-}" HEAD="${head:-}" KIND="$kind" REASON="$reason" \
    GENERATED_AT="$generated_at" RUNTIME_SURFACES="$runtime_surfaces" \
    python3 - "$packet_json" "$packet_md" <<'PY' || return 1
import json
import os
import sys

packet_json, packet_md = sys.argv[1:]
command = os.environ["COMMAND"]
pr = os.environ.get("PR") or None
issue = os.environ.get("ISSUE") or None
head = os.environ.get("HEAD") or None
kind = os.environ["KIND"]
reason = os.environ["REASON"]
generated_at = os.environ["GENERATED_AT"]
runtime_surfaces = os.environ["RUNTIME_SURFACES"]

packet = {
    "schema": "heydonna_receipt_unemittable",
    "version": 1,
    "generated_at": generated_at,
    "decision_class": "receipt_unemittable_hold_refused",
    "incident": "control-plane:receipt-emittability-admission",
    "incident_thread": "1786409291.138769",
    "command": command,
    "pr": pr,
    "issue": issue,
    "head": head,
    "hold_evidence_kind": kind,
    "evidence": {
        "reason": reason,
        "emittable": False,
        "runtime_receipt_kinds": runtime_surfaces,
    },
    "recommended_default": (
        "Do not enter or extend a receipt-style hold whose demanded "
        f"success-evidence kind '{kind}' the control-plane runtime cannot "
        "emit. Re-express the exit condition as a typed blocker plus a "
        "concrete machine-observable wake (obligation-upsert --blocker "
        "<typed> --next-review-at <iso-utc>), or use a runtime-emittable "
        f"receipt kind from {runtime_surfaces}; otherwise escalate to the "
        "CTO thread with decision class, evidence, recommended default "
        "per Rajiv directive 2026-08-11 08:27 (thread 1786409291.138769)."
    ),
    "required_pm_transition": (
        "Do not run block-pr/park-issue/resolve-pm-gate with an unemittable "
        "hold-evidence kind. Record the typed blocker + wake, or post the "
        "CTO-thread escalation from this packet; an un-emittable receipt "
        "must never gate a live-user P1."
    ),
}
with open(packet_json, "w") as fh:
    json.dump(packet, fh, indent=2, sort_keys=True)
    fh.write("\n")

if pr:
    target_line = f"- target: pr={pr}"
else:
    target_line = f"- target: issue={issue or 'unknown'}"
lines = [
    "PM_CONTROL_PLANE_RECEIPT_UNEMITTABLE",
    "",
    f"- decision_class: {packet['decision_class']}",
    f"- incident: {packet['incident']}",
    f"- incident_thread: {packet['incident_thread']}",
    f"- command: {command}",
    target_line,
    f"- pr/issue: {pr or '-'}/{issue or '-'}",
    f"- head: {head or '-'}",
    f"- hold_evidence_kind: {kind}",
    f"- evidence: reason={reason} emittable=false "
    f"runtime_receipt_kinds={runtime_surfaces}",
    f"- recommended_default: {packet['recommended_default']}",
    f"- required_pm_transition: {packet['required_pm_transition']}",
    f"- generated_at: {generated_at}",
    "",
]
with open(packet_md, "w") as fh:
    fh.write("\n".join(lines))
PY
}

admit_receipt_style_hold() {
  # Usage: admit_receipt_style_hold <command> <pr> <issue> <head> <reason>
  # Returns 0 when no receipt is demanded or the demanded receipt kind is
  # emittable. Otherwise refuses the hold BEFORE any mutation: writes a typed
  # receipt_unemittable packet (idempotent per hold key; any head/kind/target
  # change re-arms a new key) and exits 49. A packet-write failure still
  # refuses the hold (fail closed).
  local command="$1" pr="$2" issue="$3" head="$4" reason="$5"
  local kind target_type target_id hold_key dir packet_json packet_md
  kind="$(extract_hold_evidence "$reason")"
  [ -n "$kind" ] || return 0
  if receipt_kind_emittable "$kind"; then
    return 0
  fi
  if [ -n "$pr" ]; then
    target_type="pr"; target_id="$pr"
  else
    target_type="issue"; target_id="${issue:-unknown}"
  fi
  hold_key="receipt_unemittable:${command}:${target_type}:${target_id}:${head:-none}:${kind}"
  dir="${PM_RECEIPT_UNEMITTABLE_DIR:-/tmp}"
  packet_json="${dir}/pm-control-plane-receipt-unemittable-${command}-${target_type}-${target_id}-${head:-none}-${kind}.json"
  packet_md="${packet_json%.json}.md"
  if ! mkdir -p "$dir" 2>/dev/null \
    || ! write_receipt_unemittable_packet "$packet_json" "$packet_md" "$command" "$pr" "$issue" "$head" "$kind" "$reason"; then
    die 49 "receipt_unemittable_refused_failed_closed command=$command target=$target_type:$target_id issue=${issue:-none} head=${head:-none} kind=$kind packet_write_failed=$packet_json"
  fi
  if [ "$target_type" = "pr" ]; then
    record_event --source pm-transition --event receipt_unemittable_refused --target-type pr --target-id "$target_id" --pr "$target_id" --issue "$issue" --head-sha "$head" --payload "command=$command" --payload "kind=$kind" --payload "packet=$packet_json" --dedupe-key "$hold_key"
  else
    record_event --source pm-transition --event receipt_unemittable_refused --target-type issue --target-id "$target_id" --issue "$target_id" --head-sha "$head" --payload "command=$command" --payload "kind=$kind" --payload "packet=$packet_json" --dedupe-key "$hold_key"
  fi
  die 49 "receipt_unemittable_refused command=$command target=$target_type:$target_id issue=${issue:-none} head=${head:-none} kind=$kind packet=$packet_json"
}

cmd_block_pr() {
  local pr="" issue="" slot="" reason="" completed_head="" expected_epoch="" packet="" loop_class="" ci_class="" failed_run="" failed_suite="" local_preflight_proof="" capture_run="" dependency_pr="" question="" recommended_default="" source_citation=""
  local rework_packet_id="" rework_comment_url="" source_issue="" split_successor=0
  local split_successor_context="" split_marker="" split_contract="" split_digest=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      --issue) issue="${2:-}"; shift 2 ;;
      --slot) slot="${2:-}"; shift 2 ;;
      --reason) reason="${2:-}"; shift 2 ;;
      --completed-head) completed_head="${2:-}"; shift 2 ;;
      --expected-epoch) expected_epoch="${2:-}"; shift 2 ;;
      --packet) packet="${2:-}"; shift 2 ;;
      --loop-class) loop_class="${2:-}"; shift 2 ;;
      --ci-class) ci_class="${2:-}"; shift 2 ;;
      --failed-run) failed_run="${2:-}"; shift 2 ;;
      --failed-suite) failed_suite="${2:-}"; shift 2 ;;
      --local-preflight-proof) local_preflight_proof="${2:-}"; shift 2 ;;
      --capture-run) capture_run="${2:-}"; shift 2 ;;
      --dependency-pr) dependency_pr="${2:-}"; shift 2 ;;
      --question) question="${2:-}"; shift 2 ;;
      --recommended-default) recommended_default="${2:-}"; shift 2 ;;
      --source-citation) source_citation="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown block-pr arg $1" ;;
    esac
  done
  need_num pr "$pr"
  [ -n "$reason" ] || die 2 "--reason is required"
  if [ "$reason" = "product" ] || [ "$reason" = "rajiv" ] || [ "$reason" = "blocked-on-rajiv" ]; then
    [ -n "$question" ] || die 2 "product block requires --question with one exact unresolved product/data-model/authority question"
    [ -n "$recommended_default" ] || die 2 "product block requires --recommended-default"
    [ -n "$source_citation" ] || die 2 "product block requires --source-citation to the directive/spec/evidence that leaves the question unresolved"
  fi

  source_issue="$(issue_from_pr "$pr")"
  if [ -z "$issue" ]; then issue="$source_issue"; fi
  [ -n "$issue" ] && need_num issue "$issue"
  if [ -z "$slot" ]; then slot="$(slot_from_labels "$pr" "$issue")"; fi
  [ -z "$slot" ] || need_num slot "$slot"

  local block_label
  block_label="$(blocked_label_for_reason "$reason")"
  local pr_json branch head labels review_loop_proof issue_gate_parked=0 issue_live_json="" issue_live_labels="" issue_live_state=""
  pr_json="$(pr_metadata_json "$pr" || true)"
  branch="$(printf '%s' "$pr_json" | json_field headRefName 2>/dev/null || true)"
  head="$(printf '%s' "$pr_json" | json_field headRefOid 2>/dev/null || true)"
  labels="$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print(",".join(x.get("name", "") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
  if [ "$reason" = "pm-gate" ] && [ -n "$issue" ]; then
    issue_live_json="$(gh issue view "$issue" --repo "$REPO" --json state,labels 2>/dev/null || true)"
    [ -n "$issue_live_json" ] || die 1 "cannot read linked issue #$issue before PM-gate recovery"
    issue_live_state="$(printf '%s' "$issue_live_json" | json_field state 2>/dev/null || true)"
    issue_live_labels="$(printf '%s' "$issue_live_json" | python3 -c 'import json,sys; print(",".join(x.get("name", "") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
    [ "$issue_live_state" = "OPEN" ] || die 13 "linked issue #$issue is not OPEN during PM-gate recovery"
    if labels_include "$issue_live_labels" "status:blocked"; then
      labels_include "$issue_live_labels" "pm-blocked:pm-gate" \
        || die 13 "linked issue #$issue is status:blocked without pm-blocked:pm-gate"
      issue_gate_parked=1
    fi
  fi
  if [ "$reason" = "ci" ] && [ -n "$packet" ] && [ -n "$issue" ] \
    && [ "$issue" != "${source_issue:-}" ]; then
    split_successor_context="$(validate_split_successor_rework "$pr" "$issue" "$head")" \
      || die 44 "CI rework issue #$issue differs from PR #$pr source issue #${source_issue:-unknown} without a valid exact-head split-successor contract"
    IFS="$(printf '\t')" read -r source_issue split_marker split_contract split_digest <<< "$split_successor_context"
    split_successor=1
    local successor_json successor_state successor_labels
    successor_json="$(gh issue view "$issue" --repo "$REPO" --json state,labels 2>/dev/null || true)"
    [ -n "$successor_json" ] || die 44 "cannot read split successor issue #$issue"
    successor_state="$(printf '%s' "$successor_json" | json_field state 2>/dev/null || true)"
    successor_labels="$(printf '%s' "$successor_json" | python3 -c 'import json,sys; print(",".join(x.get("name", "") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
    [ "$successor_state" = "OPEN" ] || die 44 "split successor issue #$issue is not open"
    ! printf '%s\n' "$successor_labels" | tr ',' '\n' | grep -Eq '^slot:[1-4]$|^status:in-progress$' \
      || die 44 "split successor issue #$issue already has an active owner labels=$successor_labels"
  fi
  if [ "$reason" = "pm-review-wait" ]; then
    [ -n "$slot" ] || die 2 "pm-review-wait requires an owning --slot"
    need_num expected-epoch "$expected_epoch"
    [ -n "$completed_head" ] || die 2 "pm-review-wait requires --completed-head with the exact pushed PR head"
    [ -n "$head" ] || die 1 "cannot read current head for PR #$pr"
    [ "$completed_head" = "$head" ] \
      || die 13 "completed head mismatch pr=$pr completed=$completed_head current=$head"
    mop_slot_matches_target "$slot" "$pr" "$issue" "$branch" \
      || die 13 "pm-review-wait target mismatch slot=$slot pr=$pr issue=${issue:-unknown} branch=${branch:-unknown}"
    local current_epoch turn_state
    current_epoch="$(mop_slot_epoch "$slot")" \
      || die 30 "cannot read assignment_epoch for slot $slot"
    [ "$expected_epoch" = "$current_epoch" ] \
      || die 13 "assignment_epoch mismatch slot=$slot expected=$expected_epoch current=$current_epoch"
    turn_state="$(mop_slot_turn_state "$slot")" \
      || die 13 "agent-turn authority unavailable for slot $slot"
    [ "$turn_state" = "inactive" ] \
      || die 13 "pm-review-wait blocked slot=$slot active_turn_state=$turn_state"
    PM_MUTATION_EXPECTED_EPOCH="$expected_epoch"
    # Python owns the release, projection, obligation, and retryable outbox
    # atomically.  Retire this shell path before any label/release mutation so
    # it cannot double-release or admit stale-head CI.
    family2_pm_review_wait "$pr" "$issue" "$slot" "$head" "$expected_epoch"
    return $?
  fi
  admit_receipt_style_hold block-pr "$pr" "$issue" "$head" "$reason"
  if [ "$block_label" = "pm-blocked:product" ] \
    && { labels_include "$labels" "pm-state:qa-passed-awaiting-ci" \
      || labels_include "$labels" "merge-ready" \
      || labels_include "$labels" "pm-state:merge-ready"; }; then
    die 42 "product_block_regression_refused pr=$pr head=${head:0:10} state=post_review; use pm-transition rescope-pr --pr $pr, then rescope-decide --decision escalate_product_decision with current-head evidence"
  fi
  if [ -n "$packet" ]; then
    [ -f "$packet" ] || die 2 "packet file not found: $packet"
    [ -n "$issue" ] || die 43 "cannot persist rework packet for PR #$pr without linked issue"
    local packet_result
    packet_result="$(publish_rework_packet "$pr" "$issue" "$head" "$reason" "$packet")" \
      || die 43 "failed to persist current-head rework packet for PR #$pr"
    printf '%s\n' "$packet_result" >/tmp/pm-rework-packet-publish-${pr}.json
    rework_packet_id="$(printf '%s' "$packet_result" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("packet_id", ""))' 2>/dev/null || true)"
    rework_comment_url="$(printf '%s' "$packet_result" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("comment_url", ""))' 2>/dev/null || true)"
    [ -n "$rework_packet_id" ] \
      || die 43 "rework packet ledger returned no packet_id for PR #$pr"
    record_event --source pm-transition --event rework_packet_recorded --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --head-sha "$head" --payload "kind=$reason" --payload "packet_id=$rework_packet_id" --payload "comment_url=${rework_comment_url:-unknown}" --payload "source_packet=$packet"
  fi
  if [ "$reason" = "capture" ]; then
    bash "$PM_STATE" "$pr" pm-review-pending || die 1 "failed to move PR #$pr to pm-review-pending for capture block"
    remove_pm_blockers "$pr" "" >/dev/null 2>&1 || true
    gh pr edit "$pr" --repo "$REPO" --add-label "$block_label" >/dev/null || die 1 "failed to add $block_label to PR #$pr"
    if ! remote_capture_only_enabled; then
      die 12 "REMOTE_CAPTURE_ONLY is disabled; local capture is diagnostic-only and cannot satisfy capture readiness. Re-enable remote capture or use capture-local-required --reason <named-infra-defect> for debugging only."
    fi
    priority_rework_index_invalidate "blocking_state pr=$pr issue=${issue:-unknown} reason=capture"
    if [ -n "$issue" ]; then
      cmd_capture_remote_dispatch --pr "$pr" --issue "$issue" --head "$head"
    else
      cmd_capture_remote_dispatch --pr "$pr" --head "$head"
    fi
    return 0
  fi
  if [ "$reason" = "dependency" ]; then
    need_num dependency-pr "$dependency_pr"
    local dependency_payload
    dependency_payload="$(write_dependency_payload "$pr" "$issue" "$branch" "$head" "dependency" "$dependency_pr")"
    [ -n "$dependency_payload" ] || die 1 "failed to create dependency payload for PR #$pr"
    remove_pm_blockers "$pr" >/dev/null 2>&1 || true
    gh pr edit "$pr" --repo "$REPO" --add-label "$block_label" >/dev/null || die 1 "failed to add $block_label to PR #$pr"
    # Add the blocker before removing qa-passed-awaiting-ci. GitHub emits a
    # pull_request event for every label mutation; this ordering keeps every
    # event fail-closed and prevents a transient real CI/E2E allocation.
    bash "$PM_STATE" "$pr" blocked-rework || die 1 "failed to park PR #$pr in blocked-rework"
    archive_slot_ready_events_for_pr "$pr" "block-pr:dependency"
    local released_slots
    released_slots="$(release_target_slots "$pr" "$issue" "$branch" "block-pr:dependency" "$slot")"
    if [ -n "$issue" ]; then
      gh issue edit "$issue" --repo "$REPO" --remove-label "status:todo" >/dev/null 2>&1 || true
      gh issue edit "$issue" --repo "$REPO" --remove-label "status:in-progress" >/dev/null 2>&1 || true
      gh issue edit "$issue" --repo "$REPO" --add-label "status:in-review" >/dev/null || die 1 "failed to add status:in-review to issue #$issue"
    fi
    resolve_pr_obligation_kinds "$pr" "$issue" "dependency_watch_started" "dependency=$dependency_pr head=$head payload=$dependency_payload" \
      dependency_wedge pr_state_reconcile pr_state_missing pm_review_complete ci_local_preflight ci_rerun_after_preflight
    upsert_obligation --kind dependency_watch --severity high --target-type pr --target-id "$pr" --pr "$pr" --owner pm --horizon hourly --title "PR #$pr waits on dependency before exact-head review" --action "Keep pm-state:blocked-rework + pm-blocked:dependency until the named dependency merges/closes. Then retarget/rebase this PR onto main, verify the new exact head, and directly fire one label-gated CI+E2E wave; one functionality-first review may run in parallel and gates merge, not CI start. Planner/capture/marker/preflight ceremony must not block CI start." --blocker "dependency_watch" --evidence "label=$block_label" --evidence "payload=${dependency_payload:-missing}"
    record_event --source pm-transition --event dependency_watch_started --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --slot "$slot" --payload "branch=$branch" --payload "released_slots=${released_slots:-none}" --payload "payload=${dependency_payload:-missing}" --dedupe
    kanban_flag PM_TRANSITION "dependency_watch pr=$pr issue=$issue released=${released_slots:-none}"
    [ -n "$released_slots" ] && run_post_release_sweep "block-pr-dependency"
    priority_rework_index_invalidate "blocking_state pr=$pr issue=${issue:-unknown} reason=dependency"
    echo "PM_TRANSITION_OK command=block-pr pr=$pr reason=dependency label=$block_label state=blocked-rework released_slots=${released_slots:-none} payload=${dependency_payload:-missing}"
    return 0
  fi
  if [ "$reason" = "pm-review-wait" ]; then
    bash "$PM_STATE" "$pr" pm-review-pending || die 1 "failed to move PR #$pr to pm-review-pending"
  else
    bash "$PM_STATE" "$pr" blocked-rework || die 1 "failed to move PR #$pr to blocked-rework"
  fi
  remove_pm_blockers "$pr" >/dev/null 2>&1 || true
  gh pr edit "$pr" --repo "$REPO" --add-label "$block_label" >/dev/null || die 1 "failed to add $block_label to PR #$pr"
  archive_slot_ready_events_for_pr "$pr" "block-pr:$reason"
  local released_slots kept_owner_slot=""
  if [ -n "$slot" ] \
    && ! block_reason_releases_slot "$reason" \
    && mop_slot_matches_target "$slot" "$pr" "$issue" "$branch"; then
    kept_owner_slot="$slot"
  fi
  released_slots="$(release_target_slots "$pr" "$issue" "$branch" "block-pr:$reason" "$slot" "$kept_owner_slot")"
  if [ "$reason" = "pm-review-wait" ] && ! printf '%s\n' "$released_slots" | tr ' ' '\n' | grep -qx "$slot"; then
    die 13 "pm-review-wait did not release owning slot=$slot pr=$pr; checkout or release predicate remains blocked"
  fi
  if [ -n "$issue" ]; then
    if [ "$split_successor" = "1" ]; then
      # Keep the successor non-dispatchable until its issue-native priority
      # obligation is durable below. That closes the mutation window where a
      # generic todo sweep could claim it without the bound rework packet.
      :
    elif [ "$issue_gate_parked" = "1" ]; then
      # The issue-level PM gate is already the authoritative parked state.
      # Reconcile only the orphan PR; adding status:in-review here would create
      # two issue status labels and incorrectly unpark the issue contract.
      :
    else
      gh issue edit "$issue" --repo "$REPO" --remove-label "status:todo" >/dev/null 2>&1 || true
      gh issue edit "$issue" --repo "$REPO" --remove-label "status:in-progress" >/dev/null 2>&1 || true
      gh issue edit "$issue" --repo "$REPO" --add-label "status:in-review" >/dev/null || die 1 "failed to add status:in-review to issue #$issue"
    fi
  fi

  local target_args=()
  [ -n "$issue" ] && target_args+=(--issue "$issue")
  [ -n "$kept_owner_slot" ] && target_args+=(--slot "$kept_owner_slot")

  local obligation_kind="blocked_rework" obligation_title="PR #$pr blocked for rework" obligation_action="Resolve the current branch/head/expected epoch/repository/handoff and assign rework through the complete claim_slot tuple" obligation_blocker="$reason"
  local obligation_extra_args=()
  local evidence_args=(--evidence "label=$block_label")
  [ -n "$ci_class" ] && evidence_args+=(--evidence "ci_class=$ci_class")
  [ -n "$failed_run" ] && evidence_args+=(--evidence "failed_run=$failed_run")
  [ -n "$failed_suite" ] && evidence_args+=(--evidence "failed_suite=$failed_suite")
  [ -n "$local_preflight_proof" ] && evidence_args+=(--evidence "local_preflight_proof=$local_preflight_proof")
  [ -n "$capture_run" ] && evidence_args+=(--evidence "capture_run=$capture_run")
  [ -n "$dependency_pr" ] && evidence_args+=(--evidence "dependency_pr=$dependency_pr")
  if [ "$reason" = "ci" ]; then
    if [ -n "$packet" ]; then
      if [ "$split_successor" = "1" ]; then
        obligation_kind="successor_rework"
        obligation_title="Successor issue #$issue has durable CI rework from superseded PR #$pr"
        obligation_action="Run pm-transition reconcile-capacity. Assign successor issue #$issue from current main before fresh status:todo work; do not reuse PR #$pr or its branch."
        obligation_blocker="successor_rework_packet_ready"
      else
        obligation_kind="rework"
        obligation_title="PR #$pr has durable current-head CI rework packet"
        obligation_action="Resolve the complete branch/head/epoch/handoff tuple, then claim_slot assigns this PR to the first healthy free slot. The slot inspects exact failed-run logs and Modal cache, runs the canonical local repro, and pushes a descendant head; PM must not classify-terminal or trigger CI; CTO owns the next exact-head wave. No sealed proof is required."
        obligation_blocker="rework_packet_ready"
      fi
      obligation_extra_args=(--owner pm --horizon hourly)
      evidence_args+=(--evidence "packet_id=$rework_packet_id")
      evidence_args+=(--evidence "head=$head")
      evidence_args+=(--evidence "comment_url=${rework_comment_url:-unknown}")
      evidence_args+=(--evidence "source_packet=$packet")
      if [ "$split_successor" = "1" ]; then
        evidence_args+=(--evidence "assignment_mode=fresh_successor")
        evidence_args+=(--evidence "source_pr=$pr")
        evidence_args+=(--evidence "source_issue=$source_issue")
        evidence_args+=(--evidence "rescope_marker=$split_marker")
        evidence_args+=(--evidence "rescope_contract=$split_contract")
        evidence_args+=(--evidence "rescope_contract_digest=$split_digest")
      fi
      resolve_pr_obligation_kinds "$pr" "$issue" "ci_rework_packet_recorded" \
        "run=${failed_run:-unknown} packet_id=$rework_packet_id head=$head" ci_local_preflight blocked_rework
    else
      obligation_kind="ci_local_preflight"
      obligation_title="PR #$pr current-head CI/E2E failure requires local preflight"
      obligation_action="Immediately assign the failed run to the first healthy free slot. The slot inspects the exact GitHub run logs and Modal cache first, runs the named canonical local repro, fixes bugs found, and pushes a descendant head; the raw PASS/FAIL repro terminal is the classification (no sealed envelope). A proved cache miss may use one bounded capture, then the same slot resumes repro/rework. PM must not classify-terminal or trigger CI; CTO owns the next exact-head label-gated CI/E2E wave."
      obligation_blocker="${ci_class:-ci_local_preflight_required}"
    fi
  elif [ "$reason" = "pm-review-wait" ]; then
    obligation_kind="pm_review_wait"
    obligation_title="PR #$pr is slot-free and awaits PM review"
    obligation_action="Complete the exact-head PM review and materialize its canonical PASS marker for head $head first (#7399 atomic guard), then run pm-transition pm-review --pr $pr --scope phase-a (validates the marker before any state change). If review finds local rework, route it through assign-rework; otherwise run pm-review-done. Do not reserve a coding slot while PM owns review."
    obligation_blocker="pm_review_wait"
    evidence_args+=(--evidence "completed_head=$completed_head")
    evidence_args+=(--evidence "released_epoch=$expected_epoch")
    resolve_pr_obligation_kinds "$pr" "$issue" "pm_review_wait_started" \
      "head=$head released_slot=$slot epoch=$expected_epoch" blocked_rework rework_slot_idle
  elif [ "$reason" = "pm-gate" ]; then
    obligation_kind="pm_gate_review"
    obligation_title="PR #$pr is blocked on exact-head PM gate resolution"
    obligation_action="Resolve the cited exact-head PM review artifact before any slot assignment or CI. If code/proof rework is required, persist a current-head rework packet and reclassify through the canonical typed path; if the blocker is a product decision, use pm-blocked:product with one exact question and PM recommended default. Do not assign pm-blocked:pm-gate work."
    obligation_blocker="pm-gate"
    evidence_args+=(--evidence "head=$head")
    evidence_args+=(--evidence "issue_gate_parked=$issue_gate_parked")
  elif [ "$reason" = "product" ] || [ "$reason" = "rajiv" ] || [ "$reason" = "blocked-on-rajiv" ]; then
    obligation_kind="product_decision_wait"
    obligation_title="PR #$pr waits on a product/Rajiv decision"
    obligation_action="Record the exact product question and PM's recommended default. Keep the PR slot-free until Rajiv/product responds. If the block is resolved without code changes, complete a fresh exact-head PM review and materialize its canonical PASS marker (#7399 atomic guard), then run /Users/rajiv/.claude/scripts/pm-transition.sh pm-review --pr $pr --scope phase-a --reason product-decision-resolved (validates the marker before any state change), then run pm-review-done; if code changes are required, route them through assign-rework. Never clear the blocker with raw label edits."
    obligation_blocker="product_decision_wait"
    evidence_args+=(--evidence "question=$question")
    evidence_args+=(--evidence "recommended_default=$recommended_default")
    evidence_args+=(--evidence "source_citation=$source_citation")
  fi

  if [ "$split_successor" = "1" ] && [ "$obligation_kind" = "successor_rework" ]; then
    upsert_obligation --kind "$obligation_kind" --severity high \
      --target-type issue --target-id "$issue" --issue "$issue" \
      ${obligation_extra_args[@]+"${obligation_extra_args[@]}"} \
      --title "$obligation_title" --action "$obligation_action" \
      --blocker "$obligation_blocker" "${evidence_args[@]}"
    resolve_target_obligations --kind rework --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" \
      --reason "split_successor_rework_reconciled" --external-state "successor_issue=$issue packet_id=$rework_packet_id" >/dev/null 2>&1 || true
    gh issue edit "$issue" --repo "$REPO" --remove-label "status:in-review" >/dev/null 2>&1 || true
    gh issue edit "$issue" --repo "$REPO" --remove-label "status:in-progress" >/dev/null 2>&1 || true
    gh issue edit "$issue" --repo "$REPO" --remove-label "status:blocked" >/dev/null 2>&1 || true
    gh issue edit "$issue" --repo "$REPO" --add-label "status:todo" >/dev/null \
      || die 1 "failed to make split successor issue #$issue dispatchable after recording its priority obligation"
  else
    upsert_obligation --kind "$obligation_kind" --severity high --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} ${obligation_extra_args[@]+"${obligation_extra_args[@]}"} --title "$obligation_title" --action "$obligation_action" --blocker "$obligation_blocker" "${evidence_args[@]}"
  fi
  if [ "$reason" != "pm-review-wait" ]; then
    resolve_pr_obligation_kinds "$pr" "$issue" "blocked_rework_supersedes_review" \
      "reason=$reason head=$head label=$block_label" pm_review_pending
  fi
  if [ "$obligation_kind" != "product_decision_wait" ]; then
    resolve_pr_obligation_kinds "$pr" "$issue" "product_wait_reclassified" \
      "reason=$reason head=$head" product_decision_wait
  fi
  review_loop_proof=""
  if [ "$reason" != "pm-review-wait" ]; then
    review_loop_proof="$(record_review_loop_blocker "$pr" "$head" "$reason" "$block_label" "$loop_class")"
  fi
  if [[ "$review_loop_proof" == *"circuit_breaker=true"* ]]; then
    echo "PM_TRANSITION_REVIEW_CIRCUIT_BREAKER pr=$pr $review_loop_proof" >&2
    cmd_rescope_pr --pr "$pr" --issue "$issue" --reason "review-loop-circuit-breaker"
    return 0
  fi
  record_event --source pm-transition --event block_pr --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --payload "reason=$reason" --payload "label=$block_label" --payload "issue_gate_parked=$issue_gate_parked" --payload "released_slots=${released_slots:-none}" --payload "kept_owner_slot=${kept_owner_slot:-none}" --payload "completed_head=${completed_head:-none}" --payload "expected_epoch=${expected_epoch:-none}" --payload "ci_class=${ci_class:-none}" --payload "failed_run=${failed_run:-none}" --payload "failed_suite=${failed_suite:-none}" --payload "question=${question:-none}" --payload "recommended_default=${recommended_default:-none}" --payload "source_citation=${source_citation:-none}"
  kanban_flag PM_TRANSITION "block_pr pr=$pr issue=$issue slot=${released_slots:-${slot:-none}} reason=$reason kept_owner_slot=${kept_owner_slot:-none}"
  [ -n "$released_slots" ] && run_post_release_sweep "block-pr"
  [ "$split_successor" = "1" ] && capacity_reconcile_trigger successor_rework_ready
  priority_rework_index_invalidate "blocking_state pr=$pr issue=${issue:-unknown} reason=$reason"
  echo "PM_TRANSITION_OK command=block-pr pr=$pr issue=${issue:-unknown} issue_gate_parked=$issue_gate_parked released_slots=${released_slots:-none} kept_owner_slot=${kept_owner_slot:-none} label=$block_label ci_class=${ci_class:-none} failed_run=${failed_run:-none} rework_packet_id=${rework_packet_id:-none} rework_comment_url=${rework_comment_url:-none} assignment_mode=$([ "$split_successor" = "1" ] && printf fresh_successor || printf existing_pr)"
}

cmd_retract_operator_block() {
  local pr="" reason="" expected_head="" slot="" expected_epoch="" proof=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      --reason) reason="${2:-}"; shift 2 ;;
      --expected-head) expected_head="${2:-}"; shift 2 ;;
      --slot) slot="${2:-}"; shift 2 ;;
      --expected-epoch) expected_epoch="${2:-}"; shift 2 ;;
      --proof) proof="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown retract-operator-block arg $1" ;;
    esac
  done

  need_num pr "$pr"
  need_num slot "$slot"
  need_num expected-epoch "$expected_epoch"
  case "$reason" in
    product|rebase) ;;
    *) die 2 "retract-operator-block permits only --reason product|rebase" ;;
  esac
  [[ "$expected_head" =~ ^[0-9a-f]{40}$ ]] \
    || die 2 "retract-operator-block requires a full 40-character --expected-head"
  [ -s "$proof" ] || die 2 "retract-operator-block requires a nonempty --proof file"

  local blocker="pm-blocked:$reason"
  python3 - "$proof" "$pr" "$expected_head" "$blocker" "$slot" "$expected_epoch" <<'PYRETRACT'
import sys
from pathlib import Path

path, pr, head, blocker, slot, epoch = sys.argv[1:]
lines = Path(path).read_text(encoding="utf-8").splitlines()
if not lines or lines[0].strip() != "PM_OPERATOR_BLOCK_RETRACTION":
    raise SystemExit("missing PM_OPERATOR_BLOCK_RETRACTION header")
fields = {}
for line in lines[1:]:
    if ":" not in line:
        continue
    key, value = line.split(":", 1)
    fields[key.strip()] = value.strip()
required = {
    "PR": pr,
    "headRefOid": head,
    "blocker": blocker,
    "slot": slot,
    "assignment_epoch": epoch,
}
for key, expected in required.items():
    if fields.get(key) != expected:
        raise SystemExit(f"proof mismatch {key}: expected={expected} got={fields.get(key)}")
if not fields.get("source_citation"):
    raise SystemExit("proof missing source_citation")
if not fields.get("reason"):
    raise SystemExit("proof missing reason")
PYRETRACT

  local pr_json state draft branch head issue labels pm_states blockers merge_state
  pr_json="$(pr_metadata_json "$pr" 2>/dev/null || true)"
  [ -n "$pr_json" ] || die 1 "cannot read PR #$pr before operator-block retraction"
  state="$(printf '%s' "$pr_json" | json_field state 2>/dev/null || true)"
  draft="$(printf '%s' "$pr_json" | json_field isDraft 2>/dev/null || true)"
  branch="$(printf '%s' "$pr_json" | json_field headRefName 2>/dev/null || true)"
  head="$(printf '%s' "$pr_json" | json_field headRefOid 2>/dev/null || true)"
  merge_state="$(printf '%s' "$pr_json" | json_field mergeStateStatus 2>/dev/null || true)"
  labels="$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print("\n".join(x.get("name", "") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
  [ "$state" = "OPEN" ] || die 13 "retract-operator-block requires an open PR #$pr"
  case "$draft" in
    true|false) ;;
    *) die 13 "retract-operator-block cannot verify draft state for PR #$pr" ;;
  esac
  [ "$head" = "$expected_head" ] \
    || die 13 "retract-operator-block head mismatch pr=$pr expected=$expected_head live=${head:-unknown}"
  [ -n "$branch" ] || die 13 "retract-operator-block cannot read PR #$pr branch"

  pm_states="$(printf '%s\n' "$labels" | grep '^pm-state:' || true)"
  blockers="$(printf '%s\n' "$labels" | grep '^pm-blocked:' || true)"
  [ "$pm_states" = "pm-state:blocked-rework" ] \
    || die 13 "retract-operator-block requires exactly pm-state:blocked-rework on PR #$pr; got=${pm_states:-none}"
  [ "$blockers" = "$blocker" ] \
    || die 13 "retract-operator-block requires exactly $blocker on PR #$pr; got=${blockers:-none}"
  if [ "$reason" = "rebase" ]; then
    [ "$draft" = "false" ] \
      || die 13 "retract-operator-block requires a ready-for-review PR for --reason rebase"
    [ "$merge_state" = "CLEAN" ] \
      || die 13 "retract-operator-block requires mergeStateStatus=CLEAN for --reason rebase; got=${merge_state:-unknown}"
  fi

  issue="$(issue_from_pr "$pr")"
  need_num issue "$issue"
  mop_slot_matches_operator_retraction_target "$slot" "$pr" "$issue" "$branch" \
    || die 13 "retract-operator-block owner mismatch slot=$slot pr=$pr issue=$issue branch=$branch"
  local live_epoch issue_json issue_state issue_labels issue_statuses
  live_epoch="$(mop_slot_epoch "$slot")" \
    || die 30 "retract-operator-block cannot read assignment_epoch for slot $slot"
  [ "$live_epoch" = "$expected_epoch" ] \
    || die 13 "retract-operator-block assignment_epoch mismatch slot=$slot expected=$expected_epoch live=$live_epoch"

  issue_json="$(gh issue view "$issue" --repo "$REPO" --json state,labels 2>/dev/null || true)"
  [ -n "$issue_json" ] || die 1 "cannot read issue #$issue before operator-block retraction"
  issue_state="$(printf '%s' "$issue_json" | json_field state 2>/dev/null || true)"
  issue_labels="$(printf '%s' "$issue_json" | python3 -c 'import json,sys; print("\n".join(x.get("name", "") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
  issue_statuses="$(printf '%s\n' "$issue_labels" | grep '^status:' || true)"
  [ "$issue_state" = "OPEN" ] || die 13 "retract-operator-block linked issue #$issue is not open"
  printf '%s\n' "$issue_labels" | grep -qx "slot:$slot" \
    || die 13 "retract-operator-block linked issue #$issue lacks slot:$slot"
  [ "$issue_statuses" = "status:in-review" ] \
    || die 13 "retract-operator-block requires exactly status:in-review on issue #$issue; got=${issue_statuses:-none}"

  # Restore the issue while the PR blocker still fails closed. Only after that
  # succeeds may the PR blocker be removed. If the final PR mutation fails,
  # compensate the issue mutation before returning failure.
  gh issue edit "$issue" --repo "$REPO" \
    --add-label "status:in-progress" \
    --remove-label "status:in-review" >/dev/null \
    || die 1 "failed to restore issue #$issue to status:in-progress"
  if ! gh pr edit "$pr" --repo "$REPO" \
    --remove-label "pm-state:blocked-rework" \
    --remove-label "$blocker" >/dev/null; then
    gh issue edit "$issue" --repo "$REPO" \
      --add-label "status:in-review" \
      --remove-label "status:in-progress" >/dev/null \
      || die 15 "failed to remove PR block and failed to restore issue #$issue to status:in-review"
    die 1 "failed to remove the mistaken operator block from PR #$pr; issue rollback succeeded"
  fi

  local final_pr final_pr_labels final_issue final_issue_labels final_statuses final_epoch
  final_pr="$(pr_metadata_json "$pr" 2>/dev/null || true)"
  final_pr_labels="$(printf '%s' "$final_pr" | python3 -c 'import json,sys; print("\n".join(x.get("name", "") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
  ! printf '%s\n' "$final_pr_labels" | grep -Eq '^(pm-state:|pm-blocked:)' \
    || die 15 "retract-operator-block PR postcondition failed pr=$pr labels=$final_pr_labels"
  final_issue="$(gh issue view "$issue" --repo "$REPO" --json state,labels 2>/dev/null || true)"
  final_issue_labels="$(printf '%s' "$final_issue" | python3 -c 'import json,sys; print("\n".join(x.get("name", "") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
  final_statuses="$(printf '%s\n' "$final_issue_labels" | grep '^status:' || true)"
  [ "$final_statuses" = "status:in-progress" ] \
    || die 15 "retract-operator-block issue postcondition failed issue=$issue statuses=${final_statuses:-none}"
  printf '%s\n' "$final_issue_labels" | grep -qx "slot:$slot" \
    || die 15 "retract-operator-block issue postcondition lost slot:$slot on issue #$issue"
  mop_slot_matches_operator_retraction_target "$slot" "$pr" "$issue" "$branch" \
    || die 15 "retract-operator-block MoP postcondition lost owner tuple slot=$slot pr=$pr issue=$issue branch=$branch"
  final_epoch="$(mop_slot_epoch "$slot")" \
    || die 30 "retract-operator-block cannot verify final assignment_epoch for slot $slot"
  [ "$final_epoch" = "$expected_epoch" ] \
    || die 15 "retract-operator-block changed assignment_epoch slot=$slot expected=$expected_epoch live=$final_epoch"

  if [ "$reason" = "product" ]; then
    resolve_pr_obligation_kinds "$pr" "$issue" "operator_block_retracted" \
      "head=$head blocker=$blocker proof=$proof slot=$slot assignment_epoch=$expected_epoch" \
      product_decision_wait blocked_rework review_loop_rescope
  else
    resolve_pr_obligation_kinds "$pr" "$issue" "operator_block_retracted" \
      "head=$head blocker=$blocker proof=$proof slot=$slot assignment_epoch=$expected_epoch merge_state=$merge_state" \
      blocked_rework review_loop_rescope
  fi
  record_event --source pm-transition --event operator_block_retracted \
    --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --slot "$slot" \
    --head-sha "$head" --payload "blocker=$blocker" --payload "proof=$proof" \
    --payload "assignment_epoch=$expected_epoch" --payload "restored_pr_state=none" \
    --payload "restored_issue_state=status:in-progress" --dedupe
  kanban_flag PM_TRANSITION \
    "operator_block_retracted pr=$pr issue=$issue slot=$slot head=$head blocker=$blocker"
  echo "PM_TRANSITION_OK command=retract-operator-block pr=$pr issue=$issue head=${head:0:10} blocker=$blocker slot=$slot assignment_epoch=$expected_epoch restored_pr_state=none restored_issue_state=status:in-progress"
}

cmd_dependency_unblocked() {
  local pr="" issue="" proof="" proof_sha=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      --issue) issue="${2:-}"; shift 2 ;;
      --proof) proof="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown dependency-unblocked arg $1" ;;
    esac
  done
  need_num pr "$pr"
  [ -s "$proof" ] || die 2 "dependency-unblocked requires a nonempty --proof file"
  if [ -z "$issue" ]; then issue="$(issue_from_pr "$pr")"; fi
  [ -n "$issue" ] && need_num issue "$issue"

  local pr_json branch base head state labels pm_states latest_json latest_head latest_base latest_labels latest_pm_states
  pr_json="$(pr_metadata_json "$pr" 2>/dev/null || true)"
  [ -n "$pr_json" ] || die 1 "cannot read PR #$pr"
  branch="$(printf '%s' "$pr_json" | json_field headRefName 2>/dev/null || true)"
  base="$(printf '%s' "$pr_json" | json_field baseRefName 2>/dev/null || true)"
  head="$(printf '%s' "$pr_json" | json_field headRefOid 2>/dev/null || true)"
  state="$(printf '%s' "$pr_json" | json_field state 2>/dev/null || true)"
  labels="$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print(",".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
  [ -n "$head" ] || die 1 "cannot read PR #$pr head"
  [ -n "$base" ] || die 1 "cannot read PR #$pr base branch"
  [ "$state" = "OPEN" ] || die 13 "dependency-unblocked state mismatch: PR #$pr is not open (state=${state:-unknown})"
  labels_include "$labels" "pm-blocked:dependency" \
    || die 13 "dependency-unblocked state mismatch: PR #$pr lacks pm-blocked:dependency"
  pm_states="$(printf '%s\n' "$labels" | tr ',' '\n' | grep '^pm-state:' || true)"
  [ "$(printf '%s\n' "$pm_states" | sed '/^$/d' | wc -l | tr -d ' ')" = "1" ] \
    || die 13 "dependency-unblocked state mismatch: PR #$pr must have exactly one pm-state label"
  printf '%s\n' "$pm_states" | grep -Eq '^pm-state:(blocked-rework|pm-review-pending|qa-passed-awaiting-ci)$' \
    || die 13 "dependency-unblocked state mismatch: PR #$pr must be blocked-rework, pm-review-pending, or qa-passed-awaiting-ci before recovery"
  proof_sha="$(shasum -a 256 "$proof" 2>/dev/null | awk '{print $1}')"
  [ -n "$proof_sha" ] || die 2 "cannot hash dependency proof: $proof"

  # Fail closed on an unproven dependency release: the proof must carry an
  # explicit merged/closed dependency evidence bound to this PR. A watching
  # marker or a missing merge_commit is not release authority.
  python3 - "$proof" "$pr" <<'PY' >/dev/null || die 13 "dependency-unblocked proof does not prove a merged/closed dependency for PR #$pr"
import json
import re
import sys
from pathlib import Path

path, pr = sys.argv[1:3]
text = Path(path).read_text(encoding="utf-8", errors="replace")
data = None
try:
    data = json.loads(text)
except Exception:
    data = None
if isinstance(data, dict):
    dependency_pr = str(data.get("dependency_pr") or "").lstrip("#")
    dependency_state = str(data.get("dependency_state") or "").upper()
    merge_commit = str(data.get("merge_commit") or "")
    if not merge_commit and data.get("dependency_refs"):
        # A watching visible-marker is NOT proof of merge; fail closed.
        raise SystemExit(1)
    if dependency_pr and dependency_state in {"MERGED", "CLOSED"} \
        and re.fullmatch(r"[0-9a-f]{40}", merge_commit):
        raise SystemExit(0)
    raise SystemExit(1)
dependency_pr = ""
dependency_state = ""
merge_commit = ""
for line in text.splitlines():
    m = re.match(r"^\s*dependency_pr\s*[:=]\s*#?(\d+)\s*$", line)
    if m:
        dependency_pr = m.group(1)
    m = re.match(r"^\s*dependency_state\s*[:=]\s*([A-Za-z]+)\s*$", line)
    if m:
        dependency_state = m.group(1).upper()
    m = re.match(r"^\s*merge_commit\s*[:=]\s*([0-9a-f]{40})\s*$", line)
    if m:
        merge_commit = m.group(1)
if dependency_pr and dependency_state in {"MERGED", "CLOSED"} and merge_commit:
    raise SystemExit(0)
raise SystemExit(1)
PY

  # CTO decision (Slack C0ALZJHGE49 thread 1786759192.277439 ts
  # 1786760957.087989): dependency release and CI admission are SEPARATE
  # control points. Verified merged-dependency consumption clears ONLY
  # pm-blocked:dependency, preserves pm-state:blocked-rework, and materializes
  # exactly one local-repro/rework obligation. It must NOT invoke the CI
  # readiness gate or fire a workflow; the slot runs the canonical local
  # repro and records the raw classification through ci-local-preflight-pass,
  # then only CTO starts the exact-head label-gated CI/E2E wave.
  latest_json="$(pr_metadata_json "$pr" 2>/dev/null || true)"
  latest_head="$(printf '%s' "$latest_json" | json_field headRefOid 2>/dev/null || true)"
  [ "$latest_head" = "$head" ] \
    || die 13 "PR #$pr head moved during dependency unblock: pinned=$head live=${latest_head:-unknown}"

  # Retarget the PR onto main when its base is still the dependency branch
  # (the dependency has merged/closed, so its branch is no longer the
  # canonical base). The dependency hold remains in place through this step
  # and the admission below; only a successful admission clears it.
  latest_base="$(printf '%s' "$latest_json" | json_field baseRefName 2>/dev/null || true)"
  if [ -n "$latest_base" ] && [ "$latest_base" != "main" ]; then
    gh pr edit "$pr" --repo "$REPO" --base main >/dev/null \
      || die 1 "failed to retarget PR #$pr onto main (base=$latest_base) after dependency unblock"
    latest_json="$(pr_metadata_json "$pr" 2>/dev/null || true)"
    latest_head="$(printf '%s' "$latest_json" | json_field headRefOid 2>/dev/null || true)"
    latest_base="$(printf '%s' "$latest_json" | json_field baseRefName 2>/dev/null || true)"
    [ "$latest_base" = "main" ] \
      || die 13 "dependency-unblocked retarget postcondition failed: PR #$pr base=$latest_base"
    [ -n "$latest_head" ] && [ "$latest_head" = "$head" ] \
      || die 13 "PR #$pr head moved during dependency-unblock retarget: pinned=$head live=${latest_head:-unknown}"
    head="$latest_head"
  fi

  resolve_pr_obligation_kinds "$pr" "$issue" "dependency_unblocked" "proof=${proof:-none} head=$head" dependency_watch dependency_unblocked dependency_wedge
  local target_args=()
  [ -n "$issue" ] && target_args+=(--issue "$issue")
  if ! printf '%s\n' "$pm_states" | grep -qx 'pm-state:qa-passed-awaiting-ci'; then
    # Materialize exactly one local-repro/rework obligation. The PR stays
    # pm-state:blocked-rework (or pm-review-pending); the next canonical step
    # is reconcile-rework-obligation to bind the durable current-head packet,
    # then assign-rework to the first healthy free slot, local repro, raw
    # classification through ci-local-preflight-pass, and a CTO-fired
    # exact-head label-gated CI/E2E wave. No sealed proof is required and no
    # CI readiness gate is invoked or workflow fired here.
    # The rework obligation is the release authority for the dependency hold:
    # it must be durably written BEFORE the hold is cleared. A failed write
    # fails closed and leaves pm-blocked:dependency intact so the unblock can
    # be retried idempotently after the writer recovers.
    if ! upsert_obligation_strict --kind rework --severity high \
        --target-type pr --target-id "$pr" --pr "$pr" \
        ${target_args[@]+"${target_args[@]}"} \
        --owner pm --horizon hourly \
        --title "PR #$pr dependency cleared; bind current-head rework packet and assign local repro" \
        --action "Run reconcile-rework-obligation to bind the durable current-head packet, resolve branch/head/epoch/handoff, then claim_slot assigns it to the first healthy free slot. The slot inspects exact failed-run logs and Modal cache, runs the canonical local repro, and pushes a descendant head; CTO then starts one exact-head wave. No sealed proof is required." \
        --blocker "dependency_unblocked_rework" \
        --evidence "head=$head" --evidence "branch=$branch" \
        --evidence "proof=$proof" --evidence "proof_sha256=$proof_sha"; then
      die 43 "dependency-unblocked rework obligation write failed for PR #$pr; pm-blocked:dependency preserved (no release authority)"
    fi
    capacity_reconcile_trigger dependency_unblocked_rework
    next_step="reconcile-rework-obligation"
    next_text="local-repro-rework"
  else
    # Compensating retry: a prior attempt already admitted the head and fired
    # the wave but failed to clear the hold. Only clear the hold; never re-run
    # admission or fire a duplicate wave.
    next_step="ci-e2e-wave"
    next_text="ci-e2e-wave"
  fi
  gh pr edit "$pr" --repo "$REPO" --remove-label "pm-blocked:dependency" >/dev/null \
    || die 1 "failed to clear pm-blocked:dependency after verified unblock on PR #$pr"
  record_event --source pm-transition --event dependency_unblocked --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --head-sha "$head" --payload "branch=$branch" --payload "proof=$proof" --payload "proof_sha256=$proof_sha" --payload "next=$next_step" --dedupe
  kanban_flag PM_TRANSITION "dependency_unblocked pr=$pr issue=${issue:-unknown} head=$head proof=$proof proof_sha256=$proof_sha next=$next_step"
  run_post_release_sweep "dependency-unblocked"
  echo "PM_TRANSITION_OK command=dependency-unblocked pr=$pr issue=${issue:-unknown} state=${pm_states##pm-state:} head=${head:0:10} proof=$proof proof_sha256=$proof_sha next=$next_text"
}

cmd_resolve_pm_gate() {
  local pr="" issue="" proof="" proof_sha="" reason=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      --issue) issue="${2:-}"; shift 2 ;;
      --proof) proof="${2:-}"; shift 2 ;;
      --reason) reason="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown resolve-pm-gate arg $1" ;;
    esac
  done
  need_num pr "$pr"
  [ -n "$reason" ] || die 2 "resolve-pm-gate requires --reason"
  [ -s "$proof" ] || die 2 "resolve-pm-gate requires a nonempty --proof file"
  if [ -z "$issue" ]; then issue="$(issue_from_pr "$pr")"; fi
  [ -n "$issue" ] && need_num issue "$issue"

  local pr_json branch head state labels pm_states pm_state target_state
  local latest_json latest_head latest_labels latest_pm_states next_action obligation_action
  local pr_gate_present=0 issue_json="" issue_state="" issue_labels="" issue_gate_summary="" issue_gate_mirrored=0
  pr_json="$(pr_metadata_json "$pr" 2>/dev/null || true)"
  [ -n "$pr_json" ] || die 1 "cannot read PR #$pr"
  branch="$(printf '%s' "$pr_json" | json_field headRefName 2>/dev/null || true)"
  head="$(printf '%s' "$pr_json" | json_field headRefOid 2>/dev/null || true)"
  state="$(printf '%s' "$pr_json" | json_field state 2>/dev/null || true)"
  labels="$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print(",".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
  [ -n "$head" ] || die 1 "cannot read PR #$pr head"
  [ "$state" = "OPEN" ] || die 13 "resolve-pm-gate state mismatch: PR #$pr is not open (state=${state:-unknown})"
  admit_receipt_style_hold resolve-pm-gate "$pr" "$issue" "$head" "$reason"
  if labels_include "$labels" "pm-blocked:pm-gate"; then
    pr_gate_present=1
  fi
  if [ -n "$issue" ]; then
    issue_json="$(gh issue view "$issue" --repo "$REPO" --json state,labels 2>/dev/null || true)"
    [ -n "$issue_json" ] || die 13 "cannot read linked issue #$issue during PM-gate resolution"
    issue_state="$(printf '%s' "$issue_json" | json_field state 2>/dev/null || true)"
    [ "$issue_state" = "OPEN" ] \
      || die 13 "resolve-pm-gate linked issue #$issue is not open (state=${issue_state:-unknown})"
    issue_labels="$(printf '%s' "$issue_json" | python3 -c 'import json,sys; print(",".join(x.get("name", "") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
    issue_gate_summary="$(printf '%s' "$issue_labels" | python3 -c '
import sys
labels = [item for item in sys.stdin.read().strip().split(",") if item]
statuses = sorted(item for item in labels if item.startswith("status:"))
blockers = sorted(item for item in labels if item.startswith("pm-blocked:"))
print("|".join(statuses) + ";" + "|".join(blockers))
')"
    case "$issue_gate_summary" in
      "status:blocked;pm-blocked:pm-gate")
        issue_gate_mirrored=1
        ;;
      *)
        if labels_include "$issue_labels" "status:blocked" \
          || labels_include "$issue_labels" "pm-blocked:pm-gate"; then
          die 13 "resolve-pm-gate linked issue mirror is inconsistent issue=$issue actual=${issue_gate_summary:-none}"
        fi
        ;;
    esac
  fi
  [ "$pr_gate_present" = "1" ] || [ "$issue_gate_mirrored" = "1" ] \
    || die 13 "resolve-pm-gate state mismatch: PR #$pr and linked issue #${issue:-unknown} lack pm-blocked:pm-gate"
  pm_states="$(printf '%s\n' "$labels" | tr ',' '\n' | grep '^pm-state:' || true)"
  [ "$(printf '%s\n' "$pm_states" | sed '/^$/d' | wc -l | tr -d ' ')" = "1" ] \
    || die 13 "resolve-pm-gate state mismatch: PR #$pr must have exactly one pm-state label"
  pm_state="$(printf '%s\n' "$pm_states" | sed '/^$/d')"
  case "$pm_state" in
    pm-state:blocked-rework)
      target_state="$pm_state"
      next_action="rework-dispatch"
      obligation_action="The PM gate is resolved and the recorded rework remains pending. Reconcile the current-head rework obligation, then assign the existing packet to the next free slot. Do not start another PM review or CI cycle."
      ;;
    pm-state:pm-review-pending)
      target_state="pm-state:pm-review-pending"
      next_action="pm-review"
      obligation_action="After the dependency/path clears, rebase/retarget this PR onto main, verify the new exact head, and directly fire one label-gated CI+E2E wave; one functionality-first review may run in parallel and gates merge, not CI start. Planner/capture/marker/preflight ceremony must not block CI start."
      ;;
    pm-state:qa-passed-awaiting-ci)
      target_state="$pm_state"
      next_action="pm-readiness-contract"
      obligation_action="Continue the project-local pm-readiness-contract on exact head $head. Do not remove or re-add pm-state:qa-passed-awaiting-ci; clearing the PM gate is not authority to trigger another CI run."
      ;;
    *)
      die 13 "resolve-pm-gate state mismatch: PR #$pr must be blocked-rework, pm-review-pending, or qa-passed-awaiting-ci"
      ;;
  esac
  proof_sha="$(shasum -a 256 "$proof" 2>/dev/null | awk '{print $1}')"
  [ -n "$proof_sha" ] || die 2 "cannot hash PM-gate proof: $proof"

  # Resolving a gate does not erase an already-recorded rework requirement.
  # blocked-rework therefore stays blocked-rework until PM dispatches its
  # existing packet. Existing review and CI-wait states are also left intact;
  # this command only removes the independent pm-gate blocker.
  latest_json="$(pr_metadata_json "$pr" 2>/dev/null || true)"
  latest_head="$(printf '%s' "$latest_json" | json_field headRefOid 2>/dev/null || true)"
  [ "$latest_head" = "$head" ] \
    || die 13 "PR #$pr head moved during PM-gate resolution: pinned=$head live=${latest_head:-unknown}"
  if [ "$pr_gate_present" = "1" ]; then
    gh pr edit "$pr" --repo "$REPO" --remove-label "pm-blocked:pm-gate" >/dev/null \
      || die 1 "failed to clear pm-blocked:pm-gate on PR #$pr"
  fi
  if [ "$issue_gate_mirrored" = "1" ]; then
    if ! gh issue edit "$issue" --repo "$REPO" \
      --remove-label "status:blocked" \
      --remove-label "pm-blocked:pm-gate" \
      --add-label "status:in-review" >/dev/null 2>&1; then
      [ "$pr_gate_present" = "1" ] \
        && gh pr edit "$pr" --repo "$REPO" --add-label "pm-blocked:pm-gate" >/dev/null 2>&1 || true
      die 1 "failed to clear linked issue PM-gate mirror for issue #$issue; PR rollback attempted"
    fi
  fi

  latest_json="$(pr_metadata_json "$pr" 2>/dev/null || true)"
  latest_head="$(printf '%s' "$latest_json" | json_field headRefOid 2>/dev/null || true)"
  latest_labels="$(printf '%s' "$latest_json" | python3 -c 'import json,sys; print(",".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
  latest_pm_states="$(printf '%s\n' "$latest_labels" | tr ',' '\n' | grep '^pm-state:' || true)"
  [ "$latest_head" = "$head" ] \
    || die 13 "PR #$pr head moved after PM-gate resolution: pinned=$head live=${latest_head:-unknown}"
  ! labels_include "$latest_labels" "pm-blocked:pm-gate" \
    || die 13 "resolve-pm-gate postcondition failed: PR #$pr still has pm-blocked:pm-gate"
  if [ "$issue_gate_mirrored" = "1" ]; then
    issue_json="$(gh issue view "$issue" --repo "$REPO" --json state,labels 2>/dev/null || true)"
    issue_state="$(printf '%s' "$issue_json" | json_field state 2>/dev/null || true)"
    issue_labels="$(printf '%s' "$issue_json" | python3 -c 'import json,sys; print(",".join(x.get("name", "") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
    if [ "$issue_state" != "OPEN" ] \
      || labels_include "$issue_labels" "status:blocked" \
      || labels_include "$issue_labels" "pm-blocked:pm-gate" \
      || ! labels_include "$issue_labels" "status:in-review"; then
      gh issue edit "$issue" --repo "$REPO" \
        --remove-label "status:in-review" \
        --add-label "status:blocked" \
        --add-label "pm-blocked:pm-gate" >/dev/null 2>&1 || true
      [ "$pr_gate_present" = "1" ] \
        && gh pr edit "$pr" --repo "$REPO" --add-label "pm-blocked:pm-gate" >/dev/null 2>&1 || true
      die 13 "resolve-pm-gate linked issue postcondition failed issue=$issue labels=${issue_labels:-none}; rollback attempted"
    fi
  fi
  [ "$(printf '%s\n' "$latest_pm_states" | sed '/^$/d' | wc -l | tr -d ' ')" = "1" ] \
    && printf '%s\n' "$latest_pm_states" | grep -qx "$target_state" \
    || die 13 "resolve-pm-gate postcondition failed: PR #$pr is not exactly $target_state"

  resolve_pr_obligation_kinds "$pr" "$issue" "pm_gate_resolved" \
    "reason=$reason proof=$proof proof_sha256=$proof_sha head=$head" pm_gate_review
  local target_args=()
  [ -n "$issue" ] && target_args+=(--issue "$issue")
  upsert_obligation --kind pm_gate_resolved_followup --severity high \
    --target-type pr --target-id "$pr" --pr "$pr" \
    ${target_args[@]+"${target_args[@]}"} \
    --owner pm --horizon hourly \
    --title "PR #$pr PM gate cleared; exact-head follow-up required" \
    --action "$obligation_action" \
    --blocker "pm_gate_resolved_followup" \
    --evidence "head=$head" --evidence "branch=$branch" \
    --evidence "reason=$reason" --evidence "proof=$proof" \
    --evidence "proof_sha256=$proof_sha" --evidence "next=$next_action"
  record_event --source pm-transition --event pm_gate_resolved --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --head-sha "$head" --payload "branch=$branch" --payload "reason=$reason" --payload "proof=$proof" --payload "proof_sha256=$proof_sha" --payload "state=$target_state" --payload "next=$next_action" --payload "pr_gate_present=$pr_gate_present" --payload "issue_gate_mirrored=$issue_gate_mirrored" --dedupe
  kanban_flag PM_TRANSITION "pm_gate_resolved pr=$pr issue=${issue:-unknown} head=$head reason=$reason proof=$proof proof_sha256=$proof_sha state=$target_state next=$next_action"
  run_post_release_sweep "resolve-pm-gate"
  priority_rework_index_invalidate "blocking_state pr=$pr issue=${issue:-unknown} reason=resolve-pm-gate"
  echo "PM_TRANSITION_OK command=resolve-pm-gate pr=$pr issue=${issue:-unknown} state=${target_state#pm-state:} head=${head:0:10} reason=$reason proof=$proof proof_sha256=$proof_sha next=$next_action pr_gate_present=$pr_gate_present issue_gate_mirrored=$issue_gate_mirrored"
}

packet_allows_capture_rearm_after_main_sync() {
  local packet="${1:-}" pr="${2:-}" head="${3:-}" main_head="${4:-}"
  [ -n "$packet" ] || return 1
  [ -f "$packet" ] || return 1
  [ -n "$pr" ] || return 1
  [ -n "$head" ] || return 1
  [ -n "$main_head" ] || return 1
  python3 - "$packet" "$pr" "$head" "$main_head" <<'PY' >/dev/null 2>&1
import json
import re
import sys
from pathlib import Path

packet_path, expected_pr, expected_head, expected_main = (
    Path(sys.argv[1]),
    int(sys.argv[2]),
    sys.argv[3],
    sys.argv[4],
)
try:
    text = packet_path.read_text(encoding="utf-8")
except Exception:
    raise SystemExit(1)

try:
    parsed = json.loads(text)
except Exception:
    parsed = {}

def field(*names):
    for name in names:
        value = parsed.get(name)
        if value is not None:
            return value
        match = re.search(
            rf"(?im)^\s*(?:[-*]\s*)?{re.escape(name)}\s*:\s*`?([^`\n]+?)`?\s*$",
            text,
        )
        if match:
            return match.group(1).strip()
    return None

required = {
    "kind": "merge-main-conflict-rework",
    "pr": expected_pr,
    "head": expected_head,
    "main": expected_main,
    "preserve": True,
    "post_sync": "capture-remote-dispatch",
}
actual = {
    "kind": str(field("kind") or ""),
    "pr": int(field("pr") or 0),
    "head": str(field("headRefOid", "head") or ""),
    "main": str(field("mainHead", "main_head") or ""),
    "preserve": str(field("preserveCaptureGate", "preserve_capture_gate") or "").lower()
    in {"true", "1", "yes"},
    "post_sync": str(
        field("postSyncCapture", "post_sync_capture") or ""
    ),
}
raise SystemExit(0 if actual == required else 1)
PY
}

packet_allows_capture_rework() {
  local packet="${1:-}" pr="${2:-}" head="${3:-}"
  [ -n "$packet" ] || return 1
  [ -f "$packet" ] || return 1
  [ -n "$pr" ] || return 1
  [ -n "$head" ] || return 1
  python3 - "$packet" "$pr" "$head" <<'PY' >/dev/null 2>&1
import json
import re
import sys
from pathlib import Path

packet_path, expected_pr, expected_head = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
try:
    text = packet_path.read_text(encoding="utf-8")
except Exception:
    raise SystemExit(1)
if not text.strip():
    raise SystemExit(1)

try:
    parsed = json.loads(text)
except Exception:
    parsed = {}

def field(name):
    value = parsed.get(name)
    if value is not None:
        return value
    match = re.search(
        rf"(?im)^\s*(?:[-*]\s*)?{re.escape(name)}\s*:\s*`?([^`\n]+?)`?\s*$",
        text,
    )
    return match.group(1).strip() if match else None

actual = {
    "kind": str(field("kind") or ""),
    "pr": int(field("pr") or 0),
    "head": str(field("headRefOid") or ""),
    "scope": str(field("scope") or ""),
    "preserve": str(field("preserveCaptureGate") or "").lower()
    in {"true", "1", "yes"},
    "post_rework": str(field("postReworkCapture") or ""),
}
required = {
    "kind": "capture-product-rework",
    "pr": expected_pr,
    "head": expected_head,
    "scope": "product-rework",
    "preserve": True,
    "post_rework": "required",
}
raise SystemExit(0 if actual == required else 1)
PY
}

record_capture_rearm_after_main_sync() {
  local pr="$1" issue="$2" slot="$3" branch="$4" head="$5" main_head="$6" packet="$7"
  local target_args=()
  [ -n "$issue" ] && target_args+=(--issue "$issue")
  [ -n "$slot" ] && target_args+=(--slot "$slot")
  upsert_obligation --kind capture_rearm_after_main_sync --severity high \
    --target-type pr --target-id "$pr" --pr "$pr" \
    ${target_args[@]+"${target_args[@]}"} \
    --owner pm --horizon hourly \
    --dedupe-group "capture_rearm_after_main_sync:${pr}:${head}:${main_head}" \
    --title "PR #$pr must re-arm capture after merging main" \
    --action "Slot $slot must merge exact main $main_head into $branch, push the conflict resolution, and return the new exact head. Keep pm-blocked:capture intact. PM must then release the completed slot through the canonical transition and run pm-transition capture-remote-dispatch --pr $pr --head <new-head>; no pre-sync capture proof or CI is valid." \
    --blocker "capture_rearm_after_main_sync" \
    --evidence "pre_sync_head=$head" --evidence "main_head=$main_head" \
    --evidence "branch=$branch" --evidence "packet=$packet"
  record_event --source pm-transition --event capture_rearm_after_main_sync_assigned \
    --target-type pr --target-id "$pr" --pr "$pr" \
    ${target_args[@]+"${target_args[@]}"} --head-sha "$head" \
    --payload "branch=$branch" --payload "main_head=$main_head" \
    --payload "packet=$packet" --dedupe
}

record_capture_rework_assignment() {
  local pr="$1" issue="$2" slot="$3" branch="$4" head="$5" assignment_epoch="$6" packet="$7"
  local target_args=()
  [ -n "$issue" ] && target_args+=(--issue "$issue")
  [ -n "$slot" ] && target_args+=(--slot "$slot")
  upsert_obligation --kind capture_rework_assignment --severity high \
    --target-type pr --target-id "$pr" --pr "$pr" \
    ${target_args[@]+"${target_args[@]}"} \
    --owner pm --horizon hourly \
    --dedupe-group "capture_rework_assignment:${pr}:${head}:${slot}:${assignment_epoch}" \
    --title "PR #$pr authorized product rework must retain its capture gate and slot" \
    --action "Keep slot $slot assigned to $branch through the authorized product rework. Keep pm-blocked:capture intact. When the slot returns an exact-head ready packet, consume it through slot-ready; then run the required remote capture on the resulting head before CI." \
    --blocker "capture_rework_assignment" \
    --evidence "assigned_head=$head" --evidence "branch=$branch" \
    --evidence "assignment_epoch=$assignment_epoch" --evidence "packet=$packet" \
    --evidence "preserve_capture_gate=true" --evidence "post_rework_capture=required"
  record_event --source pm-transition --event capture_rework_assignment_preserved \
    --target-type pr --target-id "$pr" --pr "$pr" \
    ${target_args[@]+"${target_args[@]}"} --head-sha "$head" \
    --payload "branch=$branch" --payload "assignment_epoch=$assignment_epoch" \
    --payload "packet=$packet" --dedupe
}

capture_rework_assignment_obligation_matches() {
  local pr="$1" issue="$2" slot="$3" branch="$4" assignment_epoch="$5"
  python3 - "$pr" "$issue" "$slot" "$branch" "$assignment_epoch" <<'PY'
import json
import os
import sqlite3
import sys
from pathlib import Path

pr, issue, slot, branch, epoch = sys.argv[1:6]
db_path = os.environ.get("PM_OPS_DB") or str(
    Path.home() / ".claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db"
)
try:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = con.execute(
        """
        SELECT issue, slot, evidence_json
        FROM obligations
        WHERE status='open'
          AND kind='capture_rework_assignment'
          AND blocker='capture_rework_assignment'
          AND pr=?
        ORDER BY id DESC
        """,
        (int(pr),),
    ).fetchall()
except Exception:
    raise SystemExit(2)
finally:
    try:
        con.close()
    except Exception:
        pass

if not rows:
    raise SystemExit(1)
for row_issue, row_slot, evidence_json in rows:
    try:
        evidence = json.loads(evidence_json or "{}")
        matches = (
            int(row_issue) == int(issue)
            and int(row_slot) == int(slot)
            and str(evidence.get("branch") or "") == branch
            and int(evidence.get("assignment_epoch")) == int(epoch)
            and str(evidence.get("preserve_capture_gate") or "").lower() == "true"
            and str(evidence.get("post_rework_capture") or "") == "required"
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        matches = False
    if matches:
        raise SystemExit(0)
raise SystemExit(3)
PY
}

claim_slot_compat() {
  local work_kind="$1" command_name="$2"
  shift 2
  local slot="" issue="" pr="" branch="" head_sha="" expected_epoch=""
  local repository_id="" handoff_id="" task="" value
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --slot|--issue|--pr|--branch|--head|--head-sha|--expected-epoch|--repository-id|--handoff-id|--task)
        [ "$#" -ge 2 ] || die 2 "missing value for $1"
        value="$2"
        case "$1" in
          --slot) slot="$value" ;;
          --issue) issue="$value" ;;
          --pr) pr="$value" ;;
          --branch) branch="$value" ;;
          --head|--head-sha) head_sha="$value" ;;
          --expected-epoch) expected_epoch="$value" ;;
          --repository-id) repository_id="$value" ;;
          --handoff-id) handoff_id="$value" ;;
          --task) task="$value" ;;
        esac
        shift 2
        ;;
      --work-kind)
        [ "$#" -ge 2 ] || die 2 "missing value for --work-kind"
        [ "$2" = "$work_kind" ] || die 2 "work_kind mismatch expected=$work_kind actual=$2"
        shift 2
        ;;
      --replace-existing|--replace-blocker|--replace-reason|--verification-only|--capture-rearm-after-main-sync|--capture-rework-authorized|--final-patch-authorized|--product-wait|--packet|--handoff|--handoff-file)
        die 2 "unsupported $1 for $command_name; rebind/release or handoff preparation is outside claim_slot"
        ;;
      --dry-run)
        die 2 "unsupported --dry-run for $command_name; claim_slot is an authoritative mutation"
        ;;
      -h|--help)
        usage
        return 0
        ;;
      *)
        die 2 "unknown $command_name arg $1"
        ;;
    esac
  done
  [ -n "$slot" ] || die 2 "$command_name requires --slot"
  [ -n "$branch" ] || die 2 "$command_name requires --branch"
  [ -n "$expected_epoch" ] || die 2 "$command_name requires --expected-epoch"
  [ -n "$repository_id" ] || die 2 "$command_name requires --repository-id"
  [ -n "$handoff_id" ] || die 2 "$command_name requires --handoff-id"
  [ -n "$issue" ] || [ -n "$pr" ] || die 2 "$command_name requires --issue or --pr"
  if [ -n "$pr" ] && [ -z "$head_sha" ]; then
    die 2 "$command_name requires --head-sha for a PR target"
  fi
  local python_bin="${CONTROL_PLANE_KERNEL_PYTHON:-}"
  if [ -z "$python_bin" ] || [ ! -x "$python_bin" ]; then
    python_bin="$(command -v python3 2>/dev/null || true)"
  fi
  [ -x "$python_bin" ] || die 70 "missing Python control-plane runtime"
  local -a args=(
    --state "${CONTROL_PLANE_KERNEL_ROOT}/scripts/pm/control_plane/.claim-slot-state.json"
    claim-slot
    --slot "$slot"
    --branch "$branch"
    --work-kind "$work_kind"
    --handoff-id "$handoff_id"
    --expected-epoch "$expected_epoch"
    --repository-id "$repository_id"
    --github-repository "$REPO"
    --database "$CONTROL_PLANE_KERNEL_DATABASE"
    --mop-url "$MOP_BASE"
    --quarantine-dir "$SLOT_RELEASE_QUARANTINE_DIR"
  )
  [ -n "$issue" ] && args+=(--issue "$issue")
  [ -n "$pr" ] && args+=(--pr "$pr")
  [ -n "$head_sha" ] && args+=(--head-sha "$head_sha")
  [ -n "$task" ] && args+=(--task "$task")
  "$python_bin" "$KERNEL_ASSIGNMENT_BOUNDARY" "${args[@]}"
  local rc=$?
  [ "$rc" -eq 0 ] || return "$rc"
  printf 'PM_TRANSITION_OK command=%s slot=%s issue=%s pr=%s work_kind=%s handoff_id=%s expected_epoch=%s\n' \
    "$command_name" "${slot:-none}" "${issue:-none}" "${pr:-none}" \
    "$work_kind" "$handoff_id" "$expected_epoch"
}

cmd_assign() {
  legacy_assignment_writer_disabled "assign"
  return 423
}

cmd_assign_rework() {
  legacy_assignment_writer_disabled "assign-rework"
  return 423
}

cmd_assign_repro() {
  legacy_assignment_writer_disabled "assign-repro"
  return 423
}

cmd_assign_review() {
  legacy_assignment_writer_disabled "assign-review"
  return 423
}

cmd_fabrication_reset() {
  legacy_assignment_writer_disabled "fabrication-reset"
  return 423
}
cmd_fabrication_reset_locked() {
  legacy_assignment_writer_disabled "fabrication-reset"
  return 423
}
cmd_park_issue() {
  local issue="" slot="" blocker="" reason="" expected_epoch="" pr="" head=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --issue) issue="${2:-}"; shift 2 ;;
      --pr) pr="${2:-}"; shift 2 ;;
      --slot) slot="${2:-}"; shift 2 ;;
      --blocker) blocker="${2:-}"; shift 2 ;;
      --reason) reason="${2:-}"; shift 2 ;;
      --expected-epoch) expected_epoch="${2:-}"; shift 2 ;;
      --head) head="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown park-issue arg $1" ;;
    esac
  done
  need_num issue "$issue"
  need_num slot "$slot"
  [[ "$slot" =~ ^[1-4]$ ]] || die 2 "slot must be 1..4"
  [ -n "$reason" ] || die 2 "park-issue requires --reason"
  if [ -n "$expected_epoch" ]; then need_num expected_epoch "$expected_epoch"; fi
  if [ -n "$pr" ]; then need_num pr "$pr"; fi
  if [ -n "$head" ]; then
    [[ "$head" =~ ^[0-9a-f]{40}$ ]] || die 2 "park-issue --head must be a 40-character commit SHA"
  fi

  local blocker_label
  case "$blocker" in
    product) blocker_label="pm-blocked:product" ;;
    dependency) blocker_label="pm-blocked:dependency" ;;
    infra) blocker_label="pm-blocked:infra" ;;
    pm-gate) blocker_label="pm-blocked:pm-gate" ;;
    *) die 2 "park-issue blocker must be product, dependency, infra, or pm-gate" ;;
  esac

  local issue_json state title labels branch mop_status released_slots horizon
  local pr_json live_branch live_head linked
  issue_json="$(gh issue view "$issue" --repo "$REPO" --json state,title,labels 2>/dev/null || true)"
  [ -n "$issue_json" ] || die 1 "cannot read issue #$issue"
  state="$(printf '%s' "$issue_json" | json_field state)"
  [ "$state" = "OPEN" ] || die 1 "issue #$issue is not open (state=$state)"
  title="$(printf '%s' "$issue_json" | json_field title)"
  labels="$(printf '%s' "$issue_json" | python3 -c 'import json,sys; print(",".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
  labels_include "$labels" "slot:${slot}" || die 13 "slot_not_current_issue_owner slot=$slot issue=$issue labels=${labels:-none}"

  branch="$(slot_checkout_branch "$slot")"
  if [ -n "$pr" ]; then
    # Typed PR-bound park: accept the LIVE PR-bound tuple (pr, issue, branch,
    # 40-char head) as given; never project it to issue-only pr=none/main and
    # never require a claim-rebind.  Any drift fails closed before mutation.
    pr_json="$(gh pr view "$pr" --repo "$REPO" --json state,headRefName,headRefOid 2>/dev/null || true)"
    [ -n "$pr_json" ] || die 1 "cannot read PR #$pr"
    [ "$(printf '%s' "$pr_json" | json_field state)" = "OPEN" ] || die 1 "PR #$pr is not open"
    live_branch="$(printf '%s' "$pr_json" | json_field headRefName 2>/dev/null || true)"
    live_head="$(printf '%s' "$pr_json" | json_field headRefOid 2>/dev/null || true)"
    if [ -n "$head" ] && [ "$head" != "$live_head" ]; then
      die 13 "PR-bound park head drift pr=$pr expected=$head live=${live_head:-none} issue=$issue slot=$slot; no surface changed"
    fi
    linked="$(issue_from_pr "$pr")"
    if [ -n "$linked" ] && [ "$linked" != "$issue" ]; then
      die 13 "PR-bound park issue mismatch pr=$pr issue=$issue linked_issue=$linked"
    fi
    [ -n "$live_branch" ] && branch="$live_branch"
    [ -n "$live_head" ] && head="$live_head"
  fi
  admit_receipt_style_hold park-issue "$pr" "$issue" "$head" "$reason"
  mop_slots_healthy || die 1 "MoP unavailable before parking issue #$issue from slot $slot"
  mop_status="$(mop_slot_target_status "$slot" "$pr" "$issue" "$branch")"
  [ "$mop_status" = "match" ] || die 13 "slot_not_current_mop_owner slot=$slot pr=${pr:-unknown} issue=$issue branch=${branch:-unknown} mop_status=$mop_status"

  # Release and park the checkout before mutating issue state. The shared
  # issue-owner guard pins the epoch and blocks while an agent turn is active;
  # dirty or unpushed work likewise leaves issue state untouched.  A PR-bound
  # --pr tuple is carried through as the authoritative PR-bound tuple.
  if ! released_slots="$(release_issue_owner_for_pm_transition "$issue" "$slot" "$branch" "park-issue:$blocker" "$expected_epoch" "$pr")"; then
    die 13 "park-issue owner release failed issue=$issue slot=$slot; no issue state was advanced"
  fi
  assert_no_slot_owner_for_phase "$pr" "$issue" "$branch" "park-issue:$blocker"
  printf '%s\n' "$released_slots" | tr ' ' '\n' | grep -qx "$slot" \
    || die 1 "park-issue did not release expected slot $slot for issue #$issue"

  for label in status:todo status:in-progress status:in-review status:backlog \
    pm-state:blocked-rework pm-state:pm-review-pending \
    pm-state:qa-passed-awaiting-ci pm-state:rescope-required \
    pm-blocked:product pm-blocked:dependency pm-blocked:infra pm-blocked:pm-gate; do
    gh issue edit "$issue" --repo "$REPO" --remove-label "$label" >/dev/null 2>&1 || true
  done
  gh issue edit "$issue" --repo "$REPO" --add-label "status:blocked" --add-label "$blocker_label" >/dev/null \
    || die 1 "failed to park issue #$issue with $blocker_label"

  case "$blocker" in
    product|pm-gate) horizon="daily" ;;
    *) horizon="heartbeat" ;;
  esac
  upsert_obligation --kind parked_issue_wait --severity medium --target-type issue --target-id "$issue" --issue "$issue" --owner pm --horizon "$horizon" --next-review-at "$(utc_plus_minutes 60)" --title "Parked issue #$issue: $title" --action "Re-evaluate blocker '$reason'. When cleared, write a nonempty proof file and run pm-transition unpark-issue --issue $issue --blocker $blocker --proof <proof-file> --reason <clearance-reason>. This typed transition returns the issue to status:todo and wakes capacity reconciliation. Do not edit labels or restore slot ownership directly." --blocker "$blocker" --evidence "reason=$reason" --evidence "released_slot=$slot" --evidence "branch=${branch:-unknown}"
  record_event --source pm-transition --event issue_parked --target-type issue --target-id "$issue" --issue "$issue" --slot "$slot" --payload "blocker=$blocker" --payload "reason=$reason" --payload "released_slots=$released_slots" --payload "branch=${branch:-unknown}"
  kanban_flag PM_TRANSITION "issue_parked issue=$issue blocker=$blocker released_slots=$released_slots branch=${branch:-unknown}"
  run_post_release_sweep "park-issue"
  echo "PM_TRANSITION_OK command=park-issue issue=$issue blocker=$blocker state=blocked released_slots=$released_slots branch=${branch:-unknown}"
}

cmd_unpark_issue() {
  local issue="" blocker="" proof="" reason=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --issue) issue="${2:-}"; shift 2 ;;
      --blocker) blocker="${2:-}"; shift 2 ;;
      --proof) proof="${2:-}"; shift 2 ;;
      --reason) reason="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown unpark-issue arg $1" ;;
    esac
  done
  need_num issue "$issue"
  [ -n "$reason" ] || die 2 "unpark-issue requires --reason"
  [ -f "$proof" ] && [ -s "$proof" ] || die 2 "unpark-issue requires a nonempty --proof file"

  local blocker_label
  case "$blocker" in
    product) blocker_label="pm-blocked:product" ;;
    dependency) blocker_label="pm-blocked:dependency" ;;
    infra) blocker_label="pm-blocked:infra" ;;
    pm-gate) blocker_label="pm-blocked:pm-gate" ;;
    cto) blocker_label="pm-blocked:cto" ;;
    *) die 2 "unpark-issue blocker must be product, dependency, infra, pm-gate, or cto" ;;
  esac

  local issue_json state labels label_summary proof_sha
  issue_json="$(gh issue view "$issue" --repo "$REPO" --json state,labels 2>/dev/null || true)"
  [ -n "$issue_json" ] || die 1 "cannot read issue #$issue"
  state="$(printf '%s' "$issue_json" | json_field state)"
  [ "$state" = "OPEN" ] || die 1 "issue #$issue is not open (state=$state)"
  labels="$(printf '%s' "$issue_json" | python3 -c 'import json,sys; print(",".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
  label_summary="$(printf '%s' "$labels" | python3 -c '
import sys
labels = [x for x in sys.stdin.read().strip().split(",") if x]
statuses = sorted(x for x in labels if x.startswith("status:"))
blockers = sorted(x for x in labels if x.startswith("pm-blocked:"))
print("|".join(statuses) + ";" + "|".join(blockers))
')"
  [ "$label_summary" = "status:blocked;$blocker_label" ] \
    || die 13 "unpark-issue state mismatch issue=$issue expected=status:blocked+$blocker_label actual=${label_summary:-none}"

  # Unparking only changes tracker state. It must never steal or mutate a live
  # checkout; assignment remains a separate typed transition after this passes.
  assert_no_slot_owner_for_phase "" "$issue" "" "unpark-issue:$blocker"

  # Re-read after the shared owner guard so a concurrent state transition cannot
  # be overwritten using the earlier snapshot.
  issue_json="$(gh issue view "$issue" --repo "$REPO" --json state,labels 2>/dev/null || true)"
  [ -n "$issue_json" ] || die 1 "cannot re-read issue #$issue before unpark"
  state="$(printf '%s' "$issue_json" | json_field state)"
  labels="$(printf '%s' "$issue_json" | python3 -c 'import json,sys; print(",".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
  label_summary="$(printf '%s' "$labels" | python3 -c '
import sys
labels = [x for x in sys.stdin.read().strip().split(",") if x]
statuses = sorted(x for x in labels if x.startswith("status:"))
blockers = sorted(x for x in labels if x.startswith("pm-blocked:"))
print("|".join(statuses) + ";" + "|".join(blockers))
')"
  [ "$state" = "OPEN" ] && [ "$label_summary" = "status:blocked;$blocker_label" ] \
    || die 13 "unpark-issue state changed before mutation issue=$issue actual=${label_summary:-none}"

  if ! gh issue edit "$issue" --repo "$REPO" \
    --remove-label "status:blocked" \
    --remove-label "$blocker_label" \
    --add-label "status:todo" >/dev/null 2>&1; then
    gh issue edit "$issue" --repo "$REPO" \
      --remove-label "status:todo" \
      --add-label "status:blocked" \
      --add-label "$blocker_label" >/dev/null 2>&1 || true
    die 1 "failed to unpark issue #$issue; rollback attempted"
  fi

  issue_json="$(gh issue view "$issue" --repo "$REPO" --json state,labels 2>/dev/null || true)"
  state="$(printf '%s' "$issue_json" | json_field state)"
  labels="$(printf '%s' "$issue_json" | python3 -c 'import json,sys; print(",".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
  label_summary="$(printf '%s' "$labels" | python3 -c '
import sys
labels = [x for x in sys.stdin.read().strip().split(",") if x]
statuses = sorted(x for x in labels if x.startswith("status:"))
blockers = sorted(x for x in labels if x.startswith("pm-blocked:"))
slots = sorted(x for x in labels if x.startswith("slot:"))
print("|".join(statuses) + ";" + "|".join(blockers) + ";" + "|".join(slots))
')"
  if [ "$state" != "OPEN" ] || [ "$label_summary" != "status:todo;;" ]; then
    gh issue edit "$issue" --repo "$REPO" \
      --remove-label "status:todo" \
      --add-label "status:blocked" \
      --add-label "$blocker_label" >/dev/null 2>&1 || true
    die 1 "unpark-issue postcondition failed issue=$issue actual=${label_summary:-none}; rollback attempted"
  fi

  proof_sha="$(shasum -a 256 "$proof" | awk '{print $1}')"
  resolve_target_obligations --kind parked_issue_wait --target-type issue --target-id "$issue" \
    --issue "$issue" --reason "blocker_cleared" \
    --external-state "state=status:todo blocker=$blocker proof=$proof proof_sha256=$proof_sha"
  record_event --source pm-transition --event issue_unparked --target-type issue --target-id "$issue" \
    --issue "$issue" --payload "blocker=$blocker" --payload "reason=$reason" \
    --payload "proof=$proof" --payload "proof_sha256=$proof_sha"
  kanban_flag PM_TRANSITION "issue_unparked issue=$issue blocker=$blocker proof=$proof proof_sha256=$proof_sha"
  run_post_release_sweep "unpark-issue"
  echo "PM_TRANSITION_OK command=unpark-issue issue=$issue blocker=$blocker state=todo proof=$proof proof_sha256=$proof_sha"
}

cmd_recover_unpicked_claim() {
  legacy_assignment_writer_disabled "recover-unpicked-claim"
  return 423
}


cmd_reconcile_stale_slot_owner() {
  legacy_assignment_writer_disabled "reconcile-stale-slot-owner"
  return 423
}


cmd_reconcile_closed_slot_owner() {
  legacy_assignment_writer_disabled "reconcile-closed-slot-owner"
  return 423
}


cmd_reconcile_stale_pr_owner() {
  legacy_assignment_writer_disabled "reconcile-stale-pr-owner"
  return 423
}


cmd_reconcile_stale_github_owner() {
  legacy_assignment_writer_disabled "reconcile-stale-github-owner"
  return 423
}

confirm_drain_release() {
  local slot="$1" pr="$2" issue="$3" state_label="$4" released_slots="$5"
  if printf '%s\n' "$released_slots" | tr ' ' '\n' | grep -qx "$slot"; then
    return 0
  fi
  record_event --source pm-transition --event drain_slot_release_not_confirmed --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --payload "state=${state_label:-none}" --payload "released_slots=${released_slots:-none}" --payload "action=release_required"
  upsert_obligation --kind slot_state_drift --severity high --target-type slot --target-id "$slot" --slot "$slot" --pr "$pr" --issue "$issue" --title "Slot $slot drain did not release MoP ownership" --action "Repair the MoP target tuple or checkout parking blocker, then rerun pm-transition drain-slot --slot $slot." --blocker "release_not_confirmed" --evidence "state=${state_label:-none}" --evidence "released_slots=${released_slots:-none}" >/dev/null 2>&1 || true
  echo "SLOT_DRAIN_REQUIRED slot=$slot reason=release_not_confirmed pr=${pr:-unknown} issue=${issue:-unknown} state=${state_label:-none} released_slots=${released_slots:-none} command=/Users/rajiv/.claude/scripts/pm-transition.sh drain-slot --slot $slot"
  return 11
}


cmd_drain_slot() {
  local slot="" dry_run=0 expected_epoch="" current_epoch=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --slot) slot="${2:-}"; shift 2 ;;
      --expected-epoch) expected_epoch="${2:-}"; shift 2 ;;
      --dry-run) dry_run=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown drain-slot arg $1" ;;
    esac
  done
  need_num slot "$slot"
  [[ "$slot" =~ ^[1-4]$ ]] || die 2 "slot must be 1..4"
  if [ "$dry_run" != "1" ]; then
    case "${PM_MUTATION_CLASS:-manual_single_slot_release}" in
      compat_auto_drain)
        [ "${PM_CAPACITY_COMPAT_AUTO_DRAIN:-0}" = "1" ] || die 13 "compatibility automatic drain is disabled"
        ;;
      external_wait_release)
        [ "${PM_CAPACITY_WAIT_RELEASE:-0}" = "1" ] || die 13 "external-wait release is disabled"
        ;;
      pm_direction_release)
        [ "${PM_CAPACITY_PM_DIRECTION_RELEASE:-0}" = "1" ] || die 13 "PM-direction release is disabled"
        ;;
      quiescent_release)
        [ "${PM_CAPACITY_QUIESCENT_RELEASE:-0}" = "1" ] || die 13 "quiescent release is disabled"
        ;;
      manual_single_slot_release)
        [ "${PM_CAPACITY_MANUAL_SINGLE_SLOT_RELEASE:-1}" = "1" ] || die 13 "human-directed single-slot mutation is disabled"
        ;;
      *) die 13 "unknown mutation class: ${PM_MUTATION_CLASS:-}" ;;
    esac
    need_num expected_epoch "$expected_epoch"
    current_epoch="$(mop_slot_epoch "$slot")" || die 30 "cannot read assignment_epoch for slot $slot"
    [ "$expected_epoch" = "$current_epoch" ] \
      || die 13 "assignment_epoch mismatch slot=$slot expected=$expected_epoch current=$current_epoch"
    local turn_state
    turn_state="$(mop_slot_turn_state "$slot")" || die 13 "agent-turn authority unavailable for slot $slot"
    [ "$turn_state" = "inactive" ] \
      || die 13 "pre-release predicate blocked slot=$slot active_turn_state=$turn_state"
    PM_MUTATION_EXPECTED_EPOCH="$expected_epoch"
  fi

  local capture_lease
  if capture_lease="$(active_capture_lock_details "$slot")"; then
    if [ "$dry_run" = "1" ]; then
      echo "SLOT_DRAIN_DRY_RUN slot=$slot action=keep reason=capture_local_running $capture_lease"
      return 0
    fi
    record_event --source pm-transition --event drain_slot_deferred_capture_running --target-type slot --target-id "$slot" --slot "$slot" --payload "$capture_lease" --dedupe
    resolve_target_obligations --kind slot_state_drift --target-type slot --target-id "$slot" --reason "drain_slot_deferred_capture_running" --external-state "$capture_lease" >/dev/null 2>&1 || true
    echo "PM_TRANSITION_OK command=drain-slot slot=$slot action=kept-active reason=capture_local_running $capture_lease"
    return 0
  fi

  local mop_json mop_line occupied mop_issue mop_branch mop_task mop_assigned_at checkout_branch pr_json pr issue branch head labels state_label slot_label pending_event checkout_matches_target
  mop_json="$(curl -sS -m 4 "$MOP_BASE/slots" 2>/dev/null || true)"
  [ -n "$mop_json" ] || die 30 "cannot read MoP slots"
  mop_line="$(printf '%s' "$mop_json" | python3 -c '
import json
import sys
slot = int(sys.argv[1])
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(2)
entry = next((s for s in data.get("slots", []) if int(s.get("slot", -1)) == slot), None)
def field(value):
    text = str(value or "").replace("\t", " ")[:160]
    return text if text else "__EMPTY__"
if not entry:
    print("missing\t__EMPTY__\t__EMPTY__\t__EMPTY__\t__EMPTY__\t__EMPTY__\t__EMPTY__\t__EMPTY__")
else:
	    print("%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s" % (
	        "true" if entry.get("occupied") else "false",
	        field(entry.get("repository_id")),
	        field(entry.get("issue")),
	        field(entry.get("pr")),
	        field(entry.get("branch")),
	        field(entry.get("head_sha")),
	        field(entry.get("task")),
	        field(entry.get("assigned_at")),
	    ))
	' "$slot")"
  [ -n "$mop_line" ] || die 30 "cannot parse MoP slot $slot"
  IFS="$(printf '\t')" read -r occupied mop_repository mop_issue mop_pr mop_branch mop_head mop_task mop_assigned_at <<EOF
$mop_line
EOF
  [ "${mop_repository:-}" = "__EMPTY__" ] && mop_repository=""
  [ "${mop_issue:-}" = "__EMPTY__" ] && mop_issue=""
  [ "${mop_pr:-}" = "__EMPTY__" ] && mop_pr=""
  [ "${mop_branch:-}" = "__EMPTY__" ] && mop_branch=""
  [ "${mop_head:-}" = "__EMPTY__" ] && mop_head=""
  [ "${mop_task:-}" = "__EMPTY__" ] && mop_task=""
  [ "${mop_assigned_at:-}" = "__EMPTY__" ] && mop_assigned_at=""
  if [ "$occupied" = "missing" ]; then
    die 1 "MoP slot $slot missing"
  fi
  if [ -z "$current_epoch" ]; then
    current_epoch="$(mop_slot_epoch "$slot")" || die 30 "cannot read assignment_epoch for slot $slot"
  fi

	  checkout_branch="$(slot_checkout_branch "$slot")"
	  pr_json=""
	  if [ -n "$mop_branch" ]; then
	    pr_json="$(find_pr_by_branch "$mop_branch")"
	  fi
	  if [ -z "$pr_json" ]; then
	    pr_json="$(find_pr_by_branch "$checkout_branch")"
	  fi

  if [ "$occupied" != "true" ]; then
    local free_branch_issue free_owners free_pr free_issue free_branch free_head
    free_branch_issue="$(issue_from_branch_name "$checkout_branch")"
    free_owners="$(slot_other_open_owners "$slot" "")"
    if [ -n "$pr_json" ]; then
      free_pr="$(printf '%s' "$pr_json" | json_field number)"
      free_branch="$(printf '%s' "$pr_json" | json_field headRefName)"
      free_head="$(printf '%s' "$pr_json" | json_field headRefOid)"
      free_issue="$(issue_from_pr "$free_pr")"
      [ -z "$free_issue" ] && free_issue="$free_branch_issue"
      local free_title free_labels free_state_label free_blockers free_slot_label
      free_title="$(printf '%s' "$pr_json" | json_field title)"
      free_labels="$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print(",".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))')"
      free_state_label="$(printf '%s' "$free_labels" | tr ',' '\n' | grep '^pm-state:' | head -1 || true)"
      free_blockers="$(printf '%s' "$free_labels" | tr ',' '\n' | grep '^pm-blocked:' | paste -sd, - || true)"
      free_slot_label="$(printf '%s' "$free_labels" | tr ',' '\n' | grep -x "slot:${slot}" | head -1 || true)"
      if { [ "$free_state_label" = "pm-state:blocked-rework" ] || [ -n "$free_slot_label" ]; } \
        && ! printf '%s' "$free_labels" | tr ',' '\n' | grep -Eq '^pm-blocked:(capture|dependency|product)$'; then
        if [ "$dry_run" = "1" ]; then
          echo "SLOT_DRAIN_DRY_RUN slot=$slot action=rebind-free-open-pr pr=$free_pr issue=${free_issue:-unknown} state=$free_state_label blockers=${free_blockers:-none} checkout_branch=${checkout_branch:-unknown}"
          return 0
        fi
        echo "SLOT_DRAIN_REQUIRED slot=$slot reason=rebind_requires_complete_tuple action=defer_until_rebind_slot_boundary pr=$free_pr issue=${free_issue:-unknown} branch=$free_branch head=$free_head epoch=read_at_dispatch handoff=packet-required" >&2
        return 423
      fi
      if [ -z "$free_slot_label" ]; then
        if [ "$dry_run" = "1" ]; then
          echo "SLOT_DRAIN_DRY_RUN slot=$slot action=park-free-checkout pr=$free_pr issue=${free_issue:-unknown} state=${free_state_label:-none} blockers=${free_blockers:-none} checkout_branch=${checkout_branch:-unknown} next_branch=main"
          return 0
        fi
        if park_slot_checkout_to_main "$slot" "$free_branch" "drain-slot:free-parked-pr-not-slot-owned" "$free_pr" "$free_issue"; then
          resolve_target_obligations --kind slot_state_drift --target-type slot --target-id "$slot" --reason "drain_slot_free_parked_pr_not_slot_owned" --external-state "pr=$free_pr issue=${free_issue:-unknown} state=${free_state_label:-none} branch=$free_branch parked_checkout=main" >/dev/null 2>&1 || true
          record_event --source pm-transition --event drain_slot_free_checkout_parked --target-type slot --target-id "$slot" --pr "$free_pr" --issue "$free_issue" --slot "$slot" --payload "state=${free_state_label:-none}" --payload "blockers=${free_blockers:-none}" --payload "from_branch=$free_branch" --payload "to_branch=main"
          run_post_release_sweep "drain-slot-free-checkout-parked"
          echo "PM_TRANSITION_OK command=drain-slot slot=$slot action=parked-free-checkout reason=parked_pr_not_slot_owned pr=$free_pr issue=${free_issue:-unknown} state=${free_state_label:-none} checkout_branch=${checkout_branch:-unknown} next_branch=main"
          return 0
        fi
        upsert_obligation --kind slot_state_drift --severity high --target-type slot --target-id "$slot" --slot "$slot" --pr "$free_pr" --issue "$free_issue" --title "MoP-free slot $slot checkout cannot be parked" --action "Preserve or revert the dirty/unpushed work in slot $slot, then run pm-transition drain-slot --slot $slot before dispatching fresh work." --blocker "free_slot_checkout_not_parked" --evidence "checkout_branch=${checkout_branch:-unknown}" --evidence "pr=$free_pr" >/dev/null 2>&1 || true
        echo "SLOT_DRAIN_REQUIRED slot=$slot reason=free_slot_checkout_not_parked pr=$free_pr issue=${free_issue:-unknown} checkout_branch=${checkout_branch:-unknown} command=/Users/rajiv/.claude/scripts/pm-transition.sh drain-slot --slot $slot"
        return 11
      fi
      if [ "$dry_run" != "1" ]; then
        upsert_obligation --kind slot_state_drift --severity high --target-type slot --target-id "$slot" --slot "$slot" --pr "$free_pr" --issue "$free_issue" --title "MoP-free slot $slot still has active checkout work" --action "Repair slot $slot ownership/check-out before dispatching fresh work." --blocker "free_slot_stale_checkout" --evidence "checkout_branch=${checkout_branch:-unknown}" --evidence "pr=$free_pr" >/dev/null 2>&1 || true
      fi
      echo "SLOT_DRAIN_REQUIRED slot=$slot reason=free_slot_stale_checkout pr=$free_pr issue=${free_issue:-unknown} checkout_branch=${checkout_branch:-unknown} mop_branch=${mop_branch:-unknown} command=/Users/rajiv/.claude/scripts/pm-transition.sh drain-slot --slot $slot"
      return 11
	    fi
	    if [ -n "$free_owners" ]; then
	      if [ "$dry_run" != "1" ]; then
	        upsert_obligation --kind slot_state_drift --severity high --target-type slot --target-id "$slot" --slot "$slot" --title "MoP-free slot $slot still has GitHub slot owner labels" --action "Clear or reconcile slot:$slot owner labels before dispatching fresh work." --blocker "free_slot_active_owner" --evidence "owners=$free_owners" --evidence "checkout_branch=${checkout_branch:-unknown}" >/dev/null 2>&1 || true
	      fi
	      echo "SLOT_DRAIN_REQUIRED slot=$slot reason=free_slot_active_owner owners=$free_owners checkout_branch=${checkout_branch:-unknown} command=/Users/rajiv/.claude/scripts/pm-transition.sh drain-slot --slot $slot"
	      return 11
	    fi
	    if [ -n "$free_branch_issue" ] && issue_open_active "$free_branch_issue"; then
	      if [ "$dry_run" != "1" ]; then
	        upsert_obligation --kind slot_state_drift --severity high --target-type slot --target-id "$slot" --slot "$slot" --issue "$free_branch_issue" --title "MoP-free slot $slot checkout branch belongs to active issue #$free_branch_issue" --action "Repair slot $slot checkout before dispatching fresh work." --blocker "free_slot_stale_checkout" --evidence "checkout_branch=${checkout_branch:-unknown}" >/dev/null 2>&1 || true
	      fi
	      echo "SLOT_DRAIN_REQUIRED slot=$slot reason=free_slot_stale_checkout issue=$free_branch_issue checkout_branch=${checkout_branch:-unknown} command=/Users/rajiv/.claude/scripts/pm-transition.sh drain-slot --slot $slot"
	      return 11
	    fi
	    case "$checkout_branch" in
	      ""|main|master) ;;
	      *)
	        if [ "$dry_run" = "1" ]; then
	          echo "SLOT_DRAIN_DRY_RUN slot=$slot action=park-free-checkout reason=closed_or_unowned_branch checkout_branch=$checkout_branch next_branch=main"
	          return 0
	        fi
	        if park_slot_checkout_to_main "$slot" "$checkout_branch" "drain-slot:free-closed-or-unowned-branch" "" "$free_branch_issue"; then
	          resolve_target_obligations --kind slot_state_drift --target-type slot --target-id "$slot" --reason "drain_slot_free_closed_or_unowned_branch_parked" --external-state "slot=$slot from_branch=$checkout_branch parked_checkout=main" >/dev/null 2>&1 || true
	          record_event --source pm-transition --event drain_slot_free_checkout_parked --target-type slot --target-id "$slot" --issue "$free_branch_issue" --slot "$slot" --payload "reason=closed_or_unowned_branch" --payload "from_branch=$checkout_branch" --payload "to_branch=main"
	          run_post_release_sweep "drain-slot-free-closed-or-unowned-branch"
	          echo "PM_TRANSITION_OK command=drain-slot slot=$slot action=parked-free-checkout reason=closed_or_unowned_branch checkout_branch=$checkout_branch next_branch=main"
	          return 0
	        fi
	        upsert_obligation --kind slot_state_drift --severity high --target-type slot --target-id "$slot" --slot "$slot" --issue "$free_branch_issue" --title "MoP-free slot $slot checkout cannot be parked" --action "Preserve or revert dirty or unpushed work in slot $slot, then rerun pm-transition drain-slot --slot $slot." --blocker "free_slot_checkout_not_parked" --evidence "checkout_branch=$checkout_branch" >/dev/null 2>&1 || true
	        echo "SLOT_DRAIN_REQUIRED slot=$slot reason=free_slot_checkout_not_parked issue=${free_branch_issue:-unknown} checkout_branch=$checkout_branch command=/Users/rajiv/.claude/scripts/pm-transition.sh drain-slot --slot $slot"
	        return 11
	        ;;
	    esac
	    resolve_target_obligations --kind slot_state_drift --target-type slot --target-id "$slot" --reason "drain_slot_already_free" --external-state "slot=$slot checkout_branch=${checkout_branch:-unknown}"
	    echo "PM_TRANSITION_OK command=drain-slot slot=$slot action=already-free checkout_branch=${checkout_branch:-unknown}"
	    return 0
	  fi

	  if [ -z "$pr_json" ]; then
	    local keep_issue keep_reason branch_issue mop_branch_issue slot_owner_issues slot_owner_issue
	    keep_issue=""
	    keep_reason=""
	    branch_issue="$(issue_from_branch_name "$checkout_branch")"
	    mop_branch_issue="$(issue_from_branch_name "$mop_branch")"
	    slot_owner_issues="$(slot_open_owner_issues "$slot")"
	    slot_owner_issue="$(printf '%s\n' "$slot_owner_issues" | tr ',' '\n' | head -1)"
	    if [ -n "$mop_issue" ] && issue_active_for_slot "$mop_issue" "$slot"; then
	      keep_issue="$mop_issue"
	      keep_reason="phase_bundle_no_pr"
	    elif [ -n "$branch_issue" ] && issue_active_for_slot "$branch_issue" "$slot"; then
	      keep_issue="$branch_issue"
	      keep_reason="checkout_issue_still_active"
	    elif [ -n "$mop_branch_issue" ] && issue_active_for_slot "$mop_branch_issue" "$slot"; then
	      keep_issue="$mop_branch_issue"
	      keep_reason="mop_branch_issue_still_active"
	    elif [ -n "$slot_owner_issue" ]; then
	      keep_issue="$slot_owner_issue"
	      keep_reason="slot_label_open_issue_no_pr"
	    fi
	    if [ -n "$keep_issue" ]; then
	      if [ "$dry_run" = "1" ]; then
	        echo "SLOT_DRAIN_DRY_RUN slot=$slot action=keep reason=$keep_reason issue=$keep_issue owner_issues=${slot_owner_issues:-none} mop_issue=${mop_issue:-unknown} mop_branch=${mop_branch:-unknown} checkout_branch=${checkout_branch:-unknown}"
	        return 0
	      fi
	      resolve_target_obligations --kind slot_state_drift --target-type slot --target-id "$slot" --reason "drain_slot_no_open_pr_kept_active" --external-state "issue=$keep_issue reason=$keep_reason owner_issues=${slot_owner_issues:-none} mop_issue=${mop_issue:-unknown} mop_branch=${mop_branch:-unknown} checkout_branch=${checkout_branch:-unknown}"
	      record_event --source pm-transition --event drain_slot_kept_active --target-type slot --target-id "$slot" --slot "$slot" --issue "$keep_issue" --payload "reason=$keep_reason" --payload "owner_issues=${slot_owner_issues:-none}" --payload "mop_issue=${mop_issue:-unknown}" --payload "mop_branch=${mop_branch:-unknown}" --payload "checkout_branch=${checkout_branch:-unknown}" --dedupe
	      echo "PM_TRANSITION_OK command=drain-slot slot=$slot action=kept-active reason=$keep_reason issue=$keep_issue owner_issues=${slot_owner_issues:-none} mop_issue=${mop_issue:-unknown} mop_branch=${mop_branch:-unknown} checkout_branch=${checkout_branch:-unknown}"
	      return 0
	    fi
	    if [ "$dry_run" = "1" ]; then
	      echo "SLOT_DRAIN_DRY_RUN slot=$slot action=release reason=no_open_pr mop_issue=${mop_issue:-unknown} mop_branch=${mop_branch:-unknown} checkout_branch=${checkout_branch:-unknown}"
	      return 0
	    fi
	    if ! release_slot "$slot" "drain-slot:no-open-pr" "$current_epoch" \
	      "${mop_repository:-$MOP_PRIMARY_REPOSITORY}" "${mop_issue:-}" "${mop_pr:-}" \
	      "${mop_branch:-}" "${mop_head:-}"; then
		      record_event --source pm-transition --event drain_slot_release_failed --target-type slot --target-id "$slot" --slot "$slot" --payload "reason=no_open_pr" --payload "mop_issue=${mop_issue:-unknown}" --payload "mop_branch=${mop_branch:-unknown}" --payload "checkout_branch=${checkout_branch:-unknown}" --dedupe
		      kanban_flag PM_TRANSITION "drain_slot_release_failed slot=$slot reason=no_open_pr mop_issue=${mop_issue:-unknown} mop_branch=${mop_branch:-unknown}"
		      echo "PM_TRANSITION_FAILED command=drain-slot slot=$slot action=release-failed reason=no_open_pr mop_issue=${mop_issue:-unknown} mop_branch=${mop_branch:-unknown} checkout_branch=${checkout_branch:-unknown}"
		      return 1
		    fi
	    resolve_target_obligations --kind slot_state_drift --target-type slot --target-id "$slot" --reason "drain_slot_no_open_pr" --external-state "mop_issue=${mop_issue:-unknown} mop_branch=${mop_branch:-unknown} checkout_branch=${checkout_branch:-unknown}"
	    record_event --source pm-transition --event drain_slot --target-type slot --target-id "$slot" --slot "$slot" --payload "reason=no_open_pr" --payload "mop_issue=${mop_issue:-unknown}" --payload "mop_branch=${mop_branch:-unknown}" --payload "checkout_branch=${checkout_branch:-unknown}" --dedupe
	    kanban_flag PM_TRANSITION "drain_slot slot=$slot reason=no_open_pr mop_issue=${mop_issue:-unknown} mop_branch=${mop_branch:-unknown}"
	    run_post_release_sweep "drain-slot-no-open-pr"
	    echo "PM_TRANSITION_OK command=drain-slot slot=$slot action=released reason=no_open_pr mop_issue=${mop_issue:-unknown} mop_branch=${mop_branch:-unknown} checkout_branch=${checkout_branch:-unknown}"
	    return 0
	  fi

  pr="$(printf '%s' "$pr_json" | json_field number)"
  branch="$(printf '%s' "$pr_json" | json_field headRefName)"
  head="$(printf '%s' "$pr_json" | json_field headRefOid)"
  labels="$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print(",".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))')"
  issue="$(issue_from_pr "$pr")"
  [ -z "$issue" ] && issue="$mop_issue"
	  checkout_matches_target=0
	  if slot_checkout_matches_target_ref "$slot" "$branch" "$head"; then
	    checkout_matches_target=1
	  fi
	  state_label="$(printf '%s' "$labels" | tr ',' '\n' | grep '^pm-state:' | head -1 || true)"
	  if printf '%s' "$labels" | tr ',' '\n' | grep -qx 'merge-ready'; then
	    state_label="merge-ready"
	  fi
	  slot_label="$(printf '%s' "$labels" | tr ',' '\n' | grep -x "slot:${slot}" | head -1 || true)"

	  if [ -n "$mop_branch" ] && [ -n "$checkout_branch" ] && [ "$mop_branch" != "$checkout_branch" ] \
	    && [ "$checkout_matches_target" != "1" ]; then
	    if [[ "$mop_branch" =~ (^|[-_/])pending$ ]] && [[ "$checkout_branch" =~ ^(main|master)$ ]]; then
	      pending_age="$(
	        python3 - "$mop_assigned_at" <<'PY' 2>/dev/null || echo 999999
import datetime
import sys

raw = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
if not raw:
    print(999999)
    raise SystemExit
try:
    dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    print(max(0, int((now - dt.astimezone(datetime.timezone.utc)).total_seconds())))
except Exception:
    print(999999)
PY
	      )"
	      if [ "${pending_age:-999999}" -lt 300 ]; then
	        echo "SLOT_DRAIN_DRY_RUN slot=$slot action=keep reason=new_pending_assignment_checkout_not_started age=${pending_age}s mop_issue=${mop_issue:-unknown} mop_branch=$mop_branch checkout_branch=$checkout_branch"
	        return 0
	      fi
	    fi
	    if [ "$dry_run" != "1" ]; then
	      upsert_obligation --kind slot_state_drift --severity high --target-type slot --target-id "$slot" --slot "$slot" --pr "$pr" --issue "$issue" --title "Slot $slot MoP/check-out branch drift" --action "Repair checkout/MoP assignment before releasing or dispatching more work." --blocker "branch_mismatch" --evidence "mop_branch=$mop_branch" --evidence "checkout_branch=$checkout_branch" --evidence "pr=$pr"
	    fi
	    echo "SLOT_DRAIN_REQUIRED slot=$slot reason=branch_mismatch pr=$pr issue=${issue:-unknown} mop_branch=$mop_branch checkout_branch=$checkout_branch"
	    return 11
	  fi

	  pending_event="$(pending_ready_event_for_slot_pr "$slot" "$pr" "$head")"
  local preserve_capture_ready_event=0
  if [ -n "$pending_event" ] \
    && [ "$state_label" = "pm-state:pm-review-pending" ] \
    && printf '%s' "$labels" | tr ',' '\n' | grep -qx 'pm-blocked:capture'; then
    if [ "$checkout_matches_target" != "1" ] \
      || [ "$mop_issue" != "$issue" ] \
      || [ "$mop_branch" != "$branch" ]; then
      echo "SLOT_DRAIN_REQUIRED slot=$slot reason=capture_ready_event_tuple_mismatch pr=$pr issue=${issue:-unknown} event=$pending_event mop_issue=${mop_issue:-unknown} mop_branch=${mop_branch:-unknown} pr_branch=$branch checkout_branch=${checkout_branch:-unknown}"
      return 11
    fi
    preserve_capture_ready_event=1
  fi
  if [ -n "$pending_event" ] && [ "$preserve_capture_ready_event" != "1" ]; then
    if [ "$dry_run" = "1" ]; then
      echo "SLOT_DRAIN_DRY_RUN slot=$slot action=consume-ready event=$pending_event pr=$pr issue=${issue:-unknown} state=${state_label:-none}"
      return 0
    fi
    local consume_out consume_rc
    consume_out="family2_python_boundary_required"
    consume_rc=423
    if [ "$consume_rc" = "0" ]; then
      printf '%s\n' "$consume_out"
      echo "PM_TRANSITION_OK command=drain-slot slot=$slot action=consumed-ready event=$pending_event pr=$pr issue=${issue:-unknown} state=${state_label:-none}"
      return 0
    fi
    upsert_obligation \
      --kind slot_ready_consume_failed \
      --severity high \
      --target-type pr \
      --target-id "$pr" \
      --pr "$pr" \
      --issue "$issue" \
      --slot "$slot" \
      --owner pm \
      --horizon hourly \
      --title "PR #$pr current-head ready packet could not be consumed" \
      --action "Fix the proof or transition error for $pending_event, then rerun pm-transition slot-ready --event $pending_event. Do not archive a current-head packet merely because the PR is blocked-rework or draft." \
      --blocker "slot_ready_consume_failed" \
      --evidence "event=$pending_event" \
      --evidence "state=${state_label:-none}" \
      --evidence "error=$(printf '%s' "$consume_out" | tail -1 | tr '\n' ' ' | head -c 300)" \
      >/dev/null 2>&1 || true
    record_event --source pm-transition --event slot_ready_consume_failed --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --slot "$slot" --head-sha "$head" --payload "event=$pending_event" --payload "state=${state_label:-none}" --payload "rc=$consume_rc" --payload "error=$(printf '%s' "$consume_out" | tail -1 | head -c 300)"
    echo "SLOT_DRAIN_REQUIRED slot=$slot reason=slot_ready_consume_failed pr=$pr issue=${issue:-unknown} state=${state_label:-none} event=$pending_event command=/Users/rajiv/.claude/scripts/pm-transition.sh slot-ready --event $pending_event"
    return 11
  fi

		  case "$state_label" in
		    pm-state:blocked-rework)
		      if printf '%s' "$labels" | tr ',' '\n' | grep -qx 'pm-blocked:capture'; then
		        local capture_rework_obligation_rc
		        if capture_rework_assignment_obligation_matches \
		          "$pr" "$issue" "$slot" "$branch" "$current_epoch"; then
		          capture_rework_obligation_rc=0
		        else
		          capture_rework_obligation_rc=$?
		        fi
		        if [ "$capture_rework_obligation_rc" -eq 0 ]; then
		          record_event --source pm-transition --event drain_slot_kept_active \
		            --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" \
		            --slot "$slot" --head-sha "$head" \
		            --payload "state=$state_label" --payload "blocker=pm-blocked:capture" \
		            --payload "reason=authorized_capture_rework" \
		            --payload "assignment_epoch=$current_epoch" --dedupe
		          echo "PM_TRANSITION_OK command=drain-slot slot=$slot action=kept-active state=$state_label blocker=pm-blocked:capture reason=authorized_capture_rework pr=$pr issue=${issue:-unknown} branch=$branch assignment_epoch=$current_epoch"
		          return 0
		        fi
		        if [ "$capture_rework_obligation_rc" -eq 2 ] \
		          || [ "$capture_rework_obligation_rc" -eq 3 ]; then
		          echo "SLOT_DRAIN_REQUIRED slot=$slot reason=capture_rework_authority_$([ "$capture_rework_obligation_rc" -eq 2 ] && printf unavailable || printf tuple_mismatch) pr=$pr issue=${issue:-unknown} branch=$branch assignment_epoch=$current_epoch"
		          return 11
		        fi
		        if [ "$dry_run" = "1" ]; then
		          echo "SLOT_DRAIN_DRY_RUN slot=$slot action=release state=$state_label blocker=pm-blocked:capture pr=$pr issue=${issue:-unknown} branch=$branch"
		          return 0
		        fi
		        [ -n "$issue" ] && gh issue edit "$issue" --repo "$REPO" --remove-label "status:in-progress" --add-label "status:in-review" >/dev/null 2>&1 || true
		        local released_slots
		        released_slots="$(release_target_slots "$pr" "$issue" "$branch" "drain-slot:capture-watch" "$slot")"
		        confirm_drain_release "$slot" "$pr" "$issue" "$state_label" "$released_slots" || return 11
		        record_event --source pm-transition --event drain_slot --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --head-sha "$head" --payload "state=$state_label" --payload "blocker=pm-blocked:capture" --payload "released_slots=$released_slots"
		        kanban_flag PM_TRANSITION "drain_slot slot=$slot pr=$pr issue=$issue state=$state_label blocker=pm-blocked:capture released=${released_slots:-none}"
		        run_post_release_sweep "drain-slot-capture"
		        echo "PM_TRANSITION_OK command=drain-slot slot=$slot action=released state=$state_label blocker=pm-blocked:capture pr=$pr issue=${issue:-unknown} released_slots=${released_slots:-none}"
		        return 0
		      fi
			      if [ -n "$slot_label" ] \
			        && [ "${PM_MUTATION_CLASS:-manual_single_slot_release}" != "external_wait_release" ]; then
			        resolve_target_obligations --kind slot_state_drift --target-type slot --target-id "$slot" --reason "drain_slot_kept_active" --external-state "pr=$pr issue=${issue:-unknown} state=$state_label branch=$branch"
			        echo "PM_TRANSITION_OK command=drain-slot slot=$slot action=kept-active pr=$pr issue=${issue:-unknown} state=$state_label reason=explicit-slot-label branch=$branch"
			        return 0
		      fi
      if [ "$dry_run" = "1" ]; then
        echo "SLOT_DRAIN_DRY_RUN slot=$slot action=release state=$state_label pr=$pr issue=${issue:-unknown} branch=$branch"
        return 0
      fi
      [ -n "$issue" ] && gh issue edit "$issue" --repo "$REPO" --remove-label "status:in-progress" --add-label "status:in-review" >/dev/null 2>&1 || true
      local released_slots
      local drain_reason="drain-slot:$state_label"
      if [ "${PM_MUTATION_CLASS:-manual_single_slot_release}" = "external_wait_release" ]; then
        drain_reason="drain-slot:external-wait"
      fi
      released_slots="$(release_target_slots "$pr" "$issue" "$branch" "$drain_reason" "$slot")"
      confirm_drain_release "$slot" "$pr" "$issue" "$state_label" "$released_slots" || return 11
      record_event --source pm-transition --event drain_slot --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --head-sha "$head" --payload "state=$state_label" --payload "mutation_class=${PM_MUTATION_CLASS:-manual_single_slot_release}" --payload "released_slots=$released_slots"
      kanban_flag PM_TRANSITION "drain_slot slot=$slot pr=$pr issue=$issue state=$state_label mutation_class=${PM_MUTATION_CLASS:-manual_single_slot_release} released=${released_slots:-none}"
      run_post_release_sweep "drain-slot"
      echo "PM_TRANSITION_OK command=drain-slot slot=$slot action=released state=$state_label mutation_class=${PM_MUTATION_CLASS:-manual_single_slot_release} pr=$pr issue=${issue:-unknown} released_slots=${released_slots:-none}"
      return 0
      ;;
    pm-state:qa-passed-awaiting-ci|pm-state:pm-review-pending|merge-ready|pm-state:merge-ready)
      if [ "$dry_run" = "1" ]; then
        echo "SLOT_DRAIN_DRY_RUN slot=$slot action=release state=$state_label pr=$pr issue=${issue:-unknown} branch=$branch slot_ready_event=$([ "$preserve_capture_ready_event" = "1" ] && printf preserved || printf none)"
        return 0
      fi
      [ -n "$issue" ] && gh issue edit "$issue" --repo "$REPO" --remove-label "status:in-progress" --add-label "status:in-review" >/dev/null 2>&1 || true
      local released_slots
      released_slots="$(release_target_slots "$pr" "$issue" "$branch" "drain-slot:$state_label" "$slot")"
      confirm_drain_release "$slot" "$pr" "$issue" "$state_label" "$released_slots" || return 11
      record_event --source pm-transition --event drain_slot --target-type slot --target-id "$slot" --pr "$pr" --issue "$issue" --slot "$slot" --head-sha "$head" --payload "state=$state_label" --payload "released_slots=$released_slots" --payload "slot_ready_event=$([ "$preserve_capture_ready_event" = "1" ] && printf preserved || printf none)"
      kanban_flag PM_TRANSITION "drain_slot slot=$slot pr=$pr issue=$issue state=$state_label released=${released_slots:-none}"
      run_post_release_sweep "drain-slot"
      echo "PM_TRANSITION_OK command=drain-slot slot=$slot action=released state=$state_label pr=$pr issue=${issue:-unknown} released_slots=${released_slots:-none} slot_ready_event=$([ "$preserve_capture_ready_event" = "1" ] && printf preserved || printf none)"
	      return 0
	      ;;
	  esac

	  if [ -z "$state_label" ]; then
	    if [ "$dry_run" != "1" ]; then
	      upsert_obligation \
	        --kind pr_state_missing \
	        --severity high \
	        --target-type pr \
	        --target-id "$pr" \
	        --pr "$pr" \
	        --issue "$issue" \
	        --slot "$slot" \
	        --title "PR #$pr has no PM state while holding slot $slot" \
	        --action "Decide the next transition for PR #$pr: send the slot to QA/review, mark blocked with the exact blocker, close/backlog it, or apply the correct pm-state label before draining/releasing the slot." \
	        --blocker "pr_without_pm_state" \
	        --evidence "branch=$branch" \
	        --evidence "head=${head:0:10}" >/dev/null 2>&1 || true
	    fi
	    echo "SLOT_DRAIN_REQUIRED slot=$slot reason=pr_without_pm_state pr=$pr issue=${issue:-unknown} branch=$branch command=decide_pm_state_then_drain_slot"
	    return 11
	  fi

			  resolve_target_obligations --kind slot_state_drift --target-type slot --target-id "$slot" --reason "drain_slot_kept_active" --external-state "pr=$pr issue=${issue:-unknown} state=${state_label:-none} branch=$branch"
			  echo "PM_TRANSITION_OK command=drain-slot slot=$slot action=kept-active pr=$pr issue=${issue:-unknown} state=${state_label:-none} branch=$branch"
			}


validate_rescope_contract_arg() {
  local decision="${1:-}" contract_file="${2:-}" no_scope_change="${3:-0}"
  if [ -n "$contract_file" ] && [ "$no_scope_change" = "1" ]; then
    die 42 "rescope_contract_conflicts_with_--no-scope-change"
  fi
  if [ "$decision" = "split_and_reimplement" ] && [ -z "$contract_file" ]; then
    die 42 "split_and_reimplement_requires_--rescope-contract: provide the typed JSON contract before recording the decision"
  fi
  if [ "$decision" = "override_with_evidence" ] && [ -z "$contract_file" ] && [ "$no_scope_change" != "1" ]; then
    die 42 "override_with_evidence_requires_scope_disposition: pass --rescope-contract or --no-scope-change"
  fi
  if [ -n "$contract_file" ] && [ "$decision" != "split_and_reimplement" ] && [ "$decision" != "override_with_evidence" ]; then
    die 42 "--rescope-contract is valid only for split_and_reimplement or override_with_evidence"
  fi
  if [ "$decision" = "resume" ] && [ "$no_scope_change" != "1" ]; then
    die 42 "resume_requires_--no-scope-change: record explicitly that the CTO decision preserves the existing issue and PR scope"
  fi
  if [ "$no_scope_change" = "1" ] && [ "$decision" != "override_with_evidence" ] && [ "$decision" != "resume" ]; then
    die 42 "--no-scope-change is valid only for resume or override_with_evidence"
  fi
  if [ -n "$contract_file" ] && [ ! -f "$contract_file" ]; then
    die 42 "rescope_contract_missing file=$contract_file"
  fi
}

persist_rescope_contract() {
  local issue="${1:-}" pr="${2:-}" decision="${3:-}" contract_file="${4:-}" marker="${5:-}"
  need_num issue "$issue"
  [ -n "$pr" ] && need_num pr "$pr"
  [ -f "$RESCOPE_CONTRACT_TOOL" ] || die 70 "missing rescope contract tool: $RESCOPE_CONTRACT_TOOL"
  [ -f "$contract_file" ] || die 42 "rescope_contract_missing file=$contract_file"

  local work issue_before issue_current issue_after issue_body pr_before pr_current pr_after pr_body metadata
  local render_out digest followups followup followup_json followup_state
  work="$(mktemp -d)" || die 1 "failed to create rescope contract work directory"
  issue_before="$work/issue-before.json"
  issue_after="$work/issue-after.json"
  issue_current="$work/issue-current.json"
  issue_body="$work/issue-body.md"
  pr_before="$work/pr-before.json"
  pr_after="$work/pr-after.json"
  pr_current="$work/pr-current.json"
  pr_body="$work/pr-body.md"
  metadata="$work/metadata.json"

  if ! gh issue view "$issue" --repo "$REPO" --json body,url,updatedAt > "$issue_before"; then
    rm -r "$work"
    die 1 "cannot read issue #$issue body for rescope persistence"
  fi
  if [ -n "$pr" ] && ! gh pr view "$pr" --repo "$REPO" --json body,url,updatedAt > "$pr_before"; then
    rm -r "$work"
    die 1 "cannot read PR #$pr body for rescope persistence"
  fi

  local -a render_args=(
    render
    --contract-file "$contract_file"
    --issue "$issue"
    --decision "$decision"
    --issue-json-file "$issue_before"
    --issue-body-out "$issue_body"
    --metadata-out "$metadata"
  )
  if [ -n "$pr" ]; then
    render_args+=(--pr "$pr" --pr-json-file "$pr_before" --pr-body-out "$pr_body")
  fi
  if ! render_out="$(python3 "$RESCOPE_CONTRACT_TOOL" "${render_args[@]}")"; then
    rm -r "$work"
    die 42 "rescope_contract_invalid file=$contract_file detail=${render_out:-unknown}"
  fi
  digest="$(printf '%s\n' "$render_out" | sed -n 's/.*digest=\([^ ]*\).*/\1/p')"
  followups="$(printf '%s\n' "$render_out" | sed -n 's/.*followups=\([^ ]*\).*/\1/p')"
  [ -n "$digest" ] || { rm -r "$work"; die 42 "rescope contract renderer omitted digest"; }

  # A durable issue contract can outlive its temporary transition marker. If
  # the exact contract is already recorded on the issue, recover only the
  # marker receipt instead of trying to write the contract again. This remains
  # fail-closed on the normalized digest and deliberately applies only to
  # issue-only decisions; linked PR summaries still require their normal
  # persistence path.
  if [ -z "$pr" ] \
    && python3 "$RESCOPE_CONTRACT_TOOL" verify \
      --surface issue \
      --live-json-file "$issue_before" \
      --digest "$digest" >/dev/null 2>&1; then
    if ! python3 "$RESCOPE_CONTRACT_TOOL" receipt \
      --marker-file "$marker" \
      --contract-file "$contract_file" \
      --digest "$digest"; then
      rm -r "$work"
      die 1 "failed to recover existing rescope contract receipt marker=$marker"
    fi
    rm -r "$work"
    echo "rescope_status=RECORDED rescope_contract_digest=$digest issue_contract_updated=already_recorded pr_summary_updated=not_applicable"
    return 0
  fi

  if [ -n "$followups" ] && [ "$followups" != "none" ]; then
    local old_ifs="$IFS"
    IFS=','
    for followup in $followups; do
      IFS="$old_ifs"
      followup_json="$(gh issue view "$followup" --repo "$REPO" --json state,url 2>/dev/null || true)"
      if [ -z "$followup_json" ]; then
        rm -r "$work"
        die 42 "rescope_follow_up_missing issue=$followup"
      fi
      followup_state="$(printf '%s' "$followup_json" | json_field state 2>/dev/null || true)"
      if [ "$followup_state" != "OPEN" ]; then
        rm -r "$work"
        die 42 "rescope_follow_up_not_open issue=$followup state=${followup_state:-unknown}"
      fi
      IFS=','
    done
    IFS="$old_ifs"
  fi

  if ! gh issue view "$issue" --repo "$REPO" --json body,url,updatedAt > "$issue_current" \
    || ! python3 "$RESCOPE_CONTRACT_TOOL" unchanged --surface issue --before-json-file "$issue_before" --current-json-file "$issue_current" >/dev/null; then
    rm -r "$work"
    die 42 "issue #$issue changed while preparing the rescope contract; rerun against the current body"
  fi
  if ! gh issue edit "$issue" --repo "$REPO" --body-file "$issue_body" >/dev/null; then
    rm -r "$work"
    die 1 "failed to persist rescope contract to issue #$issue"
  fi
  if ! gh issue view "$issue" --repo "$REPO" --json body,url,updatedAt > "$issue_after" \
    || ! python3 "$RESCOPE_CONTRACT_TOOL" verify --surface issue --live-json-file "$issue_after" --digest "$digest" >/dev/null; then
    rm -r "$work"
    die 1 "issue #$issue rescope contract write did not verify digest=$digest"
  fi

  if [ -n "$pr" ]; then
    if ! gh pr view "$pr" --repo "$REPO" --json body,url,updatedAt > "$pr_current" \
      || ! python3 "$RESCOPE_CONTRACT_TOOL" unchanged --surface pr --before-json-file "$pr_before" --current-json-file "$pr_current" >/dev/null; then
      rm -r "$work"
      die 42 "PR #$pr changed while preparing the rescope summary; issue #$issue is updated but rerun is safe and required"
    fi
    if ! gh pr edit "$pr" --repo "$REPO" --body-file "$pr_body" >/dev/null; then
      rm -r "$work"
      die 1 "failed to persist linked rescope summary to PR #$pr; issue #$issue is updated but transition remains incomplete"
    fi
    if ! gh pr view "$pr" --repo "$REPO" --json body,url,updatedAt > "$pr_after" \
      || ! python3 "$RESCOPE_CONTRACT_TOOL" verify --surface pr --live-json-file "$pr_after" --digest "$digest" >/dev/null; then
      rm -r "$work"
      die 1 "PR #$pr rescope summary write did not verify digest=$digest"
    fi
  fi

  local -a receipt_args=(
    receipt
    --marker-file "$marker"
    --contract-file "$contract_file"
    --digest "$digest"
  )
  [ -n "$pr" ] && receipt_args+=(--pr "$pr")
  if ! python3 "$RESCOPE_CONTRACT_TOOL" "${receipt_args[@]}"; then
    rm -r "$work"
    die 1 "failed to persist rescope contract receipt marker=$marker"
  fi
  rm -r "$work"
  echo "rescope_status=RECORDED rescope_contract_digest=$digest issue_contract_updated=yes pr_summary_updated=$([ -n "$pr" ] && printf yes || printf not_applicable)"
}

review_cap_release_owning_slot() {
  # Off-slot review-cap handoff (Rajiv thread 1786794072.170389 ts
  # 1786796329.507399): after the dispatch tuple is frozen, release the PR's
  # owning numbered slot through the existing typed/epoch-safe release
  # machinery so it is immediately refillable. The checkout is left at the
  # frozen exact head (never parked away from it) so the off-slot rescue
  # router's live-source gate still holds; the rescue runs off-slot via
  # claude -p and never depends on the slot remaining occupied. Fail closed
  # (REVIEW_CAP_RELEASE_REFUSAL set, rc=1) when the checkout is dirty or has
  # unpushed work, the MoP tuple drifts, DND is active, the turn is genuinely
  # active/productive, or any mutation cannot persist.
  local slot="$1" pr="$2" issue="$3" branch="$4" head="$5"
  local reason="review-cap:off-slot-rescue" snapshot verdict epoch safe_state checkout_path checkout_branch checkout_head
  REVIEW_CAP_RELEASE_REFUSAL=""
  [ -n "$slot" ] || return 0
  # Checkout cleanliness/unpushed/head checks run BEFORE any free-slot
  # success return (Codex CP review 2b186a49): a MoP-free slot with a dirty,
  # drifted, or unpushed checkout must still refuse dispatch instead of
  # persisting a receipt/obligation/event.
  checkout_path="$(slot_checkout_path "$slot")" || {
    REVIEW_CAP_RELEASE_REFUSAL="reason=checkout_path_unavailable slot=$slot"
    return 1
  }
  checkout_branch="$(slot_checkout_branch "$slot")"
  checkout_head="$(slot_checkout_head "$slot")"
  if [ "$checkout_branch" != "$branch" ] && [ "$checkout_branch" != "HEAD" ]; then
    REVIEW_CAP_RELEASE_REFUSAL="reason=checkout_branch_mismatch slot=$slot branch=${checkout_branch:-unknown} expected=$branch"
    return 1
  fi
  if [ "$checkout_head" != "$head" ]; then
    REVIEW_CAP_RELEASE_REFUSAL="reason=checkout_head_mismatch slot=$slot head=${checkout_head:-unknown} expected=$head"
    return 1
  fi
  if ! slot_checkout_content_clean "$slot"; then
    REVIEW_CAP_RELEASE_REFUSAL="reason=checkout_dirty slot=$slot"
    return 1
  fi
  if worktree_unpushed_evidence "$checkout_path" "$checkout_branch" "$pr"; then
    REVIEW_CAP_RELEASE_REFUSAL="reason=checkout_unpushed slot=$slot branch=${checkout_branch:-unknown}"
    return 1
  fi
  snapshot="$(curl -sS -m 4 "$MOP_BASE/slots/${slot}" 2>/dev/null || true)"
  [ -n "$snapshot" ] || {
    REVIEW_CAP_RELEASE_REFUSAL="reason=slot_unreadable slot=$slot"
    return 1
  }
  verdict="$(PM_REVIEW_CAP_SNAPSHOT="$snapshot" python3 - "$slot" "$pr" "$issue" "$branch" "$head" <<'PY'
import json
import os
import sys

slot, pr, issue, branch, head = sys.argv[1:6]
try:
    row = json.loads(os.environ["PM_REVIEW_CAP_SNAPSHOT"])
except Exception:
    print("unreadable")
    raise SystemExit(0)
if row.get("occupied") is not True:
    print("free")
    raise SystemExit(0)
if str(row.get("slot") or "") != str(slot):
    print("slot_mismatch")
    raise SystemExit(0)
live_pr = row.get("pr")
live_issue = row.get("issue")
# A PR-bound review cap requires the live MoP owner to be the exact PR, or a
# genuine issue-only claim (pr=null) for the same exact issue/branch/head
# (the typed issue-only release admission). Any other PR/owner is drift.
pr_ok = (live_pr is not None and str(live_pr) == str(pr)) or (
    live_pr in (None, "") and str(live_issue or "") == str(issue)
)
if not pr_ok:
    print("pr_mismatch")
    raise SystemExit(0)
if str(live_issue or "") != str(issue):
    print("issue_mismatch")
    raise SystemExit(0)
if str(row.get("branch") or "") != str(branch):
    print("branch_mismatch")
    raise SystemExit(0)
if str(row.get("head_sha") or "") != str(head):
    print("head_mismatch")
    raise SystemExit(0)
if row.get("dnd") is not False:
    print("dnd_active")
    raise SystemExit(0)
if str(row.get("active_turn_state") or "").lower() not in {"", "inactive"}:
    print("active_turn")
    raise SystemExit(0)
epoch = row.get("assignment_epoch")
if not isinstance(epoch, int) or epoch < 0:
    print("epoch_missing")
    raise SystemExit(0)
print("safe\t%d" % epoch)
PY
  )" || true
  case "$verdict" in
    free)
      return 0
      ;;
    safe*)
      IFS=$'\t' read -r safe_state epoch <<<"$verdict"
      [ "$safe_state" = "safe" ] && [[ "$epoch" =~ ^[0-9]+$ ]] || {
        REVIEW_CAP_RELEASE_REFUSAL="reason=unreadable_verdict slot=$slot"
        return 1
      }
      ;;
    *)
      REVIEW_CAP_RELEASE_REFUSAL="reason=${verdict:-unknown} slot=$slot"
      return 1
      ;;
  esac
  export PM_MUTATION_EXPECTED_EPOCH="$epoch"
  export RELEASE_RESERVATION_ID=""
  export RELEASE_RESERVATION_EPOCH="$epoch"
  export RELEASE_RESERVATION_PR="$pr"
  export RELEASE_RESERVATION_ISSUE="$issue"
  export RELEASE_RESERVATION_BRANCH="$branch"
  export RELEASE_RESERVATION_HEAD="$head"
  export PM_RELEASE_PREVALIDATION_MODE=review_cap_off_slot_release
  PM_RESERVATION_PREVALIDATED=1
  # GitHub projection/cleanup is a post-commit outbox effect.  The release
  # boundary itself owns the exact tuple CAS; no pre-release label mutation or
  # shell compensation may create a second effect path.
  if ! release_slot "$slot" "$reason" "$epoch" "$MOP_PRIMARY_REPOSITORY" "$issue" "$pr" "$branch" "$head"; then
    REVIEW_CAP_RELEASE_REFUSAL="reason=release_slot_refused slot=$slot epoch=$epoch"
    return 1
  fi
  return 0
}

cmd_review_cap_dispatch() {
  local pr="" issue="" head="" kind="" marker="" checkpoint="" slot="" reason="review-cap" issue_only=0
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      --issue) issue="${2:-}"; shift 2 ;;
      --head) head="${2:-}"; shift 2 ;;
      --kind) kind="${2:-}"; shift 2 ;;
      --marker) marker="${2:-}"; shift 2 ;;
      --checkpoint) checkpoint="${2:-}"; shift 2 ;;
      --slot) slot="${2:-}"; shift 2 ;;
      --reason) reason="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown review-cap-dispatch arg $1" ;;
    esac
  done

  if [ "$pr" = "none" ]; then
    issue_only=1
    [ -n "$issue" ] || die 2 "--issue is required for issue-only dispatch (pr=none)"
  else
    need_num pr "$pr"
  fi
  [ -z "$issue" ] || need_num issue "$issue"
  [ -z "$slot" ] || need_num slot "$slot"
  [[ "$head" =~ ^[0-9a-f]{40}$ ]] || die 2 "--head must be a full lowercase SHA"
  case "$kind" in
    plan|code) ;;
    *) die 2 "--kind must be plan or code" ;;
  esac
  [ -f "$marker" ] || die 2 "review cap marker not found: $marker"
  [ -n "$checkpoint" ] || die 2 "--checkpoint is required"
  [ -n "$reason" ] || die 2 "--reason is required"

  local pr_json state live_head branch live_issue owner_slot budget_json budget_decision budget_action cap_reasons
  branch=""
  if [ "$issue_only" -eq 0 ]; then
    pr_json="$(pr_metadata_json "$pr" || true)"
    [ -n "$pr_json" ] || die 1 "cannot read PR #$pr"
    state="$(printf '%s' "$pr_json" | json_field state)"
    live_head="$(printf '%s' "$pr_json" | json_field headRefOid)"
    branch="$(printf '%s' "$pr_json" | json_field headRefName)"
    [ "$state" = "OPEN" ] || die 42 "review_cap_stale_pr pr=$pr state=${state:-unknown}"
    [ "$live_head" = "$head" ] || die 42 "review_cap_stale_head pr=$pr expected=$head live=${live_head:-missing}"

    live_issue="$(issue_from_pr "$pr")"
    if [ -n "$issue" ] && [ -n "$live_issue" ] && [ "$issue" != "$live_issue" ]; then
      die 42 "review_cap_issue_mismatch pr=$pr requested=$issue live=$live_issue"
    fi
    [ -n "$issue" ] || issue="$live_issue"
    owner_slot="$(slot_from_labels "$pr" "$issue")"
    if [ -n "$slot" ] && [ -n "$owner_slot" ] && [ "$slot" != "$owner_slot" ]; then
      die 42 "review_cap_slot_mismatch pr=$pr requested=$slot owner=$owner_slot"
    fi
    [ -n "$slot" ] || slot="$owner_slot"

    local budget_args=(--pr "$pr" --head "$head" --json --live-pr)
  [ -n "$issue" ] && budget_args+=(--issue "$issue")
  budget_json="$(python3 "$REVIEW_BUDGET" "${budget_args[@]}" 2>/dev/null || true)"
  IFS=$'\t' read -r budget_decision budget_action cap_reasons budget_unreviewed budget_blocking budget_consumed <<<"$(
    printf '%s' "$budget_json" | python3 -c '
import json, sys
data = json.load(sys.stdin)

def csv_or_dash(key):
    items = [str(item) for item in (data.get(key) or []) if item]
    return ",".join(items) if items else "-"

print("\t".join((
    str(data.get("decision") or "-"),
    str(data.get("required_pm_action") or "-"),
    csv_or_dash("cap_reasons"),
    csv_or_dash("current_head_unreviewed_types"),
    csv_or_dash("current_head_blocking_types"),
    csv_or_dash("cap_consumed_by_current_head_pass"),
)))
' 2>/dev/null || true
  )"
  # Current-head re-read: the validator re-reads the live PR head AFTER the
  # budget computation. A head drifting between budget generation and dispatch
  # must fail closed, and the budget decision must be pinned to the same live
  # head it was computed for (control-plane:review-cap-current-head-preflight).
  local live_pr_json live_head2 budget_head
  live_pr_json="$(pr_metadata_json "$pr" || true)"
  [ -n "$live_pr_json" ] || die 1 "cannot re-read PR #$pr"
  live_head2="$(printf '%s' "$live_pr_json" | json_field headRefOid)"
  [ "$live_head2" = "$head" ] || die 42 "review_cap_stale_head pr=$pr expected=$head live=${live_head2:-missing}"
  budget_head="$(printf '%s' "$budget_json" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
print(str(data.get("headRefOid") or ""))
' 2>/dev/null || true)"
  if [ -n "$budget_head" ] && [ "$budget_head" != "$head" ]; then
    die 42 "review_cap_budget_head_mismatch pr=$pr expected=$head budget=${budget_head:-missing}"
  fi
  # The live head still owes exactly one bounded current-head review in the
  # dispatched lane (history reached the lifecycle cap but no completed review
  # of the current headRefOid exists in that lane's capped review_type).
  # Rescue must not dispatch yet; run that one review, then re-evaluate.
  # current_head_unreviewed_types is per-lane: a DIFFERENT capped lane with a
  # completed current-head verdict never discharges this lane's owed review.
  if [[ ",${budget_unreviewed:-}," == *",${kind},"* ]] || [ "$budget_decision" = "current_head_review_required" ]; then
    die 42 "review_cap_current_head_review_owed pr=$pr head=$head kind=$kind decision=${budget_decision:-missing} action=${budget_action:-missing}"
  fi
  [ "$budget_decision" = "rescue_required" ] || die 42 \
    "review_cap_not_current pr=$pr head=$head decision=${budget_decision:-missing} action=${budget_action:-missing}"
  # Per-review-type cap-consumption isolation (marker 607b807b): rescue may
  # dispatch ONLY for a lane with a completed blocking verdict on the live
  # head. A lane whose cap was consumed by a current-head PASS is in
  # cap_consumed_by_current_head_pass and must never be rescued, even when a
  # DIFFERENT capped lane blocks and keeps the global decision at
  # rescue_required.
  if [[ ",${budget_consumed:-}," == *",${kind},"* ]]; then
    die 42 "review_cap_kind_cap_consumed pr=$pr head=$head kind=$kind decision=${budget_decision:-missing}"
  fi
  if [[ ",${budget_blocking:-}," != *",${kind},"* ]]; then
    die 42 "review_cap_kind_not_blocking pr=$pr head=$head kind=$kind decision=${budget_decision:-missing}"
  fi
    [ "$budget_action" = "run_pr_rescue" ] || die 42 \
      "review_cap_action_invalid pr=$pr head=$head action=${budget_action:-missing}"
  else
    # Issue-only (pr=none) dispatch: there is no PR, so the PR metadata,
    # live-head, slot, and per-lane budget gates (all PR-history-shaped) do not
    # apply. The issue+head+trigger key is bound by the issue-keyed receipt and
    # by the supersede admission before any rescue launch.
    cap_reasons="issue_only_dispatch"
  fi

  local receipt result obligation_id target_args=() slot_args=() dispatch_key slot_release="not_applicable" release_refusal=""
  mkdir -p "$REVIEW_CAP_DISPATCH_DIR" || die 1 "cannot create review cap dispatch dir: $REVIEW_CAP_DISPATCH_DIR"
  chmod 700 "$REVIEW_CAP_DISPATCH_DIR" 2>/dev/null || true
  if [ "$issue_only" -eq 0 ]; then
    receipt="$REVIEW_CAP_DISPATCH_DIR/pr-${pr}-${head}-${kind}.json"
    dispatch_key="review_cap_dispatch:${pr}:${head}:${kind}"
  else
    receipt="$REVIEW_CAP_DISPATCH_DIR/issue-${issue}-${head}-${kind}.json"
    dispatch_key="review_cap_dispatch:${issue}:${head}:${kind}"
  fi
  # Atomicity (Codex CP review e2027cb4): a PR-owned dispatch persists the
  # immutable receipt/obligation/event ONLY after the owning slot release
  # succeeds. A release refusal leaves no dispatch mutation behind so the
  # exact tuple can be replayed after the blocker is resolved. A replay with
  # an existing receipt is already frozen and must not release again.
  if [ "$issue_only" -eq 0 ] && [ -n "$slot" ]; then
    if [ -f "$receipt" ]; then
      slot_release="released_or_free"
    elif review_cap_release_owning_slot "$slot" "$pr" "$issue" "$branch" "$head"; then
      slot_release="released_or_free"
    else
      slot_release="refused"
      release_refusal="${REVIEW_CAP_RELEASE_REFUSAL:-unknown}"
      echo "PM_TRANSITION_REFUSED exit=21 reason=review_cap_slot_release_refused ${release_refusal:-unknown} next=resolve_slot_release_blocker_then_replay_review_cap_dispatch" >&2
      return 21
    fi
  fi
  result="$(PR="$pr" ISSUE="${issue:-}" HEAD_SHA="$head" BRANCH="$branch" KIND="$kind" \
    MARKER="$marker" CHECKPOINT="$checkpoint" SLOT="${slot:-}" REASON="$reason" \
    CAP_REASONS="${cap_reasons:-unknown}" RECEIPT="$receipt" ISSUE_ONLY="$issue_only" python3 <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

path = Path(os.environ["RECEIPT"])
issue_only = os.environ.get("ISSUE_ONLY", "0") == "1"
expected = {
    "schema": "heydonna_review_cap_dispatch",
    "version": 1,
    "status": "dispatch_required",
    "pr": None if issue_only else int(os.environ["PR"]),
    "issue": int(os.environ["ISSUE"]) if os.environ.get("ISSUE", "").isdigit() else None,
    "headRefOid": os.environ["HEAD_SHA"],
    "branch": os.environ["BRANCH"],
    "review_type": os.environ["KIND"],
    "marker": os.environ["MARKER"],
    "checkpoint": os.environ["CHECKPOINT"],
    "slot": int(os.environ["SLOT"]) if os.environ.get("SLOT", "").isdigit() else None,
    "reason": os.environ["REASON"],
    "cap_reasons": [item for item in os.environ.get("CAP_REASONS", "").split(",") if item],
    "required_pm_action": "run_pr_rescue",
    "recommended_skill": "pm-codex-pr-rescue",
    "dedupe_key": (
        f"review_cap_dispatch:{os.environ['ISSUE']}:{os.environ['HEAD_SHA']}:{os.environ['KIND']}"
        if issue_only
        else f"review_cap_dispatch:{os.environ['PR']}:{os.environ['HEAD_SHA']}:{os.environ['KIND']}"
    ),
}
if path.exists():
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"malformed existing dispatch receipt: {exc}")
    identity_keys = (
        "schema", "version", "status", "pr", "issue", "headRefOid", "branch",
        "review_type", "slot", "required_pm_action", "recommended_skill", "dedupe_key",
    )
    if any(current.get(key) != expected.get(key) for key in identity_keys):
        raise SystemExit("existing dispatch receipt tuple differs")
    print("already_recorded")
    raise SystemExit(0)
expected["created_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
tmp.write_text(json.dumps(expected, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(path)
print("created")
PY
  )" || die 1 "failed to persist review cap dispatch receipt pr=$pr issue=${issue:-unknown}"

  [ -n "$issue" ] && target_args+=(--issue "$issue")
  [ -n "$slot" ] && slot_args+=(--slot "$slot")
  [ -x "$PM_OPS" ] || die 1 "PM ops DB writer not executable: $PM_OPS"
  local obligation_title obligation_action
  if [ "$issue_only" -eq 0 ]; then
    obligation_title="PR #$pr review cap requires immediate exact-head rescue"
    obligation_action="Immediately run Skill(pm-pr-rescue) once for PR #$pr exact head $head using marker $marker and dispatch receipt $receipt. This dispatch freezes the exact tuple and safely releases/parks the owning slot so it is refillable; do NOT hold a numbered slot for rescue. Rescue runs OFF-SLOT via the existing claude -p transport (no PM OMP session required). Consume only a validated PATCH_READY, NO_PATCH_REQUIRED, or FAILED terminal; FAILED may then enter the existing CTO split/rescope decision path. Do not start another ordinary review or rework round."
  else
    obligation_title="Issue #$issue review cap requires immediate exact-head rescue"
    obligation_action="Immediately run Skill(pm-pr-rescue) once for issue #$issue exact head $head using marker $marker and dispatch receipt $receipt. Rescue runs OFF-SLOT via the existing claude -p transport (no PM OMP session required). Consume only a validated PATCH_READY, NO_PATCH_REQUIRED, or FAILED terminal; FAILED may then enter the existing CTO split/rescope decision path. Do not start another ordinary review or rework round."
  fi
  local -a obligation_args=(
    obligation-upsert
    --kind review_loop_rescope
    --severity high
  )
  if [ "$issue_only" -eq 0 ]; then
    obligation_args+=(--target-type pr --target-id "$pr" --pr "$pr")
  else
    obligation_args+=(--target-type issue --target-id "$issue" --issue "$issue")
  fi
  obligation_args+=(
    ${target_args[@]+"${target_args[@]}"}
    ${slot_args[@]+"${slot_args[@]}"}
    --owner pm
    --horizon hourly
    --next-review-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    --dedupe-group "$dispatch_key"
    --title "$obligation_title"
    --action "$obligation_action"
    --blocker pm_codex_rescue_dispatch_required
    --evidence "head=$head"
    --evidence "marker=$marker"
    --evidence "checkpoint=$checkpoint"
    --evidence "dispatch_receipt=$receipt"
    --evidence "review_type=$kind"
    --evidence "reasons=${cap_reasons:-unknown}"
    --evidence "slot_release=$slot_release"
    --evidence "rescue_transport=claude_p_off_slot"
    --evidence pm_stop_actionable
    --print-id
  )
  obligation_id="$(python3 "$PM_OPS" "${obligation_args[@]}")" || die 1 "failed to persist review cap dispatch obligation pr=$pr issue=${issue:-unknown}"
  obligation_id="$(printf '%s\n' "$obligation_id" | tail -1)"
  [[ "$obligation_id" =~ ^[0-9]+$ ]] || die 1 \
    "review cap dispatch obligation returned invalid id: ${obligation_id:-missing}"

  if [ "$issue_only" -eq 0 ]; then
    record_event --source pm-transition --event review_cap_dispatched \
      --target-type pr --target-id "$pr" --pr "$pr" \
      ${target_args[@]+"${target_args[@]}"} \
      ${slot_args[@]+"${slot_args[@]}"} \
      --head-sha "$head" \
      --payload "kind=$kind" \
      --payload "reason=$reason" \
      --payload "receipt=$receipt" \
      --payload "checkpoint=$checkpoint" \
      --payload "slot_release=$slot_release" \
      --dedupe-key "$dispatch_key"
  else
    record_event --source pm-transition --event review_cap_dispatched \
      --target-type issue --target-id "$issue" --issue "$issue" \
      ${slot_args[@]+"${slot_args[@]}"} \
      --head-sha "$head" \
      --payload "kind=$kind" \
      --payload "reason=$reason" \
      --payload "receipt=$receipt" \
      --payload "checkpoint=$checkpoint" \
      --payload "slot_release=$slot_release" \
      --dedupe-key "$dispatch_key"
  fi
  kanban_flag PM_TRANSITION "review_cap_dispatched pr=$pr issue=${issue:-unknown} head=$head kind=$kind slot=${slot:-unknown} receipt=$receipt"
  echo "PM_TRANSITION_OK command=review-cap-dispatch pr=$pr issue=${issue:-unknown} head=$head kind=$kind slot=${slot:-unknown} dispatch=$result receipt=$receipt obligation_id=$obligation_id slot_release=$slot_release next=run_pm_pr_rescue_off_slot"
}


cmd_rescope_pr() {
  local pr="" issue="" reason="review-loop-circuit-breaker" pm_rescue_proof=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      --issue) issue="${2:-}"; shift 2 ;;
      --reason) reason="${2:-}"; shift 2 ;;
      --pm-rescue-proof) pm_rescue_proof="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown rescope-pr arg $1" ;;
    esac
  done
  need_num pr "$pr"

  local pr_json state head branch title url labels released_slots marker_line marker ledger classes artifacts current_artifacts rounds removed_blockers owner_slot
  local hold_state procedural_cap=0
  local comment_file alert_file comment_marker existing_comment_json comment_id comment_url
  pr_json="$(pr_metadata_json "$pr" || true)"
  [ -n "$pr_json" ] || die 1 "cannot read PR #$pr"
  state="$(printf '%s' "$pr_json" | json_field state)"
  [ "$state" = "OPEN" ] || die 1 "PR #$pr is not open (state=$state)"
  head="$(printf '%s' "$pr_json" | json_field headRefOid)"
  branch="$(printf '%s' "$pr_json" | json_field headRefName)"
  title="$(printf '%s' "$pr_json" | json_field title)"
  url="$(printf '%s' "$pr_json" | json_field url)"
  labels="$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print(",".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
  [ -n "$head" ] || die 1 "PR #$pr missing headRefOid"
  if [ -z "$issue" ]; then issue="$(issue_from_pr "$pr")"; fi
  [ -n "$issue" ] && need_num issue "$issue"
  require_pm_fable_rescue_failure "$pm_rescue_proof" "$reason" "$pr" "${issue:-}" "$head"
  owner_slot="$(slot_from_labels "$pr" "$issue")"

  marker_line="$(python3 - "$pr" "${issue:-}" "$head" "$branch" "$title" "$url" "$reason" "$labels" <<'PY'
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

pr, issue, head, branch, title, url, reason, labels = sys.argv[1:9]
short = head[:10] if head else "unknown"
marker = Path(f"/tmp/pm-rescope-pr-{pr}.json")
ledger = Path(f"/tmp/pm-rescope-pr-{pr}-{short}.md")
patterns = [
    f"/tmp/pm-review-loop-{pr}.json",
    f"/tmp/slot-rework-{pr}-*.md",
    f"/tmp/pm-transition-rework-{pr}-*.md",
    f"/tmp/pm-claude-code-review-{pr}-*.md",
    f"/tmp/pm-claude-plan-review-{pr}-*.md",
    f"/tmp/pm-opus-code-review-{pr}-*.md",
    f"/tmp/pm-opus-plan-review-{pr}-*.md",
    f"/tmp/codex-app-code-review-{pr}.txt",
    f"/tmp/codex-app-plan-review-{pr}.txt",
]
paths = []
for pat in patterns:
    paths.extend(glob.glob(pat))
paths = sorted({Path(p) for p in paths if Path(p).is_file()}, key=lambda p: p.stat().st_mtime)

class_terms = {
    "runtime_control_point_drift": [
        "runtime_control_point_drift", "wrong_control_point", "directive_drift",
    ],
    "proof_or_test_harness_gap": [
        "proof_or_test_harness_gap", "fixture_or_test_harness", "proof_theater",
        "false_green", "red_on_revert", "local_regression_proof_missing",
    ],
    "scope_or_migration_split": [
        "scope_or_migration_split", "split_required", "split_and_reimplement",
        "broad_scope", "close_or_reimplement",
    ],
    "stale_contract_or_plan": [
        "stale_contract_or_plan", "stale_contract", "stale_plan",
        "issue_rewrite_required",
    ],
    "ci_or_local_test_regression": [
        "ci_or_local_test_regression", "ci_regression", "local_regression",
        "e2e_regression", "wall_budget", "test_timeout",
    ],
    "dependency_or_rebase": [
        "dependency_or_rebase", "dependency_blocked", "rebase_required",
        "merge_conflict",
    ],
    "product_or_data_model_decision": [
        "product_or_data_model_decision", "product_decision", "data_model_decision",
        "escalate_product_decision",
    ],
}

decision_field = re.compile(
    r"^(?:blocker_class|root_cause_class|root_causes|cap_decision|cap_reasons|"
    r"loop_reduction_decision|required_pm_action|terminal_decision|"
    r"review_budget_decision)\s*:\s*(.+)$",
    re.I,
)

def current_head_decision_text(path, text):
    """Return explicit decision metadata only for an exact-current-head artifact."""
    name = path.name.lower()
    exact_head = head.lower()
    head_tokens = {exact_head, exact_head[:12], exact_head[:10]}
    bound = any(token and token in name for token in head_tokens)
    if not bound:
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            artifact_head = str(parsed.get("headRefOid") or parsed.get("head_sha") or "").lower()
            bound = artifact_head == exact_head
    if not bound:
        head_line = re.compile(r"^(?:headRefOid|head_sha|head)\s*:\s*([0-9a-f]{10,40})\s*$", re.I)
        for raw in text.splitlines()[:100]:
            match = head_line.match(raw.strip())
            if match and exact_head.startswith(match.group(1).lower()):
                bound = True
                break
    if not bound:
        return ""

    values = []
    for raw in text.splitlines()[:160]:
        match = decision_field.match(raw.strip())
        if match:
            values.append(match.group(1))
    normalized = "\n".join(values).lower()
    return re.sub(r"[^a-z0-9]+", "_", normalized)

def interesting_lines(text):
    out = []
    rx = re.compile(
        r"(PM_CLAUDE_REVIEW|PM_OPUS_REVIEW|VERDICT|FINAL_REVIEWER_VERDICT|"
        r"runtime_control_point|loop_reduction_decision|required_pm_action|"
        r"REQUEST_CHANGES|BLOCK|REVISE|APPROVE|Required|BLOCKER|P0|P1|P2|"
        r"split|scope|proof|red-on-revert|false-green|control point|CI|E2E)",
        re.I,
    )
    for raw in text.splitlines():
        line = " ".join(raw.strip().split())
        if not line:
            continue
        if rx.search(line):
            out.append(line[:260])
        if len(out) >= 24:
            break
    return out

classes = {name: {"count": 0, "examples": []} for name in class_terms}
artifacts = []
current_head_artifact_count = 0
runtime_control_points = set()
for path in paths:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:200000]
    except Exception:
        continue
    snippets = interesting_lines(text)
    artifacts.append({
        "path": str(path),
        "mtime": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
        "snippets": snippets[:10],
    })
    decision_text = current_head_decision_text(path, text)
    if decision_text:
        current_head_artifact_count += 1
        for raw in text.splitlines()[:160]:
            match = re.match(
                r"^\s*runtime[_ ]control[_ ]point\s*:\s*(.+?)\s*$",
                raw,
                re.I,
            )
            if match:
                value = " ".join(match.group(1).strip().split())
                if value:
                    runtime_control_points.add(value[:240])
    for name, terms in class_terms.items():
        hit_terms = [t for t in terms if t in decision_text]
        if hit_terms:
            classes[name]["count"] += 1
            if len(classes[name]["examples"]) < 5:
                example = snippets[0] if snippets else hit_terms[0]
                classes[name]["examples"].append({"artifact": str(path), "evidence": example})

active_classes = {k: v for k, v in classes.items() if v["count"] > 0}
round_count = sum(1 for p in paths if re.search(r"/(slot-rework|pm-transition-rework|pm-claude-|pm-opus-|codex-app-)", str(p)))
now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
data = {
    "schema_version": 1,
    "status": "cto_pending",
    "pr": int(pr),
    "issue": int(issue) if str(issue).isdigit() else None,
    "headRefOid": head,
    "branch": branch,
    "title": title,
    "url": url,
    "reason": reason,
    "created_at": now,
    "labels_before": [x for x in labels.split(",") if x],
    "round_count": round_count,
    "source_artifact_count": len(artifacts),
    "current_head_artifact_count": current_head_artifact_count,
    "classification_scope": "exact-current-head structured negative decision metadata",
    "evidence_classes": active_classes,
    "runtime_control_points": sorted(runtime_control_points),
    "ledger": str(ledger),
    "required_terminal_decisions": ["cto_disposition"],
    "required_pm_action": "escalate_to_cto_and_release_slot",
}

def bullet_examples(name, info):
    rows = []
    for ex in info.get("examples", [])[:4]:
        rows.append(f"  - `{ex['artifact']}`: {ex['evidence']}")
    return "\n".join(rows) if rows else "  - No concrete snippet captured; inspect source artifacts."

lines = [
    f"# CTO Rescue Escalation: PR #{pr}{f' / issue #{issue}' if issue else ''}",
    "",
    f"- Created: {now}",
    f"- Reason: `{reason}`",
    f"- Branch: `{branch}`",
    f"- Head: `{short}`",
    f"- PR: {url or f'https://github.com/heydonna-app/heydonna-app/pull/{pr}'}",
    f"- Source artifacts: {len(artifacts)}",
    f"- Exact-current-head decision artifacts classified: {current_head_artifact_count}",
    "- Classification scope: explicit negative decision metadata only; historical artifacts remain context and cannot set the decision default.",
    f"- Estimated rework/review rounds: {round_count}",
    f"- Runtime control points: {', '.join(sorted(runtime_control_points)) or 'not recorded'}",
    "",
    "## Why This PR Is Frozen",
    "",
    "This PR has crossed the review circuit breaker. The dev slot is released and no Codex rescue or further same-PR rework is permitted. CTO owns the next disposition from this exact head.",
    "",
    "## Evidence Classes From Prior Rounds",
    "",
]
if active_classes:
    for name, info in sorted(active_classes.items(), key=lambda item: (-item[1]["count"], item[0])):
        lines.append(f"### {name} ({info['count']} artifacts)")
        lines.append("")
        lines.append(bullet_examples(name, info))
        lines.append("")
else:
    lines.extend(["No class matched automatically. PM must inspect the artifacts listed below before unfreezing this PR.", ""])

lines.extend([
    "## CTO Disposition Required",
    "",
    "### CTO Decision Matrix",
    "",
    "- Choose `split_and_reimplement` when the evidence shows broad multi-surface scope, migration/auth/data-model churn, repeated same-class blockers, fabrication/reset, or test-harness/proof churn across rounds. This is the default for circuit-breaker PRs with `scope_or_migration_split` plus repeated blocker classes.",
    "- Choose `final_verified_patch` only when the remaining blocker is narrow and PM Claude can produce one verified patch packet with `git apply --check` proof against the current branch.",
    "- Choose `override_with_evidence` only when PM has read the cited code/proof and product/runtime risk is cleared; link bounded follow-ups for residue.",
    "- Choose `escalate_product_decision` only for a concrete unresolved product/data-model/authority question that PM cannot answer from Rajiv's directive, the issue/spec, or code evidence. The escalation must include the exact question and PM's recommended default.",
    "- Choose `resume` only when CTO has explicitly decided the current PR scope is correct and bounded rework may continue without rescoping. Record it with `rescope-decide --pr <N> --decision resume --no-scope-change --proof <CTO_DECISION_PROOF> --rationale <WHY_SCOPE_IS_UNCHANGED>`.",
    "",
    "CTO must choose exactly one and update this durable PR comment before any more implementation work:",
    "",
    "1. `final_verified_patch`: one PM-Claude-generated patch packet against the current branch, with `git apply --check` proof and no broad re-review loop.",
    "2. `split_and_reimplement`: close or park this PR, create child issues from the evidence classes above, and dispatch clean scoped implementation from main/current base.",
    "3. `override_with_evidence`: only if product/runtime risk is cleared; link bounded follow-ups for non-gating residue.",
    "4. `escalate_product_decision`: only for real product/data-model ambiguity that PM cannot decide.",
    "5. `resume`: continue bounded rework on this PR with no scope change, using the exact CTO proof and rationale.",
    "",
    "## Split Guidance",
    "",
    "- Split by repeated blocker class, not by the original broad AC list.",
    "- Each child issue must name the intended capability, production failure mode, runtime control point, and one narrow proof command.",
    "- The old PR must not consume a dev slot while CTO rescue is pending.",
    "",
    "## Source Artifacts",
    "",
])
for art in artifacts:
    lines.append(f"- `{art['path']}` ({art['mtime']})")
ledger.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
tmp = marker.with_name(f"{marker.name}.{os.getpid()}.tmp")
tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(marker)
print(f"marker={marker} ledger={ledger} classes={','.join(sorted(active_classes)) or 'none'} artifacts={len(artifacts)} current_artifacts={current_head_artifact_count} rounds={round_count}")
PY
)" || die 1 "failed to build rescope packet for PR #$pr"

  marker="$(printf '%s\n' "$marker_line" | sed -n 's/.*marker=\([^ ]*\).*/\1/p')"
  ledger="$(printf '%s\n' "$marker_line" | sed -n 's/.*ledger=\([^ ]*\).*/\1/p')"
  classes="$(printf '%s\n' "$marker_line" | sed -n 's/.*classes=\([^ ]*\).*/\1/p')"
  artifacts="$(printf '%s\n' "$marker_line" | sed -n 's/.*artifacts=\([0-9][0-9]*\).*/\1/p')"
  current_artifacts="$(printf '%s\n' "$marker_line" | sed -n 's/.*current_artifacts=\([0-9][0-9]*\).*/\1/p')"
  rounds="$(printf '%s\n' "$marker_line" | sed -n 's/.*rounds=\([0-9][0-9]*\).*/\1/p')"
  [ -n "$marker" ] || die 1 "rescope marker path missing"
  [ -n "$ledger" ] || die 1 "rescope ledger path missing"
  hold_state="$(cto_review_hold_state "$reason" "${classes:-none}" "${current_artifacts:-0}")"
  [ "$hold_state" = "pm-review-pending" ] && procedural_cap=1

  mop_slots_healthy || die 1 "MoP unavailable before rescope slot release for PR #$pr"
  if ! released_slots="$(release_target_slots "$pr" "$issue" "$branch" "rescope-pr" "$owner_slot")"; then
    die 15 "rescope_slot_release_reconcile_failed pr=$pr issue=${issue:-unknown} branch=$branch action=repair_typed_slot_ownership_before_state_advance"
  fi
  assert_no_slot_owner_for_phase "$pr" "$issue" "$branch" "rescope-pr"

  bash "$PM_STATE" "$pr" "$hold_state" || die 1 "failed to move PR #$pr to $hold_state"
  removed_blockers="$(remove_pm_blockers "$pr")"
  gh pr edit "$pr" --repo "$REPO" --add-label "pm-blocked:cto" >/dev/null || die 1 "failed to add pm-blocked:cto to PR #$pr"
  clear_other_slot_labels pr "$pr" ""
  if [ -n "$issue" ]; then
    clear_other_slot_labels issue "$issue" ""
    gh issue edit "$issue" --repo "$REPO" --remove-label "status:todo" >/dev/null 2>&1 || true
    gh issue edit "$issue" --repo "$REPO" --remove-label "status:in-progress" >/dev/null 2>&1 || true
    gh issue edit "$issue" --repo "$REPO" --add-label "status:in-review" >/dev/null 2>&1 || true
  fi

  comment_marker="<!-- heydonna-cto-rescue pr=${pr} head=${head} -->"
  comment_file="/tmp/pm-cto-rescue-pr-${pr}-${head:0:12}.md"
  alert_file="/tmp/pm-cto-rescue-alert-pr-${pr}-${head:0:12}.md"
  {
    printf '%s\n\n' "$comment_marker"
    printf '> **CTO RESCUE REQUIRED** — review cap reached at exact head `%s`. The dev slot has been released and `pm-blocked:cto` prevents further slot work. Continue from the evidence below; do not restart the review loop.\n\n' "$head"
    cat "$ledger"
  } > "$comment_file"
  {
    printf '*CTO RESCUE REQUIRED* — PR #%s / issue #%s\n' "$pr" "${issue:-unknown}"
    printf -- '- Exact head: `%s` on `%s`\n' "$head" "$branch"
    printf -- '- Slot ownership released; `pm-blocked:cto` prevents further slot work.\n'
    printf -- '- Current-head evidence classes: `%s`\n' "${classes:-none}"
    printf -- '- Exact-current-head classified artifacts: %s\n' "${current_artifacts:-0}"
    printf -- '- Historical artifacts retained for context: %s; estimated rounds: %s. They do not set the disposition default.\n' "${artifacts:-0}" "${rounds:-0}"
    printf -- '- Durable continuation: PR #%s comment; marker `%s`; ledger `%s`.\n' "$pr" "$marker" "$ledger"
    if [ "$procedural_cap" = "1" ]; then
      printf -- '- Hold classification: procedural review cap with no exact-current-head defect class; state remains `pm-review-pending` under `pm-blocked:cto`, not product rescope.\n'
      printf -- '- CTO action: perform one exact-head review and use `override_with_evidence --no-scope-change` if runtime risk is cleared; record a product rescope only if that review finds a real defect.\n'
    else
      printf -- '- CTO action: choose one exact-head terminal disposition; do not restart ordinary review or Codex rescue.\n'
    fi
  } > "$alert_file"
  existing_comment_json="$(gh api "repos/${REPO}/issues/${pr}/comments" --paginate --jq ".[] | select(.body | contains(\"$comment_marker\")) | {id: .id, url: .html_url}" 2>/dev/null | head -1 || true)"
  if [ -z "$existing_comment_json" ]; then
    gh pr comment "$pr" --repo "$REPO" --body-file "$comment_file" >/dev/null \
      || die 1 "failed to post durable CTO rescue handoff comment to PR #$pr"
    existing_comment_json="$(gh api "repos/${REPO}/issues/${pr}/comments" --paginate --jq ".[] | select(.body | contains(\"$comment_marker\")) | {id: .id, url: .html_url}" 2>/dev/null | head -1 || true)"
  fi
  comment_id="$(printf '%s' "$existing_comment_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id", ""))' 2>/dev/null || true)"
  comment_url="$(printf '%s' "$existing_comment_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("url", ""))' 2>/dev/null || true)"
  [ -n "$comment_id" ] && [ -n "$comment_url" ] \
    || die 1 "durable CTO rescue handoff comment receipt missing for PR #$pr"
  python3 - "$marker" "$comment_id" "$comment_url" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
data["pr_comment_id"] = int(sys.argv[2])
data["pr_comment_url"] = sys.argv[3]
tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(path)
PY

  local target_args=()
  [ -n "$issue" ] && target_args+=(--issue "$issue")
  resolve_pr_obligation_kinds "$pr" "$issue" "cto_rescue_supersedes_review" \
    "head=$head marker=$marker state=$hold_state" pm_review_pending
  upsert_obligation --kind cto_rescue --severity high --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --owner cto --horizon hourly --next-review-at "$(utc_plus_minutes 30)" --title "PR #$pr requires CTO rescue" --action "Read the exact-head PR handoff comment and record one CTO disposition: resume with a new exact-head assignment epoch, split/reimplement, override with evidence, or close. Do not run Codex rescue or reassign while pm-blocked:cto remains." --blocker "cto_rescue_required" --evidence "marker=$marker comment_url=$comment_url classes=${classes:-none} artifacts=${artifacts:-0} rounds=${rounds:-0}"
  record_event --source pm-transition --event cto_rescue_escalated --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --head-sha "$head" --payload "reason=$reason" --payload "marker=$marker" --payload "ledger=$ledger" --payload "comment_url=$comment_url" --payload "classes=${classes:-none}" --payload "artifacts=${artifacts:-0}" --payload "current_artifacts=${current_artifacts:-0}" --payload "hold_state=$hold_state" --payload "rounds=${rounds:-0}" --payload "released_slots=${released_slots:-none}" --payload "removed_blockers=${removed_blockers:-none}"
  kanban_flag CTO_RESCUE "pr=$pr issue=${issue:-unknown} released=${released_slots:-none} marker=$marker"
  transition_alert --event decision-required --pr "$pr" --issue "${issue:-}" --state cto_review --head "$head" --branch "$branch" --reason "review-cap:${reason}" --proof "$marker" --message-file "$alert_file"
  [ -n "$released_slots" ] && run_post_release_sweep "cto-rescue-pr"
	  echo "PM_TRANSITION_OK command=cto-rescue-pr pr=$pr issue=${issue:-unknown} state=$hold_state blocker=pm-blocked:cto released_slots=${released_slots:-none} marker=$marker comment_url=$comment_url classes=${classes:-none} artifacts=${artifacts:-0} current_artifacts=${current_artifacts:-0} rounds=${rounds:-0}"
	}


# Project one already-accepted normal CTO implementation rescue onto an OPEN,
# exact-head, slot-free PR without invoking the review-cap/rescope transition.
# The acceptance receipt is authority; this command only performs the
# complete-set GitHub projection. GitHub does not expose an If-Match header
# for issue labels, so the writer uses exact pre-read + complete replacement +
# exact post-read, with verified rollback to the pre-state on any mismatch.
cmd_offslot_rescue_start() {
  local repository_id="" pr="" head_sha="" expected_labels_json="" rescue_kind="" rescue_receipt=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --repository-id) repository_id="${2:-}"; shift 2 ;;
      --pr) pr="${2:-}"; shift 2 ;;
      --head-sha) head_sha="${2:-}"; shift 2 ;;
      --expected-labels-json) expected_labels_json="${2:-}"; shift 2 ;;
      --rescue-kind) rescue_kind="${2:-}"; shift 2 ;;
      --rescue-receipt) rescue_receipt="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown offslot-rescue-start arg $1" ;;
    esac
  done

  [ "$repository_id" = "$REPO" ] \
    || die 2 "offslot-rescue-start repository mismatch expected=$REPO got=${repository_id:-none}"
  need_num pr "$pr"
  [[ "$head_sha" =~ ^[0-9a-f]{40}$ ]] \
    || die 2 "offslot-rescue-start --head-sha must be a 40-character lowercase SHA"
  [ "$rescue_kind" = "implementation/off_slot" ] \
    || die 2 "offslot-rescue-start --rescue-kind must be implementation/off_slot"
  [ -f "$rescue_receipt" ] && [ ! -L "$rescue_receipt" ] \
    || die 42 "offslot-rescue-start requires a regular non-symlink accepted rescue receipt"

  local receipt_out receipt_owner receipt_sha pr_json live_state live_draft live_head live_branch
  local live_labels_json desired_labels_json post_json post_head post_labels_json rollback_json owner_slots status
  local mop_slots_json
  receipt_out="$(python3 - "$rescue_receipt" "$repository_id" "$pr" "$head_sha" "$rescue_kind" <<'PYOFFSLOTRECEIPT'
import hashlib
import json
import sys
from pathlib import Path

path, repository, pr, head, rescue_kind = sys.argv[1:]
try:
    raw = Path(path).read_bytes()
    value = json.loads(raw)
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"invalid_receipt:{exc}")
expected = {
    "schema": "heydonna_offslot_rescue_acceptance",
    "version": 1,
    "status": "accepted",
    "repository_id": repository,
    "pr": int(pr),
    "head_sha": head,
    "rescue_kind": rescue_kind,
}
for key, wanted in expected.items():
    if value.get(key) != wanted:
        raise SystemExit(f"receipt_{key}_mismatch")
owner = value.get("owner")
if not isinstance(owner, str) or not owner.strip() or any(ch.isspace() for ch in owner):
    raise SystemExit("receipt_owner_invalid")
print(owner.strip())
print(hashlib.sha256(raw).hexdigest())
PYOFFSLOTRECEIPT
)" || die 42 "offslot-rescue-start accepted rescue receipt invalid: ${receipt_out:-unknown}"
  receipt_owner="${receipt_out%%$'\n'*}"
  receipt_sha="${receipt_out##*$'\n'}"

  expected_labels_json="$(python3 - "$expected_labels_json" <<'PYOFFSLOTLABELS'
import json
import sys
try:
    labels = json.loads(sys.argv[1])
except json.JSONDecodeError as exc:
    raise SystemExit(f"invalid_json:{exc}")
if not isinstance(labels, list) or any(not isinstance(x, str) or not x for x in labels):
    raise SystemExit("labels_must_be_nonempty_string_array")
if len(labels) != len(set(labels)):
    raise SystemExit("duplicate_labels")
print(json.dumps(sorted(labels), separators=(",", ":")))
PYOFFSLOTLABELS
)" || die 2 "offslot-rescue-start invalid --expected-labels-json"

  pr_json="$(gh pr view "$pr" --repo "$repository_id" --json state,isDraft,headRefOid,headRefName,labels 2>/dev/null || true)"
  [ -n "$pr_json" ] || die 1 "offslot-rescue-start cannot read PR #$pr"
  live_state="$(printf '%s' "$pr_json" | json_field state 2>/dev/null || true)"
  live_draft="$(printf '%s' "$pr_json" | json_field isDraft 2>/dev/null || true)"
  live_head="$(printf '%s' "$pr_json" | json_field headRefOid 2>/dev/null || true)"
  live_branch="$(printf '%s' "$pr_json" | json_field headRefName 2>/dev/null || true)"
  [ "$live_state" = "OPEN" ] || die 42 "offslot-rescue-start PR #$pr is not OPEN"
  [ "$live_draft" = "false" ] || die 42 "offslot-rescue-start PR #$pr is draft"
  [ "$live_head" = "$head_sha" ] \
    || die 42 "offslot-rescue-start head drift pr=$pr expected=$head_sha live=${live_head:-none}"

  live_labels_json="$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print(json.dumps(sorted(x.get("name", "") for x in json.load(sys.stdin).get("labels", []) if x.get("name")), separators=(",", ":")))')" \
    || die 1 "offslot-rescue-start cannot normalize live labels"
  desired_labels_json="$(python3 - "$expected_labels_json" <<'PYOFFSLOTDESIRED'
import json
import re
import sys
labels = json.loads(sys.argv[1])
owned = re.compile(r"^(?:pm-state:|pm-blocked:|cto-rescue:|ci-head:|slot:[1-4]$)")
preserved = [label for label in labels if not owned.match(label)]
desired = sorted(set(preserved) | {
    "pm-state:blocked-rework",
    "pm-blocked:cto",
    "cto-rescue:in-progress",
})
print(json.dumps(desired, separators=(",", ":")))
PYOFFSLOTDESIRED
)" || die 1 "offslot-rescue-start cannot compute desired labels"

  if [ -n "${PM_COMMAND_SNAPSHOT:-}" ] && [ -f "$PM_COMMAND_SNAPSHOT" ]; then
    mop_slots_json="$(command_snapshot_mop_slots_json)"
  else
    mop_slots_json="$(curl -sS -m 4 "$MOP_BASE/slots" 2>/dev/null || true)"
  fi
  owner_slots="$(printf '%s' "$mop_slots_json" | python3 -c '
import json
import sys

pr, branch = sys.argv[1:3]
try:
    value = json.load(sys.stdin)
except Exception as exc:
    print(f"invalid_mop_json:{exc}", file=sys.stderr)
    raise SystemExit(1)
slots = value.get("slots")
if not isinstance(slots, list):
    print("invalid_mop_slots", file=sys.stderr)
    raise SystemExit(1)
matches = []
for row in slots:
    if not isinstance(row, dict) or not row.get("occupied"):
        continue
    if str(row.get("pr") or "") == pr or str(row.get("branch") or "") == branch:
        slot = row.get("slot") or row.get("id") or row.get("number")
        if slot is not None:
            matches.append(str(slot))
print(",".join(sorted(set(matches))))
' "$pr" "$live_branch")" \
    || die 42 "offslot-rescue-start cannot prove numbered-slot ownership absent pr=$pr"
  [ -z "$owner_slots" ] \
    || die 42 "offslot-rescue-start refuses active numbered-slot ownership pr=$pr slots=$owner_slots"

  if [ "$live_labels_json" = "$desired_labels_json" ]; then
    status="already-converged"
  else
    [ "$live_labels_json" = "$expected_labels_json" ] \
      || die 43 "offslot-rescue-start complete label drift pr=$pr expected=$expected_labels_json live=$live_labels_json"
    printf '%s' "$live_labels_json" | python3 -c 'import json,sys; raise SystemExit(1 if any(x.startswith("slot:") for x in json.load(sys.stdin)) else 0)' \
      || die 42 "offslot-rescue-start refuses slot-labelled PR #$pr"

    if ! printf '{"labels":%s}' "$desired_labels_json" \
      | gh api --method PATCH "repos/${repository_id}/issues/${pr}" --input - >/dev/null 2>&1; then
      post_json="$(gh pr view "$pr" --repo "$repository_id" --json state,headRefOid,labels 2>/dev/null || true)"
      post_head="$(printf '%s' "$post_json" | json_field headRefOid 2>/dev/null || true)"
      post_labels_json="$(printf '%s' "$post_json" | python3 -c 'import json,sys; print(json.dumps(sorted(x.get("name", "") for x in json.load(sys.stdin).get("labels", []) if x.get("name")), separators=(",", ":")))' 2>/dev/null || true)"
      if [ "$post_head" = "$head_sha" ] && [ "$post_labels_json" = "$expected_labels_json" ]; then
        die 1 "offslot-rescue-start label replacement failed with pre-state intact pr=$pr"
      fi
      [ "$post_head" = "$head_sha" ] \
        || die 21 "offslot-rescue-start ambiguous write plus head drift; rollback refused pr=$pr expected_head=$head_sha live_head=${post_head:-none}"
      printf '{"labels":%s}' "$expected_labels_json" \
        | gh api --method PATCH "repos/${repository_id}/issues/${pr}" --input - >/dev/null 2>&1 \
        || die 21 "offslot-rescue-start rollback failed pr=$pr cause=ambiguous_write"
      rollback_json="$(gh pr view "$pr" --repo "$repository_id" --json headRefOid,labels 2>/dev/null || true)"
      rollback_json="$(printf '%s' "$rollback_json" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps({"headRefOid":d.get("headRefOid"),"labels":sorted(x.get("name","") for x in d.get("labels",[]) if x.get("name"))}, separators=(",", ":")))' 2>/dev/null || true)"
      [ "$rollback_json" = "{\"headRefOid\":\"$head_sha\",\"labels\":$expected_labels_json}" ] \
        || die 21 "offslot-rescue-start rollback postcondition failed pr=$pr state=${rollback_json:-unreadable}"
      die 20 "offslot-rescue-start rolled back pr=$pr cause=ambiguous_write"
    fi
    post_json="$(gh pr view "$pr" --repo "$repository_id" --json state,headRefOid,labels 2>/dev/null || true)"
    post_head="$(printf '%s' "$post_json" | json_field headRefOid 2>/dev/null || true)"
    post_labels_json="$(printf '%s' "$post_json" | python3 -c 'import json,sys; print(json.dumps(sorted(x.get("name", "") for x in json.load(sys.stdin).get("labels", []) if x.get("name")), separators=(",", ":")))' 2>/dev/null || true)"
    [ "$post_head" = "$head_sha" ] \
      || die 21 "offslot-rescue-start postcondition head drift; rollback refused pr=$pr expected_head=$head_sha live_head=${post_head:-none}"
    if [ "$post_labels_json" != "$desired_labels_json" ]; then
      printf '{"labels":%s}' "$expected_labels_json" \
        | gh api --method PATCH "repos/${repository_id}/issues/${pr}" --input - >/dev/null 2>&1 \
        || die 21 "offslot-rescue-start rollback failed pr=$pr cause=postcondition_drift"
      rollback_json="$(gh pr view "$pr" --repo "$repository_id" --json headRefOid,labels 2>/dev/null || true)"
      rollback_json="$(printf '%s' "$rollback_json" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps({"headRefOid":d.get("headRefOid"),"labels":sorted(x.get("name","") for x in d.get("labels",[]) if x.get("name"))}, separators=(",", ":")))' 2>/dev/null || true)"
      [ "$rollback_json" = "{\"headRefOid\":\"$head_sha\",\"labels\":$expected_labels_json}" ] \
        || die 21 "offslot-rescue-start rollback postcondition failed pr=$pr state=${rollback_json:-unreadable}"
      die 20 "offslot-rescue-start rolled back pr=$pr cause=postcondition_drift"
    fi
    status="applied"
  fi

  record_event --source pm-transition --event offslot_rescue_started \
    --target-type pr --target-id "$pr" --pr "$pr" --head-sha "$head_sha" \
    --payload "repository_id=$repository_id" --payload "rescue_kind=$rescue_kind" \
    --payload "owner=$receipt_owner" --payload "receipt_sha256=$receipt_sha" \
    --payload "labels=$desired_labels_json" \
    --dedupe-key "offslot_rescue_started:${repository_id}:${pr}:${head_sha}:${receipt_sha}"
  echo "PM_TRANSITION_OK command=offslot-rescue-start status=$status repository_id=$repository_id pr=$pr head=$head_sha rescue_kind=$rescue_kind owner=$receipt_owner receipt_sha256=$receipt_sha labels=$desired_labels_json"
}


mop_issue_owner_line() {
  # Detect the occupied MoP slot that owns issue $1, reading the /slots JSON from
  # arg $2 (via env var), never from stdin. The `python3 - <<'PY'` heredoc owns
  # stdin (it is the script source), so a `curl ... | python3 - <<'PY'` pipe with
  # `json.load(sys.stdin)` silently reads nothing. See bash-python3 pipe/heredoc
  # stdin collision (#6332 CP-PLAN-CAP repair).
  local issue="$1" slots_json="$2"
  MOP_SLOTS_JSON="$slots_json" python3 - "$issue" <<'PY' 2>/dev/null || true
import json
import os
import sys

issue = str(sys.argv[1])
try:
    data = json.loads(os.environ.get("MOP_SLOTS_JSON") or "{}")
except Exception:
    data = {}
for row in data.get("slots", []):
    if not row.get("occupied"):
        continue
    if str(row.get("issue") or "") != issue:
        continue
    slot = row.get("slot") or row.get("id") or row.get("number") or ""
    branch = row.get("branch") or ""
    task = row.get("task") or ""
    print(f"slot={slot} branch={branch} task={task}")
    break
PY
}

cmd_rescope_issue() {
  local issue="" slot="" branch="" reason="issue-review-loop-circuit-breaker" pm_rescue_proof=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --issue) issue="${2:-}"; shift 2 ;;
      --slot) slot="${2:-}"; shift 2 ;;
      --branch) branch="${2:-}"; shift 2 ;;
      --reason) reason="${2:-}"; shift 2 ;;
      --pm-rescue-proof) pm_rescue_proof="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown rescope-issue arg $1" ;;
    esac
  done
  need_num issue "$issue"
  require_pm_fable_rescue_failure "$pm_rescue_proof" "$reason" "" "$issue" ""
  [ -n "$slot" ] && need_num slot "$slot"
  if [ -n "$slot" ] && ! [[ "$slot" =~ ^[1-4]$ ]]; then
    die 2 "slot must be 1, 2, 3, or 4"
  fi

  local mop_line mop_slot issue_json state title url labels released_slots marker_line marker ledger classes artifacts rounds
  local comment_file comment_marker existing_comment_json comment_id comment_url
  local mop_slots_json
  mop_slots_json="$(curl -sS -m 4 "$MOP_BASE/slots" 2>/dev/null || printf '{}')"
  mop_line="$(mop_issue_owner_line "$issue" "$mop_slots_json")"
  if [ -z "$slot" ]; then
    slot="$(printf '%s\n' "$mop_line" | sed -n 's/.*slot=\([^ ]*\).*/\1/p')"
  fi
  mop_slot="$(printf '%s\n' "$mop_line" | sed -n 's/.*slot=\([^ ]*\).*/\1/p')"
  if [ -n "$slot" ] && [ -z "$mop_slot" ]; then
    die 2 "slot $slot does not currently own issue #$issue in MoP; rerun without --slot to clear stale issue labels only"
  fi
  if [ -n "$slot" ] && [ -n "$mop_slot" ] && [ "$slot" != "$mop_slot" ]; then
    die 2 "slot $slot does not match MoP owner slot $mop_slot for issue #$issue"
  fi
  if [ -z "$branch" ]; then
    branch="$(printf '%s\n' "$mop_line" | sed -n 's/.*branch=\([^ ]*\).*/\1/p')"
  fi

  issue_json="$(gh issue view "$issue" --repo "$REPO" --json state,title,url,labels 2>/dev/null || true)"
  [ -n "$issue_json" ] || die 1 "cannot read issue #$issue"
  state="$(printf '%s' "$issue_json" | json_field state)"
  [ "$state" = "OPEN" ] || die 1 "issue #$issue is not open (state=$state)"
  title="$(printf '%s' "$issue_json" | json_field title)"
  url="$(printf '%s' "$issue_json" | json_field url)"
  labels="$(printf '%s' "$issue_json" | python3 -c 'import json,sys; print(",".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"

  marker_line="$(python3 - "$issue" "$branch" "$title" "$url" "$reason" "$labels" <<'PY'
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

issue, branch, title, url, reason, labels = sys.argv[1:7]
marker = Path(f"/tmp/pm-rescope-issue-{issue}.json")
ledger = Path(f"/tmp/pm-rescope-issue-{issue}.md")
patterns = [
    f"/tmp/pm-review-loop-{issue}.json",
    f"/tmp/pm-codex-pr-rescue-issue-{issue}-*.md",
    f"/Users/rajiv/.claude/control-plane-artifacts/pm-claude-pr-rescue-issue-{issue}-*.md",
    f"/tmp/pm-claude-code-review-{issue}-*.md",
    f"/tmp/pm-claude-plan-review-{issue}-*.md",
    f"/tmp/pm-opus-code-review-{issue}-*.md",
    f"/tmp/pm-opus-plan-review-{issue}-*.md",
    f"/tmp/codex-app-code-review-{issue}.txt",
    f"/tmp/codex-app-plan-review-{issue}.txt",
    f"/tmp/slot-rework-{issue}-*.md",
    f"/tmp/pm-transition-rework-{issue}-*.md",
]
paths = []
for pat in patterns:
    paths.extend(glob.glob(pat))
paths = sorted({Path(p) for p in paths if Path(p).is_file()}, key=lambda p: p.stat().st_mtime)

class_terms = {
    "runtime_control_point_drift": [
        "runtime_control_point", "control point", "wrong control", "directive drift",
        "drifts", "wrong branch", "branch controlled by",
    ],
    "proof_or_test_harness_gap": [
        "false-green", "red-on-revert", "production path", "real production",
        "hand-simulation", "test harness", "proof", "artifact",
    ],
    "scope_or_migration_split": [
        "split", "scope-excluded", "scope excluded", "follow-up", "migration",
        "broad", "reimplement", "close_or_reimplement",
    ],
    "stale_contract_or_plan": [
        "stale plan", "issue contract", "acceptance criteria", "stale issue",
        "plan doc", "ac",
    ],
    "ci_or_local_test_regression": [
        "ci", "e2e", "wall-budget", "timeout", "local", "pytest",
        "playwright", "tsc", "lint",
    ],
    "dependency_or_rebase": [
        "rebase", "merge conflict", "dirty", "behind", "dependency", "blocked on",
    ],
    "product_or_data_model_decision": [
        "product decision", "data-model", "data model", "rajiv", "cto decision",
        "irreversible",
    ],
    "capture_or_fixture": [
        "capture", "fixture", "llm proxy", "proxy cache", "fresh-head capture",
    ],
}

def interesting_lines(text):
    out = []
    rx = re.compile(
        r"(PM_CLAUDE_REVIEW|PM_CODEX_PR_RESCUE|PM_OPUS_REVIEW|VERDICT|"
        r"FINAL_REVIEWER_VERDICT|runtime_control_point|loop_reduction_decision|"
        r"required_pm_action|REQUEST_CHANGES|BLOCK|REVISE|APPROVE|Required|"
        r"BLOCKER|P0|P1|P2|split|scope|proof|red-on-revert|false-green|"
        r"control point|CI|E2E|capture|fixture)",
        re.I,
    )
    for raw in text.splitlines():
        line = " ".join(raw.strip().split())
        if not line:
            continue
        if rx.search(line):
            out.append(line[:260])
        if len(out) >= 24:
            break
    return out

classes = {name: {"count": 0, "examples": []} for name in class_terms}
artifacts = []
for path in paths:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:200000]
    except Exception:
        continue
    lower = text.lower()
    snippets = interesting_lines(text)
    artifacts.append({
        "path": str(path),
        "mtime": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
        "snippets": snippets[:10],
    })
    if path.name == f"pm-review-loop-{issue}.json":
        try:
            loop = json.loads(text)
            for name, count in (loop.get("class_counts_48h") or {}).items():
                if name not in classes:
                    classes[name] = {"count": 0, "examples": []}
                classes[name]["count"] += int(count or 0)
        except Exception:
            pass
    for name, terms in class_terms.items():
        hit_terms = [t for t in terms if t in lower]
        if hit_terms:
            classes[name]["count"] += 1
            if len(classes[name]["examples"]) < 5:
                classes[name]["examples"].append({
                    "artifact": str(path),
                    "evidence": snippets[0] if snippets else hit_terms[0],
                })

active_classes = {k: v for k, v in classes.items() if v["count"] > 0}
round_count = sum(1 for p in paths if re.search(r"/(slot-rework|pm-transition-rework|pm-claude-|pm-opus-|pm-codex-|codex-app-)", str(p)))
now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
data = {
    "schema_version": 1,
    "status": "cto_pending",
    "pr": None,
    "issue": int(issue),
    "branch": branch,
    "title": title,
    "url": url,
    "reason": reason,
    "created_at": now,
    "labels_before": [x for x in labels.split(",") if x],
    "round_count": round_count,
    "source_artifact_count": len(artifacts),
    "evidence_classes": active_classes,
    "ledger": str(ledger),
    "required_terminal_decisions": ["cto_disposition"],
    "required_pm_action": "escalate_to_cto_and_release_slot",
}

def bullet_examples(info):
    rows = []
    for ex in info.get("examples", [])[:4]:
        rows.append(f"  - `{ex['artifact']}`: {ex['evidence']}")
    return "\n".join(rows) if rows else "  - Evidence came from review-loop counters; inspect source artifacts."

lines = [
    f"# CTO Rescue Escalation: issue #{issue}",
    "",
    f"- Created: {now}",
    f"- Reason: `{reason}`",
    f"- Branch: `{branch or 'unknown'}`",
    f"- Issue: {url or f'https://github.com/heydonna-app/heydonna-app/issues/{issue}'}",
    f"- Source artifacts: {len(artifacts)}",
    f"- Estimated rework/review rounds: {round_count}",
    "",
    "## Why This Issue Is Frozen",
    "",
    "This issue crossed the plan-review circuit breaker before a PR existed. The dev slot is released and no Codex rescue or further same-issue review is permitted. CTO owns the next disposition.",
    "",
    "## Evidence Classes From Prior Rounds",
    "",
]
if active_classes:
    for name, info in sorted(active_classes.items(), key=lambda item: (-int(item[1].get("count") or 0), item[0])):
        lines.append(f"### {name} ({info['count']} signals)")
        lines.append("")
        lines.append(bullet_examples(info))
        lines.append("")
else:
    lines.extend(["No class matched automatically. PM must inspect the listed artifacts before unfreezing this issue.", ""])

lines.extend([
    "## Required Terminal Decision",
    "",
    "### PM Decision Matrix",
    "",
    "- Choose `split_and_reimplement` when the evidence shows broad scope, repeated same-class blockers, or stale contract churn. This is the default for issue-level circuit-breaker rows unless a verified patch packet is already narrow and ready.",
    "- Choose `final_verified_patch` only when the remaining blocker is narrow and PM can cite one verified patch packet with apply/proof instructions.",
    "- Choose `override_with_evidence` only when PM has read the cited code/proof and product/runtime risk is cleared.",
    "- Choose `escalate_product_decision` only for a concrete unresolved product/data-model/authority question that PM cannot answer from Rajiv's directive, the issue/spec, or code evidence.",
    "",
    "Record the choice with `pm-transition.sh rescope-issue-decide --issue "
    f"{issue} --decision <final_verified_patch|split_and_reimplement|override_with_evidence|escalate_product_decision>` before any more implementation work.",
    "",
    "## Source Artifacts",
    "",
])
for art in artifacts:
    lines.append(f"- `{art['path']}` ({art['mtime']})")
ledger.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
tmp = marker.with_name(f"{marker.name}.{os.getpid()}.tmp")
tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(marker)
print(f"marker={marker} ledger={ledger} classes={','.join(sorted(active_classes)) or 'none'} artifacts={len(artifacts)} rounds={round_count}")
PY
)" || die 1 "failed to build rescope packet for issue #$issue"

  marker="$(printf '%s\n' "$marker_line" | sed -n 's/.*marker=\([^ ]*\).*/\1/p')"
  ledger="$(printf '%s\n' "$marker_line" | sed -n 's/.*ledger=\([^ ]*\).*/\1/p')"
  classes="$(printf '%s\n' "$marker_line" | sed -n 's/.*classes=\([^ ]*\).*/\1/p')"
  artifacts="$(printf '%s\n' "$marker_line" | sed -n 's/.*artifacts=\([0-9][0-9]*\).*/\1/p')"
  rounds="$(printf '%s\n' "$marker_line" | sed -n 's/.*rounds=\([0-9][0-9]*\).*/\1/p')"
  [ -n "$marker" ] || die 1 "issue rescope marker path missing"
  [ -n "$ledger" ] || die 1 "issue rescope ledger path missing"

  if ! released_slots="$(release_issue_owner_for_pm_transition "$issue" "$slot" "$branch" "rescope-issue")"; then
    die 13 "CTO rescue issue owner release failed issue=$issue slot=${slot:-none} branch=${branch:-unknown}; no issue state was advanced"
  fi

  gh issue edit "$issue" --repo "$REPO" --remove-label "status:todo" >/dev/null 2>&1 || true
  gh issue edit "$issue" --repo "$REPO" --remove-label "status:in-progress" >/dev/null 2>&1 || true
  gh issue edit "$issue" --repo "$REPO" --add-label "status:in-review" >/dev/null 2>&1 || true
  gh issue edit "$issue" --repo "$REPO" --add-label "pm-state:rescope-required" >/dev/null 2>&1 || true
  gh issue edit "$issue" --repo "$REPO" --add-label "pm-blocked:cto" >/dev/null \
    || die 1 "failed to add pm-blocked:cto to issue #$issue"
  clear_other_slot_labels issue "$issue" ""
  comment_marker="<!-- heydonna-cto-rescue issue=${issue} -->"
  comment_file="/tmp/pm-cto-rescue-issue-${issue}.md"
  {
    printf '%s\n\n' "$comment_marker"
    printf '> **CTO RESCUE REQUIRED** — plan-review cap reached. The dev slot has been released and `pm-blocked:cto` prevents further work. Continue from the evidence below.\n\n'
    cat "$ledger"
  } > "$comment_file"
  existing_comment_json="$(gh api "repos/${REPO}/issues/${issue}/comments" --paginate --jq ".[] | select(.body | contains(\"$comment_marker\")) | {id: .id, url: .html_url}" 2>/dev/null | head -1 || true)"
  if [ -z "$existing_comment_json" ]; then
    gh issue comment "$issue" --repo "$REPO" --body-file "$comment_file" >/dev/null \
      || die 1 "failed to post durable CTO rescue handoff comment to issue #$issue"
    existing_comment_json="$(gh api "repos/${REPO}/issues/${issue}/comments" --paginate --jq ".[] | select(.body | contains(\"$comment_marker\")) | {id: .id, url: .html_url}" 2>/dev/null | head -1 || true)"
  fi
  comment_id="$(printf '%s' "$existing_comment_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id", ""))' 2>/dev/null || true)"
  comment_url="$(printf '%s' "$existing_comment_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("url", ""))' 2>/dev/null || true)"
  [ -n "$comment_id" ] && [ -n "$comment_url" ] \
    || die 1 "durable CTO rescue handoff comment receipt missing for issue #$issue"
  python3 - "$marker" "$comment_id" "$comment_url" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
data["issue_comment_id"] = int(sys.argv[2])
data["issue_comment_url"] = sys.argv[3]
tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(path)
PY
  upsert_obligation --kind cto_rescue --severity high --target-type issue --target-id "$issue" --issue "$issue" --owner cto --horizon hourly --next-review-at "$(utc_plus_minutes 30)" --title "Issue #$issue requires CTO rescue" --action "Read the durable issue handoff comment and record the CTO disposition before PM creates a new assignment epoch. Do not run Codex rescue or reassign while pm-blocked:cto remains." --blocker "cto_rescue_required" --evidence "marker=$marker comment_url=$comment_url classes=${classes:-none} artifacts=${artifacts:-0} rounds=${rounds:-0}"
  record_event --source pm-transition --event cto_rescue_escalated --target-type issue --target-id "$issue" --issue "$issue" --slot "${slot:-}" --payload "reason=$reason" --payload "marker=$marker" --payload "ledger=$ledger" --payload "comment_url=$comment_url" --payload "classes=${classes:-none}" --payload "artifacts=${artifacts:-0}" --payload "rounds=${rounds:-0}" --payload "released_slots=${released_slots:-none}" --payload "branch=${branch:-unknown}"
  kanban_flag CTO_RESCUE "issue=$issue slot=${slot:-unknown} released=${released_slots:-none} marker=$marker"
  transition_alert --event decision-required --issue "$issue" --slot "${slot:-}" --state cto_review --branch "$branch" --reason "review-cap:${reason}" --proof "$marker" --message-file "$comment_file"
  [ -n "$released_slots" ] && run_post_release_sweep "cto-rescue-issue"
  echo "PM_TRANSITION_OK command=cto-rescue-issue issue=$issue state=rescope-required blocker=pm-blocked:cto released_slots=${released_slots:-none} marker=$marker comment_url=$comment_url classes=${classes:-none} artifacts=${artifacts:-0} rounds=${rounds:-0}"
}

issue_rescope_forward_state() {
  python3 "$ISSUE_CLAIMABILITY" forward-state
}

cmd_rescope_issue_decide() {
  local issue="" decision="" rationale="" proof="" applied_branch="" applied_head="" approval="" child_plan="" question="" recommended_default="" rescope_contract=""
  local allow_escalate=0 replace_existing=0 no_scope_change=0
  local disposition_comment_id="" disposition_comment_url="" disposition_comment_file=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --issue) issue="${2:-}"; shift 2 ;;
      --decision) decision="${2:-}"; shift 2 ;;
      --rationale) rationale="${2:-}"; shift 2 ;;
      --proof) proof="${2:-}"; shift 2 ;;
      --applied-branch) applied_branch="${2:-}"; shift 2 ;;
      --applied-head) applied_head="${2:-}"; shift 2 ;;
      --approval) approval="${2:-}"; shift 2 ;;
      --child-plan) child_plan="${2:-}"; shift 2 ;;
      --question) question="${2:-}"; shift 2 ;;
      --recommended-default) recommended_default="${2:-}"; shift 2 ;;
      --rescope-contract) rescope_contract="${2:-}"; shift 2 ;;
      --no-scope-change) no_scope_change=1; shift ;;
      --allow-escalate) allow_escalate=1; shift ;;
      --replace-existing) replace_existing=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown rescope-issue-decide arg $1" ;;
    esac
  done
  need_num issue "$issue"
  [ -n "$decision" ] || die 2 "--decision is required"

  local marker out default_decision ledger prior_decision issue_json issue_state live_labels
  local issue_forward_state
  local cto_consumed=0 forward_state=""
  marker="/tmp/pm-rescope-issue-${issue}.json"
  [ -f "$marker" ] || die 42 "issue #$issue has no rescope marker; run /Users/rajiv/.claude/scripts/pm-transition.sh rescope-issue --issue $issue first"
  validate_rescope_contract_arg "$decision" "$rescope_contract" "$no_scope_change"

  # ISSUE_APPLIED_PATCH_MATERIALIZATION_V1
  local applied_mode=0 remote_applied_head="" approval_evidence=""
  if [ -n "$applied_branch$applied_head$approval" ]; then
    applied_mode=1
    [ "$decision" = "final_verified_patch" ] || die 2 "applied patch metadata requires --decision final_verified_patch"
    [ -n "$applied_branch" ] && [ -n "$applied_head" ] && [ -n "$approval" ] \
      || die 2 "--applied-branch, --applied-head, and --approval must be provided together"
    [[ "$applied_branch" =~ ^[A-Za-z0-9._/-]+$ ]] \
      || die 2 "--applied-branch contains unsupported characters"
    [[ "$applied_head" =~ ^[0-9a-f]{40}$ ]] \
      || die 2 "--applied-head must be a full 40-character commit SHA"
    [ -f "$proof" ] || die 2 "applied patch --proof must be an existing exact-head packet"
    grep -Fq "$applied_head" "$proof" \
      || die 42 "applied patch proof is not bound to head $applied_head"
    if [ -f "$approval" ]; then
      approval_evidence="$(cat "$approval")"
    else
      approval_evidence="$approval"
    fi
    printf '%s' "$approval_evidence" | grep -Fq "$applied_head" \
      || die 42 "applied patch approval is not bound to head $applied_head"
    remote_applied_head="$(gh api "repos/${REPO}/git/ref/heads/${applied_branch}" --jq '.object.sha' 2>/dev/null || true)"
    [[ "$remote_applied_head" =~ ^[0-9a-f]{40}$ ]] \
      || die 42 "cannot resolve remote applied branch $applied_branch"
    [ "$remote_applied_head" = "$applied_head" ] \
      || die 42 "applied patch branch drift branch=$applied_branch expected=$applied_head live=$remote_applied_head"
  fi

  local rescope_decision_snapshot="" rescope_ledger_path="" rescope_ledger_existed=0
  if [ -n "$rescope_contract" ]; then
    rescope_decision_snapshot="$(mktemp -d)" \
      || die 1 "failed to create issue rescope decision snapshot"
    cp "$marker" "$rescope_decision_snapshot/marker.json" \
      || { rm -r "$rescope_decision_snapshot"; die 1 "failed to snapshot issue rescope marker"; }
    rescope_ledger_path="$(python3 - "$marker" <<'PY'
import json
import sys
from pathlib import Path

marker = Path(sys.argv[1])
data = json.loads(marker.read_text(encoding="utf-8"))
print(str(data.get("ledger") or marker.with_suffix(".md")))
PY
)"
    if [ -f "$rescope_ledger_path" ]; then
      cp "$rescope_ledger_path" "$rescope_decision_snapshot/ledger.md" \
        || { rm -r "$rescope_decision_snapshot"; die 1 "failed to snapshot issue rescope ledger"; }
      rescope_ledger_existed=1
    fi
  fi

  if ! out="$(python3 - "$marker" "$decision" "$rationale" "$proof" "$child_plan" "$question" "$recommended_default" "$allow_escalate" "$replace_existing" <<'PY' 2>&1
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

marker = Path(sys.argv[1])
decision, rationale, proof, child_plan, question, recommended_default = sys.argv[2:8]
allow_escalate = sys.argv[8] == "1"
replace_existing = sys.argv[9] == "1"
valid = {
    "final_verified_patch",
    "split_and_reimplement",
    "override_with_evidence",
    "escalate_product_decision",
}
if decision not in valid:
    raise SystemExit(f"invalid_decision={decision}; expected one of {','.join(sorted(valid))}")
if recommended_default and recommended_default not in valid:
    raise SystemExit(f"invalid_recommended_default={recommended_default}")
data = json.loads(marker.read_text(encoding="utf-8"))
prior_decision = str(data.get("terminal_decision") or "")
classes = data.get("evidence_classes") or {}
class_names = set(classes)
rounds = int(data.get("round_count") or 0)
artifacts = int(data.get("source_artifact_count") or 0)
repeated = rounds >= 3 or artifacts >= 3 or len(class_names) >= 3
if "scope_or_migration_split" in class_names and repeated:
    matrix_default = "split_and_reimplement"
elif "runtime_control_point_drift" in class_names and repeated:
    matrix_default = "split_and_reimplement"
elif "capture_or_fixture" in class_names and "proof_or_test_harness_gap" in class_names and repeated:
    matrix_default = "split_and_reimplement"
elif "proof_or_test_harness_gap" in class_names and rounds <= 2:
    matrix_default = "final_verified_patch"
elif "product_or_data_model_decision" in class_names and not (class_names & {"scope_or_migration_split", "runtime_control_point_drift"}):
    matrix_default = "escalate_product_decision"
else:
    matrix_default = "split_and_reimplement" if repeated else "final_verified_patch"

if data.get("status") == "resolved" and prior_decision and not replace_existing:
    if prior_decision == decision:
        print(f"decision={decision} default={matrix_default} marker={marker} ledger={data.get('ledger') or ''} prior={prior_decision} status=already_resolved")
        raise SystemExit(0)
    raise SystemExit(f"already_resolved prior_decision={prior_decision}; pass --replace-existing to supersede with {decision}")

def require(value: str, flag: str, why: str) -> None:
    if not str(value or "").strip():
        raise SystemExit(f"{decision}_requires_{flag}: {why}")

def _cto_disposition_resolved(issue_num: int) -> bool:
    """True when the ops ledger records a resolved cto_rescue obligation for
    the issue: the CTO disposition was actually recorded (incident 9977).
    Read-only, fail-closed; never fabricates an artifact count."""
    if issue_num <= 0:
        return False
    db_path = os.environ.get("PM_OPS_DB") or str(
        Path.home() / ".claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db"
    )
    try:
        import sqlite3
    except Exception:
        return False
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except (sqlite3.Error, OSError):
        return False
    try:
        row = con.execute(
            "SELECT 1 FROM obligations WHERE kind=? AND target_type=? AND target_id=? AND status=? LIMIT 1",
            ("cto_rescue", "issue", str(issue_num), "resolved"),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False
    finally:
        con.close()

if decision == "split_and_reimplement":
    require(child_plan, "--child-plan", "name the child issue split before resolving the old issue")
elif decision == "final_verified_patch":
    require(proof, "--proof", "cite the verified patch/instruction packet and apply/proof command")
elif decision == "override_with_evidence":
    require(proof, "--proof", "cite code/artifact evidence that product/runtime risk is cleared")
    require(rationale, "--rationale", "explain why residual issues are non-gating follow-ups")
    if artifacts < 1:
        # Incident 9977: a zero-artifact CTO-rescue marker is admissible for
        # override_with_evidence only when the recorded CTO disposition chain
        # exists: the marker is cto_pending awaiting a cto_disposition, the
        # durable rescue comment is bound, and the cto_rescue obligation has
        # been resolved in the ops ledger. Ordinary code rescues still require
        # reviewed source artifacts; nothing here fabricates an artifact count.
        cto_disposition_recorded = (
            data.get("status") == "cto_pending"
            and "cto_disposition" in (data.get("required_terminal_decisions") or [])
            and str(data.get("issue_comment_url") or "").startswith("https://")
            and _cto_disposition_resolved(int(data.get("issue") or 0))
        )
        if not cto_disposition_recorded:
            raise SystemExit(
                "override_with_evidence_requires_source_artifact: "
                "the rescue packet recorded no source artifacts"
            )
        data["cto_disposition_evidence"] = {
            "basis": "resolved cto_rescue obligation plus durable CTO rescue comment",
            "issue": int(data.get("issue") or 0),
            "issue_comment_url": data.get("issue_comment_url"),
            "proof": proof,
        }
    if not Path(proof).is_file():
        raise SystemExit(
            "override_with_evidence_requires_proof_file: "
            "pass the reviewed code/artifact packet path, not free-form prose"
        )
elif decision == "escalate_product_decision":
    require(question, "--question", "ask one concrete product/data-model/authority question")
    require(recommended_default, "--recommended-default", "state PM's default recommendation")
    require(rationale, "--rationale", "explain why PM cannot decide from directive/spec/code evidence")
    if matrix_default != "escalate_product_decision" and not allow_escalate:
        raise SystemExit(
            "escalation_blocked_by_matrix "
            f"default={matrix_default} classes={','.join(sorted(class_names)) or 'none'} rounds={rounds} artifacts={artifacts}; "
            "use the default or pass --allow-escalate with a concrete unresolved question"
        )

now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
if prior_decision:
    data.setdefault("decision_history", []).append({
        "terminal_decision": prior_decision,
        "terminal_decision_at": data.get("terminal_decision_at"),
        "rationale": data.get("terminal_decision_rationale"),
        "superseded_at": now,
    })
data["status"] = "resolved"
data["terminal_decision"] = decision
data["terminal_decision_at"] = now
data["decision"] = decision
data["decided_at"] = now
data["decision_matrix_default"] = matrix_default
data["terminal_decision_rationale"] = rationale
if proof:
    data["terminal_decision_proof"] = proof
if child_plan:
    data["split_child_plan"] = child_plan
if question:
    data["product_decision_question"] = question
if recommended_default:
    data["recommended_default"] = recommended_default
if allow_escalate:
    data["allow_escalate_override"] = True

ledger = Path(data.get("ledger") or marker.with_suffix(".md"))
section = [
    "",
    "## Terminal Rescope Decision",
    "",
    f"- Decided: {now}",
    f"- Decision: `{decision}`",
    f"- Matrix default: `{matrix_default}`",
]
if prior_decision:
    section.append(f"- Supersedes prior decision: `{prior_decision}`")
if rationale:
    section.append(f"- Rationale: {rationale}")
if proof:
    section.append(f"- Proof: `{proof}`")
if child_plan:
    section.extend(["", "### Child Plan", "", child_plan])
if question:
    section.extend(["", "### Escalation Question", "", question])
if recommended_default:
    section.append(f"- PM recommended default: `{recommended_default}`")
if ledger.exists():
    ledger.write_text(ledger.read_text(encoding="utf-8", errors="replace").rstrip() + "\n" + "\n".join(section).rstrip() + "\n", encoding="utf-8")
else:
    ledger.write_text("\n".join(section).lstrip() + "\n", encoding="utf-8")
tmp = marker.with_name(f"{marker.name}.{os.getpid()}.tmp")
tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(marker)
print(f"decision={decision} default={matrix_default} marker={marker} ledger={ledger} prior={prior_decision or 'none'} status=resolved")
PY
  )"; then
    [ -z "$rescope_decision_snapshot" ] || rm -r "$rescope_decision_snapshot"
    die 42 "$out"
  fi

  default_decision="$(printf '%s\n' "$out" | sed -n 's/.*default=\([^ ]*\).*/\1/p')"
  ledger="$(printf '%s\n' "$out" | sed -n 's/.*ledger=\([^ ]*\).*/\1/p')"
  prior_decision="$(printf '%s\n' "$out" | sed -n 's/.*prior=\([^ ]*\).*/\1/p')"

  if [ "$applied_mode" = "1" ]; then
    python3 - "$marker" "$applied_branch" "$applied_head" "$approval" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

marker = Path(sys.argv[1])
branch, head, approval = sys.argv[2:5]
data = json.loads(marker.read_text(encoding="utf-8"))
if data.get("status") != "resolved" or data.get("terminal_decision") != "final_verified_patch":
    raise SystemExit("applied patch requires a resolved final_verified_patch marker")
existing = data.get("applied_patch") or {}
if existing and (existing.get("branch") != branch or existing.get("head") != head):
    raise SystemExit(
        f"applied patch marker drift existing_branch={existing.get('branch')} "
        f"existing_head={existing.get('head')} requested_branch={branch} requested_head={head}"
    )
now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
data["applied_patch"] = {
    "branch": branch,
    "head": head,
    "approval": approval,
    "recorded_at": existing.get("recorded_at") or now,
}
tmp = marker.with_name(f"{marker.name}.{os.getpid()}.tmp")
tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(marker)
PY
    [ "$?" = "0" ] || die 42 "failed to persist applied patch metadata for issue #$issue"
  fi

  local rescope_receipt="rescope_status=NONE rescope_contract_digest=none issue_contract_updated=not_applicable pr_summary_updated=not_applicable"
  if [ -n "$rescope_contract" ]; then
    if ! rescope_receipt="$(persist_rescope_contract "$issue" "" "$decision" "$rescope_contract" "$marker")"; then
      cp "$rescope_decision_snapshot/marker.json" "$marker" \
        || die 70 "rescope contract failed and issue marker rollback also failed marker=$marker"
      if [ "$rescope_ledger_existed" = "1" ]; then
        cp "$rescope_decision_snapshot/ledger.md" "$rescope_ledger_path" \
          || die 70 "rescope contract failed and issue ledger rollback also failed ledger=$rescope_ledger_path"
      else
        rm -f "$rescope_ledger_path"
      fi
      rm -r "$rescope_decision_snapshot"
      die 42 "rescope contract persistence failed for issue #$issue: ${rescope_receipt:-unknown}"
    fi
    rm -r "$rescope_decision_snapshot"
  elif [ "$no_scope_change" = "1" ]; then
    python3 "$RESCOPE_CONTRACT_TOOL" no-scope-receipt --marker-file "$marker" --evidence "$proof" \
      || die 42 "failed to record no-scope-change receipt for issue #$issue"
    rescope_receipt="rescope_status=NONE scope_change_assertion=no rescope_contract_digest=none issue_contract_updated=not_applicable pr_summary_updated=not_applicable"
  fi

  disposition_comment_id="$(python3 - "$marker" <<'PY' 2>/dev/null || true
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(data.get("issue_comment_id") or "")
PY
)"
  if [ -z "$disposition_comment_id" ]; then
    disposition_comment_id="$(gh api "repos/${REPO}/issues/${issue}/comments" --paginate \
      --jq ".[] | select(.body | contains(\"<!-- heydonna-cto-rescue issue=${issue} -->\")) | .id" \
      2>/dev/null | tail -1 || true)"
  fi
  [ -n "$disposition_comment_id" ] \
    || die 1 "durable CTO rescue comment missing for issue #$issue; refusing an untracked terminal disposition"
  disposition_comment_file="/tmp/pm-cto-disposition-issue-${issue}.md"
  {
    printf '<!-- heydonna-cto-rescue issue=%s -->\n\n' "$issue"
    printf '> **CTO RESCUE RESOLVED** — terminal decision `%s` recorded.\n\n' "$decision"
    cat "$ledger"
  } > "$disposition_comment_file"
  gh api --method PATCH "repos/${REPO}/issues/comments/${disposition_comment_id}" \
    -F "body=@${disposition_comment_file}" >/dev/null \
    || die 1 "failed to update durable CTO rescue comment for issue #$issue decision=$decision"
  disposition_comment_url="$(gh api "repos/${REPO}/issues/comments/${disposition_comment_id}" --jq .html_url 2>/dev/null || true)"
  [ -n "$disposition_comment_url" ] \
    || die 1 "updated CTO disposition comment returned no URL for issue #$issue"
  python3 - "$marker" "$disposition_comment_id" "$disposition_comment_url" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
data["issue_comment_id"] = int(sys.argv[2])
data["issue_comment_url"] = sys.argv[3]
data["issue_comment_status"] = "terminal_decision_recorded"
data["issue_comment_updated_at"] = (
    datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
)
tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(path)
PY

  if [ "$decision" = "final_verified_patch" ] || [ "$decision" = "override_with_evidence" ]; then
    issue_json="$(gh issue view "$issue" --repo "$REPO" --json state,labels,body 2>/dev/null || true)"
    [ -n "$issue_json" ] || die 1 "cannot read issue #$issue after $decision decision"
    issue_state="$(printf '%s' "$issue_json" | json_field state 2>/dev/null || true)"
    [ "$issue_state" = "OPEN" ] || die 1 "issue #$issue is not open after $decision decision"
    live_labels="$(printf '%s' "$issue_json" | python3 -c 'import json,sys; print(",".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
    issue_forward_state="$(printf '%s' "$issue_json" | issue_rescope_forward_state 2>/dev/null || true)"
    [ "$issue_forward_state" = "status:todo" ] || [ "$issue_forward_state" = "status:deferred" ] \
      || die 1 "cannot classify issue #$issue claimability after $decision decision"
    forward_state="$issue_forward_state"
    if printf '%s\n' "$live_labels" | tr ',' '\n' | grep -Eq '^(slot:[1-4]|status:in-progress)$'; then
      die 1 "issue #$issue still has active slot ownership after $decision decision labels=$live_labels"
    fi

    if [ "$forward_state" = "status:deferred" ]; then
      if labels_include "$live_labels" "status:deferred" \
        && labels_include "$live_labels" "pm-nonclaimable:tracking" \
        && ! labels_include "$live_labels" "status:todo" \
        && ! labels_include "$live_labels" "status:in-review" \
        && ! labels_include "$live_labels" "pm-state:rescope-required" \
        && ! labels_include "$live_labels" "pm-blocked:cto"; then
        cto_consumed=already
      else
        if ! gh issue edit "$issue" --repo "$REPO" \
          --remove-label "status:todo" \
          --remove-label "status:in-review" \
          --remove-label "pm-state:rescope-required" \
          --remove-label "pm-blocked:cto" \
          --add-label "status:deferred" \
          --add-label "pm-nonclaimable:tracking" \
          >/dev/null; then
          die 1 "failed to restore issue #$issue to nonclaimable tracking after $decision decision"
        fi
        issue_json="$(gh issue view "$issue" --repo "$REPO" --json state,labels,body 2>/dev/null || true)"
        live_labels="$(printf '%s' "$issue_json" | python3 -c 'import json,sys; print(",".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
        if ! labels_include "$live_labels" "status:deferred" \
          || ! labels_include "$live_labels" "pm-nonclaimable:tracking" \
          || labels_include "$live_labels" "status:todo" \
          || labels_include "$live_labels" "status:in-review" \
          || labels_include "$live_labels" "pm-state:rescope-required" \
          || labels_include "$live_labels" "pm-blocked:cto"; then
          die 1 "issue #$issue nonclaimable tracking transition did not converge labels=$live_labels"
        fi
        cto_consumed=1
      fi
    else
      if labels_include "$live_labels" "status:todo" \
        && ! labels_include "$live_labels" "status:in-review" \
        && ! labels_include "$live_labels" "pm-state:rescope-required" \
        && ! labels_include "$live_labels" "pm-blocked:cto"; then
        cto_consumed=already
      else
        if ! gh issue edit "$issue" --repo "$REPO" \
          --remove-label "status:in-review" \
          --remove-label "pm-state:rescope-required" \
          --remove-label "pm-blocked:cto" \
          --add-label "status:todo" \
          >/dev/null; then
          gh issue edit "$issue" --repo "$REPO" \
            --add-label "status:in-review" \
            --add-label "pm-state:rescope-required" \
            --add-label "pm-blocked:cto" \
            --remove-label "status:todo" \
            >/dev/null 2>&1 || true
          die 1 "failed to move issue #$issue to status:todo after $decision decision; CTO hold restored"
        fi

        issue_json="$(gh issue view "$issue" --repo "$REPO" --json state,labels,body 2>/dev/null || true)"
        live_labels="$(printf '%s' "$issue_json" | python3 -c 'import json,sys; print(",".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
        if ! labels_include "$live_labels" "status:todo" \
          || labels_include "$live_labels" "status:in-review" \
          || labels_include "$live_labels" "pm-state:rescope-required" \
          || labels_include "$live_labels" "pm-blocked:cto"; then
          gh issue edit "$issue" --repo "$REPO" \
            --add-label "status:in-review" \
            --add-label "pm-state:rescope-required" \
            --add-label "pm-blocked:cto" \
            --remove-label "status:todo" \
            >/dev/null 2>&1 || true
          die 1 "issue #$issue $decision label transition did not converge; CTO hold restored labels=$live_labels"
        fi
        cto_consumed=1
      fi
    fi

    python3 - "$marker" "$decision" "$forward_state" <<'PY' \
      || die 1 "failed to record consumed $decision state for issue #$issue"
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

marker, decision, forward_state = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
data = json.loads(marker.read_text(encoding="utf-8"))
if data.get("status") != "resolved" or data.get("terminal_decision") != decision:
    raise SystemExit("resolved decision marker mismatch")
now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
data["decision"] = decision
data["decided_at"] = data.get("terminal_decision_at") or now
data["forward_state"] = forward_state
data["decision_consumed_at"] = now
tmp = marker.with_name(f"{marker.name}.{os.getpid()}.tmp")
tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(marker)
PY
    resolve_target_obligations --kind cto_rescue --target-type issue --target-id "$issue" --issue "$issue" --reason "cto_disposition_consumed" --external-state "decision=$decision marker=$marker state=$forward_state"
    record_event --source pm-transition --event cto_disposition_consumed --target-type issue --target-id "$issue" --issue "$issue" --payload "decision=$decision" --payload "state=$forward_state" --payload "marker=$marker"
  fi

  resolve_target_obligations --kind review_loop_rescope --target-type issue --target-id "$issue" --issue "$issue" --reason "issue_rescope_decision_recorded" --external-state "decision=$decision marker=$marker"
  case "$decision" in
    split_and_reimplement)
      upsert_obligation --kind rescope_split_execution --severity high --target-type issue --target-id "$issue" --issue "$issue" --owner pm --horizon hourly --next-review-at "$(utc_plus_minutes 30)" --title "Issue #$issue split-and-reimplement decision recorded" --action "File child issues from the rescope child plan, close/park the old broad issue, and dispatch each child from current main/current base. Do not reassign the broad old issue." --blocker "issue_rescope_split_execution" --evidence "marker=$marker ledger=${ledger:-unknown}"
      ;;
    final_verified_patch)
      if [ "$applied_mode" = "1" ]; then
        resolve_target_obligations --kind rescope_final_patch --target-type issue --target-id "$issue" --issue "$issue" --reason "approved_patch_already_applied" --external-state "branch=$applied_branch head=$applied_head approval=$approval"
        upsert_obligation --kind rescope_final_patch_materialization --severity critical --target-type issue --target-id "$issue" --issue "$issue" --owner pm --horizon hourly --next-review-at "$(utc_plus_minutes 0)" --title "Issue #$issue approved patch must be materialized as a PR" --action "Refresh $applied_branch from current main without changing the reviewed product/test blobs, create the PR from an exact descendant of $applied_head, dispatch genuine exact-head pull_request CI/E2E, and return PR/head/run IDs. Do not reassign a slot or request another review." --blocker "approved_patch_not_materialized" --evidence "proof=$proof marker=$marker branch=$applied_branch applied_head=$applied_head approval=$approval"
      else
        upsert_obligation --kind rescope_final_patch --severity high --target-type issue --target-id "$issue" --issue "$issue" --owner pm --horizon hourly --next-review-at "$(utc_plus_minutes 30)" --title "Issue #$issue final verified patch decision recorded" --action "Deliver the verified PM/Codex patch packet to the current owner or assign a fresh slot with the packet, then require local proof before another review." --blocker "issue_rescope_final_patch" --evidence "proof=${proof:-none} marker=$marker"
      fi
      ;;
    override_with_evidence)
      upsert_obligation --kind rescope_override --severity high --target-type issue --target-id "$issue" --issue "$issue" --owner pm --horizon hourly --next-review-at "$(utc_plus_minutes 30)" --title "Issue #$issue override-with-evidence decision recorded" --action "Link bounded follow-ups for non-gating residue, then dispatch or close according to the issue state." --blocker "issue_rescope_override" --evidence "proof=${proof:-none} marker=$marker"
      ;;
    escalate_product_decision)
      upsert_obligation --kind rescope_product_escalation --severity high --target-type issue --target-id "$issue" --issue "$issue" --owner pm --horizon hourly --next-review-at "$(utc_plus_minutes 30)" --title "Issue #$issue product/data-model escalation decision recorded" --action "Ask the exact recorded product/data-model question and include PM's recommended default. Do not present broad split-vs-reassign choices." --blocker "issue_rescope_product_decision" --evidence "question=${question:-none} recommended_default=${recommended_default:-none} marker=$marker"
      ;;
  esac
  record_event --source pm-transition --event rescope_issue_decide --target-type issue --target-id "$issue" --issue "$issue" --payload "decision=$decision" --payload "matrix_default=${default_decision:-unknown}" --payload "prior=${prior_decision:-none}" --payload "marker=$marker" --payload "ledger=${ledger:-unknown}" --payload "comment_url=$disposition_comment_url" --payload "applied_branch=${applied_branch:-none}" --payload "applied_head=${applied_head:-none}" --payload "$rescope_receipt"
  kanban_flag PM_TRANSITION "rescope_issue_decide issue=$issue decision=$decision default=${default_decision:-unknown} marker=$marker"
  echo "PM_TRANSITION_OK command=rescope-issue-decide issue=$issue decision=$decision default=${default_decision:-unknown} prior=${prior_decision:-none} cto_consumed=$cto_consumed forward_state=${forward_state:-decision-recorded} marker=$marker ledger=${ledger:-unknown} comment_url=$disposition_comment_url applied_branch=${applied_branch:-none} applied_head=${applied_head:-none} $rescope_receipt"
}

cmd_rescope_split_complete() {
  local issue="" pr="" child="" marker="" target_type="" target_id="" target_json=""
  local target_state="" live_labels="" child_json="" child_csv="" parent_ref="" already_closed=0
  local -a child_issues=() target_args=()

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --issue) issue="${2:-}"; shift 2 ;;
      --pr) pr="${2:-}"; shift 2 ;;
      --child-issue) child_issues+=("${2:-}"); shift 2 ;;
      -h|--help) usage; return 0 ;;
      *) die 2 "unknown rescope-split-complete arg $1" ;;
    esac
  done

  if { [ -n "$issue" ] && [ -n "$pr" ]; } || { [ -z "$issue" ] && [ -z "$pr" ]; }; then
    die 2 "rescope-split-complete requires exactly one of --issue or --pr"
  fi
  [ "${#child_issues[@]}" -gt 0 ] \
    || die 2 "rescope-split-complete requires at least one --child-issue"

  if [ -n "$issue" ]; then
    need_num issue "$issue"
    target_type="issue"
    target_id="$issue"
    marker="/tmp/pm-rescope-issue-${issue}.json"
    target_args=(--issue "$issue")
  else
    need_num pr "$pr"
    target_type="pr"
    target_id="$pr"
    marker="/tmp/pm-rescope-pr-${pr}.json"
    target_args=(--pr "$pr")
  fi

  [ -f "$marker" ] \
    || die 42 "$target_type #$target_id has no rescope marker; reconstruct and record the split decision before completion"
  python3 - "$marker" "$target_type" "$target_id" <<'PY' \
    || die 42 "$target_type #$target_id does not have a resolved split_and_reimplement marker"
import json
import sys
from pathlib import Path

marker = Path(sys.argv[1])
target_type, target_id = sys.argv[2], int(sys.argv[3])
data = json.loads(marker.read_text(encoding="utf-8"))
if data.get("status") != "resolved" or data.get("terminal_decision") != "split_and_reimplement":
    raise SystemExit(1)
marker_id = data.get("issue" if target_type == "issue" else "pr")
if marker_id != target_id:
    raise SystemExit(1)
PY

  if [ "$target_type" = "issue" ]; then
    target_json="$(gh issue view "$target_id" --repo "$REPO" --json state,labels 2>/dev/null || true)"
  else
    target_json="$(gh pr view "$target_id" --repo "$REPO" --json state,labels 2>/dev/null || true)"
  fi
  [ -n "$target_json" ] || die 1 "cannot read $target_type #$target_id"
  target_state="$(printf '%s' "$target_json" | json_field state 2>/dev/null || true)"
  if [ "$target_state" = "CLOSED" ] || [ "$target_state" = "MERGED" ]; then
    already_closed=1
  else
    [ "$target_state" = "OPEN" ] || die 42 "$target_type #$target_id is not open or terminal state=$target_state"
  fi

  live_labels="$(printf '%s' "$target_json" | python3 -c 'import json,sys; print("\n".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
  if [ "$already_closed" = "0" ] && printf '%s\n' "$live_labels" | grep -Eq '^(slot:[1-4]|status:in-progress)$'; then
    die 42 "$target_type #$target_id still has active ownership; release it canonically before split completion"
  fi

  parent_ref="#${target_id}"
  for child in "${child_issues[@]}"; do
    need_num child_issue "$child"
    [ "$target_type" != "issue" ] || [ "$child" != "$target_id" ] \
      || die 42 "split child cannot be the parent issue itself issue=$target_id"
    child_json="$(gh issue view "$child" --repo "$REPO" --json state,title,body,url 2>/dev/null || true)"
    [ -n "$child_json" ] || die 42 "cannot read split child issue #$child"
    CHILD_JSON="$child_json" PARENT_REF="$parent_ref" python3 - <<'PY' \
      || die 42 "child issue #$child does not reference parent $parent_ref"
import json
import os

data = json.loads(os.environ["CHILD_JSON"])
haystack = "\n".join((str(data.get("title") or ""), str(data.get("body") or "")))
if os.environ["PARENT_REF"] not in haystack:
    raise SystemExit(1)
PY
  done

  if [ "$already_closed" = "0" ]; then
    if [ "$target_type" = "issue" ]; then
      gh issue close "$target_id" --repo "$REPO" --reason completed >/dev/null \
        || die 1 "failed to close split parent issue #$target_id"
      target_json="$(gh issue view "$target_id" --repo "$REPO" --json state 2>/dev/null || true)"
    else
      gh pr close "$target_id" --repo "$REPO" >/dev/null \
        || die 1 "failed to close split parent PR #$target_id"
      target_json="$(gh pr view "$target_id" --repo "$REPO" --json state 2>/dev/null || true)"
    fi
    target_state="$(printf '%s' "$target_json" | json_field state 2>/dev/null || true)"
    [ "$target_state" = "CLOSED" ] \
      || die 1 "split parent $target_type #$target_id close did not converge state=${target_state:-unknown}"
  fi

  child_csv="$(IFS=,; printf '%s' "${child_issues[*]}")"
  python3 - "$marker" "$child_csv" <<'PY' \
    || die 1 "failed to persist split completion receipt marker=$marker"
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

marker = Path(sys.argv[1])
children = [int(value) for value in sys.argv[2].split(",") if value]
data = json.loads(marker.read_text(encoding="utf-8"))
now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
data["split_child_issues"] = children
data["split_parent_terminal_state"] = "closed"
data["split_completed_at"] = now
tmp = marker.with_name(f"{marker.name}.{os.getpid()}.tmp")
tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(marker)
PY

  resolve_target_obligations --kind rescope_split_execution --target-type "$target_type" --target-id "$target_id" "${target_args[@]}" --reason "split_children_recorded_parent_closed" --external-state "children=$child_csv marker=$marker"
  if [ "$target_type" = "pr" ]; then
    # A split-terminalized PR must not retain work-driving obligations that can
    # send the closed parent back through review or CTO rescue. Child execution
    # is represented by rescope_split_execution and the recorded child issues.
    resolve_target_obligations --kind pm_review_pending --target-type pr --target-id "$target_id" --pr "$target_id" --reason "split_parent_terminal" --external-state "children=$child_csv marker=$marker"
    resolve_target_obligations --kind cto_rescue --target-type pr --target-id "$target_id" --pr "$target_id" --reason "split_parent_terminal" --external-state "children=$child_csv marker=$marker"
  fi
  record_event --source pm-transition --event rescope_split_complete --target-type "$target_type" --target-id "$target_id" "${target_args[@]}" --payload "children=$child_csv" --payload "marker=$marker" --payload "state=closed"
  kanban_flag PM_TRANSITION "rescope_split_complete target_type=$target_type target_id=$target_id children=$child_csv state=closed marker=$marker"
  echo "PM_TRANSITION_OK command=rescope-split-complete target_type=$target_type target_id=$target_id children=$child_csv state=closed marker=$marker"
}


# FINAL_PATCH_DESCENDANT_RECOVERY_V1
cmd_rescope_final_patch_applied() {
  local pr="" issue="" authorized_head="" applied_head="" proof="" approval="" rationale="authorized final patch already applied on a verified descendant"
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      --issue) issue="${2:-}"; shift 2 ;;
      --authorized-head) authorized_head="${2:-}"; shift 2 ;;
      --applied-head) applied_head="${2:-}"; shift 2 ;;
      --proof) proof="${2:-}"; shift 2 ;;
      --approval) approval="${2:-}"; shift 2 ;;
      --rationale) rationale="${2:-}"; shift 2 ;;
      -h|--help) usage; return 0 ;;
      *) die 2 "unknown rescope-final-patch-applied arg $1" ;;
    esac
  done
  need_num pr "$pr"
  [ -z "$issue" ] || need_num issue "$issue"
  [[ "$authorized_head" =~ ^[0-9a-f]{40}$ ]] || die 2 "--authorized-head must be a full 40-character commit SHA"
  [ -f "$proof" ] || die 2 "--proof must be an existing exact binary diff file"
  [ -f "$approval" ] || die 2 "--approval must be an existing exact recovery approval JSON file"
  [ -f "$FINAL_PATCH_RECOVERY_TOOL" ] || die 70 "missing final patch recovery tool: $FINAL_PATCH_RECOVERY_TOOL"

  local pr_json state live_head branch live_labels marker recovery_present decision_first decision_at_live decision_at_applied recovery_receipt consume_blocker
  pr_json="$(pr_metadata_json "$pr" || true)"
  [ -n "$pr_json" ] || die 1 "cannot read PR #$pr"
  state="$(printf '%s' "$pr_json" | json_field state)"
  [ "$state" = "OPEN" ] || die 1 "PR #$pr is not open (state=$state)"
  live_head="$(printf '%s' "$pr_json" | json_field headRefOid)"
  branch="$(printf '%s' "$pr_json" | json_field headRefName)"
  live_labels="$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print(",".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
  [[ "$live_head" =~ ^[0-9a-f]{40}$ ]] || die 1 "cannot read PR #$pr exact head"
  applied_head="${applied_head:-$live_head}"
  [[ "$applied_head" =~ ^[0-9a-f]{40}$ ]] || die 2 "--applied-head must be a full 40-character commit SHA"
  [ "$live_head" != "$authorized_head" ] || die 42 "final_patch_not_applied pr=$pr head=$live_head; record rescope-decide final_verified_patch before dispatch"
  marker="/tmp/pm-rescope-pr-${pr}.json"
  [ -f "$marker" ] || die 42 "PR #$pr has no rescope marker"
  recovery_present="$(python3 - "$marker" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print("1" if data.get("final_patch_recovery") else "0")
PY
)" || die 42 "cannot inspect rescope marker for PR #$pr"
  decision_first="$(python3 - "$marker" "$authorized_head" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
expected_head = sys.argv[2]
ok = (
    data.get("status") == "resolved"
    and data.get("terminal_decision") == "final_verified_patch"
    and data.get("headRefOid") == expected_head
)
print("1" if ok else "0")
PY
)" || die 42 "cannot inspect resolved final-patch decision for PR #$pr"
  decision_at_live="$(python3 - "$marker" "$live_head" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
expected_head = sys.argv[2]
ok = (
    data.get("status") == "resolved"
    and data.get("terminal_decision") == "final_verified_patch"
    and data.get("headRefOid") == expected_head
)
print("1" if ok else "0")
PY
)" || die 42 "cannot inspect live-head final-patch decision for PR #$pr"
  decision_at_applied="$(python3 - "$marker" "$applied_head" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
expected_head = sys.argv[2]
ok = (
    data.get("status") == "resolved"
    and data.get("terminal_decision") == "final_verified_patch"
    and data.get("headRefOid") == expected_head
)
print("1" if ok else "0")
PY
)" || die 42 "cannot inspect applied-head final-patch decision for PR #$pr"
  consume_blocker="pm-blocked:cto"
  if [ "$recovery_present" != "1" ] && [ "$decision_first" != "1" ]; then
    if [ "$decision_at_live" = "1" ] || [ "$decision_at_applied" = "1" ]; then
      labels_include "$live_labels" "pm-state:blocked-rework" || die 42 "recovery_requires_blocked_rework pr=$pr labels=${live_labels:-none}"
      if printf '%s\n' "$live_labels" | tr ',' '\n' | grep '^pm-blocked:' | grep -qv '^pm-blocked:infra$'; then
        die 42 "recovery_refuses_unexpected_blocker pr=$pr labels=${live_labels:-none}"
      elif labels_include "$live_labels" "pm-blocked:infra"; then
        consume_blocker="pm-blocked:infra"
      else
        consume_blocker="none"
      fi
    else
      labels_include "$live_labels" "pm-blocked:cto" || die 42 "recovery_requires_cto_hold pr=$pr labels=${live_labels:-none}"
      if ! labels_include "$live_labels" "pm-state:rescope-required" \
        && ! labels_include "$live_labels" "pm-state:pm-review-pending"; then
        die 42 "recovery_requires_rescope_or_cto_review_cap pr=$pr labels=${live_labels:-none}"
      fi
    fi
  fi

  recovery_receipt="$(python3 "$FINAL_PATCH_RECOVERY_TOOL" \
    --repo "$FINAL_PATCH_RECOVERY_REPO" \
    --marker "$marker" \
    --pr "$pr" \
    --branch "$branch" \
    --authorized-head "$authorized_head" \
    --applied-head "$applied_head" \
    --live-head "$live_head" \
    --proof "$proof" \
    --approval "$approval" 2>&1)" || die 42 "$recovery_receipt"

  local decide_args=(--pr "$pr" --decision final_verified_patch --proof "$proof" --rationale "$rationale")
  [ -z "$issue" ] || decide_args+=(--issue "$issue")
  FINAL_PATCH_ALREADY_APPLIED=1 \
  FINAL_PATCH_AUTHORIZED_HEAD="$authorized_head" \
  FINAL_PATCH_VERIFIED_APPLIED_HEAD="$applied_head" \
  FINAL_PATCH_RECOVERY_APPROVAL="$approval" \
  FINAL_PATCH_RECOVERY_CONSUME_BLOCKER="$consume_blocker" \
  FINAL_PATCH_TRANSITION_COMMAND=rescope-final-patch-applied \
    cmd_rescope_decide "${decide_args[@]}"
}

cmd_rescope_decide() {
  local pr="" issue="" decision="" rationale="" proof="" child_plan="" question="" recommended_default="" rescope_contract="" verified_descendant_review=""
  local allow_escalate=0 replace_existing=0 no_scope_change=0
  local receipt_command="${FINAL_PATCH_TRANSITION_COMMAND:-rescope-decide}"
  local disposition_comment_id="" disposition_comment_url="" disposition_comment_file=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      --issue) issue="${2:-}"; shift 2 ;;
      --decision) decision="${2:-}"; shift 2 ;;
      --rationale) rationale="${2:-}"; shift 2 ;;
      --proof) proof="${2:-}"; shift 2 ;;
      --child-plan) child_plan="${2:-}"; shift 2 ;;
      --question) question="${2:-}"; shift 2 ;;
      --recommended-default) recommended_default="${2:-}"; shift 2 ;;
      --rescope-contract) rescope_contract="${2:-}"; shift 2 ;;
      --verified-descendant-review) verified_descendant_review="${2:-}"; shift 2 ;;
      --no-scope-change) no_scope_change=1; shift ;;
      --allow-escalate) allow_escalate=1; shift ;;
      --replace-existing) replace_existing=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown rescope-decide arg $1" ;;
    esac
  done
  need_num pr "$pr"
  [ -n "$decision" ] || die 2 "--decision is required"
  if [ -z "$issue" ]; then issue="$(issue_from_pr "$pr")"; fi
  [ -n "$issue" ] && need_num issue "$issue"

	  local pr_json state live_head branch live_labels marker out default_decision ledger prior_decision
	  local rescope_decision_snapshot="" rescope_ledger_path="" rescope_ledger_existed=0
	  pr_json="$(pr_metadata_json "$pr" || true)"
  [ -n "$pr_json" ] || die 1 "cannot read PR #$pr"
  state="$(printf '%s' "$pr_json" | json_field state)"
  [ "$state" = "OPEN" ] || die 1 "PR #$pr is not open (state=$state)"
	  live_head="$(printf '%s' "$pr_json" | json_field headRefOid)"
	  branch="$(printf '%s' "$pr_json" | json_field headRefName)"
	  live_labels="$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print(",".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
  marker="/tmp/pm-rescope-pr-${pr}.json"
  [ -f "$marker" ] || die 42 "PR #$pr has no rescope marker; run /Users/rajiv/.claude/scripts/pm-transition.sh rescope-pr --pr $pr first"
  validate_rescope_contract_arg "$decision" "$rescope_contract" "$no_scope_change"
  if [ "$decision" = "override_with_evidence" ] \
    && [ -f "$proof" ] \
    && grep -Eq '^PM_CLAUDE_PR_RESCUE_PACKET\r?$' "$proof"; then
    rescue_packet_authorizes_final_head "$proof" "$pr" "$live_head" \
      || die 42 "override_with_evidence_rescue_proof_head_mismatch pr=$pr live_head=$live_head proof=$proof"
  fi
  if [ -n "$rescope_contract" ] && [ -z "$issue" ]; then
    die 42 "rescope_contract_requires_linked_issue pr=$pr"
  fi
  if [ -n "$verified_descendant_review" ] && { [ "$decision" != "override_with_evidence" ] || [ "$replace_existing" != "1" ] || [ "$no_scope_change" != "1" ]; }; then
    die 42 "--verified-descendant-review requires override_with_evidence with --replace-existing and --no-scope-change"
  fi

  if [ -n "$rescope_contract" ]; then
    rescope_decision_snapshot="$(mktemp -d)" \
      || die 1 "failed to create PR rescope decision snapshot"
    cp "$marker" "$rescope_decision_snapshot/marker.json" \
      || { rm -r "$rescope_decision_snapshot"; die 1 "failed to snapshot PR rescope marker"; }
    rescope_ledger_path="$(python3 - "$marker" <<'PY'
import json
import sys
from pathlib import Path

marker = Path(sys.argv[1])
data = json.loads(marker.read_text(encoding="utf-8"))
print(str(data.get("ledger") or marker.with_suffix(".md")))
PY
)"
    if [ -f "$rescope_ledger_path" ]; then
      cp "$rescope_ledger_path" "$rescope_decision_snapshot/ledger.md" \
        || { rm -r "$rescope_decision_snapshot"; die 1 "failed to snapshot PR rescope ledger"; }
      rescope_ledger_existed=1
    fi
  fi

  out="$(python3 - "$marker" "$live_head" "$branch" "$FINAL_PATCH_RECOVERY_REPO" "$decision" "$rationale" "$proof" "$child_plan" "$question" "$recommended_default" "$allow_escalate" "$replace_existing" "$no_scope_change" "$verified_descendant_review" "$pr" <<'PY' 2>&1
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

marker = Path(sys.argv[1])
live_head = sys.argv[2]
branch = sys.argv[3]
repo = Path(sys.argv[4])
decision, rationale, proof, child_plan, question, recommended_default = sys.argv[5:11]
allow_escalate = sys.argv[11] == "1"
replace_existing = sys.argv[12] == "1"
no_scope_change = sys.argv[13] == "1"
verified_descendant_review = sys.argv[14]
pr = int(sys.argv[15])


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or "unknown"
        raise ValueError(f"git_{args[0]}_failed:{detail}")
    return result.stdout.strip()


def fetch_pr_and_main() -> tuple[str, str]:
    if not repo.is_dir():
        raise ValueError(f"repo_missing:{repo}")
    if subprocess.run(
        ["git", "check-ref-format", f"refs/heads/{branch}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).returncode != 0:
        raise ValueError(f"invalid_branch:{branch}")
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "fetch",
            "--quiet",
            "--no-tags",
            "origin",
            f"refs/heads/{branch}:refs/remotes/origin/{branch}",
            "refs/heads/main:refs/remotes/origin/main",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or "unknown"
        raise ValueError(f"git_fetch_failed:{detail}")
    fetched_head = git("rev-parse", f"refs/remotes/origin/{branch}")
    main_head = git("rev-parse", "refs/remotes/origin/main")
    return fetched_head, main_head


def require_commit(sha: str, label: str) -> None:
    object_type = git("cat-file", "-t", sha)
    if object_type != "commit":
        raise ValueError(f"{label}_not_commit:{sha}:{object_type or 'unknown'}")


def reanchor_verified_pure_main_merge(marker_head: str, live_head: str) -> dict[str, str]:
    for label, sha in (("marker", marker_head), ("live", live_head)):
        if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
            raise ValueError(f"{label}_head_not_full_sha:{sha or 'missing'}")
    fetched_head, main_head = fetch_pr_and_main()
    if fetched_head != live_head:
        raise ValueError(f"remote_head_mismatch:fetched={fetched_head}:live={live_head}")
    require_commit(marker_head, "marker_head")
    require_commit(live_head, "live_head")
    require_commit(main_head, "main_head")
    parents = git("rev-list", "--parents", "-n", "1", live_head).split()
    expected_parents = {marker_head, main_head}
    if len(parents) != 3 or parents[0] != live_head or set(parents[1:]) != expected_parents:
        raise ValueError(
            "not_verified_pure_main_merge:"
            f"parents={','.join(parents[1:]) or 'none'}:"
            f"expected={','.join(sorted(expected_parents))}"
        )
    expected_tree = git("merge-tree", "--write-tree", marker_head, main_head).splitlines()[0]
    live_tree = git("rev-parse", f"{live_head}^{{tree}}")
    if expected_tree != live_tree:
        raise ValueError(f"merge_tree_mismatch:expected={expected_tree}:live={live_tree}")
    return {
        "from_headRefOid": marker_head,
        "to_headRefOid": live_head,
        "main_headRefOid": main_head,
        "branch": branch,
        "merge_tree": live_tree,
    }


def reanchor_verified_authorized_descendant(
    marker_head: str, live_head: str
) -> dict[str, str]:
    if (
        decision != "override_with_evidence"
        or not replace_existing
        or not no_scope_change
    ):
        raise ValueError("authorized_descendant_contract_not_satisfied")
    if not verified_descendant_review:
        raise ValueError("verified_descendant_review_missing")
    expected_review = Path(
        f"/tmp/pm-claude-code-review-{pr}-{live_head}.md"
    )
    review = Path(verified_descendant_review)
    if review != expected_review or not review.is_file():
        raise ValueError(
            f"verified_descendant_review_path_mismatch:"
            f"expected={expected_review}:actual={review}"
        )
    text = review.read_text(encoding="utf-8", errors="replace")
    fields = {}
    for raw in text.splitlines():
        if ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        fields.setdefault(key.strip(), value.strip())
    if fields.get("PM_CLAUDE_REVIEW") != "PASS":
        raise ValueError("verified_descendant_review_not_pass")
    if fields.get("headRefOid") != live_head:
        raise ValueError(
            f"verified_descendant_review_head_mismatch:"
            f"review={fields.get('headRefOid') or 'missing'}:live={live_head}"
        )
    fetched_head, _main_head = fetch_pr_and_main()
    if fetched_head != live_head:
        raise ValueError(
            f"remote_head_mismatch:fetched={fetched_head}:live={live_head}"
        )
    require_commit(marker_head, "marker_head")
    require_commit(live_head, "live_head")
    result = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", marker_head, live_head],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError("live_head_not_descendant_of_marker_head")
    return {
        "from_headRefOid": marker_head,
        "to_headRefOid": live_head,
        "branch": branch,
        "review": str(review),
    }


valid = {
    "resume",
    "final_verified_patch",
    "split_and_reimplement",
    "override_with_evidence",
    "escalate_product_decision",
}
if decision not in valid:
    raise SystemExit(f"invalid_decision={decision}; expected one of {','.join(sorted(valid))}")
if recommended_default and recommended_default not in valid:
    raise SystemExit(f"invalid_recommended_default={recommended_default}")

data = json.loads(marker.read_text(encoding="utf-8"))
marker_head = str(data.get("headRefOid") or "")
pure_main_merge_reanchor = None
verified_descendant_reanchor = None
if live_head and marker_head and marker_head != live_head:
    try:
        pure_main_merge_reanchor = reanchor_verified_pure_main_merge(marker_head, live_head)
    except Exception as exc:
        try:
            verified_descendant_reanchor = reanchor_verified_authorized_descendant(
                marker_head, live_head
            )
        except Exception as descendant_exc:
            raise SystemExit(
                f"stale_marker_head marker={marker_head[:10]} live={live_head[:10]}; "
                f"pure_main_merge_reanchor_refused={exc}; "
                f"verified_descendant_reanchor_refused={descendant_exc}; "
                "do not create another CTO cycle. Record final_verified_patch before dispatch, "
                "or use rescope-final-patch-applied for a verified patch descendant"
            )
    data["headRefOid"] = live_head
    if pure_main_merge_reanchor:
        data["pure_main_merge_reanchor"] = pure_main_merge_reanchor
    if verified_descendant_reanchor:
        data["verified_descendant_reanchor"] = verified_descendant_reanchor

prior_decision = str(data.get("terminal_decision") or "")

classes = data.get("evidence_classes") or {}
class_names = set(classes)
rounds = int(data.get("round_count") or 0)
artifacts = int(data.get("source_artifact_count") or 0)
repeated = rounds >= 3 or artifacts >= 3 or len(class_names) >= 3

if "scope_or_migration_split" in class_names and repeated:
    matrix_default = "split_and_reimplement"
elif "runtime_control_point_drift" in class_names and repeated:
    matrix_default = "split_and_reimplement"
elif "proof_or_test_harness_gap" in class_names and rounds <= 2:
    matrix_default = "final_verified_patch"
elif "product_or_data_model_decision" in class_names and not (class_names & {"scope_or_migration_split", "runtime_control_point_drift"}):
    matrix_default = "escalate_product_decision"
else:
    matrix_default = "final_verified_patch"

if data.get("status") == "resolved" and prior_decision and not replace_existing:
    if prior_decision == decision:
        if decision == "escalate_product_decision" and matrix_default != "escalate_product_decision" and not allow_escalate:
            raise SystemExit(
                "existing_escalation_blocked_by_matrix "
                f"default={matrix_default} classes={','.join(sorted(class_names)) or 'none'} rounds={rounds} artifacts={artifacts}; "
                "supersede with --replace-existing --decision split_and_reimplement, or pass --allow-escalate with a concrete unresolved question"
            )
        print(f"decision={decision} default={matrix_default} marker={marker} ledger={data.get('ledger') or ''} prior={prior_decision} status=already_resolved")
        raise SystemExit(0)
    raise SystemExit(f"already_resolved prior_decision={prior_decision}; pass --replace-existing to supersede with {decision}")

def require(value: str, flag: str, why: str) -> None:
    if not str(value or "").strip():
        raise SystemExit(f"{decision}_requires_{flag}: {why}")

if decision == "resume":
    require(proof, "--proof", "cite the exact CTO resume decision and bounded rework packet")
    require(rationale, "--rationale", "explain why the existing scope remains valid and which in-scope rework is authorized")
elif decision == "split_and_reimplement":
    require(child_plan, "--child-plan", "name the child issue split before resolving the old PR")
elif decision == "final_verified_patch":
    require(proof, "--proof", "cite the PM Claude patch/instruction packet and git apply --check proof")
elif decision == "override_with_evidence":
    require(proof, "--proof", "cite code/artifact evidence that product/runtime risk is cleared")
    require(rationale, "--rationale", "explain why residual issues are non-gating follow-ups")
elif decision == "escalate_product_decision":
    require(question, "--question", "ask one concrete product/data-model/authority question")
    require(recommended_default, "--recommended-default", "state PM's default recommendation")
    require(rationale, "--rationale", "explain why PM cannot decide from directive/spec/code evidence")
    if matrix_default != "escalate_product_decision" and not allow_escalate:
        raise SystemExit(
            "escalation_blocked_by_matrix "
            f"default={matrix_default} classes={','.join(sorted(class_names)) or 'none'} rounds={rounds} artifacts={artifacts}; "
            "use the default or pass --allow-escalate with a concrete unresolved question"
        )

now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
if prior_decision:
    history = data.setdefault("decision_history", [])
    history.append({
        "terminal_decision": prior_decision,
        "terminal_decision_at": data.get("terminal_decision_at"),
        "rationale": data.get("terminal_decision_rationale"),
        "superseded_at": now,
    })

data["status"] = "resolved"
data["terminal_decision"] = decision
data["terminal_decision_at"] = now
data["decision_matrix_default"] = matrix_default
data["terminal_decision_rationale"] = rationale
if proof:
    data["terminal_decision_proof"] = proof
if child_plan:
    data["split_child_plan"] = child_plan
if question:
    data["product_decision_question"] = question
if recommended_default:
    data["recommended_default"] = recommended_default
if allow_escalate:
    data["allow_escalate_override"] = True

ledger = Path(data.get("ledger") or marker.with_suffix(".md"))
section = [
    "",
    "## Terminal Rescope Decision",
    "",
    f"- Decided: {now}",
    f"- Decision: `{decision}`",
    f"- Matrix default: `{matrix_default}`",
]
if prior_decision:
    section.append(f"- Supersedes prior decision: `{prior_decision}`")
if rationale:
    section.append(f"- Rationale: {rationale}")
if proof:
    section.append(f"- Proof: `{proof}`")
if pure_main_merge_reanchor:
    section.extend([
        "",
        "### Pure Main Merge Reanchor",
        "",
        f"- Previous marker head: `{pure_main_merge_reanchor['from_headRefOid']}`",
        f"- Live merge head: `{pure_main_merge_reanchor['to_headRefOid']}`",
        f"- Current main parent: `{pure_main_merge_reanchor['main_headRefOid']}`",
        f"- Verified merge tree: `{pure_main_merge_reanchor['merge_tree']}`",
    ])
if verified_descendant_reanchor:
    section.extend([
        "",
        "### Verified Authorized Descendant Reanchor",
        "",
        f"- Previous marker head: `{verified_descendant_reanchor['from_headRefOid']}`",
        f"- Live reviewed descendant: `{verified_descendant_reanchor['to_headRefOid']}`",
        f"- Exact-head PM review: `{verified_descendant_reanchor['review']}`",
    ])
if child_plan:
    section.extend(["", "### Child Plan", "", child_plan])
if question:
    section.extend(["", "### Escalation Question", "", question])
if recommended_default:
    section.append(f"- PM recommended default: `{recommended_default}`")

if ledger.exists():
    ledger.write_text(ledger.read_text(encoding="utf-8", errors="replace").rstrip() + "\n" + "\n".join(section).rstrip() + "\n", encoding="utf-8")
else:
    ledger.write_text("\n".join(section).lstrip() + "\n", encoding="utf-8")

tmp = marker.with_name(f"{marker.name}.{os.getpid()}.tmp")
tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(marker)
reanchor = "pure_main_merge" if pure_main_merge_reanchor else ("verified_authorized_descendant" if verified_descendant_reanchor else "none")
print(f"decision={decision} default={matrix_default} marker={marker} ledger={ledger} prior={prior_decision or 'none'} status=resolved reanchor={reanchor}")
PY
  )" || die 42 "$out"

  default_decision="$(printf '%s\n' "$out" | sed -n 's/.*default=\([^ ]*\).*/\1/p')"
  ledger="$(printf '%s\n' "$out" | sed -n 's/.*ledger=\([^ ]*\).*/\1/p')"
  prior_decision="$(printf '%s\n' "$out" | sed -n 's/.*prior=\([^ ]*\).*/\1/p')"

	  local rescope_receipt="rescope_status=NONE rescope_contract_digest=none issue_contract_updated=not_applicable pr_summary_updated=not_applicable"
	  if [ -n "$rescope_contract" ]; then
	    if ! rescope_receipt="$(persist_rescope_contract "$issue" "$pr" "$decision" "$rescope_contract" "$marker")"; then
	      cp "$rescope_decision_snapshot/marker.json" "$marker" \
	        || die 70 "rescope contract failed and PR marker rollback also failed marker=$marker"
	      if [ "$rescope_ledger_existed" = "1" ]; then
	        cp "$rescope_decision_snapshot/ledger.md" "$rescope_ledger_path" \
	          || die 70 "rescope contract failed and PR ledger rollback also failed ledger=$rescope_ledger_path"
	      else
	        rm -f "$rescope_ledger_path"
	      fi
	      rm -r "$rescope_decision_snapshot"
	      die 42 "rescope contract persistence failed for PR #$pr / issue #$issue: ${rescope_receipt:-unknown}"
	    fi
	    rm -r "$rescope_decision_snapshot"
	  elif [ "$no_scope_change" = "1" ]; then
	    python3 "$RESCOPE_CONTRACT_TOOL" no-scope-receipt --marker-file "$marker" --evidence "$proof" \
	      || die 42 "failed to record no-scope-change receipt for PR #$pr"
	    rescope_receipt="rescope_status=NONE scope_change_assertion=no rescope_contract_digest=none issue_contract_updated=not_applicable pr_summary_updated=not_applicable"
	  fi

	  disposition_comment_id="$(python3 - "$marker" <<'PY' 2>/dev/null || true
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(data.get("pr_comment_id") or "")
PY
)"
	  if [ -z "$disposition_comment_id" ]; then
	    disposition_comment_id="$(gh api "repos/${REPO}/issues/${pr}/comments" --paginate \
	      --jq ".[] | select(.body | contains(\"<!-- heydonna-cto-rescue pr=${pr} \")) | .id" \
	      2>/dev/null | tail -1 || true)"
	  fi
	  [ -n "$disposition_comment_id" ] \
	    || die 1 "durable CTO rescue comment missing for PR #$pr; refusing an untracked terminal disposition"
	  disposition_comment_file="/tmp/pm-cto-disposition-pr-${pr}-${live_head:0:12}.md"
	  {
	    printf '<!-- heydonna-cto-rescue pr=%s head=%s -->\n\n' "$pr" "$live_head"
	    printf '> **CTO RESCUE RESOLVED** — terminal decision `%s` recorded for exact head `%s`.\n\n' "$decision" "$live_head"
	    cat "$ledger"
	  } > "$disposition_comment_file"
	  gh api --method PATCH "repos/${REPO}/issues/comments/${disposition_comment_id}" \
	    -F "body=@${disposition_comment_file}" >/dev/null \
	    || die 1 "failed to update durable CTO rescue comment for PR #$pr decision=$decision"
	  disposition_comment_url="$(gh api "repos/${REPO}/issues/comments/${disposition_comment_id}" --jq .html_url 2>/dev/null || true)"
	  [ -n "$disposition_comment_url" ] \
	    || die 1 "updated CTO disposition comment returned no URL for PR #$pr"
	  python3 - "$marker" "$disposition_comment_id" "$disposition_comment_url" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
data["pr_comment_id"] = int(sys.argv[2])
data["pr_comment_url"] = sys.argv[3]
data["pr_comment_status"] = "terminal_decision_recorded"
data["pr_comment_updated_at"] = (
    datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
)
tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(path)
PY

	  local target_args=()
	  [ -n "$issue" ] && target_args+=(--issue "$issue")
	  local cto_forward_state="" cto_consumed=0 review_adopted=0 state_changed=0 blocker_removed=0 consume_blocker="pm-blocked:cto"
	  local review_context="" review_marker="" affected_test_plan="" review_meta="" alert_file="" next_action=""
	  case "$decision" in
	    resume) cto_forward_state="blocked-rework" ;;
	    final_verified_patch)
	      if [ "${FINAL_PATCH_ALREADY_APPLIED:-0}" = "1" ]; then
	        cto_forward_state="pm-review-pending"
	      else
	        cto_forward_state="blocked-rework"
	      fi
	      ;;
	    override_with_evidence) cto_forward_state="pm-review-pending" ;;
	  esac
	  if [ "${FINAL_PATCH_ALREADY_APPLIED:-0}" = "1" ]; then
	    consume_blocker="${FINAL_PATCH_RECOVERY_CONSUME_BLOCKER:-pm-blocked:cto}"
	    case "$consume_blocker" in
	      none|pm-blocked:cto|pm-blocked:infra) ;;
	      *) die 42 "invalid final-patch recovery blocker: $consume_blocker" ;;
	    esac
	  fi
	  if [ -n "$cto_forward_state" ]; then
	    # The exact-head resolved marker above is the authority to leave CTO hold.
	    # Change PM state first, then remove the blocker: a label-removal failure
	    # remains fail-closed instead of exposing an unconsumed CTO disposition.
	    # Exact retries are idempotent: do not toggle an already-correct state or
	    # remove an already-absent blocker, because both create noisy skipped
	    # pull_request workflows and can turn a missing Slack ACK into new churn.
	    if ! labels_include "$live_labels" "pm-state:$cto_forward_state"; then
	      bash "$PM_STATE" "$pr" "$cto_forward_state" \
	        || die 1 "failed to move PR #$pr forward after CTO decision $decision"
	      state_changed=1
	    fi
	    if [ "$consume_blocker" != "none" ] && labels_include "$live_labels" "$consume_blocker"; then
	      gh pr edit "$pr" --repo "$REPO" --remove-label "$consume_blocker" >/dev/null \
	        || die 1 "failed to consume $consume_blocker for PR #$pr after decision $decision"
	      blocker_removed=1
	    fi
	    if [ "$state_changed" = "1" ] || [ "$blocker_removed" = "1" ]; then
	      cto_consumed=1
	    else
	      cto_consumed=already
	    fi
	    if [ "$cto_consumed" = "1" ]; then
	      resolve_pr_obligation_kinds "$pr" "$issue" "cto_disposition_consumed" "decision=$decision marker=$marker state=$cto_forward_state" cto_rescue
	      record_event --source pm-transition --event cto_disposition_consumed --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --head-sha "$live_head" --payload "decision=$decision" --payload "state=$cto_forward_state" --payload "marker=$marker"
	    else
	      record_event --source pm-transition --event cto_disposition_ack_replayed --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --head-sha "$live_head" --payload "decision=$decision" --payload "state=$cto_forward_state" --payload "marker=$marker" --dedupe
	    fi
	  fi
	  if [ "$decision" = "resume" ]; then
	    next_action="Resolve branch/head/epoch/handoff and use claim_slot for the recorded bounded rework; preserve exact-head review after the final rework head."
	  elif [ "$decision" = "override_with_evidence" ]; then
	    review_context="$(adopt_current_head_review_after_override "$pr" "$issue" "$live_head" "$branch" 2>/dev/null || true)"
	    if [ -n "$review_context" ]; then
	      IFS="$(printf '\t')" read -r review_marker affected_test_plan review_meta <<< "$review_context"
	      review_adopted=1
      next_action="Run /Users/rajiv/.claude/scripts/pm-transition.sh pm-review-done --pr $pr at exact head ${live_head:0:12}. Affected-test proof is retired; the current-head Phase-A PASS marker alone binds CI start. Do not start another PM review."
	      record_event --source pm-transition --event current_head_review_adopted_after_cto_override --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --head-sha "$live_head" --payload "marker=$review_marker" --payload "plan=$affected_test_plan" --payload "meta=$review_meta" --dedupe
	    else
	      next_action="No valid exact-head Phase A approval plus affected-test plan was found. Run /Users/rajiv/.claude/scripts/pm-transition.sh pm-review --pr $pr --scope phase-a before pm-review-done."
	    fi
	  elif [ "$decision" = "final_verified_patch" ]; then
	    if [ "${FINAL_PATCH_ALREADY_APPLIED:-0}" = "1" ]; then
	      next_action="Validate the already-applied authorized patch on exact head $live_head, then run the canonical exact-head PM review/affected-test transition into label-gated CI. Do not reapply the patch or create another CTO cycle."
	    else
	      next_action="Deliver the exact-head verified patch packet; do not start a generic review or rework loop."
	    fi
	  elif [ "$decision" = "split_and_reimplement" ]; then
	    next_action="Execute the recorded child split from current main; do not reassign the old PR."
	  else
	    next_action="Resolve the recorded product decision before any further implementation."
	  fi
	  if [ "${FINAL_PATCH_ALREADY_APPLIED:-0}" = "1" ]; then
	    resolve_pr_obligation_kinds "$pr" "$issue" "authorized_patch_applied" "decision=$decision marker=$marker head=$live_head" review_loop_rescope rescope_final_patch pm_review_pending blocked_rework rework_slot_idle
	  else
	    resolve_pr_obligation_kinds "$pr" "$issue" "rescope_decision_recorded" "decision=$decision marker=$marker" review_loop_rescope rescope_final_patch rescope_override rescope_product_escalation
	  fi
	  case "$decision" in
	    resume)
	      upsert_obligation --kind blocked_rework --severity high --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --owner pm --horizon hourly --next-review-at "$(utc_plus_minutes 30)" --title "PR #$pr CTO resume decision recorded" --action "Resolve the current branch/head/expected epoch/repository/handoff and assign the recorded bounded rework through the complete claim_slot tuple before fresh Todo work. Preserve the existing issue/PR scope and require exact-head review on the final rework head." --blocker "cto_resume_rework" --evidence "proof=${proof:-none} marker=$marker"
	      ;;
	    split_and_reimplement)
	      upsert_obligation --kind rescope_split_execution --severity high --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --owner pm --horizon hourly --next-review-at "$(utc_plus_minutes 30)" --title "PR #$pr split-and-reimplement decision recorded" --action "Close or park the old PR, file child issues from the rescope child plan, and dispatch each child from current main/current base. Do not reassign the broad old PR." --blocker "rescope_split_execution" --evidence "marker=$marker ledger=${ledger:-unknown}"
	      ;;
	    final_verified_patch)
	      if [ "${FINAL_PATCH_ALREADY_APPLIED:-0}" = "1" ]; then
	        upsert_obligation --kind rescope_final_patch_validation --severity high --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --owner pm --horizon hourly --next-review-at "$(utc_plus_minutes 30)" --title "PR #$pr authorized final patch requires exact-head validation" --action "Validate exact live head $live_head as a guarded descendant of approved patch ${FINAL_PATCH_VERIFIED_APPLIED_HEAD:-unknown}, then run canonical current-head PM review/affected tests and label-gated CI. Do not reapply the patch or create another CTO cycle." --blocker "rescope_final_patch_validation" --evidence "authorized_head=${FINAL_PATCH_AUTHORIZED_HEAD:-unknown}" --evidence "approved_patch_head=${FINAL_PATCH_VERIFIED_APPLIED_HEAD:-unknown}" --evidence "live_head=$live_head" --evidence "proof=${proof:-none}" --evidence "approval=${FINAL_PATCH_RECOVERY_APPROVAL:-none}" --evidence "marker=$marker"
	      else
	        upsert_obligation --kind rescope_final_patch --severity high --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --owner pm --horizon hourly --next-review-at "$(utc_plus_minutes 30)" --title "PR #$pr final verified patch decision recorded" --action "Deliver the verified PM Claude patch/instruction packet to the current owner and require local proof before another review." --blocker "rescope_final_patch" --evidence "proof=${proof:-none} marker=$marker"
	      fi
	      ;;
	    override_with_evidence)
	      upsert_obligation --kind rescope_override --severity high --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --owner pm --horizon hourly --next-review-at "$(utc_plus_minutes 30)" --title "PR #$pr override-with-evidence decision recorded" --action "Link bounded follow-ups for non-gating residue, then run readiness/cleanup according to the PR state." --blocker "rescope_override" --evidence "proof=${proof:-none} marker=$marker"
	      ;;
	    escalate_product_decision)
	      upsert_obligation --kind rescope_product_escalation --severity high --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --owner pm --horizon hourly --next-review-at "$(utc_plus_minutes 30)" --title "PR #$pr product/data-model escalation decision recorded" --action "Ask the exact recorded product/data-model question and include PM's recommended default. Do not present broad split-vs-reassign choices." --blocker "rescope_product_decision" --evidence "question=${question:-none} recommended_default=${recommended_default:-none} marker=$marker"
	      ;;
	  esac
	  alert_file="/tmp/pm-cto-disposition-consumed-pr-${pr}-${live_head:0:12}.md"
	  {
	    printf '*CTO disposition consumed — PR #%s / issue #%s*\n' "$pr" "${issue:-unknown}"
	    printf -- '- Exact head: `%s` on `%s`\n' "$live_head" "$branch"
	    printf -- '- Decision: `%s`\n' "$decision"
	    printf -- '- Forward state: `%s`\n' "${cto_forward_state:-decision-recorded}"
	    printf -- '- Transition ACK: `PM_TRANSITION_OK command=%s decision=%s cto_consumed=%s`\n' "$receipt_command" "$decision" "$cto_consumed"
	    printf -- '- Proof: `%s`\n' "${proof:-$marker}"
	    if [ "$review_adopted" = "1" ]; then
	      printf -- '- Existing exact-head Phase A approval: adopted (`%s`)\n' "$review_marker"
	      printf -- '- Affected-test plan: `%s`\n' "$affected_test_plan"
	      printf -- '- New review required: `false`\n'
	    fi
	    printf '\nNext: %s\n' "$next_action"
	  } > "$alert_file"
	  transition_alert --event decision-required --pr "$pr" --issue "${issue:-}" --state "${cto_forward_state:-decision-recorded}" --head "$live_head" --branch "$branch" --reason "decision-consumed:$decision" --proof "${proof:-$marker}" --message-file "$alert_file"
	  record_event --source pm-transition --event rescope_decide --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --head-sha "$live_head" --payload "decision=$decision" --payload "matrix_default=${default_decision:-unknown}" --payload "prior=${prior_decision:-none}" --payload "marker=$marker" --payload "ledger=${ledger:-unknown}" --payload "comment_url=$disposition_comment_url" --payload "branch=${branch:-unknown}" --payload "$rescope_receipt"
	  kanban_flag PM_TRANSITION "rescope_decide pr=$pr issue=${issue:-unknown} decision=$decision default=${default_decision:-unknown} marker=$marker"
	  echo "PM_TRANSITION_OK command=$receipt_command pr=$pr issue=${issue:-unknown} decision=$decision default=${default_decision:-unknown} prior=${prior_decision:-none} cto_consumed=$cto_consumed forward_state=${cto_forward_state:-none} review_adopted=$review_adopted review_marker=${review_marker:-none} affected_test_plan=${affected_test_plan:-none} marker=$marker ledger=${ledger:-unknown} comment_url=$disposition_comment_url authorized_head=${FINAL_PATCH_AUTHORIZED_HEAD:-none} approved_patch_head=${FINAL_PATCH_VERIFIED_APPLIED_HEAD:-$live_head} live_head=$live_head $rescope_receipt next=\"$next_action\""
}


select_ci_rerun_candidate_from_json() {
  local runs_json="$1" head="$2" requested_run="${3:-}"
  python3 - "$runs_json" "$head" "$requested_run" <<'PY'
import json
import sys

runs = json.loads(sys.argv[1] or "[]")
head = sys.argv[2]
requested_run = sys.argv[3]
required_workflows = {"CI", "E2E Smoke Tests"}
bad_conclusions = {"failure", "cancelled", "timed_out", "action_required"}

current = [
    run
    for run in runs
    if str(run.get("headSha") or "") == head
    and str(run.get("event") or "") == "pull_request"
    and str(run.get("workflowName") or "") in required_workflows
]

workflow = ""
selection = "auto"
if requested_run:
    requested = next(
        (run for run in current if str(run.get("databaseId") or "") == requested_run),
        None,
    )
    if requested is None:
        raise SystemExit(1)
    status = str(requested.get("status") or "").lower()
    conclusion = str(requested.get("conclusion") or "").lower()
    if status == "completed" and conclusion in bad_conclusions:
        print(f"{requested_run}\t{requested.get('workflowName')}\t{conclusion}\tprovided")
        raise SystemExit(0)
    if status != "completed" or conclusion != "skipped":
        raise SystemExit(1)
    workflow = str(requested.get("workflowName") or "")
    selection = "skipped_fallback"

candidates = [
    run
    for run in current
    if (not workflow or str(run.get("workflowName") or "") == workflow)
    and str(run.get("status") or "").lower() == "completed"
    and str(run.get("conclusion") or "").lower() in bad_conclusions
]
if not candidates:
    raise SystemExit(1)
candidates.sort(
    key=lambda run: (
        str(run.get("createdAt") or ""),
        int(run.get("databaseId") or 0),
    ),
    reverse=True,
)
selected = candidates[0]
print(
    f"{selected.get('databaseId')}\t{selected.get('workflowName')}\t"
    f"{selected.get('conclusion')}\t{selection}"
)
PY
}

cmd_ci_local_preflight_pass() {
  local pr="" proof="" failed_run="" ci_class="current-head-failure" rebind_checkout=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      --proof) proof="${2:-}"; shift 2 ;;
      --failed-run) failed_run="${2:-}"; shift 2 ;;
      --ci-class) ci_class="${2:-}"; shift 2 ;;
      --rebind-checkout) rebind_checkout="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown ci-local-preflight-pass arg $1" ;;
    esac
  done
  need_num pr "$pr"
  [ -z "$failed_run" ] || need_num failed-run "$failed_run"
  # Sealed local-preflight envelopes are retired (Rajiv 1786812200.371389):
  # --proof is accepted only as an optional diagnostic and is never a
  # CI re-arm requirement. The raw slot local-repro outcome/classification
  # is authoritative.

  local pr_json live_head issue branch target runs_json rerun_selection rerun_workflow rerun_conclusion selection_source
  pr_json="$(gh pr view "$pr" --repo "$REPO" --json headRefOid,headRefName 2>/dev/null || true)"
  live_head="$(printf '%s' "$pr_json" | json_field headRefOid 2>/dev/null || true)"
  branch="$(printf '%s' "$pr_json" | json_field headRefName 2>/dev/null || true)"
  [ -n "$live_head" ] || die 1 "cannot read PR #$pr head"
  issue="$(issue_from_pr "$pr")"
  target="${proof:-none}"

  runs_json="$(gh run list --repo "$REPO" --branch "$branch" --limit 100 --json databaseId,headSha,event,status,conclusion,workflowName,createdAt 2>/dev/null || true)"
  [ -n "$runs_json" ] || die 1 "cannot read current-head CI/E2E runs for PR #$pr head=$live_head"
  rerun_selection="$(select_ci_rerun_candidate_from_json "$runs_json" "$live_head" "$failed_run" 2>/dev/null || true)"
  if [ -z "$rerun_selection" ] && [ -n "$failed_run" ]; then
    # Post-capture local repro (Rajiv thread 1786713760.734709 ts
    # 1786714368.757699): the supplied --failed-run is the exact-head remote
    # capture run, not a failed CI/E2E run. Verify the capture identity
    # (displayTitle remote-capture-pr-<pr>-head-<head>, exact head, terminal
    # success) before recording the raw local-repro classification and
    # handing the CTO-owned wave obligation. Any other run shape fails
    # closed below.
    local capture_meta capture_title capture_head capture_status capture_conclusion
    capture_meta="$(gh run view "$failed_run" --repo "$REPO" --json displayTitle,status,conclusion,headSha 2>/dev/null || true)"
    capture_title="$(printf '%s' "$capture_meta" | json_field displayTitle 2>/dev/null || true)"
    capture_head="$(printf '%s' "$capture_meta" | json_field headSha 2>/dev/null || true)"
    capture_status="$(printf '%s' "$capture_meta" | json_field status 2>/dev/null || true)"
    capture_conclusion="$(printf '%s' "$capture_meta" | json_field conclusion 2>/dev/null || true)"
    if [ "$capture_title" = "remote-capture-pr-${pr}-head-${live_head}" ] \
      && [ "$capture_head" = "$live_head" ] \
      && [ "$capture_status" = "completed" ] \
      && [ "$capture_conclusion" = "success" ]; then
      rerun_workflow="CI"
      rerun_conclusion="success"
      selection_source="post-capture-local-repro"
      rerun_selection="${failed_run}	${rerun_workflow}	${rerun_conclusion}	${selection_source}"
      # The post-capture class is authoritative: any caller-supplied class
      # is superseded so the recorded classification is unambiguously bound
      # to the post-capture local-repro path.
      ci_class="post-capture-local-repro"
    fi
  fi
  [ -n "$rerun_selection" ] || die 1 "no exact-head rerunnable CI/E2E or capture run for PR #$pr head=$live_head requested_run=${failed_run:-none}"
  IFS=$'\t' read -r failed_run rerun_workflow rerun_conclusion selection_source <<<"$rerun_selection"
  [[ "$failed_run" =~ ^[0-9]+$ ]] || die 1 "invalid selected CI/E2E rerun for PR #$pr head=$live_head"

	  local target_args=()
	  [ -n "$issue" ] && target_args+=(--issue "$issue")
	  resolve_pr_obligation_kinds "$pr" "$issue" "local_preflight_passed" "ci_class=$ci_class failed_run=$failed_run" ci_local_preflight blocked_rework
  if [ "$selection_source" = "post-capture-local-repro" ]; then
    upsert_obligation --kind ci_rerun_after_preflight --severity high --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --owner cto --horizon hourly --title "PR #$pr post-capture local repro passed; CTO label-gated CI/E2E wave required" --action "Slot local repro/classification recorded at exact head $live_head (raw terminal, no sealed envelope; capture changed external fixtures, prior green is history). CTO triggers one fresh exact-head label-gated CI+E2E wave. PM must not dispatch a workflow or toggle CI labels." --blocker "cto_ci_wave_required" --evidence "ci_class=$ci_class" --evidence "capture_run=$failed_run" --evidence "workflow=$rerun_workflow" --evidence "conclusion=$rerun_conclusion" --evidence "selection_source=$selection_source" --evidence "next_actor=cto"
  else
    upsert_obligation --kind ci_rerun_after_preflight --severity high --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --owner cto --horizon hourly --title "PR #$pr local repro classification recorded; CTO label-gated CI/E2E wave required" --action "Slot local repro/classification recorded at exact head $live_head (raw terminal, no sealed envelope). CTO triggers one exact-head label-gated CI/E2E wave. PM must not dispatch a workflow or toggle CI labels." --blocker "cto_ci_wave_required" --evidence "ci_class=$ci_class" --evidence "failed_run=$failed_run" --evidence "workflow=$rerun_workflow" --evidence "conclusion=$rerun_conclusion" --evidence "selection_source=$selection_source" --evidence "next_actor=cto"
  fi
  record_event --source pm-transition --event ci_local_preflight_pass --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --head-sha "$live_head" --payload "proof=${target:-none}" --payload "ci_class=$ci_class" --payload "failed_run=$failed_run" --payload "workflow=$rerun_workflow" --payload "conclusion=$rerun_conclusion" --payload "selection_source=$selection_source" --payload "branch=${branch:-unknown}" --payload "next_actor=cto" --dedupe
  kanban_flag PM_TRANSITION "ci_local_preflight_pass pr=$pr issue=${issue:-unknown} proof=${target:-none} ci_class=$ci_class"
  echo "PM_TRANSITION_OK command=ci-local-preflight-pass pr=$pr issue=${issue:-unknown} head=${live_head:0:8} proof=${target:-none} ci_class=$ci_class failed_run=$failed_run workflow=$rerun_workflow conclusion=$rerun_conclusion selection_source=$selection_source"
}

remote_capture_only_enabled() {
  case "${REMOTE_CAPTURE_ONLY:-1}" in 0|false|disabled) return 1 ;; *) return 0 ;; esac
}

# The pm-review precheck never owns local capture proof. Remote capture is the
# default and authoritative lane; local capture is diagnostic-only
# (capture-local-required with a named infrastructure defect) and never
# satisfies capture readiness or serves as a fallback after remote red.
pm_review_done_requires_local_capture_proof() {
  return 1
}

capture_run_head_from_title() {
  local pr="$1" title="$2" prefix="remote-capture-pr-${1}-head-" head
  prefix="remote-capture-pr-${pr}-head-"
  case "$title" in
    "${prefix}"*)
      head="${title#"$prefix"}"
      [[ "$head" =~ ^[0-9a-f]{40}$ ]] || return 1
      printf '%s\n' "$head"
      ;;
    *) return 1 ;;
  esac
}

cmd_capture_control_plane_repaired() {
  local pr="" run_id="" repair_commit=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      --run) run_id="${2:-}"; shift 2 ;;
      --repair-commit) repair_commit="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown capture-control-plane-repaired arg $1" ;;
    esac
  done
  need_num pr "$pr"
  need_num run "$run_id"
  [[ "$repair_commit" =~ ^[0-9a-f]{40}$ ]] || die 2 "repair commit must be a full SHA"

  local pr_json live_head branch run_json run_head status attempt work artifact
  local audit_receipt recovery_dir recovery_receipt budget_reconciliation run_updated repair_time issue target_args=()
  pr_json="$(gh pr view "$pr" --repo "$REPO" --json state,headRefOid,headRefName 2>/dev/null || true)"
  [ "$(printf '%s' "$pr_json" | json_field state 2>/dev/null || true)" = "OPEN" ] || die 1 "PR #$pr is not open"
  live_head="$(printf '%s' "$pr_json" | json_field headRefOid 2>/dev/null || true)"
  branch="$(printf '%s' "$pr_json" | json_field headRefName 2>/dev/null || true)"
  run_json="$(gh run view "$run_id" --repo "$REPO" --json displayTitle,status,conclusion,attempt,updatedAt,headSha,jobs 2>/dev/null || true)"
  [ -n "$run_json" ] || die 1 "cannot read capture run $run_id"
  run_head="$(capture_run_head_from_title "$pr" "$(printf '%s' "$run_json" | json_field displayTitle)" 2>/dev/null || true)"
  [ "$run_head" = "$live_head" ] || die 1 "capture run tuple mismatch run=$run_id expected=$live_head actual=${run_head:-unknown}"
  status="$(printf '%s' "$run_json" | json_field status 2>/dev/null || true)"
  [ "$status" = "completed" ] || die 1 "capture run $run_id is not completed"
  attempt="$(printf '%s' "$run_json" | json_field attempt 2>/dev/null || true)"; [ -n "$attempt" ] || attempt=1

  work="$(mktemp -d)"
  artifact="$work/capture-attempt.json"
  printf '%s\n' "$run_json" >"$work/run.json"
  gh run view "$run_id" --repo "$REPO" --log >"$work/run.log" 2>/dev/null \
    || { rm -rf "$work"; die 1 "capture run log missing run=$run_id"; }
  modal volume get --env main --force ci-runner-cache "/run-${run_id}-${attempt}/capture-attempt.json" "$artifact" >/dev/null 2>&1 \
    || { rm -rf "$work"; die 1 "capture attempt evidence missing run=$run_id attempt=$attempt"; }
  git -C "$FINAL_PATCH_RECOVERY_REPO" fetch origin main --quiet
  recovery_dir="/tmp/pm-capture-dependency-recovery/pr-${pr}-${live_head}"
  audit_receipt="$recovery_dir/control-plane-repair.json"
  python3 "$FINAL_PATCH_RECOVERY_REPO/scripts/ci/remote-capture-control-plane-recovery.py" \
    --attempt-proof "$artifact" --run-json "$work/run.json" --run-log "$work/run.log" --pr "$pr" \
    --head "$live_head" --branch "$branch" --run "$run_id" \
    --repair-commit "$repair_commit" --repo-root "$FINAL_PATCH_RECOVERY_REPO" --out "$audit_receipt" \
    || { rm -rf "$work"; die 1 "control-plane capture recovery validation failed"; }

  run_updated="$(printf '%s' "$run_json" | json_field updatedAt)"
  repair_time="$(git -C "$FINAL_PATCH_RECOVERY_REPO" show -s --format=%cI "$repair_commit")"
  recovery_receipt="$recovery_dir/recovery.json"
  python3 - "$recovery_receipt" "$audit_receipt" "$pr" "$live_head" "$run_id" "$run_updated" "$repair_time" <<'PY' \
    || { rm -rf "$work"; die 1 "failed to write compatible capture recovery receipt"; }
import json
import sys
from datetime import datetime
from pathlib import Path

out, audit_path, pr, head, run_id, run_updated, repair_time = sys.argv[1:]
failed = datetime.fromisoformat(run_updated.replace("Z", "+00:00"))
repaired = datetime.fromisoformat(repair_time.replace("Z", "+00:00"))
if repaired <= failed:
    raise SystemExit("repair commit predates failed capture")
audit = json.loads(Path(audit_path).read_text(encoding="utf-8"))
Path(out).write_text(json.dumps({
    "schema_version": 1,
    "classification": "dependency_resolved",
    "recovery_kind": "control_plane_repaired",
    "pr": int(pr),
    "head_sha": head,
    "failed_run": int(run_id),
    "run_updated_at": run_updated,
    "dependency_merged_at": repair_time,
    "repair_commit": audit["repair_commit"],
    "audit_receipt": audit_path,
}, indent=2) + "\n", encoding="utf-8")
PY
  budget_reconciliation="/tmp/remote-capture-budget-reconciliation-${pr}.json"
  python3 - "$budget_reconciliation" "$pr" "$run_id" "$recovery_receipt" "$audit_receipt" <<'PY' \
    || { rm -rf "$work"; die 1 "failed to write capture budget reconciliation"; }
import json
import sys
from pathlib import Path

path, pr, run_id, authority, evidence = (
    Path(sys.argv[1]),
    int(sys.argv[2]),
    int(sys.argv[3]),
    sys.argv[4],
    sys.argv[5],
)
payload = {"pr": pr, "exclusions": []}
if path.exists():
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = {}
    if existing.get("pr") == pr and isinstance(existing.get("exclusions"), list):
        payload = existing
payload["exclusions"] = [
    entry
    for entry in payload["exclusions"]
    if isinstance(entry, dict) and entry.get("run_id") != run_id
]
payload["exclusions"].append({
    "run_id": run_id,
    "classification": "control_plane_repaired",
    "authority": authority,
    "evidence": evidence,
})
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
tmp.replace(path)
PY
  issue="$(issue_from_pr "$pr")"
  [ -n "$issue" ] && target_args+=(--issue "$issue")
  record_event --source pm-transition --event capture_control_plane_repaired --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --head-sha "$live_head" --payload "run=$run_id" --payload "repair_commit=$repair_commit" --payload "audit_receipt=$audit_receipt" --payload "recovery_receipt=$recovery_receipt" --payload "budget_reconciliation=$budget_reconciliation" --dedupe
  rm -rf "$work"
  echo "PM_TRANSITION_OK command=capture-control-plane-repaired pr=$pr head=${live_head:0:10} run=$run_id repair_commit=$repair_commit recovery_receipt=$recovery_receipt budget_reconciliation=$budget_reconciliation next=\"capture-remote-dispatch --pr $pr --retry-run $run_id\""
}

# Sanctioned key-capture tuple for the recorded main-shape fixture-miss proof
# (incident control-plane:capture-main-shape-gate-gap:7075, obligation 10600;
# CTO dispositions 1786023151 / 1785988314.547779). The main-carrier admission
# accepts exactly this recorded proof (recorded miss runs
# 30972528228/30972708211/31103926871); any other key or runs list fails
# closed. Kept byte-identical with the request-budgeted-remote-capture.sh
# mirror.
KEY_CAPTURE_SANCTIONED_KEY="fixtures/llm/v2/gemini_api_key/generativelanguage.googleapis.com/gemini-3.1-flash-lite.generate/6e47e7293dac"
KEY_CAPTURE_SANCTIONED_MISS_RUNS="30972528228,30972708211,31103926871"
# #7075 format-stage VERTEX family key (6e47e7293dac under the gemini_vertex
# prefix, format-stage gemini-3.1-flash-lite): stage-matched admission to the
# sanctioned key-capture set (incident
# control-plane:capture-admission-exact-head-fixture-miss:7092, CTO audit
# 1786204836.198409 item 3). The recorded family miss runs above are shared by
# both stage keys; any other key or runs list fails closed. Kept byte-identical
# with the wrapper mirror.
KEY_CAPTURE_SANCTIONED_VERTEX_KEY="fixtures/llm/v2/gemini_vertex/aiplatform.googleapis.com/gemini-3.1-flash-lite.generate/6e47e7293dac"

cmd_capture_remote_dispatch() {
  remote_capture_only_enabled || die 12 "REMOTE_CAPTURE_ONLY is disabled; remote-only capture has not been rolled out"
  local pr="" issue="" expect_head="" retry_run="" source_e2e_run="" descendant_proof="" profile=""
  local key="" carrier="" key_capture=0 force_key_capture=0 key_miss_proof=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      --issue) issue="${2:-}"; shift 2 ;;
      --head) expect_head="${2:-}"; shift 2 ;;
      --retry-run) retry_run="${2:-}"; shift 2 ;;
      --source-e2e-run) source_e2e_run="${2:-}"; shift 2 ;;
      --descendant-proof-json) descendant_proof="${2:-}"; shift 2 ;;
      --profile) profile="${2:-}"; shift 2 ;;
      --key) key="${2:-}"; shift 2 ;;
      --carrier) carrier="${2:-}"; shift 2 ;;
      --force-key-capture) force_key_capture=1; shift ;;
      --key-miss-proof) key_miss_proof="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown capture-remote-dispatch arg $1" ;;
    esac
  done
  # --profile selects the workflow capture_profile input in PR mode
  # (full|auto-process-only|fresh-upload-only|acr-template-only|whisperx-stt-only|
  # manual-format-only) and is forwarded to the wrapper as --capture-profile.
  # Fail closed on any other value; when absent the dispatch is byte-identical
  # to today (workflow default `full`).
  case "$profile" in
    "") ;;
    full|auto-process-only|fresh-upload-only|acr-template-only|whisperx-stt-only|manual-format-only) ;;
    *) die 2 "invalid capture profile: $profile" ;;
  esac
  if [ -n "$key" ] || [ -n "$carrier" ] || [ "$force_key_capture" = "1" ] || [ -n "$key_miss_proof" ]; then
    key_capture=1
  fi
  if [ "$key_capture" -eq 1 ]; then
    # Key-capture mode (CTO decision thread 1786032623.694829): a sanctioned
    # fixture key is captured from a MAIN head. Admission is either the
    # explicit --force-key-capture carve or the recorded main-shape
    # fixture-miss proof (--key-miss-proof) for the sanctioned key (incident
    # control-plane:capture-main-shape-gate-gap:7075): the exact recorded miss
    # runs for the exact sanctioned key, no PR carrier. The sanctioned set is
    # stage-bound (incident
    # control-plane:capture-admission-exact-head-fixture-miss:7092): the
    # api-key family key and the #7075 format-stage VERTEX family key share
    # the recorded family miss runs; the wrapper dispatches the stage-matched
    # lane. Fail closed on any malformed or un-granted tuple; the wrapper is
    # the authoritative writer and enforces the one-shot carve expiry.
    # PR-carrier admission and the fail-closed unknown-carrier behavior are
    # unchanged.
    if [ "$force_key_capture" = "1" ]; then
      :
    elif [ -n "$key_miss_proof" ]; then
      # The sanctioned key-capture set is stage-bound: the api-key fixture key
      # (family 6e47e7293dac) and the #7075 format-stage VERTEX key (family
      # 6e47e7293dac) share the recorded family miss runs; any other key or
      # runs list fails closed.
      { [ "$key" = "$KEY_CAPTURE_SANCTIONED_KEY" ] || [ "$key" = "$KEY_CAPTURE_SANCTIONED_VERTEX_KEY" ]; } \
        || die 2 "key-capture miss-proof not sanctioned for key: ${key:-missing}"
      [ "$key_miss_proof" = "$KEY_CAPTURE_SANCTIONED_MISS_RUNS" ] \
        || die 2 "key-capture miss-proof runs mismatch: $key_miss_proof"
    else
      die 2 "key-capture carve not granted: --force-key-capture is required"
    fi
    [ -n "$key" ] || die 2 "key-capture requires --key <fixture-key>"
    [ "$carrier" = "main" ] || die 2 "key-capture requires --carrier main"
    [[ "$expect_head" =~ ^[0-9a-f]{40}$ ]] || die 2 "key-capture requires --head <main-sha>"
    [ -n "$pr" ] && die 2 "key-capture mode does not accept --pr"
    [ -n "$profile" ] && die 2 "key-capture mode does not accept --profile"
  else
    need_num pr "$pr"
    [ -n "$issue" ] && need_num issue "$issue"
    [ -n "$retry_run" ] && need_num retry-run "$retry_run"
    [ -n "$source_e2e_run" ] && need_num source-e2e-run "$source_e2e_run"
  fi

  if [ "$key_capture" -eq 1 ]; then
    local dispatch_out run_id
    local -a key_wrapper_args=(--key "$key" --carrier main --head "$expect_head")
    if [ "$force_key_capture" = "1" ]; then
      key_wrapper_args+=(--force-key-capture)
    else
      key_wrapper_args+=(--key-miss-proof "$key_miss_proof")
    fi
    dispatch_out="$(REMOTE_CAPTURE_ONLY=1 bash "$REMOTE_CAPTURE_WRAPPER" "${key_wrapper_args[@]}" 2>&1)" \
      || die 1 "remote capture key dispatch failed: $dispatch_out"
    run_id="$(printf '%s' "$dispatch_out" | sed -n 's/.* run=\([0-9][0-9]*\).*/\1/p' | tail -n1)"
    [ -n "$run_id" ] || die 1 "remote capture key wrapper returned no run id: $dispatch_out"
    record_event --source pm-transition --event capture_key_dispatched --target-type pr --target-id 0 --pr 0 --head-sha "$expect_head" --payload "key=$key" --payload "carrier=main" --payload "run=$run_id" --dedupe
    echo "PM_TRANSITION_OK command=capture-remote-dispatch mode=key-capture key=$key head=${expect_head:0:10} run=$run_id next=\"verify sanctioned fixture key on strict replay\""
    return 0
  fi

  local pr_json live_head branch state owner_slot released_slots receipt latest_head dispatch_out run_id target_args=()
  pr_json="$(gh pr view "$pr" --repo "$REPO" --json state,isDraft,headRefOid,headRefName,labels 2>/dev/null || true)"
  [ -n "$pr_json" ] || die 1 "cannot read PR #$pr"
  state="$(printf '%s' "$pr_json" | json_field state 2>/dev/null || true)"
  [ "$state" = "OPEN" ] || die 1 "PR #$pr is not open"
  [ "$(printf '%s' "$pr_json" | json_field isDraft 2>/dev/null || true)" = "false" ] || die 1 "PR #$pr is draft"
  live_head="$(printf '%s' "$pr_json" | json_field headRefOid 2>/dev/null || true)"
  branch="$(printf '%s' "$pr_json" | json_field headRefName 2>/dev/null || true)"
  [ -n "$live_head" ] && [ -n "$branch" ] || die 1 "cannot read PR #$pr tuple"
  [ -z "$expect_head" ] || [ "$expect_head" = "$live_head" ] || die 1 "PR #$pr head drift: expected=$expect_head live=$live_head"
  [ -n "$issue" ] || issue="$(issue_from_pr "$pr")"
  [ -n "$issue" ] && target_args+=(--issue "$issue")

  owner_slot="$(slot_from_labels "$pr" "$issue")"
  capture_release_target_before_ci_start released_slots \
    "$pr" "$issue" "$branch" "capture-remote-dispatch" "$owner_slot" || return $?
  latest_head="$(gh pr view "$pr" --repo "$REPO" --json headRefOid --jq '.headRefOid' 2>/dev/null || true)"
  [ "$latest_head" = "$live_head" ] || die 1 "PR #$pr head moved after capture slot release: expected=$live_head live=${latest_head:-unknown}"

  receipt="/tmp/remote-capture-release-${pr}-${live_head}.json"
  python3 - "$receipt" "$pr" "$live_head" "$branch" "${released_slots:-}" <<'PY' || die 1 "failed to write release receipt"
import json, sys
from datetime import datetime, timezone
from pathlib import Path
path, pr, head, branch, released = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
path.write_text(json.dumps({
    "schema_version": 1,
    "release_confirmed": True,
    "pr": pr,
    "head_sha": head,
    "branch": branch,
    "released_slots": released.split(),
    "confirmed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
}, indent=2) + "\n")
PY
  gh pr edit "$pr" --repo "$REPO" --add-label "pm-blocked:capture" >/dev/null || die 1 "failed to add capture blocker"
  if [ -n "$released_slots" ]; then
    local reconcile_log="/tmp/pm-remote-capture-reconcile-${pr}.log"
    if ! REMOTE_CAPTURE_ONLY=1 bash "$PM_TRANSITION_SELF" reconcile-capacity >"$reconcile_log" 2>&1; then
      # The legacy shell reconciler may fail after assignment/outbox delivery
      # errors that are unrelated to this off-slot capture.  Do not interpret
      # its mixed human log as a capacity receipt: ask the existing typed engine
      # for one read-only JSON snapshot and let the existing gate validate it.
      local reconcile_gate capacity_receipt
      [ -x "$CAPACITY_CONTROL" ] \
        || die 1 "capacity control engine is not executable: $CAPACITY_CONTROL"
      [ -x "$CAPTURE_CAPACITY_RECONCILE_GATE" ] \
        || die 1 "capacity reconcile gate is not executable: $CAPTURE_CAPACITY_RECONCILE_GATE"
      capacity_receipt="/tmp/pm-remote-capture-capacity-${pr}-${live_head}.json"
      python3 "$CAPACITY_CONTROL" reconcile --dry-run >"$capacity_receipt" 2>&1 || true
      reconcile_gate="$(python3 "$CAPTURE_CAPACITY_RECONCILE_GATE" "$capacity_receipt")" \
        || die 1 "same-cycle capacity reconciliation failed for PR #$pr"
      [ -n "$reconcile_gate" ] \
        || die 1 "same-cycle capacity reconciliation failed for PR #$pr"
      echo "PM_TRANSITION_WARN command=capture-remote-dispatch pr=$pr reconciliation=non_converged_unrelated_blockers gate=$reconcile_gate receipt=$capacity_receipt legacy_log=$reconcile_log"
    fi
  fi
  local -a wrapper_args=(--pr "$pr")
  [ -n "$retry_run" ] && wrapper_args+=(--retry-run "$retry_run")
  [ -n "$source_e2e_run" ] && wrapper_args+=(--source-e2e-run "$source_e2e_run")
  [ -n "$descendant_proof" ] && wrapper_args+=(--descendant-proof-json "$descendant_proof")
  [ -n "$profile" ] && wrapper_args+=(--capture-profile "$profile")
  dispatch_out="$(REMOTE_CAPTURE_ONLY=1 bash "$REMOTE_CAPTURE_WRAPPER" "${wrapper_args[@]}" 2>&1)" \
    || die 1 "remote capture dispatch failed for PR #$pr: $dispatch_out"
  run_id="$(printf '%s' "$dispatch_out" | sed -n 's/.* run=\([0-9][0-9]*\).*/\1/p' | tail -n1)"
  [ -n "$run_id" ] || die 1 "remote capture wrapper returned no run id: $dispatch_out"

  resolve_pr_obligation_kinds "$pr" "$issue" "remote_capture_dispatched" "run=$run_id" capture_local_preflight capture_watch capture_rearm_after_main_sync blocked_rework
  upsert_obligation --kind capture_watch --severity high --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --owner pm --horizon hourly --next-review-at "$(utc_plus_minutes 10)" --title "PR #$pr budgeted remote capture" --action "Watch run $run_id. On success run $PM_TRANSITION_SELF capture-remote-pass --pr $pr --run $run_id. Do not assign a dev slot while capture runs." --blocker "capture_remote_watch" --evidence "head=$live_head" --evidence "run=$run_id" --evidence "source_e2e_run=${source_e2e_run:-none}" --evidence "release_receipt=$receipt" --evidence "released_slots=${released_slots:-none}"
  record_event --source pm-transition --event capture_remote_dispatched --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --head-sha "$live_head" --payload "run=$run_id" --payload "retry_run=${retry_run:-none}" --payload "source_e2e_run=${source_e2e_run:-none}" --payload "branch=$branch" --payload "release_receipt=$receipt" --payload "released_slots=${released_slots:-none}" --dedupe
  echo "PM_TRANSITION_OK command=capture-remote-dispatch pr=$pr head=${live_head:0:10} run=$run_id released_slots=${released_slots:-none} next=\"capture-remote-pass --pr $pr --run $run_id\""
}

cmd_capture_remote_pass() {
  remote_capture_only_enabled || die 12 "REMOTE_CAPTURE_ONLY is disabled; remote-only capture has not been rolled out"
  local pr="" run_id=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      --run) run_id="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown capture-remote-pass arg $1" ;;
    esac
  done
  need_num pr "$pr"
  need_num run "$run_id"

  local pr_json live_head branch issue run_json run_json_file target_args=()
  pr_json="$(gh pr view "$pr" --repo "$REPO" --json state,headRefOid,headRefName 2>/dev/null || true)"
  [ "$(printf '%s' "$pr_json" | json_field state 2>/dev/null || true)" = "OPEN" ] || die 1 "PR #$pr is not open"
  live_head="$(printf '%s' "$pr_json" | json_field headRefOid 2>/dev/null || true)"
  branch="$(printf '%s' "$pr_json" | json_field headRefName 2>/dev/null || true)"
  run_json="$(gh run view "$run_id" --repo "$REPO" --json displayTitle,status,conclusion,attempt,event,url,workflowName,jobs 2>/dev/null || true)"
  [ -n "$run_json" ] || die 1 "cannot read capture run $run_id"
  run_json_file="$(mktemp)"
  printf '%s\n' "$run_json" > "$run_json_file"
  python3 "$REMOTE_CAPTURE_RUN_VALIDATOR" validate --run-json "$run_json_file" --pr "$pr" --head "$live_head" \
    || { rm -f "$run_json_file"; die 1 "remote capture workflow did not pass for current head run=$run_id"; }
  rm -f "$run_json_file"
  issue="$(issue_from_pr "$pr")"
  [ -n "$issue" ] && target_args+=(--issue "$issue")
  assert_no_slot_owner_for_phase "$pr" "$issue" "$branch" "capture-remote-pass"
  resolve_pr_obligation_kinds "$pr" "$issue" "remote_capture_passed" "run=$run_id" capture_watch capture_local_preflight blocked_rework
  record_event --source pm-transition --event capture_remote_pass --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --head-sha "$live_head" --payload "run=$run_id" --payload "evidence=github_workflow" --payload "branch=$branch" --dedupe
  gh pr edit "$pr" --repo "$REPO" --remove-label "pm-blocked:capture" >/dev/null || true

  # Rajiv release-policy (thread 1786713760.734709 ts 1786714368.757699):
  # the post-capture workflow is local repro -> label-gated CI. A successful
  # exact-head remote capture must NOT resume pm-review-done, must NOT route
  # already-green to the readiness contract, and must NOT trigger or admit
  # CI: capture changes external fixture state, so even a prior green pair is
  # history. Create the existing assignable slot-local-repro obligation
  # (ci_local_preflight / ci_local_preflight_required) bound to PR + full
  # live head + capture run. The slot runs the canonical affected local repro
  # and records the raw classification through cmd_ci_local_preflight_pass
  # (no sealed envelope), which hands off the CTO-owned cto_ci_wave_required
  # wave obligation. PM never fires CI.
  upsert_obligation --kind ci_local_preflight --severity high \
    --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} \
    --owner pm --horizon hourly \
    --dedupe-group "post_capture_local_repro:${pr}:${live_head}:${run_id}" \
    --title "PR #$pr post-capture local repro required before label-gated CI" \
    --action "Successful exact-head remote capture run $run_id at head $live_head on $branch. Run the canonical affected local repro (scripts/e2e/local-repro-preflight.sh with STRICT_FIXTURES=true LLM_PROXY_ENABLED=true) at the exact pushed head, fix any defect found, and record the raw repro classification (no sealed envelope) through pm-transition ci-local-preflight-pass --pr $pr --failed-run $run_id. Do not resume pm-review-done, route readiness, dispatch CI, or toggle labels; CTO owns the next exact-head label-gated CI+E2E wave (cto_ci_wave_required)." \
    --blocker "ci_local_preflight_required" \
    --evidence "head=$live_head" --evidence "branch=$branch" \
    --evidence "capture_run=$run_id" --evidence "next_actor=cto"
  kanban_flag PM_TRANSITION "capture_remote_pass pr=$pr issue=${issue:-unknown} head=$live_head run=$run_id next=ci-local-preflight-repro"
  echo "PM_TRANSITION_OK command=capture-remote-pass pr=$pr head=${live_head:0:10} run=$run_id next=ci-local-preflight-repro slot_ready_event=none"
}

cmd_capture_remote_exhaust() {
  remote_capture_only_enabled || die 12 "REMOTE_CAPTURE_ONLY is disabled; remote-only capture has not been rolled out"
  local pr="" first_run="" second_run=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      --first-run) first_run="${2:-}"; shift 2 ;;
      --second-run) second_run="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown capture-remote-exhaust arg $1" ;;
    esac
  done
  need_num pr "$pr"
  need_num first-run "$first_run"
  need_num second-run "$second_run"
  [ "$first_run" != "$second_run" ] || die 2 "first and second capture runs must differ"

  local pr_json live_head branch issue work first_dir second_dir first_attempt second_attempt first_meta second_meta comparison classification route evidence_dir comparison_path comment slack_result target_args=()
  pr_json="$(gh pr view "$pr" --repo "$REPO" --json state,headRefOid,headRefName,url 2>/dev/null || true)"
  [ "$(printf '%s' "$pr_json" | json_field state 2>/dev/null || true)" = "OPEN" ] || die 1 "PR #$pr is not open"
  live_head="$(printf '%s' "$pr_json" | json_field headRefOid 2>/dev/null || true)"
  branch="$(printf '%s' "$pr_json" | json_field headRefName 2>/dev/null || true)"
  issue="$(issue_from_pr "$pr")"
  [ -n "$issue" ] && target_args+=(--issue "$issue")
  assert_no_slot_owner_for_phase "$pr" "$issue" "$branch" "capture-remote-exhaust"
  work="$(mktemp -d)"
  first_dir="$work/first"
  second_dir="$work/second"
  mkdir -p "$first_dir" "$second_dir"
  first_meta="$(gh run view "$first_run" --repo "$REPO" --json displayTitle,attempt,url 2>/dev/null || true)"
  second_meta="$(gh run view "$second_run" --repo "$REPO" --json displayTitle,attempt,url 2>/dev/null || true)"
  [ "$(printf '%s' "$first_meta" | json_field displayTitle 2>/dev/null || true)" = "remote-capture-pr-${pr}-head-${live_head}" ] \
    || { rm -rf "$work"; die 1 "first run tuple mismatch"; }
  [ "$(printf '%s' "$second_meta" | json_field displayTitle 2>/dev/null || true)" = "remote-capture-pr-${pr}-head-${live_head}" ] \
    || { rm -rf "$work"; die 1 "second run tuple mismatch"; }
  first_attempt="$(printf '%s' "$first_meta" | json_field attempt 2>/dev/null || true)"; [ -n "$first_attempt" ] || first_attempt=1
  second_attempt="$(printf '%s' "$second_meta" | json_field attempt 2>/dev/null || true)"; [ -n "$second_attempt" ] || second_attempt=1
  # A cancelled or watchdog-terminated capture may only have the partial
  # attempt artifact.  It is still authoritative terminal evidence for the
  # two-attempt comparison; do not leave the PR stuck at pm-blocked:capture
  # merely because the finalizer did not get a chance to publish the full
  # artifact.  Prefer the finalized artifact, then fall back to its exact
  # tuple-matched partial counterpart.
  download_attempt_artifact() {
    local run="$1" attempt="$2" dir="$3"
    gh run download "$run" --repo "$REPO" --name "capture-attempt-${run}-${attempt}" --dir "$dir" >/dev/null 2>&1 \
      || gh run download "$run" --repo "$REPO" --name "capture-attempt-partial-${run}-${attempt}" --dir "$dir" >/dev/null 2>&1
  }
  download_attempt_artifact "$first_run" "$first_attempt" "$first_dir" \
    || { rm -rf "$work"; die 1 "first attempt artifact missing run=$first_run (final or partial)"; }
  download_attempt_artifact "$second_run" "$second_attempt" "$second_dir" \
    || { rm -rf "$work"; die 1 "second attempt artifact missing run=$second_run (final or partial)"; }
  comparison="$(python3 /Users/rajiv/Downloads/projects/heydonna-app/scripts/ci/remote-capture-attempt.py compare \
    --first "$first_dir/capture-attempt.json" --second "$second_dir/capture-attempt.json")" \
    || { rm -rf "$work"; die 1 "capture attempt comparison failed"; }
  classification="$(printf '%s' "$comparison" | json_field classification 2>/dev/null || true)"
  route="$(printf '%s' "$comparison" | json_field route 2>/dev/null || true)"
  [ "$route" = "cto_escalation" ] || [ "$route" = "product_rework" ] \
    || { rm -rf "$work"; die 1 "invalid capture exhaustion route=$route"; }

  evidence_dir="/tmp/pm-capture-exhaustion/pr-${pr}-${live_head}"
  mkdir -p "$evidence_dir"
  comparison_path="$evidence_dir/comparison.json"
  comment="$evidence_dir/pr-comment.md"
  printf '%s\n' "$comparison" > "$comparison_path"
  install -m 600 "$first_dir/capture-attempt.json" "$evidence_dir/attempt-${first_run}-${first_attempt}.json"
  install -m 600 "$second_dir/capture-attempt.json" "$evidence_dir/attempt-${second_run}-${second_attempt}.json"
  {
    printf '## Remote capture terminal disposition\n\n'
    printf -- '- PR: #%s\n- exact head: `%s`\n- branch: `%s`\n' "$pr" "$live_head" "$branch"
    printf -- '- runs: [%s](%s), [%s](%s)\n' "$first_run" "$(printf '%s' "$first_meta" | json_field url)" "$second_run" "$(printf '%s' "$second_meta" | json_field url)"
    printf -- '- classification: `%s`\n- route: `%s`\n' "$classification" "$route"
    printf -- '- last safe continuation point: slot released; capture proof not consumed; current-head CI not started\n'
    printf -- '- artifacts: `capture-attempt-%s-%s`, `capture-attempt-%s-%s`\n\n' "$first_run" "$first_attempt" "$second_run" "$second_attempt"
    printf 'Per-attempt classifications, terminal stages, key sets, and overlap:\n\n```json\n%s\n```\n' "$comparison"
    printf '\nAttempt 1:\n```json\n'; cat "$first_dir/capture-attempt.json"; printf '```\n'
    printf '\nAttempt 2:\n```json\n'; cat "$second_dir/capture-attempt.json"; printf '```\n'
  } > "$comment"
  gh pr comment "$pr" --repo "$REPO" --body-file "$comment" >/dev/null || { rm -rf "$work"; die 1 "failed to post capture exhaustion comment"; }

  if [ "$route" = "cto_escalation" ]; then
    slack_result="$(python3 - "$pr" "$live_head" "$first_run" "$second_run" "$classification" <<'PY'
import json, os, subprocess, sys
from pathlib import Path
pr, head, first, second, classification = sys.argv[1:]
token = ""
for line in Path('/Users/rajiv/Downloads/projects/heydonna-app/.env.local').read_text().splitlines():
    if line.startswith('SLACK_USER_TOKEN='):
        token = line.split('=', 1)[1].strip().strip('"').strip("'")
        break
if not token:
    raise SystemExit('SLACK_USER_TOKEN missing')
payload = {
    'channel': 'C0ALZJHGE49',
    'text': f'<@U0ALEAYCAUT> CTO capture escalation: PR #{pr} exact head {head[:10]} exhausted two classified infrastructure attempts (runs {first}, {second}; classification={classification}). Slot ownership is released. Use the diagnosis-ready PR comment to decide external recovery or closure/reimplementation. Reply APPROVE/BLOCK in this top-level thread.',
}
out = subprocess.check_output(['curl','-sS','-X','POST','https://slack.com/api/chat.postMessage','-H',f'Authorization: Bearer {token}','-H','Content-Type: application/json; charset=utf-8','-d',json.dumps(payload)], text=True)
data = json.loads(out)
if not data.get('ok'):
    raise SystemExit(data.get('error') or 'slack_post_failed')
print(data.get('ts') or '')
PY
)" || { rm -rf "$work"; die 1 "failed to create top-level CTO capture escalation"; }
    gh pr edit "$pr" --repo "$REPO" --remove-label "pm-blocked:capture" --add-label "pm-blocked:infra" >/dev/null \
      || { rm -rf "$work"; die 1 "failed to apply infrastructure exhaustion label"; }
    upsert_obligation --kind cto_capture_escalation --severity critical --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --owner cto --horizon hourly --title "PR #$pr remote capture infrastructure exhausted" --action "Continue in top-level #heydonna-dev thread $slack_result; do not assign a dev slot or retry capture." --blocker "capture_infrastructure_exhausted" --evidence "runs=$first_run,$second_run" --evidence "classification=$classification" --evidence "slack_thread_ts=$slack_result" --evidence "comment=$comment" --evidence "comparison=$comparison_path"
  else
    gh pr edit "$pr" --repo "$REPO" --remove-label "pm-blocked:capture" --add-label "pm-blocked:codex" >/dev/null \
      || { rm -rf "$work"; die 1 "failed to route non-deterministic capture to product rework"; }
    bash "$PM_STATE" "$pr" blocked-rework >/dev/null || true
    upsert_obligation --kind blocked_rework --severity high --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --owner pm --horizon hourly --title "PR #$pr capture request-key product rework" --action "Create an exact-head rework packet targeting unstable request-key inputs using both attempt artifacts; only then assign a new slot epoch." --blocker "$classification" --evidence "runs=$first_run,$second_run" --evidence "comment=$comment" --evidence "comparison=$comparison_path"
  fi
  resolve_pr_obligation_kinds "$pr" "$issue" "remote_capture_terminal" "classification=$classification" capture_watch capture_local_preflight
  record_event --source pm-transition --event capture_remote_exhaust --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --head-sha "$live_head" --payload "runs=$first_run,$second_run" --payload "classification=$classification" --payload "route=$route" --payload "slack_thread_ts=${slack_result:-none}" --dedupe
  rm -rf "$work"
  echo "PM_TRANSITION_OK command=capture-remote-exhaust pr=$pr head=${live_head:0:10} runs=$first_run,$second_run classification=$classification route=$route slack_thread_ts=${slack_result:-none}"
}

cmd_capture_remote_fail() {
  remote_capture_only_enabled || die 12 "REMOTE_CAPTURE_ONLY is disabled; remote-only capture has not been rolled out"
  local pr="" run_id="" dependency_pr=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      --run) run_id="${2:-}"; shift 2 ;;
      --dependency-pr) dependency_pr="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown capture-remote-fail arg $1" ;;
    esac
  done
  need_num pr "$pr"
  need_num run "$run_id"
  [ -z "$dependency_pr" ] || need_num dependency-pr "$dependency_pr"

  local pr_json live_head branch issue run_json title run_head status attempt work artifact evidence_dir comment target_args=()
  pr_json="$(gh pr view "$pr" --repo "$REPO" --json state,headRefOid,headRefName,url,labels 2>/dev/null || true)"
  [ "$(printf '%s' "$pr_json" | json_field state 2>/dev/null || true)" = "OPEN" ] || die 1 "PR #$pr is not open"
  live_head="$(printf '%s' "$pr_json" | json_field headRefOid 2>/dev/null || true)"
  branch="$(printf '%s' "$pr_json" | json_field headRefName 2>/dev/null || true)"
  issue="$(issue_from_pr "$pr")"
  [ -n "$issue" ] && target_args+=(--issue "$issue")
  run_json="$(gh run view "$run_id" --repo "$REPO" --json displayTitle,status,conclusion,attempt,updatedAt,url 2>/dev/null || true)"
  [ -n "$run_json" ] || die 1 "cannot read capture run $run_id"
  title="$(printf '%s' "$run_json" | json_field displayTitle 2>/dev/null || true)"
  run_head="$(capture_run_head_from_title "$pr" "$title" 2>/dev/null || true)"
  [ -n "$run_head" ] || die 1 "capture run tuple mismatch title=$title expected_pr=$pr"
  status="$(printf '%s' "$run_json" | json_field status 2>/dev/null || true)"
  [ "$status" = "completed" ] || die 1 "capture run $run_id is not completed"
  attempt="$(printf '%s' "$run_json" | json_field attempt 2>/dev/null || true)"; [ -n "$attempt" ] || attempt=1

  work="$(mktemp -d)"
  artifact="$work/capture-attempt.json"
  gh run download "$run_id" --repo "$REPO" --name "capture-attempt-${run_id}-${attempt}" --dir "$work" >/dev/null \
    || true
  if [ ! -f "$artifact" ]; then
    # Remote-capture runs never publish a GitHub Actions artifact: the workflow
    # persists canonical attempt evidence only to the Modal volume
    # `ci-runner-cache` at the exact path used by the workflow's evidence job
    # (and the sibling `capture-control-plane-repaired` fetch). Fall back to
    # the volume before declaring the evidence missing; keep exit-1 fail-closed
    # when neither source provides the attempt evidence.
    rm -f "$artifact"
    modal volume get --env main --force ci-runner-cache "/run-${run_id}-${attempt}/capture-attempt.json" "$artifact" >/dev/null 2>&1 \
      || { rm -rf "$work"; die 1 "capture attempt artifact missing run=$run_id attempt=$attempt"; }
  fi

  if [ "$run_head" != "$live_head" ]; then
    python3 - "$artifact" "$pr" "$run_head" "$run_id" <<'PY' \
      || { rm -rf "$work"; die 1 "stale capture artifact tuple mismatch run=$run_id"; }
import json
import sys

path, expected_pr, expected_head, expected_run = sys.argv[1:]
data = json.load(open(path, encoding="utf-8"))
if (
    data.get("pr_number") != int(expected_pr)
    or data.get("head_sha") != expected_head
    or data.get("run_id") != int(expected_run)
    or not data.get("head_branch")
):
    raise SystemExit("stale capture artifact is not bound to the run tuple")
PY
    evidence_dir="/tmp/pm-capture-stale/pr-${pr}-${run_head}"
    mkdir -p "$evidence_dir"
    install -m 600 "$artifact" "$evidence_dir/attempt-${run_id}-${attempt}.json"
    comment="$evidence_dir/pr-comment.md"
    {
      printf '## Remote capture discarded as stale\n\n'
      printf -- '- PR: #%s\n- capture head: `%s`\n- current live head: `%s`\n- branch: `%s`\n' "$pr" "$run_head" "$live_head" "$branch"
      printf -- '- run: [%s](%s)\n- classification: `stale_head`\n' "$run_id" "$(printf '%s' "$run_json" | json_field url)"
      printf -- '- No product rework or `pm-blocked:codex` transition was applied. The current head still requires its own exact-head capture.\n'
      printf -- '- historical artifact: `%s`\n' "$evidence_dir/attempt-${run_id}-${attempt}.json"
    } > "$comment"
    gh pr comment "$pr" --repo "$REPO" --body-file "$comment" >/dev/null \
      || { rm -rf "$work"; die 1 "failed to post stale capture evidence comment"; }
    record_event --source pm-transition --event capture_remote_stale_head --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --head-sha "$run_head" --payload "run=$run_id" --payload "live_head=$live_head" --payload "classification=stale_head" --payload "artifact=$evidence_dir/attempt-${run_id}-${attempt}.json" --dedupe
    rm -rf "$work"
    echo "PM_TRANSITION_OK command=capture-remote-fail pr=$pr capture_head=${run_head:0:10} live_head=${live_head:0:10} run=$run_id classification=stale_head route=discard artifact=$evidence_dir/attempt-${run_id}-${attempt}.json"
    return 0
  fi

  python3 /Users/rajiv/Downloads/projects/heydonna-app/scripts/ci/remote-capture-attempt.py validate-failure \
    --attempt-proof "$artifact" --pr "$pr" --head "$live_head" --branch "$branch" --run "$run_id" \
    || { rm -rf "$work"; die 1 "capture run $run_id is not a deterministic exact-head failure"; }
  assert_no_slot_owner_for_phase "$pr" "$issue" "$branch" "capture-remote-fail"

  if [ -n "$dependency_pr" ]; then
    local live_labels dependency_json dependency_state dependency_merged_at run_updated_at recovery_marker recovery_dir recovery_path
    live_labels="$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print("\n".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
    printf '%s\n' "$live_labels" | grep -qx 'pm-state:qa-passed-awaiting-ci' \
      || { rm -rf "$work"; die 44 "dependency recovery requires pm-state:qa-passed-awaiting-ci for PR #$pr; do not clear an active rework state"; }
    dependency_json="$(gh pr view "$dependency_pr" --repo "$REPO" --json state,mergedAt 2>/dev/null || true)"
    dependency_state="$(printf '%s' "$dependency_json" | json_field state 2>/dev/null || true)"
    dependency_merged_at="$(printf '%s' "$dependency_json" | json_field mergedAt 2>/dev/null || true)"
    [ "$dependency_state" = "MERGED" ] && [ -n "$dependency_merged_at" ] \
      || { rm -rf "$work"; die 44 "dependency #$dependency_pr is not merged; refusing dependency recovery for PR #$pr"; }
    run_updated_at="$(printf '%s' "$run_json" | json_field updatedAt 2>/dev/null || true)"
    [ -n "$run_updated_at" ] || { rm -rf "$work"; die 1 "capture run $run_id has no completion timestamp; refusing dependency recovery"; }
    recovery_marker="/tmp/pm-dependency-watch-acks/pr-${pr}-${live_head}-visible-marker.json"
    python3 - "$recovery_marker" "$pr" "$live_head" "$dependency_pr" "$run_updated_at" "$dependency_merged_at" <<'PY' \
      || { rm -rf "$work"; die 44 "dependency watch evidence does not prove that capture run failed before dependency #$dependency_pr merged"; }
import json
import sys
from datetime import datetime
from pathlib import Path

marker_path, pr, head, dependency, run_updated, dependency_merged = sys.argv[1:]
try:
    marker = json.loads(Path(marker_path).read_text(encoding="utf-8"))
    run_time = datetime.fromisoformat(run_updated.replace("Z", "+00:00"))
    merged_time = datetime.fromisoformat(dependency_merged.replace("Z", "+00:00"))
except Exception as exc:
    raise SystemExit(f"invalid dependency recovery evidence: {exc}")
refs = {str(value).lstrip("#") for value in marker.get("dependency_refs", [])}
if marker.get("status") != "watching" or str(marker.get("pr")) != pr or marker.get("head") != head or dependency not in refs:
    raise SystemExit("dependency watch marker tuple mismatch")
if merged_time <= run_time:
    raise SystemExit("dependency mergedAt is not after the failed capture run")
PY
    recovery_dir="/tmp/pm-capture-dependency-recovery/pr-${pr}-${live_head}"
    recovery_path="$recovery_dir/recovery.json"
    mkdir -p "$recovery_dir" || { rm -rf "$work"; die 1 "failed to create dependency recovery receipt directory"; }
    python3 - "$recovery_path" "$pr" "$live_head" "$branch" "$run_id" "$dependency_pr" "$run_updated_at" "$dependency_merged_at" "$recovery_marker" <<'PY' \
      || { rm -rf "$work"; die 1 "failed to write dependency recovery receipt"; }
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path, pr, head, branch, run_id, dependency, run_updated, dependency_merged, marker = sys.argv[1:]
Path(path).write_text(json.dumps({
    "schema_version": 1,
    "classification": "dependency_resolved",
    "pr": int(pr),
    "head_sha": head,
    "branch": branch,
    "failed_run": int(run_id),
    "dependency_pr": int(dependency),
    "run_updated_at": run_updated,
    "dependency_merged_at": dependency_merged,
    "dependency_watch_marker": marker,
    "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
    gh pr edit "$pr" --repo "$REPO" --remove-label "pm-blocked:codex" --add-label "pm-blocked:capture" >/dev/null \
      || { rm -rf "$work"; die 1 "failed to clear stale codex blocker for PR #$pr"; }
    resolve_pr_obligation_kinds "$pr" "$issue" "remote_capture_dependency_recovered" "run=$run_id dependency_pr=$dependency_pr" capture_watch capture_local_preflight blocked_rework
    record_event --source pm-transition --event capture_remote_dependency_recovered --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --head-sha "$live_head" --payload "run=$run_id" --payload "dependency_pr=$dependency_pr" --payload "dependency_merged_at=$dependency_merged_at" --payload "recovery_receipt=$recovery_path" --dedupe
    rm -rf "$work"
    echo "PM_TRANSITION_OK command=capture-remote-fail pr=$pr head=${live_head:0:10} run=$run_id classification=dependency_resolved route=remote_retry dependency_pr=$dependency_pr recovery_receipt=$recovery_path"
    return 0
  fi

  evidence_dir="/tmp/pm-capture-terminal/pr-${pr}-${live_head}"
  mkdir -p "$evidence_dir"
  install -m 600 "$artifact" "$evidence_dir/attempt-${run_id}-${attempt}.json"
  comment="$evidence_dir/pr-comment.md"
  {
    printf '## Remote capture deterministic failure\n\n'
    printf -- '- PR: #%s\n- exact head: `%s`\n- branch: `%s`\n' "$pr" "$live_head" "$branch"
    printf -- '- run: [%s](%s)\n- classification: `capture_non_convergent`\n' "$run_id" "$(printf '%s' "$run_json" | json_field url)"
    printf -- '- slot ownership remains released; no same-head retry is permitted because the attempt is not infrastructure-retryable\n'
    printf -- '- artifact: `%s`\n\n' "$evidence_dir/attempt-${run_id}-${attempt}.json"
    printf 'Attempt evidence:\n```json\n'; cat "$artifact"; printf '```\n'
  } > "$comment"
  gh pr comment "$pr" --repo "$REPO" --body-file "$comment" >/dev/null \
    || { rm -rf "$work"; die 1 "failed to post deterministic capture failure comment"; }
  gh pr edit "$pr" --repo "$REPO" --remove-label "pm-blocked:capture" --add-label "pm-blocked:codex" >/dev/null \
    || { rm -rf "$work"; die 1 "failed to route deterministic capture failure to product rework"; }
  bash "$PM_STATE" "$pr" blocked-rework >/dev/null 2>&1 || true
  upsert_obligation --kind blocked_rework --severity high --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --owner pm --horizon hourly --title "PR #$pr remote capture deterministic failure" --action "Create an exact-head rework packet for capture_non_convergent using $evidence_dir/attempt-${run_id}-${attempt}.json; only a new pushed head may request another capture." --blocker "capture_non_convergent" --evidence "run=$run_id" --evidence "head=$live_head" --evidence "artifact=$evidence_dir/attempt-${run_id}-${attempt}.json"
  resolve_pr_obligation_kinds "$pr" "$issue" "remote_capture_failed" "run=$run_id classification=capture_non_convergent" capture_watch capture_local_preflight
  record_event --source pm-transition --event capture_remote_failed --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --head-sha "$live_head" --payload "run=$run_id" --payload "classification=capture_non_convergent" --payload "artifact=$evidence_dir/attempt-${run_id}-${attempt}.json" --dedupe
  rm -rf "$work"
  echo "PM_TRANSITION_OK command=capture-remote-fail pr=$pr head=${live_head:0:10} run=$run_id classification=capture_non_convergent route=product_rework artifact=$evidence_dir/attempt-${run_id}-${attempt}.json"
}

cmd_capture_local_required() {
  # REMOTE_CAPTURE_ONLY governs automatic/default routing. This typed command
  # is the explicit diagnostic exception for an exact-head local capture,
  # bound to a named remote-capture infrastructure defect. It never satisfies
  # capture readiness and is not a fallback after remote red.
  local pr="" issue="" slot="" failed_run="" expect_head="" reason=""
  local fixture_keys=()
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      --issue) issue="${2:-}"; shift 2 ;;
      --slot) slot="${2:-}"; shift 2 ;;
      --failed-run) failed_run="${2:-}"; shift 2 ;;
      --fixture-key|--required-fixture-key) fixture_keys+=("${2:-}"); shift 2 ;;
      --head) expect_head="${2:-}"; shift 2 ;;
      --reason) reason="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown capture-local-required arg $1" ;;
    esac
  done
  need_num pr "$pr"
  [ -n "$reason" ] || die 2 "--reason is required: name the remote-capture infrastructure defect (runner_bootstrap|modal_deploy|dispatch_callback_wall|github_external_transient|capture_infra_failed)"
  case "$reason" in
    runner_bootstrap|modal_deploy|dispatch_callback_wall|github_external_transient|capture_infra_failed) ;;
    *) die 2 "capture-local-required is diagnostic-only; --reason must name a remote-capture infrastructure defect, got: $reason" ;;
  esac
  [ -n "$issue" ] && need_num issue "$issue"
  [ -n "$slot" ] && need_num slot "$slot"
  if [ -n "$slot" ] && ! [[ "$slot" =~ ^[1-4]$ ]]; then
    die 2 "slot must be 1..4"
  fi
  [ -n "$failed_run" ] && need_num failed-run "$failed_run"

	  local pr_json live_head branch title labels selected_slot mop payload packet proof next_review failed_arg slot_checkout assigned="0" delivered="0"
  pr_json="$(gh pr view "$pr" --repo "$REPO" --json state,headRefOid,headRefName,title,labels 2>/dev/null || true)"
  [ -n "$pr_json" ] || die 1 "cannot read PR #$pr"
  [ "$(printf '%s' "$pr_json" | json_field state)" = "OPEN" ] || die 1 "PR #$pr is not open"
  live_head="$(printf '%s' "$pr_json" | json_field headRefOid 2>/dev/null || true)"
  branch="$(printf '%s' "$pr_json" | json_field headRefName 2>/dev/null || true)"
  title="$(printf '%s' "$pr_json" | json_field title 2>/dev/null || true)"
  labels="$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print(",".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
  [ -n "$live_head" ] || die 1 "cannot read PR #$pr head"
  [ -z "$expect_head" ] || [ "$expect_head" = "$live_head" ] || die 1 "PR #$pr head drift: expected=$expect_head live=$live_head"
  if [ -z "$issue" ]; then issue="$(issue_from_pr "$pr")"; fi
  [ -n "$issue" ] && need_num issue "$issue"
  if [ -z "$slot" ]; then slot="$(slot_from_labels "$pr" "$issue")"; fi
  if [ -z "$slot" ]; then slot="$(first_free_slot_for_rework "1 2 3 4")"; fi
  # S1-restore: a free/inactive slot with NO owner (lingering assignment_epoch,
  # occupied may remain true) stays eligible.  Scoped to capture-local-required
  # ordinary rework sweeps keep the strict gate.
  if [ -z "$slot" ]; then slot="$(first_restorable_free_slot_for_rework "1 2 3 4")"; fi
  [ -n "$slot" ] || die 10 "no dev slot available for capture-local-required; refusing to emit PM-checkout capture proof command"
  if [ -n "$slot" ] && ! [[ "$slot" =~ ^[1-4]$ ]]; then
    die 2 "slot must be 1..4"
  fi

	  proof="/tmp/capture-local-proof-${pr}-${live_head}.ok"
	  failed_arg=""
	  [ -n "$failed_run" ] && failed_arg=" --failed-run $failed_run"
	  local fixture_arg="" fixture_key
	  if [ "${#fixture_keys[@]}" -gt 0 ]; then
	    for fixture_key in "${fixture_keys[@]}"; do
	      [ -n "$fixture_key" ] || continue
	      fixture_arg="${fixture_arg} --fixture-key $(printf '%q' "$fixture_key")"
	    done
	  fi
	  slot_checkout="$(slot_checkout_path "$slot")"
	  packet="/tmp/pm-capture-local-required-${pr}-${live_head:0:10}.md"
	  {
    printf '# PR #%s local capture diagnostic (named infra defect: %s)\n\n' "$pr" "$reason"
    printf -- '- Issue: #%s\n' "${issue:-unknown}"
    printf -- '- Branch: `%s`\n' "${branch:-unknown}"
    printf -- '- Head: `%s`\n' "$live_head"
    printf -- '- Prior capture/CI run: `%s`\n\n' "${failed_run:-unknown}"
			    printf 'Run in the assigned slot checkout. This typed transition is an explicit diagnostic exception for remote-capture infrastructure defect `%s`; automatic capture routing is remote by default. The script requires a clean checkout at the exact live pushed PR head, sources slot `.env.local`, force-boots Convex/Modal/Next for the slot, verifies slot-local Modal suffix/endpoints, auto-detects branch-carried `preview-seed.zip` and imports it into local Convex when it differs from `origin/main`, and forces local `BASE_URL=http://localhost:$PORT` for capture. Do not merge `origin/main` merely for capture. Use the exact production model and record capture source head, model, transport, prompt hash, system-instruction hash, and fixture provenance:\n\n' "$reason"
		    printf '```bash\n'
		    printf 'cd %q\n' "$slot_checkout"
		    printf 'bash %q --pr %s --head %s --slot %s --checkout %q%s%s\n' "$CAPTURE_LOCAL_PROOF" "$pr" "$live_head" "$slot" "$slot_checkout" "$failed_arg" "$fixture_arg"
	    printf '/Users/rajiv/.claude/scripts/pm-transition.sh capture-local-pass --pr %s --proof %s%s\n' "$pr" "$proof" "$failed_arg"
	    printf '```\n\n'
    if [ "${#fixture_keys[@]}" -gt 0 ]; then
      printf 'Required fixture key/hash evidence: `%s`.\n\n' "$(printf '%s\n' "${fixture_keys[@]}" | paste -sd' ' -)"
    fi
    printf 'This proof is diagnostic evidence only and never satisfies capture readiness. After debugging, release the slot and dispatch the authoritative remote capture through `capture-remote-dispatch`.\n'
  } > "$packet"

  selected_slot="$slot"
  if [ -n "$selected_slot" ]; then
    mop="$(mop_slot_status "$selected_slot")"
    # S1-restore: an explicitly targeted free/inactive slot with NO owner
    # remains eligible even when its prior epoch lingers; the MoP assign bind
    # returns a NEW assignment_epoch.
    if [[ "$mop" == free:* ]] || { [ "$mop" != "unreachable" ] && [ "$mop" != "missing" ] && mop_slot_restorable_free "$selected_slot"; }; then
      assert_slot_assignment_not_quarantined "$selected_slot" "$pr" "$issue" "$branch"
      local claim_epoch repository_id claim_output
      local -a claim_args
      claim_epoch="$(mop_slot_epoch "$selected_slot" 2>/dev/null || true)"
      repository_id="${MOP_REPOSITORY_ID:-$MOP_PRIMARY_REPOSITORY_ID}"
      [ -n "$claim_epoch" ] || die 30 "capture-local-required cannot read assignment epoch slot=$selected_slot"
      claim_args=(
        --slot "$selected_slot" --pr "$pr"
        --branch "$branch" --head-sha "$live_head" \
        --expected-epoch "$claim_epoch" --repository-id "$repository_id" \
        --handoff-id "$packet" --task "LOCAL CAPTURE PROOF PR #$pr: $title"
      )
      [ -n "$issue" ] && claim_args+=(--issue "$issue")
      claim_output="$(claim_slot_compat repro assign-repro "${claim_args[@]}" 2>&1)" || {
        printf '%s\n' "$claim_output" >&2
        die 30 "capture-local-required claim_slot deferred slot=$selected_slot pr=$pr"
      }
      printf '%s\n' "$claim_output"
      assigned="1"
      delivered="1"
    elif ! mop_slot_matches_target "$selected_slot" "$pr" "$issue" "$branch"; then
      die 10 "slot $selected_slot is not available for PR #$pr ($mop)"
    fi
    if [ "$assigned" != "1" ] && [ -x "$MESSAGE_SLOT" ]; then
      bash "$MESSAGE_SLOT" "$selected_slot" --file "$packet" --force --from PM >/tmp/pm-capture-local-message-${pr}-${selected_slot}.log 2>&1 && delivered="1" || true
    fi
  fi

  # Ownership is now written only by claim_slot. Apply the diagnostic blocker
  # projection after that authoritative claim so a failed claim cannot leave
  # GitHub labels pretending that the slot was assigned.
  remove_pm_blockers "$pr" "" >/dev/null 2>&1 || true
  gh pr edit "$pr" --repo "$REPO" --add-label "pm-blocked:capture" >/dev/null \
    || die 1 "failed to add pm-blocked:capture to PR #$pr after claim_slot"
  bash "$PM_STATE" "$pr" blocked-rework >/dev/null || true

  local target_args=()
  [ -n "$issue" ] && target_args+=(--issue "$issue")
  [ -n "$selected_slot" ] && target_args+=(--slot "$selected_slot")
  next_review="$(utc_plus_minutes 25)"
	  upsert_obligation --kind capture_local_preflight --severity high --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --owner pm --horizon hourly --next-review-at "$next_review" --dedupe-group "capture_local_preflight:${pr}:${live_head}" --title "PR #$pr local capture diagnostic (named infra defect: $reason)" --action "Slot $selected_slot must run $CAPTURE_LOCAL_PROOF --pr $pr --head $live_head --slot $selected_slot --checkout /Users/rajiv/Downloads/projects/heydonna-app-300$selected_slot${failed_arg} for diagnostic debugging of remote-capture infrastructure defect $reason; it requires a clean checkout at the exact live pushed PR head, force-boots slot Convex/Modal/Next, verifies slot-local Modal env/endpoints, local BASE_URL, and branch-carried preview-seed.zip when the PR seed differs from origin/main. Do not merge origin/main merely for capture. Record the exact production model, transport, prompt/system-instruction hashes, capture source head, and fixture provenance. Then run pm-transition capture-local-pass --pr $pr --proof $proof${failed_arg}. This proof is diagnostic evidence only and never satisfies capture readiness; after debugging, dispatch the authoritative remote capture through capture-remote-dispatch." --blocker "capture_local_proof_required" --evidence "packet=$packet" --evidence "head=$live_head" --evidence "failed_run=${failed_run:-none}" --evidence "slot_checkout=$slot_checkout" --evidence "next_review_at=$next_review" --evidence "reason=$reason"
  resolve_pr_obligation_kinds "$pr" "$issue" "capture_rearmed_after_main_sync" \
    "head=$live_head command=capture-local-required" capture_rearm_after_main_sync
  record_event --source pm-transition --event capture_local_required --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --head-sha "$live_head" --payload "packet=$packet" --payload "assigned=$assigned" --payload "delivered=$delivered" --payload "failed_run=${failed_run:-none}" --payload "reason=$reason" --dedupe
  kanban_flag PM_TRANSITION "capture_local_required pr=$pr issue=${issue:-unknown} slot=${selected_slot:-none} packet=$packet"
  echo "PM_TRANSITION_OK command=capture-local-required pr=$pr issue=${issue:-unknown} slot=${selected_slot:-none} head=${live_head:0:8} packet=$packet assigned=$assigned delivered=$delivered next_review_at=$next_review proof=$proof reason=$reason next=\"diagnostic-only; remote capture remains required\""
}

cmd_capture_local_pass() {
  # Local capture is diagnostic-only. This typed consumer records an exact-head
  # diagnostic proof bound to a named remote-capture infrastructure defect; it
  # never satisfies capture readiness and does not remove pm-blocked:capture or
  # start CI. Remote capture remains the authoritative requirement.
  local pr="" proof="" failed_run=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      --proof) proof="${2:-}"; shift 2 ;;
      --failed-run) failed_run="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown capture-local-pass arg $1" ;;
    esac
  done
  need_num pr "$pr"
  [ -n "$failed_run" ] && need_num failed-run "$failed_run"
  [ -n "$proof" ] || die 2 "--proof is required"

  local pr_json live_head issue branch target owner_slot released_slots latest_head next_command target_args=()
  pr_json="$(gh pr view "$pr" --repo "$REPO" --json headRefOid,headRefName 2>/dev/null || true)"
  live_head="$(printf '%s' "$pr_json" | json_field headRefOid 2>/dev/null || true)"
  branch="$(printf '%s' "$pr_json" | json_field headRefName 2>/dev/null || true)"
  [ -n "$live_head" ] || die 1 "cannot read PR #$pr head"
  capture_local_proof_ok "$pr" "$live_head" "$proof" "$failed_run" || die 1 "invalid current-head capture local proof for PR #$pr head=$live_head failed_run=${failed_run:-none} proof=$proof"
  issue="$(issue_from_pr "$pr")"
  target="/tmp/capture-local-proof-${pr}-${live_head}.ok"
  if [ "$proof" != "$target" ]; then
    cp "$proof" "$target" || die 1 "failed to persist local capture proof to $target"
  fi

  # Release the diagnostic slot so it does not block normal dispatch, but keep
  # the capture blocker and remote watch obligation intact: this proof is not a
  # handoff to CI.
  owner_slot="$(slot_from_labels "$pr" "$issue")"
  capture_release_target_before_ci_start released_slots \
    "$pr" "$issue" "$branch" "capture-local-pass" "$owner_slot" || return $?
  latest_head="$(gh pr view "$pr" --repo "$REPO" --json headRefOid --jq '.headRefOid' 2>/dev/null || true)"
  [ "$latest_head" = "$live_head" ] || die 1 "PR #$pr head moved after capture slot release: proof_head=$live_head live=${latest_head:-unknown}"

  next_command="$PM_TRANSITION_SELF capture-remote-dispatch --pr $pr"
  [ -n "$issue" ] && next_command="$next_command --issue $issue"
  [ -n "$failed_run" ] && next_command="$next_command --retry-run $failed_run"
  [ -n "$issue" ] && target_args+=(--issue "$issue")
  resolve_pr_obligation_kinds "$pr" "$issue" "capture_local_passed" "proof=$target" capture_local_preflight
  upsert_obligation --kind capture_watch --severity high --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --owner pm --horizon hourly --next-review-at "$(utc_plus_minutes 10)" --title "PR #$pr remote capture still required after local diagnostic" --action "Local capture diagnostic proof recorded at $target; it does not satisfy capture readiness. Run $next_command to dispatch the authoritative remote capture; consume success only with $PM_TRANSITION_SELF capture-remote-pass --pr $pr --run <RUN_ID>." --blocker "capture_remote_watch" --evidence "proof=$target" --evidence "failed_run=${failed_run:-none}" --evidence "head=$live_head" --evidence "released_slots=${released_slots:-none}"
  record_event --source pm-transition --event capture_local_pass --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --head-sha "$live_head" --payload "proof=$target" --payload "failed_run=${failed_run:-none}" --payload "branch=${branch:-unknown}" --payload "released_slots=${released_slots:-none}" --dedupe
  kanban_flag PM_TRANSITION "capture_local_pass pr=$pr issue=${issue:-unknown} proof=$target failed_run=${failed_run:-none} released_slots=${released_slots:-none}"
  echo "PM_TRANSITION_OK command=capture-local-pass pr=$pr issue=${issue:-unknown} head=${live_head:0:8} proof=$target failed_run=${failed_run:-none} released_slots=${released_slots:-none} diagnostic=true next=\"$next_command\""
}

slot_checkout_at_exact_head() {
  # capture-local-required checkout precondition: the slot checkout must be
  # ATTACHED to the exact branch, CLEAN, with an upstream set, and HEAD exactly
  # at the 40-char PR head.  Fail-closed: dispatch never moves the checkout.
  local slot="$1" branch="$2" head="$3" path current_branch current_head
  [ -n "$slot" ] && [ -n "$branch" ] || return 1
  [[ "$head" =~ ^[0-9a-f]{40}$ ]] || return 1
  path="$(slot_checkout_path "$slot")" || return 1
  current_branch="$(slot_checkout_branch "$slot")"
  [ "$current_branch" = "$branch" ] || return 1
  current_head="$(slot_checkout_head "$slot")"
  [ "$current_head" = "$head" ] || return 1
  { [ -z "$(git -C "$path" status --porcelain 2>/dev/null || printf unreadable)" ] \
      && slot_checkout_content_clean "$slot"; } || return 1
  git -C "$path" rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1 || return 1
  return 0
}


cmd_pm_review() {
  local pr="" scope="" reason="pm-review-pending"
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      --scope) scope="${2:-}"; shift 2 ;;
      --reason) reason="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown pm-review arg $1" ;;
    esac
  done
  need_num pr "$pr"
  assert_pr_not_dependency_blocked "$pr"
  case "$scope" in
    phase-a) ;;
    merge-ready) die 2 "merge-ready-scope PM Claude review is retired; run pm-readiness-contract and then pm-transition merge-ready --pr $pr" ;;
    *) die 2 "scope must be phase-a" ;;
  esac

  local pr_json state head branch issue owner_slot released_slots meta marker loop_blocker
  pr_json="$(pr_metadata_json "$pr" || true)"
  [ -n "$pr_json" ] || die 1 "cannot read PR #$pr"
  state="$(printf '%s' "$pr_json" | json_field state)"
  head="$(printf '%s' "$pr_json" | json_field headRefOid)"
  branch="$(printf '%s' "$pr_json" | json_field headRefName)"
  [ "$state" = "OPEN" ] || die 1 "PR #$pr is not open (state=$state)"
  [ -n "$head" ] || die 1 "PR #$pr missing headRefOid"
  issue="$(issue_from_pr "$pr")"
  owner_slot="$(slot_from_labels "$pr" "$issue")"
  marker="$(pm_review_marker_path "$pr" "$head")"

  # PM_REVIEW_ATOMIC_MARKER_GUARD (#7399): the authoritative phase-a review
  # transition is atomic. The exact-head canonical marker must exist and be
  # admissible BEFORE any label/state mutation (including the loop-blocker
  # obligation upsert, slot release, the move to pm-review-pending, blocker
  # removal, issue edit, meta write, obligations, events, notifications). A
  # missing or invalid marker changes NOTHING and returns one typed refusal.
  # The canonical PM review runner materializes
  # /tmp/pm-claude-code-review-<PR>-<head>.md before this transition;
  # pm-review-done consumes the same marker contract.
  pm_review_marker_ok_for_scope "$pr" "$head" phase-a \
    || die 1 "PM phase-a review marker missing/invalid for PR #$pr head=${head:0:12} marker=$marker; materialize the exact-head canonical PM review marker first; no PR state was changed"

  # Idempotent retry: a prior exact-head phase-a pm-review that already parked
  # the PR (meta matches scope+head AND live labels already carry
  # pm-state:pm-review-pending) converges once with a no-op receipt — no
  # duplicate loop-blocker obligation, slot release, label event, obligation,
  # or notification.
  local existing_scope existing_meta_head live_state_labels
  meta="$(pm_review_meta_file "$pr")"
  existing_scope="$(pm_review_meta_get "$pr" scope 2>/dev/null || true)"
  existing_meta_head="$(pm_review_meta_get "$pr" headRefOid 2>/dev/null || true)"
  if [ "$existing_scope" = "$scope" ] && [ "$existing_meta_head" = "$head" ]; then
    live_state_labels="$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print(",".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
    if printf '%s' "$live_state_labels" | tr ',' '\n' | grep -qx 'pm-state:pm-review-pending'; then
      echo "PM_TRANSITION_OK command=pm-review pr=$pr issue=${issue:-unknown} scope=$scope head=${head:0:8} resumed=already-pending marker=$marker meta=$meta"
      return 0
    fi
  fi

  loop_blocker="$(pm_review_loop_decision_blocker "$pr" "$head" "$issue")"
    if [ -n "$loop_blocker" ]; then
      local loop_target_args=()
      [ -n "$issue" ] && loop_target_args+=(--issue "$issue")
      upsert_obligation --kind review_loop_rescope --severity high --target-type pr --target-id "$pr" --pr "$pr" ${loop_target_args[@]+"${loop_target_args[@]}"} --owner pm --horizon hourly --title "PR #$pr PM Claude loop decision must be executed before another review" --action "Do not start another same-head PM Claude review. Run the canonical PM rescue path and produce a terminal exact-head disposition. Environment overrides do not authorize another ordinary review." --blocker "review_loop_rescope_required" --evidence "$loop_blocker"
      die 42 "PM Claude loop decision already exists for PR #$pr head=${head:0:8}: $loop_blocker. Run pm-transition rescope-pr and pm-transition rescope-decide, or push a new head before another pm-review."
  fi

  mop_slots_healthy || die 1 "MoP unavailable before PM review slot release for PR #$pr"
  released_slots="$(release_target_slots "$pr" "$issue" "$branch" "pm-review:$scope" "$owner_slot")"
  assert_no_slot_owner_for_phase "$pr" "$issue" "$branch" "pm-review:$scope"

  bash "$PM_STATE" "$pr" pm-review-pending || die 1 "failed to move PR #$pr to pm-review-pending"
  local removed_blockers
  # Capture is an independent exact-head product-proof gate. Moving a PR into
  # Phase-A review must not create a window where it appears capture-complete.
  removed_blockers="$(remove_pm_blockers "$pr" "pm-blocked:capture")"
  archive_slot_ready_events_for_pr "$pr" "pm-review:$scope"
  [ -n "$issue" ] && gh issue edit "$issue" --repo "$REPO" --remove-label "status:in-progress" --add-label "status:in-review" >/dev/null 2>&1 || true

  meta="$(pm_review_meta_file "$pr")"
  python3 - "$meta" "$pr" "$issue" "$head" "$branch" "$scope" "$reason" "$released_slots" "$marker" "$removed_blockers" <<'PYSUB'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
path, pr, issue, head, branch, scope, reason, released_slots, marker, removed_blockers = sys.argv[1:]
data = {
    "schema_version": 1,
    "source": "pm-transition pm-review",
    "status": "pending",
    "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "pr": int(pr),
    "issue": int(issue) if str(issue).isdigit() else None,
    "headRefOid": head,
    "branch": branch,
    "scope": scope,
    "reason": reason,
    "released_slots": [int(x) for x in released_slots.split() if x.isdigit()],
    "expected_marker": marker,
    "removed_blockers": [x for x in removed_blockers.split() if x],
}
tmp = f"{path}.{Path(path).name}.{Path(path).parent.stat().st_ino}.tmp"
Path(tmp).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
Path(tmp).replace(path)
PYSUB

  # PM_REVIEW_CONTINUATION_OBLIGATION_V1
  local review_target_args=()
  [ -n "$issue" ] && review_target_args+=(--issue "$issue")
  resolve_pr_obligation_kinds "$pr" "$issue" "pm_review_started" \
    "head=$head scope=$scope marker=$marker" blocked_rework rework pm_review_wait dependency_unblocked_ci
  upsert_obligation --kind pm_review_pending --severity high \
    --target-type pr --target-id "$pr" --pr "$pr" \
    ${review_target_args[@]+"${review_target_args[@]}"} \
    --owner pm --horizon hourly --next-review-at "$(utc_plus_minutes 10)" \
    --title "PR #$pr exact-head PM review must continue to typed CI handoff" \
    --action "Complete the exact-head Phase-A review at $marker at exact head $head, then run /Users/rajiv/.claude/scripts/pm-transition.sh pm-review-done --pr $pr (affected-test proof is retired). Keep this obligation open until PM_TRANSITION_OK command=pm-review-done binds PR #$pr to head $head; never edit labels or start CI directly." \
    --blocker "pm_review_pending" \
    --evidence "head=$head" --evidence "branch=$branch" --evidence "scope=$scope" \
    --evidence "marker=$marker" --evidence "meta=$meta"
  record_event --source pm-transition --event pm_review_pending --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --head-sha "$head" --payload "scope=$scope" --payload "marker=$marker" --payload "released_slots=${released_slots:-none}" --payload "removed_blockers=${removed_blockers:-none}" --dedupe
  kanban_flag PM_TRANSITION "pm_review_pending pr=$pr issue=$issue scope=$scope released_slots=${released_slots:-none} removed_blockers=${removed_blockers:-none}"
  [ -n "$released_slots" ] && run_post_release_sweep "pm-review"
  echo "PM_TRANSITION_OK command=pm-review pr=$pr issue=${issue:-unknown} scope=$scope head=${head:0:8} released_slots=${released_slots:-none} removed_blockers=${removed_blockers:-none} marker=$marker meta=$meta"
}

# PM_REVIEW_DONE_TERMINAL_RECEIPT_V1: one head-bound terminal-review receipt
# per PR+exact-head tuple. The receipt is the durable exact-head review-PASS
# consumption marker: consumers (pr-state-sweep) read it and never schedule
# another same-head review, and a second pm-review-done on the same head
# resumes from it without a duplicate terminal event/obligation/label event/CI
# wave. Head drift invalidates reuse (the receipt path embeds the head);
# invalid/stale/missing markers fail closed and write no terminal PASS receipt.
pm_review_done_receipt_path() {
  printf '/tmp/pm-review-done-receipt-%s-%s.json' "$1" "$2"
}

# True when the exact-head terminal receipt exists and records a handed-off
# terminal PASS (review consumed and the transition already moved the PR on).
pm_review_done_receipt_handed_off() {
  local pr="$1" head="$2" path
  path="$(pm_review_done_receipt_path "$pr" "$head")"
  [ -f "$path" ] || return 1
  python3 - "$path" "$head" <<'PY' >/dev/null 2>&1
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
ok = (
    str(data.get("head_sha") or "") == sys.argv[2]
    and str(data.get("verdict") or "") == "PASS"
    and str(data.get("handoff_status") or "") == "handed_off"
)
raise SystemExit(0 if ok else 1)
PY
}

# PM_REVIEW_DONE_TERMINAL_RECEIPT_V1 post-capture re-entry eligibility: a
# handed-off exact-head receipt makes pm-review-done a no-op resume EXCEPT when
# the PR's live state regressed back to pm-state:pm-review-pending at the SAME
# head. That tuple must proceed through the existing sanctioned re-entry gate
# (PM_CI_GATE_SOURCE=pm-review-done state helper) exactly once so
# ci-head:<full head> is published before pm-state:qa-passed-awaiting-ci and
# one fresh exact-head CI+E2E wave fires. Under the release-policy
# simplification, the current-head PASS marker alone is eligible; affected-test
# and capture receipts are optional diagnostics and cannot block CI start. The
# readiness gate enforces exact-head binding, per-PR single-flight, and the
# functional-red local-repro rule. Any state/marker drift fails closed to the
# ordinary no-op resume.
pm_review_done_reentry_eligible() {
  local pr="$1" head="$2" pr_json="$3" labels
  labels="$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print(",".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
  # CI-start terminal state live: pm-state:qa-passed-awaiting-ci means the
  # exact-head wave was materialized; resume is a true no-op (single-flight
  # preserved, no duplicate wave).
  if printf '%s\n' "$labels" | tr ',' '\n' | grep -qx "pm-state:qa-passed-awaiting-ci"; then
    return 1
  fi
  # Sanctioned re-entry class (a): post-capture regression at the SAME head
  # back to pm-review-pending.
  if printf '%s\n' "$labels" | tr ',' '\n' | grep -qx "pm-state:pm-review-pending"; then
    pm_review_marker_ok_for_scope "$pr" "$head" phase-a
    return $?
  fi
  # Sanctioned re-entry class (b) (#7312 CI-start no-op): the handed-off
  # receipt exists but the final CI-start state was never materialized — the
  # PR is still pm-state:blocked-rework + pm-blocked:ci at the SAME exact
  # head. Continue through the existing readiness gate ONCE (exact-head
  # binding + single-flight + functional-red local-repro), which consumes slot
  # ownership atomically and fires exactly one wave.
  if printf '%s\n' "$labels" | tr ',' '\n' | grep -qx "pm-state:blocked-rework" \
      && printf '%s\n' "$labels" | tr ',' '\n' | grep -qx "pm-blocked:ci"; then
    pm_review_marker_ok_for_scope "$pr" "$head" phase-a
    return $?
  fi
  return 1
}

# Atomically persist (or update) the head-bound terminal-review receipt and, on
# the FIRST persist for the exact head, record the terminal consumption once:
# record_event pm_review_done + resolve the pm_review_pending obligation kind.
# A downstream refusal updates the same receipt with one typed
# blocked_after_review class, next owner, and wake condition; success updates
# handoff_status=handed_off. Replay of the same head never re-records the
# terminal event or obligations (idempotent by exact-head receipt presence;
# record_event --dedupe and resolve are additionally idempotent).
pm_review_done_persist_terminal_receipt() {
  local pr="$1" head="$2" issue="$3" marker="$4" scope="$5" handoff_status="$6"
  local block_class="${7:-}" next_owner="${8:-}" wake="${9:-}"
  local path marker_sha256 existing terminal_recorded
  [ -n "$pr" ] && [ -n "$head" ] && [ -n "$marker" ] || return 1
  [ -f "$marker" ] || return 1
  path="$(pm_review_done_receipt_path "$pr" "$head")"
  marker_sha256="$(shasum -a 256 "$marker" 2>/dev/null | awk '{print $1}')"
  [ -n "$marker_sha256" ] || return 1
  terminal_recorded=0
  if [ -f "$path" ]; then
    PR_REVIEW_RECEIPT_PATH="$path" PR_REVIEW_RECEIPT_HEAD="$head" python3 <<'PY' >/dev/null 2>&1 && terminal_recorded=1
import json
import os
from pathlib import Path

data = json.loads(Path(os.environ["PR_REVIEW_RECEIPT_PATH"]).read_text(encoding="utf-8"))
ok = (
    str(data.get("head_sha") or "") == os.environ["PR_REVIEW_RECEIPT_HEAD"]
    and str(data.get("verdict") or "") == "PASS"
    and data.get("terminal_recorded") is True
)
raise SystemExit(0 if ok else 1)
PY
  fi
  if [ "$terminal_recorded" != "1" ]; then
    # FIRST terminal persist for this exact head: ONE consumption record.
    resolve_pr_obligation_kinds "$pr" "$issue" "pm_review_done" "receipt=$path marker=$marker" pm_review_pending
    record_event --source pm-transition --event pm_review_done --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --head-sha "$head" --payload "scope=$scope" --payload "marker=$marker" --payload "receipt=$path" --dedupe
  fi
  python3 - "$path" "$pr" "$head" "$issue" "$marker" "$marker_sha256" "$scope" "$handoff_status" "$block_class" "$next_owner" "$wake" <<'PYSUB'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path, pr, head, issue, marker, marker_sha256, scope, handoff_status, block_class, next_owner, wake = sys.argv[1:12]
now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
existing = {}
if Path(path).is_file():
    try:
        existing = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        existing = {}
# Byte-preserving reentry: a pending persist -- or an explicit blocked
# persist with the SAME blocked class -- on an already-valid exact-head
# terminal PASS receipt that is already blocked_after_review is a NO-OP.
# Do not rewrite updated_at or re-serialize the receipt; bytes/digest stay
# stable until a real state transition (a NEW blocked class or handed_off)
# occurs. This keeps the sealed marker-PASS packet digest valid across
# pm-review-done retries and gate-refusal repeats.
existing_class = str((existing.get("blocked_after_review") or {}).get("class") or "")
same_transition = (
    (handoff_status == "pending" and not block_class)
    or (
        handoff_status == "blocked_after_review"
        and bool(block_class)
        and block_class == existing_class
    )
)
if (
    same_transition
    and existing.get("terminal_recorded") is True
    and str(existing.get("verdict") or "") == "PASS"
    and str(existing.get("head_sha") or "") == head
    and existing.get("blocked_after_review")
    and str(existing.get("handoff_status") or "") == "blocked_after_review"
):
    raise SystemExit(0)
# Monotonic handoff: a resume write (pending) must never downgrade an existing
# blocked_after_review receipt. Only an explicit new blocked class or a
# handed_off completion moves the receipt forward.
if (
    handoff_status == "pending"
    and existing.get("blocked_after_review")
    and str(existing.get("handoff_status") or "") == "blocked_after_review"
):
    handoff_status = "blocked_after_review"
data = {
    "schema_version": 1,
    "source": "pm-transition pm-review-done",
    "pr": int(pr),
    "issue": int(issue) if str(issue).isdigit() else None,
    "head_sha": head,
    "verdict": "PASS",
    "marker_path": marker,
    "marker_sha256": marker_sha256,
    "scope": scope,
    "handoff_status": handoff_status,
    "terminal_recorded": True,
    "created_at": existing.get("created_at", now),
    "updated_at": now,
    "blocked_after_review": None,
}
if block_class:
    data["blocked_after_review"] = {
        "class": block_class,
        "next_owner": next_owner,
        "wake": wake,
    }
elif handoff_status == "blocked_after_review" and existing.get("blocked_after_review"):
    data["blocked_after_review"] = existing["blocked_after_review"]
tmp = f"{path}.{Path(path).name}.{Path(path).parent.stat().st_ino}.tmp"
Path(tmp).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
Path(tmp).replace(path)
PYSUB
}

cmd_pm_review_done() {
  local pr="" pm_affected_test_proof="" capture_proof_override="" rescue_proof=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      --affected-test-proof) pm_affected_test_proof="${2:-}"; shift 2 ;;
      --capture-proof) capture_proof_override="${2:-}"; shift 2 ;;
      --rescue-proof) rescue_proof="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown pm-review-done arg $1" ;;
    esac
  done
  need_num pr "$pr"
  assert_pr_not_dependency_blocked "$pr"

  local scope meta_head live_head live_draft issue branch marker proof affected_test_proof affected_test_plan meta capture_proof released_slots latest_head_after_release pr_json loop_blocker rescue_authorized failed_affected_test_log failed_affected_test_exit
  scope="$(pm_review_meta_get "$pr" scope 2>/dev/null || true)"
  meta_head="$(pm_review_meta_get "$pr" headRefOid 2>/dev/null || true)"
  pr_json="$(pr_metadata_json "$pr" || true)"
  live_head="$(printf '%s' "$pr_json" | json_field headRefOid 2>/dev/null || true)"
  [ -n "$live_head" ] || die 1 "cannot read PR #$pr head"
  live_draft="$(printf '%s' "$pr_json" | json_field isDraft 2>/dev/null || true)"
  marker="$(pm_review_marker_path "$pr" "$live_head")"
  issue="$(issue_from_pr "$pr")"
  branch="$(printf '%s' "$pr_json" | json_field headRefName 2>/dev/null || true)"

  # PM_REVIEW_DONE_TERMINAL_RECEIPT_V1 resume: a handed-off terminal PASS
  # receipt at the exact live head means this transition already consumed the
  # review and moved the PR on. A second pm-review-done is a no-op resume from
  # the receipt — no second reviewer, duplicate terminal event/obligation,
  # duplicate label event, or duplicate CI wave. Head drift invalidates reuse
  # (the receipt path embeds the head); a different head revalidates normally.
  # One fail-closed exception (post-capture regression): when the PR's live
  # state regressed back to pm-review-pending at the SAME head after a
  # completed capture gate and the full current-head re-entry proof set is
  # still valid (marker PASS digest + affected-test .ok + capture consumed),
  # the resume yields exactly once to the sanctioned re-entry gate below so
  # ci-head:<full head> is published before pm-state:qa-passed-awaiting-ci and
  # one fresh exact-head CI+E2E wave fires. Any drift stays the no-op resume.
  if pm_review_done_receipt_handed_off "$pr" "$live_head"; then
    if pm_review_done_reentry_eligible "$pr" "$live_head" "$pr_json"; then
      : # post-capture regression at exact head — fall through to re-entry gate
    else
      echo "PM_TRANSITION_OK command=pm-review-done pr=$pr issue=${issue:-unknown} scope=${scope:-phase-a} state=pm-review-pending resumed=terminal-receipt head=${live_head:0:8} receipt=$(pm_review_done_receipt_path "$pr" "$live_head")"
      return 0
    fi
  fi

  rescue_authorized=0
  if [ -n "$rescue_proof" ]; then
    rescue_packet_authorizes_final_head "$rescue_proof" "$pr" "$live_head" \
      || die 1 "PM review completion Fable rescue packet does not authorize exact final head: pr=$pr head=$live_head packet=$rescue_proof"
    rescue_authorized=1
    marker="$rescue_proof"
  fi

  # CTO-adjudicated exact-tuple CI-start admission (Rajiv thread
  # 1786724301.511569 ts 1786725255.074339 + CTO decision thread
  # 1786717451.157469 ts 1786724519.596549): for exactly three allowlisted
  # PR/head tuples, the canonical one-use override at
  # /tmp/pm-ci-start-override-<pr>-<head>.ok is the admission evidence. The
  # sealed post-capture-preflight handoff (#7275) discharges the STALE
  # same-head review-loop breaker (OUT_OF_SCOPE_CTO_RESCUE) so the CTO-owned
  # cto_ci_wave_required handoff reaches qa-passed-awaiting-ci + exact
  # ci-head; the vacuous-red tuples (#7289/#7331) validate exact evidence
  # digests + capture_required=false + the exact gate set. The readiness gate
  # performs full sealed-packet validation and one-time atomic consumption;
  # no review/rescue/local-repro/capture rerun, no merge authority, and PM
  # never fires CI.
  local exact_tuple_override=""
  if [ -f "/tmp/pm-ci-start-override-${pr}-${live_head}.ok" ] \
    && { affected_test_exact_tuple_override_ok \
          "$pr" "$live_head" "/tmp/pm-ci-start-override-${pr}-${live_head}.ok" \
        || affected_test_cancelled_run_override_ok \
          "$pr" "$live_head" "/tmp/pm-ci-start-override-${pr}-${live_head}.ok" \
        || affected_test_local_preflight_rebind_override_ok \
          "$pr" "$live_head" "/tmp/pm-ci-start-override-${pr}-${live_head}.ok"; }; then
    exact_tuple_override="/tmp/pm-ci-start-override-${pr}-${live_head}.ok"
    marker="$exact_tuple_override"
    pm_affected_test_proof="$exact_tuple_override"
  fi

  loop_blocker="$(pm_review_loop_decision_blocker "$pr" "$live_head" "$issue")"
  if [ -n "$loop_blocker" ] && [ "$rescue_authorized" != "1" ] && [ -z "$exact_tuple_override" ]; then
    # PM_REVIEW_DONE_TERMINAL_RECEIPT_V1: the review is terminal PASS at the
    # exact head even though the downstream rescue gate refuses. Persist the
    # head-bound receipt with the typed rescue-required blocker/wake BEFORE
    # the die-42 exit, guarded by "marker PASS at exact head"; an invalid or
    # stale marker fails closed and writes no terminal PASS receipt.
    if pm_review_marker_ok_for_scope "$pr" "$live_head" phase-a; then
      pm_review_done_persist_terminal_receipt "$pr" "$live_head" "$issue" "$marker" phase-a blocked_after_review rescue_required PM "rescue terminal at exact head; next transition=pm-review-done --rescue-proof or CTO rescue"
    fi
    die 42 "OUT_OF_SCOPE_CTO_RESCUE PR #$pr head=${live_head:0:8}: $loop_blocker. pm-review-done cannot consume an ordinary PASS after the review-loop circuit breaker."
  fi

  case "$scope" in
    phase-a) ;;
    merge-ready) die 2 "merge-ready-scope PM Claude review is retired; run pm-readiness-contract and then pm-transition merge-ready --pr $pr" ;;
    *)
      scope=""
      ;;
  esac

  if [ "$rescue_authorized" = "1" ]; then
    scope="phase-a"
    meta_head="$live_head"
  elif [ -n "$exact_tuple_override" ]; then
    scope="phase-a"
    meta_head="$live_head"
  elif [ "$scope" != "phase-a" ] || [ "$live_head" != "$meta_head" ]; then
    local prior_scope="$scope" prior_head="$meta_head" adopted_proof adopted_plan
    if pm_review_marker_ok_for_scope "$pr" "$live_head" phase-a; then
      # Release-policy simplification: the current-head phase-a PASS marker
      # alone adopts the exact-head metadata. The affected-test proof is an
      # optional diagnostic, not a CI-start prerequisite.
      adopted_proof="$(affected_test_proof_for_head "$pr" "$live_head" "" || true)"
      adopted_plan="$(awk -F': ' '/^plan:[[:space:]]*/ {print $2; exit}' "$adopted_proof" 2>/dev/null || true)"
      scope="phase-a"
      meta_head="$live_head"
      affected_test_proof="$adopted_proof"
      affected_test_plan="$adopted_plan"
      meta="$(pm_review_meta_file "$pr")"
      python3 - "$meta" "$pr" "$issue" "$live_head" "$branch" "$scope" "$marker" "$affected_test_proof" "$affected_test_plan" "$prior_scope" "$prior_head" <<'PYSUB'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path, pr, issue, head, branch, scope, marker, affected_test_proof, affected_test_plan, prior_scope, prior_head = sys.argv[1:]
data = {
    "schema_version": 1,
    "source": "pm-transition pm-review-done/adopt-current-head-pass",
    "status": "pending",
    "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "pr": int(pr),
    "issue": int(issue) if str(issue).isdigit() else None,
    "headRefOid": head,
    "branch": branch,
    "scope": scope,
    "reason": "adopt-current-head-pass",
    "released_slots": [],
    "expected_marker": marker,
    "affected_test_proof": affected_test_proof or None,
    "affected_test_plan": affected_test_plan or None,
    "adopted": True,
    "prior_scope": prior_scope or None,
    "prior_headRefOid": prior_head or None,
}
tmp = f"{path}.{Path(path).name}.{Path(path).parent.stat().st_ino}.tmp"
Path(tmp).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
Path(tmp).replace(path)
PYSUB
      record_event --source pm-transition --event pm_review_current_head_adopted --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --head-sha "$live_head" --payload "prior_scope=${prior_scope:-none}" --payload "prior_head=${prior_head:-none}" --payload "marker=$marker" --payload "affected_test_proof=$affected_test_proof" --dedupe
      kanban_flag PM_TRANSITION "pm_review_current_head_adopted pr=$pr issue=${issue:-unknown} marker=$marker proof=$affected_test_proof"
    elif [ -z "$scope" ]; then
      die 1 "missing/invalid pm-review metadata for PR #$pr; run pm-transition pm-review --scope phase-a first"
    else
      die 1 "PR #$pr head drift: pm-review=$meta_head live=$live_head"
    fi
  fi

  if [ "$rescue_authorized" != "1" ] && [ -z "$exact_tuple_override" ]; then
    pm_review_marker_ok_for_scope "$pr" "$live_head" "$scope" || die 1 "PM Claude review marker missing/invalid for PR #$pr scope=$scope marker=$marker"
  fi

  # PM_REVIEW_DONE_TERMINAL_RECEIPT_V1: the exact-head terminal marker just
  # validated. Atomically persist ONE head-bound terminal-review receipt
  # (PR, H, marker path+digest, PASS, timestamp, handoff status) and record the
  # terminal consumption exactly once, BEFORE any downstream-gate exit. A
  # downstream refusal updates the same receipt with a typed
  # blocked_after_review class, next owner, and wake condition.
  pm_review_done_persist_terminal_receipt "$pr" "$live_head" "$issue" "$marker" "$scope" pending

  if [ -z "${affected_test_proof:-}" ]; then
    affected_test_proof="$(pm_review_meta_get "$pr" affected_test_proof 2>/dev/null || true)"
  fi
  [ -n "$pm_affected_test_proof" ] && affected_test_proof="$pm_affected_test_proof"
  if [ -z "${affected_test_plan:-}" ]; then
    affected_test_plan="$(pm_review_meta_get "$pr" affected_test_plan 2>/dev/null || true)"
  fi
  # Release-policy simplification (Rajiv thread 1786636554.182149 ts
  # 1786641626.977319): affected-test planner receipts must not block CI
  # start. They remain optional diagnostics carried on the promotion proof.
  affected_test_proof="$(affected_test_proof_for_head "$pr" "$live_head" "$affected_test_proof" || true)"
  if [ -z "${affected_test_plan:-}" ]; then
    affected_test_plan="$(affected_test_plan_for_proof "$pr" "$live_head" "$affected_test_proof" || true)"
  fi
  # Release-policy simplification: capture receipts and review/override
  # ceremony must not block CI start. They remain optional diagnostics.
  capture_proof=""

  local phase_a_authorized="0"
  local already_green="0"
  if ! ci_ready_gate_ok "$pr" "$live_head" "pm-review-done" "$affected_test_proof" "$rescue_authorized" "$phase_a_authorized"; then
    if pm_review_done_already_green_ok "$pr" "$live_head"; then
      already_green="1"
    else
      pm_review_done_persist_terminal_receipt "$pr" "$live_head" "$issue" "$marker" "$scope" blocked_after_review ci_admission_refusal slot/PM "CI-start gate refused at exact head; inspect /tmp/pm-ci-ready-gate-${pr}-${live_head}.json. No affected-test/local-test proof is required (Rajiv thread 1786947023.747929); route product/uncertain red to assign-rework, infra/flake/shared to one same-head rerun."
      die 1 "PR #$pr failed CI-start readiness gate for head ${live_head:0:10}. Inspect /tmp/pm-ci-ready-gate-${pr}-${live_head}.json; the gate now enforces only exact-head binding and per-PR single-flight (no duplicate active wave) plus the functional-red local-repro rule."
    fi
  fi

  capture_release_target_before_ci_start released_slots \
    "$pr" "$issue" "$branch" "pm-review-done" "" || return $?
  latest_head_after_release="$(gh pr view "$pr" --repo "$REPO" --json headRefOid --jq '.headRefOid' 2>/dev/null || true)"
  [ "$latest_head_after_release" = "$live_head" ] || die 1 "PR #$pr head moved after slot release before CI: release_head=$live_head live=$latest_head_after_release"

  if ci_ready_gate_control_plane_exempt "$pr" "$live_head"; then
    local ready_proof rules_sha256 classifier_sha256 ready_content
    ready_proof="$(merge_ready_proof_path "$pr")"
    rules_sha256="$(ci_ready_gate_change_scope_field "$pr" "$live_head" rules_sha256)" \
      || die 1 "cannot read control-plane rules digest for PR #$pr"
    classifier_sha256="$(ci_ready_gate_change_scope_field "$pr" "$live_head" classifier_sha256)" \
      || die 1 "cannot read control-plane classifier digest for PR #$pr"
    ready_content="$({
      printf 'READY_PACKET: PASS\n'
      printf 'PR: %s\n' "$pr"
      printf 'issue: %s\n' "${issue:-unknown}"
      printf 'headRefOid: %s\n' "$live_head"
      printf 'branch: %s\n' "$branch"
      printf 'review_provenance: ok\n'
      if [ "$rescue_authorized" = "1" ]; then
        printf 'PM_FABLE_RESCUE: PASS\n'
        printf 'fable_rescue_packet: %s\n' "$marker"
      else
        printf 'PM_CLAUDE_REVIEW: PASS\n'
        printf 'claude_review_marker: %s\n' "$marker"
      fi
      printf 'affected_test_proof: %s\n' "${affected_test_proof:-}"
      printf 'affected_test_plan: %s\n' "${affected_test_plan:-}"
      printf 'control_plane_ci_exempt: true\n'
      printf 'change_scope: control_plane_only\n'
      printf 'change_scope_head: %s\n' "$live_head"
      printf 'change_scope_rules_sha256: %s\n' "$rules_sha256"
      printf 'change_scope_classifier_sha256: %s\n' "$classifier_sha256"
    })"
    write_promotion_proof_atomic "$ready_proof" "$pr" "merge-ready" "$live_head" "$ready_content" \
      || die 1 "refused to write exact-head control-plane merge-ready proof for PR #$pr"

    if [ "$live_draft" = "true" ]; then
      gh pr ready "$pr" --repo "$REPO" >/dev/null \
        || die 1 "failed to mark PR #$pr ready before control-plane merge-ready"
    fi
    cmd_merge_ready --pr "$pr"
    record_event --source pm-transition --event control_plane_ci_exempted \
      --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" \
      --head-sha "$live_head" --payload "source=pm-review-done" \
      --payload "proof=$ready_proof" --payload "rules_sha256=$rules_sha256" \
      --payload "classifier_sha256=$classifier_sha256" --dedupe
    pm_review_done_persist_terminal_receipt "$pr" "$live_head" "$issue" "$marker" "$scope" handed_off
    echo "PM_TRANSITION_OK command=pm-review-done pr=$pr issue=${issue:-unknown} scope=$scope state=merge-ready ci=exempt-control-plane-only head=${live_head:0:8} proof=$ready_proof marker=$marker rescue_authorized=$rescue_authorized released_slots=${released_slots:-none}"
    return 0
  fi

  proof="/tmp/pm-state-promotion-proof-${pr}-qa-passed-awaiting-ci.ok"
  local proof_content
  proof_content="$({
    printf 'CURRENT_HEAD_REVIEW_OK\n'
    printf 'PR: %s\n' "$pr"
    printf 'issue: %s\n' "${issue:-unknown}"
    printf 'headRefOid: %s\n' "$live_head"
    printf 'branch: %s\n' "$branch"
    printf 'released_slots: %s\n' "${released_slots:-none}"
    printf 'latest_rework_sha: %s\n' "$live_head"
    if [ "$rescue_authorized" = "1" ]; then
      printf 'PM_FABLE_RESCUE: PASS\n'
      printf 'fable_rescue_packet: %s\n' "$marker"
    elif [ -n "$exact_tuple_override" ]; then
      printf 'CTO_EXACT_TUPLE_CI_ADMISSION: PASS\n'
      printf 'cto_exact_tuple_override: %s\n' "$exact_tuple_override"
    else
      printf 'PM_CLAUDE_REVIEW: PASS\n'
      printf 'claude_review_marker: %s\n' "$marker"
    fi
    printf 'affected_test_proof: %s\n' "${affected_test_proof:-}"
    [ -n "$pm_affected_test_proof" ] && printf 'pm_owned_affected_test_proof: %s\n' "$pm_affected_test_proof"
    [ -n "$capture_proof" ] && printf 'capture_proof: %s\n' "$capture_proof"
    [ -n "$capture_proof_override" ] && printf 'pm_owned_capture_proof: %s\n' "$capture_proof_override"
    printf 'affected_test_plan: %s\n' "${affected_test_plan:-}"
    printf 'review_provenance:ok\n'
  })"
  write_promotion_proof_atomic "$proof" "$pr" "qa-passed-awaiting-ci" "$live_head" "$proof_content" \
    || die 1 "refused to replace promotion proof for PR #$pr: candidate was invalid or could not be written atomically"

  if [ "$live_draft" = "true" ]; then
    gh pr ready "$pr" --repo "$REPO" >/dev/null || die 1 "failed to mark PR #$pr ready before triggering CI"
  fi

	  if [ "$already_green" = "1" ]; then
	    record_event --source pm-transition --event pm_review_done_already_green --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --head-sha "$live_head" --payload "source=pm-review-done" --payload "proof=$proof" --payload "ci_run_id=${PM_ALREADY_GREEN_CI_RUN_ID}" --payload "e2e_run_id=${PM_ALREADY_GREEN_E2E_RUN_ID}" --payload "released_slots=${released_slots:-none}" --dedupe-key "pm_review_done_already_green:${pr}:${live_head}"
	    materialize_ci_reconcile_wake "$pr" "$live_head" "$issue" "$PM_ALREADY_GREEN_CI_RUN_ID" "$PM_ALREADY_GREEN_E2E_RUN_ID" "$proof"
	  else
	    local cleared_review_blockers="" review_blocker
	    if [ "$rescue_authorized" = "1" ]; then
	      for review_blocker in pm-blocked:codex pm-blocked:ci pm-blocked:pm-gate; do
	        if gh pr view "$pr" --repo "$REPO" --json labels --jq '.labels[].name' 2>/dev/null | grep -qx "$review_blocker"; then
	          gh pr edit "$pr" --repo "$REPO" --remove-label "$review_blocker" >/dev/null \
	            || die 1 "failed to clear rescue-discharged $review_blocker on PR #$pr"
	          cleared_review_blockers="${cleared_review_blockers}${cleared_review_blockers:+ }${review_blocker}"
	        fi
	      done
	    elif gh pr view "$pr" --repo "$REPO" --json labels --jq '.labels[].name' 2>/dev/null | grep -qx "pm-blocked:codex"; then
	      gh pr edit "$pr" --repo "$REPO" --remove-label "pm-blocked:codex" >/dev/null \
	        || die 1 "failed to clear Phase-A-discharged pm-blocked:codex on PR #$pr"
	      cleared_review_blockers="pm-blocked:codex"
	    fi
	    if ! PM_CI_GATE_SOURCE="pm-review-done" PM_REVIEW_RESCUE_AUTHORIZED="$rescue_authorized" bash "$PM_STATE" "$pr" qa-passed-awaiting-ci; then
	      for review_blocker in $cleared_review_blockers; do
	        gh pr edit "$pr" --repo "$REPO" --add-label "$review_blocker" >/dev/null \
	          || die 1 "failed to restore $review_blocker after CI admission failed on PR #$pr"
	      done
	      die 1 "failed to move PR #$pr to qa-passed-awaiting-ci"
	    fi
	    if [ -n "$exact_tuple_override" ]; then
	      # Atomic one-time consumption guard: the final label-control gate
	      # (pm-state-replace --commit-reentry) must have marked the one-use
	      # exact-tuple override consumed before the transition is terminal.
	      # A packet left consumable would be replayable into a second wave;
	      # fail closed instead of printing PM_TRANSITION_OK.
	      if ! grep -qE '^consumed:[[:space:]]*yes($|[[:space:]])' "$exact_tuple_override" \
	        || ! grep -qE '^consumed_marker:[[:space:]]*sha256:[0-9a-f]{64}($|[[:space:]])' "$exact_tuple_override"; then
	        die 1 "PR #$pr exact-tuple override was not atomically consumed by the final gate: $exact_tuple_override"
	      fi
	    fi
	    record_event --source pm-transition --event ci_requested --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --head-sha "$live_head" --payload "mode=initial" --payload "source=pm-review-done" --payload "proof=$proof" --payload "released_slots=${released_slots:-none}" --dedupe-key "ci_requested:${pr}:${live_head}:initial"
	  fi
	  archive_slot_ready_events_for_pr "$pr" "pm-review-done:$scope"
	  # PM_REVIEW_DONE_TERMINAL_RECEIPT_V1: the terminal event/obligation were
	  # recorded once on the first persist (before the gates); this success
	  # update only moves the head-bound receipt to handed_off (no duplicate
	  # terminal event, no duplicate label event or CI wave on replay).
	  pm_review_done_persist_terminal_receipt "$pr" "$live_head" "$issue" "$marker" "$scope" handed_off
	  if [ -n "$exact_tuple_override" ]; then
	    # CTO exact-tuple admission consumes the sealed post-capture handoff:
	    # resolve the CTO-owned cto_ci_wave_required obligation (for #7275 that
	    # is obligation 12959, kind ci_rerun_after_preflight) plus the
	    # ci-local-preflight/reconcile siblings. No PM-fired CI, no rerun.
	    resolve_pr_obligation_kinds "$pr" "$issue" "pm_review_done" "proof=$proof marker=$marker" pm_review_pending review_loop_rescope rescope_final_patch capture_watch capture_local_preflight capture_rerun_after_local_proof ci_local_preflight ci_rerun_after_preflight ci_reconcile
	  else
	    resolve_pr_obligation_kinds "$pr" "$issue" "pm_review_done" "proof=$proof marker=$marker" pm_review_pending review_loop_rescope rescope_final_patch capture_watch capture_local_preflight capture_rerun_after_local_proof
	  fi
  kanban_flag PM_TRANSITION "pm_review_done pr=$pr issue=$issue scope=$scope proof=$proof released_slots=${released_slots:-none}"
  if [ "$already_green" = "1" ]; then
    echo "PM_TRANSITION_OK command=pm-review-done pr=$pr issue=${issue:-unknown} scope=$scope state=pm-review-pending already_green=1 ci_run_id=${PM_ALREADY_GREEN_CI_RUN_ID} e2e_run_id=${PM_ALREADY_GREEN_E2E_RUN_ID} head=${live_head:0:8} proof=$proof marker=$marker rescue_authorized=$rescue_authorized released_slots=${released_slots:-none} next=pm-readiness-contract"
  else
    transition_alert --event qa-passed-awaiting-ci --pr "$pr" --issue "${issue:-}" --state qa-passed-awaiting-ci --head "$live_head" --branch "$branch" --proof "$proof" --released-slots "${released_slots:-none}"
    echo "PM_TRANSITION_OK command=pm-review-done pr=$pr issue=${issue:-unknown} scope=$scope state=qa-passed-awaiting-ci head=${live_head:0:8} proof=$proof marker=$marker rescue_authorized=$rescue_authorized released_slots=${released_slots:-none}"
  fi
}

# Fail-fast shell validation of the five typed ci-stale-run-classified closure
# holds (CTO disposition CI_BLOCKER_CLEAR_TRANSITION_MISSING, thread
# C0ALZJHGE49/1786572299.268999). The CI-start gate remains the final
# authority and re-validates every hold end-to-end through
# --ci-stale-run-classified; this helper gives the operator an immediate typed
# refusal and never mutates state. Required-workflow membership of the named
# run is enforced by the gate (REQUIRED_WORKFLOWS from required-ci-jobs.json),
# not duplicated here.
ci_stale_run_classified_holds_ok() {
  local pr="$1" head="$2" run="$3" marker="$4"
  [ -f "$marker" ] || { printf 'Fable marker missing: %s\n' "$marker"; return 1; }
  grep -qE '^PM_CLAUDE_REVIEW:[[:space:]]*PASS($|[[:space:]])' "$marker" 2>/dev/null \
    || { printf 'Fable marker is not PASS: %s\n' "$marker"; return 1; }
  grep -Fq "headRefOid: ${head}" "$marker" 2>/dev/null \
    || { printf 'Fable marker head mismatch for head %s\n' "$head"; return 1; }
  grep -qE '^review_model:[[:space:]]*fable($|[[:space:]])' "$marker" 2>/dev/null \
    || { printf 'Fable marker review_model is not fable: %s\n' "$marker"; return 1; }
  grep -qE '^blocker_reviewed:[[:space:]]*pm-blocked:ci($|[[:space:]])' "$marker" 2>/dev/null \
    || { printf 'Fable marker blocker_reviewed is not pm-blocked:ci: %s\n' "$marker"; return 1; }
  # Sealed local-preflight envelopes are retired (Rajiv 1786812200.371389):
  # the superseded failed-run binding plus the exact-head review marker are
  # sufficient for this typed closure.
  local run_json
  run_json="$(gh api "repos/${REPO}/actions/runs/${run}" --jq '{event,name,status,conclusion,head_sha}' 2>/dev/null || true)"
  [ -n "$run_json" ] || { printf 'failed run %s unreadable\n' "$run"; return 1; }
  # The JSON travels through argv, not stdin: a heredoc on the same command
  # would override the pipe as python's stdin and parse the script body.
  python3 - "$head" "$run_json" <<'PY' >/dev/null 2>&1 \
    || { printf 'failed run %s is not a completed terminal-bad pull_request run at a superseded head\n' "$run"; return 1; }
import json
import sys

try:
    run = json.loads(sys.argv[2])
except Exception:
    sys.exit(1)
head = sys.argv[1]
if run.get("event") != "pull_request":
    sys.exit(1)
if str(run.get("status") or "").lower() != "completed":
    sys.exit(1)
if str(run.get("conclusion") or "").lower() not in {"failure", "cancelled", "timed_out", "action_required"}:
    sys.exit(1)
if str(run.get("head_sha") or "") == head:
    sys.exit(1)
sys.exit(0)
PY
  return 0
}

cmd_ci_stale_run_classified() {
  local pr="" run="" issue=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      --run) run="${2:-}"; shift 2 ;;
      --issue) issue="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown ci-stale-run-classified arg $1" ;;
    esac
  done
  need_num pr "$pr"
  need_num run "$run"
  assert_pr_not_dependency_blocked "$pr"

  local pr_json state live_head branch labels other_states live_ci_blocker
  local marker affected_test_proof affected_test_plan released_slots latest_head_after_release
  pr_json="$(gh pr view "$pr" --repo "$REPO" --json state,headRefOid,headRefName,labels 2>/dev/null || true)"
  [ -n "$pr_json" ] || die 1 "cannot read PR #$pr"
  state="$(printf '%s' "$pr_json" | json_field state 2>/dev/null || true)"
  [ "$state" = "OPEN" ] || die 1 "PR #$pr is not open"
  live_head="$(printf '%s' "$pr_json" | json_field headRefOid 2>/dev/null || true)"
  branch="$(printf '%s' "$pr_json" | json_field headRefName 2>/dev/null || true)"
  [ -n "$live_head" ] || die 1 "PR #$pr has no headRefOid"
  labels="$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print("\n".join(x.get("name", "") for x in json.load(sys.stdin).get("labels", [])))' 2>/dev/null || true)"
  if [ -z "$issue" ]; then issue="$(issue_from_pr "$pr")"; fi
  [ -z "$issue" ] || need_num issue "$issue"

  # The typed closure clears ONLY pm-blocked:ci. The blocker must be live (a
  # replay after success refuses deterministically instead of re-firing CI)
  # and the pm-state must be one of the two admitted states in which
  # ci-watch/verdict rework sets pm-blocked:ci: qa-passed-awaiting-ci
  # (classified same-head hold) or blocked-rework (rework-packet disposition).
  live_ci_blocker=0
  if printf '%s\n' "$labels" | grep -qx 'pm-blocked:ci'; then live_ci_blocker=1; fi
  [ "$live_ci_blocker" = "1" ] \
    || die 43 "ci-stale-run-classified requires live pm-blocked:ci on PR #$pr (replay after success refuses)"
  other_states="$(printf '%s\n' "$labels" | grep '^pm-state:' | grep -vE '^(pm-state:qa-passed-awaiting-ci|pm-state:blocked-rework)$' || true)"
  [ -z "$other_states" ] \
    || die 43 "ci-stale-run-classified refuses contradictory PM state labels on PR #$pr: $other_states"
  printf '%s\n' "$labels" | grep -qxE '^(pm-state:qa-passed-awaiting-ci|pm-state:blocked-rework)$' \
    || die 43 "ci-stale-run-classified requires pm-state:qa-passed-awaiting-ci or pm-state:blocked-rework on PR #$pr"

  marker="$(pm_review_marker_path "$pr" "$live_head")"
  ci_stale_run_classified_holds_ok "$pr" "$live_head" "$run" "$marker" \
    || die 1 "ci-stale-run-classified five-hold check failed for PR #$pr head=${live_head:0:10} run=$run"

  if ! ci_ready_gate_ok "$pr" "$live_head" "pm-review-done" "" 0 0 "$run"; then
    die 1 "PR #$pr failed CI-start readiness gate for head ${live_head:0:10} under the stale-run closure run=$run. Inspect /tmp/pm-ci-ready-gate-${pr}-${live_head}.json; the gate re-validates the Fable marker and the superseded failed run end-to-end."
  fi
  # The gate JSON must bind the exact live head AND show the closure artifact
  # admitted for the named run; a raw pass without the closure artifact is not
  # this transition's admission.
  python3 - "/tmp/pm-ci-ready-gate-${pr}-${live_head}.json" "$live_head" "$run" <<'PY' >/dev/null 2>&1 \
    || die 1 "PR #$pr CI-start gate JSON does not bind the stale-run closure at head ${live_head:0:10}: run=$run"
import json
import sys

data = json.load(open(sys.argv[1]))
head, run = sys.argv[2], int(sys.argv[3])
if data.get("ok") is not True:
    sys.exit(1)
if data.get("headRefOid") != head:
    sys.exit(1)
artifact = (data.get("artifacts") or {}).get("ci_stale_run_classified") or {}
if artifact.get("ok") is not True or int(artifact.get("run_id") or 0) != run:
    sys.exit(1)
sys.exit(0)
PY

  # Land the admitted state exactly like the pm-review-done consumption:
  # release target slots (the admitted state is slot-free), re-check the head,
  # write the exact-head promotion proof, clear ONLY pm-blocked:ci, then apply
  # pm-state:qa-passed-awaiting-ci through the canonical state machine (its
  # own internal gate re-run uses the proof-derived pm-review-done source).
  # Every sibling pm-blocked:* blocker is preserved; the gate above would have
  # refused if any remained blocking.
  capture_release_target_before_ci_start released_slots \
    "$pr" "$issue" "$branch" "ci-stale-run-classified" "" || return $?
  latest_head_after_release="$(gh pr view "$pr" --repo "$REPO" --json headRefOid --jq '.headRefOid' 2>/dev/null || true)"
  [ "$latest_head_after_release" = "$live_head" ] \
    || die 1 "PR #$pr head moved after slot release before CI: release_head=$live_head live=$latest_head_after_release"

  affected_test_plan="$(affected_test_plan_for_proof "$pr" "$live_head" "$affected_test_proof" 2>/dev/null || true)"
  local proof="/tmp/pm-state-promotion-proof-${pr}-qa-passed-awaiting-ci.ok"
  local proof_content
  proof_content="$({
    printf 'CURRENT_HEAD_REVIEW_OK\n'
    printf 'PR: %s\n' "$pr"
    printf 'issue: %s\n' "${issue:-unknown}"
    printf 'headRefOid: %s\n' "$live_head"
    printf 'branch: %s\n' "$branch"
    printf 'released_slots: %s\n' "${released_slots:-none}"
    printf 'latest_rework_sha: %s\n' "$live_head"
    printf 'PM_CLAUDE_REVIEW: PASS\n'
    printf 'claude_review_marker: %s\n' "$marker"
    printf 'ci_stale_run_classified_run: %s\n' "$run"
    printf 'affected_test_proof: %s\n' "${affected_test_proof:-}"
    printf 'affected_test_plan: %s\n' "${affected_test_plan:-}"
    printf 'review_provenance:ok\n'
  })"
  write_promotion_proof_atomic "$proof" "$pr" "qa-passed-awaiting-ci" "$live_head" "$proof_content" \
    || die 1 "refused to replace promotion proof for PR #$pr: candidate was invalid or could not be written atomically"

  gh pr edit "$pr" --repo "$REPO" --remove-label "pm-blocked:ci" >/dev/null \
    || die 1 "failed to clear pm-blocked:ci on PR #$pr"

  if ! PM_CI_GATE_SOURCE="pm-review-done" bash "$PM_STATE" "$pr" qa-passed-awaiting-ci; then
    gh pr edit "$pr" --repo "$REPO" --add-label "pm-blocked:ci" >/dev/null 2>&1 || true
    die 1 "failed to move PR #$pr to qa-passed-awaiting-ci; pm-blocked:ci restored"
  fi

  record_event --source pm-transition --event ci_requested --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --head-sha "$live_head" --payload "mode=initial" --payload "source=ci-stale-run-classified" --payload "proof=$proof" --payload "failed_run=$run" --payload "released_slots=${released_slots:-none}" --dedupe-key "ci_requested:${pr}:${live_head}:initial"
  record_event --source pm-transition --event ci_stale_run_classified --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --head-sha "$live_head" --payload "failed_run=$run" --payload "proof=$proof" --payload "marker=$marker" --payload "cleared=pm-blocked:ci" --payload "released_slots=${released_slots:-none}" --dedupe-key "ci_stale_run_classified:${pr}:${live_head}:${run}"
  resolve_pr_obligation_kinds "$pr" "$issue" "ci_stale_run_classified" "proof=$proof run=$run marker=$marker" ci_watch blocked_rework ci_local_preflight ci_rerun_after_preflight ci_reconcile
  kanban_flag PM_TRANSITION "ci_stale_run_classified pr=$pr issue=${issue:-unknown} head=${live_head:0:10} run=$run released_slots=${released_slots:-none}"
  transition_alert --event qa-passed-awaiting-ci --pr "$pr" --issue "${issue:-}" --state qa-passed-awaiting-ci --head "$live_head" --branch "$branch" --proof "$proof" --released-slots "${released_slots:-none}"
  run_post_release_sweep "ci-stale-run-classified"
  echo "PM_TRANSITION_OK command=ci-stale-run-classified pr=$pr issue=${issue:-unknown} state=qa-passed-awaiting-ci head=${live_head:0:8} run=$run cleared=pm-blocked:ci proof=$proof marker=$marker released_slots=${released_slots:-none}"
}

# Exact-head CI admission marker helpers for the re-arm path, mirroring
# pm-state-replace.sh's remove-then-publish sequence. The paid CI/E2E jobs
# require `contains(labels, 'ci-head:<event head sha>')` in the `labeled`
# event snapshot, so the current-head marker must be applied BEFORE the
# pm-state label event that starts the paid cycle; a stale marker from the
# previous cycle must be removed first so it cannot ride the event.
remove_ci_head_markers() {
  local pr="$1" markers label encoded_label
  markers="$(gh api "repos/${REPO}/issues/${pr}" --jq '[.labels[] | select(.name | startswith("ci-head:")) | .name]' 2>/dev/null)" || {
    echo "ERROR: cannot read CI head markers for PR #${pr}" >&2
    return 1
  }
  while IFS= read -r label; do
    [ -z "$label" ] && continue
    encoded_label="$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$label")"
    gh api --method DELETE "repos/${REPO}/issues/${pr}/labels/${encoded_label}" >/dev/null 2>&1 || {
      echo "ERROR: cannot remove stale CI head marker ${label} from PR #${pr}" >&2
      return 1
    }
  done < <(printf '%s\n' "${markers:-[]}" | jq -r '.[]')
}

publish_ci_head_marker() {
  local pr="$1" head="$2" label encoded_label
  label="ci-head:${head}"
  encoded_label="$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$label")"
  if ! gh api "repos/${REPO}/labels/${encoded_label}" >/dev/null 2>&1; then
    if ! gh api --method POST "repos/${REPO}/labels" \
      -f "name=${label}" -f color=1d76db \
      -f "description=Exact-head paid CI admission for PR #${pr}" >/dev/null 2>&1; then
      gh api "repos/${REPO}/labels/${encoded_label}" >/dev/null 2>&1 || {
        echo "ERROR: cannot create CI head marker label ${label}" >&2
        return 1
      }
    fi
  fi
  if ! printf '{"labels":[%s]}\n' "$(printf '%s' "$label" | jq -R .)" \
    | gh api --method POST "repos/${REPO}/issues/${pr}/labels" --input - >/dev/null 2>&1; then
    echo "ERROR: cannot apply CI head marker ${label} to PR #${pr}" >&2
    return 1
  fi
}

cmd_ci_rearm_after_main_refresh() {
  local pr="" prior_head="" affected_test_proof="" pr_json="" live_head="" branch=""
  local live_state="" live_draft="" labels="" issue="" current_main="" topology=""
  local runs_json="" run_summary="" prior_ci_run="" prior_e2e_run="" receipt_root=""
  local receipt="" latest_head="" post_json="" post_head="" post_labels=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      --prior-head) prior_head="${2:-}"; shift 2 ;;
      --affected-test-proof) affected_test_proof="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown ci-rearm-after-main-refresh arg $1" ;;
    esac
  done
  need_num pr "$pr"
  [[ "$prior_head" =~ ^[0-9a-f]{40}$ ]] \
    || die 2 "prior-head must be a full lowercase commit SHA"
  [ -x "$CI_MAIN_REFRESH_REARM_HELPER" ] \
    || die 70 "ci main-refresh verifier is not executable: $CI_MAIN_REFRESH_REARM_HELPER"
  [ -d "$CI_MAIN_REFRESH_REARM_REPO/.git" ] \
    || die 70 "ci main-refresh repo is unavailable: $CI_MAIN_REFRESH_REARM_REPO"

  pr_json="$(gh pr view "$pr" --repo "$REPO" \
    --json state,isDraft,headRefOid,headRefName,labels 2>/dev/null)" \
    || die 1 "cannot read PR #$pr live state"
  live_state="$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state") or "")')"
  live_draft="$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print(str(bool(json.load(sys.stdin).get("isDraft"))).lower())')"
  live_head="$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("headRefOid") or "")')"
  branch="$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("headRefName") or "")')"
  labels="$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print("\n".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))')"
  [ "$live_state" = "OPEN" ] || die 1 "PR #$pr is not OPEN"
  [ "$live_draft" = "false" ] || die 1 "PR #$pr is draft"
  [[ "$live_head" =~ ^[0-9a-f]{40}$ ]] || die 1 "PR #$pr current head is invalid"
  [ "$live_head" != "$prior_head" ] || die 1 "PR #$pr current head has not advanced"
  [ -n "$branch" ] || die 1 "PR #$pr branch is missing"
  [ "$(printf '%s\n' "$labels" | grep -c '^pm-state:' || true)" -eq 1 ] \
    || die 1 "PR #$pr must have exactly one pm-state label"
  printf '%s\n' "$labels" | grep -qx 'pm-state:qa-passed-awaiting-ci' \
    || die 1 "PR #$pr is not in pm-state:qa-passed-awaiting-ci"
  if printf '%s\n' "$labels" | grep -qE '^(pm-blocked:|cto-rescue:in-progress$|slot:[1-4]$)'; then
    die 1 "PR #$pr has blocker, CTO-rescue, or slot-owner labels; ordinary recovery must run first"
  fi

  issue="$(issue_from_pr "$pr" 2>/dev/null || true)"
  if pr_requires_fresh_capture_before_ci "$pr"; then
    pm_review_capture_green_ok "$pr" "$live_head" >/dev/null 2>&1 \
      || die 1 "PR #$pr requires exact-head remote capture before CI re-arm (local capture is diagnostic-only)"
  fi
  assert_no_slot_owner_for_phase "$pr" "$issue" "$branch" "ci-rearm-after-main-refresh"

  git -C "$CI_MAIN_REFRESH_REARM_REPO" fetch --quiet origin \
    "+refs/heads/main:refs/remotes/origin/main" \
    "+refs/heads/$branch:refs/remotes/origin/$branch" \
    || die 1 "failed to refresh exact main/branch refs for PR #$pr"
  current_main="$(git -C "$CI_MAIN_REFRESH_REARM_REPO" rev-parse refs/remotes/origin/main 2>/dev/null || true)"
  [[ "$current_main" =~ ^[0-9a-f]{40}$ ]] || die 1 "cannot resolve current origin/main"

  topology="/tmp/pm-ci-main-refresh-rearm-topology-${pr}-${live_head}.json"
  local -a rearm_helper_args=(
    --repo-root "$CI_MAIN_REFRESH_REARM_REPO"
    --branch "$branch"
    --prior-head "$prior_head"
    --live-head "$live_head"
    --current-main "$current_main"
    --pr "$pr"
  )
  # Affected-test proof is retired: only pass the flag when an actual file was
  # supplied (deprecated legacy diagnostics). An empty value must be omitted so
  # argparse does not turn "" into Path(".") and reject a valid rearm.
  if [ -n "${affected_test_proof:-}" ]; then
    rearm_helper_args+=(--affected-test-proof "$affected_test_proof")
  fi
  python3 "$CI_MAIN_REFRESH_REARM_HELPER" \
    "${rearm_helper_args[@]}" \
    >"$topology" \
    || die 42 "PR #$pr current head is not a byte-verified prior-head plus main refresh"

  runs_json="/tmp/pm-ci-main-refresh-rearm-runs-${pr}-${live_head}.json"
  gh run list --repo "$REPO" --branch "$branch" --limit 100 \
    --json databaseId,workflowName,event,status,conclusion,headSha,createdAt \
    >"$runs_json" \
    || die 1 "cannot read workflow history for PR #$pr"
  run_summary="$(
    python3 - "$runs_json" "$prior_head" "$live_head" <<'PY'
import json
import sys
from pathlib import Path

path, prior_head, live_head = sys.argv[1:]
runs = json.loads(Path(path).read_text(encoding="utf-8"))
required = ("CI", "E2E Smoke Tests")
current = [
    run for run in runs
    if run.get("headSha") == live_head
    and run.get("event") == "pull_request"
    and run.get("workflowName") in required
]
if current:
    details = ",".join(
        f"{run.get('workflowName')}:{run.get('databaseId')}:{run.get('status')}:{run.get('conclusion')}"
        for run in current
    )
    print(f"current_head_real_run_exists:{details}", file=sys.stderr)
    raise SystemExit(43)

ids = {}
for workflow in required:
    candidates = [
        run for run in runs
        if run.get("headSha") == prior_head
        and run.get("event") == "pull_request"
        and run.get("workflowName") == workflow
        and run.get("status") == "completed"
        and str(run.get("conclusion") or "").lower() not in {"", "skipped"}
    ]
    if not candidates:
        print(f"prior_head_real_run_missing:{workflow}:{prior_head}", file=sys.stderr)
        raise SystemExit(44)
    candidates.sort(key=lambda run: str(run.get("createdAt") or ""), reverse=True)
    ids[workflow] = str(candidates[0].get("databaseId") or "")
print(ids["CI"] + "\t" + ids["E2E Smoke Tests"])
PY
  )" || die 44 "PR #$pr lacks the required prior-head real CI/E2E attempt or already has a current-head real run"
  IFS=$'\t' read -r prior_ci_run prior_e2e_run <<<"$run_summary"
  [[ "$prior_ci_run" =~ ^[0-9]+$ ]] || die 44 "prior-head CI run ID is invalid"
  [[ "$prior_e2e_run" =~ ^[0-9]+$ ]] || die 44 "prior-head E2E run ID is invalid"

  # Reserve the exact PR/head before any label mutation. A pending or complete
  # receipt is intentionally one-shot; recovery must inspect it rather than
  # blindly generating a second label event.
  mkdir -p "$CI_MAIN_REFRESH_REARM_RECEIPT_DIR" \
    || die 1 "cannot create CI re-arm receipt directory"
  receipt_root="$CI_MAIN_REFRESH_REARM_RECEIPT_DIR/${pr}-${live_head}"
  mkdir "$receipt_root" 2>/dev/null \
    || die 45 "CI re-arm already reserved for PR #$pr head $live_head: $receipt_root"
  receipt="$receipt_root/receipt.json"
  python3 - "$topology" "$receipt" "$prior_ci_run" "$prior_e2e_run" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

topology_path, receipt_path, ci_run, e2e_run = sys.argv[1:]
data = json.loads(Path(topology_path).read_text(encoding="utf-8"))
data.update({
    "status": "pending",
    "prior_ci_run_id": int(ci_run),
    "prior_e2e_run_id": int(e2e_run),
    "reserved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
})
path = Path(receipt_path)
tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(path)
PY

  latest_head="$(gh pr view "$pr" --repo "$REPO" --json headRefOid --jq '.headRefOid' 2>/dev/null || true)"
  [ "$latest_head" = "$live_head" ] \
    || die 1 "PR #$pr head moved before CI re-arm: expected=$live_head live=$latest_head"

  if ! gh pr edit "$pr" --repo "$REPO" --remove-label "pm-state:qa-passed-awaiting-ci" >/dev/null; then
    die 1 "failed to remove the current QA label for PR #$pr CI re-arm"
  fi
  latest_head="$(gh pr view "$pr" --repo "$REPO" --json headRefOid --jq '.headRefOid' 2>/dev/null || true)"
  if [ "$latest_head" != "$live_head" ]; then
    bash "$PM_STATE" "$pr" pm-review-pending >/dev/null 2>&1 || true
    die 46 "PR #$pr head moved during CI re-arm; left fail-closed outside awaiting-CI"
  fi
  # Publish the exact-head CI admission marker BEFORE the pm-state label
  # event that starts the paid cycle: the paid CI/E2E jobs require
  # `contains(labels, 'ci-head:<event head sha>')` in the label-event
  # snapshot, which carries only the labels applied before it. This is the
  # same remove-then-publish ordering pm-state-replace.sh uses for initial
  # admission, so a stale marker from the previous cycle cannot ride the
  # re-arm event either.
  if ! remove_ci_head_markers "$pr" || ! publish_ci_head_marker "$pr" "$live_head"; then
    bash "$PM_STATE" "$pr" pm-review-pending >/dev/null 2>&1 || true
    die 46 "failed to publish ci-head:${live_head} marker for PR #$pr CI re-arm; left fail-closed at PM review"
  fi
  latest_head="$(gh pr view "$pr" --repo "$REPO" --json headRefOid --jq '.headRefOid' 2>/dev/null || true)"
  if [ "$latest_head" != "$live_head" ]; then
    bash "$PM_STATE" "$pr" pm-review-pending >/dev/null 2>&1 || true
    die 46 "PR #$pr head moved during CI re-arm marker publication; left fail-closed outside awaiting-CI"
  fi
  if ! gh pr edit "$pr" --repo "$REPO" --add-label "pm-state:qa-passed-awaiting-ci" >/dev/null; then
    bash "$PM_STATE" "$pr" pm-review-pending >/dev/null 2>&1 || true
    die 46 "failed to restore QA label for PR #$pr; left fail-closed at PM review"
  fi

  post_json="$(gh pr view "$pr" --repo "$REPO" --json headRefOid,labels 2>/dev/null)" \
    || die 46 "cannot verify PR #$pr after CI re-arm"
  post_head="$(printf '%s' "$post_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("headRefOid") or "")')"
  post_labels="$(printf '%s' "$post_json" | python3 -c 'import json,sys; print("\n".join(x.get("name","") for x in json.load(sys.stdin).get("labels", [])))')"
  if [ "$post_head" != "$live_head" ] \
    || [ "$(printf '%s\n' "$post_labels" | grep -c '^pm-state:' || true)" -ne 1 ] \
    || ! printf '%s\n' "$post_labels" | grep -qx 'pm-state:qa-passed-awaiting-ci' \
    || printf '%s\n' "$post_labels" | grep -qE '^(pm-blocked:|cto-rescue:in-progress$|slot:[1-4]$)'; then
    bash "$PM_STATE" "$pr" pm-review-pending >/dev/null 2>&1 || true
    die 46 "PR #$pr postcondition failed after CI re-arm; left fail-closed at PM review"
  fi

  python3 - "$receipt" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
data["status"] = "rearmed"
data["rearmed_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(path)
PY

  upsert_obligation --kind ci_watch --severity high --target-type pr --target-id "$pr" \
    --pr "$pr" --owner pm --horizon hourly \
    --next-review-at "$(utc_plus_minutes 15)" \
    --title "PR #$pr current-head CI/E2E re-armed after verified main refresh" \
    --action "Watch the new real CI and E2E Smoke Tests pull_request runs on exact head $live_head. Process their terminal result through Skill(ci-success-reconciliation) or the typed CI-failure investigation path; do not toggle the label again." \
    --blocker "ci_main_refresh_rearmed" \
    --evidence "head=$live_head prior_head=$prior_head prior_ci_run=$prior_ci_run prior_e2e_run=$prior_e2e_run topology=$topology receipt=$receipt"
  record_event --source pm-transition --event ci_rearmed_after_main_refresh \
    --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --head-sha "$live_head" \
    --payload "prior_head=$prior_head" --payload "current_main=$current_main" \
    --payload "prior_ci_run=$prior_ci_run" --payload "prior_e2e_run=$prior_e2e_run" \
    --payload "topology=$topology" --payload "receipt=$receipt" \
    --dedupe-key "ci_rearmed_after_main_refresh:${pr}:${live_head}"
  kanban_flag PM_TRANSITION "ci_rearmed_after_main_refresh pr=$pr head=$live_head prior_head=$prior_head receipt=$receipt"
  echo "PM_TRANSITION_OK command=ci-rearm-after-main-refresh pr=$pr issue=${issue:-unknown} head=$live_head prior_head=$prior_head prior_ci_run=$prior_ci_run prior_e2e_run=$prior_e2e_run receipt=$receipt state=qa-passed-awaiting-ci"
}

cmd_write_promotion_proof() {
  local pr="" state="" content_file="" proof head content
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      --state) state="${2:-}"; shift 2 ;;
      --content-file) content_file="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown write-promotion-proof arg $1" ;;
    esac
  done
  need_num pr "$pr"
  case "$state" in
    qa-passed-awaiting-ci|merge-ready) ;;
    *) die 2 "state must be qa-passed-awaiting-ci or merge-ready" ;;
  esac
  [ -f "$content_file" ] || die 2 "promotion proof content file not found: $content_file"
  head="$(gh pr view "$pr" --repo "$REPO" --json headRefOid --jq '.headRefOid' 2>/dev/null || true)"
  [ -n "$head" ] || die 1 "cannot read PR #$pr current head"
  content="$(cat "$content_file")" || die 1 "cannot read promotion proof content: $content_file"
  proof="/tmp/pm-state-promotion-proof-${pr}-${state}.ok"
  write_promotion_proof_atomic "$proof" "$pr" "$state" "$head" "$content" \
    || die 1 "refused to replace promotion proof for PR #$pr: content must match current head $head and the $state evidence contract"
  echo "PM_TRANSITION_OK command=write-promotion-proof pr=$pr state=$state head=${head:0:10} proof=$proof source=$content_file"
}

cmd_merge_ready() {
  local args=("$@") pr=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown merge-ready arg $1" ;;
    esac
  done
  need_num pr "$pr"
  if [ "${PM_TRANSITION_MERGE_READY_LOCKED:-0}" != "1" ]; then
    PM_TRANSITION_MERGE_READY_LOCKED=1 \
      run_with_assign_rework_lock "$pr" cmd_merge_ready "${args[@]}"
    return $?
  fi
  local issue branch head is_draft merge_state mergeable proof pr_json released_slots
  issue="$(issue_from_pr "$pr")"
  pr_json="$(gh pr view "$pr" --repo "$REPO" --json headRefName,headRefOid,isDraft,mergeStateStatus,mergeable 2>/dev/null || true)"
  branch="$(printf '%s' "$pr_json" | json_field headRefName 2>/dev/null || true)"
  head="$(printf '%s' "$pr_json" | json_field headRefOid 2>/dev/null || true)"
  is_draft="$(printf '%s' "$pr_json" | json_field isDraft 2>/dev/null || true)"
  merge_state="$(printf '%s' "$pr_json" | json_field mergeStateStatus 2>/dev/null || true)"
  mergeable="$(printf '%s' "$pr_json" | json_field mergeable 2>/dev/null || true)"
  [ -n "$head" ] || die 1 "cannot read PR #$pr head"
  [ "$is_draft" != "true" ] || die 1 "PR #$pr is draft; mark ready before merge-ready"
  [ "$mergeable" = "MERGEABLE" ] || die 1 "PR #$pr mergeable=$mergeable; merge-ready requires a conflict-free head"
  case "$merge_state" in
    CLEAN|UNSTABLE|BEHIND)
      ;;
    *)
      die 1 "PR #$pr mergeStateStatus=$merge_state; merge-ready requires CLEAN or a conflict-free UNSTABLE/BEHIND head with required current-head CI/E2E proof"
      ;;
  esac
  assert_no_unresolved_review_threads "$pr" "$head"
  [ -x "$PROMOTION_CURRENT_HEAD_CI_GUARD" ] \
    || die 70 "promotion current-head CI guard is not executable: $PROMOTION_CURRENT_HEAD_CI_GUARD"
  # The guard runs live on every merge-ready attempt. A short-TTL cache keyed
  # only on pr|head is unsafe: a newer same-head CI/E2E run can fail within
  # the window and the cached success would mask it. The guard is already
  # bounded to the newest runs per workflow, so its wall time is acceptable.
  local gate_output="" gate_rc=0
  gate_output="$(bash "$PROMOTION_CURRENT_HEAD_CI_GUARD" "$pr" 2>&1)" || gate_rc=$?
  gate_rc="${gate_rc:-0}"
  if [ "$gate_rc" != "0" ]; then
    die 1 "PR #$pr mergeStateStatus=$merge_state and required current-head CI/E2E proof is incomplete; guard=$gate_output"
  fi
  proof="$(merge_ready_proof_path "$pr")"
  merge_ready_proof_normalize_legacy_head "$pr" "$head"
  merge_ready_proof_ok "$pr" "$head" || die 1 "$(merge_ready_proof_error "$pr" "$head"); expected $proof"
	  bash "$PM_STATE" "$pr" merge-ready || die 1 "failed to move PR #$pr to merge-ready"
	  archive_slot_ready_events_for_pr "$pr" "merge-ready"
	  resolve_pr_obligation_kinds "$pr" "$issue" "merge_ready" "proof=$proof head=$head" pm_review_pending review_loop_rescope rescope_final_patch rescope_override capture_watch capture_local_preflight capture_rerun_after_local_proof ci_local_preflight ci_rerun_after_preflight ci_reconcile ci_watch blocked_rework rework_slot_idle
	  for lbl in pm-blocked:ci pm-blocked:capture pm-blocked:codex pm-blocked:rebase pm-blocked:pm-gate pm-blocked:dependency pm-blocked:product pm-blocked:infra slot:1 slot:2 slot:3 slot:4; do
    gh pr edit "$pr" --repo "$REPO" --remove-label "$lbl" >/dev/null 2>&1 || true
  done
  released_slots="$(release_target_slots "$pr" "$issue" "$branch" "merge-ready")"
  [ -n "$issue" ] && gh issue edit "$issue" --repo "$REPO" --remove-label "status:in-progress" --add-label "status:in-review" >/dev/null 2>&1 || true
  record_event --source pm-transition --event merge_ready --target-type pr --target-id "$pr" --pr "$pr" --issue "$issue" --head-sha "$head" --payload "released_slots=${released_slots:-none}" --payload "proof=$proof" --dedupe
  kanban_flag PM_TRANSITION "merge_ready pr=$pr issue=$issue head=$head proof=$proof released_slots=${released_slots:-none}"
  notify_merge_ready_dm "$pr" "$issue" "$branch" "$released_slots"
  merge_ready_thread_notify "$pr" "$issue" "$branch" "$head" "$proof" "$released_slots"
  [ -n "$released_slots" ] && run_post_release_sweep "merge-ready"
  echo "PM_TRANSITION_OK command=merge-ready pr=$pr head=${head:0:10} proof=$proof released_slots=${released_slots:-none}"
}

cmd_validate_ready_proof() {
  local pr=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown validate-ready-proof arg $1" ;;
    esac
  done
  need_num pr "$pr"
  local pr_json head proof
  pr_json="$(gh pr view "$pr" --repo "$REPO" --json headRefOid 2>/dev/null || true)"
  head="$(printf '%s' "$pr_json" | json_field headRefOid 2>/dev/null || true)"
  [ -n "$head" ] || die 1 "cannot read PR #$pr head"
  proof="$(merge_ready_proof_path "$pr")"
  assert_no_unresolved_review_threads "$pr" "$head"
  merge_ready_proof_normalize_legacy_head "$pr" "$head"
  merge_ready_proof_ok "$pr" "$head" || die 1 "$(merge_ready_proof_error "$pr" "$head"); expected $proof"
  echo "PM_TRANSITION_OK command=validate-ready-proof pr=$pr head=${head:0:10} proof=$proof"
}

cmd_capacity_snapshot() {
  [ -x "$CAPACITY_CONTROL" ] || die 1 "capacity control engine is not executable: $CAPACITY_CONTROL"
  exec python3 "$CAPACITY_CONTROL" snapshot "$@"
}

cmd_reconcile_capacity() {
  # Kernel release intents are the authoritative fallback when the shell
  # post-commit handoff was interrupted; delivery remains idempotent.
  local kernel_outbox_dry_run=0 kernel_arg
  for kernel_arg in "$@"; do
    [ "$kernel_arg" = "--dry-run" ] && kernel_outbox_dry_run=1
  done
  if [ "$kernel_outbox_dry_run" != "1" ] && [ -f "$CONTROL_PLANE_KERNEL_DATABASE" ]; then
    local kernel_python="${CONTROL_PLANE_KERNEL_PYTHON:-$(command -v python3 2>/dev/null || true)}"
    local kernel_delay="${SLOT_RELEASE_QUARANTINE_SECONDS:-30}"
    if [ -x "$kernel_python" ] && [ -f "$KERNEL_ASSIGNMENT_BOUNDARY" ]; then
      "$kernel_python" "$KERNEL_ASSIGNMENT_BOUNDARY" capacity-reconcile \
        --database "$CONTROL_PLANE_KERNEL_DATABASE" \
        --capacity-control "$CAPACITY_CONTROL" \
        --delay-seconds "$((kernel_delay + 1))" >/dev/null 2>&1 || {
          local kernel_rc=$?
          [ "$kernel_rc" = "23" ] ||
            printf 'PM_TRANSITION_WARNING capacity_reconcile_python_deferred rc=%s\n' "$kernel_rc" >&2
        }
    fi
  fi
  case "${PM_CAPACITY_SCHEDULER_V2:-0}" in
    shadow)
      [ -x "$CAPACITY_CONTROL" ] || die 1 "capacity control engine is not executable: $CAPACITY_CONTROL"
      # The engine exec never returns to this shell, so the durable index
      # refresh must run here: reconcile-capacity self-heals the fresh-assign
      # guard in every engine mode (the legacy block refreshes below). A
      # failed PR sweep preserves the stale index and pending sweep request.
      priority_rework_index_refresh_from_sweep
      exec python3 "$CAPACITY_CONTROL" reconcile --dry-run "$@"
      ;;
    1|true|enabled)
      [ -x "$CAPACITY_CONTROL" ] || die 1 "capacity control engine is not executable: $CAPACITY_CONTROL"
      local v2_dry_run=0 v2_arg
      for v2_arg in "$@"; do
        [ "$v2_arg" = "--dry-run" ] && v2_dry_run=1
      done
      if [ "$v2_dry_run" != "1" ]; then
        assignment_outbox_drain || true
      fi
      # The engine exec never returns to this shell, so the durable index
      # refresh must run here: reconcile-capacity self-heals the fresh-assign
      # guard in every engine mode (the legacy block refreshes below). A
      # failed PR sweep preserves the stale index and pending sweep request.
      priority_rework_index_refresh_from_sweep
      exec python3 "$CAPACITY_CONTROL" reconcile "$@"
      ;;
  esac
  local dry_run=0 slots="1 2 3 4"
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --dry-run) dry_run=1; shift ;;
      --slots)
        slots="$(printf '%s' "${2:-}" | tr ',' ' ')"
        shift 2
        ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown reconcile-capacity arg $1" ;;
    esac
  done

  local mutation_dry_run="$dry_run"
  if [ "${PM_CAPACITY_ASSIGN_FILL:-0}" != "1" ]; then
    mutation_dry_run=1
  elif [ "${PM_CAPACITY_FLEET_ENABLE:-0}" != "1" ]; then
    # The legacy sweeps cannot constrain every mutation to one canary. Canary
    # assignment is owned by the typed V2 engine; compatibility stays shadow.
    mutation_dry_run=1
  fi

  local slot
  for slot in $slots; do
    need_num slot "$slot"
    [[ "$slot" =~ ^[1-4]$ ]] || die 2 "slot must be 1..4: $slot"
  done

  local released=0 consumed=0 already_free=0 kept_active=0 drift=0 errors=0
  local out rc epoch canary
  local -a drain_pids=() drain_tmps=()
  local drain_tmp drain_pid drain_idx
  for slot in $slots; do
    if [ "$dry_run" = "1" ]; then
      # Read-only drain probes are independent per slot; run them concurrently
      # (bounded by 4 slots) and collect in slot order. Mutations stay inline
      # below so epoch CAS and obligation writes never race.
      drain_tmp="$(mktemp /tmp/pm-reconcile-drain-XXXXXX 2>/dev/null || printf '/tmp/pm-reconcile-drain-%s' "$$-$slot")"
      drain_tmps+=("$drain_tmp")
      ( PM_TRANSITION_NO_SWEEP=1 cmd_drain_slot --slot "$slot" --dry-run >"$drain_tmp" 2>&1 ) &
      drain_pids+=($!)
    elif [ "${PM_CAPACITY_COMPAT_AUTO_DRAIN:-0}" != "1" ]; then
      drain_tmp="$(mktemp /tmp/pm-reconcile-drain-XXXXXX 2>/dev/null || printf '/tmp/pm-reconcile-drain-%s' "$$-$slot")"
      drain_tmps+=("$drain_tmp")
      ( PM_TRANSITION_NO_SWEEP=1 cmd_drain_slot --slot "$slot" --dry-run >"$drain_tmp" 2>&1
        printf '\nCOMPAT_AUTO_DRAIN_DISABLED slot=%s action=obligation-only\n' "$slot" >>"$drain_tmp" ) &
      drain_pids+=($!)
    else
      canary="${PM_CAPACITY_SLOT_CANARY:-}"
      if [ "${PM_CAPACITY_FLEET_ENABLE:-0}" != "1" ] && [ "$canary" != "$slot" ]; then
        out="$(PM_TRANSITION_NO_SWEEP=1 cmd_drain_slot --slot "$slot" --dry-run 2>&1)"$'\n'"COMPAT_AUTO_DRAIN_CANARY_BLOCKED slot=$slot selected=${canary:-none} action=obligation-only"
        rc=$?
      else
        epoch="$(mop_slot_epoch "$slot")" || { errors=$((errors+1)); echo "COMPAT_AUTO_DRAIN_EPOCH_UNAVAILABLE slot=$slot"; continue; }
        out="$(PM_MUTATION_CLASS=compat_auto_drain PM_TRANSITION_NO_SWEEP=1 cmd_drain_slot --slot "$slot" --expected-epoch "$epoch" 2>&1)"
        rc=$?
      fi
      case "$out" in
        *"action=released"*) released=$((released+1)) ;;
        *"action=consumed-ready"*) consumed=$((consumed+1)) ;;
        *"action=already-free"*) already_free=$((already_free+1)) ;;
        *"action=kept-active"*) kept_active=$((kept_active+1)) ;;
        *"SLOT_DRAIN_DRY_RUN"*"action=release"*) released=$((released+1)) ;;
        *"SLOT_DRAIN_DRY_RUN"*"action=consume-ready"*) consumed=$((consumed+1)) ;;
        *"SLOT_DRAIN_REQUIRED"*) drift=$((drift+1)) ;;
        *) [ "$rc" = "0" ] || errors=$((errors+1)) ;;
      esac
      printf '%s\n' "$out"
    fi
  done
  drain_idx=0
  for drain_pid in "${drain_pids[@]:-}"; do
    [ -n "$drain_pid" ] || { drain_idx=$((drain_idx+1)); continue; }
    wait "$drain_pid" 2>/dev/null
    rc=$?
    drain_tmp="${drain_tmps[$drain_idx]}"
    out="$(cat "$drain_tmp" 2>/dev/null || true)"
    rm -f "$drain_tmp"
    case "$out" in
      *"action=released"*) released=$((released+1)) ;;
      *"action=consumed-ready"*) consumed=$((consumed+1)) ;;
      *"action=already-free"*) already_free=$((already_free+1)) ;;
      *"action=kept-active"*) kept_active=$((kept_active+1)) ;;
      *"SLOT_DRAIN_DRY_RUN"*"action=release"*) released=$((released+1)) ;;
      *"SLOT_DRAIN_DRY_RUN"*"action=consume-ready"*) consumed=$((consumed+1)) ;;
      *"SLOT_DRAIN_REQUIRED"*) drift=$((drift+1)) ;;
      *) [ "$rc" = "0" ] || errors=$((errors+1)) ;;
    esac
    printf '%s\n' "$out"
    drain_idx=$((drain_idx+1))
  done

  local pr_sweep_log="/tmp/pm-transition-reconcile-capacity-pr-sweep.log"
  local pr_rework_log="/tmp/pm-transition-reconcile-capacity-rework.log"
  local pr_actionable=0 pr_dispatch_blockers=0 pr_mutated=0 pr_rework_assigned=0 pr_rework_deferred=0 pr_rework_errors=0 rework_priority_block=0
  if [ -x "$PR_SWEEP" ]; then
    if [ "$mutation_dry_run" = "1" ] && reconcile_pr_sweep_log_fresh "$pr_sweep_log"; then
      # Read-only PR sweep cache hit (bounded TTL): the last reconcile-capacity
      # dry-run sweep is fresh and no transition invalidated the index since,
      # so reuse its log instead of re-running the ~20s GitHub scan.
      echo "PR_SWEEP_CACHE_HIT trigger=reconcile-capacity log=$pr_sweep_log ttl=${PM_RECONCILE_PR_SWEEP_CACHE_TTL_SECONDS:-120}"
    elif [ "$mutation_dry_run" = "1" ]; then
      PR_SWEEP_WRITE_SENTINEL=1 bash "$PR_SWEEP" --trigger=reconcile-capacity --dry-run >"$pr_sweep_log" 2>&1 || true
    else
      bash "$PR_SWEEP" --trigger=reconcile-capacity >"$pr_sweep_log" 2>&1 || true
    fi
    # The durable priority-rework index is written after every sweep so the
    # assign/assign-rework hot paths can query it locally without invoking the
    # global sweep synchronously.
    priority_rework_index_write "$pr_sweep_log" "pr_actionable=${pr_actionable:-0} pr_dispatch_blockers=${pr_dispatch_blockers:-0}"
    priority_rework_sweep_request_consume
    pr_actionable="$(count_pr_sweep_actionable "$pr_sweep_log")"
    pr_dispatch_blockers="$(count_pr_sweep_dispatch_blockers "$pr_sweep_log")"
    pr_mutated="$(sed -n 's/.*PR_SWEEP_ACTIONABLE count=[0-9][0-9]* mutated=\([0-9][0-9]*\).*/\1/p' "$pr_sweep_log" 2>/dev/null | tail -1)"
    pr_actionable="${pr_actionable:-0}"
    pr_dispatch_blockers="${pr_dispatch_blockers:-0}"
    pr_mutated="${pr_mutated:-0}"
  fi

  : >"$pr_rework_log"
  if [ "${pr_actionable:-0}" -gt 0 ] && grep -q ' PR_REWORK_DISPATCH_REQUIRED ' "$pr_sweep_log" 2>/dev/null; then
    auto_assign_rework_from_pr_sweep "$pr_sweep_log" "$mutation_dry_run" "$slots" >"$pr_rework_log" 2>&1 || true
    cat "$pr_rework_log"
    pr_rework_assigned="$(sed -n 's/.*PR_REWORK_ASSIGNMENT_SUMMARY assigned=\([0-9][0-9]*\).*/\1/p' "$pr_rework_log" 2>/dev/null | tail -1)"
    pr_rework_deferred="$(sed -n 's/.*PR_REWORK_ASSIGNMENT_SUMMARY assigned=[0-9][0-9]* deferred=\([0-9][0-9]*\).*/\1/p' "$pr_rework_log" 2>/dev/null | tail -1)"
    pr_rework_errors="$(sed -n 's/.*PR_REWORK_ASSIGNMENT_SUMMARY assigned=[0-9][0-9]* deferred=[0-9][0-9]* errors=\([0-9][0-9]*\).*/\1/p' "$pr_rework_log" 2>/dev/null | tail -1)"
    pr_rework_assigned="${pr_rework_assigned:-0}"
    pr_rework_deferred="${pr_rework_deferred:-0}"
    pr_rework_errors="${pr_rework_errors:-0}"

    if [ "$mutation_dry_run" != "1" ] && [ "${pr_rework_assigned:-0}" -gt 0 ] && [ -x "$PR_SWEEP" ]; then
      bash "$PR_SWEEP" --trigger=reconcile-capacity >"$pr_sweep_log" 2>&1 || true
      priority_rework_index_write "$pr_sweep_log" "pr_actionable=${pr_actionable:-0} pr_dispatch_blockers=${pr_dispatch_blockers:-0}"
      pr_actionable="$(count_pr_sweep_actionable "$pr_sweep_log")"
      pr_dispatch_blockers="$(count_pr_sweep_dispatch_blockers "$pr_sweep_log")"
      pr_mutated="$(sed -n 's/.*PR_SWEEP_ACTIONABLE count=[0-9][0-9]* mutated=\([0-9][0-9]*\).*/\1/p' "$pr_sweep_log" 2>/dev/null | tail -1)"
      pr_actionable="${pr_actionable:-0}"
      pr_dispatch_blockers="${pr_dispatch_blockers:-0}"
      pr_mutated="${pr_mutated:-0}"
    fi
  fi

  if grep -q ' PR_REWORK_PACKET_REQUIRED ' "$pr_sweep_log" 2>/dev/null; then
    rework_priority_block=1
  elif grep -q ' PR_REWORK_DISPATCH_REQUIRED ' "$pr_sweep_log" 2>/dev/null \
    && { [ "${pr_rework_deferred:-0}" -gt 0 ] || [ "${pr_rework_errors:-0}" -gt 0 ]; }; then
    rework_priority_block=1
  fi

  local sweep_log="/tmp/pm-transition-reconcile-capacity-sweep.log"
  : >"$sweep_log"
  if [ "${pr_dispatch_blockers:-0}" -gt 0 ]; then
    echo "PR_SWEEP_CURRENT_WORK_REMAINS actionable=$pr_actionable dispatch_blockers=$pr_dispatch_blockers mutated=$pr_mutated rework_assigned=$pr_rework_assigned rework_deferred=$pr_rework_deferred rework_errors=$pr_rework_errors action=continue_slot_dispatch log=$pr_sweep_log rework_log=$pr_rework_log" >>"$sweep_log"
    if [ "$dry_run" != "1" ]; then
      record_event \
        --source pm-transition \
        --event reconcile_capacity_pr_sweep_continued \
        --target-type control \
        --target-id capacity \
        --payload "dry_run=$dry_run" \
        --payload "pr_actionable=$pr_actionable" \
        --payload "pr_dispatch_blockers=$pr_dispatch_blockers" \
        --payload "pr_mutated=$pr_mutated" \
        --payload "pr_rework_assigned=$pr_rework_assigned" \
        --payload "pr_rework_deferred=$pr_rework_deferred" \
        --payload "pr_rework_errors=$pr_rework_errors" \
        --payload "pr_sweep_log=$pr_sweep_log" \
        --payload "pr_rework_log=$pr_rework_log"
      kanban_flag PM_TRANSITION "reconcile_capacity current PR work remains but free capacity continues to todo dispatch: active_pr_rows=$pr_dispatch_blockers pr_sweep_log=$pr_sweep_log"
    fi
  fi

  if [ "$rework_priority_block" = "1" ]; then
    echo "PR_REWORK_PRIORITY_BLOCK fresh_todo_dispatch=skipped reason=existing_pr_rework_not_durably_packeted_or_assigned pr_sweep_log=$pr_sweep_log pr_rework_log=$pr_rework_log" >>"$sweep_log"
    if [ "$dry_run" != "1" ]; then
      upsert_obligation --kind rework_priority_block --severity high --target-type control --target-id capacity --owner pm --horizon hourly --title "Existing PR rework must be packeted and assigned before fresh todo dispatch" --action "Resolve PR_REWORK_PACKET_REQUIRED or failed PR_REWORK_DISPATCH_REQUIRED rows, then rerun pm-transition reconcile-capacity." --blocker "existing_pr_rework_precedes_fresh_todo" --evidence "pr_sweep_log=$pr_sweep_log" --evidence "pr_rework_log=$pr_rework_log" >/dev/null 2>&1 || true
      record_event --source pm-transition --event rework_priority_block --target-type control --target-id capacity --payload "pr_sweep_log=$pr_sweep_log" --payload "pr_rework_log=$pr_rework_log" --payload "fresh_todo_dispatch=skipped"
    fi
  elif [ "$mutation_dry_run" = "1" ]; then
    if [ -x "$SWEEP" ]; then
      bash "$SWEEP" --trigger=reconcile-capacity --dry-run >>"$sweep_log" 2>&1 || true
    fi
  else
    if [ -x "$SWEEP" ]; then
      bash "$SWEEP" --trigger=reconcile-capacity >>"$sweep_log" 2>&1 || true
    fi
  fi

  local assigned queued drain_required claimable
  assigned="$(grep -c 'ASSIGNED:' "$sweep_log" 2>/dev/null || true)"
  queued="$(grep -c 'QUEUED:' "$sweep_log" 2>/dev/null || true)"
  drain_required="$(grep -c 'SLOT_DRAIN_REQUIRED' "$sweep_log" 2>/dev/null || true)"
  assigned="${assigned:-0}"
  queued="${queued:-0}"
  drain_required="${drain_required:-0}"
  claimable="$(sed -n 's/.*CLAIMABLE_ISSUES count=\([0-9][0-9]*\).*/\1/p' "$sweep_log" 2>/dev/null | tail -1)"

  if [ "$rework_priority_block" = "1" ]; then
    BACKLOG_PROMOTER_STATUS="rework_priority_block"
    BACKLOG_PROMOTER_SELECTED=0
    BACKLOG_PROMOTER_LOG="/tmp/pm-transition-reconcile-capacity-backlog-promoter.log"
    BACKLOG_PROMOTER_SWEEP_LOG="/tmp/pm-transition-reconcile-capacity-backlog-promoter-sweep.log"
    printf 'BACKLOG_PROMOTER_SKIPPED reason=existing_pr_rework_precedes_fresh_todo\n' >"$BACKLOG_PROMOTER_LOG"
  else
    maybe_run_backlog_promoter_after_sweep "reconcile-capacity" "$sweep_log" "$mutation_dry_run" "$pr_sweep_log"
  fi
  if [ "${BACKLOG_PROMOTER_SELECTED:-0}" -gt 0 ] && [ -r "$BACKLOG_PROMOTER_SWEEP_LOG" ]; then
    sweep_log="$BACKLOG_PROMOTER_SWEEP_LOG"
    assigned="$(grep -c 'ASSIGNED:' "$sweep_log" 2>/dev/null || true)"
    queued="$(grep -c 'QUEUED:' "$sweep_log" 2>/dev/null || true)"
    drain_required="$(grep -c 'SLOT_DRAIN_REQUIRED' "$sweep_log" 2>/dev/null || true)"
    assigned="${assigned:-0}"
    queued="${queued:-0}"
    drain_required="${drain_required:-0}"
    claimable="$(sed -n 's/.*CLAIMABLE_ISSUES count=\([0-9][0-9]*\).*/\1/p' "$sweep_log" 2>/dev/null | tail -1)"
  fi

  if [ "$dry_run" != "1" ]; then
    record_event \
      --source pm-transition \
      --event reconcile_capacity \
      --target-type control \
      --target-id capacity \
      --payload "dry_run=$dry_run" \
      --payload "released=$released" \
      --payload "consumed_ready=$consumed" \
      --payload "already_free=$already_free" \
      --payload "kept_active=$kept_active" \
      --payload "drift=$drift" \
      --payload "errors=$errors" \
      --payload "assigned=$assigned" \
      --payload "queued=$queued" \
      --payload "drain_required=$drain_required" \
      --payload "claimable=${claimable:-unknown}" \
      --payload "pr_actionable=$pr_actionable" \
      --payload "pr_dispatch_blockers=$pr_dispatch_blockers" \
      --payload "pr_mutated=$pr_mutated" \
      --payload "pr_rework_assigned=$pr_rework_assigned" \
      --payload "pr_rework_deferred=$pr_rework_deferred" \
      --payload "pr_rework_errors=$pr_rework_errors" \
      --payload "backlog_promoter_status=$BACKLOG_PROMOTER_STATUS" \
      --payload "backlog_promoted=$BACKLOG_PROMOTER_SELECTED" \
      --payload "backlog_promoter_log=${BACKLOG_PROMOTER_LOG:-none}" \
      --payload "backlog_promoter_sweep_log=${BACKLOG_PROMOTER_SWEEP_LOG:-none}" \
      --payload "pr_sweep_log=$pr_sweep_log" \
      --payload "pr_rework_log=$pr_rework_log" \
      --payload "sweep_log=$sweep_log"
    kanban_flag PM_TRANSITION "reconcile_capacity dry_run=$dry_run released=$released consumed_ready=$consumed kept_active=$kept_active drift=$drift assigned=$assigned queued=$queued"
  fi

  # reconcile-capacity is the canonical durable outbox drainer: post-commit
  # entries enqueued by transitions (obligations, events, kanban, alerts,
  # post-release sweeps) are executed here fail-soft.
  if [ "$dry_run" != "1" ]; then
    postcommit_drain 40 || true
    assignment_outbox_drain || true
  fi

  echo "PM_TRANSITION_OK command=reconcile-capacity dry_run=$dry_run released=$released consumed_ready=$consumed already_free=$already_free kept_active=$kept_active drift=$drift errors=$errors pr_actionable=$pr_actionable pr_dispatch_blockers=$pr_dispatch_blockers pr_mutated=$pr_mutated pr_rework_assigned=$pr_rework_assigned pr_rework_deferred=$pr_rework_deferred pr_rework_errors=$pr_rework_errors rework_priority_block=$rework_priority_block assigned=$assigned queued=$queued drain_required=$drain_required claimable=${claimable:-unknown} backlog_promoter_status=$BACKLOG_PROMOTER_STATUS backlog_promoted=$BACKLOG_PROMOTER_SELECTED backlog_promoter_log=${BACKLOG_PROMOTER_LOG:-none} backlog_promoter_sweep_log=${BACKLOG_PROMOTER_SWEEP_LOG:-none} pr_sweep_log=$pr_sweep_log pr_rework_log=$pr_rework_log sweep_log=$sweep_log"
}

cmd_campaign_lock() {
  local prs="" reason="" ttl_min="480"
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --prs) prs="${2:-}"; shift 2 ;;
      --reason) reason="${2:-}"; shift 2 ;;
      --ttl-min) ttl_min="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown campaign-lock arg $1" ;;
    esac
  done
  [ -n "$prs" ] || die 2 "--prs is required"
  [ -n "$reason" ] || die 2 "--reason is required"
  need_num ttl_min "$ttl_min"

  local result
  result="$(CAMPAIGN_LOCK="$CAMPAIGN_LOCK" PRS="$prs" REASON="$reason" TTL_MIN="$ttl_min" python3 <<'PY'
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

raw_prs = os.environ["PRS"]
prs = []
for token in re.split(r"[,\s]+", raw_prs.strip()):
    if not token:
        continue
    if not token.isdigit():
        print(f"invalid_pr={token}", file=sys.stderr)
        sys.exit(2)
    prs.append(int(token))
prs = list(dict.fromkeys(prs))
if not prs:
    print("no_prs", file=sys.stderr)
    sys.exit(2)

ttl = int(os.environ.get("TTL_MIN") or "480")
now = datetime.now(timezone.utc).replace(microsecond=0)
expires = now + timedelta(minutes=ttl)
path = Path(os.environ["CAMPAIGN_LOCK"])
data = {
    "status": "active",
    "created_at": now.isoformat().replace("+00:00", "Z"),
    "expires_at": expires.isoformat().replace("+00:00", "Z"),
    "ttl_min": ttl,
    "prs": prs,
    "reason": os.environ.get("REASON") or "current-pr-campaign",
}
tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(path)
print(f"prs={','.join(str(p) for p in prs)} expires_at={data['expires_at']} lock={path}")
PY
)" || die 2 "failed to write campaign lock"

  record_event --source pm-transition --event campaign_lock --target-type control --target-id current-pr-campaign --payload "prs=$prs" --payload "reason=$reason" --payload "ttl_min=$ttl_min" --payload "lock=$CAMPAIGN_LOCK"
  kanban_flag PM_TRANSITION "campaign_lock prs=$prs reason=$reason ttl_min=$ttl_min"
  echo "PM_TRANSITION_OK command=campaign-lock $result"
}

cmd_campaign_unlock() {
  local reason=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --reason) reason="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown campaign-unlock arg $1" ;;
    esac
  done
  [ -n "$reason" ] || die 2 "--reason is required"

  local result
  result="$(CAMPAIGN_LOCK="$CAMPAIGN_LOCK" REASON="$reason" python3 <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

path = Path(os.environ["CAMPAIGN_LOCK"])
if path.exists():
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
else:
    data = {}
now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
data["status"] = "resolved"
data["resolved_at"] = now
data["resolution"] = os.environ.get("REASON") or "resolved"
tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(path)
print(f"status=resolved resolved_at={now} lock={path}")
PY
)" || die 2 "failed to resolve campaign lock"

  record_event --source pm-transition --event campaign_unlock --target-type control --target-id current-pr-campaign --payload "reason=$reason" --payload "lock=$CAMPAIGN_LOCK"
  kanban_flag PM_TRANSITION "campaign_unlock reason=$reason"
  echo "PM_TRANSITION_OK command=campaign-unlock $result"
}

cmd_campaign_status() {
  CAMPAIGN_LOCK="$CAMPAIGN_LOCK" python3 <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

path = Path(os.environ["CAMPAIGN_LOCK"])
if not path.exists():
    print(f"PM_TRANSITION_OK command=campaign-status status=missing lock={path}")
    raise SystemExit(0)
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"PM_TRANSITION_OK command=campaign-status status=malformed lock={path} error={str(exc)[:80]}")
    raise SystemExit(0)
status = data.get("status") or "unknown"
expires = data.get("expires_at") or ""
if status == "active" and expires:
    try:
        if datetime.fromisoformat(expires.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
            status = "expired"
    except Exception:
        pass
prs = ",".join(str(x) for x in data.get("prs") or [])
reason = " ".join(str(data.get("reason") or "").split())
print(f"PM_TRANSITION_OK command=campaign-status status={status} prs={prs or 'none'} expires_at={expires or 'none'} reason={reason or 'none'} lock={path}")
PY
}

cmd_cleanup_start() {
  local pr=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown cleanup-start arg $1" ;;
    esac
  done
  need_num pr "$pr"

  local pr_json state head branch issue next_review
  pr_json="$(gh pr view "$pr" --repo "$REPO" --json state,headRefOid,headRefName 2>/dev/null || true)"
  [ -n "$pr_json" ] || die 1 "cannot read PR #$pr"
  state="$(printf '%s' "$pr_json" | json_field state 2>/dev/null || true)"
  [ "$state" = "MERGED" ] || die 1 "PR #$pr is not merged (state=${state:-unknown})"
  head="$(printf '%s' "$pr_json" | json_field headRefOid 2>/dev/null || true)"
  branch="$(printf '%s' "$pr_json" | json_field headRefName 2>/dev/null || true)"
  issue="$(issue_from_pr "$pr")"

  bash "$PM_STATE" "$pr" merged-cleanup-pending || die 1 "failed to move PR #$pr to merged-cleanup-pending"
  gh pr edit "$pr" --repo "$REPO" --add-label "pm-cleanup:needed" >/dev/null || die 1 "failed to add pm-cleanup:needed to PR #$pr"
  remove_pm_blockers "$pr" "" >/dev/null 2>&1 || true
  for slot in 1 2 3 4; do
    gh pr edit "$pr" --repo "$REPO" --remove-label "slot:${slot}" >/dev/null 2>&1 || true
  done

  next_review="$(utc_plus_minutes 15)"
  local target_args=()
  [ -n "$issue" ] && target_args+=(--issue "$issue")
  upsert_obligation --kind cleanup_pr --severity high --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --owner pm --horizon hourly --next-review-at "$next_review" --title "PR #$pr merged cleanup is in progress" --action "Finish Skill(cleanup-pr), generate the delivery retro, then run pm-transition cleanup --pr $pr --retro-path /tmp/pr-retro-$pr.md." --blocker "merged_cleanup_pending" --evidence "head=${head:-unknown}" --evidence "branch=${branch:-unknown}" --evidence "started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  record_event --source pm-transition --event cleanup_started --target-type pr --target-id "$pr" --pr "$pr" ${target_args[@]+"${target_args[@]}"} --head-sha "$head" --payload "branch=${branch:-unknown}" --payload "next_review_at=$next_review" --dedupe-key "cleanup_started:${pr}"
  kanban_flag PM_TRANSITION "cleanup_started pr=$pr issue=${issue:-unknown} head=${head:-unknown}"
  echo "PM_TRANSITION_OK command=cleanup-start pr=$pr issue=${issue:-unknown} head=${head:0:8} next_review_at=$next_review"
}

cmd_cleanup() {
  local pr="" retro_path="" retro_trivial=0
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pr) pr="${2:-}"; shift 2 ;;
      --retro-path) retro_path="${2:-}"; shift 2 ;;
      --retro-trivial) retro_trivial=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die 2 "unknown cleanup arg $1" ;;
    esac
  done
  need_num pr "$pr"
  if [ "$retro_trivial" -ne 1 ]; then
    retro_path="${retro_path:-/tmp/pr-retro-$pr.md}"
    [ -f "$retro_path" ] || die 1 "missing_delivery_retro pr=$pr path=$retro_path; run pr-delivery-retro.py or pass --retro-trivial for docs/chore-only cleanup"
    grep -q '^retro_status: complete$' "$retro_path" || die 1 "incomplete_delivery_retro pr=$pr path=$retro_path"
  fi
	  bash "$PM_STATE" "$pr" closed-clean || die 1 "failed to move PR #$pr to closed-clean"
	  resolve_pr_obligation_kinds "$pr" "" "cleanup_closed_clean" "retro=${retro_path:-trivial}" cleanup_pr pm_review_pending review_loop_rescope rescope_final_patch rescope_override rescope_split_execution rescope_product_escalation capture_watch capture_local_preflight capture_rerun_after_local_proof ci_local_preflight ci_rerun_after_preflight ci_reconcile ci_watch blocked_rework rework_slot_idle dependency_watch
	  for lbl in pm-blocked:ci pm-blocked:capture pm-blocked:codex pm-blocked:rebase pm-blocked:pm-gate pm-blocked:dependency pm-blocked:product pm-blocked:infra pm-cleanup:needed slot:1 slot:2 slot:3 slot:4; do
    gh pr edit "$pr" --repo "$REPO" --remove-label "$lbl" >/dev/null 2>&1 || true
  done
  archive_slot_ready_events_for_pr "$pr" "cleanup:closed-clean"
  record_event --source pm-transition --event cleanup --target-type pr --target-id "$pr" --pr "$pr" --payload "retro_path=${retro_path:-trivial}" --payload "retro_trivial=$retro_trivial" --dedupe
  kanban_flag PM_TRANSITION "cleanup pr=$pr retro=${retro_path:-trivial}"
  echo "PM_TRANSITION_OK command=cleanup pr=$pr retro_path=${retro_path:-trivial} retro_trivial=$retro_trivial"
}

if [ "${PM_TRANSITION_SOURCE_ONLY:-0}" = "1" ]; then
  return 0 2>/dev/null || exit 0
fi

legacy_family2_retired() {
  local command="${1:-family2}"
  printf 'PM_TRANSITION_BLOCKED command=%s reason=family2_python_boundary_required\n' "$command" >&2
  return 423
}

# These assignment commands have no legacy command arm or reachable caller in
# the shell bus.  Refuse them explicitly so the installed Python operator is
# the sole public entrypoint without intercepting wrapper-owned transitions.
legacy_assignment_command_retired() {
  local command="${1:-assignment}"
  printf 'PM_TRANSITION_BLOCKED command=%s reason=pm_operator_required replacement=/Users/rajiv/.claude/scripts/pm-operator.py\n' "$command" >&2
  return 423
}

COMMAND="${1:-}"
if [ -z "$COMMAND" ] || [ "$COMMAND" = "-h" ] || [ "$COMMAND" = "--help" ]; then
  usage
  exit 0
fi
shift

case "$COMMAND" in
  claim-slot|rebind-slot|release-slot) legacy_assignment_command_retired "$COMMAND" ;;
  assign) cmd_assign "$@" ;;
  reserve-handoff) cmd_reserve_handoff "$@" ;;
  slot-ready) cmd_slot_ready "$@" ;;
  block-pr) cmd_block_pr "$@" ;;
  retract-operator-block) cmd_retract_operator_block "$@" ;;
  ci-watch) cmd_ci_watch "$@" ;;
  ci-stale-run-classified) cmd_ci_stale_run_classified "$@" ;;
  record-rework-packet) cmd_record_rework_packet "$@" ;;
  deliver-rework-packet) cmd_deliver_rework_packet "$@" ;;
  reconcile-rework-obligation) cmd_reconcile_rework_obligation "$@" ;;
  revoke-rework) cmd_revoke_rework "$@" ;;
  dependency-unblocked) cmd_dependency_unblocked "$@" ;;
  resolve-pm-gate) cmd_resolve_pm_gate "$@" ;;
  assign-rework) cmd_assign_rework "$@" ;;
  assign-repro) cmd_assign_repro "$@" ;;
  assign-review) cmd_assign_review "$@" ;;
  review-cap-dispatch) cmd_review_cap_dispatch "$@" ;;
  cto-rescue-pr|rescope-pr) cmd_rescope_pr "$@" ;;
  offslot-rescue-start) cmd_offslot_rescue_start "$@" ;;
  cto-rescue-issue|rescope-issue) cmd_rescope_issue "$@" ;;
  rescope-decide) cmd_rescope_decide "$@" ;;
  rescope-final-patch-applied) cmd_rescope_final_patch_applied "$@" ;;
  rescope-issue-decide) cmd_rescope_issue_decide "$@" ;;
  rescope-split-complete) cmd_rescope_split_complete "$@" ;;
  ci-local-preflight-pass) cmd_ci_local_preflight_pass "$@" ;;
  capture-local-required) cmd_capture_local_required "$@" ;;
  capture-local-pass) cmd_capture_local_pass "$@" ;;
  capture-remote-dispatch) cmd_capture_remote_dispatch "$@" ;;
  capture-control-plane-repaired) cmd_capture_control_plane_repaired "$@" ;;
  capture-remote-pass) cmd_capture_remote_pass "$@" ;;
  capture-remote-exhaust) cmd_capture_remote_exhaust "$@" ;;
  capture-remote-fail) cmd_capture_remote_fail "$@" ;;
  fabrication-reset) cmd_fabrication_reset "$@" ;;
  pm-review) family2_pm_review "$@" ;;
  pm-review-done) cmd_pm_review_done "$@" ;;
  ci-rearm-after-main-refresh) cmd_ci_rearm_after_main_refresh "$@" ;;
  write-promotion-proof) cmd_write_promotion_proof "$@" ;;
  adopt-issue-tuple) cmd_adopt_issue_tuple "$@" ;;
  adopt-pr-tuple) cmd_adopt_pr_tuple "$@" ;;
  accept-ready) cmd_accept_ready "$@" ;;
  validate-ready-proof) cmd_validate_ready_proof "$@" ;;
  merge-ready) cmd_merge_ready "$@" ;;
  park-issue) cmd_park_issue "$@" ;;
  unpark-issue) cmd_unpark_issue "$@" ;;
  reconcile-stale-slot-owner) cmd_reconcile_stale_slot_owner "$@" ;;
  reconcile-closed-slot-owner) cmd_reconcile_closed_slot_owner "$@" ;;
  reconcile-stale-pr-owner) cmd_reconcile_stale_pr_owner "$@" ;;
  reconcile-stale-github-owner) cmd_reconcile_stale_github_owner "$@" ;;
  recover-unpicked-claim) cmd_recover_unpicked_claim "$@" ;;
  drain-slot) cmd_drain_slot "$@" ;;
  capacity-snapshot) cmd_capacity_snapshot "$@" ;;
  reconcile-capacity) cmd_reconcile_capacity "$@" ;;
  campaign-lock) cmd_campaign_lock "$@" ;;
  campaign-unlock) cmd_campaign_unlock "$@" ;;
  cleanup-start) cmd_cleanup_start "$@" ;;
  cleanup) cmd_cleanup "$@" ;;
  *)
    die 2 "unknown command '$COMMAND'"
    ;;
esac
exit $?
