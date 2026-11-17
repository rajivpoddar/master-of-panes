"""Contract tests for the OpenCode Go pool in proxy-agent.sh (added to the repo; not run on the host).

Rajiv D0AMF0XE6TS thread 1794926930.078169, ts 1794930258.014469: pool `opencode` with ONE cap
(OPENCODE_PROXY_CAP, default 2) counting hy4-flash-free, deepseek-v4.1-flash and muse-spark-1.3-contributor.
Muse Spark is the final entry of every tier and is emergency-only.
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / 'shared-assets/claude/scripts/proxy-agent.sh'
SRC = SCRIPT.read_text()

HY4 = 'hy4-flash-free'
DEEPSEEK = 'deepseek-v4.1-flash'
MUSE = 'muse-spark-1.3-contributor'
POOL_IDS = (HY4, DEEPSEEK, MUSE)
ALL_TIERS = ('LIGHT', 'MID', 'TOP', 'PLAN', 'BUILD1', 'BUILD2', 'BUILD3', 'PROOF1', 'PROOF2', 'REVIEW')


def _slice(start, end):
    first = SRC.index(start)
    return SRC[first:SRC.index(end, first)]


# Helpers plus proxy_agent_pick_tier (the burn/pool/probe helpers are stubbed by the harness).
PICKER = _slice('# OpenCode Go pool: hy4-flash-free', '# Ordinary burn gate for capped roles')
# proxy_agent_cap_exempt, proxy_agent_pool_live, proxy_agent_pool_blocked.
CAPS = _slice('proxy_agent_cap_exempt() {', '# Primary burn check (CTO REVISE')
LOG_FN = _slice('proxy_agent_log_emergency_muse() {', 'proxy_agent_log_emergency_muse primary')


def _tier_models():
    tiers = {}
    for tier, models in re.findall(r'^\s+([A-Z0-9]+)\) models="([^"]*)";;', PICKER, re.M):
        tiers[tier] = models.split()
    return tiers


def _bash(body, tmp_path, env=None):
    full = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(tmp_path)}
    full.update(env or {})
    return subprocess.run(['bash', '-c', 'set -euo pipefail\n' + body], capture_output=True, text=True, env=full)


PICK_STUBS = r'''
TYPE=hd-builder
PROXY_AGENT_CLAUDE_CAP=5
STUB_BLOCKED="${STUB_BLOCKED:-}"
STUB_COOLED="${STUB_COOLED:-}"
STUB_FAIL="${STUB_FAIL:-}"
proxy_agent_is_capped() { return 0; }
proxy_agent_live_count() { echo 0; }
proxy_agent_burn_enforced() { return 0; }
proxy_agent_burn_refused() { return 1; }
proxy_agent_pool_blocked() { case " $STUB_BLOCKED " in *" $1 "*) return 0;; esac; return 1; }
proxy_agent_sub_usable() {
  case " $STUB_COOLED " in *" $1 "*) return 1;; esac
  case " $STUB_FAIL " in *" $1 "*) return 2;; esac
  return 0
}
'''


def _pick(tmp_path, tier, cooled='', fail='', blocked='', enforced=True):
    override = '' if enforced else 'proxy_agent_burn_enforced() { return 1; }\n'
    body = (PICK_STUBS + override + PICKER
            + f'proxy_agent_pick_tier {tier} && rc=0 || rc=$?\n'
            + 'printf "rc=%s model=%s reason=%s\\n" "$rc" "${PICK_MODEL-}" "${PICK_REASON-}"\n')
    result = _bash(body, tmp_path, {'STUB_COOLED': cooled, 'STUB_FAIL': fail, 'STUB_BLOCKED': blocked})
    assert result.returncode == 0, result.stderr
    fields = dict(part.split('=', 1) for part in result.stdout.strip().splitlines()[-1].split(' ', 2))
    return fields, result.stderr


# ---- tier order -------------------------------------------------------------------------------------------------

def test_light_and_mid_order_with_opencode_models():
    tiers = _tier_models()
    assert tiers['LIGHT'] == ['claude-haiku-5-5', HY4, 'grok-4.7', 'gpt-6-luna', MUSE]
    assert tiers['MID'] == ['claude-sonnet-5-5', DEEPSEEK, 'grok-4.7', 'gpt-6.1-terra', MUSE]
    # The requested order is preserved as a subsequence: haiku -> hy4 -> luna, sonnet -> deepseek -> terra.
    light = tiers['LIGHT']
    assert light.index('claude-haiku-5-5') < light.index(HY4) < light.index('gpt-6-luna')
    mid = tiers['MID']
    assert mid.index('claude-sonnet-5-5') < mid.index(DEEPSEEK) < mid.index('gpt-6.1-terra')


def test_hy4_and_deepseek_only_in_their_tiers():
    tiers = _tier_models()
    for tier, models in tiers.items():
        assert (HY4 in models) == (tier == 'LIGHT'), tier
        assert (DEEPSEEK in models) == (tier == 'MID'), tier


def test_muse_is_the_final_entry_of_every_tier_exactly_once():
    tiers = _tier_models()
    assert set(tiers) == set(ALL_TIERS)
    for tier, models in tiers.items():
        assert models[-1] == MUSE, tier
        assert models.count(MUSE) == 1, tier


def test_existing_claude_grok_codex_entries_are_kept():
    tiers = _tier_models()
    assert tiers['TOP'][:3] == ['claude-opus-5-5', 'grok-4.7', 'gpt-6.1-terra']
    assert tiers['PLAN'][:3] == ['grok-4.7', 'claude-opus-5-5', 'gpt-6.1-terra']
    assert tiers['BUILD1'][:2] == ['claude-haiku-5-5', 'gpt-6-luna']
    assert tiers['REVIEW'][:2] == ['claude-fable-5-2', 'gpt-6-astra']


# ---- one shared cap ---------------------------------------------------------------------------------------------

def test_single_opencode_cap_definition_covers_all_three_models():
    assert SRC.count('OPENCODE_PROXY_CAP:-2') == 1
    line = re.search(r'^\s+([^\n]*pool=opencode;[^\n]*)$', SRC, re.M).group(1)
    for model in POOL_IDS:
        assert model in line
    assert 'OPENCODE_PROXY_CAP:-2' in line
    caps_loader = re.search(r'PROXY_AGENT_TOTAL_CAP\|[^\n]*\) \[\[ -z', SRC).group(0)
    assert 'OPENCODE_PROXY_CAP' in caps_loader
    pool_live = _slice('proxy_agent_pool_live() {', 'proxy_agent_pool_blocked() {')
    for model in POOL_IDS:
        assert f'"{model}"' in pool_live
    assert '"opencode"' in pool_live


@pytest.mark.parametrize('model', POOL_IDS)
def test_cap_blocks_each_model_when_pool_is_full(tmp_path, model):
    base = 'TYPE=hd-builder\nROLE_BRIEF=\nPROXY_AGENT_CAP_OVERRIDE=\n' + CAPS
    probe = f'proxy_agent_pool_blocked {model} && echo blocked || echo free\n'
    full = _bash(base + probe, tmp_path, {'PROXY_AGENT_STUB_POOL_LIVE': '2'})
    assert full.stdout.strip() == 'blocked', full.stderr
    room = _bash(base + probe, tmp_path, {'PROXY_AGENT_STUB_POOL_LIVE': '1'})
    assert room.stdout.strip() == 'free', room.stderr
    raised = _bash(base + probe, tmp_path, {'PROXY_AGENT_STUB_POOL_LIVE': '2', 'OPENCODE_PROXY_CAP': '3'})
    assert raised.stdout.strip() == 'free', raised.stderr


def test_cap_exempt_role_and_other_pools_are_not_affected(tmp_path):
    base = 'ROLE_BRIEF=\nPROXY_AGENT_CAP_OVERRIDE=\n' + CAPS
    exempt = _bash('TYPE=mop-main-repair\n' + base + f'proxy_agent_pool_blocked {MUSE} && echo blocked || echo free\n',
                   tmp_path, {'PROXY_AGENT_STUB_POOL_LIVE': '9'})
    assert exempt.stdout.strip() == 'free', exempt.stderr
    claude = _bash('TYPE=hd-builder\n' + base + 'proxy_agent_pool_blocked claude-haiku-5-5 && echo blocked || echo free\n',
                   tmp_path, {'PROXY_AGENT_STUB_POOL_LIVE': '2', 'OPENCODE_PROXY_CAP': '1'})
    assert claude.stdout.strip() == 'free', claude.stderr


def test_shared_cap_stops_a_pool_member_even_when_another_is_cooled(tmp_path):
    # Pool full: hy4 (LIGHT) and muse are both pool_cap-skipped, so LIGHT falls through to grok, never to muse.
    fields, _ = _pick(tmp_path, 'LIGHT', cooled='claude-haiku-5-5 gpt-6-luna', blocked=f'{HY4} {DEEPSEEK} {MUSE}')
    assert fields['rc'] == '0' and fields['model'] == 'grok-4.7'
    # Everything else cooled and the pool full: Muse is blocked by the shared cap, the pick is refused.
    fields, stderr = _pick(tmp_path, 'MID', cooled='claude-sonnet-5-5 grok-4.7 gpt-6.1-terra', blocked=f'{HY4} {DEEPSEEK} {MUSE}')
    assert fields['rc'] == '75' and fields['model'] == ''


# ---- emergency-only gate ----------------------------------------------------------------------------------------

def test_muse_picked_only_after_every_earlier_entry_is_cooled(tmp_path):
    fields, _ = _pick(tmp_path, 'LIGHT')
    assert fields['model'] == 'claude-haiku-5-5'
    fields, _ = _pick(tmp_path, 'LIGHT', cooled='claude-haiku-5-5')
    assert fields['model'] == HY4
    fields, _ = _pick(tmp_path, 'LIGHT', cooled=f'claude-haiku-5-5 {HY4} grok-4.7')
    assert fields['model'] == 'gpt-6-luna'
    fields, _ = _pick(tmp_path, 'LIGHT', cooled=f'claude-haiku-5-5 {HY4} grok-4.7 gpt-6-luna')
    assert fields['rc'] == '0' and fields['model'] == MUSE
    assert fields['reason'].startswith('cooled:')


def test_pool_capped_and_cooled_entries_open_the_emergency_gate_in_a_conveyor_rung(tmp_path):
    fields, _ = _pick(tmp_path, 'BUILD1', cooled='gpt-6-luna', blocked='claude-haiku-5-5')
    assert fields['rc'] == '0' and fields['model'] == MUSE


def test_an_earlier_probe_failure_keeps_muse_gated(tmp_path):
    fields, stderr = _pick(tmp_path, 'LIGHT', fail=HY4, cooled='claude-haiku-5-5 grok-4.7 gpt-6-luna')
    assert fields['rc'] == '75' and fields['model'] == ''
    assert f'{MUSE}:emergency_gate' in stderr


def test_fallback_last_never_lands_on_muse(tmp_path):
    fields, _ = _pick(tmp_path, 'LIGHT', cooled=f'claude-haiku-5-5 {HY4} grok-4.7 gpt-6-luna {MUSE}', enforced=False)
    assert fields['model'] == 'gpt-6-luna'
    assert fields['reason'] == 'fallback_last'


def test_muse_gate_sits_before_burn_and_probe_and_burn_exempts_the_pool():
    picker = PICKER
    gate = picker.index('proxy_agent_is_emergency_muse "$id"')
    assert gate < picker.index('if proxy_agent_burn_refused "$id"; then')
    assert gate < picker.index('if proxy_agent_sub_usable "$id"; then')
    assert 'emergency_ok=0' in picker
    assert picker.count('last="$id"') == 1
    assert picker.index('else\n      last="$id"') > gate
    burn = _slice('proxy_agent_burn_refused() {', 'proxy_agent_cap_exempt() {')
    assert 'proxy_agent_is_opencode_id "$1"' in burn
    assert burn.index('proxy_agent_is_opencode_id') < burn.index('proxy-burn-shadow.py')


def test_probe_accepts_the_three_pool_ids():
    probe = _slice('model = os.environ.get("PROXY_AGENT_CLAUDE_PROBE_MODEL", "")', 'state_dir = os.environ.get("PROXY_AGENT_STATE_DIR")')
    for model in POOL_IDS:
        assert f'"{model}"' in probe


# ---- emergency log ----------------------------------------------------------------------------------------------

def test_emergency_log_path_and_position():
    assert SRC.count('proxy-emergency-muse.log') == 1
    # Logged at launch (after the dry-run exit), never during a dry-run selection.
    assert SRC.index('PROXY_AGENT_PATHS out=') < SRC.index('proxy_agent_log_emergency_muse primary "$MODEL_ID"')
    for role in ('haiku', 'sonnet', 'opus'):
        assert f'proxy_agent_log_emergency_muse {role} ' in SRC


def test_emergency_log_appends_only_for_muse(tmp_path):
    body = (PICKER + LOG_FN
            + 'TYPE=hd-builder\nSELECT_TIER=LIGHT\nSELECT_REASON=cooled:claude-haiku-5-5\nPROXY_AGENT_NAME=px-test\n'
            + f'proxy_agent_log_emergency_muse primary {MUSE}\n'
            + 'proxy_agent_log_emergency_muse primary claude-haiku-5-5\n'
            + f'proxy_agent_log_emergency_muse haiku {HY4}\n')
    result = _bash(body, tmp_path)
    assert result.returncode == 0, result.stderr
    lines = (tmp_path / '.claude/state/proxy-emergency-muse.log').read_text().splitlines()
    assert len(lines) == 1
    assert re.fullmatch(
        r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z EMERGENCY_MUSE role=primary name=px-test type=hd-builder tier=LIGHT '
        r'model=muse-spark-1\.3-contributor reason=cooled:claude-haiku-5-5', lines[0])
