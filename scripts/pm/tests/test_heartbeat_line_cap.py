"""Heartbeat composer: no output line exceeds 2,900 chars (first N items + "+K more").

Peer request from heydonna-app-88 (same MoP change): a 3,141-char "superseded saves" line made Slack
reject the post with invalid_blocks (block text limit 3,000).
"""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / 'shared-assets/claude/scripts/pm/heartbeat/heartbeat-compose.py'
spec = importlib.util.spec_from_file_location('heartbeat_line_cap', PATH)
composer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(composer)

LIMIT = 2900


def refs(count):
    return [{'time': f'{i % 24:02d}:{i % 60:02d}', 'identity_kind': 'userIdSha256',
             'user_hash': f'{i:02d}' + 'f' * 62} for i in range(count)]


def axiom_with(count):
    return {'events': 0, 'users': 0, 'superseded_saves': count, 'superseded_save_refs': refs(count)}


def test_unbounded_form_of_the_incident_line_really_exceeds_the_slack_limit():
    # Guards the test itself: the pre-fix join of 40 refs is longer than the 2,900 cap.
    raw = ', '.join(f"{r['time']} {r['identity_kind']}:{r['user_hash']}" for r in refs(40))
    assert len(raw) > LIMIT


def test_error_block_superseded_line_with_40_refs_is_bounded_with_more_tail():
    lines, _tldr, _new = composer.error_block(axiom_with(40), {}, '1h')
    line = next(line for line in lines if line.startswith('superseded saves:'))
    assert len(line) <= LIMIT
    assert 'superseded saves: 40' in line
    assert f'+{40 - composer.MAX_SUPERSEDED_REFS} more' in line
    assert line.count('userIdSha256:') == composer.MAX_SUPERSEDED_REFS


def test_error_block_with_few_refs_is_unchanged_and_has_no_more_tail():
    lines, _tldr, _new = composer.error_block(axiom_with(3), {}, '1h')
    line = next(line for line in lines if line.startswith('superseded saves:'))
    assert line.count('userIdSha256:') == 3 and 'more' not in line


def test_compose_1h_with_40_superseded_refs_has_no_line_over_the_cap():
    report = composer.compose('1h', datetime.now(timezone.utc), axiom_with(40), {}, [])
    assert max(len(line) for line in report.splitlines()) <= LIMIT
    assert any('superseded saves: 40' in line and 'more' in line for line in report.splitlines())


def test_compose_3h_with_40_superseded_refs_has_no_line_over_the_cap():
    report = composer.compose('3h', datetime.now(timezone.utc), axiom_with(40), {}, [])
    assert max(len(line) for line in report.splitlines()) <= LIMIT


def test_backstop_bounds_any_other_unbounded_list_line():
    items = [f'owner-{i:03d}-' + 'x' * 40 for i in range(120)]
    capped = composer.cap_line('*Pending PM actions:* ' + ', '.join(items))
    assert len(capped) <= LIMIT
    kept = capped.count('owner-')
    assert kept < 120 and capped.endswith(f'+{120 - kept} more')
    assert capped.startswith('*Pending PM actions:* owner-000-')


def test_backstop_prefers_item_boundaries_for_semicolon_lists():
    prefix = '*ACTIONS (PM):* '
    items = [f'PR #{i} head {"a" * 60}' for i in range(80)]
    capped = composer.cap_line(prefix + '; '.join(items))
    kept = capped.count('PR #')
    tail = f'; +{80 - kept} more'
    assert len(capped) <= LIMIT and 0 < kept < 80 and capped.endswith(tail)
    parts = capped[len(prefix):-len(tail)].split('; ')
    assert len(parts) == kept and all(part.startswith('PR #') and part.endswith('a' * 60) for part in parts)


def test_line_without_separators_is_hard_truncated_with_ellipsis():
    capped = composer.cap_line('x' * 5000)
    assert len(capped) <= LIMIT and capped.endswith('…')


def test_hard_truncation_never_leaves_half_a_mention():
    line = 'y' * (LIMIT - 5) + ' <@U0BNFGX2UAX> ' + 'z' * 100
    capped = composer.cap_line(line)
    assert len(capped) <= LIMIT and '<@' not in capped


def test_lines_within_the_limit_are_untouched_and_line_structure_is_preserved():
    text = 'short\n\n' + 'a' * LIMIT + '\n' + ', '.join(['item'] * 1000) + '\nlast'
    out = composer.cap_lines(text).split('\n')
    assert len(out) == 5 and out[0] == 'short' and out[1] == '' and out[2] == 'a' * LIMIT and out[4] == 'last'
    assert len(out[3]) <= LIMIT and out[3].endswith(' more')


def test_non_string_and_empty_input_never_raise():
    assert composer.cap_line('') == ''
    assert composer.cap_line(None) is None
    assert composer.cap_lines('') == ''
