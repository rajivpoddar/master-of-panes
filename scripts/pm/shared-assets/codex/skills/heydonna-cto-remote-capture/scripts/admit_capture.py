#!/usr/bin/env python3
"""CTO admission edge for one verified exact-head strict-fixture capture.

Consumes a fixture-miss terminal tuple directly. No PM state, labels, retry
budgets, affected-test receipts, or legacy capture-required CI verdict.
Dry-run is read-only; --apply dispatches at most one canonical capture run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

CAPTURE_WORKFLOW = "E2E LLM Proxy Capture (manual)"
DEFAULT_REPO = "heydonna-app/heydonna-app"
HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
REQUIRED = ("pr_number", "head_sha", "head_branch", "source_run", "control_sha", "capture_profile")


class Refused(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _gh(gh_bin: str, args: list[str]) -> str:
    proc = subprocess.run([gh_bin, *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise Refused("gh_query_failed", f"gh {' '.join(args[:3])}: {proc.stderr.strip()[:200]}")
    return proc.stdout


def load_terminal(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001
        raise Refused("terminal_unreadable", str(exc)[:200]) from exc
    if not isinstance(data, dict):
        raise Refused("terminal_malformed", "terminal must be a JSON object")
    for key in REQUIRED:
        if data.get(key) in (None, ""):
            raise Refused("terminal_malformed", f"missing {key}")
    if data.get("fixture_miss") is not True:
        raise Refused("non_fixture_evidence", "fixture_miss must be exactly true")
    if not str(data.get("cause", "")).strip():
        raise Refused("non_fixture_evidence", "cause is required")
    if not HEAD_RE.match(str(data["head_sha"])) or not HEAD_RE.match(str(data["control_sha"])):
        raise Refused("terminal_malformed", "head_sha/control_sha must be 40-hex")
    return data


def fence_live_pr(gh_bin: str, repo: str, t: dict) -> None:
    raw = _gh(gh_bin, ["pr", "view", str(t["pr_number"]),
                       "--json", "state,headRefOid,headRefName,isDraft", "--repo", repo])
    pr = json.loads(raw)
    if pr.get("state") != "OPEN":
        raise Refused("pr_not_open", str(pr.get("state")))
    if pr.get("headRefOid") != t["head_sha"]:
        raise Refused("head_drift", f"live={pr.get('headRefOid')} terminal={t['head_sha']}")
    if pr.get("headRefName") and pr["headRefName"] != t["head_branch"]:
        raise Refused("branch_drift", f"live={pr.get('headRefName')} terminal={t['head_branch']}")


def fence_source_run(gh_bin: str, repo: str, t: dict) -> None:
    raw = _gh(gh_bin, ["run", "view", str(t["source_run"]),
                       "--json", "headSha,attempt", "--repo", repo])
    run = json.loads(raw)
    if run.get("headSha") != t["head_sha"]:
        raise Refused("source_run_head_mismatch", f"run={run.get('headSha')} terminal={t['head_sha']}")
    attempt = t.get("source_attempt")
    if attempt is not None and run.get("attempt") not in (None, attempt):
        raise Refused("source_run_attempt_mismatch", f"run={run.get('attempt')} terminal={attempt}")


def fence_duplicate(gh_bin: str, repo: str, t: dict) -> None:
    raw = _gh(
        gh_bin,
        ["run", "list", "--workflow", CAPTURE_WORKFLOW, "--limit", "50",
         "--json", "databaseId,headSha,status,conclusion", "--repo", repo],
    )
    for run in json.loads(raw):
        if run.get("headSha") != t["head_sha"]:
            continue
        status = str(run.get("status", ""))
        if status in ("queued", "in_progress", "requested", "waiting", "pending"):
            raise Refused("duplicate_active_capture", f"run {run.get('databaseId')} {status}")
        if status == "completed" and str(run.get("conclusion")) == "success":
            raise Refused("duplicate_successful_capture", f"run {run.get('databaseId')}")


def dispatch(gh_bin: str, repo: str, t: dict) -> list[str]:
    argv = [
        gh_bin, "workflow", "run", CAPTURE_WORKFLOW,
        "--ref", str(t["head_branch"]),
        "-f", f"pr_number={t['pr_number']}",
        "-f", f"head_sha={t['head_sha']}",
        "-f", f"head_branch={t['head_branch']}",
        "-f", f"control_sha={t['control_sha']}",
        "-f", f"capture_profile={t['capture_profile']}",
        "--repo", repo,
    ]
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise Refused("dispatch_failed", proc.stderr.strip()[:200])
    return argv


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Admit one exact-head strict-fixture capture.")
    ap.add_argument("--terminal", required=True, type=Path)
    ap.add_argument("--apply", action="store_true", help="dispatch after all fences pass")
    ap.add_argument("--gh-bin", default=os.environ.get("HD_GH_BIN", "gh"))
    ap.add_argument("--repo", default=DEFAULT_REPO,
                    help="owner/name of the repository owning the PR and capture workflow")
    args = ap.parse_args(argv)

    try:
        t = load_terminal(args.terminal)
        fence_live_pr(args.gh_bin, args.repo, t)
        fence_source_run(args.gh_bin, args.repo, t)
        fence_duplicate(args.gh_bin, args.repo, t)
    except Refused as refused:
        print(json.dumps({"status": "REFUSED", "reason": refused.reason, "detail": refused.detail}))
        return 2

    if not args.apply:
        print(json.dumps({
            "status": "READY_TO_CAPTURE",
            "pr_number": t["pr_number"], "head_sha": t["head_sha"],
            "source_run": t["source_run"], "capture_profile": t["capture_profile"],
        }))
        return 0

    try:
        argv_used = dispatch(args.gh_bin, args.repo, t)
    except Refused as refused:
        print(json.dumps({"status": "REFUSED", "reason": refused.reason, "detail": refused.detail}))
        return 2
    print(json.dumps({"status": "DISPATCHED", "head_sha": t["head_sha"],
                      "capture_profile": t["capture_profile"], "argv": argv_used}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
