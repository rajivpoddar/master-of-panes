"""Sarvam Token Factory pool in proxy-agent.sh (Rajiv DM D0AMF0XE6TS ts 1795369553.133189: "Configure it with cliproxyapi. Setup proxy agents to use it";
00:19/00:20: "Configure and start using sarvam proxies", "Kimi should be top tier").

Drives the REAL helpers extracted by NAME from the deployed script (PROXY_AGENT_TEST_SCRIPT overrides the path), one bash process per launch, sharing one
temp HOME so the rotation cursor and the Kimi negative-cache file persist between "launches" exactly as in production. Nothing here calls the network
or the gateway: sub_usable / pool_live are stubbed, the Spark health URL points at a closed local port, and no test touches the real HOME or pm-ops DB.

Covered: Sarvam picks when OpenCode is full; refusal at the Sarvam cap (tier picker and primary guard); round-robin rotation; the Kimi availability
gate (flag 0/1/auto, negative-cache TTL, never poisons the emergency gate); hd-planner picks Kimi right after Grok; structural wiring of every Sarvam id.
"""
import os
import pathlib
import re
import shlex
import subprocess
import tempfile
import time
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


def var_value(name):
    return shlex.split(var(name).split('=', 1)[1])[0].split()


# Discovered from the script, not hand-kept: every id the launcher treats as Sarvam, and the round-robin ring (all of them but the gated Kimi).
SARVAM_IDS = re.findall(r'sarvam-[a-z0-9.-]+', re.search(r'case "\$id" in ([^)]*)\)', extract('proxy_agent_is_sarvam_id')).group(1))
assert len(SARVAM_IDS) >= 5, SARVAM_IDS
KIMI = 'sarvam-kimi-k3'
RING = var_value('PROXY_AGENT_SARVAM_RING')
OPENCODE_RING = var_value('PROXY_AGENT_OPENCODE_RING')

TIER_FUNCS = [
    'proxy_agent_pick_tier', 'proxy_agent_tier_models', 'proxy_agent_is_capped', 'proxy_agent_is_judgment_lane',
    'proxy_agent_is_opencode_id', 'proxy_agent_router_identity', 'proxy_agent_is_free_trial_id',
    'proxy_agent_free_guard_refuses', 'proxy_agent_is_emergency_muse', 'proxy_agent_opencode_rotation_file',
    'proxy_agent_opencode_rotate', 'proxy_agent_opencode_record', 'proxy_agent_ring_rotate', 'proxy_agent_is_sarvam_id',
    'proxy_agent_sarvam_kimi_state_file', 'proxy_agent_sarvam_kimi_flag', 'proxy_agent_sarvam_kimi_gated',
    'proxy_agent_sarvam_kimi_mark_unavailable', 'proxy_agent_sarvam_rotation_file', 'proxy_agent_sarvam_rotate',
    'proxy_agent_sarvam_record', 'proxy_agent_pool_blocked', 'proxy_agent_cap_exempt',
]
KIMI_FUNCS = [
    'proxy_agent_router_identity', 'proxy_agent_is_sarvam_id', 'proxy_agent_sarvam_kimi_state_file', 'proxy_agent_sarvam_kimi_flag',
    'proxy_agent_sarvam_kimi_gated', 'proxy_agent_sarvam_kimi_mark_unavailable',
]
GUARD_FUNCS = KIMI_FUNCS + [
    'proxy_agent_primary_pool_guard', 'proxy_agent_free_guard_refuses', 'proxy_agent_is_free_trial_id',
    'proxy_agent_is_unresolved_auto', 'proxy_agent_is_emergency_muse',
]
EXHAUST_FUNCS = KIMI_FUNCS + ['proxy_agent_route_exhausted']


def prelude(funcs, *variables):
    return '\n'.join([var(v) for v in variables] + [extract(f) for f in funcs])


PICK_PRELUDE = prelude(TIER_FUNCS, 'PROXY_AGENT_OPENCODE_RING', 'PROXY_AGENT_SARVAM_RING', 'PROXY_AGENT_FREE_TRIAL_IDS')

