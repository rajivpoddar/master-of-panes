#!/usr/bin/env python3
"""Compose the 3h heartbeat and the 1h hourly post (docs/pm/heartbeat-redesign-2026-09-29.md).

3h (--mode 3h): Axiom errors + support inbox + usage. Do not mention the CTO.

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
import hashlib
import math
import json
import importlib.util
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import threading
from itertools import islice
from time import monotonic
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

CTO = "<@U0BNFGX2UAX>"
IST = timezone(timedelta(hours=5, minutes=30))
SCRIPTS = Path(__file__).resolve().parent
AXIOM_REPORT = SCRIPTS / "heartbeat-axiom.py"  # MoP-side helper (moved from heydonna-app)
# Priority inversion and pending GitHub checks complete paginated, live reads; allow one
# bounded 45s extension for those legitimately necessary probes.
COLLECTOR_TIMEOUTS = {
    "axiom": 45, "active-users": 45, "error-map": 90, "support": 120,
    "pr-open-snapshot": 210, "priority-inversion": 90,
    "pending-github": 90, "remote-boxes": 30,
    "usage": 30, "email": 30, "proxies": 5,
}
# Independent hourly checks share one deadline, not the sum of their limits.
COMPOSE_COLLECTOR_BUDGET = 210
HOURLY_CACHE = Path.home() / ".claude/state/hourly-cache"
CACHE_MAX_AGE = 6 * 3600
MAX_CODES = 5
MAX_SUPPORT = 5
MAX_PRS = 8
# Slack rejects a block whose text exceeds 3,000 chars (invalid_blocks); a 3,141-char superseded-saves
# line did exactly that. No output line may exceed MAX_LINE_CHARS. compose() caps lines earlier by
# MAX_LINE_CHARS - LINE_NOTE_RESERVE so that main() can still append a stale-evidence note, then
# main() applies the full cap as the final backstop.
MAX_LINE_CHARS = 2900
LINE_NOTE_RESERVE = 300
MAX_SUPERSEDED_REFS = 12


def cap_line(line: str, limit: int = MAX_LINE_CHARS) -> str:
    """Bound one physical line to `limit` chars: first N items plus "+K more".

    Cuts at a ", " or "; " item boundary (whichever keeps more) so no item is split. A line with no
    usable boundary is hard-truncated with an ellipsis. Never raises; lines within the limit are
    returned unchanged.
    """
    if not isinstance(line, str) or len(line) <= limit:
        return line
    best = None
    for sep in ("; ", ", "):
        items = line.split(sep)
        total = len(items)
        joined = 0  # length of sep.join(items[:kept])
        fit = 0
        for kept in range(1, total):  # at least one item is dropped, so "+K more" is always honest
            joined += len(items[kept - 1]) + (len(sep) if kept > 1 else 0)
            if joined > limit:
                break
            if joined + len(sep) + len(f"+{total - kept} more") <= limit:
                fit = kept
        if fit:
            candidate = f"{sep.join(items[:fit])}{sep}+{total - fit} more"
            if best is None or len(candidate) > len(best):
                best = candidate
    if best is not None:
        return best
    head = line[: max(limit - 1, 0)]
    if head.rfind("<") > head.rfind(">"):  # never leave half a <@U…> mention
        head = head[: head.rfind("<")]
    return head.rstrip() + "…"


def cap_lines(text: str, limit: int = MAX_LINE_CHARS) -> str:
    """cap_line over every physical line of a report; line count and order are unchanged."""
    return "\n".join(cap_line(line, limit) for line in text.split("\n"))


def _num(value) -> str:
    if value is None:
        return "?"
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def three_hour_map_args(axiom: dict) -> list[str]:
    codes = dict.fromkeys(row['code'] for row in axiom.get('top', []) + axiom.get('all_codes', []))
    return [arg for code in codes for arg in ('--code', code)]


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
    sup = axiom.get("superseded_saves")
    sup_line = []
    if sup is not None:
        refs = axiom.get('superseded_save_refs')
        if isinstance(refs, list) and len(refs) == sup:
            if window_label == '3h':
                from collections import Counter
                users = Counter(r['user_hash'] for r in refs if r.get('identity_kind') == 'userIdSha256')
                fallback = Counter((r.get('identity_kind') or 'missing-kind', r.get('user_hash', 'unknown'))
                                   for r in refs if r.get('identity_kind') != 'userIdSha256')
                top_users = sorted(users.items(), key=lambda item: (-item[1], item[0]))[:3]
                details = [f'{user}: {count}' for user, count in top_users]
                details.extend(f'{kind} {identity}: {count}' for (kind, identity), count in
                               sorted(fallback.items(), key=lambda item: (-item[1], item[0]))[:3])
                groups = []
                if users:
                    groups.append(f'{len(users)} distinct users')
                if fallback:
                    counts = Counter()
                    for (kind, _identity), count in fallback.items():
                        counts[kind] += count
                    groups.extend(f'{kind} fallback: {count}' for kind, count in sorted(counts.items()))
                sup_line = [f"superseded saves: {sup} — {', '.join(groups) or 'no identified observations'}; "
                            f"top: {', '.join(details) or 'none'}"]
            else:
                shown = [f"{r['time']} {r.get('identity_kind', 'userSha256')}:{r['user_hash']}"
                         for r in refs[:MAX_SUPERSEDED_REFS]]
                if len(refs) > MAX_SUPERSEDED_REFS:
                    shown.append(f"+{len(refs) - MAX_SUPERSEDED_REFS} more")
                details = ', '.join(shown)
                sup_line = [f"superseded saves: {sup}" + (f" — {details}" if details else '')]
        else:
            sup_line = [f"superseded saves: {sup} — references unavailable"]
    if not axiom.get("events"):
        return [head + " — quiet"] + sup_line, "errors quiet", 0
    lines = [head]
    new = 0
    new_codes = []
    for row in axiom.get("top", [])[:MAX_CODES]:
        m = mapping.get(row["code"], {})
        if row['code'] == 'uncaught_error':
            signatures = axiom.get('uncaught_signatures')
            if isinstance(signatures, dict) and sum(signatures.get(k, 0) for k in ('resize_observer', 'unmapped')) == row['events']:
                parts = []
                if signatures.get('resize_observer'):
                    parts.append(f"#13670 ({signatures['resize_observer']} ev)")
                if signatures.get('unmapped'):
                    parts.append(f"unmapped (new signature): {signatures['unmapped']} ev")
                    new += 1
                    new_codes.append(row['code'])
                target = '→ ' + '; '.join(parts) if parts else ''
            else:
                target = '→ unmapped (signature unavailable)'
        elif m.get("status") == "NEW":
            new += 1
            new_codes.append(row['code'])
            target = "NEW → launch customer-artifact-investigator"
        elif m.get("refs"):
            target = "→ " + ", ".join(m["refs"][:2])
        else:
            target = ""
        lines.append(f"• {row['code']} — {row['events']} ev / {row['users']} users {target}".rstrip())
    # The named off-top NEW/unmapped detail belongs only to the 3h cadence.
    # Keep the 1h branch byte-for-byte compatible with the prior renderer:
    # its top-five mapper contract intentionally does not expose off-top rows.
    shown = {row["code"] for row in axiom.get("top", [])[:MAX_CODES]}
    other_codes = axiom.get("other_codes", 0)
    other_events = axiom.get("other_events", 0)
    for row in axiom.get("all_codes", []):
        code = row["code"]
        m = mapping.get(code, {})
        if code == 'uncaught_error' or code in shown:
            continue
        if window_label != "3h":
            if m.get("status") != "mapped" or not m.get("refs"):
                continue
            shown.add(code)
            lines.append(f"+ {code}: {row['events']} ev / {row['users']} user{'s' if row['users'] != 1 else ''} → {', '.join(m['refs'][:2])}")
            other_codes -= 1
            other_events -= row["events"]
            continue
        status = m.get("status")
        refs = m.get("refs") or []
        if status == "mapped" and refs:
            target = "→ " + ", ".join(refs[:2])
            prefix = "+"
        elif status == "NEW":
            new += 1
            new_codes.append(code)
            target = "NEW → launch customer-artifact-investigator"
            prefix = "•"
        elif status in {"unmapped", None} or not refs:
            target = "unmapped → classify and route for investigation"
            prefix = "•"
        else:
            continue
        shown.add(code)
        lines.append(f"{prefix} {code} — {row['events']} ev / {row['users']} users {target}")
        other_codes -= 1
        other_events -= row["events"]
    if other_codes > 0:
        lines.append(f"• +{other_codes} other codes, {other_events} events")
    tldr = f"{axiom['events']} errors / {axiom['users']} users"
    if new:
        suffix = f": {', '.join(new_codes)}" if window_label == '3h' else ''
        tldr += f" ({new} NEW code{'s' if new > 1 else ''}{suffix})"
    return lines + sup_line, tldr, new


def support_block(support: dict | None, window_label: str) -> tuple[list[str], str]:
    if support is None:
        return [f"*Support ({window_label}):* unavailable"], "support unavailable"
    failures = support.get("failures") or []
    threshold = support.get("threshold_min", 60)
    slack = support.get("slack", [])
    in_app = support.get("in_app", [])
    if failures:
        return [f"*Support ({window_label}):* UNAVAILABLE ({'; '.join(failures)})"], "support unavailable"
    if not slack and not in_app:
        return [f"*Support ({window_label}):* quiet"], "support quiet"
    needs = [t for t in slack if t["state"] == "needs_reply"]
    answered = [t for t in slack if t["state"] == "answered"]
    requester_resolved = any(t.get("answered_by") is None for t in answered)
    prefix = "PARTIAL — " if failures else ""
    lines = [f"*Support ({window_label}):* {prefix}{len(slack)} Slack thread(s), {len(in_app)} in-app"]
    if failures:
        lines.append(f"• Incomplete: {'; '.join(failures)}")
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
    return lines, (f"partial: {tldr}" if failures else tldr)


MAX_USERS = 6


def active_users_block(active: dict | None, window_label: str) -> tuple[list[str], str]:
    """One header line + up to MAX_USERS compact user lines (keeps the post <= ~22 lines)."""
    if active is None:
        return [f"*Active users ({window_label}):* unavailable"], "active users unavailable"
    users = active.get('users', [])
    def excluded_domain(row):
        email = str(row.get('user') or '').strip().lower()
        domain = email.rpartition('@')[2] if '@' in email else ''
        return domain == 'scribie.com' or domain.endswith('.scribie.com')
    users = [u for u in users if not excluded_domain(u)]
    removed = len(active.get('users', [])) - len(users)
    count = max(0, active.get("count", 0) - removed)
    non_scribie_count = active.get("non_scribie_count")
    if non_scribie_count is not None:
        non_scribie_count = max(0, non_scribie_count - removed)
    if non_scribie_count is None:
        head = f"*Active users ({window_label}):* {count}"
    else:
        head = f"*Active users ({window_label}):* {non_scribie_count} non-Scribie"
        head += f" ({count} external +{active.get('internal', 0)} internal)"
    if active.get("prior_non_scribie_count") is not None:
        head += f" (prior {active['prior_non_scribie_count']})"
    elif active.get("prior_count") is not None:
        head += f" (prior {active['prior_count']})"
    if non_scribie_count is None and active.get("internal"):
        head += f" +{active['internal']} internal"
    if "excluded_scribie" in active or removed:
        head += f"; {active.get('excluded_scribie', 0) + removed} Scribie excluded"
        head += f" — {active.get('method', '')}"
    if active.get("unclassified"):
        head += f"; unclassified {active['unclassified']} ({', '.join(active.get('unclassified_users', []))})"
    if not users:
        total = non_scribie_count if non_scribie_count is not None else count
        suffix = " — quiet" if total == 0 and not active.get("unclassified") else ""
        label = "non-Scribie active users" if non_scribie_count is not None else "active users"
        return [head + suffix], f"{total} {label}"
    shown = [f"{u['user']} ({u['files']}f)" for u in users[:MAX_USERS * 2]]
    lines = [head]
    for i in range(0, min(len(shown), MAX_USERS * 2), 3):
        lines.append("• " + ", ".join(shown[i:i + 3]))
    if len(users) > MAX_USERS * 2:
        lines.append(f"• +{len(users) - MAX_USERS * 2} more")
    total = non_scribie_count if non_scribie_count is not None else count
    label = "non-Scribie active users" if non_scribie_count is not None else "active users"
    return lines, f"{total} {label}"


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


def active_slot_max() -> int:
    """Highest active dev slot (Rajiv C0ALZJHGE49 thread 1793753137.367399, ts
    1793753271.871029: "Remove S5-S8 from the report. They have been decommissioned.")."""
    try:
        return int(os.environ.get("PM_ACTIVE_SLOT_MAX", "4"))
    except ValueError:
        return 4


def slot_block(slots_doc: dict | None, now: datetime, release_seen: dict | None = None) -> tuple[list[str], str, list[str]]:
    """Return (lines, tldr, pm_actions). Rajiv 2026-09-29 13:23 IST:
    "add the slot status to this hourly report as well. idle slots have to be checked."
    Free slots above PM_ACTIVE_SLOT_MAX (decommissioned) are omitted from the
    line, the idle count and idle-slot actions; occupied ones still show.
    """
    if slots_doc is None:
        return ["*Slots:* MoP unreachable"], "slots unknown", []
    slots = slots_doc.get("slots", []) if isinstance(slots_doc, dict) else slots_doc
    cells, actions = [], []
    max_slot = active_slot_max()
    for s in sorted(slots, key=lambda s: s.get("slot", 0)):
        n = s.get("slot")
        if s.get("decommissioned") or s.get("status") == "decommissioned":
            cells.append(f"S{n} {s.get('name') or '?'}: decommissioned")
            continue
        try:
            above_max = int(n) > max_slot
        except (TypeError, ValueError):
            above_max = False
        if above_max and not s.get("occupied"):
            continue
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
    all_decommissioned = bool(slots) and all(s.get("decommissioned") or s.get("status") == "decommissioned" for s in slots)
    tldr = "dev slots decommissioned" if all_decommissioned else (f"{idle_n} idle slot(s) ≥{IDLE_SLOT_MIN}m" if idle_n else "slots busy")
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
    non_drafts = sum(not r.get("isDraft", r.get("draft", False)) for r in prs)
    drafts = len(prs) - non_drafts
    summary = f"{non_drafts} open non-draft PRs (+{drafts} drafts)"
    cap = " — cap exceeded" if non_drafts > 25 else ""
    lines = [f"*{summary} — cap {non_drafts}/25{cap}:*"]
    order = {"red": 0, "CI+E2E green": 1}
    ranked = sorted(prs, key=lambda r: (order.get(r["state"].split(":")[0], 2), -int(r["number"])))
    for r in ranked[:max_prs]:
        lines.append(f"• #{r['number']} {r['head']} — {r['state']} — {r['owner']} — {r['next']}")
    if len(prs) > max_prs:
        lines.append(f"• +{len(prs) - max_prs} more (pr-open-snapshot.py --text)")
    red = [r for r in prs if r["state"].startswith("red")]
    unowned_red = [r for r in red if "unowned" in r["state"]]
    merge = [f"#{r['number']}@{r['head']}" for r in prs if r["next"] == "CTO merge ask"]
    tldr = summary
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
# Rajiv 2026-10-25 (ts 1792914885.859829): unfinished agents the pane still lists but that went quiet >3h (up to 24h) are
# reported as STALE instead of silently dropped, so the hourly count reconciles with the pane.
AGENT_STALE_MAX_S = 24 * 3600


def _compact(value, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = text if len(text) <= limit else text[:limit - 1].rstrip() + "…"
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


INVENTORY_KINDS = ('ready_pool', 'triage_retained_scope')


def pending_obligations(path: Path, now: datetime) -> dict:
    """Read PM-owned open obligations without creating or modifying the ledger."""
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM obligations WHERE status='open' AND lower(trim(coalesce(owner,''))) IN ('pm','') "
                                "AND kind NOT IN ('ready_pool','triage_retained_scope') ORDER BY created_at, id").fetchall()
            # Inventory rows are counted separately, never listed as PM actions.
            inventory = {k: 0 for k in INVENTORY_KINDS}
            for kind, count in conn.execute("SELECT kind, COUNT(*) FROM obligations WHERE status='open' "
                                            "AND kind IN ('ready_pool','triage_retained_scope') GROUP BY kind"):
                inventory[kind] = int(count)
        active = []
        for row in rows:
            row = dict(row)
            until = _parse_ts(row.get('suppress_until'))
            if row.get('suppress_until') and until is None:
                return {'error': 'invalid suppression timestamp'}
            if until and until > now:
                continue
            active.append(row)
        return {'items': [r for r in active if r['kind'] != 'backlog'],
                'backlog': sum(r['kind'] == 'backlog' for r in active),
                'inventory': inventory}
    except (OSError, sqlite3.Error) as exc:
        return {'error': type(exc).__name__}


_NEXT_ACTION = re.compile(r'(?im)^\s*(?:[-*]\s*)?(?:\*\*)?next_action(?:\*\*)?\s*:\s*(.+)$')
_MERGE_CONDITION = re.compile(r'\b(?:when|after)\s+(?:PR\s+)?#(\d+)\s+merges\b', re.I)


def pending_github(run=subprocess.run, timeout: int = COLLECTOR_TIMEOUTS["pending-github"]) -> dict:
    """Paginate all open issues and PRs under one overall wall-clock deadline."""
    deadline = monotonic() + timeout

    def api(endpoint):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired('gh api', timeout)
        result = run(['gh', 'api', '--cache', '5m', endpoint], capture_output=True,
                     text=True, errors='replace', timeout=min(45, remaining))
        if result.returncode:
            raise RuntimeError('GitHub API unavailable')
        return json.loads(result.stdout)
    try:
        items, page = [], 1
        while True:
            batch = api(f'repos/Scribie/heydonna-app/issues?state=open&per_page=100&page={page}')
            if not isinstance(batch, list):
                raise ValueError('invalid issue response')
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
            if page > 100:
                raise ValueError('issue pagination limit')
        cache, found = {}, []
        for item in items:
            for match in _NEXT_ACTION.finditer(item.get('body') or ''):
                text = match.group(1)
                condition = _MERGE_CONDITION.search(text)
                if not condition or not re.search(r'\bPM\b', text, re.I):
                    continue
                number = int(condition.group(1))
                if number not in cache:
                    dep = api(f'repos/Scribie/heydonna-app/issues/{number}')
                    if not isinstance(dep, dict) or 'state' not in dep:
                        raise ValueError('invalid dependency response')
                    pr = dep.get('pull_request')
                    if pr is not None and (not isinstance(pr, dict) or 'merged_at' not in pr):
                        raise ValueError('invalid PR response')
                    cache[number] = pr['merged_at'] if pr is not None else None
                if cache[number]:
                    if _parse_ts(cache[number]) is None:
                        raise ValueError('invalid merge timestamp')
                    found.append({'number': item['number'], 'text': text, 'dependency': number, 'merged_at': cache[number]})
        return {'items': found}
    except subprocess.TimeoutExpired:
        return {'error': f'UNAVAILABLE (timeout {timeout}s)'}
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        return {'error': str(exc) if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__}


def pending_actions_block(obligations: dict, github: dict, now: datetime) -> list[str]:
    head = '*Pending PM actions:*'
    lines, ledger, issues = [head], [], []
    def age(value):
        started = _parse_ts(value)
        return _fmt_dur(max(0, int((now - started).total_seconds() // 60))) if started else 'unknown'
    if 'error' in obligations:
        lines.append('• obligations: UNAVAILABLE (' + _compact(obligations['error'], 70) + ')')
    else:
        lines[0] += f" backlog rows: {obligations['backlog']} (not shown)"
        held = []
        for row in obligations['items']:
            target = f"#{row['issue']}" if row.get('issue') else f"PR#{row['pr']}" if row.get('pr') else f"obligation #{row['id']}"
            owner = _compact(row.get('owner') or 'PM triage (unowned)', 40)
            blocker = (row.get('blocker') or '').strip()
            review = row.get('next_review_at')
            review_time = _parse_ts(review)
            state = (row.get('external_state') or '').strip()
            is_held = bool(blocker or (review and (review_time is None or review_time > now)) or re.search(r'(?<![a-z0-9])(?:consumed|uncertain|unknown)(?![a-z0-9])', state, re.I))
            if is_held:
                details = []
                if blocker:
                    details.append('hold: ' + _compact(blocker, 180))
                if state:
                    details.append('state: ' + _compact(state, 80))
                if review:
                    details.append('review: ' + _compact(review, 50))
                held.append(f"• {_compact(target, 35)} | HELD | {'; '.join(details)} | {age(row.get('created_at'))} | {owner}")
            else:
                timing = f" | review: {_compact(review, 50)}" if review else ''
                ledger.append(f"• {_compact(target, 35)} | {_compact(row.get('required_action') or row.get('title') or row['kind'], 90)}{timing} | {age(row.get('created_at'))} | {owner}")
        ledger.extend(held)
    inventory_line = None
    if 'error' not in obligations and sum((obligations.get('inventory') or {}).values()):
        inv = obligations['inventory']
        inventory_line = (f"• Inventory: {inv.get('ready_pool', 0)} ready-pool, "
                          f"{inv.get('triage_retained_scope', 0)} triage (not actions)")
    if 'error' in github:
        lines.append('• GitHub: UNAVAILABLE (' + _compact(github['error'], 70) + ')')
    else:
        for item in sorted(github['items'], key=lambda r: (r['merged_at'], r['number'])):
            text = re.sub(r'\s+', ' ', item['text']).strip()
            excerpt = text if len(text) <= 140 else text[:55].rstrip() + ' … ' + text[-80:].lstrip()
            issues.append(f"• #{item['number']} | PM step {_compact(excerpt, 140)} — precondition #{item['dependency']} merged {age(item['merged_at'])} ago | {age(item['merged_at'])} | PM")
    # Reserve room for both sources so a large ledger cannot hide awakened issue steps.
    slots = max(0, 8 - (len(lines) - 1))
    issue_count = min(len(issues), slots // 2) if ledger else min(len(issues), slots)
    ledger_count = min(len(ledger), slots - issue_count)
    issue_count = min(len(issues), slots - ledger_count)
    lines.extend(ledger[:ledger_count]); lines.extend(issues[:issue_count])
    remaining = len(ledger) + len(issues) - ledger_count - issue_count
    if remaining:
        lines.append(f'• +{remaining} more')
    if len(lines) == 1:
        lines.append('• none')
    if inventory_line:
        lines.append(inventory_line)
    return lines


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
# Record types that mean the agent is mid-work or waiting on its OWN background shell/tool result
# (Rajiv 2026-10-24, C0ALZJHGE49 ts 1792828089.120229: pane showed 8, heartbeat said 4). Such agents
# write nothing for long stretches but stay listed as running in the pane.
WAITING_TYPES = {"attachment", "queue-operation", "api-request", "api-request-blob", "progress", "system"}


def _is_waiting_record(last_record: dict) -> bool:
    kind = last_record.get("type")
    if kind in WAITING_TYPES or kind == "user":
        return True
    if kind == "assistant":
        content = (last_record.get("message") or {}).get("content")
        return isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_use" for c in content)
    return False


def classify_agent(last_record: dict | None, modified_at: float, now: datetime) -> str:
    """Pure transcript classifier, not a process-liveness or TaskStop decision."""
    if not isinstance(last_record, dict) or not last_record.get("type"):
        return "unknown"
    if last_record.get("type") == "assistant" and (last_record.get("message") or {}).get("stop_reason") == "end_turn":
        return "done"
    idle_s = max(0, now.timestamp() - modified_at)
    if idle_s > AGENT_RECENT_S:
        return "expired"
    if idle_s < 3600 or _is_waiting_record(last_record):
        return "active"  # waiting on its own background work still counts as running
    return "inactive"


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


def _is_listed_agent(path: Path, include_nested: bool = True) -> bool:
    """The pane lists only DIRECT agents (nested sub-subagents share the dir with spawnDepth > 1) that
    are not yet collected: meta.finished == true means the result was delivered and the pane dropped it."""
    try:
        meta = json.loads(path.with_suffix(".meta.json").read_text())
    except (OSError, ValueError):
        return True
    if not isinstance(meta, dict):
        return True
    depth = meta.get("spawnDepth", 1)
    if isinstance(depth, (int, float)) and depth > 1 and not include_nested:
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
    """Count agents only from the newest top-level PM session transcript.
    A /clear ends the previous session's agents even without finish records.
    Keep the current session's existing running/completed/inactive classification.
    """
    now = now or datetime.now(timezone.utc)
    if not root.is_dir():
        return None
    if process_start is None:
        process_start = pm_process_start()
    if process_start is None:
        return None
    current_session = None
    newest = 0.0
    for session in root.glob("*.jsonl"):
        try:
            modified = session.stat().st_mtime
            if modified > newest:
                newest = modified
                current_session = session
        except OSError:
            continue
    if current_session is None:
        return None
    if now.timestamp() - newest > PM_SESSION_FRESH_S:
        return None  # fresh children cannot revive a dead PM pane
    running, completed, inactive, stale, unreadable = 0, [], [], [], 0
    for path in (root / current_session.stem / "subagents").glob("agent-*.jsonl"):
        try:
            modified_at = path.stat().st_mtime
            age_s = now.timestamp() - modified_at
            if age_s > AGENT_STALE_MAX_S or not _is_listed_agent(path):
                continue
            started = _session_started(path)
            if started is not None and started < process_start:
                continue  # prior claude process / restart
            state = classify_agent(_last_agent_record(path), modified_at, now)
            if state == "expired" or (state == "done" and age_s > AGENT_COMPLETED_S):
                stale.append(_agent_row(path, now, modified_at))  # listed by the pane, quiet or uncollected
                continue
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
    for rows in (completed, inactive, stale):
        rows.sort(key=lambda row: (-row["idle_min"], row["description"]))
    return {"running": running, "completed": completed, "inactive": inactive, "stale": stale, "unreadable": unreadable}


def agents_line(doc: dict | None) -> str:
    if doc is None:
        return "*Agents:* unavailable"
    completed = doc.get("completed", [])
    inactive = doc.get("inactive", [])
    running = doc["running"]
    stale = doc.get("stale", [])
    listed = running + len(completed) + len(inactive) + len(stale)
    # Proxy-era work runs in px-* tmux windows, not these transcripts. "0 listed"
    # used to read as "no agents" (Rajiv C0ALZJHGE49 thread 1794204953.859999).
    line = (f"*Agents:* {listed} listed · {running} running · {len(completed)} completed · "
            f"{len(inactive)} inactive")
    if stale:
        line += f" · {len(stale)} stale (quiet >3h or uncollected, oldest {stale[0]['idle_min'] // 60}h)"
    line += f" — PM stops {len(completed) + len(inactive)} in Step 3c"
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
    'nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader && '
    'printf "\\n__CONTAINERS__\\n" && docker ps --no-trunc --format "{{.Names}} {{.Status}}|{{.ID}}" && '
    'printf "__PROCESSES__\\n" && ps -eo args= && printf "__END__\\n" && '
    'printf "__IDENTITIES__\\n" && docker ps -q --no-trunc | xargs -r docker inspect -f "{{.Id}}:{{.State.StartedAt}}"'
)


def parse_training_output(stdout: str, expected_container: str | None = None) -> dict | None:
    """GPU activity is telemetry, not proof of trainer liveness."""
    try:
        gpu_text, rest = stdout.split("__CONTAINERS__\n", 1)
        container_text, rest = rest.split("__PROCESSES__\n", 1)
        process_text, tail = rest.split("__END__\n", 1)
        gpu = [float(m.group(1)) for m in re.finditer(r"^\s*([\d.]+)\s*%?\s*,", gpu_text, re.M)]
        identities = []
        if tail.strip():
            if not tail.startswith('__IDENTITIES__\n'):
                return None
            identities = [line for line in tail.splitlines()[1:] if re.fullmatch(r'[0-9a-f]{64}:.+', line)]
        if not gpu or not container_text.strip() or not process_text.strip():
            return None
        containers = [line.split(None, 1)[0] for line in container_text.splitlines()
                      if re.match(r"^\S+\s+Up\b", line)]
        # Match executable trainer argv, never an arbitrary busy container or monitor.
        trainers = [line for line in process_text.splitlines()
                    if re.search(r"(?:^|\s)(?:\S*/)?(?:python[\d.]*|torchrun)\s+(?:-u\s+)?(?:\S*/)?(?:train(?:_[\w-]+)?\.py|-[m]\s+\S*train\S*)", line)]
        owned = expected_container if expected_container in containers else None
        return {"running": bool(owned or trainers), "gpu": max(gpu), 'container_ids': identities,
                "run": owned or ("trainer process" if trainers else "none")}
    except (ValueError, TypeError):
        return None


def spark_probe(timeout: int = 30) -> tuple[dict | None, str]:
    """Return (doc|None, reason). doc is {} when no trainer is running."""
    import re as _re
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "dgx-spark", _SPARK_REMOTE],
            capture_output=True, text=True, errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, f"UNAVAILABLE (timeout {timeout}s)"
    except Exception as exc:
        return None, type(exc).__name__
    if r.returncode != 0:
        return None, f"ssh rc={r.returncode}"
    return parse_training_output(r.stdout), "incomplete probe"



def monitor_up(container: str | None = None) -> bool:
    """Liveness from live processes. The monitor may run as `bash spark-run-monitor.sh ...`
    or as a python process fed the script on stdin (argv lacks the script name), so match
    on its argv shape (--run-name + --container) and, when known, the active container."""
    try:
        r = subprocess.run(["ps", "-ww", "-ax", "-o", "pid=,command="], capture_output=True, text=True, errors="replace", timeout=5)
        own = os.getpid()
        for line in r.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) < 2 or not parts[0].isdigit() or int(parts[0]) == own:
                continue
            c = parts[1]
            if "grep" in c.split(None, 1)[0] or "pgrep" in c:
                continue
            argv = c.split()
            is_monitor = "spark-run-monitor" in c or ("--run-name" in argv and "--container" in argv and "--memory-config" in argv)
            if not is_monitor:
                continue
            if container and "--container" in argv:
                i = argv.index("--container")
                if i + 1 < len(argv) and argv[i + 1] != container:
                    continue
            return True
        return False
    except Exception:
        return False


import datetime as spark_datetime
import tempfile
import time

SPARK_PROGRESS_STATE_DIR = Path.home() / '.claude/state/spark-monitor/progress'


def spark_progress_state_path(run, host, log):
    import hashlib
    key = hashlib.sha256((host + '\0' + log).encode()).hexdigest()[:12]
    return SPARK_PROGRESS_STATE_DIR / (re.sub(r'[^\w.-]', '_', run) + '-' + key + '.json')


def spark_progress_update(run, host, log, step, total, now=None, *, identity):
    now = time.time() if now is None else now
    path = spark_progress_state_path(run, host, log)
    try:
        data = json.loads(path.read_text())
        samples = data['samples']
        old = data['identity']
        if (data['total'] != total or not isinstance(samples, list)
                or old['log_id'] != identity['log_id'] or old['container'] != identity['container']
                or old['size'] > identity['size']):
            samples = []
    except (OSError, ValueError, KeyError, TypeError):
        samples = []
    samples = [[t, s] for t, s in samples if isinstance(t, (int, float)) and isinstance(s, int) and now - 14400 <= t <= now]
    if samples and (step < samples[-1][1] or now < samples[-1][0]):
        samples = []
    samples.append([now, step])
    samples = samples[-256:]
    data = {'run': run, 'host': host, 'log': log, 'total': total, 'identity': identity, 'samples': samples, 'updated': now}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.progress-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return spark_progress_render(data, now)


def spark_progress_render(data, now=None):
    now = time.time() if now is None else now
    samples = data.get('samples') or []
    if not samples:
        return 'ETA n/a | sec/step n/a | steps last hour n/a'
    t, step = samples[-1]
    recent = [(a, b) for a, b in samples[:-1] if 0 < t-a <= 10800 and b < step]
    preferred = [(a, b) for a, b in recent if t-a >= 3600]
    base = (preferred or recent)
    stalled = len(samples) > 1 and samples[-2][1] == step
    sec = (t - base[0][0]) / (step - base[0][1]) if base and not stalled else None
    hour = [(a, b) for a, b in samples[:-1] if 0 < t-a <= 3600]
    if hour:
        elapsed = t - hour[0][0]
        window = 'hour' if elapsed == 3600 else ('%gm' % (elapsed / 60))
        steps = 'steps in last %s: %s' % (window, step - hour[0][1])
    else:
        steps = 'steps in last hour: n/a'
    eta = 'n/a'
    if sec is not None and data.get('total') is not None:
        remaining = max(0, data['total'] - step) * sec
        minutes = int(remaining // 60)
        duration = '%dh%02dm' % (minutes // 60, minutes % 60) if minutes >= 60 else '%dm' % minutes
        date = spark_datetime.datetime.fromtimestamp(t + remaining, spark_datetime.timezone(spark_datetime.timedelta(hours=5, minutes=30))).strftime('%Y-%m-%d %H:%M IST')
        eta = duration + ' (' + date + ')'
    result = 'ETA %s | sec/step %s | %s' % (eta, ('%.1f' % sec) if sec is not None else 'n/a', steps)
    if stalled:
        interval = max(0, int(t - samples[-2][0]))
        result += ' | NO STEP PROGRESS in %ds' % interval
    return result


def spark_progress_latest(max_age=3600, now=None, container_ids=()):
    now = time.time() if now is None else now
    found = []
    for path in SPARK_PROGRESS_STATE_DIR.glob('*.json'):
        try:
            data = json.loads(path.read_text())
            identity = data.get('identity') or {}
            if (identity.get('container', '') in container_ids
                    and data.get('samples') and 0 <= now - data['updated'] <= max_age):
                found.append(data)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if not found:
        return None
    data = max(found, key=lambda d: d['updated'])
    return data['run'] + ' · step %s/%s · ' % (data['samples'][-1][1], data['total']) + spark_progress_render(data, now)


def training_line(probe=None, mon_up=None) -> tuple[str, list[str]]:
    """Never omitted. Returns (line, actions)."""
    doc, reason = probe if probe is not None else spark_probe()
    if doc is None:
        return f"*Training:* UNKNOWN ({reason or 'probe failed'})", []
    status = "RUNNING" if doc.get("running") else "NOT RUNNING"
    line = f"*Training:* {status} · GPU {doc.get('gpu', 0):g}% · container {doc.get('run', 'none')}"
    if doc.get("running"):
        try:
            line += " · " + (spark_progress_latest(container_ids=doc.get('container_ids', ())) or "ETA n/a | sec/step n/a | steps last hour n/a")
        except Exception:
            line += " · ETA n/a | sec/step n/a | steps last hour n/a"
    return line, []



REMOTE_BOX_SCRIPT = Path.home() / ".claude/scripts/remote-test-box.sh"


# remote-test-box.sh cmd_list prints the configured target from pool_kind_set.
# The summary's second number is the live member count, not that target
# (`pool: N free / members, queued`). Browser target changed 8 → 5 on 2026-11-09
# (Rajiv C0ALZJHGE49 thread 1794200533.004039, ts 1794201011.607329).
_POOL_HEADER = re.compile(
    r"^== (browser|test) pool \((cx33|cx43), (rtb-tpool|rtb-pool)-NN, target (\d+)\) ==$"
)
_POOL_SHAPE = {"browser": ("cx43", "rtb-pool"), "test": ("cx33", "rtb-tpool")}
_POOL_STATES = {"free", "leased", "releasing", "expired", "broken", "preempting", "rebuilding", "borrowed", "reserved"}


def parse_remote_pools(stdout: str) -> str:
    """Pure parser for `remote-test-box.sh list`. Raises ValueError when unhealthy."""
    pools = {}
    current = None
    for line in stdout.splitlines():
        header = _POOL_HEADER.match(line.strip())
        if header:
            current, typ, prefix, target_s = header.groups()
            target = int(target_s)
            if current in pools or _POOL_SHAPE[current] != (typ, prefix) or not 1 <= target <= 64:
                raise ValueError("unexpected pool header")
            pools[current] = {"rows": [], "summary": None, "target": target}
            continue
        if current is None:
            continue
        row = re.match(r"^(rtb-(?:tpool|pool)-\S+)\s+(\S+(?: \(test\))?)(?:\s|$)", line.strip())
        if row:
            name, state = row.groups()
            if state == "borrowed (test)":
                state = "borrowed"
            if not name.startswith("rtb-tpool-" if current == "test" else "rtb-pool-"):
                raise ValueError("box in wrong pool")
            if state not in _POOL_STATES:
                raise ValueError("unknown box state")
            pools[current]["rows"].append((name, state))
            continue
        summary = re.match(r"^pool:\s*(\d+) free / (\d+),", line.strip())
        if summary:
            pools[current]["summary"] = tuple(map(int, summary.groups()))
    if set(pools) != {"test", "browser"}:
        raise ValueError("missing pool")
    pieces = []
    for name in ("test", "browser"):
        pool = pools[name]
        rows = pool["rows"]
        names = [box for box, _ in rows]
        count = lambda state, rows=rows: sum(s == state for _, s in rows)
        # Summary denominator is live members (cmd_list), not the configured target.
        if pool["summary"] is None or len(names) != len(set(names)) or pool["summary"] != (count("free"), len(rows)):
            raise ValueError(f"invalid {name} pool summary")
        details = [f"{count('leased')} leased", f"{count('releasing')} releasing"]
        if count("preempting"):
            details.append(f"{count('preempting')} preempting")
        if count("rebuilding"):
            details.append(f"{count('rebuilding')} rebuilding")
        if count("reserved"):
            details.append(f"{count('reserved')} reserved")
        if count("borrowed"):
            details.append(f"{count('borrowed')} borrowed")
        for state in ("expired", "broken"):
            affected = [box for box, s in rows if s == state]
            details.append(f"{len(affected)} {state}" + (f": {', '.join(affected)}" if affected else ""))
        pieces.append(f"{name} pool {count('free')} free / {pool['target']} (" + "; ".join(details) + ")")
    return "*Remote boxes:* " + " · ".join(pieces)


def remote_queue_lines(timeout: int = 5) -> str:
    """Existing hourly invocation only; append local sizing snapshots for the 48h rollout."""
    try:
        result = subprocess.run(["bash", str(REMOTE_BOX_SCRIPT), "queue-metrics", "snapshot"],
                                capture_output=True, text=True, errors="replace", timeout=timeout)
        if result.returncode:
            raise ValueError(f"collector rc={result.returncode}")
        records = [json.loads(line.split(" ", 1)[1]) for line in result.stdout.splitlines()
                   if line.startswith("RTB_QUEUE_METRICS ")]
        if len(records) != 2 or {r["pool"] for r in records} != {"pool", "pool-cx33"}:
            raise ValueError("missing/duplicate pool metrics")
        lines = []
        for r in records:
            name = "test" if r["pool"] == "pool-cx33" else "browser"
            states = r["states"]
            waits = []
            for tier, key in (("P0", "3"), ("P1", "2"), ("P2", "1"), ("untagged", "0")):
                sample = r["wait_s"][key]
                p50, p95 = sample["p50"], sample["p95"]
                value = "no samples" if not sample["n"] else f"{p50}/{p95}s"
                waits.append(f"{tier} {value}")
            broken = ""
            if "broken_box_hours" in r:
                broken = f"; broken {r.get('broken_n', 0)} ({r['broken_box_hours']} box-h)"
            lines.append(f"• RTB {name}: {states.get('free', 0)} free / {states.get('leased', 0)} leased / "
                         f"{r['queued']} queued / {r['reserved']} reserved; wait p50/p95: " + "; ".join(waits) + broken)
            for alert in r["alerts"]:
                lines.append(f"  ALERT: {name} ticket {alert['ticket']} waiting {alert['age_s']}s "
                             f"(base {'P' + str(3-alert['base']) if alert['base'] else 'untagged'})")
        return "\n".join(lines)
    except subprocess.TimeoutExpired:
        return f"*RTB queue:* UNAVAILABLE (timeout {timeout}s)"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return f"*RTB queue:* UNAVAILABLE ({type(exc).__name__})"


def remote_boxes_line(timeout: int = 20) -> str:
    """Collect both pools; malformed or incomplete output is never reported as healthy."""
    try:
        result = subprocess.run(["bash", str(REMOTE_BOX_SCRIPT), "list"], capture_output=True,
                                text=True, errors="replace", timeout=timeout)
        if result.returncode:
            raise ValueError(f"collector rc={result.returncode}")
        return parse_remote_pools(result.stdout) + "\n" + remote_queue_lines(timeout=timeout)
    except subprocess.TimeoutExpired:
        reason = f"UNAVAILABLE (timeout {timeout}s)"
    except (OSError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
    return f"*Remote boxes:* unavailable ({reason})"


def parse_mac_list(text: str) -> str:
    match = re.search(r'^boxes:\s*(\d+)/(\d+)\s*$', text, re.MULTILINE)
    if not match or int(match[1]) > int(match[2]) or int(match[2]) != 3:
        raise ValueError('invalid list')
    return f'containers {match[1]}/{match[2]}'


def parse_mac_receipts(text: str, now: datetime) -> str:
    counts = {'mac': 0, 'hetzner': 0, 'fallback': 0}
    for line in text.splitlines():
        fields = line.split('\t')
        if len(fields) != 7:
            raise ValueError('invalid TSV')
        timestamp, provider, requested, _, _, fallback, outcome = fields
        when = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        if when.tzinfo is None:
            raise ValueError('missing timezone')
        if now - timedelta(hours=1) <= when <= now and outcome == 'acquired':
            if provider not in ('mac', 'hetzner'):
                raise ValueError('unknown provider')
            counts[provider] += 1
            if fallback and fallback not in ('-', 'none'):
                counts['fallback'] += 1
    return f"provider log 60m acquired mac {counts['mac']}, hetzner {counts['hetzner']}, fallback {counts['fallback']}"


def parse_mac_load(text: str) -> str:
    load = re.search(r'load averages?:\s*([\d.]+)[, ]+([\d.]+)[, ]+([\d.]+)', text)
    cpu = re.search(r'CPU usage:\s*([\d.]+)% user,\s*([\d.]+)% sys,\s*([\d.]+)% idle', text)
    if not load or not cpu:
        raise ValueError('invalid top/uptime')
    return f'load {load[1]}/{load[2]}/{load[3]}, CPU idle {cpu[3]}%'


def mac_box_stats() -> dict:
    """Three independently cached, read-only probes; never treat missing data as zero."""
    cache = HOURLY_CACHE / 'mac-box-v2'
    now = time.time()
    py = sys.executable
    sources = (
        ('list', ['bash', str(REMOTE_BOX_SCRIPT), 'list', '--provider', 'mac'], parse_mac_list),
        ('provider-log', [py, '-I', '-c', 'import pathlib,sys; sys.stdout.write(pathlib.Path(sys.argv[1]).read_text())',
                          str(Path.home() / '.claude/logs/rtb-provider.log')],
         lambda text: parse_mac_receipts(text, datetime.now(timezone.utc))),
        ('top/uptime', ['ssh', '-i', str(Path.home() / '.ssh/id_ed25519_mac_runner'),
                        '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=8',
                        'runner@192.168.68.114', 'uptime; top -l 1 -n 0'], parse_mac_load),
    )
    parts = []
    for name, command, parse in sources:
        path = cache / (name.replace('/', '-') + '.json')
        try:
            cached = json.loads(path.read_text())
            if 0 <= now - cached['at'] < 60 and isinstance(cached['value'], str):
                parts.append(cached['value'] + ' (cached)')
                continue
        except (OSError, ValueError, KeyError, TypeError):
            pass
        try:
            result = subprocess.run(command, capture_output=True, text=True, errors='replace', timeout=10)
            if result.returncode:
                parts.append(f'{name} UNAVAILABLE (rc={result.returncode})')
                continue
            value = parse(result.stdout)
            parts.append(value)
            try:
                store_observation(path, value, time.time())
            except OSError:
                pass
        except subprocess.TimeoutExpired:
            parts.append(f'{name} UNAVAILABLE (rc=timeout, 10s)')
        except OSError:
            parts.append(f'{name} UNAVAILABLE (rc=unavailable, launch failed)')
        except (ValueError, TypeError, OverflowError):
            parts.append(f'{name} UNKNOWN (rc=0, invalid output)')
    return {'line': '• Mac test box: ' + ' · '.join(parts)}


def _elapsed_minutes(value: str) -> int:
    days, sep, rest = value.partition('-')
    if not sep:
        rest, days = days, '0'
    parts = [int(x) for x in rest.split(':')]
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return int(days) * 1440 + seconds // 60


def proxy_snapshot(tmux_text: str, ps_text: str, now: datetime, completions: list[dict]) -> dict:
    """Read-only best-effort view; never inspect environment or runner files."""
    processes = {}
    ps_lines = ps_text.splitlines()
    truncated = len(ps_lines) > 2000
    for line in islice(ps_lines, 2000):
        match = re.match(r'^\s*(\d+)\s+(\d+)\s+(\S+)\s+(.*)$', line)
        if match:
            pid, ppid, age, command = match.groups()
            processes[int(pid)] = (int(ppid), age, command)
    windows = []
    for line in tmux_text.splitlines():
        parts = line.split('\t')
        if len(parts) != 4:
            continue
        session, name, pid, dead = parts
        if not ((session == '0' and name.startswith('px-')) or session == 'proxies'):
            continue
        try:
            windows.append((session, name, int(pid), dead == '1'))
        except ValueError:
            continue

    def descendants(root):
        found = {root}
        while True:
            more = {pid for pid, (parent, _, _) in processes.items() if parent in found}
            if more <= found:
                return found
            found |= more

    rows, used = [], set()
    for session, window, root, dead in windows + [('', '', 0, False)]:
        family = descendants(root) if root else set(processes) - used
        claudes = []
        for pid in family:
            if pid not in processes:
                continue
            _, age, command = processes[pid]
            if not re.search(r'(?:^|/)claude\s', command) or not re.search(r'(?:^|\s)-p(?:\s|$)', command):
                continue
            name = re.search(r'(?:^|\s)--name(?:=|\s+)(proxy-[\w.-]+)', command)
            if name:
                claudes.append((pid, age, command, name.group(1)))
        if window and not claudes:
            rows.append({'name': f'{session}:{window}', 'state': 'dead window' if dead else 'left-over window'})
        for pid, age, command, name in claudes:
            if pid in used:
                continue
            used.add(pid)
            model = re.search(r'(?:^|\s)--model(?:=|\s+)([^\s]+)', command)
            turns = re.search(r'(?:^|\s)--max-turns(?:=|\s+)(\d+)', command)
            age_min = _elapsed_minutes(age)
            output = None
            agent = 'unknown'
            out = None
            parent = processes[pid][0]
            for other, (ppid, _, cmd) in processes.items():
                if ppid != parent or 'proxy-agent-stream.py ' not in cmd:
                    continue
                try:
                    argv = shlex.split(cmd)
                    index = next(i for i, v in enumerate(argv) if v.endswith('/proxy-agent-stream.py'))
                    out = Path(argv[index + 1])
                    live = Path(argv[index + 2])
                    output = next((p for p in (live, out.with_suffix('.log'), out) if p.is_file()), None)
                except (ValueError, IndexError, StopIteration):
                    pass
                break
            if out is not None:
                for _, (_, _, cmd) in processes.items():
                    try:
                        argv = shlex.split(cmd)
                        index = next(i for i, v in enumerate(argv) if v.endswith('/proxy-agent.sh'))
                        if '--out' in argv and Path(argv[argv.index('--out') + 1]) == out:
                            agent = argv[index + 1]
                            break
                    except (ValueError, IndexError, StopIteration):
                        continue
            idle = None
            if output:
                try:
                    idle = max(0, int((now.timestamp() - output.stat().st_mtime) // 60))
                except OSError:
                    pass
            # Conservative budget estimate: two minutes per max-turn, stale after twice that.
            stale = (idle is not None and idle >= 20) or (turns is not None and age_min > 4 * int(turns.group(1)))
            rows.append({'name': name, 'agent': agent, 'model': model.group(1) if model else 'unknown',
                         'age': age_min, 'idle': idle, 'state': 'STALE' if stale else 'running'})
    return {'rows': rows, 'completions': completions, 'truncated': truncated}


# One list-windows call. A single batched ps fills model and process age; there is
# no per-pane ps. Last-activity age comes from tmux window_activity. A failed read
# reuses the last good snapshot for up to 10 minutes, labelled stale.
PROXY_CACHE = Path.home() / ".cache" / "pm-heartbeat" / "proxies-last-good.json"
PROXY_CACHE_MAX_S = 600
PROXY_STALE_ACTIVITY_MIN = 20
PROXY_ROW_CAP = 24
_PROXY_TMUX_FMT = "#{session_name}\t#{window_name}\t#{window_activity}\t#{pane_dead}\t#{pane_pid}\t#{pane_start_command}"


def _proxy_runner_metadata(start: str, now: datetime, exited: bool) -> dict:
    """Read only launcher arguments, never expose its secret-bearing exports."""
    try:
        argv = shlex.split(start)
        if len(argv) == 1:
            argv = shlex.split(argv[0])
        if len(argv) != 2 or Path(argv[0]).name != 'bash':
            return {}
        runner = Path(argv[1])
        if runner.name != 'run.sh' or not runner.parent.name.startswith('proxy-agent-job.'):
            return {}
        text = runner.read_text()
        model = re.search(r'(?:^|\s)--model\s+([^\s]+)', text)
        peer = re.search(r'(?:^|\s)--name\s+([^\s]+)', text)
        pipeline = re.search(r'<\s+(\S+)\s+2>\s+\S+\s+\|\s+python3\s+-I\s+-u\s+\S*proxy-agent-stream\.py\s+(\S+)\s+(\S+)', text)
        result = {'model': model.group(1)} if model else {}
        if peer:
            result['launch_peer'] = shlex.split(peer.group(1))[0]
        # Identity fence only, never a source for work/stage elapsed time.
        born = getattr(runner.parent.stat(), 'st_birthtime', None)
        if born is not None and 0 < born <= now.timestamp():
            result['launch_birth'] = born
        if not pipeline:
            return result
        brief, out, jsonl = (Path(shlex.split(value)[0]) for value in pipeline.groups())
        try:
            with brief.open('rb') as handle:
                raw = handle.read(200001)
            if len(raw) > 200000:
                return result  # No partial-content digest may certify a receipt.
            result['brief_sha256'] = hashlib.sha256(raw).hexdigest()
            content = raw.decode('utf-8', 'replace')
            refs = [f'{kind.lower()}:{number}' for kind, number in re.findall(r'(?im)^\s*(PR|ISSUE):\s*#?(\d+)\s*$', content)]
            if refs:
                result['brief_refs'] = list(dict.fromkeys(refs))
        except (OSError, UnicodeError):
            pass
        try:
            mtime = jsonl.stat().st_mtime
            if math.isfinite(mtime) and 0 <= mtime <= now.timestamp():
                result['output_age'] = int((now.timestamp() - mtime) // 60)
        except (OSError, TypeError, ValueError, OverflowError):
            pass
        if exited:
            try:
                lines = out.read_text()[-200000:].splitlines()
                line = next((line.strip() for line in reversed(lines) if re.match(r'^RESULT\b', line.strip(), re.I)), None)
                if line:
                    result['result_available'] = True
            except (OSError, UnicodeError):
                pass
        return result
    except (OSError, UnicodeError, ValueError, IndexError):
        return {}


PROXY_LOG_ROOT = Path('/tmp/proxy-agent')


def _bound_proxy_stream(pid: int, processes: dict) -> Path | None:
    streams = []
    for parent, _, command in processes.values():
        if parent != pid:
            continue
        try:
            argv = shlex.split(command)
            index = next(i for i, arg in enumerate(argv) if arg.endswith('/proxy-agent-stream.py'))
            path = Path(argv[index + 2])
            if path.resolve().parent == PROXY_LOG_ROOT.resolve() and path.suffix == '.jsonl':
                streams.append(path)
        except (ValueError, IndexError, StopIteration, OSError):
            continue
    return streams[0] if len(streams) == 1 else None


def _proxy_display_metadata(name: str, start: str, pid: int, processes: dict, now: datetime) -> dict:
    name = name.removeprefix('px-') if name.startswith('px-px-') else name
    result = {}
    stream = _bound_proxy_stream(pid, processes)
    if stream:
        match = re.fullmatch(r'.+-' + re.escape(name) + r'-\d+-(\d{8}T\d{6}Z)\.jsonl', stream.name)
        if match:
            try:
                started = datetime.strptime(match.group(1), '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
                seconds = (now - started).total_seconds()
                if seconds >= 0:
                    result['runtime_minutes'] = int(seconds // 60)
            except ValueError:
                pass
    try:
        argv = shlex.split(start)
        if len(argv) == 1:
            argv = shlex.split(argv[0])
        runner = Path(argv[1]) if len(argv) == 2 and Path(argv[0]).name == 'bash' else None
        if runner and runner.name == 'run.sh' and runner.parent.name.startswith('proxy-agent-job.'):
            with (runner.parent / 'prompt0.md').open(encoding='utf-8') as source:
                content = source.read(200000)
            refs = re.findall(r'(?im)^\s*(?:ISSUE|PR):\s*#?(\d+)\s*$', content)
            if refs:
                result['display_refs'] = list(dict.fromkeys(refs))
    except (OSError, UnicodeError, ValueError):
        pass
    if not result.get('display_refs') and stream:
        try:
            with stream.open(encoding='utf-8') as source:
                for _ in range(50):
                    line = source.readline(1048577)
                    if not line or len(line) > 1048576:
                        break
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(record, dict) or record.get('type') != 'user' or record.get('parent_tool_use_id'):
                        continue
                    message = record.get('message') or {}
                    content = message.get('content') if isinstance(message, dict) else None
                    if isinstance(content, list):
                        content = '\n'.join(item.get('text', '') for item in content if isinstance(item, dict) and item.get('type') == 'text' and isinstance(item.get('text'), str))
                    if isinstance(content, str):
                        refs = re.findall(r'(?im)^\s*(?:ISSUE|PR):\s*#?(\d+)\s*$', content)
                        if refs:
                            result['display_refs'] = list(dict.fromkeys(refs))
                        break
        except (OSError, UnicodeError):
            pass
    return result


def _live_proxy_model(name: str, pid: int, age: int | None, processes: dict, now: datetime) -> tuple[str, str]:
    """Use an exact, recent launch record or this pane's own stream init only."""
    name = name.removeprefix('px-') if name.startswith('px-px-') else name
    if not re.fullmatch(r'px-[\w.-]+', name):
        return 'UNAVAILABLE', 'invalid proxy identity'
    root = PROXY_LOG_ROOT
    if age is not None:
        try:
            exact = re.compile(r'.+-' + re.escape(name) + r'-\d+-\d{8}T\d{6}Z\.launch\.log\Z')
            candidates = [path for path in root.glob('*.launch.log')
                          if path.name == name + '.launch.log' or exact.fullmatch(path.name)]
            candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
            for launch in candidates:
                # etime is minute-granular. Reject a log from a previous incarnation.
                if abs((now.timestamp() - launch.stat().st_mtime) / 60 - age) <= 2:
                    with launch.open(encoding='utf-8', errors='replace') as source:
                        text = source.read(65536)
                    ids = re.findall(r'\bmodel=([^\s]+)\s+id=([\w.:-]+)', text)
                    if ids:
                        return ids[-1][1], 'launch log'
        except OSError:
            pass
    children = [command for parent, _, command in processes.values() if parent == pid]
    streams = []
    for command in children:
        try:
            argv = shlex.split(command)
            index = next(i for i, arg in enumerate(argv) if arg.endswith('/proxy-agent-stream.py'))
            path = Path(argv[index + 2])
            if path.resolve().parent == root.resolve() and path.suffix == '.jsonl':
                streams.append(path)
        except (ValueError, IndexError, StopIteration, OSError):
            continue
    if len(streams) == 1:
        try:
            with streams[0].open(encoding='utf-8') as source:
                for _ in range(50):
                    line = source.readline(1048577)
                    if not line or len(line) > 1048576:
                        break
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict) and record.get('type') == 'system' and record.get('subtype') == 'init':
                        model = record.get('model')
                        if isinstance(model, str) and re.fullmatch(r'[\w.:-]+', model):
                            return model, 'live stream init'
                        break
        except (OSError, UnicodeError):
            pass
    return 'UNAVAILABLE', 'no matching launch record or stream model'


