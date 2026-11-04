"""hourly_report_due: hourly-thread-ts --set binds the report bucket + post receipt; Stop surfaces it.

Rajiv D0AMF0XE6TS thread 1793783982.877709, ts 1793784122.949659:
"hourly report should never be missed. can MoP add an obligation for it automatically?"
CTO REVISE C0ALZJHGE49 thread 1793789393.531209, ts 1793790035.862469 (path 3).
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
SKILL = ASSETS / "skills" / "hourly-heartbeat" / "SKILL.md"
CHANNEL = "C0ALZJHGE49"


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


def _rows(env: dict[str, str]) -> list[tuple]:
    if not Path(env["PM_OPS_DB"]).exists():
        return []
    con = sqlite3.connect(env["PM_OPS_DB"])
    try:
        return con.execute(
            "SELECT target_id, status, resolved_reason, target_type FROM obligations "
            "WHERE kind='hourly_report_due' ORDER BY id"
        ).fetchall()
    finally:
        con.close()


def _upsert(env: dict[str, str], bucket: str, next_review_at: str | None = None, target_type: str = "hourly_bucket") -> None:
    # Same arguments MoP's hourlyReportUpsertArgs passes (pinned in the node test).
    args = [
        sys.executable, str(LEGACY), "obligation-upsert",
        "--kind", "hourly_report_due", "--target-type", target_type, "--target-id", bucket,
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
    assert any(r[0] == bucket and r[1] == "open" and r[3] == target_type for r in _rows(env)), out.stderr


def _bucket(epoch: float) -> str:
    return time.strftime("%Y-%m-%d:%H", time.localtime(epoch))


def _bucket_start(epoch: float) -> float:
    return time.mktime(time.strptime(_bucket(epoch), "%Y-%m-%d:%H"))


def _ts(epoch: float) -> str:
    return f"{int(epoch)}.123456"


def _set(env, ts, bucket=None, receipt=None):
    args = [sys.executable, str(HOURLY), "--set", ts]
    if bucket is not None:
        args += ["--bucket", bucket]
    if receipt is not None:
        args += ["--receipt", receipt]
    return subprocess.run(args, env=env, capture_output=True, text=True)


def _receipt(ts: str, channel: str = CHANNEL) -> str:
    return f"posting...\nOK ts={ts} channel={channel}\n"


def _pointer(env) -> str | None:
    p = Path(env["HOURLY_THREAD_STATE"])
    return json.loads(p.read_text())["ts"] if p.exists() else None


def _status(env) -> dict[str, tuple[str, str | None]]:
    return {r[0]: (r[1], r[2]) for r in _rows(env)}


def test_report_resolves_own_bucket_and_supersedes_older_but_never_newer(tmp_path: Path) -> None:
    env = _env(tmp_path)
    now = time.time()
    current = _bucket(now)
    older = _bucket(now - 3600)
    newer = _bucket(now + 3600)
    for bucket in (older, current, newer):
        _upsert(env, bucket)
    ts = _ts(max(now - 5, _bucket_start(now) + 1))
    out = _set(env, ts, current, _receipt(ts))
    assert out.returncode == 0, out.stderr
    rows = _status(env)
    assert rows[current][0] == "resolved" and rows[current][1].startswith(f"hourly report for {current} posted ts={ts}")
    assert rows[older][0] == "resolved" and rows[older][1].startswith("superseded:")
    assert rows[newer] == ("open", None)
    assert _pointer(env) == ts


def test_set_without_bucket_or_receipt_records_pointer_but_resolves_nothing(tmp_path: Path) -> None:
    env = _env(tmp_path)
    now = time.time()
    _upsert(env, _bucket(now))
    ts = _ts(now - 5)
    out = _set(env, ts)
    assert out.returncode == 0 and "NOT resolved" in out.stderr
    assert _pointer(env) == ts
    assert [r[1] for r in _rows(env)] == ["open"]


def test_future_ts_is_refused_and_changes_nothing(tmp_path: Path) -> None:
    env = _env(tmp_path)
    now = time.time()
    _upsert(env, _bucket(now))
    old = _ts(now - 60)
    assert _set(env, old).returncode == 0
    future = _ts(now + 3600)
    out = _set(env, future, _bucket(now), _receipt(future))
    assert out.returncode == 2
    assert _pointer(env) == old, "pointer unchanged"
    assert [r[1] for r in _rows(env)] == ["open"], "future ts clears no debt"


def test_old_late_report_resolves_only_its_bucket_and_never_moves_pointer_back(tmp_path: Path) -> None:
    env = _env(tmp_path)
    now = time.time()
    prev, current = _bucket(now - 3600), _bucket(now)
    older = _bucket(now - 7200)
    for bucket in (older, prev, current):
        _upsert(env, bucket)
    newest = _ts(now - 2)
    assert _set(env, newest).returncode == 0  # a newer post is already the pointer
    late_epoch = _bucket_start(now - 3600) + 60
    late = _ts(late_epoch)
    out = _set(env, late, prev, _receipt(late))
    assert out.returncode == 0, out.stderr
    rows = _status(env)
    assert rows[prev][0] == "resolved" and rows[prev][1].startswith(f"hourly report for {prev}")
    assert rows[current] == ("open", None), "a late old report never certifies newer debt"
    assert rows[older] == ("open", None), "a late report is individual delivery, not supersession"
    assert _pointer(env) == newest, "pointer never moves backward"


def test_wrong_bucket_or_bad_receipt_resolves_nothing(tmp_path: Path) -> None:
    env = _env(tmp_path)
    now = time.time()
    current = _bucket(now)
    _upsert(env, current)
    ts = _ts(max(now - 5, _bucket_start(now) + 1))
    cases = [
        (_bucket(now + 3600), _receipt(ts)),               # future bucket
        (_bucket(now - 4 * 3600), _receipt(ts)),           # too old to be certified by this post
        ("2026-13-40:99", _receipt(ts)),                   # invalid bucket
        (current, _receipt(_ts(now - 30))),                # receipt for another post
        (current, _receipt(ts, channel="C000000")),        # wrong channel
        (current, "ERROR: Slack post failed"),             # failed post
    ]
    for bucket, receipt in cases:
        out = _set(env, ts, bucket, receipt)
        assert out.returncode == 3, (bucket, receipt, out.stderr)
        assert [r[1] for r in _rows(env)] == ["open"], (bucket, receipt)


def test_only_hourly_bucket_target_type_is_touched(tmp_path: Path) -> None:
    env = _env(tmp_path)
    now = time.time()
    current = _bucket(now)
    _upsert(env, current, target_type="session")
    ts = _ts(max(now - 5, _bucket_start(now) + 1))
    assert _set(env, ts, current, _receipt(ts)).returncode == 0
    assert [r[1] for r in _rows(env)] == ["open"]


def test_set_is_idempotent_and_fails_open_without_ledger(tmp_path: Path) -> None:
    env = _env(tmp_path)
    now = time.time()
    current = _bucket(now)
    ts = _ts(max(now - 5, _bucket_start(now) + 1))
    out = _set(env, ts, current, _receipt(ts))  # no pm-ops DB at all
    assert out.returncode == 0, out.stderr
    assert _pointer(env) == ts
    _upsert(env, current)
    for _ in range(2):
        assert _set(env, ts, current, _receipt(ts)).returncode == 0
    assert [r[1] for r in _rows(env)] == ["resolved"]


def _run_validator(env: dict[str, str]) -> str:
    out = subprocess.run(
        ["bash", str(VALIDATOR)], env=env, input=json.dumps({}), capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_stop_validator_surfaces_due_debt_waits_for_review_and_clears_after_post(tmp_path: Path) -> None:
    env = _env(tmp_path)
    now = time.time()
    bucket = _bucket(now)
    later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _upsert(env, bucket, next_review_at=later)
    assert "hourly-heartbeat" not in _run_validator(env)
    con = sqlite3.connect(env["PM_OPS_DB"])
    con.execute("UPDATE obligations SET next_review_at=NULL")
    con.commit()
    con.close()
    stdout = _run_validator(env)
    assert '"decision": "block"' in stdout
    assert "Invoke Skill(hourly-heartbeat); post report for " + bucket in stdout
    ts = _ts(max(now - 5, _bucket_start(now) + 1))
    assert _set(env, ts, bucket, _receipt(ts)).returncode == 0
    assert "hourly-heartbeat" not in _run_validator(env)


def test_skill_passes_compose_bucket_and_receipt_in_one_command() -> None:
    text = SKILL.read_text()
    assert "date +%Y-%m-%d:%H > /tmp/heartbeat-1h-bucket" in text
    step3 = text.split("STEP 3", 1)[1].split("STEP 4", 1)[0]
    line = next(l for l in step3.splitlines() if "hourly-thread-ts --set" in l)
    assert line.strip().startswith("RECEIPT=$(SLACK_SEND_AS=alerts")
    assert '--bucket "$(cat /tmp/heartbeat-1h-bucket)" --receipt "$RECEIPT"' in line


def test_validator_allowlists_kind_without_touching_user_prompt_submit() -> None:
    assert '"hourly_report_due",' in VALIDATOR.read_text()
    for path in (VALIDATOR, HOURLY):
        assert "UserPromptSubmit" not in path.read_text()
