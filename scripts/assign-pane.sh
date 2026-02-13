#!/bin/bash
# Assign a task to a tmux dev pane.
#
# Updates the pane's state file to mark it as occupied with the given task.
#
# Usage:
#   assign-pane.sh <pane> <task> [branch]
#
# Examples:
#   assign-pane.sh 1 "Fix login bug"
#   assign-pane.sh 2 "Add search feature" feature/search

PANE_NUM="${1:?Usage: assign-pane.sh <pane> <task> [branch]}"
TASK="${2:?Usage: assign-pane.sh <pane> <task> [branch]}"
BRANCH="${3:-}"

source "$(dirname "$0")/pane-lib.sh"
require_jq
load_config
validate_pane "$PANE_NUM"

STATE_FILE="$PANE_STATE_DIR/pane-${PANE_NUM}.json"
PANE_ADDR=$(pane_address "$PANE_NUM")

ensure_pane_state "$PANE_NUM"

# Verify tmux pane exists (outside lock — read-only check)
if ! pane_exists "$PANE_ADDR"; then
  echo "ERROR: tmux pane $PANE_ADDR does not exist" >&2
  exit 1
fi

# Acquire exclusive lock for atomic check-and-assign
acquire_pane_lock "$PANE_NUM"

# Check if pane is already occupied (inside lock)
occupied=$(jq -r '.occupied // false' "$STATE_FILE" 2>/dev/null)
if [ "$occupied" = "true" ]; then
  current_task=$(jq -r '.task // "unknown"' "$STATE_FILE" 2>/dev/null)
  echo "ERROR: Pane $PANE_NUM is already occupied with: $current_task" >&2
  echo "Use update-pane-state.sh $PANE_NUM --release to free it first." >&2
  exit 1
fi

# Update state file (still under lock)
NOW=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
if ! safe_jq_update "$STATE_FILE" \
  --arg task "$TASK" \
  --arg branch "$BRANCH" \
  --arg now "$NOW" \
  '.occupied = true | .task = $task | .branch = (if $branch == "" then null else $branch end) | .assigned_at = $now | .last_activity = $now'; then
  exit 1
fi

echo "Pane $PANE_NUM assigned: $TASK"
[ -n "$BRANCH" ] && echo "  Branch: $BRANCH"
echo "  Address: $PANE_ADDR"
echo "  State: $STATE_FILE"
