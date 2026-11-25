"""Three-level model routing in proxy-agent.sh (Rajiv C0ALZJHGE49 thread 1795351517.305319: ts 1795354409.166279 levels and types;
principle ts 1795353913.416459 "stronger models should drive weaker ones"; 24 Nov value-per-$ order Rajiv D0AMF0XE6TS thread
1795485653.099709, ts 1795486375.339929: Opus (77x) first, Grok (11-13x) last; OpenCode ring pinned to a single id, glm-5.3).

    TOP  claude-opus-5-5 -> ultra/claude-opus-5-5 -> claude-fable-5-2 -> gpt-6.2-sol -> sarvam-kimi-k3 -> grok-4.7
    MID  glm-5.3 -> deepseek-v4.1-flash -> hy4-flash-free -> claude-sonnet-5-5 -> ultra/claude-sonnet-5-5 -> gpt-6.1-terra   (NO ornith since 24 Nov; NO Sarvam: LOW only)
    LOW  ornith-1.5-35b-a3b -> sarvam-glm-5.3 -> sarvam-deepseek-v4.1-flash -> sarvam-105b -> sarvam-gemma4 -> gpt-6-luna -> claude-haiku-5-5
         (light lanes only; the only level that lists ornith and the Sarvam serverless ids)

Drives the REAL functions, extracted by NAME from the script under test (PROXY_AGENT_TEST_SCRIPT overrides the path), one bash process per
launch. Environment-touching helpers are stubbed: curl (Spark health), sleep, the live Claude count, the cooldown probe (proxy_agent_sub_usable),
the burn gate and the primary guards. The real cap/exemption logic (proxy_agent_pool_blocked, proxy_agent_cap_exempt) runs against
PROXY_AGENT_STUB_POOL_LIVE. HOME is a scratch dir per launch, so no proxy-no-codex flag, caps file or rotation cursor can leak in.
"""
import json
import os
import pathlib
import re
import subprocess
import tempfile
import types
import unittest

SCRIPT = pathlib.Path(os.environ.get('PROXY_AGENT_TEST_SCRIPT', pathlib.Path.home() / '.claude/scripts/proxy-agent.sh'))
SOURCE = SCRIPT.read_text()
LINES = SOURCE.split('\n')


def extract(name):
    start = next(i for i, l in enumerate(LINES) if l.startswith(name + '() {'))
    if LINES[start].rstrip().endswith('}') and LINES[start].count('{') == LINES[start].count('}'):
        return LINES[start]
    end = next(i for i in range(start + 1, len(LINES)) if LINES[i] == '}')
    return '\n'.join(LINES[start:end + 1])


def var(name):
    return next(l for l in LINES if l.startswith(name + '='))


FUNCS = [
    'proxy_agent_type_level', 'proxy_agent_tier_models', 'proxy_agent_is_judgment_lane', 'proxy_agent_router_identity',
    'proxy_agent_is_free_trial_id', 'proxy_agent_free_guard_refuses', 'proxy_agent_is_opencode_id', 'proxy_agent_is_emergency_muse',
    'proxy_agent_opencode_rotation_file', 'proxy_agent_opencode_rotate', 'proxy_agent_opencode_record', 'proxy_agent_ring_rotate',
    'proxy_agent_is_sarvam_id', 'proxy_agent_sarvam_kimi_state_file', 'proxy_agent_sarvam_kimi_flag', 'proxy_agent_sarvam_kimi_gated',
    'proxy_agent_sarvam_kimi_mark_unavailable', 'proxy_agent_sarvam_rotation_file', 'proxy_agent_sarvam_rotate', 'proxy_agent_sarvam_record',
    'proxy_agent_pick_tier',
    'proxy_agent_select', 'proxy_agent_resolve_type', 'proxy_agent_canonical_id', 'proxy_agent_alias_for_id', 'resolve_model',
    'proxy_agent_is_capped', 'proxy_agent_cap_exempt', 'proxy_agent_pool_live', 'proxy_agent_pool_blocked',
    'proxy_agent_route_exhausted', 'proxy_agent_emergency_open',
]

STUBS = r'''
PROXY_AGENT_CLAUDE_CAP=8
PROXY_BURN_GATE=shadow
curl() { if [[ -n "${SPARK_UP:-}" ]]; then printf '"ornith-1.5-35b-a3b" "inkling-2-mini"'; return 0; fi; return 22; }
sleep() { :; }
proxy_agent_live_count() { printf 0; }
proxy_agent_sub_usable() { case " $UNAVAILABLE " in *" $1 "*) return 1;; esac; case " $PROBEFAIL " in *" $1 "*) return 2;; esac; return 0; }
proxy_agent_burn_refused() { return 1; }
proxy_agent_burn_enforced() { return 1; }
proxy_agent_primary_pool_guard() { return 0; }
proxy_agent_primary_burn_check() { return 0; }
if [[ -n "${BLOCKED+x}" ]]; then proxy_agent_pool_blocked() { case " $BLOCKED " in *" $1 "*) return 0;; *) return 1;; esac; }; fi
if [[ -z "${REAL_EMERGENCY:-}" ]]; then proxy_agent_emergency_open() { return 1; }; fi
'''

PRELUDE = '\n'.join([var('PROXY_AGENT_OPENCODE_RING'), var('PROXY_AGENT_SARVAM_RING'), var('PROXY_AGENT_FREE_TRIAL_IDS')] + [extract(f) for f in FUNCS]) + '\n' + STUBS

DROP = {'CLAUDE_PROXY_CAP', 'GROK_PROXY_CAP', 'OPENCODE_PROXY_CAP', 'CODEX_PROXY_CAP', 'SPARK_PROXY_CAP', 'QWEN_PROXY_CAP',
        'INKLING_PROXY_CAP', 'ULTRA_PROXY_CAP', 'SARVAM_PROXY_CAP', 'SARVAM_KIMI_ENABLED', 'PROXY_AGENT_SARVAM_KIMI_STATE_FILE', 'PROXY_AGENT_SARVAM_ROTATION_FILE', 'PROXY_AGENT_CAP_OVERRIDE', 'PROXY_AGENT_DRY_RUN', 'PROXY_AGENT_STUB_POOL_LIVE', 'PROXY_AGENT_OPENCODE_ROTATION_FILE',
        'PROXY_AGENT_TYPE', 'SPARK_UP', 'BLOCKED', 'REAL_EMERGENCY', 'PROXY_AGENT_NO_SUBSCRIPTION', 'PA_EMERGENCY_VERDICT', 'UNAVAILABLE', 'PROBEFAIL'}

