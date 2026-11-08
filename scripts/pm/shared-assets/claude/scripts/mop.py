#!/usr/bin/env python3
"""Thin MoP REST CLI for PM (REST-only contract).

Replaces the mop_* MCP tools with the same contracts over HTTP. urllib only.
Every state transition goes through the HTTP coordinator; this process never
opens the MoP database or tmux directly.

Usage: mop <subcommand> [options]
  status --slot N | all | history --slot N [--limit N] | activity [--minutes N]
  send --slot N --command CMD [--no-force] [--raw] [--file F]
  release --slot N [--reason R] | respawn --slot N [--no-continue] [--model M]
  clear --slot N|pm|all | dnd --slot N --on|--off
  clear-composer --slot N [--no-force]   (empty a stuck composer; C-u only, never Enter)
  exit-pending --on|--off | exit-status | capture --slot N [--lines N]
  approve-plan --slot N [--option 2|4] [--comment T]
  ops-audit run|status|job|pause | pm-cadence status|run|pause
  reminder add --key K --thread TS --text T --every 30m|1h --until <ISO|+12h>
               [--channel C] [--stop-obligation N] [--first-at <ISO|+5m>] [--replace]
           list [--status active|stopped|expired|all] | cancel --key K
           (recurring HeyDonna Alerts bot posts into a Slack thread, owned by MoP)

stdout is JSON. Exit 0 on success, non-zero on refusal/transport failure.
Sanctioned path: mop (this CLI). The mop_* MCP tools are a deprecated
REST-only shim; direct DB/tmux access from clients is removed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


SANCTIONED_PATH = "mop"


def mop_base_url() -> str:
    port = os.environ.get("MOP_PORT", "3100")
    return f"http://127.0.0.1:{port}"


def http_json(method: str, url: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    data = None
    headers = {"accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["content-type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=60) as response:  # noqa: S310 - fixed local MoP base URL
            raw = response.read().decode("utf-8")
            return response.status, (json.loads(raw) if raw.strip() else {})
    except HTTPError as error:
        raw = error.read().decode("utf-8", "replace")
        try:
            return error.code, json.loads(raw)
        except json.JSONDecodeError:
            return error.code, {"error": raw[:400]}
    except URLError as error:
        return 0, {"error": f"mop_unreachable: {error}", "sanctioned_path": SANCTIONED_PATH}


REMINDER_MIN_INTERVAL_S = 600
REMINDER_MAX_HORIZON_S = 7 * 24 * 3600
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration_s(value: str) -> int:
    """`30m`, `1h`, `2d`, `900s` -> seconds."""
    match = re.fullmatch(r"(\d+)([smhd])", value.strip())
    if not match:
        raise ValueError(f"invalid duration {value!r}; use e.g. 30m, 1h, 2d")
    return int(match.group(1)) * _DURATION_UNITS[match.group(2)]


def parse_when(value: str, now: datetime | None = None) -> tuple[str, bool]:
    """`+12h` or an ISO timestamp -> (UTC ISO string, was_relative). Naive ISO means local time."""
    now = now or datetime.now(timezone.utc)
    text = value.strip()
    if text.startswith("+"):
        return (now + timedelta(seconds=parse_duration_s(text[1:]))).isoformat(), True
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"invalid time {value!r}; use +12h or an ISO timestamp") from error
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()  # local time, same as alerts-reminder.py --at
    return parsed.astimezone(timezone.utc).isoformat(), False


def build_reminder_add_body(args: argparse.Namespace, now: datetime | None = None) -> dict[str, Any]:
    interval_s = parse_duration_s(args.every)
    if interval_s < REMINDER_MIN_INTERVAL_S:
        raise ValueError(f"--every must be at least {REMINDER_MIN_INTERVAL_S // 60}m")
    until_at, until_relative = parse_when(args.until, now)
    body: dict[str, Any] = {
        "key": args.key,
        "thread_ts": args.thread,
        "text": args.text,
        "interval_s": interval_s,
        "until_at": until_at,
        "until_relative": until_relative,
        "replace": bool(args.replace),
        "created_by": args.created_by,
    }
    if args.channel:
        body["channel"] = args.channel
    if args.stop_obligation is not None:
        body["stop_obligation_id"] = args.stop_obligation
    if args.first_at:
        body["first_at"] = parse_when(args.first_at, now)[0]
    if args.source_ref:
        body["source_ref"] = args.source_ref
    return body


def emit(payload: Any, exit_code: int) -> int:
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mop", description="Thin MoP REST CLI (REST-only contract).")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p = sub.add_parser("status", help="GET /slots/:n")
    p.add_argument("--slot", type=int, required=True)

    sub.add_parser("all", help="GET /slots")

    p = sub.add_parser("history", help="GET /events for a slot")
    p.add_argument("--slot", type=int, required=True)
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("activity", help="GET /activity")
    p.add_argument("--minutes", type=int, default=60)

    p = sub.add_parser("send", help="POST /slots/:n/send")
    p.add_argument("--slot", type=int, required=True)
    p.add_argument("--command", default="")
    p.add_argument("--file", default="")
    p.add_argument("--no-force", action="store_true")
    p.add_argument("--raw", action="store_true")

    p = sub.add_parser("release", help="POST /slots/:n/release")
    p.add_argument("--slot", type=int, required=True)
    p.add_argument("--reason", default=None)

    p = sub.add_parser("respawn", help="POST /slots/:n/respawn")
    p.add_argument("--slot", type=int, required=True)
    p.add_argument("--no-continue", action="store_true")
    p.add_argument("--model", default=None)

    p = sub.add_parser("clear", help="POST /slots/:label/clear")
    p.add_argument("--slot", required=True, help="N, pm, or all")

    p = sub.add_parser("clear-composer", help="POST /slots/:n/clear-composer")
    p.add_argument("--slot", type=int, required=True)
    p.add_argument("--no-force", action="store_true")

    p = sub.add_parser("dnd", help="POST /slots/:n/dnd")
    p.add_argument("--slot", type=int, required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--on", action="store_true")
    g.add_argument("--off", action="store_true")

    p = sub.add_parser("exit-pending", help="POST /exit-pending")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--on", action="store_true")
    g.add_argument("--off", action="store_true")

    sub.add_parser("exit-status", help="GET /exit-status")

    p = sub.add_parser("capture", help="GET /slots/:n/capture")
    p.add_argument("--slot", type=int, required=True)
    p.add_argument("--lines", type=int, default=30)

    p = sub.add_parser("approve-plan", help="POST /slots/:n/approve-plan")
    p.add_argument("--slot", type=int, required=True)
    p.add_argument("--option", default="2", choices=["2", "4"])
    p.add_argument("--comment", default=None)

    p = sub.add_parser("ops-audit", help="ops-audit passthrough")
    p.add_argument("action", choices=["run", "status", "job", "pause"])
    p.add_argument("--job-id", default=None)

    p = sub.add_parser("pm-cadence", help="pm-cadence passthrough")
    p.add_argument("action", choices=["status", "run", "pause"])

    p = sub.add_parser("reminder", help="recurring HeyDonna Alerts reminders (/alert-reminders)")
    rsub = p.add_subparsers(dest="reminder_action", required=True)
    r = rsub.add_parser("add", help="create a recurring reminder; idempotent on --key")
    r.add_argument("--key", required=True)
    r.add_argument("--thread", required=True, help="Slack thread root ts, e.g. 1793953549.727149")
    r.add_argument("--text", required=True)
    r.add_argument("--every", required=True, help="interval, minimum 10m (e.g. 30m, 1h)")
    r.add_argument("--until", required=True, help="stop time, ISO or +12h; at most 7 days out")
    r.add_argument("--channel", default=None, help="default C0ALZJHGE49")
    r.add_argument("--stop-obligation", type=int, default=None, help="pm-ops obligation id; reminder stops when it resolves")
    r.add_argument("--first-at", default=None, help="first post time, ISO or +5m (default: one interval from now)")
    r.add_argument("--replace", action="store_true", help="overwrite an active reminder that has different args")
    r.add_argument("--created-by", default="cli")
    r.add_argument("--source-ref", default=None)
    r = rsub.add_parser("list", help="list reminders")
    r.add_argument("--status", default="active", choices=["active", "stopped", "expired", "all"])
    r = rsub.add_parser("cancel", help="stop a reminder")
    r.add_argument("--key", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base = mop_base_url()
    cmd = args.subcommand

    if cmd == "status":
        code, data = http_json("GET", f"{base}/slots/{args.slot}")
        return emit(data, 0 if code == 200 else 1)
    if cmd == "all":
        code, data = http_json("GET", f"{base}/slots")
        return emit(data, 0 if code == 200 else 1)
    if cmd == "history":
        code, data = http_json("GET", f"{base}/events?slot={args.slot}&limit={args.limit}")
        return emit(data.get("events", data), 0 if code == 200 else 1)
    if cmd == "activity":
        code, data = http_json("GET", f"{base}/activity?minutes={args.minutes}")
        return emit(data, 0 if code == 200 else 1)
    if cmd == "send":
        body: dict[str, Any] = {"command": args.command, "force": not args.no_force}
        if args.file:
            body["file"] = args.file
        if args.raw:
            body["raw"] = True
        code, data = http_json("POST", f"{base}/slots/{args.slot}/send", body)
        ok = code == 200 and isinstance(data, dict) and data.get("success") is True
        return emit(data, 0 if ok else 1)
    if cmd == "release":
        body = {"slot": args.slot, "reason": args.reason}
        code, data = http_json("POST", f"{base}/slots/{args.slot}/release", body)
        ok = code == 200 and isinstance(data, dict) and data.get("success") is True
        return emit(data, 0 if ok else 1)
    if cmd == "respawn":
        body = {"continue_session": not args.no_continue}
        if args.model:
            body["model"] = args.model
        code, data = http_json("POST", f"{base}/slots/{args.slot}/respawn", body)
        return emit(data, 0 if code == 200 else 1)
    if cmd == "clear":
        label = str(args.slot)
        code, data = http_json("POST", f"{base}/slots/{label}/clear", {"source": "mop-cli"})
        return emit(data, 0 if code == 200 else 1)
    if cmd == "clear-composer":
        code, data = http_json(
            "POST", f"{base}/slots/{args.slot}/clear-composer", {"force": not args.no_force}
        )
        ok = code == 200 and isinstance(data, dict) and data.get("success") is True
        return emit(data, 0 if ok else 1)
    if cmd == "dnd":
        code, data = http_json("POST", f"{base}/slots/{args.slot}/dnd", {"dnd": bool(args.on)})
        return emit(data, 0 if code == 200 else 1)
    if cmd == "exit-pending":
        code, data = http_json("POST", f"{base}/exit-pending", {"enabled": bool(args.on)})
        return emit(data, 0 if code == 200 else 1)
    if cmd == "exit-status":
        code, data = http_json("GET", f"{base}/exit-status")
        return emit(data, 0 if code == 200 else 1)
    if cmd == "capture":
        code, data = http_json("GET", f"{base}/slots/{args.slot}/capture?lines={args.lines}")
        return emit(data, 0 if code == 200 else 1)
    if cmd == "approve-plan":
        body = {"option": args.option}
        if args.comment:
            body["comment"] = args.comment
        code, data = http_json("POST", f"{base}/slots/{args.slot}/approve-plan", body)
        ok = code == 200 and isinstance(data, dict) and data.get("success") is True
        return emit(data, 0 if ok else 1)
    if cmd == "ops-audit":
        if args.action == "run":
            code, data = http_json("POST", f"{base}/ops-audit/run", {})
        elif args.action == "status":
            code, data = http_json("GET", f"{base}/ops-audit/status")
        elif args.action == "job":
            if not args.job_id:
                return emit({"error": "ops-audit job requires --job-id", "sanctioned_path": SANCTIONED_PATH}, 2)
            code, data = http_json("GET", f"{base}/ops-audit/jobs/{args.job_id}")
        else:
            code, data = http_json("POST", f"{base}/ops-audit/pause", {})
        return emit(data, 0 if code == 200 else 1)
    if cmd == "pm-cadence":
        if args.action == "status":
            code, data = http_json("GET", f"{base}/pm-cadence/status")
        else:
            code, data = http_json("POST", f"{base}/pm-cadence/{args.action}", {})
        return emit(data, 0 if code == 200 else 1)
    if cmd == "reminder":
        if args.reminder_action == "add":
            try:
                body = build_reminder_add_body(args)
            except ValueError as error:
                return emit({"error": str(error), "sanctioned_path": SANCTIONED_PATH}, 2)
            code, data = http_json("POST", f"{base}/alert-reminders", body)
        elif args.reminder_action == "list":
            code, data = http_json("GET", f"{base}/alert-reminders?status={args.status}")
        else:
            code, data = http_json("POST", f"{base}/alert-reminders/{quote(args.key, safe='')}/cancel", {})
        ok = code == 200 and isinstance(data, dict) and data.get("success") is True
        return emit(data, 0 if ok else 1)
    return emit({"error": f"unknown subcommand {cmd}", "sanctioned_path": SANCTIONED_PATH}, 2)


if __name__ == "__main__":
    raise SystemExit(main())
