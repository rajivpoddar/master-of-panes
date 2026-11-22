"""Shared definition of an ADMITTED ci.yml / e2e.yml pull_request run (read-only helpers, no I/O).

Rajiv approval C0ALZJHGE49 thread 1795324302.697259, ts 1795324706.834969 ("yes"): the merged-PR "rounds" in the
morning brief (pm/merged-pr-rounds.py) and the CI-admission cap (ci/pm-admit-cap.py) both count admissions only.

Every pull_request label event (a P2 priority label, pm-state:*, the exact ci-head:<sha> admission label) starts
a ci.yml and an e2e.yml run. Only the exact-head label activates the lane jobs; every other label event is a
gate-only bookkeeping run. Those runs are NOT admissions, and they can end red (the verifier job fails when no lane
ran) and can wait on the ops runner for over a minute, so neither the conclusion nor the run's wall time identifies them.
What does: no job that executed did real work. A no-op run's executed jobs (scope classifier plus one verifier)
each finish within seconds, while an admitted run executes lane jobs that run for minutes (observed on #14302:
no-op runs 22-99 s wall, red or green, with every executed job at most ~15 s; admitted runs 30-65 min wall).
Known limit: an admitted run in which EVERY executed job fails within QUICK_JOB_S (a total runner/setup outage)
reads as a no-op. For the rounds report that undercounts one round; for the admission cap it is an infra-class
red, which the cap does not count anyway.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

WORKFLOW_FILES = {"ci.yml": "ci", "e2e.yml": "e2e"}
WORKFLOW_NAMES = {"CI": "ci", "E2E Smoke Tests": "e2e"}
# An executed job shorter than this did no lane work (scope classifier, verifier gate).
QUICK_JOB_S = 60
# A run whose wall time exceeds this cannot be gate-only: skip the jobs lookup for it.
NOOP_WALL_S = 600


def parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def seconds_between(start: str | None, end: str | None) -> int | None:
    a, b = parse_time(start), parse_time(end)
    return int((b - a).total_seconds()) if a and b and b >= a else None


def wall_seconds(run: dict[str, Any]) -> int | None:
    """Run wall time from the workflow-runs API object (run_started_at -> updated_at)."""
    return seconds_between(run.get("run_started_at"), run.get("updated_at"))


def workflow_kind(run: dict[str, Any]) -> str | None:
    """'ci' / 'e2e' for the two paid workflows, else None. Works on a REST run (path) or gh run list (name)."""
    path = str(run.get("path") or "").split("@", 1)[0]
    if path:  # the file is authoritative: a perf.yml run is never CI just because its name says so
        return WORKFLOW_FILES.get(path.rsplit("/", 1)[-1])
    return WORKFLOW_NAMES.get(str(run.get("name") or run.get("workflowName") or ""))


def needs_jobs_lookup(wall_s: int | None) -> bool:
    """Only a short run can be gate-only; unknown timing is never assumed to be."""
    return wall_s is not None and wall_s < NOOP_WALL_S


def is_noop(wall_s: int | None, jobs: list[dict[str, Any]] | None) -> bool:
    """True when the run is short and every job that executed finished within QUICK_JOB_S.

    Not a no-op (so it counts) when: wall time unknown or long, no job list, no job executed (e.g. a
    startup_failure), or any executed job has unknown timing or ran QUICK_JOB_S or longer.
    """
    if not needs_jobs_lookup(wall_s) or not jobs:
        return False
    executed = [j for j in jobs if str(j.get("conclusion") or "") not in ("", "skipped")]
    if not executed:
        return False
    for job in executed:
        took = seconds_between(job.get("started_at"), job.get("completed_at"))
        if took is None or took >= QUICK_JOB_S:
            return False
    return True
