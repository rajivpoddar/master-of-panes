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
# Routine batch hand-off (CTO REVISE #2 C0ALZJHGE49 thread 1795353473.878109, ts 1795354512.689229: "A runnable FAST item can keep the
# routine batch from reaching the hourly lane across successive hours ... Have the existing hourly wrapper hand the runnable HOURLY
# rows to its existing batch lane ... Keep the Stop hook focused on FAST and avoid a second batch trigger."). This wrapper runs once
# per bucket (the atomic claim above), so it is the ONE batch trigger: the Stop hook only blocks on FAST and never lists routine rows.
# The runnable HOURLY rows come from the Stop hook's own classifier (`--hourly-batch`, read-only), independent of how many FAST rows are
# open, and go into this report as a bounded section: PM's hourly-report-processing batch lane ("ONE batched agent per hour") reads it
# from the posted report and from the ROUTINE_BATCH_HANDOFF line below. Fail-open (rule 50): any problem posts the report without it.
RB_TOTAL=""; RB_ROWS=""; RB_IDS=""; RB_FAST=""
_RB_HOOK="${PM_STOP_DRAIN_HOOK:-/Users/rajiv/Downloads/projects/heydonna-app/.claude/hooks/pm-ops-sync-stop-validator.sh}"
IFS= read -r -d '' _RB_PY <<'PY' || true
import json, re, sys
src, report, section = sys.argv[1:4]
d = json.load(open(src, encoding='utf-8'))
clean = lambda t: re.sub(r'[<>]', '', ' '.join(str(t or '').split()))   # no accidental Slack mentions from row titles
rows, ids = d.get('rows') or [], d.get('ids') or []
total, fast = int(d.get('hourly_runnable') or 0), int(d.get('fast_runnable') or 0)
if total:
    lines = ['', "*Routine batch (this hour's one batched agent; does not block Stop)*: %d runnable routine row(s); %d FAST row(s) stay on the Stop drain" % (total, fast)]
    for r in rows:
        lines.append('• obligation:%s [hourly] owner=%s %s: %s' % (r['id'], clean(r.get('owner')), clean(r.get('title')), clean(r.get('step'))))
    if ids:
        lines.append('also routine runnable: ' + ','.join(str(i) for i in ids))
    if d.get('deferred'):
        lines.append('+%d more routine rows (pm-ops.py obligation-list --actionable --format json)' % d['deferred'])
    text = '\n'.join(lines) + '\n'
    open(section, 'w', encoding='utf-8').write(text)
    with open(report, 'a', encoding='utf-8') as handle:
        handle.write(text)
print('%d %d %d %d' % (total, len(rows), len(ids), fast))
PY
if bash "$_RB_HOOK" --hourly-batch "$CLAIM/routine-batch.json" </dev/null > "$CLAIM/routine-batch.export" 2>&1 \
    && [ -s "$CLAIM/routine-batch.json" ] \
    && _RB=$(python3 -I -c "$_RB_PY" "$CLAIM/routine-batch.json" /tmp/heartbeat-1h-post.txt "$CLAIM/routine-batch.section.txt" 2>/dev/null); then
  read -r RB_TOTAL RB_ROWS RB_IDS RB_FAST <<< "$_RB"
else
  echo "ROUTINE_BATCH_SKIPPED the routine batch could not be built ($(head -c 160 "$CLAIM/routine-batch.export" 2>/dev/null | tr '\n\r' '  ')); the report posts without it"
