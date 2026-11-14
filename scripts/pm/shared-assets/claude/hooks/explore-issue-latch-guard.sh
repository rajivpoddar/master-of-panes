#!/usr/bin/env bash
# Shared guard for pre-issue-create-audit.sh.
#
# Keep genuine customer-origin signals fail-closed while excluding explicit
# negations such as "not a customer report". A legacy false-positive latch may
# be invalidated only when its original body is still byte-identical, it has no
# Slack origin, and the repaired classifier finds no positive customer signal.
#
# Durable obligation gate (replaces the session latch): denied customer-origin
# creates upsert ONE pm-ops obligation keyed by the Slack source
# (kind=explore_issue_required, severity=high, horizon=hourly, owner=pm,
# dedupe_group=explore-issue:<CHANNEL>:<THREAD_TS>) with evidence binding
# channel/thread_ts/permalink/title+body path/original+current body SHA/reason/
# required_at. The per-source state machine (OPEN -> IN_PROGRESS -> READY_TO_FILE
# -> RESOLVED_FILED, plus WAITING_EXTERNAL and RESOLVED_NO_ISSUE) drives a
# one-use READY_TO_FILE permit bound to channel/thread_ts/final body SHA/title
# digest; only that permit allows exactly one `gh issue create`.

explore_issue_has_customer_signal() {
  local text="$1"
  local scrubbed

  scrubbed=$(printf '%s' "$text" | perl -0pe \
    's/\b(?:not|no)\s+(?:an?\s+)?customer\s+report(?:ed)?\b//ig')

  printf '%s' "$scrubbed" | grep -qiE \
    'abilaasha|kosnicknick|customer report|reported by|\bnick\b'
}

explore_issue_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    printf '%s' "$1" | sha256sum | awk '{print $1}'
  else
    printf '%s' "$1" | shasum -a 256 | awk '{print $1}'
  fi
}

# Permit binding digest: sha256 of the EXACT bytes of the body file (or stdin when no
# argument is given). No command substitution, so trailing newlines and NUL bytes are
# part of the digest. This is the only digest a permit may be minted with.
explore_issue_file_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    if [ "$#" -gt 0 ]; then sha256sum < "$1" | awk '{print $1}'; else sha256sum | awk '{print $1}'; fi
  else
    if [ "$#" -gt 0 ]; then shasum -a 256 < "$1" | awk '{print $1}'; else shasum -a 256 | awk '{print $1}'; fi
  fi
}

explore_issue_invalidate_false_positive_latch() {
  local latch_file="$1"
  local invalidated_dir="$2"
  local reason thread_ts body_file stored_body_sha body current_body_sha
  local invalidated_file

  [ -f "$latch_file" ] || return 1

  reason=$(jq -r '.reason // ""' "$latch_file" 2>/dev/null || true)
  thread_ts=$(jq -r '.thread_ts // ""' "$latch_file" 2>/dev/null || true)
  body_file=$(jq -r '.body_file // ""' "$latch_file" 2>/dev/null || true)
  stored_body_sha=$(jq -r '.body_sha256 // ""' "$latch_file" 2>/dev/null || true)

  case "$reason" in
    "customer-origin: customer keyword in body (no Slack thread)"|\
    "customer-origin no-codex: customer keyword in body (no Slack thread)") ;;
    *) return 1 ;;
  esac

  [ -z "$thread_ts" ] || return 1
  [ -n "$body_file" ] && [ -f "$body_file" ] || return 1
  [ -n "$stored_body_sha" ] || return 1

  body=$(cat "$body_file" 2>/dev/null || true)
  current_body_sha=$(explore_issue_sha256 "$body")
  [ "$current_body_sha" = "$stored_body_sha" ] || return 1

  if explore_issue_has_customer_signal "$body"; then
    return 1
  fi

  mkdir -p "$invalidated_dir"
  invalidated_file="$invalidated_dir/$(basename "$latch_file" .json)-$(date -u +%Y%m%dT%H%M%SZ).json"
  mv "$latch_file" "$invalidated_file"
  printf '[EXPLORE_ISSUE_FALSE_POSITIVE_INVALIDATED] latch=%s audit=%s\n' \
    "$latch_file" "$invalidated_file" >&2
  return 0
}

