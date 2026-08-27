#!/usr/bin/env python3
"""
Axiom Activity Report for HeyDonna

Queries the Axiom API for HeyDonna log data and generates a structured
activity report. Respects Axiom rate limits with delays between queries.

Usage:
    python3 scripts/pm/axiom-activity-report.py --hours 24
    python3 scripts/pm/axiom-activity-report.py --days 7
    python3 scripts/pm/axiom-activity-report.py --start 2026-08-25T18:30:00Z --end 2026-08-26T18:30:00Z
    python3 scripts/pm/axiom-activity-report.py --start 2026-08-25T18:30:00Z --end 2026-08-26T18:30:00Z --frozen-proof
    python3 scripts/pm/axiom-activity-report.py --hours 1 --quiet
    python3 scripts/pm/axiom-activity-report.py --days 1 --json
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

AXIOM_API_URL = "https://api.axiom.co/v1/datasets/_apl?format=legacy"
DATASET = "heydonna-logs"
QUERY_DELAY_SECONDS = 3
IST = timezone(timedelta(hours=5, minutes=30), name="IST")
FROZEN_START = "2026-08-25T18:30:00Z"
FROZEN_END = "2026-08-26T18:30:00Z"

# #6251: production-row admission filter — source-specific, fail-closed.
# Admits Modal prod (slot), Convex prod (deployment_type), and app prod (heydonna_env) rows.
# Slot/dev traffic with environment=production but no matching identity field is excluded.
PROD_FILTER = (
    "| where ['slot'] == \"prod\" "
    "or ['convex.deployment_type'] == \"prod\" "
    "or (['source'] == \"app\" and ['heydonna_env'] == \"production\")"
)

# Friendly labels for product actions
ACTION_LABELS = {
    "export_complete": "Exports",
    "ai_proofread_complete": "Manual AI Proofreads",
    "ai_format_complete": "Manual AI Formats",
    "transcription_complete": "Transcriptions",
}

# Editor save-suppression family (#7173/#7212/#7238 monitoring). These are
# emitted by lib/storage/sync-manager.ts as logToAxiom('info',
# 'SyncSaveSuppressed', {fileId, transcriptId, action, branch, isDirty, ...}).
# The Axiom row carries the family ONLY via the top-level typed `action`
# field; the `message` field is null/absent on production rows (the
# 'SyncSaveSuppressed' string lands in `context`, not `message`). Queries must
# key on `action`, never on `message == 'SyncSaveSuppressed'`, or every real
# suppression row is silently dropped and the report reads zero.
SAVE_SUPPRESSION_ACTIONS = (
    "save_suppressed_inflight",
    "save_suppressed_sw_lease",
    "save_suppressed_lease_held",
    "save_suppressed_capability",
    "save_suppressed_latch",
    "save_suppressed_recovery_stale_base",
    "save_suppressed_sweep_incompatible",
    "save_suppressed_sweep_busy",
    "save_suppressed_sweep_lease",
    "save_suppressed_sweep_capability",
    "save_suppressed_sw_coalesce",
    "save_escape_unsynced",
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


def format_number(n: int) -> str:
    """Format a number with comma separators."""
    return f"{n:,}"


def truncate(s: str, max_len: int = 80) -> str:
    """Truncate a string to max_len characters."""
    if not s:
        return ""
    return s if len(s) <= max_len else s[: max_len - 3] + "..."


def build_human_period(hours: int) -> str:
    """Build human-readable period string."""
    if hours % 24 == 0 and hours >= 24:
        days = hours // 24
        return f"last {days} day{'s' if days > 1 else ''}"
    return f"last {hours} hour{'s' if hours > 1 else ''}"


# ---------------------------------------------------------------------------
# Axiom response parsing helpers
#
# Axiom query responses have two shapes:
#
# 1. Aggregation queries (summarize): results in buckets.totals[], each entry:
#    {"id": ..., "group": {"field": value}, "aggregations": [{"op": "count_", "value": N}]}
#
# 2. Match queries (project/take): results in matches[], each entry:
#    {"_time": "...", "_sysTime": "...", "_rowId": "...", "data": {"field": value, ...}}
# ---------------------------------------------------------------------------


def extract_matches(result: dict) -> list[dict]:
    """Extract row data from match-style (non-aggregation) queries.

    Returns a flat list of dicts with _time merged into each row's data fields.
    """
    rows = []
    for m in result.get("matches", []):
        row = dict(m.get("data", {}))
        # _time lives at the top level of each match, merge it in
        for field in ("_time", "_sysTime", "_rowId"):
            if field in m:
                row[field] = m[field]
        rows.append(row)
    return rows


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


# ---------------------------------------------------------------------------
# Query definitions
# ---------------------------------------------------------------------------


def query_event_summary(token: str, start_time: str, end_time: str) -> list[dict]:
    """Query 1: Event summary by level."""
    apl = f"['{DATASET}'] {PROD_FILTER} | summarize count() by ['level']"
    raw = run_apl_query(token, apl, start_time, end_time)
    return extract_aggregations(raw)


def query_errors_detail(token: str, start_time: str, end_time: str) -> list[dict]:
    """Query 2: Last 10 errors with details."""
    apl = (
        f"['{DATASET}'] {PROD_FILTER} "
        f"| where ['level'] == 'error' "
        f"| project ['_time'], ['action'], ['email'], ['meta'] "
        f"| order by ['_time'] desc | take 10"
    )
    raw = run_apl_query(token, apl, start_time, end_time)
    return extract_matches(raw)


def query_save_suppression(token: str, start_time: str, end_time: str) -> list[dict]:
    """Dedicated 3h aggregation for the editor save-suppression family.

    Distinct from the generic error-level top-3 summary: the family is emitted
    at info level with a typed top-level `action` field. Production rows carry
    the family ONLY via `action` and have `message=null` (the legacy
    'SyncSaveSuppressed' context string is not the row discriminator), so the
    query keys on `action` and must NOT gate on `message`. Filtering on
    `message == 'SyncSaveSuppressed'` silently drops every real suppression
    row and falsely reports zero. Returns one row per
    action/branch/fileId with count, first, and
    last time so the heartbeat can distinguish bounded expected suppression
    from unresolved dirty/inflight/lease loops and save_escape_unsynced. The
    dataset does not expose `_rowId` as a queryable field, so identity is the
    canonical aggregate tuple.  The grouped `count_` for
    save_escape_unsynced is the frozen unique-row count.
    """
    actions = ", ".join(f"'{a}'" for a in SAVE_SUPPRESSION_ACTIONS)
    apl = (
        f"['{DATASET}'] {PROD_FILTER} "
        f"| where ['action'] in ({actions}) "
        f"| summarize count(), min(['_time']), max(['_time']) "
        f"by ['action'], ['branch'], ['fileId']"
    )
    raw = run_apl_query(token, apl, start_time, end_time)
    return extract_aggregations(raw)


def build_save_suppression_summary(rows: list[dict]) -> dict:
    """Group save-suppression aggregation rows into a heartbeat-ready summary.

    Returns:
      total: int
      by_action: {action: count}
      unresolved: {action: count} for the escalation-relevant family
      affected: [{fileId, action, branch, count, first, last}]
    """
    by_action: dict[str, int] = {}
    unresolved: dict[str, int] = {}
    affected: list[dict] = []
    unresolved_actions = {
        "save_escape_unsynced",
        "save_suppressed_inflight",
        "save_suppressed_sw_lease",
        "save_suppressed_lease_held",
        "save_suppressed_sweep_busy",
        "save_suppressed_sweep_lease",
    }
    # Axiom can repeat a returned row when a response is retried or split.
    # Deduplicate exact rows deterministically before aggregating.
    seen: set[str] = set()
    for row in sorted(rows, key=lambda item: json.dumps(item, sort_keys=True, default=str)):
        fingerprint = json.dumps(row, sort_keys=True, default=str, separators=(",", ":"))
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        action = str(row.get("action") or "unknown")
        count = int(row.get("count_") or 0)
        if count <= 0 or not str(row.get("action") or "").strip():
            # Skip zero-count / empty-group aggregation shells; they are not
            # real family rows and must not render as junk in the report.
            continue
        by_action[action] = by_action.get(action, 0) + count
        if action in unresolved_actions:
            unresolved[action] = unresolved.get(action, 0) + count
        unique_count = int(
            row.get("dcount_")
            or row.get("dcount")
            or row.get("unique_row_count")
            or (count if action == "save_escape_unsynced" else 0)
        )
        affected.append(
            {
                "fileId": str(row.get("fileId") or ""),
                "action": action,
                "branch": str(row.get("branch") or ""),
                "count": count,
                "unique_count": unique_count,
                "first": str(row.get("min_") or row.get("min__time") or row.get("min") or ""),
                "last": str(row.get("max_") or row.get("max__time") or row.get("max") or ""),
            }
        )
    affected.sort(key=lambda r: (r["action"], r["fileId"]))
    unique_save_escape_unsynced = sum(
        int(row.get("unique_count", 0))
        for row in affected
        if row["action"] == "save_escape_unsynced"
    )
    total = sum(by_action.values())
    return {
        "total": total,
        "suppression_family_rows": total,
        "unique_save_escape_unsynced": unique_save_escape_unsynced,
        "by_action": dict(sorted(by_action.items(), key=lambda item: (-item[1], item[0]))),
        "unresolved": dict(sorted(unresolved.items(), key=lambda item: (-item[1], item[0]))),
        "affected": affected,
    }


def query_product_activity(token: str, start_time: str, end_time: str) -> list[dict]:
    """Query 3: successful product activity counts.

    Manual proofread/format emit a completion event for both success and
    failure. Count only successful completions; export/transcription completion
    events are success-only by contract.
    """
    apl = (
        f"['{DATASET}'] {PROD_FILTER} "
        f"| where ['action'] in "
        f"('export_complete', 'ai_proofread_complete', 'ai_format_complete', 'transcription_complete') "
        f"| project ['action'], ['meta']"
    )
    raw = run_apl_query(token, apl, start_time, end_time)
    counts: dict[str, int] = {}
    for row in extract_matches(raw):
        action = str(row.get("action") or "")
        if action in {"ai_proofread_complete", "ai_format_complete"}:
            if _parse_meta(row.get("meta")).get("success") is not True:
                continue
        counts[action] = counts.get(action, 0) + 1
    return [{"action": action, "count_": count} for action, count in sorted(counts.items())]


def query_auto_process(token: str, start_time: str, end_time: str) -> list[dict]:
    """Query 4: canonical auto-process lifecycle events.

    This intentionally excludes debug/legacy events. Callers must use
    ``build_auto_process_summary`` rather than treating rows as runs.
    """
    apl = (
        f"['{DATASET}'] {PROD_FILTER} "
        f"| where ['action'] in "
        f"('run_auto_process_start', 'auto_process_stage_complete_v2', 'auto_process_terminal') "
        f"| project ['_time'], ['action'], ['meta'] "
        f"| order by ['_time'] asc"
    )
    raw = run_apl_query(token, apl, start_time, end_time)
    return extract_matches(raw)


def _parse_meta(value) -> dict:
    """Return an event meta object from Axiom's dict-or-JSON-string shape."""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_auto_process_summary(rows: list[dict]) -> dict:
    """Build deduplicated run/stage counts from canonical lifecycle events."""
    starts: set[str] = set()
    terminals: dict[str, str] = {}
    successful_stages: set[tuple[str, str]] = set()
    trigger_sources: dict[str, int] = {}

    for index, row in enumerate(rows):
        action = row.get("action", "")
        meta = _parse_meta(row.get("meta"))
        pipeline_run_id = str(meta.get("pipeline_run_id") or "")

        if action == "run_auto_process_start":
            # run_auto_process_start predates pipeline_run_id on the event.
            # proofread_run_id is generated once per actual pipeline attempt.
            start_id = str(meta.get("proofread_run_id") or "")
            if not start_id:
                start_id = f"{meta.get('transcript_id', '')}|{row.get('_time', '')}|{index}"
            starts.add(start_id)
            source = str(meta.get("trigger_source") or "unknown")
            trigger_sources[source] = trigger_sources.get(source, 0) + 1
        elif action == "auto_process_stage_complete_v2":
            stage = str(meta.get("stage") or "")
            outcome = str(meta.get("outcome") or "")
            if pipeline_run_id and stage and outcome == "success":
                successful_stages.add((pipeline_run_id, stage))
        elif action == "auto_process_terminal" and pipeline_run_id:
            terminals[pipeline_run_id] = str(meta.get("terminal_state") or "unknown")

    return {
        "runs_started": len(starts),
        "proofreads_completed": sum(stage == "v1_proofread" for _, stage in successful_stages),
        "formats_completed": sum(stage == "v2_format" for _, stage in successful_stages),
        "terminals_success": sum(state == "SUCCESS_FULL" for state in terminals.values()),
        "terminals_failed": sum(state.startswith("FAILED_") for state in terminals.values()),
        "trigger_sources": trigger_sources,
    }


