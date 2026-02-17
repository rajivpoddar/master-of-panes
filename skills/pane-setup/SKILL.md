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

### Step 0: Verify or Create Panes

Before writing config, check if the configured panes exist:

```bash
# Check if tmux is running
if ! tmux list-sessions &>/dev/null 2>&1; then
  echo "No tmux session found. Creating one..."
  tmux new-session -d -s claude
fi

# Check if manager pane exists
if ! tmux display-message -p -t "<MANAGER_PANE>" '#{pane_id}' &>/dev/null 2>&1; then
  echo "Manager pane <MANAGER_PANE> not found."
fi

# Check each dev pane
MISSING_PANES=0
for addr in <DEV_PANES>; do
  if ! tmux display-message -p -t "$addr" '#{pane_id}' &>/dev/null 2>&1; then
    MISSING_PANES=$((MISSING_PANES + 1))
  fi
done
```

Replace `<MANAGER_PANE>` with the configured manager address and `<DEV_PANES>` with the space-separated list of dev pane addresses.

If panes are missing, ask the user:
> "N dev panes don't exist yet. Want me to create them? This will split the current tmux window into N+1 panes (1 manager + N dev panes)."

If yes, create the panes:

```bash
# Create panes by splitting the current window
SESSION_NAME="claude"
tmux new-session -d -s "$SESSION_NAME" 2>/dev/null || true  # OK if exists

# Split window to create dev panes
for i in $(seq 1 $NUM_DEV_PANES); do
  tmux split-window -t "${SESSION_NAME}:0" -h 2>/dev/null || \
  tmux split-window -t "${SESSION_NAME}:0" -v 2>/dev/null
done

# Rebalance layout
tmux select-layout -t "${SESSION_NAME}:0" tiled
```

After creating panes, discover the actual addresses and update the dev pane list:

```bash
tmux list-panes -t "${SESSION_NAME}:0" -F "#{session_name}:#{window_index}.#{pane_index}"
```

Use pane index 0 for the manager and indices 1 through N for dev panes. Update the answers accordingly before writing config.json.

If the user declines auto-creation, proceed with the addresses they provided (they may create the panes manually later).

### Step 1: Write config.json

Use `jq -n` to safely construct the JSON. This prevents injection from special characters (quotes, newlines) in user answers, and writes atomically via temp file + mv to prevent partial reads by concurrent scripts.

```bash
mkdir -p ~/.claude/tmux-panes
jq -n \
  --arg manager "<MANAGER_PANE>" \
  --arg dev_csv "<DEV_PANES_CSV>" \
  --arg state_dir "<STATE_DIR>" \
  '{panes: {manager: $manager, dev: ($dev_csv | split(",") | map(gsub("^ +| +$"; "")) | map(select(length > 0)))}, state_dir: $state_dir}' \
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

### Step 3: Install idle notification hooks

Install Stop hooks on all dev panes so the manager pane gets notified when any slot goes idle:

```bash
for i in $(seq 1 $NUM_DEV_PANES); do
  PANE_ADDR=$(source ${CLAUDE_PLUGIN_ROOT}/scripts/pane-lib.sh && load_config && pane_address $i)
  CHECKOUT_PATH=$(tmux display-message -t "$PANE_ADDR" -p '#{pane_current_path}')
  if [ -n "$CHECKOUT_PATH" ] && [ -d "$CHECKOUT_PATH" ]; then
    bash ${CLAUDE_PLUGIN_ROOT}/scripts/install-slot-hooks.sh $i "$CHECKOUT_PATH"
  else
    echo "⚠ Slot $i: could not detect checkout path (pane may not be active)"
  fi
done
```

This installs a Claude Code `Stop` hook in each checkout's `.claude/settings.json`. When a dev slot finishes responding, the hook:
1. Updates pane state with a timestamp
2. Sends a `tmux display-message` notification to the manager pane
3. Writes to `/tmp/mop-notifications.log`

**Note:** If a pane doesn't have a Claude Code session running yet, the hook can be installed later during handoff (Step 2.5 of `pane-handoff`).

### Step 4: Verify

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/get-pane-status.sh
```

Show the status table to confirm setup worked.

### Step 5: Report

Show a summary:
- Manager pane address
- Dev pane addresses
- State directory
- Config file location: `~/.claude/tmux-panes/config.json`

Tell the user: "Setup complete. All `/master-of-panes:pane-*` commands will use this configuration. Idle notification hooks are installed — you'll see 🔔 notifications in the manager pane when dev slots finish work."
