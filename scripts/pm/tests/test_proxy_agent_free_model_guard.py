"""Contract tests for the free-model scope gate in proxy-agent.sh (added to the repo; not run on the host).

Rajiv DM D0AMF0XE6TS ts 1794984605.382609 ("yes"): block free/trial OpenCode models (hy4-flash-free, muse-spark-1.3-contributor)
for agents that read customer content or write prod.

CTO REVISE of b4229671 (C0ALZJHGE49 thread 1794985651.254889, ts 1794986198.378749): the decision is bound to a VERIFIED safe
scope, default deny. A missing or ambiguous scope is ineligible, brief text can only veto, a remote-qa-runner needs a verified
replay-only mode (the spec lane's parsed --on-miss decides, not brief text), and the verdict survives `--resume` and automatic
continuation. Only the two ids are blocked; caps, the emergency gate and paid routing are untouched.

Every input here is synthetic (no customer data). Static traces plus bash snippets over sliced functions; nothing is run on the host.
"""
import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

ASSETS = Path(__file__).parents[1] / 'shared-assets/claude/scripts'
SRC = (ASSETS / 'proxy-agent.sh').read_text()
QA_SPEC = (ASSETS / 'remote-qa-spec.sh').read_text()
HY4 = 'hy4-flash-free'
MUSE = 'muse-spark-1.3-contributor'
DEEPSEEK = 'deepseek-v4.1-flash'
SCOPE = 'no-customer-content,no-prod-write'
BENIGN = 'Fix the sidebar spacing.\n'
NO_BRIEF = None
MISSING_BRIEF = object()


def _slice(start, end):
    first = SRC.index(start)
    return SRC[first:SRC.index(end, first)]


# Tier lists, the scope gate (classify/decide/restore/record), the guard helpers and proxy_agent_pick_tier.
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


def _run(tmp_path, body, brief=NO_BRIEF, scope='', qa_mode='', resume='', run_name='', spec_script=None, cooled=''):
    """Run `body` after the sliced gate. `brief`: None = no ROLE_BRIEF, MISSING_BRIEF = a path that does not exist, str = file text."""
    brief_path = ''
    if brief is MISSING_BRIEF:
        brief_path = str(tmp_path / 'absent-brief.md')
    elif brief is not None:
        brief_file = tmp_path / 'brief.md'
        brief_file.write_text(brief)
        brief_path = str(brief_file)
    state = tmp_path / 'state'
    if run_name:
        (state / run_name).mkdir(parents=True, exist_ok=True)
    if spec_script is not None:
        spec = tmp_path / '.claude/scripts/remote-qa-spec.sh'
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text(spec_script)
    header = (f'set -euo pipefail\nROLE_BRIEF={shlex.quote(brief_path)}\nDATA_SCOPE_ARG={shlex.quote(scope)}\n'
              f'QA_MODE_ARG={shlex.quote(qa_mode)}\nRESUME_NAME={shlex.quote(resume)}\nPROXY_AGENT_RUN_NAME={shlex.quote(run_name)}\n')
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(tmp_path), 'PROXY_AGENT_RUN_STATE_DIR': str(state),
           'STUB_COOLED': cooled}
    return subprocess.run(['bash', '-c', header + STUBS + PICKER + GUARD + body], capture_output=True, text=True, env=env)


def _verdict(tmp_path, agent_type, **kwargs):
    """Run proxy_agent_free_scope_classify (fresh launch, or --resume when `resume` is set) and return its verdict fields."""
    body = (f'TYPE={agent_type}\nproxy_agent_free_scope_classify\n'
            'printf "scope=%s why=%s pin=%s\\n" "$PA_FREE_SCOPE" "$PA_FREE_SCOPE_WHY" "${PROXY_AGENT_QA_REPLAY_ONLY-unset}"\n')
    result = _run(tmp_path, body, **kwargs)
    assert result.returncode == 0, result.stderr
    return dict(part.split('=', 1) for part in result.stdout.strip().splitlines()[-1].split(' '))


def _pick(tmp_path, agent_type, tier, cooled='claude-haiku-5-5', **kwargs):
    body = (f'TYPE={agent_type}\nproxy_agent_free_scope_classify\nproxy_agent_pick_tier {tier} && rc=0 || rc=$?\n'
            'printf "rc=%s model=%s\\n" "$rc" "${PICK_MODEL-}"\n')
    result = _run(tmp_path, body, cooled=cooled, **kwargs)
    assert result.returncode == 0, result.stderr
    rc, model = (part.split('=', 1)[1] for part in result.stdout.strip().splitlines()[-1].split(' ', 1))
    return rc, model, result.stderr


