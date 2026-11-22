#!/usr/bin/env python3
"""Disposable Modal namespace for remote-browser-box capture; never uses a local profile."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

ROOT = Path.home() / '.heydonna/qa-modal-leases'
COORD = 'root@178.105.82.170'
CREDS = '/etc/heydonna/hetzner-autoscaler.env'
PREFIXES = ('heydonna-audio-processor', 'heydonna-docx-processor', 'heydonna-agent-review')
BOX_RE = r'(?:rtbv?-[a-z0-9-]+-[0-9]{6}|rtbl-[0-9a-f]{16}-[0-9]{6})'  # rtbl-: warm-pool lease alias (CTO ts 1793272875.383349)
# Optional fourth per-box app: deployed (and therefore published and torn down) only when the checkout
# contains its entry file. Main has no chat_tools, so main behaves exactly as with PREFIXES alone.
CHAT_TOOLS_PREFIX = 'heydonna-chat-tools'
CHAT_TOOLS_ENTRY = 'modal/chat_tools/processor.py'
AUDIT_ENTRY = 'modal/audit/processor.py'
# Optional per-box term-autocomplete app (#11000): same rule, deployed only when the checkout has the entry file.
TERMS_ENTRY = 'modal/terms/processor.py'
# Optional per-box speaker-mapping app (#11549; endpoint from #11554): same rule, deployed only when the checkout has the
# entry file. Next reads MODAL_SPEAKER_MAPPING_URL (not *_ENDPOINT_URL) and POSTs to it as-is, so it carries the /map route.
SPEAKER_MAPPING_ENTRY = 'modal/speaker_mapping/processor.py'


def speaker_mapping_name(box):
    return 'heydonna-speaker-mapping-qa-' + identity(box)


def terms_name(box):
    return 'heydonna-terms-qa-' + identity(box)


def audit_name(box):
    return 'heydonna-audit-qa-' + identity(box)


_SECRET_ENV = ('MODAL_TOKEN_ID', 'MODAL_TOKEN_SECRET', 'MODAL_API_KEY', 'PIPELINE_AUTH_TOKEN',
               'CONVEX_ADMIN_KEY', 'CONVEX_DEPLOY_KEY')


_REGISTERED_SECRETS = set()  # box-generated --extra-secret values: in memory only, redacted verbatim by sanitize()


def register_secret(value):
    if isinstance(value, str) and len(value) >= 6:
        _REGISTERED_SECRETS.add(value)


def sanitize(text, env=None, limit=500):
    """Redact credentials, URL query strings/userinfo and secret-looking values from diagnostics."""
    text = str(text)
    for source in (env or {}, os.environ):
        for name in _SECRET_ENV:
            value = source.get(name)
            if value and len(value) >= 6:
                text = text.replace(value, '<redacted>')
    for value in _REGISTERED_SECRETS:
        text = text.replace(value, '<redacted>')
    text = re.sub(r'(https?://)[^/\s@]+@', r'\1<redacted>@', text)
    text = re.sub(r'(https?://[^\s?#]+)\?[^\s]*', r'\1?<redacted>', text)
    text = re.sub(r'(?i)(bearer|basic)\s+[A-Za-z0-9._~+/=-]+', r'\1 <redacted>', text)
    text = re.sub(r'(?i)((?:token|secret|password|passwd|api[_-]?key|authorization|auth|credential)[A-Za-z0-9_-]*\s*[=:]\s*)\S+', r'\1<redacted>', text)
    text = re.sub(r'\b(?:ak|as|sk|pk|ghp|gho|ghs|github_pat|xox[a-z])[-_][A-Za-z0-9_-]{8,}', '<redacted>', text)
    text = re.sub(r'\b[A-Za-z0-9_+/=-]{32,}\b', '<redacted>', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:limit]


def identity(box):
    if not re.fullmatch(BOX_RE, box):
        raise ValueError('Invalid remote box receipt')
    return hashlib.sha256(box.encode()).hexdigest()[:12]


def names(box):
    return [p + '-qa-' + identity(box) for p in PREFIXES]


def chat_tools_name(box):
    return CHAT_TOOLS_PREFIX + '-qa-' + identity(box)


def all_names(box):
    """Every app this box may own. stop() is list-driven, so an app that was never deployed is a no-op."""
    return names(box) + [chat_tools_name(box), 'heydonna-editor-chat-qa-' + identity(box), audit_name(box), terms_name(box), 'heydonna-qa-ledger-' + identity(box),
                                                                                                 speaker_mapping_name(box)]


def onmiss_dict_name(box):
    """Box-local capture-on-miss payload store (Modal Dict, owned by this box lease, deleted by stop())."""
    return 'heydonna-qa-onmiss-' + identity(box)


ONMISS_CAP_USD = 3

# ---- M15 scoped exact-hash capture-on-miss (CTO GO C0ALZJHGE49 thread 1794677380.515009 ts 1795301055.769519; Option A custody
# ts 1795301727.470839). Opt-in ONLY through --exact-manifest; stock --on-miss and every storage default are untouched. ----------
EXACT_MANIFEST_MAX_BYTES = 16384
EXACT_MANIFEST_MAX_HASHES = 4
EXACT_SESSION_TTL_S = 300        # fixture and report sessions are prefix-scoped and live five minutes
EXACT_SESSION_REFRESH_S = 100    # the controller re-mints this often while a leg runs; a stopped controller expires inside 5 minutes
EXACT_SESSION_WAIT_S = 60        # a proxy fixture client waits this long for its first armed session, then fails closed
CANARY_MAX_AGE_S = 48 * 3600
EXACT_ENTRY_KEYS = {'hash', 'provider', 'model', 'operation', 'canonical_key'}
# Explicit proof PROFILES (CTO GO C0ALZJHGE49 thread 1795302375.270589, ts 1795310865.915209; ladder pel-mwr0n14v-a9f99e2f). Every exact manifest NAMES
# one; a missing or unknown profile is refused. The M15 profile keeps the mandatory native-LangSmith join. The PR #14414 profile is for ONE
# pinned spec blob, ONE head and ONE approved hash: its completion is a settled request with ingress + provider response identity and a
# verified PUT-if-absent readback, and it never counts as that spec's AC1 PASS (AC1 needs its own full strict replay). There is NO generic
# switch that turns provenance off; widening the approval means adding a reviewed profile here.
EXACT_PROFILES = {
    'm15-langsmith': {'spec': 'tests/e2e/specs/qa-tests/m15-asr-draft-langsmith.spec.ts', 'provenance': 'langsmith',
                      'head': None, 'spec_blob': None, 'hash': None, 'stage': None},
    'pr14414-headerless-capture': {'spec': 'tests/e2e/specs/qa-tests/14390-headerless-proceedings.spec.ts', 'provenance': 'capture-readback',
                                   'head': '6edba2fad7b450d60a1433b81e019464cf50e6be', 'spec_blob': '6b562a1b2562b502510af1cb1cfb66694cc4697b',
                                   'hash': 'e5c629a109c8cb064baa8d4c775bd8894104de6256d590809b8b9e48d9da3fdc', 'stage': 'proofread:stream_call'},
}
EXACT_TERMINAL_CODES = ('QA_ONMISS_NOT_APPROVED', 'QA_ONMISS_LOOKUP_UNCERTAIN', 'QA_ONMISS_FIXTURE_EXISTS',
                        'QA_ONMISS_CUSTODY_UNRECONCILED', 'QA_ONMISS_BOUND_EXCEEDS_APPROVAL')


def load_exact_manifest(path, head=None):
    """Per-run exact-hash approval. Pure and fail-closed: nothing but this file widens what may be dispatched live.
    Shape: {version:1, head:<40hex>, product_pin:<40hex>, approval_ref, max_reserve_usd, langsmith:{draft_sha256, aligned_slice_sha256},
    hashes:[{hash, provider, model, operation, canonical_key}]}; full 64-hex hashes only (no prefixes or wildcards) and the canonical key
    must embed exactly that provider, model, operation and hash. max_reserve_usd binds the CTO-approved single-request bound; langsmith
    pins the persisted draft and the aligned alternate block the native trace must show; product_pin is the approved product head."""
    p = Path(path)
    if p.is_symlink() or not p.is_file() or p.stat().st_size > EXACT_MANIFEST_MAX_BYTES:
        raise ValueError('QA_EXACT_MANIFEST_INVALID: unreadable, symlink or too large')
    raw = p.read_bytes()
    try:
        data = json.loads(raw.decode('utf-8'))
    except ValueError:
        raise ValueError('QA_EXACT_MANIFEST_INVALID: not JSON') from None
    profile = data.get('profile') if isinstance(data, dict) else None
    if not isinstance(profile, str) or profile not in EXACT_PROFILES:
        raise ValueError('QA_EXACT_MANIFEST_INVALID: profile missing or unknown')
    pinned = EXACT_PROFILES[profile]
    needs_langsmith = pinned['provenance'] == 'langsmith'
    if (not isinstance(data, dict)
            or set(data) != {'version', 'profile', 'head', 'product_pin', 'approval_ref', 'max_reserve_usd', 'hashes'} | ({'langsmith'} if needs_langsmith else set())
            or data['version'] != 1
            or not isinstance(data['head'], str) or not re.fullmatch(r'[0-9a-f]{40}', data['head'])
            or not isinstance(data['product_pin'], str) or not re.fullmatch(r'[0-9a-f]{40}', data['product_pin'])
            or not isinstance(data['approval_ref'], str) or not re.fullmatch(r'[A-Za-z0-9._:/@-]{8,200}', data['approval_ref'])
            or isinstance(data['max_reserve_usd'], bool) or not isinstance(data['max_reserve_usd'], (int, float))
            or not 0 < data['max_reserve_usd'] <= ONMISS_CAP_USD
            or (needs_langsmith and (not isinstance(data['langsmith'], dict) or set(data['langsmith']) != {'draft_sha256', 'aligned_slice_sha256'}
                                     or any(not isinstance(v, str) or not re.fullmatch(r'[0-9a-f]{64}', v) for v in data['langsmith'].values())))
            or not isinstance(data['hashes'], list) or not 1 <= len(data['hashes']) <= EXACT_MANIFEST_MAX_HASHES):
        raise ValueError('QA_EXACT_MANIFEST_INVALID: shape')
    if head is not None and data['head'] != head:
        raise ValueError('QA_EXACT_MANIFEST_HEAD_MISMATCH')
    entries, seen = {}, set()
    for row in data['hashes']:
        if not isinstance(row, dict) or set(row) != EXACT_ENTRY_KEYS or not all(isinstance(v, str) for v in row.values()):
            raise ValueError('QA_EXACT_MANIFEST_INVALID: entry shape')
        digest, provider, model, operation, key = (row[k] for k in ('hash', 'provider', 'model', 'operation', 'canonical_key'))
        if (not re.fullmatch(r'[0-9a-f]{64}', digest) or not re.fullmatch(r'[a-z0-9_]{3,40}', provider)
                or not re.fullmatch(r'[A-Za-z0-9._-]{3,80}', model) or not re.fullmatch(r'[a-z0-9_]{3,20}', operation)
                or not re.fullmatch('fixtures/llm/v2/' + re.escape(provider) + r'/[a-z0-9.-]{1,120}/' + re.escape(model) + r'\.'
                                    + re.escape(operation) + '/' + digest + r'\.bin', key)
                or key in entries or digest in seen):
            raise ValueError('QA_EXACT_MANIFEST_INVALID: entry')
        seen.add(digest)
        entries[key] = {'hash': digest, 'provider': provider, 'model': model, 'operation': operation}
    if pinned['hash'] is not None and (seen != {pinned['hash']} or data['head'] != pinned['head'] or data['product_pin'] != pinned['head']):
        raise ValueError('QA_EXACT_MANIFEST_INVALID: not the one approved hash, head and product pin of this profile')
    return {'digest': hashlib.sha256(raw).hexdigest(), 'profile': profile, 'head': data['head'], 'product_pin': data['product_pin'],
            'approval_ref': data['approval_ref'], 'max_reserve_usd': float(data['max_reserve_usd']),
            'langsmith': dict(data['langsmith']) if needs_langsmith else None, 'entries': entries}


def check_exact_profile_spec(manifest, spec):
    """The profile's spec is pinned: any other spec path is refused (the launcher also verifies the spec blob on the box)."""
    if spec != EXACT_PROFILES[manifest['profile']]['spec']:
        raise ValueError('QA_EXACT_MANIFEST_INVALID: spec is not the approved spec of profile ' + manifest['profile'])


def derive_exact_ids(box, head, spend_box):
    """TRUSTED-HOST derivation of everything a scoped credential is bound to: the lease (from the controller's own box receipt), the
    physical-box identity (from the controller's pool/ephemeral receipt) and the exact head. Nothing the box wrote is used; a
    box-supplied value can only be CHECKED against this (see check_exact_ids)."""
    identity(box)
    if not isinstance(head, str) or not re.fullmatch(r'[0-9a-f]{40}', head):
        raise ValueError('QA_SCOPED_MINT_DENIED: exact head required')
    if box.startswith('rtbl-'):
        if not isinstance(spend_box, str) or not re.fullmatch(r'rtb-t?pool-[0-9]{2}(?:-[0-9]{6})?', spend_box):
            raise ValueError('QA_SCOPED_MINT_DENIED: a pool lease needs its physical pool server identity')
    elif spend_box != box:
        raise ValueError('QA_SCOPED_MINT_DENIED: an ephemeral box is its own physical identity')
    return onmiss_keys(box, spend_box, head)


def check_exact_ids(derived, claimed, manifest):
    """DENIED unless every box-written identity equals the trusted derivation and the baked manifest digest is this run's."""
    for key in ('lease', 'spend_box', 'head', 'dict', 'box_key', 'attempt_key', 'stage_run', 'run_key'):
        if claimed.get(key) != derived[key]:
            raise ValueError('QA_SCOPED_MINT_DENIED: box-supplied ' + key + ' differs from the trusted derivation')
    if claimed.get('manifest_digest') != manifest['digest'] or manifest['head'] != derived['head']:
        raise ValueError('QA_SCOPED_MINT_DENIED: box-supplied manifest digest or head differs from the approved manifest')
    return True


CUSTODY_STATES_OPEN = ('reserved', 'unknown', 'upstream_error', 'completed_unstored')


def _ledger_view(raw, approved):
    """Pure summary of one durable on-miss ledger object (None = authoritatively absent). Spend is the SAME sum the proxy uses
    (every attempt at its charged bound, settled or not); carry rows are reported separately so they are never counted twice."""
    if raw is None:
        return {'state': 'absent', 'committed_usd': 0.0, 'own_usd': 0.0, 'carry_usd': 0.0, 'unsettled': 0, 'halted': False,
                'request_states': {}, 'prior_claims': [], 'sha256': None}
    doc = json.loads(raw)
    if not isinstance(doc, dict) or not isinstance(doc.get('attempts', {}), dict) or not isinstance(doc.get('requests', {}), dict):
        raise ValueError('QA_CUSTODY_UNCERTAIN: ledger shape')
    attempts, requests = doc.get('attempts') or {}, doc.get('requests') or {}
    carry = sum(a.get('charged_bound_usd', 0) for a in attempts.values() if a.get('lease') == 'legacy-carry')
    total = sum(a.get('charged_bound_usd', 0) for a in attempts.values())
    states = {}
    for row in requests.values():
        states[row.get('state')] = states.get(row.get('state'), 0) + 1
    return {'state': 'present', 'committed_usd': round(total, 6), 'own_usd': round(total - carry, 6), 'carry_usd': round(carry, 6),
            'unsettled': sum(not a.get('settled') for a in attempts.values()), 'halted': bool(doc.get('halted')),
            'request_states': states, 'sha256': hashlib.sha256(raw).hexdigest(),
            'prior_claims': sorted(k for k, r in requests.items() if k in approved and r.get('state') != 'refused')}


def reconcile_custody(ids, manifest, legacy_get, qa_get, qa_put=None, now=None):
    """Spend custody before any dispatch, on the trusted host. Reads the LEGACY (heydonna-fixtures) and QA (heydonna-qa) on-miss
    ledgers for BOTH the physical box and the exact head. Any lookup that is not an authoritative present/absent refuses (a 403 is
    neither absence nor balance). Refuses on a prior claim for an approved hash anywhere, a halted ledger, or a combined allowance
    below the approved single-request bound. Unless read-only (dry-check), binds the combined exposure into the QA ledgers
    (carry row + a custody marker bound to this manifest digest) so the proxy's own cap and single-flight claim see it and
    refuses every reservation without that marker. legacy_get/qa_get(key) -> (raw, etag) | None; qa_put(key, doc_bytes, etag)."""
    approved = set(manifest['entries'])
    scopes = {'box': ids['box_key'], 'attempt': ids['attempt_key']}
    receipt = {'at': int(now if now is not None else time.time()), 'manifest_digest': manifest['digest'], 'head': ids['head'],
               'spend_box': ids['spend_box'], 'cap_usd': ONMISS_CAP_USD, 'max_reserve_usd': manifest['max_reserve_usd'],
               'scopes': {}, 'refusals': [], 'bound': False}
    fetched = {}
    for scope, key in scopes.items():
        legacy, qa = legacy_get(key), qa_get(key)
        view = {'key': key, 'legacy': _ledger_view(legacy[0] if legacy else None, approved),
                'qa': _ledger_view(qa[0] if qa else None, approved)}
        if legacy:
            view['legacy']['etag'] = legacy[1]
        view['combined_usd'] = round(view['legacy']['committed_usd'] + view['qa']['own_usd'], 6)
        view['remaining_usd'] = round(ONMISS_CAP_USD - view['combined_usd'], 6)
        receipt['scopes'][scope] = view
        fetched[scope] = (legacy, qa)
        for side in ('legacy', 'qa'):
            if view[side]['prior_claims']:
                receipt['refusals'].append({'code': 'QA_CUSTODY_PRIOR_CLAIM', 'scope': scope, 'side': side, 'hashes': [k.rsplit('/', 1)[-1][:64] for k in view[side]['prior_claims']]})
            if view[side]['halted']:
                receipt['refusals'].append({'code': 'QA_CUSTODY_HALTED', 'scope': scope, 'side': side})
        if view['remaining_usd'] < manifest['max_reserve_usd']:
            receipt['refusals'].append({'code': 'QA_CUSTODY_ALLOWANCE', 'scope': scope, 'combined_usd': view['combined_usd'],
                                        'remaining_usd': view['remaining_usd'], 'max_reserve_usd': manifest['max_reserve_usd']})
    receipt['allowance_usd'] = min(v['remaining_usd'] for v in receipt['scopes'].values())
    if receipt['refusals']:
        receipt['status'] = 'REFUSED'
        return receipt
    if qa_put is None:
        receipt['status'] = 'RECONCILED_READ_ONLY'
        return receipt
    for scope, key in scopes.items():
        view = receipt['scopes'][scope]
        legacy, qa = fetched[scope]
        marker = {'manifest_digest': manifest['digest'], 'at': receipt['at'], 'head': ids['head'], 'spend_box': ids['spend_box'],
                  'legacy': {'bucket': 'heydonna-fixtures', 'state': view['legacy']['state'], 'committed_usd': view['legacy']['committed_usd'],
                             'sha256': view['legacy']['sha256']}}
        doc = json.loads(qa[0]) if qa else {'run_id': 'custody-reconcile', 'cap_usd': ONMISS_CAP_USD, 'attempts': {}, 'events': [],
                                            'halted': False, 'allow_billable_tools': False, 'tool_admission_budget_usd': 2.0,
                                            'tool_cap_policy': 'soft cap: final call may overshoot by its actual search usage',
                                            'requests': {}}
        doc['custody'] = marker
        carry_id = 'legacy-carry-' + (view['legacy']['sha256'] or 'absent')[:16]
        if view['legacy']['committed_usd'] > 0 and carry_id not in doc['attempts']:
            usd = view['legacy']['committed_usd']
            doc['attempts'][carry_id] = {'hash': 'legacy-carry', 'onmiss_key': None, 'lease': 'legacy-carry', 'spend_box': ids['spend_box'],
                                         'provider': 'legacy', 'model': 'legacy-ledger', 'reserved_usd': usd, 'charged_bound_usd': usd,
                                         'settled': True, 'billable_tools': False, 'reserved_search_cost_usd': 0, 'per_search_usd': 0,
                                         'input_rate_per_million': 0, 'output_rate_per_million': 0,
                                         'legacy_source': {'bucket': 'heydonna-fixtures', 'key': key, 'sha256': view['legacy']['sha256'],
                                                           'etag': view['legacy'].get('etag')}}
        qa_put(key, json.dumps(doc).encode(), qa[1] if qa else None)
    # Read back: the marker, the carried exposure and the combined total must be exactly what was bound.
    for scope, key in scopes.items():
        got = qa_get(key)
        view = receipt['scopes'][scope]
        back = _ledger_view(got[0] if got else None, approved)
        if (not got or json.loads(got[0]).get('custody', {}).get('manifest_digest') != manifest['digest']
                or back['committed_usd'] < view['legacy']['committed_usd'] + view['qa']['own_usd'] - 1e-9):
            receipt['refusals'].append({'code': 'QA_CUSTODY_READBACK_MISMATCH', 'scope': scope})
        view['qa_after'] = back
    receipt['status'] = 'REFUSED' if receipt['refusals'] else 'RECONCILED_BOUND'
    receipt['bound'] = not receipt['refusals']
    return receipt


def check_grant_receipt(path):
    """The Run 9 parent grant, on the exact launch path: one active allow policy, object read/write only, solely on heydonna-qa."""
    p = Path(path)
    if p.is_symlink() or not p.is_file() or p.stat().st_size > 16384:
        raise ValueError('QA_GRANT_RECEIPT_INVALID: unreadable')
    r = json.loads(p.read_text())
    resources = r.get('resources') if isinstance(r, dict) else None
    ok = (isinstance(r, dict) and r.get('signer_fingerprint') == RUN9_SIGNER_FP and r.get('status') == 'active'
          and r.get('effect') == 'allow' and r.get('policy_count') == 1 and r.get('object_only') is True
          and isinstance(resources, dict) and len(resources) == 1 and all(k.endswith('_default_heydonna-qa') and v == '*' for k, v in resources.items())
          and sorted(r.get('permission_groups') or []) == ['Workers R2 Storage Bucket Item Read', 'Workers R2 Storage Bucket Item Write'])
    if not ok:
        raise ValueError('QA_GRANT_RECEIPT_INVALID: not the verified Run 9 object-only grant on heydonna-qa')
    return hashlib.sha256(p.read_bytes()).hexdigest()


