"""Contract tests for the OpenCode Go pool in proxy-agent.sh (added to the repo; not run on the host).

Rajiv D0AMF0XE6TS thread 1794926930.078169, ts 1794930258.014469: pool `opencode` with ONE cap
(OPENCODE_PROXY_CAP, default 2) counting hy4-flash-free, deepseek-v4.1-flash and muse-spark-1.3-contributor.
Muse Spark is the final entry of every tier and is emergency-only.

CTO REVISE of 677302b (C0ALZJHGE49 thread 1794931098.013929, ts 1794932145.795929): Muse is emergency-only across EVERY eligible
route (not merely last within one rung), raw/explicit pool ids are guarded by the cap and the emergency gate, and nested
Haiku/Sonnet/Opus aliases never use the (uncounted) OpenCode pool. Static traces only; nothing here is run on the host.

Free-model guard (CTO REVISE of 970d140, C0ALZJHGE49 thread 1794985651.254889, ts 1794987952.900739): hy4-flash-free and Muse are now
unconditionally ineligible for any launch, so this file exercises the retained cap and emergency-gate mechanics with the guard
turned off in the harness (FREE_GUARD_OFF); the real denial is tested in test_proxy_agent_free_model_guard.py.
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
ORNITH = 'ornith-1.5-35b-a3b'
QWEN = 'qwen3.8-flash-next'
INKLING = 'inkling-2-mini'
POOL_IDS = (HY4, DEEPSEEK, MUSE)
ALL_TIERS = ('LIGHT', 'MID', 'TOP', 'PLAN', 'BUILD1', 'BUILD2', 'BUILD3', 'PROOF1', 'PROOF2', 'REVIEW')


def _slice(start, end):
    first = SRC.index(start)
    return SRC[first:SRC.index(end, first)]


# Helpers, tier lists, the emergency gate and proxy_agent_pick_tier (the burn/pool/probe helpers are stubbed by the harness).
PICKER = _slice('# OpenCode Go pool: hy4-flash-free', '# Ordinary burn gate for capped roles')
PICK_FN = _slice('proxy_agent_pick_tier() {', '# Ordinary burn gate for capped roles')
# proxy_agent_cap_exempt, proxy_agent_pool_live, proxy_agent_pool_blocked.
# Admission canonicalizes through proxy_agent_router_identity, which sits above the cap helpers.
CAPS = (_slice('proxy_agent_router_identity() {', '# The two restricted upstreams')
        + _slice('proxy_agent_cap_exempt() {', '# Final-primary OpenCode guard'))
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
PROXY_AGENT_CLAUDE_CAP=5
STUB_BLOCKED="${STUB_BLOCKED:-}"
STUB_COOLED="${STUB_COOLED:-}"
STUB_FAIL="${STUB_FAIL:-}"
# The local Spark health gate curls /metrics and expects a quoted served ID in the body
# (peer 22 Nov 03:05). Stub the body from the launcher's own tier lists, so every id the launcher can pick is "served" and a pick is
# decided by the tier list, the cap and the cooldown only -- never by whether this host can reach the Spark, and never needing an edit
# when a tier gains a correct entry (rule 60). The gate's own behaviour is asserted in test_proxy_agent_qwen_routing.py.
curl() { printf '%s' "${STUB_SPARK_BODY:-}"; return 0; }
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


# The free-model guard is UNCONDITIONAL (test_proxy_agent_free_model_guard.py owns that behaviour): hy4-flash-free and Muse are never picked
# or launched. These tests cover the retained pool-cap and Muse emergency-gate MECHANICS (caps and the gate are unchanged), so the harness
# turns the guard off after the real definitions are loaded. Nothing outside this file's bash snippets is affected.
FREE_GUARD_OFF = 'proxy_agent_free_guard_refuses() { return 1; }\n'


def _eligible():
    """Every non-emergency route in every tier list: what the emergency gate must see exhausted."""
    return sorted({m for models in _tier_models().values() for m in models if m != MUSE})


def _spark_body():
    """A probe body with every quoted tier ID, so the launcher's model-id health gate passes for any pick."""
    ids = sorted({m for models in _tier_models().values() for m in models})
    return '{"data":[' + ','.join('{"id":"%s"}' % m for m in ids) + ']}'


