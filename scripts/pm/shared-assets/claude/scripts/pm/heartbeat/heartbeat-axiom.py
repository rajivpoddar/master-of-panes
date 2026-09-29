#!/usr/bin/env python3
"""Heartbeat Axiom queries (MoP-side): error codes (+prior / 7-day same-slot compare) and
active users (per Clerk userId, masked email, files touched, internal split).

Moved from heydonna-app scripts/axiom-activity-report.py (Rajiv 2026-09-29 13:27 IST:
"pm scripts should be in the MoP repo, not in the app repo."). Read-only Axiom API.
Token: AXIOM_API_TOKEN / AXIOM_QUERY_TOKEN / AXIOM_TOKEN env, or AXIOM_ENV_FILE.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

AXIOM_API_URL = "https://api.axiom.co/v1/datasets/_apl?format=legacy"
ERROR_CODE_EXTEND = (
    "| where ['level'] == 'error' "
    "| extend code = coalesce(tostring(['action']), tostring(['message']), 'unknown'), "
    "who = coalesce(tostring(['email']), tostring(['userId']))"
)


DATASET = "heydonna-logs"


QUERY_DELAY_SECONDS = 3


PROD_FILTER = (
    "| where ['slot'] == \"prod\" "
    "or ['convex.deployment_type'] == \"prod\" "
    "or (['source'] == \"app\" and ['heydonna_env'] == \"production\")"
)


def load_token() -> str:
    """Load Axiom API token from env var or .env.local fallback.

    Priority: AXIOM_API_TOKEN > AXIOM_QUERY_TOKEN > AXIOM_TOKEN
    (AXIOM_TOKEN is often ingest-only; query-capable tokens take priority)
    """
    TOKEN_PRIORITY = ["AXIOM_API_TOKEN", "AXIOM_QUERY_TOKEN", "AXIOM_TOKEN"]

    # Check environment variables in priority order
    for var in TOKEN_PRIORITY:
        token = os.environ.get(var)
        if token:
            return token

    # Optional local fallback for interactive use.  The shared asset has no
    # repository credential dependency; callers should prefer the environment.
    env_path = os.environ.get("AXIOM_ENV_FILE")
    env_local = Path(env_path).expanduser() if env_path else None
    if env_local and env_local.exists():
        found: dict[str, str] = {}
        for line in env_local.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key in TOKEN_PRIORITY and value:
                found[key] = value
        for var in TOKEN_PRIORITY:
            if var in found:
                return found[var]

    print("Error: AXIOM_API_TOKEN or AXIOM_QUERY_TOKEN not found in environment or AXIOM_ENV_FILE", file=sys.stderr)
    sys.exit(1)


def run_apl_query(token: str, apl: str, start_time: str, end_time: str) -> dict:
    """Execute an APL query against the Axiom API."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "apl": apl,
        "startTime": start_time,
        "endTime": end_time,
    }
    encoded = json.dumps(payload).encode("utf-8")

    def post() -> tuple[int, str]:
        request = Request(AXIOM_API_URL, data=encoded, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=30) as response:
                return response.status, response.read().decode("utf-8", errors="replace")
        except HTTPError as error:
            return error.code, error.read().decode("utf-8", errors="replace")

    status, body = post()
    if status == 429:
        print("  [rate-limited] Waiting 10s before retry...", file=sys.stderr)
        time.sleep(10)
        status, body = post()

    if status == 401:
        print("Error: Axiom API returned 401 Unauthorized. Check your AXIOM_API_TOKEN.", file=sys.stderr)
        sys.exit(1)

    if status == 403:
        print(
            "Error: Axiom API returned 403 Forbidden. The token may lack query permissions.\n"
            "  Set AXIOM_API_TOKEN to an API token with query access (not an ingest-only token).",
            file=sys.stderr,
        )
        return {"matches": [], "buckets": {"series": [], "totals": []}}

    if status != 200:
        print(f"Error: Axiom API returned {status}: {body}", file=sys.stderr)
        return {"matches": [], "buckets": {"series": [], "totals": []}}

    try:
        value = json.loads(body)
    except json.JSONDecodeError:
        print("Error: Axiom API returned invalid JSON", file=sys.stderr)
        return {"matches": [], "buckets": {"series": [], "totals": []}}
    return value if isinstance(value, dict) else {"matches": [], "buckets": {"series": [], "totals": []}}


