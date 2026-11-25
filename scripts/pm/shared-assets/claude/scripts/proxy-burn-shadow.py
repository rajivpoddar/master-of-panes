#!/usr/bin/env python3
"""Weekly provider burn decisions, shared by the launcher gate, the hourly burn section and the burn dial.

Pacing (Rajiv C0ALZJHGE49 thread 1794851199.275299, ts 1794852497.756029: "Claude account has to last till wed
9:30 am and codex till 3d 13h ... Use up any free headroom"):
  * One horizon per pool, configured in ONE file (burn-horizons.json). After a horizon passes, or when none is
    configured, an account paces to its own weekly reset.
  * Per-account budget %/h = (100 - weekly used%) / hours to the horizon. An account whose weekly resets BEFORE
    the horizon is paced to that reset, then gets 100 / hours(reset -> horizon) after it.
  * The launch decision is per POOL: pool burn vs pool budget (exhausted accounts contribute neither), so one
    exhausted or over-pace account no longer hides free headroom on the others.
  * The pool budget the gate and the dial decide on counts only USABLE accounts (CTO REVISE of 35df934,
    C0ALZJHGE49 thread 1794851199.275299, ts 1794854782.620999: "Capped or unknown session capacity must not
    subsidize ordinary admission or a dial increase"). A Claude account is usable now only with a fresh session
    sample below 100%; a capped, stale or unknown session sample is not. Disabled auth files are never bound
    (bindings()). The theoretical weekly budget (every measured account) is still reported as weekly_budget.
"""
import datetime as dt
import glob
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time

HOME = os.path.expanduser('~')
LOG = os.path.join(HOME, '.claude/state/usage-snapshots.jsonl')
NOW = time.time()
# The ONE place pacing horizons are configured: {"claude": "<ISO-8601 with offset>", "codex": "..."}; keys that
# start with "_" are comments. A missing/unreadable file or a passed horizon means "pace to each account's own reset".
HORIZON_FILE = os.path.join(HOME, '.claude/state/burn-horizons.json')
# A weekly reset within 30 min of the horizon IS the horizon (resets are reported to the minute, horizons are set by hand).
HORIZON_SLACK = 1800
# A session (5h) sample older than this, from the future, or whose reset already passed says nothing about capacity now.
SESSION_MAX_AGE = 7200
WEEKLY_MAX_AGE = 7200
CODEX_WEEKLY_SECONDS = 7 * 24 * 3600
# Session states that count as usable capacity. 'n/a' = the pool has no session meter (Codex, Grok): weekly decides alone.
USABLE_SESSION = ('n/a', 'open')

# Kept identical to usage-snapshot.py: latest log record over one hour old.
def prev(key):
    best = None
    try:
        for line in open(LOG):
            r = json.loads(line)
            if r["key"] == key and NOW - r["ts"] > 3600: best = r  # latest one >1h old
    except FileNotFoundError: pass
    return best


def burn_for(key, pct, reset):
    p = prev(key)
    if p and (not all(isinstance(p.get(k), (int, float)) and not isinstance(p.get(k), bool) and math.isfinite(p[k]) for k in ('ts', 'pct', 'reset')) or not 0 <= p['pct'] <= 100):
        return None
    if (not p or p['reset'] <= NOW) and key.startswith('codex:') and key.endswith(':weekly') and pct == 0:
        # A fresh observation in the current week reports no usage. Don't wait for the
        # one-hour anchor to cross the reset before admitting this capacity.
        if reset - CODEX_WEEKLY_SECONDS <= NOW:
            return 0.0
    if p and abs(reset - p['reset']) >= 60:
        return None
    burn = eta = None
    if p and reset - p["reset"] < 60 and pct >= p["pct"]:  # same window, no reset between
        burn = (pct - p["pct"]) / ((NOW - p["ts"]) / 3600)
    return burn


# Pace-clause measure (heartbeat-compose.py pace_status / pace_table): the rate is a ROLLING average since the first
# sample on or after the account's baseline_from in hourly-pace.json, within the same weekly window. Whole-percent
# samples make a one-hour anchor read 0 or 1 %/h, so the pool decision uses this measure for accounts that have a
# pace baseline (Rajiv hourly 2026-11-17 16:17: table 0.50 vs pool 1.00 for the same Codex account).
PACE_CONFIG = os.path.join(HOME, '.claude/state/hourly-pace.json')
ROLLING_MIN_HOURS = 0.25  # same floor as the pace clause: a baseline this young carries no rate


