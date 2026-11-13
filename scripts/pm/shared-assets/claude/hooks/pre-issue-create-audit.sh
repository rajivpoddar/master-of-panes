#!/bin/bash
# pre-issue-create-audit.sh — Block `gh issue create` for customer-origin
# issues unless a one-use READY_TO_FILE permit exists; on every denied
# customer-origin create, atomically upsert ONE durable HIGH hourly pm-ops
# obligation keyed by the Slack source.
#
# Rajiv directive 2026-04-21 13:43: enforce /explore-issue gate on issue creation
# to prevent the exploration-skipping pattern that caused #3633 SOP skip, #3634
# masking fix, #3636 duplicate filing.
#
# Rajiv directive 2026-05-01 22:27: extend with Slack-thread verification for
# customer-origin classification. audit-bypass marker is REJECTED for
# customer-origin issues. See feedback_pm_explore_issue_for_all_customer_reported.md.
#
# Rajiv directive 2026-05-26 22:40 IST thread `1779814519.469209`: mechanical gate
# for pre-issue-create-audit hook bypass anti-pattern. The session latch
# introduced here is REPLACED by the durable source-keyed obligation gate
# (CTO directive 2026-08-05, thread 1785934775.195809): one pm-ops obligation
# per Slack source (kind=explore_issue_required, severity=high, horizon=hourly,
# owner=pm, dedupe_group=explore-issue:<CHANNEL>:<THREAD_TS>) drives the intake;
# a one-use READY_TO_FILE permit bound to channel/thread_ts/final body SHA/title
# digest is the only release; the stop hook is non-blocking with a valid
# open/in-progress obligation.
#
# Logic:
# 1. Only fire on `gh issue create` (not list/view/edit/close/comment).
# 2. Extract issue body (from --body or --body-file) and --title.
# 3. Extract Slack thread refs from the body; derive CHANNEL/THREAD_TS.
# 4. PERMIT GATE: a valid READY_TO_FILE permit for this source
#    (channel/thread_ts/body SHA/title digest all bound) is consumed atomically
#    and the create is ALLOWED. A drifted permit is rejected fail-closed.
# 5. Otherwise classify origin (Slack API cache / bot allowlist / internal IDs /
#    keyword fallback).
# 6. Customer-origin → BLOCK + atomically upsert ONE obligation keyed by the
#    Slack source + write the per-source OPEN state record. audit-bypass markers
#    are NOT accepted for customer-origin.
# 7. Internal/unknown with bypass marker → allow (unchanged).
# 8. Non-customer without marker → existing MoP Codex-evidence gate + CONFIRMED
#    verdict check (unchanged).

set -euo pipefail

INPUT=$(cat)
CMD=$(echo "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)

if [ -z "$CMD" ]; then
  exit 0
fi

# Split on pipeline operators to check each segment, skipping echo/cat/bash
# blocks so we don't false-positive on quoted strings (same pattern as
# block-all-pr-merge.sh).
MATCH_GH_ISSUE_CREATE=""
while IFS= read -r seg; do
  seg=$(echo "$seg" | sed 's/^[[:space:]]*//')
  if echo "$seg" | grep -qE '^(echo |printf |cat |bash |source )'; then
    continue
  fi
  if echo "$seg" | grep -qE '^gh\s+issue\s+create'; then
    MATCH_GH_ISSUE_CREATE="yes"
    break
  fi
done <<< "$(echo "$CMD" | tr ';' '\n' | tr '|' '\n' | tr '&' '\n')"

if [ -z "$MATCH_GH_ISSUE_CREATE" ]; then
  exit 0
fi

# ---------------------------------------------------------------------------
# Extract issue body content from --body or --body-file, and --title
# ---------------------------------------------------------------------------
FALLBACK_LOG=${EXPLORE_ISSUE_FALLBACK_LOG:-/tmp/pre-issue-audit-fallback.log}
SLACK_CACHE_DIR=${EXPLORE_ISSUE_SLACK_CACHE_DIR:-/tmp/slack-thread-cache}
mkdir -p "$SLACK_CACHE_DIR" 2>/dev/null || true

extract_body() {
  local cmd="$1"
  local body=""

  # --body-file <path>
  local body_file
  body_file=$(echo "$cmd" | perl -ne 'if (/--body-file[\s=]+(?:"([^"]+)"|'\''([^'\'']+)'\''|(\S+))/) { print $1 || $2 || $3; exit }' 2>/dev/null || true)
  if [ -n "$body_file" ] && [ -f "$body_file" ]; then
    body=$(cat "$body_file" 2>/dev/null || true)
  fi

  # --body "..." (only if body still empty)
  if [ -z "$body" ]; then
    body=$(echo "$cmd" | perl -ne 'BEGIN{$/=undef} if (/--body[\s=]+"((?:[^"\\]|\\.)*)"/s) { my $v=$1; $v =~ s/\\"/"/g; print $v; exit }' 2>/dev/null || true)
  fi
  if [ -z "$body" ]; then
    body=$(echo "$cmd" | perl -ne "BEGIN{\$/=undef} if (/--body[\s=]+'((?:[^'\\\\]|\\\\.)*)'/s) { print \$1; exit }" 2>/dev/null || true)
  fi

  printf '%s' "$body"
}