def extract_aggregations(result: dict) -> list[dict]:
    """Extract rows from aggregation-style (summarize) queries.

    Returns a flat list of dicts like:
      {"level": "error", "count_": 68}
    where group fields are flattened and the first aggregation value is stored
    under its op name (typically "count_").
    """
    rows = []
    for entry in result.get("buckets", {}).get("totals", []):
        row = {}
        # Flatten group fields
        for k, v in entry.get("group", {}).items():
            row[k] = v
        # Flatten aggregation values
        for agg in entry.get("aggregations", []):
            row[agg["op"]] = agg.get("value", 0)
        rows.append(row)
    return rows


def query_errors_by_code(token: str, start_time: str, end_time: str) -> list[dict]:
    """Error events grouped by code (`action`) with distinct users."""
    apl = (
        f"['{DATASET}'] {PROD_FILTER} {ERROR_CODE_EXTEND} "
        "| summarize events = count(), users = dcountif(who, isnotempty(who)) by code"
    )
    return normalize_error_code_rows(extract_aggregations(run_apl_query(token, apl, start_time, end_time)))


def query_error_totals(token: str, start_time: str, end_time: str) -> dict:
    """Total error events and distinct users for one window."""
    apl = (
        f"['{DATASET}'] {PROD_FILTER} {ERROR_CODE_EXTEND} "
        "| summarize events = count(), users = dcountif(who, isnotempty(who))"
    )
    rows = extract_aggregations(run_apl_query(token, apl, start_time, end_time))
    row = rows[0] if rows else {}
    return {"events": int(row.get("events") or 0), "users": int(row.get("users") or 0)}


def normalize_error_code_rows(rows: list[dict]) -> list[dict]:
    """Coerce aggregation rows to {code, events, users}, sorted by events desc."""
    out = []
    for row in rows:
        out.append({
            "code": str(row.get("code") or "unknown"),
            "events": int(row.get("events") or 0),
            "users": int(row.get("users") or 0),
        })
    out.sort(key=lambda r: (-r["events"], -r["users"], r["code"]))
    return out


def compare_windows(start: datetime, end: datetime, days: int = 7) -> dict:
    """Prior adjacent window plus the same clock slot on each of the last N days."""
    span = end - start
    return {
        "prior": (start - span, start),
        "same_slot": [(start - timedelta(days=d), end - timedelta(days=d)) for d in range(1, days + 1)],
    }


