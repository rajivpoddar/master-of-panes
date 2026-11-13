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
