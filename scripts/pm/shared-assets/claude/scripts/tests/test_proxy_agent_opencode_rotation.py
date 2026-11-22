"""OpenCode Go round-robin in proxy-agent.sh (Rajiv C0ALZJHGE49 thread 1795351517.305319: "we should rotate the models each time").

Drives the real proxy_agent_pick_tier / rotation helpers extracted by NAME from the deployed script (PROXY_AGENT_TEST_SCRIPT overrides the path),
one bash process per launch, sharing one temp HOME so the rotation cursor file persists between "launches" exactly as in production.
The Spark health URL points at a closed local port so ornith is deterministically skipped (host_down) and the OpenCode ids are what is under test.
"""
import os
import pathlib
import re
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(os.environ.get('PROXY_AGENT_TEST_SCRIPT', pathlib.Path.home() / '.claude/scripts/proxy-agent.sh'))
SOURCE = SCRIPT.read_text()
LINES = SOURCE.split('\n')

FUNCS = [
    'proxy_agent_pick_tier', 'proxy_agent_tier_models', 'proxy_agent_is_capped', 'proxy_agent_is_judgment_lane',
    'proxy_agent_is_opencode_id', 'proxy_agent_router_identity', 'proxy_agent_is_free_trial_id',
    'proxy_agent_free_guard_refuses', 'proxy_agent_is_emergency_muse', 'proxy_agent_opencode_rotation_file',
    'proxy_agent_opencode_rotate', 'proxy_agent_opencode_record',
    'proxy_agent_ring_rotate', 'proxy_agent_is_sarvam_id', 'proxy_agent_sarvam_kimi_state_file', 'proxy_agent_sarvam_kimi_flag',
    'proxy_agent_sarvam_kimi_gated', 'proxy_agent_sarvam_kimi_mark_unavailable', 'proxy_agent_sarvam_rotation_file',
    'proxy_agent_sarvam_rotate', 'proxy_agent_sarvam_record',
]


def extract(name):
    start = next(i for i, l in enumerate(LINES) if l.startswith(name + '() {'))
    if LINES[start].rstrip().endswith('}') and LINES[start].count('{') == LINES[start].count('}'):
        return LINES[start]
    end = next(i for i in range(start + 1, len(LINES)) if LINES[i] == '}')
    return '\n'.join(LINES[start:end + 1])


def var(name):
    return next(l for l in LINES if l.startswith(name + '='))


PRELUDE = '\n'.join([var('PROXY_AGENT_OPENCODE_RING'), var('PROXY_AGENT_SARVAM_RING'), var('PROXY_AGENT_FREE_TRIAL_IDS')] + [extract(f) for f in FUNCS])

HARNESS = PRELUDE + r'''
PROXY_AGENT_CLAUDE_CAP=8
proxy_agent_live_count() { printf 0; }
proxy_agent_sub_usable() { case " $UNAVAILABLE " in *" $1 "*) return 1;; *) return 0;; esac; }
proxy_agent_pool_blocked() { case " $BLOCKED " in *" $1 "*) return 0;; *) return 1;; esac; }
proxy_agent_burn_refused() { return 1; }
proxy_agent_burn_enforced() { return 1; }
proxy_agent_emergency_open() { return 1; }
TYPE="$ROLE"
proxy_agent_pick_tier "$TIER" $NESTED
printf '%s|%s' "$PICK_MODEL" "$PICK_REASON"
'''

ROTATE_HARNESS = PRELUDE + r'''
proxy_agent_opencode_rotate "$MODELS"
'''

