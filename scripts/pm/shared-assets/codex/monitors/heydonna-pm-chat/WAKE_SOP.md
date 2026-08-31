# HeyDonna CTO wake-consumption SOP

## Purpose

This SOP governs the CTO task that receives `SENTINEL_*` delegations from the
PM-chat monitor. `MONITOR.md` decides whether to wake. This file decides what
the CTO task does after a wake arrives.

The sole consumer is CTO task `01a03236-2e61-71f3-a6a8-3dc24d8c8917`. The monitor task
`019fd213-c346-7b72-a46d-12d7ff146eef` must only deliver the wake; it must never
execute this SOP, run a listed skill, post to Slack, direct PM, make a decision,
or verify downstream completion. Envelope routing fields are instructions for
the receiving CTO task, not authority for the monitor.

A wake is a candidate report, not workflow proof. The receiving task must
independently verify the exact current tuple before taking action. A verified
wake with existing authority must be processed to its real next boundary; it
must not be reduced to a status summary.

## Shared release-conveyor contract

Apply the normative ownership and motion matrix in
`/Users/rajiv/.codex/skills/_shared/release-conveyor-contract.md`. Slots are
for executable implementation, reproduction, or production-shaped proof
only; CI, capture, review, decisions, and external waits are off-slot. PM
executes routine free-compatible-slot refill and code/proof-ready exact-head
CI/E2E admission. The CTO/release owner consumes workflow terminals through
causal routing or a head-pinned merge. Every nonterminal wake includes
`next_action`, `next_owner`, and `wake`; labels, holds, relays, watching, and
queue receipts are not motion. Code-ready without admission, a CI/capture
terminal, and a free compatible slot with executable drain work are hard
actionable wakes. This monitor/SOP remains read-only and routes action; it
never mutates GitHub, MoP, workflows, or product state.

The receiving CTO wake must execute or durably delegate the next transition in
that same wake; reporting or watching without a bounded owner/action/wake is
not a terminal.

## Routine PM message suppression

Routine PM acknowledgements and progress receipts are ledger-only, not CTO
work. If a PM message merely says or means “accepted,” “queued,” “assigned,”
“will execute,” handoff accepted, owner unchanged, work in progress, healthy
status, or deterministic completion already covered by the active CTO
directive, record it as consumed and stop without verification, delegation, or
Slack reply.

Such a message may proceed only when it also carries a material delta: a new
product/runtime/data/security risk, changed exact head or authority, changed
owner, failed predicate, typed terminal blocker requiring CTO action, or an
explicit CTO-only release/merge decision. Rephrasing, adding receipt IDs, or
restating the existing plan is not a material delta.

## Control-plane simplicity (Rajiv current directive)

PM reports the first literal control-plane blocker and its exact affected tuple
to CTO decisions. PM has no review, approval, deployment, retry, marker, or
admission role. CTO decisions performs the causal diagnosis and classifies the
scope as `PRODUCT`, `CONTROL_PLANE`, `MIXED`, or `UNKNOWN`; only a verified
bounded `CONTROL_PLANE` brief proceeds.

CTO decisions sends one exact implementation brief to Master of Panes task
`01a04154-c9c1-7bc1-8f7b-009a87bc7628`, which is the sole implementation owner
for bounded control-plane work and PM Operator work. That task creates a clean
current-main candidate, focused RED/GREEN and negative proof, rollout and
rollback plan, no-mutation inventory, and exact base/parent/candidate/tree/
stable-patch/path tuple. It returns that candidate directly to CTO decisions
through `$codex-stdio-send-message`; it does not publish, install, activate,
restart, deploy, inform PM, or use a numbered slot.

CTO decisions performs exactly one functionality-first inline review for the
immutable candidate. A `CTO_INLINE_BLOCK` returns one bounded correction to the
same Master of Panes task. A `CTO_INLINE_APPROVE` makes CTO decisions the sole
publisher, installer, activator, service restarter when required, live verifier,
and PM notifier. CTO decisions re-fences current main, publishes non-force,
performs the scoped rollout, verifies readiness and rollback, then sends PM one
post-deployment terminal with the landed tuple, proof, and exact next action.

Progress updates and queue receipts do not replace these boundaries. A main
advance permits one conflict-free replay only when paths and stable patch
identity remain identical; conflict or semantic change is a blocker. Candidate
returns use renderer-free `$codex-stdio-send-message` transport directly to
CTO Decisions.

### Immediate release-edge fallback

When PM reports a control-plane refusal, PM returns the first literal blocker,
exact PR and full head, and current labels immediately. PM must not retry a
marker shape, owner tuple, projected metadata, review receipt, or alternate
control-plane command. CTO decisions fresh-fences exact head, duplicate/run,
and safety state, then either executes or durably delegates one safe manual
edge in that same wake or names the concrete data-loss, security/privacy, or
irreversible harm that makes motion unsafe.

After that fence, CTO has standing authority for one smallest guarded GitHub
label edge needed to emit/resume the canonical release event. This release-only
bypass is governed exclusively by the `Native bypass contract (CTO-only, after
one high-level refusal)` in the shared release-conveyor contract: journal
literal pre/post labels and edge identity, preserve unrelated labels, reconcile
response loss, and stop after one edge. Exact-head CI/E2E and head-pinned merge
guards remain mandatory; no MoP or message-slot mutation is part of this
release-only edge.

## Responsiveness and delegation invariant

The CTO decisions task must remain continuously responsive to Rajiv and PM
decision/status messages. It is a fast verification, decision, routing, and
receipt-consumption surface — never a long-running execution surface.

- Any task that can outlive one bounded wake pass must be handed off immediately
  to one of the existing execution tasks below. This includes implementation,
  repair, deployment, test or review runs, CI investigation, browser/service
  configuration, monitoring, waiting, repeated verification, and follow-up.
- Existing execution tasks are reusable worker capacity, not single-purpose
  silos. Prefer the closest affinity, but they may accept other bounded work
  when the exact tuple, authority, scope, proof, rollback, and terminal return
  contract are explicit:
  - rescues task `019f942b-63ea-7953-b2ea-c4786c850b87` — default for PR
    rescue, hotfix, product implementation, tests, review, and investigation;
  - Master of Panes implementation task
    `01a04154-c9c1-7bc1-8f7b-009a87bc7628` — sole owner for every verified
    bounded `control_plane_only` repair and every PM Operator or `pm-transition`
    implementation, caller migration, installation, cutover, and retirement
    change; its scopes remain distinct but never create duplicate ownership;
  - merge task `01a0324b-68e0-7491-988f-e7e1549f16f7` — default for exact-head
    merge execution and related bounded release verification.
- Before handoff, verify the target does not already own conflicting work. Send
  the exact tuple once, record accepted delivery, and return to message
  handling. Do not wait for progress; completion or a typed blocker returns as
  a later wake.
- **Renderer-free Codex task transport (Rajiv 2026-08-24):** the CTO decisions
  task must never call any `codex_app` tool. This is a standing rule for every
  turn, including wake consumption, whether the task is visible, focused,
  foreground, background, or admitted through app-server stdio. It includes
  task send/read/wait/list/create/fork/handoff operations and navigation,
  title, pin, or archive tools.
  Do not use the renderer-owner IPC sender or `codex-ipc-send-message` either.
- For every Codex-task-to-Codex-task delegation, correction, handoff, or
  terminal return, invoke the installed `$codex-stdio-send-message` skill with
  the exact existing destination task ID, exact complete message, and a stable
  event-specific dedup key. A successful `thread/queue/add` with a durable
  `queuedSubmissionId` is authoritative queue acceptance and establishes the
  single-flight owner whether the helper reports `status=delivered` or the
  synchronous start call returns `resume the thread before starting a queued
  message`. The latter means only “not synchronously started”; it is not failed
  delivery, ownerlessness, or authority to retry/change the dedup key/create a
  second owner. Do not inspect, read, wait for, or poll the recipient task.
  `status=uncertain` without an authoritative queue receipt is terminal
  transport uncertainty: do not retry, change the dedup key, or fall back to
  another transport or owner. `status=unavailable` proves no queue acceptance
  and may use the blocked-owner replacement below once.
- **Blocked-owner replacement (Rajiv 2026-08-23):** if the selected execution
  task is blocked for the exact tuple, do not queue the work behind it or wait
  for that task to recover. Select the closest non-conflicting **existing**
  execution task from this SOP, transfer the complete owner/authority/proof/
  terminal contract once through `$codex-stdio-send-message`, record the
  replacement owner in the processed-wake ledger, and return. Never create,
  fork, or hand off a task through a `codex_app` tool. `Blocked` means a typed
  terminal blocker, `status=unavailable`, or an occupied task that cannot
  accept the release-critical tuple now; a task actively making productive
  progress on that same tuple is not blocked. Preserve single-flight: supersede
  the blocked route and never let the original and replacement execute the
  same tuple concurrently. If no existing task can safely accept the work,
  return one typed `DELEGATION_BLOCKED` or escalate an ownerless P0 to Rajiv;
  do not manufacture a new execution lane.
