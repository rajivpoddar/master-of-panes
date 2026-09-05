#!/usr/bin/env python3
"""Classify a change for paid CI/E2E and optional long-file coverage.

Ownership is classified separately from evidence: a control-plane-only
exemption is granted only when every changed path is control-plane-owned
(PR/slot state-machine surfaces under scripts/pm/** and .claude/**, an exact
allowlist of mechanical CI transition consumers under scripts/ci/**, plus
docs/benchmark exempt classes). App-owned surfaces always fail closed to real
CI/E2E: GitHub workflow implementation (.github/**) and every scripts/ci/**
path not on that exact allowlist is APP_CI,
test fixtures and harnesses (tests/**, __tests__/**) are APP_TEST_FIXTURE, and
everything else is APP_PRODUCT. The `ownership` field reports the per-path
class so routing consumers can describe proof obligations without transferring
ownership of an app path to the control plane. Unknown, empty, or mixed scopes
fail closed to product CI/E2E. Explicit legacy aliases allow a known
control-plane tool's rename/delete side to remain exempt without turning
arbitrary root-level script paths into control-plane changes.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_RULES = SCRIPT_DIR / "change-scope-rules.json"


def load_rules(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    data = json.loads(raw)
    if data.get("schema_version") != 1:
        raise ValueError("unsupported change-scope rules schema")
    for key in (
        "control_plane_only",
        "control_plane_ci",
        "app_ci",
        "app_test_fixture",
        "convex_runtime",
        "editor_product",
        "ui_product",
    ):
        values = data.get(key)
        if not isinstance(values, list) or not all(
            isinstance(value, str) and value for value in values
        ):
            raise ValueError(f"invalid {key} rules")
    control_plane_ci = data["control_plane_ci"]
    if len(control_plane_ci) != len(set(control_plane_ci)):
        raise ValueError("duplicate control_plane_ci rule")
    for value in control_plane_ci:
        if (
            not value.startswith("scripts/ci/")
            or value.endswith("/")
            or ".." in Path(value).parts
            or any(character in value for character in "*?[")
        ):
            raise ValueError(
                "control_plane_ci rules must be exact files under scripts/ci/"
            )
    legacy = data.get("control_plane_legacy", [])
    if not isinstance(legacy, list) or not all(
        isinstance(value, str) and value for value in legacy
    ):
        raise ValueError("invalid control_plane_legacy rules")
    return data, hashlib.sha256(raw).hexdigest()


def matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def path_ownership(
    path: str,
    control_patterns: list[str],
    control_plane_ci_patterns: list[str],
    app_ci_patterns: list[str],
    app_test_fixture_patterns: list[str],
) -> str:
    """Classify one changed path's ownership class.

    General control-plane patterns win (the docs/benchmark exempt classes live
    there too), followed by the exact mechanical CI control-plane allowlist.
    Every other workflow/CI implementation path is APP_CI, test
    fixtures/harnesses are APP_TEST_FIXTURE, and every remaining path is
    APP_PRODUCT.
    """
    if matches(path, control_patterns):
        return "control_plane"
    if matches(path, control_plane_ci_patterns):
        return "control_plane_ci"
    if matches(path, app_ci_patterns):
        return "app_ci"
    if matches(path, app_test_fixture_patterns):
        return "app_test_fixture"
    return "app_product"


def classify(paths: list[str], rules: dict[str, Any], rules_sha256: str) -> dict[str, Any]:
    changed = sorted(
        {
            path.strip()[2:] if path.strip().startswith("./") else path.strip()
            for path in paths
            if path.strip()
        }
    )
    control_patterns = rules["control_plane_only"] + rules.get("control_plane_legacy", [])
    ownership = {
        path: path_ownership(
            path,
            control_patterns,
            rules["control_plane_ci"],
            rules["app_ci"],
            rules["app_test_fixture"],
        )
        for path in changed
    }
    control_matches = [path for path in changed if ownership[path] == "control_plane"]
    control_plane_ci_matches = [
        path for path in changed if ownership[path] == "control_plane_ci"
    ]
    app_ci_matches = [path for path in changed if ownership[path] == "app_ci"]
    app_test_fixture_matches = [
        path for path in changed if ownership[path] == "app_test_fixture"
    ]
    editor_matches = [path for path in changed if matches(path, rules["editor_product"])]
    ui_matches = [path for path in changed if matches(path, rules["ui_product"])]
    convex_runtime_matches = [
        path for path in changed if matches(path, rules["convex_runtime"])
    ]
    control_plane_owned_matches = control_matches + control_plane_ci_matches
    control_plane_only = bool(changed) and len(control_plane_owned_matches) == len(changed)
    product_changed = bool(changed) and not control_plane_only
    editor_changed = bool(editor_matches)
    ui_changed = bool(ui_matches) and not control_plane_only
    convex_pool_eligible = bool(
        product_changed
        and not convex_runtime_matches
    )

    if control_plane_only:
        scope = "control_plane_only"
    elif not changed:
        scope = "unknown"
    elif control_plane_owned_matches:
        scope = "mixed"
    elif editor_changed:
        scope = "editor_product"
    else:
        scope = "product"

    return {
        "schema_version": 1,
        "scope": scope,
        "control_plane_only": control_plane_only,
        "product_changed": product_changed,
        "ci_required": not control_plane_only,
        "e2e_required": not control_plane_only,
        "lfc_required": editor_changed and not control_plane_only,
        "editor_changed": editor_changed,
        "ui_changed": ui_changed,
        "convex_pool_eligible": convex_pool_eligible,
        "convex_runtime_changed": bool(convex_runtime_matches),
        "changed_files": changed,
        "ownership": ownership,
        "control_plane_matches": control_matches,
        "control_plane_ci_matches": control_plane_ci_matches,
        "app_ci_matches": app_ci_matches,
        "app_test_fixture_matches": app_test_fixture_matches,
        "app_ci_changed": bool(app_ci_matches),
        "app_test_fixture_changed": bool(app_test_fixture_matches),
        "editor_matches": editor_matches,
        "ui_matches": ui_matches,
        "convex_runtime_matches": convex_runtime_matches,
        "rules_sha256": rules_sha256,
    }


def git_changed_files(repo_root: Path, base: str, head: str) -> list[str]:
    completed = subprocess.run(
        ["git", "diff", "--no-renames", "--name-only", f"{base}...{head}"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.stdout.splitlines()


def pr_changed_files(repo_root: Path, repo: str, pr: int, expected_head: str) -> list[str]:
    live_head = subprocess.run(
        ["gh", "api", f"repos/{repo}/pulls/{pr}", "--jq", ".head.sha"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    if live_head != expected_head:
        raise ValueError(
            f"PR head mismatch: expected={expected_head} live={live_head or 'unknown'}"
        )
    completed = subprocess.run(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{repo}/pulls/{pr}/files?per_page=100",
            "--jq",
            ".[] | [.filename, (.previous_filename // empty)] | .[]",
        ],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return completed.stdout.splitlines()


def write_github_output(path: Path, result: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for key in (
            "scope",
            "control_plane_only",
            "product_changed",
            "ci_required",
            "e2e_required",
            "lfc_required",
            "editor_changed",
            "ui_changed",
            "convex_pool_eligible",
            "convex_runtime_changed",
            "rules_sha256",
        ):
            value = result[key]
            if isinstance(value, bool):
                value = str(value).lower()
            handle.write(f"{key}={value}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    parser.add_argument("--repo-root", type=Path, default=SCRIPT_DIR.parent.parent)
    parser.add_argument("--repo", default="heydonna-app/heydonna-app")
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--paths-file", type=Path)
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--pr", type=int)
    parser.add_argument("--expected-head")
    parser.add_argument("--github-output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        rules, rules_sha256 = load_rules(args.rules)
        paths = list(args.path)
        if args.paths_file:
            paths.extend(args.paths_file.read_text(encoding="utf-8").splitlines())
        if args.pr is not None:
            if not args.expected_head:
                raise ValueError("--pr requires --expected-head")
            paths.extend(
                pr_changed_files(args.repo_root, args.repo, args.pr, args.expected_head)
            )
        elif args.base or args.head:
            if not args.base or not args.head:
                raise ValueError("--base and --head must be provided together")
            paths.extend(git_changed_files(args.repo_root, args.base, args.head))
        result = classify(paths, rules, rules_sha256)
        if args.expected_head:
            result["head"] = args.expected_head
        if args.github_output:
            write_github_output(args.github_output, result)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(f"change-scope: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
