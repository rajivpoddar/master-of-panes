"""Pool-cap enforcement on every proxy-agent.sh path, and one validity rule for the DeepSeek override (added to the repo; not run on the host).

MoP proxy cap repair 2026-11-19. MoP changes need no local testing or proof (Rajiv D0AMF0XE6TS thread 1793979986.975849, ts 1793983042.752819),
so this file is committed without being executed. Same sliced-bash style as test_proxy_agent_opencode_pool.py.

Paths covered:
- raw/explicit and resumed Claude and Grok primaries skip proxy_agent_pick_tier; they now meet the same pool_blocked decision as the
  Codex and OpenCode primaries (router-equivalent identity, so a decorated or upper-case id is the same admission);
- the non-enforced best-effort `fallback_last` never exports a pool-capped (or empty) id;
- the direct DeepSeek emergency route accepts only `P0:<digits>`, the shape every other override gate already requires, and the gate runs on
  the canonical router identity (decorated, upper-case or padded ids included) BEFORE the dry-run return (CTO REVISE of fbdfbdc,
  C0ALZJHGE49 thread 1795038046.056679, ts 1795038184.764339); the OpenCode Go id deepseek-v4.1-flash is not that route;
- the read-only helper deny of pm-admit-ci.sh has no brief-marker grant (the _ADMIT_DISPATCH lift of 707e4c1 is gone), and the Codex
  companion roles get runtime-state write paths only, never the shared config, routing files or auth store of ~/.codex-companions.
Left as designed: the total and Claude-count checks stay telemetry (burn-rate caps, 1e4787f), and the continuation runner re-checks only OpenCode.
"""
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / 'shared-assets/claude/scripts/proxy-agent.sh'
SRC = SCRIPT.read_text()


def _slice(start, end):
    first = SRC.index(start)
    return SRC[first:SRC.index(end, first)]


# Tier lists, router identity, free-model guard, emergency gate and proxy_agent_pick_tier (burn / pool / probe helpers are stubbed below).
PICKER = _slice('# OpenCode Go pool: hy4-flash-free', '# Ordinary burn gate for capped roles')
GUARD = _slice('proxy_agent_primary_pool_guard() {', '# Primary burn check (CTO REVISE')
# The direct DeepSeek gate: canonical-identity helper plus the P0:<digits> check, placed between proxy_agent_select and the dry-run return.
DEEPSEEK_GATE = _slice('proxy_agent_is_direct_deepseek() {', '# A UTC-second stamp alone')

STUBS = r'''
TYPE=hd-builder
PROXY_AGENT_CLAUDE_CAP=5
STUB_BLOCKED="${STUB_BLOCKED:-}"
STUB_COOLED="${STUB_COOLED:-}"
proxy_agent_is_capped() { return 0; }
proxy_agent_live_count() { echo 0; }
proxy_agent_burn_enforced() { return 0; }
proxy_agent_burn_refused() { return 1; }
proxy_agent_pool_blocked() { local i; i="$(proxy_agent_router_identity "$1")"; case " $STUB_BLOCKED " in *" $i "*) return 0;; esac; return 1; }
proxy_agent_sub_usable() { case " $STUB_COOLED " in *" $1 "*) return 1;; esac; return 0; }
resolve_model() { echo "$1 200000 32000"; }
'''


def _bash(body, tmp_path, env=None):
    full = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(tmp_path)}
    full.update(env or {})
    return subprocess.run(['bash', '-c', 'set -euo pipefail\n' + body], capture_output=True, text=True, env=full)


def _guard(tmp_path, model, blocked=''):
    body = (STUBS + PICKER + GUARD
            + f'MODEL={model!r}\nproxy_agent_primary_pool_guard && rc=0 || rc=$?\nprintf "rc=%s\\n" "$rc"\n')
    result = _bash(body, tmp_path, {'STUB_BLOCKED': blocked})
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1], result.stderr


def _pick(tmp_path, tier, cooled='', blocked='', enforced=True):
    override = '' if enforced else 'proxy_agent_burn_enforced() { return 1; }\n'
    body = (STUBS + override + PICKER
            + f'proxy_agent_pick_tier {tier} && rc=0 || rc=$?\n'
            + 'printf "rc=%s model=%s reason=%s\\n" "$rc" "${PICK_MODEL-}" "${PICK_REASON-}"\n')
    result = _bash(body, tmp_path, {'STUB_COOLED': cooled, 'STUB_BLOCKED': blocked})
    assert result.returncode == 0, result.stderr
    fields = dict(part.split('=', 1) for part in result.stdout.strip().splitlines()[-1].split(' ', 2))
    return fields, result.stderr


# ---- raw / resumed primaries ------------------------------------------------------------------------------------------

