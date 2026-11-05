#!/usr/bin/env python3
"""Owned, expiring Convex dev lifecycle for remote QA; never prints credentials."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import sys
import subprocess
import time
import urllib.error
import urllib.request
import uuid

API = 'https://api.convex.dev/v1'


def save(path, value):
    temporary = path.with_name(path.name + '.next')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def request(path, data=None):
    token = json.loads((Path.home() / '.convex/config.json').read_text())['accessToken']
    payload = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(API + path, data=payload, headers={
        'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            body = response.read()
            return json.loads(body) if body.strip() else None
    except urllib.error.HTTPError as error:
        # Preserve the management operation, not credentials or provider bodies.
        error.qa_method = 'POST' if data is not None else 'GET'
        error.qa_route = re.sub(r'(?<=/deployments/)[^/?]+', '<deployment>', path)
        error.qa_route = re.sub(r'(?<=/projects/)[0-9]+', '<project>', error.qa_route)
        raise


def render_env(original, values):
    lines = []
    seen = set()
    for line in original.splitlines():
        match = re.match(r'^(?:export\s+)?([A-Z][A-Z0-9_]*)=', line)
        key = match[1] if match else None
        if key in values or key == 'CONVEX_DEPLOY_KEY':
            if key in seen:
                raise ValueError('Duplicate managed environment entry')
            seen.add(key)
            continue
        lines.append(line)
    lines.extend(key + '=' + shlex.quote(value) for key, value in values.items())
    return '\n'.join(lines) + '\n'


def validate_saved(root):
    expected = json.loads((root / 'ownership.json').read_text())
    if (set(expected) != {'name', 'deploymentType', 'projectId', 'reference', 'isDefault', 'expiresAt'}
            or not re.fullmatch(r'[a-z]+-[a-z]+-[0-9]+', expected['name'])
            or expected['name'].startswith(('beloved-cardinal-', 'loyal-lynx-', 'prod-'))
            or expected['deploymentType'] != 'dev' or expected['isDefault'] is not False
            or type(expected['projectId']) is not int or expected['projectId'] <= 0
            or not re.fullmatch(r'dev/qa-fullflow-[a-f0-9]{32}', expected['reference'])
            or type(expected['expiresAt']) is not int):
        raise ValueError('Invalid owned deployment receipt')
    return expected


PROBE_DIR = Path.home() / '.claude/scripts/convex-node-probe'
PROBE_ATTEMPTS = 4
PROBE_TIMEOUT_S = 120


def node_probe(key):
    # 2026-11-03: some fresh dev deployments come up with a wedged Node action executor. Any push with a
    # "use node" module then hangs in evaluate/start_push (408 after 300s, or InvalidModules "Cannot read
    # properties of undefined (reading 'error')"), and retries on the SAME deployment never recover.
    # A trivial one-action "use node" push takes ~40-60s on a healthy fresh deployment (wedged ones never finish); the real push replaces it.
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(Path.home()), 'CONVEX_DEPLOY_KEY': key}
    started = time.time()
    try:
        rc = subprocess.run([str(PROBE_DIR / 'node_modules/.bin/convex'), 'deploy', '--yes', '--codegen', 'disable',
                             '--typecheck', 'disable'], cwd=PROBE_DIR, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=PROBE_TIMEOUT_S).returncode
    except subprocess.TimeoutExpired:
        rc = 124
    return rc == 0, int(time.time() - started), rc


def delete_owned(root):
    # Same verified-ownership delete as destroy, without consuming the receipt files.
    expected = validate_saved(root)
    route = '/deployments/' + expected['name']
    actual = request(route)
    if not all(actual.get(k) == v for k, v in expected.items()):
        raise ValueError('Live ownership mismatch; refusing deletion')
    request(route + '/delete', {})
    try:
        request(route)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    else:
        raise ValueError('Deployment still exists after deletion')


def create(root, base_env, project):
    # A unique private directory and receipt are required before any API mutation.
    if not root.is_dir() or any(root.iterdir()) or root.is_symlink():
        raise ValueError('Create requires a new empty private directory')
    os.chmod(root, 0o700)
    for attempt in range(1, PROBE_ATTEMPTS + 1):
        if create_once(root, base_env, project, attempt):
            return
    raise ValueError('No healthy disposable Convex deployment after probe attempts')


def create_once(root, base_env, project, attempt):
    original = base_env.read_text()
    reference = 'dev/qa-fullflow-' + uuid.uuid4().hex
    expiry = int((time.time() + 3 * 3600) * 1000)
    # Validate/render all local input before creating a deployment.
    render_env(original, {'CONVEX_DEPLOYMENT': 'dev:pending'})
    save(root / 'intent.json', json.dumps(dict(projectId=project, reference=reference, expiresAt=expiry)) + '\n')
    created = request(f'/projects/{project}/create_deployment', {
        'type': 'dev', 'reference': reference, 'isDefault': False,
        'region': 'aws-us-east-1', 'class': 's256', 'expiresAt': expiry})
    # Keep the response for recovery even if validation/key creation fails.
    save(root / 'creation.json', json.dumps(created) + '\n')
    expected = dict(name=created['name'], deploymentType='dev', projectId=project,
                    reference=reference, isDefault=False, expiresAt=expiry)
    save(root / 'ownership.json', json.dumps(expected) + '\n')
    expected = validate_saved(root)
    actual = request('/deployments/' + expected['name'])
    if not all(actual.get(k) == v for k, v in expected.items()):
        raise ValueError('Created deployment ownership mismatch')
    name = expected['name']
    if actual.get('kind') != 'cloud' or actual.get('deploymentUrl') != f'https://{name}.convex.cloud':
        raise ValueError('Created deployment URL mismatch')
    key = request('/deployments/' + name + '/create_deploy_key', {
        'name': reference.split('/')[1], 'expiresAt': expiry})['deployKey']
    if not isinstance(key, str) or not key.startswith('dev:' + name + '|'):
        raise ValueError('Scoped deployment credential mismatch')
    healthy, seconds, rc = node_probe(key)
    print(f'QA_CONVEX_NODE_PROBE attempt={attempt} deployment={name} ok={int(healthy)} rc={rc} seconds={seconds}')
    if not healthy:
        delete_owned(root)
        print('QA_CONVEX_NODE_PROBE_REPLACED=' + name)
        return False
    # One fresh QA-only credential, scoped to this newly verified owned deployment.
    # Never inherit a slot/prod community credential or include it in receipts/logs.
    community_secret = secrets.token_hex(32)
    payload = json.dumps({'changes': [
        {'name': 'COMMUNITY_SERVER_SECRET', 'value': community_secret}
    ]}).encode()
    req = urllib.request.Request(actual['deploymentUrl'] + '/api/update_environment_variables',
        data=payload, headers={'Authorization': 'Convex ' + key,
                               'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as response:
        response.read()
    values = {'COMMUNITY_SERVER_SECRET': community_secret,
              'CONVEX_DEPLOYMENT': 'dev:' + name, 'CONVEX_ADMIN_KEY': key,
              'CONVEX_URL': actual['deploymentUrl'], 'NEXT_PUBLIC_CONVEX_URL': actual['deploymentUrl'],
              'CONVEX_SITE_URL': f'https://{name}.convex.site',
              'NEXT_PUBLIC_CONVEX_SITE_URL': f'https://{name}.convex.site',
              'QA_CONVEX_DEPLOYMENT_EXPIRES_AT_MS': str(expiry)}
    save(root / 'dev.env', render_env(original, values))
    print('QA_DISPOSABLE_CONVEX_CREATED=' + name)
    return True


def _destroy(root):
    if not (root / 'ownership.json').exists():
        if not (root / 'intent.json').exists():
            print('QA_DISPOSABLE_CONVEX=not-created')
            return
        # A timed-out create may have succeeded: reconcile its unique reference.
        intent = json.loads((root / 'intent.json').read_text())
        if (type(intent.get('projectId')) is not int or intent['projectId'] <= 0
                or not re.fullmatch(r'dev/qa-fullflow-[a-f0-9]{32}', intent.get('reference', ''))
                or type(intent.get('expiresAt')) is not int):
            raise ValueError('Invalid creation intent')
        deployments = request(f"/projects/{intent['projectId']}/list_deployments?deploymentType=dev&isDefault=false")
        matching = [d for d in deployments if d.get('reference') == intent['reference']]
        if len(matching) > 1:
            raise ValueError('Ambiguous creation reference')
        if not matching:
            # No immediate absence claim: a timed-out create can still be in flight.
            raise ValueError('Uncertain create; retain receipt and retry cleanup before expiry')
        save(root / 'ownership.json', json.dumps(dict(intent, name=matching[0]['name'],
             deploymentType='dev', isDefault=False)) + '\n')
    expected = validate_saved(root)
    route = '/deployments/' + expected['name']
    try:
        actual = request(route)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    else:
        if not all(actual.get(k) == v for k, v in expected.items()):
            raise ValueError('Live ownership mismatch; refusing deletion')
        request(route + '/delete', {})
        try:
            request(route)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        else:
            raise ValueError('Deployment still exists after deletion')
    save(root / 'deletion.json', json.dumps(dict(expected, absenceHttpStatus=404)) + '\n')
    print('QA_DISPOSABLE_CONVEX_DESTROYED=' + expected['name'])
    return True


def destroy(root):
    env = root / 'dev.env'
    try:
        confirmed = _destroy(root)
        if confirmed and env.exists():
            subprocess.run(['/Users/rajiv/.local/bin/trash', str(env)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print('QA_DISPOSABLE_CONVEX_ENV=removed')
    finally:
        if env.exists():
            os.chmod(env, 0o600)
            print('QA_DISPOSABLE_CONVEX_ENV=retained-for-recovery')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['create', 'destroy'])
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--base-env', type=Path)
    parser.add_argument('--project', type=int, default=634554)
    args = parser.parse_args()
    if args.action == 'create':
        if args.base_env is None or args.project <= 0:
            parser.error('Create requires --base-env and a positive project ID')
        create(args.root, args.base_env, args.project)
    else:
        destroy(args.root)


if __name__ == '__main__':
    try:
        main()
    except urllib.error.HTTPError as error:
        print('QA_CONVEX_LIFECYCLE_FAILED http_status=' + str(error.code)
              + ' method=' + getattr(error, 'qa_method', 'UNKNOWN')
              + ' route=' + getattr(error, 'qa_route', 'deployment-api'), file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        # Do not print provider bodies, key values or environment content.
        print('QA_CONVEX_LIFECYCLE_FAILED class=' + type(error).__name__, file=sys.stderr)
        sys.exit(1)
