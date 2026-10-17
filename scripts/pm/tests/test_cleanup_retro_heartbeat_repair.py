"""Hermetic regressions for PM event1792200646.277489; no real cleanup/heartbeat."""
import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

ASSETS = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ASSETS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cleanup = load('pm-cleanup-pr')


def request(**kwargs):
    return {"repository": "fixture/repository", "pr": 10126, "issue": 10122,
            "head": "a"*40, "merge_commit": "b"*40, "thread_reply": False, **kwargs}


class External:
    def __init__(self, *, lose_add=False):
        self.pr = {"number": 10126, "state": "MERGED", "headRefOid": "a"*40,
                   "mergeCommit": {"oid": "b"*40}, "closingIssuesReferences": [{"number": 10122}],
                   "labels": ["ci-head:old", "pm-cleanup:needed"]}
        self.issue = {"number": 10122, "state": "CLOSED", "stateReason": "COMPLETED",
                      "labels": ["status:todo"]}
        self.effects = []
        self.lose_add = lose_add

    def read_pr(self, req): return copy.deepcopy(self.pr)
    def read_issue(self, req): return copy.deepcopy(self.issue)

    def mutate(self, scope, op, label):
        self.effects.append(f"{scope}:{op}:{label}")
        target = self.pr if scope == 'pr' else self.issue
        if op == 'remove': target['labels'].remove(label)
        else:
            target['labels'].append(label)
            if self.lose_add: raise cleanup.CleanupError('github_effect_ambiguous', ambiguous=True)

    def remove_pr_label(self, req, label): self.mutate('pr', 'remove', label)
    def add_pr_label(self, req, label): self.mutate('pr', 'add', label)
    def remove_issue_label(self, req, label): self.mutate('issue', 'remove', label)
    def add_issue_label(self, req, label): self.mutate('issue', 'add', label)


def invoke(tmp, external, **overrides):
    return cleanup.run(request(**overrides), receipt_path=tmp/'receipts.json',
                       mapping_path=tmp/'no-mapping.json', external=external)


def test_actual_reference_with_authoritative_issue_validates_before_mutation(tmp_path):
    external = External()
    external.pr['closingIssuesReferences'][0].update(id='fixture-id', url='https://example.test/issue/10122')
    assert invoke(tmp_path, external, issue_authoritative=True)['success']
    assert 'pm-state:closed-clean' in external.pr['labels']
    assert external.issue['labels'] == ['status:done']


def test_mismatched_authoritative_reference_refuses_without_mutation(tmp_path):
    external = External()
    external.pr['closingIssuesReferences'] = [{'number': 9999}]
    with pytest.raises(cleanup.CleanupError): invoke(tmp_path, external, issue_authoritative=True)
    assert external.effects == []


def test_success_reentry_does_not_duplicate_mutation(tmp_path):
    external = External()
    assert invoke(tmp_path, external)['success']
    effects = list(external.effects)
    assert invoke(tmp_path, external)['idempotent'] is True
    assert external.effects == effects


def test_response_loss_preserves_partial_unknown_and_never_retries(tmp_path):
    external = External(lose_add=True)
    with pytest.raises(cleanup.CleanupError) as error:
        invoke(tmp_path, external)
    assert error.value.ambiguous
    assert error.value.details['completed_steps'] == ['pr_label_remove:ci-head:old', 'pr_label_remove:pm-cleanup:needed']
    assert error.value.details['ambiguous_steps'] == ['pr_label_add:pm-state:closed-clean']
    assert error.value.details['remaining_steps'] == ['issue_label_remove:status:todo', 'issue_label_add:status:done']
    # No later issue mutation after the unknown PR addition.
    assert not any(e.startswith('issue:') for e in external.effects)
    prior = (tmp_path/'receipts.json').read_bytes()
    effects = list(external.effects)
    with pytest.raises(cleanup.CleanupError): invoke(tmp_path, external)
    assert external.effects == effects and (tmp_path/'receipts.json').read_bytes() == prior


def test_invalid_later_plan_step_refuses_before_any_effect(tmp_path):
    external = External()
    validated = cleanup._validate_request(request())
    req = cleanup._cleanup_identity(validated)
    req.update(cleanup_mode=validated['cleanup_mode'], thread_reply=False, post_merge_terminal=False)
    key = cleanup._cleanup_key(req)
    entry = {'request': cleanup._cleanup_identity(req), 'cleanup_mode': 'linked_issue', 'thread_reply': False,
             'thread_ts': None, 'status': 'prepared', 'steps': {}, 'ambiguous_steps': [],
             'plan': [{'name': 'pr_label_remove:ci-head:old', 'scope': 'pr', 'operation': 'remove', 'label': 'ci-head:old'},
                      {'name': 'invalid-tail', 'scope': 'pr', 'operation': 'unknown', 'label': ''}]}
    path = tmp_path/'receipts.json'; path.write_text(json.dumps({key:entry})); prior=path.read_bytes()
    with pytest.raises(cleanup.CleanupError) as error: invoke(tmp_path, external)
    assert error.value.reason == 'cleanup_receipt_plan_invalid'
    assert external.effects == [] and path.read_bytes() == prior


