---
name: session-age-clear
description: |
  Execute the PM pending-clear row and the PM self-clear nag when hourly ops audit or Rajiv
  identifies `clear_due` rows from the Sakshi heartbeat JSON. Dev slots S1-S6 are never cleared on a
  cadence here; their clearing belongs to the PM assignment boundary (`mop-assign-slot`, new_issue).
  Use only for mechanical session cleanup; not for heartbeat reporting, PM status, respawn, or
  product decisions.
author: PM-direct (Dhruva)
version: 1.2.0
date: 2026-09-21
---

# Session-Age Clear

## Purpose

This skill executes PM pending-clear tracking and the PM self-clear nag without
coupling it to heartbeat Slack reporting or hourly-ops proof generation. It does
not clear, or queue a clear for, any dev slot S1-S6.

Policy:

- PM is clear-worthy when effective session age is `>=3h`.
- Dev slots S1-S6 are never cleared on a cadence by this skill. This skill must
  not create a per-dev-slot pending-clear row, and must not call a dev-slot
  clear, for an idle slot and an active slot alike.
- A dev slot's clear belongs to the PM assignment boundary: `mop-assign-slot
  --class new_issue` owns the clear at the point a new issue is assigned, and a
  genuine operator clear requires the clear script's own distinct explicit
  acknowledgement. A stale dev slot is a scheduling/boundary problem, not
  routine hygiene.
- Autocompact count is not required.
- Clears that are safe to execute must go through MoP logging so MoP logs the
  request/result. Use `mop clear --slot pm` (REST CLI) for the MoP-logged PM clear;
  otherwise use `/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/mop-clear-slot.sh pm`.
- Never inject `/clear` directly and never use respawn as the stale-session fix.
- PM is never auto-cleared by this skill.
- Do not use `mop clear --slot all` from this skill because it clears PM
  too early and can interrupt the caller before proof is written.
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

3. Build the due list, keeping PM rows only:

- include rows where `clear_due=true`;
- exclude rows where `clear_already_requested=true`;
- the only clear-worthy row this skill acts on is PM. Collect any S1-S6
  `clear_due` rows as `ignored_dev_slots` and report them observationally; never
  queue or execute a clear for them.
- `pending_pm_todo`: the PM row when it is clear-worthy.

4. For a `pending_pm_todo` PM row, create/update a PM ops obligation and render
`pm-todo.md` before returning. The rendered row must say PM must finish
housekeeping, save any last note to pm-todo/pm-ops, and then clear through MoP
logging with the exact PM clear command:

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

If the local CLI shape differs, use the equivalent PM ops upsert/sync command,
but the rendered row must contain `SESSION_AGE_CLEAR_PENDING`, the PM owner, the
current age, and the PM self-clear command.

5. Verify proof.

For `pending_pm_todo`, accept:

- `pm-ops-obligation:<id>` plus `pm-ops-sync:<id>`
- `pm-todo-row:<line>` containing `SESSION_AGE_CLEAR_PENDING PM`

This skill produces no terminal dev-slot clear proof, because it never requests
a dev-slot clear. Dev-slot clearing is owned by the assignment boundary
(`mop-assign-slot --class new_issue`) or by a distinct explicit operator clear.

6. Write `/tmp/session-age-clear-latest.json` with:

- `generated_at`
- `source_json`: `/tmp/sakshi-heartbeat.json`
- `due_before`
- `pending_pm_todo`
- `ignored_dev_slots`
- `blocked_or_failed`

7. Return a concise proof summary to the caller:

```
Session-age clear @ <timestamp>
- PM: QUEUED proof: pm-todo-row:<line> SESSION_AGE_CLEAR_PENDING PM ...
- dev slots: no action (dev-slot clearing is owned by the assignment boundary)
- proof_file: /tmp/session-age-clear-latest.json
```

## Completion Notes

If no PM row is due, write `/tmp/session-age-clear-latest.json` with
`due_before=[]` and return `CLEAR proof: no clear_due rows`.

If dev slots are due but PM is not, write proof with `pending_pm_todo=[]`,
`ignored_dev_slots=[...]`, and return `NO_ACTION: dev-slot clearing is owned by
the assignment boundary`, never a dev-slot clear.

If PM is due during heartbeat, create/update `SESSION_AGE_CLEAR_PENDING PM` and
let `.claude/hooks/pm-self-clear-stop.sh` nag until PM clears itself through the
MoP-logged PM clear path after housekeeping. For manual session-age-clear runs,
do not auto-clear PM; leave the PM row open until terminal MoP proof from PM's
explicit clear command.