def query_hourly_distribution(token: str, start_time: str, end_time: str) -> list[dict]:
    """Query 5: Hourly event distribution."""
    apl = (
        f"['{DATASET}'] {PROD_FILTER} "
        f"| summarize count() by bin(['_time'], 1h) "
        f"| order by ['_time'] desc"
    )
    raw = run_apl_query(token, apl, start_time, end_time)
    return extract_aggregations(raw)


def query_active_users(token: str, start_time: str, end_time: str) -> list[dict]:
    """Query 6: Active users — unique emails with product actions."""
    apl = (
        f"['{DATASET}'] {PROD_FILTER} "
        f"| where ['email'] != 'unknown' and ['email'] != '' "
        f"and isnotnull(['email']) "
        f"| summarize actions=count(), last_seen=max(['_time']) by ['email'] "
        f"| order by actions desc"
    )
    raw = run_apl_query(token, apl, start_time, end_time)
    return extract_aggregations(raw)


# ---------------------------------------------------------------------------
# Pipeline soft-fail / validator-reject queries (Rajiv directive 2026-05-11)
# ---------------------------------------------------------------------------
#
# These surface how often each safety guard fires in the pipeline. Names are
# the actual production event names observed in `heydonna-logs` over 72h
# (validated 2026-05-11 — see morning-brief skill commentary).
#
# Categories:
#   - Hallucination firewall:        hallucination_firewall, hallucination_firewall_cand1/2, hallucination_firewall_error
#   - Format validator (proofread/format stage):
#         format_validator_log_only, format_validator_violations_detail,
#         format_validator_rollback_in_docx, format_validator_threshold_accepted,
#         format_validator_threshold_warn, format_validator_log_only_high_ratio_alert
#   - Speaker correction:            speaker_correction_ratio_rejected{_step,},
#                                    speaker_correction_content_mutation_rejected,
#                                    speaker_correction_rejection_flow_complete,
#                                    speaker_correction.bon.{result,exhausted_entering_retry,draw_failed,retry_aborted}
#   - BoN ladder:                    format_bon_ladder.{won,exhausted,rung_result,rung_error},
#                                    bon_escalating_to_cand{2,3}, bon_no_valid_candidates,
#                                    bon_budget_exhausted, bon_ladder_budget_exhausted,
#                                    bon_provider_503_short_circuit_to_cand1,
#                                    bon_empty_output_hard_reject, bon_global_abort_error
#   - Other hard gates:              truncation_rejection_gate_triggered, unhandled_rejection
#
# Total auto-process volume comes from `run_auto_process_start`: it represents
# an execution that actually began. `auto_process_spawned` is only scheduling
# acceptance and is not reliably emitted on the production execution surface.


