#!/usr/bin/env python3
"""HeyDonna lifecycle-email drip selector (deterministic, no LLM).

Source: Rajiv, C0ALZJHGE49 thread 1793076476.707989 (design ts 1793086314.202589,
OK ts 1793086430.314129). Consumer: Skill(lifecycle-email-processing) /
agent lifecycle-email-agent, which personalizes and sends.

Decides WHO gets WHICH touch and emits ACTIVITY FACTS. It never sends mail.

Subcommands:
  select [--dry-run] [--now ISO]      emit JSON {records, skipped, meta}
  record-sent --user-id U --email E --touch T --gmail-id G
  opt-out --email E [--reason TEXT]   add to opt-out list (e.g. a "stop" reply)

Reads prod Convex beloved-cardinal-941 READ-ONLY via `npx convex data`, Axiom
(heydonna-logs) via AXIOM_ENV_FILE, and Gmail sent mail (gws) for recent manual
contact. State: ~/.claude/state/lifecycle-drip.jsonl (append-only; send-once per
(userId, touch)), opt-outs: ~/.claude/state/lifecycle-drip-optout.jsonl.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(os.environ.get("HEYDONNA_REPO", "/Users/rajiv/Downloads/projects/heydonna-app"))
DEPLOYMENT = "beloved-cardinal-941"  # PROD, read-only
STATE = Path.home() / ".claude/state/lifecycle-drip.jsonl"
OPTOUT = Path.home() / ".claude/state/lifecycle-drip-optout.jsonl"
TEMPLATES = Path.home() / ".claude/skills/lifecycle-email-processing/templates"
AXIOM_ENV_FILE = Path(os.environ.get("AXIOM_ENV_FILE", str(REPO / ".env.local")))
ET = ZoneInfo("America/New_York")
TOUCHES = ["FIRST_FILE_READY", "D0", "D1", "D3", "D7"]  # priority order, one touch per user per run
INTERNAL_DOMAINS = ("@heydonna.law", "@scribie.com")
MANUAL_CONTACT_DAYS = 3
COHORT_DAYS = 30
DAY_MS = 86_400_000


# ---------------------------------------------------------------- sources
def convex(table: str, limit: int) -> list[dict]:
    cmd = ["npx", "convex", "data", table, "--deployment", DEPLOYMENT,
           "--format", "jsonl", "--limit", str(limit)]
    env = {k: v for k, v in os.environ.items() if k != "CONVEX_DEPLOY_KEY"}
    out = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise SystemExit(f"convex data {table} failed rc={out.returncode}: {out.stderr[:400]}")
    return [json.loads(l) for l in out.stdout.splitlines() if l.startswith("{")]


def axiom_token() -> str:
    for k in ("AXIOM_API_TOKEN", "AXIOM_QUERY_TOKEN"):
        if os.environ.get(k):
            return os.environ[k]
    found = {}
    if AXIOM_ENV_FILE.exists():
        for line in AXIOM_ENV_FILE.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() in ("AXIOM_API_TOKEN", "AXIOM_QUERY_TOKEN") and v.strip():
                found[k.strip()] = v.strip().strip('"')
    for k in ("AXIOM_API_TOKEN", "AXIOM_QUERY_TOKEN"):
        if k in found:
            return found[k]
    raise SystemExit("Axiom query token not found (env or AXIOM_ENV_FILE)")


def axiom(apl: str, start: datetime, end: datetime) -> list[dict]:
    body = json.dumps({"apl": apl, "startTime": start.isoformat().replace("+00:00", "Z"),
                       "endTime": end.isoformat().replace("+00:00", "Z")}).encode()
    req = urllib.request.Request("https://api.axiom.co/v1/datasets/_apl?format=legacy", data=body,
                                 headers={"Authorization": f"Bearer {axiom_token()}",
                                          "Content-Type": "application/json"})
    d = json.load(urllib.request.urlopen(req, timeout=180))
    rows = []
    for b in d.get("buckets", {}).get("totals") or []:
        row = dict(b["group"])
        row["_aggs"] = [agg["value"] for agg in b["aggregations"]]
        rows.append(row)
    return rows


APP_PROD = "['heydonna-logs'] | where ['source']=='app' and ['heydonna_env']=='production'"
KEY_ACTIONS = ("ai_proofread_complete", "ai_format_complete", "ai_audit_complete",
               "sample_deposition_started", "sample_deposition_cloned", "export_complete",
               "upload_complete", "onboarding_started", "onboarding_skipped")


def axiom_activity(user_ids: list[str], now: datetime) -> tuple[dict, dict]:
    """Returns (last_seen_iso by user, {user: {action: [fileIds]}})."""
    if not user_ids:
        return {}, {}
    start = now - timedelta(days=COHORT_DAYS + 1)
    ids = ", ".join(json.dumps(u) for u in user_ids)
    seen = axiom(f"{APP_PROD} and ['userId'] in ({ids}) | summarize max(_time) by ['userId']", start, now)
    acts = axiom(f"{APP_PROD} and ['userId'] in ({ids}) and ['action'] in ({', '.join(json.dumps(a) for a in KEY_ACTIONS)})"
                 " | summarize count() by ['userId'], ['action'], ['fileId']", start, now)
    def _iso(v):
        if isinstance(v, (int, float)):
            v = v / 1e6 if v > 1e15 else (v if v > 1e12 else v * 1000)  # ns / ms / s -> ms
            return datetime.fromtimestamp(v / 1000, timezone.utc).isoformat()
        return v
    last_seen = {r["userId"]: _iso(r["_aggs"][0]) for r in seen}
    actions: dict[str, dict[str, list[str]]] = {}
    for r in acts:
        actions.setdefault(r["userId"], {}).setdefault(r["action"], []).append(r.get("fileId") or "none")
    return last_seen, actions


def gmail_recent_manual_recipients(now: datetime) -> set[str]:
    """Addresses rajiv@ emailed (outside this drip) in the last MANUAL_CONTACT_DAYS."""
    q = f"in:sent newer_than:{MANUAL_CONTACT_DAYS}d"
    out = subprocess.run(["gws", "gmail", "users", "messages", "list", "--params",
                          json.dumps({"userId": "me", "q": q, "maxResults": 200})],
                         capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise SystemExit(f"gws list sent failed: {out.stderr[:300]}")
    drip_ids = {r.get("gmail_id") for r in read_jsonl(STATE)}
    rcpts: set[str] = set()
    for m in json.loads(out.stdout or "{}").get("messages", []):
        if m["id"] in drip_ids:
            continue
        g = subprocess.run(["gws", "gmail", "users", "messages", "get", "--params",
                            json.dumps({"userId": "me", "id": m["id"], "format": "full"})],
                           capture_output=True, text=True, timeout=60)
        if g.returncode != 0:
            continue
        for h in json.loads(g.stdout).get("payload", {}).get("headers", []):
            if h.get("name", "").lower() not in ("to", "cc", "bcc"):
                continue
            rcpts.update(e.lower() for e in re.findall(r"[\w.+'-]+@[\w.-]+", h.get("value", "")))
    return rcpts


# ---------------------------------------------------------------- state
def read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def append_jsonl(p: Path, row: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(row) + "\n")


def template_status() -> dict[str, str]:
    st = {}
    for t in TOUCHES:
        f = TEMPLATES / f"{t}.txt"
        if not f.exists():
            st[t] = "MISSING"
            continue
        m = re.search(r"^STATUS:\s*(\S+)", f.read_text(), re.M)
        st[t] = m.group(1) if m else "MISSING_STATUS"
    return st


# ---------------------------------------------------------------- facts
def ms(iso: str | None) -> float | None:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000 if iso else None


def credit_balance(entries: list[dict]) -> tuple[float, float]:
    """Port of convex/creditsLedger.ts deriveExactBalance (float). Returns (granted, available)."""
    settled_ops = {e.get("linkedOperationId") for e in entries if e["kind"] == "settle" and e.get("linkedOperationId")}
    released_ops = {e.get("linkedOperationId") for e in entries if e["kind"] == "release" and e.get("linkedOperationId")}
    avail = granted = 0.0
    for e in entries:
        amt = float(e.get("amountCredits") or 0)
        if e["kind"] == "grant":
            granted += amt
        if e.get("identifiedVersion") == 1 and e["kind"] in ("reserve", "release"):
            avail += amt
        elif e.get("identifiedVersion") == 1 and e["kind"] == "settle":
            pass
        elif e["kind"] == "reserve":
            if e.get("operationId") in settled_ops:
                continue
            avail += amt
        else:
            if e["kind"] == "release" and e.get("linkedOperationId") in settled_ops:
                continue
            avail += amt
    _ = released_ops
    return granted, avail


def signup_source(u: dict) -> str:
    a = u.get("signupAttribution") or {}
    src = a.get("source") or (a.get("utm") or {}).get("utm_source") or ("referral" if a.get("ref") else "")
    if not src and a.get("referrer"):
        src = a["referrer"]
    return src or ("unknown" if not a else "direct")


def first_name(u: dict) -> str:
    n = (u.get("name") or "").strip()
    return n.split()[0].capitalize() if n and "@" not in n else ""


# ---------------------------------------------------------------- select
def select(now: datetime, dry_run: bool) -> dict:
    now_ms = now.timestamp() * 1000
    state = read_jsonl(STATE)
    sent = {(r["userId"], r["touch"]) for r in state if r.get("event") == "sent"}
    runs = [r for r in state if r.get("event") == "run" and not r.get("dry_run")]
    last_run_ms = max((ms(r["ts"]) for r in runs), default=now_ms - 2 * DAY_MS)
    optouts = {r["email"].lower() for r in read_jsonl(OPTOUT)}

    users = [u for u in convex("users", 2000) if now_ms - u["_creationTime"] <= COHORT_DAYS * DAY_MS]
    uids = {u["userId"] for u in users}
    projects = [p for p in convex("projects", 3000) if p.get("userId") in uids]
    transcripts = [t for t in convex("transcripts", 3000) if t.get("userId") in uids]
    ledger = [e for e in convex("creditLedgerEntries", 8000) if e.get("accountUserId") in uids]
    workspaces = {w["_id"] for w in convex("workspaces", 2000) if w.get("externalOrgId") == "scribie"}
    scribie_members = {m["userId"] for m in convex("workspaceMembers", 5000)
                       if m.get("workspaceId") in workspaces and m.get("status") == "active"}
    last_seen, actions = axiom_activity(sorted(uids), now)
    manual = gmail_recent_manual_recipients(now)
    tstatus = template_status()

    sample_pids = {p["_id"] for p in projects if p.get("sampleSeedProjectId")}
    proj_by_id = {p["_id"]: p for p in projects}
    records, skipped = [], []

    for u in sorted(users, key=lambda x: x["_creationTime"]):
        uid, email = u["userId"], (u.get("email") or "").lower()
        src = signup_source(u)
        a = u.get("signupAttribution") or {}
        led = [e for e in ledger if e["accountUserId"] == uid]
        abuse = any(e["kind"] == "adjustment" and str(e.get("reason", "")).lower().startswith("abuse:") for e in led)
        reason = None
        if email.endswith(INTERNAL_DOMAINS):
            reason = "internal_account"
        elif uid in scribie_members or u.get("cohort") == "scribie" or re.search(
                r"scribie\.(com|ai)", " ".join([src, a.get("referrer", ""), a.get("landingPath", "")]), re.I):
            reason = "scribie_cohort"
        elif abuse:
            reason = "abuse_flagged"
        elif email in optouts:
            reason = "opted_out"

        own = sorted([t for t in transcripts if t["userId"] == uid and t.get("projectId") not in sample_pids],
                     key=lambda t: t["_creationTime"])
        own_ready = [t for t in own if t.get("lastAutoProcessCompletedAt")]
        uacts = actions.get(uid, {})
        sample_actions = sorted({act for act, fids in uacts.items()
                                 if act.startswith(("ai_", "sample_", "export"))
                                 and (act.startswith("sample_") or any(
                                     f in {t["_id"] for t in transcripts if t.get("projectId") in sample_pids} for f in fids))})
        touched_sample = bool(sample_actions) or any(p["userId"] == uid for p in projects if p["_id"] in sample_pids)
        days = (now_ms - u["_creationTime"]) / DAY_MS
        seen_ms = ms(last_seen.get(uid))
        inactive_days = (now_ms - (seen_ms or u["_creationTime"])) / DAY_MS

        # Eligible touches (rules from the approved design)
        elig = []
        first_ready = own_ready[0] if own_ready else None
        if first_ready and first_ready["lastAutoProcessCompletedAt"] > last_run_ms:
            elig.append("FIRST_FILE_READY")
        if days < 1:
            elig.append("D0")
        if 1 <= days < 3 and touched_sample and not own:
            elig.append("D1")
        if 3 <= days < 5 and not own:
            elig.append("D3")
        if 7 <= days < 9 and inactive_days >= 5:
            elig.append("D7")
        if not elig:
            continue  # not due for anything today; not a "skip"
        touch = next((t for t in elig if (uid, t) not in sent), None)
        if reason is None and touch is None:
            reason = "already_sent:" + ",".join(elig)
        if reason is None and email in manual:
            reason = f"manual_email_last_{MANUAL_CONTACT_DAYS}d"
        if reason is None and not email:
            reason = "no_email"
        if reason:
            skipped.append({"userId": uid, "email": email, "touch": touch or elig[0], "reason": reason})
            continue

        granted, avail = credit_balance(led)
        files = []
        for t in own:
            stages = set()
            if t.get("autoProcessPipeline") and t.get("lastAutoProcessCompletedAt"):
                stages |= {"transcribe", "speaker_correction", "proofread", "format"}
            elif t.get("status") not in (None, "uploading", "error"):
                stages.add("transcribe")
            for act, st in (("ai_proofread_complete", "proofread"), ("ai_format_complete", "format"),
                            ("ai_audit_complete", "review")):
                if t["_id"] in uacts.get(act, []):
                    stages.add(st)
            if t.get("proofreadStatus") == "completed":
                stages.add("proofread")
            done = t.get("lastAutoProcessCompletedAt")
            files.append({
                "title": t.get("title"),
                "audio_hours": round(t["audioDuration"] / 3600, 1) if t.get("audioDuration") else None,
                "docx_import": bool(t.get("docxImportFileKey")),
                "auto_process": bool(t.get("autoProcessPipeline")),
                "status": t.get("status"),
                "stages_ran": sorted(stages),
                "processing_minutes": round((done - t["_creationTime"]) / 60000) if done else None,
                "template": (proj_by_id.get(t.get("projectId")) or {}).get("templateName"),
                "exported": t["_id"] in uacts.get("export_complete", []),
            })
        ob = u.get("onboarding") or {}
        records.append({
            "userId": uid,
            "email": email,
            "first_name": first_name(u),
            "touch": touch,
            "template_status": tstatus.get(touch),
            "activity_facts": {
                "signup_at": u.get("createdAt"),
                "days_since_signup": round(days, 1),
                "signup_source": src,
                "heard_about": u.get("heardAboutAnswer"),
                "onboarding": {"skipped": bool(ob.get("skippedAt")), "completed_steps": ob.get("completedSteps", []),
                               "current_step": ob.get("currentStep"),
                               "tours_completed": sorted(k for k, v in ob.items() if k.endswith("TourCompleted") and v)},
                "sample": {"touched": touched_sample, "actions": sample_actions},
                "own_files": files,
                "credits": {"granted": round(granted), "used": round(granted - avail), "left": round(avail)},
                "last_seen": last_seen.get(uid),
                "days_inactive": round(inactive_days, 1),
                "template_used": sorted({f["template"] for f in files if f["template"]}),
            },
        })

    weekday = now.astimezone(ET).weekday() < 5
    meta = {"generated_at": now.isoformat(), "dry_run": dry_run, "weekday_et": weekday,
            "last_run": datetime.fromtimestamp(last_run_ms / 1000, timezone.utc).isoformat(),
            "template_status": tstatus,
            "send_allowed": weekday and all(v == "APPROVED" for v in tstatus.values()),
            "cohort_users": len(users)}
    if not dry_run:
        append_jsonl(STATE, {"event": "run", "ts": now.isoformat(), "dry_run": False,
                             "records": len(records), "skipped": len(skipped)})
    return {"meta": meta, "records": records, "skipped": skipped}


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("select")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--now")
    r = sub.add_parser("record-sent")
    for k in ("--user-id", "--email", "--touch", "--gmail-id"):
        r.add_argument(k, required=True)
    o = sub.add_parser("opt-out")
    o.add_argument("--email", required=True)
    o.add_argument("--reason", default="replied stop")
    ap.add_argument("--dry-run", action="store_true", help="shorthand for `select --dry-run`")
    args = ap.parse_args()

    if args.cmd == "record-sent":
        if args.touch not in TOUCHES:
            raise SystemExit(f"unknown touch {args.touch}")
        key = (args.user_id, args.touch)
        if any((x["userId"], x["touch"]) == key for x in read_jsonl(STATE) if x.get("event") == "sent"):
            print(json.dumps({"ok": True, "duplicate": True}))
            return
        append_jsonl(STATE, {"event": "sent", "ts": datetime.now(timezone.utc).isoformat(), "userId": args.user_id,
                             "email": args.email.lower(), "touch": args.touch, "gmail_id": args.gmail_id})
        print(json.dumps({"ok": True}))
        return
    if args.cmd == "opt-out":
        email = args.email.lower()
        if email not in {x["email"] for x in read_jsonl(OPTOUT)}:
            append_jsonl(OPTOUT, {"email": email, "ts": datetime.now(timezone.utc).isoformat(), "reason": args.reason})
        print(json.dumps({"ok": True, "email": email}))
        return
    now = datetime.fromisoformat(args.now) if getattr(args, "now", None) else datetime.now(timezone.utc)
    dry = args.dry_run or getattr(args, "dry_run", False)
    print(json.dumps(select(now, dry), indent=2, default=str))


if __name__ == "__main__":
    main()