RUN9_SIGNER_FP = '9b8c9f309b38da1f'  # access-id SHA-256 prefix of the verified Run 9 heydonna-qa object-only signer
APP_DATA_R2_NAMES = ('R2_ACCOUNT_ID', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_BUCKET_NAME', 'R2_ENDPOINT_URL')
SHARED_R2_SECRET = '"heydonna-r2-credentials"'


def app_data_values(env_text):
    """The scoped QA storage values an env file carries; refuses anything that is not the Run 9 scoped identity (explicit
    QA_R2_SCOPED selection, bucket heydonna-qa, signer fingerprint 9b8c9f309b38da1f, account endpoint, no public host,
    no production bucket reference). Returns (values, fingerprint). Values are never printed by callers."""
    values = {}
    for line in env_text.splitlines():
        key, sep, value = line.removeprefix('export ').partition('=')
        key = key.strip()
        if sep and key in APP_DATA_R2_NAMES + ('R2_PUBLIC_URL', 'QA_R2_SCOPED'):
            try:
                parts = shlex.split(value, comments=True)
            except ValueError:
                raise ValueError('QA_APP_DATA_UNSCOPED: ambiguous env entry') from None
            values[key] = parts[0] if len(parts) == 1 else ''
    fp = hashlib.sha256(values.get('R2_ACCESS_KEY_ID', '').encode()).hexdigest()[:16]
    if (any(not values.get(k) for k in APP_DATA_R2_NAMES) or values['R2_BUCKET_NAME'] != SCOPED_FIXTURE_BUCKET or fp != RUN9_SIGNER_FP
            or values['R2_ENDPOINT_URL'] != 'https://' + values['R2_ACCOUNT_ID'] + '.r2.cloudflarestorage.com'
            or values.get('R2_PUBLIC_URL') or 'heydonna-uploads' in ' '.join(values.values())):
        raise ValueError('QA_APP_DATA_UNSCOPED: app-data storage is not the Run 9 scoped heydonna-qa identity')
    return {k: values[k] for k in APP_DATA_R2_NAMES}, fp


def r2_lease_secret_name(box):
    return 'heydonna-qa-r2-' + identity(box)


def overlay_app_data_mounts(root, box, changes):
    """Exact mode: every shared R2 mount in the checkout's Modal sources (non-test) becomes THIS lease's QA-only secret, so no
    leased runtime mounts the shared (production-parity) R2 secret. Stages into changes; returns (files, replaced)."""
    files = replaced = 0
    for p in sorted(Path(root).glob('modal/**/*.py')):
        if '/tests/' in str(p) or '__pycache__' in str(p):
            continue
        text = changes.get(p) if p in changes else p.read_text()
        count = text.count(SHARED_R2_SECRET)
        if count:
            changes[p] = text.replace(SHARED_R2_SECRET, '"' + r2_lease_secret_name(box) + '"')
            files, replaced = files + 1, replaced + count
    return files, replaced


def shared_mounts_remaining(root):
    """Static readback on the box checkout: non-test Modal sources that still reference the shared R2 secret."""
    return sorted(str(p.relative_to(root)) for p in Path(root).glob('modal/**/*.py')
                  if '/tests/' not in str(p) and '__pycache__' not in str(p) and SHARED_R2_SECRET in p.read_text())


def _same_request(a, b):
    return bool(a and b) and (a == b or a.startswith(b + '/') or b.startswith(a + '/'))


def exact_capture_profile_status(report, manifest):
    """Completion of a capture-readback profile (no LangSmith): exactly ONE settled request for the ONE approved hash, with ingress and
    provider response identity, created by THIS run's PUT-if-absent and read back byte/status/content-type identical. Anything else is a
    named refusal. It certifies the FIXTURE capture only; it is never that spec's AC1 PASS."""
    if report is None:
        return 'NO_REPORT'
    pinned = EXACT_PROFILES[manifest['profile']]
    key = next(iter(manifest['entries']))
    if (report.get('exact') or {}).get('digest') != manifest['digest']:
        return 'PROFILE_MANIFEST_MISMATCH'
    if report.get('terminal_reason') or report.get('publication_error') or report.get('unapproved_staged'):
        return 'REPORT_TERMINAL'
    rows = [r for r in report.get('join') or [] if r.get('key') == key]
    if not rows:
        return 'NO_CLAIM'
    if len(rows) != 1 or len(report.get('attempts') or {}) != 1:
        return 'EXTRA_DISPATCH'
    row = rows[0]
    if row.get('hash') != manifest['entries'][key]['hash'] or row.get('hash') != pinned['hash']:
        return 'WRONG_HASH'
    if row.get('settled') is not True:
        return 'CLAIM_UNSETTLED'
    if not row.get('ingress_request_id'):
        return 'INGRESS_ID_MISSING'
    dispatch = row.get('provider_dispatch')
    if not (isinstance(dispatch, dict) and dispatch.get('provider_response_ids') and isinstance(dispatch.get('upstream_status'), int)
            and 200 <= dispatch['upstream_status'] < 300):
        return 'PROVIDER_DISPATCH_ID_MISSING'
    if list(report.get('fixtures_written') or []) != [key] or tuple(report.get('publication_counts') or ()) != (1, 0, 0):
        return 'PUT_IF_ABSENT_UNPROVEN'
    readback = [r for r in report.get('readback') or [] if r.get('key') == key]
    if len(readback) != 1 or readback[0].get('status') != 'MATCH' or (report.get('exact') or {}).get('readback_match') != 1:
        return 'READBACK_UNVERIFIED'
    return 'COMPLETE'


def exact_completion_status(report, trace, run_id, manifest):
    """The ONE completion verdict, chosen by the manifest's explicit profile: M15 keeps the mandatory verified LangSmith join; a
    capture-readback profile never consults a trace. There is no profile-less path and no switch to skip provenance."""
    if EXACT_PROFILES[manifest['profile']]['provenance'] == 'langsmith':
        return exact_join_status(report, trace, run_id, manifest)
    return exact_capture_profile_status(report, manifest)


def exact_join_status(report, trace, run_id, manifest):
    """Provenance join, verified (not merely present): the proxy's ingress id and provider dispatch id (from the durable claim)
    against the run-local spec's native LangSmith evidence for THIS case: its project, the transcript it created, the persisted
    draft and the aligned alternate block, each compared with what the approval pinned. First failing check names the status."""
    rows = (report or {}).get('join') or []
    if report is None:
        return 'NO_REPORT'
    if not rows:
        return 'NO_DISPATCH'
    ingress = [r.get('ingress_request_id') for r in rows if r.get('ingress_request_id')]
    if not ingress:
        return 'INGRESS_ID_MISSING'
    if not all(isinstance(r.get('provider_dispatch'), dict) and r['provider_dispatch'].get('provider_response_ids') for r in rows):
        return 'PROVIDER_DISPATCH_ID_MISSING'
    t = trace if isinstance(trace, dict) else {}
    if t.get('schema') != 1 or t.get('verified') is not True or t.get('completed') is not True or t.get('error') is not False:
        return 'LANGSMITH_UNJOINED'
    if not any(_same_request(t.get('request_id'), i) for i in ingress):
        return 'LANGSMITH_REQUEST_MISMATCH'
    if not t.get('run_id') or not t.get('trace_id') or not t.get('project_session_id'):
        return 'LANGSMITH_IDS_MISSING'
    if t.get('project') != 'qa-m15-' + run_id:
        return 'LANGSMITH_PROJECT_MISMATCH'
    if not t.get('transcript_id') or t.get('transcript_id') != t.get('created_transcript_id'):
        return 'LANGSMITH_TRANSCRIPT_MISMATCH'
    if t.get('draft_sha256') != manifest['langsmith']['draft_sha256'] or t.get('aligned_slice_sha256') != manifest['langsmith']['aligned_slice_sha256']:
        return 'ALIGNED_DRAFT_MISMATCH'
    return 'COMPLETE'


def load_exact_ids(path):
    """Non-secret identities the box wrote at overlay time (lease, ledger keys, staging run, manifest digest)."""
    p = Path(path)
    if p.is_symlink() or not p.is_file() or p.stat().st_size > 16384:
        raise ValueError('QA_EXACT_IDS_INVALID')
    ids = json.loads(p.read_text())
    keys = ('lease', 'spend_box', 'head', 'dict', 'box_key', 'attempt_key', 'stage_run', 'run_key', 'manifest_digest')
    if (not isinstance(ids, dict) or any(not isinstance(ids.get(k), str) or not re.fullmatch(r'[A-Za-z0-9._/-]{1,120}', ids[k])
                                         for k in keys) or not re.fullmatch(r'[0-9a-f]{64}', ids['manifest_digest'])):
        raise ValueError('QA_EXACT_IDS_INVALID')
    return ids


def onmiss_identity(root, box):
    """Server-side spend identities for capture-on-miss (CTO ts 1793378243.332849): $3 per PHYSICAL box lifetime and per
    QA attempt (exact head). Derived on the box from its hostname and the checkout HEAD, never from a caller flag, so a
    warm-pool re-lease, redeploy, restart or replacement box cannot open a fresh budget."""
    head = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=30).stdout.strip()
    if not re.fullmatch(r'[0-9a-f]{40}', head):
        raise RuntimeError('capture-on-miss needs the exact checkout head')
    host = socket.gethostname().split('.')[0]
    # Hetzner sets the hostname to the server name. A pool server name (rtb-pool-NN-HHMMSS) is fixed for that server's
    # whole life, across leases, warm reuse and resets; a rebuilt server gets a new name, and the per-attempt ledger
    # still binds that case.
    if re.fullmatch(r'rtb-t?pool-[0-9]{2}(?:-[0-9]{6})?', host) or (host == box and re.fullmatch(BOX_RE, host)):
        spend_box = host
    elif box.startswith('rtbl-'):
        # A warm-pool lease alias is per lease; it must never become the lifetime spend identity. Fail closed.
        raise RuntimeError('capture-on-miss: pool box hostname unverified (' + host + '); refusing a per-lease budget')
    else:
        spend_box = box  # ephemeral box: the receipt name is unique for the box lifetime
    return onmiss_keys(box, spend_box, head)


def onmiss_keys(box, spend_box, head):
    """Pure: the lease, ledger objects and staging prefix for (lease box, physical box, exact head). Shared by the box
    (onmiss_identity) and by the TRUSTED HOST (derive_exact_ids), so a scope can be derived without trusting the box."""
    digest = lambda s: hashlib.sha256(s.encode()).hexdigest()[:16]
    return {'lease': identity(box), 'spend_box': spend_box, 'head': head, 'dict': onmiss_dict_name(box),
            'box_key': 'qa-capture-ledgers/onmiss/box-' + digest('box:' + spend_box) + '.json',
            'attempt_key': 'qa-capture-ledgers/onmiss/attempt-' + digest('head:' + head) + '.json',
            # S161 scope: on-miss stages under its OWN run prefix (this lease + head), so a failed capture lane's or an
            # older lease's leftovers are never published by an on-miss finish (rule 32: failed capture stays FAILED).
            'stage_run': 'remote-qa-onmiss-' + identity(box) + '-' + head[:12],
            # The proxy's own per-run replay-event ledger (_QA_LEDGER, one CAS object per lease): its _log_action writes to it on every
            # hit/miss/forward, so an exact-mode write session must reach it (found by the real-R2 proof; fakes allow everything).
            'run_key': 'qa-capture-ledgers/remote-qa-' + identity(box) + '.json'}


def onmiss_effective(root, box, onmiss, exact=None, dry=False):
    """ids when capture-on-miss can bind a lifetime spend identity; otherwise None = strict replay (fail safe, visible).
    An --exact-manifest run NEVER degrades: an invalid manifest, head mismatch or unbound identity refuses the run."""
    if not onmiss:
        return None
    try:
        ids = onmiss_identity(root, box)
        if exact:
            ids['exact'] = load_exact_manifest(exact, ids['head'])
            ids['exact_dry'] = bool(dry)
        return ids
    except Exception as error:
        if exact:
            raise
        print('QA_ONMISS_DISABLED reason=' + sanitize(error, limit=160) + ' fallback=strict-replay')
        return None


def onmiss_summary(box_state, attempt_state, lease):
    """Pure: this lease's view of the durable spend ledgers + ONE typed terminal reason (None = clean)."""
    attempts = {t: r for t, r in (box_state.get('attempts') or {}).items() if r.get('lease') == lease}
    requests = {k: r for k, r in (box_state.get('requests') or {}).items() if r.get('lease') == lease}
    refused = [r for r in (box_state.get('refused_calls') or []) if r.get('lease') == lease]
    states = {}
    for r in requests.values():
        states[r.get('state')] = states.get(r.get('state'), 0) + 1
    codes = [r.get('code', '') for r in refused]
    unsettled = sum(not r.get('settled') for r in attempts.values())
    reason = None
    if 'QA_ONMISS_SPEND_CAP' in codes:
        reason = 'QA_ONMISS_SPEND_CAP'
    elif any(states.get(s) for s in ('unknown', 'upstream_error', 'completed_unstored', 'reserved')) or \
            any(c in ('QA_ONMISS_NO_RESEND', 'QA_ONMISS_IN_FLIGHT_TIMEOUT') for c in codes):
        reason = 'QA_ONMISS_NO_RESEND'
    elif any(c in EXACT_TERMINAL_CODES for c in codes):
        reason = next(c for c in codes if c in EXACT_TERMINAL_CODES)
    elif refused:
        reason = 'QA_ONMISS_INELIGIBLE'
    elif unsettled:
        reason = 'QA_ONMISS_USAGE_UNPROVEN'
    committed = lambda s: sum(r.get('charged_bound_usd', 0) for r in (s.get('attempts') or {}).values())
    return {'mode': 'capture-on-miss', 'run_id': 'lease-' + lease, 'attempts': attempts, 'events': [],
            'halted': bool(reason), 'halt_reason': reason, 'rejected_calls': refused,
            'fixtures_written': [], 'canonical_fixture_keys': [], 'publication_error': None, 'publication_counts': (0, 0, 0),
            'estimated_cost_usd': sum(r.get('estimated_cost_usd', 0) for r in attempts.values()),
            'charged_upper_bound_usd': sum(r.get('charged_bound_usd', 0) for r in attempts.values()),
            'unsettled_attempts': unsettled,
            'onmiss': {'request_states': states, 'cap_usd': ONMISS_CAP_USD,
                       'refused_hashes': sorted({r.get('hash') for r in refused if r.get('hash')}),
                       'box_lifetime_committed_usd': committed(box_state), 'box_halted': bool(box_state.get('halted')),
                       'attempt_committed_usd': committed(attempt_state), 'attempt_halted': bool(attempt_state.get('halted')),
                       'payloads': 'box-local Modal Dict for serving + staged and published canonical PUT-if-absent (S161)'},
            'terminal_reason': reason}


def qa_finish(s3, bucket, run_id, capture, publisher, onmiss=None):
    """Ledger read + canonical publication (capture lane, and capture-on-miss per S161: valid replies PUT-if-absent)."""
    import json
    if onmiss and onmiss.get('exact'):
        return qa_finish_exact(s3, bucket, publisher, onmiss)
    if onmiss:
        def read(key):
            try:
                return json.loads(s3.get_object(Bucket=bucket, Key=key)['Body'].read())
            except Exception as error:
                if getattr(error, 'response', {}).get('Error', {}).get('Code') not in ('NoSuchKey', '404', 'NotFound'):
                    raise
                return {}
        summary = onmiss_summary(read(onmiss['box_key']), read(onmiss['attempt_key']), onmiss['lease'])
        # S161 (Rajiv C0ALZJHGE49 ts 1795089328.528449): never throw away a valid LLM response. Every on-miss reply the
        # proxy staged is published canonical PUT-if-absent through the SAME publisher guard as capture; never replaces.
        plan, _ = publisher['collect_plan'](s3, bucket, onmiss['stage_run'])
        canonical_keys = {key for _, key in plan}
        written = []
        original_put = s3.put_object
        def receipt_put(**kwargs):
            response = original_put(**kwargs)
            if kwargs.get('Key') in canonical_keys:
                written.append(kwargs['Key'])
            return response
        s3.put_object = receipt_put
        try:
            summary['publication_counts'] = publisher['publish_plan'](s3, bucket, plan, replace_allowlist=frozenset())
        except Exception as error:
            summary['publication_error'] = type(error).__name__
        finally:
            s3.put_object = original_put
        summary['fixtures_written'] = sorted(set(written))
        summary['canonical_fixture_keys'] = sorted(canonical_keys)
        return summary
    # No allowlist/bypass is passed: canonical PUT-if-absent only, existing publisher guard unchanged.
    plan, skipped = publisher['collect_plan'](s3, bucket, run_id) if capture else ([], 0)
    written = []
    original_put = s3.put_object
    canonical_keys = {key for _, key in plan}
    def receipt_put(**kwargs):
        response = original_put(**kwargs)
        if kwargs.get('Key') in canonical_keys:
            written.append(kwargs['Key'])
        return response
    s3.put_object = receipt_put
    publication_error = None
    publication_counts = (0, 0, 0)
    if capture:
        try: publication_counts = publisher['publish_plan'](s3, bucket, plan, replace_allowlist=frozenset())
        except Exception as error: publication_error = type(error).__name__
    try:
        obj = s3.get_object(Bucket=bucket, Key='qa-capture-ledgers/'+run_id+'.json')
        ledger = json.loads(obj['Body'].read())
    except Exception as error:
        if getattr(error, 'response', {}).get('Error', {}).get('Code') not in ('NoSuchKey', '404', 'NotFound'):
            raise
        ledger = {'run_id': run_id, 'attempts': {}, 'events': [], 'no_proxy_requests': True}
    ledger['fixtures_written'] = sorted(set(written))
    ledger['publication_error'] = publication_error
    ledger['publication_counts'] = publication_counts
    ledger['canonical_fixture_keys'] = sorted(set(canonical for _, canonical in plan))
    ledger['estimated_cost_usd'] = sum(row.get('estimated_cost_usd', 0) for row in ledger['attempts'].values())
    ledger['charged_upper_bound_usd'] = sum(row['charged_bound_usd'] for row in ledger['attempts'].values())
    ledger['unsettled_attempts'] = sum(not row['settled'] for row in ledger['attempts'].values())
    return ledger


def exact_readback(s3, bucket, staged_key, canonical_key):
    """After the UNCHANGED publisher ran: the canonical object must equal this run's staged object byte for byte, with the same
    response status and content type. An absent object, or an older same-key object with different bytes, is not capture proof."""
    try:
        staged = s3.get_object(Bucket=bucket, Key=staged_key)
        staged_body = staged['Body'].read()
        canonical = s3.get_object(Bucket=bucket, Key=canonical_key)
        canonical_body = canonical['Body'].read()
    except Exception as error:
        return {'key': canonical_key, 'status': 'UNREADABLE', 'error': type(error).__name__}
    smeta, cmeta = staged.get('Metadata') or {}, canonical.get('Metadata') or {}
    row = {'key': canonical_key, 'bytes': len(canonical_body), 'sha256': hashlib.sha256(canonical_body).hexdigest(),
           'staged_sha256': hashlib.sha256(staged_body).hexdigest(), 'etag': canonical.get('ETag'),
           'response_status': cmeta.get('response-status'), 'staged_response_status': smeta.get('response-status'),
           'content_type': canonical.get('ContentType'), 'staged_content_type': staged.get('ContentType')}
    same = (row['bytes'] > 0 and row['sha256'] == row['staged_sha256']
            and row['response_status'] == row['staged_response_status'] and str(row['response_status'] or '').startswith('2')
            and row['content_type'] == row['staged_content_type'])
    row['status'] = 'MATCH' if same else 'MISMATCH'
    return row


def qa_finish_exact(s3, bucket, publisher, onmiss):
    """Exact-hash report. Publishes ONLY approved canonical keys whose staged object matches this run's settled claim (the staged
    canonical-key metadata alone is never trusted), through the unchanged publisher (replace_allowlist=frozenset(), PUT-if-absent),
    reads each canonical object back, and joins dispatch identity. Spend rows are read from the durable ledgers, never recomputed."""
    import json
    exact = onmiss['exact']
    approved = exact['entries']
    def read(key):
        try:
            return json.loads(s3.get_object(Bucket=bucket, Key=key)['Body'].read())
        except Exception as error:
            if getattr(error, 'response', {}).get('Error', {}).get('Code') not in ('NoSuchKey', '404', 'NotFound'):
                raise
            return {}
    box_state, attempt_state = read(onmiss['box_key']), read(onmiss['attempt_key'])
    summary = onmiss_summary(box_state, attempt_state, onmiss['lease'])
    requests = {k: r for k, r in (attempt_state.get('requests') or {}).items() if r.get('lease') == onmiss['lease']}
    attempts = attempt_state.get('attempts') or {}
    plan, _ = publisher['collect_plan'](s3, bucket, onmiss['stage_run'])
    matched, unmatched = [], []
    for staged_key, canonical in plan:
        row = requests.get(canonical) or {}
        claim = attempts.get(row.get('ticket')) or {}
        ok = (canonical in approved and row.get('state') == 'completed' and claim.get('lease') == onmiss['lease']
              and claim.get('onmiss_key') == canonical and claim.get('hash') == approved[canonical]['hash']
              and claim.get('settled') is True and not claim.get('usage_missing') and not claim.get('terminal_usage_unproven'))
        (matched if ok else unmatched).append((staged_key, canonical))
    written, counts, publication_error = [], (0, 0, 0), None
    canonical_keys = {key for _, key in matched}
    original_put = s3.put_object
    def receipt_put(**kwargs):
        response = original_put(**kwargs)
        if kwargs.get('Key') in canonical_keys:
            written.append(kwargs['Key'])
        return response
    s3.put_object = receipt_put
    try:
        if matched:
            counts = publisher['publish_plan'](s3, bucket, matched, replace_allowlist=frozenset())
    except Exception as error:
        publication_error = type(error).__name__
    finally:
        s3.put_object = original_put
    readback = [exact_readback(s3, bucket, staged_key, canonical) for staged_key, canonical in matched]
    completed = {k for k, r in requests.items() if k in approved and r.get('state') == 'completed'}
    join = []
    for ticket, claim in attempts.items():
        if claim.get('lease') == onmiss['lease'] and claim.get('onmiss_key') in approved:
            join.append({'hash': claim.get('hash'), 'key': claim.get('onmiss_key'), 'ticket': ticket,
                         'ingress_request_id': claim.get('ingress_request_id'), 'section_type': claim.get('section_type'),
                         'provider_dispatch': claim.get('dispatch'), 'settled': claim.get('settled') is True})
    reason = summary.get('terminal_reason')
    if not reason:
        if unmatched:
            reason = 'QA_ONMISS_STAGING_MISMATCH'
        elif publication_error:
            reason = 'QA_ONMISS_PUBLICATION_FAILED'
        elif any(r['status'] != 'MATCH' for r in readback):
            reason = 'QA_ONMISS_READBACK_MISMATCH'
        elif completed - canonical_keys:
            reason = 'QA_ONMISS_CAPTURE_UNPROVEN'
    summary.update(fixtures_written=sorted(set(written)), canonical_fixture_keys=sorted(canonical_keys),
                   publication_counts=counts, publication_error=publication_error, readback=readback, join=join,
                   unapproved_staged=sorted(key for _, key in unmatched),
                   exact={'digest': exact['digest'], 'profile': exact.get('profile'), 'approval_ref': exact['approval_ref'], 'approved': sorted(approved),
                          'captured': sorted(canonical_keys), 'readback_match': sum(r['status'] == 'MATCH' for r in readback)})
    if reason:
        summary.update(terminal_reason=reason, halted=True, halt_reason=summary.get('halt_reason') or reason)
    return summary


def chat_tools_present(root):
    return (Path(root)/CHAT_TOOLS_ENTRY).is_file()


