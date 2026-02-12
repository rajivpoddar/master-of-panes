---
name: setup
description: Configure the tmux slot manager — number of slots, pane layout, state directory. Run once after installing the plugin.
arguments: ""
---

# /tmux-manager:setup

Interactive setup for the tmux slot manager. Creates `config.json` and initializes state files.

## Instructions

Guide the user through configuration by asking these questions:

### Question 1: Number of dev slots

Ask: "How many dev slots do you want? (default: 4)"

Valid range: 1-9. Store as `slots`.

### Question 2: tmux pane prefix

Ask: "What is your tmux session:window prefix? (default: 0:0)"

This is the `session:window` part of your pane addresses. For example, if your dev panes are `0:0.1` through `0:0.4`, the prefix is `0:0`. If they're `main:1.1` through `main:1.4`, the prefix is `main:1`.

Store as `pane_prefix`.

### Question 3: Manager pane

Ask: "Which pane is the manager/orchestrator? (default: {pane_prefix}.0)"

This is the pane where the PM or orchestrator session runs. Store as `manager_pane`.

### Question 4: State directory

Ask: "Where should state files be stored? (default: ~/.claude/tmux-slots)"

Store as `state_dir`.

## After collecting answers

### Step 1: Write config.json

```bash
mkdir -p ~/.claude/tmux-slots
cat > ~/.claude/tmux-slots/config.json << 'JSONEOF'
{
  "slots": <slots>,
  "pane_prefix": "<pane_prefix>",
  "manager_pane": "<manager_pane>",
  "state_dir": "<state_dir>"
}
JSONEOF
```

Replace the placeholders with the user's answers.

### Step 2: Initialize state files

For each slot 1 through N:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/update-slot-state.sh <slot> --release 2>/dev/null || true
```

Or if the state files don't exist yet, they'll be auto-created on first use by `ensure_state_file()`.

### Step 3: Verify

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/get-slot-status.sh
```

Show the status table to confirm setup worked.

### Step 4: Report

Show a summary:
- Number of slots configured
- Pane prefix
- Manager pane
- State directory
- Config file location: `~/.claude/tmux-slots/config.json`

Tell the user: "Setup complete. All `/tmux-manager:*` commands will use this configuration."
