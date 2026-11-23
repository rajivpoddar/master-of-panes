import os
import pathlib
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(os.environ.get('PROXY_AGENT_TEST_SCRIPT', pathlib.Path.home() / '.claude/scripts/proxy-agent.sh'))


class CodexPrimary(unittest.TestCase):
    def run_guard(self, model, live='8', flag=False, role='hd-builder', override='', p0_ref='', helper_rc=None):
        source = SCRIPT.read_text()
        functions = source[source.index('proxy_agent_cap_exempt() {'):source.index('# Primary burn check')]
        # The guard also needs these (the harness predates them): extracted BY NAME from the same script, never re-implemented here.
        lines = source.split('\n')

        def extract(name):
            start = next(i for i, l in enumerate(lines) if l.startswith(name + '() {'))
            if lines[start].rstrip().endswith('}') and lines[start].count('{') == lines[start].count('}'):
                return lines[start]
            return '\n'.join(lines[start:next(i for i in range(start + 1, len(lines)) if lines[i] == '}') + 1])
        needed = next(l for l in lines if l.startswith('PROXY_AGENT_FREE_TRIAL_IDS=')) + '\n' + '\n'.join(extract(n) for n in (
            'proxy_agent_router_identity', 'proxy_agent_is_free_trial_id', 'proxy_agent_free_guard_refuses', 'proxy_agent_is_unresolved_auto',
            'proxy_agent_is_sarvam_id', 'proxy_agent_sarvam_kimi_gated')) + '\n'
        functions = needed + functions
        resolver = source[source.index('proxy_agent_alias_for_id() {'):source.index('\nproxy_agent_print_selection() {')]
        shell = functions + '\n' + resolver + r'''
proxy_agent_is_opencode_id() { case "$1" in hy4-flash-free|deepseek-v4.1-flash|glm-5.3|muse-spark-1.3-contributor) return 0;; *) return 1;; esac; }
proxy_agent_is_emergency_muse() { [[ "$1" == muse-spark-1.3-contributor ]]; }
MODEL="$REQUESTED" TYPE="$ROLE" ROLE_BRIEF=/nonexistent
proxy_agent_primary_pool_guard || exit $?
read -r id window max < <(resolve_model "$MODEL")
printf 'launch model=%s\n' "$id"
'''
        with tempfile.TemporaryDirectory() as home:
            state = pathlib.Path(home) / '.claude/state'
            state.mkdir(parents=True)
            if flag:
                (state / 'proxy-no-codex').touch()
            env = dict(os.environ, HOME=home, REQUESTED=model, ROLE=role,
                       PROXY_AGENT_STUB_POOL_LIVE=live, CODEX_PROXY_CAP='8',
                       PROXY_AGENT_CAP_OVERRIDE=override, PA_P0_REF=p0_ref)
            if helper_rc is not None:  # a stub proxy-preempt.py with a fixed exit code (3 = nothing preemptable)
                stub = pathlib.Path(home) / 'preempt-stub.py'
                stub.write_text(f'import sys\nsys.exit({helper_rc})\n')
                env['PROXY_AGENT_PREEMPT_HELPER'] = str(stub)
            return subprocess.run(['bash', '-c', shell], env=env, text=True, capture_output=True)

    def test_codex_cap_and_under_cap(self):
        for model, expected in [('sol', 'gpt-6.2-sol'), ('gpt-6.1-terra', 'gpt-6.1-terra')]:
            with self.subTest(model=model):
                capped = self.run_guard(model)
                self.assertEqual(capped.returncode, 75, capped.stderr)
                self.assertIn('REFUSED primary ' + expected + ' (codex pool_cap)', capped.stderr)
                allowed = self.run_guard(model, live='7')
                self.assertEqual(allowed.returncode, 0, allowed.stderr)
                self.assertIn('launch model=' + expected, allowed.stdout)

    def test_no_codex_and_exemptions(self):
        for model in ('sol', 'gpt-6.1-terra', 'gpt-custom'):
            with self.subTest(model=model):
                refused = self.run_guard(model, live='0', flag=True, override='P0:123')
                self.assertEqual(refused.returncode, 75, refused.stderr)
                self.assertIn('proxy-no-codex', refused.stderr)
                self.assertEqual(self.run_guard(model, role='pm-ops-tooling').returncode, 0)

    def test_a_p0_override_no__longer_lifts_the_codex_cap_and_a_p0_with_nothing_to_preempt_is_refused(self):
        # Rajiv C0ALZJHGE49 ts 1795439359.022079: a P0 preempts at admission or is queued; it is never launched over a cap.
        for model in ('sol', 'gpt-6.1-terra'):
            with self.subTest(model=model):
                self.assertEqual(self.run_guard(model, override='P0:123').returncode, 75)  # the override alone is only an identity marker
                self.assertEqual(self.run_guard(model, p0_ref='P0:123', helper_rc=3).returncode, 75)  # P0 at cap, helper finds no victim
                self.assertEqual(self.run_guard(model, p0_ref='P0:123', helper_rc=3, role='pm-ops-tooling').returncode, 0)  # role exemption untouched

if __name__ == '__main__':
    unittest.main()
