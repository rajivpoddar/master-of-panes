"""Focused proof: the CI-arming guard matches the executed invocation, not
free-text prose carried inside command arguments.

RED shapes (observed 2026-09-19, #7925 ledger row): ledger-write prose that
merely MENTIONS the pm-state transition label (R1) or NAMES the rerun /
workflow-dispatch commands as prohibited (R2) must ALLOW. Genuine arm/rerun/
dispatch invocations — including quoted label values and compound segments —
must keep BLOCKING. Hermetic: the hook is executed as a subprocess with
synthetic PreToolUse JSON on stdin; nothing live is touched.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[3]
HOOK = ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "hooks" / "block-ci-rerun-without-local-proof.sh"

PM_OPS = "python3 /Users/rajiv/.claude/scripts/pm-ops.py obligation-upsert --kind ci_rework --pr 7925"

R1_PROSE_LABEL = (
    PM_OPS + ' --action "Record 7925 rework: consumed pm-state-replace.sh 7925 '
    'qa-passed-awaiting-ci per CTO admission; CI started"'
)
R2_PROSE_COMMANDS = (
    PM_OPS + ' --action "Do NOT run gh run rerun or gh workflow run ci.yml manually; '
    'CI arming is CTO-owned"'
)


def verdict(command: str) -> int:
    payload = json.dumps({"tool_input": {"command": command}})
    completed = subprocess.run(
        ["bash", str(HOOK)], input=payload, capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode in (0, 2), f"unexpected hook exit {completed.returncode}: {completed.stderr[:300]}"
    return completed.returncode


def test_hook_present_and_executable() -> None:
    assert HOOK.is_file(), f"canonical hook missing: {HOOK}"


def test_prose_mentioning_transition_label_allows() -> None:
    assert verdict(R1_PROSE_LABEL) == 0, "ledger prose describing a transition must not trip the guard"


def test_prose_naming_prohibited_commands_allows() -> None:
    assert verdict(R2_PROSE_COMMANDS) == 0, "ledger prose naming prohibited commands must not trip the guard"


def test_genuine_rerun_still_blocks() -> None:
    assert verdict("gh run rerun 34597650181") == 2


def test_genuine_workflow_dispatch_still_blocks() -> None:
    assert verdict("gh workflow run ci.yml --ref main") == 2


def test_genuine_label_arm_still_blocks() -> None:
    assert verdict("gh pr edit 7925 --add-label pm-state:qa-passed-awaiting-ci") == 2


def test_genuine_quoted_label_arm_still_blocks() -> None:
    assert verdict("gh pr edit 7925 --add-label 'pm-state:qa-passed-awaiting-ci'") == 2


def test_genuine_compound_invocations_still_block() -> None:
    assert verdict("echo ok && gh run rerun 1") == 2
    assert verdict("echo start; gh workflow run e2e.yml") == 2


def test_genuine_api_mutation_still_blocks() -> None:
    assert verdict("gh api -X PATCH repos/o/r/issues/1 -f labels[]=pm-state:qa-passed-awaiting-ci") == 2


def test_approved_wrapper_still_allows() -> None:
    assert verdict("/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/ci/request-label-gated-ci.sh --pr 1") == 0


def test_ordinary_label_edit_still_allows() -> None:
    assert verdict("gh pr edit 7729 --remove-label pm-state:blocked-rework") == 0
    assert verdict("echo hello") == 0


def test_heredoc_prose_naming_rerun_allows() -> None:
    assert verdict("cat <<'EOF' > /tmp/note.txt\ngh run rerun 123\ngh workflow run ci.yml\nEOF") == 0


def test_single_quoted_prose_allows() -> None:
    ledger = (
        "python3 /Users/rajiv/.claude/scripts/pm-ops.py obligation-upsert --kind ci_rework "
        "--pr 7925 --action 'do not gh workflow run e2e.yml outside the conveyor'"
    )
    assert verdict(ledger) == 0


def test_sh_c_wrapper_genuine_still_blocks() -> None:
    assert verdict("sh -c 'gh run rerun 5'") == 2


def test_wrapper_mention_in_comment_does_not_allowlist_genuine_rerun() -> None:
    assert verdict("gh run rerun 9 # see request-label-gated-ci.sh docs") == 2


def test_rtk_prefixed_rerun_still_blocks() -> None:
    assert verdict("rtk gh run rerun 5") == 2


def test_sudo_prefixed_rerun_still_blocks() -> None:
    assert verdict("sudo gh run rerun 5") == 2
