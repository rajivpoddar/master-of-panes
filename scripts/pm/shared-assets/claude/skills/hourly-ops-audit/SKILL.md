---
name: hourly-ops-audit
description: |
  Apply the canonical procedure when MoP injects an "MoP: hourly ops audit for <reason>"
  payload into the PM pane. The payload is structured exceptions-only output from
  .claude/scripts/hourly-ops-review-bg.sh (Codex + local-signal review of the
  hourly execution surface — active slots, active PRs, current CI/QA/review blockers,
  immediate dispatch capacity, release-safety alerts, and the PM ops `hourly` snapshot).

  Trigger: pm-context-injector.sh emits `[MoP_SLOT_NOTIFICATION] hourly-ops-audit ...`
  when the first line of the prompt starts with `MoP: hourly ops audit for `.

  Owner: PM (Dhruva). Slots have no role.

  NOT for: ad-hoc Slack/GitHub investigation (use codex-comment-processing, alert-processing,
  external-comms-investigation). The hourly-ops-audit payload IS the investigation;
  PM's job is to execute the action items.
author: PM-direct (Dhruva)
version: 1.4.0
date: 2026-07-01
last-validated: 2026-07-01
---

## RETIRED HELPERS (Rajiv directive 2026-09-21)

`pm-transition.sh`, `pm-state-replace.sh`, `pm-transition-alert.sh`,
`slot-dispatch-sweep` and the rest of the legacy `.claude/scripts/pm-*`
control helpers are DEPRECATED and REMOVED. Do not invoke them, do not
restore them, and do not treat their absence as a defect. Any stale command
line below that names one of them is obsolete guidance: execute the
equivalent step through the current canonical path instead (MoP slot REST
routes, GitHub labels via gh, and the installed conveyor skills), and treat
PM state labels as advisory projections that never gate review, CI, release
or merge. A bounded cleanup of the remaining references is owned by the MoP
task; do not hand-edit this skill for it.

# Hourly Ops Audit — Execute Action Items

## Rajiv product/process decision gate

PM may diagnose freely and may execute only a mechanically determined action
under an existing Rajiv-approved rule. A product decision changes customer
behavior, scope, priority, data, or acceptance criteria. A process decision
changes, waives, overrides, excepts, or chooses among Ready Pool, slot,
PR-conveyor, review, CI/capture/rerun, merge/rollout, monitor, or automation
paths. For either class, stop before mutation and post one evidence-bound
recommendation to Abhijit CTO in `#heydonna-dev`. CTO alone DMs Rajiv and waits
for explicit approval. Multiple eligible choices, force/override, or material
drift requires escalation; PM never DMs Rajiv directly.

## Canonical location

This project-scoped PM skill and its project-scoped hooks/scripts are canonical
for this checkout:

- `.claude/skills/hourly-ops-audit/SKILL.md`
- `.claude/hooks/ops-audit-skill-posttool.sh`
- `.claude/hooks/ops-audit-stop-validator.sh`
- `.claude/hooks/pm-state-promotion-proof-guard.sh`
- `.claude/scripts/hourly-ops-review-bg.sh`
- `.claude/scripts/hourly-ops-review-prompt.txt`
- `.claude/scripts/hourly-ops-row-ledger.py`

User-scope files may exist as compatibility copies, but PM behavior for this
project must be changed here first. Do not move PM skill logic back into
`~/.claude/skills`; PM skills for this checkout are project-scoped to
`.claude/`.

## Trigger

`pm-context-injector.sh` Shape 3 fires when the PM pane prompt starts with:

```
MoP: hourly ops audit
```

(Stable title since R4 — no reason in title; reason lives in body `REASON:` line. The hook also matches the legacy `MoP: hourly ops audit for <reason>` shape during migration.)

## Payload contract (R4)

The PM-facing body shape — already stripped of the scheduler-internal envelope (`INJECT_DECISION:`, `---`, `-- META:`) by `opsAudit.ts::sanitizePMPayload`:

```
MoP: hourly ops audit

REASON: <short_label>

ACTION_ITEMS:
  - ACTION: <imperative one-liner>
    EVIDENCE: <one-line trace>
    TRANSITION: <assigned|queued|traced|demoted|promoted|rerun|dispatched|closed|escalated|blocked>
    TARGET: <PR#NNNN|issue#NNNN|slot:N|alert-thread-ts|misc>
  - ACTION: <imperative one-liner>
    EVIDENCE: ...
    TRANSITION: ...
    TARGET: ...
  ...

NEXT: invoke Skill(hourly-ops-audit) to process these action items.
```

**REASON parsing:** read the `REASON:` line from the BODY (not the title). On legacy payloads that still carry an `INJECT_DECISION:inject REASON:<x>` first line, fall back to that — opsAudit.ts strips it from new payloads, so this fallback only fires when an older mop-server build is still injecting.

**Owner is IMPLICIT — PM (Dhruva) owns diagnosis and mechanical execution of every hourly action item.** Canonical hourly payloads MUST NOT emit `OWNER:` lines. PM may assign or hand off only when an existing Rajiv-approved rule selects one action without discretion. Every product or process decision is escalated to CTO with evidence; CTO DMs Rajiv and waits for explicit approval.

Legacy single-line shape (`- OWNER:dhruv EVIDENCE:... TRANSITION:... TARGET:... \n ITEM: ...`) MAY still be emitted by older bg-script versions or Codex drift; treat any `OWNER:*` value as PM-owned routing/escalation work. The new canonical shape is multi-line and ownerless-by-default. `opsAudit.ts::sanitizePMPayload` strips those legacy OWNER lines before PM sees them, but the parser remains tolerant.

The `NEXT:` line is informational — the PreToolUse guard + Stop validator enforce the Skill invocation mechanically. No action needed on the NEXT line itself.

`TRANSITION:` in the incoming payload names the required next transition, not a
claim that the transition already executed. PM completion must use the Step 5
proof tags (`EXECUTED`, `QUEUED`, `DISPATCHED`, `ESCALATED`, `BLOCKED`,
`SKIPPED_WITH_REASON`) and cite live mutation/blocker proof before saying a PR
was promoted, resolved, closed, or cleaned up.

When `INJECT_DECISION:skip` is emitted MoP suppresses the injection — PM never sees a healthy-tick payload.

## Procedure

### Step 0 — Respect the ops horizon split

Hourly ops is the execution loop. Its job is to keep current slots and PRs moving
to standalone `merge-ready`, `pm-state:qa-passed-awaiting-ci`,
`pm-state:pm-review-pending`, `pm-state:rescope-required`, a live blocked state,
or a released slot with next work dispatched.

Do not expand hourly into the full PM backlog or hygiene board:

- `hourly`: active slots, active PRs, current CI/QA/review blockers, immediate
  `status:todo` dispatch when capacity is free, one low Ready Pool reminder when
  clean unassigned `status:todo` drops below 10, P0/P1 release-safety alerts.
- `heartbeat`: session-age clear execution, stale-process cleanup, stale `/tmp`
  latches/files, stale `/tmp/slot-ready-events` packets, Slack/MoP/Kanban
  freshness, audit runner health, PM ops sync/prune/dedupe, and aggregate
  PM_TODO/LABEL/ISSUE drift cleanup that does not block a current slot
  transition.
