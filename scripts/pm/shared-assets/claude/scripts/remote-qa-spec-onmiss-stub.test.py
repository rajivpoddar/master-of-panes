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
M15SPEC = 'tests/e2e/specs/qa-tests/m15-asr-draft-langsmith.spec.ts'
P14SPEC = 'tests/e2e/specs/qa-tests/14390-headerless-proceedings.spec.ts'
P14HEAD, P14HASH = '6edba2fad7b450d60a1433b81e019464cf50e6be', 'e5c629a109c8cb064baa8d4c775bd8894104de6256d590809b8b9e48d9da3fdc'
def dry(*extra, head=h40, spec=None):
    if spec is None:  # exact runs use the approved M15 spec (which the launcher pairs with the box-generated receipt secret: --requires-modal)
        spec = M15SPEC if '--on-miss-exact' in extra else 'tests/e2e/specs/qa-tests/x.spec.ts'
    if spec == M15SPEC and '--requires-modal' not in extra:
        extra = (*extra, '--requires-modal')
    r = subprocess.run(['bash', str(SCRIPTS / 'remote-qa-spec.sh'), '--slot', '8', '--branch', 'b', '--head', head,
                        '--spec', spec, '--out', tempfile.mkdtemp(), '--allow-seed-replace', *extra],
                       capture_output=True, text=True, env=dict(os.environ, QA_MODAL_HELPER=str(SCRIPTS / 'remote-qa-modal.py')))
    return r.returncode, (r.stdout + r.stderr).strip()
rc, o = dry('--disposable-convex', '--dry-run')
check('spec default (no flag) resolves llm_mode=capture-on-miss with the auto-approved $3 cap (Rajiv ts 1795445281.923019)',
      rc == 0 and o.endswith('llm_mode=capture-on-miss onmiss_cap_usd=3 onmiss_fixture_bucket=heydonna-fixtures onmiss_scope=run+attempt onmiss_headroom=pre-lane app_data=ci-parity app_data_bucket=heydonna-uploads'), o[-120:])
rc, o = dry('--disposable-convex', '--on-miss', '--dry-run')
ONMISS_TAIL = ('llm_mode=capture-on-miss onmiss_cap_usd=3 onmiss_fixture_bucket=heydonna-fixtures '
               'onmiss_scope=run+attempt onmiss_headroom=pre-lane app_data=ci-parity app_data_bucket=heydonna-uploads')
check('spec --on-miss resolves llm_mode=capture-on-miss', rc == 0 and o.endswith(ONMISS_TAIL), o[-100:])
rc, o = dry('--on-miss', '--dry-run')
check('--on-miss without --disposable-convex refused (synthetic-only bound)', rc == 2 and 'requires --disposable-convex' in o, o)
pf = Path(tempfile.mkdtemp()) / 'm.patch'; pf.write_text('diff --git a/tests/e2e/a.ts b/tests/e2e/a.ts\n--- a/tests/e2e/a.ts\n+++ b/tests/e2e/a.ts\n')
rc, o = dry('--disposable-convex', '--on-miss', '--mutate-patch', str(pf), '--dry-run')
check('--on-miss refuses RED/mutation legs (RED stays strict)', rc == 2 and 'GREEN-only' in o, o)
# M15 scoped exact-hash mode: opt-in only, replaces stock --on-miss, never without a passed canary receipt.
HX = 'f3dcdecd3e7f1a8b3ccd790d2c9688af6c4febc811c71ada80e3a2b3608c4a80'
KX = 'fixtures/llm/v2/gemini_vertex/aiplatform.googleapis.com/gemini-3.1-flash-lite.stream/' + HX + '.bin'
def manifest(**over):
    doc = {'version': 1, 'profile': 'm15-langsmith', 'head': h40, 'product_pin': 'e' * 40, 'approval_ref': 'C0ALZJHGE49/1795301055.769519', 'max_reserve_usd': 0.65,
           'langsmith': {'draft_sha256': '04606bdb97e2616e0d9ed943e7c4860408c769c7344f6606082b0d5335d3ad29',
                         'aligned_slice_sha256': '3d5adf320e3e151cffd1a1b66d510ebdab7ccd098174c40be75cb61b2ed62a76'},
           'hashes': [{'hash': HX, 'provider': 'gemini_vertex', 'model': 'gemini-3.1-flash-lite', 'operation': 'stream', 'canonical_key': KX}]}
    doc.update(over)
    p = Path(tempfile.mkdtemp()) / 'exact.json'
    p.write_text(json.dumps(doc))
    return str(p)
