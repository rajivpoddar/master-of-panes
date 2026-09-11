#!/usr/bin/env bash
# pm-state-replace.sh — atomically transition a PR's PM state label.
#
# Per Rajiv directive 2026-05-20 11:48 IST (Slack thread 1779250700.624619 in
# C0ALZJHGE49): PR labels are the canonical PR state surface. `pm-state:*`
# labels MUST be mutually exclusive on a PR. Non-terminal workflow states use
# exactly one `pm-state:*` label. Release readiness uses the standalone
# `merge-ready` label and must not coexist with any `pm-state:*` label.
#
# Usage:
#   pm-state-replace.sh <PR> <new-state-suffix>
#
# Example:
#   pm-state-replace.sh 4738 qa-passed-awaiting-ci
#   pm-state-replace.sh 4738 merge-ready
#   pm-state-replace.sh 4738 merged-cleanup-pending
#
# Behavior:
#   1. Reads current pm-state:* labels on the PR.
#   2. Removes ALL existing pm-state:* labels and stale merge-ready label drift.
#   3. Adds the new pm-state:<suffix> label, except merge-ready which adds
#      the standalone `merge-ready` label.
#   4. Verifies post-state count is exactly 1 effective state.
#   5. Logs the transition to /tmp/pm-state-transitions.log (override with
#      PM_STATE_TRANSITIONS_LOG; the override must be an absolute path).
#   6. Exits 1 on count != 1 (drift not resolved); exits 0 on success.
#
# Idempotent: repeated calls with the same target state are no-ops.  For the
# CI-triggering state, the durable exact-head O_EXCL reservation also makes a
# second caller fail closed instead of toggling the label and starting a
# duplicate paid cycle.
#
# Requires: gh CLI authenticated, jq.
#
# `qa-passed-awaiting-ci` is a GitHub label event that starts CI/E2E. That
# transition is therefore guarded by the canonical exact-head readiness gate;
# callers cannot bypass local/capture proof by invoking this low-level script.

set -uo pipefail
export PATH="${PATH:-}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
CONTROL_PLANE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ $# -ne 2 ]; then
  echo "ERROR: usage: $0 <PR> <new-state-suffix>" >&2
  echo "  e.g. $0 4738 qa-passed-awaiting-ci" >&2
  exit 2
fi

PR="$1"
SUFFIX="$2"
REPO="${GH_REPO:-heydonna-app/heydonna-app}"
NEW_LABEL="pm-state:${SUFFIX}"
if [ "$SUFFIX" = "merge-ready" ]; then
  NEW_LABEL="merge-ready"
