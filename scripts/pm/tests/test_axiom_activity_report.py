from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).parents[3]
MODULE_PATH = ROOT / "scripts/pm/shared-assets/claude/scripts/axiom-activity-report.py"
SPEC = importlib.util.spec_from_file_location("axiom_activity_report", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_frozen_window_and_explicit_utc_ist_rendering() -> None:
    assert MODULE.FROZEN_START == "2026-08-25T18:30:00Z"
    assert MODULE.FROZEN_END == "2026-08-26T18:30:00Z"
    assert MODULE.format_timestamp("2026-08-26T00:00:00Z") == (
        "2026-08-26 00:00:00 UTC / 2026-08-26 05:30:00 IST"
    )


def test_suppression_query_uses_action_and_unique_row_aggregation(monkeypatch) -> None:
    observed: dict[str, str] = {}

    def fake_query(token: str, apl: str, start: str, end: str) -> dict:
        observed.update({"apl": apl, "start": start, "end": end})
        return {"buckets": {"totals": []}}

    monkeypatch.setattr(MODULE, "run_apl_query", fake_query)
    assert MODULE.query_save_suppression("token", MODULE.FROZEN_START, MODULE.FROZEN_END) == []
    assert "['action'] in" in observed["apl"]
    assert "['message']" not in observed["apl"]
    assert "summarize count(), min(['_time']), max(['_time'])" in observed["apl"]
    assert observed["start"] == MODULE.FROZEN_START
    assert observed["end"] == MODULE.FROZEN_END


def test_suppression_summary_deduplicates_and_reports_frozen_counts() -> None:
    escape = {
        "action": "save_escape_unsynced",
        "branch": "main",
        "fileId": "file-a",
        "count_": 80,
        "dcount_": 80,
        "min_": "2026-08-25T18:30:00Z",
        "max_": "2026-08-26T18:30:00Z",
    }
    family = {
        "action": "save_suppressed_inflight",
        "branch": "main",
        "fileId": "file-b",
        "count_": 4810,
        "dcount_": 4810,
        "min_": "2026-08-25T18:31:00Z",
        "max_": "2026-08-26T18:29:00Z",
    }
    summary = MODULE.build_save_suppression_summary([escape, escape, family])
    assert summary["total"] == 4890
    assert summary["suppression_family_rows"] == 4890
    assert summary["unique_save_escape_unsynced"] == 80
    assert summary["by_action"] == {
        "save_suppressed_inflight": 4810,
        "save_escape_unsynced": 80,
    }
