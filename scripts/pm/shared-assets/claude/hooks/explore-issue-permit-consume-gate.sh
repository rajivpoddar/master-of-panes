#!/usr/bin/env bash
# explore-issue-permit-consume-gate.sh — PreToolUse finalizer; runs AFTER all
# other blocking hooks in the Bash chain (position after
# block-invalid-issue-contract-ledger.sh in settings.json).
#
# ROOT-CAUSE FIX for the permit-consumed-but-gh-never-ran defect:
#   pre-issue-create-audit.sh (hook N) writes a pending file instead of
#   consuming the permit. Any hook between N and this finalizer can still
#   block; if it does, the actual permit file is untouched and the pending
#   file is left behind (cleaned up on the next attempt). This hook
#   atomically moves the permit to consumed (persist-UNKNOWN-first design)
#   only when ALL blocking hooks have passed.
#
# FAIL-CLOSED contract (corrected from prior || true version):
#   RC=0 from consume_pending_permit  → permit consumed → exit 0 (allow)
#   RC=1 (no pending, no permit)      → legitimate bypass create → exit 0
#   RC=1 (no pending, permit EXISTS)  → protocol violation → exit 2 (block)
#   RC=2+ from consume_pending_permit → consume error → exit 2 (block)
#   Source parse fails, pending exist → exit 2 (block)
#   Source parse fails, no pending    → exit 0 (allow; no permit possible)
#
# Preserve-UNKNOWN guarantee: if gh fails AFTER this hook returns 0, the permit
# is in consumed/UNKNOWN state. Recovery requires manual
# explore_issue_restore_permit with proven GitHub absence. No unsafe auto-
# restore is wired here.

set -euo pipefail

INPUT=$(cat)
CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)
[ -n "$CMD" ] || exit 0

# Only fire for gh issue create commands (same segment check as pre-audit).
MATCH_GH_ISSUE_CREATE=""
while IFS= read -r seg; do
  seg=$(printf '%s' "$seg" | sed 's/^[[:space:]]*//')
  case "$seg" in echo\ *|printf\ *|cat\ *|bash\ *|source\ *) continue ;; esac
  if printf '%s' "$seg" | grep -qE '^gh[[:space:]]+issue[[:space:]]+create'; then
    MATCH_GH_ISSUE_CREATE="yes"
    break
  fi
done <<< "$(printf '%s' "$CMD" | tr ';' '\n' | tr '|' '\n' | tr '&' '\n')"
[ -n "$MATCH_GH_ISSUE_CREATE" ] || exit 0

# Source the shared guard library.
# shellcheck disable=SC1090
source "${EXPLORE_ISSUE_LATCH_GUARD:-/Users/rajiv/.claude/hooks/explore-issue-latch-guard.sh}"

# ---------------------------------------------------------------------------
# Body and title extraction — exact same helpers as pre-issue-create-audit.sh
# (single-quoted in perl to avoid shell quoting conflicts; identical patterns).
# ---------------------------------------------------------------------------
_cg_extract_body() {
  local cmd="$1" body="" body_file
  # --body-file <path> (double-quoted, single-quoted, or bare)
  body_file=$(printf '%s' "$cmd" | \
    perl -ne 'if (/--body-file[\s=]+(?:"([^"]+)"|'\''([^'\'']+)'\''|(\S+))/) { print $1 || $2 || $3; exit }' \
    2>/dev/null || true)
  if [ -n "$body_file" ] && [ -f "$body_file" ]; then
    body=$(cat "$body_file" 2>/dev/null || true)
  fi
  # --body "..." (double-quoted, unescape \")
  if [ -z "$body" ]; then
    body=$(printf '%s' "$cmd" | \
      perl -ne 'BEGIN{$/=undef} if (/--body[\s=]+"((?:[^"\\]|\\.)*)"/s) { my $v=$1; $v =~ s/\\"/"/g; print $v; exit }' \
      2>/dev/null || true)
  fi
  # --body '...' (single-quoted)
  if [ -z "$body" ]; then
    body=$(printf '%s' "$cmd" | \
      perl -ne "BEGIN{\$/=undef} if (/--body[\s=]+'((?:[^'\\\\]|\\\\.)*)'/s) { print \$1; exit }" \
      2>/dev/null || true)
  fi
  printf '%s' "$body"
}

