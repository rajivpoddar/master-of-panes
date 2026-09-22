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

Make exactly one invocation of the sanctioned atomic assignment operation.
It owns the fresh read, the `new_issue` session clear, the ownership transition,
the literal task delivery, and the dual readback, and it prints ONE JSON
terminal:

```text
python3 /Users/rajiv/.claude/scripts/mop-assign-slot.py \
  --slot <N> --class <repro|rework|new_issue> \
  --repo-id <repository_id> --issue <n> \
  [--pr <n>] [--branch <ref>] [--head <sha>] \
  [--work-kind <k>] [--handoff <id>] [--claimed-at <iso>] \
  --task-file /path/to/task.md
```

Success prints `{"status":"assigned","slot":N,"assignment_epoch":E,
"ownership_receipt":{...},"delivery_receipt":{...}}` and exits 0. A typed
refusal prints `{"status":"refused","step_failed":"clean|ownership|delivery|readback",
"reason":"...","slot_state_after":"...","sanctioned_path":"mop-assign-slot"}`
and exits non-zero; return that reason and stop.

[HIGH] Do NOT call the MoP assignment boundary directly. `POST
/slots/{slot}/assign` and `POST /slots/{slot}/adopt-issue-claim` are gated and
must not be used by PM. The complete tuple is passed to the operation instead:
for `repro`/`rework` supply `--pr`, `--branch`, `--head`, `--work-kind`, and
`--handoff` describing that same PR assignment, so the durable slot record is
exact and heartbeat-consumable. The assigned slot is the owner identity and the
literal task must retain the exact evidence and handoff context; callers must
not substitute generic ownership prose.

The `--task-file` holds the complete literal PM-authored message for the
selected work.

[HIGH] Delivery is owned by the operation (Rajiv directive 2026-09-20). Do NOT
run a separate `mop_send_to_slot` after assigning. The historical defect was
that the `/assign` POST wrote the ownership record (occupied + issue + task)
without reliably delivering to the pane: S2/#7994 read back `occupied=true`,
`issue=7994`, task present, yet the pane received nothing. `mop-assign-slot`
commits ownership FIRST and then delivers the same literal task, returning BOTH
an `ownership_receipt` and a `delivery_receipt`; success is returned only after
both readbacks pass. A delivery failure returns a typed `step_failed=delivery`
refusal naming the recoverable state — never a silent success — and re-running
the identical command resumes that same effect without a second ownership
commit.

## Slot clear before a `new_issue` assignment (Rajiv directive 2026-09-11)

For a `new_issue` assignment ONLY, `mop-assign-slot` clears the slot's session
first so the new issue starts with fresh context. That clear is part of the one
operation — do NOT issue a separate `POST .../clear`, and do NOT call
`mop-clear-slot.sh` for a dev slot. Do NOT clear for `repro` or `rework`: those
keep their prior context/history, and the operation knows the difference from
`--class`.

[HIGH] Precondition — the slot MUST already be `free` (released) before the
clear. NEVER `POST .../clear` on an `occupied`/`active` slot: a clear on an
active slot resets or queues-a-reset of its live session and destroys in-flight
work/context (it is not a read-only probe). Confirm `status:free`,
`occupied:false` from a fresh readback immediately before the clear. If the slot
is not free, release it first (idle-occupied = release-then-clear-then-assign);
if it cannot be safely freed, return `PM_ASSIGNMENT_BLOCKED` and do not clear.

## Message shapes

- `repro`: bind the existing issue and PR, repository, exact head, failing
  evidence, bounded reproduction command or question, and terminal evidence.
  This is reproduction/proof only and must not become general implementation.
- `rework`: bind the existing PR, branch, exact head, correction scope, and
  existing rework-handoff template. Do not create a new PR.
- `new_issue`: use the complete current dev-handoff template, including the
  issue contract and the canonical workflow chain below.

## Switch to the branch first (Rajiv directive 2026-09-11)

[HIGH] Every dispatched `task` MUST make the slot's FIRST operational step an
explicit branch checkout, so the slot never analyzes, edits, or commits while on
`main`. A slot left on `main` risks its edits/commits landing on `main`.
(Trigger: S5/#7730 was found on `main` during a rework because the task did not
lead with the checkout — Rajiv, thread 1789145957.)

- `repro` / `rework` (existing PR branch): the task's first step is
  `git fetch origin && git checkout <branch>` (the exact PR branch, expected at
  `head_sha`). Confirm `git branch --show-current` == `<branch>` before ANY
  log-analysis edit, repro command that writes files, code edit, or commit.
  Never edit or commit on `main`. If any change was already made on `main`,
  `git stash -u` → checkout the branch → `git stash pop` (do not commit to
  `main`, do not discard the work).
