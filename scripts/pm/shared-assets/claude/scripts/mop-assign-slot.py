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
first; the session clear is done by MoP as part of the assign.  The ONLY
refusal is a lane already bound to another occupied slot
(`duplicate_assignment`, naming that slot).  No epochs, acks, expected
tuple ceremony, or retries for the caller.

stdout is exactly one JSON terminal:

  success  {"status":"assigned","slot":N,"assignment_epoch":E,
            "ownership_receipt":{...},"delivery_receipt":{...}}   exit 0
  refusal  {"status":"refused","step_failed":"clean|ownership|delivery|readback",
            "reason":"<typed>","slot_state_after":"<state>",
            "sanctioned_path":"mop-assign-slot"}                  exit non-zero
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SELECTION_CLASSES = ("new_issue", "repro", "rework")
SANCTIONED_PATH = "mop-assign-slot"


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assign one numbered slot atomically through MoP.")
    parser.add_argument("--slot", type=int, required=True)
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--class", dest="selection_class", choices=SELECTION_CLASSES, default="new_issue")
    parser.add_argument("--repo-id", dest="repo_id", default="github:heydonna-app/heydonna-app")
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
        return emit(payload, 0)
    if payload.get("status") == "refused":
        return emit(payload, 2)
    return emit(refusal("ownership", str(payload.get("reason") or payload.get("error") or "assignment_effect_failed"), slot_state_after(payload)), 2)


if __name__ == "__main__":
    raise SystemExit(main())
