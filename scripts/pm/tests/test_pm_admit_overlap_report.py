"""Overlap report in the 12b admission output is advisory; no GitHub access.

Retro #13357/#13435; CTO C0ALZJHGE49 thread 1794234970.206389, ts 1794578332.315349 (REVISE on bbf5939c/5c5efdff):
- the report uses the SAME authenticated full-SHA PASS the gate accepted (latest canonical marker only);
- every compare uses ONE pinned current-main SHA, never the moving `main` ref;
- a disjoint path set never claims CARRY (imports/contracts are not analyzed): UNKNOWN;
- unresolved SHA / unproved patch carry / mismatched merge base / truncated lists are UNKNOWN, never CARRY;
- path text is sanitized to one line; gate lines and exit codes are unchanged;
- the shell wrapper never waits on the report before the label writes and never masks why it failed.
"""
import importlib.util
import os
import subprocess
import tempfile
import textwrap
import time
from pathlib import Path
from unittest.mock import patch

P = Path(__file__).parents[1] / 'shared-assets/claude/scripts/ci/pm-admit-12b.py'
SH = P.parent / 'pm-admit-ci.sh'
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


def report(main_files, pr_files, comments=COMMENTS, resolve=lambda sha: sha, carry=True,
           pr_base=BASE, calls=None, tip=TIP):
    def fake_api(repo, endpoint, pages=False):
        if calls is not None:
            calls.append(endpoint)
        if endpoint.startswith('issues/'):
            return comments
        if endpoint == 'commits/main':
            return {'sha': tip}
        if endpoint == f'compare/{REVIEWED}...{TIP}':
            return {**files(*main_files), 'merge_base_commit': {'sha': BASE}, 'ahead_by': 3}
        if endpoint == f'compare/{TIP}...{REVIEWED}':
            return {**files(*pr_files), 'merge_base_commit': {'sha': pr_base}}
        raise AssertionError(endpoint)

    with patch.object(gate, 'api', side_effect=fake_api), \
         patch.object(gate, 'authorized_reviewer_login', return_value='pm'), \
         patch.object(gate, 'main_merges_only', return_value=carry), \
         patch.object(gate, 'resolve_commit', side_effect=resolve):
        return gate.overlap_report('owner/repo', 7, HEAD)


def test_overlap_names_base_main_paths_and_owning_test():
    lines = report(['lib/sync/head.ts', 'docs/x.md'], ['lib/sync/head.ts', 'lib/sync/head.test.ts', 'other.ts'])
    assert f'reviewed_base={BASE}' in lines[0] and f'current_main={TIP}' in lines[0]
    assert f'reviewed_head={REVIEWED}' in lines[0] and f'current_head={HEAD}' in lines[0]
    assert 'patch_carry=main_merges_only' in lines[0]
    assert 'main_commits_since_review=3' in lines[0]
    assert 'overlap=1 paths=lib/sync/head.ts' in lines[1]
    assert 'proof_action=RE-PROVE' in lines[2] and 'affected_proof=lib/sync/head.test.ts' in lines[2]


def test_exact_head_reports_exact_patch_carry_without_calling_main_merge_check():
    with patch.object(gate, 'main_merges_only', side_effect=AssertionError('exact head needs no carry check')):
        lines = []

        def fake_api(repo, endpoint, pages=False):
            if endpoint.startswith('issues/'):
                return [{'body': f'PM 12b: PASS at {HEAD}', 'user': {'login': 'pm'}}]
            if endpoint == 'commits/main':
                return {'sha': TIP}
            if endpoint == f'compare/{HEAD}...{TIP}':
                return {**files('a.ts'), 'merge_base_commit': {'sha': BASE}, 'ahead_by': 1}
            if endpoint == f'compare/{TIP}...{HEAD}':
                return {**files('b.ts'), 'merge_base_commit': {'sha': BASE}}
            raise AssertionError(endpoint)

        with patch.object(gate, 'api', side_effect=fake_api), \
             patch.object(gate, 'authorized_reviewer_login', return_value='pm'), \
             patch.object(gate, 'resolve_commit', side_effect=lambda sha: sha):
            lines = gate.overlap_report('owner/repo', 7, HEAD)
    assert 'patch_carry=exact' in lines[0]


