"""Contract tests for the UNCONDITIONAL free-model guard in proxy-agent.sh (added to the repo; not run on the host).

Rajiv DM D0AMF0XE6TS ts 1794984605.382609 ("yes"): block free/trial OpenCode models (hy4-flash-free, muse-spark-1.3-contributor)
for agents that read customer content or write prod.

CTO REVISE of 970d140 / 9ea1b9b (C0ALZJHGE49 thread 1794985651.254889, ts 1794987952.900739): a caller-declared --data-scope plus
role/brief checks, a QA replay mode and a recorded resume verdict do not bind what the launched tools can read or write. No restricted
execution profile exists, so both ids are ineligible for EVERY launch, --resume and auto-continuation. There is no allow path. Only
the two ids are blocked; caps, the Muse emergency gate (test_proxy_agent_opencode_pool.py) and every other model are unchanged.

Every input here is synthetic (no customer data). Static traces plus bash snippets over sliced functions; nothing is run on the host.
"""
import hashlib
import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

ASSETS = Path(__file__).parents[1] / 'shared-assets/claude/scripts'
SRC = (ASSETS / 'proxy-agent.sh').read_text()
QA_SPEC = (ASSETS / 'remote-qa-spec.sh').read_text()
SKILL = (Path(__file__).parents[1] / 'shared-assets/claude/skills/proxy-agent/SKILL.md').read_text()
HY4 = 'hy4-flash-free'
MUSE = 'muse-spark-1.3-contributor'
DEEPSEEK = 'deepseek-v4.1-flash'
GLM = 'glm-5.3'
FREE_IDS = (HY4, MUSE)
# Router aliases that resolve to the same two upstreams (cliproxyapi.conf openai-compatibility `opencode-go`).
HY4_ALIAS = 'hy4-flash'
MUSE_ALIAS = 'opencode-go-muse-spark-1.3-contributor'
# Decorated names CLIProxyAPI 8.0.50 routes to the restricted base model (suffix stripped, provider resolved from the base) plus a Claude
# Code context suffix: each used to pass the literal compare and reach the router.
DECORATED_FREE = (f'{HY4}(high)', f'{HY4}(8192)', f'{HY4}(none)', f'{MUSE}(xhigh)', f'{MUSE}[1m]', f'{MUSE}[2m]', 'HY4-Flash-Free(High)',
                  f'{HY4_ALIAS}(low)', f'{MUSE_ALIAS}(high)', f' {HY4}(high)', f'{HY4}()')
# Decorated automatic selections (CTO REVISE of c901e33, ts 1794991405.296039): only the LITERAL `auto` is a tier request, so these fell to RAW,
# where the router derives the base identity `auto` and may choose ANY registered model. Each must be refused at the raw primary boundary.
DECORATED_AUTO = ('auto(high)', 'auto(8192)', 'auto(none)', 'auto()', 'auto[1m]', 'auto(xhigh)[1m]', 'AUTO', 'Auto(High)', ' auto(low) ',
                  'auto (high)', 'AUTO[2m]')
# The exact declaration 9ea1b9b treated as "verified", and the QA mode it paired with it.
VERIFIED_FLAGS = ('--data-scope', 'no-customer-content,no-prod-write', '--qa-mode', 'replay-only')
BENIGN = 'Fix the sidebar spacing.\n'


def _slice(start, end):
    first = SRC.index(start)
    return SRC[first:SRC.index(end, first)]


# Router-identity helper + restricted-id list (before the tier lists); tier lists, the guard helpers, the emergency gate and
# proxy_agent_pick_tier; the primary guard; the argv loop.
HELPERS = _slice('proxy_agent_router_identity() {', '# Ordered tiers;')
PICKER = _slice('# OpenCode Go pool: hy4-flash-free', '# Ordinary burn gate for capped roles')
GUARD = _slice('proxy_agent_primary_pool_guard() {', '# Primary burn check (CTO REVISE')
ARGV_LOOP = _slice('while [[ $# -gt 0 ]]; do\n  case "$1" in\n    --cwd)', 'case "$RUNG" in')

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
ARGV_PRELUDE = '''
usage() { echo USAGE; exit 2; }
FREE_SCOPE_FLAG_IGNORED=0; CWD=""; CWD_EXPLICIT=0; MODEL=auto; MODEL_EXPLICIT=0; RUNG=""; OUT=""; MAX_TURNS=200; PM_ROLE=0
'''


