#!/usr/bin/env python3
"""Direct DeepSeek bench gate: the ONE admission boundary for paid deepseek-direct/<model> lanes.

Why: Rajiv C0ALZJHGE49 thread 1795597777.761599, ts 1795597998.571509 wired direct DeepSeek ("use direct deepseek for this so that we don't hit
caps"); the $25 bench cap is ts 1795598139.238639. CTO REVISE of 248ae25b (ts 1795600640.261439): paid launches AND continuations are bench-only, behind
one admission check that reads the CURRENT cap, stop flag and budget. CTO REVISE of a5c85804 (ts 1795603176.516159): budgeting only the spend observed so
far lets a lane admitted at $24.70 overshoot $25 before the gate runs again, and a killed lane with unreadable usage must never settle at $0.

Everything here runs under ONE flock, so simultaneous starts cannot exceed the cap or the budget.

Every paid SEGMENT (a start, or one continuation) reserves an enforceable maximum at admission:
    worst   the dearest single request a lane can make: a full context window of cache-miss prompt plus the max output, at the peak DeepSeek rate
    grant   min(segment max, headroom - worst): the real USD this segment may spend before Claude Code stops it
    reserve grant + worst: what admission holds against the budget until the segment's usage is known
  headroom = budget - spent_usd - held - the other lanes' upper bounds - what this lane has already spent. grant < the minimum segment refuses (budget).
  So at 24.70 spent and no live lanes (headroom 0.30 < one worst request), nothing is admitted; at 24.00 a lane is admitted with a grant of ~0.65, not 1.00.
  The grant is ENFORCED, not estimated: the launcher passes the claude CLI --max-budget-usd <cum + grant * k>. Claude Code stops a run at the first request that
  crosses it (verified with the installed claude 2.1.332 against a local stub provider: budget 0.50 with requests costing 0.26 stopped after 2 requests,
  subtype error_max_budget_usd), so a segment overshoots its grant by at most one request, which is the 'worst' held in the reserve. Claude Code's cost is
  CUMULATIVE across --resume (measured: 0.26 then 0.52 for a resumed segment) and unknown model ids are priced at a flat Anthropic-class fallback. k converts real
  DeepSeek dollars to Claude Code dollars: 0.75 x the smallest ratio of the LOWEST fallback table measured (4 in / 20 out / 0.20 cache read / 5 cache write per 1M;
  an earlier real lane priced at the higher 5 in / 25 out / 0.50 cache read) to the DeepSeek peak rate, per token class: flash 10.0, v4-pro 2.27. Settlement checks it against every exact lane
  (Claude Code total_cost_usd vs the DeepSeek cost of the same usage); a lane below k records cost_basis_violation and admission refuses until it is cleared.

  admit      A start, a --dry-run start, or a --continue (the lane runner's re-admission before every resume; --grant-file is mandatory for the real ones). Refuses when
               scope   PROXY_AGENT_DEEPSEEK_DIRECT_SCOPE is not exactly bench:<run_id> of the state file (no token: refused; any other caller: refused),
               state   the bench state file is missing or invalid (fail closed),
               stop    the state file says stop (anything but a literal false),
               no_grant_channel  a real start/continuation without --grant-file (a runner baked before the segment cap existed cannot apply it),
               cost_basis  a settled lane showed Claude Code pricing below k (the enforcement assumption failed),
               budget  no segment of at least the minimum fits in the headroom (above),
               segment_unreconciled  a continuation of a lane whose previous segment ended with no result event (its spend is unknown, so its cumulative cost,
                       and therefore a correct cap for the next segment, is unknown),
               cap     the OTHER live deepseek-direct lanes already fill the cap. Cap = DEEPSEEK_DIRECT_PROXY_CAP in proxy-caps.env (re-read now),
                       else the env var of the caller, else 8. A lane that holds a reservation counts from the moment it is admitted. cap 0 = route off.
             On success it prints PROXY_AGENT_DSD_GRANT ... max_budget_usd=<N> and writes N to --grant-file; the launcher fails closed if it cannot read it.
  started    The runner calls it right before it spawns claude. A lane that never got there (a later refusal, a tmux failure) spent nothing and is RELEASED when
             it ends; only a started lane can be held.
  settle     Lane end. Exact when every segment has a result event (or the PROXY_AGENT_USAGE line covers every segment): spent_usd += the priced usage and the
             reservation is freed. Otherwise (killed lane, usage unavailable) the known part is added and the unknown segments' RESERVATIONS STAY HELD against the
             budget, never settled at 0 and never at an estimate, until reconcile. A segment that ended on the budget stop (subtype error_max_budget_usd) is exact
             for the requests it reports, but its usage OMITS the request that crossed the cap (measured; its total_cost_usd includes it, and error_max_turns reports
             every request): one worst request per such segment is held until reconcile.
  reconcile  Releases a held lane with evidence: --usd <spent> (e.g. from the provider balance), or --charge-reservation (charge the reserve in full); --all
             applies --charge-reservation to every held lane; --clear-cost-basis clears a cost_basis_violation after the table was re-measured.
  status     JSON: state, cap, lanes with upper bounds, held lanes, exposure.
  init-state Create the state file; --adopt-running records the deepseek-direct lanes already running (unenforced: counted at their stream estimate plus a full
             segment); --ended-jsonl seeds spent_usd with lanes that already ended (exact usage, else held).
  set-stop   Flip the stop flag.

Exit codes: 0 admitted; 75 refused for the cap (capacity: queue/resume later is meaningful); 2 refused for anything else, or a gate failure (fail closed).

State dir: ~/.claude/state (PROXY_AGENT_DSD_STATE_DIR overrides, for hermetic runs):
  deepseek-direct-bench.json   {run_id, budget_usd, spent_usd, stop}  (the control file; edit stop by hand or with set-stop)
  deepseek-direct-lanes.json   live reservations, held lanes, measurement offsets, the settled history (written only under the lock)
  deepseek-direct-bench.log    one line per refusal / admission / settlement / hold

DeepSeek published rates, https://api-docs.deepseek.com/quick_start/pricing (read 2026-11-25), USD per 1M tokens, "off-peak / peak":
  deepseek-flash (DeepSeek-V4.1-Flash-1017)  input cache hit 0.003 / 0.006   input cache miss 0.15 / 0.30   output 0.60 / 1.20
  deepseek-v4-pro (DeepSeek-V4-Pro-0813)     input cache hit 0.022 / 0.044   input cache miss 0.66 / 1.32   output 1.98 / 3.96
Every token is priced at the peak rate (peak is 01:00-04:00 and 06:00-10:00 UTC Mon-Fri excluding Chinese holidays, which this script cannot know). cache_creation
has no DeepSeek price and is priced as a miss. The context window is 1048576 and the max output 32000 (the launcher's own limits for these ids).

Exact pricing of a lane: from the result events' usage (per process, so the sum over segments is the lane's real total) or the PROXY_AGENT_USAGE line. Measured
against the account (25 Nov): 6 lanes priced 0.81 here vs a balance drop of ~0.51 (2-decimal API; they straddled the 10:00 UTC peak edge). A RUNNING lane that
is not ours to cap (adopted at init, or started by a pre-gate launcher) has only its stream: prompt tokens at the observed cache-hit fraction plus output at ~1
token per 3 characters, x STREAM_MARGIN (1.25: the raw estimate measured 0.86-0.97 of exact), plus a whole unenforced segment allowance.
"""
import argparse
import fcntl
import json
import math
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
SEGMENT_MAX_USD = {'deepseek-direct/deepseek-flash': 1.00, 'deepseek-direct/deepseek-v4-pro': 3.00}  # real USD a segment may spend before Claude Code stops it
DEFAULT_SEGMENT_MAX_USD = 3.00
MIN_SEGMENT_USD = 0.05  # a segment smaller than this is refused: it could not finish a normal turn
WINDOW_TOKENS = 1048576  # context_window of both ids (GET api.deepseek.com/models, 25 Nov)
MAX_OUTPUT_TOKENS = 32000  # CLAUDE_CODE_MAX_OUTPUT_TOKENS the launcher sets for these ids
# Claude Code's flat fallback price for an unknown model id, USD per 1M: the LOWEST table measured (claude 2.1.332, local stub provider, no network).
CC_FALLBACK_LOW = {'in': 4.0, 'out': 20.0, 'cache_read': 0.20, 'cache_creation': 5.0}
CC_SAFETY = 0.75
EXACT = ('usage_line', 'init_usage_line', 'stream_results', 'init_stream_results')  # settled-history sources whose cache split is real (the others are stream estimates)
MIN_CACHE_SAMPLE = 1000000  # ended prompt tokens needed before the observed cache-hit fraction is trusted
MAX_CACHE_HIT = 0.99
STREAM_MARGIN = 1.25  # applied to the stream-only estimate of a lane we do not cap (measured up to 14% low); never to an exact figure


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