extract_body_file() {
  local cmd="$1"
  echo "$cmd" | perl -ne 'if (/--body-file[\s=]+(?:"([^"]+)"|'\''([^'\'']+)'\''|(\S+))/) { print $1 || $2 || $3; exit }' 2>/dev/null || true
}

extract_title() {
  local cmd="$1"
  echo "$cmd" | perl -ne 'BEGIN{$/=undef} if (/--title[\s=]+"((?:[^"\\]|\\.)*)"/s) { my $v=$1; $v =~ s/\\"/"/g; print $v; exit }' 2>/dev/null || true
}

BODY=$(extract_body "$CMD")
BODY_FILE_PATH=$(extract_body_file "$CMD")
TITLE=$(extract_title "$CMD")
# Fall back to whole command string if extraction yielded nothing — preserves
# old behavior where the marker grep ran against the raw command.
BODY_OR_CMD="${BODY:-$CMD}"

# ---------------------------------------------------------------------------
# Body-file hand-off contract (CP repair 2026-09-20, Rajiv directive
# 1789902323.668159 "repair the issue create trap").
#
# The audit-bypass marker lives INSIDE the issue body. When --body-file names a
# path this hook cannot read (a file that does not exist yet, or a path still
# written as an unresolved shell variable), BODY is empty and the old fallback
# to the raw command produced the WRONG remedy ("requires prior Codex
# architecture review") while the ledger hook independently refused the same
# command for the unreadable body file - a contradictory double block whose
# combined advice was unsatisfiable. Refuse once, with the one remedy that
# satisfies both hooks.
# ---------------------------------------------------------------------------
if [ -n "$BODY_FILE_PATH" ] && { [ ! -f "$BODY_FILE_PATH" ] || [ ! -r "$BODY_FILE_PATH" ]; }; then
  cat <<BLOCK
{"decision": "block", "message": "BLOCKED: --body-file '${BODY_FILE_PATH}' cannot be read when this hook runs (missing file, an existing file the hook may not read, or a path that is still an unresolved shell variable), so the issue body - including any <!-- audit-bypass: internal-followup --> marker - cannot be classified. Remedy: write the completed body to a LITERAL path in a previous Write/Edit step, validate it, then run exactly one gh issue create with that literal --body-file path. Do not compute the path with a shell variable and do not create the body file inside the same Bash call."}
BLOCK
  exit 0
fi

# sha256 helper (portable mac+linux)
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    printf '%s' "$1" | sha256sum | awk '{print $1}'
  else
    printf '%s' "$1" | shasum -a 256 | awk '{print $1}'
  fi
}

# explore-issue-latch-guard: context-aware customer signal
source "${EXPLORE_ISSUE_LATCH_GUARD:-/Users/rajiv/.claude/hooks/explore-issue-latch-guard.sh}"

BODY_SHA=$(sha256_of "$BODY_OR_CMD")
CMD_SHA=$(sha256_of "$CMD")
TITLE_DIGEST=$(sha256_of "${TITLE:-}")

