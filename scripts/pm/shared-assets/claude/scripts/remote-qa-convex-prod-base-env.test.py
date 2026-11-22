#!/usr/bin/env python3
"""Self-test: remote-qa-convex.py create REFUSES a production base env before its first management API request.

CTO ruling C0ALZJHGE49 ts 1795033007.683369 (2026-11-19): slot 5 may not become the default browser-QA base env until
`create --base-env F` refuses a base env naming PROD (beloved-cardinal-941), another known shared marker, or a prod deploy
key, BEFORE any management request. The old guard only checked the created deployment receipt AFTER create_deployment.

Everything here is SYNTHETIC: sentinel env files carry fake values ("SECRETVALUE"), the management API and the deployment
API are a fake server on 127.0.0.1 (API/deployment_url/node_probe patched, HOME redirected to a temp dir with a fake token),
and three independent spies (urlopen, request, deployment_request) count calls. Nothing reaches Convex, no real env file is read.
CTO REVISE C0ALZJHGE49 ts 1795033518.676029 (round 2): create() must read the base env path EXACTLY ONCE, validate that text,
and hand the same validated text to create_once() for storage selection and rendering, across every retry. Sections 5-6
prove it: the file is replaced by a PROD sentinel right after validation and inside every unhealthy node probe (between
retries); an exact read-count spy on the path must see one read; every request body the fake server receives is scanned for
replacement bytes. A prod snapshot still makes zero API calls. (Section 7, the retired scoped-storage guards, was removed with
the separate QA bucket: rule 65, one R2 account, bucket and key.)
Run:  python3 -I ~/.claude/scripts/remote-qa-convex-prod-base-env.test.py
Red-on-revert (must FAIL, rc != 0), against the guard1 version that re-read the path in create_once():
      QA_CONVEX_HELPER=~/.claude/scripts/remote-qa-convex.py.bak-20261119-single-read-base-env python3 -I <this file>
"""
import builtins
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HELPER = Path(os.environ.get('QA_CONVEX_HELPER') or Path(__file__).with_name('remote-qa-convex.py')).expanduser()
NAME, KEY, MGMT = 'happy-otter-123', 'dev:happy-otter-123|fakekey', 'fake-management-token'
SENTINELS = ('SECRETVALUE', 'beloved-cardinal-941', 'loyal-lynx-104', 'prod-otter-1')
STATE = {'log': [], 'deployment': None, 'env': {}, 'bodies': []}
CALLS = {'urlopen': 0, 'request': [], 'deployment_request': []}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body=b''):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_any(self, method):
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        STATE['log'].append((method, self.path))
        STATE['bodies'].append(body.decode('utf-8', 'replace'))
        auth = self.headers.get('Authorization')
        if self.path.startswith('/v1/'):
            if auth != 'Bearer ' + MGMT:
                return self.reply(401)
            if self.path.endswith('/create_deployment'):
                req = json.loads(body)
                STATE['deployment'] = dict(name=NAME, deploymentType='dev', projectId=634554, reference=req['reference'],
                                           isDefault=False, expiresAt=req['expiresAt'], kind='cloud',
                                           deploymentUrl=BASE + '/dep', createTime=1)
                return self.reply(200, {'name': NAME, 'createTime': 1})
            if self.path == f'/v1/deployments/{NAME}' and method == 'GET':
                return self.reply(200, STATE['deployment']) if STATE['deployment'] else self.reply(404)
            if self.path.endswith('/create_deploy_key'):
                return self.reply(200, {'deployKey': KEY})
            if self.path == f'/v1/deployments/{NAME}/delete' and method == 'POST':
                STATE['deployment'] = None  # delete_owned() on an unhealthy node probe; the next GET must 404
                return self.reply(200, b'')
            return self.reply(404)
        if auth != 'Convex ' + KEY:
            return self.reply(401)
        if self.path == '/dep/api/update_environment_variables' and method == 'POST':
            STATE['env'].update({c['name']: c['value'] for c in json.loads(body)['changes']})
            return self.reply(200, b'')
        if self.path == '/dep/api/v1/list_environment_variables' and method == 'GET':
            return self.reply(200, {'environmentVariables': dict(STATE['env'])})
        return self.reply(404)

    def do_GET(self):
        self.handle_any('GET')

    def do_POST(self):
        self.handle_any('POST')


