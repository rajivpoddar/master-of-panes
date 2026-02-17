---
name: pane-handoff
description: Hand off a GitHub issue to a tmux dev pane — extract issue, git sync, assign state, send instructions, and optionally launch supervisor. Usage /master-of-panes:pane-handoff <pane> <issue>
arguments: "<pane> <issue>"
---

# /master-of-panes:pane-handoff

Full-workflow handoff of a GitHub issue to a tmux dev pane. Extracts the issue, syncs git, assigns state, sends instructions, and optionally launches a background supervisor.

## Arguments

`<pane> <issue>` — pane number (1-4) and GitHub issue number.

Example: `/master-of-panes:pane-handoff 4 1284`

## Instructions

Parse `$ARGUMENTS`: first token is the pane number, second is the GitHub issue number.

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

Based on issue labels and body text, detect the appropriate subagent:

| Pattern | Subagent |
|---------|----------|
| `integration.test`, `test.coverage`, `write.*test` | `integration-test-specialist` |
| `editor`, `track.change`, `prosemirror`, `tiptap`, `marks`, `extension` | `editor-specialist` |
| `pagination`, `page.break`, `template`, `layout.dsl`, `placeholder` | `pagination-template-specialist` |
| `ai`, `proofread`, `gemini`, `modal`, `transcription` | `ai-pipeline-specialist` |
| Everything else | `fullstack-dev` |

#### Step 1.3: Build handoff file

Extract the full issue (title, body, labels, comments) to `/tmp/handoff-<ISSUE>.md`.

The handoff file MUST use this exact workflow prefix template:

```markdown
Analyze the following issue and follow the workflow.

## Workflow

1. Create branch: `git checkout -b fix/<ISSUE>-short-description`
2. Enter plan mode (EnterPlanMode tool)
3. Explore codebase and design solution
4. **BEFORE exiting plan mode:** Copy your plan to `docs/plans/issue-<ISSUE>-plan.md` using the Write tool.
   This persists the plan in the repo so it survives context loss and gets committed with the PR.
5. Exit plan mode with permissions (ExitPlanMode with allowedPrompts)
6. Implement using the recommended subagent (see below)
7. Run automated tests: `npx tsc --noEmit && bunx vitest run && bun lint`
8. If ANY convex/ files were modified: run `~/.claude/skills/convex-dev-deploy/convex-dev-deploy/scripts/deploy.sh` to sync Convex functions BEFORE QA
9. Delegate to qa-tester subagent for browser verification and QA report
10. STOP after QA report is generated — PM will review and trigger PR creation

## CRITICAL: Plan Persistence

Claude Code saves plans to `~/.claude/plans/<random-name>.md` which is NOT in the repo
and has random filenames. You MUST copy your plan to `docs/plans/issue-<ISSUE>-plan.md`
BEFORE exiting plan mode. This file:
- Gets committed with the PR (via safe-commit staging rules)
- Survives session clears and context compaction
- Can be referenced by the PM or other agents

## Dev Server Rules
- If you need a dev server for QA testing, use the `/bun-dev-server` skill to start it on YOUR slot's port
- NEVER stop, kill, or restart dev servers on OTHER slots' ports
- NEVER run kill/pkill/lsof commands targeting server processes on other ports
- Each slot has its own port (3001-3004) determined by the worktree's .env.local
```

After the workflow prefix, append:
1. **Subagent recommendation** — primary subagent and QA delegation
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
# Claude Code stores sessions at ~/.claude/projects/<path-with-dashes>/
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

### Phase 3: Launch Supervisor (Optional)

After the handoff, ask the user:
> "Handoff complete. Launch a background supervisor to monitor pane $PANE for #$ISSUE?"

If yes, use `/master-of-panes:pane-monitor $PANE "Complete #$ISSUE through PR creation"` — the monitor skill handles background agent launch.

For autonomous operation (when PM is away), launch automatically.

### Report

Show a summary table:
- Issue: #<number> — <title>
- Pane: <number> (address: <tmux_addr>)
- Subagent: <detected_subagent>
- Labels: status:in-progress, slot:<N>
- Handoff file: /tmp/handoff-<ISSUE>.md
- State file: ~/.claude/tmux-panes/pane-<N>.json

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