def _pick(tmp_path, tier, cooled='', fail='', blocked='', enforced=True, nested=False, env=None):
    override = '' if enforced else 'proxy_agent_burn_enforced() { return 1; }\n'
    body = (PICK_STUBS + override + PICKER + FREE_GUARD_OFF
            + f'proxy_agent_pick_tier {tier}{" nested" if nested else ""} && rc=0 || rc=$?\n'
            + 'printf "rc=%s model=%s reason=%s\\n" "$rc" "${PICK_MODEL-}" "${PICK_REASON-}"\n')
    result = _bash(body, tmp_path, {'STUB_COOLED': cooled, 'STUB_FAIL': fail, 'STUB_BLOCKED': blocked,
                                    'STUB_SPARK_BODY': _spark_body(), **(env or {})})
    assert result.returncode == 0, result.stderr
    fields = dict(part.split('=', 1) for part in result.stdout.strip().splitlines()[-1].split(' ', 2))
    return fields, result.stderr


# ---- tier order -------------------------------------------------------------------------------------------------

def test_light_and_mid_order_with_opencode_models():
    tiers = _tier_models()
    # Order by POOL (Rajiv C0ALZJHGE49 thread 1795311834.216809, ts 1795315175.684789 "spark -> opencode -> claude proxy -> codex proxy"):
    # the local Spark id, then the OpenCode ids, then the tier's Claude id, then the Codex ladder. Asserted as relative order, not an
    # exact-list pin (rule 60): a new correct entry anywhere must not fail this test.
    for tier in ('LIGHT', 'MID', 'BUILD1', 'PROOF1'):
        models = tiers[tier]
        spark = models.index(ORNITH)
        opencode = min(models.index(m) for m in (DEEPSEEK, HY4) if m in models)
        claude = min(models.index(m) for m in models if m.startswith('claude-'))
        codex = min(models.index(m) for m in models if m.startswith('gpt-'))
        assert spark < opencode < claude < codex, (tier, models)
    # The Codex ladder keeps its luna -> terra -> sol order inside each local tier.
    for tier in ('LIGHT', 'MID', 'BUILD1', 'PROOF1'):
        ladder = [m for m in tiers[tier] if m in ('gpt-6-luna', 'gpt-6.1-terra', 'gpt-6.2-sol')]
        assert ladder == ['gpt-6-luna', 'gpt-6.1-terra', 'gpt-6.2-sol'], tier


def test_hy4_and_deepseek_stay_inside_the_tiers_that_listed_them():
    tiers = _tier_models()
    # deepseek-v4.1-flash is a MID/BUILD1/PROOF1 route and hy4-flash-free rides every local tier behind the free-model
    # guard; neither may leak into TOP/PLAN/BUILD2/BUILD3/PROOF2/REVIEW.
    for tier in ('TOP', 'PLAN', 'BUILD2', 'BUILD3', 'PROOF2', 'REVIEW'):
        assert HY4 not in tiers[tier], tier
        assert DEEPSEEK not in tiers[tier], tier


def test_muse_is_the_final_entry_of_every_tier_exactly_once():
    tiers = _tier_models()
    assert set(tiers) == set(ALL_TIERS)
    for tier, models in tiers.items():
        assert models[-1] == MUSE, tier
        assert models.count(MUSE) == 1, tier


