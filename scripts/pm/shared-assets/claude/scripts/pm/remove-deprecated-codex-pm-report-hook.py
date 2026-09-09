#!/usr/bin/env python3
"""Remove only the exact retired Codex-report hook registrations."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any


DEFAULT_SETTINGS = Path("/Users/rajiv/.claude/settings.json")
DEPRECATED_COMMAND = (
    "bash /Users/rajiv/.claude/hooks/pretooluse-reason-wrapper.sh "
    "/Users/rajiv/.claude/hooks/block-unverified-codex-pm-report.py"
)


def _result(status: str, reason: str, removed: int = 0) -> int:
    print(json.dumps({"status": status, "reason": reason, "removed": removed}))
    return 0 if status == "CHANGED" else 2


def _remove_exact(value: Any, removed: list[int]) -> Any:
    if isinstance(value, list):
        output = []
        for item in value:
            if isinstance(item, dict) and item.get("command") == DEPRECATED_COMMAND:
                removed[0] += 1
                continue
            output.append(_remove_exact(item, removed))
        return output
    if isinstance(value, dict):
        return {key: _remove_exact(item, removed) for key, item in value.items()}
    return value


def _same_metadata(path: Path, mode: int, uid: int, gid: int) -> bool:
    current = os.stat(path)
    return (
        stat.S_ISREG(current.st_mode)
        and stat.S_IMODE(current.st_mode) == mode
        and current.st_uid == uid
        and current.st_gid == gid
    )


def _atomic_replace(path: Path, payload: bytes, mode: int, uid: int, gid: int) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.retire.", dir=path.parent)
    temporary = Path(temporary_name)
    replaced = False
    try:
        os.fchmod(fd, mode)
        os.fchown(fd, uid, gid)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if not _same_metadata(temporary, mode, uid, gid):
            raise RuntimeError("temporary metadata mismatch")
        os.replace(temporary, path)
        replaced = True
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if not _same_metadata(path, mode, uid, gid):
            raise RuntimeError("post-replace metadata mismatch")
    except Exception:
        if not replaced:
            try:
                os.close(fd)
            except OSError:
                pass
        raise
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--expected-mode", type=lambda value: int(value, 8), default=None)
    parser.add_argument("--expected-uid", type=int, default=None)
    parser.add_argument("--expected-gid", type=int, default=None)
    parser.add_argument("--expected-removals", type=int, default=2)
    args = parser.parse_args()

    path = args.settings
    if not path.is_absolute():
        return _result("REFUSED", "settings_path_not_absolute")
    if not path.exists() or path.is_symlink() or not path.is_file():
        return _result("REFUSED", "settings_path_not_regular_file")

    with path.open("r+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        before = handle.read()
        before_stat = os.fstat(handle.fileno())
        before_sha = hashlib.sha256(before).hexdigest()
        if before_sha != args.expected_sha256:
            return _result("REFUSED", "settings_preimage_mismatch")
        if args.expected_mode is not None and stat.S_IMODE(before_stat.st_mode) != args.expected_mode:
            return _result("REFUSED", "settings_mode_mismatch")
        if args.expected_uid is not None and before_stat.st_uid != args.expected_uid:
            return _result("REFUSED", "settings_uid_mismatch")
        if args.expected_gid is not None and before_stat.st_gid != args.expected_gid:
            return _result("REFUSED", "settings_gid_mismatch")

        try:
            document = json.loads(before)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _result("REFUSED", "settings_json_invalid")
        removed = [0]
        updated = _remove_exact(document, removed)
        if removed[0] != args.expected_removals:
            return _result("REFUSED", "unexpected_exact_registration_count", removed[0])

        payload = (json.dumps(updated, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            _atomic_replace(
                path,
                payload,
                stat.S_IMODE(before_stat.st_mode),
                before_stat.st_uid,
                before_stat.st_gid,
            )
        except (OSError, RuntimeError):
            return _result("REFUSED", "settings_atomic_replace_failed", removed[0])
        return _result("CHANGED", "exact_registrations_removed", removed[0])


if __name__ == "__main__":
    raise SystemExit(main())
