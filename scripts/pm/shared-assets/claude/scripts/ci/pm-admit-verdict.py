#!/usr/bin/env python3
"""Read-only CI-verdict gate for pm-admit-ci.sh (PM brief 2026-11-27; Rajiv OK C0ALZJHGE49 thread 1795754821.658339, ts 1795761962.862319).

gate        Is this head's red already classified?  A head that was admitted and is red needs a ci-verdict PR comment in the
            ci-failure-investigation producer shape (<!-- ci-failure-investigation:run=<id> attempt=<n> head=<sha> -->, plus
            <!-- ci-verdict: {...} -->) bound to the exact head, the red run id and that run's CURRENT attempt, and posted by the
            trusted PM account (the `gh api user` login every PM agent posts as; unreadable -> UNAVAILABLE, never a guess).
            One reader (read_envelope) accepts both deployed envelopes: the marker as the first line, or the standard heading
            "## CI failure investigation verdict" first with the marker anywhere after it; the verdict names its head as `head` or
            `head_sha` (both present must agree); a verdict payload that carries `attempt` must equal the marker's attempt (a mismatch is not evidence).  There is no head-only fallback and an earlier attempt's verdict never governs a
            later attempt.
            Runs: per workflow (ci.yml, e2e.yml) the latest SUBSTANTIVE pull_request run for the head.  A label-wrapper run (a non-activation
            labeled event whose "Require exemption or exact-head real CI/E2E success" gate step ran) is gate-only collateral and is ignored,
            so a newer wrapper, green or red, never hides or replaces a substantive run.
              exit 0   proceed (never admitted, no red, or a red classified as PR-caused / anything outside the flaky class)
              exit 20  CLASSIFY FIRST: a red run has no ci-verdict comment bound to head + run + attempt (prints the REFUSE line)
              exit 21  every red run is classified flaky/infra/inherited-main/out-of-diff: prints the S171 admin-ask packet, admit nothing
              exit 11  UNAVAILABLE (a read failed, timed out or had an unexpected shape): never a guess
              exit 64  usage
            --scope head    (default) the head named by --head (the LIVE head)
            --scope latest  the LIVE head first (exactly like head); when it has no red, the newest earlier admitted head (of the last 5)
                            with a red is reported as informational diagnosis only (exit 0): the S171 packet binds to the live head only
descendant  Does an override recorded for --prev also cover --head?  Only when --head is a merge-only descendant of --prev:
            every commit on the first-parent chain --head..--prev is a merge, and the files the PR changes against main are the
            same set with byte-identical blobs/modes at both commits.  exit 0 yes, exit 1 no (reason printed).  Fails closed.

Read-only: only `gh api` GETs and `git` read commands.  Never posts, labels, merges or writes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

DEFAULT_REPO = "Scribie/heydonna-app"
SHA_RE = re.compile(r"[0-9a-f]{40}")
HEAD_LABEL = re.compile(r"ci-head:([0-9a-f]{40})")
TRACKED_WORKFLOWS = ("ci.yml", "e2e.yml")  # the two workflows an admission triggers
TRACKED_NAMES = {"ci": "ci.yml", "e2e": "e2e.yml"}
RED_CONCLUSIONS = {"failure", "timed_out", "startup_failure"}
LATEST_HEADS_SCANNED = 5
# Classes that make a red an admin-merge case (S171/S157), not a re-admission case.  Anything else (pr-caused, test, unknown) is not.
FLAKY_CLASSES = {"flaky", "flake", "infra", "infrastructure", "inherited-main", "inherited", "main-red", "out-of-diff", "outofdiff"}
FOLLOWUP_KEYS = ("follow_up", "followup_issue", "follow_up_issue", "flake_followup", "preexisting_followup")
TEST_KEYS = ("failing_tests", "failed_tests", "tests", "first_failing_test")
MARKER = "<!-- ci-verdict:"
# Producer shape (ci-failure-investigation skill): a marker line (run id, attempt, full head) opens the comment, or follows the standard heading
# (real producer, #14917 comment 7212581439: "## CI failure investigation verdict", blank line, marker, then the ci-verdict JSON keyed head_sha).
INVESTIGATION_RE = re.compile(r"<!--\s*ci-failure-investigation:\s*run=(\d+)\s+attempt=(\d+)\s+head=([0-9a-fA-F]{40})\s*-->")
HEADING_RE = re.compile(r"#{1,6}\s*CI failure investigation verdict\s*", re.IGNORECASE)
HEAD_KEYS = ("head", "head_sha")  # the two deployed spellings of the verdict's head
# Gate step that only runs in a label-wrapper (non-activation) run; the paid run skips it and runs "Confirm current paid ... success" instead.
WRAPPER_GATE_STEP = re.compile(r"Require exemption or exact-head real (?:CI|E2E) success")
WRAPPER_GATE_RAN = {"success", "failure"}


class Unavailable(Exception):
    pass


def gh_pages(path: str):
    """`gh api <path> --paginate --slurp` -> list of pages; one retry; Unavailable on any failure."""
    cmd = ["gh", "api", path, "--paginate", "--slurp"]
    reason = "unknown"
    for attempt in (1, 2):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if proc.returncode:
                raise Unavailable(f"gh_rc_{proc.returncode}")
            pages = json.loads(proc.stdout)
            if not isinstance(pages, list) or not pages:
                raise Unavailable("bad_response_shape")
            return pages
        except subprocess.TimeoutExpired:
            reason = "timeout"
        except OSError as exc:
            reason = f"gh_not_runnable:{type(exc).__name__}"
        except ValueError:
            reason = "bad_json"
        except Unavailable as exc:
            reason = str(exc)
        if attempt == 1:
            try:
                time.sleep(float(os.environ.get("PM_ADMIT_RETRY_SLEEP", "3")))
            except ValueError:
                time.sleep(3)
    raise Unavailable(f"{path.split('?')[0]}: {reason}")


def admitted_heads(repo: str, pr: int):
    """[(head, newest label event id)] newest first, from the `ci-head:<sha>` labeled events (same source as pm-admit-cap.py)."""
    newest = {}
    for page in gh_pages(f"repos/{repo}/issues/{pr}/events?per_page=100"):
        if not isinstance(page, list):
            raise Unavailable("events: bad_page")
        for ev in page:
            if not isinstance(ev, dict) or ev.get("event") != "labeled":
                continue
            label = ev.get("label")
            name = label.get("name") if isinstance(label, dict) else None
            m = HEAD_LABEL.fullmatch(name) if isinstance(name, str) else None
            if not m:
                continue
            eid = ev.get("id")
            if type(eid) is not int or eid <= 0:
                raise Unavailable("events: invalid_admission_event_id")
            newest[m.group(1)] = max(newest.get(m.group(1), 0), eid)
    return sorted(newest.items(), key=lambda kv: kv[1], reverse=True)


def workflow_key(run: dict):
    path = run.get("path")
    base = os.path.basename(path) if isinstance(path, str) else ""
    if base in TRACKED_WORKFLOWS:
        return base
    name = run.get("name")
    return TRACKED_NAMES.get(name.strip().lower()) if isinstance(name, str) else None


def is_wrapper(repo: str, run_id: int) -> bool:
    """True iff this run's latest attempt ran the label-wrapper gate step ("Require exemption or exact-head real CI/E2E success").

    That step only runs in a non-activation (labeled-event) wrapper run; a substantive paid run skips it.  Unreadable jobs -> Unavailable."""
    for page in gh_pages(f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"):
        jobs = page.get("jobs") if isinstance(page, dict) else None
        if not isinstance(jobs, list):
            raise Unavailable(f"jobs {run_id}: bad_page")
        for job in jobs:
            if not isinstance(job, dict):
                raise Unavailable(f"jobs {run_id}: bad_job")
            steps = job.get("steps")
            if steps is None:
                continue
            if not isinstance(steps, list):
                raise Unavailable(f"jobs {run_id}: bad_steps")
            for step in steps:
                if (isinstance(step, dict) and isinstance(step.get("name"), str) and WRAPPER_GATE_STEP.search(step["name"])
                        and step.get("conclusion") in WRAPPER_GATE_RAN):
                    return True
    return False


def substantive_runs(repo: str, head: str):
    """Latest SUBSTANTIVE `pull_request` run per tracked workflow (ci.yml, e2e.yml) on this head -> (selected, ignored_wrappers).

    Label-wrapper runs are gate-only collateral: whatever their conclusion or timestamp they never replace or hide a substantive run
    (45947144718 wrapper vs 45947144299 real failure on #14950).  selected = [{workflow, id, attempt, conclusion, url}]."""
    cands = {}
    for page in gh_pages(f"repos/{repo}/actions/runs?head_sha={head}&per_page=100"):
        runs = page.get("workflow_runs") if isinstance(page, dict) else None
        if not isinstance(runs, list):
            raise Unavailable("runs: bad_page")
        for run in runs:
            if not isinstance(run, dict):
                raise Unavailable("runs: bad_run")
            key = workflow_key(run)
            if key is None or run.get("head_sha") not in (None, head):
                continue
            event = run.get("event")
            if not isinstance(event, str) or not event:
                raise Unavailable("runs: bad_run_event")
            if event != "pull_request":
                continue
            rid, attempt = run.get("id"), run.get("run_attempt")
            if type(rid) is not int:
                raise Unavailable("runs: bad_run_id")
            if type(attempt) is not int or attempt <= 0:
                raise Unavailable("runs: bad_run_attempt")
            stamp = (str(run.get("run_started_at") or run.get("created_at") or ""), rid)
            cands.setdefault(key, []).append((stamp, run))
    selected, ignored = [], []
    for key in sorted(cands):
        for _, run in sorted(cands[key], key=lambda c: c[0], reverse=True):
            if is_wrapper(repo, run["id"]):
                ignored.append(f"{key}:{run['id']}")
                continue
            selected.append({"workflow": key, "id": run["id"], "attempt": run["run_attempt"], "conclusion": run.get("conclusion"),
                             "url": run.get("html_url") or ""})
            break
    return selected, ignored


def red_runs(selected):
    return [run for run in selected if run["conclusion"] in RED_CONCLUSIONS]


def run_ref(run) -> str:
    return f"{run['workflow']}:{run['id']}@{run['attempt']}"


def norm_class(value) -> str:
    return re.sub(r"[\s_]+", "-", str(value or "").strip().lower())


def read_envelope(body: str):
    """The ONE reader schema for a ci-verdict comment body -> (inv_match, verdict_dict) or None.

    Both deployed envelopes parse here: (a) the investigation marker is the first non-blank line, or (b) the first non-blank line is the standard
    heading "## CI failure investigation verdict" and the marker sits on a line of its own anywhere after it.  The comment must also carry a
    parseable <!-- ci-verdict: {...} --> JSON object (the last parseable one wins).  Anything else (no marker line, a marker or verdict quoted
    inside other text, no heading) is not a verdict envelope."""
    lines = body.splitlines()
    idx = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if idx is None:
        return None
    first = lines[idx].strip()
    if not INVESTIGATION_RE.fullmatch(first) and not HEADING_RE.fullmatch(first):
        return None
    inv = next((m for m in (INVESTIGATION_RE.fullmatch(ln.strip()) for ln in lines[idx:]) if m), None)
    if inv is None:
        return None
    verdict = None
    start = body.find(MARKER)
    while start >= 0:  # last parseable marker in the comment wins
        end = body.find("-->", start)
        if end < 0:
            break
        try:
            cand = json.loads(body[start + len(MARKER):end])
            if isinstance(cand, dict):
                verdict = cand
        except ValueError:
            pass
        start = body.find(MARKER, end)
    return (inv, verdict) if verdict is not None else None


def verdict_head(verdict: dict):
    """The verdict's head under either deployed key (`head` or `head_sha`), lower-cased; None when absent, not a full sha, or the keys disagree."""
    found = {verdict[k].lower() if isinstance(verdict[k], str) else None for k in HEAD_KEYS if verdict.get(k) is not None}
    if len(found) != 1:
        return None
    (only,) = found
    return only if only and SHA_RE.fullmatch(only) else None


def attempt_agrees(verdict: dict, envelope_attempt: int) -> bool:
    """True when the verdict payload names no attempt (the legacy shape carries none), or names exactly the envelope marker's attempt.

    A payload `attempt` that is present but another number, null, a bool, a float or not an integer spelling disagrees: the marker and the payload then
    name two different attempts and the comment cannot be said to bind to either (an attempt-1 payload inside an attempt-2 envelope is not evidence)."""
    if "attempt" not in verdict:
        return True
    raw = verdict["attempt"]
    if type(raw) is int:
        return raw == envelope_attempt
    if type(raw) is str and re.fullmatch(r"\d+", raw):
        return int(raw) == envelope_attempt
    return False


_TRUSTED = []


def trusted_login() -> str:
    """The account every PM agent posts as: the authenticated `gh` login (same source pm-admit-12b.py uses for its 12b verdicts).  Unreadable -> Unavailable."""
    if _TRUSTED:
        return _TRUSTED[0]
    reason = "unknown"
    for attempt in (1, 2):
        try:
            proc = subprocess.run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True, timeout=15)
            login = proc.stdout.strip() if proc.returncode == 0 else ""
            reason = "empty_login" if proc.returncode == 0 else f"gh_rc_{proc.returncode}"
            if login:
                _TRUSTED.append(login)
                return login
        except subprocess.TimeoutExpired:
            reason = "timeout"
        except OSError as exc:
            reason = f"gh_not_runnable:{type(exc).__name__}"
        if attempt == 1:
            try:
                time.sleep(float(os.environ.get("PM_ADMIT_RETRY_SLEEP", "3")))
            except ValueError:
                time.sleep(3)
    raise Unavailable(f"trusted_account_unreadable: {reason}")