fi
# Transition audit log. Overridable for hermetic tests and alternate PM
# runtimes, defaulting to the production path. An unsafe override is refused
# rather than silently appending live audit state somewhere unexpected.
LOG_FILE="${PM_STATE_TRANSITIONS_LOG:-/tmp/pm-state-transitions.log}"
case "$LOG_FILE" in
  /*) ;;
  *)
    echo "ERROR: PM_STATE_TRANSITIONS_LOG must be an absolute path: ${LOG_FILE}" >&2
    exit 2
    ;;
esac
case "$LOG_FILE" in
  *$'\n'*)
    echo "ERROR: PM_STATE_TRANSITIONS_LOG must not contain newlines" >&2
    exit 2
    ;;
esac
CI_READY_GATE="${CI_READY_GATE:-/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/pr-ci-readiness-gate.py}"
ACTOR="${USER:-pm}"
TS="$(date -Iseconds)"
# `pm-state:qa-passed-awaiting-ci` is a paid CI/E2E trigger.  The five-minute
# head-age guard prevents a moving head from being admitted too quickly, but it
# does not serialize two callers that both observe the same pre-label state.
# Keep the receipt root configurable for tests and alternate PM runtimes; it
# defaults to the durable PM state area rather than /tmp so a restart cannot
# silently re-arm an already-admitted head.
CI_CYCLE_RECEIPT_ROOT="${PM_CI_CYCLE_RECEIPT_ROOT:-${HOME:-/tmp}/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/ci-cycle-receipts}"
# Lease bound for the sanctioned pm-review-done re-entry claim. A claim older
# than this while the PR is still pm-review-pending is treated as a crashed
# pre-mutation attempt and is recoverable (taken over) on retry; a fresh
# claim serializes concurrent callers and refuses duplicates.
REENTRY_CLAIM_STALE_SECONDS="${REENTRY_CLAIM_STALE_SECONDS:-600}"

ci_cycle_receipt_path() {
  local head="$1"
  printf '%s/pr-%s-%s.json\n' "$CI_CYCLE_RECEIPT_ROOT" "$PR" "$head"
}

ci_cycle_reentry_claim_path() {
  local head="$1"
  # Distinct atomic claim for the sanctioned pm-review-done re-entry path.
  # The base receipt is intentionally idempotent for an already-admitted
  # head; this O_EXCL claim serializes two concurrent re-entry callers that
  # both observe the same pre-label state so only one re-applies the CI
  # label (one fresh wave).
  printf '%s/pr-%s-%s.reentry\n' "$CI_CYCLE_RECEIPT_ROOT" "$PR" "$head"
}

# A pre-existing re-entry claim is recoverable only when the previous attempt
# demonstrably died BEFORE any label mutation: the PR is still
# pm-state:pm-review-pending AND the claim is older than the stale lease
# bound. A fresh claim means another sanctioned caller is in-flight and must
# be refused; a qa-passed-awaiting-ci state means the wave already fired and
# the claim is the completed marker.
reentry_claim_is_recoverable() {
  local claim="$1" claim_phase claim_source claimed_at age_s state_json
  [ -f "$claim" ] || return 1
  read -r claim_phase claim_source < <(python3 - "$claim" <<'PY' 2>/dev/null || true
import json
import sys
from pathlib import Path
try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(value.get("phase") or "", value.get("rearmSource") or "")
except Exception:
    pass
PY
)
  # Only a durable pre-trigger claim is eligible for stale pre-effect cleanup.
  # effect-attempt and ambiguous-final-post are permanently fail-closed, even
  # if a later read reports the old pending state or the lease has aged out.
  [ "$claim_phase" = "pre-trigger" ] || return 1
  if [ "${PM_CI_GATE_SOURCE:-slot-ready}" = "cto-wave" ]; then
    [ "$claim_source" = "cto-wave" ] || return 1
  else
    [ "$claim_source" != "cto-wave" ] || return 1
  fi
  claimed_at="$(python3 - "$claim" <<'PY' 2>/dev/null || true
import json
import sys
from pathlib import Path
try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(value.get("claimedAt") or "")
except Exception:
    pass
PY
)"
  [ -n "$claimed_at" ] || return 1
  age_s="$(python3 - "$claimed_at" <<'PY' 2>/dev/null || true
import sys
from datetime import datetime, timezone
try:
    ts = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
    print(int((datetime.now(timezone.utc) - ts).total_seconds()))
except Exception:
    pass
PY
)"
  [[ "$age_s" =~ ^[0-9]+$ ]] || return 1
  [ "$age_s" -gt "${REENTRY_CLAIM_STALE_SECONDS:-600}" ] || return 1
  state_json="$(current_state_labels 2>/dev/null || true)"
  if printf '%s\n' "$state_json" | jq -r '.[]' 2>/dev/null | grep -qx 'pm-state:pm-review-pending'; then
    return 0
  fi
  # A CTO-wave re-arm starts from the already-admitted state. A stale claim
  # is recoverable only while that state remains present and the exact marker
  # has not reached the PR; a completed marker therefore remains terminal.
  if [ "${PM_CI_GATE_SOURCE:-slot-ready}" = "cto-wave" ] \
    && printf '%s\n' "$state_json" | jq -r '.[]' 2>/dev/null | grep -qx 'pm-state:qa-passed-awaiting-ci'; then
    local current_head expected_marker receipt claim_phase
    claim_phase="$(python3 - "$claim" <<'PY' 2>/dev/null || true
import json
import sys
from pathlib import Path
try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(value.get("phase") or "")
except Exception:
    pass
PY
)"
    [ "$claim_phase" = "pre-trigger" ] || return 1
    current_head="$(gh pr view "$PR" --repo "$REPO" --json headRefOid --jq '.headRefOid' 2>/dev/null || true)"
    [ -n "$current_head" ] || return 1
    expected_marker="ci-head:${current_head}"
    receipt="$(ci_cycle_receipt_path "$current_head")"
    if ! current_ci_head_markers | jq -e --arg marker "$expected_marker" 'index($marker) == null' >/dev/null 2>&1; then
      [ -e "$receipt" ] \
        && valid_ci_cycle_receipt "$receipt" "$current_head" \
        && [ "$(ci_cycle_receipt_status "$receipt")" = "reserved" ] || return 1
    fi
    return 0
  fi
  return 1
}

# A completed CTO-wave label write can leave a final-trigger claim behind if
# the post-state verification crashed after the receipt became
# label-accepted. Reacquire only that exact, fully reverified tuple; reserved
# receipts and other ambiguous phases remain fenced.
reentry_claim_is_completed_cto_wave_recoverable() {
  local claim="$1" head="$2" claim_phase claimed_at age_s state_json markers receipt
  [ "${PM_CI_GATE_SOURCE:-slot-ready}" = "cto-wave" ] || return 1
  [ -f "$claim" ] || return 1
  claim_phase="$(python3 - "$claim" <<'PY' 2>/dev/null || true
import json
import sys
from pathlib import Path
try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(value.get("phase") or "")
except Exception:
    pass
PY
)"
  if [ "$claim_phase" = "completed" ] || [ "$claim_phase" = "final-trigger" ]; then
    claimed_at="$(python3 - "$claim" "$claim_phase" <<'PY' 2>/dev/null || true
import json
import sys
from pathlib import Path
try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    field = "completedAt" if sys.argv[2] == "completed" else "finalTriggerAt"
    print(value.get(field) or "")
except Exception:
    pass
PY
)"
    age_s="$(python3 - "$claimed_at" <<'PY' 2>/dev/null || true
import sys
from datetime import datetime, timezone
try:
    ts = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
    print(int((datetime.now(timezone.utc) - ts).total_seconds()))
except Exception:
    pass
PY
)"
    [[ "$age_s" =~ ^[0-9]+$ ]] || return 1
    [ "$age_s" -gt "${REENTRY_CLAIM_STALE_SECONDS:-600}" ] || return 1
  else
    return 1
  fi
  state_json="$(current_state_labels 2>/dev/null || true)"
  printf '%s\n' "$state_json" | jq -e 'length == 1 and .[0] == "pm-state:qa-passed-awaiting-ci"' >/dev/null 2>&1 || return 1
  [ "$(gh pr view "$PR" --repo "$REPO" --json headRefOid --jq '.headRefOid' 2>/dev/null || true)" = "$head" ] || return 1
  markers="$(current_ci_head_markers 2>/dev/null || true)"
  printf '%s\n' "$markers" | jq -e --arg marker "ci-head:${head}" 'index($marker) != null' >/dev/null 2>&1 || return 1
  receipt="$(ci_cycle_receipt_path "$head")"
  [ -e "$receipt" ] && valid_ci_cycle_receipt "$receipt" "$head" || return 1
  if [ "$claim_phase" = "final-trigger" ] \
    && [ "$(ci_cycle_receipt_source "$receipt")" != "cto-wave" ]; then
    return 1
  fi
  [ "$(ci_cycle_receipt_status "$receipt")" = "label-accepted" ] || return 1
  return 0
}

reentry_claim_takeover_cto_wave() {
  local claim="$1" head="$2" actor="$3"
  python3 - "$claim" "$PR" "$head" "$actor" "${REENTRY_CLAIM_STALE_SECONDS:-600}" <<'PY' 2>/dev/null
import fcntl
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
path = Path(sys.argv[1])
lock_fd = os.open(str(path) + ".recovery.lock", os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(lock_fd, fcntl.LOCK_EX)
try:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        value.get("schema") != "pm-ci-cycle-reentry/v1"
        or value.get("pr") != int(sys.argv[2])
        or value.get("headRefOid") != sys.argv[3]
        or value.get("phase") != "pre-trigger"
    ):
        raise SystemExit(1)
    claimed_at = value.get("claimedAt") or ""
    claimed = datetime.fromisoformat(claimed_at.replace("Z", "+00:00"))
    age_s = (datetime.now(timezone.utc) - claimed).total_seconds()
    if age_s <= int(sys.argv[5]):
        raise SystemExit(1)
    value.update({
        "actor": sys.argv[4],
        "claimedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "claimGeneration": f"pid:{os.getpid()}:{time.time_ns()}",
    })
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    os.close(lock_fd)
PY
}

reentry_claim_acquire() {
  local claim="$1" head="$2" actor="$3" expected_generation
  if [ "${PM_CI_GATE_SOURCE:-slot-ready}" = "cto-wave" ] \
    && [ "${CTO_WAVE_EMPTY_RECOVERY:-0}" = "1" ]; then
    if python3 - "$claim" "$PR" "$head" <<'PY'
import fcntl
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
lock_path = Path(sys.argv[1] + ".recovery.lock")
lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(lock_fd, fcntl.LOCK_EX)
try:
    path = Path(sys.argv[1])
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        value.get("schema") != "pm-ci-cycle-reentry/v1"
        or value.get("pr") != int(sys.argv[2])
        or value.get("headRefOid") != sys.argv[3]
        or value.get("phase") != "pre-trigger"
    ):
        raise SystemExit(1)
    value["phase"] = "recovery-claimed"
    value["recoveryOwner"] = f"pid:{os.getpid()}"
    value["recoveryClaimedAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    value["claimGeneration"] = f"pid:{os.getpid()}:{time.time_ns()}"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
finally:
    os.close(lock_fd)
PY
    then
      return 0
    fi
    return 1
  fi
if python3 - "$claim" "$PR" "$head" "$actor" <<'PY' 2>/dev/null
import json
import os
import sys
import time
from datetime import datetime, timezone

payload = {
    "schema": "pm-ci-cycle-reentry/v1",
    "pr": int(sys.argv[2]),
    "headRefOid": sys.argv[3],
    "label": "pm-state:qa-passed-awaiting-ci",
    "actor": sys.argv[4],
    "phase": "pre-trigger",
    "claimedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "claimGeneration": f"pid:{os.getpid()}:{time.time_ns()}",
    "rearmSource": os.environ.get("PM_CI_GATE_SOURCE") if os.environ.get("PM_CI_GATE_SOURCE") in {"pm-review-done", "cto-wave"} else "",
}
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as stream:
    json.dump(payload, stream, sort_keys=True)
    stream.write("\n")
PY
  then
    return 0
  fi
  if reentry_claim_is_completed_cto_wave_recoverable "$claim" "$head"; then
    if python3 - "$claim" "$PR" "$head" "$actor" "${REENTRY_CLAIM_STALE_SECONDS:-600}" <<'PY'
import fcntl
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
path = Path(sys.argv[1])
lock_fd = os.open(str(path) + ".recovery.lock", os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(lock_fd, fcntl.LOCK_EX)
try:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        value.get("schema") != "pm-ci-cycle-reentry/v1"
        or value.get("pr") != int(sys.argv[2])
        or value.get("headRefOid") != sys.argv[3]
        or value.get("phase") not in {"final-trigger", "completed"}
    ):
        raise SystemExit(1)
    timestamp = value.get("completedAt") if value.get("phase") == "completed" else value.get("finalTriggerAt")
    if timestamp:
        marked = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if (datetime.now(timezone.utc) - marked).total_seconds() <= int(sys.argv[5]):
            raise SystemExit(1)
    value.update({
        "actor": sys.argv[4],
        "phase": "pre-trigger",
        "claimedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "claimGeneration": f"pid:{os.getpid()}:{time.time_ns()}",
    })
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
finally:
    os.close(lock_fd)
PY
    then
      return 0
    fi
    return 1
  fi
  if [ "${PM_CI_GATE_SOURCE:-slot-ready}" = "cto-wave" ] \
    && reentry_claim_is_recoverable "$claim" "$head"; then
    # The stale check is repeated under the advisory lock; never unlink a
    # claim before that locked rewrite, or two callers can both take it over.
    if reentry_claim_takeover_cto_wave "$claim" "$head" "$actor"; then
      return 0
    fi
    return 1
  fi
  # A stale pm-review-done claim is recoverable only through a locked CAS that
  # re-reads the exact old generation and live preimage. It never unlinks a
  # claim outside that lock.
  if [ "${PM_CI_GATE_SOURCE:-slot-ready}" = "pm-review-done" ] \
    && reentry_claim_is_recoverable "$claim" "$head"; then
    expected_generation="$(reentry_claim_generation "$claim")"
    if reentry_claim_takeover_pm_review_done "$claim" "$head" "$actor" "$expected_generation"; then
      return 0
    fi
  fi
  if reentry_claim_is_recoverable "$claim" "$head"; then
    return 1
  fi
  return 1
}

reentry_claim_takeover_pm_review_done() {
  local claim="$1" head="$2" actor="$3" expected_generation="$4" receipt
  receipt="$(ci_cycle_receipt_path "$head")"
  python3 - "$claim" "$PR" "$REPO" "$head" "$actor" "$expected_generation" "${REENTRY_CLAIM_STALE_SECONDS:-600}" "$receipt" "${TEST_TAKEOVER_PAUSE_FILE:-}" "${TEST_TAKEOVER_RELEASE_FILE:-}" <<'PY' 2>/dev/null
import fcntl
import json
import os
import sys
import time
from datetime import datetime, timezone
import subprocess
from pathlib import Path

path = Path(sys.argv[1])
lock_fd = os.open(str(path) + ".recovery.lock", os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(lock_fd, fcntl.LOCK_EX)
def record(event):
    target = os.environ.get("TEST_DURABILITY_LOG")
    if target:
        with open(target, "a", encoding="utf-8") as stream:
            stream.write(event + "\n")
try:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        value.get("schema") != "pm-ci-cycle-reentry/v1"
        or value.get("pr") != int(sys.argv[2])
        or value.get("headRefOid") != sys.argv[4]
        or value.get("phase") != "pre-trigger"
        or value.get("rearmSource") != "pm-review-done"
        or not value.get("claimGeneration")
        or value.get("claimGeneration") != sys.argv[6]
    ):
        raise SystemExit(1)
    claimed = datetime.fromisoformat((value.get("claimedAt") or "").replace("Z", "+00:00"))
    if (datetime.now(timezone.utc) - claimed).total_seconds() <= int(sys.argv[7]):
        raise SystemExit(1)

    def gh(*args: str) -> str:
        result = subprocess.run(["gh", *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode != 0:
            raise SystemExit(1)
        return result.stdout

    if gh("pr", "view", sys.argv[2], "--repo", sys.argv[3], "--json", "headRefOid", "--jq", ".headRefOid").strip() != sys.argv[4]:
        raise SystemExit(1)
    issue = json.loads(gh("api", f"repos/{sys.argv[3]}/issues/{sys.argv[2]}"))
    labels = [item.get("name", "") for item in issue.get("labels", [])]
    if [label for label in labels if label.startswith("pm-state:") or label == "merge-ready"] != ["pm-state:pm-review-pending"]:
        raise SystemExit(1)
    if f"ci-head:{sys.argv[4]}" in labels:
        raise SystemExit(1)
    pause, release = sys.argv[9], sys.argv[10]
    if pause and release:
        Path(pause).touch()
        for _ in range(500):
            if Path(release).exists():
                break
            time.sleep(0.01)
        else:
            raise SystemExit(1)
    receipt = json.loads(Path(sys.argv[8]).read_text(encoding="utf-8"))
    if (
        receipt.get("schema") != "pm-ci-cycle-admission/v1"
        or receipt.get("pr") != int(sys.argv[2])
        or receipt.get("headRefOid") != sys.argv[4]
        or receipt.get("mode") != "initial"
        or receipt.get("label") != "pm-state:qa-passed-awaiting-ci"
        or receipt.get("workflows") != ["CI", "E2E Smoke Tests"]
        or receipt.get("status") not in {"reserved", "label-accepted"}
    ):
        raise SystemExit(1)
    value.update({
        "actor": sys.argv[5],
        "claimedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "claimGeneration": f"pid:{os.getpid()}:{time.time_ns()}",
        "rearmSource": "pm-review-done",
    })
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
        record("takeover:file-fsync")
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
        record("takeover:directory-fsync")
    finally:
        os.close(directory_fd)
finally:
    os.close(lock_fd)
PY
}

reentry_claim_mark_final_trigger_attempt() {
  local claim="$1"
  python3 - "$claim" "$PR" "$CI_HEAD" "${REENTRY_GENERATION:-}" <<'PY'
import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
lock_fd = os.open(str(path) + ".recovery.lock", os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(lock_fd, fcntl.LOCK_EX)
value = json.loads(path.read_text(encoding="utf-8"))
if (
    value.get("schema") != "pm-ci-cycle-reentry/v1"
    or value.get("pr") != int(sys.argv[2])
    or value.get("headRefOid") != sys.argv[3]
    or value.get("claimGeneration") != sys.argv[4]
    or value.get("phase") not in {"pre-trigger", "recovery-claimed"}
):
    raise SystemExit(1)
value["phase"] = "final-trigger"
value["finalTriggerAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
with temporary.open("x", encoding="utf-8") as stream:
    json.dump(value, stream, sort_keys=True)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
os.replace(temporary, path)
directory_fd = os.open(path.parent, os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
os.close(lock_fd)
PY
}

# Fence the pm-review-done effect edge before invoking the final combined
# label POST. A process crash after this durable rewrite but before (or during)
# the request must never be mistaken for a pre-effect stale claim.
reentry_claim_mark_effect_attempt() {
  local claim="$1"
  python3 - "$claim" "$PR" "$CI_HEAD" "${REENTRY_GENERATION:-}" <<'PY' 2>/dev/null
import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
path = Path(sys.argv[1])
lock_fd = os.open(str(path) + ".recovery.lock", os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(lock_fd, fcntl.LOCK_EX)
def record(event):
    target = os.environ.get("TEST_DURABILITY_LOG")
    if target:
        with open(target, "a", encoding="utf-8") as stream:
            stream.write(event + "\n")
try:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        value.get("schema") != "pm-ci-cycle-reentry/v1"
        or value.get("pr") != int(sys.argv[2])
        or value.get("headRefOid") != sys.argv[3]
        or value.get("claimGeneration") != sys.argv[4]
        or value.get("rearmSource") == "cto-wave"
        or value.get("phase") not in {"pre-trigger", "recovery-claimed"}
    ):
        raise SystemExit(1)
    value["phase"] = "effect-attempt"
    value["effectAttemptAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
        record("effect-attempt:file-fsync")
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
        record("effect-attempt:directory-fsync")
    finally:
        os.close(directory_fd)
finally:
    os.close(lock_fd)
PY
}

# A nonzero response from the final combined label POST is ambiguous: GitHub
# may have accepted the mutation before the client lost the response.  Retain
# the claim as a durable replay fence while recording that ambiguity so a
# later caller cannot reacquire it as a fresh paid wave.
reentry_claim_mark_ambiguous() {
  local claim="$1"
  python3 - "$claim" "$PR" "$CI_HEAD" "${REENTRY_GENERATION:-}" <<'PY' 2>/dev/null
import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
path = Path(sys.argv[1])
lock_fd = os.open(str(path) + ".recovery.lock", os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(lock_fd, fcntl.LOCK_EX)
try:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        value.get("schema") != "pm-ci-cycle-reentry/v1"
        or value.get("pr") != int(sys.argv[2])
        or value.get("headRefOid") != sys.argv[3]
        or value.get("claimGeneration") != sys.argv[4]
        or value.get("phase") not in {"pre-trigger", "recovery-claimed", "effect-attempt", "ambiguous-final-post"}
    ):
        raise SystemExit(1)
    value["phase"] = "ambiguous-final-post"
    value["ambiguousAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
finally:
    os.close(lock_fd)
PY
}

reentry_claim_mark_completed() {
  local claim="$1"
  python3 - "$claim" "$PR" "$CI_HEAD" "${REENTRY_GENERATION:-}" <<'PY'
import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
lock_fd = os.open(str(path) + ".recovery.lock", os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(lock_fd, fcntl.LOCK_EX)
value = json.loads(path.read_text(encoding="utf-8"))
if (
    value.get("schema") != "pm-ci-cycle-reentry/v1"
    or value.get("pr") != int(sys.argv[2])
    or value.get("headRefOid") != sys.argv[3]
    or value.get("claimGeneration") != sys.argv[4]
    or value.get("phase") != "final-trigger"
):
    raise SystemExit(1)
value["phase"] = "completed"
value["completedAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
with temporary.open("x", encoding="utf-8") as stream:
    json.dump(value, stream, sort_keys=True)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
os.replace(temporary, path)
directory_fd = os.open(path.parent, os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
os.close(lock_fd)
PY
}

reentry_claim_mark_superseded() {
  local claim="$1" next_head="$2"
  python3 - "$claim" "$PR" "$CI_HEAD" "$next_head" "${REENTRY_GENERATION:-}" <<'PY'
import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
lock_fd = os.open(str(path) + ".recovery.lock", os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(lock_fd, fcntl.LOCK_EX)
value = json.loads(path.read_text(encoding="utf-8"))
if (
    value.get("schema") != "pm-ci-cycle-reentry/v1"
    or value.get("pr") != int(sys.argv[2])
    or value.get("headRefOid") != sys.argv[3]
    or value.get("claimGeneration") != sys.argv[5]
    or value.get("phase") != "recovery-claimed"
):
    raise SystemExit(1)
value["phase"] = "superseded"
value["supersededToHead"] = sys.argv[4]
value["supersededAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
with temporary.open("x", encoding="utf-8") as stream:
    json.dump(value, stream, sort_keys=True)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
os.replace(temporary, path)
directory_fd = os.open(path.parent, os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
os.close(lock_fd)
PY
}

reentry_claim_generation() {
  local claim="$1"
  python3 - "$claim" <<'PY' 2>/dev/null || true
import json
import sys
from pathlib import Path
try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(value.get("claimGeneration") or "")
except Exception:
    pass
PY
}

reentry_claim_assert_generation() {
  local claim="$1" head="$2" generation="$3"
  [ -n "$generation" ] || return 1
  python3 - "$claim" "$PR" "$head" "$generation" <<'PY' 2>/dev/null
import fcntl
import json
import os
import sys
from pathlib import Path
path = Path(sys.argv[1])
lock_fd = os.open(str(path) + ".recovery.lock", os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(lock_fd, fcntl.LOCK_EX)
try:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        value.get("schema") != "pm-ci-cycle-reentry/v1"
        or value.get("pr") != int(sys.argv[2])
        or value.get("headRefOid") != sys.argv[3]
        or value.get("claimGeneration") != sys.argv[4]
        or value.get("phase") not in {"pre-trigger", "recovery-claimed", "final-trigger"}
    ):
        raise SystemExit(1)
finally:
    os.close(lock_fd)
PY
}

valid_ci_cycle_receipt() {
  local receipt="$1"
  local head="$2"
  [ -r "$receipt" ] || return 1
  python3 - "$receipt" "$PR" "$head" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
expected_pr = int(sys.argv[2])
expected_head = sys.argv[3]
try:
    value = json.loads(path.read_text(encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)
if (
    value.get("schema") != "pm-ci-cycle-admission/v1"
    or value.get("pr") != expected_pr
    or value.get("headRefOid") != expected_head
    or value.get("mode") != "initial"
    or value.get("label") != "pm-state:qa-passed-awaiting-ci"
    or value.get("workflows") != ["CI", "E2E Smoke Tests"]
    or value.get("status") not in {"reserved", "label-accepted"}
):
    raise SystemExit(1)
PY
}

ci_cycle_receipt_source() {
  local receipt="$1"
  python3 - "$receipt" <<'PY'
import json
import sys
from pathlib import Path
try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(value.get("source") or "")
except (OSError, ValueError, TypeError):
    pass
PY
}

ci_cycle_receipt_status() {
  local receipt="$1"
  python3 - "$receipt" <<'PY'
import json
import sys
from pathlib import Path
try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(value.get("status") or "")
except (OSError, ValueError, TypeError):
    pass
PY
}

prepare_ci_cycle_receipt_root() {
  mkdir -p "$CI_CYCLE_RECEIPT_ROOT" || {
    echo "ERROR: cannot create CI cycle receipt root: ${CI_CYCLE_RECEIPT_ROOT}" >&2
    return 1
  }
  [ -w "$CI_CYCLE_RECEIPT_ROOT" ] || {
    echo "ERROR: CI cycle receipt root is not writable: ${CI_CYCLE_RECEIPT_ROOT}" >&2
    return 1
  }
}

reserve_ci_cycle_receipt() {
  local head="$1"
  local receipt
  receipt="$(ci_cycle_receipt_path "$head")"
  if [ -e "$receipt" ]; then
    valid_ci_cycle_receipt "$receipt" "$head"
    return $?
  fi
  # Create the final path with O_EXCL.  A concurrent runtime or a crash during
  # the write cannot replace a receipt for the same exact head; an incomplete
  # receipt is intentionally fail-closed on the next admission attempt.
  if ! python3 - "$receipt" "$PR" "$head" "$ACTOR" "${PM_CI_GATE_SOURCE:-slot-ready}" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

payload = {
    "schema": "pm-ci-cycle-admission/v1",
    "pr": int(sys.argv[2]),
    "headRefOid": sys.argv[3],
    "mode": "initial",
    "label": "pm-state:qa-passed-awaiting-ci",
    "workflows": ["CI", "E2E Smoke Tests"],
    "status": "reserved",
    "actor": sys.argv[4],
    "source": sys.argv[5],
    "reservedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
}
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as stream:
    json.dump(payload, stream, sort_keys=True)
    stream.write("\n")
PY
  then
    return 1
  fi
  valid_ci_cycle_receipt "$receipt" "$head"
}

mark_ci_cycle_label_accepted() {
  local head="$1"
  local receipt
  receipt="$(ci_cycle_receipt_path "$head")"
  # Preserve the O_EXCL reservation as the fail-closed authority.  Updating
  # its audit status is atomic; if this process dies before the replace, the
  # still-valid `reserved` receipt continues to block same-head re-admission.
  python3 - "$receipt" "$PR" "$head" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
expected_pr = int(sys.argv[2])
expected_head = sys.argv[3]
value = json.loads(path.read_text(encoding="utf-8"))
if (
    value.get("schema") != "pm-ci-cycle-admission/v1"
    or value.get("pr") != expected_pr
    or value.get("headRefOid") != expected_head
    or value.get("status") not in {"reserved", "label-accepted"}
):
    raise SystemExit(1)
if value.get("status") == "label-accepted":
    # Already finalized for this exact head (sanctioned pm-review-done
    # reentry re-admits an accepted receipt): idempotent no-op. Never
    # rewrite or downgrade an existing accepted receipt.
    raise SystemExit(0)
value["status"] = "label-accepted"
value["labelAcceptedAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
with temporary.open("x", encoding="utf-8") as stream:
    json.dump(value, stream, sort_keys=True)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
os.replace(temporary, path)
directory_fd = os.open(path.parent, os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
  valid_ci_cycle_receipt "$receipt" "$head"
}

ci_head_marker_label() {
  local head="$1"
  printf 'ci-head:%s\n' "$head"
}

current_ci_head_markers() {
  gh api "repos/${REPO}/issues/${PR}" --jq '[.labels[] | select(.name | startswith("ci-head:")) | .name]' 2>/dev/null
}

remove_ci_head_markers() {
  local markers label encoded_label
  markers="$(current_ci_head_markers)" || {
    echo "ERROR: cannot read CI head markers for PR #${PR}" >&2
    return 1
  }
  while IFS= read -r label; do
    [ -z "$label" ] && continue
    encoded_label="$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$label")"
    gh api --method DELETE "repos/${REPO}/issues/${PR}/labels/${encoded_label}" >/dev/null 2>&1 || {
      echo "ERROR: cannot remove stale CI head marker ${label} from PR #${PR}" >&2
      return 1
    }
  done < <(printf '%s\n' "${markers:-[]}" | jq -r '.[]')
}

publish_ci_head_marker() {
  local head="$1" state="${2:-}"
  local label encoded_label description payload
  label="$(ci_head_marker_label "$head")"
  encoded_label="$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$label")"
  description="Exact-head paid CI admission for PR #${PR}"

  # Dynamic head labels make the later `labeled` event self-contained: the
  # event snapshot must contain ci-head:<event-head> before any paid job can be
  # scheduled. Creating an already-existing repository label is harmless, but
  # every other API error remains fail-closed.
  if ! gh api "repos/${REPO}/labels/${encoded_label}" >/dev/null 2>&1; then
    if ! gh api --method POST "repos/${REPO}/labels" \
      -f "name=${label}" -f color=1d76db -f "description=${description}" >/dev/null 2>&1; then
      gh api "repos/${REPO}/labels/${encoded_label}" >/dev/null 2>&1 || {
        echo "ERROR: cannot create CI head marker label ${label}" >&2
        return 1
      }
    fi
  fi
  if [ -n "$state" ]; then
    # GitHub emits the paid-workflow `labeled` event from this request.  Carry
    # the exact-head marker in the same label mutation so the event snapshot
    # cannot observe the state label before ci-head is materialized.
    payload="$(jq -cn --arg marker "$label" --arg state "$state" '{labels:[$marker,$state]}')"
  else
    payload="$(jq -cn --arg marker "$label" '{labels:[$marker]}')"
  fi
  if ! printf '%s\n' "$payload" \
    | gh api --method POST "repos/${REPO}/issues/${PR}/labels" --input - >/dev/null 2>&1; then
    echo "ERROR: cannot apply CI head marker ${label}${state:+ with ${state}} to PR #${PR}" >&2
    return 1
  fi
}

current_state_labels() {
  gh api "repos/${REPO}/issues/${PR}" --jq '[.labels[] | select((.name | startswith("pm-state:")) or .name == "merge-ready") | .name]' 2>/dev/null
}

# Validate suffix is from the allowed set (catches typos like "qa-passed-await-ci")
ALLOWED=(
  draft-qa-needed
  qa-running
  qa-failed-rework
  qa-passed-awaiting-ci
  pm-review-pending
  blocked-rework
  rescope-required
  merge-ready
  merged-cleanup-pending
  closed-clean
)
VALID=0
for s in "${ALLOWED[@]}"; do
  if [ "$s" = "$SUFFIX" ]; then VALID=1; break; fi
done
if [ "$VALID" -ne 1 ]; then
  echo "ERROR: '$SUFFIX' not in allowed pm-state set: ${ALLOWED[*]}" >&2
  exit 2
fi

PM_STATE_AUTHORITY_MODE="${PM_STATE_AUTHORITY_MODE:-legacy-labels}"
case "$PM_STATE_AUTHORITY_MODE" in
  legacy-labels)
    ;;
  github-cas)
    if [ "${PM_CI_GATE_SOURCE:-slot-ready}" = "cto-wave" ]; then
      echo "ERROR: cto-wave re-arm requires the existing legacy-label state writer" >&2
      exit 1
    fi
    GITHUB_CAS_CLI="${PM_STATE_GITHUB_CAS_CLI:-$CONTROL_PLANE_DIR/github-pm-state-cli.py}"
    AUTHORIZATION_RECEIPT_ID="${PM_STATE_AUTHORIZATION_RECEIPT_ID:-}"
    EXPECTED_HEAD="${PM_STATE_EXPECTED_HEAD:-}"
    GITHUB_APP_ID="${PM_STATE_GITHUB_APP_ID:-}"
    GITHUB_INSTALLATION_ID="${PM_STATE_GITHUB_INSTALLATION_ID:-}"
    GITHUB_APP_ACTOR="${PM_STATE_GITHUB_APP_ACTOR:-}"
    GITHUB_TOKEN_FILE="${PM_STATE_GITHUB_INSTALLATION_TOKEN_FILE:-}"
    GITHUB_LOCK_ROOT="${PM_STATE_GITHUB_LOCK_ROOT:-/tmp/heydonna-control-plane/github-pm-state-locks}"
    GITHUB_API_BASE_URL="${PM_STATE_GITHUB_API_BASE_URL:-https://api.github.com}"
    GITHUB_CI_READY_GATE="${PM_STATE_GITHUB_CI_READY_GATE:-$CI_READY_GATE}"

    [ -x "$GITHUB_CAS_CLI" ] || {
      echo "ERROR: GitHub PM-state CAS CLI missing or not executable: $GITHUB_CAS_CLI" >&2
      exit 1
    }
    [ -n "$AUTHORIZATION_RECEIPT_ID" ] || {
      echo "ERROR: PM_STATE_AUTHORIZATION_RECEIPT_ID is required in github-cas mode" >&2
      exit 2
    }
    [ -n "$EXPECTED_HEAD" ] || {
      echo "ERROR: PM_STATE_EXPECTED_HEAD is required in github-cas mode" >&2
      exit 2
    }
    [ -n "$GITHUB_APP_ID" ] || {
      echo "ERROR: PM_STATE_GITHUB_APP_ID is required in github-cas mode" >&2
      exit 2
    }
    [ -n "$GITHUB_INSTALLATION_ID" ] || {
      echo "ERROR: PM_STATE_GITHUB_INSTALLATION_ID is required in github-cas mode" >&2
      exit 2
    }
    [ -n "$GITHUB_APP_ACTOR" ] || {
      echo "ERROR: PM_STATE_GITHUB_APP_ACTOR is required in github-cas mode" >&2
      exit 2
    }
    [ -n "$GITHUB_TOKEN_FILE" ] || {
      echo "ERROR: PM_STATE_GITHUB_INSTALLATION_TOKEN_FILE is required in github-cas mode" >&2
      exit 2
    }

    CAS_GATE_ARGS=()
    if [ "$SUFFIX" = "qa-passed-awaiting-ci" ]; then
      GATE_SOURCE="${PM_CI_GATE_SOURCE:-slot-ready}"
      case "$GATE_SOURCE" in
        slot-ready|slot-ready-rescue|pm-review-done|cto-wave) ;;
        *) echo "ERROR: invalid PM_CI_GATE_SOURCE=${GATE_SOURCE}" >&2; exit 1 ;;
      esac
      CAS_GATE_ARGS=(
        --ci-ready-gate "$GITHUB_CI_READY_GATE"
        --gate-source "$GATE_SOURCE"
      )
      PROMOTION_PROOF="/tmp/pm-state-promotion-proof-${PR}-qa-passed-awaiting-ci.ok"
      if [ -f "$PROMOTION_PROOF" ] \
        && grep -Fqx "PR: ${PR}" "$PROMOTION_PROOF" \
        && grep -Fqx "headRefOid: ${EXPECTED_HEAD}" "$PROMOTION_PROOF"; then
        AFFECTED_TEST_PROOF="$(sed -n 's/^affected_test_proof:[[:space:]]*//p' "$PROMOTION_PROOF" | head -1)"
        if [ -n "$AFFECTED_TEST_PROOF" ]; then
          CAS_GATE_ARGS+=(--affected-test-proof "$AFFECTED_TEST_PROOF")
        fi
      fi
    fi

    CAS_ARGS=(
      "$GITHUB_CAS_CLI"
      --repository "$REPO"
      --pr "$PR"
      --state "$SUFFIX"
      --expected-head "$EXPECTED_HEAD"
      --authorization-receipt-id "$AUTHORIZATION_RECEIPT_ID"
      --app-id "$GITHUB_APP_ID"
      --installation-id "$GITHUB_INSTALLATION_ID"
      --app-actor "$GITHUB_APP_ACTOR"
      --installation-token-file "$GITHUB_TOKEN_FILE"
      --lock-root "$GITHUB_LOCK_ROOT"
      --api-base-url "$GITHUB_API_BASE_URL"
    )
    if [ "$SUFFIX" = "qa-passed-awaiting-ci" ]; then
      CAS_ARGS+=("${CAS_GATE_ARGS[@]}")
    fi
    CAS_ARGS+=(--json)
    CAS_OUTPUT="$("${CAS_ARGS[@]}" 2>&1)"
    CAS_RC=$?
    if [ "$CAS_RC" -ne 0 ]; then
      echo "$CAS_OUTPUT" >&2
      exit "$CAS_RC"
    fi
    if ! python3 -c '
