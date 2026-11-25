#!/usr/bin/env python3
"""Direct DeepSeek bench gate: the ONE admission boundary for paid deepseek-direct/<model> lanes.

Why: Rajiv C0ALZJHGE49 thread 1795597777.761599, ts 1795597998.571509 wired direct DeepSeek ("use direct deepseek for this so that we don't hit
caps"); the $25 bench cap is ts 1795598139.238639. CTO REVISE of 248ae25b (ts 1795600640.261439): paid deepseek-direct/* launches AND continuations
are allowed only for the bench, with a budget stop, through one admission check that reads the CURRENT cap, stop flag and budget when it runs.

Everything here runs under ONE flock, so simultaneous starts cannot exceed the cap and a start/continuation can never see a cap captured earlier.

  admit      A start, a --dry-run start, or a --continue (the lane runner's re-admission before every resume). Refuses when
               scope   PROXY_AGENT_DEEPSEEK_DIRECT_SCOPE is not exactly bench:<run_id> of the state file (no token: refused; any other caller: refused),
               state   the bench state file is missing or invalid (fail closed),
               stop    the state file says stop (anything but a literal false),
               budget  spent_usd + the estimated in-flight spend of every running deepseek-direct lane (each at least the per-lane floor, which also
                       covers a lane being admitted now) would reach budget_usd,
               cap     the OTHER live deepseek-direct lanes already fill the cap. Cap = DEEPSEEK_DIRECT_PROXY_CAP in proxy-caps.env (re-read now),
                       else the env var of the caller, else 8. A lane that holds a reservation counts from the moment it is admitted (before its
                       claude process exists), which is what makes parallel starts atomic. cap 0 = route off.
  settle     Lane end: price the lane's PROXY_AGENT_USAGE line (input, cache-read and output tokens; from the stream when the line is absent), add it to
             spent_usd, free its reservation. A lane that dies without settling (killed launcher, or a pre-gate launcher) is settled by the next admit.
  status     JSON: state, cap, lanes with measured tokens and USD, exposure.
  init-state Create the state file; --adopt-running records the deepseek-direct lanes already running (baseline = what they have spent so far, which goes
             into spent_usd); --ended-jsonl seeds spent_usd with lanes that already ended (their PROXY_AGENT_USAGE, else their stream).
  set-stop   Flip the stop flag.

Exit codes: 0 admitted; 75 refused for the cap (capacity: queue/resume later is meaningful); 2 refused for anything else, or a gate failure (fail closed).

State dir: ~/.claude/state (PROXY_AGENT_DSD_STATE_DIR overrides, for hermetic runs):
  deepseek-direct-bench.json   {run_id, budget_usd, spent_usd, stop}  (the control file; edit stop by hand or with set-stop)
  deepseek-direct-lanes.json   reservations + per-lane measurement offsets + the settled history (written only under the lock)
  deepseek-direct-bench.log    one line per refusal / admission / settlement

DeepSeek published rates, https://api-docs.deepseek.com/quick_start/pricing (read 2026-11-25), USD per 1M tokens, "off-peak / peak":
  deepseek-flash (DeepSeek-V4.1-Flash-1017)  input cache hit 0.003 / 0.006   input cache miss 0.15 / 0.30   output 0.60 / 1.20
  deepseek-v4-pro (DeepSeek-V4-Pro-0813)     input cache hit 0.022 / 0.044   input cache miss 0.66 / 1.32   output 1.98 / 3.96
Off-peak is half of peak; peak is 01:00-04:00 and 06:00-10:00 UTC Mon-Fri excluding Chinese public holidays, which this script cannot know, so it
prices EVERY token at the peak rate: an upper bound, never an undercount. cache_creation has no DeepSeek price and is priced as a miss.

Pricing a lane: at its end, from its PROXY_AGENT_USAGE line (result events: input_tokens = cache misses, cache_read_tokens = hits, output_tokens).
While it runs only its stream exists, and there each model call reports its WHOLE prompt as input_tokens (the cache split arrives only in the final
result event; ended lanes showed ~98.8% of the prompt as cache reads) with a placeholder output count. A running lane is therefore priced as: prompt
tokens at the cache-hit fraction observed on this run's ended lanes (0 until 1M ended tokens exist, i.e. all misses), plus output at ~1 token per 3
characters of content, times STREAM_MARGIN (1.25). Measured on 5 ended lanes (25 Nov), the raw stream estimate was 0.86-0.97 of the exact usage-line
cost (prompt tokens 0.93-0.96, output 0.83-1.02 of exact), i.e. up to 14% LOW, which the margin covers; each live lane is also floored at
PROXY_AGENT_DSD_INFLIGHT_FLOOR_USD (0.25; whole-lane totals measured 0.10-0.25). The estimate only has to keep the budget stop ahead of the spend:
settlement replaces it with the exact usage line, and spent_usd then equals the exact figure (a lane's already-counted baseline is subtracted,
never added twice). Check against the account: the same 6 lanes priced 0.81 here against a DeepSeek balance drop of ~0.51 (95.65 -> 95.14, 2-decimal
API; the lanes straddled the 10:00 UTC peak edge), so the counted spend is an upper bound, not an estimate of the invoice.
"""
import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PREFIX = 'deepseek-direct/'
DEFAULT_CAP = 8
LOCK_WAIT_S = 30
# USD per 1M tokens at the PEAK rate (see the docstring): in = cache miss, cache_read = cache hit, out = output.
RATES = {
    'deepseek-direct/deepseek-flash': {'in': 0.30, 'cache_read': 0.006, 'out': 1.20},
    'deepseek-direct/deepseek-v4-pro': {'in': 1.32, 'cache_read': 0.044, 'out': 3.96},
}
UNKNOWN_RATE = RATES['deepseek-direct/deepseek-v4-pro']  # an id with no published rate is priced as the dearest one
EXACT = ('usage_line', 'init_usage_line')  # settled-history sources whose cache split is real (the others are stream estimates)
MIN_CACHE_SAMPLE = 1000000  # ended prompt tokens needed before the observed cache-hit fraction is trusted
MAX_CACHE_HIT = 0.99
STREAM_MARGIN = 1.25  # applied to the stream-only estimate of a RUNNING lane (measured up to 14% low, see the docstring); never to an exact usage line


