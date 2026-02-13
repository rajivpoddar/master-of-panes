---
name: status
description: Show the status of all tmux dev slots — occupancy, task, branch, and live activity.
arguments: "[--live]"
---

# /master-of-panes:status

Display the current state of all tmux dev slots.

## Instructions

Run the status script:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/get-slot-status.sh $ARGUMENTS
```

Display the output as-is (it's a pre-formatted ASCII table).

### Flags

- No flags: Shows status from state files only (occupied/free, task, branch)
- `--live`: Also checks real tmux activity via `is-active.sh` — shows ACTIVE/IDLE instead of OCCUPIED

### After displaying

If any slots show issues (e.g., occupied but no task), mention it briefly. Otherwise, just show the table with no commentary.