def _primary(tmp_path, agent_type, model, **kwargs):
    body = (f'TYPE={agent_type}\nproxy_agent_free_scope_classify\nMODEL={model}\n'
            'proxy_agent_primary_pool_guard && rc=0 || rc=$?\nprintf "rc=%s\\n" "$rc"\n')
    result = _run(tmp_path, body, **kwargs)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1], result.stderr


def test_only_the_two_approved_ids_are_free_trial_ids():
    assert re.search(r'proxy_agent_is_free_trial_id\(\) \{\n\s+case "\$1" in hy4-flash-free\|muse-spark-1\.3-contributor\) return 0;;', SRC)


# ---- default deny: scope must be declared and verified ------------------------------------------------------------

def test_a_declared_scope_with_a_benign_brief_is_the_only_way_to_a_verified_verdict(tmp_path):
    assert _verdict(tmp_path, 'hd-builder', brief=BENIGN, scope=SCOPE)['scope'] == 'verified'
    rc, model, stderr = _pick(tmp_path, 'hd-builder', 'LIGHT', brief=BENIGN, scope=SCOPE)
    assert (rc, model) == ('0', HY4) and 'REFUSED free-model guard' not in stderr


def test_missing_scope_denies_even_a_benign_launch_of_any_role(tmp_path):
    for agent_type in ('hd-builder', 'hd-planner', 'some-new-unclassified-role'):
        fields = _verdict(tmp_path, agent_type, brief=BENIGN, scope='')
        assert fields['scope'] == 'denied' and fields['why'] == 'scope_missing', agent_type
    rc, model, stderr = _pick(tmp_path, 'some-new-unclassified-role', 'LIGHT', brief=BENIGN)
    assert (rc, model) == ('0', 'grok-4.7')
    assert f'REFUSED free-model guard model={HY4}' in stderr and 'why=scope_missing' in stderr


@pytest.mark.parametrize('scope', ('no-customer-content', 'no-prod-write', 'customer-content,prod-write', SCOPE + ',extra', 'safe', ' ' + SCOPE))
def test_unrecognised_or_ambiguous_scope_denies(tmp_path, scope):
    fields = _verdict(tmp_path, 'hd-builder', brief=BENIGN, scope=scope)
    assert fields['scope'] == 'denied' and fields['why'] == 'scope_ambiguous'


@pytest.mark.parametrize('brief', (NO_BRIEF, MISSING_BRIEF))
def test_a_missing_or_unreadable_brief_denies_even_with_a_declared_scope(tmp_path, brief):
    fields = _verdict(tmp_path, 'hd-builder', brief=brief, scope=SCOPE)
    assert fields['scope'] == 'denied' and fields['why'] == 'brief_unreadable'


@pytest.mark.parametrize('agent_type', (
    'customer-artifact-investigator', 'hd-investigator', 'sync-manager-specialist', 'pm-lock-release',
    'prod-admin-retry', 'prod-modal-deploy', 'editor-lockout-specialist'))
def test_a_sensitive_role_is_denied_even_with_a_declared_scope(tmp_path, agent_type):
    fields = _verdict(tmp_path, agent_type, brief=BENIGN, scope=SCOPE)
    assert fields['scope'] == 'denied' and fields['why'] == 'sensitive_role'
    rc, model, stderr = _pick(tmp_path, agent_type, 'LIGHT', brief=BENIGN, scope=SCOPE)
    assert (rc, model) == ('0', 'grok-4.7') and f'REFUSED free-model guard model={HY4}' in stderr


@pytest.mark.parametrize('text', (
    'Read the beloved-cardinal-941 deployment.', 'Write to prod Convex.', 'Check production data.', 'Reply to the customer.',
    'Customers lost saves.'))
def test_brief_text_can_veto_a_declared_scope(tmp_path, text):
    fields = _verdict(tmp_path, 'hd-builder', brief=text + '\n', scope=SCOPE)
    assert fields['scope'] == 'denied' and fields['why'] == 'brief_names_sensitive_scope'


@pytest.mark.parametrize('text', ('Update the product copy.', 'Reproduce the layout bug.', 'Improve the producer queue.'))
def test_prod_inside_another_word_does_not_veto(tmp_path, text):
    assert _verdict(tmp_path, 'hd-builder', brief=text + '\n', scope=SCOPE)['scope'] == 'verified'


