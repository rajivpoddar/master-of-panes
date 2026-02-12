#!/bin/bash
# Display status of all tmux slots as an ASCII table.
#
# Reads state files from ~/.claude/tmux-slots/ and optionally
# checks live activity via is-active.sh.
#
# Usage:
#   get-slot-status.sh              # Status from state files only
#   get-slot-status.sh --live       # Also check live tmux activity

# Require jq
if ! command -v jq &>/dev/null; then
  echo "ERROR: jq is required but not installed. Install with: brew install jq" >&2
  exit 1
fi

STATE_DIR="$HOME/.claude/tmux-slots"
FLAG="${1:-}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Ensure state directory exists
mkdir -p "$STATE_DIR"

# Initialize state files for slots 1-4 if missing
for i in 1 2 3 4; do
  STATE_FILE="$STATE_DIR/slot-${i}.json"
  if [ ! -f "$STATE_FILE" ]; then
    cat > "$STATE_FILE" << EOF
{
  "slot": $i,
  "occupied": false,
  "pane": "0:0.$i",
  "session_id": null,
  "task": null,
  "branch": null,
  "assigned_at": null,
  "last_activity": null
}
EOF
  fi
done

# Print header
echo "╔══════╤══════════╤══════════════════════════════╤════════════════════╗"
echo "║ Slot │ Status   │ Task                         │ Branch             ║"
echo "╠══════╪══════════╪══════════════════════════════╪════════════════════╣"

for i in 1 2 3 4; do
  STATE_FILE="$STATE_DIR/slot-${i}.json"

  occupied=$(jq -r '.occupied // false' "$STATE_FILE" 2>/dev/null)
  task=$(jq -r '.task // "-"' "$STATE_FILE" 2>/dev/null)
  branch=$(jq -r '.branch // "-"' "$STATE_FILE" 2>/dev/null)

  # Determine status
  if [ "$occupied" = "true" ]; then
    status="OCCUPIED"
    # Live activity check if requested
    if [ "$FLAG" = "--live" ]; then
      if "$SCRIPT_DIR/is-active.sh" "$i" --fast 2>/dev/null; then
        status="ACTIVE  "
      else
        status="IDLE    "
      fi
    fi
  else
    status="FREE    "
  fi

  # Truncate long strings
  task_display=$(echo "$task" | cut -c1-28)
  branch_display=$(echo "$branch" | cut -c1-18)

  printf "║  %d   │ %-8s │ %-28s │ %-18s ║\n" "$i" "$status" "$task_display" "$branch_display"
done

echo "╚══════╧══════════╧══════════════════════════════╧════════════════════╝"