def _all_routes():
    tiers = re.findall(r'^\s+([A-Z0-9]+)\) models="([^"]*)";;', PICKER, re.M)
    return sorted({m for _, models in tiers for m in models.split()})


def _run(tmp_path, body, args=(), brief=BENIGN, resume='', cooled='', sidecar=None):
    """Run `body` after the sliced guard. `args` go through the launcher's real argv loop first.

    `sidecar`: text written as the 9ea1b9b per-run verdict record (<state>/<run>/free-scope) for `resume`.
    """
    state = tmp_path / 'state'
    brief_file = tmp_path / 'brief.md'
    brief_file.write_text(brief)
    if sidecar is not None:
        (state / 'run1').mkdir(parents=True, exist_ok=True)
        (state / 'run1/free-scope').write_text(sidecar)
    header = (f'set -euo pipefail\nROLE_BRIEF={shlex.quote(str(brief_file))}\nRESUME_NAME={shlex.quote(resume)}\n'
              f'PROXY_AGENT_RUN_NAME=run1\n')
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(tmp_path), 'PROXY_AGENT_RUN_STATE_DIR': str(state),
           'STUB_COOLED': cooled}
    script = header + ARGV_PRELUDE + STUBS + ARGV_LOOP + PICKER + GUARD + body
    return subprocess.run(['bash', '-c', script, 'bash', *args], capture_output=True, text=True, env=env)


def _verified_sidecar(brief_text, agent_type='hd-builder'):
    """The record 9ea1b9b wrote for a verified launch, digest-bound to the brief so its restore would have accepted it."""
    digest = hashlib.sha256(brief_text.encode()).hexdigest()
    return f'v1\ntype={agent_type}\nbrief_sha256={digest}\nscope=verified\nwhy=scope_declared_and_checked\nqa_replay_only=1\n'


def _pick(tmp_path, agent_type, tier, cooled='claude-haiku-5-5', **kwargs):
    body = (f'TYPE={agent_type}\nproxy_agent_pick_tier {tier} && rc=0 || rc=$?\n'
            'printf "rc=%s model=%s\\n" "$rc" "${PICK_MODEL-}"\n')
    result = _run(tmp_path, body, cooled=cooled, **kwargs)
    assert result.returncode == 0, result.stderr
    rc, model = (part.split('=', 1)[1] for part in result.stdout.strip().splitlines()[-1].split(' ', 1))
    return rc, model, result.stderr


def _primary(tmp_path, agent_type, model, **kwargs):
    body = f'TYPE={agent_type}\nMODEL={model}\nproxy_agent_primary_pool_guard && rc=0 || rc=$?\nprintf "rc=%s\\n" "$rc"\n'
    result = _run(tmp_path, body, **kwargs)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1], result.stderr


def test_only_the_two_restricted_models_and_their_router_aliases_are_free_trial_ids():
    assert 'PROXY_AGENT_FREE_TRIAL_IDS="hy4-flash-free hy4-flash muse-spark-1.3-contributor opencode-go-muse-spark-1.3-contributor"' in SRC
    for other in (DEEPSEEK, GLM):
        assert other not in re.search(r'PROXY_AGENT_FREE_TRIAL_IDS="([^"]*)"', SRC).group(1).split()


# ---- no allow path: nothing a caller, brief, role, QA mode or record says can lift the block ------------------------

def test_the_guard_takes_no_input_but_the_model_id_and_no_allow_machinery_remains():
    body = _slice('proxy_agent_free_guard_refuses() {', '# Ordered tiers;')
    assert body.strip().splitlines()[1].strip() == 'proxy_agent_is_free_trial_id "$(proxy_agent_router_identity "$1")"'
    for gone in ('DATA_SCOPE_ARG', 'QA_MODE_ARG', 'PA_FREE_SCOPE', 'free_scope_classify', 'free_scope_decide', 'free_scope_restore',
                 'free_scope_record', 'free-scope', 'qa_replay_only_verified', 'PA_QA_REPLAY_PINNED', 'PROXY_AGENT_QA_REPLAY_ONLY',
                 'QA_REPLAY_ONLY', 'brief_names_sensitive_scope', 'proxy_agent_sensitive_type', 'proxy_agent_sensitive_launch'):
        assert gone not in SRC, gone
    assert 'QA_REPLAY_ONLY' not in QA_SPEC and '--on-miss' in QA_SPEC  # the 9ea1b9b spec-lane pin is removed; capture-on-miss is untouched


