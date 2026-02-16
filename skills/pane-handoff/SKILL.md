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

Extract the full issue (title, body, labels, comments) to `/tmp/handoff-<ISSUE>.md` with:
1. **Workflow prefix** — branching, plan mode, implementation, testing, QA pipeline
2. **Subagent recommendation** — primary subagent and QA delegation
3. **Merge policy** — NEVER merge, PM reviews all PRs
4. **Dev server rules** — NEVER start/stop/kill dev servers

Use the same format as `gh issue view $ISSUE --json number,title,state,url,body,labels,comments` piped through jq.

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

#### Step 2.2: Clear and rename

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/send-to-pane.sh $PANE '/clear' --wait
```

Generate short description from title and rename the session:
```bash
SHORT_DESC=$(echo "$TITLE" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9 ]//g' | awk '{for(i=1;i<=3&&i<=NF;i++) printf "%s-",$i; print ""}' | sed 's/-$//')
bash ${CLAUDE_PLUGIN_ROOT}/scripts/send-to-pane.sh $PANE "/rename issue-${ISSUE}-${SHORT_DESC}" --wait
```

#### Step 2.3: Send handoff content

Load the handoff file into the pane via tmux buffer:

```bash
PANE_ADDR=$(source ${CLAUDE_PLUGIN_ROOT}/scripts/pane-lib.sh && load_config && pane_address $PANE)
tmux load-buffer "/tmp/handoff-${ISSUE}.md"
tmux paste-buffer -t "$PANE_ADDR"
sleep 0.5
tmux send-keys -t "$PANE_ADDR" Enter
```

#### Step 2.4: Assign state

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/assign-pane.sh $PANE "#$ISSUE: $TITLE"
```

#### Step 2.5: Verify delivery

Wait 5 seconds, then capture output:

```bash
sleep 5
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
