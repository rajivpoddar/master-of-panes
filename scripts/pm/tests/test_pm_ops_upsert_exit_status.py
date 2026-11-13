"""The actual CLI must distinguish a successful row ID 2 from refusal exit 2."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

WRITER = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts/pm-ops-legacy.py"


def cli(db, *args):
    return subprocess.run(
        [sys.executable, str(WRITER), *args],
        env=dict(os.environ, PM_OPS_DB=str(db)),
        capture_output=True, text=True, timeout=30,
    )


def upsert(db, target, evidence):
    return cli(db, "obligation-upsert", "--kind", "followup", "--target-type", "test",
               "--target-id", target, "--title", target, "--action", "Keep the saved commitment",
               "--evidence-json", json.dumps(evidence), "--print-id")


def refuse(db):
    return cli(db, "obligation-upsert", "--kind", "capture_recovery", "--pr", "77",
               "--owner", "pm", "--title", "invalid head", "--action", "Verify the exact head",
               "--evidence-json", json.dumps({"head": "a" * 39}))


def test_insert_and_update_row_two_are_success_without_duplicate(tmp_path):
    db = tmp_path / "pm-ops.db"
    first = upsert(db, "first", {"state": "UNKNOWN_CONSUMED_NO_RETRY"})
    assert first.returncode == 0 and first.stdout.strip() == "1", first.stderr
    second = upsert(db, "second", {"revision": 1})
    assert second.returncode == 0 and second.stdout.strip() == "2", second.stderr
    updated = upsert(db, "second", {"revision": 2})
    assert updated.returncode == 0 and updated.stdout.strip() == "2", updated.stderr
    with sqlite3.connect(db) as con:
        rows = con.execute("SELECT id, status, evidence_json FROM obligations ORDER BY id").fetchall()
    assert len(rows) == 2 and all(row[1] == "open" for row in rows)
    assert json.loads(rows[0][2]) == {"state": "UNKNOWN_CONSUMED_NO_RETRY"}
    assert json.loads(rows[1][2]) == {"revision": 2}


def test_upsert_merges_evidence_and_overwrites_only_supplied_keys(tmp_path):
    db = tmp_path / "pm-ops.db"
    assert upsert(db, "merge", {"keep": "original", "replace": "old"}).returncode == 0
    result = upsert(db, "merge", {"replace": "new", "added": 42})
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(db) as con:
        rows = con.execute("SELECT evidence_json FROM obligations WHERE target_id='merge'").fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0][0]) == {"keep": "original", "replace": "new", "added": 42}


def test_true_refusal_keeps_exit_two_and_does_not_initialize_db(tmp_path):
    db = tmp_path / "absent.db"
    result = refuse(db)
    assert result.returncode == 2
    assert "REFUSED: continuation_head_malformed" in result.stderr
    assert not db.exists()


def test_true_refusal_does_not_change_existing_rows_or_receipts(tmp_path):
    db = tmp_path / "existing.db"
    upsert(db, "first", {"state": "UNKNOWN_CONSUMED_NO_RETRY"})
    upsert(db, "second", {"revision": 1})
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    result = refuse(db)
    assert result.returncode == 2
    assert "REFUSED: continuation_head_malformed" in result.stderr
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
