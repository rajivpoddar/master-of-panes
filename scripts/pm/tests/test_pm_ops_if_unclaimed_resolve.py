"""obligation-resolve --if-unclaimed: atomic claim-aware exact-row resolve.

Hermetic: runs the canonical pm-ops-legacy.py source as a subprocess against a
fresh tmp_path PM_OPS_DB. The live ledger is never touched.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parents[3]
CANDIDATE = ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "scripts" / "pm-ops-legacy.py"


def run(db: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(CANDIDATE), *args], env=dict(os.environ, PM_OPS_DB=db),
                          capture_output=True, text=True, timeout=60)


def iso(minutes: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def seed(db: str, *extra: str) -> str:
    out = run(db, "obligation-upsert", "--kind", "orphaned_issue", "--target-type", "github_issue",
              "--target-id", "7", "--issue", "7", "--title", "seed", "--print-id", *extra)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def row(db: str, rid: str) -> tuple[str, int]:
    con = sqlite3.connect(db)
    try:
        status = con.execute("SELECT status FROM obligations WHERE id=?", (rid,)).fetchone()[0]
        receipts = con.execute("SELECT COUNT(*) FROM events WHERE event_type='obligation-resolve'").fetchone()[0]
        return status, int(receipts)
    finally:
        con.close()


def resolve(db: str, rid: str) -> subprocess.CompletedProcess[str]:
    return run(db, "obligation-resolve", "--kind", "orphaned_issue", "--id", rid, "--if-unclaimed", "--reason", "t")


def test_unclaimed_row_resolves_with_receipt(tmp_path):
    db = str(tmp_path / "a.db")
    rid = seed(db)
    out = resolve(db, rid)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "resolved"
    assert row(db, rid) == ("resolved", 1)


def test_live_claim_is_skipped_and_untouched(tmp_path):
    db = str(tmp_path / "b.db")
    rid = seed(db, "--owner", "ci-repair-agent", "--suppress-until", iso(30))
    out = resolve(db, rid)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "skipped_claimed"
    assert row(db, rid) == ("open", 0)


def test_expired_claim_and_owner_without_suppress_resolve(tmp_path):
    db = str(tmp_path / "c.db")
    expired = seed(db, "--owner", "ci-repair-agent", "--suppress-until", iso(-5))
    assert resolve(db, expired).stdout.strip() == "resolved"
    db2 = str(tmp_path / "c2.db")
    generic = seed(db2, "--owner", "PM")  # producer-set owner, no suppress_until: not a claim (S45)
    assert resolve(db2, generic).stdout.strip() == "resolved"


def test_malformed_suppress_with_owner_is_treated_as_claimed(tmp_path):
    db = str(tmp_path / "d.db")
    rid = seed(db, "--owner", "agent", "--suppress-until", "not-a-date")
    assert resolve(db, rid).stdout.strip() == "skipped_claimed"
    assert row(db, rid)[0] == "open"


def test_requires_id_and_reports_not_open(tmp_path):
    db = str(tmp_path / "e.db")
    rid = seed(db)
    no_id = run(db, "obligation-resolve", "--kind", "orphaned_issue", "--issue", "7", "--if-unclaimed")
    assert no_id.returncode != 0 and "--id" in (no_id.stderr + no_id.stdout)
    assert row(db, rid)[0] == "open"
    assert resolve(db, rid).stdout.strip() == "resolved"
    again = resolve(db, rid)
    assert again.returncode == 0 and again.stdout.strip() == "skipped_not_open"
    assert row(db, rid) == ("resolved", 1)


def test_flagless_resolve_unchanged_for_claimed_row(tmp_path):
    db = str(tmp_path / "f.db")
    rid = seed(db, "--owner", "agent", "--suppress-until", iso(30))
    out = run(db, "obligation-resolve", "--kind", "orphaned_issue", "--id", rid)
    assert out.returncode == 0 and out.stdout.strip() == ""
    assert row(db, rid)[0] == "resolved"


def test_claim_landing_during_resolve_wins_the_race(tmp_path):
    """A claim committed while the resolver waits for the write lock is honoured:
    the check runs inside the resolver's BEGIN IMMEDIATE, after the claim."""
    db = str(tmp_path / "g.db")
    rid = seed(db)
    con = sqlite3.connect(db, isolation_level=None, timeout=15)
    con.execute("BEGIN IMMEDIATE")
    con.execute("UPDATE obligations SET owner='late-agent', suppress_until=? WHERE id=?", (iso(30), rid))
    proc = subprocess.Popen([sys.executable, str(CANDIDATE), "obligation-resolve", "--kind", "orphaned_issue",
                             "--id", rid, "--if-unclaimed", "--reason", "t"],
                            env=dict(os.environ, PM_OPS_DB=db), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(1.0)
    assert proc.poll() is None, "resolver must block on the held write lock"
    con.execute("COMMIT")
    con.close()
    stdout, stderr = proc.communicate(timeout=60)
    assert proc.returncode == 0, stderr
    assert stdout.strip() == "skipped_claimed"
    assert row(db, rid) == ("open", 0)


def test_resolve_racing_a_concurrent_resolve_is_skipped_not_open(tmp_path):
    db = str(tmp_path / "h.db")
    rid = seed(db)
    con = sqlite3.connect(db, isolation_level=None, timeout=15)
    con.execute("BEGIN IMMEDIATE")
    con.execute("UPDATE obligations SET status='resolved' WHERE id=?", (rid,))
    proc = subprocess.Popen([sys.executable, str(CANDIDATE), "obligation-resolve", "--kind", "orphaned_issue",
                             "--id", rid, "--if-unclaimed", "--reason", "t"],
                            env=dict(os.environ, PM_OPS_DB=db), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(1.0)
    con.execute("COMMIT")
    con.close()
    stdout, stderr = proc.communicate(timeout=60)
    assert proc.returncode == 0, stderr
    assert stdout.strip() == "skipped_not_open"
    assert row(db, rid) == ("resolved", 0)


def _fp(db: str, rid: str) -> tuple[str, str]:
    import hashlib
    con = sqlite3.connect(db)
    try:
        updated, evidence = con.execute("SELECT updated_at, evidence_json FROM obligations WHERE id=?", (rid,)).fetchone()
    finally:
        con.close()
    return updated, hashlib.sha256((evidence or "").encode()).hexdigest()


def test_fingerprint_match_resolves_and_mismatch_is_skipped_changed(tmp_path):
    db = str(tmp_path / "fp.db")
    rid = seed(db, "--evidence-json", '{"reasons": ["done_open"]}')
    updated, digest = _fp(db, rid)
    stale = run(db, "obligation-resolve", "--kind", "orphaned_issue", "--id", rid, "--if-unclaimed", "--reason", "t",
                "--expect-updated-at", updated, "--expect-evidence-sha256", "0" * 64)
    assert stale.returncode == 0 and stale.stdout.strip() == "skipped_changed"
    stale_ts = run(db, "obligation-resolve", "--kind", "orphaned_issue", "--id", rid, "--if-unclaimed", "--reason", "t",
                   "--expect-updated-at", "1999-01-01T00:00:00Z", "--expect-evidence-sha256", digest)
    assert stale_ts.stdout.strip() == "skipped_changed" and row(db, rid) == ("open", 0)
    ok = run(db, "obligation-resolve", "--kind", "orphaned_issue", "--id", rid, "--if-unclaimed", "--reason", "t",
             "--expect-updated-at", updated, "--expect-evidence-sha256", digest)
    assert ok.stdout.strip() == "resolved" and row(db, rid) == ("resolved", 1)


def test_upsert_landing_while_resolver_waits_is_skipped_changed(tmp_path):
    """Fingerprint taken, then an upsert holds the write lock and changes evidence; the waiting
    resolver re-checks under BEGIN IMMEDIATE and skips."""
    db = str(tmp_path / "fprace.db")
    rid = seed(db, "--evidence-json", '{"reasons": ["done_open"]}')
    updated, digest = _fp(db, rid)
    con = sqlite3.connect(db, isolation_level=None, timeout=15)
    con.execute("BEGIN IMMEDIATE")
    con.execute("UPDATE obligations SET evidence_json=? WHERE id=?", ('{"reasons": ["done_open", "partial_merge"]}', rid))
    proc = subprocess.Popen([sys.executable, str(CANDIDATE), "obligation-resolve", "--kind", "orphaned_issue",
                             "--id", rid, "--if-unclaimed", "--reason", "t",
                             "--expect-updated-at", updated, "--expect-evidence-sha256", digest],
                            env=dict(os.environ, PM_OPS_DB=db), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(1.0)
    con.execute("COMMIT")
    con.close()
    stdout, stderr = proc.communicate(timeout=60)
    assert proc.returncode == 0, stderr
    assert stdout.strip() == "skipped_changed" and row(db, rid) == ("open", 0)
