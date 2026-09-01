#!/usr/bin/env python3
"""Read-only compatibility entrypoint for the Sakshi Ready Pool audit."""

from __future__ import annotations

import runpy
import sys


SAKSHI = "/Users/rajiv/.claude/scripts/sakshi-heartbeat.py"

if len(sys.argv) != 2 or sys.argv[1] != "audit-ready-pool":
    print("usage: backlog-triage.py audit-ready-pool", file=sys.stderr)
    raise SystemExit(2)

sys.argv = [SAKSHI, "--ready-pool-audit"]
runpy.run_path(SAKSHI, run_name="__main__")
