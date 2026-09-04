---
name: pm-nudge-processing
description: Process one PM release or free-slot notice through existing MoP paths.
---

## Execution contract

Re-read the current slot and Ready Pool before every action. Every PM wait/free
notice is a wake signal: never classify it as `STALE` or `IGNORED`, and never
discard it because its snapshot changed. Reconcile issue, PR, epoch, owner,
wait age, assignment, and free/occupied state from the current readback. Drift
from the carried notice is expected reconciliation input, not itself a blocker.
Choose exactly one branch from the fresh state:

| Fresh current state | One permitted result |
| --- | --- |
| free with eligible Ready Pool work | select `repro`, then `rework`, then `new_issue`, and execute the existing direct-assign contract once |
| free without eligible work | return the typed no-eligible-work blocker with no POST |
| occupied with a fresh current episode that is not due | preserve the slot and return `PM_NUDGE_RECONCILIATION_PROCESSED reason=current_wait_not_due` with no POST |
| occupied with the same current due episode | invoke the existing direct-release flow once, then reconcile free state for assignment |
| active, productive, DND, or otherwise unsafe current authority | return `PM_ASSIGNMENT_BLOCKED reason=current_state_mismatch` with no POST |
| malformed, unavailable, or uncertain current authority | return a typed current-state blocker with no POST |
| any refusal or uncertain effect response | return its typed blocker and never retry or replay the stale action |

Notice fields and notice age never remain authority. A changed or ineligible
current state is not silently dropped: use the matching branch above. Only an
unsafe or uncertain current authority returns the typed
`PM_ASSIGNMENT_BLOCKED reason=current_state_mismatch` blocker.

For an occupied PM_WAIT notice, use the notice only as a wake signal and use
the current slot tuple rather than the notice snapshot. Derive release
eligibility exclusively from the current assignment epoch, current
`assigned_at`/current wait anchor, and current episode age; never use the
notice's carried wait age to authorize release. If the notice epoch or episode
is old and the fresh current episode is not yet due, preserve the current slot,
make no release POST, and return
`PM_NUDGE_RECONCILIATION_PROCESSED reason=current_wait_not_due`.
This is a processed reconciliation result, never `STALE` or `IGNORED`. When
the fresh current episode age is at least 20 minutes, first invoke
`Skill(direct-release)` exactly once. That full contract delivers
the exact `Switch to main and pull the latest origin/main.` instruction once,
waits for natural completion, re-reads the same complete assignment at the
same epoch, requires inactive/idle/non-DND state and a clean checkout exactly
at current `origin/main`, then owns the one existing release call:

```text
POST http://127.0.0.1:<MOP_PORT>/slots/{slot}/release
```

Require the authoritative `occupied=false` readback with an advanced epoch.
Do not duplicate the release call here, release an active/productive/DND or
changed lease, or retry an error or uncertain release result. If any part of
the direct-release contract refuses, return its typed current-state blocker
and do not select replacement work.

After a confirmed release, or for a free-slot notice, select exactly one
eligible Ready Pool item in priority order: `repro`, `rework`, then `new_issue`.
Use its complete literal task message and execute the existing direct-assign
contract once. That request is `{issue, repository_id, task}` and its success
must include occupied readback plus `delivery_verified=true`.

`repro` binds existing issue/PR, exact head, failing evidence, a bounded
reproduction command or question, and terminal evidence; it is never general
implementation. `rework` binds the existing PR/branch/exact head and existing
rework-handoff correction scope; it never creates a new PR. `new_issue` uses the
full current dev-handoff template and issue/workflow contract.

For a successful `new_issue`, PM posts exactly one top-level `#heydonna-dev`
transition parent containing issue, slot, assignment summary, and CTO mention,
then uses its `thread_ts` for later PR transitions. Repro/rework reuse their
existing PR transition thread and never create a duplicate parent. A missing
or mismatched current state, no eligible work, route refusal, or uncertain
response is one typed blocker with no retry, second assignment, second pane
send, or alternate control plane.
