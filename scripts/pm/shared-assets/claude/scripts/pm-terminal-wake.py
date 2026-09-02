#!/usr/bin/env python3
"""Manifest-bound PM terminal wake adapter for the CTO Decisions task."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys


CTO_DECISIONS_TASK = "01a03236-2e61-71f3-a6a8-3dc24d8c8917"
DEFAULT_STDIO_HELPER = Path(
    "/Users/rajiv/.codex/skills/codex-stdio-send-message/scripts/send_message.py"
)
MAX_TIMEOUT_SECONDS = 120.0


def _continuity_module():
    source = Path(__file__).with_name("pm-terminal-continuity.py")
    spec = importlib.util.spec_from_file_location("pm_terminal_continuity_for_wake", source)
    if spec is None or spec.loader is None:
        raise ValueError("continuity_source_missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_envelope() -> dict[str, object]:
    try:
        value = json.load(sys.stdin)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("malformed_envelope") from exc
    module = _continuity_module()
    return module.validate_envelope(value)


def _helper_path() -> Path:
    configured = os.environ.get("PM_CTO_STDIO_HELPER")
    path = Path(configured) if configured else DEFAULT_STDIO_HELPER
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError("stdio_helper_missing_or_not_executable")
    return path


def deliver(envelope: dict[str, object], timeout_seconds: float = 30.0) -> dict[str, str]:
    module = _continuity_module()
    key = module._key(envelope)
    helper = _helper_path()
    if timeout_seconds <= 0 or timeout_seconds > MAX_TIMEOUT_SECONDS:
        raise ValueError("invalid_timeout")
    message = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
    completed = subprocess.run(
        [
            sys.executable,
            str(helper),
            "--thread-id",
            CTO_DECISIONS_TASK,
            "--dedup-key",
            key,
            "--message",
            message,
        ],
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("stdio_handoff_not_accepted")
    try:
        receipt = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("stdio_handoff_receipt_malformed") from exc
    if not isinstance(receipt, dict):
        raise ValueError("stdio_handoff_receipt_malformed")
    status = receipt.get("status")
    queued_id = receipt.get("queuedSubmissionId")
    accepted = (
        status == "delivered" and receipt.get("startAccepted") is True
    ) or (
        status == "queued_for_task_consumption"
        and receipt.get("queueAccepted") is True
        and receipt.get("startAccepted") is False
    )
    if not accepted or not isinstance(queued_id, str) or not queued_id.strip():
        raise ValueError("stdio_handoff_receipt_not_authoritative")
    return {
        "receipt": queued_id,
        "status": str(status),
        "dedup_key": key,
        "thread_id": CTO_DECISIONS_TASK,
    }


def main() -> int:
    try:
        envelope = _read_envelope()
        result = deliver(envelope)
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        print(json.dumps({"status": "AMBIGUOUS", "error": str(exc)}, separators=(",", ":")))
        return 2
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
