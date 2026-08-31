# HeyDonna PM chat-history monitor

## Single-flight and wake-delivery commit barrier (hard, every tick)

This automation runs as one bounded heartbeat tick and then exits. It must not
sleep, call a wait tool, keep the turn alive for polling, or overlap the next
heartbeat. The older keep-alive/polling language in this file is superseded by
this bounded-tick contract.

Before reading or classifying sources, create a unique `tick_id` and run:

```bash
python3 /Users/rajiv/.codex/monitors/heydonna-pm-chat/delivery_guard.py \
  acquire-tick --tick-id "$tick_id" --ttl-seconds 600
```

`TICK_BUSY` is a healthy overlap suppression: read no sources, deliver no
wakes, do not change watermarks, record `overlap_suppressed=true`, and exit.
The owning tick must run `release-tick` after its final durable state write.

Immediately before every `send_message_to_thread`, reserve the exact complete
fingerprint durably:

```bash
python3 /Users/rajiv/.codex/monitors/heydonna-pm-chat/delivery_guard.py \
  reserve-wake --tick-id "$tick_id" --fingerprint "$fingerprint" \
  --source "$source_coordinate"
```

Only `WAKE_RESERVED` permits delivery. `DUPLICATE_SUPPRESSED` forbids another
send even when the PM JSONL offset, in-memory candidate, or bounded tick was
replayed. After a successful thread-messaging receipt, run `mark-delivered`
before advancing any source or reply watermark. After a definite delivery
failure, run `mark-failed`, retain the source event, set
`monitor_integrity_failure=true`, and do not retry automatically. A `reserved`
record left by interruption is an uncertain delivery and must also suppress
automatic resend; reconcile it against the receiving CTO wake/ack ledger and
escalate integrity rather than guessing. This ordering is mandatory:

`tick lease -> fingerprint reservation -> send -> delivery commit -> watermark -> tick release`.

## Shared release-conveyor contract

The normative ownership and motion matrix is
`/Users/rajiv/.codex/skills/_shared/release-conveyor-contract.md`. This
monitor is read-only: it reports and routes work but never mutates GitHub,
MoP, workflows, slots, or product state. Every report row keeps these fields
separate: `workflow_motion`, `owner_source`, and `hold_reason`; absence of a
running workflow is not evidence that the owner is unknown.

Treat the following as hard actionable wakes: code/proof-ready with no
exact-head CI/E2E admission for 10 minutes; a CI or capture terminal awaiting
its next release boundary; and a free compatible slot with executable open-PR
drain work. For every open PR, classify exactly one motion state and include
`next_action`, `next_owner`, and `wake`. Labels, holds, relays, watching, idle
slot claims, queued shells, and historical/skipped runs do not satisfy an
active lane. Emit exceptions first; only a verified clean enumeration may
report `open_pr_activity_gaps=0`.

### Normalized open-PR motion

For each open PR at its exact current head, classify exactly one of
`CI_IN_PROGRESS`, `CAPTURE_IN_PROGRESS`, `REPRO_OR_PROOF_IN_PROGRESS`,
`REWORK_IN_PROGRESS`, `REWORK_BLOCKED`, `DEPENDENCY_BLOCKED`, or
`PROCESS_LIMBO`. Active states require live exact-head execution evidence;
blocked states require a current blocker class, one owner, and a
machine-observable wake, otherwise the state is `PROCESS_LIMBO`.

Keep `workflow_motion`, `owner_source`, and `hold_reason` separate. Absence of
a running workflow never implies `owner=unknown`. Every nonterminal row names
`next_action`, `next_owner`, and `wake`. Limbo and UNKNOWN rows are
exceptions-first, make Actions needed NOT_CLEAR, and include PR, branch, full
head, last meaningful exact-head run, claimed owner, missing predicate,
smallest executable boundary, and wake. Only a verified zero-gap enumeration
may clear Actions needed.

On any control-plane refusal, route the first literal blocker with the exact
PR, full head, and current labels immediately; do not retry marker shapes,
owner tuples, projected metadata, review receipts, or alternate commands. The
receiving CTO wake must either execute/delegate the safe manual edge in that
same wake or record concrete data-loss, security/privacy, or irreversible harm
as the reason motion is unsafe. A guarded CTO label edge is one fresh-fenced,
journaled, idempotent edge preserving unrelated labels; this monitor only
reports/routes it and never performs the mutation.

## Primary source

Monitor the active PM Claude session JSONL under:

`/Users/rajiv/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/*.jsonl`

Discover the active file rather than hard-coding a session UUID. `/clear`
creates a new session file, so the newest JSONL in this exact directory is the
PM stream authority:

1. At startup and before every poll, list `*.jsonl` directly in the directory
   above and select the file with the newest modification time. Do not recurse
   into `tool-results/`, memory, or worktree directories.
2. Use `sessionId` and cwd records as validation, not as a reason to retain an
   older file when a newer JSONL exists.
3. Track selected path, device/inode, byte offset, last complete newline,
   session ID, and last record timestamp.
4. On the monitor's first adoption only, baseline the then-newest file at its
   current end so historical records do not create wakes.
