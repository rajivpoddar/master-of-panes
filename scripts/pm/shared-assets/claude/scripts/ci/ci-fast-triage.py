#!/usr/bin/env python3
"""Fast, read-only CI failure fingerprinting from GitHub check annotations."""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Iterable


DEFAULT_REPO = "heydonna-app/heydonna-app"
WORKFLOWS = ("ci.yml", "e2e.yml")
VERDICT_MARKER = re.compile(r"<!--\s*ci-verdict:\s*(\{.*?\})\s*-->", re.S | re.I)
REQUIRED_CI_JOBS_PATH = Path(__file__).with_name("required-ci-jobs.json")


def load_required_ci_jobs(path: Path = REQUIRED_CI_JOBS_PATH) -> dict[str, str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        str(workflow): str(job)
        for workflow, job in value.items()
        if isinstance(workflow, str)
        and workflow
        and isinstance(job, str)
        and job
    }


REQUIRED_CI_JOBS = load_required_ci_jobs()


@dataclass(frozen=True)
class Annotation:
    path: str
    start_line: int | None
    level: str
    title: str
    message: str
    raw_details: str

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> "Annotation":
        return cls(
            path=str(value.get("path") or ""),
            start_line=value.get("start_line"),
            level=str(value.get("annotation_level") or ""),
            title=str(value.get("title") or ""),
            message=str(value.get("message") or ""),
            raw_details=str(value.get("raw_details") or ""),
        )

    @property
    def text(self) -> str:
        return "\n".join(part for part in (self.title, self.message, self.raw_details) if part)


class GitHubAPI:
    def __init__(self, repo: str, token: str) -> None:
        self.repo = repo
        self.token = token

    def get(self, path: str) -> Any:
        return json.loads(self.get_text(path))

    def get_text(self, path: str, max_bytes: int = 4_000_000) -> str:
        if path.startswith("http"):
            url = path
        else:
            url = f"https://api.github.com/repos/{self.repo}/{path.lstrip('/')}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "heydonna-ci-fast-triage",
            },
        )
        try:
            opener = urllib.request.build_opener(_StripAuthOnCrossHostRedirect())
            with opener.open(request, timeout=20) as response:
                return response.read(max_bytes).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub API {error.code} for {url}: {body[:500]}") from error


class _StripAuthOnCrossHostRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, new_url):  # type: ignore[no-untyped-def]
        redirected = super().redirect_request(request, fp, code, msg, headers, new_url)
        if redirected is None:
            return None
        old_host = urllib.parse.urlparse(request.full_url).hostname
        new_host = urllib.parse.urlparse(new_url).hostname
        if old_host != new_host:
            redirected.remove_header("Authorization")
        return redirected


def resolve_token(explicit: str | None) -> str:
    if explicit:
        return explicit
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
    try:
        return subprocess.check_output(["gh", "auth", "token"], text=True).strip()
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        raise RuntimeError("set GH_TOKEN/GITHUB_TOKEN or authenticate gh") from error


def normalize_message(text: str) -> str:
    value = re.sub(r"\b[0-9a-f]{7,40}\b", "<sha>", text, flags=re.I)
    value = re.sub(r"\b\d+(?:\.\d+)?(?:ms|s|m)?\b", "<n>", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:320]


def annotation_signature(annotation: Annotation) -> str:
    message_lines = [line.strip() for line in annotation.message.splitlines() if line.strip()]
    if message_lines:
        return normalize_message(message_lines[0])
    return normalize_message(annotation.title or annotation.path or "annotation-without-message")


def annotation_paths(annotations: Iterable[Annotation]) -> list[str]:
    paths = {annotation.path for annotation in annotations if annotation.path}
    pattern = re.compile(r"(?:^|\s)([A-Za-z0-9_@()./+-]+\.(?:test|spec)\.[jt]sx?)(?::\d+)?")
    for annotation in annotations:
        for match in pattern.finditer(annotation.text):
            paths.add(match.group(1))
    return sorted(paths)


def is_generic_failure_annotation(annotation: Annotation) -> bool:
    message = annotation.message.strip()
    return bool(
        re.fullmatch(r"Process completed with exit code \d+\.?", message)
        or re.fullmatch(
            r"(?:CI|E2E(?: Smoke Tests)?) final verdict FAILED\s*[—-]\s*failing step\(s\):.+",
            message,
            flags=re.I,
        )
    )