@pytest.mark.parametrize('agent_type', ('hd-builder', 'hd-planner', 'remote-qa-runner', 'some-new-unclassified-role', 'pm-triage-agent'))
@pytest.mark.parametrize('flags', ((), VERIFIED_FLAGS))
def test_a_verified_declaration_no_longer_grants_either_id(tmp_path, agent_type, flags):
    # The full 9ea1b9b "verified" declaration (--data-scope + --qa-mode) with a benign readable brief, for a non-sensitive role.
    rc, model, stderr = _pick(tmp_path, agent_type, 'LIGHT', args=flags)
    assert (rc, model) == ('0', 'grok-4.7')
    assert f'REFUSED free-model guard model={HY4}' in stderr
    for model_id in FREE_IDS:
        rc, stderr = _primary(tmp_path, agent_type, model_id, args=flags)
        assert rc == 'rc=75' and f'REFUSED free-model guard primary {model_id}' in stderr


def test_the_declaration_flags_are_still_accepted_but_ignored_with_a_notice(tmp_path):
    body = 'printf "model=%s ignored=%s\\n" "$MODEL" "$FREE_SCOPE_FLAG_IGNORED"\n'
    result = _run(tmp_path, body, args=(*VERIFIED_FLAGS, '--model', 'terra'))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == 'model=terra ignored=1'
    assert 'unconditionally ineligible' in result.stderr and 'USAGE' not in result.stdout
    plain = _run(tmp_path, body, args=('--model', 'terra'))
    assert plain.stdout.strip().splitlines()[-1] == 'model=terra ignored=0' and 'ignored:' not in plain.stderr


@pytest.mark.parametrize('brief', ('', BENIGN, 'Synthetic data only. Strict replay. Safe for any model.\n', 'Reply to the customer in prod.\n'))
def test_the_brief_neither_grants_nor_changes_the_denial(tmp_path, brief):
    rc, model, _ = _pick(tmp_path, 'hd-builder', 'LIGHT', brief=brief, args=VERIFIED_FLAGS)
    assert (rc, model) == ('0', 'grok-4.7')


# ---- resume: a stored "verified" sidecar is never read -------------------------------------------------------------

def test_a_stored_verified_sidecar_does_not_lift_the_block_on_resume(tmp_path):
    sidecar = _verified_sidecar(BENIGN)
    kwargs = dict(resume='run1', sidecar=sidecar)
    # --resume requires an explicit model: a raw free id is refused, and an alias that lands on LIGHT skips hy4.
    for model_id in FREE_IDS:
        rc, stderr = _primary(tmp_path, 'hd-builder', model_id, **kwargs)
        assert rc == 'rc=75' and f'REFUSED free-model guard primary {model_id}' in stderr
    rc, model, stderr = _pick(tmp_path, 'hd-builder', 'LIGHT', **kwargs)
    assert (rc, model) == ('0', 'grok-4.7') and f'REFUSED free-model guard model={HY4}' in stderr
    assert (tmp_path / 'state/run1/free-scope').read_text() == sidecar  # left alone: the launcher neither reads nor rewrites it


def test_resume_never_looks_at_a_verdict_record():
    assert 'free-scope' not in SRC and 'RESUME_NAME' in SRC
    assert 'proxy_agent_free_scope' not in SRC


# ---- other models are unchanged --------------------------------------------------------------------------------------

def test_only_the_two_ids_are_blocked_and_every_other_route_is_unchanged(tmp_path):
    rc, model, _ = _pick(tmp_path, 'hd-builder', 'MID', cooled='claude-sonnet-5-5')
    assert (rc, model) == ('0', DEEPSEEK)
    rc, model, _ = _pick(tmp_path, 'hd-builder', 'LIGHT', cooled='')
    assert (rc, model) == ('0', 'claude-haiku-5-5')
    rc, model, _ = _pick(tmp_path, 'hd-builder', 'LIGHT', cooled='claude-haiku-5-5 grok-4.7')
    assert (rc, model) == ('0', 'gpt-6-luna')
    for model_id in (DEEPSEEK, GLM, 'claude-haiku-5-5', 'claude-sonnet-5-5', 'claude-opus-5-5', 'grok-4.7', 'gpt-6-luna',
                     'gpt-6.1-terra', 'gpt-6.2-sol', 'gpt-6-astra', 'claude-fable-5-2'):
        rc, stderr = _primary(tmp_path, 'hd-builder', model_id, args=VERIFIED_FLAGS)
        assert rc == 'rc=0' and 'free-model guard' not in stderr, model_id


