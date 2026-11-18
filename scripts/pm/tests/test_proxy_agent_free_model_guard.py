"""Contract tests for the free-model guard in proxy-agent.sh (added to the repo; not run on the host).

Rajiv DM D0AMF0XE6TS ts 1794984605.382609 ("yes"): block free/trial OpenCode models (hy4-flash-free, muse-spark-1.3-contributor)
for agents that read customer content or write prod. A refused tier model falls back to the next tier model and logs
"REFUSED free-model guard"; a raw primary has no tier and is refused. Everything else in the routing is unchanged.
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / 'shared-assets/claude/scripts/proxy-agent.sh'
SRC = SCRIPT.read_text()
HY4 = 'hy4-flash-free'
MUSE = 'muse-spark-1.3-contributor'
DEEPSEEK = 'deepseek-v4.1-flash'


def _slice(start, end):
    first = SRC.index(start)
    return SRC[first:SRC.index(end, first)]


PICKER = _slice('# OpenCode Go pool: hy4-flash-free', '# Ordinary burn gate for capped roles')
GUARD = _slice('proxy_agent_primary_pool_guard() {', '# Primary burn check (CTO REVISE')

STUBS = r'''
PROXY_AGENT_CLAUDE_CAP=5
STUB_COOLED="${STUB_COOLED:-}"
proxy_agent_is_capped() { return 0; }
proxy_agent_live_count() { echo 0; }
proxy_agent_burn_enforced() { return 0; }
proxy_agent_burn_refused() { return 1; }
proxy_agent_pool_blocked() { return 1; }
proxy_agent_sub_usable() { case " $STUB_COOLED " in *" $1 "*) return 1;; esac; return 0; }
resolve_model() { echo "$1 200000 32000"; }
'''


def _all_routes():
    tiers = re.findall(r'^\s+([A-Z0-9]+)\) models="([^"]*)";;', PICKER, re.M)
    return sorted({m for _, models in tiers for m in models.split()})


def _run(tmp_path, body, brief=None, cooled=''):
    brief_path = ''
    if brief is not None:
        brief_file = tmp_path / 'brief.md'
        brief_file.write_text(brief)
        brief_path = str(brief_file)
    script = f'set -euo pipefail\nROLE_BRIEF={brief_path!r}\n' + STUBS + PICKER + GUARD + body
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(tmp_path), 'STUB_COOLED': cooled}
    return subprocess.run(['bash', '-c', script], capture_output=True, text=True, env=env)


def _pick(tmp_path, agent_type, tier, brief=None, cooled=''):
    body = (f'TYPE={agent_type}\nproxy_agent_pick_tier {tier} && rc=0 || rc=$?\n'
            'printf "rc=%s model=%s\\n" "$rc" "${PICK_MODEL-}"\n')
    result = _run(tmp_path, body, brief, cooled)
    assert result.returncode == 0, result.stderr
    rc, model = (part.split('=', 1)[1] for part in result.stdout.strip().splitlines()[-1].split(' ', 1))
    return rc, model, result.stderr


def _primary(tmp_path, agent_type, model, brief=None):
    body = f'TYPE={agent_type}\nMODEL={model}\nproxy_agent_primary_pool_guard && rc=0 || rc=$?\nprintf "rc=%s\\n" "$rc"\n'
    result = _run(tmp_path, body, brief)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1], result.stderr


def test_only_the_two_approved_ids_are_free_trial_ids():
    assert re.search(r'proxy_agent_is_free_trial_id\(\) \{\n\s+case "\$1" in hy4-flash-free\|muse-spark-1\.3-contributor\) return 0;;', SRC)


@pytest.mark.parametrize('agent_type', (
    'customer-artifact-investigator', 'hd-investigator', 'sync-manager-specialist', 'pm-lock-release',
    'prod-admin-retry', 'prod-modal-deploy', 'editor-lockout-specialist'))
def test_guarded_types_skip_hy4_and_fall_back_to_the_next_tier_model(tmp_path, agent_type):
    rc, model, stderr = _pick(tmp_path, agent_type, 'LIGHT', cooled='claude-haiku-5-5')
    assert (rc, model) == ('0', 'grok-4.7')
    assert f'REFUSED free-model guard model={HY4}' in stderr


def test_unguarded_launch_keeps_the_existing_routing(tmp_path):
    rc, model, stderr = _pick(tmp_path, 'hd-builder', 'LIGHT', brief='Fix the sidebar spacing.\n', cooled='claude-haiku-5-5')
    assert (rc, model) == ('0', HY4)
    assert 'REFUSED free-model guard' not in stderr
    # deepseek-v4.1-flash is outside the approved decision and stays routable even for a guarded type.
    rc, model, _ = _pick(tmp_path, 'customer-artifact-investigator', 'MID', cooled='claude-sonnet-5-5')
    assert (rc, model) == ('0', DEEPSEEK)


@pytest.mark.parametrize('text', (
    'Read the beloved-cardinal-941 deployment.', 'Write to prod Convex.', 'Check production data.', 'Reply to the customer.',
    'Customers lost saves.'))
def test_brief_text_naming_prod_or_customer_triggers_the_guard(tmp_path, text):
    rc, model, stderr = _pick(tmp_path, 'hd-builder', 'LIGHT', brief=text + '\n', cooled='claude-haiku-5-5')
    assert (rc, model) == ('0', 'grok-4.7')
    assert 'REFUSED free-model guard' in stderr


@pytest.mark.parametrize('text', ('Update the product copy.', 'Reproduce the layout bug.', 'Improve the producer queue.'))
def test_prod_inside_another_word_does_not_trigger_the_guard(tmp_path, text):
    rc, model, _ = _pick(tmp_path, 'hd-builder', 'LIGHT', brief=text + '\n', cooled='claude-haiku-5-5')
    assert (rc, model) == ('0', HY4)


def test_muse_is_refused_for_a_guarded_launch_even_when_every_other_route_is_exhausted(tmp_path):
    cooled = ' '.join(r for r in _all_routes() if r != MUSE)
    rc, model, stderr = _pick(tmp_path, 'hd-investigator', 'PLAN', cooled=cooled)
    assert (rc, model) == ('75', '')
    assert f'REFUSED free-model guard model={MUSE}' in stderr
    # The same exhaustion still lets an unguarded launch reach the emergency entry (existing behaviour).
    rc, model, _ = _pick(tmp_path, 'hd-builder', 'PLAN', brief='Fix the sidebar spacing.\n', cooled=cooled)
    assert (rc, model) == ('0', MUSE)


def test_remote_qa_runner_is_guarded_while_capturing_on_miss_and_not_under_strict_replay(tmp_path):
    rc, model, stderr = _pick(tmp_path, 'remote-qa-runner', 'LIGHT', brief='Run the QA scenario.\n', cooled='claude-haiku-5-5')
    assert (rc, model) == ('0', 'grok-4.7') and 'REFUSED free-model guard' in stderr
    rc, model, _ = _pick(tmp_path, 'remote-qa-runner', 'LIGHT', brief='Run the QA scenario with --strict-replay.\n', cooled='claude-haiku-5-5')
    assert (rc, model) == ('0', HY4)
    rc, model, _ = _pick(tmp_path, 'remote-qa-runner', 'LIGHT', brief='Run with --strict-replay but capture on miss.\n', cooled='claude-haiku-5-5')
    assert (rc, model) == ('0', 'grok-4.7')


@pytest.mark.parametrize('model', (HY4, MUSE))
def test_raw_free_primary_is_refused_for_a_guarded_launch(tmp_path, model):
    rc, stderr = _primary(tmp_path, 'customer-artifact-investigator', model)
    assert rc == 'rc=75'
    assert f'REFUSED free-model guard primary {model}' in stderr


def test_raw_primary_guard_leaves_other_ids_and_unguarded_launches_alone(tmp_path):
    rc, stderr = _primary(tmp_path, 'customer-artifact-investigator', DEEPSEEK)
    assert rc == 'rc=0' and 'free-model guard' not in stderr
    rc, stderr = _primary(tmp_path, 'customer-artifact-investigator', 'claude-haiku-5-5')
    assert rc == 'rc=0' and 'free-model guard' not in stderr
    rc, stderr = _primary(tmp_path, 'hd-builder', HY4, brief='Fix the sidebar spacing.\n')
    assert rc == 'rc=0' and 'free-model guard' not in stderr


def test_guard_runs_before_the_emergency_gate_and_before_last_is_set():
    pick = _slice('proxy_agent_pick_tier() {', '# Ordinary burn gate for capped roles')
    assert pick.index('proxy_agent_free_guard_refuses "$id"') < pick.index('proxy_agent_is_emergency_muse "$id"') < pick.index('last="$id"')
