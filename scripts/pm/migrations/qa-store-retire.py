#!/usr/bin/env python3
"""One-time retirement of the separate QA R2 bucket: carry everything captured there into the canonical fixture store.

Rajiv D0AMF0XE6TS thread 1795294139.951329, ts 1795316993.447349 ("do it", retiring the separate QA bucket; rule 65). The CTO's
pre-spend condition: custody must carry prior reservations and UNKNOWN claims across the store change, and the retired bucket and
its receipts stay intact. This tool therefore only READS the source bucket (there is no delete, overwrite or write path to it) and
copies, PUT-if-absent and digest-verified, into heydonna-fixtures:

  fixtures/llm/v2/**            -> the same key (canonical LLM fixtures; the publisher's valid-response guard applies, S161)
  staging/llm-capture/**        -> the same key (staged responses keep the staging prefix the unchanged publisher reads)
  qa-capture-ledgers/<rest>     -> qa-onmiss/ledgers/<rest>   (box/attempt/per-run ledgers: reservations, UNKNOWN claims, caps)
  everything else (QA app data: projects/, practice/, ...) is NOT copied: it is app data, not captured fixtures, and rule 65 keeps
  app data in heydonna-uploads. It is counted and left where it is; deleting the bucket needs a separate Rajiv OK.

After a clean apply it writes the carry marker (qa-onmiss/migration/qa-store-carry.json) that the exact lane's custody reconcile
requires; without it custody refuses (QA_CUSTODY_STORE_NOT_MIGRATED), so a fresh ledger can never reset the box/attempt caps.

  dry-run (default): lists, classifies, compares, writes the report; changes nothing anywhere.
  apply: also needs --confirm-no-live-qa-run (the operator attests that no QA box run is live) and refuses once the marker exists.

Run (trusted host): uv run --no-project --python 3.13 --with boto3==1.35.99 python scripts/pm/migrations/qa-store-retire.py --report FILE
Secrets are read by name from the app env file in this process and never printed.
"""
import argparse
import hashlib
import json
import shlex
import sys
import time
from pathlib import Path

SOURCE_BUCKET = 'heydonna-qa'      # retired; read-only here
DEST_BUCKET = 'heydonna-fixtures'  # the canonical fixture store
SHARED_R2_FP = '3be914b2600e2b2d'  # sha256(access key id)[:16] of the ONE shared HeyDonna R2 key (rule 65)
FIXTURE_PREFIX = 'fixtures/llm/v2/'
STAGING_PREFIX = 'staging/llm-capture/'
LEDGER_SRC = 'qa-capture-ledgers/'
LEDGER_DST = 'qa-onmiss/ledgers/'
MARKER_KEY = 'qa-onmiss/migration/qa-store-carry.json'
ENV_DEFAULT = '/Users/rajiv/Downloads/projects/heydonna-app/.env.local'
APP_DEFAULT = '/Users/rajiv/Downloads/projects/heydonna-app'
COPY_CLASSES = ('fixture', 'staging', 'ledger')


def classify(key):
    """(class, destination key or None). Only the three capture classes are copied."""
    if key.startswith(FIXTURE_PREFIX):
        return 'fixture', key
    if key.startswith(STAGING_PREFIX):
        return 'staging', key
    if key.startswith(LEDGER_SRC):
        return 'ledger', LEDGER_DST + key[len(LEDGER_SRC):]
    return 'app_data', None


def sha256(body):
    return hashlib.sha256(body).hexdigest()


def ledger_summary(body):
    """What a ledger carries across the store change: spend at its charged bound, unsettled (UNKNOWN) attempts and request states."""
    try:
        doc = json.loads(body)
    except ValueError:
        return {'readable': False}
    attempts = doc.get('attempts') if isinstance(doc, dict) and isinstance(doc.get('attempts'), dict) else {}
    requests = doc.get('requests') if isinstance(doc, dict) and isinstance(doc.get('requests'), dict) else {}
    states = {}
    for row in requests.values():
        state = row.get('state') if isinstance(row, dict) else None
        states[state] = states.get(state, 0) + 1
    return {'readable': True, 'attempts': len(attempts), 'unsettled_attempts': sum(not (a or {}).get('settled') for a in attempts.values()),
            'committed_usd': round(sum((a or {}).get('charged_bound_usd', 0) for a in attempts.values()), 6),
            'request_states': states, 'halted': bool(isinstance(doc, dict) and doc.get('halted'))}


