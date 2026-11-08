"""Firewall counts distinguish actual refusals from candidate selections."""
import importlib.util
from pathlib import Path
from unittest.mock import patch


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
            {"action": "hallucination_firewall_cand2", "meta": '{}'},
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