@pytest.mark.parametrize('model,identity', (
    ('claude-opus-5-5', 'claude-opus-5-5'),
    ('claude-fable-5-2', 'claude-fable-5-2'),
    ('claude-sonnet-5-5-high', 'claude-sonnet-5-5-high'),
    ('Claude-Opus-5-5(high)', 'claude-opus-5-5'),
    ('claude-opus-5-5[1m]', 'claude-opus-5-5'),
))
def test_raw_claude_primary_at_its_pool_cap_is_refused(tmp_path, model, identity):
    rc, stderr = _guard(tmp_path, model, blocked=identity)
    assert rc == 'rc=75'
    assert f'REFUSED primary {model} (claude pool_cap)' in stderr
    rc, stderr = _guard(tmp_path, model)
    assert rc == 'rc=0' and 'REFUSED' not in stderr


@pytest.mark.parametrize('model,identity', (('grok-4.7', 'grok-4.7'), ('grok', 'grok'), ('GROK-4.7(high)', 'grok-4.7')))
def test_raw_grok_primary_at_its_pool_cap_is_refused(tmp_path, model, identity):
    rc, stderr = _guard(tmp_path, model, blocked=identity)
    assert rc == 'rc=75'
    assert f'REFUSED primary {model} (grok pool_cap)' in stderr
    rc, stderr = _guard(tmp_path, model)
    assert rc == 'rc=0' and 'REFUSED' not in stderr


def test_a_blocked_claude_pool_does_not_refuse_grok_or_codex_primaries(tmp_path):
    for model in ('grok-4.7', 'gpt-6.1-terra', 'gpt-6-luna'):
        rc, _ = _guard(tmp_path, model, blocked='claude-opus-5-5 claude-sonnet-5-5 claude-haiku-5-5 claude-fable-5-2')
        assert rc == 'rc=0', model


def test_codex_primary_keeps_its_existing_pool_cap_and_no_codex_refusals(tmp_path):
    rc, stderr = _guard(tmp_path, 'gpt-6.1-terra', blocked='gpt-6.1-terra')
    assert rc == 'rc=75' and 'REFUSED primary gpt-6.1-terra (codex pool_cap)' in stderr


def test_the_guard_still_runs_after_the_tier_pick_and_before_the_burn_check():
    select = _slice('proxy_agent_select() {', '# Model table: alias')
    assert select.index('if [[ "$tier" != RAW ]]; then') < select.index('proxy_agent_primary_pool_guard || return 75')
    assert select.index('proxy_agent_primary_pool_guard || return 75') < select.index('proxy_agent_primary_burn_check || return 75')


# ---- fallback_last ------------------------------------------------------------------------------------------------------

def test_fallback_last_is_refused_when_the_last_id_is_pool_capped(tmp_path):
    # Non-enforced role (burn gate off), every earlier MID route cooled, the last non-emergency route (terra) at its pool cap.
    fields, stderr = _pick(tmp_path, 'MID', cooled='claude-sonnet-5-5 deepseek-v4.1-flash grok-4.7',
                           blocked='gpt-6.1-terra', enforced=False)
    assert fields['rc'] == '75' and fields['model'] == ''
    assert 'pool_cap skip model=gpt-6.1-terra' in stderr
    assert 'REFUSED fallback_last gpt-6.1-terra (pool_cap)' in stderr


def test_fallback_last_still_picks_the_last_id_when_it_has_pool_room(tmp_path):
    fields, _ = _pick(tmp_path, 'MID', cooled='claude-sonnet-5-5 deepseek-v4.1-flash grok-4.7 gpt-6.1-terra', enforced=False)
    assert fields['rc'] == '0' and fields['model'] == 'gpt-6.1-terra' and fields['reason'] == 'fallback_last'


def test_enforced_roles_keep_the_fail_closed_refusal(tmp_path):
    fields, stderr = _pick(tmp_path, 'MID', cooled='claude-sonnet-5-5 deepseek-v4.1-flash grok-4.7 gpt-6.1-terra')
    assert fields['rc'] == '75' and fields['model'] == ''
    assert 'REFUSED no usable tier model' in stderr


def test_the_fallback_check_sits_after_the_enforced_branch_and_before_the_export():
    picker = _slice('proxy_agent_pick_tier() {', '# Ordinary burn gate for capped roles')
    enforced = picker.index('if proxy_agent_burn_enforced; then')
    check = picker.index('if [[ -z "$last" ]] || proxy_agent_pool_blocked "$last"; then')
    export = picker.index('PICK_MODEL="$last"; PICK_REASON="fallback_last"')
    assert enforced < check < export


# ---- direct DeepSeek emergency gate -------------------------------------------------------------------------------------

DIRECT_DEEPSEEK = ('deepseek', 'deepseek-flash-4.1', 'deepseek-flash', 'DeepSeek', 'deepseek(high)', 'deepseek-flash-4.1(high)',
                   'DeepSeek-Flash-4.1(high)', 'deepseek-flash-4.1[1m]', ' deepseek-flash-4.1 ')


def _gate(tmp_path, override, model='deepseek'):
    body = STUBS + PICKER + f'MODEL={model!r}\n' + DEEPSEEK_GATE + 'echo passed\n'
    return _bash(body, tmp_path, {'PROXY_AGENT_CAP_OVERRIDE': override})


