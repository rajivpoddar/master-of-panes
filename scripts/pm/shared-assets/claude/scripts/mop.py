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
  exit-pending --on|--off | exit-status | capture --slot N [--lines N]
  approve-plan --slot N [--option 2|4] [--comment T]
  ops-audit run|status|job|pause | pm-cadence status|run|pause

stdout is JSON. Exit 0 on success, non-zero on refusal/transport failure.
Sanctioned path: mop (this CLI). The mop_* MCP tools are a deprecated
REST-only shim; direct DB/tmux access from clients is removed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
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
    return emit({"error": f"unknown subcommand {cmd}", "sanctioned_path": SANCTIONED_PATH}, 2)


if __name__ == "__main__":
    raise SystemExit(main())
