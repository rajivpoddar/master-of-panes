#!/usr/bin/env python3
"""Compose the 3h heartbeat and the 1h hourly post (docs/pm/heartbeat-redesign-2026-09-29.md).

3h (--mode 3h): Axiom errors + support inbox + open PRs. First line ALWAYS
@-mentions CTO (Rajiv 2026-09-29 10:58 IST, thread 1790659381.537339:
"build it. note that it has to have an cto at mention.").

1h (--mode 1h): "Hourly — HH:MM IST", Axiom errors + open PRs. Mentions CTO
ONLY when the post carries a merge ask, an admission ask or a ladder
escalation (Rajiv 11:02 IST same thread).

Both: compact blocks, no tables, "quiet" for empty operational blocks, housekeeping
collapsed to one `_Ops:_` footer line only when something failed. Decisions pending
on Rajiv use the read-only ledger (up to 6 recent rows plus overflow/stale counts),
with one blank spacer before the section. The 1h post also reports inactive agents.

The composer never posts to Slack. It prints the message; the calling skill posts.
Inputs are either collected live (default) or given as JSON files (--*-json)
for dry runs and tests.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

CTO = "<@U0BNFGX2UAX>"
IST = timezone(timedelta(hours=5, minutes=30))
SCRIPTS = Path(__file__).resolve().parent
AXIOM_REPORT = SCRIPTS / "heartbeat-axiom.py"  # MoP-side helper (moved from heydonna-app)
MAX_CODES = 5
MAX_SUPPORT = 5
MAX_PRS = 8


def _num(value) -> str:
    if value is None:
        return "?"
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def error_block(axiom: dict | None, mapping: dict, window_label: str) -> tuple[list[str], str, int]:
    """Return (lines, tldr_fragment, new_code_count)."""
    if axiom is None:
        return [f"*Axiom errors ({window_label}):* unavailable"], "Axiom unavailable", 0
    head = f"*Axiom errors ({window_label}):* {axiom.get('events', 0)} errors / {axiom.get('users', 0)} users"
    compare = []
    if "prior_events" in axiom:
        compare.append(f"prior {axiom['prior_events']}")
    if axiom.get("same_slot_median_events") is not None:
        compare.append(f"7-day same-slot median {_num(axiom['same_slot_median_events'])}")
    if compare:
        head += f" ({', '.join(compare)})"
    if not axiom.get("events"):
        return [head + " — quiet"], "errors quiet", 0
    lines = [head]
    new = 0
    for row in axiom.get("top", [])[:MAX_CODES]:
        m = mapping.get(row["code"], {})
        if m.get("status") == "NEW":
            new += 1
            target = "NEW → launch customer-artifact-investigator"
        elif m.get("refs"):
            target = "→ " + ", ".join(m["refs"][:2])
        else:
            target = ""
        lines.append(f"• {row['code']} — {row['events']} ev / {row['users']} users {target}".rstrip())
    if axiom.get("other_codes"):
        lines.append(f"• +{axiom['other_codes']} other codes, {axiom['other_events']} events")
    tldr = f"{axiom['events']} errors / {axiom['users']} users"
    if new:
        tldr += f" ({new} NEW code{'s' if new > 1 else ''})"
    return lines, tldr, new


def support_block(support: dict | None, window_label: str) -> tuple[list[str], str]:
    if support is None:
        return [f"*Support ({window_label}):* unavailable"], "support unavailable"
    threshold = support.get("threshold_min", 60)
    slack = support.get("slack", [])
    in_app = support.get("in_app", [])
    if not slack and not in_app:
        return [f"*Support ({window_label}):* quiet"], "support quiet"
    needs = [t for t in slack if t["state"] == "needs_reply"]
    answered = [t for t in slack if t["state"] == "answered"]
    requester_resolved = any(t.get("answered_by") is None for t in answered)
    lines = [f"*Support ({window_label}):* {len(slack)} Slack thread(s), {len(in_app)} in-app"]
    for t in sorted(needs, key=lambda t: -t["age_min"])[:MAX_SUPPORT]:
        flag = " ⚠" if t["age_min"] >= threshold else ""
        link = f" — linked #{t['links'][0]}" if t.get("links") else ""
        lines.append(f"• {t['channel']} — \"{t['summary'][:50]}\" — {'OVERDUE' if t.get('overdue') else 'NEEDS REPLY'} ({t['age_min']}m){link}{flag}")
    if len(answered) > 3:
        lines.append(f"• {len(answered)} {'answered/resolved' if requester_resolved else 'answered'}")
    else:
        for t in answered:
            at = datetime.fromtimestamp(t["answered_at"], IST).strftime("%H:%M") if t.get("answered_at") else ""
            link = f" — linked #{t['links'][0]}" if t.get("links") else ""
            attribution = f"answered by {t['answered_by']}" if t.get("answered_by") else "resolved by requester"
            lines.append(f"• {t['channel']} — \"{t['summary'][:50]}\" — {attribution} {at}{link}")
    for f in in_app[:3]:
        state = f"linked #{f['links'][0]}" if f.get("links") else f["state"].replace("_", " ")
        auto = " (auto)" if f.get("auto") else ""
        lines.append(f"• In-app {f['type']}{auto} — \"{f['title'][:50]}\" — {state}")
    if len(in_app) > 3:
        lines.append(f"• +{len(in_app) - 3} more in-app")
    overdue = [t for t in needs if t["age_min"] >= threshold]
    tldr = f"{len(needs)} support thread(s) need a reply" if needs else "support answered"
    if not needs and requester_resolved:
        tldr = "support answered/resolved" if any(t.get("answered_by") for t in answered) else "support resolved"
    if overdue:
        tldr += f" ({len(overdue)} overdue, oldest {max(t['age_min'] for t in overdue)}m)"
    return lines, tldr


MAX_USERS = 6


def active_users_block(active: dict | None, window_label: str) -> tuple[list[str], str]:
    """One header line + up to MAX_USERS compact user lines (keeps the post <= ~22 lines)."""
    if active is None:
        return [f"*Active users ({window_label}):* unavailable"], "active users unavailable"
    count = active.get("count", 0)
    head = f"*Active users ({window_label}):* {count}"
    if active.get("prior_count") is not None:
        head += f" (prior {active['prior_count']})"
    if active.get("internal"):
        head += f" +{active['internal']} internal"
    users = active.get("users", [])
    if not users:
        return [head + " — quiet"], f"{count} active users"
    shown = [f"{u['user']} ({u['files']}f)" for u in users[:MAX_USERS * 2]]
    lines = [head]
    for i in range(0, min(len(shown), MAX_USERS * 2), 3):
        lines.append("• " + ", ".join(shown[i:i + 3]))
    if len(users) > MAX_USERS * 2:
        lines.append(f"• +{len(users) - MAX_USERS * 2} more")
    return lines, f"{count} active users"


IDLE_SLOT_MIN = 20  # slots must not stay idle > 20 min (20-buddhi-pm.md rule 23)
MOP_SLOTS_URL = "http://127.0.0.1:3100/slots"
LANE_SPLIT_MIN = 240  # lanes over 4h get a split check (20-buddhi-pm.md 17 "Thin slices")


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # MoP naive stamps are UTC


SLOT_STATE = Path(os.environ.get(
    "HEARTBEAT_SLOT_STATE",
    str(Path.home() / ".claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/heartbeat-slot-epochs.json"),
))


def _fmt_dur(minutes: int) -> str:
    return f"{minutes // 60}h {minutes % 60}m" if minutes >= 60 else f"{minutes}m"


def lane_started(s: dict) -> datetime | None:
    """When the slot took its current issue/PR lane: MoP assigned_at, else the
    events-history fallback stamped by _fetch_slots (lane_started_at)."""
    return _parse_ts(s.get("assigned_at")) or _parse_ts(s.get("lane_started_at"))


def lane_start_from_events(events: list[dict], issue) -> str | None:
    """Earliest assign-projection timestamp of the newest contiguous run on `issue`
    (events newest-first). Fallback for rows assigned before MoP stamped assigned_at."""
    start = None
    for e in events:
        try:
            got = json.loads(e.get("payload") or "{}").get("issue_projection", {}).get("issue")
        except (ValueError, AttributeError):
            continue
        if got != issue:
            break
        start = e.get("timestamp")
    return start


def slot_transitions(slots_doc: dict | None, now: datetime, prev: dict | None) -> tuple[dict, dict]:
    """Pure: return (change_time_by_slot, new_state). A slot's change time is when its
    (assignment_epoch, occupied) pair was first seen different from the previous run;
    this is the release time used for free-slot idle."""
    prev = prev or {}
    state: dict = {}
    changed: dict = {}
    slots = (slots_doc or {}).get("slots", []) if isinstance(slots_doc, dict) else (slots_doc or [])
    for s in slots:
        key = str(s.get("slot"))
        sig = [s.get("assignment_epoch"), bool(s.get("occupied"))]
        old = prev.get(key) or {}
        since = old.get("since") if old.get("sig") == sig else now.isoformat()
        state[key] = {"sig": sig, "since": since}
        changed[key] = since
    return changed, state


def slot_provisioned(n) -> bool:
    """A dev slot is provisioned once its checkout exists (S7/S8 come later)."""
    try:
        return os.path.isdir(f"/Users/rajiv/Downloads/projects/heydonna-app-300{int(n)}")
    except (TypeError, ValueError):
        return True


def slot_block(slots_doc: dict | None, now: datetime, release_seen: dict | None = None) -> tuple[list[str], str, list[str]]:
    """Return (lines, tldr, pm_actions). Rajiv 2026-09-29 13:23 IST:
    "add the slot status to this hourly report as well. idle slots have to be checked."
    """
    if slots_doc is None:
        return ["*Slots:* MoP unreachable"], "slots unknown", []
    slots = slots_doc.get("slots", []) if isinstance(slots_doc, dict) else slots_doc
    cells, actions = [], []
    for s in sorted(slots, key=lambda s: s.get("slot", 0)):
        n = s.get("slot")
        label = f"S{n} {s.get('name') or '?'}"
        if not s.get("occupied") and not slot_provisioned(n):
            # Configured but not provisioned yet (no checkout): never an idle action.
            cells.append(f"{label}: not provisioned")
            continue
        work = ""
        if s.get("occupied"):
            work = f"#{s['issue']}" if s.get("issue") else "#?"
            if s.get("pr"):
                work += f"/PR#{s['pr']}"
            started = lane_started(s)
            if started:
                lane_min = max(0, int((now - started).total_seconds() // 60))
                work = f"on {work} for {_fmt_dur(lane_min)}"
                if lane_min >= LANE_SPLIT_MIN:
                    work += " ⏱"
                    actions.append(f"split-check S{n} {work}")
        working = s.get("occupied") and s.get("active_turn_state") == "active" and not s.get("idle")
        if s.get("occupied"):
            last = _parse_ts(s.get("last_meaningful_work_at")) or _parse_ts(s.get("last_activity"))
        else:
            # Free slot: idle since it was released, never since pre-release work.
            marks = [_parse_ts(s.get("last_activity")),
                     _parse_ts((release_seen or {}).get(str(s.get("slot"))))]
            marks = [m for m in marks if m]
            last = max(marks) if marks else None
        idle_min = int((now - last).total_seconds() // 60) if last else None
        if working:
            state = "working"
        elif not s.get("occupied"):
            state = f"free {idle_min}m" if idle_min is not None else "free"
        else:
            state = f"idle {idle_min}m" if idle_min is not None else "idle"
        flagged = not working and idle_min is not None and idle_min >= IDLE_SLOT_MIN
        if flagged:
            state += " ⚠"
            actions.append(f"idle-slot S{n} {('#' + str(s['issue'])) if s.get('occupied') and s.get('issue') else 'free'} idle {idle_min}m")
        cells.append(f"{label}: {work + ' ' if work else ''}{state}")
    lines = [f"*Slots ({len(cells)}):*"]
    for i in range(0, len(cells), 2):
        lines.append("• " + " | ".join(cells[i:i + 2]))
    idle_n = sum(a.startswith("idle-slot") for a in actions)
    tldr = f"{idle_n} idle slot(s) ≥{IDLE_SLOT_MIN}m" if idle_n else "slots busy"
    split_n = len(actions) - idle_n
    if split_n:
        tldr += f", {split_n} lane(s) ≥{LANE_SPLIT_MIN // 60}h"
    return lines, tldr, actions


def pr_block(prs: list[dict] | None, max_prs: int = MAX_PRS) -> tuple[list[str], str, list[str]]:
    """Return (lines, tldr, merge_asks)."""
    if prs is None:
        return ["*Open PRs:* unavailable"], "PRs unavailable", []
    if not prs:
        return ["*Open PRs (0):* quiet"], "no open PRs", []
    lines = [f"*Open PRs ({len(prs)}):*"]
    order = {"red": 0, "CI+E2E green": 1}
    ranked = sorted(prs, key=lambda r: (order.get(r["state"].split(":")[0], 2), -int(r["number"])))
    for r in ranked[:max_prs]:
        lines.append(f"• #{r['number']} {r['head']} — {r['state']} — {r['owner']} — {r['next']}")
    if len(prs) > max_prs:
        lines.append(f"• +{len(prs) - max_prs} more (pr-open-snapshot.py --text)")
    red = [r for r in prs if r["state"].startswith("red")]
    unowned_red = [r for r in red if "unowned" in r["state"]]
    merge = [f"#{r['number']}@{r['head']}" for r in prs if r["next"] == "CTO merge ask"]
    tldr = f"{len(prs)} open PRs"
    if red:
        tldr += f", {len(red)} red" + (f" ({len(unowned_red)} unowned)" if unowned_red else "")
    if merge:
        tldr += f", {len(merge)} merge-ready"
    return lines, tldr, merge


EMAIL_UNKNOWN_SENDER = "(unknown sender)"
_EMAIL_ADDR = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _email_field(value, limit: int) -> str:
    text = re.sub(r"\s+", " ", value if isinstance(value, str) else "").strip()
    text = _EMAIL_ADDR.sub("[address]", text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def email_block(doc: dict | None, limit: int = 8) -> list[str]:
    """Shared-channel email block (CTO REVISE, C0ALZJHGE49 1791527549.296399).
    Consumes ONLY the producer's structured name/subject fields; never parses free text,
    so no snippet or raw address can reach the channel. Absent/malformed input fails open."""
    if not isinstance(doc, dict) or doc.get("status") != "ok" or not isinstance(doc.get("items"), list):
        return ["*Email digest (last 3h):* unavailable"]
    rows = []
    for item in doc["items"][:limit]:
        if not isinstance(item, dict):
            continue
        name = item.get("name") if isinstance(item.get("name"), str) else ""
        name = EMAIL_UNKNOWN_SENDER if (not name.strip() or "@" in name) else _email_field(name, 28)
        rows.append(f"• {name} — {_email_field(item.get('subject'), 70) or '(no subject)'}")
    total = doc.get("total") if isinstance(doc.get("total"), int) else len(rows)
    if not rows:
        return ["*Email digest (last 3h):* nothing unread needs your attention."]
    return [f"*Email digest (last 3h):* {total} unread need your attention", *rows]


PM_PROJECT = Path.home() / ".claude/projects/-Users-rajiv-Downloads-projects-heydonna-app"
PM_OPS_DB = PM_PROJECT / "state/pm-ops.db"
MAX_DECISIONS = 6
MAX_INACTIVE_AGENTS = 4
PM_SESSION_FRESH_S = 15 * 60
# Rajiv 2026-10-23 (C0ALZJHGE49 ts 1792770807.935489): count every subagent of the PM pane's sessions in the last 24h.
# Pane drops TaskStopped/cleared agents without marking them finished: anything unwritten for 3h is assumed gone
# (PM correction 2026-10-23: ListAgents showed 11 vs 32). Completed agents stay listed only 60m.
AGENT_RECENT_S = 3 * 3600
AGENT_COMPLETED_S = 3600


def _compact(value, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = text if len(text) <= limit else text[:limit - 1].rstrip() + "…"
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# Rajiv C0ALZJHGE49/1792637124.844719, ts 1792637256.052779.
def read_decisions(path: Path = PM_OPS_DB, now: datetime | None = None) -> dict | None:
    """Read only open decision-kind / Rajiv-owned rows; never create or repair the ledger."""
    now = now or datetime.now(timezone.utc)
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, kind, target_type, target_id, pr, issue, owner, title, required_action, created_at, updated_at "
                "FROM obligations WHERE status='open' AND "
                "(kind IN ('rajiv_decision','rajiv_decision_pending','decision_pending','product_decision') "
                "OR owner='rajiv') ORDER BY updated_at DESC, id DESC"
            ).fetchall()
    except (OSError, sqlite3.Error):
        return None
    items, stale = [], 0
    for row in rows:
        updated = _parse_ts(row["updated_at"])
        if updated is not None and updated >= now - timedelta(days=7):
            items.append(dict(row))
        else:
            stale += 1  # unknown freshness needs ledger triage too, never a current decision claim
    return {"items": items, "stale": stale}


def decision_block(doc: dict | None, now: datetime) -> list[str]:
    head = "*Decisions pending on Rajiv:*"
    if doc is None:
        return [head + " unavailable"]
    items = doc.get("items", [])
    lines = [head] if items else [head + " None."]
    for row in items[:MAX_DECISIONS]:
        target = (f"PR#{row['pr']}" if row.get("pr") else f"#{row['issue']}" if row.get("issue")
                  else f"{row.get('target_type') or 'row'}:{row.get('target_id') or row['id']}")
        phrase = row.get("title") or row.get("required_action") or row.get("kind") or "Decision needed"
        started = _parse_ts(row.get("created_at")) or _parse_ts(row.get("updated_at"))
        minutes = max(0, int((now - started).total_seconds() // 60)) if started else None
        age = (f"{minutes // 1440}d {minutes % 1440 // 60}h" if minutes is not None and minutes >= 1440
               else _fmt_dur(minutes) if minutes is not None else "unknown")
        route = "held by Rajiv" if row.get("owner") == "rajiv" else "via CTO"
        lines.append(f"• {_compact(target, 60)} — {_compact(phrase, 92)} — age {age} — {route}")
    if len(items) > MAX_DECISIONS:
        lines.append(f"• +{len(items) - MAX_DECISIONS} more")
    if doc.get("stale"):
        lines.append(f"• +{doc['stale']} stale (ledger triage)")
    return lines


# Rajiv DM D0AMF0XE6TS/1792637361.212159, ts 1792638231.158209.
def classify_agent(last_record: dict | None, modified_at: float, now: datetime) -> str:
    """Pure transcript classifier, not a process-liveness or TaskStop decision."""
    if not isinstance(last_record, dict) or not last_record.get("type"):
        return "unknown"
    if last_record.get("type") == "assistant" and (last_record.get("message") or {}).get("stop_reason") == "end_turn":
        return "done"
    idle_s = max(0, now.timestamp() - modified_at)
    if idle_s > AGENT_RECENT_S:
        return "expired"
    return "inactive" if idle_s >= 3600 else "active"


def _last_agent_record(path: Path) -> dict:
    """Read only the final JSONL record, even when it is larger than a read chunk."""
    with path.open("rb") as handle:
        position = handle.seek(0, os.SEEK_END)
        data = b""
        while position:
            size = min(position, 8192)
            position -= size
            handle.seek(position)
            data = (handle.read(size) + data).rstrip(b"\r\n")
            boundary = data.rfind(b"\n")
            if boundary >= 0:
                return json.loads(data[boundary + 1:])
        return json.loads(data)


def _agent_row(path: Path, now: datetime, modified_at: float) -> dict:
    try:
        meta = json.loads(path.with_suffix(".meta.json").read_text())
    except (OSError, ValueError):
        meta = {}
    description = (meta.get("description") or meta.get("agentType") or path.stem) if isinstance(meta, dict) else path.stem
    return {"description": str(description), "idle_min": int((now.timestamp() - modified_at) // 60)}


def _is_listed_agent(path: Path) -> bool:
    """The pane lists only DIRECT agents (nested sub-subagents share the dir with spawnDepth > 1) that
    are not yet collected: meta.finished == true means the result was delivered and the pane dropped it."""
    try:
        meta = json.loads(path.with_suffix(".meta.json").read_text())
    except (OSError, ValueError):
        return True
    if not isinstance(meta, dict):
        return True
    depth = meta.get("spawnDepth", 1)
    if isinstance(depth, (int, float)) and depth > 1:
        return False
    return meta.get("finished") is not True


def pm_process_start(pane: str = "0:0.0") -> float | None:
    """Start time (epoch) of the claude process attached to the PM pane's tty; None when unknown."""
    try:
        tty = subprocess.run(["tmux", "display", "-p", "-t", pane, "#{pane_tty}"],
                             capture_output=True, text=True, errors="replace", timeout=5).stdout.strip()
        if not tty:
            return None
        out = subprocess.run(["ps", "-t", tty.replace("/dev/", ""), "-o", "lstart=,command="],
                             capture_output=True, text=True, errors="replace", timeout=5).stdout
        for row in out.splitlines():
            if "claude" in row.split(maxsplit=5)[-1][:60] and "--model" in row:
                return datetime.strptime(row[:24].strip(), "%a %b %d %H:%M:%S %Y").timestamp()
    except Exception:
        return None
    return None


