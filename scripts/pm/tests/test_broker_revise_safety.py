"""CTO REVISE fixtures; no real processes are signalled."""
import copy
from unittest.mock import patch

from test_codex_broker_cap_policy import MODULE, UID, broker, proc


def root():
    parent = proc(100, 1, broker('safe', '/a'))
    child = proc(101, 100, 'node /opt/homebrew/bin/codex app-server')
    return {'root': parent, 'shape': 'broker', 'rules': ['a_orphan_ppid1', 'c_idle_no_client'],
            'members': [parent, child]}


def test_disconnected_client_does_not_establish_quiescence():
    r = root()
    assert MODULE.codex_broker_strict_eligible(r, UID, [])[1] == 'backend_quiescence_unknown'
    sent = []
    out = MODULE.codex_broker_strict_kill(r, 0, send=lambda *args: sent.append(args))
    assert out['outcome'] == 'refused_backend_quiescence_unknown'
    assert sent == []


def test_sample_resets_for_membership_and_identity_changes():
    r = root()
    history = {}
    with patch.object(MODULE, 'codex_broker_strict_eligible', return_value=(True, 'fixture_quiescent')):
        MODULE.codex_broker_sample([r], history, True, UID, [], 100)
        for variant in ('new_child', 'pid_reused', 'reparented'):
            changed = copy.deepcopy(r)
            if variant == 'new_child':
                changed['members'].append(proc(102, 100, 'new descendant'))
            elif variant == 'pid_reused':
                changed['members'][1]['start_time'] = 'new-start'
            else:
                changed['members'][1]['ppid'] = 1
            previous = copy.deepcopy(history)
            ready, _ = MODULE.codex_broker_sample([changed], previous, True, UID, [], 2000)
            assert ready == []
            assert previous['codex_broker_idle']['100:t100']['first'] == 2000


def test_unknown_observation_erases_prior_credit():
    r = root()
    history = {'codex_broker_idle': {'100:t100': {'first': 1, 'members': []}}}
    ready, refused = MODULE.codex_broker_sample([r], history, True, UID, [], 2000)
    assert ready == []
    assert refused[0]['reason'] == 'backend_quiescence_unknown'
    assert history['codex_broker_idle'] == {}
