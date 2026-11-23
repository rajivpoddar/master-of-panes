#!/usr/bin/env python3
"""Proxy lane lease check: which live proxy lanes made no OBJECTIVE progress for > N minutes (default 60).

Rajiv D0AMF0XE6TS thread 1795436469.682079, ts 1795438159.967189: a lane's lease is renewed only by objective progress.
  Renews:  a new pushed commit on the lane's branch/PR (non-merge, committed after the lane started),
           a new PR/issue comment by the lane (names the lane or opens with its role prefix, created after it started),
           a posted receipt (receipts/*.json under the run dir whose ref is a GitHub comment URL that GitHub confirms).
  Never:   log freshness, tool activity, thinking tokens, the lane's own claims. Nothing here reads the transcript for liveness.
The grant is the lease_started_at/lease_renewed_at/lease_basis fields on the lane's pm-ops proxy_run row (written by proxy-run-state.py start).

This tool KILLS NOTHING and edits nothing by default. For each STALLED lane it prints the promotion packet (lane, PR/issue, head,
worktree path, unpushed/dirty state) so PM can push the WIP and hand off one tier up (S131). --record additionally writes the
checked lease fields (lease_renewed_at, lease_basis, lease_ref, lease_state, lease_checked_at) onto the lane's pm-ops row.

A source that cannot be read (gh/git timeout, no PR and no branch) is reported as UNAVAILABLE with its reason, never as "no progress".
Commit time is the committer date of the pushed commit (GitHub does not expose push time), so a commit pushed late is credited when made.

Usage: proxy-lease-check.py [--threshold-min 60] [--json] [--record] [--only LANE] [--fail-on-stalled]
Env (tests): PROXY_AGENT_RUN_STATE_DIR, PROXY_LEASE_PS (file with 'pid command' rows), PROXY_LEASE_GH, PROXY_LEASE_GIT, PROXY_LEASE_REPO,
PROXY_AGENT_PM_OPS_CLI.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
RUNS = Path(os.environ.get("PROXY_AGENT_RUN_STATE_DIR", str(HOME / ".local/state/proxy-agent-runs")))
PM_OPS = os.environ.get("PROXY_AGENT_PM_OPS_CLI", str(HOME / ".claude/scripts/pm-ops.py"))
GH = os.environ.get("PROXY_LEASE_GH", "gh")
GIT = os.environ.get("PROXY_LEASE_GIT", "git")
REPO = os.environ.get("PROXY_LEASE_REPO", "heydonna-app/heydonna-app")
LANE = re.compile(r"(?:^|\s)\S*/claude\s+-p\s+--name\s+([A-Za-z0-9._-]+)\s+--model\s+(\S+)")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
COMMENT_URL = re.compile(r"^https://github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/\d+#issuecomment-(\d+)$")
WORKTREE_HINTS = (
    re.compile(rb"lane-clone\.sh\s+clone\s+(/[^\s\"'\\;&|]+)"),
    re.compile(rb"git\s+(?:-C\s+\S+\s+)?worktree\s+add\s+(?:-b\s+\S+\s+)?(/[^\s\"'\\;&|]+)"),
    re.compile(rb"\bcd\s+((?:/private)?/tmp/[^\s\"'\\;&|]+)"),
)
TRANSCRIPT_CAP = 64 * 1024 * 1024
TIMEOUT_S = 25


def parse_ts(value) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def iso(value: dt.datetime | None) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if value else "none"


def run(cmd: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    """(rc, stdout, error). rc 124 = timeout, 127 = cannot start; the error text names why."""
    try:
        done = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT_S, check=False, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except OSError as exc:
        return 127, "", type(exc).__name__
    return done.returncode, done.stdout, (done.stderr or "").strip()[:200]


def gh_lines(path: str, jq: str) -> tuple[list[dict], str]:
    """Objects printed one per line by 'gh api <path> --paginate --jq <jq>', plus an error string ('' when fine)."""
    rc, out, err = run([GH, "api", path, "--paginate", "--jq", jq])
    if rc != 0:
        return [], f"gh_api_{path.split('/')[-1]}:{err or 'rc' + str(rc)}"
    rows = []
    for line in out.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows, ""


def live_lanes(ps_text: str) -> list[dict]:
    lanes = []
    for line in ps_text.splitlines():
        found = LANE.search(line)
        if found and NAME.fullmatch(found.group(1)):
            pid = re.match(r"\s*(\d+)\s", line)
            lanes.append({"name": found.group(1), "model": found.group(2), "pid": int(pid.group(1)) if pid else None})
    return lanes


def manifest_of(name: str) -> dict | None:
    try:
        data = json.loads((RUNS / name / "manifest.json").read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def find_worktree(manifest: dict) -> str:
    """Last existing checkout the lane created (lane-clone.sh / git worktree add / cd /tmp/...), read from its stream-json transcript."""
    out = str(manifest.get("out") or "")
    path = Path(out[:-3] + ".jsonl") if out.endswith(".md") else Path(out + ".jsonl")
    found: list[str] = []
    try:
        with open(path, "rb") as fh:
            carry = b""
            read = 0
            while read < TRANSCRIPT_CAP:
                chunk = fh.read(4 * 1024 * 1024)
                if not chunk:
                    break
                read += len(chunk)
                data = carry + chunk
                for pattern in WORKTREE_HINTS:
                    found.extend(m.group(1).decode("utf-8", "replace") for m in pattern.finditer(data))
                carry = data[-1024:]
    except OSError:
        return ""
    for candidate in reversed(found):
        if (Path(candidate) / ".git").exists():
            return candidate
    return ""


def git_state(worktree: str) -> dict:
    state = {"worktree": worktree or "UNKNOWN", "branch": "", "head": "", "dirty": None, "unpushed": None, "remote_head": ""}
    if not worktree:
        return state
    rc, out, _ = run([GIT, "-C", worktree, "rev-parse", "--abbrev-ref", "HEAD"])
    state["branch"] = out.strip() if rc == 0 else ""
    rc, out, _ = run([GIT, "-C", worktree, "rev-parse", "HEAD"])
    state["head"] = out.strip() if rc == 0 else ""
    rc, out, _ = run([GIT, "-C", worktree, "status", "--porcelain"])
    state["dirty"] = len([x for x in out.splitlines() if x.strip()]) if rc == 0 else None
    branch = state["branch"]
    if branch and branch != "HEAD":
        rc, out, _ = run([GIT, "-C", worktree, "ls-remote", "origin", f"refs/heads/{branch}"])
        found = re.match(r"([0-9a-f]{40})\s", out) if rc == 0 else None
        state["remote_head"] = found.group(1) if found else ""
        if rc == 0:
            if found:
                rc2, out2, _ = run([GIT, "-C", worktree, "rev-list", "--count", f"{found.group(1)}..HEAD"])
            else:
                rc2, out2, _ = run([GIT, "-C", worktree, "rev-list", "--count", "HEAD", "--not", "--remotes"])
            state["unpushed"] = int(out2.strip()) if rc2 == 0 and out2.strip().isdigit() else None
    return state


def pr_events(pr: str, since: dt.datetime) -> tuple[list[tuple], dict, list[str]]:
    errors: list[str] = []
    info: dict = {}
    rows, err = gh_lines(f"repos/{REPO}/pulls/{pr}", "{head: .head.sha, branch: .head.ref}")
    if err:
        errors.append(err)
    elif rows:
        info = rows[0]
    commits, err = gh_lines(f"repos/{REPO}/pulls/{pr}/commits", "(.[] | {sha: .sha, at: .commit.committer.date, parents: (.parents | length)})")
    if err:
        errors.append(err)
    events = []
    for row in commits:
        when = parse_ts(row.get("at"))
        # Merge commits (PM updating the branch from main) are not the lane's progress.
        if when and when >= since and int(row.get("parents") or 0) <= 1:
            events.append((when, "commit", f"{info.get('branch', '')}@{str(row.get('sha'))[:8]}"))
    return events, info, errors


def comment_events(number: str, since: dt.datetime, name: str, role: str) -> tuple[list[tuple], list[str]]:
    rows, err = gh_lines(f"repos/{REPO}/issues/{number}/comments", "(.[] | {at: .created_at, url: .html_url, body: .body})")
    events = []
    prefix = re.compile(r"^\W*" + re.escape(role) + r"\b", re.I) if role else None
    for row in rows:
        when = parse_ts(row.get("at"))
        body = str(row.get("body") or "")
        if when and when >= since and (name in body or (prefix and prefix.match(body))):
            events.append((when, "comment", str(row.get("url") or "")))
    return events, ([err] if err else [])


def receipt_events(name: str, since: dt.datetime) -> tuple[list[tuple], list[str]]:
    events, errors = [], []
    folder = RUNS / name / "receipts"
    try:
        files = sorted(folder.glob("*.json"))
    except OSError:
        files = []
    for path in files:
        try:
            ref = str(json.loads(path.read_text()).get("ref") or "")
        except (OSError, ValueError, AttributeError):
            continue
        found = COMMENT_URL.match(ref)
        if not found:
            continue  # only a GitHub comment URL can be confirmed; anything else is a self-claim
        rows, err = gh_lines(f"repos/{found.group(1)}/issues/comments/{found.group(2)}", "{at: .created_at, url: .html_url}")
        if err:
            errors.append(err)
        elif rows and (parse_ts(rows[0].get("at")) or since) >= since:
            events.append((parse_ts(rows[0].get("at")), "receipt", ref))
    return events, errors


def branch_pr(branch: str) -> str:
    rows, err = gh_lines(f"repos/{REPO}/pulls?head={REPO.split('/')[0]}:{branch}&state=open", "(.[] | {n: .number})")
    return str(rows[0]["n"]) if not err and rows else ""


def branch_events(state: dict, since: dt.datetime) -> list[tuple]:
    """Pushed-commit event for a lane with no PR: the remote branch tip, if its committer date is after the lane started."""
    sha = state.get("remote_head")
    if not sha:
        return []
    rc, out, _ = run([GIT, "-C", state["worktree"], "show", "-s", "--format=%cI", sha])
    when = parse_ts(out.strip()) if rc == 0 else None
    return [(when, "commit", f"{state['branch']}@{sha[:8]}")] if when and when >= since else []


def check_lane(lane: dict, manifest: dict, now: dt.datetime, threshold_min: int) -> dict:
    name = lane["name"]
    since = parse_ts(manifest.get("lease_started_at") or manifest.get("started_at")) or now
    role = str(manifest.get("role") or manifest.get("type") or "")
    pr = str(manifest.get("pr") or "")
    issue = str(manifest.get("issue") or "")
    errors: list[str] = []
    events: list[tuple] = []
    info: dict = {}
    state = git_state(find_worktree(manifest))
    if not pr and state["branch"] and state["branch"] != "HEAD":
        pr = branch_pr(state["branch"])
    if pr:
        got, info, errs = pr_events(pr, since)
        events += got
        errors += errs
    elif state["remote_head"]:
        events += branch_events(state, since)
    for number in dict.fromkeys(n for n in (pr, issue) if n):
        got, errs = comment_events(number, since, name, role)
        events += got
        errors += errs
    got, errs = receipt_events(name, since)
    events += got
    errors += errs
    applicable = bool(pr or state["remote_head"] or issue)
    last = max(events, key=lambda e: e[0]) if events else None
    renewed = last[0] if last else since
    age = int((now - renewed).total_seconds() // 60)
    if age <= threshold_min:
        status = "OK"
    elif errors and not events:
        status = "UNAVAILABLE"
    else:
        status = "STALLED"
    kinds = {k: max((e[0] for e in events if e[1] == k), default=None) for k in ("commit", "comment", "receipt")}
    return {"lane": name, "status": status, "age_min": age, "threshold_min": threshold_min, "basis": last[1] if last else "launch",
            "ref": last[2] if last else "", "last_renewal": iso(renewed), "type": role, "issue": issue or None, "pr": pr or None,
            "model": lane.get("model", ""), "started_at": iso(since), "expected_minutes": manifest.get("expected_minutes"),
            "branch": info.get("branch") or state["branch"], "last_commit": iso(kinds["commit"]), "last_comment": iso(kinds["comment"]),
            "last_receipt": iso(kinds["receipt"]),
            "reason": ("; ".join(errors) if status == "UNAVAILABLE" else ("no_pr_no_branch_resolved" if not applicable and status == "STALLED" else "")),
            "packet": {"lane": name, "pr": pr or None, "issue": issue or None, "head": info.get("head") or state["head"] or "UNKNOWN",
                       "worktree": state["worktree"], "branch": state["branch"] or info.get("branch") or "UNKNOWN",
                       "dirty": state["dirty"], "unpushed": state["unpushed"], "remote_head": state["remote_head"] or "UNKNOWN"}}


def record(result: dict, manifest: dict) -> bool:
    """Write the checked lease fields onto the lane's pm-ops proxy_run row (evidence merge; only with --record)."""
    args = [sys.executable, PM_OPS, "obligation-upsert", "--kind", "proxy_run", "--target-type", "proxy", "--target-id", result["lane"]]
    for key in ("issue", "pr"):
        if str(manifest.get(key) or "").isdigit():
            args += ["--" + key, str(manifest[key])]
    evidence = {"lease_renewed_at": result["last_renewal"], "lease_basis": result["basis"], "lease_ref": result["ref"],
                "lease_state": result["status"], "lease_checked_at": iso(dt.datetime.now(dt.timezone.utc))}
    rc, _, _ = run(args + ["--evidence-json", json.dumps(evidence)])
    return rc == 0


