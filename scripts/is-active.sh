#!/bin/bash
# Check if a Claude Code tmux slot is actively processing.
#
# Exit codes:
#   0 = ACTIVE (slot is processing)
#   1 = IDLE   (slot is waiting for input)
#   2 = ERROR  (cannot determine — tmux failure, bad pane, etc.)
#
# Detection methods:
#   1. Chevron color: Claude Code grays out the ❯ when processing.
#      IDLE  = white ❯ (default terminal color, no 38;2 color code)
#      ACTIVE = gray ❯ (38;2;153;153;153)
#   2. Content change: Captures pane content twice, 1.5s apart.
#      If content changed → ACTIVE (catches background agents).
#
# Usage:
#   is-active.sh <slot>           # Exit 0=active, 1=idle, 2=error
#   is-active.sh <slot> -v        # Verbose output
#   is-active.sh <slot> --debug   # Full debug with raw ANSI dump
#   is-active.sh <slot> --fast    # Chevron-only (skip content change)

SLOT="${1:?Usage: is-active.sh <slot> [-v|--debug|--fast]}"
FLAG="${2:-}"

# Validate slot
if ! [[ "$SLOT" =~ ^[1-4]$ ]]; then
  echo "ERROR: Slot must be 1-4, got: $SLOT" >&2
  exit 2
fi

PANE="0:0.$SLOT"

# Capture pane with ANSI escape codes for color detection
output=$(tmux capture-pane -e -t "$PANE" -p 2>/dev/null)
if [ -z "$output" ]; then
  [ "$FLAG" = "-v" ] || [ "$FLAG" = "--debug" ] && echo "ERROR: Could not capture pane $PANE"
  exit 2
fi

# Find the ❯ chevron line (last occurrence)
chevron_line=$(echo "$output" | grep '❯' | tail -1)

if [ "$FLAG" = "--debug" ]; then
  echo "=== Raw ❯ line ==="
  echo "$chevron_line" | cat -v
  echo ""
fi

# No ❯ found — unusual state, assume active
if [ -z "$chevron_line" ]; then
  [ "$FLAG" = "-v" ] || [ "$FLAG" = "--debug" ] && echo "RESULT: ACTIVE (no ❯ found)"
  exit 0
fi

# Method 1: Check if ❯ is grayed out (153;153;153 = Claude is processing)
if echo "$chevron_line" | cat -v | grep -q '38;2;153;153;153.*M-bM-\^]M-/'; then
  [ "$FLAG" = "-v" ] || [ "$FLAG" = "--debug" ] && echo "RESULT: ACTIVE (gray ❯)"
  exit 0
else
  [ "$FLAG" = "-v" ] || [ "$FLAG" = "--debug" ] && echo "Chevron is white (idle)"
fi

# Fast mode: skip content change detection
if [ "$FLAG" = "--fast" ]; then
  [ "$FLAG" = "-v" ] && echo "RESULT: IDLE"
  exit 1
fi

# Method 2: Content change detection (catches background agents with no spinner)
# Capture content area twice, 1.5s apart. Exclude bottom 6 lines (status bar area).
plain1=$(tmux capture-pane -t "$PANE" -p 2>/dev/null)
if [ -z "$plain1" ]; then
  [ "$FLAG" = "-v" ] || [ "$FLAG" = "--debug" ] && echo "ERROR: Second capture failed"
  exit 2
fi
total1=$(echo "$plain1" | wc -l | tr -d ' ')
keep1=$((total1 - 6))
if [ "$keep1" -gt 0 ]; then
  hash1=$(echo "$plain1" | head -n "$keep1" | md5)
else
  hash1=$(echo "$plain1" | md5)
fi

sleep 1.5

plain2=$(tmux capture-pane -t "$PANE" -p 2>/dev/null)
if [ -z "$plain2" ]; then
  [ "$FLAG" = "-v" ] || [ "$FLAG" = "--debug" ] && echo "ERROR: Third capture failed"
  exit 2
fi
total2=$(echo "$plain2" | wc -l | tr -d ' ')
keep2=$((total2 - 6))
if [ "$keep2" -gt 0 ]; then
  hash2=$(echo "$plain2" | head -n "$keep2" | md5)
else
  hash2=$(echo "$plain2" | md5)
fi

if [ "$FLAG" = "-v" ] || [ "$FLAG" = "--debug" ]; then
  echo ""
  echo "=== Content change detection ==="
  if [ "$FLAG" = "--debug" ]; then
    echo "Hash T=0.0s: $hash1 (lines: $keep1)"
    echo "Hash T=1.5s: $hash2 (lines: $keep2)"
  fi
fi

if [ "$hash1" != "$hash2" ]; then
  [ "$FLAG" = "-v" ] || [ "$FLAG" = "--debug" ] && echo "RESULT: ACTIVE (content changing)"
  exit 0
else
  [ "$FLAG" = "-v" ] || [ "$FLAG" = "--debug" ] && echo "RESULT: IDLE"
  exit 1
fi
