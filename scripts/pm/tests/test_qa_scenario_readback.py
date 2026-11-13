"""qa-brief scenario runner: receipt readback and recording-clock markers (no browser, no subprocess).

Retro #13357/#13435; CTO C0ALZJHGE49 ts 1794578332.315349; Rajiv ts 1794580969.974139.
- Evidence is what the live browser READ BACK (tab count, URL, origin, identity fingerprint), not the brief's claim.
- A missing, malformed (including truthy non-object) or mismatched receipt is PARTIAL/UNKNOWN and never raises.
- Two tabs in one profile share one browser identity and do not prove a different-browser scenario.
- Marker timing is the controller's recording clock, else UNKNOWN with recording_clock=unavailable.
"""
import importlib.util
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

RUN = Path(__file__).parents[1] / 'shared-assets/claude/skills/qa-brief/scenarios/run.py'
spec = importlib.util.spec_from_file_location('qa_scenario_run', RUN)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

ROUTE = '/editor/t1'
URL = 'http://localhost:3000' + ROUTE
FULL_RECEIPT = {'target_route': ROUTE, 'target_context': 'http://localhost:3000',
                'storage_scope': 'same profile', 'browser_contexts': 1}
MISSING = object()


class FakeBox:
    """Stands in for subprocess.run: adapters, the agent-browser wrapper and its readback probes."""

    def __init__(self, url=URL, devices=('dev-a',), tabs=1, fail_probes=False):
        self.url, self.devices, self.tabs, self.fail_probes = url, list(devices), tabs, fail_probes
        self.evals = 0
        self.version = 0

    def __call__(self, argv, input=None, text=None, capture_output=None, timeout=None, check=None):
        class Result:
            stdout = ''
        out = Result()
        if argv[0] != 'bash':
            if argv[0].endswith('clone'):
                out.stdout = json.dumps({'transcriptId': 't1', 'editorUrl': ROUTE})
            else:
                self.version += 1
                out.stdout = json.dumps({
                    'editor_ready': True, 'tab_a_writer': True, 'tab_b_ready': True, 'tab_b_readonly': True,
                    'tab_b_writer': True, 'tab_a_fenced': True, 'sync_idle': True, 'head_version': self.version,
                    'server_head_contains_edit': True, 'server_lease_is_tab_b': True})
            return out
        args = argv[2:]
        if args[0] in ('get', '--json', 'eval'):
            if self.fail_probes:
                raise OSError('agent-browser unavailable')
            if args[0] == 'get':
                out.stdout = self.url + '\n'
            elif args[0] == '--json':
                out.stdout = json.dumps({'tabs': [{'index': i, 'active': i == self.tabs - 1} for i in range(self.tabs)]})
            else:
                device = self.devices[min(self.evals, len(self.devices) - 1)]
                self.evals += 1
                out.stdout = json.dumps(json.dumps({'origin': 'http://localhost:3000', 'device': device,
                                                     'subject': 'user-1', 'storage_keys': [mod.DEVICE_KEY]}))
        return out


def make_brief(tmp_path, scenario='clone-sample-open-editor', receipt=FULL_RECEIPT):
    for name in ('clone', 'observe'):
        (tmp_path / name).write_text('')
    params = {'owned_disposable_backend': True, 'sampleId': 's1', 'browser': 'ab.sh', 'edit': ' x',
              'editor_selector': '.editor',
              'adapters': {'clone_internal': str(tmp_path / 'clone'), 'observe': str(tmp_path / 'observe')}}
    if receipt is not MISSING:
        params['receipt'] = receipt
    return {'scenario': scenario, 'parameters': params}


def run_scenario(tmp_path, monkeypatch, box=None, **kwargs):
    monkeypatch.setenv('QA_RECORDING_DIR', str(tmp_path / 'qa'))
    with patch.object(mod.subprocess, 'run', box or FakeBox()), patch.object(mod.time, 'sleep'):
        return mod.run(make_brief(tmp_path, **kwargs))


