---
name: pr-state-sweep
description: Canonical PM-owned pre-dispatch reconciler for open PR state. Run before Skill(direct-assign) issue dispatch to catch merge-ready slot labels, stale CI blockers, blocked-rework PRs with free capacity, draft orphans, and PR state drift.
author: Dhruva PM
version: 1.2.1
date: 2026-07-02
---

# pr-state-sweep

## Rajiv product/process decision gate

PM may diagnose freely and may execute only a mechanically determined action
under an existing Rajiv-approved rule. A product decision changes customer
behavior, scope, priority, data, or acceptance criteria. A process decision
changes, waives, overrides, excepts, or chooses among Ready Pool, slot,
PR-conveyor, review, CI/capture/rerun, merge/rollout, monitor, or automation
paths. For either class, stop before mutation and post one evidence-bound
recommendation to Abhijit CTO in `#heydonna-dev`. CTO alone DMs Rajiv and waits
for explicit approval. Multiple eligible choices, force/override, or material
drift requires escalation; PM never DMs Rajiv directly.


## Purpose

`pr-state-sweep` keeps current PRs moving before PM assigns fresh Ready Pool work.
It is intentionally separate from issue dispatch:

- `pr-state-sweep` owns open PR state transitions.
- Fresh `status:todo` issue intake dispatches only through `Skill(direct-assign)`.

Run order is mandatory:

```bash
~/.claude/skills/pr-state-sweep/scripts/sweep.sh --trigger=reconcile-capacity
```

If the PR sweep emits any `PR_*_REQUIRED` line, PM must resolve that current-PR
work before dispatching new issue work through `Skill(direct-assign)`.

## What It Catches

- `merge-ready` PRs still carrying `slot:*` labels, including legacy
  `pm-state:merge-ready` labels that need migration.
- `pm-state:qa-passed-awaiting-ci` PRs also carrying `pm-blocked:ci`.
- `pm-state:qa-passed-awaiting-ci` PRs whose `mergeStateStatus` is not `CLEAN`;
  these must be blocked as rebase/conflict work before waiting on CI or
  promoting to merge-ready.
- Label-gated CI/E2E watch runs that are still active on the latest head but
  stale; later skipped label-event runs must not mask the active same-head run.
- `qa-passed-awaiting-ci` PRs with terminal bad current-head CI/E2E. These emit
  `PR_LOCAL_PREFLIGHT_REQUIRED` unless prompt/proofread/SC/format/LLM-proxy paths
  require fresh capture first, in which case they emit
  `PR_CAPTURE_BEFORE_CI_REQUIRED`.
- `qa-passed-awaiting-ci` PRs whose only red evidence is stale/superseded-head CI
  churn. These now emit `PR_LOCAL_PREFLIGHT_REQUIRED` until the current head has
  affected-test/local-repro PASS proof; stale evidence must not authorize a fresh
  CI run by itself.
- `qa-passed-awaiting-ci` PRs with terminal green CI/test + E2E-style checks and
  clean mergeability; these must be processed through the readiness/merge-ready
  gate before fresh issue dispatch. Do not start a PM Claude merge-ready review;
  merge-ready is decided by the readiness contract.
- `pm-state:pm-review-pending` PRs whose PM Claude marker has passed, blocked, or
  gone stale. Fresh in-flight PM review is quiet for the sweep threshold.
- `pm-state:pm-review-pending` PRs still represented by an occupied MoP/dev
  slot. PM review is PM-owned work and must release the dev slot until a rework
  packet exists.
- Any PR with `pm-blocked:capture` still represented by an occupied MoP/dev
  slot or stale `slot:*` label while capture is only a missing/in-flight remote
  watch. Remote capture watch is PM-owned async work and must release dev-slot
  capacity. Exception: if the current-head or latest stale-head capture has
	  already terminal-failed/cancelled and local capture proof is missing, the PR
	  must emit `PR_CAPTURE_LOCAL_REQUIRED` with action
	  `UNSUPPORTED_LIFECYCLE_ACTION:capture-local-required`. No slot keep/assign
	  transition exists; the default applies and the slot is released. Once
	  local proof exists and the remote capture rerun is launched, release the slot
	  again.