- Every delegated execution task must return its required terminal or typed
  blocker directly to CTO decisions task
  `01a03236-2e61-71f3-a6a8-3dc24d8c8917` through
  `$codex-stdio-send-message`, with its own stable event-specific dedup key.
  Every delegation prompt must state that return transport explicitly and
  forbid `codex_app` tools. The delegated task must not send that result
  only to Slack or treat a PM acknowledgement as completion. Product/PR work
  may still have an explicit candidate-review boundary. Bounded control-plane
  work has exactly that candidate boundary: MoP returns only a candidate
  packet, CTO Decisions reviews it once, and CTO Decisions owns all
  post-approval publication/rollout/verification and the single PM terminal.
- **Control-plane ownership invariant (Rajiv current directive):** CTO decisions
  owns diagnosis, classification, inline review, publication, rollout, live
  verification, and the single post-deployment PM notification. Master of Panes
  task `01a04154-c9c1-7bc1-8f7b-009a87bc7628` is the sole implementation owner
  for bounded control-plane and PM Operator work. It returns candidate packets
  only; it never publishes, installs, activates, restarts, deploys, or informs
  PM. CTO decisions sends any `CTO_INLINE_BLOCK` rework to that same task and
  performs all post-approval release actions. PM may provide the initial
  blocker/context only; it has zero review, approval, retry, or marker role.
  Never use a numbered product slot or duplicate owner for control-plane work.
- **MoP restart-after-repair invariant (Rajiv 2026-08-27):** every approved
  Master of Panes repair must restart the canonical MoP service after the
  immutable release is activated. Staging the artifact or changing the
  `current` pointer is not live proof. Before declaring rollout complete,
  require timestamped readback of the new child PID/start time, exact release
  pointer and working directory, expected Node runtime/ABI, listener bind, and
  HTTP 200 `/health`. A rollback must atomically restore the prior release,
  restart MoP again, and pass the same bounded readiness checks. The restart
  requirement does not authorize publication, activation, or a live-slot/nudge
  canary; those remain separately gated by the exact candidate verdict.
- **PM Operator ownership invariant (Rajiv 2026-08-27):** every future change
  to the PM Operator package/runtime, direct MoP/GitHub adapters, executable
  caller migrations, `pm-transition.sh` command arms, or final hot-path
  retirement is owned only by task `01a04154-c9c1-7bc1-8f7b-009a87bc7628`,
  created from the MoP project directory. The prior PM Operator task
  `01a03c74-fc97-7a62-bb47-001ac7fb0710` is superseded and must receive no new
  work. The Master of Panes task above is the sole implementation owner for
  these changes and returns only a candidate packet to CTO Decisions.
  Stop the superseded owner before transferring its exact landed/installed
  baseline and next family, preserving single-flight.
- Every handoff prompt must name that return task explicitly and require the
  delegated task to stop at the stated review/terminal boundary. A response to
  PM instead of the CTO decisions task is a routing failure, not an accepted
  receipt; correct the route once without duplicating execution.
- **Never poll GitHub.** This prohibition applies to this task and every
  delegated task. Do not loop or schedule repeated `gh`, API, status, run, PR,
  check, or workflow queries; do not use `gh run watch`; and do not create a
  polling heartbeat/automation. One bounded live GitHub snapshot when a wake
  is consumed or a terminal merge action begins is allowed. All subsequent
  progress arrives through event-driven wake/receipt delivery.

## Current-main red P0 fast lane (Rajiv 2026-08-22)

A genuine required CI or E2E failure on the exact current `main` commit is a
P0. It must never wait behind the rolling PR portfolio, PM ceremony, an
existing numbered slot, or an unrelated rescue/control-plane queue.

- Verify once that the failed required job is a real `push` run on the exact
  current `main` commit. A skipped/cancelled shell, stale commit, notifier
  summary, or PR-branch failure is not a current-main red.
- Route the exact main-red tuple to merge task
  `01a0324b-68e0-7491-988f-e7e1549f16f7` when it can accept the incident
  immediately. If that task is already occupied, route once to existing
  rescues task `019f942b-63ea-7953-b2ea-c4786c850b87`; do not queue the P0
  behind unrelated work or create a task through `codex_app`. If both existing
  tasks are blocked, escalate the exact ownerless P0 to Rajiv.
- The accepting merge or rescues task owns one failed-log consume, causal classification, canonical
  local repro where applicable, the smallest reversible fix, focused
  RED/GREEN, direct current-main publication when safe, and one event-driven
  replacement CI/E2E receipt. Use `heydonna-cto-hotfix` for a bounded
  product/test correction; use `heydonna-cto-direct-control-plane-repair` only
  when the proven defect is strictly control-plane-only and within that
  skill's allowed surface.
- Do not blind-rerun, relax a threshold, force capture, or treat local proof as
  green main. Capture is authorized only when the fix changes prompt/request
  identity and the real E2E log proves a cache miss.
- The CTO decisions task records accepted stdio queue delivery and returns
  immediately. The accepting task reports the exact fix/head and replacement run IDs or
  one typed blocker back to CTO decisions; it does not wait or poll.

## Limbo / ownership-continuity recovery (Rajiv 2026-08-25)

A heartbeat `LIMBO` escalation is always a material wake. It means an open
issue, PR, customer incident, or rescue is tracked but has neither one active
productive owner nor a concrete forcing function to its next terminal. A
diagnosis, plan, review, candidate, proof packet, gate label, parked decision,
or PM obligation is not a terminal product outcome.

On one bounded live snapshot, the CTO decisions task must:

1. Reverify the exact issue/PR/head, customer severity, last substantive
   artifact, current labels/obligations, and every claimed live owner. Age alone
   does not prove limbo. An active productive owner with an exact next boundary
   and event-driven wake clears the flag.
2. For a live-user, data-loss, or P0 incident, restore ownership in the same
   wake. Choose the smallest safe implementation path, bind one existing
   slot/rescue owner through implementation PR, focused proof, genuine CI/E2E,
   and merge terminal, and hand it off once. Escalate any workflow/control-plane
   obstruction immediately and use the authorized degraded or manual path;
   repair the machinery separately.
3. For other P1 limbo, bind one owner and one exact next boundary in the same
   wake when the safe reversible action is already known. If product behavior,
   data policy, or irreversible scope is genuinely undecided, escalate the
   exact decision to Rajiv with one recommendation; the escalation owner and
   decision wake become the forcing function.
4. Preserve single-flight. If a claimed owner is stale or blocked, supersede it
   once under the blocked-owner replacement rule. Do not create a second
   implementation/capture/review lane for an already productive exact tuple.
5. The three-hour heartbeat retains phone escalation for a genuinely stuck P0
   that needs Rajiv's intervention. Place exactly one call for the current
   escalation state when the bounded snapshot proves any of: mitigation failed
   or customer data-loss risk resumed; the sole recovery owner disappeared; a
   promised PASS/BLOCK or other concrete continuation is at least 15 minutes
   overdue with no active work; or an exact product/data/release decision can
   only be made by Rajiv. Post the compact decision packet in Slack and place
   the call in the same heartbeat wake. A mitigated P0 with an active owner and
   an event-driven next boundary is not call-due. A Rajiv reply or terminal call
   receipt satisfies that exact escalation state; do not call again unless one
   of the material conditions above changes.

The wake closes only with one of: verified terminal state; accepted delivery to
one owner whose contract explicitly continues through merge; or a typed Rajiv
decision blocker with an owner and event-driven wake. `Tracked`, `pending`,
`candidate-ready`, `review-passed`, and `pm-blocked:pm-gate` are not closure
conditions for live-user/data-loss/P0 work.

## Release conveyor / terminal-to-terminal ownership (Rajiv 2026-08-25)

Capture, CI/E2E, and merge are one continuous release state machine, not three
independent alerts. The first accepting release owner remains accountable until
the PR is merged or a concrete product/runtime/data/security blocker is routed
to one implementation owner. A capture launch, capture success, CI launch,
dual-green pair, label transition, queued task message, or `merge-ready` receipt
is never a terminal owner handoff by itself.

For every open non-draft product PR, consume terminal events automatically:

1. **Genuine exact-head CI/E2E cache miss -> app-main fence -> capture.** When a
   required real `pull_request` CI/E2E failure proves a strict fixture/cache miss
   on exact head `H`, first classify the main advance since the branch's admitted
   base with the shared change-scope classifier, then inspect only the bounded
   intervening diff for material relevance to the PR. Refresh when that diff can
   affect the PR's runtime control point, shared runtime/build dependency,
   schema/interface/migration, or the exact request/test surface that proved the
   cache miss. An unrelated app or app-test change does not justify a refresh.
   When relevant movement exists, merge current main non-force, run the smallest
   conflict proof, and capture the resulting descendant `H2`; do not capture `H`
   first and then change request identity with a relevant merge. If the advance
   is strictly `control_plane_only=true, product_changed=false`, do not refresh the branch:
   perform one duplicate-fenced canonical capture on `H`. Do not wait for a new
   PM/CTO approval, blind-rerun the failed workflow, or release ownership at
   capture dispatch. A failure without a proven cache miss follows ordinary
   causal rework instead.
2. **Capture SUCCESS -> CI/E2E.** A terminal canonical capture `SUCCESS` on `H`
   immediately authorizes the next release boundary. Re-run the same app-main
   fence once. Merge current main only when the bounded intervening diff is
   materially relevant under that fence; unrelated app/app-test movement and
   control-plane-only movement do not stale the capture, `ci-head`, or exact-head
   proof. After any required app-main integration and
   focused proof, trigger exactly one genuine `pull_request` CI + `E2E Smoke
   Tests` pair on the final head. Do not require capture-workflow manifest,
   promotion, observed-key, body-SHA, or internal strict-replay receipts as an
   admission gate. The subsequent genuine E2E is the authoritative strict-
   replay proof. A main-only merge does not require recapture; only an exact-
   descendant E2E cache miss starts another capture cycle.
