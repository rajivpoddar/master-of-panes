"""The Stop hook surfaces live PR state from the pr-open-snapshot actions file.

CTO C0ALZJHGE49 thread 1793872014.789359, ts 1793883179.092259: routine issues,
PR status and Ready Pool membership must NOT automatically create obligations.
pr_admission / pr_red_unowned are no longer pm-ops rows; the validator reads the
snapshot's actions file instead, honours PM claim rows and fails open.
Hermetic: canonical sources against a tmp_path PM_OPS_DB and actions file.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "shared-assets" / "claude"
LEGACY = ASSETS / "scripts" / "pm-ops-legacy.py"
VALIDATOR = ASSETS / "hooks" / "pm-ops-sync-stop-validator.sh"


def _iso(delta_minutes: float = 0) -> str:
    ts = datetime.now(timezone.utc) + timedelta(minutes=delta_minutes)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(PM_OPS_DB=str(tmp_path / "pm-ops.db"), PM_OPS=str(LEGACY),
               PR_OPEN_SNAPSHOT=str(tmp_path / "absent-snapshot.py"), PM_OPS_STOP_LOG=str(tmp_path / "stop.log"),
               PR_SYNC_CACHE_STAMP=str(tmp_path / "stamp"),
               PR_OPEN_ACTIONS_STATE=str(tmp_path / "pr-actions.json"))
    return env


def _pm(env, *args):
    return subprocess.run([sys.executable, str(LEGACY), *args], env=env, capture_output=True, text=True, timeout=60)


def _actions(env, items, observed_at=None):
    Path(env["PR_OPEN_ACTIONS_STATE"]).write_text(json.dumps({"observed_at": observed_at or _iso(), "items": items}))


def _item(pr, kind="pr_admission"):
    return {"pr": pr, "kind": kind, "head": "a" * 40, "title": f"PR #{pr} not admitted: x",
            "action": "launch pm-admission-reviewer now (PASS -> pm-admit-ci.sh same turn)"}


def _stop(env):
    out = subprocess.run(["bash", str(VALIDATOR)], env=env, input="{}", capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _init_db(env):
    # Any write creates the canonical schema; resolve it so the ledger is empty.
    _pm(env, "obligation-upsert", "--kind", "cleanup_pr", "--target-type", "github_pr", "--target-id", "1",
        "--pr", "1", "--title", "seed", "--action", "seed")
    out = _pm(env, "obligation-resolve", "--kind", "cleanup_pr", "--id", "1", "--reason", "seed")
    assert out.returncode == 0, out.stderr


def _open_rows(env):
    import sqlite3

    con = sqlite3.connect(env["PM_OPS_DB"])
    try:
        return con.execute("SELECT kind, pr FROM obligations WHERE status='open'").fetchall()
    finally:
        con.close()


def test_fresh_live_pr_actions_are_surfaced_without_rows(tmp_path):
    env = _env(tmp_path)
    _init_db(env)
    _actions(env, [_item(10), _item(11, "pr_red_unowned")])
    out = _stop(env)
    assert "PM_OPS_ACTION_REQUIRED" in out
    assert "live-pr:pr_admission:10" in out and "live-pr:pr_red_unowned:11" in out
    assert "launch pm-admission-reviewer" in out
    assert _open_rows(env) == []  # surfacing never writes a row


def test_missing_stale_or_malformed_actions_file_fails_open(tmp_path):
    env = _env(tmp_path)
    _init_db(env)
    assert _stop(env).strip() == ""                       # missing
    _actions(env, [_item(10)], observed_at=_iso(-30))
    assert _stop(env).strip() == ""                       # stale (>15 min)
    Path(env["PR_OPEN_ACTIONS_STATE"]).write_text("{not json")
    assert _stop(env).strip() == ""                       # malformed
    _actions(env, [{"pr": "12", "kind": "pr_admission"}, {"pr": 13, "kind": "ready_pool"}])
    assert _stop(env).strip() == ""                       # foreign shapes ignored


def test_pm_claim_row_silences_live_action_until_suppress_lapses(tmp_path):
    env = _env(tmp_path)
    _init_db(env)
    _pm(env, "obligation-upsert", "--kind", "pr_red_unowned", "--target-type", "github_pr", "--target-id", "11",
        "--pr", "11", "--owner", "ci-repair-agent", "--suppress-until", _iso(60))
    _actions(env, [_item(11, "pr_red_unowned")])
    assert "live-pr:pr_red_unowned:11" not in _stop(env)
    _pm(env, "obligation-upsert", "--kind", "pr_red_unowned", "--target-type", "github_pr", "--target-id", "11",
        "--pr", "11", "--owner", "ci-repair-agent", "--suppress-until", _iso(-1))
    assert "live-pr:pr_red_unowned:11" in _stop(env)


def test_resolution_this_turn_still_releases_live_pr_actions(tmp_path):
    env = _env(tmp_path)
    _init_db(env)
    _actions(env, [_item(10)])
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "user", "timestamp": "2000-01-01T00:00:00Z",
                                      "message": {"content": "go"}}) + "\n")
    out = subprocess.run(["bash", str(VALIDATOR)], env=env, input=json.dumps({"transcript_path": str(transcript)}),
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0
    assert out.stdout.strip() == ""  # the seed resolve counts as progress this turn
