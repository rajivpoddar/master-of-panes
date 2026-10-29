"""Exercise the installed hook transport's actual Python body, not a copied protocol."""
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).parents[2] / 'hook-relay.sh'


def relay(event, reply=None, fail=False, slot=0, **extra):
    code = SCRIPT.read_text().split("python3 -c '\n", 1)[1].rsplit("\n' \"$HOOK_TYPE\"", 1)[0]
    payload = {'cwd': '/Users/rajiv/Downloads/projects/heydonna-app' + (f'-300{slot}' if slot else ''),
               'session_id': 'pm', 'tool_use_id': 'tool-1', **extra}
    requests = []
    output = io.StringIO()

    def send(request, **kwargs):
        requests.append(json.loads(request.data))
        if fail:
            raise TimeoutError('response lost')
        return io.BytesIO(json.dumps(reply or {}).encode())

    with patch.object(sys, 'argv', ['relay', event]), patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), \
         patch.object(sys, 'stdout', output), patch('urllib.request.urlopen', side_effect=send):
        exec(compile(code, str(SCRIPT), 'exec'), {'__name__': '__main__'})
    return json.loads(output.getvalue()) if output.getvalue().strip() else None, requests


def test_pm_admission_requires_positive_server_acknowledgement():
    response, sent = relay('PreToolUse', {'mop_tool_admitted': True})
    assert response == {}
    assert sent[0]['tool_use_id'] == 'tool-1' and sent[0]['mop_tool_tracking'] == 1


@pytest.mark.parametrize('fail', [False, True])
def test_missing_ack_or_network_error_denies_pm_tool(fail):
    response, _ = relay('PreToolUse', {}, fail=fail)
    assert response['hookSpecificOutput']['permissionDecision'] == 'deny'


def test_maintenance_denial_and_retained_prompt_are_returned_to_claude():
    denial = {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                    'permissionDecisionReason': 'maintenance'}}
    assert relay('PreToolUse', denial)[0] == denial
    prompt = {'decision': 'block', 'reason': 'retained for replay'}
    assert relay('UserPromptSubmit', prompt, prompt='inbound')[0] == prompt


def test_failed_completion_preserves_tool_and_session_identity():
    response, sent = relay('PostToolUseFailure')
    assert response is None
    assert sent[0]['hook_event_name'] == 'PostToolUseFailure'
    assert sent[0]['tool_use_id'] == 'tool-1' and sent[0]['session_id'] == 'pm'


def test_dev_hooks_keep_existing_nonblocking_transport():
    assert relay('PreToolUse', {}, fail=True, slot=2)[0] is None