def env_float(name, default):
    try:
        return max(float(os.environ[name]), 0.0)
    except (KeyError, ValueError):
        return default


def grace_s():
    return env_float('PROXY_AGENT_DSD_ADOPT_GRACE_S', 120.0)


def floor4(x):
    return math.floor(x * 10000 + 1e-9) / 10000


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


def rate_of(model):
    return RATES.get(identity(model), UNKNOWN_RATE)


def segment_max(model):
    return env_float('PROXY_AGENT_DSD_SEGMENT_MAX_USD', SEGMENT_MAX_USD.get(identity(model), DEFAULT_SEGMENT_MAX_USD))


def min_segment():
    return env_float('PROXY_AGENT_DSD_MIN_SEGMENT_USD', MIN_SEGMENT_USD)


def worst_request_usd(model):
    """The dearest single request: a full window of cache-miss prompt plus the max output at the peak rate (cache creation is priced as a miss)."""
    r = rate_of(model)
    return (WINDOW_TOKENS * r['in'] + MAX_OUTPUT_TOKENS * r['out']) / 1e6


def cc_per_usd(model):
    """Claude Code dollars per real DeepSeek dollar that can be assumed for ANY token mix: the smallest per-class ratio, with a safety factor."""
    r, c = rate_of(model), CC_FALLBACK_LOW
    return CC_SAFETY * min(c['in'] / r['in'], c['out'] / r['out'], c['cache_read'] / r['cache_read'], c['cache_creation'] / r['in'])