def test_the_other_opencode_ids_keep_their_pool_cap_check(tmp_path):
    body = ('TYPE=hd-builder\nproxy_agent_pool_blocked() { [[ "$1" == deepseek-v4.1-flash ]]; }\nMODEL=deepseek-v4.1-flash\n'
            'proxy_agent_primary_pool_guard && rc=0 || rc=$?\nprintf "rc=%s\\n" "$rc"\n')
    result = _run(tmp_path, body)
    assert result.stdout.strip().splitlines()[-1] == 'rc=75' and 'REFUSED primary deepseek-v4.1-flash (opencode pool_cap)' in result.stderr


# ---- Muse: refused before the emergency gate, so it is never picked even when that gate would open --------------------

def test_muse_is_never_picked_even_with_every_other_route_exhausted(tmp_path):
    cooled = ' '.join(r for r in _all_routes() if r != MUSE)
    for tier in ('LIGHT', 'PLAN', 'BUILD1', 'REVIEW'):
        rc, model, stderr = _pick(tmp_path, 'hd-builder', tier, cooled=cooled, args=VERIFIED_FLAGS)
        assert (rc, model) == ('75', ''), tier
        assert f'REFUSED free-model guard model={MUSE}' in stderr and 'emergency_gate skip' not in stderr


def test_a_raw_free_primary_is_refused_by_the_guard_not_the_emergency_gate(tmp_path):
    cooled = ' '.join(r for r in _all_routes() if r != MUSE)  # the emergency gate WOULD open for Muse here
    rc, stderr = _primary(tmp_path, 'hd-builder', MUSE, cooled=cooled, args=VERIFIED_FLAGS)
    assert rc == 'rc=75' and f'REFUSED free-model guard primary {MUSE}' in stderr and 'emergency_gate' not in stderr
    rc, stderr = _primary(tmp_path, 'hd-builder', HY4, args=VERIFIED_FLAGS)
    assert rc == 'rc=75' and f'REFUSED free-model guard primary {HY4}' in stderr


# ---- automatic continuation ------------------------------------------------------------------------------------------

def _continuation_line():
    runner = _slice("  case \"$TYPE\" in\n    pm-lock-release|prod-*|manual-lease-release", "  printf 'printf \"Continue the task")
    line = re.search(r"^  printf 'case %q in %s\) PROXY_AGENT_NO_CONTINUE=1;; esac\\n' \"\$\(proxy_agent_router_identity \"\$MODEL_ID\"\)\" "
                     r"\"\$\{PROXY_AGENT_FREE_TRIAL_IDS// /\|\}\|auto\"$", runner, re.M)
    assert line, runner
    return line.group(0).strip()


@pytest.mark.parametrize('model_id,expected', (
    (HY4, '1'), (MUSE, '1'), (HY4_ALIAS, '1'), (MUSE_ALIAS, '1'), *((d, '1') for d in DECORATED_FREE),
    *((a, '1') for a in DECORATED_AUTO), ('auto', '1'),
    (DEEPSEEK, '0'), (f'{DEEPSEEK}(high)', '0'), ('claude-haiku-5-5', '0'), ('claude-opus-5-5[1m]', '0'), ('gpt-6.1-terra', '0'),
    ('x(auto)', '0'), ('autoscale-9(high)', '0')))
def test_the_generated_runner_never_continues_a_free_id_run(tmp_path, model_id, expected):
    # Run the launcher's real generator line (with its real identity helper and list), then the generated line, and read the flag the
    # continuation loop tests. The generated case is keyed on the router-equivalent identity, so a decorated id cannot continue either.
    script = (f'{HELPERS}\nMODEL_ID={shlex.quote(model_id)}\n{_continuation_line()} > gen.sh\nPROXY_AGENT_NO_CONTINUE=0\n'
              'source ./gen.sh\necho "$PROXY_AGENT_NO_CONTINUE"\n')
    result = subprocess.run(['bash', '-c', script], capture_output=True, text=True, cwd=tmp_path,
                            env={'PATH': os.environ.get('PATH', '/usr/bin:/bin')})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_continuation_reuses_the_launch_model_and_the_denial_precedes_the_loop():
    # The first run and the continuation both expand the same ARGS (one --model), so no continuation re-selects a model.
    assert SRC.count('--model "$MODEL_ID" --max-turns') == 1
    assert SRC.count('printf \'%q \' "$CLAUDE_BIN" "${ARGS[@]}"') == 2
    assert SRC.index(_continuation_line()) < SRC.index("printf 'while [[ ( \"$rc\" == 0")


