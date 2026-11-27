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
# Preview only. Never claims a bucket, never samples quotas, never posts to Slack.
#   pm-hourly-post.sh --no-post [SRC] [OUT_PNG]
# prints the absolute PNG path built from the last saved hourly (or SRC).
if [ "${1:-}" = "--no-post" ]; then
  SRC="${2:-/tmp/heartbeat-1h-post.txt}"
  OUT="${3:-/tmp/hourly-chart-preview.png}"
  TXT="${OUT}.txt"
  if bash /Users/rajiv/.claude/scripts/pm/heartbeat/hourly_chart_hook.sh "$SRC" "$TXT" "$OUT"; then
    python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$OUT"
    exit 0
  fi
  echo "CHART_PREVIEW_FAILED"
  exit 1
fi
# Launch queue (Rajiv D0AMF0XE6TS thread 1795760160.752879: "the queue should be drained on proxy stops as well" [and hourly]): one detached,
# fail-open drain per hourly run. It posts nothing to Slack, does not read or write the bucket claim, and a held lock or any error is
# swallowed (rule 50), so it can never delay or fail the report. Idempotent: the drainer's lock + per-row cooldown absorb a repeat run.
if [ "${PROXY_QUEUE_DRAIN:-1}" != 0 ] && [ -f "$HOME/.claude/scripts/proxy-queue-drain.py" ]; then
  ( nohup python3 -I "$HOME/.claude/scripts/proxy-queue-drain.py" drain --trigger hourly > /dev/null 2>&1 < /dev/null & ) 2>/dev/null || true
fi
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
# Routine batch (CTO REVISE #2 C0ALZJHGE49 thread 1795353473.878109, ts 1795354512.689229: "Have the existing hourly wrapper hand the runnable
# HOURLY rows to its existing batch lane ... Keep the Stop hook focused on FAST and avoid a second batch trigger."; REVISE #3, ts
# 1795355302.148809: "Feed the existing hourly lane the private routine-batch.json from the same bucket claim. The hourly report can show
# counts. Record the report post and the lane's acceptance as distinct outcomes, using the existing lane receipt, and keep one trigger.").
# This wrapper runs once per bucket (the atomic claim above). It exports the runnable HOURLY rows from the Stop hook's own classifier
# (`--hourly-batch`, read-only, independent of how many FAST rows are open) into the PRIVATE file $CLAIM/routine-batch.json. Slack gets a
# line with the counts and this private file path: no ledger owners, titles or required actions. MoP no longer injects an hourly-processing
# prompt into the PM pane (Rajiv D0AMF0XE6TS thread 1795794483.299309, ts 1795794743.693179), so this Alerts line is the hand-off of the
# batch path and row count; this script only records the report post. Fail-open (rule 50): any problem posts the report without the line.
RB_TOTAL=""; RB_ROWS=""; RB_IDS=""; RB_FAST=""
_RB_HOOK="${PM_STOP_DRAIN_HOOK:-/Users/rajiv/Downloads/projects/heydonna-app/.claude/hooks/pm-ops-sync-stop-validator.sh}"
IFS= read -r -d '' _RB_PY <<'PY' || true
import json, sys
src, report = sys.argv[1:3]
d = json.load(open(src, encoding='utf-8'))
rows, ids = d.get('rows') or [], d.get('ids') or []
total, fast = int(d.get('hourly_runnable') or 0), int(d.get('fast_runnable') or 0)
if total:
    with open(report, 'a', encoding='utf-8') as handle:
        handle.write("\n*Routine batch*: %d runnable routine row(s); %d FAST row(s) stay on the Stop drain. The hourly processing lane reads %s.\n" % (total, fast, src))
print('%d %d %d %d' % (total, len(rows), len(ids), fast))
PY
if bash "$_RB_HOOK" --hourly-batch "$CLAIM/routine-batch.json" </dev/null > "$CLAIM/routine-batch.export" 2>&1 \
    && [ -s "$CLAIM/routine-batch.json" ] \
    && _RB=$(python3 -I -c "$_RB_PY" "$CLAIM/routine-batch.json" /tmp/heartbeat-1h-post.txt 2>/dev/null); then
  chmod 600 "$CLAIM/routine-batch.json" 2>/dev/null || true
  read -r RB_TOTAL RB_ROWS RB_IDS RB_FAST <<< "$_RB"
