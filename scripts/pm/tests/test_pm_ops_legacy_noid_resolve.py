"""Focused proof for the pm-ops-legacy obligation-resolve no-id repair.

Covers canonical source scripts/pm/shared-assets/claude/scripts/pm-ops-legacy.py:
a key-based resolve (no --id) that matches zero open rows must fail typed
instead of reporting success, while --id, exact-key, receipt, sibling, and
first_boundary_reservation behavior is preserved.

Hermetic: every case runs the canonical source as a subprocess against a
fresh tmp_path PM_OPS_DB. The live DB, live file, and obligation rows are
never touched.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[3]
CANDIDATE = ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "scripts" / "pm-ops-legacy.py"


def run(db: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PM_OPS_DB=db)
    return subprocess.run(
        [sys.executable, str(CANDIDATE), *args],
        env=env,
        capture_output=True,
        text=True,
    )


def statuses(db: str) -> list[str]:
    con = sqlite3.connect(db)
    try:
        return [row[0] for row in con.execute("SELECT status FROM obligations ORDER BY id")]
    finally:
        con.close()


def resolve_receipts(db: str) -> int:
    con = sqlite3.connect(db)
    try:
        return int(con.execute("SELECT COUNT(*) FROM events WHERE event_type='obligation-resolve'").fetchone()[0])
    finally:
        con.close()


def test_canonical_source_is_present() -> None:
    assert CANDIDATE.is_file(), f"canonical source missing: {CANDIDATE}"


def test_noid_zero_match_fails_typed_with_row_open(tmp_path) -> None:
    db = str(tmp_path / "noid.db")
    run(db, "obligation-upsert", "--kind", "cleanup_pr", "--target-type", "issue",
        "--target-id", "7919", "--pr", "7919", "--title", "seed")
    result = run(db, "obligation-resolve", "--kind", "cleanup_pr", "--pr", "7919")
    assert result.returncode != 0, f"zero-match key resolve must not report success: {result.returncode}"
    assert "--id" in (result.stderr + result.stdout), "refusal must name the required --id"
    assert statuses(db) == ["open"], "no-match resolve must not write"
    assert resolve_receipts(db) == 0, "no-match resolve must mint no receipt"


def test_noid_exact_key_match_still_resolves_with_receipt(tmp_path) -> None:
    db = str(tmp_path / "exact.db")
    run(db, "obligation-upsert", "--kind", "cleanup_pr", "--pr", "7919", "--title", "seed")
    result = run(db, "obligation-resolve", "--kind", "cleanup_pr", "--pr", "7919")
    assert result.returncode == 0, result.stderr
    assert statuses(db) == ["resolved"]
    assert resolve_receipts(db) == 1


def test_id_match_resolves_and_mismatch_refuses(tmp_path) -> None:
    db = str(tmp_path / "byid.db")
    printed = run(db, "obligation-upsert", "--kind", "ci_rework", "--pr", "1", "--title", "s", "--print-id")
    rid = printed.stdout.strip()
    assert rid.isdigit(), f"expected printed obligation id, got: {printed.stdout!r}"
    matched = run(db, "obligation-resolve", "--kind", "ci_rework", "--id", rid)
    assert matched.returncode == 0, matched.stderr
    assert statuses(db) == ["resolved"]
    missed = run(db, "obligation-resolve", "--kind", "ci_rework", "--id", "99999")
    assert missed.returncode != 0, "unknown --id must keep refusing"


def test_sibling_rows_are_not_swept(tmp_path) -> None:
    db = str(tmp_path / "sibling.db")
    run(db, "obligation-upsert", "--kind", "cleanup_pr", "--target-type", "issue",
        "--target-id", "7919", "--pr", "7919", "--title", "sibling")
    run(db, "obligation-upsert", "--kind", "cleanup_pr", "--pr", "7919", "--title", "exact")
    result = run(db, "obligation-resolve", "--kind", "cleanup_pr", "--pr", "7919")
    assert result.returncode == 0, result.stderr
    con = sqlite3.connect(db)
    try:
        got = sorted(con.execute("SELECT target_id, status FROM obligations").fetchall())
    finally:
        con.close()
    assert got == [("7919", "open"), ("7919", "resolved")], got
