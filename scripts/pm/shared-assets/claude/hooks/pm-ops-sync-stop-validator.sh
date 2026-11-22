#!/usr/bin/env bash
# Stop only: drain runnable commitments without touching the ledger or services.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
# Consume the hook payload, never the transcript or a remote status/collector. The read-only
# `--hourly-batch OUT.json` export (used by pm-hourly-post.sh) has no payload on stdin.
if [ "${1:-}" != "--hourly-batch" ]; then cat >/dev/null; fi
exec python3 - "$@" <<'PYEOF'
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time


DEFAULT_DB = ("/Users/rajiv/.claude/projects/"
              "-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db")
INVENTORY = {"ready_pool", "triage_retained_scope"}
NOW = datetime.now(timezone.utc)
DEADLINE = time.monotonic() + 0.75
# `--hourly-batch OUT.json`: read-only export of the runnable HOURLY rows for the hourly wrapper.
EXPORT_PATH = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--hourly-batch" else None

# Two-speed Stop drain (Rajiv D0AMF0XE6TS thread 1795338483.502879, ts 1795352601.197289, "Go").
# FAST rows block Stop every time they are runnable. HOURLY rows NEVER block Stop and Stop never
# lists or hands them off: the once-per-hour wrapper pm-hourly-post.sh asks this file for them with
# `--hourly-batch OUT.json` and posts them in the hourly report (CTO REVISE #2 C0ALZJHGE49
# 1795353473.878109, ts 1795354512.689229: no second batch trigger, and a standing FAST row must not
# keep the routine batch from the hourly lane). Classification uses existing row fields only
# (severity, kind, target_type, evidence), never a list of ids (rule 60). Anything
# unrecognised is FAST, so a new kind can only ever behave as the pre-change hook did.
# A Slack thread is a DESTINATION, not an urgency signal: neither the bare word "slack" in a
# kind nor a slack_thread target makes a row FAST. Urgency comes from the kind's own words
# (reply, due, promised, customer, lockout...), severity, or an unclassified kind.
FAST_SEVERITIES = {"p0", "p1", "critical", "high"}
FAST_KIND = re.compile(
    r"\b(customer|lockout|p0|custody|rajiv|cto|reply|promise|promised|cadence|due|deadline|production|"
    r"billing|security|merge ask|ci failure reconcile|main red|pr red)\b")
FAST_TARGET = re.compile(r"\b(customer|transcript|transcript hash|incident)\b")
HOURLY_KIND = re.compile(
    r"\b(followup|watch|wait|hold|held|parked|parking|queued|backlog|tooling|readback|"
    r"redate|reminder|renewal)\b")
HOURLY_TARGET = re.compile(r"\b(tooling|ops tooling|process|pm config|mop|mop reminder|reminder)\b")
HOURLY_BATCH_ROWS = 8   # routine rows listed in full in the once-per-hour batch
HOURLY_BATCH_IDS = 40   # further routine row ids listed compactly after them


def speed(row):
    """Return "FAST" or "HOURLY". Pure; any doubt or malformed field is FAST."""
    try:
        if str(row.get("severity") or "").strip().lower() in FAST_SEVERITIES:
            return "FAST"
        kind = tokens(row.get("kind"))
        target = tokens(row.get("target_type"))
        if FAST_KIND.search(kind) or FAST_TARGET.search(target):
            return "FAST"
        if kind == "proxy run":
            # A running lane is routine; its ended result (PROXY_STOP seen) is not.
            evidence = json.loads(row.get("evidence_json") or "{}")
            return "FAST" if not isinstance(evidence, dict) or evidence.get("finished_at") else "HOURLY"
        if HOURLY_KIND.search(kind) or HOURLY_TARGET.search(target):
            return "HOURLY"
    except (ValueError, TypeError, AttributeError):
        pass
    return "FAST"