# ---------------------------------------------------------------------------
# Durable obligation gate (source-keyed, replaces the session latch)
#
# The durable obligation is written through the PM-owned pm-ops CLI
# (obligation-upsert with arbitrary kind; dedupe is atomic on
# (kind, target_type, target_id) with status='open'). The per-source state
# machine and the one-use READY_TO_FILE permit are plain JSON artifacts under
# $EXPLORE_ISSUE_STATE_DIR / $EXPLORE_ISSUE_PERMIT_DIR. All paths and the CLI
# are env-overridable so the focused tests can run against temp roots.
# ---------------------------------------------------------------------------
EXPLORE_ISSUE_STATE_DIR="${EXPLORE_ISSUE_STATE_DIR:-/tmp/heydonna-explore-issue-state}"
EXPLORE_ISSUE_PERMIT_DIR="${EXPLORE_ISSUE_PERMIT_DIR:-/tmp/heydonna-explore-issue-permit}"
EXPLORE_ISSUE_CONSUMED_PERMIT_DIR="${EXPLORE_ISSUE_CONSUMED_PERMIT_DIR:-/tmp/heydonna-explore-issue-permit-consumed}"
EXPLORE_ISSUE_RECONCILED_PERMIT_DIR="${EXPLORE_ISSUE_RECONCILED_PERMIT_DIR:-/tmp/heydonna-explore-issue-permit-reconciled}"
EXPLORE_ISSUE_VERDICT_DIR="${EXPLORE_ISSUE_VERDICT_DIR:-/tmp/heydonna-explore-issue-verdict}"
# Durable in-flight records written by issue-create-owner.py BEFORE gh is spawned.
# An in-flight record makes the source reconciliation-only exactly like a consumed
# permit does (permit_status 3, no re-mint).
EXPLORE_ISSUE_INFLIGHT_DIR="${EXPLORE_ISSUE_INFLIGHT_DIR:-/tmp/heydonna-explore-issue-inflight}"
EXPLORE_ISSUE_PM_OPS_CLI="${EXPLORE_ISSUE_PM_OPS_CLI:-/Users/rajiv/.claude/scripts/pm-ops.py}"
export EXPLORE_ISSUE_CONSUMED_PERMIT_DIR
export EXPLORE_ISSUE_RECONCILED_PERMIT_DIR
export EXPLORE_ISSUE_VERDICT_DIR
export EXPLORE_ISSUE_INFLIGHT_DIR
EXPLORE_ISSUE_KIND="explore_issue_required"
EXPLORE_ISSUE_TARGET_TYPE="slack_thread"

# One invocation handles one source. Export this for a reply intake; all state,
# permit and obligation helpers share the same selector. Optional helper arguments
# override it only for the duration of that call.
explore_issue_source_key() {
  local channel="$1" root="$2" reply="${3:-${EXPLORE_ISSUE_SOURCE_MESSAGE_TS:-}}"
  local root_file root_state original_message
  [[ "$channel" =~ ^[CD][A-Z0-9]{10}$ ]] || return 2
  [[ "$root" =~ ^[0-9]{10}\.[0-9]{6}$ ]] || return 2
  if [ -n "$reply" ]; then
    [[ "$reply" =~ ^[0-9]{10}\.[0-9]{6}$ ]] || return 2
    [[ "$reply" == "$root" || "$reply" > "$root" ]] || return 2
  fi
  if [ -n "${EXPLORE_ISSUE_SELECTED_KEY:-}" ]; then
    case "$EXPLORE_ISSUE_SELECTED_KEY" in
      "${channel}__${root//./_}") ;;
      "${channel}__${root//./_}__${reply//./_}") [ -n "$reply" ] || return 2 ;;
      *) return 2 ;;
    esac
    printf '%s' "$EXPLORE_ISSUE_SELECTED_KEY"
    return 0
  fi
  # A previously selected reply remains that reply even if root state changes.
  if [ -n "$reply" ] && [ "$reply" != "$root" ] &&
     [ -f "$EXPLORE_ISSUE_STATE_DIR/${channel}__${root//./_}__${reply//./_}.json" ]; then
    printf '%s__%s__%s' "$channel" "${root//./_}" "${reply//./_}"
    return 0
  fi
  root_file="$EXPLORE_ISSUE_STATE_DIR/${channel}__${root//./_}.json"
  if [ -n "$reply" ] && [ "$reply" != "$root" ] && [ -f "$root_file" ]; then
    root_state="$(jq -er '.state' "$root_file")" || return 2
    original_message="$(jq -er '.source_message_ts // .thread_ts' "$root_file")" || return 2
    case "$root_state" in
      RESOLVED_FILED|RESOLVED_NO_ISSUE)
        if [ "$original_message" != "$reply" ]; then
          printf '%s__%s__%s' "$channel" "${root//./_}" "${reply//./_}"
          return 0
        fi ;;
    esac
  fi
  printf '%s__%s' "$channel" "${root//./_}"
}

explore_issue_state_file() {
  local EXPLORE_ISSUE_SELECTED_KEY="${EXPLORE_ISSUE_SELECTED_KEY:-}"
  EXPLORE_ISSUE_SELECTED_KEY="$(explore_issue_source_key "$1" "$2" "${3:-}")" || return $?
  local key
  key="$EXPLORE_ISSUE_SELECTED_KEY"
  printf '%s/%s.json' "$EXPLORE_ISSUE_STATE_DIR" "$key"
}