import json
import sys

pr = int(sys.argv[1])
state = sys.argv[2]
head = sys.argv[3]
try:
    result = json.load(sys.stdin)
    receipt = result["writeReceipt"]
except (KeyError, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
if (
    result.get("ok") is not True
    or receipt.get("pr") != pr
    or receipt.get("targetState") != state
    or receipt.get("targetHead") != head
    or not receipt.get("writeReceiptId")
):
    raise SystemExit(1)
' "$PR" "$SUFFIX" "$EXPECTED_HEAD" <<<"$CAS_OUTPUT"; then
      echo "ERROR: GitHub PM-state CAS CLI returned an invalid or mismatched write receipt" >&2
      exit 1
    fi
    printf '%s\n' "$CAS_OUTPUT"
    exit 0
    ;;
  *)
    echo "ERROR: unsupported PM_STATE_AUTHORITY_MODE=${PM_STATE_AUTHORITY_MODE}" >&2
    exit 2
    ;;
esac

# Prepare the durable receipt root before entering the paid-CI path. The
# receipt is the atomic exact-head single-flight CAS (O_EXCL reservation
# around gate admission -> label mutation), NOT admission ceremony; it exists
# only to refuse a second concurrent caller that observed the same pre-label
# state. A restart cannot silently re-arm an already-admitted head.
if [ "$SUFFIX" = "qa-passed-awaiting-ci" ]; then
  if ! prepare_ci_cycle_receipt_root; then
    exit 1
  fi
