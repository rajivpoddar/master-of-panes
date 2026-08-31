# Shared release-conveyor contract

This is the canonical contract for routine HeyDonna release motion. The PM
chat monitor, CTO wake SOP, and control-plane repair skill reference this file;
they must not define competing ownership or state machines.

## Ownership and executable motion

- A numbered slot owns only work executable now: implementation,
  reproduction, or production-shaped proof. CI, capture, review, product
  decisions, and external waits are off-slot; release the slot immediately.
- PM owns routine motion: refill a compatible free slot with executable work,
  and admit code/proof-ready work to exact-head CI/E2E.
- The CTO/release owner owns a workflow terminal through first-causal routing
  or a head-pinned merge. CTO approval is needed only for a product,
  security, architecture, or concrete unsafe-release exception.
- Every nonterminal state names `next_action`, `next_owner`, and `wake`.
  “Tracked”, “held”, “relay-only”, “watching”, and a queue receipt are not
  motion or ownership proof.
- A control-plane failure is escalated immediately and the smallest safe
  degraded/manual product path starts immediately. Repair the control plane
  separately; it is never a customer-lane dependency unless waiting creates
  concrete data loss, security/privacy harm, or irreversible damage.
- A control-plane candidate receives exactly one functionality-first CTO inline
  review. PM has zero review or approval role. After approval the same
  implementer publishes, activates, and verifies automatically; there is no
  candidate-only round trip or second reviewer.

## Immediate edge authority and retirement boundary

After one fresh exact-head, duplicate/run, and safety fence, CTO decisions has
standing authority to make one smallest guarded GitHub label transition needed
to emit or resume the canonical release event. The edge must journal the exact
pre/post label sets and edge identity, preserve unrelated labels, be
idempotent/replay-safe, exclude `workflow_dispatch` and blind reruns unless
separately authorized, and stop after that one edge. This is a narrow release
motion primitive, not permission to bypass CI/E2E or head-pinned merge guards.

On a control-plane refusal, PM returns the first literal blocker together with
the exact PR, full head, and current labels immediately. PM does not retry
marker shapes, owner tuples, projected metadata, review receipts, or alternate
control-plane commands. CTO consumes that receipt in the same wake: execute or
durably delegate the safe manual edge, or name the concrete data-loss,
security/privacy, or irreversible harm that makes motion unsafe. A
control-plane, provenance, or process defect alone is never a hold.

The current release/control-plane stack is eligible for a bounded retirement
assessment, not wholesale deletion. Keep exact current-head and duplicate/run
fences, one idempotent label-edge journal/replay guard, genuine non-skipped
`pull_request` CI/E2E validation, and the head-pinned merge guard: each directly
prevents stale execution, duplicate spend, false green status, or wrong-head
merges. Remove from the release-critical path, in this order, owner-tuple /
FAMILY2 coupling, phase markers and pending metadata, projected-state
overrides, PM review receipts/markers, then control-plane approval ceremony;
each is process coupling that can strand a release once the kept fences and
direct GitHub/Actions adapter are proven. Retain only a compatibility shim
while its callers migrate; expiry is zero callers plus one complete-set edge
parity readback, with rollback to the prior owner path if parity fails.

## Canonical immediate-motion example

For #7591, PM reports the `FAMILY2`/projected-marker refusal once with the
literal blocker, exact head, and labels. CTO fresh-fences the tuple, replaces
the stale `ci-head`, and emits one journaled `pm-state` label edge; the genuine
pull-request CI/E2E pair then materializes. Free compatible slots immediately
receive existing-PR executable work. Workflow and capture waits remain
off-slot, and a control-plane candidate goes once to CTO inline review and then
continues automatically with its implementer.

## Required motion scenarios

The following machine-readable matrix is normative. Consumers may add detail,
but may not replace these owners or next actions.

```json
{
  "version": 1,
  "scenarios": {
    "code_ready_without_admission": {"owner": "PM", "action": "admit_exact_head_ci_e2e", "wake": "10m"},
    "free_compatible_slot_with_executable_drain": {"owner": "PM", "action": "refill_compatible_slot", "wake": "immediate"},
    "workflow_terminal": {"owner": "CTO_RELEASE_OWNER", "action": "route_first_causal_or_head_pinned_merge", "wake": "terminal"},
    "control_plane_candidate": {"owner": "CTO", "action": "inline_review_once_then_implementer_continues", "wake": "approval_or_block"},
    "control_plane_refusal": {
      "owner": "CTO",
      "pm_input": "first_literal_blocker_exact_pr_full_head_current_labels",
      "fence": "fresh_exact_head_duplicate_run_safety",
      "action": "execute_or_durable_delegate_one_journaled_idempotent_label_edge",
      "edge": "preserve_unrelated_labels_stop_after_one_edge",
      "wake": "immediate",
      "terminal": "execute_or_durable_delegate_or_name_concrete_harm"
    }
  }
}
```

Nonterminal reports must preserve exact-head/fresh-fence, durable dedup,
fail-closed safety, and no-poll behavior. A report/action route is not itself
a mutation authority.