def pace_baseline_from(key, path=None):
    """Epoch of the account's pace baseline_from, or None when the key has no usable baseline."""
    path = path or os.environ.get('HOURLY_PACE_CONFIG') or PACE_CONFIG
    try:
        with open(path) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return None
    for a in (raw.get('accounts') if isinstance(raw, dict) else None) or []:
        if isinstance(a, dict) and a.get('key') == key and isinstance(a.get('baseline_from'), str):
            try:
                parsed = dt.datetime.fromisoformat(a['baseline_from'].strip().replace('Z', '+00:00'))
            except ValueError:
                return None
            return parsed.timestamp() if parsed.tzinfo is not None else None
    return None


def rolling_burn(key, pct, reset, ts):
    """%/h since the pace baseline: (pct - pct at first sample on/after baseline_from) / hours, same weekly window.
    None when there is no baseline, the window changed, usage fell, or the window is under ROLLING_MIN_HOURS."""
    base_from = pace_baseline_from(key)
    if base_from is None or not math.isfinite(ts):
        return None
    first = None
    try:
        with open(LOG) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(r, dict) or r.get('key') != key:
                    continue
                r_ts, r_pct, r_reset = r.get('ts'), r.get('pct'), r.get('reset')
                if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                       for v in (r_ts, r_pct, r_reset)):
                    continue
                if r_ts >= base_from and abs(reset - r_reset) < 60 and (first is None or r_ts < first['ts']):
                    first = {'ts': r_ts, 'pct': r_pct}
    except OSError:
        return None
    if first is None:
        return None
    hours = (ts - first['ts']) / 3600
    if hours < ROLLING_MIN_HOURS or pct < first['pct']:
        return None
    return (pct - first['pct']) / hours


def meter(latest, field):
    # Type-check BEFORE float(): float(False) == 0.0 would turn a bad reading into an allow.
    value = latest[field]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('bad_meter')
    return float(value)


def load_horizons(path=None):
    """pool -> horizon (epoch seconds). Missing, unreadable or malformed entries are ignored, which paces that
    pool to each account's own weekly reset."""
    path = path or os.environ.get('PROXY_BURN_HORIZONS') or HORIZON_FILE
    try:
        with open(path) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    if isinstance(raw, dict):
        for pool, stamp in raw.items():
            if not isinstance(pool, str) or pool.startswith('_') or not isinstance(stamp, str):
                continue
            try:
                parsed = dt.datetime.fromisoformat(stamp.strip().replace('Z', '+00:00'))
            except ValueError:
                continue
            if parsed.tzinfo is not None:  # an offset-less stamp is ambiguous, so it is ignored
                out[pool] = parsed.timestamp()
    return out


def active_horizon(pool, horizons, now):
    horizon = (horizons or {}).get(pool)
    return horizon if isinstance(horizon, (int, float)) and not isinstance(horizon, bool) and horizon > now else None


def pace_budget(pct, reset, now, horizon):
    """Allowed burn in %/h as (budget_now, budget_after_reset, target).

    target 'own'     : no active horizon, paced to this account's own weekly reset
           'horizon' : the weekly reset is at or after the horizon, paced to the horizon
           'reset'   : the weekly reset comes first; budget_after_reset is the pace from that reset to the horizon
    """
    left = max(0.0, 100 - pct)
    floor = 1 / 60  # never divide by less than a minute
    if horizon is None:
        return left / max((reset - now) / 3600, floor), None, 'own'
    if reset >= horizon - HORIZON_SLACK:
        return left / max((horizon - now) / 3600, floor), None, 'horizon'
    return left / max((reset - now) / 3600, floor), 100 / max((horizon - reset) / 3600, floor), 'reset'