def ps_allowance(model):
    """A lane we cannot cap (adopted, or started by a pre-gate launcher) may still spend a whole segment plus one request."""
    return segment_max(model) + worst_request_usd(model)


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
    return {'version': 2, 'lanes': {}, 'held': {}, 'settled': []}


def load_ledger():
    try:
        with open(ledger_path()) as fh:
            d = json.load(fh)
        if isinstance(d, dict) and isinstance(d.get('lanes'), dict):
            d.setdefault('settled', [])
            d.setdefault('held', {})
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
    return {'off': 0, 'last_mid': None, 'a_in': 0, 'a_out': 0, 'a_cr': 0, 'a_cc': 0, 'r_in': 0, 'r_out': 0, 'r_cr': 0, 'r_cc': 0, 'r_n': 0, 'r_ccost': 0.0, 'r_capped': 0}


def new_lane(name, model, owner, pid, jsonl, log_file=None):
    return {'name': name, 'model': model, 'owner': owner, 'pid': pid, 'jsonl': jsonl, 'log': log_file or log_of(jsonl), 'reserved_at': iso(),
            'missing_since': None, 'reserves': [], 'budgets': [], 'acc': new_acc(), 'acc_c': new_acc()}


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
    3 characters of content. result events carry the exact per-process usage (the same ones PROXY_AGENT_USAGE sums) and Claude Code's CUMULATIVE
    total_cost_usd for the session."""
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
                    if not any(k in u for k in ('input_tokens', 'output_tokens')):  # a result with no usage object proves nothing: its spend is unreadable, not zero
                        continue
                    acc['r_in'] += _int(u.get('input_tokens'))
                    acc['r_out'] += _int(u.get('output_tokens'))
                    acc['r_cr'] += _int(u.get('cache_read_input_tokens'))
                    acc['r_cc'] += _int(u.get('cache_creation_input_tokens'))
                    acc['r_n'] = acc.get('r_n', 0) + 1
                    if d.get('subtype') == 'error_max_budget_usd':  # measured: this result's usage omits the request that crossed the cap, its total_cost_usd includes it
                        acc['r_capped'] = acc.get('r_capped', 0) + 1
                    cost = d.get('total_cost_usd')
                    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost > 0:
                        acc['r_ccost'] = max(acc.get('r_ccost', 0.0), float(cost))
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
    t = {k: a.get(k, 0) + c.get(k, 0) for k in ('a_in', 'a_out', 'a_cr', 'a_cc', 'r_in', 'r_out', 'r_cr', 'r_cc', 'r_n', 'r_capped')}
    t['r_ccost'] = max(a.get('r_ccost', 0.0), c.get('r_ccost', 0.0))
    return t


def cost_usd(model, tok):
    """Exact pricing of a usage: in = cache misses, cr = cache reads, cc = cache creation (priced as a miss), out = output."""
    r = rate_of(model)
    return ((tok['in'] + tok['cc']) * r['in'] + tok['cr'] * r['cache_read'] + tok['out'] * r['out']) / 1e6


def cc_low_cost(tok):
    """Claude Code's cost for a usage at the lowest fallback table: a LOWER estimate of its cumulative cost, so a cap derived from it errs toward stopping early."""
    c = CC_FALLBACK_LOW
    return (tok['in'] * c['in'] + tok['cc'] * c['cache_creation'] + tok['cr'] * c['cache_read'] + tok['out'] * c['out']) / 1e6


