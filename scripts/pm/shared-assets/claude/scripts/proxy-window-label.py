#!/usr/bin/env python3
"""Short tmux window label for one proxy launch: px-<short-type>-<issue|PR|pid4>.

Only the tmux WINDOW NAME uses this label. The Claude session --name
(proxy-<type>-<stamp>-<pid>) and every log/out/jsonl/state filename are unchanged;
proxy-agent.sh binds the run name to the window through the tmux window option
@proxy_name, which every consumer reads before falling back to the legacy
px-<run-name> derivation (windows launched before this change).

Usage: proxy-window-label.py --type T --brief FILE --pid N  < taken-window-names
Prints the label. Window names already taken (one per stdin line) get -2, -3, ...
Never raises: an unreadable brief only means "no number".
"""
import argparse
import pathlib
import re
import sys

SHORT_TYPES = {
    'hd-builder': 'bld',
    'hd-planner': 'pln',
    'hd-proof-qa': 'pqa',
    'hd-investigator': 'inv',
    'remote-qa-runner': 'rqa',
    'remote-test-runner': 'rtr',
    'cleanup-pr-runner': 'cln',
    'ci-repair-agent': 'cir',
    'pm-triage-agent': 'tri',
    'pm-admission-reviewer': '12b',
    'claude': 'gen',
}


def short_type(agent_type: str) -> str:
    t = (agent_type or '').strip().lower()
    if t in SHORT_TYPES:
        return SHORT_TYPES[t]
    return re.sub(r'[^a-z0-9]', '', t)[:3] or 'gen'


def brief_number(text: str):
    """Same brief-header convention as the launch log: ISSUE: first, then PR:."""
    for key in ('ISSUE', 'PR'):
        m = re.search(r'^\s*' + key + r'\s*:\s*#?(\d{3,6})\b', text, re.I | re.M)
        if m:
            return m.group(1)
    return None


def label(agent_type: str, brief_text: str, pid: int, taken=()) -> str:
    number = brief_number(brief_text) or '%04d' % (int(pid) % 10000)
    base = 'px-%s-%s' % (short_type(agent_type), number)
    used = set(taken)
    name, n = base, 1
    while name in used:
        n += 1
        name = '%s-%d' % (base, n)
    return name


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--type', required=True)
    ap.add_argument('--brief', default='')
    ap.add_argument('--pid', type=int, required=True)
    args = ap.parse_args(argv)
    try:
        text = pathlib.Path(args.brief).read_text(errors='replace') if args.brief else ''
    except OSError:
        text = ''
    taken = [] if sys.stdin.isatty() else [ln.strip() for ln in sys.stdin.read().splitlines() if ln.strip()]
    print(label(args.type, text, args.pid, taken))
    return 0


if __name__ == '__main__':
    sys.exit(main())
