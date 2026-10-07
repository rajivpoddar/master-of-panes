#!/bin/bash
# Check if a Claude Code tmux slot is actively processing.
#
# Detection: Claude Code grays out the ❯ chevron when processing.
#   - IDLE:   ❯ is white (default terminal color, no 38;2 color code)
#   - ACTIVE: ❯ is gray (38;2;153;153;153)
#
# This is the most reliable single signal. We also keep content change
# detection as a fallback for edge cases (background agents with no spinner).
#
# Usage:
#   is-active.sh <slot>           # Exit 0 = active, 1 = idle
#   is-active.sh <slot> -v        # Verbose: show what it sees
#   is-active.sh <slot> --debug   # Full debug: dump raw ANSI of ❯ line
#   is-active.sh <slot> --fast    # Chevron-only check (skip content change detection)

#   is-active.sh <slot> [flag] --pane-id %N   # pin to an already verified pane
#
# v2 (MoP 2026-10-07): exit 0 = ACTIVE, 1 = IDLE, 2 = UNKNOWN. The pane is the
# slot's verified immutable pane id from slot-pane.sh (checkout-bound), never
# the index formula; an unknown, missing or rebound identity exits 2 so that
# callers fail closed (2 is never "idle").

SLOT="${1:?Usage: is-active.sh <slot> [-v|--debug|--fast] [--pane-id %N]}"
shift
FLAG=""
PIN=""
while [ $# -gt 0 ]; do
  case "$1" in
    --pane-id) PIN="${2:-}"; shift ;;
    *) [ -z "$FLAG" ] && FLAG="$1" ;;
  esac
  shift
done
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -n "$PIN" ]; then
  PANE=$("$SCRIPT_DIR/slot-pane.sh" "$SLOT" --pane-id "$PIN" 2>/dev/null)
else
  PANE=$("$SCRIPT_DIR/slot-pane.sh" "$SLOT" 2>/dev/null)
fi
if [ -z "$PANE" ]; then
  [ "$FLAG" = "-v" ] || [ "$FLAG" = "--debug" ] && echo "RESULT: UNKNOWN (slot $SLOT pane identity unverified)"
  exit 2
fi

# Capture pane with ANSI escape codes
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

# If no ❯ found at all, assume active (unusual state)
if [ -z "$chevron_line" ]; then
  [ "$FLAG" = "-v" ] || [ "$FLAG" = "--debug" ] && echo "RESULT: ACTIVE (no ❯ found)"
  exit 0
fi

# Method 1: Check if ❯ is grayed out (153;153;153 = active)
# When idle, ❯ uses default terminal color (no 38;2 code before it).
# When active, Claude Code explicitly sets it to gray 153;153;153.
if echo "$chevron_line" | cat -v | grep -q '38;2;153;153;153.*M-bM-\^]M-/'; then
  [ "$FLAG" = "-v" ] || [ "$FLAG" = "--debug" ] && echo "RESULT: ACTIVE (gray ❯)"
  exit 0
else
  if [ "$FLAG" = "-v" ] || [ "$FLAG" = "--debug" ]; then
    echo "Chevron is white (idle)"
  fi
fi

# Fast mode: skip content change detection
if [ "$FLAG" = "--fast" ]; then
  [ "$FLAG" = "-v" ] && echo "RESULT: IDLE"
  exit 1
fi

# Method 2: Content change detection (catches background agents)
# Capture content area twice, 1.5s apart. Exclude bottom 6 lines (status area).
plain1=$(tmux capture-pane -t "$PANE" -p 2>/dev/null)
total1=$(echo "$plain1" | wc -l | tr -d ' ')
keep1=$((total1 - 6))
if [ "$keep1" -gt 0 ]; then
  hash1=$(echo "$plain1" | head -n "$keep1" | md5)
else
  hash1=$(echo "$plain1" | md5)
fi

sleep 1.5

plain2=$(tmux capture-pane -t "$PANE" -p 2>/dev/null)
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
