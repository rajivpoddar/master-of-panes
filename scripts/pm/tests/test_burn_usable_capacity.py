"""Fixed-time decision table: usable capacity, not theoretical weekly headroom, decides admission and the dial.

CTO REVISE of 35df934 (C0ALZJHGE49 thread 1794851199.275299, ts 1794854782.620999): the pool gate and the dial
counted session-capped Claude capacity as usable now ("Capped or unknown session capacity must not subsidize ordinary
admission or a dial increase"; focused proof = "one fixed-time decision table/fixture through the shared calculation
and gate/report/dial for capped, freshly cap-cleared and stale/unknown session observations").

One scenario table drives every consumer at the same fixed instant:
  shared calculation  proxy-burn-shadow.py pool_status()
  launcher gate       proxy-burn-shadow.py shadow() (exemptions and P0/rajiv overrides keep working)
  hourly report       heartbeat-compose.py burn_status() (the theoretical weekly budget stays visible)
  burn dial           ~/.claude/scripts/cost/proxy-burn-dial.py (deployed-only; those cases skip if it is absent)
Not executed by the MoP repair run (Rajiv, DM D0AMF0XE6TS thread 1793979986.975849, ts 1793983042.752819: "MoP
changes do not require any local testing or proof. they are not app code. MoP is separate repo").
"""
import contextlib
import importlib.util
import io
import json
import types
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

ASSETS = Path(__file__).parents[1] / 'shared-assets/claude/scripts'
DIAL = Path.home() / '.claude/scripts/cost/proxy-burn-dial.py'


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


burn = _load('proxy_burn_shadow_usable', ASSETS / 'proxy-burn-shadow.py')
composer = _load('heartbeat_compose_usable', ASSETS / 'pm/heartbeat/heartbeat-compose.py')

H = 3600
NOW = 1_800_000_000.0                      # fixed instant: nothing in this table reads the wall clock
HORIZON = NOW + 10 * H                     # the CTO counterexample's 10-hour horizon
HORIZONS = {'claude': HORIZON}             # Codex has no horizon here: it paces to its own weekly reset (100h)
WEEKLY_RESET = NOW + 100 * H               # after the horizon, so every Claude budget is (100 - used) / 10h


def _session(acct, ts, pct, reset):
    return {'key': f'claude:{acct}:session', 'ts': ts, 'pct': pct, 'reset': reset}


# Session observations a Claude account can be in at NOW. 'cleared' is two samples: the old capped one and the
# fresh one after the cap lifted (read_latest keeps the newest per key).
SESSIONS = {
    'open': lambda a: [_session(a, NOW - 60, 20, NOW + 3 * H)],
    'capped': lambda a: [_session(a, NOW - 60, 100, NOW + 4 * H)],
    'cleared': lambda a: [_session(a, NOW - 4000, 100, NOW - 1000), _session(a, NOW - 60, 3, NOW + 5 * H)],
    'expired-cap': lambda a: [_session(a, NOW - 3000, 100, NOW - 60)],   # cap lifted by the clock, no fresh sample yet
    'stale': lambda a: [_session(a, NOW - 3 * H, 20, NOW + 3 * H)],     # older than the 2h freshness bound
    'missing': lambda a: [],
    None: lambda a: [],                                                    # Codex has no session meter
}

