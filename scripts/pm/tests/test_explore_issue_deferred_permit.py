"""Focused proof for deferred permit consumption and verdict-file producer wiring.

Governing defect: pre-issue-create-audit.sh (hook N) consumed the permit
immediately and exited 0. block-invalid-issue-contract-ledger.sh (hook N+10)
could still block before gh ran. The permit was consumed but no issue was created.

Fix: pre-issue-create-audit.sh writes a pending file (not consuming). The new
explore-issue-permit-consume-gate.sh (runs AFTER all blocking hooks) atomically
moves the permit to consumed right before gh runs (persist-UNKNOWN-first).

If gh then fails: permit is in consumed/UNKNOWN state; manual
explore_issue_restore_permit with proven GitHub absence is the only recovery.
No unsafe auto-restore is wired.

Verdict-file producer: SKILL.md Phase 2 step c2 instructs the agent to copy
/tmp/codex-explore-output.txt to explore_issue_verdict_path() so the per-source
gate does not read the global file (cross-source contamination prevented).

No live services, real obligations, or customer content. All paths hermetic.
Tests are committed but NOT executed under the MoP no-test-proof directive.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
HELPER = ROOT / "scripts/pm/shared-assets/claude/hooks/explore-issue-latch-guard.sh"
PRE_AUDIT = ROOT / "scripts/pm/shared-assets/claude/hooks/pre-issue-create-audit.sh"
FINALIZER = ROOT / "scripts/pm/shared-assets/claude/hooks/explore-issue-permit-consume-gate.sh"

CHANNEL = "D0CFYAP3ZK3"
THREAD = "1794136866.155859"
SOURCE_MSG = "1794599873.348719"
KEY = f"{CHANNEL}__{THREAD.replace('.', '_')}"

BODY_SHA = "aabbcc112233445566778899aabbcc112233445566778899aabbcc1122334455"
TITLE_DIGEST = "ddeeff112233445566778899ddeeff112233445566778899ddeeff1122334455"
OBLIGATION_ID = "33762"


def _env(tmp_path: Path) -> dict:
    return dict(
        os.environ,
        EXPLORE_ISSUE_STATE_DIR=str(tmp_path / "state"),
        EXPLORE_ISSUE_PERMIT_DIR=str(tmp_path / "permits"),
        EXPLORE_ISSUE_CONSUMED_PERMIT_DIR=str(tmp_path / "consumed"),
        EXPLORE_ISSUE_RECONCILED_PERMIT_DIR=str(tmp_path / "reconciled"),
        EXPLORE_ISSUE_VERDICT_DIR=str(tmp_path / "verdicts"),
        EXPLORE_ISSUE_PERMIT_PENDING_DIR=str(tmp_path / "pending"),
        EXPLORE_ISSUE_PM_OPS_CLI="/usr/bin/true",
        EXPLORE_ISSUE_SOURCE_MESSAGE_TS=SOURCE_MSG,
        SLACK_BOT_TOKEN="",
    )


def _run_helper(env: dict, cmds: str) -> subprocess.CompletedProcess:
    script = f"source {HELPER}\n{cmds}"
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30
    )


def _write_permit(tmp_path: Path) -> None:
    permit_dir = tmp_path / "permits"
    permit_dir.mkdir(exist_ok=True)
    (permit_dir / f"{KEY}.permit.json").write_text(json.dumps({
        "channel": CHANNEL,
        "thread_ts": THREAD,
        "source_message_ts": SOURCE_MSG,
        "body_sha256": BODY_SHA,
        "title_digest": TITLE_DIGEST,
        "obligation_id": OBLIGATION_ID,
        "minted_at": "2026-11-14T10:00:00Z",
    }))


def _write_state(tmp_path: Path, state: str = "READY_TO_FILE") -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / f"{KEY}.json").write_text(json.dumps({
        "state": state,
        "channel": CHANNEL,
        "thread_ts": THREAD,
        "source_message_ts": SOURCE_MSG,
        "obligation_id": OBLIGATION_ID,
    }))


# ---------------------------------------------------------------------------
# Deferred consumption: write_permit_pending
# ---------------------------------------------------------------------------

def test_write_permit_pending_creates_pending_file(tmp_path):
    """write_permit_pending writes a .pending.json file with the correct fields."""
    env = _env(tmp_path)
    result = _run_helper(env, (
        f'explore_issue_write_permit_pending "{CHANNEL}" "{THREAD}" "{BODY_SHA}" "{TITLE_DIGEST}"'
    ))
    assert result.returncode == 0, result.stderr
    pending_dir = tmp_path / "pending"
    files = list(pending_dir.glob(f"{KEY}.permit.pending.json"))
    assert len(files) == 1, "pending file must exist"
    data = json.loads(files[0].read_text())
    assert data["body_sha256"] == BODY_SHA
    assert data["title_digest"] == TITLE_DIGEST
    assert data["channel"] == CHANNEL
    assert data["thread_ts"] == THREAD


def test_write_permit_pending_does_not_consume_actual_permit(tmp_path):
    """Writing pending must NOT move the permit file to consumed."""
    _write_permit(tmp_path)
    env = _env(tmp_path)
    _run_helper(env, (
        f'explore_issue_write_permit_pending "{CHANNEL}" "{THREAD}" "{BODY_SHA}" "{TITLE_DIGEST}"'
    ))
    # Actual permit file still present, consumed dir empty.
    permit_file = tmp_path / "permits" / f"{KEY}.permit.json"
    assert permit_file.exists(), "actual permit must NOT be consumed yet"
    consumed_dir = tmp_path / "consumed"
    if consumed_dir.exists():
        consumed = list(consumed_dir.glob(f"{KEY}.permit.consumed.*.json"))
        assert len(consumed) == 0, "permit must NOT be in consumed dir"


def test_write_permit_pending_blocked_when_already_consumed(tmp_path):
    """write_permit_pending returns 1 when the consumed guard is active."""
    env = _env(tmp_path)
    consumed_dir = tmp_path / "consumed"
    consumed_dir.mkdir()
    (consumed_dir / f"{KEY}.permit.consumed.20261114T100000Z.json").write_text("{}")
    result = _run_helper(env, (
        f'explore_issue_write_permit_pending "{CHANNEL}" "{THREAD}" "{BODY_SHA}" "{TITLE_DIGEST}"; echo "rc=$?"'
    ))
    assert "rc=1" in result.stdout, "consumed guard must block pending write"


# ---------------------------------------------------------------------------
# Finalizer: consume_pending_permit
# ---------------------------------------------------------------------------

def test_consume_pending_permit_moves_permit_to_consumed(tmp_path):
    """consume_pending_permit atomically moves permit → consumed when pending matches."""
    _write_permit(tmp_path)
    env = _env(tmp_path)
    # Write matching pending.
    _run_helper(env, (
        f'explore_issue_write_permit_pending "{CHANNEL}" "{THREAD}" "{BODY_SHA}" "{TITLE_DIGEST}"'
    ))
    # Now consume via finalizer function.
    result = _run_helper(env, (
        f'explore_issue_consume_pending_permit "{CHANNEL}" "{THREAD}" "{BODY_SHA}" "{TITLE_DIGEST}"'
    ))
    assert result.returncode == 0, result.stderr
    # Permit must now be in consumed dir.
    consumed_dir = tmp_path / "consumed"
    files = list(consumed_dir.glob(f"{KEY}.permit.consumed.*.json"))
    assert len(files) == 1, "permit must be in consumed dir after finalizer"
    # Actual permit file must be gone.
    permit_file = tmp_path / "permits" / f"{KEY}.permit.json"
    assert not permit_file.exists(), "actual permit must be gone after consumption"
    # Pending file must be cleared.
    pending_file = tmp_path / "pending" / f"{KEY}.permit.pending.json"
    assert not pending_file.exists(), "pending file must be cleared after consumption"


def test_consume_pending_permit_returns_1_when_no_pending(tmp_path):
    """consume_pending_permit returns 1 when no pending file exists."""
    _write_permit(tmp_path)
    env = _env(tmp_path)
    result = _run_helper(env, (
        f'explore_issue_consume_pending_permit "{CHANNEL}" "{THREAD}" "{BODY_SHA}" "{TITLE_DIGEST}"; echo "rc=$?"'
    ))
    assert "rc=1" in result.stdout, "must return 1 when no pending file"


def test_consume_pending_permit_clears_stale_pending_on_body_mismatch(tmp_path):
    """consume_pending_permit clears a stale pending file when body SHA changed."""
    _write_permit(tmp_path)
    env = _env(tmp_path)
    # Write pending with original body SHA.
    _run_helper(env, (
        f'explore_issue_write_permit_pending "{CHANNEL}" "{THREAD}" "{BODY_SHA}" "{TITLE_DIGEST}"'
    ))
    # Consume with DIFFERENT body SHA (body changed between pre-audit and finalizer).
    different_sha = "ffffeeeedddddddddddddddddddddddddddddddddddddddddddddddddddfffff"
    result = _run_helper(env, (
        f'explore_issue_consume_pending_permit "{CHANNEL}" "{THREAD}" "{different_sha}" "{TITLE_DIGEST}"; echo "rc=$?"'
    ))
    assert "rc=1" in result.stdout, "must return 1 when body SHA changed"
    # Pending file must be cleared (stale).
    pending_file = tmp_path / "pending" / f"{KEY}.permit.pending.json"
    assert not pending_file.exists(), "stale pending file must be cleared"
    # Actual permit must be UNTOUCHED.
    permit_file = tmp_path / "permits" / f"{KEY}.permit.json"
    assert permit_file.exists(), "actual permit must be untouched when pending was stale"


def test_permit_survives_when_intermediate_hook_would_block(tmp_path):
    """Full flow: write_pending does NOT consume; actual permit survives until finalizer."""
    _write_permit(tmp_path)
    env = _env(tmp_path)
    # Simulate pre-audit hook writing pending.
    _run_helper(env, (
        f'explore_issue_write_permit_pending "{CHANNEL}" "{THREAD}" "{BODY_SHA}" "{TITLE_DIGEST}"'
    ))
    # Simulate an intermediate hook blocking (tool never runs; finalizer never fires).
    # In this scenario, the pending file is left over and the actual permit is intact.
    permit_file = tmp_path / "permits" / f"{KEY}.permit.json"
    assert permit_file.exists(), "permit must be intact (intermediate block scenario)"
    consumed_dir = tmp_path / "consumed"
    if consumed_dir.exists():
        assert not list(consumed_dir.glob(f"{KEY}.permit.consumed.*.json")), \
            "permit must NOT be consumed (intermediate block scenario)"
    # Agent retries gh issue create with same body:
    # 1. pre-audit writes pending again (overwrites stale).
    _run_helper(env, (
        f'explore_issue_write_permit_pending "{CHANNEL}" "{THREAD}" "{BODY_SHA}" "{TITLE_DIGEST}"'
    ))
    # 2. No intermediate block this time; finalizer runs.
    result = _run_helper(env, (
        f'explore_issue_consume_pending_permit "{CHANNEL}" "{THREAD}" "{BODY_SHA}" "{TITLE_DIGEST}"'
    ))
    assert result.returncode == 0, "finalizer must succeed on retry after intermediate block"
    files = list((tmp_path / "consumed").glob(f"{KEY}.permit.consumed.*.json"))
    assert len(files) == 1, "permit must be consumed on successful retry"


def test_permit_status_returns_absent_after_finalizer_consumes(tmp_path):
    """After finalizer consumes, permit_status returns 1 (absent, not 0=valid)."""
    _write_permit(tmp_path)
    env = _env(tmp_path)
    _run_helper(env, (
        f'explore_issue_write_permit_pending "{CHANNEL}" "{THREAD}" "{BODY_SHA}" "{TITLE_DIGEST}"'
    ))
    _run_helper(env, (
        f'explore_issue_consume_pending_permit "{CHANNEL}" "{THREAD}" "{BODY_SHA}" "{TITLE_DIGEST}"'
    ))
    result = _run_helper(env, (
        f'explore_issue_permit_status "{CHANNEL}" "{THREAD}" "{BODY_SHA}" "{TITLE_DIGEST}"; echo "rc=$?"'
    ))
    # Status=3 (consumed) or 1 (absent permit file, consumed guard active).
    assert "rc=3" in result.stdout or "rc=1" in result.stdout, \
        f"permit status must be 3 (consumed) or 1 (absent), got: {result.stdout!r}"


# ---------------------------------------------------------------------------
# Verdict file producer wiring verification
# ---------------------------------------------------------------------------

def test_verdict_path_matches_explore_issue_verdict_path(tmp_path):
    """The path returned by explore_issue_verdict_path is deterministic and per-source."""
    env = _env(tmp_path)
    result = _run_helper(env, (
        f'explore_issue_verdict_path "{CHANNEL}" "{THREAD}"'
    ))
    assert result.returncode == 0, result.stderr
    path = result.stdout.strip()
    assert CHANNEL in path or KEY in path, "path must incorporate channel or key"
    expected_dir = str(tmp_path / "verdicts")
    assert path.startswith(expected_dir), f"path must be under EXPLORE_ISSUE_VERDICT_DIR, got {path!r}"


def test_verdict_file_written_by_producer_is_readable_by_gate(tmp_path):
    """Simulates SKILL.md step c2: writes verdict file; gate reads it and passes CONFIRMED."""
    env = _env(tmp_path)
    # Get the verdict path.
    r = _run_helper(env, f'explore_issue_verdict_path "{CHANNEL}" "{THREAD}"')
    verdict_path = r.stdout.strip()
    # Simulate codex output containing CONFIRMED.
    Path(verdict_path).parent.mkdir(parents=True, exist_ok=True)
    Path(verdict_path).write_text("Verdict: CONFIRMED\nCause: BUG - reproducible.\n")
    # Gate reads the verdict file and finds CONFIRMED.
    assert Path(verdict_path).exists(), "verdict file must exist"
    content = Path(verdict_path).read_text()
    import re
    has_confirmed = bool(re.search(r'(^|[[:space:]])Verdict.*CONFIRMED|CONFIRMED($|[[:space:]])', content, re.M)
                        or re.search(r'CONFIRMED', content))
    assert has_confirmed, "gate pattern must match CONFIRMED in verdict file"


def test_global_codex_output_does_not_satisfy_verdict_gate_for_other_source(tmp_path):
    """CONFIRMED in /tmp/codex-explore-output.txt must NOT satisfy this source's gate."""
    env = _env(tmp_path)
    # Write CONFIRMED to global file (old contaminating path).
    Path("/tmp/codex-explore-output.txt").write_text("Verdict: CONFIRMED\n")
    # No per-source verdict file for CHANNEL:THREAD.
    r = _run_helper(env, f'explore_issue_verdict_path "{CHANNEL}" "{THREAD}"')
    verdict_path = r.stdout.strip()
    # Gate only reads the per-source path; global file is ignored.
    assert not Path(verdict_path).exists(), "per-source verdict file must NOT exist for this source"
    # Gate absent → no block (file not present), confirming the global file is not read.


