---
name: pm-nudge-processing
description: Process one PM free-slot notice through the existing assignment path.
---

For one free-slot notice, re-read the current slot and Ready Pool immediately before acting. If the selected work and slot are no longer eligible, return `PM_ASSIGNMENT_BLOCKED reason=current_state_mismatch` and do not assign. Otherwise make exactly one `POST http://127.0.0.1:<MOP_PORT>/slots/{slot}/assign` with JSON `issue`, `repository_id`, and the complete PM-authored `task` message.

If MoP refuses the assignment, return `PM_ASSIGNMENT_BLOCKED` with its reason and do not retry. Do not add a capability, authority header, tuple/epoch fence, reservation, receipt, acknowledgement, or second assignment path. Never release, clear, relabel, trigger workflows, or post Slack from this instruction surface.