def token_env():
    # Automatic source-to-process transport. No token in argv, files, receipts or logs.
    script = '''import json,shlex
from pathlib import Path
wanted={"MODAL_TOKEN_ID","MODAL_TOKEN_SECRET"}
values={}
for line in Path("/etc/heydonna/hetzner-autoscaler.env").read_text().splitlines():
 key,sep,value=line.removeprefix("export ").partition("=")
 if sep and key.strip() in wanted:
  parsed=shlex.split(value,comments=True)
  if len(parsed)==1: values[key.strip()]=parsed[0]
if set(values)!=wanted or not all(values.values()): raise SystemExit(2)
print(json.dumps(values))
'''
    command = ['ssh', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'ConnectTimeout=10', '-i', str(Path.home()/'.ssh/hetzner-runner-warm'),
               COORD, 'python3 -I -']
    result = subprocess.run(command, input=script, text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError('CI Modal credential transport failed (details withheld)')
    values = json.loads(result.stdout)
    if set(values) != {'MODAL_TOKEN_ID', 'MODAL_TOKEN_SECRET'} or not all(isinstance(v, str) and v for v in values.values()):
        raise RuntimeError('CI Modal credential receipt invalid')
    return values


def modal_env(remote=False):
    env = os.environ.copy()
    env.pop('MODAL_PROFILE', None)
    env.update({'MODAL_ENVIRONMENT': 'main', 'MODAL_CONFIG_PATH': '/dev/null'})
    if not remote:
        env.update(token_env())
    if not env.get('MODAL_TOKEN_ID') or not env.get('MODAL_TOKEN_SECRET'):
        raise RuntimeError('Explicit automation Modal credentials required')
    return env


def cli(args, env, json_output=False):
    binary = [sys.executable, '-m', 'modal'] if env.get('REMOTE_BOX') == '1' else ['modal']
    result = subprocess.run(binary + args, env=env, text=True, capture_output=True, timeout=180)
    if result.returncode:
        # Sanitized tail only (see sanitize()); the raw streams may carry credentials.
        tail = sanitize(((result.stderr or '').strip() or (result.stdout or '').strip())[-600:], env)
        raise RuntimeError('Modal operation failed: ' + ' '.join(args[:2]) + ' rc=' + str(result.returncode) + ' :: ' + tail)
    return json.loads(result.stdout) if json_output else None


def stop(box, remote=False):
    env = modal_env(remote)
    owned = set(all_names(box))
    apps = cli(['app', 'list', '--json'], env, True)
    if not isinstance(apps, list):
        raise RuntimeError('Unrecognised Modal app list')
    for row in apps:
        if row.get('description') not in owned or str(row.get('state', '')).lower() == 'stopped':
            continue
        app_id = row.get('app_id', '')
        if not re.fullmatch(r'ap-[A-Za-z0-9]+', app_id):
            raise RuntimeError('Unrecognised Modal app ID')
        cli(['app', 'stop', app_id, '--yes'], env)
        print('QA_MODAL_STOPPED app=' + row['description'])
    # app stop acknowledges before app list necessarily observes the transition.
    # Bound the verification wait; never delete the secret while an owned app is active.
    for attempt in range(4):
        apps = cli(['app', 'list', '--json'], env, True)
        if not isinstance(apps, list):
            raise RuntimeError('Unrecognised Modal app list')
        if not any(r.get('description') in owned and str(r.get('state', '')).lower() != 'stopped' for r in apps):
            break
        if attempt == 3:
            raise RuntimeError('QA Modal app still active')
        time.sleep(10)
    # capture-on-miss payload store: owned by this box lease, never outlives it. List-driven, idempotent.
    store = onmiss_dict_name(box)
    dicts = cli(['dict', 'list', '--json'], env, True)
    if not isinstance(dicts, list):
        raise RuntimeError('Unrecognised Modal dict list')
    if any(str(value).casefold() == store.casefold()
           for row in dicts if isinstance(row, dict)
           for key, value in row.items() if str(key).casefold() == 'name'):
        cli(['dict', 'delete', store, '--yes'], env)
        print('QA_ONMISS_LOCAL_STORE_DELETED name=' + store)
    # A missing secret is an idempotent success, determined by the list, not an ignored CLI failure.
    listed = cli(['secret', 'list', '--json'], env, True)
    if not isinstance(listed, list):
        raise RuntimeError('Unrecognised Modal secret list')
    for secret_name in ('heydonna-qa-' + identity(box), r2_lease_secret_name(box)):  # invocation secret + exact-mode lease-only R2 secret
        if any(r.get('name') == secret_name for r in listed):
            cli(['secret', 'delete', secret_name, '--yes'], env)
    print('QA_MODAL_CLEANUP=verified box=' + box)


def lease_path(box):
    identity(box)
    return ROOT / (box + '.json')


def register(box, ttl, allow_billable_tools=False, tool_spend_cap_usd=2.0, onmiss=False, exact_digest=None):
    if not 60 <= ttl <= 10800:
        raise ValueError('QA Modal TTL must be between 60 seconds and 3 hours')
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    p = lease_path(box)
    receipt = {'box': box, 'created_at': int(time.time()), 'expires_at': int(time.time())+ttl,
               'apps': all_names(box), 'secret': 'heydonna-qa-'+identity(box), 'state': 'registered',
               'billable_tools': tool_policy(allow_billable_tools, tool_spend_cap_usd),
               'llm_mode': ('capture-on-miss-exact' if exact_digest else 'capture-on-miss') if onmiss else 'strict-or-explicit-capture',
               'exact_manifest_sha256': exact_digest}
    with p.open('x') as f:
        os.chmod(p, 0o600)
        json.dump(receipt, f)
    print('QA_MODAL_BOX_ID=' + identity(box))


def finish(box):
    p = lease_path(box)
    if p.exists():
        record = json.loads(p.read_text())
        record['state'] = 'stopped'
        record['stopped_at'] = int(time.time())
        new = p.with_suffix('.next')
        new.write_text(json.dumps(record))
        os.chmod(new, 0o600)
        new.replace(p)


def reap():
    if not ROOT.exists():
        return
    failed = False
    for p in ROOT.glob('*.json'):
        try:
            record = json.loads(p.read_text())
            box = record['box']
            _prev = [n for n in all_names(box) if n != speaker_mapping_name(box)]  # receipts written before speaker-mapping
            _legacy = [n for n in _prev if n != terms_name(box)]  # receipts written before the terms app
            if lease_path(box) != p or record['apps'] not in (names(box), names(box) + [chat_tools_name(box)], _legacy[:-2], _legacy[:-1], _legacy, _prev[:-2], _prev[:-1], _prev, all_names(box)):
                raise ValueError('Invalid QA Modal ownership receipt')
            if record['state'] == 'stopped' or record['expires_at'] > time.time():
                continue
            stop(box)
            finish(box)
        except Exception as e:
            failed = True
            print('QA_MODAL_REAP_FAILED receipt=' + p.name + ' reason=' + type(e).__name__, file=sys.stderr)
    if failed:
        raise RuntimeError('QA Modal expiry cleanup needs retry')


def replace(text, old, new, count=1):
    found = text.count(old)
    if found != count:
        # Still fail closed, but name the anchor so drift is diagnosable from the lane log (anchors are static source text).
        raise RuntimeError('Canonical QA overlay anchor drifted; refuse partial overlay (found=' + str(found)
                           + ' expected=' + str(count) + ' anchor=' + repr(old.strip()[:70]) + ')')
    return text.replace(old, new)


def overlay_audit_secrets(text):
    qa_key = '("heydonna-qa-" + SUFFIX.removeprefix("-qa-") if SUFFIX.startswith("-qa-") else "heydonna-modal-api-key")'
    # Main stores the selected names in `secret_names`; older PR heads use an
    # inline tuple in the modal.Secret comprehension. Accept exactly one full
    # layout so a partial or ambiguous source still fails closed.
    layouts = (
        (
            'secret_names = ("heydonna-modal-api-key", "gcp-vertex-credentials") if CALIBRATION else (',
            '    "heydonna-modal-api-key", "anthropic-credentials", "openai-credentials",',
        ),
        (
            'secrets = [modal.Secret.from_name(name) for name in (',
            '    "heydonna-modal-api-key", "anthropic-credentials", "openai-credentials",',
        ),
    )
    selector = 'VERTEX_CREDENTIALS_SECRET_NAME = (\n    "gcp-vertex-credentials" if SUFFIX == "-prod" else "gcp-vertex-credentials-nonprod"\n)'
    selected_layout = (
        'secret_names = ("heydonna-modal-api-key", VERTEX_CREDENTIALS_SECRET_NAME) if CALIBRATION else (',
        '    "heydonna-modal-api-key", "anthropic-credentials", "openai-credentials",',
    )
    if text.count(selected_layout[0]):
        if text.count(selector) != 1:
            raise RuntimeError('Canonical QA overlay anchor drifted; refuse partial overlay (audit vertex selector)')
        layouts += (selected_layout,)
    matches = [(start, key) for start, key in layouts if text.count(start) == 1 and text.count(key) == 1]
    if len(matches) != 1 or any(text.count(anchor) not in (0, 1) for layout in layouts for anchor in layout):
        raise RuntimeError('Canonical QA overlay anchor drifted; refuse partial overlay (audit secret layout)')
    start, key = matches[0]
    if start.startswith('secret_names = '):
        text = replace(text, start, start.replace('"heydonna-modal-api-key"', qa_key), 1)
    text = replace(text, key, key.replace('"heydonna-modal-api-key"', qa_key), 1)
    secret = '"heydonna-pipeline-auth-token"'
    return replace(text, secret, '("heydonna-qa-" + SUFFIX.removeprefix("-qa-") if SUFFIX.startswith("-qa-") else ' + secret + ')')


def tool_policy(allow_billable_tools=False, tool_spend_cap_usd=2.0):
    if not math.isfinite(tool_spend_cap_usd) or tool_spend_cap_usd <= 0:
        raise ValueError('Tool admission budget must be finite and positive')
    return {'allow_billable_tools': allow_billable_tools, 'admission_budget_usd': tool_spend_cap_usd,
            'estimated_searches_per_call': 10, 'per_search_usd': 0.014,
            'cap_policy': 'soft cap: final call may overshoot by its actual search usage'}


def ledger_source(box, allow_billable_tools=False, tool_spend_cap_usd=2.0, onmiss=None):
    tool_policy(allow_billable_tools, tool_spend_cap_usd)
    source = (QA_PROXY_LEDGER_SOURCE.replace('__RUN__', repr('remote-qa-'+identity(box)))
              .replace('__ALLOW_BILLABLE_TOOLS__', repr(allow_billable_tools))
              .replace('__TOOL_SPEND_CAP_USD__', repr(tool_spend_cap_usd)))
    if not onmiss:
        return source + QA_ONMISS_OFF_SOURCE
    if allow_billable_tools:
        raise ValueError('capture-on-miss never enables paid tools/search')
    for k in ('dict', 'lease', 'spend_box', 'box_key', 'attempt_key'):
        source_value = onmiss[k]
        if not isinstance(source_value, str) or not re.fullmatch(r'[A-Za-z0-9._/-]{1,120}', source_value):
            raise ValueError('Invalid capture-on-miss identity: ' + k)
    exact = onmiss.get('exact')
    if exact is not None and (not isinstance(exact, dict) or not re.fullmatch(r'[0-9a-f]{64}', str(exact.get('digest', '')))
                              or not isinstance(exact.get('entries'), dict) or not exact['entries']):
        raise ValueError('Invalid exact-hash approval')
    exact_source = None if exact is None else {'digest': exact['digest'], 'approval_ref': exact['approval_ref'], 'profile': exact['profile'],
                                               'max_reserve_usd': float(exact['max_reserve_usd']),
                                               'dry': bool(onmiss.get('exact_dry')), 'entries': exact['entries']}
    return source + (QA_ONMISS_SOURCE.replace('__ONMISS_DICT__', repr(onmiss['dict']))
                     .replace('__ONMISS_LEASE__', repr(onmiss['lease']))
                     .replace('__ONMISS_SPEND_ID__', repr(onmiss['spend_box']))
                     .replace('__ONMISS_BOX_KEY__', repr(onmiss['box_key']))
                     .replace('__ONMISS_ATTEMPT_KEY__', repr(onmiss['attempt_key']))
                     .replace('__ONMISS_CAP_USD__', repr(ONMISS_CAP_USD))
                     .replace('__ONMISS_RUN__', repr(onmiss['stage_run']))
                     .replace('__ONMISS_EXACT__', repr(exact_source))
                     .replace('__SCOPED_BUCKET__', repr(SCOPED_FIXTURE_BUCKET))
                     .replace('__EXACT_WAIT_S__', repr(EXACT_SESSION_WAIT_S))
                     + (QA_ONMISS_EXACT_SOURCE if exact_source else ''))


def overlay(root, box=None, capture=False, allow_billable_tools=False, tool_spend_cap_usd=2.0, onmiss=False, exact=None,
            ids_out=None, dry=False):
    if onmiss and capture:
        raise ValueError('--on-miss and --capture are exclusive')
    if exact and not onmiss:
        raise ValueError('--exact-manifest requires --on-miss')
    # Deployed tooling overlay, like the existing UI-only overlay. Persisted app sync follows proof.
    preflight = root/'scripts/e2e/local-repro-preflight.sh'
    text = preflight.read_text()
    text = replace(text, 'EXPECTED_MODAL_SUFFIX="-slot${SLOT_NUM}"', '''EXPECTED_MODAL_SUFFIX="-slot${SLOT_NUM}"
if [ -n "${QA_MODAL_BOX_ID:-}" ]; then
  [[ "${REMOTE_BOX:-}" = 1 && "$QA_MODAL_BOX_ID" =~ ^[0-9a-f]{12}$ ]] || { echo 'Invalid QA Modal identity'; exit 4; }
  EXPECTED_MODAL_SUFFIX="-qa-${QA_MODAL_BOX_ID}"
fi''')
    text = replace(text, '*"audio-processor-slot${SLOT_NUM}"*', '*"audio-processor${EXPECTED_MODAL_SUFFIX}"*')
    text = replace(text, '*"docx-processor-slot${SLOT_NUM}"*', '*"docx-processor${EXPECTED_MODAL_SUFFIX}"*')
    # Use the existing REMOTE_BOX env wrapper to supply the scoped dev admin
    # key here too; bare npx has no interactive Convex login on disposable boxes.
    text = replace(text, "  if npx convex run testing_r2:cleanupE2eNamespaceR2Objects '{}' 2>/tmp/cleanup-namespace-err.txt; then",
                   "  if env -u CONVEX_DEPLOY_KEY npx convex run testing_r2:cleanupE2eNamespaceR2Objects '{}' 2>/tmp/cleanup-namespace-err.txt; then")
    # 2026-10-29 qaspeed C (Rajiv DM D0AMF0XE6TS ts 1793291081.433449): Modal pass 1 starts in the background at [B.2]
    # and overlaps the seed import + Convex deploy + seed restores; it is joined at [B.4], before resolve/URL wiring.
    # Pass 1 needs only .env.local values that exist before [B.2] (disposable CONVEX_SITE_URL, placeholder peer URLs,
    # suffix, box secret name). The ONE Convex-dependent step, deploy-modal.sh's /pipeline canary, needs the B.3 functions,
    # so it is deferred out of pass 1 and run unchanged (canary-only mode) after [B.3.x], before the join. Pass 2 still
    # rebakes every app with the final env. Abort paths kill the background tree (no orphan deploy after teardown).
    text = replace(text, "trap 'cleanup_e2e_namespace; stop_synth_egress_observer; cleanup_synth_auth_material; release_slot_clone_lease' EXIT\n",
                   '''# qaspeed C: stop an unjoined background Modal pass 1 (whole process tree, same pgid) on any exit path.
qa_modal_kill_tree() { local qa_c; for qa_c in $(pgrep -P "$1" 2>/dev/null); do qa_modal_kill_tree "$qa_c"; done; kill -TERM "$1" 2>/dev/null; }
qa_modal_bg_stop() {
  [ -n "${QA_MODAL_BG_PID:-}" ] || return 0
  qa_modal_kill_tree "$QA_MODAL_BG_PID"; wait "$QA_MODAL_BG_PID" 2>/dev/null
  echo "QA_MODAL_PASS1_BG=stopped pid=$QA_MODAL_BG_PID" >&2; QA_MODAL_BG_PID=
}
trap 'qa_modal_bg_stop; cleanup_e2e_namespace; stop_synth_egress_observer; cleanup_synth_auth_material; release_slot_clone_lease' EXIT
''')
    text = replace(text, "trap 'cleanup_e2e_namespace; stop_synth_egress_observer; cleanup_synth_auth_material; exit 130' INT\n",
                   "trap 'qa_modal_bg_stop; cleanup_e2e_namespace; stop_synth_egress_observer; cleanup_synth_auth_material; exit 130' INT\n")
    text = replace(text, "trap 'cleanup_e2e_namespace; stop_synth_egress_observer; cleanup_synth_auth_material; exit 143' TERM HUP\n",
                   "trap 'qa_modal_bg_stop; cleanup_e2e_namespace; stop_synth_egress_observer; cleanup_synth_auth_material; exit 143' TERM HUP\n")
    text = replace(text, 'echo "    [B.2] npx convex import --replace-all -y $SEED_FILE_TMP..."\n', '''QA_MODAL_BG_PID=; QA_MODAL_PASS1_RC=
if [ -n "${QA_MODAL_BOX_ID:-}" ] && [ -z "${UI_CAPTURE_ONLY:-}" ]; then
  QA_MODAL_BG_LOG="/tmp/preflight-modal-pass1-slot${SLOT_NUM}.log"
  QA_MODAL_CANARY_LOG="/tmp/preflight-modal-canary-slot${SLOT_NUM}.log"
  echo "QA_STAGE marker=[B.4bg-start] epoch=$(date +%s)"
  ( QA_MODAL_SKIP_CANARY=1 env LLM_PROXY_ENABLED=true STRICT_FIXTURES=true bash scripts/deploy-modal.sh ) >"$QA_MODAL_BG_LOG" 2>&1 &
  QA_MODAL_BG_PID=$!
  echo "    [B.4bg] Modal pass 1 started concurrently (pid $QA_MODAL_BG_PID, log $QA_MODAL_BG_LOG); canary deferred until Convex is deployed"
fi
echo "    [B.2] npx convex import --replace-all -y $SEED_FILE_TMP..."
''')
    text = replace(text, '''set +u
# REMOTE_BOX=1: the ephemeral box has no Modal token, so it reuses the slot's already-deployed Modal.
''', '''if [ -n "${QA_MODAL_BG_PID:-}" ]; then
  # Join point: the Convex functions now exist, so run deploy-modal.sh's canary exactly as-is, then wait for pass 1.
  QA_MODAL_JOIN_T0=$(date +%s)
  QA_MODAL_CANARY_ONLY=1 bash scripts/deploy-modal.sh >"$QA_MODAL_CANARY_LOG" 2>&1; QA_MODAL_CANARY_RC=$?
  if [ "$QA_MODAL_CANARY_RC" != 0 ]; then
    qa_modal_bg_stop; QA_MODAL_PASS1_RC=$QA_MODAL_CANARY_RC
  else
    wait "$QA_MODAL_BG_PID"; QA_MODAL_PASS1_RC=$?; QA_MODAL_BG_PID=
  fi
  echo "QA_MODAL_PASS1_JOIN wait_seconds=$(( $(date +%s) - QA_MODAL_JOIN_T0 )) rc=$QA_MODAL_PASS1_RC canary_rc=$QA_MODAL_CANARY_RC"
fi
set +u
# REMOTE_BOX=1: the ephemeral box has no Modal token, so it reuses the slot's already-deployed Modal.
''')
    text = replace(text, 'elif [ "${REMOTE_BOX:-}" = "1" ]; then', '''elif [ -n "${QA_MODAL_BOX_ID:-}" ]; then
    if [ -n "${QA_MODAL_PASS1_RC:-}" ]; then
      cat "$QA_MODAL_CANARY_LOG" "$QA_MODAL_BG_LOG" 2>/dev/null; (exit "$QA_MODAL_PASS1_RC")
    else
      env LLM_PROXY_ENABLED=true STRICT_FIXTURES=true bash scripts/deploy-modal.sh
    fi
  elif [ "${REMOTE_BOX:-}" = "1" ]; then''')
    text = replace(text, '-H "X-API-Key: ${MODAL_API_KEY:-}" \\\n', '--config <(printf \'header = "X-API-Key: %s"\\n\' "${MODAL_API_KEY:-}") \\\n')
    # Resolve actual Modal labels after the first deploy, then bake the resolved peer
    # routing into the SAME owned apps once. Readiness never probes bootstrap URLs.
    text = replace(text, '# A remote shell export cannot change the flags baked into the reused Modal app.', '''if [ -n "${QA_MODAL_BOX_ID:-}" ]; then
  python3 -I /root/qa/modal-lane.py resolve --box "$QA_MODAL_BOX" --root "$REPO_ROOT" --config /root/qa/config.json || exit 3
  source /root/qa/endpoints.env
  (export CONVEX_DEPLOY_KEY="$CONVEX_ADMIN_KEY"; node /root/qa/config.cjs apply /root/qa/config.json /root/qa/config-before.json) || exit 3
  env LLM_PROXY_ENABLED=true STRICT_FIXTURES=true bash scripts/deploy-modal.sh || exit 3
  python3 -I /root/qa/modal-lane.py resolve --box "$QA_MODAL_BOX" --root "$REPO_ROOT" --config /root/qa/config.json --verify || exit 3
  python3 -I /root/qa/modal-lane.py provider-ready --root "$REPO_ROOT" || exit 3
  MODAL_AUDIO_URL="$MODAL_AUDIO_ENDPOINT_URL"
  MODAL_DOCX_URL="$MODAL_DOCX_ENDPOINT_URL"
fi
# A remote shell export cannot change the flags baked into the reused Modal app.''')
    # 2026-10-25: a cold first push of the full schema + functions to a fresh disposable deployment legitimately exceeds 60s.
    # Bounded wait (default 300s) with ONE bounded retry on timeout, elapsed logged; also dedupe .env.local keys (the Convex CLI
    # refuses to update a file with duplicate NEXT_PUBLIC_CONVEX_URL / _SITE_URL entries). Fails closed if the anchor drifts.
    text = replace(text, """env -u CONVEX_DEPLOY_KEY bash -c 'set -a && source .env.local && set +a && timeout 60s npx convex dev --once --typecheck=disable' >"$CONVEX_DEV_LOG" 2>&1
CONVEX_DEV_EXIT=$?
""", """python3 -I - .env.local <<'QA_ENV_DEDUPE'
import os, re, sys
p = sys.argv[1]
lines = open(p).read().splitlines()
last = {}
for i, line in enumerate(lines):
    m = re.match(r'^(?:export\\s+)?([A-Za-z_][A-Za-z0-9_]*)=', line)
    if m:
        last[m[1]] = i
out = [l for i, l in enumerate(lines) if not (re.match(r'^(?:export\\s+)?([A-Za-z_][A-Za-z0-9_]*)=', l) and last[re.match(r'^(?:export\\s+)?([A-Za-z_][A-Za-z0-9_]*)=', l)[1]] != i)]
dropped = len(lines) - len(out)
if dropped:
    tmp = p + '.dedupe'
    open(tmp, 'w').write('\\n'.join(out) + '\\n'); os.chmod(tmp, 0o600); os.replace(tmp, p)
print('QA_ENV_DEDUPE dropped=%d' % dropped)
QA_ENV_DEDUPE
QA_CONVEX_DEPLOY_WAIT="${QA_CONVEX_DEPLOY_TIMEOUT:-300}"
# 2026-11-03 push-phase trace: method/host/path/status/ms of each Convex HTTP call goes to CONVEX_DEV_LOG (its tail is printed on
# timeout), so a hang names the stuck request. Never bodies, headers or keys.
cat >/root/qa/convex-b3-fetch-trace.cjs <<'QA_FETCH_TRACE'
const f=globalThis.fetch;if(f){globalThis.fetch=async function(u,o){const t=Date.now(),url=new URL(String(u&&u.url||u)),m=(o&&o.method)||'GET',tag=`${m} ${url.host.split('.')[0]}${url.pathname}`;
process.stderr.write(`[fetch ${new Date(t).toISOString()}] start ${tag}\\n`);try{const r=await f.apply(this,arguments);process.stderr.write(`[fetch] done ${tag} ${r.status} ${Date.now()-t}ms\\n`);return r;}catch(e){process.stderr.write(`[fetch] fail ${tag} ${e&&e.name} ${Date.now()-t}ms\\n`);throw e;}};}
QA_FETCH_TRACE
for QA_CONVEX_ATTEMPT in 1 2; do
  QA_CONVEX_T0=$(date +%s)
  env -u CONVEX_DEPLOY_KEY NODE_OPTIONS="--require /root/qa/convex-b3-fetch-trace.cjs" bash -c 'set -a && source .env.local && set +a && timeout '"$QA_CONVEX_DEPLOY_WAIT"'s npx convex dev --once --typecheck=disable' >"$CONVEX_DEV_LOG" 2>&1
  CONVEX_DEV_EXIT=$?
  echo "QA_CONVEX_DEPLOY attempt=$QA_CONVEX_ATTEMPT rc=$CONVEX_DEV_EXIT elapsed=$(( $(date +%s) - QA_CONVEX_T0 ))s bound=${QA_CONVEX_DEPLOY_WAIT}s"
  [ "$CONVEX_DEV_EXIT" = "124" ] || break
done
""")
    text = replace(text, 'ERROR: npx convex dev --once timed out after 60s.', 'ERROR: npx convex dev --once timed out after 2 bounded attempts of ${QA_CONVEX_DEPLOY_WAIT}s.')
    # Nonsecret stage timestamps make setup time attributable without shell xtrace.
    for marker in ('[B.2]', '[B.3]', '[B.4]', '[B.5]', 'SETUP_COMPLETE (receipt:'):
        lines = text.splitlines(keepends=True)
        matches = [i for i, line in enumerate(lines) if 'echo "' in line and marker in line]
        if len(matches) != 1:
            raise RuntimeError('QA timing anchor drifted: ' + marker)
        i = matches[0]
        indent = lines[i][:len(lines[i])-len(lines[i].lstrip())]
        lines.insert(i, indent + 'echo "QA_STAGE marker=' + marker.split()[0] + ' epoch=$(date +%s)"\n')
        text = ''.join(lines)
    # Child preflight may generate/default verifier material; Next must use that exact tuple.
    text = replace(text, '  echo "SETUP_COMPLETE (receipt: $RECEIPT_PATH)"', '''  {
    for qa_auth_name in SYNTH_E2E_AUTH NEXT_PUBLIC_SYNTH_E2E_AUTH SYNTH_E2E_NO_CLERK SYNTH_CONVEX_PREVIEW SYNTH_E2E_SUBJECT SYNTH_JWT_ISSUER SYNTH_JWT_AUDIENCE SYNTH_JWT_KID SYNTH_JWT_JWKS SYNTH_JWT_JWKS_URL SYNTH_JWT_PRIVATE_KEY_PATH CHAT_LOG_SECRET; do
      printf 'export %s=%q\\n' "$qa_auth_name" "${!qa_auth_name-}"
    done
  } > /root/qa/preflight-auth.env
  chmod 600 /root/qa/preflight-auth.env
  echo "SETUP_COMPLETE (receipt: $RECEIPT_PATH)"''')
    if capture:
        text = text.replace('env LLM_PROXY_ENABLED=true STRICT_FIXTURES=true bash scripts/deploy-modal.sh',
                            'env LLM_PROXY_ENABLED=true STRICT_FIXTURES=false LLM_PROXY_FIXTURE_CAPTURE=true bash scripts/deploy-modal.sh')
    # S90: bake the QA provider into every per-box deploy explicitly (image .env() reads the deploy shell).
    # 2026-10-31 on-miss fix: remote-browser-box.sh exports CI=true for box test/build commands, and the Modal
    # processors bake CI into their images (#5880). A QA image with CI=true makes _qa_onmiss_active() false and the
    # proxy's ci_forward_blocked gate refuse every miss, so capture-on-miss (S104) could never activate. Unset CI for
    # the per-box Modal deploys ONLY (same as the canonical preflight's own `env -u CI ... deploy-modal.sh`);
    # STRICT_FIXTURES=true + the -qa-<box> suffix + the priced reservation still gate every live call.
    text = text.replace('env LLM_PROXY_ENABLED=true ', 'env -u CI GEMINI_PROVIDER="${QA_GEMINI_PROVIDER:-vertex}" LLM_PROXY_ENABLED=true ')
    if 'env LLM_PROXY_ENABLED=true ' in text or text.count('env -u CI GEMINI_PROVIDER=') < 3:
        raise RuntimeError('QA Modal deploy CI-unset anchor drifted; refuse a per-box deploy that would bake CI=true')
    changes = {preflight: text}
    deploy = root/'scripts/deploy-modal.sh'
    text = deploy.read_text()
    text = replace(text, 'resolve_modal_api_key_secret() {', '''resolve_modal_api_key_secret() {
  if [[ "${MODAL_ENV_SUFFIX:-}" == -qa-* ]]; then
    [[ "${REMOTE_BOX:-}" = 1 && "${QA_MODAL_BOX_ID:-}" =~ ^[0-9a-f]{12}$ && "$MODAL_ENV_SUFFIX" = "-qa-$QA_MODAL_BOX_ID" ]] || { echo 'Invalid disposable QA suffix' >&2; return 1; }
    : "${MODAL_TOKEN_ID:?CI Modal token missing}" "${MODAL_TOKEN_SECRET:?CI Modal token missing}"
    MODAL_API_KEY_SECRET_NAME="heydonna-qa-$QA_MODAL_BOX_ID"
    export MODAL_API_KEY_SECRET_NAME
    return 0
  fi''')
    # qaspeed C: the background pass 1 defers the Convex /pipeline canary (QA suffix only); the join runs it alone.
    text = replace(text, 'if [[ -n "$PIPELINE_AUTH_TOKEN" ]]; then\n  _canary_check "/pipeline/transcription-register"',
                   'if [[ "${QA_MODAL_SKIP_CANARY:-}" == 1 && "${MODAL_ENV_SUFFIX:-}" == -qa-* ]]; then\n'
                   '  echo "[canary] deferred: QA pass 1 overlaps the Convex deploy; canary runs at the [B.4] join"\n'
                   'elif [[ -n "$PIPELINE_AUTH_TOKEN" ]]; then\n  _canary_check "/pipeline/transcription-register"')
    text = replace(text, '  echo "[canary] Skipped — PIPELINE_AUTH_TOKEN not set (CI uses Modal secrets at runtime)"\nfi\n',
                   '  echo "[canary] Skipped — PIPELINE_AUTH_TOKEN not set (CI uses Modal secrets at runtime)"\nfi\n'
                   'if [[ "${QA_MODAL_CANARY_ONLY:-}" == 1 && "${MODAL_ENV_SUFFIX:-}" == -qa-* ]]; then\n'
                   '  echo "[canary] QA canary-only pass complete (no deploy)"; exit 0\nfi\n')
    text = replace(text, '-H "Authorization: Bearer ${PIPELINE_AUTH_TOKEN}" \\\n','--config <(printf \'header = "Authorization: Bearer %s"\\n\' "$PIPELINE_AUTH_TOKEN") \\\n')
    text = replace(text, '    echo "CONVEX_SITE_CANARY_FAILURE class=auth_rejection curl_exit=0 http_status=${CANARY_STATUS} route=${route}"', '''    if [[ "$CANARY_BODY" = Unauthorized ]]; then
      echo 'CONVEX_SITE_CANARY_BODY=Unauthorized'
    else
      echo 'CONVEX_SITE_CANARY_BODY=<redacted nonstandard response body>'
    fi
    echo "CONVEX_SITE_CANARY_FAILURE class=auth_rejection curl_exit=0 http_status=${CANARY_STATUS} route=${route}"''')
    # Only the disposable QA overlay parallelizes independent apps. Both passes still
    # target the same box-owned names; Modal reuses unchanged image layers naturally.
    # Match only top-level (unindented) calls: #14069 put audit/terms inside deploy-modal.sh's
    # production-only block, so a substring check skipped the QA deploy while endpoint resolution
    # (resolve(), ~line 1854) still required the app -> QA_MODAL_FAILED NotFoundError (2026-11-19).
    def _top_level(entry):
        return re.search(r'^deploy_and_log "' + re.escape(entry) + r'"$', text, re.M) is not None
    if (root/AUDIT_ENTRY).is_file() and not _top_level(AUDIT_ENTRY):
        text += '\ndeploy_and_log "' + AUDIT_ENTRY + '"\n'
    if (root/TERMS_ENTRY).is_file() and not _top_level(TERMS_ENTRY):
        text += '\ndeploy_and_log "' + TERMS_ENTRY + '"\n'
    if (root/SPEAKER_MAPPING_ENTRY).is_file() and not _top_level(SPEAKER_MAPPING_ENTRY):
        text += '\ndeploy_and_log "' + SPEAKER_MAPPING_ENTRY + '"\n'
    deploy_lines = re.findall(r'^deploy_and_log "(modal/[^"\n]+/processor.py)"$', text, re.M)
    if not deploy_lines or len(deploy_lines) != len(set(deploy_lines)):
        raise RuntimeError('QA parallel deploy anchor drifted')
    first = 'deploy_and_log "' + deploy_lines[0] + '"'
    parallel = """qa_deploy_all() {
  local qa_deploy_pids=() qa_deploy_failed=0 qa_deploy_pid
"""
    for entry in deploy_lines:
        parallel += '  deploy_and_log "' + entry + '" &\n  qa_deploy_pids+=("$!")\n'
    parallel += """  for qa_deploy_pid in "${qa_deploy_pids[@]}"; do
    wait "$qa_deploy_pid" || qa_deploy_failed=1
  done
  return "$qa_deploy_failed"
}
qa_deploy_started=$SECONDS
qa_deploy_log_offset=$(wc -c < logs/modal.log 2>/dev/null || echo 0)
if ! qa_deploy_all; then
  # Bounded resilience: ONE in-place retry per box (atomic mkdir marker spans both deploy passes),
  # only for a transient deploy error seen in THIS pass's log output. Never a loop.
  if [ -n "${QA_MODAL_BOX_ID:-}" ] &&
     tail -c +"$((qa_deploy_log_offset+1))" logs/modal.log 2>/dev/null |
       grep -qiE 'terminated due to external shut-?down|please try again|temporarily unavailable|connection (reset|aborted)|502 bad gateway|503 service|deadline exceeded' &&
     mkdir /root/qa/modal-deploy-retry-used 2>/dev/null; then
    echo "QA_MODAL_DEPLOY_RETRY reason=transient_deploy_error backoff_seconds=20 first_attempt_seconds=$((SECONDS-qa_deploy_started))"
    sleep 20
    qa_deploy_retry_started=$SECONDS
    if qa_deploy_all; then
      echo "QA_MODAL_DEPLOY_RETRY_RESULT=ok seconds=$((SECONDS-qa_deploy_retry_started))"
    else
      echo "QA_MODAL_DEPLOY_RETRY_RESULT=failed seconds=$((SECONDS-qa_deploy_retry_started))"
      exit 3
    fi
  else
    exit 3
  fi
fi
echo "QA_MODAL_DEPLOY_SECONDS=$((SECONDS-qa_deploy_started)) parallel=true"
"""
    text = replace(text, first, parallel)
    for entry in deploy_lines[1:]:
        # Remove only the original standalone call, not the new background call.
        text = replace(text, '\ndeploy_and_log "' + entry + '"\n', '\n')
    changes[deploy] = text
    membership = 'if env_suffix in ("-dev", "-slot1", "-slot2", "-slot3", "-slot4", "-slot5", "-slot6", "-slot7", "-slot8")'
    # Match only the tuple test so a multi-line "else" on the next line (#10988 reconcile_sonnet_openrouter) is overlaid too.
    # The number of api-key secret selections per processor is NOT hardcoded: audio gained a third one on main (#10274,
    # 2026-10-19) and more can follow. Fail closed on structure instead: at least one selection must exist, and every
    # membership expression must pair with exactly one api-secret expression, so no selection can be left un-overlaid
    # and no unrelated occurrence can be rewritten.
    api_secret = 'os.environ.get("MODAL_API_KEY_SECRET_NAME", "heydonna-modal-api-key")'
    for rel in ('modal/audio/processor.py', 'modal/docx/processor.py', 'modal/agent_review/processor.py'):
        p = root/rel
        text = p.read_text()
        count = text.count(membership)
        secret_count = text.count(api_secret)
        if count < 1 or count != secret_count:
            raise RuntimeError('Canonical QA overlay anchor drifted; refuse partial overlay (' + rel
                               + ' membership=' + str(count) + ' api_key_selections=' + str(secret_count) + ')')
        text = replace(text, membership, membership.replace('if env_suffix in', 'if env_suffix.startswith("-qa-") or env_suffix in'), count)
        # The container re-imports this module without the deploy shell's env.
        # Derive the owned secret from the suffix already baked into the image.
        qa_secret = '"heydonna-qa-" + env_suffix.removeprefix("-qa-")'
        text = replace(text, api_secret, '(' + qa_secret + ' if env_suffix.startswith("-qa-") else ' + api_secret + ')', secret_count)
        # Never let a QA callback carry the shared pipeline token or modify its secret.
        old = 'modal.Secret.from_name("heydonna-pipeline-auth-token")'
        if not text.count(old):
            raise RuntimeError('Canonical QA pipeline secret anchor drifted')
        text = replace(text, old, 'modal.Secret.from_name(' + qa_secret + ' if env_suffix.startswith("-qa-") else "heydonna-pipeline-auth-token")', text.count(old))
        changes[p] = text
    # Terms app: select the box-owned API-key secret like the other processors (no pipeline secret here).
    p = root/TERMS_ENTRY
    if p.is_file():
        text = p.read_text()
        if text.count(membership) != 1 or text.count(api_secret) != 1:
            raise RuntimeError('Canonical QA overlay anchor drifted; refuse partial overlay (' + TERMS_ENTRY + ')')
        text = replace(text, membership, membership.replace('if env_suffix in', 'if env_suffix.startswith("-qa-") or env_suffix in'))
        text = replace(text, api_secret, '("heydonna-qa-" + env_suffix.removeprefix("-qa-") if env_suffix.startswith("-qa-") else ' + api_secret + ')')
        changes[p] = text
    # Speaker mapping (#11549): one api-key selection keyed on the module-level `suffix`; box-owned secret like terms.
    p = root/SPEAKER_MAPPING_ENTRY
    if p.is_file():
        text = p.read_text()
        if text.count(api_secret) != 1:
            raise RuntimeError('Canonical QA overlay anchor drifted; refuse partial overlay (' + SPEAKER_MAPPING_ENTRY + ')')
        text = replace(text, api_secret, '("heydonna-qa-" + suffix.removeprefix("-qa-") if suffix.startswith("-qa-") else ' + api_secret + ')')
        changes[p] = text
    # QA-only, authenticated and unbilled probe observes the actual container env.
    # No caller/production validation or provider execution is changed.
    p = root/CHAT_TOOLS_ENTRY
    if p.is_file():
        text = p.read_text()
        text = replace(text, '    @app.post("/chat-tool")', '''    @app.get("/qa-provider-readiness")
    async def qa_provider_readiness(request: Request):
        expected = os.environ.get("MODAL_API_KEY", "")
        supplied = request.headers.get("X-API-Key", "")
        if not expected or not supplied or not hmac.compare_digest(expected, supplied):
            raise HTTPException(status_code=401, detail="Unauthorized")
        return {"provider": os.environ.get("GEMINI_PROVIDER", "ai_studio"),
                "proxy": os.environ.get("LLM_PROXY_ENABLED"),
                "strict": os.environ.get("STRICT_FIXTURES")}

    @app.post("/chat-tool")''')
        changes[p] = text
    # Editor chat uses SUFFIX (not env_suffix) and a literal shared invocation
    # secret. Both ingress and its proxy call must authenticate with this box's key.
    p = root/'modal/editor_chat/processor.py'
    if p.is_file():
        text = p.read_text()
        # Anchor on the secret token itself so added neighbours (e.g. "heydonna-r2-credentials", #11353) don't drift it.
        text = replace(text, '"heydonna-modal-api-key", "anthropic-credentials",',
                       '("heydonna-qa-" + SUFFIX.removeprefix("-qa-") if SUFFIX.startswith("-qa-") else "heydonna-modal-api-key"), "anthropic-credentials",')
        changes[p] = text
    # Audit uses the same literal shared secrets, including the pipeline reporter.
    # Replace both with the box-owned invocation/callback secret, never a shared key.
    p = root/AUDIT_ENTRY
    if p.is_file():
        changes[p] = overlay_audit_secrets(p.read_text())
    if box:
        proxy = root/'modal/shared/llm_proxy_server.py'
        ids = onmiss_effective(root, box, onmiss, exact, dry)
        if ids:
            print('QA_ONMISS_IDENTITY lease=' + ids['lease'] + ' spend_box=' + ids['spend_box'] + ' head=' + ids['head'][:12]
                  + ' cap_usd=' + str(ONMISS_CAP_USD) + ' store=' + ids['dict'])
        if ids and ids.get('exact'):
            print('QA_ONMISS_EXACT digest=' + ids['exact']['digest'] + ' approved=' + str(len(ids['exact']['entries']))
                  + ' approval_ref=' + ids['exact']['approval_ref'])
            if ids_out:
                record = {k: ids[k] for k in ('lease', 'spend_box', 'head', 'dict', 'box_key', 'attempt_key', 'stage_run', 'run_key')}
                record['manifest_digest'] = ids['exact']['digest']
                record['canonical_keys'] = sorted(ids['exact']['entries'])
                Path(ids_out).write_text(json.dumps(record, indent=2) + '\n')
        changes[proxy] = overlay_proxy(proxy.read_text(), box, capture, allow_billable_tools, tool_spend_cap_usd, ids)
    if exact and box:
        # Exact mode: no leased runtime may mount the shared R2 secret; every mount becomes this lease's QA-only secret.
        mount_files, mount_count = overlay_app_data_mounts(root, box, changes)
        print('QA_APP_DATA_MOUNTS replaced=' + str(mount_count) + ' files=' + str(mount_files) + ' lease_secret=' + r2_lease_secret_name(box))
    # Check every anchor before replacing any file; validate shell syntax on staged copies.
    staged = []
    for p, text in changes.items():
        n = p.with_name(p.name+'.qa-next')
        n.write_text(text)
        os.chmod(n, p.stat().st_mode)
        if p.suffix == '.sh':
            subprocess.run(['bash', '-n', str(n)], check=True)
        else:
            compile(text, str(p), 'exec')
        staged.append((p, n))
    for p, n in staged:
        n.replace(p)
    record_harness_patched([p for p, _ in staged])
    print('QA_MODAL_OVERLAY=applied files=' + str(len(staged)))


def overlay_proxy(text, box, capture=False, allow_billable_tools=False, tool_spend_cap_usd=2.0, onmiss=None):
    """Patch modal/shared/llm_proxy_server.py text for one owned QA box. Pure (text in, text out) so the stub proof
    exercises the exact deployed bytes. capture-on-miss keeps STRICT_FIXTURES=true for every non-proxy path."""
    source = ledger_source(box, allow_billable_tools, tool_spend_cap_usd, onmiss)
    if True:  # moved verbatim from overlay(); original indentation kept on purpose
        if capture:
            source += QA_OPENROUTER_SOURCE
        text = replace(text, '\ndef ensure_llm_proxy_started() -> None:', source + '\ndef ensure_llm_proxy_started() -> None:')
        text = replace(text, '        # ---- Forward to upstream ----', """        # Deployed QA overlay: reserve BEFORE any billed upstream call.
        try:
            if not path.endswith((':generateContent', ':streamGenerateContent', '/messages', '/chat/completions')):
                raise RuntimeError("QA capture operation not priced")
            _qa_or = False
            _qa_or_url = _qa_or_headers = _qa_send_body = None
            if _capture_mode() and provider == "gemini_api_key":
                # S90: AI Studio is never a QA forward target; translate to OpenRouter (fail closed, before reserve).
                _qa_or_url, _qa_or_headers, _qa_send_body, _qa_or_model = _qa_gemini_to_openrouter(path, body_bytes, model)
                _qa_or = True
            qa_ticket = _qa_onmiss_ticket or _qa_reserve(provider, model, request_hash, body_bytes)
        except Exception:
            # Pipeline stages may degrade after a proxy refusal. Persist the refusal
            # so the run report cannot mistake that degradation for full capture.
            def reject_capture(state):
                state["halted"] = True
                state["halt_reason"] = "QA_CAPTURE_REJECTED_CALL: capture coverage or spend bound refused a pipeline request"
                state.setdefault("rejected_calls", []).append({"provider": provider, "model": model, "hash": request_hash})
            _qa_update(reject_capture)
            return JSONResponse({"error": "QA_CAPTURE_BLOCKED", "detail": "Run/tool admission budget, ledger failure or unsupported model/tool; no provider call"}, status_code=402)
        # ---- Forward to upstream ----""")
        # Charge completed wire responses BEFORE any persistence/quality decisions.
        text = replace(text, '        upstream_latency_ms = (time.perf_counter_ns() - t0) // 1_000_000\n        _log_action(',
                       '        _qa_settle(qa_ticket, raw)\n        upstream_latency_ms = (time.perf_counter_ns() - t0) // 1_000_000\n        _log_action(')
        # GET misses can perform paid STT polling/upload operations: block live GET.
        get_anchor = '        client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))'
        text = replace(text, get_anchor, '        return JSONResponse({"error": "QA_CAPTURE_GET_NOT_PRICED"}, status_code=402)\n' + get_anchor)
        if capture:
            text = replace(text, '        upstream_url = _build_upstream_url(upstream_base, path, request.url.query)\n\n        forward_headers = _filter_request_headers(headers)\n',
                           '        upstream_url = _build_upstream_url(upstream_base, path, request.url.query)\n        if _qa_or:\n            upstream_url = _qa_or_url\n\n        forward_headers = _filter_request_headers(headers)\n')
            text = replace(text, '                    headers=forward_headers,\n                    content=body_bytes,\n',
                           '                    headers=(_qa_or_headers if _qa_or else forward_headers),\n                    content=(_qa_send_body if _qa_or else body_bytes),\n')
            text = replace(text, '        _qa_settle(qa_ticket, raw)\n',
                           '        if _qa_or:\n            raw = _qa_openrouter_to_gemini(raw, upstream_resp.status_code)\n        _qa_settle(qa_ticket, raw)\n')
    # capture-on-miss (default only where the QA controller passes --on-miss; spec/CI lanes never do).
    text = replace(text, '        # ---- MISS ----\n        if _strict_fixtures():\n', """        # ---- MISS ----
        # Deployed QA overlay: capture-on-miss admission (server-baked mode; replay already missed above).
        _qa_onmiss_ticket = None
        if _qa_onmiss_active():
            _qa_onmiss_result = await _qa_onmiss_admit(provider, model, path, key, body_bytes)
            if isinstance(_qa_onmiss_result, str):
                _qa_onmiss_ticket = _qa_onmiss_result
            else:
                return _qa_onmiss_result
        if _strict_fixtures() and _qa_onmiss_ticket is None:
""")
    text = replace(text, '            _log_action("llm_proxy_upstream_error", **_diag)\n',
                   '            if _qa_onmiss_ticket:\n                _qa_onmiss_mark(key, "unknown", "upstream_send_error: possibly accepted; never re-dispatched")\n'
                   '            _log_action("llm_proxy_upstream_error", **_diag)\n')
    text = replace(text, '            _log_action("llm_proxy_upstream_read_error", **_diag)\n',
                   '            if _qa_onmiss_ticket:\n                _qa_onmiss_mark(key, "unknown", "upstream_read_error: possibly billed; never re-dispatched")\n'
                   '            _log_action("llm_proxy_upstream_read_error", **_diag)\n')
    text = replace(text, '        if _bad is not None:\n            if capture_enabled:\n                record_capture_attempt(request_hash, upstream_resp.status_code, False, _proxy_request_id)\n',
                   '        if _bad is not None:\n            if _qa_onmiss_ticket:\n                _qa_onmiss_mark(key, "upstream_error", "unpersistable: " + _bad)\n'
                   '            if capture_enabled:\n                record_capture_attempt(request_hash, upstream_resp.status_code, False, _proxy_request_id)\n')
    text = replace(text, '                    "capture_run_id": capture_run_id,\n',
                   '                    "capture_run_id": capture_run_id,\n                    **_qa_onmiss_health(),\n')
    if onmiss and onmiss.get('exact'):
        # Exact mode: the ingress identity reaches admission, and the dispatch identity is recorded BEFORE settlement.
        text = replace(text, '_qa_onmiss_admit(provider, model, path, key, body_bytes)',
                       '_qa_onmiss_admit(provider, model, path, key, body_bytes, request_id=_proxy_request_id, section_type=_proxy_section_type)')
        text = replace(text, '        _qa_settle(qa_ticket, raw)\n',
                       '        _qa_exact_note_dispatch(qa_ticket, upstream_resp, raw)\n        _qa_settle(qa_ticket, raw)\n')
    return text


HARNESS_MANIFEST = '/root/qa/.harness-patched.tsv'
ENV_COPY_RECORD = '/root/qa/.rtb-env-copies.tsv'


ENV_FENCE_FILE = '/var/lib/rtb/gen'   # pool box fence: the CURRENT lease generation while leased (digits only)


def _env_lease():
    """Current pool lease generation, or None (not a pool box / not leased: X/R/F fences)."""
    try:
        with open(ENV_FENCE_FILE) as f:
            g = f.read().strip()
    except OSError:
        return None
    return g if re.fullmatch(r'[1-9][0-9]*', g) else None


def _env_attested(path, data):
    """CTO REVISE ts 1793627705.456019: the attestation may only ADVANCE across an authorized QA rewrite of an env copy
    that is ALREADY attested. Returns the lease iff `data` (the snapshot about to be transformed) matches a row
    "<sha256>\t<canonical path>\t<lease>" for this exact canonical path AND the current lease; else None."""
    lease = _env_lease()
    if not lease or os.path.islink(path) or not os.path.isfile(path) or not os.path.exists(ENV_COPY_RECORD):
        return None
    canon = os.path.realpath(path)
    h = hashlib.sha256(data).hexdigest()
    with open(ENV_COPY_RECORD) as f:
        rows = f.read().splitlines()
    for line in rows:
        parts = line.rstrip('\n').split('\t')
        if len(parts) != 3 or not re.fullmatch(r'[0-9a-f]{64}', parts[0]):
            continue
        rp = os.path.join(os.path.realpath(os.path.dirname(parts[1])), os.path.basename(parts[1]))
        if parts[0] == h and rp == canon and parts[2] == lease:
            return lease
    return None


def env_rewrite(path, snapshot, new_text, label):
    """Replace the env copy with `new_text` (built from `snapshot`). Refuses if the file changed since the snapshot.
    Advances the custody attestation ONLY when the snapshot itself was attested for this path + lease; otherwise records
    nothing (the pool reset then keeps refusing it). Prints the outcome, never any value."""
    lease = _env_attested(path, snapshot)
    with open(path, 'rb') as f:
        current = f.read()
    if current != snapshot:
        raise ValueError('env copy changed during the QA rewrite; not replaced')
    n = path.with_name(path.name + '.qa-next')
    fd = os.open(n, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(new_text)
    os.chmod(n, 0o600)
    n.replace(path)
    with open(path, 'rb') as f:
        written = f.read()
    if lease and written == new_text.encode():
        _append_env_row(path, new_text.encode(), lease)
        print('QA_ENV_ATTESTATION=advanced step=' + label)
    else:
        print('QA_ENV_ATTESTATION=not_advanced step=' + label + ' (input not attested for this path+lease; reset will refuse it)')


def _append_env_row(path, data, lease):
    os.makedirs(os.path.dirname(ENV_COPY_RECORD), mode=0o700, exist_ok=True)
    row = hashlib.sha256(data).hexdigest() + '\t' + os.path.realpath(path) + '\t' + lease + '\n'
    fd = os.open(ENV_COPY_RECORD, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, 'a') as f:
        f.write(row)

def record_harness_patched(paths):
    """PM 2026-10-29: pool preservation must not snapshot harness overlays (the repo secret-scan blocks them and they are
    not owner work). Record "<sha256 of the patched content>\t<absolute path>" per file. rtb-pool-reset.sh restores a file
    to HEAD in the snapshot ONLY while it still byte-matches this hash, so a later real edit is always preserved.
    /root/qa is wiped and verified absent by every pool reset."""
    import hashlib
    os.makedirs(os.path.dirname(HARNESS_MANIFEST), mode=0o700, exist_ok=True)
    rows = {}
    if os.path.exists(HARNESS_MANIFEST):
        for line in open(HARNESS_MANIFEST):
            h, _, path = line.rstrip('\n').partition('\t')
            if path:
                rows[path] = h
    for path in paths:
        rows[str(os.path.abspath(path))] = hashlib.sha256(open(path, 'rb').read()).hexdigest()
    tmp = HARNESS_MANIFEST + '.tmp'
    with open(tmp, 'w') as f:
        f.write(''.join(h + '\t' + path + '\n' for path, h in sorted(rows.items())))
    os.chmod(tmp, 0o600)
    os.replace(tmp, HARNESS_MANIFEST)



QA_OPENROUTER_SOURCE = r'''
# S90 (Rajiv 2026-10-21): capture-mode QA forwards Gemini AI Studio (gemini_api_key) misses to
# OpenRouter with the same model id (google/ vendor slug), never to generativelanguage.googleapis.com.
_QA_OR_URL = "https://openrouter.ai/api/v1/chat/completions"

def _qa_lower_types(node):
    if isinstance(node, dict):
        return {k: (v.lower() if k == "type" and isinstance(v, str) else _qa_lower_types(v)) for k, v in node.items()}
    if isinstance(node, list):
        return [_qa_lower_types(v) for v in node]
    return node

def _qa_openrouter_model(model):
    return model if "/" in model else "google/" + model

def _qa_gemini_to_openrouter(path, body_bytes, model, environ=None):
    """Return (url, headers, body_bytes, openrouter_model). Raises (fail closed, no provider call)."""
    environ = os.environ if environ is None else environ
    if not path.endswith(":generateContent"):
        raise RuntimeError("QA OpenRouter route supports non-streaming generateContent only")
    key = environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY missing in this container; refusing AI Studio fallback")
    src = json.loads(body_bytes)
    messages, pending = [], {}
    system = src.get("systemInstruction") or {}
    stext = "".join(p.get("text", "") for p in system.get("parts", []))
    if stext:
        messages.append({"role": "system", "content": stext})
    n = 0
    for turn in src.get("contents", []):
        role = "assistant" if turn.get("role") == "model" else "user"
        text, calls, results = "", [], []
        for part in turn.get("parts", []):
            if "text" in part and not part.get("thought"):
                text += part["text"]
            elif "functionCall" in part:
                n += 1
                fc = part["functionCall"]
                cid = "call_" + str(n)
                pending.setdefault(fc["name"], []).append(cid)
                calls.append({"id": cid, "type": "function", "function": {"name": fc["name"], "arguments": json.dumps(fc.get("args", {}))}})
            elif "functionResponse" in part:
                fr = part["functionResponse"]
                ids = pending.get(fr["name"]) or []
                n += 1
                results.append({"role": "tool", "tool_call_id": ids.pop(0) if ids else "call_" + str(n),
                                "content": json.dumps(fr.get("response", {}))})
            else:
                raise RuntimeError("QA OpenRouter route cannot translate Gemini part: " + ",".join(sorted(part)))
        if results:
            messages.extend(results)
        if text or calls:
            msg = {"role": role, "content": text}
            if calls:
                msg["tool_calls"] = calls
            messages.append(msg)
    tools = []
    for tool in src.get("tools", []):
        if set(tool) - {"functionDeclarations"}:
            raise RuntimeError("QA OpenRouter route cannot translate Gemini built-in tools: " + ",".join(sorted(tool)))
        for d in tool.get("functionDeclarations", []):
            fn = {"name": d["name"], "description": d.get("description", "")}
            if d.get("parameters"):
                fn["parameters"] = _qa_lower_types(d["parameters"])
            tools.append({"type": "function", "function": fn})
    gen = src.get("generationConfig", {})
    ormodel = _qa_openrouter_model(model)
    out = {"model": ormodel, "messages": messages, "stream": False}
    if tools:
        out["tools"] = tools
    if "temperature" in gen:
        out["temperature"] = gen["temperature"]
    if "maxOutputTokens" in gen:
        out["max_tokens"] = gen["maxOutputTokens"]
    level = (gen.get("thinkingConfig") or {}).get("thinkingLevel")
    if isinstance(level, str) and level.lower() in ("low", "medium", "high"):
        out["reasoning"] = {"effort": level.lower()}
    headers = {"content-type": "application/json", "accept-encoding": "identity", "authorization": "Bearer " + key}
    return _QA_OR_URL, headers, json.dumps(out, separators=(",", ":")).encode(), ormodel

def _qa_openrouter_to_gemini(raw, status):
    if not (200 <= status < 300):
        return raw
    d = json.loads(raw)
    choice = (d.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    parts = []
    if msg.get("content"):
        parts.append({"text": msg["content"]})
    for c in msg.get("tool_calls") or []:
        fn = c.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {}
        parts.append({"functionCall": {"name": fn.get("name", ""), "args": args}})
    usage = d.get("usage") or {}
    finish = {"stop": "STOP", "length": "MAX_TOKENS", "tool_calls": "STOP"}.get(choice.get("finish_reason"), "OTHER")
    out = {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish, "index": 0}],
           "modelVersion": d.get("model", "")}
    if "prompt_tokens" in usage:
        out["usageMetadata"] = {"promptTokenCount": usage.get("prompt_tokens", 0),
                                "candidatesTokenCount": usage.get("completion_tokens", 0),
                                "totalTokenCount": usage.get("total_tokens", 0)}
    return json.dumps(out, separators=(",", ":")).encode()
'''


def route_check(capture):
    """Dry check: resolved upstream route for a gemini_api_key (AI Studio) miss. No network."""
    ns = {'os': os, 'json': json}
    exec(QA_OPENROUTER_SOURCE, ns)
    model = 'gemini-3.1-flash-lite'
    body = json.dumps({'contents': [{'role': 'user', 'parts': [{'text': 'hi'}]}],
                       'generationConfig': {'temperature': 0.2}}).encode()
    path = '/v1beta/models/' + model + ':generateContent'
    route = {'capture': capture, 'requested_provider': 'gemini_api_key', 'requested_model': model}
    if not capture:
        route.update(resolved_provider='unchanged (strict replay, no forwarding)', url='n/a')
    else:
        url, _h, out, ormodel = ns['_qa_gemini_to_openrouter'](path, body, model, {'OPENROUTER_API_KEY': 'dry-run'})
        route.update(resolved_provider='openrouter', url=url, model=ormodel, body_model=json.loads(out)['model'],
                     ai_studio_host_used=False)
        for label, p, env in (('missing_key', path, {}), ('streaming', path.replace(':generateContent', ':streamGenerateContent'), {'OPENROUTER_API_KEY': 'dry-run'})):
            try:
                ns['_qa_gemini_to_openrouter'](p, body, model, env)
                route[label] = 'UNEXPECTED_ALLOW'
            except RuntimeError as e:
                route[label] = 'fail-closed: ' + str(e)
    print('QA_CAPTURE_ROUTE=' + json.dumps(route, sort_keys=True))


QA_ONMISS_OFF_SOURCE = r'''
def _qa_onmiss_active():
    return False

def _qa_onmiss_health():
    import re as _re
    return {"qa_onmiss": False, "overlay_baked": False,
            "strict": bool(_strict_fixtures()), "capture": bool(_capture_mode()),
            "ci": os.environ.get("CI", "").lower() == "true",
            "suffix_ok": bool(_re.fullmatch(r"-qa-[0-9a-f]{12}", os.environ.get("MODAL_ENV_SUFFIX", "")))}

def _qa_onmiss_mark(key, new_state, reason=""):
    return None
'''


QA_ONMISS_SOURCE = r'''
# capture-on-miss (Rajiv A ts 1793377559.938959; CTO qualified option 1 ts 1793378243.332849). Replay first; ONE eligible
# miss is priced, durably claimed and reserved BEFORE upstream, dispatched once, and its payload stays in this box's own
# Modal Dict. Spend metadata (no payloads) lives in the existing R2 ledger, keyed per physical box AND per attempt.
_QA_ONMISS = True
_QA_ONMISS_CAP_USD = __ONMISS_CAP_USD__
_QA_ONMISS_DICT = __ONMISS_DICT__
_QA_ONMISS_LEASE = __ONMISS_LEASE__
_QA_ONMISS_SPEND_ID = __ONMISS_SPEND_ID__
_QA_ONMISS_BOX_KEY = __ONMISS_BOX_KEY__
_QA_ONMISS_ATTEMPT_KEY = __ONMISS_ATTEMPT_KEY__
_QA_ONMISS_RUN = __ONMISS_RUN__  # staging run id; qa_finish publishes it canonical PUT-if-absent (S161)
_QA_ONMISS_WAIT_S = 960  # >= the proxy's 900s LLM upstream timeout; bounded, never endless pending
# Per-run replay events stay in the per-run ledger (_QA_LEDGER); spend settles in box-lifetime AND attempt ledgers.
_QA_LEDGER_KEYS = [_QA_ONMISS_BOX_KEY, _QA_ONMISS_ATTEMPT_KEY]
_QA_ONMISS_OPS = (":generateContent", ":streamGenerateContent", "/messages", "/chat/completions")
_QA_ONMISS_GEMINI_FIELDS = {"contents", "systemInstruction", "generationConfig", "tools", "toolConfig", "safetySettings", "labels"}
_QA_EXACT = __ONMISS_EXACT__  # None (stock --on-miss) or {"digest", "approval_ref", "entries": {canonical_key: {hash, provider, model, operation}}}
_QA_SCOPED_BUCKET = __SCOPED_BUCKET__
_QA_EXACT_SESSION_WAIT_S = __EXACT_WAIT_S__

def _qa_onmiss_active():
    import re as _re
    return bool(_QA_ONMISS and _strict_fixtures() and not _capture_mode()
                and os.environ.get("CI", "").lower() != "true"
                and _re.fullmatch(r"-qa-[0-9a-f]{12}", os.environ.get("MODAL_ENV_SUFFIX", "")))

def _qa_onmiss_health():
    import re as _re
    return {"qa_onmiss": _qa_onmiss_active(), "qa_onmiss_cap_usd": _QA_ONMISS_CAP_USD,
            "overlay_baked": bool(_QA_ONMISS),
            **({"qa_onmiss_exact": True, "qa_exact_digest": _QA_EXACT["digest"], "qa_exact_dry": bool(_QA_EXACT.get("dry")), "qa_exact_profile": _QA_EXACT.get("profile")} if _QA_EXACT else {}),
            "strict": bool(_strict_fixtures()), "capture": bool(_capture_mode()),
            "ci": os.environ.get("CI", "").lower() == "true",
            "suffix_ok": bool(_re.fullmatch(r"-qa-[0-9a-f]{12}", os.environ.get("MODAL_ENV_SUFFIX", "")))}

def _qa_onmiss_dict():
    import modal
    return modal.Dict.from_name(_QA_ONMISS_DICT, create_if_missing=True)

def _qa_onmiss_store_get(key):
    try:
        return _qa_onmiss_dict().get(key)
    except Exception:
        return None  # local miss; the durable claim still forbids a resend

def _qa_onmiss_store_put(key, record):
    _qa_onmiss_dict().put(key, record)

def _qa_onmiss_thread(fn, *args):
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        return pool.submit(fn, *args).result()

def _qa_onmiss_serve(record):
    return Response(content=record["body"], status_code=record["status"], media_type=record["content_type"],
                    headers={"x-heydonna-qa-onmiss": "local-hit"})

def _qa_onmiss_terminal(status, code, detail, key):
    return JSONResponse({"error": code, "detail": detail, "key": key, "terminal": True, "qa_onmiss": True},
                        status_code=status, headers={"x-heydonna-qa-onmiss": code})

def _qa_onmiss_price(provider, model, path, body_bytes):
    if provider == "gemini_api_key":
        raise RuntimeError("QA_ONMISS_AI_STUDIO_NOT_FORWARDED (S90)")
    if not path.endswith(_QA_ONMISS_OPS):
        raise RuntimeError("QA_ONMISS_OPERATION_NOT_PRICED")
    if provider == "gemini_vertex":
        data = json.loads(body_bytes)
        extra = set(data) - _QA_ONMISS_GEMINI_FIELDS  # e.g. cachedContent carries storage/cached-token charges
        if extra:
            raise RuntimeError("QA_ONMISS_FIELD_NOT_PRICED: " + ",".join(sorted(extra)))
        modalities = (data.get("generationConfig") or {}).get("responseModalities")
        if modalities not in (None, ["TEXT"], ["text"]):
            raise RuntimeError("QA_ONMISS_OUTPUT_MODALITY_NOT_PRICED")
    priced = _qa_price(provider, model, body_bytes)
    if priced["billable_tools"]:
        raise RuntimeError("QA_ONMISS_PAID_TOOLS_DISABLED")
    return priced

def _qa_onmiss_note(key, provider, model, code, detail):
    def note(state):
        state.setdefault("refused_calls", []).append({"lease": _QA_ONMISS_LEASE, "key": key, "provider": provider,
                                                      "hash": str(key).rsplit("/", 1)[-1].removesuffix(".bin")[:64],
                                                      "model": model, "code": code, "detail": detail[:200], "at": int(time.time())})
    try:
        _qa_update(note, keys=[_QA_ONMISS_BOX_KEY])
    except Exception:
        pass

def _qa_onmiss_claim(key, provider, model, priced, request_id="", section_type=""):
    """Atomic durable claim + priced reservation: attempt ledger first (exact-request single-flight), then box lifetime."""
    ticket = uuid.uuid4().hex
    bound = priced["bound"]
    def claim(single_flight):
        def mutate(state):
            requests = state.setdefault("requests", {})
            prior = requests.get(key)
            if ticket in state["attempts"]:
                return None  # our own write landed but its ACK was lost: the CAS retry sees it, never a second claim
            if _QA_EXACT:
                # Custody must have been reconciled by the trusted host for THIS approval (legacy + QA, box + head) before ANY
                # reservation, and the priced bound may never exceed the single-request bound the approval named.
                if (state.get("custody") or {}).get("manifest_digest") != _QA_EXACT["digest"]:
                    return ("custody", "QA_ONMISS_CUSTODY_UNRECONCILED: legacy + QA custody is not bound to this approval on this ledger")
                if bound > _QA_EXACT["max_reserve_usd"]:
                    return ("bound", "QA_ONMISS_BOUND_EXCEEDS_APPROVAL: priced bound %.6f > approved %.6f" % (bound, _QA_EXACT["max_reserve_usd"]))
            # Exact mode never reclaims an earlier lease's completed claim: one approved dispatch per exact hash, ever.
            reclaim = (prior and prior.get("state") == "completed" and prior.get("lease") != _QA_ONMISS_LEASE and not _QA_EXACT)
            # A completed response from an EARLIER lease was deleted with that lease's local store: one fresh priced claim,
            # charged to the same attempt + box-lifetime caps (never a renewal). Same lease: reuse or no-resend only.
            if single_flight and prior and prior.get("state") != "refused" and not reclaim:
                return ("existing", prior.get("state", ""), prior.get("at", 0))
            if not single_flight and prior and prior.get("state") in ("reserved", "unknown", "upstream_error", "completed_unstored"):
                # Same physical box, earlier attempt/lease: an accepted or uncertain request is never re-dispatched.
                return ("existing", prior.get("state", ""), prior.get("at", 0))
            committed = sum(a["charged_bound_usd"] for a in state["attempts"].values())
            if state.get("halted") or committed + bound > _QA_ONMISS_CAP_USD:
                if not state.get("halted"):
                    state["halted"] = True
                    state["halt_reason"] = ("QA_ONMISS_SPEND_CAP: committed %.4f + next %.4f > $%s (%s)"
                                            % (committed, bound, _QA_ONMISS_CAP_USD, "attempt" if single_flight else "box lifetime"))
                return ("cap", state["halt_reason"])
            state["attempts"][ticket] = {"hash": key.rsplit("/", 1)[-1][:64], "onmiss_key": key, "lease": _QA_ONMISS_LEASE,
                "spend_box": _QA_ONMISS_SPEND_ID, "provider": provider, "model": model,
                "reserved_usd": bound, "charged_bound_usd": bound, "settled": False, "billable_tools": False,
                "reserved_search_cost_usd": 0, "per_search_usd": 0,
                "input_rate_per_million": priced["input_rate"], "output_rate_per_million": priced["output_rate"]}
            if _QA_EXACT:  # join identity, never payload text
                state["attempts"][ticket].update(ingress_request_id=str(request_id)[:200], section_type=str(section_type)[:100],
                                                 approval_ref=_QA_EXACT["approval_ref"], manifest_digest=_QA_EXACT["digest"])
            requests[key] = {"state": "reserved", "ticket": ticket, "lease": _QA_ONMISS_LEASE,
                             "provider": provider, "model": model, "at": int(time.time())}
            return None
        return mutate
    refusal = _qa_update(claim(True), keys=[_QA_ONMISS_ATTEMPT_KEY])
    if refusal:
        return refusal
    refusal = _qa_update(claim(False), keys=[_QA_ONMISS_BOX_KEY])
    if refusal:
        def rollback(state):  # nothing was dispatched: pre-dispatch release only
            state["attempts"].pop(ticket, None)
            row = state.get("requests", {}).get(key)
            if row and row.get("ticket") == ticket:
                row["state"] = "refused"
        _qa_update(rollback, keys=[_QA_ONMISS_ATTEMPT_KEY])
        return refusal
    return ("ticket", ticket)

def _qa_onmiss_claim_state(key):
    row = (_qa_ledger_read(_QA_ONMISS_ATTEMPT_KEY).get("requests") or {}).get(key) or {}
    return row.get("state", ""), row.get("at", 0)

def _qa_onmiss_mark(key, new_state, reason=""):
    def mark(state):
        row = state.setdefault("requests", {}).get(key)
        if row and row.get("lease") == _QA_ONMISS_LEASE:
            row.update(state=new_state, reason=reason[:200], at=int(time.time()))
    try:
        _qa_update(mark)
    except Exception:
        pass  # an unmarked claim stays "reserved", which already forbids any resend

async def _qa_onmiss_admit(provider, model, path, key, body_bytes, request_id="", section_type=""):
    """Returns a reservation ticket (dispatch once) or a typed terminal Response. Never a silent pending."""
    import asyncio
    if _QA_EXACT:
        # Exact allowlist FIRST: a nonmember reaches no pricing, claim, reservation or provider I/O.
        denied = _qa_exact_gate(provider, model, key)
        if denied:
            print(denied)
            await asyncio.to_thread(_qa_onmiss_note, key, provider, model, "QA_ONMISS_NOT_APPROVED", denied)
            return _qa_onmiss_terminal(424, "QA_ONMISS_NOT_APPROVED", denied + "; no provider call", key)
    record = await asyncio.to_thread(_qa_onmiss_store_get, key)
    if record is not None:
        return _qa_onmiss_serve(record)
    if _QA_EXACT:
        try:
            absent = await asyncio.to_thread(_qa_exact_confirm_miss, key)
        except Exception as error:
            detail = "authoritative replay lookup failed (" + type(error).__name__ + ")"
            print("QA_ONMISS_LOOKUP_UNCERTAIN hash=" + _qa_exact_hash(key) + " " + detail)
            await asyncio.to_thread(_qa_onmiss_note, key, provider, model, "QA_ONMISS_LOOKUP_UNCERTAIN", detail)
            return _qa_onmiss_terminal(503, "QA_ONMISS_LOOKUP_UNCERTAIN", detail + "; a lookup error is not a miss; no provider call", key)
        if not absent:
            detail = "the exact fixture already exists; replay it, never dispatch"
            await asyncio.to_thread(_qa_onmiss_note, key, provider, model, "QA_ONMISS_FIXTURE_EXISTS", detail)
            return _qa_onmiss_terminal(409, "QA_ONMISS_FIXTURE_EXISTS", detail + "; no provider call", key)
        if _QA_EXACT.get("dry"):
            print("QA_ONMISS_DRY_CHECK_MISSING hash=" + _qa_exact_hash(key) + " authoritative_miss=true")
            return _qa_onmiss_terminal(424, "QA_ONMISS_DRY_CHECK_MISSING", "authoritative replay miss proven on the scoped path; the dry-check never prices, claims, reserves or dispatches", key)
    try:
        priced = _qa_onmiss_price(provider, model, path, body_bytes)
    except Exception as error:
        detail = str(error)[:200]
        await asyncio.to_thread(_qa_onmiss_note, key, provider, model, "QA_ONMISS_INELIGIBLE", detail)
        return _qa_onmiss_terminal(424, "QA_ONMISS_INELIGIBLE", detail + "; no provider call", key)
    try:
        outcome = await asyncio.to_thread(_qa_onmiss_claim, key, provider, model, priced, request_id, section_type)
    except Exception as error:
        return _qa_onmiss_terminal(503, "QA_ONMISS_LEDGER_UNAVAILABLE", type(error).__name__ + "; no provider call", key)
    if outcome[0] == "ticket":
        return outcome[1]
    if outcome[0] in ("custody", "bound"):
        code = "QA_ONMISS_CUSTODY_UNRECONCILED" if outcome[0] == "custody" else "QA_ONMISS_BOUND_EXCEEDS_APPROVAL"
        await asyncio.to_thread(_qa_onmiss_note, key, provider, model, code, outcome[1])
        return _qa_onmiss_terminal(402, code, outcome[1] + "; no provider call", key)
    if outcome[0] == "cap":
        await asyncio.to_thread(_qa_onmiss_note, key, provider, model, "QA_ONMISS_SPEND_CAP", outcome[1])
        return _qa_onmiss_terminal(402, "QA_ONMISS_SPEND_CAP", outcome[1] + "; no provider call", key)
    state, claimed_at = outcome[1], outcome[2]
    while True:  # an accepted twin: reuse its completed response, bounded by the upstream timeout
        record = await asyncio.to_thread(_qa_onmiss_store_get, key)
        if record is not None:
            return _qa_onmiss_serve(record)
        if state != "reserved" or time.time() >= claimed_at + _QA_ONMISS_WAIT_S:
            break
        await asyncio.sleep(1)
        state, claimed_at = await asyncio.to_thread(_qa_onmiss_claim_state, key)
    code = "QA_ONMISS_IN_FLIGHT_TIMEOUT" if state == "reserved" else "QA_ONMISS_NO_RESEND"
    await asyncio.to_thread(_qa_onmiss_note, key, provider, model, code, "state=" + state)
    return _qa_onmiss_terminal(424, code, "state=" + state + "; accepted or uncertain request is never re-dispatched", key)

_qa_onmiss_original_settle = _qa_settle
def _qa_settle(ticket, raw):
    try:
        return _qa_onmiss_original_settle(ticket, raw)
    except Exception:
        row = (_qa_ledger_read(_QA_ONMISS_ATTEMPT_KEY).get("attempts") or {}).get(ticket) or {}
        if row.get("onmiss_key"):
            _qa_onmiss_mark(row["onmiss_key"], "unknown", "settle failed: usage unproven or above bound; budget retained")
        raise

def _qa_onmiss_stage(fields):
    """Stage one on-miss reply exactly like capture mode (_persist_response_record capture branch). 2xx only; rule 35
    fallback-rung keys are never staged. Conditional-create: a twin's identical staged copy is fine."""
    canonical_key = fields["key"]
    if not (200 <= int(fields["status"]) < 300):
        return None
    if any(m in "/" + canonical_key.lower().lstrip("/") for m in PROHIBITED_RUNG_PATH_MARKERS):
        return None
    staging_key = "staging/llm-capture/" + _QA_ONMISS_RUN + "/" + _CAPTURE_SESSION_ID + "/" + canonical_key
    section_type = str(fields.get("section_type") or "")
    request_id = str(fields.get("request_id") or "")
    store = _r2_store()
    store.put(staging_key + ".request", bytes(fields["request_bytes"]), metadata={
        "fixture-version": str(FIXTURE_VERSION), "record-kind": "request", "canonical-key": canonical_key,
        "request-id": request_id[:500], "section-type": section_type[:200]})
    store.put(staging_key, bytes(fields["raw"]), metadata={
        "content-type": str(fields["content_type"])[:200], "response-status": str(int(fields["status"])),
        "response-headers-b64": _encode_response_headers(fields.get("response_headers") or {}),
        "captured-at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "fixture-version": str(FIXTURE_VERSION),
        "provider": str(fields.get("provider") or ""), "model": str(fields.get("model") or "")[:200],
        "canonical-key": canonical_key, "request-id": request_id[:500], "section-type": section_type[:200]})
    return staging_key

_qa_onmiss_original_persist = _persist_response_record
def _persist_response_record(**fields):
    """capture-on-miss: payload goes to the box-local store (serving) AND run staging (S161: published canonical
    PUT-if-absent at finish, same publisher guard as capture); claim -> completed."""
    if not _qa_onmiss_active():
        return _qa_onmiss_original_persist(**fields)
    key = fields["key"]
    try:
        _qa_onmiss_thread(_qa_onmiss_stage, fields)
    except Exception as error:  # the reply is still served locally; only its publication is lost, and that is visible
        print("QA_ONMISS_STAGE_FAILED key=" + str(key)[-80:] + " error=" + type(error).__name__)
    record = {"body": bytes(fields["raw"]), "status": int(fields["status"]),
              "content_type": str(fields["content_type"]), "stored_at": int(time.time())}
    try:
        _qa_onmiss_thread(_qa_onmiss_store_put, key, record)
    except Exception:
        _qa_onmiss_mark(key, "completed_unstored", "box-local store write failed")
        raise
    _qa_onmiss_mark(key, "completed")
    return True, "qa-onmiss-local:" + _QA_ONMISS_DICT, None

'''


QA_ONMISS_EXACT_SOURCE = r'''
# ---- exact-hash scoped mode (_QA_EXACT is None for stock --on-miss): allowlist gate, prefix-scoped five-minute sessions armed in
# this box's Modal Dict, and NO fallback to the application R2 signer for any fixture, staging or ledger client. -----------------
if _QA_EXACT:
    import boto3 as _qa_boto3
    import threading as _qa_threading
    _QA_EXACT_CTX = _qa_threading.local()
    _QA_EXACT_CACHE = {}

    def _qa_exact_hash(key):
        return str(key).rsplit("/", 1)[-1].removesuffix(".bin")[:64]

    def _qa_exact_gate(provider, model, key):
        """Exact per-run allowlist: the full canonical key plus provider and model. None = approved."""
        entry = _QA_EXACT["entries"].get(key)
        if entry and entry["provider"] == provider and entry["model"] == model and entry["hash"] == _qa_exact_hash(key):
            return None
        return ("QA_ONMISS_NOT_APPROVED: request hash " + _qa_exact_hash(key) + " (" + str(provider) + "/" + str(model)
                + ") is not on this run's exact approval list")

    def _qa_scoped_session(kind):
        """The prefix-scoped five-minute session the controller armed in THIS box's Modal Dict. A missing, expired, wrong-run or
        wrong-lease session fails closed after a bounded wait; there is no environment or application-signer fallback."""
        import re as _re
        deadline = time.time() + _QA_EXACT_SESSION_WAIT_S
        while True:
            row = _qa_onmiss_thread(_qa_onmiss_store_get, "scoped-session/" + kind)
            if (isinstance(row, dict) and row.get("kind") == kind and row.get("bucket") == _QA_SCOPED_BUCKET
                    and row.get("manifest_digest") == _QA_EXACT["digest"] and row.get("lease") == _QA_ONMISS_LEASE
                    and _re.fullmatch(r"[0-9a-f]{32}", str(row.get("account", "")))
                    and all(isinstance(row.get(f), str) and row.get(f) for f in ("access", "secret", "token"))
                    and isinstance(row.get("exp"), int) and row["exp"] > time.time() + 15):
                return row
            if time.time() >= deadline:
                raise RuntimeError("QA_SCOPED_SESSION_UNAVAILABLE kind=" + kind + "; no fallback to application credentials")
            time.sleep(2)

    def _qa_scoped_cached(kind):
        row = _QA_EXACT_CACHE.get(kind)
        if row is not None and row["exp"] > time.time() + 45:
            return row
        row = _qa_scoped_session(kind)
        _QA_EXACT_CACHE[kind] = row
        return row

    def _qa_scoped_boto_client(kind, config=None):
        """One S3 client whose credentials are RE-READ from the armed session whenever botocore needs them, so a refreshed session
        is picked up without rebuilding the client and an expired one makes the request fail instead of falling back."""
        import datetime as _dt
        import boto3 as _b3
        import botocore.session as _bs
        from botocore.credentials import RefreshableCredentials
        def fetch():
            row = _qa_scoped_cached(kind)
            return {"access_key": row["access"], "secret_key": row["secret"], "token": row["token"],
                    "expiry_time": _dt.datetime.fromtimestamp(row["exp"], _dt.timezone.utc).isoformat()}
        session = _bs.get_session()
        session._credentials = RefreshableCredentials.create_from_metadata(
            metadata=fetch(), refresh_using=fetch, method="qa-scoped-session")
        return _b3.Session(botocore_session=session).client(
            "s3", endpoint_url="https://" + _qa_scoped_cached(kind)["account"] + ".r2.cloudflarestorage.com",
            region_name="auto", config=config)

    _qa_boto3_client_orig = _qa_boto3.client

    def _qa_exact_boto3_client(service_name, *args, **kwargs):
        # Only clients built INSIDE R2FixtureStore._ensure_client (flagged below) are scoped; the app-data runtimes keep their binding.
        kind = getattr(_QA_EXACT_CTX, "kind", None)
        if kind and service_name == "s3":
            return _qa_scoped_boto_client(kind, kwargs.get("config"))
        return _qa_boto3_client_orig(service_name, *args, **kwargs)

    _qa_boto3.client = _qa_exact_boto3_client
    if "R2FixtureStore" not in globals() or not hasattr(R2FixtureStore, "_ensure_client"):
        raise RuntimeError("QA_EXACT_FIXTURE_CLIENT_UNPATCHABLE: refusing to run exact mode with an unscoped fixture client")
    _qa_exact_orig_ensure = R2FixtureStore._ensure_client

    def _qa_exact_ensure(self, *, for_write=False):
        _QA_EXACT_CTX.kind = "write" if for_write else "read"
        try:
            return _qa_exact_orig_ensure(self, for_write=for_write)
        finally:
            _QA_EXACT_CTX.kind = None

    R2FixtureStore._ensure_client = _qa_exact_ensure

    def _r2_bucket():
        return _QA_SCOPED_BUCKET

    def _qa_r2_client():
        return _qa_scoped_boto_client("write", None)

    def _qa_exact_confirm_miss(key):
        """AUTHORITATIVE replay miss with the scoped read session: True only for a real NoSuchKey/404. An existing object, a 403,
        a timeout or any other lookup failure is NEVER a miss (the stock proxy treats a failed replay read as one) and so never
        reaches pricing, a reservation or the provider."""
        client = _qa_scoped_boto_client("read", None)
        try:
            client.get_object(Bucket=_QA_SCOPED_BUCKET, Key=key)
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
                return True
            raise
        return False

    def _qa_exact_note_dispatch(ticket, upstream_resp, raw):
        """Join evidence only, never payload text: upstream status, provider response id(s) and safe request-id headers.
        Recorded on the durable claim BEFORE settlement, so a settle failure still leaves the dispatch identity."""
        import re as _re
        ids = []
        for found in _re.finditer(rb'"responseId"\s*:\s*"([A-Za-z0-9_-]{6,80})"', raw or b""):
            value = found.group(1).decode()
            if value not in ids:
                ids.append(value)
            if len(ids) >= 8:
                break
        headers = {}
        for name in ("x-request-id", "request-id", "x-goog-request-id", "x-vertex-ai-request-id"):
            value = upstream_resp.headers.get(name) if upstream_resp is not None else None
            if value:
                headers[name] = str(value)[:120]
        def note(state):
            row = (state.get("attempts") or {}).get(ticket)
            if row is not None:
                row["dispatch"] = {"upstream_status": int(upstream_resp.status_code), "provider_response_ids": ids,
                                   "upstream_request_headers": headers, "at": int(time.time())}
        try:
            _qa_update(note, keys=[_QA_ONMISS_ATTEMPT_KEY])
        except Exception:
            print("QA_ONMISS_JOIN_NOTE_FAILED ticket=" + str(ticket)[:8])
'''


QA_PROXY_LEDGER_SOURCE = r'''
# Disposable QA ledger: one atomic R2 object shared by EVERY container/app in this run.
_QA_RUN = __RUN__
_QA_ALLOW_BILLABLE_TOOLS = __ALLOW_BILLABLE_TOOLS__
_QA_TOOL_SPEND_CAP_USD = __TOOL_SPEND_CAP_USD__
# Admission budget, NOT a provider hard cap: final search usage may overshoot.
_QA_SEARCH_PRICE_USD = 0.014
_QA_ESTIMATED_SEARCHES = 10
_QA_LEDGER = "qa-capture-ledgers/" + _QA_RUN + ".json"
_QA_LEDGER_KEYS = [_QA_LEDGER]

def _qa_update(mutator, keys=None):
    # capture-on-miss settles the box-lifetime AND the attempt ledger; the explicit capture lane keeps one key.
    results = [_qa_update_one(key, mutator) for key in (keys or _QA_LEDGER_KEYS)]
    return False if any(r is False for r in results) else results[0]

def _qa_r2_client():
    # Seam: stock = the application R2 env signer; exact mode (QA_ONMISS_SOURCE) replaces it with the armed scoped session.
    import boto3
    return boto3.client('s3', endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
                          aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
                          aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'], region_name='auto')

def _qa_ledger_read(key):
    client = _qa_r2_client()
    try:
        return json.loads(client.get_object(Bucket=_r2_bucket(), Key=key)["Body"].read())
    except Exception as error:
        if getattr(error, "response", {}).get("Error", {}).get("Code") not in ("NoSuchKey", "404", "NotFound"):
            raise
        return {}

def _qa_update_one(ledger_key, mutator):
    # Dedicated client: conditional headers cannot leak into concurrent fixture PUTs.
    # boto3 1.35.99 lacks modeled IfMatch; inject the real R2 conditional header.
    client = _qa_r2_client()
    condition = {}
    def conditional(request, **kwargs):
        for key, value in condition.items(): request.headers[key] = value
    client.meta.events.register_first('before-sign.s3.PutObject', conditional)
    for attempt in range(30):
        try:
            obj = client.get_object(Bucket=_r2_bucket(), Key=ledger_key)
            state = json.loads(obj["Body"].read())
            condition = {"If-Match": obj["ETag"]}
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") not in ("NoSuchKey", "404", "NotFound"):
                raise
            state = {"run_id": _QA_RUN, "cap_usd": 3, "attempts": {}, "events": [], "halted": False,
                     "allow_billable_tools": _QA_ALLOW_BILLABLE_TOOLS,
                     "tool_admission_budget_usd": _QA_TOOL_SPEND_CAP_USD,
                     "tool_cap_policy": "soft cap: final call may overshoot by its actual search usage"}
            condition = {"If-None-Match": "*"}
        result = mutator(state)
        try:
            client.put_object(Bucket=_r2_bucket(), Key=ledger_key,
                              Body=json.dumps(state).encode(), ContentType="application/json")
            return result
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") not in ("PreconditionFailed", "412", "ConditionalRequestConflict", "409"):
                raise
    raise RuntimeError("QA ledger contention; no upstream permission")

def _qa_price(provider, model, body):
    """Priced maximum for one request (input+output+provider charges). Raises for unpriced/unbounded shapes."""
    data = json.loads(body)
    billable_tools = False
    search_bound = 0.0
    # Native audit tariffs from seedProviderPricing.ts. Unknown lanes fail closed.
    rates = {("anthropic", "claude-sonnet-4-5"): (3, 15),
             ("anthropic", "claude-opus-4-6"): (5, 25),
             ("openai", "gpt-5.2"): (1.75, 14),
             ("openrouter", "deepseek/deepseek-v4-pro"): (1.32, 3.96),
             # SC native fallbacks; OpenAI uses the higher long-context tariff.
             ("openai", "gpt-6.1-sol"): (4, 15),
             ("anthropic", "claude-sonnet-5-5"): (2, 10)}
    sc_native = (provider, model) in (("openai", "gpt-6.1-sol"), ("anthropic", "claude-sonnet-5-5"))
    if provider in ("gemini_api_key", "gemini_vertex") and model in ("gemini-3.1-flash-lite", "gemini-3.1-flash-lite-preview", "gemini-3.8-flash"):
        config = data.get("generationConfig", {})
        if config.get("candidateCount", 1) != 1:
            raise RuntimeError("QA capture multiple candidates are not allowed")
        if data.get("tools"):
            # Function declarations are editor-chat output schemas, not executed
            # provider tools. Product dispatch separately meters their execution.
            tools = data["tools"]
            declarations = tools[0].get("functionDeclarations") if (
                isinstance(tools, list) and len(tools) == 1 and isinstance(tools[0], dict)
                and set(tools[0]) == {"functionDeclarations"}) else None
            if declarations is not None:
                known = {"proofread", "findAndReplace", "formatTranscript", "editSelection",
                         "searchAndDefine", "auditDocument", "editTemplateRules", "docsQA",
                         "setAlignment", "submit_feedback"}
                if (not isinstance(declarations, list) or len(declarations) > 48
                        or len(json.dumps(tools).encode()) > 65536):
                    raise RuntimeError("QA_CHAT_TOOL_SCHEMA_UNSUPPORTED")
                seen = set()
                for definition in declarations:
                    if (not isinstance(definition, dict)
                            or set(definition) - {"name", "description", "parameters"}
                            or not __import__("re").fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", str(definition.get("name", "")))
                            or definition["name"] in seen
                            or not isinstance(definition.get("description", ""), str)
                            or not isinstance(definition.get("parameters"), dict)):
                        raise RuntimeError("QA_CHAT_TOOL_SCHEMA_UNSUPPORTED")
                    seen.add(definition["name"])
            elif tools == [{"googleSearch": {}}]:
                if not _QA_ALLOW_BILLABLE_TOOLS:
                    raise RuntimeError("QA_BILLABLE_TOOLS_DISABLED: explicit synthetic capture opt-in required")
                billable_tools = True
                search_bound = _QA_ESTIMATED_SEARCHES * _QA_SEARCH_PRICE_USD
            else:
                raise RuntimeError("QA_BILLABLE_TOOL_UNPRICED: unsupported tool shape")
        maximum = config.get("maxOutputTokens", 65536)
        if type(maximum) is not int or not 0 < maximum <= 65536:
            raise RuntimeError("QA capture output limit is not bounded")
        # Entire multimodal window at the higher audio input tariff; includes thoughts.
        input_rate, output_rate = (.75, 3.75) if model == "gemini-3.8-flash" else (.50, 1.50)
        bound = (1048576 * input_rate + maximum * output_rate) / 1000000
    elif (provider, model) in rates:
        # Text-only audit envelope. UTF-8 bytes bound text tokens, with a generous
        # 8192-token framing cushion; <=100KB keeps these calls in short-context pricing.
        allowed = {"model", "messages", "system", "temperature", "max_tokens",
                   "max_completion_tokens", "stream", "stream_options"}
        if sc_native:
            allowed |= {"seed"}
        if set(data) - allowed or len(body) > 100000 or data.get("stream") is not True:
            raise RuntimeError("QA audit capture requires a bounded text-only request")
        def text_only(content):
            if isinstance(content, str): return True
            return (isinstance(content, list) and all(
                isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
                and not (set(b) - {"type", "text", "cache_control"})
                and ("cache_control" not in b or b["cache_control"] == {"type": "ephemeral"})
                for b in content))
        messages = data.get("messages")
        if (not isinstance(messages, list) or not 1 <= len(messages) <= 8
            or not text_only(data.get("system", "")) or any(
                not isinstance(m, dict) or set(m) != {"role", "content"}
                or m["role"] not in ("system", "developer", "user", "assistant")
                or not text_only(m["content"]) for m in messages)):
            raise RuntimeError("QA audit capture rejects tools and non-text content")
        maximum = data.get("max_completion_tokens", data.get("max_tokens"))
        output_limit = (128000 if provider == "openai" else 64000) if sc_native else 8192
        if type(maximum) is not int or not 0 < maximum <= output_limit:
            raise RuntimeError("QA text output exceeds its priced model bound")
        input_rate, output_rate = rates[(provider, model)]
        # 2x input also exceeds the allowed 5-minute cache-write surcharge (1.25x).
        bound = ((len(body) + 8192) * input_rate * 2 + maximum * output_rate) / 1000000
    else:
        raise RuntimeError("QA_CAPTURE_UNPRICED_MODEL: live capture model is not priced")
    bound += search_bound
    return {"bound": bound, "billable_tools": billable_tools, "search_bound": search_bound,
            "input_rate": input_rate, "output_rate": output_rate}

def _qa_reserve(provider, model, request_hash, body):
    priced = _qa_price(provider, model, body)
    bound, billable_tools, search_bound = priced["bound"], priced["billable_tools"], priced["search_bound"]
    input_rate, output_rate = priced["input_rate"], priced["output_rate"]
    ticket = uuid.uuid4().hex
    def reserve(state):
        if (state.get("allow_billable_tools", False) != _QA_ALLOW_BILLABLE_TOOLS
                or state.get("tool_admission_budget_usd", 2.0) != _QA_TOOL_SPEND_CAP_USD):
            raise RuntimeError("QA tool policy differs from the run manifest")
        if billable_tools:
            tool_committed = sum(a["charged_bound_usd"] for a in state["attempts"].values()
                                 if a.get("billable_tools"))
            if tool_committed + bound > _QA_TOOL_SPEND_CAP_USD:
                state["tool_budget_refusal"] = "QA_TOOL_ADMISSION_BUDGET: next token + search reservation exceeds budget"
                return state["tool_budget_refusal"]
        committed = sum(a["charged_bound_usd"] for a in state["attempts"].values())
        if state["halted"] or committed + bound > 3:
            state["halted"] = True
            state["halt_reason"] = "QA_CAPTURE_SPEND_CAP: next request would exceed $3"
            return state["halt_reason"]
        state["attempts"][ticket] = {"hash": request_hash, "provider": provider, "model": model,
            "reserved_usd": bound, "charged_bound_usd": bound, "settled": False,
            "billable_tools": billable_tools, "reserved_search_cost_usd": search_bound,
            "per_search_usd": _QA_SEARCH_PRICE_USD if billable_tools else 0,
            "input_rate_per_million": input_rate, "output_rate_per_million": output_rate}
        return None
    refusal = _qa_update(reserve)
    if refusal:
        raise RuntimeError(refusal + "; no provider call")
    return ticket

def _qa_settle(ticket, raw):
    # SSE usage is cumulative; take the last/highest usage, NEVER sum frames.
    payloads = []
    text = raw.decode("utf-8", errors="replace")
    try:
        value = json.loads(text)
        payloads.extend(value if isinstance(value, list) else [value])
    except ValueError:
        for line in text.splitlines():
            if line.startswith("data:"):
                try: payloads.append(json.loads(line[5:].strip()))
                except ValueError: pass
    def settle(state):
        row = state["attempts"][ticket]
        # Never release a reservation on partial/error SSE merely because initial
        # usage exists. Completed HTTP buffering does not prove model completion.
        error_frame = any(isinstance(p, dict) and (p.get("error") or p.get("type") == "error") for p in payloads)
        if row["provider"] == "anthropic":
            complete = (any(isinstance(p, dict) and p.get("type") == "message_stop" for p in payloads)
                and any(isinstance(p, dict) and p.get("type") == "message_delta"
                        and "output_tokens" in (p.get("usage") or {}) for p in payloads))
        elif row["provider"] in ("openai", "openrouter"):
            complete = "data: [DONE]" in text and any(isinstance(p, dict) and isinstance(p.get("usage"), dict)
                and "completion_tokens" in p["usage"] for p in payloads)
        else:
            complete = any(isinstance(p, dict) and any(isinstance(c, dict) and c.get("finishReason")
                for c in p.get("candidates", [])) for p in payloads)
        if error_frame or not complete:
            row["usage_missing"] = True
            row["terminal_usage_unproven"] = True
            return
        if row["provider"] in ("gemini_api_key", "gemini_vertex"):
            usages = [p["usageMetadata"] for p in payloads if isinstance(p, dict) and isinstance(p.get("usageMetadata"), dict)]
            if not usages:
                row["usage_missing"] = True
                return
            prompt = max(int(u.get("promptTokenCount", 0)) for u in usages)
            total = max(int(u.get("totalTokenCount", 0)) for u in usages)
            output = max(max(int(u.get("candidatesTokenCount", 0)) + int(u.get("thoughtsTokenCount", 0)) for u in usages), total-prompt)
            weighted_input = prompt
        else:
            usages = []
            for p in payloads:
                if not isinstance(p, dict): continue
                usage = p.get("usage") or (p.get("message") or {}).get("usage")
                if isinstance(usage, dict): usages.append(usage)
            if not usages:
                row["usage_missing"] = True
                return
            high = lambda key: max(int(u.get(key, 0) or 0) for u in usages)
            if row["provider"] == "anthropic":
                uncached, cached, written = high("input_tokens"), high("cache_read_input_tokens"), high("cache_creation_input_tokens")
                prompt = uncached + cached + written
                output = high("output_tokens")
                weighted_input = uncached + cached * .1 + written * 1.25
                row.update(cache_read_input_tokens=cached, cache_creation_input_tokens=written)
            else:
                prompt, output = high("prompt_tokens"), high("completion_tokens")
                # Conservatively charge cached tokens at full input rate.
                weighted_input = prompt
            total = prompt + output
        if prompt < 0 or output < 0 or not total:
            row["usage_missing"] = True
            return
        estimate = (weighted_input * row["input_rate_per_million"] + output * row["output_rate_per_million"]) / 1000000
        token_estimate = estimate
        if row.get("billable_tools"):
            # Same floor-one/distinct nonempty-query policy as chatActions.ts.
            # Union across cumulative SSE frames; never sum repeated metadata.
            queries = set()
            for payload in payloads:
                if not isinstance(payload, dict): continue
                for candidate in payload.get("candidates", []):
                    if not isinstance(candidate, dict): continue
                    metadata = candidate.get("groundingMetadata") or {}
                    values = metadata.get("webSearchQueries", []) if isinstance(metadata, dict) else []
                    if isinstance(values, list):
                        queries.update(q for q in values if isinstance(q, str) and q.strip())
            searches = max(1, len(queries))
            search_cost = searches * row["per_search_usd"]
            estimate += search_cost
            row.update(policy_search_count=searches, search_cost_usd=search_cost)
        if token_estimate > row["reserved_usd"] - row.get("reserved_search_cost_usd", 0):
            state["halted"] = True
            state["halt_reason"] = "QA usage exceeded model bound"
            row.update(estimated_cost_usd=estimate, charged_bound_usd=estimate,
                       usage_exceeded_bound=True, settled=False)
            return False
        row.update(prompt_tokens=prompt, output_tokens=output, estimated_cost_usd=estimate,
                   charged_bound_usd=estimate, settled=True)
        if row.get("billable_tools"):
            # Accepted final-call overshoot is truthful spend, not a lost answer.
            row["usage_exceeded_bound"] = estimate > row["reserved_usd"]
            tool_spend = sum(a["charged_bound_usd"] for a in state["attempts"].values()
                             if a.get("billable_tools"))
            state["tool_budget_exhausted"] = tool_spend >= _QA_TOOL_SPEND_CAP_USD
        return True
    if _qa_update(settle) is False:
        raise RuntimeError("QA usage exceeded model bound; halt persisted")

_qa_original_log_action = _log_action
def _log_action(action, **fields):
    if action in ("llm_proxy_hit_r2", "llm_proxy_hit_lru", "llm_proxy_capture_canonical_hit",
                  "llm_proxy_hit_stage", "llm_proxy_miss_strict", "llm_proxy_r2_stage",
                  "llm_proxy_miss_forward", "llm_proxy_upstream_error", "llm_proxy_upstream_read_error"):
        event = {k: fields[k] for k in ("key", "canonical_key", "hash", "model", "upstream_status") if k in fields}
        event["action"] = action
        _qa_update(lambda state: state["events"].append(event), keys=[_QA_LEDGER])
    return _qa_original_log_action(action, **fields)
'''


def ensure_modal_client(root):
    """The Hetzner runner snapshot does not ship the Modal client (2026-10-19 #10367: `No module named modal`).
    Install the repo's pinned base requirements on the disposable box before any `python -m modal` call."""
    if os.environ.get('REMOTE_BOX') != '1':
        return
    probe = subprocess.run([sys.executable, '-c', 'import modal'], cwd='/', capture_output=True, text=True, timeout=60)
    if probe.returncode == 0:
        return
    requirements = root/'modal/requirements.txt'
    if not requirements.is_file():
        raise RuntimeError('Modal client missing and modal/requirements.txt not found')
    result = subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', '--break-system-packages',
                             '--ignore-installed', 'typing_extensions', '-r', str(requirements)],
                            cwd='/', capture_output=True, text=True, timeout=900)
    if result.returncode:
        raise RuntimeError('Modal client install failed rc=' + str(result.returncode) + ' :: '
                           + sanitize((result.stderr or result.stdout or '')[-600:]))
    print('QA_MODAL_CLIENT=installed')


