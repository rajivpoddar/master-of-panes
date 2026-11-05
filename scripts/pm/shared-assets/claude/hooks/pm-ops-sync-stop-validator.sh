#!/usr/bin/env bash
# Stop only: drain runnable commitments without touching the ledger or services.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
# Consume the hook payload, never the transcript or a remote status/collector.
cat >/dev/null
exec python3 - <<'PYEOF'
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import time


DEFAULT_DB = ("/Users/rajiv/.claude/projects/"
              "-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db")
INVENTORY = {"ready_pool", "triage_retained_scope"}
NOW = datetime.now(timezone.utc)
DEADLINE = time.monotonic() + 0.75


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


def classify(row):
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
        return "classify", "Reconcile this retained receipt against the current item and recorded native execution IDs. Do not redispatch or replay; if correspondence is unavailable, record that specific wait."

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
    print(json.dumps({"continue": True, "reason":
          "[PM_OBLIGATION_DRAIN_UNAVAILABLE] " + reason +
          "; obligation readiness/count UNKNOWN. Stop is permitted; retain custody and repair the read path."}))


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
            "target_id, issue, pr, next_review_at, suppress_until, evidence_json "
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
    runnable.sort(key=lambda item: (
        {"critical": 0, "high": 1, "medium": 2, "normal": 3}.get(item[0].get("severity"), 4), item[0]["id"]))
    waiting = [(r, d, s) for r, d, s in findings if d not in {"runnable", "classify"}]
    parts = [f"open commitments={len(findings)}; " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))]
    for row, disposition, step in runnable[:3]:
        parts.append(f"obligation:{row['id']} [{disposition}] owner={compact(row.get('owner')) or 'PM triage'} "
                     f"{compact(row.get('title'), 100)}: {compact(step, 450)}")
    if len(runnable) > 3:
        parts.append(f"remaining runnable/classification={len(runnable) - 3}")
    for row, disposition, step in waiting[:3]:
        parts.append(f"obligation:{row['id']} [{disposition}] {compact(step, 240)}")
    if len(waiting) > 3:
        parts.append(f"remaining held/future/in-flight/unverified={len(waiting) - 3}")
    parts.append("Inspect remaining rows with pm-ops.py obligation-list --actionable --format json. "
                 "Use original authority/owners and canonical protected writers. No bulk close, unsafe replay or moved-wake waiver.")
    if runnable:
        parts.insert(0, "[PM_OBLIGATION_DRAIN] Handle every currently runnable PM step before Stop; one unrelated resolution does not waive the rest.")
        message = " | ".join(parts)
        print(json.dumps({"decision": "block", "message": message, "reason": message}))
    else:
        print(json.dumps({"continue": True, "reason": "[PM_OBLIGATION_DRAIN_STATUS] " + " | ".join(parts)}))


main()
PYEOF
