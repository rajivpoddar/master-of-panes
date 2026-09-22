#!/usr/bin/env python3
"""PM-owned ops ledger for HeyDonna control-plane state.

This is intentionally local and boring: SQLite is the durable PM workflow
ledger, while GitHub/MoP/Slack remain authoritative for their own live state.
pm-todo.md is rendered from this ledger plus fresh snapshots.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
from typing import Any


PROJECT_HOME = Path.home() / ".claude/projects/-Users-rajiv-Downloads-projects-heydonna-app"
DB_PATH = Path(os.environ.get("PM_OPS_DB", str(PROJECT_HOME / "state/pm-ops.db")))
TODO_PATH = PROJECT_HOME / "memory/pm-todo.md"
ARCHIVE_DIR = PROJECT_HOME / "memory/archive"
MOP_DB = Path.home() / ".claude/plugins/cache/rajiv-plugins/master-of-panes/1.0.0/data/mop.db"
REPO = "heydonna-app/heydonna-app"
_REPO_OWNER, _REPO_NAME = REPO.split("/", 1)

# GraphQL field selections mirroring the `gh <kind> view --json` shapes the
# per-number helpers return, so batched nodes are drop-in replacements.
_GRAPHQL_OBJECT_FIELDS = {
    "pullRequest": "number state mergedAt labels(first:100){nodes{name}}",
    "issue": "number state closedAt updatedAt labels(first:100){nodes{name}}",
}
HORIZONS = ("hourly", "heartbeat", "daily")
HORIZON_ORDER = {name: idx for idx, name in enumerate(HORIZONS)}
PR_ASSIGNMENT_SATISFIED_KINDS = {
    "blocked_rework",
    "ci-watch",
    "ci_label_reconcile",
    "ci_rework",
    "ci_watch",
    "pm_review",
    "pr_state_reconcile",
    "rework",
    "rework_slot_idle",
    "slot_state_drift",
    "stale_slot_label",
}
PR_PM_GATE_SUPERSEDED_KINDS = {
    "blocked_rework",
    "ci_label_reconcile",
    "ci_rework",
    "ci-watch",
    "ci_watch",
    "pm_gate_blocker",
    "pm_review",
    "pr_state_reconcile",
    "rework",
    "rework_slot_idle",
    "slot_state_drift",
    "stale_slot_label",
}
PR_REWORK_CANONICAL_KINDS = {
    "blocked_rework",
    "ci_rework",
    "rework",
}

_GH_BIN_CACHE: str | None = None
_GH_BIN_RESOLVED = False


def gh_bin() -> str | None:
    """Resolve the gh binary, preferring PATH then common Homebrew locations."""
    global _GH_BIN_CACHE, _GH_BIN_RESOLVED
    if _GH_BIN_RESOLVED:
        return _GH_BIN_CACHE
    found = shutil.which("gh")
    if found:
        _GH_BIN_CACHE = found
    else:
        for candidate in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh"):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                _GH_BIN_CACHE = candidate
                break
    _GH_BIN_RESOLVED = True
    return _GH_BIN_CACHE


def normalize_horizon(value: str | None) -> str | None:
    if not value:
        return None
    lowered = value.strip().lower()
    return lowered if lowered in HORIZON_ORDER else None


def obligation_text(row: sqlite3.Row | dict[str, Any]) -> str:
    parts = []
    for key in ("kind", "target_type", "owner", "title", "required_action", "blocker"):
        try:
            parts.append(str(row[key] or ""))
        except Exception:
            continue
    return " ".join(parts).lower()


def classify_obligation_horizon(row: sqlite3.Row | dict[str, Any], prefer_explicit: bool = True) -> str:
    # P0 escalation is a heartbeat-owned safety backstop.  Check the kind
    # before honoring a caller-supplied horizon so legacy/hourly producers
    # cannot create a second scheduled escalation cadence.
    try:
        kind = str(row["kind"] or "").lower()
    except Exception:
        kind = ""
    if kind == "p0_escalation":
        return "heartbeat"

    try:
        explicit = normalize_horizon(row["horizon"])
    except Exception:
        explicit = None
    if prefer_explicit and explicit:
        return explicit

    try:
        kind = str(row["kind"] or "").lower()
        target_type = str(row["target_type"] or "").lower()
        severity = str(row["severity"] or "").lower()
        owner = str(row["owner"] or "").lower()
        pr = row["pr"]
        slot = row["slot"]
    except Exception:
        kind = ""
        target_type = ""
        severity = ""
        owner = ""
        pr = None
        slot = None

    text = obligation_text(row)

    daily_kinds = {
        "ready_pool",
        "backlog",
        "followup",
        "pending_rajiv",
        "customer_incident",
        "customer_followup",
        "parking_review",
        "priority_review",
        "daily_priority",
    }
    heartbeat_kinds = {
        "session_age_clear",
        "stale_process_cleanup",
        "pm_todo_drift",
        "label_mop_drift",
        "issue_mop_drift",
        "audit_runner_health",
        "slack_bridge_health",
        "mop_health",
        "kanban_freshness",
        "pm_ops_sync",
        "infra",
        "infra_followup",
        "ops_hygiene",
        "stale_tmp_cleanup",
        "p0_escalation",
    }
    hourly_kinds = {
        "ci_reconcile",
        "ci_watch",
        "ci_failure",
        "dispatch",
        "dispatch_lock",
        "slot_dispatch",
        "issue_routing",
        "rework",
        "codex_rework",
        "slot_state_drift",
        "capacity",
        "cleanup_pr",
        "merge_ready_review",
        "pm_gate_blocker",
        "pm_review",
        "pm_review_complete",
        "readiness",
        "alert",
        "campaign_lock",
        "pm_packet_error",
        "slot_fabrication_reset",
        "explore_issue_required",
    }

    if kind in daily_kinds or owner == "rajiv":
        return "daily"
    if kind in heartbeat_kinds:
        return "heartbeat"
    if kind in hourly_kinds:
        return "hourly"

    if any(token in text for token in ("session-age", "session age", "clear_due", "stale process", "pm_todo_drift", "pm-todo drift", "audit runner", "slack bridge", "kanban freshness")):
        return "heartbeat"
    if any(token in text for token in ("ready pool", "backlog", "blocked on rajiv", "awaiting rajiv", "pending rajiv", "priority review", "morning brief")):
        return "daily"
    if pr is not None or slot is not None or target_type in {"pr", "slot"}:
        return "hourly"
    if target_type == "issue" and severity in {"critical", "high"}:
        return "hourly"
    return "daily"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def connect() -> sqlite3.Connection:
    ensure_parent(DB_PATH)
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=15000")
    return con


def connect_readonly() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def init_db() -> None:
    con = connect()
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL,
          source TEXT NOT NULL,
          actor TEXT,
          event_type TEXT NOT NULL,
          target_type TEXT,
          target_id TEXT,
          pr INTEGER,
          issue INTEGER,
          slot INTEGER,
          head_sha TEXT,
          payload_json TEXT NOT NULL DEFAULT '{}',
          dedupe_key TEXT UNIQUE
        );

        CREATE TABLE IF NOT EXISTS obligations (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'open',
          kind TEXT NOT NULL,
          severity TEXT NOT NULL DEFAULT 'normal',
          target_type TEXT,
          target_id TEXT,
          pr INTEGER,
          issue INTEGER,
          slot INTEGER,
          owner TEXT,
          title TEXT,
          required_action TEXT,
          blocker TEXT,
          horizon TEXT NOT NULL DEFAULT 'hourly',
          next_review_at TEXT,
          last_surface_at TEXT,
          suppress_until TEXT,
          dedupe_group TEXT,
          evidence_json TEXT NOT NULL DEFAULT '{}',
          resolved_at TEXT,
          resolution_event_id INTEGER
        );

        CREATE TABLE IF NOT EXISTS snapshots (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL,
          source TEXT NOT NULL,
          target_type TEXT,
          target_id TEXT,
          payload_json TEXT NOT NULL DEFAULT '{}',
          status TEXT,
          observed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS sync_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL,
          reason TEXT,
          input_event_id_max INTEGER NOT NULL DEFAULT 0,
          obligations_open INTEGER NOT NULL DEFAULT 0,
          rendered_path TEXT,
          rendered_hash TEXT,
          stale_rows_pruned INTEGER NOT NULL DEFAULT 0,
          source_health_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS human_notes (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'open',
          target_type TEXT,
          target_id TEXT,
          body TEXT NOT NULL,
          source TEXT
        );

        CREATE TABLE IF NOT EXISTS transition_packets (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          packet_key TEXT NOT NULL UNIQUE,
          packet_type TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          event_file TEXT,
          pr INTEGER,
          issue INTEGER,
          slot INTEGER,
          head_sha TEXT,
          branch TEXT,
          cwd TEXT,
          review_proof TEXT,
          review_verdict TEXT,
          qa_proof TEXT,
          payload_json TEXT NOT NULL DEFAULT '{}',
          consumed_at TEXT,
          rejected_at TEXT,
          resolution_reason TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_events_target ON events(target_type, target_id);
        CREATE INDEX IF NOT EXISTS idx_events_pr ON events(pr);
        CREATE INDEX IF NOT EXISTS idx_events_issue ON events(issue);
        CREATE INDEX IF NOT EXISTS idx_obligations_open ON obligations(status, kind, target_type, target_id);
        CREATE INDEX IF NOT EXISTS idx_obligations_pr ON obligations(pr);
        CREATE INDEX IF NOT EXISTS idx_obligations_issue ON obligations(issue);
        CREATE INDEX IF NOT EXISTS idx_snapshots_target ON snapshots(source, target_type, target_id);
        CREATE INDEX IF NOT EXISTS idx_transition_packets_status ON transition_packets(status, packet_type, updated_at);
        CREATE INDEX IF NOT EXISTS idx_transition_packets_pr_head ON transition_packets(pr, head_sha);
        """
    )
    existing_cols = {row["name"] for row in con.execute("PRAGMA table_info(obligations)").fetchall()}
    for col, ddl in {
        "resolved_reason": "ALTER TABLE obligations ADD COLUMN resolved_reason TEXT",
        "superseded_by": "ALTER TABLE obligations ADD COLUMN superseded_by INTEGER",
        "external_state": "ALTER TABLE obligations ADD COLUMN external_state TEXT",
        "last_verified_at": "ALTER TABLE obligations ADD COLUMN last_verified_at TEXT",
        "horizon": "ALTER TABLE obligations ADD COLUMN horizon TEXT NOT NULL DEFAULT 'hourly'",
        "next_review_at": "ALTER TABLE obligations ADD COLUMN next_review_at TEXT",
        "last_surface_at": "ALTER TABLE obligations ADD COLUMN last_surface_at TEXT",
        "suppress_until": "ALTER TABLE obligations ADD COLUMN suppress_until TEXT",
        "dedupe_group": "ALTER TABLE obligations ADD COLUMN dedupe_group TEXT",
        # Existing installations created before resolution_event_id existed in
        # CREATE TABLE (added there only in 39d142ff6) lack the column; the
        # typed-resolution readers/writers would fail with "no such column"
        # (ob11012/ob11095 ordinal-1 FUNCTIONAL_BLOCK).  This PRAGMA-guarded
        # ALTER keeps the migration idempotent and fail-closed.
        "resolution_event_id": "ALTER TABLE obligations ADD COLUMN resolution_event_id INTEGER",
    }.items():
        if col not in existing_cols:
            con.execute(ddl)
    con.execute("CREATE INDEX IF NOT EXISTS idx_obligations_horizon ON obligations(status, horizon, updated_at)")
    known_classified_kinds = {
        "ready_pool",
        "backlog",
        "followup",
        "pending_rajiv",
        "customer_incident",
        "customer_followup",
        "parking_review",
        "priority_review",
        "daily_priority",
        "session_age_clear",
        "stale_process_cleanup",
        "pm_todo_drift",
        "label_mop_drift",
        "issue_mop_drift",
        "audit_runner_health",
        "slack_bridge_health",
        "mop_health",
        "kanban_freshness",
        "pm_ops_sync",
        "infra",
        "infra_followup",
        "ops_hygiene",
        "stale_tmp_cleanup",
        "ci_reconcile",
        "ci_watch",
        "ci_failure",
        "dispatch",
        "dispatch_lock",
        "slot_dispatch",
        "issue_routing",
        "rework",
        "codex_rework",
        "slot_state_drift",
        "capacity",
        "cleanup_pr",
        "readiness",
        "alert",
        "campaign_lock",
        "p0_escalation",
        "explore_issue_required",
    }
    rows = con.execute("SELECT * FROM obligations WHERE status='open'").fetchall()
    for row in rows:
        kind = str(row["kind"] or "").lower()
        prefer_explicit = row["horizon"] in {"heartbeat", "daily"} and kind not in known_classified_kinds
        inferred = classify_obligation_horizon(row, prefer_explicit=prefer_explicit)
        if row["horizon"] != inferred:
            con.execute("UPDATE obligations SET horizon=? WHERE id=?", (inferred, int(row["id"])))
    con.commit()
    con.close()


