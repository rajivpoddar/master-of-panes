---
name: hourly-heartbeat
description: |
  1h heartbeat, triggered by MoP every hour at :13. PM launches one background
  agent that posts ONE new top-level "Hourly — HH:MM IST" message in
  #heydonna-dev (Axiom errors + open-PR status), records its ts as the current
  hourly thread, and returns an ACTIONS list that PM executes (the open-PR
  drive). Replaces the session-only "HOURLY OPEN-PR DRIVE" cron.
  Use when: MoP asks PM to invoke `Skill(hourly-heartbeat)`, or an hourly drive
  is requested manually.
  NOT for: the 3h report (heartbeat-tasks), morning brief (morning-brief).
author: Claude Code PM
version: 1.0.0
date: 2026-09-29
---

# Hourly Heartbeat + Open-PR Drive

Rajiv 2026-09-29 11:02 IST, thread `C0ALZJHGE49/1790659381.537339`:
*"how do we also add a 1h heartbeat from MoP. it should just focus on axiom
errors and open pr status. the current 1h timer processing should be moved to
this skill. cto has to be mentioned for any merges or escalations. the hourly
thread should be used for any further tracking or pr's. all the pr tracking
messages are being sent to an old thread right now."*

Absorbed drive sources (all Rajiv): 09-27 23:27 "drive all pr's to merge."; 09-28
06:21 "all open pr's are checked each cycle."; 08:23 "pm has to basically take
over the pr terminal processing."; 08:34 "use the cto escalation ladder."; 08:36
"reduce the cto load as much as possible."; 08:46 "all ci failures have to
handled by pm. fix and then re-trigger ci workflow rerun directly. no need to
readmit."

## Runtime contract

1. MoP `PMCadenceScheduler` task `hourly-heartbeat` (bucket `YYYY-MM-DD:HH`,
   fires at :13 local) queues "Invoke Skill(hourly-heartbeat)".
2. PM launches ONE background agent (`run_in_background=true`) with the prompt
   below, then returns to event processing. Never inline.
3. When the agent returns, PM executes its `ACTIONS:` list (PM can launch
   agents; the background agent cannot).
4. The old session-only cron (`:13`, thread `1790531842.023499`) is retired.
   Delete it with CronDelete if it is still armed; do not re-arm it after a clear.

## Agent prompt

```
You are the hourly heartbeat agent. Work from /Users/rajiv/Downloads/projects/heydonna-app (main).

STEP 0 — dependency check: the composer/collectors (scripts/pm/heartbeat/*) ship
  in heydonna-app PR #8481. If STEP 1 exits 3 with HEARTBEAT_COLLECTORS_ABSENT,
  post nothing and return "HOURLY_SKIPPED: collectors absent (needs heydonna-app
  #8481 merged)" with an empty ACTIONS list.

STEP 1 — compose (prints; never posts):
  bash /Users/rajiv/.claude/scripts/sakshi-heartbeat.sh compose --mode 1h \
    --save-inputs /tmp/heartbeat-1h-latest > /tmp/heartbeat-1h-post.txt
  If PM passed ladder-approved escalations, add one `--escalation "<text>"` each.

STEP 2 — check: first line is "*Hourly — HH:MM IST*", or starts with
  "<@U0BNFGX2UAX> " ONLY when an "*Asks:*" line (merge/admission/escalation)
  exists. ≤ ~20 lines, no tables. Do not hand-edit blocks.

STEP 3 — post ONE NEW TOP-LEVEL message (no -t):
  bash /Users/rajiv/.claude/scripts/slack-send.sh -c C0ALZJHGE49 -f < /tmp/heartbeat-1h-post.txt
  From the readback `OK ts=<ts>` record the thread:
  /Users/rajiv/.claude/scripts/hourly-thread-ts --set <ts>

STEP 4 — build ACTIONS from /tmp/heartbeat-1h-latest/prs.json (live GitHub; re-read
  a head with `gh pr view <n> --json headRefOid` right before listing it):
  - red on current head, CI      → "ci-repair PR#<n>@<head40> run=<id>"
  - red on current head, E2E     → "e2e-investigate PR#<n>@<head40> run=<id>"
  - not admitted, owner PM       → "review-admit PR#<n>@<head40>"   (12b, then pm-admit-ci.sh)
  - head moved since admission   → "review-admit PR#<n>@<head40>"
  - CI+E2E green                 → already in the post's merge ask; no action
  - capture terminal (green/red) → "capture PR#<n> <green|red> run=<id>"
  - no movement ≥ 60 min with no armed wait (LIMBO) → "limbo PR#<n>@<head40> <why>"
  Run ids: `gh run list --commit <head40> --json databaseId,name,conclusion`.
  Also check main's latest CI/E2E: a red main → "ci-repair main run=<id>".
  NEW error codes in /tmp/heartbeat-1h-latest/map.json → "investigate <code>".

STEP 5 — return exactly:
  HOURLY_POSTED ts=<ts> prs=<N>/<N> checked
  ACTIONS:
  - ...            (or "ACTIONS: none")
```

