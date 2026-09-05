---
name: direct-assign
description: Assign one eligible task to one free numbered slot under the approved priority rules.
---

## Execution contract

Follow the shared release-conveyor decision boundary. PM may execute this
routine assignment under approved priorities and safety rules; route genuine
decisions to CTO without adding a Rajiv approval hop.

Classify the selected Ready Pool work as exactly one of `repro`, `rework`, or
`new_issue` before making any request. Read the current slot and Ready Pool
immediately before acting; if the slot or selection changed, return
`PM_ASSIGNMENT_BLOCKED reason=current_state_mismatch` and make no POST.

Make exactly one request to the existing native boundary:

```text
POST http://127.0.0.1:<MOP_PORT>/slots/{slot}/assign
JSON {issue, repository_id, task}
```

For an existing PR (`repro` or `rework`), use the complete form—not the
issue-only form—so MoP writes an exact heartbeat-consumable slot record:

```text
JSON {expected_epoch, issue, repository_id, pr, branch, head_sha, work_kind, handoff_id, task}
```

`expected_epoch`, `pr`, and `head_sha` are the freshly read values; `branch`,
`work_kind`, and `handoff_id` must describe that same PR assignment. MoP
refuses a partial or stale tuple. The assigned slot is the owner identity and
the literal task must retain the exact evidence and handoff context; callers
must not substitute generic ownership prose. A `new_issue` has no PR/head yet
and retains the issue-only form above until a PR-bound assignment exists.

The `task` is the complete literal PM-authored message for the selected work.
Accept success only from an occupied slot readback with
`delivery_verified=true`; MoP performs the one delivery to that pinned slot.
Never send a second message-slot request, retry an error or uncertain response,
or treat an assignment without verified delivery as success.

## Message shapes

- `repro`: bind the existing issue and PR, repository, exact head, failing
  evidence, bounded reproduction command or question, and terminal evidence.
  This is reproduction/proof only and must not become general implementation.
- `rework`: bind the existing PR, branch, exact head, correction scope, and
  existing rework-handoff template. Do not create a new PR.
- `new_issue`: use the complete current dev-handoff template, including the
  issue contract and required workflow chain.

After successful `new_issue` assignment, post exactly one new top-level
`#heydonna-dev` transition parent containing the issue, slot, assignment
summary, and CTO mention. Record its `thread_ts`; all later PR transitions for
that assignment reply in that thread. For `repro` and `rework`, reuse the
existing authoritative PR transition thread when present and never create a
new-issue parent. A missing or ambiguous thread mapping is a typed blocker and
must not cause another assignment or delivery.

If MoP refuses, return its reason and stop. Do not change labels, slots, or
worktrees and do not invoke an alternate assignment authority.
