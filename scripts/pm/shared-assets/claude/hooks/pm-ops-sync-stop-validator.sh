#!/usr/bin/env bash
# Stop hook: surface due executable PM obligations before the PM turn closes.

set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

PM_OPS="${PM_OPS:-/Users/rajiv/.claude/scripts/pm-ops.py}"
LOG="${PM_OPS_STOP_LOG:-/tmp/pm-ops-sync-stop-validator.log}"
HOOK_INPUT="$(cat 2>/dev/null || true)"

[ -x "$PM_OPS" ] || exit 0

STATUS_JSON=$(python3 "$PM_OPS" status --format json --read-only 2>>"$LOG" || echo '{}')

python3 - "$STATUS_JSON" "$HOOK_INPUT" <<'PYEOF'
import json
import sqlite3
import sys
from datetime import datetime, timezone


# These obligation kinds are executable PM/CTO handoffs even when older
# producers did not persist evidence_json.pm_stop_actionable. Waiting and
# in-flight kinds (for example dependency_watch and ci_rerun_after_preflight)
# are intentionally absent so the Stop hook cannot deadlock on passive work.
DERIVED_EXECUTABLE_KINDS = {
    # Communications formerly checked by direct Stop validators.
    "alert",
    "customer_followup",
    "customer_incident",
    # CI/release and cleanup.
    "blocked_rework",
    "ci_rework",
    "ci_local_preflight",
    "ci_reconcile",
    "cleanup",
    "cleanup_closeout",
    "cleanup_pr",
    "cto_rescue",
    "evidence_gate",
    "merge_ready_invalid",
    "pm_gate",
    "pm_review_pending",
    # Capture terminal/rework work.
    "capture_alert",
    "capture_closure_queued",
    "capture_local_preflight",
    "capture_rework_assignment",
    # Kanban and post-issue routing drift.
    "issue_routing",
    "pr_state_missing",
    "pr_state_reconcile",
    "pr_scope_audit",
    "rescope_final_patch",
    "rescope_override",
    "rescope_split_execution",
    "review_loop_rescope",
    "slot_state_drift",
    # Slot-to-PM nudges are created by pm-context-injector on every nudge
    # arrival; PM must process them (assign a free slot / release an
    # occupied slot at >=20m) before stopping.
    "slot_nudge",
    "stale_slot_label",
    # Session clearing is produced by the heartbeat and consumed as an
    # obligation; Stop must not run its former bespoke validator.
    "session_age_clear",
    "pm-self-clear",
}

# Once the review wait has expired, this continuation is the release handoff
# itself. Resolving an unrelated obligation in the same turn must not hide it;
# otherwise busy PM turns can starve a ready PR indefinitely.
NON_WAIVABLE_CONTINUATION_KINDS = {
    "pm_review_pending",
    "review_loop_rescope",
}


try:
    status = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)
try:
    hook_input = json.loads(sys.argv[2] or "{}")
except Exception:
    hook_input = {}


def current_turn_started_at() -> str | None:
    """Return the latest real user prompt timestamp from this transcript.

    Tool results and Stop-hook feedback are stored as user records too. They do
    not begin a new PM turn, so exclude non-string content and isMeta records.
    """
    transcript_path = hook_input.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    latest = None
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as transcript:
            for raw in transcript:
                try:
                    entry = json.loads(raw)
                except Exception:
                    continue
                if entry.get("type") != "user" or entry.get("isMeta"):
                    continue
                message = entry.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                timestamp = entry.get("timestamp")
                if isinstance(content, str) and isinstance(timestamp, str) and timestamp:
                    latest = timestamp
    except OSError:
        return None
    return latest


PM_AGE_DUE_SECONDS = 6 * 60 * 60


