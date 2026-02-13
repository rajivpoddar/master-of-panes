#!/bin/bash
# Capture recent output from a tmux dev pane.
#
# Captures the visible pane content, optionally with scrollback history.
# Useful for monitoring what a pane is doing.
#
# GHOST TEXT SAFETY: By default, output is stripped of the ❯ prompt line and
# everything below it. Ghost text (autocomplete predictions) on the prompt line
# is indistinguishable from real input in plain capture-pane output and has
# caused accidental PR merges, false status reports, and unauthorized command
# triggers. Use --raw to include the prompt line (only for debugging).
#
# Usage:
#   capture-output.sh <pane>              # Last 20 lines (prompt-stripped)
#   capture-output.sh <pane> <lines>      # Last N lines (prompt-stripped)
#   capture-output.sh <pane> --full       # Full visible pane (prompt-stripped)
#   capture-output.sh <pane> --raw        # Last 20 lines (includes prompt line)
#   capture-output.sh <pane> <lines> --raw  # Last N lines (includes prompt line)

source "$(dirname "$0")/pane-lib.sh"
load_config

PANE_NUM="${1:?Usage: capture-output.sh <pane> [lines|--full] [--raw]}"
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

validate_pane "$PANE_NUM"

PANE_ADDR=$(pane_address "$PANE_NUM")

# Verify pane exists
if ! pane_exists "$PANE_ADDR"; then
  echo "ERROR: tmux pane $PANE_ADDR not found" >&2
  exit 1
fi

# Capture pane content
raw_output=$(tmux capture-pane -t "$PANE_ADDR" -p)

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