def _session_started(path: Path) -> float | None:
    """Epoch of a session JSONL's first timestamped record (birthtime fallback)."""
    try:
        with path.open("rb") as handle:
            for _ in range(20):
                line = handle.readline()
                if not line:
                    break
                try:
                    stamp = json.loads(line).get("timestamp")
                except (ValueError, AttributeError):
                    continue
                if isinstance(stamp, str):
                    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        return getattr(path.stat(), "st_birthtime", None)
    except OSError:
        return None


def read_agents(root: Path = PM_PROJECT, now: datetime | None = None, process_start: float | None = None) -> dict | None:
    """Count what the CURRENT PM pane process lists (Rajiv 2026-10-23, ts 1792770807.935489).
    Sessions = top-level JSONLs begun since the PM claude process started (chain across /clear);
    only DIRECT subagents; completed only if finished within the process lifetime.
    running = written <60m, not ended; completed = final end_turn; inactive = not ended, >=60m idle.
    Unknown process start => None (unavailable), never a guess."""
    now = now or datetime.now(timezone.utc)
    if not root.is_dir():
        return None
    if process_start is None:
        process_start = pm_process_start()
    if process_start is None:
        return None
    sessions = []
    for session in root.glob("*.jsonl"):
        try:
            if not session.is_file():
                continue
            started = _session_started(session)
            sessions.append((session.stat().st_mtime, session, started))
        except OSError:
            continue
    if not sessions:
        return None
    if now.timestamp() - max(m for m, _, _ in sessions) > PM_SESSION_FRESH_S:
        return None  # fresh children cannot revive a dead PM pane
    running, completed, inactive, unreadable = 0, [], [], 0
    for _modified, session, started in sessions:
        if started is None or started < process_start:
            continue  # prior claude process / restart
        for path in (session.parent / session.stem / "subagents").glob("agent-*.jsonl"):
            try:
                modified_at = path.stat().st_mtime
                if now.timestamp() - modified_at > AGENT_RECENT_S or not _is_listed_agent(path):
                    continue
                state = classify_agent(_last_agent_record(path), modified_at, now)
                if state == "unknown":
                    unreadable += 1
                elif state == "active":
                    running += 1
                elif state == "done":
                    if modified_at >= process_start and now.timestamp() - modified_at <= AGENT_COMPLETED_S:
                        completed.append(_agent_row(path, now, modified_at))
                elif state == "inactive":
                    inactive.append(_agent_row(path, now, modified_at))
            except FileNotFoundError:
                continue  # transcript retired during collection
            except (OSError, ValueError, TypeError, AttributeError):
                unreadable += 1
    for rows in (completed, inactive):
        rows.sort(key=lambda row: (-row["idle_min"], row["description"]))
    return {"running": running, "completed": completed, "inactive": inactive, "unreadable": unreadable}


