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
from pathlib import Path
from typing import Sequence


RELEASE_ROOT_ENV = "HEYDONNA_CONTROL_PLANE_RELEASE_ROOT"
DEFAULT_RELEASE_ROOT = Path("~/.claude/control-plane/current/heydonna")
ASSIGNMENT_COMMANDS = {"claim-slot", "rebind-slot", "release-slot"}
FAMILY2_COMMANDS = {"slot-ready", "pm-review"}
CAPACITY_COMMANDS = {"capacity-snapshot", "reconcile-capacity"}
SUPPORTED_COMMANDS = ASSIGNMENT_COMMANDS | FAMILY2_COMMANDS | CAPACITY_COMMANDS


def _package_root() -> Path:
    override = os.environ.get(RELEASE_ROOT_ENV)
    if override is not None:
        root = Path(override)
        if not root.is_absolute():
            raise ValueError(f"{RELEASE_ROOT_ENV} must be an absolute path")
        return root.resolve(strict=True)
    source = Path(__file__).resolve().parents[3]
    if (source / "scripts/pm/control_plane/assignment_boundary.py").is_file():
        return source
    return DEFAULT_RELEASE_ROOT.expanduser().resolve(strict=True)


def _blocked(command: str, reason: str) -> int:
    print(json.dumps({"decision": "BLOCK", "command": command, "reason": reason}, sort_keys=True))
    return 423


def _assignment(command: str, args: list[str], root: Path) -> int:
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
        args = [*args[: state_index + 2], command, *args[state_index + 2 :]]
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
    root = _package_root()
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
