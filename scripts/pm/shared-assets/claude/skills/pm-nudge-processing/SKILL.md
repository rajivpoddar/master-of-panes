---
name: pm-nudge-processing
description: Process one PM free-slot notice through the existing assignment path.
---

## Rajiv decision gate

Treat the notice as mechanical only when the existing Rajiv-approved Ready Pool
and slot policy select exactly one assignment without PM discretion. Multiple
eligible targets, a priority or owner choice, or any policy exception/change is
a process decision. In that case send one evidence-bound recommendation to
Abhijit CTO in `#heydonna-dev` and stop without assigning; CTO must DM Rajiv and
wait for explicit approval. PM never asks Rajiv directly.

For one free-slot notice, re-read the current slot and Ready Pool immediately before acting. If the selected work and slot are no longer eligible, return `PM_ASSIGNMENT_BLOCKED reason=current_state_mismatch` and do not assign. Otherwise make exactly one `POST http://127.0.0.1:<MOP_PORT>/slots/{slot}/assign` with JSON `issue`, `repository_id`, and the complete PM-authored `task` message.

If MoP refuses the assignment, return `PM_ASSIGNMENT_BLOCKED` with its reason and do not retry. Do not add a capability, authority header, tuple/epoch fence, reservation, receipt, acknowledgement, or second assignment path. Never release, clear, relabel, trigger workflows, or post Slack from this instruction surface.
