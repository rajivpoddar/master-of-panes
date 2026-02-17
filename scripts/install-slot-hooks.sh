#!/bin/bash
# Install MoP Stop hook into a checkout's .claude/settings.json.
#
# Called during pane-handoff to wire up idle notifications for a slot.
# Merges the Stop hook config into existing settings without overwriting
# other settings or hooks.
#
# Usage:
#   install-slot-hooks.sh <slot_number> <checkout_path>
#
# Example:
#   install-slot-hooks.sh 3 /Users/rajiv/Downloads/projects/heydonna-app-3003

set -e

SLOT_NUM="$1"
CHECKOUT_PATH="$2"
SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -z "$SLOT_NUM" ] || [ -z "$CHECKOUT_PATH" ]; then
  echo "Usage: install-slot-hooks.sh <slot_number> <checkout_path>" >&2
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
NOTIFY_SCRIPT="$SCRIPTS_DIR/slot-idle-notify.sh"

# Verify notify script exists
if [ ! -f "$NOTIFY_SCRIPT" ]; then
  echo "ERROR: slot-idle-notify.sh not found at: $NOTIFY_SCRIPT" >&2
  exit 1
fi

# Build the hook command with absolute path
HOOK_CMD="bash $NOTIFY_SCRIPT $SLOT_NUM"

# Create or merge settings.json
if [ -f "$SETTINGS_FILE" ]; then
  # Merge: add Stop hook without overwriting other settings
  # First check if Stop hook already exists for this slot
  EXISTING_HOOK=$(python3 -c "
import json, sys
try:
    with open('$SETTINGS_FILE') as f:
        d = json.load(f)
    hooks = d.get('hooks', {}).get('Stop', [])
    for h in hooks:
        for inner in h.get('hooks', []):
            if 'slot-idle-notify.sh' in inner.get('command', '') and '$SLOT_NUM' in inner.get('command', ''):
                print('exists')
                sys.exit(0)
except:
    pass
print('missing')
" 2>/dev/null)

  if [ "$EXISTING_HOOK" = "exists" ]; then
    echo "✓ Stop hook already installed for slot $SLOT_NUM in $SETTINGS_FILE"
    exit 0
  fi

  # Merge the hook into existing settings
  python3 -c "
import json

with open('$SETTINGS_FILE') as f:
    settings = json.load(f)

# Ensure hooks.Stop exists
if 'hooks' not in settings:
    settings['hooks'] = {}
if 'Stop' not in settings['hooks']:
    settings['hooks']['Stop'] = []

# Remove any existing MoP Stop hooks (clean reinstall)
settings['hooks']['Stop'] = [
    h for h in settings['hooks']['Stop']
    if not any('slot-idle-notify.sh' in inner.get('command', '')
               for inner in h.get('hooks', []))
]

# Add our hook
settings['hooks']['Stop'].append({
    'hooks': [{
        'type': 'command',
        'command': '$HOOK_CMD',
        'timeout': 10
    }]
})

with open('$SETTINGS_FILE', 'w') as f:
    json.dump(settings, f, indent=2)
    f.write('\n')
" 2>/dev/null

else
  # Create new settings.json with just the hook
  python3 -c "
import json

settings = {
    'hooks': {
        'Stop': [{
            'hooks': [{
                'type': 'command',
                'command': '$HOOK_CMD',
                'timeout': 10
            }]
        }]
    }
}

with open('$SETTINGS_FILE', 'w') as f:
    json.dump(settings, f, indent=2)
    f.write('\n')
" 2>/dev/null

fi

echo "✓ Stop hook installed for slot $SLOT_NUM → $SETTINGS_FILE"