def convex_provider(root):
    """Read the selected dev deployment's existing route; never mutate the caller."""
    def read(*args):
        command = ['bash', '-c',
                   'set -ae; source "$1/.env.local"; '
                   '[[ "$CONVEX_DEPLOYMENT" = dev:* && "${CONVEX_ADMIN_KEY%%|*}" = "$CONVEX_DEPLOYMENT" ]] || exit 4; '
                   'export CONVEX_DEPLOY_KEY="$CONVEX_ADMIN_KEY"; shift; exec npx convex env "$@"',
                   'qa-provider', str(root), *args]
        result = subprocess.run(command, cwd=root, text=True, capture_output=True, timeout=90)
        if result.returncode:
            raise RuntimeError('QA provider preflight cannot read scoped dev Convex route (details withheld)')
        return result.stdout.strip()
    present = set(read('list', '--names-only').splitlines())
    explicit = read('get', 'GEMINI_PROVIDER').lower() if 'GEMINI_PROVIDER' in present else ''
    if explicit:
        if explicit not in ('vertex', 'ai_studio'):
            raise RuntimeError('QA provider preflight: Convex GEMINI_PROVIDER must be vertex or ai_studio')
        if explicit == 'ai_studio' and os.environ.get('QA_ALLOW_AI_STUDIO') != '1':
            # S90: AI Studio is not a prod route; capture fixtures are keyed gemini_vertex, so it 402s live.
            raise RuntimeError('QA provider preflight: slot Convex GEMINI_PROVIDER=ai_studio; set it to vertex '
                               '(npx convex env set GEMINI_PROVIDER vertex on the slot dev deployment)')
        return explicit
    # S90: default to vertex (fixtures are keyed gemini_vertex); AI Studio only via explicit opt-in.
    return 'ai_studio' if os.environ.get('QA_ALLOW_AI_STUDIO') == '1' else 'vertex'