def test_brief_text_alone_never_grants_eligibility(tmp_path):
    # Text claiming safety, strict replay or "no customer content" is advisory: no declared scope means denied.
    text = 'Synthetic data only. Strict replay. Safe for any model.\n'
    fields = _verdict(tmp_path, 'hd-builder', brief=text, scope='')
    assert fields['scope'] == 'denied' and fields['why'] == 'scope_missing'


def test_only_the_two_ids_are_blocked_and_paid_routing_is_unchanged(tmp_path):
    rc, model, _ = _pick(tmp_path, 'hd-builder', 'MID', cooled='claude-sonnet-5-5', brief=BENIGN, scope='')
    assert (rc, model) == ('0', DEEPSEEK)
    rc, model, _ = _pick(tmp_path, 'hd-builder', 'LIGHT', cooled='', brief=BENIGN, scope='')
    assert (rc, model) == ('0', 'claude-haiku-5-5')
    rc, stderr = _primary(tmp_path, 'hd-builder', DEEPSEEK, brief=BENIGN, scope='')
    assert rc == 'rc=0' and 'free-model guard' not in stderr
    rc, stderr = _primary(tmp_path, 'hd-builder', 'claude-haiku-5-5', brief=BENIGN, scope='')
    assert rc == 'rc=0' and 'free-model guard' not in stderr


def test_muse_follows_the_scope_verdict_and_the_emergency_gate_is_unchanged(tmp_path):
    cooled = ' '.join(r for r in _all_routes() if r != MUSE)
    rc, model, stderr = _pick(tmp_path, 'hd-builder', 'PLAN', cooled=cooled, brief=BENIGN, scope='')
    assert (rc, model) == ('75', '') and f'REFUSED free-model guard model={MUSE}' in stderr
    rc, model, _ = _pick(tmp_path, 'hd-builder', 'PLAN', cooled=cooled, brief=BENIGN, scope=SCOPE)
    assert (rc, model) == ('0', MUSE)


@pytest.mark.parametrize('model', (HY4, MUSE))
def test_a_raw_free_primary_needs_a_verified_scope(tmp_path, model):
    rc, stderr = _primary(tmp_path, 'hd-builder', model, brief=BENIGN, scope='')
    assert rc == 'rc=75' and f'REFUSED free-model guard primary {model}' in stderr and 'why=scope_missing' in stderr
    rc, stderr = _primary(tmp_path, 'customer-artifact-investigator', model, brief=BENIGN, scope=SCOPE)
    assert rc == 'rc=75' and 'why=sensitive_role' in stderr
    # A verified scope only lifts this gate; the pool cap and Muse emergency gate (test_proxy_agent_opencode_pool.py) still apply.
    rc, _ = _primary(tmp_path, 'hd-builder', HY4, brief=BENIGN, scope=SCOPE)
    assert rc == 'rc=0'


def test_a_free_id_is_refused_when_no_verdict_was_ever_computed(tmp_path):
    body = 'TYPE=hd-builder\nunset PA_FREE_SCOPE\nproxy_agent_free_guard_refuses hy4-flash-free && echo refused || echo allowed\n'
    result = _run(tmp_path, body, brief=BENIGN, scope=SCOPE)
    assert result.stdout.strip().splitlines()[-1] == 'refused', result.stderr


# ---- QA: replay eligibility follows the verified route, not brief text ---------------------------------------------

REPLAY_SPEC = 'echo "QA_REPLAY_ONLY_REFUSED"\n'


def test_strict_replay_wording_in_a_brief_is_not_evidence_for_a_qa_runner(tmp_path):
    brief = 'Run the QA scenario with --strict-replay. Strict replay only.\n'
    fields = _verdict(tmp_path, 'remote-qa-runner', brief=brief, scope=SCOPE, spec_script=REPLAY_SPEC)
    assert fields['scope'] == 'denied' and fields['why'] == 'qa_replay_only_unverified' and fields['pin'] == 'unset'
    rc, model, stderr = _pick(tmp_path, 'remote-qa-runner', 'LIGHT', brief=brief, scope=SCOPE, spec_script=REPLAY_SPEC)
    assert (rc, model) == ('0', 'grok-4.7') and f'REFUSED free-model guard model={HY4}' in stderr


