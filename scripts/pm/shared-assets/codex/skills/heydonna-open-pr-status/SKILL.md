---
name: heydonna-open-pr-status
description: Reconcile every HeyDonna OPEN PR into one executable four-state release lane.
---

# HeyDonna Open-PR Four-State Reconciliation

This is the PR Merges release-owner audit and action contract. On every wake,
enumerate every OPEN PR at its exact 40-character head and finish each row in
exactly one state:

- `CI_E2E_IN_PROGRESS`
- `CAPTURE_IN_PROGRESS`
- `REPRO_REWORK_IN_PROGRESS`
- `REPRO_REWORK_QUEUED`

`ACTION_REQUIRED`, `UNKNOWN`, `awaiting review`, and similar labels are not
terminal states. They may be internal `INVALID_TRANSITION` diagnostics only;
before the wake completes, execute or durably route the smallest next edge.

## Authority and safety

Take one bounded snapshot; never poll or watch. Bind every decision to the
live PR head, branch, draft/mergeability state, exact workflow event/run, and
authoritative MoP owner/queue tuple. Preserve existing owners and runs and
never duplicate a genuine exact-head pair, capture, rework owner, or merge.
Labels, prose, and MoP projections are readback, not transition authority.
PM is limited to one failed-run investigation and explicitly authorized slot
assignment mechanics; PR Merges owns release transitions.

## Transition contract

1. Preserve `CAPTURE_IN_PROGRESS` only for an accepted exact-head capture that
   is queued or running.
2. Preserve `CI_E2E_IN_PROGRESS` only for genuine exact-head pull-request CI
   and E2E runs that are queued or running as a pair.
3. Preserve `REPRO_REWORK_IN_PROGRESS` only for active numbered or owned
   exact-head repro/rework with live evidence.
4. Preserve `REPRO_REWORK_QUEUED` only for a durable exact-head owner/queue
   receipt naming the next owner and executable wake.

If no protected state is proven, finish the transition in this same wake:

- dual-green exact-head CI/E2E plus review/product gates clear: run the
  canonical guard and head-pinned merge;
- completed review/capture/rework with no active lane: merge current main into
  the PR branch non-force, resolve a concrete conflict/product blocker through
  one owned or queued rework receipt, otherwise admit one genuine exact-head
  CI/E2E pair;
- failed CI/E2E/capture: consume and classify the failure once, then create or
  resume exactly one active or durable queued repro/rework packet; never blind
  rerun;
- exact-head fixture identity miss: begin one duplicate-fenced capture lane;
- process-only refusal: use the existing guarded one-refusal direct fallback
  once, then read back its terminal edge.

An unresolved merge conflict, product/data/security blocker, or unavailable
authoritative owner must become `REPRO_REWORK_IN_PROGRESS` or
`REPRO_REWORK_QUEUED` with one owner, exact PR/head, attempted edge, literal
blocker, and executable wake. A typed invariant breach is emitted if the row
still cannot satisfy one of the four states; never emit a fifth state or an
unowned waiting row.

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

The receiving CTO wake executes or durably delegates the mapped next edge in
that same wake. The mapping is: failed-run investigation -> classify and route
one next release edge; numbered proof -> consume proof and route admission or
rework; rework/review candidate -> review/admit or route correction; capture
terminal -> exact-head CI/E2E admission; assignment terminal -> verify the
assigned packet's next boundary; typed blocker -> execute the safe degraded
edge or record the concrete harm that makes it unsafe. No PM_WAIT, prose-only,
label-only, or owner-only receipt is a terminal continuation.

## Hourly terminal

Return a post-action `OPEN_PR_FOUR_STATE_RECONCILIATION` receipt containing
every open PR, exact head, final state, edge taken, owner/run, and blocker (if
the edge was safely converted to queued rework). The automation is read-only
with respect to product/customer data, but the PR Merges owner may invoke only
the existing guarded release, assignment, capture, and merge boundaries above.
This hourly audit is a continuity backstop only: it repairs one PM-terminal
that was emitted without a recorded CTO consumption/next-edge receipt, then
fails loudly with `TERMINAL_CONTINUITY_BREACH` if continuity is still absent.
It is never the normal mover and never creates `ACTION_REQUIRED`.