PICK_HARNESS = PICK_PRELUDE + r'''
PROXY_AGENT_CLAUDE_CAP=8
proxy_agent_live_count() { printf 0; }
proxy_agent_pool_live() {
  case "$1" in
    sarvam) [[ "${LIVE_SARVAM:-0}" == FAIL ]] && return 1; printf '%s' "${LIVE_SARVAM:-0}";;
    *) printf 0;;
  esac
}
proxy_agent_sub_usable() {
  case " $RC2 " in *" $1 "*) return 2;; esac
  case " $UNAVAILABLE " in *" $1 "*) return 1;; esac
  return 0
}
proxy_agent_burn_refused() { return 1; }
proxy_agent_burn_enforced() { return 1; }
proxy_agent_emergency_open() { return 1; }
TYPE="$ROLE"
proxy_agent_pick_tier "$TIER" $NESTED; rc=$?
printf '%s|%s|%s' "$PICK_MODEL" "$PICK_REASON" "$rc"
'''

KIMI_HARNESS = prelude(KIMI_FUNCS, 'PROXY_AGENT_SARVAM_RING') + '\n"$@"\n'

GUARD_HARNESS = prelude(GUARD_FUNCS, 'PROXY_AGENT_FREE_TRIAL_IDS') + r'''
resolve_model() { printf '%s 262144 32000\n' "$1"; }
proxy_agent_pool_blocked() { local i; i="$(proxy_agent_router_identity "$1")"; case " $BLOCKED " in *" $i "*) return 0;; *) return 1;; esac; }
proxy_agent_emergency_open() { return 1; }
proxy_agent_primary_pool_guard
'''

EXHAUST_HARNESS = prelude(EXHAUST_FUNCS, 'PROXY_AGENT_SARVAM_RING') + r'''
proxy_agent_burn_refused() { return 1; }
proxy_agent_pool_blocked() { return 1; }
proxy_agent_sub_usable() { [[ -n "${CALLED_MARK:-}" ]] && : > "$CALLED_MARK"; return "${SUB_RC:-0}"; }
proxy_agent_route_exhausted "$ID"
'''

BURN_HARNESS = prelude(['proxy_agent_burn_refused', 'proxy_agent_is_sarvam_id', 'proxy_agent_is_opencode_id', 'proxy_agent_router_identity']) + r'''
proxy_agent_burn_enforced() { return 0; }
proxy_agent_burn_refused "$ID"
'''


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.state = self.home / '.claude/state'
        self.cursor = self.state / 'proxy-sarvam-rotation'
        self.oc_cursor = self.state / 'proxy-opencode-rotation'
        self.kimi_state = self.state / 'proxy-sarvam-kimi-unavailable'

    def tearDown(self):
        self.tmp.cleanup()

    def env(self, **extra):
        env = dict(os.environ, HOME=str(self.home), QWEN_HEALTH_URL='http://127.0.0.1:1/metrics',
                   INKLING_HEALTH_URL='http://127.0.0.1:1/metrics', UNAVAILABLE='', RC2='', BLOCKED='', NESTED='', TIER='MID', ROLE='claude')
        for k in ('PROXY_AGENT_DRY_RUN', 'PROXY_AGENT_SARVAM_ROTATION_FILE', 'PROXY_AGENT_OPENCODE_ROTATION_FILE', 'SARVAM_KIMI_ENABLED',
                  'PROXY_AGENT_SARVAM_KIMI_STATE_FILE', 'PROXY_AGENT_SARVAM_KIMI_RETRY_S', 'SARVAM_PROXY_CAP', 'OPENCODE_PROXY_CAP',
                  'CLAUDE_PROXY_CAP', 'CODEX_PROXY_CAP', 'GROK_PROXY_CAP', 'PROXY_AGENT_CAP_OVERRIDE', 'ROLE_BRIEF', 'LIVE_SARVAM'):
            env.pop(k, None)
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def pick(self, **extra):
        done = subprocess.run(['bash', '-c', PICK_HARNESS], env=self.env(**extra), text=True, capture_output=True, check=True)
        model, reason, rc = done.stdout.rsplit('|', 2)
        return model, reason, int(rc)

    def write_kimi_state(self, epoch):
        self.state.mkdir(parents=True, exist_ok=True)
        self.kimi_state.write_text(f'{epoch}\n')

    def bash_rc(self, script, **extra):
        return subprocess.run(['bash', '-c', script], env=self.env(**extra), text=True, capture_output=True)

    def kimi(self, *args, **extra):
        return self.bash_rc(KIMI_HARNESS, **extra) if not args else subprocess.run(
            ['bash', '-c', KIMI_HARNESS, 'bash', *args], env=self.env(**extra), text=True, capture_output=True)