class GateError(Exception):
    def __init__(self, reason, detail='', rc=2):
        super().__init__(reason)
        self.reason, self.detail, self.rc = reason, detail, rc


def now():
    return time.time()


def iso(t=None):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now() if t is None else t))


def state_dir():
    return Path(os.environ.get('PROXY_AGENT_DSD_STATE_DIR') or (Path.home() / '.claude' / 'state'))


def bench_path():
    return state_dir() / 'deepseek-direct-bench.json'


def ledger_path():
    return state_dir() / 'deepseek-direct-lanes.json'


def lock_path():
    return state_dir() / 'deepseek-direct-lanes.lock'


def log_path():
    return state_dir() / 'deepseek-direct-bench.log'


def log(line):
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        with open(log_path(), 'a') as fh:
            fh.write('%s %s\n' % (iso(), line))
    except OSError:
        pass


def identity(model):
    """The router-equivalent identity (proxy_agent_router_identity): lowercase, cut at the first ( or [, trimmed."""
    return re.split(r'[(\[]', (model or '').lower(), maxsplit=1)[0].strip()


def floor_usd():
    try:
        return max(float(os.environ.get('PROXY_AGENT_DSD_INFLIGHT_FLOOR_USD', '0.25')), 0.0)
    except ValueError:
        return 0.25


def grace_s():
    try:
        return max(float(os.environ.get('PROXY_AGENT_DSD_ADOPT_GRACE_S', '120')), 0.0)
    except ValueError:
        return 120.0


def current_cap():
    """Read NOW: the caps file first (the daily dial / PM edit it), then the caller's env, then 8. Never a value captured at launch time."""
    try:
        with open(state_dir() / 'proxy-caps.env') as fh:
            for raw in fh:
                key, _, val = raw.strip().partition('=')
                if key == 'DEEPSEEK_DIRECT_PROXY_CAP' and val.isdigit():
                    return int(val)
    except OSError:
        pass
    env = os.environ.get('DEEPSEEK_DIRECT_PROXY_CAP', '')
    return int(env) if env.isdigit() else DEFAULT_CAP


# ---- locking and files -----------------------------------------------------------------------------------------------------------------

