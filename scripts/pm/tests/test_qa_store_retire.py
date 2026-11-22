"""Retirement of the separate QA R2 bucket: the one-time store migration and the custody carry (added to the repo; not run as a gate, Rajiv ts 1793983042.752819).

Rajiv D0AMF0XE6TS thread 1795294139.951329, ts 1795316993.447349; rule 65. In-memory fakes only: no network, no R2, no Modal, no provider.
Binds the migration's key mapping to the live custody keys of remote-qa-modal.py (exact mode), so the two cannot drift apart.
"""
import importlib.util
import io
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


M = _load('qa_store_retire', ROOT / 'migrations/qa-store-retire.py')
H = _load('qa_modal_controller', ROOT / 'shared-assets/claude/scripts/remote-qa-modal.py')


class _Missing(Exception):
    def __init__(self):
        self.response = {'Error': {'Code': 'NoSuchKey'}}


class FakeS3:
    def __init__(self):
        self.buckets = {M.SOURCE_BUCKET: {}, M.DEST_BUCKET: {}}
        self.writes = []

    def seed(self, bucket, key, body, content_type='application/json', metadata=None):
        self.buckets[bucket][key] = (body, content_type, dict(metadata or {}))

    def list_objects_v2(self, Bucket, MaxKeys=1000, ContinuationToken=None):
        return {'Contents': [{'Key': k, 'Size': len(v[0]), 'ETag': 'e'} for k, v in sorted(self.buckets[Bucket].items())], 'IsTruncated': False}

    def get_object(self, Bucket, Key):
        if Key not in self.buckets[Bucket]:
            raise _Missing()
        body, content_type, metadata = self.buckets[Bucket][Key]
        return {'Body': io.BytesIO(body), 'ContentType': content_type, 'Metadata': metadata}


def put_if_absent(client, bucket, key, body, content_type, metadata):
    if key in client.buckets[bucket]:
        return False
    client.writes.append((bucket, key))
    client.seed(bucket, key, body, content_type, metadata)
    return True


def ledger(attempts=None, requests=None, halted=False):
    return json.dumps({'run_id': 'x', 'attempts': attempts or {}, 'requests': requests or {}, 'halted': halted}).encode()


FIXTURE = M.FIXTURE_PREFIX + 'gemini_vertex/h/model.generate/' + 'a' * 64 + '.bin'
BOX = M.LEDGER_SRC + 'onmiss/box-0123456789abcdef.json'
RUN = M.LEDGER_SRC + 'remote-qa-0123456789ab.json'


def source_store():
    s3 = FakeS3()
    s3.seed(M.SOURCE_BUCKET, FIXTURE, b'{"ok":true}', 'application/octet-stream', {'response-status': '200'})
    s3.seed(M.SOURCE_BUCKET, BOX, ledger({'t1': {'charged_bound_usd': 1.0, 'settled': True}, 't2': {'charged_bound_usd': 0.7, 'settled': False}},
                                          {'k': {'state': 'unknown'}}))
    s3.seed(M.SOURCE_BUCKET, RUN, ledger())
    s3.seed(M.SOURCE_BUCKET, 'projects/p1/audio.mp3', b'x' * 10)
    return s3


def test_classification_and_ledger_mapping_match_the_live_custody_keys():
    assert M.classify(FIXTURE) == ('fixture', FIXTURE)
    assert M.classify(M.STAGING_PREFIX + 'run/a.bin') == ('staging', M.STAGING_PREFIX + 'run/a.bin')
    assert M.classify('projects/x') == ('app_data', None) and M.classify('practice/x') == ('app_data', None)
    keys = H.onmiss_keys('rtbl-aaaaaaaaaaaaaaaa-111111', 'rtb-pool-07-115933', 'a' * 40, exact=True)
    legacy = H.onmiss_keys('rtbl-aaaaaaaaaaaaaaaa-111111', 'rtb-pool-07-115933', 'a' * 40)
    for field in ('box_key', 'attempt_key', 'run_key'):
        assert M.classify(legacy[field]) == ('ledger', keys[field])
    assert M.LEDGER_DST == H.QA_LEDGER_PREFIX and M.MARKER_KEY == H.QA_STORE_CARRY_KEY and M.SHARED_R2_FP == H.SHARED_R2_FP


