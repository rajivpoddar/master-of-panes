"""Obligation 23162 follow-up (CTO APPROVE, C0ALZJHGE49 thread 1792341469.668239):
no pr-state-sweep row may render UNSUPPORTED_LIFECYCLE_ACTION or a retired helper
name. Every previously typed-stop row carries a current executable packet:

- block-pr            -> gh pr edit N --add-label pm-state:blocked-rework,pm-blocked:<reason>
                         (+ head-bound pm-dependency-watch:v1 comment for dependency rows)
- pm-review           -> gh pr edit N --add-label pm-state:pm-review-pending, then Agent(pm-admission-reviewer)
- pm-review-done      -> PM 12b PR comment, then ~/.claude/scripts/ci/pm-admit-ci.sh N
- reconcile-capacity  -> Skill(direct-release)/Skill(direct-assign) with the complete tuple

pm-state:qa-passed-awaiting-ci is CTO-owned label-gated CI; no packet may add it.

Static checks read the shared asset; the runtime check runs the real sweep.sh in
DRY_RUN with a PATH gh shim, a dead MOP_HOST and redirected sentinel/DB paths
(hermetic, same harness shape as test_sweep_pending_offslot_guard.py).
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).parents[3]
SWEEP = Path(
    os.environ.get(
        "SWEEP_UNDER_TEST",
        ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "skills" / "pr-state-sweep" / "scripts" / "sweep.sh",
    )
)
CLEAR_SLOT = ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "scripts" / "mop-clear-slot.sh"
LOG_PATH = Path("/tmp/pr-state-sweep.log")

FORBIDDEN = (
    "UNSUPPORTED_LIFECYCLE_ACTION",
    "reconcile-capacity",
    "pm-transition",
    "pm-ops-sync-bg",
    "pm-review-done",
)
# The add-label VALUE token itself must never carry the CTO-owned label.
QA_LABEL_ADD = re.compile(r"--add-label(?:\\ |\s+)[^\s\"']*pm-state:qa-passed-awaiting-ci")


def _sweep_text() -> str:
    return SWEEP.read_text(encoding="utf-8")


def test_sweep_source_has_no_typed_stop_or_retired_helper_names_in_rows() -> None:
    text = _sweep_text()
    assert "UNSUPPORTED_LIFECYCLE_ACTION" not in text
    assert "reconcile-capacity" not in text
    assert "pm-ops-sync-bg" not in text
    assert "pm-transition" not in text
    for helper in (
        "def block_pr_command",
        "def pm_review_command",
        "def pm_review_done_command",
        "def capture_dispatch_command",
    ):
        assert helper in text, helper
    assert "pm-state:blocked-rework,pm-blocked:" in text
    assert "pm-state:pm-review-pending" in text
    assert "Agent(pm-admission-reviewer)" in text
    assert "~/.claude/scripts/ci/pm-admit-ci.sh" in text
    assert "pm-dependency-watch:v1" in text
    assert not QA_LABEL_ADD.search(text)


def test_mop_clear_slot_sentinel_does_not_name_retired_helper() -> None:
    text = CLEAR_SLOT.read_text(encoding="utf-8")
    assert "pm-transition" not in text
    assert "reconcile-capacity" not in text
    assert "Skill(direct-assign)" in text


def fixture_pr(number: int, labels: list[str], *, draft: bool = False, merge_state: str = "CLEAN") -> dict:
    now = datetime.now(timezone.utc)
    return {
        "number": number,
        "title": f"fixture probe {number}",
        "body": "",
        "closingIssuesReferences": [{"number": 8000 + number}],
        "isDraft": draft,
        "labels": [{"name": name} for name in labels],
        "headRefName": f"fix/executable-packets-probe-{number}",
        "headRefOid": f"{number:04d}".ljust(40, "c"),
        "mergeStateStatus": merge_state,
        "statusCheckRollup": [],
        "createdAt": (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updatedAt": (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "url": f"https://github.com/heydonna-app/heydonna-app/pull/{number}",
    }


FIXTURES = {
    8101: fixture_pr(8101, ["pm-state:blocked-rework"], merge_state="DIRTY"),
    8102: fixture_pr(8102, []),
    8103: fixture_pr(8103, [], draft=True),
    8104: fixture_pr(8104, ["pm-state:qa-passed-awaiting-ci"], merge_state="DIRTY"),
    8105: fixture_pr(8105, ["pm-state:blocked-rework", "pm-blocked:dependency"]),
    8106: fixture_pr(8106, ["pm-state:qa-passed-awaiting-ci", "slot:3"]),
    8107: fixture_pr(8107, ["pm-state:pm-review-pending"]),
}


def run_sweep(tmp_path: Path) -> str:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    prs = list(FIXTURES.values())
    gh = bindir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *"--state open"* ]]; then\n'
        f"  printf '%s' '{json.dumps(prs)}'\n"
        'elif [[ "$*" == *"--state merged"* ]]; then\n'
        "  printf '[]'\n"
        "else\n"
        "  printf '[]'\n"
        "fi\n",
        encoding="utf-8",
    )
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    log_size = LOG_PATH.stat().st_size if LOG_PATH.exists() else None
    env = dict(
        os.environ,
        PATH=str(bindir) + os.pathsep + os.environ.get("PATH", ""),
        MOP_HOST="http://127.0.0.1:9",
        DRY_RUN="1",
        TRIGGER="manual",
        GH_REPO="heydonna-app/heydonna-app",
        PR_STATE_SWEEP_SENTINEL=str(tmp_path / "sentinel.json"),
        PR_STATE_SWEEP_CLEAN_PROOF=str(tmp_path / "clean.json"),
        PM_OPS_DB=str(tmp_path / "pm-ops.db"),
    )
    try:
        completed = subprocess.run(["bash", str(SWEEP)], env=env, capture_output=True, text=True, timeout=280)
    finally:
        if log_size is not None and LOG_PATH.exists():
            with open(LOG_PATH, "r+b") as handle:
                handle.truncate(log_size)
    assert completed.returncode == 0, f"sweep failed rc={completed.returncode}: {completed.stdout[-2000:]} {completed.stderr[-500:]}"
    return completed.stdout


def rows_for(out: str, kind: str, pr: int) -> list[str]:
    return [line for line in out.splitlines() if f" {kind} PR#{pr} " in line or line.startswith(f"{kind} PR#{pr} ")]


def test_runtime_rows_carry_executable_packets_and_no_retired_names(tmp_path: Path) -> None:
    out = run_sweep(tmp_path)
    for token in FORBIDDEN:
        assert token not in out, f"rendered sweep output still contains {token}:\n{out[-3000:]}"
    assert not QA_LABEL_ADD.search(out)

    rebase = rows_for(out, "PR_REBASE_REQUIRED", 8101)
    assert rebase, out
    assert "command=gh\\ pr\\ edit\\ 8101\\ --add-label\\ pm-state:blocked-rework,pm-blocked:rebase" in rebase[0]

    for pr in (8102, 8103):
        label_rows = rows_for(out, "PR_STATE_LABEL_REQUIRED", pr)
        assert label_rows, (pr, out)
        assert (
            f"command=gh\\ pr\\ edit\\ {pr}\\ --add-label\\ pm-state:pm-review-pending\\ &&\\ Agent(pm-admission-reviewer)"
            in label_rows[0]
        )

    qa_rebase = rows_for(out, "PR_REBASE_REQUIRED", 8104)
    assert qa_rebase, out
    assert (
        "--add-label\\ pm-state:blocked-rework,pm-blocked:rebase\\ --remove-label\\ pm-state:qa-passed-awaiting-ci"
        in qa_rebase[0]
    )

    wedge = rows_for(out, "PR_DEPENDENCY_WEDGE_REQUIRED", 8105)
    assert wedge, out
    assert "--add-label\\ pm-state:blocked-rework,pm-blocked:dependency\\ &&\\ gh\\ pr\\ comment\\ 8105" in wedge[0]
    assert "pm-dependency-watch:v1" in wedge[0]
    assert '"head":"8105' in wedge[0]

    release = rows_for(out, "PR_SLOT_RELEASE_BEFORE_CI_REQUIRED", 8106)
    assert release, out
    assert "Skill(direct-release)" in release[0] and "Skill(direct-assign)" in release[0]

    review = rows_for(out, "PR_PM_REVIEW_REQUIRED", 8107)
    assert review, out
    assert "pm-state:pm-review-pending\\ &&\\ Agent(pm-admission-reviewer)" in review[0]
    assert "~/.claude/scripts/ci/pm-admit-ci.sh\\ 8107" in review[0]
    assert "pm-state:blocked-rework,pm-blocked:pm-gate" in review[0]
