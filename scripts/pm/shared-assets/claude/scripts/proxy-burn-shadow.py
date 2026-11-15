#!/usr/bin/env python3
"""Shadow-only weekly provider burn decisions. Never authorizes or refuses a launch."""
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


def account_decision(key, rows, pool, now):
    latest = rows.get(key)
    if not latest:
        return 'unknown', None, None
    try:
        ts, pct, reset = (float(latest[k]) for k in ('ts', 'pct', 'reset'))
        if not all(map(math.isfinite, (ts, pct, reset))) or not 0 <= pct <= 100 or not 0 <= now - ts <= 7200 or reset <= now:
            return 'unknown', None, None
        if pct >= 100:
            return 'exhausted', None, 0.0
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
            return 'unknown', None, None
        hours = (reset - now) / 3600
        if pool == 'claude':
            hours = min(hours, 84)
        budget = (100 - pct) / hours
        return ('over_budget' if burn > budget else 'allow'), burn, budget
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OSError, json.JSONDecodeError):
        return 'unknown', None, None


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
        results = []
        for account in accounts:
            tag = hashlib.sha256(account.encode()).hexdigest()[:12]
            if pool == 'grok':
                decision, burn, budget = 'unknown', None, None
            else:
                decision, burn, budget = account_decision(f'{pool}:{account}:weekly', rows, pool, now)
            results.append(decision)
            fmt = lambda v: 'unknown' if v is None else f'{v:.4f}'
            print(f'{prefix} account={tag} decision={decision} burn={fmt(burn)} budget={fmt(budget)}')
        verdict = 'allow' if all(r == 'allow' for r in results) else 'would_refuse'
        reason = 'exempt' if exemption else ('override' if override_ok else verdict)
        print(f'{prefix} decision={verdict} effective={reason} override={override if override_ok else "none"}')
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, subprocess.SubprocessError):
        print(f'{prefix} account=unknown decision=unknown burn=unknown budget=unknown')
        print(f'{prefix} decision=would_refuse effective={"exempt" if exemption else ("override" if override_ok else "would_refuse")}')


def main():
    if len(sys.argv) != 4:
        return 2
    global LOG
    LOG = os.environ.get('PROXY_BURN_SNAPSHOT', LOG)
    shadow(sys.argv[1], sys.argv[2], sys.argv[3], os.environ.get('PROXY_BURN_CONFIG', '/opt/homebrew/etc/cliproxyapi.conf'), LOG, time.time())
    return 0


if __name__ == '__main__':
    sys.exit(main())
