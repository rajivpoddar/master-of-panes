#!/usr/bin/env python3
"""Integrate main and request CI/E2E once using Git and GitHub directly."""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

REPO = "heydonna-app/heydonna-app"
WORKFLOWS = ("ci.yml", "e2e.yml")


class Refusal(RuntimeError):
    pass


DEFAULT_COMMAND_TIMEOUT = 120
DEFAULT_FETCH_TIMEOUT = 300
FETCH_TIMEOUT_ENV = "ADMIT_FETCH_TIMEOUT_S"
DEFAULT_TARGETED_FETCH_TIMEOUT = 60
TARGETED_FETCH_TIMEOUT_ENV = "ADMIT_TARGETED_FETCH_TIMEOUT_S"
REMOTE_VERIFY_TIMEOUT = 60


def command(*args, cwd=None, data=None, timeout=DEFAULT_COMMAND_TIMEOUT):
    result = subprocess.run(args, cwd=cwd, input=data, text=True,
                            capture_output=True, timeout=timeout)
    if result.returncode:
        raise Refusal(result.stderr.strip() or result.stdout.strip() or "command failed")
    return result.stdout


def api(path, payload=None):
    args = ["gh", "api", f"repos/{REPO}/{path}"]
    if payload is not None:
        args += ["--method", "POST", "--input", "-"]
    return json.loads(command(*args, data=json.dumps(payload) if payload is not None else None))


def pr_at(number, head):
    pr = api(f"pulls/{number}")
    if pr["head"]["sha"] != head:
        raise Refusal("HEAD_CHANGED")
    if pr["state"] != "open" or pr["draft"]:
        raise Refusal("PR_NOT_OPEN_NON_DRAFT")
    if pr["base"]["ref"] != "main" or (pr["head"].get("repo") or {}).get("full_name") != REPO:
        raise Refusal("PR_MUST_TARGET_MAIN_IN_SAME_REPOSITORY")
    return pr


def existing(pr):
    head = pr["head"]["sha"]
    result = {}
    if f"ci-head:{head}" in {label["name"] for label in pr["labels"]}:
        result["label"] = f"ci-head:{head}"
    unresolved_runs = []
    completed_runs = []
    for workflow in WORKFLOWS:
        pages = json.loads(command("gh", "api", "--paginate", "--slurp",
            f"repos/{REPO}/actions/workflows/{workflow}/runs?event=pull_request&head_sha={head}&per_page=100"))
        for run in [run for page in pages for run in page["workflow_runs"]]:
            if run["head_sha"] != head or run["event"] != "pull_request" or run["head_branch"] != pr["head"]["ref"]:
                continue
            # Unrelated label events skip scope; an unresolved run must not be duplicated.
            jobs = api(f"actions/runs/{run['id']}/jobs?filter=latest&per_page=100")["jobs"]
            scope = next((job for job in jobs if job["name"] == "classify-change-scope"), None)
            if scope is None or scope.get("conclusion") != "skipped":
                # A run is only "active" while GitHub still reports an open lifecycle.
                # Confirmed-completed runs (status "completed", any conclusion) are
                # historical: they must not block a behind-main head's required
                # current-main integration, yet remain reported so same-head
                # idempotency is not globally discarded.
                if run.get("status") == "completed":
                    completed_runs.append({"id": run["id"],
                                         "status": run.get("status"),
                                         "conclusion": run.get("conclusion")})
                else:
                    unresolved_runs.append(run["id"])
    if unresolved_runs:
        result["runs"] = unresolved_runs
    if completed_runs:
        result["completed_runs"] = completed_runs
    return result or None


def _same_repo_objects_dir():
    """Absolute objects dir of the repo we run inside, or None.
    Reusing these objects is read-only and safe: git never writes into an alternate,
    and object integrity is still verified by SHA on fetch. Present only to shrink the
    integration fetch payload; when absent we fall back to the plain network fetch."""
    try:
        cwd = os.getcwd()
        common = subprocess.run(["git", "rev-parse", "--git-common-dir"],
                                capture_output=True, text=True, timeout=15)
        if common.returncode != 0:
            return None
        rel = common.stdout.strip()
        if not rel:
            return None
        git_dir = rel if os.path.isabs(rel) else os.path.normpath(os.path.join(cwd, rel))
        git_dir = os.path.realpath(git_dir)
        return os.path.join(git_dir, "objects") if os.path.isdir(os.path.join(git_dir, "objects")) else None
    except (subprocess.SubprocessError, OSError):
        return None