def test_existing_claude_grok_codex_entries_are_kept():
    tiers = _tier_models()
    # These entries must survive in their tiers; their exact positions are not pinned (rule 60). The 12b reviewer ladder keeps
    # fable before astra, and REVIEW's Codex ids keep terra -> sol.
    assert 'grok-4.7' in tiers['TOP'] and 'claude-opus-5-5' in tiers['TOP']
    assert 'grok-4.7' in tiers['PLAN'] and 'claude-opus-5-5' in tiers['PLAN']
    build1 = [m for m in tiers['BUILD1'] if m.startswith('gpt-')]
    assert build1[:2] == ['gpt-6-luna', 'gpt-6.1-terra']
    assert 'claude-haiku-5-5' in tiers['BUILD1']
    review = tiers['REVIEW']
    assert 'claude-fable-5-2' in review and 'gpt-6-astra' in review
    assert review.index('claude-fable-5-2') < review.index('gpt-6-astra')
    assert [m for m in review if m in ('gpt-6.1-terra', 'gpt-6.2-sol')] == ['gpt-6.1-terra', 'gpt-6.2-sol']


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


@pytest.mark.parametrize('reported,expected', [(ORNITH, ORNITH), (QWEN, ORNITH), (INKLING, DEEPSEEK)])
def test_ornith_accepts_either_spark_metric_but_not_inkling(tmp_path, reported, expected):
    metric = f'vllm:num_requests_total{{model_name="{reported}"}} 3\n'
    fields, _ = _pick(tmp_path, 'LIGHT', env={'STUB_SPARK_BODY': metric})
    assert fields['model'] == expected


@pytest.mark.parametrize('model', [ORNITH, QWEN])
def test_both_spark_aliases_obey_new_cap_default_and_legacy_override(tmp_path, model):
    body = 'TYPE=hd-builder\nROLE_BRIEF=\n' + CAPS
    body += f'proxy_agent_pool_blocked {model} && printf blocked || printf free\n'
    for live, env, expected in [(5, {}, 'free'), (6, {}, 'blocked'), (6, {'QWEN_PROXY_CAP': '8'}, 'free'),
                                (6, {'QWEN_PROXY_CAP': '8', 'SPARK_PROXY_CAP': '6'}, 'blocked')]:
        result = _bash(body, tmp_path, {'PROXY_AGENT_STUB_POOL_LIVE': str(live), **env})
        assert result.stdout == expected, result.stderr


def test_opencode_cap_binds_exempt_roles_other_pools_stay_exempt(tmp_path):
    # 2026-11-18: ci-repair-agent and growth are cap-exempt, so pool_blocked returned before the
    # OpenCode count and cooled Claude fell through to deepseek with no pool_cap skip.
    base = 'ROLE_BRIEF=\nPROXY_AGENT_CAP_OVERRIDE=\n' + CAPS
    exempt = _bash('TYPE=ci-repair-agent\n' + base + f'proxy_agent_pool_blocked {DEEPSEEK} && echo blocked || echo free\n',
                   tmp_path, {'PROXY_AGENT_STUB_POOL_LIVE': '2', 'OPENCODE_PROXY_CAP': '2'})
    assert exempt.stdout.strip() == 'blocked', exempt.stderr
    p0 = _bash('TYPE=hd-builder\n' + base.replace('PROXY_AGENT_CAP_OVERRIDE=\n', 'PROXY_AGENT_CAP_OVERRIDE=P0:14000\n', 1)
               + f'proxy_agent_pool_blocked {DEEPSEEK} && echo blocked || echo free\n',
               tmp_path, {'PROXY_AGENT_STUB_POOL_LIVE': '2', 'OPENCODE_PROXY_CAP': '2'})
    assert p0.stdout.strip() == 'blocked', p0.stderr
    # The same exemption still skips Claude/Grok/Codex caps.
    claude_exempt = _bash('TYPE=ci-repair-agent\n' + base + 'proxy_agent_pool_blocked claude-sonnet-5-5 && echo blocked || echo free\n',
                          tmp_path, {'PROXY_AGENT_STUB_POOL_LIVE': '9', 'CLAUDE_PROXY_CAP': '1'})
    assert claude_exempt.stdout.strip() == 'free', claude_exempt.stderr
    claude = _bash('TYPE=hd-builder\n' + base + 'proxy_agent_pool_blocked claude-haiku-5-5 && echo blocked || echo free\n',
                   tmp_path, {'PROXY_AGENT_STUB_POOL_LIVE': '2', 'OPENCODE_PROXY_CAP': '1'})
    assert claude.stdout.strip() == 'free', claude.stderr


