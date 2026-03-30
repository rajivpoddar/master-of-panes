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
#   4. Polls PM pane until idle (two captures 0.5s apart, static = not typing), then injects

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

# Read pane state to get current task info (use python3, not jq — jq may not be in hook PATH)
PANE_STATE_DIR="$HOME/.claude/tmux-panes"
STATE_FILE="$PANE_STATE_DIR/pane-${SLOT_NUM}.json"
TASK="unknown"
if [ -f "$STATE_FILE" ]; then
  TASK=$(python3 -c "import json; d=json.load(open('$STATE_FILE')); print(d.get('task') or 'unknown')" 2>/dev/null)
fi

# 1. Update pane state — last_activity timestamp
if [ -f "$SCRIPTS_DIR/update-pane-state.sh" ]; then
  bash "$SCRIPTS_DIR/update-pane-state.sh" "$SLOT_NUM" --activity 2>/dev/null
fi

# 2. Write to notification log (append, non-blocking)
LOG_FILE="/tmp/mop-notifications.log"
echo "[$TIMESTAMP] Slot $SLOT_NUM idle | $TASK | session=$SESSION_ID" >> "$LOG_FILE" 2>/dev/null

# 3. Notify PM pane via tmux send-keys (injects as user message into PM Claude Code session)
# Include branch so PM can decide to start ci-watch if a PR is open.
# Skip PM injection if pane is in DND mode (MoP still has the log from step 2).
if [ -f "$STATE_FILE" ]; then
  DND=$(python3 -c "import json; d=json.load(open('$STATE_FILE')); print(d.get('dnd', False))" 2>/dev/null)
  if [ "$DND" = "True" ] || [ "$DND" = "true" ]; then
    exit 0
  fi
fi

if command -v tmux &>/dev/null; then
  # Load config to get manager pane address
  source "$SCRIPTS_DIR/pane-lib.sh" 2>/dev/null
  load_config 2>/dev/null

  # Short task label (first 40 chars)
  SHORT_TASK=$(echo "$TASK" | cut -c1-40)

  # Include branch name so PM has context for CI watch decisions
  BRANCH=$(git -C "$CWD" branch --show-current 2>/dev/null)
  BRANCH_INFO=""
  if [ -n "$BRANCH" ] && [ "$BRANCH" != "main" ]; then
    BRANCH_INFO=" | branch: $BRANCH"
  fi

  # Poll until PM pane is idle before injecting.
  # Captures pane twice 0.5s apart — if content is unchanged, PM is not typing.
  # Each capture is timeout-guarded (0.5s) to prevent hangs.
  # Falls through after MAX_POLLS regardless (fail-open).
  # Send /slot-idle command instead of free text — forces PM to create subtasks
  # and follow the full pm-idle-notification decision tree (no shortcuts).
  COMMENT="# slot $SLOT_NUM idle — $SHORT_TASK$BRANCH_INFO | $LOCAL_TIME"
  COMMAND="/slot-idle $SLOT_NUM"

  MAX_POLLS=15
  POLL=0
  while [ $POLL -lt $MAX_POLLS ]; do
    SNAP1=$(timeout 0.5s tmux capture-pane -t "$MANAGER_PANE" -p 2>/dev/null | tail -3)
    sleep 0.5
    SNAP2=$(timeout 0.5s tmux capture-pane -t "$MANAGER_PANE" -p 2>/dev/null | tail -3)
    # Static pane = PM not typing → safe to inject
    if [ -n "$SNAP1" ] && [ "$SNAP1" = "$SNAP2" ]; then
      break
    fi
    POLL=$((POLL + 1))
  done

  # Inject context comment + slash command as ONE multi-line message.
  # Uses Shift+Enter (S-Enter) to insert newline without submitting,
  # then Enter to submit the combined message. Only Enter gets a delay.
  tmux send-keys -t "$MANAGER_PANE" "$COMMENT" S-Enter "$COMMAND" 2>/dev/null
  sleep 0.5
  tmux send-keys -t "$MANAGER_PANE" Enter 2>/dev/null
fi

# 4. Auto-release slot if POST-PR (a PR exists for the current branch).
# This frees the slot immediately — PM still gets the notification for CI watch/labels.
# Only triggers when: branch is not main AND a PR exists for that branch.
if [ -n "$BRANCH" ] && [ "$BRANCH" != "main" ] && [ -f "$STATE_FILE" ]; then
  PR_NUM=$(gh pr list --head "$BRANCH" --json number --jq '.[0].number' 2>/dev/null)
  if [ -n "$PR_NUM" ] && [ "$PR_NUM" != "null" ] && [ "$PR_NUM" != "" ]; then
    python3 -c "
import json
f = open('$STATE_FILE', 'r+')
d = json.load(f)
d['occupied'] = False
d['status'] = 'free'
d['state'] = 'FREE'
d['pr'] = int('$PR_NUM')
d['dnd'] = False
f.seek(0)
f.truncate()
json.dump(d, f, indent=2)
f.close()
" 2>/dev/null
    echo "[$TIMESTAMP] Slot $SLOT_NUM auto-released (POST-PR: #$PR_NUM)" >> "$LOG_FILE" 2>/dev/null
  fi
fi

# Always exit 0 — never block Claude from stopping
exit 0
