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
import unicodedata
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
# The PM bot ("Dhurva PM") posts as user U0ALEAYCAUT and also carries this bot_id. Real thread
# replies have both, but a bot_message-shaped post can carry only bot_id, so match either.
TEAM_BOT_IDS = {"B0ALH9R1LRK": "PM"}
IST = timezone(timedelta(hours=5, minutes=30))
LINK_RE = re.compile(r"(?<![\w/])#(\d{3,5})\b|github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/(\d+)")
# A requester's short closing message after a team reply does not reopen a thread.
# A bare acknowledgement must match the COMPLETE (trimmed, mention-stripped) message -- a
# continuation like "Thanks, but it still does not work" is not an acknowledgement and must
# not be dropped (Codex #7 P1).
_ACK_TERM = (
    r"(?:ok(?:ay)?|thanks?|thank you|thx|great|perfect|got it|yes|cool|sounds good|"
    r"(?:that|it) worked|will do|awesome|noted)"
)
# One or more ack terms ("ok thanks!"); is_closing_ack caps combinations at ACK_MAX_WORDS.
ACK_RE = re.compile(rf"^\W*{_ACK_TERM}(?:[\s,!.]+{_ACK_TERM})*[!.\s]*$", re.IGNORECASE)
ACK_MAX_WORDS = 4
# Emoji-only acknowledgements (":+1:", ":pray:", Unicode emoji). Negative/question emoji keep
# the thread open (2026-10-31: C0A56RX6FNW/1793267174.396729 flagged OVERDUE on a bare ":+1:").
SHORTCODE_RE = re.compile(r":([a-z0-9_+\-']+):", re.IGNORECASE)
NEGATIVE_SHORTCODES = {
    "-1", "thumbsdown", "x", "negative_squared_cross_mark", "disappointed", "cry", "sob",
    "confused", "worried", "rage", "angry", "warning", "question", "grey_question",
    "exclamation", "grey_exclamation", "thinking_face", "no_entry", "no_entry_sign",
    "face_with_raised_eyebrow", "slightly_frowning_face", "white_frowning_face",
}
NEGATIVE_EMOJI_NAME_RE = re.compile(
    r"THUMBS DOWN|CROSS MARK|QUESTION|EXCLAMATION|CRYING|DISAPPOINTED|CONFUSED|WORRIED|ANGRY|"
    r"POUTING|WARNING|THINKING|FROWNING|NO ENTRY",
)
# Real closings are rarely bare: "I turned it on and it worked. thank you!" (thanks at the end) or
# "<@PM> Was able to create both projects." (answers the PM's follow-up question). A short
# message with a thanks/resolved cue and NO question, contrast, negation or new-request cue is a
# closing; anything else stays needs_reply (the safe side: a missed closing is only noise).
MENTION_RE = re.compile(r"<[@!][^>]*>")
CLOSING_CUE_RE = re.compile(
    r"\b(thanks?|thank you|thx|cheers|appreciate\w*|worked|went through|fixed|resolved|solved|sorted|"
    r"all set|(?:was|were) able to|that did it|all good|(?:working|fine|good) now|will reach out if|found it)\b",
    re.IGNORECASE,
)
SELF_RESOLUTION_RE = re.compile(
    r"\b(found it|worked|went through|fixed|resolved|solved|sorted|all set|"
    r"(?:was|were) able to|that did it|all good|(?:working|fine|good) now)\b",
    re.IGNORECASE,
)
REOPEN_CUE_RE = re.compile(
    r"\?|n['\u2019]t\b|\b(but|however|although|though|still|not|never|nothing|cannot|unable|fail\w*|"
    r"error|broken|wrong|issue|problem|stuck|yet|also|another|please|pls|need|want)\b|\b(can|could|would) you\b",
    re.IGNORECASE,
)
CLOSING_MAX_CHARS = 100
MAIN_CHECKOUT = Path.home() / "Downloads/projects/heydonna-app"
OPEN_STATE = Path.home() / ".claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/support-inbox-open.json"

