# HeyDonna CTO wake-consumption SOP

## Purpose

This SOP governs the CTO task that receives `SENTINEL_*` delegations from the
PM-chat monitor. `MONITOR.md` decides whether to wake. This file decides what
the CTO task does after a wake arrives.

The sole Slack/monitor-wake consumer is CTO task `01a09112-a09c-7361-9a2a-0ada6a4e9dfb`. The monitor task
`019fd213-c346-7b72-a46d-12d7ff146eef` must only deliver the wake; it must never
execute this SOP, run a listed skill, post to Slack, direct PM, make a decision,
or verify downstream completion. Envelope routing fields are instructions for
the receiving CTO task, not authority for the monitor.

A wake is a candidate report, not workflow proof. The execution owner must
verify the exact current tuple before taking an external action. A verified
wake with existing authority must be processed to its real next boundary; it
must not be reduced to a status summary.

## Internal return split (Rajiv 2026-09-16, Ev0C31576QTA)

Rajiv approved a dedicated result consumer in DM `D0BPG55FG72`, message
`1789541986.505079`, thread `1789538813.368759`.
CTO Returns is task `01a0a905-0b30-7ca1-8fbd-14ae8f87282f` (`cto returns`),
governed by `/Users/rajiv/.codex/monitors/cto-returns/WAKE_SOP.md`.

Cutover is `ACTIVE` for NEW briefs: CTO DM verified Full access and sent
`CTO_RETURNS_ACTIVATE` on 2026-09-16 under permission completion
`Ev0C22BLHU21` (activation turn `01a0a92d-6f57-7ce3-a371-91bd3d7881a3`).
NEW Decisions execution briefs name CTO Returns as `return_task_id`, carrying the
source thread, exact tuple, existing executor, scope/authority, allowed next
steps, holds, dedups and final boundary. Require the worker to echo the compact
brief on return and to deliver COMPLETE, BLOCKED or STOPPED-INCOMPLETE even
when routine progress/ACK chatter is suppressed.

Rajiv Ev0C2BSTPGA1 (2026-09-17, DM1789625174.089799,
thread1789624059.070379) replaces the Ev0C2DHYNUTY dispatch-registration
design with a simple five-minute missing-terminal check. Do not send separate
CTO_RETURNS_TRACK registrations or maintain duplicated chain-progress records.
The executor's accepted brief, stopped turn and actual terminal-send receipt
are checked in task history. Existing registration deliveries and ledger data
are preserved as history, not replayed or deleted. Returns alone maintains
minimal task/assignment/turn delivery and one-reminder state in its existing
work-ledger.json; no new ledger or service. This does not weaken the complete
execution brief or the obligation to deliver every substantive terminal.

Decisions keeps incoming requests, scope, priorities, architecture and reserved
technical/policy decisions. Returns consumes candidates,
review verdicts and execution terminals; reconciles duplicates/superseded evidence; delegates
routine authorized continuation to the same existing owners; and closes the
chain with one material source-thread completion. Its existing heartbeat now
performs a bounded five-minute history-backed terminal-delivery check under
its SOP, with one same-owner delivery reminder for a proven missing return,
no implementation restart, no interruption/replay, and quiet
unchanged checks. It escalates only reserved decisions outside the approved
contract or concrete unresolved safety/authority conflicts, not routine
technical continuation, with one question and recommendation.
No automatic forwarding of every result or completion ACK back to Decisions.

This section supersedes lower generic instructions that Decisions must consume
every return, route every review/rollout, or post every final reply: the named
result consumer performs those duties for its chain. It does not transfer
execution, independent review or reserved decisions to Returns. Preserve
delegation-first, single-owner, review, full-suite and release safety rules.

Initial rollout changes new briefs only. Pre-cutover assignments retain their
explicit result consumer; DM-owned returns stay with CTO DM. Later migration
is an explicit per-assignment handoff, never queue copying/replay/deletion or
broadcast. No old assignment is moved by this edit. Slack bridge/PM-monitor
ingress, existing execution owners and fork-if-busy removal are unchanged.

A later Slack wake still enters Decisions. If its exact assignment is already
Returns-owned, steer only the material new evidence/authority to that consumer
once, without separately advancing the same edge here. Unbound or mixed
requests stay here for classification; a PR mention alone is not ownership.

### Workflow investigation returns (Rajiv Ev0C2CF75LSD; amended Ev0C2K9LQHCZ)

PR Merges sends E2E investigation results directly to CTO Returns
`01a0a905-0b30-7ca1-8fbd-14ae8f87282f`, not Decisions. Every such brief
must name that result consumer, including the exact source, run/attempt,
execution owner, accepted effects and remaining authority. Returns owns the
routine classification, convergence, correction/retry/release routing and
source-thread closure through existing executors. Do not send a Decisions
copy or ask Decisions to approve a routine next step within standing policy.
Escalate only a concrete unresolved safety/authority conflict or a reserved
scope, policy, access, priority or material-cost change. DM-owned assignments
still return to CTO DM.

A concrete CI **test** failure does not enter the investigation-return path.
Decisions sends the exact PR/head/run/attempt and failing test signal directly
to one eligible registered Rescues task. That owner fixes the failing tests,
runs the entire canonical suite on the corrected exact candidate with zero
skipped, deselected, filtered, quarantined, or unexecuted phases, publishes
non-force, and triggers the one fresh exact-head required workflow recovery.
It returns COMPLETE or BLOCKED to CTO Returns. Do not send the same CI test
failure to PR Merges for causal investigation and do not assign a numbered slot.

For older investigations, transfer an undelivered result by an explicit
same-assignment consumer correction, with Decisions relinquishing it before
Returns continues. Never resend a result already delivered to Decisions:
Decisions reconciles its accepted effects and hands off the remaining chain
once with the full result and authority. Do not replay investigation, clear
queues, broadcast a terminal or move an uncertain delivery.

## Decisions delegates execution (Rajiv 2026-09-15, Ev0C1YHFBBHU)

CTO Decisions is a decision-and-routing task, not an execution worker. This
role contract takes precedence over every conveyor, action-matrix, urgency,
fallback and generic "CTO" instruction below. `EXECUTE_NOW` means route the
authorized execution now; it never means perform delegated work inline.

**Inline allowlist:** minimal context reads needed to understand the request,
technical judgment, ownership selection, correspondence checks on returned
evidence, and communication/recording through the existing transport and
wake ledger. Delegate substantive investigation rather than completing it to
prepare a handoff; do not repeat the owner's proof on return.

**Named executors:**
- PR Merges `01a0324b-68e0-7491-988f-e7e1549f16f7` owns current-main
  integration, CI/E2E admission, exact-run E2E investigations, non-test CI
  infrastructure dispositions, and final head-pinned merge, including the
  fresh pre-effect checks and degraded path. Concrete CI test failures route
  directly to one eligible registered Rescues task.
- PR Reviews `01a0b53e-3316-77d3-9610-1c0c58d4ba5b` owns independent
  functionality reviews for hotfix/Rescues-origin product work and for
  control-plane candidates. Numbered-slot product work carries its Codex
  companion review and has no PR Reviews admission gate (Rajiv 2026-09-18,
  Ev0C2BE61939). Decisions adjudicates returned findings, not a second
  review. Preserve the existing one-review and explicit-waiver boundaries.
- Implementation, reproduction, tests, browser/service configuration and
  approved publication/rollout stay with the appropriate existing execution
  owner under the roster and affinity rules. PM alone executes numbered-slot
  release, clear, assignment, refill and slot-message delivery.

Urgency, a short command, tool failure, a five-minute degraded path, or a busy
owner does not transfer execution to Decisions. Resolve capacity through the
existing-owner/blocked-owner rules below; do not substitute inline work or
create duplicate ownership. Generic execution instructions below describe the
named executor's work, not additional Decisions permissions.

Steer updates to a current assignment through native
`codex_app.send_message_to_thread`, not stdio or the IPC CLI (Rajiv
Ev0C20LL3YBU). Use the existing new-assignment route for independent work.
After verified handoff acceptance, return without
waiting, polling or a follow-up heartbeat. Consume the terminal as a later
wake. Preserve accepted effects and active work; this rule adds no review,
release gate, approval hop or replay authority.

## Existing-owner routing (Rajiv 2026-09-16, Ev0C24UF2GDQ)

Source: DM D0BPG55FG72, message/thread `1789521897.130519`: "the forking of
tasks is not working. remove fork if busy from the SOP". This withdraws the
standing busy-task creation/fork rule from Ev0C198USJRK/Ev0C289NBMT2.

A busy destination does not authorize a fork or a new task. Do not invoke the
busy-task-fork procedure or send a task-creation request to CTO DM as a capacity
fallback. CTO Decisions remains a decision-and-routing task, never the inline
substitute for a busy executor.

- Keep an accepted tuple with its existing owner. Steer corrections/addenda
  through native `codex_app.send_message_to_thread`; do not enqueue them as
  unrelated jobs, duplicate execution, or interrupt active work.
- Route new independent work to an appropriate existing affinity owner using
  the existing new-assignment transport. A busy but accepting task may queue
  it. Record durable acceptance and return without waiting or polling; do not
  describe queued work as started. Use another eligible existing owner only
  within the roster/affinity and single-flight boundaries.
- If the destination is genuinely unavailable for the exact tuple, apply the
  blocked-owner rule below. Preserve the pending contract and return one
  concrete blocker if no compatible existing owner can accept it. Escalate
  only under the existing reserved-decision or live-incident rules, not merely
  because a task is busy. Never invent capacity or create a new executor.
- Already-accepted queued work remains with its owner. Any authorized
  transfer must preserve the complete latest packet and retire the old route
  with verified readback before a replacement starts. No queue clearing,
  history deletion, uncertain-delivery retry or concurrent execution.

This removes automatic routing authority only. Do not stop, archive, delete,
reassign or replay existing tasks or accepted work. Explicit task creation or
rotation requests remain separate. Preserve full canonical hotfix validation,
independent review, head-pinned release, access/budget, permissions and PM-only
numbered-slot boundaries. Existing owner IDs and accepted effects are unchanged.

## No capture receipts; strict replay is the only verdict (Rajiv 2026-09-15, Ev0C20EDNKM0)

A capture attempt is preparation, nothing more. Never create, require, repair,
inspect, consume, or gate on capture-internal success/failure receipts,
manifests, key or observed-key lists, body hashes, promotion or readback
receipts, outcome envelopes, internal replay receipts, or receipt markers.

After one bounded exact-head capture attempt, the sole verdict is a genuine
exact-head `pull_request` E2E run under strict fixtures: a pass means the
capture was sufficient, and a typed fixture miss permits the next bounded
capture. Receipt-only work must never delay a release.

Investigate capture machinery only when a capture cannot start, or when a
concrete execution or safety boundary fails. This top-level rule supersedes
any conflicting capture-receipt detail later in this SOP. The deeper
strict-replay clauses below, together with every non-capture receipt — task
handoff, CI admission, merge, Slack and wake-ledger records — remain in force.

### Capture verification: observed run id, never queue acceptance or branch-filtered listing (Rajiv 2026-09-20, Ev0C39UU6HS8)

Arming or reporting a capture binds an OBSERVED run identity — workflow name, run id, exact head — never a dispatch queue acceptance. A `queuedSubmissionId` proves delegation ownership only; it is not a start, and acceptance language ("in flight", "started", "running") is forbidden in capture status until the run is observed.

Verify by workflow name and run id (`gh run view <id>`), never by a branch-filtered `gh run list`. Pitfall observed 2026-09-20: an empty branch-filtered listing was reported as "never started" while run 35517730225 had completed green — a filtered listing is not evidence of absence.

Capture remains preparation only: after one bounded exact-head attempt the sole verdict is the genuine exact-head strict-replay E2E on the same head; a pass means the capture was sufficient, a typed fixture miss permits one further bounded attempt, and a head with a green capture is never captured twice.

### Capture implementation ownership (Rajiv 2026-09-18, Ev0C3LAP5YM6)

Capture workflow, harness, fixture-proxy, selection, and related test code in
the app repository are **app CI code**, not control-plane code. A correction to
those bytes is executable implementation work and PM assigns it to one
numbered slot through `direct-assign`. CP Repairs, Master of Panes, Rescues,
and PR Merges must not implement capture app-CI changes.

