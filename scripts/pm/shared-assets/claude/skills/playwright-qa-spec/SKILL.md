---
name: playwright-qa-spec
description: Prove a browser acceptance criterion with a logged Playwright qa-tests spec on a remote QA box.
---

# Playwright browser QA

Use for an AC requiring a real rendered browser, gesture, multi-tab behavior, screenshot or video. Use a focused Vitest integration test on the remote test box instead when the AC is fully provable at the real component/server control point without a browser. Use both when each proves a different requirement. Browser QA is scripted Playwright, never agent-browser; no host test runs. Keep qa-tests out of CI (rule 39). Use synthetic JWT only, never live Clerk (rule 41).

## [HIGH] Environment setup = internal mutations, not seed restore (Rajiv 2026-11-11)

Source: Rajiv C0ALZJHGE49 thread 1794234970.206389, 06:56 IST: *"there are more specs in qa-tests. update the skill with examples. for all editor tests, just clone the sample project with the internal mutation and open the editor with it. use internal mutations to setup the test environment, fixtures/workspaces, etc."*
- **Every editor spec:** clone the sample project with the internal clone mutation (`cloneTestProject` in `tests/e2e/helpers/convex-run.ts`), then open the editor URL for the clone. No fresh jobsheet/upload unless the AC is creation, upload or processing.
- **Everything else** (workspaces, members, fixtures, practice assets, lease preconditions that a real user flow would create) is set up through **internal mutations** via `convexRun`/helpers in `convex-run.ts`, not hand-built IDB/editor state and not dependence on preflight seed-restore steps.
- **Reuse before writing:** `tests/e2e/specs/qa-tests/` has ~130 specs. Copy the setup pattern from the closest existing one. Examples:
  - `audit-open-current.spec.ts`: `cloneTestProject(SEED_PROJECT_MEDIUM, TEST_CONFIG.ADMIN_EMAIL, TEST_CONFIG.ADMIN_TOKEN_IDENTIFIER)` → `page.goto(editorUrl)`, plus a dedicated seed/cleanup helper pair (`seedAuditOpenCurrent` / `cleanupAuditOpenCurrent`).
  - `accept-all-long-seed.spec.ts`, `9289-real-reject-ordering.spec.ts`, `8111-ownsave-ack-refetch.spec.ts`, `agent-phrase-lock.spec.ts`: clone-and-open editor specs.
  - Pattern:
    ```ts
    const c = cloneTestProject(SEED_PROJECT_MEDIUM, TEST_CONFIG.ADMIN_EMAIL, TEST_CONFIG.ADMIN_TOKEN_IDENTIFIER);
    try {
      await page.goto(`/dashboard/editor/${c.transcriptId}`);
      // ...steps per AC...
    } finally { deleteTestProject(c.projectId); }
    ```
- Always clean up what you created (`deleteTestProject` / matching cleanup helper) in `finally`.

## Write the spec

1. Add or extend `tests/e2e/specs/qa-tests/<behavior>-<issue>.spec.ts`; this is the `qa-tests` Playwright project. Do not add to core or CI. Commit/push the spec on the exact branch/head to be tested.
2. Import `test`, `expect`, `TEST_CONFIG` from `../../setup/test-setup`; use existing helpers in `../../helpers/convex-run`, `project-test-utils`, and `test-utils` as applicable. Require synthetic auth and required seed inputs; missing inputs are UNAVAILABLE, not skipped PASS.
3. For ordinary editor flows clone the non-customer medium seed under the synthetic owner using `cloneTestProject`, then navigate via `ProjectTestUtils` and wait for actual editor readiness. For creation/upload/processing ACs create a fresh project through the real UI. Do not hand-seed lease, editor, recovery, or application state to manufacture the behavior; exercise the real user and server paths.
4. Give each AC its own `await test.step('AC-1: <behavior>', async () => { ... })`, observable assertions, and named screenshots where needed. Assert the server/DB state through an appropriate existing read-only helper such as `convexRun` or `getTranscriptPipelineState`, before cleanup; save a sanitized readback as an attachment. UI evidence alone does not prove persistence.
   Specify the actual product transition and assert its outcome at the server: for Reload saved, read back the saved server head; for a writer move, read back the lease handoff and resulting holder. Assert the relevant UI too, but never substitute UI text for the server outcome. Choose selectors from the current product contract and rendered target route/state: a submitted practice document is a read-only div, not a ProseMirror editor, so do not wait for `.ProseMirror` after submit.
