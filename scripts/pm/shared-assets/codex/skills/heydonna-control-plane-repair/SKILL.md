---
name: heydonna-control-plane-repair
description: Diagnose, implement, obtain inline review from HeyDonna CTO decisions, deploy, and verify bounded control-plane repairs. Use when shared CI/E2E/capture workflows, PM transitions, labels, success processors, obligations, stop hooks, deployment scripts, workflow accounting, Master of Panes, PM Operator, or other release automation incorrectly blocks or advances product work; when multiple unrelated PRs fail through the same shared path; or when Rajiv asks to repair or roll out a non-product-code workflow gap directly on main.
---

# HeyDonna Control-Plane Repair

Repair the shared control point once, directly on `main`, instead of adding
workarounds to affected product PRs. Preserve fail-closed behavior. The
implementation owner prepares and proves the candidate; **CTO decisions reviews
every control-plane candidate inline** before publication or activation.

PM has no control-plane review, approval, admission, or marker role. Do not ask
PM to review a control-plane fix, launch a review companion, create or admit a
review marker, or relay a reviewer verdict. PM may provide incident context,
operator/runtime evidence, and affected-product state, but that context is not
release authority.

When choosing or designing a candidate or bounded rework, first read the
[minimum-solution ladder](/Users/rajiv/.codex/skills/_shared/minimum-solution-ladder.md)
after tracing the actual runtime flow. Keep the repair on the smallest existing
control point; this does not relax fail-closed, exact-tuple, proof, rollback, or
live-verification requirements.

## Shared release-conveyor contract

Follow the normative ownership and motion rules in
`/Users/rajiv/.codex/skills/_shared/release-conveyor-contract.md`. Numbered
slots are reserved for executable implementation, reproduction, or
production-shaped proof; CI, capture, review, decisions, and external waits
remain off-slot. PM owns routine free-slot refill and code/proof-ready
exact-head CI/E2E admission, while CTO/release owners consume workflow
terminals through causal routing or head-pinned merge. Every nonterminal state
must name `next_action`, `next_owner`, and `wake`.

Control-plane review is exactly one functionality-first CTO inline decision;
PM has zero review or approval role. After `CTO_INLINE_APPROVE`, this same
implementer automatically continues through publication, scoped activation,
and verification. Control-plane repair is never a customer-lane dependency:
escalate immediately and use the smallest safe degraded/manual product path;
only concrete data-loss, security/privacy, or irreversible harm makes motion
unsafe.

## Hard policy

- Treat invocation of this skill as authority to prepare a bounded
  control-plane fix.
- Implement control-plane fixes directly on current `origin/main`; do not open
  a control-plane PR.
- Do not modify a dirty main checkout. Build the candidate in a clean temporary
  worktree rooted at the exact current `origin/main`.
- Keep product code and control-plane code separate. A product change follows
  the normal product PR, review, CI, E2E, and merge path.
- The implementation owner must return the immutable candidate tuple to CTO
  decisions. CTO decisions alone issues `CTO_INLINE_APPROVE` or
  `CTO_INLINE_BLOCK`.
- PM review, PM fast-path approval, PM companion review, PM review markers,
  marker admission, and PM review receipts are deprecated and must not be used.
  Any executable assets retained for compatibility are historical only.
- Never treat candidate preparation, focused proof, a pushed branch, a staged
  release, or PM acknowledgement as approval.
- Do not publish, install, activate, restart, or mutate affected PR state until
  exact-tuple CTO inline approval exists, unless Rajiv explicitly authorizes
  direct deployment for that exact repair.

## Ownership and return boundary

Use the existing authoritative implementation owner for the repository. The
shared direct control-plane repair task remains the default implementation lane;
PM Operator repairs remain with the PM Operator implementation owner. The
implementer owns diagnosis, the clean candidate, focused proof, rollback design,
and post-approval rollout.

Every candidate returns to the CTO decisions task with:

- repository and clean worktree;
- exact base, sole parent, candidate, and tree;
- stable patch ID and exact changed paths;
- causal control point and bounded behavior change;
- RED-on-base and GREEN focused proof;
- installation/activation plan and live verification;
- rollback boundary; and
- explicit no-mutation inventory.

