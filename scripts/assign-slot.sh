#!/bin/bash
# Assign a task to a tmux slot.
#
# Updates the slot's state file to mark it as occupied with the given task.
# Optionally spawns a new Claude Code session in the slot's pane.
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

STATE_DIR="$HOME/.claude/tmux-slots"
STATE_FILE="$STATE_DIR/slot-${SLOT}.json"
PANE="0:0.$SLOT"

# Ensure state directory exists
mkdir -p "$STATE_DIR"

# Create state file if missing
if [ ! -f "$STATE_FILE" ]; then
  cat > "$STATE_FILE" << EOF
{
  "slot": $SLOT,
  "occupied": false,
  "pane": "$PANE",
  "session_id": null,
  "task": null,
  "branch": null,
  "assigned_at": null,
  "last_activity": null
}
EOF
fi

# Check if slot is already occupied
occupied=$(jq -r '.occupied // false' "$STATE_FILE" 2>/dev/null)
if [ "$occupied" = "true" ]; then
  current_task=$(jq -r '.task // "unknown"' "$STATE_FILE" 2>/dev/null)
  echo "ERROR: Slot $SLOT is already occupied with: $current_task" >&2
  echo "Use update-slot-state.sh $SLOT --release to free it first." >&2
  exit 1
fi

# Verify tmux pane exists
if ! tmux list-panes -t "0:0" -F '#{pane_index}' 2>/dev/null | grep -q "^${SLOT}$"; then
  echo "ERROR: tmux pane 0:0.$SLOT does not exist" >&2
  exit 1
fi

# Update state file
NOW=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
jq \
  --arg task "$TASK" \
  --arg branch "$BRANCH" \
  --arg now "$NOW" \
  '.occupied = true | .task = $task | .branch = (if $branch == "" then null else $branch end) | .assigned_at = $now | .last_activity = $now' \
  "$STATE_FILE" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "$STATE_FILE"

echo "Slot $SLOT assigned: $TASK"
[ -n "$BRANCH" ] && echo "  Branch: $BRANCH"
echo "  Pane: $PANE"
echo "  State: $STATE_FILE"
