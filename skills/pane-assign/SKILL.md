---
name: pane-assign
description: Assign a task to a tmux dev pane. Usage /master-of-panes:pane-assign <pane> <task> [branch]
arguments: "<pane> <task> [branch]"
---

# /master-of-panes:pane-assign

Allocate a tmux dev pane for a task by updating its state file.

## Instructions

Parse the arguments: `$ARGUMENTS` should contain `<pane> <task> [branch]`.

If arguments are missing, show usage:
```
Usage: /master-of-panes:pane-assign <pane> <task> [branch]
Example: /master-of-panes:pane-assign 1 "Fix login bug" feature/fix-login
```

Otherwise, run:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/assign-pane.sh $ARGUMENTS
```

### On success

Show the assignment confirmation. The pane is now marked as occupied in `~/.claude/tmux-panes/pane-N.json`.

### On failure

If the pane is already occupied, show the error and suggest using `/master-of-panes:pane-status` to see current state, or manually releasing with:
```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/update-pane-state.sh <pane> --release
```
