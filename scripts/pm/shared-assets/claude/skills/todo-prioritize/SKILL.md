---
name: todo-prioritize
description: |
  Fetch all open GitHub issues with `status:todo` from heydonna-app, score by P0/P1/P2/P3,
  apply current Ready Pool routing rules + live MoP availability, and produce a CONCISE
  one-line-per-issue plan. In Rajiv-facing mode, post the plan to #heydonna-dev for
  approval before updating PM ops Ready Pool obligations and rendering pm-todo.md.
  In hourly-ops-audit mode, PM may update
  ~/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db
  directly as a routine queue reconciliation, with proof, but must not dispatch work or
  change issue priority labels without pm-triage.
  Use when: PM needs a fresh view of the backlog with slot assignments, after a merge
  train clears the queue, when Rajiv asks "what's next?", or when hourly ops audit emits
  a reprioritization action. NOT for: dispatching handoffs (use Skill(direct-assign)
  after the queue is current), filing new issues (use /explore-issue), checking
  PRs (use /check-slot), or changing priority labels/frontmatter (use pm-triage).
version: 2.3.0
date: 2026-06-12
---

# Todo Prioritize

Single source of truth for the PM dispatch queue is the PM ops SQLite ledger at `~/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db`. This skill refreshes Ready Pool obligations and then renders `pm-todo.md` so `Skill(direct-assign)` can dispatch work in current priority order. In Rajiv-facing mode it posts a concise plan and waits for Rajiv approval before updating the ledger. In hourly-ops-audit mode, the audit action itself is PM authority to rewrite the queue projection when objective drift criteria are met.

## When to Use

- Backlog needs a fresh prioritised view with slot assignments.
- A merge train cleared the queue and Rajiv asks "what's next?".
- Start of a workday or post-compact session.
- Whenever the next-dispatch decision is non-trivial (4+ open todos, mixed priorities).
- Hourly ops audit emits `ACTION: run todo-prioritize...` because Ready Pool order is stale, a burst of new `status:todo` issues appeared, P0/P1 work is unqueued, newly unblocked work surfaced, or `pm-todo.md` no longer matches GitHub/MoP state.

## Mode Selection

Choose exactly one mode at the start:

- **Rajiv-facing planning mode:** user/Rajiv asks "what's next?", PM wants approval, or the change would reorder product priorities in a way Rajiv should see. Post to Slack, wait for approval, then update PM ops obligations and render `pm-todo.md`.
- **Hourly-ops-audit mode:** invoked from an hourly ops audit action item whose evidence names stale Ready Pool order, unqueued `status:todo`, new P0/P1, newly unblocked work, stale PM ops/rendered `pm-todo.md`, or issue-count burst. Do not post for Rajiv approval by default; update PM ops obligations directly and reply to the ops audit with proof.

Hourly-ops-audit mode is allowed to reorder the queue projection and add missing queued PM ops `ready_pool` obligations. It is NOT allowed to change GitHub priority labels, rewrite Ready Pool frontmatter priority values, resolve product-scope conflicts, or dispatch work directly. If those are required, emit a blocked proof pointing to `pm-triage`.

## Slot Affinity Rule (current sweep/claim contract)

All four slots are eligible claimants for clean Ready Pool work. Slot roles are soft affinity, not hard gates:

| Slot | Soft affinity |
|------|---------------|
| slot 1 Rohini | Features |
| slot 2 Hasta | Bug-fix affinity; features if Rohini busy |
| slot 3 Ashwini | Lead bug fixes |
| slot 4 Chitra | Full-stack affinity + manual testing |

Rules:

- No slot is standby by role. A free slot should not remain idle while clean `status:todo` work exists.
- Rank work by priority, release/customer/legal-output risk, unblocked age, and stale-queue impact.
- Use affinity only to break ties or choose among equally good free slots.
- Merge-ready / pending-Rajiv PRs do not consume slots.
- Do not call `/handoff` from this skill. Dispatch happens only through `Skill(direct-assign)` after the queue projection is current.

## When NOT to Use

- Dispatching handoffs — after the queue is current, dispatch only through `Skill(direct-assign)` when appropriate.
- Filing new issues — use `/explore-issue`.
- Checking in-flight PRs — use `/check-slot` or `/heartbeat-tasks`.
- Single-issue triage — use `pm-triage` for labels/metadata or `Skill(direct-assign)` for dispatch.