def agents_line(doc: dict | None) -> str:
    if doc is None:
        return "*Agents:* unavailable"
    completed = doc.get("completed", [])
    inactive = doc.get("inactive", [])
    running = doc["running"]
    listed = running + len(completed) + len(inactive)
    line = (f"*Agents:* {listed} listed · {running} running · {len(completed)} completed · "
            f"{len(inactive)} inactive — PM stops C+I in Step 3c")
    names = [f"{_compact(r['description'], 64)} (completed, last write {r['idle_min']}m ago)" for r in completed]
    names += [f"{_compact(r['description'], 64)} (inactive {r['idle_min']}m)" for r in inactive]
    if names:
        shown = names[:MAX_INACTIVE_AGENTS]
        if len(names) > MAX_INACTIVE_AGENTS:
            shown.append(f"+{len(names) - MAX_INACTIVE_AGENTS} more")
        line += ": " + ", ".join(shown)
    if doc.get("unreadable"):
        line += f" · {doc['unreadable']} unreadable"
    return line


LANGSMITH_RECONCILE = Path.home() / ".claude/scripts/langsmith-axiom-reconcile.py"
LANGSMITH_UNKNOWN = "LangSmith ingest: UNKNOWN (check failed)"


def langsmith_line(rc: int | None, out: str) -> str:
    """Pure strict renderer. Health is never inferred from a missing match: the producer's
    own status token must agree with the exit code, else UNKNOWN. WARN only for measured
    positive ingest errors; UNCOMPARABLE is an explicit non-healthy observation."""
    import re
    first = (out or "").strip().splitlines()[0] if (out or "").strip() else ""
    m = re.match(r"^LangSmith ingest: (OK|HEALTHY|FAILING|UNCOMPARABLE) \((.*)\)\s*$", first)
    if rc not in (0, 1, 2) or not m:
        return LANGSMITH_UNKNOWN
    status, detail = m.group(1), m.group(2)
    t = re.search(r"PROD [\d.]+h traces (\d+) vs calls (\d+)", detail)
    tc = f"traces {t.group(1)} / calls {t.group(2)}" if t else None
    errs = [(a, int(n)) for a, n in re.findall(r"([\w.\-]+/[\w.\-]+) (\d+) errors", detail)]
    k = sum(n for _, n in errs)
    if rc == 1 and status == "FAILING":
        if k > 0:
            return f"*LangSmith ingest (1h):* WARN — {k} ingest errors (statuses {', '.join(f'{a} {n}' for a, n in errs[:4])})"
        return LANGSMITH_UNKNOWN  # query/auth/partial failure without measured errors
    if k > 0 or tc is None or (t.group(1) == "0" and t.group(2) == "0"):
        return LANGSMITH_UNKNOWN  # conflicting or absent observations
    if rc == 2 and status == "UNCOMPARABLE" and "0 observed prod ingest errors" in detail:
        return (f"*LangSmith ingest (1h):* UNVERIFIED (not healthy) — 0 observed errors; coverage partial/"
                f"emitter rollout unverified; counts incomparable ({tc})")
    if rc == 0 and status in ("OK", "HEALTHY") and "0 observed prod ingest errors" in detail:
        return f"*LangSmith ingest (1h):* OK — 0 observed ingest errors ({tc})"
    return LANGSMITH_UNKNOWN


