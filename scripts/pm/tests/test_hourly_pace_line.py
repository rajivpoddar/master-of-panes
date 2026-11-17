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
from datetime import datetime, timezone
from pathlib import Path

ASSETS = Path(__file__).parents[1] / 'shared-assets/claude/scripts'


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


def test_hourly_first_line_still_opens_with_the_hourly_marker_and_carries_pace_first():
    """pm-hourly-post.sh rejects a body whose first line does not match this pattern, so Pace is a clause."""
    pace = '*Pace:* Claude rajiv@scribie 45% used'
    source = (ASSETS / 'pm/heartbeat/heartbeat-compose.py').read_text()
    assert 'first = f"{mention}*Hourly — {stamp}*" + (f" — {pace}" if pace else "") + f" — TL;DR: {tldr}"' in source
    first = f"<@U0BNFGX2UAX> *Hourly — 07:00 IST* — {pace} — TL;DR: ok"
    assert re.match(r'^(<@[A-Z0-9]+> *)?\*Hourly', first)
    assert first.index('*Pace:*') < first.index('TL;DR')


def test_pace_is_wired_for_the_1h_mode_only_and_persists_only_under_the_hourly_claim():
    source = (ASSETS / 'pm/heartbeat/heartbeat-compose.py').read_text()
    assert 'persist=usage_sample_claim_ok(args.sample_usage_claim)' in source
    assert re.search(r'pace = \(pace_status\(.*?\)\s*if args\.mode == "1h" else None\)', source, re.S)
    assert 'burn=burn, pace=pace' in source
