---
name: direct-assign
description: Assign one free numbered slot through Master of Panes.
---

## Rajiv decision gate

This skill may execute only a mechanically determined assignment under an
existing Rajiv-approved slot policy. If more than one eligible target exists,
the caller selects a different priority/owner, or the assignment would change,
waive, or override slot or Ready Pool policy, stop before the POST. Send one
evidence-bound recommendation to Abhijit CTO in `#heydonna-dev`; CTO must DM
Rajiv and wait for explicit approval. PM never asks Rajiv directly.

Read the current slot and Ready Pool once. If the slot is not free or the work is no longer eligible, stop without changing anything. Otherwise make exactly one `POST http://127.0.0.1:<MOP_PORT>/slots/{slot}/assign` with JSON `issue`, `repository_id`, and the complete PM-authored `task` message.

If MoP refuses, report its reason and stop. Do not add an authority header, caller epoch or tuple, capability, reservation, receipt, acknowledgement, label change, checkout mutation, pane send, fallback, or retry.