def _is_missing(error):
    return getattr(error, 'response', {}).get('Error', {}).get('Code') in ('NoSuchKey', '404', 'NotFound')


def read_object(client, bucket, key):
    """(body, content_type, metadata) or None for an authoritative absence; any other failure raises (never a guess)."""
    try:
        got = client.get_object(Bucket=bucket, Key=key)
    except Exception as error:
        if _is_missing(error):
            return None
        raise
    return got['Body'].read(), got.get('ContentType') or 'application/octet-stream', dict(got.get('Metadata') or {})


def list_all(client, bucket):
    out, token = [], None
    while True:
        args = {'Bucket': bucket, 'MaxKeys': 1000}
        if token:
            args['ContinuationToken'] = token
        page = client.list_objects_v2(**args)
        out += page.get('Contents', [])
        if not page.get('IsTruncated'):
            return sorted(out, key=lambda o: o['Key'])
        token = page['NextContinuationToken']


def build_rows(client, objects, unpublishable=None):
    """Classify every source object and compare each capture-class object with its destination. No writes."""
    rows, app_data = [], {}
    for obj in objects:
        kind, dest_key = classify(obj['Key'])
        if kind == 'app_data':
            top = obj['Key'].split('/', 1)[0]
            bucket = app_data.setdefault(top, {'objects': 0, 'bytes': 0})
            bucket['objects'] += 1
            bucket['bytes'] += obj.get('Size', 0)
            continue
        source = read_object(client, SOURCE_BUCKET, obj['Key'])
        if source is None:
            raise RuntimeError('source object vanished during the listing: ' + obj['Key'])
        body, content_type, metadata = source
        row = {'class': kind, 'source_key': obj['Key'], 'dest_key': dest_key, 'bytes': len(body), 'sha256': sha256(body),
               'content_type': content_type, 'metadata': metadata}
        if kind == 'ledger':
            row['ledger'] = ledger_summary(body)
        reason = unpublishable(body, metadata) if (unpublishable and kind in ('fixture', 'staging')) else None
        if reason:
            row['state'] = 'skipped_unpublishable'
            row['reason'] = reason
        else:
            dest = read_object(client, DEST_BUCKET, dest_key)
            row['state'] = 'would_copy' if dest is None else 'identical' if sha256(dest[0]) == row['sha256'] else 'conflict'
        row['_body'] = body
        rows.append(row)
    return rows, app_data


def apply_rows(client, rows, put_if_absent):
    """PUT-if-absent each would_copy row, then read the destination back and compare digests. Never replaces an existing object."""
    for row in rows:
        if row['state'] != 'would_copy':
            continue
        try:
            created = put_if_absent(client, DEST_BUCKET, row['dest_key'], row['_body'], row['content_type'], row['metadata'])
            back = read_object(client, DEST_BUCKET, row['dest_key'])
        except Exception as error:
            row['state'], row['reason'] = 'error', type(error).__name__
            continue
        if back is None or sha256(back[0]) != row['sha256']:
            row['state'] = 'conflict' if (back is not None and not created) else 'error'
            row['reason'] = 'readback_mismatch' if back is not None else 'readback_absent'
        else:
            row['state'] = 'copied' if created else 'identical'
    return rows


def carry_marker(rows, listing_digest, object_count, app_data, signer, at):
    ledgers = [r for r in rows if r['class'] == 'ledger']
    fixtures = [r for r in rows if r['class'] in ('fixture', 'staging')]
    complete = all(r['state'] in ('copied', 'identical') for r in ledgers) and not any(r['state'] == 'error' for r in rows)
    return {'schema': 'qa-store-carry/v1', 'complete': complete, 'dest_prefix': LEDGER_DST, 'at': at, 'signer': signer,
            'source': {'bucket': SOURCE_BUCKET, 'status': 'retained intact: this tool has no write or delete path to it',
                       'objects': object_count, 'listing_sha256': listing_digest},
            'ledgers': [{'source_key': r['source_key'], 'dest_key': r['dest_key'], 'sha256': r['sha256'], 'bytes': r['bytes'], 'state': r['state'],
                         **{k: v for k, v in r['ledger'].items() if k != 'readable'}} for r in ledgers],
            'fixtures': {'copied': sum(r['state'] == 'copied' for r in fixtures), 'identical': sum(r['state'] == 'identical' for r in fixtures),
                         'conflicts': [r['source_key'] for r in fixtures if r['state'] == 'conflict'],
                         'skipped_unpublishable': [{'key': r['source_key'], 'reason': r['reason']} for r in fixtures if r['state'] == 'skipped_unpublishable']},
            'app_data_not_copied': app_data}