@pytest.mark.parametrize('model', DIRECT_DEEPSEEK)
@pytest.mark.parametrize('override', ('P0:14000', 'P0:1'))
def test_direct_deepseek_accepts_a_valid_p0_override(tmp_path, model, override):
    result = _gate(tmp_path, override, model)
    assert result.returncode == 0 and result.stdout.strip() == 'passed', result.stderr
    assert f'DeepSeek emergency route for {override}' in result.stderr


@pytest.mark.parametrize('model', DIRECT_DEEPSEEK)
@pytest.mark.parametrize('override', ('', 'P0:', 'P0:abc', 'P0:12 extra', 'P0:12x', 'p0:12', 'rajiv:1794806595.680589', 'P1:12', ' P0:12'))
def test_direct_deepseek_refuses_an_invalid_override_in_every_decoration(tmp_path, model, override):
    result = _gate(tmp_path, override, model)
    assert result.returncode == 2 and 'passed' not in result.stdout
    assert 'deepseek_requires_p0_override' in result.stderr


@pytest.mark.parametrize('model', ('deepseek-v4.1-flash', 'deepseek-v4.1-flash(high)', 'Deepseek-V4.1-Flash', 'glm-5.3', 'claude-sonnet-5-5'))
def test_the_opencode_flash_id_and_other_models_are_not_the_direct_deepseek_route(tmp_path, model):
    result = _gate(tmp_path, '', model)
    assert result.returncode == 0 and result.stdout.strip() == 'passed', result.stderr
    assert 'DeepSeek emergency route' not in result.stderr


def test_the_deepseek_gate_runs_before_the_dry_run_return_and_exists_once():
    assert SRC.index('if proxy_agent_is_direct_deepseek "$MODEL"; then') < SRC.index(
        'if [[ "${PROXY_AGENT_DRY_RUN:-}" == "1" ]]; then\n  proxy_agent_print_selection')
    assert SRC.count('deepseek_requires_p0_override') == 1


def test_the_deepseek_gate_has_no_wildcard_p0_arm():
    assert not re.search(r'^\s+P0:\*\)', SRC, re.M)
    assert 'P0:[0-9]+$ ]]; then' in DEEPSEEK_GATE


# ---- read-only admission denial and companion write scope (CTO REVISE of 707e4c1) ---------------------------------------

CODEX_HOME = '/Users/rajiv/.codex-companions'
# Shared model/routing config, auth store and prompt-bearing state: never in a lane's write set.
SHARED_CONFIG = ('config.toml', 'auth.json', 'auth.json.symlink-target.bak', 'codex-router/merged-models.json',
                 'codex-router/model-picker.json', 'models_cache.json', 'rules', 'skills', 'plugins', 'agents',
                 'memories/MEMORY.md', 'RUNBOOK.md')


def _settings_script():
    start = SRC.index('HELPER_READONLY="$_HELPER_READONLY" PROXY_AGENT_TYPE_FOR_SANDBOX')
    body = SRC.index("<<'PY'\n", start) + len("<<'PY'\n")
    return SRC[body:SRC.index('\nPY\n', body) + 1]


def _settings(tmp_path, role, helper_readonly):
    out = tmp_path / 'settings.json'
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(tmp_path), 'PROXY_AGENT_SANDBOX': '1',
           'HELPER_READONLY': '1' if helper_readonly else '0', 'PROXY_AGENT_TYPE_FOR_SANDBOX': role}
    result = subprocess.run(['python3', '-I', '-', str(tmp_path / 'missing-settings.json'), str(out)],
                            input=_settings_script(), capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    return json.loads(out.read_text())


def _covered(path, allow):
    return any(path == a or path.startswith(a.rstrip('/') + '/') for a in allow)


@pytest.mark.parametrize('role', ('hd-builder', 'codex-code-reviewer', 'pm-ops-tooling', 'claude'))
def test_read_only_helpers_stay_denied_pm_admit_ci_with_no_override_variant(tmp_path, role):
    deny = _settings(tmp_path, role, helper_readonly=True)['permissions']['deny']
    assert 'Bash(*pm-admit-ci.sh*)' in deny
    assert not any('--no-12b-gate' in d or '--force-ci' in d or '--reason' in d for d in deny)


def test_there_is_no_brief_marker_grant_for_ci_admission():
    assert 'ADMIT_DISPATCH' not in SRC


@pytest.mark.parametrize('role', ('hd-builder', 'hd-planner', 'codex-code-reviewer', 'pm-proof-qa'))
def test_companion_roles_write_runtime_state_only_never_shared_config_or_auth(tmp_path, role):
    allow = _settings(tmp_path, role, helper_readonly=False)['sandbox']['filesystem']['allowWrite']
    assert CODEX_HOME not in allow
    assert _covered(CODEX_HOME + '/sqlite/codex-dev.db', allow) and _covered(CODEX_HOME + '/sessions/x.jsonl', allow)
    for rel in SHARED_CONFIG:
        assert not _covered(CODEX_HOME + '/' + rel, allow), rel


def test_other_roles_get_no_companion_write_path(tmp_path):
    allow = _settings(tmp_path, 'claude', helper_readonly=False)['sandbox']['filesystem']['allowWrite']
    assert not any(a.startswith(CODEX_HOME) for a in allow)