def provider_ready(root):
    if not chat_tools_present(root):
        return
    expected = convex_provider(root)
    url = os.environ['MODAL_CHAT_TOOLS_ENDPOINT_URL'].rstrip('/') + '/qa-provider-readiness'
    req = urllib.request.Request(url, headers={'X-API-Key': os.environ['MODAL_API_KEY']})
    with urllib.request.urlopen(req, timeout=90) as response:
        data = json.load(response)
    resolved = data.get('provider')
    if resolved != expected:
        raise RuntimeError('QA provider mismatch: Convex expected=' + expected + ' Modal resolved=' + str(resolved)
                           + '; disposable chat-tools GEMINI_PROVIDER must match before Playwright')
    if data.get('proxy') != 'true' or data.get('strict') != os.environ.get('STRICT_FIXTURES'):
        raise RuntimeError('QA chat-tools fixture transport mismatch before Playwright')
    print('QA_PROVIDER_PREFLIGHT=passed convex_expected=' + expected + ' modal_resolved=' + resolved)


EXTRA_SECRET_ALLOWLIST = ('SUPPORTING_DOCUMENT_RECEIPT_SECRET',)  # hard-coded: add a name only with PM approval
EXTRA_SECRET_ENV = 'QA_EXTRA_SECRET_VALUE'  # child-process env var carrying the value to the Convex helper (never argv)
_PROD_CONVEX_PREFIXES = ('beloved-cardinal-', 'loyal-lynx-', 'prod-')


