---
name: heartbeat-tasks
description: |
  3h Sakshi heartbeat, triggered by MoP every 3 hours. PM launches one
  background agent. The agent composes a product-signal post (Axiom errors,
  support inbox, open PRs), posts it as one new top-level message in
  #heydonna-dev, and runs the remaining housekeeping silently.
  Use when: MoP asks PM to invoke `Skill(heartbeat-tasks)`, or a heartbeat is
  requested manually.
  NOT for: the 1h post (use hourly-heartbeat), morning briefs (morning-brief),
  one-off status checks (pm-status).
author: Claude Code PM
version: 3.0.0
date: 2026-09-29
---

# 3h Heartbeat (product-signal report)

Spec: `docs/pm/heartbeat-redesign-2026-09-29.md`. Rajiv, thread
`C0ALZJHGE49/1790659381.537339`: *"we need to redo the 3h hearbeat report ...
it should focus on axiom errors, support requests and open prs."* Approved
2026-09-29 10:58 IST: *"build it. note that it has to have an cto at mention."*
So the 3h post ALWAYS starts with `<@U0BNFGX2UAX>`. (The 1h post has different
mention rules; see `hourly-heartbeat`.)

The original 910-line v2.2.1 skill is backed up next to the live target as
`SKILL.md.bak-2026-09-29`.

## Runtime contract

1. MoP `PMCadenceScheduler` owns the cadence (task `heartbeat`, 3h buckets
   `YYYY-MM-DD:N`) and queues a normal PM prompt to invoke
   `Skill(heartbeat-tasks)`. It never injects `/heartbeat-tasks`.
2. PM launches ONE background agent (`run_in_background=true`) with the agent
   prompt below, then returns to normal event processing. Do not run it inline.
3. The agent posts. PM acts on the agent's returned `ACTIONS:` list (it cannot
   launch nested agents itself).

## Agent prompt

```
You are the 3h heartbeat agent. Work from /Users/rajiv/Downloads/projects/heydonna-app (main).

STEP 0 — dependency check. The composer and collectors are MoP shared assets
  installed to /Users/rajiv/.claude/scripts/pm/heartbeat/. If
  /Users/rajiv/.claude/scripts/pm/heartbeat/heartbeat-compose.py
  is absent, or STEP 1 exits 3 with HEARTBEAT_COLLECTORS_ABSENT, fail soft:
  skip STEPS 1-3, still run STEP 4 housekeeping, and return exactly
  "HEARTBEAT_3H_SKIPPED: collectors absent (install the MoP release shipping pm/heartbeat)".
  Do not hand-compose a post and do not treat it as an error.

STEP 1 — compose (prints the post; never posts):
  bash /Users/rajiv/.claude/scripts/sakshi-heartbeat.sh compose --mode 3h \
    --save-inputs /tmp/heartbeat-3h-latest > /tmp/heartbeat-3h-post.txt
  Inputs land in /tmp/heartbeat-3h-latest/{axiom,map,support,prs}.json.

STEP 2 — sanity check before posting:
  - first line starts with "<@U0BNFGX2UAX> *Heartbeat"
  - at most ~20 lines, no tables, no housekeeping lines
  Do not hand-edit block content. If a block says "unavailable", keep it; the
  _Ops:_ footer already names the failure.

STEP 3 — post ONE new top-level message in #heydonna-dev (no -t):
  bash /Users/rajiv/.claude/scripts/slack-send.sh -c C0ALZJHGE49 -f < /tmp/heartbeat-3h-post.txt
  Record the `OK ts=...` readback. Never post to a DM or an old thread.

STEP 4 — silent housekeeping (see "Housekeeping" below). Output nothing unless
  a job failed or needs a human; then post ONE thread reply under the STEP 3
  message: "_Ops:_ <job> failed — <one line>".

STEP 5 — return exactly:
  HEARTBEAT_POSTED ts=<ts> lines=<n>
  ACTIONS:
  - investigate <code> (NEW)              # one per NEW error code
  - reply-needed <channel> <thread_ts> <age>m   # one per overdue support thread
  - repair PR#<n>@<head> <ci|e2e>         # one per red PR marked unowned
  (or "ACTIONS: none")
```

## PM follow-through on the returned ACTIONS

- `investigate <code>` → launch `customer-artifact-investigator` (read-only,
  background) with the code, window and `/tmp/heartbeat-3h-latest/axiom.json`.
  When the code is not user-visible, file a pm-ops obligation instead.