def test_exempt_mid_falls_through_when_opencode_is_at_cap(tmp_path):
    body = (
        'TYPE=ci-repair-agent\nROLE_BRIEF=\nPROXY_AGENT_CAP_OVERRIDE=\n'
        'curl() { printf "%s" "${STUB_SPARK_BODY:-}"; return 0; }\n'  # local Spark health gate: the ids are served, so the cap and cooldown decide
        'proxy_agent_burn_refused() { return 1; }\n'
        'proxy_agent_is_capped() { return 0; }\n'
        'proxy_agent_live_count() { echo 0; }\n'
        'PROXY_AGENT_CLAUDE_CAP=5\n'
        'proxy_agent_sub_usable() { [[ "$1" == claude-sonnet-5-5 ]] && return 1; return 0; }\n'
        + CAPS + PICKER + FREE_GUARD_OFF
        + 'proxy_agent_pick_tier MID && rc=0 || rc=$?\n'
        + 'printf "rc=%s model=%s reason=%s\\n" "$rc" "${PICK_MODEL-}" "${PICK_REASON-}"\n'
    )
    result = _bash(body, tmp_path, {'PROXY_AGENT_STUB_POOL_LIVE': '6', 'OPENCODE_PROXY_CAP': '6', 'QWEN_PROXY_CAP': '8', 'STUB_SPARK_BODY': _spark_body()})
    assert result.returncode == 0, result.stderr
    fields = dict(part.split('=', 1) for part in result.stdout.strip().splitlines()[-1].split(' ', 2))
    # ci-repair-agent is a judgment lane: the picker reorders to the tier's Claude ids (opus -> sonnet), then the Codex ladder,
    # dropping the local Spark id, inkling and gpt-6-luna. Sonnet is cooled here, so the pick is the first surviving entry — never
    # a capped pool id, never an id the tier does not list.
    excluded = set(POOL_IDS) | {ORNITH, QWEN, INKLING, 'gpt-6-luna', 'claude-sonnet-5-5', MUSE}
    judgment = [m for m in ('claude-opus-5-5', 'claude-sonnet-5-5') + tuple(_tier_models()['MID']) if m in _tier_models()['MID']]
    expected = next(m for m in judgment if m not in excluded)
    assert fields['model'] == expected, result.stderr


def test_a_pool_capped_route_is_skipped_and_the_pick_never_falls_to_muse(tmp_path):
    # The shared cap skips a pool member; LIGHT still has a route ahead of it, so the pick is a real model, never Muse.
    light = [m for m in _tier_models()['LIGHT'] if m != MUSE]
    blocked = ' '.join(m for m in light if m in POOL_IDS)
    cooled = ' '.join(m for m in light if m.startswith('claude-') or m.startswith('gpt-'))
    fields, _ = _pick(tmp_path, 'LIGHT', cooled=cooled, blocked=f'{blocked} {MUSE}')
    assert fields['rc'] == '0' and fields['model'] not in POOL_IDS + (MUSE,)
    # Everything non-pool cooled and the pool full: Muse is blocked by the shared cap, the pick is refused.
    mid = [m for m in _tier_models()['MID'] if m != MUSE]
    fields, stderr = _pick(tmp_path, 'MID', cooled=' '.join(m for m in mid if m not in POOL_IDS),
                           blocked=f'{" ".join(POOL_IDS)} {MUSE}')
    assert fields['rc'] == '75' and fields['model'] == ''


