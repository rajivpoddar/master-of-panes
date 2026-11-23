"""Claude pool cap is ENFORCED (PM add-on 2026-11-23; Rajiv C0ALZJHGE49 thread 1795437841.011459, ts 1795439210.427769 "reduce the calude proxy cap":
5 Claude lanes ran against cap 2 because the cap was telemetry-only).

  picker           : a claude-* id at/over PROXY_AGENT_CLAUDE_CAP is skipped (id:claude_cap) and the next model in the tier is used.
  explicit --model : a claude-* model at cap refuses rc 75 (PROXY_AGENT_REFUSED reason=claude_cap); the launch state machine then records
                     queued -> refused and a proxy_relaunch CAPACITY_WAIT row with the launch args.
  exempt types     : only proxy_agent_is_capped roles count. 12b (pm-admission-reviewer) and every other exempt type still launches at cap.
  total_cap        : unchanged telemetry only.
Both layers drive the REAL proxy-agent.sh beside this file (picker: functions extracted by name; launcher: a real run with a fake claude, as
test_proxy_lane_launch_states.py does). proxy-cap-count.py is also checked for the non-UTF-8 argv decode.
Run: /opt/homebrew/bin/pytest -q -p no:cacheprovider tests/test_proxy_claude_cap.py
"""
import ast
import importlib.util
import os
import re
import subprocess
import tempfile
import types
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1]
HERE = Path(__file__).parent


def _load(name, path, **env):
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# The picker harness of the tiers test, pointed at THIS script (it defaults to the deployed one), with a settable live Claude count and cap.
tiers = _load('tiers_for_claude_cap', HERE / 'test_proxy_agent_tiers.py', PROXY_AGENT_TEST_SCRIPT=str(SCRIPTS / 'proxy-agent.sh'))
launch_states = _load('launch_states_for_claude_cap', HERE / 'test_proxy_lane_launch_states.py')
lab = launch_states.lab  # fixture: temp HOME + fake claude + recording pm-ops stub + real tmux
rows = launch_states.rows

PRELUDE = (tiers.PRELUDE.replace('proxy_agent_live_count() { printf 0; }', 'proxy_agent_live_count() { printf "%s" "${CLAUDE_LIVE:-0}"; }')
           .replace('PROXY_AGENT_CLAUDE_CAP=8', 'PROXY_AGENT_CLAUDE_CAP="${CLAUDE_CAP:-8}"'))
assert 'CLAUDE_LIVE' in PRELUDE and 'CLAUDE_CAP:-8' in PRELUDE, 'the tiers harness stubs changed: update this replacement'


def pick(role, unavailable, live, cap=2):
    """proxy_agent_resolve_type + proxy_agent_select with Claude at <live> of <cap>."""
    with tempfile.TemporaryDirectory() as tmp:
        brief = Path(tmp) / 'brief.txt'
        brief.write_text('plan it\n')
        env = tiers.base_env(tmp, REQUESTED='auto', ROLE=role, BRIEF=str(brief), RUNG='', UNAVAILABLE=' '.join(unavailable), PROBEFAIL='',
                             CLAUDE_LIVE=live, CLAUDE_CAP=cap)
        done = subprocess.run(['bash', '-c', PRELUDE + '\n' + tiers.ROUTE_BODY], env=env, text=True, capture_output=True, check=True)
    f = done.stdout.strip().split('|')
    return types.SimpleNamespace(model=f[2], reason=f[3], rc=f[5], err=done.stderr)


# MID: ... claude-sonnet-5-5 -> gpt-6.1-terra. Everything before claude-sonnet-5-5 is unavailable, so the pick lands on it unless the cap skips it.
BEFORE_SONNET = tiers.MID[:tiers.MID.index('claude-sonnet-5-5')]
# TOP: grok-4.7 -> sarvam-kimi-k3 -> claude-opus-5-5 -> claude-fable-5-2 -> gpt-6.2-sol.
BEFORE_OPUS = tiers.TOP[:tiers.TOP.index('claude-opus-5-5')]