def parse_payload(items: list[str] | None, raw_json: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if raw_json:
        try:
            loaded = json.loads(raw_json)
            if isinstance(loaded, dict):
                payload.update(loaded)
            else:
                payload["value"] = loaded
        except Exception:
            payload["raw"] = raw_json
    for item in items or []:
        if "=" in item:
            k, v = item.split("=", 1)
            payload[k] = v
        else:
            # --evidence item without "=": first try to parse it as JSON.  A
            # JSON object merges its fields so json_extract consumers (e.g.
            # the first_boundary_reservation release gate's
            # $.assignment_epoch/$.branch/$.head checks) see them instead of a
            # JSON-key wrapper; anything else keeps the bare-key shape.
            # incident first-boundary-reservation-evidence-ordering.
            try:
                loaded = json.loads(item)
            except Exception:
                loaded = None
            if isinstance(loaded, dict):
                payload.update(loaded)
            else:
                payload[item] = True
    return payload


def record_event(args: argparse.Namespace) -> int:
    init_db()
    payload = parse_payload(args.payload, args.payload_json)
    target_type = args.target_type
    target_id = args.target_id
    if not target_type:
        if args.pr:
            target_type, target_id = "pr", str(args.pr)
        elif args.issue:
            target_type, target_id = "issue", str(args.issue)
        elif args.slot:
            target_type, target_id = "slot", str(args.slot)
    dedupe_key = args.dedupe_key
    if not dedupe_key and args.dedupe:
        material = json.dumps(
            {
                "source": args.source,
                "event": args.event,
                "target_type": target_type,
                "target_id": target_id,
                "pr": args.pr,
                "issue": args.issue,
                "slot": args.slot,
                "head_sha": args.head_sha,
                "payload": payload,
            },
            sort_keys=True,
        )
        dedupe_key = hashlib.sha256(material.encode()).hexdigest()

    con = connect()
    cur = con.execute(
        """
        INSERT OR IGNORE INTO events
          (ts, source, actor, event_type, target_type, target_id, pr, issue, slot, head_sha, payload_json, dedupe_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            args.ts or utc_now(),
            args.source,
            args.actor or os.environ.get("USER") or "pm",
            args.event,
            target_type,
            target_id,
            args.pr,
            args.issue,
            args.slot,
            args.head_sha,
            json.dumps(payload, sort_keys=True),
            dedupe_key,
        ),
    )
    con.commit()
    event_id = int(cur.lastrowid or 0)
    con.close()
    if args.print_id:
        print(event_id)
    return event_id


def obligation_key(args: argparse.Namespace) -> tuple[str, str | None, str | None, int | None, int | None]:
    target_type = args.target_type
    target_id = args.target_id
    if not target_type:
        if args.pr:
            target_type, target_id = "pr", str(args.pr)
        elif args.issue:
            target_type, target_id = "issue", str(args.issue)
        elif args.slot:
            target_type, target_id = "slot", str(args.slot)
    return args.kind, target_type, target_id, args.pr, args.issue


def typed_resolution_exists(kind: str, target_type: str | None, target_id: str | None, pr: int | None, issue: int | None) -> bool:
    """True when a TYPED resolution (obligation-resolve receipt) exists for the
    (kind, target) class.

    Auto-producers consume typed resolutions: after PM resolves the class
    through the obligation-resolve command, the receipt row (resolution_event_id
    present) suppresses re-creation of the same kind+target (ob11012: ci_reconcile
    rows re-created x5 for #7093, cleanup_pr x4 for #7134 even after typed
    resolutions).  The typed receipt is the consumption signal -- never raw label
    reads; a sweep-resolved row without a receipt must NOT suppress the producer.
    """
    con = connect()
    try:
        row = con.execute(
            """
            SELECT 1 FROM obligations
            WHERE status='resolved' AND resolution_event_id IS NOT NULL
              AND kind=?
              AND COALESCE(target_type,'')=COALESCE(?,'')
              AND COALESCE(target_id,'')=COALESCE(?,'')
              AND COALESCE(pr,-1)=COALESCE(?,-1)
              AND COALESCE(issue,-1)=COALESCE(?,-1)
            LIMIT 1
            """,
            (kind, target_type, target_id, pr, issue),
        ).fetchone()
    finally:
        con.close()
    return row is not None


def record_resolution_receipt(con: sqlite3.Connection, kind: str, target_type: str | None, target_id: str | None, pr: int | None, issue: int | None, ids: list[int], reason: str | None, external_state: str | None, slot: int | None = None) -> None:
    """Mint the typed resolution receipt for the obligation-resolve command.

    One events row (event_type='obligation-resolve') plus the
    resolution_event_id link on the resolved obligation rows, in the caller's
    open transaction.  Only the typed resolve command mints receipts; machine
    reclassifications (resolve_obligation_ids) never call this, so a
    sweep-resolved row does not consume the producer class.
    """
    if not ids:
        return
    now = utc_now()
    cur = con.execute(
        """
        INSERT INTO events
          (ts, source, actor, event_type, target_type, target_id, pr, issue, slot, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now,
            "pm-ops-legacy",
            os.environ.get("USER") or "pm",
            "obligation-resolve",
            target_type,
            target_id,
            pr,
            issue,
            slot,
            json.dumps(
                {
                    "reason": reason,
                    "external_state": external_state,
                    "obligation_ids": ids,
                },
                sort_keys=True,
            ),
        ),
    )
    event_id = int(cur.lastrowid or 0)
    if event_id:
        placeholders = ",".join("?" for _ in ids)
        con.execute(
            f"UPDATE obligations SET resolution_event_id=? WHERE id IN ({placeholders})",
            [event_id, *ids],
        )


# ─── Durable-continuation writer contract ──────────────────────────────
# The reader (sakshi-heartbeat.py) accepts an exact-head continuation only when
# the kind is in CONTINUATION_KIND_LANES, the evidence carries exactly one
# accepted head key with a lowercase 40-hex value, and owner/required_action are
# concrete. These constants mirror that predicate as duplicated runtime values
# (no runtime dependency on the heartbeat); the focused contract test loads the
# reader source and asserts they stay equal. `durable_continuation` is
# deliberately ABSENT - it is not reader-recognized, so validating it would
# bless rows the reader rejects.
CONTINUATION_HEAD_KEYS = (
    "head",
    "head_sha",
    "headRefOid",
    "current_head",
    "current_head_sha",
)
CONTINUATION_KIND_LANES = {
    "ci_watch": "CI",
    "pr_admission": "CI",
    "capture_release": "capture",
    "capture_recovery": "capture",
    "pr_qa_pending": "repro/proof",
    "slot_ready_pending": "repro/proof",
    "slot_retask": "rework",
    "slot_rework": "rework",
    "dependency_wait": "dependency-blocked",
    "infra_blocker": "dependency-blocked",
    "rework": "rework-blocked",
    "rework_review": "rework-blocked",
    "ci_rework": "rework-blocked",
    "control_plane_defect": "rework-blocked",
    "followup": "rework-blocked",
}
CONTINUATION_PLACEHOLDERS = {
    "unknown",
    "none",
    "n/a",
    "cto-owned",
    "relay-only",
    "not-actionable",
}
CONTINUATION_HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
CONTINUATION_OWNER_RE = re.compile(r"^[^\s]{2,}$")


def continuation_upsert_refusal(*, kind, evidence, owner, required_action):
    """Return a typed refusal reason, or None when the upsert is well-formed.

    Pure: performs no DB access. Called before init_db()/connect() so a refusal
    writes zero rows and leaves no partial state.
    """
    if kind == "durable_continuation":
        # Explicitly refused: the literal kind is NOT reader-recognized, so blessing it would
        # create rows the reader then rejects. This is the defect this contract exists to stop.
        return "continuation_kind_not_reader_recognized"
    if kind not in CONTINUATION_KIND_LANES:
        return None  # ordinary non-continuation kind: existing behavior is preserved
    heads = []
    for key in CONTINUATION_HEAD_KEYS:
        if key not in evidence:
            continue
        value = evidence.get(key)
        if not isinstance(value, str) or not CONTINUATION_HEAD_RE.fullmatch(value):
            return "continuation_head_malformed"
        heads.append(value)
    if not heads:
        return "continuation_head_missing"
    if len(set(heads)) > 1:
        return "continuation_head_conflicting"
    # Mirror the reader exactly: _concrete_motion_token does value.strip(), requires a
    # full OPEN_PR_CONCRETE_TOKEN match (^[^\s]{2,}$), then rejects the placeholder set.
    owner_text = owner.strip() if isinstance(owner, str) else ""
    if not CONTINUATION_OWNER_RE.fullmatch(owner_text):
        return "continuation_owner_shape_invalid"
    if owner_text.lower() in CONTINUATION_PLACEHOLDERS:
        return "continuation_owner_placeholder"
    action_text = required_action.strip() if isinstance(required_action, str) else ""
    if not action_text or action_text.lower() in CONTINUATION_PLACEHOLDERS:
        return "continuation_action_placeholder"
    return None


def upsert_obligation(args: argparse.Namespace) -> int:
    # Validate BEFORE init_db()/connect(): a refused continuation upsert must write zero
    # rows and leave no partial state.
    _evidence_for_contract = parse_payload(args.evidence, args.evidence_json)
    _kind_for_contract = obligation_key(args)[0]
    _refusal = continuation_upsert_refusal(
        kind=_kind_for_contract,
        evidence=_evidence_for_contract if isinstance(_evidence_for_contract, dict) else {},
        owner=getattr(args, "owner", None),
        required_action=getattr(args, "action", None),
    )
    if _refusal:
        print(f"REFUSED: {_refusal}", file=sys.stderr)
        return 2
    init_db()
    now = utc_now()
    evidence = parse_payload(args.evidence, args.evidence_json)
    kind, target_type, target_id, pr, issue = obligation_key(args)
    horizon = normalize_horizon(getattr(args, "horizon", None))
    if kind == "p0_escalation":
        # Explicit --horizon hourly is retained for CLI compatibility but is
        # never allowed to move this obligation off the heartbeat surface.
        horizon = "heartbeat"
    elif horizon is None:
        horizon = classify_obligation_horizon(
            {
                "kind": kind,
                "severity": args.severity,
                "target_type": target_type,
                "target_id": target_id,
                "pr": pr,
                "issue": issue,
                "slot": args.slot,
                "owner": args.owner,
                "title": args.title,
                "required_action": args.action,
                "blocker": args.blocker,
                "horizon": None,
            }
        )
    con = connect()
    row = con.execute(
        """
        SELECT id FROM obligations
        WHERE status='open'
          AND kind=?
          AND COALESCE(target_type,'')=COALESCE(?,'')
          AND COALESCE(target_id,'')=COALESCE(?,'')
          AND COALESCE(pr,-1)=COALESCE(?,-1)
          AND COALESCE(issue,-1)=COALESCE(?,-1)
        ORDER BY id DESC LIMIT 1
        """,
        (kind, target_type, target_id, pr, issue),
    ).fetchone()
    if row:
        oid = int(row["id"])
        con.execute(
            """
            UPDATE obligations
            SET updated_at=?, severity=?, slot=COALESCE(?, slot), owner=COALESCE(?, owner),
                title=COALESCE(?, title), required_action=COALESCE(?, required_action),
                blocker=COALESCE(?, blocker), horizon=?, next_review_at=COALESCE(?, next_review_at),
                suppress_until=COALESCE(?, suppress_until), dedupe_group=COALESCE(?, dedupe_group),
                evidence_json=?
            WHERE id=?
            """,
            (
                now,
                args.severity,
                args.slot,
                args.owner,
                args.title,
                args.action,
                args.blocker,
                horizon,
                getattr(args, "next_review_at", None),
                getattr(args, "suppress_until", None),
                getattr(args, "dedupe_group", None),
                json.dumps(evidence, sort_keys=True),
                oid,
            ),
        )
    else:
        cur = con.execute(
            """
            INSERT INTO obligations
              (created_at, updated_at, status, kind, severity, target_type, target_id,
               pr, issue, slot, owner, title, required_action, blocker, horizon,
               next_review_at, suppress_until, dedupe_group, evidence_json)
            VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                now,
                kind,
                args.severity,
                target_type,
                target_id,
                pr,
                issue,
                args.slot,
                args.owner,
                args.title,
                args.action,
                args.blocker,
                horizon,
                getattr(args, "next_review_at", None),
                getattr(args, "suppress_until", None),
                getattr(args, "dedupe_group", None),
                json.dumps(evidence, sort_keys=True),
            ),
        )
        oid = int(cur.lastrowid)
    con.commit()
    con.close()
    if args.print_id:
        print(oid)
    return oid


def resolve_obligation(args: argparse.Namespace) -> None:
    init_db()
    now = utc_now()
    kind, target_type, target_id, pr, issue = obligation_key(args)
    exact_id = getattr(args, "id", None)
    if kind == "first_boundary_reservation" and exact_id is None:
        # A first_boundary_reservation authorizes exactly one release; it may
        # be resolved ONLY through the validated consumption path that binds
        # the exact row by --id after the release persists.  A key-based
        # resolve would also sweep sibling rows (e.g. a retry reservation
        # created for the next slot-ready attempt), which is the premature
        # resolution the incident first-boundary-reservation-evidence-ordering
        # observed (row 10616 resolved before the consuming slot-ready could
        # read it).  Fail closed instead.
        raise SystemExit(
            "obligation-resolve refused: first_boundary_reservation rows resolve only by exact --id after validated consumption"
        )
    con = connect()
    resolved_ids: list[int] = []
    receipt_slot: int | None = None
    if exact_id is not None:
        # Bind the exact-row resolve to the caller-declared kind (CONTROL_POINT:
        # exact-ID reservation consumption).  --kind is a required argument; an
        # exact-ID resolve that ignored it could consume ANY open obligation row
        # through the reservation path when a caller supplies an unrelated ID.
        row = con.execute(
            """
            SELECT id, kind, target_type, target_id, pr, issue, slot
            FROM obligations
            WHERE status='open' AND id=? AND kind=?
            """,
            (exact_id, kind),
        ).fetchone()
        if row is None:
            con.rollback()
            con.close()
            raise SystemExit(
                "obligation-resolve --id matched no open row id=%s kind=%s"
                % (exact_id, kind)
            )
        cur = con.execute(
            """
            UPDATE obligations
            SET status='resolved', updated_at=?, resolved_at=?,
                resolved_reason=COALESCE(?, resolved_reason),
                external_state=COALESCE(?, external_state),
                last_verified_at=?
            WHERE status='open' AND id=? AND kind=?
            """,
            (now, now, getattr(args, "reason", None), getattr(args, "external_state", None), now, exact_id, kind),
        )
        if cur.rowcount != 1:
            con.rollback()
            con.close()
            raise SystemExit(
                "obligation-resolve --id matched no open row id=%s kind=%s"
                % (exact_id, kind)
            )
        resolved_ids = [int(row["id"])]
        target_type, target_id, pr, issue = (
            row["target_type"],
            row["target_id"],
            row["pr"],
            row["issue"],
        )
        receipt_slot = row["slot"]
    else:
        matched = con.execute(
            """
            SELECT id FROM obligations
            WHERE status='open'
              AND kind=?
              AND COALESCE(target_type,'')=COALESCE(?,'')
              AND COALESCE(target_id,'')=COALESCE(?,'')
              AND COALESCE(pr,-1)=COALESCE(?,-1)
              AND COALESCE(issue,-1)=COALESCE(?,-1)
            """,
            (kind, target_type, target_id, pr, issue),
        ).fetchall()
        resolved_ids = [int(r["id"]) for r in matched]
        if resolved_ids:
            placeholders = ",".join("?" for _ in resolved_ids)
            con.execute(
                """
                UPDATE obligations
                SET status='resolved', updated_at=?, resolved_at=?,
                    resolved_reason=COALESCE(?, resolved_reason),
                    external_state=COALESCE(?, external_state),
                    last_verified_at=?
                WHERE status='open' AND id IN (%s)
                """
                % placeholders,
                (now, now, getattr(args, "reason", None), getattr(args, "external_state", None), now, *resolved_ids),
            )
    if exact_id is None and not resolved_ids:
        # Key-based resolve matched nothing: fail typed instead of reporting
        # success. The stored key must match exactly across (kind,
        # target_type, target_id, pr, issue); a partial key such as
        # --kind/--pr alone must never read as resolved, and loosening the
        # match would risk sweeping sibling rows. Re-query the exact row
        # and retry with --id.
        con.rollback()
        con.close()
        raise SystemExit(
            "obligation-resolve matched no open row kind=%s target_type=%s target_id=%s pr=%s issue=%s;"
            " key-based resolve requires an exact stored-key match -- look up the row and retry with --id"
            % (kind, target_type, target_id, pr, issue)
        )
    # A resolve that matched nothing mints no receipt: the typed consumption
    # signal exists only when a row was actually resolved.
    if resolved_ids:
        record_resolution_receipt(
            con,
            kind,
            target_type,
            target_id,
            pr,
            issue,
            resolved_ids,
            getattr(args, "reason", None),
            getattr(args, "external_state", None),
            receipt_slot,
        )
    con.commit()
    con.close()


def resolve_target_obligations(args: argparse.Namespace) -> None:
    """Resolve all open obligations for a target, optionally limited by kind/prefix."""
    init_db()
    if (args.kind or "").startswith("first_boundary_reservation"):
        # Same contract as resolve_obligation: a release authorization row
        # resolves only through the exact-row consumption path (--id).  A
        # kind/prefix-sweep would resolve a reservation before the slot-ready
        # it authorizes could consume it.
        raise SystemExit(
            "obligation-resolve-target refused: first_boundary_reservation rows resolve only by exact obligation-resolve --id after validated consumption"
        )
    now = utc_now()
    clauses = ["status='open'"]
    params: list[Any] = []
    if args.pr is not None:
        clauses.append("COALESCE(pr,-1)=?")
        params.append(args.pr)
    if args.issue is not None:
        clauses.append("COALESCE(issue,-1)=?")
        params.append(args.issue)
    if args.target_type:
        clauses.append("COALESCE(target_type,'')=COALESCE(?,'')")
        params.append(args.target_type)
    if args.target_id:
        clauses.append("COALESCE(target_id,'')=COALESCE(?,'')")
        params.append(args.target_id)
    if args.kind:
        clauses.append("kind=?")
        params.append(args.kind)
    if args.kind_prefix:
        clauses.append("kind LIKE ?")
        params.append(f"{args.kind_prefix}%")
    if len(clauses) == 1:
        raise SystemExit("obligation-resolve-target requires a target selector")
    con = connect()
    con.execute(
        f"""
        UPDATE obligations
        SET status='resolved', updated_at=?, resolved_at=?,
            resolved_reason=COALESCE(?, resolved_reason),
            external_state=COALESCE(?, external_state),
            last_verified_at=?
        WHERE {' AND '.join(clauses)}
        """,
        [now, now, args.reason, args.external_state, now, *params],
    )
    con.commit()
    con.close()


def load_packet_payload(event_file: str | None, raw_json: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if event_file:
        path = Path(event_file)
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8", errors="replace"))
                if isinstance(loaded, dict):
                    payload.update(loaded)
                else:
                    payload["value"] = loaded
            except Exception as exc:
                payload["event_file_read_error"] = str(exc)
        payload.setdefault("event_file", event_file)
    if raw_json:
        try:
            loaded = json.loads(raw_json)
            if isinstance(loaded, dict):
                payload.update(loaded)
            else:
                payload["value"] = loaded
        except Exception:
            payload["raw"] = raw_json
    return payload


def packet_key_for(args: argparse.Namespace, payload: dict[str, Any]) -> str:
    if getattr(args, "packet_key", None):
        return args.packet_key
    event_file = getattr(args, "event_file", None) or payload.get("event_file")
    if event_file:
        return f"{args.packet_type}:file:{event_file}"
    pr = getattr(args, "pr", None) or payload.get("pr")
    head_sha = getattr(args, "head_sha", None) or payload.get("head_sha")
    if pr and head_sha:
        return f"{args.packet_type}:pr:{pr}:head:{head_sha}"
    raise SystemExit("transition packet needs --packet-key, --event-file, or --pr + --head-sha")


def packet_int(args: argparse.Namespace, payload: dict[str, Any], name: str) -> int | None:
    value = getattr(args, name, None)
    if value is None:
        value = payload.get(name)
    if value in (None, ""):
        return None
    return int(value)


def packet_text(args: argparse.Namespace, payload: dict[str, Any], name: str) -> str | None:
    value = getattr(args, name, None)
    if value is None:
        value = payload.get(name)
    if value in (None, ""):
        return None
    return str(value)


def upsert_transition_packet(args: argparse.Namespace) -> int:
    init_db()
    payload = load_packet_payload(args.event_file, args.payload_json)
    packet_key = packet_key_for(args, payload)
    now = utc_now()
    status = args.status or payload.get("status") or "pending"
    if status not in {"pending", "consumed", "rejected"}:
        raise SystemExit(f"invalid transition packet status: {status}")
    created_at = str(payload.get("created_at") or now)
    event_file = args.event_file or packet_text(args, payload, "event_file")
    pr = packet_int(args, payload, "pr")
    issue = packet_int(args, payload, "issue")
    slot = packet_int(args, payload, "slot")
    head_sha = packet_text(args, payload, "head_sha")
    branch = packet_text(args, payload, "branch")
    cwd = packet_text(args, payload, "cwd")
    review_proof = packet_text(args, payload, "review_proof")
    review_verdict = packet_text(args, payload, "review_verdict")
    qa_proof = packet_text(args, payload, "qa_proof")

    con = connect()
    row = con.execute("SELECT id, status FROM transition_packets WHERE packet_key=?", (packet_key,)).fetchone()
    consumed_at = now if status == "consumed" else None
    rejected_at = now if status == "rejected" else None
    if row:
        existing_status = str(row["status"])
        next_status = existing_status if existing_status == "consumed" and status == "pending" else status
        con.execute(
            """
            UPDATE transition_packets
            SET updated_at=?, status=?, event_file=COALESCE(?, event_file),
                pr=COALESCE(?, pr), issue=COALESCE(?, issue), slot=COALESCE(?, slot),
                head_sha=COALESCE(?, head_sha), branch=COALESCE(?, branch), cwd=COALESCE(?, cwd),
                review_proof=COALESCE(?, review_proof), review_verdict=COALESCE(?, review_verdict),
                qa_proof=COALESCE(?, qa_proof), payload_json=?,
                consumed_at=COALESCE(consumed_at, ?), rejected_at=CASE WHEN ?='rejected' THEN COALESCE(rejected_at, ?) ELSE rejected_at END,
                resolution_reason=COALESCE(?, resolution_reason)
            WHERE id=?
            """,
            (
                now,
                next_status,
                event_file,
                pr,
                issue,
                slot,
                head_sha,
                branch,
                cwd,
                review_proof,
                review_verdict,
                qa_proof,
                json.dumps(payload, sort_keys=True),
                consumed_at,
                next_status,
                rejected_at,
                getattr(args, "reason", None),
                int(row["id"]),
            ),
        )
        packet_id = int(row["id"])
    else:
        cur = con.execute(
            """
            INSERT INTO transition_packets
              (created_at, updated_at, packet_key, packet_type, status, event_file,
               pr, issue, slot, head_sha, branch, cwd, review_proof, review_verdict,
               qa_proof, payload_json, consumed_at, rejected_at, resolution_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                now,
                packet_key,
                args.packet_type,
                status,
                event_file,
                pr,
                issue,
                slot,
                head_sha,
                branch,
                cwd,
                review_proof,
                review_verdict,
                qa_proof,
                json.dumps(payload, sort_keys=True),
                consumed_at,
                rejected_at,
                getattr(args, "reason", None),
            ),
        )
        packet_id = int(cur.lastrowid)
    con.commit()
    con.close()
    if args.print_id:
        print(packet_id)
    return packet_id


def set_transition_packet_status(args: argparse.Namespace) -> int:
    init_db()
    payload = load_packet_payload(args.event_file, args.payload_json)
    packet_key = packet_key_for(args, payload)
    status = args.status
    if status not in {"pending", "consumed", "rejected"}:
        raise SystemExit(f"invalid transition packet status: {status}")
    # Ensure old /tmp packets that predate the durable table are captured before
    # status is changed.
    upsert_args = argparse.Namespace(
        packet_type=args.packet_type,
        packet_key=packet_key,
        event_file=args.event_file,
        status="pending",
        pr=args.pr,
        issue=args.issue,
        slot=args.slot,
        head_sha=args.head_sha,
        branch=args.branch,
        cwd=None,
        review_proof=None,
        review_verdict=None,
        qa_proof=None,
        payload_json=args.payload_json,
        reason=None,
        print_id=False,
    )
    packet_id = upsert_transition_packet(upsert_args)
    now = utc_now()
    con = connect()
    con.execute(
        """
        UPDATE transition_packets
        SET updated_at=?, status=?,
            consumed_at=CASE WHEN ?='consumed' THEN COALESCE(consumed_at, ?) ELSE consumed_at END,
            rejected_at=CASE WHEN ?='rejected' THEN COALESCE(rejected_at, ?) ELSE rejected_at END,
            resolution_reason=COALESCE(?, resolution_reason)
        WHERE packet_key=?
        """,
        (now, status, status, now, status, now, args.reason, packet_key),
    )
    con.commit()
    con.close()
    if args.print_id:
        print(packet_id)
    return packet_id


def transition_packet_selector(args: argparse.Namespace) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if args.status:
        clauses.append("status=?")
        params.append(args.status)
    if args.packet_type:
        clauses.append("packet_type=?")
        params.append(args.packet_type)
    if args.pr is not None:
        clauses.append("pr=?")
        params.append(args.pr)
    if args.issue is not None:
        clauses.append("issue=?")
        params.append(args.issue)
    if args.slot is not None:
        clauses.append("slot=?")
        params.append(args.slot)
    if args.head_sha:
        clauses.append("head_sha=?")
        params.append(args.head_sha)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    return where, params


def list_transition_packets(args: argparse.Namespace) -> None:
    init_db()
    where, params = transition_packet_selector(args)
    con = connect()
    rows = con.execute(
        f"""
        SELECT * FROM transition_packets
        {where}
        ORDER BY updated_at DESC, id DESC
        LIMIT ?
        """,
        [*params, args.limit],
    ).fetchall()
    con.close()
    if args.format == "json":
        print(json.dumps([dict(row) for row in rows], sort_keys=True))
        return
    if not rows:
        print("(none)")
        return
    for row in rows:
        print(
            f"packet:{row['id']} {row['packet_type']} status={row['status']} "
            f"pr={row['pr'] or '-'} issue={row['issue'] or '-'} slot={row['slot'] or '-'} "
            f"head={str(row['head_sha'] or '')[:10]} file={row['event_file'] or '-'}"
        )


def load_json(path: str | Path) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
            return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def ingest_sentinels() -> None:
    init_db()
    for path in glob.glob("/tmp/pm-required-ci-reconcile-*.json"):
        data = load_json(path)
        if data.get("status") in {"resolved", "superseded"}:
            args = argparse.Namespace(kind="ci_reconcile", target_type="pr", target_id=str(data.get("pr") or ""), pr=data.get("pr"), issue=None, slot=None)
            resolve_obligation(args)
            continue
        pr = data.get("pr")
        if not pr:
            continue
        if typed_resolution_exists("ci_reconcile", "pr", str(pr), int(pr), None):
            print(f"PM_OPS_SKIP_TYPED_RESOLUTION kind=ci_reconcile pr={pr} typed resolution consumed the class", file=sys.stderr)
            continue
        upsert_obligation(
            argparse.Namespace(
                kind="ci_reconcile",
                target_type="pr",
                target_id=str(pr),
                pr=int(pr),
                issue=None,
                slot=None,
                owner="pm",
                severity="high",
                title=f"CI terminal reconcile required for PR #{pr}",
                action="Reconcile latest CI terminal event against live PR labels/checks and either promote, demote, rerun, or write blocker proof.",
                blocker=None,
                evidence=[f"sentinel={path}", f"event={data.get('event', 'unknown')}"],
                evidence_json=json.dumps(data, sort_keys=True),
                print_id=False,
            )
        )

    for path in glob.glob("/tmp/pm-required-cleanup-pr-*.json"):
        data = load_json(path)
        pr = data.get("pr")
        if not pr:
            continue
        if data.get("status") == "resolved":
            resolve_obligation(argparse.Namespace(kind="cleanup_pr", target_type="pr", target_id=str(pr), pr=int(pr), issue=None, slot=None))
            continue
        if typed_resolution_exists("cleanup_pr", "pr", str(pr), int(pr), None):
            print(f"PM_OPS_SKIP_TYPED_RESOLUTION kind=cleanup_pr pr={pr} typed resolution consumed the class", file=sys.stderr)
            continue
        upsert_obligation(
            argparse.Namespace(
                kind="cleanup_pr",
                target_type="pr",
                target_id=str(pr),
                pr=int(pr),
                issue=None,
                slot=None,
                owner="pm",
                severity="high",
                title=f"Post-merge cleanup required for PR #{pr}",
                action="Run/resume cleanup-pr until pm-state:closed-clean is live.",
                blocker=None,
                evidence=[f"sentinel={path}"],
                evidence_json=json.dumps(data, sort_keys=True),
                print_id=False,
            )
        )

    for path in glob.glob("/tmp/post-issue-create-sweep-*.flag"):
        if os.path.exists(path + ".resolved"):
            text = Path(path).read_text(errors="ignore")
            match = re.search(r"ISSUE:\s+#(\d+)", text)
            if match:
                issue = int(match.group(1))
                resolve_obligation(argparse.Namespace(kind="issue_routing", target_type="issue", target_id=str(issue), pr=None, issue=issue, slot=None))
            continue
        text = Path(path).read_text(errors="ignore")
        match = re.search(r"ISSUE:\s+#(\d+)", text)
        if not match:
            continue
        issue = int(match.group(1))
        # Consult the authoritative live issue state before creating a routing
        # row: a claimed (status:in-progress + slot:N), queued (status:todo --
        # the Ready Pool IS the QUEUED_IN_PM_OPS routing disposition), or parked
        # (status:backlog / pm-blocked:*) issue has a routing disposition
        # already; re-creating the obligation for it is duplicate churn.
        # (CTO control-plane-issue:routing-obligation-generator-recreates-claimed-issues:10443-10445 + recurrence 10461-10462, 2026-08-06.)
        live_labels: list[str] = []
        live_data, live_err = run_json(
            ["gh", "issue", "view", str(issue), "--repo", REPO, "--json", "labels"]
        )
        if live_data is not None:
            live_labels = [label.get("name", "") for label in live_data.get("labels", [])]
        elif live_err:
            print(f"PM_OPS_WARN issue_routing live-state check failed issue={issue} err={live_err}", file=sys.stderr)
        has_in_progress = "status:in-progress" in live_labels
        has_slot = any(label.startswith("slot:") for label in live_labels)
        if (
            (has_in_progress and has_slot)
            or "status:todo" in live_labels
            or any(
                label in ("status:backlog", "status:done", "status:blocked") or label.startswith("pm-blocked:")
                for label in live_labels
            )
        ):
            resolve_obligation(
                argparse.Namespace(kind="issue_routing", target_type="issue", target_id=str(issue), pr=None, issue=issue, slot=None)
            )
            mark_post_create_flag_resolved(issue)
            continue
        if typed_resolution_exists("issue_routing", "issue", str(issue), None, issue):
            print(f"PM_OPS_SKIP_TYPED_RESOLUTION kind=issue_routing issue={issue} typed resolution consumed the class", file=sys.stderr)
            continue
        upsert_obligation(
            argparse.Namespace(
                kind="issue_routing",
                target_type="issue",
                target_id=str(issue),
                pr=None,
                issue=issue,
                slot=None,
                owner="pm",
                severity="high",
                title=f"New status:todo issue #{issue} needs routing proof",
                action="Assign to a free slot, queue with Ready Pool metadata, or block with explicit proof.",
                blocker=None,
                evidence=[f"flag={path}"],
                evidence_json=json.dumps({"flag": path, "raw": text[:2000]}, sort_keys=True),
                print_id=False,
            )
        )


def run_json(cmd: list[str], timeout_s: int = 25) -> tuple[Any, str | None]:
    """Run a JSON-producing command, surfacing real errors instead of hiding them."""
    resolved = list(cmd)
    if resolved and resolved[0] == "gh":
        gh_path = gh_bin()
        if not gh_path:
            return None, "gh not found on PATH or /opt/homebrew/bin /usr/local/bin"
        resolved[0] = gh_path
    try:
        proc = subprocess.run(
            resolved,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return None, f"timeout after {timeout_s}s"
    except Exception as exc:
        return None, str(exc)
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        snippet = " | ".join(detail[-3:]) if detail else f"rc={proc.returncode}"
        return None, f"rc={proc.returncode}: {snippet[:300]}"
    try:
        return json.loads(proc.stdout), None
    except Exception as exc:
        return None, f"invalid json: {exc}"


def run_json_partial(cmd: list[str], timeout_s: int = 45) -> tuple[Any, str | None]:
    """Like run_json, but keeps parsed stdout even on a non-zero exit.

    GraphQL returns per-node errors (e.g. NOT_FOUND for an absent PR/issue)
    alongside valid data for the other nodes in the same response, and exits
    non-zero whenever any error is present. run_json would discard that partial
    data and force a serial per-number fallback for rows it already resolved.
    """
    resolved = list(cmd)
    if resolved and resolved[0] == "gh":
        gh_path = gh_bin()
        if not gh_path:
            return None, "gh not found on PATH or /opt/homebrew/bin /usr/local/bin"
        resolved[0] = gh_path
    try:
        proc = subprocess.run(
            resolved,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return None, f"timeout after {timeout_s}s"
    except Exception as exc:
        return None, str(exc)
    try:
        return json.loads(proc.stdout), None
    except Exception as exc:
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip().splitlines()
            snippet = " | ".join(detail[-3:]) if detail else f"rc={proc.returncode}"
            return None, f"rc={proc.returncode}: {snippet[:300]}"
        return None, f"invalid json: {exc}"


def read_mop_slots() -> tuple[list[dict[str, Any]], str | None]:
    if not MOP_DB.exists():
        return [], f"missing:{MOP_DB}"
    try:
        con = sqlite3.connect(f"file:{MOP_DB}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        rows = [
            dict(row)
            for row in con.execute(
                "select slot,name,status,task,issue,branch,pr,last_activity,dnd,idle,activity from slots order by slot"
            )
        ]
        con.close()
        return rows, None
    except Exception as exc:
        return [], str(exc)


def store_snapshot(source: str, target_type: str, target_id: str, payload: Any, status: str) -> None:
    con = connect()
    con.execute(
        "INSERT INTO snapshots(ts, source, target_type, target_id, payload_json, status, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (utc_now(), source, target_type, target_id, json.dumps(payload, sort_keys=True), status, utc_now()),
    )
    con.commit()
    con.close()


def open_obligations(horizon: str | None = None, limit: int | None = None, *, readonly: bool = False) -> list[sqlite3.Row]:
    con = connect_readonly() if readonly else connect()
    clauses = ["status='open'"]
    params: list[Any] = []
    normalized = normalize_horizon(horizon)
    if normalized:
        clauses.append("horizon=?")
        params.append(normalized)
    sql = (
        "SELECT * FROM obligations WHERE "
        + " AND ".join(clauses)
        + " ORDER BY "
        + "CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END, "
        + "CASE horizon WHEN 'hourly' THEN 0 WHEN 'heartbeat' THEN 1 ELSE 2 END, updated_at DESC, id DESC"
    )
    if limit and limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    rows = con.execute(
        sql,
        params,
    ).fetchall()
    con.close()
    return rows


def format_obligation(row: sqlite3.Row, include_horizon: bool = True) -> str:
    target = row["target_id"] or row["pr"] or row["issue"] or row["slot"] or "misc"
    label = f"{row['kind']}:{row['target_type'] or 'misc'}#{target}"
    action = row["required_action"] or "reconcile"
    title = row["title"] or label
    sev = row["severity"] or "normal"
    horizon = classify_obligation_horizon(row)
    prefix = f"[{horizon}] " if include_horizon else ""
    return f"- {prefix}[{sev}] obligation:{row['id']} {label} - {title}. Next: {action}"


def print_snapshot(args: argparse.Namespace) -> None:
    readonly = bool(getattr(args, "read_only", False))
    if not readonly:
        init_db()
        ingest_sentinels()
        auto_reconcile_terminal_obligations()
    horizon = normalize_horizon(args.surface)
    if not horizon:
        raise SystemExit(f"unknown surface: {args.surface}")
    rows = open_obligations(horizon=horizon, limit=args.limit, readonly=readonly)
    packets = pending_transition_packets(readonly=readonly) if horizon == "hourly" else []

    if not readonly:
        con = connect()
        if rows:
            ids = [int(row["id"]) for row in rows]
            placeholders = ",".join("?" for _ in ids)
            con.execute(f"UPDATE obligations SET last_surface_at=? WHERE id IN ({placeholders})", [utc_now(), *ids])
            con.commit()
        con.close()

    if args.format == "json":
        print(
            json.dumps(
                {
                    "surface": horizon,
                    "generated_at": utc_now(),
                    "obligations": [dict(row) for row in rows],
                    "pending_transition_packets": [dict(row) for row in packets],
                },
                sort_keys=True,
            )
        )
        return

    print(f"━━━ PM OPS SNAPSHOT surface={horizon} generated={utc_now()} ━━━")
    if rows:
        for row in rows:
            print(format_obligation(row, include_horizon=False))
    else:
        print("- (no open obligations on this surface)")
    if packets:
        print("Pending transition packets:")
        for row in packets[: min(len(packets), args.limit)]:
            head = str(row["head_sha"] or "")[:10] or "-"
            print(
                f"- packet:{row['id']} {row['packet_type']} PR #{row['pr'] or '-'} issue #{row['issue'] or '-'} "
                f"slot:{row['slot'] or '-'} head={head} file={row['event_file'] or '-'}"
            )


def pending_transition_packets(*, readonly: bool = False) -> list[sqlite3.Row]:
    con = connect_readonly() if readonly else connect()
    rows = con.execute(
        """
        SELECT * FROM transition_packets
        WHERE status='pending'
        ORDER BY updated_at DESC, id DESC
        LIMIT 50
        """
    ).fetchall()
    con.close()
    return rows


def collect_live() -> tuple[dict[str, Any], dict[str, Any]]:
    health: dict[str, Any] = {}
    slots, slot_err = read_mop_slots()
    health["mop_slots"] = "ok" if slot_err is None else slot_err
    store_snapshot("mop", "slots", "all", slots, "ok" if slot_err is None else "error")

    prs, pr_err = run_json(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            REPO,
            "--state",
            "open",
            "--json",
            "number,title,labels,isDraft,mergeStateStatus,mergeable,statusCheckRollup,headRefName,headRefOid,updatedAt",
            "--limit",
            "30",
        ]
    )
    health["github_prs"] = "ok" if pr_err is None else pr_err
    if not isinstance(prs, list):
        prs = []
    store_snapshot("github", "prs", "open", prs, "ok" if pr_err is None else "error")

    issues, issue_err = run_json(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            REPO,
            "--state",
            "open",
            "--label",
            "status:todo",
            "--json",
            "number,title,labels,updatedAt",
            "--limit",
            "50",
        ]
    )
    health["github_status_todo"] = "ok" if issue_err is None else issue_err
    if not isinstance(issues, list):
        issues = []
    store_snapshot("github", "issues", "status:todo", issues, "ok" if issue_err is None else "error")
    return {"slots": slots, "prs": prs, "issues": issues}, health


def parse_int(value: Any) -> int | None:
    if value is None:
        return None
    match = re.search(r"\d+", str(value))
    return int(match.group(0)) if match else None


def mark_post_create_flag_resolved(issue: int) -> None:
    for path in glob.glob(f"/tmp/post-issue-create-sweep-{issue}.flag"):
        try:
            Path(path + ".resolved").touch()
        except Exception:
            pass


def auto_reconcile_from_live(live: dict[str, Any]) -> None:
    """Close routing obligations when live MoP/GitHub proves the issue is routed."""
    routed: dict[int, str] = {}
    for slot in live.get("slots") or []:
        issue = parse_int(slot.get("issue"))
        if issue:
            routed[issue] = f"mop-slot:{slot.get('slot')}"

    for issue in live.get("issues") or []:
        num = parse_int(issue.get("number"))
        if not num:
            continue
        slot_labels = [label for label in labels(issue) if label.startswith("slot:")]
        concrete = [label for label in slot_labels if re.match(r"slot:[0-9]+$", label)]
        if concrete:
            routed[num] = ",".join(concrete)

    if not routed:
        return

    now = utc_now()
    con = connect()
    for issue, proof in routed.items():
        cur = con.execute(
            """
            UPDATE obligations
            SET status='resolved', updated_at=?, resolved_at=?, evidence_json=?
            WHERE status='open'
              AND kind='issue_routing'
              AND COALESCE(issue,-1)=?
            """,
            (now, now, json.dumps({"auto_resolved_by": proof}, sort_keys=True), issue),
        )
        if cur.rowcount:
            mark_post_create_flag_resolved(issue)
    con.commit()
    con.close()


def labels(item: dict[str, Any]) -> list[str]:
    return [x.get("name", "") for x in item.get("labels") or [] if isinstance(x, dict)]


def check_state(pr: dict[str, Any]) -> str:
    pending = []
    bad = []
    success = 0
    for c in pr.get("statusCheckRollup") or []:
        name = c.get("name") or c.get("workflowName") or "check"
        status = c.get("status") or ""
        conclusion = c.get("conclusion") or ""
        if status and status != "COMPLETED":
            pending.append(name)
        elif conclusion == "SUCCESS":
            success += 1
        elif conclusion in ("SKIPPED", "NEUTRAL"):
            pass
        elif conclusion:
            bad.append(f"{name}:{conclusion}")
        else:
            pending.append(name)
    if pending:
        return f"in_flight pending={','.join(pending[:3])}"
    if bad:
        return f"red bad={','.join(bad[:3])}"
    if success:
        return f"green success_count={success}"
    return "unknown"


def gh_pr_view(pr: int) -> dict[str, Any] | None:
    data, err = run_json(
        [
            "gh",
            "pr",
            "view",
            str(pr),
            "--repo",
            REPO,
            "--json",
            "number,title,body,state,mergedAt,closedAt,labels,isDraft,mergeStateStatus,mergeable,statusCheckRollup,headRefName,headRefOid,updatedAt",
        ],
        timeout_s=20,
    )
    return data if err is None and isinstance(data, dict) else None


def gh_issue_view(issue: int) -> dict[str, Any] | None:
    data, err = run_json(
        [
            "gh",
            "issue",
            "view",
            str(issue),
            "--repo",
            REPO,
            "--json",
            "number,state,closedAt,labels,updatedAt",
        ],
        timeout_s=20,
    )
    return data if err is None and isinstance(data, dict) else None


def _normalize_graphql_node(node: dict[str, Any]) -> dict[str, Any]:
    """Flatten a GraphQL PR/issue node into the `gh ... view --json` shape."""
    out = {key: value for key, value in node.items() if key != "labels"}
    out["labels"] = [
        {"name": entry.get("name")}
        for entry in (node.get("labels") or {}).get("nodes") or []
        if isinstance(entry, dict)
    ]
    return out


def gh_graphql_batch(object_kind: str, numbers: list[int], chunk_size: int = 50) -> tuple[dict[int, dict[str, Any]], set[int]]:
    """Resolve many PR/issue nodes with a few bulk GraphQL calls.

    Returns ``(resolved, retry)``: ``resolved`` maps number to a node in the
    same shape ``gh <kind> view --json`` returns, and ``retry`` is the set of
    numbers this batch could not authoritatively classify, so the caller can
    fall back to the unchanged per-number lookup.

    A number is left out of ``retry`` only when the batch proves it is absent
    in the same namespace the per-number lookup reads, so skipping the fallback
    cannot skip a genuinely terminal target. ``gh pr view <n>`` resolves exactly
    the pull-request namespace, so a ``pullRequest`` NOT_FOUND is authoritative
    absence. ``gh issue view <n>`` also resolves pull-request numbers (GitHub's
    issue API includes PRs), so an ``issue`` NOT_FOUND is NOT authoritative and
    those numbers stay in ``retry``. A failed chunk, a non-NOT_FOUND error, or a
    missing node without an explicit NOT_FOUND also go into ``retry``.
    """
    resolved: dict[int, dict[str, Any]] = {}
    retry: set[int] = set()
    not_found_is_absent = object_kind == "pullRequest"
    fields = _GRAPHQL_OBJECT_FIELDS[object_kind]
    for start in range(0, len(numbers), chunk_size):
        chunk = numbers[start : start + chunk_size]
        aliases = "\n".join(
            f'  n{i}: {object_kind}(number: {num}) {{ {fields} }}'
            for i, num in enumerate(chunk)
        )
        query = (
            "query {\n"
            f'  repository(owner: "{_REPO_OWNER}", name: "{_REPO_NAME}") {{\n'
            f"{aliases}\n"
            "  }\n"
            "}"
        )
        payload, err = run_json_partial(["gh", "api", "graphql", "-f", f"query={query}"])
        if err is not None or not isinstance(payload, dict):
            retry.update(chunk)
            continue
        errors = [entry for entry in (payload.get("errors") or []) if isinstance(entry, dict)]
        if any(entry.get("type") != "NOT_FOUND" for entry in errors):
            retry.update(chunk)
            continue
        not_found_aliases = {
            (entry.get("path") or [None, None])[-1]
            for entry in errors
            if entry.get("type") == "NOT_FOUND"
        }
        repository = (payload.get("data") or {}).get("repository") or {}
        for index, num in enumerate(chunk):
            node = repository.get(f"n{index}")
            if isinstance(node, dict):
                resolved[int(num)] = _normalize_graphql_node(node)
            elif not (not_found_is_absent and f"n{index}" in not_found_aliases):
                # Alias hazard. Regression guard: tests/test-pm-ops-graphql-batch-alias.py
                # gh issue view <n> also resolves pull-request numbers (GitHub's issue
                # API includes PRs), so for object_kind="issue" a NOT_FOUND does NOT
                # prove the number is absent in the namespace the per-number lookup
                # reads. Treating it as absence would drop a closed PR recorded as an
                # issue out of `retry` entirely -> a silent non-resolution. Only a
                # pullRequest NOT_FOUND is authoritative absence.
                retry.add(int(num))
    return resolved, retry


def resolve_obligation_ids(con: sqlite3.Connection, ids: list[int], reason: str, external_state: str | None = None, superseded_by: int | None = None) -> None:
    if not ids:
        return
    now = utc_now()
    con.executemany(
        """
        UPDATE obligations
        SET status='resolved', updated_at=?, resolved_at=?,
            resolved_reason=?, external_state=COALESCE(?, external_state),
            superseded_by=COALESCE(?, superseded_by), last_verified_at=?
        WHERE status='open' AND id=?
        """,
        [(now, now, reason, external_state, superseded_by, now, oid) for oid in ids],
    )


def upsert_cleanup_obligation_for_merged_pr(pr_num: int, pr: dict[str, Any]) -> None:
    labs = labels(pr)
    if "pm-state:closed-clean" in labs:
        return
    if typed_resolution_exists("cleanup_pr", "pr", str(pr_num), pr_num, None):
        print(f"PM_OPS_SKIP_TYPED_RESOLUTION kind=cleanup_pr pr={pr_num} typed resolution consumed the class", file=sys.stderr)
        return
    upsert_obligation(
        argparse.Namespace(
            kind="cleanup_pr",
            target_type="pr",
            target_id=str(pr_num),
            pr=pr_num,
            issue=None,
            slot=None,
            owner="pm",
            severity="high",
            title=f"Post-merge cleanup required for PR #{pr_num}",
            action="Run/resume cleanup-pr until pm-state:closed-clean is live.",
            blocker="merged PR lacks pm-state:closed-clean",
            evidence=[f"state={pr.get('state')}", f"mergedAt={pr.get('mergedAt') or 'unknown'}"],
            evidence_json=json.dumps({"live_pr": pr}, sort_keys=True),
            print_id=False,
        )
    )


def linked_issue_from_pr_rows(pr: dict[str, Any], rows: list[sqlite3.Row]) -> str:
    row_issues = sorted({parse_int(row["issue"]) for row in rows if parse_int(row["issue"])})
    if row_issues:
        return str(row_issues[0])
    text = f"{pr.get('title') or ''}\n{pr.get('body') or ''}"
    match = re.search(r"#(\d+)", text)
    if match:
        return match.group(1)
    match = re.search(r"(?:^|[^0-9])(\d{4})(?:[^0-9]|$)", pr.get("title") or "")
    return match.group(1) if match else ""


def mop_slot_matches_pr(slot: dict[str, Any], pr_num: int, issue: str, branch: str) -> bool:
    occupied = slot.get("occupied")
    if occupied is None:
        occupied = str(slot.get("status") or "").lower() not in {"", "free", "idle"}
    if not occupied:
        return False
    pr_s = str(pr_num or "")
    issue_s = str(issue or "")
    branch_s = str(branch or "")
    slot_pr = str(slot.get("pr") or "")
    slot_issue = str(slot.get("issue") or "")
    slot_branch = str(slot.get("branch") or "")
    task = str(slot.get("task") or "")
    if pr_s and slot_pr == pr_s:
        return True
    if issue_s and slot_issue == issue_s:
        return True
    if branch_s and (slot_branch == branch_s or branch_s in task):
        return True
    return False


def live_mop_slot_for_pr(pr_num: int, pr: dict[str, Any], rows: list[sqlite3.Row], slots: list[dict[str, Any]]) -> dict[str, Any] | None:
    issue = linked_issue_from_pr_rows(pr, rows)
    branch = str(pr.get("headRefName") or "")
    for slot in slots:
        if mop_slot_matches_pr(slot, pr_num, issue, branch):
            return slot
    return None


def row_context_text(row: sqlite3.Row) -> str:
    return " ".join(str(row[key] or "") for key in ("kind", "title", "required_action", "blocker")).lower()


def pm_gate_artifact_obligation(rows: list[sqlite3.Row]) -> sqlite3.Row | None:
    cleanup_candidates = []
    generic_candidates = []
    for row in rows:
        if row["kind"] not in {"cleanup_pr", "pm_gate_blocker"}:
            continue
        text = row_context_text(row)
        if not any(token in text for token in ("artifact", "docx", "ooxml", "forensic", "proof", "download")):
            continue
        if row["kind"] == "cleanup_pr":
            cleanup_candidates.append(row)
        else:
            generic_candidates.append(row)
    candidates = cleanup_candidates or generic_candidates
    if not candidates:
        return None
    return max(candidates, key=lambda row: (str(row["updated_at"] or ""), int(row["id"])))


def promote_current_pr_obligation(con: sqlite3.Connection, row: sqlite3.Row, external_state: str) -> None:
    now = utc_now()
    con.execute(
        """
        UPDATE obligations
        SET severity='high', horizon='hourly', updated_at=?, last_verified_at=?,
            external_state=?
        WHERE status='open' AND id=?
        """,
        (now, now, external_state, int(row["id"])),
    )


def auto_reconcile_current_pr_obligations(con: sqlite3.Connection) -> None:
    """Collapse stale per-PR ops rows when live GitHub/MoP proves the current state.

    The hourly surface should show the PM's next executable transition, not every
    historical way a PR was blocked. Live MoP ownership satisfies dispatch/rework
    obligations; a current PM-gate artifact obligation supersedes stale CI/rework
    rows until PM produces the proof or parks the PR behind a non-slot blocker.
    """
    rows = con.execute("SELECT * FROM obligations WHERE status='open' AND pr IS NOT NULL").fetchall()
    by_pr: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        try:
            by_pr.setdefault(int(row["pr"]), []).append(row)
        except Exception:
            continue
    if not by_pr:
        return

    slots, slot_err = read_mop_slots()
    if slot_err is not None:
        slots = []

    for pr_num, pr_rows in sorted(by_pr.items()):
        pr = gh_pr_view(pr_num)
        if not pr:
            continue
        state = str(pr.get("state") or "").upper()
        if state != "OPEN":
            continue
        labs = {label.lower() for label in labels(pr)}
        external_parts = [
            f"pr:OPEN",
            f"pm_state={','.join(sorted(label for label in labs if label.startswith('pm-state:'))) or 'missing'}",
            f"blockers={','.join(sorted(label for label in labs if label.startswith('pm-blocked:'))) or 'none'}",
            f"branch={pr.get('headRefName') or 'unknown'}",
            f"head={str(pr.get('headRefOid') or '')[:10]}",
        ]

        owner_slot = live_mop_slot_for_pr(pr_num, pr, pr_rows, slots)
        if owner_slot:
            ids = [
                int(row["id"])
                for row in pr_rows
                if row["kind"] in PR_ASSIGNMENT_SATISFIED_KINDS
            ]
            slot_num = owner_slot.get("slot")
            external = " ".join([*external_parts, f"live_mop_slot={slot_num}", f"mop_branch={owner_slot.get('branch') or 'unknown'}"])
            resolve_obligation_ids(con, ids, "live_mop_slot_owns_current_pr_rework", external)
            continue

        artifact = pm_gate_artifact_obligation(pr_rows)
        if "pm-blocked:pm-gate" in labs and artifact:
            external = " ".join([*external_parts, "current_pm_gate_artifact_obligation=true"])
            promote_current_pr_obligation(con, artifact, external)
            ids = [
                int(row["id"])
                for row in pr_rows
                if int(row["id"]) != int(artifact["id"]) and row["kind"] in PR_PM_GATE_SUPERSEDED_KINDS
            ]
            resolve_obligation_ids(con, ids, "superseded_by_current_pm_gate_artifact_obligation", external, superseded_by=int(artifact["id"]))
            continue

        canonical = [
            row for row in pr_rows
            if row["kind"] in PR_REWORK_CANONICAL_KINDS and str(row["severity"] or "").lower() in {"critical", "high"}
        ]
        if canonical:
            current = max(canonical, key=lambda row: (str(row["updated_at"] or ""), int(row["id"])))
            ids = [int(row["id"]) for row in canonical if int(row["id"]) != int(current["id"])]
            external = " ".join([*external_parts, f"current_rework_obligation={int(current['id'])}"])
            resolve_obligation_ids(con, ids, "superseded_by_current_pr_rework_obligation", external, superseded_by=int(current["id"]))


def auto_reconcile_terminal_obligations() -> None:
    """Resolve stale obligations whose live GitHub/alert target is terminal.

    This is intentionally conservative for nonterminal targets. Merged PRs hand
    off cleanup to cleanup_pr; closed PRs/issues and explicit resolved alerts no
    longer need active CI/rework/plan obligations.
    """
    init_db()
    con = connect()
    rows = con.execute("SELECT * FROM obligations WHERE status='open'").fetchall()
    duplicate_groups: dict[tuple[Any, ...], list[sqlite3.Row]] = {}
    for row in rows:
        key = (row["kind"], row["target_type"], row["target_id"], row["pr"], row["issue"])
        duplicate_groups.setdefault(key, []).append(row)
    for group in duplicate_groups.values():
        if len(group) <= 1:
            continue
        newest = max(group, key=lambda r: int(r["id"]))
        older = [int(r["id"]) for r in group if int(r["id"]) != int(newest["id"])]
        resolve_obligation_ids(con, older, "superseded_by_newer_same_target_kind", superseded_by=int(newest["id"]))

    for row in rows:
        title_action = " ".join(str(row[k] or "") for k in ("title", "required_action", "blocker")).lower()
        if row["kind"] == "alert" and re.search(r"\b(closed-resolved|resolved|recovered)\b", title_action) and not re.search(r"\bpending\b|\bawaiting\b|\binvestigat", title_action):
            resolve_obligation_ids(con, [int(row["id"])], "alert_text_declares_resolved", "alert:resolved")

    # Do not retain a write lock while the following GitHub reads run. A status
    # call used to hold this transaction across every remote lookup, starving
    # hourly sync and background agents until their SQLite busy timeout fired.
    con.commit()

    prs = sorted({int(row["pr"]) for row in rows if row["pr"] is not None})
    pr_views, pr_retry = gh_graphql_batch("pullRequest", prs)
    for pr_num in prs:
        pr = pr_views.get(pr_num)
        if pr is None:
            if pr_num not in pr_retry:
                continue  # batch proved this number is not a PR
            pr = gh_pr_view(pr_num)
        if not pr:
            continue
        state = (pr.get("state") or "").upper()
        labs = labels(pr)
        if state == "MERGED":
            ids = [
                int(row["id"])
                for row in rows
                if row["pr"] == pr_num and row["kind"] != "cleanup_pr"
            ]
            resolve_obligation_ids(con, ids, "pr_merged_cleanup_validator_owns_followup", f"pr:{state}")
            if "pm-state:closed-clean" in labs:
                cleanup_ids = [int(row["id"]) for row in rows if row["pr"] == pr_num and row["kind"] == "cleanup_pr"]
                resolve_obligation_ids(con, cleanup_ids, "pr_merged_closed_clean", f"pr:{state}:closed-clean")
            else:
                con.commit()
                upsert_cleanup_obligation_for_merged_pr(pr_num, pr)
                con = connect()
        elif state == "CLOSED":
            ids = [int(row["id"]) for row in rows if row["pr"] == pr_num]
            resolve_obligation_ids(con, ids, "pr_closed_not_merged_terminal", f"pr:{state}")
        con.commit()

    issues = sorted({int(row["issue"]) for row in rows if row["issue"] is not None and row["pr"] is None})
    issue_views, issue_retry = gh_graphql_batch("issue", issues)
    for issue_num in issues:
        issue = issue_views.get(issue_num)
        if issue is None:
            if issue_num not in issue_retry:
                continue  # batch proved this number is absent everywhere gh issue view reads
            issue = gh_issue_view(issue_num)
        if not issue:
            continue
        state = (issue.get("state") or "").upper()
        if state == "CLOSED":
            ids = [int(row["id"]) for row in rows if row["issue"] == issue_num and row["pr"] is None]
            resolve_obligation_ids(con, ids, "issue_closed_terminal", f"issue:{state}")
        con.commit()
    auto_reconcile_current_pr_obligations(con)
    con.commit()
    con.close()


def legacy_excerpt() -> tuple[list[str], int]:
    if not TODO_PATH.exists():
        return [], 0
    lines = TODO_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()
    return [], len(lines)


def render_markdown(
    live: dict[str, Any],
    obligations: list[sqlite3.Row],
    health: dict[str, Any],
    reason: str,
    *,
    live_skipped: bool = False,
) -> str:
    now = utc_now()
    out: list[str] = [
        f"# PM Ops Dashboard - {now}",
        "",
        f"> Generated by `pm-ops.py sync` from PM-owned SQLite ledger `{DB_PATH}`.",
        f"> Reason: {reason}. GitHub/MoP/Slack remain authoritative for their live state; this file is a rendered view.",
        "",
        "## Source Health",
    ]
    for key, value in sorted(health.items()):
        out.append(f"- {key}: {value}")

    out += ["", "## Open Obligations"]
    if obligations:
        for horizon in HORIZONS:
            rows = [row for row in obligations if classify_obligation_horizon(row) == horizon]
            if not rows:
                continue
            out += ["", f"### {horizon.title()}"]
            for row in rows:
                out.append(format_obligation(row, include_horizon=False))
    else:
        out.append("- (none)")

    # CI-watch obligations render under a literal "## CI watch" heading so the
    # kanban drift checker can find owner/action assignments for pm-blocked:ci
    # PRs (obl 9581 renderer gap). Each row carries PR/head/owner/run/action.
    ci_watch_rows = [
        row for row in obligations if str(row["kind"] or "").lower() == "ci_watch"
    ]
    out += ["", "## CI watch"]
    if ci_watch_rows:
        for row in ci_watch_rows:
            out.append(format_obligation(row, include_horizon=False))
    else:
        out.append("- (none)")

    # Surface the top critical/high obligations only; the full ledger is
    # already listed by horizon under "## Open Obligations". `obligations` is
    # severity-ordered (critical, then high) by open_obligations().
    top_priority = [
        row
        for row in obligations
        if str(row["severity"] or "").lower() in {"critical", "high"}
    ][:3]
    out += ["", "## Top Priority Obligations"]
    if top_priority:
        for row in top_priority:
            out.append(format_obligation(row, include_horizon=True))
    else:
        out.append("- (none)")

    out += ["", "## Slots"]
    slots = live.get("slots") or []
    if live_skipped:
        out.append("- (live snapshot skipped by --no-live; run live sync for MoP slot state)")
    elif slots:
        for s in slots:
            slot = s.get("slot")
            status = s.get("status") or ("idle" if s.get("idle") else "unknown")
            task = (s.get("task") or "").replace("\n", " ")[:120]
            issue = s.get("issue") or "-"
            pr = s.get("pr") or "-"
            branch = s.get("branch") or "-"
            out.append(f"- slot:{slot} {s.get('name') or ''} status={status} idle={s.get('idle')} issue={issue} pr={pr} branch={branch} task={task}")
    else:
        out.append("- UNKNOWN - MoP slot snapshot unavailable")

    out += ["", "## Active PRs"]
    prs = live.get("prs") or []
    if live_skipped:
        out.append("- (live snapshot skipped by --no-live; run live sync for GitHub PR state)")
    elif prs:
        for pr in prs:
            labs = labels(pr)
            pm_state = ",".join(x for x in labs if x.startswith("pm-state:")) or "pm-state:MISSING"
            blockers = ",".join(x for x in labs if x.startswith("pm-blocked:")) or "none"
            out.append(
                f"- PR #{pr.get('number')} - {pm_state}; blockers={blockers}; draft={str(bool(pr.get('isDraft'))).lower()}; "
                f"mergeState={pr.get('mergeStateStatus') or 'UNKNOWN'}; checks={check_state(pr)}; head={str(pr.get('headRefOid') or '')[:10]} - {pr.get('title') or ''}"
            )
    else:
        out.append("- (none or GitHub PR query unavailable)")

    out += ["", "## Ready Pool"]
    issues = live.get("issues") or []
    if live_skipped:
        out.append("- (live snapshot skipped by --no-live; run live sync for GitHub issue state)")
    elif issues:
        for issue in issues:
            labs = labels(issue)
            exact_priorities = [p for p in ("P0", "P1", "P2", "P3") if p in labs]
            pri = exact_priorities[0] if len(exact_priorities) == 1 else "P?"
            slots_l = ",".join(l for l in labs if l.startswith("slot:")) or "unassigned"
            out.append(f"- {pri} issue #{issue.get('number')} {slots_l} - {issue.get('title') or ''}")
    else:
        out.append("- (none or GitHub issue query unavailable)")

    excerpt, line_count = legacy_excerpt()
    out += ["", "## Archived Prior View"]
    if line_count:
        out.append(f"- Previous pm-todo.md had {line_count} lines and was archived by sync. Legacy rows are not re-rendered into the live operational view.")
    else:
        out.append("- (none)")
    out.append("")
    return "\n".join(out)


def sync(args: argparse.Namespace) -> None:
    init_db()
    ingest_sentinels()
    live_skipped = bool(args.no_live)
    live, health = ({}, {"live": "skipped"}) if live_skipped else collect_live()
    if not args.no_live:
        # A single terminal-reconcile pass reaches the fixpoint: it resolves
        # every row its live predicate marks terminal, and
        # auto_reconcile_from_live only resolves kind='issue_routing' rows,
        # which the terminal predicate never selects, so it cannot expose a new
        # terminal row for a redundant second pass.
        auto_reconcile_terminal_obligations()
        auto_reconcile_from_live(live)
    obligations = open_obligations()
    rendered = render_markdown(live, obligations, health, args.reason, live_skipped=live_skipped)
    rendered_hash = hashlib.sha256(rendered.encode()).hexdigest()
    old_line_count = 0
    if TODO_PATH.exists():
        old_line_count = len(TODO_PATH.read_text(encoding="utf-8", errors="ignore").splitlines())
    stale_pruned = max(0, old_line_count - len(rendered.splitlines()))

    con = connect()
    event_max = con.execute("SELECT COALESCE(MAX(id), 0) AS m FROM events").fetchone()["m"]
    con.execute(
        """
        INSERT INTO sync_runs(ts, reason, input_event_id_max, obligations_open, rendered_path, rendered_hash, stale_rows_pruned, source_health_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (utc_now(), args.reason, event_max, len(obligations), str(TODO_PATH), rendered_hash, stale_pruned, json.dumps(health, sort_keys=True)),
    )
    con.commit()
    con.close()

    if args.write:
        ensure_parent(TODO_PATH)
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        if TODO_PATH.exists():
            archive = ARCHIVE_DIR / f"pm-todo-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.md"
            shutil.copy2(TODO_PATH, archive)
        tmp = TODO_PATH.with_suffix(".md.tmp")
        tmp.write_text(rendered, encoding="utf-8")
        os.replace(tmp, TODO_PATH)
        print(f"wrote {TODO_PATH} hash={rendered_hash} obligations_open={len(obligations)} stale_rows_pruned={stale_pruned}")
    else:
        print(rendered)


def status_dict(*, read_only: bool = False) -> dict[str, Any]:
    if not read_only:
        init_db()
        ingest_sentinels()
        auto_reconcile_terminal_obligations()
    con = connect_readonly() if read_only else connect()
    event_max = con.execute("SELECT COALESCE(MAX(id), 0) AS m FROM events").fetchone()["m"]
    sync_row = con.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    open_count = con.execute("SELECT COUNT(*) AS c FROM obligations WHERE status='open'").fetchone()["c"]
    horizon_counts = {
        row["horizon"]: row["c"]
        for row in con.execute(
            "SELECT horizon, COUNT(*) AS c FROM obligations WHERE status='open' GROUP BY horizon"
        ).fetchall()
    }
    high_horizon_counts = {
        row["horizon"]: row["c"]
        for row in con.execute(
            "SELECT horizon, COUNT(*) AS c FROM obligations WHERE status='open' AND severity IN ('critical','high') GROUP BY horizon"
        ).fetchall()
    }
    pending_packet_count = con.execute("SELECT COUNT(*) AS c FROM transition_packets WHERE status='pending'").fetchone()["c"]
    critical_count = con.execute("SELECT COUNT(*) AS c FROM obligations WHERE status='open' AND severity IN ('critical','high')").fetchone()["c"]
    hourly_critical_count = con.execute(
        "SELECT COUNT(*) AS c FROM obligations WHERE status='open' AND horizon='hourly' AND severity IN ('critical','high')"
    ).fetchone()["c"]
    pending_rajiv_count = con.execute(
        """
        SELECT COUNT(*) AS c FROM obligations
        WHERE status='open'
          AND severity IN ('critical','high')
          AND (
            kind='pending_rajiv'
            OR lower(COALESCE(owner,''))='rajiv'
            OR lower(COALESCE(title,'') || ' ' || COALESCE(required_action,'') || ' ' || COALESCE(blocker,'')) LIKE '%pending rajiv%'
            OR lower(COALESCE(title,'') || ' ' || COALESCE(required_action,'') || ' ' || COALESCE(blocker,'')) LIKE '%awaiting rajiv%'
            OR lower(COALESCE(title,'') || ' ' || COALESCE(required_action,'') || ' ' || COALESCE(blocker,'')) LIKE '%rajiv go-ahead%'
            OR lower(COALESCE(title,'') || ' ' || COALESCE(required_action,'') || ' ' || COALESCE(blocker,'')) LIKE '%rajiv ruling%'
            OR lower(COALESCE(title,'') || ' ' || COALESCE(required_action,'') || ' ' || COALESCE(blocker,'')) LIKE '%pending your%'
            OR lower(COALESCE(title,'') || ' ' || COALESCE(required_action,'') || ' ' || COALESCE(blocker,'')) LIKE '%awaiting your%'
          )
        """
    ).fetchone()["c"]
    hourly_pending_rajiv_count = con.execute(
        """
        SELECT COUNT(*) AS c FROM obligations
        WHERE status='open'
          AND horizon='hourly'
          AND severity IN ('critical','high')
          AND (
            kind='pending_rajiv'
            OR lower(COALESCE(owner,''))='rajiv'
            OR lower(COALESCE(title,'') || ' ' || COALESCE(required_action,'') || ' ' || COALESCE(blocker,'')) LIKE '%pending rajiv%'
            OR lower(COALESCE(title,'') || ' ' || COALESCE(required_action,'') || ' ' || COALESCE(blocker,'')) LIKE '%awaiting rajiv%'
            OR lower(COALESCE(title,'') || ' ' || COALESCE(required_action,'') || ' ' || COALESCE(blocker,'')) LIKE '%rajiv go-ahead%'
            OR lower(COALESCE(title,'') || ' ' || COALESCE(required_action,'') || ' ' || COALESCE(blocker,'')) LIKE '%rajiv ruling%'
            OR lower(COALESCE(title,'') || ' ' || COALESCE(required_action,'') || ' ' || COALESCE(blocker,'')) LIKE '%pending your%'
            OR lower(COALESCE(title,'') || ' ' || COALESCE(required_action,'') || ' ' || COALESCE(blocker,'')) LIKE '%awaiting your%'
          )
        """
    ).fetchone()["c"]
    actionable_critical_count = max(0, int(hourly_critical_count) - int(hourly_pending_rajiv_count))
    sync_event_max = int(sync_row["input_event_id_max"]) if sync_row else 0
    line_count = 0
    prune_marker = False
    current_hash = None
    if TODO_PATH.exists():
        text = TODO_PATH.read_text(encoding="utf-8", errors="ignore")
        line_count = len(text.splitlines())
        prune_marker = "FRESH-SESSION FIRST TASK" in text[:2000] or "PM_TODO_DRIFT stale rows" in text[:3000]
        current_hash = hashlib.sha256(text.encode()).hexdigest()
    effective_event_max = int(event_max)
    if sync_row and current_hash == sync_row["rendered_hash"] and event_max > sync_event_max:
        non_generated_events = con.execute(
            """
            SELECT COUNT(*) AS c FROM events
            WHERE id > ?
              AND NOT (event_type='pm_todo_manual_edit' AND target_type='file' AND target_id='pm-todo.md')
            """,
            (sync_event_max,),
        ).fetchone()["c"]
        if not non_generated_events:
            effective_event_max = sync_event_max
    con.close()
    reasons = []
    if effective_event_max > sync_event_max:
        reasons.append("events_after_last_sync")
    if actionable_critical_count:
        reasons.append("actionable_high_open_obligations")
    if pending_packet_count:
        reasons.append("pending_transition_packets")
    if line_count > 300:
        reasons.append("pm_todo_line_count_gt_300")
    if prune_marker:
        reasons.append("pm_todo_prune_marker_present")
    return {
        "db": str(DB_PATH),
        "todo": str(TODO_PATH),
        "event_max": event_max,
        "effective_event_max": effective_event_max,
        "last_sync_event_max": sync_event_max,
        "last_sync_id": int(sync_row["id"]) if sync_row else None,
        "last_sync_ts": sync_row["ts"] if sync_row else None,
        "open_obligations": open_count,
        "open_obligations_by_horizon": horizon_counts,
        "pending_transition_packets": pending_packet_count,
        "high_open_obligations": critical_count,
        "high_open_obligations_by_horizon": high_horizon_counts,
        "actionable_high_open_obligations": actionable_critical_count,
        "actionable_hourly_high_open_obligations": actionable_critical_count,
        "pending_rajiv_high_obligations": pending_rajiv_count,
        "pending_rajiv_hourly_high_obligations": hourly_pending_rajiv_count,
        "pm_todo_lines": line_count,
        "sync_required": bool(reasons),
        "reasons": reasons,
    }


def print_status(args: argparse.Namespace) -> None:
    status = status_dict(read_only=bool(getattr(args, "read_only", False)))
    if args.format == "json":
        print(json.dumps(status, sort_keys=True))
        return
    if not status["sync_required"]:
        return
    print("━━━ [PM_OPS_SYNC_REQUIRED] ━━━")
    print(f"PM ops SQLite ledger needs sync: {', '.join(status['reasons'])}")
    print(f"DB: {status['db']}")
    print(f"pm-todo.md lines: {status['pm_todo_lines']}; open obligations: {status['open_obligations']}")
    print("Run the live sync as a background job (never inline):")
    print("bash /Users/rajiv/.claude/scripts/pm-ops-sync-bg.sh --detach")
    print("Use --no-live only as a degraded manual fallback when GitHub/MoP are unavailable.")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")

    p = sub.add_parser("record")
    p.add_argument("--source", required=True)
    p.add_argument("--event", required=True)
    p.add_argument("--actor")
    p.add_argument("--ts")
    p.add_argument("--target-type")
    p.add_argument("--target-id")
    p.add_argument("--pr", type=int)
    p.add_argument("--issue", type=int)
    p.add_argument("--slot", type=int)
    p.add_argument("--head-sha")
    p.add_argument("--payload", action="append")
    p.add_argument("--payload-json")
    p.add_argument("--dedupe-key")
    p.add_argument("--dedupe", action="store_true")
    p.add_argument("--print-id", action="store_true")

    p = sub.add_parser("obligation-upsert")
    p.add_argument("--kind", required=True)
    p.add_argument("--severity", default="normal")
    p.add_argument("--target-type")
    p.add_argument("--target-id")
    p.add_argument("--pr", type=int)
    p.add_argument("--issue", type=int)
    p.add_argument("--slot", type=int)
    p.add_argument("--owner")
    p.add_argument("--title")
    p.add_argument("--action")
    p.add_argument("--blocker")
    p.add_argument("--horizon", choices=HORIZONS)
    p.add_argument("--next-review-at")
    p.add_argument("--suppress-until")
    p.add_argument("--dedupe-group")
    p.add_argument("--evidence", action="append")
    p.add_argument("--evidence-json")
    p.add_argument("--print-id", action="store_true")

    p = sub.add_parser("obligation-resolve")
    p.add_argument("--kind", required=True)
    p.add_argument("--id", type=int)
    p.add_argument("--target-type")
    p.add_argument("--target-id")
    p.add_argument("--pr", type=int)
    p.add_argument("--issue", type=int)
    p.add_argument("--slot", type=int)
    p.add_argument("--reason")
    p.add_argument("--external-state")

    p = sub.add_parser("obligation-resolve-target")
    p.add_argument("--kind")
    p.add_argument("--kind-prefix")
    p.add_argument("--target-type")
    p.add_argument("--target-id")
    p.add_argument("--pr", type=int)
    p.add_argument("--issue", type=int)
    p.add_argument("--reason", default="target_resolved")
    p.add_argument("--external-state")

    p = sub.add_parser("transition-packet-upsert")
    p.add_argument("--packet-type", required=True)
    p.add_argument("--packet-key")
    p.add_argument("--status", choices=["pending", "consumed", "rejected"], default="pending")
    p.add_argument("--event-file")
    p.add_argument("--pr", type=int)
    p.add_argument("--issue", type=int)
    p.add_argument("--slot", type=int)
    p.add_argument("--head-sha")
    p.add_argument("--branch")
    p.add_argument("--cwd")
    p.add_argument("--review-proof")
    p.add_argument("--review-verdict")
    p.add_argument("--qa-proof")
    p.add_argument("--payload-json")
    p.add_argument("--reason")
    p.add_argument("--print-id", action="store_true")

    p = sub.add_parser("transition-packet-status")
    p.add_argument("--packet-type", required=True)
    p.add_argument("--packet-key")
    p.add_argument("--status", choices=["pending", "consumed", "rejected"], required=True)
    p.add_argument("--event-file")
    p.add_argument("--pr", type=int)
    p.add_argument("--issue", type=int)
    p.add_argument("--slot", type=int)
    p.add_argument("--head-sha")
    p.add_argument("--branch")
    p.add_argument("--payload-json")
    p.add_argument("--reason")
    p.add_argument("--print-id", action="store_true")

    p = sub.add_parser("transition-packet-list")
    p.add_argument("--status", choices=["pending", "consumed", "rejected"])
    p.add_argument("--packet-type")
    p.add_argument("--pr", type=int)
    p.add_argument("--issue", type=int)
    p.add_argument("--slot", type=int)
    p.add_argument("--head-sha")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--format", choices=["text", "json"], default="text")

    sub.add_parser("ingest-sentinels")

    p = sub.add_parser("sync")
    p.add_argument("--reason", default="manual")
    p.add_argument("--write", action="store_true")
    p.add_argument("--no-live", action="store_true")

    p = sub.add_parser("snapshot")
    p.add_argument("--surface", choices=HORIZONS, required=True)
    p.add_argument("--limit", type=int, default=12)
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.add_argument("--read-only", action="store_true", help="do not ingest sentinels, auto-reconcile, or touch last_surface_at")

    p = sub.add_parser("status")
    p.add_argument("--format", choices=["prompt", "json"], default="prompt")
    p.add_argument("--read-only", action="store_true", help="read the ledger without sentinel ingestion or live reconciliation")

    args = parser.parse_args(argv)
    if args.cmd == "init":
        init_db()
    elif args.cmd == "record":
        record_event(args)
    elif args.cmd == "obligation-upsert":
        # Propagate ONLY the typed contract refusal (2). upsert_obligation also returns
        # truthy values on SUCCESS (it reports the affected id), so a bare truthiness check
        # would turn every successful upsert into a non-zero exit.
        _upsert_rc = upsert_obligation(args)
        if _upsert_rc == 2:
            return 2
    elif args.cmd == "obligation-resolve":
        resolve_obligation(args)
    elif args.cmd == "obligation-resolve-target":
        resolve_target_obligations(args)
    elif args.cmd == "transition-packet-upsert":
        upsert_transition_packet(args)
    elif args.cmd == "transition-packet-status":
        set_transition_packet_status(args)
    elif args.cmd == "transition-packet-list":
        list_transition_packets(args)
    elif args.cmd == "ingest-sentinels":
        ingest_sentinels()
    elif args.cmd == "sync":
        sync(args)
    elif args.cmd == "snapshot":
        print_snapshot(args)
    elif args.cmd == "status":
        print_status(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
