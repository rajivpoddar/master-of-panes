"""Focused proof for the PM_OPERATOR_CUTOVER home-only half.

Proves: adopted canonical sources carry zero executable/instructional
references to retired pm-transition / pm-operator / pm-state-replace /
slot-dispatch-sweep / slot-submit-ready surfaces; assignment/release route
only through Skill(direct-assign) / Skill(direct-release); retired classes
surface as no-mutation UNSUPPORTED_LIFECYCLE_ACTION typed stops; manifest
entries are sorted with verified digest/mode parity.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path


ROOT = Path(__file__).parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets"
SKILLS = SHARED / "claude" / "skills"
MANIFEST_PATH = SHARED / "manifest.json"
INSTALLER_PATH = ROOT / "scripts" / "install-release.py"
SPEC = importlib.util.spec_from_file_location("install_release_cutover", INSTALLER_PATH)
assert SPEC and SPEC.loader
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)

RETIRED = [
    "pm-transition",
    "pm-operator",
    "pm-state-replace",
    "slot-dispatch-sweep",
    "slot-submit-ready",
]

ADOPTED = [
    "pr-state-sweep/SKILL.md",
    "pr-state-sweep/scripts/sweep.sh",
    "ci-success-reconciliation/SKILL.md",
    "codex-companion-cap-mechanism/SKILL.md",
    "cp-repair-request/SKILL.md",
    "anthropic-529-overload-inline-fallback/SKILL.md",
    "heydonna-review-cap-cto-approve-handoff/SKILL.md",
    "message-slot/SKILL.md",
    "message-slot/scripts/message-slot.sh",
    "bash-single-quote-python-c-inner-quote-collision/SKILL.md",
    "codex-review-companion/SKILL.md",
    "todo-prioritize/SKILL.md",
]


def _read(relative: str) -> str:
    path = SKILLS / relative
    assert path.is_file(), f"adopted source missing: {relative}"
    return path.read_text(encoding="utf-8")


def test_adopted_sources_have_zero_retired_references() -> None:
    for relative in ADOPTED:
        text = _read(relative)
        for name in RETIRED:
            assert name not in text, f"{relative} still references retired {name}"


def test_assignment_release_route_only_through_direct_skills() -> None:
    sweep = _read("pr-state-sweep/scripts/sweep.sh")
    assert "Skill(direct-assign)" in sweep
    assert "Skill(direct-release)" in sweep
    assert '"$PM_OPERATOR"' not in sweep
    assert "PM_OPERATOR=" not in sweep
    todo = _read("todo-prioritize/SKILL.md")
    assert "Skill(direct-assign)" in todo
    sweep_skill = _read("pr-state-sweep/SKILL.md")
    assert "Skill(direct-assign)" in sweep_skill
    assert "Skill(direct-release)" in sweep_skill


def test_retired_classes_become_typed_stops() -> None:
    sweep = _read("pr-state-sweep/scripts/sweep.sh")
    for class_name in (
        "block-pr",
        "reconcile-capacity",
        "pm-review-done",
        "pm-review",
        "merge-ready",
        "validate-ready-proof",
        "slot-ready",
        "record-rework-packet",
        "capture-remote-dispatch",
        "capture-remote-pass",
        "ci-local-preflight-pass",
        "dependency-unblocked",
    ):
        assert f"UNSUPPORTED_LIFECYCLE_ACTION:{class_name}" in sweep, class_name
    sweep_skill = _read("pr-state-sweep/SKILL.md")
    for class_name in ("rescope-pr", "park-issue", "record-rework-packet"):
        assert f"UNSUPPORTED_LIFECYCLE_ACTION:{class_name}" in sweep_skill, class_name
    review_cap = _read("heydonna-review-cap-cto-approve-handoff/SKILL.md")
    for class_name in ("accept-ready", "rescope-decide"):
        assert f"UNSUPPORTED_LIFECYCLE_ACTION:{class_name}" in review_cap, class_name
    assert "UNSUPPORTED_LIFECYCLE_ACTION" in _read("ci-success-reconciliation/SKILL.md")
    assert "UNSUPPORTED_LIFECYCLE_ACTION" in _read("codex-companion-cap-mechanism/SKILL.md")
    assert "UNSUPPORTED_LIFECYCLE_ACTION" in _read("heydonna-review-cap-cto-approve-handoff/SKILL.md")
    assert "UNSUPPORTED_LIFECYCLE_ACTION" in _read("pr-state-sweep/SKILL.md")


def test_preserved_supported_paths_survive() -> None:
    sweep = _read("pr-state-sweep/scripts/sweep.sh")
    assert "message-slot" in sweep
    assert "pm-readiness-contract" in sweep
    assert "request-label-gated-ci" in _read("heydonna-review-cap-cto-approve-handoff/SKILL.md")


def test_manifest_covers_adopted_sources_with_parity() -> None:
    manifest = INSTALLER._load_shared_manifest(ROOT)
    by_path = {item["source_path"]: item for item in manifest["entries"]}
    for relative in ADOPTED:
        source_path = f"claude/skills/{relative}"
        assert source_path in by_path, source_path
        assert by_path[source_path]["dependency_status"] == "closed"
        assert by_path[source_path]["dependencies"] == []
    assert manifest["inventory"]["selected_count"] == len(manifest["entries"])


ADOPTED_EXECUTABLES = [
    "claude/scripts/ci-success-reconciliation.py",
]


def _read_executable(relative: str) -> str:
    path = SHARED / relative
    assert path.is_file(), f"adopted executable missing: {relative}"
    return path.read_text(encoding="utf-8")


def test_adopted_executables_have_zero_retired_references() -> None:
    for relative in ADOPTED_EXECUTABLES:
        text = _read_executable(relative)
        for name in RETIRED:
            assert name not in text, f"{relative} still references retired {name}"


def test_adopted_executable_has_no_undeclared_retired_transport() -> None:
    text = _read_executable("claude/scripts/ci-success-reconciliation.py")
    assert "MERGE_READY_NOTIFY_OK" in text
    assert "notification_skipped_no_transport" in text
    assert "pm-transition-alert" not in text


def test_rework_delivery_routes_only_through_direct_assign() -> None:
    sweep = _read("pr-state-sweep/scripts/sweep.sh")
    marker = "--kind rework_delivery_pending"
    assert marker in sweep
    block = sweep.split(marker, 1)[1].split(";;", 1)[0]
    assert "Skill(direct-assign)" in block
    assert "Resume the canonical claim_slot" not in block
    assert "do not resume a legacy claim_slot/message-slot outbox" in block
    assert "do not retry" in block
    skill = _read("pr-state-sweep/SKILL.md")
    assert "Skill(direct-assign)" in skill
    assert "must execute the\nnamed PM-local `message-slot.sh" not in skill
    assert "Do not use `message-slot.sh` for rework" in skill


def test_mutation_policy_matches_no_release_behavior() -> None:
    skill = _read("pr-state-sweep/SKILL.md")
    assert "removes stale `slot:*` labels" not in skill
    assert "releases matching MoP slots" not in skill
    assert "performs no label deletion and no MoP slot release itself" in skill
    assert "Skill(direct-release)" in skill
    sweep = _read("pr-state-sweep/scripts/sweep.sh")
    marker = "PR_PM_REVIEW_WORKFLOW_DRAIN_REQUIRED*)"
    assert marker in sweep
    block = sweep.split(marker, 1)[1].split(";;", 1)[0]
    assert "UNSUPPORTED_LIFECYCLE_ACTION:pm-review-done" in block
    assert "processor can continue" not in block


def test_manifest_covers_adopted_executable_with_parity() -> None:
    import hashlib
    import os

    manifest = INSTALLER._load_shared_manifest(ROOT)
    by_path = {item["source_path"]: item for item in manifest["entries"]}
    relative = "claude/scripts/ci-success-reconciliation.py"
    assert relative in by_path, relative
    entry = by_path[relative]
    assert entry["canonical_target"] == "/Users/rajiv/.claude/scripts/ci-success-reconciliation.py"
    assert entry["dependency_status"] == "closed"
    assert entry["dependencies"] == []
    source = SHARED / relative
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert entry["sha256"] == digest
    assert entry["mode"] == (os.stat(source).st_mode & 0o777) == 0o755