3. **Genuine exact-head CI + E2E SUCCESS -> merge.** When both required real
   `pull_request` workflows succeed on the exact current PR head, the same
   release owner immediately runs the review-thread/release-gate readback and
   `scripts/ci/pre-merge-current-head-ci-guard.sh`, then performs the head-
   pinned merge. Do not stop at a `merge-ready` label, PM relay, accepted merge
   handoff, or another approval request. Only a concrete failing release gate
   may stop the merge, and it must name one next owner and event-driven wake.

**Main-refresh scope fence (Rajiv 2026-08-25, clarified 2026-08-27).** Current
main is an admission snapshot, not a continuously moving invalidation target.
After a genuine exact-head pair starts, later main movement requires a refresh
only when the bounded intervening diff is materially relevant to the PR's
runtime behavior, changed control point, shared runtime/build dependency,
schema/interface/migration, or exact CI/E2E/capture proof surface. The shared
classifier is the first scope filter; `app` or `app-test` classification alone
does not establish relevance. An unrelated feature, test, or harness change
does not invalidate an admitted capture, `ci-head`, CI/E2E pair, or merge
candidate. Pure control-plane movement is likewise non-invalidating. Determine
relevance from the existing bounded diff and dependency evidence—do not add a
new receipt, review, or test cycle merely to prove irrelevance. If material
coupling cannot be determined safely from that evidence, fail closed and
refresh. On dual-green, merge immediately when the exact head remains
mergeable/conflict-free and the canonical guard passes; do not create a convoy
by refreshing and retesting for unrelated changes.

Every nonterminal terminal receipt must atomically retain a durable continuation
record keyed by PR + head + run with `next_action`, `owner`, and `wake_condition`.
The three-hour heartbeat reports any conveyor record with no active execution
or event wake as top-priority `LIMBO`; it does not perform the mutation.

Durable transport queue acceptance establishes the conveyor owner even when the
synchronous start request returns `resume the thread before starting a queued
message`. Record the `queuedSubmissionId`, keep it single-flight, and describe
it truthfully as “queued for task consumption,” not “synchronously started.” Do
not retry or create another owner. If a release-critical tuple has no execution
receipt, do not wait for the transport/control-plane repair: immediately
authoritatively supersede the queued action once and use the safe degraded
release path below through the closest non-conflicting existing execution task.
The repair remains independently owned and never becomes a release dependency.

### Stuck CI/E2E degraded recovery (Rajiv 2026-08-25)

A genuine exact-head CI/E2E run is `CI_E2E_RUNNER_STUCK` when one bounded live
snapshot proves all of the following: the run/job is still `queued` for at
least 15 minutes; no newer exact-head replacement exists; no runner has bound
the job; and concrete runner/autoscaler evidence shows repeated provisioning,
self-shutdown, registration failure, or another non-capacity control-plane loop.
Ordinary queueing, an `in_progress` job with a bound runner, or capacity pressure
without a failed binding cycle is not this class.

Do not park the PR behind a repair queue. The release owner must use this bounded
escape hatch in the same wake:

1. Reverify PR/head/branch, the stuck run/job, no newer replacement, and any
   still-valid green workflow leg. Cancel only the proven stuck queued run to
   stop resource churn; do not cancel a productive job.
2. Fetch exact current main and apply the main-refresh scope fence above. Merge
   main non-force only when the bounded intervening diff is materially relevant;
   unrelated app/app-test and control-plane-only movement do not justify a
   descendant or new evidence cycle. When a refresh is required, preserve both
   contracts, run the smallest conflict-
   sensitive proof, and push a descendant.
3. On the descendant, replace the stale `ci-head` and emit exactly one genuine
   label-gated `pull_request` CI/E2E pair through the CTO-wave boundary. When
   the branch already contains current main and no head change is justified,
   use the canonical one-time fresh-run recovery after cancellation; never raw-
   dispatch the workflow or toggle labels without its dedup receipt.
4. Preserve historical green/red legs as evidence, but release proof comes only
   from the new exact-head required jobs. Return cancellation, merge/proof/head,
   labels, and new run IDs; then stop for event-driven terminals without polling.

The merge task owns this recovery. If it cannot accept the tuple immediately,
use the rescues task under blocked-owner replacement. The direct-control-plane
task repairs the runner/transport defect separately and may never block the PR.
Never run both the old queued job and a replacement wave, never create two
release owners, and never bypass a substantive product/review/capture gate.

## Monitor callback boundary

Wake consumption is acknowledged internally, not by sending a routine callback
to the originating monitor task:

- Always retain the processed-wake ledger entry, fingerprint, deduplication,
  delivery receipt, watermark, and integrity state required by this SOP and
  `MONITOR.md`.
- A healthy `WAKE_CONSUMED` acknowledgement is ledger-only. Do not forward it
  with `$codex-stdio-send-message` (or any equivalent task message) to the
  originating monitor thread.
- A task message back to the monitor is allowed only for an actionable outcome,
  a delivery failure, a monitor-integrity failure, or an explicit decision or
  blocker receipt that requires the monitor owner to act. Suppress routine
  success, duplicate-suppressed, unchanged, and bookkeeping-only receipts.
- Callback suppression never changes delivery-guard state or source
  watermarks. Failure and integrity paths remain fail-closed and visible.

## Required wake envelope

Every wake must carry:

```text
sop_path=/Users/rajiv/.codex/monitors/heydonna-pm-chat/WAKE_SOP.md
fingerprint=
class=
action_kind=
required_skill=
authority=EXECUTE_NOW|RAJIV_DECISION|PM_CORRECTION|VERIFY_ONLY
exact_tuple=
source_evidence=
live_verification=
terminal_action=
closure_condition=
```

`required_skill=none` is valid only when no installed skill fits. The monitor
must not invent authority. It reports the authority already present in the
source directive, durable handoff, or ownership contract.

## Reply threading contract

Every Slack reply produced from a wake must preserve the source thread:

- Send every CTO Slack reply through the existing guarded postback script:
  `/Users/rajiv/Downloads/projects/heydonna-app/.agents/skills/heydonna-slack-postback/scripts/cto_slack_rest.py`.
  Follow the complete `heydonna-slack-postback` skill contract. The script must
  authenticate with `SLACK_CTO_BOT_TOKEN`, prove `user_id=U0BNFGX2UAX`, post
  the complete reply in top-level `text`, and read back the exact stored author,
  text, channel, and thread. Do not substitute raw curl, Slack MCP, Block Kit,
  `slack-send.sh`, a bridge sender, Rajiv's user token, or Dhurva's bot token.
- Compose outgoing `text` in Slack-native mrkdwn, not GitHub Markdown. Use
  `*bold*`, `_italic_`, backticks, and Slack link syntax such as
  `<https://example.com|label>`. Do not send GitHub-only emphasis such as
  `**bold**` or `__bold__`; the guarded sender intentionally preserves input
  verbatim and does not translate formatting. Before every write, inspect the
  final outgoing text and replace any double-asterisk/double-underscore
  emphasis outside code spans with Slack mrkdwn. A formatting defect is a
  pre-send composition failure, not authority to add Block Kit or another
  sender.

- Ordinary channel `message.channel`: reply using the source `thread_ts` when
  present; for a top-level CI/capture parent, reply in the thread of that
  source message (`thread_ts=<source ts>`). Never answer an admitted CI parent
  with another top-level channel post.
- Channel `app_mention`: reply in the exact source thread using
  `thread_ts=<source ts>` when the mention has a `thread_ts`, otherwise reply in
  the thread of the mention itself (`thread_ts=<mention ts>`). Never create a
  new top-level channel post.
- DM `message.im`: reply in the same DM thread using the source `thread_ts`
  when present; otherwise reply to the source message with
  `thread_ts=<source ts>` so the DM stays a thread.
- A top-level post is only allowed when the user explicitly asks for a new
  top-level message or a new thread. Exception: CTO escalation to Rajiv may
  create a new DM in `D0BPG55FG72` when no source thread exists.
- Reply delivery is downstream of wake processing. A Slack reply, bot postback,
  or post-wake Slack write must never interrupt the current wake: finish the
  active wake's verified action and terminal receipt first, then send the reply.
  New Slack events observed while the current wake is still being processed are
  queued as the next wake and must not preempt it.

## CTO escalation to Rajiv

CTO/Abhijit escalates product decisions and blockers to Rajiv by default:

- Use `SLACK_CTO_BOT_TOKEN` and Rajiv's DM channel `D0BPG55FG72`.
- Mention Rajiv as `<@UEQTTB97A>`.
- Preserve the source `thread_ts` when the escalation replies to an existing
  Slack message; otherwise send a new DM to Rajiv.
- Include the decision packet from the CTO product-decision contract below.
- Never convert a genuine product decision or release blocker into a routine
  PM operational guess. If CTO cannot safely decide it, Rajiv gets it.

## Standing CTO decision authority (Rajiv 2026-08-07)

