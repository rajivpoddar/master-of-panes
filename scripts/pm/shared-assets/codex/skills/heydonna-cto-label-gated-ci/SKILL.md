---
name: heydonna-cto-label-gated-ci
description: CTO-only exact-head CI/E2E trigger guarded by current visual acceptance proof.
---

# CTO CI-trigger admission

Only CTO/PR Merges may use this prompt. Before authorizing or applying the
CI-trigger transition, re-read the live PR head and the current linked issue
body, compute the issue-body SHA, and bind the transition to both values. The
caller must not choose an exemption or rely on an earlier snapshot.

For a UI-changing PR, require the current issue's authoritative visual
acceptance contract and one valid proof for every deterministic visual AC.
Proof must be bound to the exact PR, issue, head, issue-body SHA, AC ID, route/
state, observable, artifact digest, and production-shaped browser/auth
context. Missing, stale, malformed, head-mismatched, body-mismatched, or
incomplete proof returns a typed blocker before the first CI-trigger effect.
`QA_CAPABILITY_BLOCKED` routes only the proof step to a provisioned owner; it
never waives the proof or authorizes CI.

After a causal E2E `PRODUCT_REGRESSION`, the final exact head additionally
requires the prescribed production-shaped local PASS. That local-pass rule is
not a caller-selectable exemption: initial admission, confirmed
infrastructure, capture/strict-replay, and control-plane-only cases retain
their existing separate release rules, while UI visual proof remains bound to
the current issue contract whenever deterministic visual ACs exist.

Re-read the head and issue-body SHA again immediately before the transition.
Use only the one canonical CTO-owned trigger boundary; PM may carry status or
evidence but cannot invoke it. Do not call a retired readiness executable,
legacy state writer, PM route, raw workflow command, or alternate admission
path. Preserve the existing pre-merge visual-proof check as defense in depth.

Non-UI PRs with no visual contract retain their existing trigger path. A
substantive code review terminal may be `APPROVE_CODE_PENDING_QA_VISUAL_PROOF`
without becoming a CI-ready PASS; only a valid exact-head/current-body proof
can clear the visual blocker.
