---
name: heydonna-open-pr-status
description: Reconcile every HeyDonna OPEN PR through its reviewed-scope release lane.
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

Bind reviewed scope to the exact live changed-file set. An authoritative
exact-head classifier receipt confirming marketing website/blog-only `site`
scope, with no app code or shared app
runtime/dependency change, requires relevant exact-head site build,
media/asset, and visual proof as applicable, but no paid app CI/E2E pair.
App, mixed, or unknown scope requires the genuine exact-head CI/E2E pair;
unknown or mismatched scope is never exempt. Site-only completion is not
dual-green and is not a red-app-workflow waiver.

## Ordinary and authorized emergency merges

PR Merges remains the sole merge executor. Ordinary merges use this skill's
`scripts/merge.py`: require genuine exact-head required CI/E2E execution green
or its supported exact-head classifier exemption, plus the existing review,
product-safety and later-main overlap checks. Do not invoke retired `.claude`
PM-conveyor or pre-merge guard layers.

An emergency exception requires verified current human Rajiv authority
(`UEQTTB97A`), bound to the source channel/thread/event, requested PR, approved
full 40-character head and exact scope. Distinguish two independent permissions:

- A CI/E2E waiver permits only the explicitly authorized merge with those
  checks running, missing or red as scoped. It does not grant `--admin`.
- An additional explicit admin authorization permits `--admin` to override
  native branch protection for that same PR/head/scope. It does not by itself
  waive CI/E2E. A generic CTO/PM merge request, old waiver or protection
  refusal is not admin authority. Never infer a future blanket override.

For a verified emergency waiver, use the manual head-pinned command path
below; the ordinary helper intentionally has no waiver/admin flags. Keep all
non-waived review/product-safety gates and later-main overlap checks. Do not
repeat review on unchanged product code or wait for CI waived by Rajiv, and
never make skill repair a prerequisite for urgent recovery.
For admin authority without a CI/E2E waiver, obtain the ordinary helper's
read-only `READY_TO_MERGE` proof first; only the native-protection override
differs. Admin authority never converts running or red checks to green.

Before any merge command, reconcile previously issued, merged or uncertain
effects for the exact tuple through existing receipts and one bounded PR
readback. An issued/uncertain attempt is not permission to resend. An already
merged tuple needs no action; unresolved delivery/effect ambiguity stops the
mutation. Do not re-key an uncertain effect or automatically retry.

Set `PR` and `APPROVED_HEAD` from the verified scoped authorization. Immediately
before the single action, read current head and require equality; a moved head
does not inherit authority:

```bash
HEAD=$(gh pr view "$PR" --json headRefOid --jq .headRefOid) || exit 1
test "$HEAD" = "$APPROVED_HEAD" || exit 1
```

For a CI/E2E waiver WITHOUT explicit admin permission:

```bash
gh pr merge "$PR" --squash --delete-branch --match-head-commit "$HEAD"
```

Only WITH verified additional admin permission for this exact tuple:

```bash
gh pr merge "$PR" --squash --admin --delete-branch --match-head-commit "$HEAD"
```

Use one action and one bounded readback, never polling or waiting for CI:

```bash
gh pr view "$PR" --json headRefOid,state,mergedAt,mergeCommit
```

Record actual command outcome, head, merge commit/readback, scoped human
authorization and real workflow run IDs/statuses (including running/red,
never relabeled green). A timeout, response loss or unconfirmed readback
remains uncertain: reconcile it, never blindly repeat or upgrade the command.
A definitive native protection refusal without explicit admin authority
requires that authority, not an automatic `--admin` retry. No standing
protection edit or force push is authorized here. Post-merge red becomes a
PM-owned follow-up, not automatic rollback merely for red; proven runtime
harm still warrants mitigation under the applicable authority.

## Transition contract

1. Preserve `CAPTURE_IN_PROGRESS` only for an accepted exact-head capture that
   is queued or running.
2. Preserve `CI_E2E_IN_PROGRESS` only for genuine exact-head pull-request CI
   and E2E runs that are queued or running as a pair for app, mixed, or
   unknown scope. Never use it to imply a pair is required or green for
   confirmed marketing-only site scope.
3. Preserve `REPRO_REWORK_IN_PROGRESS` only for active numbered or owned
   exact-head repro/rework with live evidence.
4. Preserve `REPRO_REWORK_QUEUED` only for a durable exact-head owner/queue
   receipt naming the next owner and executable wake.

If no protected state is proven, finish the transition in this same wake:

- exact-head review/product gates clear and either app/mixed/unknown CI/E2E is
  dual-green, or confirmed marketing-only site scope has its required exact-head
  site proof: use the ordinary head-pinned merge path above. A site-only result
  is not reported as dual-green;
- completed review/capture/rework with no active lane: merge current main into
  the PR branch non-force, resolve a concrete conflict/product blocker through
  one owned or queued rework receipt, otherwise admit one genuine exact-head
  CI/E2E pair for app, mixed, or unknown scope; confirmed site-only scope
  instead follows its exact-head site-proof path;
- failed required CI/E2E, site proof, or capture: consume and classify the
  failure once, then create or resume exactly one active or durable queued
  repro/rework packet; never blind rerun;
- exact-head fixture identity miss: begin one duplicate-fenced capture lane;
- process-only refusal: reconcile the attempted effect and use only a
  verified scoped emergency exception above; a refusal grants no new authority.

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
terminal -> scope-appropriate next edge (app/mixed/unknown CI/E2E admission or
confirmed site-only proof); assignment terminal -> verify the
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
