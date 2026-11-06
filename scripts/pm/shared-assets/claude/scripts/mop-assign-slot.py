#!/usr/bin/env python3
"""Assign one numbered slot atomically through Master of Panes.

This is the sanctioned assignment boundary.  It performs the whole operation
as one durable effect: fresh read, (new_issue only) session clear, the
ownership transition, the literal task delivery, and the dual readback.

Ordering is deliberate and never inverted: ownership is committed BEFORE the
task is delivered, because a pane acting on a task it does not own is strictly
worse than a recorded-but-undelivered slot.  A delivery failure therefore
leaves a NAMED recoverable state (occupied + task present + delivery pending)
and never rolls the ownership back implicitly.

Re-invoking with the same binding is idempotent: no second ownership commit,
no epoch bump, no second task text.  The durable effect id is derived from the
immutable binding, so a retry resumes the same effect rather than minting a
second assignment.

Thin surface (Rajiv directive 2026-09-23): the only required flags are
--slot and --issue.  Assigning an occupied slot implicitly releases it
first; the session clear is done by MoP as part of the assign. New operations
must pass the existing Ready Pool selector before any assignment effect;
known durable operations resume without re-admitting already committed work.
No caller opt-out is provided. No epochs, acks, expected
tuple ceremony, or retries for the caller.

stdout is exactly one JSON terminal:

  success  {"status":"assigned","slot":N,"assignment_epoch":E,
            "ownership_receipt":{...},"delivery_receipt":{...}}   exit 0
  refusal  {"status":"refused","step_failed":"selection|clean|ownership|delivery|readback",
            "reason":"<typed>","slot_state_after":"<state>",
            "sanctioned_path":"mop-assign-slot"}                  exit non-zero
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlencode


SELECTION_CLASSES = ("new_issue", "repro", "rework")
WORK_KINDS = ("implementation", "rework", "repro", "review")
SANCTIONED_PATH = "mop-assign-slot"
DEFAULT_REPOSITORY_ID = os.environ.get("MOP_PROJECTION_REPOSITORY", "Scribie/heydonna-app")


def mop_base_url() -> str:
    port = os.environ.get("MOP_PORT", "3100")
    return f"http://127.0.0.1:{port}"


def http_json(method: str, url: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    data = None
    headers = {"accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["content-type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=60) as response:  # noqa: S310 - fixed local MoP base URL
            raw = response.read().decode("utf-8")
            return response.status, (json.loads(raw) if raw.strip() else {})
    except HTTPError as error:
        raw = error.read().decode("utf-8", "replace")
        try:
            return error.code, json.loads(raw)
        except json.JSONDecodeError:
            return error.code, {"error": raw[:400]}
    except URLError as error:
        return 0, {"error": f"mop_unreachable: {error}"}


def emit(payload: dict[str, Any], exit_code: int) -> int:
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()
    return exit_code


def refusal(step_failed: str, reason: str, slot_state_after: str) -> dict[str, Any]:
    return {
        "status": "refused",
        "step_failed": step_failed,
        "reason": reason,
        "slot_state_after": slot_state_after,
        "sanctioned_path": SANCTIONED_PATH,
    }


def compute_effect_id(binding: dict[str, Any], task_digest: str) -> str:
    material = json.dumps({**binding, "task_digest": task_digest}, sort_keys=True, separators=(",", ":"))
    return "assign-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def slot_state_after(payload: Any) -> str:
    if isinstance(payload, dict) and isinstance(payload.get("slot_state_after"), str):
        return payload["slot_state_after"]
    if isinstance(payload, dict) and "occupied" in payload:
        return " ".join(
            [
                f"occupied={payload.get('occupied')}",
                f"issue={payload.get('issue')}",
                f"assignment_epoch={payload.get('assignment_epoch')}",
            ]
        )
    return "unknown"


def display_repository_id(repository_id: str) -> str:
    value = repository_id.removeprefix("github:")
    if value == "heydonna-app/heydonna-app":
        return DEFAULT_REPOSITORY_ID.removeprefix("github:")
    return value


def post_assignment_notice(
    repository_id: str, issue: int, slot: int, assignment_epoch: int, priority: str, task: str,
) -> dict[str, Any]:
    """Post status for the committed assignment; Slack uncertainty never retries or undoes it."""
    sender = os.environ.get("MOP_ASSIGN_SLACK_SEND", "/Users/rajiv/.claude/scripts/slack-send.sh")
    summary = next((line.strip() for line in task.splitlines() if line.strip()), "Assignment completed.")
    summary = summary.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    body = (
        f"Assignment: {priority} {display_repository_id(repository_id)} issue {issue} assigned to slot {slot} "
        f"(assignment epoch {assignment_epoch}).\n{summary}"
    )
    binding = {"repository_id": repository_id, "issue": issue, "slot": slot,
               "assignment_epoch": assignment_epoch}
    notice_key = "assignment-notice:v1:" + hashlib.sha256(
        json.dumps(binding, sort_keys=True).encode()).hexdigest()
    # Reuse the canonical event writer's unique dedupe key. Its durable insert
    # precedes any possible Slack effect, including concurrent fresh responses.
    try:
        reserved = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("pm-ops-legacy.py")), "record",
             "--source", "mop-assign-slot", "--actor", "pm", "--event", "assignment_notice_attempt",
             "--target-type", "assignment", "--target-id", notice_key,
             "--issue", str(issue), "--slot", str(slot), "--dedupe-key", notice_key,
             "--payload-json", json.dumps(binding, sort_keys=True), "--print-id"],
            capture_output=True, text=True, timeout=20, check=False,
        )
        event_id = int(reserved.stdout.strip()) if reserved.returncode == 0 else -1
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return {"posted": False, "delivery": "not_attempted", "reason": "notice_reservation_unavailable",
                "custody_key": notice_key, "native_receipt": {"error_type": type(error).__name__}}
    if event_id <= 0:
        return {"posted": False, "delivery": "not_retried" if event_id == 0 else "not_attempted",
                "reason": "notice_attempt_consumed" if event_id == 0 else "notice_reservation_failed",
                "custody_key": notice_key, "native_receipt": {"returncode": reserved.returncode,
                "stdout": reserved.stdout[-1000:], "stderr": reserved.stderr[-1000:]}}
    try:
        result = subprocess.run(
            [sender, "-c", "C0ALZJHGE49", "-f"],
            input=body,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            cwd=str(Path(sender).resolve().parent),
            env={**os.environ, "SLACK_SEND_AS": "pm"},
        )
    except OSError as error:
        return {"posted": False, "delivery": "not_attempted", "reason": "slack_publisher_unavailable",
                "custody_key": notice_key, "event_id": event_id,
                "native_receipt": {"error_type": type(error).__name__, "detail": str(error)}}
    except subprocess.SubprocessError as error:
        return {"posted": False, "delivery": "unknown", "reason": f"slack_send_failed:{type(error).__name__}",
                "custody_key": notice_key, "event_id": event_id,
                "native_receipt": {"error_type": type(error).__name__, "detail": str(error)}}
    native = {"custody_key": notice_key, "event_id": event_id, "returncode": result.returncode,
              "stdout": result.stdout[-1000:], "stderr": result.stderr[-1000:]}
    if result.returncode != 0:
        return {"posted": False, "delivery": "unknown", "reason": "slack_send_failed", "native_receipt": native}
    for line in result.stdout.splitlines():
        if line.startswith("OK ts="):
            receipt = dict(part.split("=", 1) for part in line.split()[1:] if "=" in part)
            thread_ts = receipt.get("ts")
            channel = receipt.get("channel")
            if thread_ts and re.fullmatch(r"[0-9]+\.[0-9]+", thread_ts) and channel == "C0ALZJHGE49":
                return {"posted": True, "delivery": "sent", "thread_ts": thread_ts, "channel": channel,
                        "native_receipt": native}
    return {"posted": False, "delivery": "unknown", "reason": "slack_receipt_missing", "native_receipt": native}


def check_selection(args: argparse.Namespace) -> dict[str, Any]:
    """Use the installed selector's decision for every NEW assignment."""
    command = [sys.executable, str(Path(__file__).with_name("ready_pool_select.py")),
               "--check", str(args.issue), "--selection-class", args.selection_class]
    if args.pr is not None:
        command += ["--pr", str(args.pr)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=90)
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ValueError("selector result is not an object")
        pick = payload.get("pick") or {}
        if not isinstance(pick, dict):
            raise ValueError("selector pick is not an object")
        if result.returncode == 0 and payload.get("status") == "ok" and pick.get("issue") == args.issue and pick.get("class") == args.selection_class:
            return payload
        if payload.get("status") == "ok":
            return {"status": "LOOKUP_FAILED", "reason": "selector_result_mismatch"}
        return payload
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return {"status": "LOOKUP_FAILED", "reason": "selection_check_unavailable"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assign one numbered slot atomically through MoP.")
    parser.add_argument("--slot", type=int, required=True)
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--class", dest="selection_class", choices=SELECTION_CLASSES, default="new_issue")
    parser.add_argument("--repo-id", dest="repo_id", default=DEFAULT_REPOSITORY_ID)
    parser.add_argument("--pr", type=int, default=None)
    parser.add_argument("--branch", default=None)
    parser.add_argument("--head", default=None)
    parser.add_argument("--work-kind", dest="work_kind", default=None)
    parser.add_argument("--handoff", default=None)
    parser.add_argument("--claimed-at", dest="claimed_at", default=None)
    parser.add_argument("--task-file", dest="task_file", default=None,
                        help="File holding the literal task message. Optional: without it only ownership is committed.")
    args = parser.parse_args(argv)

    if args.slot < 1:
        return emit(refusal("ownership", "invalid_slot", "not_applied"), 2)

    if args.work_kind is not None and args.work_kind not in WORK_KINDS:
        return emit(refusal(
            "ownership",
            f"invalid_work_kind:{args.work_kind} (valid: {'|'.join(WORK_KINDS)}; "
            "--class selects the path, --work-kind describes the lane and is never new_issue)",
            "not_applied",
        ), 2)

    task_bytes = b""
    task = ""
    if args.task_file:
        try:
            with open(args.task_file, "rb") as handle:
                task_bytes = handle.read()
        except OSError as error:
            return emit(refusal("ownership", f"task_file_unreadable:{error.errno}", "not_applied"), 2)
        task = task_bytes.decode("utf-8", "replace")

    base = mop_base_url()
    status, current = http_json("GET", f"{base}/slots/{args.slot}")
    if status != 200 or not isinstance(current, dict):
        return emit(refusal("ownership", "slot_read_failed", "unknown"), 2)

    desired_tuple = {
        "repository_id": args.repo_id,
        "issue": args.issue,
        "pr": args.pr,
        "branch": args.branch,
        "head_sha": args.head,
        "work_kind": args.work_kind,
        "handoff_id": args.handoff,
        "claimed_at": args.claimed_at,
    }
    binding = {
        "slot": args.slot,
        "selection_class": args.selection_class,
        **desired_tuple,
        "task_file": args.task_file or "",
    }
    task_digest = hashlib.sha256(task_bytes).hexdigest()
    effect_id = compute_effect_id(binding, task_digest)

    # Probe historical identities first; retain the exact original tuple when
    # resuming so the server's durable request digest and notice custody match.
    aliases = [args.repo_id]
    known = ("github:heydonna-app/heydonna-app", "heydonna-app/heydonna-app",
             "github:Scribie/heydonna-app", "Scribie/heydonna-app")
    if args.repo_id in known:
        aliases = list(known)
    existing = False
    for repo_id in aliases:
        candidate_id = compute_effect_id({**binding, "repository_id": repo_id}, task_digest)
        receipt_status, receipt = http_json(
            "GET", f"{base}/slots/{args.slot}/assignment-effect-receipt?" + urlencode({"effect_id": candidate_id}),
        )
        if receipt_status == 404 and isinstance(receipt, dict) and receipt.get("code") == "assignment_effect_receipt_not_found":
            continue
        existing = (receipt_status == 200 and isinstance(receipt, dict)
                    and receipt.get("success") is True and receipt.get("effect_id") == candidate_id
                    and receipt.get("slot") == args.slot and receipt.get("selection_class") == args.selection_class
                    and receipt.get("state") in {"planned", "pending_delivery", "delivered"}
                    and receipt.get("task_digest") == task_digest
                    and isinstance(receipt.get("desired_tuple"), dict)
                    and (receipt.get("desired_tuple") or {}).get("issue") == args.issue)
        if not existing:
            return emit(refusal("selection", "assignment_receipt_unavailable", slot_state_after(current)), 2)
        effect_id = candidate_id
        desired_tuple["repository_id"] = repo_id
        break
    if existing:
        selection = {"status": "existing_effect_resume", "effect_id": effect_id}
    else:
        selection = check_selection(args)
        if selection.get("status") != "ok":
            rejected = refusal("selection", str(selection.get("status") or "LOOKUP_FAILED"), slot_state_after(current))
            rejected["selection_result"] = selection
            return emit(rejected, 2)

    body = {
        "effect_id": effect_id,
        "selection_class": args.selection_class,
        "expected_epoch": current.get("assignment_epoch"),
        **desired_tuple,
        "task": task,
        "task_file": args.task_file or "",
    }

    status, payload = http_json("POST", f"{base}/slots/{args.slot}/assign-effect", body)
    if not isinstance(payload, dict):
        return emit(refusal("ownership", "assignment_effect_response_unreadable", "unknown"), 2)
    if status == 200 and payload.get("status") == "assigned":
        terminal = {**payload, "selection_receipt": selection}
        if args.selection_class == "new_issue":
            # The returned committed assignment-effect receipt is the one-use
            # custody: only idempotent=false may publish. Any resume (including
            # an earlier UNKNOWN Slack outcome) records no-retry and never sends.
            pick = selection.get("pick") if isinstance(selection, dict) else None
            priority = pick.get("priority") if isinstance(pick, dict) else None
            epoch = payload.get("assignment_epoch")
            if payload.get("idempotent") is True:
                terminal["assignment_notice"] = {
                    "posted": False, "delivery": "not_retried", "reason": "assignment_effect_resume",
                }
            elif payload.get("idempotent") is not False:
                terminal["assignment_notice"] = {
                    "posted": False, "delivery": "unknown", "reason": "assignment_effect_custody_missing",
                }
            elif selection.get("status") != "ok":
                terminal["assignment_notice"] = {
                    "posted": False, "delivery": "not_attempted", "reason": "fresh_selection_receipt_missing",
                }
            elif (payload.get("idempotent") is not False
                  or not isinstance(priority, str) or not priority.strip()
                  or type(epoch) is not int or type(current.get("assignment_epoch")) is not int
                  or epoch <= current["assignment_epoch"]):
                terminal["assignment_notice"] = {
                    "posted": False, "delivery": "not_attempted", "reason": "committed_assignment_receipt_incomplete",
                }
            else:
                terminal["assignment_notice"] = post_assignment_notice(
                    (payload.get("ownership_receipt") or {}).get("repository_id") or args.repo_id,
                    args.issue, args.slot, epoch, priority, task,
                )
        return emit(terminal, 0)
    if payload.get("status") == "refused":
        return emit(payload, 2)
    return emit(refusal("ownership", str(payload.get("reason") or payload.get("error") or "assignment_effect_failed"), slot_state_after(payload)), 2)


if __name__ == "__main__":
    raise SystemExit(main())
