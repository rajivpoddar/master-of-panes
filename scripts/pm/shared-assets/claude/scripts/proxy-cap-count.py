#!/usr/bin/env python3
"""Count running long-lived proxy roles using their run registry metadata."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

CAPPED = frozenset({'hd-planner', 'hd-builder', 'hd-proof-qa', 'pm-hotfix',
                    'pm-proof-qa', 'hd-investigator', 'superproofer'})
PROCESS = re.compile(r'(?:^|\s)\S*/claude\s+-p\s+--name\s+([A-Za-z0-9._-]+)\s+--model\s+(\S+)')
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z')


def count_processes(processes, root, claude_only=False):
    count = 0
    for line in processes.splitlines():
        match = PROCESS.search(line)
        if not match:
            continue
        name, model = match.groups()
        if claude_only and not model.startswith('claude-'):
            continue
        if not NAME.fullmatch(name) or name in ('.', '..'):
            raise ValueError('invalid proxy name')
        manifest = root / name / 'manifest.json'
        if not manifest.exists() and not name.startswith(('px-', 'proxy-')):
            continue  # An unrelated named Claude process, not a registered proxy.
        data = json.loads(manifest.read_text())
        role = data.get('type')
        if not isinstance(role, str) or not role:
            raise ValueError('missing proxy type')
        if role in CAPPED:
            count += 1
    return count


def main():
    try:
        root = Path(os.environ.get('PROXY_AGENT_RUN_STATE_DIR', str(Path.home() / '.local/state/proxy-agent-runs')))
        output = subprocess.check_output(['/bin/ps', '-axo', 'command='], text=True, errors='replace')  # a non-UTF-8 argv made ps output undecodable → UnicodeDecodeError → caps unreadable (23 Nov)
        print(count_processes(output, root, sys.argv[1] == 'claude'))
    except (OSError, ValueError, IndexError, subprocess.SubprocessError) as exc:
        print('proxy-agent: cap metadata unavailable: ' + type(exc).__name__, file=sys.stderr)
        print(999)


if __name__ == '__main__':
    main()
