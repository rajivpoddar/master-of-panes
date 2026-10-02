"""HeyDonna /feedback draft queue hook.

The hook must return without blocking, send a draft id at most once, ignore
non-HeyDonna drafts, and forward a pointer that does not carry verdict prose
from the draft body.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
HOOK = ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "hooks" / "feedback-draft-queue-stop.py"


def load_hook():
    import importlib.util

    spec = importlib.util.spec_from_file_location("feedback_draft_queue_stop", HOOK)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_draft(directory: Path, name: str, payload: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def write_message_pm(path: Path, *, sleep_seconds: float = 0, fail: bool = False) -> None:
    path.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import os
            import pathlib
            import sys
            import time

            args = sys.argv[1:]
            if "--file" not in args:
                print("MESSAGE_PM_FAILED reason=file_not_found", file=sys.stderr)
                raise SystemExit(2)
            body = pathlib.Path(args[args.index("--file") + 1]).read_text(encoding="utf-8")
            record = pathlib.Path(os.environ["MESSAGE_PM_RECORD"])
            previous = record.read_text(encoding="utf-8") if record.exists() else ""
            record.write_text(previous + body + "\\n---\\n", encoding="utf-8")
            time.sleep({sleep_seconds})
            if {str(fail)}:
                print("MESSAGE_PM_FAILED reason=unverified_delivery", file=sys.stderr)
                raise SystemExit(1)
            print(f"MESSAGE_PM_SENT slot=0 verified=true bytes={{len(body.encode('utf-8'))}}")
            """
        ),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def heydonna_draft(draft_id: str, created_at: str, **extra) -> dict:
    payload = {
        "draft_id": draft_id,
        "created_at": created_at,
        "cwd": "/Users/rajiv/Downloads/projects/heydonna-app-3005",
        "status": "queued",
        "title": "Queue pointer only",
        "type": "bug",
        "transcript_ref": {"project_dir_key": "-Users-rajiv-Downloads-projects-heydonna-app-3005"},
    }
    payload.update(extra)
    return payload


def test_hook_return_is_nonblocking(tmp_path: Path) -> None:
    hook = load_hook()
    drafts = tmp_path / "drafts"
    state = tmp_path / "state.json"
    record = tmp_path / "sent.txt"
    message_pm = tmp_path / "message-pm.py"
    write_message_pm(message_pm, sleep_seconds=2)
    activated = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    state.write_text(
        json.dumps({"activated_at": activated.isoformat(), "delivered_ids": []}),
        encoding="utf-8",
    )
    write_draft(
        drafts,
        "draft-1.json",
        heydonna_draft("draft-1", (activated + timedelta(minutes=5)).isoformat()),
    )
    env = os.environ.copy()
    env.update(
        {
            "FEEDBACK_DRAFT_DRAFTS_DIR": str(drafts),
            "FEEDBACK_DRAFT_QUEUE_STATE": str(state),
            "MESSAGE_PM_BIN": str(message_pm),
            "MESSAGE_PM_RECORD": str(record),
        }
    )
    env.pop("FEEDBACK_DRAFT_QUEUE_FOREGROUND", None)
    env.pop("FEEDBACK_DRAFT_QUEUE_DETACHED", None)
    started = time.monotonic()
    completed = subprocess.run(
        [os.environ.get("MOP_PYTHON", "python3"), str(HOOK)],
        input=b"{}",
        capture_output=True,
        env=env,
        check=False,
    )
    elapsed = time.monotonic() - started
    assert completed.returncode == 0
    assert completed.stdout == b""
    assert b"decision" not in completed.stdout
    assert b"block" not in completed.stdout
    assert elapsed < 1.0
    deadline = time.monotonic() + 5
    delivered: list[str] = []
    while time.monotonic() < deadline:
        if state.is_file():
            delivered = json.loads(state.read_text(encoding="utf-8")).get("delivered_ids") or []
            if delivered:
                break
        time.sleep(0.05)
    assert delivered == ["draft-1"]
    assert "validate-codex-review-agent-result" not in HOOK.read_text(encoding="utf-8")
    assert hook.HOOK_COMMAND.endswith("feedback-draft-queue-stop.py")


def test_same_draft_id_is_not_sent_twice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hook = load_hook()
    drafts = tmp_path / "drafts"
    state = tmp_path / "state.json"
    record = tmp_path / "sent.txt"
    message_pm = tmp_path / "message-pm.py"
    write_message_pm(message_pm)
    monkeypatch.setenv("MESSAGE_PM_RECORD", str(record))
    activated = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    created = (activated + timedelta(minutes=1)).isoformat()
    state.write_text(
        json.dumps({"activated_at": activated.isoformat(), "delivered_ids": []}),
        encoding="utf-8",
    )
    write_draft(drafts, "one.json", heydonna_draft("shared-id", created))
    write_draft(drafts, "two.json", heydonna_draft("shared-id", created))
    first = hook.process_queue(drafts_dir=drafts, state_path=state, message_pm=str(message_pm))
    second = hook.process_queue(drafts_dir=drafts, state_path=state, message_pm=str(message_pm))
    assert first["sent"] == ["shared-id"]
    assert second["sent"] == []
    assert json.loads(state.read_text(encoding="utf-8"))["delivered_ids"] == ["shared-id"]
    assert record.read_text(encoding="utf-8").count("draft_id: shared-id") == 1


