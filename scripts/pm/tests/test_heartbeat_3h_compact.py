"""Three-hour heartbeat output contract."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path


PATH = Path(__file__).resolve().parents[1] / 'shared-assets/claude/scripts/pm/heartbeat/heartbeat-compose.py'
spec = importlib.util.spec_from_file_location('heartbeat_3h_compact', PATH)
composer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(composer)


def test_3h_summary_and_physical_line_budget():
    hashes = ['aaa'] * 12 + ['bbb'] * 8 + ['ccc'] * 5 + ['ddd'] * 3
    refs = [{'time': '12:34', 'identity_kind': 'userIdSha256', 'user_hash': h} for h in hashes]
    axiom = {'events': 0, 'users': 0, 'superseded_saves': len(refs), 'superseded_save_refs': refs}
    report = composer.compose('3h', datetime.now(timezone.utc), axiom, {}, [], escalations=[composer.CTO])
    assert composer.CTO not in report
    line = next(line for line in report.splitlines() if 'superseded saves:' in line)
    assert '28' in line and '4 distinct users' in line
    assert 'aaa: 12' in line and 'bbb: 8' in line and 'ccc: 5' in line
    assert 'ddd' not in line and '12:34' not in line
    assert len(report.splitlines()) <= 24


def test_3h_fold_keeps_named_actionable_error_codes_and_tldr_names_new_code():
    rows = [
        {"code": f"actionable_{i}", "events": 4, "users": 2}
        for i in range(8)
    ]
    axiom = {
        "events": 33, "users": 12, "top": rows[:5],
        "all_codes": rows, "other_codes": 0, "other_events": 0,
    }
    requested = composer.three_hour_map_args(axiom)[1::2]
    mapping = {code: {"status": "mapped", "refs": [f"owner-{i}"]}
               for i, code in enumerate(requested)}
    lines, tldr, _ = composer.error_block(axiom, mapping, "3h")
    # The named mappings below the top-five list must survive the 3h fold too.
    folded = composer.compact_report(lines + ["• filler"] * 8, limit=1, cadence="3h")
    for row in rows:
        assert row["code"] in folded
        assert mapping[row["code"]]["refs"][0] in folded
    assert "33 errors" in tldr


def test_3h_off_top_new_and_unmapped_mapper_results_are_named_and_actionable():
    rows = [
        {"code": f"known_{i}", "events": 10 - i, "users": 2}
        for i in range(5)
    ] + [
        {"code": "sixth_new", "events": 4, "users": 3},
        {"code": "seventh_unmapped", "events": 2, "users": 1},
    ]
    axiom = {
        "events": 36, "users": 8, "top": rows[:5],
        "all_codes": rows, "other_codes": 2, "other_events": 6,
    }
    requested = composer.three_hour_map_args(axiom)[1::2]
    assert "sixth_new" in requested and "seventh_unmapped" in requested
    # This is the heartbeat-error-map.py production result shape.
    mapping = {code: {"code": code, "status": "mapped", "refs": ["#123"]}
               for code in requested[:5]}
    mapping["sixth_new"] = {"code": "sixth_new", "status": "NEW", "refs": []}
    mapping["seventh_unmapped"] = {"code": "seventh_unmapped", "status": "unmapped", "refs": []}

    lines, tldr, new_count = composer.error_block(axiom, mapping, "3h")
    folded = composer.compact_report(lines + ["• filler"] * 8, limit=1, cadence="3h")

    assert new_count == 1
    assert "sixth_new" in folded and "NEW → launch customer-artifact-investigator" in folded
    assert "sixth_new" in tldr
    assert "seventh_unmapped" in folded and "unmapped → classify and route for investigation" in folded
    assert "+0 other codes" not in "\\n".join(lines)


def test_3h_fold_always_reports_overflow_and_hourly_compaction_is_unchanged():
    rows = [f"• actionable_{i} — 1 ev / 1 users → owner-{i}" for i in range(4)]
    rows += ["• generic detail"] * 4
    folded_3h = composer.compact_report(["*Axiom errors (3h):*", *rows], limit=1, cadence="3h")
    assert "+4 more" in folded_3h
    folded_1h = composer.compact_report(["*Axiom errors (1h):*", *rows], limit=1, cadence="1h")
    assert "+7 more" in folded_1h

    new_axiom = {"events": 1, "users": 1,
                 "top": [{"code": "new_fault", "events": 1, "users": 1}]}
    _, new_tldr, _ = composer.error_block(new_axiom, {'new_fault': {'status': 'NEW'}}, "3h")
    assert "NEW code: new_fault" in new_tldr


def test_3h_superseded_save_fallback_identities_are_not_counted_as_users():
    refs = [
        {"time": "12:34", "identity_kind": "userIdSha256", "user_hash": "u1"},
        {"time": "12:35", "identity_kind": "fileIdSha256", "user_hash": "f1"},
        {"time": "12:36", "identity_kind": "unknown", "user_hash": "unknown"},
        {"time": "12:37", "identity_kind": "userSha256", "user_hash": "u2"},
        {"time": "12:38", "user_hash": "missing"},
    ]
    lines, _, _ = composer.error_block(
        {"events": 0, "users": 0, "superseded_saves": len(refs),
         "superseded_save_refs": refs}, {}, "3h")
    line = next(item for item in lines if "superseded saves:" in item)
    assert "1 distinct users" in line
    for kind in ('userSha256', 'fileIdSha256', 'unknown', 'missing-kind'):
        assert f'{kind} fallback: 1' in line


def test_hourly_output_matches_pre_correction_renderer():
    import subprocess
    import types
    import sys
    root = PATH.parents[7]
    source = subprocess.check_output([
        'git', '-C', str(root), 'show',
        '3e44e791:scripts/pm/shared-assets/claude/scripts/pm/heartbeat/heartbeat-compose.py',
    ], text=True)
    baseline = types.ModuleType('baseline_heartbeat_renderer')
    baseline.__file__ = str(PATH)
    sys.modules[baseline.__name__] = baseline
    exec(compile(source, str(PATH), 'exec'), baseline.__dict__)
    baseline.composer_release_sha = lambda: 'fixture'
    old_release = composer.composer_release_sha
    composer.composer_release_sha = lambda: 'fixture'
    try:
        rows = [{'code': f'fault_{i}', 'events': 2, 'users': 1} for i in range(5)]
        axiom = {'events': 10, 'users': 2, 'top': rows}
        mapping = {row['code']: {'status': 'NEW'} for row in rows}
        args = ('1h', datetime(2026, 1, 1, tzinfo=timezone.utc), axiom, mapping, [])
        assert composer.compose(*args).encode() == baseline.compose(*args).encode()
    finally:
        composer.composer_release_sha = old_release
        sys.modules.pop(baseline.__name__, None)


def test_3h_retained_sample_counts_user_id_hashes_as_users():
    hashes = [f"user-hash-{i}" for i in range(6)]
    refs = [
        {"time": f"12:{i:02d}", "identity_kind": "userIdSha256", "user_hash": hashes[i % 6]}
        for i in range(68)
    ]
    lines, _, _ = composer.error_block(
        {"events": 0, "users": 0, "superseded_saves": 68,
         "superseded_save_refs": refs}, {}, "3h")
    line = next(item for item in lines if "superseded saves:" in item)
    assert "68 — 6 distinct users" in line
    assert "fallback observations" not in line
    assert "user-hash-0: 12" in line