- `daily`: full Ready Pool ordering, parked PRs, low/medium follow-ups,
  customer incident aging, blocked-on-CTO question review, full backlog
  triage/promotion strategy via `/Users/rajiv/.claude/scripts/backlog-triage.py`, and
  yesterday PM misses.

Use the PM ops SQLite ledger as authority and render/sync `pm-todo.md` after
state changes. `pm-todo.md` is not the hourly input source; the scheduler should
provide the `pm-ops.py snapshot --surface hourly --read-only` slice for
inspection. Scheduled hourly runs may omit `--read-only` when they intentionally
surface obligations and update `last_surface_at`.

### Step 1 — Verify payload shape

- Title line MUST be `MoP: hourly ops audit` (R4 stable) OR legacy `MoP: hourly ops audit for <reason>`. If neither, the prompt didn't actually trip Shape 3 — STOP.
- Body MUST contain a `REASON: <short_label>` line and an `ACTION_ITEMS:` block with ≥1 `- ACTION:` row.
- If a stray `INJECT_DECISION:skip` body line leaks through, that's a bug in `opsAudit.ts` (skip should suppress at the scheduler — not reach PM) — log it as `[ops-audit-bug] skip payload reached PM` and STOP.
- If zero `- ACTION:` rows on an inject-class payload, that's a bg-script bug — log it and STOP.
- `NEXT: invoke Skill(hourly-ops-audit) ...` line is informational; no validation needed beyond confirming the body parses.

**Row-ledger gate:** the Stop hook derives `/tmp/pm-hourly-ops-run.json` from
the original audit sentinel and treats every `- ACTION:` row as a durable row.
Completion is not based on prose or a generic "handled" summary. Every original
row must be closed by its matching Step 5 `[ACTION_N_TAG] ... proof:` line.
If an item is a legitimate wait, close that row as `BLOCKED` with the live
blocker and next owner/trigger, or `SKIPPED_WITH_REASON` only when live state
proves the row was stale/superseded. Leaving a row unmentioned keeps the audit
pending and the Stop hook will nag with the exact open row.

### Step 2 — For each action item: classify + execute

Parse each item block. An item is one logical entity starting with `- ACTION:` (canonical multi-line shape) OR `- OWNER:...` (legacy single-line shape). Fields seen on subsequent indented lines until the next `- ` or `---` belong to that item.

**Owner resolution (apply this first):**

- Every item is PM-owned. If a legacy `OWNER:*` line appears, ignore ownership and convert it into a PM action: dispatch/handoff to slot N with MoP receipt proof, or escalate to Rajiv with evidence. Do not treat `OWNER:slot` as permission for the hourly audit itself to become slot-owned.

**Transition-driven action (after owner resolution; applies to PM-owned items):**

**Current PR progress gate:** The hourly audit exists to keep current PRs moving
toward standalone `merge-ready`, not just to describe the board. For every PR
named by an action item or surfaced in drift/kanban evidence, PM must classify it
as `merge-ready`, `qa-passed-awaiting-ci`, `pm-review-pending`,
`blocked-rework`, `rescope-required`, active slot work, or cleanup/closed. If it
is not already validly `merge-ready`, PM must name and execute the next concrete
transition: readiness/Phase B promotion, PM Claude review start/consume, CI
trigger or watch row, slot-ready packet consumption, `block-pr`,
`assign-rework`, `pm-codex-pr-rescue`, `rescope-pr`, `rescope-decide`,
`drain-slot`, stale-label cleanup, or merged/closed cleanup. A passive status
line without a transition is not completion proof.

`pm-blocked:pm-gate` is not a quiet state by itself. If a blocked-rework PR has
no live MoP slot assignment, PM must either produce/verify the artifact or decision proof,
hand off a narrow forensics packet, or park it blocked-on-Rajiv with explicit
proof. Treat `PR_PM_GATE_REVIEW_REQUIRED` as current PR rescue work that blocks
fresh `status:todo` dispatch.

`PR_REWORK_PACKET_REQUIRED` is a hard current-PR prerequisite. PM must write the
exact current-head packet, deliver it with `Skill(direct-assign)` (rework packet), then
re-dispatch Ready Pool work with `Skill(direct-assign)`. Do not dispatch
fresh `status:todo` work while actionable existing-PR rework is missing its
durable packet. Labels and `/tmp` files alone are not packet persistence.

`pm-blocked:product` is a quiet slot-capacity state but not invisible. It should
surface in hourly ops as `PR_PRODUCT_DECISION_WAITING`, and PM must keep it out
of generic `assign-rework` until the exact Rajiv/product question is answered.
Closure proof is the question, PM recommended default, Rajiv/product response,
blocker removal, and a clean follow-up `pr-state-sweep` and the capacity actions via `Skill(direct-release)/Skill(direct-assign) with the complete authoritative tuple`.
If that closure proof is missing, hourly must keep the row as current PR work
ahead of fresh `status:todo` dispatch; a bare `pm-blocked:product` label is not
completion proof.

`pm-blocked:dependency` is quiet only while the named dependency is open and the
current head has machine-readable dependency proof. If `pr-state-sweep` emits
`PR_DEPENDENCY_WEDGE_REQUIRED`, PM must write current-head proof with
`gh pr edit <PR> --add-label pm-blocked:<reason> (advisory)`
which parks the PR in `pm-state:blocked-rework`, publishes a durable head-bound
`pm-dependency-watch:v1` PR comment, and releases its slot without triggering
CI. Do not apply `pm-state:qa-passed-awaiting-ci` while the dependency is open.
If the dependency proof cannot be made, remove `pm-blocked:dependency` and
resume capture/CI/rework. If the sweep emits
`PR_DEPENDENCY_UNBLOCKED_REQUIRED`, PM must run the exact emitted
`remove the pm-blocked:dependency label with gh pr edit (advisory)`

**Old PR base drift:** Hourly `pr-state-sweep` compares old PR heads directly
against `main`; do not rely on `mergeStateStatus=CLEAN`. By default it emits
`PR_MAIN_DRIFT_REQUIRED` when a PR is at least 6 hours old and lacks at least 5
commits from main. Dependency/product/capture/infra waits, merge-ready PRs, and
in-flight checks are excluded. PM must have the current slot merge `origin/main`
at the next phase boundary (or create a current-head merge-main packet and assign
it before fresh todo work), push, then obtain fresh review/QA/CI proof. Never
merge main into a branch while its checks are in flight.
command before claiming the PR is waiting. That transition removes the blocker
and intentionally applies `pm-state:qa-passed-awaiting-ci` to start current-head
label-gated CI.

`pm-state:rescope-required` is also not quiet. It means the old PR or issue hit
the rework/review circuit breaker and should not receive generic `assign-rework`
or another ordinary PM review round. PM must run `Skill(pm-codex-pr-rescue)` or
consume an existing rescue packet.
The rescue path may run:

```bash
CTO rescope decision (update the issue contract with gh issue edit)
CTO rescope decision (update the issue contract with gh issue edit)
```

Then execute exactly one terminal decision from the rescue packet and generated
`/tmp/pm-rescope-pr-<PR>-<head>.md` or `/tmp/pm-rescope-issue-<ISSUE>.md`:
`final_verified_patch`,
`split_and_reimplement`, `override_with_evidence`, `escalate_product_decision`,
issue rewrite, or Opus escalation.

Use the deterministic decision helper, not free-form Slack prose:

