#!/usr/bin/env python3
"""Synchronously reset and observe one MoP-derived numbered-slot checkout."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def git(checkout: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(checkout), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def observe(checkout: Path, *args: str) -> str | None:
    completed = git(checkout, *args)
    return completed.stdout.strip() if completed.returncode == 0 else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkout", required=True)
    parser.add_argument("--intended-main-head", required=True)
    args = parser.parse_args()

    # Preserve the exact absolute worktree identity supplied by MoP. Resolving
    # macOS /var through /private/var would make an equivalent checkout appear
    # to be a different acknowledgement path.
    checkout = Path(args.checkout).absolute()
    errors: list[str] = []
    reset_succeeded = False
    if not checkout.is_dir():
        errors.append("checkout_not_found")
    else:
        switched = git(checkout, "switch", "main")
        if switched.returncode != 0:
            errors.append(f"switch_main_failed:{switched.stderr.strip()[:300]}")
        else:
            pulled = git(checkout, "pull", "--ff-only", "origin", "main")
            if pulled.returncode != 0:
                errors.append(f"pull_main_failed:{pulled.stderr.strip()[:300]}")
            else:
                reset_succeeded = True

    branch = observe(checkout, "branch", "--show-current") if checkout.is_dir() else None
    head = observe(checkout, "rev-parse", "HEAD") if checkout.is_dir() else None
    status = observe(checkout, "status", "--porcelain") if checkout.is_dir() else None
    payload = {
        "checkout_path": str(checkout),
        "branch": branch,
        "head": head,
        "clean": status == "",
        "reset_succeeded": reset_succeeded,
        "error": ";".join(errors) or None,
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
