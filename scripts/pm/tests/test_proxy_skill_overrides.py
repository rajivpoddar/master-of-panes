"""Proxy lane skill allowlist (added to the repo; not executed on the host, Rajiv ts 1793983042.752819).

Rajiv D0AMF0XE6TS thread 1795230917.966119, ts 1795310703.137599 ("Yes, do it"): proxy lanes list only the skills lanes
use. Mechanism: Claude Code settings keys skillOverrides and skillListingMaxDescChars merged into the lane's own
--settings file by proxy-agent.sh (docs: code.claude.com/docs/en/skills#override-skill-visibility-from-settings).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ASSETS = Path(__file__).parents[1] / "shared-assets/claude/scripts"
GEN = ASSETS / "proxy-skill-overrides.py"
LAUNCHER = (ASSETS / "proxy-agent.sh").read_text()


def _skill(root, name, body="", frontmatter_name=None):
    d = root / ".claude" / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: %s\ndescription: fixture\n---\n%s\n" % (frontmatter_name or name, body), encoding="utf-8"
    )


def _run(home, *args, **env):
    out = subprocess.run(
        [sys.executable, "-I", str(GEN), *args],
        env={**os.environ, "HOME": str(home), **env},
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def test_hides_unlisted_skills_and_keeps_base_used_and_explicitly_invoked(tmp_path):
    _skill(tmp_path, "launch-agent", "Then run Skill(chained-skill).")  # BASE; invokes another skill
    _skill(tmp_path, "ci-health")  # USED
    _skill(tmp_path, "agent-named")  # invoked by the agent file
    _skill(tmp_path, "chained-skill")  # invoked by a BASE skill
    _skill(tmp_path, "mentioned-only")  # plain mention is not an invocation
    _skill(tmp_path, "unrelated")
    agent = tmp_path / "agent.md"
    agent.write_text("Use Skill(agent-named). Maybe read mentioned-only later.\n", encoding="utf-8")
    brief = tmp_path / "brief.md"
    brief.write_text("MODE: x\nSkill({skill: \"brief-named\"})\n", encoding="utf-8")
    _skill(tmp_path, "brief-named")
    frag = _run(tmp_path, "--agent-file", str(agent), "--brief", str(brief))
    assert set(frag["skillOverrides"]) == {"mentioned-only", "unrelated"}
    assert set(frag["skillOverrides"].values()) == {"off"}
    assert frag["skillListingMaxDescChars"] == 160


def test_a_skill_with_a_different_frontmatter_name_is_kept_when_either_name_is_allowed(tmp_path):
    _skill(tmp_path, "recall", frontmatter_name="recall-from-frontmatter")  # BASE by directory name
    _skill(tmp_path, "other-dir", frontmatter_name="ci-health")  # USED by frontmatter name
    _skill(tmp_path, "gone")
    assert set(_run(tmp_path)["skillOverrides"]) == {"gone"}


def test_off_switch_and_description_cap_env(tmp_path):
    _skill(tmp_path, "unrelated")
    assert _run(tmp_path, PROXY_AGENT_SKILL_ALLOWLIST="0") == {}
    assert _run(tmp_path, PROXY_AGENT_SKILL_DESC_CHARS="0") == {"skillOverrides": {"unrelated": "off"}}
    assert _run(tmp_path, PROXY_AGENT_SKILL_DESC_CHARS="90")["skillListingMaxDescChars"] == 90
    assert "unrelated" not in _run(tmp_path, PROXY_AGENT_SKILL_ALLOW_EXTRA="unrelated")["skillOverrides"]


def test_launcher_merges_the_fragment_into_the_lane_settings_file_only_and_fails_open():
    inkling = LAUNCHER.index("inkling_skill_overrides_unavailable")
    branch = LAUNCHER.index('elif [[ "${PROXY_AGENT_SKILL_ALLOWLIST:-1}" != 0 ]]; then')
    assert inkling < branch  # Inkling keeps its own allowlist; every other lane takes the elif branch
    block = LAUNCHER[branch:LAUNCHER.index("# Persist a private runner", branch)]
    assert "proxy-skill-overrides.py" in block and '"$SAFETY_SETTINGS"' in block
    assert 'PROXY_AGENT_WARN reason=skill_overrides_unavailable' in block
    assert 'PROXY_AGENT_WARN reason=skill_overrides_merge_failed' in block
    assert ".claude/settings" not in block  # never touches the user or PM settings files