## Output Format (Rajiv directive 2026-05-04 10:57 IST — Block Kit table)

The Slack post is a **markdown table** that `slack-send.sh` auto-converts to a native Slack **Block Kit `type: table` block** (verified working shape via chat.postMessage probe 2026-05-03 ts=1777795366.758379). The conversion is automatic when stdin is piped through `slack-send.sh -f`.

Render the plan as a single markdown table with columns: **P** | **Issue** | **Slug** | **Effort** | **Slot** | **Reason**.

Example markdown rendered (slack-send.sh converts to Block Kit table):

```
| P  | Issue  | Slug                                                     | Effort | Slot           | Reason                  |
|----|--------|----------------------------------------------------------|--------|----------------|-------------------------|
| P0 | #3413  | bug(editor): 1-2s typing delay on 170+ min transcript    | L      | slot 1 Rohini  | queue-depth balance     |
| P1 | #3897  | feat(scribie): integration metadata wiring               | M      | slot 3 Ashwini | queue-depth balance     |
| P1 | #3898  | fix(convex): owner loses canEdit with viewer-shares      | S      | queued         | bug affinity            |
| P2 | #3790  | fix(editor): RulerBar tick origin                        | ?      | queued         | affinity tie-break      |
```

The kanban file (`~/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/memory/pm-todo.md`) keeps the one-line-per-issue format described in Step 9 — kanban is plain markdown for git-friendly diffs, Slack post is Block Kit table for visual scanning.

No weekly framing. No "this week / next week" language. Slug truncated to ≤60 chars. Reason ≤8 words.

**Pre-flight verification:** before posting, run `SLACK_SEND_SELFTEST=1 bash ~/.claude/skills/slack-message/scripts/slack-send.sh </dev/null 2>&1 | grep "TBL-1 default emits top-level type=table"` — must show `OK`. If the shape regresses (Slack ever rejects `type: table` block again), slack-send.sh falls back to the `convert_table_to_codeblock` path automatically.

## Process

### Step 0: Capture trigger thread (Rajiv directive 2026-05-02 19:08 IST)

When Rajiv invokes this skill from a Slack thread, his trigger message is delivered to PM via the slack-bridge with a header line of the form:

```
# slack-channel C0ALZJHGE49 in thread 1777728703.933709 | Rajiv | 07:01 PM
```

Capture the channel + thread_ts so the plan post (Step 7) replies to that thread instead of posting top-level.

```bash
# Scan recent PM transcript for the most recent slack-bridge trigger header.
# The header is injected by the UserPromptSubmit hook into the prompt context.
# Find the most recent matching line in the current session's prompt context.

TRIGGER_CHANNEL=""
TRIGGER_THREAD_TS=""

# Extract from the current prompt / recent UserPromptSubmit input.
# Pattern: `# slack-channel <CHAN_ID> in thread <THREAD_TS>`
# CHAN_ID = C[A-Z0-9]+ ; THREAD_TS = digits with optional decimal

# When invoking the skill, the PM agent reads its current prompt context and runs:
#   grep -oE '# slack-channel (C[A-Z0-9]+) in thread ([0-9]+\.[0-9]+)' <prompt-source> | tail -1
# then parses the two captures.

# If no match found, leave both empty -> top-level post fallback.
```

The PM agent should look at the originating user message in this conversation turn for the `# slack-channel <CHAN> in thread <TS>` header, and if present:
- `TRIGGER_CHANNEL` ← the captured `C[A-Z0-9]+` (typically `C0ALZJHGE49`)
- `TRIGGER_THREAD_TS` ← the captured `[0-9]+\.[0-9]+`

If absent (skill triggered manually, no Slack origin), proceed with `TRIGGER_CHANNEL=C0ALZJHGE49` and no thread (top-level post).

Idempotency: re-running on the same triggering message captures the same thread_ts → re-post lands in the same thread.

### Step 1: Fetch backlog

```bash
cd /Users/rajiv/Downloads/projects/heydonna-app
gh issue list --label "status:todo" --state open \
  --json number,title,labels,body,createdAt \
  --limit 50 > /tmp/backlog-todo.json
```

If 0 issues → post `*Backlog empty.*` to `#heydonna-dev` and exit.

### Step 2: Read MoP slot state