def test_non_heydonna_draft_is_not_delivered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hook = load_hook()
    drafts = tmp_path / "drafts"
    state = tmp_path / "state.json"
    record = tmp_path / "sent.txt"
    message_pm = tmp_path / "message-pm.py"
    write_message_pm(message_pm)
    monkeypatch.setenv("MESSAGE_PM_RECORD", str(record))
    activated = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    state.write_text(
        json.dumps({"activated_at": activated.isoformat(), "delivered_ids": []}),
        encoding="utf-8",
    )
    write_draft(
        drafts,
        "other.json",
        {
            "draft_id": "other-1",
            "created_at": (activated + timedelta(minutes=2)).isoformat(),
            "cwd": "/Users/rajiv/Downloads/projects/other-app",
            "status": "queued",
            "title": "Not ours",
            "type": "bug",
            "transcript_ref": {"project_dir_key": "-Users-rajiv-Downloads-projects-other-app"},
            "details": "COMPANION_VERDICT: REJECT",
        },
    )
    result = hook.process_queue(drafts_dir=drafts, state_path=state, message_pm=str(message_pm))
    assert result["sent"] == []
    assert json.loads(state.read_text(encoding="utf-8"))["delivered_ids"] == []
    assert not record.exists()


def test_pointer_envelope_omits_verdict_words_from_the_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hook = load_hook()
    drafts = tmp_path / "drafts"
    state = tmp_path / "state.json"
    record = tmp_path / "sent.txt"
    message_pm = tmp_path / "message-pm.py"
    write_message_pm(message_pm)
    monkeypatch.setenv("MESSAGE_PM_RECORD", str(record))
    activated = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    state.write_text(
        json.dumps({"activated_at": activated.isoformat(), "delivered_ids": []}),
        encoding="utf-8",
    )
    path = write_draft(
        drafts,
        "body.json",
        heydonna_draft(
            "body-1",
            (activated + timedelta(minutes=3)).isoformat(),
            details=(
                "COMPANION_VERDICT: REJECT\n"
                "The preserved review body says APPROVE.\n"
                "status: REJECT\n"
            ),
            review="APPROVE this companion verdict",
        ),
    )
    result = hook.process_queue(drafts_dir=drafts, state_path=state, message_pm=str(message_pm))
    assert result["sent"] == ["body-1"]
    envelope = record.read_text(encoding="utf-8")
    assert "draft_id: body-1" in envelope
    assert f"path: {path}" in envelope
    assert "title: Queue pointer only" in envelope
    assert "type: bug" in envelope
    for word in ("COMPANION_VERDICT", "APPROVE", "REJECT"):
        assert word not in envelope
    assert "preserved review body" not in envelope


def test_unverified_send_is_not_marked_delivered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hook = load_hook()
    drafts = tmp_path / "drafts"
    state = tmp_path / "state.json"
    record = tmp_path / "sent.txt"
    message_pm = tmp_path / "message-pm.py"
    write_message_pm(message_pm, fail=True)
    monkeypatch.setenv("MESSAGE_PM_RECORD", str(record))
    activated = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    state.write_text(
        json.dumps({"activated_at": activated.isoformat(), "delivered_ids": []}),
        encoding="utf-8",
    )
    write_draft(
        drafts,
        "draft.json",
        heydonna_draft("draft-unverified", (activated + timedelta(minutes=1)).isoformat()),
    )
    result = hook.process_queue(drafts_dir=drafts, state_path=state, message_pm=str(message_pm))
    assert result["sent"] == []
    assert json.loads(state.read_text(encoding="utf-8"))["delivered_ids"] == []


def test_existing_backlog_is_not_flushed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hook = load_hook()
    drafts = tmp_path / "drafts"
    state = tmp_path / "state.json"
    record = tmp_path / "sent.txt"
    message_pm = tmp_path / "message-pm.py"
    write_message_pm(message_pm)
    monkeypatch.setenv("MESSAGE_PM_RECORD", str(record))
    now = datetime(2026, 10, 2, 13, 0, tzinfo=timezone.utc)
    write_draft(drafts, "old.json", heydonna_draft("old-draft", (now - timedelta(days=1)).isoformat()))
    first = hook.process_queue(drafts_dir=drafts, state_path=state, message_pm=str(message_pm), now=now)
    second = hook.process_queue(drafts_dir=drafts, state_path=state, message_pm=str(message_pm), now=now)
    assert first["sent"] == []
    assert second["sent"] == []
    assert not record.exists()


def test_settings_registration_is_async_and_outside_the_review_hook(tmp_path: Path) -> None:
    hook = load_hook()
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python3 /Users/rajiv/.claude/hooks/validate-codex-review-agent-result.py",
                                    "timeout": 10,
                                }
                            ]
                        }
                    ],
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "bash /Users/rajiv/.claude/hooks/slot-terminal-message-pm-stop.sh",
                                    "timeout": 5,
                                }
                            ]
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    assert hook.install_settings(path) is True
    assert hook.install_settings(path) is False
    settings = json.loads(path.read_text(encoding="utf-8"))
    review = settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    assert review.endswith("validate-codex-review-agent-result.py")
    for event in ("Stop", "SubagentStop"):
        matches = [
            inner
            for entry in settings["hooks"][event]
            for inner in entry["hooks"]
            if "feedback-draft-queue-stop.py" in inner["command"]
        ]
        assert len(matches) == 1
        assert matches[0]["async"] is True
        assert matches[0]["command"] == hook.HOOK_COMMAND
    stop_commands = [inner["command"] for entry in settings["hooks"]["Stop"] for inner in entry["hooks"]]
    assert stop_commands[0].endswith("slot-terminal-message-pm-stop.sh")
