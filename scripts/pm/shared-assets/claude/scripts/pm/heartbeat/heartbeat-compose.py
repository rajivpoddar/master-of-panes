#!/usr/bin/env python3
"""Compose the 3h heartbeat and the 1h hourly post (docs/pm/heartbeat-redesign-2026-09-29.md).

3h (--mode 3h): Axiom errors + support inbox + open PRs. First line ALWAYS
@-mentions CTO (Rajiv 2026-09-29 10:58 IST, thread 1790659381.537339:
"build it. note that it has to have an cto at mention.").

1h (--mode 1h): "Hourly — HH:MM IST", Axiom errors + open PRs. Mentions CTO
ONLY when the post carries a merge ask, an admission ask or a ladder
escalation (Rajiv 11:02 IST same thread).

Both: at most ~20 lines, no tables, "quiet" for empty blocks, housekeeping
collapsed to one `_Ops:_` footer line only when something failed.

The composer never posts to Slack. It prints the message; the calling skill posts.
Inputs are either collected live (default) or given as JSON files (--*-json)
for dry runs and tests.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
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
    lines = [f"*Support ({window_label}):* {len(slack)} Slack thread(s), {len(in_app)} in-app"]
    for t in sorted(needs, key=lambda t: -t["age_min"])[:MAX_SUPPORT]:
        flag = " ⚠" if t["age_min"] >= threshold else ""
        link = f" — linked #{t['links'][0]}" if t.get("links") else ""
        lines.append(f"• {t['channel']} — \"{t['summary'][:50]}\" — NEEDS REPLY ({t['age_min']}m){link}{flag}")
    if len(answered) > 3:
        lines.append(f"• {len(answered)} answered")
    else:
        for t in answered:
            at = datetime.fromtimestamp(t["answered_at"], IST).strftime("%H:%M") if t.get("answered_at") else ""
            link = f" — linked #{t['links'][0]}" if t.get("links") else ""
            lines.append(f"• {t['channel']} — \"{t['summary'][:50]}\" — answered by {t['answered_by']} {at}{link}")
    for f in in_app[:3]:
        state = f"linked #{f['links'][0]}" if f.get("links") else f["state"].replace("_", " ")
        auto = " (auto)" if f.get("auto") else ""
        lines.append(f"• In-app {f['type']}{auto} — \"{f['title'][:50]}\" — {state}")
    if len(in_app) > 3:
        lines.append(f"• +{len(in_app) - 3} more in-app")
    overdue = [t for t in needs if t["age_min"] >= threshold]
    tldr = f"{len(needs)} support thread(s) need a reply" if needs else "support answered"
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
    parts.append(pr_tldr)
    body += ["", *pr_lines]
    slot_actions: list[str] = []
    if include_slots:
        sl_lines, sl_tldr, slot_actions = slot_block(slots, now, release_seen)
        parts.append(sl_tldr)
        body += ["", *sl_lines]

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
    pm_actions = [a for r in (prs or []) for a in r.get("actions") or []] + slot_actions
    if pm_actions:
        lines.append("*ACTIONS (PM):* " + "; ".join(pm_actions))
    if failures:
        lines.append(f"_Ops:_ {len(failures)} failed — " + "; ".join(f[:80] for f in failures[:2]))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Live collection
# ---------------------------------------------------------------------------


def _run_json(cmd: list[str], failures: list[str], label: str, timeout: int = 300):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
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
    parser.add_argument("--no-slots", action="store_true")
    parser.add_argument("--escalation", action="append", default=[], help="ladder-approved escalation line")
    parser.add_argument("--no-in-app", action="store_true")
    parser.add_argument("--save-inputs", help="directory to write collected inputs to")
    args = parser.parse_args()

    failures: list[str] = []
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
                capture_output=True, text=True, timeout=60, check=False,
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
        for name, value in (("axiom", axiom), ("map", mapping_doc), ("support", support), ("prs", prs), ("active", active), ("slots", slots), ("actions", pm_actions)):
            (out / f"{name}.json").write_text(json.dumps(value, indent=2))

    print(compose(
        args.mode, datetime.now(timezone.utc), axiom, mapping_doc.get("mapping", {}), prs,
        support=support, failures=failures, escalations=args.escalation,
        active=active, include_active=not args.no_active_users,
        slots=slots, include_slots=not args.no_slots, release_seen=release_seen,
    ))
    if pm_actions:
        print("ACTIONS:" + json.dumps(pm_actions), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