def lane_results(lane):
    """What the lane's result events prove: (exact real USD, number of result events, Claude Code's cumulative cost, token totals)."""
    measure(lane)
    t = totals(lane)
    tok = {'in': t['r_in'], 'out': t['r_out'], 'cr': t['r_cr'], 'cc': t['r_cc']}
    return cost_usd(lane['model'], tok), t['r_n'], t['r_ccost'], tok


def cache_hit_fraction(ledger):
    """The share of ended prompt tokens that were cache reads on this run, from the exact figures only; 0 (all misses) until there is a sample."""
    cr = total = 0
    for s in ledger.get('settled') or []:
        if s.get('source') in EXACT and isinstance(s.get('tokens'), dict):
            t = s['tokens']
            cr += t.get('cr', 0)
            total += t.get('in', 0) + t.get('cr', 0) + t.get('cc', 0)
    return min(cr / total, MAX_CACHE_HIT) if total >= MIN_CACHE_SAMPLE else 0.0


def stream_estimate(lane, frac):
    """What a lane has spent so far according to its stream alone (an estimate, see the docstring), or the exact result total if that is larger."""
    measure(lane)
    t = totals(lane)
    r = rate_of(lane['model'])
    if t['a_cr'] > 0:  # the stream carries the split itself
        prompt = (t['a_in'] + t['a_cc']) * r['in'] + t['a_cr'] * r['cache_read']
    else:
        prompt = (t['a_in'] + t['a_cc']) * (r['in'] * (1 - frac) + r['cache_read'] * frac)
    stream = (prompt + t['a_out'] * r['out']) / 1e6 * STREAM_MARGIN
    return max(stream, lane_results(lane)[0])


USAGE_RE = re.compile(r'PROXY_AGENT_USAGE model=(\S+) results=(\d+) input_tokens=(\d+) output_tokens=(\d+) cache_read_tokens=(\d+) cache_creation_tokens=(\d+)')


def usage_from_log(path):
    """The last PROXY_AGENT_USAGE line of a lane log, from its tail only (a lane log can be enormous). 'unavailable' lines do not match."""
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
    _model, n, i, o, cr, cc = found[-1]
    return {'in': int(i), 'out': int(o), 'cr': int(cr), 'cc': int(cc), 'n': int(n)}


# ---- ledger operations (call under the lock) -------------------------------------------------------------------------------------------

def lane_ub(lane, frac):
    """An upper bound on what the lane can have spent (real USD): its exact spend plus the reservation of every segment that has no result event yet. A lane
    we do not cap (no reservations) is its stream estimate plus a whole unenforced segment."""
    reserves = lane.get('reserves') or []
    exact, n, _cc, _tok = lane_results(lane)
    if lane.get('owner') == 'ps' or not reserves:
        return max(exact, stream_estimate(lane, frac)) + ps_allowance(lane['model'])
    unknown = max(len(reserves) - n, 0)
    return exact + sum(sorted(reserves, reverse=True)[:unknown]) + totals(lane)['r_capped'] * worst_request_usd(lane['model'])


def held_total(ledger):
    return sum(float(h.get('usd', 0.0)) for h in (ledger.get('held') or {}).values())