def parse_extra_secrets(specs, convex_helper=None, convex_root=None):
    """Pure fail-closed gate run before ANY write. Only NAME=generate for an allowlisted NAME on a box with a
    disposable-Convex ownership root. Reasons are fixed codes: a spec can carry a pasted secret, so it is never echoed."""
    names = []
    for spec in specs or ():
        name, sep, mode = str(spec).partition('=')
        if not sep or mode != 'generate':
            raise ValueError('EXTRA_SECRET_MODE_REFUSED (only NAME=generate is accepted)')
        if name not in EXTRA_SECRET_ALLOWLIST:
            raise ValueError('EXTRA_SECRET_NAME_NOT_ALLOWLISTED')
        if name in names:
            raise ValueError('EXTRA_SECRET_DUPLICATE')
        names.append(name)
    if names:
        if os.environ.get('REMOTE_BOX') != '1':
            raise ValueError('EXTRA_SECRET_BOX_ONLY (leased per-box lane required)')
        if not convex_helper or not convex_root:
            raise ValueError('EXTRA_SECRET_DISPOSABLE_CONVEX_REQUIRED')
        # Early refusal of prod/shared/non-dev Convex, before anything is written; the helper re-checks (defence in depth).
        try:
            receipt = json.loads((Path(convex_root)/'ownership.json').read_text())
            deployment, kind = receipt['name'], receipt['deploymentType']
        except Exception:
            raise ValueError('EXTRA_SECRET_CONVEX_OWNERSHIP_UNREADABLE') from None
        if (not isinstance(deployment, str) or deployment.startswith(_PROD_CONVEX_PREFIXES) or kind != 'dev'
                or not re.fullmatch(r'dev/qa-fullflow-[a-f0-9]{32}', str(receipt.get('reference', '')))):
            raise ValueError('EXTRA_SECRET_CONVEX_NOT_DISPOSABLE_DEV')
    return names


def provision_extra_secrets(names, convex_helper, convex_root, run=subprocess.run):
    """Generate each value (32 random bytes, hex) on the box and set it on the owned disposable Convex deployment via
    the helper's ownership-verified set-env. Returns {name: value} for the leased per-box Modal secret ONLY.
    The value travels in a child env var, never argv; every printed line is names-only."""
    values = {}
    for name in names:
        value = secrets.token_hex(32)
        register_secret(value)
        child = os.environ.copy()
        child[EXTRA_SECRET_ENV] = value
        result = run([sys.executable, '-I', str(convex_helper), 'set-env', '--root', str(convex_root),
                      '--name', name, '--from-env', EXTRA_SECRET_ENV],
                     env=child, text=True, capture_output=True, timeout=180)
        if result.returncode:
            tail = sanitize(((result.stderr or '').strip() or (result.stdout or '').strip())[-400:])
            raise RuntimeError('EXTRA_SECRET_CONVEX_SET_FAILED name=' + name + ' rc=' + str(result.returncode) + ' :: ' + tail)
        print('QA_EXTRA_CONVEX_ENV names=' + name)
        values[name] = value
    return values


