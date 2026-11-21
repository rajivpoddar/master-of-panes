#!/usr/bin/env python3
"""STUB proof for remote-QA capture-on-miss (CTO C0ALZJHGE49 ts 1793378243.332849). Script-level tooling test, not an app suite.

Applies the DEPLOYED overlay (remote-qa-modal.py overlay_proxy) to the real modal/shared/llm_proxy_server.py and drives it
through FastAPI TestClient. Fakes: R2 (in-memory, ETag CAS), Modal Dict (in-memory, survives "restart"), upstream (httpx
MockTransport). No network, no provider, no Modal, no paid call.
Run: uv run --no-project --with fastapi==0.115.12 --with httpx==0.25.2 --with boto3==1.35.99 python ~/.claude/scripts/remote-qa-onmiss-stub.test.py
"""
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import types
from pathlib import Path

HELPER = Path(os.environ.get('QA_MODAL_HELPER', Path.home() / '.claude/scripts/remote-qa-modal.py'))
APP = Path(os.environ.get('APP', Path.home() / 'Downloads/projects/heydonna-app'))
FAILS = []


def check(name, ok, detail=''):
    print(('PASS ' if ok else 'FAIL ') + name + (' :: ' + detail if detail else ''))
    if not ok:
        FAILS.append(name)


# ---------------- fakes ----------------
class Err(Exception):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.response = {'Error': {'Code': code}, 'ResponseMetadata': {'HTTPStatusCode': status}}


class Headers(dict):
    def add_header(self, k, v):
        self[k] = v


class Events:
    def __init__(self):
        self.h = {}

    def register_first(self, ev, fn, **_):
        self.h.setdefault(ev, []).insert(0, fn)

    register = register_first

    def unregister(self, ev, fn=None, **_):
        if fn in self.h.get(ev, []):
            self.h[ev].remove(fn)


class R2:
    objects = {}  # key -> (bytes, etag, metadata)   durable across "restarts"
    lock = threading.Lock()
    drop_ack_once = set()  # ledger keys whose NEXT successful put is applied but its ACK is lost
    deny_get = set()  # keys whose reads fail with AccessDenied (a lookup error, never a miss)


class Body:
    def __init__(self, b):
        self.b = b

    def read(self):
        return self.b


class FakeS3:
    def __init__(self):
        self.meta = types.SimpleNamespace(events=Events())

    def get_object(self, Bucket, Key):
        with R2.lock:
            if Key in R2.deny_get:
                raise Err('AccessDenied', 403)
            if Key not in R2.objects:
                raise Err('NoSuchKey', 404)
            b, etag, md = R2.objects[Key]
        return {'Body': Body(b), 'ETag': etag, 'Metadata': dict(md), 'ContentType': 'application/octet-stream'}

    def put_object(self, Bucket, Key, Body, ContentType=None, Metadata=None):
        req = types.SimpleNamespace(headers=Headers())
        for fn in list(self.meta.events.h.get('before-sign.s3.PutObject', [])):
            fn(request=req)
        with R2.lock:
            cur = R2.objects.get(Key)
            if req.headers.get('If-None-Match') == '*' and cur is not None:
                raise Err('PreconditionFailed', 412)
            if 'If-Match' in req.headers and (cur is None or cur[1] != req.headers['If-Match']):
                raise Err('PreconditionFailed', 412)
            etag = '"%d"' % (len(R2.objects) * 1000 + len(Body) + (int(cur[1].strip('"')) + 1 if cur else 0))
            R2.objects[Key] = (bytes(Body), etag, dict(Metadata or {}))
            if Key in R2.drop_ack_once:
                R2.drop_ack_once.discard(Key)
                raise ConnectionResetError('ACK lost after the write was applied')
        return {'ETag': etag}

    def list_objects_v2(self, **kw):
        with R2.lock:
            return {'Contents': [{'Key': k} for k in sorted(R2.objects) if k.startswith(kw.get('Prefix', ''))], 'IsTruncated': False}


class FakeDict:
    stores = {}  # name -> {key: record}  durable across "restarts"

    def __init__(self, name):
        self.d = FakeDict.stores.setdefault(name, {})

    @classmethod
    def from_name(cls, name, create_if_missing=False, **_):
        if name not in cls.stores and not create_if_missing:
            raise KeyError(name)
        return cls(name)

    def get(self, k, default=None):
        return self.d.get(k, default)

    def put(self, k, v, skip_if_exists=False):
        self.d[k] = v
        return True


UPSTREAM = {'calls': [], 'at_send': {}, 'keys': ()}


def upstream(request):
    import httpx, time
    body = json.loads(request.content)
    text = body['contents'][0]['parts'][0]['text']
    UPSTREAM['calls'].append(text)
    if 'DROP' in text:
        raise httpx.ReadError('connection reset after upstream accepted the request')
    if 'SLOW' in text:
        time.sleep(2)
    import re as _re
    m = _re.search(r'USAGE=(\d+),(\d+)', text)
    usage = [int(m[1]), int(m[2])] if m else [1_000_000, 60_000]
    out = {'candidates': [{'content': {'role': 'model', 'parts': [{'text': 'PAYLOAD-SENTINEL ' + text}]}, 'finishReason': 'STOP'}],
           'usageMetadata': {'promptTokenCount': usage[0], 'candidatesTokenCount': usage[1], 'totalTokenCount': sum(usage)}}
    if 'EXACT' in text:  # exact-mode proof: provider response id, and the ledger state at the instant of the provider send
        out['responseId'] = 'respExact' + str(len(UPSTREAM['calls']))
        UPSTREAM['at_send'][text] = tuple(
            round(sum(a['charged_bound_usd'] for a in json.loads(R2.objects[k][0]).get('attempts', {}).values()), 6) if k in R2.objects else None
            for k in UPSTREAM['keys'])
    return httpx.Response(200, json=out, headers={'content-type': 'application/json'})


