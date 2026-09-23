---
name: heartbeat-tasks
description: |
  Sakshi scheduled maintenance triggered by MoP every 3 hours. PM launches a
  background agent. The agent runs the deterministic script for hard-gate
  evidence, reports action items through canonical PM paths, collates results,
  and posts the final Slack report.
  Use when: MoP asks PM to invoke `Skill(heartbeat-tasks)` or the heartbeat
  is requested manually.
  NOT for: Morning briefs (use morning-brief), one-off status checks (use pm-status),
  manual slot monitoring (use check-slot or monitor-slot).
author: Claude Code PM
version: 2.2.1
date: 2026-08-29
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

# PM Scheduled Tasks (Sakshi Heartbeat)

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

## Runtime Contract (Rajiv directive 2026-06-12; MoP-centralized 2026-06-20)

Scheduled heartbeat runtime is:

1. MoP `PMCadenceScheduler` owns the cadence and persists the last 3h bucket in
   MoP SQLite config (`pm_cadence_heartbeat_*`).
2. MoP queues a normal PM prompt asking PM to invoke `Skill(heartbeat-tasks)`.
   It must not inject `/heartbeat-tasks` as a slash command.
3. PM launches the heartbeat background agent with `run_in_background=true`
   using the generated canonical prompt:

```bash
bash /Users/rajiv/.claude/scripts/sakshi-heartbeat.sh --launch-prompt
```

   Use that output verbatim as the agent prompt. Never hand-copy an abbreviated
   step list; the generated prompt is the single canonical source.
4. The background agent runs the deterministic probe without Slack posting:

```bash
bash /Users/rajiv/.claude/scripts/sakshi-heartbeat.sh
```

5. The agent runs deterministic session-age reconciliation:

```bash
python3 /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/heartbeat-session-age-clear.py
```

6. The agent reads `/tmp/sakshi-heartbeat.json` and
   `/tmp/session-age-clear-latest.json`, classifies or blocks remaining action
   items through PM-owned paths, then posts the collated Slack report.

7. The agent runs heartbeat-scoped recent-intake and dependency reconciliation:

```bash
/Users/rajiv/.claude/scripts/backlog-triage.py --surface heartbeat --recent-hours 72
```

This surface is not a Ready Pool refill or a general backlog promotion pass.
It may classify issues created in the intake window, repair dependency waits,
and migrate dependency-blocked Todos. Ordinary backlog promotion remains
daily/manual PM planning.

8. Independently of the 72-hour intake window, the agent audits and repairs the
   complete live Ready Pool:

```bash
/Users/rajiv/.claude/scripts/backlog-triage.py audit-ready-pool
```

This command scans up to 1000 open issues and validates every live
`status:todo` row. It must not use a capped `gh issue list --limit 30` query.
The audit checks complete Ready Pool frontmatter, all six canonical fields
(`priority`, `lane`, `ac_summary`, `claimable_slot_type`, `blockers`,
`work_type`), canonical P0-P3 label parity, live dependencies, blocker labels,
and claimability. Per-issue handoff preflight remains owned by the subsequent
`Skill(direct-assign)`, so the full-pool audit stays bounded. Cite the emitted
`/tmp/pm-ready-pool-audit-*.json` manifest in the action ledger.

If it emits `READY_POOL_CONTRACT_REPAIR_REQUIRED`, the heartbeat background
agent repairs the exact flagged issues itself in the same run. Pin the issue
set once, read each complete live body/comments/labels/dependencies, and assign
one terminal repair:

- bounded dependency-free app-code/app-test work: repair the ICL, canonical
  priority, Ready Pool frontmatter, branch slug, validation, and status/labels
  in place;
- live dependency: move to `status:backlog + pm-blocked:dependency` with
  canonical blockers, owner, next action, and wake condition;
- nonclaimable tracking/investigation/repro/external/infra/direct-main work:
  move out of Todo with its canonical structured disposition.

Do not delegate this repair to a backlog agent and do not refill the Ready Pool
from ordinary backlog. The repair closes only when every flagged Todo either
passes the complete dispatch contract or has a valid typed non-Todo
disposition. Then rerun `audit-ready-pool`, sync PM ops, and run
`Skill(direct-assign)`.

If the dry run reports `TODO_BLOCKED_DEPENDENCY`, run the safe apply path before
posting so dependency-blocked issues no longer masquerade as runnable Ready Pool
work:

```bash
/Users/rajiv/.claude/scripts/backlog-triage.py --surface heartbeat --recent-hours 72 \
  --apply --migrate-blocked-todos
```

Heartbeat must not auto-promote arbitrary backlog rows to `status:todo`, launch
a backlog refill/promotion agent, or treat a low pool count as authority to
scan more backlog. It should report ordinary promotion candidates and invalid
parking metadata as PM/daily planning work with the
`/tmp/pm-backlog-triage-*.json` manifest path.

Heartbeat scope is creation-time based, not `updatedAt` based. It audits:

- every live non-backlog issue needed for todo/status invariants;
- backlog issues created within the last 72 hours; and
- dependency-wait backlog issues, regardless of age, so resolved dependencies
  can become claimable.

It must not pull unchanged historical backlog into the 3h report merely because
label/body reconciliation refreshed `updatedAt`. Full historical backlog
planning remains daily/manual via `backlog-triage.py` without `--surface`.

The legacy launchd script `/Users/rajiv/.claude/scripts/scheduled-heartbeat.sh`
is a compatibility shim only. It must not inject directly when MoP is healthy;
`mop-healthcheck` is the launchd-owned watchdog/restart path.

Operator controls:

```bash
curl -fsS http://127.0.0.1:3100/pm-cadence/status
curl -fsS -X POST http://127.0.0.1:3100/pm-cadence/run \
  -H 'content-type: application/json' \
  -d '{"task":"heartbeat"}'
```

The probe writes:

- `/tmp/sakshi-heartbeat.txt`
- `/tmp/sakshi-heartbeat.json`

