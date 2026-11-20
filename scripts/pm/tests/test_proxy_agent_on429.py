"""Drive the real generated proxy runner; all model, account and tmux IO is stubbed."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


LAUNCHER = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts/proxy-agent.sh"


@pytest.mark.parametrize(
    "case,model,rc,attempts,switches,cooldown",
    [
        ("switched", "claude-sonnet-5-5", 0, 2, 1, False),
        ("after_tool", "claude-sonnet-5-5", 7, 1, 0, True),
        ("single_shot", "claude-sonnet-5-5", 7, 1, 0, True),
        ("single_shot_role", "claude-sonnet-5-5", 7, 1, 0, True),
        ("changed_pin_failed_restart", "claude-sonnet-5-5", 7, 1, 0, True),
        ("switch_failed", "claude-sonnet-5-5", 7, 1, 1, True),
        ("invalid_stream", "claude-sonnet-5-5", 7, 1, 0, True),
        ("already_verified", "claude-sonnet-5-5", 0, 2, 0, False),
        ("second_429", "claude-sonnet-5-5", 7, 2, 1, True),
        ("usage_limit", "claude-sonnet-5-5", 0, 2, 1, False),
        ("old_switcher", "claude-sonnet-5-5", 7, 1, 0, True),
        ("no_headroom", "claude-sonnet-5-5", 7, 1, 1, True),
        ("other_pool", "gpt-6.2-sol", 7, 1, 0, True),
        ("success_mentions_429", "claude-sonnet-5-5", 0, 1, 0, False),
        ("unrelated_failure", "claude-sonnet-5-5", 7, 1, 0, False),
    ],
)
def test_terminal_quota_replay(tmp_path, case, model, rc, attempts, switches, cooldown):
    home, bin_dir, checkout = (tmp_path / name for name in ("home", "bin", "checkout"))
    scripts, state, agents = (home / ".claude" / name for name in ("scripts", "state", "agents"))
    config = home / ".config/cliproxyapi"
    for directory in (bin_dir, checkout, scripts, state, agents, config):
        directory.mkdir(parents=True)
    (config / "client.env").write_text("CLIPROXY_API_KEY=fixture\n")
    (state / "proxy-caps.env").write_text("PROXY_AGENT_TOTAL_CAP=8\n")
    (state / "proxy-claude-account-pin").write_text("first@example.test\n")
    role = "remote-qa-runner" if case == "single_shot_role" else "claude"
    (agents / f"{role}.md").write_text("Fixture agent.\n")
    brief, out = tmp_path / "brief.md", tmp_path / "lane.md"
    brief.write_text("A byte-identical brief, including this line.\n")

    def executable(path, body, interpreter=sys.executable):
        path.write_text(f"#!{interpreter}\n{body}\n")
        path.chmod(0o700)

    executable(bin_dir / "git", '''case "$3" in
      rev-parse) printf '%s\\n' "$2";; branch) printf 'fixture\\n';; status) :;; *) exit 98;; esac''', "/bin/bash")
    executable(bin_dir / "tmux", '''import shlex, subprocess, sys
cmd = sys.argv[1]
if cmd in ('has-session', 'list-windows'):
    sys.exit(1)
if cmd == 'new-session':
    subprocess.run(shlex.split(sys.argv[-1]), stdout=subprocess.DEVNULL)
    print('@fixture')''')
    executable(bin_dir / "curl", "raise SystemExit('network is disabled in this fixture')")
    executable(bin_dir / "claude", '''import json, os, pathlib, sys
root = pathlib.Path(os.environ['TMPDIR'])
calls = root / 'calls'
first = not calls.exists()
with calls.open('a') as f:
    print(json.dumps([sys.argv[1:], sys.stdin.read()]), file=f)
case = os.environ['PROXY_AGENT_FIXTURE_CASE']
print(json.dumps(dict(type='system', subtype='init')))
if case == 'success_mentions_429' or not first and case != 'second_429':
    print(json.dumps(dict(type='result', subtype='success', result='DONE: 429 fixture verified')))
    sys.exit(0)
if case in ('changed_pin_failed_restart', 'already_verified'):
    (pathlib.Path.home() / '.claude/state/proxy-claude-account-pin').write_text('second@example.test\\n')
    if case == 'already_verified':
        (root / 'verified_restart').touch()
if case == 'after_tool':
    print(json.dumps(dict(type='assistant', message={'content': [{'type': 'tool_use', 'id': 'fixture-tool', 'name': 'Write'}]})))
if case == 'invalid_stream':
    print('{truncated record')
msg = {'usage_limit': "You've hit your limit · resets in 3m", 'unrelated_failure': 'Could not open issue 429'}
print(json.dumps(dict(type='result', is_error=True, result=msg.get(case, 'API Error: 429 quota reached. Resets in 3m'))))
sys.exit(7)''')
    executable(scripts / "cliproxy-claude-switch.py", '''import os, pathlib, sys
root = pathlib.Path(os.environ['TMPDIR'])
case = os.environ['PROXY_AGENT_FIXTURE_CASE']
if sys.argv[1:] == ['--help']:
    print('usage: switcher --auto' if case == 'old_switcher' else 'usage: switcher --on-429 --verify-pinned')
elif sys.argv[1:] == ['--verify-pinned=second@example.test']:
    (root / 'readbacks').touch()
    if not (root / 'verified_restart').is_file():
        sys.exit(1)
    print('VERIFIED claude pin')
else:
    assert sys.argv[1:] == ['--on-429']
    with (root / 'switches').open('a') as f:
        print('switch', file=f)
    if case == 'no_headroom':
        print('NO_ACCOUNT_WITH_HEADROOM')
    else:
        (pathlib.Path.home() / '.claude/state/proxy-claude-account-pin').write_text('second@example.test\\n')
        if case == 'switch_failed':
            sys.exit(1)
        (root / 'verified_restart').touch()
        print('SWITCHED claude account=second@example.test reload=yes')''')
    executable(scripts / "proxy-agent-stream.py", '''import json, pathlib, sys
raw = sys.stdin.read()
pathlib.Path(sys.argv[2]).write_text(raw)
pathlib.Path(sys.argv[1]).write_text(json.loads(raw.splitlines()[-1])['result'])''')
    executable(scripts / "proxy-agent-stop.sh", 'printf "%s\\n" "$2" >> "$TMPDIR/stops"', "/bin/bash")
    for helper in ("proxy-run-state", "proxy-assignment-notice"):
        (scripts / f"{helper}.py").write_text("raise SystemExit(0)\n")

    env = {"HOME": str(home), "PATH": f"{bin_dir}:{Path(sys.executable).parent}:{os.defpath}",
           "TMPDIR": str(tmp_path), "GH_TOKEN": "fixture", "PROXY_AGENT_NAME": "proxy-fixture",
           "PROXY_AGENT_FIXTURE_CASE": case, "PROXY_AGENT_NO_CONTINUE": "1" if case == "single_shot" else "0",
           "PROXY_AGENT_NO_SUBSCRIPTION": "1", "PROXY_AGENT_STUB_POOL_LIVE": "0",
           "PROXY_AGENT_STATE_DIR": str(tmp_path / "quota"), "PROXY_AGENT_OUT_DIR": str(tmp_path),
           "PROXY_BURN_GATE": "shadow"}
    run = subprocess.run(["bash", str(LAUNCHER), role, str(brief), "--cwd", str(checkout),
                          "--model", model, "--max-turns", "17", "--out", str(out)],
                         env=env, capture_output=True, text=True, timeout=30)
    assert run.returncode == rc, run.stderr
    calls = [json.loads(line) for line in (tmp_path / "calls").read_text().splitlines()]
    assert calls == [calls[0]] * attempts  # matching every CLI arg and the actual stdin brief
    assert (tmp_path / "stops").read_text().splitlines() == [str(rc)]
    switch_log = tmp_path / "switches"
    assert (len(switch_log.read_text().splitlines()) if switch_log.exists() else 0) == switches
    log = out.with_suffix(".log").read_text()
    assert log.count("PROXY_AGENT_429_RELAUNCH ") == attempts - 1
    assert (tmp_path / "readbacks").exists() == (attempts == 2 or case == "changed_pin_failed_restart")
    reasons = {"old_switcher": "on_429_flag_unavailable", "second_429": "retry_already_used",
               "after_tool": "prework_not_proven", "invalid_stream": "prework_not_proven",
               "single_shot": "single_shot", "single_shot_role": "single_shot",
               "changed_pin_failed_restart": "switch_readback_unverified", "switch_failed": "switch_failed"}
    if case in reasons:
        assert f"reason={reasons[case]}" in log
    if case == "after_tool":
        assert '"fixture-tool"' in out.with_suffix(".jsonl").read_text()
    path = tmp_path / "quota/quota.json"
    assert (model in json.loads(path.read_text())["exhausted_until"] if path.exists() else False) == cooldown
