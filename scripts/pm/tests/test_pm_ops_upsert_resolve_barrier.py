"""Canonical upsert vs guarded resolve: the upsert's open-row lookup and write are one
BEGIN IMMEDIATE transaction, so fresh evidence always lands in exactly one OPEN row.

CTO review of 5063df54 (C0ALZJHGE49 thread 1793864837.357739, ts 1793872988.077099):
upsert A selected open row R, guarded resolve B resolved R and committed, then A wrote
fresh evidence onto the RESOLVED row and the debt disappeared.

Barrier: the real upsert runs in-process with its connection wrapped so it pauses right
after its open-row lookup; the real guarded resolve runs as a separate pm-ops process.
Hermetic tmp_path PM_OPS_DB.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

LEGACY = Path(__file__).resolve().parents[1] / "shared-assets" / "claude" / "scripts" / "pm-ops-legacy.py"
LOOKUP = "SELECT id, evidence_json, owner, required_action FROM obligations\n        WHERE status='open'"
KEY = ["--kind", "orphaned_issue", "--target-type", "github_issue", "--target-id", "77", "--issue", "77",
       "--title", "t"]


def _cli(db, *args):
    return subprocess.run([sys.executable, str(LEGACY), *args], env=dict(os.environ, PM_OPS_DB=db),
                          capture_output=True, text=True, timeout=60)


def _rows(db):
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT id, status, evidence_json, updated_at FROM obligations ORDER BY id").fetchall()
    finally:
        con.close()


def _load(db, monkeypatch):
    monkeypatch.setenv("PM_OPS_DB", db)  # module reads DB_PATH at import
    spec = importlib.util.spec_from_file_location(f"pm_ops_barrier_{abs(hash(db))}", LEGACY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Paused:
    """Proxy connection that pauses once, right after the upsert's open-row lookup."""

    def __init__(self, con, looked_up, release):
        self._con, self._looked_up, self._release = con, looked_up, release

    def execute(self, sql, *args):
        cur = self._con.execute(sql, *args)
        if LOOKUP in sql and not self._looked_up.is_set():
            rows = cur.fetchall()
            self._looked_up.set()
            assert self._release.wait(30), "barrier never released"

            class _Done:
                def fetchone(_):
                    return rows[0] if rows else None
            return _Done()
        return cur

    def __getattr__(self, name):
        return getattr(self._con, name)


def _guarded_resolve(db, rid, updated, digest):
    return subprocess.Popen(
        [sys.executable, str(LEGACY), "obligation-resolve", "--kind", "orphaned_issue", "--id", str(rid),
         "--if-unclaimed", "--reason", "live_recheck_cleared", "--expect-updated-at", updated,
         "--expect-evidence-sha256", digest],
        env=dict(os.environ, PM_OPS_DB=db), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_resolve_interleaved_after_upsert_lookup_cannot_strand_fresh_evidence(tmp_path, monkeypatch):
    db = str(tmp_path / "barrier.db")
    _cli(db, "obligation-upsert", *KEY, "--evidence-json", json.dumps({"reasons": ["done_open"]}))
    [(rid, _, evidence, updated)] = _rows(db)
    digest = hashlib.sha256(evidence.encode()).hexdigest()  # B verified this version
    time.sleep(1.1)  # A's write gets a new updated_at second
    module = _load(db, monkeypatch)
    looked_up, release = threading.Event(), threading.Event()
    real_connect = module.connect
    module.connect = lambda: _Paused(real_connect(), looked_up, release)
    fresh = {"reasons": ["done_open", "partial_merge"], "fresh": True}
    errors = []

    def upsert_a():
        try:
            module.main(["obligation-upsert", *KEY, "--evidence-json", json.dumps(fresh)])
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)
    a = threading.Thread(target=upsert_a)
    a.start()
    assert looked_up.wait(30), "upsert never reached its lookup"
    b = _guarded_resolve(db, rid, updated, digest)      # interleave B while A is paused
    time.sleep(1.5)
    b_finished_while_a_paused = b.poll() is not None
    release.set()
    a.join(60)
    out, err = b.communicate(timeout=60)
    assert not errors and b.returncode == 0, (errors, err)
    assert not b_finished_while_a_paused, "resolve must wait for the upsert's write transaction"
    assert out.strip() == "skipped_changed"
    open_rows = [r for r in _rows(db) if r[1] == "open"]
    assert len(open_rows) == 1 and json.loads(open_rows[0][2]) == fresh


def test_upsert_after_committed_resolve_creates_new_open_row(tmp_path):
    db = str(tmp_path / "after.db")
    _cli(db, "obligation-upsert", *KEY, "--evidence-json", json.dumps({"reasons": ["done_open"]}))
    [(rid, _, evidence, updated)] = _rows(db)
    digest = hashlib.sha256(evidence.encode()).hexdigest()
    b = _guarded_resolve(db, rid, updated, digest)
    out, _ = b.communicate(timeout=60)
    assert out.strip() == "resolved"
    fresh = {"reasons": ["unowned_lifecycle"], "fresh": True}
    _cli(db, "obligation-upsert", *KEY, "--evidence-json", json.dumps(fresh))
    rows = _rows(db)
    assert [r[1] for r in rows] == ["resolved", "open"]
    assert json.loads(rows[1][2]) == fresh and json.loads(rows[0][2]) == {"reasons": ["done_open"]}