Rajiv granted standing authority to decide the recurring classes below
automatically (DM `1786120874` + subsequent direction "yes and yes"): only
genuinely new product/architecture/data-model/release-policy choices reach
Rajiv. Do not nudge Rajiv for these — decide, direct PM with the exact
transition, and record the receipt in the wake ledger:

1. **CI-fire seals for verified rescue heads** — when a rescue head is
   code-complete with verified evidence (planner/QA/rescue receipts, tests
   green) and the ONLY blocker is the control-plane launch-event recording
   gap (`mop_fable_agent_event_missing` family), choose **recorded override on
   the sealed packet**, scoped strictly to CI-fire admission (never merge
   authority). Bind the override to PR + full 40-char head + reason +
   evidence, then direct PM to fire label-gated real CI+E2E (test + E2E
   Smoke, not the exemption shell). Always pair with the bounded REPAIR of the
   missing-event recording so future rescues do not need overrides.
2. **Stale repair closure** — when a control-plane/repair obligation's fix has
   landed on main with parity (and replay where applicable), close/resolve it
   with the landed commit + receipt. Do not reopen or re-review landed fixes
   for ceremony.
3. **`split_and_reimplement` default** — for issue-level circuit-breaker rows
   (broad scope, repeated same-class blockers, stale contract churn) with no
   verified narrow patch, confirm `split_and_reimplement` with the typed
   rescope contract (`approval_authority=cto`, `rajiv_directed=false`,
   `follow_up_issues` non-empty) and promote the successor when prerequisites
   are closed.
4. **Bounded control-plane REPAIR** — PM reports the first literal blocker and
   exact tuple. CTO decisions diagnoses and verifies bounded `CONTROL_PLANE`
   scope, then sends one exact implementation brief to Master of Panes task
   `01a04154-c9c1-7bc1-8f7b-009a87bc7628`. That task returns one immutable
   candidate packet for exactly one CTO inline review and does not publish or
   deploy. A block returns rework to the same task. On approval, CTO decisions
   alone publishes, rolls out, restarts when required, verifies, and informs PM
   once. Do not hold a numbered slot.
5. **CTO rescue and hotfix execution** — forward the exact verified tuple once
   to dedicated rescues task `019f942b-63ea-7953-b2ea-c4786c850b87`.
   This includes `CTO_RESCUE`, `CTO_DIRECT_RESCUE`, `CTO_HOTFIX`, their
   monitors, and follow-up ownership. The CTO decisions task does not
   implement, monitor, wait, or poll; accepted handoff is terminal for this
   task, and the rescue task returns a later terminal receipt or typed blocker.
   Exception: a verified exact-current-main required CI/E2E red follows the P0
   fast lane above and routes to the existing rescues task when merge is
   occupied.
6. **Merge of merge-ready PRs** — forward the exact PR/head and complete source
   tuple once to dedicated merge task `01a0324b-68e0-7491-988f-e7e1549f16f7`.
   That task independently runs the guard and head-pinned merge or returns the
   exact blocker. The CTO decisions task never waits, polls, or runs a heartbeat.
   After accepted handoff, return. Do not run or post a portfolio sweep in this
   task; the merge task owns the sole sweep after a verified successful merge.
7. **Low-risk disposition sweep** — when woken, clear the open CTO-owned rows
   (stale `cto_rescue`/`rescope_product_escalation`/`control_plane_defect`
   with landed fixes) in the same pass instead of leaving them parked.

Only these escalate to Rajiv: a genuinely new product/architecture/data-model,
security/privacy, destructive/irreversible, or release-policy choice; an
unresolved P0 ownerless obligation; or a contradiction where the evidence does
not support any standing default. Candidate count and routine control-plane
stabilization are CTO/PM decisions, not Rajiv escalations.

## Three-hour heartbeat backstop (Rajiv 2026-08-15)

The three-hour PM heartbeat is an execution backstop, not an informational
status report. Its primary operational objective is to keep numbered slots
productive and keep exact-head PR transitions moving.

### Save-suppression production-debug heartbeat

While the tracked save-suppression hotfix/debug incident is open, a
`SAVE_SUPPRESSION_PROD_DEBUG` heartbeat packet is material evidence, not
routine known-family chatter. Deduplicate by the exact UTC window together
with its `receipt_sha256` and query/report digest; consume each packet at most
once. The packet's repository, window, query identity, and source digest are
the authority, and `active_debug=true` keeps the same owner and next wake even
when every count is zero.

Consume the smallest authoritative packet once and compare its typed counts,
unresolved actions, and privacy-safe lineage to the last receipt. Deliver each
new or changed causal field exactly once to the existing sole product hotfix
owner. Never create a second investigator, act on customer files, or rerun
unrelated work. A nonzero unresolved row, a new/changed causal field, or
`isPartial`/row-query unavailable state is material; partial/unavailable is an
evidence blocker and never a clean verdict.

The investigation is persistent and event-driven until the causal transition
is proven. Every nonzero packet must be evaluated against the latest deployed
telemetry version and the prior packet; aggregate counts alone are not a root
cause. The same hotfix owner must trace the complete privacy-safe evidence path:

1. browser/client `releaseVersion` or equivalent build identity;
2. the exact logger emission payload at the suppression control point;
3. transport and Axiom field indexing;
4. the Sakshi query projection/report; and
5. the joined transition from suppression through
   `save_suppression_drain_handoff`, durable sweep/attempt, and
   `REMOTE_ADMITTED` (or the first missing terminal).

Join only on masked transcript identity plus privacy-safe candidate lineage
(`candidateId`, `localSequence`, version/checksum, owner/lease/attempt IDs).
Never read or log transcript text, raw customer identifiers, credentials,
emergency payload bytes, or unmasked storage keys. Classify `inflight`,
`sw_lease`, `sweep_busy`, and `save_escape_unsynced` separately only when the
joined state transition proves a distinct control point; reason counts by
themselves do not prove separate defects.

The first eventful post-deploy window with no new causal fields keeps the same
owner and explicitly records the missing observation boundary. A **second
consecutive nonzero window** with `new_or_changed_causal_fields=[]` is
`SAVE_SUPPRESSION_TELEMETRY_EMIT_GAP`, not routine repetition and not
ledger-only. In that same wake, send the exact window and prior receipt once to
the existing hotfix owner. That owner must locate the first missing boundary
among client build identity, logger payload, transport/indexing, and query
projection, then deploy the smallest reversible main correction or return one
typed external blocker. Do not wait for a third window before acting.

After that escalation, a byte-identical packet may be ledger-only only while
the same owner is demonstrably active on that exact emission-gap tuple and has
an event-driven terminal return. Every later owner terminal is actionable: a
landed observability correction keeps the incident open for the next natural
packet; a typed blocker must be resolved or escalated to Rajiv with the exact
missing authority/access; owner disappearance triggers the live-user/main-
hotfix ownership-continuity rule. Never reset the investigation merely because
three hours elapsed.

Any new `save_escape_unsynced`, `explicitSaveError`, committed-but-not-durably-
admitted signature, new affected lineage, or rise after a deployed correction
is immediately material even in the first window. Route the delta to the same
owner for causal classification; do not infer data loss from the event alone,
and do not suppress it as known-family noise. Preserve the audited durability
disposition separately: later durable successors can clear confirmed-loss risk
for that bounded cohort but do not close the state-machine investigation.

If the owner is absent or returns a typed blocker, route the exact incident
through the existing live-user/main-hotfix fast lane without changing the
owner. A clean three-hour window never auto-resolves this incident. Closure
requires a landed/deployed main hotfix plus production-shaped Axiom evidence
from a client whose build identity is bound, exercising the intended
suppression -> handoff -> sweep/attempt -> admission transition. The closing
evidence must classify each observed suppression family as benign bounded
contention or a fixed defect and show the durable terminal. A precise external
blocker is terminal only for that owner turn; it does not close the incident.
Keep the incident open, escalate the missing access/authority, and resume the
same causal investigation when it clears. Count reduction, a zero-row window,
server deploy time without browser build identity, or absence of customer
complaints is not closure. Until this boundary is met, every heartbeat retains
the same owner and next wake and continues the investigation.

### Open-PR activity invariant (Rajiv 2026-08-30)

Every three-hour heartbeat must enumerate every open HeyDonna PR at its exact
current head and prove that it is in at least one live execution lane:

1. a canonical capture is genuinely executing for that exact head;
2. genuine required `pull_request` CI or E2E is genuinely executing for that
   exact head; or
3. a numbered slot is actively running the exact PR's bounded reproduction,
   integration, or production-shaped proof.

Labels, an assigned release owner, a dependency/hold reason, a queued shell
with no runner, skipped/dummy workflows, historical runs, an idle slot claim,
or prose saying work is pending do not satisfy this invariant. A queued
capture/CI/E2E job with zero steps and no runner binding for at least 15
minutes is an `OPEN_PR_ACTIVITY_GAP` and, where applicable,
`CI_E2E_RUNNER_STUCK`; report its age and exact run/job instead of calling it
active.

The heartbeat report is exceptions-first. For every PR outside all three
lanes, emit exactly one `OPEN_PR_ACTIVITY_GAP` row containing PR, branch,
40-character head, current owner, last meaningful exact-head run, why each
lane is absent, and the smallest executable next boundary. A completely clean
audit reports `open_pr_activity_gaps=0` plus counts by active lane. The
heartbeat must not manufacture workflow, capture, slot, source, label, or
merge effects merely to make the report green; the CTO wake consumes each gap
and acts through the matrix below.

