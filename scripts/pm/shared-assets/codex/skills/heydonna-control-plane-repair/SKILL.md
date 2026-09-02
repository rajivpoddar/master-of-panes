---
name: heydonna-control-plane-repair
description: Route one bounded HeyDonna control-plane defect from CTO Decisions to the CP Repairs or Master of Panes task, then through PR Reviews and back to the same execution task for rollout. Use for release automation, CI/E2E/capture orchestration, labels, obligations, slot control, MoP, PM Operator, and deployment adapters. All canonical control-plane source is in the Master of Panes repository; this skill never authorizes inline CTO execution.
---

# HeyDonna Control-Plane Repair

This is a delegation and lifecycle contract. CTO Decisions classifies and
routes; it never investigates deeply, implements, reviews, publishes, deploys,
installs, activates, restarts, rolls back, monitors, or waits.

Canonical control-plane source is
`/Users/rajiv/Downloads/projects/master-of-panes`. Do not use HeyDonna app
`scripts/pm/**` or `scripts/ci/**` as the control-plane implementation boundary.
App product code, app tests, workflow definitions, capture fixtures, strict-
replay harnesses, and other app-owned surfaces remain outside this skill.

PM-owned open-PR terminals are delivered as one bounded `PM_CTO_TERMINAL`
envelope (exact PR/head, owner, evidence, next action/owner, wake, and source
receipt) to the canonical PM thread. The PM-to-CTO monitor wakes once for the
terminal tuple; routine progress is suppressed. CTO consumes it by executing
or durably delegating the mapped next edge in the same wake. The hourly audit
is only a continuity backstop and must never become a release dependency or a
fifth open-PR state.

## Task affinity

- CP Repairs `01a0324b-68e0-7491-988f-e7da9abd26ab`: shared release and
  control-plane investigation, implementation, publication, deployment,
  activation, restart/readiness, and rollback outside the MoP/PM Operator
  runtime affinity.
- Master of Panes `01a04154-c9c1-7bc1-8f7b-009a87bc7628`: MoP and PM Operator
  investigation, implementation, caller migration, install, cutover,
  restart/readiness, rollback, and retirement.
- PR Reviews `01a03265-4b66-7672-bbc2-4a38fb1005b5`: the single independent
  functionality-first candidate review. It never implements or deploys.
- CTO Decisions `01a03236-2e61-71f3-a6a8-3dc24d8c8917`: bounded
  classification, routing, verdict consumption, and the final PM communication.

Repository location does not override task affinity. Preserve one
implementation owner for the exact tuple.

## Admission

CTO Decisions may take one bounded read-only snapshot to bind the literal
blocker, affected tuple, current authority, and likely control point. Classify
as `PRODUCT`, `CONTROL_PLANE`, `MIXED`, or `UNKNOWN`.

Proceed only for a bounded `CONTROL_PLANE` scope in the Master of Panes
repository. Route product/test/harness work to Rescues or the appropriate
numbered-slot boundary. Escalate `MIXED`, `UNKNOWN`, new writable authority,
destructive recovery, or an unproven rollback boundary.

## Lifecycle

1. CTO Decisions sends one exact implementation brief to CP Repairs or Master
   of Panes using `$codex-stdio-send-message` with a stable event-specific dedup
   key. The brief binds authority, repository, base, affected tuple, allowed
   scope, runtime control point, focused proof, rollback, and terminal return.
2. The implementation task uses a clean worktree from current `origin/main`,
   traces the actual writer/guard/state transition, makes the smallest coherent
   change with `apply_patch`, and runs focused RED/GREEN, negative,
   retry/idempotency, syntax/build, and diff checks appropriate to the defect.
3. It returns one immutable candidate packet to CTO Decisions with exact base,
   parent, candidate, tree, stable patch ID, paths, proof, rollout, rollback,
   and no-mutation inventory, then stops.
4. CTO Decisions sends that packet once to PR Reviews. PR Reviews returns
   `APPROVE`, `REVISE`, or `BLOCK` directly to CTO Decisions without mutation.
5. CTO Decisions sends a block/revision back to the original implementation
   task for one bounded rework. It sends an approval back to that same task for
   non-force publication, scoped install/activation, required restart, live
   readiness, and rollback proof.
6. The execution task returns the deployment terminal or typed blocker directly
   to CTO Decisions. CTO Decisions may then send the single PM terminal; it does
   not repeat verification or monitor progress.

## Candidate and rollout contract

The candidate packet must include the Rajiv directive, intended capability,
production failure mode, exact runtime control point, causal chain, repository,
clean worktree, exact immutable tuple, focused proof, rollback preimages,
affected product tuple, and explicit no-mutation inventory.

For a MoP rollout, staging or changing a release pointer is not live proof. The
Master of Panes task must restart the canonical service and return the new child
PID/start time, exact release/cwd, Node runtime/ABI, listener bind, and HTTP 200
`/health`. Rollback restores the prior immutable release, restarts the service,
and repeats the same readiness proof.

## Fail closed

- No numbered product slot for control-plane implementation or proof.
- No GitHub, Slack, PM, slot, workflow, customer, or live-service mutation
  before approval, except an explicitly scoped read-only diagnostic.
- No direct CTO implementation, review, publication, deployment, restart, or
  rollback, even if the wake or label says `DIRECT` or `EXECUTE_NOW`.
- No PM review, reviewer marker, duplicate owner, polling, wait loop, or
  `codex_app` tool.
- Every task handoff and terminal return uses `$codex-stdio-send-message`, the
  exact existing task ID, and a stable event-specific dedup key. A durable queue
  receipt is acceptance; do not retry it or create another owner.