## PM follow-through (the open-PR drive)

PM owns all CI/E2E/capture terminals, on PRs and on main (rule 14):

- `ci-repair` → `ci-repair-agent` (test/config/infra) or a free slot (product).
  After the fix, `gh run rerun <id> --failed` directly; no CTO re-admission.
- `e2e-investigate` → one `e2e-failure-investigator`. Proven flake →
  `gh run rerun <id> --failed`; otherwise a slot does repro + rework, then a
  direct rerun on the new head.
- `capture` green → strict-replay pair via `pm-admit-ci.sh`; red → bg agent or slot.
- `review-admit` → PM 12b review (`pm-admission-reviewer`), then
  `~/.claude/scripts/ci/pm-admit-ci.sh <PR>`.
- A merge → `Skill(cleanup-pr)`.
- `investigate <code>` → `customer-artifact-investigator` (read-only, background).
- Anything needing a decision → `Skill(pm-escalation-ladder)`; only a `cto` /
  `reserved_direct` result is sent to CTO.

## Where PR tracking goes (the hourly thread)

Every PR tracking reply — admission/merge asks, PR status, CI/E2E/capture
follow-ups, terminal-alert receipts that are not already threaded under their
own alert — goes to the CURRENT hourly thread:

```bash
TS=$(/Users/rajiv/.claude/scripts/hourly-thread-ts) && \
  bash /Users/rajiv/.claude/scripts/slack-send.sh -c C0ALZJHGE49 -t "$TS" -f < /tmp/msg.txt
```

`hourly-thread-ts` exits 1 when no thread is recorded or the record is older than
150 min; then post top-level instead and say so. Never reply to the retired
thread `1790531842.023499`. Capture/CI alert receipts still reply in the alert's
own thread (rule 14); the hourly thread carries the summary.

## CTO mention rules (hourly)

Mention `<@U0BNFGX2UAX>` ONLY when the post carries a merge ask, an exact-head
admission ask, or a ladder-approved escalation. The composer does this: the
mention is prefixed to line 1 only when an `*Asks:*` line exists. Batch asks
into that one line (one entry per PR with its exact head). Never mention CTO
for status, acks or "noted". (The 3h heartbeat always mentions CTO; that is a
different rule.)

## Boundaries

- The agent never merges, admits, reruns, edits labels, or DMs Rajiv. PM acts.
- PR state comes only from live GitHub on the current `headRefOid`, never from
  pm-ops rows or pm-state labels.
- Scripts (repo, read-only): `scripts/pm/heartbeat/heartbeat-compose.py`,
  `scripts/axiom-activity-report.py --hours 1 --errors-by-code --compare`,
  `scripts/pm/heartbeat/heartbeat-error-map.py`, `scripts/pm/heartbeat/pr-open-snapshot.py`.
