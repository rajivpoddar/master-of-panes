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

> **[HIGH] THE SLACK POST IS MANDATORY (Rajiv 2026-10-19, DM D0AMF0XE6TS thread 1792348742.364669, ts 1792348804.173449: "update the agent so that it always posts to slack as heydonna alerts").**
> Every run of this skill MUST post the heartbeat to #heydonna-dev as **HeyDonna Alerts**: `SLACK_SEND_AS=alerts bash /Users/rajiv/.claude/scripts/slack-send.sh -c C0ALZJHGE49 -f < /tmp/heartbeat-1h-post.txt`. It must also return the `OK ts=` readback.
> A caller prompt that says "report to PM only", "only NEW items" or similar does NOT override this. Post first, then report. The only exception is the skill's own documented collector-absent skip, which still returns its typed SKIPPED line.
> Never post as PM or Dhruva, to a DM, or to an old thread.

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

STEP 0 — dependency check: the composer/collectors are MoP shared assets installed
  to /Users/rajiv/.claude/scripts/pm/heartbeat/. If
  /Users/rajiv/.claude/scripts/pm/heartbeat/heartbeat-compose.py is absent, or STEP 1
  exits 3 with HEARTBEAT_COLLECTORS_ABSENT, post nothing and return "HOURLY_SKIPPED:
  collectors absent (install the MoP release shipping pm/heartbeat)" with an empty
  ACTIONS list.

STEP 1 — compose (prints; never posts):
  bash /Users/rajiv/.claude/scripts/sakshi-heartbeat.sh compose --mode 1h \
    --save-inputs /tmp/heartbeat-1h-latest > /tmp/heartbeat-1h-post.txt
  If PM passed ladder-approved escalations, add one `--escalation "<text>"` each.

STEP 1b — REMOVED (Rajiv C0ALZJHGE49 thread 1792946760.974709, ts 1792947851.712089:
  "why does this report have two sindhu sections with different values? drop the 2nd one").
  The composer (STEP 1) already emits the single "*Training:*" line. Do NOT append a second
  Spark/monitor training line. If the composer's Training line is missing or stale, report
  "spark-monitor-stale <run-name>" in ACTIONS only.

