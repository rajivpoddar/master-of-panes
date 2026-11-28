"""Hourly report opens with the account pace statement (rolling rate since a persisted baseline).

Rajiv, DM D0AMF0XE6TS thread 1794876281.724039, ts 1794877877.202879: "every hourly report first state if we are above
or below this rate and ETA for these two accounts." PM correction (heydonna-app-65, 2026-11-17): R is a ROLLING
average since the persisted baseline sample, (used_now - used_base) / hours_since_base, never the last-hour delta,
because "% used" is whole-percent and 1h deltas read 0 or 1 and flip ABOVE/BELOW. The hours of data are shown and
the baseline can be re-pointed through the pace config.

CTO REVISE (C0ALZJHGE49 thread 1794878813.429829, ts 1794879652.704859): remaining quota, ceiling and ETA run from the
last observation (the sample), not from report time, and the sample age is shown; a baseline is bound to its weekly
window by reset stamp (window change or expired sample window -> UNKNOWN and re-baseline; no averaging across windows;
no invented post-reset zero); a passed horizon reads UNKNOWN with the update hint.

Fixed-time table: nothing here reads the wall clock or the live usage log.
Not executed by the MoP repair run (Rajiv, DM D0AMF0XE6TS thread 1793979986.975849, ts 1793983042.752819: "MoP
changes do not require any local testing or proof. they are not app code. MoP is separate repo").
"""
import importlib.util
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ASSETS = Path(__file__).parents[1] / 'shared-assets/claude/scripts'
SLACK_SEND = ASSETS.parent / 'skills/slack-message/scripts/slack-send.sh'


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


composer = _load('heartbeat_compose_pace', ASSETS / 'pm/heartbeat/heartbeat-compose.py')

H = 3600
BASE = datetime(2026, 11, 17, 0, 45, tzinfo=timezone.utc).timestamp()      # 06:15 IST
WINDOW = BASE + 5 * 86400                                                     # this weekly window's reset stamp
CLAUDE = 'claude:rajiv@scribie.com:weekly'
CODEX = 'codex:89aba566-ed7d-45a8-a2c4-619674a86e0c:weekly'
CLAUDE_HORIZON = '2026-11-18T04:00:00Z'                                       # Wed 11-18 09:30 IST
CODEX_HORIZON = '2026-11-20T07:04:00Z'                                        # Fri 11-20 12:34 IST (Pace; Burn keeps 13:06)


def _at(ts):
    return datetime.fromtimestamp(ts, timezone.utc)


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _write_log(path, rows):
    """rows: (ts, key, pct) or (ts, key, pct, reset). The default reset is this test window's reset stamp."""
    lines = []
    for row in rows:
        reset = row[3] if len(row) > 3 else WINDOW
        lines.append(json.dumps({'ts': row[0], 'key': row[1], 'pct': row[2], 'reset': reset}) + '\n')
    path.write_text(''.join(lines))
    return path


def _write_config(path, **overrides):
    accounts = [
        {'label': 'Claude', 'name': 'rajiv@scribie', 'key': CLAUDE, 'horizon': CLAUDE_HORIZON,
         'baseline_from': '2026-11-17T00:45:00Z'},
        {'label': 'Codex', 'name': 'rajiv@scribie', 'key': CODEX, 'horizon': CODEX_HORIZON,
         'baseline_from': '2026-11-17T00:45:00Z'},
    ]
    for account in accounts:
        account.update(overrides)
    path.write_text(json.dumps({'accounts': accounts}))
    return path


def _pace(tmp_path, rows, now, persist=False, config=None, state=None):
    return composer.pace_status(
        _at(now), snapshot=_write_log(tmp_path / 'log.jsonl', rows),
        config=config or _write_config(tmp_path / 'cfg.json'),
        state=state or (tmp_path / 'state.json'), persist=persist)


def _clause(line, label):
    return next(part for part in line.split(' · ') if part.removeprefix('*Pace:* ').startswith(label))


def test_rolling_rate_ceiling_verdict_and_eta(tmp_path):
    now = BASE + 4 * H
    line = _pace(tmp_path, [(BASE, CLAUDE, 45), (now, CLAUDE, 48),
                            (BASE, CODEX, 29), (now, CODEX, 30)], now)
    claude, codex = _clause(line, 'Claude'), _clause(line, 'Codex')
    # sample taken at 10:15 IST (age 0.0h); 3 points over 4.0h -> 0.75 %/h; ceiling (100-48)/23.25h = 2.24 %/h
    assert ('Claude rajiv@scribie 48% used at 10:15 IST (sample 0.0h old), measured 0.75 %/h over 4.0h '
            'vs ceiling 2.24 %/h (to Wed 11-18 09:30 IST) → BELOW, ETA 100% ') in claude
    assert 'Codex rajiv@scribie 30% used at 10:15 IST (sample 0.0h old), measured 0.25 %/h over 4.0h' in codex
    assert '(to Fri 11-20 12:34 IST) → BELOW' in codex
    assert 'ETA UNKNOWN' not in line


def test_above_when_rate_exceeds_ceiling(tmp_path):
    now = BASE + 4 * H
    line = _pace(tmp_path, [(BASE, CLAUDE, 45), (now, CLAUDE, 60),
                            (BASE, CODEX, 29), (now, CODEX, 29)], now)
    assert '→ ABOVE' in _clause(line, 'Claude')      # 15 points / 4h = 3.75 %/h vs ceiling 1.7 %/h
    assert '→ BELOW' in _clause(line, 'Codex')


def test_single_whole_percent_step_does_not_flip_the_verdict_on_a_wide_window(tmp_path):
    """The reason for the rolling average: one whole-percent tick over a short window must not read as a trend."""
    now = BASE + 10 * H
    flat = _pace(tmp_path, [(BASE, CLAUDE, 45), (now - 3 * H, CLAUDE, 45), (now, CLAUDE, 46)], now)
    assert 'measured 0.10 %/h over 10.0h' in _clause(flat, 'Claude')
    assert '→ BELOW' in _clause(flat, 'Claude')


def test_thin_window_is_flagged_and_too_young_baseline_is_unknown(tmp_path):
    thin = _pace(tmp_path, [(BASE, CLAUDE, 45), (BASE + 1 * H, CLAUDE, 46)], BASE + 1 * H + 30)
    assert 'over 1.0h, thin data' in _clause(thin, 'Claude')
    young = _pace(tmp_path, [(BASE, CLAUDE, 45), (BASE + 600, CLAUDE, 45)], BASE + 660)
    assert 'measured UNKNOWN (baseline is only 0.2h old)' in _clause(young, 'Claude')
    assert '→ UNKNOWN, ETA UNKNOWN' in _clause(young, 'Claude')


def test_missing_samples_are_unknown_never_zero(tmp_path):
    now = BASE + 4 * H
    no_codex = _pace(tmp_path, [(BASE, CLAUDE, 45), (now, CLAUDE, 48)], now)
    assert 'Codex rajiv@scribie UNKNOWN (no usage sample' in no_codex
    assert '0 %/h' not in _clause(no_codex, 'Codex')
    stale = _pace(tmp_path, [(BASE, CLAUDE, 45), (now - 3 * H, CLAUDE, 48)], now)   # older than USAGE_SAMPLE_MAX_AGE
    assert 'Claude rajiv@scribie UNKNOWN (no usage sample' in stale
    no_base = _pace(tmp_path, [(BASE - 1800, CLAUDE, 44)], BASE + 60)                   # nothing at/after the baseline
    assert 'measured UNKNOWN (no usage sample at or after the baseline yet)' in _clause(no_base, 'Claude')


def test_usage_decrease_means_unknown_not_a_negative_rate(tmp_path):
    now = BASE + 4 * H
    line = _pace(tmp_path, [(BASE, CLAUDE, 45), (now, CLAUDE, 3)], now)
    assert 'usage fell since the baseline, re-baseline in the pace config' in _clause(line, 'Claude')
    assert '→ UNKNOWN' in _clause(line, 'Claude')


def test_passed_horizon_is_unknown_with_an_update_hint_and_no_weekly_fallback(tmp_path):
    now = datetime(2026, 11, 18, 5, 0, tzinfo=timezone.utc).timestamp()                # after Claude's 04:00Z horizon
    # the sample's live weekly reset is still in the future; it must NOT stand in for the passed horizon
    line = _pace(tmp_path, [(BASE, CLAUDE, 45), (now, CLAUDE, 90)], now)
    clause = _clause(line, 'Claude')
    assert 'horizon Wed 11-18 09:30 IST has passed, update the pace config' in clause
    assert 'own weekly reset' not in clause and 'ABOVE' not in clause and 'BELOW' not in clause


def test_re_baselining_through_config_changes_the_window(tmp_path):
    now = BASE + 10 * H
    rows = [(BASE, CLAUDE, 45), (BASE + 6 * H, CLAUDE, 50), (now, CLAUDE, 55)]
    old = _pace(tmp_path, rows, now)
    assert 'measured 1.00 %/h over 10.0h' in _clause(old, 'Claude')
    cfg = _write_config(tmp_path / 'cfg2.json', baseline_from=_iso(BASE + 5 * H))
    new = _pace(tmp_path, rows, now, config=cfg)
    assert 'measured 1.25 %/h over 4.0h' in _clause(new, 'Claude')                     # (55-50)/4h from the 50% row


