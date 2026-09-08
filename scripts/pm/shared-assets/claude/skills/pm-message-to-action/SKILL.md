---
name: pm-message-to-action
description: Current PM message/event to existing action-path mapping.
---

# PM message-to-action SOP

This is the current mapping used by the PM. Start with an explicit event or
source envelope, then use the named existing action path. The reminder hook is
advisory only: it does not classify message text, select a skill, create an
obligation, or write a sentinel. Quoted history, forwarded text, and prior
terminal output are context, not a new event.

## Current mapping

| Explicit message/event | Existing action | Minimal example | Relevant exclusions and required follow-through |
| --- | --- | --- | --- |
| Slot nudge or assignment notification | `Skill(pm-nudge-processing)` | `NUDGE: slot 4 (...)` | Use the actual slot event and exact tuple. Do not treat ordinary slot discussion or quoted nudge text as a new nudge; keep the existing slot/lease and delivery bookkeeping in this action. |
| Codex bot inline review | `Skill(codex-comment-processing)` | A `bot:codex` inline review comment with its discussion reference | Use the actual review comment/reference, not a human summary or quoted review. Preserve the existing review follow-through and deduplication. |
| Feedback diagnostics submission | `Skill(alert-processing)` Phase 0b diagnostics path | HeyDonna Alerts feedback with an explicit `Diag`/`Diagnostics` submission | This is the diagnostics subprotocol, not a generic CI failure or a customer claim inferred from prose. Retain the existing alert-thread evidence and terminal reply path. |
| Production or processing alert | `Skill(alert-processing)` | A HeyDonna Alerts production alert or an explicit stuck/processing/validator failure | CI/E2E terminal failures and runner-pool capacity alerts use their own rows. Preserve the existing P0/SEV0 escalation obligation and alert-thread follow-through when that action requires it. |
| CI/E2E failure terminal | `Skill(ci-failure-investigation)` | An exact workflow failure alert with run, thread, PR, and head evidence | Exclude optional E2E Large File Correctness and an explicitly selected diagnostic. Verify the exact run/head before rework; retain the existing `ci_reconcile` record, evidence, sentinel, and pending-intake bookkeeping. |
| CI/E2E success terminal | `Skill(ci-success-reconciliation)` | An exact green CI+E2E terminal for a PR | Do not treat a generic green context or quoted success as current. Verify latest-head readiness and retain the existing CI terminal record, evidence, sentinel, and pending-intake bookkeeping. |
| E2E Capture terminal | `Skill(capture-alert-processing)` | Typed Capture success/failure/cancellation with run, PR, branch, head, verdict, and alert thread | This is the typed remote-capture path, not ordinary CI. Preserve the existing capture record/obligation, sentinel, exact-head evidence, and one CTO relay keyed to the originating thread. |
| PMF survey report | `Skill(survey-report-prompt-miner)` | A survey response from the configured HeyDonna Alerts survey source | Do not mine ordinary customer reports as surveys. Keep the existing survey-thread evidence, prompt-mining obligation, and report follow-through. |
| External customer report or post-fix recurrence | `Skill(customer-artifact-investigator)` | A customer/user report with its project context, or an external post-fix recurrence | Internal PM/CTO diagnostics and quoted historical text are not customer events. Preserve the existing customer artifact, recurrence obligation where applicable, and source-thread follow-through. |
| CI runner-pool stall | `Skill(pm-autoscaler-repair)` | An explicit job-level runner-pool stall | This is capacity/autoscaler work, not a customer pipeline failure or a slot assignment. Use the existing runner evidence and repair handoff. |
| CTO PR sweep or intake | `Skill(pr-state-sweep)` | An explicit merge-ready, intake, or PR-state sweep directive | This is pre-dispatch reconciliation, not ordinary PR status prose. Keep exact-head, body, label, and dependency checks and the existing sweep bookkeeping. |
| Slot-requested PR/review-cap rescue | `Skill(pm-pr-rescue)` | A concrete slot rescue or review-cap request with PR/issue and exact head | The rescue is off-slot and must not hold or change a numbered lease. Preserve the existing rescue packet, review-cap checks, and deduplication. |
| PR merged terminal | `Skill(cleanup-pr)` | A GitHub merge terminal for a specific PR | A quoted “merged” phrase is not a new merge. Verify the merge before cleanup and retain the existing cleanup record, sentinel, pending-intake bookkeeping, and `pm-state:closed-clean` follow-through. |

## Bookkeeping rule

The named action owns its existing evidence, `pm-ops` records, obligations,
sentinels, pending-intake entries, relay keys, and deduplication where listed
above. This SOP documents those responsibilities; the UserPromptSubmit hook
does not create, repair, clear, or replay them. Missing or conflicting event
identity stays unresolved in the action path rather than becoming a new
classification.

## Scope

This SOP records the current mapping only. It adds no new policy, no new event
class, and no replacement operator or recorder. Additions require a separately
explicit directive.