5. When a later poll finds a different newest JSONL, treat it as one or more
   `/clear` rotations. Build `rotation_recovery_queue` from every top-level
   JSONL after the durable selected file through the newest file, ordered by
   modification time, then creation/change time, then filename. The durable
   selected file remains the active recovery source until its tail is drained;
   then consume each queued file from byte zero in that exact order. Never jump
   directly to the newest file when an intermediate rotation exists, and never
   baseline a queued file at its current end.

   Rotation recovery is bounded across ticks. In one tick process at most 256
   complete records or 1 MiB (whichever boundary is reached first) from exactly
   one recovery source. Classify the batch in source order, deliver or durably
   retain every actionable wake, and atomically persist a
   `rotation_recovery_batch_receipt` containing source path, start/end offsets,
   last record timestamp, `record_count`, `classified_count`,
   `actionable_count`, `delivered_count`, and `retained_undelivered_count`.
   Require `record_count == classified_count` and
   `actionable_count == delivered_count + retained_undelivered_count` before
   advancing that source's durable offset. A committed partial batch is healthy
   forward progress even though the rotation backlog remains.

   Only after the current recovery source has no unread complete record may the
   monitor persist `rotation_source_drain_receipt`, remove that source from the
   front of the queue, select the next queued path at offset zero, and continue
   on a later tick. The final queued path becomes the ordinary selected stream
   after it catches up. If a source is unreadable, its offset is invalid, batch
   classification is incomplete, or actionable delivery fails, retain that
   source and its last committed offset and set `monitor_integrity_failure=true`.
   Never skip a source or fabricate a classification to clear the backlog.

   Each batch is a real classification pass, not a byte-accounting shortcut.
   Persist one ordered classification entry per complete record with source
   path, start/end offsets, record timestamp, selected class, and any
   fingerprint. If a record contains an unconditional merge-ready/CTO marker,
   `actionable_count=0` is an integrity failure unless a prior handled
   fingerprint or a live terminal/superseded receipt is recorded for that exact
   tuple. Never infer zero wakes from a later file or a marker count alone.

   **Deterministic batch classification (hard, every tick):** batch
   classification is a machine step, not an LLM judgment. For every prepared
   batch run exactly once, in order, and never hand-write or stub the
   decisions file (an empty `{"decisions": []}` placeholder is a defect):

   ```bash
   python3 /Users/rajiv/.codex/monitors/heydonna-pm-chat/rotation_recovery.py \
     prepare --state <STATE_PATH> --manifest <MANIFEST_PATH>
   python3 /Users/rajiv/.codex/monitors/heydonna-pm-chat/rotation_recovery.py \
     classify --state <STATE_PATH> --manifest <MANIFEST_PATH> \
       --decisions <DECISIONS_PATH> \
       --corpus /Users/rajiv/.codex/monitors/heydonna-pm-chat/classifier-examples.json \
       --ledger /private/tmp/heydonna-pm-chat-delivery-guard.json \
       --recovery-ledger /private/tmp/heydonna-pm-chat-recovery-ledger.jsonl \
       [--superseded-pr <PR> ...] [--superseded-run <RUN> ...]
   python3 /Users/rajiv/.codex/monitors/heydonna-pm-chat/rotation_recovery.py \
     commit --state <STATE_PATH> --manifest <MANIFEST_PATH> \
       --decisions <DECISIONS_PATH> \
       --ledger /private/tmp/heydonna-pm-chat-recovery-ledger.jsonl \
       --delivery-ledger /private/tmp/heydonna-pm-chat-delivery-guard.json
   ```

   `classify` must report `record_count == classified_count` and
   `actionable_count == delivered_count + retained_undelivered_count`;
   `commit` fails closed (no offset advance, integrity failure retained)
   otherwise. `commit` reconciles retained decisions whose exact wake
   fingerprint is durably marked `delivered` in the delivery-guard ledger
   (`--delivery-ledger`); reserved-but-undelivered or absent wakes stay
   retained (fail closed). `--superseded-pr`/`--superseded-run` may only be
   supplied with live terminal evidence (merged PR / completed run) for the
   exact tuple; they exist to deduplicate, never to clear backlog.

6. If two files have the same modification time, break the tie by creation/change
   time and then filename. While `rotation_recovery_queue` is non-empty, read
   only its oldest source; ordinary newest-stream polling resumes only after
   every predecessor is durably drained.

7. **Rotation guard (hard, every tick):** validate the durable offset against
   the live size of the current recovery source (or ordinary selected source
   when no recovery is active). An offset greater than that source's live size
   is invalid: retain the previous committed state, write a
   `state_repair_required` receipt, and set `monitor_integrity_failure=true`.
   Do not reset to byte zero on a different file and do not rescan the newest
   file ahead of queued predecessors. A path difference is expected while an
   ordered recovery queue is active; it is a defect only when the queue cannot
   account for every intervening top-level JSONL.

8. **Newest-file discovery (hard):** "newest" is strictly by modification time
   (mtime), then change time (ctime), then filename. Never prefer a file
   because it is larger or lexicographically first. Discovery appends newly
   observed rotations to the ordered recovery queue; it never overrides the
   current recovery source. If the monitor cannot discover and order the
   top-level files, report `monitor_integrity_failure` instead of advancing.

The rendered `/private/tmp/chitta-pm.log` is diagnostic fallback only. Do not
continuously poll numbered-slot logs, but inspect the exact affected
`/private/tmp/slot-N.log` before classifying or waking on any slot, turn,
assignment, release, checkout, process, repro, or proof mismatch.

## Runtime and liveness contract

### PM-report boundary (CTO-side internals stay out of PM-facing reports)

The hourly PM performance report posted to `#heydonna-dev` addresses PM-
controllable execution only. It must NEVER include CTO-side monitor internals:
wake ids / SENTINEL fingerprints, corpus SHA-256 or example counts, monitor
state / tick / last-tick / offset, monitor fixes, classifier examples, adoption
receipts, or wake-delivery details. Those belong only in the local ledger and
the CTO task's own output to Rajiv. A PM report that exposes monitor internals
is a formatting regression and must be corrected before posting.