explore_issue_permit_file() {
  local EXPLORE_ISSUE_SELECTED_KEY="${EXPLORE_ISSUE_SELECTED_KEY:-}"
  EXPLORE_ISSUE_SELECTED_KEY="$(explore_issue_source_key "$1" "$2" "${3:-}")" || return $?
  local key
  key="$EXPLORE_ISSUE_SELECTED_KEY"
  printf '%s/%s.permit.json' "$EXPLORE_ISSUE_PERMIT_DIR" "$key"
}

explore_issue_target_id() {
  local EXPLORE_ISSUE_SELECTED_KEY="${EXPLORE_ISSUE_SELECTED_KEY:-}"
  EXPLORE_ISSUE_SELECTED_KEY="$(explore_issue_source_key "$1" "$2" "${3:-}")" || return $?
  local key root_key
  key="$EXPLORE_ISSUE_SELECTED_KEY"
  root_key="${1}__${2//./_}"
  printf '%s:%s' "$1" "$2"
  if [ "$key" != "$root_key" ]; then
    printf ':%s' "${3:-${EXPLORE_ISSUE_SOURCE_MESSAGE_TS:-}}"
  fi
}

explore_issue_permalink() {
  local message="${3:-${EXPLORE_ISSUE_SOURCE_MESSAGE_TS:-$2}}"
  printf 'https://scribiers.slack.com/archives/%s/p%s?thread_ts=%s&cid=%s' "$1" "${message//./}" "$2" "$1"
}

explore_issue_upsert_obligation() {
  local EXPLORE_ISSUE_SELECTED_KEY="${EXPLORE_ISSUE_SELECTED_KEY:-}"
  EXPLORE_ISSUE_SELECTED_KEY="$(explore_issue_source_key "$1" "$2" "${EXPLORE_ISSUE_SOURCE_MESSAGE_TS:-}")" || return $?
  # channel thread_ts permalink body_file title reason required_at orig_sha cur_sha
  local channel="$1" thread_ts="$2" permalink="$3" body_file="$4" title="$5"
  local reason="$6" required_at="$7" original_sha="$8" current_sha="$9"
  local target_id dedupe_group evidence
  target_id="$(explore_issue_target_id "$channel" "$thread_ts")" || return $?
  dedupe_group="explore-issue:${target_id}"
  evidence="$(jq -nc \
    --arg channel "$channel" \
    --arg thread_ts "$thread_ts" \
    --arg source_message_ts "${EXPLORE_ISSUE_SOURCE_MESSAGE_TS:-$thread_ts}" \
    --arg source_permalink "$permalink" \
    --arg title "$title" \
    --arg body_file "$body_file" \
    --arg reason "$reason" \
    --arg required_at "$required_at" \
    --arg original_body_sha256 "$original_sha" \
    --arg current_body_sha256 "$current_sha" \
    '{channel: $channel, thread_ts: $thread_ts, source_message_ts: $source_message_ts, source_permalink: $source_permalink, title: $title, body_file: $body_file, reason: $reason, required_at: $required_at, original_body_sha256: $original_body_sha256, current_body_sha256: $current_body_sha256}')"
  "$EXPLORE_ISSUE_PM_OPS_CLI" obligation-upsert \
    --kind "$EXPLORE_ISSUE_KIND" \
    --severity high \
    --horizon hourly \
    --target-type "$EXPLORE_ISSUE_TARGET_TYPE" \
    --target-id "$target_id" \
    --owner pm \
    --dedupe-group "$dedupe_group" \
    --title "$title" \
    --action "run Skill(explore-issue) for ${channel}:${thread_ts}" \
    --evidence-json "$evidence" \
    --print-id
}

explore_issue_has_open_obligation() {
  local EXPLORE_ISSUE_SELECTED_KEY="${EXPLORE_ISSUE_SELECTED_KEY:-}"
  EXPLORE_ISSUE_SELECTED_KEY="$(explore_issue_source_key "$1" "$2" "${EXPLORE_ISSUE_SOURCE_MESSAGE_TS:-}")" || return $?
  # channel thread_ts -> 0 if an open explore_issue_required obligation exists
  local channel="$1" thread_ts="$2" target_id json
  target_id="$(explore_issue_target_id "$channel" "$thread_ts")" || return $?
  json="$("$EXPLORE_ISSUE_PM_OPS_CLI" snapshot --surface hourly --format json --read-only 2>/dev/null || true)"
  printf '%s' "$json" | jq -e --arg kind "$EXPLORE_ISSUE_KIND" \
    --arg tt "$EXPLORE_ISSUE_TARGET_TYPE" --arg tid "$target_id" '
      [.obligations[]? | select(.kind == $kind and .target_type == $tt and .target_id == $tid and .status == "open")] | length > 0
    ' >/dev/null 2>&1
}