class Lock:
    def __enter__(self):
        state_dir().mkdir(parents=True, exist_ok=True)
        self.fd = os.open(str(lock_path()), os.O_CREAT | os.O_RDWR, 0o600)
        deadline = now() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if now() > deadline:
                    os.close(self.fd)
                    raise GateError('lock_timeout', '(could not take the gate lock in %ds)' % LOCK_WAIT_S)
                time.sleep(0.02)

    def __exit__(self, *_exc):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as fh:
            json.dump(obj, fh, indent=2, sort_keys=True)
            fh.write('\n')
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_state():
    """The bench control file, validated; None when missing or invalid (the caller fails closed)."""
    try:
        with open(bench_path()) as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not isinstance(d.get('run_id'), str) or not re.fullmatch(r'[A-Za-z0-9._-]+', d['run_id']):
        return None
    for key in ('budget_usd', 'spent_usd'):
        if isinstance(d.get(key), bool) or not isinstance(d.get(key), (int, float)) or d[key] < 0:
            return None
    return d


def empty_ledger():
    return {'version': 1, 'lanes': {}, 'settled': []}


def load_ledger():
    try:
        with open(ledger_path()) as fh:
            d = json.load(fh)
        if isinstance(d, dict) and isinstance(d.get('lanes'), dict):
            d.setdefault('settled', [])
            return d
    except FileNotFoundError:
        return empty_ledger()
    except (OSError, ValueError):
        pass
    # Unreadable ledger: keep the evidence, start empty. Live lanes are re-adopted from the process table (counted in full, so over- not under-counted).
    try:
        os.replace(str(ledger_path()), str(ledger_path()) + '.corrupt-%d' % int(now()))
    except OSError:
        pass
    log('LEDGER_RESET reason=unreadable')
    return empty_ledger()


# ---- the process table -----------------------------------------------------------------------------------------------------------------

PS_LINE = re.compile(r'\s*(\d+)\s+(\d+)\s+(.*)$')
LANE_CMD = re.compile(r'(?:^|\s)\S*/claude\s+-p\s+--name\s+(\S+)\s+--model\s+(\S+)')


def ps_rows():
    if os.environ.get('PROXY_AGENT_STUB_POOL_LIVE', '') == 'unreadable':
        raise GateError('cap', '(live count unreadable: fail closed)', rc=75)
    try:
        out = subprocess.check_output(['/bin/ps', '-axo', 'pid=,ppid=,command='], encoding='utf-8', errors='replace', timeout=10)
    except (OSError, subprocess.SubprocessError):
        raise GateError('cap', '(live count unreadable: fail closed)', rc=75)
    rows = []
    for line in out.splitlines():
        m = PS_LINE.match(line)
        if m:
            rows.append((int(m.group(1)), int(m.group(2)), m.group(3)))
    return rows


def log_of(jsonl):
    return jsonl[:-len('.jsonl')] + '.log' if jsonl and jsonl.endswith('.jsonl') else None


def ps_lanes(rows):
    """name -> {model, pid, jsonl} for every running deepseek-direct claude process. The stream renderer beside it (same parent) names the jsonl."""
    stub = os.environ.get('PROXY_AGENT_STUB_POOL_LIVE', '')
    if stub.isdigit():  # tests (the launcher's own stub name): exactly this many phantom live lanes instead of the process table, never persisted
        return {'stub-live-%d' % (i + 1): {'model': PREFIX + 'deepseek-flash', 'pid': None, 'jsonl': None, 'stub': True} for i in range(int(stub))}
    streams = {}
    for _pid, ppid, cmd in rows:
        if 'proxy-agent-stream.py' in cmd:
            toks = cmd.split()
            if len(toks) >= 2:
                streams[ppid] = toks[-1]
    lanes = {}
    for pid, ppid, cmd in rows:
        m = LANE_CMD.search(cmd)
        if not m:
            continue
        model = identity(m.group(2))
        if not model.startswith(PREFIX):
            continue
        jsonl = streams.get(ppid)
        if jsonl and jsonl.endswith('.c'):
            jsonl = jsonl[:-2]
        lanes[m.group(1)] = {'model': model, 'pid': pid, 'jsonl': jsonl}
    return lanes


# ---- measuring a lane from its stream --------------------------------------------------------------------------------------------------

def new_acc():
    return {'off': 0, 'last_mid': None, 'a_in': 0, 'a_out': 0, 'a_cr': 0, 'a_cc': 0, 'r_in': 0, 'r_out': 0, 'r_cr': 0, 'r_cc': 0}