MID = 'ornith-1.5-35b-a3b deepseek-v4.1-flash glm-5.3 hy4-flash-free claude-sonnet-5-5 gpt-6.1-terra muse-spark-1.3-contributor'


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.cursor = self.home / '.claude/state/proxy-opencode-rotation'

    def tearDown(self):
        self.tmp.cleanup()

    def env(self, **extra):
        env = dict(os.environ, HOME=str(self.home), QWEN_HEALTH_URL='http://127.0.0.1:1/metrics',
                   INKLING_HEALTH_URL='http://127.0.0.1:1/metrics', UNAVAILABLE='', BLOCKED='', NESTED='', TIER='MID', ROLE='claude')
        env.pop('PROXY_AGENT_DRY_RUN', None)
        env.pop('PROXY_AGENT_OPENCODE_ROTATION_FILE', None)
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def pick(self, **extra):
        done = subprocess.run(['bash', '-c', HARNESS], env=self.env(**extra), text=True, capture_output=True, check=True)
        model, reason = done.stdout.split('|', 1)
        return model, reason

    def rotate(self, models=MID, last=None):
        if last is not None:
            self.cursor.parent.mkdir(parents=True, exist_ok=True)
            self.cursor.write_text(last + '\n')
        done = subprocess.run(['bash', '-c', ROTATE_HARNESS], env=self.env(MODELS=models), text=True, capture_output=True, check=True)
        return done.stdout.strip()

    def read_cursor(self):
        return self.cursor.read_text().strip() if self.cursor.exists() else None


class Rotate(Base):
    def test_default_order_when_no_cursor(self):
        self.assertEqual(self.rotate(), MID)

    def test_each_cursor_starts_on_the_next_ring_id_and_keeps_other_slots(self):
        cases = {
            'deepseek-v4.1-flash': 'ornith-1.5-35b-a3b glm-5.3 hy4-flash-free deepseek-v4.1-flash claude-sonnet-5-5 gpt-6.1-terra muse-spark-1.3-contributor',
            'glm-5.3': 'ornith-1.5-35b-a3b hy4-flash-free deepseek-v4.1-flash glm-5.3 claude-sonnet-5-5 gpt-6.1-terra muse-spark-1.3-contributor',
            'hy4-flash-free': MID,
        }
        for last, expected in cases.items():
            with self.subTest(last=last):
                self.assertEqual(self.rotate(last=last), expected)

    def test_unknown_or_garbage_cursor_falls_back_to_default(self):
        for junk in ('claude-sonnet-5-5', '', 'deepseek-v4.1-flash(high)'):
            with self.subTest(junk=junk):
                self.assertEqual(self.rotate(last=junk), MID)

    def test_two_ring_ids_present_rotate_between_themselves(self):
        two = 'ornith-1.5-35b-a3b deepseek-v4.1-flash hy4-flash-free claude-haiku-5-5 gpt-6-luna'
        self.assertEqual(self.rotate(two, last='deepseek-v4.1-flash'), 'ornith-1.5-35b-a3b hy4-flash-free deepseek-v4.1-flash claude-haiku-5-5 gpt-6-luna')
        self.assertEqual(self.rotate(two, last='hy4-flash-free'), two)

    def test_fewer_than_two_ring_ids_is_unchanged(self):
        for models in ('gpt-6.1-terra gpt-6.2-sol claude-opus-5-5 grok-4.7', 'claude-sonnet-5-5 glm-5.3 gpt-6.1-terra'):
            with self.subTest(models=models):
                self.assertEqual(self.rotate(models, last='glm-5.3'), models)

    def test_missing_cursor_dir_is_fine(self):
        self.assertFalse(self.cursor.parent.exists())
        self.assertEqual(self.rotate(), MID)