`mop_all_slots()` for slots 1–4. Capture: idle, dnd, task, issue, branch.

Also read the current queue projection and PM ops ledger status:

```bash
PM_TODO=~/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/memory/pm-todo.md
test -f "$PM_TODO" && sed -n '1,220p' "$PM_TODO" > /tmp/current-pm-todo.txt
python3 /Users/rajiv/.claude/scripts/pm-ops.py status --format json > /tmp/current-pm-ops-status.json
```

### Step 3: Score each issue

**Priority** — extract from labels. `P0 / P1 / P2 / P3`. No P-label → default `P2`.

**Domain** — from title prefix `<type>(<scope>):` or labels. (`editor`, `pipeline`, `convex`, `modal`, `formatter`, etc.)

**Risk** — `HIGH / MEDIUM / LOW`:
- HIGH: schema change, multi-file, editor core, DOCX export, auth, migration.
- LOW: cosmetic, single-line, rename, doc, log field.
- Otherwise MEDIUM.

(Risk is used to break ties on slot assignment; it does NOT appear in the one-line output unless HIGH.)

**Effort** — extract from labels. `effort:S|M|L|XL`. No effort label → mark as `effort:UNKNOWN` and treat as M-equivalent for tie-breaking.
- S = ≤2 hours, single-file, narrow
- M = ½ day, 2-4 files, moderate
- L = 1 day, 5-10 files, broad scope
- XL = ≥2 days, multi-component, plan-heavy

### Step 4: Ready Pool order + soft-affinity target

Sort by:

1. Priority: P0 -> P1 -> P2 -> P3.
2. Release/customer/legal-output risk: public-beta, legal-output correctness, transcript/export corruption, production incident, customer report.
3. Unblocked age: newly unblocked or oldest unassigned/unqueued `status:todo` first.
4. Rework before new work at the same priority when the PR is blocked on correction.
5. Effort only as a load-balancing tie-breaker, not as a reason to park work.

Effort weights for tie-breaking: S=1, M=3, L=8, XL=20, UNKNOWN=3.

For each issue, choose a **preferred claimant**:

- If one or more slots are free and not DND, pick the free slot whose soft affinity best matches the issue.
- If no affinity stands out, pick the free slot with lowest current queue weight.
- If all slots are occupied, write `queued` with a preferred affinity note, not a fake standby state.
- DND slots are off-limits until DND clears.
- Existing `slot:N` labels mean the issue is already assigned; keep it in `Now`, not `Queued`.

This preferred claimant is advisory. `Skill(direct-assign)` is the dispatch authority and may choose another compatible free slot.

### Step 5: Detect blockers

Mark blocked if any of:
- Title/body says "blocked on #NNNN" and that PR is OPEN.
- Body says "awaiting Rajiv decision" / "needs scope clarification".
- `status:blocked` label is present.

Blocked items go in a separate `*Blocked*` section, NOT in the priority list.

### Step 6: Render the plan

Render as a single markdown table (slack-send.sh auto-converts to native Block Kit `type: table` block). Sort rows by priority (P0 → P3), and within each priority by queue position. Blocked items go in a separate small bullet list AFTER the table — Block Kit tables don't render conditionally, so blocked items + the assignment summary live below the table as plain mrkdwn.

```
*Backlog Plan* — <YYYY-MM-DD HH:MM IST> — <N> open todos

| P  | Issue  | Slug                                                     | Effort | Slot           | Reason                |
|----|--------|----------------------------------------------------------|--------|----------------|-----------------------|
| P0 | #NNNN  | <slug ≤60 chars>                                         | L      | slot 1 Rohini  | queue-depth balance   |
| P1 | #NNNN  | <slug>                                                   | M      | slot 3 Ashwini | queue-depth balance   |
| P1 | #NNNN  | <slug>                                                   | S      | queued         | behind #NNNN          |
| P2 | #NNNN  | <slug>                                                   | ?      | queued         | affinity tie-break    |

*Blocked*
• #NNNN | <slug> | blocker: PR #XXXX still open

*Assignment plan* (all slots eligible; affinity is advisory)
• slot 1 Rohini → #NNNN next if free; queued: #MMMM
• slot 2 Hasta → #PPPP next if free
• slot 3 Ashwini → occupied on #AAAA; next bug affinity: #BBBB
• slot 4 Chitra → free; sweep should claim #CCCC unless blocked
```

