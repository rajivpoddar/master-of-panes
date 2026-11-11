"""Slot identity remains visible across manual and launcher-based starts."""
import json
import os
from pathlib import Path
import subprocess

STATUSLINE = Path(__file__).resolve().parents[1] / "shared-assets/claude/statusline.sh"


def render(tmp_path, slot, inherited_name=None):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    git = bindir / "git"
    git.write_text(f"#!/bin/sh\nprintf '%s\\n' /Users/rajiv/Downloads/projects/heydonna-app-300{slot}\n")
    git.chmod(0o755)
    env = os.environ.copy()
    env.update(HOME=str(tmp_path), PATH=f"{bindir}:/usr/bin:/bin:/opt/homebrew/bin")
    env.pop("SLOT_NAME", None)
    if inherited_name is not None:
        env["SLOT_NAME"] = inherited_name
    data = {"workspace": {"current_dir": str(tmp_path)}, "model": {"display_name": "test"}}
    return subprocess.run(["bash", str(STATUSLINE)], input=json.dumps(data), text=True,
                          capture_output=True, env=env, cwd=tmp_path, check=True).stdout


def test_manual_relaunch_without_inherited_slot_environment(tmp_path):
    assert "Rohini" in render(tmp_path, 1)


def test_auto_restart_prefers_checkout_over_stale_inherited_name(tmp_path):
    assert "Hasta" in render(tmp_path, 2, "Rohini")
    assert "Rohini" not in render(tmp_path, 2, "Rohini")
