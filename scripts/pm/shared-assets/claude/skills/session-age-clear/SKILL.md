---
name: session-age-clear
description: |
  Execute routine S1-S6 session-age clears and PM pending-clear rows when hourly ops audit or Rajiv
  identifies `clear_due` rows from the Sakshi heartbeat JSON. Use only for
  mechanical session cleanup; not for heartbeat reporting, PM status, respawn,
  or product decisions.
author: PM-direct (Dhruva)
version: 1.1.0
date: 2026-06-20
---

# Session-Age Clear

## Purpose

This skill executes the routine 3h dev-slot session-age clear policy and PM
pending-clear tracking without coupling it to heartbeat Slack reporting or
hourly-ops proof generation.

Policy:

- PM and every slot S1-S6 are clear-worthy when effective session age is `>=3h`.
- Autocompact count is not required.
- Clears that are safe to execute must go through MoP logging so MoP logs the
  request/result. Use `mop_clear_slot` when the MCP tool is loadable; otherwise
  use `/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/mop-clear-slot.sh`.
- Never inject `/clear` directly and never use respawn as the stale-session fix.
- Clear due idle/free dev slots only. PM is never auto-cleared by this skill.
- Do not use `mop_clear_slot(slot: "all")` from this skill because it clears PM
  too early and can interrupt the caller before proof is written.
- Do not queue clears for active/occupied dev slots from routine heartbeat or
  hourly hygiene. A MoP `clear_pending_queued` on an active slot fires on the
  next Stop hook and can erase PM/slot context before PM reaches the intended
  natural boundary. Active clear-worthy slots become PM-todo pending-clear rows,
  not completed hygiene.
- The normal dev-slot natural boundary is the PM state transition that releases
  the slot. A slot release (`Skill(direct-release)`), a blocked-rework label write
  (`gh pr edit --add-label pm-blocked:<reason>`) and a merge
  (`Skill(heydonna-open-pr-status)`) consume an open
  `SESSION_AGE_CLEAR_PENDING slot:<N>` row by
  running `.claude/scripts/mop-clear-slot.sh --require-terminal N`; the row is
  resolved only after terminal MoP proof. If the clear queues or fails, leave the
  PM ops row open.
- PM self-clear is a nag/manual-ack transition: heartbeat creates/updates
  `SESSION_AGE_CLEAR_PENDING PM`; the project Stop hook
  `.claude/hooks/pm-self-clear-stop.sh` only emits a non-blocking reminder for
  PM to finish housekeeping, save any last note to pm-todo/pm-ops, and manually
  run the MoP-logged PM clear path:
  `bash /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/mop-clear-slot.sh pm`.
  It must not return `decision:block`, because that can trap the clear request
  behind the Stop hook.
- The hook must not inject `/clear` or call MoP clear for PM. It resolves
  `SESSION_AGE_CLEAR_PENDING PM` only after terminal MoP proof
  (`slot_cleared`, `clear_pending_executed`, or `SessionStart source=clear`
  after the obligation was created).
- Before reminding PM, the hook must check MoP logs/config for an already
  triggered PM clear (`clear_pending_queued`,
  `clear_pending_duplicate_suppressed`,
  `send_allowed_pm_clear_control_command`, or `send_command` with `/clear`).
  If found, it exits quietly and leaves the PM row open until terminal proof.

## Procedure

1. Run a fresh deterministic probe:

```bash
bash /Users/rajiv/.claude/scripts/sakshi-heartbeat.sh
```

2. Read `/tmp/sakshi-heartbeat.json`.

3. Build the due list and split it into two buckets:

- include rows where `clear_due=true`;
- exclude rows where `clear_already_requested=true`;
- order rows as S1, S2, S3, S4, S5, S6, then PM.
- `clear_now`: dev panes that are already at a natural boundary
  (`free`/released/idle with no active task, PR, branch, or buffered handoff).
- `pending_pm_todo`: PM rows, plus active/occupied dev panes that are
  clear-worthy but still mid-work, mid-review, mid-handoff, or otherwise not at
  a PM-approved natural boundary.

4. For every `pending_pm_todo` row, create/update a PM ops obligation and render
`pm-todo.md` before returning. For dev slots:

```
python3 /Users/rajiv/.claude/scripts/pm-ops.py obligation-upsert \
  --kind session_age_clear \
  --severity high \
  --slot <N> \
  --owner pm \
  --horizon heartbeat \
  --title "SESSION_AGE_CLEAR_PENDING slot:<N>" \
  --action "Clear slot <N> at PM-approved natural boundary; do not queue an automatic MoP clear while active" \
  --evidence "sakshi-heartbeat clear_due slot:<N> age=<age>" \
  --print-id

python3 /Users/rajiv/.claude/scripts/pm-ops.py sync --write --no-live --reason session-age-clear-pending
```

If the local CLI shape differs, use the equivalent PM ops upsert/sync command,
but the rendered row must contain `SESSION_AGE_CLEAR_PENDING`, `slot:<N>`, the
current age, and the natural-boundary condition.

For PM rows, the rendered row must say PM must finish housekeeping, save any last
note to pm-todo/pm-ops, and then clear through MoP logging with the exact PM
clear command:

```
python3 /Users/rajiv/.claude/scripts/pm-ops.py obligation-upsert \
  --kind session_age_clear \
  --severity high \
  --owner pm \
  --horizon heartbeat \
  --title "SESSION_AGE_CLEAR_PENDING PM" \
  --action "Finish housekeeping, save last note to pm-todo/pm-ops, then self-clear through MoP logging: bash /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/mop-clear-slot.sh pm; hook only nags" \
  --evidence "sakshi-heartbeat clear_due PM age=<age>" \
  --print-id

python3 /Users/rajiv/.claude/scripts/pm-ops.py sync --write --no-live --reason session-age-clear-pending
```

5. Load and call MoP clear only for `clear_now` rows. Prefer MCP when available:

```
ToolSearch: select:mcp__plugin_master-of-panes_mop__mop_clear_slot,mcp__plugin_master-of-panes_mop__mop_all_slots

mop_clear_slot(slot: "1")
mop_clear_slot(slot: "2")
mop_clear_slot(slot: "3")
mop_clear_slot(slot: "4")
```

If `mop_clear_slot` is not loadable, use the HTTP wrapper for the same rows:

```bash
bash /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/mop-clear-slot.sh 1
bash /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/mop-clear-slot.sh 2
bash /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/mop-clear-slot.sh 3
bash /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/mop-clear-slot.sh 4
```

Only call rows that are due and in `clear_now` after Step 3. Never call
`mop_clear_slot` or the wrapper for PM or for a `pending_pm_todo` dev slot.

6. Verify proof.

For `clear_now`, accept only terminal clear proof per due pane:

- `slot_cleared`
- `clear_pending_executed`
- `clear_pending_failed` (only as failure proof; report as BLOCKED)

For `pending_pm_todo`, accept:

- `pm-ops-obligation:<id>` plus `pm-ops-sync:<id>`
- `pm-todo-row:<line>` containing `SESSION_AGE_CLEAR_PENDING`
- `PM_TRANSITION_SLOT_CLEAR slot=<N> status=executed ... proof=<path>` emitted by
  the MoP slot boundary when the pending row is consumed at a slot release

`clear_pending_queued` is not completion proof for active dev slots. If it is
observed, report the slot as `QUEUED/PENDING` and keep the PM-todo row open
until MoP later emits `clear_pending_executed` or `slot_cleared`.

7. Write `/tmp/session-age-clear-latest.json` with:

- `generated_at`
- `source_json`: `/tmp/sakshi-heartbeat.json`
- `due_before`
- `clear_now`
- `pending_pm_todo`
- `clears_attempted`
- `mop_results`
- `blocked_or_failed`

8. Return a concise proof summary to the caller:

```
Session-age clear @ <timestamp>
- S1: EXECUTED proof: slot_cleared|clear_pending_executed ...
- S2: QUEUED proof: pm-todo-row:<line> SESSION_AGE_CLEAR_PENDING slot:2 ...
- PM: EXECUTED proof: slot_cleared ... (PM was last)
- proof_file: /tmp/session-age-clear-latest.json
```

## Completion Notes

If no rows are due, write `/tmp/session-age-clear-latest.json` with
`due_before=[]` and return `CLEAR proof: no clear_due rows`.

If rows are due but all are active dev slots, write proof with
`clear_now=[]`, `pending_pm_todo=[...]`, and return `QUEUED/PENDING`, not
`EXECUTED`.

If PM is due during heartbeat, create/update `SESSION_AGE_CLEAR_PENDING PM` and
let `.claude/hooks/pm-self-clear-stop.sh` nag until PM clears itself through the
MoP-logged PM clear path after housekeeping. For manual session-age-clear runs,
do not auto-clear PM; leave the PM row open until terminal MoP proof from PM's
explicit clear command.