LOW = ['ornith-1.5-35b-a3b', 'sarvam-glm-5.3', 'sarvam-deepseek-v4.1-flash', 'sarvam-105b', 'sarvam-gemma4', 'gpt-6-luna', 'claude-haiku-5-5']
# 24 Nov value-per-$ order (Rajiv D0AMF0XE6TS thread 1795485653.099709, ts 1795486375.339929): MID starts on glm-5.3 (the OpenCode
# ring now holds only that id, PROXY_AGENT_OPENCODE_RING="glm-5.3", so DeepSeek/hy4 are fallbacks, not the start), TOP is Opus first
# and Grok last. The order is pinned here as literals on purpose: deriving LOW/MID/TOP from proxy_agent_tier_models would make every
# walk and cooled-chain below compare the script against itself (tautology, no regression detection). These lists ARE the contract.
# 25 Nov: the dev@scribie ULTRA credential (own CLIProxyAPI `ultra` prefix route, own pool, ULTRA_PROXY_CAP 4; Rajiv D0AMF0XE6TS
# thread 1795576437.379619, ts 1795590119.110289) sits right after the team id of the same model in MID and TOP: it takes over when the team
# id is capped or cooled, and the team pool never sees it. It is not a claude-* id, so a team cap, a team 429 and the team switch ignore it.
ULTRA_SONNET, ULTRA_OPUS = 'ultra/claude-sonnet-5-5', 'ultra/claude-opus-5-5'
MID = ['glm-5.3', 'deepseek-v4.1-flash', 'hy4-flash-free', 'claude-sonnet-5-5', ULTRA_SONNET, 'gpt-6.1-terra']
TOP = ['claude-opus-5-5', ULTRA_OPUS, 'claude-fable-5-2', 'gpt-6.2-sol', 'sarvam-kimi-k3', 'grok-4.7']
# "Team Opus/Sonnet is out" now also needs the Ultra id of the same model out, or the walk lands on Ultra first (that is the design).
OPUS_DOWN = ['claude-opus-5-5', ULTRA_OPUS]
SONNET_DOWN = ['claude-sonnet-5-5', ULTRA_SONNET]
# base_env forces SARVAM_KIMI_ENABLED=1 so the whole chain is exercised here; test_proxy_agent_sarvam_pool.py covers the availability gate itself.
KIMI = 'sarvam-kimi-k3'
SARVAM_IDS = ['sarvam-glm-5.3', 'sarvam-deepseek-v4.1-flash', 'sarvam-105b', 'sarvam-gemma4']
MUSE = 'muse-spark-1.3-contributor'

TOP_TYPES = ['hd-planner', 'hd-investigator', 'pm-admission-reviewer', 'codex-code-reviewer', 'codex-plan-reviewer', 'pm-plan-reviewer',
             'heydonna-ux-reviewer', 'pipeline-contract-reviewer', 'explore-issue-agent', 'growth', 'donna-influencer',
             'marketing-site-builder', 'blog-post-builder', 'spark-lab']  # spark-lab: Spark dev-model lab (Rajiv D0AMF0XE6TS ts 1795503812.405199); never takes a Spark model as its own driver
# 24 Nov (Rajiv C0ALZJHGE49 ts 1795484559.513619): LOW = the light lanes (cleanup, triage); ornith is listed only there.
LOW_TYPES = ['cleanup-pr-runner', 'pm-triage-agent', 'pm-backlog-promoter']
MID_TYPES = ['claude', 'hd-builder', 'hd-proof-qa', 'remote-test-runner', 'remote-qa-runner', 'ci-repair-agent',
             'customer-artifact-investigator', 'e2e-failure-investigator', 'mop-main-repair', 'autoscaler-main-repair', 'pm-ops-tooling',
             'main-red-skip', 'superproofer']
UNLISTED_TYPES = ['vendor-new-agent', 'plan-agent', 'fullstack-dev', 'pm-hotfix-typo']
# Judgment lanes start on Claude and never on the local model (proxy_agent_is_judgment_lane).
JUDGMENT_MID = ['ci-repair-agent', 'e2e-failure-investigator', 'customer-artifact-investigator']
RETIRED_TIERS = ['PLAN', 'LIGHT', 'BUILD1', 'BUILD2', 'BUILD3', 'PROOF1', 'PROOF2', 'REVIEW', 'RAW']


def base_env(home, **extra):
    env = {k: v for k, v in os.environ.items() if k not in DROP}
    env.update(HOME=str(home), PROXY_AGENT_STUB_POOL_LIVE='0', SARVAM_KIMI_ENABLED='1', UNAVAILABLE='', PROBEFAIL='')
    env.update({k: str(v) for k, v in extra.items()})
    return env


def sh(body, home=None, **env):
    with tempfile.TemporaryDirectory() as tmp:
        done = subprocess.run(['bash', '-c', PRELUDE + '\n' + body], env=base_env(home or tmp, **env), text=True, capture_output=True, check=True)
    return done.stdout.strip()


ROUTE_BODY = r'''
MODEL="$REQUESTED" TYPE="$ROLE" ROLE_BRIEF="$BRIEF" RUNG="$RUNG"
proxy_agent_resolve_type
proxy_agent_select; rc=$?
printf '%s|%s|%s|%s|%s|%s|%s|%s|%s\n' "$TYPE" "$SELECT_TIER" "$MODEL" "$SELECT_REASON" "$SELECT_RUNG" "$rc" "${HAIKU_MODEL:-}" "${SONNET_MODEL:-}" "${OPUS_MODEL:-}"
'''


def route(role, first_line='', model='auto', rung='', unavailable=(), probefail=(), spark=True, blocked=None, home=None, **env):
    """proxy_agent_resolve_type + proxy_agent_select exactly as the launcher runs them."""
    with tempfile.TemporaryDirectory() as tmp:
        brief = pathlib.Path(tmp) / 'brief.txt'
        brief.write_text(first_line + '\n')
        extra = dict(REQUESTED=model, ROLE=role, BRIEF=str(brief), RUNG=str(rung), UNAVAILABLE=' '.join(unavailable), PROBEFAIL=' '.join(probefail))
        if spark:
            extra['SPARK_UP'] = '1'
        if blocked is not None:
            extra['BLOCKED'] = ' '.join(blocked)
        extra.update(env)
        done = subprocess.run(['bash', '-c', PRELUDE + '\n' + ROUTE_BODY], env=base_env(home or tmp, **extra), text=True, capture_output=True, check=True)
    t, tier, m, reason, rg, rc, haiku, sonnet, opus = done.stdout.strip().split('|')
    return types.SimpleNamespace(type=t, tier=tier, model=m, reason=reason, rung=rg, rc=rc, haiku=haiku, sonnet=sonnet, opus=opus, err=done.stderr)


