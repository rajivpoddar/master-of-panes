#!/usr/bin/env python3
"""Finalize one already-merged HeyDonna PR and its linked issue.

This is a direct, journaled PM caller.  It never owns MoP slots.  Every
GitHub or Slack effect is reserved before execution and a started effect is
permanently ambiguous on an uncertain outcome, so replay cannot duplicate a
write or thread reply.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener


SLACK_API = "https://slack.com/api"
SLACK_CHANNEL = "C0ALZJHGE49"
CTO_USER_ID = "U0BNFGX2UAX"
CTO_MENTION = f"<@{CTO_USER_ID}>"
DEFAULT_MAPPING = Path.home() / ".claude" / "mop" / "pm-transition-parent-receipts.json"
DEFAULT_RECEIPT = Path.home() / ".claude" / "mop" / "pm-cleanup-receipts.json"
HEAD_RE = r"[0-9a-fA-F]{40}"
STALE_LABEL_PREFIXES = (
    "ci-", "ci:", "cleanup-", "cleanup:", "pm-cleanup:",
    "slot:", "status:", "pm-state:", "pm-blocked:",
)
LINKED_ISSUE_MODE = "linked_issue"
ISSUE_LESS_MODE = "merged_pr_issue_less"


class CleanupError(Exception):
    def __init__(self, reason: str, *, ambiguous: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.ambiguous = ambiguous


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CleanupError("cleanup_receipt_unreadable", ambiguous=True) from exc


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path.with_name(f".{path.name}.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with os.fdopen(fd, "r+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield
            fcntl.flock(stream, fcntl.LOCK_UN)
    finally:
        pass


def _required_string(value: Any, reason: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CleanupError(reason)
    return value.strip()


def _validate_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CleanupError("cleanup_request_invalid")
    repository = _required_string(value.get("repository"), "repository_invalid")
    if "/" not in repository or repository.startswith("/") or repository.endswith("/"):
        raise CleanupError("repository_invalid")
    cleanup_mode = value.get("cleanup_mode", LINKED_ISSUE_MODE)
    if cleanup_mode not in {LINKED_ISSUE_MODE, ISSUE_LESS_MODE}:
        raise CleanupError("cleanup_mode_invalid")
    try:
        pr = int(value["pr"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CleanupError("pr_or_issue_invalid") from exc
    if pr <= 0:
        raise CleanupError("pr_or_issue_invalid")
    if cleanup_mode == ISSUE_LESS_MODE:
        if value.get("issue") is not None or value.get("thread_ts") is not None:
            raise CleanupError("issue_less_request_shape_invalid")
        issue = None
    else:
        try:
            issue = int(value["issue"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CleanupError("pr_or_issue_invalid") from exc
        if issue <= 0:
            raise CleanupError("pr_or_issue_invalid")
    head = _required_string(value.get("head"), "head_invalid")
    merge_commit = _required_string(value.get("merge_commit"), "merge_commit_invalid")
    import re
    if re.fullmatch(HEAD_RE, head) is None or re.fullmatch(HEAD_RE, merge_commit) is None:
        raise CleanupError("head_or_merge_commit_invalid")
    caller_thread_ts = value.get("thread_ts")
    if caller_thread_ts is not None:
        caller_thread_ts = _required_string(caller_thread_ts, "thread_mapping_invalid")
        if "\n" in caller_thread_ts or "\r" in caller_thread_ts:
            raise CleanupError("thread_mapping_invalid")
    return {
        "repository": repository,
        "pr": pr,
        "issue": issue,
        "cleanup_mode": cleanup_mode,
        "head": head.lower(),
        "merge_commit": merge_commit.lower(),
        "_caller_thread_ts": caller_thread_ts,
    }


def _labels(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise CleanupError("github_labels_invalid")
    if all(isinstance(item, str) and item for item in value):
        names = value
    elif all(isinstance(item, dict) for item in value):
        names = []
        for item in value:
            name = item.get("name")
            if not isinstance(name, str) or not name:
                raise CleanupError("github_labels_invalid")
            names.append(name)
    else:
        raise CleanupError("github_labels_invalid")
    return list(dict.fromkeys(names))


def _is_stale_label(label: str) -> bool:
    return label.startswith(STALE_LABEL_PREFIXES)


def _mapping_matches(mapping: dict[str, Any], request: dict[str, Any]) -> bool:
    repo = mapping.get("repository_id", mapping.get("repository"))
    return (
        repo == request["repository"]
        and mapping.get("issue") == request["issue"]
        and mapping.get("pr") == request["pr"]
        and str(mapping.get("head_sha", mapping.get("head", ""))).lower() == request["head"]
        and isinstance(mapping.get("thread_ts"), str)
        and mapping.get("status") in {"parent_created", "thread_replied"}
    )


def resolve_thread_mapping(path: Path, request: dict[str, Any]) -> dict[str, Any]:
    raw = _load_json(path, {})
    if not isinstance(raw, dict):
        raise CleanupError("thread_mapping_invalid")
    matches = [value for value in raw.values() if isinstance(value, dict) and _mapping_matches(value, request)]
    if len(matches) != 1:
        raise CleanupError("thread_mapping_missing_or_ambiguous")
    return matches[0]


class External:
    """Small boundary adapter; tests replace this object, not the caller."""

    def _gh(self, args: list[str], *, input_bytes: bytes | None = None) -> Any:
        try:
            result = subprocess.run(
                ["gh", *args], input=input_bytes, capture_output=True, check=False, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CleanupError("github_effect_ambiguous", ambiguous=True) from exc
        if result.returncode != 0:
            raise CleanupError("github_effect_ambiguous", ambiguous=True)
        try:
            return json.loads(result.stdout.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise CleanupError("github_response_ambiguous", ambiguous=True) from exc

    def read_pr(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._gh(["pr", "view", str(request["pr"]), "--repo", request["repository"], "--json", "number,state,mergedAt,mergeCommit,headRefOid,closingIssuesReferences,labels"])

    def read_issue(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._gh(["issue", "view", str(request["issue"]), "--repo", request["repository"], "--json", "number,state,stateReason,labels"])

    def add_pr_label(self, request: dict[str, Any], label: str) -> None:
        self._gh([
            "api", f"repos/{request['repository']}/issues/{request['pr']}/labels",
            "--method", "POST", "--input", "-",
        ], input_bytes=_json_bytes({"labels": [label]}))

    def remove_pr_label(self, request: dict[str, Any], label: str) -> None:
        self._gh([
            "api", f"repos/{request['repository']}/issues/{request['pr']}/labels/{quote(label, safe='')}",
            "--method", "DELETE",
        ])

    def add_issue_label(self, request: dict[str, Any], label: str) -> None:
        self._gh([
            "api", f"repos/{request['repository']}/issues/{request['issue']}/labels",
            "--method", "POST", "--input", "-",
        ], input_bytes=_json_bytes({"labels": [label]}))

    def remove_issue_label(self, request: dict[str, Any], label: str) -> None:
        self._gh([
            "api", f"repos/{request['repository']}/issues/{request['issue']}/labels/{quote(label, safe='')}",
            "--method", "DELETE",
        ])

    def close_issue(self, request: dict[str, Any]) -> None:
        self._gh([
            "api", f"repos/{request['repository']}/issues/{request['issue']}",
            "--method", "PATCH", "-f", "state=closed", "-f", "state_reason=completed",
        ])

    def _slack(self, method: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = _json_bytes(payload)
        request = Request(
            f"{SLACK_API}/{method}", data=body,
            headers={"content-type": "application/json", "authorization": f"Bearer {token}"},
            method="POST",
        )
        try:
            with build_opener(_NoRedirect).open(request, timeout=15) as response:
                value = json.loads(response.read().decode("utf-8"))
        except (HTTPError, OSError, URLError, TimeoutError, UnicodeError, json.JSONDecodeError) as exc:
            raise CleanupError("slack_effect_ambiguous", ambiguous=True) from exc
        if not isinstance(value, dict):
            raise CleanupError("slack_response_ambiguous", ambiguous=True)
        return value

    def slack_auth(self, token: str) -> dict[str, Any]:
        return self._slack("auth.test", token, {})

    def slack_replies(self, token: str, thread_ts: str) -> dict[str, Any]:
        return self._slack("conversations.replies", token, {"channel": SLACK_CHANNEL, "ts": thread_ts, "limit": 100})

    def slack_post(self, token: str, thread_ts: str, text: str) -> dict[str, Any]:
        return self._slack("chat.postMessage", token, {"channel": SLACK_CHANNEL, "thread_ts": thread_ts, "text": text})


def _validate_merged_snapshot(request: dict[str, Any], pr: dict[str, Any]) -> list[str]:
    if pr.get("number") != request["pr"] or str(pr.get("state", "")).upper() != "MERGED":
        raise CleanupError("pr_not_merged")
    merge = pr.get("mergeCommit") or {}
    if not isinstance(merge, dict) or str(merge.get("oid", "")).lower() != request["merge_commit"]:
        raise CleanupError("merge_commit_mismatch")
    if str(pr.get("headRefOid", "")).lower() != request["head"]:
        raise CleanupError("pr_head_mismatch")
    refs = pr.get("closingIssuesReferences")
    if not isinstance(refs, list) or not any(isinstance(item, dict) and item.get("number") == request["issue"] for item in refs):
        raise CleanupError("linked_issue_mismatch")
    return _labels(pr.get("labels"))


def _validate_issue_less_snapshot(request: dict[str, Any], pr: dict[str, Any]) -> list[str]:
    if pr.get("number") != request["pr"] or str(pr.get("state", "")).upper() != "MERGED":
        raise CleanupError("pr_not_merged")
    merge = pr.get("mergeCommit") or {}
    if not isinstance(merge, dict) or str(merge.get("oid", "")).lower() != request["merge_commit"]:
        raise CleanupError("merge_commit_mismatch")
    if str(pr.get("headRefOid", "")).lower() != request["head"]:
        raise CleanupError("pr_head_mismatch")
    refs = pr.get("closingIssuesReferences")
    if not isinstance(refs, list):
        raise CleanupError("linked_issue_readback_invalid")
    if refs:
        raise CleanupError("issue_less_linked_issue_present")
    return _labels(pr.get("labels"))


def _validate_issue_snapshot(request: dict[str, Any], issue: dict[str, Any]) -> list[str]:
    if issue.get("number") != request["issue"]:
        raise CleanupError("issue_mismatch")
    return _labels(issue.get("labels"))


def _validate_auth_and_thread(external: External, token: str, mapping: dict[str, Any], request: dict[str, Any], text: str) -> None:
    if not token:
        raise CleanupError("slack_cto_token_missing")
    auth = external.slack_auth(token)
    if auth.get("ok") is not True or auth.get("user_id") != CTO_USER_ID:
        raise CleanupError("slack_identity_mismatch")
    thread_ts = _required_string(mapping.get("thread_ts"), "thread_mapping_invalid")
    if "\n" in thread_ts or "\r" in thread_ts:
        raise CleanupError("thread_mapping_invalid")
    if request.get("_caller_thread_ts") not in (None, thread_ts):
        raise CleanupError("caller_thread_mapping_mismatch")
    readback = external.slack_replies(token, thread_ts)
    if readback.get("ok") is not True:
        raise CleanupError("transition_thread_readback_failed")
    messages = readback.get("messages")
    if not isinstance(messages, list) or not any(
        isinstance(item, dict)
        and item.get("ts") == thread_ts
        and item.get("user") == CTO_USER_ID
        and item.get("channel", SLACK_CHANNEL) == SLACK_CHANNEL
        for item in messages
    ):
        raise CleanupError("transition_thread_identity_mismatch")
    if not text:
        raise CleanupError("transition_thread_mapping_invalid")


def _transition_text(request: dict[str, Any]) -> str:
    if request["cleanup_mode"] == ISSUE_LESS_MODE:
        return (
            f"PR #{request['pr']} merged-clean | no linked closing issue "
            f"| head={request['head']} | merge_commit={request['merge_commit']} "
            f"| state=closed-clean"
        )
    return (
        f"PR #{request['pr']} merged-clean | issue #{request['issue']} CLOSED/COMPLETED "
        f"| head={request['head']} | merge_commit={request['merge_commit']} "
        f"| state=closed-clean | {CTO_MENTION}"
    )


def _cleanup_identity(request: dict[str, Any]) -> dict[str, Any]:
    return {
        key: request[key]
        for key in ("repository", "pr", "issue", "head", "merge_commit")
    }


def _cleanup_key(request: dict[str, Any], thread_ts: str | None = None) -> str:
    identity = _cleanup_identity(request)
    if thread_ts is not None:
        identity["thread_ts"] = thread_ts
    return "mop-cleanup:" + hashlib.sha256(_json_bytes(identity)).hexdigest()


def _label_plan(scope: str, labels: list[str], terminal: str) -> list[dict[str, str]]:
    plan: list[dict[str, str]] = []
    for label in sorted(labels):
        if _is_stale_label(label) and label != terminal:
            plan.append({
                "name": f"{scope}_label_remove:{label}",
                "scope": scope,
                "operation": "remove",
                "label": label,
            })
    if terminal not in labels:
        plan.append({
            "name": f"{scope}_label_add:{terminal}",
            "scope": scope,
            "operation": "add",
            "label": terminal,
        })
    return plan


def run(
    raw_request: Any,
    *,
    mapping_path: Path | None = None,
    receipt_path: Path | None = None,
    external: External | None = None,
    cto_slack_token: str | None = None,
) -> dict[str, Any]:
    supplied = _validate_request(raw_request)
    caller_thread_ts = supplied.get("_caller_thread_ts")
    request = _cleanup_identity(supplied)
    request["cleanup_mode"] = supplied["cleanup_mode"]
    if caller_thread_ts is not None:
        request["_caller_thread_ts"] = caller_thread_ts
    mapping_file = mapping_path or Path(os.environ.get("MOP_TRANSITION_RECEIPT_PATH", str(DEFAULT_MAPPING))).expanduser()
    receipt_file = receipt_path or Path(os.environ.get("MOP_CLEANUP_RECEIPT_PATH", str(DEFAULT_RECEIPT))).expanduser()
    ext = external or External()
    with _locked(receipt_file):
        receipts = _load_json(receipt_file, {})
        if not isinstance(receipts, dict):
            raise CleanupError("cleanup_receipt_invalid", ambiguous=True)
        key = _cleanup_key(request)
        prior = receipts.get(key)
        mapping = None
        thread_ts = None
        if not isinstance(prior, dict) and supplied["cleanup_mode"] != ISSUE_LESS_MODE:
            mapping = resolve_thread_mapping(mapping_file, request)
            thread_ts = _required_string(mapping.get("thread_ts"), "thread_mapping_invalid")
            if caller_thread_ts not in (None, thread_ts):
                raise CleanupError("caller_thread_mapping_mismatch")
            legacy_key = _cleanup_key(request, thread_ts)
            legacy_prior = receipts.get(legacy_key)
            if isinstance(legacy_prior, dict):
                key = legacy_key
                prior = legacy_prior
        if isinstance(prior, dict):
            prior_mode = prior.get("cleanup_mode", LINKED_ISSUE_MODE)
            if prior_mode != supplied["cleanup_mode"]:
                raise CleanupError("cleanup_receipt_mode_mismatch", ambiguous=True)
            stored_thread_ts = prior.get("thread_ts")
            if supplied["cleanup_mode"] == ISSUE_LESS_MODE:
                if stored_thread_ts is not None:
                    raise CleanupError("cleanup_receipt_thread_mismatch", ambiguous=True)
            else:
                stored_thread_ts = _required_string(stored_thread_ts, "thread_mapping_invalid")
                if caller_thread_ts not in (None, stored_thread_ts):
                    raise CleanupError("caller_thread_mapping_mismatch")
            prior_request = prior.get("request")
            if not isinstance(prior_request, dict) or any(
                prior_request.get(field) != request[field]
                for field in ("repository", "pr", "issue", "head", "merge_commit")
            ):
                raise CleanupError("cleanup_receipt_request_mismatch", ambiguous=True)
            if prior.get("status") == "completed":
                return {
                    "success": True, "status": "completed", "idempotent": True,
                    "cleanup_key": key, "thread_ts": stored_thread_ts,
                }

        token = cto_slack_token or os.environ.get("SLACK_CTO_BOT_TOKEN", "")
        if isinstance(prior, dict):
            receipt = prior
            thread_ts = stored_thread_ts
            text = _transition_text(request)
            plan = receipt.get("plan")
            if not isinstance(plan, list):
                raise CleanupError("cleanup_receipt_plan_missing", ambiguous=True)
            if not isinstance(receipt.get("steps"), dict):
                raise CleanupError("cleanup_receipt_steps_invalid", ambiguous=True)
            if not isinstance(receipt.get("ambiguous_steps"), list):
                raise CleanupError("cleanup_receipt_ambiguity_invalid", ambiguous=True)
        else:
            text = _transition_text(request)
            if supplied["cleanup_mode"] == ISSUE_LESS_MODE:
                pr_labels = _validate_issue_less_snapshot(request, ext.read_pr(request))
                plan = _label_plan("pr", pr_labels, "pm-state:closed-clean")
            else:
                assert mapping is not None
                assert thread_ts is not None
                pr_labels = _validate_merged_snapshot(request, ext.read_pr(request))
                issue_labels = _validate_issue_snapshot(request, ext.read_issue(request))
                _validate_auth_and_thread(ext, token, mapping, request, text)
                plan = (
                    _label_plan("pr", pr_labels, "pm-state:closed-clean")
                    + _label_plan("issue", issue_labels, "status:done")
                    + [
                        {"name": "issue_close", "scope": "issue", "operation": "close", "label": ""},
                        {"name": "thread_reply", "scope": "slack", "operation": "reply", "label": ""},
                    ]
                )
            receipt = {
                "status": "prepared",
                "request": _cleanup_identity(request),
                "cleanup_mode": supplied["cleanup_mode"],
                "thread_ts": thread_ts,
                "payload_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "plan": plan,
                "steps": {},
                "ambiguous_steps": [],
            }
            receipts[key] = receipt
            _write_json(receipt_file, receipts)

        def persist() -> None:
            receipts[key] = receipt
            _write_json(receipt_file, receipts)

        def label_readback(scope: str, operation: str, label: str) -> bool:
            if scope == "pr":
                if request["cleanup_mode"] == ISSUE_LESS_MODE:
                    labels = _validate_issue_less_snapshot(request, ext.read_pr(request))
                else:
                    labels = _validate_merged_snapshot(request, ext.read_pr(request))
            else:
                labels = _validate_issue_snapshot(request, ext.read_issue(request))
            return (label in labels) if operation == "add" else (label not in labels)

        def run_step(step_spec: dict[str, str]) -> None:
            name = step_spec["name"]
            steps = receipt["steps"]
            if name == "thread_reply":
                close_step = steps.get("issue_close")
                if not isinstance(close_step, dict) or close_step.get("status") != "completed":
                    return
            step = steps.get(name)
            if isinstance(step, dict) and step.get("status") == "completed":
                return
            if isinstance(step, dict) and step.get("status") in {"effect_started", "ambiguous"}:
                receipt["status"] = "ambiguous"
                ambiguous_steps = receipt.setdefault("ambiguous_steps", [])
                if name not in ambiguous_steps:
                    ambiguous_steps.append(name)
                persist()
                return

            receipt["status"] = "processing"
            receipt["steps"][name] = {"status": "reserved"}
            persist()
            receipt["steps"][name] = {"status": "effect_started"}
            persist()
            try:
                scope = step_spec["scope"]
                operation = step_spec["operation"]
                label = step_spec["label"]
                if scope == "pr" and operation == "add":
                    ext.add_pr_label(request, label)
                    verified = label_readback(scope, operation, label)
                elif scope == "pr" and operation == "remove":
                    ext.remove_pr_label(request, label)
                    verified = label_readback(scope, operation, label)
                elif scope == "issue" and operation == "add":
                    ext.add_issue_label(request, label)
                    verified = label_readback(scope, operation, label)
                elif scope == "issue" and operation == "remove":
                    ext.remove_issue_label(request, label)
                    verified = label_readback(scope, operation, label)
                elif name == "issue_close":
                    ext.close_issue(request)
                    issue_readback = ext.read_issue(request)
                    verified = (
                        str(issue_readback.get("state", "")).upper() == "CLOSED"
                        and str(issue_readback.get("stateReason", "")).upper() == "COMPLETED"
                    )
                elif name == "thread_reply":
                    ext.slack_post(token, thread_ts, text)
                    verified = _thread_reply_readback(ext, token, thread_ts, text)
                else:
                    raise CleanupError(f"cleanup_unknown_step:{name}")
                if not verified:
                    raise CleanupError(f"{name}_readback_mismatch", ambiguous=True)
            except CleanupError as exc:
                receipt["status"] = "ambiguous"
                receipt["steps"][name] = {"status": "ambiguous", "error": exc.reason}
                ambiguous_steps = receipt.setdefault("ambiguous_steps", [])
                if name not in ambiguous_steps:
                    ambiguous_steps.append(name)
                persist()
                return
            receipt["steps"][name] = {"status": "completed"}
            persist()

        for step_spec in plan:
            if not isinstance(step_spec, dict) or not all(key in step_spec for key in ("name", "scope", "operation", "label")):
                raise CleanupError("cleanup_receipt_plan_invalid", ambiguous=True)
            run_step(step_spec)

        ambiguous_steps = receipt.get("ambiguous_steps") or []
        if ambiguous_steps:
            receipt["status"] = "ambiguous"
            persist()
            raise CleanupError("cleanup_ambiguous", ambiguous=True)
        receipt["status"] = "completed"
        persist()
        return {
            "success": True, "status": "completed", "idempotent": False,
            "cleanup_key": key, "thread_ts": thread_ts,
        }


def _thread_reply_readback(external: External, token: str, thread_ts: str, text: str) -> bool:
    value = external.slack_replies(token, thread_ts)
    if value.get("ok") is not True or not isinstance(value.get("messages"), list):
        return False
    return any(
        isinstance(item, dict)
        and item.get("user") == CTO_USER_ID
        and item.get("thread_ts", thread_ts) == thread_ts
        and item.get("text") == text
        for item in value["messages"]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Finalize one merged PR and linked issue")
    parser.add_argument("--mapping-path", type=Path)
    parser.add_argument("--receipt-path", type=Path)
    args = parser.parse_args(argv)
    try:
        result = run(json.load(__import__("sys").stdin), mapping_path=args.mapping_path, receipt_path=args.receipt_path)
    except CleanupError as exc:
        print(json.dumps({"success": False, "error": exc.reason, "ambiguous": exc.ambiguous}, separators=(",", ":")))
        return 2
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
