#!/bin/bash
# Send a command to a Claude Code tmux slot and block until it finishes.
#
# Three-phase flow:
#   1. Send command to slot via send-to-slot.sh
#   2. Wait for slot to become active (confirms command was picked up)
#   3. Poll until slot goes idle (command completed)
#
# Exit codes:
#   0 = Slot went idle (command completed)
#   1 = Error (bad slot, pane not found, send failed)
#   2 = Timeout waiting for activation (command may not have been picked up)
#   3 = Timeout waiting for completion
#
# Usage:
#   run-and-wait.sh <slot> <command> [--timeout 600] [--poll 5]
#
# Examples:
#   run-and-wait.sh 1 '/review-and-pr' --timeout 600
#   run-and-wait.sh 2 '/git-sync-main' --timeout 60
#   run-and-wait.sh 1 'commit this and push' --timeout 300
#
#   # Chain commands sequentially:
#   run-and-wait.sh 1 '/git-sync-main' --timeout 60 && \
#   run-and-wait.sh 1 '/clear' --timeout 15 && \
#   send-to-slot.sh 1 "start working on the feature"

SLOT="${1:?Usage: run-and-wait.sh <slot> <command> [--timeout 600] [--poll 5]}"
COMMAND="${2:?Provide a command to send}"
shift 2

# Validate slot
if ! [[ "$SLOT" =~ ^[1-4]$ ]]; then
  echo "ERROR: Slot must be 1-4, got: $SLOT" >&2
  exit 1
fi

TIMEOUT=600   # Max seconds to wait for completion
POLL=5        # Seconds between idle checks
ACTIVATION_TIMEOUT=30  # Max seconds to wait for slot to become active

while [[ $# -gt 0 ]]; do
  case "$1" in
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --poll)    POLL="$2"; shift 2 ;;
    *) echo "Unknown flag: $1" >&2; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SEND="$SCRIPT_DIR/send-to-slot.sh"
IS_ACTIVE="$SCRIPT_DIR/is-active.sh"
CAPTURE="$SCRIPT_DIR/capture-output.sh"
PANE="0:0.$SLOT"

# Verify pane exists
if ! tmux has-session -t 0 2>/dev/null; then
  echo "ERROR: tmux session 0 not found" >&2
  exit 1
fi
if ! tmux list-panes -t 0:0 -F '#{pane_index}' 2>/dev/null | grep -q "^${SLOT}$"; then
  echo "ERROR: Pane $PANE not found" >&2
  exit 1
fi

# ── Phase 1: Send command ───────────────────────────────
echo "Sending to slot $SLOT: $COMMAND" >&2
if ! "$SEND" "$SLOT" "$COMMAND" 2>/dev/null; then
  echo "ERROR: Failed to send command" >&2
  exit 1
fi

# ── Phase 2: Wait for activation ────────────────────────
# Use --fast (chevron-only) for quick checks during activation wait
echo "Waiting for slot $SLOT to become active..." >&2
elapsed=0
activated=false
while [ $elapsed -lt $ACTIVATION_TIMEOUT ]; do
  "$IS_ACTIVE" "$SLOT" --fast 2>/dev/null
  rc=$?
  if [ $rc -eq 0 ]; then
    activated=true
    break
  fi
  if [ $rc -eq 2 ]; then
    echo "ERROR: Cannot check activity for slot $SLOT" >&2
    exit 1
  fi
  sleep 2
  elapsed=$((elapsed + 2))
done

# One more check with full detection (catches background agents)
if [ "$activated" = false ]; then
  "$IS_ACTIVE" "$SLOT" 2>/dev/null
  rc=$?
  if [ $rc -eq 0 ]; then
    activated=true
  elif [ $rc -eq 2 ]; then
    echo "ERROR: Cannot check activity for slot $SLOT" >&2
    exit 1
  fi
fi

if [ "$activated" = false ]; then
  echo "Slot $SLOT never became active (${ACTIVATION_TIMEOUT}s)" >&2
  "$CAPTURE" "$SLOT" 10 2>/dev/null
  exit 2
fi

echo "Slot $SLOT is active" >&2

# ── Phase 3: Wait for idle ──────────────────────────────
# Full detection (color + content change) to avoid false idle.
# Require 2 consecutive idle checks to confirm — avoids false positives
# during brief pauses between tool calls.
echo "Waiting for completion (timeout: ${TIMEOUT}s, poll: ${POLL}s)..." >&2
elapsed=0
consecutive_idle=0
while [ $elapsed -lt $TIMEOUT ]; do
  sleep "$POLL"
  elapsed=$((elapsed + POLL))

  "$IS_ACTIVE" "$SLOT" 2>/dev/null
  rc=$?
  if [ $rc -eq 2 ]; then
    echo "ERROR: Activity detection failed at ${elapsed}s" >&2
    exit 1
  fi

  if [ $rc -eq 0 ]; then
    consecutive_idle=0
    if [ $((elapsed % 60)) -eq 0 ]; then
      echo "  Still running... (${elapsed}s)" >&2
    fi
  else
    consecutive_idle=$((consecutive_idle + 1))
    if [ $consecutive_idle -ge 2 ]; then
      echo "Slot $SLOT is idle after ${elapsed}s" >&2
      "$CAPTURE" "$SLOT" 10 2>/dev/null
      exit 0
    fi
  fi
done

echo "Timeout after ${TIMEOUT}s" >&2
"$CAPTURE" "$SLOT" 10 2>/dev/null
exit 3
