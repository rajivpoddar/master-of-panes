"""Contract tests for shadow provider burn admission (not run on the host)."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
from unittest import mock

import pytest

SCRIPT = Path(__file__).parents[1] / 'shared-assets/claude/scripts/proxy-burn-shadow.py'
spec = importlib.util.spec_from_file_location('proxy_burn_shadow', SCRIPT)
burn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(burn)


def test_collector_formula_parity():
    collector = Path.home() / '.claude/scripts/usage-snapshot.py'
    if not collector.is_file():
        pytest.skip('live collector unavailable; parity requires the collector')
    original = collector.read_text()
    copy = SCRIPT.read_text()
    original_prev = original[original.index('def prev(key):'):original.index('\nrows = readings()')]
    copied_prev = copy[copy.index('def prev(key):'):copy.index('\n\n\ndef burn_for')]
    assert original_prev.strip() == copied_prev.strip()
    for line in ('    p = prev(key)', '    burn = eta = None',
                 '    if p and reset - p["reset"] < 60 and pct >= p["pct"]:  # same window, no reset between',
                 '        burn = (pct - p["pct"]) / ((NOW - p["ts"]) / 3600)'):
        assert line in original and line in copy


def test_budget_and_unknown(tmp_path):
    path = tmp_path / 'snapshots'
    key = 'claude:acct:weekly'
    now = 1000000
    previous = {'key': key, 'ts': now - 7200, 'reset': now + 100 * 3600, 'pct': 30}
    path.write_text(json.dumps(previous) + '\n')
    rows = {key: dict(previous, ts=now, pct=32)}
    with mock.patch.object(burn, 'LOG', str(path)):
        result, rate, budget = burn.account_decision(key, rows, 'claude', now)
        assert result == 'over_budget' and rate == 1 and budget == 68 / 84
        assert burn.account_decision(key, rows, 'codex', now)[2] == 68 / 100
        assert burn.account_decision(key, {key: dict(rows[key], ts=now-7201)}, 'claude', now)[0] == 'unknown'
        assert burn.account_decision(key, {key: dict(rows[key], pct=100)}, 'claude', now)[0] == 'exhausted'
        assert burn.account_decision('missing', rows, 'claude', now)[0] == 'unknown'
        path.write_text(json.dumps(dict(previous, reset=previous['reset'] + 600)) + '\n')
        assert burn.account_decision(key, rows, 'claude', now)[0] == 'unknown'
        path.write_text('')
        assert burn.account_decision(key, rows, 'claude', now)[0] == 'unknown'


def test_bindings_require_enabled_known_accounts(tmp_path):
    config = {'auth-dir': str(tmp_path)}
    (tmp_path / 'a.json').write_text(json.dumps({'type': 'claude', 'email': 'one', 'disabled': False}))
    (tmp_path / 'b.json').write_text(json.dumps({'type': 'claude', 'email': 'two', 'disabled': True}))
    assert burn.bindings('claude-haiku-5-5', config) == ('claude', ['one'])
    (tmp_path / 'c.json').write_text(json.dumps({'type': 'claude', 'disabled': False}))
    with pytest.raises(ValueError):
        burn.bindings('claude-haiku-5-5', config)


def test_shadow_per_account_and_exempt_override():
    config = {'auth-dir': '/not-used'}
    rows = {'codex:a:weekly': {'ts': 10000, 'pct': 10, 'reset': 50000},
            'codex:b:weekly': {'ts': 10000, 'pct': 100, 'reset': 50000}}
    with mock.patch.object(burn, 'read_config', return_value=config), \
         mock.patch.object(burn, 'bindings', return_value=('codex', ['a', 'b'])), \
         mock.patch.object(burn, 'read_latest', return_value=rows), \
         mock.patch.object(burn, 'burn_for', return_value=0):
        for role, override, effective in [('hd-builder', '', 'would_refuse'),
                                           ('ci-repair', '', 'exempt'),
                                           ('hd-builder', 'P0:123', 'override')]:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                burn.shadow('gpt-6-luna', role, override, 'unused', 'unused', 10000)
            assert 'decision=exhausted' in out.getvalue()
            assert 'effective=' + effective in out.getvalue()


def test_unmapped_and_grok_unknown():
    with mock.patch.object(burn, 'read_config', side_effect=ValueError()):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            burn.shadow('grok-4.7', 'hd-builder', '', 'unused', 'unused', 10000)
        assert 'pool=grok' in out.getvalue()
        assert 'decision=would_refuse' in out.getvalue()


@pytest.mark.parametrize('model', ['claude-haiku-5-5', 'gpt-6-luna', 'grok-4.7'])
def test_unknown_decisions_block_capped_roles_but_not_exemptions_or_override(model):
    with mock.patch.object(burn, 'read_config', side_effect=ValueError()):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            assert burn.shadow(model, 'hd-builder', '', '', '', 10000) == 'would_refuse'
            assert burn.shadow(model, 'ci-repair', '', '', '', 10000) == 'exempt'
            assert burn.shadow(model, 'hd-builder', 'P0:123', '', '', 10000) == 'override'
        assert 'override=P0:123' in out.getvalue()


def test_tier_picker_skips_burn_refusal_and_fails_closed():
    launcher = (SCRIPT.parent / 'proxy-agent.sh').read_text()
    picker = launcher[launcher.index('proxy_agent_pick_tier() {'):launcher.index('# hd-* split compat')]
    # Burn refusal is checked BEFORE the usability probe, so no provider call is made for a refused id.
    refused = picker.index('if proxy_agent_burn_refused "$id"; then')
    assert refused < picker.index('if proxy_agent_sub_usable "$id"; then')
    assert 'id:burn_refused' in picker[refused:]
    assert 'continue' in picker[refused:refused + 200]
    # Capped types fail closed when no tier model survives.
    assert 'if proxy_agent_burn_enforced; then' in picker
    assert 'return 75' in picker
    # Primary and every nested alias (Haiku/Sonnet/Opus) go through the same picker.
    assert 'PICK_ENFORCE_BURN' not in launcher
    assert 'proxy_agent_pick_tier "$tier" || return 75' in launcher
    assert 'proxy_agent_pick_tier LIGHT || true; HAIKU_MODEL="$PICK_MODEL"' in launcher
    assert 'proxy_agent_pick_tier MID || true; SONNET_MODEL="$PICK_MODEL"' in launcher
    assert 'proxy_agent_pick_tier TOP || true; OPUS_MODEL="$PICK_MODEL"' in launcher
    # An empty refused alias exports a non-model sentinel, never an unchecked fallback.
    assert 'ANTHROPIC_DEFAULT_HAIKU_MODEL="${HAIKU_MODEL:-burn-refused-haiku}"' in launcher
    assert 'PROXY_BURN_GATE="${PROXY_BURN_GATE:-enforce}"' in launcher


@pytest.mark.parametrize('field', ['pct', 'reset'])
def test_bool_or_non_number_meter_is_unknown(field):
    # float(False) == 0.0 would otherwise turn a bad reading into an allow.
    row = {'ts': 9000, 'pct': 10, 'reset': 50000}
    for bad in (False, True, '10', None):
        row[field] = bad
        assert burn.account_decision('codex:a:weekly', {'codex:a:weekly': row}, 'codex', 10000)[0] == 'unknown'