def test_dry_run_changes_nothing_and_reports_carried_exposure():
    s3 = source_store()
    rows, app_data = M.build_rows(s3, M.list_all(s3, M.SOURCE_BUCKET))
    assert s3.writes == [] and not s3.buckets[M.DEST_BUCKET]
    states = {r['source_key']: r['state'] for r in rows}
    assert states == {FIXTURE: 'would_copy', BOX: 'would_copy', RUN: 'would_copy'}
    box = next(r for r in rows if r['source_key'] == BOX)
    assert box['dest_key'] == M.LEDGER_DST + 'onmiss/box-0123456789abcdef.json'
    assert box['ledger'] == {'readable': True, 'attempts': 2, 'unsettled_attempts': 1, 'committed_usd': 1.7, 'request_states': {'unknown': 1}, 'halted': False}
    assert app_data == {'projects': {'objects': 1, 'bytes': 10}}


def test_apply_is_put_if_absent_digest_verified_and_never_overwrites_or_touches_the_source():
    s3 = source_store()
    s3.seed(M.DEST_BUCKET, FIXTURE, b'{"ok":true}', 'application/octet-stream')           # already present, identical
    s3.seed(M.DEST_BUCKET, M.LEDGER_DST + 'remote-qa-0123456789ab.json', b'DIFFERENT')    # present, different bytes
    before_source = dict(s3.buckets[M.SOURCE_BUCKET])
    rows, _ = M.build_rows(s3, M.list_all(s3, M.SOURCE_BUCKET))
    M.apply_rows(s3, rows, put_if_absent)
    states = {r['source_key']: r['state'] for r in rows}
    assert states == {FIXTURE: 'identical', BOX: 'copied', RUN: 'conflict'}
    assert s3.writes == [(M.DEST_BUCKET, M.LEDGER_DST + 'onmiss/box-0123456789abcdef.json')]
    assert s3.buckets[M.DEST_BUCKET][M.LEDGER_DST + 'remote-qa-0123456789ab.json'][0] == b'DIFFERENT'
    assert s3.buckets[M.SOURCE_BUCKET] == before_source
    assert 'delete_object' not in (ROOT / 'migrations/qa-store-retire.py').read_text()


def test_unpublishable_fixture_is_skipped_by_the_existing_guard_and_never_copied():
    s3 = source_store()
    rows, _ = M.build_rows(s3, M.list_all(s3, M.SOURCE_BUCKET), lambda body, metadata: 'non_2xx_status_429')
    row = next(r for r in rows if r['source_key'] == FIXTURE)
    assert row['state'] == 'skipped_unpublishable' and row['reason'] == 'non_2xx_status_429'
    M.apply_rows(s3, rows, put_if_absent)
    assert FIXTURE not in s3.buckets[M.DEST_BUCKET]


def test_marker_is_complete_only_when_every_ledger_was_carried_and_custody_accepts_it():
    s3 = source_store()
    rows, app_data = M.build_rows(s3, M.list_all(s3, M.SOURCE_BUCKET))
    M.apply_rows(s3, rows, put_if_absent)
    marker = M.carry_marker(rows, 'd' * 64, 4, app_data, M.SHARED_R2_FP, 1)
    assert marker['complete'] is True and marker['dest_prefix'] == H.QA_LEDGER_PREFIX
    carried = next(l for l in marker['ledgers'] if l['source_key'] == BOX)
    assert carried['committed_usd'] == 1.7 and carried['unsettled_attempts'] == 1 and carried['request_states'] == {'unknown': 1}
    assert marker['app_data_not_copied'] == {'projects': {'objects': 1, 'bytes': 10}} and 'retained intact' in marker['source']['status']
    assert H._carry_view(json.dumps(marker).encode())['state'] == 'complete'
    # a ledger conflict, or a copy error, leaves the marker incomplete and custody refuses
    s3b = source_store()
    s3b.seed(M.DEST_BUCKET, M.LEDGER_DST + 'remote-qa-0123456789ab.json', b'DIFFERENT')
    rows_b, app_b = M.build_rows(s3b, M.list_all(s3b, M.SOURCE_BUCKET))
    M.apply_rows(s3b, rows_b, put_if_absent)
    incomplete = M.carry_marker(rows_b, 'd' * 64, 4, app_b, M.SHARED_R2_FP, 1)
    assert incomplete['complete'] is False and H._carry_view(json.dumps(incomplete).encode())['state'] == 'invalid'
    assert H._carry_view(None)['state'] == 'absent'
