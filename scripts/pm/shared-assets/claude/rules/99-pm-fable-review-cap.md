# Companion Review-Cap Routing

The installed Codex review companion's actionable cap packet is the routing
authority. Preserve its exact frozen source, head, review result, and
`supported_route`; do not invent a rescue route or review evidence.

When `PLAN_REVIEW_CAP_REACHED` reports
`supported_route=cto_plan_adjudication`, send the frozen plan and scope packet
to CTO for one bounded plan adjudication (`NO_PATCH_REQUIRED` or
`PATCH_READY`). Do not delegate a PM rescue agent, rerun another reviewer, or
change the plan while it is frozen. Keep the slot held: no release, no reassign,
no `pm-blocked:cto`; the slot keeps its ownership, branch, and worktree.

When `CODE_REVIEW_CAP_REACHED` reports
`supported_route=cto_review_adjudication`, send the exact-current-head review
packet to CTO for one bounded review-cap adjudication. Do not invoke a PM
rescue selector/backend, run off-slot code, or manufacture review/ready
evidence. Keep the slot held: no release, no reassign, no `pm-blocked:cto`; the
slot keeps its ownership, branch, and worktree.

For other cap packet types, follow only the packet's explicit supported route;
if it has none, stop and return the exact packet for adjudication. Never infer
a route from the cap label alone.
