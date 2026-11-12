"""The client save exception remains mapped even outside the top-five codes."""
import importlib.util
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