# accts: account -> (weekly used %, weekly burn %/h, session observation). Budgets are (100 - used) / 10h on Claude.
# decision/burn/budget/weekly describe the POOL; budget is the usable budget, weekly the theoretical one.
# unusable: accounts with a measured weekly window that cannot take traffic now. gate: ordinary launch for a
# capped role. vote: the dial's pool vote.
SCENARIOS = [
    dict(id='capped-account-does-not-subsidize',      # the CTO's static counterexample
         accts={'a': (20, 0.0, 'capped'), 'b': (99, 0.5, 'open')},
         decision='over_budget', burn=0.5, budget=0.1, weekly=8.1, usable=1, live=2, unusable={'a'},
         gate='would_refuse', vote='DOWN'),
    dict(id='freshly-cap-cleared-counts-again',
         accts={'a': (20, 0.0, 'cleared'), 'b': (99, 0.5, 'open')},
         decision='allow', burn=0.5, budget=8.1, weekly=8.1, usable=2, live=2, unusable=set(),
         gate='allow', vote='UP'),
    dict(id='stale-session-sample-does-not-subsidize',
         accts={'a': (20, 0.0, 'stale'), 'b': (99, 0.5, 'open')},
         decision='over_budget', burn=0.5, budget=0.1, weekly=8.1, usable=1, live=2, unusable={'a'},
         gate='would_refuse', vote='DOWN'),
    dict(id='unknown-session-does-not-subsidize',
         accts={'a': (20, 0.0, 'missing'), 'b': (99, 0.5, 'open')},
         decision='over_budget', burn=0.5, budget=0.1, weekly=8.1, usable=1, live=2, unusable={'a'},
         gate='would_refuse', vote='DOWN'),
    dict(id='cap-lifted-by-the-clock-waits-for-a-fresh-sample',
         accts={'a': (20, 0.0, 'expired-cap'), 'b': (99, 0.5, 'open')},
         decision='over_budget', burn=0.5, budget=0.1, weekly=8.1, usable=1, live=2, unusable={'a'},
         gate='would_refuse', vote='DOWN'),
    dict(id='every-measured-account-capped-nothing-usable',
         accts={'a': (20, 0.0, 'capped'), 'b': (50, 0.0, 'capped')},
         decision='unavailable', burn=0.0, budget=0.0, weekly=13.0, usable=0, live=2, unusable={'a', 'b'},
         gate='would_refuse', vote='HOLD'),            # HOLD, never UP: there is nothing to dial into
    dict(id='all-capped-with-demand-is-still-unavailable',
         accts={'a': (20, 1.0, 'capped')},
         decision='unavailable', burn=1.0, budget=0.0, weekly=8.0, usable=0, live=1, unusable={'a'},
         gate='would_refuse', vote='HOLD'),
    dict(id='open-account-within-its-budget',
         accts={'a': (20, 0.3, 'open')},
         decision='allow', burn=0.3, budget=8.0, weekly=8.0, usable=1, live=1, unusable=set(),
         gate='allow', vote='UP'),
    dict(id='capped-accounts-demand-lands-on-the-usable-one',
         accts={'a': (20, 2.0, 'capped'), 'b': (90, 0.1, 'open')},
         decision='over_budget', burn=2.1, budget=1.0, weekly=9.0, usable=1, live=2, unusable={'a'},
         gate='would_refuse', vote='DOWN'),
    dict(id='weekly-exhausted-account-adds-nothing-even-with-an-open-session',
         accts={'a': (100, 0.0, 'open'), 'b': (50, 0.2, 'open')},
         decision='allow', burn=0.2, budget=5.0, weekly=5.0, usable=1, live=1, unusable=set(),
         gate='allow', vote='UP'),
    dict(id='codex-has-no-session-meter-so-weekly-alone-decides',
         pool='codex', accts={'a': (20, 0.3, None)},
         decision='allow', burn=0.3, budget=0.8, weekly=0.8, usable=1, live=1, unusable=set(),
         gate='allow', vote='UP'),                     # Codex: (100 - 20) / 100h own reset, no horizon
]
IDS = [s['id'] for s in SCENARIOS]
MODEL = {'claude': 'claude-opus-5-5', 'codex': 'gpt-6.1-terra'}


def _differs(scn):
    """True when the usable budget is below the theoretical weekly one (the report then shows both)."""
    return abs(scn['weekly'] - scn['budget']) > 1e-9


def _build(scn, tmp_path):
    """(pool, accounts, rows, burns) for a scenario; the OTHER pool gets one healthy account so every consumer runs."""
    pool = scn.get('pool', 'claude')
    other = 'codex' if pool == 'claude' else 'claude'
    records, burns = [], {}
    for acct, (used, rate, session) in scn['accts'].items():
        records.append({'key': f'{pool}:{acct}:weekly', 'ts': NOW - 60, 'pct': used, 'reset': WEEKLY_RESET})
        records += SESSIONS[session](acct)
        burns[f'{pool}:{acct}:weekly'] = rate
    records.append({'key': f'{other}:ok:weekly', 'ts': NOW - 60, 'pct': 10, 'reset': WEEKLY_RESET})
    if other == 'claude':
        records += SESSIONS['open']('ok')
    path = tmp_path / 'usage-snapshots.jsonl'
    path.write_text(''.join(json.dumps(r) + '\n' for r in records))
    return pool, list(scn['accts']), burn.read_latest(str(path)), burns


def _burn_for(burns):
    return mock.patch.object(burn, 'burn_for', side_effect=lambda key, pct, reset: burns.get(key, 0.0))


