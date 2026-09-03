#!/usr/bin/env python3
"""One-shot, authenticated MoP session-age client.

The default mode is a read-only exact-tuple dry run. A real clear is only
attempted with --execute against the explicit session-clear route; this client
never falls back to the broad /clear or pane-send endpoints.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_BASE_URL = "http://127.0.0.1:3100"
DEFAULT_AUTHORITY = "mop-release-assign-v1"
AUTHORITY_HEADER = "x-heydonna-direct-client"
CAPABILITY_HEADER = "x-mop-capability"
SESSION_CLEAR_PATH = "/slots/{slot}/session/clear"
SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
LOOPBACK_RE = re.compile(r"^http://127\.0\.0\.1:[0-9]{1,5}$")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        raise urllib.error.HTTPError(request.full_url, code, "redirect refused", headers, file)


OPENER = urllib.request.build_opener(NoRedirect)


def endpoint_is_exact_loopback(value: str) -> bool:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        return False
    return bool(
        LOOPBACK_RE.fullmatch(value)
        and parsed.scheme == "http"
        and parsed.hostname == "127.0.0.1"
        and parsed.username is None
        and parsed.password is None
        and parsed.path in ("", "/")
        and not parsed.query
        and not parsed.fragment
        and port is not None
        and 1 <= port <= 65535
    )


def request_json(
    base_url: str,
    path: str,
    method: str,
    body: dict[str, object] | None,
    capability: str,
) -> tuple[int, dict[str, object]]:
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(f"{base_url.rstrip('/')}{path}", data=data, method=method)
    request.add_header("Accept", "application/json")
    request.add_header(AUTHORITY_HEADER, DEFAULT_AUTHORITY)
    request.add_header(CAPABILITY_HEADER, capability)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with OPENER.open(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status, payload if isinstance(payload, dict) else {}
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {"success": False, "code": "mop_http_error"}
        return error.code, payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError) as error:
        return 599, {"success": False, "code": "mop_response_unreadable", "reason": type(error).__name__}


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
    result.add_argument("--execute", action="store_true", help="request the explicit session-clear route after all fences")
    result.add_argument("--dry-run", action="store_true", help="report the exact fence without an effect")
    return result


def _checkout_observation(args: argparse.Namespace) -> tuple[bool, dict[str, object]]:
    path = Path(args.checkout_path)
    if not path.is_absolute() or not path.is_dir() or path.is_symlink():
        return False, {"code": "checkout_unavailable"}
    commands = {
        "branch": ["branch", "--show-current"],
        "head": ["rev-parse", "HEAD"],
        "status": ["status", "--porcelain", "--untracked-files=all"],
        "unpushed": ["rev-list", "@{upstream}..HEAD"],
    }
    values: dict[str, str] = {}
    for name, command in commands.items():
        try:
            result = subprocess.run(
                ["git", "-C", str(path), *command],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return False, {"code": "checkout_observation_failed"}
        if result.returncode != 0:
            return False, {"code": "checkout_observation_failed", "field": name}
        values[name] = result.stdout.strip()
    observed = {
        "checkout_path": str(path),
        "checkout_branch": values["branch"],
        "checkout_head": values["head"].lower(),
        "checkout_clean": values["status"] == "",
        "unpushed_commits": [line for line in values["unpushed"].splitlines() if line],
    }
    if (
        observed["checkout_branch"] != args.checkout_branch
        or observed["checkout_head"] != args.checkout_head.lower()
        or observed["checkout_clean"] is not True
        or observed["unpushed_commits"] != []
    ):
        return False, {"code": "checkout_state_drift", "observed": observed}
    return True, observed


def _fence(args: argparse.Namespace, observed: dict[str, object]) -> tuple[bool, str | None]:
    required = {
        "assignment_epoch": args.expected_epoch,
        "session_id": args.expected_session_id,
        "session_started_at": args.expected_session_started_at,
    }
    if any(observed.get(key) != value for key, value in required.items()):
        return False, "session_state_drift"
    if (
        observed.get("occupied") is not False
        or observed.get("dnd") is not False
        or observed.get("idle") is not True
        or observed.get("active_turn_id") is not None
        or observed.get("active_turn_state") != "inactive"
    ):
        return False, "session_not_free_inactive"
    return True, None


def main() -> int:
    args = parser().parse_args()
    capability = os.environ.get("MOP_LOCAL_CAPABILITY", "")
    if args.slot < 1 or args.slot > 6 or args.expected_age_seconds <= 6 * 60 * 60:
        print(json.dumps({"success": False, "effect": False, "code": "invalid_session_clear_request"}, sort_keys=True))
        return 2
    if not SHA_RE.fullmatch(args.checkout_head):
        print(json.dumps({"success": False, "effect": False, "code": "invalid_checkout_head"}, sort_keys=True))
        return 2
    if not endpoint_is_exact_loopback(args.base_url):
        print(json.dumps({"success": False, "effect": False, "code": "MOP_ENDPOINT_NOT_LOCAL"}, sort_keys=True))
        return 2
    if not re.fullmatch(r"[0-9a-fA-F]{64}", capability):
        print(json.dumps({"success": False, "effect": False, "code": "MOP_CAPABILITY_MISSING"}, sort_keys=True))
        return 2
    status, observed = request_json(
        args.base_url,
        f"/slots/{args.slot}",
        "GET",
        None,
        capability,
    )
    if status != 200:
        print(json.dumps({"success": False, "effect": False, "code": "mop_read_failed", "status": status}, sort_keys=True))
        return 2
    ok, reason = _fence(args, observed)
    if not ok:
        print(json.dumps({"success": False, "effect": False, "code": reason}, sort_keys=True))
        return 2
    checkout_ok, checkout = _checkout_observation(args)
    if not checkout_ok:
        print(json.dumps({"success": False, "effect": False, **checkout}, sort_keys=True))
        return 2
    body = {
        "expected_epoch": args.expected_epoch,
        "expected_session_id": args.expected_session_id,
        "expected_session_started_at": args.expected_session_started_at,
        "expected_age_seconds": args.expected_age_seconds,
        "checkout_path": str(Path(args.checkout_path)),
        "checkout_branch": args.checkout_branch,
        "checkout_head": args.checkout_head.lower(),
        "checkout_clean": True,
        "unpushed_commits": [],
        "request_token": args.request_token,
    }
    if args.dry_run or not args.execute:
        print(json.dumps({"success": True, "effect": False, "mode": "dry-run", "observed": observed, "checkout": checkout}, sort_keys=True))
        return 0
    response_status, response = request_json(
        args.base_url,
        SESSION_CLEAR_PATH.format(slot=args.slot),
        "POST",
        body,
        capability,
    )
    print(json.dumps(response, sort_keys=True))
    return 0 if response_status == 200 and response.get("success") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