def _fetch_timeout():
    """Bounded fetch timeout in seconds: env override or conservative default.
    The 120s command default timed out twice fetching both SHA closures on
    this repository, so integration fetches get a larger but still finite
    fail-closed bound. Garbage/zero/negative values fall back to the default."""
    try:
        value = int(os.environ.get(FETCH_TIMEOUT_ENV, DEFAULT_FETCH_TIMEOUT))
    except (TypeError, ValueError):
        return DEFAULT_FETCH_TIMEOUT
    return value if value > 0 else DEFAULT_FETCH_TIMEOUT


def _targeted_fetch_timeout(bound):
    """First-attempt network fetch bound: env override or conservative default,
    never larger than the overall fetch bound. Garbage/zero/negative values
    fall back to the default. A timeout here does not refuse yet; the caller
    tries the verified-local escape, then resumes the network fetch."""
    try:
        value = int(os.environ.get(TARGETED_FETCH_TIMEOUT_ENV, DEFAULT_TARGETED_FETCH_TIMEOUT))
    except (TypeError, ValueError):
        value = DEFAULT_TARGETED_FETCH_TIMEOUT
    if value <= 0:
        value = DEFAULT_TARGETED_FETCH_TIMEOUT
    return min(value, bound)


def _is_partial_clone():
    """True when the current checkout is a partial clone (missing blobs).
    A partial-clone object store advertises commits but not file blobs, so
    reusing it as an alternate lets fetch/checkout succeed and then kills the
    merge on unable-to-read-sha1 (PR #7644). Fall back to a plain network
    fetch, which retrieves the complete closure."""
    for key in ("remote.origin.partialclonefilter", "extensions.partialClone"):
        try:
            result = subprocess.run(["git", "config", "--get", key],
                                    capture_output=True, text=True, timeout=15)
        except (subprocess.SubprocessError, OSError):
            continue
        if result.returncode == 0 and result.stdout.strip():
            return True
    return False


def _ascii_reprible(path):
    """The alternates file is written with encoding=ascii. A source path the
    current encoding cannot represent is ineligible for reuse so we take the
    plain network fetch instead of raising mid-write. Deciding before any
    alternates file is written leaves no partial file behind the opt-out."""
    try:
        path.encode("ascii")
        return True
    except (UnicodeEncodeError, TypeError):
        return False


def _reuseable_alternate(objects, head, main):
    """True only when the candidate store already holds both SHAs, so the integration
    fetch reuses them and transfers only the (small) delta. When either is absent, or the
    store is unreadable, the path cannot be written as ASCII alternates, or the
    store belongs to a partial clone with missing blobs, we fall back to a
    plain network fetch and preserve behavior."""
    if objects is None or not _ascii_reprible(objects):
        return None
    if _is_partial_clone():
        return None
    try:
        head_ok = subprocess.run(["git", "-C", objects, "cat-file", "-e", head],
                                 capture_output=True, text=True, timeout=15).returncode == 0
        main_ok = subprocess.run(["git", "-C", objects, "cat-file", "-e", main],
                                 capture_output=True, text=True, timeout=15).returncode == 0
    except subprocess.SubprocessError:
        return None
    return objects if (head_ok and main_ok) else None


