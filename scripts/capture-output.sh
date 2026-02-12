#!/bin/bash
# Capture recent output from a tmux slot.
#
# Captures the visible pane content, optionally with scrollback history.
# Useful for monitoring what a slot is doing.
#
# GHOST TEXT SAFETY: By default, output is stripped of the ❯ prompt line and
# everything below it. Ghost text (autocomplete predictions) on the prompt line
# is indistinguishable from real input in plain capture-pane output and has
# caused accidental PR merges, false status reports, and unauthorized command
# triggers. Use --raw to include the prompt line (only for debugging).
#
# Usage:
#   capture-output.sh <slot>              # Last 20 lines (prompt-stripped)
#   capture-output.sh <slot> <lines>      # Last N lines (prompt-stripped)
#   capture-output.sh <slot> --full       # Full visible pane (prompt-stripped)
#   capture-output.sh <slot> --raw        # Last 20 lines (includes prompt line)
#   capture-output.sh <slot> <lines> --raw  # Last N lines (includes prompt line)

source "$(dirname "$0")/slot-lib.sh"

SLOT="${1:?Usage: capture-output.sh <slot> [lines|--full] [--raw]}"
shift

LINES="20"
RAW=""

for arg in "$@"; do
  case "$arg" in
    --full) LINES="--full" ;;
    --raw)  RAW="--raw" ;;
    *)      LINES="$arg" ;;
  esac
done

# Validate slot
if ! [[ "$SLOT" =~ ^[1-4]$ ]]; then
  echo "ERROR: Slot must be 1-4, got: $SLOT" >&2
  exit 1
fi

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

# Capture pane content
raw_output=$(tmux capture-pane -t "$PANE" -p)

# Strip ghost text (prompt line + below) unless --raw
if [ "$RAW" = "--raw" ]; then
  safe_output="$raw_output"
else
  safe_output=$(echo "$raw_output" | strip_prompt_line)
fi

if [ "$LINES" = "--full" ]; then
  echo "$safe_output"
else
  echo "$safe_output" | tail -n "$LINES"
fi
