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


def test_dev_slot_skill_sync_enables_review_lifecycle_and_browser_toolkit(
    tmp_path: Path,
) -> None:
    slot_root = tmp_path / "slot"
    user_skills_root = tmp_path / "skills"
    (slot_root / ".claude").mkdir(parents=True)
    unrelated = user_skills_root / "unrelated-skill"
    unrelated.mkdir(parents=True)
    (unrelated / "SKILL.md").write_text(
        "---\nname: unrelated-skill\ndescription: test fixture\n---\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["node", str(SKILL_SYNC)],
        env={
            **os.environ,
            "HEYDONNA_DEV_SLOT_ROOTS": str(slot_root),
            "CLAUDE_USER_SKILLS_DIR": str(user_skills_root),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    settings = json.loads(
        (slot_root / ".claude" / "settings.local.json").read_text(encoding="utf-8")
    )
    required_skills = {
        "codex-app-plan-review",
        "codex-plan-review",
        "codex-app-code-review",
        "codex-code-review",
        "codex-app-qa-review",
        "codex-qa-review",
        "qa-brief",
        "proofshot",
        "heydonna-agent-browser",
        "playwright-testmatch-override-qa-only",
        "respawn",
        "agent-browser",
        "agent-browser-login",
        "agent-browser-project-creation",
        "agent-browser-proofreading",
        "agent-browser-prosemirror-selection",
        "agent-browser-prosemirror-typing",
    }
    for skill in required_skills:
        assert settings["skillOverrides"][skill] == "on", skill
    assert settings["skillOverrides"]["unrelated-skill"] == "off"