### Numbered-slot assignment (Rajiv 2026-08-29)

Assignment is deliberately simple. Before assigning, check the selected
slot's current session age. If it is older than six hours, clear that session
once and prove a fresh context boundary; if the clear fails, stop the
assignment. Otherwise PM performs exactly three operations:

1. Assign the issue in MoP with one curl:
   `POST /slots/<slot>/assign` with header
   `x-heydonna-assignment-authority: pm-transition-v1` and JSON
   `{"issue": <issue>, "task": "<short task>"}`.
2. Edit the GitHub issue labels to add `slot:<slot>` and
   `status:in-progress`, removing `status:todo` / `status:in-review` when
   present.
3. Deliver the literal work packet once with `message-slot`.

The only assignment conflict is the same issue already assigned to another
slot. MoP enforces that atomically. Do not require or compare epochs, owner
tuples, branch/head/PR bindings, active-turn telemetry, release receipts,
claim packets, pickup acknowledgements, or projection read-backs for ordinary
assignment. Apart from the required over-six-hour session clear, do not run
release/clear/rebind before assigning; assigning the selected slot replaces
its prior projection. If MoP reports
`target_already_assigned`, leave the duplicate issue alone and choose different
work. Otherwise continue through labels and message delivery without ceremony.

When this ordinary path is used as a degraded/manual bypass after one typed
refusal, it is governed exclusively by the shared native bypass contract:
direct MoP `curl` with the endpoint's exact slot/identity/epoch, canonical
authority header and payload, recorded HTTP response/readback, then one
complete-set GitHub label reconciliation, then exactly one literal
`message-slot` packet. A primitive failure stops subsequent effects.

### Stuck numbered-slot -> rescue-lane transfer (Rajiv 2026-08-25)

A numbered slot is stuck when its product work cannot advance because the slot's
model, launcher, tool transport, host, or control-plane runtime is failing. MoP
`active` is not authoritative when the live pane shows only terminal startup or
transport errors. A stuck slot may not retain a product PR while eligible work
waits.

- Preserve the exact checkout, dirty/unpushed work, packet identity, and causal
  proof. Transfer the PR/fix continuation once to the existing rescues task;
  reproduction already completed in the slot must not be repeated.
- After durable rescue acceptance, PM releases the stuck slot at the exact epoch
  through the Python/degraded ownership path, clears only its slot projections,
  and immediately refills it with the highest-priority eligible packet. Do not
  release before the rescue owner has the complete state handoff.
- The old slot stops work after handoff. Never run slot and rescue implementations
  concurrently, redeliver the packet, or discard dirty state. A true repro that
  still requires slot-local Modal/Convex execution stays in a healthy slot; the
  implementation/fix moves to rescues once the causal evidence is sealed.
- A terminal startup/model error must mark the turn inactive/blocked so heartbeat
  and capacity reconciliation can see the limbo; it must never remain falsely
  `active` because a retry loop is still emitting the same terminal error.

### CI is off-slot; E2E requires a numbered slot (Rajiv 2026-08-26)

CI and E2E have different execution boundaries. An exact-head CI failure and
its bounded local reproduction, causal classification, test/harness repair,
focused proof, and publication may be handed directly to the existing rescues
task. E2E failed-job log and retained-artifact consumption, static analysis,
and causal classification may also happen off-slot. A numbered slot is required
when work must actually execute E2E: production-shaped reproduction, live
runtime interaction, strict replay, or final focused E2E proof.

- Route exact-head CI failure work directly to rescues. Preserve any valid
  green workflow leg and do not blind-rerun.
- Route exact-head E2E logs and retained artifacts to rescues for one bounded
  off-slot analysis. If the evidence proves a bounded code/test control point,
  rescues may implement the non-E2E correction and local proof. Any actual E2E
  reproduction, replay, or final production-shaped proof must run in a numbered
  slot.
- For a mixed CI/E2E red, split the boundaries without creating competing
  owners: rescues may repair CI off-slot while the numbered slot owns E2E.
- PR #7468 follows this split explicitly: rescues owns its
  `requestAnimationFrame` CI repair plus one E2E failed-log/artifact analysis.
  If that analysis requires E2E reproduction or after it produces a correction,
  the E2E execution/final proof remains in the numbered-slot queue after #7509.
- Capture workflow/profile, fixture, strict-replay capture, and
  promotion/readability changes remain numbered-slot app-CI work through
  focused proof.

### Maximum safe slot utilization under WIP freeze (Rajiv 2026-08-26)

The numbered-slot capacity objective is maximum safe utilization. A WIP freeze
is an admission-ordering rule: exhaust safe work on existing open PRs before
admitting new backlog. It does not authorize an idle healthy slot.

- Before accepting `no eligible packet`, audit every open PR's exact live head
  and next executable boundary. Do not treat assignment-gate categories,
  `pm-state:*`, `pm-blocked:*`, or stale ownership labels as authoritative
  eligibility by themselves.
- A published correction head that is OPEN and mergeable remains eligible for
  numbered-slot production-shaped reproduction, strict replay, live-runtime
  interaction, or final E2E proof even when stale `blocked-rework` or
  `pm-blocked:*` labels remain. Reconcile those labels separately; do not let
  them hide executable work from refill.
- Count a PR as owner-held only when a live productive owner owns its exact next
  boundary. An off-slot rescue/release owner does not own a required numbered-
  slot E2E execution unless that slot tuple is also actively assigned.
- Prefer an existing durable packet. If none is locatable but an exact-head
  terminal and named spec define the bounded slot boundary, escalate once for
  explicit packet-construction authority instead of holding the slot free.
- When an existing open PR has a safe numbered-slot-required boundary, assign
  the highest-priority such boundary before new work. If a complete exact-head
  audit proves none is claimable, immediately assign the highest-priority
  validated Ready-Pool item to the free slot, one item per free slot. A free
  hold is valid only for a concrete safety, privacy, data-loss, runtime, or
  unavailable-worker refusal—not merely because the WIP freeze is on.

For every heartbeat wake, before acknowledging it:

1. Check the PM session and all S1-S6 session ages. When the PM session is
   older than six hours, ensure one open `pm-self-clear` obligation exists and
   explicitly remind PM to execute it at the next safe Stop boundary. For each
   slot older than six hours, explicitly remind PM that the session must be
   cleared before that slot's next assignment. Do not interrupt productive
   active work for age alone, and do not create duplicate clear obligations.
2. Extract every current control-plane defect, exact stuck transition, idle or
   indeterminate slot, failed drain/release/assignment, stale proof/review
   binding, and overdue high/critical repair obligation. Reconcile each item
   against live MoP, the numbered-slot log, GitHub state, and the repair ledger.
3. A defect is not "processed" merely because the heartbeat named it, PM
   recorded an obligation, or CTO repeated it in Slack. In the same wake, each
   defect must reach exactly one terminal routing result:
   - an already-active exact incident owner with candidate/receipt evidence;
   - a newly accepted bounded repair handoff to Master of Panes task
     `01a04154-c9c1-7bc1-8f7b-009a87bc7628`; or
   - one typed refusal naming the missing authority or non-control-plane scope.
4. Dispatch bounded repairs immediately. Do not wait for Rajiv to repeat the
   instruction, for the next heartbeat, or for all related defects to be
   understood. Independent producer/validator boundaries are separate repair
   tuples and must not be bundled into one broad patch. Prioritize in this
   order: free wedged capacity, restore CI/transition motion for sealed heads,
   restore fresh assignment, then repair non-blocking proof/receipt machinery.
5. For every capacity-blocking defect, record the post-repair replay now:
   re-read the live slot epoch and active-turn state, drain/release exactly
   once at that epoch, reconcile capacity once, and assign the highest-priority
   eligible packet to the first healthy free slot. Never preempt productive
   work and never reserve a numbered slot for the repair itself.
6. For every PR-transition defect, bind the repair and replay to PR + full head
   + source proof/run/receipt. After installed parity, replay the canonical
   transition exactly once and require real run IDs or one typed refusal. Never
   raw-edit labels, synthesize evidence, or dispatch a workflow directly.
7. The heartbeat wake closes only after the repair handoffs are accepted and
   the processed-wake ledger names each incident, owner, priority, replay, and
   closure condition. Repair implementation completes asynchronously through
   later receipts; the CTO decisions task does not poll or wait.

## Consumption sequence

For every wake, in source order:

1. Read this SOP and deduplicate the fingerprint against the durable processed
   wake ledger. A changed PR head, slot epoch, run attempt, materially changed
   repair, or product decision contract is a new tuple. A routine
   control-plane replay or candidate count is not a new decision ceremony.
2. Re-read the smallest authoritative live evidence once. For slot wakes, read the
   exact numbered-slot log first, then MoP, checkout, process/status, GitHub,
   and the newest PM records. For PR wakes, take one bounded GitHub snapshot;
   never poll for a state change.
3. Suppress or replace stale wakes. Never forward a monitor correction
   verbatim after the tuple changes.
4. Resolve `action_kind`, `required_skill`, and `authority` through the matrix
   below. Read the selected skill completely before acting. Normalize any PM
   message whose visible Slack body declares a specific PR `MERGE READY` or
   `merge-ready` with an exact head to `MERGE_READY` or
   `MERGE_READY_INVALID`, even when the monitor envelope says
   `CTO_DECISION_CONSUMPTION`.
