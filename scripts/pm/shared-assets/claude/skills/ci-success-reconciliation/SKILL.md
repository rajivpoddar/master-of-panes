---
name: ci-success-reconciliation
description: Reconcile a terminal green HeyDonna CI and E2E event into the typed PM state machine. Use when HeyDonna Alerts posts CI + E2E success, the CI-success watchdog reports a green PR still in qa-passed-awaiting-ci or blocked by stale CI state, or pr-state-sweep emits PR_READY_PROMOTION_REQUIRED. Verifies exact-head workflows and readiness, then records one durable CTO merge-ready handoff or an explicit fail-closed blocker disposition.
---

# CI Success Reconciliation

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


Consume one terminal green CI event completely. Do not leave a green PR in
`pm-state:qa-passed-awaiting-ci` and do not edit labels directly.

## Inputs

Require:

- `pr=<number>`
- `run_id=<number|unknown>`
- `alert_thread_ts=<timestamp|unknown>`

Use `/tmp/pm-required-ci-reconcile-<PR>.json` as the durable event when present.
The event may come from Slack ingress or the independent green-CI watchdog.

## Workflow

1. Claim the event before mutating state:

   ```bash
   python3 /Users/rajiv/.claude/scripts/ci-success-reconciliation.py \
     --claim --pr <PR>
   ```

   If the claim reports `superseded`, stop without changing the PR. If it
   reports another active claim, re-read live state and avoid a duplicate
   transition.

2. Read live PR state and bind every decision to the current head:

   ```bash
   gh pr view <PR> --repo heydonna-app/heydonna-app \
     --json state,isDraft,headRefName,headRefOid,labels,mergeStateStatus,mergeable
   /Users/rajiv/Downloads/projects/heydonna-app/scripts/ci/pre-merge-current-head-ci-guard.sh --mode promotion <PR>
   ```

   The guard must run in promotion mode (`--mode promotion`): it verifies the
   PR's own exact-head `pull_request` CI/E2E required jobs and the exact-head
   proof gates but does NOT consult current-main CI/E2E health — a red main
   run never suppresses a terminal-green promotion. Current-main health stays
   a default-mode merge gate for CTO merge safety.

   For app, product, mixed, empty, or unknown scope, the guard must pass and
   name successful real `CI` and `E2E Smoke Tests` `pull_request` runs on the
   exact `headRefOid`. `statusCheckRollup` alone is never sufficient.

   A green `Change Scope Classifier` workflow, classifier job, or validation job
   is never exemption proof: all of them also succeed for product and mixed
   diffs. Accept the control-plane exemption only when the exact-head guard
   itself reports `exemption=control_plane_only`; that output is backed by the
   shared classifier tuple `control_plane_only=true`,
   `product_changed=false`, `ci_required=false`, and `e2e_required=false`.
   If the head changed after a prior real CI/E2E success and the new head is not
   exempt, treat the old success as stale and use the typed re-arm path.

   Admission must have run on a PR head containing the then-current exact
   `origin/main`; non-overlap never exempts that admission integration. If main
   advances after those genuine exact-head runs, the final merge owner audits
   only the later main-only delta. Unrelated later movement preserves the
   exact-head evidence; overlapping or ambiguous movement requires main
   integration and fresh CI/E2E. The guarded head-pinned PR merge remains a
   post-admission action, never part of admission itself.

3. Re-query live Codex review threads and active capture workflows. Never use a
   stale Slack summary or marker as the current result. Any current-head capture
   or strict-replay requirement must already have terminal proof.

4. Invoke project-local `Skill(pm-readiness-contract)` for this exact PR/head.
   Do not run another Phase-A code review merely because CI became green.

5. Apply the ownership handoff rule only after the exact-head readiness checks
   above. Ownership is not a terminal CI disposition: a CTO/PR-merges/rescue
   owner must never be converted to `superseded` merely because that owner
   performs the final merge. For an exact-head green and ready PR, emit the
   non-persisted instruction below in the existing alert thread, then hand off
   without creating a new PM state or blocker:

   ```text
   CI_SUCCESS_CTO_RELAY_REQUIRED pr=<PR> head=<full SHA> ci=<CI run IDs> e2e=<E2E run IDs> owner=cto|pr-merges|rescue thread=<alert_thread_ts>
   ```

   The relay MUST mention `<@U0BNFGX2UAX>` and preserve `alert_thread_ts` so
   the CTO bridge wakes. Include the exact repository/PR/head and both real
   workflow run IDs. PR-merges may guard and merge directly under its existing
   head-pinned path; do not require another review solely because of ownership
   when Rajiv's portfolio rule delegates final merge to PR-merges. Normal
   PM-owned exact-head green still continues to the direct durable merge-ready
   handoff in
   step 6. Only genuine head drift uses `CI_SUCCESS_SUPERSEDED`; real
   product/capture/review-thread/readiness failures remain typed blocked.