PR Merges remains the executor for exact-head capture runs, strict-replay E2E,
run investigation, admission, and merge. A numbered slot owns only the
capture-code candidate and focused local proof; after the final code change it
returns one candidate for the normal single functionality review. On APPROVE,
PR Merges executes capture and strict replay. Capture **code** is on-slot;
capture **execution** is off-slot.

Keep capture minimal: run the same E2E suite in capture mode, selecting only
tests that invoke Gemini, and record the raw provider exchanges. Do not add a
per-issue producer, fixture/body/key/hash validator, receipt or promotion gate,
or a second capture verdict. The genuine exact-head strict-replay E2E run is
the sole fixture and product validator.

Rajiv 2026-09-18 (Ev0C2T8R5T8S): the "Validate and promote staged capture
fixtures" step and its validation machinery are REMOVED for ALL capture
profiles, not only the index-sections lane. Capture ends at record + persist
where strict replay reads; there is no promotion gate and no receipt
consumption anywhere in the capture path.

## PR admission and final merge path (Rajiv 2026-09-11, Ev0C11U29GNP)

PR Merges executes PR CI admission and final merge using only the two
self-contained conveyor skills and no shared release-conveyor contract, extra receipts, proofs,
transitions, or ownership records. This is the normative PR admission/merge path.
Decisions delegates the exact packet and consumes the result; it does not run
either skill, integrate, admit or merge inline.

**CI admission (PR Merges):** check existing visual QA only when the PR changes
UI; merge exact current main into the PR with an ordinary conflict-free merge when it is
not already contained; then admit one exact-head CI/E2E pair with the bundled
admission skill.
Avoid only a genuine exact-head duplicate or a product-relevant merge conflict.

**PR merge (PR Merges):** require genuine exact-head CI and E2E success; inspect
only the later-main delta for changes relevant to the PR; match existing visual QA for a
UI-changing PR; then merge head-pinned. Relevant or ambiguous later-main
movement requires one integration and fresh admission. Unrelated movement does
not invalidate the green pair.

**Five-minute degraded path:** control-plane, proof-publisher, transition,
receipt, label-adapter, or owner machinery may delay either path for no more
than five minutes. PR Merges performs one bounded safety and duplicate check,
then uses the smallest direct GitHub edge that preserves exact-head and label-trigger
semantics; route machinery repair separately. Decisions delegates this edge
to PR Merges as well: degraded tools never change the executor. Do not
raw-dispatch workflows.
Block only for concrete likely data loss, security/privacy harm, irreversible
damage, unsafe customer behavior, a product-relevant conflict, or red required
CI/E2E.

### Mandatory PM post-merge cleanup (Rajiv 2026-09-17, Ev0C2E738QJX)

Every verified merged PR must enter PM's canonical `pm-cleanup-pr` path exactly
once after the merge terminal. This is a post-merge lifecycle action and must
never delay an otherwise safe head-pinned merge. PM starts it within 15 minutes
of the merge and the PR is not lifecycle-complete until live readback shows
`pm-state:closed-clean` or PM returns one typed cleanup blocker.

Use `/Users/rajiv/.claude/skills/pm-cleanup-pr/SKILL.md` and its manifest-installed
caller. Bind the cleanup to the verified repository, PR number, exact merged PR
head, merge commit, and GitHub's authoritative `closingIssuesReferences`. Never
infer a linked issue from the PR title, body text, or an incidental issue number.
When GitHub reports zero closing issues, use the documented
`cleanup_mode=merged_pr_issue_less` contract; it cleans only the PR and must not
close a referenced issue. Linked-issue cleanup may close only the authoritative
closing issue under the canonical caller's contract.

On every merge wake, Decisions verifies whether `pm-state:closed-clean` already
exists. If it does not, direct PM in the source thread to run the canonical
cleanup and return its readback; do not substitute PR Merges, CTO Decisions, a
numbered slot, or manual label surgery. A merge notice, issue closure, branch
deletion, or successful cleanup workflow is not by itself proof that PM cleanup
completed.

### Continuous open-PR conveyor (Rajiv 2026-09-12, Ev0C1A1JQHJA)

This is the controlling loop for every open PR. Rajiv must not need to prompt
the next step. Keep at least four distinct PRs moving whenever four safe,
eligible PRs exist:

1. **Admit:** delegate the next conflict-free head to PR Merges for
   its fresh fence and exact-head CI/E2E pair with `heydonna-cto-label-gated-ci`.
   Review gate by origin (Rajiv 2026-09-18, Ev0C2BE61939): numbered-slot
   product work is reviewed by its Codex companion during slot execution and
   is admitted WITHOUT a PR Reviews gate; only hotfix/Rescues-origin work
   requires the PR Reviews independent review before admission. This
   supersedes the 2026-08-11 review-convergence gate for slot-origin PRs.
2. **Merge green:** when both genuine exact-head workflows pass, immediately
   delegate to PR Merges to run `heydonna-open-pr-status` and merge head-pinned
   if its checks pass.
3. **Fix red:** when CI names a concrete failing test, send the exact tuple
   directly to one eligible Rescues owner for repair, the entire canonical
   suite, non-force publication, and one fresh exact-head workflow recovery.
   When E2E fails, send the exact run to PR Merges and explicitly require PM's
   independent read-only investigation; both return to CTO Returns for
   convergence. Non-test CI infrastructure failures remain with PR Merges.
4. **Refill:** every terminal frees or advances its lane. In the same wake,
   route the next eligible PR to refill below four. Do not poll active work.
5. **Keep the safety rails:** exact head, no duplicate active run, no blind
   rerun, and no red merge. Never use PM-side scripts, retry budgets,
   `pm-state` labels, or generic holds as conveyor gates.

A lane counts only while genuine CI/E2E, an exact-run investigation, a bounded
correction/reproduction/capture, or a head-pinned merge is actively executing.
Labels, completed work, status prose, and idle owners do not count. If fewer
than four PRs are safely eligible, move all eligible PRs and report only the
concrete product/runtime/data/security blocker for each empty lane. Older SOP
detail is subordinate to this loop when it would stop otherwise safe motion;
the Decisions delegation role contract above still governs every step.

## Shared release-conveyor contract

PR CI admission and final merge are outside this contract. The two self-
contained skills `heydonna-cto-label-gated-ci` (admission) and
`heydonna-open-pr-status` (merge) are the entire PR conveyor and depend on
nothing else.

Apply the normative ownership and motion matrix in
`/Users/rajiv/.codex/skills/_shared/release-conveyor-contract.md`. Slots are
for executable implementation, reproduction, or production-shaped proof
only. CI/E2E admission, capture execution, review, decisions, and external
waits are off-slot; implementation of capture workflow/harness/test bytes in
the app repository is app CI work and is assigned to a numbered slot. PM owns
Abi/customer-reported issue intake end to end: perform a bounded
investigation, deduplicate and file the accurate symptom/evidence, promote it
under the existing priority rules, assign eligible numbered work, and confirm
actual pickup. A confirmed symptom may be filed while its cause is unknown;
investigating, filing, promoting, and assigning do not require CTO or Rajiv
approval. Correcting a causal claim does not suspend intake. If no eligible
capacity exists, retain the owner and concrete next action; never invent free
capacity or overwrite preserved work. Return status, findings, and blockers in
the original report thread. Escalate only genuine product/acceptance,
access/data, destructive/high-risk, or material-priority decisions.
CTO Decisions owns technical rescue and release judgments. PR Merges executes
current-main integration, CI/E2E admission, authorized capture runs, strict
replay, and head-pinned merge. A numbered slot implements any required capture
workflow/harness/test correction before that execution resumes.
Decisions consumes workflow terminals and delegates the resulting edge. PM
alone executes every numbered-slot release, clear, assignment, refill, and slot-message delivery;
CTO supplies the exact technical packet or decision and never performs those
MoP mutations directly. Every nonterminal wake includes
`next_action`, `next_owner`, and `wake`; labels, holds, relays, watching, and
queue receipts are not motion. Code-ready without admission, a CI/capture
terminal, and a free compatible slot with executable drain work are hard
actionable wakes routed to CTO for the technical decision; any numbered-slot
release or assignment required by that decision is directed to and executed
by PM under the existing priority/safety rules.
Decisions routes action; it never mutates GitHub, MoP, workflows or product
state inline. The monitor remains delivery-only.

The receiving CTO wake must adjudicate and durably delegate the next transition
in that same wake; reporting or watching without a bounded owner/action/wake is
not a terminal.

### Numbered-slot mutation ownership (Rajiv 2026-09-13, Ev0C1CSJM3BQ)

PM is the sole executor for numbered-slot lifecycle mutations. CTO may inspect
live MoP and checkout state, decide the technical owner/packet, and direct PM,
but CTO must not call `direct-release`, `direct-assign`, `message-slot`, or any
`/slots/<slot>/release`, `/clear`, or `/assign` endpoint itself. This separation
prevents CTO and PM from creating competing slot authority or split-brain
ownership.

An idle occupied slot counts as available capacity only after PM releases it
through the canonical `direct-release` path. PM then uses canonical
`direct-assign` for the exact next packet, including any assignment-time clear
required by that skill. Dirty, unpushed, active, DND, or drifted slot state
remains a typed PM release/assignment blocker; preserving files elsewhere does
not by itself authorize CTO to clean or mutate the slot. CTO returns the exact
decision and constraints to PM and waits for PM's authoritative mutation
receipt before treating the slot as released or assigned.

This section supersedes every older statement in this SOP that gives CTO,
PR Merges, Rescues, or another off-slot owner direct numbered-slot release,
clear, refill, assignment, label, or pane-delivery authority. A future
exception requires a new explicit Rajiv instruction naming the exact slot
operation; technical urgency alone is not an exception.

### PM terminal -> CTO next edge

Every PM-owned terminal for an OPEN PR is delivered as exactly one bounded
`PM_CTO_TERMINAL` envelope in the canonical PM Slack thread. It is
non-suppressible and immediately material to CTO; prose, `PM_WAIT`, labels, or
an owner/queue receipt alone are not terminals. The envelope contains
`terminal_type`, PR number, full exact head, optional run/capture identity,
owner, bounded evidence summary, `next_action`, `next_owner`, `wake`, and a
durable `source_receipt`. Supported terminal types are
`FAILED_RUN_INVESTIGATION`, `NUMBERED_PROOF`, `REWORK_REVIEW_CANDIDATE`,
`CAPTURE_TERMINAL`, `ASSIGNMENT_TERMINAL`, and `TYPED_BLOCKER`.

The monitor wakes CTO once per terminal type + exact PR/head/source receipt;
duplicate delivery is suppressed. CTO consumes the envelope and, in that same
wake, adjudicates and durably delegates the mapped edge to its named executor:
failed investigation to causal routing; numbered proof to admission/rework; candidate to review or
correction; capture terminal to exact-head CI/E2E; assignment to next-boundary
verification; typed blocker to the safe degraded edge or a concrete harm
record. PM never executes CTO-owned CI/E2E admission, capture runs, integration,
or merge; PM does assign capture app-CI implementation to a numbered slot when
the capture code itself needs correction. The hourly open-PR audit checks only continuity (terminal emitted,
consumed, and next edge recorded); it may repair one missed wake once and then
fails loudly with `TERMINAL_CONTINUITY_BREACH`. It is not the normal mover.

The executable producer/parser/router is the manifest-mapped
`/Users/rajiv/.claude/scripts/pm-terminal-continuity.py`. The PM completion
boundary calls `complete`; the monitor calls `deliver` for that reserved key
using the existing CTO wake transport. `deliver` persists `effect-start`
before its first external effect and commits `delivered` only from an
authoritative receipt. Crash, timeout, nonzero, malformed, and response-loss
outcomes persist `ambiguous` and are never replayed. Exact replays are
suppressed; changed head or terminal type is a new key. `hourly-repair` is
permitted once only for an emitted key lacking CTO-consumption or next-edge
continuity, and never for an `effect-start`/`ambiguous` delivery.

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

## Outcome-driven communication

Post only a decision, actionable blocker, meaningful completion, or a requested
answer. PM mentions CTO for those material outcomes, not for routine chatter;
keep detailed review findings and receipts in the task or PR, where CTO can
adjudicate one repeated disagreement and move non-blocking nits to follow-up.
Every ordinary Slack post starts with a one-sentence `TL;DR`, uses at most three
bullets, and targets 120 words; link to detailed evidence instead of pasting
hashes. Longer text is allowed when safe execution requires it or the user
explicitly asks. There is no hard word-count rejection or truncation. Preserve
source-thread routing, guarded identity, requested answers, and all safety and
active-work protections.

