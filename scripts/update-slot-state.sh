#!/bin/bash
# Update slot state — release, set session_id, or update activity timestamp.
#
# Used by hooks (SessionEnd auto-cleanup) and skills (manual management).
#
# Usage:
#   update-slot-state.sh <slot> --release              # Mark slot as free
#   update-slot-state.sh <slot> --session <id>         # Set session ID
#   update-slot-state.sh <slot> --activity              # Update last_activity
#   update-slot-state.sh --cleanup-session <id>         # Find and release slot by session ID

SLOT="$1"
ACTION="$2"
VALUE="$3"

STATE_DIR="$HOME/.claude/tmux-slots"

# Handle --cleanup-session (no slot number needed)
if [ "$SLOT" = "--cleanup-session" ]; then
  SESSION_ID="$ACTION"
  if [ -z "$SESSION_ID" ]; then
    echo "Usage: update-slot-state.sh --cleanup-session <session_id>" >&2
    exit 1
  fi

  # Find which slot has this session_id
  for i in 1 2 3 4; do
    STATE_FILE="$STATE_DIR/slot-${i}.json"
    [ ! -f "$STATE_FILE" ] && continue
    file_session=$(jq -r '.session_id // ""' "$STATE_FILE" 2>/dev/null)
    if [ "$file_session" = "$SESSION_ID" ]; then
      echo "Found session $SESSION_ID in slot $i — releasing"
      NOW=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
      jq --arg now "$NOW" \
        '.occupied = false | .session_id = null | .task = null | .branch = null | .assigned_at = null | .last_activity = $now' \
        "$STATE_FILE" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "$STATE_FILE"
      echo "Slot $i released"
      exit 0
    fi
  done

  echo "No slot found with session_id: $SESSION_ID"
  exit 0
fi

# Standard usage requires slot number
if [ -z "$SLOT" ] || [ -z "$ACTION" ]; then
  echo "Usage:" >&2
  echo "  update-slot-state.sh <slot> --release" >&2
  echo "  update-slot-state.sh <slot> --session <id>" >&2
  echo "  update-slot-state.sh <slot> --activity" >&2
  echo "  update-slot-state.sh --cleanup-session <session_id>" >&2
  exit 1
fi

STATE_FILE="$STATE_DIR/slot-${SLOT}.json"

if [ ! -f "$STATE_FILE" ]; then
  echo "ERROR: State file not found: $STATE_FILE" >&2
  exit 1
fi

NOW=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

case "$ACTION" in
  --release)
    jq --arg now "$NOW" \
      '.occupied = false | .session_id = null | .task = null | .branch = null | .assigned_at = null | .last_activity = $now' \
      "$STATE_FILE" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "$STATE_FILE"
    echo "Slot $SLOT released"
    ;;

  --session)
    if [ -z "$VALUE" ]; then
      echo "Usage: update-slot-state.sh <slot> --session <id>" >&2
      exit 1
    fi
    jq --arg sid "$VALUE" --arg now "$NOW" \
      '.session_id = $sid | .last_activity = $now' \
      "$STATE_FILE" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "$STATE_FILE"
    echo "Slot $SLOT session set: $VALUE"
    ;;

  --activity)
    jq --arg now "$NOW" '.last_activity = $now' \
      "$STATE_FILE" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "$STATE_FILE"
    echo "Slot $SLOT activity updated"
    ;;

  *)
    echo "Unknown action: $ACTION" >&2
    exit 1
    ;;
esac