def comment_author(c: dict) -> str:
    identity = c.get("user") or c.get("author") or {}
    return identity.get("login", "") if isinstance(identity, dict) else str(identity)


def verdicts_for_head(repo: str, pr: int, head: str):
    """-> (verdicts, untrusted): ci-verdict comments bound to exactly this head and posted by the trusted account, newest first
    ([{cls, run_id, attempt, raw, url, key}]), plus the number of otherwise valid ones ignored for another author.

    Evidence needs ALL of: a read_envelope() envelope whose marker head is this head; a verdict whose run_id equals the marker's run,
    whose head (`head` or `head_sha`) is this full head, and whose `attempt`, when it carries one, equals the marker's attempt; and the trusted posting account as comment author.  Anything else (no envelope,
    a missing/short/other head, a disagreeing run id, another author) is not evidence.  The trusted login is only read when a candidate exists."""
    out, untrusted = [], 0
    for page in gh_pages(f"repos/{repo}/issues/{pr}/comments?per_page=100"):
        if not isinstance(page, list):
            raise Unavailable("comments: bad_page")
        for c in page:
            body = c.get("body") if isinstance(c, dict) else None
            if not isinstance(body, str) or MARKER not in body:
                continue
            env = read_envelope(body)
            if env is None:
                continue
            inv, verdict = env
            if inv.group(3).lower() != head:
                continue
            v_run = verdict.get("run_id")
            if type(v_run) not in (int, str) or not re.fullmatch(r"\d+", str(v_run)) or int(str(v_run)) != int(inv.group(1)):
                continue  # verdict names no run, or another run than the marker
            if verdict_head(verdict) != head:
                continue  # verdict names no head, a short head, another head, or two disagreeing spellings
            if not attempt_agrees(verdict, int(inv.group(2))):
                continue  # the payload's own `attempt` differs from the marker's: the envelope and the verdict name two attempts
            if comment_author(c) != trusted_login():
                untrusted += 1
                continue
            out.append({"cls": norm_class(verdict.get("classification")), "run_id": int(inv.group(1)), "attempt": int(inv.group(2)),
                        "raw": verdict, "url": c.get("html_url") or "",
                        "key": (str(c.get("created_at") or ""), c.get("id") if type(c.get("id")) is int else 0)})
    out.sort(key=lambda v: v["key"], reverse=True)
    return out, untrusted