def collect_langsmith(py: str, timeout: int = 60) -> str:
    """Bounded (60s) reconcile call; any failure => UNKNOWN, never raises."""
    try:
        p = subprocess.run([py, str(LANGSMITH_RECONCILE), "--hours", "1"], capture_output=True, text=True, errors="replace", timeout=timeout)
        return langsmith_line(p.returncode, p.stdout)
    except Exception:
        return LANGSMITH_UNKNOWN


# ---------------------------------------------------------------------------
# Spark training line (Rajiv 2026-10-23, C0ALZJHGE49 thread 1792694694.833149):
# ALWAYS present in the hourly post; probes the Spark directly, read-only.
# ---------------------------------------------------------------------------
_SPARK_REMOTE = (
    'C=$(docker ps --format "{{.Names}}" | grep -iE "train|sindhu" | head -1); '
    'echo "##C $C"; [ -n "$C" ] || exit 0; '
    'L=$(ls -t /home/user/*/native.log /home/user/*/*/native.log /home/user/*/*/*/native.log 2>/dev/null | head -1); '
    '{ docker logs --tail 600 "$C" 2>&1; [ -n "$L" ] && tail -c 200000 "$L"; } | tr "\\r" "\\n" | tail -400'
)


def spark_probe(timeout: int = 8) -> tuple[dict | None, str]:
    """Return (doc|None, reason). doc is {} when no trainer is running."""
    import re as _re
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", "dgx-spark", _SPARK_REMOTE],
            capture_output=True, text=True, errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, "ssh timeout"
    except Exception as exc:
        return None, type(exc).__name__
    if r.returncode != 0 and "##C" not in r.stdout:
        return None, f"ssh rc={r.returncode}"
    out = r.stdout
    m = _re.search(r"^##C (.*)$", out, _re.M)
    name = (m.group(1).strip() if m else "")
    if not name:
        return {}, ""
    doc: dict = {"run": name}
    prog = None
    for mm in _re.finditer(r"(\d+)/(\d+)\s*\[[^\]]*?([\d.]+)s/it", out):
        if prog is None or int(mm[2]) >= int(prog[2]):
            prog = mm
    if prog:
        doc.update(step=int(prog[1]), total=int(prog[2]), spi=float(prog[3]))
    losses = _re.findall(r"'loss': '?([\d.]+)'?", out)
    if losses:
        doc["loss"] = losses[-1]
    mins = _re.findall(r'"min_free_mib": ([\d.]+)', out)
    if mins:
        doc["mem_min"] = int(float(mins[-1]))
    return doc, ""


