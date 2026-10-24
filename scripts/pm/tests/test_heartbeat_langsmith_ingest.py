"""Hourly heartbeat LangSmith ingest line: strict renderer, never infers health from absence."""

import importlib.util
import subprocess
from datetime import datetime, timezone
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts/pm/heartbeat/heartbeat-compose.py"
SPEC = importlib.util.spec_from_file_location("heartbeat_langsmith", SOURCE)
composer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(composer)
UNKNOWN = "LangSmith ingest: UNKNOWN (check failed)"

REAL = ("LangSmith ingest: UNCOMPARABLE (PROD 1h traces 6 vs calls 3; 0 observed prod ingest errors "
        "(emitter rollout unverified); format traces 2 vs calls 2; coverage=partial; sample basis differs; counts are not parity)")
ZERO = REAL.replace("traces 6 vs calls 3", "traces 0 vs calls 0")
FAIL = "LangSmith ingest: FAILING (PROD 1h traces 6 vs calls 3; app/fn 5 errors; x/y 2 errors; coverage=partial)"
AUTH = "LangSmith ingest: FAILING (canonical credentials unreadable; traces unknown vs calls unknown; coverage=unknown)"
QFAIL = "LangSmith ingest: FAILING (PROD 1h traces unknown vs calls unknown; Axiom missing/partial query status)"
OKOUT = "LangSmith ingest: OK (PROD 1h traces 6 vs calls 6; 0 observed prod ingest errors)"


def test_real_uncomparable_is_explicitly_not_healthy():
    line = composer.langsmith_line(2, REAL)
    assert "OK" not in line.split("—")[0]
    assert "0 observed errors; coverage partial/emitter rollout unverified; counts incomparable" in line


def test_zero_traces_and_calls_unknown():
    assert composer.langsmith_line(2, ZERO) == UNKNOWN


def test_empty_malformed_conflicting_unknown():
    for rc, out in [(0, ""), (2, ""), (0, "garbage"), (2, "LangSmith ingest: UNCOMPARABLE"), (None, REAL), (7, REAL),
                    (0, REAL), (2, FAIL), (1, REAL), (2, OKOUT), (0, FAIL)]:
        assert composer.langsmith_line(rc, out) == UNKNOWN, (rc, out)


def test_missing_script_stderr_only_rc2_unknown():
    assert composer.langsmith_line(2, "") == UNKNOWN


def test_auth_query_failure_without_errors_unknown():
    assert composer.langsmith_line(1, AUTH) == UNKNOWN
    assert composer.langsmith_line(1, QFAIL) == UNKNOWN


def test_positive_errors_warn():
    assert composer.langsmith_line(1, FAIL) == "*LangSmith ingest (1h):* WARN — 7 ingest errors (statuses app/fn 5, x/y 2)"


def test_rc0_requires_explicit_healthy_status():
    assert composer.langsmith_line(0, OKOUT).startswith("*LangSmith ingest (1h):* OK — 0 observed ingest errors")


def test_timeout_and_exception_unknown(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired("x", 60)
    monkeypatch.setattr(composer.subprocess, "run", boom)
    assert composer.collect_langsmith("python3") == UNKNOWN


def test_subprocess_stub_renders_real_output(monkeypatch):
    class P:
        returncode = 2
        stdout = REAL
    monkeypatch.setattr(composer.subprocess, "run", lambda *a, **k: P())
    assert "counts incomparable" in composer.collect_langsmith("python3")


def test_hourly_only_3h_excluded():
    line = "*LangSmith ingest (1h):* X"
    now = datetime.now(timezone.utc)
    # LangSmith ingest line dropped from the hourly (Rajiv ts 1792806414.519859).
    assert line not in composer.compose("1h", now, None, {}, [], langsmith=line)
    assert line not in composer.compose("3h", now, None, {}, [], langsmith=line)


def test_hourly_renders_training_line_but_not_langsmith():
    ls = "*LangSmith ingest (1h):* OK — 0 observed ingest errors (traces 5 / calls 5)"
    tr = ("*Training:* run-x · 10/100 (10%) · loss 0.1 · ETA 1h · mem min 1 MiB · monitor up", [])
    out = composer.compose("1h", datetime.now(timezone.utc), None, {}, [], langsmith=ls, training=tr)
    assert ls not in out and tr[0] in out
