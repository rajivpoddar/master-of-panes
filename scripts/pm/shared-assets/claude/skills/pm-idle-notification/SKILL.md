---
name: pm-idle-notification
description: |
  PM decision matrix for handling slot idle notifications. Determines
  notification type from context (pane state, PR existence, QA reports)
  and takes the appropriate action. Covers: DND, post-implementation,
  post-QA, post-PR, post-Claude's Corner, and free/unassigned slots.
  Use when: `/slot-idle N` command is triggered in PM pane (sent by slot-idle-notify.sh hook).
  NOT for: Scheduled heartbeats (use heartbeat-tasks), manual status
  checks (use check-slot), or plan approvals (use pm-plan-approval).
author: Claude Code PM
version: 2.0.0
date: 2026-03-05
---

# PM Idle Notification Handler

## PM State Label Transitions (Rajiv directive 2026-05-20 11:48 IST thread `1779250700.624619`)

`pm-state:*` labels are the canonical PR state surface. After processing each idle notification, transition the PR's pm-state via `~/.claude/scripts/pm-state-replace.sh <PR> <suffix>`. The helper enforces mutual exclusion (exactly ONE active `pm-state:*` per PR).

When this skill flips a PR's status based on idle context, apply the matching label transition:

| Idle context | New pm-state | Companion label |
|---|---|---|
| Slot just pushed draft PR + QA brief not yet written | `draft-qa-needed` | — |
| Slot is running qa-tester subagent | `qa-running` | — |
| Fresh QA report posted + PASS verdict, no post-review rework | `qa-passed-awaiting-ci` | (`pr-mark-ready` Phase A applies this) |
| Rework QA passed, PM Claude Phase A review in flight | `pm-review-pending` | set via `pm-transition pm-review --pr <PR> --scope phase-a` |
| QA report posted + FAIL verdict, slot starting rework | `qa-failed-rework` | — |
| Slot blocked on CI red / Codex P0 / rebase conflict | `blocked-rework` | add `pm-blocked:ci` / `pm-blocked:codex` / `pm-blocked:rebase` |

Example: `~/.claude/scripts/pm-state-replace.sh 4738 qa-running`. Log written to `/tmp/pm-state-transitions.log`. Companion `pm-blocked:*` labels are additive (use `gh pr edit --add-label`); they coexist with `pm-state:blocked-rework`.

## When NOT to Use

- Scheduled 3h heartbeat — use `heartbeat-tasks`
- Plan approval — use `pm-plan-approval`
- Manual slot check — use `check-slot` or `monitor-slot`
- Sending work to a slot — use `pm-handoff` / `pane-handoff`
- Marking a PR ready for Rajiv's manual merge — use `pr-mark-ready` (replaces deprecated `merge-pr`)

## Trigger

`/slot-idle N` slash command arrives in PM pane, sent by each slot's `slot-idle-notify.sh`
Stop hook. The hook also prepends a `#` comment with context:

```
# slot 2 idle — #1621: fix(tests): broken tests | branch: fix/1621-tests | 12:26:30
/slot-idle 2
```

The `/slot-idle` command file (`~/.claude/commands/slot-idle.md`) mandates creating 5 subtasks
via TaskCreate before taking any action. This prevents the PM from taking shortcuts.

Legacy format (pre-2026-03-05, no longer sent by hooks):
```
[slot 2 idle — #1621: fix(tests): broken tests | branch: fix/1621-tests] [12:26:30]
[slot 3 idle — unknown] [12:10:54]
[slot 4 idle — unknown | branch: fix/1612-layout-template] [12:05:03]
```

## ⛔ MANDATORY: Create Task List BEFORE Any Action

**The shortcut is to skip TaskCreate and process inline — classify the slot, take
action, move on. This shortcut ALWAYS leads to dropped actions.** In 8+ hour sessions,
PM reliably collapses the 6-step process to "quick classify → act" without tasks.
The result: forgotten QA briefs, skipped label updates, missing Rajiv notifications.
(Alignment research: inoculation prompting reduces this ~9x.)

**ENFORCEMENT: The very first tool call after receiving `/slot-idle N` MUST be
TaskCreate.** If your first tool call is Bash, Read, ToolSearch, or anything else,
you are violating this rule. Stop and create the task first.

