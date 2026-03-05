---
name: pane-handoff
description: Hand off a GitHub issue to a tmux dev pane — extract issue, git sync, assign state, send instructions. Usage /master-of-panes:pane-handoff <pane> <issue>
arguments: "<pane> <issue>"
---

# /master-of-panes:pane-handoff

Full-workflow handoff of a GitHub issue to a tmux dev pane. Extracts the issue, syncs git, assigns state, and sends instructions.

## Arguments

`<pane> <issue>` — pane number (1-4) and GitHub issue number.

Example: `/master-of-panes:pane-handoff 4 1284`

## Instructions

Parse `$ARGUMENTS`: first token is the pane number, second is the GitHub issue number.

### Phase 0: Issue Freshness Review (MANDATORY)

Before extracting, review the issue for stale information. This prevents slots from building plans on outdated assumptions.

```bash
ISSUE=<issue_number>

# Check issue age + recent merged PRs that may invalidate assumptions
CREATED_DATE=$(gh issue view $ISSUE --json createdAt -q '.createdAt' | cut -dT -f1)
echo "Issue created: $CREATED_DATE"

# PRs merged since issue creation
gh pr list --state merged --search "merged:>=$CREATED_DATE" --limit 15 \
  --json number,title --jq '.[] | "#\(.number): \(.title)"'

# Dependency status
gh issue view $ISSUE --json body -q '.body' | grep -oE '#[0-9]+' | sort -u | while read ref; do
  NUM=${ref#\#}; STATE=$(gh issue view $NUM --json state -q '.state' 2>/dev/null || echo "NOT_FOUND")
  echo "$ref: $STATE"
done
```

**Review for:** stale API limits/file sizes, resolved dependencies still marked "blocked", superseded comments, missing context from recent PRs.

**If stale:** Post a "PM Notes for Implementation" comment on the issue with corrections before handing off.

**If fresh:** Proceed to Phase 1.

### Phase 1: Extract and Prepare

#### Step 1.1: Fetch issue details

```bash
ISSUE=<issue_number>
PANE=<pane_number>

# Fetch issue
TITLE=$(gh issue view $ISSUE --json title -q '.title')
LABELS=$(gh issue view $ISSUE --json labels -q '.labels[].name' | tr '\n' ',')
BODY=$(gh issue view $ISSUE --json body -q '.body')
```

#### Step 1.2: Detect recommended subagent