STEP 2 — check: first line is "*Hourly — HH:MM IST*", or starts with
  "<@U0BNFGX2UAX> " ONLY when an "*Asks:*" line (merge/admission/escalation)
  exists. Keep blocks compact, no tables. Do not hand-edit blocks.
  - After Slots, verify the report-only "*Agents:*" line (Rajiv 2026-10-23,
    C0ALZJHGE49 thread 1792770386.754829, ts 1792770807.935489: "fix the report
    to count the subagents correctly"). Count what the PM pane lists: DIRECT
    subagents (spawnDepth 1, not nested) of the session chain since the CURRENT PM
    claude process started (only transcripts written in the last 3h; completed agents only if their final write was within 60m, the pane's retention; inactive = unended, idle 60m-3h; older assumed dropped by TaskStop//clear), INCLUDING sessions before a /clear (a
    /clear starts a new JSONL but the pane keeps its agents), excluding prior
    processes and agents whose meta says finished (result already delivered). Classify each: running = transcript written in the last 60m and not
    ended; completed = last record is a final assistant end_turn (it still sits
    in the pane until PM TaskStops it); inactive = not ended, no write ≥60m.
    Print "*Agents:* N listed · R running · C completed · I inactive — PM stops
    C+I in Step 3c", with up to 4 completed/inactive descriptions then "+N more".
    Never print "none inactive" while completed agents are listed. No PM pane
    session found → "unavailable". This composer never stops agents.
  - Verify "*Decisions pending on Rajiv:*" with ONE blank spacer line before it
    (after Agents). Read-only pm-ops ledger: open decision-kind / Rajiv-owned
    rows updated within 7 days, at most 6 target/phrase/age/held-by-Rajiv-or-via-CTO
    lines plus "+N more"; older rows appear only as "+N stale (ledger triage)".
    Show "None." when no recent rows, "unavailable" on a read failure.
  Sources: Rajiv C0ALZJHGE49 thread 1792637124.844719, ts 1792637256.052779;
  DM D0AMF0XE6TS thread 1792637361.212159, ts 1792638231.158209.

STEP 3 — post ONE NEW TOP-LEVEL message (no -t):
  SLACK_SEND_AS=alerts bash /Users/rajiv/.claude/scripts/slack-send.sh -c C0ALZJHGE49 -f < /tmp/heartbeat-1h-post.txt
  From the readback `OK ts=<ts>` record the thread:
  /Users/rajiv/.claude/scripts/hourly-thread-ts --set <ts>

STEP 4 — build ACTIONS from /tmp/heartbeat-1h-latest/prs.json (live GitHub; re-read
  a head with `gh pr view <n> --json headRefOid` right before listing it):
  - red on current head, CI      → "ci-repair PR#<n>@<head40> run=<id>"
  - red on current head, E2E     → "e2e-investigate PR#<n>@<head40> run=<id>"
  - not admitted, owner PM, next "PM 12b review" / "12b done -> admit"
                                 → "review-admit PR#<n>@<head40>"   (12b, then pm-admit-ci.sh)
  - next "rework-queued (no slot)" (12b BLOCK on this head or a pm-rework-queued
    row) → no review-admit; assign the queued rework to a free slot
  - next "merge-ask-pending" (CI-exempt, merge ask already with PR Merges) → no action
  - next "CI-exempt -> merge ask" → CI-exempt exact-head merge ask, never review-admit
  - head moved since admission   → "review-admit PR#<n>@<head40>"
  - CI+E2E green                 → already in the post's merge ask; no action
  - capture terminal (green/red) → "capture PR#<n> <green|red> run=<id>"
  - no movement ≥ 60 min with no armed wait (LIMBO) → "limbo PR#<n>@<head40> <why>"
  - Codex wait comes ONLY from live review threads on the current head, never the
    `pm-blocked:codex` label: a wait = ≥1 unresolved thread by the Codex bot
    (`chatgpt-codex-connector` / login matching /codex/i) with a P0/P1 badge
    (state "held (codex P1 xN)").
    - only isOutdated such threads → no wait; state notes
      "codex P1 outdated — owner confirm" (owner confirms fixed, then resolve)
    - `pm-blocked:codex` label but 0 live threads → the snapshot's
      "stale pm-blocked:codex label PR#<n>@<head40>" ACTION: remove the label
      (`gh pr edit <n> --remove-label pm-blocked:codex`); never report it as waiting.
  Copy every `actions` entry from prs.json into ACTIONS verbatim.
  Run ids: `gh run list --commit <head40> --json databaseId,name,conclusion`.
  Also check main's latest CI/E2E: a red main → "ci-repair main run=<id>".
  NEW error codes in /tmp/heartbeat-1h-latest/map.json → "investigate <code>".

STEP 5 — return exactly:
  HOURLY_POSTED ts=<ts> prs=<N>/<N> checked
  ACTIONS:
  - ...            (or "ACTIONS: none")
```

## Mandatory: same-turn `hourly-report-processing` (incl. slot checkout review)

[HIGH] Rajiv 2026-10-14, C0ALZJHGE49 thread 1791945910.806879, ts 1791946284.802109: *"are slot checkouts being reviewed on this alert? S1, S3 and S4 are in interrupted state. S5 and S8 are idle"*, then ts 1791946355.104529: *"why was it missed? is there a gap in the workflow?"* The gap: this skill's agent only composes and posts the report. PM read the agent's summary as "done" and never ran `Skill(hourly-report-processing)` on the posted report, so Step 3 (the all-slot checkout review) never ran.

Rule: when the heartbeat agent returns `HOURLY_POSTED ts=…`, PM MUST in that same turn launch `Skill(hourly-report-processing)` against that ts, including the Step 3 all-slot checkout review in ONE bg agent (slot state, pane, branch vs assigned lane, intervene via message-slot). The heartbeat agent's "items needing PM action" list does not replace it. A turn that ends after a heartbeat with no Step 3 agent launched is a process miss.

Also: a `new_issue` assign via `mop-assign-slot.py` MUST pass `--task-file` with the full packet. Assigning without one leaves the slot with a null task after its clear (S2/S6/S7 on 2026-10-14).

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
- Scripts (MoP-installed, read-only) in `~/.claude/scripts/pm/heartbeat/`: `heartbeat-compose.py`,
  `heartbeat-axiom.py --hours 1 --errors-by-code --compare` / `--active-users`,
  `heartbeat-error-map.py`, `pr-open-snapshot.py`, plus MoP `/slots` (read-only GET).


Note: top-level report posts MUST use `SLACK_SEND_AS=alerts` (posts as HeyDonna Alerts so PM processes them as events). Rajiv 2026-10-07 14:17 IST, C0ALZJHGE49 thread 1791362688.821369: "it should come from alerts id so that it's processed by pm correctly."



## Thread replies are PM, never Alerts (Rajiv 2026-10-20)
Source: C0ALZJHGE49 thread 1792510362.837119, ts 1792510805.167479: "note that the replies has to be posted by pm, not alerts. alerts just post the top level thread which the pm processes."
- `SLACK_SEND_AS=alerts` is ONLY for the top-level report post.
- Every reply/disposition IN the thread is posted as PM: plain `bash /Users/rajiv/.claude/scripts/slack-send.sh -c C0ALZJHGE49 -t <thread_ts> -f < file` (no SLACK_SEND_AS).
- A bg agent running this skill returns its disposition text to PM; PM (or the agent as PM identity) posts the thread reply the SAME turn.

## Unblockable now (Rajiv 2026-10-25, C0ALZJHGE49 thread 1792910728.369929, ts 1792910836.320109)
Rajiv: "why are highlighting stale MoP labels in hourly review? we need to unblock the dependencies, not fix the labels."

The 1h composer (`heartbeat-compose.py`) renders `*Unblockable now:*` itself: open issues in status:backlog, status:blocked or pm-blocked:* whose cited blocker (a #N in the body's `blockers:` / `Blocked on` / `unblock_condition` line) is now CLOSED or MERGED (a closed-unmerged PR does not count). One line each: issue, priority, blocker, resolved date; top 10 by priority, "+N more" beyond. Read-only; no label commentary in the report.