def monitor_up() -> bool:
    try:
        r = subprocess.run(["pgrep", "-f", "spark-run-monitor\\.sh"], capture_output=True, text=True, errors="replace", timeout=3)
        own = os.getpid()
        pids = [int(x) for x in r.stdout.split() if x.isdigit() and int(x) != own]
        for pid in pids:
            c = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, errors="replace", timeout=3).stdout
            if "spark-run-monitor.sh --run-name" in c or c.strip().startswith(("bash", "/bin/bash", "sh ")) and "spark-run-monitor" in c:
                return True
        return False
    except Exception:
        return False


def training_line(probe=None, mon_up=None) -> tuple[str, list[str]]:
    """Never omitted. Returns (line, actions)."""
    doc, reason = probe if probe is not None else spark_probe()
    if doc is None:
        return f"*Training:* UNAVAILABLE ({reason or 'probe failed'})", []
    if not doc:
        return "*Training:* no run active", []
    up = monitor_up() if mon_up is None else mon_up
    bits = [doc["run"]]
    if doc.get("step") is not None:
        pct = 100.0 * doc["step"] / doc["total"] if doc["total"] else 0
        bits.append(f"{doc['step']}/{doc['total']} ({pct:.0f}%)")
    else:
        bits.append("step n/a")
    bits.append(f"loss {doc.get('loss', 'n/a')}")
    if doc.get("step") is not None and doc.get("spi"):
        h = (doc["total"] - doc["step"]) * doc["spi"] / 3600
        bits.append(f"ETA {h:.0f}h" if h < 48 else f"ETA {h/24:.1f}d")
    else:
        bits.append("ETA n/a")
    bits.append(f"mem min {doc['mem_min']} MiB" if "mem_min" in doc else "mem min n/a")
    bits.append(f"monitor {'up' if up else 'DOWN'}")
    acts = [] if up else [f"spark-monitor DOWN while {doc['run']} active: restart spark-run-monitor.sh"]
    return "*Training:* " + " · ".join(bits), acts


