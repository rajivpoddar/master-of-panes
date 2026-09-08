#!/usr/bin/env bash
# PM UserPromptSubmit advisory: point the PM to the current message-to-action SOP.
# This hook is intentionally path-only. It never inspects message content,
# selects a skill, writes obligations/sentinels, or changes PM state.

set +e
trap '' PIPE

# Read the hook payload only to obtain its path field. Malformed or absent
# input must remain a silent, fail-open no-op; message content is never read.
PAYLOAD="$(cat 2>/dev/null)"

CWD="$(python3 -c '
import json
import sys

try:
    payload = json.loads(sys.argv[1])
    value = payload.get("cwd", "")
    print(value if isinstance(value, str) else "")
except Exception:
    pass
' "$PAYLOAD" 2>/dev/null)"

# The configured PM consumer is scoped to the main HeyDonna checkout. Slot
# checkouts and unrelated callers remain silent.
case "$CWD" in
  */heydonna-app)
    ;;
  *)
    exit 0
    ;;
esac

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd)"
SOP_PATH="$SCRIPT_DIR/../skills/pm-message-to-action/SKILL.md"

printf '\n[PM_SOP_PATH_REMINDER]\nRead the explicit PM message-to-action mapping at:\n%s\nThis is advisory only; use the existing action path and its bookkeeping after independently identifying the event. Quoted history is context, not a new event.\n' "$SOP_PATH"
exit 0