- `new_issue` (new branch): the task's first step is to create + checkout the
  branch slug off latest `main`
  (`git checkout main && git pull --ff-only origin main && git checkout -b <branch-slug>`),
  then confirm `git branch --show-current` == `<branch-slug>` before any edit or
  commit.

[HIGH] NO WORKTREES — use the slot's own checkout (Rajiv directive 2026-09-12,
thread 1789150956.391049, verbatim): *"update the direct assign skill and
prohibit worktrees in the slot messages. they have to use their own checkout
which has everything setup correctly."* Every dispatched `task` MUST require the
slot to do ALL work — analysis, repro, edits, commit, push — in its OWN checkout
(`heydonna-app-<slot>`), and MUST NOT create or use a `git worktree` (in
particular no `/tmp` worktree) for the assignment. The slot's own clone is the
only checkout with the env, services, ports, and Convex/dev setup wired
correctly; a worktree does not have that, and committing/pushing from one is
prohibited. The checkout in the "branch first" step above is a
`git checkout <branch>` IN THE SLOT'S OWN CLONE, never a `git worktree add`.
Include this line verbatim in every dispatched `task` message: *"Work only in
your own checkout (heydonna-app-<slot>); do NOT create or use a git worktree (no
`git worktree add`, no /tmp worktree) — check out the assigned branch in your own
clone, which has everything set up."* (Trigger: S5/#7730 worked the assigned
branch in `/private/tmp/rework7730sel` while its clone stayed on `main` — Rajiv,
thread 1789150956.) If a slot is found working in a worktree, steer it to
migrate back to its own clone on the branch and remove the worktree only after
confirming it holds no unpushed unique commits.

This step is part of the literal `task` message the skill POSTs, not a separate
delivery. Keep it in sync with the MoP-side dispatch/handoff template.

## Canonical workflow chain (Rajiv directive 2026-09-11, thread 1789111520)

The dev workflow STARTS AT THE PLAN-AGENT STEP. `/handoff` is NOT a workflow
step and must never appear in a dispatched task's workflow line. The canonical
chain every dispatched `task` (repro/rework/new_issue) carries is exactly:

```text
plan-agent → Codex plan review → implement → self-QA → Codex code review → PR
```

Do not prefix this with `/handoff` or any handoff command. The task message the
skill POSTs to MoP is itself the handoff; the work the slot performs begins at
plan-agent. This chain is kept in sync with the MoP-side dispatch/handoff
template — both must read plan-agent-first with no `/handoff` step.

### `self-QA` names the qa-tester agent when browser testing is required

[HIGH] Rajiv directive 2026-09-22 22:15 IST (thread `1790095531.366469`), verbatim:

> *"during assignments, the message should mention the qa-tester agent for self qa when browser testing is required. get this updated."*

Every dispatched `task` **whose acceptance criteria require browser testing**
— a real editor/UI path, a real-browser reproduction, a visual/screenshot proof,
or an end-to-end user gesture — MUST name the **`qa-tester` agent** in its
`self-QA` step, so the slot performs browser self-QA through that agent rather
than improvising a harness. State it explicitly in the task message, e.g.:

```text
… → implement → self-QA (use the `qa-tester` agent for the browser/UI leg) → Codex code review → PR
```

Where browser testing is **not** required (pure unit / integration / docs /
tooling lanes), the `self-QA` step stays as-is — do not add the agent by rote.
The test is whether the AC's required proof includes a browser, visual, or
end-to-end surface; if it does, naming `qa-tester` is mandatory, not optional.

This is part of the literal `task` message the skill POSTs, not a separate
delivery. Keep it in sync with the MoP-side dispatch/handoff template.

After successful `new_issue` assignment, post exactly one new top-level
`#heydonna-dev` transition parent containing the issue, slot, assignment
summary, and CTO mention. Record its `thread_ts`; all later PR transitions for
that assignment reply in that thread. For `repro` and `rework`, reuse the
existing authoritative PR transition thread when present and never create a
new-issue parent. A missing or ambiguous thread mapping is a typed blocker and
must not cause another assignment or delivery.

If `mop-assign-slot` refuses, return its `reason` and `slot_state_after` and
stop. Do not retry with a raw assign POST, do not change labels, slots, or
worktrees, and do not invoke an alternate assignment authority.