5. If authority is `EXECUTE_NOW`, run the skill or terminal action now. When
   the matrix names a dedicated execution task, send the complete exact tuple
   to that existing task once; accepted delivery is the terminal action for
   this CTO decisions task. If that task is blocked under the blocked-owner
   replacement rule, select another existing non-conflicting task instead of
   queueing or waiting; the exact-current-main red P0 fast lane routes to the
   existing rescues task when PR-merges is occupied. Never
   wait for the delegated task, poll its progress, poll GitHub, or install a
   heartbeat.
   If authority is `RAJIV_DECISION`, present the verified decision with options,
   recommendation, consequences, and exact closure evidence to Rajiv in DM
   `D0BPG55FG72` with `<@UEQTTB97A>` and the source `thread_ts`. If it is
   `PM_CORRECTION`, send one exact correction to PM in `#heydonna-dev` only.
6. For work executed in this task, verify the downstream terminal state. For
   a dedicated execution lane, verify only exact-tuple handoff acceptance and
   return; its terminal result is consumed as a later wake. A label, plan,
   acknowledgement, command launch, packet path, or prose receipt is not
   completion of the delegated execution itself.
7. After every non-duplicate normalized `MERGE_READY` wake, use
   `heydonna-cto-merge-ready-sweep` only to validate and hand off the exact
   tuple; do not take or post a portfolio snapshot. For `MERGE_READY_INVALID`,
   post only the exact correction and stop. The dedicated merge task emits the
   sole bounded portfolio sweep after it verifies a successful merge. Never
   wait for the merge task, poll GitHub, use `gh run watch`, or create a
   heartbeat/automation.
8. Post at most one concise outcome to `#heydonna-dev` when PM coordination is
   required. Never post to `#heydonna-pm` or `#heydonna-feedback`.
9. Record fingerprint, verified tuple, action, skill, authority, result, and
   closure evidence in the processed-wake ledger. Record the healthy
   `WAKE_CONSUMED` acknowledgement in that ledger only; do not forward a
   routine callback to the originating monitor task. Send a monitor-task
   message only for the actionable/error/decision/blocker classes allowed by
   the monitor callback boundary above.

These steps run only in the receiving CTO task. A monitor delivery receipt is
not wake consumption, and the monitor must not wait for this sequence to
finish.

## CI and capture alert processing

Treat genuine HeyDonna CI/E2E and capture alerts as executable release events,
not report-only notifications. Verify the repository, PR or `main` identity,
full 40-character head, workflow/event, run and required job once before acting.
Deduplicate by PR/head/run (or capture/head/run) and preserve single-flight.
Green and capture terminals below are owned by merge task
`01a0324b-68e0-7491-988f-e7e1549f16f7`. Send the exact tuple there when it can
accept the work immediately. If it is already occupied, route product/test/
capture work once to existing rescues task
  `019f942b-63ea-7953-b2ea-c4786c850b87`, or a proven bounded control-plane
  defect once to Master of Panes task
  `01a04154-c9c1-7bc1-8f7b-009a87bc7628` for candidate preparation.
Never create a task through `codex_app`. The accepting task remains the single
accountable owner through the terminal receipt. Do not send CI/capture alert
ownership to the standing rescues task by default when merge can accept it.

### PM-investigated PR CI/E2E failure reports (Rajiv 2026-08-29)

For a terminal-bad required `pull_request` CI or E2E run, a raw alert is not a
CTO investigation handoff. PM launches exactly one read-only *Sonnet 5* failure
investigation agent. That agent binds the exact PR/head/run/attempt, consumes
the relevant run/job logs and retained Modal artifacts once, identifies the
first causal boundary, and completes the causal report. PM posts the full
report to the PR transition thread and relays that same completed report to
CTO. PM does not raw-relay the alert, block or relabel the PR, dispatch a slot,
rerun, or capture before the report.

On receipt, CTO sends the completed report to PR-merges task
`01a0324b-68e0-7491-988f-e7e1549f16f7` for evidence verification and disposition.
This verification is not a Codex review and must not invoke a Codex reviewer,
companion review, or review-marker gate. PR-merges acts as follows:

- confirmed infrastructure/flake: after duplicate-active and eligibility
  checks, trigger exactly one unchanged-head retry;
- verified strict-replay fixture miss: trigger the canonical exact-head capture;
- production-shaped reproduction required: return the verified report to PM so
  PM assigns a numbered repro slot;
- conclusive product/test cause not requiring reproduction: route the smallest
  bounded off-slot rescue or hold through the existing release owner.

The raw alert may be recorded/deduplicated locally, but CTO takes no release
mutation from it while the PM investigation report is pending. Exact-current-
`main` red retains the separate P0 fast lane below.

An affected-test proof file or receipt is never a prerequisite for an
authorized fresh CI/E2E recovery. Exact PR/head binding, the prior terminal,
absence of a duplicate active run, and the workflow eligibility condition are
the complete recovery gate. Remove or bypass any control-plane consumer that
still demands `affected-test-proof-*` before triggering the one fresh wave.

1. **Green CI/E2E:** when both real required `pull_request` workflows—`CI` and
   `E2E Smoke Tests`—are successful on the exact current PR head, send that
   exact tuple to merge task `01a0324b-68e0-7491-988f-e7e1549f16f7`. The merge
   task verifies zero unresolved review threads and product/release gates, runs
   `scripts/ci/pre-merge-current-head-ci-guard.sh`, and merges head-pinned. One
   green leg alone, dummy/generic contexts, stale runs, or skipped shells are
   not merge authority.
2. **Red CI/E2E:** for a PR, wait for the completed PM/Sonnet 5 investigation
   report above, then send that exact report once to PR-merges for verification
   and the deterministic disposition. Do not consume the failed log again or
   add a Codex review step. Preserve any still-valid green workflow leg instead
   of duplicating the whole pair. Never rerun before causal confirmation, merge
   red, or infer cause from notifier prose alone. Exact-current-`main` red
   continues to use the P0 fast lane above. A nonterminal run trapped in a
   proven runner/JIT binding loop is not a red-workflow investigation; use the
   `CI_E2E_RUNNER_STUCK` degraded recovery above immediately and repair the
   control plane separately.
3. **Green capture:** verify the canonical capture workflow completed
   `SUCCESS` on the exact target head. The same release owner then merges exact
   current main non-force when needed, runs the smallest conflict-sensitive
   focused proof, and triggers exactly one real `pull_request` `CI` + `E2E
   Smoke Tests` wave on the resulting head. Capture-internal manifest,
   promotion, observed-key, body-SHA, and replay receipts are diagnostic only,
   not admission gates; the subsequent genuine E2E is authoritative. Do not
   repeat capture unless that subsequent exact-head E2E proves another cache
   miss.
4. **Red capture:** the accepting merge or rescues task owns one
   exact-run investigation. Consume the failed job logs and retained capture
   artifacts once, identify the precise producer/request, fixture-promotion,
   product, or infrastructure boundary, and return a typed cause plus smallest
   next action. Do not recapture, rerun CI/E2E, hand-edit fixtures/hashes, or
   merge until that causal terminal authorizes the next boundary.

Post only the material action or typed blocker in the originating Slack thread.
Record the verified tuple, owner, action, and closure condition in the durable
processed-wake ledger.

### Capture-harness changes are app CI slot work (Rajiv 2026-08-24)

- A change to a capture workflow, capture profile or selector, E2E capture spec,
  capture fixture/seed, strict-replay harness, or promotion/readability assertion
  is app CI/test-harness work, not `control_plane_only` work.
- Reproduction, implementation, and focused proof for such a change require a
  numbered slot. Never route a capture-harness fix to the dedicated
  direct-control-plane repair task.
- Reserve `control_plane_only` for orchestration outside the app CI harness—such
  as admission state, receipts, budgets, serialization, or deployment wrappers—
  that does not change which app/E2E producer runs or what that producer proves.
- After the slot returns a new exact head and focused proof, use the normal
  functionality-review, admission, capture, and strict-replay sequence.

## CTO wave admission and stale `ci-head` recovery (Rajiv 2026-08-24)

PM transition/control-plane machinery is for PM-owned work. A CTO-directed PR
review, release, or CI wave must not invoke `pm-transition`, Family-2, numbered
slot ownership, MoP assignment, or a PM parked-target/assignment-owner check.
Those PM-side records cannot block or authorize a CTO wave.

For a CTO wave, the dedicated review task supplies the exact-head functionality
verdict and the merge task owns admission. After reverifying OPEN, non-draft,
exact head, mergeability, required product/capture/review/visual-QA/privacy/
release gates, and absence of a genuine active exact-head pair, PR-merges may
directly reconcile only the wave-admission labels: clear stale superseded
`pm-state:blocked-rework` plus its stale `pm-blocked:*` companion when the CTO
review expressly found no current product/runtime/data/security risk, set the
valid CI-admission state, replace a stale `ci-head:<old-sha>` with the full
current head, and let the sanctioned label gate emit exactly one genuine CI +
E2E pair. This is the normal CTO-wave boundary, not a degraded PM transition or
a control-plane repair.