def close_lane(ledger, state, name, lane, source):
    """End of a lane. The exact part (result events, or the PROXY_AGENT_USAGE line when it covers more segments) is added to spent_usd and the
    reservation is freed; every segment WITHOUT a readable result keeps its reservation HELD until reconcile: a killed lane is never settled at 0 or at an
    estimate. Returns (exact USD added, USD held)."""
    if lane.get('owner') == 'dry':
        ledger['lanes'].pop(name, None)
        return 0.0, 0.0
    exact_s, n_s, cc_cost, tok_s = lane_results(lane)
    usage = usage_from_log(lane.get('log'))
    # A lane admitted by an earlier gate version has no 'reserves' key and always ran: only a lane THIS version reserved for can be proven never started.
    if lane.get('owner') == 'launcher' and 'reserves' in lane and not lane.get('started') and n_s == 0 and not usage:  # the runner never reached its claude spawn: nothing was spent, nothing to hold
        ledger['lanes'].pop(name, None)
        log('RELEASED name=%s model=%s reason=never_started reserve_usd=%.6f' % (name, lane['model'], sum(lane.get('reserves') or [])))
        return 0.0, 0.0
    best_cost, best_n, best_tok, src = exact_s, n_s, tok_s, 'stream_results'
    if usage and (usage['n'] > n_s or (usage['n'] == n_s and cost_usd(lane['model'], usage) > exact_s)):
        best_cost, best_n, best_tok, src = cost_usd(lane['model'], usage), usage['n'], {k: usage[k] for k in ('in', 'out', 'cr', 'cc')}, 'usage_line'
    if source == 'init':
        src = 'init_' + src
    reserves = list(lane.get('reserves') or [])
    if lane.get('owner') == 'ps' or not reserves:
        reserves = [ps_allowance(lane['model'])] * max(best_n, 1)
    unknown = max(len(reserves) - best_n, 0)
    capped = totals(lane)['r_capped']  # segments that ended on the --max-budget-usd stop: their usage omits the request that crossed it
    held = round(sum(sorted(reserves, reverse=True)[:unknown]) + capped * worst_request_usd(lane['model']), 6)
    state['spent_usd'] = round(float(state['spent_usd']) + best_cost, 6)
    ledger['lanes'].pop(name, None)
    if held > 0:
        h = ledger['held'].get(name) or {'usd': 0.0, 'known_usd': 0.0, 'unknown_segments': 0}
        ledger['held'][name] = {'usd': round(h['usd'] + held, 6), 'known_usd': round(h['known_usd'] + best_cost, 6), 'unknown_segments': h['unknown_segments'] + unknown,
                                'capped_segments': h.get('capped_segments', 0) + capped, 'model': lane['model'], 'since': h.get('since') or iso(),
                                'reason': ('no_usage_for_%d_segment(s)' % unknown if unknown else '') + (' ' if unknown and capped else '') + ('%d_segment(s)_ended_on_the_budget_stop:_the_crossing_request_is_unreported' % capped if capped else '')}
    ledger['settled'] = (ledger.get('settled') or [])[-199:] + [{'name': name, 'model': lane['model'], 'usd': round(best_cost, 6), 'held_usd': held, 'results': best_n,
                                                                  'segments': len(reserves), 'tokens': best_tok, 'source': src, 'at': iso()}]
    log('%s name=%s model=%s source=%s results=%d/%d usd=%.6f held_usd=%.6f in=%d out=%d cache_read=%d cache_creation=%d spent_usd=%.6f held_total_usd=%.6f'
        % ('HELD' if held > 0 else 'SETTLED', name, lane['model'], src, best_n, len(reserves), best_cost, held, best_tok['in'], best_tok['out'], best_tok['cr'],
           best_tok['cc'], state['spent_usd'], held_total(ledger)))
    # The enforcement assumption, checked on every exact lane: Claude Code's cumulative cost must be at least k x the real DeepSeek cost of the same usage.
    if held == 0 and n_s == best_n and exact_s > 0 and cc_cost > 0:
        ratio, k = cc_cost / exact_s, cc_per_usd(lane['model'])
        ledger['min_cc_ratio'] = round(min(float(ledger.get('min_cc_ratio', ratio)), ratio), 4)
        if ratio < k:
            ledger['cost_basis_violation'] = {'name': name, 'model': lane['model'], 'ratio': round(ratio, 4), 'k': round(k, 4), 'at': iso()}
            log('COST_BASIS_VIOLATION name=%s ratio=%.4f k=%.4f: admission refused until reconcile --clear-cost-basis' % (name, ratio, k))
    return best_cost, held


def refresh(ledger, state, rows):
    """Close the lanes that have ended, adopt running deepseek-direct lanes nobody reserved (launched before this gate), and return the live lanes."""
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
            if lane.get('missing_since') is None:  # process ends): grace before closing
                lane['missing_since'] = t
            if t - lane['missing_since'] < grace_s():
                alive[name] = lane
                continue
        close_lane(ledger, state, name, lane, 'reconcile')
    for name, seen in running.items():
        if name in alive:
            continue
        lane = new_lane(name, seen['model'], 'ps', seen.get('pid'), seen.get('jsonl'))
        if not seen.get('stub'):
            ledger['lanes'][name] = lane
        alive[name] = lane
    return alive