```bash
CTO rescope decision (update the issue contract with gh issue edit)
CTO rescope decision (update the issue contract with gh issue edit)
```

Decision matrix:
- Default to `split_and_reimplement` for broad multi-surface scope, migration or
  auth/data-model churn, repeated same-class blockers, fabrication/reset, or
  test-harness/proof churn across rounds.
- Use `final_verified_patch` only for one narrow blocker with a PM Claude patch
  packet and `git apply --check` proof.
- Use `override_with_evidence` only when PM has read the cited code/proof and
  product/runtime risk is cleared.
- Use `escalate_product_decision` only for a concrete unresolved
  product/data-model/authority question; include PM's recommended default. Do
  not escalate broad "split vs reassign" choices.

**Capacity reconciler gate:** When the action item involves stale/idle slots,
Ready Pool work, slot/MoP drift, or "all slots busy" claims, PM should first run:

```bash
PM Ready Pool dispatch/reconciliation via Skill(direct-assign)
```

Use the command output as the proof line. It drains provably non-slot-consuming
work, consumes pending ready packets, runs `pr-state-sweep`, and only then runs
the issue dispatch sweep. If it reports `pr_actionable>0`, `drift>0`,
`SLOT_DRAIN_REQUIRED`, or `PR_*_REQUIRED`, keep the audit action open with the
exact PR/slot blocker instead of claiming capacity is healthy.

The hourly audit background job also runs this reconciler before generating the
PM payload. If the payload or the pr-state-sweep watch log shows
`PR_SWEEP_BLOCKED_ISSUE_DISPATCH`, `PR_*_REQUIRED`, `SLOT_DRAIN_REQUIRED`,
`MOP_IDLE_OCCUPIED_SLOTS`, `CLAIMABLE_ISSUES count>0`, or
`QUEUED_APPEND_FAILED`, PM must close that exact capacity wedge with
PR transition / assignment / queue / blocker proof. Do not accept a passive
"all slots busy", "no status:todo", or "monitoring" response while those lines
remain.

**PR sweep sentinel gate:** If `/tmp/pm-required-pr-reconcile.json` is pending,
current PR work is still ahead of fresh issue dispatch. Run:

```bash
PM Ready Pool dispatch/reconciliation via Skill(direct-assign)
```

Then inspect `/tmp/pr-state-sweep.log` and execute the named transition:
`merge-ready` slot cleanup, `PR_READY_PROMOTION_REQUIRED` readiness/merge-ready
processing for green `qa-passed-awaiting-ci` PRs, `PR_PM_REVIEW_REQUIRED` /
`PR_PM_REVIEW_COMPLETE_REQUIRED` PM Claude transitions,
`PR_PM_REVIEW_CAPTURE_REQUIRED` / `PR_PM_REVIEW_CAPTURE_COMPLETE_REQUIRED`
capture-gated PM review transitions, stale `pm-blocked:ci` label reconcile +
label-gated CI trigger, `PR_CAPTURE_*` capture-watch/rerun transitions for
`pm-blocked:capture`, `PR_PM_GATE_REVIEW_REQUIRED` PM-gate proof/forensics,
`drain-slot`, or draft orphan close/backlog.
`Skill(direct-release)/Skill(direct-assign) with the complete authoritative tuple` assigns safe
`PR_REWORK_DISPATCH_REQUIRED` rows to free slots in live mode; if
`pr_rework_assigned>0`, verify the PR/issue `slot:N` labels and MoP handoff
before closing that row. Do not close the audit with `Skill(direct-assign)` proof
alone while this sentinel is pending.

**PM ops sync gate:** If the payload includes `PM_OPS_SYNC_REQUIRED` or
`pm-ops sync preflight: failed/missing/timeout`, run the bounded render path:

```bash
python3 ~/.claude/scripts/pm-ops.py sync --write --no-live --reason hourly-ops-repair
```

Then verify `pm-todo.md` reflects the open SQLite obligations before closing the
audit item. PM ops DB is the durable source; `pm-todo.md` is a rendered surface.

**Audit runner health gate:** If the payload includes
`codex_status=error|timeout` or an action targeting `hourly-ops-audit`, repair
the audit runner path before treating the hourly loop as healthy. A broken audit
runner is itself an ops miss because it can hide stale slots, queued work, and
drift rows.

**Hetzner / CI-runner-stall investigation gate:** Before probing ops-light, the
autoscaler, or any `hd-ci-eph-*` runner, or emitting any Hetzner / CI-runner
health verdict (including CI-runner-pool-stall alert triage), MUST invoke
`Skill(hetzner-ops-light-ssh-access)`. It encodes the exact
`-i ~/.ssh/hetzner-runner-warm -o IdentitiesOnly=yes` command, forbids treating a
bare-`ssh` `Permission denied` as an access-blocked signal, and distinguishes
coordinator probes (warm key works) from ephemeral-runner probes (reused-IP
host-key changes + publickey denial are EXPECTED). A bare-`ssh` denial or an
ephemeral-runner host-key change is NEVER an access-blocked or coordinator-outage
verdict.

If the payload includes `OPS_AUDIT_ZERO_BYTE` or the Kanban annotation says the
latest `/tmp/mop-ops-audit-*.stdout` is zero bytes, treat the audit loop as
failed even if the sentinel says complete. Inspect `/tmp/mop-ops-audit.log`,
repair or re-run the scheduler path, and keep the action open until a non-empty
stdout payload with parseable `INJECT_DECISION` proof exists.

**Alert closure gate:** If the payload targets `alert-thread-ts` or includes
`ALERT_CLOSURE_GAP thread_ts=...`, PM must verify the original Slack thread in
`#heydonna-alerts`. If the thread lacks a PM/bot reply with `status:`, `assignee:`,
and `next action:`, post that closure reply in the alert thread. Top-level posts
in `#heydonna-alerts` remain forbidden; thread-only closure replies are required.
If a valid closure reply already exists but the bridge cache missed it, cite the
Slack timestamp as proof. Do not close the action with only a #heydonna-dev
summary.

**P0 escalation call gate:** If the payload includes
`P0_ESCALATION_GAP thread_ts=...`, PM must treat it as urgent release-safety
work, not ordinary closure hygiene. This gate is severity-based: every P0
production incident requires the call path after the grace window unless the
originating alert thread has terminal recovery proof for every affected artifact
or Rajiv has replied in that alert thread that the call is not needed. Run the exact command named in the action item,
with the repo as cwd:

```bash
cd /Users/rajiv/Downloads/projects/heydonna-app && bash scripts/escalation-call.sh "P0: production incident unresolved; alert thread <thread_ts>"
```

Then reply in the original `#heydonna-alerts` thread with `status:`,
`assignee:`, `next action:`, and the call SID/status. Completion proof for the
hourly audit must use `ESCALATED` and cite the call output or Slack thread ts.
Do not mark this as `SKIPPED_WITH_REASON` because there is an investigation,
fix-identified note, recovery dispatch, or "overnight summary"; only terminal
recovery of every affected file or Rajiv's alert-thread reply can close it without
the call. If Rajiv replied, cite the alert-thread reply timestamp in the
resolution and clear the `p0_escalation` obligation through pm-ops.

