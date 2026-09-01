"""Read-only Claude/OMP and MoP observation for the retained heartbeat."""

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
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _session_records(path: Path) -> tuple[list[Mapping[str, Any]], datetime | None, datetime | None, str | None]:
    records: list[Mapping[str, Any]] = []
    first = start = latest = None
    session_id = None
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
                if timestamp and value.get("type") == "session":
                    start, session_id = timestamp, _text(value.get("sessionId") or value.get("id"))
                elif session_id is None:
                    session_id = _text(value.get("sessionId") or value.get("id"))
                if timestamp and (latest is None or timestamp > latest):
                    latest = timestamp
    except OSError:
        return [], None, None, None
    return records, start or first, latest or start or first, session_id


def _active_omp_evidence(records: list[Mapping[str, Any]], *, session_start: datetime | None, now: datetime) -> bool:
    outstanding: dict[str, datetime] = {}
    for record in records:
        timestamp = parse_timestamp(record.get("timestamp"))
        if record.get("type") == "custom":
            data = record.get("data") if isinstance(record.get("data"), Mapping) else {}
            call_id = _text(data.get("toolCallId") or data.get("callId"))
            custom_type = _text(record.get("customType")) or ""
            if call_id and custom_type.endswith("tool_execution_start") and timestamp:
                outstanding[call_id] = timestamp
            elif call_id and custom_type.endswith(("tool_execution_end", "tool_execution_result")):
                outstanding.pop(call_id, None)
        elif record.get("type") == "message" and isinstance(record.get("message"), Mapping):
            message = record["message"]
            if message.get("role") == "assistant" and isinstance(message.get("content"), list):
                for item in message["content"]:
                    if isinstance(item, Mapping) and item.get("type") == "toolCall" and timestamp:
                        call_id = _text(item.get("id") or item.get("toolCallId"))
                        if call_id:
                            outstanding[call_id] = timestamp
            elif message.get("role") == "toolResult":
                outstanding.pop(_text(message.get("toolCallId")) or "", None)
    return any(timestamp >= (session_start or timestamp) and now - timestamp <= ACTIVE_EVIDENCE_MAX_AGE for timestamp in outstanding.values())


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
    error: str | None = None


