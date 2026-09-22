#!/usr/bin/env python3
"""Pre-acquisition freshness gate for Codex companion app-server brokers.

A broker loads credentials at startup and keeps presenting them for its whole
life, so any auth refresh (routine token refresh, or the 2026-09-19 auth-store
migration) makes every older broker present a stale credential.

Decision contract
  fresh                        -> noop, reuse the broker
  stale + live-verified idle   -> recycle that broker parent and its app-server children
  stale + live-verified active -> defer without interruption, typed diagnostic
  stale + unverifiable/unknown -> defer (typed); never guess
  missing / invalid epochs     -> fail safe with a typed diagnostic

Live idle/active evidence is the broker's own JSON-RPC surface: a fresh
connection sends the only read-only app-server method, `thread/list`. The broker
answers rpcCode -32001 ("Shared Codex broker is busy.", BROKER_BUSY_RPC_CODE in
the plugin's lib/app-server.mjs) BEFORE forwarding when another socket holds the
request/stream, so -32001 means active and any other answer means idle. Socket
file presence is never used as busy-state evidence.

It reads ONLY the auth store's top-level `last_refresh` timestamp. It never
reads, copies, logs or returns token material.

Upgrade-safe by construction: a check that runs BEFORE the broker, not a patch
to the vendor broker. Delete it once the upstream plugin reloads credentials or
enforces equivalent per-request/TTL freshness, and that behaviour is verified.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import signal
import socket
import subprocess
import sys
import time
from typing import Any, Callable

DEFAULT_HOME = pathlib.Path("/Users/rajiv/.codex-companions")
BROKER_MARKER = "app-server-broker.mjs"
CHILD_MARKER = "app-server"
BUSY_RPC_CODE = -32001           # vendor BROKER_BUSY_RPC_CODE
PROBE_TIMEOUT_SECONDS = 3.0
PROBE_METHOD = "thread/list"     # the only read-only method the app-server exposes

NOOP = "noop"
RECYCLE = "recycle"
DEFER = "defer"
FAIL_SAFE = "fail_safe"
IDLE = "idle"
ACTIVE = "active"
UNKNOWN = "unknown"


def parse_iso_epoch(value: Any) -> float | None:
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
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.strptime(value.strip(), "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return None
    return parsed.timestamp()


def read_refresh_epoch(home: pathlib.Path) -> tuple[float | None, str]:
    """(epoch, error) for the companion home auth refresh time.

    Reads ONLY the top-level `last_refresh` field; token material is never
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
        if not pid.isdigit() or not ppid.isdigit():
            continue
        rows.append({
            "pid": int(pid), "ppid": int(ppid),
            "start_epoch": parse_ps_lstart(" ".join(parts[2:7])),
            "command": parts[7],
        })
    return rows


def command_cwd(command: str) -> str | None:
    """The EXACT --cwd value, so /work/co1 cannot prefix-match /work/co10."""
    match = re.search(r"(?:^|\s)--cwd(?:=|\s+)(\S+)", command)
    return match.group(1) if match else None


def command_endpoint(command: str) -> str | None:
    match = re.search(r"(?:^|\s)--endpoint(?:=|\s+)unix:(\S+)", command)
    return match.group(1) if match else None


def select_brokers(processes: list[dict[str, Any]], cwd: str) -> list[dict[str, Any]]:
    picked = [
        p for p in processes
        if BROKER_MARKER in p["command"] and command_cwd(p["command"]) == cwd
    ]
    return sorted(picked, key=lambda p: (p["start_epoch"] or 0.0, p["pid"]))


def select_children(processes: list[dict[str, Any]], parent_pid: int) -> list[dict[str, Any]]:
    return [p for p in processes if p["ppid"] == parent_pid and CHILD_MARKER in p["command"]]


def probe_busy(endpoint: str | None, timeout: float = PROBE_TIMEOUT_SECONDS) -> tuple[str, str]:
    """Live idle/active evidence via the broker's own read-only RPC surface."""
    if not endpoint:
        return UNKNOWN, "broker endpoint not discoverable from the command line"
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        try:
            client.connect(endpoint)
        except (FileNotFoundError, ConnectionRefusedError):
            # No listener: the broker is not serving, so it cannot be busy.
            return IDLE, "broker endpoint has no listener (not serving)"
        except OSError as exc:
            return UNKNOWN, f"broker connect failed:{type(exc).__name__}"
        client.sendall((json.dumps({"id": 1, "method": PROBE_METHOD, "params": {}}) + "\n").encode())
        buffer = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                chunk = client.recv(65536)
            except socket.timeout:
                return UNKNOWN, "broker probe timed out"
            if not chunk:
                break
            buffer += chunk
            if b"\n" in buffer:
                break
        line = buffer.split(b"\n", 1)[0]
        if not line:
            return UNKNOWN, "broker probe returned no response"
        try:
            message = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return UNKNOWN, "broker probe response was not JSON"
        error = message.get("error") if isinstance(message, dict) else None
        if isinstance(error, dict) and error.get("code") == BUSY_RPC_CODE:
            return ACTIVE, "broker answered BROKER_BUSY_RPC_CODE"
        if isinstance(message, dict) and ("result" in message or "error" in message):
            return IDLE, "broker answered a read-only probe without the busy code"
        return UNKNOWN, "broker probe response was not a JSON-RPC reply"
    finally:
        try:
            client.close()
        except OSError:
            pass


