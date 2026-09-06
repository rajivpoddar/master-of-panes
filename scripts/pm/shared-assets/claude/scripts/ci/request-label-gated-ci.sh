#!/usr/bin/env bash
# Canonical exact-head CI/E2E admission caller.
#
# The installed native adapter owns live PR/issue/head binding, classifier and
# QA/readiness proof, duplicate/concurrency fencing, and the one guarded
# trigger-label effect. This wrapper resolves the authoritative tuple and
# forwards it; it never writes GitHub state or manufactures a review marker.

set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

REPO="$(printenv GH_REPO 2>/dev/null || true)"
[ -n "$REPO" ] || REPO="heydonna-app/heydonna-app"
ADMISSION_ADAPTER="$(printenv CI_ADMISSION_ADAPTER 2>/dev/null || true)"
[ -n "$ADMISSION_ADAPTER" ] || ADMISSION_ADAPTER="/Users/rajiv/.claude/scripts/ci/heydonna-cto-label-gated-ci.py"
CHECKOUT="$(printenv RLGC_CHECKOUT 2>/dev/null || true)"
[ -n "$CHECKOUT" ] || CHECKOUT="/Users/rajiv/Downloads/projects/heydonna-app"
GH_BIN="$(printenv GH_BIN 2>/dev/null || true)"
[ -n "$GH_BIN" ] || GH_BIN="gh"

gh_cli() { "$GH_BIN" "$@"; }

die() {
  echo "REQUEST_LABEL_GATED_CI_FAILED reason=$*" >&2
  exit 1
}

usage() {
  cat >&2 <<'EOF'
Usage:
  request-label-gated-ci.sh --pr <PR>
  request-label-gated-ci.sh --slot-ready-event <EVENT_JSON>
  request-label-gated-ci.sh --fresh-run-recovery --pr <PR> --cancelled-run <RUN_ID>
EOF
}

require_file() {
  [ -f "$1" ] || die "required_file_missing path=$1"
}

validate_tuple() {
  local number="$1" issue="$2" head="$3" base="$4"
  [[ "$number" =~ ^[0-9]+$ ]] || die "pr_identity_malformed"
  [[ "$issue" =~ ^[0-9]+$ ]] || die "linked_issue_identity_malformed"
  [[ "$head" =~ ^[0-9a-f]{40}$ ]] || die "head_identity_malformed"
  [[ "$base" =~ ^[0-9a-f]{40}$ ]] || die "base_identity_malformed"
}

live_tuple() {
  local pr="$1" payload
  payload="$(gh_cli pr view "$pr" --repo "$REPO" --json number,headRefOid,baseRefOid,closingIssuesReferences 2>/dev/null)" \
    || die "cannot_read_pr_tuple pr=$pr"
  python3 - "$pr" "$payload" <<'PY'
import json
import re
import sys

expected = int(sys.argv[1])
try:
    value = json.loads(sys.argv[2])
except json.JSONDecodeError:
    raise SystemExit("pr_tuple_malformed")
if not isinstance(value, dict) or value.get("number") != expected:
    raise SystemExit("pr_identity_malformed")
head = value.get("headRefOid")
base = value.get("baseRefOid")
refs = value.get("closingIssuesReferences")
if not re.fullmatch(r"[0-9a-f]{40}", str(head or "")):
    raise SystemExit("head_identity_malformed")
if not re.fullmatch(r"[0-9a-f]{40}", str(base or "")):
    raise SystemExit("base_identity_malformed")
if not isinstance(refs, list) or len(refs) != 1:
    raise SystemExit("linked_issue_relationship_ambiguous")
issue = refs[0].get("number") if isinstance(refs[0], dict) else None
if not isinstance(issue, int) or issue <= 0:
    raise SystemExit("linked_issue_identity_malformed")
print(str(expected) + "\t" + str(issue) + "\t" + str(head) + "\t" + str(base))
PY
}

