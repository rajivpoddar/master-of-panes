---
name: pane-setup
description: Configure the tmux pane manager — dev pane addresses, manager pane, state directory. Run once after installing the plugin.
arguments: ""
---

# /master-of-panes:pane-setup

Interactive setup for the tmux pane manager. Creates `config.json` and initializes state files.

## Instructions

Guide the user through configuration by asking these questions:

### Question 1: Manager pane

Ask: "Which pane is the manager/orchestrator? (default: 0:0.0)"

This is the tmux pane where the PM or orchestrator session runs. Store as `panes.manager`.

### Question 2: Dev panes

Ask: "Which panes are dev panes? Provide as comma-separated tmux addresses. (default: 0:0.1,0:0.2,0:0.3,0:0.4)"

These are the panes where Claude Code dev sessions run. Each address is in `session:window.pane` format. Store as `panes.dev` (an array).

### Question 3: State directory

Ask: "Where should state files be stored? (default: ~/.claude/tmux-panes)"

Store as `state_dir`.

## After collecting answers

### Step 1: Write config.json

Use `jq -n` to safely construct the JSON. This prevents injection from special characters (quotes, newlines) in user answers, and writes atomically via temp file + mv to prevent partial reads by concurrent scripts.

```bash
mkdir -p ~/.claude/tmux-panes
jq -n \
  --arg manager "<MANAGER_PANE>" \
  --arg dev_csv "<DEV_PANES_CSV>" \
  --arg state_dir "<STATE_DIR>" \
  '{panes: {manager: $manager, dev: ($dev_csv | split(",") | map(gsub("^ +| +$"; "")))}, state_dir: $state_dir}' \
  > /tmp/master-of-panes-config.$$.json \
  && mv /tmp/master-of-panes-config.$$.json ~/.claude/tmux-panes/config.json
```

Replace `<MANAGER_PANE>` with the manager address, `<DEV_PANES_CSV>` with the comma-separated dev pane list, and `<STATE_DIR>` with the state directory path. The `--arg` flags handle JSON escaping automatically.

**IMPORTANT:** The `split(",")` in the jq filter converts the CSV string into a JSON array. The `gsub` trims whitespace from each entry.

### Step 2: Initialize state files

For each dev pane 1 through N:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/update-pane-state.sh <pane> --release 2>/dev/null || true
```

Or if the state files don't exist yet, they'll be auto-created on first use by `ensure_pane_state()`.

### Step 3: Verify

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/get-pane-status.sh
```

Show the status table to confirm setup worked.

### Step 4: Report

Show a summary:
- Manager pane address
- Dev pane addresses
- State directory
- Config file location: `~/.claude/tmux-panes/config.json`

Tell the user: "Setup complete. All `/master-of-panes:pane-*` commands will use this configuration."