# ---- ordering --------------------------------------------------------------------------------------------------------

def test_the_guard_runs_before_the_emergency_gate_and_before_last_is_set():
    pick = _slice('proxy_agent_pick_tier() {', '# Ordinary burn gate for capped roles')
    assert pick.index('proxy_agent_free_guard_refuses "$id"') < pick.index('proxy_agent_is_emergency_muse "$id"') < pick.index('last="$id"')
    select = _slice('proxy_agent_select() {', '# Model table: alias')
    assert select.index('proxy_agent_primary_pool_guard || return 75') < select.index('proxy_agent_pick_tier LIGHT nested')
    assert 'proxy_agent_resolve_type\nproxy_agent_select || exit 75' in SRC  # no classification step sits between them any more


def test_the_skill_documents_the_unconditional_rule_and_no_declaration_flow():
    assert 'UNCONDITIONAL' in SKILL and 'DEFAULT-DENY' not in SKILL and 'Declare it per launch' not in SKILL


# ---- router-equivalent identity (CTO REVISE ts 1794989163.540309) -------------------------------------------------------
# CLIProxyAPI 8.0.50 strips a supported reasoning suffix and resolves the provider from the base model, so a decorated name routes to the
# restricted upstream. The deny runs on that identity at raw selection and in the generated continuation; permitted models keep their
# original argument.

def _primary_raw(tmp_path, model, resume='', sidecar=None):
    body = (f'TYPE=hd-builder\nMODEL={shlex.quote(model)}\nproxy_agent_primary_pool_guard && rc=0 || rc=$?\n'
            'printf "rc=%s model=[%s]\\n" "$rc" "$MODEL"\n')
    result = _run(tmp_path, body, resume=resume, sidecar=sidecar)
    assert result.returncode == 0, result.stderr
    rc, model_after = re.match(r'rc=(\d+) model=\[(.*)\]$', result.stdout.strip().splitlines()[-1]).groups()
    return rc, model_after, result.stderr


IDENTITY_CASES = ((HY4, HY4), (f'{HY4}(high)', HY4), ('HY4-Flash-Free(High)', HY4), (f' {HY4}(8192) ', HY4), (f'{MUSE}[1m]', MUSE),
                  ('auto(high)', 'auto'), ('AUTO[1m]', 'auto'), ('x(auto)', 'x'),
                  (f'{MUSE}(xhigh)[1m]', MUSE), ('claude-opus-5-5[1m]', 'claude-opus-5-5'), (f'{DEEPSEEK}(high)', DEEPSEEK), (GLM, GLM),
                  (f'x({HY4})', 'x'), ('', ''))


@pytest.mark.parametrize('raw,expected', IDENTITY_CASES)
def test_router_identity_lowercases_and_cuts_at_the_first_suffix_bracket(raw, expected):
    result = subprocess.run(['bash', '-c', f'{HELPERS}\nproxy_agent_router_identity {shlex.quote(raw)}\n'], capture_output=True, text=True,
                            env={'PATH': os.environ.get('PATH', '/usr/bin:/bin')})
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected


@pytest.mark.parametrize('resume', ('', 'run1'))
@pytest.mark.parametrize('model_id', (*DECORATED_FREE, HY4_ALIAS, MUSE_ALIAS, f'{HY4} (high)'))
def test_a_decorated_or_aliased_restricted_primary_is_refused_at_raw_selection_and_resume(tmp_path, model_id, resume):
    rc, _, stderr = _primary_raw(tmp_path, model_id, resume=resume, sidecar=_verified_sidecar(BENIGN) if resume else None)
    assert rc == '75' and 'REFUSED free-model guard primary' in stderr and 'emergency_gate' not in stderr


@pytest.mark.parametrize('model_id', (f'{DEEPSEEK}(high)', f'{GLM}(low)', 'claude-opus-5-5[1m]', 'claude-sonnet-5-5(high)',
                                      'gpt-6.1-terra(high)', 'grok-4.7', 'x(hy4-flash-free)'))
