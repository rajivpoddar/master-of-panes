#!/usr/bin/env python3
"""Support-inbox snapshot for the 3h heartbeat.

Sources:
  * Slack #heydonna-feedback (C0A56RX6FNW) and #heydonna-pm (C0AGWPQFKHA) via
    conversations.history + conversations.replies with the PM bot token
    (SLACK_BOT_TOKEN from the environment or .env.local; never printed).
  * In-app `feedback` table on Convex prod via `npx convex data feedback --prod`
    (PM-only prod read, rule 11c). Skipped with --no-in-app.

Each Slack thread is classified:
  answered     last human message is from the team and not from the requester
  needs_reply  last human message is from the requester (customer)
  linked       additionally set when the thread references an issue/PR number
Bot/alert roots are excluded. Output: JSON on stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

CHANNELS = {"C0A56RX6FNW": "#heydonna-feedback", "C0AGWPQFKHA": "#heydonna-pm"}
TEAM = {
    "U0ALEAYCAUT": "PM",
    "U0BNFGX2UAX": "CTO",
    "UEQTTB97A": "Rajiv",
    "U09L1CTGP9T": "Abi",
}
IST = timezone(timedelta(hours=5, minutes=30))
LINK_RE = re.compile(r"(?<![\w/])#(\d{3,5})\b|github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/(\d+)")
# A requester's short closing message after a team reply does not reopen a thread.
ACK_RE = re.compile(
    r"^\W*(ok(ay)?|thanks?|thank you|thx|great|perfect|got it|yes|cool|sounds good|"
    r"(that|it) worked|will do|awesome|noted)\b",
    re.IGNORECASE,
)
MAIN_CHECKOUT = Path.home() / "Downloads/projects/heydonna-app"
OPEN_STATE = Path.home() / ".claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/support-inbox-open.json"


def load_env(name: str) -> str | None:
    if os.environ.get(name):
        return os.environ[name]
    for base in (Path(__file__).resolve().parents[3], MAIN_CHECKOUT):
        env = base / ".env.local"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def needs_reply_threshold_minutes(now: datetime) -> int:
    """60 min inside IST business hours (Mon-Sat 09:00-20:00), else 180."""
    local = now.astimezone(IST)
    business = local.weekday() < 6 and 9 <= local.hour < 20
    return 60 if business else 180


def is_team(msg: dict) -> bool:
    return msg.get("user") in TEAM


def is_human(msg: dict) -> bool:
    """A human message: team member (bot-posted PM counts) or a non-bot user."""
    if is_team(msg):
        return True
    return not msg.get("bot_id") and msg.get("subtype") in (None, "thread_broadcast") and bool(msg.get("user"))


def classify_thread(messages: list[dict], now_ts: float) -> dict | None:
    """Pure: classify one thread (root first). None when the root is internal."""
    if not messages:
        return None
    root = messages[0]
    requester = root.get("user")
    if not is_human(root) or (requester in TEAM and requester != "U09L1CTGP9T"):
        # Team-started threads (except Abi, who relays Scribie users) are not support asks.
        return None
    humans = [m for m in messages if is_human(m)]
    # Drop trailing requester acknowledgements ("Great! It worked") that follow a team reply.
    effective = list(humans)
    while (
        len(effective) > 1
        and effective[-1].get("user") == requester
        and len(effective[-1].get("text") or "") <= 80
        and ACK_RE.match(effective[-1].get("text") or "")
        and any(is_team(m) and m.get("user") != requester for m in effective[:-1])
    ):
        effective.pop()
    last = effective[-1]
    answered = last.get("user") != requester and is_team(last)
    text = " ".join(m.get("text") or "" for m in messages)
    links = sorted({int(a or b) for a, b in LINK_RE.findall(text)})
    last_requester_ts = max(float(m["ts"]) for m in effective if m.get("user") == requester)
    return {
        "ts": root["ts"],
        "requester": requester,
        "state": "answered" if answered else "needs_reply",
        "answered_by": TEAM.get(last.get("user")) if answered else None,
        "answered_at": float(last["ts"]) if answered else None,
        "age_min": int((now_ts - last_requester_ts) / 60),
        "links": links,
        "summary": re.sub(r"<@[A-Z0-9]+>|\s+", " ", root.get("text") or "").strip()[:70],
        "last_activity": float(messages[-1]["ts"]),
    }


def select_items(threads: list[dict], window_start: float) -> list[dict]:
    """Pure: keep threads active in the window, plus every open needs_reply
    (a reopened answered thread is needs_reply, so it is always kept)."""
    return [t for t in threads if t["last_activity"] >= window_start or t["state"] == "needs_reply"]


def slack_get(token: str, method: str, params: dict) -> dict:
    url = f"https://slack.com/api/{method}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    for attempt in range(3):
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.load(resp)
        if body.get("ok"):
            return body
        if body.get("error") == "ratelimited" and attempt < 2:
            time.sleep(5)
            continue
        raise RuntimeError(f"slack {method}: {body.get('error')}")
    raise RuntimeError(f"slack {method}: ratelimited")


def fetch_thread(token: str, channel: str, ts: str, now_ts: float) -> dict | None:
    replies = slack_get(token, "conversations.replies", {"channel": channel, "ts": ts, "limit": 200})
    item = classify_thread(replies.get("messages", []), now_ts)
    if item:
        item["channel"] = CHANNELS.get(channel, channel)
    return item


def fetch_channel(token: str, channel: str, oldest: float, now_ts: float) -> list[dict]:
    hist = slack_get(token, "conversations.history", {"channel": channel, "oldest": f"{oldest:.6f}", "limit": 200})
    out = []
    for root in hist.get("messages", []):
        messages = [root]
        if root.get("reply_count"):
            replies = slack_get(token, "conversations.replies", {"channel": channel, "ts": root["ts"], "limit": 200})
            messages = replies.get("messages", [root])
        item = classify_thread(messages, now_ts)
        if item:
            item["channel"] = CHANNELS.get(channel, channel)
            out.append(item)
    return out


TRACK_DAYS = 14  # stop re-fetching a tracked root after this long without activity


def load_open_state(path: Path = OPEN_STATE) -> dict[str, dict[str, float]]:
    """{channel: {root_ts: last_seen_reply_ts}} for ALL tracked roots (answered too).
    Legacy {channel: [root_ts]} files load with last_seen 0."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, dict[str, float]] = {}
    if not isinstance(data, dict):
        return {}
    for ch, v in data.items():
        if isinstance(v, list):
            out[str(ch)] = {str(t): 0.0 for t in v}
        elif isinstance(v, dict):
            out[str(ch)] = {str(t): float(ls or 0) for t, ls in v.items()}
    return out


