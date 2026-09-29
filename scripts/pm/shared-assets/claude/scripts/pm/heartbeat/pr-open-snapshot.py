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
import re
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
CODEX_LABEL = "pm-blocked:codex"
CODEX_AUTHOR = re.compile(r"codex", re.I)  # chatgpt-codex-connector and friends
CODEX_BLOCKING = re.compile(r"\bP[01]\b")  # "![P1 Badge]" etc.


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


def codex_threads(threads: list[dict] | None) -> dict | None:
    """Pure: count unresolved Codex-bot P0/P1 review threads (live vs isOutdated).

    `threads` items: {isResolved, isOutdated, author, body} (first comment).
    None means the live read failed -> unknown.
    """
    if threads is None:
        return None
    live = outdated = 0
    for t in threads:
        if t.get("isResolved") or not CODEX_AUTHOR.search(t.get("author") or ""):
            continue
        if not CODEX_BLOCKING.search(t.get("body") or ""):
            continue
        if t.get("isOutdated"):
            outdated += 1
        else:
            live += 1
    return {"live": live, "outdated": outdated}


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
    # The Codex wait comes from live review threads, never the pm-blocked:codex label.
    codex = codex_threads(pr.get("reviewThreads"))
    has_codex_label = CODEX_LABEL in labels
    blocked = [x.split(":", 1)[1] for x in labels
               if x.startswith("pm-blocked:") and (x != CODEX_LABEL or codex is None)]
    actions: list[str] = []
    if codex and codex["live"]:
        blocked.insert(0, f"codex P1 x{codex['live']}")
    if has_codex_label and codex is not None and codex["live"] == 0:
        actions.append(f"stale pm-blocked:codex label PR#{pr.get('number')}@{head} (0 live Codex P0/P1 threads) -> remove label")
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
    if codex and codex["outdated"]:
        state += f" (codex P1 outdated x{codex['outdated']} — owner confirm)"
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
        "actions": actions,
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
    prs = json.loads(proc.stdout)
    threads = fetch_review_threads()
    for pr in prs:
        pr["reviewThreads"] = None if threads is None else threads.get(pr.get("number"), [])
    return prs


THREADS_QUERY = """query($owner:String!,$name:String!,$after:String){
  repository(owner:$owner,name:$name){pullRequests(states:OPEN,first:25,after:$after){
    pageInfo{hasNextPage endCursor}
    nodes{number reviewThreads(first:100){nodes{isResolved isOutdated
      comments(first:1){nodes{author{login} body}}}}}}}}"""


def fetch_review_threads() -> dict[int, list[dict]] | None:
    """Live review threads per open PR; None on failure (caller falls back to the label)."""
    owner, name = REPO.split("/")
    out: dict[int, list[dict]] = {}
    after = None
    try:
        while True:
            cmd = ["gh", "api", "graphql", "-f", f"query={THREADS_QUERY}",
                   "-F", f"owner={owner}", "-F", f"name={name}"]
            if after:
                cmd += ["-F", f"after={after}"]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if proc.returncode != 0:
                return None
            page = json.loads(proc.stdout)["data"]["repository"]["pullRequests"]
            for node in page["nodes"]:
                rows = []
                for t in node["reviewThreads"]["nodes"]:
                    first = (t["comments"]["nodes"] or [{}])[0]
                    rows.append({"isResolved": t["isResolved"], "isOutdated": t["isOutdated"],
                                 "author": ((first.get("author") or {}).get("login") or ""),
                                 "body": first.get("body") or ""})
                out[node["number"]] = rows
            if not page["pageInfo"]["hasNextPage"]:
                return out
            after = page["pageInfo"]["endCursor"]
    except (subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Live open-PR snapshot")
    parser.add_argument("--text", action="store_true")
    args = parser.parse_args()
    rows = sorted((classify_pr(pr) for pr in fetch_open_prs()), key=lambda r: -int(r["number"]))
    if args.text:
        print("\n".join(format_line(r) for r in rows))
        for r in rows:
            for a in r["actions"]:
                print(f"ACTION: {a}")
    else:
        print(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
