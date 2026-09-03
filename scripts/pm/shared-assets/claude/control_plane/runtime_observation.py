"""Read-only runtime observation for the retained Sakshi heartbeat.

This adapter is independent of PM Operator. It observes the bound top-level
Claude/OMP transcript and the read-only MoP row; it never clears sessions,
changes ownership, sends messages, or writes a projection.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


ACTIVE_EVIDENCE_MAX_AGE = timedelta(hours=3)
SESSION_AGE_THRESHOLD_HOURS = 6


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


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _slot_key(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _events(payload: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("events"), list):
        return []
    return [event for event in payload["events"] if isinstance(event, Mapping)]


def _session_records(
    path: Path,
) -> tuple[list[Mapping[str, Any]], datetime | None, datetime | None, str | None]:
    records: list[Mapping[str, Any]] = []
    first: datetime | None = None
    start: datetime | None = None
    session_id: str | None = None
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(value, Mapping):
                    continue
                records.append(value)
                timestamp = parse_timestamp(value.get("timestamp"))
                first = first or timestamp
                candidate_id = _text(value.get("sessionId") or value.get("id"))
                if value.get("type") == "session" and timestamp:
                    start = timestamp
                    session_id = candidate_id
                elif session_id is None and candidate_id:
                    session_id = candidate_id
    except OSError:
        return [], None, None, None
    start = start or first
    latest = start
    for record in records:
        timestamp = parse_timestamp(record.get("timestamp"))
        if timestamp and (latest is None or timestamp > latest):
            latest = timestamp
    return records, start, latest, session_id


def _session_identity(path: Path) -> tuple[datetime | None, str | None]:
    _records, start, _latest, session_id = _session_records(path)
    return start, session_id


def _session_activity_hint(path: Path) -> datetime | None:
    _records, _start, latest, _session_id = _session_records(path)
    return latest


def _recent_activity(
    records: list[Mapping[str, Any]],
    *,
    session_start: datetime | None,
    now: datetime,
) -> bool:
    latest: datetime | None = None
    for record in records:
        timestamp = parse_timestamp(record.get("timestamp"))
        if timestamp and (session_start is None or timestamp >= session_start):
            if latest is None or timestamp > latest:
                latest = timestamp
    return latest is not None and now - latest <= ACTIVE_EVIDENCE_MAX_AGE


def _active_omp_evidence(
    records: list[Mapping[str, Any]],
    *,
    session_start: datetime | None,
    now: datetime,
) -> bool:
    """Recognize only unfinished top-level OMP tool calls as active work."""
    outstanding: set[str] = set()
    started_at: dict[str, datetime] = {}
    for record in records:
        timestamp = parse_timestamp(record.get("timestamp"))
        kind = record.get("type")
        if kind == "custom":
            custom_type = _text(record.get("customType")) or ""
            payload = record.get("data") if isinstance(record.get("data"), Mapping) else {}
            call_id = _text(payload.get("toolCallId") or payload.get("callId"))
            if not call_id:
                continue
            if custom_type.endswith("tool_execution_start"):
                outstanding.add(call_id)
                if timestamp:
                    started_at[call_id] = timestamp
            elif custom_type.endswith(("tool_execution_end", "tool_execution_result")):
                outstanding.discard(call_id)
                started_at.pop(call_id, None)
            continue
        if kind != "message" or not isinstance(record.get("message"), Mapping):
            continue
        message = record["message"]
        content = message.get("content") if isinstance(message.get("content"), list) else []
        if message.get("role") == "assistant":
            for item in content:
                if not isinstance(item, Mapping) or item.get("type") != "toolCall":
                    continue
                call_id = _text(item.get("id") or item.get("toolCallId"))
                if call_id:
                    outstanding.add(call_id)
                    if timestamp:
                        started_at[call_id] = timestamp
        elif message.get("role") == "toolResult":
            call_id = _text(message.get("toolCallId"))
            if call_id:
                outstanding.discard(call_id)
                started_at.pop(call_id, None)
    return any(
        timestamp >= (session_start or timestamp)
        and now - timestamp <= ACTIVE_EVIDENCE_MAX_AGE
        for timestamp in started_at.values()
        if timestamp
    )


@dataclass(frozen=True)
class RuntimeObservation:
    slot: str
    source: str
    session_path: str | None
    session_id: str | None
    runtime_session_id: str | None
    session_start: datetime | None
    session_started_at: datetime | None
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
    error: str | None = None


class RuntimeObservationAdapter:
    """Observe one bound runtime/MoP tuple without any mutation authority."""

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
            os.environ.get("HEYDONNA_CLAUDE_PROJECTS_ROOT", str(Path.home() / ".claude/projects"))
        )
        self.runtime_source = runtime_source
        self.runtime_project_dir = runtime_project_dir
        self.session_age_activity = session_age_activity

    @staticmethod
    def _pane_target(slot: str) -> str:
        return f"0:0.{0 if slot == 'pm' else slot}"

    @classmethod
    def detect_runtime_identity(cls, slot: str) -> tuple[str | None, Path | None]:
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
        command_name = Path(command.strip()).name.lower()
        if command_name in {"omp", "opencode"}:
            return "omp", Path(raw_path.strip())
        if command_name in {"claude", "claude-code"}:
            return "claude", Path(raw_path.strip())
        return None, None

    def _runtime_identity(self, slot: str) -> tuple[str | None, Path | None]:
        if self.runtime_source == "auto":
            configured_root = Path(
                os.environ.get("HEYDONNA_OMP_SESSIONS_ROOT", str(Path.home() / ".omp/sessions"))
            )
            if self.runtime_project_dir is None and self.omp_sessions_root != configured_root:
                return "omp", None
            return self.detect_runtime_identity(slot)
        if self.runtime_source in {"omp", "claude"}:
            return self.runtime_source, self.runtime_project_dir
        return None, None

    @staticmethod
    def clear_due_for(
        effective_start: datetime | None,
        *,
        occupied: bool | None,
        idle: bool | None,
        active: bool | None,
        session_id: str | None = None,
        session_started_at: datetime | None = None,
        is_pm: bool = False,
        now: datetime,
        threshold_hours: float = SESSION_AGE_THRESHOLD_HOURS,
    ) -> bool:
        if effective_start is None or active is not False:
            return False
        # Numbered-slot session expiry is actionable only for a free, idle
        # session.  Keep the observation contract identical to the one-shot
        # clear client; an occupied slot is never a clear candidate.
        if not is_pm and (
            occupied is not False
            or idle is not True
            or not session_id
            or session_started_at is None
        ):
            return False
        return (now.astimezone(timezone.utc) - effective_start).total_seconds() > threshold_hours * 3600

    def latest_omp_session(self, slot: str) -> tuple[Path, datetime, datetime, str] | None:
        session_dir = self.omp_sessions_root / ("heydonna-pm" if slot == "pm" else f"heydonna-slot{slot}")
        candidates: list[tuple[datetime, datetime, float, Path, str]] = []
        try:
            paths = list(session_dir.glob("*.jsonl"))
        except OSError:
            return None
        for path in paths:
            if not path.is_file():
                continue
            _records, start, latest, _session_id = _session_records(path)
            if start is None or latest is None or not _session_id:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            candidates.append((start, latest, mtime, path, _session_id))
        if not candidates:
            return None
        start, latest, _mtime, path, session_id = max(candidates, key=lambda item: (item[0], item[2]))
        return path, start, latest, session_id

    @staticmethod
    def _claude_project_name(project_dir: Path) -> str:
        return "-" + str(project_dir).strip("/").replace("/", "-")

    def latest_claude_session(
        self, project_dir: Path, *, now: datetime | None = None
    ) -> tuple[Path, datetime, datetime, str] | None:
        session_dir = self.claude_projects_root / self._claude_project_name(project_dir)
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        candidates: list[tuple[datetime, datetime | None, float, Path, str]] = []
        try:
            paths = list(session_dir.glob("*.jsonl"))
        except OSError:
            return None
        for path in paths:
            if not path.is_file():
                continue
            start, session_id = _session_identity(path)
            if start is None or not session_id:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            candidates.append((start, _session_activity_hint(path), mtime, path, session_id))
        if not candidates:
            return None
        active = [item for item in candidates if item[1] and now - item[1] <= ACTIVE_EVIDENCE_MAX_AGE]
        if active:
            newest_activity = max(item[1] for item in active if item[1])
            selected = [item for item in active if item[1] == newest_activity]
        else:
            newest_start = max(item[0] for item in candidates)
            selected = [item for item in candidates if item[0] == newest_start]
        if len(selected) != 1:
            return None
        start, _hint, _mtime, path, session_id = selected[0]
        _records, parsed_start, latest, parsed_id = _session_records(path)
        if parsed_start != start or parsed_id != session_id or latest is None:
            return None
        return path, start, latest, session_id

    @staticmethod
    def _successful_clear(slot: str, mop_events: Mapping[str, Any] | None) -> tuple[datetime, str] | None:
        candidates: list[tuple[datetime, str]] = []
        for source_type, payload in (mop_events or {}).items():
            for event in _events(payload):
                event_type = _text(event.get("event_type")) or _text(source_type)
                if event_type not in {"slot_cleared", "clear_pending_executed"}:
                    continue
                if _slot_key(event.get("slot") or event.get("slot_id")) != slot:
                    continue
                timestamp = parse_timestamp(event.get("timestamp"))
                if timestamp:
                    candidates.append((timestamp, event_type or "slot_cleared"))
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
        runtime_source, project_dir = self._runtime_identity(slot)
        session: tuple[Any, ...] | None = None
        if runtime_source == "omp" and slot in {"pm", "1", "2", "3", "4", "5", "6"}:
            session = self.latest_omp_session(slot)
        elif runtime_source == "claude" and project_dir is not None:
            session = self.latest_claude_session(project_dir, now=now)
        records: list[Mapping[str, Any]] = []
        session_start = latest_record = None
        session_id = None
        path: Path | None = None
        if session:
            path, session_start, latest_record = session[:3]
            records, session_start, latest_record, session_id = _session_records(path)
        clear = self._successful_clear(slot, mop_events)
        clear_at, clear_type = clear if clear else (None, None)
        runtime_session_id = session_id
        if slot == "pm":
            authoritative_session_id = runtime_session_id
            authoritative_session_started_at = session_start
            age_start = session_start
        else:
            # Transcript identity/timing is activity evidence only. Clear
            # requests must use the exact MoP identity recorded at the
            # UserPromptSubmit boundary; those timestamps are not aliases.
            authoritative_session_id = _text(row.get("session_id"))
            authoritative_session_started_at = parse_timestamp(row.get("session_started_at"))
            age_start = authoritative_session_started_at if authoritative_session_id else None
        effective = max((value for value in (age_start, clear_at) if value), default=None)
        activity = (
            _active_omp_evidence(records, session_start=session_start, now=now)
            if runtime_source == "omp"
            else _recent_activity(records, session_start=session_start, now=now)
        )
        state = _text(row.get("active_turn_state"))
        active_id = _text(row.get("active_turn_id"))
        state_active = state.lower() in {"active", "productive", "working", "busy"} if state else False
        active = bool(
            state_active
            or active_id
            or (activity if runtime_source == "omp" or self.session_age_activity else False)
        )
        occupied = row.get("occupied") if isinstance(row.get("occupied"), bool) else None
        if occupied is False and not active:
            active = False
        raw_idle = row.get("idle") if isinstance(row.get("idle"), bool) else None
        idle = (not active) if occupied is False else ((not active) if raw_idle is None else bool(raw_idle) and not active)
        checkout = row.get("checkout") if isinstance(row.get("checkout"), Mapping) else {}
        checkout_clean = row.get("checkout_clean", checkout.get("clean"))
        if not isinstance(checkout_clean, bool):
            checkout_clean = None
        checkout_head = _text(row.get("checkout_head") or checkout.get("head"))
        target_present = row.get("issue") is not None or row.get("pr") is not None
        head_valid = row.get("pr") is None or bool(_text(row.get("head_sha") or row.get("head")))
        handoff_ready = bool(
            session is not None and occupied is True and row.get("repository_id") is not None
            and isinstance(row.get("assignment_epoch"), int) and target_present and head_valid
            and _text(row.get("branch")) and _text(row.get("work_kind"))
            and _text(row.get("handoff_id")) and _text(row.get("claimed_at")) and not active
        )
        error = None if session else (
            "runtime identity unavailable" if runtime_source is None
            else ("no bound Claude session file found" if runtime_source == "claude" else "no top-level OMP session file found")
        )
        evidence_available = session is not None and runtime_source is not None
        return RuntimeObservation(
            slot=slot,
            source=f"{runtime_source}_top_level" if runtime_source else "unknown",
            session_path=str(path) if path else None,
            session_id=authoritative_session_id,
            runtime_session_id=runtime_session_id,
            session_start=session_start,
            session_started_at=authoritative_session_started_at,
            latest_record=latest_record,
            effective_start=effective,
            occupied=occupied,
            idle=idle if evidence_available else None,
            status=_text(row.get("status") or row.get("state")),
            dnd=row.get("dnd") if isinstance(row.get("dnd"), bool) else None,
            active_turn_id=active_id,
            active_turn_state=state,
            active=active if evidence_available else None,
            issue=row.get("issue"),
            pr=row.get("pr"),
            branch=_text(row.get("branch")),
            head_sha=_text(row.get("head_sha") or row.get("head")),
            assignment_epoch=row.get("assignment_epoch") if isinstance(row.get("assignment_epoch"), int) else None,
            repository_id=row.get("repository_id"),
            work_kind=_text(row.get("work_kind")),
            handoff_id=_text(row.get("handoff_id")),
            claimed_at=_text(row.get("claimed_at")),
            checkout_clean=checkout_clean,
            checkout_head=checkout_head,
            clear_event_type=clear_type,
            clear_event_at=clear_at,
            handoff_ready=handoff_ready,
            clear_due=self.clear_due_for(
                effective,
                occupied=occupied,
                idle=idle,
                active=active,
                session_id=authoritative_session_id,
                session_started_at=authoritative_session_started_at,
                is_pm=slot == "pm",
                now=now,
            ),
            error=error,
        )
