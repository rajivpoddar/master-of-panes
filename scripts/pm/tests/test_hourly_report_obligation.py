"""hourly_report_due: hourly-thread-ts --set resolves it; the Stop validator surfaces it.

Rajiv D0AMF0XE6TS thread 1793783982.877709, ts 1793784122.949659:
"hourly report should never be missed. can MoP add an obligation for it automatically?"
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "shared-assets" / "claude"
LEGACY = ASSETS / "scripts" / "pm-ops-legacy.py"
HOURLY = ASSETS / "scripts" / "hourly-thread-ts"
VALIDATOR = ASSETS / "hooks" / "pm-ops-sync-stop-validator.sh"


def _env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        PM_OPS_DB=str(tmp_path / "pm-ops.db"),
        PM_OPS=str(LEGACY),
        HOURLY_THREAD_STATE=str(tmp_path / "hourly-thread.json"),
        PR_OPEN_SNAPSHOT=str(tmp_path / "absent-snapshot.py"),
        PM_OPS_STOP_LOG=str(tmp_path / "stop.log"),
    )
    return env


def _upsert(env: dict[str, str], bucket: str, next_review_at: str | None = None) -> None:
    # Same arguments MoP's hourlyReportUpsertArgs passes (pinned in the node test).
    args = [
        sys.executable, str(LEGACY), "obligation-upsert",
        "--kind", "hourly_report_due", "--target-type", "hourly_bucket", "--target-id", bucket,
        "--owner", "pm", "--severity", "high", "--horizon", "hourly",
        "--title", f"Hourly report due for {bucket}",
        "--action", f"Invoke Skill(hourly-heartbeat); post report for {bucket}.",
        "--evidence-json", json.dumps({"bucket": bucket, "source": "mop-pm-cadence"}),
    ]
    if next_review_at:
        args += ["--next-review-at", next_review_at]
    # pm-ops-legacy exits 2 when the affected row id is exactly 2 (oid/refusal
    # collision in main()); assert on the ledger row instead of the exit code.
    out = subprocess.run(args, env=env, capture_output=True, text=True)
    assert "REFUSED" not in out.stderr, out.stderr
    assert any(r[0] == bucket and r[1] == "open" for r in _rows(env)), out.stderr


def _rows(env: dict[str, str]) -> list[tuple]:
    con = sqlite3.connect(env["PM_OPS_DB"])
    try:
        return con.execute(
            "SELECT target_id, status, resolved_reason FROM obligations WHERE kind='hourly_report_due' ORDER BY id"
        ).fetchall()
    finally:
        con.close()


def _bucket(epoch: float) -> str:
    return time.strftime("%Y-%m-%d:%H", time.localtime(epoch))


def _slack_ts(epoch: float) -> str:
    return f"{int(epoch)}.123456"


def test_set_resolves_current_and_older_buckets_but_not_future(tmp_path: Path) -> None:
    env = _env(tmp_path)
    now = time.time()
    current, older, future = _bucket(now), _bucket(now - 3600), _bucket(now + 3600)
    for bucket in (older, current, future):
        _upsert(env, bucket)
    ts = _slack_ts(now)
    out = subprocess.run([sys.executable, str(HOURLY), "--set", ts], env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ts
    rows = {target: (status, reason) for target, status, reason in _rows(env)}
    assert rows[current][0] == "resolved"
    assert rows[older][0] == "resolved"
    assert ts in rows[current][1]
    assert rows[future][0] == "open"
    assert json.loads(Path(env["HOURLY_THREAD_STATE"]).read_text())["ts"] == ts


def test_set_is_idempotent_and_fails_open_without_ledger(tmp_path: Path) -> None:
    env = _env(tmp_path)
    ts = _slack_ts(time.time())
    # No pm-ops DB at all: --set still records the thread and exits 0.
    out = subprocess.run([sys.executable, str(HOURLY), "--set", ts], env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert json.loads(Path(env["HOURLY_THREAD_STATE"]).read_text())["ts"] == ts
    _upsert(env, _bucket(time.time()))
    _upsert(env, _bucket(time.time()))  # duplicate upsert keeps one open row
    assert len(_rows(env)) == 1
    for _ in range(2):
        out = subprocess.run([sys.executable, str(HOURLY), "--set", ts], env=env, capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
    assert [r[1] for r in _rows(env)] == ["resolved"]


def _run_validator(env: dict[str, str]) -> str:
    out = subprocess.run(
        ["bash", str(VALIDATOR)], env=env, input=json.dumps({}), capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_stop_validator_surfaces_due_hourly_report_as_executable(tmp_path: Path) -> None:
    env = _env(tmp_path)
    bucket = _bucket(time.time())
    _upsert(env, bucket)
    stdout = _run_validator(env)
    assert '"decision": "block"' in stdout
    assert "Invoke Skill(hourly-heartbeat); post report for " + bucket in stdout


def test_stop_validator_waits_for_next_review_and_clears_after_post(tmp_path: Path) -> None:
    env = _env(tmp_path)
    bucket = _bucket(time.time())
    later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _upsert(env, bucket, next_review_at=later)
    assert "hourly_report_due" not in _run_validator(env) and "hourly-heartbeat" not in _run_validator(env)
    con = sqlite3.connect(env["PM_OPS_DB"])
    con.execute("UPDATE obligations SET next_review_at=NULL")
    con.commit()
    con.close()
    assert "hourly-heartbeat" in _run_validator(env)
    subprocess.run([sys.executable, str(HOURLY), "--set", _slack_ts(time.time())], env=env, check=True, capture_output=True)
    assert "hourly-heartbeat" not in _run_validator(env)


def test_validator_allowlists_kind_without_touching_user_prompt_submit() -> None:
    text = VALIDATOR.read_text()
    assert '"hourly_report_due",' in text
    assert "UserPromptSubmit" not in text
    assert "UserPromptSubmit" not in HOURLY.read_text()
