#!/usr/bin/env bash
# hook-relay.sh — Forward Claude Code hook events to MoP server
#
# Called as a Claude Code command hook for PostToolUse and Notification events.
# Reads JSON from stdin (Claude Code hook protocol), determines slot number
# from the cwd field, and POSTs to the MoP HTTP server.
#
# Slot detection: Each slot runs in /Users/rajiv/Downloads/projects/heydonna-app-300N
# where N is the slot number (1-4). PM pane (heydonna-app, no suffix) is skipped.
#
# Usage: hook-relay.sh <hook_type>
#   hook_type: PostToolUse | PreToolUse | Notification

set -euo pipefail

HOOK_TYPE="${1:-}"
[ -z "$HOOK_TYPE" ] && exit 0

# All processing in Python: read stdin JSON, detect slot from cwd, POST to MoP
python3 -c "
import json, sys, re, urllib.request

hook_type = sys.argv[1]

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

# Determine slot number from cwd: heydonna-app-300N → N
cwd = data.get('cwd', '')
m = re.search(r'heydonna-app-300([1-4])', cwd)
if not m:
    sys.exit(0)  # Not a dev slot (PM pane or unknown) — skip

slot = m.group(1)

# Add hook_event_name (command hooks don't include it, only HTTP hooks do)
data['hook_event_name'] = hook_type

try:
    req = urllib.request.Request(
        f'http://localhost:3100/hooks/slot/{slot}',
        data=json.dumps(data).encode(),
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    urllib.request.urlopen(req, timeout=3)
except Exception:
    pass  # Never block Claude Code — fire and forget
" "$HOOK_TYPE" 2>/dev/null

exit 0