5. For multi-tab proof create one browser context **per tab**, with the same appropriate synthetic storage state and explicit `recordVideo` per context; attach the logger below to every page. Close each context before collecting its video. A separate context changes browser identity; for same-browser lease semantics use an explicitly preserved shared browser identity and verify the scenario really represents it. Capture trace for every context; Playwright's fixture trace does not automatically trace manually created contexts (`context.tracing.start` / `stop` to distinct files).
6. Put logging in every spec using the following pattern. Place a shared helper in `tests/e2e/helpers/qa-logging.ts` via a separate app PR if reused; this skill does not edit the app repo. Each Playwright `testInfo.outputPath` is unique per test/retry and is copied within the leg's `test-results` before the next leg. Attach before navigation. Never log response bodies, headers, query strings, tokens, transcript text or customer content; raw console/error strings can contain secrets, so retain only sanitized synthetic-data logs privately.

```ts
import { appendFileSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import type { Page } from '@playwright/test';

export function qaLog(page: Page, file: string, tab: string) {
  mkdirSync(dirname(file), { recursive: true });
  // Deliberately record event metadata only: no URLs, error messages, or content.
  const write = (event: string, details: Record<string, unknown> = {}) =>
    appendFileSync(file, JSON.stringify({ ts: new Date().toISOString(), tab, event, ...details }) + '\n');
  page.on('console', msg => write('console', { level: msg.type() }));
  page.on('pageerror', () => write('pageerror'));
  page.on('requestfailed', req => write('requestfailed', { method: req.method(), resource: req.resourceType() }));
  page.on('response', res => {
    if (res.status() >= 400) write('response', { status: res.status(), method: res.request().method(), resource: res.request().resourceType() });
  });
}
// In the test: qaLog(page, testInfo.outputPath('browser.jsonl'), 'tab-1');
```

For additional diagnostic detail, add explicitly reviewed redaction/allowlisting for the exact synthetic scenario, never blindly dump console messages or full URLs. Use `mkdir(..., { recursive: true })` and per-leg/test/tab paths: `QA_CONTROLLER_RUN_ID` is shared across legs, not a unique directory name.

## Run remotely

Prepare an isolated private dev env with synthetic JWT and a **non-production R2 bucket**; verify the bucket and endpoint before running. Never use a production bucket, shared PM deployment or another slot's backend. `--allow-seed-replace` is safe here only because `--disposable-convex` creates an owned fresh dev deployment. Use a valid logical slot number and its explicitly supplied isolated `--env-file`; this is not permission to borrow a slot's backend. Obtain the exact pushed SHA and an absolute new output directory. First run the exact command with `--dry-run` appended:

```bash
~/.claude/scripts/remote-qa-spec.sh \
  --pool --priority p2 --disposable-convex --requires-modal \
  --slot N --branch BRANCH --head SHA40 --env-file /private/qa-dev.env \
  --spec tests/e2e/specs/qa-tests/NAME.spec.ts --out /absolute/new/evidence \
  --allow-seed-replace --trace --mutate-patch /absolute/test-only.patch \
  --legs GREEN,RED,GREEN --dry-run
# After inspecting the dry run, repeat without --dry-run, in background.
```

`--requires-modal` is accepted but the spec lane always deploys its own Modal; strict replay is the default, **not** a `--strict-replay` flag on this script. Do not pass `--on-miss` for strict proof. `--trace` retains trace and video per test. The patch must be a readable git diff limited to `tests/e2e/**`; it applies only for RED, reverses afterwards, and the controller checks Playwright failed rather than setup alone. Inspect the named failing assertion and both GREEN legs, not just the exit code. This is a **test mutation**, not product-fix RED-on-revert: separately prove the behavioral regression against a safely reverted fix at an exact head on an isolated remote box using the applicable remote-test/QA route. Do not claim product RED-on-revert if unavailable. For a GREEN-only run omit both `--mutate-patch` and `--legs`.

## [HIGH] State debug logs + iterate until it works (Rajiv 2026-11-11)