def render(result: dict) -> str:
    p = result["packet"]
    lines = [f"PROXY_LEASE lane={result['lane']} status={result['status']} age_min={result['age_min']} threshold_min={result['threshold_min']} "
             f"basis={result['basis']} last_renewal={result['last_renewal']}"]
    if result["status"] == "OK":
        return lines[0]
    lines.append(f"  type={result['type'] or '-'} issue={result['issue'] or '-'} pr={result['pr'] or '-'} model={result['model']} "
                 f"started={result['started_at']} expected_min={result['expected_minutes'] or '-'}")
    lines.append(f"  evidence: branch={result['branch'] or 'UNKNOWN'} last_commit={result['last_commit']} last_comment={result['last_comment']} "
                 f"last_receipt={result['last_receipt']}")
    if result["reason"]:
        lines.append(f"  UNAVAILABLE reason={result['reason']}" if result["status"] == "UNAVAILABLE" else f"  note={result['reason']}")
    lines.append(f"  packet: lane={p['lane']} pr={p['pr'] or '-'} issue={p['issue'] or '-'} head={p['head']} worktree={p['worktree']} branch={p['branch']} "
                 f"dirty={'UNKNOWN' if p['dirty'] is None else p['dirty']} unpushed={'UNKNOWN' if p['unpushed'] is None else p['unpushed']} "
                 f"remote_head={p['remote_head']}")
    if result["status"] == "STALLED":
        lines.append("  action: push WIP from the worktree (non-force), then hand off one tier up (S131); this tool stops nothing")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--threshold-min", type=int, default=60)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--only", default="")
    parser.add_argument("--fail-on-stalled", action="store_true")
    parser.add_argument("--now", default="")
    args = parser.parse_args(argv)
    now = parse_ts(args.now) if args.now else dt.datetime.now(dt.timezone.utc)
    ps_file = os.environ.get("PROXY_LEASE_PS")
    if ps_file:
        ps_text = Path(ps_file).read_text()
    else:
        rc, ps_text, err = run(["/bin/ps", "-axww", "-o", "pid=,command="])
        if rc != 0:
            print(f"PROXY_LEASE_SUMMARY UNAVAILABLE reason=ps_failed:{err}")
            return 0
    results, manifests = [], {}
    for lane in live_lanes(ps_text):
        manifest = manifest_of(lane["name"])
        if manifest is None or (args.only and lane["name"] != args.only):
            continue  # not a lane this launcher registered
        manifests[lane["name"]] = manifest
        results.append(check_lane(lane, manifest, now, args.threshold_min))
    if args.record:
        for result in results:
            result["recorded"] = record(result, manifests[result["lane"]])
    counts = {s: sum(1 for r in results if r["status"] == s) for s in ("OK", "STALLED", "UNAVAILABLE")}
    if args.json:
        print(json.dumps({"lanes": results, "summary": counts, "threshold_min": args.threshold_min}, indent=1))
    else:
        for result in results:
            print(render(result))
        print(f"PROXY_LEASE_SUMMARY lanes={len(results)} ok={counts['OK']} stalled={counts['STALLED']} unavailable={counts['UNAVAILABLE']} "
              f"threshold_min={args.threshold_min}")
    return 3 if args.fail_on_stalled and counts["STALLED"] else 0


if __name__ == "__main__":
    sys.exit(main())