def compose(
    mode: str,
    now: datetime,
    axiom: dict | None,
    mapping: dict,
    prs: list[dict] | None,
    support: dict | None = None,
    failures: list[str] | None = None,
    escalations: list[str] | None = None,
    active: dict | None = None,
    include_active: bool = False,
    slots: dict | None = None,
    include_slots: bool = False,
    release_seen: dict | None = None,
    usage_lines: list[str] | None = None,
    cleanup_alert: str | None = None,
    email_doc: dict | None = None,
    decisions: dict | None = None,
    agents: dict | None = None,
    training: tuple | None = None,
    langsmith: str | None = None,
) -> str:
    """Pure: build the Slack mrkdwn message."""
    window = "3h" if mode == "3h" else "1h"
    stamp = now.astimezone(IST).strftime("%H:%M IST")
    err_lines, err_tldr, _ = error_block(axiom, mapping or {}, window)
    # Slots block costs ~5 lines; trim the PR list so the post stays <= ~24 lines.
    pr_lines, pr_tldr, merge_asks = pr_block(prs, MAX_PRS - 4 if include_slots else MAX_PRS)
    parts = []
    body: list[str] = []
    if include_active:
        au_lines, au_tldr = active_users_block(active, window)
        parts.append(au_tldr)
        body += ["", *au_lines]
    parts.append(err_tldr)
    body += ["", *err_lines]
    if mode == "3h":
        sup_lines, sup_tldr = support_block(support, window)
        parts.append(sup_tldr)
        body += ["", *sup_lines]
    # 3h heartbeat: no open-PR or slots report; Claude + Codex usage always
    # (Rajiv 2026-10-07 12:25 IST, C0ALZJHGE49 thread 1791355967.794539).
    if mode == "3h":
        include_slots = False
        merge_asks = []
        ul = [u for u in (usage_lines or []) if u.strip()]
        body += ["", "*Usage (Claude + Codex):*", *(["• " + u for u in ul] or ["• unavailable (usage-snapshot failed)"])]
        _bad = any("UNAVAILABLE" in u or "unavailable" in u for u in ul) or not any(u.startswith("Claude") or u.startswith("WARN Claude") for u in ul)
        _warn = any(u.startswith("WARN") for u in ul)
        if _warn:
            import re as _re
            _w = next(u for u in ul if u.startswith("WARN"))
            _acct = _re.sub(r"^WARN\s+", "", _w).split(":")[0]
            _m = _re.search(r"ETA 100% in (\S+(?: \S+)?) \(BEFORE reset\)", _w)
            parts.append(f"usage WARN: {_acct} projects 100% in {_m.group(1)} before reset" if _m else f"usage WARN: {_acct} at or above 80% used")
        else:
            parts.append("usage " + ("Claude UNAVAILABLE" if _bad else "ok"))
        # Email digest lives only in the 3h heartbeat; the DM is removed (Rajiv 2026-10-09 12:06 IST,
        # DM D0AMF0XE6TS 1791527801.053979). Shared channel: sender + subject only, previews stripped.
        body += ["", *email_block(email_doc)]
    else:
        parts.append(pr_tldr)
        body += ["", *pr_lines]
    slot_actions: list[str] = []
    if include_slots:
        sl_lines, sl_tldr, slot_actions = slot_block(slots, now, release_seen)
        parts.append(sl_tldr)
        body += ["", *sl_lines]
    if mode == "1h":
        body.append(agents_line(agents))
        if langsmith:
            body.append(langsmith)
        tl, t_acts = training if training is not None else training_line()
        body.append(tl)
        slot_actions = slot_actions + t_acts
    # Exactly one empty spacer before this shared 1h/3h section.
    body += ["", *decision_block(decisions, now)]

    asks = []
    if merge_asks:
        asks.append("merge ask: " + ", ".join(merge_asks))
    for esc in escalations or []:
        asks.append(f"escalation: {esc}")
    tldr = "; ".join(parts) + "."
    if mode == "3h":
        first = f"{CTO} *Heartbeat {stamp}* — TL;DR: {tldr}"
    else:
        mention = f"{CTO} " if asks else ""
        first = f"{mention}*Hourly — {stamp}* — TL;DR: {tldr}"
    lines = [first, *body]
    if asks:
        lines += ["", "*Asks:* " + " | ".join(asks)]
    pm_actions = ([] if mode == "3h" else [a for r in (prs or []) for a in r.get("actions") or []]) + slot_actions
    if pm_actions:
        lines += ["", "*ACTIONS (PM):* " + "; ".join(pm_actions)]
    if cleanup_alert:
        lines.append(f"*ALERT:* {cleanup_alert}")
    if failures:
        lines.append(f"_Ops:_ {len(failures)} failed — " + "; ".join(f[:80] for f in failures[:2]))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Live collection
