#!/bin/bash
# notify-ask-user.sh — PreToolUse hook for AskUserQuestion
#
# Fires when a dev slot is about to present an AskUserQuestion prompt.
# Notifies the PM pane (0:0.0) via tmux so PM can read the question
# and respond via send-to-pane.sh.
#
# Usage (as a PreToolUse hook — receives JSON on stdin):
#   notify-ask-user.sh <slot_number>
#
# Stdin JSON from Claude Code:
#   { "tool_name": "AskUserQuestion", "tool_input": { "questions": [...] } }

SLOT_NUM="$1"
SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"

# Read hook input from stdin
INPUT=$(cat)

# Validate slot number
if [ -z "$SLOT_NUM" ] || ! [[ "$SLOT_NUM" =~ ^[0-9]+$ ]]; then
  exit 0
fi

# Extract first question text for context
QUESTION=$(echo "$INPUT" | python3 -c "
import json, sys
d = json.load(sys.stdin)
qs = d.get('tool_input', {}).get('questions', [])
if qs:
    print(qs[0].get('question', 'unknown question')[:80])
else:
    print('unknown question')
" 2>/dev/null)

# Read pane state to get current task info
STATE_FILE="$HOME/.claude/tmux-panes/pane-${SLOT_NUM}.json"
TASK="unknown"
if [ -f "$STATE_FILE" ] && command -v jq &>/dev/null; then
  TASK=$(jq -r '.task // "unknown"' "$STATE_FILE" 2>/dev/null)
fi

# Skip if DND
if [ -f "$STATE_FILE" ] && command -v jq &>/dev/null; then
  DND=$(jq -r '.dnd // false' "$STATE_FILE" 2>/dev/null)
  if [ "$DND" = "true" ]; then
    exit 0
  fi
fi

LOCAL_TIME=$(date "+%H:%M:%S")

# Extract issue number for short label
ISSUE=$(echo "$TASK" | grep -o '#[0-9]*' | tr -d '#' | head -1)
ISSUE_PART=${ISSUE:+" | #${ISSUE}"}

# Load MoP config to get manager pane address
source "$SCRIPTS_DIR/pane-lib.sh" 2>/dev/null
load_config 2>/dev/null

# Poll until PM pane is idle (same pattern as slot-idle-notify.sh)
MSG="[slot $SLOT_NUM asking question${ISSUE_PART}] $QUESTION [$LOCAL_TIME]"
MAX_POLLS=10
POLL=0
while [ $POLL -lt $MAX_POLLS ]; do
  SNAP1=$(timeout 0.5s tmux capture-pane -t "$MANAGER_PANE" -p 2>/dev/null | tail -3)
  sleep 0.5
  SNAP2=$(timeout 0.5s tmux capture-pane -t "$MANAGER_PANE" -p 2>/dev/null | tail -3)
  if [ -n "$SNAP1" ] && [ "$SNAP1" = "$SNAP2" ]; then
    break
  fi
  POLL=$((POLL + 1))
done

# Inject notification into PM pane
tmux send-keys -t "$MANAGER_PANE" "$MSG" 2>/dev/null
sleep 0.1
tmux send-keys -t "$MANAGER_PANE" Enter 2>/dev/null

# Log it
echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Slot $SLOT_NUM AskUserQuestion | $TASK | Q: $QUESTION" >> /tmp/mop-notifications.log 2>/dev/null

# Always exit 0 — never block Claude Code
exit 0