def busy_state(
    broker: dict[str, Any],
    mode: str,
    probe: Callable[[str | None], tuple[str, str]] = probe_busy,
) -> tuple[str, str]:
    if mode == "idle":
        return IDLE, "operator asserted idle"
    if mode == "active":
        return ACTIVE, "operator asserted an in-flight review"
    return probe(command_endpoint(broker["command"]))


def decide(refresh_epoch, refresh_error, broker, busy, busy_reason) -> dict[str, Any]:
    if refresh_epoch is None:
        return {"action": FAIL_SAFE, "reason": refresh_error or "auth_refresh_missing_or_unparseable"}
    if broker is None:
        return {"action": NOOP, "reason": "no broker for this checkout; the next invocation spawns one fresh"}
    start = broker["start_epoch"]
    if start is None:
        return {"action": FAIL_SAFE, "reason": f"broker_start_epoch_unparseable:pid={broker['pid']}"}
    if start >= refresh_epoch:
        return {"action": NOOP, "reason": "broker start is not older than the auth refresh"}
    if busy == IDLE:
        return {"action": RECYCLE, "reason": "broker predates the auth refresh and the live probe reports idle"}
    if busy == ACTIVE:
        return {"action": DEFER, "reason": "broker predates the auth refresh but the live probe reports active"}
    return {"action": DEFER, "reason": f"stale broker and busy state unverified ({busy_reason})"}


def build_plan(
    home: pathlib.Path,
    cwd: str,
    busy_mode: str,
    processes: list[dict[str, Any]] | None = None,
    probe: Callable[[str | None], tuple[str, str]] = probe_busy,
) -> dict[str, Any]:
    processes = list_processes() if processes is None else processes
    refresh_epoch, refresh_error = read_refresh_epoch(home)
    brokers = select_brokers(processes, cwd)
    entries = []
    for broker in brokers:
        busy, busy_reason = busy_state(broker, busy_mode, probe)
        verdict = decide(refresh_epoch, refresh_error, broker, busy, busy_reason)
        entries.append({
            "broker_pid": broker["pid"],
            "broker_start_epoch": broker["start_epoch"],
            "busy": busy,
            "busy_reason": busy_reason,
            "action": verdict["action"],
            "reason": verdict["reason"],
            "children": [c["pid"] for c in select_children(processes, broker["pid"])],
        })
    if not brokers:
        verdict = decide(refresh_epoch, refresh_error, None, UNKNOWN, "")
        entries.append({"broker_pid": None, "action": verdict["action"], "reason": verdict["reason"], "children": []})
    return {
        "home": str(home), "cwd": cwd,
        "auth_refresh_epoch": refresh_epoch, "auth_refresh_error": refresh_error,
        "brokers": entries,
    }


def ensure_fresh(
    home: pathlib.Path,
    cwd: str,
    busy_mode: str,
    *,
    processes: list[dict[str, Any]] | None = None,
    probe: Callable[[str | None], tuple[str, str]] = probe_busy,
    killer: Callable[[int, list[int]], tuple[bool, str]] | None = None,
) -> dict[str, Any]:
    """Plan, then perform at most one scoped recycle per stale-idle broker.

    Idle is re-probed immediately before any signal, so a review that starts
    after planning is never interrupted.
    """
    procs = list_processes() if processes is None else processes
    plan = build_plan(home, cwd, busy_mode, processes=procs, probe=probe)
    killer = recycle if killer is None else killer
    performed: list[dict[str, Any]] = []
    for entry in plan["brokers"]:
        if entry["action"] != RECYCLE or not entry["broker_pid"]:
            continue
        current = next((p for p in procs if p["pid"] == entry["broker_pid"]), None)
        if current is None or current["start_epoch"] != entry["broker_start_epoch"]:
            performed.append({"broker_pid": entry["broker_pid"], "ok": False, "detail": "broker_identity_changed"})
            entry["action"], entry["reason"] = DEFER, "broker identity changed before recycle"
            continue
        if busy_mode == "auto":
            recheck, why = probe(command_endpoint(current["command"]))
            if recheck != IDLE:
                performed.append({"broker_pid": entry["broker_pid"], "ok": False, "detail": f"idle_recheck_failed:{recheck}"})
                entry["action"], entry["reason"] = DEFER, f"idle re-check before recycle reported {recheck} ({why})"
                continue
        ok, detail = killer(entry["broker_pid"], entry["children"])
        performed.append({"broker_pid": entry["broker_pid"], "ok": ok, "detail": detail})
        if ok:
            entry["action"], entry["reason"] = RECYCLE, "recycled; the next invocation spawns a fresh broker"
    plan["recycled"] = performed
    return plan


def recycle(broker_pid: int, children: list[int]) -> tuple[bool, str]:
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
    parser.add_argument("--home", default=str(DEFAULT_HOME))
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--busy", choices=("auto", "idle", "active"), default="auto")
    parser.add_argument("--recycle", action="store_true",
                        help="perform the guarded recycle for live-verified stale-idle brokers (default: dry-run)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.recycle:
        plan = ensure_fresh(pathlib.Path(args.home), args.cwd, args.busy)
    else:
        plan = build_plan(pathlib.Path(args.home), args.cwd, args.busy)

    for entry in plan["brokers"]:
        if not args.json:
            print(f"{entry['action']}: {entry['reason']} (broker_pid={entry['broker_pid']})")
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))

    actions = {e["action"] for e in plan["brokers"]}
    if FAIL_SAFE in actions:
        return 4
    if DEFER in actions:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
