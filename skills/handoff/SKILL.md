---
name: handoff
description: Hand off a task to a tmux dev slot — assign state, send instructions via tmux. Usage /tmux-manager:handoff <slot> <message>
arguments: "<slot> <message>"
---

# /tmux-manager:handoff

Send a task to a tmux dev slot. This assigns the slot and sends a message to the Claude Code session running in that pane.

## Instructions

Parse `$ARGUMENTS`: first token is the slot number, the rest is the message to send.

### Step 1: Verify the slot

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/get-slot-status.sh
```

Check that the target slot exists and note its current state.

### Step 2: Assign the slot (if not already occupied)

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/assign-slot.sh <slot> "<task summary>"
```

Use the first ~50 characters of the message as the task summary.

### Step 3: Send the message

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/send-to-slot.sh <slot> '<message>'
```

This waits for the slot to become idle, then sends the message via tmux send-keys.

### Step 4: Verify delivery

Wait 3 seconds, then capture recent output to confirm:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/capture-output.sh <slot> 5
```

### Report

Show a summary:
- Slot number and pane address
- Task assigned
- Whether message was delivered successfully
- Snippet of pane output after delivery
