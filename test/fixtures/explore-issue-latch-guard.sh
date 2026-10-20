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
EXPLORE_ISSUE_PM_OPS_CLI="${EXPLORE_ISSUE_PM_OPS_CLI:-/Users/rajiv/.claude/scripts/pm-ops.py}"
EXPLORE_ISSUE_KIND="explore_issue_required"
EXPLORE_ISSUE_TARGET_TYPE="slack_thread"

explore_issue_source_key() {
  # channel thread_ts -> C0AGWPQFKHA__1785932534_656769 (dot in ts -> underscore)
  printf '%s__%s' "$1" "${2//./_}"
}

explore_issue_state_file() {
  printf '%s/%s.json' "$EXPLORE_ISSUE_STATE_DIR" "$(explore_issue_source_key "$1" "$2")"
}

explore_issue_permit_file() {
  printf '%s/%s.permit.json' "$EXPLORE_ISSUE_PERMIT_DIR" "$(explore_issue_source_key "$1" "$2")"
}

explore_issue_target_id() {
  printf '%s:%s' "$1" "$2"
}

explore_issue_permalink() {
  local ts_without_dot="${2//./}"
  printf 'https://scribiers.slack.com/archives/%s/p%s?thread_ts=%s&cid=%s' "$1" "$ts_without_dot" "$2" "$1"
}

explore_issue_upsert_obligation() {
  # channel thread_ts permalink body_file title reason required_at orig_sha cur_sha
  local channel="$1" thread_ts="$2" permalink="$3" body_file="$4" title="$5"
  local reason="$6" required_at="$7" original_sha="$8" current_sha="$9"
  local target_id dedupe_group evidence
  target_id="$(explore_issue_target_id "$channel" "$thread_ts")"
  dedupe_group="explore-issue:${channel}:${thread_ts}"
  evidence="$(jq -nc \
    --arg channel "$channel" \
    --arg thread_ts "$thread_ts" \
    --arg source_permalink "$permalink" \
    --arg title "$title" \
    --arg body_file "$body_file" \
    --arg reason "$reason" \
    --arg required_at "$required_at" \
    --arg original_body_sha256 "$original_sha" \
    --arg current_body_sha256 "$current_sha" \
    '{channel: $channel, thread_ts: $thread_ts, source_permalink: $source_permalink, title: $title, body_file: $body_file, reason: $reason, required_at: $required_at, original_body_sha256: $original_body_sha256, current_body_sha256: $current_body_sha256}')"
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
  # channel thread_ts -> 0 if an open explore_issue_required obligation exists
  local channel="$1" thread_ts="$2" target_id json
  target_id="$(explore_issue_target_id "$channel" "$thread_ts")"
  json="$("$EXPLORE_ISSUE_PM_OPS_CLI" snapshot --surface hourly --format json --read-only 2>/dev/null || true)"
  printf '%s' "$json" | jq -e --arg kind "$EXPLORE_ISSUE_KIND" \
    --arg tt "$EXPLORE_ISSUE_TARGET_TYPE" --arg tid "$target_id" '
      [.obligations[]? | select(.kind == $kind and .target_type == $tt and .target_id == $tid and .status == "open")] | length > 0
    ' >/dev/null 2>&1
}

explore_issue_atomic_write_json() {
  local file="$1" json="$2" tmp
  mkdir -p "$(dirname "$file")"
  tmp="${file}.tmp.$$"
  printf '%s\n' "$json" > "$tmp"
  mv -f "$tmp" "$file"
}

explore_issue_write_state() {
  # channel thread_ts state body_file title reason required_at orig_sha cur_sha obligation_id
  local channel="$1" thread_ts="$2" state="$3" body_file="$4" title="$5"
  local reason="$6" required_at="$7" original_sha="$8" current_sha="$9" obligation_id="${10:-}"
  local file permalink now
  file="$(explore_issue_state_file "$channel" "$thread_ts")"
  permalink="$(explore_issue_permalink "$channel" "$thread_ts")"
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  explore_issue_atomic_write_json "$file" "$(jq -nc \
    --arg state "$state" \
    --arg channel "$channel" \
    --arg thread_ts "$thread_ts" \
    --arg source_permalink "$permalink" \
    --arg body_file "$body_file" \
    --arg title "$title" \
    --arg reason "$reason" \
    --arg required_at "$required_at" \
    --arg original_body_sha256 "$original_sha" \
    --arg current_body_sha256 "$current_sha" \
    --arg obligation_id "$obligation_id" \
    --arg updated_at "$now" \
    '{state: $state, channel: $channel, thread_ts: $thread_ts, source_permalink: $source_permalink, body_file: $body_file, title: $title, reason: $reason, required_at: $required_at, original_body_sha256: $original_body_sha256, current_body_sha256: $current_body_sha256, obligation_id: $obligation_id, updated_at: $updated_at}')"
}

