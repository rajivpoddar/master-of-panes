# Shared release-conveyor contract

This is the canonical contract for routine HeyDonna release motion. The PM
chat monitor, CTO wake SOP, and control-plane repair skill reference this file;
they must not define competing ownership or state machines.

## Routine motion and ownership

- A numbered slot owns only work executable now: implementation,
  reproduction, or production-shaped proof. CI, capture, review, decisions,
  and external waits are off-slot.
- For an open PR, PM has exactly two operational responsibilities: launch and
  return one bounded CI/E2E failure investigation tied to the exact failed
  PR/head/run, and perform the minimal numbered-slot/session/packet mechanics
  for a CTO-authorized rework, reproduction, or production-shaped proof
  packet. A conclusive investigation returns its exact packet to CTO; PM does
  not relabel, arm, capture, retry, rescue, release, sync, or merge the PR.
- CTO/release ownership covers every other open-PR action: exact-head
  CI/E2E admission/arming, capture decisions and dispatch, PR label/state
  edges, control-plane bypasses, workflow-terminal disposition, rerun/retry
  decisions, rescue/release routing, sync/integration, release gates, and
  head-pinned merge. PM may report evidence and status for these actions, but
  does not execute them. Every nonterminal state names `next_action`,
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
use the native bypass contract below for one smallest guarded operational edge
needed to emit or resume the canonical release event. It does not bypass
CI/E2E or head-pinned merge guards.

On a control-plane refusal, PM returns the first literal blocker and exact PR,
full head, and current labels once. PM does not retry alternate commands or
receipts. CTO either executes/delegates the safe manual edge in that wake or
records the concrete harm that makes motion unsafe.

Retirement may remove owner-tuple/FAMILY2 coupling, phase markers, projected
overrides, PM review receipts, or approval ceremony only after the kept thin
fences are proven: exact current head, duplicate/single-flight journal, real
non-skipped `pull_request` CI/E2E, and head-pinned merge guard. Compatibility
shims expire after zero callers and complete-set parity with rollback ready.

## Native bypass contract (CTO-only, after one high-level refusal)

The high-level typed/control-plane path is attempted once. A literal refusal or
proven unavailability is the trigger; do not retry it, vary marker/owner/
receipt shapes, invoke another wrapper, or wait for the repair. CTO Decisions
fresh-fences the exact head or slot, active/duplicate owner, current blockers,
and idempotent edge identity before any primitive. Only the primitives needed
by the blocked transition are allowed.

- Release/ownership/status movement uses one direct GitHub complete-set label
  edit. Bind PR and full head where applicable, journal literal pre/post label
  sets and exact edge identity, preserve unrelated labels, reconcile response
  loss, and stop after that one idempotent edge. It performs no MoP or
  `message-slot` mutation.
- Slot assign/release/rebind/controlled respawn uses the direct MoP HTTP API
  through `curl`, with the endpoint's exact slot and current identity/epoch,
  canonical authority header and payload, HTTP response, and readback recorded.
  Do not call a broken wrapper or alternate command. If assignment/refill also
  requires product ownership projection, perform one complete-set GitHub
  label reconciliation after the MoP readback, then deliver exactly one
  literal `message-slot` continuation packet. The order is MoP -> GitHub ->
  message-slot; failure at any step stops before later effects.
- No raw `workflow_dispatch`, blind rerun, CI/E2E/capture/review/merge or
  product-gate bypass, second message, renderer-mediated task message,
  numbered-slot repair work, or polling is permitted. Preserve substantive
  blockers and unrelated state.

Every degraded/manual/bypass route names this section as its sole primitive
authority. A bypass restores only the blocked operational edge; the durable
control-plane repair follows the PM-report -> CTO-diagnosis -> MoP-candidate
-> CTO-review -> CTO-deploy process above.

## Normative motion matrix

```json
{
  "version": 3,
  "scenarios": {
    "ci_failure_investigation": {"owner": "PM", "action": "launch_one_exact_failed_pr_head_run_investigation", "wake": "terminal_or_block"},
    "cto_routed_rework_or_repro_slot_assignment": {"owner": "PM", "action": "assign_one_compatible_numbered_slot_after_cto_authorization", "wake": "assignment_terminal"},
    "code_ready_without_admission": {"owner": "CTO_DECISIONS", "action": "admit_exact_head_ci_e2e", "wake": "10m"},
    "capture_decision_or_dispatch": {"owner": "CTO_DECISIONS", "action": "decide_or_dispatch_capture", "wake": "terminal"},
    "pr_label_or_state_transition": {"owner": "CTO_DECISIONS", "action": "journaled_complete_set_label_state_edge", "wake": "terminal"},
    "rerun_or_retry_decision": {"owner": "CTO_DECISIONS", "action": "authorize_one_exact_head_retry", "wake": "terminal"},
    "rescue_or_release_routing": {"owner": "CTO_DECISIONS", "action": "route_guarded_rescue_or_release", "wake": "terminal"},
    "sync_integration_and_merge": {"owner": "CTO_DECISIONS", "action": "sync_release_and_head_pinned_merge", "wake": "terminal"},
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