CTO decisions reviews that packet inline against the candidate diff and may
inspect or run focused read-only checks. It returns exactly one of:

- `CTO_INLINE_APPROVE`, bound to the base, candidate, tree, stable patch ID,
  paths, and focused proof; or
- `CTO_INLINE_BLOCK`, naming a concrete reachable runtime, data-integrity,
  authorization, or release-safety defect and the smallest correction.

An approval authorizes the same implementation owner to perform one ordinary
non-force publication, the repository's canonical scoped rollout, and live
verification. A block returns only the bounded correction to the same owner.
Do not add a second reviewer or send the candidate through PM.

If Rajiv explicitly says `skip review`, `deploy directly`, or equivalent for
the exact repair, record `DIRECT_CONTROL_PLANE_DEPLOY_AUTHORIZED` with the full
tuple, proof, rollout, verification, and rollback evidence. This is a distinct
Rajiv authorization, not PM admission, and never bypasses failed focused proof,
a known product/runtime defect, or a candidate-specific safety failure.

## Classification and safety

- Diagnose read-only before editing. Trace symptom -> writer -> guard -> state
  transition -> downstream consumer.
- Classify the incident as `PRODUCT`, `CONTROL_PLANE`, `MIXED`, or `UNKNOWN`.
  Stop before editing on `UNKNOWN`.
- Name the exact shared surface: workflow selector, typed transition, success or
  failure processor, capture promotion, fixture routing, deployment adapter,
  accounting rule, obligation registry, installer, service activation, or stop
  hook.
- Do not classify a raw workflow `failure` as a product regression until a real
  product job and test are proven to have run. Separate dummy/skipped shells,
  configuration failures, runner failures, fixture misses, and stale-head
  evidence.
- For CI/E2E incidents, reproduction that needs a production-shaped environment
  belongs in a numbered slot. A rescue/control-plane owner may implement the
  bounded fix after the discriminator is known.
- Do not duplicate the app change-scope classifier. A control-plane exemption
  must prove `control_plane_only=true`, `product_changed=false`,
  `ci_required=false`, and `e2e_required=false`; any mismatch fails closed to
  real CI/E2E.
- A parked state must retain one next owner, one machine-observable wake, and
  one canonical next transition.

## Workflow

### 1. Pin the incident

Read `AGENTS.md` and the relevant repository instructions completely. Capture:

- affected PRs and exact heads;
- workflow name, run ID, event, definition source, job, and failing step;
- terminal logs and persisted evidence;
- current `origin/main`;
- live labels, typed transition receipts, and authoritative Slack thread;
- the single changed transition family; and
- every candidate/base/stable-patch tuple already prepared for the incident.

### 2. Prove the boundary

Compare the affected product diff with the failing runtime path and current
main where safe. Identify the authoritative writer and the first incorrect
decision or state transition. Do not edit until the control-plane classification
and product-code separation are evidence-backed.

### 3. Define the smallest coherent repair

Write a short contract for one existing transition family:

1. start state;
2. event or command;
3. guard and authority check;
4. authoritative writer;
5. observed and expected next state;
6. preserved invariants;
7. next owner and wake for every nonterminal state;
8. exact allowed paths;
9. desired, forbidden, retry, idempotency, and applicable interruption cases;
10. RED-on-base discriminator;
11. focused validation;
12. rollout, live verification, and rollback; and
13. affected-PR reconciliation.

Prefer one missing predicate, adapter, or obligation over a duplicate planner,
broad refactor, raw-label automation, or synthesized state. Do not bundle
nearby repairs merely because they occurred in the same session.

### 4. Build an exact-main candidate

Fetch `origin/main`, create a clean temporary worktree at its exact head, and
change only the bounded control-plane surface. Use `apply_patch` for edits.

Validate in proportion to risk:

- focused unit or contract tests;
- desired, forbidden, retry/idempotency, and partial-write cases;
- syntax, type, lint, action, or workflow validation as applicable;
- RED-on-base or RED-on-revert and GREEN proof;
- fail-closed negative cases;
- persistence and idempotency for installed scripts or state;
- `git diff --check`; and
- unrelated-delta and product-path audits.

Do not require broad suites when a focused bounded suite proves the changed
control point. A failure already present on the exact base is not a candidate
regression, but record it honestly.

