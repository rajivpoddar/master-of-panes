---
name: direct-release
description: Release one quiescent numbered slot through Master of Panes.
---

Read the current slot once. If it is active, productive, or DND, stop without changing anything. Otherwise make exactly one empty `POST http://127.0.0.1:<MOP_PORT>/slots/{slot}/release`.

If MoP refuses, report its reason and stop. Do not add an authority header, caller epoch or tuple, capability, reset instruction, checkout mutation, reservation, receipt, acknowledgement, fallback, or retry.