def covering(verdicts, run):
    """Newest verdict bound to this run's id AND current attempt, else None (no head-only fallback, no earlier-attempt carry)."""
    for v in verdicts:
        if v["run_id"] == run["id"] and v["attempt"] == run["attempt"]:
            return v
    return None


def first(raw: dict, keys):
    for k in keys:
        v = raw.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def packet(pr: int, head: str, current: str, pairs) -> list[str]:
    lines = [f"ADMIN_ASK_PACKET (S171/S157) #{pr} head={head}" + ("" if current == head else f" current_pr_head={current}")]
    tests, followups, seen = [], [], set()
    for run, v in pairs:
        raw = v["raw"]
        lines.append(f"  run {run_ref(run)} conclusion={run['conclusion']} class={v['cls']} {run['url']}".rstrip())
        if raw.get("evidence"):
            lines.append(f"    evidence: {str(raw['evidence'])[:300]}")
        lines.append(f"    verdict comment: {v['url'] or 'UNAVAILABLE (no url)'}")
        found = first(raw, TEST_KEYS)
        for t in (found if isinstance(found, list) else [found]):
            if t and str(t) not in seen:
                seen.add(str(t)); tests.append(str(t))
        fu = first(raw, FOLLOWUP_KEYS)
        if fu and str(fu) not in followups:
            followups.append(str(fu))
    lines.append("  failing_tests: " + ("; ".join(tests[:10]) + (f" (+{len(tests) - 10} more)" if len(tests) > 10 else "") if tests
                                       else "UNAVAILABLE (not in the verdict; read the verdict comment above)"))
    lines.append("  follow_up_issue: " + (", ".join(followups) if followups else "UNAVAILABLE (none in the verdict; file one open issue per flaky/inherited test before asking)"))
    lines.append("  next: NOT re-admitting, rerunning or skipping. Send the CTO the head-pinned admin-merge ask for this head only if E2E executed green "
                 "at it (shard logs, S157); procedure S167: enforce_admins off -> gh pr merge --squash --admin --match-head-commit <head> -> on -> readback.")
    return lines


