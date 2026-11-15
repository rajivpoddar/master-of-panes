#!/usr/bin/env bash
# pm-hourly-post.sh: compose + post the 1h heartbeat as HeyDonna Alerts, run by PM directly (no agent).
# Source: hourly-heartbeat skill runtime contract (updated 2026-11-13 after the 20:16 IST report needed two
# manual workarounds: an agent type that may not post to Slack, and a 3,141-char line rejected as invalid_blocks).
# Usage (PM): bash ~/.claude/scripts/pm-hourly-post.sh   (launch with Bash run_in_background:true)
# Prints POSTED ts=<ts> bucket=<b>, or HOURLY_POST_UNCERTAIN/FAILED with the reason. Never resends into an existing
# delivered claim.
# Bucket ownership comes FIRST (CTO REVISE C0ALZJHGE49 thread 1794753780.694859, ts 1794756599.261169): the quota sampler
# (usage-snapshot.py) runs inside compose, so compose may only start after THIS invocation holds the atomic bucket claim.
# A posted or uncertain bucket returns below without composing, so it never reaches any quota endpoint, and two overlapping
# invocations cannot both sample. The claim path is passed as --sample-usage-claim; that flag is the only way to enable the
# sampler. Until the Slack send starts, an empty claim is released on every exit (compose failure, skip, bad first line).
set -uo pipefail
BUCKET=$(date +%Y-%m-%d:%H)
echo "$BUCKET" > /tmp/heartbeat-1h-bucket
CLAIM="$HOME/.claude/hourly-heartbeat-posts/$BUCKET"
mkdir -p "$HOME/.claude/hourly-heartbeat-posts"
if ! mkdir "$CLAIM" 2>/dev/null; then
  if grep -Eq '^OK ts=[0-9]+\.[0-9]+ channel=C0ALZJHGE49$' "$CLAIM/receipt" 2>/dev/null; then
    echo "HOURLY_ALREADY_POSTED bucket=$BUCKET $(cat "$CLAIM/receipt")"; exit 0
  fi
  echo "HOURLY_POST_UNCERTAIN bucket=$BUCKET existing claim without a valid receipt; reconcile in Slack before any retry (an EMPTY claim means a prior run died before the Slack send: confirm no pm-hourly-post.sh is running, then rmdir it)"; exit 1
fi
SEND_STARTED=0
# rmdir only succeeds while the claim is empty, i.e. before the send created receipt/errors; it can never drop a delivered claim.
_release_unsent_claim() { if [ "$SEND_STARTED" = 0 ]; then rmdir "$CLAIM" 2>/dev/null || true; fi; }
trap _release_unsent_claim EXIT
trap 'exit 130' INT TERM HUP
if ! bash /Users/rajiv/.claude/scripts/sakshi-heartbeat.sh compose --mode 1h --sample-usage-claim "$CLAIM" \
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
SEND_RC=0
SEND_STARTED=1  # from here the claim is never released: delivery is UNKNOWN until a valid receipt is read
SLACK_SEND_AS=alerts bash /Users/rajiv/.claude/scripts/slack-send.sh -c C0ALZJHGE49 -f < /tmp/heartbeat-1h-post.txt \
  > "$CLAIM/receipt" 2> "$CLAIM/errors" || SEND_RC=$?
RECEIPT=$(<"$CLAIM/receipt")
if [ "$(grep -Ec '^OK ts=[0-9]+\.[0-9]+ channel=C0ALZJHGE49$' "$CLAIM/receipt" || true)" -ne 1 ]; then
  echo "HOURLY_POST_UNCERTAIN bucket=$BUCKET sender_rc=$SEND_RC errors=$(head -c 300 "$CLAIM/errors")"; exit 1
fi
TS=$(sed -n 's/^OK ts=\([0-9][0-9]*\.[0-9][0-9]*\) channel=C0ALZJHGE49$/\1/p' "$CLAIM/receipt")
/Users/rajiv/.claude/scripts/hourly-thread-ts --set "$TS" --bucket "$BUCKET" --receipt "$RECEIPT" >/dev/null 2>&1 \
  || echo "WARN hourly-thread-ts --set failed for ts=$TS"
# CTO verification reminder (Rajiv C0ALZJHGE49 thread 1794667497.289909, ts 1794668173.816399: "why is cto at mention alerts
# reminder missing for this? it should triggered for each hourly report thread"). MoP's +5m mention is skipped when PM/this
# script posts the parent, so schedule it here: +10 min backstop (Rajiv 21:27 IST ts 1794671837.615089: "the cto review message is missing again"). PM posts the CTO verification itself as soon as Step 3 + per-PR replies are done, and deletes this backstop.
# CTO review reminder is now scheduled +5m by slack-send.sh right after the per-PR status reply (single source; Rajiv ts 1794672251.671699).
# Dead tmux window cleanup, every hourly (Rajiv C0ALZJHGE49 thread 1794671177.730179, ts 1794672013.713759: "there are some tmux dead windows
# which has not been cleaned up yet ... how do we enforce it?"). Deterministic: any window whose pane process has exited (pane_dead=1) in
# sessions 0/proxies, PM scratch windows included, except the PM 'claude' window. Live panes are never touched.
bash /Users/rajiv/.claude/scripts/tmux-dead-window-cleanup.sh --apply 2>&1 | tail -3 | sed "s/^/DEAD_WINDOWS: /"
echo "POSTED ts=$TS bucket=$BUCKET"
# Explicit +5m CTO review reminder with a receipt check (Rajiv C0ALZJHGE49 thread 1794674773.624389, ts 1794675774.355359:
# "where's the cto at mention on this thread"). The sender-side hook did not fire on the 22:16 hourly; this is the primary path.
_AT=$(python3 -c 'import datetime as d;print((d.datetime.now(d.timezone.utc)+d.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"))')
_REM=$(python3 /Users/rajiv/Downloads/projects/heydonna-app/.claude/skills/alerts-reminder/scripts/alerts-schedule.py --channel C0ALZJHGE49 --thread "$TS" --at "$_AT" --text "<@U0BNFGX2UAX> CTO review: please review all PM actions on this hourly with your hourly review skill (15-item checklist reply, Step 3 lane steering with send disposition and checkpoints, per-PR dispositions and STUCK owners, dead windows closed, caps)." 2>&1 | grep -E '^OK scheduled' | head -1)
if [ -n "$_REM" ]; then echo "CTO_REVIEW_REMINDER $_REM"; else echo "CTO_REVIEW_REMINDER_FAILED ts=$TS (PM must post the CTO mention by hand)"; fi
