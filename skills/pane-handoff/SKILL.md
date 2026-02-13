---
name: pane-handoff
description: Hand off a task to a tmux dev pane — assign state, send instructions via tmux. Usage /master-of-panes:pane-handoff <pane> <message>
arguments: "<pane> <message>"
---

# /master-of-panes:pane-handoff

Send a task to a tmux dev pane. This assigns the pane and sends a message to the Claude Code session running in that pane.

## Instructions

Parse `$ARGUMENTS`: first token is the pane number, the rest is the message to send.

### Step 1: Verify the pane

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/get-pane-status.sh
```

Check that the target pane exists and note its current state.

### Step 2: Assign the pane (if not already occupied)

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/assign-pane.sh <pane> "<task summary>"
```

Use the first ~50 characters of the message as the task summary.

### Step 3: Send the message

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/send-to-pane.sh <pane> '<message>'
```

This waits for the pane to become idle, then sends the message via tmux send-keys.

### Step 4: Verify delivery

Wait 3 seconds, then capture recent output to confirm:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/capture-output.sh <pane> 5
```

### Report

Show a summary:
- Pane number and address
- Task assigned
- Whether message was delivered successfully
- Snippet of pane output after delivery