For PM-owned admission outside a CTO wave, keep using the canonical PM
transition and its control-plane rules. Never use the CTO-wave boundary to
clear a real product blocker, unresolved review risk, capture requirement,
visual-QA/privacy/release hold, or an active exact-head workflow. Never mutate
numbered slots or MoP, raw-edit unrelated labels, directly dispatch workflows,
reconstruct proof packets, rerun, merge, or bypass any substantive gate.

**Docs-only exemption (Rajiv 2026-08-24):** an exact-head PR whose complete
changed-file set is documentation-only does not require paid `CI` or `E2E Smoke
Tests`. Bind the exemption to the shared exact-head change-scope classifier
(`control_plane_only=true`, `product_changed=false`, `ci_required=false`, and
`e2e_required=false`) and preserve all applicable review, issue-contract, and
merge-head guards. Never infer docs-only from a title, label, or PR prose, and
never apply this exemption to mixed, empty/unknown, workflow, fixture, or
product/runtime diffs.

## Action matrix

| Verified wake | action_kind | required skill/action | Authority and terminal behavior |
| --- | --- | --- | --- |
| Exact-head merge-ready handoff with all gates satisfied | `MERGE_READY` | `heydonna-cto-merge-ready-sweep` | `EXECUTE_NOW`. Forward the exact tuple once to dedicated merge task `01a0324b-68e0-7491-988f-e7e1549f16f7`; do not merge, wait, or emit a portfolio sweep in this task. The merge task emits exactly one sweep only after verified merge success. |
| Premature or contradictory merge-ready claim | `MERGE_READY_INVALID` | `heydonna-pr-review`, then `heydonna-cto-merge-ready-sweep` | `PM_CORRECTION`. Name the single missing or stale gate and require canonical promotion; do not raw-edit labels, merge, or emit a portfolio sweep. |
| Exact-head CTO review requested | `CTO_PR_REVIEW` | `heydonna-pr-review` | `EXECUTE_NOW` for read-only review. Return approve/revise/block and the one terminal next transition. |
| PM-owned bounded PR rescue requested after a verified review/rework cap | `CTO_RESCUE` | hand off to rescues task `019f942b-63ea-7953-b2ea-c4786c850b87`, which uses `heydonna-cto-rescue-pr` | `EXECUTE_NOW` only when the durable handoff assigns the exact PR/head and bounded contract to CTO rescue. Forward once and return; the rescues task sends the digest-bound patch/evidence packet to CTO decisions task `01a03236-2e61-71f3-a6a8-3dc24d8c8917` for independent review, without pushing, merging, or posting it directly to PM. |
| Rajiv explicitly authorizes Codex to repair and push one exact PR head | `CTO_DIRECT_RESCUE` | hand off to rescues task `019f942b-63ea-7953-b2ea-c4786c850b87`, which uses `heydonna-cto-direct-rescue-pr` | `EXECUTE_NOW`. Forward the exact source tuple once and return. The rescues task preserves the exact source head, uses a detached worktree, pushes non-force, and returns the exact new head and proof. Direct rescue is never inferred from `CTO_RESCUE_REQUIRED` alone. |
| Rajiv explicitly authorizes an urgent P0 ship/hotfix | `CTO_HOTFIX` | hand off to rescues task `019f942b-63ea-7953-b2ea-c4786c850b87`, which uses `heydonna-cto-hotfix` | `EXECUTE_NOW`. Forward once and return; the rescues task stays within the named incident and release boundary and must not broaden the hotfix. |
| Genuine required CI or E2E failure on the exact current `main` commit | `MAIN_RED_P0` | send to PR-merges, or to the existing rescues task if PR-merges is occupied; use `heydonna-cto-hotfix` for product/test scope or `heydonna-cto-direct-control-plane-repair` for proven allowed control-plane-only scope | `EXECUTE_NOW` under Rajiv's standing 2026-08-22 authority. Verify the exact push run/job once, hand off immediately through `$codex-stdio-send-message`, and return after accepted delivery. The task owns causal repro, smallest fix, focused proof, safe direct publication, and event-driven replacement CI/E2E. A confirmed infrastructure failure must receive one unchanged-head retry after causal proof, duplicate-active check, and the concrete health/eligibility condition clears. If both existing owners are blocked, escalate the exact ownerless P0 to Rajiv. No task creation, PM/slot ceremony, pre-classification rerun, polling, or unrelated scope. |
| Issue-only rescue/rescope circuit breaker with no exact PR patch | `CTO_ISSUE_RESCUE` | decision analysis; use rescue skill only after an exact patch exists | Usually `RAJIV_DECISION`. Verify the issue, original directive, failure mode, and artifacts; recommend `final_verified_patch`, `split_and_reimplement`, or `override_with_evidence`. After Rajiv chooses, require the canonical typed rescope transition and successor issue when applicable. |
| Product, architecture, privacy, data-model, UX, or release-policy decision | `CTO_PRODUCT_DECISION` | product/architecture decision analysis | `RAJIV_DECISION` unless Rajiv's choice is already durable. State intended capability, production failure mode, runtime control point, options, recommendation, rollout/rollback, acceptance criteria, and downstream PM transition. Never treat this as a routine operational correction. |
| CTO product decision already made but not consumed | `CTO_DECISION_CONSUMPTION` | verify decision, then exact PM transition | `PM_CORRECTION`. Quote the durable choice and require one canonical consumption transition; do not reopen the decision. |
| Any verified bounded control-plane defect or stabilization | `CONTROL_PLANE_REPAIR` | PM reports the first literal blocker/tuple to CTO; CTO diagnoses and sends one exact brief to MoP task `01a04154-c9c1-7bc1-8f7b-009a87bc7628` | `EXECUTE_NOW` after verifying bounded non-product scope and no duplicate owner. MoP returns candidate-only for exactly one CTO inline review; BLOCK returns rework to the same task; APPROVE makes CTO sole publisher/rollout/live verifier/PM notifier. PM has zero review/approval/retry role; never use a numbered slot. |
| PM Operator, direct MoP/GitHub command adapter, executable caller cutover, or `pm-transition` retirement change | `PM_OPERATOR_CUTOVER` | CTO sends one exact brief to MoP task `01a04154-c9c1-7bc1-8f7b-009a87bc7628` | `EXECUTE_NOW` under Rajiv's current directive. MoP prepares and returns one candidate-only packet for CTO inline review; it does not publish or deploy. CTO alone publishes, rolls out, verifies, and notifies PM after approval. Preserve the exact baseline, migrate one reachable family at a time, and never route this work to PM, the retired generic repair route, or a numbered slot. |
| Legacy or explicit `CTO_DIRECT_CONTROL_PLANE_REPAIR` label for a bounded control-plane defect | `CTO_DIRECT_CONTROL_PLANE_REPAIR` | normalize to the same CTO/MoP candidate-only contract above | `EXECUTE_NOW`; MoP prepares one candidate for exactly one CTO inline functionality review. A block returns to MoP; an approval is published and rolled out only by CTO Decisions. |
| Numbered slot held by a control-plane repair (occupied-inactive or free-standby, off-slot repair running) | `CONTROL_PLANE_REPAIR_SLOT_HOLD` | exact-slot verification; then CTO Decisions applies the shared native bypass contract | `CTO_CORRECTION` only after one typed high-level refusal. Verify the exact slot log first (inactive turn, no active process, clean exact-head checkout, incident binding). For a full release/refill, use direct MoP `curl` with exact slot/identity/epoch and canonical authority header/payload, record response/readback, then complete-set GitHub labels, then exactly one literal `message-slot` packet; stop on failure and preserve substantive blockers. The repair stays off-slot and may not delay refill; no PM mutation, second owner, raw workflow, or numbered-slot repair work is allowed. |
| Slot/PR ownership or transition mismatch | `SLOT_TRANSITION` | exact-slot verification; `codex-slot-rescue` only when explicitly authorized | Usually `PM_CORRECTION`. Preserve productive work. Direct PM to one canonical transition only after reading the exact slot log and live tuple. |
| Occupied slot reports repeated same-head "in progress"/LOCAL_CONTINUE with no commit (PM JSONL slot deliveries + MoP nudge corroboration) | `SLOT_FALSE_PROGRESS` | verify the exact slot log tail, live MoP row/epoch, and checkout head; confirm no active canonical assignment/reconcile command | `PM_CORRECTION`. Direct PM to force terminalization now: commit + push the current work to a new head (affected proof, phase-a/planner/CI at the new head) or name a typed blocker; if no shippable commit within the 30m SLA, park/block the issue/PR preserving the partial patch, release the slot, and assign the next highest eligible rework. Failure: the slot remains at the same head with only "will continue" hold updates past the SLA. |
| PM checkout is on a feature branch instead of main (PM JSONL/logs show the PM clone at `fix/...` while doing PM-owned work) | `PM_CHECKOUT_BRANCH` | verify PM clone branch: `git -C /Users/rajiv/Downloads/projects/heydonna-app rev-parse --abbrev-ref HEAD`; exclude disposable worktrees and slot clones | `PM_CORRECTION`. Remind PM to return to main: `git checkout main && git pull --ff-only origin main`, then rerun the affected-test/review through the dedicated background agent or a disposable worktree / the owning slot's clone. |
| Save-suppression production-debug heartbeat while the incident is open | `SAVE_SUPPRESSION_PROD_DEBUG` | consume one exact window/receipt; continue the existing rescues/hotfix owner through the emission, transport, query, and durable-transition boundary | `EXECUTE_NOW` for every material delta. A second consecutive nonzero packet with no new causal fields is `SAVE_SUPPRESSION_TELEMETRY_EMIT_GAP` and must be returned to the same owner immediately for the smallest main observability correction or typed external blocker. New escape/error/loss signatures are immediately material. Never create a second investigator, poll Axiom, act on customer files, infer loss from counts, or close on a clean/quiet window. Closure requires build-bound production evidence joining suppression to handoff/sweep/admission after the deployed fix. |
| Three-hour heartbeat proves an open PR has no genuine exact-head capture, CI/E2E, or active numbered reproduction/integration proof | `OPEN_PR_ACTIVITY_GAP` | verify one bounded live PR/head/run/job and MoP owner snapshot, then select the existing safe lane | `EXECUTE_NOW`. If a canonical capture or genuine CI/E2E boundary is already authorized and safe, send the exact tuple once to PR-merges. If the missing boundary requires production-shaped reproduction/integration proof, direct PM to assign one eligible numbered slot to the exact PR without duplicating a live owner. If a job has remained queued with zero steps/no runner for at least 15 minutes, apply `CI_E2E_RUNNER_STUCK` or the equivalent capture recovery through the release owner. A concrete product/security/data-safety blocker may remain held only with its exact evidence, owner, and executable wake condition; passive dependency labels or process defects are not a fourth lane. Post the material action or typed blocker to the originating PM/heartbeat thread, record it once, and return without polling. |
| Raw terminal-bad PR CI/E2E alert, PM report not yet complete | `CI_FAILURE_REPORT_PENDING` | no CTO investigation or reviewer; await the one PM-launched Sonnet 5 report | `VERIFY_ONLY`. Record/deduplicate the exact PR/head/run/attempt and stop. Do not raw-relay, consume logs, dispatch a slot, block/relabel, rerun, capture, or invoke Codex review. |
| Completed PM/Sonnet 5 PR CI/E2E investigation report | `CI_FAILURE_REPORT_COMPLETE` | send the exact report once to PR-merges task `01a0324b-68e0-7491-988f-e7e1549f16f7` for evidence verification, with no Codex review step | `EXECUTE_NOW`. PR-merges verifies the report without re-consuming the failed log: confirmed infra gets one unchanged-head retry after duplicate/eligibility checks; verified fixture miss gets canonical exact-head capture; production-shaped repro requirement returns to PM for a numbered slot; otherwise route the smallest bounded off-slot rescue/hold through the existing release owner. Preserve any valid green leg and single-flight ownership. |
| Exact-head CI/E2E queued at least 15m with no runner binding and a proven repeated runner/JIT control-plane loop | `CI_E2E_RUNNER_STUCK` | merge task executes the stuck-run degraded recovery; rescues is the one fallback owner | `EXECUTE_NOW`. Cancel only the proven stuck queued run, merge current main non-force when behind, run focused conflict proof, push a descendant, and emit one label-gated exact-head pair. If already current-main-bound, use the canonical one-time fresh-run recovery. Repair the runner defect separately; never wait for that repair, raw-dispatch, duplicate the old job, or create a second release owner. |
| PM reply to a CTO/decisions thread | `PM_REPLY` | render thread and verify claimed downstream state | `VERIFY_ONLY`, then execute any newly authorized matrix action. A reply wake must never be acknowledged without consumption. |
| Dropped or unroutable communication | `TRANSPORT_FAILURE` | verify alternate durable delivery | `PM_CORRECTION` only if still unhandled; otherwise close as duplicate delivery. |
| Stale, duplicate, healthy in-flight, or disproved candidate | `IGNORE` | none | Record suppression; send no PM chatter. |

