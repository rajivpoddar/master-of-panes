#!/usr/bin/env python3
"""One-shot authenticated direct-MoP free-session replacement client."""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request


DEFAULT_BASE_URL = "http://127.0.0.1:3100"
AUTHORITY_HEADER = "x-heydonna-assignment-authority"


def request_json(url: str, method: str, body: dict[str, object] | None, authority: str) -> tuple[int, dict[str, object]]:
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    request.add_header(AUTHORITY_HEADER, authority)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {"success": False, "code": "mop_http_error"}
        return error.code, payload
    except (OSError, json.JSONDecodeError) as error:
        return 599, {"success": False, "code": "mop_response_unreadable", "reason": str(error)}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--slot", type=int, required=True)
    result.add_argument("--expected-epoch", type=int, required=True)
    result.add_argument("--expected-session-id", required=True)
    result.add_argument("--expected-session-started-at", required=True)
    result.add_argument("--expected-age-seconds", type=float, required=True)
    result.add_argument("--checkout-path", required=True)
    result.add_argument("--checkout-branch", required=True)
    result.add_argument("--checkout-head", required=True)
    result.add_argument("--request-token", required=True)
    result.add_argument("--base-url", default=os.environ.get("MOP_BASE_URL", DEFAULT_BASE_URL))
    result.add_argument("--authority", default=os.environ.get("MOP_ASSIGNMENT_AUTHORITY", "pm-transition-v1"))
    result.add_argument("--dry-run", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    if args.slot < 1 or args.slot > 6 or args.expected_age_seconds <= 6 * 60 * 60:
        print(json.dumps({"success": False, "code": "invalid_session_clear_request"}, sort_keys=True))
        return 2
    base = args.base_url.rstrip("/")
    status, observed = request_json(f"{base}/slots/{args.slot}", "GET", None, args.authority)
    if status != 200:
        print(json.dumps(observed, sort_keys=True))
        return 2
    if any(observed.get(key) != value for key, value in {
        "assignment_epoch": args.expected_epoch,
        "session_id": args.expected_session_id,
        "session_started_at": args.expected_session_started_at,
    }.items()):
        print(json.dumps({"success": False, "code": "session_state_drift"}, sort_keys=True))
        return 2
    if observed.get("occupied") is not False or observed.get("dnd") is not False or observed.get("idle") is not True:
        print(json.dumps({"success": False, "code": "session_not_free_inactive"}, sort_keys=True))
        return 2
    if observed.get("active_turn_id") is not None or observed.get("active_turn_state") != "inactive":
        print(json.dumps({"success": False, "code": "active_turn"}, sort_keys=True))
        return 2
    body = {
        "expected_epoch": args.expected_epoch,
        "expected_session_id": args.expected_session_id,
        "expected_session_started_at": args.expected_session_started_at,
        "expected_age_seconds": args.expected_age_seconds,
        "checkout_path": args.checkout_path,
        "checkout_branch": args.checkout_branch,
        "checkout_head": args.checkout_head,
        "checkout_clean": True,
        "unpushed_commits": [],
        "request_token": args.request_token,
    }
    if args.dry_run:
        print(json.dumps({"success": True, "effect": False, "observed": observed}, sort_keys=True))
        return 0
    response_status, response = request_json(f"{base}/slots/{args.slot}/session/clear", "POST", body, args.authority)
    print(json.dumps(response, sort_keys=True))
    return 0 if response_status == 200 and response.get("success") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