@pytest.mark.parametrize('scn', SCENARIOS, ids=IDS)
def test_shared_calculation(scn, tmp_path):
    pool, accounts, rows, burns = _build(scn, tmp_path)
    with _burn_for(burns):
        status = burn.pool_status(pool, accounts, rows, NOW, HORIZONS)
    assert status['decision'] == scn['decision']
    assert status['burn'] == pytest.approx(scn['burn'])
    assert status['budget'] == pytest.approx(scn['budget'])                 # usable budget: what every consumer decides on
    assert status['weekly_budget'] == pytest.approx(scn['weekly'])          # theoretical weekly budget: still reported
    assert status['headroom'] == pytest.approx(scn['budget'] - scn['burn'])
    assert status['weekly_headroom'] == pytest.approx(scn['weekly'] - scn['burn'])
    assert (status['usable'], status['live']) == (scn['usable'], scn['live'])
    unusable = {a for a, d in status['accounts'] if d['decision'] in ('allow', 'over_budget') and not d['usable']}
    assert unusable == scn['unusable']


@pytest.mark.parametrize('scn', SCENARIOS, ids=IDS)
def test_gate_refuses_capped_roles_but_keeps_exemptions_and_overrides(scn, tmp_path):
    pool, accounts, rows, burns = _build(scn, tmp_path)
    cases = [('hd-builder', '', scn['gate']),               # ordinary admission follows the usable pool decision
             ('ci-repair', '', 'exempt'),                   # CI/admin exemption continues
             ('hd-builder', 'P0:123', 'override'),          # logged P0 override continues
             ('hd-builder', 'rajiv:1794854413.227469', 'override')]
    for role, override, effective in cases:
        out = io.StringIO()
        with _burn_for(burns), \
             mock.patch.object(burn, 'read_config', return_value={}), \
             mock.patch.object(burn, 'bindings', return_value=(pool, accounts)), \
             mock.patch.object(burn, 'read_latest', return_value=rows), \
             mock.patch.object(burn, 'load_horizons', return_value=HORIZONS), \
             contextlib.redirect_stdout(out):
            assert burn.shadow(MODEL[pool], role, override, 'unused', 'unused', NOW) == effective
        text = out.getvalue()
        assert f'pool_decision={scn["decision"]}' in text
        assert f'usable_accounts={scn["usable"]}/{scn["live"]}' in text
        assert f'pool_budget={scn["budget"]:.4f}' in text and f'pool_weekly_budget={scn["weekly"]:.4f}' in text


@pytest.mark.parametrize('scn', SCENARIOS, ids=IDS)
def test_hourly_report_shows_usable_budget_and_keeps_the_theoretical_one(scn, tmp_path):
    pool, accounts, rows, burns = _build(scn, tmp_path)
    other = 'codex' if pool == 'claude' else 'claude'
    bound = {pool: accounts, other: ['ok']}
    shadow = types.SimpleNamespace(
        LOG='', read_latest=lambda path: rows, read_config=lambda path: {}, load_horizons=lambda: HORIZONS,
        active_horizon=burn.active_horizon, pool_status=burn.pool_status, account_labels=lambda p, c: {},
        bindings=lambda model, conf: (('claude' if model.startswith('claude') else 'codex'),
                                      bound['claude' if model.startswith('claude') else 'codex']))
    with _burn_for(burns):
        section, modes = composer.burn_status(datetime.fromtimestamp(NOW, timezone.utc), snapshot=Path('/x'),
                                              config=Path('/x'), shadow=shadow)
    label = pool.capitalize()
    verdict = {'allow': 'OK', 'over_budget': 'OVER', 'unavailable': 'NO USABLE ACCOUNT (deny)'}[scn['decision']]
    mode = 'NORMAL' if scn['decision'] == 'allow' else 'P0-ONLY'
    assert modes[label] == mode

    # Rajiv C0ALZJHGE49 ts 1795323839.696369: the Burn section is a pipe table, one pool row then one row per account.
    # Columns follow composer.BURN_TABLE_HEADER: Pool / account, State, Weekly used, Burn, Budget, Headroom, Session,
    # Weekly resets, Note.
    def table_row(first_cell):
        row = next(r for r in section.splitlines() if r.startswith(f'| {first_cell} |'))
        cells = [c.strip() for c in row.strip().strip('|').split('|')]
        assert len(cells) == len(composer.BURN_TABLE_HEADER), row
        return dict(zip(composer.BURN_TABLE_HEADER, cells))

    pool_row = table_row(f'{label} pool')
    assert pool_row['State'] == f'{verdict} · routes {mode}'
    assert pool_row['Burn %/h'] == f'{scn["burn"]:.2f}'
    if _differs(scn):
        # Theoretical weekly headroom stays in the report, labelled as not usable now.
        assert pool_row['Budget %/h'] == f'{scn["budget"]:.2f}'
        assert (f'usable budget; weekly budget {scn["weekly"]:.2f}, '
                f'{scn["live"] - scn["usable"]} account(s) not usable now') in pool_row['Note']
    else:
        assert pool_row['Budget %/h'] == f'{scn["budget"]:.2f}' and 'usable budget' not in pool_row['Note']
    for acct in accounts:
        account_row = table_row(f'{label} {acct}')
        assert ('not usable now' in account_row['Note']) == (acct in scn['unusable']), account_row