server = ThreadingHTTPServer(('127.0.0.1', 0), Fake)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f'http://127.0.0.1:{server.server_address[1]}'
TMP = Path(tempfile.mkdtemp(prefix='qa-convex-prod-base-env-test-'))
os.environ['HOME'] = str(TMP / 'home')
(TMP / 'home/.convex').mkdir(parents=True)
(TMP / 'home/.convex/config.json').write_text(json.dumps({'accessToken': MGMT}))

# Load by loader (not by suffix) so the same test can run against a `.bak-...` copy for red-on-revert.
loader = importlib.machinery.SourceFileLoader('qa_convex', str(HELPER))
spec = importlib.util.spec_from_loader('qa_convex', loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
mod.API = BASE + '/v1'
mod.deployment_url = lambda name: BASE + '/dep'
mod.node_probe = lambda key: (True, 0, 0)

real_urlopen, real_request, real_deployment_request = urllib.request.urlopen, mod.request, mod.deployment_request


def spy_urlopen(*args, **kwargs):
    CALLS['urlopen'] += 1
    return real_urlopen(*args, **kwargs)


def spy_request(path, data=None):
    CALLS['request'].append(path)
    return real_request(path, data)


def spy_deployment_request(url, key, path, data=None):
    CALLS['deployment_request'].append(path)
    return real_deployment_request(url, key, path, data)


urllib.request.urlopen = spy_urlopen
mod.request = spy_request
mod.deployment_request = spy_deployment_request
passed = 0


def reset():
    STATE.update(log=[], deployment=None, env={}, bodies=[])
    CALLS.update(urlopen=0, request=[], deployment_request=[])


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = mod.cli(argv)
    return rc, out.getvalue(), err.getvalue()


def check(label, condition):
    global passed
    if not condition:
        raise SystemExit(f'FAIL {label}\nfake-server-log={STATE["log"]} spies={CALLS}')
    passed += 1
    print(f'PASS {label}')


def new_root(tag):
    root = TMP / tag
    root.mkdir()
    return root


def env_text(lines):
    return '\n'.join(['PORT=3002', 'OPENAI_API_KEY=sk-SECRETVALUE-openai', *lines]) + '\n'


def write_env(tag, lines):
    path = TMP / f'{tag}.env'
    path.write_text(env_text(lines))
    return path


def swap(path, lines):
    """Atomically replace the base env FILE (new inode, like a CI step or editor rewriting it) with different bytes."""
    staged = path.with_name(path.name + '.swap')
    staged.write_text(env_text(lines))
    os.replace(staged, path)


# Exact read-count spy. Path.read_text/read_bytes/open and open() all go through io.open, so every read of a path is seen.
READS = []
real_io_open = io.open


def spy_open(file, *args, **kwargs):
    if not isinstance(file, int):
        READS.append(os.path.realpath(os.fsdecode(os.fspath(file))))
    return real_io_open(file, *args, **kwargs)


@contextlib.contextmanager
def watching():
    READS.clear()
    io.open = builtins.open = spy_open
    try:
        yield
    finally:
        io.open = builtins.open = real_io_open


def read_count(path):
    return READS.count(os.path.realpath(path))


def no_calls():
    return STATE['log'] == [] and CALLS == {'urlopen': 0, 'request': [], 'deployment_request': []}


def refuses(tag, lines, names, extra_args=()):
    """create on a base env made of `lines` refuses with exactly these NAMES, zero API calls, nothing written."""
    reset()
    root = new_root(tag)
    rc, out, err = run(['create', '--root', str(root), '--base-env', str(write_env(tag, lines)), *extra_args])
    expected = f'QA_CONVEX_LIFECYCLE_FAILED class=QaRefusal reason=BASE_ENV_PRODUCTION_CONVEX names={",".join(sorted(names))}\n'
    check(f'{tag}: refused names={",".join(sorted(names))}, rc1, no API call, no intent.json or any file written',
          rc == 1 and err == expected and no_calls() and not any(root.iterdir()))
    check(f'{tag}: no value (secret or deployment name) in output', not any(s in out + err for s in SENTINELS))


# 1. Full synthetic PROD sentinel env (what a prod .env.local looks like): refused, ZERO management/deployment calls.
PROD_FULL = ['NEXT_PUBLIC_CONVEX_URL=https://beloved-cardinal-941.convex.cloud', 'CONVEX_URL=https://beloved-cardinal-941.convex.cloud',
             'CONVEX_DEPLOYMENT=prod:beloved-cardinal-941', 'CONVEX_DEPLOY_KEY=prod:beloved-cardinal-941|SECRETVALUE-prod-deploy-key',
             'CLERK_SECRET_KEY=sk_live_SECRETVALUE-clerk']
refuses('prod-full', PROD_FULL, ['NEXT_PUBLIC_CONVEX_URL', 'CONVEX_URL', 'CONVEX_DEPLOYMENT', 'CONVEX_DEPLOY_KEY'])
# Also with --stripe-test-env: the production refusal comes first, not a Stripe validation reason.
refuses('prod-full-stripe', PROD_FULL, ['NEXT_PUBLIC_CONVEX_URL', 'CONVEX_URL', 'CONVEX_DEPLOYMENT', 'CONVEX_DEPLOY_KEY'],
        ['--stripe-test-env'])

# 2. Per-variable refusals, each alone in an otherwise dev base env.
DEV = ['NEXT_PUBLIC_CONVEX_URL=https://slot.convex.cloud', 'CONVEX_DEPLOYMENT=dev:slot-dev-1']
PROD_CASES = [
    ('url', ['CONVEX_URL=https://beloved-cardinal-941.convex.cloud'], ['CONVEX_URL']),
    ('public-url', ['NEXT_PUBLIC_CONVEX_URL=https://beloved-cardinal-941.convex.cloud'], ['NEXT_PUBLIC_CONVEX_URL']),
    ('site-url', ['CONVEX_SITE_URL=https://beloved-cardinal-941.convex.site'], ['CONVEX_SITE_URL']),
    ('public-site-url', ['NEXT_PUBLIC_CONVEX_SITE_URL=https://beloved-cardinal-941.convex.site'], ['NEXT_PUBLIC_CONVEX_SITE_URL']),
    ('deployment-typed', ['CONVEX_DEPLOYMENT=prod:beloved-cardinal-941'], ['CONVEX_DEPLOYMENT']),
    ('deployment-bare', ['CONVEX_DEPLOYMENT=beloved-cardinal-941'], ['CONVEX_DEPLOYMENT']),
    ('deployment-comment', ['CONVEX_DEPLOYMENT=prod:beloved-cardinal-941 # team: heydonna'], ['CONVEX_DEPLOYMENT']),
    ('deployment-prod-type-unknown-name', ['CONVEX_DEPLOYMENT=prod:renamed-otter-7'], ['CONVEX_DEPLOYMENT']),
    ('deployment-dev-typed-prod-name', ['CONVEX_DEPLOYMENT=dev:prod-otter-1'], ['CONVEX_DEPLOYMENT']),
    ('deploy-key', ['CONVEX_DEPLOY_KEY=prod:beloved-cardinal-941|SECRETVALUE-key'], ['CONVEX_DEPLOY_KEY']),
    ('deploy-key-prod-type-unknown-name', ['CONVEX_DEPLOY_KEY=prod:renamed-otter-7|SECRETVALUE-key'], ['CONVEX_DEPLOY_KEY']),
    ('admin-key', ['CONVEX_ADMIN_KEY=prod:beloved-cardinal-941|SECRETVALUE-key'], ['CONVEX_ADMIN_KEY']),
    ('shared-pm-dev', ['CONVEX_URL=https://loyal-lynx-104.convex.cloud'], ['CONVEX_URL']),
    ('prod-prefix', ['CONVEX_URL=https://prod-otter-1.convex.cloud'], ['CONVEX_URL']),
    ('quoted-export', ['export CONVEX_URL="https://beloved-cardinal-941.convex.cloud"'], ['CONVEX_URL']),
    ('lax-spacing', ['  CONVEX_DEPLOYMENT = prod:beloved-cardinal-941'], ['CONVEX_DEPLOYMENT']),
    ('other-variable-names-prod', ['CONVEX_HTTP_ACTIONS_URL=https://beloved-cardinal-941.convex.site/x'], ['CONVEX_HTTP_ACTIONS_URL']),
]
for tag, lines, names in PROD_CASES:
    refuses('prod-' + tag, lines, names)
# A dev entry cannot mask a production one: the duplicate prod entry is still seen (dev first, then prod).
refuses('prod-after-dev-duplicate', [*DEV, 'CONVEX_DEPLOYMENT=prod:beloved-cardinal-941'], ['CONVEX_DEPLOYMENT'])
# A production URL next to otherwise-valid dev entries (the realistic mis-pointed slot env).
refuses('prod-among-dev', [*DEV, 'CONVEX_URL=https://beloved-cardinal-941.convex.cloud'], ['CONVEX_URL'])

# 3. Dev base env proceeds to the (mocked) FIRST management call, which is create_deployment.
DEV_FULL = [*DEV, 'CONVEX_URL=https://slot.convex.cloud', 'CONVEX_SITE_URL=https://slot.convex.site',
            'CONVEX_DEPLOY_KEY=dev:slot-dev-1|SECRETVALUE-dev-key',
            # near-misses of the prod markers that must NOT be refused
            'NEXT_PUBLIC_NOTE=productive-otter-1', 'CONVEX_ADMIN_KEY=dev:productive-otter-1|SECRETVALUE-dev-admin']
reset()
root = new_root('dev-proceeds')
rc, out, err = run(['create', '--root', str(root), '--base-env', str(write_env('dev-proceeds', DEV_FULL))])
check('dev env: create rc0 and reports the disposable deployment', rc == 0 and 'QA_DISPOSABLE_CONVEX_CREATED=' + NAME in out)
check('dev env: first management API request is create_deployment (mocked), reached once',
      CALLS['request'][:1] == ['/projects/634554/create_deployment'] and CALLS['request'].count('/projects/634554/create_deployment') == 1
      and STATE['log'][:1] == [('POST', '/v1/projects/634554/create_deployment')])
check('dev env: intent.json written only after the guard passed', (root / 'intent.json').exists())
check('dev env: no secret value printed', 'SECRETVALUE' not in out + err)

# 4. The pure guard itself: None for dev, QaRefusal with a fixed name-only reason for prod; never echoes a value.
check('guard: dev text passes', mod.refuse_production_base_env('\n'.join(DEV_FULL) + '\n') is None)
check('guard: empty text passes', mod.refuse_production_base_env('') is None)
try:
    mod.refuse_production_base_env('\n'.join(PROD_FULL))
    check('guard: prod text raises', False)
except mod.QaRefusal as refusal:
    check('guard: prod text raises QaRefusal with names only',
          str(refusal) == 'BASE_ENV_PRODUCTION_CONVEX names=CONVEX_DEPLOYMENT,CONVEX_DEPLOY_KEY,CONVEX_URL,NEXT_PUBLIC_CONVEX_URL'
          and not any(s in str(refusal) for s in SENTINELS))

# 5. Single read, non-scoped (CI-parity) storage. The base file is replaced by a PROD sentinel after create() validated it.
# Bytes of the replacement (and the production markers it carries) must never reach the deployment or dev.env.
SNAP = [*DEV, 'R2_ACCOUNT_ID=snapacct', 'R2_BUCKET_NAME=snap-bucket', 'R2_ACCESS_KEY_ID=DEVSNAP-key-id',
        'R2_SECRET_ACCESS_KEY=DEVSNAP-secret', 'R2_ENDPOINT_URL=https://snapacct.r2.cloudflarestorage.com']
SWAPPED = [*PROD_FULL, 'R2_ACCOUNT_ID=SWAPPED-acct', 'R2_BUCKET_NAME=SWAPPED-bucket', 'R2_ACCESS_KEY_ID=SWAPPED-key-id',
           'R2_SECRET_ACCESS_KEY=SWAPPED-secret', 'R2_ENDPOINT_URL=https://SWAPPED.r2.cloudflarestorage.com',
           'SWAPPED_PROD_NOTE=beloved-cardinal-941']
SWAP_MARKERS = ('SWAPPED', 'beloved-cardinal-941', 'sk_live_')


def swap_run(tag, snapshot, replacement, *, after_validation=False, unhealthy_probes=0, seed_env=None):
    """create on `snapshot`; the file is atomically replaced by `replacement`
    (a) right after create() validated it: synth_changes, the last validation call, has just returned, and/or
    (b) inside each of the first `unhealthy_probes` node probes, i.e. between retries (the probe fails, delete_owned runs,
        the next attempt creates a new deployment)."""
    reset()
    if seed_env:
        STATE['env'].update(seed_env)
    root, path = new_root(tag), write_env(tag, snapshot)
    swaps, probes = [], []
    real_synth, real_probe = mod.synth_changes, mod.node_probe

    def synth_then_swap(original):
        result = real_synth(original)
        if after_validation:
            swap(path, replacement)
            swaps.append('validated')
        return result

    def probe(key):
        probes.append(1)
        if len(probes) <= unhealthy_probes:
            swap(path, replacement)
            swaps.append('retry')
            return False, 1, 1
        return True, 0, 0

    mod.synth_changes, mod.node_probe = synth_then_swap, probe
    try:
        with watching():
            rc, out, err = run(['create', '--root', str(root), '--base-env', str(path)])
    finally:
        mod.synth_changes, mod.node_probe = real_synth, real_probe
    return dict(rc=rc, out=out, err=err, root=root, path=path, swaps=swaps, probes=len(probes), reads=read_count(path))


def leaked():
    return [m for m in SWAP_MARKERS if any(m in body for body in STATE['bodies'])]


def check_snapshot_only(tag, r):
    dev_env = (r['root'] / 'dev.env').read_text() if (r['root'] / 'dev.env').exists() else ''
    check(f'{tag}: create rc0 on the validated snapshot', r['rc'] == 0 and 'QA_DISPOSABLE_CONVEX_CREATED=' + NAME in r['out'])
    check(f'{tag}: the base path was read exactly once (create() snapshot; no re-read in create_once or any retry)', r['reads'] == 1)
    check(f'{tag}: no byte of the replacement file reached the deployment API (every request body scanned)', leaked() == [])
    check(f'{tag}: deployment env holds exactly the validated snapshot R2 values',
          STATE['env'].get('R2_ACCESS_KEY_ID') == 'DEVSNAP-key-id' and STATE['env'].get('R2_BUCKET_NAME') == 'snap-bucket'
          and STATE['env'].get('R2_SECRET_ACCESS_KEY') == 'DEVSNAP-secret')
    check(f'{tag}: dev.env renders the validated snapshot, none of the replacement',
          'DEVSNAP-key-id' in dev_env and not any(m in dev_env for m in SWAP_MARKERS))
    check(f'{tag}: no secret or marker in output', not any(s in r['out'] + r['err'] for s in SENTINELS))


r = swap_run('swap-after-validation', SNAP, SWAPPED, after_validation=True)
check('swap-after-validation: the file really was replaced by the PROD sentinel once, after validation',
      r['swaps'] == ['validated'] and 'SWAPPED' in r['path'].read_text())
check_snapshot_only('swap-after-validation', r)

r = swap_run('swap-between-retries', SNAP, SWAPPED, unhealthy_probes=2)
check('swap-between-retries: two unhealthy probes replaced the file, so three attempts ran and two owned deployments were deleted',
      r['swaps'] == ['retry', 'retry'] and r['probes'] == 3 and 'SWAPPED' in r['path'].read_text()
      and CALLS['request'].count('/projects/634554/create_deployment') == 3
      and CALLS['request'].count(f'/deployments/{NAME}/delete') == 2 and r['out'].count('QA_CONVEX_NODE_PROBE_REPLACED=') == 2)
check_snapshot_only('swap-between-retries', r)

r = swap_run('swap-both', SNAP, SWAPPED, after_validation=True, unhealthy_probes=2)
check('swap-both: replaced after validation and again in each retry', r['swaps'] == ['validated', 'retry', 'retry'])
check_snapshot_only('swap-both', r)

# 6. A PROD snapshot still makes ZERO API calls, even when the file turns into a clean dev env the instant after it was read.
reset()
root, path = new_root('prod-snapshot-then-dev'), write_env('prod-snapshot-then-dev', PROD_FULL)
real_refuse = mod.refuse_production_base_env


def swap_to_dev_then_refuse(text):
    swap(path, SNAP)
    return real_refuse(text)


mod.refuse_production_base_env = swap_to_dev_then_refuse
try:
    with watching():
        rc, out, err = run(['create', '--root', str(root), '--base-env', str(path)])
finally:
    mod.refuse_production_base_env = real_refuse
check('prod snapshot, file swapped to dev right after the read: the swap really happened', 'DEVSNAP' in path.read_text())
check('prod snapshot, file swapped to dev: refused by NAME (BASE_ENV_PRODUCTION_CONVEX), rc1',
      rc == 1 and 'class=QaRefusal reason=BASE_ENV_PRODUCTION_CONVEX names=' in err)
check('prod snapshot, file swapped to dev: ZERO API calls, nothing written under root, one read of the path',
      no_calls() and not any(root.iterdir()) and read_count(path) == 1)
check('prod snapshot: no value in output', not any(s in out + err for s in SENTINELS))

# 7. Retired: the scoped storage selection (a separate QA bucket with a private QA identity, and its guarded readback comparisons) no longer
# exists (rule 65: one R2 account, bucket and key in every environment), so there is nothing to compare against the validated snapshot.

print(f'OK {passed} checks (helper={HELPER.name})')