def test_baseline_is_cached_only_when_persisting_and_survives_a_trimmed_log(tmp_path):
    now = BASE + 4 * H
    rows = [(BASE, CLAUDE, 45), (now, CLAUDE, 48)]
    _pace(tmp_path, rows, now, persist=False)
    assert not (tmp_path / 'state.json').exists()                                      # no claim, no write
    _pace(tmp_path, rows, now, persist=True)
    cached = json.loads((tmp_path / 'state.json').read_text())['baselines'][CLAUDE]
    assert cached == {'from': '2026-11-17T00:45:00Z', 'ts': BASE, 'pct': 45.0, 'reset': WINDOW}
    trimmed = _pace(tmp_path, [(now, CLAUDE, 48)], now)                                # old rows gone from the log
    assert 'measured 0.75 %/h over 4.0h' in _clause(trimmed, 'Claude')


def test_stored_baseline_from_a_previous_window_is_never_reused(tmp_path):
    now = BASE + 4 * H
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'version': 1, 'baselines': {CLAUDE: {
        'from': '2026-11-17T00:45:00Z', 'ts': BASE, 'pct': 45.0, 'reset': WINDOW - 7 * 86400}}}))
    line = _pace(tmp_path, [(now, CLAUDE, 48)], now, persist=False, state=state)
    clause = _clause(line, 'Claude')
    assert 'weekly window changed since the baseline' in clause and '→ UNKNOWN, ETA UNKNOWN' in clause
    assert 'ABOVE' not in clause and 'BELOW' not in clause


# claude:keychain:weekly alias (Rajiv 2026-11-18): the account follows the PM pane keychain across account switches.
# The newest weekly keychain row is the sample; the baseline is the latest of the configured baseline_from, the first
# sample of the current identity and the first sample after the weekly reset stamp changed; a neutral IST note names the
# restart; the configured horizon is untouched. CTO REVISE (C0ALZJHGE49 thread 1794988176.080829, ts 1794988503.096489):
# the cached baseline is bound to the sampled identity (opaque tag) and weekly window, a usage run without a keychain
# sample leaves the previous login ineligible, alias + concrete key both read, and the note carries no login.
# Synthetic sequences only (not executed by the MoP repair run).
ALIAS = 'claude:keychain:weekly'
KEYCHAIN_DEV = 'claude:keychain-dev@scribie.com:weekly'
KEYCHAIN_RAJIV = 'claude:keychain-rajiv@scribie.com:weekly'
WINDOW_RAJIV = WINDOW + 2 * 86400


def _alias_config(path, baseline_from=BASE, extra=()):
    path.write_text(json.dumps({'accounts': [
        {'label': 'Claude', 'name': 'keychain', 'key': ALIAS, 'horizon': CLAUDE_HORIZON,
         'baseline_from': _iso(baseline_from), 'display': 'Claude: PM pane (keychain)'}, *extra]}))
    return path


def _note_lines(table):
    return [l for l in table.splitlines() if l.startswith('Pace note: ')]


def test_keychain_alias_identity_switch_restarts_the_baseline_and_annotates_the_ist_switch(tmp_path):
    now = BASE + 8 * H
    rows = [(BASE, KEYCHAIN_DEV, 40, WINDOW), (BASE + 2 * H, KEYCHAIN_DEV, 44, WINDOW),
            (BASE + 4 * H, KEYCHAIN_RAJIV, 10, WINDOW_RAJIV), (BASE + 6 * H, KEYCHAIN_RAJIV, 13, WINDOW_RAJIV),
            (BASE + 8 * H, KEYCHAIN_RAJIV, 16, WINDOW_RAJIV)]
    cfg = _alias_config(tmp_path / 'cfg.json')
    line = _pace(tmp_path, rows, now, config=cfg)
    clause = _clause(line, 'Claude')
    # baseline = the new identity's first sample (10% at 10:15 IST), never averaged with the 40-44% rows of the old one:
    # (16-10)/4.0h = 1.50 %/h; ceiling 84/19.25h = 4.36 %/h to the UNCHANGED configured horizon.
    assert ('Claude keychain 16% used at 14:15 IST (sample 0.0h old), measured 1.50 %/h over 4.0h '
            'vs ceiling 4.36 %/h (to Wed 11-18 09:30 IST) → BELOW') in clause
    assert 'PM keychain account changed (10:15 IST)' in clause and 'UNKNOWN' not in clause
    assert 'scribie' not in line and '@' not in line
    table = composer.pace_table(_at(now), snapshot=tmp_path / 'log.jsonl', config=cfg, state=tmp_path / 'state.json')
    lines = table.splitlines()
    assert _cells(_pipe_row(table, 'Claude: PM pane (keychain)'))[2] == '1.50 ✅'
    assert lines[-1] == 'Pace note: PM keychain account changed (10:15 IST)'   # its own line, no login, no suffix
    assert len(lines) == 4 and '@' not in table and 'scribie' not in table


def test_keychain_alias_weekly_reset_stamp_change_restarts_the_baseline_and_caches_it(tmp_path):
    now = BASE + 9 * H
    rolled = WINDOW + 7 * 86400
    rows = [(BASE, KEYCHAIN_DEV, 60, WINDOW), (BASE + 3 * H, KEYCHAIN_DEV, 70, WINDOW),
            (BASE + 5 * H, KEYCHAIN_DEV, 2, rolled), (BASE + 7 * H, KEYCHAIN_DEV, 5, rolled + 0.7),   # sub-second jitter
            (BASE + 9 * H, KEYCHAIN_DEV, 8, rolled)]
    cfg, state = _alias_config(tmp_path / 'cfg.json'), tmp_path / 'state.json'
    clause = _clause(_pace(tmp_path, rows, now, persist=True, config=cfg, state=state), 'Claude')
    # same identity, new weekly reset stamp: baseline = first sample after it (2% at 11:15 IST); (8-2)/4.0h = 1.50 %/h,
    # no UNKNOWN for the window change, no 60-70% rows from the old window in the average.
    assert 'Claude keychain 8% used at 15:15 IST (sample 0.0h old), measured 1.50 %/h over 4.0h' in clause
    assert 'vs ceiling 5.04 %/h (to Wed 11-18 09:30 IST) → BELOW' in clause
    assert 'PM keychain weekly window changed (11:15 IST)' in clause
    assert 'account changed' not in clause and 'UNKNOWN' not in clause
    cached = json.loads(state.read_text())['baselines'][ALIAS]
    assert cached == {'from': '2026-11-17T05:45:00+00:00', 'ts': BASE + 5 * H, 'pct': 2.0, 'reset': rolled,
                      'identity': composer._pace_ident('dev@scribie.com'), 'seen': BASE + 9 * H,
                      'why': 'weekly window changed'}
    assert 'dev@' not in state.read_text() and 'scribie' not in state.read_text()   # the tag, never the login
    again = _clause(_pace(tmp_path, rows, now, persist=False, config=cfg, state=state), 'Claude')
    assert again == clause                                                    # the cached restart baseline is stable


def test_keychain_alias_latest_configured_baseline_wins_and_other_keychain_rows_are_ignored(tmp_path):
    now = BASE + 8 * H
    rows = [(BASE, KEYCHAIN_DEV, 40, WINDOW), (BASE + 2 * H, KEYCHAIN_RAJIV, 20, WINDOW_RAJIV),   # switch before baseline
            (BASE + 5 * H, KEYCHAIN_RAJIV, 22, WINDOW_RAJIV),
            (BASE + 6 * H, KEYCHAIN_RAJIV + ' Fable', 99, WINDOW_RAJIV), (BASE + 7 * H, 'claude:keychain:5h', 100, WINDOW_RAJIV),
            (BASE + 8 * H, KEYCHAIN_RAJIV, 26, WINDOW_RAJIV)]
    cfg = _alias_config(tmp_path / 'cfg.json', baseline_from=BASE + 4 * H)
    line = _pace(tmp_path, rows, now, config=cfg)
    clause = _clause(line, 'Claude')
    # the configured baseline (10:15 IST) is later than the switch, so it is the baseline: first sample at/after it is
    # 22% at 11:15 IST, (26-22)/3.0h = 1.33 %/h. Fable and 5h rows are not weekly keychain rows. No annotation.
    assert ('Claude keychain 26% used at 14:15 IST (sample 0.0h old), measured 1.33 %/h over 3.0h '
            'vs ceiling 3.84 %/h (to Wed 11-18 09:30 IST) → BELOW') in clause
    assert 'PM keychain' not in clause and 'UNKNOWN' not in clause
    table = composer.pace_table(_at(now), snapshot=tmp_path / 'log.jsonl', config=cfg, state=tmp_path / 'state.json')
    assert 'Pace note: ' not in table and len(table.splitlines()) == 3


