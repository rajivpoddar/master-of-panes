"""Overlap report in the 12b admission output is advisory; no GitHub access.

Retro #13357/#13435; CTO C0ALZJHGE49 thread 1794234970.206389, ts 1794578332.315349.
"""
import importlib.util
from pathlib import Path
from unittest.mock import patch

P = Path(__file__).parents[1] / 'shared-assets/claude/scripts/ci/pm-admit-12b.py'
spec = importlib.util.spec_from_file_location('admit_overlap_report', P)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

REVIEWED = 'a' * 40
BASE = 'b' * 40
TIP = 'c' * 40
HEAD = 'd' * 40
COMMENTS = [{'body': f'PM 12b: PASS at {REVIEWED}', 'user': {'login': 'pm'}}]


def files(*names):
    return {'files': [{'filename': name} for name in names]}


def report(main_files, pr_files, comments=COMMENTS):
    def fake_api(repo, endpoint, pages=False):
        if endpoint.startswith('issues/'):
            return comments
        if endpoint == f'compare/{REVIEWED}...main':
            return {**files(*main_files), 'merge_base_commit': {'sha': BASE}, 'ahead_by': 3}
        if endpoint == f'compare/main...{REVIEWED}':
            return files(*pr_files)
        if endpoint == 'commits/main':
            return {'sha': TIP}
        raise AssertionError(endpoint)

    with patch.object(gate, 'api', side_effect=fake_api), \
         patch.object(gate, 'authorized_reviewer_login', return_value='pm'), \
         patch.object(gate, 'resolve_commit', side_effect=lambda sha: sha):
        return gate.overlap_report('owner/repo', 7, HEAD)


def test_overlap_names_base_main_paths_and_owning_test():
    lines = report(['lib/sync/head.ts', 'docs/x.md'], ['lib/sync/head.ts', 'lib/sync/head.test.ts', 'other.ts'])
    assert f'reviewed_base={BASE}' in lines[0] and f'current_main={TIP}' in lines[0]
    assert f'reviewed_head={REVIEWED}' in lines[0] and f'current_head={HEAD}' in lines[0]
    assert 'main_commits_since_review=3' in lines[0]
    assert 'overlap=1 paths=lib/sync/head.ts' in lines[1]
    assert 'proof_action=RE-PROVE affected_proof=lib/sync/head.test.ts' in lines[2]


def test_no_overlap_carries_reviewed_proof():
    lines = report(['docs/x.md'], ['lib/sync/head.ts'])
    assert 'overlap=0 paths=none' in lines[1]
    assert 'proof_action=CARRY' in lines[2]


def test_overlap_without_owning_test_is_unknown_proof_not_silence():
    lines = report(['lib/a.ts'], ['lib/a.ts'])
    assert 'proof_action=RE-PROVE affected_proof=UNKNOWN' in lines[2]


def test_truncated_file_list_never_claims_carry():
    lines = report([f'main/{n}.ts' for n in range(300)], ['lib/a.ts'])
    assert 'proof_action=UNKNOWN' in lines[2]


def test_no_authorized_marker_reports_unknown():
    assert report([], [], comments=[]) == ['12B_OVERLAP: UNKNOWN reason=no_authorized_12b_marker']


def test_api_failure_reports_unknown_and_never_raises():
    with patch.object(gate, 'api', side_effect=OSError('down')):
        assert gate.overlap_report('owner/repo', 7, HEAD) == ['12B_OVERLAP: UNKNOWN reason=OSError']


def test_overlap_only_prints_report_and_no_verdict(capsys):
    with patch.object(gate, 'overlap_report', return_value=['12B_OVERLAP: x']), \
         patch.object(gate, 'evaluate') as evaluate, \
         patch('sys.argv', ['pm-admit-12b.py', '--repo', 'owner/repo', '--pr', '7', '--head', HEAD, '--overlap-only']):
        assert gate.main() == 0
        evaluate.assert_not_called()
    assert capsys.readouterr().out.splitlines() == ['12B_OVERLAP: x']


def test_default_gate_output_and_exit_code_are_unchanged(capsys):
    with patch.object(gate, 'evaluate', return_value=(False, 'REFUSE: 12b_missing_or_stale pr=7')), \
         patch.object(gate, 'overlap_report') as report, \
         patch('sys.argv', ['pm-admit-12b.py', '--repo', 'owner/repo', '--pr', '7', '--head', HEAD]):
        assert gate.main() == 3
        report.assert_not_called()
    assert capsys.readouterr().out.splitlines() == ['REFUSE: 12b_missing_or_stale pr=7']


def test_admission_script_prints_report_after_main_update_before_admit_and_never_blocks():
    text = (P.parent / 'pm-admit-ci.sh').read_text()
    assert text.index('REFUSE: conflict with main') < text.index('overlap_report "$HEAD"') < text.index('echo "ADMITTED')
    assert '|| echo "12B_OVERLAP: UNKNOWN reason=report_unavailable"' in text