def stamp(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp lacks timezone")
    return parsed.astimezone(timezone.utc)


def compact(value, limit=220):
    return " ".join(str(value or "").split())[:limit]


def tokens(value):
    # Only current custody fields, never historical/quoted issue contracts.
    return re.sub(r"[_/\s-]+", " ", str(value or "").lower())


def obligation_deferred(row, now):
    """Only an explicit future suppression postpones an open obligation."""
    for key in ("suppress_until",):
        value = row[key]
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is not None and parsed.astimezone(timezone.utc) > now:
                return True
        except (ValueError, TypeError, OverflowError):
            continue
    return False


def classify(row):
    if obligation_deferred(row, NOW):
        return "future", "Postponed until its recorded review or suppression time."
    evidence = json.loads(row.get("evidence_json") or "{}")
    if not isinstance(evidence, dict):
        raise ValueError("evidence is not an object")
    review = stamp(row.get("next_review_at"))
    suppress = stamp(row.get("suppress_until"))
    action = compact(row.get("required_action"), 800)
    blocker = compact(row.get("blocker"), 500)
    owner = compact(row.get("owner"))
    custody = " ".join(tokens(evidence.get(k)) for k in
                       ("state", "status", "outcome", "disposition", "delivery", "effect_state"))
    records = [evidence[k] for k in ("receipt", "handoff", "execution")
               if isinstance(evidence.get(k), dict)]
    receipt = next((r for r in records if r.get("status") or r.get("state")),
                   records[0] if records else None)
    if isinstance(receipt, dict):
        custody += " " + tokens(receipt.get("status") or receipt.get("state"))
    custody = custody.replace("root cause unknown", "")
    if ("no retry" in custody or "consumed" in custody or
            re.search(r"\b(unknown|uncertain|ambiguous|unconfirmed)\b", custody) or
            re.search(r"\b(no retry|consumed|unknown consumed|uncertain effect)\b", tokens(action + " " + blocker))):
        return "held", "Protected effect custody: read retained receipts only; no resend, charge or replay."
    if row.get("kind") == "pm-self-clear":
        return "held", "Legacy clear row superseded by the existing MoP save-handoff path."

    # Receipt existence is not correspondence. Bind to this item or to its
    # separately retained native execution IDs; every supplied binding must
    # agree. Missing/foreign acceptance is a safe reconciliation, never resend.
    if isinstance(receipt, dict):
        item = {"obligation_id": row["id"], "issue": row.get("issue"),
                "pr": row.get("pr"), "target_id": row.get("target_id")}
        identity_keys = ("threadId", "clientUserMessageId", "queuedSubmissionId",
                         "submission_id", "run_id", "effect_id", "receipt_id")
        supplied = [k for k in item if receipt.get(k) not in (None, "")]
        same = all(str(receipt[k]) == str(item[k]) for k in supplied)
        bound = bool(supplied) and same
        for recorded in [evidence] + [r for r in records if r is not receipt]:
            expected = [k for k in identity_keys if recorded.get(k) not in (None, "")]
            if not expected:
                continue
            same_item = all(str(recorded[k]) == str(item[k]) for k in item
                            if recorded.get(k) not in (None, ""))
            matches = same_item and all(str(receipt.get(k)) == str(recorded[k]) for k in expected)
            same = same and matches
            bound = bound or matches
        accepted = tokens(receipt.get("status") or receipt.get("state"))
        identity = any(receipt.get(k) for k in
                       ("queuedSubmissionId", "submission_id", "run_id", "effect_id", "receipt_id"))
        native_queued = (accepted == "queued for task consumption" and
                         receipt.get("queueAccepted") is True and
                         receipt.get("executionOwnership") == "queued" and
                         all(isinstance(receipt.get(k), str) and receipt[k].strip()
                             for k in ("threadId", "clientUserMessageId", "queuedSubmissionId")))
        if same and bound and identity and (native_queued or accepted in {"accepted", "queued", "running", "in progress"}):
            return "in_flight", "Accepted execution receipt retained; consume its real terminal, do not duplicate."

    wait = " ".join((blocker, compact(evidence.get("hold_reason")),
                     compact(evidence.get("review_reason")))).lower()
    # Unavailable evidence is not evidence of runnable work or of completion.
    if re.search(r"\b(unavailable|unreachable|lookup.failed|access denied|denied access)\b", wait):
        return "unverified", "Required evidence unavailable: " + compact(wait)
    authority = re.search(r"\b(rajiv|cto|customer|vendor|provider|external)\b", wait)
    waiting = re.search(r"\b(wait|waiting|await|awaiting|approval|authority|permission|response|reply|decision|blocked|requires|required|pending)\b", wait)
    if owner.lower() == "rajiv" or (authority and waiting):
        return "held", "Authority/external wait: " + (blocker or owner)

    future = max((t for t in (review, suppress) if t is not None), default=None)
    review_reason = evidence.get("review_reason") or evidence.get("hold_reason")
    # A trailing review clause must not hide an earlier runnable next step.
    scheduled = re.match(r"\s*(review|observe|monitor|wait|await)\b", action.lower())
    if future and future > NOW and (review_reason or (blocker and scheduled)):
        return "future", f"Review {future.isoformat()}: {blocker or compact(review_reason)}"

    # An unmatched receipt is not an exemption, but must not override a
    # documented wait above or prevent reconciliation from converging to it.
    if isinstance(receipt, dict):
        return "classify", "Reconcile this retained receipt against the current item and recorded native execution IDs. Do not redispatch or replay; if correspondence is unavailable, record that specific wait."

    # Conditional customer/release work is never an unconditional effect.
    # A concrete cached blocker is visible; an unestablished prerequisite is
    # a safe classification step rather than permission to execute the action.
    conditional = re.match(r"\s*(after|once|when|until|on)\b.{0,100}\b(merge|merged|deploy|deployed|complet|approval|receipt|terminal)\w*", action.lower())
    if conditional and blocker.lower() not in {"", "none", "no blocker", "n/a"} and not evidence.get("prerequisite_met"):
        return "held", "Prerequisite retained: " + blocker
    if conditional and evidence.get("prerequisite_met") is not True:
        return "classify", "Establish the stated prerequisite from existing receipts; if unavailable record the specific wait. Do not perform the conditional effect."
    if not action or (blocker and blocker.lower() not in {"none", "no blocker", "n/a"}):
        return "classify", "Reconcile the recorded blocker/readiness using retained evidence, then record the concrete next step or external/review wait. Do not repeat an uncertain effect."
    return "runnable", action


def emit_unavailable(reason):
    if EXPORT_PATH:
        print("ROUTINE_BATCH_UNAVAILABLE " + reason)
        return
    print(json.dumps({"continue": True, "reason":
          "[PM_OBLIGATION_DRAIN_UNAVAILABLE] " + reason +
          "; obligation readiness/count UNKNOWN. Stop is permitted; retain custody and repair the read path."}))


def export_hourly_batch(hourly_runnable, fast_count, path):
    """Write the runnable HOURLY rows for pm-hourly-post.sh. Read-only and atomic; Stop never calls it.

    Same classifier and ordering as the Stop hook (one definition of "routine", rule 60). Bounded:
    HOURLY_BATCH_ROWS rows in full, then HOURLY_BATCH_IDS ids, then a count of the rest. An open FAST
    row does not affect it, so a standing FAST item cannot starve the routine batch across hours.
    """
    rows = [{"id": row["id"], "owner": compact(row.get("owner")) or "PM triage",
             "title": compact(row.get("title"), 100), "step": compact(step, 160)}
            for row, _, step in hourly_runnable[:HOURLY_BATCH_ROWS]]
    ids = [row["id"] for row, _, _ in hourly_runnable[HOURLY_BATCH_ROWS:HOURLY_BATCH_ROWS + HOURLY_BATCH_IDS]]
    payload = {"generated_at": NOW.isoformat(), "fast_runnable": fast_count,
               "hourly_runnable": len(hourly_runnable), "rows": rows, "ids": ids,
               "deferred": len(hourly_runnable) - len(rows) - len(ids)}
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(tmp, path)
    print(f"ROUTINE_BATCH_EXPORTED hourly={len(hourly_runnable)} fast={fast_count} rows={len(rows)} ids={len(ids)}")


def main():
    con = None
    try:
        path = Path(os.environ.get("PM_OPS_DB", DEFAULT_DB)).expanduser().resolve()
        con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.15)
        con.row_factory = sqlite3.Row
        con.set_progress_handler(lambda: int(time.monotonic() > DEADLINE), 500)
        # One consistent read; no status CLI, ingestion, MoP or provider call.
        rows = [dict(r) for r in con.execute(
            "SELECT id, kind, severity, horizon, owner, title, required_action, blocker, "
            "target_type, target_id, issue, pr, next_review_at, suppress_until, evidence_json "
            "FROM obligations WHERE status='open' ORDER BY id")]
    except (OSError, ValueError, sqlite3.Error) as error:
        emit_unavailable(type(error).__name__ + ": " + compact(error))
        return
    finally:
        if con is not None:
            con.close()

    findings = []
    for row in rows:
        if time.monotonic() > DEADLINE:
            emit_unavailable("local decision deadline exceeded")
            return
        if row.get("kind") in INVENTORY:
            continue
        try:
            disposition, step = classify(row)
        except (ValueError, TypeError, OverflowError) as error:
            disposition, step = "unverified", "Malformed local evidence/timing: " + compact(error)
        findings.append((row, disposition, step))
    counts = Counter(d for _, d, _ in findings)
    runnable = [(r, d, s) for r, d, s in findings if d in {"runnable", "classify"}]
    speeds = {r["id"]: speed(r) for r, _, _ in runnable}
    fast_runnable = [x for x in runnable if x[1] == "runnable" and speeds[x[0]["id"]] == "FAST"]
    hourly_runnable = [x for x in runnable if x[1] == "runnable" and speeds[x[0]["id"]] == "HOURLY"]
    # Runnable first, then FAST before HOURLY, then urgent (P0/customer) classify, then severity
    # (CTO REVISE 11-16 ts 1794817176.197419).
    def order(item):
        return (
            0 if item[1] == "runnable" else 1,
            0 if speeds[item[0]["id"]] == "FAST" else 1,
            0 if (str(item[0].get("severity") or "") == "P0" or str(item[0].get("kind") or "") == "p0_custody"
                  or str(item[0].get("kind") or "").startswith("customer_")) else 1,
            {"critical": 0, "high": 1, "medium": 2, "normal": 3}.get(item[0].get("severity"), 4), item[0]["id"])
    # The Stop prompt is scoped to FAST: runnable FAST rows plus classify rows. Routine (HOURLY)
    # runnable rows are never part of it (CTO REVISE 1795353556.350659); the hourly wrapper gets
    # them through --hourly-batch, independent of how many FAST rows are open.
    actionable = sorted((x for x in runnable if not (x[1] == "runnable" and speeds[x[0]["id"]] == "HOURLY")), key=order)
    hourly_runnable.sort(key=order)
    waiting = [(r, d, s) for r, d, s in findings if d not in {"runnable", "classify"}]
    if EXPORT_PATH:
        try:
            export_hourly_batch(hourly_runnable, len(fast_runnable), EXPORT_PATH)
        except OSError as error:
            emit_unavailable(type(error).__name__ + ": " + compact(error))
        return
    parts = [f"open commitments={len(findings)}; " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))]
    parts.append(f"fast-runnable={len(fast_runnable)} hourly-runnable={len(hourly_runnable)} "
                 "(routine rows go to the hourly report batch, never to Stop)")
    for row, disposition, step in actionable[:3]:
        parts.append(f"obligation:{row['id']} [{disposition}] owner={compact(row.get('owner')) or 'PM triage'} "
                     f"{compact(row.get('title'), 100)}: {compact(step, 450)}")
    if len(actionable) > 3:
        parts.append(f"remaining runnable/classification={len(actionable) - 3}")
    for row, disposition, step in waiting[:3]:
        parts.append(f"obligation:{row['id']} [{disposition}] {compact(step, 240)}")
    if len(waiting) > 3:
        parts.append(f"remaining held/future/in-flight/unverified={len(waiting) - 3}")
    parts.append("Inspect remaining rows with pm-ops.py obligation-list --actionable --format json. "
                 "Use original authority/owners and canonical protected writers. No bulk close, unsafe replay or moved-wake waiver.")
    # Rajiv D0AMF0XE6TS ts 1794815313.330999 (11-16): "why has the pm turn not ended for 4h? the stop hook keeps blocking it."
    # Only concrete runnable steps block Stop; classify rows are advisory and are worked at the hourly/brief.
    # Side-agent catch (11-16): P0 and customer classify rows must still block; only routine classify rows are advisory.
    # CTO REVISE 11-16 (ts 1794817176.197419): only executable (runnable) rows block; urgency only orders the advisory list.
    # Two-speed drain: runnable FAST rows block every Stop; an HOURLY-only set always allows Stop.
    if fast_runnable:
        parts.insert(0, "[PM_OBLIGATION_DRAIN] Handle every currently runnable FAST PM step before Stop; one unrelated "
                        "resolution does not waive the rest. Routine (hourly) rows never block Stop and are not asked of this turn.")
        message = " | ".join(parts)
        print(json.dumps({"decision": "block", "message": message, "reason": message}))
    else:
        print(json.dumps({"continue": True, "reason": "[PM_OBLIGATION_DRAIN_STATUS] " + " | ".join(parts)}))


main()
PYEOF