SOFT_FAIL_ACTIONS = [
    # Hallucination firewall
    "hallucination_firewall",
    "hallucination_firewall_cand1",
    "hallucination_firewall_cand2",
    "hallucination_firewall_error",
    # Format validator
    "format_validator_log_only",
    "format_validator_violations_detail",
    "format_validator_rollback_in_docx",
    "format_validator_threshold_accepted",
    "format_validator_threshold_warn",
    "format_validator_log_only_high_ratio_alert",
    "format_degraded_proceedings",
    # Speaker correction
    "speaker_correction_ratio_rejected",
    "speaker_correction_ratio_rejected_step",
    "speaker_correction_content_mutation_rejected",
    "speaker_correction_rejection_flow_complete",
    "speaker_correction.bon.result",
    "speaker_correction.bon.exhausted_entering_retry",
    "speaker_correction.bon.draw_failed",
    "speaker_correction.bon.retry_aborted",
    # BoN ladder
    "format_bon_ladder.won",
    "format_bon_ladder.exhausted",
    "format_bon_ladder.rung_result",
    "format_bon_ladder.rung_error",
    "bon_escalating_to_cand2",
    "bon_escalating_to_cand3",
    "bon_no_valid_candidates",
    "bon_budget_exhausted",
    "bon_ladder_budget_exhausted",
    "bon_provider_503_short_circuit_to_cand1",
    "bon_empty_output_hard_reject",
    "bon_global_abort_error",
    # Other hard gates
    "truncation_rejection_gate_triggered",
    "unhandled_rejection",
]


def query_soft_fail_counts(token: str, start_time: str, end_time: str) -> list[dict]:
    """Query soft-fail / validator-reject event counts by action."""
    actions_list = ", ".join(f"'{a}'" for a in SOFT_FAIL_ACTIONS)
    apl = (
        f"['{DATASET}'] {PROD_FILTER} "
        f"| where ['action'] in ({actions_list}) "
        f"| summarize count() by ['action'] | order by count_ desc"
    )
    raw = run_apl_query(token, apl, start_time, end_time)
    return extract_aggregations(raw)