good = manifest()
rc, o = dry('--disposable-convex', '--on-miss-exact', good, '--canary-receipt', '/nonexistent', '--grant-receipt', '/nonexistent', '--dry-run')
EXACT_TAIL = ('llm_mode=capture-on-miss-exact onmiss_cap_usd=3 onmiss_fixture_bucket=heydonna-fixtures '
              'onmiss_scope=run+attempt onmiss_headroom=pre-lane app_data=ci-parity app_data_bucket=heydonna-uploads')
check('spec --on-miss-exact resolves llm_mode=capture-on-miss-exact', rc == 0 and o.endswith(EXACT_TAIL), o[-100:])
rc, o = dry('--disposable-convex', '--on-miss', '--on-miss-exact', good, '--canary-receipt', '/nonexistent', '--grant-receipt', '/nonexistent', '--dry-run')
check('--on-miss-exact replaces stock --on-miss (never both)', rc == 2 and 'replaces --on-miss' in o, o[-80:])
rc, o = dry('--disposable-convex', '--on-miss-exact', good, '--dry-run')
check('--on-miss-exact refused without --canary-receipt', rc == 2 and 'requires --canary-receipt' in o, o[-80:])
rc, o = dry('--disposable-convex', '--on-miss-exact', good, '--canary-receipt', '/nonexistent', '--dry-run')
check('--on-miss-exact refused without --grant-receipt (the Run 9 object-only grant)', rc == 2 and 'requires --grant-receipt' in o, o[-80:])
rc, o = dry('--disposable-convex', '--on-miss-exact', good, '--canary-receipt', '/nonexistent', '--grant-receipt', '/nonexistent', '--exact-dry-check', '--dry-run')
check('--exact-dry-check resolves llm_mode=capture-on-miss-exact', rc == 0 and o.endswith(EXACT_TAIL), o[-100:])
rc, o = dry('--disposable-convex', '--on-miss', '--exact-dry-check', '--dry-run')
check('--exact-dry-check is only valid with --on-miss-exact', rc == 2 and 'only valid with --on-miss-exact' in o, o[-80:])
bad_pin = manifest(product_pin='e' * 12)
rc, o = dry('--disposable-convex', '--on-miss-exact', bad_pin, '--canary-receipt', '/nonexistent', '--grant-receipt', '/nonexistent', '--dry-run')
check('--on-miss-exact refuses a manifest without a full product pin', rc == 2 and 'QA_EXACT_MANIFEST_REFUSED' in o, o[-80:])
rc, o = dry('--disposable-convex', '--on-miss-exact', manifest(max_reserve_usd=9), '--canary-receipt', '/nonexistent', '--grant-receipt', '/nonexistent', '--dry-run')
check('--on-miss-exact refuses a max_reserve_usd above the $3 cap', rc == 2 and 'QA_EXACT_MANIFEST_REFUSED' in o, o[-80:])
rc, o = dry('--disposable-convex', '--canary-receipt', '/nonexistent', '--dry-run')
check('--canary-receipt alone is refused', rc == 2 and 'only valid with --on-miss-exact' in o, o[-80:])
rc, o = dry('--on-miss-exact', good, '--canary-receipt', '/nonexistent', '--grant-receipt', '/nonexistent', '--dry-run')
check('--on-miss-exact without --disposable-convex refused (synthetic-only bound)', rc == 2 and '--disposable-convex' in o, o[-80:])
for why, bad in (('a 12-char hash prefix', manifest(hashes=[{'hash': HX[:12], 'provider': 'gemini_vertex', 'model': 'gemini-3.1-flash-lite', 'operation': 'stream', 'canonical_key': KX}])),
                 ('a manifest for another head', manifest(head='b' * 40))):
    rc, o = dry('--disposable-convex', '--on-miss-exact', bad, '--canary-receipt', '/nonexistent', '--grant-receipt', '/nonexistent', '--dry-run')
    check('--on-miss-exact refuses ' + why, rc == 2 and 'QA_EXACT_MANIFEST_REFUSED' in o, o[-80:])
