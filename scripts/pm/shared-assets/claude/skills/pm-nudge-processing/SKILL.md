---
name: pm-nudge-processing
description: Process one PM release or free-slot notice through existing MoP paths.
---

## Execution contract

Follow the shared release-conveyor decision boundary. PM owns this routine
scheduling and lifecycle transition under approved priorities and safety rules;
route genuine decisions to CTO without adding a Rajiv approval hop.

Re-read the current slot and Ready Pool before every action. A changed,
ineligible, active, productive, DND, or uncertain readback returns a typed
`PM_ASSIGNMENT_BLOCKED reason=current_state_mismatch` and makes no POST.

For an occupied PM_WAIT notice whose carried wait age is at least 20 minutes,
first invoke `Skill(direct-release)` exactly once. That full contract delivers
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
contract once. For `repro` and `rework`, send the freshly read complete PR
identity so the native route writes a heartbeat-consumable slot record:

```text
POST http://127.0.0.1:<MOP_PORT>/slots/{slot}/assign
JSON {expected_epoch, issue, repository_id, pr, branch, head_sha, work_kind, handoff_id, task}
```

The request must contain one matching full 40-hex head, branch, work kind,
handoff, and expected epoch; a partial or stale tuple is a typed zero-effect
refusal. Success must include occupied readback, `delivery_verified=true`, the
same PR/head/branch/work identity, and the returned assignment epoch. For
`new_issue`, there is no PR/head yet, so retain the issue-only
`{issue, repository_id, task}` form.

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