The monitor is the active bounded heartbeat task, not a shell tail. A
background `tail`, PTY, or unified-exec session may transport bytes, but it
cannot classify an event or call `send_message_to_thread` by itself.

- Each heartbeat performs one bounded source pass and exits without waiting.
- On the next heartbeat or hot reload, continue from the last durably committed
  complete-record offset. Never jump to current EOF merely because the prior
  Codex turn ended.
- Advance the durable offset only after every complete record through that
  offset has been classified and every required wake has been sent.
- On rotation, each bounded batch's classification ledger and count equality
  are its commit barrier. Advancing past a batch, removing a recovery source,
  or scanning a later file before that barrier is satisfied is forbidden.
- Process all distinct fingerprints in source order. One high-priority event
  must not suppress another complete candidate, including a merge-ready handoff
  that precedes a later control-plane incident.
- If the active Codex turn cannot remain alive, wake once with
  `MONITOR_RUNTIME_INACTIVE`; a surviving background shell is not healthy
  monitoring.

## Separation of duties and sole wake target

This monitor is a read-only detector and delivery mechanism. Its sole wake
target is CTO task `01a03236-2e61-71f3-a6a8-3dc24d8c8917`. The receiving task,
not this monitor, owns `WAKE_SOP.md` consumption and every downstream action.

- The monitor may discover, classify, minimally verify, deduplicate, and send
  one structured `SENTINEL_*` envelope. `action_kind`, `required_skill`, and
  `authority` are routing metadata only; they never authorize the monitor to
  run the skill or terminal action.
- The monitor must not make or consume a CTO decision, run a review, rescue,
  hotfix, merge, control-plane repair, PM transition, slot operation, GitHub
  mutation, or Slack post. It must not send recovery instructions to PM.
- Deliver wakes only with the Codex thread-messaging tool to task
  `01a03236-2e61-71f3-a6a8-3dc24d8c8917`. Do not use `codex exec resume`, a
  subprocess, shell injection, or any mechanism that starts or synchronously
  drives the receiving task.
- After a successful message-delivery receipt, record delivery and return from
  the bounded tick. Do not wait for, inspect, narrate, or verify the receiving
  task's SOP execution. A downstream outcome may arrive later as new source
  evidence and is classified normally.
- If thread messaging is unavailable or delivery fails, retain the event as
  undelivered, do not advance its handled watermark, and report monitor
  integrity failure. Never substitute direct processing for failed delivery.

## CTO Slack DM exclusion

Do not poll, classify, or forward Abhijit CTO DMs, channel mentions,
`/tmp/cto-slack-events.json`, or `/tmp/cto-slack-queue.jsonl`. The dedicated
Slack monitor/relay task owns that source and delivery path. Treating it as a
PM-chat source would duplicate wakes.

## Secondary source — slack-bridge transport

Monitor `/private/tmp/slack-bridge.log` only for transport-envelope failures:
dropped, unroutable, rejected, or delivery-failed source events. Classify the
envelope fields, not error words inside the forwarded message body. If the
source already appears in PM JSONL or has a durable reply/handoff, stay silent.

### Tertiary source: PM obligations SQLite (repair queue)

Monitor the PM obligations ledger for accumulating control-plane repair debt.
The DB is the PM-owned ledger at
`/Users/rajiv/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db`
(schema: `obligations`; fall back to the `PM_OPS_DB` env value if the path
rotates). Read it read-only each tick.

Wake when either condition holds:

- a high/critical open obligation whose kind/blocker/required_action is
  control-plane/infra/repair/wedge (or whose required action contains REPAIR or
  RECONCILE) has been open past its `next_review_at` for more than one report
  window, with no terminal receipt (landed main commit + parity + replay); or
- the open count of that same class exceeds a small threshold (e.g. >=3) and
  rows already past their `next_review_at` were re-touched without a terminal
  receipt (touched-but-unresolved accumulation). A re-touch that sets a future
  `next_review_at` with a typed owner/directive in the decision thread is a
  re-surface inside its window, not accumulation; count it only after that
  window passes without a receipt.

This catches the "repair queue accumulates because PM re-prioritizes without
completing the loop" pattern that is invisible to the PM JSONL source. Bind
the wake to the obligation ids and the exact repair class. A single open row
inside its review window, a row with a typed terminal receipt, or a re-surface
that extends `next_review_at` into the future with a typed owner/directive,
is IGNORE.

Reading is read-only; never mutate the obligations DB.

### Rajiv ↔ PM Slack DM scan (direct source)

Each tick, call `conversations.history` for Rajiv's PM DM channel
`D0AMF0XE6TS` with `SLACK_USER_TOKEN`, verified by `auth.test` as Rajiv
`UEQTTB97A`. Read every message newer than the durable
`rajiv_pm_dm_latest_scanned_ts` field in
`/private/tmp/heydonna-pm-chat-reply-watermark.json`. Render Block Kit before
classification and preserve channel, message `ts`, author, bot id, thread ts,
and visible text.

- Messages from PM (`U0ALEAYCAUT`, including PM bot-authored messages) that
  declare `MERGE READY`, `State: merge-ready`, or the normal latest-head
  pre-merge handoff are unconditional `CTO_DECISION` candidates. Bind the wake
  to the live PR and 40-character head, using GitHub to expand a short head.