def exposure(ledger, state, alive, skip=None):
    """(spent_usd, held USD, the live lanes' upper bounds): everything admission counts against the budget."""
    frac = cache_hit_fraction(ledger)
    reserved = sum(lane_ub(ln, frac) for n, ln in alive.items() if n != skip)
    return float(state['spent_usd']), held_total(ledger), reserved


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
    if a.grant_file:  # a stale grant must never be read by the launcher
        try:
            os.unlink(a.grant_file)
        except OSError:
            pass

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
    grant_line = None
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
                if ledger.get('cost_basis_violation'):
                    return refuse('cost_basis', '(%s: Claude Code priced a lane below the assumed floor; re-measure, then reconcile --clear-cost-basis)'
                                  % json.dumps(ledger['cost_basis_violation'], sort_keys=True))
                if not a.dry_run and not a.grant_file:  # a runner that cannot receive the segment cap cannot enforce one: no cap, no segment (covers runners baked before this version)
                    return refuse('no_grant_channel', '(a start or continuation must pass --grant-file so the launcher can apply --max-budget-usd; this runner predates it: relaunch the lane)')
                existing = alive.get(name)
                if existing is not None and not a.cont and not a.dry_run and existing.get('pid') != a.pid:
                    return refuse('duplicate', '(a live lane already holds this name)')
                # What this lane has already spent, and Claude Code's cumulative cost for its session (a resumed segment's budget is cumulative).
                committed, cum_cc, prior = 0.0, 0.0, existing
                if existing is not None and existing.get('owner') == 'ps' and not a.cont and not a.dry_run:  # an adopted lane being re-admitted: what it showed so far is spent
                    committed = stream_estimate(existing, cache_hit_fraction(ledger))
                if a.cont:
                    if prior is None:  # the ledger lost the lane: rebuild what it proves from its stream
                        prior = new_lane(name, model, 'launcher', a.pid, a.jsonl or None, a.log or None)
                    exact, n, ccc, tok = lane_results(prior)
                    reserves = prior.get('reserves') or []
                    if n == 0 or n < len(reserves):
                        return refuse('segment_unreconciled', '(%d of %d previous segment(s) have a result event: their spend and cumulative Claude Code cost are unknown, '
                                      'so a cap for the next segment cannot be set; the lane ends and its reservation stays held until reconcile)' % (n, max(len(reserves), 1)))
                    committed, cum_cc = exact + totals(prior)['r_capped'] * worst_request_usd(model), (ccc if ccc > 0 else cc_low_cost(tok))
                spent, held, others = exposure(ledger, state, alive, skip=name)
                budget = float(state['budget_usd'])
                worst, seg_max, floor_seg = worst_request_usd(model), segment_max(model), min_segment()
                headroom = budget - spent - held - others - committed
                grant = floor4(min(seg_max, headroom - worst))
                if grant < floor_seg - 1e-9:
                    return refuse('budget', '(headroom %.4f = budget %.2f - spent %.4f - held %.4f - other lanes %.4f - this lane %.4f; a segment needs one worst-case '
                                  'request %.4f + at least %.2f)' % (headroom, budget, spent, held, others, committed, worst, floor_seg))
                others_n = len([n for n in alive if n != name])
                cap = current_cap()
                if others_n >= cap:
                    return refuse('cap', '(%d live >= %d)' % (others_n, cap), rc=75)
                reserve = round(grant + worst, 6)
                k = cc_per_usd(model)
                cc_budget = floor4(cum_cc + grant * k)
                if existing is None or (existing.get('owner') == 'ps' and not a.dry_run) or (not a.cont and not a.dry_run):
                    lane = new_lane(name, model, 'dry' if a.dry_run else 'launcher', os.getpid() if a.dry_run else a.pid, a.jsonl or None, a.log or None)
                    if a.cont and prior is not None:
                        lane['acc'], lane['acc_c'] = prior['acc'], prior['acc_c']
                        lane['reserves'] = [0.0] * max(lane_results(prior)[1], 0)
                    elif existing is not None and existing.get('owner') == 'ps':  # a pre-gate lane being re-admitted keeps what its stream already showed
                        lane['acc'], lane['acc_c'] = existing['acc'], existing['acc_c']
                        lane['jsonl'] = lane['jsonl'] or existing.get('jsonl')
                        lane['log'] = lane['log'] or existing.get('log')
                    lane['reserves'] = list(lane['reserves']) + [reserve]
                    lane['budgets'] = [cc_budget]
                    lane['started'] = bool(a.cont)
                    ledger['lanes'][name] = lane
                elif not a.dry_run:
                    existing.update({k2: v for k2, v in (('pid', a.pid), ('jsonl', a.jsonl), ('log', a.log)) if v})
                    existing['owner'] = 'launcher'
                    existing['reserves'] = list(existing.get('reserves') or []) + [reserve]
                    existing['budgets'] = list(existing.get('budgets') or []) + [cc_budget]
                    existing['started'] = True  # the runner spawns claude right after a continuation admission
                line = ('PROXY_AGENT_DSD_GATE admitted %s run=%s others=%d cap=%d spent_usd=%.4f held_usd=%.4f other_lanes_usd=%.4f budget_usd=%.2f'
                        % (base, state['run_id'], others_n, cap, spent, held, others, budget))
                grant_line = ('PROXY_AGENT_DSD_GRANT %s segment=%d grant_usd=%.4f reserve_usd=%.4f worst_request_usd=%.4f cc_per_usd=%.4f cum_cc_usd=%.4f max_budget_usd=%.4f'
                              % (base, len((ledger['lanes'].get(name) or {}).get('reserves') or [reserve]), grant, reserve, worst, k, cum_cc, cc_budget))
                print(line)
                print(grant_line)
                log(line)
                log(grant_line)
                if a.grant_file:
                    write_json_text(Path(a.grant_file), '%.4f\n' % cc_budget)
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


