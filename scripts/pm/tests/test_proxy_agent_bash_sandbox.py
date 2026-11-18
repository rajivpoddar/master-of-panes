"""Proxy Bash sandbox allowlist (added to the repo; not executed on the host).

Rajiv DM D0AMF0XE6TS thread 1795004502.133389, ts 1795006580.326659; layer 2 approved
("ok", ts 1795011133.027879) because agents deleted directories with Python shutil.
Proxy Bash runs inside Claude Code's sandbox: writes only to /tmp, the package caches
and the two ~/.claude state dirs; no repo root, no $HOME and no .git entry is writable.
The network is limited to the package and GitHub hosts. Only the remote-box scripts and
ssh run outside the sandbox ("git worktree *" was removed after the CTO REVISE on f203baec,
C0ALZJHGE49 thread 1795015806.949919, ts 1795015902.846089; Rajiv D0AMF0XE6TS ts 1795017876.764629
then exempted ssh and denies ssh back to this Mac).
The fence is default-ON again (PM live edit 21:17 IST, Rajiv "sandbox now", DM D0AMF0XE6TS ts
1795016785.315309; opt-out PROXY_AGENT_SANDBOX=0) and never applies to ops/state lanes.
The PM pane does not load this file.
"""
import re
from pathlib import Path

SRC = (Path(__file__).parents[1] / "shared-assets/claude/scripts/proxy-agent.sh").read_text()


def _list(name):
    match = re.search(name + r" = \[(.*?)\n\]", SRC, re.S)
    assert match, name + " list missing"
    body = "\n".join(line for line in match.group(1).splitlines() if not line.lstrip().startswith("#"))
    return re.findall(r'"([^"]+)"', body)


def test_sandbox_is_default_on_and_cannot_be_bypassed():
    assert '_fence_on = (os.environ.get("PROXY_AGENT_SANDBOX", "1") != "0") and not _ops_lane' in SRC
    assert 'PROXY_AGENT_SANDBOX="${PROXY_AGENT_SANDBOX:-1}"' in SRC
    assert '"enabled": _fence_on' in SRC
    assert '"failIfUnavailable": _fence_on' in SRC
    assert '"autoAllowBashIfSandboxed": True' in SRC
    assert '"allowUnsandboxedCommands": False' in SRC
    assert '"enableWeakerNetworkIsolation": True' in SRC
    assert "strictAllowlist" not in SRC


def test_only_the_named_paths_are_writable():
    assert _list("allow_write") == [
        "/tmp",
        "/private/tmp",
        "/Users/rajiv/.npm",
        "/Users/rajiv/.bun",
        "/Users/rajiv/.cache",
        "/Users/rajiv/.claude/state",
        "/Users/rajiv/.claude/projects",
        "/tmp/proxy-agent",
        "/tmp/agent-progress",
    ]
    for path in _list("allow_write"):
        assert not path.endswith("/.git") and "/.git/" not in path
        assert path not in ("/Users/rajiv", "/Users/rajiv/.heydonna")
        assert "Downloads/projects" not in path


def test_network_is_limited_to_package_and_github_hosts():
    assert _list("allowed_domains") == [
        "registry.npmjs.org",
        "registry.yarnpkg.com",
        "objects.githubusercontent.com",
        "github.com",
        "api.github.com",
        "codeload.github.com",
    ]


def test_only_remote_box_scripts_and_ssh_run_outside_the_sandbox():
    # Live contract (Rajiv D0AMF0XE6TS ts 1795017876.764629): ssh is exempt, and ssh back to this Mac is denied below.
    assert _list("excluded_commands") == [
        "~/.claude/scripts/remote-test-box.sh *",
        "/Users/rajiv/.claude/scripts/remote-test-box.sh *",
        "~/.claude/scripts/remote-browser-box.sh *",
        "/Users/rajiv/.claude/scripts/remote-browser-box.sh *",
        "ssh *",
    ]
    for command in _list("excluded_commands"):
        first = command.split()[0]
        assert first not in ("gh", "python", "python3", "node", "npm", "npx", "bash", "sh", "bun", "git", "rtk")


def test_ssh_back_to_this_mac_is_denied():
    deny = SRC.split("deny += [\"Bash(ssh *localhost*)\"", 1)[1].split("\nif os.environ.get(\"HELPER_READONLY\")", 1)[0]
    for target in ("localhost", "127.0.0.1", "heydonna.local", "192.168.68.111"):
        assert "ssh *" + target + "*" in deny


def test_runner_resolves_gh_token_at_run_time_and_never_writes_it():
    assert 'GH_TOKEN="$(gh auth token 2>/dev/null || true)"' in SRC
    assert "export GH_TOKEN=%q" not in SRC
    assert "printf 'export %s=%q\\n' \"$key\" \"${!key}\"" in SRC
    assert "GH_*" not in SRC.split("while IFS= read -r key; do", 1)[1].split("done < <(compgen -e)", 1)[0]
