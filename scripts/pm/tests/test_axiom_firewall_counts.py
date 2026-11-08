"""Firewall counts distinguish actual refusals from candidate selections."""
import importlib.util
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
import io
import json

import pytest


SCRIPT = Path(__file__).parents[1] / "shared-assets/claude/scripts/axiom-activity-report.py"
spec = importlib.util.spec_from_file_location("axiom_activity_report", SCRIPT)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def test_firewall_refusals_exclude_selections_errors_and_nonfires():
    grouped = {"buckets": {"totals": [
        {"group": {"action": "hallucination_firewall"}, "aggregations": [{"op": "count_", "value": 12}]},
        {"group": {"action": "hallucination_firewall_error"}, "aggregations": [{"op": "count_", "value": 2}]},
    ]}}
    evaluations = {"buckets": {"totals": [
        {"group": row, "aggregations": [{"op": "count_", "value": 1}]} for row in [
            {"action": "bon_cand1_escalation_check", "meta": '{"immediate_escalate": true}'},
            {"action": "bon_cand1_escalation_check", "meta": '{"immediate_escalate": false}'},
            {"action": "hallucination_firewall_cand2", "meta": '{"immediate_escalate": true}'},
            {"action": "hallucination_firewall_cand2", "meta": '{"immediate_escalate": false}'},
        ]
    ]}}
    with patch.object(report, "run_apl_query", side_effect=[grouped, evaluations]):
        counts = report.query_soft_fail_counts("token", "start", "end")
    result = report.build_soft_fail_report(counts, 10)
    firewall = result["categories"]["hallucination_firewall"]
    assert firewall["total"] == 2
    assert firewall["rate"] == "20.0%"
    assert result["rates_vs_auto_process"]["firewall_rate"] == "20.0%"
    assert firewall["breakdown"] == {"cand1_fires": 1, "cand2_fires": 1, "selected_chunks": 12, "errors": 2}


@pytest.mark.parametrize("status,body,reason", [
    (403, b"forbidden", "http_403"),
    (500, b"error", "http_500"),
    (503, b"error", "http_503"),
    (429, b"limited", "http_429"),
    (200, b"{", "invalid_json"),
    (200, b"[]", "non_object"),
])
def test_failed_evaluation_is_not_zero(monkeypatch, capsys, status, body, reason):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return body
    Response.status = status
    monkeypatch.setattr(report, "urlopen", lambda *args, **kwargs: Response())
    monkeypatch.setattr(report.time, "sleep", lambda _: None)
    failed = report.run_apl_query("token", "query", "start", "end")
    assert failed == {"_unavailable": reason}
    grouped = {"buckets": {"totals": [
        {"group": {"action": "hallucination_firewall"}, "aggregations": [{"op": "count_", "value": 12}]},
        {"group": {"action": "hallucination_firewall_error"}, "aggregations": [{"op": "count_", "value": 2}]},
    ]}}
    with patch.object(report, "run_apl_query", side_effect=[grouped, failed]):
        counts = report.query_soft_fail_counts("token", "start", "end")
    result = report.build_soft_fail_report(counts, 10)
    firewall = json.loads(json.dumps(result))["categories"]["hallucination_firewall"]
    assert firewall["status"] == "partial"
    assert firewall["evaluation_error"] == reason
    assert firewall["total"] is None and firewall["rate"] is None
    assert result["rates_vs_auto_process"]["firewall_rate"] is None
    assert firewall["breakdown"] == {"cand1_fires": None, "cand2_fires": None, "selected_chunks": 12, "errors": 2}
    report.print_soft_fail_report(result)
    output = capsys.readouterr().out
    assert "evaluation unavailable" in output and "selected chunks: 12  errors: 2" in output
    assert "0 fires" not in output.split("Hallucination Firewall:")[1].splitlines()[0]


@pytest.mark.parametrize("meta", [None, "", "{", "[]", "{}", '{"immediate_escalate": "true"}'])
def test_invalid_evaluation_metadata_is_partial(meta):
    grouped = {"buckets": {"totals": []}}
    evaluation = {"buckets": {"totals": [{"group": {"action": "bon_cand1_escalation_check", "meta": meta}, "aggregations": [{"op": "count_", "value": 1}]}]}}
    with patch.object(report, "run_apl_query", side_effect=[grouped, evaluation]):
        counts = report.query_soft_fail_counts("token", "start", "end")
    firewall = report.build_soft_fail_report(counts, 10)["categories"]["hallucination_firewall"]
    assert firewall["evaluation_error"] == "invalid_evaluation_metadata"
    assert firewall["total"] is None and firewall["rate"] is None


def test_http_error_exception_and_retry(monkeypatch):
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        raise HTTPError("https://example.invalid", 429, "limited", {}, io.BytesIO(b"limited"))
    monkeypatch.setattr(report, "urlopen", fail)
    monkeypatch.setattr(report.time, "sleep", lambda _: None)
    assert report.run_apl_query("token", "query", "start", "end") == {"_unavailable": "http_429"}
    assert len(calls) == 2