def median(values: list[int]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2


def build_error_code_report(
    codes: list[dict],
    totals: dict,
    prior: dict | None = None,
    same_slot: list[dict] | None = None,
    top: int = 5,
) -> dict:
    """Pure: shape the heartbeat error block (top codes, rest, deltas)."""
    codes = normalize_error_code_rows(codes)
    shown = codes[:top]
    rest = codes[top:]
    report = {
        "events": int(totals.get("events", 0)),
        "users": int(totals.get("users", 0)),
        "top": shown,
        "other_codes": len(rest),
        "other_events": sum(r["events"] for r in rest),
    }
    if prior is not None:
        report["prior_events"] = int(prior.get("events", 0))
        report["delta_vs_prior"] = report["events"] - report["prior_events"]
    if same_slot is not None:
        med = median([int(w.get("events", 0)) for w in same_slot])
        report["same_slot_median_events"] = med
        report["same_slot_events"] = [int(w.get("events", 0)) for w in same_slot]
        if med is not None:
            report["delta_vs_median"] = report["events"] - med
    return report


IDENTITY_SENTINELS = {"not_collected", "userid-only", "unknown", "pseudonymous", "cron", ""}


INTERNAL_DOMAINS = ("scribie.com", "heydonna.com", "heydonna.app", "example.com")


INTERNAL_LOCAL_PREFIXES = ("e2e-", "e2e+", "e2e.", "qa-", "qa+", "playwright", "test+", "test-")


INTERNAL_USER_IDS = {u.strip() for u in os.environ.get("HEYDONNA_INTERNAL_USER_IDS", "").split(",") if u.strip()}


def query_active_user_files(token: str, start_time: str, end_time: str) -> list[dict]:
    """Per Clerk userId (stable identity): actions, real files touched, observed (masked) emails."""
    apl = (
        f"['{DATASET}'] {PROD_FILTER} "
        f"| where ['userId'] startswith 'user_' "
        f"| summarize actions=count(), "
        f"files=dcountif(['fileId'], isnotempty(['fileId']) and ['fileId'] != 'none'), "
        f"emails=make_set(['email'], 5) by ['userId'] "
        f"| order by actions desc"
    )
    return extract_aggregations(run_apl_query(token, apl, start_time, end_time))


def mask_email(email: str) -> str:
    """Same masking as the text report's Active Users section."""
    if "*" in email:  # already masked at log ingest; keep the more informative ingest mask
        return email
    parts = email.split("@") if email else ["?"]
    if len(parts) == 2 and len(parts[0]) > 2:
        return parts[0][:2] + "***@" + parts[1]
    return email


def _masked_prefix_match(local: str, prefix: str) -> bool:
    """`*` in the masked local part matches any char; needs >=2 concrete matching chars."""
    if len(local) < len(prefix):
        return False
    concrete = 0
    for got, want in zip(local, prefix):
        if got == "*":
            continue
        if got != want:
            return False
        concrete += 1
    return concrete >= 2


def is_internal_identity(user_id: str, email: str) -> bool:
    if user_id in INTERNAL_USER_IDS:
        return True
    e = (email or "").lower()
    if "@" not in e:
        return False
    local, domain = e.rsplit("@", 1)
    if domain in INTERNAL_DOMAINS or any(domain.endswith("." + d) for d in INTERNAL_DOMAINS):
        return True
    return any(_masked_prefix_match(local, p) for p in INTERNAL_LOCAL_PREFIXES)


def _display_email(emails) -> str:
    if isinstance(emails, str):
        emails = [emails]
    real = [str(e) for e in emails or [] if "@" in str(e) and str(e).lower() not in IDENTITY_SENTINELS]
    return sorted(real, key=lambda e: (-len(e), e))[0] if real else ""


def build_active_users_report(rows: list[dict], prior_rows: list[dict] | None = None) -> dict:
    """Pure: one entry per Clerk userId; external users (masked email) vs an internal count."""
    def split(rs):
        by_uid: dict[str, dict] = {}
        for r in rs or []:
            uid = str(r.get("userId") or "").strip()
            if not uid.startswith("user_"):
                continue
            cur = by_uid.setdefault(uid, {"actions": 0, "files": 0, "emails": []})
            cur["actions"] += int(r.get("actions") or 0)
            cur["files"] = max(cur["files"], int(r.get("files") or 0))
            em = r.get("emails", r.get("email"))
            cur["emails"] += em if isinstance(em, list) else [em]
        ext, internal = [], 0
        for uid, u in by_uid.items():
            email = _display_email(u["emails"])
            if is_internal_identity(uid, email):
                internal += 1
                continue
            ext.append({"user": mask_email(email) if email else uid[-6:], "files": u["files"],
                        "actions": u["actions"]})
        ext.sort(key=lambda u: (-u["files"], -u["actions"], u["user"]))
        return ext, internal

    users, internal = split(rows)
    report = {"count": len(users), "internal": internal, "users": users}
    if prior_rows is not None:
        report["prior_count"] = len(split(prior_rows)[0])
    return report



def _fmt(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    p = argparse.ArgumentParser(description="Heartbeat Axiom queries (JSON on stdout)")
    p.add_argument("--hours", type=int, required=True)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--errors-by-code", action="store_true")
    mode.add_argument("--active-users", action="store_true")
    p.add_argument("--compare", action="store_true", help="errors: prior window + 7-day same-slot median")
    p.add_argument("--top", type=int, default=5)
    a = p.parse_args()
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(hours=a.hours)
    s, e = _fmt(start_dt), _fmt(end_dt)
    token = load_token()
    if a.active_users:
        rows = query_active_user_files(token, s, e)
        time.sleep(QUERY_DELAY_SECONDS)
        prior = query_active_user_files(token, _fmt(start_dt - (end_dt - start_dt)), s)
        report = build_active_users_report(rows, prior)
    else:
        codes = query_errors_by_code(token, s, e)
        time.sleep(QUERY_DELAY_SECONDS)
        totals = query_error_totals(token, s, e)
        prior = same_slot = None
        if a.compare:
            w = compare_windows(start_dt, end_dt)
            time.sleep(QUERY_DELAY_SECONDS)
            prior = query_error_totals(token, _fmt(w["prior"][0]), _fmt(w["prior"][1]))
            same_slot = []
            for sd, ed in w["same_slot"]:
                time.sleep(QUERY_DELAY_SECONDS)
                same_slot.append(query_error_totals(token, _fmt(sd), _fmt(ed)))
        report = build_error_code_report(codes, totals, prior, same_slot, top=a.top)
        report["all_codes"] = codes
    report["window"] = {"start": s, "end": e}
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
