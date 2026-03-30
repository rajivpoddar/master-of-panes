#!/usr/bin/env bash
# notify-manager.sh — Send idle notification to manager pane when a slot's session ends.
#
# Called as a Claude Code Stop hook. Uses MoP HTTP API (not legacy pane-N.json files)
# to identify which slot this session belongs to.
#
# Rajiv directive 2026-03-20: "fix it. audit other hooks as well. add logging."
# Root cause: was reading pane-N.json which is blocked by redirect-pane-json-to-mcp.sh hook.
# Fix: use MoP HTTP API at localhost:3100 instead.

set -euo pipefail

LOG_FILE="/tmp/mop-notifications.log"
MOP_PORT="${MOP_PORT:-3100}"

log() {
  echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] [notify-manager] $*" >> "$LOG_FILE" 2>/dev/null || true
}

# Read hook JSON from stdin (Claude Code hook protocol)
INPUT=$(cat 2>/dev/null || true)

# CRITICAL: Prevent infinite loop
STOP_HOOK_ACTIVE=$(echo "$INPUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('stop_hook_active', False))" 2>/dev/null || echo "false")
if [ "$STOP_HOOK_ACTIVE" = "True" ] || [ "$STOP_HOOK_ACTIVE" = "true" ]; then
  log "Skipped: stop_hook_active=true (loop prevention)"
  exit 0
fi

# Get session ID from stdin JSON
SESSION_ID=$(echo "$INPUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('session_id', ''))" 2>/dev/null || echo "")
if [ -z "$SESSION_ID" ]; then
  SESSION_ID="${SESSION_ID:-}"
fi
if [ -z "$SESSION_ID" ]; then
  log "Skipped: no session_id in Stop hook payload"
  exit 0
fi

log "Stop hook fired: session=$SESSION_ID"

# Use MoP HTTP API to find which slot this session belongs to
SLOT_INFO=$(curl -s "http://localhost:${MOP_PORT}/slots" 2>/dev/null || echo "{}")
PANE_NUM=""
TASK=""

if [ -n "$SLOT_INFO" ] && [ "$SLOT_INFO" != "{}" ]; then
  # Parse slot data via Python for reliability
  read -r PANE_NUM TASK <<< $(echo "$SLOT_INFO" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    slots = data if isinstance(data, list) else data.get('slots', [])
    session_id = '$SESSION_ID'
    for slot in slots:
        sid = slot.get('session_id', '')
        if sid == session_id:
            print(f'{slot[\"slot\"]} {slot.get(\"task\", \"unknown\")}')
            sys.exit(0)
    # No session match — try matching by CWD/pane address
    # Fallback: determine slot from the CWD in the hook payload
    print('')
except Exception as e:
    print('')
" 2>/dev/null || echo "")
fi

# Fallback: determine slot from CWD if MoP session lookup failed
if [ -z "$PANE_NUM" ]; then
  CWD=$(echo "$INPUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('cwd', ''))" 2>/dev/null || echo "")
  case "$CWD" in
    *heydonna-app-3001*) PANE_NUM=1 ;;
    *heydonna-app-3002*) PANE_NUM=2 ;;
    *heydonna-app-3003*) PANE_NUM=3 ;;
    *heydonna-app-3004*) PANE_NUM=4 ;;
    *) ;;
  esac
  if [ -n "$PANE_NUM" ]; then
    # Get task from MoP for this slot
    TASK=$(curl -s "http://localhost:${MOP_PORT}/slots/${PANE_NUM}" 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('task','unknown'))" 2>/dev/null || echo "unknown")
    log "Slot identified via CWD fallback: slot=$PANE_NUM task=$TASK"
  fi
fi

if [ -z "$PANE_NUM" ]; then
  log "Skipped: could not identify slot for session=$SESSION_ID"
  exit 0
fi

log "Slot $PANE_NUM idle | task=$TASK | session=$SESSION_ID"

# Post Stop event to MoP HTTP for event logging
curl -s -X POST "http://localhost:${MOP_PORT}/hooks/slot/${PANE_NUM}" \
  -H "Content-Type: application/json" \
  -d "{\"type\":\"Stop\",\"session_id\":\"$SESSION_ID\"}" 2>/dev/null || true

# Send notification to manager pane (0:0.0) via tmux send-keys
if command -v tmux &>/dev/null; then
  MANAGER_PANE="${MANAGER_PANE:-0:0.0}"
  SHORT_TASK=$(echo "$TASK" | cut -c1-50)
  MSG="/slot-idle $PANE_NUM"
  tmux send-keys -t "$MANAGER_PANE" "$MSG" 2>/dev/null || true
  sleep 0.3
  tmux send-keys -t "$MANAGER_PANE" Enter 2>/dev/null || true
  log "Sent idle notification to PM: $MSG"
fi

exit 0
