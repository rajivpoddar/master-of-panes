"""pr-open-snapshot resolves pr_* rows only by exact id through
obligation-resolve --if-unclaimed, and resolves stale rows only after a live
read proves the PR closed/merged. Hermetic: temp PM_OPS_DB, real writer
source, gh mocked."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts/pm/heartbeat"
LEGACY = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts/pm-ops-legacy.py"


def _load():
    spec = importlib.util.spec_from_file_location("pr_open_snapshot_unclaimed", HERE / "pr-open-snapshot.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


prsnap = _load()


def _iso(minutes):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed(db, kind, pr, *extra):
    out = subprocess.run([sys.executable, str(LEGACY), "obligation-upsert", "--kind", kind, "--target-type",
                          "github_pr", "--target-id", str(pr), "--pr", str(pr), "--title", "t", "--print-id", *extra],
                         env=dict(os.environ, PM_OPS_DB=db), capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return int(out.stdout.strip())


def _statuses(db):
    con = sqlite3.connect(db)
    try:
        return {r[0]: r[1] for r in con.execute("SELECT id, status FROM obligations")}
    finally:
        con.close()


class _Env:
    """Point the module and the writer subprocess at a temp ledger."""

    def __init__(self, db):
        self.db = db
        self.patches = [patch.object(prsnap, "PM_OPS_DB", Path(db)), patch.object(prsnap, "PM_OPS_CLI", LEGACY),
                        patch.dict(os.environ, {"PM_OPS_DB": db})]

    def __enter__(self):
        for p in self.patches:
            p.start()

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()


def test_resolve_uses_exact_id_and_if_unclaimed():
    with patch.object(prsnap, "PM_OPS_CLI", LEGACY), patch.object(prsnap, "_open_row_ids", return_value=[11, 12]), \
         patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess([], 0, "resolved\n", "")
        assert prsnap._obligation_resolve(kind="pr_admission", pr=5, reason="r") == ["resolved", "resolved"]
    for call, rid in zip(run.call_args_list, ("11", "12")):
        argv = call.args[0]
        assert argv[argv.index("--id") + 1] == rid
        assert "--if-unclaimed" in argv and "--pr" not in argv and "--target-id" not in argv


def test_claimed_row_survives_in_list_resolve_and_unclaimed_resolves(tmp_path):
    db = str(tmp_path / "a.db")
    claimed = _seed(db, "pr_red_unowned", 7, "--owner", "e2e-failure-investigator", "--suppress-until", _iso(30))
    generic = _seed(db, "pr_admission", 7, "--owner", "PM")
    with _Env(db):
        assert prsnap._obligation_resolve(kind="pr_red_unowned", pr=7, reason="required_gates_not_red") == ["skipped_claimed"]
        assert prsnap._obligation_resolve(kind="pr_admission", pr=7, reason="ci-head_matches_current_head") == ["resolved"]
    assert _statuses(db) == {claimed: "open", generic: "resolved"}


def test_stale_rows_resolve_only_when_live_read_proves_closed(tmp_path):
    db = str(tmp_path / "b.db")
    merged = _seed(db, "pr_admission", 1)
    still_open = _seed(db, "pr_admission", 2)  # beyond a truncated --limit 100 list
    failed = _seed(db, "pr_merge_ask", 3)
    listed = _seed(db, "pr_admission", 4)
    states = {1: "MERGED", 2: "OPEN", 3: None}
    with _Env(db), patch.object(prsnap, "_live_pr_state", side_effect=lambda n: states[n]) as live:
        prsnap._resolve_stale_pr_obligations({4})
    assert sorted(c.args[0] for c in live.call_args_list) == [1, 2, 3]
    assert _statuses(db) == {merged: "resolved", still_open: "open", failed: "open", listed: "open"}


def test_stale_claimed_rows_are_not_reread_or_resolved(tmp_path):
    db = str(tmp_path / "c.db")
    claimed = _seed(db, "pr_red_unowned", 9, "--owner", "ci-repair-agent", "--suppress-until", _iso(30))
    with _Env(db), patch.object(prsnap, "_live_pr_state", return_value="CLOSED") as live:
        prsnap._resolve_stale_pr_obligations(set())
    live.assert_not_called()
    assert _statuses(db) == {claimed: "open"}


def test_stale_recheck_is_capped():
    rows = [{"pr": n, "owner": None, "suppress_until": None} for n in range(1, 61)]

    class _Con:
        row_factory = None

        def execute(self, *a):
            return self

        def fetchall(self):
            return rows

        def close(self):
            pass

    with patch.object(prsnap, "PM_OPS_DB", Path(__file__)), patch("sqlite3.connect", return_value=_Con()), \
         patch.object(prsnap, "_live_pr_state", return_value="OPEN") as live:
        prsnap._resolve_stale_pr_obligations(set())
    assert live.call_count == prsnap.STALE_RECHECK_CAP == 50


def test_live_pr_state_failure_is_none():
    with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "boom")):
        assert prsnap._live_pr_state(5) is None
    with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps({"number": 6, "state": "MERGED"}), "")):
        assert prsnap._live_pr_state(5) is None  # wrong PR echoed back
        assert prsnap._live_pr_state(6) == "MERGED"
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["gh"], 15)):
        assert prsnap._live_pr_state(6) is None