# ---------------------------------------------------------------- tier picker: pool, cap, rotation
class SarvamPick(Base):
    def test_picks_sarvam_when_opencode_pool_is_off(self):
        model, reason, rc = self.pick(OPENCODE_PROXY_CAP=0)
        self.assertEqual((model, rc), (RING[0], 0))
        self.assertIn('deepseek-v4.1-flash:pool_cap', reason)

    def test_ring_is_every_sarvam_id_except_gated_kimi(self):
        self.assertEqual(sorted(RING), sorted(i for i in SARVAM_IDS if i != KIMI))

    def test_at_cap_skips_every_sarvam_id_and_falls_through(self):
        model, reason, rc = self.pick(OPENCODE_PROXY_CAP=0, LIVE_SARVAM=4)
        self.assertEqual((model, rc), ('claude-sonnet-5-5', 0))
        for sid in RING:
            self.assertIn(f'{sid}:pool_cap', reason)

    def test_one_below_cap_still_picks_and_cap_is_configurable(self):
        dry = {'OPENCODE_PROXY_CAP': 0, 'PROXY_AGENT_DRY_RUN': 1}  # dry-run: no cursor carried between the picks below
        self.assertEqual(self.pick(LIVE_SARVAM=3, **dry)[0], RING[0])
        self.assertEqual(self.pick(LIVE_SARVAM=3, SARVAM_PROXY_CAP=3, **dry)[0], 'claude-sonnet-5-5')
        self.assertEqual(self.pick(LIVE_SARVAM=7, SARVAM_PROXY_CAP=8, **dry)[0], RING[0])

    def test_cap_zero_turns_the_pool_off_without_counting(self):
        model, reason, rc = self.pick(OPENCODE_PROXY_CAP=0, SARVAM_PROXY_CAP=0, LIVE_SARVAM='FAIL')
        self.assertEqual((model, rc), ('claude-sonnet-5-5', 0))
        self.assertIn(f'{RING[0]}:pool_cap', reason)

    def test_cap_exempt_role_is_still_capped_at_the_picker(self):
        model, reason, rc = self.pick(OPENCODE_PROXY_CAP=0, LIVE_SARVAM=4, ROLE='pm-ops-tooling')
        self.assertEqual((model, rc), ('claude-sonnet-5-5', 0))
        self.assertIn(f'{RING[0]}:pool_cap', reason)

    def test_unreadable_live_count_is_blocked_not_a_free_pass(self):
        model, reason, rc = self.pick(OPENCODE_PROXY_CAP=0, LIVE_SARVAM='FAIL')
        self.assertEqual((model, rc), ('claude-sonnet-5-5', 0))
        self.assertIn(f'{RING[0]}:pool_cap', reason)

    def test_everything_capped_refuses_75_with_no_model(self):
        model, reason, rc = self.pick(OPENCODE_PROXY_CAP=0, SARVAM_PROXY_CAP=0, CLAUDE_PROXY_CAP=0, CODEX_PROXY_CAP=0)
        self.assertEqual((model, rc), ('', 75))

    def test_successive_launches_cycle_the_ring_and_wrap(self):
        got = [self.pick(OPENCODE_PROXY_CAP=0)[0] for _ in range(len(RING) * 2)]
        self.assertEqual(got, RING * 2)
        self.assertEqual(self.cursor.read_text().strip(), RING[-1])

    def test_cursor_on_last_ring_id_wraps_to_first(self):
        self.state.mkdir(parents=True)
        self.cursor.write_text(RING[-1] + '\n')
        self.assertEqual(self.pick(OPENCODE_PROXY_CAP=0)[0], RING[0])

    def test_garbage_or_foreign_cursor_falls_back_to_ring_order(self):
        self.state.mkdir(parents=True)
        for junk in ('claude-sonnet-5-5', '', 'sarvam-nope', f'{RING[0]}(high)'):
            with self.subTest(junk=junk):
                self.cursor.write_text(junk + '\n')
                self.assertEqual(self.pick(OPENCODE_PROXY_CAP=0, PROXY_AGENT_DRY_RUN=1)[0], RING[0])

    def test_dry_run_never_writes_the_cursor(self):
        got = [self.pick(OPENCODE_PROXY_CAP=0, PROXY_AGENT_DRY_RUN=1)[0] for _ in range(3)]
        self.assertEqual(got, [RING[0]] * 3)
        self.assertFalse(self.cursor.exists())

    def test_nested_alias_pick_excludes_sarvam_and_leaves_cursor_alone(self):
        model, reason, rc = self.pick(NESTED='nested')
        self.assertEqual((model, rc), ('claude-sonnet-5-5', 0))
        for sid in SARVAM_IDS:
            self.assertNotIn(sid, reason)
        self.assertFalse(self.cursor.exists())

    def test_cooled_sarvam_id_is_skipped_to_next_in_rotation(self):
        model, reason, rc = self.pick(OPENCODE_PROXY_CAP=0, UNAVAILABLE=RING[0])
        self.assertEqual((model, rc), (RING[1], 0))
        self.assertIn(RING[0], reason)
        self.assertEqual(self.cursor.read_text().strip(), RING[1])

    def test_non_sarvam_pick_leaves_cursor_alone(self):
        model, _, _ = self.pick(OPENCODE_PROXY_CAP=0, UNAVAILABLE=' '.join(RING))
        self.assertEqual(model, 'claude-sonnet-5-5')
        self.assertFalse(self.cursor.exists())

    def test_sarvam_pick_does_not_move_the_opencode_cursor(self):
        self.pick(OPENCODE_PROXY_CAP=0)
        self.assertFalse(self.oc_cursor.exists())

    def test_opencode_still_goes_first_when_it_has_room(self):
        model, _, _ = self.pick()
        self.assertIn(model, OPENCODE_RING)
        self.assertFalse(self.cursor.exists())