def account_detail(key, rows, pool, now, horizons=None):
    """Everything the gate, the report and the dial need about one account's weekly window.

    decision: allow | over_budget | exhausted | unknown. burn/budget/headroom are %/h; headroom = budget - burn
    (signed) and is None for exhausted and unknown accounts. 'after' is the budget once a pre-horizon reset lands.
    """
    horizons = load_horizons() if horizons is None else horizons
    d = {'decision': 'unknown', 'pct': None, 'reset': None, 'ts': None, 'burn': None, 'budget': None,
         'headroom': None, 'after': None, 'target': None}
    latest = rows.get(key)
    if not latest:
        return d
    try:
        ts, pct, reset = (meter(latest, k) for k in ('ts', 'pct', 'reset'))
        if all(map(math.isfinite, (ts, pct, reset))) and 0 <= pct <= 100:
            d.update(pct=pct, reset=reset, ts=ts)  # kept for display even when the sample is unusable
        if not all(map(math.isfinite, (ts, pct, reset))) or not 0 <= pct <= 100 or not 0 <= now - ts <= WEEKLY_MAX_AGE or reset <= now:
            return d
        budget, after, target = pace_budget(pct, reset, now, active_horizon(pool, horizons, now))
        d.update(budget=budget, after=after, target=target)
        if pct >= 100:
            d.update(decision='exhausted', budget=0.0)
            return d
        # Collector uses NOW as the timestamp of its current observation. For an
        # existing snapshot, use the collector timestamp, not launch time.
        global NOW
        old = NOW
        NOW = ts
        try:
            burn = burn_for(key, pct, reset)
        finally:
            NOW = old
        if burn is None or not math.isfinite(burn) or burn < 0:
            d['budget'] = d['after'] = None
            return d
        d.update(burn=burn, headroom=budget - burn, decision='over_budget' if burn > budget else 'allow')
        return d
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OSError, json.JSONDecodeError):
        return {'decision': 'unknown', 'pct': None, 'reset': None, 'ts': None, 'burn': None, 'budget': None,
                'headroom': None, 'after': None, 'target': None}


def account_decision(key, rows, pool, now, horizons=None):
    """(decision, burn, budget) for one account; same contract the gate and its tests always had."""
    d = account_detail(key, rows, pool, now, horizons)
    if d['decision'] == 'unknown':
        return 'unknown', None, None
    return d['decision'], d['burn'], d['budget']


def session_state(pool, account, rows, now):
    """Presently usable session capacity of one account, from the latest ':session' sample.

    state: n/a      the pool has no session meter (Codex, Grok); the weekly window alone decides
           open     fresh sample below 100% with its reset still ahead: usable now
           capped   fresh sample at 100% with its reset still ahead: not usable until it resets
           stale    sample older than SESSION_MAX_AGE, dated in the future, or its reset has passed: unknown now
           unknown  no sample, or an unreadable one
    Only 'n/a' and 'open' are usable. A sample is never promoted: a cap is cleared only by a FRESH sample below 100%.
    """
    out = {'state': 'unknown', 'pct': None, 'reset': None, 'ts': None}
    if pool != 'claude':
        out['state'] = 'n/a'
        return out
    try:
        row = (rows or {}).get(f'{pool}:{account}:session')
        ts, pct, reset = (meter(row, k) for k in ('ts', 'pct', 'reset'))
    except (KeyError, TypeError, ValueError):
        return out
    if not all(map(math.isfinite, (ts, pct, reset))) or not 0 <= pct <= 100:
        return out
    out.update(pct=pct, reset=reset, ts=ts)
    if not 0 <= now - ts <= SESSION_MAX_AGE or reset <= now:
        out['state'] = 'stale'
    else:
        out['state'] = 'capped' if pct >= 100 else 'open'
    return out


def pool_status(pool, accounts, rows, now, horizons=None):
    """Pool-level decision over bound accounts. Exhausted accounts contribute no burn and no budget.

    Two budgets (all %/h). weekly_budget is the THEORETICAL pace of every measured account. budget is the USABLE
    pace: only accounts that can take traffic now (a fresh session sample below 100% on Claude) contribute.
    burn is the pool's whole measured demand, because a capped account's traffic lands on the others. The
    decision, headroom and every consumer's default use the usable budget; weekly_budget/weekly_headroom are
    kept so the report can still show what the weekly windows would allow.

    decision: allow       pool burn within the usable pool budget
              over_budget  pool burn above the usable pool budget (wins over unknown: it is measured)
              unknown     an account is unmeasured and the measured rest is within budget (fail closed)
              exhausted   every bound account is at 100% weekly
              unavailable measured accounts exist but none can take traffic now (capped/stale/unknown session)
    Each account detail gains session (state), session_reset and usable.
    """
    horizons = load_horizons() if horizons is None else horizons
    details = []
    for a in accounts:
        d = account_detail(f'{pool}:{a}:weekly', rows, pool, now, horizons)
        s = session_state(pool, a, rows, now)
        d.update(session=s['state'], session_reset=s['reset'],
                 usable=d['decision'] in ('allow', 'over_budget') and s['state'] in USABLE_SESSION)
        # The pool verdict and the per-account row use the same pace-clause rate as the hourly table when a baseline
        # exists; accounts without one keep the one-hour anchor. The launcher gate (account_detail) is unchanged.
        if d['decision'] in ('allow', 'over_budget') and d.get('ts') is not None:
            roll = rolling_burn(f'{pool}:{a}:weekly', d['pct'], d['reset'], d['ts'])
            if roll is not None:
                d.update(burn=roll, headroom=d['budget'] - roll, burn_measure='rolling',
                         decision='over_budget' if roll > d['budget'] else 'allow')
        details.append((a, d))
    live = [d for _, d in details if d['decision'] in ('allow', 'over_budget')]
    usable = [d for d in live if d['usable']]
    burn = sum(d['burn'] for d in live)
    weekly_budget = sum(d['budget'] for d in live)
    budget = sum(d['budget'] for d in usable)
    if usable and burn > budget:
        decision = 'over_budget'
    elif any(d['decision'] == 'unknown' for _, d in details):
        decision = 'unknown'
    elif not live:
        decision = 'exhausted'
    elif not usable:
        decision = 'unavailable'
    else:
        decision = 'allow'
    return {'pool': pool, 'decision': decision, 'burn': burn, 'budget': budget, 'headroom': budget - burn,
            'weekly_budget': weekly_budget, 'weekly_headroom': weekly_budget - burn,
            'live': len(live), 'usable': len(usable),
            'horizon': active_horizon(pool, horizons, now), 'accounts': details}


