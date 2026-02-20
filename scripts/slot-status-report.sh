#!/bin/bash
# Print slot status table from pane state files.
# Called by the SessionStart hook in the manager pane to inject
# live slot assignments into the PM's session context.

python3 - <<'PYEOF'
import json, os, glob

state_dir = os.path.expanduser('~/.claude/tmux-panes')
files = sorted(glob.glob(os.path.join(state_dir, 'pane-*.json')))

if not files:
    print('No slot state files found in ' + state_dir)
else:
    for f in files:
        try:
            d = json.load(open(f))
            num = d.get('pane', '?')
            dnd = ' [DND]' if d.get('dnd') else ''
            occ = 'OCCUPIED' if d.get('occupied') else 'FREE'
            task = d.get('task', 'unassigned')
            print(f'Slot {num}: {occ}{dnd} — {task}')
        except Exception as e:
            print(f'{os.path.basename(f)}: error ({e})')

print('=========================')
PYEOF
