"""Direct DeepSeek emergency gate, read-only admission deny and Codex companion write scope of proxy-agent.sh (added to the repo; not run on the host).

Protected rollback target for c7fbbec (CTO ruling, C0ALZJHGE49 thread 1795038046.056679, ts 1795039413.858179). This tree is c7fbbec with ONLY
the two pool-cap enforcement hunks of fbdfbdc reverted (explicit/resumed Claude and Grok primary guard, fallback_last refusal), so the
cases for those hunks are removed with them. Every protection of c7fbbec stays and keeps its cases here. MoP changes need no local
testing or proof (Rajiv D0AMF0XE6TS thread 1793979986.975849, ts 1793983042.752819): this file is committed without being executed.

Paths covered:
- the direct DeepSeek emergency route accepts only `P0:<digits>`, and the gate runs on the canonical router identity (decorated,
  upper-case or padded ids included) BEFORE the dry-run return; the OpenCode Go id deepseek-v4.1-flash is not that route;
- the read-only helper deny of pm-admit-ci.sh has no brief-marker grant (the _ADMIT_DISPATCH lift of 707e4c1 is gone), and the Codex
  companion roles get runtime-state write paths only, never the shared config, routing files or auth store of ~/.codex-companions.
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
