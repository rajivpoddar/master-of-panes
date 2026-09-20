---
name: direct-release
description: Release one quiescent numbered slot through Master of Panes.
---

## Execution contract

Follow the shared release-conveyor decision boundary. PM may execute this
routine safety release under approved cadence and ownership rules; route
genuine decisions to CTO without adding a Rajiv approval hop.

Use this for the routine release required by the approved 20-minute safety
rule. Read
the current slot once and pin its complete assignment tuple (slot, epoch,
repository, issue, PR, branch, head, work kind, handoff, owner, and task). If it is active,
productive, DND, changed, or no longer the same held assignment, return
`PM_RELEASE_BLOCKED reason=current_state_mismatch` with no pane input and no
POST.

In the assigned checkout, first:

```text
Switch to main and pull the latest origin/main.
```

Deliver that exact literal instruction to the owning pane exactly once through
the existing message-slot/direct-send path, then wait for the slot's natural
completion. Do not send a second or fallback instruction if delivery is
uncertain. Re-read the same slot and require the pinned epoch and complete
assignment tuple to be unchanged, inactive and idle, non-DND, with a clean
checkout, no unpushed work, branch `main`, and `HEAD` equal to the current
`origin/main` head. Any delivery error or uncertainty, active/productive
state, tuple drift, dirty/unpushed checkout, pull failure, or head drift is
`PM_RELEASE_BLOCKED` and stops before release.

Then invoke the existing direct release boundary exactly once:

```text
POST http://127.0.0.1:<MOP_PORT>/slots/{slot}/release
headers: x-heydonna-assignment-authority: pm-transition-v1
JSON {
  slot, expected_epoch, expected_repository_id, expected_issue, expected_pr,
  expected_branch, expected_head_sha, expected_work_kind, expected_handoff_id,
  expected_claimed_at,          // the slot's LIVE claimed_at value, not null
  intended_main_head            // exact current main head to pull and attest
}
JSON for an idle issue-only legacy owner: add release_mode: "quiescent_legacy_issue_only"
(the effect identity is minted server-side; never supply effect_id).
```

The MCP client `mop_release_slot` carries the same contract (authority header,
complete expected tuple, intended_main_head, release_mode) and is registered
from the canonical current release (`.../master-of-panes/current/dist/mcp.js`),
so the documented path and the client path are the same request shape.

Accept only the route's authoritative readback: the post-commit slot state for a
successful release, or the route's typed refusal (`assignment_authority_required`,
`observed_tuple_mismatch`, `slot_not_idle`, `productive_work`, `dnd_active`,
`quiescent_release_required`, ...). On refusal, error, or uncertain response
return its typed reason and stop. Never retry, reset, force, stash, alter
ownership, send another pane message, or use another release path.