Never use `sakshi-heartbeat.sh --send-slack` from the scheduled path. Slack
posting is owned by the background agent after action handling.

Hard-gate rule: PM/agent MUST NOT claim `no session-age flags`, `Actions needed:
none`, or `Queue-motion gate: CLEAR` unless the JSON artifact contains a fresh
proof table for all seven sessions: `PM`, `S1`, `S2`, `S3`, `S4`, `S5`, and `S6`.

Session age is measured from the latest runtime session's effective start.
PM and S1-S6 use the runtime-observation adapter, which selects the live Claude
Code project session for each current checkout and uses `SessionStart:clear`
when present, otherwise the session's first timestamp; `SessionStart:compact`
is context pressure, not a clear. Nested reviewer and subagent JSONLs are
excluded. Take the later of that runtime session start and the latest successful
MoP `slot_cleared` or `clear_pending_executed` event. Queued, skipped, failed,
or latched clear requests never reset age. If no current runtime source exists,
report unknown and fail closed; never substitute a stale session from another
runtime or checkout.

The deterministic heartbeat probe and heartbeat background agent own recurring
session-age detection and PM-todo tracking. They do NOT own dev-slot clearing: a
numbered slot's session is cleared only at the new-issue assignment boundary,
inside the atomic assignment operation (`Skill(direct-assign)` /
`mop-assign-slot`). If the probe reports session-age flags or `clear_due`, the
heartbeat agent MAY still run `.claude/scripts/heartbeat-session-age-clear.py`
before Slack posting, because that script is materialize-only for numbered slots
and never clears a session. Do not punt routine session-age tracking back to
hourly ops.

After reconciliation, the heartbeat must explicitly remind PM about the PM
session only. If PM is pending, remind PM to satisfy the existing
`pm-self-clear` obligation at the next safe Stop boundary after housekeeping.
This is a reminder layer only: do not interrupt active work, do not auto-clear
PM, and do not create duplicate obligations.

Session-age clear policy: dev slots S1-S6 are NEVER cleared on a cadence. A
numbered slot's session is cleared only at the new-issue assignment boundary,
which owns that step inside the atomic assignment operation
(`Skill(direct-assign)` / `mop-assign-slot`). The heartbeat must not produce,
consume, remind on, or act on `SESSION_AGE_CLEAR_PENDING slot:N`, and must not
invoke `.claude/scripts/mop-clear-slot.sh` for any dev slot.

PM remains clear-worthy on cadence:
- due PM → heartbeat creates/updates `SESSION_AGE_CLEAR_PENDING PM`; the
  `pm-self-clear-stop` hook only emits a non-blocking Stop reminder. PM must
  finish housekeeping, save any last note to pm-todo/pm-ops, and then clear
  itself through MoP logging with:
  `bash /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/mop-clear-slot.sh pm`.
  The hook resolves the row only with terminal MoP proof after that clear. The
  reminder must not use `decision:block`, because that can trap the clear
  request behind the Stop hook. If MoP already logged the PM clear request, the
  hook must not remind again; it waits quietly until terminal MoP proof arrives.
Active dev slots must not receive routine queued MoP clears; queued clears fire
on the next Stop hook and can interrupt PM/slot handoff context.

The background agent may include PM-owned operational items in the Slack report:
dispatch queue work through `Skill(direct-assign)` / `Skill(direct-assign)`, reconcile PM
labels with the PM state helper, resume interrupted `cleanup-pr` through its
skill, run heartbeat-owned hygiene such as session-age clear and stale-process
cleanup, and reconcile aggregate PM ops drift. The agent must not take
product/customer/data-model decisions; it should report those as blocked with
exact evidence.

## [HIGH] PM State Label Drift Sweep (Rajiv directive 2026-05-20 11:48 IST thread `1779250700.624619`)

On every 3h heartbeat, the bg agent MUST run a drift sweep over open PRs and report any of these conditions:

```bash
gh pr list --state open --json number,title,isDraft,labels --jq '
  .[] | {
    number,
    isDraft,
    pm_state_count: ([.labels[].name | select(startswith("pm-state:"))] | length),
    pm_state: ([.labels[].name | select(startswith("pm-state:"))][0] // "MISSING"),
    pm_blocked: [.labels[].name | select(startswith("pm-blocked:") or startswith("pm-cleanup:"))]
  }'
```

Drift conditions to surface in the heartbeat report:
- `pm_state == "MISSING"` on an open PR → fact-check the lifecycle phase + apply via `gh pr edit <PR> --add-label pm-state:<suffix>`.
- `pm_state_count > 1` → next state transition via the helper will normalize; surface for visibility.
- `pm-blocked:*` set without `pm-state:blocked-rework` → inconsistency.
- `pm-cleanup:needed` set without `pm-state:merged-cleanup-pending` → interrupted `cleanup-pr`; dispatch a `cleanup-pr` resumption bg agent.

These rows go in the heartbeat report alongside existing stale-PR / GitHub-label
tracking surfaces.

## [HIGH] Pre-post reconciliation — 5-source check (Rajiv directive 2026-05-21 18:47 IST thread `1779367576.553679`)

Before any heartbeat Slack post that names a specific PR + state ("PR #NNNN is qa-passed-awaiting-ci / pm-review-pending / merge-ready / blocked-rework"), the bg agent MUST run a 5-source reconciliation on that PR. If ANY of the 5 sources disagree on the effective state, do NOT emit the unqualified state line — log the mismatch and either reconcile OR include a `STATE_MISMATCH` annotation in the status post.

The 5 sources (all must agree before stating an effective state):

1. **GitHub PR labels** — canonical `pm-state:*` + `pm-blocked:*` + `slot:N`:
   ```bash
   gh pr view $PR --json labels --jq '[.labels[].name | select(startswith("pm-state:") or startswith("pm-blocked:") or startswith("slot:"))]'
   ```

