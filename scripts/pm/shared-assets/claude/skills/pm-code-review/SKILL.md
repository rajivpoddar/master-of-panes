---
name: pm-code-review
description: PM-owned status-only code review at the exact production control point.
---

# PM code review

Bind the review to the exact repository, PR, base, head, changed paths, linked
issue, and production control point. Re-read the issue body before the final
status and record its body SHA alongside the head. Require the same
auth/storageState, deployment/seed, provider, fixture/capture, and wall-budget
parity fields used by the producer and reviewers. Stateful behavior requires
full-file serial proof. Missing or non-parity evidence is a typed environment
blocker, not a green assumption.

For a UI-changing PR, the current issue body must contain the authoritative
visual acceptance contract: stable deterministic AC IDs, route/state,
expected observable, and evidence binding. A missing or malformed contract is
blocked. If code review is approved while visual evidence is still pending,
emit only `APPROVE_CODE_PENDING_QA_VISUAL_PROOF`; it is not a readiness-bearing
PASS. A missing browser/auth capability emits `QA_CAPABILITY_BLOCKED` and
requests proof-only reassignment to a provisioned owner. Do not waive required
screenshots as out of scope or replace them with component tests.

PM may carry the review status and exact evidence reference, but this skill
cannot invoke paid CI/E2E, labels, workflow reruns, or any alternate admission
path. A substantive APPROVE, REVISE, or BLOCK is terminal and remains bound to
the exact head, issue-body SHA, and changed paths. Non-UI PRs with no visual
contract retain the existing code-review path.
