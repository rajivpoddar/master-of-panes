#!/bin/bash
# pm-idle-handler.sh — UserPromptSubmit hook on the PM pane.
#
# Fires when any user message arrives in the PM Claude Code session.
# Detects slot idle notifications that include a branch name and auto-starts
# ci-watch.sh if an open PR exists on that branch.
#
# Usage (as a UserPromptSubmit hook — receives JSON on stdin):
#   pm-idle-handler.sh
#
# Stdin JSON from Claude Code:
#   { "session_id": "...", "prompt": "...", "cwd": "..." }
#
# Idle notification format (sent by slot-idle-notify.sh):
#   [slot N idle — task | branch: fix/NNN-name] [HH:MM:SS]

INPUT=$(cat)
PROMPT=$(echo "$INPUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('prompt', ''))" 2>/dev/null)

# Only act on slot idle notifications that include a branch
if ! echo "$PROMPT" | grep -qE '\[slot [0-9]+ idle.*\| branch: [^]]+\]'; then
  exit 0
fi

# Extract branch name from the notification
BRANCH=$(echo "$PROMPT" | grep -oE 'branch: [^]]+\]' | sed 's/branch: //; s/\]//')

if [ -z "$BRANCH" ] || [ "$BRANCH" = "main" ]; then
  exit 0
fi

# Check for an open PR on this branch
PR_NUMBER=$(gh pr list --head "$BRANCH" --state open --json number --jq '.[0].number' 2>/dev/null)

if [ -z "$PR_NUMBER" ] || [ "$PR_NUMBER" = "null" ]; then
  exit 0
fi

# Guard: don't start ci-watch if already watching this PR
if pgrep -f "ci-watch.sh $PR_NUMBER" > /dev/null 2>&1; then
  exit 0
fi

# Start ci-watch in background from PM pane
CI_WATCH="$HOME/.claude/commands/ci-watch.sh"
if [ -f "$CI_WATCH" ]; then
  bash "$CI_WATCH" "$PR_NUMBER" >> "/tmp/ci-watch-${PR_NUMBER}.log" 2>&1 &
  TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  echo "[$TIMESTAMP] pm-idle-handler: started ci-watch for PR #$PR_NUMBER (branch: $BRANCH)" >> /tmp/mop-notifications.log
fi

exit 0
