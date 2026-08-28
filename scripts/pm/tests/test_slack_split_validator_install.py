from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets"
MANIFEST_PATH = SHARED / "manifest.json"
INSTALLER_PATH = ROOT / "scripts" / "install-release.py"
VALIDATOR = SHARED / "claude/skills/slack-message/scripts/block-unsupported-transcript-split.py"
SENDER = SHARED / "claude/skills/slack-message/scripts/slack-send.sh"

SPEC = importlib.util.spec_from_file_location("install_release_slack_split", INSTALLER_PATH)
assert SPEC and SPEC.loader
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)


def _run_validator(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(VALIDATOR), *args], capture_output=True, text=True, check=False
    )


def test_red_old_absolute_app_target_refuses_before_any_send() -> None:
    with tempfile.TemporaryDirectory() as temp:
        script = Path(temp) / "slack-send.sh"
        text = SENDER.read_text(encoding="utf-8")
        text = text.replace(
            "/Users/rajiv/.claude/skills/slack-message/scripts/block-unsupported-transcript-split.py",
            str(Path(temp) / "missing" / "block-unsupported-transcript-split.py"),
            1,
        )
        script.write_text(text, encoding="utf-8")
        script.chmod(0o755)
        result = subprocess.run(["bash", str(script), "safe status"], capture_output=True, text=True)
        assert result.returncode == 24
        assert "required Slack product-safety hook missing" in result.stderr


def test_manifest_import_and_colocated_sender_preserve_safe_gate() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    entries = {entry["source_path"]: entry for entry in manifest["entries"]}
    validator_key = "claude/skills/slack-message/scripts/block-unsupported-transcript-split.py"
    sender_key = "claude/skills/slack-message/scripts/slack-send.sh"
    assert manifest["inventory"]["selected_count"] == 28
    assert entries[validator_key]["canonical_target"] == "/Users/rajiv/.claude/skills/slack-message/scripts/block-unsupported-transcript-split.py"
    assert entries[sender_key]["canonical_target"] == "/Users/rajiv/.claude/skills/slack-message/scripts/slack-send.sh"
    assert hashlib.sha256(VALIDATOR.read_bytes()).hexdigest() == entries[validator_key]["sha256"]
    assert stat.S_IMODE(VALIDATOR.stat().st_mode) == 0o755
    assert "/Users/rajiv/Downloads/projects/heydonna-app/scripts/pm/block-unsupported-transcript-split.py" not in SENDER.read_text(encoding="utf-8")

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        release = root / "release"
        shutil.copytree(SHARED, release / "scripts/pm/shared-assets")
        targets = root / "targets"
        rollback = root / "rollback"
        INSTALLER.install_shared_assets(release_dir=release, target_root=targets, rollback_bundle=rollback)
        installed_validator = targets / "Users/rajiv/.claude/skills/slack-message/scripts/block-unsupported-transcript-split.py"
        installed_sender = targets / "Users/rajiv/.claude/skills/slack-message/scripts/slack-send.sh"
        assert installed_validator.read_bytes() == VALIDATOR.read_bytes()
        assert stat.S_IMODE(installed_validator.stat().st_mode) == 0o755
        assert stat.S_IMODE(installed_sender.stat().st_mode) == 0o755

        runnable = root / "runnable-slack-send.sh"
        runnable.write_text(
            installed_sender.read_text(encoding="utf-8").replace(
                "/Users/rajiv/.claude/skills/slack-message/scripts/block-unsupported-transcript-split.py",
                str(installed_validator),
                1,
            ),
            encoding="utf-8",
        )
        runnable.chmod(0o755)
        fake_bin = root / "bin"
        fake_bin.mkdir()
        fake_curl = fake_bin / "curl"
        fake_curl.write_text("#!/bin/sh\nprintf '%s\\n' '{\"ok\":true,\"ts\":\"T\",\"channel\":\"C\"}'\n", encoding="utf-8")
        fake_curl.chmod(0o755)
        env = {**os.environ, "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}", "SLACK_BOT_TOKEN": "test-token"}
        safe = subprocess.run(["bash", str(runnable), "safe status"], env=env, capture_output=True, text=True)
        assert safe.returncode == 0
        assert "OK ts=T channel=C" in safe.stdout
        unsafe = subprocess.run(["bash", str(runnable), "Please split this transcript into sections"], env=env, capture_output=True, text=True)
        assert unsafe.returncode == 23
        assert "UNSUPPORTED_TRANSCRIPT_SPLIT_BLOCKED" in unsafe.stderr


def test_late_failure_restores_existing_caller_bytes_and_mode() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        release = root / "release"
        shutil.copytree(SHARED, release / "scripts/pm/shared-assets")
        targets = root / "targets"
        target = targets / "Users/rajiv/.claude/skills/slack-message/scripts/block-unsupported-transcript-split.py"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"previous-validator\n")
        target.chmod(0o640)
        before = target.read_bytes()
        before_mode = stat.S_IMODE(target.stat().st_mode)
        with pytest.raises(INSTALLER.InstallerError):
            INSTALLER.install_shared_assets(
                release_dir=release,
                target_root=targets,
                rollback_bundle=root / "rollback",
                fail_after=1,
            )
        assert target.read_bytes() == before
        assert stat.S_IMODE(target.stat().st_mode) == before_mode