rc, o = dry('--disposable-convex', '--on-miss-exact', good, '--canary-receipt', '/nonexistent', '--grant-receipt', '/nonexistent', '--mutate-patch', str(pf), '--dry-run')
check('--on-miss-exact is GREEN-only like stock on-miss', rc == 2 and 'GREEN-only' in o, o[-80:])
# ---- explicit proof PROFILES (CTO GO C0ALZJHGE49 thread 1795302375.270589) ----
def pmanifest(**over):
    doc = {'version': 1, 'profile': 'pr14414-headerless-capture', 'head': P14HEAD, 'product_pin': P14HEAD, 'approval_ref': 'C0ALZJHGE49/1795302375.270589/1795310865.915209',
           'max_reserve_usd': 0.65, 'hashes': [{'hash': P14HASH, 'provider': 'gemini_vertex', 'model': 'gemini-3.1-flash-lite', 'operation': 'stream',
                                                   'canonical_key': 'fixtures/llm/v2/gemini_vertex/aiplatform.googleapis.com/gemini-3.1-flash-lite.stream/' + P14HASH + '.bin'}]}
    doc.update(over)
    p = Path(tempfile.mkdtemp()) / 'profile.json'
    p.write_text(json.dumps(doc))
    return str(p)
RECEIPTS = ('--canary-receipt', '/nonexistent', '--grant-receipt', '/nonexistent', '--dry-run')
rc, o = dry('--disposable-convex', '--on-miss-exact', pmanifest(), *RECEIPTS, head=P14HEAD, spec=P14SPEC)
check('PR 14414 profile: the pinned head + pinned spec + approved hash resolve llm_mode=capture-on-miss-exact', rc == 0 and o.endswith(EXACT_TAIL), o[-100:])
for why, kwargs, manifest_path in (('another spec', dict(head=P14HEAD, spec=M15SPEC), pmanifest()), ('another head', dict(head='b' * 40, spec=P14SPEC), pmanifest(head='b' * 40, product_pin='b' * 40)),
                                   ('another hash', dict(head=P14HEAD, spec=P14SPEC), pmanifest(hashes=[{'hash': HX, 'provider': 'gemini_vertex', 'model': 'gemini-3.1-flash-lite', 'operation': 'stream', 'canonical_key': KX}])),
                                   ('a langsmith block (no provenance toggle: the shape is fixed per profile)', dict(head=P14HEAD, spec=P14SPEC), pmanifest(langsmith={'draft_sha256': '1' * 64, 'aligned_slice_sha256': '2' * 64})),
                                   ('a missing profile', dict(head=P14HEAD, spec=P14SPEC), pmanifest(profile=None)), ('an unknown profile', dict(head=P14HEAD, spec=P14SPEC), pmanifest(profile='pr9999-anything'))):
    if 'a missing profile' == why:
        doc = json.loads(Path(manifest_path).read_text()); doc.pop('profile'); Path(manifest_path).write_text(json.dumps(doc))
    rc, o = dry('--disposable-convex', '--on-miss-exact', manifest_path, *RECEIPTS, **kwargs)
    check('PR 14414 profile refuses ' + why, rc == 2 and 'QA_EXACT_MANIFEST_REFUSED' in o, o[-80:])
