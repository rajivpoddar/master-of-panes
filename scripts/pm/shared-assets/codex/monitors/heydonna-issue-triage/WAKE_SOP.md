# HeyDonna issue-triage wake SOP

## Purpose

This SOP governs one wake of the heartbeat automation
`heydonna-3h-ready-pool-reconciliation` in issue-triage task
`019f5f2c-48c1-7bc2-816e-6bcf67852204`.

The automation prompt supplies cadence and mutation authority. This file is the
canonical consumption procedure. Read it completely before any GitHub query or
mutation. `AGENTS.md`, a newer explicit Rajiv directive, and live
product/data-safety evidence take precedence if they conflict with this SOP.

A detector result, old rejection, label, issue comment, or remembered count is
not live truth. Every decision must be rebound to current GitHub, repository,
workflow, dependency, and implementation state.

## Required outcome

The wake is terminal only when:

- every intake issue is classified;
- every open issue has exactly one `status:*` label and one canonical priority;
- every Todo is bounded, dependency-free, dispatch-valid app-code/app-test
  work claimable by a numbered slot;
- every dependency wait names only live blockers and each chain's current head
  is Todo or has a typed non-Todo hold;
- no eligible bounded backlog candidate remains parked;
- all changed bodies pass the canonical issue-contract validator;
- required backlog/Ready Pool audit buckets are zero; and
- one compact terminal receipt is delivered to the active CTO decisions task,
  which owns any required PM relay.

Ten clean Todos is a minimum capacity target, never a maximum. Promote every
eligible bounded candidate found in the pinned surface.

## Authority and hard boundaries

The issue-triage wake may mutate GitHub issue bodies, titles, labels,
priorities, and issue workflow state. It must not:

- edit repository files, create branches/PRs, merge, deploy, or run migrations;
- operate, clear, interrupt, or message numbered dev slots;
- execute production repairs or migration apply steps;
- ask PM to triage, repair contracts, repair dependencies, or promote issues;
- file an internal control-plane defect as app work; or
- infer authority from the shared GitHub actor.

MoP, slot, admission, workflow, Slack, and `/tmp` inspection is read-only.
Newly proven control-plane defects are reported in the Slack receipt unless
Rajiv explicitly authorizes an existing control-plane task handoff.

## One-wake consumption sequence

### 1. Establish the intake boundary

Use the previous successful heartbeat as the start. On the first run, use the
preceding three hours. Intake is creation/reopen time based, not `updatedAt`
based. Include unlabeled issues.

### 2. Build one pinned snapshot

Query live GitHub and pin exactly once:

- every intake issue;
- every open `status:todo` issue;
- every open `pm-blocked:dependency` issue;
- every open issue with zero or more than one `status:*` label; and
- every full-surface backlog promotion candidate.

Do not replace or silently expand this snapshot later in the wake.

Run:

```bash
python3 scripts/pm/control-plane/backlog-triage.py --dry-run --surface full
```

This is a detector, not authority. Read complete live state for every issue
that may be mutated.

### 3. Review every pinned issue once

Bind the review to body SHA, relevant-label fingerprint, and dependency
fingerprint. Read:

- complete title/body/comments and timeline where material;
- linked issues and PRs, including the current PR head and real required runs;
- current implementation state and current-main ancestry;
- repository control points and focused tests already present;
- relevant read-only slot/MoP/admission logs and `/tmp` artifacts; and
- the original Rajiv/customer directive when applicable.

Review directive fidelity, intended capability, production failure mode,
runtime control point, boundedness, positive/negative ACs, proof, priority,
branch slug, and slot suitability.

For new intake, attribute the underlying source as PM, slot 1/2/3/4,
CTO/Codex, customer/production alert, automation, or unknown using concrete
timestamps and artifacts. Never infer the source from `rajivpoddar` or another
shared actor alone.

### 4. Assign exactly one disposition

#### `PROMOTE`

Use only for one bounded, executable, dependency-free app-code or app-test
change suitable for a numbered slot. Repair the complete contract, remove
backlog/blocker labels, and add `status:todo`.

App-CI producer/proof work is app-test work when bounded and therefore requires
a numbered slot. This includes CI/E2E workflow behavior, fixtures/capture
producers, promotion proof, classifiers, test planners, watchdogs, harnesses,
and deterministic test infrastructure.

Migration implementation/test work may be Todo, but production execution
remains CTO-owned and separately authorized.

#### `DEPENDENCY_WAIT`