def save_open_state(open_map: dict, path: Path = OPEN_STATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(open_map, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def carry_forward(threads: list[dict], prev_tracked, fetch_thread) -> list[dict]:
    """Re-fetch every previously tracked root (answered or not) that fell out of the
    lookback, so a new customer reply on an answered thread flips it back to needs_reply
    (classify_thread decides from the full reply list)."""
    seen = {t["ts"] for t in threads}
    out = list(threads)
    for ts in (prev_tracked or {}):
        if ts in seen:
            continue
        item = fetch_thread(ts)
        if item:
            out.append(item)
    return out


def tracked_roots(threads: list[dict], now_ts: float) -> dict[str, float]:
    """All roots to keep tracking with their last-seen reply ts (pruned after TRACK_DAYS idle)."""
    horizon = now_ts - TRACK_DAYS * 86400
    return {t["ts"]: t["last_activity"] for t in sorted(threads, key=lambda t: t["ts"])
            if t["state"] == "needs_reply" or t["last_activity"] >= horizon}


def open_roots(threads: list[dict]) -> list[str]:
    return sorted(t["ts"] for t in threads if t["state"] == "needs_reply")


def classify_feedback_rows(rows: list[dict], window_start_ms: int) -> list[dict]:
    """Pure: in-app feedback rows created in the window."""
    out = []
    for row in rows:
        if int(row.get("createdAt") or 0) < window_start_ms:
            continue
        text = f"{row.get('title', '')} {row.get('resolutionNote') or ''}"
        links = sorted({int(a or b) for a, b in LINK_RE.findall(text)})
        out.append({
            "id": row.get("_id"),
            "type": row.get("type"),
            "severity": row.get("severity"),
            "auto": bool(row.get("autoSubmitted")),
            "status": row.get("status"),
            "title": (row.get("title") or "")[:70],
            "links": links,
            "state": "linked" if links else ("answered" if row.get("status") in ("resolved", "wont_fix") else "needs_triage"),
        })
    return out


def fetch_in_app(window_start_ms: int, limit: int = 50) -> list[dict]:
    proc = subprocess.run(
        ["npx", "convex", "data", "feedback", "--prod", "--limit", str(limit), "--order", "desc", "--format", "jsonl"],
        capture_output=True, text=True, timeout=120, cwd=str(MAIN_CHECKOUT),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"convex data feedback failed: {proc.stderr.strip()[-200:]}")
    rows = [json.loads(line) for line in proc.stdout.splitlines() if line.strip().startswith("{")]
    return classify_feedback_rows(rows, window_start_ms)


def main() -> int:
    parser = argparse.ArgumentParser(description="Support inbox snapshot (JSON)")
    parser.add_argument("--hours", type=float, default=3.0)
    parser.add_argument("--lookback-hours", type=float, default=24.0, help="how far back to find still-open threads")
    parser.add_argument("--no-in-app", action="store_true")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()
    window_start = now_ts - args.hours * 3600
    result: dict = {"threshold_min": needs_reply_threshold_minutes(now), "slack": [], "in_app": [], "failures": []}
    token = load_env("SLACK_BOT_TOKEN")
    if not token:
        result["failures"].append("SLACK_BOT_TOKEN missing")
    else:
        prev_state = load_open_state()
        new_state = dict(prev_state)
        for channel in CHANNELS:
            try:
                threads = fetch_channel(token, channel, now_ts - args.lookback_hours * 3600, now_ts)
                threads = carry_forward(
                    threads, prev_state.get(channel, []),
                    lambda ts, ch=channel: fetch_thread(token, ch, ts, now_ts),
                )
                new_state[channel] = tracked_roots(threads, now_ts)
                result["slack"].extend(select_items(threads, window_start))
            except Exception as exc:  # noqa: BLE001
                # Keep the previous open set for this channel so nothing is dropped.
                result["failures"].append(f"{CHANNELS[channel]}: {exc}")
        try:
            save_open_state(new_state)
        except OSError as exc:
            result["failures"].append(f"open-state save: {exc}")
    if not args.no_in_app:
        try:
            result["in_app"] = fetch_in_app(int(window_start * 1000))
        except Exception as exc:  # noqa: BLE001
            result["failures"].append(f"in-app feedback: {exc}")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
