#!/bin/bash
# Send a command to a Claude Code tmux slot and block until it finishes.
#
# Usage:
#   run-and-wait.sh <slot> <command> [--timeout 600] [--poll 5]
#
# Flow:
#   1. Send <command> to slot via send-to-slot.sh
#   2. Wait for slot to become active (spinner detected)
#   3. Poll until slot goes idle (no spinner, no content changes)
#   4. Print final state JSON (from check-slot.sh) and exit
#
# Exit codes:
#   0 = Slot went idle (command completed)
#   1 = Error (pane not found, send failed)
#   2 = Timeout waiting for activation (command may not have been picked up)
#   3 = Timeout waiting for completion
#
# Examples:
#   # Send /review-and-pr and wait up to 10 min
#   run-and-wait.sh 1 '/review-and-pr' --timeout 600
#
#   # Send /git-sync-main and wait (quick command)
#   run-and-wait.sh 2 '/git-sync-main' --timeout 60
#
#   # Send a freeform instruction
#   run-and-wait.sh 1 'commit this and push' --timeout 300
#
#   # Use in a pipeline: run command, then do something with result
#   run-and-wait.sh 1 '/review-and-pr' && echo "Done!"
#
#   # Chain commands: sync, then handoff
#   run-and-wait.sh 1 '/git-sync-main' --timeout 60 && \
#   run-and-wait.sh 1 '/clear' --timeout 15 && \
#   send-to-slot.sh 1 --file /tmp/handoff-1121.md

set -euo pipefail

# ── Args ─────────────────────────────────────────────────
SLOT="${1:?Usage: run-and-wait.sh <slot> <command> [--timeout 600] [--poll 5]}"
COMMAND="${2:?Provide a command to send}"
shift 2

TIMEOUT=600   # Max seconds to wait for completion
POLL=5        # Seconds between idle checks
ACTIVATION_TIMEOUT=30  # Max seconds to wait for slot to become active

while [[ $# -gt 0 ]]; do
  case "$1" in
    --timeout) TIMEOUT="$2"; shift 2;;
    --poll)    POLL="$2"; shift 2;;
    *) echo "Unknown flag: $1" >&2; exit 1;;
  esac
done

# ── Paths ────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SEND="$SCRIPT_DIR/send-to-slot.sh"
IS_ACTIVE="$SCRIPT_DIR/is-active.sh"
CHECK_SLOT="$HOME/.claude/skills/monitor-slot/scripts/check-slot.sh"
# ── Verify pane identity (checkout-bound pane id; fail closed) ──
PANE=$("$SCRIPT_DIR/slot-pane.sh" "$SLOT" 2>/dev/null)
if [ -z "$PANE" ]; then
  echo "ERROR: slot $SLOT pane identity unknown (slot-pane.sh)" >&2
  exit 1
fi

# ── Phase 1: Send command ───────────────────────────────
echo "⏵ Sending to slot $SLOT: $COMMAND" >&2
"$SEND" "$SLOT" "$COMMAND" 2>/dev/null
if [ $? -ne 0 ]; then
  echo "ERROR: Failed to send command" >&2
  exit 1
fi

# ── Phase 2: Wait for activation ────────────────────────
# Use --fast (color-only) for quick checks during activation wait
echo "⏳ Waiting for slot $SLOT to become active..." >&2
elapsed=0
activated=false
while [ $elapsed -lt $ACTIVATION_TIMEOUT ]; do
  if "$IS_ACTIVE" "$SLOT" --fast --pane-id "$PANE" 2>/dev/null; then
    activated=true
    break
  fi
  sleep 2
  elapsed=$((elapsed + 2))
done

if [ "$activated" = false ]; then
  # One more check with full detection (catches background agents)
  if "$IS_ACTIVE" "$SLOT" --pane-id "$PANE" 2>/dev/null; then
    activated=true
  fi
fi

if [ "$activated" = false ]; then
  echo "⚠️  Slot $SLOT never became active (${ACTIVATION_TIMEOUT}s)" >&2
  # Still output current state for caller to inspect
  "$CHECK_SLOT" "$SLOT" 2>/dev/null
  exit 2
fi

echo "✓ Slot $SLOT is active" >&2

# ── Phase 3: Wait for idle ──────────────────────────────
# Full detection (color + content change) to avoid false idle
echo "⏳ Waiting for completion (timeout: ${TIMEOUT}s, poll: ${POLL}s)..." >&2
elapsed=0
consecutive_idle=0
while [ $elapsed -lt $TIMEOUT ]; do
  sleep "$POLL"
  elapsed=$((elapsed + POLL))

  "$IS_ACTIVE" "$SLOT" --pane-id "$PANE" 2>/dev/null
  if [ $? -ne 1 ]; then
    consecutive_idle=0
    if [ $((elapsed % 60)) -eq 0 ]; then
      echo "  Still running... (${elapsed}s)" >&2
    fi
  else
    consecutive_idle=$((consecutive_idle + 1))
    # Require 2 consecutive idle checks to confirm (avoids false positives
    # during brief pauses between tool calls)
    if [ $consecutive_idle -ge 2 ]; then
      echo "✓ Slot $SLOT is idle after ${elapsed}s" >&2
      "$CHECK_SLOT" "$SLOT" 2>/dev/null
      exit 0
    fi
  fi
done

echo "⚠️  Timeout after ${TIMEOUT}s" >&2
"$CHECK_SLOT" "$SLOT" 2>/dev/null
exit 3