class LevelTable(unittest.TestCase):
    def test_every_listed_type_maps_to_its_level(self):
        for level, roles in (('TOP', TOP_TYPES), ('LOW', LOW_TYPES), ('MID', MID_TYPES)):
            for role in roles:
                with self.subTest(role=role):
                    self.assertEqual(sh('proxy_agent_type_level "%s"' % role), level)

    def test_every_reviewer_suffix_is_top(self):
        for role in ('codex-qa-reviewer', 'ux-reviewer', 'security-reviewer', 'tech-lead-reviewer', 'pm-arch-reviewer', 'brand-new-reviewer'):
            with self.subTest(role=role):
                self.assertEqual(sh('proxy_agent_type_level "%s"' % role), 'TOP')

    def test_unlisted_type_defaults_to_mid(self):
        for role in UNLISTED_TYPES + ['']:
            with self.subTest(role=role):
                self.assertEqual(sh('proxy_agent_type_level "%s"' % role), 'MID')

    def test_unlisted_type_never_defaults_to_top_even_for_a_plan_brief(self):
        # The old "MODE: PLAN -> TOP for an unlisted type" default is gone: planning types are listed, everything else is MID.
        got = route('vendor-new-agent', 'MODE: PLAN')
        self.assertEqual((got.type, got.tier, got.model), ('vendor-new-agent', 'MID', MID[0]))

    def test_level_lists(self):
        # TYPE unset strips ornith from LOW (the strip only keeps it for LOW-level lanes), so the bare-list read of LOW
        # is the Sarvam ladder; each level's own lane sees the full list (Selection walks pin those).
        for level, expected in (('LOW', LOW), ('MID', MID), ('TOP', TOP)):
            with self.subTest(level=level):
                bare = sh('proxy_agent_tier_models ' + level).split()
                if level == 'LOW':
                    self.assertEqual(bare, [m for m in LOW if m != 'ornith-1.5-35b-a3b'] + [MUSE])
                else:
                    self.assertEqual(bare, expected + [MUSE])

    def test_no_codex_flag_drops_gpt_ids_from_every_level(self):
        with tempfile.TemporaryDirectory() as home:
            state = pathlib.Path(home) / '.claude/state'
            state.mkdir(parents=True)
            (state / 'proxy-no-codex').write_text('')
            for level, expected in (('LOW', LOW), ('MID', MID), ('TOP', TOP)):
                with self.subTest(level=level):
                    bare = sh('proxy_agent_tier_models ' + level, home=home).split()
                    if level == 'LOW':
                        expected = [m for m in LOW if m != 'ornith-1.5-35b-a3b']
                    self.assertEqual(bare, [m for m in expected + [MUSE] if not m.startswith('gpt-')])

    def test_retired_tier_names_are_not_pickable(self):
        for tier in RETIRED_TIERS:
            with self.subTest(tier=tier):
                self.assertEqual(sh('TYPE=claude; proxy_agent_pick_tier %s 2>/dev/null; echo rc=$?' % tier), 'rc=1')

    def test_no_gemini_ids_in_any_level(self):
        # PM 2026-11-14: Google banned the Antigravity logins (TOS_VIOLATION); no level may list a gemini-* id.
        lines = [l for l in extract('proxy_agent_tier_models').splitlines() if re.match(r'\s+(LOW|MID|TOP)\) models="', l)]
        self.assertEqual(len(lines), 3)
        for line in lines:
            with self.subTest(line=line.strip()):
                self.assertNotIn('gemini', line)

    def test_sol_is_62_and_terra_stays_in_mid(self):
        for level in ('LOW', 'MID', 'TOP'):
            self.assertNotIn('gpt-6.1-sol', sh('proxy_agent_tier_models ' + level).split())
        self.assertIn('gpt-6.2-sol', sh('proxy_agent_tier_models TOP').split())
        self.assertIn('gpt-6.1-terra', sh('proxy_agent_tier_models MID').split())


class Selection(unittest.TestCase):
    def test_each_listed_type_starts_on_its_levels_first_usable_model(self):
        for role in TOP_TYPES:
            with self.subTest(role=role):
                got = route(role)
                self.assertEqual((got.tier, got.model, got.reason, got.rc), ('TOP', TOP[0], 'first_usable', '0'))
        for role in MID_TYPES:
            expected = 'claude-sonnet-5-5' if role in JUDGMENT_MID else MID[0]
            with self.subTest(role=role):
                got = route(role)
                self.assertEqual((got.tier, got.model, got.reason, got.rc), ('MID', expected, 'first_usable', '0'))
        for role in LOW_TYPES:
            with self.subTest(role=role):
                got = route(role)
                self.assertEqual((got.tier, got.model, got.reason, got.rc), ('LOW', LOW[0], 'first_usable', '0'))
        for role in UNLISTED_TYPES:
            with self.subTest(role=role):
                got = route(role)
                self.assertEqual((got.tier, got.model, got.rc), ('MID', MID[0], '0'))

    def walk(self, role, chain, **kw):
        for i, expected in enumerate(chain):
            with self.subTest(role=role, pos=i):
                got = route(role, unavailable=chain[:i], **kw)
                self.assertEqual(got.model, expected)
                self.assertEqual(got.reason, 'first_usable' if i == 0 else 'cooled:' + ','.join(chain[:i]))
        with self.subTest(role=role, pos='all-cooled'):
            got = route(role, unavailable=chain, **kw)
            self.assertEqual((got.model, got.reason, got.rc), (chain[-1], 'fallback_last', '0'))

    def test_top_order_opus_then_fable_then_sol_then_kimi_then_grok(self):
        self.walk('hd-planner', TOP)
        self.walk('pm-admission-reviewer', TOP)

    def test_mid_order_glm_then_deepseek_then_hy4_then_claude_then_terra(self):
        self.walk('hd-builder', MID)
        self.walk('hd-proof-qa', MID)

    def test_low_order_ornith_then_sarvam_then_luna_then_haiku(self):
        self.walk('cleanup-pr-runner', LOW)

    def test_fallback_last_stays_inside_the_level(self):
        # Everything cooled: the best-effort pick is the LAST id of the SAME level, never an id of another level.
        self.assertEqual(route('hd-builder', unavailable=MID + [MUSE]).model, 'gpt-6.1-terra')
        self.assertEqual(route('cleanup-pr-runner', unavailable=LOW).model, 'claude-haiku-5-5')
        self.assertEqual(route('hd-planner', unavailable=TOP).model, 'grok-4.7')

    def test_spark_down_skips_ornith_on_mid_and_low(self):
        got = route('hd-builder', spark=False)
        self.assertEqual((got.model, got.reason), ('glm-5.3', 'first_usable'))  # ornith is not listed in MID at all (24 Nov)
        got = route('cleanup-pr-runner', spark=False)
        self.assertEqual((got.model, got.reason), ('sarvam-glm-5.3', 'cooled:ornith-1.5-35b-a3b:host_down'))

    def test_free_model_guard_keeps_hy4_for_builders_and_provers_only(self):
        cooled = ['glm-5.3', 'deepseek-v4.1-flash']
        self.assertEqual(route('hd-builder', unavailable=cooled).model, 'hy4-flash-free')
        self.assertEqual(route('hd-proof-qa', unavailable=cooled).model, 'hy4-flash-free')
        got = route('claude', unavailable=cooled)
        self.assertEqual(got.model, 'claude-sonnet-5-5')
        self.assertIn('hy4-flash-free:free_model_guard', got.reason)
        # Sarvam ids sit in LOW only now (Rajiv ts 1795420232.692599), so a cooled non-builder MID lane goes straight to Claude.
        got = route('claude', unavailable=cooled + SARVAM_IDS)
        self.assertEqual(got.model, 'claude-sonnet-5-5')
        self.assertIn('hy4-flash-free:free_model_guard', got.reason)

    def test_judgment_lanes_start_on_claude_not_the_local_model(self):
        for role in JUDGMENT_MID:
            with self.subTest(role=role):
                self.assertEqual(route(role).model, 'claude-sonnet-5-5')
        # A P0 launch (PA_P0_REF, captured from the P0:<issue> override at launch) makes any lane a judgment lane; it gets no cap bypass.
        self.assertEqual(route('claude', PA_P0_REF='P0:13907').model, 'claude-sonnet-5-5')
        self.assertEqual(route('hd-investigator').model, 'claude-opus-5-5')

    def test_opencode_ring_is_pinned_to_glm_single_id(self):
        # Rajiv 24 Nov ts 1795486375.339929: PROXY_AGENT_OPENCODE_RING holds only glm-5.3, so MID no longer rotates onto DeepSeek
        # (fewer than two ring ids present: proxy_agent_opencode_rotate returns the list unchanged). DeepSeek/hy4 stay as fallbacks.
        self.assertEqual(var('PROXY_AGENT_OPENCODE_RING').split('=', 1)[1].split('#', 1)[0].strip().strip('"'), 'glm-5.3')
        with tempfile.TemporaryDirectory() as home:
            got = [route('hd-builder', spark=False, home=home).model for _ in range(4)]
            cursor = pathlib.Path(home) / '.claude/state/proxy-opencode-rotation'
            self.assertEqual(cursor.read_text().strip(), 'glm-5.3')  # the pick still records itself; rotation stays a no-op reorder
        self.assertEqual(got, ['glm-5.3', 'glm-5.3', 'glm-5.3', 'glm-5.3'])

    def test_top_and_low_never_touch_the_rotation_cursor(self):
        with tempfile.TemporaryDirectory() as home:
            route('hd-planner', home=home)
            route('cleanup-pr-runner', spark=False, home=home)
            self.assertFalse((pathlib.Path(home) / '.claude/state/proxy-opencode-rotation').exists())


