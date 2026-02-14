#!/bin/bash
# Display status of all tmux panes as an ASCII table.
#
# Reads state files from ~/.claude/tmux-panes/ and optionally
# checks live activity via is-active.sh.
#
# GHOST TEXT SAFETY: The --live flag uses is-active.sh for activity detection,
# which is immune to ghost text (uses chevron color + content hashing, never
# reads prompt-line text). This script does NOT read or report any text from
# the ❯ prompt line. Status is derived from JSON state files and color-based
# activity detection only.
#
# Usage:
#   get-pane-status.sh              # Status from state files only
#   get-pane-status.sh --live       # Also check live tmux activity

source "$(dirname "$0")/pane-lib.sh"
require_jq
load_config
require_tmux

FLAG="${1:-}"

# Ensure state directory and all pane files exist
for i in $(seq 1 "$NUM_DEV_PANES"); do
  ensure_pane_state "$i"
done

# Print header
echo "╔══════╤══════════╤══════════╤══════════════════════════════╤════════════════════╗"
echo "║ Pane │ Address  │ Status   │ Task                         │ Branch             ║"
echo "╠══════╪══════════╪══════════╪══════════════════════════════╪════════════════════╣"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Manager row
printf "║  M   │ %-8s │ %-8s │ %-28s │ %-18s ║\n" "$MANAGER_PANE" "MANAGER" "—" "—"

# Dev pane rows
for i in $(seq 1 "$NUM_DEV_PANES"); do
  STATE_FILE="$PANE_STATE_DIR/pane-${i}.json"
  PANE_ADDR=$(pane_address "$i")

  occupied=$(jq -r '.occupied // false' "$STATE_FILE" 2>/dev/null)
  task=$(jq -r '.task // "-"' "$STATE_FILE" 2>/dev/null)
  branch=$(jq -r '.branch // "-"' "$STATE_FILE" 2>/dev/null)

  # Determine status
  if [ "$FLAG" = "--live" ]; then
    # Live activity detection — always run regardless of state file
    "$SCRIPT_DIR/is-active.sh" "$i" --fast 2>/dev/null
    rc=$?
    if [ $rc -eq 0 ]; then
      status="ACTIVE  "
    elif [ $rc -eq 2 ]; then
      status="ERROR   "
    elif [ "$occupied" = "true" ]; then
      status="IDLE    "
    else
      status="FREE    "
    fi
  elif [ "$occupied" = "true" ]; then
    status="OCCUPIED"
  else
    status="FREE    "
  fi

  # Truncate long strings
  task_display=$(echo "$task" | cut -c1-28)
  branch_display=$(echo "$branch" | cut -c1-18)

  printf "║  %d   │ %-8s │ %-8s │ %-28s │ %-18s ║\n" "$i" "$PANE_ADDR" "$status" "$task_display" "$branch_display"
done

echo "╚══════╧══════════╧══════════╧══════════════════════════════╧════════════════════╝"
