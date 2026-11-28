#!/usr/bin/env python3
"""ONE paid CI/E2E round per PR (paid-round gate for pm-admit-ci.sh, the ONLY path that dispatches CI/E2E).

Directive (Rajiv C0ALZJHGE49 thread 1795841629.732639, ts 1795845160.422969 + ts 1795845739.605399):
"yes, admin merge after testing with remote box. the qa videos should be posted to slack for review".
Each PR gets ONE paid CI/E2E round.  Every later head is proved on remote boxes scoped to the failing tests and then
merged by a head-pinned S167 admin merge; no further paid round is dispatched.
CI admission is PM-owned (Rajiv ts 1795848120.047269: "cto is not supposed to admit ci at all, only do merges"): admit.py refuses to dispatch.

ACTIVATION = an exact `ci-head:<sha>` labeled event (the same source pm-admit-cap.py counts).  Its workflow runs are every ci.yml / e2e.yml
`pull_request` run on that head that is NOT a label-wrapper (a wrapper is a gate-only run whose "Require exemption or exact-head real CI/E2E
success" step ran; a non-wrapper run is the activation run by construction).  ALL such runs are read (not just the latest per workflow), so a later
no-op rerun can never hide an earlier executed attempt.  Per run, from its jobs (all attempts):
  executed    a LANE job started and did not end skipped (lane = every job except the gate jobs classify-change-scope, literal-test-reads, test)
  queued      the run or a job is still queued / in progress and no lane job has started: an activation in flight
  idle        completed and no lane job executed (gate-only / classifier-skipped): NOT a paid round
  unresolved  no job rows to judge by
No elapsed-time proxy is used: a lane job that fails in two seconds still executed.  A jobs read that fails is UNAVAILABLE (rc 11), never "free".
Per ADMITTED head: executed > queued > unresolved > idle; an admitted head with no visible activation run is unresolved.
Executed, queued and unresolved heads all BLOCK a new paid write: an unresolved prior activation is never treated as free.

gate --repo R --pr N --head SHA [--repo-dir DIR] [--dry-run]
  rc 0   dispatch: no earlier activation blocks, OR this head's own activation is still in flight (the caller keeps ALREADY_ADMITTED), OR a logged override
  rc 21  BOX_ROUND_REQUIRED / PAID_ROUND_UNRESOLVED: nothing is dispatched; prints the box test list
         (an earlier head's paid/queued/unresolved activation, or this head's own COMPLETED activation: re-arming it would be a second paid pair)
  rc 11  UNAVAILABLE (GitHub/git unreadable): the caller refuses, never assumes round 0
  rc 64  usage
The last line is `paid_round_same_head=<none|idle|in_flight|completed_paid|unresolved>` for the caller's re-arm block.

Override (logged), the ONLY way past a block: PM_ADMIT_PAID_ROUND_OVERRIDE=cto:<ts>@<pr>:<sha>  or  rajiv:<ts>@<pr>:<sha>  (exact pr and head).
A PR-authored CI-input change (`.github/workflows/**`, non-test `scripts/ci/**`) is NOT an automatic exception: a second paid pair for it needs that pinned
authority.  Rule 62's CI-input merge of main (a remote-box step) is decided by the admission integration check, not here.

E2E coverage in the box packet comes from the app repo's change_scope --select-e2e-specs run in a VERIFIED detached checkout of the exact head.
Only mode=none reason=no_area is a verified "none"; a failure, mode=failed, any other none reason, or an unverified checkout leaves coverage UNRESOLVED
(SELECT_ON_BOX), never "none selected".  For selected specs the receipt lists each spec's MP4 for Slack review.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _load_verdict():
    spec = importlib.util.spec_from_file_location("pm_admit_verdict", HERE / "pm-admit-verdict.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pav = _load_verdict()
Unavailable = pav.Unavailable
DEFAULT_REPO = pav.DEFAULT_REPO
SHA_RE = pav.SHA_RE
EXIT_BOX, EXIT_UNAVAILABLE, EXIT_USAGE = 21, 11, 64
OVERRIDE_ENV = "PM_ADMIT_PAID_ROUND_OVERRIDE"
OVERRIDE_RE = re.compile(r"(cto|rajiv):(\d+\.\d+)@(\d+):([0-9a-f]{40})")
LOG = Path(os.environ.get("PM_ADMIT_PAID_ROUND_LOG") or Path.home() / ".claude/logs/pm-admit-skip.log")
MAX_LIST = 25
RED_JOB = {"failure", "timed_out", "startup_failure"}
CI_INPUT_PREFIXES = (".github/workflows/",)
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|test_[^/]*|[^/]*[._-]tests?\.[^/]+|[^/]*\.(test|spec)\.[^/]+)($|/)")
# Gate / bookkeeping jobs of ci.yml and e2e.yml.  Every OTHER job is a lane job, so an unknown or renamed job counts as lane work (blocks, never leaks a paid round).
GATE_JOB = re.compile(r"classify-change-scope|literal-test-reads|test")
LIVE_STATUS = {"queued", "in_progress", "waiting", "pending", "requested"}
BLOCKING = ("executed", "queued", "unresolved")
VERIFIED_NONE_REASON = "no_area"


def log(line: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as fh:
            fh.write(time.strftime("%Y-%m-%dT%H:%M:%S%z") + " " + line + "\n")
    except OSError:
        pass


def runs_of(repo: str, head: str):
    """Every ci.yml / e2e.yml `pull_request` run on this head (all of them, newest first)."""
    runs = []
    for page in pav.gh_pages(f"repos/{repo}/actions/runs?head_sha={head}&per_page=100"):
        got = page.get("workflow_runs") if isinstance(page, dict) else None
        if not isinstance(got, list):
            raise Unavailable("runs: bad_page")
        for run in got:
            if not isinstance(run, dict):
                raise Unavailable("runs: bad_run")
            if pav.workflow_key(run) is None or run.get("head_sha") not in (None, head):
                continue
            event = run.get("event")
            if not isinstance(event, str) or not event:
                raise Unavailable("runs: bad_run_event")
            if event != "pull_request":
                continue
            if type(run.get("id")) is not int:
                raise Unavailable("runs: bad_run_id")
            if not isinstance(run.get("status"), str):
                raise Unavailable("runs: bad_run_status")
            runs.append(run)
    return sorted(runs, key=lambda r: (str(r.get("run_started_at") or r.get("created_at") or ""), r["id"]), reverse=True)


def jobs_of(repo: str, run_id: int):
    """Jobs of EVERY attempt of the run (steps included)."""
    jobs = []
    for page in pav.gh_pages(f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100&filter=all"):
        got = page.get("jobs") if isinstance(page, dict) else None
        if not isinstance(got, list):
            raise Unavailable(f"jobs {run_id}: bad_page")
        for j in got:
            if not isinstance(j, dict):
                raise Unavailable(f"jobs {run_id}: bad_job")
            jobs.append(j)
    return jobs


def is_wrapper_jobs(jobs) -> bool:
    """The label-wrapper gate step ran in this run's jobs (same definition as pm-admit-verdict.is_wrapper)."""
    for job in jobs:
        steps = job.get("steps")
        if steps is None:
            continue
        if not isinstance(steps, list):
            raise Unavailable("jobs: bad_steps")
        for step in steps:
            if (isinstance(step, dict) and isinstance(step.get("name"), str) and pav.WRAPPER_GATE_STEP.search(step["name"])
                    and step.get("conclusion") in pav.WRAPPER_GATE_RAN):
                return True
    return False