# ---------------------------------------------------------------- Kimi gate at the picker (TOP tier)
class KimiPick(Base):
    TOP = {'TIER': 'TOP', 'ROLE': 'hd-planner'}

    def test_hd_planner_picks_kimi_right_after_grok_when_grok_is_cooled(self):
        model, _, rc = self.pick(UNAVAILABLE='grok-4.7', SARVAM_KIMI_ENABLED=1, **self.TOP)
        self.assertEqual((model, rc), (KIMI, 0))

    def test_hd_planner_picks_kimi_when_grok_pool_is_capped_off(self):
        model, reason, rc = self.pick(GROK_PROXY_CAP=0, SARVAM_KIMI_ENABLED=1, **self.TOP)
        self.assertEqual((model, rc), (KIMI, 0))
        self.assertIn('grok-4.7:pool_cap', reason)

    def test_non_judgment_top_role_follows_the_same_order(self):
        model, _, _ = self.pick(UNAVAILABLE='grok-4.7', SARVAM_KIMI_ENABLED=1, TIER='TOP', ROLE='claude')
        self.assertEqual(model, KIMI)

    def test_top_order_grok_kimi_opus_fable_sol(self):
        cooled, expected = '', ['grok-4.7', KIMI, 'claude-opus-5-5', 'claude-fable-5-2', 'gpt-6.2-sol']
        for want in expected:
            for role in ('hd-planner', 'claude'):
                with self.subTest(role=role, want=want):
                    model, _, rc = self.pick(UNAVAILABLE=cooled, SARVAM_KIMI_ENABLED=1, TIER='TOP', ROLE=role)
                    self.assertEqual((model, rc), (want, 0))
            cooled = f'{cooled} {want}'.strip()

    def test_flag_0_skips_kimi_to_opus_and_says_why(self):
        model, reason, rc = self.pick(UNAVAILABLE='grok-4.7', SARVAM_KIMI_ENABLED=0, **self.TOP)
        self.assertEqual((model, rc), ('claude-opus-5-5', 0))
        self.assertIn(f'{KIMI}:not_enabled', reason)

    def test_auto_with_live_negative_cache_skips_without_probing(self):
        self.write_kimi_state(int(time.time()) + 600)
        # RC2 would mark Kimi unavailable if it were probed; the file must stay byte-identical, proving no probe ran
        before = self.kimi_state.read_text()
        model, reason, _ = self.pick(UNAVAILABLE='grok-4.7', RC2=KIMI, **self.TOP)
        self.assertEqual(model, 'claude-opus-5-5')
        self.assertIn(f'{KIMI}:not_enabled', reason)
        self.assertEqual(self.kimi_state.read_text(), before)

    def test_auto_with_expired_negative_cache_probes_again_and_recovers(self):
        self.write_kimi_state(int(time.time()) - 5)
        model, _, rc = self.pick(UNAVAILABLE='grok-4.7', **self.TOP)
        self.assertEqual((model, rc), (KIMI, 0))

    def test_auto_provisioned_throughput_400_marks_unavailable_for_the_ttl(self):
        t0 = int(time.time())
        model, reason, _ = self.pick(UNAVAILABLE='grok-4.7', RC2=KIMI, PROXY_AGENT_SARVAM_KIMI_RETRY_S=120, **self.TOP)
        self.assertEqual(model, 'claude-opus-5-5')
        self.assertIn(f'{KIMI}:not_enabled', reason)
        until = int(self.kimi_state.read_text().strip())
        self.assertTrue(t0 + 115 <= until <= int(time.time()) + 125, until)
        # Negative cache holds on the next launch even though the stub would now answer 200.
        self.assertEqual(self.pick(UNAVAILABLE='grok-4.7', **self.TOP)[0], 'claude-opus-5-5')

    def test_rate_limited_kimi_is_cooled_not_marked_unavailable(self):
        model, reason, _ = self.pick(UNAVAILABLE=f'grok-4.7 {KIMI}', **self.TOP)
        self.assertEqual(model, 'claude-opus-5-5')
        self.assertIn(KIMI, reason)
        self.assertNotIn(f'{KIMI}:not_enabled', reason)
        self.assertFalse(self.kimi_state.exists())

    def test_forced_flag_and_dry_run_never_write_the_negative_cache(self):
        for extra in ({'SARVAM_KIMI_ENABLED': 1}, {'PROXY_AGENT_DRY_RUN': 1}):
            with self.subTest(extra=extra):
                model, _, _ = self.pick(UNAVAILABLE='grok-4.7', RC2=KIMI, **self.TOP, **extra)
                self.assertEqual(model, 'claude-opus-5-5')
                self.assertFalse(self.kimi_state.exists())

    def test_kimi_never_reaches_mid_or_low(self):
        for tier in ('MID', 'LOW'):
            with self.subTest(tier=tier):
                _, reason, _ = self.pick(TIER=tier, SARVAM_KIMI_ENABLED=1)
                self.assertNotIn(KIMI, reason)

    def test_gate_runs_before_the_fallback_candidate_is_recorded(self):
        body = extract('proxy_agent_pick_tier')
        gate = body.index('== sarvam-kimi-k3 ]] && proxy_agent_sarvam_kimi_gated')
        self.assertLess(gate, body.index('last="$id"'))


