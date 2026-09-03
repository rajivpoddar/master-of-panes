---
name: pm-nudge-processing
description: Process one PM free-slot notice through the existing assignment path.
---

For one free-slot notice, re-read the current slot and Ready Pool immediately before acting. If the selected work and slot are no longer eligible, return `PM_ASSIGNMENT_BLOCKED reason=current_state_mismatch` and make no POST. Otherwise use the existing assignment contract exactly once: `POST http://127.0.0.1:<MOP_PORT>/slots/{slot}/assign`, with header `x-heydonna-assignment-authority: pm-transition-v1`, and the existing complete assignment body containing `expected_epoch`, `repository_id`, `issue`, `pr` (positive integer or explicit null), `branch`, `head_sha` (40 hex), `work_kind`, `handoff_id`, and `task`.

Accept success only from HTTP 200 whose returned slot projection is occupied and matches the requested issue, task, repository, PR, branch, head, work kind, and handoff, with the returned assignment epoch. A non-200, malformed, uncertain, or mismatched readback returns `PM_ASSIGNMENT_BLOCKED` with the server reason or `readback_mismatch`; never retry. Do not create a new route, reservation, receipt, capability, or assignment authority. Never perform pane, release, clear, checkout, label, workflow, or Slack effects from this instruction surface.