# ---------------------------------------------------------------------------


def _run_json(cmd: list[str], failures: list[str], label: str, timeout: int = 300):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        failures.append(f"{label}: timeout")
        return None
    if proc.returncode != 0:
        failures.append(f"{label}: rc={proc.returncode} {proc.stderr.strip()[-120:]}")
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        failures.append(f"{label}: invalid JSON")
        return None


def _fetch_slots(url: str = MOP_SLOTS_URL, timeout: int = 5):
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # GET only
            doc = json.loads(resp.read())
    except Exception:
        return None
    slots = doc.get("slots", []) if isinstance(doc, dict) else doc
    base = url.rsplit("/slots", 1)[0]
    for s in slots:
        if not s.get("occupied") or s.get("assigned_at") or not s.get("issue"):
            continue
        try:
            q = f"{base}/events?slot={s['slot']}&type=assignment_effect_projection_completed&limit=50"
            with urllib.request.urlopen(q, timeout=timeout) as resp:
                s["lane_started_at"] = lane_start_from_events(json.loads(resp.read()).get("events", []), s["issue"])
        except Exception:
            pass
    return doc


CLEANUP_STATUS = Path("/tmp/stale-process-cleanup-status.json")


def cleanup_status_alert(path: Path = CLEANUP_STATUS) -> str | None:
    """'stale-process-cleanup REFUSED: <reason>' when the last run refused or errored."""
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or not (doc.get("refused") or doc.get("error")):
        return None
    return f"stale-process-cleanup REFUSED: {doc.get('reason') or 'unknown'}"