If the payload includes `p0_escalation_gap_query_failed` or an action to repair
the P0 escalation detector, the hourly audit is failed-closed. PM must inspect
recent `#heydonna-alerts` stuck/auto-failed transcription threads manually,
repair the detector path, and either run the escalation call for any unresolved
candidate or cite terminal recovery/Rajiv alert-thread reply proof. A detector error is not
a clean audit.

**Current-PR campaign lock gate:** If the payload includes
`CAMPAIGN_LOCK_ACTIVE`, new issue dispatch is intentionally paused while current
PRs are forced to terminal PM state. PM must drive the listed PRs to
standalone `merge-ready`, blocked-on-Rajiv/product, or closed/cleanup, then run:

```bash
(retired: no current-PR campaign lock exists)
```

If Rajiv directs "finish current PRs first" or equivalent, create the lock
before running dispatch/reconcile:

```bash
(retired: no current-PR campaign lock exists)
```

While the lock is active, `Skill(direct-assign)` should emit `BLOCKED:
reason=current-pr-campaign-lock` for clean `status:todo` issues. That is valid
dispatch-block proof, but it is not completion proof for current PR progress.
The completion proof must name the PR transition or the unlock.

**Codex review / merge-readiness freshness gate:** For PR action items derived from a Codex review alert, merge-ready claim, stale blocker summary, or PR-state drift candidate, re-query live GitHub `reviewThreads` before demoting, dispatching rework, escalating, or clearing a blocker. If all Codex threads are `isResolved:true` and any P2 deferral has a linked follow-up `#NNNN` plus current-head green CI/capture evidence, the action is readiness/merge-ready verification, not rework. Unresolved P0/P1 remains a hard blocker. Unresolved P2 blocks unless PM can resolve it through a valid follow-up rationale before Phase B.

**PR-state reconciliation gate:** If an action item targets a PR and the
evidence mentions `blocked-rework`, `pm-blocked:*`, `qa-passed-awaiting-ci`,
`merge-ready`, `PR state drift candidate`, `LABEL_MOP_DRIFT`, `terminal_green`,
`latest-head CI`, `statusCheckRollup`, or `mergeStateStatus`, PM must perform a
live reconciliation before closing the item:

Before reconciling CI rows, run the independent five-minute failure watchdog:

```bash
python3 /Users/rajiv/Downloads/projects/heydonna-app/scripts/ci/ci-fast-triage.py \
  --watchdog --older-than-minutes 5 --lookback-hours 6
```

Every `missing_verdicts` row is an hourly action item. Launch
`Skill(ci-failure-investigation)` for that run before treating the board as
quiet. This query is authoritative even when the Slack alert or bridge entry is
missing.

```bash
gh pr view <PR> --json labels,isDraft,mergeStateStatus,mergeable,statusCheckRollup,headRefOid
gh api graphql -f query='{ repository(owner: "heydonna-app", name: "heydonna-app") { pullRequest(number: <PR>) { reviewThreads(first: 100) { nodes { isResolved } } } } }'
```

Then execute the matching terminal path:

- standalone `merge-ready` while any latest-head non-skipped check is queued,
  in-progress, failed, cancelled, timed out, or `mergeStateStatus` is not
  `CLEAN`: demote or move to CI watch immediately; proof must include
  a `pm-state:*` label write plus the run/job or merge-state blocker.
- `pm-state:qa-passed-awaiting-ci` with latest-head checks terminal green,
  `mergeStateStatus=CLEAN`, and no unresolved review threads:
  `pr-state-sweep` should emit `PR_READY_PROMOTION_REQUIRED`. Invoke
  project-local `pm-readiness-contract`; if it returns `READY_PACKET: PASS`,
  write `/tmp/pm-state-promotion-proof-<PR>-merge-ready.ok` with exact
  `headRefOid: <current head sha>`, run
  `Skill(heydonna-open-pr-status)` — it performs the current-head readiness
  check and merges head-pinned. If readiness blocks, keep or apply
  `blocked-rework`/`pm-blocked:pm-gate` and cite the missing proof.
- `pm-state:blocked-rework` with `pm-blocked:ci` but latest-head checks are
  terminal green: clear `pm-blocked:ci`, then run the same readiness path. Do
  not leave the PR blocked on a stale CI label.
