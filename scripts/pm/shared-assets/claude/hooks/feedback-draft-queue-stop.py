#!/usr/bin/env python3
"""Nonblocking Stop/SubagentStop queue for HeyDonna /feedback drafts.

This hook is separate from the review-verdict path. It never inspects a
companion review, never prints a hook decision, and never submits a draft to
Anthropic. It only forwards a pointer (draft id, path, title, type) through
message-pm.sh, and only after that send is verified.

Drafts created before the hook is activated are left queued. A draft id that
was already verified is not sent again. The id is persisted as started/uncertain
before the transport runs and becomes delivered only after a verified receipt.
An uncertain effect is not sent again.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


HOOK_COMMAND = "/Users/rajiv/.claude/hooks/feedback-draft-queue-stop.py"
MESSAGE_PM_DEFAULT = "/Users/rajiv/.claude/scripts/message-pm.sh"
POINTER_HEADER = "HeyDonna feedback draft pointer"


def default_drafts_dir() -> Path:
    return Path.home() / ".claude" / "feedback" / "drafts"


def default_state_path() -> Path:
    return Path.home() / ".claude" / "feedback" / "queue-state.json"


def one_line(value: object) -> str:
    return " ".join(str(value if value is not None else "").replace("\r", " ").replace("\n", " ").split())


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


_SUPPORTED_CHECKOUT = re.compile(r"heydonna-app(?:-\d+)?\Z")


def project_dir_key_for(cwd: str) -> str:
    """Claude's project_dir_key is the absolute cwd with slashes turned into hyphens."""
    return cwd.replace("/", "-")


def normalize_draft_cwd(value: object) -> str | None:
    """Absolute lexical cwd. Relative, empty, or non-canonical paths are malformed."""
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if "\x00" in value or "\\" in value or not value.startswith("/"):
        return None
    if value.endswith("/") or "//" in value:
        return None
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts[1:]):
        return None
    return value


def _supported_checkout_name(path: str) -> bool:
    return _SUPPORTED_CHECKOUT.fullmatch(path.rsplit("/", 1)[-1]) is not None