### Merge-refusal DMs: PR and reason only

Rajiv's Ev0C15CJFNLW directive (2026-09-11, thread 1789134183.031749)
overrides the ordinary 120-word target for merge-refusal notices. Use one short
line: `TL;DR: PR #<number> — merge refused: <plain-English reason>.`
Link the PR number. Do not paste command output, hashes, run inventories,
dedup tuples, causal/review history, policy reconciliation or rollback plans;
retain those in the owning task/PR unless Rajiv asks. If already merged, say
so briefly rather than implying a pending hold. Attach any existing relevant,
safe-to-share visual proof with the DM; no new capture or unrelated customer
content. If attachment delivery is unavailable, state that briefly and link
the existing evidence rather than claiming it was attached. This is a message
format change only: preserve refusal/override authority, source threading,
identity checks and duplicate suppression. Never infer an override from it.

## Control-plane simplicity (Rajiv current directive)

### Rescues and hotfix task roster

Rajiv directive Ev0C1FNKPS2E, DM D0BPG55FG72, thread `1789263120.785199`,
message `1789267024.772969` (2026-09-13 IST): three Rescues tasks and one
dedicated hotfix task. Exact native task titles and IDs:

| Task | Task ID | Routing boundary |
| --- | --- | --- |
| rescues | `01a0b3b9-cb21-7200-b426-469defef65dd` | Registered Rescues owner; eligible for new work when live status shows it is idle and has no incomplete assignment. |
| rescues backup | `01a095ce-4e47-7a42-b3e5-a0bb53dc017c` | Registered Rescues owner; eligible for new work when live status shows it is idle and has no incomplete assignment. |
| rescues standby | `01a0986b-9951-7b01-a47b-af3b9a1309ca` | Registered Rescues owner with verified Full access; eligible for new work when live status shows it is idle and has no incomplete assignment. |
| hotfixes | `01a095ce-4e47-7a42-b3e5-a09fbd1ed60a` | Dedicated hotfix task; retain accepted work and verify capacity before assigning a new hotfix. |

This roster supersedes the fixed single-task routing in Ev0C18FF1894, not
accepted ownership or release authority. New hotfix work uses `hotfixes`,
including shared E2E auth/runner reliability and bounded control-plane
hotfixes. Capture workflow/harness/fixture-proxy/test implementation in the app
repository is excluded and routes through PM to a numbered slot. Bounded PR
rescues and confirmed exact-head CI corrections use one
eligible registered Rescues task. Before every independent new assignment,
read the live state of all three Rescues tasks and choose an idle task before
any busy task. `idle`, `inactive`, or `notLoaded` counts only when the task has
no in-progress turn or incomplete accepted assignment. Never route new work to
a busy Rescues task while another registered Rescues task is genuinely idle.
This live idle-first selection supersedes the prior predecessor fence and the
standby's `ROTATION_ACTIVATE` restriction (Rajiv Ev0C3BPT9PJ4). It does not
move an already accepted in-progress tuple: preserve that sole owner unless
Rajiv explicitly orders a transfer. Registration alone does not archive,
transfer work, or create another executor.
Existing Naomi, Yitzkak/S1 #7783, PR7769/S4, PR7721 and PR7740 assignments stay
with their accepted owners; rework and approval return to that same owner.

Choose one eligible, scope-compatible existing owner for each independent
assignment under existing-owner routing above; a busy destination does not
authorize task creation. Never duplicate execution or queue a
current-assignment correction as unrelated work.
Current-assignment updates are steered under Ev0C1B40M78V below. The entire
canonical hotfix suite, exact-byte review, single-flight and release boundaries
remain unchanged. CP Repairs owns non-hotfix control-plane repair outside MoP
affinity, but never capture workflow/harness/fixture-proxy/test code in the app
repository; that surface is app CI and slot-owned. `CONTROL_PLANE`
classification does not erase hotfix affinity.

PM reports the first literal control-plane blocker and its exact affected tuple
to CTO decisions. PM has no review, approval, deployment, retry, marker, or
admission role. CTO decisions delegates any needed causal investigation and
classifies the returned evidence as `PRODUCT`, `CONTROL_PLANE`, `MIXED`, or
`UNKNOWN`; only a verified bounded `CONTROL_PLANE` brief proceeds.

CTO decisions sends one exact implementation brief to the matching existing
execution task under existing-owner routing. New hotfixes use the dedicated
`hotfixes` entry in
the roster above; accepted work stays with its existing owner. Non-hotfix shared release/control-plane
work outside MoP affinity goes to CP Repairs task
`01a08f69-45db-71c2-b433-678419139ed7`; Master of Panes, PM Operator, and
`pm-transition` work goes to MoP task
`01a04154-c9c1-7bc1-8f7b-009a87bc7628`. The selected task creates a clean
current-main candidate, focused RED/GREEN and negative proof, rollout and
rollback plan, no-mutation inventory, and exact base/parent/candidate/tree/
stable-patch/path tuple, then returns it directly to the assignment's named
result consumer (CTO Returns for new post-activation chains) through
`$codex-stdio-send-message`. It does not use a numbered product slot or inform
PM.

The named result consumer sends that immutable candidate once to PR Reviews task
`01a0b53e-3316-77d3-9610-1c0c58d4ba5b`. PR Reviews performs the single
functionality-first independent review and returns `APPROVE`, `REVISE`, or
`BLOCK` to that same result consumer. A negative verdict returns one bounded correction to
the same implementation task. Approval returns to that same task for non-force
publication, installation/activation, required service restart, live proof,
and rollback verification. The implementation task returns one rollout
terminal; the named result consumer verifies the tuple and sends PM one terminal
with the landed tuple, proof, and exact next action. Neither consumer performs
implementation, review, publication, installation, deployment, restart,
monitoring or waiting.

Progress updates and queue receipts do not replace these boundaries. A main
advance permits one conflict-free replay only when paths and stable patch
identity remain identical; conflict or semantic change is a blocker. Candidate
returns use renderer-free `$codex-stdio-send-message` transport directly to
the assignment's named result consumer.

### Immediate release-edge fallback

When PM reports a control-plane refusal, PM returns the first literal blocker,
exact PR and full head, and current labels immediately. PM must not retry a
marker shape, owner tuple, projected metadata, review receipt, or alternate
control-plane command. CTO Decisions adjudicates the blocker and delegates one
safe manual edge to the named release executor in that same wake, or names the
concrete data-loss, security/privacy or irreversible harm that makes motion
unsafe. The executor fresh-fences exact head, duplicate/run and safety state.

After that fence, the named release executor has standing authority for one
smallest guarded GitHub label edge needed to emit/resume the canonical release
event. PR admission/merge use PR Merges and the two-skill degraded path above,
without a shared-contract dependency. Other release-only bypasses retain the
existing shared-contract safety rules: journal
literal pre/post labels and edge identity, preserve unrelated labels, reconcile
response loss, and stop after one edge. Exact-head CI/E2E and head-pinned merge
guards remain mandatory; no MoP or message-slot mutation is part of this
release-only edge.

## Responsiveness and delegation invariant

### Operational investigation example: #7495 (Rajiv 2026-09-08)

Bounded client-editor heap/OOM diagnosis, scheduled within existing priorities,
capacity, and access, is a CTO/PM operational call—not a product/process
approval request. P2/deferred wording or an unknown cause alone does not change
that. Escalate a proposed P1 promotion only if it reverses Rajiv's explicit
deferral or materially displaces committed work; identify the concrete tradeoff
first. Do not bundle routine investigation with that separate priority decision.
Delegate diagnosis, preserve live save-safety work, and retain investigation-only
scope. This example authorizes no implementation or priority change.
Source: Ev0C0BF5DSQ4; SOP update Ev0C07R19326, DM thread 1788847689.150449.

### Delegation-first hot path (Rajiv 2026-09-08, Ev0C03345BNH)

CTO Decisions owns intake triage and new decisions. The assignment's named
result consumer owns routine returned-evidence judgment and the final threaded
Slack reply, not a repeat investigation. Neither performs the investigation
before delegation.
This section takes precedence over later instructions to audit every wake or
execute reviews, diagnosis, or multi-step operations inside Decisions.

- Aim to answer or hand off within one minute. This is a triage target, not a
  timeout that cancels active work. Answer simple questions from available
  evidence. Consume known duplicates, superseded progress, and routine ACKs
  quietly using the existing ledger/context; do not fetch Slack/GitHub or
  inspect a task merely to reconfirm a known no-op. Preserve material new
  findings, user questions, changed authority, and real blockers.
- Delegate log consumption, code inspection, substantive review, reproduction,
  implementation, and multi-step execution immediately to the appropriate
  existing owner below under existing-owner routing. Include unresolved facts
  as questions for that owner;
  do not complete the investigation as a prerequisite to handing it off.
  Existing numbered-slot and independent-review boundaries remain unchanged.
- Carry `source_channel`, `reply_thread_ts`, `source_message_ts`,
  `source_event_id`, scope/authority, and `return_task_id` in the existing
  handoff. Use the source thread timestamp when present, otherwise the source
  message timestamp. Never infer routing from quoted messages. For non-Slack
  work explicitly mark the Slack fields absent. After the activation described
  above, new Decisions briefs use return task
  `01a0a905-0b30-7ca1-8fbd-14ae8f87282f` and the CTO Returns SOP. Until then,
  use `01a09112-a09c-7361-9a2a-0ada6a4e9dfb`. Explicit pre-cutover and DM-owned
  return destinations remain unchanged. Carry the existing executor, allowed
  continuation, holds and final boundary; the worker echoes this compact
  contract with its result, evidence and concise proposed reply. No separate
  Returns registration or context-only/ACK loop is required; task history is
  the check source. Every stop still owes a truthful
  COMPLETE, BLOCKED or STOPPED-INCOMPLETE terminal with an actual return receipt.
- Accepted delegation ends this wake. Move on
  without waiting, polling, duplicate ownership, or an unnecessary progress
  reply. The executor owns an
  already-authorized sequence through its stated terminal; routine pickup ACKs
  do not require Decisions to approve each next step. Do not interrupt current
  work or broaden authority to enforce this rule.
- On return, check correspondence to the request and make the actual CTO
  judgment. Reuse the owner's evidence; reopen investigation only for a
  concrete contradiction, material drift, or new runtime/data/security risk.
  If a fresh pre-effect check is required, the executor performs it. The named
  result consumer posts once through the guarded CTO sender to the carried Slack thread on
  completion or an actionable blocker. Progress-only returns stay quiet.

The CTO decisions task must remain continuously responsive to Rajiv and PM
decision/status messages. It is a fast verification, decision, routing, and
receipt-consumption surface. It does not execute implementation, review,
publication, installation, activation, restart, investigation, monitoring, or
waiting work.

- Any task that can outlive one bounded wake pass must be handed off immediately
  to the matching existing execution task below under existing-owner routing.
  This includes
  implementation, repair, test or review runs, CI investigation,
  browser/service configuration, publication, deployment, restart, monitoring,
  waiting, repeated verification, and follow-up.
- Existing execution tasks are reusable worker capacity, not single-purpose
  silos. Prefer the closest affinity, but they may accept other bounded work
  when the exact tuple, authority, scope, proof, rollback, and terminal return
  contract are explicit:
  - the three registered Rescues tasks above, subject to their rotation and
    capacity boundaries, own bounded exception and confirmed exact-head
    CI-failure correction work; the dedicated `hotfixes` task owns new
    hotfix assignments, including control-plane/shared-runner hotfixes. Capture
    workflow/harness/fixture-proxy/test implementation is app CI work assigned
    through PM to a numbered slot, not a Rescues or CP Repairs assignment. CI-failure
    fixes stay off-slot even when they change app/test code; ordinary planned
    product implementation and production-shaped proof still go through PM to
    a numbered slot;
  - CP Repairs task `01a08f69-45db-71c2-b433-678419139ed7` — default for
    non-hotfix bounded shared release/control-plane repair work outside MoP
    affinity, including candidate implementation and approved rollout, but
    excluding capture app-CI code in the app repository. Installed-skill edits
    (files and docs under `/Users/rajiv/.codex/skills` or
    `/Users/rajiv/.claude/skills`) are in scope here or with MoP by affinity,
    never the CTO DM lane;
  - Master of Panes implementation task
    `01a04154-c9c1-7bc1-8f7b-009a87bc7628` — sole owner for every verified
    MoP-affine repair and every PM Operator or `pm-transition` implementation,
    caller migration, installation, cutover, and retirement change;
  - PR Reviews task `01a0b53e-3316-77d3-9610-1c0c58d4ba5b` — sole independent
    functionality-review owner for immutable control-plane candidates;
  - PR Merges task `01a0324b-68e0-7491-988f-e7e1549f16f7` — named executor for
    current-main integration, CI/E2E admission, exact-run CI/E2E investigation,
    final head-pinned merge and related bounded release verification. Main
    integration is off-slot and is never assigned to a numbered product slot.