Check if the project defines a subagent routing table (typically in the project's own `pm-handoff` skill or `CLAUDE.md`). Route based on issue labels and body text.

If no project-specific routing exists, use a generic developer subagent.

#### Step 1.3: Build handoff file

Extract the full issue (title, body, labels, comments) to `/tmp/handoff-<ISSUE>.md`.

The handoff file MUST use this workflow prefix template:

```markdown
Analyze the following issue and follow the workflow.

## Workflow

1. Create branch: `git checkout -b fix/<ISSUE>-short-description`
2. Enter plan mode (EnterPlanMode tool)
3. Explore codebase and design solution
4. Exit plan mode with permissions (ExitPlanMode with allowedPrompts)
   — Plan is saved automatically to `docs/plans/` (configured in `.claude/settings.json`)
   — PM is notified automatically via tmux when the plan file is written
   — **Immediately after ExitPlanMode**, rename the plan file to the issue slug:
     ```bash
     PLAN=$(ls -t docs/plans/*.md | head -1)
     mv "$PLAN" docs/plans/issue-<ISSUE>-short-slug.md
     ```
5. Implement using the recommended subagent (see below)
   **For bug fixes — test-first is mandatory, no exceptions:**
   - Write a failing test that reproduces the bug BEFORE implementing
   - The test must fail on current code, pass after your fix
   - If you can't write a failing test, STOP and ask PM for clarification
6. Run project-specific tests and linting (see project's CLAUDE.md for commands)
7. Deploy if needed (see project's deploy skills/scripts)
8. **STOP and wait for PM.** Do NOT run QA yourself. Do NOT create a PR.
   The PM will send the next instruction — just wait.
```

**Project-specific rules:** The project's `pm-handoff` skill may inject additional sections into the handoff file (dev server rules, deploy commands, test data, etc.). Check for a project-level handoff skill and include its content after the workflow prefix.

After the workflow prefix, append:
1. **Subagent recommendation** — from project-specific routing (Step 1.2)
2. **Merge policy** — NEVER merge, PM reviews all PRs
3. Full issue content from `gh issue view $ISSUE --json number,title,state,url,body,labels,comments` piped through jq

#### Step 1.4: Update GitHub labels

```bash
gh issue edit $ISSUE --remove-label status:todo --remove-label status:in-review \
  --add-label status:in-progress --add-label "slot:$PANE"
```

### Phase 2: Tmux Handoff

#### Step 2.1: Git sync

Send `/git-sync-main` to the pane and wait for idle:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/send-to-pane.sh $PANE '/git-sync-main' --wait
```

#### Step 2.2: Clear session

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/send-to-pane.sh $PANE '/clear' --wait
```

**NOTE:** Do NOT send `/rename` after `/clear`. The `/clear` command creates a new session,
so any `/rename` sent afterward either applies to the dead session or gets lost. Session
tracking is handled by the pane state file (`assign-pane.sh`) instead.

#### Step 2.3: Send handoff content

Load the handoff file into the pane via tmux buffer.
**IMPORTANT:** Use separate send-keys calls with a delay between text and Enter.

```bash
PANE_ADDR=$(source ${CLAUDE_PLUGIN_ROOT}/scripts/pane-lib.sh && load_config && pane_address $PANE)
tmux load-buffer "/tmp/handoff-${ISSUE}.md"
tmux paste-buffer -t "$PANE_ADDR"
sleep 1
tmux send-keys -t "$PANE_ADDR" Enter
```

#### Step 2.4: Assign state

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/assign-pane.sh $PANE "#$ISSUE: $TITLE"
```

#### Step 2.5: Capture session ID

After the handoff prompt is delivered, wait for the session to start, then capture the
Claude Code session UUID from the most recent `.jsonl` file in the checkout's project dir.

```bash
sleep 5

# Derive the project dir from the checkout path
CHECKOUT_PATH=$(tmux display-message -t "$PANE_ADDR" -p '#{pane_current_path}')
PROJECT_DIR_NAME=$(echo "$CHECKOUT_PATH" | sed 's|^/||; s|/|-|g')
PROJECT_DIR="$HOME/.claude/projects/-${PROJECT_DIR_NAME}"

# Get the most recent session file
SESSION_ID=$(ls -t "$PROJECT_DIR"/*.jsonl 2>/dev/null | head -1 | xargs basename 2>/dev/null | sed 's/.jsonl$//')

if [ -n "$SESSION_ID" ]; then
  bash ${CLAUDE_PLUGIN_ROOT}/scripts/update-pane-state.sh $PANE --session "$SESSION_ID"
  echo "Session ID: $SESSION_ID"
fi
```

This allows later `/resume $SESSION_ID` if the session needs to be restored after a `/clear`.

#### Step 2.6: Verify delivery

Capture output to confirm the pane received the handoff:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/capture-output.sh $PANE 5
```

### Phase 3: Verify Hooks Are Active

No supervisor needed — hooks handle the full lifecycle automatically. Hooks are permanently
configured in each slot's settings (no per-handoff installation needed):

- **Plan written** → PostToolUse hook fires → PM pane receives notification
- **Slot goes idle** → Stop hook fires → PM pane receives idle notification

After verifying delivery (Step 2.6), the handoff is complete.

### Report

Show a summary table:
- Issue: #<number> — <title>
- Pane: <number> (address: <tmux_addr>)
- Subagent: <detected_subagent>
- Labels: status:in-progress, slot:<N>
- Handoff file: /tmp/handoff-<ISSUE>.md

## Error Handling

- If pane is in TESTING mode: Show testing info, suggest a different pane. NEVER overwrite a TESTING pane.
- If pane is already OCCUPIED: Show current task and suggest releasing first
- If issue not found: Show error and exit
- If git-sync fails: Retry once, then report failure
- If pane is ACTIVE when handoff starts: Wait for idle (up to 2 minutes)

## Slot Selection Guidelines

When the PM doesn't specify a slot number, prefer higher-numbered free slots (3, 2, 1) to avoid
conflicts with PM manual testing which typically uses lower-numbered slots. Always check pane
status first and skip any slots in TESTING or OCCUPIED state.