6. On `READY_PACKET: PASS`:

   - execute the single canonical reconciliation command. It re-reads the
     live OPEN/non-draft exact head, runs the existing exact-head CI+E2E and
     readiness gates, and records one durable event plus one CTO obligation.
     That durable handoff is the delivery authority; an optional notify
     transport posts the already-bound handoff once when configured and never
     mutates PR state. It does not edit labels or create merge authority:

     ```bash
     python3 /Users/rajiv/.claude/scripts/ci-success-reconciliation.py \
     --resolve --pr <PR> --resolution merge_ready
     ```

   - the resolver first verifies the exact-head phase-a review marker binds
     the current head (guard row 3) and the PR is still OPEN/non-draft; a
     missing, stale, merged, synthetic, or mismatched head refuses before
     ledger/notification effects;
   - the durable handoff records PR, full head, real CI/E2E run IDs, source
     thread/destination, and CTO mention. A replay of the same exact-head
     resolution returns the existing receipt without a second event,
     obligation, or notification;

7. On `READY_PACKET: BLOCKED`, do not leave an implicit wait:

   - pass only the exact readiness packet as `--proof`; the resolver requires
     `READY_PACKET: BLOCKED`, matching `PR:` and `headRefOid:` lines, and one
     recognized typed `blocker:` (`codex`, `capture`, `rebase`, `ci`,
     `product`, `infra`, or `pm-gate`); prose receipts are rejected;
   - identify exactly one current blocker owner and proof;
   - return `UNSUPPORTED_LIFECYCLE_ACTION:block-pr` with the matching typed
     reason (`codex`, `capture`, `rebase`, `ci`, `product`, `infra`, or `pm-gate`)
     instead of applying any blocker transition, then continue to the durable
     blocked-proof resolution below;
   - never rerun CI, recapture, operate a slot, or remove/re-add labels;
   - when the only blocker is one or more non-gating Codex P2 threads that lack
     linked follow-up issues, do not assign product rework. Resolve with the
     explicit successor action so the PM-owned obligation is created before the
     CI event becomes terminal:

     The packet's blocker line must be
     `blocker: codex - deferred P2 has no follow-up issue`; the resolver rejects
     `codex_followup` for every other blocker, including branch currency.

     ```bash
     python3 /Users/rajiv/.claude/scripts/ci-success-reconciliation.py \
       --resolve --pr <PR> --resolution blocked --proof <proof-path> \
       --blocked-next-action codex_followup
     ```

     The command must fail closed if `pm-ops` cannot durably upsert the
     `codex_followup_reconcile` obligation. That obligation invokes
     `Skill(codex-comment-processing)` to re-read the exact-head threads, file
     and link concrete follow-up issues for accepted P2 deferrals, reply inline,
     resolve the threads, and rerun readiness. If the head is unchanged and
     readiness passes, use the single canonical promotion command above; do
     not rerun CI or edit labels directly. That `--resolve --resolution
     merge_ready` closes the successor obligation and promotes once.
   - for every other blocker, resolve the event as blocked with the durable
     blocker proof:

     ```bash
     python3 /Users/rajiv/.claude/scripts/ci-success-reconciliation.py \
       --resolve --pr <PR> --resolution blocked --proof <proof-path>
     ```

8. Reply in `alert_thread_ts` when it is known. Report the exact head, CI/E2E
   run IDs, readiness verdict, transition receipt, and either `merge-ready` or
   the typed blocker. Use the approved HeyDonna user-token Slack procedure.

## Fail-Closed Rules

- Head drift after the event means `superseded`, never promotion.
- CTO/PR-merges/rescue ownership alone never means `superseded`; relay an
  exact-head green/ready tuple to `<@U0BNFGX2UAX>` in the source thread and
  hand off through the existing PR-merges/CTO path.
- Draft, dirty/conflicting mergeability, missing real workflows, unresolved
  Codex threads, active capture, missing required strict replay, or incomplete
  product proof means `READY_PACKET: BLOCKED`.
- A deferred P2 counts as cleared only when its inline thread is resolved and
  links a concrete follow-up issue accepted by the readiness contract.
- A deferred-P2-only block is not terminally handed off until the PM-owned
  `codex_followup_reconcile` successor obligation is durable and immediately
  actionable by the existing PM-ops Stop validator.
- Existing `merge-ready` on the same exact head is an idempotent success; verify
  the durable handoff and resolve the event without reapplying labels.
- Never synthesize readiness proof. Only the exact-head guard and
  `pm-readiness-contract` may produce the evidence consumed by the direct
  handoff.
- Any later canonical pre-merge-guard or head-pinned-merge refusal is a
  mandatory one-time CTO DM to Rajiv in `D0BPG55FG72`, bound to the exact
  PR/head/main tuple and literal refusal with one recommendation. Deduplicate
  unchanged refusal tuples. The DM requirement does not itself authorize an
  override.

## Terminal Output

Return exactly one disposition after the handoff instruction has been emitted
when applicable. The relay line is an instruction, not a persisted PM state:

```text
CI_SUCCESS_RECONCILED pr=<PR> head=<SHA> transition=merge_ready_handoff proof=<path>
```

or:

```text
CI_SUCCESS_BLOCKED pr=<PR> head=<SHA> blocker=<typed-reason> proof=<path>
```

or:

```text
CI_SUCCESS_SUPERSEDED pr=<PR> event_head=<SHA> current_head=<SHA>
```