class FullLevelRefuses(unittest.TestCase):
    """A full level REFUSES (rc 75, the PM queues the launch). It never borrows from the level above or below."""

    def test_full_mid_refuses_and_never_falls_to_top(self):
        for role in ('hd-builder', 'hd-proof-qa', 'claude', 'ci-repair-agent', 'vendor-new-agent'):
            with self.subTest(role=role):
                got = route(role, blocked=MID + [MUSE])
                self.assertEqual(got.rc, '75')
                self.assertEqual(got.model, 'auto')  # nothing was picked
                self.assertIn('REFUSED', got.err)
                self.assertNotIn(got.model, TOP)

    def test_full_mid_leaves_top_untouched_but_unused(self):
        # TOP is fully available here: the refusal is not because nothing exists, it is because MID work does not run on TOP.
        got = route('hd-builder', blocked=MID)
        self.assertEqual(got.rc, '75')
        self.assertTrue(all(m not in got.err for m in ('model=claude-opus-5-5', 'model=claude-fable-5-2', 'model=gpt-6.2-sol')))

    def test_full_low_refuses_and_never_falls_to_mid_or_top(self):
        got = route('cleanup-pr-runner', blocked=LOW + [MUSE])
        self.assertEqual((got.rc, got.model), ('75', 'auto'))

    def test_full_top_refuses_and_never_falls_down(self):
        got = route('hd-planner', blocked=TOP + [MUSE])
        self.assertEqual((got.rc, got.model), ('75', 'auto'))

    def test_real_pool_caps_at_zero_refuse_mid(self):
        # The real proxy_agent_pool_blocked (not the stub): every MID pool at cap 0, TOP pools untouched in name only (claude/codex pools are shared with MID).
        # hd-builder keeps the approved free-model exception, so it still launches on hy4-flash-free; the refusal case is pinned on non-free lanes.
        got = route('claude', blocked=None, SPARK_PROXY_CAP=0, INKLING_PROXY_CAP=0, OPENCODE_PROXY_CAP=0, SARVAM_PROXY_CAP=0, CLAUDE_PROXY_CAP=0, CODEX_PROXY_CAP=0, ULTRA_PROXY_CAP=0)
        self.assertEqual((got.rc, got.model), ('75', 'auto'))
        self.assertIn('REFUSED', got.err)


class Escalation(unittest.TestCase):
    """A role moves up a level ONLY on explicit escalation: --rung 2+ or an explicit opus/sol."""

    def test_builder_rungs(self):
        got = route('hd-builder')
        self.assertEqual((got.tier, got.rung), ('MID', '1'))
        got = route('hd-builder', rung=1)
        self.assertEqual((got.tier, got.rung), ('MID', '1'))
        for rung in (2, 3, 9):
            with self.subTest(rung=rung):
                got = route('hd-builder', rung=rung)
                self.assertEqual((got.tier, got.model, got.rung, got.rc), ('TOP', 'claude-opus-5-5', '3', '0'))
                self.assertNotIn('ignored', got.err)

    def test_prover_rungs(self):
        self.assertEqual((route('hd-proof-qa').tier, route('hd-proof-qa').rung), ('MID', '1'))
        for rung in (2, 3):
            with self.subTest(rung=rung):
                got = route('hd-proof-qa', rung=rung)
                self.assertEqual((got.tier, got.model, got.rung), ('TOP', 'claude-opus-5-5', '2'))

    def test_other_mid_roles_escalate_one_level_per_rung(self):
        for role in ('claude', 'vendor-new-agent'):
            with self.subTest(role=role):
                got = route(role, rung=2)
                self.assertEqual((got.tier, got.model, got.rung), ('TOP', 'claude-opus-5-5', ''))

    def test_low_role_rung_two_is_mid_and_rung_three_is_top(self):
        self.assertEqual(route('cleanup-pr-runner', rung=2).tier, 'MID')
        self.assertEqual(route('cleanup-pr-runner', rung=3).tier, 'TOP')

    def test_top_role_ignores_rung_with_a_note(self):
        got = route('hd-planner', rung=2)
        self.assertEqual(got.tier, 'TOP')
        self.assertIn('--rung 2 ignored (type hd-planner is already TOP)', got.err)

    def test_escalation_works_when_mid_is_full_but_only_because_it_was_explicit(self):
        # Full MID refuses by default (FullLevelRefuses); the explicit --rung 2 is what reaches TOP.
        self.assertEqual(route('hd-builder', blocked=MID).rc, '75')
        got = route('hd-builder', rung=2, blocked=MID)
        self.assertEqual((got.rc, got.tier, got.model), ('0', 'TOP', 'claude-opus-5-5'))

    def test_explicit_opus_is_a_top_lever(self):
        for role, rung in (('hd-builder', '3'), ('hd-proof-qa', '2'), ('growth', ''), ('claude', '')):
            with self.subTest(role=role):
                got = route(role, model='opus')
                self.assertEqual((got.tier, got.model, got.rung), ('TOP', 'claude-opus-5-5', rung))
                # With Opus cooled the same lever walks the rest of TOP in the value-per-$ order (24 Nov): Fable, Sol, Kimi, then Grok.
                self.assertEqual(route(role, model='opus', unavailable=['claude-opus-5-5']).model, ULTRA_OPUS)  # 25 Nov: the Ultra Opus is the overflow behind the team Opus
                self.assertEqual(route(role, model='opus', unavailable=OPUS_DOWN).model, 'claude-fable-5-2')
                self.assertEqual(route(role, model='opus', unavailable=OPUS_DOWN + ['claude-fable-5-2']).model, 'gpt-6.2-sol')
                self.assertEqual(route(role, model='opus', unavailable=OPUS_DOWN + ['claude-fable-5-2', 'gpt-6.2-sol']).model, KIMI)
                self.assertEqual(route(role, model='opus', unavailable=OPUS_DOWN + ['claude-fable-5-2', 'gpt-6.2-sol', KIMI]).model, 'grok-4.7')

    def test_explicit_sol_is_the_exact_62_id_and_stays_raw(self):
        for role, rung in (('hd-builder', '3'), ('hd-proof-qa', '2'), ('hd-planner', ''), ('growth', '')):
            for unavailable in ((), ('gpt-6.2-sol',), ('claude-opus-5-5', 'gpt-6.1-terra')):
                with self.subTest(role=role, unavailable=unavailable):
                    got = route(role, model='sol', unavailable=unavailable)
                    # RAW: a tier here would let the picker return another id and override the alias.
                    self.assertEqual((got.tier, got.model, got.reason, got.rc), ('RAW', 'sol', 'first_usable', '0'))
                    self.assertEqual(got.rung, rung)  # conveyor roles keep their top rung so auto-escalation reports top_rung
        self.assertEqual(sh('resolve_model sol'), 'gpt-6.2-sol 1050000 128000')

    def test_explicit_model_ignores_rung_with_a_note(self):
        self.assertIn('--rung 2 ignored (explicit --model sol)', route('hd-builder', model='sol', rung=2).err)
        self.assertIn('--rung 3 ignored (explicit --model opus)', route('hd-builder', model='opus', rung=3).err)

    def test_exact_ids_stay_raw(self):
        for role in ('hd-builder', 'growth', 'claude'):
            for model in ('vendor-custom', 'claude-opus-5-5', 'gpt-6.2-sol', 'astra', 'gpt-6-astra'):
                with self.subTest(role=role, model=model):
                    got = route(role, model=model)
                    self.assertEqual((got.tier, got.model), ('RAW', model))

    def test_never_escalates_by_default(self):
        for role in MID_TYPES + UNLISTED_TYPES:
            with self.subTest(role=role):
                self.assertEqual(route(role).tier, 'MID')
                # Even with every MID id cooled the pick is MID's fallback, not a TOP id.
                got = route(role, unavailable=MID + [MUSE])
                self.assertNotIn(got.model, TOP)


