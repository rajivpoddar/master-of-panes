---
name: heydonna-control-plane-repair
description: Prepare one bounded control-plane repair candidate for CTO Decisions inline review. Use for shared CI/E2E/capture workflows, PM transitions, labels, obligations, deployment adapters, Master of Panes, PM Operator, and other release automation that incorrectly blocks or advances product work.
---

# HeyDonna Control-Plane Repair

This skill prepares a bounded repair candidate. It does not publish, install,
activate, restart, deploy, or mutate product/control-plane state. The sole
implementation owner for a verified bounded control-plane repair, including a
PM Operator or `pm-transition` repair, is Master of Panes task
`01a04154-c9c1-7bc1-8f7b-009a87bc7628`. The scope may be tagged
`CONTROL_PLANE` or `PM_OPERATOR`, but the implementation owner is not
duplicated.

PM reports the first literal blocker and exact affected tuple to CTO Decisions.
PM has no review, approval, deployment, retry, marker, or admission role.
CTO Decisions performs causal diagnosis and classifies the issue as
`PRODUCT`, `CONTROL_PLANE`, `MIXED`, or `UNKNOWN`; only a verified bounded
`CONTROL_PLANE` brief is sent to the implementation task.

## Normative process

1. CTO Decisions sends one exact implementation brief to the MoP task.
2. MoP fetches the exact current `origin/main` and creates a clean detached
   worktree. It traces the actual writer/guard/state transition, makes the
   smallest coherent change with `apply_patch`, and proves RED-on-base,
   GREEN, negative, retry/idempotency, and applicable interruption cases.
3. MoP records the no-mutation inventory, rollback boundary, and immutable
   base/parent/candidate/tree/stable-patch/path tuple. It returns that
   candidate packet directly to CTO Decisions through
   `$codex-stdio-send-message`.
4. CTO Decisions performs exactly one functionality-first inline review bound
   to that immutable tuple. `CTO_INLINE_BLOCK` returns one bounded correction
   to this same MoP task. No PM review, PM marker, PM companion, second owner,
   or duplicate reviewer ceremony is used.
5. `CTO_INLINE_APPROVE` transfers post-approval release execution to CTO
   Decisions. CTO re-fences current main, publishes by ordinary non-force
  fast-forward, stages/activates the canonical immutable release, restarts
  the affected service when required, verifies live readiness and rollback,
  and sends the single PM notification with the landed tuple and exact next
  action. MoP stops
   after the approved candidate packet and never deploys after approval.

The only approval that changes this boundary is an explicit Rajiv directive
for the exact repair. A queue receipt, PM acknowledgement, candidate branch,
or stale approval is not publication authority. If main advances, CTO carries
the approval only across one conflict-free replay with identical semantics,
stable patch ID, and exact path set; conflict or semantic drift requires a new
inline decision.

## Safety and scope

- Do not edit a dirty main checkout. Use a clean worktree at the exact fetched
  origin head and preserve unrelated work.
- Diagnose read-only before editing: symptom, writer, guard, authority,
  transition, consumer, and the smallest existing control point.
- Keep product code separate. Product/test changes use their normal product
  owner and release path; a control-plane repair is not a customer-lane
  dependency.
- Preserve exact repository, PR, full head, owner, epoch, workflow, receipt,
  and authority fences relevant to the affected path. Refuse missing,
  malformed, stale, ambiguous, drifted, duplicate, or unavailable evidence.
- Prefer an existing predicate, adapter, journal, or obligation. Do not add a
  second writer, planner, state machine, raw-label bypass, raw workflow
  dispatch, synthesized marker, or retry loop.
- Never reserve a numbered product slot for control-plane implementation or
  proof. No polling or wait loop is part of the candidate.
- For CI/E2E evidence, distinguish real exact-head `pull_request` runs from
  skipped shells, dummy contexts, stale heads, capture misses, and runner
  failures. Preserve head-pinned merge and duplicate/single-flight guards.
- A direct manual edge, if separately authorized by CTO, remains one fresh-
  fenced, journaled, idempotent edge preserving unrelated state. This skill
  only specifies the candidate; it does not execute that edge.

## Candidate contract

The candidate packet must state:

- Rajiv directive, intended capability, production failure mode, and exact
  runtime control point;
- classification and causal chain;
- repository and clean worktree;
- exact base, sole parent, candidate, tree, stable patch ID, and changed paths;
- RED-on-base discriminator, GREEN proof, negative/refusal matrix, and
  persistence/response-loss proof;
- source/staged/live install mapping and parity plan;
- exact rollback release/preimages;
- affected product tuple and explicit no-mutation inventory; and
- the post-approval boundary naming CTO Decisions as publisher, installer,
  activator, service restarter when required, live verifier, and PM notifier.

Use one short transition contract with start state, event, guard, authoritative
writer, expected state, preserved invariants, retry/idempotency behavior,
nonterminal owner/wake, RED discriminator, focused proof, rollout boundary,
and affected-PR reconciliation. Evidence must be emittable by the changed
runtime; do not add an impossible receipt gate.

## Validation

Run only relevant focused tests and package/installer checks. Include:

- RED-on-base and GREEN on the actual changed caller/writer;
- exact positive and fail-closed negative cases;
- concurrent/single-flight or response-loss checks when applicable;
- syntax/compile, manifest/source parity, and `git diff --check`; and
- unrelated-delta and product-path audits.

Commit only the local candidate after the proof is complete. Re-read its tuple
and clean status before returning it. Do not push, install, activate, restart,
or run a live product canary from this implementation task.

## CTO handoff

Send exactly one candidate packet to CTO Decisions task
`01a03236-2e61-71f3-a6a8-3dc24d8c8917` with a stable event-specific dedup key
using `$codex-stdio-send-message`. Do not send it to PM or Slack and do not
use `codex_app` tools. The packet is an actionable CTO review wake, not a
ledger-only note.

On `CTO_INLINE_BLOCK`, change only the named bounded defect in this same task,
re-run the focused proof, and return one corrected immutable packet. On
`CTO_INLINE_APPROVE`, do not continue to publication: CTO Decisions performs
the fresh fence, non-force publication, scoped install/activation, required
MoP restart/readiness proof, live parity, rollback verification, and single PM
notification. This task returns no post-approval deployment terminal.

## No-mutation inventory

Every candidate packet must explicitly say that no GitHub, Slack, PM, slot,
workflow, product, customer, live service, or affected PR state was mutated;
no reviewer/provider was invoked; and no capture, rerun, merge, label edge, or
numbered-slot action was performed unless the exact brief explicitly scopes a
read-only fixture. Preserve all prior release pointers and owner-only
preimages for CTO rollback.
