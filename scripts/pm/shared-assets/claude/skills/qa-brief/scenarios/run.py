#!/usr/bin/env python3
"""Run on the owned remote QA box after AGENT_BROWSER_READY; never on host.
Adapters are reviewed, exact-head scripts, not shell snippets or LLM calls.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def run(brief):
    name = brief['scenario']
    if name not in ('clone-sample-open-editor', 'editor-takeover-two-tabs'):
        raise ValueError('SCENARIO_NOT_FOUND: log a scenario-miss lesson')
    p = brief['parameters']
    if p.get('owned_disposable_backend') is not True:
        raise ValueError('OWNED_DISPOSABLE_BACKEND_REQUIRED')
    # All adapters receive JSON via stdin; credentials never go in argv.
    adapters = p['adapters']
    for key in ('clone_internal', 'observe'):
        if not Path(adapters[key]).is_absolute() or not Path(adapters[key]).is_file():
            raise ValueError('SCENARIO_BINDING_MISSING: ' + key)
    browser = p.get('browser', '/root/qa/agent-browser.sh')
    started = time.monotonic()
    events = []

    def command(argv, payload=None):
        t = time.monotonic()
        result = subprocess.run(argv, input=json.dumps(payload) if payload is not None else None,
                                text=True, capture_output=True, timeout=45, check=True)
        events.append({'elapsed_s': round(time.monotonic()-started, 3),
                       'duration_s': round(time.monotonic()-t, 3), 'operation': argv[0]})
        return result.stdout

    def ab(*args):
        return command(['bash', browser, *args])

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
    ab('open', clone['editorUrl'])
    ab('set', 'viewport', '1440', '900')
    wait(lambda s: s['editor_ready'] and s['tab_a_writer'])
    ab('screenshot', '/root/qa/out/01-editor-a.png')
    if name == 'editor-takeover-two-tabs':
        # A real second tab in the SAME session/profile; never another profile.
        ab('tab', 'new', clone['editorUrl'])
        wait(lambda s: s['tab_b_ready'] and s['tab_b_readonly'])
        ab('find', 'role', 'button', 'click', '--name', 'Continue editing here')
        # Explicit same-user move confirmation, if the real UI requires it.
        if p.get('confirm_button'):
            ab('find', 'role', 'button', 'click', '--name', p['confirm_button'])
        before = wait(lambda s: s['tab_b_writer'] and s['tab_a_fenced'])
        ab('click', p['editor_selector'])
        ab('press', 'End')
        ab('keyboard', 'type', p['edit'])
        ab('press', p.get('save_shortcut', 'Control+s'))
        final = wait(lambda s: s['sync_idle'] and s['head_version'] != before['head_version'])
        # Only authoritative server readback proves persistence/lease ownership.
        if not (final['server_head_contains_edit'] and final['server_lease_is_tab_b']):
            raise AssertionError('SERVER_READBACK_FAILED')
        ab('screenshot', '/root/qa/out/02-editor-b-saved.png')
    return {'scenario': name, 'wall_s': round(time.monotonic()-started, 3), 'events': events}


if __name__ == '__main__':
    output = Path(os.environ.get('QA_SCENARIO_RECEIPT', '/root/qa/out/scenario.json'))
    try:
        receipt = run(json.loads(Path(sys.argv[1]).read_text()))
        output.write_text(json.dumps(receipt, indent=2)+'\n')
    except Exception as exc:
        output.write_text(json.dumps({'verdict': 'blocked', 'reason': str(exc)})+'\n')
        raise