def new_lane(name, model, owner, pid, jsonl, log_file=None):
    return {'name': name, 'model': model, 'owner': owner, 'pid': pid, 'jsonl': jsonl, 'log': log_file or log_of(jsonl), 'reserved_at': iso(),
            'baseline_usd': 0.0, 'missing_since': None, 'acc': new_acc(), 'acc_c': new_acc()}


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def _content_chars(content):
    n = 0
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        kind = block.get('type')
        if kind == 'text':
            n += len(block.get('text') or '')
        elif kind == 'thinking':
            n += len(block.get('thinking') or '')
        elif kind == 'tool_use':
            n += len(block.get('name') or '') + len(json.dumps(block.get('input') or {}))
        else:
            n += len(json.dumps(block))
    return n


def scan_file(path, acc):
    """Fold the complete lines appended since acc['off'] into acc. Assistant events repeat per content block, so one message counts once; its
    usage carries the whole prompt as input_tokens and a placeholder output count, so output is the larger of the reported count and ~1 token per
    3 characters of content. result events carry the exact per-run totals (the same ones PROXY_AGENT_USAGE sums)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size < acc['off']:  # replaced file: measure it afresh
        acc.update(new_acc())
    pos = acc['off']
    try:
        with open(path, 'rb') as fh:
            fh.seek(pos)
            for line in fh:
                if not line.endswith(b'\n'):
                    break  # a partial last line: not consumed, read next time
                pos += len(line)
                head = line[:160]
                is_assistant = b'"type":"assistant"' in head or b'"type": "assistant"' in head
                # A result event does not start with its type (the keys come alphabetically, usage before type): look at the whole line, as PROXY_AGENT_USAGE does.
                is_result = (not is_assistant) and (b'"type":"result"' in line or b'"type": "result"' in line)
                if not (is_assistant or is_result):
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if is_assistant:
                    msg = d.get('message') if isinstance(d.get('message'), dict) else {}
                    mid = msg.get('id')
                    if mid is not None and mid == acc['last_mid']:
                        continue
                    acc['last_mid'] = mid
                    u = msg.get('usage') if isinstance(msg.get('usage'), dict) else {}
                    est = _content_chars(msg.get('content')) // 3
                    acc['a_in'] += _int(u.get('input_tokens'))
                    acc['a_cr'] += _int(u.get('cache_read_input_tokens'))
                    acc['a_cc'] += _int(u.get('cache_creation_input_tokens'))
                    acc['a_out'] += max(_int(u.get('output_tokens')), est)
                elif d.get('type') == 'result':
                    u = d.get('usage') if isinstance(d.get('usage'), dict) else {}
                    acc['r_in'] += _int(u.get('input_tokens'))
                    acc['r_out'] += _int(u.get('output_tokens'))
                    acc['r_cr'] += _int(u.get('cache_read_input_tokens'))
                    acc['r_cc'] += _int(u.get('cache_creation_input_tokens'))
    except OSError:
        return
    acc['off'] = pos


def measure(lane):
    jl = lane.get('jsonl')
    if not jl:
        return
    scan_file(jl, lane['acc'])
    if os.path.exists(jl + '.c'):  # a continuation streams to .c and is appended to the main file only when it ends
        scan_file(jl + '.c', lane['acc_c'])
    else:
        lane['acc_c'] = new_acc()


def totals(lane):
    a, c = lane['acc'], lane['acc_c']
    return {k: a[k] + c[k] for k in ('a_in', 'a_out', 'a_cr', 'a_cc', 'r_in', 'r_out', 'r_cr', 'r_cc')}


def rate_of(model):
    return RATES.get(identity(model), UNKNOWN_RATE)


def cost_usd(model, tok):
    """Exact pricing of a usage line: in = cache misses, cr = cache reads, cc = cache creation (priced as a miss), out = output."""
    r = rate_of(model)
    return ((tok['in'] + tok['cc']) * r['in'] + tok['cr'] * r['cache_read'] + tok['out'] * r['out']) / 1e6


def cache_hit_fraction(ledger):
    """The share of ended prompt tokens that were cache reads on this run, from the exact usage lines only; 0 (all misses) until there is a sample."""
    cr = total = 0
    for s in ledger.get('settled') or []:
        if s.get('source') in EXACT and isinstance(s.get('tokens'), dict):
            t = s['tokens']
            cr += t.get('cr', 0)
            total += t.get('in', 0) + t.get('cr', 0) + t.get('cc', 0)
    return min(cr / total, MAX_CACHE_HIT) if total >= MIN_CACHE_SAMPLE else 0.0


def lane_usd(lane, frac):
    """What a lane has spent so far: the stream estimate (see the docstring) or the exact result-event total, whichever is larger."""
    measure(lane)
    t = totals(lane)
    r = rate_of(lane['model'])
    if t['a_cr'] > 0:  # the stream carries the split itself
        prompt = (t['a_in'] + t['a_cc']) * r['in'] + t['a_cr'] * r['cache_read']
    else:
        prompt = (t['a_in'] + t['a_cc']) * (r['in'] * (1 - frac) + r['cache_read'] * frac)
    stream = (prompt + t['a_out'] * r['out']) / 1e6 * STREAM_MARGIN
    exact = cost_usd(lane['model'], {'in': t['r_in'], 'out': t['r_out'], 'cr': t['r_cr'], 'cc': t['r_cc']})
    return max(stream, exact)


USAGE_RE = re.compile(r'PROXY_AGENT_USAGE model=(\S+) results=\d+ input_tokens=(\d+) output_tokens=(\d+) cache_read_tokens=(\d+) cache_creation_tokens=(\d+)')


def usage_from_log(path):
    """The last PROXY_AGENT_USAGE line of a lane log, from its tail only (a lane log can be enormous)."""
    if not path:
        return None
    try:
        with open(path, 'rb') as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(fh.tell() - 262144, 0))
            tail = fh.read().decode('utf-8', 'replace')
    except OSError:
        return None
    found = USAGE_RE.findall(tail)
    if not found:
        return None
    _model, i, o, cr, cc = found[-1]
    return {'in': int(i), 'out': int(o), 'cr': int(cr), 'cc': int(cc)}


# ---- ledger operations (call under the lock) -------------------------------------------------------------------------------------------

def settle_lane(ledger, state, name, lane, source, usage=None):
    """Price the lane and add what is not already in spent_usd (its baseline) to spent_usd; drop the reservation. Returns the USD added."""
    if lane.get('owner') == 'dry':
        ledger['lanes'].pop(name, None)
        return 0.0
    usage = usage or usage_from_log(lane.get('log'))
    if usage:
        total, tok, src = cost_usd(lane['model'], usage), usage, ('init_usage_line' if source == 'init' else 'usage_line')
    else:
        total = lane_usd(lane, cache_hit_fraction(ledger))
        t = totals(lane)
        tok, src = {'in': t['a_in'], 'out': t['a_out'], 'cr': t['a_cr'], 'cc': t['a_cc']}, ('init_stream' if source == 'init' else source)
    delta = max(total - float(lane.get('baseline_usd') or 0.0), 0.0)
    state['spent_usd'] = round(float(state['spent_usd']) + delta, 6)
    ledger['lanes'].pop(name, None)
    ledger['settled'] = (ledger.get('settled') or [])[-199:] + [{'name': name, 'model': lane['model'], 'usd': round(delta, 6), 'total_usd': round(total, 6),
                                                                  'tokens': tok, 'source': src, 'at': iso()}]
    log('SETTLED name=%s model=%s source=%s usd=%.6f total_usd=%.6f in=%d out=%d cache_read=%d cache_creation=%d spent_usd=%.6f'
        % (name, lane['model'], src, delta, total, tok['in'], tok['out'], tok['cr'], tok['cc'], state['spent_usd']))
    return delta


def refresh(ledger, state, rows):
    """Settle the lanes that have ended, adopt running deepseek-direct lanes nobody reserved (launched before this gate), and return the live lanes."""
    pidcmd = {pid: cmd for pid, _pp, cmd in rows}
    running = ps_lanes(rows)
    t = now()
    alive = {}
    for name, lane in list(ledger['lanes'].items()):
        owner = lane.get('owner')
        seen = running.get(name)
        if owner == 'ps':
            live = seen is not None
        elif owner == 'dry':
            live = lane.get('pid') in pidcmd and 'deepseek-direct-gate' in pidcmd[lane['pid']]
        else:  # a launcher reservation lives as long as its launcher (the claude process comes and goes between continuations)
            live = (lane.get('pid') in pidcmd and 'proxy-agent.sh' in pidcmd[lane['pid']]) or seen is not None
        if seen is not None:
            lane['missing_since'] = None
            if not lane.get('jsonl') and seen.get('jsonl'):
                lane['jsonl'] = seen['jsonl']
                lane['log'] = lane.get('log') or log_of(seen['jsonl'])
        if live:
            alive[name] = lane
            continue
        if owner == 'ps':  # a pre-gate runner's claude process is briefly absent between continuations (and its launcher prints PROXY_AGENT_USAGE a moment after the
            if lane.get('missing_since') is None:  # process ends): grace before settling
                lane['missing_since'] = t
            if t - lane['missing_since'] < grace_s():
                alive[name] = lane
                continue
        settle_lane(ledger, state, name, lane, 'reconcile')
    for name, seen in running.items():
        if name in alive:
            continue
        lane = new_lane(name, seen['model'], 'ps', seen.get('pid'), seen.get('jsonl'))
        if not seen.get('stub'):
            ledger['lanes'][name] = lane
        alive[name] = lane
    return alive


def exposure(ledger, state, alive, extra_floor):
    """spent_usd + the estimated in-flight spend: each live lane's spend since its baseline, at least the floor; extra_floor is a lane being admitted."""
    flo, frac = floor_usd(), cache_hit_fraction(ledger)
    in_flight = 0.0
    for lane in alive.values():
        in_flight += max(lane_usd(lane, frac) - float(lane.get('baseline_usd') or 0.0), flo)
    in_flight += flo if extra_floor else 0.0
    return float(state['spent_usd']), in_flight


