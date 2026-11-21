"""TEST-ONLY wrapper that runs a REAL bulk-read entrypoint (guard, bulk-read, code-write) with the managed constants of bulk_read_lib
replaced from a JSON file. The deployed entrypoints have no environment seam (CTO REVISE on fb6de68d6a35, ts 1795292716.429489); this file is
the only way a test points them at a stub router, a fake claude, a temp HOME or a temp state dir. Nothing deployed imports or reads it.

  BULK_READ_HARNESS_CONFIG=<json> python3 -I bulk_read_harness.py <bulk_read_guard.py|bulk-read|code-write> [args...]
"""
import json
import os
import runpy
import sys

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, SCRIPTS)
import bulk_read_lib as lib  # noqa: E402

with open(os.environ.pop("BULK_READ_HARNESS_CONFIG")) as handle:
    for key, value in json.load(handle).items():
        setattr(lib, key, tuple(value) if key == "CLAUDE_CANDIDATES" else value)
entry = os.path.join(SCRIPTS, sys.argv[1])
sys.argv = [entry] + sys.argv[2:]
runpy.run_path(entry, run_name="__main__")