def _dial():
    if not DIAL.is_file():
        pytest.skip('deployed burn dial unavailable; the dial is a deployed-only script')
    return _load('proxy_burn_dial_usable', DIAL)


@pytest.mark.parametrize('scn', SCENARIOS, ids=IDS)
def test_dial_votes_on_usable_budget_and_reports_the_weekly_one(scn, tmp_path):
    dial = _dial()
    pool, accounts, rows, burns = _build(scn, tmp_path)
    with _burn_for(burns):
        status = burn.pool_status(pool, accounts, rows, NOW, HORIZONS)
    vote, _ratio = dial.vote(status)
    assert vote == scn['vote']
    item = {'pool': pool, 'label': pool.capitalize(), 'names': {a: a for a in accounts}, 'maxout': set(),
            'status': status, 'drive': status, 'vote': vote, 'ratio': _ratio}
    report = dial.report_rows([item])
    if _differs(scn):
        assert 'usable budget' in report[0] and f'weekly {scn["weekly"]:.2f}' in report[0]
    assert ('(no usable account now)' in report[0]) == (scn['decision'] == 'unavailable')
    for acct in accounts:
        row = next(r for r in report if f'{item["label"]} {acct}:' in r)
        assert ('NOT USABLE NOW' in row) == (acct in scn['unusable']), row


@pytest.mark.parametrize('scn', SCENARIOS, ids=IDS)
def test_dial_verdict_end_to_end_through_the_real_shared_module(scn, tmp_path, monkeypatch):
    dial = _dial()
    pool, accounts, rows, burns = _build(scn, tmp_path)
    other = 'codex' if pool == 'claude' else 'claude'
    bound = {pool: accounts, other: ['ok']}
    shared = types.SimpleNamespace(
        LOG='/nonexistent', read_config=lambda path: {}, read_latest=lambda path: rows, load_horizons=lambda: HORIZONS,
        pool_status=burn.pool_status, session_state=burn.session_state, account_labels=lambda p, c: {},
        bindings=lambda model, conf: (('claude' if model.startswith('claude') else 'codex'),
                                      bound['claude' if model.startswith('claude') else 'codex']))
    state = tmp_path / 'proxy-caps.env'
    state.write_text('PROXY_AGENT_TOTAL_CAP=6\nPROXY_AGENT_CLAUDE_CAP=3\nPROXY_DIAL_CHANGED_AT=0\n')
    monkeypatch.setattr(dial, 'STATE', str(state))
    monkeypatch.setattr(dial, 'load_shared', lambda: shared)
    out = io.StringIO()
    with _burn_for(burns), contextlib.redirect_stdout(out):
        assert dial.main(['--no-refresh', '--apply'], now=NOW) == 0
    first = out.getvalue().splitlines()[0]
    # The other pool is healthy (vote UP), so the overall verdict is the scenario pool's vote.
    assert first.startswith(f'Burn rate: {scn["vote"]} ')
    caps = dict(line.split('=') for line in state.read_text().split())
    # UP: total +2, claude +1. HOLD: unchanged. DOWN: total -2 and Claude straight to 2 (every DOWN row here is Claude
    # at a >=2x ratio from 3, i.e. max(2, 3-1) = 2).
    expected = {'UP': ('8', '4'), 'HOLD': ('6', '3'), 'DOWN': ('4', '2')}[scn['vote']]
    assert (caps['PROXY_AGENT_TOTAL_CAP'], caps['PROXY_AGENT_CLAUDE_CAP']) == expected