def save(state, state_before, ledger):
    if state is not None and state != state_before:
        write_json(bench_path(), state)
    write_json(ledger_path(), ledger)


# ---- commands --------------------------------------------------------------------------------------------------------------------------

def cmd_admit(a):
    model = identity(a.model)
    name = a.name
    kind = 'dry-run' if a.dry_run else ('continue' if a.cont else 'start')
    base = 'model=%s name=%s kind=%s' % (model, name, kind)

    def refuse(reason, detail='', rc=2):
        line = 'PROXY_AGENT_REFUSED reason=deepseek_direct_%s %s %s' % (reason, base, detail)
        print(line.strip())
        log(line.strip())
        return rc

    if not model.startswith(PREFIX):
        return refuse('model', '(not a deepseek-direct id)')
    token = os.environ.get('PROXY_AGENT_DEEPSEEK_DIRECT_SCOPE', '')
    if not token:
        return refuse('scope', '(PROXY_AGENT_DEEPSEEK_DIRECT_SCOPE=bench:<run-id> is required: paid deepseek-direct lanes are for the bench only)')
    m = re.fullmatch(r'bench:([A-Za-z0-9._-]+)', token)
    if not m:
        return refuse('scope', '(scope token must be bench:<run-id>)')
    try:
        with Lock():
            state = load_state()
            if state is None:
                return refuse('state', '(%s missing or invalid: fail closed)' % bench_path())
            if m.group(1) != state['run_id']:
                return refuse('scope', '(token run-id %s is not the active bench run %s)' % (m.group(1), state['run_id']))
            before = json.loads(json.dumps(state))
            ledger = load_ledger()
            try:
                alive = refresh(ledger, state, ps_rows())
            except GateError as exc:
                save(state, before, ledger)
                return refuse(exc.reason, exc.detail, exc.rc)
            try:
                if state.get('stop') is not False:
                    return refuse('stop', '(stop flag is set in %s)' % bench_path())
                existing = alive.get(name)
                if existing is not None and not a.cont and not a.dry_run and existing.get('pid') != a.pid:
                    return refuse('duplicate', '(a live lane already holds this name)')
                spent, in_flight = exposure(ledger, state, alive, extra_floor=existing is None)
                budget = float(state['budget_usd'])
                if spent + in_flight >= budget:
                    return refuse('budget', '(spent_usd=%.4f + in_flight_usd=%.4f >= budget_usd=%.2f)' % (spent, in_flight, budget))
                others = len([n for n in alive if n != name])
                cap = current_cap()
                if others >= cap:
                    return refuse('cap', '(%d live >= %d)' % (others, cap), rc=75)
                if existing is None or (existing.get('owner') == 'ps' and not a.dry_run):
                    lane = new_lane(name, model, 'dry' if a.dry_run else 'launcher', os.getpid() if a.dry_run else a.pid, a.jsonl or None, a.log or None)
                    if existing is not None:  # a pre-gate lane being re-admitted: keep what is already counted for it
                        lane.update({k: existing[k] for k in ('baseline_usd', 'acc', 'acc_c') if k in existing})
                        lane['jsonl'] = lane['jsonl'] or existing.get('jsonl')
                        lane['log'] = lane['log'] or existing.get('log')
                    ledger['lanes'][name] = lane
                elif not a.dry_run:
                    existing.update({k: v for k, v in (('pid', a.pid), ('jsonl', a.jsonl), ('log', a.log)) if v})
                    existing['owner'] = 'launcher'
                line = ('PROXY_AGENT_DSD_GATE admitted %s run=%s others=%d cap=%d spent_usd=%.4f in_flight_usd=%.4f budget_usd=%.2f'
                        % (base, state['run_id'], others, cap, spent, in_flight, budget))
                print(line)
                log(line)
            finally:
                save(state, before, ledger)
    except GateError as exc:
        return refuse(exc.reason, exc.detail, exc.rc)
    if a.dry_run and a.hold > 0:  # a dry run holds its slot for --hold seconds so concurrent dry runs see each other, then frees it
        time.sleep(a.hold)
    if a.dry_run:
        with Lock():
            ledger = load_ledger()
            mine = ledger['lanes'].get(name)
            if mine is not None and mine.get('owner') == 'dry' and mine.get('pid') == os.getpid():
                ledger['lanes'].pop(name)
                write_json(ledger_path(), ledger)
    return 0


