#!/usr/bin/env bash
# pm-hourly-post.sh: compose + post the 1h heartbeat as HeyDonna Alerts, run by PM directly (no agent).
# Source: hourly-heartbeat skill runtime contract (updated 2026-11-13 after the 20:16 IST report needed two
# manual workarounds: an agent type that may not post to Slack, and a 3,141-char line rejected as invalid_blocks).
# Usage (PM): bash ~/.claude/scripts/pm-hourly-post.sh   (launch with Bash run_in_background:true)
# Prints POSTED ts=<ts> bucket=<b>, or HOURLY_POST_UNCERTAIN/FAILED with the reason. Never resends into an existing
# delivered claim.
set -uo pipefail
BUCKET=$(date +%Y-%m-%d:%H)
echo "$BUCKET" > /tmp/heartbeat-1h-bucket
if ! bash /Users/rajiv/.claude/scripts/sakshi-heartbeat.sh compose --mode 1h \
    --save-inputs /tmp/heartbeat-1h-latest > /tmp/heartbeat-1h-post.raw.txt 2>/tmp/heartbeat-1h-compose.err; then
  rc=$?
  if [ "$rc" = 3 ]; then echo "HOURLY_SKIPPED: collectors absent ($(tail -1 /tmp/heartbeat-1h-compose.err))"; exit 0; fi
  echo "HOURLY_COMPOSE_FAILED rc=$rc $(tail -1 /tmp/heartbeat-1h-compose.err)"; exit 1
fi
# Slack rejects any block over 3000 chars (invalid_blocks); cap each line at 2900.
awk 'length($0)>2900{print substr($0,1,2800) " … (truncated; full list in /tmp/heartbeat-1h-latest)";next}{print}' \
  /tmp/heartbeat-1h-post.raw.txt > /tmp/heartbeat-1h-post.txt
# The real 1h composer prefixes a CTO mention when Asks is present (CTO REVISE on MoP 54e8f693, 2026-11-13).
head -1 /tmp/heartbeat-1h-post.txt | grep -Eq '^(<@[A-Z0-9]+> *)?\*Hourly' || { echo "HOURLY_COMPOSE_FAILED unexpected first line"; exit 1; }
CLAIM="$HOME/.claude/hourly-heartbeat-posts/$BUCKET"
mkdir -p "$HOME/.claude/hourly-heartbeat-posts"
if ! mkdir "$CLAIM" 2>/dev/null; then
  if grep -Eq '^OK ts=[0-9]+\.[0-9]+ channel=C0ALZJHGE49$' "$CLAIM/receipt" 2>/dev/null; then
    echo "HOURLY_ALREADY_POSTED bucket=$BUCKET $(cat "$CLAIM/receipt")"; exit 0
  fi
  echo "HOURLY_POST_UNCERTAIN bucket=$BUCKET existing claim without a valid receipt; reconcile in Slack before any retry"; exit 1
fi
SEND_RC=0
SLACK_SEND_AS=alerts bash /Users/rajiv/.claude/scripts/slack-send.sh -c C0ALZJHGE49 -f < /tmp/heartbeat-1h-post.txt \
  > "$CLAIM/receipt" 2> "$CLAIM/errors" || SEND_RC=$?
RECEIPT=$(<"$CLAIM/receipt")
if [ "$(grep -Ec '^OK ts=[0-9]+\.[0-9]+ channel=C0ALZJHGE49$' "$CLAIM/receipt" || true)" -ne 1 ]; then
  echo "HOURLY_POST_UNCERTAIN bucket=$BUCKET sender_rc=$SEND_RC errors=$(head -c 300 "$CLAIM/errors")"; exit 1
fi
TS=$(sed -n 's/^OK ts=\([0-9][0-9]*\.[0-9][0-9]*\) channel=C0ALZJHGE49$/\1/p' "$CLAIM/receipt")
/Users/rajiv/.claude/scripts/hourly-thread-ts --set "$TS" --bucket "$BUCKET" --receipt "$RECEIPT" >/dev/null 2>&1 \
  || echo "WARN hourly-thread-ts --set failed for ts=$TS"
echo "POSTED ts=$TS bucket=$BUCKET"