def prepare(box, root, config_path, capture=False, allow_billable_tools=False, tool_spend_cap_usd=2.0, onmiss=False,
            extra_secrets=(), convex_helper=None, convex_root=None, exact=None):
    extra_names = parse_extra_secrets(extra_secrets, convex_helper, convex_root)  # fail closed before any write
    if exact and not onmiss:
        raise ValueError('--exact-manifest requires --on-miss')
    exact_ids = onmiss_effective(root, box, onmiss, exact)  # an invalid exact manifest refuses here, before any write
    onmiss = bool(exact_ids)
    policy = tool_policy(allow_billable_tools, tool_spend_cap_usd)
    (config_path.parent/'billable-tools.json').write_text(json.dumps(policy, indent=2)+'\n')
    print('QA_BILLABLE_TOOL_POLICY=' + json.dumps(policy, sort_keys=True))
    # Match preflight BASE_URL and capture QA_URL; never inherit a slot/prod domain.
    env_snapshot = (root/'.env.local').read_bytes()   # the ONE input of the rewrite below (preimage)
    port_match = re.search(r'^PORT=(.*)$', env_snapshot.decode(), re.MULTILINE)
    port_parts = shlex.split(port_match[1], comments=True) if port_match else []
    port = port_parts[0] if len(port_parts) == 1 else ''
    if not re.fullmatch(r'[0-9]+', port) or not 1 <= int(port) <= 65535:
        raise ValueError('QA Next listener PORT must be explicitly configured (1-65535)')
    site_domain = 'localhost:' + port
    ensure_modal_client(root)
    box_id = identity(box)
    key = secrets.token_urlsafe(48)
    pipeline = secrets.token_urlsafe(48)
    suffix = '-qa-'+box_id
    # Non-routable bootstrap targets only satisfy deploy-time image requirements.
    # Never guess Modal's workspace/region/truncation/hash naming algorithm.
    updates = {'NEXT_PUBLIC_SITE_DOMAIN': site_domain,
               'QA_MODAL_BOX_ID': box_id, 'QA_MODAL_BOX': box, 'MODAL_ENV_SUFFIX': suffix,
               'MODAL_API_KEY_SECRET_NAME': 'heydonna-qa-'+box_id,
               'MODAL_AUDIO_ENDPOINT_URL': 'https://'+names(box)[0]+'.invalid',
               'MODAL_DOCX_ENDPOINT_URL': 'https://'+names(box)[1]+'.invalid',
               'MODAL_SPEAKER_NAMES_ENDPOINT_URL': 'https://'+names(box)[1]+'.invalid',
               'MODAL_REVIEW_ENDPOINT_URL': 'https://'+names(box)[2]+'.invalid/agent_review',
               'MODAL_API_KEY': key, 'PIPELINE_AUTH_TOKEN': pipeline,
               'MODAL_ENVIRONMENT': 'main', 'LLM_PROXY_ENABLED': 'true', 'STRICT_FIXTURES': 'true',
               'LLM_PROXY_FIXTURE_CAPTURE': 'true' if capture else 'false',
               'LLM_PROXY_CAPTURE_RUN_ID': 'remote-qa-'+box_id,
               'QA_LLM_ONMISS': '1' if onmiss else '0'}
    if onmiss:
        print('LLM_MODE=capture-on-miss strict_fixtures=true cap_usd=' + str(ONMISS_CAP_USD)
              + ' scope=box-lifetime+attempt payloads=box-local')
        if exact_ids and exact_ids.get('exact'):
            print('QA_ONMISS_EXACT_PREPARED digest=' + exact_ids['exact']['digest'] + ' approved=' + str(len(exact_ids['exact']['entries'])))
    if capture:
        updates['STRICT_FIXTURES'] = 'false'
        print('CAPTURE_MODE=on run_id=remote-qa-'+box_id+' cap_usd=3 synthetic_data_only=true')
    if (root/'modal/editor_chat/processor.py').is_file():
        updates['MODAL_EDITOR_CHAT_ENDPOINT_URL'] = 'https://heydonna-editor-chat-qa-'+box_id+'.invalid'
    if (root/AUDIT_ENTRY).is_file():
        updates['MODAL_AUDIT_ENDPOINT_URL'] = 'https://'+audit_name(box)+'.invalid'
    if (root/TERMS_ENTRY).is_file():
        updates['MODAL_TERMS_ENDPOINT_URL'] = 'https://'+terms_name(box)+'.invalid'
    if (root/SPEAKER_MAPPING_ENTRY).is_file():
        updates['MODAL_SPEAKER_MAPPING_URL'] = 'https://'+speaker_mapping_name(box)+'.invalid/map'
    chat_tools = chat_tools_present(root)
    if chat_tools:
        updates['MODAL_CHAT_TOOLS_ENDPOINT_URL'] = 'https://'+chat_tools_name(box)+'.invalid'
        # Like CI E2E, bake the caller's route into the image at deploy time.
        # Do not set GEMINI_PROVIDER on Convex: its existing route is authoritative.
        updates['GEMINI_PROVIDER'] = convex_provider(root)
        print('QA_CHAT_TOOLS_DEPLOY_PROVIDER=' + updates['GEMINI_PROVIDER'])
    # Regression guard: unknown empty definitions are inferred as required by Varlock.
    empty_updates = [name for name, value in updates.items() if value == '']
    if empty_updates:
        raise ValueError('QA-generated env definitions must be nonempty: ' + ','.join(empty_updates))
    config = json.loads(config_path.read_text())
    if not isinstance(config, dict) or any(k.startswith(('MODAL_', 'LLM_PROXY_')) or k in ('NEXT_PUBLIC_SITE_DOMAIN','PIPELINE_AUTH_TOKEN','STRICT_FIXTURES','LLM_PROXY_ENABLED','LLM_PROXY_FIXTURE_CAPTURE') for k in config):
        raise ValueError('Feature config cannot override QA routing, credentials or strict fixtures')
    for k in ('NEXT_PUBLIC_SITE_DOMAIN','MODAL_AUDIO_ENDPOINT_URL','MODAL_DOCX_ENDPOINT_URL','MODAL_SPEAKER_NAMES_ENDPOINT_URL','MODAL_REVIEW_ENDPOINT_URL','MODAL_CHAT_TOOLS_ENDPOINT_URL','MODAL_EDITOR_CHAT_ENDPOINT_URL','MODAL_AUDIT_ENDPOINT_URL','MODAL_TERMS_ENDPOINT_URL','MODAL_SPEAKER_MAPPING_URL','MODAL_API_KEY','PIPELINE_AUTH_TOKEN','LLM_PROXY_ENABLED','STRICT_FIXTURES','LLM_PROXY_FIXTURE_CAPTURE'):
        if k in updates:
            config[k] = updates[k]
    config_path.write_text(json.dumps(config))
    os.chmod(config_path, 0o600)
    p = root/'.env.local'
    lines = []
    for line in env_snapshot.decode().splitlines():
        k = line.removeprefix('export ').partition('=')[0].strip()
        # Omit bypass controls entirely: unknown empty dotenv fields fail Varlock validation.
        if k not in updates and k not in ('MODAL_PROFILE','MODAL_TOKEN_ID','MODAL_TOKEN_SECRET','MODAL_SECRET','MODAL_KEY',
                                          'LLM_PROXY_CAPTURE_BYPASS_HASHES','LLM_PROXY_CAPTURE_BYPASS_PROFILE'):
            lines.append(line)
    lines += [k+'='+shlex.quote(v) for k,v in updates.items()]
    env_rewrite(p, env_snapshot, '\n'.join(lines)+'\n', 'prepare')
    # Convex first: a refusal/failure there aborts before the leased Modal secret exists (nothing to unwind).
    extra = provision_extra_secrets(extra_names, convex_helper, convex_root) if extra_names else {}
    secret_env = config_path.parent/'invocation.env'
    secret_env.write_text('MODAL_API_KEY='+key+'\nPIPELINE_AUTH_TOKEN='+pipeline+'\n'
                          + ''.join(n+'='+v+'\n' for n, v in extra.items()))
    os.chmod(secret_env, 0o600)
    env = modal_env(True)
    cli(['secret','create','--from-dotenv',str(secret_env),updates['MODAL_API_KEY_SECRET_NAME']], env)
    print('QA_MODAL_SECRET=created name='+updates['MODAL_API_KEY_SECRET_NAME'])
    if exact_ids and exact_ids.get('exact'):
        # Exact mode: the lease-only QA R2 secret every leased runtime mounts instead of the shared one (Run 9 binding), built from the
        # scoped .env.local values after they are proven to be the verified heydonna-qa identity; removed at teardown by stop().
        r2_values, r2_fp = app_data_values(env_snapshot.decode())
        r2_env = config_path.parent/'r2-lease.env'
        r2_env.write_text(''.join(k + '=' + v + '\n' for k, v in r2_values.items()))
        os.chmod(r2_env, 0o600)
        register_secret(r2_values['R2_SECRET_ACCESS_KEY'])
        try:
            cli(['secret','create','--from-dotenv',str(r2_env),r2_lease_secret_name(box)], env)
        finally:
            r2_env.unlink()
        print('QA_APP_DATA_SECRET=created name=' + r2_lease_secret_name(box) + ' bucket=' + SCOPED_FIXTURE_BUCKET + ' signer=' + r2_fp)
    if extra:
        print('QA_EXTRA_MODAL_SECRET names='+','.join(extra)+' secret='+updates['MODAL_API_KEY_SECRET_NAME'])


def deployed_urls(box, chat_tools=False, editor_chat=False, audit=False, terms=False, speaker_mapping=False):
    """Metadata lookup only; never invokes a function or reconstructs its label.

    REVIEW publishes the full request URL (the app's route fetches the env value as-is, and the
    review app serves POST /agent_review); CHAT_TOOLS publishes the bare base (callers append /chat-tool).
    The optional chat-tools app is looked up only when the checkout deploys it."""
    # Explicit automation environment is required even if a personal profile exists.
    env = modal_env(True)
    os.environ.pop('MODAL_PROFILE', None)
    os.environ.update(env)
    import modal
    urls = {}
    specs = [('AUDIO', names(box)[0], 'audio_fastapi_app', ''),
             ('DOCX', names(box)[1], 'fastapi_app', ''),
             ('SPEAKER_NAMES', names(box)[1], 'speaker_names_app', ''),
             ('REVIEW', names(box)[2], 'agent_review', '/agent_review')]
    if chat_tools:
        specs.append(('CHAT_TOOLS', chat_tools_name(box), 'endpoint', ''))
    if editor_chat:
        specs.append(('EDITOR_CHAT', 'heydonna-editor-chat-qa-'+identity(box), 'web', ''))
    if audit:
        specs.append(('AUDIT', audit_name(box), 'web', ''))
    if terms:
        specs.append(('TERMS', terms_name(box), 'terms_app', ''))
    if speaker_mapping:
        specs.append(('SPEAKER_MAPPING', speaker_mapping_name(box), 'speaker_mapping_app', '/map'))
    for label, app, function, route in specs:
        endpoint = modal.Function.from_name(app, function, environment_name='main')
        endpoint.hydrate()
        url = endpoint.get_web_url()
        if not isinstance(url, str):
            raise ValueError('Missing owned Modal web endpoint')
        parsed = urlsplit(url)
        host = parsed.hostname or ''
        # Accept SDK-assigned hashes/regions, but never another box/app, custom
        # domain, userinfo, port, path, query or fragment. The exact app/function
        # lookup above establishes ownership; the host check is defense in depth.
        hostname_pattern = r'[a-z0-9-]+--' + re.escape(app) + r'-[a-z0-9-]+(?:\.[a-z0-9-]+)*\.modal\.run'
        if (parsed.scheme != 'https' or not __import__("re").fullmatch(hostname_pattern, host)
                or parsed.netloc != host or parsed.path not in ('', '/')
                or parsed.query or parsed.fragment
                or any(len(part) > 63 for part in host.split('.'))):
            raise ValueError('Foreign or invalid Modal web endpoint metadata')
        # The speaker-mapping route reads MODAL_SPEAKER_MAPPING_URL (app/api/speaker-mapping/route.ts).
        key = 'MODAL_SPEAKER_MAPPING_URL' if label == 'SPEAKER_MAPPING' else 'MODAL_'+label+'_ENDPOINT_URL'
        urls[key] = url.rstrip('/') + route
    return urls


def resolve(box, root, config_path, verify=False):
    """Publish a complete owned pair atomically per file, or abort with cleanup."""
    box_id = identity(box)
    if (os.environ.get('REMOTE_BOX') != '1'
            or os.environ.get('QA_MODAL_BOX_ID') != box_id
            or os.environ.get('MODAL_ENV_SUFFIX') != '-qa-'+box_id):
        raise ValueError('QA endpoint resolution identity mismatch')
    try:
        urls = deployed_urls(box, chat_tools_present(root), (root/'modal/editor_chat/processor.py').is_file(),
                             (root/AUDIT_ENTRY).is_file(), (root/TERMS_ENTRY).is_file(),
                             (root/SPEAKER_MAPPING_ENTRY).is_file())
        receipt = config_path.parent/'endpoints.json'
        if verify:
            if json.loads(receipt.read_text()) != urls:
                raise ValueError('Owned endpoint metadata changed after cached redeploy')
            print('QA_MODAL_URLS=stable after_cached_redeploy=true')
            return
        config = json.loads(config_path.read_text())
        config.update(urls)
        p = root/'.env.local'
        env_snapshot = p.read_bytes()
        lines = [line for line in env_snapshot.decode().splitlines()
                 if line.removeprefix('export ').partition('=')[0].strip() not in urls]
        lines += [key+'='+shlex.quote(value) for key, value in urls.items()]
        files = {
            config_path: json.dumps(config),
            config_path.parent/'endpoints.env': ''.join('export '+key+'='+shlex.quote(value)+'\n' for key, value in urls.items()),
            receipt: json.dumps(urls),
        }
        staged = []
        for path, text in files.items():
            temporary = path.with_name(path.name+'.resolved-next')
            with temporary.open('w') as stream:
                os.chmod(temporary, 0o600)
                stream.write(text)
            staged.append((temporary, path))
        for temporary, path in staged:
            temporary.replace(path)
        env_rewrite(p, env_snapshot, '\n'.join(lines)+'\n', 'resolve')
        print('QA_MODAL_URLS=resolved source=deployed_function_metadata box='+box)
        for key, url in urls.items():
            print(key+'='+url)
    except Exception:
        # Outer controller restores tracked Convex state and retries this exact
        # owned cleanup too. A failed stop never marks the ownership lease done.
        try:
            stop(box, remote=True)
        except Exception:
            print('QA_MODAL_METADATA_CLEANUP=retry-pending', file=sys.stderr)
        raise



SCOPED_FIXTURE_BUCKET = 'heydonna-qa'
SCOPED_FIXTURE_R2_ENV = tuple('QA_FIXTURE_R2_' + name for name in (
    'BUCKET', 'ACCOUNT_ID', 'ACCESS_KEY_ID', 'SECRET_ACCESS_KEY', 'SESSION_TOKEN'))


def scoped_fixture_env(values):
    """No implicit SDK chain, parent env aliases, or bucket default in scoped mode."""
    selected = {name: values.get(name, '') for name in SCOPED_FIXTURE_R2_ENV}
    bucket, account, *_ = selected.values()
    if (bucket != SCOPED_FIXTURE_BUCKET or not re.fullmatch(r'[0-9a-f]{32}', account)
            or not all(value and value == value.strip() for value in selected.values())):
        raise RuntimeError('QA_SCOPED_R2_UNAVAILABLE: explicit QA bucket and all three session credential fields required')
    return selected


def scoped_fixture_client(values):
    import boto3
    from botocore.config import Config
    bucket, account, access, secret, token = scoped_fixture_env(values).values()
    return boto3.client('s3', endpoint_url=f'https://{account}.r2.cloudflarestorage.com', region_name='auto',
                        aws_access_key_id=access, aws_secret_access_key=secret, aws_session_token=token,
                        config=Config(retries={'total_max_attempts': 1})), bucket


def scoped_canary_keys(nonce):
    if not re.fullmatch(r'[0-9a-f]{32}', nonce):
        raise ValueError('Invalid QA canary identity')
    run = 'm15-scope-canary-' + nonce
    canonical = 'fixtures/llm/v2/qa_canary/' + nonce + '/' + hashlib.sha256(run.encode()).hexdigest() + '.bin'
    return run, canonical, 'staging/llm-capture/' + run + '/probe.bin', 'qa-capture-ledgers/onmiss/' + run + '.json'


def qa_scope_canary(s3, bucket, publisher, nonce):
    run, canonical, staged, ledger = scoped_canary_keys(nonce)
    body = b'{"synthetic":"QA fixture scope canary"}'
    ctype = 'application/json'
    metadata = {'canonical-key': canonical, 'response-status': '200', 'fixture-version': '2'}
    absent = publisher['_put_if_absent']
    owned = {}

    def take(key, expected):
        row = s3.get_object(Bucket=bucket, Key=key)
        raw = row['Body'].read()
        if raw != expected or not row.get('ETag'):
            raise RuntimeError('QA_SCOPED_CANARY_READBACK_MISMATCH')
        return {'sha256': hashlib.sha256(raw).hexdigest(), 'etag': row['ETag']}

    try:
        for key, payload, meta in ((staged, body, metadata), (ledger, b'{"synthetic":true}', {})):
            if not absent(s3, bucket, key, payload, ctype, meta):
                raise RuntimeError('QA_SCOPED_CANARY_INPUT_NOT_UNIQUE')
            owned[key] = take(key, payload)['etag']
        plan, skipped = publisher['collect_plan'](s3, bucket, run)
        if plan != [(staged, canonical)] or skipped:
            raise RuntimeError('QA_SCOPED_CANARY_STAGE_LIST_MISMATCH')
        first = publisher['publish_plan'](s3, bucket, plan, replace_allowlist=frozenset())
        if first != (1, 0, 0):
            raise RuntimeError('QA_SCOPED_CANARY_FIRST_PUT_UNPROVEN')
        before = take(canonical, body)
        owned[canonical] = before['etag']
        listed = s3.list_objects_v2(Bucket=bucket, Prefix=canonical.rsplit('/', 1)[0] + '/')
        if [obj.get('Key') for obj in listed.get('Contents', [])] != [canonical] or listed.get('IsTruncated'):
            raise RuntimeError('QA_SCOPED_CANARY_CANONICAL_LIST_MISMATCH')
        if absent(s3, bucket, canonical, b'{"synthetic":"must not replace"}', ctype, metadata):
            raise RuntimeError('QA_SCOPED_CANARY_SECOND_PUT_REPLACED')
        after = take(canonical, body)
        if before != after:
            raise RuntimeError('QA_SCOPED_CANARY_SECOND_PUT_CHANGED')
        try:
            s3.get_object(Bucket=bucket, Key='qa-scope-denial/' + nonce + '.bin')
        except Exception as error:
            response = getattr(error, 'response', {})
            if response.get('Error', {}).get('Code') != 'AccessDenied' or response.get('ResponseMetadata', {}).get('HTTPStatusCode') != 403:
                raise RuntimeError('QA_SCOPED_CANARY_DENIAL_UNPROVEN') from None
        else:
            raise RuntimeError('QA_SCOPED_CANARY_OUTSIDE_SCOPE_READ_SUCCEEDED')
    finally:
        for key, etag in owned.items():
            s3.delete_object(Bucket=bucket, Key=key, IfMatch=etag)
            try:
                s3.get_object(Bucket=bucket, Key=key)
            except Exception as error:
                if getattr(error, 'response', {}).get('Error', {}).get('Code') in ('NoSuchKey', '404', 'NotFound'):
                    continue
                raise
            raise RuntimeError('QA_SCOPED_CANARY_TEARDOWN_UNVERIFIED')
    return {'bucket': bucket, 'run': run, 'list_and_read': True, 'first_conditional_put': 'created',
            'second_conditional_put': 'precondition_failed', 'before': before, 'after': after,
            'out_of_scope_get': 'HTTP 403 AccessDenied', 'canary_objects_removed': len(owned)}


SCOPED_SESSION_ACTIONS_CANARY = ['ListObjectsV2', 'GetObject', 'PutObject', 'DeleteObject']


def _mint_session(paths, actions, ttl_s):
    """Trusted host only; a prefix-scoped session derived from the verified Run 9 parent, with no standing token creation.
    Returns (QA_FIXTURE_R2_* values, expiry epoch)."""
    import base64, hmac, runpy
    if os.environ.get('REMOTE_BOX') == '1':
        raise RuntimeError('QA_SCOPED_R2_REFUSED: mint only on trusted host')
    parent = runpy.run_path(str(Path(__file__).with_name('remote-qa-r2.py')))['qa_values']()
    account, access, secret = (parent['R2_' + k] for k in ('ACCOUNT_ID', 'ACCESS_KEY_ID', 'SECRET_ACCESS_KEY'))
    if hashlib.sha256(access.encode()).hexdigest()[:16] != RUN9_SIGNER_FP:
        raise RuntimeError('QA_SCOPED_R2_REFUSED: signer does not match the verified Run 9 parent')
    register_secret(secret)
    now = int(time.time())
    claims = {'iss': access, 'sub': account, 'aud': account + '.r2.cloudflarestorage.com',
              'iat': now, 'exp': now + ttl_s, 'bucket': SCOPED_FIXTURE_BUCKET,
              'actions': list(actions), 'paths': {'prefixPaths': list(paths)}}
    b64 = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b'=')
    unsigned = b64(b'{"alg":"HS256","typ":"JWT"}') + b'.' + b64(json.dumps(claims, separators=(',', ':')).encode())
    jwt = unsigned + b'.' + b64(hmac.digest(secret.encode(), unsigned, 'sha256'))
    values = (SCOPED_FIXTURE_BUCKET, account, access, hashlib.sha256(jwt).hexdigest(),
              base64.b64encode(b'jwt/' + jwt).decode())
    for value in values[2:]:
        register_secret(value)
    return dict(zip(SCOPED_FIXTURE_R2_ENV, values)), now + ttl_s


def mint_scoped_canary_session(nonce):
    """Scoped to the three synthetic canary paths (unchanged behaviour: 900 s)."""
    run, canonical, _, ledger = scoped_canary_keys(nonce)
    paths = [canonical.rsplit('/', 1)[0] + '/', 'staging/llm-capture/' + run + '/', ledger]
    return _mint_session(paths, SCOPED_SESSION_ACTIONS_CANARY, 900)[0]


def mint_exact_sessions(kind, ids, manifest):
    """Prefix-scoped five-minute sessions for ONE exact run. proxy = fixture reads (read-only over the fixture store and this run's
    staging prefix) plus staging/ledger writes (this run's staging prefix and its two ledger objects only); report = the staging
    prefix, the approved canonical keys and the two ledger objects. Nothing else in the bucket is reachable."""
    stage = 'staging/llm-capture/' + ids['stage_run'] + '/'
    ledgers = [ids['box_key'], ids['attempt_key']]
    if kind == 'proxy':
        specs = {'read': (['fixtures/llm/v2/', stage], ['GetObject']),
                 'write': ([stage] + ledgers + [ids['run_key']], ['GetObject', 'PutObject'])}
    elif kind == 'report':
        specs = {'report': ([stage] + sorted(manifest['entries']) + ledgers, ['ListObjectsV2', 'GetObject', 'PutObject'])}
    elif kind == 'custody':  # trusted host only: bind legacy + QA custody into the two ledger objects (nothing else)
        specs = {'custody': (ledgers, ['GetObject', 'PutObject'])}
    else:
        raise ValueError('unknown session kind')
    rows = {}
    for name, (paths, actions) in specs.items():
        env, exp = _mint_session(paths, actions, EXACT_SESSION_TTL_S)
        bucket, account, access, secret, token = (env[n] for n in SCOPED_FIXTURE_R2_ENV)
        rows[name] = {'kind': name, 'bucket': bucket, 'account': account, 'access': access, 'secret': secret, 'token': token,
                      'exp': exp, 'manifest_digest': manifest['digest'], 'lease': ids['lease']}
    return rows


LEGACY_BUCKET = 'heydonna-fixtures'
LEGACY_ENV_DEFAULT = '/Users/rajiv/Downloads/projects/heydonna-app/.env.local'


def _env_names(path, names):
    """Parse ONLY the named entries of an env file inside this process; values are never printed, logged or put in a message."""
    found = {}
    for line in Path(path).read_text().splitlines():
        key, sep, value = line.removeprefix('export ').partition('=')
        if sep and key.strip() in names:
            parts = shlex.split(value, comments=True)
            if len(parts) == 1:
                found[key.strip()] = parts[0]
    return found


def _is_missing(error):
    return getattr(error, 'response', {}).get('Error', {}).get('Code') in ('NoSuchKey', '404', 'NotFound')