def cmd_settle(a):
    with Lock():
        state = load_state()
        ledger = load_ledger()
        lane = ledger['lanes'].get(a.name)
        if state is None:
            ledger['lanes'].pop(a.name, None)
            write_json(ledger_path(), ledger)
            log('SETTLE_SKIPPED name=%s reason=state_missing' % a.name)
            return 0
        before = json.loads(json.dumps(state))
        if lane is None:  # a lane that never reserved (pre-gate launcher): price its usage line alone, or its stream
            lane = new_lane(a.name, identity(a.model), 'launcher', None, a.jsonl or None, a.log or None)
        else:
            lane['log'] = a.log or lane.get('log')
            lane['jsonl'] = lane.get('jsonl') or a.jsonl or None
        delta = settle_lane(ledger, state, a.name, lane, 'stream')
        save(state, before, ledger)
    print('PROXY_AGENT_DSD_GATE settled name=%s usd=%.6f spent_usd=%.6f budget_usd=%.2f run=%s'
          % (a.name, delta, state['spent_usd'], state['budget_usd'], state['run_id']))
    return 0


def lane_report(name, lane, frac):
    t = totals(lane)
    return {'name': name, 'model': lane['model'], 'owner': lane.get('owner'), 'pid': lane.get('pid'), 'jsonl': lane.get('jsonl'),
            'prompt_tokens_stream': t['a_in'] + t['a_cr'] + t['a_cc'], 'output_tokens_est': t['a_out'],
            'result_tokens': {'in': t['r_in'], 'cache_read': t['r_cr'], 'cache_creation': t['r_cc'], 'out': t['r_out']},
            'usd_so_far': round(lane_usd(lane, frac), 6), 'baseline_usd': round(float(lane.get('baseline_usd') or 0.0), 6)}