def test_keychain_alias_a_to_b_to_a_restarts_at_each_switch_and_never_averages_across_accounts(tmp_path):
    now = BASE + 9 * H
    rows = [(BASE, KEYCHAIN_DEV, 40, WINDOW), (BASE + 2 * H, KEYCHAIN_DEV, 44, WINDOW),
            (BASE + 3 * H, KEYCHAIN_RAJIV, 10, WINDOW), (BASE + 4 * H, KEYCHAIN_RAJIV, 11, WINDOW),   # same stamp: identity alone
            (BASE + 5 * H, KEYCHAIN_DEV, 46, WINDOW), (BASE + 7 * H, KEYCHAIN_DEV, 48, WINDOW),       # A is back
            (BASE + 9 * H, KEYCHAIN_DEV, 52, WINDOW)]
    cfg, state = _alias_config(tmp_path / 'cfg.json'), tmp_path / 'state.json'
    clause = _clause(_pace(tmp_path, rows, now, persist=True, config=cfg, state=state), 'Claude')
    # baseline = the first sample of the SECOND A stint (46% at 11:15 IST): (52-46)/4.0h = 1.50 %/h; ceiling 48/18.25h.
    assert 'Claude keychain 52% used at 15:15 IST (sample 0.0h old), measured 1.50 %/h over 4.0h' in clause
    assert 'vs ceiling 2.63 %/h (to Wed 11-18 09:30 IST) → BELOW' in clause
    assert 'PM keychain account changed (11:15 IST)' in clause and 'UNKNOWN' not in clause
    table = composer.pace_table(_at(now), snapshot=tmp_path / 'log.jsonl', config=cfg, state=state)
    assert _note_lines(table) == ['Pace note: PM keychain account changed (11:15 IST)'] and 'scribie' not in table
    cached = json.loads(state.read_text())['baselines'][ALIAS]
    assert cached['identity'] == composer._pace_ident('dev@scribie.com') and cached['ts'] == BASE + 5 * H


def test_keychain_alias_failed_sample_right_after_a_switch_leaves_the_previous_login_ineligible(tmp_path):
    cfg, state = _alias_config(tmp_path / 'cfg.json'), tmp_path / 'state.json'
    before = [(BASE, KEYCHAIN_DEV, 40, WINDOW), (BASE + 1 * H, KEYCHAIN_DEV, 41, WINDOW), (BASE + 2 * H, KEYCHAIN_DEV, 42, WINDOW)]
    _pace(tmp_path, before, BASE + 2 * H, persist=True, config=cfg, state=state)
    cached = json.loads(state.read_text())['baselines'][ALIAS]
    # The login switched; the next usage run wrote its other Claude rows (same ts) but its keychain read failed.
    failed = [*before, (BASE + 3 * H, CLAUDE, 12, WINDOW_RAJIV)]
    now = BASE + 3 * H
    clause = _clause(_pace(tmp_path, failed, now, persist=True, config=cfg, state=state), 'Claude')
    assert 'UNKNOWN (the latest usage run took no PM keychain sample, the previous sample is not used)' in clause
    assert '42%' not in clause and 'BELOW' not in clause and 'ABOVE' not in clause
    table = composer.pace_table(_at(now), snapshot=tmp_path / 'log.jsonl', config=cfg, state=state)
    assert _cells(_pipe_row(table, 'Claude: PM pane (keychain)'))[1:] == ['UNKNOWN'] * 4
    assert json.loads(state.read_text())['baselines'][ALIAS] == cached         # the baseline is kept, not dropped
    # The following run samples the new login: a new segment starts there, visibly, with the old login's rows unused.
    recovered = [*failed, (BASE + 4 * H, CLAUDE, 13, WINDOW_RAJIV), (BASE + 4 * H, KEYCHAIN_RAJIV, 15, WINDOW_RAJIV)]
    table = composer.pace_table(_at(BASE + 4 * H), snapshot=_write_log(tmp_path / 'log.jsonl', recovered), config=cfg, state=state)
    row = _cells(_pipe_row(table, 'Claude: PM pane (keychain)'))
    assert row[1] == '15%' and row[2] == 'UNKNOWN'                             # baseline only 0.0h old: UNKNOWN, not 42%
    assert 'Pace note: PM keychain account changed (10:15 IST)' in table.splitlines()
    # The claimed producer's own usage run failed outright (no rows written): its caller says so, sample ineligible.
    table = composer.pace_table(_at(BASE + 4 * H), snapshot=tmp_path / 'log.jsonl', config=cfg, state=state, sampled=False)
    assert _cells(_pipe_row(table, 'Claude: PM pane (keychain)'))[1:] == ['UNKNOWN'] * 4
    source = (ASSETS / 'pm/heartbeat/heartbeat-compose.py').read_text()
    assert 'sampled=not any(f.startswith("usage:") for f in failures)' in source


def test_keychain_alias_and_a_configured_concrete_key_each_read_the_same_sample_with_their_own_baseline(tmp_path):
    now = BASE + 6 * H
    rows = [(BASE, KEYCHAIN_DEV, 40, WINDOW), (BASE + 2 * H, KEYCHAIN_DEV, 46, WINDOW),
            (BASE + 4 * H, KEYCHAIN_DEV, 48, WINDOW), (BASE + 6 * H, KEYCHAIN_DEV, 52, WINDOW)]
    concrete = {'label': 'Claude', 'name': 'pinned', 'key': KEYCHAIN_DEV, 'horizon': CLAUDE_HORIZON,
                'baseline_from': _iso(BASE + 2 * H), 'display': 'Claude: pinned account'}
    cfg = _alias_config(tmp_path / 'cfg.json', extra=(concrete,))
    table = composer.pace_table(_at(now), snapshot=_write_log(tmp_path / 'log.jsonl', rows), config=cfg,
                                state=tmp_path / 'state.json')
    alias_row = _cells(_pipe_row(table, 'Claude: PM pane (keychain)'))
    pinned_row = _cells(_pipe_row(table, 'Claude: pinned account'))
    assert alias_row[1] == pinned_row[1] == '52%'                              # the concrete row is never UNKNOWN
    assert alias_row[2] == '2.00 ✅'                                            # (52-40)/6.0h from the configured BASE
    assert pinned_row[2] == '1.50 ✅'                                           # (52-46)/4.0h from ITS baseline, +2h
    assert 'UNKNOWN' not in table and 'Pace note: ' not in table


def test_keychain_alias_trimming_the_boundary_keeps_the_cached_baseline_only_for_the_same_account(tmp_path):
    cfg, state = _alias_config(tmp_path / 'cfg.json'), tmp_path / 'state.json'
    full = [(BASE, KEYCHAIN_DEV, 40, WINDOW), (BASE + 1 * H, KEYCHAIN_DEV, 41, WINDOW),
            (BASE + 2 * H, KEYCHAIN_RAJIV, 10, WINDOW_RAJIV), (BASE + 3 * H, KEYCHAIN_RAJIV, 11, WINDOW_RAJIV),
            (BASE + 4 * H, KEYCHAIN_RAJIV, 13, WINDOW_RAJIV), (BASE + 5 * H, KEYCHAIN_RAJIV, 13, WINDOW_RAJIV),
            (BASE + 6 * H, KEYCHAIN_RAJIV, 14, WINDOW_RAJIV)]
    now = BASE + 6 * H
    _pace(tmp_path, full, now, persist=True, config=cfg, state=state)
    # (a) the log is trimmed past the switch and the baseline row: the cached baseline (same identity tag and window)
    # is kept, so the rate is still (14-10)/4.0h and the note survives; a re-baseline would read (14-13)/2.0h = 0.50.
    trimmed = [r for r in full if r[0] >= BASE + 4 * H]
    clause = _clause(_pace(tmp_path, trimmed, now, config=cfg, state=state), 'Claude')
    assert 'measured 1.00 %/h over 4.0h' in clause and 'PM keychain account changed (08:15 IST)' in clause
    # (b) the CTO case: only the OTHER account's rows remain and its reset stamp EQUALS the cached one. The cache belongs
    # to a different identity tag, so it is not reused: a new baseline starts at the first remaining sample, visibly.
    state.unlink()
    _pace(tmp_path, [(BASE, KEYCHAIN_DEV, 40, WINDOW), (BASE + 2 * H, KEYCHAIN_DEV, 43, WINDOW)], BASE + 2 * H,
          persist=True, config=cfg, state=state)
    only_b = [(BASE + 3 * H, KEYCHAIN_RAJIV, 20, WINDOW), (BASE + 4 * H, KEYCHAIN_RAJIV, 21, WINDOW),
              (BASE + 5 * H, KEYCHAIN_RAJIV, 23, WINDOW)]
    clause = _clause(_pace(tmp_path, only_b, BASE + 5 * H, config=cfg, state=state), 'Claude')
    assert 'measured 1.50 %/h' in clause and 'usage fell' not in clause and 'UNKNOWN' not in clause
    assert 'PM keychain account changed (09:15 IST)' in clause
    # (c) rows trimmed past what the cache last saw cannot be tied to the cached segment: restart, visibly.
    late = [(BASE + 9 * H, KEYCHAIN_DEV, 50, WINDOW), (BASE + 10 * H, KEYCHAIN_DEV, 51, WINDOW)]
    clause = _clause(_pace(tmp_path, late, BASE + 10 * H, config=cfg, state=state), 'Claude')
    assert 'PM keychain baseline restarted (15:15 IST)' in clause