def write_json_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=str(path.parent))
    with os.fdopen(fd, 'w') as fh:
        fh.write(text)
    os.replace(tmp, str(path))


def cmd_started(a):
    """The runner calls this right before it spawns claude: from here a lane that dies without a readable result keeps its reservation held."""
    with Lock():
        ledger = load_ledger()
        lane = ledger['lanes'].get(a.name)
        if lane is None:
            print('PROXY_AGENT_REFUSED reason=deepseek_direct_no_reservation name=%s (the gate holds no reservation for this lane: fail closed)' % a.name)
            return 2
        lane['started'] = True
        write_json(ledger_path(), ledger)
    log('STARTED name=%s' % a.name)
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
        if lane is None:  # a lane that never reserved (pre-gate launcher): price what its usage line / stream prove, hold the rest
            lane = new_lane(a.name, identity(a.model), 'launcher', None, a.jsonl or None, a.log or None)
            lane['started'] = True
        else:
            lane['log'] = a.log or lane.get('log')
            lane['jsonl'] = lane.get('jsonl') or a.jsonl or None
        added, held = close_lane(ledger, state, a.name, lane, 'stream')
        save(state, before, ledger)
    print('PROXY_AGENT_DSD_GATE %s name=%s usd=%.6f held_usd=%.6f spent_usd=%.6f held_total_usd=%.6f budget_usd=%.2f run=%s'
          % ('held' if held > 0 else 'settled', a.name, added, held, state['spent_usd'], held_total(ledger), state['budget_usd'], state['run_id']))
    return 0


def lane_report(name, lane, frac):
    exact, n, ccc, tok = lane_results(lane)
    t = totals(lane)
    return {'name': name, 'model': lane['model'], 'owner': lane.get('owner'), 'pid': lane.get('pid'), 'jsonl': lane.get('jsonl'),
            'segments': len(lane.get('reserves') or []), 'segments_with_result': n, 'reserves_usd': lane.get('reserves') or [],
            'exact_usd': round(exact, 6), 'cc_cumulative_usd': round(ccc, 6), 'upper_bound_usd': round(lane_ub(lane, frac), 6),
            'prompt_tokens_stream': t['a_in'] + t['a_cr'] + t['a_cc'], 'output_tokens_est': t['a_out']}


def cmd_status(_a):
    with Lock():
        state = load_state()
        ledger = load_ledger()
        rows = ps_rows()
        before = json.loads(json.dumps(state)) if state else None
        alive, spent, held, reserved = {}, 0.0, 0.0, 0.0
        if state is not None:
            alive = refresh(ledger, state, rows)
            spent, held, reserved = exposure(ledger, state, alive)
            save(state, before, ledger)
        frac = cache_hit_fraction(ledger)
        lanes = [lane_report(n, ln, frac) for n, ln in sorted(alive.items())]
    print(json.dumps({'state': state, 'cap': current_cap(), 'live_lanes': len(alive), 'lanes_upper_bound_usd': round(reserved, 6), 'held_usd': round(held, 6),
                      'exposure_usd': round(spent + held + reserved, 6), 'held': ledger.get('held') or {}, 'cost_basis_violation': ledger.get('cost_basis_violation'),
                      'min_cc_ratio_observed': ledger.get('min_cc_ratio'), 'cache_hit_fraction_observed': round(frac, 4), 'lanes': lanes,
                      'settled_recent': (ledger.get('settled') or [])[-8:]}, indent=2, sort_keys=True))
    return 0