- Before handoff, verify the target does not already own conflicting work. Send
  the exact tuple once, record accepted delivery, and return to message
  handling. Do not wait for progress; completion or a typed blocker returns as
  a later wake.
- **Current-assignment updates (Rajiv Ev0C1B40M78V; clarified Ev0C20LL3YBU):**
  use the native `codex_app.send_message_to_thread` tool with the exact existing
  `threadId` and complete `prompt` for a correction, scope addendum or ownership
  fence. Omit model/effort overrides. Latest source: DM D0BPG55FG72, thread
  `1789483320.726039`, message `1789486580.112529` (2026-09-15 IST).
  Do not use stdio, `codex-ipc-send-message`, or a queued job for these updates.
  Record the native result and only the receipt fields actually returned;
  delivery is not proof of consumption or completion. Do not steer unrelated
  work, interrupt active commands, create a second executor or replay an
  accepted message. Uncertain delivery is terminal: no retry or transport
  fallback. Independent new assignments and final terminal returns retain
  their existing transport rules.
- **Native-tool boundary:** current-assignment message delivery above is the
  explicit exception to the older renderer-free/codex_app ban. Other native
  task operations remain prohibited for Decisions, including read/wait/list,
  create/fork/handoff, navigation, title, pin and archive operations. This
  exception changes message transport only, not execution or task ownership.
  Do not substitute the renderer-owner IPC CLI for the native message tool.
- For a new independent Codex-task-to-Codex-task delegation, handoff, or
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
- **Blocked-owner replacement (Rajiv 2026-08-23; existing-owner routing
  corrected by Ev0C24UF2GDQ):** a typed terminal blocker,
  `status=unavailable`, or inability to accept the exact release-critical
  tuple may justify one compatible existing replacement under the roster and
  affinity rules. Busy status alone does not require replacement; productive
  progress on the same tuple is not blockage. Preserve the complete contract,
  supersede the old route with verified readback before replacement execution,
  and never let two owners execute the same tuple concurrently. If no
  compatible existing owner can accept it, retain the contract and return the
  concrete blocker under existing incident/escalation rules. Do not fork,
  create a task, poll for capacity, or move execution into Decisions.
- Every delegated execution task must return its required terminal or typed
  blocker directly to its explicit `return_task_id` under the internal return
  split above (CTO Returns for new post-activation Decisions chains) through
  `$codex-stdio-send-message`, with its own stable event-specific dedup key.
  Every delegation prompt must state that return transport explicitly; the
  native current-assignment update exception does not apply to terminal
  returns. The delegated task must not send that result
  only to Slack or treat a PM acknowledgement as completion. Product/PR work
  may still have an explicit candidate-review boundary. Bounded control-plane
  work has exactly that boundary: the implementation owner returns a candidate,
  the named result consumer routes it once to PR Reviews, and the same implementation owner
  performs approved publication/rollout/verification before returning a
  terminal. That result consumer sends the single final PM notification.
- **Control-plane ownership invariant (Rajiv current directive):** CTO decisions
  owns intake classification, initial routing and new decisions; the named
  result consumer owns verdict consumption, authorized continuation and the
  single post-deployment PM notification. The registered dedicated `hotfixes` task
  owns new hotfix assignments; existing accepted owners retain their work.
  CP Repairs task
  `01a08f69-45db-71c2-b433-678419139ed7` owns non-hotfix shared
  release/control-plane work outside MoP affinity. Master of Panes task
  `01a04154-c9c1-7bc1-8f7b-009a87bc7628` owns MoP/PM Operator work. PR Reviews
  task `01a0b53e-3316-77d3-9610-1c0c58d4ba5b` owns the single independent
  functionality review. A negative verdict returns to the same implementation
  owner; approval returns to that owner for rollout. PM may provide the initial
  blocker/context only; it has zero review, approval, retry, or marker role.
  Never use a numbered product slot or duplicate owner for control-plane work.
- **MoP restart-after-repair invariant (Rajiv 2026-08-27):** every approved
  Master of Panes repair must restart the canonical MoP service after the
  immutable release is activated. Staging the artifact or changing the
  `current` pointer is not live proof. Before declaring rollout complete,
  the MoP implementation owner must provide timestamped readback of the new
  child PID/start time, exact release
  pointer and working directory, expected Node runtime/ABI, listener bind, and
  HTTP 200 `/health`. A rollback must atomically restore the prior release,
  restart MoP again, and pass the same bounded readiness checks. The restart
  requirement is activated only by the exact candidate approval and does not
  authorize a live-slot/nudge canary.
- **PM Operator ownership invariant (Rajiv 2026-08-27):** every future change
  to the PM Operator package/runtime, direct MoP/GitHub adapters, executable
  caller migrations, `pm-transition.sh` command arms, or final hot-path
  retirement is owned only by task `01a04154-c9c1-7bc1-8f7b-009a87bc7628`,
  created from the MoP project directory. The prior PM Operator task
  `01a03c74-fc97-7a62-bb47-001ac7fb0710` is superseded and must receive no new
  work. The Master of Panes task above is the sole implementation owner for
  these changes. It returns a candidate packet to the named result consumer,
  then performs authorized rollout only after that consumer returns the
  PR Reviews verdict, preserving separate holds and authority boundaries.
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

### Required PR Merges delegation instructions

Rajiv directive Ev0C2GQKDWJK (2026-09-18, DM thread
`1789753493.988259`, message `1789754021.283059`): every delegation to
PR Merges must explicitly include the following instructions in the delivered
brief, including current-assignment continuations and corrections. Do not rely
on the recipient remembering this SOP or an earlier message.

> Do not sleep, busy-wait, use `gh run watch`, or poll GitHub. Take only one
> bounded live snapshot at wake consumption or a terminal action. If the next
> authorized step depends on queued/running workflows, return the exact
> PR/head/run IDs, pending condition and next workflow/merge-ready event to the
> named result consumer, then END THE TURN. Preserve the unfinished assignment;
> do not claim a merge or other pending work complete. Resume on that event,
> not a timer. A generic "Continue" or residency heartbeat must not restart
> an unchanged external wait or a sleep-and-check loop. Leave actual GitHub
> runs and productive commands untouched; this is not a blanket timeout on
> tests, admission or merge commands.

A delivered external-wait disposition is a legitimate wait, not a missing
terminal requiring another reminder. Keep existing return routes and dedup
rules; this adds no monitor, timer, release gate or runtime enforcement.

## Current-main red P0 fast lane (Rajiv 2026-08-22)

A genuine required CI or E2E failure on the exact current `main` commit is a
P0. It must never wait behind the rolling PR portfolio, PM ceremony, an
existing numbered slot, or an unrelated rescue/control-plane queue.

### Direct-main hotfix scope ceiling (Rajiv 2026-09-10)

A direct hotfix on `main` must be the smallest precise change at the single
proven control point, with as few changed lines and paths as safely possible.
Do not add a helper, abstraction, framework, state, telemetry, cleanup, or
adjacent refactor unless the observed failure cannot be fixed safely in the
existing code. Add only the focused test cases needed to prove the failing
boundary and its fail-closed negatives. Every candidate and review must state
the exact path/line delta and why each changed unit is necessary. If the fix
requires broader structure or combines concerns, stop the direct-main hotfix
and place the correction in an appropriate existing PR; do not create a new
PR. Main rebinding may preserve only the identical reviewed semantic delta.

- Verify once that the failed required job is a real `push` run on the exact
  current `main` commit. A skipped/cancelled shell, stale commit, notifier
  summary, or PR-branch failure is not a current-main red.
- Route the exact main-red tuple to merge task
  `01a0324b-68e0-7491-988f-e7e1549f16f7` under existing-owner routing,
  explicitly carrying the P0 priority. Any resulting hotfix keeps the
  Rescues/hotfix roster affinity. If the exact incident cannot be accepted,
  use the blocked-owner rule without creating a task; preserve the existing
  live-incident escalation deadlines. Escalate to Rajiv only under those
  incident rules or for a genuine reserved decision.
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

## Live customer lockout escalation (Rajiv 2026-09-15, thread 1789488468.086069)

A named live customer repeatedly unable to edit or complete the core workflow
is a P0, even when local bytes are preserved and a temporary reload/export
workaround exists. "Data is safe" must never downgrade loss of working
capability.

Escalate to Rajiv immediately when recurrence, repeated lockout, or workaround
failure is confirmed; never leave the customer blocked in PM, issue,
PR-review, or CI threads while those lanes deliberate. The existing two-hour
hard escalation is an outer bound, not a target.

The escalation states the first authoritative wait timestamp and elapsed
duration, the current customer/workaround state, the exact fix/PR/head and
CI/E2E state, and one ship-or-hold recommendation.

When Rajiv authorizes an emergency merge/hotfix despite red required CI, move
the reviewed product fix immediately and repair deterministic test-only
failures separately on fresh main. Preserve the red evidence and the
rollback/canary plan; never call the workflow green.

This rule adds no service, receipt, gate, approval hop, owner class, or
automation. Delegation-first executor ownership, the emergency-red merge
rule, Slack identity/threading, and all non-conflicting SOP text are
unchanged. Where this top-level rule conflicts with later detail, this rule
governs.

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
4. Preserve single-flight. If a claimed owner is stale or blocked, use the
   blocked-owner replacement rule with a compatible existing owner. Do not
   create a task because the destination is busy or create a second
   implementation/capture/review lane for an already productive exact tuple.
5. The three-hour heartbeat retains phone escalation for a genuinely stuck P0
   that needs Rajiv's intervention. Place exactly one call for the current
   escalation state when the bounded snapshot proves any of: mitigation failed
   or customer data-loss risk resumed; the sole recovery owner disappeared; a
   promised PASS/BLOCK or other concrete continuation is at least 5 minutes
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

If capture cannot start or a capture-code defect is proven, PR Merges retains
the release tuple but pauses execution and returns the exact implementation
packet to CTO. CTO directs PM to assign that app-CI correction to one numbered
slot. The slot returns the reviewed candidate; PR Merges then resumes the same
release tuple. Never route capture-code implementation to CP Repairs or let the
release owner edit it inline.

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
2. **Capture attempt -> CI/E2E.** One duplicate-fenced canonical capture attempt
   on `H` immediately authorizes the next release boundary; its internal
   success/failure or receipt shape is not evidence. Re-run the same app-main
   fence once. Merge current main only when the bounded intervening diff is
   materially relevant under that fence; unrelated app/app-test movement and
   control-plane-only movement do not stale the capture, `ci-head`, or exact-head
   proof. After any required app-main integration and
   focused proof, trigger exactly one genuine `pull_request` CI + `E2E Smoke
   Tests` pair on the final head. Do not build, repair, consume, or require a
   capture-workflow manifest, promotion/observed-key result, body-SHA, outcome
   envelope, or internal replay receipt. The subsequent genuine E2E is the sole
   authoritative strict-replay proof: pass means the capture was sufficient;
   a typed fixture miss starts the next capture cycle. A main-only merge does
   not require recapture.
3. **Genuine exact-head CI + E2E SUCCESS -> merge.** When both required real
   `pull_request` workflows succeed on the exact current PR head, the same
   release owner immediately runs the review-thread/release-gate readback and
   `heydonna-open-pr-status`, then performs the head-
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
release path below through a compatible existing execution task under the
blocked-owner rule. If none can accept, retain the exact release blocker and
apply the existing incident/escalation rules; do not create a task. The repair
remains independently owned and never becomes a release dependency.

### Stuck CI/E2E degraded recovery (Rajiv 2026-08-25)