def lane_jobs(jobs):
    return [j for j in jobs if not GATE_JOB.fullmatch(str(j.get("name") or "").strip())]


def job_ran(job) -> bool:
    """A lane job that started and did not end skipped (a cancelled job that never started did no work)."""
    if job.get("status") == "in_progress":
        return True
    conclusion = str(job.get("conclusion") or "")
    if conclusion in ("", "skipped"):
        return False
    return bool(job.get("started_at")) or conclusion != "cancelled"


def classify_run(run, jobs) -> dict:
    live = str(run.get("status")) in LIVE_STATUS or any(str(j.get("status") or "completed") in LIVE_STATUS for j in jobs)
    if any(job_ran(j) for j in lane_jobs(jobs)):
        state = "executed"
    elif live:
        state = "queued"
    elif not jobs:
        state = "unresolved"
    else:
        state = "idle"
    return {"run": run, "jobs": jobs, "state": state, "live": live}


def head_state(repo: str, head: str) -> dict:
    """State of one ADMITTED head from all of its non-wrapper runs -> {kind, live, runs:[(run, jobs)], records}."""
    records = []
    for run in runs_of(repo, head):
        jobs = jobs_of(repo, run["id"])
        if is_wrapper_jobs(jobs):
            continue
        records.append(classify_run(run, jobs))
    states = {r["state"] for r in records}
    if not records:
        kind = "unresolved"  # admitted, but no activation run is visible: a pending activation, never free
    else:
        kind = next((k for k in ("executed", "queued", "unresolved") if k in states), "idle")
    live = any(r["live"] for r in records)
    executed = [(r["run"], r["jobs"]) for r in records if r["state"] == "executed"]
    return {"head": head, "kind": kind, "live": live, "runs": executed, "records": records}