Source: Rajiv C0ALZJHGE49 thread 1794234970.206389, ts 1794383415.272539: *"are debug logs being added to the specs? the agents should fix the bugs in the spec and keep iterating till it works."* Then ts 1794383523.751909: *"update the qa spec skill"*.
- **Log app state, not just console.** At every AC step the spec writes one JSONL line via `qaLog` with the domain state the step depends on. For editor/sync specs that means: sync phase, dirty/rowDirty, attempt/reservation present, conflict/incompatibleSave code, server head version (internal readback), lease holder/session, save request→response code, and the visible bar/dialog state. Read IDB or internal queries the same way existing specs do (`read()` helpers); never mutate state to log it.
- **Classify before routing rework.** On failure, read the assertions and Playwright error-context FIRST, ahead of larger artifacts, wherever the path you are using lets you read them separately (see "Artifact retrieval" below). Record the first failing boundary in the existing output/receipt as `product | harness | oracle | UNKNOWN`, with evidence and the failing assertion; use UNKNOWN when evidence is insufficient. Then inspect JSONL/trace and iterate on proven spec or harness defects (selector, ordering, completion signal, setup). Route a proven product defect to the authorized product owner, with exact evidence; do not attribute an oracle or harness failure to the product.

**Artifact retrieval (existing path, no new knobs)**
- **Do not set `RTB_FETCH_TIMEOUT_SECONDS` on `remote-qa-spec.sh`.** The variable is read by the shared SSH transport (`tracked_rssh` in `remote-test-box.sh`), so on this script it also wraps the live Playwright SSH leg and can kill a legitimate run mid-test. The run budget stays as the controller sets it; never extend or shorten it to get a fetch through.
- **What the spec lane does.** During cleanup the controller takes two frozen snapshots of `/root/qa/out`: small evidence first (assertions, error-context, logs and readbacks), then deduplicated media (trace ZIPs and videos). Each uses the existing leased/generation-fenced, hash-verified `remote-browser-box.sh fetch` path. Only artifact operations receive scoped deadlines (300s small transfer, 900s media transfer, each with bounded controller grace); the Playwright budget is unchanged. Read small evidence first while media is collected. Media publication verifies hashes and restores duplicate paths before the box is destroyed.
- **Media PARTIAL is not lane-ok.** A media timeout, publication failure or missing trace/video preserves the fetched small evidence and original Playwright result (`lane.rc` / `playwright_rc`), but emits `media_status=PARTIAL`, `verdict=lane-partial` and a nonzero terminal exit (3 for media-only incompleteness; earlier failures retain their code). `lane.txt` records `media_status` and `terminal_rc`; `terminal.rc` records the final exit separately. Required-video QA is incomplete even when Playwright passed. Inspect `fetch.log` and recovery paths; never rerun Playwright solely to retry a fetch. COMPLETE here establishes media presence/publication, not per-tab or per-AC sufficiency, which still needs review.
- **Where the bounded, owned fetch is available directly.** `~/.claude/scripts/remote-browser-box.sh fetch <box> <exact absolute remote path> <absolute local dir>` is the existing artifact-fetch step for a box you still hold (for example a detached capture or remote-browser session before `down`). It takes one exact artifact path per call (no globs, no `..`, no existing destination), runs through the lease generation fence so only the owning lease can use it, hashes a frozen snapshot on the box, verifies before publishing, and refuses after teardown. A failure leaves a named recovery directory; it never reaches into the live test process. Use it to pull a small assertions file first and the large artifacts after, only when the box is still held.
- **Fetch failed or timed out = UNKNOWN, not evidence.** Report the fetch log and recovery directory, mark the affected artifact UNKNOWN, and do not re-run the Playwright leg just to re-fetch.
- **Valid stops:** a passing run with every tab's video, OR a proven product bug with log lines, OR an environment blocker the runner can't fix (named exactly). "The test failed" alone is never a terminal result.
- **Make the trigger visible.** Prefer setting up the precondition through internal mutations/real server paths (e.g. advance the server head through the real save path, create a lease via the real `claimSession`) over invisible network tricks (offline toggles, request rewrites), so the video shows the scenario. Network interception is for secondary/edge cases only.
- Never weaken assertions, add sleeps or raise timeouts to get green.

## [HIGH] Learnings from the P0 #13435 / #13357 marathon (Rajiv 2026-11-11)