rc, o = dry('--disposable-convex', '--on-miss-exact', good, *RECEIPTS, spec=P14SPEC)
check('M15 profile refuses the PR 14414 spec', rc == 2 and 'QA_EXACT_MANIFEST_REFUSED' in o, o[-80:])
# ---- M15 spec inputs staged by the CONTROLLER + the controller-selected app-data bucket (coordinator add-on 2026-11-22) ----
import hashlib
m15dir = Path(tempfile.mkdtemp())
(m15dir / 'draft.txt').write_bytes(b'Q. Aligned draft.\nA. Yes.\n')
(m15dir / 'observe.sh').write_text('#!/usr/bin/env bash\necho observer\n')
(m15dir / 'empty.sh').write_bytes(b'')
(m15dir / 'big.sh').write_bytes(b'x' * 262145)
(m15dir / 'other.txt').write_bytes(b'not the pinned draft')
os.symlink(m15dir / 'draft.txt', m15dir / 'link.txt')
pinned = manifest(langsmith={'draft_sha256': hashlib.sha256((m15dir / 'draft.txt').read_bytes()).hexdigest(),
                             'aligned_slice_sha256': '3d5adf320e3e151cffd1a1b66d510ebdab7ccd098174c40be75cb61b2ed62a76'})
M15 = lambda d, o: ('--m15-draft', str(m15dir / d), '--m15-observer', str(m15dir / o))
rc, o = dry('--disposable-convex', '--on-miss-exact', pinned, *M15('draft.txt', 'observe.sh'), *RECEIPTS)
check('M15 stage: the manifest-pinned draft + an observer resolve llm_mode=capture-on-miss-exact', rc == 0 and o.endswith(EXACT_TAIL), o[-100:])
for why, args, manifest_path in (('a draft that is not the manifest-pinned bytes', M15('other.txt', 'observe.sh'), pinned),
                                 ('a symlinked draft', M15('link.txt', 'observe.sh'), pinned),
                                 ('an empty observer', M15('draft.txt', 'empty.sh'), pinned),
                                 ('an oversize observer', M15('draft.txt', 'big.sh'), pinned),
                                 ('a missing observer file', M15('draft.txt', 'nope.sh'), pinned),
                                 ('a draft when the manifest pins another digest', M15('draft.txt', 'observe.sh'), good)):
    rc, o = dry('--disposable-convex', '--on-miss-exact', manifest_path, *args, *RECEIPTS)
    check('M15 stage refuses ' + why, rc == 2 and 'QA_M15_STAGE_REFUSED' in o, o[-80:])
rc, o = dry('--disposable-convex', '--on-miss-exact', pinned, '--m15-draft', str(m15dir / 'draft.txt'), *RECEIPTS)
check('M15 stage: the draft and the observer go together', rc == 2 and 'go together' in o, o[-80:])
rc, o = dry('--disposable-convex', *M15('draft.txt', 'observe.sh'), '--dry-run')
check('M15 stage flags are only valid with --on-miss-exact', rc == 2 and 'only valid with --on-miss-exact' in o, o[-80:])
rc, o = dry('--disposable-convex', '--on-miss-exact', pmanifest(), *M15('draft.txt', 'observe.sh'), *RECEIPTS, head=P14HEAD, spec=P14SPEC)
check('M15 stage flags are refused for any profile but m15-langsmith', rc == 2 and 'only valid with the m15-langsmith profile' in o, o[-80:])
check('spec env: the controller records the CI-parity app-data mode and bucket it selected (rule 65: heydonna-qa retired as app data; QA_R2_SCOPED selects nothing), never echoed back from R2_BUCKET_NAME',
      'app_data=scoped app_data_bucket=heydonna-qa' not in SPEC_SH and 'app_data=ci-parity app_data_bucket=heydonna-uploads' in SPEC_SH
      and 'spec_env="QA_APP_DATA=$app_data QA_APP_DATA_BUCKET=$app_data_bucket"' in SPEC_SH and 'val R2_BUCKET_NAME' not in SPEC_SH)