- `reply-needed` → answer the customer in that thread (customer notes go to the
  customer's own thread, never an internal alert thread).
- `repair PR` → CI red: `ci-repair-agent`; E2E red: `e2e-failure-investigator`
  (rule 14). The hourly drive normally already owns these; do not duplicate a
  live repair.
- Merge asks are carried by the post's `*Asks:*` line; do not send a second ask.

## Blocks (what the composer does)

Scripts are MoP-installed at `~/.claude/scripts/pm/heartbeat/` (Axiom queries in `heartbeat-axiom.py`), all read-only:

| Block | Script |
|---|---|
| Axiom errors | `axiom-activity-report.py --hours 3 --errors-by-code --compare` (codes, distinct users, prior 3h, 7-day same-slot median) |
| Code → issue | `heartbeat-error-map.py` (open GitHub issues/PRs, open pm-ops rows, `docs/investigations/`); unmapped = `NEW` |
| Support | `support-inbox-snapshot.py` (C0A56RX6FNW + C0AGWPQFKHA via the PM bot token; in-app `feedback` via `npx convex data feedback --prod`, PM-only per rule 11c) |
| Open PRs | `pr-open-snapshot.py` (live `gh` checks on `headRefOid`; never pm-ops rows or pm-state labels) |

Rules the composer enforces:
- One-sentence TL;DR on line 1, after the CTO mention.
- Empty block → the word "quiet". Top 5 codes, then "+N other codes".
- Support: needs-reply flagged ⚠ at ≥60 min in IST business hours
  (Mon–Sat 09–20), ≥180 min otherwise; answered threads collapse to a count
  above 3. A customer's closing "thanks/ok/it worked" does not reopen a thread.
- PRs: red first, then merge-ready, capped at 8 lines.
- `*Asks:*` line only when a PR is CI+E2E green on its current head (merge ask).
- `_Ops:_` footer only when a collector failed.

## Housekeeping (silent side jobs)

Run after posting. None of these produce post lines. Report only failures.

```bash
bash /Users/rajiv/.claude/scripts/sakshi-heartbeat.sh            # session-age probe → /tmp/sakshi-heartbeat.json
python3 /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/heartbeat-session-age-clear.py
python3 /Users/rajiv/.claude/scripts/backlog-triage.py --surface heartbeat --recent-hours 72
python3 /Users/rajiv/.claude/scripts/backlog-triage.py audit-ready-pool
python3 /Users/rajiv/.claude/scripts/stale-process-cleanup.py --apply
python3 /Users/rajiv/.claude/scripts/pm-ops.py snapshot --surface heartbeat --limit 12
```

Needs a human (one `_Ops:_` thread reply each):
- `READY_POOL_LOW` / `READY_POOL_CONTRACT_REPAIR_REQUIRED` → PM launches
  `pm-backlog-promoter`.
- A probe or script exit ≠ 0.

### Session-age policy (unchanged contract)

Dev slots S1-S6 are NEVER cleared on a cadence. A numbered slot's session is
cleared only at the new-issue assignment boundary, inside the atomic assignment
operation (`Skill(direct-assign)` / `mop-assign-slot`). The heartbeat must not
produce, consume, or act on a per-slot pending row, and must not invoke
`mop-clear-slot.sh` for any dev slot. `heartbeat-session-age-clear.py` is
materialize-only for numbered slots.

PM stays clear-worthy on cadence. Due PM becomes a PM pending-clear row
(`SESSION_AGE_CLEAR_PENDING PM`, PM effective session age ≥ 3h). The
`pm-self-clear-stop` hook only emits a non-blocking Stop reminder; PM finishes
housekeeping, saves any last note to pm-todo/pm-ops, then self-clears through
MoP logging with
`bash /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/mop-clear-slot.sh pm`.
Never inject raw `/clear`; never respawn as the stale-session fix.

## Dropped from the post (do not re-add)

Session-age tables and self-clear reminders, slot occupancy and queue-motion
gates, backlog-triage and Ready Pool counts, stale-process/chrome counts, PM
state-label drift, PROCESS_LIMBO lists, ledger-hygiene notes, artifact-path
proof lines. Labels are advisory; PR state comes only from live GitHub.

## Boundaries

- The agent never merges, admits, reruns CI, edits labels, or DMs Rajiv.
- Product/customer/data-model decisions go to CTO through
  `Skill(pm-escalation-ladder)`, never decided here.
- Retired helpers (`pm-transition.sh`, `pm-state-replace.sh`,
  `slot-dispatch-sweep`, ...) stay retired; their absence is not a defect.

## Proof of done

A run is done when: the post is ≤ ~20 lines, has all three blocks, starts with
the CTO mention, contains no housekeeping lines, and the `OK ts=` readback is
recorded. Target: zero CTO "heartbeat correction" replies over 4 runs.
