# Vendored from pm-operator (RETIRED) src/pm_operator/control_plane/runtime_observation.py on 2026-09-03
# Rajiv directive: remove pm_operator dependency from sakshi-heartbeat.py. Self-contained (stdlib only).
"""Canonical read-only runtime observation for OMP and Claude Code/MoP.

The adapter is deliberately observation-only: it never clears a session,
changes ownership, or writes a projection.  Slot consumers get one normalized
view of the bound top-level session, the authoritative MoP row, checkout
facts, and successful clear events.  OMP panes use their canonical journals;
Claude Code panes use the matching project transcript.  Unknown or ambiguous
runtime identity fails closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


OMP_SESSION_IDS = frozenset({"pm", "1", "2", "3", "4"})
RUNTIME_SOURCES = frozenset({"omp", "claude"})
SUCCESSFUL_CLEAR_EVENTS = frozenset({"slot_cleared", "clear_pending_executed"})
_ACTIVE_EVIDENCE_MAX_AGE = timedelta(hours=3)
_OWNERSHIP_FIELDS = (
    "slot",
    "repository_id",
    "assignment_epoch",
    "issue",
    "pr",
    "branch",
    "head_sha",
    "work_kind",
    "handoff_id",
    "claimed_at",
)
_INCOMPLETE_OWNER_FIELDS = ("repository_id", "slot", "assignment_epoch", "issue", "pr")
_TERMINAL_ERROR_RE = re.compile(
    r"(?:404|not found|does not exist|connection\s*refused|transport\s+(?:error|closed)|startup\s+failure)",
    re.IGNORECASE,
)


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _terminal_error_evidence(
    records: list[Mapping[str, Any]], *, session_start: datetime | None
) -> str | None:
    """Return only typed terminal evidence belonging to the current session."""
    for record in reversed(records):
        timestamp = parse_timestamp(record.get("timestamp"))
        if timestamp is None or (session_start is not None and timestamp < session_start):
            continue
        kind = str(record.get("type") or record.get("event_type") or "").lower()
        status = str(record.get("status") or "").lower()
        # Terminality is a current state, not a historical attribute.  The
        # newest timestamped current-session record must itself be a typed
        # terminal record; any later productive/other record proves recovery
        # or makes the state ambiguous and therefore fails closed.
        if kind not in {"startup_error", "transport_error", "turn_error"} or status not in {"terminal", "failed", "error"}:
            return None
        try:
            encoded = json.dumps(record, ensure_ascii=True, sort_keys=True)
        except (TypeError, ValueError):
            continue
        match = _TERMINAL_ERROR_RE.search(encoded)
        return match.group(0) if match else None
    return None


def _slot_key(value: Any) -> str:
    return str(value if value is not None else "").strip()


def ownership_fingerprint_from_mapping(value: Mapping[str, Any]) -> str:
    """Stable identity for an ownership record, excluding runtime activity."""

    payload = {field: value.get(field) for field in _OWNERSHIP_FIELDS}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def incomplete_owner_identity(value: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """Return the stable ownership identity used before actionable rendering."""

    # Use only the authoritative MoP owner tuple.  Rendered PR prose may have
    # a successor PR/branch/head even when the committed MoP row is still the
    # issue-only legacy shape; those projections must not defeat dedupe.
    aliases = {
        "repository_id": ("repository_id", "owner_repository_id"),
        "slot": ("slot", "owner_slot", "id"),
        "assignment_epoch": ("assignment_epoch", "owner_epoch", "owner_assignment_epoch"),
        "issue": ("owner_issue", "mop_issue", "issue"),
        "pr": ("owner_pr", "mop_pr", "pr"),
    }
    normalized: dict[str, Any] = {}
    for field, names in aliases.items():
        raw = next(
            (value[name] for name in names if name in value),
            None,
        )
        if isinstance(raw, str) and raw.strip().lower() in {"", "null", "none"}:
            raw = None
        normalized[field] = raw
    epoch = normalized.get("assignment_epoch")
    if epoch is None:
        return None
    if normalized.get("issue") is None and normalized.get("pr") is None:
        return None
    return tuple(
        None if normalized.get(field) is None else str(normalized.get(field))
        for field in _INCOMPLETE_OWNER_FIELDS
    )


def suppress_repeated_incomplete_owner(
    previous_rows: list[Mapping[str, Any]], current: Mapping[str, Any]
) -> bool:
    """Suppress an unchanged incomplete owner using existing action rows."""

    identity = incomplete_owner_identity(current)
    if identity is None:
        return False
    return any(incomplete_owner_identity(row) == identity for row in previous_rows)


def _events(payload: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    raw = payload.get("events")
    if not isinstance(raw, list):
        return []
    return [event for event in raw if isinstance(event, Mapping)]


def _slot_rows(payload: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    raw = payload.get("slots")
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, Mapping)]


def _active_omp_evidence(
    records: list[Mapping[str, Any]],
    *,
    session_start: datetime | None,
    now: datetime,
) -> tuple[bool, str | None]:
    """Infer detached work from an unfinished OMP tool execution.

    OMP can temporarily omit ``active_turn_id`` while a tool is still running.
    The event stream is the durable runtime fact in that window.  This parser
    only recognizes existing OMP event shapes; it does not create a new state.
    """

    outstanding: dict[str, datetime] = {}
    latest_activity: str | None = None
    for record in records:
        kind = record.get("type")
        timestamp = parse_timestamp(record.get("timestamp"))
        if timestamp:
            latest_activity = timestamp.isoformat()
        if kind == "custom":
            custom_type = _text(record.get("customType")) or ""
            data = record.get("data")
            data = data if isinstance(data, Mapping) else {}
            call_id = _text(data.get("toolCallId") or data.get("callId"))
            if custom_type.endswith("tool_execution_start") and call_id:
                if timestamp:
                    outstanding[call_id] = timestamp
            elif custom_type.endswith(("tool_execution_end", "tool_execution_result")) and call_id:
                outstanding.pop(call_id, None)
            continue
        if kind != "message":
            continue
        message = record.get("message")
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        content_items = content if isinstance(content, list) else []
        if message.get("role") == "assistant":
            for item in content_items:
                if not isinstance(item, Mapping) or item.get("type") != "toolCall":
                    continue
                call_id = _text(item.get("id") or item.get("toolCallId"))
                if call_id:
                    if timestamp:
                        outstanding[call_id] = timestamp
        elif message.get("role") == "toolResult":
            call_id = _text(message.get("toolCallId"))
            if call_id:
                outstanding.pop(call_id, None)
    active = any(
        timestamp >= (session_start or timestamp)
        and now - timestamp <= _ACTIVE_EVIDENCE_MAX_AGE
        for timestamp in outstanding.values()
    )
    return active, latest_activity


@dataclass(frozen=True)
class RuntimeObservation:
    slot: str
    source: str
    session_path: str | None
    session_id: str | None
    session_start: datetime | None
    latest_record: datetime | None
    effective_start: datetime | None
    occupied: bool | None
    idle: bool | None
    status: str | None
    dnd: bool | None
    active_turn_id: str | None
    active_turn_state: str | None
    active: bool | None
    issue: Any = None
    pr: Any = None
    branch: str | None = None
    head_sha: str | None = None
    assignment_epoch: int | None = None
    repository_id: Any = None
    work_kind: str | None = None
    handoff_id: str | None = None
    claimed_at: str | None = None
    checkout_clean: bool | None = None
    checkout_head: str | None = None
    clear_event_type: str | None = None
    clear_event_at: datetime | None = None
    handoff_ready: bool = False
    clear_due: bool = False
    omp_activity_at: datetime | None = None
    terminal_error: str | None = None
    error: str | None = None

    def is_clear_due(self, now: datetime, *, threshold_hours: float = 3) -> bool:
        """Return age policy for this observation without mutating it."""

        return RuntimeObservationAdapter.clear_due_for(
            self.effective_start,
            occupied=self.occupied,
            idle=self.idle,
            active=self.active,
            is_pm=self.slot == "pm",
            now=now,
            threshold_hours=threshold_hours,
        )

    @property
    def owner_tuple(self) -> dict[str, Any]:
        return {
            "repository_id": self.repository_id,
            "slot": self.slot,
            "assignment_epoch": self.assignment_epoch,
            "issue": self.issue,
            "pr": self.pr,
            "branch": self.branch,
            "head_sha": self.head_sha,
            "active_turn_id": self.active_turn_id,
            "work_kind": self.work_kind,
            "handoff_id": self.handoff_id,
            "claimed_at": self.claimed_at,
        }

    def fingerprint(self) -> str:
        payload = {
            "slot": self.slot,
            "session_id": self.session_id,
            "session_start": _iso(self.session_start),
            "latest_record": _iso(self.latest_record),
            "effective_start": _iso(self.effective_start),
            "occupied": self.occupied,
            "idle": self.idle,
            "status": self.status,
            "dnd": self.dnd,
            "active_turn_id": self.active_turn_id,
            "active_turn_state": self.active_turn_state,
            "active": self.active,
            "issue": self.issue,
            "pr": self.pr,
            "branch": self.branch,
            "head_sha": self.head_sha,
            "assignment_epoch": self.assignment_epoch,
            "repository_id": self.repository_id,
            "work_kind": self.work_kind,
            "handoff_id": self.handoff_id,
            "claimed_at": self.claimed_at,
            "checkout_clean": self.checkout_clean,
            "checkout_head": self.checkout_head,
            "clear_event_type": self.clear_event_type,
            "clear_event_at": _iso(self.clear_event_at),
            "handoff_ready": self.handoff_ready,
            "error": self.error,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def ownership_fingerprint(self) -> str:
        """Fingerprint only the committed ownership tuple, never session churn."""
        return ownership_fingerprint_from_mapping(self.owner_tuple)


class RuntimeObservationAdapter:
    """Read-only adapter for one canonical OMP/MoP observation.

    The default adapter binds the session source to the live tmux pane.  Tests
    and callers with an explicit fixture root retain the historical OMP source
    by passing no runtime source.  An unknown pane command is intentionally
    fail-closed instead of falling back to whichever journal has the newest
    mtime.
    """

    def __init__(
        self,
        *,
        omp_sessions_root: Path | None = None,
        claude_projects_root: Path | None = None,
        runtime_source: str | None = "auto",
        runtime_project_dir: Path | None = None,
        session_age_activity: bool = False,
    ) -> None:
        self.omp_sessions_root = omp_sessions_root or Path(
            os.environ.get("HEYDONNA_OMP_SESSIONS_ROOT", str(Path.home() / ".omp/sessions"))
        )
        self.claude_projects_root = claude_projects_root or Path(
            os.environ.get(
                "HEYDONNA_CLAUDE_PROJECTS_ROOT",
                str(Path.home() / ".claude/projects"),
            )
        )
        self.runtime_source = runtime_source
        self.runtime_project_dir = runtime_project_dir
        self.session_age_activity = session_age_activity

    @staticmethod
    def _pane_target(slot: str) -> str:
        pane = "0" if slot == "pm" else slot
        return f"0:0.{pane}"

    @classmethod
    def detect_runtime_identity(cls, slot: str) -> tuple[str | None, Path | None]:
        """Resolve runtime kind and project from the exact live pane.

        The command is an identity signal, not a model/provider alias.  A
        missing pane, unknown command, or missing path returns ``(None, None)``
        so consumers remain fail-closed.
        """

        try:
            result = subprocess.run(
                [
                    "tmux",
                    "display-message",
                    "-t",
                    cls._pane_target(_slot_key(slot)),
                    "-p",
                    "#{pane_current_command}\t#{pane_current_path}",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return None, None
        if result.returncode != 0:
            return None, None
        command, separator, raw_path = result.stdout.strip().partition("\t")
        if not separator or not raw_path.strip():
            return None, None
        command = Path(command.strip()).name.lower()
        if command in {"omp", "opencode"}:
            source = "omp"
        elif command in {"claude", "claude-code"}:
            source = "claude"
        else:
            return None, None
        return source, Path(raw_path.strip())

    def _runtime_identity(self, slot: str) -> tuple[str | None, Path | None]:
        if self.runtime_source == "auto":
            # Explicit fixture roots are the existing OMP test contract.  The
            # production default probes the live pane instead.
            if self.runtime_project_dir is None and self.omp_sessions_root != Path(
                os.environ.get("HEYDONNA_OMP_SESSIONS_ROOT", str(Path.home() / ".omp/sessions"))
            ):
                return "omp", None
            return self.detect_runtime_identity(slot)
        if self.runtime_source in RUNTIME_SOURCES:
            return self.runtime_source, self.runtime_project_dir
        return None, None

    @staticmethod
    def clear_due_for(
        effective_start: datetime | None,
        *,
        occupied: bool | None,
        idle: bool | None,
        active: bool | None,
        is_pm: bool = False,
        now: datetime,
        threshold_hours: float = 3,
    ) -> bool:
        if effective_start is None or active is not False:
            return False
        # PM has no numbered-slot MoP ownership row.  Its explicit policy is
        # top-level OMP session age plus inactive runtime evidence; numbered
        # slots remain fail-closed on authoritative occupied/idle ownership.
        if not is_pm and (occupied is not True or idle is not True):
            return False
        now = now.astimezone(timezone.utc)
        return (now - effective_start).total_seconds() >= threshold_hours * 3600
    def _session_records(self, path: Path) -> tuple[list[Mapping[str, Any]], datetime | None, datetime | None, str | None]:
        records: list[Mapping[str, Any]] = []
        first_timestamp: datetime | None = None
        session_timestamp: datetime | None = None
        session_id: str | None = None
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                # Runtime activity is authoritative even after a long OMP
                # session. Read the complete top-level JSONL so an
                # outstanding tool start cannot be hidden beyond a prefix.
                for line in handle:
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(value, Mapping):
                        continue
                    records.append(value)
                    timestamp = parse_timestamp(value.get("timestamp"))
                    first_timestamp = first_timestamp or timestamp
                    candidate_session_id = _text(value.get("sessionId") or value.get("id"))
                    if value.get("type") == "session" and timestamp:
                        session_timestamp = timestamp
                        session_id = candidate_session_id
                    elif session_id is None and candidate_session_id:
                        session_id = candidate_session_id
                if session_timestamp is None:
                    session_timestamp = first_timestamp
        except OSError:
            return [], None, None, None
        latest = session_timestamp
        for record in records:
            timestamp = parse_timestamp(record.get("timestamp"))
            if timestamp and (latest is None or timestamp > latest):
                latest = timestamp
        return records, session_timestamp, latest, session_id

    def latest_omp_session(self, slot: str) -> tuple[Path, datetime, datetime] | None:
        session_name = "heydonna-pm" if slot == "pm" else f"heydonna-slot{slot}"
        session_dir = self.omp_sessions_root / session_name
        candidates: list[tuple[datetime, datetime, float, Path]] = []
        for path in session_dir.glob("*.jsonl"):
            if not path.is_file():
                continue
            _records, start, latest, _session_id = self._session_records(path)
            if start is None or latest is None:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            candidates.append((start, latest, mtime, path))
        if not candidates:
            return None
        start, latest, _mtime, path = max(candidates, key=lambda item: (item[0], item[2]))
        return path, start, latest

    @staticmethod
    def _claude_project_name(project_dir: Path) -> str:
        return "-" + str(project_dir).strip("/").replace("/", "-")

    def latest_claude_session(
        self, project_dir: Path, *, now: datetime | None = None
    ) -> tuple[Path, datetime, datetime, str] | None:
        """Select one direct Claude session for the bound pane project.

        Nested reviewer/background JSONL is excluded.  Equal-start candidates
        are ambiguous and fail closed; a restart/session change therefore
        selects the one newer session identity rather than a stale OMP file.
        """

        session_dir = self.claude_projects_root / self._claude_project_name(project_dir)
        candidates: list[tuple[datetime, datetime | None, float, Path, str]] = []
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        try:
            paths = list(session_dir.glob("*.jsonl"))
        except OSError:
            return None
        for path in paths:
            if not path.is_file():
                continue
            start, session_id = self._session_identity(path)
            if start is None or not session_id:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            latest_hint = self._session_activity_hint(path)
            candidates.append((start, latest_hint, mtime, path, session_id))
        if not candidates:
            return None
        active = [
            item
            for item in candidates
            if item[1] is not None and now - item[1] <= _ACTIVE_EVIDENCE_MAX_AGE
        ]
        # A resumed/replacement Claude session is selected by the newest
        # substantive activity, not mtime or session start.  Equal activity
        # timestamps cannot be bound to one pane and fail closed.
        if active:
            newest_activity = max(item[1] for item in active if item[1] is not None)
            newest = [item for item in active if item[1] == newest_activity]
            if len(newest) != 1:
                return None
        else:
            newest_start = max(item[0] for item in candidates)
            newest = [item for item in candidates if item[0] == newest_start]
        if len(newest) != 1:
            return None
        start, _latest_hint, _mtime, path, session_id = newest[0]
        _records, parsed_start, latest, parsed_session_id = self._session_records(path)
        if parsed_start is None or parsed_session_id is None:
            return None
        if parsed_start != start or parsed_session_id != session_id:
            return None
        return path, start, latest, session_id

    @staticmethod
    def _session_activity_hint(path: Path) -> datetime | None:
        """Read a bounded tail to identify a recently resumed transcript."""

        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 128 * 1024))
                text = handle.read().decode("utf-8", errors="ignore")
        except OSError:
            return None
        latest: datetime | None = None
        for line in text.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(value, Mapping):
                continue
            # Session bootstrap records identify creation, not resumed work.
            # Count substantive top-level records only for activity binding.
            if value.get("type") in {"mode", "file-history-snapshot"}:
                continue
            timestamp = parse_timestamp(value.get("timestamp"))
            if timestamp and (latest is None or timestamp > latest):
                latest = timestamp
        return latest

    @staticmethod
    def _session_identity(path: Path) -> tuple[datetime | None, str | None]:
        """Read only the JSONL identity prefix while selecting a session.

        Claude transcripts can be multi-gigabyte.  Selection must bind the
        pane's project and a session identity before parsing one chosen file;
        scanning every historical transcript in full made the heartbeat
        itself a source of latency.  Activity is parsed fully only after this
        identity pass selects one unique session start.
        """

        first_timestamp: datetime | None = None
        session_timestamp: datetime | None = None
        session_id: str | None = None
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                consumed = 0
                for line in handle:
                    consumed += len(line.encode("utf-8", errors="ignore"))
                    if consumed > 64 * 1024:
                        break
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(value, Mapping):
                        continue
                    timestamp = parse_timestamp(value.get("timestamp"))
                    first_timestamp = first_timestamp or timestamp
                    candidate_id = _text(value.get("sessionId") or value.get("id"))
                    if value.get("type") == "session" and timestamp:
                        session_timestamp = timestamp
                        session_id = candidate_id
                    elif session_id is None and candidate_id:
                        session_id = candidate_id
        except (OSError, ValueError):
            return None, None
        return session_timestamp or first_timestamp, session_id

    @staticmethod
    def _successful_clear(slot: str, mop_events: Mapping[str, Any] | None) -> tuple[datetime, str] | None:
        candidates: list[tuple[datetime, str]] = []
        for source_type, payload in (mop_events or {}).items():
            for event in _events(payload):
                event_type = _text(event.get("event_type")) or _text(source_type)
                if event_type not in SUCCESSFUL_CLEAR_EVENTS:
                    continue
                event_slot = _slot_key(event.get("slot") or event.get("slot_id"))
                timestamp = parse_timestamp(event.get("timestamp"))
                if event_slot == slot and timestamp:
                    candidates.append((timestamp, event_type))
        return max(candidates, default=None, key=lambda item: item[0])

    def observe_slot(
        self,
        slot: str,
        *,
        mop_row: Mapping[str, Any] | None,
        mop_events: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> RuntimeObservation:
        slot = _slot_key(slot)
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        row = dict(mop_row or {})
        runtime_source, runtime_project_dir = self._runtime_identity(slot)
        session: tuple[Path, datetime, datetime] | tuple[Path, datetime, datetime, str] | None
        if runtime_source == "omp" and slot in OMP_SESSION_IDS:
            session = self.latest_omp_session(slot)
        elif runtime_source == "claude":
            session = (
                self.latest_claude_session(runtime_project_dir, now=now)
                if runtime_project_dir is not None
                else None
            )
        else:
            session = None
        records: list[Mapping[str, Any]] = []
        session_start = latest_record = None
        session_id = None
        path: Path | None = None
        if session:
            path, session_start, latest_record = session[:3]
            records, session_start, latest_record, parsed_session_id = self._session_records(path)
            session_id = parsed_session_id or (session[3] if len(session) == 4 else None)
        clear = self._successful_clear(slot, mop_events)
        clear_at, clear_type = clear if clear else (None, None)
        age_boundary = session_start if runtime_source == "claude" else latest_record
        effective = max((item for item in (age_boundary, clear_at) if item), default=None)
        omp_active, omp_activity_at = _active_omp_evidence(records, session_start=session_start, now=now)
        terminal_error = _terminal_error_evidence(records, session_start=session_start)
        if runtime_source == "claude" and self.session_age_activity:
            # Claude's top-level JSONL has no OMP tool_execution event shape.
            # A recently written bound session is active evidence; once quiet
            # beyond the existing freshness window it may become due.
            omp_active = bool(
                latest_record is not None
                and now - latest_record <= _ACTIVE_EVIDENCE_MAX_AGE
            )
        state = _text(row.get("active_turn_state"))
        active_id = _text(row.get("active_turn_id"))
        state_active = state.lower() in {"active", "productive", "working", "busy"} if state else False
        active = bool(state_active or active_id or omp_active)
        occupied = row.get("occupied") if isinstance(row.get("occupied"), bool) else None
        # A stale/free MoP projection cannot erase a live OMP turn. The OMP
        # event stream remains authoritative during pre-cutover owner drift;
        # only suppress a raw MoP active signal when OMP proves no work.
        if occupied is False and not omp_active:
            active = False
        status = _text(row.get("status") or row.get("state"))
        idle_value = row.get("idle") if isinstance(row.get("idle"), bool) else None
        # FREE is the authoritative ownership boundary.  A stale raw idle flag
        # must not turn a free/inactive slot into clear_due work.
        if occupied is False:
            # A free slot is reusable only when the canonical runtime is also
            # inactive. A stale raw idle flag must not authorize clear, nudge,
            # or a fresh claim over an active OMP turn.
            idle = not active
        else:
            idle = (not active) if idle_value is None else bool(idle_value) and not active
        checkout = row.get("checkout") if isinstance(row.get("checkout"), Mapping) else {}
        checkout_clean = row.get("checkout_clean", checkout.get("clean"))
        if not isinstance(checkout_clean, bool):
            checkout_clean = None
        checkout_head = _text(row.get("checkout_head") or checkout.get("head"))
        repository_id = row.get("repository_id")
        work_kind = _text(row.get("work_kind"))
        handoff_id = _text(row.get("handoff_id"))
        claimed_at = _text(row.get("claimed_at"))
        target_present = row.get("issue") is not None or row.get("pr") is not None
        head_valid = row.get("pr") is None or bool(_text(row.get("head_sha") or row.get("head")))
        handoff_ready = bool(
            session is not None
            and occupied is True
            and repository_id is not None
            and row.get("assignment_epoch") is not None
            and target_present
            and head_valid
            and _text(row.get("branch"))
            and work_kind
            and handoff_id
            and claimed_at
            and not active
        )
        if session:
            error = None
        elif runtime_source is None:
            error = "runtime identity unavailable"
        elif runtime_source == "claude":
            error = "no bound Claude session file found"
        else:
            error = "no top-level OMP session file found" if slot in OMP_SESSION_IDS else None
        return RuntimeObservation(
            slot=slot,
            source=(f"{runtime_source}_top_level" if runtime_source else "unknown"),
            session_path=str(path) if path else None,
            session_id=session_id,
            session_start=session_start,
            latest_record=latest_record,
            effective_start=effective,
            occupied=occupied,
            idle=idle if session or runtime_source not in {"omp", None} or slot not in OMP_SESSION_IDS else None,
            status=status,
            dnd=row.get("dnd") if isinstance(row.get("dnd"), bool) else None,
            active_turn_id=active_id,
            active_turn_state=state,
            active=active if session or runtime_source not in {"omp", None} or slot not in OMP_SESSION_IDS else None,
            issue=row.get("issue"),
            pr=row.get("pr"),
            branch=_text(row.get("branch")),
            head_sha=_text(row.get("head_sha") or row.get("head")),
            assignment_epoch=row.get("assignment_epoch") if isinstance(row.get("assignment_epoch"), int) else None,
            repository_id=repository_id,
            work_kind=work_kind,
            handoff_id=handoff_id,
            claimed_at=claimed_at,
            checkout_clean=checkout_clean,
            checkout_head=checkout_head,
            clear_event_type=clear_type,
            clear_event_at=clear_at,
            handoff_ready=handoff_ready,
            clear_due=RuntimeObservationAdapter.clear_due_for(
                effective,
                occupied=occupied,
                idle=idle if session or runtime_source not in {"omp", None} or slot not in OMP_SESSION_IDS else None,
                active=active if session or runtime_source not in {"omp", None} or slot not in OMP_SESSION_IDS else None,
                is_pm=slot == "pm",
                now=now,
            ),
            omp_activity_at=omp_activity_at,
            terminal_error=terminal_error,
            error=error,
        )

    @staticmethod
    def suppress_repeated_incomplete_tuple(previous: RuntimeObservation | None, current: RuntimeObservation) -> bool:
        """Return true when unchanged authoritative facts need no new noise."""

        if previous is None or previous.ownership_fingerprint() != current.ownership_fingerprint():
            return False
        return (
            current.occupied is True
            and current.assignment_epoch is not None
            and not current.handoff_ready
            and (
                current.repository_id is None
                or (current.issue is None and current.pr is None)
                or not current.branch
                or not current.work_kind
                or not current.handoff_id
                or not current.claimed_at
                or (current.pr is not None and not current.head_sha)
            )
        )

    @staticmethod
    def apply_clear_result(observation: RuntimeObservation, *, status: str) -> RuntimeObservation:
        """Record clear outcome without ever changing ownership facts."""

        if status not in {"success", "failed", "deferred", "unknown"}:
            raise ValueError("invalid clear observation status")
        return observation
