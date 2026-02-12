#!/bin/bash
# Assign a task to a tmux slot.
#
# Updates the slot's state file to mark it as occupied with the given task.
#
# Usage:
#   assign-slot.sh <slot> <task> [branch]
#
# Examples:
#   assign-slot.sh 1 "Fix login bug"
#   assign-slot.sh 2 "Add search feature" feature/search

SLOT="${1:?Usage: assign-slot.sh <slot> <task> [branch]}"
TASK="${2:?Usage: assign-slot.sh <slot> <task> [branch]}"
BRANCH="${3:-}"

source "$(dirname "$0")/slot-lib.sh"
require_jq
validate_slot "$SLOT"

STATE_FILE="$SLOT_STATE_DIR/slot-${SLOT}.json"
PANE="0:0.$SLOT"

ensure_state_file "$SLOT"

# Verify tmux pane exists (outside lock — read-only check)
if ! tmux list-panes -t "0:0" -F '#{pane_index}' 2>/dev/null | grep -q "^${SLOT}$"; then
  echo "ERROR: tmux pane 0:0.$SLOT does not exist" >&2
  exit 1
fi

# Acquire exclusive lock for atomic check-and-assign
acquire_slot_lock "$SLOT"

# Check if slot is already occupied (inside lock)
occupied=$(jq -r '.occupied // false' "$STATE_FILE" 2>/dev/null)
if [ "$occupied" = "true" ]; then
  current_task=$(jq -r '.task // "unknown"' "$STATE_FILE" 2>/dev/null)
  echo "ERROR: Slot $SLOT is already occupied with: $current_task" >&2
  echo "Use update-slot-state.sh $SLOT --release to free it first." >&2
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

echo "Slot $SLOT assigned: $TASK"
[ -n "$BRANCH" ] && echo "  Branch: $BRANCH"
echo "  Pane: $PANE"
echo "  State: $STATE_FILE"
