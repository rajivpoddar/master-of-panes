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
    refs = [{'time': '12:34', 'user_hash': h} for h in hashes]
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
    mapping = {row["code"]: {"status": "mapped", "refs": [f"owner-{i}"]}
               for i, row in enumerate(rows)}
    axiom = {
        "events": 33, "users": 12, "top": rows,
        "all_codes": rows, "other_codes": 0, "other_events": 0,
    }
    lines, tldr, _ = composer.error_block(axiom, mapping, "3h")
    folded = composer.compact_report(lines + ["• filler"] * 8, limit=1)
    for row in rows:
        assert row["code"] in folded
        assert mapping[row["code"]]["refs"][0] in folded
    assert "33 errors" in tldr

    new_axiom = {"events": 1, "users": 1,
                 "top": [{"code": "new_fault", "events": 1, "users": 1}]}
    _, new_tldr, _ = composer.error_block(new_axiom, {}, "3h")
    assert "NEW code: new_fault" in new_tldr


def test_3h_superseded_save_fallback_identities_are_not_counted_as_users():
    refs = [
        {"time": "12:34", "identity_kind": "userSha256", "user_hash": "u1"},
        {"time": "12:35", "identity_kind": "fileIdSha256", "user_hash": "f1"},
        {"time": "12:36", "identity_kind": "unknown", "user_hash": "unknown"},
    ]
    lines, _, _ = composer.error_block(
        {"events": 0, "users": 0, "superseded_saves": 3,
         "superseded_save_refs": refs}, {}, "3h")
    line = next(item for item in lines if "superseded saves:" in item)
    assert "1 distinct users" in line
    assert "2 fallback observations" in line
    assert "fileIdSha256 f1" in line and "unknown unknown" in line
