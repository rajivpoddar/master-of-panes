---
name: heydonna-cto-label-gated-ci
description: CTO lane for HeyDonna PR merges and rule-62 main integration. CI/E2E admission is PM-owned and is never dispatched from here.
---

# HeyDonna PR conveyor: CTO lane is merge-only

CI/E2E ADMISSION IS PM-OWNED (Rajiv, ts 1795848120.047269: "cto is not supposed to admit ci at all, only do merges").
The ONLY path that dispatches CI/E2E is `~/.claude/scripts/ci/pm-admit-ci.sh <PR>` (PM runs it). It enforces ONE paid CI/E2E round per PR
(Rajiv C0ALZJHGE49 thread 1795841629.732639, ts 1795845160.422969 + ts 1795845739.605399): every later head is proved on remote boxes scoped to the
failing tests, the QA videos are posted to Slack, and the CTO then does a head-pinned S167 admin merge.

`admit.py` in this skill no longer dispatches anything. Any invocation other than `--integrate-only` returns
`REFUSED: CI_ADMISSION_OWNED_BY_PM`. Never create or attach a `ci-head:<sha>` label by hand, and never raw-dispatch workflows. If a PR needs CI, ask PM.

This skill and `heydonna-open-pr-status` are the CTO conveyor. Use only their bundled scripts, Python's standard library, Git, and authenticated `gh`.

## What the CTO lane does here

1. Bind the reviewed changed-file scope to the exact live head. An authoritative exact-head classifier receipt confirming marketing website/blog-only
   `site` scope, with no app code or shared app runtime/dependency change, does not require paid app CI/E2E. Require the relevant site build,
   media/asset checks, and exact-head visual QA for UI. App, mixed, and unknown scope are never exempt. Do not call the site-only case dual-green
   or a red-app-workflow waiver.
2. For a NEW or MOVED UI change (placement/layout/shell) or a visual-only change no test can assert, inspect its exact live head and require a PASS
   from the versioned visual-proof gate before the merge ask. Any other behavior change with `Visual proof: WAIVED — covered by <test file::cases>`
   and exact-head integration/unit tests through the real control point needs NO visual gate (Rajiv 2026-10-29, C0ALZJHGE49 thread 1793275486.848389,
   ts 1793295441.005939 / 1793295548.599969 / 1793295657.837009; buddhi S106). Run (UI-gated cases only):

   ```bash
   python3 /Users/rajiv/.claude/scripts/qa-visual-proof-gate.py --pr PR --repo Scribie/heydonna-app --expect-head FULL_HEAD --json
   ```

   Require exit status 0 and `ok: true`; do not use `--skip-artifact-availability`. The gate verifies current Slack v2 receipt bytes and their file IDs
   in the declared PR/head/AC thread, while retaining valid legacy durable artifact receipts. A missing, stale, malformed, inaccessible, or mismatched
   proof blocks the merge with a typed result; request corrected proof without requiring Rajiv's reply. If exact-main integration changes the head or
   visual behavior, refresh the affected proof and rerun the gate on the new full head. Non-UI changes need no visual proof.
3. Rule-62 main integration (merge only when GitHub reports a conflict or the PR's files overlap files main changed). Read-only preview, then apply:

   ```bash
   python3 /Users/rajiv/.codex/skills/heydonna-cto-label-gated-ci/scripts/admit.py --pr PR --head FULL_HEAD --integrate-only
   python3 /Users/rajiv/.codex/skills/heydonna-cto-label-gated-ci/scripts/admit.py --pr PR --head FULL_HEAD --integrate-only --apply
   ```

   Statuses: `MAIN_INTEGRATION_REQUIRED` (overlap; apply to merge main into the head with an old-head lease), `MAIN_INTEGRATED` (new head; match existing
   QA to it and rerun the visual gate), `MAIN_INTEGRATION_NOT_REQUIRED`, `READY_TO_ADMIT` (no integration needed; hand back to PM), and
   `ALREADY_ADMITTED` (an existing request or runs; consume workflow events, do not poll or retrigger). A conflict or failed probe never touches the head;
   send conflicts to the existing owner. `*_UNCERTAIN` means reconcile the named effect before any retry. Integration never dispatches CI/E2E and
   never merges the PR; a CI-input integration is a separate step followed by a remote-box run, never a second CI/E2E round (rule 62).
4. Merge: after PM's remote-box proof and the Slack-posted QA videos, do the head-pinned S167 admin merge (enforce_admins off, merge the pinned head, on, readback).
