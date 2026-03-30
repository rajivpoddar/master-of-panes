#!/bin/bash
# Send a command to a Claude Code tmux pane and block until it finishes.
#
# Three-phase flow:
#   1. Send command to pane via send-to-pane.sh
#   2. Wait for pane to become active (confirms command was picked up)
#   3. Poll until pane goes idle (command completed)
#
# Exit codes:
#   0 = Pane went idle (command completed)
#   1 = Error (bad pane, pane not found, send failed)
#   2 = Timeout waiting for activation (command may not have been picked up)
#   3 = Timeout waiting for completion
#
# Usage:
#   run-and-wait.sh <pane> <command> [--timeout 600] [--poll 5]
#
# Examples:
#   run-and-wait.sh 1 '/review-and-pr' --timeout 600
#   run-and-wait.sh 2 '/git-sync-main' --timeout 60
#   run-and-wait.sh 1 'commit this and push' --timeout 300
#
#   # Chain commands sequentially:
#   run-and-wait.sh 1 '/git-sync-main' --timeout 60 && \
#   run-and-wait.sh 1 '/clear' --timeout 15 && \
#   send-to-pane.sh 1 "start working on the feature"

PANE_NUM="${1:?Usage: run-and-wait.sh <pane> <command> [--timeout 600] [--poll 5]}"
COMMAND="${2:?Provide a command to send}"
shift 2

source "$(dirname "$0")/pane-lib.sh"
load_config
validate_pane "$PANE_NUM"

TIMEOUT=600   # Max seconds to wait for completion
POLL=5        # Seconds between idle checks
ACTIVATION_TIMEOUT=30  # Max seconds to wait for pane to become active

while [[ $# -gt 0 ]]; do
  case "$1" in
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --poll)    POLL="$2"; shift 2 ;;
    *) echo "Unknown flag: $1" >&2; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SEND="$SCRIPT_DIR/send-to-pane.sh"
IS_ACTIVE="$SCRIPT_DIR/is-active.sh"
CAPTURE="$SCRIPT_DIR/capture-output.sh"
PANE_ADDR=$(pane_address "$PANE_NUM")

# Verify pane exists
if ! pane_exists "$PANE_ADDR"; then
  echo "ERROR: tmux pane $PANE_ADDR not found" >&2
  exit 1
fi

# ── Phase 1: Send command ───────────────────────────────
echo "Sending to pane $PANE_NUM: $COMMAND" >&2
if ! "$SEND" "$PANE_NUM" "$COMMAND" 2>/dev/null; then
  echo "ERROR: Failed to send command" >&2
  exit 1
fi

# ── Phase 2: Wait for activation ────────────────────────
# Use --fast (chevron-only) for quick checks during activation wait
echo "Waiting for pane $PANE_NUM to become active..." >&2
elapsed=0
activated=false
while [ $elapsed -lt $ACTIVATION_TIMEOUT ]; do
  "$IS_ACTIVE" "$PANE_NUM" --fast 2>/dev/null
  rc=$?
  if [ $rc -eq 0 ]; then
    activated=true
    break
  fi
  if [ $rc -eq 2 ]; then
    echo "ERROR: Cannot check activity for pane $PANE_NUM" >&2
    exit 1
  fi
  sleep 2
  elapsed=$((elapsed + 2))
done

# One more check with full detection (catches background agents)
if [ "$activated" = false ]; then
  "$IS_ACTIVE" "$PANE_NUM" 2>/dev/null
  rc=$?
  if [ $rc -eq 0 ]; then
    activated=true
  elif [ $rc -eq 2 ]; then
    echo "ERROR: Cannot check activity for pane $PANE_NUM" >&2
    exit 1
  fi
fi

if [ "$activated" = false ]; then
  echo "Pane $PANE_NUM never became active (${ACTIVATION_TIMEOUT}s)" >&2
  "$CAPTURE" "$PANE_NUM" 10 2>/dev/null
  exit 2
fi

echo "Pane $PANE_NUM is active" >&2

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

  "$IS_ACTIVE" "$PANE_NUM" 2>/dev/null
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
      echo "Pane $PANE_NUM is idle after ${elapsed}s" >&2
      "$CAPTURE" "$PANE_NUM" 10 2>/dev/null
      exit 0
    fi
  fi
done

echo "Timeout after ${TIMEOUT}s" >&2
"$CAPTURE" "$PANE_NUM" 10 2>/dev/null
exit 3
