#!/usr/bin/env bash
# pr-state-sweep -- deterministic open-PR reconciliation before issue dispatch.
#
# This sweep owns current PR motion. Fresh status:todo issue intake dispatches
# only through Skill(direct-assign). Run this first so "no status:todo" cannot hide stale CI blockers,
# blocked rework, merge-ready slot labels, or draft orphan PRs.

set -uo pipefail
export PATH="${PATH:-}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

TRIGGER="manual"
DRY_RUN="${DRY_RUN:-0}"
GH_REPO="${GH_REPO:-heydonna-app/heydonna-app}"
MOP_HOST="${MOP_HOST:-http://localhost:3100}"
PM_OPS="${PM_OPS:-/Users/rajiv/.claude/scripts/pm-ops.py}"
PM_OPS_DB_WAS_EXPLICIT="${PM_OPS_DB+x}"
PM_OPS_DB="${PM_OPS_DB:-/Users/rajiv/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db}"
CAPTURE_REQUIRED="${CAPTURE_REQUIRED:-/Users/rajiv/.claude/scripts/capture-required.py}"
CAPTURE_LOCAL_PROOF="${CAPTURE_LOCAL_PROOF:-/Users/rajiv/.claude/scripts/capture-local-proof.sh}"
REVIEW_BUDGET="${REVIEW_BUDGET:-/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/pr-review-budget.py}"
SCOPE_RISK="${SCOPE_RISK:-/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/pr-scope-risk.py}"
REPO_ROOT="${PR_SWEEP_REPO_ROOT:-/Users/rajiv/Downloads/projects/heydonna-app}"
CONTROL_PLANE_ROOT="${CONTROL_PLANE_ROOT:-/Users/rajiv/.claude/control_plane/current/heydonna}"
FAMILY2_MODULE="${FAMILY2_MODULE:-scripts.pm.control_plane.family2_boundary}"
CI_FAST_TRIAGE="${CI_FAST_TRIAGE:-$REPO_ROOT/scripts/ci/ci-fast-triage.py}"
REQUIRED_CI_JOBS_FILE="${REQUIRED_CI_JOBS_FILE:-$REPO_ROOT/scripts/ci/required-ci-jobs.json}"
LOG="/tmp/pr-state-sweep.log"
SENTINEL="${PR_STATE_SWEEP_SENTINEL:-/tmp/pm-required-pr-reconcile.json}"
CLEAN_PROOF="${PR_STATE_SWEEP_CLEAN_PROOF:-/tmp/pm-pr-state-sweep-clean.json}"
SENTINEL_WRITE_MODE="${PR_SWEEP_WRITE_SENTINEL:-auto}"

# The TEST_OPEN_PRS/TEST_RUNS hooks are production-shape test inputs. Never let
# an omitted PM_OPS_DB make those fixtures write synthetic obligations into the
# live operator database.
SWEEP_TEST_DB=""
if [ -z "$PM_OPS_DB_WAS_EXPLICIT" ] && { [ -n "${TEST_OPEN_PRS:-}" ] || [ -n "${TEST_RUNS:-}" ]; }; then
  SWEEP_TEST_DB="$(mktemp /tmp/pr-state-sweep-test-db.XXXXXX)"
  PM_OPS_DB="$SWEEP_TEST_DB"
  trap 'rm -f "$SWEEP_TEST_DB" "$SWEEP_TEST_DB-shm" "$SWEEP_TEST_DB-wal"' EXIT
fi
export PM_OPS_DB

for arg in "$@"; do
  case "$arg" in
    --trigger=*) TRIGGER="${arg#--trigger=}" ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      sed -n '1,80p' "$0"
      exit 0
      ;;
    *)
      echo "PR_SWEEP_FAILED reason=unknown_arg arg=$arg" >&2
      exit 2
      ;;
  esac
done

TS="$(date -Iseconds)"
MODE_TAG=""
[ "$DRY_RUN" = "1" ] && MODE_TAG="[DRY_RUN]"
TAG="[$TS][trigger=$TRIGGER]${MODE_TAG}"

emit() {
  echo "$TAG $*" | tee -a "$LOG"
}

json_count() {
  python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null
}

one_line_excerpt() {
  tr '\n' ' ' | sed 's/[[:space:]]\{1,\}/ /g' | head -c 240
}

sentinel_write_enabled() {
  case "$SENTINEL_WRITE_MODE" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    0|false|FALSE|no|NO|off|OFF) return 1 ;;
  esac
  case "$TRIGGER" in
    pm-stop|hourly-ops-audit|hourly-*|reconcile-capacity|slot-ready*|new-issue-filed|*-backlog-promoter)
      return 0
      ;;
  esac
  return 1
}

resolve_sentinel_if_clean() {
  [ "$DRY_RUN" != "1" ] || return 0
  [ -f "$SENTINEL" ] || return 0
  SENTINEL="$SENTINEL" TRIGGER="$TRIGGER" python3 <<'PYEOF' 2>/dev/null || true
import datetime
import json
import os

path = os.environ["SENTINEL"]
try:
    data = json.load(open(path, encoding="utf-8"))
except Exception:
    data = {}
if data.get("status") == "pending":
    data["status"] = "resolved"
    data["resolved_at"] = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    data["resolution"] = "pr_state_sweep_clean"
    data["resolved_by_trigger"] = os.environ.get("TRIGGER") or "unknown"
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
PYEOF
}

write_sentinel() {
  local output="$1"
  SENTINEL="$SENTINEL" TRIGGER="$TRIGGER" ACTIONABLE="$output" PRS_JSON="${PRS_JSON:-[]}" python3 <<'PYEOF' 2>/dev/null || true
import datetime
import hashlib
import json
import os
import re

path = os.environ["SENTINEL"]
actionable = os.environ.get("ACTIONABLE", "").strip()
now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
current_lines = [line for line in actionable.splitlines() if line.strip()]

def extract_prs(text):
    nums = []
    for a, b in re.findall(r"PR#(\d+)|pr=(\d+)", text or ""):
        nums.append(a or b)
    return nums
try:
    previous = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
except Exception:
    previous = {}

try:
    prs = json.loads(os.environ.get("PRS_JSON") or "[]")
except Exception:
    prs = []

def label_names(pr):
    return sorted(str(x.get("name") or "") for x in (pr.get("labels") or []) if x.get("name"))

rows = []
latest_updated_at = ""
for pr in sorted(prs, key=lambda p: int(p.get("number") or 0)):
    number = str(pr.get("number") or "")
    head = str(pr.get("headRefOid") or "")
    updated = str(pr.get("updatedAt") or "")
    labels = ",".join(label_names(pr))
    rows.append("|".join([number, head, updated, labels]))
    if updated > latest_updated_at:
        latest_updated_at = updated
open_pr_digest = hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()

# The sentinel is an executable Stop-hook contract, not a backlog ledger.
# Write only the current sweep rows; old rows may be retained as metadata but
# must never be replayed through actionable_output.
current_actionable = "\n".join(current_lines).strip()
pr_nums = sorted(set(extract_prs(current_actionable)), key=int)
slots = sorted(set(re.findall(r"slot[:=]([0-9]+)", current_actionable)), key=int)
previous_actionable = str(previous.get("actionable_output") or "").strip()
previous_prs = sorted(set(extract_prs(previous_actionable)), key=int)
dropped_prior_prs = sorted(set(previous_prs) - set(pr_nums), key=int)
created_at = now.isoformat().replace("+00:00", "Z")
if (
    previous.get("status") == "pending"
    and previous.get("created_at")
    and previous_actionable == current_actionable
):
    created_at = previous.get("created_at")

def parse_action_row(line):
    parts = line.split()
    row = {
        "raw": line,
        "kind": parts[0] if parts else "",
    }
    m = re.search(r"\bPR#(\d+)\b", line)
    if m:
        row["pr"] = int(m.group(1))
    m = re.search(r"\bissue=#?(\d+)\b", line)
    if m:
        row["issue"] = int(m.group(1))
    m = re.search(r"\bslot:([0-9]+)\b", line)
    if m:
        row["slot"] = int(m.group(1))
    for token in parts[1:]:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        key = key.strip()
        if not key or key in row:
            continue
        row[key] = value
    return row

action_rows = [parse_action_row(line) for line in current_lines]
data = {
    "schema_version": 1,
    "source": "pr-state-sweep",
    "status": "pending",
    "created_at": created_at,
    "updated_at": now.isoformat().replace("+00:00", "Z"),
    "trigger": os.environ.get("TRIGGER") or "unknown",
    "reason": "pr_state_sweep_actionable",
    "required_command": "UNSUPPORTED_LIFECYCLE_ACTION:reconcile-capacity",
    "prs": pr_nums,
    "slots": slots,
    "actionable_output": current_actionable,
    "action_rows": action_rows,
    "carried_over_prs": [],
    "dropped_prior_prs": dropped_prior_prs,
    "open_pr_count": len(prs),
    "latest_pr_updated_at": latest_updated_at,
    "open_pr_digest": open_pr_digest,
    "log": "/tmp/pr-state-sweep.log",
}
tmp = f"{path}.{os.getpid()}.tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
os.replace(tmp, path)
PYEOF
}

write_clean_proof() {
  PRS_JSON="$PRS_JSON" CLEAN_PROOF="$CLEAN_PROOF" TRIGGER="$TRIGGER" python3 <<'PYEOF' 2>/dev/null || true
import datetime
import hashlib
import json
import os

path = os.environ["CLEAN_PROOF"]
trigger = os.environ.get("TRIGGER") or "unknown"
try:
    prs = json.loads(os.environ.get("PRS_JSON") or "[]")
except Exception:
    prs = []

def label_names(pr):
    return sorted(str(x.get("name") or "") for x in (pr.get("labels") or []) if x.get("name"))

rows = []
latest_updated_at = ""
for pr in sorted(prs, key=lambda p: int(p.get("number") or 0)):
    number = str(pr.get("number") or "")
    head = str(pr.get("headRefOid") or "")
    updated = str(pr.get("updatedAt") or "")
    labels = ",".join(label_names(pr))
    rows.append("|".join([number, head, updated, labels]))
    if updated > latest_updated_at:
        latest_updated_at = updated

digest = hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()
now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
data = {
    "schema_version": 1,
    "source": "pr-state-sweep",
    "status": "clean",
    "created_at": now.isoformat().replace("+00:00", "Z"),
    "updated_at": now.isoformat().replace("+00:00", "Z"),
    "trigger": trigger,
    "open_pr_count": len(prs),
    "latest_pr_updated_at": latest_updated_at,
    "open_pr_digest": digest,
    "open_prs": [int(p.get("number")) for p in prs if p.get("number") is not None],
    "log": "/tmp/pr-state-sweep.log",
}
tmp = f"{path}.{os.getpid()}.tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, sort_keys=True)
    f.write("\n")
os.replace(tmp, path)
PYEOF
}

if [ "${PR_STATE_SWEEP_WRITE_SENTINEL_ONLY:-0}" = "1" ]; then
  write_sentinel "${PR_STATE_SWEEP_ACTIONABLE:-}"
  exit 0
fi

release_slot_label_for_pr() {
  local pr="$1" slot="$2" issue="$3" branch="$4" reason="${5:-merge-ready-slot-label}"
  local mop_json release_decision should_release expected_epoch release_output mop_release
  [ "$DRY_RUN" != "1" ] || return 0
  [ -n "$slot" ] || return 0
  mop_json="$(curl -fsS -m 4 "$MOP_HOST/slots" 2>/dev/null || true)"
  release_decision="$(MOP_JSON="$mop_json" PR="$pr" ISSUE="$issue" BRANCH="$branch" SLOT="$slot" python3 <<'PYEOF' 2>/dev/null || true
import json
import os

try:
    data = json.loads(os.environ.get("MOP_JSON") or "{}")
except Exception:
    data = {}

slot = str(os.environ.get("SLOT") or "")
pr = str(os.environ.get("PR") or "")
issue = str(os.environ.get("ISSUE") or "")
branch = str(os.environ.get("BRANCH") or "")

entry = next((s for s in data.get("slots", []) if str(s.get("slot") or "") == slot), None)
if not entry or not entry.get("occupied"):
    print("no\t")
    raise SystemExit(0)

task = str(entry.get("task") or "")
entry_repository = str(entry.get("repository_id") or "")
entry_pr = str(entry.get("pr") or "")
entry_issue = str(entry.get("issue") or "")
entry_branch = str(entry.get("branch") or "")
entry_head = str(entry.get("head_sha") or "")

matches = (
    (pr and (entry_pr == pr or f"#{pr}" in task))
    or (issue and (entry_issue == issue or f"#{issue}" in task))
    or (branch and entry_branch == branch)
)
epoch = entry.get("assignment_epoch")
if matches and isinstance(epoch, int) and epoch >= 0 and entry_repository and entry_branch:
    print("\t".join(("yes", str(epoch), entry_repository, entry_issue, entry_pr, entry_branch, entry_head)))
elif matches:
    print("unsafe\t")
else:
    print("no\t")
PYEOF
)"
  IFS="$(printf '\t')" read -r should_release expected_epoch expected_repository expected_issue expected_pr expected_branch expected_head <<< "$release_decision"
  if [ "$should_release" = "yes" ]; then
    emit "PR_SLOT_RELEASE_REQUIRED PR#$pr slot:$slot issue=#${issue:-0} branch=${branch:-unknown} expected_epoch=$expected_epoch reason=$reason action=Skill(direct-release) tuple=repository:${expected_repository},issue:${expected_issue},pr:${expected_pr},branch:${expected_branch},head:${expected_head}"
    if [ -x "$PM_OPS" ]; then
      python3 "$PM_OPS" record \
        --source pr-state-sweep \
        --event pr_slot_release_delegated \
        --target-type pr \
        --target-id "$pr" \
        --pr "$pr" \
        --issue "${issue:-0}" \
        --slot "$slot" \
        --payload "branch=$branch" \
        --payload "reason=$reason" \
        --payload "expected_epoch=$expected_epoch" \
        --payload "action=Skill(direct-release)" >/dev/null 2>&1 || true
    fi
    return 1
  elif [ "$should_release" = "unsafe" ] || [ -z "$should_release" ]; then
    emit "PR_SLOT_RELEASE_FAILED PR#$pr slot:$slot issue=#${issue:-0} branch=${branch:-unknown} reason=assignment_epoch_unavailable"
    return 1
  fi
}

upsert_obligation() {
  [ "$DRY_RUN" != "1" ] || return 0
  [ -x "$PM_OPS" ] || return 0
  python3 "$PM_OPS" obligation-upsert "$@" >/dev/null 2>&1 || true
}

if [ -d "$REPO_ROOT" ]; then
  cd "$REPO_ROOT" || {
    emit "PR_SWEEP_FAILED reason=repo_root_cd_failed repo_root=$REPO_ROOT"
    exit 2
  }
fi

emit "PR_SWEEP_CONTEXT repo=$GH_REPO cwd=$(pwd)"

# macOS/BSD mktemp requires the X's to be trailing; a suffix after XXXXXX can be
# treated literally and collide across sweeps.
PR_ERR="$(mktemp /tmp/pr-state-sweep-gh.XXXXXX)"
PRS_JSON="$(gh pr list --repo "$GH_REPO" --state open --limit 100 --json number,title,body,closingIssuesReferences,isDraft,labels,headRefName,headRefOid,mergeStateStatus,statusCheckRollup,createdAt,updatedAt,url 2>"$PR_ERR")"
PR_RC=$?
if [ "$PR_RC" -ne 0 ] || [ -z "$PRS_JSON" ]; then
  emit "PR_SWEEP_FAILED reason=gh_pr_list_failed exit=$PR_RC stderr='$(one_line_excerpt <"$PR_ERR")'"
  rm -f "$PR_ERR"
  exit 2
fi
rm -f "$PR_ERR"

PR_COUNT="$(printf '%s' "$PRS_JSON" | json_count || echo parse_error)"
if [ "$PR_COUNT" = "parse_error" ]; then
  emit "PR_SWEEP_FAILED reason=gh_pr_list_parse_failed"
  exit 2
fi
emit "PR_SWEEP_OPEN count=$PR_COUNT"

MERGED_ERR="$(mktemp /tmp/pr-state-sweep-merged-gh.XXXXXX)"
MERGED_JSON="$(gh pr list --repo "$GH_REPO" --state merged --limit 50 --json number,title,labels,mergedAt,headRefOid,url 2>"$MERGED_ERR" || true)"
if [ -z "$MERGED_JSON" ]; then
  MERGED_JSON="[]"
fi
rm -f "$MERGED_ERR"

MOP_JSON="$(curl -fsS -m 5 "$MOP_HOST/slots" 2>/dev/null || true)"

ACTION_LINES_FILE="$(mktemp /tmp/pr-state-sweep-actions.XXXXXX)"
if ! PRS_JSON="$PRS_JSON" MERGED_JSON="$MERGED_JSON" MOP_JSON="$MOP_JSON" SENTINEL="$SENTINEL" REPO_ROOT="$REPO_ROOT" PR_SWEEP_RUNTIME_OBSERVATION="${PR_SWEEP_RUNTIME_OBSERVATION:-}" PR_SWEEP_SOURCE_OVERRIDE="${PR_SWEEP_SOURCE_OVERRIDE:-0}" TRIGGER="$TRIGGER" DRY_RUN="$DRY_RUN" REMOTE_CAPTURE_ONLY="${REMOTE_CAPTURE_ONLY:-1}" CAPTURE_REQUIRED="$CAPTURE_REQUIRED" CAPTURE_LOCAL_PROOF="$CAPTURE_LOCAL_PROOF" REVIEW_BUDGET="$REVIEW_BUDGET" SCOPE_RISK="$SCOPE_RISK" CI_FAST_TRIAGE="$CI_FAST_TRIAGE" PM_OPS_DB="$PM_OPS_DB" MOP_CLEANUP_RECEIPT_PATH="${MOP_CLEANUP_RECEIPT_PATH:-/Users/rajiv/.claude/mop/pm-cleanup-receipts.json}" REQUIRED_CI_JOBS_FILE="$REQUIRED_CI_JOBS_FILE" LOCAL_PREFLIGHT_VALIDATOR="${LOCAL_PREFLIGHT_VALIDATOR:-$REPO_ROOT/.claude/scripts/local-preflight-proof.py}" REWORK_PACKET_LEDGER="${REWORK_PACKET_LEDGER:-/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/rework-packet-ledger.py}" python3 >"$ACTION_LINES_FILE" <<'PYEOF'
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import importlib.util
import sys
from pathlib import Path
from datetime import datetime, timezone

_runtime_observation_path = os.environ.get("PR_SWEEP_RUNTIME_OBSERVATION")
if os.environ.get("PR_SWEEP_SOURCE_OVERRIDE") == "1":
    _runtime_observation_path = str(
        Path(os.environ.get("REPO_ROOT") or "/Users/rajiv/Downloads/projects/heydonna-app")
        / "scripts/pm/control_plane/runtime_observation.py"
    )
if not _runtime_observation_path:
    _runtime_observation_path = str(Path.home() / ".claude/control_plane/runtime_observation.py")
_runtime_spec = importlib.util.spec_from_file_location(
    "heydonna_installed_runtime_observation", _runtime_observation_path
)
if _runtime_spec is None or _runtime_spec.loader is None:
    raise RuntimeError(f"runtime observation module unavailable: {_runtime_observation_path}")
_runtime_module = importlib.util.module_from_spec(_runtime_spec)
sys.modules[_runtime_spec.name] = _runtime_module
_runtime_spec.loader.exec_module(_runtime_module)
incomplete_owner_identity = _runtime_module.incomplete_owner_identity
suppress_repeated_incomplete_owner = _runtime_module.suppress_repeated_incomplete_owner

prs = json.loads(os.environ["PRS_JSON"])
try:
    merged_prs = json.loads(os.environ.get("MERGED_JSON") or "[]")
except Exception:
    merged_prs = []
gh_repo = os.environ.get("GH_REPO") or "heydonna-app/heydonna-app"
try:
    required_ci_jobs = json.loads(
        Path(os.environ["REQUIRED_CI_JOBS_FILE"]).read_text(encoding="utf-8")
    )
    if not isinstance(required_ci_jobs, dict):
        required_ci_jobs = {}
except Exception:
    required_ci_jobs = {}
capture_required_script = os.environ.get("CAPTURE_REQUIRED") or "/Users/rajiv/.claude/scripts/capture-required.py"
capture_local_proof_script = os.environ.get("CAPTURE_LOCAL_PROOF") or "/Users/rajiv/.claude/scripts/capture-local-proof.sh"
local_preflight_validator = os.environ.get("LOCAL_PREFLIGHT_VALIDATOR") or ""
review_budget_script = os.environ.get("REVIEW_BUDGET") or "/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/pr-review-budget.py"
scope_risk_script = os.environ.get("SCOPE_RISK") or "/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/pr-scope-risk.py"
ci_fast_triage_script = os.environ.get("CI_FAST_TRIAGE") or "/Users/rajiv/Downloads/projects/heydonna-app/scripts/ci/ci-fast-triage.py"
stale_ci_minutes = int(os.environ.get("PR_SWEEP_STALE_CI_MINUTES") or "45")
stale_capture_minutes = int(os.environ.get("PR_SWEEP_STALE_CAPTURE_MINUTES") or "90")
pm_review_stale_minutes = int(os.environ.get("PR_SWEEP_PM_REVIEW_STALE_MINUTES") or "30")
dependency_wedge_warn_minutes = int(os.environ.get("PR_SWEEP_DEPENDENCY_WEDGE_WARN_MINUTES") or "240")
review_loop_refresh_minutes = int(os.environ.get("PR_SWEEP_REVIEW_LOOP_REFRESH_MINUTES") or "15")
review_loop_refresh_enabled = str(os.environ.get("PR_SWEEP_RUN_REVIEW_LOOP_REFRESH") or "").lower() in {"1", "true", "yes"}
scope_risk_refresh_enabled = str(os.environ.get("PR_SWEEP_RUN_SCOPE_RISK_REFRESH") or "").lower() in {"1", "true", "yes"}
merge_unknown_grace_seconds = int(os.environ.get("PR_SWEEP_MERGE_UNKNOWN_GRACE_SECONDS") or "90")
old_pr_age_hours = int(os.environ.get("PR_SWEEP_OLD_PR_AGE_HOURS") or "6")
main_behind_threshold = int(os.environ.get("PR_SWEEP_MAIN_BEHIND_THRESHOLD") or "5")
merge_unknown_watch_path = Path(os.environ.get("PR_SWEEP_MERGE_UNKNOWN_WATCH") or "/tmp/pr-merge-ready-unknown-watch.json")
trigger = os.environ.get("TRIGGER") or "manual"
dry_run = os.environ.get("DRY_RUN") == "1"
CONTROL_PLANE_ROOT = os.environ.get("CONTROL_PLANE_ROOT") or "/Users/rajiv/.claude/control_plane/current/heydonna"
FAMILY2_MODULE = os.environ.get("FAMILY2_MODULE") or "scripts.pm.control_plane.family2_boundary"
# Remote capture is the default and authoritative lane. Local capture is
# diagnostic-only (capture-local-required with a named infrastructure defect)
# and never satisfies capture readiness or serves as a fallback after remote
# red, so the sweep always routes capture actions through the remote lane.
remote_capture_only = True
dependency_ack_dir = Path(os.environ.get("PR_SWEEP_DEPENDENCY_ACK_DIR") or "/tmp/pm-dependency-watch-acks")
dependency_log = Path(os.environ.get("PR_SWEEP_LOG") or "/tmp/pr-state-sweep.log")
delivery_ack_dir = Path(os.environ.get("PR_SWEEP_DELIVERY_ACK_DIR") or "/tmp/pm-delivery-acks")
rework_packet_ledger = Path(os.environ.get("REWORK_PACKET_LEDGER") or "/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/rework-packet-ledger.py")
clone_lock_dir = Path(os.environ.get("PM_CLONE_LOCK_DIR") or "/tmp")
pm_ops_db = Path(os.environ.get("PM_OPS_DB") or "")
try:
    previous_sentinel = json.loads(Path(os.environ["SENTINEL"]).read_text(encoding="utf-8"))
    previous_action_rows = previous_sentinel.get("action_rows") or []
except Exception:
    previous_action_rows = []
incomplete_owner_seen: set[tuple[object, ...]] = set()


def emit_incomplete_owner(entry: dict) -> bool:
    """Deduplicate at the authoritative MoP tuple boundary before rendering."""

    if suppress_repeated_incomplete_owner(previous_action_rows, entry):
        return False
    identity = incomplete_owner_identity(entry)
    if identity is None:
        return True
    if identity in incomplete_owner_seen:
        return False
    incomplete_owner_seen.add(identity)
    return True


def owner_tokens(entry: dict) -> str:
    """Render authoritative MoP identity alongside existing action prose."""

    return (
        f"owner_repository_id={entry.get('repository_id') if entry.get('repository_id') is not None else 'null'} "
        f"owner_slot={entry.get('slot') or entry.get('id') or 'null'} "
        f"owner_epoch={entry.get('assignment_epoch') if entry.get('assignment_epoch') is not None else 'null'} "
        f"owner_issue={entry.get('issue') if entry.get('issue') is not None else 'null'} "
        f"owner_pr={entry.get('pr') if entry.get('pr') is not None else 'null'}"
    )
cleanup_receipt_path = Path(
    os.environ.get("MOP_CLEANUP_RECEIPT_PATH")
    or str(Path.home() / ".claude/mop/pm-cleanup-receipts.json")
)
cleanup_receipts = {}
cleanup_journal_error = ""


def cleanup_receipt_identity(receipt):
    """Return a syntactic PR/head alias without trusting the receipt yet."""

    if not isinstance(receipt, dict) or not isinstance(receipt.get("request"), dict):
        return None
    request = receipt["request"]
    try:
        pr_number = int(request.get("pr"))
    except (TypeError, ValueError):
        return None
    head = str(request.get("head") or "").strip().lower()
    if pr_number <= 0 or re.fullmatch(r"[0-9a-f]{40}", head) is None:
        return None
    return pr_number, head