def query_convex_proceedings_degraded(token: str, start_time: str, end_time: str) -> int:
    """Count DISTINCT Convex-side nested `format_proceedings_degraded` detections.

    The Modal pipeline emits its proceedings-degraded event with a TOP-LEVEL
    Axiom `action == 'format_degraded_proceedings'` (counted in
    query_soft_fail_counts). The Convex side (autoprocess.ts always-log #5309)
    emits a DIFFERENT name, `format_proceedings_degraded`, via convexLogger ->
    Convex log stream, which wraps the structured JSON into a string field
    `data.message` — so it never appears under the top-level `['action']`
    column and was silently counted as 0 (observability gap, Rajiv CTO
    2026-06-07 thread C0ALZJHGE49/1780806816.081179).

    Match the EXACT double-quoted token `"action":"format_proceedings_degraded"`
    inside `data.message`. The quoted token avoids two false positives:
    (1) `format_proceedings_degraded_suppressed` (the suppressed variant — the
    closing quote excludes it), and (2) any bare-substring / numeric-token
    collision (per 21-lessons Axiom precise-token rule).

    DEDUP (Codex P2, PR #5323): `format_proceedings_degraded` is emitted at TWO
    call sites for the SAME detection — autoprocess.ts:1214 (#5309 always-log
    path, fires when `_proceedingsQaExpected`) AND autoprocess.ts:4527
    (`notifyProceedingsDegradation` handler, "Axiom tracking — fires on every
    detection regardless of Slack delivery"). For a QA-expected detection where
    the scheduled action also runs, the SAME detection is logged twice, so a raw
    `count()` of log lines OVERSTATES distinct detections. We count DISTINCT
    detections instead.

    Distinct key: the detection is keyed by the pipeline run. `fileId`
    (= the transcript id; CORE_KEY in convexLogger.structureLogData) appears as
    a top-level `"fileId":"<id>"` in the JSON blob and is identical in BOTH
    emissions; `pipelineRunId` is a non-core field nested (double-escaped) inside
    the stringified `meta` map and is `v.optional`, so it may be absent. We dedup
    on the COMPOSITE `(fileId + pipelineRunId)` extracted from `data.message`:
    when both emissions of one detection carry the same pipelineRunId they
    collapse to one row; when pipelineRunId is absent the composite degrades to
    fileId-only (still collapses the two same-transcript emissions). A single
    transcript that legitimately degrades on two separate pipeline runs keeps
    distinct rows via the differing pipelineRunId.
    """
    # Parse fileId (top-level JSON key) and pipelineRunId (nested/escaped inside
    # the stringified `meta`) out of the data.message string blob, then count
    # rows of the distinct (fileId, pipelineRunId) composite. The pipelineRunId
    # regex tolerates the JSON.stringify-of-meta escaping (\" or ") around the
    # key/value; when no pipelineRunId is present the captured group is empty so
    # the composite collapses to fileId-only.
    apl = (
        f"['{DATASET}'] {PROD_FILTER} "
        f"| where tostring(['data.message']) contains "
        f"'\"action\":\"format_proceedings_degraded\"' "
        f"| extend _msg = tostring(['data.message']) "
        f"| extend _fileId = extract('\\\\\"fileId\\\\\":\\\\\"([^\\\\\"]+)\\\\\"', 1, _msg) "
        f"| extend _pipelineRunId = extract('pipelineRunId\\\\\\\\?\\\\\"\\\\s*:\\\\s*\\\\\\\\?\\\\\"([^\\\\\"\\\\\\\\]+)', 1, _msg) "
        f"| extend _detectionKey = strcat(_fileId, '|', _pipelineRunId) "
        f"| summarize count() by _detectionKey"
    )
    raw = run_apl_query(token, apl, start_time, end_time)
    rows = extract_aggregations(raw)
    # Each row is one distinct detection composite; the number of rows is the
    # deduped distinct-detection count.
    return len(rows)


def query_auto_process_total(token: str, start_time: str, end_time: str) -> int:
    """Total auto-process executions that actually started."""
    apl = (
        f"['{DATASET}'] {PROD_FILTER} "
        f"| where ['action'] == 'run_auto_process_start' "
        f"| summarize count()"
    )
    raw = run_apl_query(token, apl, start_time, end_time)
    rows = extract_aggregations(raw)
    if not rows:
        return 0
    return int(rows[0].get("count_", 0))


def query_auto_process_by_trigger_source(token: str, start_time: str, end_time: str) -> list[dict]:
    """#6249: run_auto_process_start grouped by trigger_source (no day bin — per-source sums).

    Returns list[dict] with keys: {"trigger_source": str, "count_": int}
    """
    apl = (
        f"['{DATASET}'] {PROD_FILTER} "
        f"| where ['action'] == 'run_auto_process_start' "
        f"| project ['meta']"
    )
    result = run_apl_query(token, apl, start_time, end_time)
    counts: dict[str, int] = {}
    for row in extract_matches(result):
        source = str(_parse_meta(row.get("meta")).get("trigger_source") or "unknown")
        counts[source] = counts.get(source, 0) + 1
    return [
        {"trigger_source": source, "count_": count}
        for source, count in sorted(counts.items())
    ]


def build_soft_fail_report(counts: list[dict], total_runs: int, convex_proceedings_degraded: int = 0,
                          trigger_source_breakdown: dict | None = None) -> dict:
    """Group soft-fail counts into categories with rate vs total auto-process runs.

    `convex_proceedings_degraded` is the Convex-side nested
    `format_proceedings_degraded` count (from query_convex_proceedings_degraded),
    reported distinctly from the Modal top-level `format_degraded_proceedings`
    count so the observability blind spot can't recur silently.

    `trigger_source_breakdown` is the #6249 per-source run count (from
    query_auto_process_by_trigger_source), added to the returned dict when present.
    """
    by_action = {row.get("action", ""): int(row.get("count_", 0)) for row in counts}

    def get(*keys: str) -> int:
        return sum(by_action.get(k, 0) for k in keys)

    rate = lambda n: f"{(n / total_runs * 100):.1f}%" if total_runs > 0 else "n/a"

    modal_proceedings_degraded = by_action.get("format_degraded_proceedings", 0)
    proceedings_degraded_total = modal_proceedings_degraded + convex_proceedings_degraded

    return {
        "total_auto_process_runs": total_runs,
        "categories": {
            "hallucination_firewall": {
                "total": get("hallucination_firewall", "hallucination_firewall_cand1",
                             "hallucination_firewall_cand2", "hallucination_firewall_error"),
                "rate": rate(get("hallucination_firewall", "hallucination_firewall_cand1",
                                  "hallucination_firewall_cand2", "hallucination_firewall_error")),
                "breakdown": {
                    "cand1_fires": by_action.get("hallucination_firewall_cand1", 0),
                    "cand2_fires": by_action.get("hallucination_firewall_cand2", 0),
                    "primary_fires": by_action.get("hallucination_firewall", 0),
                    "errors": by_action.get("hallucination_firewall_error", 0),
                },
            },
            "format_validator": {
                "log_only": by_action.get("format_validator_log_only", 0),
                "violations_detail": by_action.get("format_validator_violations_detail", 0),
                "rollback_in_docx": by_action.get("format_validator_rollback_in_docx", 0),
                "threshold_accepted": by_action.get("format_validator_threshold_accepted", 0),
                "threshold_warn": by_action.get("format_validator_threshold_warn", 0),
                "high_ratio_alert": by_action.get("format_validator_log_only_high_ratio_alert", 0),
            },
            "proceedings_degradation": {
                "total": proceedings_degraded_total,
                "modal_side": modal_proceedings_degraded,
                "convex_side": convex_proceedings_degraded,
            },
            "speaker_correction": {
                "ratio_rejected": by_action.get("speaker_correction_ratio_rejected", 0),
                "ratio_rejected_step": by_action.get("speaker_correction_ratio_rejected_step", 0),
                "content_mutation_rejected": by_action.get("speaker_correction_content_mutation_rejected", 0),
                "rejection_flow_complete": by_action.get("speaker_correction_rejection_flow_complete", 0),
                "bon_result": by_action.get("speaker_correction.bon.result", 0),
                "bon_exhausted_retry": by_action.get("speaker_correction.bon.exhausted_entering_retry", 0),
                "bon_draw_failed": by_action.get("speaker_correction.bon.draw_failed", 0),
                "bon_retry_aborted": by_action.get("speaker_correction.bon.retry_aborted", 0),
            },
            "bon_ladder": {
                "won": by_action.get("format_bon_ladder.won", 0),
                "exhausted": by_action.get("format_bon_ladder.exhausted", 0),
                "rung_result": by_action.get("format_bon_ladder.rung_result", 0),
                "rung_error": by_action.get("format_bon_ladder.rung_error", 0),
                "escalate_cand2": by_action.get("bon_escalating_to_cand2", 0),
                "escalate_cand3": by_action.get("bon_escalating_to_cand3", 0),
                "no_valid_candidates": by_action.get("bon_no_valid_candidates", 0),
                "budget_exhausted": by_action.get("bon_budget_exhausted", 0)
                                    + by_action.get("bon_ladder_budget_exhausted", 0),
                "provider_503_short_circuit": by_action.get("bon_provider_503_short_circuit_to_cand1", 0),
                "empty_output_hard_reject": by_action.get("bon_empty_output_hard_reject", 0),
                "global_abort_error": by_action.get("bon_global_abort_error", 0),
            },
            "other_hard_gates": {
                "truncation_rejection_gate_triggered": by_action.get("truncation_rejection_gate_triggered", 0),
                "unhandled_rejection": by_action.get("unhandled_rejection", 0),
            },
        },
        "rates_vs_auto_process": {
            "firewall_rate": rate(get("hallucination_firewall", "hallucination_firewall_cand1",
                                       "hallucination_firewall_cand2")),
            "format_validator_rollback_rate": rate(by_action.get("format_validator_rollback_in_docx", 0)),
            "speaker_correction_reject_rate": rate(get("speaker_correction_ratio_rejected",
                                                        "speaker_correction_ratio_rejected_step",
                                                        "speaker_correction_content_mutation_rejected")),
            "bon_exhausted_rate": rate(by_action.get("format_bon_ladder.exhausted", 0)),
            "bon_escalate_cand2_rate": rate(by_action.get("bon_escalating_to_cand2", 0)),
            "bon_escalate_cand3_rate": rate(by_action.get("bon_escalating_to_cand3", 0)),
            "proceedings_degradation_rate": rate(proceedings_degraded_total),
        },
        # #6249: per-source provenance breakdown (injected when --soft-fail is active)
        **({"trigger_source_breakdown": trigger_source_breakdown} if trigger_source_breakdown is not None else {}),
    }