def test_a_capped_role_skips_a_claude_id_at_the_cap_and_takes_the_next_model():
    full = pick('hd-builder', BEFORE_SONNET, live=2, cap=2)
    assert full.model == 'gpt-6.1-terra' and full.rc == '0'
    assert 'claude_cap skip model=claude-sonnet-5-5 (2 live >= 2)' in full.err and 'claude-sonnet-5-5:claude_cap' in full.reason


def test_over_the_cap_is_skipped_too_and_under_the_cap_is_not():
    assert pick('hd-builder', BEFORE_SONNET, live=5, cap=2).model == 'gpt-6.1-terra'
    free = pick('hd-builder', BEFORE_SONNET, live=1, cap=2)
    assert free.model == 'claude-sonnet-5-5' and 'claude_cap' not in free.err + free.reason


def test_every_claude_id_of_a_top_tier_is_skipped_at_the_cap():
    got = pick('hd-planner', BEFORE_OPUS, live=3, cap=3)
    assert got.model == 'gpt-6.2-sol'  # opus and fable both skipped
    assert 'claude-opus-5-5:claude_cap' in got.reason and 'claude-fable-5-2:claude_cap' in got.reason


@pytest.mark.parametrize('role', ['pm-admission-reviewer', 'codex-code-reviewer', 'pm-plan-reviewer'])
def test_an_exempt_role_still_gets_claude_at_the_cap(role):
    got = pick(role, BEFORE_OPUS, live=9, cap=2)
    assert got.model == 'claude-opus-5-5' and 'claude_cap' not in got.err + got.reason


def test_the_capped_set_is_the_launchers_own_predicate_and_excludes_12b():
    """Read from the launcher's proxy_agent_is_capped, not hand-listed here."""
    src = (SCRIPTS / 'proxy-agent.sh').read_text()
    body = src[src.index('proxy_agent_is_capped() {'):]
    body = body[:body.index('\n}')]
    capped = set(re.search(r'case "\$1" in ([^)]*)\)', body).group(1).strip().split('|'))
    assert 'pm-admission-reviewer' not in capped and 'hd-builder' in capped and 'hd-planner' in capped


# ---- the launcher itself ------------------------------------------------------------------------------------------------------------

COUNT_STUB = 'import os, sys\nprint(os.environ.get("PROXY_TEST_LIVE_" + sys.argv[1].upper(), "0"))\n'


def REFUSED_FOR_CAP(stderr):
    """The enforcement messages only: the burn shadow line also prints effective=claude_cap_only, which is telemetry, not a refusal."""
    return 'PROXY_AGENT_REFUSED reason=claude_cap' in stderr or 'claude_cap skip model=' in stderr


def prepare(lab, role):
    _tmp, home, _env = lab
    (home / '.claude/scripts/proxy-cap-count.py').write_text(COUNT_STUB)
    (home / '.claude/agents' / f'{role}.md').write_text('---\ntools: Bash\n---\nTest worker.\n')


def run(lab, role, name, model, live, cap=2, brief='brief.md', **env):
    """A real launch with <live> Claude lanes counted against <cap>. PIN_STRICT keeps the explicit --model (no tier walk, no provider probes)."""
    tmp, _home, base = lab
    return subprocess.run(['bash', str(SCRIPTS / 'proxy-agent.sh'), role, str(tmp / brief), '--out', str(tmp / f'{name}.md'), '--model', model],
                          env={**base, 'PROXY_AGENT_NAME': name, 'PROXY_TEST_LIVE_CLAUDE': str(live), 'PROXY_TEST_LIVE_TOTAL': '0', 'PROXY_AGENT_CLAUDE_CAP': str(cap),
                               'PROXY_AGENT_PIN_STRICT': '1', 'PROXY_TEST_SLEEP': '0.3', **env}, capture_output=True, text=True, timeout=240)


