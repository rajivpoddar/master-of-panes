---
name: pm-nudge-processing
description: Process one PM free-slot or held-slot release notice through existing paths.
---

## Rajiv decision gate

Treat the notice as mechanical only when the existing Rajiv-approved Ready Pool
and slot policy select exactly one assignment without PM discretion. Multiple
eligible targets, a priority or owner choice, or any policy exception/change is
a process decision. In that case send one evidence-bound recommendation to
Abhijit CTO in `#heydonna-dev` and stop without assigning; CTO must DM Rajiv and
wait for explicit approval. PM never asks Rajiv directly.

Every PM wait/free notice is a wake signal. Never classify it as `STALE` or
`IGNORED`, and never discard it because the notice snapshot changed. Re-read
the named slot and current Ready Pool before choosing an action; issue, PR,
epoch, owner, wait age, assignment, and free/occupied changes are reconciled
from that current readback.

For an occupied `release_required=true` / `action=RELEASE_REQUIRED` notice,
use the current slot tuple rather than the notice snapshot. If the current
slot is occupied, inactive, idle, and non-DND, construct/relay the required release message through the existing PM path and invoke exactly one empty `POST http://127.0.0.1:<MOP_PORT>/slots/{slot}/release`. Accept only HTTP 200
with a returned slot projection showing `occupied=false`; a refusal, non-200,
malformed response, or uncertain response is `PM_RELEASE_BLOCKED` with the
server reason and must not be retried. If the current slot is active,
productive, DND-protected, missing, or otherwise unsafe, preserve it and
return `PM_NUDGE_RECONCILIATION_BLOCKED reason=current_state_mismatch` with no
release POST; this is not a stale or ignored nudge. Never classify it as `STALE` or `IGNORED`.
Re-read the current Ready
Pool only after confirmed release. If its normal policy selects exactly one
eligible target, continue with one assignment POST as below; otherwise return
its typed no-eligible/state-changed blocker and make no assignment call.

For one free-slot notice, re-read the current slot and Ready Pool immediately
before acting. Use the current free slot and fixed priority: `repro`, then `rework`, then `new_issue`; do not use stale notice fields. If current state
is not eligible, return `PM_NUDGE_RECONCILIATION_BLOCKED reason=current_state_mismatch` and do not assign; never return `STALE` or `IGNORED`. If the existing assignment endpoint rejects a current-state mismatch, report its `PM_ASSIGNMENT_BLOCKED reason=current_state_mismatch` without retry. Otherwise make exactly one `POST http://127.0.0.1:<MOP_PORT>/slots/{slot}/assign` with JSON `issue`,
`repository_id`, and the complete PM-authored `task` message.

If MoP refuses the assignment, return `PM_ASSIGNMENT_BLOCKED` with its reason and do not retry. Do not add a capability, authority header, tuple/epoch fence, reservation, receipt, acknowledgement, or second assignment path. Never clear, relabel, trigger workflows, or post Slack from this instruction surface; release-required notices use only the existing direct release route above.