else
  echo "ROUTINE_BATCH_SKIPPED the routine batch could not be built ($(head -c 160 "$CLAIM/routine-batch.export" 2>/dev/null | tr '\n\r' '  ')); the report posts without it"
fi
# Charts (Rajiv C0ALZJHGE49 thread 1795495430.992669, ts 1795497671.257839 / ts 1795497761.265599):
# one PNG for burn gauges, OpenCode Go and the proxy lanes. The account-pace table stays in the parent.
# OFF until ~/.claude/state/hourly-chart-enabled exists (Rajiv has not approved the chart): the hook
# then returns rc 3 and this post keeps the original tables, including the account table.
# FAIL OPEN: a missing flag, a missing venv python, a render error, or (after the post) a failed upload
# leaves the current text tables and never blocks this post. The upload itself runs only after the parent receipt is valid.
CHART_READY=0
REPORT_SRC=/tmp/heartbeat-1h-post.txt
CHART_PNG="$CLAIM/hourly-chart.png"
if bash /Users/rajiv/.claude/scripts/pm/heartbeat/hourly_chart_hook.sh \
    "$REPORT_SRC" /tmp/heartbeat-1h-post.chart.txt "$CHART_PNG"; then
  if head -1 /tmp/heartbeat-1h-post.chart.txt | grep -Eq '^(<@[A-Z0-9]+> *)?\*Hourly'; then
    REPORT_SRC=/tmp/heartbeat-1h-post.chart.txt
    CHART_READY=1
  else
    echo "CHART_FALLBACK reason=rewritten_text_lost_first_line"
    rm -f -- /tmp/heartbeat-1h-post.chart.txt /tmp/heartbeat-1h-post.chart.txt.tables "$CHART_PNG"
  fi
fi
# Overflow tables (Rajiv C0ALZJHGE49 thread 1795351517.305319, ts 1795353266.972289: later tables were thread
# replies so each could be its own native Slack table; Slack allows ONE native table block per message).
# Superseded for three tables (Rajiv C0ALZJHGE49 thread 1795488293.729669, ts 1795488682.501089: "move these
# tables to the main report rather than a reply to the thread"): Burn, OpenCode Go and the proxy
# Lane | Model | Liveness table stay in the parent when the chart did not render. When the chart did
# render, those tables are already headlines, and any OTHER later table is still one thread reply.
# The Hourly per-PR status reply is a different sender and is not split here.
# chat.postMessage rejects text over 40,000 characters. Never truncate the parent to fit: report the length
# and do not send. A splitter crash (any rc other than 2) leaves N_TABLE_REPLIES=0 and posts the unsplit report.
SPLIT_RC=0
N_TABLE_REPLIES=$(python3 -I /Users/rajiv/.claude/scripts/pm/heartbeat/hourly_table_split.py \
  "$CLAIM" "$REPORT_SRC" /tmp/heartbeat-1h-post.parent.txt) || SPLIT_RC=$?
if [ "$SPLIT_RC" -eq 2 ]; then
  echo "HOURLY_PARENT_TOO_LONG chars=$N_TABLE_REPLIES limit=40000 (not truncated, not posted)"
  EXIT_REASON="parent_over_slack_text_limit chars=$N_TABLE_REPLIES"
  exit 1
fi
if [ "$SPLIT_RC" -ne 0 ]; then N_TABLE_REPLIES=0; fi
case "$N_TABLE_REPLIES" in ''|*[!0-9]*) N_TABLE_REPLIES=0 ;; esac
PARENT_BODY="$REPORT_SRC"
if [ "$N_TABLE_REPLIES" -gt 0 ] && head -1 /tmp/heartbeat-1h-post.parent.txt | grep -Eq '^(<@[A-Z0-9]+> *)?\*Hourly'; then
  PARENT_BODY=/tmp/heartbeat-1h-post.parent.txt
else
  N_TABLE_REPLIES=0   # no split, or a parent that fails the first-line check: post REPORT_SRC (original text, or the chart headlines)
