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
        # No active horizon: each account paces to its own weekly reset, (100 - used) / hours-to-reset.
        result, rate, budget = burn.account_decision(key, rows, 'claude', now, {})
        assert result == 'over_budget' and rate == 1 and budget == 68 / 100
        assert burn.account_decision(key, rows, 'codex', now, {})[2] == 68 / 100
        # A horizon before the reset tightens the pace to the horizon, so the same burn is within budget.
        horizons = {'claude': now + 50 * 3600}
        result, rate, budget = burn.account_decision(key, rows, 'claude', now, horizons)
        assert result == 'allow' and budget == 68 / 50
        assert burn.account_decision(key, {key: dict(rows[key], ts=now-7201)}, 'claude', now, {})[0] == 'unknown'
        assert burn.account_decision(key, {key: dict(rows[key], pct=100)}, 'claude', now, {})[0] == 'exhausted'
        assert burn.account_decision('missing', rows, 'claude', now, {})[0] == 'unknown'
        path.write_text(json.dumps(dict(previous, reset=previous['reset'] + 600)) + '\n')
        assert burn.account_decision(key, rows, 'claude', now, {})[0] == 'unknown'
        path.write_text('')
        assert burn.account_decision(key, rows, 'claude', now, {})[0] == 'unknown'


@pytest.mark.parametrize('history,sample_offset,pct,decision', [
    ('old_week', 120, 0, 'allow'),
    ('none', 120, 0, 'allow'),
    ('old_week', -1, 0, 'unknown'),
    ('old_week', 120, 1, 'unknown'),
    ('old_week', 120, False, 'unknown'),
    ('same_week', 4200, 0, 'unknown'),  # an observed drop inside one window is not a reset
])
def test_codex_zero_sample_at_weekly_rollover(tmp_path, monkeypatch, history, sample_offset, pct, decision):
    start = 1_800_000_000
    new_reset = start + 7 * 24 * 3600
    ts = start + sample_offset
    now = max(start, ts)
    key = 'codex:account:weekly'
    log = tmp_path / 'snapshots'
    anchor = {'key': key, 'ts': ts - 4000, 'pct': 1 if history == 'same_week' else 100,
              'reset': new_reset if history == 'same_week' else start}
    log.write_text('' if history == 'none' else json.dumps(anchor) + '\n')
    monkeypatch.setattr(burn, 'LOG', str(log))
    row = {'key': key, 'ts': ts, 'pct': pct, 'reset': new_reset}
    assert burn.account_decision(key, {key: row}, 'codex', now, {})[0] == decision
    assert burn.account_decision(key, {key: row}, 'codex', ts + 7201, {})[0] == 'unknown'
    row['reset'] = start  # even a recent 0% sample from an already expired week is unusable
    assert burn.account_decision(key, {key: row}, 'codex', now, {})[0] == 'unknown'


def test_fresh_codex_rollover_contributes_to_non_p0_sol_gate(tmp_path, monkeypatch, capsys):
    start, now = 1_800_000_000, 1_800_000_120
    log = tmp_path / 'snapshots'
    rows = [
        {'key': 'codex:recent:weekly', 'ts': now - 7200, 'pct': 100, 'reset': start},
        {'key': 'codex:recent:weekly', 'ts': now, 'pct': 0, 'reset': start + 7 * 24 * 3600},
        {'key': 'codex:busy:weekly', 'ts': now - 7200, 'pct': 85, 'reset': now + 28 * 3600},
        {'key': 'codex:busy:weekly', 'ts': now, 'pct': 87, 'reset': now + 28 * 3600},
    ]
    log.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    for account in ('recent', 'busy'):
        (tmp_path / f'{account}.json').write_text(json.dumps({'type': 'codex', 'account_id': account}))
    monkeypatch.setattr(burn, 'LOG', str(log))
    monkeypatch.setattr(burn, 'read_config', lambda _: {'auth-dir': str(tmp_path)})
    monkeypatch.setattr(burn, 'load_horizons', lambda: {'codex': now + 96 * 3600})
    assert burn.shadow('gpt-6.2-sol', 'hd-builder', '', 'unused', str(log), now) == 'allow'
    assert 'usable_accounts=2/2' in capsys.readouterr().out
    log.write_text(''.join(json.dumps(row) + '\n' for row in rows if row is not rows[1]))
    assert burn.shadow('gpt-6.2-sol', 'hd-builder', '', 'unused', str(log), now) == 'would_refuse'