def _local_objects_ready(objects, head, main):
    """Enclosing git dir when the local store provably holds both commits, else None.
    Partial clones are excluded (missing blobs would only fail downstream), so a
    partial-clone store falls through to the resumed network fetch. Commit
    presence only; downstream checkout/merge stays fail-closed on any gap."""
    if objects is None or _is_partial_clone():
        return None
    git_dir = os.path.dirname(objects)
    try:
        for sha in (head, main):
            result = subprocess.run(["git", "--git-dir", git_dir, "cat-file", "-e", sha + "^{commit}"],
                                    capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                return None
    except (subprocess.SubprocessError, OSError):
        return None
    return git_dir


def _remote_oid(checkout, ref):
    """Authoritative remote OID for one fully-qualified ref via ls-remote.
    Bounded; zero/multiple lines, ref mismatch, or malformed OID all refuse as
    verification uncertainty, never as evidence."""
    try:
        out = command("git", "ls-remote", "origin", ref, cwd=checkout, timeout=REMOTE_VERIFY_TIMEOUT)
    except (Refusal, OSError, subprocess.TimeoutExpired) as exc:
        raise Refusal(f"REMOTE_VERIFICATION_UNCERTAIN ref={ref}: {exc}") from exc
    lines = [line for line in out.splitlines() if line.strip()]
    if len(lines) != 1:
        raise Refusal(f"REMOTE_VERIFICATION_UNCERTAIN ref={ref}: ambiguous ls-remote")
    parts = lines[0].split()
    if len(parts) != 2 or parts[1] != ref or not re.fullmatch(r"[0-9a-f]{40}", parts[0]):
        raise Refusal(f"REMOTE_VERIFICATION_UNCERTAIN ref={ref}: malformed ls-remote")
    return parts[0]


def _verified_local_fetch(checkout, head, main, branch, git_dir, bound):
    """Timeout fallback: fetch both SHAs from the proven-local store, but only
    after authoritative remote-OID verification shows neither ref drifted.
    Missing objects, remote drift, or verification uncertainty all refuse;
    the push still goes to the network remote under the descendant-only lease."""
    remote_main = _remote_oid(checkout, "refs/heads/main")
    if remote_main != main:
        raise Refusal(f"REMOTE_MAIN_DRIFT expected={main} remote={remote_main}")
    remote_head = _remote_oid(checkout, f"refs/heads/{branch}")
    if remote_head != head:
        raise Refusal(f"REMOTE_HEAD_DRIFT expected={head} remote={remote_head}")
    print("[admit] network fetch timed out; remote OIDs verified, objects local; fetching from local store",
          file=sys.stderr)
    return command("git", "fetch", "--quiet", git_dir, head, main, cwd=checkout, timeout=bound)


def _write_alternates(checkout, source):
    info = os.path.join(checkout, ".git", "objects", "info")
    os.makedirs(info, exist_ok=True)
    with open(os.path.join(info, "alternates"), "w", encoding="ascii") as handle:
        handle.write(source + "\n")


def _clear_alternates(checkout):
    try:
        os.remove(os.path.join(checkout, ".git", "objects", "info", "alternates"))
    except OSError:
        pass


def _fetch_head_and_main(checkout, head, main, branch):
    """Fetch head and main, reusing this checkout's objects when they hold both SHAs
    with complete blobs. Otherwise try a bounded targeted network fetch first; when
    that times out yet the exact objects are already local, verify the authoritative
    remote OIDs and fetch from the local store instead of spending the full bound.
    With no local objects the plain network fetch resumes with the remaining bound,
    so the pre-existing success envelope is preserved. Non-timeout fetch failures
    propagate unchanged; the bounded-refusal and documented manual fallback hold."""
    bound = _fetch_timeout()
    source = _reuseable_alternate(_same_repo_objects_dir(), head, main)
    if source is not None:
        try:
            _write_alternates(checkout, source)
            return command("git", "fetch", "--quiet", "origin", head, main,
                           cwd=checkout, timeout=bound)
        except (Refusal, OSError) as exc:
            _clear_alternates(checkout)
            print(f"[admit] local object reuse failed ({exc}); direct fetch", file=sys.stderr)
    targeted = _targeted_fetch_timeout(bound)
    try:
        return command("git", "fetch", "--quiet", "origin", head, main,
                       cwd=checkout, timeout=targeted)
    except subprocess.TimeoutExpired:
        print("[admit] targeted network fetch timed out; trying verified-local escape",
              file=sys.stderr)
    git_dir = _local_objects_ready(_same_repo_objects_dir(), head, main)
    if git_dir is not None:
        return _verified_local_fetch(checkout, head, main, branch, git_dir, bound)
    remaining = bound - targeted
    if remaining <= 0:
        raise Refusal(f"FETCH_TIMEOUT_EXCEEDED head={head} main={main}")
    print("[admit] no verified local objects; resuming network fetch", file=sys.stderr)
    return command("git", "fetch", "--quiet", "origin", head, main,
                   cwd=checkout, timeout=remaining)


def integrate(pr, main):
    head, branch = pr["head"]["sha"], pr["head"]["ref"]
    with tempfile.TemporaryDirectory(prefix="heydonna-admission-") as checkout:
        command("git", "init", "--quiet", checkout)
        command("git", "remote", "add", "origin", f"https://github.com/{REPO}.git", cwd=checkout)
        _fetch_head_and_main(checkout, head, main, branch)
        command("git", "checkout", "--quiet", "--detach", head, cwd=checkout)
        command("git", "-c", "user.name=Abhijit CTO", "-c", "user.email=abhijit-cto@users.noreply.github.com",
                "merge", "--no-edit", main, cwd=checkout)
        new_head = command("git", "rev-parse", "HEAD", cwd=checkout).strip()
        command("git", "merge-base", "--is-ancestor", head, new_head, cwd=checkout)
        pr_at(pr["number"], head)
        try:
            command("git", "push", f"--force-with-lease=refs/heads/{branch}:{head}",
                    "origin", f"HEAD:refs/heads/{branch}", cwd=checkout)
        except (Refusal, subprocess.TimeoutExpired) as exc:
            raise Refusal(f"PUSH_UNCERTAIN expected_head={new_head}: {exc}") from exc
    return new_head


def admit(number, head, apply=False, integrate_only=False):
    pr = pr_at(number, head)
    main = api("git/ref/heads/main")["object"]["sha"]
    comparison = api(f"compare/{main}...{head}")
    behind = comparison["merge_base_commit"]["sha"] != main
    prior = existing(pr)
    if behind:
        # Only genuine unresolved active runs protect a behind head. A stale
        # ci-head label with no active runs must not block the required
        # current-main integration, so defer the duplicate check to the
        # post-integration re-check.
        if prior and prior.get("runs"):
            return {"status": "ALREADY_ADMITTED", "head": head, **prior}
    elif prior:
        # Head already contains current main: honor any admission (label or
        # active runs) as-is, exactly as the installed preimage did.
        return {"status": "ALREADY_ADMITTED", "head": head, **prior}
    if not apply:
        return {"status": "MAIN_INTEGRATION_REQUIRED" if behind else "READY_TO_ADMIT",
                "head": head, "main": main}
    if behind:
        head = integrate(pr, main)
    if integrate_only:
        return {"status": "MAIN_INTEGRATED", "head": head, "main": main}
    pr = pr_at(number, head)
    if api("git/ref/heads/main")["object"]["sha"] != main:
        raise Refusal(f"MAIN_MOVED_BEFORE_ADMISSION head={head}")
    prior = existing(pr)
    if prior:
        return {"status": "ALREADY_ADMITTED", "head": head, **prior}
    label = f"ci-head:{head}"
    command("gh", "label", "create", label, "--repo", REPO, "--color", "0e8a16",
            "--description", "CI and E2E for this PR head", "--force")
    pr_at(number, head)
    try:
        # Adding a label preserves unrelated labels and never toggles an existing request.
        labels = api(f"issues/{number}/labels", {"labels": [label]})
        stored = api(f"pulls/{number}")
        if label not in {item["name"] for item in labels} or stored["head"]["sha"] != head:
            raise Refusal("LABEL_OR_HEAD_READBACK_MISMATCH")
    except (Refusal, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise Refusal(f"ADMISSION_UNCERTAIN head={head}: {exc}") from exc
    return {"status": "ADMISSION_REQUESTED", "pr": number, "head": head,
            "main": main, "label": label, "jobs_started": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--head", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--integrate-only", action="store_true")
    args = parser.parse_args()
    if args.pr <= 0 or not re.fullmatch(r"[0-9a-f]{40}", args.head):
        parser.error("positive PR number and full lowercase head SHA required")
    try:
        print(json.dumps(admit(args.pr, args.head, args.apply, args.integrate_only), sort_keys=True))
        return 0
    except (Refusal, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "REFUSED", "reason": str(exc)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