def evaluate(repo: str, pr: int, head: str, scope: str):
    """-> (rc, lines)."""
    admitted = admitted_heads(repo, pr)
    live_admitted = any(h == head for h, _ in admitted)
    selected, ignored = substantive_runs(repo, head) if live_admitted else ([], [])
    reds = red_runs(selected)
    seen = ""
    if live_admitted:
        picked = ",".join(run_ref(r) + ":" + str(r["conclusion"] or "none") for r in selected) or "none"
        seen = f"selected={picked} ignored_wrapper={','.join(ignored) or 'none'}"
    if not reds:
        lines = [f"ci_verdict: proceed pr={pr} head={head} scope={scope} (live head has no red substantive run)" + (f" {seen}" if seen else "")]
        if scope == "latest":  # earlier admitted heads inform diagnosis only; the S171 packet binds to the live head
            for cand, _ in admitted[:LATEST_HEADS_SCANNED]:
                if cand == head:
                    continue
                old_sel, _ = substantive_runs(repo, cand)
                old_reds = red_runs(old_sel)
                if old_reds:
                    lines.append(f"ci_verdict: info historical head={cand} red run(s) {','.join(run_ref(r) for r in old_reds)} "
                                 f"(diagnosis only; nothing binds to a head other than the live head {head})")
                    break
        return 0, lines
    verdicts, untrusted = verdicts_for_head(repo, pr, head)
    pairs = [(run, covering(verdicts, run)) for run in reds]
    missing = [run for run, v in pairs if v is None]
    if missing:
        ids = ",".join(run_ref(r) for r in missing)
        other = f"; {untrusted} otherwise valid verdict comment(s) ignored: not posted by the trusted PM account" if untrusted else ""
        return 20, [f"REFUSE: ci_verdict_required #{pr} head={head} red run(s) {ids} have no ci-verdict comment bound to this head + run id + attempt "
                    f"posted by the trusted PM account in the ci-failure-investigation producer shape; classify first "
                    f"(<!-- ci-failure-investigation:run=<id> attempt=<n> head=<sha> --> plus <!-- ci-verdict: {{...}} -->), "
                    f"then re-run admission; nothing admitted ({seen}){other}"]
    nonflaky = [(run, v) for run, v in pairs if v["cls"] not in FLAKY_CLASSES]
    if nonflaky:
        run, v = nonflaky[0]
        return 0, [f"ci_verdict: proceed pr={pr} head={head} run={run_ref(run)} class={v['cls'] or 'unknown'} (not flaky/infra/inherited/out-of-diff) {seen}"]
    return 21, packet(pr, head, head, pairs) + [f"  selection: {seen}"]