def test_initial_journal_failure_is_typed_and_has_zero_effects(tmp_path):
    external = External()
    with patch.object(cleanup, '_write_json', side_effect=OSError('fake disk unavailable')):
        with pytest.raises(cleanup.CleanupError) as error: invoke(tmp_path, external)
    assert error.value.reason == 'cleanup_receipt_write_failed'
    assert not error.value.ambiguous and error.value.details['completed_steps'] == []
    assert external.effects == []


def test_completion_journal_failure_preserves_started_receipt_and_no_retry(tmp_path):
    external = External(); real_write=cleanup._write_json
    def fail_completed(path, data):
        if any(s.get('status')=='completed' for entry in data.values() for s in entry.get('steps',{}).values()):
            raise OSError('fake lost completion write')
        return real_write(path,data)
    with patch.object(cleanup, '_write_json', side_effect=fail_completed):
        with pytest.raises(cleanup.CleanupError) as error: invoke(tmp_path,external)
    assert error.value.reason=='cleanup_receipt_write_failed' and error.value.ambiguous
    assert error.value.details['receipt_write_confirmed'] is False
    assert error.value.details['completed_steps']==['pr_label_remove:ci-head:old']
    prior=(tmp_path/'receipts.json').read_bytes();effects=list(external.effects)
    with pytest.raises(cleanup.CleanupError): invoke(tmp_path,external)
    assert external.effects==effects and (tmp_path/'receipts.json').read_bytes()==prior


def test_unknown_github_response_is_not_invented_success():
    result=subprocess.CompletedProcess([],1,b'',b'fake private error')
    with patch.object(cleanup.subprocess,'run',return_value=result) as run:
        with pytest.raises(cleanup.CleanupError) as error:
            cleanup.External().add_pr_label(request(),'pm-state:closed-clean')
    assert error.value.ambiguous and run.call_count==1


def test_terminal_validation_failure_reports_completed_effects_without_success(tmp_path):
    external=External(); read=external.read_issue
    def reopen(req):
        result=read(req)
        if result['labels']==['status:done']:result.update(state='OPEN',stateReason='REOPENED')
        return result
    external.read_issue=reopen
    with pytest.raises(cleanup.CleanupError) as error:invoke(tmp_path,external)
    assert error.value.reason=='linked_issue_not_terminal' and error.value.ambiguous
    assert len(error.value.details['completed_steps'])==5
    prior=(tmp_path/'receipts.json').read_bytes();effects=list(external.effects)
    with pytest.raises(cleanup.CleanupError):invoke(tmp_path,external)
    assert external.effects==effects and (tmp_path/'receipts.json').read_bytes()==prior


def test_cli_partial_terminal_is_one_json_response(tmp_path,monkeypatch,capsys):
    import io
    monkeypatch.setattr(cleanup,'External',lambda:External(lose_add=True))
    monkeypatch.setattr('sys.stdin',io.StringIO(json.dumps(request())))
    assert cleanup.main(['--receipt-path',str(tmp_path/'receipts.json')])==2
    output=capsys.readouterr().out
    result=json.loads(output)
    assert len(output.splitlines())==1
    assert result['success'] is False and result['status']=='ambiguous'
    assert result['completed_steps'] and result['remaining_steps'] and result['ambiguous_steps']


@pytest.mark.parametrize('override', [None, 'explicit-store'])
def test_retro_store_default_and_override_keep_out_distinct(tmp_path,monkeypatch,override):
    monkeypatch.delenv('HEYDONNA_RETRO_STORE_DIR',raising=False)
    if override:monkeypatch.setenv('HEYDONNA_RETRO_STORE_DIR',str(tmp_path/override))
    with patch.object(Path,'home',return_value=tmp_path):retro=load('pr-delivery-retro')
    expected=tmp_path/override if override else tmp_path/'.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/retros'
    assert retro.durable_paths(10126)==(expected/'pr-10126.md',expected/'pr-10126.json')
    out_md=tmp_path/'requested/result.md';out_json=tmp_path/'requested/result.json'
    with patch.object(retro,'render_markdown',return_value='# offline fixture\n'):
        retro.write_outputs({'pr':{'number':10126}},out_md,out_json)
    assert out_md.exists() and out_json.exists()
    assert (expected/'pr-10126.md').exists() and (expected/'pr-10126.json').exists()
    assert not (tmp_path/'Downloads/projects/heydonna-app/docs/retros').exists()


def test_legacy_status_has_no_missing_background_sync_advice(capsys):
    ops=load('pm-ops-legacy')
    status={'sync_required':True,'reasons':['fixture stale view'],'db':'fixture.db',
            'pm_todo_lines':2,'open_obligations':1}
    with patch.object(ops,'status_dict',return_value=status):
        ops.print_status(argparse.Namespace(format='text',read_only=True))
    output=capsys.readouterr().out
    assert 'pm-ops-sync-bg.sh' not in output and '--no-live' not in output
    assert 'current PM' in output
    for path in [ASSETS/'pm/heartbeat/heartbeat-compose.py', ASSETS/'sakshi-heartbeat.sh']:
        assert 'pm-ops-sync-bg.sh' not in path.read_text()