fi
# Overflow tables as native thread replies (Rajiv C0ALZJHGE49 thread 1795351517.305319, ts 1795353266.972289: "why did these two
# tables not get converted to slack boldkit format like the top one?"). Slack allows ONE native table block per message, and the
# sender renders every later table in the same message as plain pipe text, so the Burn and OpenCode Go tables (built by the same
# _pipe_table helper as the pace table) showed as raw pipes. The first table stays in the parent; each later table, with its one-line
# intro, goes out as a thread reply under the parent, so each is its own message with its own native table block. A table over
# Slack's 100-row block limit is paged into several replies with the header repeated. Columns and content are unchanged.
# Failure-safe: any splitter problem leaves N_TABLE_REPLIES=0 and the unsplit report is posted exactly as before.
IFS= read -r -d '' _SPLIT_PY <<'PY' || true
import re, sys
claim, src, out = sys.argv[1:4]
ROW = re.compile(r'^\s*\|[^\n]*\|[^\n]*\|')    # same row / separator tests as slack-send.sh convert_text_to_blocks
SEP = re.compile(r'^\s*\|[-:\s|]+\|')
MAX_BODY_ROWS = 99                               # a Slack table block holds 100 rows, header row included
lines = open(src, encoding='utf-8').read().split('\n')
runs, i = [], 0
while i < len(lines):
    if ROW.match(lines[i]):
        j = i
        while j < len(lines) and ROW.match(lines[j]):
            j += 1
        runs.append((i, j))
        i = j
    else:
        i += 1
drop, replies = set(), []
for start, end in runs[1:]:                      # the first table stays in the parent
    run = lines[start:end]
    body = [l for l in run[1:] if not SEP.match(l)]
    if not body:
        continue                                 # header only: nothing to render natively, leave it in the parent
    rule = [run[1]] if SEP.match(run[1]) else []
    first = start
    intro = []
    if start >= 2 and lines[start - 1].strip() and not lines[start - 2].strip():
        intro, first = [lines[start - 1]], start - 1     # a one-line paragraph right above the table is its intro
    for page in range(0, len(body), MAX_BODY_ROWS):
        replies.append('\n'.join((intro if page == 0 else []) + [run[0]] + rule + body[page:page + MAX_BODY_ROWS]) + '\n')
    drop.update(range(first, end))
if replies:
    parent, dropped = [], False
    for idx, line in enumerate(lines):
        if idx in drop:
            dropped = True
            continue
        if dropped and not line.strip() and parent and not parent[-1].strip():
            dropped = False                      # the removal left two blank lines in a row: keep one
            continue
        dropped = False
        parent.append(line)
    for n, text in enumerate(replies, 1):
        open('%s/table-reply-%d.txt' % (claim, n), 'w', encoding='utf-8').write(text)
    open(out, 'w', encoding='utf-8').write('\n'.join(parent))
print(len(replies))
PY
N_TABLE_REPLIES=$(python3 -I -c "$_SPLIT_PY" "$CLAIM" /tmp/heartbeat-1h-post.txt /tmp/heartbeat-1h-post.parent.txt 2>/dev/null) || N_TABLE_REPLIES=0
case "$N_TABLE_REPLIES" in ''|*[!0-9]*) N_TABLE_REPLIES=0 ;; esac
PARENT_BODY=/tmp/heartbeat-1h-post.txt
if [ "$N_TABLE_REPLIES" -gt 0 ] && head -1 /tmp/heartbeat-1h-post.parent.txt | grep -Eq '^(<@[A-Z0-9]+> *)?\*Hourly'; then
  PARENT_BODY=/tmp/heartbeat-1h-post.parent.txt
else
  N_TABLE_REPLIES=0   # no split, or a parent that fails the first-line check: post the whole report as one message, as before
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
# Routine batch receipt: written only now, after the parent receipt is valid, so it names the parent ts the batch went out in. The claim
# is per hour and the section is part of the one parent post, so a second run in this bucket exits above and cannot hand it off twice.
if [ -n "$RB_TOTAL" ]; then
  printf 'ts=%s parent_ts=%s bucket=%s hourly_runnable=%s rows=%s ids=%s fast_open=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$TS" "$BUCKET" \
    "$RB_TOTAL" "$RB_ROWS" "$RB_IDS" "$RB_FAST" > "$CLAIM/routine-batch.handoff" 2>/dev/null || true
  echo "ROUTINE_BATCH_HANDOFF parent_ts=$TS hourly_runnable=$RB_TOTAL listed_rows=$RB_ROWS listed_ids=$RB_IDS fast_open=$RB_FAST receipt=$CLAIM/routine-batch.handoff"
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
