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
transcript. Each `result` event carries `model_usage_since_last_result`, the exact usage of that completion, and the
lane's totals are the SUM of those per-completion deltas (one row id per result uuid, merged by max, so a re-scan of an
unchanged file can only re-observe the same rows). A result with no per-completion delta carries only the CUMULATIVE
`modelUsage` of its session. Those snapshots are ONE counter keyed by the result's own `session_id` (the single trusted
invocation key): each completion is the growth since what that session already counted, so snapshots 10 and 20 cost 20,
never 30. A counter that goes backwards, or a snapshot with no session key, means the invocation boundary is unknown:
that usage is UNAVAILABLE (excluded and reported), never summed and never maxed. A lane that has not written a result
yet (in flight, or killed first) has no verified usage; it is reported as such and never costed.

Time comes from the completion itself. A `result` event has no clock of its own, so a completion is placed at its own
`timestamp` when the record carries one, else at the timestamp of the newest assistant/user event of the same
`session_id` written before it (the completion's last message). The transcript mtime is never used for placement: it is
the end time of the LAST completion and would drag every earlier one into the newest window and month. A completion
with no trusted time keeps its tokens but is marked untimed: month-to-date and, while it could still be inside the
window, the 3h burn are UNAVAILABLE with that reason, never estimated.

Counters are verified, never coerced: an entry needs all four counters as non-negative integers. A missing, malformed
or empty usage object is excluded and reported (unverified), never turned into a priced zero row.

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
carry "v": 3; anything else in an older ledger is ignored and the month is rescanned (v2 rows were stamped with the
transcript mtime and could double-count cumulative snapshots, so they are never reused). A row whose time could not be
recovered stores ts null plus "seen", the transcript's last write, which only rules it out of earlier windows/months.
Without --record the script is
read-only (the dry-run path). It never reads the Sarvam key, never posts to Slack and never touches the pm-ops DB.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
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
SCHEMA = 3
ALIAS_PREFIX = "sarvam-"
LOG_HEAD_BYTES = 65536
COUNTERS = ("inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens", "outputTokens")

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


def _counters(entry):
    """[input + cache writes, cached reads, output] when ALL four counters are present as non-negative integers, else
    None. A missing, malformed or empty usage object is unverified; it must never be coerced into a priced zero row."""
    if not isinstance(entry, dict):
        return None
    values = []
    for key in COUNTERS:
        value = entry.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        values.append(value)
    tokens_in, tokens_read, cache_write, tokens_out = values
    return [tokens_in + cache_write, tokens_read, tokens_out]


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    """One row per id (one lane COMPLETION): earliest known timestamp, largest token counts (a re-scanned unchanged
    file re-observes the same completion rows, so the max merge is pure dedupe; a grown file adds new result uuids).
    A row with no trusted time carries `seen` (the transcript's last write), an upper bound only."""
    old = rows.get(row["id"])
    if old is None:
        rows[row["id"]] = dict(row)
        return
    for key in ("in", "cin", "out"):
        old[key] = max(old[key], row[key])
    old_t, new_t = _parse_ts(old.get("ts")), _parse_ts(row.get("ts"))
    if new_t and (old_t is None or new_t < old_t):
        old["ts"] = row["ts"]
    old_s, new_s = _parse_ts(old.get("seen")), _parse_ts(row.get("seen"))
    if new_s and (old_s is None or new_s > old_s):
        old["seen"] = row["seen"]


def read_ledger(path: Path):
    """(rows by id, newest scan watermark epoch or None, exists). Tolerates torn, foreign and pre-v3 lines."""
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
            row = {"id": data["id"], "ts": data.get("ts"), "route": data["route"],
                   "in": _int(data.get("in")), "cin": _int(data.get("cin")), "out": _int(data.get("out"))}
            if isinstance(data.get("seen"), str):
                row["seen"] = data["seen"]
            _merge(rows, row)
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

    Returns {"route", "verified": {alias: [in, cin, out]}, "results": [{uuid, alias, delta, ts}], "unverified": [alias],
    "malformed": [alias], "unbounded": [alias], "unattributed": [model names], "foreign_usage": bool}. `route` is the
    lane's REGISTERED Sarvam route from its manifest or init event only -- bare upstream ids never make a lane Sarvam,
    because other providers sell the same upstream models. `verified`/`results` come only from `result` events once the
    lane has a registered route: each result is bound to its invocation by its own uuid and counts its
    `model_usage_since_last_result` delta exactly once, so continuations sum instead of collapsing into one per-field
    maximum. A result with only cumulative `modelUsage` is differenced against what its (session_id, route) chain has
    already counted; with no session_id, or a counter that goes backwards, the boundary is unknown and the usage is
    reported in `unbounded` (never summed). Each result row carries `ts`, the completion's own time (see the module
    docstring), or None when it cannot be recovered. `malformed` is a registered route whose result usage had missing,
    non-integer, negative or empty counters (excluded, never a zero row). `unverified` is a registered route whose
    requests were seen but never reported usage. `unattributed` is a lane whose manifest/init alias is `sarvam-*` yet
    unregistered, or whose registered route produced only unregistered upstream ids. `foreign_usage` flags a foreign
    lane (no registered route) whose result named a registered alias anyway: attribution refused there, so that
    traffic is loudly reported, never silently counted.
    """
    route = manifest if isinstance(manifest, str) and manifest in ROUTES else None  # bare upstream ids never route
    declared = manifest
    verified: dict = {}
    results: list = []
    malformed: set = set()
    unbounded: set = set()
    seen: set = set()  # ids of assistant messages on a registered model (routed lanes only)
    others: set = set()  # other non-synthetic upstream ids seen (routed lanes only)
    foreign_usage = False
    stamps: dict = {}  # session_id -> newest unconsumed assistant/user event time
    chains: dict = {}  # (session_id, alias) -> [in, cin, out] already counted for that invocation
    broken: set = set()  # chains whose cumulative boundary turned out to be unknown
    done: set = set()  # (uuid, alias) already counted in this file
    ordinal = 0  # position of the result event in the file: a stable id when a result carries no uuid
    for line in data.split(b"\n"):
        is_init = b'"subtype":"init"' in line
        is_result = b'"type":"result"' in line
        is_message = route is not None and (
            b'"type":"assistant"' in line or (b'"type":"user"' in line and b'"timestamp"' in line))
        if not (is_init or is_result or is_message):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind in ("assistant", "user"):
            moment, sid = _parse_ts(event.get("timestamp")), event.get("session_id")
            if moment is not None and isinstance(sid, str):
                stamps[sid] = moment
        if kind == "system" and event.get("subtype") == "init":
            model = event.get("model")
            if route is None and isinstance(model, str) and model in ROUTES:
                route = model  # a registered alias from the lane's own init; upstream ids never reach here
            if declared is None and isinstance(model, str):
                declared = model
        elif kind == "result":
            ordinal += 1
            sid = event.get("session_id")
            sid = sid if isinstance(sid, str) and sid else None
            # The completion's own time: a timestamp on the result record, else the newest message of the same session
            # written before it, consumed so a later result can never borrow it. Never the transcript mtime.
            moment = _parse_ts(event.get("timestamp"))
            if moment is None and sid is not None:
                moment = stamps.pop(sid, None)
            stamp = _iso(moment) if moment is not None else None
            usage = event.get("model_usage_since_last_result")
            if isinstance(usage, dict) and usage:
                entries, cumulative = usage, False
            elif isinstance(event.get("modelUsage"), dict) and event["modelUsage"]:
                # No per-completion delta: only trustworthy as a snapshot of its session's running cumulative.
                entries, cumulative = event["modelUsage"], True
            else:
                entries, cumulative = {}, False
                if route is not None:
                    malformed.add(route)  # a completion that reported no usage at all is unverified, not zero
            uuid = event.get("uuid")
            if not isinstance(uuid, str) or not uuid:
                uuid = f"ord{ordinal}"
            for key, usage_row in entries.items():
                alias = resolve_route(key)
                if alias is None:
                    continue
                if route is None:
                    foreign_usage = True  # a registered alias reported by a lane with no registered Sarvam route
                    continue
                got = _counters(usage_row)
                if got is None:
                    malformed.add(alias)
                    continue
                if (uuid, alias) in done:
                    continue  # the same completion written twice
                done.add((uuid, alias))
                chain = (sid, alias)
                if cumulative:
                    if sid is None or chain in broken:
                        unbounded.add(alias)
                        continue
                    delta = [g - c for g, c in zip(got, chains.get(chain, [0, 0, 0]))]
                    if any(d < 0 for d in delta):
                        broken.add(chain)  # the counter went backwards: a reset or a foreign counter, boundary unknown
                        unbounded.add(alias)
                        continue
                    chains[chain] = list(got)
                    verified.setdefault(alias, [0, 0, 0])
                    if not any(delta):
                        continue  # a repeat of a snapshot already counted
                else:
                    delta = got
                    if sid is not None:
                        base = chains.get(chain, [0, 0, 0])
                        chains[chain] = [b + g for b, g in zip(base, got)]
                results.append({"uuid": uuid, "alias": alias, "delta": list(delta), "ts": stamp})
                cur = verified.setdefault(alias, [0, 0, 0])
                for i, value in enumerate(delta):
                    cur[i] += value
        elif kind == "assistant" and isinstance(event.get("message"), dict):
            message = event["message"]
            model = message.get("model")
            alias = resolve_route(model)
            if alias is not None:
                if route is not None and isinstance(message.get("id"), str):
                    seen.add((alias, message["id"]))
            elif isinstance(model, str) and model and not model.startswith("<"):
                others.add(model)
    unverified = sorted({alias for alias, _ in seen if alias not in verified})
    unattributed = []
    if route is None and isinstance(declared, str) and declared.startswith(ALIAS_PREFIX):
        unattributed.append(declared)  # a sarvam-* alias nobody registered
    elif route is not None and route not in verified and not any(a == route for a, _ in seen) and others:
        unattributed.extend(sorted(others))  # routed, but every upstream id it printed is unregistered
    return {"route": route, "verified": verified, "results": results, "unverified": unverified,
            "malformed": sorted(malformed), "unbounded": sorted(unbounded), "unattributed": unattributed,
            "foreign_usage": foreign_usage}


def scan_transcripts(root: Path, since_epoch: float, deadline: float):
    """Rows for verified Sarvam lane usage in transcripts modified at/after since_epoch.

    Returns (rows by id, info): info has error (str|None), partial (bool), files (int), unverified (int lanes),
    unattributed (int lanes), unattributed_models (sorted list), unverified_routes, malformed / unbounded (int lanes,
    with their *_routes) and foreign_usage (int lanes).
    Each row is one lane COMPLETION (lane-stem:route:result-uuid) stamped with that completion's own time, so
    continuations count once each, land in their true window and month, and a re-scan of an unchanged lane dedupes to
    the same rows. A completion whose time cannot be recovered keeps ts None plus `seen` (the file's last write). A file is only parsed
    when its manifest or its bytes name a registered route, so scanning GBs of unrelated lane logs stays a
    memchr-speed pass; a foreign lane with no registered route is still counted in foreign_usage when its bytes
    name a registered alias, so refused attribution stays visible instead of silently reading as zero.
    """
    rows: dict = {}
    info = {"error": None, "partial": False, "files": 0, "unverified": 0, "unattributed": 0,
            "unattributed_models": [], "unverified_routes": [], "malformed": 0, "malformed_routes": [],
            "unbounded": 0, "unbounded_routes": [], "foreign_usage": 0}
    models: set = set()
    routes: set = set()
    bad_routes: set = set()
    open_routes: set = set()
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
        seen_at = _iso(datetime.fromtimestamp(stat.st_mtime, timezone.utc))  # an upper bound only, never a placement
        for res in lane["results"]:
            tokens_in, tokens_cached, tokens_out = res["delta"]
            row = {"id": f"{stem}:{res['alias']}:{res['uuid']}", "ts": res["ts"], "route": res["alias"],
                   "in": tokens_in, "cin": tokens_cached, "out": tokens_out}
            if res["ts"] is None:
                row["seen"] = seen_at
            _merge(rows, row)
        if lane["unverified"]:
            info["unverified"] += 1
            routes.update(lane["unverified"])
        if lane["malformed"]:
            info["malformed"] += 1
            bad_routes.update(lane["malformed"])
        if lane["unbounded"]:
            info["unbounded"] += 1
            open_routes.update(lane["unbounded"])
    info["unattributed_models"] = sorted(models)
    info["unverified_routes"] = sorted(routes)
    info["malformed_routes"] = sorted(bad_routes)
    info["unbounded_routes"] = sorted(open_routes)
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
    """Month-to-date and trailing-window cost from rows placed by their own completion time. A priced row with no
    trusted time (ts None) is never placed by guesswork: unless its last-write bound rules it out of this month, it
    makes month-to-date UNAVAILABLE (`untimed`, by route), and the trailing-window burn too when its last write is
    inside that window (`untimed_recent`), instead of being summed or dropped."""
    month = _month_key(now)
    cutoff = now - timedelta(hours=window_h)
    used = window_cost = 0.0
    priced = 0
    unpriced: dict = {}
    untimed: dict = {}
    untimed_recent = 0
    tokens = {"in": 0, "cin": 0, "out": 0}
    for row in rows.values():
        moment = _parse_ts(row.get("ts"))
        if moment is None:
            bound = _parse_ts(row.get("seen"))
            if bound is not None and _month_key(bound) != month:
                continue  # last written outside this month: the completion cannot belong to it
            if lane_cost(row) is not None:
                untimed[row["route"]] = untimed.get(row["route"], 0) + 1
                if bound is None or bound >= cutoff:
                    untimed_recent += 1  # it could still be inside the burn window
                continue
        elif _month_key(moment) != month:
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
            "unpriced": unpriced, "untimed": untimed, "untimed_recent": untimed_recent, "tokens": tokens}


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
    if scan.get("malformed"):
        routes = ", ".join(scan.get("malformed_routes") or [])
        notes.append(f"{_lanes(scan['malformed'])} on {routes} reported empty or malformed usage counters; excluded "
                     "as unverified (not counted as zero)")
    if scan.get("unbounded"):
        routes = ", ".join(scan.get("unbounded_routes") or [])
        notes.append(f"{_lanes(scan['unbounded'])} on {routes} reported only cumulative usage with an unknown "
                     "invocation boundary (no session id, or the counter went backwards); UNAVAILABLE, never summed")
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
    untimed = summary.get("untimed") or {}
    if untimed:
        count = sum(untimed.values())
        where = ", ".join(sorted(untimed))
        measured = (f"UNAVAILABLE ({count} completion{'' if count == 1 else 's'} on {where} with no recoverable "
                    "per-completion timestamp; month-to-date and burn need each completion's own time)")
        available = False
    elif priced:
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
    if summary.get("untimed_recent"):
        burn = "n/a (completion times unrecoverable)"
    elif not (available or untimed):
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
    if available or untimed:
        notes.extend(gaps)
    if partial:
        notes.append("partial scan (deadline)")
    return f"Sarvam TF: measured estimate {measured}, burn {burn}, {balance} -- " + "; ".join(notes)


def reconcile_ledger(path: Path, now: datetime) -> dict:
    """Back up the ledger, then rewrite it keeping only current-schema lines plus ONE tombstone naming what was dropped.

    Older rows were stamped with the transcript mtime and could double-count cumulative snapshots; the reader already
    ignores them, and dropping their lines (and the old watermark) makes the next scan rebuild the month from the
    transcripts with the corrected rules. Nothing is lost: the untouched original is kept beside it as `.bak-<utc>`.
    """
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return {"reconciled": False, "reason": "no ledger", "dropped": 0}
    keep, dropped = [], 0
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("v") == SCHEMA:
            keep.append(raw)
        else:
            dropped += 1
    if not dropped:
        return {"reconciled": False, "reason": "already current", "dropped": 0}
    backup = path.with_name(f"{path.name}.bak-{now.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    shutil.copy2(path, backup)
    tombstone = json.dumps({"v": SCHEMA, "tombstone": {"at": _iso(now), "dropped_lines": dropped,
                                                       "backup": backup.name}}, separators=(",", ":"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(keep + [tombstone]) + "\n")
    os.replace(tmp, path)
    return {"reconciled": True, "dropped": dropped, "kept": len(keep), "backup": str(backup)}


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
    elif info["malformed"] or info["unbounded"]:
        burn_reason = "some lane usage is unavailable"
    line = format_line(summary, info, allowance, allowance_reason, window_h, burn_reason, info["partial"],
                       info["files"], bool(rows))
    recorded = 0
    if record and not info["error"] and not info["partial"]:
        batch = []
        for row in fresh.values():
            kept = rows[row["id"]]
            keys = ("id", "ts", "route", "in", "cin", "out") + (("seen",) if kept.get("seen") else ())
            batch.append(json.dumps({"v": SCHEMA, **{k: kept[k] for k in keys}}, separators=(",", ":")))
        batch.append(json.dumps({"v": SCHEMA, "scan_at": scan_started}, separators=(",", ":")))
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with open(ledger, "a") as fh:
            fh.write("\n".join(batch) + "\n")
        recorded = len(fresh)
    return {"line": line, "available": summary["priced_lanes"] > 0 and not summary["untimed"], "recorded": recorded,
            "new_rows": new_rows, "summary": summary, "allowance": allowance, "files_scanned": info["files"],
            "unverified_lanes": info["unverified"], "unattributed_lanes": info["unattributed"],
            "malformed_lanes": info["malformed"], "unbounded_lanes": info["unbounded"],
            "foreign_usage_lanes": info["foreign_usage"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--record", action="store_true",
                        help="append new per-completion rows + scan watermark to the ledger")
    parser.add_argument("--json", action="store_true", help="print the full summary as JSON instead of the line")
    parser.add_argument("--reconcile-ledger", action="store_true",
                        help="back up the ledger and drop pre-current-schema lines (tombstoned), then exit")
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
    if args.reconcile_ledger:
        print(json.dumps(reconcile_ledger(Path(args.ledger), now)))
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