explore_issue_atomic_write_json() {
  # Serialize writers, then check terminality inside the lock. A denied retry
  # must never reopen or replace a terminal root (or terminal reply) record.
  python3 -c '
import fcntl, glob, json, os, sys, tempfile
path, payload = sys.argv[1:]
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path + ".lock", "a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    if path.endswith(".permit.json"):
        key = os.path.basename(path)[:-len(".permit.json")]
        consumed = os.environ.get("EXPLORE_ISSUE_CONSUMED_PERMIT_DIR", "/tmp/heydonna-explore-issue-permit-consumed")
        if glob.glob(os.path.join(consumed, key + ".permit.consumed.*.json")):
            sys.exit(1)  # Consumed/UNKNOWN creates are reconciliation-only.
        inflight = os.environ.get("EXPLORE_ISSUE_INFLIGHT_DIR", "/tmp/heydonna-explore-issue-inflight")
        if os.path.exists(os.path.join(inflight, key + ".inflight.json")):
            sys.exit(1)  # An in-flight create (gh may have started) is reconciliation-only too.
    if os.path.exists(path):
        with open(path) as f:
            previous = json.load(f)
        if previous.get("state") in ("RESOLVED_FILED", "RESOLVED_NO_ISSUE"):
            sys.exit(1)
    json.loads(payload)
    fd, temporary = tempfile.mkstemp(prefix=os.path.basename(path) + ".tmp.", dir=os.path.dirname(path))
    with os.fdopen(fd, "w") as f:
        f.write(payload + "\n")
    os.replace(temporary, path)
' "$1" "$2"
}

explore_issue_write_state() {
  local EXPLORE_ISSUE_SELECTED_KEY="${EXPLORE_ISSUE_SELECTED_KEY:-}"
  EXPLORE_ISSUE_SELECTED_KEY="$(explore_issue_source_key "$1" "$2" "${EXPLORE_ISSUE_SOURCE_MESSAGE_TS:-}")" || return $?
  # channel thread_ts state body_file title reason required_at orig_sha cur_sha obligation_id
  local channel="$1" thread_ts="$2" state="$3" body_file="$4" title="$5"
  local reason="$6" required_at="$7" original_sha="$8" current_sha="$9" obligation_id="${10:-}"
  local file permalink now
  file="$(explore_issue_state_file "$channel" "$thread_ts")" || return $?
  permalink="$(explore_issue_permalink "$channel" "$thread_ts")"
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  explore_issue_atomic_write_json "$file" "$(jq -nc \
    --arg state "$state" \
    --arg channel "$channel" \
    --arg thread_ts "$thread_ts" \
    --arg source_message_ts "${EXPLORE_ISSUE_SOURCE_MESSAGE_TS:-$thread_ts}" \
    --arg source_permalink "$permalink" \
    --arg body_file "$body_file" \
    --arg title "$title" \
    --arg reason "$reason" \
    --arg required_at "$required_at" \
    --arg original_body_sha256 "$original_sha" \
    --arg current_body_sha256 "$current_sha" \
    --arg obligation_id "$obligation_id" \
    --arg updated_at "$now" \
    '{state: $state, channel: $channel, thread_ts: $thread_ts, source_message_ts: $source_message_ts, source_permalink: $source_permalink, body_file: $body_file, title: $title, reason: $reason, required_at: $required_at, original_body_sha256: $original_body_sha256, current_body_sha256: $current_body_sha256, obligation_id: $obligation_id, updated_at: $updated_at}')"
}

explore_issue_read_state() {
  local EXPLORE_ISSUE_SELECTED_KEY="${EXPLORE_ISSUE_SELECTED_KEY:-}"
  EXPLORE_ISSUE_SELECTED_KEY="$(explore_issue_source_key "$1" "$2" "${EXPLORE_ISSUE_SOURCE_MESSAGE_TS:-}")" || return $?
  # channel thread_ts -> prints state JSON (or nothing if absent)
  local file
  file="$(explore_issue_state_file "$1" "$2")" || return $?
  [ -f "$file" ] || return 1
  cat "$file"
}

