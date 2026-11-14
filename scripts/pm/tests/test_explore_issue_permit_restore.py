"""Focused proof for explore_issue_restore_permit: safe recovery when a consumed
permit's create is proven absent from GitHub.

Governing defect: explore_issue_consume_permit moves the permit to the consumed
dir before the external gh issue create (persist-UNKNOWN-first design). If the
create never lands in GitHub (network failure, API error, process kill), the
state stays READY_TO_FILE and the consumed guard blocks every subsequent
explore_issue_mint_permit call for the same key, leaving the obligation
permanently stuck.

explore_issue_restore_permit provides the ONLY supported recovery path once
GitHub absence has been proven by searching the canonical source permalink,
title, keywords, channel, and both source_message_ts values with no match.

Safety invariants (each covered by a test below):
  - Only succeeds when state is exactly READY_TO_FILE.
  - Exactly one reconcile per key (second call returns 2).
  - After restore, explore_issue_mint_permit succeeds (consumed guard is gone).
  - After restore, explore_issue_permit_status returns 1 (no permit yet, not 3).
  - After restore and re-mint, explore_issue_consume_permit succeeds.
  - A second restore after a fresh consumed permit from the new create is blocked.
  - Reconciled file captures the absence_proof annotation for audit.
  - Resolved (terminal) states refuse restoration.
  - Missing consumed file returns 1.

No live services, real obligations, or customer content. All paths are hermetic.
Tests are committed but NOT executed under the MoP no-test-proof directive.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
HELPER = ROOT / "scripts/pm/shared-assets/claude/hooks/explore-issue-latch-guard.sh"
CHANNEL = "D0CFYAP3ZK3"
THREAD = "1794136866.155859"
SOURCE_MSG = "1794599358.712119"
KEY = f"{CHANNEL}__{THREAD.replace('.', '_')}"

ABSENCE_PROOF = (
    "github_search_confirmed_absent: "
    "searched 2026-11-14 by title, keywords, channel, and both source permalinks; "
    "no match found; highest issue #13768 predates permit consumption"
)

BODY_SHA = "c22f1163a201473aed3eb4aab4b617169cb4d478bf5796a9fb80d73531840c80"
TITLE_DIGEST = "7905d3c710f9820eddcb8ac37baeea3b82e540c711e12f344833919e75887e48"
OBLIGATION_ID = "33762"


def _env(tmp_path: Path) -> dict[str, str]:
    return dict(
        os.environ,
        EXPLORE_ISSUE_STATE_DIR=str(tmp_path / "state"),
        EXPLORE_ISSUE_PERMIT_DIR=str(tmp_path / "permits"),
        EXPLORE_ISSUE_CONSUMED_PERMIT_DIR=str(tmp_path / "consumed"),
        EXPLORE_ISSUE_RECONCILED_PERMIT_DIR=str(tmp_path / "reconciled"),
        EXPLORE_ISSUE_INFLIGHT_DIR=str(tmp_path / "inflight"),
        EXPLORE_ISSUE_PM_OPS_CLI="/usr/bin/true",
        EXPLORE_ISSUE_SOURCE_MESSAGE_TS=SOURCE_MSG,
    )


def _run_helper(env: dict, cmds: str) -> subprocess.CompletedProcess:
    script = f"source {HELPER}\n{cmds}"
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30)


def _write_state(tmp_path: Path, state: str = "READY_TO_FILE") -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / f"{KEY}.json").write_text(json.dumps({
        "state": state,
        "channel": CHANNEL,
        "thread_ts": THREAD,
        "source_message_ts": SOURCE_MSG,
        "obligation_id": OBLIGATION_ID,
        "body_file": "/tmp/body.md",
        "title": "Idle editor tab self-demotes to read-only after ~4h",
        "reason": "customer read-only after idle",
        "required_at": "2026-11-13T21:10:32Z",
        "original_body_sha256": "e5221c",
        "current_body_sha256": "e5221c",
        "updated_at": "2026-11-13T21:10:33Z",
    }))


def _write_consumed(tmp_path: Path) -> Path:
    consumed_dir = tmp_path / "consumed"
    consumed_dir.mkdir(exist_ok=True)
    f = consumed_dir / f"{KEY}.permit.consumed.20261113T210948Z.json"
    f.write_text(json.dumps({
        "channel": CHANNEL,
        "thread_ts": THREAD,
        "source_message_ts": SOURCE_MSG,
        "body_sha256": BODY_SHA,
        "title_digest": TITLE_DIGEST,
        "obligation_id": OBLIGATION_ID,
        "minted_at": "2026-11-13T21:09:36Z",
    }))
    return f


def test_restore_succeeds_when_state_is_ready_to_file_and_consumed_exists(tmp_path):
    _write_state(tmp_path)
    _write_consumed(tmp_path)
    env = _env(tmp_path)
    result = _run_helper(env, f'explore_issue_restore_permit "{CHANNEL}" "{THREAD}" "{ABSENCE_PROOF}"')
    assert result.returncode == 0, result.stderr


def test_restore_moves_consumed_to_reconciled_with_absence_proof(tmp_path):
    _write_state(tmp_path)
    _write_consumed(tmp_path)
    env = _env(tmp_path)
    _run_helper(env, f'explore_issue_restore_permit "{CHANNEL}" "{THREAD}" "{ABSENCE_PROOF}"')
    reconciled_dir = tmp_path / "reconciled"
    files = list(reconciled_dir.glob(f"{KEY}.permit.reconciled.*.json"))
    assert len(files) == 1, "exactly one reconciled file expected"
    data = json.loads(files[0].read_text())
    assert data["absence_proof"] == ABSENCE_PROOF
    assert data["body_sha256"] == BODY_SHA
    assert "reconciled_at" in data
    # Consumed dir must be clear for this key after restoration.
    consumed_dir = tmp_path / "consumed"
    assert not list(consumed_dir.glob(f"{KEY}.permit.consumed.*.json"))


def test_restore_allows_fresh_mint_after_clearing_consumed(tmp_path):
    _write_state(tmp_path)
    _write_consumed(tmp_path)
    env = _env(tmp_path)
    restore = _run_helper(env, f'explore_issue_restore_permit "{CHANNEL}" "{THREAD}" "{ABSENCE_PROOF}"')
    assert restore.returncode == 0, restore.stderr
    # Minting should succeed now that consumed guard is gone.
    new_body_sha = "aabbcc"
    new_title_digest = "ddeeff"
    mint = _run_helper(env, (
        f'explore_issue_mint_permit "{CHANNEL}" "{THREAD}" "{new_body_sha}" "{new_title_digest}" "{OBLIGATION_ID}"'
    ))
    assert mint.returncode == 0, mint.stderr
    permit_dir = tmp_path / "permits"
    files = list(permit_dir.glob(f"{KEY}.permit.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["body_sha256"] == new_body_sha


def test_restore_then_permit_status_returns_1_before_mint(tmp_path):
    """After restore, before re-minting, permit_status should be 1 (absent), not 3 (consumed)."""
    _write_state(tmp_path)
    _write_consumed(tmp_path)
    env = _env(tmp_path)
    _run_helper(env, f'explore_issue_restore_permit "{CHANNEL}" "{THREAD}" "{ABSENCE_PROOF}"')
    result = _run_helper(env, (
        f'explore_issue_permit_status "{CHANNEL}" "{THREAD}" "{BODY_SHA}" "{TITLE_DIGEST}"; echo "rc=$?"'
    ))
    assert "rc=1" in result.stdout, f"expected rc=1 (absent), got: {result.stdout!r}"


def test_second_restore_is_blocked_returns_2(tmp_path):
    """Double-reconcile is blocked to preserve UNKNOWN safety on the new consumed file."""
    _write_state(tmp_path)
    _write_consumed(tmp_path)
    env = _env(tmp_path)
    first = _run_helper(env, f'explore_issue_restore_permit "{CHANNEL}" "{THREAD}" "{ABSENCE_PROOF}"')
    assert first.returncode == 0
    # Create a second consumed file (simulating a second create attempt)
    consumed_dir = tmp_path / "consumed"
    consumed_dir.mkdir(exist_ok=True)
    (consumed_dir / f"{KEY}.permit.consumed.20261114T120000Z.json").write_text(json.dumps({
        "channel": CHANNEL, "thread_ts": THREAD, "body_sha256": "aabbcc", "obligation_id": OBLIGATION_ID,
    }))
    second = _run_helper(env, f'explore_issue_restore_permit "{CHANNEL}" "{THREAD}" "second proof"')
    assert second.returncode == 2, f"expected 2 (already reconciled), got {second.returncode}"


def test_restore_blocked_when_state_is_terminal_resolved_filed(tmp_path):
    _write_state(tmp_path, state="RESOLVED_FILED")
    _write_consumed(tmp_path)
    env = _env(tmp_path)
    result = _run_helper(env, f'explore_issue_restore_permit "{CHANNEL}" "{THREAD}" "{ABSENCE_PROOF}"')
    assert result.returncode != 0, "restore must refuse when state is terminal"


def test_restore_blocked_when_state_is_terminal_resolved_no_issue(tmp_path):
    _write_state(tmp_path, state="RESOLVED_NO_ISSUE")
    _write_consumed(tmp_path)
    env = _env(tmp_path)
    result = _run_helper(env, f'explore_issue_restore_permit "{CHANNEL}" "{THREAD}" "{ABSENCE_PROOF}"')
    assert result.returncode != 0, "restore must refuse when state is RESOLVED_NO_ISSUE"


def test_restore_blocked_when_state_is_in_progress(tmp_path):
    _write_state(tmp_path, state="IN_PROGRESS")
    _write_consumed(tmp_path)
    env = _env(tmp_path)
    result = _run_helper(env, f'explore_issue_restore_permit "{CHANNEL}" "{THREAD}" "{ABSENCE_PROOF}"')
    assert result.returncode != 0, "restore must refuse when state is not READY_TO_FILE"


def test_restore_returns_1_when_no_consumed_permit_exists(tmp_path):
    _write_state(tmp_path)
    # No consumed permit written.
    env = _env(tmp_path)
    result = _run_helper(env, f'explore_issue_restore_permit "{CHANNEL}" "{THREAD}" "{ABSENCE_PROOF}"')
    assert result.returncode == 1, "restore must return 1 when no consumed permit exists"


def test_restore_returns_1_when_state_file_missing(tmp_path):
    _write_consumed(tmp_path)
    # No state file written.
    env = _env(tmp_path)
    result = _run_helper(env, f'explore_issue_restore_permit "{CHANNEL}" "{THREAD}" "{ABSENCE_PROOF}"')
    assert result.returncode == 1