# ---- receipt normalization -------------------------------------------------

@pytest.mark.parametrize('declared', ['all good', ['x'], 5, True, 0, '', []])
def test_non_object_receipt_is_malformed_never_raises(declared):
    receipt, unknown, malformed, whole = mod.normalize_receipt(declared)
    assert whole is True
    assert set(unknown) == set(mod.RECEIPT_FIELDS) and all(v == 'UNKNOWN' for v in receipt.values())


def test_missing_receipt_is_unknown_but_not_malformed():
    for declared in (None, {}):
        receipt, unknown, malformed, whole = mod.normalize_receipt(declared)
        assert whole is False and malformed == [] and set(unknown) == set(mod.RECEIPT_FIELDS)


def test_unusable_field_values_are_malformed_fields():
    receipt, unknown, malformed, whole = mod.normalize_receipt(
        {'target_route': '  ', 'target_context': True, 'storage_scope': [], 'browser_contexts': 2})
    assert receipt['browser_contexts'] == 2 and receipt['target_route'] == 'UNKNOWN'
    assert set(malformed) == {'target_route', 'target_context', 'storage_scope'} and whole is False


# ---- readback --------------------------------------------------------------

def test_read_state_records_url_tabs_origin_and_identity_fingerprint_only():
    answers = {('get', 'url'): URL + '\n', ('--json', 'tab'): json.dumps([{}, {}]),
               ('eval', mod.IDENTITY_JS): json.dumps(json.dumps({'origin': 'http://localhost:3000', 'device': 'dev-a'}))}
    state = mod.read_state(lambda *args: answers.get(args), 'tab_a_before_action', {'identity': 'user-1'})
    assert state['path'] == ROUTE and state['tab_count'] == 2 and state['origin'] == 'http://localhost:3000'
    assert state['device_id_sha256'] == mod.fingerprint('dev-a') != 'UNKNOWN'
    assert 'dev-a' not in json.dumps(state) and 'user-1' not in json.dumps(state)


def test_read_state_is_unknown_when_every_probe_fails():
    state = mod.read_state(lambda *args: None, 'x')
    assert [state[k] for k in ('url', 'path', 'origin', 'tab_count', 'device_id_sha256')] == ['UNKNOWN'] * 5


@pytest.mark.parametrize('garbage', ['', 'not json', 'null', '[]', '"{"', '123'])
def test_read_state_tolerates_garbage_probe_output(garbage):
    state = mod.read_state(lambda *args: garbage, 'x')
    assert state['device_id_sha256'] == 'UNKNOWN' and state['tab_count'] in ('UNKNOWN', 0)


# ---- end to end ------------------------------------------------------------

def test_matching_readback_is_complete_evidence(tmp_path, monkeypatch):
    out = run_scenario(tmp_path, monkeypatch)
    assert out['partial_evidence'] is False and out['partial_reasons'] == []
    assert out['receipt_status']['target_route'] == 'MATCH' and out['receipt_status']['target_context'] == 'MATCH'
    assert out['receipt_status']['browser_contexts'] == 'MATCH'
    assert out['readback']['before_action'][0]['label'] == 'tab_a_before_action'


@pytest.mark.parametrize('receipt', ['everything verified', ['ok'], 42, True])
def test_truthy_non_object_receipt_does_not_raise_and_is_partial(tmp_path, monkeypatch, receipt):
    out = run_scenario(tmp_path, monkeypatch, receipt=receipt)
    assert out['partial_evidence'] is True and 'receipt_malformed' in out['partial_reasons']
    assert set(out['unknown_fields']) >= set(mod.RECEIPT_FIELDS)


def test_missing_receipt_is_partial_with_unknown_fields(tmp_path, monkeypatch):
    out = run_scenario(tmp_path, monkeypatch, receipt=MISSING)
    assert out['partial_evidence'] is True
    assert {'receipt_field_missing:' + k for k in mod.RECEIPT_FIELDS} <= set(out['partial_reasons'])


