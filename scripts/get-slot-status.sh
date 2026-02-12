#!/bin/bash
# Display status of all tmux slots as an ASCII table.
#
# Reads state files from ~/.claude/tmux-slots/ and optionally
# checks live activity via is-active.sh.
#
# GHOST TEXT SAFETY: The --live flag uses is-active.sh for activity detection,
# which is immune to ghost text (uses chevron color + content hashing, never
# reads prompt-line text). This script does NOT read or report any text from
# the ❯ prompt line. Status is derived from JSON state files and color-based
# activity detection only.
#
# Usage:
#   get-slot-status.sh              # Status from state files only
#   get-slot-status.sh --live       # Also check live tmux activity

source "$(dirname "$0")/slot-lib.sh"
require_jq

FLAG="${1:-}"

# Ensure state directory and all slot files exist
for i in 1 2 3 4; do
  ensure_state_file "$i"
done

# Print header
echo "╔══════╤══════════╤══════════════════════════════╤════════════════════╗"
echo "║ Slot │ Status   │ Task                         │ Branch             ║"
echo "╠══════╪══════════╪══════════════════════════════╪════════════════════╣"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

for i in 1 2 3 4; do
  STATE_FILE="$SLOT_STATE_DIR/slot-${i}.json"

  occupied=$(jq -r '.occupied // false' "$STATE_FILE" 2>/dev/null)
  task=$(jq -r '.task // "-"' "$STATE_FILE" 2>/dev/null)
  branch=$(jq -r '.branch // "-"' "$STATE_FILE" 2>/dev/null)

  # Determine status
  if [ "$occupied" = "true" ]; then
    status="OCCUPIED"
    # Live activity check if requested
    if [ "$FLAG" = "--live" ]; then
      "$SCRIPT_DIR/is-active.sh" "$i" --fast 2>/dev/null
      rc=$?
      if [ $rc -eq 0 ]; then
        status="ACTIVE  "
      elif [ $rc -eq 2 ]; then
        status="ERROR   "
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