def print_soft_fail_report(report: dict):
    """Print the pipeline soft-fail section."""
    total = report.get("total_auto_process_runs", 0)
    cats = report.get("categories", {})
    rates = report.get("rates_vs_auto_process", {})

    print("─" * 40)
    print("  Pipeline Soft-Fail Rates")
    print("─" * 40)
    print(f"  Total auto-process runs (spawned): {format_number(total)}")
    # #6249: per-source provenance breakdown
    tsb = report.get("trigger_source_breakdown", {})
    if tsb:
        print("    By trigger source:")
        for source, count in sorted(tsb.items()):
            print(f"      {source}: {format_number(count)}")
    print("  Rates below = fires-per-run (events can fire multiple times per run:")
    print("  per stage, per BoN candidate). >100% means avg fires per run > 1.")
    print()

    fw = cats.get("hallucination_firewall", {})
    print(f"  Hallucination Firewall: {format_number(fw.get('total', 0))} fires ({rates.get('firewall_rate', 'n/a')} of runs)")
    bd = fw.get("breakdown", {})
    print(f"    cand1: {bd.get('cand1_fires', 0)}  cand2: {bd.get('cand2_fires', 0)}  "
          f"primary: {bd.get('primary_fires', 0)}  errors: {bd.get('errors', 0)}")
    print()

    fv = cats.get("format_validator", {})
    print(f"  Format Validator:")
    print(f"    log_only:           {fv.get('log_only', 0)}")
    print(f"    violations_detail:  {fv.get('violations_detail', 0)}")
    print(f"    rollback_in_docx:   {fv.get('rollback_in_docx', 0)}  ({rates.get('format_validator_rollback_rate', 'n/a')} of runs)")
    print(f"    threshold_accepted: {fv.get('threshold_accepted', 0)}")
    print(f"    threshold_warn:     {fv.get('threshold_warn', 0)}")
    print(f"    high_ratio_alert:   {fv.get('high_ratio_alert', 0)}")
    print()

    pd = cats.get("proceedings_degradation", {})
    print(f"  Proceedings Q/A Degradation: {pd.get('total', 0)} fires ({rates.get('proceedings_degradation_rate', 'n/a')} of runs)")
    print(f"    modal-side (action=format_degraded_proceedings):       {pd.get('modal_side', 0)}")
    print(f"    convex-side (data.message action=format_proceedings_degraded): {pd.get('convex_side', 0)}")
    print()

    sc = cats.get("speaker_correction", {})
    print(f"  Speaker Correction:")
    print(f"    ratio_rejected (final + step):   {sc.get('ratio_rejected', 0)} + {sc.get('ratio_rejected_step', 0)} "
          f"({rates.get('speaker_correction_reject_rate', 'n/a')} of runs)")
    print(f"    content_mutation_rejected:       {sc.get('content_mutation_rejected', 0)}")
    print(f"    rejection_flow_complete:         {sc.get('rejection_flow_complete', 0)}")
    print(f"    bon (result/retry/fail/aborted): {sc.get('bon_result', 0)} / "
          f"{sc.get('bon_exhausted_retry', 0)} / {sc.get('bon_draw_failed', 0)} / "
          f"{sc.get('bon_retry_aborted', 0)}")
    print()

    bl = cats.get("bon_ladder", {})
    print(f"  BoN Ladder (format stage):")
    print(f"    won:                       {bl.get('won', 0)}")
    print(f"    exhausted:                 {bl.get('exhausted', 0)}  ({rates.get('bon_exhausted_rate', 'n/a')} of runs)")
    print(f"    rung_result / rung_error:  {bl.get('rung_result', 0)} / {bl.get('rung_error', 0)}")
    print(f"    escalate cand2 / cand3:    {bl.get('escalate_cand2', 0)} ({rates.get('bon_escalate_cand2_rate', 'n/a')}) / "
          f"{bl.get('escalate_cand3', 0)} ({rates.get('bon_escalate_cand3_rate', 'n/a')})")
    print(f"    no_valid_candidates:       {bl.get('no_valid_candidates', 0)}")
    print(f"    budget_exhausted:          {bl.get('budget_exhausted', 0)}")
    print(f"    provider_503_short_circuit:{bl.get('provider_503_short_circuit', 0)}")
    print(f"    empty_output_hard_reject:  {bl.get('empty_output_hard_reject', 0)}")
    print(f"    global_abort_error:        {bl.get('global_abort_error', 0)}")
    print()

    og = cats.get("other_hard_gates", {})
    if any(og.values()):
        print(f"  Other Hard Gates:")
        for k, v in og.items():
            if v > 0:
                print(f"    {k}: {v}")
        print()


