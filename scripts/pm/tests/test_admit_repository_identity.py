#!/usr/bin/env python3
"""Hermetic coverage for canonical repository identity admission guards."""

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIT_PATH = REPO_ROOT / "pm" / "shared-assets" / "codex" / "skills" / "heydonna-cto-label-gated-ci" / "scripts" / "admit.py"
SPEC = importlib.util.spec_from_file_location("admit_identity_under_test", ADMIT_PATH)
admit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(admit)

HEAD = "a" * 40


def make_pr(*, head_repo="Scribie/heydonna-app", base_ref="main"):
    return {
        "head": {"sha": HEAD, "repo": {"full_name": head_repo}},
        "base": {"ref": base_ref},
        "state": "open",
        "draft": False,
    }


def expect_refusal(pr, reason):
    admit.api = lambda path: pr
    try:
        admit.pr_at(12872, HEAD)
    except admit.Refusal as exc:
        assert str(exc) == reason
    else:
        raise AssertionError(f"expected refusal: {reason}")


def test_native_scribie_metadata_passes_same_repository_guard():
    assert admit.REPO == "Scribie/heydonna-app"
    admit.api = lambda path: make_pr()
    assert admit.pr_at(12872, HEAD)["head"]["repo"]["full_name"] == admit.REPO


def test_fork_metadata_is_refused():
    expect_refusal(make_pr(head_repo="attacker/heydonna-app"),
                   "PR_MUST_TARGET_MAIN_IN_SAME_REPOSITORY")


def test_non_main_base_is_refused():
    expect_refusal(make_pr(base_ref="release"),
                   "PR_MUST_TARGET_MAIN_IN_SAME_REPOSITORY")


if __name__ == "__main__":
    for test in (test_native_scribie_metadata_passes_same_repository_guard,
                 test_fork_metadata_is_refused,
                 test_non_main_base_is_refused):
        test()
    print("3 repository identity checks passed")
