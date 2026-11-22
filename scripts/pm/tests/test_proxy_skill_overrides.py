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
    local_model = LAUNCHER.index("local_model_skill_overrides_unavailable")
    branch = LAUNCHER.index('elif [[ "${PROXY_AGENT_SKILL_ALLOWLIST:-1}" != 0 ]]; then')
    assert local_model < branch  # local Spark lanes keep their own allowlist; every other lane takes the elif branch
    block = LAUNCHER[branch:LAUNCHER.index("# Persist a private runner", branch)]
    assert "proxy-skill-overrides.py" in block and '"$SAFETY_SETTINGS"' in block
    assert 'PROXY_AGENT_WARN reason=skill_overrides_unavailable' in block
    assert 'PROXY_AGENT_WARN reason=skill_overrides_merge_failed' in block
    assert ".claude/settings" not in block  # never touches the user or PM settings files


def _run_launcher_block(tmp_path, settings, fault):
    """Run the REAL skill-overrides block of proxy-agent.sh under /bin/bash (3.2 on this Mac) with set -euo pipefail."""
    start = LAUNCHER.index('elif [[ "${PROXY_AGENT_SKILL_ALLOWLIST:-1}" != 0 ]]; then')
    block = LAUNCHER[start:LAUNCHER.index("\nfi\n", start) + 4]
    scripts = tmp_path / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "proxy-skill-overrides.py").write_text(GEN.read_text())
    for n in range(200):  # enough skills that the merged settings exceed the 4096-byte write limit used below
        _skill(tmp_path, "unlisted-skill-%d" % n)
    agent = tmp_path / "agent.md"
    agent.write_text("x\n")
    program = 'if [[ "${MODEL_ID:-}" == inkling-2-mini ]]; then :\n' + block
    return subprocess.run(
        ["/bin/bash", "-c", "set -euo pipefail\n" + ("ulimit -f 4\n" if fault else "") + 'eval "$PROG"; echo block-exit=0'],
        env={**os.environ, "HOME": str(tmp_path), "PROG": program, "MODEL_ID": "gpt-6.1-terra", "PM_ROLE": "1",
             "CWD": str(tmp_path), "AGENT_FILE": str(agent), "PROMPT_FILE": str(agent), "SAFETY_SETTINGS": str(settings)},
        capture_output=True, text=True, check=False,
    )


def _safety_settings(path):
    path.write_text(json.dumps({
        "sandbox": {"enabled": True, "failIfUnavailable": True},
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]}]},
    }))
    path.chmod(0o600)


def test_settings_merge_is_atomic_a_partial_write_keeps_the_original_byte_identical(tmp_path):
    """CTO REVISE on 20ec646 (C0ALZJHGE49 thread 1795312317.789329, ts 1795312540.092029). A real partial write (RLIMIT_FSIZE)."""
    settings = tmp_path / "proxy-agent-safety.test"
    _safety_settings(settings)
    original = settings.read_bytes()
    res = _run_launcher_block(tmp_path, settings, fault=True)
    assert "block-exit=0" in res.stdout  # fail-open: the launch continues
    assert "PROXY_AGENT_WARN reason=skill_overrides_merge_failed" in res.stderr
    assert settings.read_bytes() == original
    assert not list(tmp_path.glob("proxy-agent-safety.test.*.tmp"))  # the private temp file is removed


def test_settings_merge_keeps_sandbox_and_hooks_and_adds_the_overrides(tmp_path):
    settings = tmp_path / "proxy-agent-safety.test"
    _safety_settings(settings)
    before = json.loads(settings.read_text())
    assert "block-exit=0" in _run_launcher_block(tmp_path, settings, fault=False).stdout
    after = json.loads(settings.read_text())
    assert {k: after[k] for k in before} == before
    assert after["skillOverrides"] and after["skillListingMaxDescChars"] == 160
    assert (settings.stat().st_mode & 0o777) == 0o600
    assert not list(tmp_path.glob("proxy-agent-safety.test.*.tmp"))


def test_launcher_never_opens_the_settings_file_for_writing_in_place():
    block = LAUNCHER[LAUNCHER.index('elif [[ "${PROXY_AGENT_SKILL_ALLOWLIST:-1}" != 0 ]]; then'):LAUNCHER.index("# Persist a private runner")]
    assert 'open(p, "w")' not in block and "os.replace(tmp, p)" in block and "os.fsync" in block
