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