# ---------------------------------------------------------------- Kimi helpers in isolation
class KimiFlag(Base):
    def gated(self, **extra):
        return self.kimi('proxy_agent_sarvam_kimi_gated', **extra).returncode == 0

    def test_flag_0_gates_flag_1_opens_auto_without_state_opens(self):
        self.assertTrue(self.gated(SARVAM_KIMI_ENABLED=0))
        self.assertFalse(self.gated(SARVAM_KIMI_ENABLED=1))
        self.assertFalse(self.gated())

    def test_flag_comes_from_proxy_caps_env_and_env_wins(self):
        self.state.mkdir(parents=True)
        (self.state / 'proxy-caps.env').write_text('GROK_PROXY_CAP=2\nSARVAM_KIMI_ENABLED=0\n')
        self.assertTrue(self.gated())
        self.assertFalse(self.gated(SARVAM_KIMI_ENABLED=1))
        (self.state / 'proxy-caps.env').write_text('SARVAM_KIMI_ENABLED=1\n')
        self.assertFalse(self.gated())
        self.assertTrue(self.gated(SARVAM_KIMI_ENABLED=0))

    def test_invalid_flag_values_are_ignored(self):
        self.state.mkdir(parents=True)
        (self.state / 'proxy-caps.env').write_text('SARVAM_KIMI_ENABLED=maybe\n')
        self.assertFalse(self.gated(SARVAM_KIMI_ENABLED='yes'))

    def test_negative_cache_ttl(self):
        self.write_kimi_state(int(time.time()) + 300)
        self.assertTrue(self.gated())
        self.write_kimi_state(int(time.time()) - 1)
        self.assertFalse(self.gated())

    def test_garbage_state_file_does_not_gate(self):
        for junk in ('', 'soon', '12abc', '-5'):
            with self.subTest(junk=junk):
                self.state.mkdir(parents=True, exist_ok=True)
                self.kimi_state.write_text(junk + '\n')
                self.assertFalse(self.gated())

    def test_mark_unavailable_writes_now_plus_retry_and_defaults_to_300(self):
        t0 = int(time.time())
        self.assertEqual(self.kimi('proxy_agent_sarvam_kimi_mark_unavailable').returncode, 0)
        until = int(self.kimi_state.read_text().strip())
        self.assertTrue(t0 + 295 <= until <= int(time.time()) + 305, until)
        self.kimi_state.unlink()
        self.kimi('proxy_agent_sarvam_kimi_mark_unavailable', PROXY_AGENT_SARVAM_KIMI_RETRY_S='abc')
        self.assertTrue(t0 + 295 <= int(self.kimi_state.read_text().strip()) <= int(time.time()) + 305)

    def test_mark_unavailable_is_a_no_op_for_dry_run_and_forced_flags(self):
        for extra in ({'PROXY_AGENT_DRY_RUN': 1}, {'SARVAM_KIMI_ENABLED': 0}, {'SARVAM_KIMI_ENABLED': 1}):
            with self.subTest(extra=extra):
                self.assertEqual(self.kimi('proxy_agent_sarvam_kimi_mark_unavailable', **extra).returncode, 0)
                self.assertFalse(self.kimi_state.exists())

    def test_mark_unavailable_is_best_effort_when_state_dir_is_unwritable(self):
        done = self.kimi('proxy_agent_sarvam_kimi_mark_unavailable', PROXY_AGENT_SARVAM_KIMI_STATE_FILE='/dev/null/x/state')
        self.assertEqual(done.returncode, 0)