event_tuple() {
  local event="$1"
  python3 - "$event" <<'PY'
import json
import re
import sys
from pathlib import Path

try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    raise SystemExit("slot_ready_event_malformed")
if not isinstance(value, dict):
    raise SystemExit("slot_ready_event_malformed")
def first(*keys):
    for key in keys:
        item = value.get(key)
        if item not in (None, ""):
            return item
    return None
pr = first("pr", "pull_request")
issue = first("issue", "issue_number", "linked_issue")
head = first("headRefOid", "head", "head_sha")
base = first("baseRefOid", "base", "base_sha")
checkout = first("checkout", "checkout_path")
if isinstance(pr, dict):
    pr = pr.get("number")
if isinstance(issue, dict):
    issue = issue.get("number")
if not isinstance(pr, int) or pr <= 0 or not isinstance(issue, int) or issue <= 0:
    raise SystemExit("slot_ready_event_identity_malformed")
if not re.fullmatch(r"[0-9a-f]{40}", str(head or "")) or not re.fullmatch(r"[0-9a-f]{40}", str(base or "")):
    raise SystemExit("slot_ready_event_head_or_base_malformed")
if not isinstance(checkout, str) or not checkout.startswith("/"):
    raise SystemExit("slot_ready_event_checkout_malformed")
print(str(pr) + "\t" + str(issue) + "\t" + str(head) + "\t" + str(base) + "\t" + checkout)
PY
}
pr=""
event=""
fresh_run=0
cancelled_run=""
capture_required=0
capture_run=""
capture_verdict=""
source_run_attempt="$(printenv RLGC_SOURCE_RUN_ATTEMPT 2>/dev/null || true)"
causal_log="$(printenv RLGC_CAUSAL_LOG 2>/dev/null || true)"
local_log="$(printenv RLGC_LOCAL_LOG 2>/dev/null || true)"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --pr) pr="$2"; shift 2 ;;
    --slot-ready-event|--event) event="$2"; shift 2 ;;
    --fresh-run-recovery) fresh_run=1; shift ;;
    --cancelled-run|--cancelled-e2e-run) cancelled_run="$2"; shift 2 ;;
    --capture-required) capture_required=1; shift ;;
    --capture-run) capture_run="$2"; shift 2 ;;
    --capture-verdict) capture_verdict="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --recover-stale-capture|--recover-missing-edge|--recover-pm-override|--recover-pm-override-projected|--override-marker)
      die "retired_recovery_mode_unsupported_use_exact_head_admission" ;;
    *) die "unknown_arg=$1" ;;
  esac
done

[ -f "$ADMISSION_ADAPTER" ] || die "native_admission_adapter_missing path=$ADMISSION_ADAPTER"
[ -x "$ADMISSION_ADAPTER" ] || die "native_admission_adapter_not_executable path=$ADMISSION_ADAPTER"
[ -z "$pr" ] || [ -z "$event" ] || die "use either --pr or --slot-ready-event, not both"

if [ -n "$event" ]; then
  require_file "$event"
  IFS=$'\t' read -r pr issue head base CHECKOUT < <(event_tuple "$event") \
    || die "slot_ready_event_rejected"
else
  [[ "$pr" =~ ^[0-9]+$ ]] || die "--pr must be numeric"
  IFS=$'\t' read -r number issue head base < <(live_tuple "$pr") \
    || die "live_pr_tuple_rejected"
  pr="$number"
fi

validate_tuple "$pr" "$issue" "$head" "$base"
[ -d "$CHECKOUT" ] || die "checkout_missing path=$CHECKOUT"

if [ "$capture_required" -eq 1 ]; then
  [ -n "$capture_run" ] || die "capture_run_required_explicit_arg"
  [ -n "$capture_verdict" ] || die "capture_verdict_required_explicit_arg"
  require_file "$capture_verdict"
else
  [ -z "$capture_run" ] || die "capture_required_flag_missing"
  [ -z "$capture_verdict" ] || die "capture_required_flag_missing"
fi

if [ "$fresh_run" -eq 1 ]; then
  [ -z "$event" ] || die "fresh_run_recovery_takes_no_slot_ready_event"
  [ -n "$cancelled_run" ] || die "fresh_run_recovery_requires_cancelled_run"
  [[ "$cancelled_run" =~ ^[0-9]+$ ]] || die "cancelled_run_not_numeric"
  if [ -z "$source_run_attempt" ]; then
    source_run_attempt="$(gh_cli run view "$cancelled_run" --repo "$REPO" --json attempt --jq '.attempt' 2>/dev/null)" \
      || die "source_run_attempt_unavailable"
  fi
  [[ "$source_run_attempt" =~ ^[0-9]+$ ]] || die "source_run_attempt_invalid"
  [ -n "$causal_log" ] || die "reentry_causal_log_required"
  [ -n "$local_log" ] || die "reentry_local_log_required"
  require_file "$causal_log"
  require_file "$local_log"
  exec "$ADMISSION_ADAPTER" --pr "$pr" --issue "$issue" --head "$head" --base "$base" \
    --checkout "$CHECKOUT" --gh "$GH_BIN" --source-run-id "$cancelled_run" \
    --source-run-attempt "$source_run_attempt" --causal-log "$causal_log" \
    --local-log "$local_log"
fi

[ -z "$cancelled_run" ] || die "cancelled_run_requires_fresh_run_recovery"
[ -z "$source_run_attempt" ] || die "source_run_attempt_requires_fresh_run_recovery"
[ -z "$causal_log" ] || die "causal_log_requires_fresh_run_recovery"
[ -z "$local_log" ] || die "local_log_requires_fresh_run_recovery"

exec "$ADMISSION_ADAPTER" --pr "$pr" --issue "$issue" --head "$head" --base "$base" \
  --checkout "$CHECKOUT" --gh "$GH_BIN"