def test_replay_only_needs_the_route_to_carry_the_enforcement(tmp_path):
    for spec in (None, 'echo "an older spec lane that still accepts --on-miss"\n'):
        fields = _verdict(tmp_path, 'remote-qa-runner', brief=BENIGN, scope=SCOPE, qa_mode='replay-only', spec_script=spec)
        assert fields['scope'] == 'denied' and fields['why'] == 'qa_replay_only_unverified' and fields['pin'] == 'unset'


def test_a_declared_replay_only_mode_on_a_capable_route_with_a_safe_scope_is_eligible_and_pinned(tmp_path):
    fields = _verdict(tmp_path, 'remote-qa-runner', brief=BENIGN, scope=SCOPE, qa_mode='replay-only', spec_script=REPLAY_SPEC)
    assert fields['scope'] == 'verified' and fields['pin'] == '1'
    rc, model, _ = _pick(tmp_path, 'remote-qa-runner', 'LIGHT', brief=BENIGN, scope=SCOPE, qa_mode='replay-only', spec_script=REPLAY_SPEC)
    assert (rc, model) == ('0', HY4)


def test_replay_only_without_a_safe_scope_is_still_denied(tmp_path):
    fields = _verdict(tmp_path, 'remote-qa-runner', brief=BENIGN, scope='', qa_mode='replay-only', spec_script=REPLAY_SPEC)
    assert fields['scope'] == 'denied' and fields['why'] == 'scope_missing'


def test_the_replay_pin_is_an_exported_launcher_mode_and_the_capture_lane_is_denied():
    assert 'export PROXY_AGENT_QA_REPLAY_ONLY=1' in SRC
    assert 'QA_REPLAY_ONLY="${PA_QA_REPLAY_PINNED:-0}" python3 -' in SRC
    assert '"Bash(*remote-browser-box.sh capture*)", "Bash(*remote-qa-capture.sh*)", "Bash(*--on-miss*)"' in SRC


def _qa_guard():
    start = QA_SPEC.index('if [ "${PROXY_AGENT_QA_REPLAY_ONLY:-}" = 1 ]')
    return QA_SPEC[start:QA_SPEC.index('if [ -n "$llm_onmiss" ]; then\n  # CTO bounds', start)]


@pytest.mark.parametrize('pin,onmiss,expected_rc', (('1', '--on-miss', 2), ('1', '', 0), ('', '--on-miss', 0)))
def test_the_spec_lane_refuses_a_parsed_on_miss_only_under_the_launcher_pin(tmp_path, pin, onmiss, expected_rc):
    script = f'llm_onmiss={shlex.quote(onmiss)}\n{_qa_guard()}\necho reached\n'
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'PROXY_AGENT_QA_REPLAY_ONLY': pin}
    result = subprocess.run(['bash', '-c', script], capture_output=True, text=True, env=env)
    assert result.returncode == expected_rc
    assert ('QA_REPLAY_ONLY_REFUSED' in result.stderr) == (expected_rc == 2)
    assert ('reached' in result.stdout) == (expected_rc == 0)


def test_the_spec_lane_refusal_precedes_credentials_leases_and_dry_run():
    refusal = QA_SPEC.index('QA_REPLAY_ONLY_REFUSED')
    assert refusal < QA_SPEC.index('if [ -n "$llm_onmiss" ]; then\n  # CTO bounds')
    assert refusal < QA_SPEC.index('if [ "$dry_run" = yes ]; then echo "DRY_RUN')
    assert refusal < QA_SPEC.index('check-credentials')


# ---- resume: the recorded verdict is restored, never recomputed ----------------------------------------------------

def test_the_verdict_recorded_at_launch_is_restored_on_resume_without_any_declaration(tmp_path):
    first = _verdict(tmp_path, 'hd-builder', brief=BENIGN, scope=SCOPE, run_name='run1')
    assert first['scope'] == 'verified'
    assert (tmp_path / 'state/run1/free-scope').is_file()
    # --resume carries no --data-scope; the restored role/brief only confirm the record belongs to them.
    again = _verdict(tmp_path, 'hd-builder', brief=BENIGN, scope='', resume='run1', run_name='run1')
    assert again['scope'] == 'verified' and again['why'].startswith('resume:')
    rc, model, _ = _pick(tmp_path, 'hd-builder', 'LIGHT', brief=BENIGN, scope='', resume='run1', run_name='run1')
    assert (rc, model) == ('0', HY4)