def cmd_status(_a):
    with Lock():
        state = load_state()
        ledger = load_ledger()
        rows = ps_rows()
        before = json.loads(json.dumps(state)) if state else None
        alive, spent, in_flight = {}, 0.0, 0.0
        if state is not None:
            alive = refresh(ledger, state, rows)
            spent, in_flight = exposure(ledger, state, alive, extra_floor=False)
            save(state, before, ledger)
        frac = cache_hit_fraction(ledger)
        lanes = [lane_report(n, ln, frac) for n, ln in sorted(alive.items())]
    print(json.dumps({'state': state, 'cap': current_cap(), 'live_lanes': len(alive), 'in_flight_usd': round(in_flight, 6), 'exposure_usd': round(spent + in_flight, 6),
                      'cache_hit_fraction_observed': round(frac, 4), 'lanes': lanes, 'settled_recent': (ledger.get('settled') or [])[-8:]}, indent=2, sort_keys=True))
    return 0


def lane_from_ended(jsonl):
    """A lane that already ended, rebuilt from its files: name and model from the LAUNCH line of its log."""
    log_file = log_of(jsonl)
    name, model = Path(jsonl).stem, PREFIX + 'deepseek-flash'
    try:
        with open(log_file, 'rb') as fh:
            head = fh.read(16384).decode('utf-8', 'replace')
        m = re.search(r'LAUNCH name=(\S+) .*?model_id=(\S+)', head)
        if m:
            name, model = m.group(1), identity(m.group(2))
    except (OSError, TypeError):
        pass
    return name, new_lane(name, model, 'ended', None, jsonl, log_file)


