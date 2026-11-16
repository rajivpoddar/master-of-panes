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
    if p and (not all(isinstance(p.get(k), (int, float)) and not isinstance(p.get(k), bool) and math.isfinite(p[k]) for k in ('ts', 'pct', 'reset')) or not 0 <= p['pct'] <= 100 or abs(reset - p['reset']) >= 60):
        return None
    burn = eta = None
    if p and reset - p["reset"] < 60 and pct >= p["pct"]:  # same window, no reset between
        burn = (pct - p["pct"]) / ((NOW - p["ts"]) / 3600)
    return burn


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
        if not all(map(math.isfinite, (ts, pct, reset))) or not 0 <= pct <= 100 or not 0 <= now - ts <= 7200 or reset <= now:
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


def pool_status(pool, accounts, rows, now, horizons=None):
    """Pool-level decision over bound accounts. Exhausted accounts contribute no burn and no budget.

    decision: allow     pool burn within pool budget
              over_budget  pool burn above pool budget (wins over unknown: it is measured)
              unknown   an account is unmeasured and the measured rest is within budget (fail closed)
              exhausted every bound account is at 100%
    """
    horizons = load_horizons() if horizons is None else horizons
    details = [(a, account_detail(f'{pool}:{a}:weekly', rows, pool, now, horizons)) for a in accounts]
    live = [d for _, d in details if d['decision'] in ('allow', 'over_budget')]
    burn = sum(d['burn'] for d in live)
    budget = sum(d['budget'] for d in live)
    if live and burn > budget:
        decision = 'over_budget'
    elif any(d['decision'] == 'unknown' for _, d in details):
        decision = 'unknown'
    elif not live:
        decision = 'exhausted'
    else:
        decision = 'allow'
    return {'pool': pool, 'decision': decision, 'burn': burn, 'budget': budget, 'headroom': budget - burn,
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
    if model in ('gpt-6-luna', 'gpt-6.1-terra', 'gpt-6.1-sol', 'gpt-6-astra'):
        return 'codex'
    if model == 'grok-4.7':
        return 'grok'
    return None


def bindings(model, config):
    pool = model_pool(model)
    if not pool or config.get('force-model-prefix'):
        raise ValueError('unknown_route')
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
        if d.get('disabled') not in (True, False, None) or d.get('prefix'):
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
    try:
        pool, accounts = bindings(model, read_config(config_path))
        rows = read_latest(snapshot_path)
        horizons = load_horizons()
        fmt = lambda v: 'unknown' if v is None else f'{v:.4f}'
        if pool == 'grok':
            for account in accounts:
                tag = hashlib.sha256(account.encode()).hexdigest()[:12]
                print(f'{prefix} account={tag} decision=unknown burn=unknown budget=unknown')
            status = {'decision': 'unknown', 'burn': 0.0, 'budget': 0.0, 'horizon': None}
        else:
            status = pool_status(pool, accounts, rows, now, horizons)
            for account, d in status['accounts']:
                tag = hashlib.sha256(account.encode()).hexdigest()[:12]
                print(f'{prefix} account={tag} decision={d["decision"]} burn={fmt(d["burn"])} budget={fmt(d["budget"])}')
        print(f'{prefix} pool_decision={status["decision"]} pool_burn={fmt(status["burn"])} pool_budget={fmt(status["budget"])} '
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