# ---------------------------------------------------------------------------
# Slack thread / channel extraction
# ---------------------------------------------------------------------------
# Only a labelled Source/handoff line can supply the pair. Never combine a
# channel from one line with a timestamp from quoted, unrelated material.
# A reply URL must be corroborated by Slack's thread response before use.
SOURCE_RESULT=$(printf '%s' "$BODY_OR_CMD" | python3 -c '
import json, os, re, subprocess, sys, urllib.parse
body = sys.stdin.read()
lines = re.findall(r"(?im)^\s*(?:[-*]\s*)?(?:source|handoff)\s*:\s*(.*?)\s*$", body)
if not lines:
    print("NONE")
    sys.exit()
if len(lines) != 1:
    print("INVALID")
    sys.exit()
line = lines[0]
pair = re.fullmatch(r"\s*([CD][A-Z0-9]{10}):([0-9]{10}\.[0-9]{6})\s*", line)
if pair:
    print(pair.group(1), pair.group(2), pair.group(2))
    sys.exit()
url = re.fullmatch(r"\s*<?(https?://[^\s<>]+)>?\s*", line)
if not url:
    print("INVALID")
    sys.exit()
u = urllib.parse.urlsplit(url.group(1))
host = (u.hostname or "").lower()
if host == "github.com":
    gm = re.fullmatch(r"/(?:Scribie|heydonna-app)/heydonna-app/(?:pull|issues)/[0-9]+", u.path)
    cm = re.fullmatch(r"issuecomment-([0-9]+)", u.fragment or "")
    # One GitHub comment URL is a source. The shell verifies the comment exists.
    # Any other GitHub URL stays INVALID. Do not accept a renamed label.
    if gm and cm and not u.query:
        print("GITHUB", cm.group(1))
    else:
        print("INVALID")
    sys.exit()
m = re.fullmatch(r"/archives/([CD][A-Z0-9]{10})/p([0-9]{16})", u.path)
q = urllib.parse.parse_qs(u.query)
if not m or not host.endswith(".slack.com") or len(q.get("thread_ts", [])) != 1:
    print("INVALID")
    sys.exit()
channel, digits = m.groups()
reply = digits[:10] + "." + digits[10:]
root = q["thread_ts"][0]
if not re.fullmatch(r"[0-9]{10}\.[0-9]{6}", root) or ("cid" in q and q["cid"] != [channel]):
    print("INVALID")
    sys.exit()
cache = os.path.join(os.environ.get("EXPLORE_ISSUE_SLACK_CACHE_DIR", "/tmp/slack-thread-cache"), root + ".json")
data = {}
try:
    with open(cache) as f: data = json.load(f)
except (OSError, ValueError): pass
def matches(d):
    return d.get("ok") is True and any(x.get("ts") == reply and x.get("thread_ts", x.get("ts")) == root for x in d.get("messages", [])) and any(x.get("ts") == root for x in d.get("messages", []))
# Cached results alone do not establish channel identity; only use a cache
# explicitly tagged with the channel. Otherwise query Slack for this pair.
if data.get("channel") != channel or not matches(data):
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        try:
            with open(os.environ.get("EXPLORE_ISSUE_ENV_LOCAL", os.path.expanduser("~/Downloads/projects/heydonna-app/.env.local"))) as f:
                for entry in f:
                    if entry.startswith("SLACK_BOT_TOKEN="):
                        token = entry.split("=", 1)[1].strip().strip("\"\047")
                        break
        except OSError: pass
    if token:
        args = urllib.parse.urlencode({"channel": channel, "ts": root, "limit": 1000})
        try:
            p = subprocess.run(["curl", "-s", "--max-time", "5", "-H", "Authorization: Bearer " + token, "https://slack.com/api/conversations.replies?" + args], capture_output=True, text=True, timeout=6)
            data = json.loads(p.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired): data = {}
    else: data = {}
if not matches(data):
    print("INVALID")
else:
    # Reuse the corroborated response for origin classification; a later reply
    # need not appear in the first ten messages fetched by the legacy path.
    data["channel"] = channel
    try:
        with open(cache + ".tmp." + str(os.getpid()), "w") as f: json.dump(data, f)
        os.replace(cache + ".tmp." + str(os.getpid()), cache)
    except OSError: pass
    print(channel, root, reply)
' || true)
CHANNEL=""
THREAD_TS=""
# A single heydonna-app issue/PR comment URL is a source only after the
# comment id reads back. Failure stays a source-identity block. The body
# line is not rewritten.
if [[ "$SOURCE_RESULT" == GITHUB\ * ]]; then
  GITHUB_COMMENT_ID="${SOURCE_RESULT#GITHUB }"
  if gh api "repos/heydonna-app/heydonna-app/issues/comments/${GITHUB_COMMENT_ID}" --jq .id >/dev/null 2>&1; then
    SOURCE_RESULT="NONE"
  else
    printf '%s\n' '{"decision":"block","message":"BLOCKED: Source identity is a GitHub comment URL but that comment could not be read. Keep the single Source: line; do not rename it. No permit was consumed and no obligation was written."}'
    exit 0
  fi
fi
if [ "$SOURCE_RESULT" = "INVALID" ] || [ -z "$SOURCE_RESULT" ]; then
  printf '%s\n' '{"decision":"block","message":"BLOCKED: Source identity is missing, ambiguous, or its reply-to-root correspondence could not be verified. Provide one Source: CHANNEL:ROOT or a verified Source permalink. No permit was consumed and no obligation was written."}'
  exit 0
fi
if [ "$SOURCE_RESULT" != "NONE" ]; then
  read -r CHANNEL THREAD_TS EXPLORE_ISSUE_SOURCE_MESSAGE_TS <<< "$SOURCE_RESULT"
  export EXPLORE_ISSUE_SOURCE_MESSAGE_TS
  EXPLORE_ISSUE_SELECTED_KEY="$(explore_issue_source_key "$CHANNEL" "$THREAD_TS")" || exit 2
  SELECTED_STATE="$(explore_issue_state_file "$CHANNEL" "$THREAD_TS")"
  if [ -f "$SELECTED_STATE" ] && jq -e ' .state == "RESOLVED_FILED" or .state == "RESOLVED_NO_ISSUE" ' "$SELECTED_STATE" >/dev/null; then
    printf '%s\n' '{"decision":"block","message":"BLOCKED: This source is already terminal. For a new report, use the later reply permalink with the original thread_ts. The terminal record was not changed."}'
    exit 0
  fi
fi

# ---------------------------------------------------------------------------
# READY_TO_FILE permit gate (one-use; source/body-bound)
# ---------------------------------------------------------------------------
PERMIT_STATUS=""
if [ -n "$THREAD_TS" ]; then
  if explore_issue_permit_status "$CHANNEL" "$THREAD_TS" "$BODY_SHA" "$TITLE_DIGEST"; then
    PERMIT_STATUS=0
  else
    PERMIT_STATUS=$?
  fi
  if [ "$PERMIT_STATUS" = "0" ]; then
    # Valid permit — write a pending marker; the finalizer hook
    # (explore-issue-permit-consume-gate.sh, which runs AFTER all other blocking
    # hooks including block-invalid-issue-contract-ledger.sh) atomically consumes
    # the permit so that any intervening hook block preserves the permit intact.
    # The actual persist-UNKNOWN-first consumption happens in the finalizer.
    if explore_issue_write_permit_pending "$CHANNEL" "$THREAD_TS" "$BODY_SHA" "$TITLE_DIGEST"; then
      exit 0  # Pending written; finalizer will consume right before gh runs.
    fi
    PERMIT_STATUS=3  # Consumed guard hit: permit already consumed/UNKNOWN.
  fi
  if [ "$PERMIT_STATUS" = "3" ]; then
    printf '%s\n' '{"decision":"block","message":"BLOCKED: This source already consumed its create permit; the create result may be UNKNOWN. Reconcile by searching GitHub for the canonical Source permalink. Do not re-mint or create again for this source."}'
    exit 0
  fi
fi

# ---------------------------------------------------------------------------
# Legacy false-positive latch invalidation (proven legacy latches only)
# ---------------------------------------------------------------------------
# explore-issue-latch-guard: invalidate only a proven legacy false positive
EXPLORE_ISSUE_INVALIDATED_DIR="${EXPLORE_ISSUE_INVALIDATED_DIR:-/tmp/heydonna-explore-issue-invalidated}"
if [ -n "$THREAD_TS" ]; then
  LATCH_FILE=""
  explore_issue_invalidate_false_positive_latch "$LATCH_FILE" "$EXPLORE_ISSUE_INVALIDATED_DIR" || true
fi

# ---------------------------------------------------------------------------
# Internal-user classification (regex on display name OR exact user IDs).
# Includes:
#   UEQTTB97A    — Rajiv
#   U0AMF0XE6TS  — (legacy / Dhruva DM)
#   U0ALEAYCAUT  — Dhruva PM bot (Slack user_id)
#   U0AJZTN7SM6  — HeyDonna Alerts bot (Slack user_id) — added per Rajiv directive 2026-05-26 22:40 IST
# ---------------------------------------------------------------------------
INTERNAL_USER_IDS_REGEX='^(U0ALEAYCAUT|U0AMF0XE6TS|UEQTTB97A|U0AJZTN7SM6|U0BNFGX2UAX)$'
# Structured CUSTOMER SOURCE signal (real signal, not free text): the ids of
# the customer-facing / feedback Slack channels. Empty by default — an
# operator/CTO names the real channels explicitly; until then classification
# rests on the originator identity (bot/app/user allowlists) below.
CUSTOMER_CHANNELS_REGEX="${EXPLORE_ISSUE_CUSTOMER_CHANNELS_REGEX:-}"
INTERNAL_NAME_REGEX='Rajiv|Dhruva|Dhurva|HeyDonna PM|HeyDonna Alerts|Codex|claude|mop|master.of.panes'

# Internal infra bot allowlist (NEW). bot_id OR app_id match → internal regardless of user_id.
INTERNAL_BOT_IDS_REGEX='^(B0ALH9R1LRK|B0AJ3HSC2PQ|B0BNMPL5EN6)$'
INTERNAL_APP_IDS_REGEX='^(A0ALQA1BVLL|A0AHQ6WMKF1)$'

ORIGIN_CLASS=""   # "internal" | "customer" | "unknown"
ORIGIN_REASON=""

if [ -n "$THREAD_TS" ]; then
  CACHE_FILE="$SLACK_CACHE_DIR/${THREAD_TS}.json"
  USE_CACHE=""
  if [ -f "$CACHE_FILE" ]; then
    MTIME=$(stat -c %Y "$CACHE_FILE" 2>/dev/null || stat -f %m "$CACHE_FILE" 2>/dev/null || echo 0)
    [ -z "$MTIME" ] && MTIME=0
    AGE=$(( $(date +%s) - MTIME ))
    if [ "$AGE" -lt 3600 ] && [ -s "$CACHE_FILE" ]; then
      USE_CACHE="yes"
    fi
  fi

  if [ -z "$USE_CACHE" ]; then
    # Source slot env to get SLACK_BOT_TOKEN (best-effort)
    SLACK_BOT_TOKEN="${SLACK_BOT_TOKEN:-}"
    ENV_LOCAL="${EXPLORE_ISSUE_ENV_LOCAL:-$HOME/Downloads/projects/heydonna-app/.env.local}"
    if [ -z "$SLACK_BOT_TOKEN" ] && [ -f "$ENV_LOCAL" ]; then
      SLACK_BOT_TOKEN=$(grep -E '^SLACK_BOT_TOKEN=' "$ENV_LOCAL" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true)
    fi
    if [ -n "$SLACK_BOT_TOKEN" ]; then
      curl -s --max-time 5 -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
        "https://slack.com/api/conversations.replies?channel=${CHANNEL}&ts=${THREAD_TS}&limit=10" \
        > "$CACHE_FILE" 2>/dev/null || true
    else
      echo "[$(date -u +%FT%TZ)] no SLACK_BOT_TOKEN — falling back to regex" >> "$FALLBACK_LOG"
    fi
  fi

  if [ -f "$CACHE_FILE" ] && [ -s "$CACHE_FILE" ]; then
    OK=$(jq -r '.ok // false' "$CACHE_FILE" 2>/dev/null || echo "false")
    if [ "$OK" = "true" ]; then
      ORIGINATOR_USER=$(jq -r --arg ts "$EXPLORE_ISSUE_SOURCE_MESSAGE_TS" --arg root "$THREAD_TS" '([.messages[]? | select(.ts == $ts)][0] // (if $ts == $root then .messages[0] else null end)).user // empty' "$CACHE_FILE" 2>/dev/null || true)
      ORIGINATOR_USERNAME=$(jq -r --arg ts "$EXPLORE_ISSUE_SOURCE_MESSAGE_TS" --arg root "$THREAD_TS" '([.messages[]? | select(.ts == $ts)][0] // (if $ts == $root then .messages[0] else null end)).username // empty' "$CACHE_FILE" 2>/dev/null || true)
      ORIGINATOR_BOT_ID=$(jq -r --arg ts "$EXPLORE_ISSUE_SOURCE_MESSAGE_TS" --arg root "$THREAD_TS" '([.messages[]? | select(.ts == $ts)][0] // (if $ts == $root then .messages[0] else null end)).bot_id // empty' "$CACHE_FILE" 2>/dev/null || true)
      ORIGINATOR_APP_ID=$(jq -r --arg ts "$EXPLORE_ISSUE_SOURCE_MESSAGE_TS" --arg root "$THREAD_TS" '([.messages[]? | select(.ts == $ts)][0] // (if $ts == $root then .messages[0] else null end)).app_id // empty' "$CACHE_FILE" 2>/dev/null || true)

      if [ -n "$ORIGINATOR_BOT_ID" ] && echo "$ORIGINATOR_BOT_ID" | grep -qE "$INTERNAL_BOT_IDS_REGEX"; then
        ORIGIN_CLASS="internal"
        ORIGIN_REASON="bot_id $ORIGINATOR_BOT_ID (infra-bot allowlist)"
      elif [ -n "$ORIGINATOR_APP_ID" ] && echo "$ORIGINATOR_APP_ID" | grep -qE "$INTERNAL_APP_IDS_REGEX"; then
        ORIGIN_CLASS="internal"
        ORIGIN_REASON="app_id $ORIGINATOR_APP_ID (infra-bot allowlist)"
      elif [ -n "$ORIGINATOR_USER" ] && echo "$ORIGINATOR_USER" | grep -qE "$INTERNAL_USER_IDS_REGEX"; then
        ORIGIN_CLASS="internal"
        ORIGIN_REASON="user-id $ORIGINATOR_USER"
      elif [ -n "$ORIGINATOR_USERNAME" ] && echo "$ORIGINATOR_USERNAME" | grep -qiE "$INTERNAL_NAME_REGEX"; then
        ORIGIN_CLASS="internal"
        ORIGIN_REASON="username $ORIGINATOR_USERNAME"
      elif [ -n "$ORIGINATOR_USER" ]; then
        ORIGIN_CLASS="customer"
        ORIGIN_REASON="non-internal user-id $ORIGINATOR_USER"
      elif [ -n "$ORIGINATOR_BOT_ID" ]; then
        ORIGIN_CLASS="unknown"
        ORIGIN_REASON="bot $ORIGINATOR_BOT_ID without internal-name match"
      fi
    else
      echo "[$(date -u +%FT%TZ)] Slack API !ok ($(jq -r '.error // "?"' "$CACHE_FILE" 2>/dev/null)) for thread $THREAD_TS" >> "$FALLBACK_LOG"
    fi
  fi
fi

# Origin classification uses STRUCTURED signals only:
#   1. the source channel identity (a customer/feedback channel id), and
#   2. the originator identity (internal bot/app/user allowlists, or a
#      non-internal user id).
#
# The former free-text scan of the issue body is GONE. Every issue body carries
# our own ledger/issue-template boilerplate ("- Customer-notification owner,
# channel, and timing: N/A", template "Reported by:" rows, ...), so scanning
# body text classified internal follow-ups as customer-origin and demanded a
# READY_TO_FILE permit the filing never needed. Body prose is not a source
# signal; if a prose-only signal is ever genuinely required it must arrive as a
# structured field instead.
if [ "$ORIGIN_CLASS" = "internal" ]; then
  : # internal allowlist already resolved the originator
elif [ -n "$CUSTOMER_CHANNELS_REGEX" ] && printf '%s' "$CHANNEL" | grep -qE "$CUSTOMER_CHANNELS_REGEX"; then
  ORIGIN_CLASS="customer"
  ORIGIN_REASON="customer source channel $CHANNEL"
elif [ -z "$ORIGIN_CLASS" ]; then
  ORIGIN_CLASS="unknown"
  ORIGIN_REASON="${ORIGIN_REASON:-no resolvable source or originator signal}"
fi

HAS_BYPASS_MARKER=""
if echo "$BODY_OR_CMD" | grep -qE '<!-- audit-bypass: internal-followup -->'; then
  HAS_BYPASS_MARKER="yes"
fi

# ---------------------------------------------------------------------------
# Durable obligation helper — BLOCK + atomically upsert ONE obligation keyed
# by the Slack source, and write/refresh the per-source OPEN state record.
# ---------------------------------------------------------------------------
write_durable_block() {
  local detail="$1" reason="$2" now_iso required_at
  now_iso=$(date -u +%FT%TZ)
  required_at="$now_iso"
  local state_file existing_original original_sha
  state_file="$(explore_issue_state_file "$CHANNEL" "$THREAD_TS")"
  existing_original=""
  if [ -f "$state_file" ]; then
    existing_original="$(jq -r '.original_body_sha256 // ""' "$state_file" 2>/dev/null || true)"
  fi
  original_sha="${existing_original:-$BODY_SHA}"

  # Atomic upsert: exactly ONE open obligation per source. If the upsert
  # fails, still BLOCK fail-closed with the gap note; the durable obligation
  # is the PM wake mechanism for the intake.
  local obligation_id
  obligation_id="$(explore_issue_upsert_obligation \
    "$CHANNEL" "$THREAD_TS" \
    "$(explore_issue_permalink "$CHANNEL" "$THREAD_TS")" \
    "$BODY_FILE_PATH" "$TITLE" "$reason" "$required_at" \
    "$original_sha" "$BODY_SHA" 2>/dev/null || true)"
  obligation_id="$(printf '%s' "$obligation_id" | grep -E '^[0-9]+$' | head -1 || true)"

  explore_issue_write_state "$CHANNEL" "$THREAD_TS" OPEN \
    "$BODY_FILE_PATH" "$TITLE" "$reason" "$required_at" \
    "$original_sha" "$BODY_SHA" "$obligation_id" || true

  local gap_note=""
  if [ -z "$obligation_id" ]; then
    gap_note="\nCONTROL_PLANE_GAP: durable obligation could not be written — surface this to PM."
  fi
  cat <<BLOCK
{"decision": "block", "message": "BLOCKED: Customer-origin issue (${detail}) requires /explore-issue validation. audit-bypass marker is NOT accepted for customer-origin issues. A durable HIGH hourly pm-ops obligation is open for this Slack source; the one-use READY_TO_FILE permit (minted after CONFIRMED review) is the only release.\n\nObligation kind=explore_issue_required source=$(explore_issue_target_id "$CHANNEL" "$THREAD_TS") (dedupe_group=explore-issue:$(explore_issue_target_id "$CHANNEL" "$THREAD_TS"))${gap_note}\nState file: ${state_file}\nRemedy: run Skill(explore-issue) with this source; after CONFIRMED the agent mints the permit, files the issue, and resolves the obligation."}
BLOCK
  exit 0
}

# ---------------------------------------------------------------------------
# Decision tree
# ---------------------------------------------------------------------------

# Drifted permit (source/body/title no longer match) → fail closed. The agent
# must re-mint after the final body/title is fixed.
if [ -n "$THREAD_TS" ] && [ "$PERMIT_STATUS" = "2" ]; then
  cat <<BLOCK
{"decision": "block", "message": "BLOCKED: READY_TO_FILE permit drift for ${CHANNEL}:${THREAD_TS} — the create body or title no longer matches the permit's bound final body SHA/title digest. Re-run the explore-issue agent so it re-mints a permit against the exact final body/title, then retry the identical command. Replay/edited retries are rejected."}
BLOCK
  exit 0
fi

# Customer-origin with no resolvable source ts: BLOCK with an explicit remedy
# and write NOTHING. An obligation keyed (CHANNEL, "") could never match a
# permit (permits are always minted against a real ts), so upserting one would
# strand a structurally unreleasable obligation.
if [ "$ORIGIN_CLASS" = "customer" ] && [ -z "$THREAD_TS" ]; then
  cat <<BLOCK
{"decision": "block", "message": "BLOCKED: Customer-origin issue with no resolvable Source Slack thread_ts requires /explore-issue validation. No durable obligation was written because an empty thread identity can never match a permit.\n\nRemedy: attach the Source Slack permalink (dotted ts or .../p<ts> form) to the issue body, then run Skill(explore-issue) with that source; after CONFIRMED the agent mints the permit, files the issue, and resolves the obligation."}
BLOCK
  exit 0
fi

# Customer-origin → REJECT bypass marker; BLOCK + durable obligation upsert.
if [ "$ORIGIN_CLASS" = "customer" ]; then
  DETAIL="Slack thread ${THREAD_TS:-(none)} originated from non-internal user (${ORIGIN_REASON})"
  write_durable_block "$DETAIL" "customer-origin: ${ORIGIN_REASON}"
fi

# Internal/unknown with bypass marker → allow (existing behavior).
if [ "$ORIGIN_CLASS" = "internal" ] && [ -n "$HAS_BYPASS_MARKER" ]; then
  exit 0
fi
if [ -n "$HAS_BYPASS_MARKER" ]; then
  # No customer signals, no internal classification, marker present → allow
  # (preserves prior behavior for purely internal follow-ups).
  exit 0
fi

# ---------------------------------------------------------------------------
# MoP events Codex-evidence gate (existing logic, non-customer path)
# ---------------------------------------------------------------------------
NOW=$(date +%s)
WINDOW_START=$((NOW - 1800))

# PM-pane Codex architecture-review evidence gate REMOVED (Rajiv, DM D0AMF0XE6TS
# thread 1794566037.087919, ts 1794566485.412849: "drop it"). Slots are retired; every
# proxy dev run already performs Codex plan/code/QA reviews, and customer-origin issues
# still require the explore-issue CONFIRMED permit above. Backup:
# ~/.claude/hooks/pre-issue-create-audit.sh.bak-20261113-droparch

# ---------------------------------------------------------------------------
# Verdict gate: source-bound per-investigation verdict file
#
# Defects fixed vs. prior implementation:
#   1. Loose grep: grep for CONFIRMED anywhere matched "observation CONFIRMED"
#      even when Verdict: line said NEEDS_DEEPER_INVESTIGATION. Now uses anchored
#      python3 parsing that extracts only the value on the Verdict: line.
#   2. Absent/stale file silently permitted FIX/CONFIRMED claims. Now blocks.
#   3. INVESTIGATION bypassed the verdict check entirely (including when verdict
#      was absent, stale, invalid, or CANNOT_REPRODUCE). Now INVESTIGATION still
#      requires a fresh verdict file; it is only allowed when the parsed verdict
#      is CONFIRMED, NEEDS_DEEPER_INVESTIGATION, or MISDIAGNOSED.
#
# Non-verdict creates (no Status: FIX/CONFIRMED/INVESTIGATION claim, or no
# Source: line) are preserved as-is and do not require a verdict file.
# ---------------------------------------------------------------------------

# Parse the anchored Verdict: line from a file and return one of:
#   CONFIRMED | NEEDS_DEEPER_INVESTIGATION | MISDIAGNOSED | CANNOT_REPRODUCE
#   ABSENT  — file missing or unreadable
#   STALE   — file exists but mtime <= WINDOW_START
#   INVALID — file present and fresh but no recognized Verdict: line
_vg_parse_verdict() {
  local path="$1" window_start="$2"
  python3 - "$path" "$window_start" <<'PYEOF'
import os, re, sys
path, window_start = sys.argv[1], int(sys.argv[2])
try:
    s = os.stat(path)
except OSError:
    print("ABSENT"); sys.exit()
if int(s.st_mtime) <= window_start:
    print("STALE"); sys.exit()
try:
    content = open(path).read()
except OSError:
    print("ABSENT"); sys.exit()
# Anchored to start of line; value must be a known verdict word; optional
# trailing prose (e.g. "CONFIRMED - looks right") is allowed after a non-word
# boundary character.
m = re.search(
    r"(?im)^Verdict:\s+(CONFIRMED|NEEDS_DEEPER_INVESTIGATION|MISDIAGNOSED|CANNOT_REPRODUCE)(?:[^_A-Z]|$)",
    content,
)
print(m.group(1) if m else "INVALID")
PYEOF
}

# Classify the body claim that determines which verdict is required.
# Only Status: FIX and Status: INVESTIGATION trigger the verdict gate;
# all other bodies (internal creates, follow-ups without a Status: claim)
# are preserved without change.
_VG_CLAIMS_FIX=false
_VG_CLAIMS_INVESTIGATION=false
if printf '%s' "$BODY_OR_CMD" | grep -qiE '^Status:[[:space:]]*(FIX|CONFIRMED)'; then
  _VG_CLAIMS_FIX=true
fi
if printf '%s' "$BODY_OR_CMD" | grep -qiE '^Status:[[:space:]]*INVESTIGATION' && \
   printf '%s' "$BODY_OR_CMD" | grep -qiE '^Cause:[[:space:]]*(HYPOTHESIS|UNKNOWN)'; then
  _VG_CLAIMS_INVESTIGATION=true
fi

if [ -n "$THREAD_TS" ] && { [ "$_VG_CLAIMS_FIX" = "true" ] || [ "$_VG_CLAIMS_INVESTIGATION" = "true" ]; }; then
  _VERDICT_PATH="$(explore_issue_verdict_path "$CHANNEL" "$THREAD_TS" 2>/dev/null || true)"
  _VG_VERDICT="ABSENT"
  if [ -n "$_VERDICT_PATH" ]; then
    _VG_VERDICT="$(_vg_parse_verdict "$_VERDICT_PATH" "$WINDOW_START")"
  fi

  if [ "$_VG_CLAIMS_FIX" = "true" ]; then
    # FIX/CONFIRMED body: requires CONFIRMED and nothing else.
    # ABSENT, STALE, INVALID, NEEDS_DEEPER_INVESTIGATION, MISDIAGNOSED and
    # CANNOT_REPRODUCE all block.
    if [ "$_VG_VERDICT" != "CONFIRMED" ]; then
      printf '%s\n' "{\"decision\": \"block\", \"message\": \"BLOCKED: Body claims Status: FIX or CONFIRMED but Codex verdict is '${_VG_VERDICT}' for ${CHANNEL}:${THREAD_TS}. A CONFIRMED verdict is required. Address the concerns raised by Codex, or file a tracking ticket with Status: INVESTIGATION + Cause: HYPOTHESIS/UNKNOWN instead.\"}"
      exit 0
    fi
  else
    # INVESTIGATION + HYPOTHESIS/UNKNOWN body.
    # Allowed: CONFIRMED, NEEDS_DEEPER_INVESTIGATION, MISDIAGNOSED.
    # Blocked: ABSENT, STALE, INVALID, CANNOT_REPRODUCE.
    case "$_VG_VERDICT" in
      CONFIRMED|NEEDS_DEEPER_INVESTIGATION|MISDIAGNOSED)
        : # Verdict is acceptable for a tracking/investigation ticket.
        ;;
      *)
        printf '%s\n' "{\"decision\": \"block\", \"message\": \"BLOCKED: Body declares Status: INVESTIGATION but Codex verdict is '${_VG_VERDICT}' for ${CHANNEL}:${THREAD_TS}. An INVESTIGATION ticket requires a fresh verdict file containing CONFIRMED, NEEDS_DEEPER_INVESTIGATION, or MISDIAGNOSED. A missing, stale, or CANNOT_REPRODUCE verdict does not permit filing.\"}"
        exit 0
        ;;
    esac
  fi
fi

# All gates passed. Allow gh issue create.
exit 0
