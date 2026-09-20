---
name: direct-release
description: Release one numbered slot through Master of Panes.
---

## Execution contract

Follow the shared release-conveyor decision boundary. PM may execute this
routine safety release under approved cadence and ownership rules; route
genuine decisions to CTO without adding a Rajiv approval hop.

MoP is a single-user local tool used only by PM and CTO. There is **no
authority header**: the retired `x-heydonna-assignment-authority` header is
ignored if a stale client still sends it.

Read the current slot once. Then invoke the release boundary exactly once:

```text
POST http://127.0.0.1:<MOP_PORT>/slots/{slot}/release
JSON {
  slot, expected_epoch, expected_repository_id, expected_issue, expected_pr,
  expected_branch, expected_head_sha, expected_work_kind, expected_handoff_id,
  expected_claimed_at,          // the slot's LIVE claimed_at value from that read
  intended_main_head            // current main head you want the checkout reset to
}
```

The MCP client `mop_release_slot` carries the same contract (complete observed
identity plus `intended_main_head`); it is registered from the canonical current
release (`.../master-of-panes/current/dist/mcp.js`). There is no release mode to
pass: a clean-main checkout needs no pane instruction, and anything else is
reset through the pane automatically.

### Release succeeds whenever the slot is not actively working

Release is refused **only** while the slot is still working — the single
`slot_not_idle` refusal, whose `cause` names the reason:

- `active_turn` — a turn is active or indeterminate. The remediation names the
  exact turn id and its canonical escape:
  `POST /slots/{slot}/abandon-turn {"turn_id":"<id>","reason":"<why>","actor":"<who>"}`.
  Abandon that exact turn, then retry; the release then succeeds.
- `productive_work` — the row reports `idle=false`.
- `dnd` — DND is active.
- `quiescence` — the slot finished meaningful work inside the short settling
  window. This is a bounded wait, not a blocker: retry once the remediation's
  remaining seconds elapse.

Everything else is **superseded, never refused**, and recorded in the response
(`superseded` / `superseded.repair`) and in MoP's event log:

- a drifted or unusable observed identity (MoP's live row wins),
- a stale or mis-moded open release intent (superseded atomically; the response
  names the intent id it replaced),
- an `intended_main_head` that no longer matches the checkout's main head.

A repeat call after a completed release is an idempotent success: no second
epoch bump and no second effect.

### Recommended operator sequence

Send the slot the literal instruction — those preconditions still make the
release clean:

```text
Switch to main and pull the latest origin/main.
```

Deliver it exactly once through the existing message-slot/direct-send path, then
wait for the slot's natural completion and re-read it before releasing. Never
send a second or fallback instruction if delivery is uncertain. Verify the
release by reading the returned slot state: `occupied=false`, the epoch
advanced by one, and the owner tuple cleared.

Never hand-edit MoP DB state, slot rows, or labels to force a release.
