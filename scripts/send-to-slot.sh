#!/bin/bash
# Send a message/command to a Claude Code session in a tmux slot.
#
# Waits for the slot to become idle before sending. Handles INSERT/NORMAL
# mode detection for Claude Code's vim-style input.
#
# Usage:
#   send-to-slot.sh <slot> <message>           # Send and return
#   send-to-slot.sh <slot> <message> --wait    # Send and wait for completion
#   send-to-slot.sh <slot> <message> --force   # Skip idle wait (urgent corrections)

SLOT="$1"
MESSAGE="$2"
shift 2 2>/dev/null

WAIT=""
FORCE=""
for arg in "$@"; do
  case "$arg" in
    --wait)  WAIT="--wait" ;;
    --force) FORCE="--force" ;;
  esac
done

if [ -z "$SLOT" ] || [ -z "$MESSAGE" ]; then
  echo "Usage: send-to-slot.sh <slot> <message> [--wait] [--force]" >&2
  exit 1
fi

# Validate slot
if ! [[ "$SLOT" =~ ^[1-4]$ ]]; then
  echo "ERROR: Slot must be 1-4, got: $SLOT" >&2
  exit 1
fi

PANE="0:0.$SLOT"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Delegate activity check to is-active.sh
# Returns: 0=active, 1=idle, 2=error
is_slot_active() {
  "$SCRIPT_DIR/is-active.sh" "$SLOT" 2>/dev/null
}

# Wait for slot to become idle
wait_for_idle() {
  local max_wait=${1:-120}
  local count=0
  echo "Waiting for slot $SLOT to become idle..."
  while [ $count -lt $max_wait ]; do
    is_slot_active
    local rc=$?
    if [ $rc -eq 2 ]; then
      echo "ERROR: Cannot detect activity for slot $SLOT" >&2
      return 1
    fi
    if [ $rc -eq 1 ]; then
      echo "Slot $SLOT is idle"
      return 0
    fi
    sleep 2
    count=$((count + 2))
    if [ $((count % 10)) -eq 0 ]; then
      echo "  Still waiting... (${count}s)"
    fi
  done
  echo "ERROR: Timeout waiting for slot $SLOT to become idle (${max_wait}s)" >&2
  return 1
}

# Wait for Claude Code prompt to appear (command completed)
wait_for_prompt() {
  local max_wait=${1:-60}
  local count=0
  while [ $count -lt $max_wait ]; do
    if tmux capture-pane -t "$PANE" -p | tail -5 | grep -q '^❯' && \
       tmux capture-pane -t "$PANE" -p | tail -3 | grep -q 'INSERT'; then
      return 0
    fi
    sleep 1
    count=$((count + 1))
  done
  echo "ERROR: Timeout waiting for prompt in slot $SLOT" >&2
  return 1
}

# Wait for idle before sending (unless --force)
if [ "$FORCE" = "--force" ]; then
  echo "Force mode — sending immediately (skipping idle wait)"
else
  is_slot_active
  rc=$?
  if [ $rc -eq 2 ]; then
    echo "ERROR: Cannot check activity for slot $SLOT (tmux error)" >&2
    exit 1
  fi
  if [ $rc -eq 0 ]; then
    wait_for_idle 120 || exit 1
  fi
fi

# Detect INSERT/NORMAL mode and send appropriately.
# Use -l (literal) flag for message text to prevent shell metacharacters
# and tmux key binding names (e.g., C-c, Space) from being interpreted.
PANE_BOTTOM=$(tmux capture-pane -t "$PANE" -p | tail -5)
if echo "$PANE_BOTTOM" | grep -q 'INSERT'; then
  # Already in INSERT mode — send directly
  tmux send-keys -t "$PANE" -l "$MESSAGE"
  sleep 0.5
  tmux send-keys -t "$PANE" Enter
elif echo "$PANE_BOTTOM" | grep -q 'NORMAL'; then
  # In NORMAL mode — press 'i' first to enter INSERT
  tmux send-keys -t "$PANE" i
  sleep 0.3
  tmux send-keys -t "$PANE" -l "$MESSAGE"
  sleep 0.5
  tmux send-keys -t "$PANE" Enter
else
  # Can't determine mode — assume INSERT (default Claude Code state)
  tmux send-keys -t "$PANE" -l "$MESSAGE"
  sleep 0.5
  tmux send-keys -t "$PANE" Enter
fi

echo "Sent to slot $SLOT: $MESSAGE"

# If --wait, wait for command to complete and slot to become idle
if [ "$WAIT" = "--wait" ]; then
  echo "Waiting for completion..."
  if wait_for_prompt 60; then
    sleep 1
    is_slot_active
    rc=$?
    if [ $rc -eq 0 ]; then
      echo "  Slot became active again, waiting for idle..."
      wait_for_idle 120
    fi
    echo "Command completed"
  fi
fi