def paid_state(repo: str, pr: int, head: str):
    """-> (blockers, same) for the admitted heads of this PR.

    blockers: earlier admitted heads (newest first) whose activation executed, is queued, or is unresolved.  same: this head's own state or None
    when this head was never admitted."""
    admitted = [h for h, _ in pav.admitted_heads(repo, pr)]
    blockers, same = [], None
    for h in admitted:
        st = head_state(repo, h)
        if h == head:
            same = st
        elif st["kind"] in BLOCKING:
            blockers.append(st)
    return blockers, same


def same_head_label(same) -> str:
    if same is None:
        return "none"
    if same["kind"] in ("executed", "queued") and same["live"]:
        return "in_flight"
    if same["kind"] == "executed":
        return "completed_paid"
    if same["kind"] == "unresolved":
        return "unresolved"
    return "idle"


def parse_override(value: str, pr: int, head: str):
    m = OVERRIDE_RE.fullmatch((value or "").strip())
    if m and int(m.group(3)) == pr and m.group(4) == head:
        return f"{m.group(1)}:{m.group(2)}@{pr}:{head}"
    return None


def is_ci_input(path: str) -> bool:
    if path.startswith(CI_INPUT_PREFIXES):
        return True
    return path.startswith("scripts/ci/") and not TEST_PATH.search(path)


def ci_input_delta(repo_dir: str, prev: str, head: str, main_ref: str = "origin/main"):
    """PR-authored files changed prev..head that are CI inputs (main-merge changes excluded by intersecting with the PR's own files)."""
    delta = {f for f in pav.git(repo_dir, "diff", "--no-renames", "--name-only", "-z", prev, head, "--").split("\0") if f}
    mine = pav.pr_files(repo_dir, main_ref, head)
    return sorted(f for f in delta & mine if is_ci_input(f))


def merged_main(repo_dir: str, prev: str, head: str) -> bool:
    chain = pav.git(repo_dir, "rev-list", "--first-parent", "--parents", f"{prev}..{head}").splitlines()
    return any(len(line.split()) >= 3 for line in chain)


def nearest_tests(repo_dir: str, head: str, files):
    """Nearest-owner tests for the overlapping files: tracked test files sharing the file's stem (rule 62 overlap tests)."""
    tracked = [p for p in pav.git(repo_dir, "ls-tree", "-r", "--name-only", head).splitlines() if p]
    tests = [p for p in tracked if TEST_PATH.search(p)]
    found = []
    for f in sorted(files):
        if TEST_PATH.search(f):
            if f not in found:
                found.append(f)
            continue
        stem = re.sub(r"\.[^./]+$", "", os.path.basename(f))
        if len(stem) < 3:
            continue
        for t in tests:
            base = os.path.basename(t)
            if (stem in base) and t not in found:
                found.append(t)
    return found


