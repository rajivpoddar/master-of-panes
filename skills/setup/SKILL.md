---
name: setup
description: Configure the tmux slot manager — number of slots, pane layout, state directory. Run once after installing the plugin.
arguments: ""
---

# /master-of-panes:setup

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

Use `jq -n` to safely construct the JSON. This prevents injection from special characters (quotes, newlines) in user answers, and writes atomically via temp file + mv to prevent partial reads by concurrent scripts.

```bash
mkdir -p ~/.claude/tmux-slots
jq -n \
  --argjson slots <SLOTS> \
  --arg pane_prefix "<PANE_PREFIX>" \
  --arg manager_pane "<MANAGER_PANE>" \
  --arg state_dir "<STATE_DIR>" \
  '{slots: $slots, pane_prefix: $pane_prefix, manager_pane: $manager_pane, state_dir: $state_dir}' \
  > /tmp/master-of-panes-config.$$.json \
  && mv /tmp/master-of-panes-config.$$.json ~/.claude/tmux-slots/config.json
```

Replace `<SLOTS>` with the numeric value (no quotes), and the others with the user's string answers. The `--arg` flags handle JSON escaping automatically.

**IMPORTANT:** Use `--argjson` (not `--arg`) for `slots` since it's a number. Use `--arg` for strings.

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

Tell the user: "Setup complete. All `/master-of-panes:*` commands will use this configuration."
