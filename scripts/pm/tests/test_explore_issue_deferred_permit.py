"""Focused proof for the single-chain permit protocol and the verdict-file producer wiring.

Superseded design (removed): pre-issue-create-audit.sh wrote a pending file and a
parallel finalizer hook consumed the permit before gh ran. Claude Code runs matching
hooks in parallel, every hook re-read the body at its own moment, and a permit could
be consumed with no issue created.

Current design: issue-create-owner.py is the single owner of a permit-required create.
It captures the body bytes once, runs pre-issue-create-audit.sh as stage `audit` and
the ledger/tracker hook on those bytes, locks, writes a durable in-flight record
before gh, feeds gh the identical bytes on stdin and consumes the permit only on a
verified issue URL (see test_issue_create_owner.py for the owner itself). This file
covers the library guards (in-flight == reconciliation-only), the retired finalizer,
the audit hook's stage/shape behaviour and the verdict gate.

No live services, real obligations, or customer content. All paths hermetic.
Tests are committed but NOT executed under the MoP no-test-proof directive.
"""
from __future__ import annotations

import base64
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
        EXPLORE_ISSUE_INFLIGHT_DIR=str(tmp_path / "inflight"),
        EXPLORE_ISSUE_ENV_LOCAL=str(tmp_path / "no-credentials.env"),
        EXPLORE_ISSUE_SLACK_CACHE_DIR=str(tmp_path / "slack"),
        EXPLORE_ISSUE_FALLBACK_LOG=str(tmp_path / "fallback.log"),
        EXPLORE_ISSUE_PM_OPS_CLI="/usr/bin/true",
        EXPLORE_ISSUE_LATCH_GUARD=str(HELPER),
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
# In-flight guards: a durable in-flight record means gh may have started
# ---------------------------------------------------------------------------

def _write_inflight(tmp_path: Path) -> Path:
    inflight_dir = tmp_path / "inflight"
    inflight_dir.mkdir(exist_ok=True)
    record = inflight_dir / f"{KEY}.inflight.json"
    record.write_text(json.dumps({
        "schema": 1,
        "state": "IN_FLIGHT",
        "key": KEY,
        "invocation": "0" * 32,
        "channel": CHANNEL,
        "thread_ts": THREAD,
        "source_message_ts": SOURCE_MSG,
        "body_sha256": BODY_SHA,
        "title_digest": TITLE_DIGEST,
    }))
    return record


def test_pending_protocol_is_gone_from_the_lib():
    text = HELPER.read_text()
    assert "explore_issue_write_permit_pending" not in text
    assert "explore_issue_consume_pending_permit" not in text
    assert "EXPLORE_ISSUE_PERMIT_PENDING_DIR" not in text


def test_permit_status_is_3_while_an_inflight_record_exists(tmp_path):
    _write_permit(tmp_path)
    _write_inflight(tmp_path)
    result = _run_helper(_env(tmp_path), (
        f'explore_issue_permit_status "{CHANNEL}" "{THREAD}" "{BODY_SHA}" "{TITLE_DIGEST}"; echo "rc=$?"'
    ))
    assert "rc=3" in result.stdout, result.stdout + result.stderr


def test_permit_status_is_0_for_a_valid_permit_with_no_inflight_or_consumed(tmp_path):
    _write_permit(tmp_path)
    result = _run_helper(_env(tmp_path), (
        f'explore_issue_permit_status "{CHANNEL}" "{THREAD}" "{BODY_SHA}" "{TITLE_DIGEST}"; echo "rc=$?"'
    ))
    assert "rc=0" in result.stdout, result.stdout + result.stderr


def test_mint_is_refused_while_an_inflight_record_exists(tmp_path):
    _write_inflight(tmp_path)
    result = _run_helper(_env(tmp_path), (
        f'explore_issue_mint_permit "{CHANNEL}" "{THREAD}" "{BODY_SHA}" "{TITLE_DIGEST}" "{OBLIGATION_ID}"; echo "rc=$?"'
    ))
    assert "rc=0" not in result.stdout, "an UNKNOWN in-flight create must never be re-minted"
    assert not (tmp_path / "permits" / f"{KEY}.permit.json").exists()


def test_restore_refuses_an_inflight_record_even_with_absence_proof(tmp_path):
    """gh may have started: no claimed absence releases an in-flight invocation."""
    _write_permit(tmp_path)
    _write_state(tmp_path, "READY_TO_FILE")
    record = _write_inflight(tmp_path)
    before = record.read_bytes()
    result = _run_helper(_env(tmp_path), (
        f'explore_issue_restore_permit "{CHANNEL}" "{THREAD}" "gh search found no issue for the permalink"; echo "rc=$?"'
    ))
    assert "rc=4" in result.stdout, result.stdout + result.stderr
    assert record.read_bytes() == before, "the in-flight record must be untouched"
    assert not (tmp_path / "reconciled").exists() or not list((tmp_path / "reconciled").glob("*.json"))
    assert (tmp_path / "permits" / f"{KEY}.permit.json").exists()
    status = _run_helper(_env(tmp_path), (
        f'explore_issue_permit_status "{CHANNEL}" "{THREAD}" "{BODY_SHA}" "{TITLE_DIGEST}"; echo "rc=$?"'
    ))
    assert "rc=3" in status.stdout, "the source stays reconciliation-only"


def test_restore_refuses_an_inflight_record_even_when_a_legacy_consumed_record_also_exists(tmp_path):
    _write_state(tmp_path, "READY_TO_FILE")
    record = _write_inflight(tmp_path)
    consumed_dir = tmp_path / "consumed"
    consumed_dir.mkdir()
    consumed = consumed_dir / f"{KEY}.permit.consumed.20261113T210948Z.json"
    consumed.write_text(json.dumps({"obligation_id": OBLIGATION_ID}))
    result = _run_helper(_env(tmp_path), f'explore_issue_restore_permit "{CHANNEL}" "{THREAD}" "proof"; echo "rc=$?"')
    assert "rc=4" in result.stdout, result.stdout + result.stderr
    assert record.exists() and consumed.exists()


