#!/bin/bash
# Create a standard Master of Panes tmux layout.
# Usage: scripts/create-layout.sh [session_name] [num_dev_panes]
#
# Creates a tmux session with 1 manager pane + N dev panes in a tiled layout.

set -e

SESSION="${1:-claude}"
NUM_PANES="${2:-4}"

# Source lib for shared helpers
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/pane-lib.sh"

if ! command -v tmux &>/dev/null; then
  echo "ERROR: tmux not installed. Install with: $(_install_hint tmux)" >&2
  exit 1
fi

# Create session (or reuse existing)
if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "Session '$SESSION' already exists. Using it."
else
  tmux new-session -d -s "$SESSION"
  echo "Created session: $SESSION"
fi

# Create dev panes by splitting
WINDOW="${SESSION}:0"
for i in $(seq 1 "$NUM_PANES"); do
  if [ $((i % 2)) -eq 1 ]; then
    tmux split-window -t "$WINDOW" -h
  else
    tmux split-window -t "$WINDOW" -v
  fi
done

# Tiled layout for even distribution
tmux select-layout -t "$WINDOW" tiled

# List created panes
echo ""
echo "Created layout:"
tmux list-panes -t "$WINDOW" -F "  Pane #{pane_index}: #{session_name}:#{window_index}.#{pane_index}"

echo ""
echo "Manager pane: ${SESSION}:0.0"
echo "Dev panes: ${SESSION}:0.1 through ${SESSION}:0.${NUM_PANES}"
echo ""
echo "Next: Run /master-of-panes:pane-setup to configure."
