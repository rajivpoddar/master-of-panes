"""Hourly heartbeat LangSmith ingest line (reconcile exit 0/2 OK, 1 WARN, else UNKNOWN)."""

import importlib.util
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts/pm/heartbeat/heartbeat-compose.py"
SPEC = importlib.util.spec_from_file_location("heartbeat_langsmith", SOURCE)
composer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(composer)

OUT = "LangSmith ingest: UNCOMPARABLE (PROD 1h traces 6 vs calls 3; 0 observed prod ingest errors)"


def test_uncomparable_is_ok_with_note():
    assert composer.langsmith_line(2, OUT) == "*LangSmith ingest (1h):* OK — 0 ingest errors (traces 6 / calls 3, uncomparable basis)"


def test_healthy_is_ok():
    assert composer.langsmith_line(0, OUT).startswith("*LangSmith ingest (1h):* OK — 0 ingest errors (traces 6 / calls 3)")


def test_failing_warns_with_count():
    out = "LangSmith ingest: FAILING (PROD 1h traces 6 vs calls 3; app/fn 5 errors; x/y 2 errors)"
    assert composer.langsmith_line(1, out) == "*LangSmith ingest (1h):* WARN — 7 ingest errors (statuses app/fn 5, x/y 2)"


def test_unexpected_rc_unknown():
    assert composer.langsmith_line(None, "") == "LangSmith ingest: UNKNOWN (check failed)"


def test_timeout_and_exception_unknown(monkeypatch):
    import subprocess
    def boom(*a, **k):
        raise subprocess.TimeoutExpired("x", 60)
    monkeypatch.setattr(composer.subprocess, "run", boom)
    assert composer.collect_langsmith("python3") == "LangSmith ingest: UNKNOWN (check failed)"


def test_only_in_hourly_compose():
    from datetime import datetime, timezone
    line = "*LangSmith ingest (1h):* OK"
    h = composer.compose("1h", datetime.now(timezone.utc), None, {}, [], langsmith=line)
    assert line in h
    t = composer.compose("3h", datetime.now(timezone.utc), None, {}, [], langsmith=line)
    assert line not in t
