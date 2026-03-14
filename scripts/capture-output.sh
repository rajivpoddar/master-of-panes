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
#
# STATUS BAR STRIPPING: By default, the Claude Code TUI status bar (bottom 3
# lines: border + model/git info + mode/permissions) is stripped from output.
# During active processing, there is no ❯ prompt — the status bar contains a
# text-based spinner that changes constantly, causing noise in captures.
# Use --keep-statusbar to include the status bar (only for debugging).

source "$(dirname "$0")/pane-lib.sh"
load_config
require_tmux

PANE_NUM="${1:?Usage: capture-output.sh <pane> [lines|--full] [--raw]}"
shift

LINES="20"
RAW=""
KEEP_STATUSBAR=""

for arg in "$@"; do
  case "$arg" in
    --full) LINES="--full" ;;
    --raw)  RAW="--raw" ;;
    --keep-statusbar) KEEP_STATUSBAR="1" ;;
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

# Strip status bar (bottom 3 lines: border + model/git + mode/permissions)
# During active processing, there's no ❯ prompt, so strip_prompt_line doesn't
# help — the status bar contains a text spinner that creates capture noise.
# When idle, strip_prompt_line already removed ❯ and below, but the status bar
# lines above ❯ (border + info) remain. This strips them too.
if [ -z "$KEEP_STATUSBAR" ] && [ "$RAW" != "--raw" ]; then
  # Count lines, strip bottom 3 (border + model info + INSERT mode line)
  line_count=$(echo "$safe_output" | wc -l | tr -d ' ')
  if [ "$line_count" -gt 3 ]; then
    keep=$((line_count - 3))
    safe_output=$(echo "$safe_output" | head -n "$keep")
  fi
fi

if [ "$LINES" = "--full" ]; then
  echo "$safe_output"
else
  echo "$safe_output" | tail -n "$LINES"
fi