# Total wall budget for the whole collector. The heartbeat composer blocks on this
# process, so exceeding it stalls the entire report (observed: two missed heartbeats).
# Override with SUPPORT_INBOX_BUDGET_S.
SUPPORT_BUDGET_S = float(os.environ.get("SUPPORT_INBOX_BUDGET_S") or 45.0)
# Cap how many previously-tracked roots one run re-checks (newest first). Roots beyond
# the cap keep their prior state and are picked up on a later run, so nothing is dropped.
MAX_CARRY_FORWARD = int(os.environ.get("SUPPORT_MAX_CARRY_FORWARD") or 40)



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


def team_name(msg: dict) -> str | None:
    """Team display name for a message, by Slack user id or by the PM bot's bot_id."""
    return TEAM.get(msg.get("user")) or TEAM_BOT_IDS.get(msg.get("bot_id"))


def is_team(msg: dict) -> bool:
    return team_name(msg) is not None


def is_emoji_only(text: str) -> bool:
    """Pure: True when text is only positive/neutral emoji (shortcodes or Unicode) and whitespace."""
    for code in SHORTCODE_RE.findall(text):
        if code.lower() in NEGATIVE_SHORTCODES:
            return False
    rest = SHORTCODE_RE.sub("", text).strip()
    if not rest and SHORTCODE_RE.search(text):
        return True
    if not rest:
        return False
    for ch in rest:
        if ch.isspace() or ord(ch) in (0x200D, 0xFE0F) or 0x1F3FB <= ord(ch) <= 0x1F3FF:
            continue
        if unicodedata.category(ch) != "So":
            return False
        if NEGATIVE_EMOJI_NAME_RE.search(unicodedata.name(ch, "")):
            return False
    return True


def is_closing_ack(text: str) -> bool:
    """Pure: True when a requester message only closes the thread (thanks / it worked / done)."""
    body = " ".join(MENTION_RE.sub(" ", text or "").split())
    if not body or len(body) > CLOSING_MAX_CHARS:
        return False
    if is_emoji_only(body):
        return True
    if ACK_RE.match(body) and len(body.split()) <= ACK_MAX_WORDS:
        return True
    return not REOPEN_CUE_RE.search(body) and bool(CLOSING_CUE_RE.search(body))


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
    if not is_human(root) or (is_team(root) and requester != "U09L1CTGP9T"):
        # Team-started threads (except Abi, who relays Scribie users) are not support asks.
        return None
    humans = [m for m in messages if is_human(m)]
    # Drop trailing requester acknowledgements ("Great! It worked") that follow a team reply.
    # A requester self-resolving with no team reply ("Thanks. Found it") is also closed.
    effective = list(humans)
    self_closed_ts = None
    while (
        len(effective) > 1
        and effective[-1].get("user") == requester
        and is_closing_ack(effective[-1].get("text") or "")
    ):
        # Gratitude alone cannot resolve an unanswered ask or a reopened issue.
        body = " ".join(MENTION_RE.sub(" ", effective[-1].get("text") or "").split())
        if SELF_RESOLUTION_RE.search(body) and not REOPEN_CUE_RE.search(body):
            self_closed_ts = self_closed_ts or float(effective[-1]["ts"])
        effective.pop()
    last = effective[-1]
    team_answered = last.get("user") != requester and is_team(last)
    answered = team_answered or self_closed_ts is not None
    text = " ".join(m.get("text") or "" for m in messages)
    links = sorted({int(a or b) for a, b in LINK_RE.findall(text)})
    last_requester_ts = max(float(m["ts"]) for m in effective if m.get("user") == requester)
    return {
        "ts": root["ts"],
        "requester": requester,
        "state": "answered" if answered else "needs_reply",
        "answered_by": team_name(last) if team_answered else None,
        "answered_at": float(last["ts"]) if team_answered else self_closed_ts,
        "age_min": int((now_ts - last_requester_ts) / 60),
        "links": links,
        "summary": re.sub(r"<@[A-Z0-9]+>|\s+", " ", root.get("text") or "").strip()[:70],
        "last_activity": float(messages[-1]["ts"]),
        "last_requester_ts": last_requester_ts,
    }