class Aliases(unittest.TestCase):
    def test_luna_and_haiku_are_low(self):
        for alias in ('luna', 'haiku'):
            with self.subTest(alias=alias):
                self.assertEqual(route('hd-builder', model=alias).tier, 'LOW')
        # With Spark up, the LOW start is ornith; with Spark down the walk continues into the Sarvam ids before luna.
        self.assertEqual(route('growth', model='luna', spark=False, unavailable=SARVAM_IDS).model, 'gpt-6-luna')

    def test_terra_and_sonnet_are_mid(self):
        for alias in ('terra', 'sonnet'):
            with self.subTest(alias=alias):
                self.assertEqual(route('growth', model=alias).tier, 'MID')
        self.assertEqual(route('hd-builder', model='terra').rung, '1')

    def test_opus_is_top_and_sol_stays_exact(self):
        self.assertEqual(route('claude', model='opus').tier, 'TOP')
        self.assertEqual(route('claude', model='sol').tier, 'RAW')

    def test_nested_alias_exports_follow_the_levels_without_opencode_or_spark(self):
        got = route('claude')
        # LOW nested drops ornith and the Sarvam ids (Spark/Sarvam), MID nested drops OpenCode, TOP nested is Opus first (24 Nov value order).
        self.assertEqual((got.haiku, got.sonnet, got.opus), ('gpt-6-luna', 'claude-sonnet-5-5', 'claude-opus-5-5'))
        got = route('claude', unavailable=['claude-opus-5-5'])
        self.assertEqual(got.opus, 'claude-fable-5-2')  # nested aliases never take the Ultra id: the cap counter sees primaries only
        got = route('claude', unavailable=['claude-sonnet-5-5'])
        self.assertEqual(got.sonnet, 'gpt-6.1-terra')
        got = route('claude', unavailable=['claude-opus-5-5', 'claude-fable-5-2'])
        self.assertEqual(got.opus, 'gpt-6.2-sol')
        got = route('claude', unavailable=['claude-opus-5-5', 'claude-fable-5-2', 'gpt-6.2-sol', KIMI])
        self.assertEqual(got.opus, 'grok-4.7')


class GrokTop(unittest.TestCase):
    """Grok is the LAST id of TOP now (24 Nov value-per-$ order): these pin the cooldown levers that demote everything ahead of it."""

    def test_grok_cooldown_402_falls_through_the_top_ladder_to_grok(self):
        got = route('hd-planner', unavailable=['claude-opus-5-5'])
        self.assertEqual((got.model, got.reason), (ULTRA_OPUS, 'cooled:claude-opus-5-5'))
        got = route('hd-planner', unavailable=OPUS_DOWN)
        self.assertEqual((got.model, got.reason), ('claude-fable-5-2', 'cooled:claude-opus-5-5,' + ULTRA_OPUS))
        got = route('hd-planner', unavailable=OPUS_DOWN + ['claude-fable-5-2'])
        self.assertEqual((got.model, got.reason), ('gpt-6.2-sol', 'cooled:claude-opus-5-5,%s,claude-fable-5-2' % ULTRA_OPUS))
        got = route('hd-planner', unavailable=OPUS_DOWN + ['claude-fable-5-2', 'gpt-6.2-sol'])
        self.assertEqual((got.model, got.reason), (KIMI, 'cooled:claude-opus-5-5,%s,claude-fable-5-2,gpt-6.2-sol' % ULTRA_OPUS))
        got = route('hd-planner', unavailable=OPUS_DOWN + ['claude-fable-5-2', 'gpt-6.2-sol', KIMI])
        self.assertEqual((got.model, got.reason), ('grok-4.7', 'cooled:claude-opus-5-5,%s,claude-fable-5-2,gpt-6.2-sol,%s' % (ULTRA_OPUS, KIMI)))

    def test_failed_probe_is_unavailable_not_exhausted(self):
        got = route('hd-planner', probefail=['claude-opus-5-5'])
        self.assertEqual((got.model, got.reason), (ULTRA_OPUS, 'cooled:claude-opus-5-5:probe_failed'))
        got = route('hd-planner', unavailable=OPUS_DOWN + ['claude-fable-5-2'], probefail=['gpt-6.2-sol'])
        self.assertEqual((got.model, got.reason), (KIMI, 'cooled:claude-opus-5-5,%s,claude-fable-5-2,gpt-6.2-sol:probe_failed' % ULTRA_OPUS))
        got = route('hd-planner', probefail=TOP)
        self.assertEqual((got.model, got.reason), ('grok-4.7', 'fallback_last'))

    def test_grok_cap_zero_means_pool_off_even_for_cap_exempt_roles(self):
        # GROK_PROXY_CAP=0 switches the Grok pool off. Grok sits LAST in TOP (24 Nov), so an uncooled walk stops at Opus before ever
        # reaching it; the skip must fire when the walk DOES reach Grok, and before `last` is set, so the pick is Kimi, never a
        # pool that is off.
        for role in ('hd-planner', 'pm-admission-reviewer', 'growth'):
            for cap in ('0', '00'):
                with self.subTest(role=role, cap=cap):
                    got = route(role, GROK_PROXY_CAP=cap)
                    self.assertEqual((got.model, got.reason), ('claude-opus-5-5', 'first_usable'))
                    got = route(role, GROK_PROXY_CAP=cap, unavailable=OPUS_DOWN + ['claude-fable-5-2', 'gpt-6.2-sol', KIMI])
                    self.assertEqual((got.model, got.reason), (KIMI, 'fallback_last'))
                    self.assertIn('pool_cap skip model=grok-4.7', got.err)

    def test_grok_cap_above_zero_or_unset_keeps_grok_reachable(self):
        self.assertNotEqual(route('hd-planner').model, 'grok-4.7')  # Opus first (24 Nov value order)
        self.assertEqual(route('hd-planner', unavailable=OPUS_DOWN + ['claude-fable-5-2', 'gpt-6.2-sol', KIMI]).model, 'grok-4.7')
        self.assertEqual(route('hd-planner', GROK_PROXY_CAP=3, unavailable=OPUS_DOWN + ['claude-fable-5-2', 'gpt-6.2-sol', KIMI]).model, 'grok-4.7')

    def test_grok_402_records_exact_model_cooldown(self):
        self.assertIn('grok-4.7) slot=grok_exhausted_until;;', SOURCE)


