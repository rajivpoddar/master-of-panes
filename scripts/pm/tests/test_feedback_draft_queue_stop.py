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
HEYDONNA_CWD = ""


@pytest.fixture(autouse=True)
def isolated_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    checkout = tmp_path / "heydonna-app-3005"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin",
                    "https://github.com/heydonna-app/heydonna-app.git"], check=True)
    monkeypatch.setitem(globals(), "HEYDONNA_CWD", str(checkout))
    return checkout


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
        "cwd": HEYDONNA_CWD,
        "status": "queued",
        "title": "Queue pointer only",
        "type": "bug",
        "transcript_ref": {"project_dir_key": HEYDONNA_CWD.replace("/", "-")},
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


def _activated_state(path: Path, activated: datetime, **extra: object) -> None:
    payload = {"activated_at": activated.isoformat(), "delivered_ids": []}
    payload.update(extra)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _effect_sender(path: Path, *, mode: str) -> None:
    path.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json
            import os
            import pathlib
            import sys

            args = sys.argv[1:]
            if "--file" not in args:
                raise SystemExit(2)
            body = pathlib.Path(args[args.index("--file") + 1]).read_text(encoding="utf-8")
            state_path = pathlib.Path(os.environ["FEEDBACK_DRAFT_QUEUE_STATE"])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            expected = os.environ["EXPECT_UNCERTAIN_ID"]
            record = pathlib.Path(os.environ["MESSAGE_PM_RECORD"])
            previous = record.read_text(encoding="utf-8") if record.exists() else ""
            if expected not in (state.get("uncertain_ids") or []):
                record.write_text(previous + "MISSING_UNCERTAIN\\n", encoding="utf-8")
                raise SystemExit(3)
            record.write_text(previous + "ACCEPTED " + expected + "\\n" + body + "\\n---\\n", encoding="utf-8")
            if {mode!r} == "lose_response":
                raise SystemExit(1)
            print("MESSAGE_PM_SENT slot=0 verified=true bytes={{}}".format(len(body.encode("utf-8"))))
            """
        ),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _prepare_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, draft_id: str):
    hook = load_hook()
    drafts = tmp_path / "drafts"
    state = tmp_path / "state.json"
    record = tmp_path / "sent.txt"
    message_pm = tmp_path / "message-pm.py"
    activated = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    _activated_state(state, activated)
    write_draft(
        drafts,
        "draft.json",
        heydonna_draft(draft_id, (activated + timedelta(minutes=1)).isoformat()),
    )
    monkeypatch.setenv("FEEDBACK_DRAFT_QUEUE_STATE", str(state))
    monkeypatch.setenv("MESSAGE_PM_RECORD", str(record))
    monkeypatch.setenv("EXPECT_UNCERTAIN_ID", draft_id)
    return hook, drafts, state, record, message_pm, activated


def test_response_loss_does_not_send_a_second_effect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hook, drafts, state, record, message_pm, _activated = _prepare_queue(tmp_path, monkeypatch, "draft-loss")
    _effect_sender(message_pm, mode="lose_response")
    first = hook.process_queue(drafts_dir=drafts, state_path=state, message_pm=str(message_pm))
    second = hook.process_queue(drafts_dir=drafts, state_path=state, message_pm=str(message_pm))
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert first["sent"] == []
    assert second["sent"] == []
    assert saved["delivered_ids"] == []
    assert saved["uncertain_ids"] == ["draft-loss"]
    assert record.read_text(encoding="utf-8").count("ACCEPTED draft-loss") == 1
    assert "MISSING_UNCERTAIN" not in record.read_text(encoding="utf-8")


def test_post_ack_crash_does_not_send_a_second_effect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hook, drafts, state, record, message_pm, _activated = _prepare_queue(tmp_path, monkeypatch, "draft-crash")
    _effect_sender(message_pm, mode="verified")
    real_send = hook.send_verified

    def crash_after_ack(message_pm_bin: str, envelope: str) -> bool:
        ok = real_send(message_pm_bin, envelope)
        assert ok is True
        raise SystemExit(86)

    monkeypatch.setattr(hook, "send_verified", crash_after_ack)
    with pytest.raises(SystemExit):
        hook.process_queue(drafts_dir=drafts, state_path=state, message_pm=str(message_pm))
    monkeypatch.setattr(hook, "send_verified", real_send)
    second = hook.process_queue(drafts_dir=drafts, state_path=state, message_pm=str(message_pm))
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert second["sent"] == []
    assert saved["delivered_ids"] == []
    assert saved["uncertain_ids"] == ["draft-crash"]
    assert record.read_text(encoding="utf-8").count("ACCEPTED draft-crash") == 1


def test_reentry_of_uncertain_effect_does_not_send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hook, drafts, state, record, message_pm, activated = _prepare_queue(tmp_path, monkeypatch, "draft-reentry")
    _activated_state(state, activated, uncertain_ids=["draft-reentry"])
    _effect_sender(message_pm, mode="verified")
    result = hook.process_queue(drafts_dir=drafts, state_path=state, message_pm=str(message_pm))
    assert result["sent"] == []
    assert not record.exists()
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["uncertain_ids"] == ["draft-reentry"]
    assert saved["delivered_ids"] == []


def test_foreign_and_lookalike_provenance_is_not_delivered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hook = load_hook()
    drafts = tmp_path / "drafts"
    state = tmp_path / "state.json"
    record = tmp_path / "sent.txt"
    message_pm = tmp_path / "message-pm.py"
    write_message_pm(message_pm)
    monkeypatch.setenv("MESSAGE_PM_RECORD", str(record))
    activated = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    created = (activated + timedelta(minutes=2)).isoformat()
    _activated_state(state, activated)
    write_draft(
        drafts,
        "stale-key.json",
        {
            "draft_id": "stale-key",
            "created_at": created,
            "cwd": "/Users/rajiv/Downloads/projects/other-app",
            "status": "queued",
            "title": "Foreign checkout",
            "type": "bug",
            "transcript_ref": {"project_dir_key": "-Users-rajiv-Downloads-projects-heydonna-app"},
        },
    )
    lookalike = "/Users/rajiv/Downloads/projects/heydonna-app-unrelated"
    write_draft(
        drafts,
        "lookalike.json",
        {
            "draft_id": "lookalike",
            "created_at": created,
            "cwd": lookalike,
            "status": "queued",
            "title": "Lookalike",
            "type": "bug",
            "transcript_ref": {"project_dir_key": lookalike.replace("/", "-")},
        },
    )
    write_draft(
        drafts,
        "malformed.json",
        {
            "draft_id": "malformed",
            "created_at": created,
            "cwd": "/Users/rajiv/Downloads/projects/heydonna-app/../other-app",
            "status": "queued",
            "title": "Malformed",
            "type": "bug",
            "transcript_ref": {"project_dir_key": "-Users-rajiv-Downloads-projects-heydonna-app"},
        },
    )
    unrelated_repo = tmp_path / "heydonna-app-unrelated"
    unrelated_repo.mkdir()
    subprocess.run(["git", "init"], cwd=unrelated_repo, check=True, capture_output=True, text=True)
    write_draft(
        drafts,
        "unrelated-repo.json",
        {
            "draft_id": "unrelated-repo",
            "created_at": created,
            "cwd": str(unrelated_repo),
            "status": "queued",
            "title": "Unrelated repo",
            "type": "bug",
            "transcript_ref": {"project_dir_key": str(unrelated_repo).replace("/", "-")},
        },
    )
    result = hook.process_queue(drafts_dir=drafts, state_path=state, message_pm=str(message_pm))
    assert result["sent"] == []
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["delivered_ids"] == []
    assert (saved.get("uncertain_ids") or []) == []
    assert not record.exists()


def test_legitimate_heydonna_worktree_delivers_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hook = load_hook()
    repo = tmp_path / "heydonna-app"
    repo.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "mop-test",
            "GIT_AUTHOR_EMAIL": "mop-test@example.com",
            "GIT_COMMITTER_NAME": "mop-test",
            "GIT_COMMITTER_EMAIL": "mop-test@example.com",
        }
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True, text=True, env=env)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    "https://github.com/heydonna-app/heydonna-app.git"], check=True)
    (repo / "README").write_text("heydonna\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=repo, check=True, capture_output=True, text=True, env=env)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    worktree = tmp_path / "feature-wt"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "--detach", str(worktree), "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    cwd = str(worktree.resolve())
    assert "heydonna-app" not in worktree.name
    drafts = tmp_path / "drafts"
    state = tmp_path / "state.json"
    record = tmp_path / "sent.txt"
    message_pm = tmp_path / "message-pm.py"
    write_message_pm(message_pm)
    monkeypatch.setenv("MESSAGE_PM_RECORD", str(record))
    activated = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    _activated_state(state, activated)
    write_draft(
        drafts,
        "worktree.json",
        heydonna_draft(
            "worktree-1",
            (activated + timedelta(minutes=4)).isoformat(),
            cwd=cwd,
            transcript_ref={"project_dir_key": cwd.replace("/", "-")},
        ),
    )
    first = hook.process_queue(drafts_dir=drafts, state_path=state, message_pm=str(message_pm))
    second = hook.process_queue(drafts_dir=drafts, state_path=state, message_pm=str(message_pm))
    assert first["sent"] == ["worktree-1"]
    assert second["sent"] == []
    assert record.read_text(encoding="utf-8").count("draft_id: worktree-1") == 1
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["delivered_ids"] == ["worktree-1"]
    assert saved["uncertain_ids"] == []


def test_eight_simultaneous_invocations_deliver_once(tmp_path: Path) -> None:
    drafts = tmp_path / "drafts"
    state = tmp_path / "state.json"
    record = tmp_path / "sent.txt"
    message_pm = tmp_path / "message-pm.py"
    write_message_pm(message_pm)
    activated = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    _activated_state(state, activated)
    write_draft(
        drafts,
        "draft.json",
        heydonna_draft("draft-once", (activated + timedelta(minutes=1)).isoformat()),
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
    env.pop("FEEDBACK_DRAFT_QUEUE_DETACHED", None)
    processes = [
        subprocess.Popen(
            [os.environ.get("MOP_PYTHON", "python3"), str(HOOK), "--foreground"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        for _ in range(8)
    ]
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        assert stdout == b""
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["delivered_ids"] == ["draft-once"]
    assert saved["uncertain_ids"] == []
    assert record.read_text(encoding="utf-8").count("draft_id: draft-once") == 1


@pytest.mark.parametrize("history", [
    {"uncertain_ids": ["held"], "outcomes": {}},
    {"outcomes": {"held": {"status": "uncertain", "reason": "legacy_unknown_outcome"}}},
    {"uncertain_ids": ["held"], "outcomes": {
        "other-held": {"status": "uncertain", "started_at": "2026-10-02T12:00:00Z"},
        "done-outcome": {"status": "delivered", "verified_at": "2026-10-02T12:00:00Z"}}},
])
def test_both_schema_histories_survive_send_and_fresh_reentry(tmp_path, monkeypatch, history):
    hook, drafts, state, record, sender, activated = _prepare_queue(tmp_path, monkeypatch, "new")
    _activated_state(state, activated, delivered_ids=["done"], **history)
    before = json.loads(state.read_text())
    for draft_id in ("held", "other-held", "done-outcome", "done"):
        # Only create the IDs represented in this case's recorded history.
        if draft_id not in before.get("uncertain_ids", []) and draft_id not in before.get("outcomes", {}) and draft_id != "done":
            continue
        write_draft(drafts, draft_id + ".json", heydonna_draft(draft_id, (activated + timedelta(minutes=1)).isoformat()))
    _effect_sender(sender, mode="verified")
    first = hook.process_queue(drafts_dir=drafts, state_path=state, message_pm=str(sender))
    second = load_hook().process_queue(drafts_dir=drafts, state_path=state, message_pm=str(sender))
    saved = json.loads(state.read_text())
    assert first["sent"] == ["new"]
    assert second["sent"] == []
    assert record.read_text().count("ACCEPTED ") == 1
    assert "MISSING_UNCERTAIN" not in record.read_text()
    assert saved["activated_at"] == before["activated_at"]
    assert saved["delivered_ids"] == ["done", "new"]
    assert saved.get("outcomes", {}) == before.get("outcomes", {})
    assert saved["uncertain_ids"][:len(before.get("uncertain_ids", []))] == before.get("uncertain_ids", [])
    assert set(before.get("uncertain_ids", [])) <= set(saved["uncertain_ids"])
    assert set(key for key, value in before.get("outcomes", {}).items() if value["status"] == "uncertain") <= set(saved["uncertain_ids"])


@pytest.mark.parametrize("invalid", [
    "{", "[]", '{"activated_at":"bad","delivered_ids":[]}',
    '{"activated_at":"2026-10-02T12:00:00Z","delivered_ids":null}',
    {"uncertain_ids": "held"}, {"uncertain_ids": [None]}, {"uncertain_ids": [" held "]},
    {"outcomes": []}, {"outcomes": {"held": {"status": "unknown"}}},
    {"outcomes": {"held": None}}, {"outcomes": {" held ": {"status": "uncertain"}}},
    {"future_no_send_ids": ["held"]},
])
def test_malformed_or_unrecognized_history_never_sends_or_rewrites(tmp_path, monkeypatch, invalid):
    hook, drafts, state, record, sender, activated = _prepare_queue(tmp_path, monkeypatch, "new")
    if isinstance(invalid, dict):
        payload = {"activated_at": activated.isoformat(), "delivered_ids": []}
        payload.update(invalid)
        state.write_text(json.dumps(payload))
    else:
        state.write_text(invalid)
    before = state.read_bytes()
    _effect_sender(sender, mode="verified")
    with pytest.raises(ValueError, match="reconcile before sending"):
        hook.process_queue(drafts_dir=drafts, state_path=state, message_pm=str(sender))
    assert state.read_bytes() == before
    assert not record.exists()


def test_supported_name_cannot_admit_foreign_git_origin(tmp_path, isolated_checkout):
    subprocess.run(["git", "-C", str(isolated_checkout), "remote", "set-url", "origin",
                    "https://github.com/other/other-app.git"], check=True)
    assert not load_hook().is_heydonna_source(heydonna_draft("foreign", "2026-10-02T12:01:00Z"))


def test_git_context_cannot_redirect_worktree_identity(tmp_path, monkeypatch, isolated_checkout):
    # A caller's Git context must not make a lookalike path a supported worktree.
    other = tmp_path / "heydonna-app-unrelated"
    other.mkdir()
    monkeypatch.setenv("GIT_DIR", str(isolated_checkout / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    draft = heydonna_draft("foreign", "2026-10-02T12:01:00Z", cwd=str(other),
                          transcript_ref={"project_dir_key": str(other).replace("/", "-")})
    assert not load_hook().is_heydonna_source(draft)


@pytest.mark.parametrize("remote", [
    "https://github.com/heydonna-app/heydonna-app",
    "git@github.com:heydonna-app/heydonna-app.git",
    "ssh://git@github.com/heydonna-app/heydonna-app.git",
])
def test_supported_origin_forms_remain_eligible(isolated_checkout, remote):
    subprocess.run(["git", "-C", str(isolated_checkout), "remote", "set-url", "origin", remote], check=True)
    assert load_hook().is_heydonna_source(heydonna_draft("valid", "2026-10-02T12:01:00Z"))