- Deduplicate the DM and PM-JSONL copies by semantic tuple
  `cto-decision:merge:<pr>:<40-character-head>`, while retaining both source
  coordinates as evidence. Never suppress a current wake merely because the
  same transition appears in two sources.
- Advance the DM watermark only after every message through that timestamp is
  classified and every actionable wake is delivered or durably retained as
  undelivered. A read or delivery failure leaves the watermark unchanged and
  sets `monitor_integrity_failure=true`.
- On first adoption, scan from the configured adoption watermark rather than
  from zero. Classify already-merged or head-superseded historical handoffs as
  consumed/superseded after live verification; wake every still-current open
  handoff.
- Routine heartbeats, healthy status reports, acknowledgements, and messages
  with no CTO/product/merge authority boundary are `IGNORE` after
  classification. Do not forward the whole DM stream.
- PM messages whose only new information is “accepted,” “queued,” “assigned,”
  “will execute,” handoff accepted, owner unchanged, execution in progress, or
  deterministic completion already authorized by the active CTO directive are
  `IGNORE`. Do not wake merely because the message mentions CTO or includes a
  new queue, task, run, or receipt identifier. Wake only when the same message
  also changes exact head, authority, owner, product/runtime/data/security
  risk, causal classification, terminal blocker, failed predicate, or the
  requested CTO decision.

If DM history is unavailable but PM JSONL contains the durable merge-ready
transition or `PM_TRANSITION_MERGE_READY_DM status=sent`, the primary-source
wake remains mandatory. Record the DM source failure separately; it must not
suppress the JSONL wake.

### Slack channel reply scan (degraded-secondary fallback)

The per-tick automation also requires scanning `#heydonna-dev` top-level roots
authored by Rajiv (`UEQTTB97A`) or Abhijit CTO (`U0BNFGX2UAX`) and PM replies
under those roots since the reply watermark. The Abhijit source exclusion above
applies to CTO DMs and mention ingress, not to discovery of CTO-authored audit or
decision roots whose downstream PM replies this monitor owns. Dynamically add
every newly discovered eligible root before scanning its replies; never rely
only on the pre-seeded root registry. If the in-thread Slack history/MCP tool is
unavailable, use the repo-local read-only curl path exactly as in the postback
skill:

```bash
set -a
source /Users/rajiv/Downloads/projects/heydonna-app/.env.local
set +a
curl -sS -X POST https://slack.com/api/conversations.history \
  -H "Authorization: Bearer $SLACK_USER_TOKEN" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode channel=C0ALZJHGE49 --data-urlencode limit=20 |
  python3 /Users/rajiv/Downloads/projects/heydonna-app/.agents/skills/heydonna-slack-postback/scripts/render_slack_blocks.py
```

Use the same transport with `conversations.replies` per tracked `thread_ts`.
Never post or mutate Slack. If both the in-thread tool and the curl path fail,
record `slack_history_available=false` as a degraded secondary source, but do
not report monitor integrity failure while the primary PM JSONL offset is
advancing. Integrity failure / `MONITOR_SOURCE_UNAVAILABLE` applies only when
the primary PM JSONL cannot be discovered or parsed for two consecutive polls.

## Incremental record handling

Read only complete new JSONL records. Candidate-bearing record types are:

- `queue-operation`: enqueue/remove lifecycle and source content;
- `user`: incoming slot, Slack, hook, alert, and tool-result messages;
- `assistant`: PM decisions, claimed transitions, tool launches, and receipts;
- `attachment`: queued commands, task reminders, hook results, and delivery
  metadata;
- `system`: session/cwd evidence only.

Ignore model thinking as proof. A plan, acknowledgement, command launch, queue
removal, or prose claim is not a downstream state transition.

### Required downstream follow-through timers

When a PM record proves a precursor transition succeeded and the same durable
directive or receipt names a required downstream postcondition with an SLA,
create a pending follow-through timer keyed to that exact tuple. Examples
include stale-owner cleanup followed by capacity reconciliation/assignment,
terminal proof followed by canonical consumption, and release followed by
next-owner assignment.

- Re-evaluate the timer on every poll even if the only newer records concern
  unrelated work. Absence of an explicit failure record does not close it.
- Close it only with the named typed downstream receipt and matching live
  state, or an independently verified incompatibility/no-work park allowed by
  the directive.
- While an exact canonical downstream command is live within its stated SLA,
  classify `IGNORE` and wait for its terminal result.
- If the SLA expires, no exact downstream writer is live, and later PM records
  show unrelated work progressing, select `PM_OPS_MISS`. For a numbered slot,
  apply the slot verification gate before waking.
- Track each affected slot independently; progress on one slot cannot suppress
  an expired follow-through timer for another.

### Customer holding-reply and investigation follow-through (hard PM-ops wake)

Treat every customer-origin Slack record in PM JSONL as an independently owned
follow-through tuple keyed by `<channel>:<thread_ts-or-message-ts>`. This
includes a second customer saying "same issue" in a separate top-level Slack
message; do not assume that an adjacent message shares the first customer's
thread or sentinel.

Start a five-minute timer when any of the following appears:

- a customer Slack `queue-operation` / queued-command attachment;
- an `external-comms-investigation` invocation;
- creation of `/tmp/customer-issue-pending-<thread_ts>.json`; or
- PM text that says it will inspect, investigate, reply, or return to that
  customer after other work.

Reply proof is only a real Slack send timestamp or customer draft id bound to
the same source thread. A diagnosis, root-cause note, investigator completion,
sentinel, issue draft, agent launch, or intention to reply is not reply proof.