- `qa-passed-awaiting-ci` PRs with terminal green rollup but missing required
  CI/test or E2E evidence; these remain CI classification work, not merge-ready.
- `merge-ready` PRs whose readiness packet is missing/stale, whose current-head
  CI/E2E is not terminal-green, whose mergeability is not clean, or which still
  carry any `pm-blocked:*` label. These must be demoted/reconciled before merge.
- `pm-blocked:rebase` PRs whose live merge state is already `CLEAN`; the stale
  rebase blocker must be removed or replaced with current blocker proof.
- `pm-blocked:ci` PRs in non-CI-watch states when latest-head CI/E2E is terminal
  green; stale CI blockers cannot hide under `blocked-rework` or
  `pm-review-pending`. If the PR is already `pm-state:blocked-rework` and the
  latest head is terminal bad or missing/skipped, the failure is already
  classified: the sweep must route through active owner / idle owner /
  `PR_REWORK_DISPATCH_REQUIRED`, not re-emit CI classification.
- `pm-state:blocked-rework` PRs whose merge state is `DIRTY`/`BEHIND` without a
  rebase blocker.
- `pm-state:blocked-rework` PRs with no slot while free slots exist.
- When multiple exact-head blocked-rework PRs are eligible for unowned capacity,
  emit their dispatch rows oldest PR `createdAt` first; PR number is the
  deterministic tie-breaker. Rework still precedes fresh issue intake.
- `pm-state:blocked-rework` PRs parked behind `pm-blocked:pm-gate` but not
  represented by a live MoP slot; PM must resolve the artifact/decision proof,
  assign narrow forensics, or relabel as `pm-blocked:product` with the exact
  Rajiv/product question and PM recommended default before fresh issue dispatch.
- `pm-state:blocked-rework` PRs parked behind `pm-blocked:product`; these emit
  `PR_PRODUCT_DECISION_WAITING` and must not emit `PR_REWORK_DISPATCH_REQUIRED`.
  Product waits are not invisible parked work: PM must capture/post the exact
  product question, PM recommended default, and Rajiv/product response. Until
  that proof exists, stop-sweep and hourly ops should keep the PR in current-PR
  work ahead of fresh issue dispatch.
- A `pm-blocked:product` PR with an exact active MoP owner is contradictory and
  emits `PR_EXTERNAL_WAIT_SLOT_CONFLICT_REQUIRED` before active-slot handling.
  If the decision has landed, dispatch `Skill(direct-assign)` with the
  durable current-head packet; successful delivery clears the product blocker
  and resolves `product_decision_wait`. If the decision is still pending,
  return `UNSUPPORTED_LIFECYCLE_ACTION:block-pr` and release the slot only
  through `Skill(direct-release)`.
- `pm-state:blocked-rework` PRs that have hit the rework circuit breaker: a
  repeated same finding-class PM/Codex/CTO review marker on the current head, or
  an explicit review-cap marker. Multiple NEW finding classes in a re-review are
  productive rework, not churn, and raw review count must not trip the breaker.
  Rescope/override bookkeeping artifacts are not review rounds and
  must not inflate the circuit breaker. PM must run
  project-local `Skill(pm-codex-pr-rescue)`
  first; Claude/Opus rescue is fallback only when Codex returns
  `ESCALATE_TO_OPUS` or is unavailable. The old PR stops consuming generic
  rework until a terminal rescue/rescope decision exists.
- `pm-state:rescope-required` PRs whose rescope packet is missing or still
  pending. These remain current-PR work until PM chooses final verified patch,
  split-and-reimplement, override-with-evidence, or product escalation.
- QA-cycle states `pm-state:draft-qa-needed`, `pm-state:qa-running`, and
  `pm-state:qa-failed-rework` are valid PM states and must not be reported as
  `unknown_pm_state`. Other stale/contradictory conditions on those PRs may
  still be flagged by their own rules.
- `pm-state:blocked-rework` PRs assigned to an idle slot, with or without DND.
- `pm-state:blocked-rework` PRs with `slot:*` labels that do not match MoP's
  current slot owner by PR, linked issue, or branch.