def test_load_horizons_is_the_one_config(tmp_path, monkeypatch):
    cfg = tmp_path / 'burn-horizons.json'
    cfg.write_text(json.dumps({'_comment': 'ignored', 'claude': '2026-11-18T04:00:00Z',
                               'codex': '2026-11-20T13:06:00+05:30', 'grok': 'not-a-date', 'bad': 5,
                               'naive': '2026-11-20T13:06:00'}))
    got = burn.load_horizons(str(cfg))
    assert got == {'claude': 1794974400.0, 'codex': 1795160160.0}
    monkeypatch.setenv('PROXY_BURN_HORIZONS', str(cfg))
    assert burn.load_horizons() == got
    cfg.write_text('not json')
    assert burn.load_horizons(str(cfg)) == {}
    assert burn.load_horizons(str(tmp_path / 'missing.json')) == {}


def test_horizon_passed_falls_back_to_own_reset():
    now = 2000
    assert burn.active_horizon('claude', {'claude': now - 1}, now) is None
    assert burn.active_horizon('claude', {'claude': now + 1}, now) == now + 1
    assert burn.active_horizon('claude', {}, now) is None
    reset = now + 10 * 3600
    assert burn.pace_budget(50, reset, now, None) == (5.0, None, 'own')


def test_pace_budget_two_phase_when_reset_precedes_horizon():
    now = 0
    # Resets in 10h, horizon in 40h: budget now = left / 10h; after the reset the full 100% spreads over 30h.
    budget, after, target = burn.pace_budget(60, 10 * 3600, now, 40 * 3600)
    assert target == 'reset' and budget == 4.0 and after == pytest.approx(100 / 30)
    # A reset within the slack of the horizon IS the horizon.
    budget, after, target = burn.pace_budget(60, 40 * 3600 - 600, now, 40 * 3600)
    assert target == 'horizon' and after is None and budget == pytest.approx(40 / 40)
    # Exhausted accounts keep the after-reset budget so the report can show it.
    assert burn.pace_budget(100, 10 * 3600, now, 40 * 3600)[0] == 0.0


def _rows(now, spec, session=True):
    """spec: acct -> (pct, reset_in_hours); every row is a fresh sample.

    Claude capacity is usable only with a fresh session sample below 100% (CTO REVISE of 35df934), so each account
    also gets an open 5h-session sample; session=False omits them. The capped/stale/unknown cases live in
    test_burn_usable_capacity.py."""
    rows = {f'claude:{a}:weekly': {'key': f'claude:{a}:weekly', 'ts': now, 'pct': pct, 'reset': now + h * 3600}
            for a, (pct, h) in spec.items()}
    if session:
        rows.update({f'claude:{a}:session': {'key': f'claude:{a}:session', 'ts': now, 'pct': 20, 'reset': now + 3 * 3600}
                     for a in spec})
    return rows


def test_pool_status_headroom_offsets_overspend_and_exhausted_adds_nothing():
    now = 1000000
    rows = _rows(now, {'a': (20, 100), 'b': (50, 100), 'dead': (100, 100)})
    burns = {'claude:a:weekly': 0.2, 'claude:b:weekly': 0.3, 'claude:dead:weekly': 9.0}
    with mock.patch.object(burn, 'burn_for', side_effect=lambda key, pct, reset: burns[key]):
        status = burn.pool_status('claude', ['a', 'b', 'dead'], rows, now, {})
    by = dict(status['accounts'])
    assert by['dead']['decision'] == 'exhausted' and by['dead']['headroom'] is None
    # a is far under its own budget (0.8) and b is over (0.5): the pool nets out within budget.
    assert by['a']['headroom'] == pytest.approx(0.8 - 0.2) and by['b']['headroom'] == pytest.approx(0.5 - 0.3)
    assert status['burn'] == pytest.approx(0.5) and status['budget'] == pytest.approx(1.3)
    assert status['decision'] == 'allow' and status['headroom'] == pytest.approx(0.8)


def test_pool_status_over_budget_wins_over_unknown_and_unknown_blocks_otherwise():
    now = 1000000
    rows = _rows(now, {'a': (80, 10), 'b': (10, 100)})
    del rows['claude:b:weekly']  # unmeasured account
    with mock.patch.object(burn, 'burn_for', return_value=5.0):  # a: budget 2.0, burn 5.0
        assert burn.pool_status('claude', ['a', 'b'], rows, now, {})['decision'] == 'over_budget'
    with mock.patch.object(burn, 'burn_for', return_value=0.1):
        assert burn.pool_status('claude', ['a', 'b'], rows, now, {})['decision'] == 'unknown'
        assert burn.pool_status('claude', ['a'], rows, now, {})['decision'] == 'allow'
    rows_all_dead = _rows(now, {'a': (100, 10)})
    assert burn.pool_status('claude', ['a'], rows_all_dead, now, {})['decision'] == 'exhausted'