- If five minutes elapse without reply proof, and no customer-send operation is
  live, classify `PM_OPS_MISS` and wake once with action
  `CUSTOMER_FOLLOWUP_MISS`, authority `PM_CORRECTION`, and required skill
  `heydonna-slack-postback`.
- If PM notices a customer record but advances unrelated PR/control-plane work
  without invoking the customer skill or creating a sentinel, the same
  five-minute wake applies. This is an ingestion/ownership miss, not healthy
  queueing.
- If the investigation is already terminal, require the final customer-safe
  reply immediately; do not ask for a belated holding reply. Keep the wake open
  until final reply proof plus tracking proof (issue, PR, exact duplicate, or
  explicit Rajiv no-issue override) exist.
- Re-evaluate open `/tmp/customer-issue-pending-*.json` sentinels and open
  `customer_followup` / `explore_issue_required` obligations every tick.
  Unrelated PM progress never suppresses an expired customer timer.
- While the tuple is younger than five minutes and its investigation is active,
  classify `IGNORE`. After reply proof plus tracking proof, close/suppress the
  fingerprint.

Use fingerprint
`pm-ops-miss:customer-followup:<channel>:<thread_ts-or-message-ts>` and include
the source customer text, PM investigation/sentinel coordinates, elapsed time,
missing proof, and exact closure evidence. The monitor remains read-only: it
delivers the wake but never replies to the customer or mutates PM obligations.

## Classification

At the start of every tick, before classification, reload `classifier-examples.json`
from disk, compute its SHA-256, and compare with the last recorded corpus SHA in
the durable state. On a change (or missing SHA), adopt the corpus immediately and
record the adoption receipt; never classify from a stale corpus. A file edit is
therefore adopted on the next tick even without a hot-reload message. Use the
nearest examples to classify each candidate into exactly one of:

- `IGNORE`
- `PM_OPS_MISS`
- `CONTROL_PLANE_ISSUE`
- `CTO_DECISION`
- `PR_SLOT_TRANSITION_ISSUE`
- `TRANSPORT_FAILURE`

The examples are precedent, not string-match rules. Exact IDs and heads vary.
Give negative examples equal weight so ordinary PM work remains silent.

### Control-plane ordinal circuit breaker

Treat an explicit current semantic candidate or repair ordinal greater than 3
as an immediate `CTO_DECISION`, even when PM calls the work bounded, in flight,
or informational. This is a hard circuit breaker against reviewer-driven
behavior-patch loops:

- Verify that the ordinal belongs to the current active control-plane incident,
  not a historical tuple, task number, PR number, run attempt, or quoted log.
- Read the current repair-state artifact and exact candidate/reviewer receipts.
- Wake once per `<incident>:<ordinal>` with
  `SENTINEL_CTO_DECISION_WAKE`; a later higher ordinal is a new fingerprint and
  must wake again.
- Include the incident, current ordinal, elapsed time, affected PRs/slots, last
  approved/deployed candidate, latest functional block or failed live
  validation, and the exact stop/stabilize/replay decision required.
- Never classify an ordinal greater than 3 as routine repair progress or
  `IGNORE`. Do not wait for the implementer or reviewer to finish before
  waking.

### Control-plane repair slot-capacity hold (hard wake)

A numbered slot that is OCCUPIED but inactive — `active_turn_state=inactive`,
no active turn id, no running command in the slot log, clean exact-head
checkout, not dirty/unpushed/DND — and is retained solely because a
control-plane repair phase is active (repair checkpoint, rescue-consumption /
authorizer-envelope obligation, or standby reservation with an incident
binding, while the repair itself runs off-slot) classifies
`CONTROL_PLANE_ISSUE`, never `ACTIVE_WAITING_EXTERNAL` and never `IGNORE`.
A FREE+inactive slot reserved for a pending repair/replay is the same class.

- Read the exact numbered-slot log before waking; confirm no active turn or
  process and a clean exact-head checkout.
- Bind the wake to the exact slot/epoch/PR-or-issue and the repair incident id.
- The wake must name the required release primitives: exact-epoch MoP release
  (`release_slot` / drain-slot with `expected_epoch`), `block-pr` to preserve
  the real blocker, `reconcile-capacity --slots <N>`, then reassignment of the
  next eligible exact-head rework. Never let the repair occupy the slot.
- Do not wake for ACTIVE_PRODUCTIVE, dirty, unpushed, DND, or head-diverged
  work, or for legitimate typed product/dependency waits with an owner/wake.

### Merge-ready circuit breaker

Treat any current PM record containing `PM_TRANSITION_OK command=merge-ready`,
`merge-ready complete`, or `merge authorization handed to Codex` as an
immediate candidate. Do not wait for another state change.

**Hard classifier-execution guard (unconditional wake):** all merge-ready
handoffs, all CTO decisions, and all product decisions must trigger a wake. A record containing any of
`PM_TRANSITION_OK command=merge-ready`, `write-promotion-proof` plus
`validate-ready-proof` completion, `PM_TRANSITION_MERGE_READY_DM status=sent`,
an explicit CTO decision/escalation marker, or an "Awaiting ... CTO/Rajiv"
summary must be classified `CTO_DECISION` and woken. A tick that reads such a
record and records `IGNORE`, or advances the offset without a wake, is a
monitor integrity failure (classifier false negative), even when the
surrounding PM summary looks routine or the Slack secondary is unavailable.
The same rule applies to `cto_rescue` / circuit-breaker / decision-required
escalations surfaced on the PM stop hook or in PM JSONL. Product decisions
mean any PM record that asks CTO/Rajiv to choose among product, architecture,
data-model, UX, scope, release-policy, or customer-visible options with a
recommended default or materially different options, including "product
decision request", "decision class: product", "requires CTO adjudication", or
an explicit "awaiting Rajiv/CTO" product question; those must wake
unconditionally as CTO_PRODUCT_DECISION even when the surrounding summary looks
routine.