fi
_OK_RE='^OK ts=[0-9]+\.[0-9]+ channel=C0ALZJHGE49$'
# One overflow-table reply. Best effort: it never writes the parent receipt file and never changes this script's exit status, so a
# failed reply can never turn a delivered parent into HOURLY_POST_UNCERTAIN. Falls back to the old plain pipe text ONLY on Slack's
# explicit rejection of the table block: the sender prints exactly one SLACK_SEND_FAILED line whose error= is invalid_blocks or
# invalid_blocks_format (a parsed ok:false reply, so nothing was posted). An empty, malformed or otherwise unreadable response
# (error=..._delivery_uncertain), a transport error, a timeout, any other Slack error or any unrecognised output is never re-sent,
# so a native reply Slack accepted before the response became unreadable can't appear twice (CTO REVISE ts 1795354634.420069).
_table_reply() {
  local n="$1" base="$CLAIM/table-reply-$1" rc=0
  SLACK_SEND_AS=alerts bash /Users/rajiv/.claude/scripts/slack-send.sh -c C0ALZJHGE49 -t "$TS" -f < "$base.txt" \
    > "$base.receipt" 2> "$base.errors" || rc=$?
  if [ "$rc" -eq 0 ] && [ "$(grep -Ec "$_OK_RE" "$base.receipt" || true)" -eq 1 ]; then
    echo "TABLE_REPLY n=$n native $(cat "$base.receipt")"; return 0
  fi
  if [ "$(grep -c '^SLACK_SEND_FAILED ' "$base.errors" || true)" -eq 1 ] \
     && grep -Eq '^SLACK_SEND_FAILED channel=[A-Z0-9]+ thread=[0-9.]+ error=(invalid_blocks|invalid_blocks_format)$' "$base.errors"; then
    rc=0
    SLACK_TABLE_PLAIN=1 SLACK_SEND_AS=alerts bash /Users/rajiv/.claude/scripts/slack-send.sh -c C0ALZJHGE49 -t "$TS" -f < "$base.txt" \
      > "$base.plain.receipt" 2> "$base.plain.errors" || rc=$?
    if [ "$rc" -eq 0 ] && [ "$(grep -Ec "$_OK_RE" "$base.plain.receipt" || true)" -eq 1 ]; then
      echo "TABLE_REPLY_FALLBACK n=$n plain text (native post rejected: $(head -c 200 "$base.errors" | tr '\n\r' '  ')) $(cat "$base.plain.receipt")"; return 0
    fi
    echo "TABLE_REPLY_FAILED n=$n native post rejected and the plain re-send failed (see $base.plain.errors)"; return 0
  fi
  echo "TABLE_REPLY_UNCERTAIN n=$n sender_rc=$rc, not re-sent (see $base.errors); check the thread before any retry"
}
SEND_RC=0
SEND_STARTED=1  # from here the claim is never released: delivery is UNKNOWN until a valid receipt is read
SLACK_SEND_AS=alerts bash /Users/rajiv/.claude/scripts/slack-send.sh -c C0ALZJHGE49 -f < "$PARENT_BODY" \
  > "$CLAIM/receipt" 2> "$CLAIM/errors" || SEND_RC=$?
RECEIPT=$(<"$CLAIM/receipt")
if [ "$(grep -Ec '^OK ts=[0-9]+\.[0-9]+ channel=C0ALZJHGE49$' "$CLAIM/receipt" || true)" -ne 1 ]; then
  echo "HOURLY_POST_UNCERTAIN bucket=$BUCKET sender_rc=$SEND_RC errors=$(head -c 300 "$CLAIM/errors")"; exit 1
fi
TS=$(sed -n 's/^OK ts=\([0-9][0-9]*\.[0-9][0-9]*\) channel=C0ALZJHGE49$/\1/p' "$CLAIM/receipt")
/Users/rajiv/.claude/scripts/hourly-thread-ts --set "$TS" --bucket "$BUCKET" --receipt "$RECEIPT" >/dev/null 2>&1 \
  || echo "WARN hourly-thread-ts --set failed for ts=$TS"
# Chart image on the parent thread. A failed upload posts the original tables back into the thread
# (plain pipe text) and does not change this script's exit status: the hourly parent is already delivered.
if [ "$CHART_READY" = 1 ]; then
  UP_RC=0
  bash /Users/rajiv/.claude/scripts/slack-upload-file.sh "$CHART_PNG" C0ALZJHGE49 "$TS" "Hourly charts" \
    > "$CLAIM/chart-upload.txt" 2>&1 || UP_RC=$?
  if [ "$UP_RC" -eq 0 ] && grep -q '^OK ' "$CLAIM/chart-upload.txt"; then
    echo "CHART_UPLOADED $(head -1 "$CLAIM/chart-upload.txt")"
  else
    echo "CHART_UPLOAD_FALLBACK rc=$UP_RC (posting the text tables in the thread; the hourly parent was not blocked)"
    if [ -s /tmp/heartbeat-1h-post.chart.txt.tables ]; then
      FB_RC=0
      SLACK_TABLE_PLAIN=1 SLACK_SEND_AS=alerts bash /Users/rajiv/.claude/scripts/slack-send.sh -c C0ALZJHGE49 -t "$TS" -f < /tmp/heartbeat-1h-post.chart.txt.tables \
        > "$CLAIM/chart-fallback.receipt" 2> "$CLAIM/chart-fallback.errors" || FB_RC=$?
      echo "CHART_UPLOAD_FALLBACK_REPLY rc=$FB_RC"
    fi
  fi
