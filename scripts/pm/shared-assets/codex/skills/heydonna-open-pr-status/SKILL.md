---
name: heydonna-open-pr-status
description: Reconcile affected HeyDonna OPEN PR observations into the applicable release lane without fabricating motion.
---

# HeyDonna Open-PR Activity Reconciliation

## Routine ownership and escalation

Follow the shared release-conveyor decision boundary. Read and diagnose every
PR freely; PM executes routine scheduling/lifecycle/conveyor actions and CTO
executes routine technical and release work. Choosing among eligible actions
is not itself a Rajiv escalation; reserved escalation classes remain governed
by the shared contract.

This is the PR Merges release-owner audit and action contract. On a normal wake,
inspect only the affected PR/head tuple named by the wake. A full portfolio
snapshot is a deliberate backstop or an explicit request, not a per-wake
requirement. Report the truthful observation for each tuple using the applicable
state:

- `CI_E2E_IN_PROGRESS`
- `CAPTURE_IN_PROGRESS`
- `REPRO_REWORK_IN_PROGRESS`
- `REPRO_REWORK_QUEUED`
- `HELD`
- `BLOCKED`
- `WAITING`
- `COMPLETED`

`ACTION_REQUIRED`, `UNKNOWN`, `awaiting review`, and similar labels are
diagnostics, not proof of motion. Do not fabricate a queued rework edge or an
executable owner to make a row fit a release lane. When no active lane is
proven, report the exact blocker, owner, next action, and wake; preserve a
truthful hold or wait until an authorized edge exists.

## Authority and safety

Take one bounded snapshot; never poll or watch. Bind every decision to the
live PR head, branch, draft/mergeability state, exact workflow event/run, and
authoritative MoP owner/queue tuple. Preserve existing owners and runs and
never duplicate a genuine exact-head pair, capture, rework owner, or merge.
Labels, prose, and MoP projections are readback, not transition authority.
PM is limited to one failed-run investigation and explicitly authorized slot
assignment mechanics; PR Merges owns release transitions.

## PM code-review visual contract

For every UI-changing PR, read the current linked issue body and require its
current visual acceptance contract before issuing a readiness-bearing review
status. Each deterministic visual criterion must identify its stable AC ID,
route/state, expected observable, and evidence binding. Missing, malformed,
stale, head-mismatched, or body-mismatched visual evidence is never a PASS.

Code review may finish with the typed terminal
`APPROVE_CODE_PENDING_QA_VISUAL_PROOF` when the code review itself is sound but
the visual proof is not yet complete. If the assigned slot cannot provide the
required browser/auth capability, return `QA_CAPABILITY_BLOCKED` and route only
the proof step to a provisioned owner; never waive screenshots, call them out
of scope, or substitute component tests. PM may carry the code verdict and
evidence reference, but cannot trigger paid CI/E2E or turn either terminal
into a readiness-bearing PASS. Non-UI PRs with no visual contract retain the
ordinary review path.

## Transition contract

1. Preserve `CAPTURE_IN_PROGRESS` only for an accepted exact-head capture that
   is queued or running.
2. Preserve `CI_E2E_IN_PROGRESS` only for genuine exact-head pull-request CI
   and E2E runs that are queued or running as a pair.
3. Preserve `REPRO_REWORK_IN_PROGRESS` only for active numbered or owned
   exact-head repro/rework with live evidence.
4. Preserve `REPRO_REWORK_QUEUED` only for a durable exact-head owner/queue
   receipt naming the next owner and executable wake.

If no protected state is proven, preserve the observation and route only the
smallest already-authorized next edge: merge only after the exact current-head
guard is green, classify one failed run before any retry, capture only after an
exact identity miss, and send concrete rework only to an existing owner with a
durable receipt. A merge conflict, product/data/security blocker, or missing
owner remains `BLOCKED` or `HELD`; it must not be converted into invented
queued work. Never blind-rerun, duplicate an active lane, or treat a label,
projection, or prose promise as motion.

Identical snapshots are idempotent: do not repeat a run, capture, owner, or
merge whose exact tuple is already active or durably recorded.

## PM terminal handoff

PM-owned terminal work is a material handoff, not a status summary. Emit one
and only one bounded `PM_CTO_TERMINAL` envelope in the canonical PM Slack
thread for each terminal type: `FAILED_RUN_INVESTIGATION`, `NUMBERED_PROOF`,
`REWORK_REVIEW_CANDIDATE`, `CAPTURE_TERMINAL`, `ASSIGNMENT_TERMINAL`, or
`TYPED_BLOCKER`. Required fields are `terminal_type`, `pr`, full `head`,
`run_or_capture` when applicable, `owner`, `evidence_summary`, `next_action`,
`next_owner`, `wake`, and `source_receipt`. The PM-to-CTO monitor routes each
first envelope immediately and suppresses routine progress/ack receipts.
Deduplicate by terminal type plus exact PR/head/source receipt; an identical
terminal emits no second wake. PM may report or perform authorized slot
mechanics, but the envelope cannot execute CTO-owned CI/E2E admission, capture,
integration, or merge.

Use the manifest-mapped executable
`/Users/rajiv/.claude/scripts/pm-terminal-continuity.py complete` as the PM
completion producer and require a successful `RESERVED` or exact
`DUPLICATE_SUPPRESSED` result before claiming the terminal is recorded. The
monitor then invokes `deliver`, whose manifest-mapped
`/Users/rajiv/.claude/scripts/pm-terminal-wake.py` adapter performs the single
CTO task handoff. Completion reserves the exact terminal-type/PR/full-
head/source-receipt key before waking CTO; response-loss and exact replay are
fail-closed, while changed head or terminal type creates a distinct key. A
nonzero or malformed completion result stops the PM transition and cannot be
reported as a healthy terminal.

The receiving CTO wake executes or durably delegates the mapped next edge in
that same wake. The mapping is: failed-run investigation -> classify and route
one next release edge; numbered proof -> consume proof and route admission or
rework; rework/review candidate -> review/admit or route correction; capture
terminal -> exact-head CI/E2E admission; assignment terminal -> verify the
assigned packet's next boundary; typed blocker -> execute the safe degraded
edge or record the concrete harm that makes it unsafe. No PM_WAIT, prose-only,
label-only, or owner-only receipt is a terminal continuation.

## Hourly terminal

Return a post-action `OPEN_PR_ACTIVITY_RECONCILIATION` receipt for the affected
tuple(s), containing exact head, truthful state, edge taken (if any), owner/run,
and blocker. A full portfolio receipt is emitted only for an explicit request
or the scheduled backstop. The automation is read-only
with respect to product/customer data, but the PR Merges owner may invoke only
the existing guarded release, assignment, capture, and merge boundaries above.
This hourly audit is a continuity backstop only: it repairs one PM-terminal
that was emitted without a recorded CTO consumption/next-edge receipt, then
fails loudly with `TERMINAL_CONTINUITY_BREACH` if continuity is still absent.
It is never the normal mover and never creates `ACTION_REQUIRED`.
