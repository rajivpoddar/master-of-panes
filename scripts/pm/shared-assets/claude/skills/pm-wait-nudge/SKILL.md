---
name: pm-wait-nudge
description: Classify an injected slot wait notice and report it to PM.
---

## Rajiv decision gate

Classify and report freely. If the notice exposes a product decision or a
process decision such as a discretionary slot choice, priority change,
reassignment, release, exception, or conveyor-policy change, PM sends one
evidence-bound recommendation to Abhijit CTO in `#heydonna-dev` and stops before
mutation. CTO alone DMs Rajiv and waits for explicit approval.

Read the already-injected numbered-slot notice. If it is `LOCAL_CONTINUE`, continue local work and do not notify PM. If it is `PM_WAIT`, send PM one concise status/blocker notice through `message-pm` exactly once. If it is an idle/free assignment notice, send PM one concise free-slot and ready-work notice through `message-pm` exactly once. Do not assign, release, clear, reserve, or message a numbered slot from this skill.