fi

# 1. Read current pm-state:* labels (may be 0, 1, or >1 if pre-existing drift)
if ! CURRENT_JSON="$(current_state_labels)"; then
  echo "ERROR: cannot read current PM state labels for PR #${PR} through REST" >&2
  exit 1
fi
if [ -z "$CURRENT_JSON" ]; then CURRENT_JSON='[]'; fi
CURRENT_COUNT="$(echo "$CURRENT_JSON" | jq 'length')"

cto_wave_empty_state_recovery() {
  [ "${PM_CI_GATE_SOURCE:-slot-ready}" = "cto-wave" ] || return 1
  local head claim receipt markers
  head="$(gh pr view "$PR" --repo "$REPO" --json headRefOid --jq '.headRefOid' 2>/dev/null || true)"
  [ -n "$head" ] || return 1
  claim="$(ci_cycle_reentry_claim_path "$head")"
  receipt="$(ci_cycle_receipt_path "$head")"
  [ -f "$claim" ] && [ -e "$receipt" ] || return 1
  python3 - "$claim" "$PR" "$head" <<'PY'
import json
import sys
from pathlib import Path
try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)
if (
    value.get("schema") != "pm-ci-cycle-reentry/v1"
    or value.get("pr") != int(sys.argv[2])
    or value.get("headRefOid") != sys.argv[3]
    or value.get("phase") != "pre-trigger"
    or value.get("rearmSource") != "cto-wave"
):
    raise SystemExit(1)
