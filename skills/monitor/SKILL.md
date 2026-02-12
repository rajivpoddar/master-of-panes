---
name: monitor
description: Monitor a tmux dev slot — poll activity and report progress. Usage /tmux-manager:monitor <slot> [goal]
arguments: "<slot> [goal]"
---

# /tmux-manager:monitor

Launch a background monitoring loop for a tmux slot. Polls activity and captures output periodically to track progress toward a goal.

## Instructions

Parse `$ARGUMENTS`: first token is the slot number, the rest is an optional goal description.

### Monitoring Protocol

Use the Task tool with `run_in_background: true` to launch a monitoring agent. The agent should:

1. **Poll every 60 seconds** using:
   ```bash
   bash {{PLUGIN_DIR}}/scripts/is-active.sh <slot> && echo "ACTIVE" || echo "IDLE"
   ```

2. **Capture recent output** each cycle:
   ```bash
   bash {{PLUGIN_DIR}}/scripts/capture-output.sh <slot> 20
   ```

3. **Detect stages** in the pane output:
   - **Planning**: Look for "EnterPlanMode", "plan mode", "approve"
   - **Implementing**: Look for code changes, file edits, test runs
   - **Testing**: Look for "vitest", "tsc", "pytest", "test", "PASS", "FAIL"
   - **Stalled**: Idle for more than 10 minutes with no progress

4. **Report progress**: Log observations to `/tmp/slot-<N>-monitor.log`

5. **Handle stalls**: If idle for >10 minutes with no new output, send a nudge:
   ```bash
   bash {{PLUGIN_DIR}}/scripts/send-to-slot.sh <slot> "continue with the current task"
   ```

6. **Stop conditions**:
   - Goal appears to be met (based on pane output)
   - Slot becomes unoccupied (state file shows `occupied: false`)
   - 60 minutes elapsed (maximum monitoring duration)

### Output

Provide an initial confirmation:
- Slot being monitored
- Goal (if provided)
- Log file location: `/tmp/slot-<N>-monitor.log`

The background agent will continue monitoring autonomously.
