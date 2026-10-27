"""Drafts remain visible but never consume the non-draft PR cap."""
import importlib.util
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / 'shared-assets/claude/scripts/pm/heartbeat/heartbeat-compose.py'
spec = importlib.util.spec_from_file_location('heartbeat_pr_drafts', PATH)
compose = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compose)


def rows(non_drafts, drafts):
    return [dict(number=i, head='abc1234', state='draft' if i >= non_drafts else 'CI running',
                 owner='PM', next='wait', draft=i >= non_drafts)
            for i in range(non_drafts + drafts)]


def test_drafts_excluded_from_count_and_cap():
    lines, summary, asks = compose.pr_block(rows(25, 4))
    assert '25 open non-draft PRs (+4 drafts)' in summary
    assert 'cap 25/25' in '\n'.join(lines)
    assert 'cap exceeded' not in '\n'.join(lines)
    assert not asks


def test_non_draft_overflow_and_all_drafts():
    lines, summary, _ = compose.pr_block(rows(26, 2))
    assert '26 open non-draft PRs (+2 drafts)' in summary
    assert 'cap exceeded' in '\n'.join(lines)
    _, summary, _ = compose.pr_block(rows(0, 3))
    assert summary == '0 open non-draft PRs (+3 drafts)'


def test_live_fetch_preserves_github_isdraft_for_mixed_inventory(monkeypatch):
    import json
    producer_path = PATH.with_name('pr-open-snapshot.py')
    spec = importlib.util.spec_from_file_location('draft_snapshot', producer_path)
    producer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(producer)
    raw = [dict(number=i + 1, headRefOid='a' * 40, title='Mixed fixture',
                isDraft=i >= 19, labels=[], statusCheckRollup=[], body='', comments=[])
           for i in range(26)]
    def run(argv, **kwargs):
        assert 'isDraft' in argv[-1]
        return type('Proc', (), dict(returncode=0, stdout=json.dumps(raw), stderr=''))()
    monkeypatch.setattr(producer.subprocess, 'run', run)
    monkeypatch.setattr(producer, '_read_ci_reconciliation', lambda pr: None)
    fetched = producer.fetch_open_prs(with_threads=False)
    classified = [producer.classify_pr(pr) for pr in fetched]
    assert [r['isDraft'] for r in classified] == [r['isDraft'] for r in raw]
    # GitHub's field wins even when a legacy alias or operational state disagrees.
    for r in classified:
        r['draft'] = False
        r['state'] = 'CI running'
    lines, summary, _ = compose.pr_block(classified)
    assert summary == '19 open non-draft PRs (+7 drafts)'
    assert 'cap 19/25' in lines[0]