def category_for(annotations: Iterable[Annotation], jobs: Iterable[dict[str, Any]]) -> str:
    text = "\n".join(annotation.text for annotation in annotations)
    job_text = "\n".join(str(job.get("name") or "") for job in jobs)
    combined = f"{text}\n{job_text}"
    lower = combined.lower()

    if "convex preview deploy" in lower and "start_push" in lower:
        return "convex-preview-deploy"
    if "heydonna_env is required" in lower or "environment identity" in lower:
        return "environment-contract"
    if re.search(r"\bF(?:401|541|811)\b", combined) or "ruff" in lower:
        return "static-lint"
    if re.search(r"\bTS\d{4}\b", combined) or "typecheck" in lower:
        return "typescript-typecheck"
    if "_workerenabled" in lower or (
        "worker" in lower and any(token in lower for token in ("positionindex", "postmessage", "terminate"))
    ):
        return "worker-contract"
    if any(token in lower for token in ("llm_proxy_cache_miss", "fixture miss", "capture-required")):
        return "capture-or-fixture"
    if any(token in lower for token in ("timed out", "timeout", "wall budget", "wall-budget")):
        return "timeout-or-wall-budget"
    if (
        any(token in lower for token in ("performance", "under_100ms", "threshold"))
        or re.search(r"(?:<|under)\s*\d+(?:\.\d+)?\s*ms\b", lower)
    ):
        return "performance-threshold"
    if any(token in lower for token in ("assert", "vitest", "pytest", "test failure", "tests failed")):
        return "test-failure"
    if any(token in lower for token in ("checkout", "install dependencies", "setup job")):
        return "setup-or-runner"
    return "unknown"


def local_command(category: str, annotations: Iterable[Annotation]) -> str | None:
    paths = annotation_paths(annotations)
    py_paths = [path for path in paths if path.endswith(".py")]
    ts_paths = [path for path in paths if path.endswith((".ts", ".tsx"))]
    ts_test_paths = [
        path
        for path in ts_paths
        if "/__tests__/" in f"/{path}" or re.search(r"\.(?:test|spec)\.[jt]sx?$", path)
    ]
    e2e_paths = [path for path in ts_test_paths if path.startswith("tests/e2e/")]

    if category == "convex-preview-deploy":
        return "not applicable (external Convex control-plane failure)"
    if category == "static-lint":
        targets = " ".join(py_paths) if py_paths else "modal/ scripts/modal-gc-classify.py"
        return f"python3 -m ruff check {targets}"
    if category == "typescript-typecheck":
        if ts_paths and all(path.startswith("convex/") for path in ts_paths):
            return "npx tsc --noEmit --project convex/tsconfig.json"
        return "npx tsc --noEmit"
    if category in {"environment-contract", "worker-contract", "test-failure"}:
        if ts_test_paths:
            return f"npx vitest run {' '.join(ts_test_paths)}"
        if ts_paths:
            return f"npx vitest run {' '.join(ts_paths)}"
        if py_paths:
            return f"python3 -m pytest {' '.join(py_paths)} -q"
    if category == "performance-threshold":
        if e2e_paths:
            return f"bash scripts/e2e/local-repro-preflight.sh --detach --spec {e2e_paths[0]}"
        if py_paths:
            return f"python3 -m pytest {' '.join(py_paths)} -q"
    if category == "test-failure" and e2e_paths:
        return f"bash scripts/e2e/local-repro-preflight.sh --detach --spec {e2e_paths[0]}"
    if category in {"capture-or-fixture", "timeout-or-wall-budget"}:
        return "bash scripts/e2e/local-repro-preflight.sh --detach --spec <failed-spec>"
    return None


def fingerprint(annotations: list[Annotation], jobs: list[dict[str, Any]]) -> dict[str, Any]:
    failure_annotations = [annotation for annotation in annotations if annotation.level == "failure"]
    evidence = failure_annotations or [annotation for annotation in annotations if annotation.level != "warning"]
    non_generic_evidence = [annotation for annotation in evidence if not is_generic_failure_annotation(annotation)]
    if non_generic_evidence:
        evidence = non_generic_evidence
    elif evidence:
        evidence = []

    category = category_for(evidence, jobs)
    groups: collections.Counter[str] = collections.Counter()
    representatives: dict[str, Annotation] = {}
    for annotation in evidence:
        key = annotation_signature(annotation)
        groups[key] += 1
        representatives.setdefault(key, annotation)

    if groups:
        key, count = groups.most_common(1)[0]
        representative = representatives[key]
    else:
        key, count = "no-check-annotation", 0
        representative = Annotation("", None, "", "", "", "")

    return {
        "category": category,
        "signature": key,
        "occurrences": count,
        "annotation_count": len(evidence),
        "raw_annotation_count": len(annotations),
        "needs_log_fallback": not evidence,
        "representative": asdict(representative),
        "paths": annotation_paths(evidence),
        "local_repro_command": local_command(category, evidence),
    }