- `qa-passed-awaiting-ci` / capture-blocked PRs hidden behind a
  `/tmp/pm-dependency-blocked-<PR>.txt` marker whose named blocker has already
  merged or closed.
- `blocked-rework` PRs carrying `pm-blocked:dependency` whose current-head
  required CI/E2E is terminal bad or missing/skipped but parked behind an open
  dependency marker or current-head `ci-verdict` PR comment. This emits once per
  PR/head/dependency/workflow tuple and writes
  `/tmp/pm-dependency-watch-acks/pr-<PR>-<key>.json`; repeated sweeps still log
  `PR_CI_DEPENDENCY_WATCHING` for visibility. If the visible
  `pm-blocked:dependency` label is missing, the sweep emits
  `PR_CI_DEPENDENCY_BLOCKED_REQUIRED` with the deterministic
  `UNSUPPORTED_LIFECYCLE_ACTION:block-pr` typed stop (dependency class,
  `<PR_OR_ISSUE>` recorded as evidence).
- PRs carrying `pm-blocked:dependency` without current-head, machine-readable
  dependency proof. A plain human comment or label is not enough because it
  cannot self-invalidate when the head moves or when the blocker closes. The
  sweep emits `PR_DEPENDENCY_WEDGE_REQUIRED` until PM returns
  `UNSUPPORTED_LIFECYCLE_ACTION:block-pr` for the dependency naming or removes
  `pm-blocked:dependency` and resumes the normal capture/CI path. The dependency transition publishes a
  durable `pm-dependency-watch:v1` PR comment, parks the PR in
  `pm-state:blocked-rework`, and must not apply the CI-triggering
  `pm-state:qa-passed-awaiting-ci` label. When the named dependency reaches
  terminal state, the sweep emits `PR_DEPENDENCY_UNBLOCKED_REQUIRED` with the
  typed stop:
  `UNSUPPORTED_LIFECYCLE_ACTION:dependency-unblocked`; no clears, applies, or
  reconciliation execute. PM must record that stop before claiming the board is quiet.
- `qa-passed-awaiting-ci` PRs with a deliberately classified same-head CI hold
  return `UNSUPPORTED_LIFECYCLE_ACTION:ci-watch` rather than any blocker
  transition; no state changes execute. The marker is valid
  only for the exact current head and failed run named in the proof. Hourly
  sweep emits `PR_CI_HOLD_WATCHING`; the Stop hook does not block on this
  legitimate wait. A changed head or newer failed run requires fresh
  classification, while terminal green resumes normal blocker cleanup and
  readiness promotion.
- Recent draft PRs without any `pm-state:*`, unless MoP establishes one exact
  active-owner tuple: one occupied row with the same PR number and branch. The
  row's slot is authoritative; a PR-side `slot:N` label is not required because
  initial issue claims label the issue, not the later-created PR. This is the
  valid slot-owned implementation/QA window; do not force a PM state change
  mid-phase.
- Recent open non-draft PRs without any `pm-state:*`, with the same exact
  PR+branch exemption while implementation/QA remains slot-owned. Partial
  matches by issue, branch text, or stale slot metadata are not exemptions and
  must still emit reconciliation.

Long-parked PRM calibration and old rebase backlog drafts are skipped by default;
they belong to daily planning unless Rajiv explicitly asks to work them.

## Proof Lines

Actionable output always starts with one of:

```text
PR_SLOT_RELEASE_REQUIRED
PR_READY_PROMOTION_REQUIRED
PR_PM_REVIEW_REQUIRED
PR_PM_REVIEW_COMPLETE_REQUIRED
PR_PM_REVIEW_SLOT_RELEASE_REQUIRED
PR_CI_WATCH_STUCK_REQUIRED
PR_CI_LABEL_RECONCILE_REQUIRED
PR_CI_CLASSIFICATION_REQUIRED
PR_LOCAL_PREFLIGHT_REQUIRED
PR_CI_RERUN_AFTER_PREFLIGHT_REQUIRED
PR_CI_STALE_HEAD_CHURN_REQUIRED
PR_CAPTURE_BEFORE_CI_REQUIRED
PR_CAPTURE_SLOT_RELEASE_REQUIRED
PR_CAPTURE_LOCAL_REQUIRED
PR_CAPTURE_RERUN_AFTER_LOCAL_REQUIRED
PR_CI_DEPENDENCY_BLOCKED_REQUIRED
PR_DEPENDENCY_UNBLOCKED_REQUIRED
PR_STALE_BLOCKER_REQUIRED
PR_REVIEW_CIRCUIT_BREAKER_REQUIRED
PR_RESCOPE_REQUIRED
PR_RESCOPE_EXECUTION_REQUIRED
PR_REWORK_DISPATCH_REQUIRED
PR_REWORK_DELIVERY_PENDING_REQUIRED
PR_PM_GATE_REVIEW_REQUIRED
PR_PRODUCT_DECISION_WAITING
PR_EXTERNAL_WAIT_SLOT_CONFLICT_REQUIRED
PR_ACTIVE_REWORK_IDLE_REQUIRED
PR_STALE_SLOT_LABEL_REQUIRED
PR_STATE_RECONCILE_REQUIRED
PR_DRAFT_ORPHAN_REVIEW_REQUIRED
PR_STATE_LABEL_REQUIRED
```

Clean output:

```text
PR_SWEEP_CLEAN actionable=0
```

Stop-hook dry-runs write `/tmp/pm-required-pr-reconcile.json` when actionable PR
work exists. The sentinel is a replace-only current sweep contract: each write
must replace `actionable_output` with the current sweep rows only. Historical
rows may appear only as non-blocking metadata such as `dropped_prior_prs`; they
must never be carried into the Stop-hook blocking payload. The sentinel is
resolved only when a later sweep is clean.

## Mutation Policy

Dry-run mode mutates nothing:

```bash
~/.claude/skills/pr-state-sweep/scripts/sweep.sh --trigger=pm-stop --dry-run
```

Real mode performs no label deletion and no MoP slot release itself. It
emits `PR_SLOT_RELEASE_REQUIRED` rows that delegate release to
`Skill(direct-release)` with the complete authoritative tuple, records a PM ops
event for the delegation, and otherwise reports audit rows plus required PM
actions upserted into PM ops obligations. The only in-sweep state transition
is the separately preserved family2 pm-review completion boundary.

All other PR transitions are emitted as required PM actions and upserted into PM
ops obligations. `PR_READY_PROMOTION_REQUIRED` is not an auto-flip: PM must run
the readiness contract, write the merge-ready proof with exact current
`headRefOid`, return `UNSUPPORTED_LIFECYCLE_ACTION:validate-ready-proof` and
`UNSUPPORTED_LIFECYCLE_ACTION:merge-ready`; no readiness or merge-ready flip
executes.
`PR_PM_REVIEW_REQUIRED` means PM must start or finish PM Claude
phase-A review; return `UNSUPPORTED_LIFECYCLE_ACTION:pm-review` when review
is in flight. Merge-ready uses `pm-readiness-contract`, not PM Claude review.
`PR_PM_REVIEW_SLOT_RELEASE_REQUIRED` means a dev slot is still held
while PM owns review; drain/release that slot through `Skill(direct-release)`
and only reassign if review returns slot-owned rework. `PR_PM_REVIEW_COMPLETE_REQUIRED` means a phase-A marker passed
and PM returns `UNSUPPORTED_LIFECYCLE_ACTION:pm-review-done`; no move back
to `qa-passed-awaiting-ci` executes. If the marker is
capture-gated, the sweep emits `PR_PM_REVIEW_CAPTURE_REQUIRED` instead; PM must
start/watch fresh-head capture first, and only after
`PR_PM_REVIEW_CAPTURE_COMPLETE_REQUIRED` should PM remove `pm-blocked:capture`
and return `UNSUPPORTED_LIFECYCLE_ACTION:pm-review-done`. If a PR already reached `qa-passed-awaiting-ci` before
that required capture proof, the sweep emits
`PR_PM_REVIEW_CAPTURE_BYPASS_REQUIRED`; PM must move it to
`pm-blocked:capture` and ignore the wrongly-started CI until capture is green.
`PR_CAPTURE_SLOT_RELEASE_REQUIRED` means a capture-blocked PR is still holding a
dev slot through MoP or a `slot:*` label while the only remaining work is remote
capture watch. Release the slot first. It must never be emitted while a matching
live `capture-local-proof` lease owns that slot checkout; that state is reported
as non-actionable `PR_CAPTURE_LOCAL_RUNNING` until the lease PID exits.
`PR_CAPTURE_LOCAL_REQUIRED` is the
exception: remote capture already terminal-failed/cancelled and local proof is
	missing, so the sweep emits `UNSUPPORTED_LIFECYCLE_ACTION:capture-local-required`;
	no slot is kept or assigned and no proof packet is delivered by transition.
	`PR_CAPTURE_RERUN_AFTER_LOCAL_REQUIRED` means the local proof exists;