A genuine exact-head CI/E2E run is `CI_E2E_RUNNER_STUCK` when one bounded live
snapshot proves all of the following: the run/job is still `queued` for at
least 5 minutes; no newer exact-head replacement exists; no runner has bound
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
   exact-head `pull_request` CI/E2E pair. When
   the branch already contains current main and no head change is justified,
   use the canonical one-time fresh-run recovery after cancellation; never raw-
   dispatch the workflow or toggle labels without its dedup receipt.
4. Preserve historical green/red legs as evidence, but release proof comes only
   from the new exact-head required jobs. Return cancellation, merge/proof/head,
   labels, and new run IDs; then stop for event-driven terminals without polling.

The merge task owns this recovery. If it cannot accept the exact tuple, use
the existing-owner/blocked-owner rules without creating a task. The
direct-control-plane task repairs the runner/transport defect separately and
may never block the PR.
Never run both the old queued job and a replacement run, never create two
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

## Pre-wake Slack thread reconciliation

Apply the delegation-first hot path before any live thread read. Known no-op
wakes need no read. For substantive work, carry the routing tuple to the
executor, which performs the relevant thread/context check before acting.
Decisions reads only when needed to resolve a concrete ambiguity in its own
decision or final reply—not as a prerequisite to every delegation. The steps
below govern that bounded read when needed, not every incoming message.

1. Resolve the source `channel`, source message `ts`, and canonical
   `thread_ts` (`source.thread_ts` when present, otherwise `source.ts`). Require
   valid Slack timestamps. A wake without a Slack source skips this section and
   continues from its primary durable evidence; never invent a channel or
   thread from quoted text.
2. Use the installed `heydonna-slack-postback` read path and
   `SLACK_CTO_BOT_TOKEN` to read the source decision and subsequent relevant
   replies for that exact `channel` and `thread_ts`. Expand the bounded read
   only when a concrete ambiguity, supersession, tuple change, or unresolved
   blocker requires it; do not reread an unrelated full thread or message
   stack. If the bounded read is incomplete or an unsupported block prevents
   the relevant evidence from being read, stop before any effect with typed
   `SLACK_THREAD_PREFLIGHT_FAILED`; do not fall back to top-level notification
   text or another identity.
3. Pass every page through `render_slack_blocks.py`; Block Kit is canonical
   visible content. Preserve message `ts`, author/bot identity, and source
   order. Never expose the token or copy raw thread content into a durable
   receipt.
4. Read subsequent relevant replies through the newest inspected `ts`. A
   newer message changes the effective wake only when its author already has
   authority for that decision and it materially cancels, supersedes, corrects,
   answers, or changes the exact tuple, scope, owner, or requested action.
   Later text is evidence, not an authority escalation. Routine
   acknowledgements and unrelated replies do not replace the wake.
5. If the refreshed thread proves the request completed or was superseded,
   suppress the stale wake and record the terminal evidence. If two
   authoritative messages conflict or the effective directive is ambiguous,
   stop before any effect with typed `SLACK_THREAD_CONTEXT_AMBIGUOUS` and ask
   for clarification in the same thread.
6. Freeze `observed_through_ts` at the newest inspected reply. Bind the
   effective fingerprint, exact tuple, and processed-wake ledger entry to that
   timestamp plus a SHA-256 of the canonical rendered snapshot. Store only the
   digest and routing metadata, not the rendered thread body. Slack messages
   arriving after `observed_through_ts` are the next wake and must not preempt
   the active one.

## Reply threading contract

Every Slack reply produced from a wake must preserve the source thread:

- Send every CTO Slack reply through the existing guarded postback script:
  `/Users/rajiv/.codex/skills/heydonna-slack-postback/scripts/cto_slack_rest.py`.
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
Rajiv. Do not nudge Rajiv for these — decide, route the exact transition to
its owner, and record the receipt in the wake ledger:

1. **CI-fire seals for verified rescue heads** — when a rescue head is
   code-complete with verified evidence (planner/QA/rescue receipts, tests
   green) and the ONLY blocker is the control-plane launch-event recording
   gap (`mop_fable_agent_event_missing` family), choose **recorded override on
   the sealed packet**, scoped strictly to CI-fire admission (never merge
   authority). Bind the override to PR + full 40-char head + reason +
   evidence, then PR Merges admits real exact-head CI+E2E (test + E2E Smoke, not the
   exemption shell). PM only reports the evidence and does not arm, relabel,
   retry, or capture the PR. Always pair with the bounded REPAIR of the
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
4. **Bounded non-hotfix control-plane REPAIR** — PM reports the first literal
   blocker and exact tuple. CTO Decisions delegates needed diagnosis, adjudicates
   bounded `CONTROL_PLANE` scope, then sends one exact brief to CP Repairs task
   `01a08f69-45db-71c2-b433-678419139ed7` or, for MoP/PM Operator affinity, to
   Master of Panes task `01a04154-c9c1-7bc1-8f7b-009a87bc7628`. That task
   returns one immutable candidate to the named result consumer, which routes
   it once to PR Reviews
   task `01a0b53e-3316-77d3-9610-1c0c58d4ba5b`. A negative verdict returns
   rework to the same implementation owner; approval returns to that owner for
   authorized publication, rollout, required restart, and live verification.
   The named result consumer informs PM only after the rollout terminal.
   Do not hold a numbered slot.
5. **CTO rescue and hotfix execution** — forward the exact verified tuple once
   to one eligible registered Rescues owner for `CTO_RESCUE`/`CTO_DIRECT_RESCUE`,
   or to dedicated `hotfixes` for a new `CTO_HOTFIX`, regardless of `PRODUCT`,
   `CONTROL_PLANE` or shared-runner/auth classification. Capture app-CI code is
   excluded and routes through PM to one numbered slot. Preserve already accepted
   owners, monitors and follow-up ownership. The CTO decisions task
   does not implement, monitor, wait, or poll; accepted handoff is terminal for
   this task, and the rescue task returns a later terminal receipt or typed
   blocker. A verified exact-current-main required CI/E2E red first routes to PR
   Merges for causal disposition; a new resulting hotfix uses the dedicated
   hotfix task under the roster's capacity and ownership boundaries.
6. **Merge of merge-ready PRs** — forward the exact PR/head and complete source
   tuple once to dedicated merge task `01a0324b-68e0-7491-988f-e7e1549f16f7`.
   That task independently runs the guard and head-pinned merge or returns the
   exact blocker. The CTO decisions task never waits, polls, or runs a heartbeat.
   After accepted handoff, return. Do not run or post a portfolio snapshot in this
   task; the merge task owns the sole validated handoff after a verified successful merge.
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

### Open-PR activity backstop (Rajiv 2026-08-30)

The scheduled three-hour heartbeat is the explicit full-portfolio backstop. A
normal wake remains tuple-scoped and must not enumerate every open PR. When the
backstop runs, enumerate every open HeyDonna PR at its exact current head and
prove that each is in at least one live execution lane:

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

### Numbered-slot assignment (PM-owned execution)

Assignment is deliberately simple and is permitted for eligible issue work
under the existing priority and safety rules, including a confirmed symptom
whose cause remains unknown, or after CTO Decisions authorizes an exact
rework, reproduction, or production-shaped proof packet. PM owns the complete
slot lifecycle operation. CTO provides the exact technical packet and must not
execute any slot mutation.

**Urgent numbered-slot routing (Rajiv 2026-09-19, Ev0C2M0GGGSK; supersedes
Ev0C1BM0P20Y for urgent work):** S3-S6 are slow slots because they run on DGX
Spark. Do not assign urgent work that requires a numbered slot to S3-S6;
route it only to S1 or S2. If both are busy, PM preempts one through the
canonical stop-and-preserve flow, records the displaced tuple and continuation
boundary, reconciles/releases that slot, and assigns the urgent packet. PM
remains the sole executor of every interrupt, stop, release, clear, and
assignment. This is explicit preemption authority for urgent numbered-slot
work and supersedes generic no-preemption language only for that case. It does
not move off-slot Hotfixes or Rescues work onto a numbered slot, and it does
not discard or duplicate the displaced work. For non-urgent work, S3-S6 remain
eligible under normal affinity and capacity rules. There is no fixed ordering
between S1 and S2.

1. If the selected slot is occupied but idle, PM runs canonical
   `direct-release` end to end. Idle-occupied means available-after-release,
   never assign-in-place. A refusal or uncertain receipt stops the transition.
2. PM runs canonical `direct-assign` end to end with the complete current
   issue/PR/head/task tuple. Any assignment-time session clear is performed
   only by PM and only as permitted by that skill.
3. PM accepts assignment only from the skill's authoritative delivery/readback
   receipt and reports that receipt to CTO. CTO does not duplicate the send,
   labels, release, clear, or assignment.

If either canonical skill is unavailable or refuses, PM returns its first typed
blocker. Neither PM nor CTO falls back to raw MoP endpoints, direct pane input,
manual slot labels, a second send, or another slot authority.

### Stuck numbered-slot -> rescue-lane transfer (Rajiv 2026-08-25)

A numbered slot is stuck when its product work cannot advance because the slot's
model, launcher, tool transport, host, or control-plane runtime is failing. MoP
`active` is not authoritative when the live pane shows only terminal startup or
transport errors. A stuck slot may not retain a product PR while eligible work
waits.

- Preserve the exact checkout, dirty/unpushed work, packet identity, and causal
  proof. Transfer the PR/fix continuation once to the existing rescues task;
  reproduction already completed in the slot must not be repeated.
- After durable rescue acceptance, CTO Decisions owns the rescue decision and
  exact routing packet. PM alone executes any resulting numbered-slot release
  or subsequent CTO-authorized rework/repro assignment. Do not release before
  the rescue owner has the complete state handoff.
- The old slot stops work after handoff. Never run slot and rescue implementations
  concurrently, redeliver the packet, or discard dirty state. A true repro that
  still requires slot-local Modal/Convex execution stays in a healthy slot; the
  implementation/fix moves to rescues once the causal evidence is sealed.
- A terminal startup/model error must mark the turn inactive/blocked so heartbeat
  and capacity reconciliation can see the limbo; it must never remain falsely
  `active` because a retry loop is still emitting the same terminal error.

### Main integration and CI-failure fixes are off-slot; E2E execution may require a numbered slot (Rajiv 2026-09-08, Ev0C0BELEZ0A)

Main integration, CI correction, and E2E execution have distinct ownership.
Merging current main into an open PR branch is release integration owned by the
PR Merges task and stays off-slot. Once an exact-head CI failure has a bounded
causal correction contract, the Rescues task owns that smallest fix and its
focused unit/static proof off-slot; do not send the fix to PM or reserve a
numbered slot for it. E2E failed-job log and retained-artifact consumption,
static analysis, and causal classification may also happen off-slot. A
numbered slot is required only when work must actually execute a
production-shaped E2E reproduction, live runtime interaction, strict replay,
or final focused E2E proof that cannot be completed by the release/rescue
owner's bounded environment.

- Route every required branch refresh/current-main integration to PR Merges.
  It fresh-fences the PR/head/base, performs the smallest non-force merge and
  conflict-sensitive proof, and returns the descendant. PM and numbered slots
  do not perform main integration.
- Route a confirmed exact-head CI code/test correction to Rescues with the
  failed-run tuple, causal control point, smallest correction contract,
  focused proof, rollback, and return task. Preserve any valid green workflow
  leg and do not blind-rerun. The correction remains off-slot through its
  immutable terminal.
- Route exact-head E2E logs and retained artifacts for one bounded off-slot
  analysis. If the evidence proves a CI-side correction as well, Rescues owns
  that correction off-slot. Any actual E2E reproduction, replay, or final
  production-shaped proof still requiring the app runtime must run in a
  numbered slot.
- For a mixed CI/E2E red, split the boundaries without creating competing
  owners: PR Merges owns main integration, Rescues owns the confirmed CI
  correction, and PM assigns a numbered slot only for the remaining executable
  E2E reproduction/proof packet. Never implement the same fix in both places.
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
5. For every capacity-blocking defect, record the post-repair replay now and
   direct PM to execute it. PM re-reads the live slot state, runs canonical
   `direct-release` once when needed, reconciles capacity once, and runs
   canonical `direct-assign` for the highest-priority eligible packet. CTO does
   not perform the slot mutations. Never preempt productive work and never
   reserve a numbered slot for the repair itself.
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

1. Apply the delegation-first hot path: classify known no-ops from available
   context before any live read. Resolve the source routing tuple for material
   work and delegate promptly; use pre-wake thread reconciliation only when
   needed as specified above.
