#!/usr/bin/env python3
"""Sarvam Token Factory burn line for the hourly report.

Prints one line:
  Sarvam TF: measured estimate <used> this month (<basis>), burn <x>/h (<N>h avg), <balance> -- <notes>

Sarvam publishes no usage, credit or balance API (the docs point at the dashboard), and CLIProxyAPI keeps no usage
statistics (usage-statistics-enabled: false). So the figure is a MEASURED ESTIMATE: token usage that Claude Code
recorded for each proxy lane (/tmp/proxy-agent/*.jsonl) times the published rupee rates per 1M tokens (ex-GST).

Attribution is by REGISTERED ROUTE ONLY. A lane is Sarvam usage when its launch manifest (<stem>.log LAUNCH ... model_alias=)
or its init event names a registered gateway alias (a `sarvam-*` entry of ROUTES). Bare upstream ids (`glm5.3`,
`deepseekv4.1-flash-1017`, ...) are never enough on their own: other providers sell the same upstream models (a GLM lane
routed via OpenCode prints `glm-5.3` and would be misattributed), so usage from a lane with no registered Sarvam route is
ignored, whatever id its events print. Within a Sarvam lane the usage counted is the lane's own `result` events for that
alias: inputTokens, cacheReadInputTokens, cacheCreationInputTokens and outputTokens as the upstream reported them. The
per-message `usage` in `assistant` events is NOT used for cost: for Sarvam routes it carries a prompt size and a zero
output_tokens placeholder, and it prints the upstream id (`glm5.3`), not the alias.

Continuations are counted per completion, exactly once. The launcher appends continuation results to the same lane
transcript, and a re-invoked lane RESETS its cumulative counter, so "max per field at one file time" both overcounts and
undercounts. Each `result` event is bound to its invocation by its own `uuid` and carries `model_usage_since_last_result`,
the exact usage of that completion; the lane's totals are the SUM of those per-completion deltas (one row id per result
uuid, merged by max, so a re-scan of an unchanged file can only re-observe the same rows). A lane that has not written a
result yet (in flight, or killed first) therefore has no verified usage; it is reported as such and never costed.

Never invents a number, and never prints a bare zero for a missing input. Each gap is reported with its reason:
  - no verified usage at all but Sarvam lanes exist (in flight, unpriced route, or an unregistered alias/upstream id):
    the measured estimate is UNAVAILABLE with that reason, not 0.
  - balance: Sarvam has no balance API, so the provider balance is UNAVAILABLE until the operator sets an allowance
    (env SARVAM_TF_ALLOWANCE_INR, or that key in ~/.claude/state/proxy-caps.env). With one set, the line shows the
    allowance minus the measured estimate, labelled as an allowance, never as a provider reading. No default exists.
  - transcripts: unreadable and no ledger means the whole line is UNAVAILABLE.

State: --record appends deduped per-completion usage rows plus a scan watermark to ~/.claude/state/sarvam-usage-ledger.jsonl,
so the month total survives /tmp being cleared and later scans only read files changed since the watermark. Each row id
is lane-stem:route:result-uuid, so an unchanged lane re-scanned past the watermark dedupes to itself. Rows and watermarks
carry "v": 2; anything else in an older ledger is ignored (and the month is rescanned). Without --record the script is
read-only (the dry-run path). It never reads the Sarvam key, never posts to Slack and never touches the pm-ops DB.
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
NO_ALLOWANCE = "no operator allowance set"
WINDOW_H = 3.0
SCAN_DEADLINE_S = 20.0
SCAN_OVERLAP_S = 120  # rescan margin below the watermark; rows dedupe by id
SCHEMA = 2
ALIAS_PREFIX = "sarvam-"
LOG_HEAD_BYTES = 65536

# One registered route per CLIProxyAPI gateway alias: the upstream id the provider reports back in message.model, and
# rupees per 1M tokens (input, cached input, output), ex-GST. Source: Sarvam Token Factory published price list (pulled
# 2026-11-23). sarvam-105b and sarvam-deepseek-v4.1-flash carry introductory prices (the deepseek one runs through end
# of 2026), so re-check this table when they end. Reasoning tokens bill as output. Cache writes have no published
# charge, so they are billed as ordinary input. sarvam-kimi-k3 is Provisioned Throughput (billed by capacity, not
# tokens): its price is None, so its usage is counted as "unpriced" and never costed.
ROUTES = {
    "sarvam-glm-5.3": {"upstream": "glm5.3", "price": (126.0, 20.0, 390.0)},
    "sarvam-deepseek-v4.1-flash": {"upstream": "deepseekv4.1-flash-1017", "price": (14.0, 0.25, 55.0)},
    "sarvam-105b": {"upstream": "sarvam-105b", "price": (10.0, 0.25, 25.0)},
    "sarvam-gemma4": {"upstream": "gemma4", "price": (35.0, 10.0, 90.0)},
    "sarvam-kimi-k3": {"upstream": "kimi-k3", "price": None},
}
UPSTREAM_ALIAS = {route["upstream"]: alias for alias, route in ROUTES.items()}
# Cheap whole-file precheck: a transcript is only parsed when it names a registered alias or a quoted upstream id
# (derived from ROUTES, so a new route needs no second edit here).
MARKS = tuple(f'"{name}"'.encode() for name in (*ROUTES, *UPSTREAM_ALIAS))

_NUM = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_LAUNCH_ALIAS = re.compile(rb"\bLAUNCH\b[^\n]*?\bmodel_alias=(\S+)")


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


def resolve_route(model) -> str | None:
    """REGISTERED gateway alias for an alias OR its upstream id, else None.

    Only ever consulted for a lane that already carries a registered Sarvam route (manifest/init alias), so a bare
    upstream id from a foreign-provider lane is never attributed on its own. Foreign aliases like `glm-5.3` (OpenCode)
    do not match `glm5.3` (Sarvam's upstream id) and never resolve.
    """
    if not isinstance(model, str):
        return None
    if model in ROUTES:
        return model
    return UPSTREAM_ALIAS.get(model)


def _merge(rows: dict, row: dict) -> None:
    """One row per id (one lane COMPLETION): earliest timestamp, largest token counts (a re-scanned unchanged file
    re-observes the same completion rows, so the max merge is pure dedupe; a grown file adds new result uuids)."""
    old = rows.get(row["id"])
    if old is None:
        rows[row["id"]] = dict(row)
        return
    for key in ("in", "cin", "out"):
        old[key] = max(old[key], row[key])
    old_t, new_t = _parse_ts(old.get("ts")), _parse_ts(row.get("ts"))
    if new_t and (old_t is None or new_t < old_t):
        old["ts"] = row["ts"]


def read_ledger(path: Path):
    """(rows by id, newest scan watermark epoch or None, exists). Tolerates torn, foreign and pre-v2 lines."""
    rows: dict = {}
    watermark = None
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return rows, None, False
    for raw in text.splitlines():
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("v") != SCHEMA:
            continue
        if isinstance(data.get("scan_at"), (int, float)):
            watermark = max(watermark or 0.0, float(data["scan_at"]))
            continue
        if isinstance(data.get("id"), str) and isinstance(data.get("route"), str):
            _merge(rows, {"id": data["id"], "ts": data.get("ts"), "route": data["route"],
                          "in": _int(data.get("in")), "cin": _int(data.get("cin")), "out": _int(data.get("out"))})
    return rows, watermark, True


def _launch_alias(jsonl_path: str):
    """The lane manifest's own record of the alias it was launched on (first LAUNCH line of <stem>.log)."""
    try:
        with open(jsonl_path[: -len(".jsonl")] + ".log", "rb") as fh:
            head = fh.read(LOG_HEAD_BYTES)
    except OSError:
        return None
    match = _LAUNCH_ALIAS.search(head)
    return match.group(1).decode("utf-8", "replace") if match else None


def scan_lane(manifest, data: bytes):
    """Usage facts for one lane transcript; `manifest` is the lane's LAUNCH model_alias (or None).

    Returns {"route", "verified": {alias: [in, cin, out]}, "results": [{uuid, alias, delta}], "unverified": [alias],
    "unattributed": [model names], "foreign_usage": bool}. `route` is the lane's REGISTERED Sarvam route from its
    manifest or init event only -- bare upstream ids never make a lane Sarvam, because other providers sell the same
    upstream models. `verified`/`results` come only from `result` events once the lane has a registered route: each
    result is bound to its invocation by its own uuid and counts its `model_usage_since_last_result` delta exactly
    once, so continuations sum instead of collapsing into one per-field maximum. `unverified` is a registered route
    whose requests were seen but never reported usage. `unattributed` is a lane whose manifest/init alias is `sarvam-*`
    yet unregistered, or whose registered route produced only unregistered upstream ids. `foreign_usage` flags a
    foreign lane (no registered route) whose result named a registered alias anyway: attribution refused there, so
    that traffic is loudly reported, never silently counted.
    """
    route = manifest if isinstance(manifest, str) and manifest in ROUTES else None  # bare upstream ids never route
    declared = manifest
    verified: dict = {}
    results: list = []
    seen: set = set()  # ids of assistant messages on a registered model (routed lanes only)
    others: set = set()  # other non-synthetic upstream ids seen (routed lanes only)
    foreign_usage = False
    last_assistant_id = None  # invocation basis for cumulative result rows without a delta field
    for line in data.split(b"\n"):
        is_init = b'"subtype":"init"' in line
        is_result = b'"type":"result"' in line
        is_assistant = (route is not None or declared is not None) and b'"type":"assistant"' in line
        if not (is_init or is_result or is_assistant):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            model = event.get("model")
            if route is None and isinstance(model, str) and model in ROUTES:
                route = model  # a registered alias from the lane's own init; upstream ids never reach here
            if declared is None and isinstance(model, str):
                declared = model
        elif kind == "result":
            usage = event.get("model_usage_since_last_result")
            if isinstance(usage, dict) and usage:
                deltas = usage
                cumulative = False
            elif isinstance(event.get("modelUsage"), dict):
                # Older/edge completions without the per-result delta: only trustworthy as a per-invocation cumulative.
                deltas = event["modelUsage"]
                cumulative = True
            else:
                deltas = {}
                cumulative = False
            for key, usage_row in deltas.items():
                alias = resolve_route(key)
                if alias is None or not isinstance(usage_row, dict):
                    continue
                if route is None:
                    foreign_usage = True  # a registered alias reported by a lane with no registered Sarvam route
                    continue
                got = [_int(usage_row.get("inputTokens")) + _int(usage_row.get("cacheCreationInputTokens")),
                       _int(usage_row.get("cacheReadInputTokens")), _int(usage_row.get("outputTokens"))]
                uuid = event.get("uuid")
                if not isinstance(uuid, str):
                    uuid = f"idx{event.get('result_index')}"
                if cumulative:
                    # A cumulative row without a uuid of its own is per-invocation: the invocation boundary is the
                    # last-seen assistant message id, so successive results with the same basis keep only the max
                    # (the cumulative can only grow), while a new basis starts a fresh row.
                    basis = last_assistant_id
                    row_id = f"{uuid}:{basis}"
                    cur = verified.setdefault(alias, [0, 0, 0])
                    for i, value in enumerate(got):
                        cur[i] = max(cur[i], value)
                    results.append({"uuid": row_id, "alias": alias, "delta": list(got)})
                    continue
                results.append({"uuid": f"{uuid}", "alias": alias, "delta": list(got)})
                cur = verified.setdefault(alias, [0, 0, 0])
                for i, value in enumerate(got):
                    cur[i] += value
        elif kind == "assistant" and isinstance(event.get("message"), dict):
            message = event["message"]
            model = message.get("model")
            alias = resolve_route(model)
            if alias is not None:
                if route is not None and isinstance(message.get("id"), str):
                    seen.add((alias, message["id"]))
                    last_assistant_id = message["id"]
            elif isinstance(model, str) and model and not model.startswith("<"):
                others.add(model)
    unverified = sorted({alias for alias, _ in seen if alias not in verified})
    unattributed = []
    if route is None and isinstance(declared, str) and declared.startswith(ALIAS_PREFIX):
        unattributed.append(declared)  # a sarvam-* alias nobody registered
    elif route is not None and route not in verified and not any(a == route for a, _ in seen) and others:
        unattributed.extend(sorted(others))  # routed, but every upstream id it printed is unregistered
    return {"route": route, "verified": verified, "results": results, "unverified": unverified,
            "unattributed": unattributed, "foreign_usage": foreign_usage}


def scan_transcripts(root: Path, since_epoch: float, deadline: float):
    """Rows for verified Sarvam lane usage in transcripts modified at/after since_epoch.

    Returns (rows by id, info): info has error (str|None), partial (bool), files (int), unverified (int lanes),
    unattributed (int lanes), unattributed_models (sorted list), unverified_routes and foreign_usage (int lanes).
    Each row is one lane COMPLETION (lane-stem:route:result-uuid, summed via the lane's verified totals), so
    continuations count once each and a re-scan of an unchanged lane dedupes to the same rows. A file is only parsed
    when its manifest or its bytes name a registered route, so scanning GBs of unrelated lane logs stays a
    memchr-speed pass; a foreign lane with no registered route is still counted in foreign_usage when its bytes
    name a registered alias, so refused attribution stays visible instead of silently reading as zero.
    """
    rows: dict = {}
    info = {"error": None, "partial": False, "files": 0, "unverified": 0, "unattributed": 0,
            "unattributed_models": [], "unverified_routes": [], "foreign_usage": 0}
    models: set = set()
    routes: set = set()
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
            stat = entry.stat()
            if stat.st_mtime < since_epoch:
                continue
            with open(entry.path, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        info["files"] += 1
        manifest = _launch_alias(entry.path)
        routed = isinstance(manifest, str) and (manifest in ROUTES or manifest.startswith(ALIAS_PREFIX))
        if not (routed or any(mark in data for mark in MARKS)):
            continue  # neither the manifest nor the transcript names a Sarvam route: skip without parsing
        lane = scan_lane(manifest, data)
        if lane["foreign_usage"]:
            info["foreign_usage"] += 1
        if lane["unattributed"]:
            info["unattributed"] += 1
            models.update(lane["unattributed"])
        if lane["route"] is None:
            continue  # foreign lane: registered-alias bytes are not Sarvam usage, whatever ids its events print
        stem = entry.name[:-len(".jsonl")]
        stamp = datetime.fromtimestamp(stat.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for res in lane["results"]:
            tokens_in, tokens_cached, tokens_out = res["delta"]
            _merge(rows, {"id": f"{stem}:{res['alias']}:{res['uuid']}", "ts": stamp, "route": res["alias"],
                          "in": tokens_in, "cin": tokens_cached, "out": tokens_out})
        if lane["unverified"]:
            info["unverified"] += 1
            routes.update(lane["unverified"])
    info["unattributed_models"] = sorted(models)
    info["unverified_routes"] = sorted(routes)
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
        return None, NO_ALLOWANCE
    if not _NUM.fullmatch(raw) or float(raw) <= 0:
        return None, f"allowance invalid in {source}"
    return float(raw), ""


def lane_cost(row: dict):
    """Rupees for one lane row, or None when its route is unregistered or unpriced."""
    route = ROUTES.get(row["route"])
    if route is None or route["price"] is None:
        return None
    rate_in, rate_cached, rate_out = route["price"]
    return (row["in"] * rate_in + row["cin"] * rate_cached + row["out"] * rate_out) / 1_000_000


def summarize(rows: dict, now: datetime, window_h: float) -> dict:
    month = _month_key(now)
    cutoff = now - timedelta(hours=window_h)
    used = window_cost = 0.0
    priced = 0
    unpriced: dict = {}
    tokens = {"in": 0, "cin": 0, "out": 0}
    for row in rows.values():
        moment = _parse_ts(row.get("ts"))
        if moment is None or _month_key(moment) != month:
            continue
        cost = lane_cost(row)
        if cost is None:
            unpriced[row["route"]] = unpriced.get(row["route"], 0) + 1
            continue
        used += cost
        priced += 1
        for key in tokens:
            tokens[key] += row[key]
        if cutoff <= moment <= now:
            window_cost += cost
    return {"month": month, "used_inr": used, "window_inr": window_cost, "priced_lanes": priced,
            "unpriced": unpriced, "tokens": tokens}


def _inr(value: float) -> str:
    return f"₹{value:,.2f}"


def _lanes(n: int) -> str:
    return f"{n} lane" + ("" if n == 1 else "s")


def gap_notes(summary: dict, scan: dict) -> list:
    """One plain note per way the measured estimate is incomplete."""
    notes = []
    if scan.get("unverified"):
        routes = ", ".join(scan.get("unverified_routes") or [])
        notes.append(f"{_lanes(scan['unverified'])} on {routes} without verified usage yet (in flight or ended "
                     "without a result) not counted")
    if scan.get("unattributed"):
        names = ", ".join(scan.get("unattributed_models") or [])
        notes.append(f"{_lanes(scan['unattributed'])} on unregistered route/upstream id ({names}) not attributable")
    if scan.get("foreign_usage"):
        notes.append(f"{_lanes(scan['foreign_usage'])} with no registered Sarvam route named a registered alias in "
                     "its usage; refused (not Sarvam traffic, e.g. the same upstream model via another provider)")
    if summary["unpriced"]:
        notes.append("unpriced (provisioned throughput or unregistered rate): "
                     + ", ".join(f"{route} x{_lanes(n)}" for route, n in sorted(summary["unpriced"].items())))
    return notes


def format_line(summary: dict, scan: dict, allowance, allowance_reason: str, window_h: float, burn_reason: str,
                partial: bool, files: int, have_history: bool) -> str:
    priced = summary["priced_lanes"]
    gaps = gap_notes(summary, scan)
    used = summary["used_inr"]
    if priced:
        measured = f"{_inr(used)} this month (ex-GST, {_lanes(priced)} with verified usage)"
        available = True
    elif gaps:
        measured = "UNAVAILABLE (no verified priced usage: " + "; ".join(gaps) + ")"
        available = False
    elif files == 0 and not have_history:
        measured = "UNAVAILABLE (no lane transcripts in range and no ledger; nothing to measure)"
        available = False
    else:
        measured = f"{_inr(0.0)} this month (ex-GST; no Sarvam lane usage found in {files} transcripts scanned)"
        available = True
    if not available:
        burn = "n/a (no verified usage)"
    elif burn_reason:
        burn = f"n/a ({burn_reason})"
    else:
        burn = f"{_inr(summary['window_inr'] / window_h)}/h ({window_h:g}h avg)"
    if allowance is None:
        balance = f"provider balance UNAVAILABLE ({allowance_reason})"
    elif not available:
        balance = f"balance vs operator allowance {_inr(allowance)} UNAVAILABLE (measured estimate unavailable)"
    else:
        balance = (f"balance vs operator allowance {_inr(max(allowance - used, 0.0))} of {_inr(allowance)} "
                   "(allowance minus measured estimate; not a provider reading)")
    notes = ["lane result usage x Sarvam published rates incl. cached-input"]
    if allowance is None and allowance_reason == NO_ALLOWANCE:
        notes.append(f"Sarvam publishes no balance API; set {ALLOWANCE_KEY} to compare against an allowance")
    if available:
        notes.extend(gaps)
    if partial:
        notes.append("partial scan (deadline)")
    return f"Sarvam TF: measured estimate {measured}, burn {burn}, {balance} -- " + "; ".join(notes)


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
                "nothing to measure)")
        return {"line": line, "available": False, "recorded": 0}
    new_rows = 0
    for row in fresh.values():
        before = rows.get(row["id"])
        _merge(rows, row)
        after = rows[row["id"]]
        if before is None or any(before[k] != after[k] for k in ("in", "cin", "out")):
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
    elif info["unverified"]:
        burn_reason = "in-flight lanes report usage when they end"
    line = format_line(summary, info, allowance, allowance_reason, window_h, burn_reason, info["partial"],
                       info["files"], bool(rows))
    recorded = 0
    if record and not info["error"] and not info["partial"]:
        batch = []
        for row in fresh.values():
            kept = rows[row["id"]]
            batch.append(json.dumps({"v": SCHEMA, **{k: kept[k] for k in ("id", "ts", "route", "in", "cin", "out")}},
                                    separators=(",", ":")))
        batch.append(json.dumps({"v": SCHEMA, "scan_at": scan_started}, separators=(",", ":")))
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with open(ledger, "a") as fh:
            fh.write("\n".join(batch) + "\n")
        recorded = len(fresh)
    return {"line": line, "available": summary["priced_lanes"] > 0, "recorded": recorded, "new_rows": new_rows,
            "summary": summary, "allowance": allowance, "files_scanned": info["files"],
            "unverified_lanes": info["unverified"], "unattributed_lanes": info["unattributed"],
            "foreign_usage_lanes": info["foreign_usage"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--record", action="store_true",
                        help="append new per-completion rows + scan watermark to the ledger")
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
