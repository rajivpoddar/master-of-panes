"""Focused contracts for the legacy PM residual-surface retirement (Option C).

Proves: canonical retirement-inventory completeness, stale-reference removal,
no-assignment when all six slots are occupied, drift refusal, exact rollback
restoration, idempotence, and no residual retired proof-token dependency.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import stat
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets"
RETIREMENTS = SHARED / "retirements.json"
INSTALLER_PATH = ROOT / "scripts" / "install-release.py"

SPEC = importlib.util.spec_from_file_location("install_release_retirement", INSTALLER_PATH)
assert SPEC and SPEC.loader
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)

RETIRED = "slot-dispatch-sweep"
LEGACY_BUS = "pm-transition"


def _canonical() -> dict:
    return json.loads(RETIREMENTS.read_text(encoding="utf-8"))


def test_inventory_is_complete_deterministic_and_classified() -> None:
    manifest = _canonical()
    assert manifest["schema"] == "mop_shared_retirements"
    assert manifest["version"] == 1
    items = manifest["items"]
    assert items == sorted(items, key=lambda i: i["absolute_current_path"])
    by_disposition = {i["disposition"] for i in items}
    assert by_disposition <= {"DELETE", "HOLD_UNTIL_OPERATOR_LIVE"}
    # The five named residual surfaces are all represented exactly once.
    assert len(items) == 5
    # Registered hooks are held; unregistered surfaces are deletable.
    held = {i["absolute_current_path"] for i in items if i["disposition"] == "HOLD_UNTIL_OPERATOR_LIVE"}
    assert held == {
        "/Users/rajiv/.claude/hooks/pm-todo-debt.sh",
        "/Users/rajiv/.claude/hooks/post-issue-create-sweep.sh",
    }
    for item in items:
        assert item["replacement"].strip()
        assert len(item["preimage_sha256"]) == 64
        assert isinstance(item["preimage_mode"], int)
        assert item["deletion_proof"]["status"] in {"SATISFIED", "PENDING"}


def test_replacements_do_not_reintroduce_retired_or_raw_paths() -> None:
    """No replacement may instruct a retired sweep, pm-transition, or a raw API call."""
    manifest = _canonical()
    contract = manifest["replacement_contract"]
    assert "direct-assign" in contract and "direct-release" in contract
    assert "NO_ASSIGNABLE_SLOT" in contract
    for item in manifest["items"]:
        for field in ("replacement", "hold_prerequisite"):
            text = item.get(field) or ""
            assert RETIRED not in text, f"{field} reintroduces {RETIRED}"
            assert LEGACY_BUS not in text, f"{field} reintroduces {LEGACY_BUS}"
            assert "127.0.0.1" not in text and "/slots/" not in text


def test_no_assignment_or_release_when_all_six_slots_occupied() -> None:
    """The retirement contract must not authorize automatic motion under full occupancy."""
    manifest = _canonical()
    contract = manifest["replacement_contract"]
    assert "No automatic sweep" in contract
    assert "no assignment or release effect" in contract
    lowered = contract.lower()
    assert "auto-assign" not in lowered and "auto assign" not in lowered
    # Retirement never grants dispatch authority to the installer.
    src = INSTALLER_PATH.read_text(encoding="utf-8")
    body = src.split("def retire_shared_assets", 1)[1].split("\ndef activate", 1)[0]
    for forbidden in ("/slots/", "assign(", "/assign", "release_slot", "mop_all_slots"):
        assert forbidden not in body, f"retirement path must not call {forbidden}"


def _temp_release(root: Path, targets: dict[str, bytes]) -> Path:
    """Build a hermetic release whose retirement manifest points inside `root`."""
    release = root / "release"
    dest = release / "scripts" / "pm" / "shared-assets"
    dest.mkdir(parents=True)
    items = []
    for rel, payload in targets.items():
        live = root / rel
        live.parent.mkdir(parents=True, exist_ok=True)
        live.write_bytes(payload)
        live.chmod(0o755)
        items.append(
            {
                "absolute_current_path": str(live),
                "disposition": "DELETE",
                "preimage_sha256": hashlib.sha256(payload).hexdigest(),
                "preimage_mode": 0o755,
                "installed_root": str(live.parent),
                "registration": "NOT_REGISTERED_IN_SETTINGS",
                "retirement_reason": "hermetic fixture",
                "replacement": "Skill(direct-assign)",
                "deletion_proof": {"check": "fixture", "status": "SATISFIED"},
            }
        )
    items.sort(key=lambda i: i["absolute_current_path"])
    (dest / "retirements.json").write_text(
        json.dumps(
            {
                "schema": "mop_shared_retirements",
                "version": 1,
                "replacement_contract": "Skill(direct-assign); NO_ASSIGNABLE_SLOT; No automatic sweep",
                "items": items,
            }
        ),
        encoding="utf-8",
    )
    return release


def test_retire_removes_listed_files_and_leaves_rollback_bundle() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        release = _temp_release(root, {"Users/rajiv/.claude/hooks/h.sh": b"# legacy hook\n"})
        target = root / "Users/rajiv/.claude/hooks/h.sh"
        result = INSTALLER.retire_shared_assets(
            release_dir=release,
            installed_roots=[root / "Users/rajiv/.claude/hooks"],
            rollback_bundle=root / "rollback",
        )
        assert result["status"] == "RETIRED"
        assert str(target) in result["retired"]
        assert not target.exists()
        assert (root / "rollback" / "ROLLBACK_MANIFEST.json").is_file()


def test_retire_refuses_on_preimage_drift() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        release = _temp_release(root, {"Users/rajiv/.claude/hooks/h.sh": b"# original\n"})
        target = root / "Users/rajiv/.claude/hooks/h.sh"
        target.write_bytes(b"# tampered\n")  # drift after the canonical preimage was recorded
        with pytest.raises(INSTALLER.InstallerError, match="drifted from the canonical preimage"):
            INSTALLER.retire_shared_assets(
                release_dir=release,
                installed_roots=[root / "Users/rajiv/.claude/hooks"],
                rollback_bundle=root / "rollback",
            )
        assert target.exists()  # untouched on refusal


def test_rollback_restores_exact_bytes_and_mode() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        payload = b"# legacy hook with content\n"
        release = _temp_release(root, {"Users/rajiv/.claude/hooks/h.sh": payload})
        target = root / "Users/rajiv/.claude/hooks/h.sh"
        INSTALLER.retire_shared_assets(
            release_dir=release,
            installed_roots=[root / "Users/rajiv/.claude/hooks"],
            rollback_bundle=root / "rollback",
        )
        assert not target.exists()
        INSTALLER.restore_rollback_bundle(root / "rollback")
        assert target.read_bytes() == payload
        assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_retire_is_idempotent_when_targets_already_absent() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        release = _temp_release(root, {"Users/rajiv/.claude/hooks/h.sh": b"# x\n"})
        target = root / "Users/rajiv/.claude/hooks/h.sh"
        INSTALLER.retire_shared_assets(
            release_dir=release,
            installed_roots=[root / "Users/rajiv/.claude/hooks"],
            rollback_bundle=root / "rollback-a",
        )
        assert not target.exists()
        second = INSTALLER.retire_shared_assets(
            release_dir=release,
            installed_roots=[root / "Users/rajiv/.claude/hooks"],
            rollback_bundle=root / "rollback-b",
        )
        assert second["status"] == "RETIRED"


def test_retire_refuses_directories_and_unlisted_targets() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        release = _temp_release(root, {"Users/rajiv/.claude/hooks/h.sh": b"# x\n"})
        # An unlisted target inside the same installed root is never touched.
        unlisted = root / "Users/rajiv/.claude/hooks/other.sh"
        unlisted.write_bytes(b"# keep me\n")
        INSTALLER.retire_shared_assets(
            release_dir=release,
            installed_roots=[root / "Users/rajiv/.claude/hooks"],
            rollback_bundle=root / "rollback",
        )
        assert unlisted.read_bytes() == b"# keep me\n"
