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
```

Accept only the route's authoritative free-slot readback. On refusal, error,
or uncertain response return its typed reason and stop. Never retry, reset,
force, stash, alter ownership, send another pane message, or use another
release path.
