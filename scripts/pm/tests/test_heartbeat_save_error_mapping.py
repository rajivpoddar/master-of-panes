"""The client save exception remains mapped even outside the top-five codes."""
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

HEARTBEAT = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts/pm/heartbeat"
SPEC = importlib.util.spec_from_file_location("heartbeat_error_map_save", HEARTBEAT / "heartbeat-error-map.py")
mapper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mapper)


def test_client_save_exception_has_both_existing_issue_refs():
    known = mapper.load_known_codes(HEARTBEAT / "heartbeat-known-codes.json")
    result = mapper.classify_mapping("save_failed_client_exception", [], [], [], known)
    assert result["status"] == "mapped"
    assert "#11312" in result["refs"][0] and "#6973" in result["refs"][0]


def test_retained_off_top_exception_reaches_hourly_report():
    axiom = json.loads((Path(__file__).parent / "fixtures/heartbeat-offtop-13-events.json").read_text())
    spec = importlib.util.spec_from_file_location("heartbeat_compose_offtop", HEARTBEAT / "heartbeat-compose.py")
    composer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(composer)
    known = mapper.load_known_codes(HEARTBEAT / "heartbeat-known-codes.json")
    code = "save_failed_client_exception"
    mapping = {code: mapper.classify_mapping(code, [], [], [], known)}
    report = composer.compose("1h", datetime.now(timezone.utc), axiom, mapping, [])
    assert axiom["events"] == 13 and code not in [row["code"] for row in axiom["top"]]
    assert "13 errors / 6 users" in report
    assert report.count(code) == 1
    line = next(line for line in report.splitlines() if code in line)
    assert "1 ev / 1 user" in line and "#11312" in line and "#6973" in line
    assert "+1 other codes, 1 events" not in report