def test_keychain_alias_unknown_hour_without_a_live_reset_keeps_the_cached_baseline(tmp_path):
    cfg, state = _alias_config(tmp_path / 'cfg.json'), tmp_path / 'state.json'
    rows = [(BASE, KEYCHAIN_DEV, 40, WINDOW), (BASE + 2 * H, KEYCHAIN_DEV, 44, WINDOW)]
    _pace(tmp_path, rows, BASE + 2 * H, persist=True, config=cfg, state=state)
    cached = json.loads(state.read_text())['baselines'][ALIAS]
    clause = _clause(_pace(tmp_path, [*rows, (BASE + 3 * H, KEYCHAIN_DEV, 45, None)], BASE + 3 * H,
                           persist=True, config=cfg, state=state), 'Claude')
    assert 'UNKNOWN (the sample has no live weekly reset' in clause
    assert json.loads(state.read_text())['baselines'][ALIAS] == cached         # the producer no longer drops it


# Fixed-time static fixture table (CTO proof request, ts 1794879652.704859). Times are UTC.
#   NOW          Tue 11-17 12:00Z (17:30 IST), the report time
#   sample       the last observation, 1h before NOW
#   baseline     the persisted baseline sample, 4h before the sample (07:00Z)
FIXED_NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc).timestamp()
FIXED_WINDOW = FIXED_NOW + 6 * 86400
CASES = [
    {
        # (a) aged accepted sample: 40% -> 80% over 4.0h = 10 %/h. Horizon is 1.5h after NOW, 2.5h after the sample.
        # Observation-time ceiling = 20 / 2.5h = 8.00 %/h, so 10 > 8 -> ABOVE. ETA = 2.0h after the sample = 13:00Z,
        # which is before the 13:30Z horizon. The old report-time basis gave ceiling 20 / 1.5h = 13.33 and read BELOW.
        'name': 'aged accepted sample, ABOVE',
        'rows': [(FIXED_NOW - 5 * H, CLAUDE, 40, FIXED_WINDOW), (FIXED_NOW - 1 * H, CLAUDE, 80, FIXED_WINDOW)],
        'horizon': FIXED_NOW + 1.5 * H,
        'baseline_from': FIXED_NOW - 5 * H,
        'expect': [
            'Claude rajiv@scribie 80% used at 16:30 IST (sample 1.0h old), measured 10.00 %/h over 4.0h '
            'vs ceiling 8.00 %/h (to Tue 11-17 19:00 IST) → ABOVE, ETA 100% Tue 11-17 18:30 IST',
        ],
    },
    {
        # (b) reset-then-caught-up: the baseline (70%) sits in the old window, the latest sample (5%) in a new one.
        # The rate is not averaged across windows and no zero is invented: UNKNOWN, re-baselined to the new window.
        'name': 'reset then caught up, UNKNOWN',
        'rows': [(FIXED_NOW - 6 * H, CLAUDE, 70, FIXED_WINDOW - 7 * 86400),
                 (FIXED_NOW - 1 * H, CLAUDE, 5, FIXED_WINDOW)],
        'horizon': FIXED_NOW + 1.5 * H,
        'baseline_from': FIXED_NOW - 6 * H,
        'expect': [
            'Claude rajiv@scribie 5% used at 16:30 IST (sample 1.0h old), measured UNKNOWN (weekly window changed '
            'since the baseline, re-baselined at 16:30 IST) → UNKNOWN, ETA UNKNOWN',
        ],
    },
]


def test_fixed_time_table_pins_observation_basis_and_window_binding(tmp_path):
    for case in CASES:
        cfg = _write_config(tmp_path / f"{case['name'].replace(' ', '_')}.json",
                            horizon=_iso(case['horizon']), baseline_from=_iso(case['baseline_from']))
        line = _pace(tmp_path, case['rows'], FIXED_NOW, config=cfg)
        clause = _clause(line, 'Claude')
        for expected in case['expect']:
            assert expected in clause, (case['name'], clause)
        assert line.startswith('*Pace:* ')


def test_hourly_first_line_still_opens_with_the_hourly_marker_and_pace_table_sits_under_it():
    """pm-hourly-post.sh rejects a body whose first line does not match this pattern, so the table goes under it."""
    source = (ASSETS / 'pm/heartbeat/heartbeat-compose.py').read_text()
    assert 'first = f"{mention}*Hourly — {stamp}*" + f" — TL;DR: {tldr}"' in source
    assert 'lines = [first, *pace_lines, *(burn_line.splitlines() if burn_line else []), *body]' in source
    first = "<@U0BNFGX2UAX> *Hourly — 07:00 IST* — TL;DR: ok"
    assert re.match(r'^(<@[A-Z0-9]+> *)?\*Hourly', first)


def test_pace_is_wired_for_the_1h_mode_only_and_persists_only_under_the_hourly_claim():
    source = (ASSETS / 'pm/heartbeat/heartbeat-compose.py').read_text()
    assert 'persist=usage_sample_claim_ok(args.sample_usage_claim)' in source
    assert re.search(r'pace = \(pace_table\(.*?\)\s*if args\.mode == "1h" else None\)', source, re.S)
    assert 'burn=burn, pace=pace' in source


PACE_HEADER = ['Account', 'Used', 'Burn %/h', 'Target %/h', 'Runs out at']


def _pipe_row(table, prefix):
    """The pipe-table row whose first cell starts with prefix (note lines start with "Pace note: ", never a pipe)."""
    return next(l for l in table.splitlines() if l.startswith('| ' + prefix))


def _cells(row):
    """The cell texts of one pipe-table row."""
    return [c.strip() for c in row.strip().strip('|').split('|')]


def _slack_send_blocks(text):
    """Mirror of slack-send.sh has_markdown_table / _split_row / convert_text_to_blocks (the plain-text path used by
    pm-hourly-post.sh: slack-send.sh -c C0ALZJHGE49 -f). Keep in sync with ~/.claude/scripts/slack-send.sh."""
    if len([l for l in text.strip().split('\n') if re.match(r'^\s*\|.*\|.*\|', l)]) < 2:
        return None

    def split_row(line):
        cells = line.split('|')
        if cells and cells[0].strip() == '':
            cells = cells[1:]
        if cells and cells[-1].strip() == '':
            cells = cells[:-1]
        return [c.strip() for c in cells]

    blocks, prose, rows, in_table = [], [], [], False

    def flush_prose():
        chunk = '\n'.join(prose).strip('\n')
        if chunk.strip():
            blocks.append({'type': 'section', 'text': {'type': 'mrkdwn', 'text': chunk}})
        prose.clear()

    def flush_table():
        width = max(len(r) for r in rows)
        blocks.append({'type': 'table', 'rows': [r + [''] * (width - len(r)) for r in rows]})
        rows.clear()

    for line in text.split('\n'):
        is_row = bool(re.match(r'^\s*\|[^\n]*\|[^\n]*\|', line))
        is_sep = bool(re.match(r'^\s*\|[-:\s|]+\|', line))
        if is_sep and in_table:
            continue
        if is_row and not is_sep:
            if not in_table:
                flush_prose()
                in_table = True
            rows.append(split_row(line))
        else:
            if in_table:
                flush_table()
                in_table = False
            prose.append(line)
    flush_table() if in_table else flush_prose()
    return blocks


def test_pace_table_rows_show_burn_marker_target_and_runs_out_with_no_login_email(tmp_path):
    now = BASE + 4 * H
    cfg = _write_config(tmp_path / 'cfg.json')
    accounts = json.loads(cfg.read_text())['accounts']
    for account, display in zip(accounts, ('Claude: PM pane (keychain)', 'Codex: rajiv-scribie CLI')):
        account['display'] = display
    cfg.write_text(json.dumps({'accounts': accounts}))
    table = composer.pace_table(_at(now), snapshot=_write_log(tmp_path / 'log.jsonl',
                                [(BASE, CLAUDE, 45), (now, CLAUDE, 48), (BASE, CODEX, 29), (now, CODEX, 30)]),
                                config=cfg, state=tmp_path / 'state.json', persist=False)
    lines = table.splitlines()
    # Rajiv 2026-11-17 (C0ALZJHGE49 1794890771.399469): no code fence, so slack-send.sh renders a native Slack table.
    assert '```' not in table
    assert lines[0] == '| ' + ' | '.join(PACE_HEADER) + ' |'
    assert lines[1] == '|---|---|---|---|---|'
    assert '@' not in table
    claude_row = _pipe_row(table, 'Claude: PM pane (keychain)')
    assert '48%' in claude_row and '0.75 ✅' in claude_row and '≤2.24 to Wed 18 Nov 09:30 IST' in claude_row
    # Rajiv 2026-11-17 (C0ALZJHGE49 1794897995.293229): the burn cell is exactly the rate and the status mark.
    assert _cells(claude_row)[2] == '0.75 ✅'
    codex_row = _pipe_row(table, 'Codex: rajiv-scribie CLI')
    assert '0.25 ✅' in codex_row and 'not at this rate' not in codex_row
    assert _cells(codex_row)[2] == '0.25 ✅'
    assert not any(word in table for word in ('window', 'as of', ' old', 'thin'))
    assert lines[2] == claude_row and lines[3] == codex_row and len(lines) == 4
    # Every row has exactly the header's cell count: no stray pipe inside a cell shifts a column.
    assert all(l.count('|') == len(PACE_HEADER) + 1 for l in lines)