# ---------------------------------------------------------------- primary guard for raw `--model sarvam-*`
class PrimaryGuard(Base):
    def guard(self, model, **extra):
        return self.bash_rc(f'MODEL={shlex.quote(model)}\n' + GUARD_HARNESS, **extra)

    def test_open_pool_passes_and_capped_pool_refuses_75(self):
        self.assertEqual(self.guard('sarvam-glm-5.3').returncode, 0)
        done = self.guard('sarvam-glm-5.3', BLOCKED='sarvam-glm-5.3')
        self.assertEqual(done.returncode, 75)
        self.assertIn('sarvam pool_cap', done.stderr)

    def test_decorated_or_uppercase_id_is_the_same_admission(self):
        for raw in ('Sarvam-GLM-5.3', 'sarvam-glm-5.3(high)'):
            with self.subTest(raw=raw):
                self.assertEqual(self.guard(raw, BLOCKED='sarvam-glm-5.3').returncode, 75)

    def test_every_sarvam_id_is_guarded(self):
        for sid in SARVAM_IDS:
            with self.subTest(sid=sid):
                self.assertEqual(self.guard(sid, BLOCKED=sid, SARVAM_KIMI_ENABLED=1).returncode, 75)

    def test_kimi_refused_when_gated_and_allowed_when_open(self):
        done = self.guard(KIMI, SARVAM_KIMI_ENABLED=0)
        self.assertEqual(done.returncode, 75)
        self.assertIn('not_enabled', done.stderr)
        self.assertEqual(self.guard(KIMI, SARVAM_KIMI_ENABLED=1).returncode, 0)
        self.write_kimi_state(int(time.time()) + 300)
        self.assertEqual(self.guard(KIMI).returncode, 75)

    def test_other_sarvam_ids_do_not_depend_on_the_kimi_gate(self):
        self.assertEqual(self.guard('sarvam-105b', SARVAM_KIMI_ENABLED=0).returncode, 0)


