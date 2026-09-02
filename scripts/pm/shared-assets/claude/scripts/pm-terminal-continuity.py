#!/usr/bin/env python3
"""Executable PM terminal producer/parser and durable CTO continuity guard."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
import subprocess
from pathlib import Path
from typing import Any, Mapping


TERMINAL_TYPES = {
    "FAILED_RUN_INVESTIGATION",
    "NUMBERED_PROOF",
    "REWORK_REVIEW_CANDIDATE",
    "CAPTURE_TERMINAL",
    "ASSIGNMENT_TERMINAL",
    "TYPED_BLOCKER",
}
HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_TEXT = 2000
DEFAULT_WAKE_EFFECT = Path(__file__).with_name("pm-terminal-wake.py")


def _state_path() -> Path:
    return Path(os.environ.get("PM_TERMINAL_CONTINUITY_STATE", "~/.claude/state/pm-terminal-continuity.json")).expanduser()


def _key(envelope: Mapping[str, Any]) -> str:
    return "{}:{}:{}:{}".format(envelope["terminal_type"], envelope["pr"], envelope["head"], envelope["source_receipt"])


def _digest(envelope: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_envelope(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("envelope_not_object")
    required = ("terminal_type", "pr", "head", "owner", "evidence_summary", "next_action", "next_owner", "wake", "source_receipt")
    if any(not isinstance(value.get(name), str) or not value.get(name).strip() for name in required if name != "pr"):
        raise ValueError("missing_terminal_field")
    if value.get("terminal_type") not in TERMINAL_TYPES:
        raise ValueError("invalid_terminal_type")
    if not isinstance(value.get("pr"), int) or isinstance(value.get("pr"), bool) or value["pr"] <= 0:
        raise ValueError("invalid_pr")
    if not HEAD_RE.fullmatch(value["head"]):
        raise ValueError("invalid_head")
    if len(value["evidence_summary"]) > MAX_TEXT or len(value["next_action"]) > MAX_TEXT:
        raise ValueError("bounded_field_exceeded")
    run = value.get("run_or_capture")
    if run is not None and (not isinstance(run, str) or len(run) > 200):
        raise ValueError("invalid_run_or_capture")
    return {
        "terminal_type": value["terminal_type"],
        "pr": value["pr"],
        "head": value["head"],
        "run_or_capture": run,
        "owner": value["owner"],
        "evidence_summary": value["evidence_summary"],
        "next_action": value["next_action"],
        "next_owner": value["next_owner"],
        "wake": value["wake"],
        "source_receipt": value["source_receipt"],
    }


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema": 1, "records": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("state_malformed") from exc
    if not isinstance(value, Mapping) or value.get("schema") != 1 or not isinstance(value.get("records"), dict):
        raise ValueError("state_malformed")
    return {"schema": 1, "records": dict(value["records"])}


def _save(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class ContinuityStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock_path = path.with_suffix(path.suffix + ".lock")

    def __enter__(self) -> "ContinuityStore":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = self.lock_path.open("a+")
        fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX)
        self.state = _load(self.path)
        return self

    def __exit__(self, *_: Any) -> None:
        fcntl.flock(self.lock.fileno(), fcntl.LOCK_UN)
        self.lock.close()

    def save(self) -> None:
        _save(self.path, self.state)


def emit(envelope: Mapping[str, Any], *, response_lost: bool = False) -> dict[str, Any]:
    value = validate_envelope(envelope)
    key = _key(value)
    digest = _digest(value)
    with ContinuityStore(_state_path()) as store:
        existing = store.state["records"].get(key)
        if existing is not None:
            if existing.get("envelope_digest") != digest:
                raise ValueError("exact_key_payload_conflict")
            return {"status": "DUPLICATE_SUPPRESSED", "key": key, "wake": False}
        store.state["records"][key] = {
            "envelope_digest": digest,
            "status": "ambiguous" if response_lost else "reserved",
            "repair_count": 0,
            "consumption_receipt": None,
            "next_edge_receipt": None,
            "delivery_receipt": None,
            "delivery_generation": None,
            "delivery_started_at": None,
            "reservation_uncertain": response_lost,
        }
        store.save()
    return {"status": "UNCERTAIN" if response_lost else "EMITTED", "key": key, "wake": not response_lost}


def _start_delivery(envelope: Mapping[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """Reserve the real monitor effect before invoking its wake transport.

    The effect-start write is the crash fence: once it is durable, a later
    invocation can never replay the wake, even if the transport accepted it
    and the process died before receiving a response.
    """
    value = validate_envelope(envelope)
    key = _key(value)
    with ContinuityStore(_state_path()) as store:
        record = store.state["records"].get(key)
        if record is None or record.get("envelope_digest") != _digest(value):
            raise ValueError("terminal_not_reserved")
        status = record.get("status")
        if record.get("reservation_uncertain"):
            return "ambiguous", dict(record)
        if status in {"effect-start", "ambiguous", "delivered", "consumed", "edge_bound", "repaired"}:
            return status, dict(record)
        if status not in {"reserved", "emitted"}:
            raise ValueError("delivery_phase_invalid")
        generation = hashlib.sha256(
            f"{key}:{os.getpid()}:{os.times().elapsed}".encode()
        ).hexdigest()
        record["status"] = "effect-start"
        record["delivery_generation"] = generation
        record["delivery_started_at"] = os.times().elapsed
        store.save()
        return "started", dict(record)


def _mark_delivery(envelope: Mapping[str, Any], *, status: str, receipt: str | None = None) -> None:
    value = validate_envelope(envelope)
    key = _key(value)
    with ContinuityStore(_state_path()) as store:
        record = store.state["records"].get(key)
        if record is None or record.get("envelope_digest") != _digest(value):
            raise ValueError("terminal_not_reserved")
        if record.get("status") not in {"effect-start", "ambiguous"}:
            return
        record["status"] = status
        if receipt is not None:
            record["delivery_receipt"] = receipt
        store.save()


def deliver(
    envelope: Mapping[str, Any],
    *,
    effect_command: str | None = None,
    timeout_seconds: float = 30.0,
    crash_before_send: bool = False,
) -> dict[str, Any]:
    """Run the monitor's one wake effect behind durable start/ambiguity fences.

    ``effect_command`` is the existing monitor delivery adapter (argv is never
    shell-expanded); it receives the validated envelope on stdin and must emit
    exactly ``{"receipt": "..."}``. Any transport, timeout, malformed response,
    or response-loss outcome becomes permanently ambiguous and is not replayed.
    """
    value = validate_envelope(envelope)
    key = _key(value)
    status, record = _start_delivery(value)
    if status == "started":
        if crash_before_send:
            os._exit(86)
        command = (
            effect_command
            or os.environ.get("PM_CTO_WAKE_EFFECT_COMMAND")
            or str(DEFAULT_WAKE_EFFECT)
        )
        if not command:
            _mark_delivery(value, status="ambiguous")
            return {"status": "AMBIGUOUS_SUPPRESSED", "key": key, "reason": "effect_command_missing"}
        try:
            completed = subprocess.run(
                [command],
                input=json.dumps(value, sort_keys=True, separators=(",", ":")),
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
            if completed.returncode != 0:
                raise ValueError("effect_nonzero")
            response = json.loads(completed.stdout)
            receipt = response.get("receipt") if isinstance(response, Mapping) else None
            if not isinstance(receipt, str) or not receipt.strip() or len(receipt) > MAX_TEXT:
                raise ValueError("effect_receipt_invalid")
        except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
            _mark_delivery(value, status="ambiguous")
            return {"status": "AMBIGUOUS_SUPPRESSED", "key": key, "reason": str(exc)}
        _mark_delivery(value, status="delivered", receipt=receipt)
        return {"status": "DELIVERED", "key": key, "wake": True, "receipt": receipt}
    if status == "delivered":
        return {"status": "DELIVERED_REPLAY_SUPPRESSED", "key": key, "wake": False}
    if status in {"effect-start", "ambiguous"}:
        return {"status": "AMBIGUOUS_SUPPRESSED", "key": key, "wake": False}
    return {"status": "DELIVERY_ALREADY_COMMITTED", "key": key, "wake": False}


def transition(envelope: Mapping[str, Any], kind: str, receipt: str) -> dict[str, Any]:
    value = validate_envelope(envelope)
    if not isinstance(receipt, str) or not receipt or len(receipt) > 200:
        raise ValueError("invalid_receipt")
    key = _key(value)
    with ContinuityStore(_state_path()) as store:
        record = store.state["records"].get(key)
        if record is None or record.get("envelope_digest") != _digest(value):
            raise ValueError("terminal_not_reserved")
        if kind == "consume":
            if record.get("status") != "delivered" or not record.get("delivery_receipt"):
                raise ValueError("delivery_not_confirmed")
            if record.get("consumption_receipt") not in (None, receipt):
                raise ValueError("consumption_conflict")
            record["consumption_receipt"] = receipt
            record["status"] = "consumed"
        elif kind == "edge":
            if record.get("status") != "consumed" or not record.get("delivery_receipt"):
                raise ValueError("delivery_not_confirmed")
            if not record.get("consumption_receipt"):
                raise ValueError("consumption_missing")
            if record.get("next_edge_receipt") not in (None, receipt):
                raise ValueError("next_edge_conflict")
            record["next_edge_receipt"] = receipt
            record["status"] = "edge_bound"
        else:
            raise ValueError("invalid_transition")
        store.save()
    return {"status": "BOUND", "key": key, "kind": kind}


def hourly_repair(envelope: Mapping[str, Any]) -> dict[str, Any]:
    value = validate_envelope(envelope)
    key = _key(value)
    with ContinuityStore(_state_path()) as store:
        record = store.state["records"].get(key)
        if record is None or record.get("envelope_digest") != _digest(value):
            raise ValueError("terminal_not_emitted")
        if record.get("next_edge_receipt"):
            return {"status": "CONTINUITY_PRESENT", "key": key, "wake": False}
        # A lost response is an uncertain delivery, not an emitted terminal
        # eligible for replay.  Keep it fail-closed until a human/CTO receipt
        # reconciles the exact key; never turn uncertainty into a second wake.
        if record.get("reservation_uncertain") or record.get("status") in {"effect-start", "ambiguous"}:
            return {"status": "UNCERTAIN_SUPPRESSED", "key": key, "wake": False}
        if record.get("repair_count", 0) >= 1:
            return {"status": "REPAIR_ALREADY_USED", "key": key, "wake": False}
        record["repair_count"] = 1
        record["status"] = "repair-due"
        store.save()
    return {"status": "HOURLY_REPAIR", "key": key, "wake": True, "repair_kind": "missing_consumption_or_edge"}


def _read_stdin() -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError("malformed_envelope") from exc
    return validate_envelope(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("complete", "emit", "route", "deliver", "consume", "edge", "hourly-repair"))
    parser.add_argument("--receipt")
    parser.add_argument("--response-lost", action="store_true")
    parser.add_argument("--effect-command")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--crash-before-send", action="store_true")
    args = parser.parse_args(argv)
    try:
        envelope = _read_stdin()
        if args.command in {"complete", "emit", "route"}:
            result = emit(envelope, response_lost=args.response_lost)
            if args.command == "complete" and result.get("status") == "EMITTED":
                result["status"] = "RESERVED"
            if args.command == "route" and result.get("status") == "EMITTED":
                result["route"] = "CTO_DECISIONS"
        elif args.command == "deliver":
            result = deliver(
                envelope,
                effect_command=args.effect_command,
                timeout_seconds=args.timeout_seconds,
                crash_before_send=args.crash_before_send,
            )
        elif args.command in {"consume", "edge"}:
            if not args.receipt:
                raise ValueError("receipt_required")
            result = transition(envelope, args.command, args.receipt)
        else:
            result = hourly_repair(envelope)
    except ValueError as exc:
        print(json.dumps({"status": "REFUSED", "error_class": str(exc)}, separators=(",", ":")))
        return 2
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