def resolve_pr(api: GitHubAPI, run: dict[str, Any]) -> dict[str, Any] | None:
    pull_requests = run.get("pull_requests") or []
    if pull_requests:
        number = pull_requests[0].get("number")
        if number:
            return api.get(f"pulls/{number}")

    sha = run.get("head_sha")
    if not sha:
        return None
    candidates = api.get(f"commits/{sha}/pulls?per_page=20")
    if not candidates:
        return None
    open_candidates = [item for item in candidates if item.get("state") == "open"]
    return (open_candidates or candidates)[0]


def fetch_annotations(api: GitHubAPI, jobs: list[dict[str, Any]]) -> list[Annotation]:
    values: list[Annotation] = []
    for job in jobs:
        if job.get("conclusion") not in {"failure", "timed_out", "cancelled"}:
            continue
        job_id = job.get("id")
        if not job_id:
            continue
        for value in api.get(f"check-runs/{job_id}/annotations?per_page=100"):
            values.append(Annotation.from_json(value))
    return values


def extract_log_annotations(log_text: str) -> list[Annotation]:
    log_text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", log_text)
    log_text = re.sub(r"(?m)^\d{4}-\d{2}-\d{2}T\S+Z\s+", "", log_text)
    annotations: list[Annotation] = []
    seen: set[tuple[str, int | None, str]] = set()

    convex_start_push = re.compile(
        r"Error fetching\s+(?:GET|POST)\s+https?://\S+\.convex\.cloud/"
        r"api/deploy2/start_push\s+(\d{3})\s+([^\n]+)",
        flags=re.I,
    )
    for match in convex_start_push.finditer(log_text):
        status, detail = match.groups()
        message = f"Convex preview deploy start_push failed: HTTP {status} {detail.strip()}"
        key = ("", None, message)
        if key not in seen:
            seen.add(key)
            annotations.append(Annotation("", None, "failure", "", message, ""))

    ruff_block = re.compile(
        r"(?m)^(F\d{3}[^\n]+)\n\s*-->\s*([^:\n]+\.py):(\d+):\d+"
    )
    for match in ruff_block.finditer(log_text):
        message, path, line_text = match.groups()
        key = (path, int(line_text), message)
        if key not in seen:
            seen.add(key)
            annotations.append(Annotation(path, int(line_text), "failure", "", message, ""))

    pytest_progress = re.compile(
        r"(?m)^(?:\S+\s+)?([^\s:]+\.py)::([^\s]+).*\sFAILED(?:\s|$)"
    )
    for match in pytest_progress.finditer(log_text):
        path = match.group(1)
        message = f"{path}::{match.group(2)} FAILED"
        key = (path, None, message)
        if key not in seen:
            seen.add(key)
            annotations.append(Annotation(path, None, "failure", "", message, ""))

    playwright_failure = re.compile(
        r"(?ms)^\s*\d+\)\s+\[[^\]]+\]\s+›\s+"
        r"([^:\n]+\.(?:spec|test)\.[jt]sx?):(\d+):\d+\s+›[^\n]*\n"
        r"(?:\s*\n)?\s*Error:\s+([^\n]+)"
    )
    for match in playwright_failure.finditer(log_text):
        path, line_text, message_text = match.groups()
        message = f"Error: {message_text.strip()}"
        key = (path, int(line_text), message)
        if key not in seen:
            seen.add(key)
            annotations.append(Annotation(path, int(line_text), "failure", "", message, ""))

    patterns = (
        re.compile(r"(?m)^([^\s:]+\.py):(\d+):\d+:\s+(F\d{3}\s+[^\n]+)$"),
        re.compile(r"(?m)^([^\s:(]+\.(?:ts|tsx))\((\d+),\d+\):\s+(error\s+TS\d{4}:[^\n]+)$", re.I),
        re.compile(r"(?m)^FAILED\s+([^\s:]+\.py)(?:::\S+)?\s+-\s+([^\n]+)$"),
    )
    for pattern in patterns:
        for match in pattern.finditer(log_text):
            path = match.group(1)
            try:
                line = int(match.group(2))
                message = match.group(3)
            except ValueError:
                line = None
                message = match.group(2)
            key = (path, line, message)
            if key in seen:
                continue
            seen.add(key)
            annotations.append(Annotation(path, line, "failure", "", message, ""))

    # Vitest/pytest annotations are normally rich. This fallback handles runners
    # that only expose a generic check annotation but retain the root error in logs.
    if not annotations:
        error_patterns = (
            re.compile(r"(?m)^(Error:\s+HEYDONNA_ENV[^\n]+)$"),
            re.compile(r"(?m)^(AssertionError:\s+[^\n]+)$"),
            re.compile(r"(?m)^([^\n]*(?:under_\d+ms|performance threshold)[^\n]*)$", re.I),
        )
        for pattern in error_patterns:
            match = pattern.search(log_text)
            if match:
                annotations.append(Annotation("", None, "failure", "", match.group(1).strip(), ""))
                break
    return annotations


