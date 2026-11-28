#!/usr/bin/env python3
"""ONE paid CI/E2E round per PR (paid-round gate for pm-admit-ci.sh).

Directive (Rajiv C0ALZJHGE49 thread 1795841629.732639, ts 1795845160.422969 + ts 1795845739.605399):
"yes, admin merge after testing with remote box. the qa videos should be posted to slack for review".
Each PR gets ONE paid CI/E2E round.  Every later head is proved on remote boxes scoped to the failing tests and then
merged by a head-pinned S167 admin merge; no further paid round is dispatched.

A PAID ROUND = an earlier admitted head (an exact `ci-head:<sha>` labeled event, the same source pm-admit-cap.py counts) with at
least one SUBSTANTIVE ci.yml/e2e.yml pull_request run (label-wrapper gate runs ignored, pm-admit-verdict.substantive_runs) in which a job
did real work: executed jobs that are not all quick gate/classifier jobs (ci_admitted_runs.is_noop), or a job still running.
Gate-only / classifier-skipped runs never count.  The cap breaker (pm-admit-cap.py, CAP=2) is a separate, older guard; both stay.

gate --repo R --pr N --head SHA [--repo-dir DIR] [--dry-run]
  rc 0   dispatch: first paid round, same head re-admission, CI-input delta, or a logged override
  rc 21  BOX_ROUND_REQUIRED: a paid round already ran for another head; NOTHING is dispatched; prints the box test list
  rc 11  UNAVAILABLE (GitHub/git unreadable): the caller refuses, never assumes round 0
  rc 64  usage

Override (logged): PM_ADMIT_PAID_ROUND_OVERRIDE=cto:<ts>@<pr>:<sha>  or  rajiv:<ts>@<pr>:<sha>  (exact pr and head; anything else is ignored).
CI-input delta: the PR-authored delta (prev..head, intersected with the PR's own files, so a main merge does not count) touches
`.github/workflows/**` or non-test `scripts/ci/**`: that is a fresh paid round (rule 62 third trigger), logged.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import json
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ci_admitted_runs as car  # noqa: E402


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


def log(line: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as fh:
            fh.write(time.strftime("%Y-%m-%dT%H:%M:%S%z") + " " + line + "\n")
    except OSError:
        pass


def jobs_of(repo: str, run_id: int):
    jobs = []
    for page in pav.gh_pages(f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100&filter=latest"):
        got = page.get("jobs") if isinstance(page, dict) else None
        if not isinstance(got, list):
            raise Unavailable(f"jobs {run_id}: bad_page")
        jobs.extend(j for j in got if isinstance(j, dict))
    return jobs


def run_executed(jobs) -> bool:
    """True when the run's jobs did real work: a job is still running, or the executed jobs are not all quick gate jobs."""
    if not jobs:
        return False
    if any(j.get("status") == "in_progress" for j in jobs):
        return True
    stamps = [car.parse_time(j.get("started_at")) for j in jobs if j.get("started_at")]
    ends = [car.parse_time(j.get("completed_at")) for j in jobs if j.get("completed_at")]
    wall = int((max(ends) - min(stamps)).total_seconds()) if stamps and ends and max(ends) >= min(stamps) else None
    executed = [j for j in jobs if str(j.get("conclusion") or "") not in ("", "skipped")]
    if not executed:
        return False
    return not car.is_noop(wall, jobs)


def paid_heads(repo: str, pr: int, head: str):
    """-> [{head, runs: [(run, jobs)]}] for earlier admitted heads (newest first, excluding `head`) whose run executed real work."""
    out = []
    for cand, _ in pav.admitted_heads(repo, pr):
        if cand == head:
            continue
        selected, _ignored = pav.substantive_runs(repo, cand)
        paid = []
        for run in selected:
            jobs = jobs_of(repo, run["id"])
            if run_executed(jobs):
                paid.append((run, jobs))
        if paid:
            out.append({"head": cand, "runs": paid})
    return out


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
        for run, jobs in item["runs"]:
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


