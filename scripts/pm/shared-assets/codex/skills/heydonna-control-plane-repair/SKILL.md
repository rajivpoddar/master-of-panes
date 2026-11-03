---
name: heydonna-control-plane-repair
description: Repair bounded MoP and PM Operator defects directly in the current chat through focused proof, one independent review, publication, install, restart, and live verification. Route other shared control-plane defects to CP Repairs. Canonical source is in the Master of Panes repository.
---

# HeyDonna Control-Plane Repair

## Routine ownership and escalation

Follow the shared release-conveyor decision boundary: PM owns routine
scheduling/lifecycle/conveyor execution; CTO owns routine technical diagnosis,
bounded repair, same-owner continuation, reviewed publication, and normal
CI/capture/merge/rollout. Choosing among eligible actions is not itself a
process-policy change. User-authorized repairs continue through proof, warranted
independent review, publication, and scoped install without another approval
hop; only the reserved escalation classes in the shared contract go to Rajiv.

For MoP and PM Operator defects, the current CTO chat owns diagnosis,
implementation, focused validation, publication, scoped install, restart,
live verification, and rollback. Do not hand this work to the Master of Panes
task. Rajiv-authorized repair and restart requests continue to completion
without another approval hop. Analysis-only requests remain read-only.

For other shared control-plane defects, CTO Decisions routes execution to
CP Repairs and consumes its candidate and rollout results as described below.

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

The executable PM terminal producer/parser/router is the shared,
manifest-mapped `/Users/rajiv/.claude/scripts/pm-terminal-continuity.py`.
The PM completion boundary calls `complete`; the monitor calls `deliver`.
`deliver` durably records `effect-start` before the CTO wake and commits
`delivered` only from an authoritative receipt. Crash, timeout, nonzero,
malformed, or response-loss outcomes are permanent `ambiguous` records and
cannot replay. It binds CTO consumption and next-edge receipts to the exact
key and permits one hourly repair only for a non-ambiguous emitted terminal
lacking consumption or edge. The active PR-Merges scheduler prompt is
updated only through the supported
`/Users/rajiv/.claude/scripts/pm-merges-automation-update.py` payload path,
which preserves scheduler-owned metadata, notification policy, dynamic
timestamps, and the complete prior prompt/metadata rollback preimage.

## Execution ownership

- CP Repairs `01a08f69-45db-71c2-b433-678419139ed7`: shared release and
  control-plane investigation, implementation, publication, deployment,
  activation, restart/readiness, and rollback outside the MoP/PM Operator
  runtime affinity.
- Current CTO chat: MoP and PM Operator investigation, implementation,
  caller migration, install, cutover, restart/readiness, rollback, and
  retirement. No dedicated MoP task handoff.
- PR Reviews `01a0b53e-3316-77d3-9610-1c0c58d4ba5b`: the single independent
  functionality-first candidate review. It never implements or deploys.
- CTO Decisions `01a09112-a09c-7361-9a2a-0ada6a4e9dfb`: also classifies and
  routes non-MoP repairs, consumes verdicts, and handles final PM communication
  when authorized.

Repository location does not override task affinity. Preserve one
implementation owner for the exact tuple. Reconcile or transfer any repair
already active elsewhere before taking over; do not create competing changes
or restarts.

## Admission

Bind the literal blocker, affected tuple, current authority, and runtime
control point. Classify
as `PRODUCT`, `CONTROL_PLANE`, `MIXED`, or `UNKNOWN`.

Proceed only for a bounded `CONTROL_PLANE` scope in the Master of Panes
repository. Route product/test/harness work to Rescues or the appropriate
numbered-slot boundary. Escalate `MIXED`, `UNKNOWN`, new writable authority,
destructive recovery, or an unproven rollback boundary.

## Direct MoP and PM Operator repair

1. Use a clean worktree from verified current `origin/main`. Trace the actual
   writer, guard, or state transition and make the smallest coherent change
   with `apply_patch`. Preserve unrelated work and active PM/dev agents.
2. Run one focused proof plan: reproduce the observed failure, verify the
   correction and relevant negative or retry cases, and run required
   syntax/build checks.
3. Obtain one independent functionality-first review after the final code
   change. PR Reviews may review the immutable candidate; implementation and
   rollout stay in this chat. Do not repeat review for unchanged code or
   delay safe recovery for wording or receipt-only nits.
4. Publish without force, install the scoped change, restart the canonical
   service when required, and verify the actual live process and restored
   capability. Preserve the prior immutable release and rollback preimages.
5. Report the deployed revision, focused proof, restart/readiness evidence,
   and material limitations. Stop when the requested outcome and minimum safe
   proof are satisfied.

For live-user incidents, spend at most 15 minutes on workflow machinery before
using the safe degraded/manual recovery path. A queued handoff or staged
release is not a completed repair.

## Delegated lifecycle for other control-plane repairs

1. CTO Decisions sends one exact implementation brief to CP Repairs
   using `$codex-stdio-send-message` with a stable event-specific dedup
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
current execution owner must restart the canonical service and report the new child
PID/start time, exact release/cwd, Node runtime/ABI, listener bind, and HTTP 200
`/health`. Rollback restores the prior immutable release, restarts the service,
and repeats the same readiness proof.

## Fail closed

- No numbered product slot for control-plane implementation or proof.
- Repair authorization covers the requested control-plane change and scoped
  rollout. PM alone performs numbered-slot lifecycle and production-DB
  effects. Slack posts require explicit authorization.
- Direct CTO implementation and rollout are allowed for authorized MoP and
  PM Operator repairs. Other control-plane repairs retain the CP Repairs
  execution boundary.
- No PM review, reviewer marker, or duplicate implementation owner. Receipt
  and routing machinery must not delay safe user recovery.
- Every task handoff and terminal return uses `$codex-stdio-send-message`, the
  exact existing task ID, and a stable event-specific dedup key. A durable queue
  receipt is acceptance; do not retry it or create another owner.