def cancelled_envelope(jobs: list[dict[str, Any]], stale: bool) -> str:
    if stale:
        return "superseded-stale-run"
    if not jobs:
        return "phantom-zero-job"
    successful_steps = sum(
        1
        for job in jobs
        for step in (job.get("steps") or [])
        if step.get("conclusion") == "success"
    )
    terminal_jobs = [job for job in jobs if job.get("conclusion") not in {None, "skipped", "cancelled"}]
    if successful_steps == 0 and not terminal_jobs:
        return "concurrency-cancel"
    return "current-head-cancel-needs-local-classification"


def required_job_envelope(workflow: str, jobs: list[dict[str, Any]]) -> dict[str, Any]:
    required_job = REQUIRED_CI_JOBS.get(workflow)
    required = next(
        (job for job in jobs if str(job.get("name") or "") == required_job),
        None,
    )
    failed_jobs = [
        str(job.get("name") or "")
        for job in jobs
        if job.get("conclusion") in {"failure", "timed_out"}
    ]
    required_status = str((required or {}).get("status") or "")
    required_conclusion = str((required or {}).get("conclusion") or "")
    optional_sibling_failure = bool(
        required_job
        and required
        and required_status == "completed"
        and required_conclusion == "success"
        and failed_jobs
        and all(name != required_job for name in failed_jobs)
    )
    return {
        "required_job": required_job,
        "required_job_status": required_status,
        "required_job_conclusion": required_conclusion,
        "failed_jobs": failed_jobs,
        "optional_sibling_failure": optional_sibling_failure,
    }