# ---------------------------------------------------------------- emergency gate must not be poisoned by Kimi
class RouteExhausted(Base):
    def exhausted(self, id_, **extra):
        return self.bash_rc(f'ID={shlex.quote(id_)}\n' + EXHAUST_HARNESS, **extra).returncode

    def test_gated_kimi_is_known_unavailable_and_is_not_probed(self):
        mark = self.home / 'probed'
        self.assertEqual(self.exhausted(KIMI, SARVAM_KIMI_ENABLED=0, CALLED_MARK=mark, SUB_RC=0), 0)
        self.assertFalse(mark.exists())

    def test_kimi_provisioned_throughput_400_is_exhausted_not_unknown_and_cached(self):
        self.assertEqual(self.exhausted(KIMI, SUB_RC=2), 0)
        self.assertTrue(self.kimi_state.exists())

    def test_other_probe_failure_stays_unknown_and_keeps_the_gate_closed(self):
        self.assertEqual(self.exhausted('claude-opus-5-5', SUB_RC=2), 1)
        self.assertFalse(self.kimi_state.exists())

    def test_usable_kimi_is_not_exhausted_and_rate_limited_kimi_is(self):
        self.assertEqual(self.exhausted(KIMI, SUB_RC=0, SARVAM_KIMI_ENABLED=1), 1)
        self.assertEqual(self.exhausted(KIMI, SUB_RC=1, SARVAM_KIMI_ENABLED=1), 0)

    def test_no_subscription_mode_is_never_exhausted(self):
        self.assertEqual(self.exhausted(KIMI, SARVAM_KIMI_ENABLED=0, PROXY_AGENT_NO_SUBSCRIPTION=1), 1)


POOL_HARNESS = prelude(['proxy_agent_pool_blocked', 'proxy_agent_cap_exempt', 'proxy_agent_router_identity']) + r'''
proxy_agent_pool_live() { printf '%s' "${LIVE:-0}"; }
proxy_agent_pool_blocked "$ID"
'''