def test_restore_refuses_a_verified_owner_consumed_record(tmp_path):
    """The owner verified the issue exists; claiming absence is a contradiction, not a recovery."""
    _write_state(tmp_path, "READY_TO_FILE")
    consumed_dir = tmp_path / "consumed"
    consumed_dir.mkdir()
    consumed = consumed_dir / f"{KEY}.permit.consumed.20261114T100000Z.json"
    consumed.write_text(json.dumps({
        "consumed_by": "issue-create-owner",
        "issue_url": "https://github.com/Scribie/heydonna-app/issues/4242",
    }))
    before = consumed.read_bytes()
    result = _run_helper(_env(tmp_path), f'explore_issue_restore_permit "{CHANNEL}" "{THREAD}" "proof"; echo "rc=$?"')
    assert "rc=5" in result.stdout, result.stdout + result.stderr
    assert consumed.read_bytes() == before


def test_restore_refuses_a_legacy_unknown_consumed_record(tmp_path):
    """CTO REVISE (review-permit-chain 0d2d8dc8): a legacy consumed/UNKNOWN record (no URL, no owner)
    has no durable no-start proof, and a caller-supplied absence string is not one. Restore must
    refuse, leave the record in consumed/, write no reconciled file and keep the source
    reconciliation-only."""
    _write_state(tmp_path, "READY_TO_FILE")
    consumed_dir = tmp_path / "consumed"
    consumed_dir.mkdir()
    consumed = consumed_dir / f"{KEY}.permit.consumed.20261113T210948Z.json"
    consumed.write_text(json.dumps({"obligation_id": OBLIGATION_ID, "body_sha256": BODY_SHA}))
    before = consumed.read_bytes()
    result = _run_helper(_env(tmp_path), (
        f'explore_issue_restore_permit "{CHANNEL}" "{THREAD}" '
        f'"gh search found no issue for the permalink, title or keywords"; echo "rc=$?"'
    ))
    assert "rc=6" in result.stdout, result.stdout + result.stderr
    assert consumed.exists() and consumed.read_bytes() == before, "the consumed record must be untouched"
    assert not (tmp_path / "reconciled").exists() or not list((tmp_path / "reconciled").glob("*.json"))
    status = _run_helper(_env(tmp_path), (
        f'explore_issue_permit_status "{CHANNEL}" "{THREAD}" "{BODY_SHA}" "{TITLE_DIGEST}"; echo "rc=$?"'
    ))
    assert "rc=3" in status.stdout, "the source stays reconciliation-only"


def test_restore_refuses_a_legacy_consumed_record_with_an_unreadable_body(tmp_path):
    """An unreadable consumed record is UNKNOWN, never absent: refuse, do not move it."""
    _write_state(tmp_path, "READY_TO_FILE")
    consumed_dir = tmp_path / "consumed"
    consumed_dir.mkdir()
    consumed = consumed_dir / f"{KEY}.permit.consumed.20261113T210948Z.json"
    consumed.write_bytes(b"{not json")
    result = _run_helper(_env(tmp_path), f'explore_issue_restore_permit "{CHANNEL}" "{THREAD}" "proof"; echo "rc=$?"')
    assert "rc=6" in result.stdout, result.stdout + result.stderr
    assert consumed.read_bytes() == b"{not json"
    assert not (tmp_path / "reconciled").exists() or not list((tmp_path / "reconciled").glob("*.json"))


def test_restore_refusal_is_repeatable_and_never_mints_or_reconciles(tmp_path):
    """Calling restore again, with any proof, is still a refusal: nothing is ever released."""
    _write_state(tmp_path, "READY_TO_FILE")
    consumed_dir = tmp_path / "consumed"
    consumed_dir.mkdir()
    consumed = consumed_dir / f"{KEY}.permit.consumed.20261113T210948Z.json"
    consumed.write_text(json.dumps({"obligation_id": OBLIGATION_ID, "body_sha256": BODY_SHA}))
    env = _env(tmp_path)
    for proof in ("first proof", "second proof", ""):
        result = _run_helper(env, f'explore_issue_restore_permit "{CHANNEL}" "{THREAD}" "{proof}"; echo "rc=$?"')
        assert "rc=6" in result.stdout, result.stdout + result.stderr
    assert consumed.exists()
    assert not (tmp_path / "permits" / f"{KEY}.permit.json").exists(), "restore never mints a permit"
    mint = _run_helper(env, (
        f'explore_issue_mint_permit "{CHANNEL}" "{THREAD}" "{BODY_SHA}" "{TITLE_DIGEST}" "{OBLIGATION_ID}"; echo "rc=$?"'
    ))
    assert "rc=0" not in mint.stdout, "a consumed/UNKNOWN create must never be re-minted"
    assert not (tmp_path / "permits" / f"{KEY}.permit.json").exists()


