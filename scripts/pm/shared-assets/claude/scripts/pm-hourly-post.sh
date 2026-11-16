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
# sampler.
# Custody marker (CTO REVISE C0ALZJHGE49 thread 1794753780.694859, ts 1794758174.846249): the sampler writes nothing into the
# claim, so an empty claim after compose started proves nothing. Right BEFORE compose (the only way to reach the sampler) this
# invocation writes sampling_started (timestamp + pid) into the claim. From then on quota I/O MAY have begun, so every exit that
# has not started the Slack send (compose failure, bad first line, INT/TERM/HUP, any crash) KEEPS the claim and records why in
# sampling_retained; a SIGKILL leaves the marker itself. Only a claim that provably never reached sampling is removed: it is
# still empty, the marker write failed (compose was never launched), or the sakshi-heartbeat.sh shim reported collectors absent
# (it exits 3 as a lone stderr line before its exec, so neither compose nor the sampler ran).
# Known and deliberately NOT changed here: `if ! cmd; then rc=$?` below always reads rc=0 (separate follow-up).
set -uo pipefail
BUCKET=$(date +%Y-%m-%d:%H)
echo "$BUCKET" > /tmp/heartbeat-1h-bucket
CLAIM="$HOME/.claude/hourly-heartbeat-posts/$BUCKET"
mkdir -p "$HOME/.claude/hourly-heartbeat-posts"
if ! mkdir "$CLAIM" 2>/dev/null; then
  if grep -Eq '^OK ts=[0-9]+\.[0-9]+ channel=C0ALZJHGE49$' "$CLAIM/receipt" 2>/dev/null; then
    echo "HOURLY_ALREADY_POSTED bucket=$BUCKET $(cat "$CLAIM/receipt")"; exit 0
  fi
  if [ -e "$CLAIM/sampling_started" ] || [ -e "$CLAIM/sampling_retained" ]; then
    echo "HOURLY_POST_UNCERTAIN bucket=$BUCKET existing claim without a valid receipt; it holds the sampling_started marker, so the quota sampler and the Slack send may have run: this is RECONCILIATION, not deletion. Read $CLAIM/sampling_started and sampling_retained, prove the recorded pid and any compose or usage-snapshot child are gone, check usage-snapshots.jsonl and Slack for this bucket, and never rmdir it just to sample again"; exit 1
  fi
  echo "HOURLY_POST_UNCERTAIN bucket=$BUCKET existing claim without a valid receipt; reconcile in Slack before any retry (a claim with NO sampling_started marker and no receipt/errors means a prior run died before compose could start: confirm no pm-hourly-post.sh is running, then rmdir it; if it holds receipt or errors the send may have started, so reconcile instead)"; exit 1
fi
SAMPLING_MARKED=0   # 1 = quota I/O may have begun: the claim is never removed
SEND_STARTED=0
EXIT_REASON=""
_settle_unsent_claim() {
  local rc=$?
  trap - EXIT
  if [ "$SEND_STARTED" = 0 ]; then
    if [ "$SAMPLING_MARKED" = 1 ]; then
      # Sampling may have begun: keep custody and record why (advisory; the sampling_started marker alone already pins the claim).
      printf 'ts=%s pid=%s exit_rc=%s reason=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$$" "$rc" "${EXIT_REASON:-exit_before_send}" \
        > "$CLAIM/sampling_retained" 2>/dev/null || true
    else
      # rmdir only succeeds on an EMPTY claim, so it can never drop a marked or delivered claim.
      rmdir "$CLAIM" 2>/dev/null || true
    fi
  fi
  exit "$rc"
}
_claim_note() { if [ "$SAMPLING_MARKED" = 1 ]; then echo "claim $BUCKET RETAINED (sampling may have begun; reconcile, never rmdir to re-sample)"; else echo "claim $BUCKET released (compose never ran)"; fi; }
trap _settle_unsent_claim EXIT
trap 'EXIT_REASON=signal_INT; exit 130' INT
trap 'EXIT_REASON=signal_TERM; exit 130' TERM
trap 'EXIT_REASON=signal_HUP; exit 130' HUP
SAMPLING_MARKED=1
if ! printf 'ts=%s pid=%s bucket=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$$" "$BUCKET" > "$CLAIM/sampling_started"; then
  rm -f -- "$CLAIM/sampling_started"; SAMPLING_MARKED=0   # compose is launched only after this write succeeds
  echo "HOURLY_COMPOSE_FAILED could not record sampling_started in the claim; compose was not launched ($(_claim_note))"; exit 1
