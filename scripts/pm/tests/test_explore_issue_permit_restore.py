"""Focused proof for explore_issue_restore_permit: a consumed permit is NEVER released.

Governing defect (CTO REVISE, review key review-permit-chain:0d2d8dc8...:1794624109.107149:v1):
explore_issue_consume_permit moves the permit to the consumed dir before the external
gh issue create (persist-UNKNOWN-first design). The earlier restore helper accepted
READY_TO_FILE plus a LEGACY consumed record (no issue_url, no consumed_by), archived it with
a caller-supplied absence_proof string and let the obligation re-mint. A legacy record has no
durable no-start proof, and a free-text absence string is an assertion, not proof, so that
released a possibly-created issue for a second create.

Contract now (each covered by a test below):
  - A consumed record is reconciliation-only. Restore is refuse-only and releases nothing.
  - A legacy consumed/UNKNOWN record (including source 33762) -> rc 6, record untouched.
  - A verified owner create (issue_url / consumed_by) -> rc 5, record untouched.
  - An in-flight create (gh may have started) -> rc 4, record untouched.
  - An unreadable consumed record is UNKNOWN, never absent -> rc 6.
  - No reconciled/ record is ever written, so no caller can unlock a re-mint with a string.
  - Mint stays refused and permit_status stays 3 (reconciliation-only) after any restore call.
  - Repeating restore, with any proof, is still a refusal.
  - Non READY_TO_FILE states, a missing consumed record and a missing state file keep their
    non-release results (rc != 0 / rc 1).

No live services, real obligations, or customer content. All paths are hermetic.
Tests are committed but NOT executed under the MoP no-test-proof directive.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

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


def _restore(env: dict, proof: str = ABSENCE_PROOF) -> subprocess.CompletedProcess:
    return _run_helper(env, f'explore_issue_restore_permit "{CHANNEL}" "{THREAD}" "{proof}"; echo "rc=$?"')


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


def _write_consumed(tmp_path: Path, **extra) -> Path:
    """Default is the LEGACY shape (no issue_url, no consumed_by), as source 33762 was left."""
    consumed_dir = tmp_path / "consumed"
    consumed_dir.mkdir(exist_ok=True)
    f = consumed_dir / f"{KEY}.permit.consumed.20261113T210948Z.json"
    record = {
        "channel": CHANNEL,
        "thread_ts": THREAD,
        "source_message_ts": SOURCE_MSG,
        "body_sha256": BODY_SHA,
        "title_digest": TITLE_DIGEST,
        "obligation_id": OBLIGATION_ID,
        "minted_at": "2026-11-13T21:09:36Z",
    }
    record.update(extra)
    f.write_text(json.dumps(record))
    return f


def _no_reconciled_record(tmp_path: Path) -> bool:
    reconciled_dir = tmp_path / "reconciled"
    return not reconciled_dir.exists() or not list(reconciled_dir.glob("*.json"))


def test_restore_refuses_a_legacy_unknown_consumed_record_for_source_33762(tmp_path):
    _write_state(tmp_path)
    consumed = _write_consumed(tmp_path)
    before = consumed.read_bytes()
    result = _restore(_env(tmp_path))
    assert "rc=6" in result.stdout, result.stdout + result.stderr
    assert consumed.exists() and consumed.read_bytes() == before
    assert _no_reconciled_record(tmp_path)


def test_restore_never_writes_a_reconciled_record_whatever_the_absence_proof(tmp_path):
    _write_state(tmp_path)
    consumed = _write_consumed(tmp_path)
    before = consumed.read_bytes()
    env = _env(tmp_path)
    for proof in (ABSENCE_PROOF, "", "no issue found", "github_search_confirmed_absent"):
        result = _restore(env, proof)
        assert "rc=6" in result.stdout, result.stdout + result.stderr
    assert consumed.read_bytes() == before
    assert _no_reconciled_record(tmp_path)


def test_restore_refuses_a_verified_owner_create_record(tmp_path):
    _write_state(tmp_path)
    consumed = _write_consumed(tmp_path, issue_url="https://github.com/o/r/issues/1", consumed_by="issue-create-owner")
    before = consumed.read_bytes()
    result = _restore(_env(tmp_path))
    assert "rc=5" in result.stdout, result.stdout + result.stderr
    assert consumed.read_bytes() == before
    assert _no_reconciled_record(tmp_path)


def test_restore_refuses_an_unreadable_consumed_record(tmp_path):
    _write_state(tmp_path)
    consumed_dir = tmp_path / "consumed"
    consumed_dir.mkdir()
    consumed = consumed_dir / f"{KEY}.permit.consumed.20261113T210948Z.json"
    consumed.write_bytes(b"{not json")
    result = _restore(_env(tmp_path))
    assert "rc=6" in result.stdout, result.stdout + result.stderr
    assert consumed.read_bytes() == b"{not json"
    assert _no_reconciled_record(tmp_path)


def test_restore_refuses_while_a_create_is_in_flight(tmp_path):
    _write_state(tmp_path)
    consumed = _write_consumed(tmp_path)
    inflight_dir = tmp_path / "inflight"
    inflight_dir.mkdir()
    (inflight_dir / f"{KEY}.inflight.json").write_text(json.dumps({"obligation_id": OBLIGATION_ID}))
    before = consumed.read_bytes()
    result = _restore(_env(tmp_path))
    assert "rc=4" in result.stdout, result.stdout + result.stderr
    assert consumed.read_bytes() == before
    assert _no_reconciled_record(tmp_path)


def test_mint_stays_refused_after_a_restore_attempt(tmp_path):
    _write_state(tmp_path)
    _write_consumed(tmp_path)
    env = _env(tmp_path)
    _restore(env)
    mint = _run_helper(env, (
        f'explore_issue_mint_permit "{CHANNEL}" "{THREAD}" "aabbcc" "ddeeff" "{OBLIGATION_ID}"; echo "rc=$?"'
    ))
    assert "rc=0" not in mint.stdout, "a consumed/UNKNOWN create must never be re-minted"
    assert not (tmp_path / "permits" / f"{KEY}.permit.json").exists()


def test_permit_status_stays_reconciliation_only_after_a_restore_attempt(tmp_path):
    """Before the fix status flipped 3 -> 1 after restore, which is what unlocked the re-mint."""
    _write_state(tmp_path)
    _write_consumed(tmp_path)
    env = _env(tmp_path)
    _restore(env)
    result = _run_helper(env, (
        f'explore_issue_permit_status "{CHANNEL}" "{THREAD}" "{BODY_SHA}" "{TITLE_DIGEST}"; echo "rc=$?"'
    ))
    assert "rc=3" in result.stdout, f"expected rc=3 (reconciliation-only), got: {result.stdout!r}"


def test_a_second_restore_is_still_a_refusal(tmp_path):
    _write_state(tmp_path)
    consumed = _write_consumed(tmp_path)
    env = _env(tmp_path)
    first = _restore(env)
    assert "rc=6" in first.stdout, first.stdout + first.stderr
    second = _restore(env, "second proof")
    assert "rc=6" in second.stdout, second.stdout + second.stderr
    assert consumed.exists()
    assert _no_reconciled_record(tmp_path)


def test_restore_blocked_when_state_is_terminal_resolved_filed(tmp_path):
    _write_state(tmp_path, state="RESOLVED_FILED")
    consumed = _write_consumed(tmp_path)
    result = _run_helper(_env(tmp_path), f'explore_issue_restore_permit "{CHANNEL}" "{THREAD}" "{ABSENCE_PROOF}"')
    assert result.returncode != 0, "restore must refuse when state is terminal"
    assert consumed.exists()


def test_restore_blocked_when_state_is_terminal_resolved_no_issue(tmp_path):
    _write_state(tmp_path, state="RESOLVED_NO_ISSUE")
    consumed = _write_consumed(tmp_path)
    result = _run_helper(_env(tmp_path), f'explore_issue_restore_permit "{CHANNEL}" "{THREAD}" "{ABSENCE_PROOF}"')
    assert result.returncode != 0, "restore must refuse when state is RESOLVED_NO_ISSUE"
    assert consumed.exists()


def test_restore_blocked_when_state_is_in_progress(tmp_path):
    _write_state(tmp_path, state="IN_PROGRESS")
    consumed = _write_consumed(tmp_path)
    result = _run_helper(_env(tmp_path), f'explore_issue_restore_permit "{CHANNEL}" "{THREAD}" "{ABSENCE_PROOF}"')
    assert result.returncode != 0, "restore must refuse when state is not READY_TO_FILE"
    assert consumed.exists()


def test_restore_returns_1_when_no_consumed_permit_exists(tmp_path):
    _write_state(tmp_path)
    result = _run_helper(_env(tmp_path), f'explore_issue_restore_permit "{CHANNEL}" "{THREAD}" "{ABSENCE_PROOF}"')
    assert result.returncode == 1, "restore must return 1 when no consumed permit exists"


def test_restore_returns_1_when_state_file_missing(tmp_path):
    consumed = _write_consumed(tmp_path)
    result = _run_helper(_env(tmp_path), f'explore_issue_restore_permit "{CHANNEL}" "{THREAD}" "{ABSENCE_PROOF}"')
    assert result.returncode == 1
    assert consumed.exists()