def test_pace_table_unknown_row_reads_unknown_with_a_note_and_no_zero(tmp_path):
    now = BASE + 4 * H
    table = composer.pace_table(_at(now), snapshot=_write_log(tmp_path / 'log.jsonl', [(BASE, CLAUDE, 45), (now, CLAUDE, 48)]),
                                config=_write_config(tmp_path / 'cfg.json'), state=tmp_path / 'state.json', persist=False)
    codex_row = _pipe_row(table, 'Codex: ')
    assert 'UNKNOWN' in codex_row and '0.00' not in codex_row
    # The reason is a plain "Pace note: " prose line, not a bullet: compact_report folds bullets onto the row above.
    assert any(l.startswith('Pace note: Codex: ') and 'no usage sample' in l for l in table.splitlines())
    assert not any(l.startswith(('•', '- ', ' ')) for l in table.splitlines())
    assert '```' not in table and '@' not in table


def test_pace_table_is_accepted_by_the_slack_send_table_converter_as_one_native_table(tmp_path):
    """slack-send.sh turns an unfenced pipe table into ONE native `table` block (header row + one row per account) and
    leaves the UNKNOWN note lines as prose. The old fenced block was space-aligned with no pipes, so the converter never
    saw a table and Slack showed monospace text."""
    now = BASE + 4 * H
    table = composer.pace_table(_at(now), snapshot=_write_log(tmp_path / 'log.jsonl', [(BASE, CLAUDE, 45), (now, CLAUDE, 48)]),
                                config=_write_config(tmp_path / 'cfg.json'), state=tmp_path / 'state.json', persist=False)
    body = f"*Hourly — 07:00 IST* — TL;DR: ok\n{table}\n\n*Burn:* pace to Claude Wed 09:30 IST"
    blocks = _slack_send_blocks(body)
    assert blocks is not None
    tables = [b for b in blocks if b['type'] == 'table']
    assert len(tables) == 1                                       # Slack allows one table block per message
    rows = tables[0]['rows']
    assert rows[0] == PACE_HEADER and len(rows) == 3              # header + Claude + Codex; separator row dropped
    assert all(len(r) == len(PACE_HEADER) for r in rows)
    assert rows[2][0].startswith('Codex') and rows[2][1] == 'UNKNOWN'
    prose = ' '.join(b['text']['text'] for b in blocks if b['type'] == 'section')
    assert '*Hourly' in prose and 'no usage sample' in prose and '|---|' not in prose and '```' not in prose


def _pipe_cells(line):
    return [c.strip() for c in line.strip().strip('|').split('|')]


def test_sender_pages_large_secondary_table_as_plain_pipe_text_without_fences_or_losing_cells():
    body = '\n'.join(['| A | B |', '|---|---|', '| x | y |', '*Later:*', '| Item | Note |', '|---|---|',
                      *(f'| row-{i:03} | {"long note " * 12} |' for i in range(100))])
    blocks = _converter_blocks(body)
    assert sum(b['type'] == 'table' for b in blocks) == 1
    assert not any('```' in b['text']['text'] for b in blocks if b['type'] == 'section')
    pages = [b['text']['text'] for b in blocks if b['type'] == 'section' and b['text']['text'].startswith('| Item |')]
    assert len(pages) > 1
    assert all(len(p) <= 3000 for p in pages)
    assert all(p.splitlines()[:2] == ['| Item | Note |', '|---|---|'] for p in pages)
    assert re.findall(r'row-\d{3}', '\n'.join(pages)) == [f'row-{i:03}' for i in range(100)]


def test_sender_keeps_a_long_cell_whole_and_every_section_within_3000_after_a_wide_header():
    """CTO REVISE on 4cf292ea (C0ALZJHGE49 thread 1795344281.588589, ts 1795346339.695999): a secondary-table row over
    2,800 chars was cut with an ellipsis, and the first row on a page skipped the paging test, so a wide repeated header
    plus a long row could pass Slack's 3000-char section limit. The row is now split into unfenced continuation sections,
    never cut, and every section is budgeted after its header. Width 70 = a ~1k-char header that repeats on each page;
    width 200 = a ~2.6k-char header that is printed once and not repeated."""
    long_cell = ' '.join(f'tok{n:04}' for n in range(420))                 # ~3.4k chars: more than a whole section

    def line(cells):
        return '| ' + ' | '.join(cells) + ' |'

    def table_row(tag, cell='c'):
        return [tag, 'a', 'b', cell, *('d' for _ in range(8))]

    for width in (70, 200):
        head = [f'Column {i:02} ' + 'h' * width for i in range(12)]
        rule = '|' + '|'.join('---' for _ in head) + '|'
        rows = [table_row('row-first', long_cell), table_row('row-mid'), table_row('row-long', long_cell),
                table_row('row-tail')]
        body = '\n'.join(['| A | B |', '|---|---|', '| x | y |', '*Later:*', line(head), rule, *(line(r) for r in rows)])
        blocks = _converter_blocks(body)
        assert [b['type'] for b in blocks].count('table') == 1              # the A/B table; the wide one is plain text
        sections = [b['text']['text'] for b in blocks if b['type'] == 'section']
        assert sections[0] == '*Later:*' and not any('```' in s for s in sections)
        wide = sections[1:]
        assert all(0 < len(s) <= 3000 for s in wide), [len(s) for s in wide]
        text = ' '.join('\n'.join(wide).split())                             # section breaks read as one space
        assert text.count(long_cell) == 2 and '…' not in text               # the whole cell, twice; no truncation mark
        assert re.findall(r'row-(?:first|mid|long|tail)', text) == ['row-first', 'row-mid', 'row-long', 'row-tail']
        assert text.count(line(head)) == (4 if width == 70 else 1)
        if width == 70:
            led = [s for s in wide if 'row-' in s]                           # every page that holds a row starts
            assert len(led) == 4 and all(s.splitlines()[:2] == [line(head), rule] for s in led)  # with the header
        else:
            assert wide[0] == line(head) + '\n' + rule


HOURLY_POST = ASSETS / 'pm-hourly-post.sh'