This circuit breaker must fire even when the Slack reply scan is unavailable:
the handoff is detectable entirely from PM JSONL plus GitHub, so a failed
secondary Slack history call must never suppress the wake. The same applies to
an explicit `cto_rescue` / circuit-breaker escalation surfaced on the PM stop
hook. Record Slack-secondary unavailability as degraded-secondary, classify
from the primary PM JSONL source, and wake normally.

- Bind the claim to the live PR and 40-character head.
- Independently verify current-head CTO review, Phase B, applicable affected
  proof, real pull_request CI and E2E, unresolved review threads, and typed
  merge-ready state.
- If every gate is satisfied, classify `CTO_DECISION` and wake with
  `SENTINEL_CTO_DECISION_WAKE`, fingerprint
  `cto-decision:merge:<pr>:<head>`.
- If PM's merge-ready claim is premature or contradicts the live head/state,
  classify `PR_SLOT_TRANSITION_ISSUE` and wake with the exact missing gate.
- A later unrelated or higher-priority incident cannot suppress this candidate.

### CI+E2E-green promotion follow-through (cross-tick, hard wake)

Rajiv directive (2026-08-12, Slack C0ALZJHGE49 thread 1786544317.974259, ts
1786545323.157199): if a terminal required CI+E2E green observation has not
been consumed by a canonical merge-ready/promotion transition within two
monitor ticks, emit exactly one deduplicated `CONTROL_PLANE_ISSUE` wake to the
CTO task so CTO can remind PM to run readiness/promotion. This is a cross-tick
state machine, not an immediate circuit breaker: tick 1 (first green
observation) never wakes; tick 2 with the tuple still unconsumed wakes exactly
once.

**Detection (event-driven only; never poll GitHub):**
- Treat a current PM/Slack record as a qualifying green observation only when
  it names an integer PR **and** a full 40-character lowercase hex head AND
  shows terminal required CI+E2E green evidence (for example `#7267's wave
  went GREEN (E2E 31602981269 SUCCESS + CI)`).
- Incomplete PR/head or ambiguous evidence fails closed: create no tuple, emit
  no wake, and record the invalid event.
- The full 40-char head must be derived from source records (for example the
  rescue-terminal record naming the head); a truncated 8-char head is not
  sufficient.

**State machine (deterministic, authoritative):**
```bash
python3 /Users/rajiv/.codex/monitors/heydonna-pm-chat/promotion-followup.py \
  --state /private/tmp/heydonna-pm-chat-promotion-followup-state.json \
  tick \
  --tick-id "$tick_id" \
  --tick-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  [--green '<JSON>' ...] \
  [--merge-ready '<JSON>' ...] \
  [--blocker '<JSON>' ...] \
  [--supersede '<JSON>' ...] \
  --delivery-ledger /private/tmp/heydonna-pm-chat-delivery-guard.json
```
- Run this once per tick, after the rotation prepare/classify/commit sequence.
- `--green` events: first observation persists `pending` with `tick_count=1`;
  each later distinct tick advances `tick_count` by exactly one (same-tick
  replay is idempotent).
- `--merge-ready` / `--blocker`: canonical consumption; the tuple is cleared
  and no wake fires. Prose such as "route to merge lane" is NOT consumption.
- `--supersede` (same PR, new full head): old head becomes `superseded`; the
  new head is a new tuple only when it carries its own green evidence.
- When `tick_count >= 2` and the tuple is still pending, the script prints one
  `wakes_due` entry with fingerprint
  `control-plane-issue:promotion-unconsumed-green:<pr>:<head>`. Emit the wake
  through the delivery-guard reservation/send/receipt barrier below and then
  call `mark --status delivered`.
- If the wake fingerprint already has a `delivered` receipt in the delivery
  guard ledger, the script marks the tuple `woke` and never re-emits.

**Delivery barrier (same hard ordering as every wake):**
```bash
delivery_guard.py reserve-wake --tick-id "$tick_id" --fingerprint "$fp" --source "$source"
# WAKE_RESERVED only -> send_message_to_thread to CTO task 019f7922-... ->
# mark-delivered with the thread receipt, then:
promotion-followup.py --state ... mark --tick-id "$tick_id" --fingerprint "$fp" --status delivered
```
- A definite delivery failure: `delivery_guard.py mark-failed`, then
  `promotion-followup.py mark --status failed` (tuple retained pending), and
  set `monitor_integrity_failure=true`. Do not auto-retry.
- One wake per PR+head; a new head is a new tuple. A duplicate tick/replay of
  the same tick id must never emit a second wake.

### Control-plane review/admission verification

Before waking on a reviewer refusal, marker, admission, tuple mismatch, or
missing verdict:

1. Re-read the newest PM JSONL records, repair-state artifacts, marker/admission
   files, and the live reviewer/companion process list immediately before the
   wake.
2. Distinguish an incremental packet tuple from a later explicitly authorized
   cumulative tuple. A refusal bound to the incremental base/patch does not
   prove that a cumulative review invocation is mismatched or terminal.