PY
  valid_ci_cycle_receipt "$receipt" "$head" || return 1
  [ "$(ci_cycle_receipt_source "$receipt")" = "slot-ready" ] || [ "$(ci_cycle_receipt_source "$receipt")" = "cto-wave" ] || return 1
  [ "$(ci_cycle_receipt_status "$receipt")" = "reserved" ] || [ "$(ci_cycle_receipt_status "$receipt")" = "label-accepted" ] || return 1
  markers="$(current_ci_head_markers 2>/dev/null || true)"
  printf '%s\n' "${markers:-[]}" | jq -e --arg marker "ci-head:${head}" 'index($marker) != null' >/dev/null 2>&1
}

cto_wave_validate_superseded_claim() {
  local claim="$1" live_head="$2" predecessor_head="$3" generation="$4" receipt="" markers=""
  [ -n "$predecessor_head" ] && [ -n "$generation" ] || return 1
  receipt="$(ci_cycle_receipt_path "$predecessor_head")"
  valid_ci_cycle_receipt "$receipt" "$predecessor_head" || return 1
  [ "$(ci_cycle_receipt_source "$receipt")" = "slot-ready" ] || [ "$(ci_cycle_receipt_source "$receipt")" = "cto-wave" ] || return 1
  [ "$(ci_cycle_receipt_status "$receipt")" = "reserved" ] || [ "$(ci_cycle_receipt_status "$receipt")" = "label-accepted" ] || return 1
  markers="$(current_ci_head_markers 2>/dev/null || true)"
  printf '%s\n' "$markers" | jq -e --arg marker "ci-head:${predecessor_head}" 'index($marker) != null' >/dev/null 2>&1 || return 1
  python3 - "$claim" "$PR" "$predecessor_head" "$generation" "$live_head" <<'PY' 2>/dev/null
import fcntl
import json
import os
import sys
from pathlib import Path
path = Path(sys.argv[1])
lock_fd = os.open(str(path) + ".recovery.lock", os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(lock_fd, fcntl.LOCK_EX)
try:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        value.get("schema") != "pm-ci-cycle-reentry/v1"
        or value.get("pr") != int(sys.argv[2])
        or value.get("headRefOid") != sys.argv[3]
        or value.get("claimGeneration") != sys.argv[4]
        or value.get("rearmSource") != "cto-wave"
        or value.get("phase") != "superseded"
        or value.get("supersededToHead") != sys.argv[5]
    ):
        raise SystemExit(1)
finally:
    os.close(lock_fd)
PY
}