def test_pool_status_capped_session_is_not_usable_budget():
    now = 1000000
    rows = _rows(now, {'a': (20, 100), 'b': (99, 100)})
    rows['claude:a:session'] = {'key': 'claude:a:session', 'ts': now, 'pct': 100, 'reset': now + 4 * 3600}
    burns = {'claude:a:weekly': 0.0, 'claude:b:weekly': 0.5}
    horizons = {'claude': now + 10 * 3600}
    with mock.patch.object(burn, 'burn_for', side_effect=lambda key, pct, reset: burns[key]):
        status = burn.pool_status('claude', ['a', 'b'], rows, now, horizons)
    # a has 8.0 %/h of weekly headroom but is session-capped; only b's 0.1 %/h is usable, so 0.5 %/h is over budget.
    assert status['weekly_budget'] == pytest.approx(8.1) and status['budget'] == pytest.approx(0.1)
    assert status['decision'] == 'over_budget'
    assert dict(status['accounts'])['a']['session'] == 'capped' and not dict(status['accounts'])['a']['usable']


def test_pool_status_pre_horizon_reset_reports_after_budget():
    now = 1000000
    rows = _rows(now, {'qa': (100, 8.7)})
    status = burn.pool_status('claude', ['qa'], rows, now, {'claude': now + 33.8 * 3600})
    detail = dict(status['accounts'])['qa']
    assert detail['decision'] == 'exhausted' and detail['budget'] == 0.0
    assert detail['target'] == 'reset' and detail['after'] == pytest.approx(100 / (33.8 - 8.7))


def test_bindings_require_enabled_known_accounts(tmp_path):
    config = {'auth-dir': str(tmp_path)}
    (tmp_path / 'a.json').write_text(json.dumps({'type': 'claude', 'email': 'one', 'disabled': False}))
    (tmp_path / 'b.json').write_text(json.dumps({'type': 'claude', 'email': 'two', 'disabled': True}))
    assert burn.bindings('claude-haiku-5-5', config) == ('claude', ['one'])
    (tmp_path / 'c.json').write_text(json.dumps({'type': 'claude', 'disabled': False}))
    with pytest.raises(ValueError):
        burn.bindings('claude-haiku-5-5', config)


def test_shadow_pool_decision_and_exempt_override():
    config = {'auth-dir': '/not-used'}
    rows = {'codex:a:weekly': {'ts': 10000, 'pct': 10, 'reset': 50000},
            'codex:b:weekly': {'ts': 10000, 'pct': 100, 'reset': 50000}}
    with mock.patch.object(burn, 'read_config', return_value=config), \
         mock.patch.object(burn, 'bindings', return_value=('codex', ['a', 'b'])), \
         mock.patch.object(burn, 'read_latest', return_value=rows), \
         mock.patch.object(burn, 'load_horizons', return_value={}), \
         mock.patch.object(burn, 'burn_for', return_value=0):
        # One account is exhausted, but a is under budget, so the pool allows launches (free headroom is used).
        for role, override, effective in [('hd-builder', '', 'allow'),
                                           ('ci-repair', '', 'exempt'),
                                           ('hd-builder', 'P0:123', 'override')]:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                assert burn.shadow('gpt-6-luna', role, override, 'unused', 'unused', 10000) == effective
            text = out.getvalue()
            assert 'decision=exhausted' in text  # per-account line for b
            assert 'pool_decision=allow' in text and 'horizon=own-reset' in text
            assert 'effective=' + effective in text


def test_shadow_refuses_when_pool_burn_exceeds_pool_budget():
    config = {'auth-dir': '/not-used'}
    rows = {'codex:a:weekly': {'ts': 10000, 'pct': 90, 'reset': 50000}}
    with mock.patch.object(burn, 'read_config', return_value=config), \
         mock.patch.object(burn, 'bindings', return_value=('codex', ['a'])), \
         mock.patch.object(burn, 'read_latest', return_value=rows), \
         mock.patch.object(burn, 'load_horizons', return_value={}), \
         mock.patch.object(burn, 'burn_for', return_value=5.0):
        for role, override, effective in [('hd-builder', '', 'would_refuse'),
                                           ('ci-repair', '', 'exempt'),
                                           ('hd-builder', 'rajiv:1794.5', 'override')]:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                assert burn.shadow('gpt-6-luna', role, override, 'unused', 'unused', 10000) == effective
            assert 'pool_decision=over_budget' in out.getvalue()


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
