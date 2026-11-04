---
name: pm-triage
description: Individually reconcile HeyDonna issues, Ready Pool membership, live dependencies, priorities, ownership, and PM obligations.
---

# HeyDonna issue triage

These rules incorporate Rajiv's October 3/11 and November 4 directives. They
supersede earlier pool caps, blanket P3 exclusions, promoter architecture-review
prerequisites, and dependency-to-backlog migrations. AGENTS.md and newer explicit
Rajiv directives take precedence.

## Individual assessment

Read every issue's full body, relevant comments, original directive, linked PRs,
current-main implementation, and live MoP ownership. Use Recall with `--no-index`
for prior decisions. Never decide from a title, shared actor, keyword match, or a
bulk labeling operation.

First reconcile active PR/slot ownership to the correct execution/review status.
Then check main, merged PRs, symbols and tests for delivered, duplicate, superseded,
or obsolete work. Close only with cited evidence, preserving explicit later
reopens and customer-verification wakes. Administrative closure is not delivery.
For partial delivery, name the exact remaining AC before re-queuing.

Every issue gets one disposition: PROMOTE, CLOSE, dependency-held pool membership,
other BLOCKED/PARKED hold, or NEEDS DECISION with an exact question and default.
No unexplained holding state or generic needs-triage label.

## Contract and slice

Preserve the original directive, intended capability, production failure, actual
runtime control point, scope, positive and negative ACs, forbidden approaches,
and smallest production-path proof. Require a valid Issue Contract Ledger (ICL)
and complete Ready Pool metadata. Repair from existing evidence in this run;
never invent ACs, rewrite away constraints, or silently weaken acceptance.

A slice has at most THREE ACs, is independently shippable, and takes half a day
or less. Split broader work before assignment. Heavy QA/capture/model experiments
may need their own slice. A P1 umbrella keeps its next child pre-split and ready.
The parent/tracker remains nonclaimable.

Customer/file/artifact issues need a grounded investigation before root-cause
claims or implementation ACs. Use actual source files, runs, logs and artifacts;
report insufficient evidence as one concrete missing identifier/question. For
legal SC/proofread prompt changes, obey AGENTS.md's artifact-derived examples.

Each AC has a stable ID, Surface, QA reachability, and Required proof. Deterministic
visual ACs name a screenshot scenario and durable evidence; nondeterministic
visual ACs need a concrete exemption and alternate proof. Unit/integration proof
is preferred; E2E is reserved for critical workflows.

## Ready Pool versus assignment

Pool membership and execution eligibility are separate. `status:todo` includes
bounded app-code/app-test work with named dependencies at EVERY priority. Preserve
canonical dependency refs and `pm-blocked:dependency`; the selector checks them
live immediately before assignment. Never turn a named dependency into `none`
merely to admit it, and never demote a Todo solely because its dependency is open.
A dependency membership is not permission to execute. Unknown/unparseable/live
requirements remain gated; resolved predecessors with successor/proof gates are
not automatically satisfied.

Ten currently claimable, unassigned issues is the MINIMUM, never a ceiling.
Promote ALL eligible P0/P1/P2 issues even above ten. After those, if the claimable
pool is below ten, triage runnable P3s to fill the deficit. Close stale P3s,
correct genuinely wrong priority with evidence, promote eligible small slices,
or retain with an explicit reason. Honor attributable issue-specific parks until
a newer decision supersedes them. No blanket P3 ban and no promotion of surplus
P3s. Report total pool members separately from currently claimable and held work.

The backlog promoter has NO Codex architecture-review prerequisite (Rajiv Oct 11).
Do not create fake markers to satisfy retired checks. This does not waive the
separate implementation-plan, code-review, or release-safety requirements.

Ready Pool fields: `priority`, `lane`, `branch_slug`, `ac_summary` (<=140 chars),
`claimable_slot_type`, `blockers`, `work_type`; keep required validation, owner,
next action, wake condition, disposition and application state explicit. Named
blockers use `#N` or `[#N, #M]`; `none` means no dependency. Branch slugs are <=40
lowercase alphanumeric/hyphen characters. Metadata must match the issue contract.

## Priority and dispatch

Exactly one canonical P0/P1/P2/P3 label must match the body and tracked-feature
priority. P0 is a verified critical incident; P1 covers bugs/user-visible lag and
user/data/export harm; P2 covers benchmarks and agency/workspace features; P3 is
low-priority bounded work. Academy, multi-doc upload, and speaker mapping/sidebar
are P2. PRM was dropped for now; routine triage must not revive it.

PM uses `~/.claude/scripts/ready_pool_select.py` and its `--check <issue>` before
canonical `direct-assign`. Order is P0>P1>P2>P3, then rework>repro>new_issue, then
oldest. Live dependencies are checked for selection AND assignment. PM alone
operates numbered slots. Ready P0/P1 work with no available slot starts through
PM's existing pm-hotfix path that turn. Other ready work remains visibly queued;
lack of a slot is not a backlog rationale.

## Other holds and operational routing

On every promoter run inspect no-status/no-priority intake, blocked/deferred
issues, and stale PM holds as well as the P1/P2 backlog. A hold needs blocker
type, owner, next action and concrete wake; parking needs parent/blocker,
unblock condition and owner/next reviewer. Reverify holds older than seven days
against live state. PM-owned gates require an actual PM action. Batch genuine
product questions to CTO with defaults; routine bookkeeping is not a new decision.

Internal control-plane, provisioning, DNS, docs-only and PM-ops work belongs in
operational obligations with owner/action/wake, outside app Ready Pool. Actual
application runtime or executable test-harness/CI-producer fixes remain development
work regardless of a stale infra label. Tracking, investigations, external actions,
unresolved product decisions and direct-main work are nonclaimable. Production-DB
effects are PM-only; preparation is distinct from authorized execution.

## State, commands and completion

GitHub is the label store; `pm-ops.db` is the workflow ledger; `pm-todo.md` is its
generated projection, not a separate queue to hand-edit. Keep exactly one coherent
status, one priority and the prescribed release gate. Update obligations and sync
with `python3 ~/.claude/scripts/pm-ops.py sync --write --no-live --reason pm-triage`.
GitHub Projects membership is not required.

Read-only audit: `python3 ~/.claude/scripts/backlog-triage.py --dry-run`.
Promotion detector: `python3 ~/.claude/scripts/backlog-triage.py promote --dry-run`.
Pool contract audit: `python3 ~/.claude/scripts/backlog-triage.py audit-ready-pool`.
Actual dispatch check: `python3 ~/.claude/scripts/backlog-triage.py check-dispatch-readiness --issues N`.
Use that last command for assignment eligibility, not dependency-held membership.
Existing Todo metadata repair: `backlog-triage.py repair-ready-pool --apply
--metadata-json <hash-pinned evidence proposal> --issues N`. Run its dry-run first;
`review_proof` cites source evidence and does not require a reviewer. This repairs
membership in place and preserves dependency/wake constraints.
The old `--migrate-blocked-todos` flag is a compatibility no-op under S113.

A dry run or agent launch is not completion. Record actual promoted/closed/held/
decision outcomes, issue numbers, owner/wake/evidence, total versus claimable counts
by priority, manifest paths, failures, and whether labels changed. Label hygiene
alone is not completed triage. Stop when the requested outcome and useful proof
are satisfied; process-only gaps must not delay safe customer recovery.
