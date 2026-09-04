#!/usr/bin/env python3
"""Classify internal HeyDonna control-plane issue proposals.

Control-plane defects are operational incidents. They must be reported to the
CTO in the authoritative Slack thread and repaired there, not filed as product
or app-test GitHub issues.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys


INTERNAL_FOLLOWUP_MARKER = "<!-- audit-bypass: internal-followup -->"

_EXPLICIT_TITLE_RE = re.compile(
    r"(?:^|[\[(])(?:fix|test|chore|infra)?[(:\s-]*control[-_ ]plane\b",
    re.IGNORECASE,
)
_STRUCTURED_BODY_RE = re.compile(
    r"(?mi)^\s*(?:lane|owner|work_type)\s*:\s*"
    r"(?:internal[-_ ]?)?control[-_ ]plane\b"
)
# Control plane is bounded to the PR/slot state machine: typed PR
# transitions, slot ownership/epochs, and admission/dispatch. An internal
# follow-up is control-plane work only when it carries explicit structured
# metadata (above) or names an actual machine path/symbol. scripts/ci/** is APP_CI per the CTO boundary
# (workflow implementation), so only scripts/pm matches. Incidental prose
# that merely mentions a control-plane-adjacent tool (for example "the
# affected-test planner") is not control-plane evidence, so only the planner
# script symbol `affected-test-plan[.py]` matches here -- never the prose
# forms "planner"/"planning".
_INTERNAL_SURFACE_RE = re.compile(
    r"\b(?:"
    r"github/workflows|"
    r"scripts/pm|"
    r"affected[-_ ]test[-_ ]plan(?:\.py)?|"
    r"pm[-_ ]transition|"
    r"pr[-_ ]ci[-_ ]readiness[-_ ]gate|"
    r"slot[-_ ](?:dispatch|claim|submit[-_ ]ready|ready)|"
    r"backlog[-_ ]triage|"
    r"issue[-_ ]admission|"
    r"(?:issue[-_ ]contract[-_ ]ledger|icl)[-_ ](?:hook|validator|admission)|"
    r"master[-_ ]of[-_ ]panes|"
    r"control[-_ ]plane"
    r")\b",
    re.IGNORECASE,
)
_PRODUCT_FOLLOWUP_OWNER_RE = re.compile(
    r"(?mi)^\s*owner\s*:\s*slot[-_ ]dispatch\s*$"
)


def internal_control_plane_reason(title: str, body: str) -> str | None:
    """Return a stable reason when a proposed issue is control-plane work."""
    if _EXPLICIT_TITLE_RE.search(title):
        return "explicit_control_plane_title"
    if _STRUCTURED_BODY_RE.search(body):
        return "structured_control_plane_metadata"
    followup_contract = _PRODUCT_FOLLOWUP_OWNER_RE.sub("", body)
    if (
        INTERNAL_FOLLOWUP_MARKER in body
        and _INTERNAL_SURFACE_RE.search(f"{title}\n{followup_contract}")
    ):
        return "internal_followup_targets_control_plane_surface"
    return None


def followup_issue_number(value: str) -> int:
    """Normalize an accepted issue reference to its numeric issue id."""
    match = re.search(r"(?:^#|/issues/)?(\d+)(?:/?$)", value.strip())
    if not match:
        raise ValueError(
            "follow-up must be an issue number, #number, or GitHub issue URL"
        )
    return int(match.group(1))


def followup_issue_contract_error(issue: dict[str, object]) -> str | None:
    """Reject closed or control-plane tickets as affected-test follow-ups."""
    number = issue.get("number") or "unknown"
    state = str(issue.get("state") or "").upper()
    if state != "OPEN":
        return f"follow-up issue #{number} is not open"
    reason = internal_control_plane_reason(
        str(issue.get("title") or ""),
        str(issue.get("body") or ""),
    )
    if reason:
        return (
            f"follow-up issue #{number} is internal control-plane work ({reason}); "
            "report it to the CTO in the authoritative operational thread and "
            "repair the shared control point instead"
        )
    return None


def validate_live_followup_issue(
    value: str,
    *,
    repo: str = "heydonna-app/heydonna-app",
    cwd: str | None = None,
    gh_bin: str | None = None,
) -> dict[str, object]:
    """Fetch and validate the current product/app-test follow-up contract."""
    number = followup_issue_number(value)
    proc = subprocess.run(
        [
            gh_bin or os.environ.get("GH_BIN", "gh"),
            "issue",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "number,state,title,body",
        ],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    if proc.returncode != 0:
        raise ValueError(
            f"cannot verify live follow-up issue #{number}: "
            f"{(proc.stderr or proc.stdout).strip() or 'gh issue view failed'}"
        )
    try:
        issue = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"cannot parse live follow-up issue #{number}"
        ) from exc
    error = followup_issue_contract_error(issue)
    if error:
        raise ValueError(error)
    return issue


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate-followup",))
    parser.add_argument("--issue", required=True)
    parser.add_argument("--repo", default="heydonna-app/heydonna-app")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        issue = validate_live_followup_issue(args.issue, repo=args.repo)
    except ValueError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}))
        else:
            print(str(exc), file=sys.stderr)
        return 13
    if args.json:
        print(json.dumps({"ok": True, "issue": issue}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