check('spec env: written to the .env.local.next copy BEFORE the install (the pool-reset byte-hash covers it) and must match the box readback',
      SPEC_SH.index('QA_APP_DATA_BUCKET=$app_data_bucket"') < SPEC_SH.index("mv '$remote/.env.local.next'")
      and 'QA_APP_DATA_READBACK mode=$app_data bucket=$app_data_bucket ' in SPEC_SH
      and SPEC_SH.index('check-app-data --env') < SPEC_SH.index('QA_APP_DATA_READBACK mode=$app_data bucket=$app_data_bucket '))
check('M15 stage: fixed box paths, digest re-verified on the box, exported to the spec env; the launcher stages files only',
      all(s in SPEC_SH for s in ('/root/qa/m15/draft.txt', '/root/qa/m15/observe.sh', 'QA_M15_DRAFT_PATH=/root/qa/m15/draft.txt', 'QA_M15_OBSERVER_PATH=/root/qa/m15/observe.sh',
                                 'QA_M15_OBSERVER_LOG=/root/qa/m15/modal.raw', "sha256sum </root/qa/m15/draft.txt", 'bash -n /root/qa/m15/observe.sh', 'does not run the observer'))
      and SPEC_SH.index('check-m15-stage') < SPEC_SH.index('rtb" up --owner'))
check('exact wiring: the profile spec BLOB is verified on the box; capture is reported separate from the spec AC1 PASS',
      'QA_SPEC_BLOB ok' in SPEC_SH and 'spec_ac_pass=NOT_ASSESSED' in SPEC_SH and 'EXACT_PROFILE name=' in SPEC_SH
      and not any(s in SPEC_SH for s in ('--skip-provenance', '--no-langsmith', '--no-join', 'skip_provenance')))
check('llm_onmiss starts empty; set by the --on-miss/--on-miss-exact flags or the DEFAULT fill (GREEN, non-CI, no strict/exact/RED); CI never sets it',
      "llm_onmiss='' stripe_test_env='' pre_write_check=''" in SPEC_SH
      and len(re.findall(r'^\s+--on-miss[a-z-]*\) .*llm_onmiss=--on-miss', SPEC_SH, re.M)) == 2
      and '--on-miss) llm_onmiss=--on-miss; onmiss_stock=1; shift;;' in SPEC_SH
      and 'if [ -z "$llm_onmiss$strict_manifest$exact_manifest" ]' in SPEC_SH
      and '"${CI:-0}" != 1' in SPEC_SH)
si = {name: SPEC_SH.find(name) for name in ('QA_ONMISS_EXACT_BOUND', 'scoped_arm_loop & arm_pid=$!', 'mint-session --kind report', 'qa_spec_fetch >>"$out/fetch.log"')}
cs, ce = SPEC_SH.index('cleanup() {'), SPEC_SH.index('trap cleanup EXIT')
check('exact wiring: sessions armed only after prepare bound the manifest digest, report session minted in cleanup (after the leg), no shared secret anywhere',
      all(v > 0 for v in si.values()) and si['QA_ONMISS_EXACT_BOUND'] < si['scoped_arm_loop & arm_pid=$!'] and cs < si['mint-session --kind report'] < ce
      and 'heydonna-r2-credentials' not in SPEC_SH and 'LLM_FIXTURE_R2' not in SPEC_SH and 'R2_SECRET' not in SPEC_SH, str(si))