def git(repo_dir: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", repo_dir, *args], capture_output=True, text=True, timeout=30)
    if proc.returncode:
        raise Unavailable(f"git {args[0]} rc={proc.returncode}")
    return proc.stdout


def pr_files(repo_dir: str, main_ref: str, commit: str):
    base = git(repo_dir, "merge-base", main_ref, commit).strip()
    return {f for f in git(repo_dir, "diff", "--no-renames", "--name-only", "-z", base, commit, "--").split("\0") if f}


def blobs(repo_dir: str, commit: str, files):
    found = {}
    files = sorted(files)
    for i in range(0, len(files), 200):
        for ent in git(repo_dir, "ls-tree", "-r", "-z", commit, "--", *files[i:i + 200]).split("\0"):
            if ent:
                meta, _, path = ent.partition("\t")
                found[path] = meta
    return found  # absent paths are simply missing


def descendant(repo_dir: str, main_ref: str, head: str, prev: str):
    """-> (ok, reason).  --head must be reachable from --prev through merge commits only, with an identical PR file set and blobs."""
    if head == prev:
        return False, "same head (nothing inherited)"
    chain = git(repo_dir, "rev-list", "--first-parent", "--parents", "-n", "11", head).splitlines()
    for line in chain:
        parts = line.split()
        if parts[0] == prev:
            break
        if len(parts) < 3:
            return False, f"commit {parts[0][:12]} between the heads is not a merge commit"
    else:
        return False, "the earlier head is not within 10 first-parent merge hops of the current head"
    files_prev, files_head = pr_files(repo_dir, main_ref, prev), pr_files(repo_dir, main_ref, head)
    if files_prev != files_head:
        return False, f"the PR changes a different file set ({len(files_prev)} vs {len(files_head)} files)"
    if blobs(repo_dir, prev, files_prev) != blobs(repo_dir, head, files_head):
        return False, "a PR file differs between the two heads"
    return True, f"merge-only descendant: {len(files_head)} PR file(s) byte-identical"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gate")
    g.add_argument("--repo", default=DEFAULT_REPO)
    g.add_argument("--pr", type=int, required=True)
    g.add_argument("--head", required=True)
    g.add_argument("--scope", choices=("head", "latest"), default="head")
    d = sub.add_parser("descendant")
    d.add_argument("--repo-dir", required=True)
    d.add_argument("--main-ref", default="origin/main")
    d.add_argument("--head", required=True)
    d.add_argument("--prev", required=True)
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        return 64
    if not SHA_RE.fullmatch(args.head) or (args.cmd == "descendant" and not SHA_RE.fullmatch(args.prev)) or (args.cmd == "gate" and args.pr <= 0):
        print("ci_verdict: usage error (full 40-hex sha and a positive PR number required)")
        return 64
    try:
        if args.cmd == "gate":
            rc, lines = evaluate(args.repo, args.pr, args.head, args.scope)
        else:
            ok, reason = descendant(args.repo_dir, args.main_ref, args.head, args.prev)
            rc, lines = (0 if ok else 1), [f"override_inherit: {'yes' if ok else 'no'} {reason}"]
    except (Unavailable, OSError, subprocess.TimeoutExpired) as exc:
        print(f"ci_verdict: UNAVAILABLE reason={exc}")
        return 11
    print("\n".join(lines))
    return rc


if __name__ == "__main__":
    sys.exit(main())
