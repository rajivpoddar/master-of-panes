#!/usr/bin/env python3
"""Run on the owned remote QA box after AGENT_BROWSER_READY; never on host.
Adapters are reviewed, exact-head scripts, not shell snippets or LLM calls.

Evidence contract (retro #13357/#13435, CTO C0ALZJHGE49 ts 1794578332.315349, Rajiv ts 1794580969.974139):
- The receipt records what the brief DECLARED next to what the live browser READ BACK
  (tab count, URL, origin, a fingerprint of the browser identity) before the first action.
  Evidence is the readback, never the declaration.
- A missing, malformed or mismatched receipt makes the run PARTIAL with UNKNOWN fields. It never raises
  and never refuses the run.
- Two tabs in one profile share one browser identity, so they never prove a different-browser scenario.
- Marker timing is bound to the capture controller's recording clock (`recording.started_ms`).
  Without a valid clock, marker timing is UNKNOWN and `recording_clock` is `unavailable`.
  `elapsed_s` is the scenario clock and is never recording time.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit

# A field the brief omits or malforms is recorded as UNKNOWN and the run is labeled partial evidence.
RECEIPT_FIELDS = ('target_route', 'target_context', 'storage_scope', 'browser_contexts')
UNKNOWN = 'UNKNOWN'
DEVICE_KEY = 'heydonna-device-id'
IDENTITY_JS = ("JSON.stringify({origin: location.origin, "
               "device: localStorage.getItem('%s'), "
               "subject: window.Clerk?.user?.id || null, "
               "storage_keys: Object.keys(localStorage).sort()})" % DEVICE_KEY)
CLAIMS_DIFFERENT = re.compile(r'different|separate|independent|distinct|two[ -]?browsers?|other browser', re.I)
CLAIMS_SAME = re.compile(r'same[ -]?(profile|browser|storage|session)|shared', re.I)


def usable(value):
    """A declared receipt value that carries information."""
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (int, float)):
        return True
    return isinstance(value, (list, dict)) and bool(value)


def normalize_receipt(declared):
    """Declared brief receipt -> (receipt, unknown_fields, malformed_fields, receipt_malformed). Never raises.

    A non-object receipt (a string, a list, a number, `true`) is malformed as a whole, even when truthy.
    """
    whole_bad = declared is not None and not isinstance(declared, dict)
    source = declared if isinstance(declared, dict) else {}
    receipt, unknown, malformed = {}, [], []
    for key in RECEIPT_FIELDS:
        value = source.get(key)
        valid_type = (isinstance(value, str) if key != 'browser_contexts' else
                      isinstance(value, (str, list)) or (type(value) is int and value > 0))
        if usable(value) and valid_type:
            receipt[key] = value
        else:
            receipt[key] = UNKNOWN
            unknown.append(key)
            if key in source and value is not None:
                malformed.append(key)
    return receipt, unknown, malformed, whole_bad


def fingerprint(value):
    """Short digest of a browser identity. The raw id is never written to the receipt."""
    if not isinstance(value, str) or not value:
        return UNKNOWN
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def unwrap_json(text):
    """agent-browser prints eval results JSON-encoded, sometimes twice. Returns the object or None."""
    value = text
    for _ in range(3):
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value


def count_tabs(payload):
    """`agent-browser --json tab` shapes seen: a list, {'tabs': [...]}, {'data': {'tabs': [...]}}."""
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        if isinstance(payload.get('tabs'), list):
            return len(payload['tabs'])
        return count_tabs(payload.get('data'))
    return None


def path_of(url):
    try:
        return urlsplit(str(url)).path.rstrip('/') or '/'
    except ValueError:
        return None


def host_of(url):
    try:
        return urlsplit(str(url)).netloc.lower() or None
    except ValueError:
        return None


def active_tab(payload):
    """Only an explicit active/current flag identifies the selected tab; never infer it from a script label."""
    if isinstance(payload, dict):
        if 'data' in payload:
            return active_tab(payload['data'])
        payload = payload.get('tabs')
    if isinstance(payload, list):
        selected = [tab for tab in payload if isinstance(tab, dict) and
                    (tab.get('active') is True or tab.get('current') is True)]
        if len(selected) == 1:
            tab = selected[0]
            return str(tab.get('index', tab.get('id', UNKNOWN)))
    return UNKNOWN


def read_state(probe, label, observed=None):
    """Read back the live tab before acting. `probe(*args)` returns stdout or None. Never raises."""
    state = {'label': label, 'url': UNKNOWN, 'path': UNKNOWN, 'origin': UNKNOWN,
             'tab_count': UNKNOWN, 'active_tab': UNKNOWN, 'storage_keys': UNKNOWN,
             'subject_sha256': UNKNOWN, 'device_id_sha256': UNKNOWN, 'observe_identity_sha256': UNKNOWN}
    url = probe('get', 'url')
    if isinstance(url, str) and url.strip():
        state['url'] = url.strip()
        state['path'] = path_of(state['url']) or UNKNOWN
    tab_payload = unwrap_json(probe('--json', 'tab'))
    tabs = count_tabs(tab_payload)
    state['active_tab'] = active_tab(tab_payload)
    if tabs is not None:
        state['tab_count'] = tabs
    identity = unwrap_json(probe('eval', IDENTITY_JS))
    if isinstance(identity, dict):
        if isinstance(identity.get('origin'), str) and identity['origin']:
            state['origin'] = identity['origin']
        state['device_id_sha256'] = fingerprint(identity.get('device'))
        state['subject_sha256'] = fingerprint(identity.get('subject'))
        keys = identity.get('storage_keys')
        if isinstance(keys, list) and all(isinstance(k, str) for k in keys):
            # Names only: never copy tokens or local transcript values into evidence.
            state['storage_keys'] = keys
    if isinstance(observed, dict):
        state['observe_identity_sha256'] = fingerprint(observed.get('identity'))
    return state


def combine(statuses):
    if 'MISMATCH' in statuses:
        return 'MISMATCH'
    if UNKNOWN in statuses:
        return UNKNOWN
    return 'MATCH'


def required_identities(value):
    """How many independent browser identities a declared value claims, or None when it makes no claim."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, list):
        return len(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
        if CLAIMS_DIFFERENT.search(text):
            return 2
        if re.search(r'\bone browser\b', text, re.I):
            return 1
    return None


def assess(receipt, readbacks, two_tabs):
    """Compare the declared receipt with what was read back. Returns (field_status, partial_reasons, summary)."""
    reasons = []
    identities = {s['device_id_sha256'] for s in readbacks if s['device_id_sha256'] != UNKNOWN}
    identity_unread = any(s['device_id_sha256'] == UNKNOWN for s in readbacks)
    status = {}

    declared = receipt['target_route']
    if declared == UNKNOWN or not isinstance(declared, str) or path_of(declared) is None:
        status['target_route'] = UNKNOWN
    else:
        want = path_of(declared)
        status['target_route'] = combine(
            [UNKNOWN if s['path'] == UNKNOWN else ('MATCH' if s['path'] == want else 'MISMATCH') for s in readbacks]
            or [UNKNOWN])

    declared = receipt['target_context']
    if declared == UNKNOWN or not isinstance(declared, str):
        status['target_context'] = UNKNOWN
    elif host_of(declared):
        want = host_of(declared)
        status['target_context'] = combine(
            [UNKNOWN if s['origin'] == UNKNOWN else ('MATCH' if host_of(s['origin']) == want else 'MISMATCH')
             for s in readbacks] or [UNKNOWN])
    else:
        status['target_context'] = UNKNOWN  # free text cannot prove the declared environment

    needed = max([n for n in (required_identities(receipt['browser_contexts']),
                              required_identities(receipt['storage_scope'])) if n is not None] or [0])
    for key in ('storage_scope', 'browser_contexts'):
        declared = receipt[key]
        if declared == UNKNOWN:
            status[key] = UNKNOWN
            continue
        claim = required_identities(declared)
        says_same = isinstance(declared, str) and bool(CLAIMS_SAME.search(declared))
        if claim is None and not says_same:
            status[key] = UNKNOWN
        elif identity_unread or not readbacks:
            status[key] = UNKNOWN
        elif claim is not None and (len(identities) < claim or (two_tabs and claim >= 2)):
            status[key] = 'MISMATCH'
        elif says_same and len(identities) > 1:
            status[key] = 'MISMATCH'
        else:
            status[key] = 'MATCH'

    if needed >= 2 and (two_tabs or len(identities) < needed) and not identity_unread and readbacks:
        reasons.append('two_tabs_one_profile' if two_tabs else 'independent_browsers_not_proven')
    for key, value in status.items():
        # A field the brief never declared is reported once, as receipt_field_missing/malformed, by run().
        if value == 'MISMATCH' or (value == UNKNOWN and receipt[key] != UNKNOWN):
            reasons.append(('mismatch:' if value == 'MISMATCH' else 'unknown:') + key)
    if not readbacks or identity_unread or any(
            any(s.get(k, UNKNOWN) == UNKNOWN for k in ('url', 'active_tab', 'storage_keys', 'subject_sha256'))
            for s in readbacks):
        reasons.append('readback_incomplete')
    subjects = {s.get('subject_sha256', UNKNOWN) for s in readbacks} - {UNKNOWN}
    if two_tabs and len(subjects) > 1:
        reasons.append('mismatch:same_user_identity')
    summary = {'tabs_read': len(readbacks), 'distinct_browser_identities': len(identities) if not identity_unread else UNKNOWN,
               'independent_browsers': (not two_tabs and len(identities) >= 2) if not identity_unread and readbacks else UNKNOWN}
    return status, sorted(set(reasons)), summary


def recording_window(qa_dir, now_ms):
    """(started_ms, stop_ms) from the capture controller's recording clock, or None when unavailable."""
    def number(name):
        try:
            text = (Path(qa_dir) / name).read_text().strip()
        except OSError:
            return None
        # 13 digits = epoch milliseconds; a seconds value or any other text is malformed, never rescaled.
        return int(text) if re.fullmatch(r'[0-9]{13}', text) else False

    started = number('recording.started_ms')
    if not started or started > now_ms + 2000:  # missing, malformed or in the future
        return None
    stop = number('recording.stop_ms')
    if stop is False or (stop is not None and stop < started):
        return None
    return started, stop


def bind_recording_clock(events, window):
    """Replace each event's wall stamp with a recording offset in seconds, or UNKNOWN. Negative = before recording."""
    for event in events:
        wall_ms = event.pop('_wall_ms', None)
        if window is None or wall_ms is None or (window[1] is not None and wall_ms > window[1]):
            event['recording_s'] = UNKNOWN
        else:
            event['recording_s'] = round((wall_ms - window[0]) / 1000, 3)
        marker = event.get('marker')
        if isinstance(marker, dict):
            outcome_ms = marker.pop('outcome_wall_ms', None)
            marker['outcome_recording_s'] = (
                round((outcome_ms - window[0]) / 1000, 3)
                if window is not None and outcome_ms is not None and
                (window[1] is None or outcome_ms <= window[1]) else UNKNOWN)
    return 'unavailable' if window is None else 'recording.started_ms'


def run(brief):
    name = brief['scenario']
    if name not in ('clone-sample-open-editor', 'editor-takeover-two-tabs'):
        raise ValueError('SCENARIO_NOT_FOUND: log a scenario-miss lesson')
    p = brief['parameters']
    if p.get('owned_disposable_backend') is not True:
        raise ValueError('OWNED_DISPOSABLE_BACKEND_REQUIRED')
    receipt, unknown, malformed, receipt_malformed = normalize_receipt(p.get('receipt'))
    # All adapters receive JSON via stdin; credentials never go in argv.
    adapters = p['adapters']
    for key in ('clone_internal', 'observe'):
        if not Path(adapters[key]).is_absolute() or not Path(adapters[key]).is_file():
            raise ValueError('SCENARIO_BINDING_MISSING: ' + key)
    browser = p.get('browser', '/root/qa/agent-browser.sh')
    started = time.monotonic()
    events = []
    readbacks = []

    def command(argv, payload=None, marker=None):
        actual = read_state(probe, 'marker_before_action') if marker else None
        action_ms = int(time.time() * 1000)
        t = time.monotonic()
        result = subprocess.run(argv, input=json.dumps(payload) if payload is not None else None,
                                text=True, capture_output=True, timeout=45, check=True)
        event = {'elapsed_s': round(time.monotonic()-started, 3),  # scenario clock, NOT recording time
                 'duration_s': round(time.monotonic()-t, 3), 'operation': argv[0],
                 '_wall_ms': int(time.time() * 1000)}
        if marker:
            # Target markers (context/tab, route, action) let qa-video-review map each step to the clip.
            event['_wall_ms'] = action_ms
            event['marker'] = {'action': marker['action'], 'declared_tab': marker.get('tab'),
                               'tab': actual['active_tab'], 'route': actual['url'],
                               'before_action': actual,
                               'outcome': read_state(probe, 'marker_after_action'),
                               'outcome_wall_ms': int(time.time() * 1000)}
        events.append(event)
        return result.stdout

    def ab(*args, marker=None):
        return command(['bash', browser, *args], marker=marker)

    def probe(*args):
        """Readback is evidence: a failed read is UNKNOWN, never a refusal and never an exception."""
        try:
            return subprocess.run(['bash', browser, *args], text=True, capture_output=True,
                                  timeout=20, check=True).stdout
        except Exception:
            return None

    def observe():
        return json.loads(command([adapters['observe']], {'transcriptId': clone['transcriptId'], 'edit': p.get('edit', '')}))

    def wait(predicate):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            state = observe()
            if predicate(state):
                return state
            time.sleep(1)
        raise TimeoutError('SCENARIO_STATE_TIMEOUT')

    clone = json.loads(command([adapters['clone_internal']], {'sampleId': p['sampleId']}))
    # clone_internal must use an existing internal mutation on the owned deployment.
    # It returns transcriptId/editorUrl, never a fabricated DOM or injected editor.
    route = clone['editorUrl']
    ab('open', route, marker={'tab': 'A', 'route': route, 'action': 'open editor'})
    ab('set', 'viewport', '1440', '900')
    state = wait(lambda s: s['editor_ready'] and s['tab_a_writer'])
    readbacks.append(read_state(probe, 'tab_a_before_action', state))
    ab('screenshot', '/root/qa/out/01-editor-a.png', marker={'tab': 'A', 'route': route, 'action': 'screenshot editor A'})
    if name == 'editor-takeover-two-tabs':
        # A real second tab in the SAME session/profile; never another profile.
        ab('tab', 'new', route, marker={'tab': 'B', 'route': route, 'action': 'open second tab'})
        state = wait(lambda s: s['tab_b_ready'] and s['tab_b_readonly'])
        readbacks.append(read_state(probe, 'tab_b_before_action', state))
        ab('find', 'role', 'button', 'click', '--name', 'Continue editing here',
           marker={'tab': 'B', 'route': route, 'action': 'click Continue editing here'})
        # Explicit same-user move confirmation, if the real UI requires it.
        if p.get('confirm_button'):
            ab('find', 'role', 'button', 'click', '--name', p['confirm_button'],
               marker={'tab': 'B', 'route': route, 'action': 'confirm ' + p['confirm_button']})
        before = wait(lambda s: s['tab_b_writer'] and s['tab_a_fenced'])
        ab('click', p['editor_selector'])
        ab('press', 'End')
        ab('keyboard', 'type', p['edit'], marker={'tab': 'B', 'route': route, 'action': 'type edit'})
        ab('press', p.get('save_shortcut', 'Control+s'), marker={'tab': 'B', 'route': route, 'action': 'save'})
        final = wait(lambda s: s['sync_idle'] and s['head_version'] != before['head_version'])
        # Only authoritative server readback proves persistence/lease ownership.
        if not (final['server_head_contains_edit'] and final['server_lease_is_tab_b']):
            raise AssertionError('SERVER_READBACK_FAILED')
        ab('screenshot', '/root/qa/out/02-editor-b-saved.png',
           marker={'tab': 'B', 'route': route, 'action': 'screenshot editor B saved'})
    field_status, reasons, readback_summary = assess(receipt, readbacks, name == 'editor-takeover-two-tabs')
    if receipt_malformed:
        reasons.append('receipt_malformed')
    reasons += ['receipt_field_malformed:' + key for key in malformed]
    reasons += ['receipt_field_missing:' + key for key in unknown if key not in malformed]
    clock = bind_recording_clock(events, recording_window(os.environ.get('QA_RECORDING_DIR', '/root/qa'),
                                                          int(time.time() * 1000)))
    reasons = sorted(set(reasons))
    return {'scenario': name, 'receipt': receipt, 'receipt_status': field_status,
            'readback': {'summary': readback_summary, 'before_action': readbacks},
            'partial_evidence': bool(reasons), 'partial_reasons': reasons,
            'unknown_fields': sorted(set(unknown) | {k for k, v in field_status.items() if v == UNKNOWN}),
            'recording_clock': clock, 'wall_s': round(time.monotonic()-started, 3), 'events': events}


if __name__ == '__main__':
    output = Path(os.environ.get('QA_SCENARIO_RECEIPT', '/root/qa/out/scenario.json'))
    try:
        receipt = run(json.loads(Path(sys.argv[1]).read_text()))
        output.write_text(json.dumps(receipt, indent=2)+'\n')
    except Exception as exc:
        output.write_text(json.dumps({'verdict': 'blocked', 'reason': str(exc)})+'\n')
        raise