Slack post does NOT include a "Rajiv merge queue" section — Rajiv reads merge queue separately via `gh pr list`. Adding it here is noise.

The "Assignment plan" section is a queue projection, not a direct handoff command. It can name next preferred claimants, but dispatch still goes only through `Skill(direct-assign)`.

Risk only included if HIGH (append `· HIGH` to the Reason cell).

**Slot column format:** `slot N Name` (no parens — keeps cell narrow for table rendering) when there is a preferred free claimant. Use `queued` when all compatible slots are occupied, `backlog` only for non-dispatch-ready parked work, and `blocked` only in the separate blocked section.

**Issue cell:** `#NNNN` only — no link wrapping (Slack auto-detects issue numbers in the heydonna-app channel context; wrapping breaks Block Kit table cell shape).

### Step 7: Publish or retain the plan, depending on mode

#### Rajiv-facing planning mode

Use the channel + thread_ts captured in Step 0. If triggered from a Slack thread, post as a reply to that thread; otherwise post top-level.

```bash
# TRIGGER_CHANNEL and TRIGGER_THREAD_TS are set in Step 0.
# Default channel if Step 0 found nothing: #heydonna-dev (C0ALZJHGE49).

CHAN="${TRIGGER_CHANNEL:-C0ALZJHGE49}"

if [ -n "$TRIGGER_THREAD_TS" ]; then
  cat /tmp/backlog-plan.txt | bash ~/.claude/skills/slack-message/scripts/slack-send.sh -c "$CHAN" -t "$TRIGGER_THREAD_TS" -f
else
  cat /tmp/backlog-plan.txt | bash ~/.claude/skills/slack-message/scripts/slack-send.sh -c "$CHAN" -f
fi
```

Must show `OK ts=... channel=<CHAN>`. When threading, the response also includes the parent `thread_ts`.

Why thread-reply: Rajiv directive 2026-05-02 19:08 IST — *"update the prioritise todo skill to reply to the correct thread id where it was triggered from"*. Plan posts must thread to the originating conversation so Rajiv's approval reply lands in context. (See `feedback_todo_prioritize_thread_reply.md`.)

#### Hourly-ops-audit mode

Do not post to Slack by default. Save the generated plan to `/tmp/todo-prioritize-hourly-ops-plan.md` and continue to Step 9. The ops-audit proof line must cite:

- trigger action/evidence from the hourly audit;
- changed issue numbers or `no_order_change`;
- `pm-todo.md` row/section proof;
- whether `Skill(direct-assign)` dispatched after the write.

### Step 8: Approval gate

In Rajiv-facing planning mode: do NOT write `pm-todo.md` until Rajiv approves the whole plan or specific items. After approval, proceed to Step 9.

In hourly-ops-audit mode: the audit action is PM authority for routine queue reconciliation. Proceed to Step 9 without Rajiv approval only if all changes are queue projection changes: order, missing queued obligations, stale resolved obligations, preferred claimant notes, or PM ops/rendered `pm-todo.md` freshness. If priority labels/frontmatter values, product scope, or release-gate classification must change, STOP and return:

`BLOCKED proof: needs pm-triage for priority/frontmatter change; no PM ops queue rewrite`

### Step 9: Update PM ops queue obligations and render `pm-todo.md`

After approval in Rajiv-facing mode, or immediately in hourly-ops-audit mode when the change is routine queue reconciliation, the approved/current plan becomes PM ops Ready Pool state. `Skill(direct-assign)` consumes this projection alongside live GitHub/MoP state. `pm-todo.md` is generated from PM ops; direct dispatch still goes only through `Skill(direct-assign)`.