_cg_extract_title() {
  local cmd="$1"
  printf '%s' "$cmd" | \
    perl -ne 'BEGIN{$/=undef} if (/--title[\s=]+"((?:[^"\\]|\\.)*)"/s) { my $v=$1; $v =~ s/\\"/"/g; print $v; exit }' \
    2>/dev/null || true
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    printf '%s' "$1" | sha256sum | awk '{print $1}'
  else
    printf '%s' "$1" | shasum -a 256 | awk '{print $1}'
  fi
}

BODY=$(_cg_extract_body "$CMD")
BODY_OR_CMD="${BODY:-$CMD}"
TITLE=$(_cg_extract_title "$CMD")
BODY_SHA=$(sha256_of "$BODY_OR_CMD")
TITLE_DIGEST=$(sha256_of "${TITLE:-}")

# ---------------------------------------------------------------------------
# Source identity extraction — same python3 parser as pre-issue-create-audit.sh.
# Cache-only path: the finalizer never makes live Slack API calls because the
# source was already corroborated by pre-audit before the pending was written.
# GitHub comment URLs are treated as NONE (identity verified in pre-audit;
# the pending file holds the resolved channel/thread key).
# ---------------------------------------------------------------------------
SOURCE_RESULT=$(printf '%s' "$BODY_OR_CMD" | python3 -c '
import json, os, re, sys, urllib.parse
body = sys.stdin.read()
lines = re.findall(r"(?im)^\s*(?:[-*]\s*)?(?:source|handoff)\s*:\s*(.*?)\s*$", body)
if not lines:
    print("NONE"); sys.exit()
if len(lines) != 1:
    print("INVALID"); sys.exit()
line = lines[0]
pair = re.fullmatch(r"\s*([CD][A-Z0-9]{10}):([0-9]{10}\.[0-9]{6})\s*", line)
if pair:
    print(pair.group(1), pair.group(2), pair.group(2)); sys.exit()
url = re.fullmatch(r"\s*<?(https?://[^\s<>]+)>?\s*", line)
if not url:
    print("INVALID"); sys.exit()
u = urllib.parse.urlsplit(url.group(1))
host = (u.hostname or "").lower()
if host == "github.com":
    # GitHub comment URL: treat as NONE for the finalizer; identity was
    # verified in pre-audit; the pending file holds the resolved key.
    print("NONE"); sys.exit()
m = re.fullmatch(r"/archives/([CD][A-Z0-9]{10})/p([0-9]{16})", u.path)
q = urllib.parse.parse_qs(u.query)
if not m or not host.endswith(".slack.com") or len(q.get("thread_ts", [])) != 1:
    print("INVALID"); sys.exit()
channel, digits = m.groups()
reply = digits[:10] + "." + digits[10:]
root = q["thread_ts"][0]
if not re.fullmatch(r"[0-9]{10}\.[0-9]{6}", root) or ("cid" in q and q["cid"] != [channel]):
    print("INVALID"); sys.exit()
# Cache-only: use the cache written by pre-audit; never make a live API call.
cache_dir = os.environ.get("EXPLORE_ISSUE_SLACK_CACHE_DIR", "/tmp/slack-thread-cache")
cache = os.path.join(cache_dir, root + ".json")
try:
    with open(cache) as f: data = json.load(f)
except (OSError, ValueError): data = {}
def matches(d):
    return (d.get("channel") == channel and
            any(x.get("ts") == reply and x.get("thread_ts", x.get("ts")) == root
                for x in d.get("messages", [])) and
            any(x.get("ts") == root for x in d.get("messages", [])))
if not matches(data):
    print("INVALID"); sys.exit()
print(channel, root, reply)
' 2>/dev/null || true)

CHANNEL=""
THREAD_TS=""
if [ -n "$SOURCE_RESULT" ] && [ "$SOURCE_RESULT" != "NONE" ] && [ "$SOURCE_RESULT" != "INVALID" ]; then
  read -r CHANNEL THREAD_TS EXPLORE_ISSUE_SOURCE_MESSAGE_TS <<< "$SOURCE_RESULT"
  export EXPLORE_ISSUE_SOURCE_MESSAGE_TS
fi

PENDING_DIR="${EXPLORE_ISSUE_PERMIT_PENDING_DIR:-/tmp/heydonna-explore-issue-permit-pending}"

# ---------------------------------------------------------------------------
# Source parse failed: fail closed if any pending permit files exist.
# ---------------------------------------------------------------------------
if [ -z "$CHANNEL" ] || [ -z "$THREAD_TS" ]; then
  HAS_PENDING=0
  if [ -d "$PENDING_DIR" ]; then
    for _pf in "$PENDING_DIR"/*.permit.pending.json; do
      [ -f "$_pf" ] && HAS_PENDING=1 && break
    done 2>/dev/null || true
  fi
  if [ "$HAS_PENDING" -eq 1 ]; then
    printf '%s\n' '{"decision":"block","reason":"source-parse-failed-with-pending","message":"BLOCKED: gh issue create has a Source line that could not be resolved to a channel:thread_ts pair, but a pending permit exists in the queue. Cannot safely proceed — the create may require a permit that cannot be matched. Correct the Source: line or use the CHANNEL:THREAD_TS form before retrying.","context":"finalizer-fail-closed"}'
    exit 2
  fi
  # No pending, no parseable source — cannot be a permit-required create.
  exit 0
fi

# ---------------------------------------------------------------------------
# Consume the pending permit (fail-closed on any error).
# ---------------------------------------------------------------------------
CONSUME_RC=0
explore_issue_consume_pending_permit "$CHANNEL" "$THREAD_TS" "$BODY_SHA" "$TITLE_DIGEST" \
  || CONSUME_RC=$?

if [ "$CONSUME_RC" -eq 0 ]; then
  # Permit consumed successfully; gh may proceed.
  exit 0
fi

if [ "$CONSUME_RC" -eq 1 ]; then
  # No matching pending permit. Check whether a valid permit file exists for
  # this source, which would indicate pre-audit failed to write the pending
  # marker — a protocol violation that must block to protect the one-use guarantee.
  _permit_file=""
  _permit_file="$(explore_issue_permit_file "$CHANNEL" "$THREAD_TS" 2>/dev/null || true)"
  if [ -n "$_permit_file" ] && [ -f "$_permit_file" ]; then
    printf '%s\n' '{"decision":"block","reason":"permit-exists-no-pending","message":"BLOCKED: A valid permit exists for this source but no pending state was written by pre-issue-create-audit.sh. This indicates the deferred-consumption protocol was not followed or a stale pending mismatch cleared it before this run. The permit is INTACT — rerun from the pre-audit hook so it writes the pending marker, then retry the create.","context":"finalizer-fail-closed"}'
    exit 2
  fi
  # No permit and no pending — legitimate bypass create (audit-bypass marker,
  # internal-origin, or non-customer create that pre-audit allowed without a permit).
  exit 0
fi

# CONSUME_RC >= 2: pending was found but actual permit consumption failed
# (permit drifted, already consumed from another path, or UNKNOWN record
# could not be written). Fail closed to preserve the one-use guarantee.
printf '%s\n' '{"decision":"block","reason":"consume-error","message":"BLOCKED: Pending permit was found for this source but actual permit consumption failed (permit may have drifted, been consumed already from another path, or the UNKNOWN WAL record could not be written). The create cannot proceed safely. Use explore_issue_permit_status to check the permit state and reconcile before retrying.","context":"finalizer-fail-closed"}'
exit 2