fi
if ! bash /Users/rajiv/.claude/scripts/sakshi-heartbeat.sh compose --mode 1h --sample-usage-claim "$CLAIM" \
    --save-inputs /tmp/heartbeat-1h-latest > /tmp/heartbeat-1h-post.raw.txt 2>/tmp/heartbeat-1h-compose.err; then
  rc=$?
  # Provable no-sample: the shim's collectors-absent exit is a lone stderr line emitted before its exec of compose.
  if [ "$(grep -c . /tmp/heartbeat-1h-compose.err)" = 1 ] && grep -q '^HEARTBEAT_COLLECTORS_ABSENT: ' /tmp/heartbeat-1h-compose.err; then
    rm -f -- "$CLAIM/sampling_started"; SAMPLING_MARKED=0
  fi
  EXIT_REASON="compose_failed: $(tail -1 /tmp/heartbeat-1h-compose.err | tr '\n\r' '  ' | head -c 200)"
  if [ "$rc" = 3 ]; then echo "HOURLY_SKIPPED: collectors absent ($(tail -1 /tmp/heartbeat-1h-compose.err))"; exit 0; fi
  echo "HOURLY_COMPOSE_FAILED rc=$rc $(tail -1 /tmp/heartbeat-1h-compose.err) ($(_claim_note))"; exit 1
fi
# Slack rejects any block over 3000 chars (invalid_blocks); cap each line at 2900.
awk 'length($0)>2900{print substr($0,1,2800) " … (truncated; full list in /tmp/heartbeat-1h-latest)";next}{print}' \
  /tmp/heartbeat-1h-post.raw.txt > /tmp/heartbeat-1h-post.txt
# The real 1h composer prefixes a CTO mention when Asks is present (CTO REVISE on MoP 54e8f693, 2026-11-13).
head -1 /tmp/heartbeat-1h-post.txt | grep -Eq '^(<@[A-Z0-9]+> *)?\*Hourly' || { EXIT_REASON="compose_output_rejected_before_send"; echo "HOURLY_COMPOSE_FAILED unexpected first line ($(_claim_note))"; exit 1; }
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
# Explicit CTO review reminder with a receipt check (Rajiv C0ALZJHGE49 thread 1794674773.624389, ts 1794675774.355359:
# "where's the cto at mention on this thread"). The sender-side hook did not fire on the 22:16 hourly; this is the primary path.
# Anchor = the POSTED parent ts ($TS, from the confirmed receipt) + 300 s, NOT the wrapper exit time. Slack post_at is whole
# seconds, so use ceil(ts)+300: the reminder never fires before ts+300. Before: now()+300 landed ~93 s past ts+300 (CTO audits 11:16, 12:15).
_AT=$(python3 -c 'import datetime as d,math,sys;print(d.datetime.fromtimestamp(math.ceil(float(sys.argv[1]))+300,d.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))' "$TS" 2>/dev/null)
_REM=""
if [ -n "$_AT" ]; then
  _REM=$(python3 /Users/rajiv/Downloads/projects/heydonna-app/.claude/skills/alerts-reminder/scripts/alerts-schedule.py --channel C0ALZJHGE49 --thread "$TS" --at "$_AT" --text "<@U0BNFGX2UAX> CTO review: please review all PM actions on this hourly with your hourly review skill (15-item checklist reply, Step 3 lane steering with send disposition and checkpoints, per-PR dispositions and STUCK owners, dead windows closed, caps)." 2>&1 | grep -E '^OK scheduled' | head -1)
fi
if [ -n "$_REM" ]; then echo "CTO_REVIEW_REMINDER $_REM anchor_ts=$TS"; else echo "CTO_REVIEW_REMINDER_FAILED ts=$TS (PM must post the CTO mention by hand)"; fi