3. If a newer exact reviewer/companion is actively running on the authorized
   base/candidate/patch and no push/deploy occurred, classify `IGNORE` while it
   is in flight. Do not emit a CTO scope decision merely because an earlier
   wrapper refused a different path or tuple.
4. Wake only on the newest terminal result: admitted marker, functional block,
   verified invocation failure, or a current unresolved ownership boundary.
   Include the live process check and exact terminal artifact in the wake.

### CTO-decision-required marker (deterministic wake)

PM JSONL text that explicitly names a pending CTO authority boundary must wake
the CTO task unconditionally, regardless of Luna-low few-shot similarity or
Slack-secondary availability. Detectable markers include: "PM escalation" or
"escalated" naming CTO, "awaiting the CTO answer" / "CTO answer pending",
"disposition vs" a named gate (ICL, control-plane, validator), "requires CTO
adjudication", "recommended default" with a CTO question, or a held downstream
transition whose blocker is a CTO decision. Bind the escalation to the exact
PR/issue/head and confirm no CTO verdict receipt exists for the same
contradiction before waking once. This covers the case where PM has already
relayed the wait into the CTO thread and the only failure left is wake delivery.

Also treat a terse PM status summary of the form
`Awaiting: <Rajiv's ...> (#<PR-or-issue>[, ...]), <CTO ...> (#<issue>[, ...])`
— or any "Awaiting"/"awaiting" line that names Rajiv's decision and/or a CTO
slot-tuple/disposition with explicit item numbers — as an explicit pending
CTO/Rajiv authority boundary. Wake `CTO_DECISION` once per dedupe key
`cto-decision:pm-awaiting:<issue-or-pr>` when no CTO verdict/consumption
receipt for the same tuple exists. This shape recurs in PM's "In-flight /
Awaiting" progress summaries and was previously invisible because it is not
one of the longer-form markers above.

### Occupied-slot false progress (covered via PM JSONL slot deliveries)

An occupied numbered slot that repeatedly reports continuation ("rework in
progress", LOCAL_CONTINUE, "addressing blockers before committing") to PM at an
unchanged head, with no commit/push and no typed blocker, is false progress and
classifies `PR_SLOT_TRANSITION_ISSUE`, never `IGNORE` and never
`ACTIVE_WAITING_EXTERNAL`. The slot's own delivery records (`message-pm.sh`
queue-operations, attachments, or slack-channel records) for the same
slot/epoch/issue/PR are primary PM JSONL source evidence; the MoP
idle_occupied_continue injections corroborate but are not required.

- A single status update or a slot that actually pushed a new head / named a
  typed blocker is routine and stays silent.
- Same-head continuation claims recurring across multiple nudge intervals
  (>=2) with no terminal artifact is a flow stall. Apply the slot candidate
  verification gate before waking: read the exact `/private/tmp/slot-N.log`
  tail, confirm the LOCAL_CONTINUE/resumed loop with no commit, push, or typed
  blocker, confirm the live MoP row/checkout still bind the same epoch and head
  (dirty-but-unpushed worktree or no meaningful work), and confirm no canonical
  assignment/reconcile command is in flight for that slot.
- Wake once per slot/epoch/PR with fingerprint
  `pr-slot-transition:false-progress:<slot>:<epoch>:<pr_or_issue>`, binding the
  required correction: PM must force terminalization (commit + push new head,
  affected proof, phase-a/CI at the new head) or park/release and reassign the
  next eligible rework within an SLA; a "will continue" hold update is not
  progress.

## Verification boundary

The PM JSONL selects candidates. Only after a non-IGNORE candidate is selected,
perform the smallest read-only verification needed:

- PM obligation/transition database or receipt for PM-ops consumption;
- exact GitHub PR/head/jobs for PR state;
- one affected MoP slot for a claimed assignment/release/rebind;
- the affected checkout branch/head for a claimed park, release, rebind, or
  head update;
- exact repair candidate, marker, admission, deploy/parity, or replay receipts;
- live reviewer/companion process plus newest terminal artifact for any
  control-plane review or admission candidate;
- exact Slack source only for a transport failure.

Do not continuously poll GitHub, MoP, checkouts, processes, or numbered-slot
logs. Candidate-specific verification is mandatory, however: a slot-related
wake may not be emitted until the affected slot log, live MoP row, checkout,
and relevant process/status artifact have been reconciled.

### Slot candidate verification gate

For every candidate involving a numbered slot:

1. Read the current tail of `/private/tmp/slot-N.log` and identify the exact
   task, PR/issue, branch/head, repro/proof session, monitor, and terminal or
   reporting state. Do this before deciding whether an active turn is stale.
2. Re-read the live MoP row, checkout branch/head/upstream, relevant process
   tree/status file, and newest PM JSONL records immediately before waking. A
   report whose tuple already changed is stale and must be suppressed or
   reclassified.
3. Before waking on a free or incompletely populated MoP row, inspect the live
   process tree for a canonical per-slot transition already in progress,
   including `pm-transition.sh assign`, `assign-rework`,
   `ci-repro-dispatch`, `reconcile-capacity`, or a `slot-dispatch-sweep` child
   targeting that slot. While such a command is live and within its 120-second
   transition SLA, classify the sample `IGNORE` and wait for its terminal
   result. A transition may temporarily move through free, owner-only, or
   owner-without-head/turn states; none is independently wake-worthy while the
   exact canonical writer is still active. Re-read MoP, the slot log, checkout,
   process tree, and PM terminal receipt after the command exits. Wake only if
   it fails/rolls back and leaves the slot free with no succeeding transition,
   or exceeds the SLA without a named blocking receipt.
