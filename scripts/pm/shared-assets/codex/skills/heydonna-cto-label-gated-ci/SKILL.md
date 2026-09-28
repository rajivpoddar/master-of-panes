---
name: heydonna-cto-label-gated-ci
description: Admit one HeyDonna PR to the exact-head checks required by its reviewed scope.
---

# HeyDonna CI admission

This skill and `heydonna-open-pr-status` are the entire PR conveyor. Use only
their bundled scripts, Python's standard library, Git, and authenticated `gh`.
Never use PM scripts under `.claude` for admission or merge.

Bind the reviewed changed-file scope to the exact live head before admission.
An authoritative exact-head classifier receipt confirming marketing
website/blog-only `site` scope, with no app code or shared app
runtime/dependency change, does not require paid app CI/E2E. Require the
relevant site build, media/asset checks, and exact-head visual QA for UI.
App, mixed, and unknown scope still require one exact-head CI/E2E pair; missing,
unknown, or mismatched scope is never exempt. Do not call the site-only case
dual-green or a red-app-workflow waiver.

The current `admit.py --apply` path always requests both paid workflows. Do not
use it or add `ci-head:<full-head>` for confirmed site-only scope; use the
existing exact-head site-proof path instead. Making executable admission
scope-aware is a separate follow-up, not a reason to delay this policy.

1. For a UI change, inspect its exact live head and require a PASS from the
   versioned visual-proof gate before requesting admission. Run:

   ```bash
   python3 /Users/rajiv/.claude/scripts/qa-visual-proof-gate.py --pr PR --repo heydonna-app/heydonna-app --expect-head FULL_HEAD --json
   ```

   Require exit status 0 and `ok: true`; do not use
   `--skip-artifact-availability` for admission. The gate verifies current
   Slack v2 receipt bytes and their file IDs in the declared PR/head/AC thread,
   while retaining valid legacy durable artifact receipts. A missing,
   stale,
   malformed, inaccessible, or mismatched proof blocks admission with a typed
   result; request corrected proof without requiring Rajiv's reply. If exact-main
   integration changes the head or visual behavior, refresh the affected proof
   and rerun the gate on the new full head. Non-UI changes need no visual proof.
2. For app, mixed, or unknown scope, inspect the live head, then run the
   bundled script. It integrates exact current main by an ordinary merge in a
   temporary checkout and pushes only a descendant with an old-head lease. A
   conflict returns for resolution by the existing owner. A changed visual
   surface stops before the CI label is added. For confirmed site-only scope,
   do not use this script's `--apply` path; verify the exact-head site proof.
3. For app, mixed, or unknown scope, add `ci-head:<full-head>` once to start
   real `pull_request` CI and E2E. Keep existing admissions and unrelated
   labels. Admission never merges the PR. Site-only scope gets no paid pair.

```bash
python3 /Users/rajiv/.codex/skills/heydonna-cto-label-gated-ci/scripts/admit.py --pr PR --head FULL_HEAD
python3 /Users/rajiv/.codex/skills/heydonna-cto-label-gated-ci/scripts/admit.py --pr PR --head FULL_HEAD --apply
```

The first invocation is read-only; all commands above apply only to
pair-requiring scope. For UI changes that need main integration, use
`--integrate-only --apply`, match existing QA to the resulting head, then
rerun the visual-proof gate and invoke `--apply` on that head. Do not invent a
separate QA receipt format.

`ADMISSION_REQUESTED` means the label was stored, not that jobs started.
`ALREADY_ADMITTED` preserves the existing request or runs, including a partial
pair. Consume workflow events; do not poll or retrigger. `*_UNCERTAIN` means
reconcile the named effect before any retry. Never raw-dispatch workflows.

If auxiliary tooling delays this path for five minutes, use the same direct
Git/GitHub operations with the current head and duplicate check. Repair the
tooling separately. Add no other dependency, review, or approval step.
