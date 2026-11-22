#!/usr/bin/env python3
"""Read-only CI-admission cap check for pm-admit-ci.sh (mirrors the 2-round review cap).

Rajiv approval C0ALZJHGE49 thread 1795324302.697259, ts 1795324706.834969 ("yes"): after 2 red admissions caused
by the PR itself, the PR stops re-admitting and goes to the S131 one-tier-up handoff (or a CTO packet).

Counts DISTINCT admitted heads of the PR whose ci.yml or e2e.yml ended red with a PR-caused classification.
  admitted head   = an exact `ci-head:<40-hex>` label event on the PR (the pm-admit-ci.sh admission record).
  red             = a ci.yml / e2e.yml pull_request run at that head concluded failure, timed_out or startup_failure,
                    or cancelled when no later head was admitted (a cancel on a superseded head is a concurrency
                    cancel by the next admission, not a verdict on the PR). Gate-only no-op label runs never count
                    (ci_admitted_runs.is_noop): they are not admissions even when they end red.
  PR-caused       = everything except a red recorded as infra / flaky / out-of-diff / inherited-main. The record is
                    the existing `<!-- ci-verdict: {...} -->` PR comment that ci-failure-investigation posts for the
                    exact run, attempt and head (newest comment wins; its `classification` field is free text, so it
                    is matched on tokens). A red with no verdict, or a verdict that does not say so, is PR-caused.
No ci_failure_reconcile or ci_rework obligation is read: reconcile rows carry no classification and rework rows
hold placeholders (pending, under-investigation), and the pm-ops DB is never touched.

Exit: 0 under the cap | 10 cap reached | 11 UNAVAILABLE (never guessed; prints the reason) | 64 usage.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))  # python3 -I does not add the script dir
import ci_admitted_runs as car  # noqa: E402

CAP = 2
DEFAULT_REPO = "Scribie/heydonna-app"
HEAD_LABEL = re.compile(r"^ci-head:([0-9a-f]{40})$")
RED_CONCLUSIONS = {"failure", "timed_out", "startup_failure"}
CANCELLED = "cancelled"
VERDICT_MARKER = re.compile(r"ci-failure-investigation:run=(\d+) attempt=(\d+) head=([0-9a-f]{40})")
VERDICT_JSON = re.compile(r"<!--\s*ci-verdict:\s*(\{.*?\})\s*-->", re.S | re.I)
EXEMPT_WORDS = {"infra", "infrastructure", "flaky", "flake", "inherited"}
NEGATIONS = {"not", "non", "no", "never"}
PR_OWNED_PHRASES = (("pr", "caused"), ("pr", "introduced"), ("candidate", "owned"), ("code", "regression"))
OUT_OF_DIFF = ("out", "of", "diff")

Api = Callable[..., list]


class Unavailable(Exception):
    pass


def make_api(repo: str) -> Api:
    """GET repos/<repo>/<path> with pagination flattened; one retry; Unavailable with a reason on failure."""
    def api(path: str, key: str | None = None) -> list:
        cmd = ["gh", "api", f"repos/{repo}/{path}", "--paginate", "--slurp"]
        reason = "unknown"
        for attempt in (1, 2):
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            except subprocess.TimeoutExpired:
                reason = "timeout"
            except OSError as exc:
                reason = f"gh_not_runnable:{type(exc).__name__}"
            else:
                if proc.returncode == 0:
                    try:
                        pages = json.loads(proc.stdout or "[]")
                        return [x for page in pages for x in ((page or {}).get(key, []) if key else (page or []))]
                    except (ValueError, AttributeError, TypeError):
                        reason = "bad_json"
                else:
                    reason = f"gh_rc_{proc.returncode}"
            if attempt == 1:
                try:
                    time.sleep(float(os.environ.get("PM_ADMIT_RETRY_SLEEP", "3")))
                except ValueError:
                    time.sleep(3)
        raise Unavailable(f"{path.split('?')[0]}: {reason}")
    return api


def admitted_heads(events: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Distinct heads that got an exact ci-head:<sha> label, as (sha, first_labeled_at), oldest admission first."""
    first: dict[str, str] = {}
    for event in events:
        if event.get("event") != "labeled":
            continue
        match = HEAD_LABEL.match(str((event.get("label") or {}).get("name") or ""))
        if match:
            stamp = str(event.get("created_at") or "")
            sha = match.group(1)
            first[sha] = min(first.get(sha, stamp), stamp)
    return sorted(first.items(), key=lambda item: (item[1], item[0]))


def _has_phrase(words: list[str], phrase: tuple[str, ...]) -> bool:
    size = len(phrase)
    for i in range(len(words) - size + 1):
        if tuple(words[i:i + size]) == phrase and not (i > 0 and words[i - 1] in NEGATIONS):
            return True
    return False


def classification_is_exempt(text: Any) -> bool:
    """True when a verdict's free-text classification says infra, flaky, out-of-diff or inherited-main.

    A PR-owned phrase (pr-caused, candidate-owned, code-regression...) wins, and a negated term (not-flaky) never exempts.
    """
    words = [w for w in re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).split("_") if w]
    if any(_has_phrase(words, phrase) for phrase in PR_OWNED_PHRASES):
        return False
    for i, word in enumerate(words):
        if word in EXEMPT_WORDS and not (i > 0 and words[i - 1] in NEGATIONS):
            return True
    return _has_phrase(words, OUT_OF_DIFF)