def test_declared_route_that_the_browser_is_not_on_is_a_mismatch_not_a_pass(tmp_path, monkeypatch):
    out = run_scenario(tmp_path, monkeypatch, box=FakeBox(url='http://localhost:3000/dashboard'))
    assert out['receipt_status']['target_route'] == 'MISMATCH'
    assert out['partial_evidence'] is True and 'mismatch:target_route' in out['partial_reasons']


def test_readback_failure_is_unknown_never_an_exception(tmp_path, monkeypatch):
    out = run_scenario(tmp_path, monkeypatch, box=FakeBox(fail_probes=True))
    assert out['partial_evidence'] is True and 'readback_incomplete' in out['partial_reasons']
    assert out['receipt_status']['target_route'] == 'UNKNOWN'
    assert out['readback']['summary']['independent_browsers'] == 'UNKNOWN'


def test_two_tabs_in_one_profile_do_not_prove_a_different_browser(tmp_path, monkeypatch):
    receipt = {**FULL_RECEIPT, 'storage_scope': 'separate profile per context', 'browser_contexts': 'two different browsers'}
    out = run_scenario(tmp_path, monkeypatch, box=FakeBox(devices=('dev-a', 'dev-a'), tabs=2),
                       scenario='editor-takeover-two-tabs', receipt=receipt)
    assert out['partial_evidence'] is True and 'two_tabs_one_profile' in out['partial_reasons']
    assert out['readback']['summary'] == {'tabs_read': 2, 'distinct_browser_identities': 1, 'independent_browsers': False}
    assert out['receipt_status']['browser_contexts'] == 'MISMATCH' and out['receipt_status']['storage_scope'] == 'MISMATCH'
    assert [s['label'] for s in out['readback']['before_action']] == ['tab_a_before_action', 'tab_b_before_action']


def test_two_tabs_declared_as_one_profile_is_not_flagged(tmp_path, monkeypatch):
    receipt = {**FULL_RECEIPT, 'storage_scope': 'same profile, two tabs', 'browser_contexts': 'one browser, two tabs'}
    out = run_scenario(tmp_path, monkeypatch, box=FakeBox(devices=('dev-a', 'dev-a'), tabs=2),
                       scenario='editor-takeover-two-tabs', receipt=receipt)
    assert out['partial_evidence'] is False, out['partial_reasons']


def test_changing_device_storage_inside_two_tabs_does_not_prove_independent_browsers(tmp_path, monkeypatch):
    receipt = {**FULL_RECEIPT, 'storage_scope': 'separate profile', 'browser_contexts': 2}
    out = run_scenario(tmp_path, monkeypatch, box=FakeBox(devices=('dev-a', 'dev-b'), tabs=2),
                       scenario='editor-takeover-two-tabs', receipt=receipt)
    assert out['partial_evidence'] is True
    assert 'two_tabs_one_profile' in out['partial_reasons']
    assert out['readback']['summary']['independent_browsers'] is False


def test_raw_browser_identity_never_reaches_the_receipt(tmp_path, monkeypatch):
    out = run_scenario(tmp_path, monkeypatch, box=FakeBox(devices=('secret-device-id',)))
    assert 'secret-device-id' not in json.dumps(out)


def test_markers_use_live_tab_and_url_not_scripted_labels(tmp_path, monkeypatch):
    out = run_scenario(tmp_path, monkeypatch, box=FakeBox(url='http://localhost:3000/dashboard', tabs=2))
    markers = [e['marker'] for e in out['events'] if 'marker' in e]
    assert markers and all(m['tab'] == '1' for m in markers)
    assert all(m['route'] == 'http://localhost:3000/dashboard' for m in markers)
    assert all(m['outcome']['subject_sha256'] == mod.fingerprint('user-1') for m in markers)
    assert all(m['outcome_recording_s'] == 'UNKNOWN' for m in markers)


