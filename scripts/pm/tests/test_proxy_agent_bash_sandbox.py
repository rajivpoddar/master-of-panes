"""Proxy Bash sandbox allowlist (added to the repo; not executed on the host).

Rajiv DM D0AMF0XE6TS thread 1795004502.133389, ts 1795006580.326659: proxy agents
run inside Claude Code's Bash sandbox. Writes stay in the worktree, /tmp, scratch
and the named QA/cache dirs. ~/.heydonna itself is not writable, so a script cannot
delete sibling dirs the way disk1716 did. The PM pane does not load this file.
"""
import re
from pathlib import Path

SRC = (Path(__file__).parents[1] / "shared-assets/claude/scripts/proxy-agent.sh").read_text()


def _allow_write():
    match = re.search(r"allow_write = \[(.*?)\n\]", SRC, re.S)
    assert match, "allow_write list missing"
    return re.findall(r'"([^"]+)"', match.group(1))


def test_sandbox_is_on_and_cannot_be_bypassed():
    assert '"enabled": True' in SRC
    assert '"failIfUnavailable": True' in SRC
    assert '"autoAllowBashIfSandboxed": True' in SRC
    assert '"allowUnsandboxedCommands": False' in SRC
    assert "allowedDomains" not in SRC.split("allow_write = [", 1)[1].split("PY\n", 1)[0]


def test_home_and_heydonna_root_are_not_writable():
    allow = _allow_write()
    assert "/Users/rajiv" not in allow
    assert "/Users/rajiv/.heydonna" not in allow
    assert "/tmp" in allow
    assert "/private/tmp" in allow
    assert "/private/var/folders" in allow
    assert "/Users/rajiv/Downloads/projects/heydonna-app-proxy" in allow
    assert "/Users/rajiv/Downloads/projects/heydonna-app" in allow
    assert "/Users/rajiv/.heydonna/qa-*" in allow
    assert "/Users/rajiv/.heydonna/qa-*/**" in allow
    assert "/Users/rajiv/.claude/state" in allow
    assert "/Users/rajiv/.claude/projects" in allow
    assert "/Users/rajiv/.cache" in allow
    assert "/Users/rajiv/.npm" in allow
    assert "/Users/rajiv/.bun" in allow
    assert "/Users/rajiv/Library/Caches" in allow
