#!/usr/bin/env python3
"""Run one bounded group of PM Stop validators and emit one hook result."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any


DEFAULT_HOOK_ROOT = Path("/Users/rajiv/Downloads/projects/heydonna-app/.claude/hooks")
DEFAULT_LOG = Path("/tmp/pm-stop-group-runner.log")
DEBUG_LOG = Path("/tmp/pm-stop-group-runner.debug.log")
DEFAULT_MAX_TURN_SECONDS = 3600
DEFAULT_FINDING_COOLDOWN_SECONDS = 300
DEFAULT_FINDING_COOLDOWN_STATE = Path("/tmp/pm-stop-finding-cooldown.json")
NON_COOLDOWN_FINDING_PREFIXES = (
    "[PM_OBLIGATION_DRAIN]",
    "[PM_STOP_CHILD_TIMEOUT]",
    "[PM_STOP_CHILD_FAILED]",
    "[PM_STOP_GROUP_INVALID]",
)


@dataclass(frozen=True)
class Child:
    name: str
    argv: tuple[str, ...]
    timeout: float


@dataclass(frozen=True)
class Result:
    name: str
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def wrapped(root: Path, name: str, script: str, timeout: float) -> Child:
    return Child(
        name=name,
        argv=(
            "bash",
            str(root / "stop-hook-runner.sh"),
            name,
            "bash",
            str(root / script),
        ),
        timeout=timeout,
    )


def direct(root: Path, name: str, script: str, timeout: float) -> Child:
    return Child(
        name=name,
        argv=("bash", str(root / script)),
        timeout=timeout,
    )


def default_groups(root: Path) -> dict[str, tuple[Child, ...]]:
    return {
        "obligations": (
            direct(
                root,
                "pm-ops-sync-stop-validator",
                "pm-ops-sync-stop-validator.sh",
                2,
            ),
            direct(
                root,
                "ops-audit-stop-validator",
                "ops-audit-stop-validator.sh",
                5,
            ),
            # Rajiv 2026-10-24 DM D0AMF0XE6TS ts 1792823741.932929: block until Slack threads get PM replies.
            direct(
                root,
                "slack-reply-due",
                "slack-reply-due.sh",
                9,  # Leave wrapper overhead inside the registered 10s Stop budget.
            ),
            # Rajiv 2026-10-28 DM D0AMF0XE6TS ts 1793208036.358899: block while a slot is idle >=10 min.
            direct(
                root,
                "idle-slot-due",
                "idle-slot-due.sh",
                6,
            ),
        ),
    }


def load_groups(root: Path) -> dict[str, tuple[Child, ...]]:
    override = os.environ.get("PM_STOP_GROUP_COMMANDS_JSON")
    if not override:
        return default_groups(root)
    raw = json.loads(override)
    if not isinstance(raw, dict):
        raise ValueError("PM_STOP_GROUP_COMMANDS_JSON must be an object")
    groups: dict[str, tuple[Child, ...]] = {}
    for group, entries in raw.items():
        if not isinstance(group, str) or not isinstance(entries, list):
            raise ValueError("invalid test command group")
        children: list[Child] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("invalid test command entry")
            argv = entry.get("argv")
            if not isinstance(argv, list) or not all(isinstance(value, str) for value in argv):
                raise ValueError("test command argv must be a string array")
            children.append(
                Child(
                    name=str(entry["name"]),
                    argv=tuple(argv),
                    timeout=float(entry.get("timeout", 5)),
                )
            )
        groups[group] = tuple(children)
    return groups


def run_child(child: Child, payload: str) -> Result:
    try:
        completed = subprocess.run(
            child.argv,
            input=payload,
            text=True,
            capture_output=True,
            timeout=child.timeout,
            check=False,
        )
        return Result(
            name=child.name,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    except subprocess.TimeoutExpired as error:
        return Result(
            name=child.name,
            returncode=124,
            stdout=(error.stdout or "") if isinstance(error.stdout, str) else "",
            stderr=(error.stderr or "") if isinstance(error.stderr, str) else "",
            timed_out=True,
        )
    except OSError as error:
        return Result(child.name, 127, "", f"{type(error).__name__}: {error}")


def output_reasons(output: str) -> tuple[list[str], list[str]]:
    blocking: list[str] = []
    advisory: list[str] = []
    for line in output.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            value: Any = json.loads(text)
        except json.JSONDecodeError:
            advisory.append(text)
            continue
        if not isinstance(value, dict):
            advisory.append(text)
            continue
        reason = str(value.get("message") or value.get("reason") or "").strip()
        if not reason:
            continue
        if value.get("decision") == "block" or value.get("continue") is False:
            blocking.append(reason)
        else:
            advisory.append(reason)
    return blocking, advisory


def unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def finding_cooldown_seconds() -> int:
    try:
        value = int(os.environ.get("PM_STOP_FINDING_COOLDOWN_SECONDS") or str(DEFAULT_FINDING_COOLDOWN_SECONDS))
    except ValueError:
        return DEFAULT_FINDING_COOLDOWN_SECONDS
    return max(0, value)


def canonical_finding(reason: str) -> str:
    """Remove volatile clock values without weakening target/head identity."""

    value = re.sub(
        r"\b20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b",
        "<timestamp>",
        reason,
    )
    value = re.sub(
        r"\b(?:(?:[A-Za-z_][A-Za-z0-9_-]*_)?age_min|age|elapsed|wait(?:ed|ing)?_seconds)=\d+(?:\.\d+)?\b",
        lambda match: f"{match.group(0).split('=', 1)[0]}=<seconds>",
        value,
        flags=re.IGNORECASE,
    )
    return " ".join(value.split())


def finding_fingerprint(group: str, reason: str) -> str:
    payload = f"{group}\0{canonical_finding(reason)}".encode()
    return hashlib.sha256(payload).hexdigest()


def finding_cooldown_path() -> Path:
    return Path(
        os.environ.get(
            "PM_STOP_FINDING_COOLDOWN_STATE",
            str(DEFAULT_FINDING_COOLDOWN_STATE),
        )
    )


def load_cooldown_findings(path: Path) -> dict[str, float]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("unsupported cooldown state")
    findings = value.get("findings")
    if not isinstance(findings, dict):
        raise ValueError("cooldown findings must be an object")
    parsed: dict[str, float] = {}
    for key, surfaced_at in findings.items():
        if isinstance(key, str) and isinstance(surfaced_at, (int, float)):
            parsed_timestamp = float(surfaced_at)
            if not math.isfinite(parsed_timestamp):
                raise ValueError("cooldown timestamp must be finite")
            parsed[key] = parsed_timestamp
    return parsed


def write_cooldown_findings(path: Path, findings: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "findings": findings}, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def apply_finding_cooldown(
    group: str,
    session_id: object,
    blocking: list[str],
    *,
    now: float | None = None,
) -> tuple[list[str], list[str], str | None]:
    """Suppress identical findings briefly; infrastructure failures stay closed."""

    grace = finding_cooldown_seconds()
    if grace <= 0 or not isinstance(session_id, str) or not session_id:
        return blocking, [], None

    always_block = [reason for reason in blocking if reason.startswith(NON_COOLDOWN_FINDING_PREFIXES)]
    candidates = [reason for reason in blocking if reason not in always_block]
    if not candidates:
        return blocking, [], None

    timestamp = time.time() if now is None else now
    state_path = finding_cooldown_path()
    lock_path = Path(f"{state_path}.lock")
    surfaced: list[str] = []
    suppressed: list[str] = []

    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            findings = load_cooldown_findings(state_path)
            if any(surfaced_at > timestamp for surfaced_at in findings.values()):
                raise ValueError("cooldown timestamp cannot be in the future")
            retention = max(grace * 4, 3600)
            findings = {
                key: surfaced_at for key, surfaced_at in findings.items() if timestamp - surfaced_at <= retention
            }
            for reason in candidates:
                fingerprint = finding_fingerprint(group, reason)
                key = f"{session_id}:{fingerprint}"
                last_surface = findings.get(key)
                if last_surface is not None and timestamp - last_surface < grace:
                    suppressed.append(reason)
                    continue
                findings[key] = timestamp
                surfaced.append(reason)
            write_cooldown_findings(state_path, findings)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return blocking, [], f"{type(error).__name__}: {error}"

    return always_block + surfaced, suppressed, None


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def real_user_turn_started_at(payload: dict[str, Any]) -> datetime | None:
    """Return the latest real prompt, excluding tool and compaction records."""

    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return None

    latest: datetime | None = None
    try:
        with Path(transcript_path).open(encoding="utf-8", errors="replace") as transcript:
            for raw in transcript:
                try:
                    entry = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "user":
                    continue
                if (
                    entry.get("isMeta")
                    or entry.get("isCompactSummary")
                    or entry.get("isVisibleInTranscriptOnly")
                    or entry.get("sourceToolAssistantUUID")
                    or entry.get("sourceToolUseID")
                    or entry.get("attachment")
                ):
                    continue
                message = entry.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, str) or not content.strip():
                    continue
                if content.lstrip().startswith("<command-name>"):
                    continue
                origin = entry.get("origin")
                origin_kind = origin.get("kind") if isinstance(origin, dict) else None
                prompt_source = entry.get("promptSource")
                if origin_kind not in (None, "human"):
                    continue
                if prompt_source not in (None, "typed", "pasted"):
                    continue
                timestamp = parse_timestamp(entry.get("timestamp"))
                if timestamp is not None:
                    latest = timestamp
    except OSError:
        return None
    return latest


def max_turn_seconds() -> int:
    try:
        value = int(os.environ.get("PM_STOP_MAX_TURN_SECONDS") or str(DEFAULT_MAX_TURN_SECONDS))
    except ValueError:
        return DEFAULT_MAX_TURN_SECONDS
    return max(0, value)


def long_turn_bypass(payload: dict[str, Any]) -> tuple[bool, float, datetime | None]:
    threshold = max_turn_seconds()
    started_at = real_user_turn_started_at(payload)
    if threshold <= 0 or started_at is None:
        return False, 0.0, started_at
    age_seconds = max(
        0.0,
        (datetime.now(timezone.utc) - started_at).total_seconds(),
    )
    return age_seconds > threshold, age_seconds, started_at


def append_log(group: str, results: list[Result]) -> None:
    path = Path(os.environ.get("PM_STOP_GROUP_LOG", str(DEFAULT_LOG)))
    record = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "group": group,
        "children": [
            {
                "name": result.name,
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "stdout_bytes": len(result.stdout.encode()),
                "stderr_bytes": len(result.stderr.encode()),
            }
            for result in results
        ],
    }
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        pass


def append_bypass_log(
    group: str,
    payload: dict[str, Any],
    age_seconds: float,
    started_at: datetime,
) -> None:
    path = Path(os.environ.get("PM_STOP_GROUP_LOG", str(DEFAULT_LOG)))
    record = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "group": group,
        "skipped": True,
        "reason": "PM_STOP_GROUP_SKIPPED_LONG_TURN",
        "session_id": payload.get("session_id"),
        "transcript_path": payload.get("transcript_path"),
        "turn_started_at": started_at.isoformat().replace("+00:00", "Z"),
        "turn_age_seconds": int(age_seconds),
        "threshold_seconds": max_turn_seconds(),
    }
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        pass


def append_cooldown_log(
    group: str,
    payload: dict[str, Any],
    surfaced: list[str],
    suppressed: list[str],
    error: str | None,
) -> None:
    path = Path(os.environ.get("PM_STOP_GROUP_LOG", str(DEFAULT_LOG)))
    record = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "group": group,
        "reason": "PM_STOP_FINDING_COOLDOWN",
        "session_id": payload.get("session_id"),
        "cooldown_seconds": finding_cooldown_seconds(),
        "surfaced_fingerprints": [finding_fingerprint(group, reason) for reason in surfaced],
        "suppressed_fingerprints": [finding_fingerprint(group, reason) for reason in suppressed],
        "state_error": error,
    }
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        pass


def debug_log(message: str) -> None:
    """Append one always-on debug line to a dedicated, non-cooldown log.

    Intentionally separate from the finding cooldown state so every invocation
    is observable, including early-return paths that never reach append_log().
    """
    path = Path(os.environ.get("PM_STOP_GROUP_DEBUG_LOG", str(DEBUG_LOG)))
    record = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "pid": os.getpid(),
        "msg": message,
    }
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "group",
        choices=("communications", "release", "capacity-state", "obligations"),
    )
    args = parser.parse_args()
    debug_log(f"invoked group={args.group} argv={sys.argv}")

    root = Path(os.environ.get("PM_STOP_HOOK_ROOT", str(DEFAULT_HOOK_ROOT)))
    groups = load_groups(root)
    children = groups.get(args.group)
    if children is None:
        debug_log(f"unknown group={args.group}")
        print(
            json.dumps(
                {
                    "decision": "block",
                    "message": f"[PM_STOP_GROUP_INVALID] unknown group={args.group}",
                }
            )
        )
        return 0

    payload = sys.stdin.read()
    try:
        hook_input = json.loads(payload or "{}")
    except (json.JSONDecodeError, TypeError):
        hook_input = {}
    if not isinstance(hook_input, dict):
        hook_input = {}
    debug_log(
        "payload session_id=%s transcript_path=%s stdin_bytes=%d"
        % (
            hook_input.get("session_id"),
            bool(hook_input.get("transcript_path")),
            len(payload.encode()) if isinstance(payload, str) else -1,
        )
    )

    bypass, age_seconds, started_at = long_turn_bypass(hook_input)
    if bypass and started_at is not None:
        debug_log(f"long_turn_bypass age_seconds={age_seconds:.0f}")
        append_bypass_log(args.group, hook_input, age_seconds, started_at)
        # Long turns still drain runnable commitments. Preserve the historical
        # bypass for unrelated validators; it cannot waive the drain decision.
        children = tuple(child for child in children
                         if child.name == "pm-ops-sync-stop-validator")
        if not children:
            return 0

    with ThreadPoolExecutor(max_workers=max(1, len(children))) as executor:
        results = list(executor.map(lambda child: run_child(child, payload), children))
    append_log(args.group, results)

    blocking: list[str] = []
    advisory: list[str] = []
    for result in results:
        if result.name == "pm-ops-sync-stop-validator" and (
            result.timed_out or result.returncode != 0
        ):
            # No trustworthy ledger decision: fail OPEN, including partial
            # stdout. Other validators retain their existing failure policy.
            advisory.append(
                "[PM_OBLIGATION_DRAIN_UNAVAILABLE] local obligation child "
                f"{'timed out' if result.timed_out else 'failed'} rc={result.returncode}; "
                "readiness/count UNKNOWN; no obligation block inferred."
            )
            continue
        child_blocking, child_advisory = output_reasons(result.stdout)
        blocking.extend(child_blocking)
        advisory.extend(child_advisory)
        if result.timed_out:
            blocking.append(f"[PM_STOP_CHILD_TIMEOUT] group={args.group} hook={result.name}")
        elif result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().replace("\n", " ")[:300]
            blocking.append(
                f"[PM_STOP_CHILD_FAILED] group={args.group} hook={result.name} "
                f"rc={result.returncode} detail={detail or 'none'}"
            )

    blocking = unique(blocking)
    advisory = unique(advisory)
    debug_log(f"validator results blocking={len(blocking)} advisory={len(advisory)}")
    blocking, suppressed, cooldown_error = apply_finding_cooldown(
        args.group,
        hook_input.get("session_id"),
        blocking,
    )
    debug_log(
        f"after cooldown blocking={len(blocking)} suppressed={len(suppressed)} error={cooldown_error}"
    )
    append_cooldown_log(
        args.group,
        hook_input,
        blocking,
        suppressed,
        cooldown_error,
    )
    if blocking:
        message = " | ".join(blocking + advisory)[:6000]
        print(json.dumps({"decision": "block", "message": message, "reason": message}))
    elif advisory:
        print(
            json.dumps(
                {
                    "continue": True,
                    "reason": " | ".join(advisory)[:6000],
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