def cmd_init(a):
    with Lock():
        if load_state() is not None and not a.force:
            print('REFUSED: %s already exists (use --force to replace it)' % bench_path())
            return 2
        state = {'run_id': a.run_id, 'budget_usd': a.budget_usd, 'spent_usd': round(a.spent_usd, 6), 'stop': False}
        ledger = empty_ledger() if a.force else load_ledger()
        seeded, adopted, skipped = [], [], []
        running_now = ps_lanes(ps_rows())
        ended = [lane_from_ended(j) for j in a.ended_jsonl]
        # Exact usage lines first: the cache-hit fraction they establish prices the stream-only lanes after them.
        ended.sort(key=lambda nl: usage_from_log(nl[1].get('log')) is None)
        for name, lane in ended:  # lanes that ended before the gate existed: their exact usage (else their stream) is already spent
            if name in running_now:  # still running: adopted below with a baseline, never seeded twice
                skipped.append(name)
                continue
            usd = settle_lane(ledger, state, name, lane, 'init')
            seeded.append({'name': name, 'usd': round(usd, 6), 'source': ledger['settled'][-1]['source'], 'tokens': ledger['settled'][-1]['tokens']})
        frac = cache_hit_fraction(ledger)
        if a.adopt_running:
            for name, seen in sorted(running_now.items()):
                if seen.get('stub'):
                    continue
                lane = new_lane(name, seen['model'], 'ps', seen.get('pid'), seen.get('jsonl'))
                usd = lane_usd(lane, frac)
                lane['baseline_usd'] = round(usd, 6)  # already inside spent_usd: only what it spends after this is added at settle
                ledger['lanes'][name] = lane
                state['spent_usd'] = round(state['spent_usd'] + usd, 6)
                adopted.append(lane_report(name, lane, frac))
        write_json(bench_path(), state)
        write_json(ledger_path(), ledger)
        log('INIT run_id=%s budget_usd=%s spent_usd=%s ended=%d adopted=%d' % (a.run_id, a.budget_usd, state['spent_usd'], len(seeded), len(adopted)))
    print(json.dumps({'state': state, 'ended_seeded': seeded, 'skipped_still_running': skipped, 'adopted_running': adopted, 'cache_hit_fraction_observed': round(frac, 4)}, indent=2, sort_keys=True))
    return 0


def cmd_set_stop(a):
    with Lock():
        state = load_state()
        if state is None:
            print('REFUSED: %s missing or invalid' % bench_path())
            return 2
        state['stop'] = a.value == 'true'
        write_json(bench_path(), state)
        log('SET_STOP stop=%s' % state['stop'])
    print(json.dumps(state, sort_keys=True))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = p.add_subparsers(dest='cmd', required=True)
    ad = sub.add_parser('admit')
    ad.add_argument('--name', required=True)
    ad.add_argument('--model', required=True)
    ad.add_argument('--pid', type=int, default=0)
    ad.add_argument('--jsonl', default='')
    ad.add_argument('--log', default='')
    ad.add_argument('--continue', dest='cont', action='store_true')
    ad.add_argument('--dry-run', action='store_true')
    ad.add_argument('--hold', type=float, default=0.0)
    st = sub.add_parser('settle')
    st.add_argument('--name', required=True)
    st.add_argument('--model', default='')
    st.add_argument('--log', default='')
    st.add_argument('--jsonl', default='')
    sub.add_parser('status')
    ini = sub.add_parser('init-state')
    ini.add_argument('--run-id', required=True)
    ini.add_argument('--budget-usd', type=float, required=True)
    ini.add_argument('--spent-usd', type=float, default=0.0)
    ini.add_argument('--adopt-running', action='store_true')
    ini.add_argument('--ended-jsonl', action='append', default=[])
    ini.add_argument('--force', action='store_true')
    ss = sub.add_parser('set-stop')
    ss.add_argument('value', choices=('true', 'false'))
    a = p.parse_args(argv)
    try:
        return {'admit': cmd_admit, 'settle': cmd_settle, 'status': cmd_status, 'init-state': cmd_init, 'set-stop': cmd_set_stop}[a.cmd](a)
    except GateError as exc:
        print('PROXY_AGENT_REFUSED reason=deepseek_direct_%s %s' % (exc.reason, exc.detail))
        return exc.rc
    except Exception as exc:  # fail closed: an admission that cannot be decided is a refusal; a settlement never blocks a lane's end
        line = 'PROXY_AGENT_REFUSED reason=deepseek_direct_gate_error (%s: %s)' % (type(exc).__name__, exc)
        print(line)
        log(line)
        return 0 if a.cmd == 'settle' else 2


if __name__ == '__main__':
    sys.exit(main())