def validate_cleanup_receipt(key, receipt):
    """Validate the existing writer's record before it can suppress closeout."""

    if not isinstance(key, str) or not re.fullmatch(r"mop-cleanup:[0-9a-f]{64}", key):
        return False, "receipt_key_invalid"
    if not isinstance(receipt, dict):
        return False, "receipt_not_an_object"
    request = receipt.get("request")
    if not isinstance(request, dict):
        return False, "receipt_request_missing"
    required = {"repository", "pr", "issue", "head", "merge_commit"}
    if not required.issubset(request):
        return False, "receipt_request_incomplete"
    if request.get("repository") != gh_repo:
        return False, "receipt_repository_mismatch"
    try:
        pr_number = int(request.get("pr"))
    except (TypeError, ValueError):
        return False, "receipt_pr_invalid"
    if pr_number <= 0:
        return False, "receipt_pr_invalid"
    head = str(request.get("head") or "").strip().lower()
    merge_commit = str(request.get("merge_commit") or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", head) is None:
        return False, "receipt_head_invalid"
    if re.fullmatch(r"[0-9a-f]{40}", merge_commit) is None:
        return False, "receipt_merge_commit_invalid"
    mode = receipt.get("cleanup_mode")
    if mode not in {"linked_issue", "merged_pr_issue_less"}:
        return False, "receipt_mode_invalid"
    if mode == "merged_pr_issue_less":
        if request.get("issue") is not None or receipt.get("thread_ts") is not None:
            return False, "receipt_issue_less_shape_invalid"
    else:
        try:
            if int(request.get("issue")) <= 0:
                return False, "receipt_issue_invalid"
        except (TypeError, ValueError):
            return False, "receipt_issue_invalid"
        if not isinstance(receipt.get("thread_ts"), str) or not receipt["thread_ts"].strip():
            return False, "receipt_thread_missing"
    identity = {
        field: request[field]
        for field in ("repository", "pr", "issue", "head", "merge_commit")
    }
    expected_keys = {
        "mop-cleanup:" + hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()
    }
    if receipt.get("thread_ts") is not None:
        identity["thread_ts"] = receipt["thread_ts"]
        expected_keys.add(
            "mop-cleanup:" + hashlib.sha256(
                json.dumps(identity, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
            ).hexdigest()
        )
    if key not in expected_keys:
        return False, "receipt_key_identity_mismatch"
    if receipt.get("status") not in {"prepared", "processing", "completed", "ambiguous"}:
        return False, "receipt_status_invalid"
    if not isinstance(receipt.get("plan"), list):
        return False, "receipt_plan_invalid"
    if not isinstance(receipt.get("steps"), dict):
        return False, "receipt_steps_invalid"
    if not isinstance(receipt.get("ambiguous_steps"), list):
        return False, "receipt_ambiguity_invalid"
    if not isinstance(receipt.get("payload_sha256"), str) or re.fullmatch(r"[0-9a-f]{64}", receipt["payload_sha256"].lower()) is None:
        return False, "receipt_payload_digest_invalid"
    return True, ""


try:
    if cleanup_receipt_path.is_file():
        raw_cleanup_receipts = json.loads(
            cleanup_receipt_path.read_text(encoding="utf-8")
        )
        if not isinstance(raw_cleanup_receipts, dict):
            raise ValueError("cleanup journal must be an object")
        for key, receipt in raw_cleanup_receipts.items():
            identity = cleanup_receipt_identity(receipt)
            if identity is None:
                continue
            valid, reason = validate_cleanup_receipt(key, receipt)
            cleanup_receipts.setdefault(identity, []).append(
                {
                    "valid": valid,
                    "reason": reason,
                    "status": str(receipt.get("status") or "unknown"),
                    "started_at": str(receipt.get("started_at") or ""),
                }
            )
except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
    cleanup_journal_error = f"cleanup journal unavailable: {exc}"


def cleanup_state_for(pr_number, head):
    """Join one merged PR to the existing direct-cleanup journal by exact head."""

    if cleanup_journal_error:
        return {"state": "unavailable"}
    identity = (int(pr_number), str(head or "").strip().lower())
    records = cleanup_receipts.get(identity) or []
    if not records:
        return {"state": "missing"}
    if len(records) != 1 or not records[0].get("valid"):
        return {"state": "uncertain", "reason": records[0].get("reason", "receipt_ambiguous")}
    record = records[0]
    status = record.get("status")
    if status == "completed":
        return {"state": "completed"}
    if status in {"prepared", "processing"}:
        return {"state": "started", "started_at": record.get("started_at") or ""}
    return {"state": "uncertain"}


capture_rearm_by_pr = {}
capture_rework_by_pr = {}
capture_rearm_authority_available = False
if pm_ops_db.exists():
    try:
        con = sqlite3.connect(f"file:{pm_ops_db}?mode=ro", uri=True)
        for row in con.execute(
            """
            SELECT id, pr, issue, slot, evidence_json
            FROM obligations
            WHERE status='open'
              AND kind='capture_rearm_after_main_sync'
              AND blocker='capture_rearm_after_main_sync'
              AND pr IS NOT NULL
            ORDER BY id DESC
            """
        ):
            obligation_id, pr_number, issue_number, slot_number, evidence_json = row
            try:
                evidence = json.loads(evidence_json or "{}")
                pr_number = int(pr_number)
                slot_number = int(slot_number)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            capture_rearm_by_pr.setdefault(
                pr_number,
                {
                    "id": int(obligation_id),
                    "issue": int(issue_number) if issue_number is not None else None,
                    "slot": slot_number,
                    "branch": str(evidence.get("branch") or ""),
                    "main_head": str(evidence.get("main_head") or ""),
                    "pre_sync_head": str(evidence.get("pre_sync_head") or ""),
                },
            )
        for row in con.execute(
            """
            SELECT id, pr, issue, slot, evidence_json
            FROM obligations
            WHERE status='open'
              AND kind='capture_rework_assignment'
              AND blocker='capture_rework_assignment'
              AND pr IS NOT NULL
            ORDER BY id DESC
            """
        ):
            obligation_id, pr_number, issue_number, slot_number, evidence_json = row
            try:
                evidence = json.loads(evidence_json or "{}")
                pr_number = int(pr_number)
                slot_number = int(slot_number)
                assignment_epoch = int(evidence.get("assignment_epoch"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            capture_rework_by_pr.setdefault(
                pr_number,
                {
                    "id": int(obligation_id),
                    "issue": int(issue_number) if issue_number is not None else None,
                    "slot": slot_number,
                    "branch": str(evidence.get("branch") or ""),
                    "assignment_epoch": assignment_epoch,
                    "assigned_head": str(evidence.get("assigned_head") or ""),
                    "preserve_capture_gate": str(
                        evidence.get("preserve_capture_gate") or ""
                    ).lower(),
                    "post_rework_capture": str(
                        evidence.get("post_rework_capture") or ""
                    ),
                },
            )
        capture_rearm_authority_available = True
        con.close()
    except Exception:
        capture_rearm_by_pr = {}
        capture_rework_by_pr = {}
        capture_rearm_authority_available = False

if dry_run:
    _real_subprocess_run = subprocess.run

    def _dry_run_subprocess(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "")

    subprocess.run = _dry_run_subprocess

def read_only_subprocess_run(cmd, *args, **kwargs):
    """Allow evidence reads in dry-run while preserving the global mutation guard."""
    runner = globals().get("_real_subprocess_run", subprocess.run)
    return runner(cmd, *args, **kwargs)

try:
    mop = json.loads(os.environ.get("MOP_JSON") or "{}")
except Exception:
    mop = {}

slots = mop.get("slots") or []
free_slots = [
    int(s.get("slot"))
    for s in slots
    if s.get("slot") is not None and not s.get("occupied") and not s.get("dnd")
]
slot_by_num = {int(s.get("slot")): s for s in slots if s.get("slot") is not None}

def labels(pr):
    return [x.get("name", "") for x in pr.get("labels") or []]

def has_label(pr, name):
    return name in labels(pr)

def any_label(pr, pred):
    return [x for x in labels(pr) if pred(x)]

def state_label(pr):
    states = any_label(pr, lambda x: x.startswith("pm-state:"))
    return states[0] if states else ""

def capture_rearm_watch(pr_number, active_slot, branch):
    obligation = capture_rearm_by_pr.get(int(pr_number))
    if not obligation or not active_slot:
        return None
    try:
        active_slot_number = int(active_slot.get("slot"))
    except (TypeError, ValueError):
        return None
    if (
        obligation.get("slot") != active_slot_number
        or obligation.get("branch") != str(branch or "")
        or len(str(obligation.get("main_head") or "")) != 40
        or len(str(obligation.get("pre_sync_head") or "")) != 40
    ):
        return None
    return obligation

def capture_rework_watch(pr_number, active_slot, branch):
    obligation = capture_rework_by_pr.get(int(pr_number))
    if not obligation or not active_slot:
        return None
    try:
        active_slot_number = int(active_slot.get("slot"))
        active_epoch = int(active_slot.get("assignment_epoch"))
    except (TypeError, ValueError):
        return None
    if (
        obligation.get("slot") != active_slot_number
        or obligation.get("branch") != str(branch or "")
        or obligation.get("assignment_epoch") != active_epoch
        or obligation.get("preserve_capture_gate") != "true"
        or obligation.get("post_rework_capture") != "required"
        or len(str(obligation.get("assigned_head") or "")) != 40
    ):
        return None
    return obligation

def pm_state_labels(pr):
    return any_label(pr, lambda x: x.startswith("pm-state:"))

def effective_state(pr):
    if has_label(pr, "merge-ready") or has_label(pr, "pm-state:merge-ready"):
        return "merge-ready"
    return state_label(pr)

def state_drift_reason(pr):
    states = pm_state_labels(pr)
    blockers = any_label(pr, lambda x: x.startswith("pm-blocked:"))
    allowed_states = {
        "pm-state:draft-qa-needed",
        "pm-state:qa-running",
        "pm-state:qa-failed-rework",
        "pm-state:qa-passed-awaiting-ci",
        "pm-state:pm-review-pending",
        "pm-state:blocked-rework",
        "pm-state:rescope-required",
    }
    allowed_blockers = {
        "pm-blocked:ci",
        "pm-blocked:capture",
        "pm-blocked:codex",
        "pm-blocked:rebase",
        "pm-blocked:pm-gate",
        "pm-blocked:dependency",
        "pm-blocked:product",
        "pm-blocked:infra",
        "pm-blocked:cto",
    }
    unknown_states = [s for s in states if s not in allowed_states and s != "pm-state:merge-ready"]
    unknown_blockers = [b for b in blockers if b not in allowed_blockers]
    if unknown_states:
        return f"unknown_pm_state labels={','.join(unknown_states)}"
    if unknown_blockers:
        return f"unknown_pm_blocker labels={','.join(unknown_blockers)}"
    if len(blockers) > 1:
        # A qa-passed PR may temporarily carry both labels during a dependency
        # watch: pm-blocked:dependency is the visible terminal state, and a
        # stale/legacy pm-blocked:ci label must not preempt the dependency
        # handler when a current-head dependency marker exists. If the marker is
        # missing, keep treating multiple blockers as drift so PM repairs the
        # labels/proof instead of hiding an invalid state.
        if set(blockers) == {"pm-blocked:ci", "pm-blocked:dependency"}:
            dep_state, _dep_reason = ci_dependency_blocked_state(
                int(pr.get("number") or 0),
                str(pr.get("headRefOid") or ""),
            )
            if dep_state in {"blocked", "unblocked"}:
                return ""
        return f"multiple_pm_blockers blockers={','.join(blockers)}"
    if has_label(pr, "merge-ready"):
        extra_states = [s for s in states if s != "pm-state:merge-ready"]
        if extra_states:
            return f"merge_ready_has_pm_state labels={','.join(extra_states)}"
    return ""

def slot_labels(pr):
    out = []
    for label in labels(pr):
        m = re.fullmatch(r"slot:([1-4])", label)
        if m:
            out.append(int(m.group(1)))
    return out

def linked_issue(pr):
    for ref in pr.get("closingIssuesReferences") or []:
        number = str(ref.get("number") or "") if isinstance(ref, dict) else ""
        if number:
            return number
    branch = pr.get("headRefName") or ""
    m = re.fullmatch(
        r"(?:.*/)?(?:(?:fix|feat|feature|bug|test|chore|perf|refactor|enhance)/)?"
        r"(?P<issue>[0-9]{3,6})(?:[-_/].*)?",
        branch.strip(),
    )
    if m:
        return m.group("issue")
    title = pr.get("title") or ""
    body = pr.get("body") or ""
    for text in (title, body):
        m = re.search(r"(?i)\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\s+#(\d+)\b", text)
        if m:
            return m.group(1)
    m = re.search(r"#(\d+)", title)
    if m:
        return m.group(1)
    m = re.search(r"(?:^|[^0-9])(\d{4})(?:[^0-9]|$)", title)
    return m.group(1) if m else ""

def check_summary(pr):
    checks = pr.get("statusCheckRollup") or []
    names = []
    bad = []
    success_names = []
    success = 0
    pending = 0
    skipped = 0
    for c in checks:
        name = c.get("name") or c.get("workflowName") or c.get("__typename") or "check"
        conclusion = (c.get("conclusion") or "").upper()
        status = (c.get("status") or "").upper()
        if status and status != "COMPLETED":
            pending += 1
            names.append(f"{name}:PENDING")
        elif conclusion == "SUCCESS":
            success += 1
            success_names.append(name)
        elif conclusion == "SKIPPED":
            skipped += 1
        elif conclusion:
            bad.append(f"{name}:{conclusion}")
    if pending:
        status = "pending"
    elif bad:
        status = "terminal_bad"
    elif success:
        status = "terminal_green"
    else:
        status = "unknown"
    return status, bad, success, skipped, success_names

def has_required_label_gated_ci(success_names):
    normalized = [str(name).lower() for name in success_names]
    has_ci = any(name in ("ci", "test") or "ci" in name for name in normalized)
    has_e2e = any("e2e" in name or "smoke" in name for name in normalized)
    return has_ci and has_e2e

def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None

def merge_unknown_watch_status(pr_number, head, issue, branch):
    now = datetime.now(timezone.utc)
    key = f"{pr_number}:{head or 'unknown'}"
    try:
        data = json.loads(merge_unknown_watch_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    rows = data.get("rows") if isinstance(data.get("rows"), dict) else {}

    # Drop stale rows for the same PR on older heads so a push never inherits a
    # prior transient UNKNOWN observation.
    for old_key in list(rows):
        if old_key.startswith(f"{pr_number}:") and old_key != key:
            rows.pop(old_key, None)

    row = rows.get(key) if isinstance(rows.get(key), dict) else None
    if not row:
        row = {
            "first_seen": now.isoformat().replace("+00:00", "Z"),
            "pr": int(pr_number),
            "head": head,
            "issue": issue,
            "branch": branch,
        }
        rows[key] = row
        status = "watch"
        age = 0
    else:
        first_seen = parse_time(row.get("first_seen"))
        age = int((now - first_seen).total_seconds()) if first_seen else 0
        status = "persistent" if age >= merge_unknown_grace_seconds else "watch"
        row["last_seen"] = now.isoformat().replace("+00:00", "Z")
        row["seen_count"] = int(row.get("seen_count") or 1) + 1

    data["rows"] = rows
    data["updated_at"] = now.isoformat().replace("+00:00", "Z")
    try:
        tmp = merge_unknown_watch_path.with_name(f"{merge_unknown_watch_path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(merge_unknown_watch_path)
    except Exception:
        pass
    return status, age

def merge_unknown_watch_clear(pr_number, head):
    key = f"{pr_number}:{head or 'unknown'}"
    try:
        data = json.loads(merge_unknown_watch_path.read_text(encoding="utf-8"))
        rows = data.get("rows") if isinstance(data.get("rows"), dict) else {}
    except Exception:
        return
    changed = False
    for old_key in list(rows):
        if old_key == key or old_key.startswith(f"{pr_number}:"):
            rows.pop(old_key, None)
            changed = True
    if not changed:
        return
    data["rows"] = rows
    data["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        tmp = merge_unknown_watch_path.with_name(f"{merge_unknown_watch_path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(merge_unknown_watch_path)
    except Exception:
        pass

CAPTURE_FORMAT_DONE_MARKER = "E2E_CAPTURE_FORMAT_ONLY=true: pipeline reached terminal formatted output"
CAPTURE_FIXTURE_WRITE_MARKERS = (
    "llm_proxy_r2_persist",
    "aai_fixture_save_success",
    "aai_fixture_saved",
)

def _capture_modal_volume_text(run_id, attempt, name, timeout=8):
    if dry_run:
        return ""
    if not run_id:
        return ""
    attempt = str(attempt or "1")
    remote = f"/run-{run_id}-{attempt}/{name}"
    try:
        proc = subprocess.run(
            ["modal", "volume", "get", "ci-runner-cache", remote, "-", "--env", "main"],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout or ""

def capture_artifact_success(run_id, attempt=None):
    """True when capture reached formatted output and wrote fresh fixtures.

    The workflow may fail later on assertions or cleanup. For PM state, capture
    success is the artifact contract: format terminal reached + fixture write
    proof. Newer runs persist capture-success.json; older runs are inferred
    from persisted logs when possible.
    """
    if not run_id:
        return False, {}
    if not hasattr(capture_artifact_success, "_cache"):
        capture_artifact_success._cache = {}
    cache_key = (str(run_id), str(attempt or ""))
    cache = capture_artifact_success._cache
    if cache_key in cache:
        return cache[cache_key]

    proof_text = _capture_modal_volume_text(run_id, attempt or "1", "capture-success.json", timeout=6)
    if proof_text:
        try:
            proof = json.loads(proof_text)
        except Exception:
            proof = {}
        if proof.get("capture_success") is True:
            result = True, {
                "capture_artifact_conclusion": "format_complete_fixture_written",
                "capture_format_done": True,
                "capture_fixture_written": True,
                "capture_proof": "capture-success.json",
            }
            cache[cache_key] = result
            return result
        result = False, {
            "capture_artifact_conclusion": "",
            "capture_format_done": bool(proof.get("format_done")),
            "capture_fixture_written": bool(proof.get("fixture_written")),
            "capture_proof": "capture-success.json",
        }
        cache[cache_key] = result
        return result

    texts = []
    try:
        proc = subprocess.run(
            ["gh", "run", "view", str(run_id), "--repo", gh_repo, "--log"],
            text=True,
            capture_output=True,
            timeout=20,
        )
        if proc.returncode == 0:
            texts.append(proc.stdout or "")
    except Exception:
        pass
    for name in ("playwright.log", "app.log", "browser.log", "modal-audio.log", "modal-docx.log"):
        text = _capture_modal_volume_text(run_id, attempt or "1", name, timeout=6)
        if text:
            texts.append(text)

    joined = "\n".join(texts)
    format_done = CAPTURE_FORMAT_DONE_MARKER in joined
    fixture_written = any(marker in joined for marker in CAPTURE_FIXTURE_WRITE_MARKERS)
    ok = bool(format_done and fixture_written)
    detail = {
        "capture_artifact_conclusion": "format_complete_fixture_written" if ok else "",
        "capture_format_done": format_done,
        "capture_fixture_written": fixture_written,
    }
    result = ok, detail
    cache[cache_key] = result
    return result

def branch_pr_runs(pr):
    if dry_run:
        return []
    branch = pr.get("headRefName") or ""
    if not branch:
        return []
    cache_key = branch
    if not hasattr(branch_pr_runs, "_cache"):
        branch_pr_runs._cache = {}
    cache = branch_pr_runs._cache
    if cache_key in cache:
        return cache[cache_key]
    cmd = [
        "gh", "run", "list",
        "--repo", gh_repo,
        "--branch", branch,
        "--limit", "80",
        "--json", "databaseId,workflowName,event,status,conclusion,headSha,createdAt,updatedAt,url",
    ]
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=8)
        if proc.returncode != 0:
            cache[cache_key] = []
            return []
        runs = json.loads(proc.stdout or "[]")
    except Exception:
        cache[cache_key] = []
        return []
    out = [
        run for run in runs
        if run.get("event") == "pull_request"
        and run.get("workflowName") in {"CI", "E2E Smoke Tests"}
    ]
    cache[cache_key] = out
    return out

def same_head_workflow_runs(pr):
    return [
        run for run in same_head_pr_runs(pr)
        if run.get("workflowName") in {"CI", "E2E Smoke Tests"}
    ]

def same_head_pr_runs(pr):
    head = pr.get("headRefOid") or ""
    if not head:
        return []
    return [run for run in branch_pr_runs(pr) if run.get("headSha") == head]

def stale_or_superseded_bad_runs(pr):
    head = pr.get("headRefOid") or ""
    out = []
    for run in branch_pr_runs(pr):
        if run.get("headSha") == head:
            continue
        conclusion = str(run.get("conclusion") or "").lower()
        status = str(run.get("status") or "").lower()
        if status == "completed" and conclusion and conclusion not in {"success", "skipped"}:
            out.append(run)
    return out

def pr_patch_text(pr_number):
    if not hasattr(pr_patch_text, "_cache"):
        pr_patch_text._cache = {}
    cache = pr_patch_text._cache
    if pr_number in cache:
        return cache[pr_number]
    try:
        proc = subprocess.run(
            ["gh", "pr", "diff", str(pr_number), "--repo", gh_repo, "--patch"],
            text=True,
            capture_output=True,
            timeout=10,
        )
        text = proc.stdout if proc.returncode == 0 else ""
    except Exception:
        text = ""
    cache[pr_number] = text
    return text

def pr_requires_fresh_capture_before_ci(pr_number):
    if not Path(capture_required_script).is_file():
        return False
    try:
        proc = subprocess.run(
            [
                "python3",
                capture_required_script,
                "--pr",
                str(pr_number),
                "--repo",
                gh_repo,
                "--json",
            ],
            text=True,
            capture_output=True,
            timeout=25,
        )
    except Exception:
        return False
    return proc.returncode == 0

def local_preflight_proof_status(pr_number, head, *, failure_specific=False):
    candidates = [
        Path(f"/tmp/ci-local-preflight-proof-{pr_number}-{head}.ok"),
        Path(f"/tmp/ci-local-preflight-proof-{pr_number}-{head[:8]}.ok"),
    ]
    if not failure_specific:
        candidates.extend(
            [
                Path(f"/tmp/affected-test-proof-{pr_number}-{head}.ok"),
                Path(f"/tmp/affected-test-proof-{pr_number}-{head[:8]}.ok"),
            ]
        )
    prefix = "CI_LOCAL_PREFLIGHT" if failure_specific else "(?:CI_LOCAL_PREFLIGHT|AFFECTED_TESTS)"
    clean_pass_re = re.compile(
        rf"(?m)^{prefix}:\s*"
        r"(?:PASS|DOCS_ONLY)(?:\s|$)"
    )
    exception_pass_re = re.compile(
        rf"(?m)^{prefix}:\s*"
        r"(?:PASS_WITH_PREEXISTING_FAILURES|NO_LOCAL_EQUIVALENT)(?:\s|$)"
    )
    followup_re = re.compile(
        r"(?mi)^(?:flake_followup|followup_issue|follow_up_issue|preexisting_followup|"
        r"pre_existing_followup|follow-up):\s*(?:#\d+|https://github\.com/[^ \n]+/issues/\d+)\s*$"
    )
    for path in candidates:
        if not path.exists():
            continue
        if path.name.startswith("ci-local-preflight-proof-"):
            if not local_preflight_validator or not Path(local_preflight_validator).is_file():
                continue
            try:
                validated = subprocess.run(
                    [
                        "python3",
                        local_preflight_validator,
                        "validate",
                        "--pr",
                        str(pr_number),
                        "--head",
                        head,
                        "--proof",
                        str(path),
                    ],
                    text=True,
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
            except Exception:
                continue
            if validated.returncode == 0:
                return True, str(path)
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if head and f"headRefOid: {head}" not in text:
            continue
        if clean_pass_re.search(text):
            return True, str(path)
        if exception_pass_re.search(text) and followup_re.search(text):
            return True, str(path)
    return False, "missing"

def local_capture_proof_status(pr_number, head, failed_run=""):
    candidates = [
        Path(f"/tmp/capture-local-proof-{pr_number}-{head}.ok"),
        Path(f"/tmp/capture-local-proof-{pr_number}-{head[:8]}.ok"),
    ]
    pass_re = re.compile(r"(?m)^CAPTURE_LOCAL:\s*(?:PASS|PASS_NOT_REQUIRED)(?:\s|$)")
    for path in candidates:
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if head and f"headRefOid: {head}" not in text:
            continue
        if failed_run and f"failed_remote_run: {failed_run}" not in text:
            continue
        if not pass_re.search(text):
            continue
        if re.search(r"(?m)^CAPTURE_LOCAL:\s*PASS_NOT_REQUIRED(?:\s|$)", text):
            if not re.search(r"(?m)^fixture_capture_not_required_proof:\s*no_fixture_producing_request(?:\s|$)", text):
                continue
        if not (
            re.search(r"(?m)^label_gated_ci_allowed_after_local_capture:\s*true(?:\s|$)", text)
            or re.search(r"(?m)^rerun_remote_capture_allowed:\s*true(?:\s|$)", text)
        ):
            continue
        has_current_head_proof = (
            re.search(r"(?m)^current_head_verified_before_capture:\s*true(?:\s|$)", text)
            and re.search(r"(?m)^current_head_status:\s*exact_pr_head(?:\s|$)", text)
        )
        has_legacy_main_proof = (
            re.search(r"(?m)^origin_main_merged_before_capture:\s*true(?:\s|$)", text)
            and re.search(r"(?m)^main_merge_status:\s*already_contains_origin_main(?:\s|$)", text)
        )
        if not (has_current_head_proof or has_legacy_main_proof):
            continue
        if not re.search(r"(?m)^strict_replay_after_capture_required:\s*true(?:\s|$)", text):
            continue
        if not re.search(r"(?m)^full_ci_e2e_same_head_required_after_capture:\s*true(?:\s|$)", text):
            continue
        return True, str(path)
    return False, "missing"

def capture_local_action_line(
    pr_number,
    head,
    issue,
    branch,
    capture_run,
    conclusion,
    reason,
    capture_status="",
    release_slot_after_rerun="",
):
    if remote_capture_only:
        base = (
            f"capture_run={capture_run or 'unknown'} conclusion={conclusion or 'unknown'} "
            f"head={head[:10]} issue=#{issue or 'unknown'} branch={branch}"
        )
        if capture_status:
            base += f" capture_status={command_quote(capture_status)}"
        if conclusion in {"success", "remote_capture_green"} and capture_run:
            return (
                f"PR_CAPTURE_REMOTE_PASS_REQUIRED PR#{pr_number} reason={reason}_exact_head_remote_success "
                f"{base} remediation=record_the_exact_remote_capture_result_and_continue_the_existing_CI_workflow"
            )
        if "status=in_progress" in capture_status or "status=queued" in capture_status:
            return f"PR_CAPTURE_REMOTE_WATCHING PR#{pr_number} reason={reason}_remote_in_flight {base}"
        if capture_run and conclusion not in {"", "unknown", "missing"}:
            return (
                f"PR_CAPTURE_REMOTE_FAILURE_REVIEW_REQUIRED PR#{pr_number} reason={reason}_classify_before_retry "
                f"{base} command=classify_capture_failure_then_retry_only_if_infrastructure_and_budget_allows"
            )
        return (
            f"PR_CAPTURE_REMOTE_DISPATCH_REQUIRED PR#{pr_number} reason={reason}_exact_head_remote_missing "
            f"{base} remediation=dispatch_the_authoritative_remote_capture_workflow_for_this_exact_head"
        )
    proof_ok, proof = local_capture_proof_status(pr_number, head, capture_run)
    ensure_capture_label = f"gh\\ pr\\ edit\\ {pr_number}\\ --repo\\ {gh_repo}\\ --add-label\\ pm-blocked:capture"
    base = (
        f"capture_run={capture_run or 'unknown'} conclusion={conclusion or 'unknown'} "
        f"head={head[:10]} issue=#{issue or 'unknown'} branch={branch}"
    )
    if capture_status:
        base += f" capture_status={command_quote(capture_status)}"
    if proof_ok:
        failed_arg = f"\\ --failed-run\\ {capture_run}" if capture_run else ""
        release_tail = ""
        if release_slot_after_rerun:
            release_tail = "\\ &&\\ release_the_slot_with_its_complete_authoritative_tuple"
        return (
            f"PR_CAPTURE_LOCAL_PASS_CI_REQUIRED PR#{pr_number} reason={reason}_local_capture_green "
            f"{base} local_capture_proof={proof} slot_policy=release_after_ci_start "
            f"command={ensure_capture_label}\\ &&\\ consume_the_exact_local_capture_result"
            f"{failed_arg}\\ &&\\ gh\\ pr\\ edit\\ {pr_number}\\ --repo\\ {gh_repo}\\ --remove-label\\ pm-blocked:capture"
            f"\\ &&\\ /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/ci/request-label-gated-ci.sh\\ --pr\\ {pr_number}{release_tail}"
        )
    proof_target = f"/tmp/capture-local-proof-{pr_number}-{head}.ok"
    failed_arg = f"\\ --failed-run\\ {capture_run}" if capture_run else ""
    return (
        f"PR_CAPTURE_LOCAL_REQUIRED PR#{pr_number} reason={reason}_local_capture_proof_missing "
        f"{base} local_capture_proof=missing slot_policy=hold_or_assign_slot_for_local_proof "
        f"remediation=run_the_existing_local_capture_workflow_for_this_exact_head reason_detail={reason}"
    )

def latest_stale_terminal_bad_capture_run(branch, head):
    if dry_run:
        return {}
    if not branch:
        return {}
    key = (branch, head)
    if not hasattr(latest_stale_terminal_bad_capture_run, "_cache"):
        latest_stale_terminal_bad_capture_run._cache = {}
    cache = latest_stale_terminal_bad_capture_run._cache
    if key in cache:
        return cache[key]
    cmd = [
        "gh", "run", "list",
        "--repo", gh_repo,
        "--workflow", "E2E LLM Proxy Capture (manual)",
        "--branch", branch,
        "--limit", "50",
        "--json", "databaseId,workflowName,headSha,status,conclusion,createdAt,updatedAt,url",
    ]
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=10)
        if proc.returncode != 0:
            cache[key] = {}
            return {}
        runs = json.loads(proc.stdout or "[]")
    except Exception:
        runs = []
    bad = []
    for run in runs:
        run_head = str(run.get("headSha") or "")
        status = str(run.get("status") or "").lower()
        conclusion = str(run.get("conclusion") or "").lower()
        if head and run_head == head:
            continue
        if status != "completed":
            continue
        if not conclusion or conclusion in {"success", "skipped"}:
            continue
        effective = run_status(str(run.get("databaseId") or ""))
        if str(effective.get("conclusion") or "").lower() == "success":
            continue
        if effective.get("conclusion"):
            run = dict(run)
            run["conclusion"] = effective.get("conclusion")
        bad.append(run)
    if not bad:
        cache[key] = {}
        return {}
    bad.sort(
        key=lambda run: parse_time(run.get("createdAt")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    run = bad[0]
    updated = parse_time(run.get("updatedAt")) or parse_time(run.get("createdAt"))
    age_min = int((datetime.now(timezone.utc) - updated).total_seconds() // 60) if updated else 0
    out = {
        "run_id": str(run.get("databaseId") or ""),
        "head": run.get("headSha") or "",
        "status": str(run.get("status") or "").lower(),
        "conclusion": str(run.get("conclusion") or "").lower(),
        "createdAt": run.get("createdAt") or "",
        "updatedAt": run.get("updatedAt") or "",
        "age_min": age_min,
        "url": run.get("url") or "",
    }
    cache[key] = out
    return out

def stale_capture_local_action_line(pr_number, head, issue, branch, reason, capture_status, release_slot_after_rerun=""):
    if "missing_current_head_capture_verdict" not in str(capture_status or ""):
        return ""
    stale = latest_stale_terminal_bad_capture_run(branch, head)
    if not stale:
        return ""
    run_id = str(stale.get("run_id") or "")
    stale_head = str(stale.get("head") or "")
    conclusion = str(stale.get("conclusion") or "unknown")
    status = str(stale.get("status") or "unknown")
    detail = (
        f"{capture_status} stale_capture_run={run_id or 'unknown'} "
        f"stale_head={stale_head[:10] or 'unknown'} stale_status={status} "
        f"stale_conclusion={conclusion} stale_age_min={stale.get('age_min', 0)}"
    )
    return capture_local_action_line(
        pr_number,
        head,
        issue,
        branch,
        run_id,
        conclusion,
        f"{reason}_stale_capture_bad_current_head_missing",
        detail,
        release_slot_after_rerun,
    )

def capture_local_transition_line(pr_number, head, issue, branch, reason, release_slot_after_rerun=""):
    if remote_capture_only:
        capture_green, capture_detail = current_head_capture_green(pr_number, head)
        run_match = re.search(r"capture_run=([^ ]+)", capture_detail)
        conclusion_match = re.search(r"conclusion=([^ ]+)", capture_detail)
        run_id = run_match.group(1) if run_match else ""
        conclusion = "success" if capture_green else (conclusion_match.group(1) if conclusion_match else "")
        return capture_local_action_line(
            pr_number,
            head,
            issue,
            branch,
            run_id,
            conclusion,
            reason,
            capture_detail,
            release_slot_after_rerun,
        )
    proof_ok, _proof = local_capture_proof_status(pr_number, head)
    if proof_ok:
        return capture_local_action_line(
            pr_number,
            head,
            issue,
            branch,
            "",
            "local_capture_proof",
            reason,
            "local_capture_proof=present",
            release_slot_after_rerun,
        )
    capture_green, capture_detail = current_head_capture_green(pr_number, head)
    if capture_green:
        return capture_local_action_line(
            pr_number,
            head,
            issue,
            branch,
            "",
            "remote_capture_green",
            f"{reason}_remote_capture_green_not_authoritative",
            capture_detail,
            release_slot_after_rerun,
        )
    if "status=completed" in capture_detail and "conclusion=success" not in capture_detail:
        run_match = re.search(r"capture_run=([^ ]+)", capture_detail)
        conclusion_match = re.search(r"conclusion=([^ ]+)", capture_detail)
        return capture_local_action_line(
            pr_number,
            head,
            issue,
            branch,
            run_match.group(1) if run_match else "",
            conclusion_match.group(1) if conclusion_match else "unknown",
            reason,
            capture_detail,
            release_slot_after_rerun,
        )
    return stale_capture_local_action_line(
        pr_number,
        head,
        issue,
        branch,
        reason,
        capture_detail,
        release_slot_after_rerun,
    )

def ci_pressure(pr, workflow_state, ci_run, e2e_run):
    stale_bad = stale_or_superseded_bad_runs(pr)
    current_bad = 0
    current_cancelled = 0
    current_pending_stale = 0
    for bucket in (ci_run, e2e_run):
        if bucket.get("state") == "bad":
            current_bad += 1
        if bucket.get("state") == "cancelled":
            current_cancelled += 1
        if bucket.get("state") == "pending_stale":
            current_pending_stale += 1
    if current_bad:
        ci_class = "current-head-failure"
    elif current_cancelled:
        ci_class = "current-head-cancelled"
    elif current_pending_stale:
        ci_class = "current-head-stuck"
    elif stale_bad:
        ci_class = "stale-or-superseded-head"
    else:
        ci_class = workflow_state.replace("_", "-")
    return {
        "ci_class": ci_class,
        "current_head_bad": current_bad,
        "current_head_cancelled": current_cancelled,
        "current_head_stuck": current_pending_stale,
        "stale_bad": len(stale_bad),
        "stale_run": str(stale_bad[0].get("databaseId") or "") if stale_bad else "",
    }

def current_ci_circuit_breaker(pr_number, head, run_id):
    if not run_id or not str(run_id).isdigit() or not Path(ci_fast_triage_script).is_file():
        return {}
    try:
        proc = subprocess.run(
            [
                "python3",
                ci_fast_triage_script,
                "--run-id",
                str(run_id),
                "--repo",
                gh_repo,
            ],
            text=True,
            capture_output=True,
            timeout=15,
        )
        result = json.loads(proc.stdout or "{}") if proc.returncode == 0 else {}
    except Exception:
        return {}
    if result.get("degraded"):
        return {}
    if str(result.get("pr") or "") != str(pr_number):
        return {}
    if str(result.get("run_head_sha") or "") != str(head):
        return {}
    if result.get("current_for_pr") not in {True, "true"}:
        return {}
    if str(result.get("circuit_breaker") or "") != "rescue-or-split-required":
        return {}
    return result

def ci_required_action_line(pr, reason, workflow_state, ci_run, e2e_run, issue, branch):
    n = int(pr["number"])
    head = str(pr.get("headRefOid") or "")
    pressure = ci_pressure(pr, workflow_state, ci_run, e2e_run)
    bad_run = ""
    for bucket in (ci_run, e2e_run):
        if bucket.get("state") in {"bad", "cancelled"} and bucket.get("run_id") not in {"", "none", "unknown", None}:
            bad_run = str(bucket.get("run_id"))
            break
    rerun_wrapper = (
        f"/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/ci/rerun-after-local-proof.sh\\ "
        f"--pr\\ {n}\\ --run\\ {bad_run or '<failed-run>'}"
    )
    fields = (
        f"ci_class={pressure['ci_class']} current_head_bad={pressure['current_head_bad']} "
        f"current_head_cancelled={pressure['current_head_cancelled']} "
        f"current_head_stuck={pressure['current_head_stuck']} stale_bad={pressure['stale_bad']} "
        f"stale_run={pressure['stale_run'] or 'none'} workflows={workflow_bad_summary(ci_run, e2e_run)} "
        f"head={head[:10]} issue=#{issue or 'unknown'} branch={branch}"
    )
    if workflow_state == "terminal_cancelled":
        cancelled = e2e_run if e2e_run.get("state") == "cancelled" else ci_run
        return (
            f"PR_CI_CANCELLED_CLASSIFICATION_REQUIRED PR#{n} reason={reason}_current_head_cancelled_not_failed "
            f"{fields} cancelled_workflow={command_quote(cancelled.get('workflow') or 'unknown')} "
            f"cancelled_run={cancelled.get('run_id') or 'unknown'} "
            f"command=dispatch_ci-status-investigator_for_cancelled_run_then_use_canonical_typed_transition; "
            f"do_not_require_local_preflight_without_a_proven_pr_local_failure"
        )
    breaker = current_ci_circuit_breaker(n, head, bad_run)
    if breaker:
        fingerprint = breaker.get("fingerprint") if isinstance(breaker.get("fingerprint"), dict) else {}
        category = str(fingerprint.get("category") or "unknown")
        signature = str(fingerprint.get("signature") or "unknown")
        return (
            f"PR_CI_CIRCUIT_BREAKER_DIAGNOSIS_REQUIRED PR#{n} "
            f"reason={reason}_rescue_or_split_required "
            f"{fields} failed_run={bad_run} category={command_quote(category)} "
            f"signature={command_quote(signature)} "
            f"command=UNSUPPORTED_LIFECYCLE_ACTION:block-pr"
            f"\\ &&\\ dispatch_exact_head_local_diagnosis_then_require_rescue_split_or_rescope_packet_before_any_rerun"
        )
    if workflow_state == "terminal_bad" and pr_requires_fresh_capture_before_ci(n):
        if remote_capture_only:
            return capture_local_transition_line(
                n,
                head,
                issue,
                branch,
                f"{reason}_prompt_or_llm_path_requires_capture",
            )
        local_capture_ok, _local_capture_proof = local_capture_proof_status(n, head)
        if local_capture_ok:
            pass
        else:
            capture_green, capture_detail = current_head_capture_green(n, head)
            if capture_green:
                return capture_local_action_line(
                    n,
                    head,
                    issue,
                    branch,
                    "",
                    "remote_capture_green",
                    f"{reason}_prompt_or_llm_path_requires_capture_remote_capture_green_not_authoritative",
                    capture_detail,
                )
            if "status=completed" in capture_detail and "conclusion=success" not in capture_detail:
                run_match = re.search(r"capture_run=([^ ]+)", capture_detail)
                conclusion_match = re.search(r"conclusion=([^ ]+)", capture_detail)
                return capture_local_action_line(
                    n,
                    head,
                    issue,
                    branch,
                    run_match.group(1) if run_match else "",
                    conclusion_match.group(1) if conclusion_match else "unknown",
                    f"{reason}_prompt_or_llm_path_requires_capture",
                    capture_detail,
                )
            local_line = stale_capture_local_action_line(
                n,
                head,
                issue,
                branch,
                f"{reason}_prompt_or_llm_path_requires_capture",
                capture_detail,
            )
            if local_line:
                return local_line
            return (
                f"PR_CAPTURE_BEFORE_CI_REQUIRED PR#{n} reason={reason}_prompt_or_llm_path_requires_capture "
                f"{fields} capture_status={command_quote(capture_detail)} "
                f"command=UNSUPPORTED_LIFECYCLE_ACTION:block-pr"
            )
    if workflow_state == "terminal_bad" and pressure["current_head_bad"] > 0:
        verdict = latest_ci_verdict(n, head)
        if ci_verdict_requires_rework(verdict):
            classification = str(verdict.get("classification") or "unknown")
            verdict_run = str(verdict.get("run_id") or bad_run or "unknown")
            comment_url = str(verdict.get("comment_url") or "unknown")
            return (
                f"PR_CI_VERDICT_REWORK_REQUIRED PR#{n} reason={reason}_current_head_ci_verdict_requires_rework "
                f"{fields} verdict_class={classification} verdict_run={verdict_run} verdict_comment={command_quote(comment_url)} "
                f"command=UNSUPPORTED_LIFECYCLE_ACTION:block-pr"
            )
        # Sealed local-preflight envelopes are retired (Rajiv
        # 1786812200.371389). Non-rework current-head failures get AT MOST ONE
        # same-head retry through the rerun wrapper (infra/flake/shared
        # classes); the wrapper's typed refusal routes product/uncertain
        # classes to a block typed stop + Skill(direct-assign) with the exact failed-run packet.
        return (
            f"PR_CI_CAPPED_RERUN_REQUIRED PR#{n} reason={reason}_current_head_failed "
            f"{fields} verdict_class={str(verdict.get('classification') or 'unclassified') if verdict else 'unclassified'} "
            f"command={rerun_wrapper} (at most one same-head retry, no sealed proof); if the wrapper refuses with a non-infra classification, UNSUPPORTED_LIFECYCLE_ACTION:block-pr"
        )
    if workflow_state == "missing_or_skipped" and pressure["stale_bad"] > 0:
        # The live head has NO current-head runs while bad runs exist only at
        # SUPERSEDED heads: the head moved. A superseded run is never retried
        # (run_head_mismatch); the current head gets its own FRESH initial
        # exact-head CI start through the typed CI-start path, with no
        # local-preflight proof requirement (Rajiv 1786812200.371389).
        return (
            f"PR_CI_STALE_RUN_RERUN_REFUSED PR#{n} "
            f"reason=do_not_rerun_stale_run_current_head_unclassified "
            f"{fields} stale_run={pressure['stale_run'] or 'none'} "
            f"command=start_fresh_initial_ci_for_current_head_via_typed_ci_start_path_never_rerun_the_superseded_run"
        )
    return (
        f"PR_CI_CLASSIFICATION_REQUIRED PR#{n} reason={reason} "
        f"{fields} command=ci-failure-investigation_then_label_transition"
    )

def pr_files(pr_number):
    if dry_run:
        return []
    if not hasattr(pr_files, "_cache"):
        pr_files._cache = {}
    cache = pr_files._cache
    if pr_number in cache:
        return cache[pr_number]
    cmd = [
        "gh", "pr", "view", str(pr_number),
        "--repo", gh_repo,
        "--json", "files",
    ]
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=8)
        if proc.returncode != 0:
            cache[pr_number] = []
            return []
        data = json.loads(proc.stdout or "{}")
        files = [str(f.get("path") or "") for f in data.get("files") or []]
    except Exception:
        files = []
    cache[pr_number] = files
    return files

def touches_workflow(pr_number):
    return any(path.startswith(".github/workflows/") for path in pr_files(pr_number))

def rollup_blockers(pr):
    pending = []
    bad = []
    for c in pr.get("statusCheckRollup") or []:
        name = c.get("name") or c.get("workflowName") or c.get("__typename") or "check"
        conclusion = str(c.get("conclusion") or "").upper()
        status = str(c.get("status") or "").upper()
        if status and status != "COMPLETED":
            pending.append(f"{name}:status={status}")
        elif conclusion and conclusion not in {"SUCCESS", "SKIPPED"}:
            bad.append(f"{name}:{conclusion}")
    return pending, bad

def workflow_change_guard(pr):
    # A PR that changes GitHub workflow files is self-testing the workflow
    # surface. Non-required jobs/checks touched by that workflow are gates until
    # green or explicitly waived by Rajiv/CTO; do not hide them behind the
    # branch-protection "required checks" subset.
    n = int(pr.get("number") or 0)
    if not touches_workflow(n):
        return "", ""
    pending, bad = rollup_blockers(pr)
    runs = same_head_pr_runs(pr)
    non_skipped_seen = False
    for run in runs:
        workflow = str(run.get("workflowName") or "workflow")
        run_id = str(run.get("databaseId") or "unknown")
        status = str(run.get("status") or "").lower()
        conclusion = str(run.get("conclusion") or "").lower()
        if status in {"queued", "in_progress", "pending", "requested", "waiting"}:
            pending.append(f"{workflow}:run={run_id}:status={status}")
        elif conclusion and conclusion != "skipped":
            non_skipped_seen = True
            if conclusion != "success":
                bad.append(f"{workflow}:run={run_id}:conclusion={conclusion}")
    if bad:
        return "bad", ",".join(dict.fromkeys(bad))
    if pending:
        return "pending", ",".join(dict.fromkeys(pending))
    if not non_skipped_seen:
        return "missing_or_skipped", "workflow_changed_but_no_non_skipped_same_head_pull_request_run"
    return "green", "workflow_change_checks_green"

def review_loop_circuit_breaker(pr_number, head=""):
    path = Path(f"/tmp/pm-review-loop-{pr_number}.json")
    should_refresh = False
    if Path(review_budget_script).is_file():
        if not path.exists():
            should_refresh = True
        else:
            try:
                age_seconds = datetime.now(timezone.utc).timestamp() - path.stat().st_mtime
                should_refresh = age_seconds > review_loop_refresh_minutes * 60
            except Exception:
                should_refresh = True
    if Path(review_budget_script).is_file() and review_loop_refresh_enabled and should_refresh:
        cmd = ["python3", review_budget_script, "--pr", str(pr_number), "--write", "--json"]
        if head:
            cmd.extend(["--head", str(head)])
        try:
            subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=20,
            )
        except Exception:
            pass
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    decision = str(data.get("decision") or "")
    if decision == "rescue_required":
        # The budget classifier (pr-review-budget.py) is the authoritative
        # writer of the rescue decision: lifecycle cap reached AND a completed
        # blocking verdict on the live head in a capped lane. Consume it
        # directly — the local head-filtered re-derivation below is a
        # duplicate rule that resets the lifecycle cap when the head moves
        # (the classifier's events live across superseded heads) and silently
        # drops the rescue/split transition, so the tuple re-enters ordinary
        # review instead of firing the breaker (incident
        # review-cap-cb-moving-head-ci, Behavior 1; 16 missed rescue/split
        # transitions: #7202 #7161 #7132 #7128 #7127 #7126 #7107 #7106 #7103
        # #7094 #7072 #7069 #7067 #7066 #7065 #7048). No extra review round is
        # inserted before the cap fires; the classifier's per-head preflight
        # (current_head_review_required) stays the classifier's own decision.
        # Exact-head rescue authorization (ordinal-5 FUNCTIONAL_BLOCK
        # correction, admitted block receipt
        # 56dd23dc95b4e37dd7c183a2a8ee0314c52f01193928fb938242134c360ece45):
        # the persisted decision is bound to the head the classifier observed
        # (data.headRefOid). Require that binding to EQUAL the supplied live
        # head before consumption — an old-head decision must never fire the
        # breaker for a new head; the new head stays unclassified until the
        # classifier produces a current-head decision. Mirrors the
        # marker_matches_head convention below (empty head = no live head
        # supplied, e.g. the issue-level sweep, which has no head to bind).
        decision_matches_head = not head or str(data.get("headRefOid") or "") == str(head)
        if not decision_matches_head:
            return ""
        cap_reasons = [str(item) for item in (data.get("cap_reasons") or [])]
        if not cap_reasons:
            return ""
        label, _, count_text = cap_reasons[0].partition(":")
        count = int(count_text) if count_text.isdigit() else 1
        total = int(data.get("blocking_event_count") or 0)
        return (
            f"class={label or 'review_cap'} count={count} total={total} "
            f"cap_reasons={','.join(cap_reasons)} proof={path}"
        )
    if decision and decision != "rescue_required":
        return ""
    events = data.get("events") or []
    if head:
        events = [event for event in events if str(event.get("headRefOid") or "") == str(head)]
    blocking_events = [event for event in events if event.get("blocking") is not False]
    counts = {}
    round_counts = {}
    for event in blocking_events:
        klass = str(event.get("class") or "").strip()
        if klass:
            counts[klass] = counts.get(klass, 0) + 1
        round_key = (str(event.get("layer") or ""), str(event.get("review_type") or ""))
        round_counts[round_key] = round_counts.get(round_key, 0) + 1
    hot = [(klass, count) for klass, count in counts.items() if count >= 2]
    hard_cap = any(count >= 3 for count in round_counts.values())
    total = len(blocking_events)
    if hot:
        hot.sort(key=lambda item: (-item[1], item[0]))
        klass, count = hot[0]
    else:
        cap_reasons = [str(item) for item in (data.get("cap_reasons") or [])]
        marker_matches_head = not head or str(data.get("headRefOid") or "") == str(head)
        if "explicit_cap_marker" not in cap_reasons or not marker_matches_head or not hard_cap:
            return ""
        klass, count = "explicit_cap_marker", 1
    return f"class={klass} count={count} total={total} proof={path}"

def scope_risk_circuit_breaker(pr_number):
    if not scope_risk_refresh_enabled:
        return ""
    if not Path(scope_risk_script).is_file():
        return ""
    try:
        proc = subprocess.run(
            ["python3", scope_risk_script, "--pr", str(pr_number), "--json"],
            text=True,
            capture_output=True,
            timeout=25,
        )
    except Exception:
        return ""
    try:
        data = json.loads(proc.stdout or "{}")
    except Exception:
        return ""
    if str(data.get("status") or "") != "split_or_rescope_required":
        return ""
    reasons = ",".join(str(item) for item in (data.get("reasons") or []) if item)
    surfaces = ",".join(str(item) for item in (data.get("surfaces") or []) if item)
    changed = str(data.get("changed_files") or "0")
    churn = str(data.get("churn") or "0")
    return (
        f"class=scope_risk count=1 total=1 proof={scope_risk_script} "
        f"changed_files={changed} churn={churn} surfaces={surfaces or 'none'} "
        f"reasons={reasons or 'broad_scope'}"
    )

def rescope_marker(pr_number, head):
    path = Path(f"/tmp/pm-rescope-pr-{pr_number}.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    status = str(data.get("status") or "pending")
    marker_head = str(data.get("headRefOid") or "")
    ledger = str(data.get("ledger") or "")
    rounds = str(data.get("round_count") or "0")
    artifacts = str(data.get("source_artifact_count") or "0")
    decision = str(data.get("terminal_decision") or "")
    proof = str(data.get("proof") or "")
    stale = "true" if head and marker_head and marker_head != head else "false"
    return f"marker={path} status={status} decision={decision or 'none'} marker_head={marker_head[:10] or 'unknown'} stale={stale} ledger={ledger or 'missing'} rounds={rounds} artifacts={artifacts} proof={proof or 'missing'}"

def issue_rescope_marker(issue_number):
    path = Path(f"/tmp/pm-rescope-issue-{issue_number}.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    status = str(data.get("status") or "pending")
    ledger = str(data.get("ledger") or "")
    rounds = str(data.get("round_count") or "0")
    artifacts = str(data.get("source_artifact_count") or "0")
    decision = str(data.get("terminal_decision") or "")
    return f"marker={path} status={status} decision={decision or 'none'} ledger={ledger or 'missing'} rounds={rounds} artifacts={artifacts}"

def fetch_open_issues_with_label(label):
    cmd = [
        "gh", "issue", "list",
        "--repo", gh_repo,
        "--state", "open",
        "--limit", "100",
        "--label", label,
        "--json", "number,title,labels,updatedAt,url,state",
    ]
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=10)
        if proc.returncode != 0:
            return []
        return json.loads(proc.stdout or "[]")
    except Exception:
        return []

def fetch_open_issue(issue_number):
    cmd = [
        "gh", "issue", "view",
        str(issue_number),
        "--repo", gh_repo,
        "--json", "number,title,labels,updatedAt,url,state",
    ]
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=8)
        if proc.returncode != 0:
            return {}
        return json.loads(proc.stdout or "{}")
    except Exception:
        return {}

def issue_labels(issue):
    return [x.get("name", "") for x in issue.get("labels") or []]

def issue_has_label(issue, name):
    return name in issue_labels(issue)

def issue_slot_labels(issue):
    out = []
    for label in issue_labels(issue):
        m = re.fullmatch(r"slot:([1-4])", label)
        if m:
            out.append(int(m.group(1)))
    return out

def merge_ready_proof_status(pr_number, head):
    path = Path(f"/tmp/pm-state-promotion-proof-{pr_number}-merge-ready.ok")
    if not path.exists():
        return "missing", f"proof={path}"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return "unreadable", f"proof={path}"
    if not re.search(r"^READY_PACKET:\s*PASS(?:\s|$)", text, re.M):
        return "invalid", f"proof={path}:missing_READY_PACKET_PASS"
    # Exact-tuple authorization (reviewer BLOCK exact_tuple_authorization,
    # ordinal-2): the packet's PR identity must equal the sweep's PR. A
    # missing or different PR field fails the readiness gate and never
    # promotes — the merge_ready_review obligation stays the typed path.
    if not re.search(r"^PR:\s*\d+(?:\s|$)", text, re.M):
        return "invalid", f"proof={path}:missing_PR"
    if not re.search(rf"^PR:\s*{pr_number}(?:\s|$)", text, re.M):
        return "invalid", f"proof={path}:PR_mismatch"
    if head and not re.search(rf"^headRefOid:\s*{re.escape(head)}\s*$", text, re.M):
        return "stale", f"proof={path}:head_mismatch"
    if not re.search(r"^review_provenance:\s*ok(?:\s|$)", text, re.M):
        return "invalid", f"proof={path}:missing_review_provenance_ok"
    return "ok", f"proof={path}"

def rollup_workflow_bucket(pr, workflow_name):
    required_job = str(required_ci_jobs.get(workflow_name) or "")
    if not required_job:
        return {
            "workflow": workflow_name,
            "state": "unknown",
            "run_id": "none",
            "age_min": 0,
            "url": "",
            "detail": "required_job_contract_missing",
        }
    matches = []
    for check in pr.get("statusCheckRollup") or []:
        name = str(check.get("workflowName") or check.get("name") or "")
        check_name = str(check.get("name") or "")
        if (
            (name == workflow_name or name.startswith(f"{workflow_name} /"))
            and (not required_job or check_name == required_job)
        ):
            matches.append(check)
    if not matches:
        return None
    rows = []
    for check in matches:
        # statusCheckRollup job nodes do not carry Actions run ids; parse from
        # detailsUrl (.../actions/runs/<id>/job/...) so ci-watch can match
        # failed_run against dry-run buckets (run_id was permanently "rollup").
        details = str(check.get("detailsUrl") or check.get("details_url") or "")
        m = re.search(r"/actions/runs/(\d+)", details)
        run_id = str(
            check.get("databaseId")
            or (check.get("checkSuite") or {}).get("databaseId")
            or (m.group(1) if m else "")
            or "rollup"
        )
        status = str(check.get("status") or "").lower()
        conclusion = str(check.get("conclusion") or "").lower()
        row = {
            "workflow": workflow_name,
            "run_id": run_id,
            "url": check.get("detailsUrl") or "",
            "status": status,
            "conclusion": conclusion,
            "age_min": 0,
        }
        rows.append(row)

    # A head can have several workflow attempts in statusCheckRollup. A newer
    # concurrency-cancel is informational only when the canonical exact-run,
    # exact-head verdict closes it. Remove only those admitted cancellations
    # before selecting the newest substantive run so an older executed green
    # remains usable, matching the pre-merge guard's fail-closed semantics.
    rows = [
        row for row in rows
        if not (
            row["status"] == "completed"
            and row["conclusion"] == "cancelled"
            and has_closed_concurrency_cancel(
                pr.get("number"), pr.get("headRefOid"), row["run_id"]
            )
        )
    ]

    # Classify only the newest remaining Actions run; otherwise an old failure
    # permanently masks a later successful rerun on the same head.
    if rows and all(row["run_id"].isdigit() for row in rows):
        substantive_run_ids = {
            row["run_id"]
            for row in rows
            if row["status"] != "completed"
            or row["conclusion"] not in {"", "skipped"}
        }
        candidate_run_ids = substantive_run_ids or {row["run_id"] for row in rows}
        latest_run_id = max(candidate_run_ids, key=int)
        rows = [row for row in rows if row["run_id"] == latest_run_id]

    pending = []
    bad = []
    success = []
    skipped = []
    for row in rows:
        status = row["status"]
        conclusion = row["conclusion"]
        if status and status != "completed":
            row["state"] = "pending"
            pending.append(row)
        elif conclusion == "success":
            row["state"] = "success"
            success.append(row)
        elif conclusion == "skipped":
            row["state"] = "skipped"
            skipped.append(row)
        elif conclusion:
            row["state"] = "bad"
            bad.append(row)
        else:
            row["state"] = "unknown"
            bad.append(row)
    if pending:
        return pending[0]
    if bad:
        return bad[0]
    if success:
        return success[0]
    if skipped:
        return skipped[0]
    return {
        "workflow": workflow_name,
        "state": "missing",
        "run_id": "none",
        "age_min": 0,
        "url": "",
    }

def workflow_bucket(pr, workflow_name):
    if dry_run:
        bucket = rollup_workflow_bucket(pr, workflow_name)
        if bucket is not None:
            return bucket
    runs = [r for r in same_head_workflow_runs(pr) if r.get("workflowName") == workflow_name]
    if not runs:
        return {
            "workflow": workflow_name,
            "state": "missing",
            "run_id": "none",
            "age_min": 0,
            "url": "",
        }

    def sort_key(run):
        return parse_time(run.get("createdAt")) or datetime.min.replace(tzinfo=timezone.utc)

    required_job = str(required_ci_jobs.get(workflow_name) or "")
    if not required_job:
        return {
            "workflow": workflow_name,
            "state": "unknown",
            "run_id": "none",
            "age_min": 0,
            "url": "",
            "detail": "required_job_contract_missing",
        }
    for run in sorted(runs, key=sort_key, reverse=True):
        run_id = str(run.get("databaseId") or "")
        required_job_state = ""
        if run_id and run_id.isdigit():
            try:
                proc = subprocess.run(
                    [
                        "gh", "api",
                        f"repos/{gh_repo}/actions/runs/{run_id}/jobs?per_page=100",
                        "--jq", ".jobs",
                    ],
                    text=True,
                    capture_output=True,
                    timeout=8,
                )
                jobs = json.loads(proc.stdout or "[]") if proc.returncode == 0 else []
            except Exception:
                jobs = []
            required = next(
                (job for job in jobs if str(job.get("name") or "") == required_job),
                None,
            )
            if required:
                job_status = str(required.get("status") or "").lower()
                job_conclusion = str(required.get("conclusion") or "").lower()
                if job_status == "completed" and job_conclusion == "success":
                    required_job_state = "success"
                elif job_status != "completed":
                    required_job_state = "pending"
                elif job_conclusion in {"skipped", "neutral"}:
                    required_job_state = "ignored"
                elif job_conclusion == "cancelled":
                    required_job_state = "cancelled"
                else:
                    required_job_state = "bad"
        if required_job_state == "success":
            state = "success"
        elif required_job_state == "pending":
            state = "pending"
        elif required_job_state == "ignored":
            continue
        elif required_job_state == "cancelled":
            if has_closed_concurrency_cancel(
                pr.get("number"), pr.get("headRefOid"), run_id
            ):
                continue
            state = "cancelled"
        elif required_job_state == "bad":
            state = "bad"
        else:
            continue
        updated = parse_time(run.get("updatedAt")) or parse_time(run.get("createdAt"))
        age_min = int((datetime.now(timezone.utc) - updated).total_seconds() // 60) if updated else 0
        if state == "pending" and age_min >= stale_ci_minutes:
            state = "pending_stale"
        return {
            "workflow": workflow_name,
            "state": state,
            "run_id": str(run.get("databaseId") or "unknown"),
            "age_min": age_min,
            "url": run.get("url") or "",
            "status": run.get("status") or "",
            "conclusion": run.get("conclusion") or "",
        }

    return {
        "workflow": workflow_name,
        "state": "missing",
        "run_id": "none",
        "age_min": 0,
        "url": "",
        "status": "",
        "conclusion": "",
    }

def workflow_summary(pr):
    ci = workflow_bucket(pr, "CI")
    e2e = workflow_bucket(pr, "E2E Smoke Tests")
    buckets = [ci, e2e]
    if any(b["state"] == "pending_stale" for b in buckets):
        return "pending_stale", ci, e2e
    if any(b["state"] == "pending" for b in buckets):
        return "pending", ci, e2e
    if any(b["state"] == "bad" for b in buckets):
        return "terminal_bad", ci, e2e
    if any(b["state"] == "cancelled" for b in buckets):
        return "terminal_cancelled", ci, e2e
    if all(b["state"] == "success" for b in buckets):
        return "terminal_green", ci, e2e
    if any(b["state"] in {"missing", "skipped", "unknown"} for b in buckets):
        return "missing_or_skipped", ci, e2e
    return "unknown", ci, e2e

def workflow_bad_summary(ci, e2e):
    parts = []
    for bucket in (ci, e2e):
        parts.append(
            f"{bucket['workflow']}:{bucket['state']}:run={bucket.get('run_id')}:"
            f"status={bucket.get('status','')}:conclusion={bucket.get('conclusion','')}:age_min={bucket.get('age_min',0)}"
        )
    return ",".join(parts)

def pr_comments(pr_number):
    if not hasattr(pr_comments, "_cache"):
        pr_comments._cache = {}
    cache = pr_comments._cache
    if pr_number in cache:
        return cache[pr_number]
    cmd = [
        "gh", "pr", "view", str(pr_number),
        "--repo", gh_repo,
        "--json", "comments",
    ]
    try:
        proc = read_only_subprocess_run(cmd, text=True, capture_output=True, timeout=8)
        if proc.returncode != 0:
            cache[pr_number] = []
            return []
        data = json.loads(proc.stdout or "{}")
        comments = data.get("comments") or []
    except Exception:
        comments = []
    cache[pr_number] = comments
    return comments

def extract_ci_verdict(text):
    marker = "<!-- ci-verdict:"
    start = text.find(marker)
    if start < 0:
        return None
    end = text.find("-->", start)
    if end < 0:
        return None
    raw = text[start + len(marker):end].strip()
    try:
        return json.loads(raw)
    except Exception:
        return None

def verdict_head(verdict):
    return str(
        verdict.get("current_pr_head_sha")
        or verdict.get("current_for_pr_head_sha")
        or verdict.get("run_head_sha")
        or verdict.get("head_sha")
        or verdict.get("sha")
        or verdict.get("head")
        or ""
    )

def has_closed_concurrency_cancel(pr_number, head, run_id):
    if not pr_number or not head or not run_id:
        return False
    for comment in pr_comments(pr_number):
        verdict = extract_ci_verdict(comment.get("body") or "")
        if not verdict:
            continue
        blocking = verdict.get("blocking_for_merge")
        if isinstance(blocking, str):
            blocking = blocking.lower() == "true"
        if (
            str(verdict.get("run_id") or "") == str(run_id)
            and verdict_head(verdict) == str(head)
            and str(verdict.get("classification") or "").lower() == "concurrency-cancel"
            and blocking is False
            and str(verdict.get("local_repro_result") or "").lower() == "not-applicable"
            and str(verdict.get("pm_action_status") or "").lower()
            in {"executed", "not-required"}
        ):
            return True
    return False

def latest_ci_verdict(pr_number, head):
    comments = sorted(
        pr_comments(pr_number),
        key=lambda c: parse_time(c.get("createdAt")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    for comment in comments:
        verdict = extract_ci_verdict(comment.get("body") or "")
        if not verdict:
            continue
        verdict_pr = str(verdict.get("pr") or "")
        if verdict_pr and verdict_pr != str(pr_number):
            continue
        v_head = verdict_head(verdict)
        if head and v_head and v_head != str(head):
            continue
        if head and not v_head:
            continue
        verdict["comment_created_at"] = comment.get("createdAt") or ""
        verdict["comment_url"] = comment.get("url") or ""
        return verdict
    return None

def ci_verdict_requires_rework(verdict):
    if not verdict:
        return False
    classification = str(verdict.get("classification") or "").lower()
    if classification in {
        "generated-metadata-key-drift",
        "prompt-induced-output-regression",
        "real-regression",
        "pr-introduced-regression",
    }:
        return True
    recapture_fixes = verdict.get("recapture_fixes_it")
    if isinstance(recapture_fixes, bool) and not recapture_fixes:
        return True
    if isinstance(recapture_fixes, str) and recapture_fixes.strip().lower() in {"false", "no", "n"}:
        return True
    rerun_permitted = verdict.get("rerun_permitted")
    if isinstance(rerun_permitted, bool) and not rerun_permitted:
        return True
    if isinstance(rerun_permitted, str) and rerun_permitted.strip().lower() in {"false", "no", "n"}:
        return True
    text = " ".join(
        str(verdict.get(key) or "")
        for key in ("recommended_next_action", "action", "terminal_state", "stage_detail")
    ).lower()
    return "recapture won't fix" in text or "no rerun" in text or "slot rework" in text

def latest_capture_verdict(pr_number, head):
    comments = sorted(
        pr_comments(pr_number),
        key=lambda c: parse_time(c.get("createdAt")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    capture_classes = {
        "capture-required",
        "e2e-cache-miss",
        "fixture-stale-or-invalid",
        "fixture-observability-missing",
    }
    for comment in comments:
        verdict = extract_ci_verdict(comment.get("body") or "")
        if not verdict:
            continue
        classification = str(verdict.get("classification") or "")
        if classification not in capture_classes:
            continue
        v_head = verdict_head(verdict)
        if head and v_head and v_head != head:
            continue
        verdict["comment_created_at"] = comment.get("createdAt") or ""
        return verdict
    return None

def run_status(run_id):
    if not run_id:
        return {"status": "missing", "conclusion": "", "age_min": 0, "url": ""}
    if not hasattr(run_status, "_cache"):
        run_status._cache = {}
    cache = run_status._cache
    run_id = str(run_id)
    if run_id in cache:
        return cache[run_id]
    cmd = [
        "gh", "run", "view", run_id,
        "--repo", gh_repo,
        "--json", "databaseId,workflowName,headBranch,headSha,status,conclusion,createdAt,updatedAt,url,jobs,attempt",
    ]
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=8)
        if proc.returncode != 0:
            out = {"status": "missing", "conclusion": "", "age_min": 0, "url": ""}
            cache[run_id] = out
            return out
        data = json.loads(proc.stdout or "{}")
    except Exception:
        data = {}
    updated = parse_time(data.get("updatedAt")) or parse_time(data.get("createdAt"))
    age_min = int((datetime.now(timezone.utc) - updated).total_seconds() // 60) if updated else 0
    out = {
        "run_id": run_id,
        "workflow": data.get("workflowName") or "",
        "branch": data.get("headBranch") or "",
        "head": data.get("headSha") or "",
        "status": str(data.get("status") or "").lower(),
        "conclusion": str(data.get("conclusion") or "").lower(),
        "createdAt": data.get("createdAt") or "",
        "updatedAt": data.get("updatedAt") or "",
        "age_min": age_min,
        "url": data.get("url") or "",
        "attempt": str(data.get("attempt") or "1"),
    }
    # E2E capture success is defined by the artifact contract, not the whole
    # workflow conclusion. Downstream assertions/cleanup can fail after the
    # format path wrote fresh fixtures.
    if (
        out["workflow"] == "E2E LLM Proxy Capture (manual)"
        and out["status"] == "completed"
        and out["conclusion"] != "success"
    ):
        artifact_ok, artifact_detail = capture_artifact_success(run_id, out.get("attempt") or data.get("attempt") or "1")
        if artifact_ok:
            out["conclusion"] = "success"
            out.update(artifact_detail)
        elif artifact_detail.get("capture_proof") == "capture-success.json":
            out.update(artifact_detail)
        else:
            for job in data.get("jobs") or []:
                if job.get("name") == "e2e-capture" and job.get("conclusion") == "success":
                    out["conclusion"] = "success"
                    out["capture_job_conclusion"] = "success"
                    break
    cache[run_id] = out
    return out

def latest_watch_run_id(watch_runs):
    best = ""
    best_time = None
    for run_id in watch_runs:
        status = run_status(run_id)
        created = parse_time(status.get("createdAt"))
        if not created:
            continue
        if best_time is None or created > best_time:
            best_time = created
            best = str(run_id)
    return best or (str(watch_runs[0]) if watch_runs else "")

def latest_live_capture_run(pr_number, head):
    if not head:
        return {}
    if not hasattr(latest_live_capture_run, "_cache"):
        latest_live_capture_run._cache = {}
    cache = latest_live_capture_run._cache
    if head in cache:
        return cache[head]
    cmd = [
        "gh", "run", "list",
        "--repo", gh_repo,
        "--workflow", "E2E LLM Proxy Capture (manual)",
        "--limit", "50",
        "--json", "databaseId,workflowName,displayTitle,headSha,status,conclusion,createdAt,updatedAt,url",
    ]
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=10)
        if proc.returncode != 0:
            cache[head] = {}
            return {}
        runs = json.loads(proc.stdout or "[]")
    except Exception:
        runs = []
    expected_title = f"remote-capture-pr-{pr_number}-head-{head}"
    matches = [
        run for run in runs
        if (
            str(run.get("displayTitle") or "") == expected_title
            if remote_capture_only
            else str(run.get("headSha") or "") == head
        )
    ]
    if not matches:
        cache[head] = {}
        return {}
    matches.sort(
        key=lambda run: parse_time(run.get("createdAt")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    run = matches[0]
    run_id = str(run.get("databaseId") or "")
    effective = run_status(run_id)
    updated = parse_time(effective.get("updatedAt") or run.get("updatedAt")) or parse_time(run.get("createdAt"))
    age_min = int((datetime.now(timezone.utc) - updated).total_seconds() // 60) if updated else 0
    out = {
        "run_id": run_id,
        "workflow": effective.get("workflow") or run.get("workflowName") or "E2E LLM Proxy Capture (manual)",
        "head": effective.get("head") or run.get("headSha") or "",
        "status": effective.get("status") or str(run.get("status") or "").lower(),
        "conclusion": effective.get("conclusion") or str(run.get("conclusion") or "").lower(),
        "createdAt": effective.get("createdAt") or run.get("createdAt") or "",
        "updatedAt": effective.get("updatedAt") or run.get("updatedAt") or "",
        "age_min": age_min,
        "url": effective.get("url") or run.get("url") or "",
        "capture_job_conclusion": effective.get("capture_job_conclusion") or "",
        "capture_artifact_conclusion": effective.get("capture_artifact_conclusion") or "",
        "capture_format_done": effective.get("capture_format_done") or False,
        "capture_fixture_written": effective.get("capture_fixture_written") or False,
    }
    cache[head] = out
    return out

def recently_updated(pr, hours=96):
    updated = pr.get("updatedAt")
    if not updated:
        return False
    try:
        dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() <= hours * 3600
    except Exception:
        return False

def pr_age_hours(pr):
    created = parse_time(pr.get("createdAt")) or parse_time(pr.get("updatedAt"))
    if not created:
        return None
    return int((datetime.now(timezone.utc) - created).total_seconds() // 3600)

main_behind_cache = {}

def commits_behind_main(pr):
    """Return commits the PR head lacks from main, or None when GitHub cannot prove it."""
    head = str(pr.get("headRefOid") or "")
    if not head:
        return None
    if head in main_behind_cache:
        return main_behind_cache[head]
    try:
        proc = read_only_subprocess_run(
            [
                "gh", "api", f"repos/{gh_repo}/compare/main...{head}",
                "--jq", ".behind_by",
            ],
            text=True,
            capture_output=True,
            timeout=8,
        )
        value = int((proc.stdout or "").strip()) if proc.returncode == 0 else None
    except Exception:
        value = None
    main_behind_cache[head] = value
    return value

def file_recent(path, hours=12):
    try:
        age_s = datetime.now(timezone.utc).timestamp() - path.stat().st_mtime
        return age_s <= hours * 3600
    except Exception:
        return False

def text_names_head(text, head):
    if not head:
        return False
    variants = {head}
    if len(head) >= 10:
        variants.add(head[:10])
    if len(head) >= 8:
        variants.add(head[:8])
    return any(v and v in text for v in variants)


def age_minutes_from_iso(value):
    dt = parse_time(value)
    if not dt:
        return None
    return int((datetime.now(timezone.utc) - dt).total_seconds() // 60)

def slot_activity_time(entry):
    if not entry:
        return None
    for key in ("last_activity", "lastActivity", "updated_at", "updatedAt"):
        dt = parse_time(entry.get(key))
        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
    return None

def rework_packet_candidates(pr_number, slot):
    patterns = [
        f"/tmp/slot{slot}-{pr_number}-*.md",
        f"/tmp/slot-{slot}-{pr_number}-*.md",
        f"/tmp/slot-rework-{pr_number}-*.md",
    ]
    paths = []
    for pattern in patterns:
        paths.extend(Path("/").glob(pattern.lstrip("/")))
    uniq = []
    seen = set()
    for path in paths:
        try:
            resolved = str(path)
            if resolved in seen or not path.is_file() or not file_recent(path, 24):
                continue
            seen.add(resolved)
            uniq.append(path)
        except Exception:
            continue
    return uniq

def durable_rework_packet_record(pr_number, head):
    if not hasattr(durable_rework_packet_record, "_cache"):
        durable_rework_packet_record._cache = {}
    cache = durable_rework_packet_record._cache
    key = (int(pr_number), str(head or ""))
    if key in cache:
        return cache[key]
    if not head or not rework_packet_ledger.is_file():
        cache[key] = None
        return None
    output = Path(f"/tmp/slot-rework-{pr_number}-pr-comment-{head[:12]}.md")
    try:
        proc = read_only_subprocess_run(
            [
                sys.executable,
                str(rework_packet_ledger),
                "fetch",
                "--repo",
                gh_repo,
                "--pr",
                str(pr_number),
                "--head",
                str(head),
                "--output",
                str(output),
            ],
            text=True,
            capture_output=True,
            timeout=12,
        )
    except Exception:
        cache[key] = None
        return None
    if proc.returncode != 0 or not output.is_file() or not output.stat().st_size:
        cache[key] = None
        return None
    try:
        metadata = json.loads(proc.stdout or "{}")
    except Exception:
        metadata = {}
    if str(metadata.get("head") or "") != str(head):
        cache[key] = None
        return None
    record = {**metadata, "path": output}
    cache[key] = record
    return record

def durable_rework_packet(pr_number, head):
    record = durable_rework_packet_record(pr_number, head)
    return record.get("path") if record else None

def durable_packet_supersedes_review_loop(pr_number, head):
    record = durable_rework_packet_record(pr_number, head)
    if not record:
        return False
    packet_time = parse_time(record.get("created_at"))
    if packet_time is None:
        return False
    path = Path(f"/tmp/pm-review-loop-{pr_number}.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    blocking_times = []
    for event in data.get("events") or []:
        if str(event.get("headRefOid") or "") != str(head):
            continue
        if event.get("blocking") is False:
            continue
        try:
            blocking_times.append(datetime.fromtimestamp(float(event.get("mtime")), timezone.utc))
        except Exception:
            event_time = parse_time(event.get("created_at"))
            if event_time is not None:
                blocking_times.append(event_time)
    return not blocking_times or packet_time > max(blocking_times)

def rework_delivery_pending(pr_number, slot, entry, head=""):
    durable = durable_rework_packet(pr_number, head)
    packets = rework_packet_candidates(pr_number, slot)
    if durable and durable not in packets:
        packets.append(durable)
    if not packets:
        return None
    latest = max(packets, key=lambda path: path.stat().st_mtime)
    latest_mtime = latest.stat().st_mtime

    ack = delivery_ack_dir / f"pr-{pr_number}-slot-{slot}.ack"
    try:
        ack_text = ack.read_text(encoding="utf-8", errors="replace")
        if "MESSAGE_SLOT_OK" in ack_text:
            fields = {}
            for line in ack_text.splitlines():
                key, sep, value = line.partition("=")
                if sep:
                    fields[key.strip()] = value.strip()
            try:
                latest_sha256 = hashlib.sha256(latest.read_bytes()).hexdigest()
            except Exception:
                latest_sha256 = ""
            acknowledged_digests = {
                fields.get("packet_sha256", ""),
                fields.get("source_packet_sha256", ""),
            }
            if latest_sha256 and latest_sha256 in acknowledged_digests:
                return None
            # Preserve compatibility with pre-identity acknowledgements. They
            # remain valid only when the ack itself is at least as new as the
            # newest candidate packet.
            if not any(acknowledged_digests) and ack.stat().st_mtime >= latest_mtime - 1:
                return None
    except Exception:
        pass

    # If the slot has already produced activity after the packet was written,
    # it has picked up the message even if a legacy/manual delivery path did
    # not leave the new ack file.
    activity = slot_activity_time(entry)
    if activity and activity.timestamp() >= latest_mtime - 1:
        return None

    age_min = int((datetime.now(timezone.utc).timestamp() - latest_mtime) // 60)
    return {"packet": str(latest), "age_min": age_min}

def pm_review_meta(pr_number):
    path = Path(f"/tmp/pm-review-pending-{pr_number}.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"path": str(path), "missing": True}
    data["path"] = str(path)
    return data

# PM_REVIEW_DONE_TERMINAL_RECEIPT_V1 consumer: read the head-bound terminal
# head-bound terminal review-PASS receipt (retired pm-review-done class) for the exact head. When
# present, the review is terminal at that head: the sweep must NEVER schedule
# another same-head review. The receipt carries handoff_status and, on a
# downstream refusal, one typed blocked_after_review class, next owner, and
# wake condition that route the next action.
def pm_review_done_terminal_receipt(pr_number, head):
    if not head:
        return None
    path = Path(f"/tmp/pm-review-done-receipt-{pr_number}-{head}.json")
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if str(data.get("head_sha") or "") != str(head):
        return None
    if str(data.get("verdict") or "") != "PASS":
        return None
    data["_receipt_path"] = str(path)
    return data

def pm_review_done_receipt_line(pr_number, head, issue, branch, prefix):
    """Emit the typed receipt routing for a PR whose review is terminal at the
    exact head: a blocked_after_review receipt surfaces the typed
    class/next_owner/wake; a pending/handed_off receipt surfaces the idempotent
    resume command. Returns True when a line was emitted (caller continues)."""
    receipt = pm_review_done_terminal_receipt(pr_number, head)
    if receipt is None:
        return False
    blocked = receipt.get("blocked_after_review") or {}
    handoff = str(receipt.get("handoff_status") or "pending")
    if blocked.get("class"):
        print(
            f"PR_PM_REVIEW_TERMINAL_BLOCKED_REQUIRED PR#{pr_number} reason={prefix}_review_terminal_blocked "
            f"head={head} blocked_after_review={blocked.get('class')} "
            f"next_owner={blocked.get('next_owner') or 'PM'} wake={blocked.get('wake') or 'unknown'} "
            f"receipt={receipt.get('_receipt_path')} issue=#{issue or 'unknown'} branch={branch} "
            f"command=execute_wake_then_resume_pm-review-done"
        )
    else:
        print(
            f"PR_PM_REVIEW_TERMINAL_RECEIPT_REQUIRED PR#{pr_number} reason={prefix}_review_terminal_receipt "
            f"handoff={handoff} head={head} receipt={receipt.get('_receipt_path')} "
            f"issue=#{issue or 'unknown'} branch={branch} "
            f"command=UNSUPPORTED_LIFECYCLE_ACTION:pm-review-done"
        )
    return True

def pm_review_scope(pr, workflow_state, merge_state, draft):
    n = int(pr.get("number") or 0)
    head = str(pr.get("headRefOid") or "")
    meta = pm_review_meta(n)
    scope = str(meta.get("scope") or "")
    if scope == "phase-a" and str(meta.get("headRefOid") or "") == head:
        return scope, meta
    return "phase-a", meta

def pm_review_pending_is_stale(meta, head=""):
    if meta.get("missing"):
        return True
    meta_head = str(meta.get("headRefOid") or "")
    if head and meta_head and meta_head != head:
        return True
    age = age_minutes_from_iso(meta.get("created_at"))
    if age is None:
        return True
    return age >= pm_review_stale_minutes

def current_pm_review_contract(pr_number, head):
    meta = pm_review_meta(pr_number)
    if meta.get("missing"):
        return None, ""
    if head and str(meta.get("headRefOid") or "") != head:
        return None, ""
    return meta, str(meta.get("scope") or "phase-a")

def opus_marker_status(pr_number, head, scope):
    marker = Path(f"/tmp/pm-claude-code-review-{pr_number}-{head}.md")
    legacy_marker = Path(f"/tmp/pm-opus-code-review-{pr_number}-{head}.md")
    marker_kind = "claude"
    if not marker.exists():
        marker = legacy_marker
        marker_kind = "legacy-opus"
    if not marker.exists():
        return "missing", str(Path(f"/tmp/pm-claude-code-review-{pr_number}-{head}.md"))
    if not file_recent(marker, 24):
        return "stale", str(marker)
    try:
        text = marker.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return "unreadable", str(marker)
    if re.search(r"^PM_CLAUDE_REVIEW:\s*BLOCKED", text, re.M) or re.search(r"^PM_OPUS_REVIEW:\s*BLOCKED", text, re.M):
        return "blocked", str(marker)
    required = []
    if marker_kind == "claude":
        required.extend([
            r"^PM_CLAUDE_REVIEW:\s*PASS",
            r"^review_model:\s*(sonnet|opus|fable|kimi3)(?:\s|$)",
            r"^model_reason:\s*\S.+",
        ])
    else:
        required.append(r"^PM_OPUS_REVIEW:\s*PASS")
    required.extend([
        rf"^headRefOid:\s*{re.escape(head)}\s*$",
        r"^runtime_control_point:\s*\S.+",
        r"^pass_scope:\s*\S+",
    ])
    for pattern in required:
        if not re.search(pattern, text, re.M):
            return "invalid", str(marker)
    if scope == "phase-a":
        if not re.search(r"^pass_scope:\s*(phase-a|blocker-clear)(?:\s|$)", text, re.M):
            return "wrong_scope", str(marker)
        return "pass", str(marker)
    if scope == "merge-ready":
        merge_ready_required = [
            r"^pass_scope:\s*merge-ready(?:\s|$)",
            r"^branch_freshness:\s*\S.+",
            r"^unresolved_review_threads:\s*\S.+",
            r"^product_ac_proof:\s*\S.+",
        ]
        for pattern in merge_ready_required:
            if not re.search(pattern, text, re.M):
                return "invalid_merge_ready_scope", str(marker)
        return "pass", str(marker)
    return "invalid_scope", str(marker)

def marker_field(path, name):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    m = re.search(rf"^{re.escape(name)}:\s*(.*)$", text, re.M)
    return m.group(1).strip() if m else ""

def pm_review_marker_capture_gated(marker_path):
    ceiling = marker_field(marker_path, "readiness_ceiling")
    required = marker_field(marker_path, "required_pm_action")
    combined = f"{ceiling}\n{required}".lower()
    return "capture-gated" in combined or (
        "capture" in combined and "before" in combined and "qa-passed-awaiting-ci" in combined
    )

def capture_gated_phase_a_marker(pr_number, head):
    if not pr_requires_fresh_capture_before_ci(pr_number):
        return ""
    for marker in (
        Path(f"/tmp/pm-claude-code-review-{pr_number}-{head}.md"),
        Path(f"/tmp/pm-opus-code-review-{pr_number}-{head}.md"),
    ):
        if not marker.exists() or not file_recent(marker, 24):
            continue
        try:
            text = marker.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if not re.search(r"^(PM_CLAUDE_REVIEW|PM_OPUS_REVIEW):\s*PASS", text, re.M):
            continue
        marker_head = marker_field(marker, "headRefOid")
        if marker_head and marker_head != head:
            continue
        pass_scope = marker_field(marker, "pass_scope")
        if not re.fullmatch(r"(phase-a|blocker-clear)(?:\s.*)?", pass_scope or ""):
            continue
        if pm_review_marker_capture_gated(marker):
            return str(marker)
    return ""

def current_head_capture_green(pr_number, head):
    # Exact-head live capture workflow state is authoritative over a stale
    # pre-capture ci-verdict's watch_runs (Rajiv thread 1786713760.734709 ts
    # 1786716645.606919). The verdict comment is produced BEFORE the capture
    # dispatch and can name an older run; once a terminal successful exact-head
    # E2E LLM Proxy Capture (manual) run exists, it must win so the sweep never
    # emits PR_CAPTURE_REMOTE_DISPATCH_REQUIRED for an already-green capture.
    live_run = latest_live_capture_run(pr_number, head)
    if live_run:
        run_status_value = live_run.get("status") or "unknown"
        conclusion = live_run.get("conclusion") or ""
        if run_status_value == "completed" and conclusion == "success":
            artifact = live_run.get("capture_artifact_conclusion") or live_run.get("capture_job_conclusion") or "workflow_success"
            return True, (
                f"capture_run={live_run.get('run_id')} status=completed "
                f"conclusion=success capture_artifact={artifact} classification=live_capture_workflow"
            )
        # A live exact-head capture run that is NOT terminal success is
        # authoritative over the stale verdict too: return its real state so
        # the consumer emits watch/failure (never dispatch) for an in-flight or
        # failed exact-head capture.
        return False, (
            f"capture_run={live_run.get('run_id')} status={run_status_value} "
            f"conclusion={conclusion or 'unknown'} age_min={live_run.get('age_min', 0)} "
            f"format_done={live_run.get('capture_format_done', False)} "
            f"fixture_written={live_run.get('capture_fixture_written', False)} "
            "classification=live_capture_workflow"
        )
    verdict = latest_capture_verdict(pr_number, head)
    if not verdict:
        return False, "missing_current_head_capture_verdict"
    watch_runs = [str(x) for x in (verdict.get("watch_runs") or []) if str(x)]
    capture_run = latest_watch_run_id(watch_runs)
    if not capture_run:
        return False, f"capture_verdict_missing_watch_run classification={verdict.get('classification')}"
    status = run_status(capture_run)
    run_status_value = status.get("status") or "unknown"
    conclusion = status.get("conclusion") or ""
    if run_status_value == "completed" and conclusion == "success":
        artifact = status.get("capture_artifact_conclusion") or status.get("capture_job_conclusion") or "workflow_success"
        return True, f"capture_run={capture_run} status=completed conclusion=success capture_artifact={artifact}"
    return False, (
        f"capture_run={capture_run} status={run_status_value} "
        f"conclusion={conclusion or 'unknown'} format_done={status.get('capture_format_done', False)} "
        f"fixture_written={status.get('capture_fixture_written', False)} classification={verdict.get('classification')}"
    )

def pending_slot_ready_event_for_pr(pr_number, head):
    matches = []
    for path in Path("/tmp/slot-ready-events").glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(data.get("status") or "") != "pending":
            continue
        if str(data.get("pr") or "") != str(pr_number):
            continue
        if head and str(data.get("head_sha") or "") != str(head):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0
        matches.append((mtime, str(path)))
    return sorted(matches)[-1][1] if matches else ""

def ci_investigation_active_reason(pr_number, head):
    # PM may deliberately keep pm-blocked:ci while a same-head
    # ci-failure-investigation agent is classifying a terminal red run. In that
    # state the label is not stale and must not trigger a remove/re-add loop.
    candidates = [
        Path(f"/tmp/pm-{pr_number}-ci-blocker-proof.txt"),
        Path(f"/tmp/ci-blocker-proof-{pr_number}.txt"),
        Path(f"/tmp/ci-failure-investigation-{pr_number}.txt"),
    ]
    candidates.extend(Path("/tmp").glob(f"*{pr_number}*ci*blocker*proof*.txt"))
    candidates.extend(Path("/tmp").glob(f"*{pr_number}*ci*investigation*.txt"))

    active_re = re.compile(
        r"\b(?:IN\s+FLIGHT|in[_ -]?flight|actively\s+running|classifying|"
        r"classification\s+is\s+actively\s+running|decision\s+gated\s+on\s+verdict)\b",
        re.I,
    )
    terminal_re = re.compile(
        r"\b(?:CI\s+NOW\s+GREEN|terminal\s+condition\s+REACHED|"
        r"both\s+current-head\s+runs\s+green|terminal-green\s+achieved|"
        r"completed\s+SUCCESS|remove\s+pm-blocked:ci|NOT\s+a\s+CI\s+matter)\b",
        re.I,
    )
    for path in dict.fromkeys(candidates):
        if not path.exists() or not file_recent(path, 12):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if not text_names_head(text, head):
            continue
        if "ci-failure-investigation" not in text.lower():
            continue
        if terminal_re.search(text):
            continue
        if not active_re.search(text):
            continue
        return f"same_head_ci_investigation_active proof={path}"
    return ""

def dependency_refs_from_marker(text, pr_number):
    refs = []
    for line in text.splitlines():
        if not re.search(r"\b(blocked|blocker|dependency|depends|resolution|merges?|unblock)\b", line, re.I):
            continue
        line_refs = re.findall(r"#(\d{3,6})", line)
        line_refs.extend(
            re.findall(r"\bblocked[-_ ]?(?:on|by)[:\s-]+#?(\d{3,6})", line, re.I)
        )
        for raw in line_refs:
            if str(raw) == str(pr_number):
                continue
            refs.append(str(raw))
    return list(dict.fromkeys(refs))

def dependency_ref_status(ref):
    if not hasattr(dependency_ref_status, "_cache"):
        dependency_ref_status._cache = {}
    cache = dependency_ref_status._cache
    ref = str(ref)
    if ref in cache:
        return cache[ref]

    for kind, args in (
        ("pr", ["gh", "pr", "view", ref, "--repo", gh_repo, "--json", "state,mergedAt"]),
        ("issue", ["gh", "issue", "view", ref, "--repo", gh_repo, "--json", "state,closedAt"]),
    ):
        try:
            proc = read_only_subprocess_run(args, text=True, capture_output=True, timeout=8)
            if proc.returncode != 0:
                continue
            data = json.loads(proc.stdout or "{}")
        except Exception:
            continue
        state = str(data.get("state") or "").upper()
        open_ = state == "OPEN"
        terminal = state in {"MERGED", "CLOSED"} or bool(data.get("mergedAt") or data.get("closedAt"))
        out = {"ref": ref, "kind": kind, "state": state or "UNKNOWN", "open": open_, "terminal": terminal}
        cache[ref] = out
        return out

    out = {"ref": ref, "kind": "unknown", "state": "UNKNOWN", "open": False, "terminal": False}
    cache[ref] = out
    return out

def dependency_label_age_minutes(pr_number):
    if not hasattr(dependency_label_age_minutes, "_cache"):
        dependency_label_age_minutes._cache = {}
    cache = dependency_label_age_minutes._cache
    pr_number = str(pr_number)
    if pr_number in cache:
        return cache[pr_number]
    try:
        proc = read_only_subprocess_run(
            ["gh", "api", f"repos/{gh_repo}/issues/{pr_number}/events?per_page=100"],
            text=True,
            capture_output=True,
            timeout=8,
        )
        if proc.returncode != 0:
            cache[pr_number] = None
            return None
        events = json.loads(proc.stdout or "[]")
    except Exception:
        events = []
    latest = None
    for event in events:
        if str(event.get("event") or "") != "labeled":
            continue
        label = event.get("label") or {}
        if str(label.get("name") or "") != "pm-blocked:dependency":
            continue
        created = parse_time(event.get("created_at") or event.get("createdAt"))
        if created and (latest is None or created > latest):
            latest = created
    cache[pr_number] = (
        int((datetime.now(timezone.utc) - latest).total_seconds() // 60)
        if latest
        else None
    )
    return cache[pr_number]

def dependency_age_token(pr_number):
    age = dependency_label_age_minutes(pr_number)
    return str(age) if age is not None else "unknown"

def dependency_marker_refs(raw, marker_data, pr_number):
    blob = raw
    structured_refs = []
    if marker_data:
        blob += "\n" + json.dumps(marker_data, sort_keys=True)
        for key in ("blocked_on", "blocked_by", "dependency", "dependencies", "dependency_refs", "refs", "blockers"):
            value = marker_data.get(key)
            if value is None:
                continue
            values = value if isinstance(value, list) else [value]
            for item in values:
                blob += f"\n{key}: {item}"
                match = re.fullmatch(r"#?(\d{3,6})", str(item).strip())
                if match and match.group(1) != str(pr_number):
                    structured_refs.append(match.group(1))
    return list(dict.fromkeys(structured_refs + dependency_refs_from_marker(blob, pr_number)))

def dependency_watch_markers(text):
    markers = []
    for match in re.finditer(r"<!--\s*pm-dependency-watch:\s*", text, re.I):
        start = match.end()
        end = text.find("-->", start)
        if end == -1:
            continue
        raw = text[start:end].strip()
        raw = re.sub(r"^v\d+\s*", "", raw, count=1, flags=re.I)
        data = {}
        if raw.startswith("{"):
            try:
                data = json.loads(raw)
            except Exception:
                data = {}
        else:
            for line in raw.splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                data[key.strip()] = value.strip()
        markers.append((raw, data))
    return markers

def ci_dependency_blocked_state_from_pr_comments(pr_number, head):
    if not hasattr(ci_dependency_blocked_state_from_pr_comments, "_cache"):
        ci_dependency_blocked_state_from_pr_comments._cache = {}
    cache = ci_dependency_blocked_state_from_pr_comments._cache
    key = (str(pr_number), str(head or ""))
    if key in cache:
        return cache[key]

    comments = sorted(
        pr_comments(pr_number),
        key=lambda c: parse_time(c.get("createdAt")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    for comment in comments:
        text = str(comment.get("body") or "")
        ts = str(comment.get("createdAt") or "")
        for raw, marker_data in dependency_watch_markers(text):
            marker_pr = str(marker_data.get("pr") or "")
            if marker_pr and marker_pr != str(pr_number):
                continue
            marker_head = str(
                marker_data.get("head")
                or marker_data.get("headRefOid")
                or marker_data.get("head_sha")
                or marker_data.get("sha")
                or ""
            )
            if head and marker_head and marker_head not in {str(head), str(head)[:10]}:
                continue
            if head and not marker_head and not text_names_head(text, head):
                continue
            refs = dependency_marker_refs(raw, marker_data, pr_number)
            if not refs:
                continue
            statuses = [dependency_ref_status(ref) for ref in refs]
            open_refs = [s for s in statuses if s.get("open")]
            proof = f"pr-comment:{ts or 'unknown'}:pm-dependency-watch"
            if open_refs:
                open_summary = ",".join(f"#{s['ref']}:{s['kind']}:{s['state']}" for s in open_refs)
                cache[key] = ("blocked", f"dependency_blocked refs={open_summary} proof={proof}")
                return cache[key]
            terminal_refs = [s for s in statuses if s.get("terminal")]
            if terminal_refs and len(terminal_refs) == len(statuses):
                terminal_summary = ",".join(f"#{s['ref']}:{s['kind']}:{s['state']}" for s in terminal_refs)
                cache[key] = ("unblocked", f"dependency_unblocked refs={terminal_summary} proof={proof}")
                return cache[key]

        verdicts = []
        for match in re.finditer(r"<!--\s*ci-verdict:\s*", text, re.I):
            start = match.end()
            end = text.find("-->", start)
            if end == -1:
                continue
            raw = text[start:end].strip()
            try:
                verdicts.append(json.loads(raw))
            except Exception:
                continue
        for verdict in verdicts:
            verdict_pr = str(verdict.get("pr") or "")
            if verdict_pr and verdict_pr != str(pr_number):
                continue
            verdict_head = str(
                verdict.get("current_pr_head_sha")
                or verdict.get("run_head_sha")
                or verdict.get("sha")
                or ""
            )
            if head and verdict_head and verdict_head != str(head):
                continue
            if head and not verdict_head and not text_names_head(text, head):
                continue
            blob = json.dumps(verdict, sort_keys=True) + "\n" + text
            if "pm-blocked:dependency" not in blob and not re.search(r"\bdependency\b", blob, re.I):
                continue
            refs = dependency_refs_from_marker(blob, pr_number)
            if not refs:
                continue
            statuses = [dependency_ref_status(ref) for ref in refs]
            open_refs = [s for s in statuses if s.get("open")]
            proof = f"pr-comment:{ts or 'unknown'}"
            if open_refs:
                open_summary = ",".join(f"#{s['ref']}:{s['kind']}:{s['state']}" for s in open_refs)
                cache[key] = ("blocked", f"dependency_blocked refs={open_summary} proof={proof}")
                return cache[key]
            terminal_refs = [s for s in statuses if s.get("terminal")]
            if terminal_refs and len(terminal_refs) == len(statuses):
                terminal_summary = ",".join(f"#{s['ref']}:{s['kind']}:{s['state']}" for s in terminal_refs)
                cache[key] = ("unblocked", f"dependency_unblocked refs={terminal_summary} proof={proof}")
                return cache[key]

    cache[key] = ("", "")
    return cache[key]

def ci_dependency_blocked_state(pr_number, head):
    # Deliberate not-stale pm-blocked:ci on a blameless PR whose terminal-red or
    # skipped CI is caused by a pre-existing regression fixed in a sibling linchpin
    # PR. Consulted ONLY in the terminal_bad / missing_or_skipped branches, which run
    # AFTER the terminal_green promote path, so a PR that later goes green is never
    # hidden by this skip. Gated on a head-specific marker that names the blocking PR
    # and self-invalidates on head move. Single quotes stay balanced (macos bash scans
    # heredoc bodies in command substitution for quote balance).
    marker = Path(f"/tmp/pm-dependency-blocked-{pr_number}.txt")
    if not marker.exists() or not file_recent(marker, 24):
        return ci_dependency_blocked_state_from_pr_comments(pr_number, head)
    try:
        text = marker.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ci_dependency_blocked_state_from_pr_comments(pr_number, head)
    if not text_names_head(text, head):
        return ci_dependency_blocked_state_from_pr_comments(pr_number, head)
    if not re.search(r"blocked[_ -]on[:\s]+#?\d+", text, re.I):
        return ci_dependency_blocked_state_from_pr_comments(pr_number, head)
    refs = dependency_refs_from_marker(text, pr_number)
    if not refs:
        return ci_dependency_blocked_state_from_pr_comments(pr_number, head)
    statuses = [dependency_ref_status(ref) for ref in refs]
    open_refs = [s for s in statuses if s.get("open")]
    if open_refs:
        open_summary = ",".join(f"#{s['ref']}:{s['kind']}:{s['state']}" for s in open_refs)
        return "blocked", f"dependency_blocked refs={open_summary} proof={marker}"
    terminal_refs = [s for s in statuses if s.get("terminal")]
    if terminal_refs and len(terminal_refs) == len(statuses):
        terminal_summary = ",".join(f"#{s['ref']}:{s['kind']}:{s['state']}" for s in terminal_refs)
        return "unblocked", f"dependency_unblocked refs={terminal_summary} proof={marker}"
    return ci_dependency_blocked_state_from_pr_comments(pr_number, head)

def ci_dependency_blocked_reason(pr_number, head):
    state, reason = ci_dependency_blocked_state(pr_number, head)
    return reason if state == "blocked" else ""

def ci_hold_watch_state(pr_number, head, ci_run, e2e_run):
    marker = Path(f"/tmp/pm-ci-watch-{pr_number}.json")
    if not marker.exists():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    if payload.get("schema") != "pm-ci-watch/v1" or payload.get("status") != "active":
        return None
    if str(payload.get("headRefOid") or "") != str(head or ""):
        return None
    failed_run = str(payload.get("failed_run") or "")
    current_bad_runs = {
        str(bucket.get("run_id") or "")
        for bucket in (ci_run, e2e_run)
        if bucket.get("state") == "bad"
    }
    if not failed_run or failed_run not in current_bad_runs:
        return None
    proof = Path(str(payload.get("proof") or ""))
    if not proof.is_file():
        return None
    try:
        proof_text = proof.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    if not text_names_head(proof_text, head) or failed_run not in proof_text:
        return None
    return {
        "classification": str(payload.get("classification") or "unknown"),
        "failed_run": failed_run,
        "baseline_run": str(payload.get("baseline_run") or "none"),
        "proof": str(proof),
        "marker": str(marker),
    }

def dependency_watch_key(pr_number, head, dep_reason, workflows):
    raw = "|".join([
        str(pr_number),
        str(head or ""),
        " ".join(str(dep_reason or "").split()),
        " ".join(str(workflows or "").split()),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

def dependency_watch_ack_path(pr_number, key):
    return dependency_ack_dir / f"pr-{pr_number}-{key}.json"

def prior_dependency_watch_logged(pr_number, head, dep_reason, workflows):
    if not dependency_log.exists():
        return False
    try:
        text = dependency_log.read_text(encoding="utf-8", errors="replace")[-250000:]
    except Exception:
        return False
    needles = [
        f"PR_CI_DEPENDENCY_BLOCKED_REQUIRED PR#{pr_number}",
        f"head={str(head or '')[:10]}",
        str(dep_reason or ""),
        str(workflows or ""),
    ]
    return all(needle and needle in text for needle in needles)

def record_dependency_watch_ack(pr_number, head, dep_reason, workflows, key, source):
    if dry_run:
        return
    try:
        dependency_ack_dir.mkdir(parents=True, exist_ok=True)
        path = dependency_watch_ack_path(pr_number, key)
        payload = {
            "schema_version": 1,
            "status": "acknowledged",
            "source": source,
            "trigger": trigger,
            "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "pr": int(pr_number),
            "head": str(head or ""),
            "head_short": str(head or "")[:10],
            "dependency_reason": str(dep_reason or ""),
            "workflows": str(workflows or ""),
            "key": key,
            "note": "Suppress repeated open dependency watch rows until dependency state, PR head, or workflow evidence changes.",
        }
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass

def dependency_watch_acknowledged(pr_number, head, dep_reason, workflows):
    key = dependency_watch_key(pr_number, head, dep_reason, workflows)
    path = dependency_watch_ack_path(pr_number, key)
    if path.exists():
        return True, key, str(path)
    if prior_dependency_watch_logged(pr_number, head, dep_reason, workflows):
        record_dependency_watch_ack(pr_number, head, dep_reason, workflows, key, "prior_pr_state_sweep_log")
        return True, key, str(path)
    return False, key, str(path)

def should_emit_dependency_watch(pr_number, head, dep_reason, workflows):
    acknowledged, key, path = dependency_watch_acknowledged(pr_number, head, dep_reason, workflows)
    if acknowledged:
        return False, key, path
    record_dependency_watch_ack(pr_number, head, dep_reason, workflows, key, "first_pr_state_sweep_emit")
    return True, key, path

def dependency_visible_marker_ok(pr, ack_path):
    if not has_label(pr, "pm-blocked:dependency"):
        return False
    try:
        if Path(ack_path).exists():
            return True
    except Exception:
        return False
    # Stop hooks commonly run this sweep in dry-run mode, where
    # should_emit_dependency_watch() intentionally does not write the ack file.
    # The visible state is still legitimate when the PR has the dependency label
    # and the current-head dependency marker is valid/open.
    dep_state, _dep_reason = ci_dependency_blocked_state(
        int(pr.get("number") or 0),
        str(pr.get("headRefOid") or ""),
    )
    return dep_state == "blocked"

def dependency_watch_output_enabled():
    return trigger == "hourly-ops-audit" or trigger.startswith("hourly-")

def dependency_workflow_summary(pr):
    workflow_state, ci_run, e2e_run = workflow_summary(pr)
    bad = workflow_bad_summary(ci_run, e2e_run)
    return bad or f"workflow_state={workflow_state}"

def slot_entry_matches_pr(entry, pr_number, branch):
    if not entry or not entry.get("occupied"):
        return False
    pr_s = str(pr_number or "")
    branch_s = str(branch or "")
    slot_pr = str(entry.get("pr") or "")
    slot_branch = str(entry.get("branch") or "")
    return bool(pr_s and branch_s and slot_pr == pr_s and slot_branch == branch_s)

def slot_entry_summary(entry):
    if not entry:
        return "missing"
    return command_quote(
        f"occupied={entry.get('occupied')} pr={entry.get('pr')} issue={entry.get('issue')} "
        f"branch={entry.get('branch')} task={str(entry.get('task') or '')[:80]}"
    )

def skip_long_parked(pr):
    labs = labels(pr)
    if has_label(pr, "status:on-hold-prm-calibration"):
        return True
    if has_label(pr, "pm-blocked:rebase") and pr.get("isDraft") and not recently_updated(pr, 96):
        return True
    if has_label(pr, "P3") and has_label(pr, "pm-blocked:pm-gate") and not recently_updated(pr, 96):
        return True
    return False

def active_slot_for_pr(pr_number, issue, branch):
    issue_s = str(issue or "")
    branch_s = str(branch or "")
    pr_s = str(pr_number or "")
    for entry in slots:
        if not entry.get("occupied"):
            continue
        slot_pr = str(entry.get("pr") or "")
        slot_issue = str(entry.get("issue") or "")
        slot_branch = str(entry.get("branch") or "")
        task = str(entry.get("task") or "")
        if pr_s and slot_pr == pr_s:
            return entry
        if issue_s and slot_issue == issue_s:
            return entry
        if branch_s and (slot_branch == branch_s or branch_s in task):
            return entry
    return None

def exact_mop_owner_for_pr(pr_number, branch):
    """Return one occupied MoP owner only when PR and branch both match."""
    matches = [entry for entry in slots if slot_entry_matches_pr(entry, pr_number, branch)]
    return matches[0] if len(matches) == 1 else None

def live_capture_lease(slot, pr_number, head):
    path = clone_lock_dir / f"pm-clone-lock-{slot}"
    try:
        fields = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition(": ")
            if sep:
                fields[key] = value
        if fields.get("kind") != "capture-local-proof":
            return None
        if str(fields.get("pr") or "") != str(pr_number):
            return None
        if head and str(fields.get("headRefOid") or "") != str(head):
            return None
        pid = int(fields.get("pid") or "0")
        if pid <= 0:
            return None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            pass
        return {"pid": pid, "path": str(path), "branch": fields.get("branch") or "unknown"}
    except Exception:
        return None

def command_quote(s):
    return str(s).replace(" ", "\\ ")

def dependency_unblocked_command(n, dep_reason=""):
    return "UNSUPPORTED_LIFECYCLE_ACTION:dependency-unblocked"

for pr in sorted(merged_prs, key=lambda p: int(p.get("number") or 0), reverse=True):
    n = int(pr.get("number") or 0)
    labs = [str(x.get("name") or "") for x in pr.get("labels") or [] if x.get("name")]
    if "pm-state:merged-cleanup-pending" not in labs and "pm-cleanup:needed" not in labs:
        continue
    merged_at = parse_time(pr.get("mergedAt"))
    age = int((datetime.now(timezone.utc) - merged_at).total_seconds() // 60) if merged_at else 99999
    if age < 15:
        continue
    cleanup = cleanup_state_for(n, str(pr.get("headRefOid") or ""))
    cleanup_state = cleanup["state"]
    cleanup_age = None
    if cleanup_state == "started" and cleanup.get("started_at"):
        try:
            started_at = datetime.fromisoformat(cleanup["started_at"].replace("Z", "+00:00"))
            cleanup_age = int((datetime.now(timezone.utc) - started_at).total_seconds() // 60)
        except (TypeError, ValueError):
            cleanup_age = None
    if cleanup_state == "completed":
        continue
    if cleanup_state == "started" and cleanup_age is not None and cleanup_age < 15:
        continue
    if "pm-blocked:dependency" in labs:
        # Cleanup correctly blocked on a tracked dependency (e.g. a recovery-fix
        # follow-up PR whose migration must ship + re-run before closed-clean).
        # dependency-watch handling owns this (hourly-filtered), so do not re-flag
        # it as a stalled cleanup-closeout on every Stop-hook sweep.
        continue
    title = command_quote((pr.get("title") or "").replace("|", "/")[:120])
    reason = {
        "started": "cleanup_started_over_15m" if cleanup_age is not None else "cleanup_start_state_unavailable",
        "uncertain": "cleanup_current_head_uncertain",
        "unavailable": "cleanup_start_state_unavailable",
    }.get(cleanup_state, "merged_cleanup_pending_without_start_over_15m")
    cleanup_age_token = str(cleanup_age) if cleanup_age is not None else "unknown" if cleanup_state != "missing" else "missing"
    print(
        f"PR_CLEANUP_CLOSEOUT_REQUIRED PR#{n} reason={reason} "
        f"age_min={age} cleanup_started_age_min={cleanup_age_token} labels={','.join(labs) or 'none'} title={title} "
        f"remediation=run Skill(cleanup-pr) for PR#{n}"
    )

for pr in sorted(prs, key=lambda p: int(p.get("number") or 0), reverse=True):
    n = int(pr["number"])
    title = (pr.get("title") or "").replace("|", "/")[:140]
    state = effective_state(pr)
    # CTO rescue is exclusive ownership outside the PM slot/rework state
    # machine. Do not emit any PM transition for this PR until terminal rescue
    # processing removes the ownership label.
    if has_label(pr, "cto-rescue:in-progress"):
        continue

    pm_state = state_label(pr)
    slots_for_pr = slot_labels(pr)
    branch = pr.get("headRefName") or ""
    issue = linked_issue(pr)
    exact_owner = exact_mop_owner_for_pr(n, branch)
    if not issue and exact_owner:
        issue = str(exact_owner.get("issue") or "")
    check_state, bad_checks, success_count, skipped_count, success_names = check_summary(pr)
    blockers = [x for x in labels(pr) if x.startswith("pm-blocked:")]
    draft = bool(pr.get("isDraft"))
    merge_state = pr.get("mergeStateStatus") or "UNKNOWN"
    workflow_guard_cache = []

    def get_workflow_guard():
        if not workflow_guard_cache:
            workflow_guard_cache.append(workflow_change_guard(pr))
        return workflow_guard_cache[0]

    if skip_long_parked(pr):
        continue

    head = str(pr.get("headRefOid") or "")

    # A CTO hold is an explicit external wait, not missing PM review work.
    # Keep it visible to hourly audit while excluding it from Stop-time actions.
    if has_label(pr, "pm-blocked:cto"):
        print(
            f"PR_CTO_DECISION_WAITING PR#{n} reason={state or 'no_state'}_cto_decision_wait "
            f"blocker=cto issue=#{issue or 'unknown'} branch={branch} head={head[:10]} "
            f"command=wait_for_cto_disposition_then_reconcile"
        )
        continue

    drift = state_drift_reason(pr)

    # Hourly owns aggregate base-branch drift. mergeStateStatus is insufficient:
    # GitHub may report CLEAN when branch protection does not require a current
    # base. Count the actual commits missing from main for old PRs, but do not
    # create moving-head churn during active CI or legitimate external waits.
    if dependency_watch_output_enabled():
        age_hours = pr_age_hours(pr)
        drift_wait_blockers = {
            "pm-blocked:dependency",
            "pm-blocked:product",
            "pm-blocked:capture",
            "pm-blocked:infra",
        }
        eligible_for_main_drift = (
            age_hours is not None
            and age_hours >= old_pr_age_hours
            and state not in {"merge-ready", "pm-state:merge-ready"}
            and check_state != "pending"
            and not any(has_label(pr, label) for label in drift_wait_blockers)
        )
        if eligible_for_main_drift:
            behind_by = commits_behind_main(pr)
            # CTO decision 2026-08-09 thread 1786253446.752739: behind_by is
            # telemetry only for a conflict-free exact-head-green PR. A PR that
            # GitHub reports CLEAN/MERGEABLE with real exact-head required
            # CI/E2E green must never be forced into a merge-main/rebase by
            # main drift alone; the pre-merge guard owns merge health.
            # Telemetry-only drift requires the required workflows themselves
            # genuinely green. statusCheckRollup can carry OLD successful
            # entries for a required job alongside a NEWER skipped/failed one,
            # so name membership in success_names is not evidence. Bind
            # required_green to the NEWEST rollup entry per required job: it
            # must be COMPLETED with conclusion SUCCESS; a newer skipped or
            # failed entry overrides an older success. Fail closed when the
            # required-job mapping is unreadable or a required job has no
            # terminal entry.
            required_jobs_ok = (
                isinstance(required_ci_jobs, dict)
                and len(required_ci_jobs) >= 2
                and all(str(k).strip() for k in required_ci_jobs)
            )
            required_green = False
            if required_jobs_ok:
                required_values = {str(v).lower() for v in required_ci_jobs.values()}
                newest_by_job = {}
                for check in pr.get("statusCheckRollup") or []:
                    check_name = str(
                        check.get("name") or check.get("workflowName") or ""
                    ).lower()
                    if check_name not in required_values:
                        continue
                    check_ts = str(
                        check.get("completed_at")
                        or check.get("started_at")
                        or check.get("created_at")
                        or ""
                    )
                    if (
                        check_name not in newest_by_job
                        or check_ts >= newest_by_job[check_name][0]
                    ):
                        newest_by_job[check_name] = (check_ts, check)
                required_green = (
                    set(newest_by_job) == required_values
                    and all(
                        (check.get("status") or "").upper() == "COMPLETED"
                        and (check.get("conclusion") or "").upper() == "SUCCESS"
                        for _, check in newest_by_job.values()
                    )
                )
            mergeable_and_green = (
                merge_state in {"CLEAN", "MERGEABLE"}
                and check_state == "terminal_green"
                and bad_checks == 0
                and required_green
            )
            if behind_by is not None and behind_by >= main_behind_threshold:
                if mergeable_and_green:
                    print(
                        f"PR_MAIN_DRIFT_INFO PR#{n} reason=old_pr_far_behind_main_telemetry_only "
                        f"age_hours={age_hours} behind_by={behind_by} threshold={main_behind_threshold} "
                        f"state={state or 'no_state'} mergeStateStatus={merge_state} "
                        f"check_state={check_state} "
                        f"issue=#{issue or 'unknown'} branch={branch} head={head[:10]} "
                        f"command=no_required_rebase_mergeable_and_green"
                    )
                else:
                    owner = exact_mop_owner_for_pr(n, branch)
                    owner_slot = str((owner or {}).get("slot") or "")
                    slot_token = f" slot:{owner_slot}" if owner_slot else ""
                    next_step = (
                        f"message-slot\ --slot\ {owner_slot}\ --force\ merge_origin/main_at_next_phase_boundary_then_push_and_stop"
                        if owner_slot
                        else "create_current_head_merge-main_packet_then_Skill(direct-assign)_before_fresh_todo"
                    )
                    print(
                        f"PR_MAIN_DRIFT_REQUIRED PR#{n}{slot_token} reason=old_pr_far_behind_main "
                        f"age_hours={age_hours} behind_by={behind_by} threshold={main_behind_threshold} "
                        f"state={state or 'no_state'} mergeStateStatus={merge_state} "
                        f"check_state={check_state} "
                        f"issue=#{issue or 'unknown'} branch={branch} head={head[:10]} "
                        f"command={next_step}"
                    )

    # Cross-state invariants run before the state-specific handlers. These keep
    # stale blockers from hiding once PM moves a PR out of qa-passed-awaiting-ci.
    if has_label(pr, "pm-blocked:rebase") and merge_state == "CLEAN" and not draft:
        owner_slot = str((exact_owner or {}).get("slot") or "<slot>")
        owner_epoch = str((exact_owner or {}).get("assignment_epoch") or "<epoch>")
        print(
            f"PR_STALE_BLOCKER_REQUIRED PR#{n} reason=rebase_blocker_but_merge_state_clean "
            f"blocker=pm-blocked:rebase issue=#{issue or 'unknown'} branch={branch} head={head[:10]} "
            f"remediation=resolve_the_exact_head_rebase_condition_before_releasing_the_operator_block"
        )
        continue

    if has_label(pr, "pm-blocked:dependency"):
        dep_state, dep_reason = ci_dependency_blocked_state(n, head)
        workflows = dependency_workflow_summary(pr)
        dep_age = dependency_age_token(n)
        if dep_state == "blocked":
            _emit_watch, ack_key, ack_path = should_emit_dependency_watch(n, head, dep_reason, workflows)
            if dependency_visible_marker_ok(pr, ack_path):
                print(
                    f"PR_CI_DEPENDENCY_WATCHING PR#{n} reason={state or 'no_state'}_dependency_open_acknowledged "
                    f"{dep_reason} workflows={workflows} label=pm-blocked:dependency watch_ack={ack_path} ack_key={ack_key} "
                    f"head={head[:10]} command=wait_dependency_then_retarget_rebase_and_cto_label_gated_wave"
                )
                continue
        if dep_state == "unblocked":
            print(
                f"PR_DEPENDENCY_UNBLOCKED_REQUIRED PR#{n} reason={state or 'no_state'}_dependency_blocker_terminal "
                f"{dep_reason} head={head[:10]} command={dependency_unblocked_command(n, dep_reason)}"
            )
            continue
        wedge_age = dependency_label_age_minutes(n)
        wedge_severity = "stale" if wedge_age is None or wedge_age >= dependency_wedge_warn_minutes else "missing"
        print(
            f"PR_DEPENDENCY_WEDGE_REQUIRED PR#{n} reason={state or 'no_state'}_dependency_label_without_current_head_machine_proof "
            f"age_min={dep_age} severity={wedge_severity} workflows={workflows} label=pm-blocked:dependency "
            f"issue=#{issue or 'unknown'} branch={branch} head={head[:10]} "
            f"command=UNSUPPORTED_LIFECYCLE_ACTION:block-pr"
        )
        continue

    if has_label(pr, "pm-blocked:capture"):
        active_slot = exact_mop_owner_for_pr(n, branch)
        fuzzy_slot = active_slot_for_pr(n, issue, branch)
        if not active_slot and fuzzy_slot:
            slot = fuzzy_slot.get("slot") or "unknown"
            if emit_incomplete_owner(fuzzy_slot):
                print(
                f"PR_STATE_RECONCILE_REQUIRED PR#{n} slot:{slot} reason=capture_mop_owner_tuple_incomplete "
                f"state={state or 'no_state'} issue=#{issue or 'unknown'} branch={branch} head={head[:10]} "
                f"assignment_epoch={fuzzy_slot.get('assignment_epoch')} {owner_tokens(fuzzy_slot)} mop={slot_entry_summary(fuzzy_slot)} command=UNSUPPORTED_LIFECYCLE_ACTION:reconcile-capacity"
                )
            continue
        if active_slot and state == "pm-state:blocked-rework":
            rearm = capture_rearm_watch(n, active_slot, branch)
            if rearm:
                slot = active_slot.get("slot") or "unknown"
                print(
                    f"PR_CAPTURE_REARM_AFTER_MAIN_SYNC_WATCHING PR#{n} slot:{slot} "
                    f"reason=authorized_conflict_resolution_preserves_capture_gate "
                    f"obligation={rearm.get('id')} issue=#{issue or rearm.get('issue') or 'unknown'} "
                    f"branch={branch} head={head[:10]} main_head={str(rearm.get('main_head') or '')[:10]} "
                    f"mop={slot_entry_summary(active_slot)} "
                    f"command=merge_authorized_main_release_slot_then_UNSUPPORTED_LIFECYCLE_ACTION:capture-remote-dispatch_on_new_exact_head"
                )
                continue
            capture_rework = capture_rework_watch(n, active_slot, branch)
            if capture_rework:
                slot = active_slot.get("slot") or "unknown"
                print(
                    f"PR_CAPTURE_REWORK_IN_PROGRESS PR#{n} slot:{slot} "
                    f"reason=authorized_product_rework_preserves_capture_gate "
                    f"obligation={capture_rework.get('id')} issue=#{issue or capture_rework.get('issue') or 'unknown'} "
                    f"branch={branch} head={head[:10]} "
                    f"assignment_epoch={capture_rework.get('assignment_epoch')} "
                    f"mop={slot_entry_summary(active_slot)} "
                    f"command=continue_product_rework_then_slot-ready_and_capture_resulting_exact_head"
                )
                continue
            if capture_rework_by_pr.get(int(n)):
                slot = active_slot.get("slot") or "unknown"
                print(
                    f"PR_STATE_RECONCILE_REQUIRED PR#{n} slot:{slot} "
                    f"reason=capture_rework_authority_tuple_mismatch "
                    f"state={state} issue=#{issue or 'unknown'} branch={branch} head={head[:10]} "
                    f"mop={slot_entry_summary(active_slot)} "
                    f"command=UNSUPPORTED_LIFECYCLE_ACTION:reconcile-capacity"
                )
                continue
            if not capture_rearm_authority_available:
                slot = active_slot.get("slot") or "unknown"
                print(
                    f"PR_STATE_RECONCILE_REQUIRED PR#{n} slot:{slot} "
                    f"reason=capture_rearm_authority_unavailable "
                    f"state={state} issue=#{issue or 'unknown'} branch={branch} head={head[:10]} "
                    f"command=UNSUPPORTED_LIFECYCLE_ACTION:reconcile-capacity"
                )
                continue
        lease_slot = str((active_slot or {}).get("slot") or "")
        if not lease_slot:
            for candidate_slot in slots_for_pr:
                if live_capture_lease(candidate_slot, n, head):
                    lease_slot = str(candidate_slot)
                    break
        capture_lease = live_capture_lease(lease_slot, n, head) if lease_slot else None
        if capture_lease:
            cl_pid = capture_lease.get("pid")
            cl_path = capture_lease.get("path")
            state_disp = state if state else "no_state"
            issue_disp = issue if issue else "unknown"
            print(
                f"PR_CAPTURE_LOCAL_RUNNING PR#{n} slot:{lease_slot} reason=live_capture_checkout_lease "
                f"pid={cl_pid} state={state_disp} issue=#{issue_disp} "
                f"branch={branch} head={head[:10]} lock={cl_path} command=wait_for_local_capture_terminal"
            )
            continue
        release_slot_after_rerun = ""
        if active_slot:
            release_slot_after_rerun = str(active_slot.get("slot") or "")
        local_capture_line = capture_local_transition_line(
            n,
            head,
            issue,
            branch,
            "capture_blocker",
            release_slot_after_rerun,
        )
        if local_capture_line:
            print(local_capture_line)
            continue
        if active_slot:
            slot = active_slot.get("slot") or "unknown"
            print(
                f"PR_CAPTURE_SLOT_RELEASE_REQUIRED PR#{n} slot:{slot} reason=capture_blocker_holds_dev_slot "
                f"state={state or 'no_state'} issue=#{issue or 'unknown'} branch={branch} head={head[:10]} "
                f"mop={slot_entry_summary(active_slot)} "
                f"remediation=release_slot_{slot}_via_Skill(direct-release)_with_the_complete_authoritative_tuple"
            )
            continue
        if slots_for_pr and state != "pm-state:pm-review-pending":
            slot = slots_for_pr[0]
            print(
                f"PR_CAPTURE_SLOT_RELEASE_REQUIRED PR#{n} slot:{slot} reason=capture_blocker_has_slot_label "
                f"state={state or 'no_state'} issue=#{issue or 'unknown'} branch={branch} head={head[:10]} "
                f"command=gh\\ pr\\ edit\\ {n}\\ --remove-label\\ slot:{slot}\\ &&\\ UNSUPPORTED_LIFECYCLE_ACTION:reconcile-capacity"
            )
            continue

    if state == "pm-state:blocked-rework" and merge_state in {"BEHIND", "DIRTY"} and not has_label(pr, "pm-blocked:rebase"):
        print(
            f"PR_REBASE_REQUIRED PR#{n} reason=blocked_rework_blocked_by_merge_state "
            f"mergeStateStatus={merge_state} issue=#{issue or 'unknown'} branch={branch} "
            f"head={head[:10]} command=UNSUPPORTED_LIFECYCLE_ACTION:block-pr"
        )
        continue

    if has_label(pr, "pm-blocked:ci") and state != "pm-state:qa-passed-awaiting-ci":
        workflow_state, ci_run, e2e_run = workflow_summary(pr)
        if (
            workflow_state == "terminal_green"
            and check_state == "terminal_green"
            and merge_state == "CLEAN"
            and not draft
        ):
            print(
                f"PR_CI_LABEL_RECONCILE_REQUIRED PR#{n} reason={state or 'no_state'}_has_stale_ci_blocker_terminal_green_clean "
                f"issue=#{issue or 'unknown'} branch={branch} head={head[:10]} "
                f"success_count={success_count} skipped_count={skipped_count} "
                f"command=clear_pm-blocked:ci_then_run_current_state_transition"
            )
            continue
        # An active rerun remains CI-owned even if a prior PM review marker is
        # PASS. Do not fall through to pm-review-done while pm-blocked:ci is
        # intentionally holding the current-head CI watch.
        if workflow_state == "pending":
            continue
        if workflow_state == "pending_stale":
            stale = e2e_run if e2e_run["state"] == "pending_stale" else ci_run
            print(
                f"PR_CI_WATCH_STUCK_REQUIRED PR#{n} reason={state or 'no_state'}_ci_blocker_workflow_in_progress_stale "
                f"workflow={command_quote(stale['workflow'])} run={stale.get('run_id')} age_min={stale.get('age_min')} "
                f"head={head[:10]} command=inspect_or_cancel_rerun_required_workflow"
            )
            continue
        if workflow_state in {"terminal_bad", "terminal_cancelled", "missing_or_skipped"}:
            if state == "pm-state:blocked-rework":
                if pr_requires_fresh_capture_before_ci(n):
                    if remote_capture_only:
                        print(capture_local_transition_line(
                            n,
                            head,
                            issue,
                            branch,
                            "blocked_rework_ci_capture_required",
                        ))
                        continue
                    local_capture_ok, _local_capture_proof = local_capture_proof_status(n, head)
                    if not local_capture_ok:
                        print(capture_local_action_line(
                            n,
                            head,
                            issue,
                            branch,
                            "",
                            "local_capture_required",
                            "blocked_rework_ci_capture_required_local_proof_missing",
                            "local_capture_proof=missing",
                        ))
                        continue
                # A real pm-blocked:ci transition owns recovery even when a
                # current-head PM review marker also passes. In particular, a
                # GREY/post-test-tail verdict plus exact-head local proof must
                # reach the canonical rerun wrapper; falling through to the
                # generic blocked-rework review handler emits pm-review-done,
                # which the CI-start gate correctly rejects while this blocker
                # is active.
                print(ci_required_action_line(
                    pr,
                    f"blocked_rework_has_ci_blocker_latest_head_{workflow_state}",
                    workflow_state,
                    ci_run,
                    e2e_run,
                    issue,
                    branch,
                ))
                continue
            else:
                dep_state, dep_reason = ci_dependency_blocked_state(n, head)
                if dep_state == "blocked":
                    bad = workflow_bad_summary(ci_run, e2e_run)
                    _emit_watch, ack_key, ack_path = should_emit_dependency_watch(n, head, dep_reason, bad)
                    if dependency_visible_marker_ok(pr, ack_path):
                        print(
                            f"PR_CI_DEPENDENCY_WATCHING PR#{n} reason={state or 'no_state'}_ci_blocker_dependency_open_acknowledged "
                            f"{dep_reason} workflows={bad} label=pm-blocked:dependency watch_ack={ack_path} ack_key={ack_key} "
                            f"head={head[:10]} command=wait_dependency_then_retarget_rebase_and_cto_label_gated_wave"
                        )
                        continue
                    print(
                        f"PR_CI_DEPENDENCY_BLOCKED_REQUIRED PR#{n} reason={state or 'no_state'}_ci_blocker_dependency_open "
                        f"{dep_reason} workflows={bad} label_missing={str(not has_label(pr, 'pm-blocked:dependency')).lower()} "
                        f"watch_ack={ack_path} ack_key={ack_key} head={head[:10]} "
                        f"command=UNSUPPORTED_LIFECYCLE_ACTION:block-pr"
                    )
                    continue
                if dep_state == "unblocked":
                    print(
                        f"PR_DEPENDENCY_UNBLOCKED_REQUIRED PR#{n} reason=ci_blocker_dependency_terminal_outside_qa "
                        f"{dep_reason} head={head[:10]} command={dependency_unblocked_command(n, dep_reason)}"
                    )
                    continue
                print(ci_required_action_line(
                    pr,
                    f"{state or 'no_state'}_has_ci_blocker_latest_head_{workflow_state}",
                    workflow_state,
                    ci_run,
                    e2e_run,
                    issue,
                    branch,
                ))
                continue

    if state == "merge-ready":
        workflow_state, ci_run, e2e_run = workflow_summary(pr)
        invalid_reason = ""
        invalid_detail = ""
        proof_state, proof_detail = merge_ready_proof_status(n, head)
        if merge_state != "UNKNOWN":
            merge_unknown_watch_clear(n, head)
        if blockers:
            invalid_reason = "merge_ready_has_pm_blocker"
            invalid_detail = ",".join(blockers)
        elif draft:
            invalid_reason = "merge_ready_while_draft"
            invalid_detail = "draft=true"
        elif merge_state == "UNKNOWN":
            unknown_status, unknown_age = merge_unknown_watch_status(n, head, issue, branch)
            if unknown_status != "persistent":
                print(
                    f"PR_MERGE_READY_UNKNOWN_WATCHING PR#{n} reason=merge_ready_merge_state_unknown_transient "
                    f"detail=mergeStateStatus=UNKNOWN age_sec={unknown_age} grace_sec={merge_unknown_grace_seconds} "
                    f"issue=#{issue or 'unknown'} branch={branch} head={head[:10]} command=wait_for_github_mergeability_recompute"
                )
                continue
            invalid_reason = "merge_ready_merge_state_unknown_persistent"
            invalid_detail = f"mergeStateStatus=UNKNOWN age_sec={unknown_age}"
        elif merge_state != "CLEAN":
            invalid_reason = "merge_ready_merge_state_not_clean"
            invalid_detail = f"mergeStateStatus={merge_state}"
        else:
            workflow_guard_state, workflow_guard_detail = get_workflow_guard()
            if workflow_guard_state in {"bad", "pending", "missing_or_skipped"}:
                invalid_reason = f"merge_ready_workflow_changed_{workflow_guard_state}"
                invalid_detail = workflow_guard_detail
            elif workflow_state != "terminal_green":
                invalid_reason = "merge_ready_required_workflow_not_green"
                invalid_detail = workflow_bad_summary(ci_run, e2e_run)
            elif proof_state != "ok":
                invalid_reason = f"merge_ready_readiness_proof_{proof_state}"
                invalid_detail = proof_detail
        if invalid_reason:
            print(
                f"PR_MERGE_READY_INVALID_REQUIRED PR#{n} reason={invalid_reason} "
                f"detail={command_quote(invalid_detail or 'unknown')} issue=#{issue or 'unknown'} "
                f"branch={branch} head={str(pr.get('headRefOid') or '')[:10]} "
                f"command=demote_or_reconcile_ci_before_merge"
            )
            continue

    if drift:
        print(
            f"PR_STATE_RECONCILE_REQUIRED PR#{n} reason={drift} "
            f"issue=#{issue or 'unknown'} branch={branch} head={head[:10]} "
            f"command=UNSUPPORTED_LIFECYCLE_ACTION:reconcile-capacity"
        )
        continue

    if state == "merge-ready" and has_label(pr, "pm-state:merge-ready"):
        print(
            f"PR_STATE_RECONCILE_REQUIRED PR#{n} reason=legacy_pm_state_merge_ready_label "
            f"issue=#{issue or 'unknown'} branch={branch} head={head[:10]} "
            f"command=UNSUPPORTED_LIFECYCLE_ACTION:merge-ready"
        )
        continue

    if state == "merge-ready" and slots_for_pr:
        for slot in slots_for_pr:
            print(
                f"PR_SLOT_RELEASE_REQUIRED PR#{n} slot:{slot} reason=merge_ready_holds_slot "
                f"issue=#{issue or 'unknown'} branch={branch} remediation=release_slot_{slot}_via_Skill(direct-release)_with_the_complete_authoritative_tuple"
            )
        continue

    if state == "pm-state:qa-passed-awaiting-ci":
        active_slot = exact_mop_owner_for_pr(n, branch)
        fuzzy_slot = active_slot_for_pr(n, issue, branch)
        if not active_slot and fuzzy_slot:
            slot = fuzzy_slot.get("slot") or "unknown"
            if emit_incomplete_owner(fuzzy_slot):
                print(
                f"PR_STATE_RECONCILE_REQUIRED PR#{n} slot:{slot} reason=qa_awaiting_ci_mop_owner_tuple_incomplete "
                f"issue=#{issue or 'unknown'} branch={branch} head={head[:10]} "
                f"assignment_epoch={fuzzy_slot.get('assignment_epoch')} {owner_tokens(fuzzy_slot)} mop={slot_entry_summary(fuzzy_slot)} command=UNSUPPORTED_LIFECYCLE_ACTION:reconcile-capacity"
                )
            continue
        if active_slot:
            slot = active_slot.get("slot") or "unknown"
            print(
                f"PR_SLOT_RELEASE_BEFORE_CI_REQUIRED PR#{n} slot:{slot} reason=qa_passed_awaiting_ci_holds_dev_slot "
                f"issue=#{issue or 'unknown'} branch={branch} head={head[:10]} mop={slot_entry_summary(active_slot)} "
                f"remediation=release_slot_{slot}_via_Skill(direct-release)_with_the_complete_authoritative_tuple\\ &&\\ UNSUPPORTED_LIFECYCLE_ACTION:reconcile-capacity"
            )
            continue
        if slots_for_pr:
            slot = slots_for_pr[0]
            print(
                f"PR_SLOT_RELEASE_BEFORE_CI_REQUIRED PR#{n} slot:{slot} reason=qa_passed_awaiting_ci_has_slot_label "
                f"issue=#{issue or 'unknown'} branch={branch} head={head[:10]} "
                f"command=gh\\ pr\\ edit\\ {n}\\ --repo\\ {gh_repo}\\ --remove-label\\ slot:{slot}\\ &&\\ UNSUPPORTED_LIFECYCLE_ACTION:reconcile-capacity"
            )
            continue

    if state == "pm-state:qa-passed-awaiting-ci" and draft:
        print(
            f"PR_READY_BEFORE_CI_REQUIRED PR#{n} reason=qa_awaiting_ci_while_draft "
            f"issue=#{issue or 'unknown'} branch={branch} head={str(pr.get('headRefOid') or '')[:10]} "
            f"command=gh_pr_ready_then_retrigger_label_gated_ci_if_required"
        )
        continue

    # Only genuine rebase/conflict states need a rebase action here. UNSTABLE
    # (pending OR failing CI) / BLOCKED / UNKNOWN fall through to the CI-aware
    # handlers below (~999 / ~1092), which silently watch in-progress CI, flag
    # terminal_bad via PR_CI_CLASSIFICATION_REQUIRED, and promote terminal_green.
    # Previously `merge_state != "CLEAN"` fired PR_REBASE_REQUIRED on every
    # qa-passed-awaiting-ci PR whose CI was merely in-flight (UNSTABLE) — a
    # recurring false-positive that re-fired the pm-stop sweep each Stop while
    # CI ran (PR #5770, 2026-06-24).
    if state == "pm-state:qa-passed-awaiting-ci" and merge_state in {"BEHIND", "DIRTY"}:
        print(
            f"PR_REBASE_REQUIRED PR#{n} reason=qa_awaiting_ci_blocked_by_merge_state "
            f"mergeStateStatus={merge_state} issue=#{issue or 'unknown'} branch={branch} "
            f"head={str(pr.get('headRefOid') or '')[:10]} command=UNSUPPORTED_LIFECYCLE_ACTION:block-pr"
        )
        continue

    if state == "pm-state:qa-passed-awaiting-ci":
        head = str(pr.get("headRefOid") or "")
        capture_marker = capture_gated_phase_a_marker(n, head)
        if capture_marker:
            local_capture_ok, _local_capture_proof = local_capture_proof_status(n, head)
            if not local_capture_ok and not has_label(pr, "pm-blocked:capture"):
                print(
                    f"PR_PM_REVIEW_CAPTURE_BYPASS_REQUIRED PR#{n} reason=qa_awaiting_ci_started_before_required_local_capture "
                    f"scope=phase-a marker={capture_marker} capture_status=local_capture_proof_missing "
                    f"issue=#{issue or 'unknown'} branch={branch} head={head[:10]} "
                    f"command=UNSUPPORTED_LIFECYCLE_ACTION:block-pr"
                )
                continue

    if state == "pm-state:qa-passed-awaiting-ci" and has_label(pr, "pm-blocked:capture"):
        head = str(pr.get("headRefOid") or "")
        if remote_capture_only:
            print(capture_local_transition_line(
                n,
                head,
                issue,
                branch,
                "qa_awaiting_ci_capture_blocker",
            ))
            continue
        local_capture_ok, _local_capture_proof = local_capture_proof_status(n, head)
        if local_capture_ok:
            print(capture_local_action_line(
                n,
                head,
                issue,
                branch,
                "",
                "local_capture_proof",
                "qa_awaiting_ci_capture_blocker_local_proof_ready",
                "local_capture_proof=present",
            ))
            continue
        print(capture_local_action_line(
            n,
            head,
            issue,
            branch,
            "",
            "local_capture_required",
            "qa_awaiting_ci_capture_blocker_local_proof_missing",
            "local_capture_proof=missing",
        ))
        continue

    if state == "pm-state:qa-passed-awaiting-ci" and has_label(pr, "pm-blocked:ci"):
        head = str(pr.get("headRefOid") or "")
        capture_verdict = latest_capture_verdict(n, head)
        if capture_verdict:
            local_capture_ok, _local_capture_proof = local_capture_proof_status(n, head)
            if not local_capture_ok:
                print(
                    f"PR_CAPTURE_LABEL_RECONCILE_REQUIRED PR#{n} reason=capture_verdict_has_generic_ci_blocker_local_proof_missing "
                    f"classification={capture_verdict.get('classification')} local_capture_proof=missing "
                    f"head={head[:10]} issue=#{issue or 'unknown'} branch={branch} "
                    f"command=UNSUPPORTED_LIFECYCLE_ACTION:block-pr"
                )
                continue
            watch_runs = [str(x) for x in (capture_verdict.get("watch_runs") or []) if str(x)]
            capture_run = latest_watch_run_id(watch_runs)
            capture_status = run_status(capture_run) if capture_run else {}
            capture_run_status = capture_status.get("status") or "missing"
            capture_conclusion = capture_status.get("conclusion") or ""
            # Once capture is green, pm-blocked:ci is correct: PM is watching
            # the original CI/E2E rerun. Before that, the distinct capture
            # blocker prevents the PR from looking like uninvestigated CI.
            if not (capture_run_status == "completed" and capture_conclusion == "success"):
                print(
                    f"PR_CAPTURE_LABEL_RECONCILE_REQUIRED PR#{n} reason=capture_verdict_has_generic_ci_blocker "
                    f"classification={capture_verdict.get('classification')} capture_run={capture_run or 'missing'} "
                    f"capture_status={capture_run_status} capture_conclusion={capture_conclusion or 'unknown'} "
                    f"head={head[:10]} issue=#{issue or 'unknown'} branch={branch} "
                    f"command=UNSUPPORTED_LIFECYCLE_ACTION:block-pr"
                )
                continue
        workflow_state, ci_run, e2e_run = workflow_summary(pr)
        if workflow_state == "pending":
            continue
        if workflow_state == "pending_stale":
            stale = e2e_run if e2e_run["state"] == "pending_stale" else ci_run
            print(
                f"PR_CI_WATCH_STUCK_REQUIRED PR#{n} reason=qa_awaiting_ci_required_workflow_in_progress_stale "
                f"workflow={command_quote(stale['workflow'])} run={stale.get('run_id')} age_min={stale.get('age_min')} "
                f"head={str(pr.get('headRefOid') or '')[:10]} command=inspect_or_cancel_rerun_required_workflow"
            )
            continue
        if workflow_state == "terminal_cancelled":
            print(ci_required_action_line(
                pr,
                "qa_awaiting_ci_required_workflow_cancelled_with_ci_blocker",
                workflow_state,
                ci_run,
                e2e_run,
                issue,
                branch,
            ))
            continue
        if workflow_state == "terminal_bad":
            ci_hold = ci_hold_watch_state(n, str(pr.get("headRefOid") or ""), ci_run, e2e_run)
            if ci_hold:
                print(
                    f"PR_CI_HOLD_WATCHING PR#{n} reason=classified_same_head_ci_hold "
                    f"classification={command_quote(ci_hold['classification'])} failed_run={ci_hold['failed_run']} "
                    f"baseline_run={ci_hold['baseline_run']} proof={command_quote(ci_hold['proof'])} marker={ci_hold['marker']} "
                    f"head={str(pr.get('headRefOid') or '')[:10]} "
                    f"command=wait_for_external_blocker_then_rerun_original_workflow_without_label_toggle"
                )
                continue
            active_ci_reason = ci_investigation_active_reason(n, str(pr.get("headRefOid") or ""))
            if active_ci_reason:
                continue
            dep_state, dep_reason = ci_dependency_blocked_state(n, str(pr.get("headRefOid") or ""))
            if dep_state == "blocked":
                bad = workflow_bad_summary(ci_run, e2e_run)
                emit_watch, ack_key, ack_path = should_emit_dependency_watch(
                    n, str(pr.get("headRefOid") or ""), dep_reason, bad
                )
                if dependency_visible_marker_ok(pr, ack_path):
                    print(
                        f"PR_CI_DEPENDENCY_WATCHING PR#{n} reason=qa_awaiting_ci_ci_blocker_dependency_open_acknowledged "
                        f"{dep_reason} workflows={bad} label=pm-blocked:dependency watch_ack={ack_path} ack_key={ack_key} "
                        f"head={str(pr.get('headRefOid') or '')[:10]} "
                        f"command=wait_dependency_then_retarget_rebase_and_cto_label_gated_wave"
                    )
                    continue
                print(
                    f"PR_CI_DEPENDENCY_BLOCKED_REQUIRED PR#{n} reason=qa_awaiting_ci_ci_blocker_dependency_open "
                    f"{dep_reason} workflows={bad} label_missing={str(not has_label(pr, 'pm-blocked:dependency')).lower()} watch_ack={ack_path} ack_key={ack_key} "
                    f"head={str(pr.get('headRefOid') or '')[:10]} "
                    f"command=UNSUPPORTED_LIFECYCLE_ACTION:block-pr"
                )
                continue
            if dep_state == "unblocked":
                print(
                    f"PR_DEPENDENCY_UNBLOCKED_REQUIRED PR#{n} reason=ci_dependency_blocker_terminal "
                    f"{dep_reason} head={str(pr.get('headRefOid') or '')[:10]} "
                    f"command={dependency_unblocked_command(n, dep_reason)}"
                )
                continue
            print(ci_required_action_line(
                pr,
                "qa_awaiting_ci_ci_blocker_needs_classification",
                workflow_state,
                ci_run,
                e2e_run,
                issue,
                branch,
            ))
            continue
        if workflow_state == "terminal_green" and merge_state == "CLEAN" and not draft:
            workflow_guard_state, workflow_guard_detail = get_workflow_guard()
            if workflow_guard_state in {"bad", "pending", "missing_or_skipped"}:
                print(
                    f"PR_CI_CLASSIFICATION_REQUIRED PR#{n} reason=qa_awaiting_ci_workflow_changed_{workflow_guard_state} "
                    f"detail={command_quote(workflow_guard_detail or 'unknown')} "
                    f"head={str(pr.get('headRefOid') or '')[:10]} "
                    f"command=verify_changed_workflow_jobs_or_get_cto_waiver_before_merge_ready"
                )
                continue
            print(
                f"PR_CI_LABEL_RECONCILE_REQUIRED PR#{n} reason=qa_awaiting_ci_has_stale_ci_blocker_terminal_green_clean "
                f"issue=#{issue or 'unknown'} branch={branch} head={str(pr.get('headRefOid') or '')[:10]} "
                f"success_count={success_count} skipped_count={skipped_count} "
                f"command=clear_pm-blocked:ci_then_run_pm_readiness_contract_or_cto_hold"
            )
            continue
        if workflow_state == "missing_or_skipped" and merge_state == "CLEAN" and not draft:
            dep_state, dep_reason = ci_dependency_blocked_state(n, str(pr.get("headRefOid") or ""))
            if dep_state == "blocked":
                bad = workflow_bad_summary(ci_run, e2e_run)
                emit_watch, ack_key, ack_path = should_emit_dependency_watch(
                    n, str(pr.get("headRefOid") or ""), dep_reason, bad
                )
                if dependency_visible_marker_ok(pr, ack_path):
                    print(
                        f"PR_CI_DEPENDENCY_WATCHING PR#{n} reason=qa_awaiting_ci_required_workflow_missing_or_skipped_dependency_open_acknowledged "
                        f"{dep_reason} workflows={bad} label=pm-blocked:dependency watch_ack={ack_path} ack_key={ack_key} "
                        f"head={str(pr.get('headRefOid') or '')[:10]} "
                        f"command=wait_dependency_then_retarget_rebase_and_cto_label_gated_wave"
                    )
                    continue
                print(
                    f"PR_CI_DEPENDENCY_BLOCKED_REQUIRED PR#{n} reason=qa_awaiting_ci_required_workflow_missing_or_skipped_dependency_open "
                    f"{dep_reason} workflows={bad} label_missing={str(not has_label(pr, 'pm-blocked:dependency')).lower()} watch_ack={ack_path} ack_key={ack_key} "
                    f"head={str(pr.get('headRefOid') or '')[:10]} "
                    f"command=UNSUPPORTED_LIFECYCLE_ACTION:block-pr"
                )
                continue
            if dep_state == "unblocked":
                print(
                    f"PR_DEPENDENCY_UNBLOCKED_REQUIRED PR#{n} reason=missing_or_skipped_dependency_blocker_terminal "
                    f"{dep_reason} head={str(pr.get('headRefOid') or '')[:10]} "
                    f"command={dependency_unblocked_command(n, dep_reason)}"
                )
                continue
            print(ci_required_action_line(
                pr,
                "qa_awaiting_ci_required_workflow_missing_or_skipped_with_ci_blocker",
                workflow_state,
                ci_run,
                e2e_run,
                issue,
                branch,
            ))
            continue
        bad = ",".join(bad_checks) if bad_checks else "none"
        print(
            f"PR_CI_LABEL_RECONCILE_REQUIRED PR#{n} reason=qa_awaiting_ci_has_ci_blocker "
            f"checks={check_state} bad={bad} head={str(pr.get('headRefOid') or '')[:10]} "
            f"command=inspect_stale_pm-blocked:ci_then_clear_or_rerun_without_label_toggle"
        )
        continue

    if state == "pm-state:qa-passed-awaiting-ci" and not has_label(pr, "pm-blocked:ci"):
        workflow_state, ci_run, e2e_run = workflow_summary(pr)
        if workflow_state == "pending":
            continue
        if workflow_state == "pending_stale":
            stale = e2e_run if e2e_run["state"] == "pending_stale" else ci_run
            print(
                f"PR_CI_WATCH_STUCK_REQUIRED PR#{n} reason=qa_awaiting_ci_required_workflow_in_progress_stale "
                f"workflow={command_quote(stale['workflow'])} run={stale.get('run_id')} age_min={stale.get('age_min')} "
                f"head={str(pr.get('headRefOid') or '')[:10]} command=inspect_or_cancel_rerun_required_workflow"
            )
            continue
        if workflow_state == "terminal_cancelled":
            print(ci_required_action_line(
                pr,
                "qa_awaiting_ci_required_workflow_cancelled_without_ci_blocker",
                workflow_state,
                ci_run,
                e2e_run,
                issue,
                branch,
            ))
            continue
        if workflow_state == "terminal_bad":
            if has_label(pr, "pm-blocked:dependency"):
                dep_state, dep_reason = ci_dependency_blocked_state(n, str(pr.get("headRefOid") or ""))
                bad = workflow_bad_summary(ci_run, e2e_run)
                if dep_state == "blocked":
                    emit_watch, ack_key, ack_path = should_emit_dependency_watch(
                        n, str(pr.get("headRefOid") or ""), dep_reason, bad
                    )
                    if dependency_visible_marker_ok(pr, ack_path):
                        print(
                            f"PR_CI_DEPENDENCY_WATCHING PR#{n} reason=qa_awaiting_ci_dependency_open_acknowledged "
                            f"{dep_reason} workflows={bad} label=pm-blocked:dependency watch_ack={ack_path} ack_key={ack_key} "
                            f"head={str(pr.get('headRefOid') or '')[:10]} "
                            f"command=wait_dependency_then_retarget_rebase_and_cto_label_gated_wave"
                        )
                        continue
                if dep_state == "unblocked":
                    print(
                        f"PR_DEPENDENCY_UNBLOCKED_REQUIRED PR#{n} reason=ci_dependency_blocker_terminal_without_ci_label "
                        f"{dep_reason} head={str(pr.get('headRefOid') or '')[:10]} "
                        f"command={dependency_unblocked_command(n, dep_reason)}"
                    )
                    continue
                print(
                    f"PR_CI_DEPENDENCY_BLOCKED_REQUIRED PR#{n} reason=qa_awaiting_ci_dependency_label_missing_or_invalid_proof "
                    f"workflows={bad} label_missing=false head={str(pr.get('headRefOid') or '')[:10]} "
                    f"command=UNSUPPORTED_LIFECYCLE_ACTION:block-pr"
                )
                continue
            print(ci_required_action_line(
                pr,
                "qa_awaiting_ci_terminal_bad_without_blocker",
                workflow_state,
                ci_run,
                e2e_run,
                issue,
                branch,
            ))
            continue
        if workflow_state == "terminal_green" and merge_state == "CLEAN" and not draft:
            workflow_guard_state, workflow_guard_detail = get_workflow_guard()
            if workflow_guard_state in {"bad", "pending", "missing_or_skipped"}:
                print(
                    f"PR_CI_CLASSIFICATION_REQUIRED PR#{n} reason=qa_awaiting_ci_workflow_changed_{workflow_guard_state} "
                    f"detail={command_quote(workflow_guard_detail or 'unknown')} "
                    f"head={str(pr.get('headRefOid') or '')[:10]} "
                    f"command=verify_changed_workflow_jobs_or_get_cto_waiver_before_merge_ready"
                )
                continue
            # Independent readiness consumer gates. The consumer (bash
            # PR_READY_PROMOTION_REQUIRED processor below) runs the existing
            # readiness contract and the canonical typed transitions only when
            # ready_gate=pass: current-head Phase-A/QA proof at the exact head,
            # or an already-valid READY_PACKET proof (a previous readiness
            # contract run with review_provenance ok). Missing or stale
            # current-head review proof fails closed into the PM obligation.
            phase_a_status, _phase_a_path = opus_marker_status(n, str(pr.get("headRefOid") or ""), "phase-a")
            proof_state, _proof_detail = merge_ready_proof_status(n, str(pr.get("headRefOid") or ""))
            if phase_a_status == "pass" or proof_state == "ok":
                ready_gate = "pass"
                ready_gate_reason = "none"
            else:
                ready_gate = "blocked"
                ready_gate_reason = f"phase_a_{phase_a_status}_proof_{proof_state}"
            print(
                f"PR_READY_PROMOTION_REQUIRED PR#{n} reason=qa_awaiting_ci_terminal_green_clean "
                f"issue=#{issue or 'unknown'} branch={branch} head={str(pr.get('headRefOid') or '')} "
                f"success_count={success_count} skipped_count={skipped_count} "
                f"phase_a={phase_a_status} proof_state={proof_state} "
                f"ready_gate={ready_gate} ready_gate_reason={ready_gate_reason} "
                f"command=run_pm_readiness_contract_then_UNSUPPORTED_LIFECYCLE_ACTION:validate-ready-proof"
            )
            continue
        if workflow_state == "missing_or_skipped" and merge_state == "CLEAN" and not draft:
            if has_label(pr, "pm-blocked:dependency"):
                dep_state, dep_reason = ci_dependency_blocked_state(n, str(pr.get("headRefOid") or ""))
                bad = workflow_bad_summary(ci_run, e2e_run)
                if dep_state == "blocked":
                    emit_watch, ack_key, ack_path = should_emit_dependency_watch(
                        n, str(pr.get("headRefOid") or ""), dep_reason, bad
                    )
                    if dependency_visible_marker_ok(pr, ack_path):
                        print(
                            f"PR_CI_DEPENDENCY_WATCHING PR#{n} reason=qa_awaiting_ci_missing_or_skipped_dependency_open_acknowledged "
                            f"{dep_reason} workflows={bad} label=pm-blocked:dependency watch_ack={ack_path} ack_key={ack_key} "
                            f"head={str(pr.get('headRefOid') or '')[:10]} "
                            f"command=wait_dependency_then_retarget_rebase_and_cto_label_gated_wave"
                        )
                        continue
                if dep_state == "unblocked":
                    print(
                        f"PR_DEPENDENCY_UNBLOCKED_REQUIRED PR#{n} reason=missing_or_skipped_dependency_blocker_terminal_without_ci_label "
                        f"{dep_reason} head={str(pr.get('headRefOid') or '')[:10]} "
                        f"command={dependency_unblocked_command(n, dep_reason)}"
                    )
                    continue
                print(
                    f"PR_CI_DEPENDENCY_BLOCKED_REQUIRED PR#{n} reason=qa_awaiting_ci_missing_or_skipped_dependency_label_missing_or_invalid_proof "
                    f"workflows={bad} label_missing=false head={str(pr.get('headRefOid') or '')[:10]} "
                    f"command=UNSUPPORTED_LIFECYCLE_ACTION:block-pr"
                )
                continue
            print(ci_required_action_line(
                pr,
                "qa_awaiting_ci_required_workflow_missing_or_skipped",
                workflow_state,
                ci_run,
                e2e_run,
                issue,
                branch,
            ))
            continue


    if state == "pm-state:pm-review-pending":
        if has_label(pr, "pm-blocked:capture"):
            active_slot = exact_mop_owner_for_pr(n, branch)
            fuzzy_slot = active_slot_for_pr(n, issue, branch)
            if not active_slot and fuzzy_slot:
                slot = fuzzy_slot.get("slot") or "unknown"
                if emit_incomplete_owner(fuzzy_slot):
                    print(
                    f"PR_STATE_RECONCILE_REQUIRED PR#{n} slot:{slot} reason=pm_review_capture_mop_owner_tuple_incomplete "
                    f"issue=#{issue or 'unknown'} branch={branch} head={str(pr.get('headRefOid') or '')[:10]} "
                    f"assignment_epoch={fuzzy_slot.get('assignment_epoch')} {owner_tokens(fuzzy_slot)} mop={slot_entry_summary(fuzzy_slot)} command=UNSUPPORTED_LIFECYCLE_ACTION:reconcile-capacity"
                    )
                continue
            if active_slot:
                slot = active_slot.get("slot") or "unknown"
                print(
                    f"PR_CAPTURE_SLOT_RELEASE_REQUIRED PR#{n} slot:{slot} reason=capture_watch_holds_dev_slot "
                    f"issue=#{issue or 'unknown'} branch={branch} head={str(pr.get('headRefOid') or '')[:10]} "
                    f"mop={slot_entry_summary(active_slot)} "
                    f"remediation=release_slot_{slot}_via_Skill(direct-release)_with_the_complete_authoritative_tuple"
                )
                continue
            if slots_for_pr:
                slot = slots_for_pr[0]
                print(
                    f"PR_CAPTURE_SLOT_RELEASE_REQUIRED PR#{n} slot:{slot} reason=capture_watch_has_slot_label "
                    f"issue=#{issue or 'unknown'} branch={branch} head={str(pr.get('headRefOid') or '')[:10]} "
                    f"command=gh\\ pr\\ edit\\ {n}\\ --remove-label\\ slot:{slot}\\ &&\\ UNSUPPORTED_LIFECYCLE_ACTION:reconcile-capacity"
                )
                continue
            capture_head = str(pr.get("headRefOid") or "")
            pending_event = pending_slot_ready_event_for_pr(n, capture_head)
            if remote_capture_only:
                print(capture_local_transition_line(
                    n,
                    capture_head,
                    issue,
                    branch,
                    "pm_review_pending_capture_before_ci",
                ))
                continue
            local_capture_ok, local_capture_proof = local_capture_proof_status(n, capture_head)
            if local_capture_ok and pending_event:
                print(
                    f"PR_CAPTURE_COMPLETE_SLOT_READY_REQUIRED PR#{n} reason=capture_before_ci_local_proof_pending_slot_ready "
                    f"local_capture_proof={local_capture_proof} issue=#{issue or 'unknown'} branch={branch} "
                    f"head={str(pr.get('headRefOid') or '')[:10]} "
                    f"remediation=consume_the_exact_local_capture_result\\ &&\\ gh\\ pr\\ edit\\ {n}\\ --remove-label\\ pm-blocked:capture\\ &&\\ UNSUPPORTED_LIFECYCLE_ACTION:slot-ready"
                )
                continue
            if local_capture_ok:
                print(
                    f"PR_CAPTURE_COMPLETE_PM_REVIEW_DONE_REQUIRED PR#{n} reason=capture_before_ci_local_proof_no_slot_packet "
                    f"local_capture_proof={local_capture_proof} issue=#{issue or 'unknown'} branch={branch} "
                    f"head={str(pr.get('headRefOid') or '')[:10]} "
                    f"remediation=consume_the_exact_local_capture_result\\ &&\\ gh\\ pr\\ edit\\ {n}\\ --remove-label\\ pm-blocked:capture\\ &&\\ UNSUPPORTED_LIFECYCLE_ACTION:pm-review-done"
                )
                continue
            capture_green, capture_detail = current_head_capture_green(n, capture_head)
            if capture_green and pending_event:
                print(
                    f"PR_CAPTURE_LOCAL_REQUIRED PR#{n} reason=capture_before_ci_remote_green_pending_slot_ready_local_proof_missing "
                    f"event={pending_event} {capture_detail} issue=#{issue or 'unknown'} branch={branch} "
                    f"head={str(pr.get('headRefOid') or '')[:10]} "
                    f"remediation=run_the_existing_local_capture_workflow_for_this_exact_head"
                )
                continue
            if capture_green:
                print(
                    f"PR_CAPTURE_LOCAL_REQUIRED PR#{n} reason=capture_before_ci_remote_green_no_slot_packet_local_proof_missing "
                    f"{capture_detail} issue=#{issue or 'unknown'} branch={branch} "
                    f"head={str(pr.get('headRefOid') or '')[:10]} "
                    f"remediation=run_the_existing_local_capture_workflow_for_this_exact_head"
                )
                continue
            if "status=completed" in capture_detail and "conclusion=success" not in capture_detail:
                run_match = re.search(r"capture_run=([^ ]+)", capture_detail)
                conclusion_match = re.search(r"conclusion=([^ ]+)", capture_detail)
                print(capture_local_action_line(
                    n,
                    capture_head,
                    issue,
                    branch,
                    run_match.group(1) if run_match else "",
                    conclusion_match.group(1) if conclusion_match else "unknown",
                    "pm_review_pending_capture_before_ci",
                    capture_detail,
                ))
                continue
            local_line = stale_capture_local_action_line(
                n,
                capture_head,
                issue,
                branch,
                "pm_review_pending_capture_before_ci",
                capture_detail,
            )
            if local_line:
                print(local_line)
                continue
            print(
                f"PR_CAPTURE_BEFORE_CI_WATCH_REQUIRED PR#{n} reason=pm_review_pending_capture_before_ci "
                f"capture_status={capture_detail} pending_event={pending_event or 'missing'} "
                f"issue=#{issue or 'unknown'} branch={branch} head={str(pr.get('headRefOid') or '')[:10]} "
                f"command=trigger_or_watch_fresh_head_capture_before_qa_passed_awaiting_ci"
            )
            continue
        workflow_state, ci_run, e2e_run = workflow_summary(pr)
        active_slot = exact_mop_owner_for_pr(n, branch)
        fuzzy_slot = active_slot_for_pr(n, issue, branch)
        if not active_slot and fuzzy_slot:
            slot = fuzzy_slot.get("slot") or "unknown"
            if emit_incomplete_owner(fuzzy_slot):
                print(
                f"PR_STATE_RECONCILE_REQUIRED PR#{n} slot:{slot} reason=pm_review_mop_owner_tuple_incomplete "
                f"issue=#{issue or 'unknown'} branch={branch} head={str(pr.get('headRefOid') or '')[:10]} "
                f"assignment_epoch={fuzzy_slot.get('assignment_epoch')} {owner_tokens(fuzzy_slot)} mop={slot_entry_summary(fuzzy_slot)} command=UNSUPPORTED_LIFECYCLE_ACTION:reconcile-capacity"
                )
            continue
        if active_slot:
            slot = active_slot.get("slot") or "unknown"
            print(
                f"PR_PM_REVIEW_SLOT_RELEASE_REQUIRED PR#{n} slot:{slot} reason=pm_review_pending_holds_dev_slot "
                f"issue=#{issue or 'unknown'} branch={branch} head={str(pr.get('headRefOid') or '')[:10]} "
                f"mop={slot_entry_summary(active_slot)} "
                f"command=cd\\ {CONTROL_PLANE_ROOT}\\ &&\\ PYTHONPATH={CONTROL_PLANE_ROOT}\\ python3\\ -m\\ {FAMILY2_MODULE}\\ --transition-type\\ pm_review\\ --slot\\ {slot}\\ --issue\\ {issue}\\ --pr\\ {n}"
            )
            continue
        # PM_REVIEW_DONE_TERMINAL_RECEIPT_V1 consumer: a head-bound terminal
        # review-PASS receipt at the exact head means pm-review-done already
        # validated this review. Never schedule another same-head review and
        # never blindly re-invoke pm-review-done when the receipt carries a
        # typed blocked_after_review class; surface the typed blocker/wake (or
        # the idempotent resume) instead.
        if pm_review_done_receipt_line(n, str(pr.get("headRefOid") or ""), issue, branch, "pm_review_pending"):
            continue
        scope, meta = pm_review_scope(pr, workflow_state, merge_state, draft)
        marker_status, marker_path = opus_marker_status(n, str(pr.get("headRefOid") or ""), scope)
        if marker_status == "pass":
            if scope == "phase-a":
                # Rajiv thread 1786811168.455449 ts 1786811850.717079:
                # affected-test proof is retired — never a CI-start
                # prerequisite. Keep the value only as an optional diagnostic.
                _affected_ok, affected_test_proof = local_preflight_proof_status(
                    n, str(pr.get("headRefOid") or "")
                )
                if workflow_state in {"pending", "pending_stale"}:
                    print(
                        f"PR_PM_REVIEW_WORKFLOW_DRAIN_REQUIRED PR#{n} reason=pm_review_phase_a_passed_current_head_workflow_active "
                        f"scope=phase-a marker={marker_path} affected_test_proof={affected_test_proof} "
                        f"workflows={workflow_bad_summary(ci_run, e2e_run)} "
                        f"issue=#{issue or 'unknown'} branch={branch} head={str(pr.get('headRefOid') or '')} "
                        f"command=wait_for_exact_head_workflows_terminal_then_rerun_pr-state-sweep"
                    )
                    continue
                if pm_review_marker_capture_gated(marker_path) and pr_requires_fresh_capture_before_ci(n):
                    if remote_capture_only:
                        print(capture_local_transition_line(
                            n,
                            str(pr.get("headRefOid") or ""),
                            issue,
                            branch,
                            "pm_review_phase_a_capture_gated",
                        ))
                        continue
                    local_capture_ok, local_capture_proof = local_capture_proof_status(n, str(pr.get("headRefOid") or ""))
                    if local_capture_ok:
                        print(
                            f"PR_PM_REVIEW_CAPTURE_COMPLETE_REQUIRED PR#{n} reason=pm_review_phase_a_local_capture_proof "
                            f"scope=phase-a marker={marker_path} local_capture_proof={local_capture_proof} "
                            f"affected_test_proof={affected_test_proof} "
                            f"issue=#{issue or 'unknown'} branch={branch} head={str(pr.get('headRefOid') or '')[:10]} "
                            f"remediation=consume_the_exact_local_capture_result\\ &&\\ gh\\ pr\\ edit\\ {n}\\ --remove-label\\ pm-blocked:capture\\ &&\\ UNSUPPORTED_LIFECYCLE_ACTION:pm-review-done"
                        )
                        continue
                    capture_green, capture_detail = current_head_capture_green(n, str(pr.get("headRefOid") or ""))
                    if capture_green:
                        print(
                            f"PR_CAPTURE_LOCAL_REQUIRED PR#{n} reason=pm_review_phase_a_remote_capture_green_local_proof_missing "
                            f"scope=phase-a marker={marker_path} {capture_detail} "
                            f"issue=#{issue or 'unknown'} branch={branch} head={str(pr.get('headRefOid') or '')[:10]} "
                            f"remediation=run_the_existing_local_capture_workflow_for_this_exact_head"
                        )
                        continue
                    if not has_label(pr, "pm-blocked:capture"):
                        print(
                            f"PR_PM_REVIEW_CAPTURE_REQUIRED PR#{n} reason=pm_review_phase_a_capture_gated_before_ci "
                            f"scope=phase-a marker={marker_path} capture_status={capture_detail} "
                            f"issue=#{issue or 'unknown'} branch={branch} head={str(pr.get('headRefOid') or '')[:10]} "
                            f"command=UNSUPPORTED_LIFECYCLE_ACTION:block-pr"
                        )
                        continue
                    if "status=completed" in capture_detail and "conclusion=success" not in capture_detail:
                        run_match = re.search(r"capture_run=([^ ]+)", capture_detail)
                        conclusion_match = re.search(r"conclusion=([^ ]+)", capture_detail)
                        print(capture_local_action_line(
                            n,
                            str(pr.get("headRefOid") or ""),
                            issue,
                            branch,
                            run_match.group(1) if run_match else "",
                            conclusion_match.group(1) if conclusion_match else "unknown",
                            "pm_review_phase_a_capture_gated_watch",
                            capture_detail,
                        ))
                        continue
                    local_line = stale_capture_local_action_line(
                        n,
                        str(pr.get("headRefOid") or ""),
                        issue,
                        branch,
                        "pm_review_phase_a_capture_gated_watch",
                        capture_detail,
                    )
                    if local_line:
                        print(local_line)
                        continue
                    print(
                        f"PR_PM_REVIEW_CAPTURE_REQUIRED PR#{n} reason=pm_review_phase_a_capture_gated_watch "
                        f"scope=phase-a marker={marker_path} capture_status={capture_detail} "
                        f"issue=#{issue or 'unknown'} branch={branch} head={str(pr.get('headRefOid') or '')[:10]} "
                        f"command=trigger_fresh_head_capture_and_update_ci_verdict_watch_runs"
                    )
                    continue
                if workflow_state == "terminal_bad" and ci_verdict_requires_rework(latest_ci_verdict(n, str(pr.get("headRefOid") or ""))):
                    print(ci_required_action_line(
                        pr,
                        "pm_review_phase_a_passed_but_ci_verdict_requires_rework",
                        workflow_state,
                        ci_run,
                        e2e_run,
                        issue,
                        branch,
                    ))
                    continue
                if not slots_for_pr:
                    # Off-slot origin (Rescues/off-slot flow): pm-review-pending
                    # records a genuinely-owed review whose verdict the merge
                    # gate owns. Never advance it as stale legacy from the sweep.
                    continue
                print(
                    f"PR_PM_REVIEW_COMPLETE_REQUIRED PR#{n} reason=pm_review_phase_a_passed "
                    f"scope=phase-a marker={marker_path} affected_test_proof={affected_test_proof} "
                    f"issue=#{issue or 'unknown'} branch={branch} "
                    f"head={str(pr.get('headRefOid') or '')} "
                    f"command=cd\\ {CONTROL_PLANE_ROOT}\\ &&\\ PYTHONPATH={CONTROL_PLANE_ROOT}\\ python3\\ -m\\ {FAMILY2_MODULE}\\ --transition-type\\ pm_review\\ --issue\\ {issue or 'unknown'}\\ --pr\\ {n}\\ --review-evidence\\ {marker_path}"
                )
                continue
            if workflow_state in {"pending", "pending_stale"}:
                continue
            print(ci_required_action_line(
                pr,
                "pm_review_phase_a_passed_but_ci_not_green",
                workflow_state,
                ci_run,
                e2e_run,
                issue,
                branch,
            ))
            continue

        if marker_status == "blocked":
            print(
                f"PR_PM_REVIEW_REQUIRED PR#{n} reason=pm_claude_blocked_apply_rework_state "
                f"scope={scope} marker={marker_path} issue=#{issue or 'unknown'} branch={branch} "
                f"head={str(pr.get('headRefOid') or '')[:10]} "
                f"command=UNSUPPORTED_LIFECYCLE_ACTION:block-pr"
            )
            continue

        if pm_review_pending_is_stale(meta, str(pr.get("headRefOid") or "")):
            age = age_minutes_from_iso(meta.get("created_at"))
            age_part = f" age_min={age}" if age is not None else " age_min=unknown"
            print(
                f"PR_PM_REVIEW_REQUIRED PR#{n} reason=pm_review_pending_stale_or_missing_marker "
                f"scope={scope} marker_status={marker_status} marker={marker_path}{age_part} "
                f"issue=#{issue or 'unknown'} branch={branch} head={str(pr.get('headRefOid') or '')[:10]} "
                f"command=run_pm_claude_code_review_then_UNSUPPORTED_LIFECYCLE_ACTION:pm-review-done_or_rework"
            )
            continue
        continue

    if state == "pm-state:rescope-required":
        marker = rescope_marker(n, str(pr.get("headRefOid") or ""))
        if marker:
            head_oid = str(pr.get("headRefOid") or "")
            if "status=resolved" in marker and "decision=override_with_evidence" in marker and "stale=false" in marker:
                plan = f"/tmp/affected-test-plan-{n}-{head_oid}.json"
                print(
                    f"PR_OVERRIDE_VERIFICATION_REQUIRED PR#{n} reason=resolved_override_requires_current_head_proof "
                    f"{marker} affected_test_plan={plan} issue=#{issue or 'unknown'} branch={branch} "
                    f"head={head_oid[:10]} "
                    f"command=cd\\ {CONTROL_PLANE_ROOT}\\ &&\\ PYTHONPATH={CONTROL_PLANE_ROOT}\\ python3\\ -m\\ {FAMILY2_MODULE}\\ --transition-type\\ pm_review\\ --issue\\ {issue or 'unknown'}\\ --pr\\ {n}\\ --review-evidence\\ {marker_path}"
                )
            else:
                print(
                    f"PR_RESCOPE_EXECUTION_REQUIRED PR#{n} reason=rescope_required_state_pending "
                    f"{marker} issue=#{issue or 'unknown'} branch={branch} "
                    f"head={head_oid[:10]} "
                    f"remediation=make_the_GitHub_rescope_or_reimplementation_decision"
                )
        else:
            print(
                f"PR_RESCOPE_REQUIRED PR#{n} reason=rescope_required_state_missing_marker "
                f"issue=#{issue or 'unknown'} branch={branch} "
                f"head={str(pr.get('headRefOid') or '')[:10]} "
                f"remediation=make_the_GitHub_rescope_or_reimplementation_decision"
            )
        continue

    if state == "pm-state:blocked-rework":
        workflow_state, ci_run, e2e_run = workflow_summary(pr)
        if has_label(pr, "pm-blocked:rebase") and merge_state in {"BEHIND", "DIRTY"}:
            if free_slots and recently_updated(pr, 168):
                target = free_slots.pop(0)
                print(
                    f"PR_REBASE_REQUIRED PR#{n} slot:{target} reason=blocked_rework_rebase_unassigned_free_slot "
                    f"mergeStateStatus={merge_state} issue=#{issue or 'unknown'} branch={branch} "
                    f"head={str(pr.get('headRefOid') or '')[:10]} "
                    f"command=claim_slot_resolution_required branch={branch} head={str(pr.get('headRefOid') or '')[:10]} epoch=read_at_dispatch handoff=missing"
                )
            else:
                print(
                    f"PR_REBASE_REQUIRED PR#{n} reason=blocked_rework_rebase_unassigned_no_free_slot "
                    f"mergeStateStatus={merge_state} issue=#{issue or 'unknown'} branch={branch} "
                    f"head={str(pr.get('headRefOid') or '')[:10]} "
                    f"command=claim_slot_resolution_required branch={branch} head={str(pr.get('headRefOid') or '')[:10]} epoch=read_at_dispatch handoff=missing"
                )
            continue

        # A capture-only wait is terminal for the generic rework dispatcher.
        # The capture handler above owns exact-head dispatch/pass/release. If
        # it cannot yet emit one of those typed actions, fail closed here
        # instead of treating the mere presence of a durable packet as proof
        # that more product rework is owed.
        if has_label(pr, "pm-blocked:capture"):
            print(
                f"PR_CAPTURE_TRANSITION_REQUIRED PR#{n} reason=blocked_rework_capture_only_no_slot "
                f"issue=#{issue or 'unknown'} branch={branch} "
                f"head={str(pr.get('headRefOid') or '')[:10]} "
                f"command=inspect_exact_head_remote_capture_then_use_capture-remote-dispatch_or_capture-remote-pass"
            )
            continue

        # A terminal review-loop circuit breaker outranks a later ordinary PM
        # review marker. Once the review budget requires rescue, do not surface
        # pm-review-done and accidentally re-enter the normal PM state machine.
        head_oid = str(pr.get("headRefOid") or "")
        # PM_REVIEW_DONE_TERMINAL_RECEIPT_V1 consumer: a head-bound terminal
        # review-PASS receipt at the exact head means the review is terminal;
        # surface the typed blocker/wake (or idempotent resume) and never
        # schedule another same-head review from the blocked-rework handler.
        if pm_review_done_receipt_line(n, head_oid, issue, branch, "blocked_rework"):
            continue
        packet_supersedes_loop = durable_packet_supersedes_review_loop(n, head_oid)
        loop_breaker = review_loop_circuit_breaker(n, head_oid)
        if packet_supersedes_loop:
            loop_breaker = ""
        loop_wait_labels = (
            has_label(pr, "pm-blocked:capture")
            or has_label(pr, "pm-blocked:dependency")
            or has_label(pr, "pm-blocked:product")
            or has_label(pr, "pm-blocked:rebase")
            or has_label(pr, "pm-blocked:pm-gate")
            or has_label(pr, "pm-blocked:cto")
        )
        if loop_breaker and not loop_wait_labels:
            print(
                f"PR_REVIEW_CIRCUIT_BREAKER_REQUIRED PR#{n} reason=second_same_class_review_loop "
                f"{loop_breaker} issue=#{issue or 'unknown'} branch={branch} "
                f"head={str(pr.get('headRefOid') or '')[:10]} "
                f"command=run_Skill(pm-codex-pr-rescue)_from_current_exact-head_dispatch_receipt"
            )
            continue

        review_meta, review_scope = current_pm_review_contract(n, head_oid)
        if review_meta and not packet_supersedes_loop:
            marker_status, marker_path = opus_marker_status(n, str(pr.get("headRefOid") or ""), review_scope or "phase-a")
            if marker_status == "pass":
                if workflow_state == "terminal_bad" and ci_verdict_requires_rework(latest_ci_verdict(n, str(pr.get("headRefOid") or ""))):
                    print(ci_required_action_line(
                        pr,
                        "blocked_rework_current_head_ci_verdict_requires_rework",
                        workflow_state,
                        ci_run,
                        e2e_run,
                        issue,
                        branch,
                    ))
                    continue
                print(
                    f"PR_PM_REVIEW_COMPLETE_REQUIRED PR#{n} reason=blocked_rework_has_current_pm_review_pass "
                    f"scope={review_scope or 'phase-a'} marker={marker_path} issue=#{issue or 'unknown'} branch={branch} "
                    f"head={str(pr.get('headRefOid') or '')[:10]} "
                    f"command=cd\\ {CONTROL_PLANE_ROOT}\\ &&\\ PYTHONPATH={CONTROL_PLANE_ROOT}\\ python3\\ -m\\ {FAMILY2_MODULE}\\ --transition-type\\ pm_review\\ --issue\\ {issue or 'unknown'}\\ --pr\\ {n}\\ --review-evidence\\ {marker_path}"
                )
                continue
            if marker_status == "blocked":
                print(
                    f"PR_PM_REVIEW_REQUIRED PR#{n} reason=blocked_rework_has_current_pm_review_blocked_marker "
                    f"scope={review_scope or 'phase-a'} marker={marker_path} issue=#{issue or 'unknown'} branch={branch} "
                    f"head={str(pr.get('headRefOid') or '')[:10]} "
                    f"command=UNSUPPORTED_LIFECYCLE_ACTION:block-pr"
                )
                continue

        scope_breaker = "" if packet_supersedes_loop else scope_risk_circuit_breaker(n)
        if scope_breaker and state == "pm-state:blocked-rework" and not loop_wait_labels:
            print(
                f"PR_REVIEW_CIRCUIT_BREAKER_REQUIRED PR#{n} reason=scope_risk_requires_rescue_or_split "
                f"{scope_breaker} issue=#{issue or 'unknown'} branch={branch} "
                f"head={str(pr.get('headRefOid') or '')[:10]} "
                f"command=run_Skill(pm-codex-pr-rescue)_then_consume_split_or_rescope_terminal"
            )
            continue
        active_slot = exact_mop_owner_for_pr(n, branch)
        if active_slot and has_label(pr, "pm-blocked:product"):
            slot = active_slot.get("slot") or "unknown"
            packet = durable_rework_packet(n, str(pr.get("headRefOid") or ""))
            if packet:
                command = (
                    "claim_slot_resolution_required "
                    f"branch={branch} head={str(pr.get('headRefOid') or '')[:10]} "
                    f"epoch=read_at_dispatch handoff={packet}"
                )
            else:
                command = (
                    f"record_the_exact_rework_packet_in_the_existing_GitHub_workflow for PR {n}"
                )
            print(
                f"PR_EXTERNAL_WAIT_SLOT_CONFLICT_REQUIRED PR#{n} slot:{slot} "
                f"reason=product_wait_has_active_owner issue=#{issue or 'unknown'} branch={branch} "
                f"head={str(pr.get('headRefOid') or '')[:10]} packet={packet or 'missing'} "
                f"command={command}"
            )
            continue
        if active_slot:
            slot = active_slot.get("slot") or "unknown"
            turn_active = active_slot.get("active_turn_state") == "active"
            idle = not turn_active and (
                bool(active_slot.get("idle")) or active_slot.get("activity") in (None, "", "idle")
            )
            dnd = bool(active_slot.get("dnd"))
            if idle:
                pending_delivery = rework_delivery_pending(n, slot, active_slot, str(pr.get("headRefOid") or ""))
                if pending_delivery:
                    print(
                        f"PR_REWORK_DELIVERY_PENDING_REQUIRED PR#{n} slot:{slot} reason=packet_created_without_delivery_ack "
                        f"issue=#{issue or 'unknown'} branch={branch} packet={pending_delivery['packet']} "
                        f"age_min={pending_delivery['age_min']} "
                        "command=UNSUPPORTED_LIFECYCLE_ACTION:reconcile-capacity"
                    )
                else:
                    reason = "blocked_rework_slot_idle_dnd" if dnd else "blocked_rework_slot_idle"
                    print(
                        f"PR_ACTIVE_REWORK_IDLE_REQUIRED PR#{n} slot:{slot} reason={reason} "
                        f"issue=#{issue or 'unknown'} branch={branch} command=inspect_slot_or_dispatch_rework_nudge_or_free_slot"
                    )
            continue
        if slots_for_pr:
            matched_slots = []
            stale_slots = []
            for slot in slots_for_pr:
                entry = slot_by_num.get(slot) or {}
                if slot_entry_matches_pr(entry, n, branch):
                    matched_slots.append((slot, entry))
                else:
                    stale_slots.append((slot, entry))
            if stale_slots:
                stale_summary = ",".join(
                    f"slot:{slot}:{slot_entry_summary(entry)}" for slot, entry in stale_slots
                )
                print(
                    f"PR_STALE_SLOT_LABEL_REQUIRED PR#{n} reason=blocked_rework_slot_label_mop_mismatch "
                    f"slot_labels={','.join('slot:'+str(slot) for slot, _ in stale_slots)} "
                    f"mop={stale_summary} issue=#{issue or 'unknown'} branch={branch} "
                    f"command=remove_stale_slot_label_then_assign_rework_or_park"
                )
                continue
            for slot, entry in matched_slots:
                turn_active = entry.get("active_turn_state") == "active"
                idle = not turn_active and (
                    bool(entry.get("idle")) or entry.get("activity") in (None, "", "idle")
                )
                dnd = bool(entry.get("dnd"))
                if idle:
                    pending_delivery = rework_delivery_pending(n, slot, entry, str(pr.get("headRefOid") or ""))
                    if pending_delivery:
                        print(
                            f"PR_REWORK_DELIVERY_PENDING_REQUIRED PR#{n} slot:{slot} reason=packet_created_without_delivery_ack "
                            f"issue=#{issue or 'unknown'} branch={branch} packet={pending_delivery['packet']} "
                            f"age_min={pending_delivery['age_min']} "
                            "command=UNSUPPORTED_LIFECYCLE_ACTION:reconcile-capacity"
                        )
                        continue
                    reason = "blocked_rework_slot_idle_dnd" if dnd else "blocked_rework_slot_idle"
                    print(
                        f"PR_ACTIVE_REWORK_IDLE_REQUIRED PR#{n} slot:{slot} reason={reason} "
                        f"issue=#{issue or 'unknown'} branch={branch} command=inspect_slot_or_dispatch_rework_nudge_or_free_slot"
                )
            continue
        fuzzy_slot = active_slot_for_pr(n, issue, branch)
        if fuzzy_slot:
            slot = fuzzy_slot.get("slot") or "unknown"
            if emit_incomplete_owner(fuzzy_slot):
                print(
                f"PR_STATE_RECONCILE_REQUIRED PR#{n} slot:{slot} reason=blocked_rework_mop_owner_tuple_incomplete "
                f"issue=#{issue or 'unknown'} branch={branch} assignment_epoch={fuzzy_slot.get('assignment_epoch')} {owner_tokens(fuzzy_slot)} mop={slot_entry_summary(fuzzy_slot)} "
                f"command=UNSUPPORTED_LIFECYCLE_ACTION:reconcile-capacity"
                )
            continue
        if has_label(pr, "pm-blocked:product"):
            print(
                f"PR_PRODUCT_DECISION_WAITING PR#{n} reason=blocked_rework_product_decision_wait "
                f"blocker=product issue=#{issue or 'unknown'} branch={branch} "
                f"head={str(pr.get('headRefOid') or '')[:10]} "
                f"command=wait_for_rajiv_or_record_product_decision_then_reconcile"
            )
            continue
        if has_label(pr, "pm-blocked:pm-gate"):
            print(
                f"PR_PM_GATE_REVIEW_REQUIRED PR#{n} reason=blocked_rework_pm_gate_unassigned "
                f"blocker=pm-gate issue=#{issue or 'unknown'} branch={branch} "
                f"head={str(pr.get('headRefOid') or '')[:10]} "
                f"command=resolve_pm_gate_artifact_or_apply_pm-blocked:product_with_exact_question_before_fresh_dispatch"
            )
            continue
        packet_record = durable_rework_packet_record(n, str(pr.get("headRefOid") or ""))
        packet = packet_record.get("path") if packet_record else None
        packet_created_at = (
            packet_record.get("created_at")
            if packet_record and packet_record.get("created_at")
            else pr.get("createdAt")
        )
        if not packet:
            print(
                f"PR_REWORK_PACKET_REQUIRED PR#{n} reason=blocked_rework_missing_current_head_packet "
                f"issue=#{issue or 'unknown'} branch={branch} head={str(pr.get('headRefOid') or '')[:10]} "
                f"remediation=record_the_exact_rework_packet_in_the_existing_GitHub_workflow"
            )
            continue
        if free_slots:
            target = free_slots.pop(0)
            print(
                f"PR_REWORK_DISPATCH_REQUIRED PR#{n} slot:{target} reason=blocked_rework_unassigned_free_slot "
                f"blocker=rework issue=#{issue or 'unknown'} created_at={packet_created_at or 'unknown'} "
                f"packet={packet} command=claim_slot_resolution_required branch={branch} head={str(pr.get('headRefOid') or '')[:10]} epoch=read_at_dispatch handoff={packet}"
            )
        else:
            print(
                f"PR_REWORK_DISPATCH_REQUIRED PR#{n} reason=blocked_rework_unassigned_no_free_slot "
                f"blocker=rework issue=#{issue or 'unknown'} branch={branch} "
                f"head={str(pr.get('headRefOid') or '')[:10]} "
                f"created_at={packet_created_at or 'unknown'} packet={packet} "
                f"command=claim_slot_resolution_required branch={branch} head={str(pr.get('headRefOid') or '')[:10]} epoch=read_at_dispatch handoff={packet}"
            )
        continue

    if draft and not state and not blockers and recently_updated(pr, 96):
        # Slot-owned implementation/QA is already a complete active state. Do
        # not force a PM state transition that would release the slot mid-phase.
        if exact_mop_owner_for_pr(n, branch):
            continue
        active_slot = active_slot_for_pr(n, issue, branch)
        if active_slot:
            slot = active_slot.get("slot") or "unknown"
            if emit_incomplete_owner(active_slot):
                print(
                f"PR_STATE_RECONCILE_REQUIRED PR#{n} slot:{slot} reason=active_draft_mop_owner_tuple_incomplete "
                f"branch={branch} assignment_epoch={active_slot.get('assignment_epoch')} {owner_tokens(active_slot)} checks={check_state} mop={slot_entry_summary(active_slot)} "
                f"command=UNSUPPORTED_LIFECYCLE_ACTION:reconcile-capacity"
                )
            continue
        # A released draft is not green merely because docs-only/dummy checks
        # succeeded. Reuse the exact pull_request CI/E2E workflow accounting
        # used by merge readiness, and consume an existing exact-head PM
        # BLOCKED marker instead of asking PM to make an untyped prose choice.
        workflow_state, ci_run, e2e_run = workflow_summary(pr)
        # PM_REVIEW_DONE_TERMINAL_RECEIPT_V1 consumer: a terminal review-PASS
        # receipt at the exact head means this PR already passed review; never
        # schedule a fresh same-head review (draft-without-state path).
        if pm_review_done_receipt_line(n, str(pr.get("headRefOid") or ""), issue, branch, "draft_without_state"):
            continue
        marker_status, marker_path = opus_marker_status(n, head, "phase-a")
        if marker_status == "blocked":
            print(
                f"PR_PM_REVIEW_REQUIRED PR#{n} reason=pm_claude_blocked_apply_rework_state "
                f"scope=phase-a marker={marker_path} issue=#{issue or 'unknown'} branch={branch} "
                f"head={head[:10]} workflows={workflow_bad_summary(ci_run, e2e_run)} "
                f"command=UNSUPPORTED_LIFECYCLE_ACTION:block-pr"
            )
            continue
        print(
            f"PR_STATE_LABEL_REQUIRED PR#{n} reason=draft_without_pm_state "
            f"branch={branch} workflows={workflow_bad_summary(ci_run, e2e_run)} "
            f"marker_status={marker_status} marker={marker_path} "
            f"command=UNSUPPORTED_LIFECYCLE_ACTION:pm-review"
        )
        continue

    if not state and not draft and recently_updated(pr, 96):
        # A fresh ready-for-review PR may still be in slot-owned QA. The exact
        # MoP PR + branch tuple is authoritative until the slot emits the next
        # phase boundary; the PR does not need to duplicate the issue slot label.
        if exact_mop_owner_for_pr(n, branch):
            continue
        active_slot = active_slot_for_pr(n, issue, branch)
        if active_slot:
            slot = active_slot.get("slot") or "unknown"
            if emit_incomplete_owner(active_slot):
                print(
                f"PR_STATE_RECONCILE_REQUIRED PR#{n} slot:{slot} reason=active_open_mop_owner_tuple_incomplete "
                f"branch={branch} assignment_epoch={active_slot.get('assignment_epoch')} {owner_tokens(active_slot)} checks={check_state} mop={slot_entry_summary(active_slot)} "
                f"command=UNSUPPORTED_LIFECYCLE_ACTION:reconcile-capacity"
                )
            continue
        # PM_REVIEW_DONE_TERMINAL_RECEIPT_V1 consumer: never schedule a fresh
        # same-head review when the exact-head terminal receipt exists.
        if pm_review_done_receipt_line(n, str(pr.get("headRefOid") or ""), issue, branch, "open_pr_without_state"):
            continue
        print(
            f"PR_STATE_LABEL_REQUIRED PR#{n} reason=open_pr_without_pm_state "
            f"branch={branch} checks={check_state} command=UNSUPPORTED_LIFECYCLE_ACTION:pm-review"
        )

open_pr_issues = {str(linked_issue(pr)) for pr in prs if linked_issue(pr)}
issue_candidates = {}
for label in ("status:in-progress", "pm-state:rescope-required", "pm-state:blocked-rework"):
    for issue in fetch_open_issues_with_label(label):
        number = str(issue.get("number") or "")
        if number:
            issue_candidates[number] = issue
for entry in slots:
    if not entry.get("occupied"):
        continue
    issue_number = str(entry.get("issue") or "")
    if not issue_number or issue_number in issue_candidates:
        continue
    fetched = fetch_open_issue(issue_number)
    if fetched:
        issue_candidates[issue_number] = fetched

for issue in sorted(issue_candidates.values(), key=lambda item: int(item.get("number") or 0), reverse=True):
    n = int(issue.get("number") or 0)
    if not n:
        continue
    if str(n) in open_pr_issues:
        continue
    issue_labs = issue_labels(issue)
    slots_for_issue = issue_slot_labels(issue)
    active_slot = active_slot_for_pr("", n, "")
    slot = ""
    label_slot = str(slots_for_issue[0]) if slots_for_issue else ""
    branch = ""
    if active_slot:
        slot = str(active_slot.get("slot") or "")
        branch = str(active_slot.get("branch") or "")
    stale_slot_note = ""
    if label_slot and not slot:
        stale_slot_note = f" stale_slot_label=slot:{label_slot}"
    marker = issue_rescope_marker(n)
    if marker and "status=pending" in marker:
        print(
            f"ISSUE_RESCOPE_EXECUTION_REQUIRED issue#{n} reason=rescope_required_issue_pending "
            f"{marker} slot:{slot or 'none'}{stale_slot_note} branch={branch or 'unknown'} "
            f"remediation=make_the_GitHub_issue_rescope_or_reimplementation_decision"
        )
        continue
    if issue_has_label(issue, "pm-state:rescope-required") and not marker:
        slot_arg = f"\\ --slot\\ {slot}" if slot else ""
        branch_arg = f"\\ --branch\\ {command_quote(branch)}" if branch else ""
        print(
            f"ISSUE_RESCOPE_REQUIRED issue#{n} reason=rescope_required_issue_missing_marker "
            f"slot:{slot or 'none'}{stale_slot_note} branch={branch or 'unknown'} "
            f"remediation=make_the_GitHub_issue_rescope_or_reimplementation_decision"
        )
        continue
    if marker and "status=resolved" in marker:
        continue
    active_issue = (
        issue_has_label(issue, "status:in-progress")
        or issue_has_label(issue, "pm-state:blocked-rework")
        or bool(slots_for_issue)
        or bool(active_slot)
    )
    if not active_issue:
        continue
    loop_breaker = review_loop_circuit_breaker(n, "")
    if loop_breaker:
        slot_arg = f"\\ --slot\\ {slot}" if slot else ""
        branch_arg = f"\\ --branch\\ {command_quote(branch)}" if branch else ""
        print(
            f"ISSUE_REVIEW_CIRCUIT_BREAKER_REQUIRED issue#{n} reason=issue_second_same_class_review_loop "
            f"{loop_breaker} slot:{slot or 'none'}{stale_slot_note} labels={','.join(issue_labs) or 'none'} "
            f"branch={branch or 'unknown'} "
            f"remediation=make_the_GitHub_issue_rescope_or_reimplementation_decision"
        )
PYEOF
then
  unlink "$ACTION_LINES_FILE"
  exit 2
fi
ACTION_LINES="$(<"$ACTION_LINES_FILE")"
unlink "$ACTION_LINES_FILE"

SORTED_ACTION_LINES="$(
  ACTION_LINES_RAW="$ACTION_LINES" python3 <<'PYEOF'
import os
import re

raw = os.environ.get("ACTION_LINES_RAW", "")
priority = [
    (0, re.compile(r"^PR_MERGE_READY_INVALID_REQUIRED\b")),
    (1, re.compile(r"^PR_REBASE_REQUIRED\b|^PR_MAIN_DRIFT_REQUIRED\b")),
    (2, re.compile(r"^PR_READY_PROMOTION_REQUIRED\b|^PR_CI_LABEL_RECONCILE_REQUIRED\b|^PR_REVIEW_CIRCUIT_BREAKER_REQUIRED\b|^ISSUE_REVIEW_CIRCUIT_BREAKER_REQUIRED\b|^PR_EXTERNAL_WAIT_SLOT_CONFLICT_REQUIRED\b")),
    (3, re.compile(r"^PR_CLEANUP_CLOSEOUT_REQUIRED\b|^PR_CAPTURE_SLOT_RELEASE_REQUIRED\b|^PR_PM_REVIEW_REQUIRED\b|^PR_PM_REVIEW_COMPLETE_REQUIRED\b|^PR_PM_REVIEW_AFFECTED_TEST_REQUIRED\b|^PR_PM_REVIEW_WORKFLOW_DRAIN_REQUIRED\b|^PR_PM_REVIEW_CAPTURE_(?:REQUIRED|COMPLETE_REQUIRED|BYPASS_REQUIRED)\b|^PR_PM_REVIEW_SLOT_RELEASE_REQUIRED\b|^PR_PM_REVIEW_TERMINAL_(?:BLOCKED|RECEIPT)_REQUIRED\b")),
    (4, re.compile(r"^PR_CI_VERDICT_REWORK_REQUIRED\b|^PR_LOCAL_PREFLIGHT_REQUIRED\b|^PR_CI_RERUN_AFTER_PREFLIGHT_REQUIRED\b|^PR_CI_CAPPED_RERUN_REQUIRED\b|^PR_CI_STALE_RUN_RERUN_REFUSED\b|^PR_CI_CLASSIFICATION_REQUIRED\b|^PR_CI_WATCH_STUCK_REQUIRED\b|^PR_CAPTURE_")),
    (5, re.compile(r"^PR_CI_DEPENDENCY_BLOCKED_REQUIRED\b|^PR_DEPENDENCY_UNBLOCKED_REQUIRED\b|^PR_DEPENDENCY_WEDGE_REQUIRED\b")),
    (6, re.compile(r"^PR_REWORK_PACKET_REQUIRED\b|^PR_REWORK_DISPATCH_REQUIRED\b|^PR_REWORK_DELIVERY_PENDING_REQUIRED\b|^PR_ACTIVE_REWORK_IDLE_REQUIRED\b|^PR_PM_GATE_REVIEW_REQUIRED\b|^PR_PRODUCT_DECISION_WAITING\b|^PR_RESCOPE_|^ISSUE_RESCOPE_")),
    (7, re.compile(r"^PR_SLOT_RELEASE_REQUIRED\b|^PR_SLOT_RELEASE_BEFORE_CI_REQUIRED\b|^PR_STALE_SLOT_LABEL_REQUIRED\b|^PR_STALE_BLOCKER_REQUIRED\b|^PR_STATE_RECONCILE_REQUIRED\b|^PR_STATE_LABEL_REQUIRED\b|^PR_DRAFT_ORPHAN_REVIEW_REQUIRED\b|^PR_READY_BEFORE_CI_REQUIRED\b")),
    (8, re.compile(r"^PR_CI_STALE_HEAD_CHURN_REQUIRED\b")),
    (9, re.compile(r"^PR_CI_(?:DEPENDENCY|HOLD)_WATCHING\b|^PR_CTO_DECISION_WAITING\b|^PR_CAPTURE_REARM_AFTER_MAIN_SYNC_WATCHING\b")),
]

def line_priority(line):
    for value, pattern in priority:
        if pattern.search(line):
            return value
    return 8

def pr_key(line):
    m = re.search(r"PR#(\d+)", line)
    return m.group(1) if m else None

rows = [(line_priority(line), i, line) for i, line in enumerate(raw.splitlines()) if line.strip()]
rows.sort(key=lambda item: (item[0], item[1]))
seen_prs = set()
out = []
for prio, _idx, line in rows:
    pr = pr_key(line)
    if pr and not line.startswith(("PR_CI_DEPENDENCY_WATCHING ", "PR_CI_HOLD_WATCHING ", "PR_CTO_DECISION_WAITING ", "PR_CAPTURE_LOCAL_RUNNING ", "PR_CAPTURE_REARM_AFTER_MAIN_SYNC_WATCHING ")):
        if pr in seen_prs:
            continue
        seen_prs.add(pr)
    out.append(line)
print("\n".join(out))
PYEOF
)"
ACTION_LINES="$SORTED_ACTION_LINES"
unset SORTED_ACTION_LINES

WATCH_LINES="$(printf '%s\n' "$ACTION_LINES" | sed -n '/^PR_CI_DEPENDENCY_WATCHING /p;/^PR_CI_HOLD_WATCHING /p;/^PR_CTO_DECISION_WAITING /p;/^PR_CAPTURE_LOCAL_RUNNING /p;/^PR_CAPTURE_REARM_AFTER_MAIN_SYNC_WATCHING /p')"
ACTION_LINES="$(printf '%s\n' "$ACTION_LINES" | sed '/^[[:space:]]*$/d;/^PR_CI_DEPENDENCY_WATCHING /d;/^PR_CI_HOLD_WATCHING /d;/^PR_CTO_DECISION_WAITING /d;/^PR_CAPTURE_LOCAL_RUNNING /d;/^PR_CAPTURE_REARM_AFTER_MAIN_SYNC_WATCHING /d')"

case "$TRIGGER" in
  hourly-ops-audit|hourly-*) ;;
  *)
    WATCH_LINES=""
    ACTION_LINES="$(printf '%s\n' "$ACTION_LINES" | sed '/^PR_CI_DEPENDENCY_BLOCKED_REQUIRED /d')"
    ;;
esac

ACTION_COUNT="$(printf '%s\n' "$ACTION_LINES" | sed '/^[[:space:]]*$/d' | wc -l | tr -d ' ')"

MUTATED=0
if [ -n "$WATCH_LINES" ]; then
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    emit "$line"
  done <<< "$WATCH_LINES"
fi

if [ -n "$ACTION_LINES" ]; then
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    emit "$line"
    case "$line" in
      PR_SLOT_RELEASE_REQUIRED*|PR_SLOT_RELEASE_BEFORE_CI_REQUIRED*|PR_CAPTURE_SLOT_RELEASE_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        slot="$(printf '%s\n' "$line" | sed -n 's/.*slot:\([0-9][0-9]*\).*/\1/p')"
        issue="$(printf '%s\n' "$line" | sed -n 's/.*issue=#\([0-9][0-9]*\).*/\1/p')"
        branch="$(printf '%s\n' "$line" | sed -n 's/.*branch=\([^ ]*\).*/\1/p')"
        release_reason="merge-ready-slot-label"
        case "$line" in
          PR_SLOT_RELEASE_BEFORE_CI_REQUIRED*) release_reason="qa-passed-ci-start-slot-release" ;;
          PR_CAPTURE_SLOT_RELEASE_REQUIRED*) release_reason="capture-watch-slot-release" ;;
        esac
        release_slot_label_for_pr "$pr" "$slot" "$issue" "$branch" "$release_reason"
        ;;
      PR_CI_LABEL_RECONCILE_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind ci_label_reconcile \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --title "PR #$pr stale ci-blocker while qa-passed-awaiting-ci" \
          --action "Clear stale pm-blocked:ci only if latest-head CI/E2E are green, then run pm-readiness-contract / CTO hold handling. If not green, classify the failure and require current-head local preflight proof before any same-head rerun; do not label-toggle churn." \
          --blocker "qa_awaiting_ci_has_ci_blocker" \
          --evidence "$line"
        ;;
      PR_REWORK_DISPATCH_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind rework \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --title "PR #$pr blocked rework has free slot capacity" \
          --action "Dispatch rework to the named free slot or explicitly park/escalate if PM-gate still blocks." \
          --blocker "blocked_rework_unassigned_free_slot" \
          --evidence "$line"
        ;;
      PR_REWORK_PACKET_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind rework_packet_required \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr needs a durable current-head rework packet" \
          --action "Compose the exact head-bound rework packet file and record it with rework-packet-ledger.py publish (--repo/--pr/--issue/--head/--kind rework/--packet; idempotent per packet content and head, replay returns existing), then dispatch only through Skill(direct-assign) with the recorded packet identity. Existing PR rework keeps priority over fresh status:todo work; never hand-write the packet comment when the writer is available." \
          --blocker "blocked_rework_missing_current_head_packet" \
          --evidence "$line"
        ;;
      PR_REWORK_DELIVERY_PENDING_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        slot="$(printf '%s\n' "$line" | sed -n 's/.*slot:\([0-9][0-9]*\).*/\1/p')"
        packet="$(printf '%s\n' "$line" | sed -n 's/.*packet=\([^ ]*\).*/\1/p')"
        command="$(printf '%s\n' "$line" | sed -n 's/.*command=\(.*\)$/\1/p' | sed 's/\\ / /g')"
        upsert_obligation \
          --kind rework_delivery_pending \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --slot "$slot" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr rework packet exists but slot delivery is unproven" \
          --action "Deliver the exact handoff identity for PR #$pr slot:$slot only through Skill(direct-assign) and require delivery_verified=true before treating the handoff as delivered; do not invoke the retired shell handoff surface and do not resume a legacy claim_slot/message-slot outbox. If the prior delivery is uncertain or unverified, keep the blocker open and do not retry." \
          --blocker "packet_created_without_delivery_ack" \
          --evidence "$line"
        ;;
      PR_READY_PROMOTION_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        head="$(printf '%s\n' "$line" | sed -n 's/.*head=\([0-9a-f][0-9a-f]*\).*/\1/p')"
        issue="$(printf '%s\n' "$line" | sed -n 's/.*issue=#\([0-9][0-9]*\).*/\1/p')"
        branch="$(printf '%s\n' "$line" | sed -n 's/.*branch=\([^ ]*\).*/\1/p')"
        ready_gate="$(printf '%s\n' "$line" | sed -n 's/.*ready_gate=\([^ ]*\).*/\1/p')"
        ready_gate_reason="$(printf '%s\n' "$line" | sed -n 's/.*ready_gate_reason=\([^ ]*\).*/\1/p')"
        proof_state="$(printf '%s\n' "$line" | sed -n 's/.*proof_state=\([^ ]*\).*/\1/p')"
        if [ "$DRY_RUN" = "1" ] || [ "$ready_gate" != "pass" ]; then
          # Fail closed: dry-run never transitions; a blocked readiness gate
          # (missing/stale current-head Phase-A/QA proof, or head drift) keeps
          # the existing PM-side merge_ready_review obligation as the typed
          # resolution path and never promotes on its own.
          if [ "$DRY_RUN" != "1" ]; then
            emit "PR_READY_PROMOTION_BLOCKED PR#$pr reason=readiness_gate_${ready_gate_reason:-unknown} head=${head:0:10} issue=#${issue:-unknown} branch=${branch:-unknown}"
          fi
          upsert_obligation \
            --kind merge_ready_review \
            --severity high \
            --target-type pr \
            --target-id "$pr" \
            --pr "$pr" \
            --owner pm \
            --horizon hourly \
            --title "PR #$pr green CI needs merge-ready readiness" \
            --action "Run pm-readiness-contract; if PASS, write /tmp/pm-state-promotion-proof-$pr-merge-ready.ok with exact current headRefOid. Promotion transitions are retired (UNSUPPORTED_LIFECYCLE_ACTION:validate-ready-proof, UNSUPPORTED_LIFECYCLE_ACTION:merge-ready); no merge-ready flip executes. If BLOCKED, apply the blocker state instead of dispatching new work." \
            --blocker "qa_awaiting_ci_terminal_green_clean" \
            --evidence "$line" \
            --evidence "ready_gate=$ready_gate" \
            --evidence "ready_gate_reason=${ready_gate_reason:-none}"
        else
        # Independent readiness consumer (CTO directive 1786344650.999889):
        # re-verify the exact current head did not drift since emission, then
        # execute the existing readiness contract and the canonical typed
        # transitions through their existing handlers. The typed transitions
        # re-validate everything (proof, threads, mergeability, current-head CI
        # guard); on success the merge-ready handler itself resolves the
        # ci_reconcile/ci_watch obligations. A failure leaves the PR untouched
        # and falls back to the PM-side obligation; the next sweep retries.
        live_head=""
        transition_output=""
        transition_rc=0
        merge_output=""
        merge_rc=0
        live_json="$(gh pr view "$pr" --repo "$GH_REPO" --json headRefOid 2>/dev/null || true)"
        live_head="$(printf '%s' "$live_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("headRefOid",""))' 2>/dev/null || true)"
        if [ -z "$live_head" ] || [ "$live_head" != "$head" ]; then
          emit "PR_READY_PROMOTION_BLOCKED PR#$pr reason=readiness_gate_head_drift head=${head:0:10} live_head=${live_head:0:10} issue=#${issue:-unknown} branch=${branch:-unknown}"
          upsert_obligation \
            --kind merge_ready_review \
            --severity high \
            --target-type pr \
            --target-id "$pr" \
            --pr "$pr" \
            --owner pm \
            --horizon hourly \
            --title "PR #$pr green CI needs merge-ready readiness" \
            --action "Run pm-readiness-contract; if PASS, write /tmp/pm-state-promotion-proof-$pr-merge-ready.ok with exact current headRefOid. Promotion transitions are retired (UNSUPPORTED_LIFECYCLE_ACTION:validate-ready-proof, UNSUPPORTED_LIFECYCLE_ACTION:merge-ready); no merge-ready flip executes. If BLOCKED, apply the blocker state instead of dispatching new work." \
            --blocker "qa_awaiting_ci_terminal_green_clean" \
            --evidence "$line" \
            --evidence "ready_gate=blocked" \
            --evidence "ready_gate_reason=head_drift"
        else
          if [ "$proof_state" = "invalid" ] || [ "$proof_state" = "unreadable" ]; then
            # An existing packet that fails the exact-tuple contract (missing
            # or different PR identity, missing READY_PACKET PASS or
            # review_provenance, or unreadable) is never rewritten and never
            # promoted: conflicting evidence stays untouched and the PM-side
            # merge_ready_review obligation is retained as the typed
            # resolution path (reviewer BLOCK exact_tuple_authorization,
            # ordinal-2).
            emit "PR_READY_PROMOTION_BLOCKED PR#$pr reason=readiness_gate_proof_${proof_state} head=${head:0:10} issue=#${issue:-unknown} branch=${branch:-unknown}"
            upsert_obligation \
              --kind merge_ready_review \
              --severity high \
              --target-type pr \
              --target-id "$pr" \
              --pr "$pr" \
              --owner pm \
              --horizon hourly \
              --title "PR #$pr green CI needs merge-ready readiness" \
              --action "Run pm-readiness-contract; if PASS, write /tmp/pm-state-promotion-proof-$pr-merge-ready.ok with exact current headRefOid. Promotion transitions are retired (UNSUPPORTED_LIFECYCLE_ACTION:validate-ready-proof, UNSUPPORTED_LIFECYCLE_ACTION:merge-ready); no merge-ready flip executes. If BLOCKED, apply the blocker state instead of dispatching new work." \
              --blocker "qa_awaiting_ci_terminal_green_clean" \
              --evidence "$line" \
              --evidence "ready_gate=blocked" \
              --evidence "ready_gate_reason=proof_${proof_state}"
          else
            if [ "$proof_state" != "ok" ]; then
              # proof_state is missing or stale-for-the-same-PR: the readiness
              # contract verdict (current-head Phase-A/QA proof) materializes
              # the merge-ready packet at the exact head. An existing valid
              # packet is kept untouched (idempotent).
              printf 'READY_PACKET: PASS\nPR: %s\nheadRefOid: %s\nreview_provenance: ok\n' \
                "$pr" "$head" >"/tmp/pm-state-promotion-proof-${pr}-merge-ready.ok.$$" \
                && mv "/tmp/pm-state-promotion-proof-${pr}-merge-ready.ok.$$" "/tmp/pm-state-promotion-proof-${pr}-merge-ready.ok"
            fi
          # validate-ready-proof and merge-ready transitions are retired: refuse
          # without mutation so the existing FAILED path below carries the stop.
          merge_output="UNSUPPORTED_LIFECYCLE_ACTION:validate-ready-proof"
          merge_rc=3
          if false; then
            emit "PR_READY_PROMOTION_APPLIED PR#$pr head=$head issue=#${issue:-unknown} branch=${branch:-unknown} receipt=$(printf '%s' "$merge_output" | one_line_excerpt)"
          else
            emit "PR_READY_PROMOTION_FAILED PR#$pr head=$head rc=$merge_rc output=$(printf '%s' "$merge_output" | one_line_excerpt)"
            upsert_obligation \
              --kind merge_ready_review \
              --severity high \
              --target-type pr \
              --target-id "$pr" \
              --pr "$pr" \
              --owner pm \
              --horizon hourly \
              --title "PR #$pr green CI needs merge-ready readiness" \
              --action "Run pm-readiness-contract; if PASS, write /tmp/pm-state-promotion-proof-$pr-merge-ready.ok with exact current headRefOid. Promotion transitions are retired (UNSUPPORTED_LIFECYCLE_ACTION:validate-ready-proof, UNSUPPORTED_LIFECYCLE_ACTION:merge-ready); no merge-ready flip executes. If BLOCKED, apply the blocker state instead of dispatching new work." \
              --blocker "qa_awaiting_ci_terminal_green_clean" \
              --evidence "$line" \
              --evidence "ready_gate=pass" \
              --evidence "processor_rc=$merge_rc processor_output=$(printf '%s' "$merge_output" | one_line_excerpt)"
          fi
          fi
        fi
        fi
        ;;
      PR_CLEANUP_CLOSEOUT_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind cleanup_pr \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr merged cleanup closeout is overdue" \
          --action "Run/resume Skill(cleanup-pr) for PR #$pr. Do not leave pm-state:merged-cleanup-pending or pm-cleanup:needed older than 15 minutes without an explicit blocker post." \
          --blocker "merged_cleanup_pending_over_15m" \
          --evidence "$line"
        ;;
      PR_PM_REVIEW_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind pm_review \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr needs PM Claude phase-A review transition" \
          --action "Run/finish PM Claude phase-A review only. Review transitions are retired (UNSUPPORTED_LIFECYCLE_ACTION:pm-review, UNSUPPORTED_LIFECYCLE_ACTION:pm-review-done); no review state changes execute. Merge-ready uses pm-readiness-contract, not pm-review --scope merge-ready." \
          --blocker "pm_review_pending" \
          --evidence "$line"
        ;;
      PR_PM_REVIEW_TERMINAL_BLOCKED_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        blocked_class="$(printf '%s\n' "$line" | sed -n 's/.*blocked_after_review=\([^ ]*\).*/\1/p')"
        wake="$(printf '%s\n' "$line" | sed -n 's/.*wake=\([^ ]*\).*/\1/p')"
        upsert_obligation \
          --kind pm_review_complete \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr review is terminal PASS but blocked after review ($blocked_class)" \
          --action "Do not launch another same-head review (UNSUPPORTED_LIFECYCLE_ACTION:pm-review-done stays retired) until the blocker clears. Execute the typed wake: ${wake:-see sweep row}. After the wake is satisfied, the resume stays retired; no duplicate terminal event/obligation/label event/CI wave." \
          --blocker "review_terminal_blocked_after_review" \
          --evidence "$line"
        ;;
      PR_PM_REVIEW_TERMINAL_RECEIPT_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind pm_review_complete \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr review is terminal PASS with an exact-head receipt" \
          --action "The head-bound terminal receipt no longer resumes any transition (UNSUPPORTED_LIFECYCLE_ACTION:pm-review-done). No second reviewer, no duplicate terminal event, obligation, label event, or CI wave; head drift invalidates reuse." \
          --blocker "review_terminal_receipt_resume" \
          --evidence "$line"
        ;;
      PR_PM_REVIEW_AFFECTED_TEST_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind pm_review_pending \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr PM Claude review passed (affected-test proof retired)" \
          --action "Affected-test proof is retired (Rajiv thread 1786811168.455449). The pm-review-done ownership release is retired (UNSUPPORTED_LIFECYCLE_ACTION:pm-review-done); no move to qa-passed-awaiting-ci executes; do not rerun PM review." \
          --blocker "pm_review_passed_legacy_affected_test_obligation" \
          --evidence "$line"
        ;;
      PR_PM_REVIEW_WORKFLOW_DRAIN_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind pm_review_complete \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr PM review passed while an exact-head workflow is active" \
          --action "Do not duplicate or label-toggle the active workflow. Wait for every exact-head CI/E2E run to reach terminal and inspect any failure. The pm-review-done ownership release is retired (UNSUPPORTED_LIFECYCLE_ACTION:pm-review-done); rerunning pr-state-sweep resumes no transition and performs no release." \
          --blocker "pm_review_passed_current_head_workflow_active" \
          --evidence "$line"
        ;;
      PR_PM_REVIEW_COMPLETE_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        head="$(printf '%s\n' "$line" | sed -n 's/.*head=\([0-9a-f][0-9a-f]*\).*/\1/p')"
        issue="$(printf '%s\n' "$line" | sed -n 's/.*issue=#\([0-9][0-9]*\).*/\1/p')"
        marker="$(printf '%s\n' "$line" | sed -n 's/.*marker=\([^ ]*\).*/\1/p')"
        affected_test_proof="$(printf '%s\n' "$line" | sed -n 's/.*affected_test_proof=\([^ ]*\).*/\1/p')"
        transition_output=""
        transition_rc=0
        if [ "$DRY_RUN" != "1" ] && [ -n "$issue" ] && [ -n "$marker" ]; then
          transition_output="$(cd "$CONTROL_PLANE_ROOT" && PYTHONPATH="$CONTROL_PLANE_ROOT" python3 -m "$FAMILY2_MODULE" --transition-type pm_review --issue "$issue" --pr "$pr" --review-evidence "$marker" 2>&1)"
          transition_rc=$?
        elif [ "$DRY_RUN" != "1" ]; then
          transition_rc=2
          transition_output="family2 parked pm-review requires linked issue and exact-head marker"
        fi
        if [ "$DRY_RUN" != "1" ] \
          && [ "$transition_rc" -eq 0 ]; then
          emit "PR_PM_REVIEW_COMPLETE_APPLIED PR#$pr head=$head affected_test_proof=$affected_test_proof receipt=$(printf '%s' "$transition_output" | one_line_excerpt)"
          MUTATED=$((MUTATED+1))
        elif [ "$DRY_RUN" != "1" ] \
          && [ "$transition_rc" -eq 23 ] \
          && printf '%s\n' "$transition_output" | grep -q '"decision": "committed_deferred"'; then
          # The Python boundary already committed the sole obligation/outbox;
          # do not create a legacy projected duty for the retryable effect.
          emit "PR_PM_REVIEW_COMPLETE_DEFERRED PR#$pr head=$head affected_test_proof=$affected_test_proof result=$(printf '%s' "$transition_output" | one_line_excerpt)"
          MUTATED=$((MUTATED+1))
        else
          upsert_obligation \
            --kind pm_review_complete \
            --severity high \
            --target-type pr \
            --target-id "$pr" \
            --pr "$pr" \
            --owner pm \
            --horizon hourly \
            --title "PR #$pr PM Claude review passed; transition to next state" \
            --action "Run the canonical Python family2 pm-review boundary for PR #$pr at exact head $head; it must bind the linked issue/marker and refuse slot-owner drift before projecting qa-passed-awaiting-ci." \
            --blocker "pm_review_passed_not_consumed" \
            --evidence "$line" \
            --evidence "processor_rc=$transition_rc processor_output=$(printf '%s' "$transition_output" | one_line_excerpt)"
          if [ "$DRY_RUN" != "1" ]; then
            emit "PR_PM_REVIEW_COMPLETE_FAILED PR#$pr head=$head affected_test_proof=$affected_test_proof rc=$transition_rc output=$(printf '%s' "$transition_output" | one_line_excerpt)"
          fi
        fi
        ;;
      PR_PM_REVIEW_CAPTURE_REQUIRED*|PR_PM_REVIEW_CAPTURE_BYPASS_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind capture_watch \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr PM Claude review requires remote capture before CI" \
	          --action "The pm-review-done and capture dispatch/consume transitions are retired (UNSUPPORTED_LIFECYCLE_ACTION:pm-review-done, UNSUPPORTED_LIFECYCLE_ACTION:capture-remote-dispatch, UNSUPPORTED_LIFECYCLE_ACTION:capture-remote-pass). Local capture is diagnostic-only and never satisfies capture readiness." \
          --blocker "pm_review_capture_gated" \
          --evidence "$line"
        ;;
      PR_PM_REVIEW_CAPTURE_COMPLETE_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind pm_review_complete \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr remote capture complete; consume PM Claude review" \
	          --action "Exact-head remote capture is authoritative. Its consume transitions are retired (UNSUPPORTED_LIFECYCLE_ACTION:capture-remote-pass, UNSUPPORTED_LIFECYCLE_ACTION:pm-review-done, UNSUPPORTED_LIFECYCLE_ACTION:ci-local-preflight-pass); no local-repro requirement is created and no readiness/CI is routed. The slot runs the canonical local repro and records the raw classification (no sealed envelope); CTO fires the next exact-head label-gated CI+E2E wave (cto_ci_wave_required)." \
          --blocker "pm_review_capture_complete_not_consumed" \
          --evidence "$line"
        ;;
      PR_CI_WATCH_STUCK_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind ci_watch \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr label-gated CI/E2E watch is stuck" \
          --action "Inspect the named same-head workflow run; cancel/rerun or classify the CI blocker before treating the PR as green or dispatching around it." \
          --blocker "qa_awaiting_ci_required_workflow_in_progress_stale" \
          --evidence "$line"
        ;;
      PR_CAPTURE_REMOTE_DISPATCH_REQUIRED*|PR_CAPTURE_REMOTE_PASS_REQUIRED*|PR_CAPTURE_REMOTE_WATCHING*|PR_CAPTURE_REMOTE_FAILURE_REVIEW_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind capture_watch \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr budgeted remote capture transition" \
          --action "Run the exact emitted remote capture action. Dispatch only through capture-remote-dispatch; keep every dev slot released; consume success only through capture-remote-pass; classify terminal failure before any same-head retry." \
          --blocker "capture_remote_transition" \
          --evidence "$line"
        ;;
      PR_CAPTURE_LOCAL_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind capture_local_preflight \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr local capture diagnostic (named infra defect only)" \
          --action "Run the existing local capture diagnostic only for a concrete remote-capture infrastructure failure; the local proof is diagnostic evidence and does not satisfy capture readiness." \
          --blocker "capture_local_proof_required" \
          --evidence "$line"
        ;;
      PR_CAPTURE_LOCAL_PASS_CI_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        proof="$(printf '%s\n' "$line" | sed -n 's/.*local_capture_proof=\([^ ]*\).*/\1/p')"
        upsert_obligation \
          --kind capture_watch \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr remote capture still required after local diagnostic" \
          --action "The local diagnostic proof was recorded; it does not satisfy capture readiness. The authoritative remote-capture dispatch/consume transitions are retired (UNSUPPORTED_LIFECYCLE_ACTION:capture-remote-dispatch, UNSUPPORTED_LIFECYCLE_ACTION:capture-remote-pass)." \
          --blocker "capture_remote_watch" \
          --evidence "$line"
        ;;
      PR_CAPTURE_BEFORE_CI_WATCH_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind capture_watch \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr local capture proof required before label-gated CI" \
	          --action "Keep the PR parked at pm-review-pending + pm-blocked:capture until slot-local capture proof is written on the exact current PR head. Then consume capture-local-pass and start label-gated CI directly. Remote capture is not required after local capture proof." \
          --blocker "capture_before_ci" \
          --evidence "$line"
        ;;
      PR_CAPTURE_COMPLETE_SLOT_READY_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind capture_watch \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr local capture proof ready; resume saved slot-ready transition" \
          --action "The slot-ready transition is retired (UNSUPPORTED_LIFECYCLE_ACTION:slot-ready); no slot release, ownership assertion, or qa-passed-awaiting-ci application executes. Skipped/dummy contexts do not count as readiness." \
          --blocker "capture_complete_slot_ready_not_consumed" \
          --evidence "$line"
        ;;
      PR_CAPTURE_COMPLETE_PM_REVIEW_DONE_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind pm_review_complete \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr local capture proof ready; finish PM review transition" \
          --action "The pm-review-done transition is retired (UNSUPPORTED_LIFECYCLE_ACTION:pm-review-done); no slot release, ownership assertion, or label-gated CI start executes. Skipped/dummy contexts do not count as readiness." \
          --blocker "capture_complete_pm_review_not_consumed" \
          --evidence "$line"
        ;;
      PR_CAPTURE_BEFORE_CI_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind capture_watch \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr prompt/LLM-path CI failure requires capture before rerun" \
	          --action "Return UNSUPPORTED_LIFECYCLE_ACTION:block-pr for the capture block. Run slot-local capture proof on the exact current PR head, consume it, then trigger label-gated CI directly. Do not dispatch generic local CI rework or rerun CI until local capture proof exists on the current head." \
          --blocker "capture_before_ci" \
          --evidence "$line"
        ;;
      PR_CAPTURE_COMPLETE_RERUN_CI_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind ci_rerun_after_preflight \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr capture job/local repro green; capped same-head CI/E2E rerun" \
          --action "Capture-green means formatting reached terminal output and fresh fixtures were written; it is only a pre-CI artifact, not readiness. Run at most one same-head rerun of the original failed CI/E2E run through /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/ci/rerun-after-local-proof.sh --pr $pr --run <failed-run> (no sealed proof required; Rajiv 1786812200.371389), then swap pm-blocked:capture back to pm-blocked:ci. Skipped/dummy checks do not satisfy readiness." \
          --blocker "capture_complete_ci_rerun_after_preflight" \
          --evidence "$line"
        ;;
      PR_CI_VERDICT_REWORK_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        verdict_class="$(printf '%s\n' "$line" | sed -n 's/.*verdict_class=\([^ ]*\).*/\1/p')"
        upsert_obligation \
          --kind ci_verdict_rework \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr current-head CI verdict requires rework, not rerun" \
          --action "Do not rerun capture/CI for this current-head failure. The latest ci-verdict marker says ${verdict_class:-current-head failure} needs code/prompt rework; keep/add pm-blocked:ci and dispatch the verdict comment to the owning slot or a free slot after reconcile-capacity." \
          --blocker "ci_verdict_rework_required" \
          --evidence "$line"
        ;;
      PR_CAPTURE_WATCH_REQUIRED*|PR_CAPTURE_WATCH_STUCK_REQUIRED*|PR_CAPTURE_FAILED_REQUIRED*|PR_CAPTURE_LABEL_RECONCILE_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind capture_watch \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr capture proof watch needs PM action" \
	          --action "Use the emitted capture transition. The required sequence is exact-current-head slot-local capture proof -> consume capture-local-pass -> label-gated CI/E2E on the same head -> merge-ready. Do not merge main solely for capture. Wait only for active local proof work; inspect stale/missing proof; never treat skipped CI or dummy checks as readiness." \
          --blocker "capture_watch" \
          --evidence "$line"
        ;;
      PR_LOCAL_PREFLIGHT_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind ci_local_preflight \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr current-head CI/E2E failed; slot local repro + rework required" \
          --action "Return UNSUPPORTED_LIFECYCLE_ACTION:block-pr for the CI block and UNSUPPORTED_LIFECYCLE_ACTION:reconcile-capacity for capacity; dispatch rework only through Skill(direct-assign) with the exact failed-run packet to the first healthy free slot. The slot inspects the failed run logs/Modal cache, runs one focused local repro at the exact head, fixes any defect, and pushes a descendant head; the raw PASS/FAIL repro terminal is the classification (no sealed envelope; Rajiv 1786812200.371389). CTO fires the next exact-head label-gated CI+E2E wave. Do not raise wall budgets." \
          --blocker "ci_local_preflight_required" \
          --evidence "$line"
        ;;
      PR_CI_RERUN_AFTER_PREFLIGHT_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind ci_rerun_after_preflight \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr capped same-head CI/E2E retry (infra/flake/shared class)" \
          --action "Run at most one same-head rerun of the failed CI/E2E run through /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/ci/rerun-after-local-proof.sh --pr $pr --run <failed-run> (no sealed proof required; Rajiv 1786812200.371389). The wrapper enforces the one-retry cap and refuses product classes; on a non-infra refusal, UNSUPPORTED_LIFECYCLE_ACTION:block-pr, then dispatch rework only through Skill(direct-assign). Never toggle CI labels on the same head." \
          --blocker "ci_rerun_after_preflight" \
          --evidence "$line"
        ;;
      PR_CI_CAPPED_RERUN_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind ci_rerun_after_preflight \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr current-head CI/E2E red; at most one capped same-head retry" \
          --action "Run at most one same-head rerun of the failed CI/E2E run through /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/ci/rerun-after-local-proof.sh --pr $pr --run <failed-run> (no sealed proof required; Rajiv 1786812200.371389). The wrapper enforces the one-retry cap and refuses product classes; on a non-infra refusal, UNSUPPORTED_LIFECYCLE_ACTION:block-pr and UNSUPPORTED_LIFECYCLE_ACTION:reconcile-capacity (dispatch rework only through Skill(direct-assign) with the exact failed-run packet). Never toggle CI labels on the same head." \
          --blocker "ci_rerun_after_preflight" \
          --evidence "$line"
        ;;
      PR_CI_STALE_RUN_RERUN_REFUSED*)
        # Immediate consumer of the moved-head refusal line: the line is a PM
        # CI-start directive, not a dead end. The current head gets FRESH
        # initial label-gated CI through the typed CI-start path; a superseded
        # run must never be rerun (run_head_mismatch refusal) and
        # superseded-head failures must never dispatch product rework.
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        stale_run="$(printf '%s\n' "$line" | sed -n 's/.*stale_run=\([^ ]*\).*/\1/p')"
        upsert_obligation \
          --kind ci_start_after_moved_head_classification \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr current head unclassified by superseded runs; start fresh label-gated CI" \
          --action "Start FRESH initial label-gated CI for the CURRENT head through the typed CI-start path /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/ci/request-label-gated-ci.sh --pr $pr (UNSUPPORTED_LIFECYCLE_ACTION:pm-review-done stays retired even when review gates pass). No local-preflight proof is required (Rajiv 1786812200.371389); NEVER rerun the superseded run ${stale_run:-<stale-run>} via rerun-after-local-proof.sh (run_head_mismatch refusal) and do not dispatch product rework from superseded-head failures." \
          --blocker "ci_start_after_moved_head_classification" \
          --evidence "$line"
        ;;
      PR_CI_STALE_HEAD_CHURN_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind ci_stale_head_churn \
          --severity medium \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr has stale/superseded CI churn, not current-head proof" \
          --action "Do not dispatch product rework from stale-head failures alone. Once the head is stable, run the affected local test/E2E preflight on the current head before any label-gated CI/E2E retry; if the stale failure is a flake/pre-existing issue, file or cite that follow-up issue first." \
          --blocker "ci_stale_head_churn" \
          --evidence "$line"
        ;;
      PR_DEPENDENCY_UNBLOCKED_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind dependency_unblocked \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr dependency blocker is terminal; resume capture/CI" \
          --action "Resolve the GitHub dependency for PR $pr using the emitted proof, then run UNSUPPORTED_LIFECYCLE_ACTION:reconcile-capacity. The dependency-block marker no longer justifies a passive wait." \
          --blocker "dependency_blocker_terminal" \
          --evidence "$line"
        ;;
      PR_DEPENDENCY_WEDGE_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        age="$(printf '%s\n' "$line" | sed -n 's/.*age_min=\([^ ]*\).*/\1/p')"
        upsert_obligation \
          --kind dependency_wedge \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr dependency watch is missing current-head proof" \
          --action "Do not treat pm-blocked:dependency as a passive wait until the blocker is machine-readable. Return UNSUPPORTED_LIFECYCLE_ACTION:block-pr for the dependency block, or remove pm-blocked:dependency and resume capture/CI. Current dependency label age_min=${age:-unknown}." \
          --blocker "dependency_watch_missing_proof" \
          --evidence "$line"
        ;;
      PR_ACTIVE_REWORK_IDLE_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        slot="$(printf '%s\n' "$line" | sed -n 's/.*slot:\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind rework_slot_idle \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --slot "$slot" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr blocked rework slot is idle" \
          --action "Inspect slot, nudge rework, or move PR to a non-slot-consuming blocked state and free the slot." \
          --blocker "blocked_rework_slot_idle" \
          --evidence "$line"
        ;;
      PR_EXTERNAL_WAIT_SLOT_CONFLICT_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        slot="$(printf '%s\n' "$line" | sed -n 's/.*slot:\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind external_wait_slot_conflict \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --slot "$slot" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr is both product-blocked and actively slot-owned" \
          --action "Decide which state is true. If Rajiv/product answered, resolve the current branch/head/expected epoch/repository/handoff and use the complete Skill(direct-assign) tuple for slot $slot; successful delivery clears pm-blocked:product and resolves product_decision_wait. If still waiting, return UNSUPPORTED_LIFECYCLE_ACTION:block-pr, then release the slot only through Skill(direct-release)." \
          --blocker "product_wait_has_active_owner" \
          --evidence "$line"
        ;;
      PR_PM_REVIEW_SLOT_RELEASE_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        slot="$(printf '%s\n' "$line" | sed -n 's/.*slot:\([0-9][0-9]*\).*/\1/p')"
        issue="$(printf '%s\n' "$line" | sed -n 's/.*issue=#\([0-9][0-9]*\).*/\1/p')"
        family2_output=""
        family2_rc=0
        if [ "$DRY_RUN" != "1" ] && [ -n "$pr" ] && [ -n "$issue" ] && [ -n "$slot" ]; then
          family2_output="$(cd "$CONTROL_PLANE_ROOT" 2>/dev/null && PYTHONPATH="$CONTROL_PLANE_ROOT" python3 -m "$FAMILY2_MODULE" \
            --transition-type pm_review --slot "$slot" --issue "$issue" --pr "$pr" 2>&1)"
          family2_rc=$?
        fi
        if [ "$DRY_RUN" != "1" ] && [ "$family2_rc" -eq 0 ] \
          && printf '%s\n' "$family2_output" | grep -q '"decision": "committed"'; then
          emit "PR_PM_REVIEW_SLOT_RELEASE_APPLIED PR#$pr slot=$slot issue=$issue result=$(printf '%s' "$family2_output" | one_line_excerpt)"
          MUTATED=$((MUTATED+1))
          continue
        fi
        if [ "$DRY_RUN" != "1" ] && [ "$family2_rc" -eq 23 ] \
          && printf '%s\n' "$family2_output" | grep -q '"decision": "committed_deferred"'; then
          # The Python boundary already owns exactly one canonical obligation
          # and retryable outbox effect.  Do not add a legacy projected duty.
          emit "PR_PM_REVIEW_SLOT_RELEASE_DEFERRED PR#$pr slot=$slot issue=$issue result=$(printf '%s' "$family2_output" | one_line_excerpt)"
          MUTATED=$((MUTATED+1))
          continue
        fi
        upsert_obligation \
          --kind pm_review_slot_release \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --slot "${slot:-0}" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr is in PM review but still holds a dev slot" \
          --action "Run the exact Python family2 PM-review boundary once for slot $slot; it releases ownership and performs the successor projection, or retains one typed retryable outbox effect." \
          --blocker "pm_review_pending_holds_dev_slot" \
          --evidence "$line" \
          --evidence "processor_rc=$family2_rc processor_output=$(printf '%s' "$family2_output" | one_line_excerpt)"
        ;;
      PR_STALE_BLOCKER_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        blocker="$(printf '%s\n' "$line" | sed -n 's/.*blocker=\([^ ]*\).*/\1/p')"
        upsert_obligation \
          --kind stale_blocker_label \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr has a stale blocker label" \
          --action "Remove ${blocker:-the stale blocker} or replace it with the current blocker proof; then rerun pr-state-sweep before fresh dispatch." \
          --blocker "stale_blocker_label" \
          --evidence "$line"
        ;;
      PR_STALE_SLOT_LABEL_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        slot="$(printf '%s\n' "$line" | sed -n 's/.*slot_labels=slot:\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind stale_slot_label \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --slot "${slot:-0}" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr has stale slot label hiding blocked rework" \
          --action "Remove the stale slot label, reconcile issue/PR ownership against MoP, then either assign real rework to a free slot or park the PR with a non-slot-consuming blocker." \
          --blocker "blocked_rework_slot_label_mop_mismatch" \
          --evidence "$line"
        ;;
      PR_PM_GATE_REVIEW_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind pm_gate_blocker \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr is PM-gated with no live slot owner" \
          --action "Resolve the PM-gate blocker before fresh issue dispatch: produce/verify the missing artifact or decision proof, assign a narrow forensics packet if needed, or relabel as pm-blocked:product only with the exact Rajiv/product question and PM's recommended default." \
          --blocker "blocked_rework_pm_gate_unassigned" \
          --evidence "$line"
        ;;
      PR_PRODUCT_DECISION_WAITING*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind product_decision_wait \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr is waiting on product/Rajiv decision" \
          --action "Do not treat pm-blocked:product as a quiet parked state without proof. PM must capture or post the exact product question, PM's recommended default, and Rajiv/product response. Once answered, remove pm-blocked:product and rerun pr-state-sweep; until then this remains current PR work ahead of fresh issue dispatch." \
          --blocker "product_decision_wait" \
          --evidence "$line"
        ;;
      PR_DRAFT_ORPHAN_REVIEW_REQUIRED*|PR_STATE_LABEL_REQUIRED*|PR_STATE_RECONCILE_REQUIRED*|PR_CI_CLASSIFICATION_REQUIRED*|PR_CI_DEPENDENCY_BLOCKED_REQUIRED*|PR_DEPENDENCY_UNBLOCKED_REQUIRED*|PR_READY_BEFORE_CI_REQUIRED*|PR_REBASE_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind pr_state_reconcile \
          --severity medium \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --title "PR #$pr needs PM state reconciliation" \
	          --action "Apply only supported transitions before dispatching fresh issue work. For PR_CI_DEPENDENCY_BLOCKED_REQUIRED, the blocker naming is retired (UNSUPPORTED_LIFECYCLE_ACTION:block-pr); wait until the dependency merges/closes before rerunning CI. For PR_READY_BEFORE_CI_REQUIRED, mark the PR ready first, then remove/re-add the CI-trigger label only if required workflows did not start on the current head." \
          --blocker "pr_state_reconcile" \
          --evidence "$line"
        ;;
      PR_MERGE_READY_INVALID_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind merge_ready_invalid \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr merge-ready state is invalid" \
          --action "Demote or reconcile CI/mergeability before merge. For workflow-changing PRs, changed workflow jobs/checks must be green or explicitly waived by Rajiv/CTO." \
          --blocker "merge_ready_invalid" \
          --evidence "$line"
        ;;
      PR_REVIEW_CIRCUIT_BREAKER_REQUIRED*)
        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind review_loop_rescope \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr hit PM rework circuit breaker" \
          --action "Immediately run Skill(pm-pr-rescue) once for the current exact head. The review-cap dispatch freezes the exact tuple and releases/parks the owning slot so it is refillable; rescue runs OFF-SLOT via claude -p and must not hold a numbered slot. Consume its typed terminal: PATCH_READY/NO_PATCH_REQUIRED continue without another ordinary review; validated FAILED enters the existing split/rescope/CTO disposition path. Do not assign another generic rework packet." \
          --blocker "review_loop_rescope_required" \
          --evidence "$line" \
          --evidence pm_stop_actionable
        ;;
	      PR_RESCOPE_REQUIRED*|PR_RESCOPE_EXECUTION_REQUIRED*)
	        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
	        upsert_obligation \
	          --kind review_loop_rescope \
          --severity high \
          --target-type pr \
          --target-id "$pr" \
          --pr "$pr" \
          --owner pm \
          --horizon hourly \
          --title "PR #$pr requires rescope/reimplementation decision" \
          --action "Make the GitHub rescope or reimplementation decision for PR $pr; default broad/repeated churn to a split and escalate only a concrete unresolved product/data-model question." \
	          --blocker "review_loop_rescope_required" \
	          --evidence "$line"
	        ;;
	      PR_OVERRIDE_VERIFICATION_REQUIRED*)
	        pr="$(printf '%s\n' "$line" | sed -n 's/.*PR#\([0-9][0-9]*\).*/\1/p')"
	        upsert_obligation \
	          --kind override_verification \
	          --severity high \
	          --target-type pr \
	          --target-id "$pr" \
	          --pr "$pr" \
	          --owner pm \
	          --horizon hourly \
	          --title "PR #$pr override resolved; proceed to exact-head pm-review-done" \
          --action "Affected-test verification is retired (Rajiv thread 1786811168.455449). The pm-review-done binding is retired (UNSUPPORTED_LIFECYCLE_ACTION:pm-review-done); no Phase-A PASS marker binds CI start." \
	          --blocker "override_verification_required" \
	          --evidence "$line"
	        ;;
      ISSUE_REVIEW_CIRCUIT_BREAKER_REQUIRED*)
        issue="$(printf '%s\n' "$line" | sed -n 's/.*issue#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind review_loop_rescope \
          --severity high \
          --target-type issue \
          --target-id "$issue" \
          --issue "$issue" \
          --owner pm \
          --horizon hourly \
          --title "Issue #$issue hit PM rework circuit breaker before PR" \
          --action "Make the GitHub issue rescope or reimplementation decision for issue $issue; default repeated same-class churn to a split and do not send another generic same-issue review/rework round." \
          --blocker "issue_review_loop_rescope_required" \
          --evidence "$line"
        ;;
      ISSUE_RESCOPE_REQUIRED*|ISSUE_RESCOPE_EXECUTION_REQUIRED*)
        issue="$(printf '%s\n' "$line" | sed -n 's/.*issue#\([0-9][0-9]*\).*/\1/p')"
        upsert_obligation \
          --kind review_loop_rescope \
          --severity high \
          --target-type issue \
          --target-id "$issue" \
          --issue "$issue" \
          --owner pm \
          --horizon hourly \
          --title "Issue #$issue requires rescope/reimplementation decision" \
          --action "Make the GitHub issue rescope or reimplementation decision for issue $issue; default broad/repeated churn to a split and escalate only a concrete unresolved product/data-model question." \
          --blocker "issue_review_loop_rescope_required" \
          --evidence "$line"
        ;;
	    esac
	  done <<< "$ACTION_LINES"

  if sentinel_write_enabled; then
    write_sentinel "$ACTION_LINES"
  fi
  emit "PR_SWEEP_ACTIONABLE count=$ACTION_COUNT mutated=$MUTATED"
  exit 0
fi

resolve_sentinel_if_clean
write_clean_proof
emit "PR_SWEEP_CLEAN actionable=0"
exit 0