def _run_attempt(run: dict[str, Any]) -> int:
    value = run.get("run_attempt")
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise RuntimeError("run attempt provenance is missing or malformed")
    try:
        attempt = int(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError("run attempt provenance is missing or malformed") from error
    if attempt < 1 or str(attempt) != str(value).strip():
        raise RuntimeError("run attempt provenance is missing or malformed")
    return attempt


def _jobs_for_attempt(
    jobs_payload: Any, run_attempt: int
) -> list[dict[str, Any]]:
    if not isinstance(jobs_payload, dict) or not isinstance(jobs_payload.get("jobs"), list):
        raise RuntimeError("workflow jobs response is missing or malformed")
    selected: list[dict[str, Any]] = []
    for job in jobs_payload["jobs"]:
        if not isinstance(job, dict):
            raise RuntimeError("workflow job attempt provenance is malformed")
        value = job.get("run_attempt")
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise RuntimeError("workflow job attempt provenance is missing or malformed")
        try:
            job_attempt = int(value)
        except (TypeError, ValueError) as error:
            raise RuntimeError("workflow job attempt provenance is missing or malformed") from error
        if job_attempt < 1 or str(job_attempt) != str(value).strip():
            raise RuntimeError("workflow job attempt provenance is missing or malformed")
        if job_attempt == run_attempt:
            if job.get("id") in (None, ""):
                raise RuntimeError("current workflow job id is missing or malformed")
            selected.append(job)
    if jobs_payload["jobs"] and not selected:
        raise RuntimeError("current run attempt has no matching workflow jobs")
    return selected


def triage_run(
    api: GitHubAPI,
    run_id: int,
    *,
    run_detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run = run_detail if run_detail is not None else api.get(f"actions/runs/{run_id}")
    if not isinstance(run, dict):
        raise RuntimeError("authoritative workflow run response is malformed")
    run_attempt = _run_attempt(run)
    jobs_payload = api.get(f"actions/runs/{run_id}/jobs?filter=all&per_page=100")
    jobs = _jobs_for_attempt(jobs_payload, run_attempt)
    pr = resolve_pr(api, run)
    current_head = ((pr or {}).get("head") or {}).get("sha")
    run_head = run.get("head_sha")
    current_for_pr: bool | str = "main-run" if run.get("head_branch") == "main" else bool(pr and current_head == run_head)
    stale = current_for_pr is False

    conclusion = run.get("conclusion")
    if conclusion == "cancelled":
        envelope_class = cancelled_envelope(jobs, stale)
    else:
        envelope_class = "superseded-stale-run" if stale else "current-head-run"

    required = required_job_envelope(str(run.get("name") or ""), jobs)
    sibling_only_failure = bool(required["optional_sibling_failure"])
    main_release_sibling_failure = bool(
        sibling_only_failure and run.get("head_branch") == "main"
    )
    optional_sibling_failure = bool(
        sibling_only_failure and not main_release_sibling_failure
    )
    annotations = [] if stale or sibling_only_failure else fetch_annotations(api, jobs)
    fp = fingerprint(annotations, jobs)
    evidence_source = (
        "required-job-contract" if sibling_only_failure else "check-annotations"
    )
    log_bytes_scanned = 0
    if not stale and not sibling_only_failure and fp["needs_log_fallback"]:
        failed_job = next(
            (job for job in jobs if job.get("conclusion") in {"failure", "timed_out"}),
            None,
        )
        if failed_job and failed_job.get("id"):
            log_text = api.get_text(f"actions/jobs/{failed_job['id']}/logs")
            log_bytes_scanned = len(log_text.encode("utf-8"))
            evidence_source = "representative-job-log-unclassified"
            log_annotations = extract_log_annotations(log_text)
            if log_annotations:
                annotations = log_annotations
                fp = fingerprint(annotations, jobs)
                evidence_source = "representative-job-log"

    prior_fingerprint_count = 0
    if pr and not stale and fp["signature"] != "no-check-annotation":
        comments = api.get(f"issues/{pr['number']}/comments?per_page=100&sort=created&direction=desc")
        prior_fingerprint_count = count_prior_fingerprint(comments, fp["signature"], run_id)
    result = {
        "schema_version": 1,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "workflow": run.get("name"),
        "conclusion": conclusion,
        "status": run.get("status"),
        "url": run.get("html_url"),
        "head_branch": run.get("head_branch"),
        "run_head_sha": run_head,
        "pr": (pr or {}).get("number"),
        "pr_state": (pr or {}).get("state"),
        "current_head_sha": current_head,
        "current_for_pr": current_for_pr,
        "attempt_provenance": {
            "run_attempt": run_attempt,
            "job_ids": [str(job["id"]) for job in jobs],
            "annotation_job_ids": [
                str(job["id"])
                for job in jobs
                if job.get("conclusion") in {"failure", "timed_out", "cancelled"}
            ],
        },
        "actionable": not stale
        and not optional_sibling_failure
        and (
            conclusion in {"failure", "timed_out"}
            or envelope_class == "current-head-cancel-needs-local-classification"
        ),
        "alert_classification": (
            "optional-sibling-failure"
            if optional_sibling_failure
            else (
                "main-release-sibling-failure"
                if main_release_sibling_failure
                else "required-path-or-unclassified-failure"
            )
        ),
        "envelope_class": envelope_class,
        "required_job": required["required_job"],
        "required_job_status": required["required_job_status"],
        "required_job_conclusion": required["required_job_conclusion"],
        "failed_jobs": required["failed_jobs"],
        "fingerprint": fp,
        "evidence_source": evidence_source,
        "log_bytes_scanned": log_bytes_scanned,
        "same_fingerprint_prior_count": prior_fingerprint_count,
        "circuit_breaker": "rescue-or-split-required" if prior_fingerprint_count >= 1 else None,
    }
    return result


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def expected_main_ci_supersession(
    workflow: str, run: dict[str, Any], workflow_runs: list[dict[str, Any]]
) -> bool:
    """Recognize only main CI cancellations replaced by a newer main SHA."""
    if (
        workflow != "ci.yml"
        or run.get("conclusion") != "cancelled"
        or run.get("event") != "push"
        or run.get("head_branch") != "main"
    ):
        return False
    created_at = parse_time(str(run.get("created_at")))
    run_head = str(run.get("head_sha") or "")
    return any(
        candidate is not run
        and candidate.get("event") == "push"
        and candidate.get("head_branch") == "main"
        and parse_time(str(candidate.get("created_at"))) > created_at
        and str(candidate.get("head_sha") or "") != run_head
        for candidate in workflow_runs
    )


def has_verdict_marker(
    api: GitHubAPI, pr_number: int, run_id: int, attempt: int, head_sha: str
) -> bool:
    comments = api.get(f"issues/{pr_number}/comments?per_page=100&sort=created&direction=desc")
    return verdict_for_run(comments, run_id, attempt, head_sha) is not None


def verdict_for_run(
    comments: Iterable[dict[str, Any]], run_id: int, attempt: int, head_sha: str
) -> dict[str, Any] | None:
    marker = (
        f"ci-failure-investigation:run={run_id} attempt={attempt} head={head_sha}"
    )
    for comment in reversed(list(comments)):
        body = str(comment.get("body") or "")
        if marker not in body:
            continue
        matches = list(VERDICT_MARKER.finditer(body))
        if not matches:
            return {}
        try:
            verdict = json.loads(matches[-1].group(1))
        except json.JSONDecodeError:
            return {}
        if isinstance(verdict, dict):
            return verdict
        return {}
    return None


def pending_action(verdict: dict[str, Any]) -> bool:
    return (
        verdict.get("pm_action_status") == "pending"
        or verdict.get("terminal_state") == "pending-pm-action"
    )


def workflow_ids(api: GitHubAPI) -> dict[str, int]:
    payload = api.get("actions/workflows?per_page=100")
    result: dict[str, int] = {}
    for workflow in payload.get("workflows") or []:
        path = str(workflow.get("path") or "")
        workflow_id = workflow.get("id")
        if path and workflow_id:
            result[path.rsplit("/", 1)[-1]] = int(workflow_id)
    return result


def recent_workflow_runs(
    api: GitHubAPI,
    workflow_id: int,
    cutoff: dt.datetime,
    *,
    per_page: int = 20,
    max_pages: int = 5,
) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        query = urllib.parse.urlencode(
            {"status": "completed", "per_page": per_page, "page": page}
        )
        payload = api.get(f"actions/workflows/{workflow_id}/runs?{query}")
        page_runs = payload.get("workflow_runs") or []
        runs.extend(page_runs)
        if len(page_runs) < per_page:
            break
        oldest = min(parse_time(str(run["created_at"])) for run in page_runs)
        if oldest < cutoff:
            break
    return runs


def count_prior_fingerprint(comments: Iterable[dict[str, Any]], signature: str, run_id: int) -> int:
    count = 0
    for comment in comments:
        body = str(comment.get("body") or "")
        for match in VERDICT_MARKER.finditer(body):
            try:
                verdict = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            if str(verdict.get("run_id")) == str(run_id):
                continue
            fast = verdict.get("fast_fingerprint") or {}
            prior_signature = fast.get("signature") if isinstance(fast, dict) else None
            if prior_signature == signature:
                count += 1
    return count


def watchdog(api: GitHubAPI, older_than_minutes: int, lookback_hours: int) -> dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=lookback_hours)
    overdue: list[dict[str, Any]] = []
    pending_actions: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    checked = 0

    try:
        ids = workflow_ids(api)
    except Exception as error:  # noqa: BLE001 - stable degraded envelope.
        return {
            "schema_version": 2,
            "degraded": True,
            "errors": [{"workflow": "all", "error": str(error)}],
            "checked_failures": 0,
            "older_than_minutes": older_than_minutes,
            "lookback_hours": lookback_hours,
            "actionable_count": 0,
            "missing_verdict_count": 0,
            "pending_action_count": 0,
            "missing_verdicts": [],
            "pending_actions": [],
        }

    for workflow in WORKFLOWS:
        workflow_id = ids.get(workflow)
        if workflow_id is None:
            errors.append({"workflow": workflow, "error": "workflow id not found"})
            continue
        try:
            workflow_runs = recent_workflow_runs(api, workflow_id, cutoff)
        except Exception as error:  # noqa: BLE001 - preserve results from the other workflow.
            errors.append({"workflow": workflow, "error": str(error)})
            continue
        for run in workflow_runs:
            if run.get("conclusion") not in {"failure", "cancelled", "timed_out"}:
                continue
            if expected_main_ci_supersession(workflow, run, workflow_runs):
                continue
            created_at = parse_time(run["created_at"])
            completed_at = parse_time(run["updated_at"])
            if created_at < cutoff or (now - completed_at).total_seconds() < older_than_minutes * 60:
                continue
            checked += 1
            try:
                authoritative_run = api.get(f"actions/runs/{run['id']}")
                if not isinstance(authoritative_run, dict):
                    raise RuntimeError("authoritative workflow run response is malformed")
                attempt = _run_attempt(authoritative_run)
                if authoritative_run.get("conclusion") not in {"failure", "cancelled", "timed_out"}:
                    continue
                head_sha = str(authoritative_run.get("head_sha") or "")
                pull = resolve_pr(api, authoritative_run)
                pr_number = (pull or {}).get("number")
                current_head = str(((pull or {}).get("head") or {}).get("sha") or "")
                if pr_number and current_head != head_sha:
                    continue
                comments = (
                    api.get(
                        f"issues/{pr_number}/comments?per_page=100&sort=created&direction=desc"
                    )
                    if pr_number
                    else []
                )
                verdict = verdict_for_run(comments, int(authoritative_run["id"]), attempt, head_sha)
                if verdict is not None:
                    if pending_action(verdict):
                        pending_actions.append(
                            {
                                "run_id": authoritative_run["id"],
                                "workflow": authoritative_run.get("name"),
                                "pr": pr_number,
                                "head_sha": head_sha,
                                "attempt": attempt,
                                "completed_at": authoritative_run.get("updated_at"),
                                "classification": verdict.get("classification") or "unknown",
                                "requested_owner_action": verdict.get("requested_owner_action") or "unknown",
                                "pm_action_packet": verdict.get("pm_action_packet"),
                                "pr_comment_url": verdict.get("pr_comment_url"),
                                "url": authoritative_run.get("html_url"),
                            }
                        )
                    continue
                if not pr_number and os.path.exists(
                    f"/tmp/ci-verdict-{authoritative_run['id']}-attempt-{attempt}.json"
                ):
                    continue
                triage = triage_run(api, int(authoritative_run["id"]), run_detail=authoritative_run)
            except Exception as error:  # noqa: BLE001 - one bad run must not hide sibling failures.
                errors.append(
                    {
                        "workflow": workflow,
                        "run_id": str(run.get("id") or "unknown"),
                        "error": str(error),
                    }
                )
                continue
            if not triage["actionable"]:
                continue
            overdue.append(
                {
                    "run_id": run["id"],
                    "workflow": authoritative_run.get("name"),
                    "pr": pr_number,
                    "head_sha": authoritative_run.get("head_sha"),
                    "attempt": attempt,
                    "completed_at": authoritative_run.get("updated_at"),
                    "category": triage["fingerprint"]["category"],
                    "local_repro_command": triage["fingerprint"]["local_repro_command"],
                    "url": run.get("html_url"),
                }
            )

    return {
        "schema_version": 2,
        "degraded": bool(errors),
        "errors": errors,
        "checked_failures": checked,
        "older_than_minutes": older_than_minutes,
        "lookback_hours": lookback_hours,
        "actionable_count": len(overdue) + len(pending_actions),
        "missing_verdict_count": len(overdue),
        "pending_action_count": len(pending_actions),
        "missing_verdicts": overdue,
        "pending_actions": pending_actions,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run-id", type=int)
    group.add_argument("--watchdog", action="store_true")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--token")
    parser.add_argument("--older-than-minutes", type=int, default=5)
    parser.add_argument("--lookback-hours", type=int, default=6)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        api = GitHubAPI(args.repo, resolve_token(args.token))
        if args.watchdog:
            result = watchdog(api, args.older_than_minutes, args.lookback_hours)
        else:
            result = triage_run(api, args.run_id)
        json.dump(result, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    except Exception as error:  # noqa: BLE001 - CLI must emit a stable degraded envelope.
        json.dump({"schema_version": 1, "degraded": True, "error": str(error)}, sys.stdout)
        sys.stdout.write("\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