cto_wave_superseded_empty_state_recovery() {
  [ "${PM_CI_GATE_SOURCE:-slot-ready}" = "cto-wave" ] || return 1
  local head claim target claim_head phase receipt markers generation rearm_source
  head="$(gh pr view "$PR" --repo "$REPO" --json headRefOid --jq '.headRefOid' 2>/dev/null || true)"
  [ -n "$head" ] || return 1
  for claim in "$CI_CYCLE_RECEIPT_ROOT"/pr-"$PR"-*.reentry; do
    [ -f "$claim" ] || continue
    read -r phase claim_head generation target rearm_source < <(python3 - "$claim" <<'PY' 2>/dev/null || true
import json
import sys
from pathlib import Path
try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(value.get("phase") or "", value.get("headRefOid") or "", value.get("claimGeneration") or "", value.get("supersededToHead") or "", value.get("rearmSource") or "")
except Exception:
    pass
PY
 )
    if [ "$phase" = "superseded" ] && [ "$target" = "$head" ]; then
      cto_wave_validate_superseded_claim "$claim" "$head" "$claim_head" "$generation" && return 0
      continue
    fi
    [ "$phase" = "pre-trigger" ] && [ -n "$claim_head" ] && [ "$claim_head" != "$head" ] || continue
    [ -n "$generation" ] || continue
    [ "$rearm_source" = "cto-wave" ] || continue
    receipt="$(ci_cycle_receipt_path "$claim_head")"
    valid_ci_cycle_receipt "$receipt" "$claim_head" || continue
    [ "$(ci_cycle_receipt_status "$receipt")" = "reserved" ] || [ "$(ci_cycle_receipt_status "$receipt")" = "label-accepted" ] || continue
    [ "$(ci_cycle_receipt_source "$receipt")" = "slot-ready" ] || [ "$(ci_cycle_receipt_source "$receipt")" = "cto-wave" ] || continue
    markers="$(current_ci_head_markers 2>/dev/null || true)"
    printf '%s\n' "$markers" | jq -e --arg marker "ci-head:${claim_head}" 'index($marker) != null' >/dev/null 2>&1 || continue
    if python3 - "$claim" "$PR" "$claim_head" "$generation" "$head" <<'PY' 2>/dev/null
import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
path = Path(sys.argv[1])
lock_fd = os.open(str(path) + ".recovery.lock", os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(lock_fd, fcntl.LOCK_EX)
try:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        value.get("schema") != "pm-ci-cycle-reentry/v1"
        or value.get("pr") != int(sys.argv[2])
        or value.get("headRefOid") != sys.argv[3]
        or value.get("claimGeneration") != sys.argv[4]
        or value.get("phase") != "pre-trigger"
    ):
        raise SystemExit(1)
    value["phase"] = "superseded"
    value["supersededToHead"] = sys.argv[5]
    value["supersededAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
finally:
    os.close(lock_fd)
PY
    then
      return 0
    fi
  done
  return 1
}

# The CTO-wave event is a re-arm, never an initial state transition. It may
# consume only the already-admitted exact state; any other state or label
# drift fails closed before readiness, receipt, or label work.
CTO_WAVE_EMPTY_RECOVERY=0
CTO_WAVE_SUPERSEDED_RECOVERY=0
if [ "${PM_CI_GATE_SOURCE:-slot-ready}" = "cto-wave" ]; then
  if [ "$SUFFIX" != "qa-passed-awaiting-ci" ]; then
    echo "ERROR: cto-wave re-arm is only valid for qa-passed-awaiting-ci" >&2
    exit 1
  fi
  if [ "$CURRENT_COUNT" = "1" ] \
    && [ "$(echo "$CURRENT_JSON" | jq -r '.[0] // ""')" = "$NEW_LABEL" ]; then
    :
  elif [ "$CURRENT_COUNT" = "0" ] && cto_wave_empty_state_recovery; then
    CTO_WAVE_EMPTY_RECOVERY=1
  elif [ "$CURRENT_COUNT" = "0" ] && cto_wave_superseded_empty_state_recovery; then
    CTO_WAVE_SUPERSEDED_RECOVERY=1
  else
    echo "ERROR: cto-wave re-arm requires exactly ${NEW_LABEL} as the current PM state" >&2
    exit 1
  fi
fi

# `qa-passed-awaiting-ci` is the label-gated CI/E2E trigger. A draft PR can
# carry labels but will not enter the intended ready-for-review PR flow, so
# enforce the ready-before-CI invariant at the state transition boundary.
if [ "$SUFFIX" = "qa-passed-awaiting-ci" ]; then
  IS_DRAFT="$(gh api "repos/${REPO}/pulls/${PR}" --jq '.draft' 2>/dev/null || true)"
  if [ "$IS_DRAFT" != "true" ] && [ "$IS_DRAFT" != "false" ]; then
    echo "ERROR: cannot prove draft state for PR #${PR} through REST" >&2
    exit 1
  fi
  if [ "$IS_DRAFT" = "true" ]; then
    echo "ERROR: PR #${PR} is still draft; mark it ready before applying ${NEW_LABEL}." >&2
    echo "Use: gh pr ready ${PR} --repo heydonna-app/heydonna-app" >&2
    echo "Preferred caller: request-label-gated-ci.sh --pr ${PR} --head <current-head>" >&2
    echo "${TS} PR #${PR}: BLOCKED draft-before-ci → ${NEW_LABEL} (actor: ${ACTOR})" >> "$LOG_FILE"
    exit 1
  fi
fi

# Sanctioned pm-review-done reentry declaration: cmd_pm_review_done forwards
# PM_CI_GATE_SOURCE=pm-review-done AND has already written a current-head
# phase-a PASS promotion proof. When the PR is ALREADY at
# qa-passed-awaiting-ci, the plain idempotent no-op must yield to the reentry so
# the label is removed and re-applied below: the resulting `labeled` event
# (with the exact-head ci-head marker re-emitted in the same final mutation)
# restarts the label-gated CI/E2E cycle at the same head. Every other source,
# and any missing or mismatched proof, keeps the fail-closed no-op. This is the
# combined-label re-arm admission (incident
# control-plane:reentry-dedup-caller-env-forwarding); the check mirrors the
# sanctioned-reentry guard of the ci-cycle receipt dedup below.
sanctioned_pm_review_done_reentry() {
  [ "$SUFFIX" = "qa-passed-awaiting-ci" ] || return 1
  [ "${PM_CI_GATE_SOURCE:-slot-ready}" = "pm-review-done" ] || return 1
  local reentry_head reentry_proof
  reentry_head="$(gh pr view "$PR" --repo "$REPO" --json headRefOid --jq '.headRefOid' 2>/dev/null || true)"
  [ -n "$reentry_head" ] || return 1
  reentry_proof="/tmp/pm-state-promotion-proof-${PR}-qa-passed-awaiting-ci.ok"
  [ -f "$reentry_proof" ] || return 1
  grep -Fqx "PR: ${PR}" "$reentry_proof" || return 1
  grep -Fqx "headRefOid: ${reentry_head}" "$reentry_proof" || return 1
  grep -qE '^PM_CLAUDE_REVIEW:[[:space:]]*PASS($|[[:space:]])' "$reentry_proof" || return 1
  return 0
}

# Explicit CTO-wave re-arm declaration. This is the only exception to the
# ordinary same-state no-op: it is source-qualified, treats a completed
# exact-marker-plus-accepted-receipt pair as terminal, and still runs the
# unchanged exact-head readiness gate before any label mutation.
sanctioned_cto_wave_reentry() {
  [ "$SUFFIX" = "qa-passed-awaiting-ci" ] || return 1
  [ "${PM_CI_GATE_SOURCE:-slot-ready}" = "cto-wave" ] || return 1
  local reentry_head expected_marker markers receipt receipt_status
  reentry_head="$(gh pr view "$PR" --repo "$REPO" --json headRefOid --jq '.headRefOid' 2>/dev/null || true)"
  [ -n "$reentry_head" ] || return 1
  expected_marker="ci-head:${reentry_head}"
  markers="$(current_ci_head_markers 2>/dev/null || true)"
  receipt="$(ci_cycle_receipt_path "$reentry_head")"
  if printf '%s\n' "${markers:-[]}" | jq -e --arg marker "$expected_marker" 'index($marker) != null' >/dev/null 2>&1 \
    && [ -e "$receipt" ] \
    && valid_ci_cycle_receipt "$receipt" "$reentry_head"; then
    # The receipt and marker are only the prior admission record. The
    # readiness gate below remains authoritative for whether a genuine or
    # active exact-head pair exists; let both reserved and label-accepted
    # receipts reach that gate so a missing pair can be re-armed.
    return 0
  fi
  return 0
}

cto_wave_receipt_reentry() {
  local receipt="$1"
  sanctioned_cto_wave_reentry || return 1
  # A valid exact-head admission receipt may have been created by the
  # preceding slot-ready writer. For a normal re-arm, the readiness gate—not
  # receipt provenance—decides whether a genuine/active pair exists. The
  # stricter cto-wave source binding remains on empty-state and
  # final-trigger recovery paths.
  return 0
}

# 2. Preserve idempotent reads of an already-applied state. They do not emit a new
# label event and therefore cannot allocate CI. Exception: a sanctioned
# pm-review-done reentry at an already-admitted head falls through so the label
# is re-applied and the `labeled` event re-fires the exact-head CI/E2E cycle.
if [ "$CURRENT_COUNT" = "1" ]; then
  ONLY="$(echo "$CURRENT_JSON" | jq -r '.[0]')"
  if [ "$ONLY" = "$NEW_LABEL" ]; then
    if sanctioned_pm_review_done_reentry || sanctioned_cto_wave_reentry; then
      echo "${TS} PR #${PR}: sanctioned ${PM_CI_GATE_SOURCE:-slot-ready} reentry at already-admitted ${NEW_LABEL} — re-firing labeled event for an exact-head CI/E2E cycle (actor: ${ACTOR})" >> "$LOG_FILE"
    else
      echo "${TS} PR #${PR}: already at ${NEW_LABEL} (no-op, actor: ${ACTOR})" >> "$LOG_FILE"
      echo "no-op: PR #${PR} already at ${NEW_LABEL}"
      exit 0
    fi
  fi
fi

# The label itself is the CI/E2E start control point. Run the existing canonical
# readiness gate here, not only in higher-level transition helpers. The gate
# validates exact-head affected-test receipts and, when the diff requires it,
# exact-head local or remote capture proof. Missing gate/proof is fail-closed.
if [ "$SUFFIX" = "qa-passed-awaiting-ci" ]; then
  [ -r "$CI_READY_GATE" ] || {
    echo "ERROR: CI readiness gate missing: ${CI_READY_GATE}" >&2
    exit 1
  }

  # The readiness gate resolves the qa-visual proof gate by preferring a
  # mutable same-directory sibling such as
  # <runtime>/.claude/scripts/qa-visual-proof-gate.py.  That installed sibling
  # can lag the versioned repo-owned gate and shadow it, and a stale copy exits
  # at import with empty stdout -- which the readiness gate then reports as
  # "qa visual proof gate returned invalid JSON" even though the canonical gate
  # emits a valid receipt.  Bind the readiness gate's supported
  # HEYDONNA_QA_VISUAL_PROOF_GATE override to the VERSIONED repo-owned gate
  # (the same resolution the pre-merge and promotion guards already use via
  # QA_VISUAL_PROOF_GATE) so a stale installed sibling can never be parsed as
  # the visual proof source.  A missing or unreadable versioned gate fails
  # closed here, before any label mutation.
  REPO_ROOT="$(cd "${CONTROL_PLANE_DIR}/../../.." && pwd -P)"
  export HEYDONNA_QA_VISUAL_PROOF_GATE="${HEYDONNA_QA_VISUAL_PROOF_GATE:-${REPO_ROOT}/scripts/pm/qa-visual-proof-gate.py}"
  [ -r "$HEYDONNA_QA_VISUAL_PROOF_GATE" ] || {
    echo "ERROR: versioned qa-visual proof gate missing or unreadable: ${HEYDONNA_QA_VISUAL_PROOF_GATE}" >&2
    exit 1
  }

  CI_HEAD="$(gh pr view "$PR" --repo "$REPO" --json headRefOid --jq '.headRefOid' 2>/dev/null || true)"
  [ -n "$CI_HEAD" ] || {
    echo "ERROR: cannot read current head for PR #${PR} before CI admission" >&2
    exit 1
  }

  CI_CYCLE_RECEIPT="$(ci_cycle_receipt_path "$CI_HEAD")"
  REENTRY_CLAIM="$(ci_cycle_reentry_claim_path "$CI_HEAD")"
  SANCTIONED_REENTRY=0
  if sanctioned_cto_wave_reentry; then
    SANCTIONED_REENTRY=1
  fi
  # Atomic exact-head single-flight CAS: a valid reservation for this exact
  # head means another caller already passed the readiness gate and is
  # mutating labels (or a previous admission completed). The sanctioned
  # pm-review-done re-entry (post-capture regression at the SAME head with a
  # current-head PASS promotion proof) falls through to the gate, which
  # re-validates and re-reserves idempotently. Every other same-head caller is
  # refused BEFORE any label mutation, so two concurrent label triggers cannot
  # both publish ci-head or fire duplicate waves.
  if [ -e "$CI_CYCLE_RECEIPT" ]; then
    if valid_ci_cycle_receipt "$CI_CYCLE_RECEIPT" "$CI_HEAD"; then
      REENTRY_PROOF="/tmp/pm-state-promotion-proof-${PR}-qa-passed-awaiting-ci.ok"
      if [ "${PM_CI_GATE_SOURCE:-slot-ready}" = "pm-review-done" ] \
        && [ -f "$REENTRY_PROOF" ] \
        && grep -Fqx "PR: ${PR}" "$REENTRY_PROOF" \
        && grep -Fqx "headRefOid: ${CI_HEAD}" "$REENTRY_PROOF" \
        && grep -qE '^PM_CLAUDE_REVIEW:[[:space:]]*PASS($|[[:space:]])' "$REENTRY_PROOF"; then
        : # sanctioned re-entry — fall through to the gate invocation below
        SANCTIONED_REENTRY=1
      elif cto_wave_receipt_reentry "$CI_CYCLE_RECEIPT"; then
        : # explicit CTO-wave re-arm — source-bound receipt and absent marker
        SANCTIONED_REENTRY=1
      else
        echo "ERROR: ci-cycle-already-admitted PR #${PR} exact head ${CI_HEAD} already admitted for an initial CI/E2E cycle; a same-head label trigger would duplicate the wave" >&2
        echo "${TS} PR #${PR}: BLOCKED ci-cycle-already-admitted head=${CI_HEAD} → ${NEW_LABEL} (actor: ${ACTOR})" >>"$LOG_FILE"
        exit 1
      fi
    else
      echo "ERROR: invalid CI cycle reservation for PR #${PR} exact head ${CI_HEAD}: ${CI_CYCLE_RECEIPT}" >&2
      echo "${TS} PR #${PR}: BLOCKED ci-cycle-reservation-invalid head=${CI_HEAD} → ${NEW_LABEL} (actor: ${ACTOR})" >>"$LOG_FILE"
      exit 1
    fi
  fi

  PROMOTION_PROOF="/tmp/pm-state-promotion-proof-${PR}-qa-passed-awaiting-ci.ok"
  GATE_SOURCE="${PM_CI_GATE_SOURCE:-slot-ready}"
  PROOF_ARGS=()
  if [ -f "$PROMOTION_PROOF" ] \
    && grep -Fqx "PR: ${PR}" "$PROMOTION_PROOF" \
    && grep -Fqx "headRefOid: ${CI_HEAD}" "$PROMOTION_PROOF"; then
    if [ "$GATE_SOURCE" != "cto-wave" ] \
      && grep -qE '^PM_CLAUDE_REVIEW:[[:space:]]*PASS($|[[:space:]])' "$PROMOTION_PROOF"; then
      GATE_SOURCE="pm-review-done"
    fi
    if [ "$GATE_SOURCE" != "cto-wave" ]; then
      AFFECTED_TEST_PROOF="$(sed -n 's/^affected_test_proof:[[:space:]]*//p' "$PROMOTION_PROOF" | head -1)"
      if [ -n "$AFFECTED_TEST_PROOF" ]; then
        PROOF_ARGS+=(--affected-test-proof "$AFFECTED_TEST_PROOF")
      fi
    fi
  fi
  case "$GATE_SOURCE" in
    slot-ready|slot-ready-rescue|pm-review-done|cto-wave) ;;
    *) echo "ERROR: invalid PM_CI_GATE_SOURCE=${GATE_SOURCE}" >&2; exit 1 ;;
  esac

  GATE_OUT="/tmp/pm-ci-ready-gate-${PR}-${CI_HEAD}.json"
  GATE_ERR="/tmp/pm-ci-ready-gate-${PR}-${CI_HEAD}.err"
  if [ "${PM_REVIEW_RESCUE_AUTHORIZED:-0}" = "1" ]; then
    PROOF_ARGS+=(--rescue-authorized)
  fi
  if ! python3 "$CI_READY_GATE" \
    --pr "$PR" \
    --repo "$REPO" \
    --expect-head "$CI_HEAD" \
    --source "$GATE_SOURCE" \
    --commit-reentry \
    ${PROOF_ARGS[@]+"${PROOF_ARGS[@]}"} \
    --json >"$GATE_OUT" 2>"$GATE_ERR"; then
    echo "ERROR: PR #${PR} failed final CI admission gate for head ${CI_HEAD}; inspect ${GATE_OUT} and ${GATE_ERR}" >&2
    echo "${TS} PR #${PR}: BLOCKED ci-admission-gate → ${NEW_LABEL} head=${CI_HEAD} (actor: ${ACTOR})" >> "$LOG_FILE"
    exit 1
  fi

  LATEST_HEAD="$(gh pr view "$PR" --repo "$REPO" --json headRefOid --jq '.headRefOid' 2>/dev/null || true)"
  if [ "$LATEST_HEAD" != "$CI_HEAD" ]; then
    echo "ERROR: PR #${PR} head moved during CI admission: expected=${CI_HEAD} live=${LATEST_HEAD:-unknown}" >&2
    exit 1
  fi

  # Claim this exact head durably before the first label mutation. GitHub may
  # accept a DELETE/POST even if this process exits before observing the
  # response; keeping the reservation in every ambiguous failure case prevents
  # a later caller from starting a second paid cycle for the same head.
  if ! reserve_ci_cycle_receipt "$CI_HEAD"; then
    echo "ERROR: cannot reserve initial CI/E2E cycle for PR #${PR} exact head ${CI_HEAD}: ${CI_CYCLE_RECEIPT}" >&2
    echo "${TS} PR #${PR}: BLOCKED ci-cycle-reservation-failed head=${CI_HEAD} → ${NEW_LABEL} (actor: ${ACTOR})" >>"$LOG_FILE"
    exit 1
  fi
  if [ "$SANCTIONED_REENTRY" = "1" ]; then
    # Serialize the re-entry itself: only one sanctioned caller may re-apply
    # the CI label for this exact head. A competing concurrent caller fails
    # the O_EXCL claim and is refused before any label mutation; a crash
    # after claiming keeps the claim durable so a restart cannot emit a
    # second re-entry wave. A stale claim with the PR still pm-review-pending
    # is a crashed pre-mutation attempt and is taken over on retry (the
    # lease bound prevents taking over an in-flight caller).
    if ! reentry_claim_acquire "$REENTRY_CLAIM" "$CI_HEAD" "$ACTOR"; then
      echo "ERROR: ci-cycle-reentry-already-claimed PR #${PR} exact head ${CI_HEAD} has an in-flight or completed re-entry; refuse duplicate label re-application" >&2
      echo "${TS} PR #${PR}: BLOCKED ci-cycle-reentry-already-claimed head=${CI_HEAD} → ${NEW_LABEL} (actor: ${ACTOR})" >>"$LOG_FILE"
      exit 1
    fi
    REENTRY_GENERATION="$(reentry_claim_generation "$REENTRY_CLAIM")"
    if [ -z "$REENTRY_GENERATION" ]; then
      echo "ERROR: CTO-wave re-entry claim has no ownership generation for PR #${PR}" >&2
      exit 1
    fi
    if ! reentry_claim_assert_generation "$REENTRY_CLAIM" "$CI_HEAD" "$REENTRY_GENERATION"; then
      echo "ERROR: CTO-wave re-entry claim ownership changed before final label mutation for PR #${PR}" >&2
      exit 1
    fi
  fi
  # Remove stale exact-head markers as part of state preparation. The only
  # paid-triggering marker write is the combined ci-head + PM-state mutation
  # below; no marker-only POST is allowed on this path.
  if ! remove_ci_head_markers; then
    echo "${TS} PR #${PR}: BLOCKED ci-head-marker-cleanup-failed head=${CI_HEAD} → ${NEW_LABEL} (actor: ${ACTOR})" >> "$LOG_FILE"
    exit 1
  fi