def test_untyped_or_nonfinite_receipt_fields_never_raise(tmp_path, monkeypatch):
    out = run_scenario(tmp_path, monkeypatch, receipt={
        'target_route': {'route': ROUTE}, 'target_context': ['context'],
        'storage_scope': 42, 'browser_contexts': float('inf')})
    assert out['partial_evidence'] is True
    assert set(out['unknown_fields']) == set(mod.RECEIPT_FIELDS)


def test_active_tab_is_unknown_without_an_explicit_selection():
    assert mod.active_tab({'data': {'tabs': [{'index': 0, 'active': True}]}}) == '0'
    assert mod.active_tab({'tabs': [{'index': 0}, {'index': 1}]}) == 'UNKNOWN'
    assert mod.active_tab(True) == 'UNKNOWN'


# ---- recording clock -------------------------------------------------------

def write_clock(tmp_path, started=None, stop=None):
    qa = tmp_path / 'qa'
    qa.mkdir(exist_ok=True)
    if started is not None:
        (qa / 'recording.started_ms').write_text(str(started) + '\n')
    if stop is not None:
        (qa / 'recording.stop_ms').write_text(str(stop) + '\n')


def test_without_a_recording_clock_marker_timing_is_unknown(tmp_path, monkeypatch):
    out = run_scenario(tmp_path, monkeypatch)
    assert out['recording_clock'] == 'unavailable'
    assert out['events'] and all(e['recording_s'] == 'UNKNOWN' for e in out['events'])
    assert all('_wall_ms' not in e for e in out['events'])
    assert all(isinstance(e['elapsed_s'], float) for e in out['events'])  # scenario clock stays, labeled as such


def test_with_a_recording_clock_markers_are_offsets_from_recording_start(tmp_path, monkeypatch):
    write_clock(tmp_path, started=int(time.time() * 1000) - 5000)
    out = run_scenario(tmp_path, monkeypatch)
    assert out['recording_clock'] == 'recording.started_ms'
    offsets = [e['recording_s'] for e in out['events']]
    assert all(isinstance(o, float) and 4.0 < o < 60 for o in offsets)


@pytest.mark.parametrize('started', ['', 'abc', '1794580969', '-1794580969000', '0', '1794580969000x'])
def test_malformed_recording_start_is_unavailable(tmp_path, started):
    write_clock(tmp_path, started=started)
    assert mod.recording_window(tmp_path / 'qa', int(time.time() * 1000)) is None


def test_future_recording_start_is_unavailable(tmp_path):
    now = int(time.time() * 1000)
    write_clock(tmp_path, started=now + 600000)
    assert mod.recording_window(tmp_path / 'qa', now) is None


def test_stop_before_start_or_malformed_stop_is_unavailable(tmp_path):
    now = int(time.time() * 1000)
    write_clock(tmp_path, started=now - 5000, stop=now - 9000)
    assert mod.recording_window(tmp_path / 'qa', now) is None
    write_clock(tmp_path, started=now - 5000, stop='soon')
    assert mod.recording_window(tmp_path / 'qa', now) is None


def test_missing_start_file_is_unavailable_even_when_a_stop_file_exists(tmp_path):
    now = int(time.time() * 1000)
    write_clock(tmp_path, stop=now - 100)
    assert mod.recording_window(tmp_path / 'qa', now) is None


def test_event_after_stop_or_before_start_is_not_mislabeled():
    events = [{'_wall_ms': 999_000}, {'_wall_ms': 1_002_000}, {'_wall_ms': 1_010_000}, {}]
    clock = mod.bind_recording_clock(events, (1_000_000, 1_005_000))
    assert clock == 'recording.started_ms'
    assert [e['recording_s'] for e in events] == [-1.0, 2.0, 'UNKNOWN', 'UNKNOWN']
