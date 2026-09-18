---
name: heydonna-review-cap-cto-approve-handoff
description: |
  Handle the HeyDonna handoff when a PR is in a Codex review-cap / review-loop
  state and CTO/PM gives explicit APPROVE without a genuine codex-review-companion
  marker. Use when: (1) `request-label-gated-ci.sh` rejects the slot-ready event with
  "PR #N packet lacks a genuine current-head approving Codex marker or a fully
  cited PM override", (2) `/tmp/pm-review-loop-{pr}.json` exists and ordinary
  slot-driven CI dispatch is blocked, (3) the PR carries `pm-state:blocked-rework`
  or `pm-blocked:*` labels. NOT for: ordinary PRs with a real Codex APPROVE marker
  (use normal slot-ready flow), PRs needing code changes (rework first), or cases
  where the review-cap has not been acknowledged by PM/CTO in writing.
author: Claude Code
version: 1.0.0
date: 2026-07-30
last-validated: 2026-07-30
supersedes: []
---

# HeyDonna Review-Cap CTO-Approve Handoff

## When NOT to Use
- Do NOT use when the PR already has a real `codex-review-companion` APPROVE marker — run the normal `request-label-gated-ci.sh` flow.
- Do NOT use to fabricate Codex markers or override authority; the CTO/PM APPROVE must be explicit and written.
- Do NOT use if code changes are still required; this is a verification/handoff skill, not a rework shortcut.

## Problem
A PR hits `CODE_REVIEW_CAP_REACHED` (repeated blocker class) or has an active
review-loop file (`/tmp/pm-review-loop-{pr}.json`). The slot receives a message
such as:

> "CTO APPROVE at {head}. Run one real exact-head CI test + primary E2E cycle on
> this head, then canonical merge-ready admission. No code changes needed."

The slot assembles readiness evidence with a PM-authored review marker,
but the PM-owned CI-start wrapper rejects it.

## Context / Trigger Conditions
Exact symptoms that indicate this skill applies:

1. Slot-side readiness evidence exists for the exact head.
2. `/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/ci/request-label-gated-ci.sh --slot-ready-event /tmp/slot-ready-events/slot-{N}-pr-{PR}-{head}.json` fails with:
   ```
   PM_TRANSITION_FAILED exit=1 reason=PR #{PR} packet lacks a genuine current-head approving Codex marker or a fully cited PM override
   ```
3. A review-loop file exists: `ls /tmp/pm-review-loop-{PR}.json`.
4. The PR labels include `pm-state:blocked-rework` or `pm-blocked:*`.
5. No genuine `/tmp/codex-app-code-review-{PR}.txt` or `MARKER_PROVENANCE: codex-review-companion` marker exists for the current head.

## Solution
The slot's job stops at submitting a clean slot-ready packet and notifying PM.
Only PM-owned transitions can clear the review-loop gate.

### Slot-side steps
1. Ensure the branch is at the exact head PM approved.
2. Verify ancestry of the pinned-main SHA.
3. Re-run affected tests / affected-test-plan for the current head.
4. Create or update a pinned-main receipt and a PM-override review marker for the current head (record the PM/CTO approval verbatim, including source citation and rationale).
5. Assemble the slot-ready packet contents for the exact head (review-proof
   path, affected-test proof/plan paths) and hand them to PM with the
   notification in step 6. No supported slot-side packet submitter exists,
   so no packet is submitted from the slot.
6. Try the CI-start wrapper once to surface the blocker, then stop.

### PM-side next steps
Both PM-owned clear-gate transitions are retired. PM returns the typed stop
for the requested class and performs no state mutation:

- `UNSUPPORTED_LIFECYCLE_ACTION:accept-ready` — the override marker stays as
  evidence only; PM does not proceed to label-gated CI on this head without
  a genuine marker.
- `UNSUPPORTED_LIFECYCLE_ACTION:rescope-decide` — same; no override decision
  is recorded and no state changes.

## Verification
- Slot-side evidence (exact head, review marker path, affected-test proof path) is assembled and handed to PM.
- `request-label-gated-ci.sh` rejection message is exactly the "lacks a genuine current-head approving Codex marker" text (any other failure requires different handling).
- PM is notified with the exact head SHA, review marker path, affected-test proof path, and the typed stop (no PM transition command exists).

## Example
PR #6925, issue #6907, slot 1, head `ee8b7add9...`:

```bash
cd /Users/rajiv/Downloads/projects/heydonna-app-3001
git checkout fix/6907-docx-export-text-fidelity-oracle
git pull origin fix/6907-docx-export-text-fidelity-oracle
git merge-base --is-ancestor dae76da13 HEAD  # OK

/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/affected-test-plan.py \
  --pr 6925 --write --verify

# (slot-side packet submission retired; the assembled evidence above goes to PM directly)
```

Then notify PM that `request-label-gated-ci.sh` is blocked and that the PM-owned
clear-gate transitions are retired (`UNSUPPORTED_LIFECYCLE_ACTION:accept-ready` /
`:rescope-decide`); PM performs no state mutation.

## Notes
- A review proof carries an approving verdict; on its own it does not prove a genuine Codex marker.
- `request-label-gated-ci.sh` enforces the stricter marker requirement because the PR is in a review-loop / blocked-rework state.
- Do not self-author a `MARKER_PROVENANCE: codex-review-companion` marker; that is fabrication and invalidates the handoff.
- Keep the PM-override marker verbatim: quote the PM/CTO approval, timestamp, source citation, and rationale.

## References
- `.claude/rules/99-pm-fable-review-cap.md` — terminal review adjudication policy.
- `/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/ci/request-label-gated-ci.sh` — PM-owned CI-start wrapper.