def build_proxy_snapshot(tmux_text: str, ps_text: str, now: datetime, completions: list[dict]) -> dict:
    """px-* windows only. ps_text is one process table, optional; commands are not kept."""
    windows = []
    for line in tmux_text.splitlines():
        parts = line.split("\t", 5)
        if len(parts) not in (5, 6):
            continue
        session, name, activity, dead, pid = parts[:5]
        start = parts[5] if len(parts) == 6 else ''
        if not name.startswith("px-") or session not in {"0", "proxies"}:
            continue
        try:
            windows.append((name, int(activity), dead == "1", int(pid), start))
        except ValueError:
            continue
    processes = {}
    for line in ps_text.splitlines():
        match = re.match(r"^\s*(\d+)\s+(\d+)\s+(\S+)\s+(.*)$", line)
        if not match:
            continue
        pid_s, ppid_s, age, command = match.groups()
        pid, ppid = int(pid_s), int(ppid_s)
        processes[pid] = (ppid, age, command)
    # A proxy's named Claude can be reparented outside the tmux pane. Match the
    # launch identity across the complete process table, never by pane ancestry.
    named_processes: dict[str, list[str]] = {}
    for _ppid, _age, command in processes.values():
        if not re.search(r"(?:^|/)claude(?:\s|$)", command):
            continue
        try:
            argv = shlex.split(command)
            if '--name' in argv:
                peer = argv[argv.index('--name') + 1]
                named_processes.setdefault(peer, []).append(command)
        except (ValueError, IndexError):
            continue
    rows = []
    for name, activity, dead, pid, start in windows:
        try:
            age_min = _elapsed_minutes(processes[pid][1]) if pid in processes else None
        except ValueError:
            age_min = None
        metadata = _proxy_runner_metadata(start, now, dead)
        model = metadata.get('model', 'unknown')
        peer = metadata.get('launch_peer')
        matches = named_processes.get(peer, [])
        if not dead and peer and len(matches) == 1:
            metadata['current_peer'] = peer
            found = re.search(r"(?:^|\s)--model(?:=|\s+)(\S+)", matches[0])
            if found:
                model = found.group(1)
        if not dead:
            model, model_source = _live_proxy_model(name, pid, age_min, processes, now)
            metadata['model_source'] = model_source
            metadata.update(_proxy_display_metadata(name, start, pid, processes, now))
        last = max(0, int((now.timestamp() - activity) // 60))
        stale = (not dead) and last >= PROXY_STALE_ACTIVITY_MIN
        rows.append({
            **metadata, "name": name, "model": model, "age": age_min, "last_activity": last,
            "dead": dead, "stale": stale, "state": "dead" if dead else "stale" if stale else "running",
        })
    rows.sort(key=lambda row: (row["dead"], row["age"] is None, -(row["age"] or 0), row["name"]))
    return {"rows": rows, "completions": completions, "truncated": False}


def _proxy_cache_load(path: Path, now: datetime) -> dict | None:
    try:
        doc = json.loads(path.read_text())
        age = now.timestamp() - float(doc["collected_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not 0 <= age <= PROXY_CACHE_MAX_S or not isinstance(doc.get("rows"), list):
        return None
    return {
        "rows": doc["rows"],
        "completions": doc.get("completions") if isinstance(doc.get("completions"), list) else [],
        "truncated": bool(doc.get("truncated")),
        "cache_age_min": int(age // 60),
    }


def _proxy_cache_save(path: Path, snapshot: dict, now: datetime) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "collected_at": now.timestamp(),
            "rows": snapshot.get("rows") or [],
            "completions": snapshot.get("completions") or [],
            "truncated": bool(snapshot.get("truncated")),
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        return


def _proxy_completions(now: datetime) -> tuple[list[dict], bool]:
    db = Path.home() / ".local/share/master-of-panes/data/mop.db"
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)) as conn:
        entries = conn.execute(
            "SELECT payload FROM events WHERE event_type='send_command' AND timestamp >= ? AND payload LIKE '%PROXY_STOP%' ORDER BY id DESC LIMIT 200",
            ((now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S"),),
        ).fetchall()
    completed = []
    for (payload,) in entries:
        command = json.loads(payload).get("command", "")
        match = re.match(r"PROXY_STOP name=([A-Za-z0-9_.-]+) rc=([0-9]+)\s+out=\S+\s+(RESULT\b[^\n]*)", command)
        if match:
            completed.append({"name": match.group(1), "rc": match.group(2), "result_available": True})
        else:
            match = re.match(r"PROXY_STOP name=([A-Za-z0-9_.-]+) rc=([0-9]+)\s", command)
            if match:
                completed.append({"name": match.group(1), "rc": match.group(2), "result": "unknown"})
    return completed, len(entries) == 200


def proxy_work_history(rows: list[dict], now: datetime, path: Path = PM_OPS_DB) -> None:
    """Existing custody records only; process uptime never establishes work age."""
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
            records = conn.execute(
                "SELECT issue, payload_json, ts FROM events WHERE source='proxy-agent' "
                "AND target_type='proxy_assignment' ORDER BY id DESC LIMIT 1001"
            ).fetchall()
        # A truncated history cannot certify a complete original work clock.
        complete = len(records) <= 1000
        by_name = {}
        for issue, raw, timestamp in records[:1000]:
            payload = json.loads(raw)
            name = payload.get('proxy')
            if isinstance(name, str) and name not in by_name:
                by_name[name] = (issue, payload, timestamp)
        for row in rows:
            # Never guess the native peer from its lossy tmux display label.
            peer = row.get('current_peer')
            digest = row.get('brief_sha256')
            born = row.get('launch_birth')
            if not peer or not digest or not isinstance(born, (int, float)):
                continue
            record = by_name.get(peer)
            if not record:
                continue
            issue, payload, timestamp = record
            recorded = _parse_ts(timestamp)
            # Native receipt timestamps have second precision. Same-second or
            # older records cannot prove correspondence to this launch.
            if not recorded or not born < recorded.timestamp() <= now.timestamp():
                continue
            if payload.get('brief_sha256') != digest:
                continue
            ref = payload.get('ref', '')
            if not isinstance(ref, str) or not re.fullmatch(r'(issue|pr):[1-9][0-9]*', ref):
                continue
            if ref not in row.get('brief_refs', []):
                continue
            row['work_ref'] = ref
            row['work_issue'] = issue
            head = payload.get('head_sha')
            if isinstance(head, str) and re.fullmatch(r'[0-9a-fA-F]{40}', head):
                row['work_head'] = head
            for field in ('stage_started_at', 'work_started_at', 'last_progress_at'):
                start = _parse_ts(payload.get(field))
                if start and start <= now and (field != 'work_started_at' or complete):
                    row[field + '_minutes'] = int((now - start).total_seconds() // 60)
            for field in ('boundary', 'next_checkpoint'):
                if isinstance(payload.get(field), str):
                    row[field] = _compact(payload[field], 100)
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError):
        return  # Unavailable metadata stays UNKNOWN; never substitute process age.


def _proxy_live_processes() -> dict[str, list[tuple[int, int]]] | None:
    """Cross-check named Claude processes against live PID sockets, without reading logs."""
    try:
        result = subprocess.run(['ps', '-ww', '-axo', 'pid,etime,command'],
                                capture_output=True, text=True, timeout=3, check=True)
        live = {}
        for line in result.stdout.splitlines():
            match = re.match(r'^\s*(\d+)\s+(\S+)\s+(.*)$', line)
            if not match:
                continue
            pid, elapsed, command = match.groups()
            try:
                executable = command.split(maxsplit=1)[0]
                names = re.findall(r'(?:^|\s)--name(?:=|\s+)([\w.-]+)(?=\s|$)', command)
                if Path(executable).name != 'claude' or len(names) != 1 or not (Path('/tmp/cc-socks') / (pid + '.sock')).is_socket():
                    continue
                age = _elapsed_minutes(elapsed)
                live.setdefault(names[0], []).append((int(pid), age))
            except (ValueError, IndexError, OSError):
                continue
        return live
    except (OSError, subprocess.SubprocessError):
        return None


def _proxy_live_panes() -> dict[str, set[int]] | None:
    """Return pane roots for each live proxy window."""
    try:
        result = subprocess.run(['tmux', 'list-panes', '-a', '-F',
                                 '#{session_name}\t#{window_name}\t#{pane_dead}\t#{pane_pid}'],
                                capture_output=True, text=True, timeout=3, check=True)
        names = {}
        for line in result.stdout.splitlines():
            parts = line.split('\t')
            if len(parts) != 4:
                return None
            session, window, dead, pid = parts
            if dead not in {'0', '1'} or not pid.isdigit():
                return None
            if dead == '0' and window.startswith('px-'):
                names.setdefault(window[3:], set()).add(int(pid))
        return names
    except (OSError, subprocess.SubprocessError):
        return None


def _proxy_ancestry() -> dict[int, int] | None:
    try:
        result = subprocess.run(['ps', '-axo', 'pid,ppid'], capture_output=True, text=True, timeout=3, check=True)
        parents = {}
        for line in result.stdout.splitlines()[1:]:
            fields = line.split()
            if len(fields) != 2 or not all(field.isdigit() for field in fields):
                return None
            parents[int(fields[0])] = int(fields[1])
        return parents
    except (OSError, subprocess.SubprocessError):
        return None


def _pane_owns_pid(pid: int, roots: set[int], parents: dict[int, int]) -> bool:
    seen = set()
    while pid not in seen and pid in parents:
        if pid in roots:
            return True
        seen.add(pid)
        pid = parents[pid]
    return False


def _proxy_pid_token_matches(pid: int, token: object) -> bool:
    if not isinstance(token, str) or not re.fullmatch(r'[\w.-]+', token):
        return False
    try:
        result = subprocess.run(['ps', '-Eww', '-p', str(pid), '-o', 'command='],
                                capture_output=True, text=True, timeout=3, check=True)
        tokens = re.findall(r'(?:^|\s)PROXY_AGENT_RUN_TOKEN=([^\s]+)', result.stdout)
        return tokens == [token]
    except (OSError, subprocess.SubprocessError):
        return False


def _proxy_run_binding(name: str, token: object, started: datetime | None) -> bool:
    """Require the ledger run to own the matching active lock and manifest."""
    if not re.fullmatch(r'px-[\w.-]+', name) or not isinstance(token, str) or not token or started is None:
        return False
    base = Path.home() / '.local/state/proxy-agent-runs' / name
    try:
        lock_token = (base / 'active' / 'token').read_text()
        manifest = json.loads((base / 'manifest.json').read_text())
        return (isinstance(manifest, dict) and lock_token == token and manifest.get('token') == token
                and manifest.get('name') == name and _parse_ts(manifest.get('started_at')) == started
                and 'finished_at' not in manifest and 'rc' not in manifest)
    except (OSError, ValueError, TypeError):
        return False


def _read_proxies(now: datetime, path: Path | None = None, liveness: dict | None = None, binding=None, panes: dict | None = None, ancestry: dict | None = None, pid_binding=None) -> dict | None:
    """Read proxy runs from the PM ledger; socket/process data is only a cross-check."""
    path = path or PM_OPS_DB
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as conn:
            conn.row_factory = sqlite3.Row
            records = conn.execute("SELECT target_id, issue, pr, evidence_json "
                                   "FROM obligations WHERE kind='proxy_run' AND status='open'").fetchall()
    except (OSError, sqlite3.Error):
        return None
    rows = []
    candidates_by_row = []
    liveness = _proxy_live_processes() if liveness is None else liveness
    panes = _proxy_live_panes() if panes is None else panes
    ancestry = _proxy_ancestry() if ancestry is None else ancestry
    if liveness is None or panes is None or ancestry is None:
        return None  # Failed collection is unavailable, not evidence of termination.
    binding = binding or _proxy_run_binding
    pid_binding = pid_binding or _proxy_pid_token_matches
    for record in records:
        try:
            evidence = json.loads(record['evidence_json'] or '{}')
        except (TypeError, ValueError):
            evidence = {}
        if not isinstance(evidence, dict):
            evidence = {}
        if 'finished_at' in evidence or 'rc' in evidence:
            continue  # Terminal evidence overrides stale OPEN status, even for rc=0.
        name = str(record['target_id'] or '')
        model = evidence.get('model')
        if not isinstance(model, str) or not re.fullmatch(r'[\w.:-]+', model):
            model = '?'
        started = _parse_ts(evidence.get('started_at')) if isinstance(evidence.get('started_at'), str) else None
        minutes = int((now - started).total_seconds() // 60) if started and started <= now else None
        refs = [str(value) for value in (record['issue'], record['pr']) if value is not None and str(value).isdigit()]
        candidates = []
        if minutes is not None and binding(name, evidence.get('token'), started):
            candidates = [pid for pid, age in liveness.get(name, [])
                          if -1 <= minutes - age <= 3 and _pane_owns_pid(pid, panes.get(name, set()), ancestry)
                          and pid_binding(pid, evidence.get('token'))]
        candidates_by_row.append(candidates)
        rows.append({'name': name, 'state': 'uncertain', 'model': model,
                     'runtime_minutes': minutes, 'model_source': 'model missing in PM ledger',
                     'display_refs': list(dict.fromkeys(refs))})
    # A PID may certify one and only one row; duplicate tokens/rows and shared
    # candidate processes are ambiguous, even when each row has one candidate.
    claims = {}
    for candidates in candidates_by_row:
        for pid in set(candidates):
            claims[pid] = claims.get(pid, 0) + 1
    for row, candidates in zip(rows, candidates_by_row):
        if len(candidates) == 1 and claims[candidates[0]] == 1:
            row['state'] = 'running'
    return {'rows': rows, 'completions': [], 'truncated': False}


def read_proxies(now: datetime, cache_path: Path | None = None, collect=None, budget: float | None = None) -> dict | None:
    # Do not reuse the legacy process/log cache: only the PM ledger is authoritative.
    limit = COLLECTOR_TIMEOUTS["proxies"] if budget is None else budget
    result: list = []

    def run():
        result.append((collect or _read_proxies)(now))

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(limit)
    if not worker.is_alive() and result and result[0] is not None:
        return result[0]
    if worker.is_alive():
        return {"budget_exhausted": True}
    return result[0] if result else None


def proxies_block(doc: dict | None) -> list[str]:
    if doc is None:
        return ["*Proxies:* unavailable"]
    if doc.get("budget_exhausted"):
        return ["*Proxies:* unavailable (collector budget)"]
    # The report contains live proxy obligations only.
    visible = [r for r in doc["rows"] if r.get("state") not in {'finished', 'dead', 'dead window', 'left-over window'} and not r.get('dead')]

    def _dead(row: dict) -> bool:
        return bool(row.get("dead") or row.get("state") in {"dead", "dead window"})

    live = [r for r in visible if r.get('state') != 'uncertain']
    uncertain = sum(r.get('state') == 'uncertain' for r in visible)
    stale_note = f" (stale, {doc['cache_age_min']}m old)" if doc.get("cache_age_min") is not None else ""
    model_counts = {}
    for row in live:
        model = row.get('model') or 'unknown'
        model_counts[model] = model_counts.get(model, 0) + 1
    counts = ', '.join(f'{model}: {count}' for model, count in sorted(model_counts.items())) or 'none'
    uncertain_note = f' · {uncertain} liveness ?' if uncertain else ''
    lines = [f"*Proxies:* {len(live)} live ({counts}){uncertain_note}{stale_note}"]
    rendered = []
    def runtime(row):
        return row.get('runtime_minutes') if row.get('runtime_minutes') is not None else row.get('age')
    visible.sort(key=lambda row: (_dead(row), runtime(row) is None, -(runtime(row) or 0), row.get('name', '')))
    for row in visible:
        if not _dead(row) and row.get('state') != 'left-over window':
            name = row['name'].removeprefix('px-') if row['name'].startswith('px-px-') else row['name']
            model = row.get('model') or 'UNAVAILABLE'
            if model == 'UNAVAILABLE':
                model += f" ({row.get('model_source', 'no model evidence')})"
            minutes = runtime(row)
            duration = f'{minutes // 60}h{minutes % 60:02d}m' if isinstance(minutes, int) and minutes >= 0 else '?'
            refs = row.get('display_refs') or [ref.split(':', 1)[1] for ref in (row.get('brief_refs') or []) if re.fullmatch(r'(?:issue|pr):\d+', ref)]
            details = ' · ' + (', '.join('#' + str(ref) for ref in refs) if refs else '?')
            state = f'running {duration}' if row.get('state') != 'uncertain' else f'? · elapsed {duration}'
            rendered.append(f'• {name} · {model} · {state}{details}')
            continue
        if row.get("state") in {"dead window", "left-over window"} or "age" not in row and "last_activity" not in row:
            rendered.append(f"• {row['name']} — {row.get('state')}")
            continue
        age = f"{row['age']}m" if row.get("age") is not None else "unknown"
        last = row.get("last_activity", row.get("idle"))
        last_s = f"{last}m" if last is not None else "unknown"
        flags = []
        if row.get("dead") or row.get("state") == "dead":
            flags.append("dead")
        elif row.get("stale") or row.get("state") == "STALE":
            flags.append("stale")
        flag = f" · {' · '.join(flags)}" if flags else ""
        def elapsed(field):
            value = row.get(field + '_minutes')
            return f'{value // 60}h{value % 60:02d}m' if isinstance(value, int) and value >= 0 else 'UNKNOWN'
        refs = row.get('brief_refs') or ([row['work_ref']] if row.get('work_ref') else [])
        ref = ', '.join(refs) if refs else 'issue/PR UNKNOWN'
        output = row.get('output_age')
        output_s = f'{output}m' if output is not None else 'UNKNOWN'
        name = row['name'].removeprefix('px-') if row['name'].startswith('px-px-') else row['name']
        model = row.get('model', 'UNAVAILABLE')
        if model == 'UNAVAILABLE':
            model += f" ({row.get('model_source', 'no model evidence')})"
        result = f" · RESULT {'AVAILABLE' if row.get('result_available') is True else 'UNKNOWN'}" if _dead(row) else ''
        rendered.append(f"• {ref} @{row.get('work_head', 'UNKNOWN')} · {model} {name} · this stage {elapsed('stage_started_at')} · total {elapsed('work_started_at')} · last progress {elapsed('last_progress_at')} ago · boundary {row.get('boundary', 'UNKNOWN')} · next {row.get('next_checkpoint', 'UNKNOWN')} · process age {age} · output age {output_s} · activity {last_s}{flag}{result}")
    # Parent compaction may abbreviate this block; saved input/full block retains
    # every row for the existing hourly inspector rather than dropping row 25+.
    lines.extend(rendered)
    return lines


# Rajiv C0ALZJHGE49 thread 1794184422.765479, ts 1794186319.204389: the heartbeat
# tracks these four accounts only. Antigravity/Gemini/Grok stay in the proxy quota
# cache; they are not part of this report.
USAGE_ACCOUNTS = (
    ("Claude", "dev@scribie.com"),
    ("Claude", "rajiv@scribie.com"),
    ("Codex", "rajiv@scribie.com"),
    ("Codex", "rpoddar@gmail.com"),
)


def account_usage_lines(raw: list[str] | None, missing_reason: str = "not reported") -> list[str]:
    """Exactly one line per Claude/Codex account. Keeps a real weekly reading, else that account's UNAVAILABLE line."""
    lines = [u.strip() for u in (raw or []) if u and u.strip()]
    out = []
    for kind, email in USAGE_ACCOUNTS:
        prefix = re.compile(rf"^(WARN\s+)?{re.escape(kind)}\s+{re.escape(email)}\b")
        matched = [ln for ln in lines if prefix.match(ln) and "fable" not in ln.lower()]
        good = [ln for ln in matched if "unavailable" not in ln.lower()]
        weekly = [ln for ln in good if re.search(r"\bweekly:", ln)]
        if weekly or good:
            out.append((weekly or good)[0])
            continue
        bad = [ln for ln in matched if "unavailable" in ln.lower()]
        out.append(bad[0] if bad else f"{kind} {email}: UNAVAILABLE ({missing_reason})")
    return out


def _usage_tldr(lines: list[str]) -> str:
    """ok only when all four account lines are real readings. WARN stays the low-quota prefix."""
    bad = []
    for line in lines:
        if "unavailable" in line.lower():
            bad.append(re.sub(r"^WARN\s+", "", line).split(":", 1)[0].strip())
    if bad:
        return "usage UNAVAILABLE: " + ", ".join(bad)
    warns = [line for line in lines if line.startswith("WARN")]
    if warns:
        def _used(line: str) -> float:
            match = re.search(r"(\d+(?:\.\d+)?)% used", line)
            return float(match.group(1)) if match else -1.0
        head = max(warns, key=_used)
        acct = re.sub(r"^WARN\s+", "", head).split(":", 1)[0]
        eta = re.search(r"ETA 100% in (\S+(?: \S+)?) \(BEFORE reset\)", head)
        if eta:
            return f"usage WARN: {acct} projects 100% in {eta.group(1)} before reset"
        return f"usage WARN: {acct} at or above 80% used"
    return "usage ok"


def compact_report(lines: list[str], limit: int, cadence: str | None = None) -> str:
    """Bound physical lines without discarding sections or unavailable evidence.

    The named-action overflow behavior belongs only to the 3h report; 1h keeps
    its historical compaction contract.
    """
    rows = [row for line in lines for row in line.splitlines() if row.strip()]
    # Fold item runs into their section, retaining counts and overflow evidence.
    while len(rows) > limit:
        runs = []
        in_proxies = False
        for i, row in enumerate(rows):
            if row.startswith('*Proxies:'):
                in_proxies = True
            elif row.startswith('*') or row.startswith('_Ops:_'):
                in_proxies = False
            if not in_proxies and row.lstrip().startswith(("•", "- ", "  ")) and i:
                start = i
                while start > 0 and rows[start - 1].lstrip().startswith(("•", "- ")):
                    start -= 1
                runs.append((i - start + 1, start, i + 1))
        if not runs:
            # Never merge named sections; retain all evidence if sections alone
            # exceed the content-line budget.
            break
        _, start, end = max(runs)
        items = rows[start:end]
        unavailable = [item for item in items if "UNAVAILABLE" in item]
        # Error-code rows carry routing instructions. Fold them together without
        # replacing named actions with a generic "+N more" that loses ownership.
        actionable = [item for item in items if re.search(r"• [A-Za-z0-9_]+ — \d+ ev", item)]
        top = items if cadence == "3h" else (unavailable or items[:1])
        more = len(items) - len(top)
        summary = f" ({len(items)} items): " + "; ".join(top)
        if more:
            summary += f"; +{more} more"
        rows[start - 1:end] = [rows[start - 1] + summary]
    # The cap counts content lines, not the restored section separators.
    spaced = []
    for row in rows:
        if spaced and (row.startswith("*") or row.startswith("_Ops:_")):
            spaced.append("")
        spaced.append(row)
    if cadence == '3h':
        # Keep every named folded route visible in Slack. Prefer extra physical
        # lines over cap_line's lossy '+K more' when a folded section is long.
        expanded = []
        for row in spaced:
            while len(row) > MAX_LINE_CHARS - LINE_NOTE_RESERVE:
                boundary = row.rfind('; ', 0, MAX_LINE_CHARS - LINE_NOTE_RESERVE)
                if boundary < 1:
                    boundary = MAX_LINE_CHARS - LINE_NOTE_RESERVE - 20
                expanded.append(row[:boundary])
                row = '↳ ' + row[boundary:].lstrip('; ')
            expanded.append(row)
        spaced = expanded
    return "\n".join(spaced)


def composer_release_sha(current: Path | None = None, source: Path | None = None) -> str:
    """Bind the executing composer bytes to the current release's shared manifest."""
    current = current or Path.home() / '.local/share/master-of-panes/current'
    source = source or Path(__file__)
    try:
        release = current.resolve(strict=True)
        manifest = json.loads((release / 'RELEASE_MANIFEST.json').read_text())
        sha = manifest.get('commit')
        if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}', sha) or release.name != sha:
            return 'UNVERIFIED'
        shared_path = release / 'scripts/pm/shared-assets/manifest.json'
        shared_bytes = shared_path.read_bytes()
        if hashlib.sha256(shared_bytes).hexdigest() != manifest.get('shared_assets', {}).get('sha256'):
            return 'UNVERIFIED'
        shared = json.loads(shared_bytes)
        entries = [entry for entry in shared.get('entries', [])
                   if entry.get('source_path') == 'claude/scripts/pm/heartbeat/heartbeat-compose.py']
        if len(entries) != 1:
            return 'UNVERIFIED'
        expected = entries[0].get('sha256')
        deployed_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        payload = release / 'scripts/pm/shared-assets/claude/scripts/pm/heartbeat/heartbeat-compose.py'
        if deployed_sha != expected or hashlib.sha256(payload.read_bytes()).hexdigest() != expected:
            return 'UNVERIFIED'
        return sha if current.resolve(strict=True) == release else 'UNVERIFIED'
    except (OSError, ValueError, TypeError, AttributeError):
        return 'UNVERIFIED'


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
    unblockable: list[str] | None = None,
    tracked: list[str] | None = None,
    priority_flags: list[str] | None = None,
    priority_unavailable: str | None = None,
    remote_boxes: str | None = None,
    pending_actions: tuple[dict, dict] | None = None,
    proxies: dict | None = None,
    usage_error: str | None = None,
    slots_authoritative: bool = True,
) -> str:
    """Pure: build the Slack mrkdwn message."""
    window = "3h" if mode == "3h" else "1h"
    stamp = now.astimezone(IST).strftime("%H:%M IST")
    err_lines, err_tldr, _ = error_block(axiom, mapping or {}, window)
    pr_lines, pr_tldr, merge_asks = pr_block(prs, MAX_PRS)
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
        ul = account_usage_lines(usage_lines, missing_reason=usage_error or "not reported")
        body += ["", "*Usage (Claude + Codex):*", *["• " + u for u in ul]]
        parts.append(_usage_tldr(ul))
        # Email digest lives only in the 3h heartbeat; the DM is removed (Rajiv 2026-10-09 12:06 IST,
        # DM D0AMF0XE6TS 1791527801.053979). Shared channel: sender + subject only, previews stripped.
        body += ["", *email_block(email_doc)]
    else:
        parts.append(pr_tldr)
        body += ["", *pr_lines]
    if mode == "1h":
        body += ["", remote_boxes or "*Remote boxes:* unavailable (collector not run)"]
    if tracked:
        # Rajiv 2026-10-27 DM D0AMF0XE6TS thread 1793097714.304129: tracked features right after Slots.
        body += ["", *tracked]
    if mode == "1h":
        # Training has its own thread and is not part of the hourly report.
        if unblockable:
            body += ["", *unblockable]
        body += ["", agents_line(agents)]
    # Exactly one empty spacer before this shared 1h/3h section.
    body += ["", *decision_block(decisions, now)]
    if mode == "1h":
        body += ["", *proxies_block(proxies)]
        ledger, github = pending_actions if pending_actions is not None else ({'error': 'collector not run'}, {'error': 'collector not run'})
        body += ["", *pending_actions_block(ledger, github, now)]

    asks = []
    if merge_asks:
        asks.append("merge ask: " + ", ".join(merge_asks))
    for esc in escalations or []:
        asks.append(f"escalation: {esc}")
    tldr = "; ".join(parts) + "."
    if mode == "3h":
        first = f"*Heartbeat {stamp}* — TL;DR: {tldr}"
    else:
        mention = f"{CTO} " if asks else ""
        first = f"{mention}*Hourly — {stamp}* — TL;DR: {tldr}"
    lines = [first, *body]
    if asks:
        lines += ["", "*Asks:* " + " | ".join(asks)]
    pm_actions = [] if mode == "3h" else [a for r in (prs or []) for a in r.get("actions") or []]
    if pm_actions:
        lines += ["", "*ACTIONS (PM):* " + "; ".join(pm_actions)]
    if cleanup_alert:
        lines.append(f"*ALERT:* {cleanup_alert}")
    if failures:
        timed_out = [f for f in failures if "UNAVAILABLE (timeout " in f]
        shown = timed_out + [f for f in failures if f not in timed_out]
        lines.append(f"_Ops:_ {len(failures)} failed — " + "; ".join(f[:100] for f in shown[:4]))
    report = cap_lines(compact_report(lines, 23 if mode == "3h" else 20, cadence=mode),
                       MAX_LINE_CHARS - LINE_NOTE_RESERVE)
    if mode == "3h":
        report = '\n'.join(line for line in report.splitlines() if line.strip()).replace(CTO, '')
        return report + f"\n_MoP composer release: {composer_release_sha()}_"
    return report + f"\n\n_MoP composer release: {composer_release_sha()}_"



# ---------------------------------------------------------------------------
# Unblockable now (Rajiv 2026-10-25, C0ALZJHGE49 ts 1792910836.320109):
# "we need to unblock the dependencies, not fix the labels."
# ---------------------------------------------------------------------------
UNBLOCK_REPO = "heydonna-app/heydonna-app"
UNBLOCK_CAP = 10
_BLOCKER_LINE = re.compile(r"(?im)^[^\n]*?(?:\bblockers?\s*:|\bblocked on\b|\bunblock_condition\b)[^\n]*$")
# Rajiv 2026-10-25 (C0ALZJHGE49 ts 1792914885.859829): tighten. Only a strict `blockers:` line counts.
_STRICT_BLOCKERS = re.compile(r"(?im)^[ \t>*\-]*\**blockers?\**\s*:(?P<rest>[^\n]*)$")
_WAKE_LINE = re.compile(r"(?im)^[ \t>*\-]*\**(?:wake(?:[ _]condition)?|unblock[_ ]condition)\**\s*[:=](?P<rest>[^\n]*)$")
_CLAIM_TYPE = re.compile(r"claimable_slot_type\s*[:=]\s*`?([\w-]+)")
_FILLER = {"pr", "prs", "issue", "issues", "and", "or", "merged", "closed", "resolved", "to", "main", "the", "is", "are", "on", "child", "parent"}
UNBLOCK_FRESH_DAYS = 14


def _pure_issue_refs(text: str) -> bool:
    """True when text is only #N refs plus filler words (no Rajiv/CTO/recurrence/bench/deploy etc.)."""
    rest = re.sub(r"#\d+", " ", text or "")
    words = re.findall(r"[A-Za-z_]+", rest)
    return all(w.lower() in _FILLER for w in words)


def _blocker_refs(body: str) -> list[int] | None:
    """Refs from the strict `blockers:` line; None when absent or the line carries anything but issue refs."""
    refs: list[int] = []
    seen = False
    for m in _STRICT_BLOCKERS.finditer(body or ""):
        seen = True
        if not _pure_issue_refs(m.group("rest")):
            return None
        for n in re.findall(r"#(\d+)", m.group("rest")):
            if int(n) not in refs:
                refs.append(int(n))
    return refs if seen and refs else None


def _has_event_wake(body: str) -> bool:
    return any(not _pure_issue_refs(m.group("rest")) for m in _WAKE_LINE.finditer(body or ""))


def _issue_priority(labels: list[str], body: str) -> int:
    for lab in labels:
        m = re.fullmatch(r"(?:priority:\s*)?[Pp](\d)", lab.strip())
        if m:
            return int(m.group(1))
    m = re.search(r"(?m)^priority:\s*P(\d)", body or "")
    return int(m.group(1)) if m else 9


def unblockable_items(issues: list[dict], resolve, today: datetime | None = None) -> list[dict]:
    """Pure: open backlog/blocked issues that are claimable, whose strict `blockers:` line cites ONLY
    issues/PRs that are ALL closed/merged, with no non-issue wake condition, and resolved <=14d ago (or P0/P1).
    resolve(n) -> {"resolved": bool, "date": "YYYY-MM-DD"} | None."""
    today = today or datetime.now(timezone.utc)
    out = []
    for it in issues:
        labs = [l["name"] if isinstance(l, dict) else l for l in it.get("labels", [])]
        if not any(l in ("status:backlog", "status:blocked") or l.startswith("pm-blocked:") for l in labs):
            continue
        if any(l.startswith("pm-nonclaimable") or "tracking" in l.lower() for l in labs):
            continue
        body = it.get("body") or ""
        types = _CLAIM_TYPE.findall(body)
        if types and types[-1].lower() == "none":
            continue
        if re.search(r"TRACKING_ONLY", body):
            continue
        refs = [r for r in (_blocker_refs(body) or []) if r != it["number"]]
        if not refs or _has_event_wake(body):
            continue
        res = [resolve(r) for r in refs]
        if not all(r and r.get("resolved") for r in res):
            continue
        date = max(r.get("date", "?") for r in res)
        prio = _issue_priority(labs, body)
        try:
            age = (today - datetime.fromisoformat(date).replace(tzinfo=timezone.utc)).days
        except ValueError:
            age = 0
        if age > UNBLOCK_FRESH_DAYS and prio > 1:
            continue
        out.append({"issue": it["number"], "prio": prio, "blocker": refs[-1] if len(refs) == 1 else ",".join(f"#{r}" for r in refs).lstrip("#"), "date": date})
    out.sort(key=lambda x: (x["prio"], -x["issue"]))
    return out


def unblockable_block(failures: list[str], issues: list[dict] | None = None, resolve=None) -> list[str]:
    try:
        if issues is None:
            r = subprocess.run(["gh", "issue", "list", "--repo", UNBLOCK_REPO, "--state", "open", "--limit", "500",
                                "--json", "number,labels,body"], capture_output=True, text=True, errors="replace", timeout=120)
            if r.returncode != 0:
                raise RuntimeError(r.stderr.strip()[:80] or f"rc={r.returncode}")
            issues = json.loads(r.stdout)
        cache: dict[int, dict | None] = {}

        def _resolve(n: int):
            if n not in cache:
                try:
                    q = subprocess.run(["gh", "api", f"repos/{UNBLOCK_REPO}/issues/{n}"], capture_output=True, text=True, errors="replace", timeout=30)
                    d = json.loads(q.stdout) if q.returncode == 0 else {}
                except Exception:
                    d = {}
                if d.get("state") != "closed":
                    cache[n] = None
                else:
                    pr = d.get("pull_request")
                    if pr is not None and not pr.get("merged_at"):
                        cache[n] = None  # closed unmerged PR covers nothing
                    else:
                        cache[n] = {"resolved": True, "date": ((pr or {}).get("merged_at") or d.get("closed_at") or "?")[:10]}
            return cache[n]

        items = unblockable_items(issues, resolve or _resolve)
    except Exception as exc:
        failures.append(f"unblockable: {type(exc).__name__}")
        return ["*Unblockable now:*", "• unavailable"]
    lines = ["*Unblockable now:*"]
    if not items:
        return lines + ["• None."]
    for it in items[:UNBLOCK_CAP]:
        lines.append(f"• #{it['issue']} P{it['prio'] if it['prio'] != 9 else '?'}, blocker #{it['blocker']} resolved {it['date']}")
    if len(items) > UNBLOCK_CAP:
        lines.append(f"• +{len(items) - UNBLOCK_CAP} more")
    return lines

# ---------------------------------------------------------------------------
# Live collection
# ---------------------------------------------------------------------------


def _run_json(cmd: list[str], failures: list[str], label: str, timeout: int = 20):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        failures.append(f"{label}: UNAVAILABLE (timeout {timeout}s)")
        return None
    if proc.returncode != 0:
        failures.append(f"{label}: rc={proc.returncode} {proc.stderr.strip()[-120:]}")
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        failures.append(f"{label}: invalid JSON")
        return None


def lane_event_actions(slots_doc, authoritative=True):
    if not authoritative or not isinstance(slots_doc, dict):
        return []
    return [f"{'HIGH ' if e.get('severity') == 'high' else ''}{e.get('threshold')} slot {e.get('slot')} issue #{e.get('issue')} PR {e.get('pr') or 'none'} age {e.get('age_minutes')}m commits {e.get('commit_count') if e.get('commit_count') is not None else 'unknown'}"
            for e in slots_doc.get('lane_events', []) if isinstance(e, dict)]


def structured_pm_actions(prs, slot_actions, slots, mode, slots_authoritative):
    return [a for r in (prs or []) for a in r.get("actions") or []] if mode == "1h" else []


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


def _priority_inversion_worker(selector_path: str, slots_doc: dict | list | None, timeout: int) -> list[str]:
    spec = importlib.util.spec_from_file_location("ready_pool_select", selector_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("priority selector unavailable")
    rps = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rps)
    slots = slots_doc.get("slots", []) if isinstance(slots_doc, dict) else (slots_doc or [])
    # Bound each dependency read as well as the worker's outer deadline.
    graphql_http = rps.graphql_http

    def bounded_graphql(*args, **kwargs):
        kwargs['timeout'] = min(kwargs.get('timeout', 20), 20)
        return graphql_http(*args, **kwargs)

    rps.graphql_http = bounded_graphql
    issues = rps.load_open_issues(timeout=min(timeout, 60))
    prio = {int(i["number"]): rps.priority_rank({l["name"] for l in i.get("labels") or []}, i.get("body") or "")
            for i in issues}
    # Match the selector's batched native-dependency path; without this shared
    # cache ranking falls back to one REST subprocess per candidate.
    cache = {}
    rps.prefetch_native(rps.native_candidates(issues, []), cache, persist=False)
    return rps.priority_inversions(slots, prio, rps.rank_candidates(issues, cache=cache))


def priority_inversion_flags(slots_doc, failures: list[str], timeout: int = COLLECTOR_TIMEOUTS["priority-inversion"],
                             selector_path: Path | None = None) -> list[str] | None:
    """Run load and dependency ranking in a killable process under one wall-clock deadline."""
    import signal

    selector_path = selector_path or Path.home() / ".claude/scripts/ready_pool_select.py"
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--priority-inversion-worker", str(selector_path), str(timeout)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    slots = slots_doc.get("slots", []) if isinstance(slots_doc, dict) else (slots_doc or [])
    try:
        stdout, _ = proc.communicate(input=json.dumps(slots), timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError("priority worker failed")
        result = json.loads(stdout)
        if not isinstance(result, list) or any(not isinstance(flag, str) for flag in result):
            raise ValueError("invalid priority worker response")
        return result
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        failures.append(f"priority-inversion: UNAVAILABLE (timeout {timeout}s; ready_pool_select.load_open_issues/rank_candidates GraphQL candidate and dependency lookups)")
        return None
    except Exception as exc:  # noqa: BLE001 - heartbeat must still post
        failures.append(f"priority-inversion: {type(exc).__name__}")
        return None


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


def collector_success(value) -> bool:
    """Never refresh last-good timestamps with a sentinel or a partial failure."""
    if value is None:
        return False
    if isinstance(value, dict):
        return (not value.get("error") and not value.get("failures")
                and not value.get("cached") and not value.get("incomplete")
                and value.get("cache_age_min") is None)
    if isinstance(value, str):
        return not any(token in value.lower() for token in ("unavailable", "unknown", "(as of ", "(stale,"))
    return True


def store_observation(path, value, at):
    """Serialize compare-and-replace so older invocations cannot overwrite newer data."""
    import fcntl
    import tempfile
    if not collector_success(value):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            current = json.loads(path.read_text())
            if current["at"] >= at:
                return
        except (OSError, ValueError, KeyError, TypeError):
            pass
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
            json.dump({"at": at, "value": value}, f)
            tmp = f.name
        os.replace(tmp, path)


def parallel_collect(jobs, wall_cap=COMPOSE_COLLECTOR_BUDGET, cache_dir=HOURLY_CACHE):
    """Daemon workers cannot extend the report deadline; only the coordinator caches.

    Jobs must bound their own subprocess calls. Late results are never cached or
    rendered, and a cached value's timestamp is never advanced on a failed probe.
    """
    import queue
    import tempfile
    import time
    inbox = queue.Queue()
    deadline = monotonic() + wall_cap
    results, stale = {}, {}
    observed = {}

    def work(name, collect):
        try:
            value = collect()
            inbox.put((name, value, None, time.time()))
        except Exception as exc:
            inbox.put((name, None, "timed out" if isinstance(exc, subprocess.TimeoutExpired) or "timeout" in str(exc).lower() or "timed out" in str(exc).lower() else type(exc).__name__, time.time()))

    for name, collect in jobs.items():
        threading.Thread(target=work, args=(name, collect), daemon=True).start()
    pending = set(jobs)
    while pending:
        try:
            name, value, error, at = inbox.get(timeout=max(0, deadline - monotonic()))
        except queue.Empty:
            break
        pending.remove(name)
        results[name] = (value, error)
        observed[name] = at
    for name in jobs:
        value, error = results.get(name, (None, "timed out"))
        path = cache_dir / (name + ".json")
        if name in {'mac-box', 'proxies'}:
            # Proxy observations must never fall back to legacy MoP/log caches.
            # Individual mac-box sources own their own caches.
            results[name] = value
            continue
        if collector_success(value):
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                store_observation(path, value, observed[name])
            except OSError:
                pass  # A cache-write failure must not hide a good live result.
            results[name] = value
            continue
        reason = error or ("timed out" if "timeout" in str(value).lower() else "failed")
        try:
            cached = json.loads(path.read_text())
            age = time.time() - cached["at"]
            if not 0 <= age < CACHE_MAX_AGE or not collector_success(cached["value"]):
                raise ValueError("expired/invalid cache")
            value = cached["value"]
            at = datetime.fromtimestamp(cached["at"], IST).strftime("%H:%M")
            stale[name] = f" (as of {at}, live check {reason}; age {int(age // 60)}m)"
        except (OSError, ValueError, KeyError, TypeError):
            value = None
        results[name] = value
    return results, stale


def collect_hourly(args, py, hours, now, failures):
    """Independent network probes start together; dependencies remain in their worker."""
    def json_job(name, cmd):
        def run():
            errors = []
            value = _run_json(cmd, errors, name, COLLECTOR_TIMEOUTS[name])
            if errors:
                failures.extend(errors)
                raise RuntimeError("; ".join(errors))
            return value
        return run

    def slots_job():
        return _load(args.slots_json) if args.slots_json else _fetch_slots()

    def priority_job():
        slots = slots_job()
        return priority_inversion_flags(slots, [], timeout=COLLECTOR_TIMEOUTS["priority-inversion"]) if slots is not None else None

    jobs = {
        "axiom": json_job("axiom", [py, str(AXIOM_REPORT), "--hours", hours, "--errors-by-code", "--compare"]),
        "prs": json_job("pr-open-snapshot", [py, str(SCRIPTS / "pr-open-snapshot.py")]),
        "decisions": lambda: _load(args.decisions_json) if args.decisions_json else read_decisions(PM_OPS_DB, now),
        "agents": lambda: _load(args.agents_json) if args.agents_json else read_agents(PM_PROJECT, now),
        "remote-boxes": lambda: remote_boxes_line(timeout=COLLECTOR_TIMEOUTS["remote-boxes"]),
        "mac-box": mac_box_stats,
        "pending-github": lambda: pending_github(timeout=COLLECTOR_TIMEOUTS["pending-github"]),
        "proxies": lambda: read_proxies(now),
    }
    for key, path in (("axiom", args.axiom_json), ("prs", args.prs_json)):
        if path:
            jobs[key] = lambda path=path: _load(path)
    if not args.no_active_users:
        jobs["active"] = (lambda: _load(args.active_json)) if args.active_json else json_job("active-users", [py, str(AXIOM_REPORT), "--hours", hours, "--active-users"])
    # Error mapping depends only on Axiom, not on any of the slow GitHub probes.
    axiom_job = jobs["axiom"]
    ready = threading.Event()
    axiom_result = [None]
    def axiom_with_signal():
        try:
            axiom_result[0] = axiom_job()
            return axiom_result[0]
        finally:
            ready.set()
    jobs["axiom"] = axiom_with_signal
    def mapping_job():
        if args.map_json:
            return _load(args.map_json)
        if not ready.wait(COLLECTOR_TIMEOUTS["axiom"]):
            return None
        if axiom_result[0] is None:
            return None
        rows = axiom_result[0].get("top", [])
        codes = [a for row in rows for a in ("--code", row["code"])]
        if any(r.get("code") == "save_failed_client_exception" for r in axiom_result[0].get("all_codes", [])) and "save_failed_client_exception" not in codes:
            codes += ["--code", "save_failed_client_exception"]
        return json_job("error-map", [py, str(SCRIPTS / "heartbeat-error-map.py"), *codes])() if codes else {}
    jobs["map"] = mapping_job
    return parallel_collect(jobs, cache_dir=HOURLY_CACHE / args.mode)


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
    hourly, stale = collect_hourly(args, sys.executable, "1", now, failures) if args.mode == "1h" else ({}, {})
    decisions = hourly.get("decisions") if args.mode == "1h" else (_load(args.decisions_json) if args.decisions_json else read_decisions(PM_OPS_DB, now))
    agents = None
    if args.mode == "1h":
        agents = hourly.get("agents")
        if agents is None:
            failures.append("agents: transcripts unavailable")
    if decisions is None:
        failures.append("decisions: ledger unavailable")
    hours = "3" if args.mode == "3h" else "1"
    py = sys.executable
    axiom = hourly.get("axiom") if args.mode == "1h" else _load(args.axiom_json)
    if args.mode != "1h" and axiom is None and not args.axiom_json:
        axiom = _run_json(
            [py, str(AXIOM_REPORT), "--hours", hours, "--errors-by-code", "--compare"],
            failures, "axiom", COLLECTOR_TIMEOUTS["axiom"],
        )
    active = None
    if not args.no_active_users:
        active = hourly.get("active") if args.mode == "1h" else _load(args.active_json)
        if args.mode != "1h" and active is None and not args.active_json:
            active = _run_json([py, str(AXIOM_REPORT), "--hours", hours, "--active-users"], failures, "active-users", COLLECTOR_TIMEOUTS["active-users"])
    slots = None
    release_seen = None
    mapping_doc = hourly.get("map") if args.mode == "1h" else _load(args.map_json)
    if args.mode != "1h" and mapping_doc is None and axiom:
        codes = three_hour_map_args(axiom)
        if any(r.get("code") == "save_failed_client_exception" for r in axiom.get("all_codes", [])) and "save_failed_client_exception" not in codes:
            codes += ["--code", "save_failed_client_exception"]
        mapping_doc = _run_json([py, str(SCRIPTS / "heartbeat-error-map.py"), *codes], failures, "error-map", COLLECTOR_TIMEOUTS["error-map"]) if codes else {}
    mapping_doc = mapping_doc or {}
    failures += mapping_doc.get("failures", [])
    support = None
    if args.mode == "3h":
        support = _load(args.support_json)
        if support is None and not args.support_json:
            cmd = [py, str(SCRIPTS / "support-inbox-snapshot.py"), "--hours", hours]
            if args.no_in_app:
                cmd.append("--no-in-app")
            support = _run_json(cmd, failures, "support", COLLECTOR_TIMEOUTS["support"])
        if support:
            failures += support.get("failures", [])
    prs = hourly.get("prs") if args.mode == "1h" else _load(args.prs_json)
    if args.mode != "1h" and prs is None and not args.prs_json:
        prs = _run_json([py, str(SCRIPTS / "pr-open-snapshot.py")], failures, "pr-open-snapshot", COLLECTOR_TIMEOUTS["pr-open-snapshot"])
        # The snapshot already supplies direct PR actions. Routine PR progress
        # does not create a second queue in the obligations ledger.

    priority_flags = []
    # Historical status is useful; historical authorization is not.
    if "prs" in stale:
        prs = [dict(r, actions=[], next="live head recheck required") for r in prs]
    pm_actions = structured_pm_actions(prs, [], None, args.mode, False)

    if args.save_inputs:
        out = Path(args.save_inputs)
        out.mkdir(parents=True, exist_ok=True)
        for name, value in (("axiom", axiom), ("map", mapping_doc), ("support", support), ("prs", prs), ("active", active), ("slots", slots), ("actions", pm_actions), ("decisions", decisions), ("agents", agents)):
            (out / f"{name}.json").write_text(json.dumps(value, indent=2))

    usage_lines = None
    usage_error = None
    if args.mode == "3h":
        # Read stdout even on timeout. A killed snapshot has often already printed
        # the account lines; discarding them is what turned a late helper into
        # "usage Claude UNAVAILABLE" with no Codex lines.
        proc = subprocess.Popen(
            [py, str(Path.home() / ".claude/scripts/usage-snapshot.py")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace",
        )
        try:
            out, _err = proc.communicate(timeout=COLLECTOR_TIMEOUTS["usage"])
            usage_lines = [ln for ln in (out or "").splitlines() if ln.strip()]
            if proc.returncode != 0 and not usage_lines:
                usage_error = f"usage-snapshot rc={proc.returncode}"
                failures.append(f"usage: {usage_error}")
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _err = proc.communicate()
            usage_lines = [ln for ln in (out or "").splitlines() if ln.strip()]
            usage_error = f"usage-snapshot timed out after {COLLECTOR_TIMEOUTS['usage']}s"
            failures.append(f"usage: {usage_error}")
        except Exception as exc:
            usage_error = f"usage-snapshot {type(exc).__name__}"
            failures.append(f"usage: {usage_error}")
            try:
                proc.kill()
            except OSError:
                pass
    email_doc = None
    if args.mode == "3h":
        try:
            ep = subprocess.run([py, str(Path(__file__).with_name("email-digest.py")), "--hours", "3", "--json"], capture_output=True, text=True, errors="replace", timeout=COLLECTOR_TIMEOUTS["email"])
            email_doc = json.loads(ep.stdout)
        except Exception as exc:
            failures.append(f"email: {type(exc).__name__}")
    # Tracked features and issue lifecycle moved to their own thread replies
    # (tracked-features-report.py / issue-lifecycle-report.py, invoked by slack-send.sh
    # after the Hourly parent, like pr-status-report.py). Rajiv C0ALZJHGE49
    # thread 1793116023.781399, ts 1793118979.539329.
    unblockable = None
    tracked = None
    langsmith = None  # LangSmith ingest dropped from the hourly (Rajiv ts 1792806414.519859)
    report = compose(
        args.mode, datetime.now(timezone.utc), axiom, mapping_doc.get("mapping", {}), prs,
        support=support, failures=failures, escalations=args.escalation,
        active=active, include_active=not args.no_active_users,
        slots=slots, include_slots=not args.no_slots, release_seen=release_seen,
        slots_authoritative=not ({"slots", "priority"} & stale.keys()),
        usage_lines=usage_lines, usage_error=usage_error, cleanup_alert=cleanup_status_alert(),
        email_doc=email_doc, decisions=decisions, agents=agents, langsmith=langsmith, unblockable=unblockable, tracked=tracked,
        priority_flags=priority_flags,
        priority_unavailable=(f"UNAVAILABLE (timeout {COLLECTOR_TIMEOUTS['priority-inversion']}s)" if priority_flags is None else None),
        remote_boxes=((hourly.get("remote-boxes") or "*Remote boxes:* UNAVAILABLE (no recent successful probe)")
                      + "\n" + (hourly.get("mac-box") or {}).get("line", "• Mac test box: UNKNOWN (collector deadline)")) if args.mode == "1h" else None,
        pending_actions=(pending_obligations(PM_OPS_DB, now), hourly.get("pending-github") or {"error": "UNAVAILABLE (no recent successful probe)"}) if args.mode == "1h" else None,
        proxies=hourly.get("proxies") if args.mode == "1h" else None,
    )
    labels = {"axiom": "*Axiom errors", "active": "*Active users", "prs": "open non-draft PRs",
              "slots": "*Slots", "priority": "*Slots",
              "remote-boxes": "*Remote boxes:", "pending-github": "*Pending PM actions:",
              "agents": "*Agents", "decisions": "*Decisions pending", "proxies": "*Proxies",
              "map": "*Axiom errors"}
    lines = report.splitlines()
    for name, note in stale.items():
        for i, line in enumerate(lines):
            if labels.get(name, "\0") in line and not line.startswith("*Hourly"):
                lines[i] += note
                break
    print(cap_lines("\n".join(lines)))
    if pm_actions:
        print("ACTIONS:" + json.dumps(pm_actions), file=sys.stderr)
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--priority-inversion-worker":
        try:
            worker_slots = json.load(sys.stdin)
            result = _priority_inversion_worker(sys.argv[2], worker_slots, float(sys.argv[3]))
            print(json.dumps(result))
            raise SystemExit(0)
        except Exception as exc:
            print(f"priority worker failed: {type(exc).__name__}", file=sys.stderr)
            raise SystemExit(1)
    sys.exit(main())

