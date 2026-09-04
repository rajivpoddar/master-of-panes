---
name: pm-wait-nudge
description: Classify an injected slot wait notice and report the required action to PM.
---

## Rajiv decision gate

Classify and report freely. If the notice exposes a product decision or a
process decision such as a discretionary slot choice, priority change,
reassignment, release choice, exception, or conveyor-policy change, PM sends one
evidence-bound recommendation to Abhijit CTO in `#heydonna-dev` and stops before
mutation. CTO alone DMs Rajiv and waits for explicit approval.

Read the already-injected numbered-slot notice as a wake signal, not as an
authoritative state snapshot. Never classify a PM wait or free-slot notice as
`STALE` or `IGNORED`. PM must re-read the named slot and current Ready Pool in
`pm-nudge-processing`; changes in issue, PR, epoch, owner, wait age,
assignment, or free/occupied state change the action to match current
readback.

If it is `LOCAL_CONTINUE`, continue local work, do not notify PM, and finish with:

`PM_WAIT_NUDGE_RESULT classification=LOCAL_CONTINUE action=continued`

If it is `PM_WAIT` before the release boundary, send PM one concise status/blocker notice through `message-pm` exactly once. If it contains `release_required=true` or `action=RELEASE_REQUIRED`, send PM one concise release-required notice containing the slot, assignment epoch, and wait age through `message-pm` exactly once; PM must perform the release through `pm-nudge-processing`, not this skill. For either PM wait, finish with the exact machine-readable marker:

`PM_WAIT_NUDGE_RESULT classification=PM_WAIT action=reminded_pm waiting=<wait_age_minutes> urgency=<urgency>`

If it is an idle/free assignment notice, send PM one concise free-slot and ready-work notice through `message-pm` exactly once. Do not assign, release, clear, reserve, or message a numbered slot from this skill; the processor must reconcile the current state before acting.
