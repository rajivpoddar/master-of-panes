#!/usr/bin/env python3
"""Pre-acquisition freshness gate for Codex companion app-server brokers.

A broker loads credentials at startup and keeps presenting them for its whole
life, so a routine auth refresh makes every older broker present a stale
credential. This helper compares the broker parent's start epoch against the
companion home's authoritative auth refresh epoch and decides whether the
broker may be reused.

Decision contract
  fresh              -> no-op, reuse the broker
  stale + idle       -> recycle that broker parent and its app-server children
  stale + busy       -> defer without interruption, typed diagnostic
  missing / invalid  -> fail safe with a typed diagnostic, never guess

It reads ONLY the auth store's top-level `last_refresh` timestamp. It never
reads, copies, logs or returns token material.

This is upgrade-safe by construction: it is a check that runs BEFORE the broker,
not a patch to the upstream broker. Delete it once the upstream plugin reloads
credentials (or enforces equivalent per-request/TTL freshness) and that
behaviour is verified.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time
from typing import Any

DEFAULT_HOME = pathlib.Path("/Users/rajiv/.codex-companions")
BROKER_MARKER = "app-server-broker.mjs"
CHILD_MARKER = "app-server"
PLACEHOLDER = {"", "unknown", "none", "n/a"}

# actions
NOOP = "noop"
RECYCLE = "recycle"
DEFER = "defer"
FAIL_SAFE = "fail_safe"


def parse_iso_epoch(value: Any) -> float | None:
    """Parse an ISO-8601 timestamp (auth store style) to an epoch, else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def parse_ps_lstart(value: str) -> float | None:
    """Parse `ps -o lstart=` text ('Wed Sep 24 10:00:00 2026') to an epoch."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.strptime(value.strip(), "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return None
    return parsed.timestamp()


def read_refresh_epoch(home: pathlib.Path) -> tuple[float | None, str]:
    """Return (epoch, error) for the companion home's auth refresh time.

    Reads ONLY the top-level `last_refresh` field. Token material is never
    accessed, copied or printed.
    """
    auth = home / "auth.json"
    try:
        raw = auth.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"auth_store_unreadable:{type(exc).__name__}"
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        return None, "auth_store_not_json"
    if not isinstance(doc, dict):
        return None, "auth_store_not_an_object"
    epoch = parse_iso_epoch(doc.get("last_refresh"))
    if epoch is None:
        return None, "auth_refresh_missing_or_unparseable"
    return epoch, ""


def list_processes() -> list[dict[str, Any]]:
    """Return [{pid, ppid, start_epoch, command}] for the whole process table."""
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,lstart=,command="],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows: list[dict[str, Any]] = []
    for line in out.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        pid, ppid = parts[0], parts[1]
        lstart = " ".join(parts[2:7])
        command = parts[7]
        if not pid.isdigit() or not ppid.isdigit():
            continue
        rows.append({
            "pid": int(pid), "ppid": int(ppid),
            "start_epoch": parse_ps_lstart(lstart), "command": command,
        })
    return rows


def select_brokers(processes: list[dict[str, Any]], cwd: str) -> list[dict[str, Any]]:
    """Broker parents serving exactly this checkout, oldest first."""
    picked = [
        p for p in processes
        if BROKER_MARKER in p["command"] and f"--cwd {cwd}" in p["command"]
    ]
    return sorted(picked, key=lambda p: (p["start_epoch"] or 0.0, p["pid"]))


def select_children(processes: list[dict[str, Any]], parent_pid: int) -> list[dict[str, Any]]:
    """The app-server children of one broker parent (never unrelated processes)."""
    return [
        p for p in processes
        if p["ppid"] == parent_pid and CHILD_MARKER in p["command"]
    ]


def broker_socket(broker: dict[str, Any]) -> str | None:
    match = re.search(r"--endpoint\s+unix:(\S+)", broker["command"])
    return match.group(1) if match else None


def busy_evidence(broker: dict[str, Any], mode: str) -> tuple[str, str]:
    """('idle'|'active'|'unknown', reason). Never guesses."""
    if mode == "idle":
        return "idle", "operator verified idle at a turn boundary"
    if mode == "active":
        return "active", "operator reported an in-flight review"
    path = broker_socket(broker)
    if not path:
        return "unknown", "broker endpoint not discoverable from the command line"
    if not os.path.exists(path):
        return "idle", "broker socket absent (not serving)"
    return "unknown", "broker socket present but busy state is not verifiable here"


def decide(
    refresh_epoch: float | None,
    refresh_error: str,
    broker: dict[str, Any] | None,
    busy: str,
    busy_reason: str,
) -> dict[str, Any]:
    """The whole policy, pure and testable."""
    if refresh_epoch is None:
        return {"action": FAIL_SAFE, "reason": refresh_error or "auth_refresh_missing_or_unparseable"}
    if broker is None:
        return {"action": NOOP, "reason": "no broker for this checkout; the next invocation spawns one fresh"}
    start = broker["start_epoch"]
    if start is None:
        return {"action": FAIL_SAFE, "reason": f"broker_start_epoch_unparseable:pid={broker['pid']}"}
    if start >= refresh_epoch:
        return {"action": NOOP, "reason": "broker start is not older than the auth refresh"}
    if busy == "idle":
        return {"action": RECYCLE, "reason": "broker predates the auth refresh and is verified idle"}
    if busy == "active":
        return {"action": DEFER, "reason": "broker predates the auth refresh but a review is in flight"}
    return {"action": DEFER, "reason": f"stale broker and busy state unknown ({busy_reason}); deferring"}


def build_plan(
    home: pathlib.Path,
    cwd: str,
    busy_mode: str,
    processes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    processes = list_processes() if processes is None else processes
    refresh_epoch, refresh_error = read_refresh_epoch(home)
    brokers = select_brokers(processes, cwd)
    entries = []
    for broker in brokers:
        busy, busy_reason = busy_evidence(broker, busy_mode)
        verdict = decide(refresh_epoch, refresh_error, broker, busy, busy_reason)
        entries.append({
            "broker_pid": broker["pid"],
            "broker_start_epoch": broker["start_epoch"],
            "busy": busy,
            "action": verdict["action"],
            "reason": verdict["reason"],
            "children": [c["pid"] for c in select_children(processes, broker["pid"])],
        })
    if not brokers:
        verdict = decide(refresh_epoch, refresh_error, None, "unknown", "")
        entries.append({"broker_pid": None, "action": verdict["action"], "reason": verdict["reason"], "children": []})
    return {
        "home": str(home),
        "cwd": cwd,
        "auth_refresh_epoch": refresh_epoch,
        "auth_refresh_error": refresh_error,
        "brokers": entries,
    }


def recycle(broker_pid: int, children: list[int]) -> tuple[bool, str]:
    """SIGTERM then SIGKILL one broker parent and its app-server children only."""
    if broker_pid <= 1 or broker_pid == os.getpid():
        return False, "refused_unsafe_pid"
    for pid in list(children) + [broker_pid]:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError:
            return False, f"permission_denied:pid={pid}"
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not any(_alive(p) for p in list(children) + [broker_pid]):
            return True, "recycled"
        time.sleep(0.2)
    for pid in list(children) + [broker_pid]:
        if _alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    return True, "recycled_after_sigkill"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", default=str(DEFAULT_HOME), help="companion CODEX_HOME")
    parser.add_argument("--cwd", required=True, help="checkout whose broker is being acquired")
    parser.add_argument("--busy", choices=("auto", "idle", "active"), default="auto")
    parser.add_argument("--recycle", action="store_true", help="perform the recycle (default is dry-run)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    plan = build_plan(pathlib.Path(args.home), args.cwd, args.busy)
    performed: list[dict[str, Any]] = []
    if args.recycle:
        processes = list_processes()
        for entry in plan["brokers"]:
            if entry["action"] == RECYCLE and entry["broker_pid"]:
                current = next((p for p in processes if p["pid"] == entry["broker_pid"]), None)
                if current is None or current["start_epoch"] != entry["broker_start_epoch"]:
                    performed.append({"broker_pid": entry["broker_pid"], "ok": False, "detail": "broker_identity_changed"})
                    continue
                ok, detail = recycle(entry["broker_pid"], entry["children"])
                performed.append({"broker_pid": entry["broker_pid"], "ok": ok, "detail": detail})
    plan["recycled"] = performed

    actions = {e["action"] for e in plan["brokers"]}
    if not args.json:
        for entry in plan["brokers"]:
            print(f"{entry['action']}: {entry['reason']} (broker_pid={entry['broker_pid']})")
    else:
        print(json.dumps(plan, indent=2, sort_keys=True))
    if FAIL_SAFE in actions:
        return 4
    if DEFER in actions:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
