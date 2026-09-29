#!/usr/bin/env python3
"""Live open-PR snapshot shared by the hourly drive and the 3h heartbeat.

State comes only from live GitHub (`gh pr list` statusCheckRollup on the
current headRefOid). pm-ops rows and advisory pm-state labels are never used
to decide state. Read-only; takes no PR actions.

Output (JSON, or --text one line per PR):
  {number, head, title, draft, state, owner, next}
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

REPO = "heydonna-app/heydonna-app"
RED = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}
PENDING = {"", "PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "EXPECTED", "REQUESTED"}
# Required merge gates: the CI `test` aggregate job (ci.yml) and the E2E `e2e` job
# (e2e.yml). Branch protection is not readable on this plan, so these aggregate job
# names are the required-check source. Every other check (e.g. core-large-file-correctness)
# is advisory: it never makes a PR red and never suppresses a merge ask.
GATES = ("test", "e2e")


def _check_result(check: dict) -> tuple[str, str]:
    name = check.get("name") or check.get("context") or "?"
    value = (check.get("conclusion") or check.get("state") or "").upper()
    if "exemption" in (check.get("workflowName") or "").lower():
        value = "PLACEHOLDER"  # CI-exemption stub: never counts as a real green gate
    if not check.get("conclusion") and (check.get("status") or "").upper() not in ("", "COMPLETED"):
        value = ""
    return name, value


def summarize_checks(rollup: list[dict]) -> dict:
    """Pure: collapse duplicate-named checks (placeholder SKIPPED + real run)."""
    by_name: dict[str, set[str]] = {}
    for check in rollup or []:
        name, value = _check_result(check)
        by_name.setdefault(name, set()).add(value)
    red = sorted(n for n, v in by_name.items() if v & RED)
    pending = sorted(n for n, v in by_name.items() if n not in red and v & PENDING)
    green = sorted(n for n, v in by_name.items() if n not in red and n not in pending and "SUCCESS" in v)
    return {"red": red, "pending": pending, "green": green}


def classify_pr(pr: dict) -> dict:
    """Pure: one PR -> state/owner/next from live head checks and labels."""
    head = pr.get("headRefOid") or ""
    labels = [label.get("name", "") for label in pr.get("labels", [])]
    slots = [x.split(":", 1)[1] for x in labels if x.startswith("slot:")]
    admitted = f"ci-head:{head}" in labels
    # PM owns the CI/E2E terminal of an admitted head (Rajiv 2026-09-28); a slot label
    # only names the owner before admission.
    owner = "PM" if admitted or not slots else f"S{slots[0]}"
    stale_admission = any(x.startswith("ci-head:") for x in labels) and not admitted
    blocked = [x.split(":", 1)[1] for x in labels if x.startswith("pm-blocked:")]
    checks = summarize_checks(pr.get("statusCheckRollup") or [])
    gates_green = all(g in checks["green"] for g in GATES)

    required_red = [n for n in checks["red"] if n in GATES]
    advisory_red = [n for n in checks["red"] if n not in GATES]
    gates_pending = any(g in checks["pending"] for g in GATES)

    if pr.get("isDraft"):
        state, nxt = "draft", f"{owner} finishing"
    elif blocked:
        # A pm-blocked hold wins over every CI state: a blocked PR never gets a merge ask.
        state, nxt = f"held ({','.join(blocked)})", f"{owner} / {blocked[0]} wait"
    elif required_red:
        state = "red: " + ",".join(required_red)
        e2e = any("e2e" in n for n in required_red)
        nxt = "e2e-failure-investigator" if e2e else "ci-repair-agent"
        if owner == "PM":
            state += ", unowned"
    elif gates_pending or (admitted and not gates_green and checks["pending"]):
        state, nxt = "CI running", "await terminal"
    elif gates_green:
        state = "CI+E2E green"
        if not admitted:
            nxt = "not admitted: no merge ask"
        elif slots:
            nxt = f"slot:{slots[0]} lane open: no merge ask"
        else:
            nxt = "CTO merge ask"
    else:
        state = "stale admission" if stale_admission else "not admitted"
        nxt = "PM 12b review -> pm-admit-ci" if owner == "PM" else f"{owner} rework -> PM admit"
    if advisory_red:
        state += " (advisory red: " + ",".join(advisory_red[:3]) + ")"
    return {
        "number": pr.get("number"),
        "head": head[:7],
        "head_full": head,
        "title": (pr.get("title") or "")[:60],
        "draft": bool(pr.get("isDraft")),
        "admitted": admitted,
        "state": state,
        "owner": owner,
        "next": nxt,
    }


def format_line(row: dict) -> str:
    return f"#{row['number']} {row['head']} — {row['state']} — {row['owner']} — {row['next']}"


def fetch_open_prs() -> list[dict]:
    proc = subprocess.run(
        [
            "gh", "pr", "list", "--repo", REPO, "--state", "open", "--limit", "100",
            "--json", "number,headRefOid,title,isDraft,labels,statusCheckRollup",
        ],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh pr list failed: {proc.stderr.strip()[:200]}")
    return json.loads(proc.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description="Live open-PR snapshot")
    parser.add_argument("--text", action="store_true")
    args = parser.parse_args()
    rows = sorted((classify_pr(pr) for pr in fetch_open_prs()), key=lambda r: -int(r["number"]))
    if args.text:
        print("\n".join(format_line(r) for r in rows))
    else:
        print(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