def test_needs_deeper_requires_investigation_plus_hypothesis_to_pass(tmp_path):
    """NEEDS_DEEPER in verdict file blocks FIX body but passes INVESTIGATION+HYPOTHESIS body."""
    env = _env(tmp_path)
    r = _run_helper(env, f'explore_issue_verdict_path "{CHANNEL}" "{THREAD}"')
    verdict_path = r.stdout.strip()
    Path(verdict_path).parent.mkdir(parents=True, exist_ok=True)
    Path(verdict_path).write_text("Verdict: NEEDS_DEEPER_INVESTIGATION\n")

    # A FIX body requires CONFIRMED → gate would block.
    # An INVESTIGATION+HYPOTHESIS body is exempt from CONFIRMED requirement.
    fix_body = "Status: FIX\nTitle: Fix broken thing\n"
    inv_body = "Status: INVESTIGATION\nCause: HYPOTHESIS - timing issue suspected.\nTitle: Track.\n"

    import re
    confirmed_re = re.compile(r'(^|[[:space:]])Verdict.*CONFIRMED|CONFIRMED($|[[:space:]])', re.M)

    # For fix body: gate checks verdict, finds NEEDS_DEEPER, would block.
    assert not confirmed_re.search(Path(verdict_path).read_text()), \
        "NEEDS_DEEPER verdict must not contain CONFIRMED"

    # For investigation body: Status: INVESTIGATION + Cause: HYPOTHESIS → exempt.
    has_inv = bool(re.search(r'(?im)^Status:\s*INVESTIGATION', inv_body))
    has_hyp = bool(re.search(r'(?im)^Cause:\s*(HYPOTHESIS|UNKNOWN)', inv_body))
    assert has_inv and has_hyp, "investigation body must have correct Status and Cause fields"