## Exact-head remote-capture rule for fixture misses

A verified strict-replay fixture miss — including an HTTP 424
`fixture_miss` or independently proven absent canonical request key — is
resolved only by a remote capture on the exact current PR head that produced
the miss.

- The CTO rescue owner runs the canonical remote-capture workflow on that PR
  head. PM does not trigger the capture or the subsequent label-gated wave.
- Fail closed on head drift. Capture on `main`, a different PR head, a local
  checkout only, or a stale merge ref does not satisfy the missing-key tuple.
- A terminal canonical capture `SUCCESS` completes the capture boundary.
  Capture-internal manifests, observed-key lists, promotion/readability
  receipts, body hashes, and capture-workflow replay are diagnostic evidence,
  not release gates. Generated local fixtures and prose receipts do not replace
  the canonical capture terminal.
- After capture succeeds, the CTO release owner merges exact current main
  non-force when needed, preserves both contracts with focused proof, and fires
  exactly one real label-gated `pull_request` CI + E2E wave on the resulting
  descendant head. The genuine E2E strict replay is authoritative.
- A blind rerun, local repro PASS, or product-code review without the canonical
  remote capture does not clear the original fixture miss. A current-main-only
  merge after successful capture does not require recapture; only a new exact-
  descendant E2E cache miss does.

## CTO product-decision contract

Product decisions are first-class CTO wakes. The decision packet must include:

- Rajiv's original directive or the closest durable source;
- intended user capability and production failure being prevented;
- runtime control point and data/authorization boundary;
- materially different options and privacy/data-integrity consequences;
- one recommended option, rollout, rollback, and measurable acceptance criteria;
- the exact PM transition that consumes the decision.

If Rajiv's choice already exists in the thread, consume it; do not ask again.
If the decision changes the issue contract, PM must update the issue/ACs before
implementation begins.

## CTO rescue contract

Rescue is not one generic action:

- `CTO_RESCUE` is a bounded, PM-routed rescue that returns a patch and evidence
  to CTO decisions task `01a03236-2e61-71f3-a6a8-3dc24d8c8917` for independent
  review without pushing or replying directly to PM.
- `CTO_DIRECT_RESCUE` may push only after explicit Rajiv authorization for the
  exact PR/head.
- `CTO_HOTFIX` is reserved for explicitly authorized urgent release work.
- `CTO_ISSUE_RESCUE` is a rescope/product decision until a concrete exact-head
  patch exists.

All executable `CTO_RESCUE`, `CTO_DIRECT_RESCUE`, and `CTO_HOTFIX` work is
owned by dedicated rescues task `019f942b-63ea-7953-b2ea-c4786c850b87`.
The CTO decisions task verifies the wake, forwards the exact contract once,
records accepted delivery, and returns immediately. It never performs the
implementation itself and never waits on rescue progress. Rescue monitors,
review follow-ups, and terminal handling stay with the rescues task until it
returns a verified new head, digest-bound patch, or typed blocker as a later
wake to CTO decisions task `01a03236-2e61-71f3-a6a8-3dc24d8c8917`. Delegated
tasks never return execution results directly to PM; CTO decisions owns the
review and any downstream PM communication.

Every PR rescue binds issue, PR, 40-character source head, directive, runtime
control point, allowed paths, proof commands, rollback, and downstream PM
transition. A head change invalidates affected proof. Rescue completion is a
verified new head or digest-bound patch plus canonical PM consumption, not a
process launch or packet path.

## Fail-closed rules

- The CTO decisions task never uses any `codex_app` tool. All messages to
  existing Codex tasks, including delegated terminal returns to CTO decisions,
  use `$codex-stdio-send-message` with exact task IDs and stable event-specific
  dedup keys. Never retry or reroute `status=uncertain`.
- Never merge when the live head differs from the wake or the fresh guard is
  not green on real `pull_request` CI and E2E.
- Never execute or monitor a PR merge in the CTO decisions task. Forward it to
  dedicated merge task `01a0324b-68e0-7491-988f-e7e1549f16f7`; do not poll,
  run `gh run watch`, or create a heartbeat while waiting.
- Never execute or monitor a rescue or hotfix in the CTO decisions task.
  Forward it once to dedicated rescues task
  `019f942b-63ea-7953-b2ea-c4786c850b87` and return without polling.
- Never implement, deploy, or monitor any bounded control-plane repair in CTO
  decisions or PM. After ownership and scope verification, CTO sends one
  exact brief to MoP task `01a04154-c9c1-7bc1-8f7b-009a87bc7628`; MoP returns
  its candidate for one CTO inline review. CTO alone publishes, rolls out,
  restarts when required, verifies, and informs PM after approval.
- Never route a PM Operator or `pm-transition` cutover change to superseded task
  `01a03c74-fc97-7a62-bb47-001ac7fb0710` or the generic control-plane task. Use
  only `01a04154-c9c1-7bc1-8f7b-009a87bc7628`.
- Never keep any implementation, test, review, investigation, deployment,
  browser/service configuration, monitor, wait, or repeated-verification task
  in the CTO decisions task. Route it to one of the three reusable execution
  tasks and return after exact-tuple handoff acceptance.
- Never poll GitHub in any task. Only a single bounded snapshot at wake
  consumption or terminal action is permitted; further state must arrive by
  event or receipt.
- Never perform a direct product-code rescue without explicit Rajiv authority.
- Never interrupt or duplicate a live slot run based only on MoP or a stale
  monitor sample.
- Never let a control-plane repair reserve numbered-slot capacity.
- Never convert a product/architecture decision into a PM operational guess.
- Never mark a wake complete from prose, a label, or an unverified receipt.
