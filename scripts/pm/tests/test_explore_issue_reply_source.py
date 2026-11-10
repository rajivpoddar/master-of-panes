"""Reply-scoped issue intake: exercise shipped helpers and the real audit hook.

No live services, customer content, or real obligations. These regressions are
committed but not executed under the MoP no-test-proof directive.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
HOOKS = ROOT / "scripts/pm/shared-assets/claude/hooks"
HELPER = HOOKS / "explore-issue-latch-guard.sh"
HOOK = HOOKS / "pre-issue-create-audit.sh"
CHANNEL = "C0AGWPQFKHA"
THREAD = "1793975640.657599"
REPLY = "1794296583.975299"
ROOT_KEY = f"{CHANNEL}__{THREAD.replace('.', '_')}"
REPLY_KEY = f"{ROOT_KEY}__{REPLY.replace('.', '_')}"


@pytest.fixture
def intake(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    root = state / f"{ROOT_KEY}.json"
    root.write_text(json.dumps({"channel": CHANNEL, "thread_ts": THREAD,
                                "state": "RESOLVED_FILED", "issue_number": 12977}))
    cache = tmp_path / "slack"
    cache.mkdir()
    # Internal opener, customer reply: classification must use the reply.
    (cache / f"{THREAD}.json").write_text(json.dumps({
        "ok": True, "channel": CHANNEL, "messages": [
            {"ts": THREAD, "user": "U0ALEAYCAUT"},
            {"ts": REPLY, "thread_ts": THREAD, "user": "U0CUSTOMER01"},
        ]}))
    recorder = tmp_path / "pm-ops"
    recorder.write_text("#!/usr/bin/env python3\nimport json,os,sys\n"
                        "with open(os.environ['CALLS'], 'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n"
                        "print(4242)\n")
    recorder.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text("#!/bin/sh\nexit 99\n")
    curl.chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}",
               EXPLORE_ISSUE_STATE_DIR=str(state),
               EXPLORE_ISSUE_PERMIT_DIR=str(tmp_path / "permits"),
               EXPLORE_ISSUE_CONSUMED_PERMIT_DIR=str(tmp_path / "consumed"),
               EXPLORE_ISSUE_SLACK_CACHE_DIR=str(cache),
               EXPLORE_ISSUE_PM_OPS_CLI=str(recorder),
               EXPLORE_ISSUE_LATCH_GUARD=str(HELPER),
               EXPLORE_ISSUE_ENV_LOCAL=str(tmp_path / "no-credentials"),
               SLACK_BOT_TOKEN="", CALLS=str(tmp_path / "calls.jsonl"),
               EXPLORE_ISSUE_SOURCE_MESSAGE_TS=REPLY)
    return tmp_path, root, env


def shell(env, command):
    return subprocess.run(["bash", "-c", f"set -euo pipefail; source {shlex.quote(str(HELPER))}; {command}"],
                          env=env, text=True, capture_output=True, timeout=30)


def write_state(env, state="OPEN"):
    return shell(env, f'explore_issue_write_state {CHANNEL} {THREAD} {state} /tmp/synthetic.md title reason now original current 4242')


def calls(env):
    path = Path(env["CALLS"])
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def run_hook(tmp_path, env, reply=REPLY):
    body = tmp_path / "body.md"
    body.write_text(f"Synthetic report.\nSource: https://scribiers.slack.com/archives/{CHANNEL}/p{reply.replace('.', '')}?thread_ts={THREAD}&cid={CHANNEL}\n")
    payload = {"tool_input": {"command": f'gh issue create --title "synthetic" --body-file {body}'}}
    return subprocess.run(["bash", str(HOOK)], input=json.dumps(payload), env=env,
                          text=True, capture_output=True, timeout=30)


@pytest.mark.parametrize("terminal", ["RESOLVED_FILED", "RESOLVED_NO_ISSUE"])
def test_terminal_root_routes_new_reply_and_preserves_root(intake, terminal):
    tmp_path, root, env = intake
    data = json.loads(root.read_text())
    data["state"] = terminal
    root.write_text(json.dumps(data))
    before = root.read_bytes()
    result = write_state(env)
    assert result.returncode == 0, result.stderr
    child = json.loads((tmp_path / "state" / f"{REPLY_KEY}.json").read_text())
    assert child["thread_ts"] == THREAD
    assert child["source_message_ts"] == REPLY
    assert f"/p{REPLY.replace('.', '')}?thread_ts={THREAD}" in child["source_permalink"]
    assert root.read_bytes() == before


def test_open_root_retains_legacy_key(intake):
    tmp_path, root, env = intake
    root.write_text(json.dumps({"state": "OPEN", "thread_ts": THREAD}))
    assert write_state(env).returncode == 0
    assert json.loads(root.read_text())["source_message_ts"] == REPLY
    assert not (tmp_path / "state" / f"{REPLY_KEY}.json").exists()


def test_original_source_replay_does_not_create_child(intake):
    tmp_path, root, env = intake
    data = json.loads(root.read_text())
    data["source_message_ts"] = REPLY
    root.write_text(json.dumps(data))
    before = root.read_bytes()
    assert write_state(env).returncode != 0
    assert root.read_bytes() == before
    assert not (tmp_path / "state" / f"{REPLY_KEY}.json").exists()


@pytest.mark.parametrize("operation", [
    f"explore_issue_set_state {CHANNEL} {THREAD} IN_PROGRESS",
    f"explore_issue_write_state {CHANNEL} {THREAD} OPEN body title reason now old new 1",
])
def test_terminal_root_is_immutable_without_reply(intake, operation):
    _, root, env = intake
    env["EXPLORE_ISSUE_SOURCE_MESSAGE_TS"] = ""
    before = root.read_bytes()
    assert shell(env, operation).returncode != 0
    assert root.read_bytes() == before


def test_hook_uses_verified_reply_and_separate_obligation(intake):
    tmp_path, root, env = intake
    before = root.read_bytes()
    result = run_hook(tmp_path, env)
    assert result.returncode == 0, result.stderr
    decision = json.loads(result.stdout)
    assert decision["decision"] == "block"
    assert REPLY_KEY in decision["message"]
    assert root.read_bytes() == before
    child = json.loads((tmp_path / "state" / f"{REPLY_KEY}.json").read_text())
    assert child["state"] == "OPEN"
    assert child["source_message_ts"] == REPLY
    invocation, = calls(env)
    assert invocation[invocation.index("--target-id") + 1] == f"{CHANNEL}:{THREAD}:{REPLY}"
    assert invocation[invocation.index("--dedupe-group") + 1] == f"explore-issue:{CHANNEL}:{THREAD}:{REPLY}"
    evidence = json.loads(invocation[invocation.index("--evidence-json") + 1])
    assert evidence["thread_ts"] == THREAD and evidence["source_message_ts"] == REPLY


def test_reply_permit_is_one_use_and_not_another_replys_permit(intake):
    tmp_path, root, env = intake
    before = root.read_bytes()
    assert write_state(env).returncode == 0
    assert shell(env, f"explore_issue_mint_permit {CHANNEL} {THREAD} sha title 4242").returncode == 0
    other = dict(env, EXPLORE_ISSUE_SOURCE_MESSAGE_TS="1794296584.975299")
    assert shell(other, f"explore_issue_consume_permit {CHANNEL} {THREAD} sha title").returncode != 0
    assert shell(env, f"explore_issue_consume_permit {CHANNEL} {THREAD} sha title").returncode == 0
    assert shell(env, f"explore_issue_consume_permit {CHANNEL} {THREAD} sha title").returncode != 0
    assert len(list((tmp_path / "consumed").glob(f"{REPLY_KEY}.*.json"))) == 1
    assert root.read_bytes() == before


@pytest.mark.parametrize("resolver", ["filed", "no_issue"])
def test_resolution_targets_child_and_is_idempotent(intake, resolver):
    tmp_path, root, env = intake
    before = root.read_bytes()
    assert write_state(env).returncode == 0
    # Explicit optional argument works even without an exported selector.
    env["EXPLORE_ISSUE_SOURCE_MESSAGE_TS"] = ""
    command = (f"explore_issue_resolve_filed {CHANNEL} {THREAD} 999 https://example.invalid/999 title sha {REPLY}"
               if resolver == "filed" else
               f"explore_issue_resolve_no_issue {CHANNEL} {THREAD} fixed_directly_on_main title sha {REPLY}")
    assert shell(env, command).returncode == 0
    assert shell(env, command).returncode == 0
    invocation, = calls(env)
    assert invocation[invocation.index("--target-id") + 1] == f"{CHANNEL}:{THREAD}:{REPLY}"
    child = json.loads((tmp_path / "state" / f"{REPLY_KEY}.json").read_text())
    assert child["state"] == ("RESOLVED_FILED" if resolver == "filed" else "RESOLVED_NO_ISSUE")
    assert root.read_bytes() == before


def test_unverified_reply_writes_nothing(intake):
    tmp_path, root, env = intake
    before = root.read_bytes()
    result = run_hook(tmp_path, env, reply="1794296584.975299")
    assert result.returncode == 0
    assert "could not be verified" in json.loads(result.stdout)["message"]
    assert calls(env) == []
    assert root.read_bytes() == before
    assert not (tmp_path / "state" / f"{REPLY_KEY}.json").exists()


def test_terminal_reply_does_not_reopen(intake):
    tmp_path, root, env = intake
    assert write_state(env, "RESOLVED_NO_ISSUE").returncode == 0
    child = tmp_path / "state" / f"{REPLY_KEY}.json"
    before = child.read_bytes()
    assert write_state(env).returncode != 0
    assert child.read_bytes() == before
