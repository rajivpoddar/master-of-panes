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


def test_signature_and_superseded_queries_use_exact_window_and_evidence(monkeypatch):
    spec = importlib.util.spec_from_file_location('heartbeat_axiom_signature', HEARTBEAT / 'heartbeat-axiom.py')
    axiom = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(axiom)
    seen = []
    def query(token, apl, start, end):
        seen.append((apl, start, end))
        if 'by signature' in apl:
            return {'buckets': {'totals': [
                {'group': {'signature': axiom.RESIZE_OBSERVER_SIGNATURE + '.'}, 'aggregations': [{'op': 'events', 'value': 2}]},
                {'group': {'signature': 'different signature'}, 'aggregations': [{'op': 'events', 'value': 1}]}]}}
        return {'matches': [{'_time': '2026-11-13T03:27:32.543Z', 'data': {'user_hash': 'a' * 64}}]}
    monkeypatch.setattr(axiom, 'run_apl_query', query)
    assert axiom.query_uncaught_signatures('fake', 'start', 'end') == {'resize_observer': 2, 'unmapped': 1}
    assert axiom.query_superseded_saves('fake', 'start', 'end') == {'count': 1, 'refs': [{'time': '2026-11-13T03:27:32.543Z', 'user_hash': 'a' * 64}]}
    assert all(start == 'start' and end == 'end' for _, start, end in seen)
    assert "m.superseded" in seen[1][0] and 'SaveSafetyLifecycle' in seen[1][0]


def test_uncaught_mapping_is_signature_bound_not_action_bound():
    assert mapper.classify_mapping('uncaught_error', [{'number': 10038}], [], [], {})['refs'] == []
    spec = importlib.util.spec_from_file_location('heartbeat_compose_signature', HEARTBEAT / 'heartbeat-compose.py')
    composer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(composer)
    doc = {'events': 3, 'users': 1, 'top': [{'code': 'uncaught_error', 'events': 3, 'users': 1}],
           'uncaught_signatures': {'resize_observer': 2, 'unmapped': 1},
           'superseded_saves': 1, 'superseded_save_refs': [{'time': '2026-11-13T03:27:32.543Z', 'user_hash': 'a' * 64}]}
    lines, _, _ = composer.error_block(doc, {'uncaught_error': {'status': 'mapped', 'refs': ['#10038']}}, '1h')
    text = '\n'.join(lines)
    assert '#13670 (2 ev)' in text and 'unmapped (new signature): 1 ev' in text
    assert '#10038' not in text and '2026-11-13T03:27:32.543Z userSha256:' + 'a' * 64 in text
    doc.pop('uncaught_signatures')
    assert '#10038' not in '\n'.join(composer.error_block(doc, {'uncaught_error': {'refs': ['#10038']}}, '1h')[0])


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