def test_a_denied_launch_stays_denied_on_resume_whatever_the_restored_inputs_say(tmp_path):
    assert _verdict(tmp_path, 'hd-builder', brief=BENIGN, scope='', run_name='run2')['scope'] == 'denied'
    # The old heuristic would re-run on a benign restored brief and pass; the record wins, and a resume-time --data-scope is ignored.
    again = _verdict(tmp_path, 'hd-builder', brief=BENIGN, scope=SCOPE, resume='run2', run_name='run2')
    assert again['scope'] == 'denied' and again['why'] == 'resume:scope_missing'
    rc, stderr = _primary(tmp_path, 'hd-builder', HY4, brief=BENIGN, scope=SCOPE, resume='run2', run_name='run2')
    assert rc == 'rc=75' and 'REFUSED free-model guard primary' in stderr


def test_resume_without_a_record_is_denied(tmp_path):
    fields = _verdict(tmp_path, 'hd-builder', brief=BENIGN, scope=SCOPE, resume='never-launched', run_name='never-launched')
    assert fields['scope'] == 'denied' and fields['why'] == 'resume_scope_unrecorded'


def test_resume_with_a_changed_brief_or_role_is_denied(tmp_path):
    assert _verdict(tmp_path, 'hd-builder', brief=BENIGN, scope=SCOPE, run_name='run3')['scope'] == 'verified'
    changed = _verdict(tmp_path, 'hd-builder', brief=BENIGN + 'One more line.\n', scope='', resume='run3', run_name='run3')
    assert changed['scope'] == 'denied' and changed['why'] == 'resume_scope_mismatch'
    other = _verdict(tmp_path, 'hd-planner', brief=BENIGN, scope='', resume='run3', run_name='run3')
    assert other['scope'] == 'denied' and other['why'] == 'resume_scope_mismatch'


def test_the_qa_replay_pin_is_restored_on_resume(tmp_path):
    first = _verdict(tmp_path, 'remote-qa-runner', brief=BENIGN, scope=SCOPE, qa_mode='replay-only', spec_script=REPLAY_SPEC, run_name='run4')
    assert first['scope'] == 'verified' and first['pin'] == '1'
    again = _verdict(tmp_path, 'remote-qa-runner', brief=BENIGN, scope='', qa_mode='', resume='run4', run_name='run4')
    assert again['scope'] == 'verified' and again['pin'] == '1'


def test_classification_runs_once_after_role_resolution_and_resume_never_decides_again():
    assert 'proxy_agent_resolve_type\nproxy_agent_free_scope_classify\nproxy_agent_select || exit 75' in SRC
    classify = _slice('proxy_agent_free_scope_classify() {', '# 0 (refuse) when model id $1 is a free/trial id')
    resumed = classify[:classify.index('else')]
    assert 'proxy_agent_free_scope_restore' in resumed and 'proxy_agent_free_scope_decide' not in resumed
    assert classify.index('proxy_agent_free_scope_decide') < classify.index('proxy_agent_free_scope_record')
    assert SRC.count('proxy_agent_free_scope_decide\n') == 1 and 'proxy_agent_sensitive_launch' not in SRC


# ---- automatic continuation ----------------------------------------------------------------------------------------

def test_auto_continuation_reuses_the_launch_model_and_stops_a_free_run_without_a_verified_verdict():
    runner = _slice("  case \"$TYPE\" in\n    pm-lock-release|prod-*|manual-lease-release", "  printf 'printf \"Continue the task")
    guard = "printf 'case %q in hy4-flash-free|muse-spark-1.3-contributor) [[ %q == verified ]] || PROXY_AGENT_NO_CONTINUE=1;; esac\\n' \"$MODEL_ID\" \"${PA_FREE_SCOPE:-denied}\""
    assert guard in runner
    # The first run and the continuation both expand the same ARGS (one --model), so no continuation re-selects a model.
    assert SRC.count('--model "$MODEL_ID" --max-turns') == 1
    assert SRC.count('printf \'%q \' "$CLAUDE_BIN" "${ARGS[@]}"') == 2
    loop = SRC.index("printf 'while [[ ( \"$rc\" == 0")
    assert SRC.index(guard) < loop


# ---- ordering inside the picker ------------------------------------------------------------------------------------

def test_guard_runs_before_the_emergency_gate_and_before_last_is_set():
    pick = _slice('proxy_agent_pick_tier() {', '# Ordinary burn gate for capped roles')
    assert pick.index('proxy_agent_free_guard_refuses "$id"') < pick.index('proxy_agent_is_emergency_muse "$id"') < pick.index('last="$id"')
