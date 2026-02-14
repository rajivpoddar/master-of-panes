#!/bin/bash
# Send a message/command to a Claude Code session in a tmux dev pane.
#
# Waits for the pane to become idle before sending. Handles INSERT/NORMAL
# mode detection for Claude Code's vim-style input.
#
# Usage:
#   send-to-pane.sh <pane> <message>           # Send and return
#   send-to-pane.sh <pane> <message> --wait    # Send and wait for completion
#   send-to-pane.sh <pane> <message> --force   # Skip idle wait (urgent corrections)

PANE_NUM="$1"
MESSAGE="$2"
shift 2 2>/dev/null

source "$(dirname "$0")/pane-lib.sh"
load_config
require_tmux

WAIT=""
FORCE=""
for arg in "$@"; do
  case "$arg" in
    --wait)  WAIT="--wait" ;;
    --force) FORCE="--force" ;;
  esac
done

if [ -z "$PANE_NUM" ] || [ -z "$MESSAGE" ]; then
  echo "Usage: send-to-pane.sh <pane> <message> [--wait] [--force]" >&2
  exit 1
fi

validate_pane "$PANE_NUM"

PANE_ADDR=$(pane_address "$PANE_NUM")
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Delegate activity check to is-active.sh
# Returns: 0=active, 1=idle, 2=error
is_pane_active() {
  "$SCRIPT_DIR/is-active.sh" "$PANE_NUM" 2>/dev/null
}

# Wait for pane to become idle
wait_for_idle() {
  local max_wait=${1:-120}
  local count=0
  echo "Waiting for pane $PANE_NUM to become idle..."
  while [ $count -lt $max_wait ]; do
    is_pane_active
    local rc=$?
    if [ $rc -eq 2 ]; then
      echo "ERROR: Cannot detect activity for pane $PANE_NUM" >&2
      return 1
    fi
    if [ $rc -eq 1 ]; then
      echo "Pane $PANE_NUM is idle"
      return 0
    fi
    sleep 2
    count=$((count + 2))
    if [ $((count % 10)) -eq 0 ]; then
      echo "  Still waiting... (${count}s)"
    fi
  done
  echo "ERROR: Timeout waiting for pane $PANE_NUM to become idle (${max_wait}s)" >&2
  return 1
}

# Wait for Claude Code prompt to appear (command completed)
wait_for_prompt() {
  local max_wait=${1:-60}
  local count=0
  while [ $count -lt $max_wait ]; do
    if tmux capture-pane -t "$PANE_ADDR" -p | tail -5 | grep -q '^❯' && \
       tmux capture-pane -t "$PANE_ADDR" -p | tail -3 | grep -q 'INSERT'; then
      return 0
    fi
    sleep 1
    count=$((count + 1))
  done
  echo "ERROR: Timeout waiting for prompt in pane $PANE_NUM" >&2
  return 1
}

# Wait for idle before sending (unless --force)
if [ "$FORCE" = "--force" ]; then
  echo "Force mode — sending immediately (skipping idle wait)"
else
  is_pane_active
  rc=$?
  if [ $rc -eq 2 ]; then
    echo "ERROR: Cannot check activity for pane $PANE_NUM (tmux error)" >&2
    exit 1
  fi
  if [ $rc -eq 0 ]; then
    wait_for_idle 120 || exit 1
  fi
fi

# Detect INSERT/NORMAL mode and send appropriately.
# Use -l (literal) flag for message text to prevent shell metacharacters
# and tmux key binding names (e.g., C-c, Space) from being interpreted.
PANE_BOTTOM=$(tmux capture-pane -t "$PANE_ADDR" -p | tail -5)
if echo "$PANE_BOTTOM" | grep -q 'INSERT'; then
  # Already in INSERT mode — send directly
  tmux send-keys -t "$PANE_ADDR" -l "$MESSAGE"
  sleep 0.5
  tmux send-keys -t "$PANE_ADDR" Enter
elif echo "$PANE_BOTTOM" | grep -q 'NORMAL'; then
  # In NORMAL mode — press 'i' first to enter INSERT
  tmux send-keys -t "$PANE_ADDR" i
  sleep 0.3
  tmux send-keys -t "$PANE_ADDR" -l "$MESSAGE"
  sleep 0.5
  tmux send-keys -t "$PANE_ADDR" Enter
else
  # Can't determine mode — assume INSERT (default Claude Code state)
  tmux send-keys -t "$PANE_ADDR" -l "$MESSAGE"
  sleep 0.5
  tmux send-keys -t "$PANE_ADDR" Enter
fi

echo "Sent to pane $PANE_NUM: $MESSAGE"

# If --wait, wait for command to complete and pane to become idle
if [ "$WAIT" = "--wait" ]; then
  echo "Waiting for completion..."
  if wait_for_prompt 60; then
    sleep 1
    is_pane_active
    rc=$?
    if [ $rc -eq 0 ]; then
      echo "  Pane became active again, waiting for idle..."
      wait_for_idle 120
    fi
    echo "Command completed"
  fi
fi