explore_issue_read_state() {
  # channel thread_ts -> prints state JSON (or nothing if absent)
  local file
  file="$(explore_issue_state_file "$1" "$2")"
  [ -f "$file" ] || return 1
  cat "$file"
}

explore_issue_set_state() {
  # channel thread_ts state — transition the per-source state machine
  # (OPEN|IN_PROGRESS|READY_TO_FILE|WAITING_EXTERNAL|RESOLVED_FILED|
  # RESOLVED_NO_ISSUE) while preserving every other record field.
  local channel="$1" thread_ts="$2" state="$3"
  local file now tmp
  file="$(explore_issue_state_file "$channel" "$thread_ts")"
  [ -f "$file" ] || return 1
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  tmp="${file}.tmp.$$"
  if ! jq --arg state "$state" --arg updated_at "$now" \
    '.state = $state | .updated_at = $updated_at' "$file" > "$tmp"; then
    rm -f "$tmp"
    return 1
  fi
  mv -f "$tmp" "$file"
}

explore_issue_mint_permit() {
  # channel thread_ts final_body_sha title_digest obligation_id
  local channel="$1" thread_ts="$2" body_sha="$3" title_digest="$4" obligation_id="$5"
  local file now
  file="$(explore_issue_permit_file "$channel" "$thread_ts")"
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  explore_issue_atomic_write_json "$file" "$(jq -nc \
    --arg channel "$channel" \
    --arg thread_ts "$thread_ts" \
    --arg body_sha256 "$body_sha" \
    --arg title_digest "$title_digest" \
    --arg obligation_id "$obligation_id" \
    --arg minted_at "$now" \
    '{channel: $channel, thread_ts: $thread_ts, body_sha256: $body_sha256, title_digest: $title_digest, obligation_id: $obligation_id, minted_at: $minted_at}')"
}

explore_issue_permit_status() {
  # channel thread_ts body_sha title_digest -> 0 valid, 1 absent, 2 drift
  local channel="$1" thread_ts="$2" body_sha="$3" title_digest="$4"
  local file p_channel p_thread p_body p_title
  file="$(explore_issue_permit_file "$channel" "$thread_ts")"
  [ -f "$file" ] || return 1
  p_channel="$(jq -r '.channel // ""' "$file" 2>/dev/null || true)"
  p_thread="$(jq -r '.thread_ts // ""' "$file" 2>/dev/null || true)"
  p_body="$(jq -r '.body_sha256 // ""' "$file" 2>/dev/null || true)"
  p_title="$(jq -r '.title_digest // ""' "$file" 2>/dev/null || true)"
  if [ "$p_channel" = "$channel" ] && [ "$p_thread" = "$thread_ts" ] \
     && [ "$p_body" = "$body_sha" ] && [ "$p_title" = "$title_digest" ]; then
    return 0
  fi
  return 2
}

explore_issue_consume_permit() {
  # channel thread_ts body_sha title_digest -> 0 consumed (allow create), 1 absent, 2 drift
  local channel="$1" thread_ts="$2" body_sha="$3" title_digest="$4"
  local file consumed now key
  file="$(explore_issue_permit_file "$channel" "$thread_ts")"
  [ -f "$file" ] || return 1
  if ! explore_issue_permit_status "$channel" "$thread_ts" "$body_sha" "$title_digest"; then
    return 2
  fi
  mkdir -p "$EXPLORE_ISSUE_CONSUMED_PERMIT_DIR"
  key="$(explore_issue_source_key "$channel" "$thread_ts")"
  now="$(date -u +%Y%m%dT%H%M%SZ)"
  consumed="$EXPLORE_ISSUE_CONSUMED_PERMIT_DIR/${key}.permit.consumed.${now}.json"
  mv -f "$file" "$consumed" 2>/dev/null || return 1
  return 0
}

explore_issue_resolve_obligation() {
  # channel thread_ts external_state reason
  local channel="$1" thread_ts="$2" external_state="$3" reason="$4"
  "$EXPLORE_ISSUE_PM_OPS_CLI" obligation-resolve \
    --kind "$EXPLORE_ISSUE_KIND" \
    --target-type "$EXPLORE_ISSUE_TARGET_TYPE" \
    --target-id "$(explore_issue_target_id "$channel" "$thread_ts")" \
    --external-state "$external_state" \
    --reason "$reason"
}

explore_issue_resolve_filed() {
  # channel thread_ts issue_number issue_url title body_sha
  local channel="$1" thread_ts="$2" issue_number="$3" issue_url="$4" title="$5" body_sha="$6"
  local file state
  explore_issue_resolve_obligation "$channel" "$thread_ts" RESOLVED_FILED \
    "filed issue #${issue_number} ${issue_url}" || true
  file="$(explore_issue_state_file "$channel" "$thread_ts")"
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
  local file state
  explore_issue_resolve_obligation "$channel" "$thread_ts" RESOLVED_NO_ISSUE \
    "$resolution" || true
  file="$(explore_issue_state_file "$channel" "$thread_ts")"
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
