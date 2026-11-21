#!/usr/bin/env python3
"""STUB proof for SPEC-mode capture-on-miss (Rajiv D0AMF0XE6TS thread 1793524869.957399 ev 1793528200.649109; CTO option A
C0ALZJHGE49 1793524845.153109). Script-level tooling test, not an app suite. Reuses the fakes of remote-qa-onmiss-stub.test.py
(in-memory R2 with ETag CAS, Modal Dict, httpx MockTransport upstream) and drives the DEPLOYED overlay (remote-qa-modal.py)
plus the DEPLOYED spec controller wiring (remote-qa-spec.sh / remote-qa-spec-runtime.sh). No network, no provider, no Modal.
Covers the CTO list: strict sends zero; one eligible capture; shared $3 cap across capture+spec with concurrency, restart and
mode switch; UNKNOWN refusal; zero canonical writes; local replay of the captured payload; ON_MISS_CAPTURED labelling.
Run: uv run --no-project --with fastapi==0.115.12 --with httpx==0.25.2 --with boto3==1.35.99 python remote-qa-spec-onmiss-stub.test.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

SCRIPTS = Path(os.environ.get('QA_SCRIPTS', Path.home() / '.claude/scripts'))
BASE = (SCRIPTS / 'remote-qa-onmiss-stub.test.py').read_text()
# Harness only (fakes, boot/call/ledger helpers); the capture-lane scenarios after it are not re-run here.
exec(compile(BASE[:BASE.index('\nIDS = ids_for(BOX')], 'onmiss-stub-harness', 'exec'), globals())

SPEC_SH = (SCRIPTS / 'remote-qa-spec.sh').read_text()
RUNTIME_SH = (SCRIPTS / 'remote-qa-spec-runtime.sh').read_text()
CAPTURE_SH = (SCRIPTS / 'remote-qa-capture.sh').read_text()
canon = lambda: sorted(k for k in R2.objects if k.startswith(('fixtures/', 'staging/llm-capture/')))

# ---- A. controller wiring: opt-in only, SAME controller commands as the capture lane ----
h40 = 'a' * 40
def dry(*extra):
    r = subprocess.run(['bash', str(SCRIPTS / 'remote-qa-spec.sh'), '--slot', '8', '--branch', 'b', '--head', h40,
                        '--spec', 'tests/e2e/specs/qa-tests/x.spec.ts', '--out', tempfile.mkdtemp(), '--allow-seed-replace', *extra],
                       capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()
rc, o = dry('--disposable-convex', '--dry-run')
check('spec default (no flag) resolves llm_mode=strict', rc == 0 and o.endswith('llm_mode=strict'), o[-40:])
rc, o = dry('--disposable-convex', '--on-miss', '--dry-run')
check('spec --on-miss resolves llm_mode=capture-on-miss', rc == 0 and o.endswith('llm_mode=capture-on-miss'), o[-40:])
rc, o = dry('--on-miss', '--dry-run')
check('--on-miss without --disposable-convex refused (synthetic-only bound)', rc == 2 and 'requires --disposable-convex' in o, o)
pf = Path(tempfile.mkdtemp()) / 'm.patch'; pf.write_text('diff --git a/tests/e2e/a.ts b/tests/e2e/a.ts\n--- a/tests/e2e/a.ts\n+++ b/tests/e2e/a.ts\n')
rc, o = dry('--disposable-convex', '--on-miss', '--mutate-patch', str(pf), '--dry-run')
check('--on-miss refuses RED/mutation legs (RED stays strict)', rc == 2 and 'GREEN-only' in o, o)
check('llm_onmiss starts empty and is set ONLY by the --on-miss flag',
      "llm_onmiss='' pre_write_check=''" in SPEC_SH and len(re.findall(r'llm_onmiss=--on-miss', SPEC_SH)) == 1
      and '--on-miss) llm_onmiss=--on-miss; shift;;' in SPEC_SH)
for cmd in ('register --box "$box" --ttl "$ttl_seconds" $llm_onmiss',
            "modal-lane.py overlay --root '$remote' --box '$box' $llm_onmiss",
            "modal-lane.py prepare --box '$box' --root '$remote' --config /root/qa/config.json $llm_onmiss",
            "modal-lane.py capture-report --box '$box' --root '$remote' --output /root/qa/out/llm-ledger.json $llm_onmiss"):
    check('spec passes the flag to the shared controller: ' + cmd.split(' --')[0].split()[-1], cmd in SPEC_SH)
check('capture lane uses the same controller commands (register/overlay/prepare/capture-report + $llm_onmiss)',
      all(s in CAPTURE_SH for s in ('modal-lane.py overlay', 'modal-lane.py prepare', 'modal-lane.py capture-report', '$llm_onmiss')))
check('spec runtime keeps STRICT_FIXTURES=true and capture off in BOTH modes',
      'export STRICT_FIXTURES=true LLM_PROXY_ENABLED=true LLM_PROXY_FIXTURE_CAPTURE=false' in RUNTIME_SH and 'llm_mode' in RUNTIME_SH)
check('no CI workflow or UserPromptSubmit hook referenced by the change',
      not any(s in SPEC_SH + RUNTIME_SH for s in ('.github/workflows', 'UserPromptSubmit')))

# ---- B. proxy behaviour (deployed overlay) ----
SPEC_BOX = 'rtbl-5pec000000000000-101010'.replace('5pec', '5bec')  # hex lease alias
POOL_HOST = 'rtb-pool-08-090909'
HEADX = 'c' * 40
IDS_CAP = ids_for('rtbl-ca00000000000000-111111', POOL_HOST, head=HEADX)  # capture-lane lease on the same physical box + head
IDS_SPEC = ids_for(SPEC_BOX, POOL_HOST, head=HEADX)                        # spec-lane lease, same physical box + head
check('capture and spec on the same physical box + head bind the SAME durable ledgers',
      IDS_CAP['box_key'] == IDS_SPEC['box_key'] and IDS_CAP['attempt_key'] == IDS_SPEC['attempt_key'] and IDS_CAP['lease'] != IDS_SPEC['lease'])

# 1. strict spec mode sends zero (no --on-miss -> OFF source baked)
m, c = boot(None, box=SPEC_BOX)
r = call(c, 'S-STRICT')
check('strict spec: miss -> 424 fixture_miss, 0 upstream, healthz qa_onmiss=false',
      r.status_code == 424 and r.json().get('error') == 'fixture_miss' and n_calls('S-STRICT') == 0
      and c.get('/_healthz').json().get('qa_onmiss') is False, str(r.status_code))
# CI=true never enables on-miss even when the overlay is baked
m, c = boot(IDS_SPEC, ci=True, box=SPEC_BOX)
r = call(c, 'S-CI')
check('CI=true with on-miss overlay: 424, 0 upstream', r.status_code == 424 and n_calls('S-CI') == 0)

# 2. one eligible capture in spec mode
m, cs = boot(IDS_SPEC, box=SPEC_BOX)
r = call(cs, 'S-ONE', max_out=8000, usage=[20_000, 2_000])
check('spec --on-miss: eligible miss captured once (200, 1 upstream)', r.status_code == 200 and n_calls('S-ONE') == 1 and 'PAYLOAD-SENTINEL' in r.text)
after_one = committed(IDS_SPEC['box_key'])

# 6. local replay of the captured payload: restart (new process, same lease) -> served box-locally, no upstream
m, cs = boot(IDS_SPEC, box=SPEC_BOX)
r = call(cs, 'S-ONE', max_out=8000, usage=[20_000, 2_000])
check('restart: captured payload replayed locally (local-hit), still 1 upstream, no new charge',
      r.status_code == 200 and r.headers.get('x-heydonna-qa-onmiss') == 'local-hit' and 'PAYLOAD-SENTINEL' in r.text
      and n_calls('S-ONE') == 1 and committed(IDS_SPEC['box_key']) == after_one)

# 3. shared cap across capture + spec, concurrency, restart and mode switch
mc, cc = boot(IDS_CAP, box='rtbl-ca00000000000000-111111')  # capture lane on the same box
r = call(cc, 'C-SPEND')  # default usage settles ~0.975
cap_used = committed(IDS_CAP['box_key'])
check('capture lane spend lands in the same box+attempt ledgers (cumulative with spec)',
      r.status_code == 200 and cap_used > after_one and committed(IDS_SPEC['attempt_key']) == cap_used, '%.6f -> %.6f' % (after_one, cap_used))
res = {}
def lane(i):
    if i == 0:
        _, cl = boot(IDS_CAP, box='rtbl-ca00000000000000-111111')
    else:
        _, cl = boot(IDS_SPEC, box=SPEC_BOX)
    res[i] = call(cl, 'X-CONC SLOW')  # default usage settles ~0.975
ts = [threading.Thread(target=lane, args=(i,)) for i in (0, 1)]
[t.start() for t in ts]; [t.join() for t in ts]
codes = sorted(res[i].status_code for i in (0, 1))
check('concurrent capture+spec callers, same exact request: dispatched ONCE (single-flight)',
      n_calls('X-CONC') == 1 and 200 in codes and all(cd in (200, 424) for cd in codes), 'codes=%s upstream=%d' % (codes, n_calls('X-CONC')))
pre_cap = committed(IDS_SPEC['box_key'])
# mode switch spec -> strict on the same box: strict never dispatches and never reads the box-local payloads
m, cstrict = boot(None, box=SPEC_BOX)
r = call(cstrict, 'S-ONE', max_out=8000, usage=[20_000, 2_000])
check('mode switch to strict: previously captured request is a plain 424 miss (no local payload, 0 upstream)',
      r.status_code == 424 and r.json().get('error') == 'fixture_miss' and n_calls('S-ONE') == 1)
# switch back + restart: budget is not renewed; the next priced max (1.032192) no longer fits -> refused before dispatch
m, cs = boot(IDS_SPEC, box=SPEC_BOX)
r = call(cs, 'S-OVER')
check('restart + mode switch back: cumulative cap (capture+spec) enforced before dispatch (typed 402, 0 upstream)',
      pre_cap + 1.032192 > 3 and r.status_code == 402 and r.json().get('error') == 'QA_ONMISS_SPEND_CAP' and n_calls('S-OVER') == 0,
      'committed %.6f + 1.032192; got %d %s' % (pre_cap, r.status_code, r.json().get('detail', '')[:80]))
check('cap never exceeded across capture+spec', committed(IDS_SPEC['box_key']) <= 3 and committed(IDS_SPEC['attempt_key']) <= 3,
      '%.6f / %.6f' % (committed(IDS_SPEC['box_key']), committed(IDS_SPEC['attempt_key'])))
# replacement box / warm re-lease for the same head (attempt) cannot renew
IDS_REP = ids_for('rtb-qa-specrep-202020', 'rtb-qa-specrep-202020', head=HEADX)
m, cr = boot(IDS_REP, box='rtb-qa-specrep-202020')
r = call(cr, 'S-REP')
check('replacement box, same attempt: attempt cap still applies (402, 0 upstream)',
      r.status_code == 402 and n_calls('S-REP') == 0 and IDS_REP['attempt_key'] == IDS_SPEC['attempt_key'])

# 4. UNKNOWN refusal (own box+head so it does not depend on the exhausted budget above)
IDS_U = ids_for('rtbl-d000000000000000-303030', 'rtb-pool-10-303030', head='d' * 40)
m, cu = boot(IDS_U, box='rtbl-d000000000000000-303030')
r = call(cu, 'S-UNK DROP', max_out=1000)
st = ledger(IDS_U['attempt_key']).get('requests', {})
check('spec: accepted-then-lost upstream -> state unknown, bound retained', r.status_code == 502 and any(v['state'] == 'unknown' for v in st.values()))
u_before = committed(IDS_U['attempt_key'])
m, cu = boot(IDS_U, box='rtbl-d000000000000000-303030')
r = call(cu, 'S-UNK DROP', max_out=1000)
check('spec restart: UNKNOWN never resent (424 QA_ONMISS_NO_RESEND, 1 upstream total)',
      r.status_code == 424 and r.json().get('error') == 'QA_ONMISS_NO_RESEND' and n_calls('S-UNK') == 1)
IDS_U2 = ids_for('rtbl-d100000000000000-404040', 'rtb-pool-10-303030', head='d' * 40)  # new lease, same box + head
m, cu2 = boot(IDS_U2, box='rtbl-d100000000000000-404040')
r = call(cu2, 'S-UNK DROP', max_out=1000)
check('new lease, same box+head: UNKNOWN still blocks (no blind resend, no reset)',
      r.status_code in (402, 424) and n_calls('S-UNK') == 1 and committed(IDS_U['attempt_key']) == u_before,
      '%d %s' % (r.status_code, r.json().get('error')))

# 5. zero canonical writes
check('S161: on-miss stages replies (with .request sidecars) and never writes canonical directly', not [k for k in canon() if k.startswith('fixtures/')] and all(k.startswith('staging/llm-capture/remote-qa-') for k in canon()) and any(k.endswith('.request') for k in canon()), str(canon()[:3]))

# ---- C. controller report + verdict labelling (spec controller's own onmiss_label) ----
boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError('canonical publisher called'))
fn = SPEC_SH[SPEC_SH.index('# ONMISS_LABEL_BEGIN'):SPEC_SH.index('# ONMISS_LABEL_END')]
def label(summary, terminal_txt=None, report=True):
    d = Path(tempfile.mkdtemp()); (d / 'out').mkdir()
    if report:
        (d / 'out/llm-ledger.json').write_text(json.dumps(summary))
    if terminal_txt:
        (d / 'llm-onmiss.txt').write_text(terminal_txt)
    o = subprocess.run(['bash', '-c', fn + '\nonmiss_label "$1" 0; echo "$ONMISS_VERDICT|$ONMISS_LABEL|$ONMISS_RC|$ONMISS_REASON|$ONMISS_CAPTURES"', '_', str(d)],
                       capture_output=True, text=True).stdout.strip()
    return o
s_spec = H.qa_finish(FakeS3(), 'b', 'x', False, {'collect_plan': boom, 'publish_plan': boom}, dict(IDS_SPEC, lease=H.identity(SPEC_BOX)))
check('controller report (spec lease) sees its live capture(s), publisher never called',
      len(s_spec['attempts']) >= 1 and s_spec['canonical_fixture_keys'] == [] and s_spec['fixtures_written'] == [], str(len(s_spec['attempts'])))
clean = dict(s_spec, terminal_reason=None)
o = label(clean)
check('run with a live capture is labelled ON_MISS_CAPTURED (never lane-ok / strict PASS)', o.startswith('ON_MISS_CAPTURED|ON_MISS_CAPTURED|0||'), o)
o = label(dict(clean, attempts={}))
check('on-miss run with zero live calls is lane-ok-onmiss-replay-only', o.startswith('lane-ok-onmiss-replay-only|ON_MISS_REPLAY_ONLY|0'), o)
o = label(clean, 'QA_ONMISS_TERMINAL reason=QA_ONMISS_NO_RESEND\n')
check('typed QA_ONMISS_* terminal -> lane-fail reason carried', o.startswith('lane-fail|ON_MISS_TERMINAL|1|QA_ONMISS_NO_RESEND'), o)
o = label(clean, report=False)
check('missing controller report fails closed', o.startswith('lane-fail|ON_MISS_UNVERIFIED|1|QA_ONMISS_REPORT_MISSING'), o)
o = label(dict(clean, canonical_fixture_keys=['fixtures/x']))
check('report claiming a canonical write fails closed', o.startswith('lane-fail|ON_MISS_UNVERIFIED'), o)
try:
    H.emit_report(H.qa_finish(FakeS3(), 'b', 'x', False, {'collect_plan': boom, 'publish_plan': boom}, dict(IDS_U, lease=H.identity('rtbl-d000000000000000-303030'))), 'x')
    check('UNKNOWN lease report raises typed terminal', False)
except RuntimeError as e:
    check('UNKNOWN lease report raises typed terminal (never PASS)', 'QA_ONMISS_TERMINAL QA_ONMISS_NO_RESEND' in str(e), str(e)[:80])

# runtime proxy-mode assertion: strict expects qa_onmiss=false, on-miss expects true
check('runtime asserts deployed proxy mode == requested (strict -> qa_onmiss=false)',
      'QA_PROXY_MODE_MISMATCH' in RUNTIME_SH and 'want=$([ "$llm_mode" = onmiss ] && echo true || echo false)' in RUNTIME_SH)

print('UPSTREAM_CALLS total=%d' % len(UPSTREAM['calls']))
print('LEDGER shared box=%s committed=%.6f | attempt committed=%.6f' % (IDS_SPEC['box_key'].rsplit('/', 1)[-1], committed(IDS_SPEC['box_key']), committed(IDS_SPEC['attempt_key'])))
shutil.rmtree(WORK, ignore_errors=True)
print('SPEC_ONMISS_STUB_PROOF ' + ('PASS' if not FAILS else 'FAIL ' + ','.join(FAILS)))
sys.exit(1 if FAILS else 0)