def test_the_weekly_only_view_would_have_allowed_and_voted_up_on_the_counterexample(tmp_path):
    """Why the table exists: weekly-only budget 8.1 vs burn 0.5 is a 0.06x ratio (UP, gate allows)."""
    pool, accounts, rows, burns = _build(SCENARIOS[0], tmp_path)
    with _burn_for(burns):
        status = burn.pool_status(pool, accounts, rows, NOW, HORIZONS)
    assert status['burn'] / status['weekly_budget'] < 0.6      # what the weekly-only view saw: UP and allow
    assert status['decision'] == 'over_budget'                 # what the usable view decides: refuse and DOWN


def test_disabled_auth_file_is_never_bound_so_it_cannot_subsidize(tmp_path):
    auth = tmp_path / 'auth'
    auth.mkdir()
    (auth / 'a.json').write_text(json.dumps({'type': 'claude', 'email': 'a', 'disabled': True}))   # pinned away
    (auth / 'b.json').write_text(json.dumps({'type': 'claude', 'email': 'b', 'disabled': False}))
    pool, accounts = burn.bindings('claude-opus-5-5', {'auth-dir': str(auth)})
    assert (pool, accounts) == ('claude', ['b'])
    scn = SCENARIOS[0]                                       # a: 80% weekly left and an open session, but disabled
    _, _, rows, burns = _build(dict(scn, accts={'a': (20, 0.0, 'open'), 'b': (99, 0.5, 'open')}), tmp_path)
    with _burn_for(burns):
        status = burn.pool_status(pool, accounts, rows, NOW, HORIZONS)
    assert status['decision'] == 'over_budget' and status['budget'] == pytest.approx(0.1)
    assert status['weekly_budget'] == pytest.approx(0.1)     # not even the theoretical view counts a disabled file
    assert [a for a, _ in status['accounts']] == ['b']


def test_session_state_table_and_fresh_sample_clears_a_cap(tmp_path):
    def row(**kw):
        base = {'ts': NOW - 60, 'pct': 20, 'reset': NOW + H}
        base.update(kw)
        return {'claude:a:session': base}

    def state(rows, pool='claude'):
        return burn.session_state(pool, 'a', rows, NOW)['state']

    assert state(row()) == 'open' and state(row(pct=99.9)) == 'open'
    assert state(row(pct=100)) == 'capped'
    assert state(row(ts=NOW - 7200)) == 'open' and state(row(ts=NOW - 7201)) == 'stale'   # 2h bound is inclusive
    assert state(row(ts=NOW + 1)) == 'stale'                                               # a future sample is not fresh
    assert state(row(reset=NOW)) == 'stale' and state(row(pct=100, reset=NOW - 1)) == 'stale'
    assert state({}) == 'unknown' and state({'claude:a:session': None}) == 'unknown'
    for bad in (True, False, '10', None, float('nan'), float('inf')):
        assert state(row(pct=bad)) == 'unknown', bad
        assert state(row(reset=bad)) == 'unknown', bad
        assert state(row(ts=bad)) == 'unknown', bad
    assert state(row(pct=101)) == 'unknown' and state(row(pct=-1)) == 'unknown'
    assert state(row(), pool='codex') == 'n/a' and state({}, pool='grok') == 'n/a'
    # Freshly cap-cleared: the newest sample per key wins, so a capped sample followed by a fresh one below 100% is open.
    path = tmp_path / 'log.jsonl'
    path.write_text(''.join(json.dumps(r) + '\n' for r in SESSIONS['cleared']('a')))
    assert state(burn.read_latest(str(path))) == 'open'
    path.write_text(''.join(json.dumps(r) + '\n' for r in SESSIONS['expired-cap']('a')))
    assert state(burn.read_latest(str(path))) == 'stale'


def test_report_session_wording_matches_the_shared_session_state():
    """The composer's display text and the shared usable decision must read the same observation the same way."""
    shown_and_state = {'open': ('session 20%', 'open'), 'capped': ('session CAPPED', 'capped'),
                       'cleared': ('session 3%', 'open'), 'expired-cap': ('session stale', 'stale'),
                       'stale': ('session stale', 'stale'), 'missing': ('session n/a', 'unknown')}
    for kind, (text, state) in shown_and_state.items():
        # Later records win, as read_latest keeps the newest sample per key ('cleared' lists the fresh one last).
        rows = {r['key']: r for r in SESSIONS[kind]('a')}
        assert text in composer._session_state(rows, 'claude', 'a', NOW), kind
        assert burn.session_state('claude', 'a', rows, NOW)['state'] == state, kind
