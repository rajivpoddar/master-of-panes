---
name: backlog-triage
description: Reconcile backlog and dependency-held Ready Pool work; repair contracts and promote every eligible P0-P2, then P3 refill below ten.
---

# Backlog triage

Read the full current policy in
`/Users/rajiv/Downloads/projects/heydonna-app/.claude/skills/pm-triage/skill.md`.
In particular: ten is a claimable FLOOR, P3 may refill a deficit, named dependency
waits belong in the pool at every priority, and the promoter needs no Codex
architecture review. Dependencies are checked live before assignment.

## Run

1. Read each issue, comments, original directive, main implementation and live
   PR/MoP ownership. Close proven delivered/obsolete/duplicate work with evidence;
   retain explicit reopened customer-verification obligations.
2. Inspect no-status/no-priority intake, blocked/deferred issues, and stale holds.
   Reverify blockers, owner and wake. Operational work goes to PM obligations.
3. Run `python3 ~/.claude/scripts/backlog-triage.py promote --dry-run`.
4. Launch PM's background `pm-triage-agent` for full triage plus contract
   repair in the SAME run, regardless of pool size if eligible P0-P2 remain.
5. Apply valid candidates with `python3 ~/.claude/scripts/backlog-triage.py
   promote --apply --issues N ...`. The optional hash-pinned PM decision manifest
   is supported; it is not an architecture-review prerequisite.
6. Repair incomplete candidates with `python3
   /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/ready-pool-contract-repair.py
   --source-manifest /tmp/pm-backlog-promoter-....json --apply`.
   Deterministic repair preserves existing ACs/dependencies, validates live bodies,
   and continues through verified promotion in the same invocation. Missing facts
   become exact questions, never invented ACs or generic triage labels.
For existing Todo metadata defects, use `backlog-triage.py repair-ready-pool
--dry-run --issues N`, then `--apply --metadata-json <hash-pinned evidence proposal>
--issues N`. `review_proof` is a legacy field for cited source evidence, not a
reviewer prerequisite. Repair happens in place; no backlog promotion is needed.

7. Validate changed bodies and run `audit-ready-pool`. Report named dependency
   waits separately from contract defects. PM syncs obligations/pm-todo and uses
   canonical `direct-assign`; never invoke retired sweep or handoff helpers.

Every P0-P2 passing the contract/routing checks is handled; no stop-at-ten, first-
five-row shortcut, or implicit promotion just because a slot is free. P3s fill
only the remaining claimable deficit after higher-priority work. Preserve explicit
parks. Do not use the legacy `--migrate-blocked-todos` flag to demote dependencies.

Report promoted/closed/held/decision issue numbers, total and claimable counts,
blockers/owner/wake, manifest paths, live label effects, and one next action.