# ---------------- harness ----------------
def load_helper():
    spec = importlib.util.spec_from_file_location('qa_modal_helper', HELPER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


H = load_helper()
WORK = Path(tempfile.mkdtemp(prefix='onmiss-stub-'))
for f in ('llm_proxy_server.py', 'llm_fixture_store.py', 'llm_proxy_config.py', 'sanitize_error_body.py'):
    shutil.copy(APP / 'modal/shared' / f, WORK / f)
sys.path.insert(0, str(WORK))
sys.modules['modal'] = types.SimpleNamespace(Dict=FakeDict)
import boto3  # noqa: E402
boto3.client = lambda *a, **k: FakeS3()
import httpx  # noqa: E402
_orig_init = httpx.AsyncClient.__init__


def _init(self, *a, **k):
    k['transport'] = httpx.MockTransport(upstream)
    _orig_init(self, *a, **k)


httpx.AsyncClient.__init__ = _init
from fastapi.testclient import TestClient  # noqa: E402

SOURCE = (WORK / 'llm_proxy_server.py').read_text()
BOX = 'rtbl-0123456789abcdef-123456'
HEAD1 = subprocess.run(['git', '-C', str(APP), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
_n = [0]


def ids_for(box, host, head=None):
    real = socket.gethostname
    socket.gethostname = lambda: host
    try:
        ids = H.onmiss_identity(APP, box)
    finally:
        socket.gethostname = real
    if head:  # a different QA attempt (head) on the same physical box
        ids = dict(ids, head=head, attempt_key='qa-capture-ledgers/onmiss/attempt-' + __import__('hashlib').sha256(('head:' + head).encode()).hexdigest()[:16] + '.json')
    return ids


def boot(ids, ci=False, box=BOX):
    """A fresh proxy process (module instance) built from the deployed overlay; durable fakes persist."""
    text = H.overlay_proxy(SOURCE, box, False, False, 2.0, ids)
    _n[0] += 1
    path = WORK / ('llm_proxy_server_%d.py' % _n[0])
    path.write_text(text)
    os.environ.update({'LLM_PROXY_ENABLED': 'true', 'STRICT_FIXTURES': 'true', 'LLM_PROXY_FIXTURE_CAPTURE': 'false',
                       'MODAL_ENV_SUFFIX': '-qa-' + H.identity(box), 'LLM_PROXY_CAPTURE_RUN_ID': 'remote-qa-' + H.identity(box),
                       'R2_ACCOUNT_ID': 'stub', 'R2_ACCESS_KEY_ID': 'stub', 'R2_SECRET_ACCESS_KEY': 'stub', 'CI': 'true' if ci else ''})
    spec = importlib.util.spec_from_file_location('llm_proxy_server_%d' % _n[0], path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    if ids:
        m._QA_ONMISS_WAIT_S = 3
    return m, TestClient(m._build_app())


def call(client, text, model='gemini-3.8-flash', max_out=65536, usage=None, provider='vertex'):
    body = {'contents': [{'role': 'user', 'parts': [{'text': text}]}], 'generationConfig': {'maxOutputTokens': max_out}}
    if usage:
        body['contents'][0]['parts'][0]['text'] += ' USAGE=%d,%d' % tuple(usage)  # stub upstream reads this marker
    if provider == 'vertex':
        hdr = {'X-Upstream-Base': 'https://aiplatform.googleapis.com', 'Authorization': 'Bearer stub'}
        path = '/v1beta1/projects/p/locations/global/publishers/google/models/%s:generateContent' % model
    else:
        hdr = {'X-Upstream-Base': 'https://generativelanguage.googleapis.com', 'x-goog-api-key': 'stub'}
        path = '/v1beta/models/%s:generateContent' % model
    return client.post(path, content=json.dumps(body), headers=hdr)


def ledger(key):
    return json.loads(R2.objects[key][0]) if key in R2.objects else {}


def committed(key):
    return round(sum(a['charged_bound_usd'] for a in ledger(key).get('attempts', {}).values()), 6)


def n_calls(marker):
    return sum(1 for c in UPSTREAM['calls'] if marker in c)


IDS = ids_for(BOX, 'rtb-pool-07-115933')
print('IDENTITY lease=%s spend_box=%s head=%s box_key=%s attempt_key=%s' % (IDS['lease'], IDS['spend_box'], IDS['head'][:12], IDS['box_key'], IDS['attempt_key']))
try:
    ids_for(BOX, 'some-unverified-host')
    check('pool lease alias never becomes the spend identity', False)
except RuntimeError as e:
    check('pool lease alias never becomes the spend identity', 'refusing a per-lease budget' in str(e), str(e)[:90])

# 1. CI stays strict / non-onmiss lanes unchanged
m, c = boot(IDS, ci=True)
r = call(c, 'CI-MISS')
check('CI=true: on-miss inactive, strict miss 424 fixture_miss, 0 upstream', r.status_code == 424 and r.json().get('error') == 'fixture_miss' and n_calls('CI-MISS') == 0, str(r.status_code))
check('CI=true: healthz reports qa_onmiss=false', c.get('/_healthz').json().get('qa_onmiss') is False)
m, c = boot(None)
r = call(c, 'SPEC-MISS')
canonical_key = r.json().get('key')
check('spec/strict lane (no --on-miss): strict miss 424, 0 upstream', r.status_code == 424 and n_calls('SPEC-MISS') == 0, str(r.status_code))
check('spec/strict lane: healthz qa_onmiss=false', c.get('/_healthz').json().get('qa_onmiss') is False)

# 2. replay first -> 0 paid calls
R2.objects[canonical_key] = (b'{"candidates":[{"content":{"parts":[{"text":"replayed"}]},"finishReason":"STOP"}]}', '"1"', {'response-status': '200', 'content-type': 'application/json'})
m, c = boot(IDS)
check('on-miss: healthz qa_onmiss=true, mode strict', c.get('/_healthz').json().get('qa_onmiss') is True and c.get('/_healthz').json().get('mode') == 'strict')
r = call(c, 'SPEC-MISS')
check('replay hit: 200 from recorded fixture, 0 upstream, 0 reservations', r.status_code == 200 and 'replayed' in r.text and n_calls('SPEC-MISS') == 0 and committed(IDS['box_key']) == 0, str(r.status_code))

# 3. priced miss dispatched ONCE under a concurrent duplicate
res = {}
def dup(i):
    _, cc = boot(IDS) if i else (m, c)
    res[i] = call(cc, 'MISS-1 SLOW')
threads = [threading.Thread(target=dup, args=(i,)) for i in (0, 1)]
[t.start() for t in threads]; [t.join() for t in threads]
hdrs = sorted(res[i].headers.get('x-heydonna-qa-onmiss', 'live') for i in (0, 1))
check('concurrent duplicate: both 200, upstream dispatched exactly once', all(res[i].status_code == 200 for i in (0, 1)) and n_calls('MISS-1') == 1, str([res[0].status_code, res[1].status_code]) + ' upstream=' + str(n_calls('MISS-1')))
check('concurrent duplicate: twin reused the completed response (local-hit)', hdrs == ['live', 'local-hit'], str(hdrs))
row = [a for a in ledger(IDS['box_key'])['attempts'].values()][0]
check('reservation priced BEFORE dispatch, settled from usage', row['reserved_usd'] == 1.032192 and row['settled'] and round(row['charged_bound_usd'], 6) == 0.975, json.dumps({k: row[k] for k in ('reserved_usd', 'charged_bound_usd', 'settled')}))
m, c = boot(IDS)  # restart
r = call(c, 'MISS-1 SLOW')
check('after restart: same request served box-locally, still 1 upstream', r.status_code == 200 and r.headers.get('x-heydonna-qa-onmiss') == 'local-hit' and n_calls('MISS-1') == 1)

# 4. accepted lost-ACK + restart: no resend, no budget reset
before = committed(IDS['box_key'])
r = call(c, 'MISS-2 DROP')
after_drop = committed(IDS['box_key'])
st = ledger(IDS['attempt_key'])['requests']
key2 = [k for k, v in st.items() if v['state'] == 'unknown']
check('accepted upstream, response lost: 502, request state=unknown, bound retained', r.status_code == 502 and len(key2) == 1 and round(after_drop - before, 6) == 1.032192, 'state=%s committed %.6f->%.6f' % ([v['state'] for v in st.values()], before, after_drop))
m, c = boot(IDS)  # restart
r = call(c, 'MISS-2 DROP')
check('restart + resend of possibly-billed request: typed 424 QA_ONMISS_NO_RESEND, no second dispatch', r.status_code == 424 and r.json().get('error') == 'QA_ONMISS_NO_RESEND' and r.json().get('terminal') is True and n_calls('MISS-2') == 1, str(r.status_code) + ' ' + str(r.json().get('error')))
check('restart did not reset or release budget', committed(IDS['box_key']) == after_drop and committed(IDS['attempt_key']) == after_drop, '%.6f' % committed(IDS['box_key']))
# ledger-write ACK lost on the CLAIM (write applied, caller saw an error). Own box+attempt so it cannot consume the main budget.
IDS_ACK = ids_for('rtbl-aaaaaaaaaaaaaaaa-222222', 'rtb-pool-09-101010', head='a' * 40)
ma, ca = boot(IDS_ACK, box='rtbl-aaaaaaaaaaaaaaaa-222222')
R2.drop_ack_once.add(IDS_ACK['attempt_key'])
r = call(ca, 'MISS-ACK', max_out=1000)
check('claim ACK lost: 503 QA_ONMISS_LEDGER_UNAVAILABLE, 0 upstream', r.status_code == 503 and n_calls('MISS-ACK') == 0, str(r.status_code) + ' ' + str(r.json().get('error')))
ma, ca = boot(IDS_ACK, box='rtbl-aaaaaaaaaaaaaaaa-222222')
r = call(ca, 'MISS-ACK', max_out=1000)
check('claim ACK lost + restart: durable claim blocks dispatch (typed in-flight terminal, never pending)', r.status_code == 424 and r.json().get('error') == 'QA_ONMISS_IN_FLIGHT_TIMEOUT' and n_calls('MISS-ACK') == 0, str(r.status_code) + ' ' + str(r.json().get('error')))
check('claim ACK lost: reservation retained (never released on uncertainty)', committed(IDS_ACK['attempt_key']) > 0, '%.6f' % committed(IDS_ACK['attempt_key']))

# 5. cap boundary ($3 per box lifetime)
base = committed(IDS['box_key'])
r = call(c, 'MISS-3', max_out=55000, usage=[100_000, 1_000])
b3 = (1048576 * .75 + 55000 * 3.75) / 1e6
check('cap boundary: committed %.6f + bound %.6f = %.6f <= $3 -> admitted, 1 upstream' % (base, b3, base + b3), r.status_code == 200 and base + b3 <= 3 and n_calls('MISS-3') == 1)
base = committed(IDS['box_key'])
r = call(c, 'MISS-4')
check('cap boundary: committed %.6f + bound 1.032192 = %.6f > $3 -> typed 402 QA_ONMISS_SPEND_CAP, 0 upstream' % (base, base + 1.032192),
      r.status_code == 402 and r.json().get('error') == 'QA_ONMISS_SPEND_CAP' and n_calls('MISS-4') == 0, str(r.status_code))
# 6. lifetime: new lease on the SAME physical box (warm reuse), and a replacement box for the SAME attempt
ids_l2 = ids_for('rtbl-fedcba9876543210-654321', 'rtb-pool-07-115933', head='f' * 40)
m2, c2 = boot(ids_l2, box='rtbl-fedcba9876543210-654321')
r = call(c2, 'MISS-5')
check('warm re-lease, new head, same physical box: box-lifetime cap still applies, 0 upstream', r.status_code == 402 and r.json().get('error') == 'QA_ONMISS_SPEND_CAP' and n_calls('MISS-5') == 0 and ids_l2['box_key'] == IDS['box_key'], str(r.status_code) + ' ' + r.json().get('detail', '')[:90])
ids_rep = ids_for('rtb-qa-replace-111111', 'rtb-qa-replace-111111')
m3, c3 = boot(ids_rep, box='rtb-qa-replace-111111')
r = call(c3, 'MISS-6', max_out=1000, usage=[10, 10])
check('replacement box, same QA attempt (head): attempt cap still applies, 0 upstream', r.status_code == 402 and r.json().get('error') == 'QA_ONMISS_SPEND_CAP' and n_calls('MISS-6') == 0 and ids_rep['attempt_key'] == IDS['attempt_key'] and ids_rep['box_key'] != IDS['box_key'], str(r.status_code) + ' ' + r.json().get('detail', '')[:90])

# 6b. same head on a NEW lease of the same physical box: earlier payload was deleted with its lease -> one fresh priced claim,
# charged cumulatively (no renewal), and a same-lease repeat is reused locally.
IDS_R1 = ids_for('rtbl-bbbbbbbbbbbbbbbb-333333', 'rtb-pool-11-121212', head='b' * 40)
mr, cr = boot(IDS_R1, box='rtbl-bbbbbbbbbbbbbbbb-333333')
r1 = call(cr, 'MISS-R', max_out=1000, usage=[100, 10])
c1 = committed(IDS_R1['attempt_key'])
IDS_R2 = dict(ids_for('rtbl-cccccccccccccccc-444444', 'rtb-pool-11-121212', head='b' * 40))
mr2, cr2 = boot(IDS_R2, box='rtbl-cccccccccccccccc-444444')
r2 = call(cr2, 'MISS-R', max_out=1000, usage=[100, 10])
r3 = call(cr2, 'MISS-R', max_out=1000, usage=[100, 10])
c2 = committed(IDS_R2['attempt_key'])
check('new lease, same head: deleted payload -> one fresh priced dispatch, cumulative charge (no reset)',
      r1.status_code == r2.status_code == 200 and n_calls('MISS-R') == 2 and c2 > c1 > 0 and IDS_R2['attempt_key'] == IDS_R1['attempt_key'],
      'upstream=%d attempt %.6f->%.6f' % (n_calls('MISS-R'), c1, c2))
check('new lease, same head: repeat within the lease reuses the local record', r3.status_code == 200 and r3.headers.get('x-heydonna-qa-onmiss') == 'local-hit' and n_calls('MISS-R') == 2)

# 7. ineligible / unpriced / paid tools: refused before upstream, typed
r = call(c, 'MISS-AIS', model='gemini-3.8-flash', provider='ai_studio')
check('AI Studio miss refused before upstream (S90), typed 424 QA_ONMISS_INELIGIBLE', r.status_code == 424 and r.json().get('error') == 'QA_ONMISS_INELIGIBLE' and n_calls('MISS-AIS') == 0)
r = call(c, 'MISS-UNPRICED', model='gemini-9-ultra')
check('unpriced model refused before upstream, typed 424', r.status_code == 424 and 'UNPRICED' in r.json().get('detail', '') and n_calls('MISS-UNPRICED') == 0, r.json().get('detail', '')[:60])
try:
    m._qa_onmiss_price('gemini_vertex', 'gemini-3.8-flash', '/m:generateContent', json.dumps({'contents': [], 'tools': [{'googleSearch': {}}], 'generationConfig': {'maxOutputTokens': 10}}).encode())
    check('paid search tool refused', False)
except RuntimeError as e:
    check('paid search tool refused (never enabled)', 'BILLABLE' in str(e) or 'PAID' in str(e), str(e)[:60])

# 8. cap refusal reaches the terminal controller state
class Pub(dict):
    pass
def boom(*a, **k):
    raise AssertionError('canonical publisher called')
PUBCALLS = {}
def rec_collect(s3, bucket, run_id):
    PUBCALLS['collect'] = run_id
    return ([('staging/llm-capture/remote-qa-x/s/fixtures/llm/v2/' + 'a' * 64 + '.bin', 'fixtures/llm/v2/' + 'a' * 64 + '.bin')], 0)
def rec_publish(s3, bucket, plan, replace_allowlist=None):
    PUBCALLS['publish'] = (list(plan), replace_allowlist)
    return (1, 0, 0)
summary = H.qa_finish(FakeS3(), 'b', 'remote-qa-x', False, {'collect_plan': rec_collect, 'publish_plan': rec_publish}, IDS)
check('S161: on-miss qa_finish publishes the run staging set, PUT-if-absent only (empty replace allowlist)',
      PUBCALLS.get('collect') == IDS['stage_run'] and IDS['stage_run'].startswith('remote-qa-onmiss-') and PUBCALLS.get('publish', (None, None))[1] == frozenset()
      and summary['canonical_fixture_keys'] == ['fixtures/llm/v2/' + 'a' * 64 + '.bin'] and summary['publication_counts'] == (1, 0, 0),
      json.dumps({k: str(v)[:80] for k, v in PUBCALLS.items()}))
check('report: typed terminal reason QA_ONMISS_SPEND_CAP', summary['terminal_reason'] == 'QA_ONMISS_SPEND_CAP', json.dumps(summary['onmiss']['request_states']))
try:
    H.emit_report(summary, summary['run_id'])
    check('controller terminal raises (verdict fail, never PASS)', False)
except RuntimeError as e:
    check('controller terminal raises (verdict fail, never PASS)', 'QA_ONMISS_TERMINAL QA_ONMISS_SPEND_CAP' in str(e), str(e)[:80])
out = WORK / 'out'; out.mkdir()
(out / 'llm-onmiss.txt').write_text('QA_ONMISS_TERMINAL reason=QA_ONMISS_SPEND_CAP\n')
verdict = subprocess.run(['bash', '-c', '''out="$1"; rc=1; verdict=fail; reason=''
  [ ! -s "$out/llm-onmiss.txt" ] || ! grep -q '^QA_ONMISS_TERMINAL' "$out/llm-onmiss.txt" || reason="${reason:-$(sed -n 's/^QA_ONMISS_TERMINAL reason=//p' "$out/llm-onmiss.txt" | head -1)}"
  echo "REMOTE_QA verdict=$verdict${reason:+ reason=$reason}"''', '_', str(out)], capture_output=True, text=True).stdout.strip()
check('controller verdict line carries the typed reason', verdict == 'REMOTE_QA verdict=fail reason=QA_ONMISS_SPEND_CAP', verdict)
cap_src = Path(os.environ.get('QA_CAPTURE_SH', Path.home() / '.claude/scripts/remote-qa-capture.sh')).read_text()
check('remote-qa-capture.sh carries the same verdict snippet', "reason=\"${reason:-$(sed -n 's/^QA_ONMISS_TERMINAL reason=//p' \"$out/llm-onmiss.txt\" | head -1)}\"" in cap_src)

# 9. S161: every completed on-miss reply is ALSO staged like capture (full metadata + .request), never written canonical
# directly (publication happens only through qa_finish's publisher, PUT-if-absent); spend metadata carries no payload
direct_canonical = [k for k in R2.objects if k.startswith('fixtures/') and k != canonical_key]
check('on-miss never writes canonical directly', not direct_canonical, str(direct_canonical[:3]))
staged = {k: v for k, v in R2.objects.items() if k.startswith('staging/llm-capture/remote-qa-') and not k.endswith('.request')}
staged_ok = [k for k, v in staged.items() if v[2].get('canonical-key') and v[2].get('response-status', '').startswith('2')
             and v[2].get('fixture-version') and 'response-headers-b64' in v[2] and (k + '.request') in R2.objects]
check('S161 scope: on-miss staging lives only under its own run prefix (never the capture lane run id)', all(k.startswith('staging/llm-capture/remote-qa-onmiss-') for k in staged), str(list(staged)[:1]))
check('S161: each completed on-miss reply staged with capture metadata + request sidecar', len(staged) >= 1 and len(staged_ok) == len(staged),
      '%d staged, %d complete' % (len(staged), len(staged_ok)))
ledgers = {k: v[0] for k, v in R2.objects.items() if k.startswith('qa-capture-ledgers/')}
check('spend ledgers hold metadata only (no payload bytes)', ledgers and not any(b'PAYLOAD-SENTINEL' in v or b'"body"' in v for v in ledgers.values()), ','.join(sorted(ledgers)))
local = FakeDict.stores.get(IDS['dict'], {})
check('payloads live only in the box-local store', len(local) == 2 and not any(FakeDict.stores.get(IDS_R2['dict'], {}).get(k) is None for k in FakeDict.stores.get(IDS_R2['dict'], {})) and all(b'PAYLOAD-SENTINEL' in v['body'] for v in local.values()), '%d records in %s' % (len(local), IDS['dict']))
print('UPSTREAM_CALLS total=%d %s' % (len(UPSTREAM['calls']), sorted(set(c.split()[0] for c in UPSTREAM['calls']))))
print('LEDGER box=%s committed=%.6f halted=%s | attempt committed=%.6f halted=%s' % (
    IDS['box_key'].rsplit('/', 1)[-1], committed(IDS['box_key']), ledger(IDS['box_key']).get('halted'),
    committed(IDS['attempt_key']), ledger(IDS['attempt_key']).get('halted')))
# Scoped report canary: real guarded publisher, fake Cloudflare policy and ETag storage.
import runpy

publisher = runpy.run_path(str(APP / 'scripts/ci/publish-staged-fixtures-bare.py'))


class ScopedCanaryS3(FakeS3):
    denial = Err('AccessDenied', 403)

    def get_object(self, Bucket, Key):
        if Key.startswith('qa-scope-denial/'):
            if self.denial:
                raise self.denial
            return {'Body': Body(b'unexpected access')}
        return super().get_object(Bucket, Key)

    def delete_object(self, Bucket, Key, IfMatch):
        with R2.lock:
            assert R2.objects[Key][1] == IfMatch
            del R2.objects[Key]


scope_s3 = ScopedCanaryS3()
canary = H.qa_scope_canary(scope_s3, H.SCOPED_FIXTURE_BUCKET, publisher, '1' * 32)
check('scoped report canary proves create, conditional conflict, unchanged readback, denial and cleanup',
      canary['first_conditional_put'] == 'created' and canary['second_conditional_put'] == 'precondition_failed'
      and canary['before'] == canary['after'] and canary['out_of_scope_get'] == 'HTTP 403 AccessDenied'
      and canary['canary_objects_removed'] == 3)
for index, (denial, expected) in enumerate(((Err('NoSuchKey', 404), 'DENIAL_UNPROVEN'),
                                            (None, 'OUTSIDE_SCOPE_READ_SUCCEEDED')), start=2):
    scope_s3.denial = denial
    nonce = str(index) * 32
    try:
        H.qa_scope_canary(scope_s3, H.SCOPED_FIXTURE_BUCKET, publisher, nonce)
    except RuntimeError as error:
        check('scoped canary rejects ' + expected, str(error) == 'QA_SCOPED_CANARY_' + expected
              and all(key not in R2.objects for key in H.scoped_canary_keys(nonce)[1:]))
    else:
        check('scoped canary rejects ' + expected, False)

scoped_env = dict(zip(H.SCOPED_FIXTURE_R2_ENV, ('heydonna-qa', 'a'*32, 'session-id', 'session-secret', 'session-token')))
kwargs = {}
saved_client = boto3.client
try:
    boto3.client = lambda *a, **kw: kwargs.update(kw) or scope_s3
    client, bucket = H.scoped_fixture_client(scoped_env)
    check('scoped report signs with session credentials and explicit QA bucket',
          client is scope_s3 and bucket == 'heydonna-qa' and kwargs['aws_session_token'] == 'session-token'
          and kwargs['aws_secret_access_key'] == 'session-secret')
    kwargs.clear()
    try:
        H.scoped_fixture_client(dict(scoped_env, QA_FIXTURE_R2_SESSION_TOKEN='', AWS_SESSION_TOKEN='ambient-token'))
    except RuntimeError as error:
        check('missing scoped token does not fall back to ambient credentials', 'UNAVAILABLE' in str(error) and not kwargs)
    else:
        check('missing scoped token does not fall back to ambient credentials', False)
finally:
    boto3.client = saved_client

# ---------------- 10. M15 scoped exact-hash mode (CTO GO C0ALZJHGE49 thread 1794677380.515009 ts 1795301055.769519) ----------------
# Fakes: prefix-scoped sessions live in the box's Modal Dict (as the controller arms them); boto3.Session is faked so the credentials
# every scoped client actually resolves can be asserted; boto3.client is counted so ANY application-signer client built by an exact
# proxy for fixtures, staging or ledgers is caught.
import hashlib as _hl
import io as _io
import time as _time

EX_HEAD = 'd' * 40
EX_BOX, EX2_BOX, EX3_BOX, NS_BOX = 'rtbl-dddddddddddddddd-565656', 'rtbl-eeeeeeeeeeeeeeee-676767', 'rtbl-ffffffffffffffff-898989', 'rtbl-9999999999999999-787878'
EX_HOST, NS_HOST = 'rtb-pool-13-141414', 'rtb-pool-14-151515'
APPROVAL = 'C0ALZJHGE49/1795301055.769519'


def learn(text):
    _, lc = boot(None)
    return call(lc, text).json()['key']


def entry(key):
    parts = key.split('/')
    model, op = parts[5].rsplit('.', 1)
    return {'hash': parts[6][:-4], 'provider': parts[3], 'model': model, 'operation': op, 'canonical_key': key}


K_OK, K_TWIN, K_LK, K_NA = learn('EXACT-OK'), learn('EXACT-TWIN SLOW'), learn('EXACT-LOOKUP'), learn('EXACT-NOT-APPROVED')
H_NA = entry(K_NA)['hash']
MDIR = Path(tempfile.mkdtemp())


def manifest_for(head, keys, name):
    p = MDIR / name
    p.write_text(json.dumps({'version': 1, 'head': head, 'approval_ref': APPROVAL, 'hashes': [entry(k) for k in keys]}))
    return p


MAN_PATH = manifest_for(EX_HEAD, [K_OK, K_TWIN, K_LK], 'a.json')
MAN = H.load_exact_manifest(MAN_PATH, EX_HEAD)
check('exact manifest: full hashes, canonical keys bound to provider/model/operation/hash', set(MAN['entries']) == {K_OK, K_TWIN, K_LK}
      and all(len(e['hash']) == 64 for e in MAN['entries'].values()))
for why, mutate in (('prefix hash', lambda x: x['hashes'][0].__setitem__('hash', x['hashes'][0]['hash'][:12])),
                    ('wildcard key', lambda x: x['hashes'][0].__setitem__('canonical_key', K_OK.replace(entry(K_OK)['hash'], '*'))),
                    ('head mismatch', lambda x: x.__setitem__('head', 'e' * 40)),
                    ('empty set', lambda x: x.__setitem__('hashes', [])),
                    ('unknown field', lambda x: x.__setitem__('extra', 1)),
                    ('duplicate hash', lambda x: x['hashes'].append(dict(x['hashes'][0])))):
    doc = json.loads(MAN_PATH.read_text())
    mutate(doc)
    bad = MDIR / 'bad.json'
    bad.write_text(json.dumps(doc))
    try:
        H.load_exact_manifest(bad, EX_HEAD)
        check('exact manifest refuses ' + why, False)
    except ValueError as error:
        check('exact manifest refuses ' + why, 'QA_EXACT_MANIFEST' in str(error), str(error)[:50])

EX_IDS = dict(ids_for(EX_BOX, EX_HOST, head=EX_HEAD), exact=MAN)
EX2_IDS = dict(ids_for(EX2_BOX, EX_HOST, head=EX_HEAD), exact=MAN)   # second lease, same physical box + head
EX3_IDS = dict(ids_for(EX3_BOX, EX_HOST, head=EX_HEAD), exact=MAN)   # third lease: no reclaim of a completed earlier claim
NS_IDS = dict(ids_for(NS_BOX, NS_HOST, head='c' * 40), exact=H.load_exact_manifest(manifest_for('c' * 40, [K_OK], 'ns.json'), 'c' * 40))
CREDS, ENV_CLIENTS = [], []


class FakeSession:
    def __init__(self, botocore_session=None, **_):
        self.bs = botocore_session

    def client(self, *a, **kw):
        c = self.bs.get_credentials().get_frozen_credentials()
        CREDS.append((c.access_key, c.secret_key, c.token, kw.get('endpoint_url')))
        return FakeS3()


def arm(ids, kinds=('read', 'write'), exp=None, digest=None, lease=None):
    store = FakeDict.stores.setdefault(ids['dict'], {})
    for kind in kinds:
        store['scoped-session/' + kind] = {'kind': kind, 'bucket': 'heydonna-qa', 'account': 'a' * 32, 'access': 'SESS-ACCESS-' + kind,
                                           'secret': 'SESS-SECRET', 'token': 'SESS-TOKEN', 'exp': exp or int(_time.time()) + 300,
                                           'manifest_digest': digest or ids['exact']['digest'], 'lease': lease or ids['lease']}


def call_x(client, text, rid='req-exact-1', section='proofread'):
    body = {'contents': [{'role': 'user', 'parts': [{'text': text}]}], 'generationConfig': {'maxOutputTokens': 65536}}
    hdr = {'X-Upstream-Base': 'https://aiplatform.googleapis.com', 'Authorization': 'Bearer stub',
           'x-heydonna-request-id': rid, 'x-heydonna-section-type': section}
    return client.post('/v1beta1/projects/p/locations/global/publishers/google/models/gemini-3.8-flash:generateContent',
                       content=json.dumps(body), headers=hdr)


saved_session, saved_client_ex = boto3.Session, boto3.client
boto3.Session = FakeSession
boto3.client = lambda *a, **k: (ENV_CLIENTS.append(k.get('aws_access_key_id')) or FakeS3())
try:
    UPSTREAM['keys'] = (EX_IDS['box_key'], EX_IDS['attempt_key'])
    arm(EX_IDS)
    me, ce = boot(EX_IDS, box=EX_BOX)
    me._QA_EXACT_SESSION_WAIT_S = 1
    hz = ce.get('/_healthz').json()
    check('exact: healthz reports the baked manifest digest', hz.get('qa_onmiss') is True and hz.get('qa_exact_digest') == MAN['digest'], str(hz)[:80])
    # a. nonmember: refused BEFORE pricing, claim or provider I/O, with its full hash
    r = call(ce, 'EXACT-NOT-APPROVED')
    check('exact: nonmember miss refused with its FULL hash, zero upstream, zero reservation',
          r.status_code == 424 and r.json().get('error') == 'QA_ONMISS_NOT_APPROVED' and H_NA in r.json().get('detail', '')
          and n_calls('EXACT-NOT-APPROVED') == 0 and committed(EX_IDS['box_key']) == 0 and committed(EX_IDS['attempt_key']) == 0,
          str(r.status_code) + ' ' + r.text[:90])
    r = call(ce, 'EXACT-NOT-APPROVED', model='gemini-9-ultra')
    check('exact: an unpriced model nonmember is refused as NOT_APPROVED first (no pricing attempted)', r.status_code == 424
          and r.json().get('error') == 'QA_ONMISS_NOT_APPROVED' and n_calls('EXACT-NOT-APPROVED') == 0)
    # b. lookup failure is NOT a miss (the stock proxy treats a failed replay read as one)
    R2.deny_get.add(K_LK)
    r = call(ce, 'EXACT-LOOKUP')
    R2.deny_get.discard(K_LK)
    check('exact: a 403 on the replay lookup is never a miss: typed 503 QA_ONMISS_LOOKUP_UNCERTAIN, zero upstream, zero reservation',
          r.status_code == 503 and r.json().get('error') == 'QA_ONMISS_LOOKUP_UNCERTAIN' and n_calls('EXACT-LOOKUP') == 0
          and committed(EX_IDS['box_key']) == 0, str(r.status_code) + ' ' + r.text[:90])
    codes = [x['code'] for x in ledger(EX_IDS['box_key']).get('refused_calls', [])]
    s_x = H.qa_finish(FakeS3(), 'heydonna-qa', 'x', False, publisher, EX_IDS)
    check('exact: report carries the typed refusal reason and the refused full hash',
          'QA_ONMISS_NOT_APPROVED' in codes and 'QA_ONMISS_LOOKUP_UNCERTAIN' in codes and s_x['terminal_reason'] == 'QA_ONMISS_NOT_APPROVED'
          and H_NA in s_x['onmiss']['refused_hashes'] and s_x['fixtures_written'] == [], str(codes))
    # c. approved member: both CAS reservations BEFORE the single send; join identity recorded; scoped creds only
    UPSTREAM['keys'] = (EX2_IDS['box_key'], EX2_IDS['attempt_key'])
    arm(EX2_IDS)
    m2e, c2e = boot(EX2_IDS, box=EX2_BOX)
    m2e._QA_EXACT_SESSION_WAIT_S = 1
    r = call_x(c2e, 'EXACT-OK')
    at = UPSTREAM['at_send'].get('EXACT-OK', (None, None))
    check('exact: approved hash dispatched exactly once, 200', r.status_code == 200 and n_calls('EXACT-OK') == 1 and 'PAYLOAD-SENTINEL' in r.text, str(r.status_code))
    check('exact: box-lifetime AND attempt reservations were both durable at the instant of the provider send',
          all(v and v > 0 for v in at), str(at))
    row = [a for a in ledger(EX2_IDS['attempt_key'])['attempts'].values() if a.get('onmiss_key') == K_OK][0]
    check('exact: claim binds approval ref + manifest digest, ingress request id and section',
          row['approval_ref'] == APPROVAL and row['manifest_digest'] == MAN['digest'] and row['ingress_request_id'] == 'req-exact-1'
          and row['section_type'] == 'proofread', json.dumps({k: row.get(k) for k in ('approval_ref', 'ingress_request_id')}))
    check('exact: provider dispatch id (response id) + upstream status recorded BEFORE settlement, settled from usage',
          row['dispatch']['upstream_status'] == 200 and row['dispatch']['provider_response_ids'][0].startswith('respExact') and row['settled'],
          json.dumps(row.get('dispatch')))
    # d. concurrent twins of an approved hash: one send
    res = {}

    def twin(i):
        tm, tc = boot(EX2_IDS, box=EX2_BOX)
        tm._QA_ONMISS_WAIT_S = 30  # the scoped clients add ledger round trips; production waits 960 s
        res[i] = call_x(tc, 'EXACT-TWIN SLOW', rid='req-twin-%d' % i)
    ts = [threading.Thread(target=twin, args=(i,)) for i in (0, 1)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    check('exact: concurrent twins of an approved hash send exactly once', n_calls('EXACT-TWIN') == 1 and sorted(x.status_code for x in res.values()) == [200, 200],
          str([x.status_code for x in res.values()]) + ' upstream=' + str(n_calls('EXACT-TWIN')))
    # e. zero retries / no reclaim: a NEW lease never re-dispatches an earlier lease's completed approved claim
    UPSTREAM['keys'] = (EX3_IDS['box_key'], EX3_IDS['attempt_key'])
    arm(EX3_IDS)
    m3e, c3e = boot(EX3_IDS, box=EX3_BOX)
    m3e._QA_EXACT_SESSION_WAIT_S = 1
    r = call_x(c3e, 'EXACT-OK')
    check('exact: no stock reclaim of an earlier lease completed claim (typed 424 NO_RESEND, still 1 upstream)',
          r.status_code == 424 and r.json().get('error') == 'QA_ONMISS_NO_RESEND' and n_calls('EXACT-OK') == 1, str(r.status_code) + ' ' + r.text[:80])
    # f. scope denial / no fallback: missing, expired, other-run and other-lease sessions fail closed and no application signer is used
    sessions = (('no session armed', {}), ('expired session', {'exp': int(_time.time()) - 5}),
                ('session for another manifest', {'digest': '0' * 64}), ('session for another lease', {'lease': 'f' * 12}))
    for why, kw in sessions:
        FakeDict.stores.pop(NS_IDS['dict'], None)
        if why != 'no session armed':
            arm(NS_IDS, **kw)
        mn, cn = boot(NS_IDS, box=NS_BOX)
        mn._QA_EXACT_SESSION_WAIT_S = 1
        before = n_calls('EXACT-OK')
        r = call(cn, 'EXACT-OK')
        check('exact scope: ' + why + ' fails closed (typed 503, zero upstream, no fallback)',
              r.status_code == 503 and r.json().get('error') == 'QA_ONMISS_LOOKUP_UNCERTAIN' and n_calls('EXACT-OK') == before,
              str(r.status_code) + ' ' + r.text[:80])
    check('exact scope: every scoped client resolved ONLY the armed session credentials; no application-signer client was built',
          CREDS and all(c[0].startswith('SESS-ACCESS-') and c[1] == 'SESS-SECRET' and c[2] == 'SESS-TOKEN' and c[3] == 'https://' + 'a' * 32 + '.r2.cloudflarestorage.com'
                        for c in CREDS) and not ENV_CLIENTS, 'creds=%d env_clients=%s' % (len(CREDS), ENV_CLIENTS))
    # g. report: unchanged guarded publisher, approved + settled only, readback, join; a conflicting older object is not proof
    R2.objects[K_TWIN] = (b'older different bytes', '"7"', {'response-status': '200'})
    rogue_key = 'fixtures/llm/v2/gemini_vertex/aiplatform.googleapis.com/rogue.generate/' + '9' * 64 + '.bin'
    sx = H.qa_finish(FakeS3(), 'heydonna-qa', 'x', False, publisher, EX2_IDS)
    ok_row = [x for x in sx['readback'] if x['key'] == K_OK]
    tw_row = [x for x in sx['readback'] if x['key'] == K_TWIN]
    check('exact report: approved settled capture published PUT-if-absent and read back MATCH (bytes, status, content type)',
          sx['fixtures_written'] == [K_OK] and ok_row and ok_row[0]['status'] == 'MATCH', json.dumps(sx['readback'])[:160])
    check('exact report: a same-key older object with different bytes is not capture proof (MISMATCH, typed terminal), valid capture kept',
          tw_row and tw_row[0]['status'] == 'MISMATCH' and R2.objects[K_TWIN][0] == b'older different bytes'
          and sx['terminal_reason'] == 'QA_ONMISS_READBACK_MISMATCH' and sx['exact']['readback_match'] == 1, str(sx['terminal_reason']))
    jr = [x for x in sx['join'] if x['key'] == K_OK]
    check('exact report: join carries ingress request id, section and provider dispatch id',
          jr and jr[0]['ingress_request_id'] == 'req-exact-1' and jr[0]['provider_dispatch']['provider_response_ids'] and jr[0]['settled'], json.dumps(jr)[:120])
    stage_key = [k for k in R2.objects if k.startswith('staging/llm-capture/' + EX2_IDS['stage_run'] + '/') and k.endswith(K_OK)][0]
    R2.objects[stage_key.replace(K_OK, rogue_key)] = (b'{"candidates":[{"finishReason":"STOP"}]}', '"9"', {'canonical-key': rogue_key, 'response-status': '200'})
    sx2 = H.qa_finish(FakeS3(), 'heydonna-qa', 'x', False, publisher, EX2_IDS)
    check('exact report: a staged object that is not an approved, settled claim is never published (typed STAGING_MISMATCH)',
          rogue_key not in R2.objects and rogue_key in sx2['unapproved_staged'] and sx2['terminal_reason'] == 'QA_ONMISS_STAGING_MISMATCH', str(sx2['terminal_reason']))
    try:
        H.emit_report(sx2, 'x')
        check('exact report: terminal raises (verdict fail, never PASS)', False)
    except RuntimeError as error:
        check('exact report: terminal raises (verdict fail, never PASS)', 'QA_ONMISS_TERMINAL QA_ONMISS_STAGING_MISMATCH' in str(error))
    # h. session minting/arming/report-session unit checks (no network, no Modal)
    mint_calls = []
    saved_mint = H._mint_session
    H._mint_session = lambda paths, actions, ttl: (mint_calls.append((list(paths), list(actions), ttl)) or (
        dict(zip(H.SCOPED_FIXTURE_R2_ENV, ('heydonna-qa', 'a' * 32, 'acc', 'sec', 'tok'))), int(_time.time()) + ttl))
    try:
        stage = 'staging/llm-capture/' + EX2_IDS['stage_run'] + '/'
        proxy_rows = H.mint_exact_sessions('proxy', EX2_IDS, MAN)
        check('mint proxy: read = fixture store + this run staging, GetObject only; write = staging + the two ledger objects only; 5 minutes',
              mint_calls[0] == (['fixtures/llm/v2/', stage], ['GetObject'], 300)
              and mint_calls[1] == ([stage, EX2_IDS['box_key'], EX2_IDS['attempt_key']], ['GetObject', 'PutObject'], 300)
              and not any(p.startswith('fixtures/') for p in mint_calls[1][0]) and set(proxy_rows) == {'read', 'write'}
              and all(r['manifest_digest'] == MAN['digest'] and r['lease'] == EX2_IDS['lease'] for r in proxy_rows.values()), str(mint_calls[:2]))
        mint_calls.clear()
        H.mint_exact_sessions('report', EX2_IDS, MAN)
        check('mint report: staging + approved canonical keys + the two ledger objects; List/Get/Put; 5 minutes',
              mint_calls == [([stage] + sorted(MAN['entries']) + [EX2_IDS['box_key'], EX2_IDS['attempt_key']], ['ListObjectsV2', 'GetObject', 'PutObject'], 300)], str(mint_calls)[:100])
    finally:
        H._mint_session = saved_mint
    expired_payload = json.dumps({k: {'kind': k, 'bucket': 'heydonna-qa', 'account': 'a' * 32, 'access': 'x', 'secret': 'y', 'token': 'z',
                                      'exp': int(_time.time()) - 5, 'manifest_digest': MAN['digest'], 'lease': H.identity(EX_BOX)} for k in ('read', 'write')})
    saved_stdin = sys.stdin
    sys.stdin = _io.StringIO(expired_payload)
    try:
        H.arm_exact_sessions(EX_BOX, MAN_PATH)
        check('arm: an expired session is refused before any Modal write', False)
    except ValueError as error:
        check('arm: an expired session is refused before any Modal write', 'QA_SCOPED_ARM_INVALID' in str(error))
    finally:
        sys.stdin = saved_stdin
    os.environ.update({'MODAL_TOKEN_ID': 'tid-stub', 'MODAL_TOKEN_SECRET': 'tsecret-stub'})
    real_host = socket.gethostname
    socket.gethostname = lambda: EX_HOST
    try:
        head_manifest = manifest_for(HEAD1, [K_OK], 'head.json')
        for why, args, needle in (('no session file', (None,), 'QA_SCOPED_R2_REFUSED'),
                                  ('missing session file', (str(MDIR / 'absent.json'),), 'QA_SCOPED_R2_UNAVAILABLE')):
            try:
                H.capture_report(EX_BOX, APP, MDIR / 'rep.json', False, False, 2.0, True, False, str(head_manifest), *args)
                check('exact report container: ' + why + ' refuses (no shared-secret fallback)', False)
            except RuntimeError as error:
                check('exact report container: ' + why + ' refuses (no shared-secret fallback)', needle in str(error), str(error)[:70])
        sess = MDIR / 'report-session.json'
        sess.write_text(json.dumps({'kind': 'report', 'bucket': 'heydonna-qa', 'account': 'a' * 32, 'access': 'x', 'secret': 'y', 'token': 'z',
                                    'exp': int(_time.time()) - 5, 'manifest_digest': MAN['digest'], 'lease': EX2_IDS['lease']}))
        try:
            H.load_report_session(sess, EX2_IDS)
            check('exact report container: an expired session refuses and the session file is deleted', False)
        except RuntimeError as error:
            check('exact report container: an expired session refuses and the session file is deleted', 'UNAVAILABLE' in str(error) and not sess.exists())
        try:
            H.capture_report(EX_BOX, APP, MDIR / 'rep.json', False, False, 2.0, False, False, str(head_manifest), None)
            check('--exact-manifest without --on-miss is refused', False)
        except RuntimeError as error:
            check('--exact-manifest without --on-miss is refused', 'requires --on-miss' in str(error))
    finally:
        socket.gethostname = real_host
    receipt = {'bucket': 'heydonna-qa', 'list_and_read': True, 'first_conditional_put': 'created', 'second_conditional_put': 'precondition_failed',
               'before': {'sha256': 'a', 'etag': 'b'}, 'after': {'sha256': 'a', 'etag': 'b'}, 'out_of_scope_get': 'HTTP 403 AccessDenied',
               'canary_objects_removed': 3, 'modal_teardown': 'verified no active canary app', 'report_secret_mount': 'ephemeral scoped session only',
               'tooling_sha256': _hl.sha256(HELPER.read_bytes()).hexdigest(), 'at': int(_time.time())}
    for why, mutate, expect in (('a passed canary of this controller', lambda x: None, True), ('another controller file', lambda x: x.__setitem__('tooling_sha256', '0' * 64), False),
                                ('an older-than-48h canary', lambda x: x.__setitem__('at', int(_time.time()) - 49 * 3600), False),
                                ('a canary without the denial proof', lambda x: x.__setitem__('out_of_scope_get', 'none'), False),
                                ('a canary whose second PUT changed bytes', lambda x: x.__setitem__('after', {'sha256': 'c', 'etag': 'b'}), False)):
        doc = dict(receipt)
        mutate(doc)
        rp = MDIR / 'receipt.json'
        rp.write_text(json.dumps(doc))
        try:
            H.check_canary_receipt(rp)
            ok = True
        except ValueError:
            ok = False
        check('arming prerequisite: ' + ('accepts ' if expect else 'rejects ') + why, ok is expect)
finally:
    boto3.Session, boto3.client = saved_session, saved_client_ex

shutil.rmtree(WORK, ignore_errors=True)
print('ONMISS_STUB_PROOF ' + ('PASS' if not FAILS else 'FAIL ' + ','.join(FAILS)))
sys.exit(1 if FAILS else 0)