2. **PR body** — latest body markers (`Awaiting Rajiv`, `decision pending`, `BLOCKED on #NNNN`, etc.):
   ```bash
   gh pr view $PR --json body --jq '.body' | head -40
   ```

3. **Codex inline comment state** — count of unresolved Codex-authored review threads:
   ```bash
   gh api graphql -f query='{repository(owner:"heydonna-app",name:"heydonna-app"){pullRequest(number:'$PR'){reviewThreads(first:100){nodes{isResolved comments(first:1){nodes{author{login}}}}}}}}' \
     --jq '[.data.repository.pullRequest.reviewThreads.nodes[]
       | select(.isResolved == false)
       | select(.comments.nodes[0].author.login | test("codex"; "i"))] | length'
   ```

4. **CI on latest head** — required-check rollup on the head SHA:
   ```bash
   gh pr view $PR --json statusCheckRollup --jq '[.statusCheckRollup[] | select(.conclusion != null) | {name, conclusion}]'
   ```

5. **MoP slot state** — live slot occupancy (GitHub labels are the tracking
   surface):
   ```bash
   mop status --slot $SLOT  # REST CLI
   ```

**Reconciliation decision tree:**

- ALL 5 agree → post the named state.
- ANY disagree → do NOT post the unqualified state line. Instead:
  - Log the mismatch to `/tmp/mop-notifications.log` with timestamp + PR + per-source values.
  - If a single reconciliation step (label flip, draft-flip) resolves it, run that step BEFORE the heartbeat post.
  - Otherwise, include `STATE_MISMATCH: <one-line source disagreement>` in the heartbeat status row for that PR (instead of an unqualified state name).

**Banned pattern:** posting `PR #NNNN: merge-ready` to Slack while pm-state label says `qa-passed-awaiting-ci`, or while Codex unresolved count > 0, or while CI is still pending. The drift-without-mention is the chitta-2026-05-20 pattern that produced phantom `ready` claims and pulled lead slots out of cycle.

Pre-post reconciliation is checklist-shaped — the bg agent renders a 5-row mini-table per PR named in the heartbeat report:

```
PR #NNNN reconciliation:
  labels:    pm-state:qa-passed-awaiting-ci, slot:3
  body:      "Awaiting CI green" (no decision-pending marker)
  codex:     0 unresolved
  CI:        2/3 SUCCESS, 1 PENDING (e2e)
  slot: slot 3 STANDBY, GitHub labels match
  verdict:   AGREE → qa-passed-awaiting-ci (CI in flight)
```

## [HIGH] Queue-motion gate (Rajiv directive 2026-05-20 11:15 IST)

Before any heartbeat report claims `Actions needed: none` or `No follow-up items`, the bg agent MUST run all three checks AND have all three return CLEAR. If any returns NOT_CLEAR, replace the "none" claim with the specific action(s) required and route dispatch/rework through the canonical `Skill(direct-assign)` / `Skill(direct-assign)` path.

1. **Live slot state vs in-flight work mismatch.** Run `mop all` and cross-reference with `gh pr list --state open --json number,title,headRefName,labels,isDraft`. For every slot in FREE / STANDBY state, the Ready Pool must either (a) have no clean `status:todo` item claimable by `Skill(direct-assign)`, OR (b) carry a concrete blocker emitted by `Skill(direct-assign)`. Otherwise -> dispatch required.
2. **Approved dispatchable queue (GitHub labels).** Run the canonical
   `Skill(direct-assign)` / `Skill(direct-assign)` path over the GitHub Ready Pool labels.
   If `free_slots > 0` and `CLEAN_CLAIMABLE_ISSUES count > 0`, the heartbeat
   report MUST surface a dispatch action, not "none". If raw
   `CLAIMABLE_ISSUES` is nonzero but every item is
   `TODO_DEPENDENCY_TRIAGE_REQUIRED`, the required action is backlog-triage
   migration, not dispatch.
3. **Blocked / stale PRs needing PM motion.** From the pre-injected "Stale PRs (open >24h)" block, filter to: (a) PRs older than 24h, (b) PRs marked draft with last-push older than 4h, (c) PRs with ≥2 unresolved Codex bot threads, (d) PRs with `Awaiting Rajiv` flag where the dependency has cleared. Any match → surface as required action.

**Banned phrases when any of the three above returns NOT_CLEAR:** `Actions needed: none`, `No follow-up items`, `No PM action required`, `Nothing to do`. Replace with the specific dispatch / unblock / rework decision. Reporting "none" with dispatchable queue work is INVALID - it's the chitta-2026-05-20 stall pattern (PR #4711 + #4735 + #4727 slots sat idle while ready PRs aged in Rajiv's merge queue and other slots had free capacity).

Companion: `pm-idle-notification` Type 6 invariant + `feedback_pm_idle_actions_none_invalid_when_queue_has_dispatchable_work`.


## [HIGH] Control-plane defect escalation gate (Rajiv directives 2026-08-15 07:21 + 07:28 IST, thread `1786757064.752059`)

The 3h heartbeat is the BACK STOP for control-plane defects. Any workflow
machinery defect that blocks slot or PR motion must be (a) surfaced in the
heartbeat report as a top-priority dispatch row — never a bare "pending"
obligation line — and (b) escalated to the CTO immediately, not deferred to
the next heartbeat.

The bg agent MUST run this check before emitting the report:

1. **Scan for CP-blocking evidence.** From MoP slot states, transition
   refusals, pm-ops obligation rows, and the pre-injected data: any
   `active_turn_state=indeterminate` on an occupied slot, exit-13/exit-32
   release or claim refusals, index/fresh-assign self-invalidation,
   CI-start no-ops (terminal-receipt resume with no real run), stale
   review-head start refusals, admission-hold wedges, or planner/gate
   registry gaps counts as a CP defect.
2. **Classify each as dispatch-blocking or latent.** Blocking = the defect
   directly prevents slot release/claim/CI-start/merge motion on a live
   PR/issue. Latent = no live PR/issue currently blocked.