explore_issue_set_state() {
  local EXPLORE_ISSUE_SELECTED_KEY="${EXPLORE_ISSUE_SELECTED_KEY:-}"
  EXPLORE_ISSUE_SELECTED_KEY="$(explore_issue_source_key "$1" "$2" "${EXPLORE_ISSUE_SOURCE_MESSAGE_TS:-}")" || return $?
  # channel thread_ts state — transition the per-source state machine
  # (OPEN|IN_PROGRESS|READY_TO_FILE|WAITING_EXTERNAL|RESOLVED_FILED|
  # RESOLVED_NO_ISSUE) while preserving every other record field.
  local channel="$1" thread_ts="$2" state="$3"
  local file now tmp
  file="$(explore_issue_state_file "$channel" "$thread_ts")" || return $?
  [ -f "$file" ] || return 1
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  local updated
  updated="$(jq --arg state "$state" --arg updated_at "$now" \
    '.state = $state | .updated_at = $updated_at' "$file")" || return 1
  explore_issue_atomic_write_json "$file" "$updated"
}

explore_issue_mint_permit() {
  local EXPLORE_ISSUE_SELECTED_KEY="${EXPLORE_ISSUE_SELECTED_KEY:-}"
  EXPLORE_ISSUE_SELECTED_KEY="$(explore_issue_source_key "$1" "$2" "${EXPLORE_ISSUE_SOURCE_MESSAGE_TS:-}")" || return $?
  # channel thread_ts final_body_sha title_digest obligation_id
  local channel="$1" thread_ts="$2" body_sha="$3" title_digest="$4" obligation_id="$5"
  local file now
  file="$(explore_issue_permit_file "$channel" "$thread_ts")" || return $?
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  explore_issue_atomic_write_json "$file" "$(jq -nc \
    --arg channel "$channel" \
    --arg thread_ts "$thread_ts" \
    --arg source_message_ts "${EXPLORE_ISSUE_SOURCE_MESSAGE_TS:-$thread_ts}" \
    --arg body_sha256 "$body_sha" \
    --arg title_digest "$title_digest" \
    --arg obligation_id "$obligation_id" \
    --arg minted_at "$now" \
    '{channel: $channel, thread_ts: $thread_ts, source_message_ts: $source_message_ts, body_sha256: $body_sha256, title_digest: $title_digest, obligation_id: $obligation_id, minted_at: $minted_at}')"
}

explore_issue_permit_status() {
  local EXPLORE_ISSUE_SELECTED_KEY="${EXPLORE_ISSUE_SELECTED_KEY:-}"
  EXPLORE_ISSUE_SELECTED_KEY="$(explore_issue_source_key "$1" "$2" "${EXPLORE_ISSUE_SOURCE_MESSAGE_TS:-}")" || return $?
  # channel thread_ts body_sha title_digest -> 0 valid, 1 absent, 2 drift
  local channel="$1" thread_ts="$2" body_sha="$3" title_digest="$4"
  local file p_channel p_thread p_message p_body p_title
  file="$(explore_issue_permit_file "$channel" "$thread_ts")" || return $?
  local consumed
  for consumed in "$EXPLORE_ISSUE_CONSUMED_PERMIT_DIR/${EXPLORE_ISSUE_SELECTED_KEY}.permit.consumed."*.json; do
    [ ! -f "$consumed" ] || return 3
  done
  # A durable in-flight record means gh may have started: reconciliation-only.
  [ ! -f "$EXPLORE_ISSUE_INFLIGHT_DIR/${EXPLORE_ISSUE_SELECTED_KEY}.inflight.json" ] || return 3
  [ -f "$file" ] || return 1
  p_channel="$(jq -r '.channel // ""' "$file" 2>/dev/null || true)"
  p_thread="$(jq -r '.thread_ts // ""' "$file" 2>/dev/null || true)"
  # Legacy root permits have no message field; reply-specific filenames still
  # prevent those permits being reused after a terminal root.
  p_message="$(jq -r '.source_message_ts // ""' "$file" 2>/dev/null || true)"
  p_body="$(jq -r '.body_sha256 // ""' "$file" 2>/dev/null || true)"
  p_title="$(jq -r '.title_digest // ""' "$file" 2>/dev/null || true)"
  if [ "$p_channel" = "$channel" ] && [ "$p_thread" = "$thread_ts" ] \
     && { [ -z "$p_message" ] || [ "$p_message" = "${EXPLORE_ISSUE_SOURCE_MESSAGE_TS:-$thread_ts}" ]; } \
     && [ "$p_body" = "$body_sha" ] && [ "$p_title" = "$title_digest" ]; then
    return 0
  fi
  return 2
}