else
  # A marker is meaningful only while the PR is awaiting the paid exact-head
  # cycle. Canonical transitions away from that state remove it.
  remove_ci_head_markers || exit 1
fi

# 3. Remove ALL existing pm-state:* and merge-ready labels (handles drift count > 1 gracefully)
if [ "${SANCTIONED_REENTRY:-0}" = "1" ] \
  && [ "${PM_CI_GATE_SOURCE:-slot-ready}" = "cto-wave" ] \
  && ! reentry_claim_assert_generation "$REENTRY_CLAIM" "$CI_HEAD" "$REENTRY_GENERATION"; then
  echo "ERROR: CTO-wave re-entry claim ownership changed before state mutation for PR #${PR}" >&2
  exit 1
fi
OLD_LABELS_CSV=""
if [ "$CURRENT_COUNT" -gt 0 ]; then
  while IFS= read -r lbl; do
    [ -z "$lbl" ] && continue
    if [ -n "$OLD_LABELS_CSV" ]; then OLD_LABELS_CSV="${OLD_LABELS_CSV},${lbl}"; else OLD_LABELS_CSV="${lbl}"; fi
    encoded_label="$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$lbl")"
    if ! gh api --method DELETE "repos/${REPO}/issues/${PR}/labels/${encoded_label}" >/dev/null 2>&1; then
      if [ "${SANCTIONED_REENTRY:-0}" = "1" ] \
        && [ "${PM_CI_GATE_SOURCE:-slot-ready}" != "cto-wave" ]; then
        rm -f "$REENTRY_CLAIM"
      fi
      echo "ERROR: failed to remove ${lbl} from PR #${PR}" >&2
      exit 1
    fi
  done < <(echo "$CURRENT_JSON" | jq -r '.[]')
  if [ "$CURRENT_COUNT" -gt 1 ]; then
    echo "WARN: PR #${PR} had ${CURRENT_COUNT} pm-state:* labels (drift) — cleared all" >&2
  fi