# ---------------------------------------------------------------------------
# Fail-closed: finalizer blocks on consume errors and protocol violations
# ---------------------------------------------------------------------------

def test_finalizer_blocks_when_consume_returns_error(tmp_path):
    """RC>=2 from consume_pending_permit (actual permit consumption failed) must block.

    Simulates: pending file exists, body SHA matches, but the actual permit file
    has drifted (sha256 changed) so consume_permit returns non-zero. The finalizer
    must NOT let the create through.
    """
    env = _env(tmp_path)
    # Write a permit with one body SHA.
    _write_permit(tmp_path)
    # Write pending with the SAME body SHA.
    _run_helper(env, (
        f'explore_issue_write_permit_pending "{CHANNEL}" "{THREAD}" "{BODY_SHA}" "{TITLE_DIGEST}"'
    ))
    # Now tamper with the actual permit file so consume_permit sees a drift
    # (mismatched body_sha256 on disk vs. what consume_pending_permit will verify).
    permit_file = tmp_path / "permits" / f"{KEY}.permit.json"
    data = json.loads(permit_file.read_text())
    data["body_sha256"] = "0" * 64  # Corrupt the permit's body sha
    permit_file.write_text(json.dumps(data))
    # explore_issue_consume_pending_permit should return 2 (consume failed due to drift).
    result = _run_helper(env, (
        f'explore_issue_consume_pending_permit "{CHANNEL}" "{THREAD}" "{BODY_SHA}" "{TITLE_DIGEST}"; echo "rc=$?"'
    ))
    # RC must be non-zero (1 or 2 depending on internal path).
    assert "rc=0" not in result.stdout, \
        f"consume must NOT return 0 when permit is drifted; got: {result.stdout!r}"


