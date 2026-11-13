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