def test_a_permitted_decorated_model_is_not_denied_and_keeps_its_original_argument(tmp_path, model_id):
    rc, model_after, stderr = _primary_raw(tmp_path, model_id)
    assert rc == '0' and 'free-model guard' not in stderr
    assert model_after == model_id  # the guard never rewrites MODEL; the launcher still passes the original string to --model


# ---- unresolved automatic selection (CTO REVISE of c901e33, ts 1794991405.296039) --------------------------------------
# Only the literal `auto` is a tier request. A decorated `auto(high)` is RAW; CLIProxyAPI 8.0.50 derives the base identity before resolving
# `auto` and may choose any registered model (including the two restricted ones), so the raw primary guard refuses it, on a fresh launch,
# a --resume --model, and (keyed on the same identity) the generated continuation. Literal auto keeps the vetted tier path.

@pytest.mark.parametrize('resume', ('', 'run1'))
@pytest.mark.parametrize('model_id', DECORATED_AUTO)
def test_a_decorated_automatic_primary_is_refused_at_raw_selection_and_resume(tmp_path, model_id, resume):
    rc, model_after, stderr = _primary_raw(tmp_path, model_id, resume=resume, sidecar=_verified_sidecar(BENIGN) if resume else None)
    assert rc == '75' and 'REFUSED unresolved automatic selection primary' in stderr and 'identity=auto' in stderr
    assert 'emergency_gate' not in stderr and 'free-model guard' not in stderr
    assert model_after == model_id  # refused, never rewritten


@pytest.mark.parametrize('model_id', (f'{DEEPSEEK}(high)', 'claude-opus-5-5[1m]', 'gpt-6.1-terra(high)', 'grok-4.7', 'x(auto)', 'autoscale-9(high)',
                                      'auto-x', 'xauto(high)'))
def test_decorated_arguments_for_explicitly_permitted_models_are_preserved(tmp_path, model_id):
    rc, model_after, stderr = _primary_raw(tmp_path, model_id)
    assert rc == '0' and 'unresolved automatic selection' not in stderr
    assert model_after == model_id


@pytest.mark.parametrize('tier', ('LIGHT', 'MID', 'PLAN', 'BUILD1', 'REVIEW'))
def test_literal_auto_keeps_the_vetted_tier_path_and_the_picked_model_passes_the_guard(tmp_path, tier):
    rc, model, _ = _pick(tmp_path, 'hd-builder', tier, cooled='')
    assert rc == '0' and model
    guard_rc, _, stderr = _primary_raw(tmp_path, model)
    assert guard_rc == '0' and 'unresolved automatic selection' not in stderr


def test_the_literal_auto_tier_request_is_unchanged_and_the_refusal_runs_on_the_post_pick_primary():
    select = _slice('proxy_agent_select() {', '# Model table: alias')
    assert re.search(r'case "\$requested" in\n    auto\)\n', select)  # still the literal-only tier request
    assert select.index('MODEL="$PICK_MODEL"') < select.index('proxy_agent_primary_pool_guard || return 75')
    assert GUARD.index('proxy_agent_free_guard_refuses "$_guard_id"') < GUARD.index('proxy_agent_is_unresolved_auto "$_guard_id"') \
        < GUARD.index('case "$_guard_id" in\n    gpt-*)')
    assert GUARD.count('proxy_agent_is_unresolved_auto') == 1
    # resume and fresh launch share the one select call, so the refusal covers --resume --model <decorated auto> too
    assert SRC.count('proxy_agent_select || exit 75') == 1 and SRC.index('resume_requires_explicit_model_and_original_cwd') < SRC.index('proxy_agent_select || exit 75')


def test_the_free_guard_precedes_the_exact_opencode_id_early_return_and_launch_keeps_the_original_argument():
    assert GUARD.index('proxy_agent_free_guard_refuses "$_guard_id"') < GUARD.index('proxy_agent_is_opencode_id "$_guard_id" || return 0')
    assert GUARD.count('proxy_agent_free_guard_refuses') == 1  # one deny site on the resolved primary, now ahead of the early return
    assert SRC.count('read -r MODEL_ID WINDOW MAX_OUT < <(resolve_model "$MODEL")') == 1  # --model "$MODEL_ID" is never normalized