fi

# 4. Add the new effective state label. For the paid CI/E2E state, apply the
# exact-head marker in this sole combined mutation: GitHub's triggering
# `labeled` event must contain both labels, without a preceding marker POST.
if [ "$SUFFIX" = "qa-passed-awaiting-ci" ]; then
  if [ "${SANCTIONED_REENTRY:-0}" = "1" ] \
    && [ "${PM_CI_GATE_SOURCE:-slot-ready}" = "cto-wave" ]; then
    if ! reentry_claim_mark_final_trigger_attempt "$REENTRY_CLAIM"; then
      echo "ERROR: cannot fence the CTO-wave final trigger attempt for PR #${PR}" >&2
      exit 1
    fi
  fi
  if [ "${SANCTIONED_REENTRY:-0}" = "1" ] \
    && [ "${PM_CI_GATE_SOURCE:-slot-ready}" = "pm-review-done" ]; then
    if ! reentry_claim_mark_effect_attempt "$REENTRY_CLAIM"; then
      echo "ERROR: cannot durably fence the pm-review-done final effect attempt for PR #${PR}" >&2
      exit 1
    fi
  fi
  # State preparation may remove the old PM-state labels before the final
  # combined label write. Revalidate the live head only after that preparation
  # (and immediately before the sole paid-triggering mutation) so a moving PR
  # cannot emit a ci-head event for a stale head. A pre-effect drift is
  # definite and therefore does not enter the ambiguous-post fence.
  FINAL_HEAD="$(gh pr view "$PR" --repo "$REPO" --json headRefOid --jq '.headRefOid' 2>/dev/null || true)"
  if [ "$FINAL_HEAD" != "$CI_HEAD" ]; then
    if [ "${SANCTIONED_REENTRY:-0}" = "1" ]; then
      if [ "${PM_CI_GATE_SOURCE:-slot-ready}" = "cto-wave" ] \
        && [ "${CTO_WAVE_EMPTY_RECOVERY:-0}" = "1" ]; then
        reentry_claim_mark_superseded "$REENTRY_CLAIM" "$FINAL_HEAD" || true
      else
        rm -f "$REENTRY_CLAIM"
      fi
    fi
    echo "ERROR: PR #${PR} head moved after state preparation before final CI label POST: expected=${CI_HEAD} live=${FINAL_HEAD:-unknown}" >&2
    echo "${TS} PR #${PR}: BLOCKED ci-final-head-drift reserved=${CI_HEAD} live=${FINAL_HEAD:-unknown} → ${NEW_LABEL} (actor: ${ACTOR})" >> "$LOG_FILE"
    exit 1
  fi
  if ! publish_ci_head_marker "$CI_HEAD" "$NEW_LABEL"; then
    # The underlying gh helper does not return an authoritative pre-effect
    # refusal distinction. Treat every nonzero/timeout/transport result as
    # ambiguous and retain the durable claim indefinitely; a later caller may
    # not reacquire it from transiently stale marker/state reads.
    if [ "${SANCTIONED_REENTRY:-0}" = "1" ] \
      && [ "${PM_CI_GATE_SOURCE:-slot-ready}" = "pm-review-done" ]; then
      reentry_claim_mark_ambiguous "$REENTRY_CLAIM" || true
    fi
    echo "${TS} PR #${PR}: ADD-FAILED → ${NEW_LABEL} (actor: ${ACTOR})" >> "$LOG_FILE"
    exit 1
  fi
  if [ "${SANCTIONED_REENTRY:-0}" = "1" ] \
    && [ "${PM_CI_GATE_SOURCE:-slot-ready}" = "cto-wave" ]; then
    FINAL_HEAD="$(gh pr view "$PR" --repo "$REPO" --json headRefOid --jq '.headRefOid' 2>/dev/null || true)"
    if [ "$FINAL_HEAD" != "$CI_HEAD" ]; then
      if [ "${CTO_WAVE_EMPTY_RECOVERY:-0}" = "1" ]; then
        reentry_claim_mark_superseded "$REENTRY_CLAIM" "$FINAL_HEAD" || true
      fi
      echo "ERROR: PR #${PR} head moved after final CI label POST: expected=${CI_HEAD} live=${FINAL_HEAD:-unknown}" >&2
      echo "${TS} PR #${PR}: BLOCKED ci-final-head-drift reserved=${CI_HEAD} live=${FINAL_HEAD:-unknown} → ${NEW_LABEL} (actor: ${ACTOR})" >> "$LOG_FILE"
      exit 1
    fi
  fi
elif ! printf '{"labels":[%s]}\n' "$(printf '%s' "$NEW_LABEL" | jq -R .)" | gh api --method POST "repos/${REPO}/issues/${PR}/labels" --input - >/dev/null 2>&1; then
  [ "${SANCTIONED_REENTRY:-0}" = "1" ] && rm -f "$REENTRY_CLAIM"
  echo "ERROR: failed to add ${NEW_LABEL} to PR #${PR}" >&2
  echo "${TS} PR #${PR}: ADD-FAILED → ${NEW_LABEL} (actor: ${ACTOR})" >> "$LOG_FILE"
  exit 1
fi

if [ "$SUFFIX" = "qa-passed-awaiting-ci" ]; then
  # Mark the pre-existing exact-head reservation accepted after GitHub
  # confirms the label POST. A crash before this update leaves a valid
  # `reserved` receipt, which is deliberately enough to block another paid
  # same-head admission.
  if ! mark_ci_cycle_label_accepted "$CI_HEAD"; then
    echo "ERROR: CI/E2E label was applied but the exact-head reservation could not be marked accepted: ${CI_CYCLE_RECEIPT}" >&2
    echo "${TS} PR #${PR}: RECEIPT-UPDATE-FAILED head=${CI_HEAD} → ${NEW_LABEL} (actor: ${ACTOR})" >>"$LOG_FILE"
    exit 1
  fi
fi

# 5. Verify post-state count is exactly 1 effective state.
if ! POST_STATE_JSON="$(current_state_labels)"; then
  echo "ERROR: cannot verify PM state labels for PR #${PR} through REST" >&2
  exit 1
fi
POST_COUNT="$(echo "${POST_STATE_JSON:-[]}" | jq 'length')"
POST_ONLY="$(echo "${POST_STATE_JSON:-[]}" | jq -r '.[0] // ""')"
if [ "$POST_COUNT" != "1" ] || [ "$POST_ONLY" != "$NEW_LABEL" ]; then
  echo "ERROR: post-state effective PM state is ${POST_STATE_JSON:-[]} (expected only ${NEW_LABEL}) on PR #${PR}" >&2
  echo "${TS} PR #${PR}: VERIFY-FAILED count=${POST_COUNT} got=${POST_STATE_JSON:-[]} → ${NEW_LABEL} (actor: ${ACTOR})" >> "$LOG_FILE"
  exit 1
fi

if [ "${SANCTIONED_REENTRY:-0}" = "1" ] \
  && [ "${PM_CI_GATE_SOURCE:-slot-ready}" = "cto-wave" ]; then
  # The state and exact-head receipt are now fully verified. Retain a durable
  # completed claim for the short run-visibility lease so an immediate retry
  # cannot emit a second paid event before the first pair is observable.
  if ! reentry_claim_mark_completed "$REENTRY_CLAIM"; then
    echo "ERROR: cannot persist the completed CTO-wave re-entry claim for PR #${PR}" >&2
    exit 1
  fi
fi

# 6. Log the transition
OLD_DISPLAY="${OLD_LABELS_CSV:-<none>}"
echo "${TS} PR #${PR}: ${OLD_DISPLAY} → ${NEW_LABEL} (actor: ${ACTOR})" >> "$LOG_FILE"

echo "ok: PR #${PR} ${OLD_DISPLAY} → ${NEW_LABEL}"
exit 0