Source: Rajiv C0ALZJHGE49 thread 1794234970.206389, 18:04 IST: *"finally! update the qa spec skill with all the learnings from this marathon session. this needs to be released asap."* Each item below cost at least one failed or orphaned run that day.

**Spec correctness**
- **Use the real wire values.** Copy enum/literal values from the production source (`grep` the type), never from memory or the issue text. A spec asserted `page_direct` when the wire value is `sync_manager_direct`; it failed on correct product behavior.
- **Wait for the test's own baseline to be admitted** before any probe. The first edit's save must reach server admission (internal readback shows the new head) before you go offline, open tab 2, or trigger Reload/Continue. Otherwise you test a race you created.
- **Never delete or mutate the fixture while an upload/save is in flight.** Cleanup goes in `finally`, after the last awaited save completion, never in a parallel step.
- **Assert durable outcomes, not legacy fields.** Prefer conflict/API response codes, server head readback and visible UI state over transitional fields (e.g. `incompatibleSaveCode`). Dropping or changing an assertion is allowed only in the PR's OWN new spec and is recorded in the PR (S109); baseline/core assertions are never loosened.
- **Scope every selector.** `getByRole('button', { name: 'View' })` matched two buttons (strict-mode failure before the AC). Scope to the menubar/toolbar container or use `exact: true` plus a container. UI copy changes break specs: the stale `/Scribie/` template label is now **"Timestamped blank"**. Grep current copy before writing a text selector.
- **Compare head vs main when useful.** The same spec on current main can help isolate a regression, but a failure on both is not proof of a spec/environment defect and a head-only failure is not by itself proof of a product defect. Classify the first failing boundary from the actual evidence.

**Running**
- **Bind to the exact PR head.** Record the 40-char SHA. If the head moves only through QA-spec commits, say so explicitly ("product diff unchanged since <sha>") in the evidence; any product commit means a new run.
- **Env:** pass `--env-file /Users/rajiv/Downloads/projects/heydonna-app-3005/.env.local` **by path only**. Never cat/grep/print it (the secret-dump guard blocks it, and grep output leaks values). QA bucket is `heydonna-uploads`; always `--disposable-convex --requires-modal`.
- **Lease priority + wait.** P0/data-loss work uses `--priority p0`. Run in background and use `capture-wait`; **never exit, cancel, or let the parent die while your run is queued.** Two P0 runs were orphaned that way. If the pool is saturated, report queue position and keep waiting (or PM adds a box); don't resubmit duplicates.
- **Stale warm-pool deps** (missing module, odd type errors on a box only) are environment, not product: the box reinstalls on a lockfile-hash change; report the box id if it recurs.
- **Verify teardown** every run: Convex deployment 404, Modal app stopped, pool lease released. Missing receipt = UNKNOWN.