2. Deduplicate the effective fingerprint against the durable processed-wake
   ledger. A changed PR head, slot epoch, run attempt, materially changed
   repair, authorized Slack directive, or product decision contract is a new
   tuple. A routine acknowledgement, unrelated reply, control-plane replay, or
   candidate count is not a new decision ceremony.
3. The execution owner reads the smallest authoritative live evidence once
   before acting; Decisions does not duplicate this work. For slot wakes, read the
   exact numbered-slot log first, then MoP, checkout, process/status, GitHub,
   and the newest PM records. For PR wakes, take one bounded GitHub snapshot;
   never poll for a state change.
4. Suppress or replace stale wakes. Never forward a monitor correction
   verbatim after the tuple changes.
5. Resolve `action_kind`, `required_skill`, and `authority` through the matrix
   below. The executor reads the selected execution skill completely before
   acting; Decisions reads only the routing contract it needs. Normalize any PM
   message whose visible Slack body declares a specific PR `MERGE READY` or
   `merge-ready` with an exact head to `MERGE_READY` or
   `MERGE_READY_INVALID`, even when the monitor envelope says
   `CTO_DECISION_CONSUMPTION`.
6. If authority is `EXECUTE_NOW`, delegate now to the named executor. Send the
   complete exact tuple once; accepted delivery is the terminal action for
   this CTO Decisions wake, not completion of the execution. If the destination
   cannot accept the tuple, apply the existing affinity/capacity routing rule;
   never use inline execution as a fallback. Keep active work with its accepted
   owner and steer updates to that assignment instead of queueing another job.
   Never wait for the delegated task, poll its progress, poll GitHub, or install a
   heartbeat.
   If authority is `RAJIV_DECISION`, present the verified decision with options,
   recommendation, consequences, and exact closure evidence to Rajiv in DM
   `D0BPG55FG72` with `<@UEQTTB97A>` and the source `thread_ts`. If it is
   `PM_CORRECTION`, send one exact correction to PM in `#heydonna-dev` only.
7. For the inline allowlist, verify the decision/communication receipt. For
   delegated execution, verify only exact-tuple handoff acceptance and return;
   its terminal result is consumed as a later wake. A label, plan,
   acknowledgement, command launch, packet path, or prose receipt is not
   completion of the delegated execution itself.
8. After every non-duplicate normalized `MERGE_READY` wake, forward the exact
   tuple to PR Merges; only that executor runs `heydonna-open-pr-status` and its
   pre-effect checks. Do not take or post a portfolio snapshot. For
   `MERGE_READY_INVALID`, post only the exact correction and stop. The dedicated merge task emits the
   sole bounded validated handoff after it verifies a successful merge. Never
   wait for the merge task, poll GitHub, use `gh run watch`, or create a
   heartbeat/automation.
9. Post at most one concise outcome to `#heydonna-dev` when PM coordination is
   required. Never post to `#heydonna-pm` or `#heydonna-feedback`.
10. Record fingerprint, verified tuple, action, skill, authority, result,
   `observed_through_ts` and snapshot digest when Slack-backed, and closure
   evidence in the processed-wake ledger. Record the healthy
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
CI and E2E failures use the decision matrix below. A concrete CI test failure
goes directly to one eligible registered Rescues task; PR Merges does not
investigate it. PR Merges always receives an E2E exact run, and PM launches an
independent investigator for E2E failures only. CI correction and fresh
workflow recovery stay off-slot with Rescues; candidate-owned E2E
reproduction/rework may use a numbered slot after the two investigations
converge. A causal failure
shared across PRs or `main` is one Hotfixes incident, not per-PR rework.
A busy destination does not authorize task creation or inline Decisions
execution. The accepting existing task remains the single accountable owner
through the terminal receipt. Do not send CI/capture alert
ownership to the standing rescues task by default when merge can accept it.

### CI/E2E failure decision matrix (Rajiv 2026-09-16, Ev0C27FSUX1C)

This supersedes the 2026-09-15 PM stand-down rule and any combined CI/E2E
investigation workflow.

**CI test failures are direct Rescues work and stay off-slot.** When the exact
run names a concrete failing test, Decisions sends the bound
PR/head/run/attempt/job/test tuple directly to one eligible registered Rescues
task. No preliminary PR Merges or PM investigation is required. Rescues owns
the smallest correction, the entire canonical local suite on final bytes with
zero skipped/deselected/filtered/quarantined/unexecuted phases, non-force
publication, duplicate/capacity checks, and the one fresh exact-head required
workflow trigger. Preserve unrelated work and return COMPLETE or BLOCKED to
CTO Returns. A non-test CI infrastructure/control-plane failure remains with
PR Merges for bounded disposition; one unchanged-head retry is allowed only
after its concrete health condition clears and no duplicate run is active.
Never assign a numbered slot for CI correction or recovery.

**E2E failures require two independent investigations.** Decisions routes the
exact tuple to PR Merges and PM launches one independent read-only investigator
for the same tuple. They do not share hypotheses, reuse each other's report as
their investigation, or create a third investigator. Each binds the exact
PR/head/run/attempt/job and returns the first causal boundary. PR Merges returns
directly to CTO Returns. If PM's independent finding arrives through Slack,
Decisions steers it once to Returns for that bound chain without adjudicating
the same result separately. Both findings
must converge in the original failure Slack thread on the classification,
concrete next action, and one execution owner before mutation. If they differ,
CTO Returns adjudicates there within standing policy; only a concrete unresolved
safety/authority conflict or reserved change goes to Decisions.

**Mandatory PM reminder on every E2E failure alert.** In the same source-thread
response that records the PR Merges handoff, Decisions must explicitly mention
PM and instruct PM to launch the independent read-only investigator on the
exact PR/head/run/attempt/job tuple. Do not rely on this SOP being implicit and
do not post only the PR Merges status. The wake is not fully consumed until the
PM instruction is posted and the processed-wake ledger records that instruction.
Require PM to return the actual investigator launch/delivery receipt, not an
ACK or plan. If the launch receipt is absent on the next wake, correct the
missing PM arm immediately; do not reroute or duplicate the PR Merges arm.
After one verified PM launch, do not remind again for that exact tuple.

Use this E2E outcome matrix:

- confirmed infrastructure/platform transient: one bounded unchanged-head
  rerun after the condition clears and no duplicate run is active;
- HTTP 424 or verified `fixture_miss`: canonical exact-head capture, followed
  by one fresh exact-head pair;
- candidate-owned regression: PM assigns one numbered slot for the exact repro
  and smallest rework, followed by one fresh exact-head pair;
- workflow/config-only E2E defect: route the applicable off-slot Rescues owner;
- the same causal failure across PRs or `main`: route one dedicated Hotfixes
  owner and integrate the corrected main into affected PRs; never patch or
  rerun every PR independently.

The raw alert is recorded and deduplicated before delegation. Investigation
acceptance alone authorizes no rerun, capture, edit, slot assignment, or merge.
An earlier completed exact-run terminal may be reused by that same investigator
without refetching evidence, but it does not replace the other independent E2E
investigation.

**No failure-thread black holes.** Every originating CI or E2E Slack thread
must receive a terminal plan containing the exact run/head, classification,
concrete next action, named owner, and closure condition. `shared`,
`investigating`, `accepted`, a label, or a handoff receipt is not a terminal
plan. Record the same owner/action/closure tuple in the processed-wake ledger.
Exact-current-`main` red retains the separate P0 fast lane below.

An affected-test proof file or receipt is never a prerequisite for an
authorized fresh CI/E2E recovery. Exact PR/head binding, the prior terminal,
absence of a duplicate active run, and the workflow eligibility condition are
the complete recovery gate. Remove or bypass any control-plane consumer that
still demands `affected-test-proof-*` before triggering the one fresh
exact-head CI/E2E pair.

1. **Green CI/E2E:** when both real required `pull_request` workflows—`CI` and
   `E2E Smoke Tests`—are successful on the exact current PR head, route that
   success notification promptly and once to merge task
   `01a0324b-68e0-7491-988f-e7e1549f16f7`, carrying the source
   channel/thread/event and PR/head/run/attempt tuple. Decisions need not wait
 for a separate PM merge-ready message or perform a local release
   investigation. The self-contained `heydonna-open-pr-status` performs the required review-thread, product, and release-gate checks; the merge task
and merges head-pinned; an incoming green notification is only a routing
   signal, not merge authority. If an existing rescues or release owner already
   owns an active end-to-end release, merge reuses and coordinates that owner
   rather than starting a concurrent merge executor. One green leg alone,
   dummy/generic contexts, stale runs, or skipped shells are not merge
   authority.
2. **Red CI:** when a concrete failing test is named, send the exact tuple
   directly to one eligible Rescues owner. It fixes the failing tests, runs the
   entire canonical suite on final bytes, publishes non-force, and triggers the
   one fresh exact-head required workflow recovery. No investigation hop and no
   numbered slot. Non-test CI infrastructure reds stay with PR Merges.
3. **Red E2E:** send the exact run to PR Merges and require PM's independent
   exact-tuple investigation. Do not mutate until both reports converge in the
   originating alert thread on one classification, next action and owner. Use
   the matrix above; do not add a third reviewer or investigator.
4. For either workflow, never rerun before causal confirmation, merge red, or
   infer cause from notifier prose alone. Exact-current-`main` red continues to
   use the P0 fast lane above. A nonterminal run trapped in a proven runner/JIT
   binding loop is not a red-workflow investigation; use the
   `CI_E2E_RUNNER_STUCK` degraded recovery above immediately and repair the
   control plane separately.
5. **Completed capture attempt:** do not classify the capture as green or red
   from internal receipts. The same release owner merges exact current main
   non-force when needed, runs the smallest conflict-sensitive focused proof,
   and triggers exactly one real `pull_request` `CI` + `E2E Smoke Tests` pair
   on the resulting head. That genuine exact-head strict-fixture E2E is the only
   capture verdict. Investigate capture machinery only when the capture cannot
   start or a concrete safety boundary fails, never because a receipt is absent
   or malformed.

Post only the material action or typed blocker in the originating Slack thread.
Record the verified tuple, owner, action, and closure condition in the durable
processed-wake ledger.

### Capture-harness changes are app CI slot work (Rajiv 2026-08-24)

- A change to a capture workflow, capture profile or selector, E2E capture spec,
  capture fixture/seed, strict-replay harness, or promotion/readability assertion
  is app CI/test-harness work, not `control_plane_only` work.
- Do not open, review, publish, or hold a release for a change whose only outcome
  is a capture manifest, key list, promotion/readback receipt, outcome envelope,
  or internal replay receipt. Exact-head strict-fixture E2E supersedes those
  layers.
- Reproduction, implementation, and focused proof for such a change require a
  numbered slot. Never route a capture-harness fix to the dedicated
  direct-control-plane repair task.
- Reserve `control_plane_only` for orchestration outside the app CI harness—such
  as admission state, receipts, budgets, serialization, or deployment wrappers—
  that does not change which app/E2E producer runs or what that producer proves.
- After the slot returns a new exact head and focused proof, CTO/release
  ownership applies the normal functionality-review, admission, capture, and
  strict-replay sequence. PM only returns the slot packet and does not execute
  those PR transitions.

## CTO PR admission and stale `ci-head` recovery (Rajiv 2026-08-24)

PM failure reporting and CTO-authorized rework/repro slot assignment are the
only PM operational responsibilities on an open PR. A CTO-directed PR review,
release, or CI admission must not invoke `pm-transition`, Family-2, numbered-slot
ownership, MoP assignment, or a PM parked-target/assignment-owner check. Those
PM records cannot block or authorize a CTO action.

PR Merges `01a0324b-68e0-7491-988f-e7e1549f16f7` owns every PR admission;
Decisions only routes and adjudicates. Retain existing exact-head review and
waiver requirements; this adds no review prerequisite. After reverifying OPEN, non-draft,
exact head, mergeability, required product/capture/review/visual-QA/privacy/
release gates, and absence of a genuine active exact-head pair, PR-merges may
directly reconcile only the admission labels: clear stale superseded
`pm-state:blocked-rework` plus its stale `pm-blocked:*` companion when the CTO
review expressly found no current product/runtime/data/security risk, set the
valid CI-admission state, replace a stale `ci-head:<old-sha>` with the full
current head, and let the sanctioned label gate emit exactly one genuine CI +
E2E pair. This is the normal CTO action boundary, not a degraded PM transition or
a control-plane repair.