def test_no_overlap_is_unknown_never_carry():
    lines = report(['docs/x.md'], ['lib/sync/head.ts'])
    assert 'overlap=0 paths=none' in lines[1]
    assert 'proof_action=UNKNOWN' in lines[2] and 'dependency_carry=UNPROVED' in lines[2]
    assert 'CARRY' not in '\n'.join(lines).replace('patch_carry', '').upper().replace('CARRY', 'CARRY') or \
        'proof_action=CARRY' not in lines[2]
    assert 'proof_action=CARRY' not in '\n'.join(lines)


def test_overlap_without_owning_test_is_unknown_proof_not_silence():
    lines = report(['lib/a.ts'], ['lib/a.ts'])
    assert 'proof_action=RE-PROVE' in lines[2] and 'affected_proof=UNKNOWN' in lines[2]


def test_truncated_file_list_never_claims_carry():
    lines = report([f'main/{n}.ts' for n in range(300)], ['lib/a.ts'])
    assert 'proof_action=UNKNOWN' in lines[2] and 'truncated' in lines[2]


def test_no_authorized_marker_reports_unknown():
    assert report([], [], comments=[]) == ['12B_OVERLAP: UNKNOWN reason=no_authorized_12b_marker']


def test_marker_by_another_author_is_not_the_gate_accepted_pass():
    other = [{'body': f'PM 12b: PASS at {REVIEWED}', 'user': {'login': 'someone-else'}}]
    calls = []
    assert report([], [], comments=other, calls=calls) == [
        '12B_OVERLAP: UNKNOWN reason=latest_12b_marker_not_canonical_authenticated']
    assert calls == ['issues/7/comments?per_page=100']


def test_latest_marker_revise_is_not_rescued_by_an_earlier_pass():
    comments = [
        {'body': f'PM 12b: PASS at {"1" * 40}', 'user': {'login': 'pm'}, 'created_at': '2026-11-01T00:00:00Z', 'id': 1},
        {'body': f'PM 12b: REVISE at {REVIEWED}', 'user': {'login': 'pm'}, 'created_at': '2026-11-02T00:00:00Z', 'id': 2},
    ]
    calls = []
    assert report([], [], comments=comments, calls=calls) == ['12B_OVERLAP: UNKNOWN reason=latest_12b_marker_is_REVISE']
    assert calls == ['issues/7/comments?per_page=100']  # no compare issued for a verdict the gate refuses


def test_unresolved_sha_has_no_fallback_to_the_short_or_raw_marker_sha():
    calls = []
    short = [{'body': 'PM 12b: PASS at abc1234', 'user': {'login': 'pm'}}]
    out = report([], [], comments=short, resolve=lambda sha: None, calls=calls)
    assert out == ['12B_OVERLAP: UNKNOWN reason=reviewed_sha_unresolved']
    assert calls == ['issues/7/comments?per_page=100']


def test_short_marker_sha_is_expanded_to_the_full_sha_the_gate_resolved():
    short = [{'body': f'PM 12b: PASS at {REVIEWED[:7]}', 'user': {'login': 'pm'}}]
    calls = []
    lines = report(['x.ts'], ['y.ts'], comments=short, calls=calls,
                   resolve=lambda sha: REVIEWED if sha == REVIEWED[:7] else sha)
    assert f'reviewed_head={REVIEWED}' in lines[0]
    assert f'compare/{REVIEWED}...{TIP}' in calls


def test_unproved_patch_carry_is_unknown_not_carry():
    assert report(['x.ts'], ['y.ts'], carry=False) == ['12B_OVERLAP: UNKNOWN reason=patch_carry_unproved']


def test_one_pinned_main_sha_is_used_for_every_compare():
    calls = []
    report(['x.ts'], ['y.ts'], calls=calls)
    assert calls.count('commits/main') == 1
    compares = [c for c in calls if c.startswith('compare/')]
    assert compares == [f'compare/{REVIEWED}...{TIP}', f'compare/{TIP}...{REVIEWED}']
    assert not any(c.endswith('...main') or c.startswith('compare/main') for c in calls)