# ---- emergency-only gate ----------------------------------------------------------------------------------------

def test_the_pick_follows_the_tier_list_and_lands_on_muse_only_when_all_else_is_cooled(tmp_path):
    order = [m for m in _tier_models()['LIGHT'] if m != MUSE]
    fields, _ = _pick(tmp_path, 'LIGHT')
    assert fields['model'] == order[0]
    fields, _ = _pick(tmp_path, 'LIGHT', cooled=order[0])
    assert fields['model'] == order[1]
    fields, _ = _pick(tmp_path, 'LIGHT', cooled=' '.join(order[:4]))
    assert fields['model'] == order[4]
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
    order = [m for m in _tier_models()['LIGHT'] if m != MUSE]
    rest = ' '.join(m for m in order if m != HY4)
    fields, stderr = _pick(tmp_path, 'LIGHT', fail=HY4, cooled=rest)
    assert fields['rc'] == '75' and fields['model'] == ''
    assert f'{MUSE}:emergency_gate' in stderr


def test_fallback_last_never_lands_on_muse(tmp_path):
    order = [m for m in _tier_models()['LIGHT'] if m != MUSE]
    # Every route cooled, Muse cooled too, gate not enforced: the fallback is the LAST non-Muse entry, never Muse.
    fields, _ = _pick(tmp_path, 'LIGHT', cooled=' '.join(order) + f' {MUSE}', enforced=False)
    assert fields['model'] == order[-1]
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
    # Every BUILD1 route is cooled; grok (not a BUILD1 entry) is healthy, so Muse stays gated.
    build1 = ' '.join(m for m in _tier_models()['BUILD1'] if m != MUSE)
    fields, stderr = _pick(tmp_path, 'BUILD1', cooled=build1)
    assert fields['rc'] == '75' and fields['model'] == ''
    assert f'{MUSE}:emergency_gate' in stderr
    assert 'emergency_gate skip' in stderr and 'blocker=' in stderr
    # Not enforced: the fallback_last pick still never lands on Muse.
    fields, _ = _pick(tmp_path, 'BUILD1', cooled=build1, enforced=False)
    assert fields['model'] == _tier_models()['BUILD1'][-2] and fields['reason'] == 'fallback_last'


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
    # A nested alias drops every OpenCode-pool id, so cooling the tier head moves the pick to the first non-pool entry.
    for tier in ('LIGHT', 'MID'):
        order = _tier_models()[tier]
        head = order[0]
        non_pool = [m for m in order if m not in POOL_IDS + (MUSE,)]
        expected = non_pool[1] if non_pool[0] == head else non_pool[0]
        fields, _ = _pick(tmp_path, tier, nested=True, cooled=head)
        assert fields['model'] == expected, tier


def test_select_resolves_nested_aliases_with_nested_and_the_primary_without():
    calls = re.findall(r'proxy_agent_pick_tier (LIGHT|MID|TOP)( nested)?', SELECT)
    assert [(tier, bool(flag)) for tier, flag in calls] == [('LIGHT', True), ('MID', True), ('TOP', True)]
    assert 'proxy_agent_pick_tier "$tier" ||' in SELECT


# ---- raw primaries: cap and emergency gate on the final resolved primary ----------------------------------------------

