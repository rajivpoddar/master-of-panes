"""Inventory rows (ready_pool, triage_retained_scope) are kept but are not PM actions.

Rajiv C0ALZJHGE49 thread 1793864837.357739, ts 1793867355.054319: "what are
these open obligations? i have asked the pm to fix this multiple times".
Hermetic: canonical sources against a tmp_path PM_OPS_DB.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "shared-assets" / "claude"
LEGACY = ASSETS / "scripts" / "pm-ops-legacy.py"
VALIDATOR = ASSETS / "hooks" / "pm-ops-sync-stop-validator.sh"


def _env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(PM_OPS_DB=str(tmp_path / "pm-ops.db"), PM_OPS=str(LEGACY),
               PR_OPEN_SNAPSHOT=str(tmp_path / "absent-snapshot.py"), PM_OPS_STOP_LOG=str(tmp_path / "stop.log"),
               PR_SYNC_CACHE_STAMP=str(tmp_path / "stamp"),
               PR_OPEN_ACTIONS_STATE=str(tmp_path / "absent-pr-actions.json"))
    return env


def _pm(env, *args):
    return subprocess.run([sys.executable, str(LEGACY), *args], env=env, capture_output=True, text=True, timeout=60)


def _seed(env, kind, issue, severity="high", horizon="hourly", evidence=None):
    out = _pm(env, "obligation-upsert", "--kind", kind, "--target-type", "github_issue", "--target-id", str(issue),
              "--issue", str(issue), "--severity", severity, "--horizon", horizon, "--owner", "pm",
              "--title", f"{kind} {issue}", "--action", f"act on {kind} {issue}",
              "--evidence-json", json.dumps(evidence or {"pm_stop_actionable": True}))
    assert "REFUSED" not in out.stderr, out.stderr


def test_status_excludes_inventory_from_actionable_counts_and_reports_it(tmp_path):
    env = _env(tmp_path)
    _seed(env, "ready_pool", 1)
    _seed(env, "ready_pool", 2, severity="critical")
    _seed(env, "triage_retained_scope", 3)
    _seed(env, "cleanup_pr", 4)
    status = json.loads(_pm(env, "status", "--format", "json", "--read-only").stdout)
    assert status["open_obligations"] == 1
    assert status["open_obligations_all"] == 4
    assert status["inventory_obligations"] == {"ready_pool": 2, "triage_retained_scope": 1}
    assert status["inventory_open_obligations"] == 3
    assert status["high_open_obligations"] == 1
    assert status["actionable_high_open_obligations"] == 1
    assert status["open_obligations_by_horizon"] == {"hourly": 1}


def test_obligation_list_actionable_and_inventory(tmp_path):
    env = _env(tmp_path)
    _seed(env, "ready_pool", 1)
    _seed(env, "triage_retained_scope", 3)
    _seed(env, "cleanup_pr", 4)
    actionable = json.loads(_pm(env, "obligation-list", "--actionable", "--format", "json").stdout)
    assert actionable["count"] == 1 and actionable["by_kind"] == {"cleanup_pr": 1}
    inventory = json.loads(_pm(env, "obligation-list", "--inventory", "--counts", "--format", "json").stdout)
    assert inventory["by_kind"] == {"ready_pool": 1, "triage_retained_scope": 1} and inventory["rows"] == []
    text = _pm(env, "obligation-list", "--actionable").stdout
    assert text.startswith("actionable open obligations: 1") and "ready_pool" not in text


def test_stop_hook_never_blocks_on_inventory_rows(tmp_path):
    env = _env(tmp_path)
    _seed(env, "ready_pool", 1)
    _seed(env, "triage_retained_scope", 3)
    out = subprocess.run(["bash", str(VALIDATOR)], env=env, input="{}", capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "PM_OPS_ACTION_REQUIRED" not in out.stdout
    _seed(env, "cleanup_pr", 4)
    out = subprocess.run(["bash", str(VALIDATOR)], env=env, input="{}", capture_output=True, text=True, timeout=60)
    assert "PM_OPS_ACTION_REQUIRED" in out.stdout and "cleanup_pr" in out.stdout
    assert "ready_pool 1" not in out.stdout and "triage_retained_scope 3" not in out.stdout