up_at = SPEC_SH.index('rtb" up --owner')
chk = {name: SPEC_SH.index(name) for name in ('check-grant-receipt', 'app_data=ci-parity', 'check-app-data', 'check-exact-ids', 'check-app-mounts', 'reconcile-custody', 'scoped_arm_loop & arm_pid=$!')}
check('REVISE 1 / option A: the app-data mode (scoped | CI-parity by Rajiv decision) is decided explicitly before any lease, box or spend; the grant receipt is still required',
      chk['check-grant-receipt'] < up_at and chk['app_data=ci-parity'] < up_at and 'QA_R2_SCOPED' in SPEC_SH and 'QA_APP_DATA_SCOPE_REQUIRED' not in SPEC_SH
      and 'option A' in SPEC_SH and '1795311474.240329' in SPEC_SH, str(chk))
check('option A: CI-parity app data skips ONLY the mount replacement/readback; the readback is mode-aware and the mode is recorded in lane.txt',
      '[ "$app_data" = scoped ]' in SPEC_SH and "--app-data '$app_data'" in SPEC_SH and 'QA_APP_MOUNTS mode=ci-parity' in SPEC_SH and 'by Rajiv decision' in SPEC_SH
      and 'exact_args="$exact_args --app-data $app_data"' in SPEC_SH)
check('REVISE 1: box env readback, trusted-derivation check (DENIED), shared-mount readback and custody reconcile all precede the first session',
      chk['check-app-data'] < chk['check-exact-ids'] < chk['check-app-mounts'] < chk['reconcile-custody'] < chk['scoped_arm_loop & arm_pid=$!'] and 'product-pin.log' in SPEC_SH, str(chk))
check('REVISE 1: every mint passes the trusted lease, physical box and head (never only a box-written file)',
      len(re.findall(r'mint-session --kind (?:proxy|report) --box "\$box" --head "\$head" --spend-box "\$exact_spend_box" --run-id "\$run_id" --ids', SPEC_SH)) == 2
      and 'exact_spend_box="${pool_box:-$box}"' in SPEC_SH)
check('REVISE 2: custody reconcile runs on the trusted host (uv + boto3) and the launcher itself never touches the legacy read key',
      'reconcile-custody --box "$box" --head "$head" --spend-box "$exact_spend_box" --run-id "$run_id"' in SPEC_SH and 'LEGACY_ENV' not in SPEC_SH and 'R2_SECRET' not in SPEC_SH)
check('REVISE 3: the join is verified by the helper (project, transcript, aligned draft), not accepted from the spec file',
      'verify-join --report' in SPEC_SH and 'JOIN\n' not in SPEC_SH)
check('exact wiring: the manifest and canary receipt are validated before any lease, box or spend',
      SPEC_SH.index('check-exact-manifest') < SPEC_SH.index('rtb" up --owner') and SPEC_SH.index('check-canary-receipt') < SPEC_SH.index('rtb" up --owner')
      and SPEC_SH.index('mint-session --kind check') < SPEC_SH.index('rtb" up --owner'))
for cmd in ('register --box "$box" --ttl "$ttl_seconds" $llm_onmiss',
            "modal-lane.py overlay --root '$remote' --box '$box' --run-id '$run_id' $llm_onmiss",
            "modal-lane.py prepare --box '$box' --root '$remote' --config /root/qa/config.json --run-id '$run_id' $llm_onmiss",
            "modal-lane.py capture-report --box '$box' --root '$remote' --output /root/qa/out/llm-ledger.json --run-id '$run_id' $llm_onmiss"):
    check('spec passes the flag to the shared controller: ' + cmd.split(' --')[0].split()[-1], cmd in SPEC_SH)
check('capture lane uses the same controller commands (register/overlay/prepare/capture-report + $llm_onmiss)',
      all(s in CAPTURE_SH for s in ('modal-lane.py overlay', 'modal-lane.py prepare', 'modal-lane.py capture-report', '$llm_onmiss')))