class PoolCaps(unittest.TestCase):
    def test_pool_cap_skips_triage_but_not_the_exempt_list(self):
        # Rajiv ts 1794921123.696379: pm-triage-agent and pm-backlog-promoter are NOT exempt. Spark is down and OpenCode is off, so the Claude cap is reached.
        blocked = dict(spark=False, CLAUDE_PROXY_CAP='0', OPENCODE_PROXY_CAP='0', SARVAM_PROXY_CAP='0', ULTRA_PROXY_CAP='0')  # Ultra off too: a full team pool otherwise overflows onto it (UltraRoute pins that)
        # 24 Nov: triage and the promoter alias moved to LOW (light lanes), so the non-exempt MID cap case is pinned on claude and an unlisted type
        # (hd-builder keeps the approved hy4-flash-free builder exception and would land there); cap exemption itself is unchanged.
        got = route('claude', **blocked)
        self.assertEqual(got.model, 'gpt-6.1-terra')
        self.assertIn('claude-sonnet-5-5:pool_cap', got.reason)
        got = route('vendor-new-agent', **blocked)
        self.assertIn('claude-sonnet-5-5:pool_cap', got.reason)
        # ci repair, 12b and a P0 override stay on the first Claude model. Counted as telemetry, not queued. (OpenCode pool cap 0 still applies to everyone.)
        for role in ('ci-repair-agent', 'e2e-failure-investigator', 'main-red-skip', 'cleanup-pr-runner', 'pm-ops-tooling', 'mop-main-repair',
                     'autoscaler-main-repair'):
            with self.subTest(role=role):
                self.assertNotIn('claude-sonnet-5-5:pool_cap', route(role, **blocked).reason)
                self.assertNotIn('claude-haiku-5-5:pool_cap', route(role, **blocked).reason)
        for role in ('pm-admission-reviewer', 'growth'):
            with self.subTest(role=role):
                self.assertNotIn('pool_cap', route(role, CLAUDE_PROXY_CAP='0').reason)
        # A P0 override no longer lifts a pool cap (Rajiv C0ALZJHGE49 ts 1795439359.022079): the P0 preempts at admission instead (test_proxy_preempt.py).
        self.assertIn('claude-sonnet-5-5:pool_cap', route('claude', PROXY_AGENT_CAP_OVERRIDE='P0:13907', **blocked).reason)
        self.assertNotIn('claude-sonnet-5-5:pool_cap', route('claude', 'MODE: heartbeat', **blocked).reason)

    def test_spark_proxy_cap_is_read_from_proxy_caps_env(self):
        # Rajiv C0ALZJHGE49 thread 1795355101.961449, ts 1795356430.728759: Spark pool 8. The loader used to skip SPARK_PROXY_CAP, so the pool stayed at 6.
        loader = re.search(r'^_PROXY_TOTAL_CAP_ENV=.*?^fi$', SOURCE, re.M | re.S).group(0)
        probe = 'printf "%s|%s|%s|%s|%s|%s" "${SPARK_PROXY_CAP:-}" "${CLAUDE_PROXY_CAP:-}" "${GROK_PROXY_CAP:-}" "${OPENCODE_PROXY_CAP:-}" "${CODEX_PROXY_CAP:-}" "${INKLING_PROXY_CAP:-}"'
        with tempfile.TemporaryDirectory() as home:
            state = pathlib.Path(home) / '.claude/state'
            state.mkdir(parents=True)
            (state / 'proxy-caps.env').write_text('SPARK_PROXY_CAP=8\nCLAUDE_PROXY_CAP=4\nGROK_PROXY_CAP=0\nOPENCODE_PROXY_CAP=6\nCODEX_PROXY_CAP=oops\nINKLING_PROXY_CAP=7\n')
            run = lambda **env: subprocess.run(['bash', '-c', loader + '\n' + probe], env=base_env(home, **env), text=True, capture_output=True, check=True).stdout
            self.assertEqual(run(), '8|4|0|6||')  # non-numeric ignored; INKLING_PROXY_CAP is not a loaded key
            self.assertEqual(run(SPARK_PROXY_CAP=3), '3|4|0|6||')  # explicit env still wins

    def test_spark_pool_blocks_at_the_cap_from_the_file_not_at_the_old_default(self):
        loader = re.search(r'^_PROXY_TOTAL_CAP_ENV=.*?^fi$', SOURCE, re.M | re.S).group(0)
        body = loader + '\nTYPE=hd-builder\nproxy_agent_pool_blocked ornith-1.5-35b-a3b; echo rc=$?'
        with tempfile.TemporaryDirectory() as home:
            state = pathlib.Path(home) / '.claude/state'
            state.mkdir(parents=True)
            (state / 'proxy-caps.env').write_text('SPARK_PROXY_CAP=8\n')
            for live, rc in (('6', 'rc=1'), ('7', 'rc=1'), ('8', 'rc=0')):
                with self.subTest(live=live):
                    self.assertEqual(sh(body, home=home, PROXY_AGENT_STUB_POOL_LIVE=live), rc)