start exactly one remote capture rerun on the same branch/head, then release the
slot while PM watches the remote capture.
`pm-state:pm-review-pending`
is a holding state, not blocked rework; only stale/missing markers, BLOCKED
markers, or a live dev-slot hold are actionable. `PR_STALE_BLOCKER_REQUIRED`
means a `pm-blocked:*` label no longer matches live GitHub state and must be
removed or replaced with current proof before dispatch.
Project-local `Skill(pm-codex-pr-rescue)` is the only normal first transition
after `PR_REVIEW_CIRCUIT_BREAKER_REQUIRED`. The rescue writes
`/tmp/pm-rescope-pr-N-<head>.md` and returns
`UNSUPPORTED_LIFECYCLE_ACTION:rescope-pr` for the retired state transition;
dev-slot release goes only through `Skill(direct-release)`, and generic rework
dispatch on the old PR stays blocked. `PR_RESCOPE_REQUIRED` means the state is set but
the packet is missing; run `Skill(pm-codex-pr-rescue)`. Use
`Skill(pm-claude-pr-rescue)` only as Opus fallback when Codex returns
`ESCALATE_TO_OPUS` or cannot run.
`PR_RESCOPE_EXECUTION_REQUIRED` means the packet exists but its terminal
decision has no supported executor (`UNSUPPORTED_LIFECYCLE_ACTION:rescope-decide`).
The retired option set was
--decision <final_verified_patch|split_and_reimplement|override_with_evidence|escalate_product_decision>`.
For broad/repeated churn, migrations, fabrication resets, or multiple blocker
classes, PM defaults to `split_and_reimplement` with `--child-plan`; PM must not
punt that choice to Rajiv. `escalate_product_decision` is valid only for a
concrete unresolved product/data-model/authority question and must include PM's
recommended default. No automatic capacity reconciliation exists;
`PR_REWORK_DISPATCH_REQUIRED` rows dispatch only through `Skill(direct-assign)`,
one PR per eligible free slot.
Control-plane integrity defects are escalations too, but not product decisions:
if this sweep proves a helper, hook, MoP read, Slack routing path, capture/CI
gate, or watcher is lying/wedged and one deterministic PM-owned repair cannot
make the state truthful/actionable, PM escalates to Rajiv with PR/issue/slot,
expected transition, actual stuck state, evidence path or command output,
attempted fix, recommended default, and merge-ready/slot-dispatch impact. Do
not ask Rajiv to choose routine ops while the deterministic repair path works.
`PR_REWORK_DELIVERY_PENDING_REQUIRED` means a PM/rework packet exists but neither
`MESSAGE_SLOT_OK` nor slot-side pickup proof is current; PM must deliver the
exact handoff identity only through `Skill(direct-assign)` and keep the blocker
open until `delivery_verified=true`. Do not use `message-slot.sh` for rework
delivery, do not resume a legacy claim_slot/message-slot outbox, and do not
retry after an uncertain or unverified delivery.
`PR_REWORK_PACKET_REQUIRED` means an actionable blocked-rework PR has no durable
packet comment bound to its current head. PM must create the exact packet, but
its recording transition is retired
(`UNSUPPORTED_LIFECYCLE_ACTION:record-rework-packet`); dispatch of that PR
through `Skill(direct-assign)` waits for a supported packet path. This row keeps
priority over fresh `status:todo` dispatch; never substitute a generic packet
reconstructed from labels or stale `/tmp` prose.
PM-gated blocked-rework PRs are not auto-dispatched to a slot, but they are still
hourly current-PR work when no live owner is present. `PR_PM_GATE_REVIEW_REQUIRED`
means PM must produce/verify the missing proof, hand a narrow forensics packet to
a slot only if needed, or relabel as `pm-blocked:product` with an exact
Rajiv/product question plus PM recommended default. Remaining rows still require
PM to execute the named command or explicitly park/escalate.
For issue-only work with no open PR, HOLD/STAND DOWN returns
`UNSUPPORTED_LIFECYCLE_ACTION:park-issue`; no park transition executes and no
ownership changes. Resume only through `status:todo` plus `Skill(direct-assign)`
when a supported path exists.
`PR_LOCAL_PREFLIGHT_REQUIRED` means latest-head CI/E2E failed. PM returns
`UNSUPPORTED_LIFECYCLE_ACTION:block-pr` instead of blocking the PR, sends the
failed suite/spec to the owning slot, and requires
current-head local preflight PASS proof before rerunning CI. Wall-budget/timeouts
are treated as PR-local stuck/slow regressions until local proof or a fresh
capture proves the real path. Flake/pre-existing/no-local-equivalent exceptions
only count if the proof cites a follow-up issue. `PR_CI_RERUN_AFTER_PREFLIGHT_REQUIRED`
means that proof exists; rerun label-gated CI only with
`/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/ci/rerun-after-local-proof.sh --pr <PR> --run <RUN_ID> --proof <proof>`.
Stale/superseded bad evidence
does not authorize a fresh CI run without current-head local preflight proof.
`PR_CAPTURE_BEFORE_CI_REQUIRED` means prompt/proofread/SC/format or LLM-proxy
changes require fresh-head capture before any CI rerun, and the original CI/E2E
rerun after capture green still requires current-head local preflight proof.
`PR_CI_DEPENDENCY_BLOCKED_REQUIRED` means the dependency wait is not yet visibly
represented; PM returns `UNSUPPORTED_LIFECYCLE_ACTION:block-pr` instead of
parking the PR; no state change, no slot action, and no CI trigger. PM still
records current-head dependency proof in a PR comment and keeps watching the
named dependency; label-gated CI starts only after it merges or closes. The same open watch
remains visible as `PR_CI_DEPENDENCY_WATCHING`, but no longer blocks stop-hook
completion once the visible label and payload exist.
`PR_DEPENDENCY_WEDGE_REQUIRED` means the label exists but the current-head
dependency proof is missing, stale, or unparsable. Stop-hook should treat that
as actionable PM cleanup, not a quiet wait: PM must either return
`UNSUPPORTED_LIFECYCLE_ACTION:block-pr` for the blocker naming or remove the
dependency label and continue capture/CI/rework.
`PR_DEPENDENCY_UNBLOCKED_REQUIRED` is never suppressed and must fire immediately
when the dependency reaches terminal state. PM action is
`UNSUPPORTED_LIFECYCLE_ACTION:dependency-unblocked`; none of the clears,
applies, resolves, or reconciliation execute.
Already-classified
`pm-state:blocked-rework + pm-blocked:ci` terminal-bad PRs are rework routing
work, not CI classification work: keep live slot owners quiet, nudge idle
owners, or dispatch to an eligible free slot.

## Integration Points

- PM stop hook runs PR sweep first. If PR sweep has actionable output, stop is
  blocked before any issue dispatch runs.
- No automatic capacity reconciliation exists. PM dispatches only through
  `Skill(direct-assign)`, and only when the remaining PR sweep is clean.
- Hourly ops should treat `/tmp/pm-required-pr-reconcile.json` exactly like the
  dispatch sentinel: a real PM action is required before all-clear.