def _guard(tmp_path, model, cooled='', fail='', blocked='', env=None):
    body = (PICK_STUBS + 'resolve_model() { echo "$1 200000 32000"; }\n' + PICKER + FREE_GUARD_OFF + GUARD
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
    # The guard refuses on the primary's OWN pool cap, so only the OpenCode pool may be blocked here: a non-pool primary must
    # still be allowed with every OpenCode id at its cap.
    rc, stderr = _guard(tmp_path, model, blocked=' '.join(POOL_IDS))
    assert rc == 'rc=0' and 'REFUSED' not in stderr


def test_cap_zero_turns_the_pool_off_even_when_the_process_list_is_unreadable(tmp_path):
    base = 'TYPE=hd-builder\nROLE_BRIEF=\nPROXY_AGENT_CAP_OVERRIDE=\n' + CAPS + 'proxy_agent_pool_live() { return 1; }\n'
    for model in POOL_IDS:
        probe = f'proxy_agent_pool_blocked {model} && echo blocked || echo free\n'
        zero = _bash(base + probe, tmp_path, {'OPENCODE_PROXY_CAP': '0'})
        assert zero.stdout.strip() == 'blocked', (model, zero.stderr)
        # CTO REVISE ts 1795010788.320629: an unreadable OpenCode count skips the id (blocked), it does not fail open.
        three = _bash(base + probe, tmp_path, {'OPENCODE_PROXY_CAP': '3'})
        assert three.stdout.strip() == 'blocked', (model, three.stderr)
        assert 'pool_cap UNAVAILABLE pool=opencode' in three.stderr
    other = _bash(base + 'proxy_agent_pool_blocked claude-haiku-5-5 && echo blocked || echo free\n', tmp_path,
                  {'CLAUDE_PROXY_CAP': '0'})
    assert other.stdout.strip() == 'free', other.stderr


def test_decorated_opencode_id_uses_the_same_cap_as_the_canonical_id(tmp_path):
    # CTO REVISE ts 1795010788.320629: admission matches the router identity, not the literal argv string.
    base = 'TYPE=hd-builder\nROLE_BRIEF=\nPROXY_AGENT_CAP_OVERRIDE=\n' + CAPS
    full = _bash(base + f"proxy_agent_pool_blocked '{DEEPSEEK}(high)' && echo blocked || echo free\n",
                 tmp_path, {'PROXY_AGENT_STUB_POOL_LIVE': '2', 'OPENCODE_PROXY_CAP': '2'})
    assert full.stdout.strip() == 'blocked', full.stderr
    room = _bash(base + f"proxy_agent_pool_blocked '{DEEPSEEK}[1m]' && echo blocked || echo free\n",
                 tmp_path, {'PROXY_AGENT_STUB_POOL_LIVE': '1', 'OPENCODE_PROXY_CAP': '2'})
    assert room.stdout.strip() == 'free', room.stderr
    glm = _bash(base + "proxy_agent_pool_blocked 'GLM-5.3(low)' && echo blocked || echo free\n",
                tmp_path, {'PROXY_AGENT_STUB_POOL_LIVE': '2'})
    assert glm.stdout.strip() == 'blocked', glm.stderr
    exempt = _bash('TYPE=ci-repair-agent\n' + base.replace('TYPE=hd-builder\n', '', 1)
                   + "proxy_agent_pool_blocked 'deepseek-v4.1-flash(high)' && echo blocked || echo free\n",
                   tmp_path, {'PROXY_AGENT_STUB_POOL_LIVE': '2', 'OPENCODE_PROXY_CAP': '2'})
    assert exempt.stdout.strip() == 'blocked', exempt.stderr
    claude = _bash('TYPE=ci-repair-agent\nROLE_BRIEF=\nPROXY_AGENT_CAP_OVERRIDE=\n' + CAPS
                   + "proxy_agent_pool_blocked 'claude-sonnet-5-5[1m]' && echo blocked || echo free\n",
                   tmp_path, {'PROXY_AGENT_STUB_POOL_LIVE': '9', 'CLAUDE_PROXY_CAP': '1'})
    assert claude.stdout.strip() == 'free', claude.stderr


def test_unavailable_opencode_count_falls_through_to_the_next_route(tmp_path):
    body = (
        'TYPE=hd-builder\nROLE_BRIEF=\nPROXY_AGENT_CAP_OVERRIDE=\n'
        'curl() { printf "%s" "${STUB_SPARK_BODY:-}"; return 0; }\n'  # local Spark health gate: the ids are served, so only the unreadable cap decides
        'proxy_agent_is_capped() { return 1; }\n'
        'proxy_agent_burn_refused() { return 1; }\n'
        'proxy_agent_sub_usable() { [[ "$1" == claude-sonnet-5-5 ]] && return 1; return 0; }\n'
        + CAPS + PICKER + FREE_GUARD_OFF
        + 'proxy_agent_pool_live() { return 1; }\n'
        + 'proxy_agent_pick_tier MID && rc=0 || rc=$?\n'
        + 'printf "rc=%s model=%s reason=%s\\n" "$rc" "${PICK_MODEL-}" "${PICK_REASON-}"\n'
    )
    result = _bash(body, tmp_path, {'OPENCODE_PROXY_CAP': '2', 'STUB_SPARK_BODY': _spark_body()})
    assert result.returncode == 0, result.stderr
    assert 'pool_cap UNAVAILABLE pool=opencode' in result.stderr
    assert 'pool_cap skip model=deepseek-v4.1-flash' in result.stderr
    fields = dict(part.split('=', 1) for part in result.stdout.strip().splitlines()[-1].split(' ', 2))
    # Derived from the live MID list: an unreadable count blocks the OpenCode/locally-counted ids, so the pick is the first
    # entry outside those pools that is not cooled (sonnet).
    mid = _tier_models()['MID']
    expected = next(m for m in mid if m not in set(POOL_IDS) | {ORNITH, QWEN, INKLING, 'claude-sonnet-5-5', MUSE})
    assert fields['rc'] == '0' and fields['model'] == expected, result.stderr


def test_live_counter_uses_the_same_router_cut_as_admission():
    pool_live = _slice('proxy_agent_pool_live() {', 'proxy_agent_pool_blocked() {')
    identity = _slice('proxy_agent_router_identity() {', '# The two restricted upstreams')
    assert 're.split(r"[(\\[]"' in pool_live
    assert 're.split(r"[(\\[]"' in identity
    for model in ('hy4-flash-free', 'deepseek-v4.1-flash', 'glm-5.3', 'muse-spark-1.3-contributor'):
        assert f'"{model}"' in pool_live


def test_auto_continuation_rechecks_opencode_cap_before_the_fresh_spawn():
    gate = SRC.index('if proxy_agent_is_opencode_id "$MODEL_ID"')
    spawn = SRC.index("printf '  %q ' \"$CLAUDE_BIN\" \"${ARGS[@]}\"", gate)
    between = SRC[gate:spawn]
    assert 'proxy_agent_pool_blocked "$MODEL_ID"' in between
    assert 'PROXY_AGENT_CONTINUE_REFUSED' in between
    assert 'proxy_agent_resolve_opencode_cap' in between
    assert 'capacity-stop' in between
    assert 'exit 75' in between
    assert 'break;' not in between
    assert 'declare -f proxy_agent_router_identity' in SRC
    assert 'declare -f proxy_agent_is_opencode_id' in SRC
    assert 'declare -f proxy_agent_pool_live' in SRC
    assert 'declare -f proxy_agent_pool_blocked' in SRC
    assert 'declare -f proxy_agent_resolve_opencode_cap' in SRC
    # The free-model continuation denial still precedes the loop.
    assert SRC.index('PROXY_AGENT_NO_CONTINUE=1;; esac') < SRC.index("printf 'while [[ ( \"$rc\" == 0")
    # Last-valid file assignment is gone; the shared resolver returns on the first valid value.
    assert '&& OPENCODE_PROXY_CAP="$_v"' not in SRC
    resolver = _slice('proxy_agent_resolve_opencode_cap() {', '# Daily dial')
    assert resolver.index('return 0') < resolver.index("printf '2'")


def test_opencode_cap_resolution_is_env_then_first_file_then_two(tmp_path):
    # PM decision ts 1795012176.225749. Not executed on the host by the suite; the generated-runner
    # proof is a separate no-provider run.
    fn = _slice('proxy_agent_resolve_opencode_cap() {', '# Daily dial')
    body = fn + 'printf "%s\\n" "$(proxy_agent_resolve_opencode_cap "${OPENCODE_PROXY_CAP:-}")"\n'
    caps = tmp_path / '.claude' / 'state' / 'proxy-caps.env'
    caps.parent.mkdir(parents=True)
    caps.write_text('OPENCODE_PROXY_CAP=nope\nOPENCODE_PROXY_CAP=1\nOPENCODE_PROXY_CAP=9\n')
    env_wins = _bash(body, tmp_path, {'OPENCODE_PROXY_CAP': '4'})
    assert env_wins.returncode == 0 and env_wins.stdout.strip() == '4', env_wins.stderr
    first = _bash(body, tmp_path, {})
    assert first.returncode == 0 and first.stdout.strip() == '1', first.stderr
    caps.write_text('GROK_PROXY_CAP=3\nOPENCODE_PROXY_CAP=abc\n')
    default = _bash(body, tmp_path, {})
    assert default.returncode == 0 and default.stdout.strip() == '2', default.stderr


def test_opencode_cap_zero_is_not_lifted_by_exemption(tmp_path):
    # Cap 0 turns the pool off for every launch. Role exemptions and P0 still apply to other pools.
    base = ('ROLE_BRIEF=\nPROXY_AGENT_CAP_OVERRIDE=\n' + CAPS + 'proxy_agent_pool_live() { return 1; }\n')
    probe = f'proxy_agent_pool_blocked {HY4} && echo blocked || echo free\n'
    exempt = _bash('TYPE=mop-main-repair\n' + base + probe, tmp_path, {'OPENCODE_PROXY_CAP': '0'})
    assert exempt.stdout.strip() == 'blocked', exempt.stderr
    p0 = _bash('TYPE=hd-builder\n' + base.replace('PROXY_AGENT_CAP_OVERRIDE=\n', 'PROXY_AGENT_CAP_OVERRIDE=P0:13900\n', 1) + probe,
               tmp_path, {'OPENCODE_PROXY_CAP': '0'})
    assert p0.stdout.strip() == 'blocked', p0.stderr
    claude = _bash('TYPE=mop-main-repair\n' + base + 'proxy_agent_pool_blocked claude-haiku-5-5 && echo blocked || echo free\n',
                   tmp_path, {'PROXY_AGENT_STUB_POOL_LIVE': '9', 'CLAUDE_PROXY_CAP': '0'})
    assert claude.stdout.strip() == 'free', claude.stderr


def test_an_exemption_is_not_muse_permission(tmp_path):
    # A cap-exempt launch (pool_blocked never true) still needs every eligible route known exhausted for Muse.
    body = (PICK_STUBS + 'proxy_agent_pool_blocked() { return 1; }\nresolve_model() { echo "$1 200000 32000"; }\n'
            + PICKER + FREE_GUARD_OFF + GUARD + f'MODEL={MUSE}\nproxy_agent_primary_pool_guard && echo allowed || echo refused\n')
    result = _bash(body, tmp_path, {'STUB_COOLED': '', 'STUB_FAIL': '', 'STUB_BLOCKED': ''})
    assert result.stdout.strip() == 'refused', result.stderr


def test_dial_ownership_is_outside_this_repo():
    # proxy-burn-dial.py is deployed-only tooling under ~/.claude/scripts/cost (PM-owned), not a shared asset: the launcher only
    # reads OPENCODE_PROXY_CAP from the file the dial writes, and must keep reading it.
    manifest = (Path(__file__).parents[1] / 'shared-assets/manifest.json').read_text()
    assert 'proxy-burn-dial' not in manifest
    assert 'OPENCODE_PROXY_CAP' in SRC