```
[slot N idle] arrives
    ↓
1. TaskCreate("Slot N idle: capture + classify") ← FIRST TOOL CALL, NO EXCEPTIONS
    ↓
2. Check DND → if true → complete task as "DND, skipping" → STOP
    ↓
3. Determine notification type (see context checks below)
    ↓
4. Create remaining tasks for the detected type
    ↓
5. Execute tasks with CHECKPOINTS, marking each in_progress → completed
```

### Checkpoints (Verify Each Action Landed)

After each action, verify it actually worked. Don't assume success:

| Action | Checkpoint |
|--------|-----------|
| Send command to slot | `tmux capture-pane -t 0:0.N -p -S -3` — verify command appears |
| Release slot | `curl -s localhost:3100/slots/N` — verify status is free |
| Update GitHub labels | `gh issue view ISSUE --json labels` — verify label applied |
| DM Rajiv | Check curl response for `"ok":true` |
| Send /review-and-pr | Wait 5s, capture pane, verify slot started PR creation |
| POST-PR QA assignment | If diff touches `modal/`, `convex/`, or pipeline code → verify QA brief sent back to implementing slot. Rajiv directive: "all Modal changes must be QA'ed for regression." |

If a checkpoint fails, retry once. If retry fails, escalate to Rajiv.

(Lesson: send-to-slot approvals silently dropped 3 times in one session. Without
checkpoints, PM assumed the slot received the message and moved on.)

## Decision Matrix

When an idle notification arrives, the PM determines the context and acts.
The task list above MUST already exist before reaching this point.

**Every idle notification MUST produce a task list before taking action.** This prevents
dropped actions when multiple slots go idle simultaneously or the PM is mid-task.

After determining the notification type, immediately create tasks:

```
TaskCreate: "Process slot N idle — [TYPE]"
  description: "[specific actions from the type's checklist below]"
  activeForm: "Processing slot N idle notification"
```

**Examples by type:**

