#!/bin/bash
# Capture recent output from a tmux slot.
#
# Captures the visible pane content, optionally with scrollback history.
# Useful for monitoring what a slot is doing.
#
# Usage:
#   capture-output.sh <slot>              # Last 20 lines
#   capture-output.sh <slot> <lines>      # Last N lines
#   capture-output.sh <slot> --full       # Full visible pane

SLOT="${1:?Usage: capture-output.sh <slot> [lines|--full]}"
LINES="${2:-20}"
PANE="0:0.$SLOT"

# Verify pane exists
if ! tmux has-session -t "0" 2>/dev/null; then
  echo "ERROR: tmux session 0 not found" >&2
  exit 1
fi

if ! tmux list-panes -t "0:0" -F '#{pane_index}' 2>/dev/null | grep -q "^${SLOT}$"; then
  echo "ERROR: Pane $SLOT not found in window 0:0" >&2
  exit 1
fi

if [ "$LINES" = "--full" ]; then
  # Full visible pane
  tmux capture-pane -t "$PANE" -p
else
  # Last N lines
  tmux capture-pane -t "$PANE" -p | tail -n "$LINES"
fi
