#!/usr/bin/env python3
"""The public Python mutation boundary for the PM hot path.

This launcher is intentionally thin.  It selects the immutable control-plane
release and delegates to the existing assignment, family-2, and capacity
kernels; it does not contain a second state machine or writer.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from pathlib import Path
from typing import Sequence


RELEASE_ROOT_ENV = "HEYDONNA_CONTROL_PLANE_RELEASE_ROOT"
DEFAULT_RELEASE_ROOT = Path("~/.claude/control-plane/current/heydonna")
ASSIGNMENT_COMMANDS = {"claim-slot", "rebind-slot", "release-slot"}
FAMILY2_COMMANDS = {"slot-ready", "pm-review"}
CAPACITY_COMMANDS = {"capacity-snapshot", "reconcile-capacity"}
SUPPORTED_COMMANDS = ASSIGNMENT_COMMANDS | FAMILY2_COMMANDS | CAPACITY_COMMANDS


def _package_root(command: str) -> Path:
    override = os.environ.get(RELEASE_ROOT_ENV)
    if override is not None:
        root = Path(override)
        if not root.is_absolute():
            raise ValueError(f"{RELEASE_ROOT_ENV} must be an absolute path")
        return root.resolve()
    if command in ASSIGNMENT_COMMANDS:
        # The installed assignment facade is release-owned and must not fall
        # back to the retired local assignment database/kernel.
        return Path(__file__).resolve().parent
    # Family-2 and capacity commands still consume their already-versioned
    # immutable kernels until their direct MoP/GitHub replacements land.
    return DEFAULT_RELEASE_ROOT.expanduser().resolve(strict=True)


def _blocked(command: str, reason: str) -> int:
    print(json.dumps({"decision": "BLOCK", "command": command, "reason": reason}, sort_keys=True))
    return 423


def _flag_values(args: list[str]) -> dict[str, str | None]:
    values: dict[str, str | None] = {}
    index = 0
    while index < len(args):
        token = args[index]
        if not token.startswith("--"):
            raise ValueError(f"unexpected argument {token}")
        name = token[2:].replace("-", "_")
        if index + 1 >= len(args) or args[index + 1].startswith("--"):
            raise ValueError(f"--{name.replace('_', '-')} requires a value; use null explicitly when allowed")
        values[name] = args[index + 1]
        index += 2
    return values


def _required(values: dict[str, str | None], names: Sequence[str]) -> None:
    missing = [name for name in names if values.get(name) in (None, "")]
    if missing:
        raise ValueError(f"missing complete MoP fields: {', '.join('--' + name.replace('_', '-') for name in missing)}")


def _integer(values: dict[str, str | None], name: str) -> int:
    raw = values.get(name)
    try:
        return int(raw or "")
    except ValueError as error:
        raise ValueError(f"--{name.replace('_', '-')} must be an integer") from error


def _direct_assignment(command: str, args: list[str]) -> int:
    """Call the authoritative MoP HTTP surface without local state or fallback writes."""
    if "--state" in args:
        raise ValueError("--state is not supported by the stateless MoP facade")
    values = _flag_values(args)
    base_url = (values.get("mop_url") or os.environ.get("MOP_URL") or "http://127.0.0.1:3100").rstrip("/")
    slot = _integer(values, "slot")
    expected_epoch = _integer(values, "expected_epoch")
    headers = {
        "content-type": "application/json",
        "x-heydonna-assignment-authority": os.environ.get(
            "MOP_ASSIGNMENT_AUTHORITY", "pm-transition-v1"
        ),
    }
    if command == "claim-slot":
        _required(values, ("repository_id", "issue", "branch", "session_id", "work_kind", "handoff_id"))
        body: dict[str, object] = {
            "task": values.get("task") or "",
            "repository_id": values["repository_id"],
            "issue": _integer(values, "issue"),
            "pr": None if values.get("pr") in (None, "null") else _integer(values, "pr"),
            "branch": values["branch"],
            "session_id": values["session_id"],
            "head_sha": None if values.get("head_sha") in (None, "null") else values["head_sha"],
            "work_kind": values["work_kind"],
            "handoff_id": values["handoff_id"],
            "expected_epoch": expected_epoch,
        }
        path = f"/slots/{slot}/assign"
    elif command == "rebind-slot":
        current_fields = ("repository_id", "issue", "pr", "branch", "head_sha", "work_kind", "handoff_id", "claimed_at")
        _required(values, current_fields)
        allowed_new_fields = {
            "new_repository_id", "new_issue", "new_pr", "new_branch", "new_head_sha",
            "new_work_kind", "new_handoff_id",
        }
        unexpected_new_fields = sorted(
            key for key in values if key.startswith("new_") and key not in allowed_new_fields
        )
        if unexpected_new_fields:
            raise ValueError(f"unsupported rebind fields: {', '.join('--' + key.replace('_', '-') for key in unexpected_new_fields)}")
        if ("new_work_kind" in values) != ("new_handoff_id" in values):
            raise ValueError("--new-work-kind and --new-handoff-id must be supplied together")
        body = {}
        for name in current_fields:
            raw = values[name]
            expected_value = None if raw in (None, "null") else raw
            desired_raw = raw if name == "claimed_at" else values.get(f"new_{name}", raw)
            desired_value = None if desired_raw in (None, "null") else desired_raw
            body[f"expected_current_{name}"] = expected_value
            body[name] = desired_value
        body["expected_epoch"] = expected_epoch
        for key in ("issue", "pr", "expected_current_issue", "expected_current_pr"):
            if body.get(key) is not None:
                body[key] = int(str(body[key]))
        path = f"/slots/{slot}/adopt-issue-claim"
    elif command == "release-slot":
        fields = (
            "expected_session_id", "expected_repository_id", "expected_issue", "expected_pr",
            "expected_branch", "expected_head_sha", "expected_work_kind", "expected_handoff_id",
            "expected_claimed_at", "intended_main_head",
        )
        _required(values, fields)
        body = {key: (None if value in (None, "null") else value) for key, value in values.items()}
        body["expected_epoch"] = expected_epoch
        for key in ("expected_issue", "expected_pr"):
            if body.get(key) is not None:
                body[key] = int(str(body[key]))
        path = f"/slots/{slot}/release"
    else:
        raise ValueError(f"unsupported direct assignment command {command}")

    request = Request(f"{base_url}{path}", method="POST", headers=headers, data=json.dumps(body).encode("utf-8"))
    try:
        # Native release may synchronously wait for pane quiescence and checkout
        # reset. Keep the client alive through that bounded workflow so a
        # successful clear is returned as the authoritative terminal result.
        timeout = 360 if command == "release-slot" else 10
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            print(json.dumps(payload, sort_keys=True))
            return 0 if payload.get("success", True) is True and payload.get("ok", True) is not False else 1
    except HTTPError as error:
        try:
            payload = json.loads(error.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {"success": False, "reason": f"http_{error.code}"}
        print(json.dumps(payload, sort_keys=True))
        return 1
    except (URLError, TimeoutError, OSError) as error:
        print(json.dumps({"success": False, "reason": "mop_unavailable", "error": str(error)}, sort_keys=True))
        return 1


def _assignment(command: str, args: list[str], root: Path) -> int:
    if not (root / "scripts/pm/control_plane/assignment_boundary.py").is_file():
        return _direct_assignment(command, args)
    sys.path.insert(0, str(root))
    from scripts.pm.control_plane.assignment_boundary import main as boundary_main

    state = os.environ.get(
        "CONTROL_PLANE_ASSIGNMENT_STATE",
        str(Path("~/.claude/control-plane-artifacts/kernel/assignment-canary-arm.json").expanduser()),
    )
    if "--state" not in args:
        args = ["--state", state, command, *args]
    else:
        state_index = args.index("--state")
        if state_index + 1 >= len(args):
            raise ValueError("--state requires a path")
        selected_state = args[state_index + 1]
        remaining = [*args[:state_index], *args[state_index + 2:]]
        args = ["--state", selected_state, command, *remaining]
    return boundary_main(args)


def _family2(command: str, args: list[str], root: Path) -> int:
    sys.path.insert(0, str(root))
    from scripts.pm.control_plane.family2_boundary import main as boundary_main

    transition = "slot_ready" if command == "slot-ready" else "pm_review"
    return boundary_main(["--transition-type", transition, *args])


def _capacity(command: str, args: list[str], root: Path) -> int:
    sys.path.insert(0, str(root))
    source = root / "scripts/pm/capacity-control.py"
    if not source.is_file():
        return _blocked(command, "capacity_kernel_unavailable")
    spec = importlib.util.spec_from_file_location("pm_capacity_control", source)
    if spec is None or spec.loader is None:
        return _blocked(command, "capacity_kernel_unloadable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    old_argv = sys.argv
    sys.argv = [str(source), "snapshot" if command == "capacity-snapshot" else "reconcile", *args]
    try:
        return int(module.main())
    finally:
        sys.argv = old_argv


def main(argv: Sequence[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if not values or values[0] in {"-h", "--help"}:
        print("usage: pm-operator.py {claim-slot|rebind-slot|release-slot|slot-ready|pm-review|capacity-snapshot|reconcile-capacity} ...")
        return 0
    command, args = values[0], values[1:]
    if command not in SUPPORTED_COMMANDS:
        return _blocked(command, "command_not_cut_over")
    root = _package_root(command)
    if command in ASSIGNMENT_COMMANDS:
        return _assignment(command, args, root)
    if command in FAMILY2_COMMANDS:
        return _family2(command, args, root)
    return _capacity(command, args, root)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"decision": "BLOCK", "reason": str(error)}, sort_keys=True), file=sys.stderr)
        raise SystemExit(20)