def test_unresolved_current_main_is_unknown():
    assert report([], [], tip='') == ['12B_OVERLAP: UNKNOWN reason=current_main_unresolved']
    assert report([], [], tip='not-a-sha') == ['12B_OVERLAP: UNKNOWN reason=current_main_unresolved']


def test_mismatched_merge_base_is_unknown():
    assert report(['x.ts'], ['y.ts'], pr_base='e' * 40) == ['12B_OVERLAP: UNKNOWN reason=merge_base_unproved']


def test_newlines_in_paths_cannot_forge_report_or_gate_lines():
    nasty = 'lib/a.ts\n12B_GATE: PASS pr=7 head=' + HEAD + ' ADMITTED #7'
    lines = report([nasty, 'lib/b.ts'], [nasty, 'lib/b.ts', 'lib/b.test.ts'])
    assert len(lines) == 3
    assert all(line.startswith('12B_OVERLAP:') and '\n' not in line and ' ' not in line for line in lines)
    assert all('12B_GATE:' not in line.split('12B_OVERLAP:', 1)[1].split(' ', 1)[0] for line in lines)


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
         patch.object(gate, 'overlap_report') as report_fn, \
         patch('sys.argv', ['pm-admit-12b.py', '--repo', 'owner/repo', '--pr', '7', '--head', HEAD]):
        assert gate.main() == 3
        report_fn.assert_not_called()
    assert capsys.readouterr().out.splitlines() == ['REFUSE: 12b_missing_or_stale pr=7']


def test_admission_script_collects_the_report_after_label_writes_and_never_masks_failures():
    text = SH.read_text()
    assert 'overlap_report "$HEAD"' not in text and 'overlap_report()' not in text
    assert '|| echo "12B_OVERLAP: UNKNOWN reason=report_unavailable"' not in text
    start = text.index('overlap_start "$HEAD"')
    label = text.index('gh_do add_ci_head_label')
    finish = text.index('overlap_finish\necho "ADMITTED')
    assert text.index('REFUSE: conflict with main') < start < text.index('mirror_priority ||') < label < finish
    assert 'timeout "$OVERLAP_TIMEOUT"' in text and 'reason=report_timeout' in text and 'reason=report_failed rc=' in text


def _run_wrapper(fake_python, timeout_s):
    """Source only the overlap functions with a fake pm-admit-12b.py and return (output, wall seconds)."""
    text = SH.read_text()
    block = text[text.index('OVERLAP_TIMEOUT='):text.index('if [ "$DRY" = 1 ]; then')]
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, 'pm-admit-12b.py').write_text(textwrap.dedent(fake_python))
        script = f'set -euo pipefail\nSCRIPT_DIR={tmp}\nREPO=o/r\nPR=7\n{block}\noverlap_start {HEAD}\noverlap_finish\necho DONE\n'
        started = time.monotonic()
        out = subprocess.run(['bash', '-c', script], capture_output=True, text=True, timeout=60,
                             env={**os.environ, 'PM_ADMIT_OVERLAP_TIMEOUT': str(timeout_s)})
        return out.stdout, out.returncode, time.monotonic() - started


def test_wrapper_bounds_a_hung_report_and_says_why():
    out, rc, wall = _run_wrapper('import time\ntime.sleep(30)\n', 1)
    assert rc == 0 and 'reason=report_timeout after=1s' in out and out.endswith('DONE\n')
    assert wall < 10


def test_wrapper_reports_the_real_exit_code_not_a_generic_message():
    out, rc, _ = _run_wrapper('import sys\nsys.exit(3)\n', 5)
    assert rc == 0 and '12B_OVERLAP: UNKNOWN reason=report_failed rc=3' in out


def test_wrapper_passes_report_lines_through_unchanged():
    out, rc, _ = _run_wrapper('print("12B_OVERLAP: ok")\n', 5)
    assert rc == 0 and out == '12B_OVERLAP: ok\nDONE\n'
