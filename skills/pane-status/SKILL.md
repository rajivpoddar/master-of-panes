---
name: pane-status
description: Show the status of all tmux dev panes — occupancy, task, branch, and live activity.
arguments: "[--live]"
---

# /master-of-panes:pane-status

Display the current state of all tmux dev panes.

## Instructions

Run the status script:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/get-pane-status.sh $ARGUMENTS
```

Display the output as-is (it's a pre-formatted ASCII table).

### Flags

- No flags: Shows status from state files only (occupied/free, task, branch)
- `--live`: Also checks real tmux activity via `is-active.sh` — shows ACTIVE/IDLE instead of OCCUPIED

### After displaying

If any panes show issues (e.g., occupied but no task), mention it briefly. Otherwise, just show the table with no commentary.
