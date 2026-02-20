#!/bin/bash
# Install MoP SessionStart hook into the manager pane's .claude/settings.json.
#
# Injects a slot status table into the manager's session context at startup
# by reading pane state files from ~/.claude/tmux-panes/.
#
# Called during pane-setup to wire up the manager pane.
# Merges the SessionStart hook config into existing settings without
# overwriting other settings or hooks. Idempotent — safe to re-run.
#
# Usage:
#   install-manager-hooks.sh <manager_checkout_path>
#
# Example:
#   install-manager-hooks.sh /Users/rajiv/Downloads/projects/heydonna-app

set -e

CHECKOUT_PATH="$1"
SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -z "$CHECKOUT_PATH" ]; then
  echo "Usage: install-manager-hooks.sh <manager_checkout_path>" >&2
  exit 1
fi

if [ ! -d "$CHECKOUT_PATH" ]; then
  echo "ERROR: Checkout path does not exist: $CHECKOUT_PATH" >&2
  exit 1
fi

# Ensure .claude directory exists
CLAUDE_DIR="$CHECKOUT_PATH/.claude"
mkdir -p "$CLAUDE_DIR"

SETTINGS_FILE="$CLAUDE_DIR/settings.json"
STATUS_SCRIPT="$SCRIPTS_DIR/slot-status-report.sh"

# Verify status script exists
if [ ! -f "$STATUS_SCRIPT" ]; then
  echo "ERROR: slot-status-report.sh not found at: $STATUS_SCRIPT" >&2
  exit 1
fi

# Use a temp Python file to avoid quoting issues with single quotes in HOOK_CMD
MERGE_SCRIPT=$(mktemp /tmp/mop-install-manager-XXXXXX.py)
trap "rm -f $MERGE_SCRIPT" EXIT

cat > "$MERGE_SCRIPT" << PYEOF
import json, sys, os

settings_file = sys.argv[1]
status_script = sys.argv[2]

hook_cmd = "echo '=== Slot Status ===' && bash \"" + status_script + "\""

# Load or init settings
if os.path.exists(settings_file):
    with open(settings_file) as f:
        settings = json.load(f)
else:
    settings = {}

# Ensure hooks.SessionStart exists
if "hooks" not in settings:
    settings["hooks"] = {}
if "SessionStart" not in settings["hooks"]:
    settings["hooks"]["SessionStart"] = []

# Check if already installed (idempotent)
for entry in settings["hooks"]["SessionStart"]:
    for inner in entry.get("hooks", []):
        if "slot-status-report.sh" in inner.get("command", ""):
            print("already installed")
            sys.exit(0)

# Remove any stale MoP SessionStart hooks (clean reinstall)
settings["hooks"]["SessionStart"] = [
    h for h in settings["hooks"]["SessionStart"]
    if not any("slot-status-report.sh" in inner.get("command", "")
               for inner in h.get("hooks", []))
]

# Append our hook
settings["hooks"]["SessionStart"].append({
    "hooks": [{
        "type": "command",
        "command": hook_cmd,
        "timeout": 5
    }]
})

with open(settings_file, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

print("installed")
PYEOF

RESULT=$(python3 "$MERGE_SCRIPT" "$SETTINGS_FILE" "$STATUS_SCRIPT")

if [ "$RESULT" = "already installed" ]; then
  echo "✓ SessionStart hook already installed in $SETTINGS_FILE"
elif [ "$RESULT" = "installed" ]; then
  echo "✓ SessionStart hook installed in $SETTINGS_FILE"
else
  echo "ERROR: unexpected result: $RESULT" >&2
  exit 1
fi