check('spec runtime keeps capture OFF in BOTH modes; the Playwright STRICT_FIXTURES is derived from the SELECTED llm_mode (strict=true 180/300, onmiss=false 600/720) and baked into the npx shim',
      'export LLM_PROXY_ENABLED=true LLM_PROXY_FIXTURE_CAPTURE=false' in RUNTIME_SH and 'LLM_PROXY_FIXTURE_CAPTURE=true' not in RUNTIME_SH
      and 'if [ "$llm_mode" = onmiss ]; then export STRICT_FIXTURES=false; else export STRICT_FIXTURES=true; fi' in RUNTIME_SH
      and 'write_npx_shim "$qa/bin/npx" "$real_npx" "$([ "$llm_mode" = onmiss ] && echo onmiss || echo strict)"' in RUNTIME_SH
      and 'QA_TIMEOUT_MODE_MISMATCH' in RUNTIME_SH and 'exp_pipeline_ms=600000; exp_test_ms=720000' in RUNTIME_SH)
check('no CI workflow or UserPromptSubmit hook referenced by the change',
      not any(s in SPEC_SH + RUNTIME_SH for s in ('.github/workflows', 'UserPromptSubmit')))

# ---- B. proxy behaviour (deployed overlay) ----
SPEC_BOX = 'rtbl-5pec000000000000-101010'.replace('5pec', '5bec')  # hex lease alias
POOL_HOST = 'rtb-pool-08-090909'
HEADX = 'c' * 40
RUN_B = 'qa-spec-20261124T010101Z-20001'  # ONE QA run: its capture and spec legs share the run's $3 (Rajiv ts 1795459248.640279 "per run")
IDS_CAP = ids_for('rtbl-ca00000000000000-111111', POOL_HOST, head=HEADX, run_id=RUN_B)  # capture-lane lease on the same physical box + head, same run
IDS_SPEC = ids_for(SPEC_BOX, POOL_HOST, head=HEADX, run_id=RUN_B)                        # spec-lane lease, same physical box + head, same run
check('capture and spec in the same QA run (same box + head) bind the SAME durable ledgers',
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
IDS_REP = ids_for('rtb-qa-specrep-202020', 'rtb-qa-specrep-202020', head=HEADX, run_id=RUN_B)
m, cr = boot(IDS_REP, box='rtb-qa-specrep-202020')
r = call(cr, 'S-REP')
check('replacement box, same run + attempt: the run cap still applies (402, 0 upstream)',
      r.status_code == 402 and n_calls('S-REP') == 0 and IDS_REP['attempt_key'] == IDS_SPEC['attempt_key'] and IDS_REP['box_key'] == IDS_SPEC['box_key'])

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
IDS_U2 = ids_for('rtbl-d100000000000000-404040', 'rtb-pool-10-303030', head='d' * 40)  # new lease, same box + head, same run (default run is per host)
m, cu2 = boot(IDS_U2, box='rtbl-d100000000000000-404040')
r = call(cu2, 'S-UNK DROP', max_out=1000)
check('new lease, same box+head: UNKNOWN still blocks (no blind resend, no reset)',
      r.status_code in (402, 424) and n_calls('S-UNK') == 1 and committed(IDS_U['attempt_key']) == u_before,
      '%d %s' % (r.status_code, r.json().get('error')))

# 5. zero canonical writes
check('S161: on-miss stages replies (with .request sidecars) and never writes canonical directly', not [k for k in canon() if k.startswith('fixtures/')] and all(k.startswith('staging/llm-capture/remote-qa-') for k in canon()) and any(k.endswith('.request') for k in canon()), str(canon()[:3]))

# ---- C. controller report + verdict labelling (spec controller's own onmiss_label) ----
# S161 (2026-11-19): on-miss qa_finish DOES run the guarded publisher over this run's own staging prefix. These report checks use an
# empty staging set, so nothing is published; the S161 publication behaviour itself is proven in remote-qa-onmiss-stub.test.py.
boom = {'collect_plan': lambda s3, bucket, run_id: ([], 0), 'publish_plan': lambda s3, bucket, plan, replace_allowlist=None: (0, 0, 0)}
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
s_spec = H.qa_finish(FakeS3(), 'b', 'x', False, boom, dict(IDS_SPEC, lease=H.identity(SPEC_BOX)))
check('controller report (spec lease) sees its live capture(s), nothing staged so nothing published',
      len(s_spec['attempts']) >= 1 and s_spec['canonical_fixture_keys'] == [] and s_spec['fixtures_written'] == [], str(len(s_spec['attempts'])))
clean = dict(s_spec, terminal_reason=None)
o = label(clean)
check('run with a live capture is labelled ON_MISS_CAPTURED (never lane-ok / strict PASS)', o.startswith('ON_MISS_CAPTURED|ON_MISS_CAPTURED|0||'), o)
o = label(dict(clean, attempts={}))
check('on-miss run with zero live calls is lane-ok-onmiss-replay-only', o.startswith('lane-ok-onmiss-replay-only|ON_MISS_REPLAY_ONLY|0'), o)
o = label(clean, 'QA_ONMISS_TERMINAL reason=QA_ONMISS_NO_RESEND\n')
check('typed QA_ONMISS_* terminal -> lane-fail reason carried', o.startswith('lane-fail|ON_MISS_TERMINAL|1|QA_ONMISS_NO_RESEND'), o)
def label_exact(summary, join, match):
    d = Path(tempfile.mkdtemp()); (d / 'out').mkdir()
    (d / 'out/llm-ledger.json').write_text(json.dumps(dict(summary, exact={'readback_match': match})))
    (d / 'llm-join.txt').write_text('QA_ONMISS_JOIN_RESULT status=%s dispatches=1\n' % join)
    return subprocess.run(['bash', '-c', 'exact_manifest=m.json\n' + fn + '\nonmiss_label "$1" 0; echo "$ONMISS_VERDICT|$ONMISS_LABEL|$ONMISS_RC|$ONMISS_REASON"', '_', str(d)],
                          capture_output=True, text=True).stdout.strip()
o = label_exact(clean, 'COMPLETE', 1)
check('exact: live capture with canonical readback MATCH and a COMPLETE join is ON_MISS_CAPTURED', o.startswith('ON_MISS_CAPTURED|ON_MISS_CAPTURED|0|'), o)
o = label_exact(clean, 'COMPLETE', 0)
check('exact: a live call without a matching canonical readback is lane-fail QA_ONMISS_CAPTURE_UNPROVEN', o == 'lane-fail|ON_MISS_TERMINAL|1|QA_ONMISS_CAPTURE_UNPROVEN', o)
o = label_exact(clean, 'LANGSMITH_UNJOINED', 1)
check('exact: no LangSmith join is lane-fail QA_ONMISS_JOIN_UNPROVEN (capture stays published)', o == 'lane-fail|ON_MISS_TERMINAL|1|QA_ONMISS_JOIN_UNPROVEN', o)
o = label_exact(dict(clean, attempts={}), 'NO_DISPATCH', 0)
check('exact: zero live calls stays lane-ok-onmiss-replay-only (an empty publication plan is not capture proof)', o.startswith('lane-ok-onmiss-replay-only|ON_MISS_REPLAY_ONLY|0'), o)
o = label(clean, report=False)
check('missing controller report fails closed', o.startswith('lane-fail|ON_MISS_UNVERIFIED|1|QA_ONMISS_REPORT_MISSING'), o)
o = label(dict(clean, canonical_fixture_keys=['fixtures/x'], fixtures_written=['fixtures/x']))
check('S161: a report with guarded canonical publication is still ON_MISS_CAPTURED (the pre-S161 zero-canonical-write pin is retired)',
      o.startswith('ON_MISS_CAPTURED|ON_MISS_CAPTURED|0|'), o)
try:
    H.emit_report(H.qa_finish(FakeS3(), 'b', 'x', False, boom, dict(IDS_U, lease=H.identity('rtbl-d000000000000000-303030'))), 'x')
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
