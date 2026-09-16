"""Dev-slot Codex reviews must never run as unbounded background Agent calls.

S6 (/Users/rajiv/Downloads/projects/heydonna-app-3006) launched
`Agent(subagent_type="codex-code-reviewer")` with no `run_in_background` key on
2026-09-16T00:08:13Z. Claude backgrounded it, and because that slot's project
settings were a 90-byte statusLine stub the dev-slot foreground-Agent guard was
never registered, so the review ran unbounded for 9h29m36s.

These tests pin the launch-time control point: the MoP-owned slot settings
writer must materialize the guard registration and must fail closed when the
guard executable is unavailable.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[3]
SKILL_SYNC = (
    ROOT
    / "scripts"
    / "pm"
    / "shared-assets"
    / "claude"
    / "scripts"
    / "sync-dev-slot-skill-allowlist.mjs"
)

DEFAULT_AGENT_GUARD = Path(
    "/Users/rajiv/.claude/hooks/force-dev-slot-foreground-agent.py"
)

# The exact tool_input recorded by the hanging S6 session: no run_in_background.
RECORDED_AGENT_INPUT = {
    "description": "Codex code review of #7748",
    "subagent_type": "codex-code-reviewer",
    "prompt": "Review the in-progress working-tree diff for HeyDonna issue #7748.",
}


def _run_sync(slot_root: Path, skills_root: Path, guard: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["node", str(SKILL_SYNC)],
        env={
            **os.environ,
            "HEYDONNA_DEV_SLOT_ROOTS": str(slot_root),
            "CLAUDE_USER_SKILLS_DIR": str(skills_root),
            "DEV_SLOT_AGENT_GUARD_HOOK": str(guard),
        },
        capture_output=True,
        text=True,
        check=False,
    )


def _guard_commands(settings: dict) -> list[str]:
    commands: list[str] = []
    for entry in settings.get("hooks", {}).get("PreToolUse", []) or []:
        if entry.get("matcher") != "Agent":
            continue
        for hook in entry.get("hooks", []) or []:
            if isinstance(hook.get("command"), str):
                commands.append(hook["command"])
    return commands


def test_slot_settings_gain_foreground_agent_guard_and_stay_idempotent(
    tmp_path: Path,
) -> None:
    slot_root = tmp_path / "heydonna-app-3006"
    skills_root = tmp_path / "skills"
    guard = tmp_path / "force-dev-slot-foreground-agent.py"
    (slot_root / ".claude").mkdir(parents=True)
    skills_root.mkdir(parents=True)
    guard.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n", encoding="utf-8")
    guard.chmod(0o755)

    # RED baseline: the observed S6 shape carried only statusLine.
    settings_path = slot_root / ".claude" / "settings.json"
    settings_path.write_text(
        json.dumps({"statusLine": {"type": "command", "command": "~/.claude/statusline.sh"}}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    assert _guard_commands(json.loads(settings_path.read_text(encoding="utf-8"))) == []

    result = _run_sync(slot_root, skills_root, guard)
    assert result.returncode == 0, result.stderr

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    commands = _guard_commands(settings)
    assert commands == [str(guard)], commands
    # Unrelated content survives the materialization.
    assert settings["statusLine"]["command"] == "~/.claude/statusline.sh"

    after_first = settings_path.read_bytes()
    second = _run_sync(slot_root, skills_root, guard)
    assert second.returncode == 0, second.stderr
    assert settings_path.read_bytes() == after_first
    assert "AGENT_GUARD_OK" in second.stdout


def test_slot_provisioning_fails_closed_when_guard_is_absent(tmp_path: Path) -> None:
    slot_root = tmp_path / "heydonna-app-3005"
    skills_root = tmp_path / "skills"
    (slot_root / ".claude").mkdir(parents=True)
    skills_root.mkdir(parents=True)

    result = _run_sync(slot_root, skills_root, tmp_path / "does-not-exist.py")

    assert result.returncode == 2, result.stdout
    assert "AGENT_GUARD_MISSING" in result.stderr
    assert "DEV_SLOT_AGENT_GUARD_FAILED" in result.stderr


def _run_guard(guard: Path, cwd: Path, stdin: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", str(guard)],
        cwd=str(cwd),
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def test_registered_guard_selects_bounded_foreground_review(tmp_path: Path) -> None:
    guard = DEFAULT_AGENT_GUARD
    if not guard.exists():
        import pytest

        pytest.skip(f"dev-slot Agent guard not installed at {guard}")

    slot_cwd = tmp_path / "heydonna-app-3006"
    slot_cwd.mkdir(parents=True)
    pm_cwd = tmp_path / "heydonna-app"
    pm_cwd.mkdir(parents=True)

    # The exact recorded S6 dispatch: backgrounded because the field was omitted.
    backgrounded = _run_guard(guard, slot_cwd, json.dumps({"tool_input": RECORDED_AGENT_INPUT}))
    assert backgrounded.returncode == 0, backgrounded.stderr
    decision = json.loads(backgrounded.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "allow"
    assert decision["updatedInput"]["run_in_background"] is False
    assert decision["updatedInput"]["subagent_type"] == "codex-code-reviewer"

    # An explicit background request is normalized the same way.
    explicit = _run_guard(
        guard,
        slot_cwd,
        json.dumps({"tool_input": {**RECORDED_AGENT_INPUT, "run_in_background": True}}),
    )
    assert json.loads(explicit.stdout)["hookSpecificOutput"]["updatedInput"]["run_in_background"] is False

    # An already-foreground call passes through untouched.
    foreground = _run_guard(
        guard,
        slot_cwd,
        json.dumps({"tool_input": {**RECORDED_AGENT_INPUT, "run_in_background": False}}),
    )
    assert foreground.returncode == 0
    assert foreground.stdout.strip() == ""

    # Non-slot checkouts (PM pane) are untouched by this guard.
    pm = _run_guard(guard, pm_cwd, json.dumps({"tool_input": RECORDED_AGENT_INPUT}))
    assert pm.returncode == 0
    assert pm.stdout.strip() == ""

    # Fail closed when background mode cannot be ruled out.
    malformed = _run_guard(guard, slot_cwd, "{not json")
    assert json.loads(malformed.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