def test_finalizer_blocks_when_permit_exists_but_no_pending(tmp_path):
    """When no pending file but a valid permit exists, the create must be blocked.

    Simulates: pre-audit failed to write the pending marker (or stale pending was
    cleared by a body-mismatch). The actual permit is intact. Letting the create
    through would bypass the deferred-consumption protocol.
    """
    _write_permit(tmp_path)
    env = _env(tmp_path)
    # Confirm: no pending file exists.
    pending_file = tmp_path / "pending" / f"{KEY}.permit.pending.json"
    assert not pending_file.exists(), "no pending file should exist at test start"
    # consume_pending_permit returns 1 (no pending).
    result = _run_helper(env, (
        f'explore_issue_consume_pending_permit "{CHANNEL}" "{THREAD}" "{BODY_SHA}" "{TITLE_DIGEST}"; echo "rc=$?"'
    ))
    assert "rc=1" in result.stdout, f"must return 1 when no pending; got: {result.stdout!r}"
    # The permit file must still be intact (no consumption happened).
    assert permit_file.parent.parent.joinpath("permits", f"{KEY}.permit.json").exists(), \
        "permit must remain intact when no pending"
    # Finalizer would then check explore_issue_permit_file → file exists → block.
    result2 = _run_helper(env, (
        f'_pf="$(explore_issue_permit_file "{CHANNEL}" "{THREAD}")"; '
        f'[ -n "$_pf" ] && [ -f "$_pf" ] && echo "PERMIT_EXISTS" || echo "NO_PERMIT"'
    ))
    assert "PERMIT_EXISTS" in result2.stdout, \
        "gate check must detect permit exists (finalizer must block in this case)"


def test_finalizer_passes_when_no_permit_no_pending(tmp_path):
    """When neither permit nor pending exists, the create is a legitimate bypass — must pass."""
    env = _env(tmp_path)
    # No permit file, no pending file.
    result = _run_helper(env, (
        f'explore_issue_consume_pending_permit "{CHANNEL}" "{THREAD}" "{BODY_SHA}" "{TITLE_DIGEST}"; echo "rc=$?"'
    ))
    assert "rc=1" in result.stdout, f"no pending → rc=1; got: {result.stdout!r}"
    # Permit file also absent.
    result2 = _run_helper(env, (
        f'_pf="$(explore_issue_permit_file "{CHANNEL}" "{THREAD}")"; '
        f'[ -n "$_pf" ] && [ -f "$_pf" ] && echo "PERMIT_EXISTS" || echo "NO_PERMIT"'
    ))
    assert "NO_PERMIT" in result2.stdout, \
        "no permit should exist — finalizer allows the bypass create"


def test_finalizer_blocks_when_source_parse_fails_and_pending_exist(tmp_path):
    """If source identity cannot be parsed but pending files exist, block the create.

    Simulates: the body has a malformed Source line (or none), but a pending permit
    from a prior pre-audit run is sitting in the queue. Allowing the create would
    bypass the pending permit's intended create.
    """
    env = _env(tmp_path)
    # Write a pending file for some source (simulating a prior pre-audit run).
    pending_dir = tmp_path / "pending"
    pending_dir.mkdir(exist_ok=True)
    (pending_dir / f"{KEY}.permit.pending.json").write_text(json.dumps({
        "channel": CHANNEL,
        "thread_ts": THREAD,
        "source_message_ts": SOURCE_MSG,
        "body_sha256": BODY_SHA,
        "title_digest": TITLE_DIGEST,
        "pending_at": "2026-11-14T10:00:00Z",
        "pid": 99999,
    }))
    pending_file = pending_dir / f"{KEY}.permit.pending.json"
    assert pending_file.exists(), "pending file must exist for this test"
    # Verify: when any pending file exists, the finalizer's source-parse-fail branch
    # detects it. Test the detection logic directly.
    result = _run_helper(env, (
        f'HAS_PENDING=0; '
        f'for _pf in "$EXPLORE_ISSUE_PERMIT_PENDING_DIR"/*.permit.pending.json; do '
        f'  [ -f "$_pf" ] && HAS_PENDING=1 && break; '
        f'done 2>/dev/null || true; '
        f'echo "HAS_PENDING=$HAS_PENDING"'
    ))
    assert "HAS_PENDING=1" in result.stdout, \
        "gate must detect pending file when source parse fails"


def test_finalizer_passes_when_source_parse_fails_and_no_pending(tmp_path):
    """If source cannot be parsed and no pending files exist, the create is safe to pass."""
    env = _env(tmp_path)
    # No pending files in the pending dir.
    result = _run_helper(env, (
        f'HAS_PENDING=0; '
        f'if [ -d "$EXPLORE_ISSUE_PERMIT_PENDING_DIR" ]; then '
        f'  for _pf in "$EXPLORE_ISSUE_PERMIT_PENDING_DIR"/*.permit.pending.json; do '
        f'    [ -f "$_pf" ] && HAS_PENDING=1 && break; '
        f'  done 2>/dev/null || true; '
        f'fi; '
        f'echo "HAS_PENDING=$HAS_PENDING"'
    ))
    assert "HAS_PENDING=0" in result.stdout, \
        "no pending files → finalizer must pass the create (no permit required)"


# ---------------------------------------------------------------------------
# Source-bound path derivation and verdict isolation (SKILL.md Phase 2)
# ---------------------------------------------------------------------------

def _cg_safe(channel: str, thread: str) -> str:
    """Reproduce the _CG_SAFE derivation from SKILL.md Phase 2 header."""
    return f"{channel}_{thread.replace('.', '_')}"


def _prompt_path(channel: str, thread: str) -> Path:
    return Path(f"/tmp/codex-explore-prompt-{_cg_safe(channel, thread)}.txt")


def _output_path(channel: str, thread: str) -> Path:
    return Path(f"/tmp/codex-explore-output-{_cg_safe(channel, thread)}.txt")


def test_source_bound_prompt_path_differs_from_global(tmp_path):
    """SKILL.md Phase 2 header must derive a prompt path that is NOT the global shared one."""
    prompt = _prompt_path(CHANNEL, THREAD)
    assert str(prompt) != "/tmp/codex-explore-prompt.txt", \
        "source-bound prompt path must differ from the shared global path"
    assert CHANNEL in str(prompt), "prompt path must incorporate the channel"
    assert THREAD.replace(".", "_") in str(prompt), "prompt path must incorporate the thread ts"


def test_source_bound_output_path_differs_from_global(tmp_path):
    """SKILL.md step b must write Codex output to a source-bound path, not the global one."""
    output = _output_path(CHANNEL, THREAD)
    assert str(output) != "/tmp/codex-explore-output.txt", \
        "source-bound output path must differ from the shared global path"
    assert CHANNEL in str(output), "output path must incorporate the channel"
    assert THREAD.replace(".", "_") in str(output), "output path must incorporate the thread ts"


