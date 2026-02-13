#!/bin/bash
# Update pane state — release, set session_id, or update activity timestamp.
#
# Used by hooks (Stop auto-cleanup) and skills (manual management).
#
# Usage:
#   update-pane-state.sh <pane> --release              # Mark pane as free
#   update-pane-state.sh <pane> --session <id>         # Set session ID
#   update-pane-state.sh <pane> --activity             # Update last_activity
#   update-pane-state.sh --cleanup-session <id>        # Find and release pane by session ID

source "$(dirname "$0")/pane-lib.sh"
require_jq
load_config

PANE_NUM="$1"
ACTION="$2"
VALUE="$3"

# Handle --cleanup-session (no pane number needed, scans all panes)
if [ "$PANE_NUM" = "--cleanup-session" ]; then
  SESSION_ID="$ACTION"
  if [ -z "$SESSION_ID" ]; then
    echo "Usage: update-pane-state.sh --cleanup-session <session_id>" >&2
    exit 1
  fi

  for i in $(seq 1 "$NUM_DEV_PANES"); do
    STATE_FILE="$PANE_STATE_DIR/pane-${i}.json"
    [ ! -f "$STATE_FILE" ] && continue
    file_session=$(jq -r '.session_id // ""' "$STATE_FILE" 2>/dev/null)
    if [ "$file_session" = "$SESSION_ID" ]; then
      echo "Found session $SESSION_ID in pane $i — releasing"
      acquire_pane_lock "$i"
      NOW=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
      if ! safe_jq_update "$STATE_FILE" --arg now "$NOW" \
        '.occupied = false | .session_id = null | .task = null | .branch = null | .assigned_at = null | .last_activity = $now'; then
        exit 1
      fi
      echo "Pane $i released"
      exit 0
    fi
  done

  echo "No pane found with session_id: $SESSION_ID"
  exit 0
fi

# Standard usage requires pane number
if [ -z "$PANE_NUM" ] || [ -z "$ACTION" ]; then
  echo "Usage:" >&2
  echo "  update-pane-state.sh <pane> --release" >&2
  echo "  update-pane-state.sh <pane> --session <id>" >&2
  echo "  update-pane-state.sh <pane> --activity" >&2
  echo "  update-pane-state.sh --cleanup-session <session_id>" >&2
  exit 1
fi

validate_pane "$PANE_NUM"
STATE_FILE="$PANE_STATE_DIR/pane-${PANE_NUM}.json"

if [ ! -f "$STATE_FILE" ]; then
  echo "ERROR: State file not found: $STATE_FILE" >&2
  exit 1
fi

# Acquire lock before any read-modify-write
acquire_pane_lock "$PANE_NUM"
NOW=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

case "$ACTION" in
  --release)
    if ! safe_jq_update "$STATE_FILE" --arg now "$NOW" \
      '.occupied = false | .session_id = null | .task = null | .branch = null | .assigned_at = null | .last_activity = $now'; then
      exit 1
    fi
    echo "Pane $PANE_NUM released"
    ;;

  --session)
    if [ -z "$VALUE" ]; then
      echo "Usage: update-pane-state.sh <pane> --session <id>" >&2
      exit 1
    fi
    if ! safe_jq_update "$STATE_FILE" --arg sid "$VALUE" --arg now "$NOW" \
      '.session_id = $sid | .last_activity = $now'; then
      exit 1
    fi
    echo "Pane $PANE_NUM session set: $VALUE"
    ;;

  --activity)
    if ! safe_jq_update "$STATE_FILE" --arg now "$NOW" \
      '.last_activity = $now'; then
      exit 1
    fi
    echo "Pane $PANE_NUM activity updated"
    ;;

  *)
    echo "Unknown action: $ACTION" >&2
    exit 1
    ;;
esac