Use `status:backlog + pm-blocked:dependency` for otherwise claimable work with
a live predecessor or proof gate. Record exact issue/PR blockers, owner, next
action, wake condition, evidence, and fingerprint.

#### Typed non-Todo hold

Keep tracking, parent/umbrella, investigation, reproduction, docs-only,
external, direct production repair, direct-main, unresolved product/CTO,
deferred, and internal control-plane work out of Todo. Record one structured
reason, owner, next action, wake condition, evidence, and fingerprint.

Classify by runtime control point, not path name. App-CI producer/proof changes
go to slots. Orchestration, admission, MoP, receipt, and autoscaler ownership is
control-plane work. Rajiv-routed autoscaler obligations belong to the sole
bounded control-plane implementation task `01a04154-c9c1-7bc1-8f7b-009a87bc7628`,
never Todo. That task returns a candidate-only packet to CTO Decisions; it
does not publish or deploy.

#### `CLOSE`

Close as completed only with unambiguous live proof: merged/current-main
implementation plus required validation, a canonical duplicate, or a
conclusively superseding implementation. A stale open issue can already be
implemented; verify ancestry and focused proof instead of dispatching duplicate
work.

Before applying that rule, re-read the current issue lifecycle state after the
ancestry/validation check. Merged ancestry or deployed validation is readiness
evidence, never a close command. A later deliberate reopen is authoritative: if
the issue is still `OPEN` and carries `status:todo`, `status:in-progress`, or
`status:in-review` together with an explicit customer-verification artifact,
template, or reporter wake, keep it open and preserve that nonterminal state.
Do not replace it with `status:done`, remove its active labels, or close it from
ancestor evidence. Close only when the current state contains an explicit
terminal-close intent or the required customer-verification terminal is
verified. Outside this protected class, preserve the existing duplicate or
completed-close path when its live proof is unambiguous. Missing, stale, or
contradictory lifecycle/wake evidence fails closed and remains open with one
owner, next action, and wake.

The close discriminator is intentionally machine-readable so the heartbeat
automation cannot reconstruct a weaker rule from prose:

```json
{
  "name": "customer_verification_reopen_close_guard",
  "version": 1,
  "merged_ancestry_is_not_terminal": true,
  "protected_open_statuses": ["status:todo", "status:in-progress", "status:in-review"],
  "requires_customer_verification_wake": true,
  "nonterminal_action": "keep_open_preserve_status_and_labels",
  "terminal_authority": "explicit_terminal_close_intent_or_verified_customer_verification_terminal_or_unambiguous_completion_or_duplicate",
  "idempotent": true,
  "ambiguous_state": "fail_closed_keep_open"
}
```

If Rajiv explicitly orders an unresolved issue closed, close it as
`not planned`, remove active backlog/blocker labels, and state which repair,
customer reply, migration, or validation was not performed. Never claim
completion from administrative closure.

## Ready Pool contract

Every Todo must have one valid Issue Contract Ledger and one canonical Ready
Pool block containing:

- `priority`, `lane`, `ac_summary` (maximum 140 characters);
- `claimable_slot_type`, `blockers`, `work_type`, `branch_slug`;
- `required_validation`, `owner`, `next_action`, `wake_condition`;
- `disposition`, `application_state`, `reason_class`, `evidence`, and
  `fingerprint`.

Todo invariants:

- exactly one `status:todo` label;
- exactly one matching P0/P1/P2/P3 label;
- `blockers: none`, no dependency label, and no PM blocker label;
- bounded app-code/app-test work only; and
- `disposition: PROMOTE`, a valid branch slug, and focused validation.

Priority rules:

- P0: verified active critical incident;
- P1: live-user harm, bug, data/export risk, or user-visible lag;
- P2: PRM, benchmarking, agency/workspace, and ordinary bounded improvements;
- P3: genuinely low-priority bounded work.

Body priority and label must match exactly.

## Dependency graph procedure

1. Parse canonical Ready Pool `blockers`; it wins over incidental prose.
2. Verify every referenced issue and PR live. Bind PR blockers to the current
   head and real required proof; repair obsolete head pins.
3. Reconstruct the whole chain/DAG, including nested proof and PM/CTO gates.
4. Closure alone does not clear a blocker. Check split/superseding children and
   required artifacts, production receipts, merge, and deploy gates.
5. Identify each current head:
   - bounded app-code/app-test with predecessors and proof complete: promote;
   - open PR: keep waiting for exact-head proof/merge/deploy as contracted;
   - control-plane/product/PM-gate head: typed non-Todo hold with actual owner;
   - tracker with claimable children: keep tracker nonclaimable and promote the
     earliest dependency-free child.