Commit the candidate locally. Record base, parent, candidate, tree, stable
patch ID, paths, proof commands/results, rollout, verification, rollback, and
the no-mutation inventory.

### 5. Obtain CTO decisions inline review

Return the exact candidate packet to CTO decisions. Do not post it to PM for
review and do not invoke any PM-side control-plane review companion, fast path,
marker-admission script, or reviewer workflow.

CTO decisions applies a functionality-first review:

- Does the candidate fix the proven shared runtime boundary?
- Does it preserve authorization, idempotency, fail-closed behavior, and
  product/data safety?
- Do the RED/GREEN and negative cases exercise the changed writer?
- Is rollout scoped, live-verifiable, and reversible?
- Are the tuple and changed paths exact?

Docs, naming, packet prose, historical asset cleanup, or unrelated hardening do
not block a sound bounded repair. Record them as follow-ups. Block only for a
concrete reachable runtime, data-integrity, authorization, or release-safety
risk.

The inline verdict must include the exact base, candidate, tree, stable patch
ID, path set, and proof identity. A stale or tuple-mismatched verdict is no
approval. If the candidate changes semantically, return the new exact tuple to
CTO decisions once; do not carry approval across semantic drift.

### 6. Publish and roll out after approval

On `CTO_INLINE_APPROVE` or `DIRECT_CONTROL_PLANE_DEPLOY_AUTHORIZED`:

1. fetch `origin/main` again;
2. if main equals the approved base, use the exact approved candidate;
3. if main advanced, replay in a clean worktree and rerun focused validation;
4. carry approval forward only when replay is conflict-free, stable patch ID is
   unchanged, paths are identical, and every required validation passes;
5. otherwise stop and return the new tuple to CTO decisions for fresh inline
   review;
6. publish only by ordinary non-force fast-forward;
7. verify remote main equals the landed candidate;
8. run the repository's canonical scoped rollout; and
9. live-verify exact hashes/content, pointer or service state, health/check
   output, runtime/ABI when applicable, and rollback readiness.

For installed services, a new release pointer alone is not live proof. Restart
the scoped service when required and prove the new child PID/start time,
resolved release/cwd, runtime/ABI, listener, and health response. Never invent
an install or deployment path.

If rollout verification fails, stop at the exact boundary and use only the
documented rollback. Do not expand scope or mutate affected product work to
mask the control-plane failure.

### 7. Reconcile affected product work

Do not copy the control-plane fix into product branches as a workaround.

- Re-read every affected PR's exact head and state.
- Refresh a product branch only when it must contain the fixed code/config.
- Use canonical typed transitions; never perform unguarded raw edits to target
  state labels. The sole narrow exception is the CTO-authorized fallback in the
  shared conveyor contract: after a fresh exact-head, duplicate/run, and safety
  fence, CTO may perform one journaled, idempotent GitHub label edge that
  preserves unrelated labels and stops after that edge. PM never performs or
  retries this fallback.
- Resume only the workflow justified by the incident.
- Preserve existing exact-head green legs where policy permits.
- Require real CI/E2E runs, excluding skipped or dummy shells, before merge
  readiness.

### 8. Verify the repaired path

Require one production-shaped downstream terminal proving:

- the expected workflow/event ran on the exact product head;
- the repaired control point executed;
- the original false block or false advancement is gone;
- legitimate failures still fail closed;
- no unrelated PR or workflow state changed; and
- any durable obligation has one owner and wake.

Do not call the repair complete merely because publication or activation
succeeded.

## Final report

Report:

- classification and causal chain;
- authoritative control point;
- reviewed base/candidate/tree, stable patch ID, paths, and landed tuple;
- exact `CTO_INLINE_APPROVE` or `CTO_INLINE_BLOCK` from CTO decisions, or the
  exact Rajiv direct-deploy authorization;
- focused validation and RED/GREEN proof;
- non-force publication and scoped rollout evidence;
- live verification and rollback status;
- affected product exact-head results and typed receipts; and
- remaining product blockers, separated from control-plane follow-ups.

Do not claim completion until the approved change is on `origin/main`, the
scoped rollout is live-verified, and at least one production-shaped downstream
path proves the repaired behavior.