def _load(path: str | None):
    return json.loads(Path(path).read_text()) if path else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Compose the 3h/1h heartbeat post (prints, never posts)")
    parser.add_argument("--mode", choices=("3h", "1h"), default="3h")
    parser.add_argument("--axiom-json")
    parser.add_argument("--map-json")
    parser.add_argument("--support-json")
    parser.add_argument("--prs-json")
    parser.add_argument("--active-json")
    parser.add_argument("--no-active-users", action="store_true")
    parser.add_argument("--slots-json")
    parser.add_argument("--decisions-json")
    parser.add_argument("--agents-json")
    parser.add_argument("--no-slots", action="store_true")
    parser.add_argument("--escalation", action="append", default=[], help="ladder-approved escalation line")
    parser.add_argument("--no-in-app", action="store_true")
    parser.add_argument("--save-inputs", help="directory to write collected inputs to")
    args = parser.parse_args()

    failures: list[str] = []
    now = datetime.now(timezone.utc)
    decisions = _load(args.decisions_json) if args.decisions_json else read_decisions(PM_OPS_DB, now)
    agents = None
    if args.mode == "1h":
        agents = _load(args.agents_json) if args.agents_json else read_agents(PM_PROJECT, now)
        if agents is None:
            failures.append("agents: transcripts unavailable")
    if decisions is None:
        failures.append("decisions: ledger unavailable")
    hours = "3" if args.mode == "3h" else "1"
    py = sys.executable
    axiom = _load(args.axiom_json)
    if axiom is None and not args.axiom_json:
        axiom = _run_json(
            [py, str(AXIOM_REPORT), "--hours", hours, "--errors-by-code", "--compare"],
            failures, "axiom",
        )
    active = None
    if not args.no_active_users:
        active = _load(args.active_json)
        if active is None and not args.active_json:
            active = _run_json([py, str(AXIOM_REPORT), "--hours", hours, "--active-users"], failures, "active-users")
    slots = None
    if not args.no_slots:
        slots = _load(args.slots_json)
        if slots is None and not args.slots_json:
            slots = _fetch_slots()  # read-only; None => "MoP unreachable", never a failure
    release_seen = None
    if slots is not None:
        try:
            prev = json.loads(SLOT_STATE.read_text())
        except (OSError, ValueError):
            prev = {}
        release_seen, new_state = slot_transitions(slots, datetime.now(timezone.utc), prev)
        try:
            SLOT_STATE.parent.mkdir(parents=True, exist_ok=True)
            SLOT_STATE.write_text(json.dumps(new_state, indent=2))
        except OSError as exc:
            failures.append(f"slot-state save: {exc}")
    mapping_doc = _load(args.map_json)
    if mapping_doc is None and axiom:
        codes = [a for row in axiom.get("top", []) for a in ("--code", row["code"])]
        mapping_doc = _run_json([py, str(SCRIPTS / "heartbeat-error-map.py"), *codes], failures, "error-map") if codes else {}
    mapping_doc = mapping_doc or {}
    failures += mapping_doc.get("failures", [])
    support = None
    if args.mode == "3h":
        support = _load(args.support_json)
        if support is None and not args.support_json:
            cmd = [py, str(SCRIPTS / "support-inbox-snapshot.py"), "--hours", hours]
            if args.no_in_app:
                cmd.append("--no-in-app")
            support = _run_json(cmd, failures, "support")
        if support:
            failures += support.get("failures", [])
    prs = _load(args.prs_json)
    if prs is None and not args.prs_json:
        prs = _run_json([py, str(SCRIPTS / "pr-open-snapshot.py")], failures, "pr-open-snapshot")
        # Deterministic every-turn open-PR obligations (Rajiv 2026-09-30,
        # thread C0ALZJHGE49/1790707604.948989): piggyback on this same
        # existing hourly/3h producer call, fire-and-forget, never blocks or
        # fails the heartbeat compose itself.
        try:
            subprocess.run(
                [py, str(SCRIPTS / "pr-open-snapshot.py"), "--sync-obligations"],
                capture_output=True, text=True, errors="replace", timeout=60, check=False,
            )
        except Exception:
            pass

    # Idle-slot actions are only rendered into the post's ACTIONS line by `compose()`.
    # The runtime contract requires them in the structured ACTIONS: channel the agent
    # copies into its return value, so compute + persist them here too (Codex #7 P2).
    slot_actions: list[str] = []
    if slots is not None and not args.no_slots:
        _, _, slot_actions = slot_block(slots, datetime.now(timezone.utc), release_seen)
    pm_actions = [a for r in (prs or []) for a in r.get("actions") or []] + slot_actions

    if args.save_inputs:
        out = Path(args.save_inputs)
        out.mkdir(parents=True, exist_ok=True)
        for name, value in (("axiom", axiom), ("map", mapping_doc), ("support", support), ("prs", prs), ("active", active), ("slots", slots), ("actions", pm_actions), ("decisions", decisions), ("agents", agents)):
            (out / f"{name}.json").write_text(json.dumps(value, indent=2))

    usage_lines = None
    if args.mode == "3h":
        try:
            up = subprocess.run([py, str(Path.home() / ".claude/scripts/usage-snapshot.py")], capture_output=True, text=True, errors="replace", timeout=120)
            usage_lines = [l for l in up.stdout.splitlines() if l.strip()]
            if up.returncode != 0 and not usage_lines:
                failures.append(f"usage: rc={up.returncode}")
        except Exception as exc:
            failures.append(f"usage: {exc}")
    email_doc = None
    if args.mode == "3h":
        try:
            ep = subprocess.run([py, str(Path(__file__).with_name("email-digest.py")), "--hours", "3", "--json"], capture_output=True, text=True, errors="replace", timeout=120)
            email_doc = json.loads(ep.stdout)
        except Exception as exc:
            failures.append(f"email: {type(exc).__name__}")
    training = training_line() if args.mode == "1h" else None
    langsmith = collect_langsmith(py) if args.mode == "1h" else None
    print(compose(
        args.mode, datetime.now(timezone.utc), axiom, mapping_doc.get("mapping", {}), prs,
        support=support, failures=failures, escalations=args.escalation,
        active=active, include_active=not args.no_active_users,
        slots=slots, include_slots=not args.no_slots, release_seen=release_seen,
        usage_lines=usage_lines, cleanup_alert=cleanup_status_alert(),
        email_doc=email_doc, decisions=decisions, agents=agents, training=training, langsmith=langsmith,
    ))
    pm_actions += (training[1] if training else [])
    if pm_actions:
        print("ACTIONS:" + json.dumps(pm_actions), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

