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
JSON { slot }   // only the slot number; anything else in the body is advisory
```

The thin CLI `mop release --slot N [--reason R]` carries the same contract
(slot number plus an optional reason) over REST; prefer it for all releases.

### Release always succeeds on the named slot

Freeing a named slot never refuses on state. If a turn is live, MoP
interrupts it and terminalizes the turn id, then frees the row in one atomic
write with a single audit row carrying the prior owner/issue/PR/epoch/turn
and the observed worktree dirty/clean state. The worktree itself is never
touched. An already-free slot is an idempotent success. DND does not block a
release. There is nothing to wait out and nothing to retry: assign the next
lane directly after the release returns.

Verify the release by reading the returned slot state: `occupied=false`, the
epoch advanced by one, and the owner tuple cleared.

GitHub labels are server-projected: the release unwinds the lane's labels
itself. Do NOT edit labels by hand or with `gh`.

Never hand-edit MoP DB state, slot rows, or labels to force a release.
