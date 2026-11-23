"""Structured exit result (Rajiv D0AMF0XE6TS thread 1795436469.682079, ts 1795438159.967189): the RESULT_BEGIN..RESULT_END JSON block a lane prints
before its final DONE:/BLOCKED:/HANDOFF: line is parsed and validated by proxy-agent-notify.py, put in the PROXY_STOP message and stored on the run row.
Missing or invalid -> UNSTRUCTURED, head derived from git ls-remote (instead of "RESULT UNAVAILABLE reason=no_final_result_line").
Hermetic: temp HOME with copies of the scripts, recording pm-ops stub, local bare git remotes; nothing is posted anywhere.
Run: /opt/homebrew/bin/pytest -q -p no:cacheprovider tests/test_proxy_result_block.py
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location('notify_under_test', SCRIPTS / 'proxy-agent-notify.py')
notify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notify)
HEAD = '0123456789abcdef0123456789abcdef01234567'
NAME = 'proxy-hd-builder-14461t'
FENCE = chr(96) * 3


def block(**over):
    base = {'lane': NAME, 'type': 'hd-builder', 'issue': 14461, 'pr': 14470, 'head': HEAD, 'verdict': 'HANDOFF', 'proof_links': ['https://github.com/o/r/pull/14470'],
            'next_stage': 'hd-proof-qa', 'notes': 'Plan implemented.'}
    base.update(over)
    return base


def final_text(obj, marker='HANDOFF: pushed, ready for QA', before='All done.'):
    body = obj if isinstance(obj, str) else json.dumps(obj, indent=1)
    return f'{before}\nRESULT_BEGIN\n{body}\nRESULT_END\n{marker}\n'


# ---- validation -------------------------------------------------------------------------------------------------------------

def test_a_valid_block_is_accepted_and_normalised():
    clean, why = notify.validate_block(json.dumps(block(issue='#14461', pr=None, head=HEAD.upper(), verdict='done', unknown_key='dropped')), NAME)
    assert why == '' and clean['issue'] == 14461 and clean['pr'] is None and clean['head'] == HEAD and clean['verdict'] == 'DONE'
    assert set(clean) == set(notify.SCHEMA_KEYS) and clean['proof_links'] == ['https://github.com/o/r/pull/14470']


@pytest.mark.parametrize('over, reason', [
    ({'head': HEAD[:12]}, 'head_invalid'), ({'head': 'g' * 40}, 'head_invalid'), ({'head': None}, 'head_invalid'),
    ({'verdict': 'MAYBE'}, 'verdict_invalid'), ({'verdict': None}, 'verdict_invalid'),
    ({'lane': ''}, 'lane_invalid'), ({'type': 5}, 'type_invalid'),
    ({'issue': 'abc'}, 'issue_invalid'), ({'pr': True}, 'pr_invalid'), ({'pr': -3}, 'pr_invalid'),
    ({'proof_links': 'https://x'}, 'proof_links_invalid'), ({'proof_links': [1]}, 'proof_links_invalid'), ({'proof_links': ['u'] * 21}, 'proof_links_invalid'),
    ({'next_stage': ['a']}, 'next_stage_invalid'), ({'notes': 5}, 'notes_invalid'),
])
def test_an_invalid_block_is_refused_with_its_reason(over, reason):
    assert notify.validate_block(json.dumps(block(**over)), NAME) == (None, reason)


def test_missing_keys_and_non_json_and_non_objects_are_invalid():
    partial = {k: v for k, v in block().items() if k not in ('head', 'notes')}
    assert notify.validate_block(json.dumps(partial)) == (None, 'missing:head,notes')
    assert notify.validate_block('{not json') == (None, 'json_parse')
    assert notify.validate_block('[1, 2]') == (None, 'not_object')


def test_secrets_are_redacted_and_a_lane_name_mismatch_is_normalised_not_fatal():
    clean, why = notify.validate_block(json.dumps(block(lane='some-other-name', notes='token=abc123secret and Bearer xyz.789')), NAME)
    assert why == '' and clean['lane'] == NAME and clean['lane_declared'] == 'some-other-name'
    assert 'abc123secret' not in clean['notes'] and 'xyz.789' not in clean['notes'] and '[REDACTED]' in clean['notes']


def test_the_last_block_wins_and_code_fences_inside_it_are_tolerated():
    text = final_text(block(verdict='BLOCKED')) + final_text(FENCE + 'json\n' + json.dumps(block(verdict='DONE')) + '\n' + FENCE, marker='DONE: ok')
    clean, why = notify.validate_block(notify.extract_block(text), NAME)
    assert why == '' and clean['verdict'] == 'DONE'
    assert notify.extract_block('no block here\nDONE: x') is None
    assert notify.extract_block('RESULT_BEGIN\n{}\n') is None  # an unterminated block is not a block


# ---- the result command ------------------------------------------------------------------------------------------------------

def run_result(tmp, text, *, brief='ISSUE: 14461\nPR: 14470\n', cwd=None, env=None, fmt='json'):
    out = tmp / 'out.md'
    out.write_text(text)
    bf = tmp / 'brief.md'
    bf.write_text(brief)
    done = subprocess.run([sys.executable, '-I', str(SCRIPTS / 'proxy-agent-notify.py'), 'result', '--name', NAME, '--type', 'hd-builder', '--brief', str(bf),
                           '--out', str(out), '--jsonl', str(tmp / 'none.jsonl'), '--cwd', str(cwd or tmp), '--format', fmt],
                          capture_output=True, text=True, env={**os.environ, **(env or {})})
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout) if fmt == 'json' else done.stdout


def test_a_structured_result_is_reported_with_the_lanes_own_head(tmp_path):
    got = run_result(tmp_path, final_text(block()))
    assert got['status'] == 'STRUCTURED' and got['head'] == HEAD and got['verdict'] == 'HANDOFF' and got['reason'] == ''
    assert json.loads(got['message_json'])['next_stage'] == 'hd-proof-qa' and '\n' not in got['message_json']


def git(*a, cwd=None):
    return subprocess.run(['git', '-c', 'user.name=T', '-c', 'user.email=t@example.invalid', *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def remote_with_pr_head(tmp_path):
    origin = tmp_path / 'origin.git'
    git('init', '-q', '--bare', str(origin))
    clone = tmp_path / 'clone'
    git('init', '-q', '-b', 'main', str(clone))
    git('remote', 'add', 'origin', str(origin), cwd=clone)
    (clone / 'f').write_text('x')
    git('add', 'f', cwd=clone)
    git('commit', '-q', '-m', 'c', cwd=clone)
    git('push', '-q', 'origin', 'HEAD:refs/pull/14470/head', cwd=clone)
    return clone, git('rev-parse', 'HEAD', cwd=clone)


@pytest.mark.parametrize('text, reason', [
    ('Did the work.\nDONE: finished', 'no_result_block'),
    (final_text(block(head='deadbeef')), 'invalid:head_invalid'),
    (final_text('{broken', marker='DONE: x'), 'invalid:json_parse'),
])
def test_a_missing_or_invalid_block_is_unstructured_and_the_head_comes_from_git_ls_remote(tmp_path, remote_with_pr_head, text, reason):
    clone, sha = remote_with_pr_head
    got = run_result(tmp_path, text, cwd=clone)
    assert got['status'] == 'UNSTRUCTURED' and got['reason'] == reason and got['block'] is None
    assert got['head'] == sha  # refs/pull/14470/head on the remote, not anything the lane wrote


def test_unstructured_with_no_pr_and_no_observed_push_has_an_empty_head_never_an_invented_one(tmp_path):
    got = run_result(tmp_path, 'Prose only, mentions ' + HEAD + ' in passing.\n', brief='ISSUE: 14461\n')
    assert got['status'] == 'UNSTRUCTURED' and got['head'] == ''  # a sha in the lane's own prose is not evidence


def test_the_shell_format_is_only_quoted_assignments(tmp_path):
    out = run_result(tmp_path, final_text(block(notes="it's 'quoted' $(danger) " + chr(96) + 'x' + chr(96))), fmt='shell')
    assert all(line.startswith('RESULT_') and '=' in line for line in out.splitlines())
    probe = subprocess.run(['bash', '-c', out + '\nprintf "%s|%s" "$RESULT_STATUS" "$RESULT_VERDICT"'], capture_output=True, text=True)
    assert probe.stdout == 'STRUCTURED|HANDOFF' and probe.stderr == ''  # evaluating it executes nothing


# ---- the stop script: PROXY_STOP message and the run row ---------------------------------------------------------------------

@pytest.fixture
def world(tmp_path):
    home = tmp_path / 'home'
    scripts = home / '.claude' / 'scripts'
    scripts.mkdir(parents=True)
    for name in ('proxy-agent-notify.py', 'proxy-run-state.py', 'proxy-agent-stop.sh'):
        shutil.copy(SCRIPTS / name, scripts / name)
    (scripts / 'message-pm.sh').write_text('#!/bin/bash\nprintf "%s\\n" "$2" >> "$HOME/messages"\nprintf "MESSAGE_PM_SENT verified=true\\n"\n')
    (scripts / 'message-pm.sh').chmod(0o755)
    (tmp_path / 'stub.py').write_text('import json, os, sys\nopen(os.environ["STUB_LOG"], "a").write(json.dumps(sys.argv[1:]) + "\\n")\n')
    (tmp_path / 'repo').mkdir()
    subprocess.run(['git', 'init', '-q', str(tmp_path / 'repo')], check=True)
    env = {**os.environ, 'HOME': str(home), 'PROXY_AGENT_PM_OPS_CLI': str(tmp_path / 'stub.py'), 'STUB_LOG': str(tmp_path / 'calls.jsonl'),
           'PROXY_AGENT_RUN_STATE_DIR': str(tmp_path / 'runs'), 'PROXY_AGENT_COMBINED_LOG': str(tmp_path / 'all.log'),
           'PROXY_AGENT_STOP_JSONL': str(tmp_path / 'x.jsonl'), 'PROXY_AGENT_STOP_TYPE': 'hd-builder', 'PROXY_AGENT_STOP_MODEL': 'claude-sonnet-5-5'}
    for k in ('PROXY_AGENT_RUN_NAME', 'PROXY_AGENT_RUN_TOKEN'):
        env.pop(k, None)
    return tmp_path, home, env


def stop(world, text, *, rc='0', brief='ISSUE: 14461\n', start=False):
    tmp, home, env = world
    out = tmp / 'out.md'
    if text is not None:
        out.write_text(text)
    (tmp / 'brief.md').write_text(brief)
    env = {**env, 'PROXY_AGENT_STOP_BRIEF': str(tmp / 'brief.md')}
    if start:
        run = [sys.executable, '-I', str(SCRIPTS / 'proxy-run-state.py')]
        subprocess.run([*run, 'acquire', '--name', NAME, '--token', 't1'], env=env, check=True)
        subprocess.run([*run, 'start', '--name', NAME, '--token', 't1', '--type', 'hd-builder', '--brief', str(tmp / 'brief.md'), '--cwd', str(tmp / 'repo'),
                        '--out', str(out), '--model', 'claude-sonnet-5-5', '--pm-role', '0'], env=env, check=True)
        env = {**env, 'PROXY_AGENT_RUN_NAME': NAME, 'PROXY_AGENT_RUN_TOKEN': 't1'}
    subprocess.run(['bash', str(SCRIPTS / 'proxy-agent-stop.sh'), NAME, rc, str(out), str(tmp / 'receipt')], env=env, check=True, cwd=str(tmp))
    return (home / 'messages').read_text().splitlines()[-1]


def finish_row(tmp):
    rows = [json.loads(x) for x in (tmp / 'calls.jsonl').read_text().splitlines()]
    last = [r for r in rows if r[0] == 'obligation-upsert' and '--kind' in r and r[r.index('--kind') + 1] == 'proxy_run'][-1]
    return json.loads(last[last.index('--evidence-json') + 1])


def test_the_proxy_stop_message_carries_the_validated_block_and_the_row_stores_it(world):
    msg = stop(world, final_text(block(), marker='DONE: pushed, ready for QA'), start=True)
    assert msg.startswith(f'PROXY_STOP name={NAME} rc=0 ') and 'RESULT DONE: pushed, ready for QA' in msg
    assert f'result=STRUCTURED verdict=HANDOFF head={HEAD} RESULT_JSON {{' in msg and '"next_stage":"hd-proof-qa"' in msg
    ev = finish_row(world[0])
    assert ev['state'] == 'done' and ev['result_status'] == 'STRUCTURED' and ev['head'] == HEAD and ev['result_block'] == block()


def test_a_handoff_lane_with_a_valid_block_is_a_structured_result_even_though_the_legacy_line_matcher_ignores_handoff(world):
    msg = stop(world, final_text(block()), start=True)  # final marker is HANDOFF: the legacy RESULT/DONE/BLOCKED/VERDICT matcher does not see it
    assert 'RESULT STRUCTURED' in msg and 'verdict=HANDOFF' in msg and f'head={HEAD}' in msg and 'RESULT UNAVAILABLE' not in msg
    assert finish_row(world[0])['state'] == 'done'


def test_a_lane_with_no_marker_line_but_a_valid_block_still_ends_as_a_result(world):
    msg = stop(world, final_text(block(verdict='DONE'), marker=''), start=True)
    assert 'RESULT STRUCTURED' in msg and 'RESULT UNAVAILABLE' not in msg and finish_row(world[0])['state'] == 'done'


def test_no_block_falls_back_to_todays_result_line_and_marks_it_unstructured(world):
    msg = stop(world, 'Worked.\nDONE: finished the thing', start=True)
    assert 'RESULT DONE: finished the thing' in msg and 'result=UNSTRUCTURED head=UNKNOWN reason=no_result_block' in msg
    ev = finish_row(world[0])
    assert ev['result_status'] == 'UNSTRUCTURED' and ev['state'] == 'done' and 'result_block' not in ev  # the legacy terminal status is unchanged


def test_no_block_and_no_final_line_is_unstructured_not_result_unavailable_and_stays_non_terminal(world):
    msg = stop(world, 'I ran out of things to say.\n', rc='0', start=True)
    assert 'RESULT UNAVAILABLE' not in msg and 'RESULT UNSTRUCTURED head=UNKNOWN reason=no_result_block' in msg
    ev = finish_row(world[0])
    assert ev['state'] == 'failed' and ev['result_status'] == 'UNSTRUCTURED' and ev['result'].startswith('RESULT UNSTRUCTURED')  # same status as before: not terminal


def test_an_invalid_block_names_why(world):
    msg = stop(world, final_text(block(head='abc'), marker='DONE: x'))
    assert 'result=UNSTRUCTURED' in msg and 'reason=invalid:head_invalid' in msg


def test_a_missing_output_file_keeps_the_existing_unavailable_text(world):
    assert stop(world, None).endswith('RESULT UNAVAILABLE reason=output_missing')


def test_old_run_scripts_without_the_new_env_still_stop_cleanly(world):
    tmp, home, env = world
    env.pop('PROXY_AGENT_STOP_TYPE', None)
    env.pop('PROXY_AGENT_STOP_BRIEF', None)
    (tmp / 'out.md').write_text('DONE: old lane\n')
    done = subprocess.run(['bash', str(SCRIPTS / 'proxy-agent-stop.sh'), NAME, '0', str(tmp / 'out.md'), str(tmp / 'receipt')], env=env, cwd=str(tmp))
    assert done.returncode == 0 and 'RESULT DONE: old lane' in (home / 'messages').read_text()


def test_the_launcher_adds_the_result_block_instruction_to_every_lane_preamble():
    launcher = (SCRIPTS / 'proxy-agent.sh').read_text()
    assert 'RESULT BLOCK (required): immediately BEFORE your final DONE:/BLOCKED:/HANDOFF: line' in launcher
    assert 'RESULT_BEGIN' in launcher and 'RESULT_END' in launcher and '"verdict": "<DONE|BLOCKED|FAILED|HANDOFF>"' in launcher