3. **Report every blocking defect as a top-priority row** in the Slack
   report: defect class, evidence (exact transition/exit code/head), the
   stuck PR/issue, and the CTO-thread where it was reported. Banned:
   burying a blocking CP defect as a generic "pending" obligation without
   the escalation callout.
4. **Escalate to the CTO immediately on discovery** (same heartbeat cycle,
   not the next one): decision class + evidence + exact stuck transition,
   in the dev thread with the CTO mentioned. Rajiv directive 07:21:
   report ALL control-plane issues to the CTO whenever discovered so they
   can be repaired. Do not batch CP reports into a later cycle.
5. **If the defect was already reported and is in the CTO's repair lane**,
   the heartbeat row must state the CTO lane status (accepted/in flight)
   and the landed-receipt wake — do not re-report, do not bypass.

Companion: `feedback_pm_heartbeat_cp_defect_escalation_gate` (2026-08-15).

## Pre-injected Data (dynamic context)

### Stale PRs (open >24h):
!`gh pr list --state open --json number,title,updatedAt,isDraft --jq '.[] | "#\(.number) [\(if .isDraft then "DRAFT" else "OPEN" end)] \(.title) (updated: \(.updatedAt[:10]))"' 2>/dev/null || echo "gh unavailable"`

### In-progress issues:
!`gh issue list --label "status:in-progress" --json number,title,updatedAt --jq '.[] | "#\(.number): \(.title) (updated: \(.updatedAt[:10]))"' 2>/dev/null | head -10 || echo "none"`

### CC reports pending:
!`ls -la /tmp/claudes-corner-*.md 2>/dev/null | wc -l || echo "0"`

## When NOT to Use

- Start-of-day comprehensive brief — use `morning-brief` instead
- Quick status check — use `pm-status`
- Monitoring a specific slot — use `check-slot` or `monitor-slot`
- Handoff work to a slot — use `pm-handoff` / `pane-handoff`
- Merging PRs — use `merge-pr`

## Trigger

`/heartbeat-tasks` arrives in the PM pane, injected by MoP `/pm-cadence`.
launchd does not directly inject the heartbeat when MoP is healthy.

## Execution Model — Background Agent

**v2.0 (2026-03-24):** Heartbeat runs as a BACKGROUND AGENT so PM stays
event-driven. The previous inline model blocked PM for 2-5 minutes, causing
merge requests, slot-idle notifications, and Rajiv messages to queue up.

### PM Main Thread Actions (< 5 seconds)

When the heartbeat trigger arrives, PM does exactly TWO things:

1. Launch the heartbeat background agent (see prompt below)
2. Resume processing events — PM is free immediately

### When the Agent Completes

PM receives the completion notification and:
1. Reads the agent's report summary
2. Verifies every action item is either `EXECUTED`, `DISPATCHED`, or `BLOCKED`
   with proof
3. Continues normal PM routing

**The agent sends the Slack report directly after action handling.** PM does not
need to relay it.

## Background Agent Prompt

Use this EXACT prompt when launching the agent. This prompt supersedes the older
inline/checklist wording that follows in historical sections of this file:

```
You are the Sakshi Heartbeat background agent.

Working directory: /Users/rajiv/Downloads/projects/heydonna-app

Mission:
1. Run the deterministic heartbeat probe.
2. Read and validate its JSON proof artifact.
3. Classify operational action items through canonical PM paths.
4. Collate actions taken, blocked items, proof paths, and residual risks.
5. Post the final Slack report only after action handling is complete.

Do not run `sakshi-heartbeat.sh --send-slack`.

STEP 1 - Run deterministic probe:
  bash /Users/rajiv/.claude/scripts/sakshi-heartbeat.sh

  Required artifacts:
  - /tmp/sakshi-heartbeat.json
  - /tmp/sakshi-heartbeat.txt

  The report is invalid unless /tmp/sakshi-heartbeat.json contains fresh rows
  for PM, S1, S2, S3, S4, S5, and S6.

STEP 1.5 - Run deterministic session-age reconciliation:
  python3 /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/heartbeat-session-age-clear.py

  Required artifact:
  - /tmp/session-age-clear-latest.json

  This script is materialize-only for numbered slots: it never clears a
  session, and every age-due dev pane is emitted as
  `status: pending, can_clear_now: false, materialized_only: true`. It creates or
  refreshes the single `pm-self-clear` obligation for PM and syncs `pm-todo.md`.

  The heartbeat must NOT clear a dev slot from this artifact, must not promote a
  materialized dev-slot row into a clear action, and must not create
  `SESSION_AGE_CLEAR_PENDING slot:N`. Dev-slot clearing belongs to the new-issue
  assignment boundary.

  The report is invalid if:
  - the agent attempts or reports any dev-slot clear from this artifact;
  - any `SESSION_AGE_CLEAR_PENDING slot:N` row exists for a dev slot;
  - PM is due and `pending_pm_todo` does not include `PM`.

STEP 1.55 - Remind PM to satisfy the PM self-clear obligation:
  Read `/tmp/session-age-clear-latest.json` after reconciliation.
  - Do NOT emit any dev-slot clear reminder. A materialized dev-slot row is never
    an action item; dev slots are cleared only at the next new-issue assignment
    boundary.
  - If `PM` is pending, verify that one open `pm-self-clear` obligation exists
    and add an explicit reminder to satisfy it at the next safe Stop boundary
    after housekeeping. Name obligation 15606 while it remains open; after it
    resolves, name only the single current open obligation.
  - Include the PM reminder in both the heartbeat Slack report and the summary
    returned to PM until terminal clear proof exists.
  - Do not interrupt active work, auto-clear PM, or create duplicate
    obligations.

STEP 1.6 - Run heartbeat-scoped recent intake and dependency reconciliation:
  /Users/rajiv/.claude/scripts/backlog-triage.py --surface heartbeat --recent-hours 72

  If it emits TODO_BLOCKED_DEPENDENCY, run:
  /Users/rajiv/.claude/scripts/backlog-triage.py --surface heartbeat --recent-hours 72 --apply --migrate-blocked-todos

  Reconcile every open pm-blocked:dependency issue from the pinned heartbeat
  snapshot against live issue/PR state:
  - canonical Ready Pool blockers win over incidental prose;
  - if all real blockers are closed/merged and required proof gates pass,
    rewrite the issue to PROMOTE, set blockers:none, remove backlog/dependency
    labels, add status:todo, preserve/repair canonical priority and validation,
    and validate dispatch readiness;
  - if a closed blocker was split or superseded by open children, rewrite the
    blocker to those live successors and keep DEPENDENCY_WAIT;
  - never promote a downstream child ahead of an open predecessor.

  Do not broaden this intake scan to unchanged historical backlog. Ignore
  ordinary backlog promotion/refill candidates; daily/manual planning owns
  them.

STEP 1.75 - Audit the complete live Ready Pool:
  /Users/rajiv/.claude/scripts/backlog-triage.py audit-ready-pool

  Required artifact:
  - /tmp/pm-ready-pool-audit-*.json from this invocation

  This scan is independent of the 72-hour backlog intake window and must cover
  every open status:todo issue (the deterministic command scans up to 1000).
  If it emits READY_POOL_CONTRACT_REPAIR_REQUIRED, repair the exact issue list
  directly in this heartbeat agent. Pin the list once and read every complete
  body/comments/labels/dependency state. For each issue, either:
  - repair the full ICL, canonical priority, Ready Pool metadata, branch slug,
    validation contract, and labels so it remains a dispatch-valid Todo; or
  - move it to the canonical typed non-Todo disposition with owner, next
    action, wake condition, and evidence.

  Do not launch a backlog repair/refill agent and do not scan ordinary backlog
  to replace invalid Todos. Rerun the audit after repair and require either a
  valid Todo or typed non-Todo proof for every flagged issue before marking the
  action EXECUTED. Then sync PM ops and run Skill(direct-assign).

STEP 2 - Parse action items:
  Read /tmp/sakshi-heartbeat.json, /tmp/sakshi-heartbeat.txt, and the fresh
  /tmp/pm-ready-pool-audit-*.json manifest.
  Build an action ledger with one row per item:
  - source
  - required action
  - owner/path
  - terminal state: EXECUTED, DISPATCHED, CLEAR, or BLOCKED
  - proof

STEP 3 - Classify through canonical PM paths:
  - Session-age cleanup: must already be reconciled by
    `.claude/scripts/heartbeat-session-age-clear.py`. Cite
    `/tmp/session-age-clear-latest.json`, `clears_attempted`, `mop_results`, and
    `pending_pm_todo`. Do not replace this with a prose-only PM-todo row.
  - Queue-motion / dispatchable work: use `Skill(direct-assign)` / `Skill(direct-assign)`.
    Do not hand-roll labels, MoP assignment, or handoff delivery.
  - PM label drift: reconcile with the PM state helper after fact-checking PR state.
  - PM ops heartbeat snapshot: run
    `python3 /Users/rajiv/.claude/scripts/pm-ops.py snapshot --surface heartbeat --limit 12`
    and execute/prune/dedupe heartbeat obligations. Do not copy those rows into
    hourly unless one directly blocks an active slot/PR transition.
  - P0 phone escalation: the 3-hour heartbeat is the sole scheduled evaluator
    and call owner. From one bounded current snapshot, call Rajiv only when the
    P0 genuinely needs intervention: mitigation failed or data-loss risk
    resumed; the sole owner disappeared; a promised PASS/BLOCK or concrete
    continuation is at least 15 minutes overdue with no active work; or an
    exact product/data/release decision can only be made by Rajiv. A mitigated
    P0 with an active owner and an event-driven next boundary is not call-due.
    When call-due, post one compact Slack decision packet and place exactly one
    Twilio call in this same heartbeat wake. A Rajiv reply or terminal call
    receipt satisfies that exact escalation state; do not call again unless a
    material escalation condition changes. Never add hourly, autonomous, or
    five-minute retry/nag paths, and never treat a missing optional receipt as
    permission to place a second call.
  - Stale-process cleanup: must already be reconciled by
    `python3 /Users/rajiv/.claude/scripts/stale-process-cleanup.py --apply`.
    Cite `/tmp/stale-process-cleanup-latest.json`, `before.candidates`,
    `killed`, and `after.candidates`. Do not replace this with prose-only
    cleanup claims or ad-hoc `ps | grep | kill` snippets.
  - `pm-cleanup:needed` drift: launch/resume `cleanup-pr` as a background agent.
  - Customer-visible Axiom or artifact symptoms: launch customer-artifact-investigator
    or mark BLOCKED with the exact required agent prompt.
  - Product, legal-output, data-model, or roadmap decisions: do not decide inside
    heartbeat; mark BLOCKED with evidence and the PM/Rajiv question.

STEP 4 - Reconcile before Slack:
  Re-read the relevant source after every classification. Do not claim an item is done
  from send-only proof. Required examples:
  - Session-age clear: cite `/tmp/session-age-clear-latest.json` and MoP clear
    event proof for idle dev slots, plus per-pane PM-todo proof for active dev
    slots and PM. A combined "PM + all slots pending" row is invalid when any
    due dev slot is idle/free.
  - Dispatch: cite MoP delivery event plus subsequent same-slot pickup/response event proof.
  - Label drift: cite current GitHub labels.
  - Cleanup: cite cleanup-pr state or explicit PENDING_FLIP line.

STEP 5 - Slack report:
  Post one concise Slack report after action classification. Include:
  - Health/session/slot summary from /tmp/sakshi-heartbeat.txt
  - Actions taken with proof
  - Blocked items with exact next owner and evidence
  - Residual risks

  Use Slack mrkdwn, not markdown:
  - single asterisk for bold
  - blank lines between sections
  - bullets for action rows

  Send via:
  bash ~/.claude/skills/slack-message/scripts/slack-send.sh -c C0ALZJHGE49 -f

  Routing (mandatory):
  - Post one NEW TOP-LEVEL message in #heydonna-dev (C0ALZJHGE49). Do NOT pass
    `-t`, do not reply to an older thread, and never fall back to the Rajiv DM
    default for the heartbeat report.
  - Start the report with a CTO mention so the decision/status thread is visible
    to Abhijit CTO: `<@U0BNFGX2UAX>` on the first line.

STEP 6 - Return PM summary:
  Return a structured summary with:
  - health_status
  - actions_executed
  - actions_dispatched
  - blocked_items
  - slack_post_proof
```

