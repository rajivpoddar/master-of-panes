"""Contract tests for the OpenCode Go pool in proxy-agent.sh (added to the repo; not run on the host).

Rajiv D0AMF0XE6TS thread 1794926930.078169, ts 1794930258.014469: pool `opencode` with ONE cap
(OPENCODE_PROXY_CAP, default 2) counting hy4-flash-free, deepseek-v4.1-flash and muse-spark-1.3-contributor.
Muse Spark is the final entry of every tier and is emergency-only.

CTO REVISE of 677302b (C0ALZJHGE49 thread 1794931098.013929, ts 1794932145.795929): Muse is emergency-only across EVERY eligible
route (not merely last within one rung), raw/explicit pool ids are guarded by the cap and the emergency gate, and nested
Haiku/Sonnet/Opus aliases never use the (uncounted) OpenCode pool. Static traces only; nothing here is run on the host.
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


# Helpers, tier lists, the emergency gate and proxy_agent_pick_tier (the burn/pool/probe helpers are stubbed by the harness).
PICKER = _slice('# OpenCode Go pool: hy4-flash-free', '# Ordinary burn gate for capped roles')
PICK_FN = _slice('proxy_agent_pick_tier() {', '# Ordinary burn gate for capped roles')
# proxy_agent_cap_exempt, proxy_agent_pool_live, proxy_agent_pool_blocked.
CAPS = _slice('proxy_agent_cap_exempt() {', '# Final-primary OpenCode guard')
GUARD = _slice('proxy_agent_primary_pool_guard() {', '# Primary burn check (CTO REVISE')
SELECT = _slice('proxy_agent_select() {', '# Model table: alias')
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
PA_FREE_SCOPE=verified  # the free-model scope gate has its own tests (test_proxy_agent_free_model_guard.py)
PA_FREE_SCOPE_WHY=stub
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


def _eligible():
    """Every non-emergency route in every tier list: what the emergency gate must see exhausted."""
    return sorted({m for models in _tier_models().values() for m in models if m != MUSE})


def _pick(tmp_path, tier, cooled='', fail='', blocked='', enforced=True, nested=False, env=None):
    override = '' if enforced else 'proxy_agent_burn_enforced() { return 1; }\n'
    body = (PICK_STUBS + override + PICKER
            + f'proxy_agent_pick_tier {tier}{" nested" if nested else ""} && rc=0 || rc=$?\n'
            + 'printf "rc=%s model=%s reason=%s\\n" "$rc" "${PICK_MODEL-}" "${PICK_REASON-}"\n')
    result = _bash(body, tmp_path, {'STUB_COOLED': cooled, 'STUB_FAIL': fail, 'STUB_BLOCKED': blocked, **(env or {})})
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
    # Muse needs the whole eligible set exhausted, not just this rung: every tier's non-emergency route is cooled here.
    fields, _ = _pick(tmp_path, 'LIGHT', cooled=' '.join(_eligible()))
    assert fields['rc'] == '0' and fields['model'] == MUSE
    assert fields['reason'].startswith('cooled:')


def test_pool_capped_and_cooled_entries_open_the_emergency_gate_in_a_conveyor_rung(tmp_path):
    # Capped (pool_blocked) and cooled routes both count as exhausted, but only when they cover every eligible route.
    eligible = _eligible()
    half = len(eligible) // 2
    fields, _ = _pick(tmp_path, 'BUILD1', cooled=' '.join(eligible[:half]), blocked=' '.join(eligible[half:]))
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
    picker = PICK_FN
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


# ---- CTO REVISE 2026-11-17 (C0ALZJHGE49 thread 1794931098.013929, ts 1794932145.795929) --------------------------------
# Verification table: (1) a healthy provider outside the current rung blocks Muse; (2) raw Muse cannot bypass that decision;
# (3) raw and nested pool use respects the configured cap; (4) unsupported/unknown ids never become an unchecked fallback.

def test_eligible_set_covers_claude_grok_codex_and_the_other_pool_ids():
    eligible = _eligible()
    for model in ('claude-haiku-5-5', 'claude-sonnet-5-5', 'claude-opus-5-5', 'claude-fable-5-2', 'grok-4.7',
                  'gpt-6-luna', 'gpt-6.1-terra', 'gpt-6-astra', HY4, DEEPSEEK):
        assert model in eligible, model
    assert MUSE not in eligible


def test_a_healthy_provider_outside_the_current_rung_blocks_muse(tmp_path):
    # BUILD1 is haiku/luna only; both are cooled. Grok (not a BUILD1 entry) is healthy, so Muse stays gated.
    fields, stderr = _pick(tmp_path, 'BUILD1', cooled='claude-haiku-5-5 gpt-6-luna')
    assert fields['rc'] == '75' and fields['model'] == ''
    assert f'{MUSE}:emergency_gate' in stderr
    assert 'emergency_gate skip' in stderr and 'blocker=' in stderr
    # Not enforced: the fallback_last pick still never lands on Muse.
    fields, _ = _pick(tmp_path, 'BUILD1', cooled='claude-haiku-5-5 gpt-6-luna', enforced=False)
    assert fields['model'] == 'gpt-6-luna' and fields['reason'] == 'fallback_last'


@pytest.mark.parametrize('open_route', _eligible())
def test_any_single_usable_eligible_route_keeps_muse_gated(tmp_path, open_route):
    cooled = ' '.join(m for m in _eligible() if m != open_route)
    fields, _ = _pick(tmp_path, 'BUILD1', cooled=cooled)
    assert fields['model'] != MUSE
    assert fields['model'] in ('', open_route)


def test_muse_opens_when_every_eligible_route_is_exhausted_by_cooldown_or_cap(tmp_path):
    fields, _ = _pick(tmp_path, 'BUILD1', cooled=' '.join(_eligible()))
    assert fields['rc'] == '0' and fields['model'] == MUSE
    fields, _ = _pick(tmp_path, 'BUILD1', blocked=' '.join(_eligible()))
    assert fields['rc'] == '0' and fields['model'] == MUSE


def test_an_unknown_provider_outside_the_rung_keeps_muse_gated(tmp_path):
    # Grok's probe fails (unknown, rc 2) while everything else is cooled: unknown availability does not prove exhaustion.
    cooled = ' '.join(m for m in _eligible() if m != 'grok-4.7')
    fields, _ = _pick(tmp_path, 'BUILD1', cooled=cooled, fail='grok-4.7')
    assert fields['rc'] == '75' and fields['model'] == ''


def test_the_no_subscription_switch_is_unknown_not_exhausted(tmp_path):
    fields, _ = _pick(tmp_path, 'BUILD1', cooled=' '.join(_eligible()), env={'PROXY_AGENT_NO_SUBSCRIPTION': '1'})
    assert fields['rc'] == '75' and fields['model'] == ''


def test_emergency_verdict_is_computed_once_per_launch():
    gate = _slice('proxy_agent_emergency_open() {', '# Picks the first usable model')
    assert 'PA_EMERGENCY_VERDICT=closed' in gate and 'PA_EMERGENCY_VERDICT=open' in gate
    assert gate.index('${PA_EMERGENCY_VERDICT:-}') < gate.index('proxy_agent_tier_models')
    exhausted = _slice('proxy_agent_route_exhausted() {', 'PA_EMERGENCY_VERDICT=""')
    assert exhausted.index('PROXY_AGENT_NO_SUBSCRIPTION') < exhausted.index('proxy_agent_burn_refused')
    assert exhausted.index('proxy_agent_burn_refused') < exhausted.index('proxy_agent_pool_blocked') < exhausted.index('proxy_agent_sub_usable')


# ---- nested aliases never use the (uncounted) OpenCode pool ----------------------------------------------------------

@pytest.mark.parametrize('tier', ('LIGHT', 'MID', 'TOP'))
def test_nested_picks_never_use_the_opencode_pool(tmp_path, tier):
    fields, _ = _pick(tmp_path, tier, nested=True)
    assert fields['model'] not in POOL_IDS
    # Everything on the approved routes cooled: the pool is NOT a substitute, the alias is refused (the export block sentinels it).
    cooled = ' '.join(m for m in _eligible() if m not in POOL_IDS)
    fields, _ = _pick(tmp_path, tier, nested=True, cooled=cooled)
    assert fields['rc'] == '75' and fields['model'] == ''
    fields, _ = _pick(tmp_path, tier, nested=True, cooled=' '.join(_eligible()), enforced=False)
    assert fields['model'] not in POOL_IDS


def test_nested_light_skips_hy4_and_goes_to_the_next_approved_route(tmp_path):
    fields, _ = _pick(tmp_path, 'LIGHT', nested=True, cooled='claude-haiku-5-5')
    assert fields['model'] == 'grok-4.7'
    fields, _ = _pick(tmp_path, 'MID', nested=True, cooled='claude-sonnet-5-5')
    assert fields['model'] == 'grok-4.7'


def test_select_resolves_nested_aliases_with_nested_and_the_primary_without():
    calls = re.findall(r'proxy_agent_pick_tier (LIGHT|MID|TOP)( nested)?', SELECT)
    assert [(tier, bool(flag)) for tier, flag in calls] == [('LIGHT', True), ('MID', True), ('TOP', True)]
    assert 'proxy_agent_pick_tier "$tier" ||' in SELECT


# ---- raw primaries: cap and emergency gate on the final resolved primary ----------------------------------------------

def _guard(tmp_path, model, cooled='', fail='', blocked='', env=None):
    body = (PICK_STUBS + 'resolve_model() { echo "$1 200000 32000"; }\n' + PICKER + GUARD
            + f'MODEL={model}\nproxy_agent_primary_pool_guard && rc=0 || rc=$?\nprintf "rc=%s\\n" "$rc"\n')
    result = _bash(body, tmp_path, {'STUB_COOLED': cooled, 'STUB_FAIL': fail, 'STUB_BLOCKED': blocked, **(env or {})})
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1], result.stderr


def test_guard_runs_before_the_burn_check_and_before_any_nested_probe():
    assert 'proxy_agent_primary_pool_guard || return 75' in SELECT
    guard = SELECT.index('proxy_agent_primary_pool_guard || return 75')
    assert guard < SELECT.index('proxy_agent_primary_burn_check || return 75') < SELECT.index('proxy_agent_pick_tier LIGHT nested')
    # RAW primaries skip the tier pick but not the guard.
    assert SELECT.index('if [[ "$tier" != RAW ]]; then') < guard


@pytest.mark.parametrize('model', (HY4, DEEPSEEK, MUSE))
def test_raw_pool_id_at_its_cap_is_refused(tmp_path, model):
    rc, stderr = _guard(tmp_path, model, blocked=model)
    assert rc == 'rc=75'
    assert f'REFUSED primary {model} (opencode pool_cap)' in stderr


@pytest.mark.parametrize('model', (HY4, DEEPSEEK))
def test_raw_non_emergency_pool_id_under_the_cap_is_allowed_without_the_emergency_gate(tmp_path, model):
    rc, _ = _guard(tmp_path, model)
    assert rc == 'rc=0'


def test_raw_muse_cannot_bypass_the_emergency_gate(tmp_path):
    rc, stderr = _guard(tmp_path, MUSE)
    assert rc == 'rc=75' and f'REFUSED primary {MUSE} (emergency_gate blocker=' in stderr
    rc, _ = _guard(tmp_path, MUSE, cooled=' '.join(_eligible()))
    assert rc == 'rc=0'
    # Unknown availability (probe failure) is not exhaustion.
    rc, _ = _guard(tmp_path, MUSE, cooled=' '.join(m for m in _eligible() if m != 'grok-4.7'), fail='grok-4.7')
    assert rc == 'rc=75'


def test_raw_muse_at_the_pool_cap_is_refused_even_when_the_gate_is_open(tmp_path):
    rc, stderr = _guard(tmp_path, MUSE, cooled=' '.join(_eligible()), blocked=MUSE)
    assert rc == 'rc=75' and 'opencode pool_cap' in stderr


@pytest.mark.parametrize('model', ('claude-haiku-5-5', 'grok-4.7', 'gpt-6-luna', 'luna', 'deepseek-flash-4.1'))
def test_guard_leaves_every_non_pool_primary_alone(tmp_path, model):
    rc, stderr = _guard(tmp_path, model, blocked=' '.join(_eligible()))
    assert rc == 'rc=0' and 'REFUSED' not in stderr


def test_cap_zero_turns_the_pool_off_even_when_the_process_list_is_unreadable(tmp_path):
    base = 'TYPE=hd-builder\nROLE_BRIEF=\nPROXY_AGENT_CAP_OVERRIDE=\n' + CAPS + 'proxy_agent_pool_live() { return 1; }\n'
    for model in POOL_IDS:
        probe = f'proxy_agent_pool_blocked {model} && echo blocked || echo free\n'
        zero = _bash(base + probe, tmp_path, {'OPENCODE_PROXY_CAP': '0'})
        assert zero.stdout.strip() == 'blocked', (model, zero.stderr)
        # Any other numeric cap with an unreadable count keeps the existing fail-open behaviour.
        three = _bash(base + probe, tmp_path, {'OPENCODE_PROXY_CAP': '3'})
        assert three.stdout.strip() == 'free', (model, three.stderr)
    other = _bash(base + 'proxy_agent_pool_blocked claude-haiku-5-5 && echo blocked || echo free\n', tmp_path,
                  {'CLAUDE_PROXY_CAP': '0'})
    assert other.stdout.strip() == 'free', other.stderr


def test_cap_zero_keeps_actual_scoped_exemptions(tmp_path):
    base = ('ROLE_BRIEF=\nPROXY_AGENT_CAP_OVERRIDE=\n' + CAPS + 'proxy_agent_pool_live() { return 1; }\n')
    probe = f'proxy_agent_pool_blocked {HY4} && echo blocked || echo free\n'
    exempt = _bash('TYPE=mop-main-repair\n' + base + probe, tmp_path, {'OPENCODE_PROXY_CAP': '0'})
    assert exempt.stdout.strip() == 'free', exempt.stderr
    p0 = _bash('TYPE=hd-builder\n' + base.replace('PROXY_AGENT_CAP_OVERRIDE=\n', 'PROXY_AGENT_CAP_OVERRIDE=P0:13900\n', 1) + probe,
               tmp_path, {'OPENCODE_PROXY_CAP': '0'})
    assert p0.stdout.strip() == 'free', p0.stderr


def test_an_exemption_is_not_muse_permission(tmp_path):
    # A cap-exempt launch (pool_blocked never true) still needs every eligible route known exhausted for Muse.
    body = (PICK_STUBS + 'proxy_agent_pool_blocked() { return 1; }\nresolve_model() { echo "$1 200000 32000"; }\n'
            + PICKER + GUARD + f'MODEL={MUSE}\nproxy_agent_primary_pool_guard && echo allowed || echo refused\n')
    result = _bash(body, tmp_path, {'STUB_COOLED': '', 'STUB_FAIL': '', 'STUB_BLOCKED': ''})
    assert result.stdout.strip() == 'refused', result.stderr


def test_dial_ownership_is_outside_this_repo():
    # proxy-burn-dial.py is deployed-only tooling under ~/.claude/scripts/cost (PM-owned), not a shared asset: the launcher only
    # reads OPENCODE_PROXY_CAP from the file the dial writes, and must keep reading it.
    manifest = (Path(__file__).parents[1] / 'shared-assets/manifest.json').read_text()
    assert 'proxy-burn-dial' not in manifest
    assert 'OPENCODE_PROXY_CAP' in SRC