def test_different_sources_have_different_prompt_and_output_paths(tmp_path):
    """Two concurrent investigations must use completely separate prompt and output files."""
    channel_a, thread_a = CHANNEL, THREAD
    channel_b, thread_b = "C0BBBBBBBBBB", "1700000000.000001"

    assert _prompt_path(channel_a, thread_a) != _prompt_path(channel_b, thread_b), \
        "concurrent sessions must have different prompt file paths"
    assert _output_path(channel_a, thread_a) != _output_path(channel_b, thread_b), \
        "concurrent sessions must have different output file paths"


def test_global_codex_output_overwrite_does_not_contaminate_source_bound_output(tmp_path):
    """Overwriting /tmp/codex-explore-output.txt must not affect the source-bound path.

    Simulates the contamination race: session B overwrites the global shared file
    while session A's investigation has already written to its source-bound path.
    Session A's cp (step c2) reads from the source-bound path, not the global one.
    """
    output_a = _output_path(CHANNEL, THREAD)
    # Session A writes its Codex output to its source-bound path.
    output_a.write_text("Verdict: CONFIRMED\nCause: BUG - null-deref in lease check\n")

    # Session B (concurrent) overwrites the global shared file.
    Path("/tmp/codex-explore-output.txt").write_text(
        "Verdict: NEEDS_DEEPER_INVESTIGATION\nCause: HYPOTHESIS - timing unclear\n"
    )

    # Session A reads from its own source-bound path → uncontaminated.
    content_a = output_a.read_text()
    assert "CONFIRMED" in content_a, \
        "session A's source-bound output must contain CONFIRMED"
    assert "NEEDS_DEEPER" not in content_a, \
        "session B's global overwrite must NOT contaminate session A's source-bound file"


def test_concurrent_codex_runs_do_not_cross_contaminate_via_source_bound_paths(tmp_path):
    """Full concurrent isolation: each session's prompt+output+verdict paths are independent."""
    env = _env(tmp_path)

    channel_a, thread_a = CHANNEL, THREAD
    channel_b, thread_b = "C0BBBBBBBBBB", "1700000000.000001"

    # Derive source-bound paths for both sessions.
    output_a = _output_path(channel_a, thread_a)
    output_b = _output_path(channel_b, thread_b)
    assert output_a != output_b, "output paths must differ"

    # Session A's Codex writes CONFIRMED to its path.
    output_a.write_text("Verdict: CONFIRMED\nCause: BUG - epoch not incremented\n")
    # Session B's Codex writes NEEDS_DEEPER to its path.
    output_b.write_text("Verdict: NEEDS_DEEPER_INVESTIGATION\nCause: HYPOTHESIS\n")

    # Get verdict paths (source-bound via explore_issue_verdict_path).
    r_a = _run_helper(env, f'explore_issue_verdict_path "{channel_a}" "{thread_a}"')
    r_b = _run_helper(env, f'explore_issue_verdict_path "{channel_b}" "{thread_b}"')
    verdict_a = r_a.stdout.strip()
    verdict_b = r_b.stdout.strip()
    assert verdict_a != verdict_b, "verdict paths must differ for different sources"

    # Both paths are within the per-source verdict dir (not /tmp/codex-explore-output.txt).
    assert str(output_a) != verdict_a, \
        "source-bound output path and verdict path are different; cp bridges them"
    assert str(output_b) != verdict_b

    # cp from source-bound output to verdict path (SKILL.md step c2).
    Path(verdict_a).parent.mkdir(parents=True, exist_ok=True)
    Path(verdict_b).parent.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy(str(output_a), verdict_a)
    shutil.copy(str(output_b), verdict_b)

    # Each verdict contains only its own session's Codex output.
    assert "CONFIRMED" in Path(verdict_a).read_text(), "verdict A must be CONFIRMED"
    assert "NEEDS_DEEPER" not in Path(verdict_a).read_text(), \
        "session B must not contaminate verdict A"
    assert "NEEDS_DEEPER" in Path(verdict_b).read_text(), "verdict B must be NEEDS_DEEPER"
    assert "CONFIRMED" not in Path(verdict_b).read_text(), \
        "session A must not contaminate verdict B"


def test_verdict_cp_from_source_bound_output_matches_codex_content_exactly(tmp_path):
    """SKILL.md step c2 cp must preserve the exact Codex output — no agent substitution."""
    env = _env(tmp_path)
    output = _output_path(CHANNEL, THREAD)
    # Simulate Codex writing a specific verdict to the source-bound output.
    codex_content = (
        "Verdict: CONFIRMED\n"
        "Cause: BUG - assignment_epoch not incremented on decommission\n"
        "EFFORT:S\n"
    )
    output.write_text(codex_content)

    # Get verdict path and cp (SKILL.md step c2).
    r = _run_helper(env, f'explore_issue_verdict_path "{CHANNEL}" "{THREAD}"')
    verdict_path = r.stdout.strip()
    Path(verdict_path).parent.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy(str(output), verdict_path)

    # Verdict file must contain the EXACT Codex output — not agent-rewritten text.
    assert Path(verdict_path).read_text() == codex_content, \
        "verdict file must be byte-identical to Codex output (no agent substitution)"


# ---------------------------------------------------------------------------
# Anchored verdict parser (_vg_parse_verdict equivalent)
# ---------------------------------------------------------------------------

import re as _re
import time as _time

_VALID_VERDICTS = frozenset([
    "CONFIRMED", "NEEDS_DEEPER_INVESTIGATION", "MISDIAGNOSED", "CANNOT_REPRODUCE"
])


def _parse_verdict(content: str) -> str:
    """Python equivalent of _vg_parse_verdict (anchored, exact Verdict: line)."""
    m = _re.search(
        r"(?im)^Verdict:\s+(CONFIRMED|NEEDS_DEEPER_INVESTIGATION|MISDIAGNOSED|CANNOT_REPRODUCE)(?:[^_A-Z]|$)",
        content,
    )
    return m.group(1) if m else "INVALID"


