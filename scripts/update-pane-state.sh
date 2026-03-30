#!/bin/bash
# Update pane state via MoP HTTP API.
#
# Migrated from legacy pane-N.json to MoP HTTP. All state operations
# now go through localhost:3100. (Rajiv directive 2026-03-20)
#
# Usage:
#   update-pane-state.sh <pane> --release
#   update-pane-state.sh --cleanup-session <session_id>

set -euo pipefail

MOP_PORT="${MOP_PORT:-3100}"
LOG_FILE="/tmp/mop-notifications.log"

log() {
  echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] [update-pane-state] $*" >> "$LOG_FILE" 2>/dev/null || true
}

# Handle --cleanup-session (scans all slots via MoP HTTP)
if [ "${1:-}" = "--cleanup-session" ]; then
  SESSION_ID="${2:-}"
  if [ -z "$SESSION_ID" ]; then
    echo "Usage: update-pane-state.sh --cleanup-session <session_id>" >&2
    exit 1
  fi

  log "cleanup-session: $SESSION_ID"

  SLOT=$(curl -s "http://localhost:${MOP_PORT}/slots" 2>/dev/null | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    slots = data if isinstance(data, list) else data.get('slots', [])
    for slot in slots:
        if slot.get('session_id', '') == '$SESSION_ID':
            print(slot['slot'])
            sys.exit(0)
    print('')
except:
    print('')
" 2>/dev/null || echo "")

  if [ -n "$SLOT" ]; then
    log "Found session $SESSION_ID in slot $SLOT — releasing"
    curl -s -X POST "http://localhost:${MOP_PORT}/slots/${SLOT}/release" \
      -H "Content-Type: application/json" 2>/dev/null || true
    log "Slot $SLOT released"
  else
    log "Session $SESSION_ID not found in any slot"
  fi
  exit 0
fi

PANE_NUM="${1:-}"
ACTION="${2:-}"

if [ -z "$PANE_NUM" ] || [ -z "$ACTION" ]; then
  echo "Usage: update-pane-state.sh <pane> --release|--cleanup-session" >&2
  exit 1
fi

case "$ACTION" in
  --release)
    log "Releasing slot $PANE_NUM"
    curl -s -X POST "http://localhost:${MOP_PORT}/slots/${PANE_NUM}/release" \
      -H "Content-Type: application/json" 2>/dev/null || true
    ;;
  *)
    log "Unknown action: $ACTION (legacy actions removed — use MoP HTTP directly)"
    ;;
esac

exit 0
