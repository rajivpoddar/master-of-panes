#!/usr/bin/env python3
"""pm-handoff-fresh.py: is the PM session handoff note newer than the last hourly parent post?

Rajiv D0AMF0XE6TS thread 1795441948.588769, ts 1795443847.471589 ("how we use if for pm self clear and restart with continuation?").
The PM /clear and respawn path calls this first; when the note is STALE the PM rewrites it (Skill pm-handoff, mode checkpoint) and
then proceeds. It is a CHECKPOINT ONLY and never gates input (rule 50): it always exits 0 unless --exit-code is passed (a script
that wants 0 fresh / 1 stale / 2 unknown), it reads two small files, and any problem is reported as HANDOFF_UNKNOWN, never an error.

  HANDOFF_FRESH   note_mtime=... hourly_parent_ts=... delta_s=+N shape=ok
  HANDOFF_STALE   reason=older_than_hourly_parent|shape_missing:<sections> ...
  HANDOFF_UNKNOWN reason=...

The hourly parent ts is the "ts" in the hourly-thread state record (the Slack ts of the last hourly parent post; it is epoch seconds).
The note must also carry the sections GOAL, DONE, OPEN, NEXT, DECIDED + WHY, DON'T (any heading level).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

HOME = Path.home()
PROJECT = HOME / ".claude/projects/-Users-rajiv-Downloads-projects-heydonna-app"
NOTE = PROJECT / "memory/MEMORY-session-handoff-current.md"
STATE = Path(os.environ.get("HOURLY_THREAD_STATE", str(PROJECT / "state/hourly-thread.json")))
REQUIRED = ("GOAL", "DONE", "OPEN", "NEXT", "DECIDED+WHY", "DONT")


def iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sections_missing(text: str) -> list[str]:
    seen = set()
    for line in text.splitlines():
        m = re.match(r"^#{1,4}\s+(.+?)\s*$", line)
        if not m:
            continue
        title = re.sub(r"\(.*?\)", "", m.group(1))
        cleaned = re.sub(r"[^A-Z+]", "", title.upper())
        for name in REQUIRED:
            if cleaned.startswith(name):
                seen.add(name)
    return [("DECIDED + WHY" if n == "DECIDED+WHY" else "DON'T" if n == "DONT" else n) for n in REQUIRED if n not in seen]


def check(note: Path, state: Path) -> tuple[str, str]:
    try:
        mtime = note.stat().st_mtime
        text = note.read_text(errors="replace")
    except OSError as exc:
        return "HANDOFF_UNKNOWN", f"reason=note_unreadable:{type(exc).__name__} note={note}"
    try:
        parent = float(json.loads(state.read_text())["ts"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return "HANDOFF_UNKNOWN", f"reason=hourly_state_unreadable:{type(exc).__name__} note_mtime={iso(mtime)}"
    missing = sections_missing(text)
    base = (f"note_mtime={iso(mtime)} hourly_parent_ts={parent:.6f} hourly_parent={iso(parent)} "
            f"delta_s={mtime - parent:+.0f} shape={'ok' if not missing else 'missing:' + ','.join(missing)}")
    reasons = []
    if mtime <= parent:
        reasons.append("older_than_hourly_parent")
    if missing:
        reasons.append("shape_missing:" + ",".join(missing))
    if reasons:
        return "HANDOFF_STALE", f"reason={'+'.join(reasons)} {base}"
    return "HANDOFF_FRESH", base


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pm-handoff-fresh.py")
    parser.add_argument("--note", default=str(NOTE))
    parser.add_argument("--state", default=str(STATE))
    parser.add_argument("--exit-code", action="store_true", help="exit 0 fresh, 1 stale, 2 unknown (default: always 0)")
    args = parser.parse_args(argv)
    try:
        status, detail = check(Path(args.note), Path(args.state))
    except Exception as exc:  # fail open
        status, detail = "HANDOFF_UNKNOWN", f"reason=error:{type(exc).__name__}"
    print(f"{status} {detail}")
    if args.exit_code:
        return {"HANDOFF_FRESH": 0, "HANDOFF_STALE": 1}.get(status, 2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