def overlap_files(repo_dir: str, prev: str, head: str, main_ref: str = "origin/main"):
    """Files the PR changes (at `head`) that main changed since the PAID head's merge-base (what rule 62 overlap meant for the earlier head)."""
    base = pav.git(repo_dir, "merge-base", main_ref, prev).strip()
    main_changed = {f for f in pav.git(repo_dir, "diff", "--no-renames", "--name-only", "-z", base, main_ref, "--").split("\0") if f}
    return sorted(pav.pr_files(repo_dir, main_ref, head) & main_changed)


def failing_tests(repo: str, pr: int, paid):
    """Failing test ids from the round-1 ci-verdict comments; failed job names as the fallback."""
    tests, jobs_failed = [], []
    for item in paid:
        for _run, jobs in item["runs"]:
            for j in jobs:
                if j.get("conclusion") in RED_JOB and j.get("name") and j["name"] not in jobs_failed:
                    jobs_failed.append(j["name"])
        try:
            verdicts, _ = pav.verdicts_for_head(repo, pr, item["head"])
        except Unavailable:
            verdicts = []
        for v in verdicts:
            found = pav.first(v["raw"], pav.TEST_KEYS)
            for t in (found if isinstance(found, list) else [found]):
                if t and str(t) not in tests:
                    tests.append(str(t))
    return tests, jobs_failed


def _selection(status: str, mode=None, reason=None, specs=None, detail: str = "") -> dict:
    return {"status": status, "mode": mode, "reason": reason, "specs": list(specs or []), "detail": detail}