def verdict_classifications(comments: list[dict[str, Any]]) -> dict[tuple[int, int, str], str]:
    """(run_id, attempt, head) -> classification from the newest ci-failure-investigation verdict comment for that run."""
    best: dict[tuple[int, int, str], tuple[tuple[str, int], str]] = {}
    for comment in comments:
        body = str(comment.get("body") or "")
        marker = VERDICT_MARKER.search(body)
        payload = VERDICT_JSON.search(body)
        if not marker or not payload:
            continue
        try:
            verdict = json.loads(payload.group(1))
        except ValueError:
            continue
        classification = verdict.get("classification") if isinstance(verdict, dict) else None
        if not isinstance(classification, str) or not classification.strip():
            continue  # malformed or empty never masks an earlier usable verdict
        if str(verdict.get("run_id") or marker.group(1)) != marker.group(1):
            continue  # marker and JSON disagree on the run: not a verdict for this run
        key = (int(marker.group(1)), int(marker.group(2)), marker.group(3))
        stamp = (str(comment.get("updated_at") or comment.get("created_at") or ""), int(comment.get("id") or 0))
        if key not in best or stamp >= best[key][0]:
            best[key] = (stamp, classification)
    return {key: value[1] for key, value in best.items()}


def red_runs(runs: list[dict[str, Any]], pr: int, superseded: bool) -> list[dict[str, Any]]:
    """The head's ci.yml / e2e.yml pull_request runs that ended red (no-op filtering is done by the caller)."""
    out = []
    for run in runs:
        if run.get("event") != "pull_request" or not car.workflow_kind(run):
            continue
        numbers = [int(p.get("number") or 0) for p in (run.get("pull_requests") or [])]
        if numbers and pr not in numbers:
            continue  # same head sha on another PR
        conclusion = str(run.get("conclusion") or "")
        if conclusion in RED_CONCLUSIONS or (conclusion == CANCELLED and not superseded):
            out.append(run)
    return out


def evaluate(pr: int, events: list[dict[str, Any]], api: Api, cap: int = CAP) -> dict[str, Any]:
    """Pure decision over fetched data; `api` is only called for comments, runs and jobs and may raise Unavailable."""
    heads = admitted_heads(events)
    result: dict[str, Any] = {"pr": pr, "cap": cap, "admitted_heads": len(heads), "red_heads": [], "status": "ok"}
    if len(heads) < cap:
        return result  # fewer admitted heads than the cap: it cannot be reached, no further lookups
    verdicts = verdict_classifications(api(f"issues/{pr}/comments?per_page=100"))
    for index, (sha, _) in enumerate(heads):
        runs = api(f"actions/runs?head_sha={sha}&event=pull_request&per_page=100", key="workflow_runs")
        reds = []
        for run in red_runs(runs, pr, superseded=index < len(heads) - 1):
            wall = car.wall_seconds(run)
            if car.needs_jobs_lookup(wall) and car.is_noop(wall, api(f"actions/runs/{run['id']}/jobs?per_page=100", key="jobs")):
                continue  # gate-only bookkeeping run, not an admission
            classification = verdicts.get((int(run["id"]), int(run.get("run_attempt") or 1), sha))
            if classification is not None and classification_is_exempt(classification):
                continue
            reds.append({"workflow": car.workflow_kind(run), "run_id": int(run["id"]),
                         "conclusion": str(run.get("conclusion")), "classification": classification or "unrecorded"})
        if reds:
            result["red_heads"].append({"head": sha, "runs": reds})
            if len(result["red_heads"]) >= cap:
                result["status"] = "cap"
                break  # enough to refuse; later heads need no lookup
    return result


def _clean(text: Any, limit: int = 60) -> str:
    return re.sub(r"[^A-Za-z0-9._:/+ -]", "?", str(text))[:limit]


def render(result: dict[str, Any]) -> str:
    count = len(result["red_heads"])
    if result["status"] == "ok":
        return (f"ADMISSION_CAP: ok pr={result['pr']} admitted_heads={result['admitted_heads']} "
                f"pr_caused_red_heads={count} cap={result['cap']}\n")
    lines = [f"ADMISSION_CAP_{result['cap']}: PR #{result['pr']} has {count} admitted heads that ended red with a PR-caused "
             f"classification (not infra/flaky/out-of-diff); admitted_heads={result['admitted_heads']}"]
    for entry in result["red_heads"]:
        for run in entry["runs"]:
            lines.append(f"  head {entry['head'][:10]} {run['workflow']} run {run['run_id']} {run['conclusion']} "
                         f"classification={_clean(run['classification'])}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None, api: Api | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--cap", type=int, default=CAP)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 64
    if args.pr <= 0 or args.cap <= 0:
        print("ADMISSION_CAP: usage error")
        return 64
    api = api or make_api(args.repo)
    try:
        events = api(f"issues/{args.pr}/events?per_page=100")
        result = evaluate(args.pr, events, api, args.cap)
    except Unavailable as exc:
        print(f"ADMISSION_CAP: UNAVAILABLE reason={_clean(exc, 120)}")
        return 11
    sys.stdout.write(render(result))
    return 10 if result["status"] == "cap" else 0


if __name__ == "__main__":
    sys.exit(main())
