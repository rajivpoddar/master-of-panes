---
name: pm-wait-nudge
description: Classify an injected slot wait notice and report the required action to PM.
---

## Routine ownership and escalation

Follow the shared release-conveyor decision boundary. PM may carry out routine
scheduling, lifecycle, and conveyor actions under approved priorities and
safety rules; route genuine decisions to CTO and do not add an approval hop to
a routine nudge.

Read the already-injected numbered-slot notice. If it is `LOCAL_CONTINUE`, continue local work, do not notify PM, and finish with:

`PM_WAIT_NUDGE_RESULT classification=LOCAL_CONTINUE action=continued`

If it is `PM_WAIT` before the release boundary, send PM one concise status/blocker notice through `message-pm` exactly once. If it contains `release_required=true` or `action=RELEASE_REQUIRED`, send PM one concise release-required notice containing the slot, assignment epoch, and wait age through `message-pm` exactly once; PM must perform the release through `pm-nudge-processing`, not this skill. For either PM wait, finish with the exact machine-readable marker:

`PM_WAIT_NUDGE_RESULT classification=PM_WAIT action=reminded_pm waiting=<wait_age_minutes> urgency=<urgency>`

If it is an idle/free assignment notice, send PM one concise free-slot and ready-work notice through `message-pm` exactly once. Do not assign, release, clear, reserve, or message a numbered slot from this skill.
