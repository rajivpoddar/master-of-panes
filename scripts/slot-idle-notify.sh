#!/bin/bash
# Slot idle notification — runs as a Claude Code Stop hook.
#
# Called automatically when a Claude Code session in a dev slot finishes
# responding (becomes idle). Updates pane state and writes to notification log.
#
# Usage (as a Stop hook — receives JSON on stdin):
#   slot-idle-notify.sh <slot_number>
#
# Stdin JSON from Claude Code:
#   { "session_id": "...", "stop_hook_active": true/false, "cwd": "..." }
#
# What it does:
#   1. Checks stop_hook_active to prevent infinite loops
#   2. Updates pane state with last_activity timestamp
#   3. Writes to /tmp/mop-notifications.log
#   4. Notifies PM pane via tmux send-keys (injects as user message)
#   5. Auto-starts ci-watch.sh if an open PR exists for the slot's branch

SLOT_NUM="$1"
SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"

# Read hook input from stdin
INPUT=$(cat)

# CRITICAL: Prevent infinite loop — if this Stop was triggered by a hook
# action, don't fire again
STOP_HOOK_ACTIVE=$(echo "$INPUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('stop_hook_active', False))" 2>/dev/null)
if [ "$STOP_HOOK_ACTIVE" = "True" ] || [ "$STOP_HOOK_ACTIVE" = "true" ]; then
  exit 0
fi

# Validate slot number
if [ -z "$SLOT_NUM" ] || ! [[ "$SLOT_NUM" =~ ^[0-9]+$ ]]; then
  exit 0  # Silent exit — don't break Claude Code
fi

# Extract session info
SESSION_ID=$(echo "$INPUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('session_id', 'unknown'))" 2>/dev/null)
CWD=$(echo "$INPUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('cwd', ''))" 2>/dev/null)
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
LOCAL_TIME=$(date "+%H:%M:%S")

# Read pane state to get current task info
PANE_STATE_DIR="$HOME/.claude/tmux-panes"
STATE_FILE="$PANE_STATE_DIR/pane-${SLOT_NUM}.json"
TASK="unknown"
if [ -f "$STATE_FILE" ] && command -v jq &>/dev/null; then
  TASK=$(jq -r '.task // "unknown"' "$STATE_FILE" 2>/dev/null)
fi

# 1. Update pane state — last_activity timestamp
if [ -f "$SCRIPTS_DIR/update-pane-state.sh" ]; then
  bash "$SCRIPTS_DIR/update-pane-state.sh" "$SLOT_NUM" --activity 2>/dev/null
fi

# 2. Write to notification log (append, non-blocking)
LOG_FILE="/tmp/mop-notifications.log"
echo "[$TIMESTAMP] Slot $SLOT_NUM idle | $TASK | session=$SESSION_ID" >> "$LOG_FILE" 2>/dev/null

# 3. Notify PM pane via tmux send-keys (injects as user message into PM Claude Code session)
if command -v tmux &>/dev/null; then
  # Load config to get manager pane address
  source "$SCRIPTS_DIR/pane-lib.sh" 2>/dev/null
  load_config 2>/dev/null

  # Short task label (first 40 chars)
  SHORT_TASK=$(echo "$TASK" | cut -c1-40)

  tmux send-keys -t "$MANAGER_PANE" \
    "[slot $SLOT_NUM idle — $SHORT_TASK] [$LOCAL_TIME]" Enter 2>/dev/null
fi

# 5. Auto-start ci-watch if an open PR exists for this slot's branch
# Runs ci-watch.sh in the background so CI completion triggers a Slack DM.
# Guard: skip if ci-watch is already running for this PR (prevents duplicates).
CI_WATCH="$HOME/.claude/commands/ci-watch.sh"
if [ -n "$CWD" ] && [ -f "$CI_WATCH" ] && command -v gh &>/dev/null; then
  BRANCH=$(git -C "$CWD" branch --show-current 2>/dev/null)
  if [ -n "$BRANCH" ] && [ "$BRANCH" != "main" ]; then
    PR_NUMBER=$(gh pr list --head "$BRANCH" --state open --json number --jq '.[0].number' 2>/dev/null)
    if [ -n "$PR_NUMBER" ] && [ "$PR_NUMBER" != "null" ]; then
      if ! pgrep -f "ci-watch.sh $PR_NUMBER" > /dev/null 2>&1; then
        bash "$CI_WATCH" "$PR_NUMBER" >> "/tmp/ci-watch-${PR_NUMBER}.log" 2>&1 &
        echo "[$TIMESTAMP] Started ci-watch for PR #$PR_NUMBER (slot $SLOT_NUM, branch $BRANCH)" >> "$LOG_FILE" 2>/dev/null
      fi
    fi
  fi
fi

# Always exit 0 — never block Claude from stopping
exit 0