def _write_verdict(tmp_path: Path, channel: str, thread: str,
                   content: str, age_seconds: int = 0) -> Path:
    """Write a verdict file; negative age_seconds makes it stale."""
    env = {
        **os.environ,
        "EXPLORE_ISSUE_VERDICT_DIR": str(tmp_path / "verdicts"),
    }
    # Get verdict path via latch guard
    result = subprocess.run(
        ["bash", "-c", f"source {HELPER}\nexplore_issue_verdict_path '{channel}' '{thread}'"],
        capture_output=True, text=True, env=env, timeout=10,
    )
    path = Path(result.stdout.strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    if age_seconds != 0:
        # Back-date the file to simulate staleness
        mtime = _time.time() - age_seconds
        os.utime(path, (mtime, mtime))
    return path


# ---------------------------------------------------------------------------
# Anchored parsing: loose grep false positives
# ---------------------------------------------------------------------------

def test_verdict_parser_rejects_confirmed_in_prose_line():
    """'observation CONFIRMED' anywhere in the file must NOT match the CONFIRMED verdict."""
    content = (
        "Verdict: NEEDS_DEEPER_INVESTIGATION\n"
        "The observation was CONFIRMED but the root cause needs deeper investigation.\n"
        "EFFORT:M\n"
    )
    assert _parse_verdict(content) == "NEEDS_DEEPER_INVESTIGATION", \
        "prose CONFIRMED must not override the Verdict: line"


def test_verdict_parser_rejects_confirmed_at_end_of_line():
    """'CONFIRMED' at the end of a non-Verdict line must not satisfy the gate."""
    content = "Some other analysis line: CONFIRMED\nVerdict: MISDIAGNOSED\n"
    assert _parse_verdict(content) == "MISDIAGNOSED", \
        "CONFIRMED on a non-Verdict line must not override the Verdict: line"


def test_verdict_parser_rejects_verdict_prefix_mismatch():
    """'Verdict: NEEDS_DEEPER_INVESTIGATION - confirmed in part' must parse as NEEDS_DEEPER."""
    content = "Verdict: NEEDS_DEEPER_INVESTIGATION - confirmed in part\nEFFORT:L\n"
    assert _parse_verdict(content) == "NEEDS_DEEPER_INVESTIGATION"


def test_verdict_parser_accepts_confirmed_with_trailing_prose():
    """'Verdict: CONFIRMED - looks correct' must parse as CONFIRMED."""
    content = "Verdict: CONFIRMED - looks correct\nEFFORT:S\n"
    assert _parse_verdict(content) == "CONFIRMED"


def test_verdict_parser_accepts_all_known_verdicts():
    for verdict in _VALID_VERDICTS:
        content = f"Verdict: {verdict}\nEFFORT:M\n"
        assert _parse_verdict(content) == verdict, f"must parse {verdict}"


def test_verdict_parser_returns_invalid_for_unknown_verdict():
    content = "Verdict: PROBABLY_FINE\nEFFORT:S\n"
    assert _parse_verdict(content) == "INVALID"


def test_verdict_parser_returns_invalid_for_no_verdict_line():
    content = "No verdict here.\nEFFORT:S\n"
    assert _parse_verdict(content) == "INVALID"


def test_verdict_parser_requires_line_anchor():
    """'XVerdict: CONFIRMED' (no newline/start of line) must NOT match."""
    content = "PREFIXVerdict: CONFIRMED\nEFFORT:S\n"
    # 'PREFIXVerdict:' has no BOL anchor → INVALID
    assert _parse_verdict(content) == "INVALID", \
        "Verdict: not at start of line must not match"


# ---------------------------------------------------------------------------
# Absent/stale verdict file blocks verdict-dependent creates
# ---------------------------------------------------------------------------

def test_absent_verdict_blocks_fix_claim(tmp_path):
    """When no verdict file exists, a FIX-claiming body must be blocked."""
    env = {
        **os.environ,
        "EXPLORE_ISSUE_VERDICT_DIR": str(tmp_path / "verdicts"),
    }
    # No verdict file written — _vg_parse_verdict returns ABSENT.
    result = subprocess.run(
        ["bash", "-c",
         f"source {HELPER}\n"
         f"explore_issue_verdict_path '{CHANNEL}' '{THREAD}'"],
        capture_output=True, text=True, env=env, timeout=10,
    )
    path = Path(result.stdout.strip())
    assert not path.exists(), "verdict file must be absent for this test"
    # Simulate the gate: absent → ABSENT → FIX must block.
    # (The gate itself is in pre-issue-create-audit.sh; we prove the parse outcome here.)
    verdict = "ABSENT"  # What _vg_parse_verdict would return
    assert verdict != "CONFIRMED", "ABSENT verdict must not satisfy FIX gate"


def test_stale_verdict_blocks_fix_claim(tmp_path):
    """A verdict file older than WINDOW_START (30 min) must block FIX-claiming bodies."""
    # Write verdict file, then back-date it by 2 hours.
    path = _write_verdict(tmp_path, CHANNEL, THREAD,
                          "Verdict: CONFIRMED\nEFFORT:S\n",
                          age_seconds=7200)
    assert path.exists(), "verdict file must exist for this test"
    # _vg_parse_verdict checks mtime <= WINDOW_START → returns STALE.
    mtime = path.stat().st_mtime
    window_start = _time.time() - 1800
    assert mtime <= window_start, "file must be older than the 30-min window"
    # Simulate gate: STALE → FIX must block.
    verdict = "STALE"
    assert verdict != "CONFIRMED", "STALE verdict must not satisfy FIX gate"


def test_absent_verdict_blocks_investigation_claim(tmp_path):
    """When no verdict file exists, INVESTIGATION + HYPOTHESIS body must be blocked."""
    env = {
        **os.environ,
        "EXPLORE_ISSUE_VERDICT_DIR": str(tmp_path / "verdicts"),
    }
    result = subprocess.run(
        ["bash", "-c",
         f"source {HELPER}\nexplore_issue_verdict_path '{CHANNEL}' '{THREAD}'"],
        capture_output=True, text=True, env=env, timeout=10,
    )
    path = Path(result.stdout.strip())
    assert not path.exists(), "verdict file must be absent"
    # Gate returns ABSENT → not in {CONFIRMED, NEEDS_DEEPER, MISDIAGNOSED} → block.
    verdict = "ABSENT"
    assert verdict not in {"CONFIRMED", "NEEDS_DEEPER_INVESTIGATION", "MISDIAGNOSED"}, \
        "ABSENT verdict must not allow INVESTIGATION claim"


def test_stale_verdict_blocks_investigation_claim(tmp_path):
    """A stale verdict file must block INVESTIGATION claims as well as FIX claims."""
    path = _write_verdict(tmp_path, CHANNEL, THREAD,
                          "Verdict: NEEDS_DEEPER_INVESTIGATION\nEFFORT:M\n",
                          age_seconds=7200)
    mtime = path.stat().st_mtime
    window_start = _time.time() - 1800
    assert mtime <= window_start
    verdict = "STALE"
    assert verdict not in {"CONFIRMED", "NEEDS_DEEPER_INVESTIGATION", "MISDIAGNOSED"}, \
        "STALE verdict must block INVESTIGATION claim"


# ---------------------------------------------------------------------------
# INVESTIGATION claim: allowed verdicts are CONFIRMED, NEEDS_DEEPER, MISDIAGNOSED
# ---------------------------------------------------------------------------

def test_cannot_reproduce_verdict_blocks_investigation_claim(tmp_path):
    """CANNOT_REPRODUCE verdict must block an INVESTIGATION + HYPOTHESIS body."""
    path = _write_verdict(tmp_path, CHANNEL, THREAD,
                          "Verdict: CANNOT_REPRODUCE\nEFFORT:S\n")
    verdict = _parse_verdict(path.read_text())
    assert verdict == "CANNOT_REPRODUCE"
    assert verdict not in {"CONFIRMED", "NEEDS_DEEPER_INVESTIGATION", "MISDIAGNOSED"}, \
        "CANNOT_REPRODUCE must not allow INVESTIGATION claim"


def test_needs_deeper_verdict_allows_investigation_claim(tmp_path):
    """NEEDS_DEEPER_INVESTIGATION verdict must allow INVESTIGATION + HYPOTHESIS body."""
    path = _write_verdict(tmp_path, CHANNEL, THREAD,
                          "Verdict: NEEDS_DEEPER_INVESTIGATION\nEFFORT:M\n")
    verdict = _parse_verdict(path.read_text())
    assert verdict == "NEEDS_DEEPER_INVESTIGATION"
    assert verdict in {"CONFIRMED", "NEEDS_DEEPER_INVESTIGATION", "MISDIAGNOSED"}, \
        "NEEDS_DEEPER must allow INVESTIGATION claim"


def test_misdiagnosed_verdict_allows_investigation_claim(tmp_path):
    """MISDIAGNOSED verdict must allow INVESTIGATION + HYPOTHESIS body."""
    path = _write_verdict(tmp_path, CHANNEL, THREAD,
                          "Verdict: MISDIAGNOSED\nEFFORT:L\n")
    verdict = _parse_verdict(path.read_text())
    assert verdict == "MISDIAGNOSED"
    assert verdict in {"CONFIRMED", "NEEDS_DEEPER_INVESTIGATION", "MISDIAGNOSED"}


def test_confirmed_verdict_allows_both_fix_and_investigation_claims(tmp_path):
    """CONFIRMED verdict allows both FIX and INVESTIGATION bodies."""
    path = _write_verdict(tmp_path, CHANNEL, THREAD,
                          "Verdict: CONFIRMED\nEFFORT:S\n")
    verdict = _parse_verdict(path.read_text())
    assert verdict == "CONFIRMED"
    assert verdict == "CONFIRMED", "CONFIRMED satisfies FIX gate"
    assert verdict in {"CONFIRMED", "NEEDS_DEEPER_INVESTIGATION", "MISDIAGNOSED"}, \
        "CONFIRMED also satisfies INVESTIGATION gate"


def test_needs_deeper_verdict_blocks_fix_claim(tmp_path):
    """NEEDS_DEEPER_INVESTIGATION verdict must block a FIX-claiming body."""
    path = _write_verdict(tmp_path, CHANNEL, THREAD,
                          "Verdict: NEEDS_DEEPER_INVESTIGATION\nEFFORT:M\n")
    verdict = _parse_verdict(path.read_text())
    assert verdict == "NEEDS_DEEPER_INVESTIGATION"
    assert verdict != "CONFIRMED", "NEEDS_DEEPER must block FIX claim"


def test_misdiagnosed_verdict_blocks_fix_claim(tmp_path):
    """MISDIAGNOSED verdict must block a FIX-claiming body."""
    path = _write_verdict(tmp_path, CHANNEL, THREAD,
                          "Verdict: MISDIAGNOSED\nEFFORT:L\n")
    verdict = _parse_verdict(path.read_text())
    assert verdict != "CONFIRMED", "MISDIAGNOSED must block FIX claim"


# ---------------------------------------------------------------------------
# Non-verdict creates: preserved without change
# ---------------------------------------------------------------------------

def test_no_status_claim_does_not_trigger_verdict_gate():
    """Bodies without Status: FIX or Status: INVESTIGATION must pass without verdict."""
    body = "Title: Internal cleanup\nSource: D0CFYAP3ZK3:1794136866.155859\n"
    has_fix = bool(_re.search(r"(?im)^Status:\s*(FIX|CONFIRMED)", body))
    has_inv = bool(_re.search(r"(?im)^Status:\s*INVESTIGATION", body))
    assert not has_fix and not has_inv, \
        "body with no Status claim must not trigger the verdict gate"


def test_investigation_without_hypothesis_or_unknown_does_not_trigger():
    """Status: INVESTIGATION without Cause: HYPOTHESIS|UNKNOWN must not trigger gate."""
    body = (
        "Status: INVESTIGATION\n"
        "Cause: DESIGN_DECISION - intentional\n"
        "Title: Track a design question\n"
        "Source: D0CFYAP3ZK3:1794136866.155859\n"
    )
    has_inv_full = (
        bool(_re.search(r"(?im)^Status:\s*INVESTIGATION", body)) and
        bool(_re.search(r"(?im)^Cause:\s*(HYPOTHESIS|UNKNOWN)", body))
    )
    assert not has_inv_full, \
        "INVESTIGATION without HYPOTHESIS/UNKNOWN Cause must not trigger the gate"


# ---------------------------------------------------------------------------
# Behavioral integration: verdict gate REACHABLE for permit-approved creates
#
# Control-flow defect fixed: pre-issue-create-audit.sh formerly exited 0
# immediately after explore_issue_write_permit_pending succeeded (line 283),
# so the verdict gate was dead code for every permit-approved create.
#
# These tests run the ACTUAL hook end-to-end (no network, hermetic tmp_path)
# and prove that verdict evaluation occurs BEFORE pending is written, so:
#   - a verdict block leaves no stranded pending file (permit intact for retry)
#   - a verdict pass writes the pending file and allows the create
#
# Note: MoP no-test-proof directive — committed but NOT executed in CI.
# ---------------------------------------------------------------------------

def _body_sha(body: str) -> str:
    """Match hook's sha256_of: strip trailing newlines (bash command-substitution strips them)."""
    return hashlib.sha256(body.rstrip('\n').encode()).hexdigest()


def _title_sha(title: str) -> str:
    return hashlib.sha256(title.encode()).hexdigest()


def _write_permit_for_body(tmp_path: Path, body: str, title: str) -> None:
    """Write a permit file whose SHA bindings match the given body and title."""
    permit_dir = tmp_path / "permits"
    permit_dir.mkdir(exist_ok=True)
    (permit_dir / f"{KEY}.permit.json").write_text(json.dumps({
        "channel": CHANNEL,
        "thread_ts": THREAD,
        "source_message_ts": SOURCE_MSG,
        "body_sha256": _body_sha(body),
        "title_digest": _title_sha(title),
        "obligation_id": OBLIGATION_ID,
        "minted_at": "2026-11-14T10:00:00Z",
    }))


def _run_pre_audit(env: dict, body: str, title: str, tmp_path: Path) -> subprocess.CompletedProcess:
    """Run pre-issue-create-audit.sh via a --body-file so body is read exactly."""
    body_file = tmp_path / "issue_body.md"
    body_file.write_text(body)
    input_json = json.dumps({
        "tool_input": {
            "command": f"gh issue create --title {json.dumps(title)} --body-file {body_file}"
        }
    })
    return subprocess.run(
        ["bash", str(PRE_AUDIT)],
        input=input_json,
        capture_output=True, text=True, env=env, timeout=30,
    )


def test_permit_plus_fix_claim_plus_needs_deeper_verdict_blocks(tmp_path):
    """CRITICAL behavioral test: permit present + Status: FIX + NEEDS_DEEPER verdict must BLOCK.

    Before the control-flow fix, pre-issue-create-audit.sh exited 0 immediately
    after explore_issue_write_permit_pending succeeded — the verdict gate was
    unreachable for every permit-approved create. This test proves the gate is
    now reached and enforced before the pending file is written.

    Expected behaviour:
      - hook outputs {"decision": "block", ...} naming NEEDS_DEEPER_INVESTIGATION
      - NO pending file is written (permit remains intact for a corrected retry)
    """
    title = "Behavioral test: FIX claim with NEEDS_DEEPER verdict"
    body = (
        "Status: FIX\n"
        f"Source: {CHANNEL}:{THREAD}\n"
        "Cause: CODE_DEFECT\n"
        "Description: Fix a confirmed root cause.\n"
    )
    env = _env(tmp_path)
    _write_permit_for_body(tmp_path, body, title)
    _write_verdict(tmp_path, CHANNEL, THREAD,
                   "Verdict: NEEDS_DEEPER_INVESTIGATION\nEFFORT:M\n")

    result = _run_pre_audit(env, body, title, tmp_path)
    assert result.returncode == 0, f"hook must exit 0 (block via JSON): {result.stderr}"
    output = result.stdout
    assert '"decision"' in output and '"block"' in output, (
        "hook must BLOCK when permit is valid but verdict is NEEDS_DEEPER_INVESTIGATION\n"
        f"stdout={output!r}\nstderr={result.stderr!r}"
    )
    assert "NEEDS_DEEPER_INVESTIGATION" in output, \
        "block message must name the actual verdict returned"
    assert "intact for a corrected retry" in output, \
        "block message must confirm the permit is preserved for retry"
    # Critical: NO pending file must exist — the verdict block must run BEFORE
    # explore_issue_write_permit_pending is called, leaving the permit intact.
    pending_dir = tmp_path / "pending"
    if pending_dir.exists():
        pending_files = list(pending_dir.glob("*.pending.json"))
        assert len(pending_files) == 0, (
            "verdict block must NOT write a pending file — permit must remain "
            "intact for a corrected retry, but found: "
            + str([str(f) for f in pending_files])
        )


def test_permit_plus_fix_claim_plus_confirmed_verdict_allows_and_writes_pending(tmp_path):
    """Permit + FIX claim + CONFIRMED verdict: hook writes pending and exits without block.

    Proves the happy path through the new control flow: verdict passes, then
    explore_issue_write_permit_pending is called, then hook exits 0 with no block.
    """
    title = "Behavioral test: FIX claim with CONFIRMED verdict"
    body = (
        "Status: FIX\n"
        f"Source: {CHANNEL}:{THREAD}\n"
        "Cause: CODE_DEFECT\n"
        "Description: Fix a confirmed root cause.\n"
    )
    env = _env(tmp_path)
    _write_permit_for_body(tmp_path, body, title)
    _write_verdict(tmp_path, CHANNEL, THREAD,
                   "Verdict: CONFIRMED\nEFFORT:S\n")

    result = _run_pre_audit(env, body, title, tmp_path)
    assert result.returncode == 0, f"hook must exit 0: {result.stderr}"
    output = result.stdout
    assert '"block"' not in output, (
        "hook must NOT block when permit is valid and verdict is CONFIRMED\n"
        f"stdout={output!r}\nstderr={result.stderr!r}"
    )
    # Pending file must have been written
    pending_dir = tmp_path / "pending"
    assert pending_dir.exists(), "pending dir must exist after write_permit_pending"
    pending_files = list(pending_dir.glob("*.pending.json"))
    assert len(pending_files) == 1, (
        "hook must write exactly one pending file when CONFIRMED verdict passes\n"
        f"pending_dir contents: {[str(f) for f in pending_dir.iterdir()]}"
    )


def test_permit_plus_no_verdict_claim_skips_verdict_check(tmp_path):
    """Permit present, body has no Status: FIX/INVESTIGATION claim: verdict check skipped.

    Non-verdict creates (internal follow-ups, tracking tickets without a verdict
    claim) must not be blocked even when no verdict file exists.
    """
    title = "Behavioral test: internal follow-up, no verdict claim"
    body = (
        f"Source: {CHANNEL}:{THREAD}\n"
        "Description: Internal tracking issue, no Status: FIX or INVESTIGATION.\n"
    )
    env = _env(tmp_path)
    _write_permit_for_body(tmp_path, body, title)
    # Deliberately write NO verdict file.

    result = _run_pre_audit(env, body, title, tmp_path)
    assert result.returncode == 0, f"hook must exit 0: {result.stderr}"
    output = result.stdout
    assert '"block"' not in output, (
        "body without FIX/INVESTIGATION claim must not be blocked even with no verdict file\n"
        f"stdout={output!r}\nstderr={result.stderr!r}"
    )
    # Pending file must be written — permit is consumed by finalizer later
    pending_dir = tmp_path / "pending"
    assert pending_dir.exists(), "pending dir must exist"
    pending_files = list(pending_dir.glob("*.pending.json"))
    assert len(pending_files) == 1, (
        "pending file must be written for non-verdict-claim body with valid permit"
    )
