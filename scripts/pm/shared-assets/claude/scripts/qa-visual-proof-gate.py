#!/usr/bin/env python3
"""Gate UI PRs on exact-head, AC-scoped visual QA proof.

The issue contract decides whether screenshots are required. A changed-file
classifier decides only whether this gate must inspect that contract. Proof is
accepted from GitHub Actions artifacts, GitHub attachments, HTTPS R2 objects,
or a hash-bound local PNG under /tmp for same-host promotion.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def _find_repo_root(anchor: Path) -> Path | None:
    for candidate in (anchor, *anchor.parents):
        if (candidate / "scripts" / "ci" / "change_scope.py").is_file():
            return candidate
    return None


def resolve_repo_root() -> Path:
    """Locate the repository root in the source or the installed layout.

    deploy-control-plane.sh installs this gate to ``<runtime>/.claude/scripts``
    while the repository checkout keeps the classifier in ``scripts/``. A
    fixed source-depth parent chain breaks after relocation, so walk up from
    the script and from the working directory until the repository marker is
    found, and fail closed otherwise.
    """
    for anchor in (Path(__file__).resolve(), Path.cwd().resolve()):
        root = _find_repo_root(anchor)
        if root is not None:
            return root
    raise RuntimeError(
        "cannot resolve repository root: scripts/ci/change_scope.py not found "
        "above the gate or the working directory"
    )


REPO_ROOT = resolve_repo_root()
MARKER = re.compile(r"<!--\s*qa-visual-proof:\s*(\{.*?\})\s*-->", re.I | re.S)
SHA256 = re.compile(r"[0-9a-f]{64}")
ALLOWED_ARTIFACT_KINDS = {
    "actions_artifact",
    "github_attachment",
    "local_tmp",
    "r2",
}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_PNG_BYTES = 25 * 1024 * 1024
MAX_ACTIONS_ARCHIVE_BYTES = 100 * 1024 * 1024


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(command: list[str], *, timeout: int = 60) -> str:
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError((completed.stderr or completed.stdout or "command failed").strip())
    return completed.stdout


def download(command: list[str], target: Path, *, timeout: int = 90) -> None:
    with target.open("wb") as output:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            stdout=output,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    if completed.returncode:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(message or "artifact download failed")


def validate_png_bytes(data: bytes) -> str | None:
    if len(data) > MAX_PNG_BYTES:
        return "png_too_large"
    if not data.startswith(PNG_SIGNATURE):
        return "not_png"
    offset = len(PNG_SIGNATURE)
    first_chunk = True
    saw_iend = False
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        chunk_type = data[offset + 4 : offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > len(data):
            return "malformed_png"
        chunk_data = data[offset + 8 : offset + 8 + length]
        expected_crc = int.from_bytes(data[offset + 8 + length : chunk_end], "big")
        actual_crc = zlib.crc32(chunk_type)
        actual_crc = zlib.crc32(chunk_data, actual_crc) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            return "invalid_png_crc"
        if first_chunk and (chunk_type != b"IHDR" or length != 13):
            return "invalid_png_header"
        first_chunk = False
        offset = chunk_end
        if chunk_type == b"IEND":
            if length != 0 or offset != len(data):
                return "invalid_png_end"
            saw_iend = True
            break
    if not saw_iend:
        return "missing_png_end"
    return None


def body_sha256(body: str) -> str:
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()


def resolve_validator_path() -> Path:
    """Locate the issue-contract-ledger validator in any install layout."""
    override = os.environ.get("HEYDONNA_ISSUE_CONTRACT_LEDGER_VALIDATOR")
    candidates = [
        Path(override) if override else None,
        Path(__file__).resolve().parent / "validate-issue-contract-ledger.py",
        REPO_ROOT / "scripts/pm/validate-issue-contract-ledger.py",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate
    raise RuntimeError(
        "cannot resolve issue-contract-ledger validator: expected "
        "scripts/pm/validate-issue-contract-ledger.py in the "
        "repository or validate-issue-contract-ledger.py beside the gate"
    )


def proof_contract(body: str) -> tuple[list[dict[str, Any]], list[str]]:
    # Issue Contract Ledger and QA-proof fields were introduced after a set of
    # existing UI issues had already been authored.  Those legacy bodies do
    # not declare deterministic visual acceptance criteria, so they cannot
    # produce screenshot IDs for this gate.  Keep the compatibility narrow:
    # only the unmistakable pre-ledger ``What to Build``/``Why`` shape is
    # accepted, while any body that declares the current QA schema or an
    # Acceptance criteria section remains subject to the strict ledger
    # validator below.
    if legacy_issue_contract_compatible(body):
        return [], []
    validator = load_module(
        "heydonna_issue_contract_ledger", resolve_validator_path()
    )
    errors = list(validator.validate(body, require_qa_proof=True))
    criteria = list(validator.parse_ac_proof_contract(body))
    return criteria, errors


def legacy_issue_contract_compatible(body: str) -> bool:
    """Recognize the bounded pre-ledger issue shape without guessing proof.

    Legacy issues are admitted only when they have the old prose headings and
    contain no current QA/AC markers.  Explicit visual-proof language still
    requires the current schema so a deterministic visual contract cannot
    bypass exact-head screenshot evidence.
    """
    text = body or ""
    if (
        re.search(r"(?i)<!--\s*qa-proof-schema\s*:\s*1\s*-->", text)
        or re.search(r"(?im)^##\s+Issue Contract Ledger\s*$", text)
        or re.search(r"(?im)^##\s+(?:Acceptance criteria|Acceptance criterion|ACs?)\s*$", text)
    ):
        return False
    if re.search(r"(?i)\b(?:screenshot|visual\s+(?:qa|proof)|playwright)\b", text):
        return False
    headings = {
        re.sub(r"\s+", " ", match.group(1).strip().lower())
        for match in re.finditer(r"(?im)^##\s+(.+?)\s*$", text)
    }
    return "what to build" in headings and "why" in headings


BRANCH_ISSUE_RE = re.compile(
    r"^(?:.*/)?(?:(?:fix|feat|feature|bug|test|chore|perf|refactor|enhance)/)?"
    r"(?P<issue>[0-9]{3,6})(?:[-_/].*)?$"
)
PROSE_ISSUE_RE = re.compile(r"#([0-9]+)")


def resolve_pr_issue_from_metadata(pr: dict[str, Any]) -> int:
    """Resolve exactly one implementation issue from read-only PR metadata.

    The retired resolver was a repository dependency.  The PR payload already
    contains the authoritative closing-issue references; branch/title/body are
    deterministic fallbacks for older PRs.  Ambiguous or absent ownership is
    refused rather than guessed.
    """
    closing: set[int] = set()
    for ref in pr.get("closingIssuesReferences") or []:
        if isinstance(ref, dict) and isinstance(ref.get("number"), int) and ref["number"] > 0:
            closing.add(int(ref["number"]))
    if len(closing) == 1:
        return next(iter(closing))
    if len(closing) > 1:
        raise RuntimeError("ambiguous implementation issue: multiple closing issue references")

    branch = pr.get("headRefName")
    if isinstance(branch, str):
        match = BRANCH_ISSUE_RE.fullmatch(branch.strip())
        if match:
            return int(match.group("issue"))

    text = f"{pr.get('title') or ''}\n{pr.get('body') or ''}"
    prose = {int(number) for number in PROSE_ISSUE_RE.findall(text)}
    if len(prose) == 1:
        return next(iter(prose))
    if len(prose) > 1:
        raise RuntimeError("ambiguous implementation issue: multiple issue references in PR metadata")
    raise RuntimeError("cannot resolve implementation issue from read-only PR metadata")


EXPECTED_CHANGE_SCOPE_RULES_SHA256 = "bb572ca70c1c464267b92732a69cfca762c151ca05a5a7340da76a2c75191834"
ALLOWED_CHANGE_SCOPE_SCOPES = {
    "control_plane_only",
    "mixed",
    "editor_product",
    "product",
}


def validate_change_scope(scope: Any, *, expected_head: str) -> dict[str, Any]:
    """Validate the co-deployed exact-head classifier before any issue lookup.

    The classifier is an authority boundary, not a hint. A missing, stale,
    malformed, empty, or rules-drifted result cannot grant the non-UI
    exemption. The rules digest is pinned to the authoritative app-origin
    classifier release that supplies this gate.
    """
    if not isinstance(scope, dict):
        raise RuntimeError("change_scope returned a non-object result")
    if scope.get("schema_version") != 1:
        raise RuntimeError("change_scope schema is unsupported")
    if not re.fullmatch(r"[0-9a-f]{40}", expected_head):
        raise RuntimeError("live PR head is malformed")
    observed_head = scope.get("head")
    if not isinstance(observed_head, str) or observed_head != expected_head:
        raise RuntimeError(
            f"change_scope head mismatch expected={expected_head} "
            f"actual={observed_head or 'missing'}"
        )
    if scope.get("rules_sha256") != EXPECTED_CHANGE_SCOPE_RULES_SHA256:
        raise RuntimeError("change_scope rules digest mismatch")
    changed_files = scope.get("changed_files")
    if (
        not isinstance(changed_files, list)
        or not changed_files
        or any(not isinstance(path, str) or not path.strip() for path in changed_files)
        or len(set(changed_files)) != len(changed_files)
    ):
        raise RuntimeError("change_scope diff is empty or malformed")
    if scope.get("scope") not in ALLOWED_CHANGE_SCOPE_SCOPES:
        raise RuntimeError("change_scope scope is unknown")
    for key in ("ui_changed", "product_changed", "control_plane_only"):
        if not isinstance(scope.get(key), bool):
            raise RuntimeError(f"change_scope {key} is malformed")
    ownership = scope.get("ownership")
    if not isinstance(ownership, dict) or set(ownership) != set(changed_files):
        raise RuntimeError("change_scope ownership is missing or malformed")
    return scope


def required_screenshot_ac_ids(criteria: list[dict[str, Any]]) -> list[str]:
    required: list[str] = []
    for criterion in criteria:
        fields = criterion.get("fields") or {}
        surface = str(fields.get("surface") or "").strip().lower()
        reachability = str(fields.get("qa_reachability") or "").strip().lower()
        proofs = {
            item.strip().lower().replace(" ", "-")
            for item in re.split(
                r"\s*(?:,|\+|\band\b)\s*",
                str(fields.get("required_proof") or ""),
                flags=re.I,
            )
            if item.strip()
        }
        if surface == "visual" and reachability == "deterministic" and "screenshot" in proofs:
            required.append(str(criterion.get("id") or "").upper())
    return sorted(set(required))


def extract_receipts(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    for comment in comments:
        for match in MARKER.finditer(str(comment.get("body") or "")):
            try:
                receipt = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            if isinstance(receipt, dict):
                receipt["_comment_created_at"] = str(comment.get("createdAt") or "")
                receipt["_comment_url"] = str(comment.get("url") or "")
                receipts.append(receipt)
    return sorted(receipts, key=lambda item: item.get("_comment_created_at") or "", reverse=True)


def receipt_matches_tuple(
    receipt: dict[str, Any], *, pr: int, issue: int, head: str, issue_body_sha: str
) -> bool:
    return (
        str(receipt.get("pr") or "") == str(pr)
        and str(receipt.get("issue") or "") == str(issue)
        and str(receipt.get("head_sha") or "") == head
        and str(receipt.get("issue_body_sha256") or "") == issue_body_sha
    )


def local_tmp_path(url: str) -> Path | None:
    parsed = urlparse(url)
    if parsed.scheme == "file":
        if parsed.netloc or parsed.query or parsed.fragment:
            return None
        raw_path = parsed.path
    elif not parsed.scheme:
        raw_path = url
    else:
        return None
    candidate = Path(raw_path)
    if not candidate.is_absolute() or candidate.suffix.lower() != ".png":
        return None
    resolved = candidate.resolve(strict=False)
    allowed_roots = {Path("/tmp").resolve(), Path("/private/tmp").resolve()}
    if not any(resolved == root or root in resolved.parents for root in allowed_roots):
        return None
    return candidate


def validate_artifact_url(scenario: dict[str, Any]) -> list[str]:
    ac_id = str(scenario.get("ac_id") or "").upper() or "unknown"
    kind = str(scenario.get("artifact_kind") or "").strip().lower()
    url = str(scenario.get("artifact_url") or "").strip()
    errors: list[str] = []
    if kind not in ALLOWED_ARTIFACT_KINDS:
        errors.append(f"invalid_artifact_kind:{ac_id}:{kind or 'missing'}")
    parsed = urlparse(url)
    if kind == "local_tmp":
        if local_tmp_path(url) is None:
            errors.append(f"invalid_local_tmp_artifact_url:{ac_id}")
    else:
        if parsed.scheme != "https" or not parsed.netloc:
            errors.append(f"non_durable_artifact_url:{ac_id}")
        if url.startswith(("/tmp", "file:", "data:")) or "/tmp/" in url:
            errors.append(f"local_artifact_url:{ac_id}")
    if kind == "github_attachment" and parsed.netloc not in {
        "github.com",
        "user-images.githubusercontent.com",
    }:
        errors.append(f"invalid_github_attachment_host:{ac_id}")
    if (
        kind == "github_attachment"
        and parsed.netloc == "github.com"
        and not parsed.path.startswith("/user-attachments/assets/")
    ):
        errors.append(f"invalid_github_attachment_path:{ac_id}")
    if kind == "actions_artifact":
        artifact_id = str(scenario.get("artifact_id") or "")
        artifact_member = str(scenario.get("artifact_member") or "").strip()
        if not artifact_id.isdigit():
            errors.append(f"missing_actions_artifact_id:{ac_id}")
        if (
            not artifact_member
            or not artifact_member.lower().endswith(".png")
            or artifact_member.startswith(("/", "\\"))
            or ".." in Path(artifact_member).parts
        ):
            errors.append(f"invalid_actions_artifact_member:{ac_id}")
        if parsed.netloc not in {"github.com", "api.github.com"}:
            errors.append(f"invalid_actions_artifact_host:{ac_id}")
        if artifact_id and artifact_id not in parsed.path:
            errors.append(f"actions_artifact_url_mismatch:{ac_id}")
    if kind == "r2":
        extra_hosts = {
            host.strip().lower()
            for host in os.environ.get("QA_VISUAL_R2_HOSTS", "").split(",")
            if host.strip()
        }
        r2_host = parsed.netloc.lower()
        if not (
            r2_host.endswith(".r2.dev")
            or r2_host.endswith(".r2.cloudflarestorage.com")
            or r2_host in extra_hosts
        ):
            errors.append(f"invalid_r2_artifact_host:{ac_id}")
    if not SHA256.fullmatch(str(scenario.get("sha256") or "").lower()):
        errors.append(f"invalid_screenshot_sha256:{ac_id}")
    if not str(scenario.get("viewport") or "").strip():
        errors.append(f"missing_viewport:{ac_id}")
    if not str(scenario.get("state") or "").strip():
        errors.append(f"missing_state:{ac_id}")
    if not str(scenario.get("captured_at") or "").strip():
        errors.append(f"missing_captured_at:{ac_id}")
    return errors


def verify_remote_artifact(
    scenario: dict[str, Any], *, repo: str, head: str
) -> list[str]:
    ac_id = str(scenario.get("ac_id") or "").upper() or "unknown"
    kind = str(scenario.get("artifact_kind") or "").lower()
    url = str(scenario.get("artifact_url") or "")
    declared_sha = str(scenario.get("sha256") or "").lower()
    try:
        with tempfile.TemporaryDirectory(prefix="heydonna-qa-proof-") as temp_dir:
            temp = Path(temp_dir)
            if kind == "local_tmp":
                artifact = local_tmp_path(url)
                if artifact is None:
                    return [f"invalid_local_tmp_artifact_url:{ac_id}"]
                resolved = artifact.resolve(strict=True)
                allowed_roots = {Path("/tmp").resolve(), Path("/private/tmp").resolve()}
                if not any(
                    resolved == root or root in resolved.parents
                    for root in allowed_roots
                ):
                    return [f"local_tmp_artifact_path_escape:{ac_id}"]
                if resolved.stat().st_size > MAX_PNG_BYTES:
                    return [f"png_too_large:{ac_id}"]
                screenshot = resolved.read_bytes()
            elif kind == "actions_artifact":
                artifact_id = str(scenario.get("artifact_id") or "")
                artifact = json.loads(run(["gh", "api", f"repos/{repo}/actions/artifacts/{artifact_id}"]))
                if artifact.get("expired") is True:
                    return [f"actions_artifact_expired:{ac_id}"]
                run_head = str((artifact.get("workflow_run") or {}).get("head_sha") or "")
                if not run_head or run_head != head:
                    return [f"actions_artifact_head_mismatch:{ac_id}"]
                archive = temp / "artifact.zip"
                download(
                    ["gh", "api", f"repos/{repo}/actions/artifacts/{artifact_id}/zip"],
                    archive,
                )
                if archive.stat().st_size > MAX_ACTIONS_ARCHIVE_BYTES:
                    return [f"actions_artifact_too_large:{ac_id}"]
                member = str(scenario.get("artifact_member") or "")
                with zipfile.ZipFile(archive) as zipped:
                    try:
                        member_info = zipped.getinfo(member)
                    except KeyError:
                        return [f"actions_artifact_member_missing:{ac_id}"]
                    if member_info.file_size > MAX_PNG_BYTES:
                        return [f"png_too_large:{ac_id}"]
                    screenshot = zipped.read(member_info)
            else:
                artifact = temp / "screenshot.png"
                download(
                    [
                        "curl", "--fail", "--location", "--silent", "--show-error",
                        "--max-filesize", str(MAX_PNG_BYTES + 1), "--output", str(artifact), url,
                    ],
                    temp / "curl.stdout",
                )
                screenshot = artifact.read_bytes()
    except (RuntimeError, json.JSONDecodeError, OSError, subprocess.TimeoutExpired, zipfile.BadZipFile) as exc:
        return [f"artifact_unavailable:{ac_id}:{type(exc).__name__}"]
    png_error = validate_png_bytes(screenshot)
    if png_error:
        return [f"{png_error}:{ac_id}"]
    if hashlib.sha256(screenshot).hexdigest() != declared_sha:
        return [f"screenshot_sha256_mismatch:{ac_id}"]
    return []


def validate_receipt(
    receipt: dict[str, Any],
    *,
    pr: int,
    issue: int,
    head: str,
    issue_body_sha: str,
    required_ac_ids: list[str],
    repo: str,
    verify_remote: bool,
) -> list[str]:
    errors: list[str] = []
    if receipt.get("schema") != "heydonna_qa_visual_proof" or receipt.get("version") != 1:
        errors.append("invalid_receipt_schema")
    if str(receipt.get("verdict") or "").lower() != "pass":
        errors.append("receipt_verdict_not_pass")
    if str(receipt.get("pr") or "") != str(pr):
        errors.append("receipt_pr_mismatch")
    if str(receipt.get("issue") or "") != str(issue):
        errors.append("receipt_issue_mismatch")
    if str(receipt.get("head_sha") or "") != head:
        errors.append("receipt_head_mismatch")
    if str(receipt.get("issue_body_sha256") or "") != issue_body_sha:
        errors.append("receipt_issue_body_mismatch")

    scenarios = receipt.get("scenarios")
    if not isinstance(scenarios, list):
        return [*errors, "receipt_scenarios_missing"]
    by_id: dict[str, dict[str, Any]] = {}
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            errors.append("invalid_receipt_scenario")
            continue
        ac_id = str(scenario.get("ac_id") or "").upper()
        if ac_id in by_id:
            errors.append(f"duplicate_receipt_scenario:{ac_id or 'missing'}")
        by_id[ac_id] = scenario
        errors.extend(validate_artifact_url(scenario))
    for ac_id in required_ac_ids:
        if ac_id not in by_id:
            errors.append(f"missing_screenshot_proof:{ac_id}")
        elif verify_remote:
            errors.extend(verify_remote_artifact(by_id[ac_id], repo=repo, head=head))
    return errors


def evaluate(
    *,
    pr: int,
    issue: int,
    head: str,
    issue_body: str,
    ui_changed: bool,
    comments: list[dict[str, Any]],
    repo: str,
    verify_remote: bool = True,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": "heydonna_qa_visual_proof_gate",
        "version": 1,
        "pr": pr,
        "issue": issue,
        "head_sha": head,
        "issue_body_sha256": body_sha256(issue_body),
        "ui_changed": ui_changed,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    if not ui_changed:
        return {**result, "ok": True, "status": "pass", "reason": "non_ui_change", "required_ac_ids": []}

    criteria, contract_errors = proof_contract(issue_body)
    required_ac_ids = required_screenshot_ac_ids(criteria)
    result["required_ac_ids"] = required_ac_ids
    if contract_errors:
        return {
            **result,
            "ok": False,
            "status": "blocked",
            "reason": "invalid_issue_qa_proof_contract",
            "errors": contract_errors,
        }
    if not required_ac_ids:
        reason = (
            "legacy_issue_contract_accepted"
            if legacy_issue_contract_compatible(issue_body)
            else "screenshots_not_required_by_ac"
        )
        return {**result, "ok": True, "status": "pass", "reason": reason}

    receipts = extract_receipts(comments)
    if not receipts:
        return {
            **result,
            "ok": False,
            "status": "blocked",
            "reason": "durable_screenshot_receipt_missing",
            "errors": [f"missing_screenshot_proof:{ac_id}" for ac_id in required_ac_ids],
        }
    # Comments are append-only; an out-of-order stale retry can land after the
    # current-tuple proof. Restrict selection to receipts bound to the current
    # PR/issue/head/body tuple (extract_receipts is newest-first), so a newer
    # stale-head receipt cannot shadow the still-valid current-tuple receipt.
    # When no receipt matches the current tuple, fall back to the newest
    # receipt so its typed mismatch errors surface unchanged (fail closed).
    current_tuple_receipts = [
        receipt
        for receipt in receipts
        if receipt_matches_tuple(
            receipt,
            pr=pr,
            issue=issue,
            head=head,
            issue_body_sha=result["issue_body_sha256"],
        )
    ]
    candidate = current_tuple_receipts[0] if current_tuple_receipts else receipts[0]
    errors = validate_receipt(
        candidate,
        pr=pr,
        issue=issue,
        head=head,
        issue_body_sha=result["issue_body_sha256"],
        required_ac_ids=required_ac_ids,
        repo=repo,
        verify_remote=verify_remote,
    )
    if errors:
        return {
            **result,
            "ok": False,
            "status": "blocked",
            "reason": "durable_screenshot_receipt_invalid",
            "errors": errors,
            "receipt_comment_url": candidate.get("_comment_url") or "",
        }
    return {
        **result,
        "ok": True,
        "status": "pass",
        "reason": "durable_screenshot_receipt_valid",
        "receipt_comment_url": candidate.get("_comment_url") or "",
    }


def live_evaluate(args: argparse.Namespace) -> dict[str, Any]:
    pr_data = json.loads(
        run(
            [
                "gh", "pr", "view", str(args.pr), "--repo", args.repo, "--json",
                "number,title,body,state,headRefOid,headRefName,closingIssuesReferences,comments",
            ]
        )
    )
    head = str(pr_data.get("headRefOid") or "")
    if args.expect_head and head != args.expect_head:
        raise RuntimeError(f"head mismatch expected={args.expect_head} live={head or 'unknown'}")
    scope = json.loads(
        run(
            [
                "python3", str(REPO_ROOT / "scripts/ci/change_scope.py"),
                "--repo-root", str(REPO_ROOT), "--repo", args.repo,
                "--pr", str(args.pr), "--expected-head", head,
            ],
            timeout=90,
        )
    )
    scope = validate_change_scope(scope, expected_head=head)
    ui_changed = scope["ui_changed"]
    if ui_changed:
        issue = resolve_pr_issue_from_metadata(pr_data)
        issue_data = json.loads(
            run(["gh", "issue", "view", str(issue), "--repo", args.repo, "--json", "number,body,state"])
        )
        result = evaluate(
            pr=int(args.pr), issue=int(issue), head=head,
            issue_body=str(issue_data.get("body") or ""),
            ui_changed=True,
            comments=list(pr_data.get("comments") or []), repo=args.repo,
            verify_remote=not args.skip_artifact_availability,
        )
    else:
        result = evaluate(
            pr=int(args.pr), issue=0, head=head,
            issue_body="", ui_changed=False,
            comments=list(pr_data.get("comments") or []), repo=args.repo,
            verify_remote=not args.skip_artifact_availability,
        )
    result["change_scope"] = scope
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--repo", default="heydonna-app/heydonna-app")
    parser.add_argument("--expect-head", default="")
    parser.add_argument("--skip-artifact-availability", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = live_evaluate(args)
    except Exception as exc:  # fail closed with a typed receipt
        result = {
            "schema": "heydonna_qa_visual_proof_gate", "version": 1,
            "pr": int(args.pr), "ok": False, "status": "blocked",
            "reason": "gate_error", "errors": [str(exc)],
        }
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            f"QA_VISUAL_PROOF: {'PASS' if result.get('ok') else 'BLOCKED'} "
            f"pr={args.pr} reason={result.get('reason')}"
        )
        for error in result.get("errors") or []:
            print(f"- {error}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
