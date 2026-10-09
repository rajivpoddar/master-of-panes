#!/usr/bin/env python3
"""Rajiv email digest for the 3h heartbeat (Rajiv DM D0AMF0XE6TS 1791446658.671909).
Privacy boundary (CTO REVISE, C0ALZJHGE49 1791527549.296399): output carries ONLY a display name and a
redacted subject per row. Snippets are used for attention/sensitivity filtering and are NEVER emitted;
raw sender addresses are NEVER emitted (missing display name -> "(unknown sender)").
--json prints {"status","hours","total","items":[{"name","subject"}]} for heartbeat-compose.py.
Fail-open: any gws failure yields status "unavailable" (or the text line) and exits 0.
Read-only (gws gmail +triage / messages.get metadata). Bodies are never fetched."""
import argparse, json, re, subprocess, sys
from email.utils import parseaddr

URGENT = re.compile(r"urgent|asap|action required|immediately|deadline|overdue|past due|final notice|payment (failed|declined)|invoice|outage|down\b|security alert|expires?|reply|response needed|waiting on you|\?\s*$", re.I)
SENSITIVE = re.compile(r"(otp|one[- ]time|verification code|verify your|passcode|password reset|reset (your )?password|sign[- ]in code|login code|2fa|security code)", re.I)
AUTOMATED = re.compile(r"(no-?reply|do-?not-?reply|notifications?@|alerts?@|mailer|bounce|billing@|support@|team@|news(letter)?@|hello@|info@|updates?@|marketing@|@.*\.(mailchimp|sendgrid|hubspot)|calendar-notification|github\.com|googlegroups)", re.I)
UNKNOWN_SENDER = "(unknown sender)"
EMAIL_ADDR = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
REDACT = [
    (EMAIL_ADDR, "[address]"),
    (re.compile(r"https?://\S+"), "[link]"),
    (re.compile(r"\b\d{6,}\b"), "[num]"),
    (re.compile(r"\b(?:\d[ -]?){9,}\d\b"), "[num]"),
]

def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip().splitlines()[-1][:120] if (p.stderr or p.stdout).strip() else f"rc={p.returncode}")
    if p.stdout.lstrip().startswith("No messages found"):
        return {"messages": []}
    return json.loads(p.stdout)

def clean(s, n):
    s = re.sub(r"[\u200b-\u200f\u034f\u00ad\u2060\ufeff]", "", s)
    for rx, rep in REDACT:
        s = rx.sub(rep, s)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"

def display_name(from_header):
    """Display name only; never the address. Name-shaped-like-an-address also maps to the placeholder."""
    name, _addr = parseaddr(from_header or "")
    if not name.strip() or "@" in name:
        return UNKNOWN_SENDER
    return clean(name, 28) or UNKNOWN_SENDER

def collect(msgs, fetch_snippet):
    """Structured rows: [{"name","subject"}]. Snippet only filters, it is never returned."""
    rows, seen = [], set()
    for m in msgs:
        _name, addr = parseaddr(m.get("from", ""))
        subj = m.get("subject") or "(no subject)"
        key = (addr.lower(), subj.strip().lower())
        if key in seen:
            continue
        seen.add(key)
        gist = ""
        try:
            gist = fetch_snippet(m) or ""
        except Exception:
            pass
        if SENSITIVE.search(subj) or SENSITIVE.search(gist):
            continue
        human = not AUTOMATED.search(addr or "")
        if not (human or URGENT.search(subj) or URGENT.search(gist)):
            continue
        rows.append({"name": display_name(m.get("from", "")), "subject": clean(subj, 70)})
    return rows

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=3)
    ap.add_argument("--max", type=int, default=8)
    ap.add_argument("--json", action="store_true", help="structured name+subject rows for heartbeat-compose")
    ap.add_argument("--whoami", action="store_true", help="print account email only")
    a = ap.parse_args()
    try:
        if a.whoami:
            print(run(["gws", "gmail", "users", "getProfile", "--params", '{"userId":"me"}'])["emailAddress"]); return 0
        q = f"newer_than:{a.hours}h is:unread -category:promotions -category:social -category:forums"
        data = run(["gws", "gmail", "+triage", "--max", "50", "--query", q, "--format", "json"])
        msgs = data.get("messages", []) if isinstance(data, dict) else data
    except Exception as e:
        if a.json:
            print(json.dumps({"status": "unavailable", "hours": a.hours, "total": 0, "items": []})); return 0
        print(f"email digest unavailable: {clean(str(e), 120)}"); return 0
    # Rajiv 2026-10-08 (DM 1791447006.047629): "only for unread messages, only the ones that need my attention".
    # Attention = a real person wrote (not an automated sender) or the mail asks for action.
    def snippet(m):
        return run(["gws", "gmail", "users", "messages", "get", "--params", json.dumps({"userId": "me", "id": m["id"], "format": "metadata"})]).get("snippet", "")
    rows = collect(msgs, snippet)
    if a.json:
        print(json.dumps({"status": "ok", "hours": a.hours, "total": len(rows), "items": rows[: a.max]})); return 0
    if not rows:
        print(f"*Email digest (last {a.hours}h):* nothing unread needs your attention."); return 0
    shown = [f"• {r['name']} — {r['subject']}" for r in rows[: a.max]]
    more = f"\n_+{len(rows) - a.max} more not shown_" if len(rows) > a.max else ""
    print(f"*Email digest (last {a.hours}h):* {len(rows)} unread need your attention\n" + "\n".join(shown) + more)
    return 0

if __name__ == "__main__":
    sys.exit(main())
