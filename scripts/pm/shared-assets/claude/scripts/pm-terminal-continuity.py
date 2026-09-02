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
            "status": "reserved" if response_lost else "emitted",
            "repair_count": 0,
            "consumption_receipt": None,
            "next_edge_receipt": None,
        }
        store.save()
    return {"status": "UNCERTAIN" if response_lost else "EMITTED", "key": key, "wake": not response_lost}


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
            if record.get("consumption_receipt") not in (None, receipt):
                raise ValueError("consumption_conflict")
            record["consumption_receipt"] = receipt
            record["status"] = "consumed"
        elif kind == "edge":
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
        if record.get("consumption_receipt") or record.get("next_edge_receipt"):
            return {"status": "CONTINUITY_PRESENT", "key": key, "wake": False}
        # A lost response is an uncertain delivery, not an emitted terminal
        # eligible for replay.  Keep it fail-closed until a human/CTO receipt
        # reconciles the exact key; never turn uncertainty into a second wake.
        if record.get("status") == "reserved":
            return {"status": "UNCERTAIN_SUPPRESSED", "key": key, "wake": False}
        if record.get("repair_count", 0) >= 1:
            return {"status": "REPAIR_ALREADY_USED", "key": key, "wake": False}
        record["repair_count"] = 1
        record["status"] = "repaired"
        store.save()
    return {"status": "HOURLY_REPAIR", "key": key, "wake": True}


def _read_stdin() -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError("malformed_envelope") from exc
    return validate_envelope(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("emit", "route", "consume", "edge", "hourly-repair"))
    parser.add_argument("--receipt")
    parser.add_argument("--response-lost", action="store_true")
    args = parser.parse_args(argv)
    try:
        envelope = _read_stdin()
        if args.command in {"emit", "route"}:
            result = emit(envelope, response_lost=args.response_lost)
            if args.command == "route" and result.get("status") == "EMITTED":
                result["route"] = "CTO_DECISIONS"
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