**Evidence**
- **One JSONL marker per step** (`{step:'AC-2:start'}` / `AC-2:pass`) so PM can map each video timestamp to an AC. Mark the route/tab and action on the clip as well.
- In the existing runner receipt record the scenario, target route/tab, storage scope and required browser contexts; include the first-failing-boundary classification when a run fails.
- **Read back, never assert from the brief (retro #13357/#13435; Rajiv ts 1794580969.974139).** Before the action under test, the scenario runner (`qa-brief/scenarios/run.py`) reads back the ACTUAL tab count, URL (path and origin), browser identity (a hash of `localStorage['heydonna-device-id']`, never the raw id) and storage scope, and compares them with the receipt's declared values. Write the evidence section from that readback (`readback.before_action`, `receipt_status`), not from what the brief says should be true. A mismatching, missing or malformed receipt, including a truthy value that is not an object, becomes `PARTIAL`/`UNKNOWN` in the output with the reason. It never raises and never refuses the run.
- **Two tabs in one profile are not two browsers.** A different-browser scenario (for example a same-user writer move, rule 48) needs two distinct browser identities read back (`independent_browsers: true`). Two tabs of one persistent profile share one `heydonna-device-id`, so the runner reports `two_tabs_one_profile` and the claim stays `PARTIAL`. To prove a different-browser move, launch a second isolated browser context or profile with its own storage; do not call a second tab a second device.
- **Markers use the recording clock or are UNKNOWN.** Each event carries `elapsed_s` (scenario clock, includes setup before the recording) and `recording_s` (seconds from the capture controller's `recording.started_ms`). Quote `recording_s` when pointing a reviewer at a video timestamp. When the controller wrote no usable start (`recording_clock=unavailable`), `recording_s` is `UNKNOWN`; say so in the receipt rather than guessing from `elapsed_s`. A negative `recording_s` means the action happened before recording began.
- **UNKNOWN is a result.** An unreadable probe, an absent receipt field or an unavailable recording clock is reported as UNKNOWN/PARTIAL in the artifact the PM reads. The run still completes and its other evidence still counts.
- **Preserve per-tab video:** close every page, then its context, before reading `page.video().path()`. Closing the browser first, or a crash in cleanup, loses the video. Wrap cleanup so video save runs even when the test fails.
- **Per-AC screenshots + MP4 with sha256** in the PR comment, plus the server readback. PM uploads to Slack and labels partial/historical clips honestly (never present an older head's clip as the current proof).
- **Playback/asset ACs:** record R2/asset request status (method, resource type, status only) for the audio and speaker-sample fetches; a failed asset fetch, not the player, was the cause of a customer report.

## Debug with logging

- In each AC step record timestamps and assertions. Use the JSONL event stream plus `leg*/preflight.log`, `assertions.txt`, Playwright logs, screenshots and traces; correlate by timestamp and tab. For app-specific extra diagnostics, set `NEXT_PUBLIC_E2E_DEBUG=true` in the private QA env **before Next build/start**; it enables only instrumentation already guarded by that flag, not universal verbosity. Never print the env file. Use `DEBUG=pw:api` when needed only after verifying the controller actually forwards it to the box; otherwise arrange box-side env through the approved runner and report if unavailable. Debug logs can contain sensitive data.
- Before teardown, retain sanitized box-side Next (`next.log` or the actual preflight Next log), disposable Convex and owned Modal logs under the evidence directory. The controller copies `/tmp/preflight-*-slot*.log` and fetches `/root/qa/out`; it does **not** guarantee every service log is there. Arrange capture into that output while the owned run is live, without printing credentials; if a log cannot be collected, mark it UNAVAILABLE with reason. Never claim collection by assumption. Keep raw logs private.
- Open a retained trace on an appropriate workstation: `npx playwright show-trace /absolute/path/trace.zip`; inspect the Network tab, action timeline, console and snapshots. Traces can contain private data; do not upload to a public viewer or commit them.

## Failure signatures

- Editor readiness timeout + `Transcript not found` + R2 `CLIENT_FETCH_FAILED`: investigate storage, CORS and QA environment/bucket binding first; verify non-prod bucket. The `px-qa-r2-isolation` guard is being added; do not assume it is deployed.
- `Canonical QA overlay anchor drifted`: compare exact checkout preflight with deployed `remote-qa-modal.py` overlay anchors. A 2026-11-11 fix exists; verify the installed version and report new drift, never bypass the guard.
- `EEXIST` using `QA_CONTROLLER_RUN_ID`: it is reused for all legs; make separate leg/test directories with recursive mkdir.
- HTTP 424 fixture miss (possibly wrapped by 500): strict replay lacks a matching fixture; identify the request safely, use the existing fixture route, never silently enable live calls or call it PASS.
- Auth redirect / synthetic JWT failure: inspect sanitized auth readiness, subject/verifier/expiry and deployment alignment; never use live Clerk. Missing inputs, timeout or missing logs = UNAVAILABLE with reason.

## Handoff

Retain exact head, command, rc and per-leg verdict/assertion lines; sanitized DB readback; every tab's video and trace, JSONL and service logs, with SHA256; `lane.txt`, `legs.tsv`, tooling hashes and teardown receipts. Verify `remote-teardown.txt`/Modal stopped, Convex deletion receipt and 404, and `teardown.txt` pool release; missing or ambiguous proof is UNKNOWN, not success. Preserve private evidence securely; do not post credentials, customer data or raw trace files.

Send PM the artifact paths and hashes, head, test/leg results, teardown status, and **every video** converted to H.264 MP4 plus a per-AC PASS/FAIL/UNAVAILABLE summary with video timestamps and the corresponding DB readback. PM reviews and posts every video and per-AC summary to the source thread. Do not post to Slack from this workflow unless explicitly authorized.