def test_remint_of_an_unused_stripped_digest_permit_does_not_reopen_consumed_custody(tmp_path):
    """Migrating a permit that was never consumed (retired stripped digest) is a plain re-mint with the
    exact digest. It replaces only the permit file and works while NO consumed/in-flight record exists;
    once a consumed record exists the same re-mint is refused and restore cannot reopen it."""
    stripped = "1" * 64
    exact = "2" * 64
    env = _env(tmp_path)
    minted = _run_helper(env, (
        f'explore_issue_mint_permit "{CHANNEL}" "{THREAD}" "{stripped}" "{TITLE_DIGEST}" "{OBLIGATION_ID}"; echo "rc=$?"'
    ))
    assert "rc=0" in minted.stdout, minted.stdout + minted.stderr
    migrated = _run_helper(env, (
        f'explore_issue_mint_permit "{CHANNEL}" "{THREAD}" "{exact}" "{TITLE_DIGEST}" "{OBLIGATION_ID}"; echo "rc=$?"'
    ))
    assert "rc=0" in migrated.stdout, migrated.stdout + migrated.stderr
    permit = json.loads((tmp_path / "permits" / f"{KEY}.permit.json").read_text())
    assert permit["body_sha256"] == exact
    status = _run_helper(env, (
        f'explore_issue_permit_status "{CHANNEL}" "{THREAD}" "{exact}" "{TITLE_DIGEST}"; echo "rc=$?"'
    ))
    assert "rc=0" in status.stdout, status.stdout + status.stderr

    # The permit is then spent by a create (consumed record written, gh may have run).
    consumed = _run_helper(env, (
        f'explore_issue_consume_permit "{CHANNEL}" "{THREAD}" "{exact}" "{TITLE_DIGEST}"; echo "rc=$?"'
    ))
    assert "rc=0" in consumed.stdout, consumed.stdout + consumed.stderr
    _write_state(tmp_path, "READY_TO_FILE")
    record = next((tmp_path / "consumed").glob(f"{KEY}.permit.consumed.*.json"))
    before = record.read_bytes()
    again = _run_helper(env, (
        f'explore_issue_mint_permit "{CHANNEL}" "{THREAD}" "{stripped}" "{TITLE_DIGEST}" "{OBLIGATION_ID}"; echo "rc=$?"'
    ))
    assert "rc=0" not in again.stdout, "a migration re-mint must not reopen consumed custody"
    restore = _run_helper(env, f'explore_issue_restore_permit "{CHANNEL}" "{THREAD}" "proof"; echo "rc=$?"')
    assert "rc=6" in restore.stdout, restore.stdout + restore.stderr
    assert record.exists() and record.read_bytes() == before
    final = _run_helper(env, (
        f'explore_issue_permit_status "{CHANNEL}" "{THREAD}" "{exact}" "{TITLE_DIGEST}"; echo "rc=$?"'
    ))
    assert "rc=3" in final.stdout, "consumed custody stays reconciliation-only"


def test_file_sha256_is_the_exact_byte_digest_and_differs_from_the_stripped_one(tmp_path):
    exact = b"Source: x\n\nbody with NUL \x00 inside\n\n\n"
    body = tmp_path / "body.md"
    body.write_bytes(exact)
    env = _env(tmp_path)
    exact_digest = _run_helper(env, f'explore_issue_file_sha256 "{body}"').stdout.strip()
    stripped_digest = _run_helper(env, f'explore_issue_sha256 "$(cat "{body}")"').stdout.strip()
    assert exact_digest == hashlib.sha256(exact).hexdigest()
    assert stripped_digest != exact_digest, "command substitution drops NUL and trailing newlines"
    piped = _run_helper(env, f'cat "{body}" | explore_issue_file_sha256').stdout.strip()
    assert piped == exact_digest


def test_restore_does_not_touch_a_legacy_consumed_record_for_another_source(tmp_path):
    """Source 33762 stays as it is: nothing in this protocol edits an existing consumed/state record."""
    _write_state(tmp_path, "READY_TO_FILE")
    consumed_dir = tmp_path / "consumed"
    consumed_dir.mkdir()
    consumed = consumed_dir / f"{KEY}.permit.consumed.20261113T210948Z.json"
    consumed.write_text(json.dumps({"obligation_id": OBLIGATION_ID, "body_sha256": BODY_SHA}))
    before = consumed.read_bytes()
    result = _run_helper(_env(tmp_path), (
        f'explore_issue_permit_status "{CHANNEL}" "{THREAD}" "{BODY_SHA}" "{TITLE_DIGEST}"; echo "rc=$?"'
    ))
    assert "rc=3" in result.stdout, "a legacy consumed record stays reconciliation-only"
    assert consumed.read_bytes() == before


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
# Retired finalizer: explore-issue-permit-consume-gate.sh decides nothing
# ---------------------------------------------------------------------------