| Type | Tasks to Create |
|------|----------------|
| POST-IMPL | 1. "Send /review-and-pr to slot N" 2. "Note QA decision for POST-PR" |
| POST-QA | 1. "Read QA report for #ISSUE" 2. "DM screenshots + summary to Rajiv" 3. "Wait for Rajiv approval" |
| POST-PR | 1. "Update issue #ISSUE to status:in-review" 2. "Release slot N (mop_release_slot)" 3. "QA decision: generate brief for same slot OR notify Rajiv (no QA)" 4. "DM Rajiv — PR #NNN ready" |
| POST-MERGE | 1. "Release slot N (mop_release_slot)" 2. "Sync slot N to main" 3. "Send Claude's Corner to slot N" 4. "Notify Rajiv merge complete" |
| POST-CC (Claude's Corner) | Fall through to FREE SLOT — CC report processing moved to heartbeat only |
| SELF-DEFER | 1. "Verify assigned work is unfinished and has no external blocker" 2. "Create same-turn continuation obligation" 3. "Send exact foreground continuation action to slot N" |

**Mark tasks completed as you go.** Never move to the next idle notification until all
tasks from the current one are done.

(Lesson: 2026-03-02 — PM processed idle notifications reactively without tracking.
When slots 2 and 4 went idle within seconds of each other, the PM sent Claude's Corner
to both but never collected the outputs when they came back idle. Task lists prevent this.)

### Decision Tree

```
[slot N idle] arrives
    ↓
1. Check DND → if true → IGNORE (stop, do nothing)
    ↓
2. Check for invalid self-deferral → if true → SELF-DEFER
    ↓
3. Check if slot is OCCUPIED (has assigned task)
    ↓ YES                          ↓ NO
    ├── PR exists? ──→ POST-PR     └── Claude's Corner report exists? ──→ COLLECT
    ├── QA report exists? ──→ POST-QA                                    ↓ NO
    └── Otherwise ──→ POST-IMPL                                     IDLE (wait for work)
```

### Pre-Check: Invalid Self-Deferral?

Before normal POST-IMPL classification, check whether an occupied slot deferred
unfinished assigned work to a "next session", "later", or equivalent boundary.
This is a PM decision, not a Stop-hook transcript heuristic.

Classify as SELF-DEFER only when all are true:

- MoP still assigns the slot to the same PR/issue/branch.
- The current handoff or PR proves assigned work remains unfinished.
- There is no concrete external blocker that requires PM, Rajiv, CI, or a dependency.
- The slot has ended the turn instead of running the next foreground action.

When those conditions are proven, create a durable obligation immediately before
sending the corrective instruction. Use the slot checkout's current branch and
head so a later assignment or head change cannot inherit the obligation:

```bash
SLOT=N
PR=NNNN
ISSUE=NNNN
CHECKOUT="/Users/rajiv/Downloads/projects/heydonna-app-300${SLOT}"
BRANCH=$(git -C "$CHECKOUT" branch --show-current)
HEAD=$(git -C "$CHECKOUT" rev-parse HEAD)

OBLIGATION_ID=$(python3 /Users/rajiv/.claude/scripts/pm-ops.py obligation-upsert \
  --kind slot_same_turn_continuation \
  --severity high \
  --target-type slot \
  --target-id "$SLOT" \
  --slot "$SLOT" \
  --pr "$PR" \
  --issue "$ISSUE" \
  --owner slot \
  --horizon hourly \
  --title "Slot $SLOT must continue assigned work in this session" \
  --action "Run the assigned foreground implementation/review action now; do not stop after another PM status message." \
  --blocker invalid_self_deferral \
  --dedupe-group "slot_same_turn_continuation:${SLOT}:${PR}:${HEAD}" \
  --evidence "branch=$BRANCH" \
  --evidence "head_sha=$HEAD" \
  --evidence "source=pm-idle-notification" \
  --print-id)

bash ~/.claude/skills/message-slot/scripts/message-slot.sh "$SLOT" \
  "PM continuation obligation $OBLIGATION_ID: continue the assigned work now using the required foreground agent/tool. Do not send another status-only message. If a concrete external blocker exists, send ESCALATION with proof." \
  --force
```

The dev-slot Stop hook enforces this obligation. A PM message, MoP status call,
task-list update, or prose such as "continuing" does not satisfy it. The hook
resolves it after a meaningful foreground tool call made after the obligation
timestamp. If the slot still stops without work, the open obligation remains
visible to PM ops and hourly audit; PM may then use `fabrication-reset`.

Do not create this obligation for legitimate waits or product decisions. Do not
ask the Stop hook to decide whether a blocker is legitimate.

### Pre-Check: Plan Awaiting Approval?

**Before running the decision matrix**, check if the slot is waiting for plan approval.
If the capture mentions "awaiting approval" or "plan written", classify as PLAN-READY.

**Plan approval flow (Rajiv directive 2026-04-10):**
- PM reads the plan file from the slot's worktree
- If Codex review is present → approve: `bash ~/.claude/skills/message-slot/scripts/message-slot.sh N "Plan approved. Proceed with implementation." --force`
- If Codex review is missing → tell slot: `bash ~/.claude/skills/message-slot/scripts/message-slot.sh N "Run Codex adversarial review on the plan before proceeding." --force`
- **Never send "2"** — by the time PM processes the idle notification, the slot is no longer at the numbered prompt. Always use natural language.

### How to Determine Context

Use MoP MCP tools (not legacy pane-N.json files):

```
# 1. Read slot state via MoP MCP
mop_slot_status(slot: N)
# Returns: status, occupied, task, issue, branch, pr, dnd, last_activity, name

# 2. Extract issue number from the returned issue field (integer) or task string

# 3. Extract branch from notification
BRANCH=$(echo "$NOTIFICATION" | grep -oE 'branch: [^]]+' | sed 's/branch: //')

# 4. Check for PR on branch
PR_NUMBER=""
if [ -n "$BRANCH" ] && [ "$BRANCH" != "main" ]; then
  PR_NUMBER=$(gh pr list --head "$BRANCH" --state open --json number --jq '.[0].number' 2>/dev/null)
fi

# 5. Check for QA report
QA_REPORT=""
if [ -n "$ISSUE" ]; then
  QA_REPORT=$(ls /tmp/qa-report-${ISSUE}.md 2>/dev/null)
fi

# 6. Check for Claude's Corner reports (use name comparison against docs/claudes-corner/)
CC_REPORTS=$(for f in /tmp/claudes-corner-*.md; do BASENAME=$(basename "$f" | sed 's/claudes-corner-//'); [ ! -f "docs/claudes-corner/$BASENAME" ] && echo "$f"; done)
```

## Actions by Type

### Type 1: DND — IGNORE

**Condition:** `dnd == true`

**Action:** Do nothing. Do not send commands, do not check status, do not log.

```
# Silent return — slot belongs to Rajiv
```

### Type 2: POST-IMPLEMENTATION — Send /review-and-pr

**Condition:** `occupied == true`, no PR on branch, no QA report

This is the most common notification after a slot finishes implementing and
running tests. The PM always sends `/review-and-pr` — QA decision is deferred to POST-PR
(after the PR exists and can have a QA brief attached as a comment).

**Action:**

1. Quick diff check (informational — informs QA decision at POST-PR):
```bash
CHECKOUT="/Users/rajiv/Downloads/projects/heydonna-app-300${SLOT}"
cd "$CHECKOUT" && git diff main --stat
```

2. Note the change type for POST-PR QA decision (don't act yet):
   - UI-visible → will need QA brief at POST-PR
   - Backend-only → will ask Rajiv at POST-PR
   - Pure infra/types/refactor → no QA needed

3. Send `/review-and-pr` to the dev slot:
```bash
~/.claude/skills/tmux-slot-command/scripts/send-to-slot.sh $SLOT '/review-and-pr'
```

**Anti-pattern:** Never let the slot self-trigger PR creation. PM decides.

**Canonical PR-state on draft-PR creation (Rajiv directive 2026-05-20 11:48 IST):** after the slot creates the draft PR, the `pr-mark-ready`/`handoff` flow will set `pm-state:draft-qa-needed` via `~/.claude/scripts/pm-state-replace.sh <PR> draft-qa-needed`. If the PM finds a freshly-created draft PR without a `pm-state:*` label (e.g., slot bypassed the helper), apply it here:

```bash
PR_NUMBER=$(gh pr list --head "$BRANCH" --state open --json number --jq '.[0].number' 2>/dev/null)
if [ -n "$PR_NUMBER" ]; then
  CURRENT=$(gh pr view $PR_NUMBER --json labels --jq '[.labels[] | select(.name | startswith("pm-state:"))] | length')
  if [ "$CURRENT" = "0" ]; then
    ~/.claude/scripts/pm-state-replace.sh $PR_NUMBER draft-qa-needed
  fi
fi
```

### Type 3: POST-QA — Review QA Report (Any Slot)

**Condition:** Slot goes idle, QA report exists at `/tmp/qa-report-${ISSUE}.md`

Any slot can complete QA (self-QA after implementation). When a slot goes idle with a QA report, it means QA testing is complete.

**Action:**

1. Read the QA report:
```bash
cat /tmp/qa-report-${ISSUE}.md
```

2. If this QA pass follows a rework commit on an existing PR, do not jump
   directly to `qa-passed-awaiting-ci`. First run:

   ```bash
   ~/.claude/scripts/pm-transition.sh pm-review --pr "$PR" --scope phase-a --reason post-rework-qa-pass
   ```

   This parks the PR in `pm-state:pm-review-pending`, releases the slot, and
   records the expected PM Claude marker. Then run `Skill(pm-claude-code-review)`.
   If review passes, run:

   ```bash
   ~/.claude/scripts/pm-transition.sh pm-review-done --pr "$PR"
   ```

   That writes the current-head proof and moves the PR to
   `pm-state:qa-passed-awaiting-ci`, which triggers label-gated CI. If review
   blocks, keep/apply `pm-state:blocked-rework` plus the relevant
   `pm-blocked:*` label and send the rework back to the slot.

3. Evaluate results:

| QA Result | PM Action |
|-----------|-----------|
| **ALL PASS** | **POST-QA PASS — move to the next PM-owned gate, release slot, dispatch next (Rajiv directives 2026-05-08, 2026-05-12, 2026-05-20, revised 2026-05-29):** QA pass is the slot release event; CI is NOT a gate on slot release. For first-pass PRs, invoke `pr-mark-ready` immediately on QA pass. For post-rework PRs, run `pm-transition pm-review --pr "$PR" --scope phase-a` first, then PM Claude review, then `pm-review-done` on PASS. Do NOT pre-check or wait on CI while a slot is held.<br><br>**Step B — POST-GATE release + dispatch:** Always release + advance the kanban after `pr-mark-ready` or `pm-review` succeeds, even if CI has not started yet.<br>0. Add/update `pm-todo.md ## CI watch` or PM-review watch with PR, issue, head SHA, terminal condition, `next_action_on_pass`, `next_action_on_block`, and timestamp.<br>1. `mop_release_slot(slot: N)` unless `pm-transition pm-review` already released it.<br>2. Run `Skill(slot-dispatch-sweep)` with trigger `post-qa-pass-release`.<br>3. The sweep assigns the next highest-priority clean Ready Pool item to any free slot using soft affinity. Lead/overflow standby is retired.<br>4. CI is tracked separately after the PM review gate consumes. If CI later fails, prefer the origin slot for fresh rework only if it is free; otherwise rework returns to the Ready Pool and can be claimed by any compatible slot.<br><br>**Anti-pattern:** Slot stays occupied "waiting for PM Claude/CI green" while kanban has dispatchable work and `Actions needed: none` is reported. |
| PARTIAL PASS | Classify before escalating. Product/customer-visible behavior, data-model, roadmap, merge approval, or irreversible decisions → DM Rajiv with findings and wait. PM-owned proof/process gaps → make the call: if runtime behavior is sound but proof is incomplete, file/attach a bounded follow-up and keep readiness moving; if there is a real runtime defect or missing QA gate, keep/apply `pm-state:blocked-rework` and send specific rework to the slot. Do NOT invoke `pr-mark-ready` until the blocker class is resolved. |
| FAIL (critical) | Atomic-replace PR pm-state to `qa-failed-rework`: `~/.claude/scripts/pm-state-replace.sh $PR qa-failed-rework`. Send rework instructions to the *same dev slot* that implemented the change. Do NOT invoke `pr-mark-ready`. |
| LOW confidence | Investigate before escalating. |

**Canonical PR-state hooks (Rajiv directive 2026-05-20 11:48 IST):**
- BEFORE launching the qa-tester subagent (or evaluating QA report on slot-idle): `~/.claude/scripts/pm-state-replace.sh $PR qa-running` (flips from `draft-qa-needed`).
- ON FRESH QA PASS: `pr-mark-ready` Phase A already runs `pm-state-replace.sh $PR qa-passed-awaiting-ci` — no extra action needed here.
- ON POST-REWORK QA PASS: `pm-transition pm-review --pr $PR --scope phase-a`; after PM Claude PASS, `pm-transition pm-review-done --pr $PR`.
- ON QA FAIL: `pm-state-replace.sh $PR qa-failed-rework` (in the table row above).

3. For FAIL — send rework to the **same slot**:
```bash
# Find which dev slot owns this issue
DEV_SLOT=$(cat ~/.claude/tmux-panes/pane-*.json | python3 -c "
import json,sys,glob
for f in glob.glob('$HOME/.claude/tmux-panes/pane-*.json'):
  d = json.load(open(f))
  if '#$ISSUE' in d.get('task',''):
    print(f.split('pane-')[1].split('.json')[0])
    break
")
~/.claude/skills/tmux-slot-command/scripts/send-to-slot.sh $DEV_SLOT \
  'QA found issues on PR #$PR_NUMBER. Fix these: [specific issues from report]. Push to branch and notify when ready for re-test.'
```

4. After Rajiv merges (manually) → run `/cleanup-pr`. After Rajiv rejects → rework flow.

**Slot after QA:** Returns to main and waits for the next task.
```bash
# Slot cleanup (included in QA brief's post-QA section)
# Slot returns to main: git checkout main
# Modal apps and dev server stay running (Rajiv directive 2026-04-04)
```

**Key rule:** Rework goes back to the same slot that implemented the change.

### Type 4: POST-PR — Notify Rajiv + QA Decision

**Condition:** `occupied == true`, PR exists on branch (slot created draft PR)

**Action:** Notify Rajiv, update labels, make QA decision. CI pass/fail arrives via
#heydonna-alerts Slack channel automatically — no active monitoring needed.

**Step 1: Update issue label**

POST-PR (draft PR exists, before QA pass) → leave as `status:in-progress`. The `status:in-review` transition is owned by `pr-mark-ready` Phase A which fires IMMEDIATELY on QA PASS — it does NOT wait for CI. CI green is a separate Phase B promotion to standalone `merge-ready` (see CP #16 in `.claude/rules/20-buddhi-pm.md` and Rajiv directive 2026-05-08 08:12 + 2026-05-12 16:13 IST: *"mark it as ready after qa-pass"*). Do NOT set `status:in-review` here.

If neither QA nor CI is required for this PR (pure infra/types/refactor — see decision matrix below), then this skill may set `status:in-review` directly:
```bash
gh issue edit $ISSUE --remove-label status:in-progress --add-label status:in-review
```

**Step 2: QA Decision**

Apply the decision matrix **by user-visible EFFECT, not file location**:

| Change Effect | Examples | PM Action |
|---------------|----------|-----------|
| **User-visible output changes** | Components, styles, editor extensions, formatting pipeline, DOCX export, auto-process output | Generate QA brief (`pm-qa-brief` skill) → post to PR as comment → send back to **same slot** |
| **Backend-only, no visible change** | Convex mutations, webhooks, storage, auth | Ask Rajiv via Slack: "Skip QA for #ISSUE? Backend-only." |
| **Pure infra/types/refactor/tests** | Types, test files, CI config, docs, deps | No QA — notify Rajiv: "PR #NNN ready, no QA needed" |

**Key rule:** The test is "could a user see a difference?", not "is this a React
component?" Files in `modal/`, `lib/editor/`, `lib/formatter/` all produce visible output.

**For QA needed:**
1. Generate QA brief using `pm-qa-brief` skill (reads diff, builds scenarios)
2. Post brief to PR as comment: `gh pr comment $PR_NUMBER --body "$(cat /tmp/qa-brief-${ISSUE}.md)"`
3. Send QA brief back to same slot with checkout instructions (see `pm-qa-brief` skill)

**Step 3: Notify Rajiv**
DM Rajiv with PR summary, QA decision, and whether CI passed (from #heydonna-alerts).

**CI monitoring:** Rely on #heydonna-alerts for CI pass/fail. When CI alert arrives,
PM acts: merge (if approved) or investigate failure.

**Never poll CI inline.** CI pass/fail notifications arrive automatically via #heydonna-alerts Slack channel.

### Type 5: POST-CLAUDE'S CORNER — Skip (Heartbeat Only)

**CC report processing has been moved to the heartbeat cycle only.**
When a slot returns idle after Claude's Corner, do NOT process CC reports here.
The 3h heartbeat (`heartbeat-tasks` skill) handles all CC report processing
via the `cc-processing` background agent.

**Action:** Fall through to Type 6 (FREE SLOT) — check for queued work or leave idle.

### Type 6: FREE SLOT — Proactive Dispatch from Kanban (UPDATED 2026-04-30)

**[HIGH] Hard invariant (Rajiv directive 2026-05-20 11:15 IST; revised 2026-05-29):** When ANY slot is in state FREE / STANDBY / BLOCKED and there is at least one approved-dispatchable item in the Ready Pool, PM MUST run `Skill(slot-dispatch-sweep)` and let it dispatch through `slot-claim`, or emit a concrete blocker such as `slot-worktree-dirty`, `open-dependency`, `umbrella-tracker`, `customer-p0-awaiting-pm-triage`, `mop-http-unreachable`, or `all_free_slots_assigned`. Reporting `Actions needed: none` while dispatchable Kanban work exists is INVALID and constitutes a queue-stall. Detector before posting "no action": run the sweep or count clean `status:todo` items; if `free_slots > 0 AND claimable_issues > 0`, the post is malformed unless it includes dispatch proof or a concrete blocker.

**[HIGH] Reconciliation auto-invoke (Rajiv CTO directive 2026-05-26 11:47 IST thread `1779773350.706679` reply `1779776327.681149` ITEM 5b; revised 2026-05-29):** When Type 6 FREE SLOT classification fires AND `free_slots > 0 AND claimable_issues > 0`, PM MUST auto-invoke `Skill(slot-dispatch-sweep)` with trigger-source `slot-freed` (or `pm-idle-notification`) BEFORE returning to the event loop. The sweep emits canonical ASSIGNED / QUEUED / IDLE / BLOCKED proof-lines per CP #17. Do NOT manually decide dispatch routing here — the sweep is the canonical reconciler and now uses soft affinity across all slots. Lane identity is advisory only and must not leave a slot idle while clean `status:todo` work exists. The pre-2026-05-26 inline dispatch procedure below is fallback-only when the sweep script is missing or exits non-zero.

**[HIGH] No standby-by-role (Rajiv directive 2026-05-29):** Slots 1-6 are all eligible for clean Ready Pool work. A free slot can remain idle only if every remaining `status:todo` item is blocked, deny-listed, an umbrella/tracker, already assigned, or all free slots were consumed earlier in the same sweep. The proof must name that blocker.

Trigger incidents (chitta 2026-05-20): PR #4711 + #4735 + #4727 leads sat 30-60min "waiting for CI green" while Queued had P1+P2 dispatchable items. PM reported `Actions needed: none` four times in the same idle cycle. Companion to the updated POST-QA PASS row above + `feedback_pm_idle_actions_none_invalid_when_queue_has_dispatchable_work`.

**Condition:** `occupied == false`, `dnd == false`, slot just became free
(released from a task).

**Source of truth for fallback only:** `~/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/memory/pm-todo.md` — the PM kanban written by `todo-prioritize`
after Rajiv approves the plan. Do NOT re-query `gh issue list` for dispatch
decisions; the kanban is authoritative for what to assign next.

(Rajiv directive 2026-04-30 11:01: "todos should be like a kanban board for the
PM. update slot idle post free slot workflow to follow the assignment plan.")

**Action — proactive dispatch within 5 min of slot freeing (fallback only if slot-dispatch-sweep is unavailable):**

1. **Read the kanban:**
   ```bash
   cat ~/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/memory/pm-todo.md
   ```

2. **Pick the next item for THIS slot:**
   Walk the `## Queued` section in priority order (P0 → P1 → P2 → P3). For each
   line, the assignment is encoded as `→ slot N (Name)`. Match against the slot
   that just freed:
   - If the line says `→ slot <N>` matching the freed slot → that's the dispatch
     target.
   - If the line says `→ queued behind #MMMM` → skip (still blocked).
   - If the line says `→ backlog` → skip (no slot match).
   - **Soft-affinity fallback (Rajiv directive 2026-05-29):** if no item is
     explicitly targeted at the freed slot, assign the highest-priority clean
     Ready Pool item to that slot. Role lane is advisory only; do not keep a
     slot idle because a different slot would have been a better lane match.

3. **If a target item is found:**
   - Verify the issue is still `status:todo` (it might have been picked up by
     another slot since the kanban was written):
     ```bash
     gh issue view <NNNN> --json labels --jq '[.labels[].name] | contains(["status:todo"])'
     ```
   - If still `status:todo` → run `/handoff <slot> <NNNN>`.
   - Update `~/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/memory/pm-todo.md` — move the line from `## Queued` into `## Now`
     under the slot (use `Edit` tool, single-line replacement).
   - Continue the slot-idle handler completion.

4. **If no target item is found AND the kanban's `## Queued` section is empty:**
   - Fall through to Claude's Corner (Step 5 below).

5. **Claude's Corner fallback (cooldown applies):**
   The CC fallback only fires when the kanban queue has no clean dispatchable
   item for any slot. If queued work exists but is blocked, assigned elsewhere,
   or deny-listed, emit the blocker proof before considering CC.

**Anti-pattern (do not):** Re-query `gh issue list status:todo --limit 3` and
dispatch the head of that list. The kanban exists to enforce the role-routing
rules; bypassing it via raw `gh issue list` reintroduces the proactive-dispatch
gap that produced #3413 sitting unassigned overnight despite 3 free dev slots.
(Rajiv directive 2026-04-30 11:01.)

**Action — Claude's Corner (only if kanban Queued has no clean dispatchable item):**

**Check cooldown before sending CC.** CC burns tokens with diminishing returns when
cycling every few minutes. Only send if >1 hour since last CC was sent to ANY slot:

```bash
# Check global CC cooldown (1 hour)
LAST_CC=$(cat /tmp/cc-last-sent-global.timestamp 2>/dev/null || echo "0")
NOW=$(date +%s)
ELAPSED=$(( NOW - LAST_CC ))
if [ "$ELAPSED" -lt 3600 ]; then
  echo "CC cooldown: ${ELAPSED}s since last CC send (need 3600s). Skipping."
  # Leave slot idle — it will get work from next handoff or next heartbeat
  return
fi

# Cooldown passed — send CC and update timestamp
mop_send_to_slot(slot: N, command: "/claudes-corner", force: true)
date +%s > /tmp/cc-last-sent-global.timestamp
```

CC is interruptible — if real work arrives via handoff, the slot drops CC immediately.

**CC is triggered by the idle handler, NOT by heartbeat-tasks.** The heartbeat only
*processes* accumulated CC reports (via the `cc-processing` background agent).
The idle handler is the single place where CC gets sent to free slots.

**Budget awareness:** CC is exploratory — no direct user value until issues are filed
and fixed. Rajiv directive: "we're not making any money yet and the budget for this
project is limited. we're just burning through tokens if we keep cycling." One CC
cycle per hour across all slots is the maximum. (Lesson: 2026-03-11)

## Full Decision Flowchart

```
[slot N idle] notification arrives
│
├─ DND? ──────────────────────────── YES → IGNORE (stop)
│   NO
│
├─ QA report exists? ─────── YES → POST-QA (read report, DM Rajiv)
│   NO
│
├─ OCCUPIED? ─────────────────────── YES → Check context:
│   │                                    │
│   │                                    ├─ PR exists on branch? → POST-PR (notify + QA decision)
│   │                                    │
│   │                                    └─ No PR? → POST-IMPL (send /review-and-pr)
│   │
│   NO (FREE — applies to ALL slots 1-6)
│   │
│   ├─ Queued issues (status:todo)? → invoke slot-dispatch-sweep; HANDOFF only if sweep unavailable
│   │
│   └─ Nothing queued? → CLAUDE'S CORNER (send /claudes-corner to ANY free slot)
│       (CC can be resent even if slot just finished CC — keeps slots productive)
```

**All slots (1-6) are unified:**
- Any slot can receive issue handoffs, implement, create PRs, and run self-QA
- After implementation, same slot proceeds to self-QA (Phase 5-8)
- After QA report, slot returns to main and waits for next task
- **Receives Claude's Corner when idle**

## Slot Release After Merge / Parked PR

All slots follow the same occupancy rule: the slot is occupied only while it has
local execution work. Once the PR is pushed and the next state is CI, Codex
review, PM QA, or Rajiv merge, release the slot and run `slot-dispatch-sweep`.
Keep PR ownership as metadata (`origin-slot:N` / pm-todo watch), not as worker
reservation.

When Rajiv merges a PR manually, run `/cleanup-pr` (post-merge cleanup), then release:
```
mop_release_slot(slot: N)
```

The next idle notification from that slot will hit the FREE path and must run
the sweep if any clean `status:todo` item exists.

## Multiple Rapid Notifications

Slots sometimes send multiple idle notifications in quick succession (e.g., after
autocompact). To avoid duplicate actions:

- **Debounce:** If the last action for this slot was < 30 seconds ago, skip.
- **Idempotent:** Most actions are safe to repeat (collecting reports, checking PR status).
- **Track:** Use the pane state file's `last_activity` timestamp to detect rapid-fire.

## Integration with Existing Hooks

This skill defines PM **behavior** for idle notifications. The notification
delivery comes from the dev slot's Stop hook:

| Component | Location | Role |
|-----------|----------|------|
| `slot-idle-notify.sh` (Stop hook) | Dev slot `.claude/settings.json` | Sends `[slot N idle ...]` to PM pane |
| **This skill** | PM inline processing | PM's decision logic for all idle types |

There is no separate hook on the PM pane for idle notifications. The PM receives
the idle message as a regular user prompt and processes it using this skill's
decision matrix. CI pass/fail arrives via #heydonna-alerts Slack channel automatically.

## Anti-Patterns

- **Never send CC more than once per hour globally** — check `/tmp/cc-last-sent-global.timestamp`. Free slots without queued issues stay idle until the next CC window or work arrives. Rajiv directive: budget-conscious CC usage
- **Never let slots self-trigger QA or PR** — PM decides the next step
- **Never skip QA for user-visible changes** — classify by effect ("could a user see a difference?"), not file path. `modal/*/processor.py`, `lib/editor/`, `lib/formatter/` all produce visible output
- **Never `/clear` before QA** — QA runs directly in the implementing slot's main session. `/clear` is handoff-only (`pane-handoff` Step 2.2), never needed for QA or `/review-and-pr`
- **Never `/clear --wait`** — `/clear` starts a new session instantly. `--wait` polls for idle which times out (120s) because the new session is already idle
- **Never act on DND slots** — Rajiv is working there
- **Never block PM main thread** — if a decision takes research, note it and come back
- **Never auto-merge** — always Slack Rajiv "PR ready, CI green — merge?" and wait
- **Never hand off issues without checking backlog** — all slots can receive any work
- **Never send rework to a different slot** — when QA fails, rework goes back to the same slot that implemented the change
- **Never run QA from PM pane** — QA runs in the implementing slot via qa-tester subagent