def fetch_convex_signups(hours: int) -> list[dict]:
    """Fetch recent user sign-ups from Convex prod via CLI.

    Returns list of dicts with: email, name, createdAt, onboarding_steps.
    """
    project_root = Path(__file__).resolve().parent.parent
    try:
        result = subprocess.run(
            ["npx", "convex", "data", "users", "--prod", "--order", "desc", "--limit", "20"],
            capture_output=True,
            text=True,
            cwd=project_root,
            timeout=30,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    users = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    # Parse the table output — columns separated by |
    lines = result.stdout.strip().split("\n")
    # Find header line to get column positions
    header_line = None
    for i, line in enumerate(lines):
        if "email" in line and "name" in line and "_creationTime" in line:
            header_line = i
            break

    if header_line is None:
        return []

    headers = [h.strip() for h in lines[header_line].split("|")]
    # Skip the separator line (dashes)
    data_start = header_line + 2 if header_line + 1 < len(lines) and "---" in lines[header_line + 1] else header_line + 1

    for line in lines[data_start:]:
        cols = [c.strip().strip('"') for c in line.split("|")]
        if len(cols) < len(headers):
            continue

        row = dict(zip(headers, cols))
        created_str = row.get("createdAt", row.get("_creationTime", ""))

        # Parse creation time
        try:
            if isinstance(created_str, str) and "T" in created_str:
                created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            else:
                # _creationTime is epoch ms
                created_dt = datetime.fromtimestamp(float(created_str) / 1000, tz=timezone.utc)
        except (ValueError, TypeError):
            continue

        # Parse onboarding steps
        onboarding = row.get("onboarding", "")
        completed_steps = []
        if "completedSteps" in onboarding:
            match = re.search(r'completedSteps":\s*\[([^\]]*)\]', onboarding)
            if match:
                steps_raw = match.group(1)
                completed_steps = [s.strip().strip('"') for s in steps_raw.split(",") if s.strip().strip('"')]

        users.append({
            "email": row.get("email", "?"),
            "name": row.get("name", "?"),
            "created_at": created_dt.isoformat(),
            "created_dt": created_dt,
            "completed_steps": completed_steps,
            "is_in_period": created_dt >= cutoff,
        })

    return users


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def format_timestamp(ts: str) -> str:
    """Format an ISO timestamp with explicit UTC and IST representations."""
    if not ts:
        return "?"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        utc = dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        ist = dt.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S IST")
        return f"{utc} / {ist}"
    except (ValueError, AttributeError):
        return str(ts)[:19]


def print_text_report(data: dict, human_period: str, quiet: bool):
    """Print the structured text report to stdout."""
    now = format_timestamp(datetime.now(timezone.utc).isoformat())

    print()
    print("\u2550" * 50)
    print(f"  HeyDonna Activity Report")
    print("\u2550" * 50)
    print(f"  Period: {human_period}")
    print(f"  Generated: {now}")
    print()

    # --- Event Summary ---
    print("\u2500" * 40)
    print("  Event Summary")
    print("\u2500" * 40)
    summary = data.get("event_summary", [])
    total = 0
    level_counts = {}
    for row in summary:
        level = row.get("level")
        if level is None or level == "":
            level = "(unlabeled)"
        count = int(row.get("count_", 0))
        level_counts[level] = count
        total += count

    print(f"  Total: {format_number(total)}")
    # Print in preferred order
    for lvl in ["error", "warn", "info", "debug", "(unlabeled)"]:
        if lvl in level_counts:
            print(f"    {lvl}: {format_number(level_counts[lvl])}")
    # Any remaining levels not in the preferred order
    for lvl, cnt in level_counts.items():
        if lvl not in ["error", "warn", "info", "debug", "(unlabeled)"]:
            print(f"    {lvl}: {format_number(cnt)}")
    print()

    # --- Errors Detail ---
    print("\u2500" * 40)
    print("  Errors (last 10)")
    print("\u2500" * 40)
    errors = data.get("errors_detail", [])
    if not errors:
        print("  (none)")
    for err in errors:
        ts = format_timestamp(err.get("_time", ""))
        action = err.get("action", "?")
        email = err.get("email", "?")
        meta = err.get("meta", "")
        if isinstance(meta, dict):
            meta = json.dumps(meta, separators=(",", ":"))
        print(f"  [{ts}] action={action} email={email}")
        if meta:
            print(f"    meta: {truncate(str(meta), 120)}")
    print()

    # --- Product Activity ---
    print("\u2500" * 40)
    print("  Product Activity")
    print("\u2500" * 40)
    product = data.get("product_activity", [])
    product_counts = {}
    for row in product:
        action = row.get("action", "")
        count = int(row.get("count_", 0))
        product_counts[action] = count

    for action_key, label in ACTION_LABELS.items():
        count = product_counts.get(action_key, 0)
        print(f"    {label}: {format_number(count)}")
    print()

    # --- Save Suppression (editor family) ---
    print("\u2500" * 40)
    print("  Save Suppression")
    print("\u2500" * 40)
    save_suppression = data.get("save_suppression", {})
    if not save_suppression:
        print("  (none)")
    else:
        total_saves = int(save_suppression.get("total", 0))
        print(f"    Total: {format_number(total_saves)}")
        if "unique_save_escape_unsynced" in save_suppression:
            print(
                "    Unique save_escape_unsynced rows: "
                f"{format_number(int(save_suppression['unique_save_escape_unsynced']))}"
            )
        by_action = save_suppression.get("by_action", {})
        for action, count in by_action.items():
            print(f"    {action}: {format_number(count)}")
        unresolved = save_suppression.get("unresolved", {})
        if unresolved:
            rendered = ", ".join(f"{k}={format_number(v)}" for k, v in unresolved.items())
            print(f"    Unresolved family: {rendered}")
        affected = save_suppression.get("affected", [])
        if affected:
            print("    Affected (top 10 by count):")
            for row in affected[:10]:
                file_id = str(row.get("fileId") or "?")
                action = str(row.get("action") or "?")
                branch = str(row.get("branch") or "?")
                count = int(row.get("count", 0))
                first = str(row.get("first") or "?")[:19]
                last = str(row.get("last") or "?")[:19]
                print(f"      {file_id} {action} branch={branch} x{format_number(count)} {first}..{last}")
    print()

    # --- Auto-Process Runs ---
    print("\u2500" * 40)
    print("  Auto-Process Runs")
    print("\u2500" * 40)
    auto_summary = data.get("auto_process_summary", {})
    if not auto_summary:
        print("  (none)")
    else:
        print(f"    Runs started: {format_number(auto_summary.get('runs_started', 0))}")
        print(f"    Proofread stages completed: {format_number(auto_summary.get('proofreads_completed', 0))}")
        print(f"    Format stages completed: {format_number(auto_summary.get('formats_completed', 0))}")
        print(f"    Successful terminals: {format_number(auto_summary.get('terminals_success', 0))}")
        print(f"    Failed terminals: {format_number(auto_summary.get('terminals_failed', 0))}")
        sources = auto_summary.get("trigger_sources", {})
        if sources:
            rendered = ", ".join(f"{name}={count}" for name, count in sorted(sources.items()))
            print(f"    Trigger sources: {rendered}")
    print()

    # --- Active Users ---
    print("\u2500" * 40)
    print("  Active Users")
    print("\u2500" * 40)
    active_users = data.get("active_users", [])
    if not active_users:
        print("  (none)")
    else:
        print(f"  Unique users with events: {len(active_users)}")
        for user in active_users[:15]:  # Top 15
            email = user.get("email") or "?"
            actions = int(user.get("actions", 0))
            last_seen_raw = user.get("last_seen", "")
            # Handle nanosecond timestamps from Axiom aggregations
            if isinstance(last_seen_raw, (int, float)) or (isinstance(last_seen_raw, str) and last_seen_raw.isdigit()):
                try:
                    ts_ns = int(last_seen_raw)
                    ts_s = ts_ns / 1_000_000_000 if ts_ns > 1e15 else ts_ns / 1000 if ts_ns > 1e12 else ts_ns
                    dt = datetime.fromtimestamp(ts_s, tz=timezone.utc)
                    last_seen = (
                        dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                        + " / "
                        + dt.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")
                    )
                except (ValueError, OSError):
                    last_seen = "?"
            else:
                last_seen = format_timestamp(str(last_seen_raw))
            # Mask email for privacy
            parts = email.split("@") if email else ["?"]
            if len(parts) == 2 and len(parts[0]) > 2:
                masked = parts[0][:2] + "***@" + parts[1]
            else:
                masked = email
            print(f"    {masked}: {format_number(actions)} events (last: {last_seen})")
    print()

    # --- Sign-ups ---
    print("\u2500" * 40)
    print("  Sign-ups")
    print("\u2500" * 40)
    signups = data.get("signups", [])
    if not signups:
        print("  (Convex unavailable)")
    else:
        in_period = [s for s in signups if s.get("is_in_period")]
        print(f"  New in period: {len(in_period)}")
        if in_period:
            for s in in_period:
                name = s.get("name", "?")
                email = s.get("email", "?")
                steps = s.get("completed_steps", [])
                if not steps:
                    status = "signed up only"
                elif "review_changes" in steps or "use_ai_feature" in steps:
                    status = "full onboarding"
                elif "open_editor" in steps:
                    status = "opened editor"
                elif "create_project" in steps:
                    status = "created project"
                else:
                    status = f"{len(steps)} steps"
                print(f"    {name} ({email}) — {status}")
        else:
            print("  (no new sign-ups)")

        # Show recent sign-ups for context
        recent = signups[:5]
        if recent and not in_period:
            print(f"\n  Most recent (before period):")
            for s in recent:
                name = s.get("name", "?")
                email = s.get("email", "?")
                created = s.get("created_at", "?")
                steps = s.get("completed_steps", [])
                if not steps:
                    status = "no onboarding"
                elif "review_changes" in steps or "use_ai_feature" in steps:
                    status = "full"
                elif "open_editor" in steps:
                    status = "partial"
                else:
                    status = f"{len(steps)} steps"
                try:
                    dt = datetime.fromisoformat(created)
                    date_str = dt.strftime("%b %d")
                except (ValueError, TypeError):
                    date_str = "?"
                print(f"    {name} ({email}) — {date_str} — {status}")
    print()

    # --- Hourly Distribution ---
    if not quiet:
        print("\u2500" * 40)
        print("  Hourly Distribution")
        print("\u2500" * 40)
        hourly = data.get("hourly_distribution", [])
        if not hourly:
            print("  (none)")
        else:
            # Parse all entries and find max for bar scaling
            max_count = 0
            hourly_parsed = []
            for row in hourly:
                ts = row.get("_time", "")
                count = int(row.get("count_", 0))
                max_count = max(max_count, count)
                hourly_parsed.append((ts, count))

            bar_max_width = 30
            for ts, count in hourly_parsed:
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    hour_label = (
                        dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                        + " / "
                        + dt.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")
                    )
                except (ValueError, AttributeError):
                    hour_label = str(ts)[:16]
                bar_len = int((count / max_count) * bar_max_width) if max_count > 0 else 0
                bar = "\u2588" * bar_len
                print(f"  {hour_label}  {bar} {format_number(count)}")
        print()

    print("\u2550" * 50)
    print()


def main():
    parser = argparse.ArgumentParser(description="Generate HeyDonna activity report from Axiom logs")
    period_group = parser.add_mutually_exclusive_group()
    period_group.add_argument("--hours", type=int, help="Period in hours (e.g., 24)")
    period_group.add_argument("--days", type=int, help="Period in days (e.g., 7)")
    parser.add_argument("--start", help="Explicit UTC window start (ISO-8601)")
    parser.add_argument("--end", help="Explicit UTC window end (ISO-8601)")
    parser.add_argument(
        "--frozen-proof",
        action="store_true",
        help="Require the controlling frozen window and 80/4890 suppression result",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON instead of formatted text")
    parser.add_argument("--quiet", action="store_true", help="Suppress hourly distribution section")
    parser.add_argument(
        "--soft-fail",
        action="store_true",
        help="Include pipeline soft-fail / validator-reject rates (Rajiv directive 2026-05-11)",
    )
    args = parser.parse_args()

    if (args.start is None) != (args.end is None):
        parser.error("--start and --end must be supplied together")
    if args.start and (args.hours is not None or args.days is not None):
        parser.error("explicit --start/--end cannot be combined with --hours/--days")
    if not args.start and args.hours is None and args.days is None:
        parser.error("supply --hours/--days or explicit --start and --end")

    def parse_utc(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            parser.error(f"invalid ISO-8601 timestamp: {value} ({error})")
        if parsed.tzinfo is None:
            parser.error(f"timestamp must include a timezone: {value}")
        return parsed.astimezone(timezone.utc)

    if args.start:
        start_dt = parse_utc(args.start)
        end_dt = parse_utc(args.end)
        if end_dt <= start_dt:
            parser.error("--end must be after --start")
        hours = int((end_dt - start_dt).total_seconds() / 3600)
        human_period = (
            f"{start_dt.strftime('%Y-%m-%dT%H:%M:%SZ')} .. "
            f"{end_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        )
    else:
        hours = args.hours if args.hours is not None else args.days * 24
        if hours <= 0:
            parser.error("period must be positive")
        human_period = build_human_period(hours)
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(hours=hours)

    start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if args.frozen_proof and (start_iso, end_iso) != (FROZEN_START, FROZEN_END):
        parser.error(f"--frozen-proof requires {FROZEN_START}..{FROZEN_END}")

    token = load_token()

    print(f"Querying Axiom for {human_period}...", file=sys.stderr)

    # Query 1: Event summary
    print("  [1/7] Event summary by level...", file=sys.stderr)
    event_summary = query_event_summary(token, start_iso, end_iso)
    time.sleep(QUERY_DELAY_SECONDS)

    # Query 2: Errors detail
    print("  [2/7] Error details...", file=sys.stderr)
    errors_detail = query_errors_detail(token, start_iso, end_iso)
    time.sleep(QUERY_DELAY_SECONDS)

    # Query 3: Product activity
    print("  [3/7] Product activity...", file=sys.stderr)
    product_activity = query_product_activity(token, start_iso, end_iso)
    time.sleep(QUERY_DELAY_SECONDS)

    # Query 4: Auto-process stats
    print("  [4/7] Auto-process pipeline...", file=sys.stderr)
    auto_process = query_auto_process(token, start_iso, end_iso)
    auto_process_summary = build_auto_process_summary(auto_process)
    time.sleep(QUERY_DELAY_SECONDS)

    # Query 5: Hourly distribution
    hourly_distribution = []
    if not args.quiet:
        print("  [5/7] Hourly distribution...", file=sys.stderr)
        hourly_distribution = query_hourly_distribution(token, start_iso, end_iso)
        time.sleep(QUERY_DELAY_SECONDS)
    else:
        print("  [5/7] Hourly distribution... (skipped, --quiet)", file=sys.stderr)

    # Query 6: Active users
    print("  [6/7] Active users...", file=sys.stderr)
    active_users = query_active_users(token, start_iso, end_iso)
    time.sleep(QUERY_DELAY_SECONDS)

    # Query 7: Save-suppression family (editor)
    print("  [7/8] Save suppression...", file=sys.stderr)
    save_suppression_rows = query_save_suppression(token, start_iso, end_iso)
    save_suppression = build_save_suppression_summary(save_suppression_rows)
    if args.frozen_proof:
        expected = (80, 4890)
        observed = (
            int(save_suppression["unique_save_escape_unsynced"]),
            int(save_suppression["suppression_family_rows"]),
        )
        if observed != expected:
            parser.error(
                "frozen suppression proof mismatch: "
                f"observed unique_save_escape_unsynced={observed[0]}, "
                f"suppression_family_rows={observed[1]}; expected 80/4890"
            )
    time.sleep(QUERY_DELAY_SECONDS)

    # Query 8: Sign-ups from Convex
    print("  [8/8] Sign-ups (Convex prod)...", file=sys.stderr)
    signups = fetch_convex_signups(hours)

    # Query 8: Pipeline soft-fail / validator-reject rates (optional)
    soft_fail_report = None
    if args.soft_fail:
        time.sleep(QUERY_DELAY_SECONDS)
        print("  [8/10] Pipeline soft-fail counts...", file=sys.stderr)
        soft_fail_counts = query_soft_fail_counts(token, start_iso, end_iso)
        time.sleep(QUERY_DELAY_SECONDS)
        print("  [9/10] Convex-side nested proceedings-degraded...", file=sys.stderr)
        convex_proceedings_degraded = query_convex_proceedings_degraded(token, start_iso, end_iso)
        time.sleep(QUERY_DELAY_SECONDS)
        print("  [10/11] Auto-process total (denominator)...", file=sys.stderr)
        total_runs = query_auto_process_total(token, start_iso, end_iso)
        time.sleep(QUERY_DELAY_SECONDS)
        print("  [11/11] Auto-process by trigger source (#6249)...", file=sys.stderr)
        trigger_source_rows = query_auto_process_by_trigger_source(token, start_iso, end_iso)
        trigger_source_breakdown = {
            row.get("trigger_source") or "unknown": int(row.get("count_", 0))
            for row in trigger_source_rows
        }
        soft_fail_report = build_soft_fail_report(
            soft_fail_counts, total_runs, convex_proceedings_degraded,
            trigger_source_breakdown=trigger_source_breakdown,
        )

    print("  Done.", file=sys.stderr)

    data = {
        "period": human_period,
        "window": {"start": start_iso, "end": end_iso},
        "generated": datetime.now(timezone.utc).isoformat(),
        "event_summary": event_summary,
        "errors_detail": errors_detail,
        "product_activity": product_activity,
        "auto_process": auto_process,
        "auto_process_summary": auto_process_summary,
        "hourly_distribution": hourly_distribution,
        "active_users": active_users,
        "save_suppression": save_suppression,
        "signups": signups,
    }
    if soft_fail_report is not None:
        data["soft_fail"] = soft_fail_report

    if args.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        print_text_report(data, human_period, args.quiet)
        if soft_fail_report is not None:
            print_soft_fail_report(soft_fail_report)


if __name__ == "__main__":
    main()