4. Compare a turn start against the earliest authoritative dispatch/transition
   command time, not only the later delivery ACK. A turn that starts after
   dispatch but before ACK, within 15 seconds, is an expected delivery race
   when the slot log shows the exact dispatched task.
5. If the slot log or process/status artifact shows the exact assigned repro,
   proof, implementation, monitor, or terminal-reporting work, preserve it.
   Missing MoP ownership may still be a control-plane issue, but the only safe
   correction is `ADOPT`/`BIND`/`CONSUME` of the existing work. Never prescribe
   interrupt, clear, restart, reassignment, or a duplicate run.
6. A terminal monitor/result may create a short reporting turn. Wait one full
   poll (up to 60 seconds), then re-verify. If the turn self-clears or the slot
   log shows the result was delivered to PM, classify `IGNORE`; do not wake on
   the transient `free+active` sample.
7. Prescribe interrupt or drain only when the slot log and process evidence
   prove the turn is unrelated, wrong-task, stuck beyond its named SLA, or has
   no active/terminal artifact to preserve. `free+active`, turn age, or
   start-before-ACK alone is never sufficient.

For release transitions, enforce this invariant: an exact tuple-bound
pre-mutation reservation must be acquired and validated before checkout,
MoP, label, or ownership mutation. A missing or stale reservation after the
checkout was already parked is a `CONTROL_PLANE_ISSUE`, not a routine retry.

### Control-plane repair capacity gate

A numbered slot may not be made unavailable solely because a control-plane
repair, deploy, or replay primitive is still in flight. This applies even when
MoP truthfully reports the slot as free: a PM directive, reservation note, or
slot acknowledgement that says to stand by and reject otherwise eligible work
is still a capacity hold.

Before classifying such a hold, read the affected slot log, current MoP row,
the reservation/obligation, and the current repair state. If the slot is
free/inactive and its only reason for refusing work is a pending repair or
future replay, classify `CONTROL_PLANE_ISSUE`. Preserve exact-head artifacts
and proof sessions off-slot, keep their reservation durable, dispatch the slot
to current eligible work, and replay on the next safe boundary after the repair
lands. Do not wake when the slot is actively running exact work, reporting a
terminal result within the grace period, or held for a non-control-plane
external dependency that genuinely requires that slot.

## Wake matrix

- `PM_OPS_MISS`, `CONTROL_PLANE_ISSUE`, `PR_SLOT_TRANSITION_ISSUE`, or
  `TRANSPORT_FAILURE` -> `SENTINEL_WAKE`.
- `CTO_DECISION` -> `SENTINEL_CTO_DECISION_WAKE`.
- `IGNORE` -> no message.

Every wake must include a stable fingerprint, exact source JSONL path and
record timestamp/offset, candidate class, verification timestamp, affected
slot-log evidence (path plus current tail marker/offset), independent live
verification, one evidence-supported transition or decision, and closure
evidence. Deduplicate unchanged fingerprints. ACKs from the main task close or
replace fingerprints.

Before classifying or emitting any wake, read
`/Users/rajiv/.codex/monitors/heydonna-pm-chat/WAKE_SOP.md` completely. Every
wake must include its consumer-routing envelope: `sop_path`, `action_kind`,
`required_skill`, and `authority`. The monitor classifies and delivers only;
the receiving CTO task independently verifies and executes the SOP. Reading
the SOP does not authorize the monitor to perform any action from it.

Use these distinctions when assigning `action_kind`:

- A merge authorization is `MERGE_READY` and routes to
  `heydonna-cto-merge`; it is not a status-only CTO decision.
- A product, architecture, privacy, data-model, UX, or release-policy choice is
  `CTO_PRODUCT_DECISION`. Include the original directive, intended capability,
  production failure mode, runtime control point, concrete options, and the
  downstream PM transition. If Rajiv already chose, route it as
  `CTO_DECISION_CONSUMPTION` rather than asking again.
- A bounded exact-head PR rescue is `CTO_RESCUE` and routes to
  `heydonna-cto-rescue-pr` only when the durable handoff assigns that exact
  PR/head to CTO rescue. A direct push is `CTO_DIRECT_RESCUE` and requires
  explicit Rajiv authorization. An urgent ship path is `CTO_HOTFIX` and also
  requires explicit Rajiv authorization.
- An issue-only rescue/rescope circuit breaker without an exact patch is
  `CTO_ISSUE_RESCUE`; wake for the final-verified-patch/split/override decision
  rather than pretending a PR rescue can run.
- A PM reply to a CTO or decisions thread is `PM_REPLY`; the target must render
  and consume it, then execute any newly authorized action from the SOP.

A wake is a candidate report, not authority to mutate, message PM, or process
the action in this monitor. The main task must independently re-read the
affected slot log and live tuple before processing the wake or sending any PM
instruction. Never forward the wake's proposed correction verbatim from stale
evidence.

## Safety

Read-only. Do not post to Slack, message PM, or mutate GitHub, MoP, labels,
slots, tmux, checkouts, files, obligations, or transitions. If the active PM
JSONL cannot be discovered or parsed for two consecutive polling intervals,
wake once with `MONITOR_SOURCE_UNAVAILABLE`; never silently claim healthy
monitoring. Slack-history unavailability alone is not `MONITOR_SOURCE_UNAVAILABLE`;
record it as degraded-secondary and keep processing the primary PM JSONL source.