class Compat(unittest.TestCase):
    def test_compat_types_resolve(self):
        for first, expected in [('MODE: PLAN', 'hd-planner'), ('MODE:PLAN', 'hd-planner'), ('MODE: IMPLEMENT', 'hd-builder'),
                                ('MODE: REWORK', 'hd-builder'), ('', 'hd-builder'), ('MODE: PLANNING', 'hd-builder')]:
            with self.subTest(first=first):
                got = route('pm-hotfix', first)
                self.assertEqual(got.type, expected)
                self.assertIn('compat type pm-hotfix -> ' + expected, got.err)
        got = route('pm-proof-qa', 'MODE: PROOF')
        self.assertEqual((got.type, got.tier), ('hd-proof-qa', 'MID'))
        self.assertIn('compat type pm-proof-qa -> hd-proof-qa', got.err)
        for role in ('hd-planner', 'hd-builder', 'hd-proof-qa', 'growth'):
            with self.subTest(role=role):
                got = route(role, 'MODE: PLAN')
                self.assertEqual(got.type, role)
                self.assertNotIn('compat type', got.err)

    def test_compat_pm_hotfix_selects_like_new_roles(self):
        got = route('pm-hotfix', 'MODE: PLAN')
        self.assertEqual((got.type, got.tier, got.model), ('hd-planner', 'TOP', 'claude-opus-5-5'))
        got = route('pm-hotfix', 'MODE: IMPLEMENT')
        self.assertEqual((got.type, got.tier, got.model), ('hd-builder', 'MID', MID[0]))
        got = route('pm-hotfix', 'MODE: REWORK', rung=2)
        self.assertEqual((got.type, got.tier, got.rung), ('hd-builder', 'TOP', '3'))


class EmergencyGate(unittest.TestCase):
    ids = list(dict.fromkeys(LOW + MID + TOP))

    def test_gate_opens_only_when_every_id_of_every_level_is_exhausted(self):
        body = 'proxy_agent_emergency_open; echo "rc=$? blocker=${PA_EMERGENCY_BLOCKER:-}"'
        self.assertEqual(sh(body, REAL_EMERGENCY='1', UNAVAILABLE=' '.join(self.ids)), 'rc=0 blocker=')

    def test_gate_reads_all_three_levels(self):
        body = 'TYPE=cleanup-pr-runner; proxy_agent_emergency_open; echo "rc=$? blocker=${PA_EMERGENCY_BLOCKER:-}"'
        # TYPE is a LOW-level lane so the ornith entry of LOW survives the strip; every other blocker is level-independent.
        for usable in self.ids:
            with self.subTest(usable=usable):
                cooled = [i for i in self.ids if i != usable]
                self.assertEqual(sh(body, REAL_EMERGENCY='1', UNAVAILABLE=' '.join(cooled)), 'rc=1 blocker=' + usable)


class ModelHelpers(unittest.TestCase):
    def test_run_402_records_exact_model_cooldown(self):
        # Extracted by name: note_log_exhaustion -> log_exhaustion (python heredoc) + resolve_model / alias_for_id / canonical_id.
        needed = '\n'.join(extract(f) for f in ('proxy_agent_note_log_exhaustion', 'proxy_agent_log_exhaustion', 'proxy_agent_alias_for_id',
                                                  'proxy_agent_canonical_id', 'resolve_model'))
        for model in ('claude-sonnet-5-5', 'grok-4.7'):
            with self.subTest(model=model), tempfile.TemporaryDirectory() as tmp:
                log = pathlib.Path(tmp) / 'run.log'
                log.write_text('HTTP 402 payment required\n')
                shell = 'proxy_agent_state_dir() { printf "%s\\n" "$STATE"; }\n' + needed + '\nproxy_agent_note_log_exhaustion ' + model + ' "$LOG" ""\n'
                subprocess.run(['bash', '-c', shell], env=dict(os.environ, STATE=tmp, LOG=str(log)), check=True, capture_output=True, text=True)
                quota = json.loads((pathlib.Path(tmp) / 'quota.json').read_text())
                self.assertGreater(quota['exhausted_until'][model], 0)

    def test_astra_is_a_known_model(self):
        # Rajiv ts 1794640502.657479: gpt-6-astra is handled like gpt-6.1-sol (alias, canonical id, exhaustion slot, probe allowlist).
        out = sh('for k in astra gpt-6-astra; do proxy_agent_canonical_id $k; done\nproxy_agent_alias_for_id gpt-6-astra\nresolve_model astra\nresolve_model gpt-6-astra').split('\n')
        self.assertEqual(out, ['gpt-6-astra', 'gpt-6-astra', 'astra', 'gpt-6-astra 200000 32000', 'gpt-6-astra 200000 32000'])
        self.assertIn('gpt-6-astra) slot=astra_exhausted_until;;', SOURCE)
        self.assertRegex(SOURCE, r'gpt-6\.1-sol\|gpt-6-astra\|grok-4\.7')  # exact-id allowlist: astra is a known id like sol

    def test_sol_alias_canonical_ids(self):
        out = sh('proxy_agent_canonical_id sol; proxy_agent_canonical_id gpt-6.2-sol; proxy_agent_canonical_id gpt-6.1-sol; '
                 'proxy_agent_canonical_id terra; proxy_agent_alias_for_id gpt-6.1-terra').split('\n')
        self.assertEqual(out, ['gpt-6.2-sol', 'gpt-6.2-sol', 'gpt-6.1-sol', 'gpt-6.1-terra', 'terra'])


class OrnithLightOnly(unittest.TestCase):
    """24 Nov (Rajiv C0ALZJHGE49 thread 1795481047.444089, ts 1795484559.513619): ornith only for light work. MID and TOP never list or pick it."""
    ORNITH = 'ornith-1.5-35b-a3b'
    LIGHT = ['cleanup-pr-runner', 'pm-triage-agent', 'pm-backlog-promoter']
    NOT_LIGHT = ['hd-builder', 'hd-proof-qa', 'ci-repair-agent', 'remote-test-runner', 'remote-qa-runner', 'mop-main-repair', 'autoscaler-main-repair',
                 'pm-ops-tooling', 'e2e-failure-investigator', 'customer-artifact-investigator', 'vendor-new-agent', 'claude']

    def test_only_the_low_level_lists_ornith(self):
        lines = [l for l in extract('proxy_agent_tier_models').splitlines() if re.match(r'\s+(LOW|MID|TOP)\) models="', l)]
        self.assertEqual(len(lines), 3)
        for line in lines:
            with self.subTest(level=line.strip()[:3]):
                self.assertEqual(self.ORNITH in line, line.strip().startswith('LOW)'))

    def test_light_lanes_map_to_low_and_start_on_ornith(self):
        for role in self.LIGHT:
            with self.subTest(role=role):
                self.assertEqual(sh('proxy_agent_type_level "%s"' % role), 'LOW')
                self.assertEqual(sh('TYPE="%s"; proxy_agent_tier_models LOW' % role).split()[0], self.ORNITH)
                got = route(role)
                self.assertEqual((got.tier, got.model), ('LOW', self.ORNITH))

    def test_heartbeat_and_digest_briefs_are_light(self):
        for brief in ('MODE: heartbeat', 'MODE: digest'):
            with self.subTest(brief=brief):
                got = route('claude', brief)
                self.assertEqual((got.tier, got.model), ('LOW', self.ORNITH))

    def test_mid_launches_never_pick_or_list_ornith(self):
        for role in self.NOT_LIGHT:
            with self.subTest(role=role):
                self.assertNotIn(self.ORNITH, sh('TYPE="%s"; proxy_agent_tier_models MID' % role).split())
                got = route(role)  # Spark is up, so ornith would win if it were still listed
                self.assertNotEqual(got.model, self.ORNITH)
                self.assertNotIn('ornith', got.reason)

    def test_non_light_types_lose_ornith_even_when_asked_for_the_low_level(self):
        for role in self.NOT_LIGHT:
            with self.subTest(role=role):
                self.assertNotIn(self.ORNITH, sh('TYPE="%s"; proxy_agent_tier_models LOW' % role).split())

    def test_top_never_lists_ornith(self):
        for role in ('hd-planner', 'hd-investigator', 'pm-admission-reviewer', 'growth'):
            with self.subTest(role=role):
                self.assertNotIn(self.ORNITH, sh('TYPE="%s"; proxy_agent_tier_models TOP' % role).split())
                self.assertNotEqual(route(role).model, self.ORNITH)