def cmd_reconcile(a):
    with Lock():
        state = load_state()
        if state is None:
            print('REFUSED: %s missing or invalid' % bench_path())
            return 2
        before = json.loads(json.dumps(state))
        ledger = load_ledger()
        if a.clear_cost_basis:
            ledger.pop('cost_basis_violation', None)
            log('COST_BASIS_CLEARED')
        if a.all and not a.charge_reservation:
            print('REFUSED: --all needs --charge-reservation')
            return 2
        names = sorted(ledger['held']) if a.all else ([a.name] if a.name else [])
        if not names and not a.clear_cost_basis:
            print('REFUSED: name a held lane (--name), --all --charge-reservation, or --clear-cost-basis')
            return 2
        if a.name and a.usd is None and not a.charge_reservation and not a.clear_cost_basis:
            print('REFUSED: --name needs --usd <spent> or --charge-reservation')
            return 2
        done = []
        for n in names:
            h = ledger['held'].get(n)
            if h is None:
                print('REFUSED: %s is not held' % n)
                return 2
            charge = float(h['usd']) if a.charge_reservation else float(a.usd)
            if charge < 0:
                print('REFUSED: negative amount')
                return 2
            state['spent_usd'] = round(float(state['spent_usd']) + charge, 6)
            ledger['held'].pop(n)
            done.append({'name': n, 'charged_usd': round(charge, 6), 'held_was_usd': h['usd']})
            log('RECONCILED name=%s charged_usd=%.6f held_was_usd=%.6f basis=%s evidence=%s spent_usd=%.6f'
                % (n, charge, float(h['usd']), 'reservation' if a.charge_reservation else 'operator', (a.evidence or '-').replace(' ', '_')[:200], state['spent_usd']))
        save(state, before, ledger)
    print(json.dumps({'reconciled': done, 'spent_usd': state['spent_usd'], 'held_usd': round(held_total(ledger), 6)}, indent=2, sort_keys=True))
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
    lane = new_lane(name, model, 'ended', None, jsonl, log_file)
    lane['started'] = True
    return name, lane


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
        for name, lane in ended:  # lanes that ended before the gate existed: exact usage is spent, an unreadable one stays held
            if name in running_now:  # still running: adopted below, never seeded twice
                skipped.append(name)
                continue
            usd, held = close_lane(ledger, state, name, lane, 'init')
            seeded.append({'name': name, 'usd': round(usd, 6), 'held_usd': round(held, 6), 'source': ledger['settled'][-1]['source'] if ledger['settled'] else None})
        frac = cache_hit_fraction(ledger)
        if a.adopt_running:
            for name, seen in sorted(running_now.items()):
                if seen.get('stub'):
                    continue
                lane = new_lane(name, seen['model'], 'ps', seen.get('pid'), seen.get('jsonl'))
                ledger['lanes'][name] = lane
                adopted.append(lane_report(name, lane, frac))
        write_json(bench_path(), state)
        write_json(ledger_path(), ledger)
        log('INIT run_id=%s budget_usd=%s spent_usd=%s held_usd=%s ended=%d adopted=%d' % (a.run_id, a.budget_usd, state['spent_usd'], round(held_total(ledger), 6), len(seeded), len(adopted)))
    print(json.dumps({'state': state, 'ended_seeded': seeded, 'skipped_still_running': skipped, 'adopted_running': adopted, 'held': ledger['held'],
                      'cache_hit_fraction_observed': round(frac, 4)}, indent=2, sort_keys=True))
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
    ad.add_argument('--grant-file', default='')
    ad.add_argument('--continue', dest='cont', action='store_true')
    ad.add_argument('--dry-run', action='store_true')
    ad.add_argument('--hold', type=float, default=0.0)
    st = sub.add_parser('settle')
    st.add_argument('--name', required=True)
    st.add_argument('--model', default='')
    st.add_argument('--log', default='')
    st.add_argument('--jsonl', default='')
    sd = sub.add_parser('started')
    sd.add_argument('--name', required=True)
    sub.add_parser('status')
    rc = sub.add_parser('reconcile')
    rc.add_argument('--name', default='')
    rc.add_argument('--usd', type=float, default=None)
    rc.add_argument('--charge-reservation', action='store_true')
    rc.add_argument('--all', action='store_true')
    rc.add_argument('--clear-cost-basis', action='store_true')
    rc.add_argument('--evidence', default='')
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
        return {'admit': cmd_admit, 'started': cmd_started, 'settle': cmd_settle, 'status': cmd_status, 'reconcile': cmd_reconcile, 'init-state': cmd_init, 'set-stop': cmd_set_stop}[a.cmd](a)
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