def select_items(threads: list[dict], window_start: float) -> list[dict]:
    """Pure: keep threads active in the window, plus every still-unanswered needs_reply.
    A needs_reply whose latest customer message predates the window is flagged overdue=True
    (rendered "OVERDUE"). Answered threads (latest message ours) never surface outside the window."""
    out = []
    for t in threads:
        if t["state"] == "needs_reply":
            t = dict(t, overdue=t.get("last_requester_ts", t["last_activity"]) < window_start)
            out.append(t)
        elif t["last_activity"] >= window_start:
            out.append(t)
    return out


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


def parent_thread_ts(msg: dict) -> str | None:
    """Pure: parent root ts when msg is a thread reply (incl. a reply broadcast to the channel)."""
    t = msg.get("thread_ts")
    return t if t and t != msg.get("ts") else None


def fetch_thread(token: str, channel: str, ts: str, now_ts: float) -> dict | None:
    replies = slack_get(token, "conversations.replies", {"channel": channel, "ts": ts, "limit": 200})
    parent = parent_thread_ts(replies.get("messages", [{}])[0])
    if parent:
        replies = slack_get(token, "conversations.replies", {"channel": channel, "ts": parent, "limit": 200})
    item = classify_thread(replies.get("messages", []), now_ts)
    if item:
        item["channel"] = CHANNELS.get(channel, channel)
    return item


def fetch_channel(token: str, channel: str, oldest: float, now_ts: float) -> list[dict]:
    hist = slack_get(token, "conversations.history", {"channel": channel, "oldest": f"{oldest:.6f}", "limit": 200})
    out = []
    seen_roots: set[str] = set()
    for root in hist.get("messages", []):
        # A reply broadcast to the channel shows up in history as its own message; judge the
        # whole thread via its parent, never the reply alone (it would look unanswered).
        parent = parent_thread_ts(root)
        if parent:
            if parent in seen_roots:
                continue
            seen_roots.add(parent)
            root = slack_get(token, "conversations.replies", {"channel": channel, "ts": parent, "limit": 1})["messages"][0]
        seen_roots.add(root["ts"])
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


def carry_forward(threads: list[dict], prev_tracked, fetch_thread, *, deadline: float | None = None) -> list[dict]:
    """Re-fetch every previously tracked root (answered or not) that fell out of the
    lookback, so a new customer reply on an answered thread flips it back to needs_reply
    (classify_thread decides from the full reply list).

    BOUNDED (#support-inbox-hang): tracked roots accumulate forever by design, so this
    loop grew to ~196 sequential Slack calls and made the collector exceed the heartbeat's
    wall budget, stalling the whole report. Two bounds now apply and BOTH are required:
      - `deadline` (monotonic) stops the loop once the collector's budget is spent; roots
        not reached keep their prior state and are retried next run (no data is dropped).
      - `MAX_CARRY_FORWARD` caps how many roots a single run re-checks, newest first, so a
        large backlog is drained progressively instead of in one unbounded pass.
    Newest-first matters: a recent reply is the one most likely to need an answer.
    """
    seen = {t["ts"] for t in threads}
    out = list(threads)
    pending = [ts for ts in (prev_tracked or {}) if ts not in seen]
    # Slack ts values are decimal strings, so a float sort orders them chronologically.
    pending.sort(key=lambda s: float(s) if _is_ts(s) else 0.0, reverse=True)
    for ts in pending[:MAX_CARRY_FORWARD]:
        if deadline is not None and time.monotonic() >= deadline:
            break
        item = fetch_thread(ts)
        if item:
            out.append(item)
    return out


def _is_ts(value) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False



def tracked_roots(threads: list[dict], now_ts: float) -> dict[str, float]:
    """All roots to keep tracking with their last-seen reply ts.

    `conversations.history` only searches the latest 24h by default, so an answered
    root is the ONLY way a later customer reply on it gets rediscovered. Retain answered
    roots indefinitely (never prune by TRACK_DAYS) -- pruning them silently drops any
    reply that lands after the cutoff (Codex #7 P1).
    """
    return {t["ts"]: t["last_activity"] for t in sorted(threads, key=lambda t: t["ts"])}


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
        capture_output=True, text=True, errors="replace", timeout=120, cwd=str(MAIN_CHECKOUT),
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
    deadline = time.monotonic() + SUPPORT_BUDGET_S
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
                    deadline=deadline,
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
