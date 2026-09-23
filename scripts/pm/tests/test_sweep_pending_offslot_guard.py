"""Focused proof: pr-state-sweep never advances pm-state:pm-review-pending as
stale legacy on off-slot PRs (no slot:* label), while slot-authored behavior
is unchanged.

Runs the real sweep.sh end-to-end in DRY_RUN with a PATH gh shim (two fixture
PRs), dead MOP_HOST, redirected sentinel/clean-proof/PM_OPS_DB, fixture PASS
markers, and byte-exact LOG restoration. Hermetic: no live GitHub/MoP/DB/slot
mutation; the only live touch is an appended-then-truncated LOG segment.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[3]
SWEEP = Path(
    os.environ.get(
        "SWEEP_UNDER_TEST",
        ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "skills" / "pr-state-sweep" / "scripts" / "sweep.sh",
    )
)

OFF_PR = 7990
SLOT_PR = 7991
OFF_HEAD = "a" * 40
SLOT_HEAD = "b" * 40
OFF_BRANCH = "fix/offslot-pending-guard-probe-7990"
SLOT_BRANCH = "fix/slot-pending-guard-probe-7991"
LOG_PATH = Path("/tmp/pr-state-sweep.log")

MARKER_BODY = """PM_CLAUDE_REVIEW: PASS
review_model: sonnet
model_reason: fixture probe for offslot guard
headRefOid: {head}
runtime_control_point: fixture
pass_scope: phase-a
"""


def marker_path(number: int, head: str) -> Path:
    return Path(f"/tmp/pm-claude-code-review-{number}-{head}.md")


def fixture_pr(number: int, head: str, branch: str, labels: list[str]) -> dict:
    return {
        "number": number,
        "title": f"fixture probe {number}",
        "body": "",
        "closingIssuesReferences": [{"number": 7000 + number}],
        "isDraft": False,
        "labels": [{"name": name} for name in labels],
        "headRefName": branch,
        "headRefOid": head,
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [],
        "createdAt": "2026-09-18T00:00:00Z",
        "updatedAt": "2026-09-19T00:00:00Z",
        "url": f"https://github.com/heydonna-app/heydonna-app/pull/{number}",
    }


def run_sweep(sweep: Path, tmp_path: Path, review_fixtures=None) -> str:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    prs = [
        fixture_pr(OFF_PR, OFF_HEAD, OFF_BRANCH, ["pm-state:pm-review-pending", "ci-head:" + "d" * 40]),
        fixture_pr(SLOT_PR, SLOT_HEAD, SLOT_BRANCH, ["pm-state:pm-review-pending", "slot:2"]),
    ]
    gh = bindir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *"--state open"* ]]; then\n'
        f"  printf '%s' '{json.dumps(prs)}'\n"
        "elif [[ \"$*\" == *\"--state merged\"* ]]; then\n"
        "  printf '[]'\n"
        'elif [[ "$*" == *"/pulls/7990/reviews"* ]]; then\n'
        '  printf "%s" "$REVIEW_7990"\n'
        'elif [[ "$*" == *"/pulls/7991/reviews"* ]]; then\n'
        '  printf "%s" "$REVIEW_7991"\n'
        "else\n"
        "  printf '[]'\n"
        "fi\n",
        encoding="utf-8",
    )
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    markers = []
    for number, head in ((OFF_PR, OFF_HEAD), (SLOT_PR, SLOT_HEAD)):
        marker = marker_path(number, head)
        marker.write_text(MARKER_BODY.format(head=head), encoding="utf-8")
        markers.append(marker)
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
        REVIEW_7990=json.dumps([review_fixtures.get(str(OFF_PR), [])]) if review_fixtures else "[[]]",
        REVIEW_7991=json.dumps([review_fixtures.get(str(SLOT_PR), [])]) if review_fixtures else "[[]]",
    )
    try:
        completed = subprocess.run(
            ["bash", str(sweep)], env=env, capture_output=True, text=True, timeout=280,
        )
    finally:
        for marker in markers:
            marker.unlink(missing_ok=True)
        if log_size is not None and LOG_PATH.exists():
            with open(LOG_PATH, "r+b") as handle:
                handle.truncate(log_size)
    assert completed.returncode == 0, f"sweep failed rc={completed.returncode}: {completed.stdout[-2000:]} {completed.stderr[-500:]}"
    return completed.stdout


def test_offslot_pending_survives_while_slot_completes(tmp_path) -> None:
    assert SWEEP.is_file(), f"sweep missing: {SWEEP}"
    out = run_sweep(SWEEP, tmp_path)
    assert f"PR_PM_REVIEW_COMPLETE_REQUIRED PR#{SLOT_PR}" in out, (
        f"slot-authored control must still complete:\n{out[-3000:]}"
    )
    assert f"PR_PM_REVIEW_COMPLETE_REQUIRED PR#{OFF_PR}" not in out, (
        f"off-slot pm-review-pending must survive the sweep:\n{out[-3000:]}"
    )
    assert "PR_SWEEP_ACTIONABLE" in out or "PR_SWEEP_CLEAN" in out, "sweep must reach its terminal"


def test_codex_review_body_badge_is_reported_but_dashboard_summary_is_not(tmp_path) -> None:
    review_fixtures = {
        str(OFF_PR): [{
            "id": 5289094711,
            "user": {"login": "chatgpt-codex-connector[bot]"},
            "state": "COMMENTED",
            "commit_id": OFF_HEAD,
            "body": "### P1 Preserve manual hard rejects through the API route",
        }],
        str(SLOT_PR): [{
            "id": 5289094712,
            "user": {"login": "chatgpt-codex-connector[bot]"},
            "state": "COMMENTED",
            "commit_id": SLOT_HEAD,
            "body": "Review Summary: no findings were included in this dashboard body.",
        }],
    }
    out = run_sweep(SWEEP, tmp_path, review_fixtures)
    assert (
        f"PR_CODEX_REVIEW_BODY_FINDING PR#{OFF_PR} review=5289094711 severity=P1 "
        f"review_commit={OFF_HEAD} head={OFF_HEAD} head_moved=false "
        "unresolved_by_default=true resolution_requires_moved_head_and_finding_addressed"
    ) in out, f"review-body-only P1 was not surfaced:\n{out[-3000:]}"
    assert "review=5289094712" not in out, "unbadged dashboard summary must not be flagged"