def public(rows):
    return [{k: v for k, v in r.items() if k not in ('_body',)} for r in rows]


def env_names(path, names):
    found = {}
    for line in Path(path).read_text().splitlines():
        key, sep, value = line.removeprefix('export ').partition('=')
        if sep and key.strip() in names:
            parts = shlex.split(value, comments=True)
            if len(parts) == 1:
                found[key.strip()] = parts[0]
    return found


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('dry-run', 'apply'), default='dry-run')
    parser.add_argument('--env', default=ENV_DEFAULT)
    parser.add_argument('--app-root', default=APP_DEFAULT, help='checkout holding scripts/ci/publish-staged-fixtures-bare.py (the existing publisher guard)')
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--confirm-no-live-qa-run', action='store_true', help='apply only: attest that no QA box run is live')
    args = parser.parse_args(argv)
    if args.mode == 'apply' and not args.confirm_no_live_qa_run:
        parser.error('apply needs --confirm-no-live-qa-run (check remote-browser-box.sh list first)')
    import runpy
    import boto3
    from botocore.config import Config
    values = env_names(args.env, ('R2_ACCOUNT_ID', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY'))
    if len(values) != 3 or sha256(values['R2_ACCESS_KEY_ID'].encode())[:16] != SHARED_R2_FP:
        raise SystemExit('QA_STORE_RETIRE_REFUSED: the shared R2 key is unavailable or is not the one shared HeyDonna key')
    client = boto3.client('s3', endpoint_url='https://' + values['R2_ACCOUNT_ID'] + '.r2.cloudflarestorage.com', aws_access_key_id=values['R2_ACCESS_KEY_ID'],
                          aws_secret_access_key=values['R2_SECRET_ACCESS_KEY'], region_name='auto', config=Config(retries={'total_max_attempts': 3}, read_timeout=60, connect_timeout=10))
    publisher = runpy.run_path(str(Path(args.app_root) / 'scripts/ci/publish-staged-fixtures-bare.py'))
    if args.mode == 'apply' and read_object(client, DEST_BUCKET, MARKER_KEY) is not None:
        raise SystemExit('QA_STORE_RETIRE_REFUSED: the carry marker already exists; the migration was applied once and is not repeated')
    objects = list_all(client, SOURCE_BUCKET)
    listing_digest = sha256('\n'.join('%s %d %s' % (o['Key'], o['Size'], o.get('ETag', '')) for o in objects).encode())
    rows, app_data = build_rows(client, objects, publisher['_unpublishable_staged_reason'])
    marker = None
    if args.mode == 'apply':
        apply_rows(client, rows, publisher['_put_if_absent'])
        marker = carry_marker(rows, listing_digest, len(objects), app_data, SHARED_R2_FP, int(time.time()))
        if marker['complete']:
            marker['confirmed_no_live_qa_run'] = True
            if not publisher['_put_if_absent'](client, DEST_BUCKET, MARKER_KEY, json.dumps(marker, indent=2, sort_keys=True).encode(), 'application/json', {}):
                raise SystemExit('QA_STORE_RETIRE_REFUSED: the carry marker appeared concurrently')
    counts = {}
    for row in rows:
        counts.setdefault(row['class'], {})
        counts[row['class']][row['state']] = counts[row['class']].get(row['state'], 0) + 1
    report = {'schema': 'qa-store-retire-report/v1', 'mode': args.mode, 'at': int(time.time()), 'signer': SHARED_R2_FP, 'source_bucket': SOURCE_BUCKET,
              'dest_bucket': DEST_BUCKET, 'source_objects': len(objects), 'source_bytes': sum(o['Size'] for o in objects), 'listing_sha256': listing_digest,
              'counts': counts, 'app_data_not_copied': app_data, 'rows': public(rows), 'marker': marker}
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    print('QA_STORE_RETIRE mode=%s source_objects=%d counts=%s app_data_not_copied=%d complete=%s' % (
        args.mode, len(objects), json.dumps(counts, sort_keys=True), sum(v['objects'] for v in app_data.values()), marker['complete'] if marker else 'n/a'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
