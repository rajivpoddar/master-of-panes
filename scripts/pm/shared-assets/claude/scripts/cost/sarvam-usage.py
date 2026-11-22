#!/usr/bin/env python3
"""Sarvam Token Factory burn line for the hourly report.

Prints one line:
  Sarvam TF: <used>/<allowance> this month, burn <x>/h (<N>h avg), balance <y> -- <basis>

Sarvam publishes no usage, credit or balance API (the docs point at the dashboard), and CLIProxyAPI keeps no usage
statistics (usage-statistics-enabled: false). So the figure is ESTIMATED: per-request token counts recorded in the
proxy-lane transcripts (/tmp/proxy-agent/*.jsonl, `assistant` events whose model is a `sarvam-*` gateway alias) times
the published rupee price per 1M tokens. Credits are treated as rupees. It is a LOWER BOUND: nested alias requests
made inside non-Sarvam lanes, curl smokes and the dashboard's own accounting are not seen.

Never invents a number. A missing input is reported as UNAVAILABLE with its reason:
  - allowance: set only by the operator (env SARVAM_TF_ALLOWANCE_INR, or that key in ~/.claude/state/proxy-caps.env);
    no primary source states one, so there is no default. No allowance means no balance either.
  - transcripts: unreadable and no ledger means the whole line is UNAVAILABLE.

State: --record appends deduped per-message token rows plus a scan watermark to
~/.claude/state/sarvam-usage-ledger.jsonl, so the month total survives /tmp being cleared and later scans only read
files changed since the watermark. Without --record the script is read-only (the dry-run path). It never reads the
Sarvam key, never posts to Slack and never touches the pm-ops DB.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
HOME = Path.home()
TRANSCRIPTS = Path(os.environ.get("SARVAM_USAGE_TRANSCRIPTS", "/tmp/proxy-agent"))
LEDGER = HOME / ".claude/state/sarvam-usage-ledger.jsonl"
CAPS = HOME / ".claude/state/proxy-caps.env"
ALLOWANCE_KEY = "SARVAM_TF_ALLOWANCE_INR"
WINDOW_H = 3.0
SCAN_DEADLINE_S = 20.0
SCAN_OVERLAP_S = 120  # rescan margin below the watermark; rows dedupe by message id
MODEL_MARK = b'"model":"sarvam-'

# Rupees per 1M tokens (input, output), keyed by CLIProxyAPI gateway alias. Source: Sarvam Token Factory published price
# list (pulled 2026-11-23). sarvam-105b and sarvam-deepseek-v4.1-flash are introductory prices (the deepseek one runs
# through end of 2026), so re-check this table when they end. Reasoning tokens bill as output. No cached-input discount is
# published, so cache reads are charged as input. sarvam-kimi-k3 is Provisioned Throughput (billed by capacity, not
# tokens), so it is deliberately absent: its messages are counted as "unpriced", never costed.
PRICES = {
    "sarvam-glm-5.3": (126.0, 390.0),
    "sarvam-deepseek-v4.1-flash": (14.0, 55.0),
    "sarvam-105b": (10.0, 25.0),
    "sarvam-gemma4": (35.0, 90.0),
}

_NUM = re.compile(r"[0-9]+(?:\.[0-9]+)?")


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _parse_ts(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _month_key(moment: datetime) -> str:
    return moment.astimezone(IST).strftime("%Y-%m")


def _month_start_epoch(now: datetime) -> float:
    local = now.astimezone(IST)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def _merge(rows: dict, row: dict) -> None:
    """One row per message id: earliest timestamp, largest token counts (streaming events carry partial usage)."""
    old = rows.get(row["id"])
    if old is None:
        rows[row["id"]] = dict(row)
        return
    old["in"] = max(old["in"], row["in"])
    old["out"] = max(old["out"], row["out"])
    old_t, new_t = _parse_ts(old.get("ts")), _parse_ts(row.get("ts"))
    if new_t and (old_t is None or new_t < old_t):
        old["ts"] = row["ts"]


def read_ledger(path: Path):
    """(rows by id, newest scan watermark epoch or None, exists). Tolerates torn or foreign lines."""
    rows: dict = {}
    watermark = None
    try:
        text = path.read_text(errors="replace")
    except FileNotFoundError:
        return rows, None, False
    except OSError:
        return rows, None, False
    for raw in text.splitlines():
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        if isinstance(data.get("scan_at"), (int, float)):
            watermark = max(watermark or 0.0, float(data["scan_at"]))
            continue
        if isinstance(data.get("id"), str) and isinstance(data.get("model"), str):
            _merge(rows, {"id": data["id"], "ts": data.get("ts"), "model": data["model"],
                          "in": _int(data.get("in")), "out": _int(data.get("out"))})
    return rows, watermark, True


def scan_transcripts(root: Path, since_epoch: float, deadline: float):
    """Rows for `sarvam-*` assistant messages in lane transcripts modified at/after since_epoch.

    Returns (rows by id, info) where info has error (str|None), partial (bool), files (int), no_id (int).
    A file is only parsed when the byte pre-check finds a sarvam model marker, so scanning GBs of unrelated lane logs
    stays a memchr-speed pass; the JSON check below still requires an `assistant` event whose message.model matches
    (a transcript that merely mentions "sarvam" in text is not counted).
    """
    rows: dict = {}
    info = {"error": None, "partial": False, "files": 0, "no_id": 0}
    try:
        entries = list(os.scandir(root))
    except OSError as exc:
        info["error"] = f"{type(exc).__name__}"
        return rows, info
    for entry in entries:
        if not entry.name.endswith(".jsonl"):
            continue
        if time.monotonic() > deadline:
            info["partial"] = True
            break
        try:
            if entry.stat().st_mtime < since_epoch:
                continue
            with open(entry.path, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        info["files"] += 1
        if MODEL_MARK not in data:
            continue
        for line in data.split(b"\n"):
            if MODEL_MARK not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("type") != "assistant":
                continue
            message = event.get("message")
            if not isinstance(message, dict):
                continue
            model = message.get("model")
            if not isinstance(model, str) or not model.startswith("sarvam-"):
                continue
            usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
            msg_id = message.get("id")
            if not isinstance(msg_id, str) or not msg_id:
                info["no_id"] += 1
                continue
            _merge(rows, {
                "id": msg_id, "ts": event.get("timestamp"), "model": model,
                "in": _int(usage.get("input_tokens")) + _int(usage.get("cache_read_input_tokens"))
                + _int(usage.get("cache_creation_input_tokens")),
                "out": _int(usage.get("output_tokens")),
            })
    return rows, info


def read_allowance(environ, caps_path: Path):
    """(rupees or None, reason). Operator-set only: env first, then the first terminated key line of the caps file."""
    raw = (environ.get(ALLOWANCE_KEY) or "").strip()
    source = f"env {ALLOWANCE_KEY}"
    if not raw:
        source = "proxy-caps.env"
        try:
            text = caps_path.read_text(errors="replace")
        except OSError:
            text = ""
        for line in text.split("\n")[:-1]:  # an unterminated last line is ignored, like the launcher's read loop
            key, sep, value = line.partition("=")
            if sep and key == ALLOWANCE_KEY:
                raw = value.strip()
                break
    if not raw:
        return None, "no allowance configured"
    if not _NUM.fullmatch(raw) or float(raw) <= 0:
        return None, f"allowance invalid in {source}"
    return float(raw), ""


def summarize(rows: dict, now: datetime, window_h: float) -> dict:
    month = _month_key(now)
    cutoff = now - timedelta(hours=window_h)
    used = window_cost = 0.0
    priced = 0
    unpriced: dict = {}
    for row in rows.values():
        moment = _parse_ts(row.get("ts"))
        if moment is None or _month_key(moment) != month:
            continue
        price = PRICES.get(row["model"])
        if price is None:
            unpriced[row["model"]] = unpriced.get(row["model"], 0) + 1
            continue
        cost = (row["in"] * price[0] + row["out"] * price[1]) / 1_000_000
        used += cost
        priced += 1
        if cutoff <= moment <= now:
            window_cost += cost
    return {"month": month, "used_inr": used, "window_inr": window_cost, "priced_msgs": priced, "unpriced": unpriced}


def _inr(value: float) -> str:
    return f"₹{value:,.2f}"


def format_line(summary: dict, allowance, allowance_reason: str, window_h: float, burn_reason: str,
                partial: bool) -> str:
    used = summary["used_inr"]
    allow = f"₹{allowance:,.0f}" if allowance is not None else f"UNAVAILABLE ({allowance_reason})"
    if burn_reason:
        burn = f"n/a ({burn_reason})"
    else:
        burn = f"{_inr(summary['window_inr'] / window_h)}/h ({window_h:g}h avg)"
    if allowance is None:
        balance = f"UNAVAILABLE ({allowance_reason})"
    else:
        balance = f"≤{_inr(max(allowance - used, 0.0))}"
    notes = ["est. from lane transcripts x published price, lower bound"]
    if allowance is None and allowance_reason == "no allowance configured":
        notes.append(f"Sarvam publishes no balance API; set {ALLOWANCE_KEY} for a balance")
    if summary["unpriced"]:
        notes.append("unpriced msgs: " + ", ".join(f"{m} x{n}" for m, n in sorted(summary["unpriced"].items())))
    if partial:
        notes.append("partial scan (deadline)")
    return (f"Sarvam TF: {_inr(used)}/{allow} this month, burn {burn}, balance {balance} -- " + "; ".join(notes))


def collect(now: datetime, root: Path = TRANSCRIPTS, ledger: Path = LEDGER, caps: Path = CAPS, environ=None,
            window_h: float = WINDOW_H, deadline_s: float = SCAN_DEADLINE_S, record: bool = False) -> dict:
    environ = os.environ if environ is None else environ
    rows, watermark, ledger_exists = read_ledger(ledger)
    scan_started = now.timestamp()  # the scan runs at `now`; it is also the watermark a --record run stores
    since = max(_month_start_epoch(now), (watermark - SCAN_OVERLAP_S) if watermark else 0.0)
    fresh, info = scan_transcripts(root, since, time.monotonic() + deadline_s)
    allowance, allowance_reason = read_allowance(environ, caps)
    if info["error"] and not ledger_exists:
        line = (f"Sarvam TF: UNAVAILABLE (lane transcripts unreadable: {info['error']}, and no ledger yet; "
                "nothing to estimate from)")
        return {"line": line, "available": False, "recorded": 0}
    new_rows = 0
    for row in fresh.values():
        before = rows.get(row["id"])
        _merge(rows, row)
        after = rows[row["id"]]
        if before is None or (before["in"], before["out"]) != (after["in"], after["out"]):
            new_rows += 1
    summary = summarize(rows, now, window_h)
    # Burn is only trustworthy when a scan actually covered the window: a failed scan on a stale ledger would read as
    # a quiet hour and understate real spend.
    last_scan = scan_started if not info["error"] else watermark
    burn_reason = ""
    if last_scan is None or now.timestamp() - last_scan > window_h * 3600:
        burn_reason = f"no lane-transcript scan in the last {window_h:g}h"
    elif info["partial"]:
        burn_reason = "scan hit its deadline"
    line = format_line(summary, allowance, allowance_reason, window_h, burn_reason, info["partial"])
    recorded = 0
    if record and not info["error"] and not info["partial"]:
        batch = []
        for row in fresh.values():
            batch.append(json.dumps({k: rows[row["id"]][k] for k in ("id", "ts", "model", "in", "out")},
                                    separators=(",", ":")))
        batch.append(json.dumps({"scan_at": scan_started}, separators=(",", ":")))
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with open(ledger, "a") as fh:
            fh.write("\n".join(batch) + "\n")
        recorded = len(fresh)
    return {"line": line, "available": True, "recorded": recorded, "new_rows": new_rows, "summary": summary,
            "allowance": allowance, "files_scanned": info["files"], "no_id": info["no_id"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--record", action="store_true", help="append new per-message rows + scan watermark to the ledger")
    parser.add_argument("--json", action="store_true", help="print the full summary as JSON instead of the line")
    parser.add_argument("--now", help="ISO time override (tests/dry-run)")
    parser.add_argument("--transcripts", default=str(TRANSCRIPTS))
    parser.add_argument("--ledger", default=str(LEDGER))
    parser.add_argument("--caps", default=str(CAPS))
    parser.add_argument("--window-hours", type=float, default=WINDOW_H)
    parser.add_argument("--deadline-s", type=float, default=SCAN_DEADLINE_S)
    args = parser.parse_args(argv)
    now = _parse_ts(args.now) if args.now else datetime.now(timezone.utc)
    if now is None or args.window_hours <= 0:
        print("Sarvam TF: UNAVAILABLE (bad --now or --window-hours)")
        return 0
    try:
        result = collect(now, Path(args.transcripts), Path(args.ledger), Path(args.caps), None, args.window_hours,
                         args.deadline_s, args.record)
    except Exception as exc:  # the hourly must always get a line, never a traceback
        print(f"Sarvam TF: UNAVAILABLE ({type(exc).__name__})")
        return 0
    print(json.dumps(result, default=str, indent=2) if args.json else result["line"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