explore_issue_consume_permit() {
  local EXPLORE_ISSUE_SELECTED_KEY="${EXPLORE_ISSUE_SELECTED_KEY:-}"
  EXPLORE_ISSUE_SELECTED_KEY="$(explore_issue_source_key "$1" "$2" "${EXPLORE_ISSUE_SOURCE_MESSAGE_TS:-}")" || return $?
  # channel thread_ts body_sha title_digest -> 0 consumed (allow create), 1 absent, 2 drift
  local channel="$1" thread_ts="$2" body_sha="$3" title_digest="$4"
  local file consumed now key
  file="$(explore_issue_permit_file "$channel" "$thread_ts")" || return $?
  [ -f "$file" ] || return 1
  if ! explore_issue_permit_status "$channel" "$thread_ts" "$body_sha" "$title_digest"; then
    return 2
  fi
  mkdir -p "$EXPLORE_ISSUE_CONSUMED_PERMIT_DIR"
  key="$EXPLORE_ISSUE_SELECTED_KEY"
  now="$(date -u +%Y%m%dT%H%M%SZ)"
  consumed="$EXPLORE_ISSUE_CONSUMED_PERMIT_DIR/${key}.permit.consumed.${now}.json"
  python3 -c '
import fcntl, glob, json, os, sys
path, consumed, body, title, message = sys.argv[1:]
with open(path + ".lock", "a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    history = consumed.rsplit(".permit.consumed.", 1)[0] + ".permit.consumed.*.json"
    if glob.glob(history) or not os.path.exists(path):
        sys.exit(1)
    with open(path) as f:
        permit = json.load(f)
    if permit.get("body_sha256") != body or permit.get("title_digest") != title or permit.get("source_message_ts", message) != message:
        sys.exit(2)
    os.rename(path, consumed)  # Persist UNKNOWN before allowing the external create.
' "$file" "$consumed" "$body_sha" "$title_digest" "${EXPLORE_ISSUE_SOURCE_MESSAGE_TS:-$thread_ts}" || return $?
  return 0
}

explore_issue_restore_permit() {
  # channel thread_ts absence_proof -> 0 cleared, 1 not found or wrong state, 2 already reconciled,
  #                                    4 refused: an in-flight create exists (gh may have started),
  #                                    5 refused: the consumed record is a verified owner create
  #
  # Moves a LEGACY consumed permit (UNKNOWN create from the retired finalizer) to the reconciled
  # dir when the caller has proven that the guarded create never landed in GitHub (searched by
  # title, keywords, channel, and both source permalinks; no match found). After this returns 0,
  # the agent MUST re-prepare the body with the correct source_message_ts, compute a fresh exact
  # body digest (explore_issue_file_sha256) and title_digest, call explore_issue_mint_permit, set
  # state READY_TO_FILE, and run the create owner. This function never re-mints the permit itself.
  #
  # Hard refusals (no absence proof, however strong, overrides them):
  #   - an `<key>.inflight.json` record exists: issue-create-owner.py wrote it BEFORE gh was
  #     spawned, so gh may have started. It is released ONLY by the owner itself, and only on a
  #     definite spawn failure. Restore never touches it.
  #   - the consumed record carries consumed_by=issue-create-owner / issue_url: the owner verified
  #     that the issue exists; "absence" is a contradiction, not a recovery.
  #
  # Safety invariants:
  #   - Only succeeds when state is READY_TO_FILE (not any terminal state).
  #   - Exactly one reconcile per key: a second call returns 2.
  #   - All checks and the move run under the SAME permit lock the mint (atomic write) and the
  #     owner use: `<permit file>.lock`.
  local EXPLORE_ISSUE_SELECTED_KEY="${EXPLORE_ISSUE_SELECTED_KEY:-}"
  EXPLORE_ISSUE_SELECTED_KEY="$(explore_issue_source_key "$1" "$2" "${EXPLORE_ISSUE_SOURCE_MESSAGE_TS:-}")" || return $?
  local channel="$1" thread_ts="$2" absence_proof="${3:-}"
  local consumed_dir reconciled_dir key state_file state permit_file
  key="$EXPLORE_ISSUE_SELECTED_KEY"
  consumed_dir="${EXPLORE_ISSUE_CONSUMED_PERMIT_DIR:-/tmp/heydonna-explore-issue-permit-consumed}"
  reconciled_dir="${EXPLORE_ISSUE_RECONCILED_PERMIT_DIR:-/tmp/heydonna-explore-issue-permit-reconciled}"
  permit_file="$(explore_issue_permit_file "$channel" "$thread_ts")" || return $?

  # State must be READY_TO_FILE (not terminal, not unstarted).
  state_file="$EXPLORE_ISSUE_STATE_DIR/$key.json"
  [ -f "$state_file" ] || return 1
  state="$(jq -er .state "$state_file" 2>/dev/null)" || return 1
  [ "$state" = "READY_TO_FILE" ] || return 1

  local inflight_file="${EXPLORE_ISSUE_INFLIGHT_DIR:-/tmp/heydonna-explore-issue-inflight}/${key}.inflight.json"
  local now
  now="$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$reconciled_dir"
  python3 -c '
import fcntl, glob, json, os, sys
key, permit_file, consumed_dir, inflight_file, reconciled_dir, now, absence_proof = sys.argv[1:]
os.makedirs(os.path.dirname(permit_file), exist_ok=True)
with open(permit_file + ".lock", "a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    if os.path.exists(inflight_file):
        sys.exit(4)  # gh may have started: never released by restore.
    found = ""
    for candidate in sorted(glob.glob(os.path.join(consumed_dir, key + ".permit.consumed.*.json"))):
        found = candidate
        break
    if not found:
        sys.exit(1)
    if glob.glob(os.path.join(reconciled_dir, key + ".permit.reconciled.*.json")):
        sys.exit(2)  # Already reconciled once; preserve UNKNOWN safety.
    with open(found) as f:
        permit = json.load(f)
    if permit.get("consumed_by") == "issue-create-owner" or permit.get("issue_url"):
        sys.exit(5)  # A verified create exists; absence cannot be claimed.
    permit["reconciled_at"] = now
    permit["absence_proof"] = absence_proof
    reconciled = os.path.join(reconciled_dir, key + ".permit.reconciled." + now + ".json")
    with open(reconciled + ".tmp", "w") as f:
        json.dump(permit, f)
    os.replace(reconciled + ".tmp", reconciled)
    os.remove(found)
' "$key" "$permit_file" "$consumed_dir" "$inflight_file" "$reconciled_dir" "$now" "$absence_proof" || return $?
}

explore_issue_verdict_path() {
  # channel thread_ts [source_msg_ts] -> prints path for the per-source verdict file.
  # The verdict file is written by the explore-issue agent after CONFIRMED/INVESTIGATION
  # review. The pre-issue-create gate reads this file — not the global
  # /tmp/codex-explore-output.txt — to avoid cross-source contamination when two
  # sessions run concurrently for different sources.
  local EXPLORE_ISSUE_SELECTED_KEY="${EXPLORE_ISSUE_SELECTED_KEY:-}"
  EXPLORE_ISSUE_SELECTED_KEY="$(explore_issue_source_key "$1" "$2" "${3:-${EXPLORE_ISSUE_SOURCE_MESSAGE_TS:-}}")" || return $?
  local key
  key="$EXPLORE_ISSUE_SELECTED_KEY"
  printf '%s/%s.verdict.txt' "${EXPLORE_ISSUE_VERDICT_DIR:-/tmp/heydonna-explore-issue-verdict}" "$key"
}

# ---------------------------------------------------------------------------
# Single-chain create protocol (issue-create-owner.py)
#
# There is NO pending file and NO separate finalizer any more. The old deferred
# protocol (pre-audit wrote `<key>.permit.pending.json`, a parallel finalizer
# hook consumed the permit before gh) is gone: Claude Code runs matching hooks in
# parallel, so "last hook" was never guaranteed, every hook re-read the body at its
# own moment, and the permit was consumed before gh had run.
#
# One owner process (issue-create-owner.py create ...) now does, in order:
#   capture the exact bytes once -> pre-issue-create-audit.sh stage `audit` and the
#   ledger/tracker hook on those bytes (neither writes a permit record) -> per-source
#   flock bound to source/invocation/digest -> durable `<key>.inflight.json` BEFORE
#   gh -> gh fed the identical bytes on stdin -> permit consumed ONLY on a verified
#   issue URL (consumed record carries the URL).
#
# States, per source key:
#   permit file only          READY_TO_FILE, create allowed through the owner
#   inflight file             gh may have started: reconciliation-only (status 3)
#   consumed file             verified create (or legacy consume): reconciliation-only
#   The in-flight record is released ONLY by the owner itself, and only when gh
#   definitely never started (spawn failure). explore_issue_restore_permit refuses an
#   in-flight record outright, whatever absence is claimed.
#
# One lock: `<permit file>.lock`. Mint (explore_issue_atomic_write_json), the legacy
# consume, restore and the owner's check + binding + in-flight write + consume all take
# that same lock; none of them nests it or holds it across gh.
# ---------------------------------------------------------------------------

explore_issue_resolve_obligation() {
  # channel thread_ts external_state reason
  local channel="$1" thread_ts="$2" external_state="$3" reason="$4"
  local EXPLORE_ISSUE_SOURCE_MESSAGE_TS="${5:-${EXPLORE_ISSUE_SOURCE_MESSAGE_TS:-}}"
  local EXPLORE_ISSUE_SELECTED_KEY="${EXPLORE_ISSUE_SELECTED_KEY:-}"
  EXPLORE_ISSUE_SELECTED_KEY="$(explore_issue_source_key "$1" "$2" "${EXPLORE_ISSUE_SOURCE_MESSAGE_TS:-}")" || return $?
  local target_id
  target_id="$(explore_issue_target_id "$channel" "$thread_ts")" || return $?
  "$EXPLORE_ISSUE_PM_OPS_CLI" obligation-resolve \
    --kind "$EXPLORE_ISSUE_KIND" \
    --target-type "$EXPLORE_ISSUE_TARGET_TYPE" \
    --target-id "$target_id" \
    --external-state "$external_state" \
    --reason "$reason"
}

explore_issue_resolve_filed() {
  # channel thread_ts issue_number issue_url title body_sha
  local channel="$1" thread_ts="$2" issue_number="$3" issue_url="$4" title="$5" body_sha="$6"
  local EXPLORE_ISSUE_SOURCE_MESSAGE_TS="${7:-${EXPLORE_ISSUE_SOURCE_MESSAGE_TS:-}}"
  local EXPLORE_ISSUE_SELECTED_KEY="${EXPLORE_ISSUE_SELECTED_KEY:-}"
  EXPLORE_ISSUE_SELECTED_KEY="$(explore_issue_source_key "$1" "$2" "${EXPLORE_ISSUE_SOURCE_MESSAGE_TS:-}")" || return $?
  local file state
  file="$(explore_issue_state_file "$channel" "$thread_ts")" || return $?
  [ -f "$file" ] || return 1
  state="$(jq -er .state "$file")" || return 1
  case "$state" in RESOLVED_FILED|RESOLVED_NO_ISSUE) return 0 ;; esac
  explore_issue_resolve_obligation "$channel" "$thread_ts" RESOLVED_FILED \
    "filed issue #${issue_number} ${issue_url}" || true
  file="$(explore_issue_state_file "$channel" "$thread_ts")" || return $?
  if [ -f "$file" ]; then
    state="$(jq -r '.state // "OPEN"' "$file" 2>/dev/null || true)"
    if [ "$state" != "RESOLVED_FILED" ] && [ "$state" != "RESOLVED_NO_ISSUE" ]; then
      explore_issue_write_state "$channel" "$thread_ts" RESOLVED_FILED \
        "$(jq -r '.body_file // ""' "$file" 2>/dev/null || true)" \
        "$title" "$(jq -r '.reason // ""' "$file" 2>/dev/null || true)" \
        "$(jq -r '.required_at // ""' "$file" 2>/dev/null || true)" \
        "$(jq -r '.original_body_sha256 // ""' "$file" 2>/dev/null || true)" \
        "$body_sha" "$(jq -r '.obligation_id // ""' "$file" 2>/dev/null || true)"
    fi
  fi
}

explore_issue_resolve_no_issue() {
  # channel thread_ts resolution_reason title body_sha
  local channel="$1" thread_ts="$2" resolution="$3" title="$4" body_sha="$5"
  local EXPLORE_ISSUE_SOURCE_MESSAGE_TS="${6:-${EXPLORE_ISSUE_SOURCE_MESSAGE_TS:-}}"
  local EXPLORE_ISSUE_SELECTED_KEY="${EXPLORE_ISSUE_SELECTED_KEY:-}"
  EXPLORE_ISSUE_SELECTED_KEY="$(explore_issue_source_key "$1" "$2" "${EXPLORE_ISSUE_SOURCE_MESSAGE_TS:-}")" || return $?
  local file state
  file="$(explore_issue_state_file "$channel" "$thread_ts")" || return $?
  [ -f "$file" ] || return 1
  state="$(jq -er .state "$file")" || return 1
  case "$state" in RESOLVED_FILED|RESOLVED_NO_ISSUE) return 0 ;; esac
  explore_issue_resolve_obligation "$channel" "$thread_ts" RESOLVED_NO_ISSUE \
    "$resolution" || true
  file="$(explore_issue_state_file "$channel" "$thread_ts")" || return $?
  if [ -f "$file" ]; then
    state="$(jq -r '.state // "OPEN"' "$file" 2>/dev/null || true)"
    if [ "$state" != "RESOLVED_FILED" ] && [ "$state" != "RESOLVED_NO_ISSUE" ]; then
      explore_issue_write_state "$channel" "$thread_ts" RESOLVED_NO_ISSUE \
        "$(jq -r '.body_file // ""' "$file" 2>/dev/null || true)" \
        "$title" "$(jq -r '.reason // ""' "$file" 2>/dev/null || true)" \
        "$(jq -r '.required_at // ""' "$file" 2>/dev/null || true)" \
        "$(jq -r '.original_body_sha256 // ""' "$file" 2>/dev/null || true)" \
        "$body_sha" "$(jq -r '.obligation_id // ""' "$file" 2>/dev/null || true)"
    fi
  fi
}
