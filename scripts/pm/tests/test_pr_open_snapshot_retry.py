"""The live PR collector retries transient failures without inventing an empty list."""
import importlib.util
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from unittest.mock import patch

import pytest

PATH = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts/pm/heartbeat/pr-open-snapshot.py"
spec = importlib.util.spec_from_file_location("snapshot_retry", PATH)
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)


def test_retry_then_success():
    failed = CompletedProcess([], 1, stdout="", stderr="stream err")
    success = CompletedProcess([], 0, stdout='[{"number": 42, "isDraft": true}]', stderr="")
    with patch.object(snapshot.subprocess, "run", side_effect=[failed, success]) as run, \
         patch.object(snapshot.time, "sleep") as sleep, \
         patch.object(snapshot, "_read_ci_reconciliation", return_value=None):
        rows = snapshot._fetch_open_prs_live(with_threads=False)
    assert rows[0]["number"] == 42
    assert run.call_count == 2
    assert run.call_args.kwargs["timeout"] == snapshot.LIST_TIMEOUT_SECONDS
    cmd = run.call_args.args[0]
    assert cmd[cmd.index("--limit") + 1] == "200"
    assert cmd[cmd.index("--json") + 1] == snapshot.LIST_FIELDS
    sleep.assert_called_once_with(1)


def test_persistent_failure_is_unavailable():
    with patch.object(snapshot.subprocess, "run", side_effect=TimeoutExpired("gh", 20)) as run, \
         patch.object(snapshot.time, "sleep"):
        with pytest.raises(RuntimeError, match="UNAVAILABLE: gh pr list failed"):
            snapshot._fetch_open_prs_live(with_threads=False)
    assert run.call_count == snapshot.LIST_ATTEMPTS