def legacy_reader(env_path):
    """Trusted host only: exact-key GetObject on the legacy ledger bucket with the app's read key. Exposes ONLY get(); the key is
    never mounted into a box and nothing is written. Returns (raw, etag), None for an authoritative absence; anything else raises."""
    import boto3
    from botocore.config import Config
    values = _env_names(env_path, ('R2_ACCOUNT_ID', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY'))
    if len(values) != 3 or not re.fullmatch(r'[0-9a-f]{32}', values['R2_ACCOUNT_ID']):
        raise RuntimeError('QA_CUSTODY_UNCERTAIN: legacy read credentials unavailable')
    register_secret(values['R2_SECRET_ACCESS_KEY'])
    client = boto3.client('s3', endpoint_url='https://' + values['R2_ACCOUNT_ID'] + '.r2.cloudflarestorage.com',
                          aws_access_key_id=values['R2_ACCESS_KEY_ID'], aws_secret_access_key=values['R2_SECRET_ACCESS_KEY'],
                          region_name='auto', config=Config(retries={'total_max_attempts': 2}, read_timeout=15, connect_timeout=10))
    def get(key):
        if not re.fullmatch(r'qa-capture-ledgers/onmiss/(?:box|attempt)-[0-9a-f]{16}\.json', key):
            raise RuntimeError('QA_CUSTODY_UNCERTAIN: not an on-miss ledger key')
        try:
            obj = client.get_object(Bucket=LEGACY_BUCKET, Key=key)
            return obj['Body'].read(), obj.get('ETag')
        except Exception as error:
            if _is_missing(error):
                return None
            raise RuntimeError('QA_CUSTODY_UNCERTAIN: legacy ledger lookup failed (' + type(error).__name__ + ')') from None
    return get


def qa_ledger_access(row):
    """QA ledger get/put with the derived custody session only: CAS-style (If-None-Match create / If-Match update)."""
    import boto3
    from botocore.config import Config
    client = boto3.client('s3', endpoint_url='https://' + row['account'] + '.r2.cloudflarestorage.com', aws_access_key_id=row['access'],
                          aws_secret_access_key=row['secret'], aws_session_token=row['token'], region_name='auto',
                          config=Config(retries={'total_max_attempts': 1}, read_timeout=15, connect_timeout=10))
    condition = {}
    client.meta.events.register_first('before-sign.s3.PutObject', lambda request, **kw: [request.headers.__setitem__(k, v) for k, v in condition.items()])
    def get(key):
        try:
            obj = client.get_object(Bucket=SCOPED_FIXTURE_BUCKET, Key=key)
            return obj['Body'].read(), obj.get('ETag')
        except Exception as error:
            if _is_missing(error):
                return None
            raise RuntimeError('QA_CUSTODY_UNCERTAIN: QA ledger lookup failed (' + type(error).__name__ + ')') from None
    def put(key, body, etag):
        condition.clear()
        condition.update({'If-Match': etag} if etag else {'If-None-Match': '*'})
        try:
            client.put_object(Bucket=SCOPED_FIXTURE_BUCKET, Key=key, Body=body, ContentType='application/json')
        except Exception as error:
            raise RuntimeError('QA_CUSTODY_CONFLICT: QA ledger write refused (' + type(error).__name__ + ')') from None
    return get, put


def reconcile_custody_cli(box, head, spend_box, manifest_path, output, read_only, legacy_env):
    manifest = load_exact_manifest(manifest_path, head)
    ids = derive_exact_ids(box, head, spend_box)
    row = mint_exact_sessions('custody', ids, manifest)['custody']
    qa_get, qa_put = qa_ledger_access(row)
    receipt = reconcile_custody(ids, manifest, legacy_reader(legacy_env), qa_get, None if read_only else qa_put)
    Path(output).write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
    print('QA_CUSTODY status=' + receipt['status'] + ' allowance_usd=' + str(receipt['allowance_usd']) + ' bound=' + str(receipt['bound']).lower())
    if receipt['refusals']:
        raise RuntimeError('QA_CUSTODY_REFUSED: ' + ','.join(sorted({r['code'] for r in receipt['refusals']})))


def arm_exact_sessions(box, manifest_path):
    """Box side: put the controller's freshly minted proxy sessions in THIS box's Modal Dict (deleted by stop()). Input is JSON on
    stdin (never argv or disk); every row must be for this manifest and this lease and unexpired."""
    manifest = load_exact_manifest(manifest_path)
    payload = json.loads(sys.stdin.read(65536))
    now = time.time()
    if not isinstance(payload, dict) or set(payload) != {'read', 'write'}:
        raise ValueError('QA_SCOPED_ARM_INVALID')
    for name, row in payload.items():
        if (not isinstance(row, dict) or row.get('kind') != name or row.get('bucket') != SCOPED_FIXTURE_BUCKET
                or row.get('manifest_digest') != manifest['digest'] or row.get('lease') != identity(box)
                or not re.fullmatch(r'[0-9a-f]{32}', str(row.get('account', '')))
                or any(not isinstance(row.get(f), str) or not row[f] for f in ('access', 'secret', 'token'))
                or not isinstance(row.get('exp'), int) or not now + 30 < row['exp'] <= now + EXACT_SESSION_TTL_S + 30):
            raise ValueError('QA_SCOPED_ARM_INVALID')
        register_secret(row['secret'])
        register_secret(row['token'])
    os.environ.pop('MODAL_PROFILE', None)
    os.environ.update(modal_env(True))
    import modal
    store = modal.Dict.from_name(onmiss_dict_name(box), create_if_missing=True)
    for name, row in payload.items():
        store.put('scoped-session/' + name, row)
    print('QA_SCOPED_ARM kinds=read,write expires_in_s=' + str(min(int(r['exp'] - now) for r in payload.values())))


def load_report_session(path, ids):
    """Controller-piped report session (JSON, mode 600 on the box): validated against THIS run, exported for the scoped report
    container only, then deleted. Missing, expired or another run's session refuses; there is no fallback."""
    p = Path(path)
    if p.is_symlink() or not p.is_file() or p.stat().st_size > 16384:
        raise RuntimeError('QA_SCOPED_R2_UNAVAILABLE: report session missing')
    try:
        row = json.loads(p.read_text())
    finally:
        try:
            p.unlink()
        except OSError:
            pass
    if (not isinstance(row, dict) or row.get('kind') != 'report' or row.get('bucket') != SCOPED_FIXTURE_BUCKET
            or row.get('manifest_digest') != ids['exact']['digest'] or row.get('lease') != ids['lease']
            or not isinstance(row.get('exp'), int) or row['exp'] <= time.time() + 20
            or not re.fullmatch(r'[0-9a-f]{32}', str(row.get('account', '')))
            or any(not isinstance(row.get(f), str) or not row[f] for f in ('access', 'secret', 'token'))):
        raise RuntimeError('QA_SCOPED_R2_UNAVAILABLE: report session invalid, expired or for another run')
    register_secret(row['secret'])
    register_secret(row['token'])
    return dict(zip(SCOPED_FIXTURE_R2_ENV, (row['bucket'], row['account'], row['access'], row['secret'], row['token'])))


def check_canary_receipt(path):
    """Arming prerequisite: a PASSED publish/readback canary produced by THIS controller file, not older than 48 hours."""
    p = Path(path)
    if p.is_symlink() or not p.is_file() or p.stat().st_size > 65536:
        raise ValueError('QA_SCOPED_CANARY_RECEIPT_INVALID: unreadable')
    r = json.loads(p.read_text())
    ok = (isinstance(r, dict) and r.get('bucket') == SCOPED_FIXTURE_BUCKET and r.get('list_and_read') is True
          and r.get('first_conditional_put') == 'created' and r.get('second_conditional_put') == 'precondition_failed'
          and isinstance(r.get('before'), dict) and r.get('before') == r.get('after')
          and 'HTTP 403' in str(r.get('out_of_scope_get', '')) and (r.get('canary_objects_removed') or 0) >= 3
          and str(r.get('modal_teardown', '')).startswith('verified')
          and r.get('report_secret_mount') == 'ephemeral scoped session only'
          and r.get('tooling_sha256') == hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
          and isinstance(r.get('at'), int) and 0 <= time.time() - r['at'] <= CANARY_MAX_AGE_S)
    if not ok:
        raise ValueError('QA_SCOPED_CANARY_RECEIPT_INVALID: not a passed canary of this controller, or older than 48 hours')


def run_capture_report(root, app_name, run_id, capture, ids, scoped_env=None, canary=None):
    import modal
    app = modal.App(app_name)
    image = modal.Image.debian_slim().pip_install('boto3==1.40.49').add_local_file(
        str(root/'scripts/ci/publish-staged-fixtures-bare.py'), '/qa/publisher.py')
    scoped = scoped_env is not None
    mounts = ([modal.Secret.from_dict(scoped_fixture_env(scoped_env))] if scoped
              else [modal.Secret.from_name('heydonna-r2-credentials')])

    @app.function(image=image, secrets=mounts,
                  max_containers=1, retries=0, timeout=180, serialized=True)
    def finish_run(run_id, capture, ids, scoped, canary):
        import os, runpy
        publisher = runpy.run_path('/qa/publisher.py')
        if scoped:
            s3, bucket = scoped_fixture_client(os.environ)
        else:
            s3, bucket = publisher['_client'](), os.environ.get('LLM_FIXTURE_R2_BUCKET', 'heydonna-fixtures')
        if canary:
            return qa_scope_canary(s3, bucket, publisher, canary)
        return qa_finish(s3, bucket, run_id, capture and not ids, publisher, ids)
    from contextlib import nullcontext
    with (nullcontext() if scoped else modal.enable_output()), app.run():
        return finish_run.remote(run_id, capture, ids, scoped, canary)


def scoped_report_canary(root, output):
    nonce = secrets.token_hex(16)
    scoped_env = mint_scoped_canary_session(nonce)
    env = modal_env()
    os.environ.update(env)
    app_name = 'heydonna-qa-ledger-canary-' + nonce
    try:
        report = run_capture_report(root, app_name, '', False, None, scoped_env, nonce)
    finally:
        for attempt in range(5):
            apps = cli(['app', 'list', '--json'], env, True)
            if not any(a.get('description') == app_name and a.get('state', '').lower() != 'stopped' for a in apps):
                break
            if attempt == 4:
                raise RuntimeError('QA_SCOPED_CANARY_MODAL_TEARDOWN_UNVERIFIED')
            time.sleep(3)
    report['modal_teardown'] = 'verified no active canary app'
    report['report_secret_mount'] = 'ephemeral scoped session only'
    report['tooling_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report['at'] = int(time.time())
    output.write_text(json.dumps(report, indent=2) + '\n')
    print('QA_SCOPED_CANARY=PASS')


def capture_report(box, root, output, capture=False, allow_billable_tools=False, tool_spend_cap_usd=2.0,
                   onmiss=False, scoped=False, exact=None, session_file=None):
    os.environ.update(modal_env(True))
    run_id = 'remote-qa-' + identity(box)
    if exact and not onmiss:
        raise RuntimeError('--exact-manifest requires --on-miss')
    ids = onmiss_effective(root, box, onmiss, exact)
    if exact:
        # Exact mode: the report container gets ONLY the controller-piped prefix-scoped session (no shared secret, no fallback).
        scoped = True
        if capture or not ids or not session_file:
            raise RuntimeError('QA_SCOPED_R2_REFUSED: exact report requires a bound lease and an armed report session')
        os.environ.update(load_report_session(session_file, ids))
    if scoped and (capture or not ids):
        raise RuntimeError('QA_SCOPED_R2_REFUSED: scoped report requires a bound on-miss lease')
    report = run_capture_report(root, 'heydonna-qa-ledger-' + identity(box), run_id, capture, ids,
                                scoped_fixture_env(os.environ) if scoped else None)
    report['billable_tools'] = tool_policy(allow_billable_tools, tool_spend_cap_usd)
    report['tool_charged_upper_bound_usd'] = sum(row['charged_bound_usd'] for row in report['attempts'].values()
                                                if row.get('billable_tools'))
    output.write_text(json.dumps(report, indent=2)+'\n')
    emit_report(report, run_id)


def emit_report(report, run_id):
    """Typed controller terminal: a cap hit, refusal, unknown or unsettled outcome raises (verdict fail), never PASS."""
    print('QA_CAPTURE_REPORT run_id='+run_id+' estimated_cost_usd='+str(report['estimated_cost_usd'])+
          ' charged_upper_bound_usd='+str(report['charged_upper_bound_usd']))
    for key in report['canonical_fixture_keys']:
        print('QA_CAPTURE_FIXTURE='+key)
    if report.get('mode') == 'capture-on-miss':
        print('QA_ONMISS_REPORT ' + json.dumps(report['onmiss'], sort_keys=True))
        if report.get('exact'):
            exact = report['exact']
            print('QA_ONMISS_EXACT approved=%d captured=%d readback_match=%d digest=%s' % (
                len(exact['approved']), len(exact['captured']), exact['readback_match'], exact['digest']))
            for row in report.get('readback') or []:
                print('QA_ONMISS_READBACK ' + json.dumps(row, sort_keys=True))
            for row in report.get('join') or []:
                print('QA_ONMISS_JOIN ' + json.dumps(row, sort_keys=True))
        if report.get('terminal_reason'):
            print('QA_ONMISS_TERMINAL reason=' + report['terminal_reason'])
            raise RuntimeError('QA_ONMISS_TERMINAL ' + report['terminal_reason'] + ': capture-on-miss refused, capped or uncertain; not a PASS')
    if report.get('halted') or report.get('rejected_calls') or report['unsettled_attempts'] or report.get('publication_error'):
        raise RuntimeError('QA capture halted or has uncertain provider spend; see charged upper bound')


def ready():
    for label in ('AUDIO','DOCX'):
        url = os.environ['MODAL_'+label+'_ENDPOINT_URL'].rstrip('/')+'/llm-proxy-readiness'
        req = urllib.request.Request(url, headers={'X-API-Key':os.environ['MODAL_API_KEY']})
        with urllib.request.urlopen(req, timeout=90) as response:
            data = json.load(response)
        expected = 'capture' if os.environ.get('LLM_PROXY_FIXTURE_CAPTURE') == 'true' else 'strict'
        if data.get('mode') != expected:
            raise RuntimeError('QA '+label+' proxy mode mismatch; expected '+expected)
        if expected == 'capture' and data.get('capture_run_id') != os.environ.get('LLM_PROXY_CAPTURE_RUN_ID'):
            raise RuntimeError('QA capture run identity mismatch')
        onmiss = os.environ.get('QA_LLM_ONMISS') == '1'
        if bool(data.get('qa_onmiss')) != onmiss:
            # Allowlist modes and actual booleans only; never dump health JSON or local env.
            diagnostic = {'qa_onmiss_present': 'qa_onmiss' in data,
                          'mode': data.get('mode') if data.get('mode') in ('strict', 'capture') else 'unknown'}
            for predicate in ('overlay_baked', 'strict', 'capture', 'ci', 'suffix_ok'):
                diagnostic[predicate + '_present'] = isinstance(data.get(predicate), bool)
                if diagnostic[predicate + '_present']:
                    diagnostic[predicate] = data[predicate]
            print('QA_ONMISS_HEALTH endpoint='+label.lower()+' '+json.dumps(diagnostic, sort_keys=True),
                  file=sys.stderr, flush=True)
            raise RuntimeError('QA '+label+' capture-on-miss mode mismatch; expected '+str(onmiss).lower())
        print('QA_MODAL_READINESS endpoint='+label.lower()+' mode='+expected+' onmiss='+str(onmiss).lower())
    # The review and chat-tools apps expose no /llm-proxy-readiness route; ownership and hostname are
    # already validated at resolve time, so they are reported as skipped rather than probed.
    # Terms (#11000) has no readiness route: an authenticated empty POST must reach the handler and be
    # rejected as invalid (400). 401 = QA API-key secret mismatch, 503 = service unconfigured.
    if os.environ.get('MODAL_TERMS_ENDPOINT_URL'):
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs): return None
        opener = urllib.request.build_opener(_NoRedirect)
        url = os.environ['MODAL_TERMS_ENDPOINT_URL'].rstrip('/')+'/materialize_editor_keys'
        deadline = time.time()+900
        while True:
            req = urllib.request.Request(url, data=b'{}', method='POST',
                                         headers={'X-API-Key':os.environ['MODAL_API_KEY'],'Content-Type':'application/json'})
            try:
                opener.open(req, timeout=300)
                status = 200
            except urllib.error.HTTPError as e:
                status = e.code
                # Modal answers 303 with a polling Location while a cold container (spaCy load) starts; re-POST there.
                if status == 303 and e.headers.get('Location') and time.time() < deadline:
                    url = e.headers['Location']
                    continue
            except OSError:
                if time.time() < deadline:
                    time.sleep(5)
                    continue
                raise
            break
        # 404/405 = an older terms app without this route (other branches); reachable, so not a readiness failure.
        if status not in (400, 404, 405, 422):
            raise RuntimeError('QA terms endpoint not ready; HTTP '+str(status))
        print('QA_MODAL_READINESS endpoint=terms probe=reachable http='+str(status))
    for label in ('REVIEW','CHAT_TOOLS','EDITOR_CHAT','AUDIT'):
        if os.environ.get('MODAL_'+label+'_ENDPOINT_URL'):
            print('QA_MODAL_READINESS endpoint='+label.lower()+' probe=skipped')
    if os.environ.get('MODAL_SPEAKER_MAPPING_URL'):
        print('QA_MODAL_READINESS endpoint=speaker_mapping probe=skipped')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['register','credentials','check-credentials','finish','stop','reap','overlay','prepare','resolve','ready','provider-ready','capture-report','scoped-report-canary','tool-policy','route-check',
                                            'check-exact-manifest','check-canary-receipt','mint-session','arm-session','check-exact-ids',
                                            'reconcile-custody','check-grant-receipt','check-app-data','check-app-mounts','verify-join'])
    parser.add_argument('--box')
    parser.add_argument('--ttl',type=int,default=2700)
    parser.add_argument('--root',type=Path)
    parser.add_argument('--config',type=Path)
    parser.add_argument('--remote',action='store_true')
    parser.add_argument('--verify',action='store_true')
    parser.add_argument('--capture',action='store_true')
    parser.add_argument('--on-miss',action='store_true',
                        help='capture-on-miss (default QA capture lane): replay first, one priced eligible miss, $3 box+attempt cap')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--allow-billable-tools',action='store_true',
                        help='Opt-in synthetic capture: metered Google Search with a soft admission budget')
    parser.add_argument('--tool-spend-cap-usd',type=float,default=2.0,
                        help='Tool token + search admission budget; final call may overshoot (default: 2)')
    parser.add_argument('--extra-secret',action='append',default=[],metavar='NAME=generate',
                        help='prepare: generate ONE dev-only secret on the box and set it on the disposable Convex env AND the leased '
                             'per-box Modal secret. NAME must be allowlisted ('+', '.join(EXTRA_SECRET_ALLOWLIST)+'); the value is never printed')
    parser.add_argument('--convex-helper',type=Path,help='prepare --extra-secret: remote-qa-convex.py copy on the box')
    parser.add_argument('--convex-root',type=Path,help='prepare --extra-secret: dir with the controller ownership.json receipt')
    parser.add_argument('--exact-manifest',type=Path,help='M15 scoped exact-hash capture (needs --on-miss): per-run approval manifest; only '
                        'its full hashes may dispatch live, every other miss is a typed refusal with zero provider I/O')
    parser.add_argument('--ids-out',type=Path,help='overlay --exact-manifest: write the non-secret run identities here')
    parser.add_argument('--ids',type=Path,help='mint-session: the non-secret run identities file')
    parser.add_argument('--kind',choices=['proxy','report','custody','check'],help='mint-session')
    parser.add_argument('--spend-box',help='trusted-host physical box identity (pool server name; the box itself for an ephemeral box)')
    parser.add_argument('--dry-check',action='store_true',help='overlay --exact-manifest: authoritative-miss dry-check, never prices, claims, reserves or dispatches')
    parser.add_argument('--read-only',action='store_true',help='reconcile-custody: read both buckets, write nothing')
    parser.add_argument('--env',type=Path,help='check-app-data: the .env.local to read back')
    parser.add_argument('--legacy-env',type=Path,default=Path(LEGACY_ENV_DEFAULT),help='reconcile-custody: app env file holding the legacy-bucket read key (names only; never printed)')
    parser.add_argument('--report',type=Path,help='verify-join')
    parser.add_argument('--trace',type=Path,help='verify-join: the run-local spec langsmith-trace.json (optional)')
    parser.add_argument('--run-id',help='verify-join: the controller run id')
    parser.add_argument('--session-file',type=Path,help='capture-report --exact-manifest: controller-piped report session (deleted after use)')
    parser.add_argument('--receipt',type=Path,help='check-canary-receipt')
    parser.add_argument('--head',help='check-exact-manifest: required checkout head')
    parser.add_argument('--spec',help='check-exact-manifest: the launch spec path, which must be the profile-approved spec')
    a=parser.parse_args()
    tool_policy(a.allow_billable_tools, a.tool_spend_cap_usd)
    if a.extra_secret and a.command!='prepare':
        parser.error('--extra-secret is only valid with prepare')
    if a.allow_billable_tools and not a.capture:
        parser.error('--allow-billable-tools requires --capture (synthetic data only)')
    if a.on_miss and a.capture:
        parser.error('--on-miss and --capture are exclusive')
    if a.exact_manifest and a.command in ('register','overlay','prepare','capture-report') and not a.on_miss:
        parser.error('--exact-manifest requires --on-miss')
    if a.exact_manifest and a.command not in ('register','overlay','prepare','capture-report','check-exact-manifest','mint-session','arm-session',
                                              'check-exact-ids','reconcile-custody','verify-join'):
        parser.error('--exact-manifest is not valid for this command')
    if a.dry_check and a.command != 'overlay':
        parser.error('--dry-check is only valid with overlay')
    if a.command in ('register','credentials','finish','stop','prepare'):
        identity(a.box or '')
    if a.command=='route-check': route_check(a.capture); raise SystemExit(0)
    if a.command=='tool-policy': print(json.dumps(tool_policy(a.allow_billable_tools, a.tool_spend_cap_usd), sort_keys=True))
    elif a.command=='register':
        register(a.box,a.ttl,a.allow_billable_tools,a.tool_spend_cap_usd,a.on_miss,
                 load_exact_manifest(a.exact_manifest)['digest'] if a.exact_manifest else None)
    elif a.command=='check-credentials':
        token_env()
        print('QA_MODAL_AUTOMATION_CREDENTIALS=present')
    elif a.command=='credentials':
        for k,v in token_env().items(): print('export '+k+'='+shlex.quote(v))
    elif a.command=='finish': finish(a.box)
    elif a.command=='stop': stop(a.box,a.remote)
    elif a.command=='reap': reap()
    elif a.command=='overlay': overlay(a.root,a.box,a.capture,a.allow_billable_tools,a.tool_spend_cap_usd,a.on_miss,a.exact_manifest,a.ids_out,a.dry_check)
    elif a.command=='prepare': prepare(a.box,a.root,a.config,a.capture,a.allow_billable_tools,a.tool_spend_cap_usd,a.on_miss,
                                       a.extra_secret,a.convex_helper,a.convex_root,a.exact_manifest)
    elif a.command=='capture-report': capture_report(a.box,a.root,a.output,a.capture,a.allow_billable_tools,a.tool_spend_cap_usd,a.on_miss,
                                                     False,a.exact_manifest,a.session_file)
    elif a.command=='check-exact-manifest':
        if not a.exact_manifest:
            parser.error('check-exact-manifest requires --exact-manifest')
        manifest = load_exact_manifest(a.exact_manifest, a.head)
        if a.spec is not None:
            check_exact_profile_spec(manifest, a.spec)
        print('QA_EXACT_MANIFEST ok digest=' + manifest['digest'] + ' head=' + manifest['head'] + ' product_pin=' + manifest['product_pin']
              + ' profile=' + manifest['profile'] + ' spec_blob=' + str(EXACT_PROFILES[manifest['profile']]['spec_blob'] or 'none')
              + ' max_reserve_usd=' + str(manifest['max_reserve_usd']) + ' hashes=' + str(len(manifest['entries']))
              + ' approval_ref=' + manifest['approval_ref'])
    elif a.command=='check-canary-receipt':
        if not a.receipt:
            parser.error('check-canary-receipt requires --receipt')
        check_canary_receipt(a.receipt)
        print('QA_SCOPED_CANARY_RECEIPT ok')
    elif a.command=='mint-session':
        if a.kind=='check':
            _mint_session(['fixtures/llm/v2/'], ['GetObject'], 60)
            print('QA_SCOPED_MINT=ok')
        elif a.kind in ('proxy','report','custody') and a.box and a.head and a.exact_manifest:
            # Scope is derived HERE from the trusted lease, physical-box identity and exact head. A box-written ids file is only
            # CHECKED against it (DENIED on any difference); it never selects a path.
            manifest = load_exact_manifest(a.exact_manifest, a.head)
            ids = derive_exact_ids(a.box, a.head, a.spend_box)
            if a.ids:
                check_exact_ids(ids, load_exact_ids(a.ids), manifest)
            print(json.dumps(mint_exact_sessions(a.kind, ids, manifest)))
        else:
            parser.error('mint-session needs --kind check, or --kind proxy|report|custody with --box, --head, --spend-box and --exact-manifest')
    elif a.command=='check-exact-ids':
        if not (a.box and a.head and a.ids and a.exact_manifest):
            parser.error('check-exact-ids requires --box, --head, --spend-box, --ids and --exact-manifest')
        manifest = load_exact_manifest(a.exact_manifest, a.head)
        ids = derive_exact_ids(a.box, a.head, a.spend_box)
        check_exact_ids(ids, load_exact_ids(a.ids), manifest)
        print('QA_EXACT_IDS ok lease=' + ids['lease'] + ' spend_box=' + ids['spend_box'] + ' head=' + ids['head'][:12])
    elif a.command=='reconcile-custody':
        if not (a.box and a.head and a.exact_manifest and a.output):
            parser.error('reconcile-custody requires --box, --head, --spend-box, --exact-manifest and --output')
        reconcile_custody_cli(a.box, a.head, a.spend_box, a.exact_manifest, a.output, a.read_only, a.legacy_env)
    elif a.command=='check-grant-receipt':
        if not a.receipt:
            parser.error('check-grant-receipt requires --receipt')
        print('QA_GRANT_RECEIPT ok sha256=' + check_grant_receipt(a.receipt))
    elif a.command=='check-app-data':
        if not a.env:
            parser.error('check-app-data requires --env')
        _values, fingerprint = app_data_values(a.env.read_text())
        print('QA_APP_DATA_READBACK bucket=' + SCOPED_FIXTURE_BUCKET + ' signer=' + fingerprint + ' public_url=none ok')
    elif a.command=='check-app-mounts':
        if not a.root:
            parser.error('check-app-mounts requires --root')
        remaining = shared_mounts_remaining(a.root)
        print('QA_APP_MOUNTS shared_mount_remaining=' + str(len(remaining)) + (' files=' + ','.join(remaining[:5]) if remaining else ''))
        if remaining:
            raise RuntimeError('QA_APP_MOUNTS_SHARED_REMAIN: a leased runtime would mount the shared R2 secret')
    elif a.command=='verify-join':
        if not (a.report and a.exact_manifest and a.run_id and a.output):
            parser.error('verify-join requires --report, --exact-manifest, --run-id and --output')
        manifest = load_exact_manifest(a.exact_manifest)
        def _load(path):
            try:
                return json.loads(Path(path).read_text()) if path else None
            except (OSError, ValueError):
                return None
        report, trace = _load(a.report), _load(a.trace)
        status = exact_completion_status(report, trace, a.run_id, manifest)
        rows = (report or {}).get('join') or []
        keep = ('request_id', 'run_id', 'trace_id', 'project', 'project_session_id', 'transcript_id', 'created_transcript_id', 'draft_sha256', 'aligned_slice_sha256')
        langsmith = manifest['langsmith'] is not None
        a.output.write_text(json.dumps({'status': status, 'profile': manifest['profile'], 'proxy': rows,
                                        'langsmith': {k: trace.get(k) for k in keep} if langsmith and isinstance(trace, dict) else None,
                                        'expected': ({'project': 'qa-m15-' + a.run_id, **manifest['langsmith']} if langsmith else
                                                     {'approved_hash': next(iter(manifest['entries'].values()))['hash'], 'stage': EXACT_PROFILES[manifest['profile']]['stage']})},
                                       indent=2, sort_keys=True) + '\n')
        print('QA_ONMISS_JOIN_RESULT status=' + status + ' dispatches=' + str(len(rows)) + ' profile=' + manifest['profile'])
    elif a.command=='arm-session':
        if not a.exact_manifest or not a.box:
            parser.error('arm-session requires --box and --exact-manifest')
        arm_exact_sessions(a.box, a.exact_manifest)
    elif a.command=='scoped-report-canary':
        if not a.root or not a.output or a.on_miss or a.capture:
            parser.error('scoped-report-canary requires --root and --output, with no capture or provider dispatch flags')
        scoped_report_canary(a.root, a.output)
    elif a.command=='resolve': resolve(a.box,a.root,a.config,a.verify)
    elif a.command=='ready': ready()
    elif a.command=='provider-ready': provider_ready(a.root)

if __name__=='__main__':
    try: main()
    except Exception as e:
        # Provider/CLI errors may contain credentials: emit the class plus a SANITIZED message and
        # last traceback frame (file basename:line in function). Never env, never raw streams.
        import traceback
        frame = traceback.extract_tb(e.__traceback__)[-1] if e.__traceback__ else None
        where = (os.path.basename(frame.filename)+':'+str(frame.lineno)+' in '+frame.name) if frame else 'unknown'
        print('QA_MODAL_FAILED class='+type(e).__name__,file=sys.stderr)
        print('QA_MODAL_FAILED_MESSAGE='+sanitize(e),file=sys.stderr)
        print('QA_MODAL_FAILED_FRAME='+sanitize(where, limit=200),file=sys.stderr)
        sys.exit(1)
