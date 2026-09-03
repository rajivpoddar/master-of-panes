---
name: direct-release
description: Release one quiescent numbered slot through Master of Panes.
---

## Rajiv decision gate

This skill may execute only a mechanically required release under an existing
Rajiv-approved slot policy. A discretionary release, park, drain, reassignment,
force, exception, or change to slot policy is a process decision: stop before
the POST and send one evidence-bound recommendation to Abhijit CTO in
`#heydonna-dev`. CTO must DM Rajiv and wait for explicit approval. PM never asks
Rajiv directly.

Read the current slot once. If it is active, productive, or DND, stop without changing anything. Otherwise make exactly one empty `POST http://127.0.0.1:<MOP_PORT>/slots/{slot}/release`.

If MoP refuses, report its reason and stop. Do not add an authority header, caller epoch or tuple, capability, reset instruction, checkout mutation, reservation, receipt, acknowledgement, fallback, or retry.