## Legacy Check Details

The detailed checks below describe the historical health domains. They are
reference material only. Do not bypass the runtime contract above and do not
post Slack before action handling.

STEP 0 — Load tools:
  Use the REST CLI for slot state (no MCP tool load needed).

STEP 1 — Axiom Error Health (3h window):
  Run: python3 scripts/axiom-activity-report.py --hours 3 --quiet
  This queries Axiom REST API directly (no MCP needed) with built-in rate-limit handling.
  Parse the output for error count and breakdown.
  If errors > 0: record breakdown. If errors == 0: record "Clean".
  If script fails: record "Axiom unavailable", move on.
  If the breakdown contains customer-visible pipeline/export/DOCX/template/editor corruption,
  auto-process failure, Modal/R2/LangSmith-backed artifact evidence, or a named
  transcript_id/project_id/file/customer identifier: do NOT root-cause it inside the
  heartbeat. Launch customer-artifact-investigator if subagent launch is available from
  this context; otherwise return REQUIRED_AGENT with the exact prompt for PM to launch.
  The heartbeat report must include either:
    - investigation_report: docs/investigations/YYYY-MM-DD-*.md, or
    - REQUIRED_AGENT: customer-artifact-investigator pending for <identifier>
  Banned: "likely caused by <code path>" from Axiom summary alone.

