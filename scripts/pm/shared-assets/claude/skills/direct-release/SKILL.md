---
name: direct-release
description: Release one quiescent numbered slot through Master of Panes.
---

## Execution contract

Use this only for an already-authorized, mechanically required release. Read
the current slot once. If it is active, productive, DND, changed, or no longer
the same held assignment, return `PM_RELEASE_BLOCKED reason=current_state_mismatch`
with no POST.

In the assigned checkout, first:

```text
Switch to main and pull the latest origin/main.
```

Then invoke the existing direct release boundary exactly once:

```text
POST http://127.0.0.1:<MOP_PORT>/slots/{slot}/release
```

Accept only the route's authoritative free-slot readback. On refusal, error,
or uncertain response return its typed reason and stop. Never retry, reset,
force, stash, alter ownership, send pane input, or use another release path.