def read_config(path):
    # Parse YAML with macOS Ruby's standard library; project no dependency on PyYAML.
    code = '''require "psych"; require "json"; d=Psych.safe_load(File.read(ARGV[0]), permitted_classes: [], aliases: false); puts JSON.generate(d)'''
    p = subprocess.run(['/usr/bin/ruby', '-e', code, path], capture_output=True, text=True, timeout=5, check=True)
    data = json.loads(p.stdout)
    if not isinstance(data, dict):
        raise ValueError('bad_config')
    return data


def model_pool(model):
    if model in ('claude-haiku-5-5', 'claude-sonnet-5-5', 'claude-opus-5-5', 'claude-fable-5-2'):
        return 'claude'
    if model in ('gpt-6-luna', 'gpt-6.1-terra', 'gpt-6.1-sol', 'gpt-6.2-sol', 'gpt-6-astra'):
        return 'codex'
    if model == 'grok-4.7':
        return 'grok'
    return None


def bindings(model, config):
    pool = model_pool(model)
    if not pool:
        raise ValueError('unknown_route')
    # force-model-prefix: true (ultra route, 25 Nov): a bare model id is served ONLY by credentials without a prefix, so a
    # prefixed credential (dev@scribie `ultra`) is not part of this pool. Without the flag a prefixed credential stays ambiguous.
    forced_prefix = bool(config.get('force-model-prefix'))
    # A configured alternate provider alias can make model->account ambiguous.
    for provider, entries in (config.get('oauth-model-alias') or {}).items():
        for entry in entries or []:
            if entry.get('alias') == model and provider != {'grok': 'xai'}.get(pool, pool):
                raise ValueError('ambiguous_route')
    for provider in config.get('openai-compatibility') or []:
        for entry in provider.get('models') or []:
            if (entry.get('alias') or entry.get('name')) == model:
                raise ValueError('ambiguous_route')
    auth = config.get('auth-dir')
    if not isinstance(auth, str) or not auth:
        raise ValueError('unknown_auth_dir')
    auth = os.path.expanduser(auth)
    if not os.path.isdir(auth):
        raise ValueError('unknown_auth_dir')
    accounts = set()
    for path in glob.glob(os.path.join(auth, '*.json')):
        with open(path) as f:
            d = json.load(f)
        typ = {'xai': 'grok'}.get(d.get('type'), d.get('type'))
        if typ != pool or d.get('disabled') is True:
            continue
        if d.get('disabled') not in (True, False, None):
            raise ValueError('unknown_binding')
        if d.get('prefix'):
            if forced_prefix:
                continue
            raise ValueError('unknown_binding')
        ident = d.get('account_id') if pool == 'codex' else d.get('email')
        if not isinstance(ident, str) or not ident.strip():
            raise ValueError('unknown_binding')
        accounts.add(ident)
    if not accounts:
        raise ValueError('no_accounts')
    return pool, sorted(accounts)


