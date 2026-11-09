"""12b receipt policy and complete-history checks; no live GitHub calls."""
import importlib.util
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timezone
import subprocess

P = Path(__file__).parents[1] / 'shared-assets/claude/scripts/ci/pm-admit-12b.py'
spec = importlib.util.spec_from_file_location('admit12b', P)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
HEAD = 'a' * 40
OLD = 'b' * 40
REVIEW = {'HEAD_SHA': OLD}


def evaluate(body=None, review=REVIEW, history=True, grace=False, exempt=False):
    comments = [] if body is None else [{'body': body}]
    with patch.object(gate, 'api', return_value=comments), \
         patch.object(gate, 'exempt', return_value=exempt), \
         patch.object(gate, 'read_review', return_value=review), \
         patch.object(gate, 'verify_history', return_value=history) as verify, \
         patch.object(gate, 'transition_open', return_value=grace):
        result = gate.evaluate('owner/repo', 123, HEAD)
        return result, verify.call_args


def test_marker_exact_without_comment():
    assert evaluate()[0][0]


def test_marker_main_merges_without_owned_changes():
    result, call = evaluate(f'## PM 12b: PASS at {HEAD}')
    assert result[0] and call.args[-1] is False


def test_mechanical_uses_history_not_comment_authority():
    body = f'PM 12b delta: PASS at {HEAD} (mechanical)'
    assert evaluate(body)[1].args[-1] is True
    assert not evaluate(body, history=False)[0][0]
    assert not evaluate(body, review=None)[0][0]


def test_comment_alone_refused_strict():
    result, _ = evaluate(f'PM 12b: PASS at {HEAD}', review=None)
    assert result == (False, f'REFUSE: 12b_provenance_missing pr=123 head={HEAD} last_marker=none')


def test_transition_warns_and_preserves_heading_prefix():
    result, _ = evaluate(f'## PM 12b: PASS at {HEAD}', review=None, grace=True)
    assert result[0] and '12B_GATE: WARN provenance_unverified' in result[1]


def test_negative_verdict_overrides_old_receipt():
    assert not evaluate(f'PM 12b: REVISE at {HEAD}')[0][0]


def test_exemption_unchanged():
    assert evaluate(review=None, exempt=True)[0] == (True, '12B_GATE: exempt (control-plane-only)')


def test_missing_objects_fail_closed():
    with patch.object(gate, 'verify_history', side_effect=RuntimeError('missing')):
        with patch.object(gate, 'read_review', return_value=REVIEW), \
             patch.object(gate, 'exempt', return_value=False), \
             patch.object(gate, 'api', return_value=[]), \
             patch.object(gate, 'transition_open', return_value=False):
            assert not gate.evaluate('owner/repo', 123, HEAD)[0]


def test_strict_env_and_fixed_expiry():
    with patch.dict(gate.os.environ, {'PM_ADMIT_12B_STRICT': '1'}):
        assert not gate.transition_open()
    with patch.object(gate, 'datetime') as clock, patch.dict(gate.os.environ, {}, clear=True):
        clock.now.return_value = datetime(2026, 11, 11, 18, 40, 8, tzinfo=timezone.utc)
        assert gate.transition_open()
        clock.now.return_value = gate.PROVENANCE_DEADLINE
        assert not gate.transition_open()


def test_receipt_validation(tmp_path):
    marker = tmp_path / 'receipt'
    good = f'PR: 123\nHEAD_SHA: {HEAD}\nVERDICT: PASS\nREVIEWER: pm-admission-reviewer\nTIMESTAMP: 2026-11-09T00:00:00Z\n'
    with patch.object(gate, 'Path', return_value=marker):
        marker.write_text(good)
        assert gate.read_review(123)['HEAD_SHA'] == HEAD
        for old, new in [('PR: 123', 'PR: 124'), (HEAD, 'abc'), ('VERDICT: PASS', 'VERDICT: BLOCK'),
                         ('REVIEWER: pm-admission-reviewer', 'REVIEWER: PM'),
                         ('2026-11-09T00:00:00Z', '2999-01-01T00:00:00Z'),
                         ('2026-11-09T00:00:00Z', 'nonsense')]:
            marker.write_text(good.replace(old, new))
            assert gate.read_review(123) is None
        marker.write_text(good + 'VERDICT: PASS\n')
        assert gate.read_review(123) is None


def test_test_file_allowlist():
    for name in ['test/a.test.ts', 'scripts/tests/test_gate.py', 'tests/test-admit.sh', 'x.spec.tsx']:
        assert gate.test_file(name)
    for name in ['lib/a.ts', 'tests/helper.py', 'tests/fixtures/customer.json', 'tests/config.ts']:
        assert not gate.test_file(name)


def history(*, mechanical=False, merge=False, paths='tests/a.test.ts', conflict=False,
            wrong_tree=False, fake_main=False, subject="Merge branch 'main' into topic"):
    # Exercise the actual verifier's git protocol and first-parent inspection.
    def output(argv, **kwargs):
        args = argv[3:]
        if args[:2] == ['merge-base', '--is-ancestor'] and fake_main and args[2] == 'c' * 40:
            raise subprocess.CalledProcessError(1, argv)
        if args[:1] == ['merge-base'] and '--is-ancestor' not in args:
            return b'dddddddddddddddddddddddddddddddddddddddd'
        if args[:1] == ['rev-list']:
            return HEAD.encode()
        if args[:3] == ['show', '-s', '--format=%P']:
            return (OLD + (' ' + 'c' * 40 if merge else '')).encode()
        if args[:3] == ['show', '-s', '--format=%s']:
            return subject.encode()
        if args[:1] == ['diff']:
            return b'lib/owned.ts' if args[-1] == OLD else paths.encode()
        if args[:1] == ['merge-tree']:
            if conflict:
                raise subprocess.CalledProcessError(1, argv)
            return b'tree'
        if args[:1] == ['rev-parse']:
            return b'wrong' if wrong_tree else b'tree'
        return b''
    with patch.object(gate.subprocess, 'check_output', side_effect=output), \
         patch.object(gate, 'api', return_value=[{'filename': 'lib/owned.ts'}]):
        try:
            return gate.verify_history('owner/repo', 123, OLD, HEAD, mechanical)
        except subprocess.CalledProcessError:
            return False


def test_history_test_only_mechanical():
    assert history(mechanical=True)
    assert not history(mechanical=False)
    assert not history(mechanical=True, paths='lib/owned.ts')
    assert not history(mechanical=True, paths='tests/a.test.ts\nlib/owned.ts')


def test_history_merge_clean_owned_and_unowned():
    assert history(merge=True, paths='lib/unowned.ts')
    assert not history(merge=True, paths='lib/owned.ts')
    assert history(merge=True, paths='lib/owned.ts', mechanical=True)
    assert not history(merge=True, mechanical=True, conflict=True)
    assert not history(merge=True, mechanical=True, wrong_tree=True)
    assert not history(merge=True, mechanical=True, fake_main=True)
    assert not history(merge=True, mechanical=True, subject='pretend merge')