class Pick(Base):
    def test_successive_builder_launches_cycle_every_opencode_model(self):
        got = [self.pick(ROLE='hd-builder')[0] for _ in range(6)]
        self.assertEqual(got, ['deepseek-v4.1-flash', 'glm-5.3', 'hy4-flash-free'] * 2)
        self.assertEqual(self.read_cursor(), 'hy4-flash-free')

    def test_non_builder_skips_free_guard_model_and_alternates(self):
        got = [self.pick(ROLE='claude') for _ in range(4)]
        self.assertEqual([m for m, _ in got], ['deepseek-v4.1-flash', 'glm-5.3', 'deepseek-v4.1-flash', 'glm-5.3'])
        # the guard skip is reported in the reason on the launch that started on hy4
        self.assertEqual(got[2][1].count('hy4-flash-free:free_model_guard'), 1)

    def test_cooled_ring_id_is_skipped_without_stalling_the_ring(self):
        self.assertEqual(self.pick(ROLE='hd-builder')[0], 'deepseek-v4.1-flash')
        model, reason = self.pick(ROLE='hd-builder', UNAVAILABLE='glm-5.3')
        self.assertEqual(model, 'hy4-flash-free')
        self.assertIn('glm-5.3', reason)
        self.assertEqual(self.pick(ROLE='hd-builder', UNAVAILABLE='glm-5.3')[0], 'deepseek-v4.1-flash')

    def test_pool_capped_ring_id_is_skipped(self):
        self.assertEqual(self.pick(ROLE='hd-builder', BLOCKED='deepseek-v4.1-flash glm-5.3 hy4-flash-free sarvam-glm-5.3 sarvam-deepseek-v4.1-flash sarvam-105b sarvam-gemma4')[0], 'claude-sonnet-5-5')
        # no ring id picked, so the cursor never moved
        self.assertIsNone(self.read_cursor())

    def test_non_opencode_pick_leaves_cursor_alone(self):
        self.cursor.parent.mkdir(parents=True, exist_ok=True)
        self.cursor.write_text('glm-5.3\n')
        model, _ = self.pick(ROLE='hd-builder', UNAVAILABLE='deepseek-v4.1-flash glm-5.3 hy4-flash-free sarvam-glm-5.3 sarvam-deepseek-v4.1-flash sarvam-105b sarvam-gemma4')
        self.assertEqual(model, 'claude-sonnet-5-5')
        self.assertEqual(self.read_cursor(), 'glm-5.3')

    def test_dry_run_reads_cursor_but_never_advances_it(self):
        self.cursor.parent.mkdir(parents=True, exist_ok=True)
        self.cursor.write_text('deepseek-v4.1-flash\n')
        first = self.pick(ROLE='hd-builder', PROXY_AGENT_DRY_RUN=1)[0]
        second = self.pick(ROLE='hd-builder', PROXY_AGENT_DRY_RUN=1)[0]
        self.assertEqual((first, second), ('glm-5.3', 'glm-5.3'))
        self.assertEqual(self.read_cursor(), 'deepseek-v4.1-flash')

    def test_dry_run_alone_never_creates_the_cursor(self):
        self.pick(ROLE='hd-builder', PROXY_AGENT_DRY_RUN=1)
        self.assertFalse(self.cursor.exists())

    def test_nested_alias_picks_never_touch_the_cursor_or_use_opencode(self):
        self.cursor.parent.mkdir(parents=True, exist_ok=True)
        self.cursor.write_text('deepseek-v4.1-flash\n')
        model, _ = self.pick(ROLE='hd-builder', NESTED='nested')
        self.assertEqual(model, 'claude-sonnet-5-5')
        self.assertEqual(self.read_cursor(), 'deepseek-v4.1-flash')

    def test_record_is_atomic_and_leaves_no_temp_files(self):
        for _ in range(4):
            self.pick(ROLE='hd-builder')
        self.assertEqual(sorted(p.name for p in self.cursor.parent.iterdir()), ['proxy-opencode-rotation'])

    def test_unwritable_state_dir_never_fails_the_launch(self):
        self.cursor.parent.mkdir(parents=True)
        os.chmod(self.cursor.parent, 0o500)
        try:
            model, _ = self.pick(ROLE='hd-builder')
        finally:
            os.chmod(self.cursor.parent, 0o700)
        self.assertEqual(model, 'deepseek-v4.1-flash')

    def test_judgment_lane_keeps_claude_first_and_is_not_rotated_ahead_of_it(self):
        # judgment lanes reorder to Claude/Codex first; the rotation only permutes ring ids inside their own slots, so Claude still wins.
        for _ in range(3):
            self.assertEqual(self.pick(ROLE='hd-investigator', TIER='MID')[0], 'claude-sonnet-5-5')


if __name__ == '__main__':
    unittest.main()
