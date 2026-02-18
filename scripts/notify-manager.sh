#!/usr/bin/env bash
# notify-manager.sh — Send idle notification to manager pane when a slot's session ends.
#
# Called as a Claude Code Stop hook. Reads JSON from stdin (Claude Code hook protocol).
# Scans pane state files to find which slot this session belongs to, then sends
# a tmux send-keys message to the manager pane so the PM session receives it.
#
# Why send-keys instead of display-message:
#   display-message shows a brief overlay in the pane border — the PM's Claude Code
#   session doesn't receive it as input. send-keys types text into the input buffer,
#   which the PM Claude Code session receives and can act on.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/pane-lib.sh" 2>/dev/null || exit 0
require_jq 2>/dev/null || exit 0
load_config 2>/dev/null || true

# Read hook JSON from stdin (Claude Code hook protocol)
INPUT=$(cat 2>/dev/null || true)

# CRITICAL: Prevent infinite loop — if this Stop was triggered by a hook action, don't fire again
STOP_HOOK_ACTIVE=$(echo "$INPUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('stop_hook_active', False))" 2>/dev/null || echo "false")
if [ "$STOP_HOOK_ACTIVE" = "True" ] || [ "$STOP_HOOK_ACTIVE" = "true" ]; then
  exit 0
fi

# Get session ID — prefer stdin JSON, fall back to env var
SESSION_ID=$(echo "$INPUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('session_id', ''))" 2>/dev/null || echo "")
if [ -z "$SESSION_ID" ]; then
  SESSION_ID="${SESSION_ID:-}"  # From env var set by hooks.json
fi
[ -z "$SESSION_ID" ] && exit 0

# Scan pane state files to find which slot this session belongs to
PANE_NUM=""
TASK=""
for i in $(seq 1 "$NUM_DEV_PANES"); do
  STATE_FILE="$PANE_STATE_DIR/pane-${i}.json"
  [ ! -f "$STATE_FILE" ] && continue
  file_session=$(jq -r '.session_id // ""' "$STATE_FILE" 2>/dev/null || echo "")
  if [ "$file_session" = "$SESSION_ID" ]; then
    PANE_NUM="$i"
    TASK=$(jq -r '.task // "unknown"' "$STATE_FILE" 2>/dev/null || echo "unknown")
    break
  fi
done

[ -z "$PANE_NUM" ] && exit 0  # Not a tracked dev pane — skip

# Write to notification log
LOG_FILE="/tmp/mop-notifications.log"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
echo "[$TIMESTAMP] Slot $PANE_NUM idle | $TASK | session=$SESSION_ID" >> "$LOG_FILE" 2>/dev/null || true

# Send notification to manager pane via send-keys so PM session receives it as input
if command -v tmux &>/dev/null; then
  LOCAL_TIME=$(date "+%H:%M:%S")
  SHORT_TASK=$(echo "$TASK" | cut -c1-50)
  MSG="[slot $PANE_NUM idle — $SHORT_TASK] [$LOCAL_TIME]"
  tmux send-keys -t "$MANAGER_PANE" "$MSG" 2>/dev/null || true
  sleep 0.3
  tmux send-keys -t "$MANAGER_PANE" Enter 2>/dev/null || true
fi

# Always exit 0 — never block Claude from stopping
exit 0