def account_labels(pool, config):
    """identity -> email for display (Codex accounts are bound by account_id, which reads poorly in a report)."""
    auth = config.get('auth-dir')
    if not isinstance(auth, str) or not auth:
        return {}
    labels = {}
    for path in glob.glob(os.path.join(os.path.expanduser(auth), '*.json')):
        try:
            with open(path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        typ = {'xai': 'grok'}.get(d.get('type'), d.get('type'))
        ident = d.get('account_id') if pool == 'codex' else d.get('email')
        if typ == pool and isinstance(ident, str) and isinstance(d.get('email'), str):
            labels[ident] = d['email']
    return labels


def read_latest(path):
    rows = {}
    with open(path) as f:
        for line in f:
            row = json.loads(line)
            if not isinstance(row, dict) or not isinstance(row.get('key'), str):
                raise ValueError('bad_snapshot')
            ts = row.get('ts')
            if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts):
                raise ValueError('bad_snapshot_timestamp')
            if row['key'] not in rows or ts >= rows[row['key']]['ts']:
                rows[row['key']] = row
    return rows


def shadow(model, role, override, config_path, snapshot_path, now):
    pool = model_pool(model) or 'unknown'
    exemption = role not in ('hd-planner', 'hd-builder', 'hd-proof-qa', 'pm-hotfix', 'pm-proof-qa', 'hd-investigator', 'superproofer')
    override_ok = bool(re.fullmatch(r'(?:P0:[0-9]+|rajiv:[0-9]+\.[0-9]+)', override or ''))
    prefix = f'proxy-burn: mode=shadow pool={pool} model={model}'
    if pool == 'claude':
        # No burn/pace gate on the Claude pool (Rajiv C0ALZJHGE49 thread 1795369444.132999, ts 1795372352.279289, 23 Nov:
        # "drop the P0 gate ... we don't have to track the burn rate for calude proxy accounts. just keep the cap to 4 and
        # switch accounts whenever it maxxes out"). The cap (CLAUDE_PROXY_CAP=4) and cliproxy-claude-switch.py --auto govern it.
        print(f'{prefix} decision=allow effective=claude_cap_only override={override if override_ok else "none"}')
        return 'allow'
    try:
        pool, accounts = bindings(model, read_config(config_path))
        rows = read_latest(snapshot_path)
        horizons = load_horizons()
        fmt = lambda v: 'unknown' if v is None else f'{v:.4f}'
        if pool == 'grok':
            for account in accounts:
                tag = hashlib.sha256(account.encode()).hexdigest()[:12]
                print(f'{prefix} account={tag} decision=unknown burn=unknown budget=unknown')
            status = {'decision': 'unknown', 'burn': 0.0, 'budget': 0.0, 'weekly_budget': 0.0, 'live': 0, 'usable': 0,
                      'horizon': None}
        else:
            status = pool_status(pool, accounts, rows, now, horizons)
            for account, d in status['accounts']:
                tag = hashlib.sha256(account.encode()).hexdigest()[:12]
                print(f'{prefix} account={tag} decision={d["decision"]} burn={fmt(d["burn"])} budget={fmt(d["budget"])} '
                      f'session={d["session"]} usable={"yes" if d["usable"] else "no"}')
        print(f'{prefix} pool_decision={status["decision"]} pool_burn={fmt(status["burn"])} pool_budget={fmt(status["budget"])} '
              f'pool_weekly_budget={fmt(status["weekly_budget"])} usable_accounts={status["usable"]}/{status["live"]} '
              f'horizon={"own-reset" if status["horizon"] is None else dt.datetime.fromtimestamp(status["horizon"], dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")}')
        verdict = 'allow' if status['decision'] == 'allow' else 'would_refuse'
        reason = 'exempt' if exemption else ('override' if override_ok else verdict)
        print(f'{prefix} decision={verdict} effective={reason} override={override if override_ok else "none"}')
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, subprocess.SubprocessError):
        print(f'{prefix} account=unknown decision=unknown burn=unknown budget=unknown')
        reason = 'exempt' if exemption else ('override' if override_ok else 'would_refuse')
        print(f'{prefix} decision=would_refuse effective={reason} override={override if override_ok else "none"}')
    return reason


def main():
    if len(sys.argv) != 4:
        return 2
    global LOG
    LOG = os.environ.get('PROXY_BURN_SNAPSHOT', LOG)
    decision = shadow(sys.argv[1], sys.argv[2], sys.argv[3], os.environ.get('PROXY_BURN_CONFIG', '/opt/homebrew/etc/cliproxyapi.conf'), LOG, time.time())
    return 75 if decision == 'would_refuse' else 0


if __name__ == '__main__':
    sys.exit(main())