6. Repair contradictory prose and stale wake conditions after deciding the
   live graph.

Never finish with an eligible head in backlog, a closed/nonexistent stale
blocker, a blocked Todo, or a downstream child ahead of an open predecessor.

## Repository-wide status hygiene

On every wake, query all open issues, not only intake. Repair zero/multiple
status labels directly after reading the live issue:

- clean bounded app work -> fully repaired Todo;
- live dependency -> dependency wait;
- low-priority explicitly parked work -> `status:deferred`;
- active execution/review/QA -> its one live lifecycle status;
- tracking/investigation/external/production-operation/control-plane/product
  decision -> typed non-Todo status.

The wake cannot finish while any open issue has zero or multiple statuses.

For an explicit manual last-four-weeks audit, use creation time. Report closed
unlabeled issues separately; they are not missed open Ready Pool work.

## Validation

Validate every changed body:

```bash
python3 scripts/pm/control-plane/validate-issue-contract-ledger.py \
  --json --require-qa-proof
```

Validate changed/promoted Todos:

```bash
python3 scripts/pm/control-plane/backlog-triage.py \
  check-dispatch-readiness --issues <ISSUES...>
```

Then rerun:

```bash
python3 scripts/pm/control-plane/backlog-triage.py --dry-run --surface full
```

Required zero buckets:

- `status_hygiene`
- `backlog_promote_candidate`
- `todo_dispatch_readiness_failed`
- `todo_missing_ready_pool`
- `todo_priority_label_drift`
- `todo_blocked_dependency`

Any mutation failure or nonzero required bucket is `ACTION REQUIRED`; name the
exact issue and failure. Never stop at diagnosis or call a proposal complete.

## CTO decisions handoff and PM relay

After every wake, read and use the complete
`/Users/rajiv/.codex/skills/codex-stdio-send-message/SKILL.md` contract. Send
one exact task-to-task message to the active CTO decisions task
`01a03236-2e61-71f3-a6a8-3dc24d8c8917` through the bundled app-server stdio
helper. Do not use renderer-mediated `codex_app` messaging, and do not post the
reconciliation directly to Slack or PM from this issue-triage task.

The message must begin `ISSUE_TRIAGE_RECONCILIATION_TERMINAL` and include:

- `source_task=019f5f2c-48c1-7bc2-816e-6bcf67852204`;
- a stable dedup key
  `issue-triage:<completed-wake-UTC-timestamp>:terminal:v1`, reused unchanged
  for any reconciliation of the same logical delivery;
- `relay_required=PM`;
- `relay_instruction=Consume this terminal, adjudicate any CTO question, then
  relay the compact outcome to PM in the canonical PM Slack thread as Abhijit
  CTO.`; and
- the compact receipt below.

Target eight receipt lines and 1,500 characters maximum. Include only:

- CLEAN or ACTION REQUIRED and final P0/P1/P2/P3 Ready Pool counts;
- intake and untriaged-status counts;
- compact new-issue source-to-disposition entries;
- repaired/promoted/closed issue numbers;
- promoted dependency heads and remaining-wait count;
- genuine CTO questions or exact failures; and
- final audit verdict.

Do not include titles, full chains, unchanged inventories, detailed evidence,
manifest paths, or chronology. The CTO decisions task owns Slack identity,
thread selection, read-back verification, and the returned Slack timestamp.
The triage wake must record the stdio helper receipt in this task, including
`status`, `threadId`, `queuedSubmissionId`, `clientUserMessageId`, and
`startAccepted` when emitted. `delivered` and `queued_for_task_consumption` are
terminal acceptance receipts; do not poll the destination. `queued`,
`unavailable`, or `uncertain` must be reported exactly under the skill contract
without retrying under a new key or falling back to another transport.

## Fail-closed rules

- Never trust stale snapshots, old promoter decisions, shared actors, or memory
  over live state.
- Never promote from blocker closure alone when a successor/proof gate exists.
- Never dispatch tracking, investigation, production-operation, direct-main,
  or control-plane work to a numbered slot.
- Never classify app-CI producer/proof changes as control-plane solely because
  of their path.
- Never close unresolved work as completed.
- Never silently skip a failed mutation or invalid contract.
- Never finish without the final full audit and a canonical stdio delivery
  receipt for the CTO-decisions handoff.
