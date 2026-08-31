# Shared release-conveyor contract

This is the canonical contract for routine HeyDonna release motion. The PM
chat monitor, CTO wake SOP, and control-plane repair skill reference this file;
they must not define competing ownership or state machines.

## Routine motion and ownership

- A numbered slot owns only work executable now: implementation,
  reproduction, or production-shaped proof. CI, capture, review, decisions,
  and external waits are off-slot.
- PM owns routine free-slot refill and exact-head CI/E2E admission for ordinary
  code/proof-ready work. PM also reports the first literal control-plane
  blocker and exact tuple to CTO Decisions, but has zero review, approval,
  deployment, retry, marker, or admission authority for repairs.
- The CTO/release owner owns workflow terminals through causal routing or a
  head-pinned merge. Every nonterminal state names `next_action`,
  `next_owner`, and `wake`.
- A control-plane failure is escalated immediately and the smallest safe
  degraded product path may proceed. Repair the control plane separately; it
  is not a customer-lane dependency unless waiting creates concrete harm.

## Bounded control-plane repair

1. CTO Decisions performs causal diagnosis and classifies the issue as
   `PRODUCT`, `CONTROL_PLANE`, `MIXED`, or `UNKNOWN`. Only a verified bounded
   `CONTROL_PLANE` repair proceeds.
2. CTO sends one exact implementation brief to Master of Panes task
   `01a04154-c9c1-7bc1-8f7b-009a87bc7628`, the sole implementation owner for
   bounded control-plane and PM Operator work. The scopes remain distinct, but
   no duplicate owner is created.
3. MoP uses a clean exact-current-main worktree, makes the smallest change,
   proves RED/GREEN and fail-closed negatives, and returns an immutable
   candidate packet with base/parent/candidate/tree/stable-patch/path,
   rollback, and no-mutation inventory. MoP does not publish, install,
   activate, restart, deploy, use a slot, or inform PM.
4. MoP sends the candidate directly to CTO Decisions through renderer-free
   stdio. The packet is an actionable CTO review wake, not a ledger-only
   record.
5. CTO Decisions performs exactly one functionality-first inline review bound
   to the exact tuple. A block returns one bounded correction to the same MoP
   task. No PM review, PM marker, PM companion, or second reviewer is used.
6. On approval, CTO Decisions alone re-fences current main, publishes by
   ordinary non-force fast-forward, stages/activates the immutable release,
   restarts the affected service when required, verifies readiness/parity and
   rollback, then informs PM once with the landed tuple and exact next action.
   MoP never deploys after approval.

An approval may carry across one conflict-free replay only when semantics,
stable patch ID, and path set remain identical. Conflict or semantic drift
requires a fresh inline decision. No polling, numbered-slot capacity, raw
workflow dispatch, or unguarded label mutation is authorized by this contract.

## Immediate edge and retirement boundary

After one fresh exact-head, duplicate/run, and safety fence, CTO Decisions may
make one smallest guarded GitHub label edge needed to emit or resume the
canonical release event. Journal the literal pre/post label sets and edge
identity, preserve unrelated labels, make it idempotent, and stop after the
edge. This does not bypass CI/E2E or head-pinned merge guards.

On a control-plane refusal, PM returns the first literal blocker and exact PR,
full head, and current labels once. PM does not retry alternate commands or
receipts. CTO either executes/delegates the safe manual edge in that wake or
records the concrete harm that makes motion unsafe.

Retirement may remove owner-tuple/FAMILY2 coupling, phase markers, projected
overrides, PM review receipts, or approval ceremony only after the kept thin
fences are proven: exact current head, duplicate/single-flight journal, real
non-skipped `pull_request` CI/E2E, and head-pinned merge guard. Compatibility
shims expire after zero callers and complete-set parity with rollback ready.

## Normative motion matrix

```json
{
  "version": 2,
  "scenarios": {
    "code_ready_without_admission": {"owner": "PM", "action": "admit_exact_head_ci_e2e", "wake": "10m"},
    "free_compatible_slot_with_executable_drain": {"owner": "PM", "action": "refill_compatible_slot", "wake": "immediate"},
    "workflow_terminal": {"owner": "CTO_RELEASE_OWNER", "action": "route_first_causal_or_head_pinned_merge", "wake": "terminal"},
    "control_plane_candidate": {"owner": "MOP_IMPLEMENTATION_TASK_01a04154", "action": "return_candidate_to_cto_inline", "wake": "cto_inline_review"},
    "control_plane_block": {"owner": "MOP_IMPLEMENTATION_TASK_01a04154", "action": "return_bounded_rework_to_same_mop_task", "wake": "cto_inline_review"},
    "control_plane_approval": {"owner": "CTO_DECISIONS", "action": "publish_rollout_verify_and_notify_pm", "wake": "terminal"},
    "control_plane_refusal": {"owner": "CTO_DECISIONS", "action": "execute_or_durable_delegate_one_guarded_edge_or_record_harm", "wake": "immediate"}
  }
}
```

Labels, holds, relays, watching, idle claims, queued shells, and historical or
skipped runs do not satisfy active workflow motion. Every nonterminal report
preserves exact-head/fresh-fence, durable dedup, fail-closed safety, and
`next_action`, `next_owner`, and `wake`.