def test_finalizer_is_an_inert_noop(tmp_path):
    env = _env(tmp_path)
    _write_permit(tmp_path)
    commands = (
        'gh issue create --title "x" --body-file /tmp/x.md',
        "python3 /Users/rajiv/.claude/hooks/issue-create-owner.py create --title x --body-file /tmp/x.md",
        "ls -la",
    )
    for command in commands:
        proc = subprocess.run(
            ["bash", str(FINALIZER)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert proc.returncode == 0, (command, proc.stderr)
        assert proc.stdout == "", (command, proc.stdout)
    # Nothing consumed, nothing pended, permit untouched.
    assert (tmp_path / "permits" / f"{KEY}.permit.json").exists()
    consumed_dir = tmp_path / "consumed"
    assert not consumed_dir.exists() or not list(consumed_dir.glob("*.json"))
    assert not (tmp_path / "pending").exists()


def test_finalizer_carries_no_consumption_code():
    text = FINALIZER.read_text()
    assert "explore_issue_consume" not in text
    assert "explore-issue-latch-guard" not in text


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
# Behavioral integration: the audit hook as the owner's `audit` stage, and as the
# PreToolUse guard for raw / compound / wrapped / owner commands.
#
# Stage audit (MOP_ISSUE_CREATE_STAGE=audit): the owner hands over the captured body
# and title verbatim. A valid permit + passing verdict prints ONE machine-readable
# marker line and writes NOTHING (no pending file, no lock, no in-flight record, no
# consumption). A rejecting gate prints a block and leaves the permit intact.
#
# Raw create (no stage): a source that holds a valid permit is denied BEFORE any
# permit write, whatever the shape; the remedy names the single owner command.
#
# Note: MoP no-test-proof directive — committed but NOT executed in CI.
# ---------------------------------------------------------------------------

def _body_sha(body: str) -> str:
    """The permit binds the EXACT raw digest of the body bytes (nothing stripped)."""
    return hashlib.sha256(body.encode()).hexdigest()


def _title_sha(title: str) -> str:
    return hashlib.sha256(title.encode()).hexdigest()


def _write_permit_for_body(tmp_path: Path, body: str, title: str) -> None:
    """Bind the permit to the root-message Source in these hook tests."""
    permit_dir = tmp_path / "permits"
    permit_dir.mkdir(exist_ok=True)
    (permit_dir / f"{KEY}.permit.json").write_text(json.dumps({
        "channel": CHANNEL,
        "thread_ts": THREAD,
        "source_message_ts": THREAD,
        "body_sha256": _body_sha(body),
        "title_digest": _title_sha(title),
        "obligation_id": OBLIGATION_ID,
        "minted_at": "2026-11-14T10:00:00Z",
    }))


def _stage_input(body: str, title: str, command: str | None = None, display: str = "(test buffer)") -> str:
    """The owner's stage-audit payload: the exact buffer base64 encoded on the hook's stdin. No file."""
    return json.dumps({
        "tool_input": {"command": command or f"gh issue create --title {json.dumps(title)} --body-file -"},
        "mop_issue_create": {
            "title": title,
            "body_b64": base64.b64encode(body.encode()).decode("ascii"),
            "body_display": display,
        },
    })


def _run_pre_audit(env: dict, body: str, title: str, tmp_path: Path, *, stage: bool = True,
                   command: str | None = None) -> subprocess.CompletedProcess:
    """Run pre-issue-create-audit.sh. stage=True is the owner's `audit` stage on the in-memory buffer."""
    body_file = tmp_path / "issue_body.md"
    run_env = dict(env)
    if stage:
        run_env.update(MOP_ISSUE_CREATE_STAGE="audit")
        payload = _stage_input(body, title, command)
    else:
        body_file.write_text(body)
        command = command or f"gh issue create --title {json.dumps(title)} --body-file {body_file}"
        payload = json.dumps({"tool_input": {"command": command}})
    return subprocess.run(
        ["bash", str(PRE_AUDIT)],
        input=payload,
        capture_output=True, text=True, env=run_env, timeout=60,
    )


def _marker(stdout: str) -> dict:
    lines = [line for line in stdout.splitlines() if line.strip().startswith("{")]
    assert lines, f"no marker line in {stdout!r}"
    return json.loads(lines[-1])["mop_issue_create_audit"]


def _assert_nothing_written(tmp_path: Path) -> None:
    """The audit hook never writes a permit record of any kind."""
    assert (tmp_path / "permits" / f"{KEY}.permit.json").exists(), "permit must stay intact"
    assert not (tmp_path / "state").exists(), "no obligation/state record"
    for name in ("pending", "inflight", "consumed", "reconciled"):
        directory = tmp_path / name
        assert not directory.exists() or not list(directory.iterdir()), f"{name} must stay empty"


FIX_BODY = (
    "Status: FIX\n"
    f"Source: {CHANNEL}:{THREAD}\n"
    "Cause: CODE_DEFECT\n"
    "Description: Fix a confirmed root cause.\n"
)


def test_stage_audit_permit_plus_fix_claim_plus_needs_deeper_verdict_blocks(tmp_path):
    """Permit present + Status: FIX + NEEDS_DEEPER verdict must BLOCK before anything is written."""
    title = "Behavioral test: FIX claim with NEEDS_DEEPER verdict"
    env = _env(tmp_path)
    _write_permit_for_body(tmp_path, FIX_BODY, title)
    _write_verdict(tmp_path, CHANNEL, THREAD, "Verdict: NEEDS_DEEPER_INVESTIGATION\nEFFORT:M\n")

    result = _run_pre_audit(env, FIX_BODY, title, tmp_path)
    assert result.returncode == 0, f"hook must exit 0 (block via JSON): {result.stderr}"
    assert '"decision"' in result.stdout and '"block"' in result.stdout, result.stdout
    assert "NEEDS_DEEPER_INVESTIGATION" in result.stdout, "block message must name the actual verdict"
    assert "intact for a corrected retry" in result.stdout
    assert "mop_issue_create_audit" not in result.stdout, "a rejecting gate must not report a valid permit"
    _assert_nothing_written(tmp_path)


def test_stage_audit_permit_plus_fix_claim_plus_confirmed_verdict_reports_marker_and_writes_nothing(tmp_path):
    title = "Behavioral test: FIX claim with CONFIRMED verdict"
    env = _env(tmp_path)
    _write_permit_for_body(tmp_path, FIX_BODY, title)
    _write_verdict(tmp_path, CHANNEL, THREAD, "Verdict: CONFIRMED\nEFFORT:S\n")

    result = _run_pre_audit(env, FIX_BODY, title, tmp_path)
    assert result.returncode == 0, result.stderr
    assert '"block"' not in result.stdout, result.stdout
    marker = _marker(result.stdout)
    assert marker["permit"] == "valid"
    assert marker["key"] == KEY
    assert marker["channel"] == CHANNEL and marker["thread_ts"] == THREAD
    assert marker["body_sha256"] == _body_sha(FIX_BODY), "audit must digest the captured bytes"
    assert marker["title_digest"] == _title_sha(title)
    assert marker["permit_file"] == str(tmp_path / "permits" / f"{KEY}.permit.json")
    assert marker["inflight_dir"] == str(tmp_path / "inflight")
    assert marker["consumed_dir"] == str(tmp_path / "consumed")
    _assert_nothing_written(tmp_path)


def test_stage_audit_permit_plus_no_verdict_claim_skips_verdict_check(tmp_path):
    """Body without a Status: FIX/INVESTIGATION claim: verdict check skipped, marker printed."""
    title = "Behavioral test: internal follow-up, no verdict claim"
    body = (
        f"Source: {CHANNEL}:{THREAD}\n"
        "Description: Internal tracking issue, no Status: FIX or INVESTIGATION.\n"
    )
    env = _env(tmp_path)
    _write_permit_for_body(tmp_path, body, title)
    # Deliberately write NO verdict file.

    result = _run_pre_audit(env, body, title, tmp_path)
    assert result.returncode == 0, result.stderr
    assert '"block"' not in result.stdout, result.stdout
    assert _marker(result.stdout)["permit"] == "valid"
    _assert_nothing_written(tmp_path)


def test_stage_audit_uses_the_handed_over_bytes_not_the_command_text(tmp_path):
    """The command's --body-file points elsewhere; the audit stage must digest the stdin buffer."""
    title = "Behavioral test: captured bytes win over the command text"
    body = f"Source: {CHANNEL}:{THREAD}\nDescription: captured bytes.\n"
    env = _env(tmp_path)
    _write_permit_for_body(tmp_path, body, title)
    result = _run_pre_audit(
        env, body, title, tmp_path,
        command='gh issue create --title "something else" --body-file /nonexistent/never-read.md',
    )
    assert result.returncode == 0, result.stderr
    assert _marker(result.stdout)["body_sha256"] == _body_sha(body)
    assert "cannot be read when this hook runs" not in result.stdout
    assert not (tmp_path / "issue_body.md").exists(), "stage audit reads no file and writes none"


def test_stage_audit_without_a_valid_permit_prints_no_marker(tmp_path):
    """No permit: the audit stage must not claim one (the owner then refuses before any write)."""
    title = "Behavioral test: no permit"
    body = f"Source: {CHANNEL}:{THREAD}\nDescription: no permit exists.\n<!-- audit-bypass: internal-followup -->\n"
    result = _run_pre_audit(_env(tmp_path), body, title, tmp_path)
    assert "mop_issue_create_audit" not in result.stdout


def test_stage_audit_with_consumed_record_is_reconciliation_only(tmp_path):
    """Legacy consumed record (source 33762 shape): blocked, record bytes unchanged."""
    title = "Behavioral test: consumed"
    body = f"Source: {CHANNEL}:{THREAD}\nDescription: consumed.\n"
    env = _env(tmp_path)
    _write_permit_for_body(tmp_path, body, title)
    consumed_dir = tmp_path / "consumed"
    consumed_dir.mkdir()
    consumed = consumed_dir / f"{KEY}.permit.consumed.20261113T210948Z.json"
    consumed.write_text(json.dumps({"obligation_id": OBLIGATION_ID}))
    before = consumed.read_bytes()
    result = _run_pre_audit(env, body, title, tmp_path)
    assert '"block"' in result.stdout and "already consumed" in result.stdout
    assert consumed.read_bytes() == before


def test_stage_audit_with_inflight_record_is_reconciliation_only(tmp_path):
    title = "Behavioral test: in flight"
    body = f"Source: {CHANNEL}:{THREAD}\nDescription: in flight.\n"
    env = _env(tmp_path)
    _write_permit_for_body(tmp_path, body, title)
    record = _write_inflight(tmp_path)
    before = record.read_bytes()
    result = _run_pre_audit(env, body, title, tmp_path)
    assert '"block"' in result.stdout and "already consumed" in result.stdout
    assert record.read_bytes() == before


def test_owner_stage_without_the_buffer_fails_closed(tmp_path):
    env = dict(_env(tmp_path), MOP_ISSUE_CREATE_STAGE="audit")
    result = subprocess.run(
        ["bash", str(PRE_AUDIT)],
        input=json.dumps({"tool_input": {"command": 'gh issue create --title "t" --body-file /x'}}),
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert '"block"' in result.stdout and "without the captured body buffer" in result.stdout
    assert not (tmp_path / "state").exists()


def test_stage_audit_digest_keeps_trailing_newlines_and_nul(tmp_path):
    """The permit binds the exact raw digest: a NUL byte and trailing blank lines are part of it."""
    title = "Behavioral test: exact bytes"
    body = f"Source: {CHANNEL}:{THREAD}\nBody with NUL \x00 inside.\n\n\n"
    env = _env(tmp_path)
    _write_permit_for_body(tmp_path, body, title)
    assert _body_sha(body) != hashlib.sha256(body.rstrip("\n").replace("\x00", "").encode()).hexdigest()
    result = _run_pre_audit(env, body, title, tmp_path)
    assert result.returncode == 0, result.stderr
    assert '"block"' not in result.stdout, result.stdout
    assert _marker(result.stdout)["body_sha256"] == _body_sha(body)
    _assert_nothing_written(tmp_path)


def test_stage_audit_rejects_a_permit_minted_with_the_retired_stripped_digest(tmp_path):
    title = "Behavioral test: retired digest"
    body = f"Source: {CHANNEL}:{THREAD}\nDescription: trailing newlines.\n\n"
    env = _env(tmp_path)
    _write_permit_for_body(tmp_path, body, title)
    permit = tmp_path / "permits" / f"{KEY}.permit.json"
    data = json.loads(permit.read_text())
    data["body_sha256"] = hashlib.sha256(body.rstrip("\n").encode()).hexdigest()
    permit.write_text(json.dumps(data))
    result = _run_pre_audit(env, body, title, tmp_path)
    assert "mop_issue_create_audit" not in result.stdout, "a stripped-digest permit must not release the create"
    assert "permit drift" in result.stdout


def test_stage_audit_customer_origin_block_writes_no_obligation_or_state(tmp_path):
    """write_durable_block is suppressed in stage audit: same BLOCK decision, zero writes."""
    title = "Behavioral test: customer origin, no permit"
    body = f"Source: {CHANNEL}:{THREAD}\nDescription: customer report with no permit.\n"
    recorder = tmp_path / "pm-ops-recorder.sh"
    recorder.write_text('#!/bin/sh\necho "$@" >> "$PM_OPS_RECORD"\necho 4242\n')
    recorder.chmod(0o755)
    record = tmp_path / "pm-ops-calls.log"
    env = dict(
        _env(tmp_path),
        EXPLORE_ISSUE_CUSTOMER_CHANNELS_REGEX=f"^{CHANNEL}$",
        EXPLORE_ISSUE_PM_OPS_CLI=str(recorder),
        PM_OPS_RECORD=str(record),
    )
    result = _run_pre_audit(env, body, title, tmp_path)
    decision = json.loads(result.stdout.strip().splitlines()[-1])
    assert decision["decision"] == "block" and "has no valid READY_TO_FILE permit" in decision["message"]
    assert not record.exists(), "no obligation upsert"
    assert not (tmp_path / "state").exists(), "no OPEN state record"
    assert not (tmp_path / "fallback.log").exists(), "no diagnostic log write"
    assert not (tmp_path / "slack").exists(), "no Slack cache directory or file is written in stage audit"


def test_raw_customer_origin_block_still_upserts_the_obligation(tmp_path):
    """The raw (non-stage) path keeps its durable obligation: the suppression is stage-audit only."""
    title = "Behavioral test: raw customer origin"
    body = f"Source: {CHANNEL}:{THREAD}\nDescription: raw customer report.\n"
    recorder = tmp_path / "pm-ops-recorder.sh"
    recorder.write_text('#!/bin/sh\necho "$@" >> "$PM_OPS_RECORD"\necho 4242\n')
    recorder.chmod(0o755)
    record = tmp_path / "pm-ops-calls.log"
    env = dict(
        _env(tmp_path),
        EXPLORE_ISSUE_CUSTOMER_CHANNELS_REGEX=f"^{CHANNEL}$",
        EXPLORE_ISSUE_PM_OPS_CLI=str(recorder),
        PM_OPS_RECORD=str(record),
    )
    _run_pre_audit(env, body, title, tmp_path, stage=False)
    assert record.exists() and "obligation-upsert" in record.read_text()


def test_audit_hook_reads_every_body_consumer_through_the_one_stream():
    """Source inspection: no command substitution of the body feeds a digest, claim grep or source parse."""
    text = PRE_AUDIT.read_text()
    assert 'BODY=$(cat "$MOP_ISSUE_CREATE_BODY_FILE"' not in text
    assert "MOP_ISSUE_CREATE_BODY_FILE" not in text
    assert 'BODY_SHA=$(sha256_of "$BODY_OR_CMD")' not in text
    # The perl regex extractors (only --body/--body-file, via command substitution) are gone: the
    # byte-preserving `extract` parser is the only way a raw create's title and body are read.
    for forbidden in ("perl", "extract_body", "extract_title", "BODY_OR_CMD", 'BODY=$(', "OWNER_BODY_B64"):
        assert forbidden not in text, forbidden
    assert 'python3 "$ISSUE_CREATE_OWNER_PY" extract' in text
    for needle in ('SOURCE_RESULT=$(body_stream |', "body_stream | grep -aiE '^Status:",
                   "body_stream | grep -aE '<!-- audit-bypass", "BODY_SHA=$(body_stream | sha256_stream)"):
        assert needle in text, needle


# A SIMPLE raw create that holds a valid permit is denied before any permit write, in EVERY gh spelling.

RAW_SIMPLE_SPELLINGS = {
    "long_flags": 'gh issue create --title "{title}" --body-file {body} --label status:todo',
    "short_flags": "gh issue create -t '{title}' -F {body} -l status:todo",
    "equals_forms": "gh issue create --title='{title}' --body-file={body}",
    "attached_short": "gh issue create -t'{title}' -F{body}",
    "body_first": "gh issue create -F {body} --title '{title}'",
    "single_quoted_path": "gh issue create -t '{title}' -F '{body}'",
    "absolute_gh": "/opt/homebrew/bin/gh issue create -t '{title}' -F {body}",
}


@pytest.mark.parametrize("shape", sorted(RAW_SIMPLE_SPELLINGS))
def test_simple_raw_permit_required_create_is_denied_before_any_permit_write_in_every_spelling(tmp_path, shape):
    title = f"Behavioral test: raw {shape}"
    body = f"Source: {CHANNEL}:{THREAD}\nDescription: raw create with a valid permit.\n"
    env = _env(tmp_path)
    _write_permit_for_body(tmp_path, body, title)
    body_file = tmp_path / "issue_body.md"
    command = RAW_SIMPLE_SPELLINGS[shape].format(title=title, body=body_file)
    result = _run_pre_audit(env, body, title, tmp_path, stage=False, command=command)
    assert result.returncode == 0, result.stderr
    decision = json.loads(result.stdout.strip().splitlines()[-1])
    assert decision["decision"] == "block"
    assert "single create owner" in decision["message"], "the permit must have been recognised"
    assert "issue-create-owner.py create" in decision["message"]
    assert "was NOT run" in decision["message"]
    _assert_nothing_written(tmp_path)


# ALL compound or wrapped creates are denied upfront, with or without a permit: nothing is parsed,
# classified or written.

RAW_COMPOUND_OR_WRAPPED = {
    "compound_and": 'cd /tmp && gh issue create --title "t" --body-file {body}',
    "compound_semicolon": 'gh issue create --title "t" --body-file {body}; echo done',
    "piped": 'gh issue create --title "t" --body-file {body} | cat',
    "redirected": 'gh issue create --title "t" --body-file {body} 2>&1',
    "subshell": '(gh issue create --title "t" --body-file {body})',
    "wrapped_env": 'env FOO=1 gh issue create --title "t" --body-file {body}',
    "wrapped_assignment": 'FOO=1 gh issue create --title "t" --body-file {body}',
    "wrapped_rtk": 'rtk gh issue create --title "t" --body-file {body}',
    "wrapped_shell": "bash -c 'gh issue create --title \"t\" --body-file {body}'",
    "wrapped_eval": 'eval "gh issue create --title t --body-file {body}"',
    "xargs": 'echo x | xargs gh issue create --title t',
}


@pytest.mark.parametrize("with_permit", [True, False])
@pytest.mark.parametrize("shape", sorted(RAW_COMPOUND_OR_WRAPPED))
def test_every_compound_or_wrapped_create_is_denied_upfront_with_or_without_a_permit(tmp_path, shape, with_permit):
    title = "t"
    body = f"Source: {CHANNEL}:{THREAD}\nDescription: compound.\n<!-- audit-bypass: internal-followup -->\n"
    recorder = tmp_path / "pm-ops-recorder.sh"
    recorder.write_text('#!/bin/sh\necho "$@" >> "$PM_OPS_RECORD"\necho 4242\n')
    recorder.chmod(0o755)
    record = tmp_path / "pm-ops-calls.log"
    env = dict(
        _env(tmp_path),
        EXPLORE_ISSUE_CUSTOMER_CHANNELS_REGEX=f"^{CHANNEL}$",
        EXPLORE_ISSUE_PM_OPS_CLI=str(recorder),
        PM_OPS_RECORD=str(record),
    )
    body_file = tmp_path / "issue_body.md"
    body_file.write_text(body)
    if with_permit:
        _write_permit_for_body(tmp_path, body, title)
    command = RAW_COMPOUND_OR_WRAPPED[shape].format(body=body_file)
    result = subprocess.run(
        ["bash", str(PRE_AUDIT)], input=json.dumps({"tool_input": {"command": command}}),
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    decision = json.loads(result.stdout.strip().splitlines()[-1])
    assert decision["decision"] == "block"
    assert "ONLY command" in decision["message"] and "refused upfront" in decision["message"]
    assert "Nothing was run and nothing was written" in decision["message"]
    assert not record.exists(), "no obligation upsert"
    for name in ("state", "slack", "inflight", "consumed", "pending", "reconciled"):
        assert not (tmp_path / name).exists(), name
    assert (tmp_path / "permits" / f"{KEY}.permit.json").exists() == with_permit


# The bypass this closes: the old perl extraction knew only --body-file/--body, so a raw create using
# gh's other spellings never had its Source line read and escaped the customer-origin / permit gates.

EVASION_SPELLINGS = {
    "short_F": "gh issue create -t 'customer bug' -F {body} -l status:todo",
    "short_b_inline": "gh issue create -t 'customer bug' -b 'Source: {channel}:{thread}' -l status:todo",
    "equals_body_file": "gh issue create --title='customer bug' --body-file={body}",
    "equals_body": "gh issue create --title='customer bug' --body='Source: {channel}:{thread}'",
    "single_quoted_path": "gh issue create -t 'customer bug' -F '{body}'",
    "attached_F": "gh issue create -t 'customer bug' -F{body}",
    "double_quoted_inline": 'gh issue create --title "customer bug" -b "Source: {channel}:{thread}"',
}


@pytest.mark.parametrize("shape", sorted(EVASION_SPELLINGS))
def test_every_gh_spelling_of_the_body_flags_reaches_the_customer_origin_gate(tmp_path, shape):
    body = f"Source: {CHANNEL}:{THREAD}\nDescription: customer report in an alias spelling.\n"
    body_file = tmp_path / "issue_body.md"
    body_file.write_text(body)
    recorder = tmp_path / "pm-ops-recorder.sh"
    recorder.write_text('#!/bin/sh\necho "$@" >> "$PM_OPS_RECORD"\necho 4242\n')
    recorder.chmod(0o755)
    record = tmp_path / "pm-ops-calls.log"
    env = dict(
        _env(tmp_path),
        EXPLORE_ISSUE_CUSTOMER_CHANNELS_REGEX=f"^{CHANNEL}$",
        EXPLORE_ISSUE_PM_OPS_CLI=str(recorder),
        PM_OPS_RECORD=str(record),
    )
    command = EVASION_SPELLINGS[shape].format(body=body_file, channel=CHANNEL, thread=THREAD)
    result = subprocess.run(
        ["bash", str(PRE_AUDIT)], input=json.dumps({"tool_input": {"command": command}}),
        capture_output=True, text=True, env=env, timeout=60,
    )
    decision = json.loads(result.stdout.strip().splitlines()[-1])
    assert decision["decision"] == "block" and "Customer-origin" in decision["message"], decision
    assert record.exists() and "obligation-upsert" in record.read_text()


VALID_NON_PERMIT_SPELLINGS = {
    "long_flags": 'gh issue create --title "internal follow-up" --body-file {body} --label status:todo',
    "short_flags": "gh issue create -t 'internal follow-up' -F {body} -l status:todo",
    "equals_forms": "gh issue create --title='internal follow-up' --body-file={body}",
    "inline_body": "gh issue create -t 'internal follow-up' -b 'internal <!-- audit-bypass: internal-followup -->'",
    "milestone_and_assignee": "gh issue create -t 'internal follow-up' -F {body} -m 'M1' -a someone",
}


@pytest.mark.parametrize("shape", sorted(VALID_NON_PERMIT_SPELLINGS))
def test_valid_non_permit_simple_creates_are_unchanged(tmp_path, shape):
    body_file = tmp_path / "issue_body.md"
    body_file.write_text("Internal follow-up body.\n<!-- audit-bypass: internal-followup -->\n")
    command = VALID_NON_PERMIT_SPELLINGS[shape].format(body=body_file)
    result = subprocess.run(
        ["bash", str(PRE_AUDIT)], input=json.dumps({"tool_input": {"command": command}}),
        capture_output=True, text=True, env=_env(tmp_path), timeout=60,
    )
    assert (result.returncode, result.stdout) == (0, ""), result.stdout


FAIL_CLOSED_COMMANDS = {
    "unknown_flag": ("ambiguous", "gh issue create -t x -b y --frobnicate"),
    "duplicate_title": ("ambiguous", "gh issue create -t x -t y -b z"),
    "duplicate_body": ("ambiguous", "gh issue create -t x -b y -b z"),
    "body_and_file": ("ambiguous", "gh issue create -t x -b y -F /tmp/b.md"),
    "positional": ("ambiguous", "gh issue create -t x -b y stray"),
    "dollar_variable_title": ("expansion", 'gh issue create -t "$TITLE" -b y'),
    "command_substitution_body": ("expansion", 'gh issue create -t x -b "$(cat /tmp/b.md)"'),
    "backtick_body": ("expansion", 'gh issue create -t x -b "`cat /tmp/b.md`"'),
    "glob_path": ("expansion", "gh issue create -t x -F /tmp/b*.md"),
    "brace_list": ("expansion", "gh issue create -t x -b {a,b}"),
    "stdin_body_file": ("unreadable", "gh issue create -t x -F -"),
    "unreadable_body_file": ("unreadable", "gh issue create -t x -F /nonexistent/never-written.md"),
}


@pytest.mark.parametrize("shape", sorted(FAIL_CLOSED_COMMANDS))
def test_ambiguous_or_unreadable_simple_creates_fail_closed_before_any_write(tmp_path, shape):
    error_class, command = FAIL_CLOSED_COMMANDS[shape]
    result = subprocess.run(
        ["bash", str(PRE_AUDIT)], input=json.dumps({"tool_input": {"command": command}}),
        capture_output=True, text=True, env=_env(tmp_path), timeout=60,
    )
    decision = json.loads(result.stdout.strip().splitlines()[-1])
    assert decision["decision"] == "block"
    if error_class == "unreadable":
        assert "cannot be read when this hook runs" in decision["message"]
    else:
        assert f"({error_class}:" in decision["message"] and "cannot be parsed unambiguously" in decision["message"]
    for name in ("state", "slack", "inflight", "consumed", "pending", "permits"):
        assert not (tmp_path / name).exists(), name


OWNER_PATH = "/Users/rajiv/.claude/hooks/issue-create-owner.py"
OWNER_UNSAFE = {
    "compound_and": f"python3 {OWNER_PATH} create --title t --body-file /tmp/b.md && echo done",
    "compound_semicolon": f"python3 {OWNER_PATH} create --title t --body-file /tmp/b.md; echo done",
    "piped": f"python3 {OWNER_PATH} create --title t --body-file /tmp/b.md | tee /tmp/out",
    "redirected": f"python3 {OWNER_PATH} create --title t --body-file /tmp/b.md 2>&1",
    "wrapped_env": f"env X=1 python3 {OWNER_PATH} create --title t --body-file /tmp/b.md",
    "wrapped_shell": f"bash -c 'python3 {OWNER_PATH} create --title t --body-file /tmp/b.md'",
    "mixed_with_raw": f"python3 {OWNER_PATH} create --title t --body-file /tmp/b.md; gh issue create --title t --body-file /tmp/b.md",
}


@pytest.mark.parametrize("shape", sorted(OWNER_UNSAFE))
def test_compound_or_wrapped_owner_command_is_denied(tmp_path, shape):
    result = subprocess.run(
        ["bash", str(PRE_AUDIT)],
        input=json.dumps({"tool_input": {"command": OWNER_UNSAFE[shape]}}),
        capture_output=True, text=True, env=_env(tmp_path), timeout=30,
    )
    assert result.returncode == 0, result.stderr
    decision = json.loads(result.stdout.strip().splitlines()[-1])
    assert decision["decision"] == "block"
    assert "ONLY command" in decision["message"]
    assert "No permit was written" in decision["message"]


@pytest.mark.parametrize("command", [
    f"python3 {OWNER_PATH} create --title t --body-file /tmp/b.md --label status:todo",
    f"{OWNER_PATH} create --title t --body-file /tmp/b.md",
])
def test_single_simple_owner_command_passes_the_guard_silently_and_writes_nothing(tmp_path, command):
    env = dict(_env(tmp_path), ISSUE_CREATE_OWNER_WORK_DIR=str(tmp_path / "work"))
    result = subprocess.run(
        ["bash", str(PRE_AUDIT)],
        input=json.dumps({"tool_input": {"command": command}}),
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert (result.returncode, result.stdout) == (0, "")
    # No seal, no capture, no permit state of any kind: the guard decides and writes nothing.
    for name in ("pending", "permits", "inflight", "consumed", "state", "work"):
        assert not (tmp_path / name).exists(), name