Ordinary open-PR admission uses the same PR Merges executor and the canonical
two-skill path; it is not an exception permitting Decisions to act inline.
Never use a PM report or slot assignment to clear a real product blocker, unresolved review
risk, capture requirement, visual-QA/privacy/release hold, or an active
exact-head workflow. PM does not mutate PR labels/state, dispatch workflows,
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

Execution cells below are instructions for the named delegate. `EXECUTE_NOW`
does not expand the Decisions inline allowlist. Current-assignment updates use
steering even where an older example names queue-only delivery; independent
assignments and final returns retain their existing transport.

**Installed-skill changes route to control-plane owners (Rajiv 2026-09-18,
Ev0C2Y9BSY82; thread 1789755030.344279):** skill files under
`/Users/rajiv/.codex/skills` or `/Users/rajiv/.claude/skills`, including their
skill docs and CHECK/reference text, are control-plane work. Route them to CP
Repairs task `01a08f69-45db-71c2-b433-678419139ed7` or MoP task
`01a04154-c9c1-7bc1-8f7b-009a87bc7628` by affinity, never the CTO DM lane. A
skill edit already made on the CTO DM lane is reported for reconciliation
through the CP owner, not reverted unilaterally.

| Verified wake | action_kind | required skill/action | Authority and terminal behavior |
| --- | --- | --- | --- |
| Scribie user requests HeyDonna access or a signup link (Rajiv Ev0C1CU8SRNJ, DM thread 1789242945.681389, message 1789243066.821799) | `SCRIBIE_ACCOUNT_CONNECTION` | Existing PM/support owner guides the supported Scribie account connection flow | Scribie users do not need an invite; connect their existing Scribie account with HeyDonna. Do not request invite approval, issue a separate invite, or manually create a Clerk account for this path. If Scribie status is unclear, confirm the existing Scribie account rather than infer it from email. Preserve supported account/access verification and non-Scribie onboarding policy; do not open public signup or change provider configuration. Return a real connection blocker through the same owner; guidance delivery is not connection success. |
| Already-reviewed bounded control-plane increment with established scoped direct-main authority (PR #7720 requester `7b800440`; Rajiv correction Ev0C14B75SLW, SOP update Ev0C1A0J0F36, DM thread 1789120012.261569) | `REVIEWED_CONTROL_PLANE_MAIN_PUBLICATION` | CTO adjudicates; continue the same CP Repairs owner via queue-only `codex-stdio-send-message`, without preemption or repeated unchanged-code review | `EXECUTE_NOW` within the existing authority. **Negative example:** asking Rajiv again solely because publication targets main, conflating control-plane repair with an app/product release exception. Preserve fresh exact-main and semantic/stable-patch equivalence, focused proof, non-force publication and ordinary-revert rollback. Main pushes can deploy production: check same-ref deployment state and automatic rollout; stop if publication would cancel an active deployment or cause an unsafe Convex effect. Name that concrete blocker; escalate only a genuinely new reserved exception. No arbitrary main-write authority, scope expansion or waiver of required CI/E2E/product-release gates. Publication does not authorize capture dispatch or changing the global capture lane. Historical example only: retain the existing owner and dedup; queue acceptance is not publication, and this row must not replay work. |
| Existing supported one-file production metadata recovery plus one server-side format retry (Scribie `jn762zksmav3`, #7690 recurrence; Rajiv correction Ev0C202VQY80, SOP update Ev0C0QC7M5C7, DM thread 1789120012.261569) | `ROUTINE_INCIDENT_RECOVERY` | CTO operational decision; delegate once through `codex-stdio-send-message` to the existing recovery owner (PR Merges for this incident) | `EXECUTE_NOW` under existing access, policy and budget. **Negative example:** asking Rajiv to authorize this solely because it writes production metadata or consumes a bounded format/LLM cycle. Neither alone is a product/process change. The executor fresh-checks target/owner/source, customer edits, active writers and duplicate recovery; preserves prior metadata and original/later versions; adopts equivalent work and never blindly retries uncertain writes. Stop on a concrete safety conflict; escalate only an actual reserved scope, policy, access, material-cost or high-risk change. No fabricated verification, forced readiness or additional audio work. Report queued ownership, server-side retry dispatch and readiness separately; do not ask the customer to click Retry. Historical example only: no replay or second owner. |
| Rank eligible customer backlog under existing priorities (Abilaasha: Judith/ACR, #6529, #6693, Santosh; Rajiv correction Ev0C0X7AJ9D2, SOP update Ev0C0MAL5Q2F, DM thread 1789015501.023989) | `ROUTINE_CUSTOMER_PRIORITY` | CTO/PM operational judgment; existing PM owner schedules eligible work | `EXECUTE_NOW` without Rajiv confirmation. Use customer impact, urgency, and age; ranking alone is not a product/process change. Preserve active data-loss work; this example does not freeze the historical order or authorize preemption/reassignment. Escalate only a concrete reserved tradeoff such as policy change, material added cost, reversal of Rajiv's explicit direction, or material displacement of committed work. Name that effect, not a blanket queue-approval hold. |
| Exact-head merge-ready handoff with all gates satisfied | `MERGE_READY` | `heydonna-open-pr-status` | `EXECUTE_NOW`. Forward the exact tuple once to dedicated merge task `01a0324b-68e0-7491-988f-e7e1549f16f7`; do not merge, wait, or post a portfolio snapshot in this task. The merge task performs exactly one validated handoff only after verified merge success. |
| Premature or contradictory merge-ready claim | `MERGE_READY_INVALID` | `heydonna-open-pr-status` | `PM_CORRECTION`. Name the single missing or stale gate and require canonical promotion through the self-contained merge skill; a genuine pending-review contradiction is resolved via the dedicated `CTO_PR_REVIEW` row, not as a merge prerequisite. |
| Exact-head CTO review requested | `CTO_PR_REVIEW` | delegate to existing PR Reviews task with source Slack routing | Send once and return; the reviewer uses `heydonna-pr-review` and returns its verdict/evidence/proposed reply to the explicit result consumer, which consumes it without repeating review and posts in the original thread. |
| CTO-routed bounded PR rescue requested after a verified review/rework cap | `CTO_RESCUE` | hand off to one eligible Rescues task from the roster above, which uses `heydonna-cto-rescue-pr` | `EXECUTE_NOW` only when the durable handoff assigns the exact PR/head and bounded contract to CTO rescue. Forward once and return; the rescues task sends the digest-bound patch/evidence packet to the explicit result consumer under the internal return split for independent review, without pushing, merging, or posting it directly to PM. PM may assign a numbered slot only when CTO explicitly routes a rework/repro packet. |
| Rajiv explicitly authorizes Codex to repair and push one exact PR head | `CTO_DIRECT_RESCUE` | hand off to one eligible Rescues task from the roster above, which uses `heydonna-cto-direct-rescue-pr` | `EXECUTE_NOW`. Forward the exact source tuple once and return. The rescues task preserves the exact source head, uses a detached worktree, pushes non-force, and returns the exact new head and proof. Direct rescue is never inferred from `CTO_RESCUE_REQUIRED` alone. |
| Rajiv explicitly authorizes an urgent P0 ship/hotfix | `CTO_HOTFIX` | hand off to dedicated `hotfixes` from the roster above, preserving any existing accepted owner, which uses `heydonna-cto-hotfix` | `EXECUTE_NOW`. Forward once and return; the hotfix owner stays within the named incident and release boundary and must not broaden the hotfix. Runs the entire canonical CI suite locally, no focused-filter substitute, per `heydonna-cto-hotfix`. |
| Genuine required CI or E2E failure on the exact current `main` commit | `MAIN_RED_P0` | PR Merges performs the bounded causal disposition; new hotfix implementation/publication uses dedicated `hotfixes` under the roster's capacity and ownership boundaries | `EXECUTE_NOW` under Rajiv's standing 2026-08-22 authority. Verify the exact push run/job once and route the causal investigation to PR Merges. If a hotfix is proven, hand the exact correction once to the selected hotfix owner using the applicable new-assignment or steered-update transport; never retain it in PR Merges or send it to CP Repairs. The accepting hotfix owner owns the smallest fix, focused proof, safe direct publication, and event-driven replacement CI/E2E. A confirmed infrastructure failure may receive one unchanged-head retry only after causal proof, duplicate-active check, and the concrete health/eligibility condition clears. Use existing-owner routing for busy destinations and the blocked-owner rule when the exact incident cannot be accepted; never create a task or move execution into Decisions. Preserve live-incident escalation deadlines and escalate to Rajiv only under those rules or for a genuine reserved decision. No PM/slot ceremony, pre-classification rerun, polling, or unrelated scope. |
| Issue-only rescue/rescope circuit breaker with no exact PR patch | `CTO_ISSUE_RESCUE` | decision analysis; use rescue skill only after an exact patch exists | Usually `RAJIV_DECISION`. Verify the issue, original directive, failure mode, and artifacts; recommend `final_verified_patch`, `split_and_reimplement`, or `override_with_evidence`. After Rajiv chooses, require the canonical typed rescope transition and successor issue when applicable. |
| Product, architecture, privacy, data-model, UX, or release-policy decision | `CTO_PRODUCT_DECISION` | product/architecture decision analysis | `RAJIV_DECISION` unless Rajiv's choice is already durable. State intended capability, production failure mode, runtime control point, options, recommendation, rollout/rollback, acceptance criteria, and downstream PM transition. Never treat this as a routine operational correction. |
| CTO product decision already made but not consumed | `CTO_DECISION_CONSUMPTION` | verify decision, then exact PM transition | `PM_CORRECTION`. Quote the durable choice and require one canonical consumption transition; do not reopen the decision. |
| Any verified bounded non-hotfix control-plane defect or stabilization | `CONTROL_PLANE_REPAIR` | PM reports the first literal blocker/tuple to CTO; CTO delegates needed diagnosis and sends one exact brief to CP Repairs task `01a08f69-45db-71c2-b433-678419139ed7`, or to MoP task `01a04154-c9c1-7bc1-8f7b-009a87bc7628` when the change is MoP-affine, under existing-owner routing | `EXECUTE_NOW` after verifying bounded non-product, non-hotfix scope and no duplicate owner. Capture workflow/harness/fixture-proxy/test code in the app repository is explicitly excluded: classify it as app CI and assign it through PM to a numbered slot. New hotfixes use dedicated `hotfixes` even when classified `CONTROL_PLANE` or shared runner/auth; preserve existing accepted owners. The implementation owner returns a candidate; CTO routes it once to PR Reviews task `01a0b53e-3316-77d3-9610-1c0c58d4ba5b`; a negative verdict returns rework to the same owner and approval returns rollout to that owner. CTO only consumes terminals and notifies PM. Never use a numbered slot for actual control-plane work. |
| PM Operator, direct MoP/GitHub command adapter, executable caller cutover, or `pm-transition` retirement change | `PM_OPERATOR_CUTOVER` | CTO sends one exact brief to MoP task `01a04154-c9c1-7bc1-8f7b-009a87bc7628` | `EXECUTE_NOW` under Rajiv's current directive. MoP prepares and returns one candidate for PR Reviews. After CTO returns an approval, MoP publishes, rolls out, verifies, and returns the terminal. Preserve the exact baseline, migrate one reachable family at a time, and never route this work to PM or a numbered slot. |
| Legacy or explicit `CTO_DIRECT_CONTROL_PLANE_REPAIR` label for a bounded control-plane defect | `CTO_DIRECT_CONTROL_PLANE_REPAIR` | normalize to the same delegated affinity-owner and PR Reviews contract above | `EXECUTE_NOW`; the selected implementation owner prepares one candidate, PR Reviews supplies the single verdict, and the same implementation owner performs approved rollout. CTO Decisions only routes and consumes terminals. |
| Numbered slot held by a control-plane repair (occupied-inactive or free-standby, off-slot repair running) | `CONTROL_PLANE_REPAIR_SLOT_HOLD` | exact-slot verification; CTO decides the safe technical boundary and directs PM | `PM_CORRECTION`. PM alone executes canonical `direct-release`/`direct-assign` and returns the authoritative receipt. CTO never uses raw MoP, labels, or pane delivery for numbered-slot lifecycle. Stop on the first typed blocker and preserve substantive work. |
| Slot/PR ownership or transition mismatch | `SLOT_TRANSITION` | exact-slot verification; CTO selects the safe technical boundary and sends one exact instruction to PM | Usually `PM_CORRECTION`. Preserve productive work. PM alone executes any numbered-slot release, clear, rescue handoff mechanics, or assignment through the canonical skills; CTO does not mutate the slot. |
| Occupied slot reports repeated same-head "in progress"/LOCAL_CONTINUE with no commit (PM JSONL slot deliveries + MoP nudge corroboration) | `SLOT_FALSE_PROGRESS` | verify the exact slot log tail, live MoP row/epoch, and checkout head; confirm no active canonical assignment/reconcile command | `PM_CORRECTION`. CTO chooses and communicates the exact terminalization/rescue/rework decision; PM alone performs any numbered-slot release or reassignment and returns the canonical receipt. |
| PM checkout is on a feature branch instead of main (PM JSONL/logs show the PM clone at `fix/...` while doing PM-owned work) | `PM_CHECKOUT_BRANCH` | verify PM clone branch: `git -C /Users/rajiv/Downloads/projects/heydonna-app rev-parse --abbrev-ref HEAD`; exclude disposable worktrees and slot clones | `PM_CORRECTION`. Remind PM to return to main: `git checkout main && git pull --ff-only origin main`, then rerun the affected-test/review through the dedicated background agent or a disposable worktree / the owning slot's clone. |
| Save-suppression production-debug heartbeat while the incident is open | `SAVE_SUPPRESSION_PROD_DEBUG` | consume one exact window/receipt; continue the existing rescues/hotfix owner through the emission, transport, query, and durable-transition boundary | `EXECUTE_NOW` for every material delta. A second consecutive nonzero packet with no new causal fields is `SAVE_SUPPRESSION_TELEMETRY_EMIT_GAP` and must be returned to the same owner immediately for the smallest main observability correction or typed external blocker. New escape/error/loss signatures are immediately material. Never create a second investigator, poll Axiom, act on customer files, infer loss from counts, or close on a clean/quiet window. Closure requires build-bound production evidence joining suppression to handoff/sweep/admission after the deployed fix. |
| Three-hour heartbeat proves an open PR has no genuine exact-head capture, CI/E2E, or active numbered reproduction/integration proof | `OPEN_PR_ACTIVITY_GAP` | consume one bounded PR/head/run/job and MoP owner snapshot, then delegate the safe next edge | `EXECUTE_NOW`. Decisions makes the technical release judgment; PR Merges executes CI/E2E admission and the assigned release owner executes capture. If the missing boundary requires production-shaped reproduction/integration proof, CTO sends PM one exact rework/repro packet; PM alone releases/assigns the eligible numbered slot without duplicating a live owner. If a job has remained queued with zero steps/no runner for at least 5 minutes, CTO delegates the equivalent capture recovery to the release owner. A concrete product/security/data-safety blocker may remain held only with its exact evidence, owner, and executable wake condition; passive dependency labels or process defects are not a fourth lane. Post the material action or typed blocker to the originating PM/heartbeat thread, record it once, and return without polling. |
| Raw terminal-bad PR CI alert with a concrete failing test | `CI_TEST_FAILURE_DIRECT_RESCUE` | delegate the exact PR/head/run/attempt/job/test tuple once to one eligible registered Rescues task | `EXECUTE_NOW` means direct off-slot correction, not investigation. Rescues fixes the failing tests, runs the entire canonical suite on final bytes with no skipped/deselected/filtered/quarantined/unexecuted phase, publishes non-force, performs duplicate/capacity checks, and triggers one fresh exact-head required workflow recovery. Return COMPLETE or BLOCKED to CTO Returns. No PM investigator, numbered slot, or PR Merges investigation. |
| Raw terminal-bad PR CI alert without a concrete test failure (infra/control-plane/unknown) | `CI_INFRA_FAILURE_REPORT_PENDING` | delegate once to PR Merges `01a0324b-68e0-7491-988f-e7e1549f16f7`; reuse any accepted exact-run owner | `EXECUTE_NOW` means bounded off-slot infrastructure disposition only. Record/deduplicate PR/head/run/attempt. No numbered slot for the investigation. Confirmed infra may receive one bounded retry after health clears; a proven ordinary code/test correction transfers once to Rescues, while a proven capture app-CI correction transfers through PM to one numbered slot. |
| Raw terminal-bad PR E2E alert without two completed findings | `E2E_FAILURE_REPORT_PENDING` | delegate once to PR Merges and instruct PM to launch one independent exact-tuple investigator | `EXECUTE_NOW` means two independent read-only investigations only. No mutation until both findings converge in the originating alert thread. Then use infra-rerun / 424-capture / candidate-slot-rework / capture-code-slot-rework / shared-runner-hotfix routing. |
| Completed direct Rescues CI-test correction | `CI_TEST_FAILURE_RECOVERY_COMPLETE` | CTO Returns checks the final head, full-suite receipt and fresh workflow trigger | `EXECUTE_NOW`. Do not add an investigation or numbered-slot hop. Preserve exact-head and single-flight; PR Merges resumes only for final merge or a later non-test release boundary. |
| Completed independent PM and PR Merges E2E investigations | `E2E_FAILURE_REPORT_COMPLETE` | CTO Returns compares the two findings and adjudicates one plan/owner in the original failure thread | `EXECUTE_NOW`. No routine Decisions round trip and no third investigation. Confirmed infra gets one bounded retry; HTTP 424/fixture_miss gets exact-head capture; candidate regression or capture app-CI defect gets numbered-slot repro/rework; a shared runner/auth cause gets one Hotfixes owner. Preserve single-flight and post the closure condition. |
| Exact-head CI/E2E queued at least 5m with no runner binding and a proven repeated runner/JIT control-plane loop | `CI_E2E_RUNNER_STUCK` | merge task executes the stuck-run degraded recovery; rescues is the one fallback owner | `EXECUTE_NOW`. Cancel only the proven stuck queued run, merge current main non-force when behind, run focused conflict proof, push a descendant, and emit one exact-head CI/E2E pair. If already current-main-bound, use the canonical one-time fresh-run recovery. Repair the runner defect separately; never wait for that repair, raw-dispatch, duplicate the old job, or create a second release owner. |
| PM reply to a CTO/decisions thread | `PM_REPLY` | triage from available context | Routine ACK/progress is consumed quietly without a live audit. Delegate material verification or execution; Decisions retains judgment and the final source-thread reply. |
| Dropped or unroutable communication | `TRANSPORT_FAILURE` | verify alternate durable delivery | `PM_CORRECTION` only if still unhandled; otherwise close as duplicate delivery. |
| Stale, duplicate, healthy in-flight, or disproved candidate | `IGNORE` | none | Record suppression; send no PM chatter. |

## Exact-head remote-capture rule for fixture misses

A verified strict-replay fixture miss — including an HTTP 424
`fixture_miss` or independently proven absent canonical request key — is
resolved only by a remote capture on the exact current PR head that produced
the miss.

- The CTO rescue owner runs the canonical remote-capture workflow on that PR
  head. PM does not trigger the capture or the subsequent CI/E2E admission.
- Fail closed on head drift. Capture on `main`, a different PR head, a local
  checkout only, or a stale merge ref does not satisfy the missing-key tuple.
- A remote capture attempt is preparation, not evidence. Do not require or
  consume a capture-success terminal, manifest, observed-key list,
  promotion/readability receipt, body hash, outcome envelope, or
  capture-workflow replay receipt.
- After the capture attempt, the CTO release owner merges exact current main
  non-force when needed, preserves both contracts with focused proof, and fires
  exactly one real exact-head `pull_request` CI + E2E pair on the resulting
  descendant head. The genuine E2E strict replay is the sole capture verdict.
  If it passes, the capture was sufficient regardless of internal capture
  status; if it reports a typed fixture miss, perform the next bounded capture
  cycle. A current-main-only merge after the capture attempt does not require
  recapture.

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

- `CTO_RESCUE` is a bounded, CTO-routed rescue that returns a patch and evidence
  to CTO decisions task `01a09112-a09c-7361-9a2a-0ada6a4e9dfb` for independent
  review without pushing or replying directly to PM.
- `CTO_DIRECT_RESCUE` may push only after explicit Rajiv authorization for the
  exact PR/head.
- `CTO_HOTFIX` is reserved for explicitly authorized urgent release work.
- `CTO_ISSUE_RESCUE` is a rescope/product decision until a concrete exact-head
  patch exists.

Executable `CTO_RESCUE` and `CTO_DIRECT_RESCUE` work uses one eligible registered
Rescues task; new `CTO_HOTFIX` work uses dedicated `hotfixes`. Existing accepted
assignments retain their owner under the roster above.
The CTO decisions task verifies the wake, forwards the exact contract once to
the appropriate existing owner under existing-owner routing, records accepted
delivery, and returns immediately.
It never performs the implementation itself and never waits on rescue progress.
Rescue monitors, review follow-ups, and terminal handling stay with the rescues
task until it
returns a verified new head, digest-bound patch, or typed blocker as a later
wake to the assignment's explicit result consumer under the internal return
split. Delegated tasks never return execution results directly to PM; the
named result consumer owns review routing and downstream PM communication.

Every PR rescue binds issue, PR, 40-character source head, directive, runtime
control point, allowed paths, proof commands, rollback, and downstream PM
transition. A head change invalidates affected proof. Rescue completion is a
verified new head or digest-bound patch plus canonical PM consumption, not a
process launch or packet path.

## Fail-closed rules

- Current-assignment updates use native `codex_app.send_message_to_thread`
  under Rajiv's Ev0C20LL3YBU clarification, not stdio or the IPC CLI. This is
  the sole exception to the Decisions native-tool ban; do not queue updates as
  new jobs. Independent new
  assignments and delegated terminal returns use `$codex-stdio-send-message`
  with exact existing task IDs and stable event-specific dedup keys. Never
  request or create a fork/task solely because the destination is busy.
  Never retry or reroute uncertain delivery.
- Never merge when the live head differs from the wake or the fresh guard is
  not green on real `pull_request` CI and E2E.
- Never run admission/merge skills, integrate a PR branch, admit CI/E2E,
  investigate failed runs, execute or monitor a PR merge in CTO Decisions.
  Forward the exact packet to
  dedicated merge task `01a0324b-68e0-7491-988f-e7e1549f16f7`; do not poll,
  run `gh run watch`, or create a heartbeat while waiting.
- Never execute or monitor a rescue or hotfix in the CTO decisions task.
  Forward it once to the eligible registered Rescues or dedicated hotfix owner
  under the roster and existing-owner routing above, and return without
  polling. Keep accepted
  work with its existing owner; never send new work to a fenced predecessor,
  activate a standby task merely because its ID is listed, or duplicate a
  running tuple.
- Never implement or continuously monitor a bounded control-plane repair in
  CTO decisions or PM. After ownership and scope verification, CTO sends one
  exact brief to the existing CP Repairs or MoP owner by affinity under
  existing-owner routing; that task returns its candidate,
  CTO routes it once to PR Reviews, and the same implementation task receives
  rework or approval. Only that task publishes, installs/activates, restarts
  when required, live-reads back, and rolls back if necessary. CTO Decisions
  verifies the returned terminal and informs PM; it does not wait or poll.
- Never route a PM Operator or `pm-transition` cutover change to superseded task
  `01a03c74-fc97-7a62-bb47-001ac7fb0710` or CP Repairs. Use only
  `01a04154-c9c1-7bc1-8f7b-009a87bc7628` for that affinity.
- Never keep any implementation, test, review, investigation, browser/service
  configuration, monitor, wait, or repeated-verification task in the CTO
  decisions task. Approved control-plane publication/install/activation/
  restart/live-readback/rollback also stays with its existing execution owner.
  Return after exact-tuple handoff acceptance; consume its terminal later.
- Never poll GitHub in any task. Only a single bounded snapshot at wake
  consumption or terminal action is permitted; further state must arrive by
  event or receipt.
- Never perform a direct product-code rescue without explicit Rajiv authority.
- Never interrupt or duplicate a live slot run based only on MoP or a stale
  monitor sample.
- Never let a control-plane repair reserve numbered-slot capacity.
- Never convert a product/architecture decision into a PM operational guess.
- Never mark a wake complete from prose, a label, or an unverified receipt.
