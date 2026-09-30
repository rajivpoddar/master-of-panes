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

## Escalation ladder first (Rajiv 2026-09-28 11:31 IST, thread C0ALZJHGE49/1790575062.387079)

Rajiv asked *"was the cto escalation ladder used for this?"* about a #8408 plan cap that PM sent straight to CTO. It hadn't been. Before
sending ANY `supported_route=cto_plan_adjudication` or `cto_review_adjudication` packet to CTO,
run `Skill(pm-escalation-ladder)` (Jev → Fable). Only a ladder result of `cto` or
`reserved_direct` reaches CTO. A Fable/Jev NO_PATCH_REQUIRED or PATCH_READY ruling is applied by
PM directly, and the slot stays held on the lane throughout. This overrides the direct-to-CTO routing above
(Rajiv 08:34/08:36: "use the cto escalation ladder", "reduce the cto load as much as possible").