fi
# Posting does not establish lane acceptance; the processing lane records its own receipt.
if [ "${RB_TOTAL:-0}" -gt 0 ] 2>/dev/null; then
  printf 'outcome=REPORT_POSTED ts=%s parent_ts=%s bucket=%s hourly_runnable=%s fast_open=%s lane_acceptance=unconfirmed\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$TS" "$BUCKET" "$RB_TOTAL" "$RB_FAST" > "$CLAIM/routine-batch.posted" 2>/dev/null || true
  echo "ROUTINE_BATCH_REPORT_POSTED parent_ts=$TS hourly_runnable=$RB_TOTAL fast_open=$RB_FAST lane_acceptance=unconfirmed record=$CLAIM/routine-batch.posted"
fi
# Overflow-table replies (see the splitter above): after the parent receipt is validated and registered, one Alerts thread reply per
# later table, in report order, each its own message so each renders a native table block.
_n=1
while [ "$_n" -le "$N_TABLE_REPLIES" ]; do _table_reply "$_n"; _n=$((_n + 1)); done
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
# DISABLED 2026-11-16 (Rajiv C0ALZJHGE49 thread 1794836804.990559, ts 1794837337.978459: "stop the cto review of checklist items").
_AT=""
_REM="disabled"
if [ -n "$_AT" ]; then
  _REM=$(python3 /Users/rajiv/Downloads/projects/heydonna-app/.claude/skills/alerts-reminder/scripts/alerts-schedule.py --channel C0ALZJHGE49 --thread "$TS" --at "$_AT" --text "<@U0BNFGX2UAX> CTO review: please review all PM actions on this hourly with your hourly review skill (15-item checklist reply, Step 3 lane steering with send disposition and checkpoints, per-PR dispositions and STUCK owners, dead windows closed, caps)." 2>&1 | grep -E '^OK scheduled' | head -1)
fi
if [ "$_REM" = "disabled" ]; then echo "CTO_REVIEW_REMINDER disabled (Rajiv ts 1794837337.978459)"; elif [ -n "$_REM" ]; then echo "CTO_REVIEW_REMINDER $_REM anchor_ts=$TS"; else echo "CTO_REVIEW_REMINDER_FAILED ts=$TS (PM must post the CTO mention by hand)"; fi

# PM processing reminder (Rajiv C0ALZJHGE49 thread 1795398411.851649, ts 1795400210.759519: "set an alerts reminder on each
# hourly thread to remind you to run the hourly report processing skill correctly 5m after the report is posted. add it as the
# last step in hourly report generation script."). Anchor = the POSTED parent ts + 300 s (ceil, so never early). LAST step.
if [ -n "${TS:-}" ]; then
  _PM_AT=$(python3 -c 'import math,sys,datetime; print(datetime.datetime.fromtimestamp(math.ceil(float(sys.argv[1]))+300, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))' "$TS" 2>/dev/null)
  if [ -n "$_PM_AT" ]; then
    _PM_REM=$(python3 /Users/rajiv/Downloads/projects/heydonna-app/.claude/skills/alerts-reminder/scripts/alerts-schedule.py --channel C0ALZJHGE49 --thread "$TS" --at "$_PM_AT" --text "<@U0ALEAYCAUT> PM: run Skill(hourly-report-processing) for THIS thread now. Load the skill (don't hand-roll from memory) and work its checklist line by line, incl. 0 pace, 3a review-cap sweep, 3b tooling-blocked dual greens, 7 lane steering, 11/11a caps + account switch. Reply here with every line." 2>&1 | tail -1)
    echo "PM_PROCESSING_REMINDER ${_PM_REM:-UNKNOWN} at=$_PM_AT anchor_ts=$TS"
  else
    echo "PM_PROCESSING_REMINDER FAILED: could not compute anchor from ts=$TS"
  fi
fi