def select_e2e_specs(repo_dir: str, prev: str, head: str):
    """Best-effort: run the app repo's change_scope --select-e2e-specs on an archive of `head`; None -> select on the box."""
    try:
        with tempfile.TemporaryDirectory(prefix="paid-round-") as tmp:
            arch = subprocess.run(f"git -C {repo_dir!s} archive {head} | tar -x -C {tmp}", shell=True, capture_output=True, timeout=120)
            if arch.returncode or not Path(tmp, "scripts/ci/change_scope.py").exists():
                return None
            proc = subprocess.run(
                [sys.executable, "-I", "scripts/ci/change_scope.py", "--select-e2e-specs", "--event", "push", "--base", prev, "--head", head,
                 "--unit-execution-sha", head, "--unit-workflow-sha", head, "--unit-expected-workflow-sha", head, "--repo-root", tmp],
                cwd=tmp, capture_output=True, text=True, timeout=120)
            if proc.returncode:
                return None
            data = json.loads(proc.stdout)
            specs = data.get("specs")
            return [str(s) for s in specs] if isinstance(specs, list) else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def box_packet(pr: int, head: str, paid, repo: str, repo_dir: str | None, selector=select_e2e_specs):
    prev = paid[0]["head"]
    tests, jobs_failed = failing_tests(repo, pr, paid)
    overlap_tests, merged = [], False
    if repo_dir:
        try:
            if merged_main(repo_dir, prev, head):
                merged = True
                overlap_tests = nearest_tests(repo_dir, head, overlap_files(repo_dir, prev, head))
        except (Unavailable, OSError, subprocess.TimeoutExpired):
            overlap_tests = []
    specs = selector(repo_dir, prev, head) if repo_dir else None
    lines = [f"BOX_ROUND_REQUIRED pr={pr} head={head}",
             f"  why: ONE paid CI/E2E round per PR already ran (head {prev}); this head is proved on remote boxes, then a head-pinned S167 admin merge. "
             "Nothing was dispatched, labelled or written.",
             "  paid_rounds: " + ", ".join(f"{i['head'][:12]}[" + ",".join(pav.run_ref(r) + ":" + str(r["conclusion"] or "none") for r, _ in i["runs"]) + "]" for i in paid),
             "  box tests (remote-test-runner, scoped, red-green not required):",
             "    failing_tests: " + ("; ".join(tests[:MAX_LIST]) if tests else "UNAVAILABLE (not in a ci-verdict comment; read the round-1 run logs)")
             + (f" (+{len(tests) - MAX_LIST} more)" if len(tests) > MAX_LIST else ""),
             "    failed_jobs: " + ("; ".join(jobs_failed[:MAX_LIST]) if jobs_failed else "none recorded"),
             "    rule62_overlap_nearest_owner_tests: " + (("; ".join(overlap_tests[:MAX_LIST]) if overlap_tests else "none found") if merged else "n/a (main not merged since the paid head)"),
             "    typecheck: app tsc AND tests tsc on the box at this head (both required)",
             "  e2e (remote-qa-runner, named Playwright specs, video ON; the receipt MUST list the MP4 paths for Slack review):"]
    if specs is None:
        lines.append(f"    SELECT_ON_BOX: scripts/ci/change_scope.py --select-e2e-specs --event push --base {prev} --head {head} (run at the head checkout on the box)")
    else:
        lines.append("    specs: " + ("; ".join(specs[:MAX_LIST]) if specs else "none selected for this delta"))
    lines.append("  next: post the MP4s to Slack, then the CTO head-pinned admin-merge ask (S167). To dispatch a paid round anyway set "
                 f"{OVERRIDE_ENV}=cto:<ts>@{pr}:{head} (or rajiv:<ts>@{pr}:{head}); it is logged.")
    return lines


def evaluate(repo: str, pr: int, head: str, repo_dir: str | None, env=None, dry: bool = False, selector=select_e2e_specs):
    env = os.environ if env is None else env
    tag = "[dry-run] " if dry else ""
    paid = paid_heads(repo, pr, head)
    if not paid:
        return 0, [f"{tag}paid_round: dispatch pr={pr} head={head} (no earlier paid round)"]
    ov = parse_override(env.get(OVERRIDE_ENV, ""), pr, head)
    if ov:
        if not dry:
            log(f"paid_round override pr={pr} head={head} override={ov} earlier_paid={paid[0]['head']}")
        return 0, [f"{tag}paid_round: dispatch pr={pr} head={head} OVERRIDE {ov} (logged; earlier paid head {paid[0]['head']})"]
    if repo_dir:
        try:
            changed = ci_input_delta(repo_dir, paid[0]["head"], head)
        except (Unavailable, OSError, subprocess.TimeoutExpired) as exc:
            raise Unavailable(f"ci_input_delta: {exc}")
        if changed:
            if not dry:
                log(f"paid_round ci_input_delta pr={pr} head={head} prev={paid[0]['head']} files={','.join(changed[:10])}")
            return 0, [f"{tag}paid_round: dispatch pr={pr} head={head} CI-input delta {','.join(changed[:5])}{'...' if len(changed) > 5 else ''} (logged; fresh paid round)"]
    return EXIT_BOX, box_packet(pr, head, paid, repo, repo_dir, selector)


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
    except (Unavailable, OSError, subprocess.TimeoutExpired) as exc:
        print(f"paid_round: UNAVAILABLE reason={exc}")
        return EXIT_UNAVAILABLE
    print("\n".join(lines))
    return rc


if __name__ == "__main__":
    sys.exit(main())