def _is_linked_worktree_of_supported_checkout(cwd: str) -> bool:
    """True when cwd is a linked worktree whose common git dir is a supported checkout.

    Directory names such as heydonna-app-unrelated are not accepted merely because
    they contain the checkout name. A worktree of heydonna-app (or a numbered
    slot clone) is accepted even when its own path does not.
    """
    if not Path(cwd).is_dir():
        return False
    try:
        completed = subprocess.run(
            [
                "git",
                "--no-optional-locks",
                "-C",
                cwd,
                "rev-parse",
                "--path-format=absolute",
                "--show-toplevel",
                "--git-common-dir",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if completed.returncode != 0:
        return False
    lines = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
    if len(lines) != 2:
        return False
    toplevel, common = lines
    try:
        if Path(toplevel).resolve() != Path(cwd).resolve():
            return False
        common_path = Path(common).resolve()
    except OSError:
        return False
    if common_path.name != ".git":
        return False
    return _supported_checkout_name(str(common_path.parent))


def is_heydonna_source(draft: dict) -> bool:
    """Supported HeyDonna checkout or linked worktree, with consistent provenance.

    cwd and transcript_ref.project_dir_key must describe the same absolute
    checkout. Contradictory, lookalike, and malformed provenance stays unsent.
    """
    if not isinstance(draft, dict):
        return False
    cwd = normalize_draft_cwd(draft.get("cwd"))
    if cwd is None:
        return False
    ref = draft.get("transcript_ref")
    if not isinstance(ref, dict):
        return False
    key = ref.get("project_dir_key")
    if not isinstance(key, str) or key != project_dir_key_for(cwd):
        return False
    if _supported_checkout_name(cwd):
        return True
    return _is_linked_worktree_of_supported_checkout(cwd)


def build_pointer(draft: dict, path: Path) -> str | None:
    """Pointer envelope. Callers must not pass review body or verdict prose."""
    draft_id = one_line(draft.get("draft_id") or "")
    if not draft_id:
        return None
    lines = [
        POINTER_HEADER,
        f"draft_id: {draft_id}",
        f"path: {one_line(path)}",
        f"title: {one_line(draft.get('title') or '')}",
        f"type: {one_line(draft.get('type') or '')}",
    ]
    return "\n".join(lines) + "\n"


def load_drafts(directory: Path) -> list[tuple[Path, dict]]:
    if not directory.is_dir():
        return []
    found: list[tuple[Path, dict]] = []
    for path in sorted(directory.glob("*.json")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        draft_id = payload.get("draft_id") or path.stem
        if not isinstance(draft_id, str) or not draft_id.strip():
            continue
        draft = dict(payload)
        draft["draft_id"] = draft_id.strip()
        found.append((path, draft))
    return found


def _empty_state(now: datetime) -> dict:
    return {"activated_at": now.isoformat(), "delivered_ids": [], "uncertain_ids": []}


def _id_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _read_state(path: Path, now: datetime) -> tuple[dict, bool]:
    if not path.is_file():
        return _empty_state(now), True
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _empty_state(now), True
    if not isinstance(payload, dict) or parse_timestamp(payload.get("activated_at")) is None:
        return _empty_state(now), True
    delivered = payload.get("delivered_ids")
    if not isinstance(delivered, list):
        delivered = []
    clean = _id_list(delivered)
    raw_uncertain = payload.get("uncertain_ids", None)
    uncertain = _id_list(raw_uncertain)
    delivered_set = set(clean)
    uncertain = [item for item in uncertain if item not in delivered_set]
    changed = clean != delivered
    if isinstance(raw_uncertain, list):
        changed = changed or uncertain != raw_uncertain
    elif raw_uncertain is not None:
        changed = True
    return {
        "activated_at": payload["activated_at"],
        "delivered_ids": clean,
        "uncertain_ids": uncertain,
    }, changed


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(state, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _log(state_path: Path, line: str) -> None:
    try:
        log_path = state_path.with_name("queue.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line.rstrip() + "\n")
    except OSError:
        return


def send_verified(message_pm: str, envelope: str) -> bool:
    """True only when message-pm.sh reports a verified delivery."""
    if not message_pm or not Path(message_pm).is_file():
        return False
    directory = Path(os.environ.get("TMPDIR") or "/tmp")
    envelope_path = directory / f"feedback-draft-pointer-{os.getpid()}.txt"
    try:
        envelope_path.write_text(envelope, encoding="utf-8")
        completed = subprocess.run(
            [message_pm, "--file", str(envelope_path)],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    finally:
        try:
            envelope_path.unlink()
        except OSError:
            pass
    stdout = completed.stdout or ""
    return completed.returncode == 0 and "MESSAGE_PM_SENT" in stdout and "verified=true" in stdout


def process_queue(
    *,
    drafts_dir: Path | None = None,
    state_path: Path | None = None,
    message_pm: str | None = None,
    now: datetime | None = None,
    activate_only: bool = False,
) -> dict:
    drafts_dir = drafts_dir or Path(os.environ.get("FEEDBACK_DRAFT_DRAFTS_DIR") or default_drafts_dir())
    state_path = state_path or Path(os.environ.get("FEEDBACK_DRAFT_QUEUE_STATE") or default_state_path())
    message_pm = message_pm if message_pm is not None else os.environ.get("MESSAGE_PM_BIN", MESSAGE_PM_DEFAULT)
    now = now or utc_now()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    sent: list[str] = []
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state, changed = _read_state(state_path, now)
        if changed:
            _write_state(state_path, state)
        activated_at = parse_timestamp(state.get("activated_at"))
        if activate_only or activated_at is None:
            _log(state_path, f"activated_at={state['activated_at']} sent=0")
            return {"activated": True, "activated_at": state["activated_at"], "sent": [], "delivered_ids": list(state["delivered_ids"])}
        delivered = list(state["delivered_ids"])
        uncertain = list(state.get("uncertain_ids") or [])
        seen = set(delivered)
        uncertain_seen = set(uncertain)
        for path, draft in load_drafts(drafts_dir):
            draft_id = draft["draft_id"]
            if draft_id in seen or draft_id in uncertain_seen:
                continue
            if draft.get("status") != "queued":
                continue
            if not is_heydonna_source(draft):
                continue
            created_at = parse_timestamp(draft.get("created_at"))
            if created_at is None or created_at <= activated_at:
                continue
            envelope = build_pointer(draft, path)
            if envelope is None:
                continue
            if not message_pm or not Path(message_pm).is_file():
                _log(state_path, f"not_started draft_id={draft_id}")
                continue
            # Started/uncertain is durable before the transport. A lost response
            # or a crash after the effect must not send this id again.
            uncertain.append(draft_id)
            uncertain_seen.add(draft_id)
            state["uncertain_ids"] = uncertain
            _write_state(state_path, state)
            if not send_verified(message_pm, envelope):
                _log(state_path, f"uncertain draft_id={draft_id}")
                continue
            uncertain.remove(draft_id)
            uncertain_seen.remove(draft_id)
            delivered.append(draft_id)
            seen.add(draft_id)
            sent.append(draft_id)
            state["delivered_ids"] = delivered
            state["uncertain_ids"] = uncertain
            _write_state(state_path, state)
        _log(state_path, f"sent={len(sent)}")
        return {
            "activated": False,
            "activated_at": state["activated_at"],
            "sent": sent,
            "delivered_ids": delivered,
        }


def ensure_settings_hooks(settings: dict, command: str = HOOK_COMMAND) -> bool:
    """Register a separate async Stop and SubagentStop command. Never edits other hooks."""
    if not isinstance(settings, dict):
        raise ValueError("settings root must be an object")
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        settings["hooks"] = hooks
    changed = False
    for event in ("Stop", "SubagentStop"):
        entries = hooks.get(event)
        if not isinstance(entries, list):
            entries = []
            hooks[event] = entries
            changed = True
        match: dict | None = None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for inner in entry.get("hooks") or []:
                if isinstance(inner, dict) and "feedback-draft-queue-stop.py" in str(inner.get("command") or ""):
                    match = inner
                    break
            if match is not None:
                break
        if match is None:
            entries.append(
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                            "async": True,
                            "timeout": 5,
                        }
                    ]
                }
            )
            changed = True
            continue
        if match.get("command") != command or match.get("async") is not True:
            match["command"] = command
            match["async"] = True
            match.setdefault("type", "command")
            match.setdefault("timeout", 5)
            changed = True
    return changed


def install_settings(path: Path, command: str = HOOK_COMMAND) -> bool:
    path = Path(path)
    if path.is_file():
        settings = json.loads(path.read_text(encoding="utf-8"))
    else:
        settings = {}
    changed = ensure_settings_hooks(settings, command)
    if not changed:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return True


def _spawn_detached() -> int:
    try:
        sys.stdin.read()
    except Exception:
        pass
    env = os.environ.copy()
    env["FEEDBACK_DRAFT_QUEUE_DETACHED"] = "1"
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve())],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        env=env,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--self-check" in args:
        print("feedback-draft-queue-stop ready")
        return 0
    if "--install-settings" in args:
        target = Path(os.environ.get("CLAUDE_SETTINGS") or (Path.home() / ".claude" / "settings.json"))
        print("settings-updated" if install_settings(target) else "settings-present")
        return 0
    if "--activate" in args:
        result = process_queue(activate_only=True)
        print(f"activated_at={result['activated_at']}")
        return 0
    foreground = "--foreground" in args or os.environ.get("FEEDBACK_DRAFT_QUEUE_FOREGROUND") == "1"
    detached = os.environ.get("FEEDBACK_DRAFT_QUEUE_DETACHED") == "1"
    if not foreground and not detached:
        return _spawn_detached()
    if foreground:
        try:
            sys.stdin.read()
        except Exception:
            pass
    try:
        process_queue()
    except Exception as exc:
        _log(Path(os.environ.get("FEEDBACK_DRAFT_QUEUE_STATE") or default_state_path()), f"error={type(exc).__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