class RuntimeObservationAdapter:
    """Observe bound runtime/MoP facts; never clear, assign, or send."""

    def __init__(self, *, omp_sessions_root: Path | None = None, claude_projects_root: Path | None = None,
                 runtime_source: str | None = "auto", runtime_project_dir: Path | None = None,
                 session_age_activity: bool = False) -> None:
        self.omp_sessions_root = omp_sessions_root or Path(os.environ.get("HEYDONNA_OMP_SESSIONS_ROOT", str(Path.home() / ".omp/sessions")))
        self.claude_projects_root = claude_projects_root or Path(os.environ.get("HEYDONNA_CLAUDE_PROJECTS_ROOT", str(Path.home() / ".claude/projects")))
        self.runtime_source = runtime_source
        self.runtime_project_dir = runtime_project_dir
        self.session_age_activity = session_age_activity

    @classmethod
    def detect_runtime_identity(cls, slot: str) -> tuple[str | None, Path | None]:
        try:
            result = subprocess.run(["tmux", "display-message", "-t", f"0:0.{0 if slot == 'pm' else slot}", "-p", "#{pane_current_command}\t#{pane_current_path}"], check=False, capture_output=True, text=True, timeout=2)
        except (OSError, subprocess.SubprocessError):
            return None, None
        if result.returncode != 0:
            return None, None
        command, separator, path = result.stdout.strip().partition("\t")
        if not separator or not path:
            return None, None
        name = Path(command).name.lower()
        return ("omp" if name in {"omp", "opencode"} else "claude" if name in {"claude", "claude-code"} else None), Path(path) if name in {"omp", "opencode", "claude", "claude-code"} else None

    def _runtime_identity(self, slot: str) -> tuple[str | None, Path | None]:
        if self.runtime_source == "auto":
            configured = Path(os.environ.get("HEYDONNA_OMP_SESSIONS_ROOT", str(Path.home() / ".omp/sessions")))
            return ("omp", None) if self.runtime_project_dir is None and self.omp_sessions_root != configured else self.detect_runtime_identity(slot)
        return (self.runtime_source, self.runtime_project_dir) if self.runtime_source in {"omp", "claude"} else (None, None)

    @staticmethod
    def clear_due_for(effective_start: datetime | None, *, occupied: bool | None, idle: bool | None,
                      active: bool | None, dnd: bool | None, is_pm: bool = False, now: datetime,
                      threshold_hours: float = SESSION_AGE_THRESHOLD_HOURS) -> bool:
        if effective_start is None or active is not False or is_pm:
            return False
        if occupied is not False or idle is not True or dnd is not False:
            return False
        return (now.astimezone(timezone.utc) - effective_start).total_seconds() > threshold_hours * 3600

    def latest_omp_session(self, slot: str) -> tuple[Path, datetime, datetime] | None:
        directory = self.omp_sessions_root / ("heydonna-pm" if slot == "pm" else f"heydonna-slot{slot}")
        candidates: list[tuple[datetime, datetime, float, Path]] = []
        try:
            paths = list(directory.glob("*.jsonl"))
        except OSError:
            return None
        for path in paths:
            if not path.is_file():
                continue
            _records, start, latest, _session_id = _session_records(path)
            if start is not None and latest is not None:
                try:
                    candidates.append((start, latest, path.stat().st_mtime, path))
                except OSError:
                    pass
        if not candidates:
            return None
        start, latest, _mtime, path = max(candidates, key=lambda row: (row[0], row[2]))
        return path, start, latest

    @staticmethod
    def _claude_project_name(project_dir: Path) -> str:
        return "-" + str(project_dir).strip("/").replace("/", "-")

    def latest_claude_session(self, project_dir: Path, *, now: datetime | None = None) -> tuple[Path, datetime, datetime, str] | None:
        directory = self.claude_projects_root / self._claude_project_name(project_dir)
        candidates: list[tuple[datetime, datetime, float, Path, str]] = []
        try:
            paths = list(directory.glob("*.jsonl"))
        except OSError:
            return None
        for path in paths:
            if not path.is_file():
                continue
            _records, start, latest, session_id = _session_records(path)
            if start is not None and latest is not None and session_id:
                try:
                    candidates.append((start, latest, path.stat().st_mtime, path, session_id))
                except OSError:
                    pass
        if not candidates:
            return None
        now = now or datetime.now(timezone.utc)
        active = [row for row in candidates if now - row[1] <= ACTIVE_EVIDENCE_MAX_AGE]
        selected = active if active else [row for row in candidates if row[0] == max(item[0] for item in candidates)]
        if len(selected) != 1:
            return None
        start, _latest, _mtime, path, session_id = selected[0]
        return path, start, _latest, session_id

    @staticmethod
    def _successful_clear(slot: str, mop_events: Mapping[str, Any] | None) -> tuple[datetime, str] | None:
        candidates = []
        for source, payload in (mop_events or {}).items():
            for event in (payload.get("events", []) if isinstance(payload, Mapping) else []):
                if not isinstance(event, Mapping) or str(event.get("slot") or event.get("slot_id") or "").strip() != slot:
                    continue
                event_type = _text(event.get("event_type")) or source
                timestamp = parse_timestamp(event.get("timestamp"))
                if event_type in {"slot_cleared", "clear_pending_executed"} and timestamp:
                    candidates.append((timestamp, event_type))
        return max(candidates, default=None, key=lambda item: item[0])

    def observe_slot(self, slot: str, *, mop_row: Mapping[str, Any] | None, mop_events: Mapping[str, Any] | None = None,
                     now: datetime | None = None) -> RuntimeObservation:
        slot = str(slot).strip()
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        row = dict(mop_row or {})
        runtime_source, project_dir = self._runtime_identity(slot)
        session = self.latest_omp_session(slot) if runtime_source == "omp" else self.latest_claude_session(project_dir, now=now) if runtime_source == "claude" and project_dir else None
        records: list[Mapping[str, Any]] = []
        start = latest = session_id = path = None
        if session:
            path, start, latest = session[:3]
            records, start, latest, session_id = _session_records(path)
        clear = self._successful_clear(slot, mop_events)
        clear_at, clear_type = clear if clear else (None, None)
        effective = max((value for value in (start, clear_at) if value), default=None)
        active = bool(_active_omp_evidence(records, session_start=start, now=now) if runtime_source == "omp" else False)
        active_id = _text(row.get("active_turn_id"))
        state = _text(row.get("active_turn_state"))
        active = active or bool(active_id) or (state is not None and state.lower() in {"active", "productive", "working", "busy"})
        occupied = row.get("occupied") if isinstance(row.get("occupied"), bool) else None
        if occupied is False and not active:
            active = False
        raw_idle = row.get("idle") if isinstance(row.get("idle"), bool) else None
        idle = (not active) if occupied is False else ((not active) if raw_idle is None else bool(raw_idle) and not active)
        checkout = row.get("checkout") if isinstance(row.get("checkout"), Mapping) else {}
        checkout_clean = row.get("checkout_clean", checkout.get("clean"))
        checkout_clean = checkout_clean if isinstance(checkout_clean, bool) else None
        handoff_ready = bool(session and occupied is True and row.get("repository_id") is not None and isinstance(row.get("assignment_epoch"), int)
                             and (row.get("issue") is not None or row.get("pr") is not None) and _text(row.get("branch"))
                             and _text(row.get("head_sha") or row.get("head")) and _text(row.get("work_kind")) and _text(row.get("handoff_id"))
                             and _text(row.get("claimed_at")) and not active)
        return RuntimeObservation(
            slot=slot, source=f"{runtime_source}_top_level" if runtime_source else "unknown", session_path=str(path) if path else None,
            session_id=session_id, session_start=start, latest_record=latest, effective_start=effective, occupied=occupied,
            idle=idle if session or runtime_source else None, status=_text(row.get("status") or row.get("state")),
            dnd=row.get("dnd") if isinstance(row.get("dnd"), bool) else None, active_turn_id=active_id,
            active_turn_state=state, active=active if session or runtime_source else None, issue=row.get("issue"), pr=row.get("pr"),
            branch=_text(row.get("branch")), head_sha=_text(row.get("head_sha") or row.get("head")),
            assignment_epoch=row.get("assignment_epoch") if isinstance(row.get("assignment_epoch"), int) else None,
            repository_id=row.get("repository_id"), work_kind=_text(row.get("work_kind")), handoff_id=_text(row.get("handoff_id")),
            claimed_at=_text(row.get("claimed_at")), checkout_clean=checkout_clean,
            checkout_head=_text(row.get("checkout_head") or checkout.get("head")), clear_event_type=clear_type,
            clear_event_at=clear_at, handoff_ready=handoff_ready,
            clear_due=self.clear_due_for(effective, occupied=occupied, idle=idle, active=active, dnd=row.get("dnd") if isinstance(row.get("dnd"), bool) else None,
                                         is_pm=slot == "pm", now=now),
            error=None if session else ("runtime identity unavailable" if runtime_source is None else "no bound session file found"),
        )