class PoolBlocked(Base):
    def blocked(self, id_, **extra):
        return self.bash_rc(f'ID={shlex.quote(id_)}\n' + POOL_HARNESS, **extra).returncode == 0

    def test_exempt_roles_do_not_lift_the_sarvam_cap_and_a_p0_override_lifts_no_pool(self):
        # Sarvam is paid with no meter (like OpenCode): no role exemption. Contrast: the Claude pool IS lifted by an exempt ROLE.
        # A P0 override lifts NO pool any more (Rajiv C0ALZJHGE49 ts 1795439359.022079: a P0 preempts instead; see test_proxy_preempt.py).
        for extra in ({'TYPE': 'pm-ops-tooling'}, {'TYPE': 'hd-planner', 'PROXY_AGENT_CAP_OVERRIDE': 'P0:123'}):
            with self.subTest(extra=extra):
                self.assertTrue(self.blocked(RING[0], LIVE=4, **extra))
                self.assertFalse(self.blocked(RING[0], LIVE=3, **extra))
        self.assertFalse(self.blocked('claude-opus-5-5', LIVE=99, CLAUDE_PROXY_CAP=1, TYPE='pm-ops-tooling'))
        self.assertTrue(self.blocked('claude-opus-5-5', LIVE=99, CLAUDE_PROXY_CAP=1, TYPE='hd-planner', PROXY_AGENT_CAP_OVERRIDE='P0:123'))

    def test_every_sarvam_id_shares_one_cap(self):
        for sid in SARVAM_IDS:
            with self.subTest(sid=sid):
                self.assertTrue(self.blocked(sid, LIVE=4))
                self.assertFalse(self.blocked(sid, LIVE=3))
                self.assertTrue(self.blocked(sid, LIVE=0, SARVAM_PROXY_CAP=0))

    def test_decorated_and_uppercase_ids_resolve_to_the_same_pool(self):
        self.assertTrue(self.blocked('Sarvam-GLM-5.3(high)', LIVE=4))


class BurnMeter(Base):
    def test_sarvam_ids_are_never_burn_refused(self):
        for sid in SARVAM_IDS:
            with self.subTest(sid=sid):
                done = self.bash_rc(f'ID={shlex.quote(sid)}\n' + BURN_HARNESS)
                self.assertEqual(done.returncode, 1, done.stderr)


# ---------------------------------------------------------------- structural wiring (discovered ids, no hand-kept site list)
class Wiring(unittest.TestCase):
    SITES = ['proxy_agent_probe_claude_messages', 'proxy_agent_pool_blocked', 'proxy_agent_pool_live', 'resolve_model', 'proxy_agent_tier_models']

    def test_there_are_sarvam_ids(self):
        self.assertIn(KIMI, SARVAM_IDS)
        self.assertGreaterEqual(len(SARVAM_IDS), 5)

    def test_every_sarvam_id_is_wired_into_every_site(self):
        for fn in self.SITES:
            body = extract(fn)
            for sid in SARVAM_IDS:
                with self.subTest(fn=fn, sid=sid):
                    self.assertIn(sid, body)

    def test_every_sarvam_id_is_in_the_known_model_case(self):
        line = next(l for l in LINES if 'Sarvam Token Factory via CLIProxyAPI' in l and l.lstrip().startswith('sarvam-'))
        for sid in SARVAM_IDS:
            self.assertIn(sid, line)

    def test_sarvam_is_paid_so_never_a_free_trial_id(self):
        free = var_value('PROXY_AGENT_FREE_TRIAL_IDS')
        self.assertFalse(set(free) & set(SARVAM_IDS))

    def test_tier_lists(self):
        def tier(name):
            out = subprocess.run(['bash', '-c', prelude(['proxy_agent_tier_models']) + f'\nHOME=/nonexistent proxy_agent_tier_models {name}'],
                                 text=True, capture_output=True, check=True)
            return out.stdout.split()
        top, mid, low = tier('TOP'), tier('MID'), tier('LOW')
        self.assertEqual(top[:5], ['grok-4.7', KIMI, 'claude-opus-5-5', 'claude-fable-5-2', 'gpt-6.2-sol'])
        self.assertFalse(set(SARVAM_IDS) & set(low))
        self.assertEqual([i for i in mid if i in SARVAM_IDS], RING)
        self.assertNotIn(KIMI, mid)
        last_opencode = max(mid.index(i) for i in OPENCODE_RING)
        self.assertTrue(all(mid.index(i) > last_opencode for i in RING))
        self.assertTrue(all(mid.index(i) < mid.index('claude-sonnet-5-5') for i in RING))


if __name__ == '__main__':
    unittest.main()