The kanban file DOES include the "Rajiv merge queue" section (PM uses it internally to track in-review PRs awaiting Rajiv's merge). This section is intentionally absent from the Slack post in Step 6 — Rajiv reads merge queue separately via `gh pr list`.

For each queued issue, upsert a PM ops `ready_pool` obligation, then render:

```bash
python3 /Users/rajiv/.claude/scripts/pm-ops.py obligation-upsert \
  --kind ready_pool --target-type issue --target-id <NNNN> --issue <NNNN> \
  --owner pm --title "#<NNNN> <slug>" \
  --action "Keep queued in Ready Pool until Skill(direct-assign) assigns or PM blocks it." \
  --evidence "priority=<P0|P1|P2|P3>" --evidence "lane=<lane>" --evidence "preferred_slot=<slot|queued>"

python3 /Users/rajiv/.claude/scripts/pm-ops.py sync --write --no-live --reason todo-prioritize
```

The rendered `pm-todo.md` should have this structure:

```markdown
# PM Kanban — <YYYY-MM-DD HH:MM IST>

## Now (assigned, in-flight)
- *slot 1 (Rohini)* — #NNNN <slug> [branch: feat/...] [PR #PPPP if exists]
- *slot 2 (Hasta)* — #NNNN <slug>
- *slot 3 (Ashwini)* — #NNNN <slug>
- *slot 4 (Chitra)* — #NNNN <slug>  (or "free")

## Queued (Ready Pool projection, waiting for sweep/claim)
### P0
- #NNNN | <slug> | → preferred slot N (Name) | reason

### P1
- #NNNN | <slug> | → preferred slot N (Name) | reason
- #NNNN | <slug> | → queued; affinity <feature|bug|infra|investigation>

### P2
- #NNNN | <slug> | → queued; affinity <feature|bug|infra|investigation>

### P3
- #NNNN | <slug> | → backlog only if intentionally parked with metadata

## Blocked
- #NNNN | <slug> | blocker: PR #XXXX still open

## Rajiv merge queue
- PR #PPPP — #NNNN <slug> — `status:in-review`, CI+E2E green, Codex VERIFIED

## Recently merged (last 24h)
- PR #PPPP — #NNNN <slug> — merged YYYY-MM-DD HH:MM IST
```

Render conventions:
- Update timestamp on every regeneration.
- Each issue appears in EXACTLY ONE section (Now, Queued, Blocked, merge queue, Recently merged).
- When `cleanup-pr` finishes, it resolves/updates the PM ops obligation and sync renders "Recently merged".
- When `Skill(direct-assign)` dispatches, it records the slot claim and resolves the queue/routing obligation so sync renders "Now" under the slot.

In hourly-ops-audit mode, after writing `pm-todo.md`, perform no sweep and dispatch nothing from this skill; individual dispatch happens only through `Skill(direct-assign)`. If dispatch is blocked, keep the blocker open in the ops-audit proof instead of claiming the queue is healthy.

### Step 10: Confirm to Rajiv

Reply in the Slack thread:
```
Plan written to PM ops ledger and rendered to ~/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/memory/pm-todo.md. <K> assignments active. <M> queued.
PM dispatches the next clean Ready Pool item only through `Skill(direct-assign)`.
```

In hourly-ops-audit mode, do not send this Slack confirmation unless the audit explicitly requested a Slack post. Instead, return an ops-audit proof line:

`[ACTION_N_REPRIORITIZE] EXECUTED proof: todo-prioritize hourly-ops mode; pm-todo-row:<line>; changed=<issues|no_order_change>; dispatch=<ASSIGNED|QUEUED|BLOCKED|IDLE>`

## Output Reliability

- In Rajiv-facing mode, the plan is a recommendation until Rajiv approves.
- In hourly-ops-audit mode, routine queue reconciliation may update PM ops obligations and render `pm-todo.md` directly, but priority label/frontmatter changes require `pm-triage`.
- Auto-dispatch is forbidden from this skill. Dispatch happens only through `Skill(direct-assign)`.
- The PM ops ledger is the PM queue authority; rendered `pm-todo.md` is the queue projection for sweep/claim — keep both synced.
- Idempotent — re-running on an unchanged backlog produces an identical plan and an identical kanban.
- If Rajiv revises specific assignments, regenerate the kanban with his revisions baked in before sweep/claim reads it.

## Common Pitfalls

- **Don't auto-dispatch.** This skill updates queue projection only; sweep/claim dispatches.
- **Don't bypass DND.** DND slots are off-limits.
- **Don't keep the old "weekly plan" framing** — that's been removed (Rajiv directive 2026-04-30 11:01). Just priority + issue + slug.
- **Don't truncate too aggressively** — 60 chars on the slug, not 30. Rajiv needs to recognize the issue at a glance.
- **Don't leave PM ops/rendered kanban out of sync** with `gh issue list status:todo`. If a new issue arrives mid-day with P0/P1, regenerate.
- **Don't resurrect role-based standby.** All slots are eligible; affinity is advisory.
