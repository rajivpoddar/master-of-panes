---
name: assign
description: Assign a task to a tmux dev slot. Usage /tmux-manager:assign <slot> <task> [branch]
arguments: "<slot> <task> [branch]"
---

# /tmux-manager:assign

Allocate a tmux slot for a task by updating its state file.

## Instructions

Parse the arguments: `$ARGUMENTS` should contain `<slot> <task> [branch]`.

If arguments are missing, show usage:
```
Usage: /tmux-manager:assign <slot> <task> [branch]
Example: /tmux-manager:assign 1 "Fix login bug" feature/fix-login
```

Otherwise, run:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/assign-slot.sh $ARGUMENTS
```

### On success

Show the assignment confirmation. The slot is now marked as occupied in `~/.claude/tmux-slots/slot-N.json`.

### On failure

If the slot is already occupied, show the error and suggest using `/tmux-manager:status` to see current state, or manually releasing with:
```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/update-slot-state.sh <slot> --release
```