STEP 2 — Slot Status + Session Age:
  Call `mop all` for state data.
  Also capture live tmux: for i in 1 2 3 4; tmux capture-pane -t 0:0.$i -p -S -5
  Check for stuck-on-prompt: "Would you like to proceed?" pattern.
  Record per-slot status (task, activity, idle/active, DND).

  SESSION AGE + ANALYSIS (Rajiv directive 2026-04-06):
  For each active slot, check session age from the OMP runtime:
    slot N: look at ~/.omp/sessions/heydonna-slotN/*.jsonl (top-level files only;
    exclude nested reviewer/subagent JSONLs).
    Use the newest OMP session's latest timestamped record (falling back to its
    header/first timestamp), then take the later of that value and the latest
    successful MoP slot_cleared or
    clear_pending_executed event. Queued/skipped/failed/latched clears do not
    reset age. If no OMP file or successful clear source exists, report
    unknown/fail closed rather than using a legacy Claude JSONL.
    PM alone retains the legacy Claude-project SessionStart:clear behavior.
    If session age >= 3 hours: flag "⚠️ Slot N session: Xh old"
    If session age >= 6 hours: flag "🔴 Slot N session: Xh old"
    (Rajiv directive 2026-08-02, AMENDED by the control-plane correction of 2026-09-21: the deterministic heartbeat probe and heartbeat background agent own recurring session-age detection and PM-todo tracking, but NOT dev-slot clearing. Dev slots S1-S6 are NEVER cleared on a cadence; a numbered slot's session is cleared only at the new-issue assignment boundary inside the atomic assignment operation `Skill(direct-assign)` / `mop-assign-slot`. The heartbeat must not produce, consume, or remind on `SESSION_AGE_CLEAR_PENDING slot:N`, and must not invoke `.claude/scripts/mop-clear-slot.sh` for any dev slot. Heartbeat MUST run `.claude/scripts/heartbeat-session-age-clear.py` after the probe, which is materialize-only for numbered slots and never clears a session. Clear-worthy condition for the PM session: PM effective session age >= 3h. Autocompact count is not required. Due PM becomes a PM pending-clear row; `.claude/hooks/pm-self-clear-stop.sh` only emits a non-blocking Stop reminder until PM finishes housekeeping, saves any last note to pm-todo/pm-ops, and self-clears through MoP logging with `bash /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/mop-clear-slot.sh pm`; it must not return `decision:block`, and it resolves the row only with terminal MoP proof after that clear. Do not queue automatic MoP clears for active work or PM. Do not inject raw `/clear`, and do not use respawn as the stale-session fix. Escalate every product or process decision to CTO; CTO DMs Rajiv and waits for explicit approval. Mechanical execution of the PM self-clear reminder remains heartbeat-owned; dev-slot clearing is owned by the new-issue assignment boundary.)

  SESSION CONTENT ANALYSIS (Rajiv directive 2026-04-06):
  For sessions >=3h, do a quick content analysis — MUST use context-mode sandbox.
  NEVER Read the raw .jsonl into main context — these files are 50-500MB.

  Use ctx_execute_file with an intent string:
    mcp__plugin_context-mode_context-mode__ctx_execute_file({
      path: '<session-jsonl-path>',
      language: 'python',
      intent: 'decisions and directives, issues worked, error-fix pairs, tool call count',
      code: 'import json\\nissues=set(); errors=0; tools=0\\nfor line in FILE_CONTENT.splitlines()[-500:]:\\n  try: o=json.loads(line)\\n  except: continue\\n  if o.get("type")=="assistant":\\n    for c in o.get("message",{}).get("content",[]):\\n      if isinstance(c,dict) and c.get("type")=="tool_use":\\n        tools+=1\\n        if c.get("name")=="Bash" and "error" in str(c.get("input","")).lower(): errors+=1\\nprint(f"issues={issues}, tools={tools}, errors={errors}")'
    })

  Summarize in 1-2 lines per slot in the report: "Slot N: 8h session, worked on #3300, 45 tool calls, 2 errors"
  This gives Rajiv visibility into session productivity without needing to export and analyze manually.

  ⚠️ Test this on ONE heartbeat before full rollout. If ctx_execute_file times out or
  returns empty, fall back to direct python3 subprocess (still via ctx_execute, never Read).

  Legacy note: the current runtime contract above supersedes the old inline
  behavior. The background agent reports operational action items with proof;
  session-age clear detection is owned by heartbeat, executable idle dev clears
  go through `.claude/scripts/heartbeat-session-age-clear.py`, active-slot
  clears remain pending until PM releases the slot at a natural boundary, and PM
  clears remain pending until PM manually self-clears after housekeeping.

STEP 3 — Claude's Corner check:
  Check for unprocessed CC reports: find /tmp/claudes-corner-*.md 2>/dev/null
  If found: record count. PM will launch cc-processing separately.
  If none: record "No CC reports".

STEP 4 — Stale Work Detection:
  PRs open >24h: gh pr list --state open --json number,title,updatedAt,isDraft
  Issues in-progress >24h: gh issue list --label "status:in-progress" --json number,title,updatedAt
  Filter by updatedAt < yesterday. Record findings.

STEP 5 — Product Intelligence:
  New users: npx convex data users --prod --order desc --limit 5 (timeout 30s)
  Product activity: already captured in STEP 1 output (axiom-activity-report.py includes
  product activity section — exports, proofreads, formats, transcriptions).
  Extract from the STEP 1 report. If zero activity: skip. If notable: record.

STEP 6 — Housekeeping:
  Old temp files: find /tmp/handoff-*.md /tmp/qa-report-*.md -mtime +7 2>/dev/null
  Stale assignments: check MoP slots where occupied=true but issue is CLOSED in GitHub.
  Record findings. DO NOT clean up — just report.

  PM Ops Autonomy Gate audit (CP #8c, Rajiv directive 2026-05-26 13:11 IST):
  Verify no `[PM_OPS_PUNT_DETECTED]` reminders fired in the last hour without follow-up auto-execution.
    SINCE=$(date -u -v-1H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)
    grep -c "PM_OPS_PUNT_DETECTED" "$HOME/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/skill-hint-log/$(date -u +%Y-%m-%d).tsv" 2>/dev/null
  If count > 0: record "PM punt-phrase fired Nx in last hour — verify each was auto-executed not deferred to Rajiv."

STEP 6.5 — Stale process sweep (Rajiv directive 2026-05-06 11:09 IST):
  cleanup-pr Step 8.5 only runs on PR merges. If hours pass between merges, stale
  agent-browser Chromes, orphaned `convex dev --once` runs, and orphaned `tsc --noEmit`
  procs accumulate and silently consume CPU. Heartbeat runs every 3h regardless of
  merge cadence — a perfect place to sweep.

  Run the canonical helper, never inline shell process killing:

    python3 /Users/rajiv/.claude/scripts/stale-process-cleanup.py --apply

  The helper is intentionally allowlisted and proof-backed:
    - agent-browser / Chrome-for-Testing / Chromium remote-debugging:
      default threshold 60 minutes;
    - Next.js/node (`next dev`, `next start`, `next-server`):
      default threshold 6 hours;
    - Convex `convex dev` and TypeScript `tsc --noEmit`:
      default threshold 30 minutes.

  Any process whose cwd maps to a slot checkout
  `/Users/rajiv/Downloads/projects/heydonna-app-300N` is killable only when
  MoP says that slot is `status=free`, `occupied=0`, and `idle=1`. If the
  associated slot is active, occupied, or not idle, the helper must skip the PID
  regardless of age.

  The heartbeat Slack report is invalid unless it cites
  `/tmp/stale-process-cleanup-latest.json` from the current run:
    - `before.candidates` count;
    - `killed` count and PIDs, or `clean`;
    - `after.candidates` count;
    - any skipped active-slot candidates if relevant.

STEP 6.6 — REMOVED (Rajiv directive 2026-08-12, thread 1786538007.514889):
  "note that hourly ops audit is deprecated. remove it from the heartbeat tasks."
  The hourly ops audit and its MoP status checks are REMOVED from the heartbeat
  tasks entirely. Do not inspect, pause, resume, or report the retired
  ops-audit state. (Kanban status refresh is also removed below per the Rajiv
  2026-08-12 kanban directive; GitHub labels are the tracking surface.)

STEP 6.7 — Ops hygiene enforcement (Rajiv directive 2026-05-29):
  PreToolUse blocking is NOT the primary enforcement mechanism for PM ops hygiene.
  It can wedge the PM pane, misdiagnose the real guard, and prevent the PM from
  taking the corrective action. Heartbeat must audit the state and produce
  concrete PM actions with proof requirements instead of relying on hook denial.

  Run these checks:
    1. MoP health:
       curl -sS --max-time 3 http://127.0.0.1:3100/health
       curl -sS --max-time 3 http://127.0.0.1:3100/slots
       If MoP is unreachable, action: "Dhurv: restart MoP and post /health proof."

    2. check-slot-bg disabled:
       launchctl print gui/501/com.heydonna.mop-server | grep MOP_CHECK_SLOT_BG_ENABLED
       tail -n 200 /tmp/mop-server.log | grep -E "check-slot-bg disabled|check_slot_skipped"
       Expected env is `MOP_CHECK_SLOT_BG_ENABLED=0`. check-slot-bg polling is retired
       because MoP's slot idle hook now handles promised-action miss recovery by
       injecting `continue your work` directly to the idle slot. If env is `1`,
       action: "Dhruv: disable MOP_CHECK_SLOT_BG_ENABLED in the MoP LaunchAgent,
       restart MoP, and post launchctl + /health proof."

    3. REMOVED — Kanban refresh (Rajiv directive 2026-08-12, thread
       1786538007.514889): "kanban is basically tracked with github labels now.
       we don't need a to maintain it separately." Do not run kanban-status or
       maintain a separate Kanban board; GitHub labels are the tracking surface.

    4. Slack bridge freshness:
       tail -n 200 /tmp/slack-bridge.log
       If recent Rajiv/PM messages are not forwarded or the bridge is stale,
       action: "Dhurv: restart/repair Slack bridge and post the latest forwarded timestamp proof."

    5. Pending post-issue latch files:
       ls -la /tmp/post-issue-create-sweep-*.flag /tmp/post-issue-create-sweep-*.resolved 2>/dev/null
       If a post-issue-create latch is pending, action must name the exact
       terminal transition: route the issue ASSIGNED or QUEUED, write/observe
       the .resolved proof, or escalate with blocker. Do not clear files by hand
       as the default fix.

    6. Occupied + idle slot routing:
       For each slot where occupied=true and idle=true, inspect the live pane
       and GitHub issue/PR state (labels). Produce one terminal PM action:
       nudge, release, dispatch next issue, mark waiting with evidence, or escalate.
       "Idle but assigned" is not enough; the heartbeat report must say what PM
       should do next and what proof closes it.

  Report format:
    - If all checks pass: "Ops hygiene: clean"
    - If anything fails: add each item to Actions needed with exact PM command,
      evidence source, and missing terminal transition.
    - Banned closure: "PreToolUse will enforce this." Heartbeat owns only the
      recurring hygiene/freshness audit; the hourly ops audit is deprecated
      (Rajiv 2026-08-12) and is not part of heartbeat tasks. Kanban is tracked
      via GitHub labels; no separate kanban-status is maintained.

STEP 7 — Send Slack Report:
  ⚠️ FORMAT RULES (Slack mrkdwn — NOT markdown):
  - Use *bold* (single asterisk), NOT **bold**
  - Use blank lines between sections for readability
  - Use bullet points (•) for lists
  - NEVER send as a single paragraph wall of text
  - Use \n\n between sections when building the string

  Compose report with CLEAR SECTION BREAKS:
  ```
  *Heartbeat HH:MM UTC*

  *Health:* [status] ([N] errors in 3h)

  *Slots:*
  • S1-Rohini: [status]
  • S2-Hasta: [status]
  • S3-Ashwini: [status]
  • S4-Chitra: [status]
  • S5-Revati: [status]
  • S6-Pushya: [status]

  *Stale:* [PRs/issues >24h or "none"]

  *Sweep:* chrome=N dev=N next=N

  *Product:* [activity summary or "quiet"]

  *Actions needed:* [numbered list or "none"]
  ```

  Send via stdin pipe for proper newline handling:
  echo "REPORT_TEXT" | bash ~/.claude/skills/slack-message/scripts/slack-send.sh -c C0ALZJHGE49 -f

  Routing (mandatory):
  - Post one NEW TOP-LEVEL message in #heydonna-dev (C0ALZJHGE49). Do NOT pass
    `-t` (thread reply) and never reply to an older heartbeat thread.
  - First line must mention CTO for visibility:
    `<@U0BNFGX2UAX> *Heartbeat HH:MM UTC*`
  - Do not use the Rajiv DM default channel for the heartbeat report.

STEP 8 — Return summary to PM:
  Return a structured summary with:
  - health_status: "clean" | "errors" | "unavailable"
  - error_count: N
  - slots_needing_attention: [list of slot numbers + reason] or []
  - stale_items: [list] or []
  - actions_needed: [list of recommended PM actions] or ["none"]

  This is what PM reads to decide follow-up actions.

## Launching the Agent

```python
Agent(
  subagent_type="general-purpose",
  run_in_background=True,
  description="3h heartbeat checks",
  prompt="<the prompt above with actual values>"
)
```

## What Changed from v1.0

| Aspect | v1.0 (inline) | v2.0 (background agent) |
|--------|---------------|------------------------|
| PM blocked | 2-5 minutes | 0 seconds |
| Task tracking | 7 TaskCreate calls | Agent-internal |
| Slack report | PM composes + sends | Agent sends after action handling |
| Actions on slots | PM applies only one mechanically required approved action | Agent escalates every product or process choice to CTO; CTO DMs Rajiv |
| Axiom timeout | Blocks PM | Agent handles gracefully |
| Event responsiveness | Queued during heartbeat | PM processes immediately |

## Anti-Patterns

- **Never run heartbeat inline** — always background agent (v2.0 directive)
- **Never post before action handling** — agent must execute, dispatch, or block each action item with proof first.
- **Never let heartbeat decide product/customer/data-model questions** — block those with evidence and owner.
- **Never skip the agent and just report** — "I'll quickly check" becomes 5 minutes inline
- **Never block on agent completion** — PM stays event-driven
- **Never take action on DND slots** — agent skips them in report
- **Never run morning-brief from here** — morning-brief is comprehensive; this is lightweight

## Timing

The launchd agent runs every 3h: midnight, 3 AM, 6 AM, 9 AM, 12 PM, 3 PM, 6 PM, 9 PM.

The script injects `/heartbeat-tasks` into the PM pane. PM handles the command
by launching the background agent and immediately returning to the event loop.

## Setup

### Install LaunchAgent

The plist is at `~/Library/LaunchAgents/com.heydonna.pm-heartbeat.plist`.

```bash
# Load (or reload after edits)
launchctl unload ~/Library/LaunchAgents/com.heydonna.pm-heartbeat.plist 2>/dev/null
launchctl load ~/Library/LaunchAgents/com.heydonna.pm-heartbeat.plist
```

### Verify

```bash
launchctl list | grep heartbeat
# Should show: -  0  com.heydonna.pm-heartbeat
```

### Test Manually

```bash
bash ~/.claude/scripts/scheduled-heartbeat.sh
```