- `pm-state:blocked-rework` with `pm-blocked:ci`, no queued/in-progress latest
  head checks, and terminal-red/failed checks: do not leave it in passive CI
  watch. If the failure has a named failed test/suite, require local preflight
  first (`bash scripts/e2e/local-repro-preflight.sh --spec <spec>`) and only
  rerun through
  gh run rerun <RUN_ID> --failed`.
  If the failure is wall-budget cancel, treat it as a PR-local stuck/slow
  regression by default: identify the budget-consuming spec/suite, dispatch
  local preflight or full-suite repro, and never raise CI/Playwright budget.
  If the PR changed proofread/SC/format prompt construction/content or
  LLM-proxy fixture/cache key inputs and the failure is in an LLM-backed path,
  classify `capture-required` first: trigger fresh head capture, then still
  require current-head local preflight PASS proof before rerunning
  the original CI/E2E. If a current-head capture
  workflow terminal-fails/cancels, or a stale-head capture already failed and
  the current head has no green capture, do not run another remote capture; first
	  PM runs the capture-local transition on the current PR head:
	  `the CI/capture lane (remote capture + Skill(heydonna-cto-label-gated-ci))`.
	  That transition labels the PR, assigns/nudges a slot, writes the exact
	  local proof packet, and requires `capture-local-pass` before label-gated CI.
	  If the failed run/comment reports a fixture key or hash prefix, the slot
	  local proof command must include `--fixture-key <key-or-hash>`. For fresh
	  capture-required PRs with no prior key, do not fabricate one; the wrapper
	  keyless-verifies newly written `fixtures/llm/v2/*.bin` objects from the
	  capture window. The proof is only acceptable when a required or just-written
	  key is verified in `heydonna-fixtures`.
  Capture proof is the local auto-process regression wrapper, not a smoke
  suite. The wrapper auto-selects depth: formatting/default-rules diffs run
  format capture, but formatting rules YAML is template-scoped, so default
  proof must cover both NY Standard and ACR template auto-process specs.
  Proofread / speaker-correction prompt diffs must run full auto-process
  (`E2E_CAPTURE_FORMAT_ONLY=false`) so SC writeback/raw-S-code assertions run
  before `CAPTURE_LOCAL: PASS`. Do not route capture-required proof through
  `smoke-legal-deposition`, `smoke-general-project`,
  `smoke-ai-proofreading`, hearing, template-vars, manual remote capture, or a
  package script that bypasses the wrapper unless Rajiv/CTO explicitly scopes a
  broader capture.
  Use
  the diff itself as the detector (capture-required.py deprecated 2026-10-03);
  docs/tests and deterministic post-processing changes are not enough. If the
  failure is a flake/pre-existing/no-local-equivalent case, file or cite a
  follow-up issue in the local proof before any retry. Otherwise dispatch rework
  with an owner and blocker proof.
- `pm-state:qa-passed-awaiting-ci` with `pm-blocked:capture`: this is not an
  uninvestigated CI failure. Read the latest CI verdict PR comment, verify the
  capture/local proof state in `watch_runs`, and follow `pr-state-sweep` output:
  in-flight/stale capture stays `pm-blocked:capture`; once current-head local
  capture proof is consumed, start label-gated CI directly and swap to
  `pm-blocked:ci` for the CI watch. Capture failed emits
  `PR_CAPTURE_LOCAL_REQUIRED` until local proof exists.
	  `PR_CAPTURE_LOCAL_REQUIRED` is slot-owned work: run
	  PM dispatches that local-proof rework with `Skill(direct-assign)` (rework
	  packet). The slot becomes releasable after `capture-local-pass`
	  consumes `CAPTURE_LOCAL: PASS`; remote capture is not required after local
	  capture proof.
	  If the PR carries a changed `tests/e2e/fixtures/preview-seed.zip`, keep this
	  in the control plane: the local capture wrapper auto-imports the branch seed
	  into local Convex before capture, so do not park it behind an app-code seed
	  preflight issue.
- `pm-state:pm-review-pending` with `pm-blocked:capture`: this is the canonical
  capture-before-CI parked state. It is not a CI watch and not a free-to-skip
  blocker. Follow the emitted `PR_CAPTURE_BEFORE_CI_WATCH_REQUIRED`,
  `PR_CAPTURE_COMPLETE_SLOT_READY_REQUIRED`, or
  `PR_CAPTURE_COMPLETE_PM_REVIEW_DONE_REQUIRED` action. Do not apply
  `qa-passed-awaiting-ci` until current-head capture is green.
- `pm-state:blocked-rework` with `pm-blocked:codex`: re-query review threads. If
  unresolved blocking threads remain, keep it blocked and cite
  `reviewThreads unresolved=<n>`. If none remain, run readiness. Do not leave
  a stale Codex blocker without live unresolved-thread proof.
- Any stale `slot:N` label on a PR not actively owned by MoP must be removed or
  explicitly preserved with live MoP proof.
- `LABEL_MOP_DRIFT` means GitHub labels and MoP slot mapping disagree. Reconcile
  live GitHub PR labels, current MoP slot row, and CI state only when the row
  changes the next transition for an active PR or frees/assigns a current slot.
  Standalone label hygiene belongs to the heartbeat drift batch, not the hourly
  action list.
- `PM_TODO_DRIFT` means the rendered PM working view or legacy Markdown contradicts live GitHub/MoP/PM ops obligations.
  Treat live GitHub/MoP plus deterministic drift rows as authoritative over
  rendered `pm-todo.md`, but do not emit PM_TODO_DRIFT as hourly work unless the
  stale row directly blocks an active PR transition or slot assignment. Ordinary
  PM-todo sync/prune/dedupe belongs to heartbeat.
- Stale `/tmp/slot-ready-events/*.json` packets are heartbeat hygiene unless the
  packet is for the active PR/slot transition being reconciled in this hourly
  item. If the packet is active, consume it by admitting the exact head with
  `Skill(heydonna-cto-label-gated-ci)`; otherwise record the label/blocker change
  with `gh pr edit` so the packet is treated as obsolete. Do not list old packets as
  one hourly action per file.
- `SLOT_PROMISE_MISSING` means a dev slot told PM it would run a same-turn
  action such as Codex review, QA, PR creation, or tests, but MoP `/events`
  shows no matching same-slot Agent/Bash/PR tool call afterward. Re-instruct the slot
  to run the promised foreground tool, or send a corrected PM status that
  retracts the promise and cites the real blocker/state proof. Another passive
  PM status note is not valid closure.
- A deterministic `PR_DRIFT target=... carryover=true` row means the same
  target/reason survived a prior hourly audit. Treat it as a wedge: completion
  requires a state mutation, rerun id, dispatch proof, or explicit blocker proof.
  "Monitoring" is not valid for carryover drift.
- A deterministic `LABEL_MOP_DRIFT target=... carryover=true` row is also a
  wedge. Completion requires a GitHub label/state mutation, MoP slot correction,
  dispatch proof, or explicit live proof that the label is intentionally
  preserved.

Valid completion proof for PR-state reconciliation must include at least one
state mutation (`gh pr edit --add-label/--remove-label` for `pm-state:*`, or
`label:pm-state:*`) OR an explicit blocking proof (`READY_PACKET: BLOCKED`,
`reviewThreads unresolved=<n>`, `statusCheckRollup`, `ci-run:<id>`,
`mergeStateStatus=<state>`, or `mop-slot:<N>` live ownership proof). "Checked",
"reconciled", "monitoring", or a corrected Kanban post alone is not accepted.

**Semantic close gate:** proof lines are necessary but not sufficient. The Stop
validator re-reads the audit payload and re-checks targeted PRs before marking
the sentinel complete. If a targeted PR still has the same stale state after the
PM reply, the audit re-pends with `semantic_pr_state_drift`. Examples that keep
the audit open: merged PR without `pm-state:merged-cleanup-pending` or
`pm-state:closed-clean`; `pm-state:blocked-rework + pm-blocked:ci` after
latest-head terminal-green CI; `pm-state:blocked-rework + pm-blocked:codex` after
terminal-green CI and `reviewThreads unresolved=0`; `pm-state:qa-passed-awaiting-ci`
after terminal-green CI and `mergeStateStatus=CLEAN`; standalone `merge-ready`
while checks or mergeability are not clean; stale `slot:N` labels on an action
that explicitly asked to clear/reconcile stale slot ownership.

**Routing/audit drift hard gate (Rajiv directive 2026-05-27):** If `REASON:` is
`unassigned_status_todo`, `slot_state_drift`, `stale_kanban`, or
`unassigned_customer_followups`, first run:

```bash
PM Ready Pool dispatch/reconciliation via Skill(direct-assign)
```

Then use the reconciler/sweep output as proof for any status:todo/free-slot
item. A completion that only says "checked", "noted", "corrected Kanban", or
"no action" is invalid.
unless it includes `ASSIGNED`, `QUEUED`, `BLOCKED`, `DISPATCH_OK`, or a concrete
no-eligible-slot proof from the sweep. Any free slot must dispatch or block P0,
customer P1, rework, CI-red, and QA-failed items; standby-by-role is not a valid
proof for those classes.

If `/tmp/pm-required-slot-dispatch.json` is pending, treat it as a routing/audit
drift item even if the current payload reason is different. Run the real
user-scope sweep:

```bash
bash /Users/rajiv/.claude/skills/Skill(direct-assign)/scripts/sweep.sh --trigger=pm-required-dispatch
```

Completion proof must include the sweep's `ASSIGNED`, `QUEUED`, `BLOCKED`, or
`DISPATCH_OK` output and the sentinel status becoming `resolved`. A dry-run
`ASSIGNMENT_SKIPPED` line is not closure.

**Ready Pool reprioritization gate:** If `REASON:` is `stale_kanban`,
`unassigned_status_todo`, `ready_pool_priority_drift`, `issue_burst`, or the action
text asks to reprioritize/reorder the Ready Pool, invoke `todo-prioritize` in
hourly-ops-audit mode before closing the item when any of these are true:

- PM ops Ready Pool / rendered `pm-todo.md` top queue no longer matches open `status:todo` priority/risk order;
- new P0/P1 or customer/legal-output issue is absent from PM ops / rendered `pm-todo.md`;
- a blocked issue became unblocked and is still parked below lower-priority work;
- issue count increased materially and the current queue was not regenerated;
- Ready Pool rows are missing priority/lane/next-action proof.

`todo-prioritize` may update PM ops queue obligations, render `pm-todo.md`, and then run
`Skill(direct-assign)`. It must not change GitHub priority labels or Ready Pool
frontmatter priority values; those require `pm-triage`. Completion proof must name
changed issue numbers or `no_order_change`, cite `pm-todo.md`, and include the
post-write sweep result. If `todo-prioritize` detects a required label/frontmatter
priority change, complete the action as `BLOCKED proof: needs pm-triage`.

**Control-plane freshness hard gate:** If the audit evidence names stale Slack
bridge input, `latest_message_age` above 60 minutes, `bridge_db_stale`,
`bridge_db_file_stale`, MoP slot-state drift, `ISSUE_MOP_DRIFT`, hidden Pending
Rajiv, or degraded Kanban, do not close the item as "checked" or "no action."
The completion proof must show one of:

- live 5-source reconciliation with GitHub PR labels/body, Codex threads, latest
  CI, live MoP, PM ops obligations, and rendered `pm-todo.md`;
- explicit `CONTROL_PLANE_DEGRADED` / `SLACK_INPUT_DEGRADED` / `STATE_MISMATCH`
  row kept open;
- a concrete repair action with proof, such as bridge restart, kanban refresh,
  Skill(direct-assign) output, demotion, dispatch, or queued blocker.

Banned conclusions from stale bridge/MoP/Kanban input: "Pending Rajiv none",
"all slots free", "ready pool empty", "no Slack exceptions", "no open actions",
or "all clear."

**Drift evidence scoping:** `LABEL_MOP_DRIFT`, `ISSUE_MOP_DRIFT`, and
`PM_TODO_DRIFT` are diagnostic evidence in hourly. They become hourly action
items only when they directly block one of the two hourly jobs: move an active PR
toward standalone `merge-ready` / a live blocker, or assign/release a slot for
current work. Otherwise route them to heartbeat as one drift cleanup batch.

**Repeat-offender hard gate:** If this audit reason or target appeared in the
previous hourly audit and is still present, treat it as a control-plane failure,
not a fresh nudge. The completion proof must show a terminal transition:
`ASSIGNED`, `QUEUED`, `BLOCKED`, `DISPATCH_OK`, `demoted`, `rerun` with run id,
`closed`, or `escalated` with Slack ts. Repeating "nudged", "posted status",
"noted", "will monitor", or "waiting" is invalid unless paired with one of those
proofs and a concrete blocker.

Before accepting a repeated `stale_kanban` / `unassigned_status_todo` finding as
complete, inspect the latest tails of:

```bash
tail -80 /tmp/Skill(direct-assign).log
tail -80 /tmp/hourly-ops-Skill(direct-assign).log
tail -80 /tmp/mop-server.log
```

If those logs show `table_header_not_found`, `DISPATCH_FAILED`, `SWEEP_BLOCKED`,
`SWEEP_DEGRADED`, `/slots` timeout, or `event-loop-lag`, the action item is a
PM-direct infra repair or reroute decision; do not close it as a normal nudge.

For stale `/tmp` handoff/escalation files (`/tmp/slot-delivery-*`,
`/tmp/pm-escalation-pending-slot-*`, `/tmp/escalation-slot-*`), completion is
not "inspected" unless PM either executes the required action or proves it was
already superseded, then archives the file or creates `<file>.resolved`. Hourly
audit ignores `*.resolved`, `*.processed`, `*-processed-*`, and `/tmp/resolved/*`;
leave no acted-on live file behind.

The hourly detector launches one PM background `pm-backlog-promoter` when
READY_POOL_LOW or CONTRACT_REPAIR_REQUIRED fires, or when it finds eligible
P0-P2, a resolved prior park, or untriaged P0/P1 intake, REGARDLESS of pool size.
Use `mode=refill-ready-pool` or `mode=repair-ready-pool issues=#...`; each mode
performs individual triage, contract repair, and promotion in the same run.
Read `.claude/skills/pm-triage/skill.md` for the complete current contract.

Ten claimable issues is a minimum, not a ceiling. Handle ALL eligible P0-P2;
then runnable P3s may refill the deficit below ten. No promoter Codex architecture
review or nested-auditor prerequisite remains. Use the deterministic project
`ready-pool-contract-repair.py --source-manifest <manifest> --apply` for missing
contracts and continue to verified promotion in that invocation. Never invent ACs.
Record exact unresolved questions, owner and wake, and batch genuine decisions.

S113: dependency-held app issues of every priority remain in the Ready Pool with
canonical refs and dependency labels. `TODO_DEPENDENCY_GATED` is a wait count,
not a demotion instruction or contract defect. The live selector and `--check`
block premature assignment. Repair missing/contradictory metadata separately;
never migrate a valid Todo to backlog merely for a live dependency. Report total
membership separately from claimable work. PM uses canonical `direct-assign`.

Run one writer for refill/repair, sync PM ops and generated pm-todo, and report
verified promotions/holds/closures plus source/apply manifests and label effects.
A launch, dry run or REPAIRED body alone is not completion. Operational-only
work goes to PM obligations, not application slots.

1. **TRANSITION: traced + TARGET: misc** → investigation. Read the EVIDENCE, decide follow-through, execute (Slack reply, GitHub label, slot dispatch, alert close). For PR/Codex traces, include live `reviewThreads` state in the proof line if the action changes readiness or rework state.
2. **TRANSITION: assigned** → PM owns the assignment. Re-verify slot availability via `mop_all_slots`, then hand off the concrete packet to the chosen slot and verify receipt via MoP event proof: delivery event plus subsequent same-slot pickup/response event. If not visible within 30s, use the `message-slot` fallback pattern and keep the action blocked until receipt proof exists.
3. **TRANSITION: queued** → verify the item already appears as a PM ops `ready_pool` obligation and in rendered `pm-todo.md` with Ready Pool frontmatter (CP #17). If absent, queue it now through `pm-ops.py obligation-upsert` and `pm-ops.py sync --write --no-live`.
4. **TRANSITION: demoted** → first apply the Codex review / merge-readiness freshness gate above. If a blocker remains live, flip standalone `merge-ready` → `pm-state:blocked-rework` per `pr-mark-ready` Phase B-revoke template and cite the live audit evidence on the PR. Do not demote on stale Codex alerts whose threads are already resolved with valid P2 follow-up rationale.
5. **TRANSITION: rerun** → ALWAYS combine with `ci-failure-investigation` Step 7
   verdict BEFORE rerunning. Naked rerun is a process violation (CP #14). Every
   CI/E2E failure class requires current-head local-repro
   PASS proof before rerun; flake/pre-existing/no-local-equivalent retries
   additionally require a cited follow-up issue in that proof.
6. **TRANSITION: closed** → `gh issue close --reason completed` OR `gh pr close` as appropriate, then run `cleanup-pr` if a PR is involved.
7. **TRANSITION: escalated** → PM escalates to Rajiv via `slack-send.sh` (NOT slack_send_message which impersonates). Include the audit EVIDENCE verbatim.
8. **TRANSITION: blocked** → confirm blocker exists in code/CI/Slack. If blocker is gone, the audit is stale — run the PR-state reconciliation gate instead of skipping.
9. **TRANSITION: reprioritized** → invoke `todo-prioritize` in hourly-ops-audit mode. Update PM ops obligations only for queue projection changes, render `pm-todo.md`, then run `Skill(direct-assign) --trigger=todo-prioritize-hourly-ops`. Proof tag should be `EXECUTED` when the queue was updated or confirmed current, `BLOCKED` when `pm-triage` is required, or `QUEUED` when a missing Ready Pool row was added but dispatch waits on occupied slots.
10. **TRANSITION: promoted** → reserved for PM-owned PR-state drift repair. Run
    the PR-state reconciliation gate, then promote only through
    project-local `pm-readiness-contract` and the PM state helper. If any gate
    fails, complete as `BLOCKED proof:` with the live blocker instead of
    promoting.

Example parse:

```
- ACTION: demote PR #4942 from merge-ready (latest-head CI failed)
  EVIDENCE: gh pr view 4942 statusCheckRollup FAILURE on commit abc1234
  TRANSITION: demoted
  TARGET: PR#4942
```
→ `owner=PM, action=demote, transition=demoted, target=PR#4942` → flip label, cite EVIDENCE on PR.

```
- ACTION: dispatch rework on PR #5012 R2 (Codex P0 unresolved)
  EVIDENCE: /tmp/codex-app-code-review-5012.txt VERDICT:REVISE
  TRANSITION: dispatched
  TARGET: PR#5012
```
→ `owner=PM, action=dispatch, transition=dispatched, target=PR#5012` → PM re-verifies slot availability, handoffs to the selected slot, and cites MoP receipt proof.

### Step 3 — PM authority check (CP #8, CP #8c, CP #17)

Action items involving slot routing, queue-vs-dispatch, rework assignment, rerun-vs-wait, session clear, respawn/restart, Kanban correction, label cleanup, alert tracing, follow-up deferral, or follow-up filing are mechanical PM actions only when an existing Rajiv-approved rule determines one outcome. Any choice, change, waiver, override, force, or exception is a process decision: PM sends CTO one evidence-bound recommendation and stops before mutation; CTO DMs Rajiv. Session-age and stale-process cleanup remain recurring hygiene only where the approved rule is unambiguous. If hourly inputs contain `CLEAR_PENDING_PM_TODO_OPEN`, `CLEAR_PENDING_PM_TODO_MISSING`, or `CLEAR_PENDING_PM_TODO_STALE`, PM may apply only the single action mandated by that rule; otherwise escalate the process choice.

If an action item explicitly says `invoke Skill(session-age-clear)`, run that
skill before emitting the `Ops-audit @ ...` completion block. The skill must not
queue automatic clears for active dev slots; those become
`SESSION_AGE_CLEAR_PENDING` PM-todo obligations until PM reaches a natural
boundary. Do not call
`mop_clear_slot(slot: "all")` inside hourly-ops-audit; the clear skill owns the
ordering so PM is cleared last.

For stale agent-browser/Next.js cleanup, hourly should normally cite the
heartbeat surface instead of executing cleanup inline. If an action item says
`invoke Skill(stale-process-cleanup)` because a current active slot is blocked
by the stale process, run that skill before emitting the completion block. Do
not use broad `pkill`, `killall`, or raw command-substring kills from
hourly-ops-audit; the cleanup skill owns the PID allowlist, TERM/KILL sequence,
skip policy, and before/after proof.

Action items that touch PRODUCT or PROCESS decisions → escalate to CTO with
evidence; CTO DMs Rajiv and waits for explicit approval. Product decisions
include customer-visible behavior, data model, roadmap, merge approval, or
irreversible action. Process decisions include slot, Ready Pool, PR conveyor,
review, CI/capture/rerun, rollout, monitor, or automation changes. PM never
escalates directly to Rajiv. Include the audit evidence in the CTO handoff.

Action items that expose CONTROL-PLANE INTEGRITY failures also escalate to
CTO when they block PR movement, hide real state, or require any behavior or
policy change. CTO DMs Rajiv and waits for approval before that change.
Control-plane integrity means the
workflow machinery is lying or wedged: MoP/GitHub ownership drift that changes
routing, dispatcher/pr-state-sweep syntax or runtime failures, stop-hook loops,
Slack/thread routing failures, capture/CI gate false-green or false-block,
autoscaler/watcher missed transitions, PM ops/todo ledger drift that prevents
dispatch, or a helper accepting/rejecting proof contrary to the documented state
machine. PM may execute routine cleanup only when the approved rule has one
outcome; every choice or change goes to CTO and then Rajiv.

Control-plane escalation proof must include PR/issue/slot, expected transition,
actual stuck state, evidence path or command output, GitHub head/labels, MoP
state, PM attempted fix or recommended default, and impact on merge-ready or
slot dispatch.

### Step 4 — Update PM Ops and Render pm-todo.md

Every dispatched/queued/closed action item per CP #11 mandatory write-points. Record/update the PM ops event or obligation with `--horizon hourly` for active slot/PR work, then run `python3 /Users/rajiv/.claude/scripts/pm-ops.py sync --write --no-live --reason hourly-ops-audit`.

For any `PM_TODO_DRIFT` action, update/resolve the stale PM ops obligation or regenerate the rendered row before replying. Use `pm-ops-obligation:<id>` plus `pm-ops-sync:<id>` proof, or `pm-todo-row:<line>` proof for the refreshed rendered row, and cite the live GitHub/MoP proof that replaced it.

### Step 5 — Reply to invocation (proof-bar — Rajiv directive 2026-05-27 11:48 IST thread `1779862714.741229`)

Reply in the PM pane with the canonical 2-block shape: header line + per-item proof block. **Marker-only completion (`Ops-audit @ <ts>` summary alone) is REJECTED by the `ops-audit-stop-validator` Stop hook and the audit re-pends.**

The proof block is also the row-ledger close command. `ACTION_1` closes the
first original `- ACTION:` row, `ACTION_2` closes the second, and so on. Do not
renumber, combine rows, or omit rows because they are "known waits"; use
`BLOCKED proof:` / `SKIPPED_WITH_REASON proof:` with concrete evidence instead.

```
Ops-audit @ <timestamp> · <decision_reason> · N item(s)

[ACTION_1_TAG] EXECUTED proof: <one-line trace — label state | slot handoff proof | Slack ts | CI run id | pm-todo.md row>
[ACTION_2_TAG] QUEUED proof: <one-line — pm-todo.md row ref OR Ready Pool frontmatter>
[ACTION_3_TAG] DISPATCHED proof: <one-line — MoP delivery event id/ts + subsequent same-slot pickup/response event id/ts OR `/tmp/slot-delivery-*.md` path with matching MoP pickup proof; send-only/MESSAGE_SLOT_OK-only is invalid>
[ACTION_4_TAG] ESCALATED proof: <one-line — Slack DM ts to Rajiv + thread channel>
[ACTION_5_TAG] BLOCKED proof: <one-line — blocker reference (CI run id / PR # / external dep) + ETA>
[ACTION_6_TAG] SKIPPED_WITH_REASON proof: <one-line — reason + cited memo/rule>
```

Do NOT echo the full payload back. Do NOT post to Slack unless the audit explicitly says to.

## Completion proof schema (REQUIRED — Rajiv directive 2026-05-27 11:48 IST)

The Stop hook `ops-audit-stop-validator.sh` reads the assistant transcript and enforces these shape constraints. If validation fails, the audit completion sentinel is removed and a `[OPS_AUDIT_INCOMPLETE]` reminder is posted requesting a re-pending of the audit.

**Hard requirements:**

1. **Header line** must match `Ops-audit @ <ISO-timestamp> · <reason> · <N> item(s)` where N is a positive integer equal to the action-item count from the payload.
2. **One proof block per action item.** Number of `[ACTION_N_TAG]` lines (or per-item proof lines as defined below) must equal N.
3. **Each proof line** must start with one of the six recognized transition tags in caps: `EXECUTED`, `QUEUED`, `DISPATCHED`, `ESCALATED`, `BLOCKED`, `SKIPPED_WITH_REASON`. Free-form transitions are rejected.
4. **Each proof line** must include `proof:` followed by a non-empty, non-placeholder substring (≥ 8 chars; reject `proof: <one-line>`, `proof: TODO`, `proof: tbd`, `proof: pending`).
5. **Proof content** SHOULD cite one of the canonical proof types:
   - `label:pm-state:*` (PR/issue label state — verify via `gh pr view --json labels`)
   - `slot-delivery:/tmp/slot-delivery-*.md` plus MoP pickup event id/ts OR `mop_send_to_slot event id=<> ts=<>` plus subsequent same-slot pickup/response event id/ts from `/events`
   - `slack-ts:<10.6>` (Slack post timestamp)
   - `ci-run:<run-id>` (CI run id)
   - `pm-ops-obligation:<id>` plus `pm-ops-sync:<id>` (PM ops ledger/render proof)
   - `pm-todo-row:<line>` (rendered pm-todo.md row anchor)
   - `session-age-clear:/tmp/session-age-clear-latest.json` plus
     `slot_cleared`, `clear_pending_executed`,
     `clear_pending_stale_repaired`, or `clear_pending_failed`;
     `clear_pending_queued` is only QUEUED/PENDING proof and must be paired with
     `pm-ops-obligation:<id>` / `pm-todo-row:<line>` until execution proof lands
   - `stale-process-cleanup:/tmp/stale-process-cleanup-latest.json` plus
     before/killed/after counts or explicit skip proof
   - `alert-thread-ts:<ts>` plus `slack-ts:<reply-ts>` for alert closure
   - `campaign-lock:/tmp/pm-current-pr-campaign-lock.json` plus
     the campaign-lock file path (the campaign-lock helper is retired; no
     helper output exists)
   - `gh-issue:#NNNN` OR `gh-pr:#NNNN` (linked GitHub object)
   - `PM_TODO_DRIFT` reconciliation: `pm-todo-row:<line>` plus `gh-pr:#NNNN` /
     `gh-issue:#NNNN`, `mop-slot:<N>`, or a label/state mutation
   - PR-state reconciliation evidence: a `pm-state:*` label write, `READY_PACKET:
     PASS|BLOCKED`, `reviewThreads unresolved=<n>`, `statusCheckRollup`,
     `mergeStateStatus=<state>`, or `headRefOid=<sha>`
6. **No marker-only summaries.** `Ops-audit @ <ts>` followed only by paragraph prose ("- N items processed", "- Evidence:", "- Next:") is the legacy shape and is REJECTED.
7. **PR-state drift actions must semantically clear.** If the original action
   targeted a PR-state drift, stale blocker, stale slot label, readiness
   promotion, CI state, mergeability state, or post-merge cleanup, the hook
   re-checks the targeted PR before accepting completion. A proof line that
   mentions `gh pr view`, `statusCheckRollup`, or `label:pm-state:*` is rejected
   when the live PR still shows the same stale state.

**Validator behavior:**

- If transcript contains the new shape's header + ≥ N proof lines matching the regex `^\[ACTION_[0-9]+(_[A-Z_]+)?\] (EXECUTED|QUEUED|DISPATCHED|ESCALATED|BLOCKED|SKIPPED_WITH_REASON) proof: \S.{7,}$` → ACCEPT.
- If transcript contains the legacy `Ops-audit @ ` header but no proof-lines → REJECT, remove sentinel, post `[OPS_AUDIT_INCOMPLETE]`.
- If transcript lacks `Ops-audit @ ` entirely → no-op (the audit didn't claim completion; nothing to validate).

**Skill writer's checklist before posting Step 5:**

- [ ] Header line matches canonical shape with integer N.
- [ ] N proof lines emitted, one per action item, with transition tag + concrete proof.
- [ ] Each proof references a verifiable artifact (file path, label state, Slack ts, CI run id, pm-todo row).
- [ ] No `proof: <one-line>` placeholders remain.
- [ ] For every PR-state item, the live PR no longer matches the stale state
      named in the audit payload, or the proof cites the current blocker
      (`READY_PACKET: BLOCKED`, unresolved Codex count, non-green run, conflict,
      or cleanup-pr in progress).

## Failure modes the skill must surface

- **Codex timeout** (`-- META: codex_status=timeout`) → degraded local-signal payload. Trust action items but verify each against fresh state (CP #9 — gate the action).
- **Codex malformed output** (`INJECT_DECISION:inject REASON:codex_malformed_output`) → read the `-- ORIGINAL_CODEX_OUTPUT:` trailer; if Codex is drifting on prompt contract repeatedly, file a PM-direct fix-forward issue on the bg-script.
- **bridge_down** (`REASON:codex_*_degraded_local_signals` + slack-bridge item) → fire the slack-bridge PM-direct repair per `feedback_pm_direct_for_slack_bridge_infra_tooling_no_issues_or_prs` (clear log, restart daemon).
- **mop_db_locked** → restart MoP via `Skill(mop-restart)` then re-tick via `mop_ops_audit_now`.

## Configuration / controls

- Manual force run: `mop_ops_audit_now(reason: "manual")` (bypasses pause).
- Status: `mop_ops_audit_status()` — paused flag, last-run summary.
- Pause: `mop_ops_audit_pause(paused: true)` — persists in MoP SQLite config.
- Trace log: `/tmp/mop-ops-audit.log` (bg-script side) and MoP server stdout (scheduler side).
- bg-script: `.claude/scripts/hourly-ops-review-bg.sh`
- Scheduler: `src/opsAudit.ts` in MoP (`OpsAuditScheduler.start()` from `src/server.ts`).

## Companion infrastructure

- Hook chain: `pm-context-injector.sh` writes `/tmp/pm-required-skill-hourly-ops-audit.json`; `ops-audit-pretooluse-guard.sh` blocks ad-hoc tools until Skill invocation; `ops-audit-skill-posttool.sh` flips `pending` to `skill_invoked`; `ops-audit-stop-validator.sh` accepts only the proof schema and writes `complete`. The substantive hooks for this checkout live under project `.claude/hooks`.
- Codex automation `/Users/rajiv/.codex/automations/heydonna-dev-hourly-activity-review/automation.toml` is PAUSED (replaced by MoP-side audit). Status field set to `"PAUSED"`.

## References

- Rajiv CTO directive 2026-05-26 thread `C0ALZJHGE49/1779790681.847219` (original spec).
- `feedback_pm_infra_patch_acceptance_bar_real_stdout_not_skill_text_quotes` (proof-bar for any future audit-related fix-forward).
- `feedback_pm_direct_for_slack_bridge_infra_tooling_no_issues_or_prs` (slack-bridge repair path).
- CP #8 / #8b / #8c / #17 / #11 (PM authority, infra-direct, ops autonomy, intake transactional terminal, todo write-points).