def parse_ts(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def current_session_age():
    session_id = hook_input.get("session_id")
    transcript_path = hook_input.get("transcript_path")
    if not isinstance(session_id, str) or not session_id.strip():
        return None, None, "current_session_identity_unavailable"
    if not isinstance(transcript_path, str) or not transcript_path:
        return session_id, None, "current_session_age_unavailable"
    started_at = None
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as transcript:
            for raw in transcript:
                try:
                    entry = json.loads(raw)
                except Exception:
                    continue
                if entry.get("isSidechain") is True:
                    continue
                started_at = parse_ts(entry.get("timestamp"))
                if started_at is not None:
                    break
    except OSError:
        return session_id, None, "current_session_age_unavailable"
    if started_at is None:
        return session_id, None, "current_session_age_unavailable"
    age_seconds = (datetime.now(timezone.utc) - started_at).total_seconds()
    if age_seconds < 0:
        return session_id, None, "current_session_age_unavailable"
    return session_id, age_seconds, None


def pm_self_clear_decision(row, evidence):
    if not isinstance(evidence, dict):
        return False, "pm_self_clear_evidence_unavailable"
    current_session_id, age_seconds, age_reason = current_session_age()
    recorded_session_id = evidence.get("session_id")
    if not isinstance(recorded_session_id, str) or not recorded_session_id:
        return False, "pm_self_clear_evidence_unavailable"
    if parse_ts(evidence.get("session_started_at")) is None:
        return False, "pm_self_clear_evidence_unavailable"
    if not current_session_id:
        return False, "current_session_identity_unavailable"
    if recorded_session_id != current_session_id:
        return False, "current_session_mismatch"
    if age_reason or age_seconds is None:
        return False, age_reason or "current_session_age_unavailable"
    if hook_input.get("stop_hook_active") is True:
        return False, "stop_hook_active"
    if age_seconds <= PM_AGE_DUE_SECONDS:
        return False, "current_session_not_due"
    return True, "pm_self_clear_pending"

db_path = status.get("db") or (
    "/Users/rajiv/.claude/projects/"
    "-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db"
)
p0_actions = []
due_actions = []
continuation_actions = []
deferred_pm_self_clear = []
resolved_this_turn = False
turn_started_at = current_turn_started_at()

try:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
    con.row_factory = sqlite3.Row
    p0_rows = con.execute(
        """
        SELECT id, title, required_action, target_id
        FROM obligations
        WHERE status='open'
          AND kind='p0_escalation'
          AND (suppress_until IS NULL OR suppress_until='' OR suppress_until <= strftime('%Y-%m-%dT%H:%M:%SZ','now'))
          AND (
            (next_review_at IS NOT NULL AND next_review_at != '' AND next_review_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            OR (created_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now','-30 minutes'))
          )
        ORDER BY updated_at DESC, id DESC
        LIMIT 3
        """
    ).fetchall()

    candidates = con.execute(
        """
        SELECT id, kind, severity, title, required_action, target_type,
               target_id, pr, issue, slot, owner, created_at, evidence_json
        FROM obligations
        WHERE status='open'
          AND severity IN ('critical','high')
          AND (
            horizon='hourly'
            OR (kind IN ('session_age_clear', 'pm-self-clear') AND horizon='heartbeat')
          )
          AND COALESCE(required_action,'') != ''
          AND lower(COALESCE(owner,'')) != 'rajiv'
          AND (suppress_until IS NULL OR suppress_until='' OR suppress_until <= strftime('%Y-%m-%dT%H:%M:%SZ','now'))
          AND (next_review_at IS NULL OR next_review_at='' OR next_review_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        ORDER BY CASE
                   WHEN pr IS NOT NULL AND kind != 'cleanup_pr' THEN 0
                   WHEN kind = 'cleanup_pr' THEN 1
                   ELSE 2
                 END,
                 CASE severity WHEN 'critical' THEN 0 ELSE 1 END,
                 created_at ASC, id ASC
        """
    ).fetchall()
    if turn_started_at:
        resolved_this_turn = con.execute(
            """
            SELECT 1
            FROM obligations
            WHERE status='resolved'
              AND resolved_at IS NOT NULL
              AND julianday(resolved_at) >= julianday(?)
            LIMIT 1
            """,
            (turn_started_at,),
        ).fetchone() is not None
    con.close()

    for row in p0_rows:
        title = row["title"] or f"P0 escalation obligation {row['id']}"
        target = row["target_id"] or "<thread_ts>"
        action = row["required_action"] or (
            "Invoke Skill(alert-processing); inspect the alert thread, then "
            f"execute the canonical escalation for {target}."
        )
        p0_actions.append(f"obligation:{row['id']} {title}. {action}")

    for row in candidates:
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
        except Exception:
            evidence = {}
        if row["kind"] == "pm-self-clear":
            actionable, pm_reason = pm_self_clear_decision(row, evidence)
            if not actionable:
                deferred_pm_self_clear.append(f"obligation:{row['id']}:{pm_reason}")
                continue
        explicitly_actionable = evidence.get("pm_stop_actionable") in (1, True)
        derived_actionable = row["kind"] in DERIVED_EXECUTABLE_KINDS
        if not explicitly_actionable and not derived_actionable:
            continue

        title = row["title"] or f"PM obligation {row['id']}"
        target = f"{row['target_type'] or 'target'}:{row['target_id'] or 'unknown'}"
        if row["pr"] is not None:
            target += f" pr:{row['pr']}"
        elif row["issue"] is not None:
            target += f" issue:{row['issue']}"
        if row["slot"] is not None:
            target += f" slot:{row['slot']}"
        rendered_action = (
            f"obligation:{row['id']} {title} ({target}). {row['required_action']}"
        )
        due_actions.append(rendered_action)
        if row["kind"] in NON_WAIVABLE_CONTINUATION_KINDS:
            continuation_actions.append(rendered_action)
except Exception:
    sys.exit(0)

hard = []
if p0_actions:
    hard.append("P0 escalation call pending: " + " | ".join(p0_actions))
if continuation_actions:
    shown = continuation_actions[:3]
    remaining = len(continuation_actions) - len(shown)
    suffix = f" | remaining_continuations={remaining}" if remaining else ""
    hard.append(
        f"{len(continuation_actions)} release-critical PM review continuations "
        "are due and cannot be waived by unrelated obligation progress. "
        "Complete one through its canonical typed transition before stopping. "
        "Top actions: "
        + " | ".join(shown)
        + suffix
    )
elif due_actions and not resolved_this_turn:
    shown = due_actions[:3]
    remaining = len(due_actions) - len(shown)
    suffix = f" | remaining_due={remaining}" if remaining else ""
    hard.append(
        f"{len(due_actions)} executable PM obligations are due. "
        "Resolve at least one obligation in this PM turn before stopping; "
        "do not use raw label/state edits. Top actions: "
        + " | ".join(shown)
        + suffix
    )

if not hard:
    if deferred_pm_self_clear:
        print(json.dumps({
            "kind": "pm-self-clear",
            "status": "deferred",
            "reason": ",".join(deferred_pm_self_clear),
        }, sort_keys=True))
    sys.exit(0)

message = "[PM_OPS_ACTION_REQUIRED] " + "; ".join(hard)
print(json.dumps({"decision": "block", "message": message, "reason": message}))
PYEOF