def select_e2e_specs(repo_dir: str | None, prev: str, head: str, pr: int | None = None) -> dict:
    """Run the app repo's change_scope --select-e2e-specs in a VERIFIED detached checkout of exactly `head`.

    -> {status: selected | verified_none | unresolved, mode, reason, specs, detail}.  Only mode=none reason=no_area is a verified none.
    Any failure (checkout not verified, selector error, mode=failed, any other mode/reason) is `unresolved`, never "none selected"."""
    if not repo_dir:
        return _selection("unresolved", reason="no_repo_dir", detail="no --repo-dir")
    try:
        with tempfile.TemporaryDirectory(prefix="paid-round-") as tmp:
            co = str(Path(tmp, "checkout"))
            for cmd in (["git", "clone", "-q", "--no-checkout", "--shared", str(repo_dir), co], ["git", "-C", co, "checkout", "-q", "--detach", head]):
                step = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
                if step.returncode:
                    return _selection("unresolved", reason="checkout_failed", detail=(step.stderr or "").strip()[:200])
            got = subprocess.run(["git", "-C", co, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30).stdout.strip()
            dirty = subprocess.run(["git", "-C", co, "status", "--porcelain"], capture_output=True, text=True, timeout=60).stdout.strip()
            if got != head or dirty or not Path(co, "scripts/ci/change_scope.py").exists():
                return _selection("unresolved", reason="checkout_not_verified", detail=f"head={got[:12]} dirty={bool(dirty)}")
            cmd = [sys.executable, "-I", "scripts/ci/change_scope.py", "--select-e2e-specs", "--event", "push", "--base", prev, "--head", head,
                   "--unit-execution-sha", head, "--unit-workflow-sha", head, "--unit-expected-workflow-sha", head, "--repo-root", co]
            if pr:
                cmd += ["--pr", str(pr)]
            proc = subprocess.run(cmd, cwd=co, capture_output=True, text=True, timeout=180)
            if proc.returncode:
                return _selection("unresolved", reason="selector_failed", detail=f"rc={proc.returncode} {(proc.stderr or '').strip()[:200]}")
            data = json.loads(proc.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return _selection("unresolved", reason="selector_error", detail=f"{type(exc).__name__}: {exc}"[:200])
    mode, reason, specs = data.get("mode"), data.get("reason"), data.get("specs")
    if mode == "selected" and isinstance(specs, list) and specs:
        return _selection("selected", mode, reason, [str(s) for s in specs])
    if mode == "none" and reason == VERIFIED_NONE_REASON:
        return _selection("verified_none", mode, reason)
    return _selection("unresolved", mode, reason, [str(s) for s in specs] if isinstance(specs, list) else [], str(data.get("detail") or ""))


def e2e_lines(sel: dict, prev: str, head: str):
    cmd = f"scripts/ci/change_scope.py --select-e2e-specs --event push --base {prev} --head {head} (run at the head checkout on the box)"
    status = sel.get("status")
    if status == "selected":
        out = [f"    specs ({len(sel['specs'])}): " + "; ".join(sel["specs"][:MAX_LIST]) + (f" (+{len(sel['specs']) - MAX_LIST} more)" if len(sel["specs"]) > MAX_LIST else ""),
               "    videos for Slack (one MP4 per spec; the receipt links or lists each path):"]
        out += [f"      {s} -> <mp4 path or artifact link>" for s in sel["specs"][:MAX_LIST]]
        return out
    if status == "verified_none":
        return [f"    specs: none (verified: mode={sel.get('mode')} reason={sel.get('reason')})"]
    return [f"    specs: UNRESOLVED (mode={sel.get('mode')} reason={sel.get('reason')}"
            + (f" detail={sel['detail']}" if sel.get("detail") else "") + "): coverage is NOT 'none selected'; "
            + "SELECT_ON_BOX: " + cmd]


def box_packet(pr: int, head: str, blockers, same, repo: str, repo_dir: str | None, selector=select_e2e_specs):
    paid = [b for b in blockers if b["kind"] == "executed"] + [b for b in blockers if b["kind"] != "executed"]
    if same is not None and same["kind"] != "idle" and same_head_label(same) in ("completed_paid", "unresolved"):
        paid.append(same)
    prev = paid[0]["head"] if paid else head
    if prev == head and repo_dir:
        try:
            prev = pav.git(repo_dir, "merge-base", "origin/main", head).strip() or head
        except (Unavailable, OSError, subprocess.SubprocessError):
            prev = "origin/main"
    tests, jobs_failed = failing_tests(repo, pr, paid)
    overlap_tests, merged = [], False
    ci_input = None
    if repo_dir and paid and paid[0]["head"] != head:
        try:
            if merged_main(repo_dir, paid[0]["head"], head):
                merged = True
                overlap_tests = nearest_tests(repo_dir, head, overlap_files(repo_dir, paid[0]["head"], head))
        except (Unavailable, OSError, subprocess.SubprocessError):
            overlap_tests = []
        try:
            ci_input = ci_input_delta(repo_dir, paid[0]["head"], head)
        except (Unavailable, OSError, subprocess.SubprocessError):
            ci_input = None
    sel = selector(repo_dir, prev, head) if repo_dir else _selection("unresolved", reason="no_repo_dir", detail="no --repo-dir")
    if isinstance(sel, list):  # defensive: a bare spec list from an older selector is never a verified none
        sel = _selection("selected", "selected", None, sel) if sel else _selection("unresolved", reason="legacy_selector_empty")
    unresolved_only = all(b["kind"] == "unresolved" for b in paid) if paid else True
    header = "PAID_ROUND_UNRESOLVED" if unresolved_only else "BOX_ROUND_REQUIRED"
    why = ("an earlier activation for this PR is unresolved (queued, or no readable run/jobs), so a new paid write is blocked until it resolves"
           if unresolved_only else
           "ONE paid CI/E2E round per PR already ran or is in flight; this head is proved on remote boxes, then a head-pinned S167 admin merge")
    lines = [f"{header} pr={pr} head={head}",
             f"  why: {why}. Nothing was dispatched, labelled or written.",
             "  activations: " + ", ".join(
                 f"{i['head'][:12]}[{i['kind']}" + ("".join("," + pav.run_ref({'workflow': pav.workflow_key(r) or '?', 'id': r['id'], 'attempt': r.get('run_attempt', 1)}) + ":" + str(r.get("conclusion") or r.get("status") or "none")
                                                         for r, _ in i["runs"])) + "]" for i in paid),
             "  box tests (remote-test-runner, scoped, red-green not required):",
             "    failing_tests: " + ("; ".join(tests[:MAX_LIST]) if tests else "UNAVAILABLE (not in a ci-verdict comment; read the round-1 run logs)")
             + (f" (+{len(tests) - MAX_LIST} more)" if len(tests) > MAX_LIST else ""),
             "    failed_jobs: " + ("; ".join(jobs_failed[:MAX_LIST]) if jobs_failed else "none recorded"),
             "    rule62_overlap_nearest_owner_tests: " + (("; ".join(overlap_tests[:MAX_LIST]) if overlap_tests else "none found") if merged else "n/a (main not merged since the paid head)"),
             "    typecheck: app tsc AND tests tsc on the box at this head (both required)"]
    if ci_input:
        lines.append(f"    ci_input_delta: {','.join(ci_input[:5])}{'...' if len(ci_input) > 5 else ''} (PR-authored CI input changed; a second paid pair for it needs pinned authority, no automatic exception)")
    elif ci_input is None and repo_dir and paid and paid[0]["head"] != head:
        lines.append("    ci_input_delta: UNAVAILABLE (git read failed)")
    lines.append("  e2e (remote-qa-runner, named Playwright specs, video ON; the receipt MUST link the MP4s for Slack review):")
    lines += e2e_lines(sel, prev, head)
    lines.append("  next: post the MP4s to Slack, then the CTO head-pinned admin-merge ask (S167). To dispatch a paid round anyway set "
                 f"{OVERRIDE_ENV}=cto:<ts>@{pr}:{head} (or rajiv:<ts>@{pr}:{head}); it is logged.")
    return lines


def evaluate(repo: str, pr: int, head: str, repo_dir: str | None, env=None, dry: bool = False, selector=select_e2e_specs):
    env = os.environ if env is None else env
    tag = "[dry-run] " if dry else ""
    blockers, same = paid_state(repo, pr, head)
    label = same_head_label(same)
    tail = f"paid_round_same_head={label}"
    if label == "in_flight":
        return 0, [f"{tag}paid_round: same head {head} activation in flight; no new paid write (caller keeps ALREADY_ADMITTED)", tail]
    if not blockers and label not in ("completed_paid", "unresolved"):
        return 0, [f"{tag}paid_round: dispatch pr={pr} head={head} (no earlier paid, queued or unresolved activation)", tail]
    blocked_by = ",".join(f"{b['head'][:12]}:{b['kind']}" for b in blockers) or f"{head[:12]}:{label}"
    ov = parse_override(env.get(OVERRIDE_ENV, ""), pr, head)
    if ov:
        if not dry:
            log(f"paid_round override pr={pr} head={head} override={ov} blocked_by={blocked_by}")
        return 0, [f"{tag}paid_round: dispatch pr={pr} head={head} OVERRIDE {ov} (logged; blocked by {blocked_by})", tail]
    return EXIT_BOX, box_packet(pr, head, blockers, same, repo, repo_dir, selector) + [tail]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gate")
    g.add_argument("--repo", default=DEFAULT_REPO)
    g.add_argument("--pr", type=int, required=True)
    g.add_argument("--head", required=True)
    g.add_argument("--repo-dir")
    g.add_argument("--dry-run", action="store_true")
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        return EXIT_USAGE
    if not SHA_RE.fullmatch(args.head) or args.pr <= 0:
        print("paid_round: usage error (full 40-hex sha and a positive PR number required)")
        return EXIT_USAGE
    try:
        rc, lines = evaluate(args.repo, args.pr, args.head, args.repo_dir, dry=args.dry_run)
    except (Unavailable, OSError, subprocess.SubprocessError) as exc:
        print(f"paid_round: UNAVAILABLE reason={exc}")
        return EXIT_UNAVAILABLE
    print("\n".join(lines))
    return rc


if __name__ == "__main__":
    sys.exit(main())