class UltraRoute(unittest.TestCase):
    """dev@scribie ULTRA credential (own `ultra/` CLIProxyAPI prefix route; Rajiv D0AMF0XE6TS thread 1795576437.379619, ts 1795590119.110289:
    "set the cap to 4 initially and measure the burn rate in hourlies"). Overflow right behind the team id of the same model, own pool and own
    hard cap, invisible to the team Claude pool. The picker tests below drive the REAL proxy_agent_pool_blocked: PROXY_AGENT_STUB_POOL_LIVE is
    one number for every pool, so each case separates the pools by their caps (CLAUDE_PROXY_CAP vs ULTRA_PROXY_CAP), not by the count."""
    OPENCODE = ['glm-5.3', 'deepseek-v4.1-flash']  # out of MID while the OpenCode hold runs; listing them keeps these cases independent of it
    MID_BEFORE_TEAM_SONNET = OPENCODE + ['hy4-flash-free']

    def test_team_id_still_wins_when_it_is_usable(self):
        self.assertEqual(route('hd-planner').model, 'claude-opus-5-5')
        self.assertEqual(route('claude', unavailable=self.MID_BEFORE_TEAM_SONNET).model, 'claude-sonnet-5-5')

    def test_ultra_is_the_overflow_right_behind_the_team_id(self):
        got = route('hd-planner', unavailable=['claude-opus-5-5'])
        self.assertEqual((got.tier, got.model, got.reason), ('TOP', ULTRA_OPUS, 'cooled:claude-opus-5-5'))
        got = route('claude', unavailable=self.MID_BEFORE_TEAM_SONNET + ['claude-sonnet-5-5'])
        self.assertEqual((got.tier, got.model), ('MID', ULTRA_SONNET))
        # ...and only then the next provider.
        self.assertEqual(route('claude', unavailable=self.MID_BEFORE_TEAM_SONNET + SONNET_DOWN).model, 'gpt-6.1-terra')

    def test_judgment_lanes_keep_ultra_behind_its_team_id_not_at_the_end(self):
        # proxy_agent_pick_tier reorders judgment lanes onto Claude first; the ultra ids must travel with their team ids.
        for role in JUDGMENT_MID:
            with self.subTest(role=role):
                self.assertEqual(route(role, unavailable=['claude-sonnet-5-5']).model, ULTRA_SONNET)
                self.assertNotEqual(route(role, unavailable=SONNET_DOWN).model, ULTRA_SONNET)

    def test_ultra_cap_applies_to_every_role_including_the_cap_exempt_ones(self):
        # Team pool wide open (cap 99), Ultra full (4 live of 4): the walk skips Ultra with a pool_cap reason and moves on to Fable.
        for role in ('hd-planner', 'pm-admission-reviewer', 'growth'):
            with self.subTest(role=role):
                got = route(role, unavailable=['claude-opus-5-5'], PROXY_AGENT_STUB_POOL_LIVE='4', CLAUDE_PROXY_CAP='99', ULTRA_PROXY_CAP='4')
                self.assertEqual(got.model, 'claude-fable-5-2')
                self.assertIn('%s:pool_cap' % ULTRA_OPUS, got.reason)

    def test_ultra_under_cap_is_taken(self):
        got = route('hd-planner', unavailable=['claude-opus-5-5'], PROXY_AGENT_STUB_POOL_LIVE='3', CLAUDE_PROXY_CAP='99', ULTRA_PROXY_CAP='4')
        self.assertEqual(got.model, ULTRA_OPUS)

    def test_cap_zero_switches_the_route_off_without_counting(self):
        for role in ('hd-planner', 'growth'):
            with self.subTest(role=role):
                got = route(role, unavailable=['claude-opus-5-5'], PROXY_AGENT_STUB_POOL_LIVE='0', ULTRA_PROXY_CAP='0')
                self.assertEqual(got.model, 'claude-fable-5-2')
                self.assertIn('%s:pool_cap' % ULTRA_OPUS, got.reason)

    def test_unset_cap_defaults_to_four(self):
        blocked = lambda live: sh('TYPE=hd-planner; proxy_agent_pool_blocked %s && echo blocked || echo open' % ULTRA_OPUS,
                                  PROXY_AGENT_STUB_POOL_LIVE=live, CLAUDE_PROXY_CAP='99')
        self.assertEqual((blocked('3'), blocked('4')), ('open', 'blocked'))

    def test_unreadable_count_fails_closed_not_open(self):
        out = sh('TYPE=growth; proxy_agent_pool_blocked %s && echo blocked || echo open' % ULTRA_OPUS, PROXY_AGENT_STUB_POOL_LIVE='unreadable')
        self.assertEqual(out, 'blocked')

    def test_team_cap_never_governs_ultra_and_ultra_cap_never_governs_team(self):
        body = 'TYPE=hd-planner; proxy_agent_pool_blocked %s && echo blocked || echo open'
        # Team Claude pool full (cap 0), Ultra open: the team id is blocked, the ultra id is not.
        self.assertEqual(sh(body % 'claude-opus-5-5', PROXY_AGENT_STUB_POOL_LIVE='0', CLAUDE_PROXY_CAP='0', ULTRA_PROXY_CAP='4'), 'blocked')
        self.assertEqual(sh(body % ULTRA_OPUS, PROXY_AGENT_STUB_POOL_LIVE='0', CLAUDE_PROXY_CAP='0', ULTRA_PROXY_CAP='4'), 'open')
        # Ultra off (cap 0), team open: the other way round.
        self.assertEqual(sh(body % ULTRA_OPUS, PROXY_AGENT_STUB_POOL_LIVE='0', CLAUDE_PROXY_CAP='4', ULTRA_PROXY_CAP='0'), 'blocked')
        self.assertEqual(sh(body % 'claude-opus-5-5', PROXY_AGENT_STUB_POOL_LIVE='0', CLAUDE_PROXY_CAP='4', ULTRA_PROXY_CAP='0'), 'open')

    def test_nested_haiku_sonnet_opus_aliases_never_take_the_ultra_id(self):
        # The cap counter sees primary `claude -p --model ultra/...` processes only, so a nested child on the Ultra credential would be uncounted.
        got = route('claude', unavailable=['claude-sonnet-5-5', 'claude-opus-5-5'])
        self.assertNotIn(got.sonnet, (ULTRA_SONNET, ULTRA_OPUS))
        self.assertNotIn(got.opus, (ULTRA_SONNET, ULTRA_OPUS))
        self.assertEqual((got.sonnet, got.opus), ('gpt-6.1-terra', 'claude-fable-5-2'))


if __name__ == '__main__':
    unittest.main()