def test_an_explicit_claude_model_at_the_cap_is_refused_75_and_queued_with_its_launch_args(lab):
    tmp, home, _ = lab
    prepare(lab, 'hd-builder')
    done = run(lab, 'hd-builder', 'proxy-test-claude-cap', 'claude-sonnet-5-5', live=2)
    assert done.returncode == 75 and 'PROXY_AGENT_REFUSED reason=claude_cap model=claude-sonnet-5-5 (2 live >= 2)' in done.stderr, done.stderr
    assert not (home / 'prompt-seen.txt').exists()  # no lane started
    assert [r['evidence'].get('state') for r in rows(tmp, 'proxy_run')] == ['queued', 'refused']
    (relaunch,) = rows(tmp, 'proxy_relaunch')
    spec = relaunch['evidence']['relaunch']
    assert relaunch['--blocker'].startswith('CAPACITY_WAIT:') and relaunch['--target-id'] == 'proxy-test-claude-cap'
    assert spec['type'] == 'hd-builder' and spec['model_args'] == ['--model', 'claude-sonnet-5-5'] and '--model claude-sonnet-5-5' in spec['cmd']


def test_an_explicit_claude_model_under_the_cap_launches(lab):
    tmp, home, _ = lab
    prepare(lab, 'hd-builder')
    done = run(lab, 'hd-builder', 'proxy-test-claude-room', 'claude-sonnet-5-5', live=1)
    assert done.returncode == 0 and not REFUSED_FOR_CAP(done.stderr), done.stderr
    assert (home / 'prompt-seen.txt').exists() and rows(tmp, 'proxy_relaunch') == []


def test_an_exempt_type_at_the_cap_still_launches(lab):
    """The lab's own type (test) is not a proxy_agent_is_capped role, so it launches with 9 live Claude lanes against cap 2. The 12b type itself
    (pm-admission-reviewer) is covered by the picker test above and by the predicate test: its real launch also consults GitHub review-cap history
    (rc 76 REVIEW_CAP_UNKNOWN without it), which the hermetic lab has none of."""
    tmp, home, _ = lab
    prepare(lab, 'test')
    done = run(lab, 'test', 'proxy-test-exempt-at-cap', 'claude-sonnet-5-5', live=9)
    assert done.returncode == 0 and not REFUSED_FOR_CAP(done.stderr), done.stderr
    assert (home / 'prompt-seen.txt').exists() and rows(tmp, 'proxy_relaunch') == []


def test_the_total_cap_stays_telemetry_only(lab):
    _tmp, home, _ = lab
    prepare(lab, 'hd-builder')
    done = run(lab, 'hd-builder', 'proxy-test-total-cap', 'claude-sonnet-5-5', live=0, PROXY_TEST_LIVE_TOTAL='99', PROXY_AGENT_TOTAL_CAP='2')
    assert done.returncode == 0 and 'telemetry total_cap (99 live >= 2)' in done.stderr, done.stderr
    assert (home / 'prompt-seen.txt').exists()


# ---- proxy-cap-count.py: a non-UTF-8 argv must not make the caps unreadable -------------------------------------------------------------

def test_cap_count_decodes_ps_output_with_errors_replace():
    tree = ast.parse((SCRIPTS / 'proxy-cap-count.py').read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, 'attr', '') == 'check_output']
    assert calls and all(any(k.arg == 'errors' and getattr(k.value, 'value', None) == 'replace' for k in c.keywords) for c in calls)


def test_cap_count_survives_invalid_utf8_in_the_process_table(tmp_path):
    """Run the real script with /bin/ps replaced (a stub earlier in the module's subprocess call) by a process table holding bytes \\xff\\xfe."""
    fake = tmp_path / 'ps'
    fake.write_bytes(b'#!/bin/sh\nprintf "/x/claude -p --name n1 --model claude-opus-5-5 \\377\\376 brief\\n"\n')
    fake.chmod(0o755)
    src = (SCRIPTS / 'proxy-cap-count.py').read_text().replace("'/bin/ps'", repr(str(fake)))
    assert str(fake) in src
    patched = tmp_path / 'proxy-cap-count.py'
    patched.write_text(src)
    done = subprocess.run(['python3', '-I', str(patched), 'claude'], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0 and 'cap metadata unavailable' not in done.stderr and done.stdout.strip().isdigit(), done.stderr