def _split_report(tmp_path, report):
    """Run the REAL overflow-table splitter embedded in pm-hourly-post.sh (the python3 heredoc between the _SPLIT_PY markers)
    on a composed report, exactly as the poster does. Returns (parent_text, [reply_text, ...])."""
    source = HOURLY_POST.read_text(encoding='utf-8')
    start = source.index("IFS= read -r -d '' _SPLIT_PY <<'PY' || true\n") + len("IFS= read -r -d '' _SPLIT_PY <<'PY' || true\n")
    code = source[start:source.index('\nPY\n', start)] + '\n'
    claim = tmp_path / 'claim'
    claim.mkdir(exist_ok=True)
    src, out = tmp_path / 'report.txt', tmp_path / 'parent.txt'
    src.write_text(report, encoding='utf-8')
    done = subprocess.run([sys.executable, '-I', '-c', code, str(claim), str(src), str(out)],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    count = int(done.stdout.strip())
    replies = [(claim / f'table-reply-{n}.txt').read_text(encoding='utf-8') for n in range(1, count + 1)]
    assert not (claim / f'table-reply-{count + 1}.txt').exists()
    return (out.read_text(encoding='utf-8') if count else report), replies


def _composed_three_table_report(tmp_path):
    now = BASE + 4 * H
    pace = composer.pace_table(_at(now), snapshot=_write_log(tmp_path / 'log.jsonl', [(BASE, CLAUDE, 45), (now, CLAUDE, 48)]),
                               config=_write_config(tmp_path / 'cfg.json'), state=tmp_path / 'state.json', persist=False)
    burn_rows = [['Claude pool', 'OK · routes NORMAL', 'n/a', '0.00', '0.63', '+0.63', 'n/a', 'n/a', '—'],
                 ['Claude a@example.test', 'enabled · pinned', '12%', '0.40', '0.63', '+0.23', '5%', 'n/a', '—']]
    burn = ('\n'.join(['*Burn:* legend', *composer._pipe_table(composer.BURN_TABLE_HEADER, burn_rows)]), {})
    (tmp_path / 'runs').mkdir()                                    # an absent run-state root reads UNAVAILABLE, not 0
    opencode = composer.opencode_line(_at(now), caps=tmp_path / 'caps', root=tmp_path / 'runs', log=tmp_path / 'muse.log',
                                      ps_text='    1     0 root /sbin/launchd\n', environ={},
                                      snapshot=tmp_path / 'no-usage-snapshots.jsonl')
    spark = composer.spark_cap_line({'QWEN_PROXY_CAP': '4', 'SPARK_PROXY_CAP': '6'})
    return composer.compose('1h', _at(now), None, {}, [], failures=[], pace=pace, burn=burn, opencode=opencode, spark=spark)


def test_pace_burn_and_opencode_tables_each_come_out_as_a_native_slack_table_block(tmp_path):
    """Rajiv C0ALZJHGE49 thread 1795351517.305319, ts 1795353266.972289: the Burn and OpenCode Go tables rendered as raw pipes.
    Slack allows ONE native table block per message and the sender renders any later table in a message as plain pipe text,
    so the poster sends each later table as its own thread reply. All three tables must come out as table blocks, one per
    message, with the intro line above each kept as text and no pipe-separator text left anywhere."""
    report = _composed_three_table_report(tmp_path)
    parent, replies = _split_report(tmp_path, report)
    assert len(replies) == 2
    messages = [parent, *replies]

    all_blocks = [_converter_blocks(m) for m in messages]
    for blocks in all_blocks:                                      # one native table per message: Slack would reject two
        assert [b['type'] for b in blocks].count('table') == 1
    headers = [[_cell_text(c) for c in next(b for b in blocks if b['type'] == 'table')['rows'][0]] for blocks in all_blocks]
    assert headers == [PACE_HEADER, list(composer.BURN_TABLE_HEADER), list(composer.OPENCODE_TABLE_HEADER)]

    sections = [b['text']['text'] for blocks in all_blocks for b in blocks if b['type'] == 'section']
    assert not any('|---|' in s or s.lstrip().startswith('|') for s in sections)   # no raw pipe table text anywhere
    assert '```' not in report and not any('```' in s for s in sections)           # still no code fence (ts 1795345746.446959)
    assert '*Spark proxy cap:* 6' in parent                                        # the rest of the report stays in the parent
    for blocks, intro in ((all_blocks[1], '*Burn:*'), (all_blocks[2], '*OpenCode Go:*')):
        kinds = [b['type'] for b in blocks]
        assert kinds.index('table') == 1 and blocks[0]['text']['text'].startswith(intro)   # intro text sits above its table
    assert '*Burn:*' not in parent and '*OpenCode Go:*' not in parent

    # No content or column change: each table's cells and row order are exactly what the composer produced.
    burn_table = next(b for b in all_blocks[1] if b['type'] == 'table')
    assert [_cell_text(c) for c in burn_table['rows'][2]][:3] == ['Claude a@example.test', 'enabled · pinned', '12%']
    opencode_table = next(b for b in all_blocks[2] if b['type'] == 'table')
    assert [_cell_text(c) for c in opencode_table['rows'][1]][:3] == ['OpenCode Go pool', '0', '0']
    assert any(_cell_text(r[0]) == 'hy4-flash-free' for r in opencode_table['rows'])
    joined = '\n'.join(messages)
    for line in report.splitlines():                               # every composed line survives in some message
        assert line in joined, line


def test_splitter_leaves_a_single_table_report_untouched_and_parent_keeps_its_first_line(tmp_path):
    report = '\n'.join(['*Hourly — 18:15 IST* — TL;DR: x', *composer._pipe_table(PACE_HEADER, [['a', '1', '2', '3', '4']]),
                        '', '*Spark proxy cap:* 6'])
    parent, replies = _split_report(tmp_path, report)
    assert replies == [] and parent == report                      # one table: nothing to split, the report is posted as-is
    three = _composed_three_table_report(tmp_path)
    (tmp_path / 'three').mkdir()
    parent3, replies3 = _split_report(tmp_path / 'three', three)
    assert len(replies3) == 2
    # The poster's own first-line gate runs on the parent too: the split must not eat the "*Hourly" header line.
    assert parent3.splitlines()[0] == three.splitlines()[0]


def test_splitter_pages_an_overflow_table_over_the_100_row_block_limit_with_the_header_repeated(tmp_path):
    rows = [[f'row-{i:03}', 'x'] for i in range(250)]
    report = '\n'.join(['*Hourly — 18:15 IST* — x', '| A | B |', '|---|---|', '| p | q |', '', '*Big:* intro',
                        *composer._pipe_table(['Item', 'Note'], rows), '', '*Tail:* t'])
    parent, replies = _split_report(tmp_path, report)
    assert len(replies) == 3                                       # 99 + 99 + 52 body rows per reply
    for n, reply in enumerate(replies):
        blocks = _converter_blocks(reply)
        table = next(b for b in blocks if b['type'] == 'table')
        assert len(table['rows']) <= 100                           # header row included: Slack's per-block row limit
        assert [_cell_text(c) for c in table['rows'][0]] == ['Item', 'Note']
        assert (blocks[0]['type'] == 'section' and blocks[0]['text']['text'] == '*Big:* intro') == (n == 0)
    got = [_cell_text(r[0]) for reply in replies for b in _converter_blocks(reply) if b['type'] == 'table' for r in b['rows'][1:]]
    assert got == [f'row-{i:03}' for i in range(250)]
    assert '*Tail:* t' in parent and 'row-000' not in parent


def test_poster_sends_each_table_reply_in_the_hourly_thread_and_falls_back_only_on_a_definitive_rejection():
    source = HOURLY_POST.read_text(encoding='utf-8')
    # Replies go out as thread replies under the validated parent ts, after the parent receipt and thread registration.
    assert '-c C0ALZJHGE49 -t "$TS" -f < "$base.txt"' in source
    assert source.index('hourly-thread-ts --set') < source.index('_table_reply "$_n"')
    # The parent send takes the split body; a splitter problem leaves the full report to go out unsplit, as before.
    assert '-c C0ALZJHGE49 -f < "$PARENT_BODY"' in source and 'PARENT_BODY=/tmp/heartbeat-1h-post.txt\n' in source
    assert '|| N_TABLE_REPLIES=0' in source
    # Plain-text fallback ONLY on Slack's explicit table rejection (exactly one SLACK_SEND_FAILED line, error=invalid_blocks or
    # invalid_blocks_format). An unreadable response, transport error or any other error is never re-sent (CTO REVISE ts 1795354634.420069).
    fallback = source[source.index('_table_reply() {'):source.index('SEND_RC=0\nSEND_STARTED=1')]
    assert fallback.count('SLACK_TABLE_PLAIN=1') == 1
    gate = 'error=(invalid_blocks|invalid_blocks_format)$'
    assert "grep -c '^SLACK_SEND_FAILED '" in fallback and gate in fallback
    assert fallback.index(gate) < fallback.index('SLACK_TABLE_PLAIN=1')
    assert 'TABLE_REPLY_UNCERTAIN' in fallback and 'exit' not in fallback.replace('exit status', '')
    # A reply never touches the parent receipt file, so a failed reply cannot make the parent HOURLY_POST_UNCERTAIN.
    assert '"$CLAIM/receipt"' not in fallback


def test_sender_plain_table_env_sends_every_table_as_plain_sections_for_the_overflow_fallback():
    source = SLACK_SEND.read_text(encoding='utf-8')
    assert "_os.environ.get('SLACK_TABLE_PLAIN') == '1'" in source
    start = source.index('PAYLOAD=$(python3 -c "\n', source.index('# Auto-wrap plain text in Block Kit')) + len('PAYLOAD=$(python3 -c "\n')
    code = re.sub(r'\\(["\\$`])', r'\1', source[start:source.index('" "$CHANNEL" "$THREAD_TS" "$MESSAGE")', start)])
    body = '\n'.join(['*Later:*', '| Item | Note |', '|---|---|', '| a | b |'])
    plain = subprocess.run([sys.executable, '-I', '-c', code, 'C0ALZJHGE49', '', body], capture_output=True, text=True,
                           timeout=30, env={'SLACK_TABLE_PLAIN': '1', 'PATH': '/usr/bin:/bin'})
    assert plain.returncode == 0, plain.stderr
    blocks = json.loads(plain.stdout)['blocks']
    assert all(b['type'] == 'section' for b in blocks)             # no native table block: the old plain pipe text
    assert any('| Item | Note |' in b['text']['text'] for b in blocks)
    native = _converter_blocks(body)                               # without the env var the table is still native
    assert [b['type'] for b in native].count('table') == 1


_OFFLINE_OK = '{"ok":true,"channel":"C0ALZJHGE49","ts":"1795360000.000100"}'
# Offline stand-in for curl: no network. Thread-check GETs (-G) answer "unknown"; every chat.postMessage POST is logged as native
# (payload carries a table block) or plain, then answered per FAKE_MODE. A native post is rejected only in the reject_* modes.
_FAKE_CURL = '''import json, os, sys
args = sys.argv[1:]
if '-G' in args:
    sys.stdout.write('{"ok":false,"error":"missing_scope"}')
    sys.exit(0)
body = json.loads(args[args.index('-d') + 1])
native = any(b.get('type') == 'table' for b in body.get('blocks', []))
with open(os.environ['FAKE_LOG'], 'a') as log:
    log.write('native\\n' if native else 'plain\\n')
mode = os.environ['FAKE_MODE']
ok = os.environ['FAKE_OK']
out = {
    'ok': ok,
    'reject_table': '{"ok":false,"error":"invalid_blocks"}' if native else ok,
    'reject_format': '{"ok":false,"error":"invalid_blocks_format"}' if native else ok,
    'reject_both': '{"ok":false,"error":"invalid_blocks"}',
    'other_error': '{"ok":false,"error":"ratelimited"}',
    'empty': '',
    'html': '<html>502 Bad Gateway</html>',
    'array': '[]',
    'no_ok': '{"error":"invalid_blocks"}',
    'ok_false_no_error': '{"ok":false}',
    'ok_no_ts': '{"ok":true}',
}[mode]
sys.stdout.write(out)
'''


def _table_reply_offline(tmp_path, mode):
    """Run the REAL slack-send.sh (only its token file path is repointed at a fake env file) and the REAL _table_reply function
    extracted from pm-hourly-post.sh, with curl faked on PATH. Nothing touches Slack or a real token."""
    source = HOURLY_POST.read_text(encoding='utf-8')
    func = source[source.index('_table_reply() {'):source.index('SEND_RC=0\nSEND_STARTED=1')]
    real_sender = '/Users/rajiv/.claude/scripts/slack-send.sh'
    assert func.count(real_sender) == 2                             # the native send and the one plain re-send
    sender_src = SLACK_SEND.read_text(encoding='utf-8')
    env_line = 'PM_ENV="/Users/rajiv/Downloads/projects/heydonna-app/.env.local"'
    assert env_line in sender_src
    # The sender runs under set -euo pipefail and greps both token names, so the fake env file carries both lines.
    (tmp_path / 'fake.env').write_text('SLACK_BOT_TOKEN=offline-test-token\nSLACK_ALERTS_BOT_TOKEN=offline-test-token\n', encoding='utf-8')
    sender = tmp_path / 'slack-send.sh'
    sender.write_text(sender_src.replace(env_line, f'PM_ENV="{tmp_path}/fake.env"'), encoding='utf-8')
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    (bindir / 'curl').write_text(f'#!{sys.executable}\n{_FAKE_CURL}', encoding='utf-8')
    (bindir / 'curl').chmod(0o755)
    claim = tmp_path / 'claim'
    claim.mkdir()
    parent_receipt = 'OK ts=1795355101.961449 channel=C0ALZJHGE49\n'
    (claim / 'receipt').write_text(parent_receipt, encoding='utf-8')
    (claim / 'table-reply-1.txt').write_text('*Burn:* intro\n| A | B |\n|---|---|\n| 1 | 2 |\n', encoding='utf-8')
    driver = tmp_path / 'driver.sh'
    driver.write_text('set -uo pipefail\nCLAIM="$1"\nTS=1795355101.961449\n'
                      "_OK_RE='^OK ts=[0-9]+\\.[0-9]+ channel=C0ALZJHGE49$'\n"
                      + func.replace(real_sender, str(sender)) + '\n_table_reply 1\necho "RC=$?"\n', encoding='utf-8')
    env = {'PATH': f'{bindir}:/usr/bin:/bin', 'HOME': str(tmp_path), 'FAKE_MODE': mode, 'FAKE_OK': _OFFLINE_OK,
           'FAKE_LOG': str(tmp_path / 'posts.log')}
    done = subprocess.run(['bash', str(driver), str(claim)], capture_output=True, text=True, timeout=120, env=env, cwd=tmp_path)
    log = tmp_path / 'posts.log'
    return {'stdout': done.stdout, 'stderr': done.stderr,
            'posts': log.read_text(encoding='utf-8').split() if log.exists() else [],
            'errors': (claim / 'table-reply-1.errors').read_text(encoding='utf-8'),
            'parent_receipt_unchanged': (claim / 'receipt').read_text(encoding='utf-8') == parent_receipt}


def test_table_reply_resends_plain_only_after_an_explicit_table_rejection_and_never_after_an_unreadable_response(tmp_path):
    """CTO REVISE on ef00398 (C0ALZJHGE49 thread 1795351517.305319, ts 1795354634.420069): an explicit table rejection permits
    exactly one plain reply; an empty, malformed or otherwise unreadable response permits zero (Slack may already have posted the
    native reply); the confirmed parent receipt stays recorded and _table_reply still returns 0 in every case."""
    cases = [  # mode, native/plain posts, result line, error= text the sender printed for the native post
        ('ok', ['native'], 'TABLE_REPLY n=1 native OK ts=', None),
        ('reject_table', ['native', 'plain'], 'TABLE_REPLY_FALLBACK n=1', 'error=invalid_blocks\n'),
        ('reject_format', ['native', 'plain'], 'TABLE_REPLY_FALLBACK n=1', 'error=invalid_blocks_format\n'),
        ('reject_both', ['native', 'plain'], 'TABLE_REPLY_FAILED n=1', 'error=invalid_blocks\n'),
        ('empty', ['native'], 'TABLE_REPLY_UNCERTAIN n=1', 'error=unreadable_response_delivery_uncertain\n'),
        ('html', ['native'], 'TABLE_REPLY_UNCERTAIN n=1', 'error=unreadable_response_delivery_uncertain\n'),
        ('array', ['native'], 'TABLE_REPLY_UNCERTAIN n=1', 'error=unreadable_response_delivery_uncertain\n'),
        ('no_ok', ['native'], 'TABLE_REPLY_UNCERTAIN n=1', 'error=unreadable_response_delivery_uncertain\n'),
        ('ok_false_no_error', ['native'], 'TABLE_REPLY_UNCERTAIN n=1', 'error=unreadable_response_delivery_uncertain\n'),
        ('ok_no_ts', ['native'], 'TABLE_REPLY_UNCERTAIN n=1', 'error=missing_ts_delivery_uncertain\n'),
        ('other_error', ['native'], 'TABLE_REPLY_UNCERTAIN n=1', 'error=ratelimited\n'),
    ]
    for mode, posts, result_line, error_text in cases:
        case_dir = tmp_path / mode
        case_dir.mkdir()
        got = _table_reply_offline(case_dir, mode)
        assert got['posts'] == posts, (mode, got)
        assert result_line in got['stdout'] and 'RC=0' in got['stdout'], (mode, got)
        assert got['parent_receipt_unchanged'], mode
        if error_text:
            assert error_text in got['errors'], (mode, got['errors'])


def test_pace_cell_neutralises_pipes_and_newlines_so_columns_cannot_shift():
    assert composer._pace_cell('a | b\nc') == 'a / b c'
    assert composer._pace_cell('0.75 ✅') == '0.75 ✅'


def test_pace_table_numeric_burn_cell_is_rate_and_mark_only_with_no_window_or_age(tmp_path):
    """Rajiv 2026-11-17 (C0ALZJHGE49 thread 1794897995.293229, ts 1794898350.080809: "drop the window stats form the
    burn % column"), superseding CTO REVISE ts 1794881751.339589. A numeric burn cell is the rate and the status mark
    ('1.68 ✅'): no window length, no thin flag, no as-of time and no sample age, for a thin window and a non-thin one.
    The calculations are unchanged, so the rate and marker equal what the clause reports."""
    window_s = 2 * H + 18 * 60                                   # 2.3h, non-thin, not a whole hour
    sample = BASE + window_s
    now = sample + 12 * 60                                       # accepted sample, 12m old
    thin_key = 'grok:local:weekly'
    cfg = tmp_path / 'cfg.json'
    cfg.write_text(json.dumps({'accounts': [
        {'label': 'Claude', 'name': 'rajiv-scribie', 'key': CLAUDE, 'horizon': CLAUDE_HORIZON,
         'baseline_from': '2026-11-17T00:45:00Z', 'display': 'Claude: PM pane (keychain)'},
        {'label': 'Grok', 'name': 'local', 'key': thin_key, 'horizon': CLAUDE_HORIZON,
         'baseline_from': '2026-11-17T00:45:00Z', 'display': 'Grok: local'},
        {'label': 'Codex', 'name': 'rajiv-scribie', 'key': CODEX, 'horizon': CODEX_HORIZON,
         'baseline_from': '2026-11-17T00:45:00Z', 'display': 'Codex: rajiv-scribie CLI'},
    ]}))
    log = _write_log(tmp_path / 'log.jsonl', [
        (BASE, CLAUDE, 40), (sample, CLAUDE, 41),                 # 1 point / 2.3h, aged 12m
        (sample - H, thin_key, 10), (sample, thin_key, 11),       # 1.0h, still thin
    ])
    table = composer.pace_table(_at(now), snapshot=log, config=cfg, state=tmp_path / 'state.json', persist=False)
    burn_cell = re.compile(r'^\d+\.\d\d (✅|⚠️)$')
    claude_row = _pipe_row(table, 'Claude: PM pane (keychain)')
    assert burn_cell.match(_cells(claude_row)[2]), claude_row
    thin_row = _pipe_row(table, 'Grok: local')
    assert burn_cell.match(_cells(thin_row)[2]) and 'UNKNOWN' not in thin_row, thin_row
    for row in (claude_row, thin_row):
        assert not any(word in row for word in ('window', 'as of', ' old', 'thin', '08:33')), row
    codex_row = _pipe_row(table, 'Codex: ')
    assert 'UNKNOWN' in codex_row and '0.00' not in codex_row
    assert any(l.startswith('Pace note: Codex: ') and 'no usage sample' in l for l in table.splitlines())
    assert '```' not in table and '@' not in table


# ---------------------------------------------------------------------------------------------------------------
# Full-path static fixture (CTO REVISE on fd5876ee, C0ALZJHGE49 thread 1794890771.399469, ts 1794893959.552039).
# Composer -> the REAL slack-send.sh converter, no network. Two defects shipped together in fd5876ee:
#   1. Pace UNKNOWN notes were "• " bullets, so compact_report folded them onto the account's pipe row (a 6th cell).
#   2. slack-send.sh _flush_prose emitted a whole prose run as ONE section; Slack rejects a section over 3000 chars.
# ---------------------------------------------------------------------------------------------------------------
def _converter_blocks(text):
    """Run the real auto-wrap python embedded in slack-send.sh (PAYLOAD=$(python3 -c "...")) on text and return its
    BlockKit blocks. The body sits inside a bash double-quoted string, so undo bash's double-quote escapes
    (backslash before a double quote, dollar, backtick or backslash) and run it with the script's own argv."""
    source = SLACK_SEND.read_text(encoding='utf-8')
    start = source.index('PAYLOAD=$(python3 -c "\n', source.index('# Auto-wrap plain text in Block Kit'))
    start += len('PAYLOAD=$(python3 -c "\n')
    end = source.index('" "$CHANNEL" "$THREAD_TS" "$MESSAGE")', start)
    code = re.sub(r'\\(["\\$`])', r'\1', source[start:end])
    done = subprocess.run([sys.executable, '-I', '-c', code, 'C0ALZJHGE49', '', text],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)['blocks']


def _cell_text(cell):
    return cell['elements'][0]['elements'][0]['text']


def test_full_path_long_report_with_unknown_pace_notes_yields_one_3x5_table_and_bounded_sections(tmp_path):
    now = BASE + 4 * H
    # An empty usage log: both accounts are UNKNOWN, so the table carries one reason note per account.
    pace = composer.pace_table(_at(now), snapshot=_write_log(tmp_path / 'log.jsonl', []),
                               config=_write_config(tmp_path / 'cfg.json'), state=tmp_path / 'state.json', persist=False)
    assert pace.count('Pace note: ') == 2 and 'no usage sample' in pace
    # Compaction pressure: 24 sections of 2-bullet runs. The 1h budget is ~26 physical lines, so compact_report must
    # fold every foldable run, and the Pace note run is the earliest and smallest of them.
    tracked = []
    for n in range(24):
        tracked += [f'*Tracked feature {n}:* status', f'• item {n}a ' + 'a' * 380, f'• item {n}b ' + 'b' * 380]
    report = composer.compose('1h', _at(now), None, {}, [], failures=[], tracked=tracked, pace=pace,
                              burn=('*Burn:* Claude 52% used · Codex 31% used', {}))
    lines = report.splitlines()

    assert '(2 items):' in report                                    # the pressure was real: runs were folded
    pipe_rows = [i for i, l in enumerate(lines) if l.lstrip().startswith('|')]
    assert pipe_rows == list(range(pipe_rows[0], pipe_rows[0] + 4))  # header, separator, Claude, Codex: contiguous
    assert all(lines[i].count('|') == 6 for i in pipe_rows)          # 5 cells each, no folded 6th cell
    after_table = '\n'.join(lines[pipe_rows[-1] + 1:])
    assert len(after_table) >= 4500                                  # a long prose run after the table
    assert 'Pace note: Claude' in after_table and 'Pace note: Codex' in after_table

    blocks = _converter_blocks(report)
    tables = [b for b in blocks if b['type'] == 'table']
    assert len(tables) == 1
    grid = [[_cell_text(c) for c in row] for row in tables[0]['rows']]
    assert len(grid) == 3 and all(len(row) == 5 for row in grid)     # exactly one 3x5 table
    assert grid[0] == PACE_HEADER and grid[1][1] == 'UNKNOWN' and grid[2][1] == 'UNKNOWN'
    assert not any('no usage sample' in cell or 'Pace note' in cell for row in grid for cell in row)  # reasons outside

    sections = [b['text']['text'] for b in blocks if b['type'] == 'section']
    assert all(0 < len(s) <= 3000 for s in sections), [len(s) for s in sections]
    assert len(sections) >= 3                                        # the long run was split, not shipped whole
    prose = '\n'.join(sections)
    assert 'Pace note: Claude' in prose and 'Pace note: Codex' in prose and 'no usage sample' in prose
    assert all(f'Tracked feature {n}:' in prose for n in range(24))  # chunking dropped no section
    assert blocks[0]['type'] == 'section' and re.match(r'^(<@[A-Z0-9]+> *)?\*Hourly', blocks[0]['text']['text'])


def test_slack_send_prose_sections_stay_within_3000_with_and_without_a_table():
    long_run = '\n'.join(f'row {n} ' + 'w' * 300 for n in range(40))       # ~12.4k chars of short lines
    unbroken = 'Z' * 7000                                                  # one line, no space to wrap at
    with_table = f'*Hourly — 07:00 IST*\n| A | B |\n|---|---|\n| 1 | 2 |\n{long_run}\n{unbroken}'
    sections = [b['text']['text'] for b in _converter_blocks(with_table) if b['type'] == 'section']
    assert [b['type'] for b in _converter_blocks(with_table)].count('table') == 1
    assert sections[0] == '*Hourly — 07:00 IST*'
    assert all(0 < len(s) <= 3000 for s in sections) and 'row 39 ' in '\n'.join(sections)
    assert sum(s.count('Z') for s in sections) == 7000                     # hard wrap loses no characters
    # No-table path: the first line alone is over the old splitter's limit, which used to emit an empty section.
    plain = [b['text']['text'] for b in _converter_blocks(unbroken + '\n' + long_run) if b['type'] == 'section']
    assert all(0 < len(s) <= 3000 for s in plain)
    # A short message is still ONE untouched section.
    assert [b['text']['text'] for b in _converter_blocks('plain status')] == ['plain status']


def test_compact_report_never_folds_bullets_onto_a_pipe_table_row():
    rows = ['*Hourly — 07:00 IST* — TL;DR: ok', '| A | B |', '|---|---|', '| x | y |',
            '• stray one', '• stray two', '*Next:*', '• keep one', '• keep two']
    folded = composer.compact_report(rows, 5, cadence='1h')
    assert [l for l in folded.splitlines() if l.startswith('|')] == ['| A | B |', '|---|---|', '| x | y |']
    assert '• stray one' in folded and '• stray two' in folded           # skipped, never merged into the table row
    assert '*Next:* (2 items): • keep one; +1 more' in folded            # other runs still fold as before


# --- Ultra "Runs out at" uses a recent window (PM addendum heydonna-app-79, 28 Nov) -------------------------------------
ULTRA = 'claude:ultra-dev:weekly'
ULTRA_OTHER = 'claude:ultra-other:weekly'


def _ultra(tmp_path, rows, now):
    return composer._ultra_pace_read(_at(now), snapshot=_write_log(tmp_path / 'ultra.jsonl', rows),
                                     caps_file=tmp_path / 'caps.env')


def test_ultra_burn_uses_trailing_3h_not_weekly_average(tmp_path):
    now = BASE + 71 * H
    rows = [(BASE, ULTRA, 0.0), (now - 3 * H, ULTRA, 40.0), (now, ULTRA, 50.0)]   # slow week, fast last 3h
    data = _ultra(tmp_path, rows, now)
    assert data['hours'] == 3.0 and abs(data['rate'] - 10 / 3) < 1e-9             # not 50/71
    assert data['t']['verdict'] == 'ABOVE' and 'runs out' in data['t']['detail']


def test_ultra_short_history_uses_oldest_row_and_flags_thin(tmp_path):
    now = BASE + 2 * H
    data = _ultra(tmp_path, [(BASE, ULTRA, 10.0), (now, ULTRA, 12.0)], now)
    assert data['hours'] == 2.0 and data['thin'] is True


def test_ultra_recent_window_never_mixes_accounts_or_weekly_windows(tmp_path):
    now = BASE + 10 * H
    rows = [(now - 3 * H, ULTRA_OTHER, 90.0),                         # other account at the baseline instant
            (now - 3 * H - 60, ULTRA, 20.0),                          # same account, same window: the baseline
            (now - 3 * H + 60, ULTRA, 21.0, WINDOW - 7 * 86400),      # same account, an old weekly window
            (now, ULTRA, 23.0)]
    data = _ultra(tmp_path, rows, now)
    assert abs(data['hours'] - (3 * H + 60) / H) < 1e-9 and abs(data['rate'] - 3 / ((3 * H + 60) / H)) < 1e-9
