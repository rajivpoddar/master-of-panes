from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets"
SOURCE = SHARED / "claude" / "scripts" / "qa-visual-proof-gate.py"
MANIFEST = SHARED / "manifest.json"
HEAD = "f026a0094573fc10e9613d3dde1351d7724c103b"
RULES_SHA256 = "bb572ca70c1c464267b92732a69cfca762c151ca05a5a7340da76a2c75191834"
APP_ORIGIN_COMMIT = "a9edd8a9f3bd2c70375073f67d1d41e9ab3c4f1a"
APP_ORIGIN_BLOB = "2bde63420e0070debb048c94d6f2513785638c3e"
APP_ORIGIN_PREIMAGE_SHA256 = "3d3b58a625a8a15b5c1336c5fb9791f173ae16ab48cce888635a7a9ea206904a"
APP_TARGET = "/Users/rajiv/Downloads/projects/heydonna-app/scripts/pm/qa-visual-proof-gate.py"
INSTALLED_TARGET = "/Users/rajiv/.claude/scripts/qa-visual-proof-gate.py"
APP_LOCAL_TARGET = "/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/qa-visual-proof-gate.py"


def _load_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    repo = tmp_path / "fixture-repo"
    (repo / "scripts" / "ci").mkdir(parents=True)
    (repo / "scripts" / "ci" / "change_scope.py").write_text("# fixture classifier\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    spec = importlib.util.spec_from_file_location("qa_visual_proof_gate_asset", SOURCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scope(*, ui_changed: bool, head: str = HEAD, rules_sha256: str = RULES_SHA256) -> dict:
    path = "scripts/ci/observability.py"
    return {
        "schema_version": 1,
        "scope": "mixed" if ui_changed else "product",
        "control_plane_only": False,
        "product_changed": True,
        "ci_required": True,
        "e2e_required": True,
        "ui_changed": ui_changed,
        "changed_files": [path],
        "ownership": {path: "app_product"},
        "rules_sha256": rules_sha256,
        "head": head,
    }


def _args(*, expect_head: str = HEAD) -> argparse.Namespace:
    return argparse.Namespace(
        pr=7589,
        repo="heydonna-app/heydonna-app",
        expect_head=expect_head,
        skip_artifact_availability=True,
    )


def test_manifest_has_one_versioned_payload_and_three_existing_targets() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    entries = [entry for entry in manifest["entries"] if entry["source_path"] == "claude/scripts/qa-visual-proof-gate.py"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["canonical_target"] == APP_TARGET
    assert entry["additional_targets"] == [INSTALLED_TARGET, APP_LOCAL_TARGET]
    assert entry["mode"] == 0o755
    assert entry["sha256"] == hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    assert entry["source_authority"] == {
        "repository": "heydonna-app/heydonna-app",
        "commit": APP_ORIGIN_COMMIT,
        "path": "scripts/pm/qa-visual-proof-gate.py",
        "blob_sha": APP_ORIGIN_BLOB,
        "preimage_sha256": APP_ORIGIN_PREIMAGE_SHA256,
        "mode": 0o755,
    }
    assert SOURCE.read_text(encoding="utf-8").count("def validate_change_scope") == 1


def test_non_ui_live_path_classifies_before_issue_resolution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    gate = _load_gate(monkeypatch, tmp_path)
    calls: list[list[str]] = []
    pr = {"number": 7589, "headRefOid": HEAD, "headRefName": "fix/7589-observability"}

    def fake_run(command: list[str], *, timeout: int = 60) -> str:
        del timeout
        calls.append(command)
        if command[:3] == ["gh", "pr", "view"]:
            return json.dumps(pr)
        if command[0] == "python3":
            return json.dumps(_scope(ui_changed=False))
        raise AssertionError(f"unexpected effect before non-UI decision: {command}")

    monkeypatch.setattr(gate, "run", fake_run)
    result = gate.live_evaluate(_args())
    assert result["ok"] is True
    assert result["reason"] == "non_ui_change"
    assert not any(command[:3] == ["gh", "issue", "view"] for command in calls)
    classifier_calls = [command for command in calls if command[0] == "python3"]
    assert len(classifier_calls) == 1
    assert classifier_calls[0][classifier_calls[0].index("--expected-head") + 1] == HEAD


def test_authoritative_classifier_output_for_exact_7589_head_is_consumable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    classifier = Path("/Users/rajiv/Downloads/projects/heydonna-app/scripts/ci/change_scope.py")
    rules = classifier.with_name("change-scope-rules.json")
    completed = subprocess.run(
        [
            "python3", str(classifier), "--rules", str(rules),
            "--repo", "heydonna-app/heydonna-app",
            "--path", "scripts/ci/observability.py",
            "--expected-head", HEAD,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    scope = json.loads(completed.stdout)
    assert scope["head"] == HEAD
    assert scope["rules_sha256"] == RULES_SHA256
    gate = _load_gate(monkeypatch, tmp_path)
    validated = gate.validate_change_scope(scope, expected_head=HEAD)
    assert validated["ui_changed"] is False


def test_ui_live_path_still_resolves_issue_after_classifier(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    gate = _load_gate(monkeypatch, tmp_path)
    calls: list[list[str]] = []
    pr = {
        "number": 7589,
        "headRefOid": HEAD,
        "headRefName": "fix/7589-observability",
        "closingIssuesReferences": [{"number": 7589}],
        "comments": [],
    }

    def fake_run(command: list[str], *, timeout: int = 60) -> str:
        del timeout
        calls.append(command)
        if command[:3] == ["gh", "pr", "view"]:
            return json.dumps(pr)
        if command[0] == "python3":
            return json.dumps(_scope(ui_changed=True))
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"number": 7589, "body": "## What to Build\n\n## Why\n", "state": "OPEN"})
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(gate, "run", fake_run)
    result = gate.live_evaluate(_args())
    assert result["ok"] is True
    assert result["reason"] == "legacy_issue_contract_accepted"
    assert any(command[:3] == ["gh", "issue", "view"] for command in calls)


def test_ui_without_issue_metadata_remains_fail_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    gate = _load_gate(monkeypatch, tmp_path)
    pr = {"number": 7589, "headRefOid": HEAD, "headRefName": "observability-fix"}

    def fake_run(command: list[str], *, timeout: int = 60) -> str:
        del timeout
        if command[:3] == ["gh", "pr", "view"]:
            return json.dumps(pr)
        if command[0] == "python3":
            return json.dumps(_scope(ui_changed=True))
        raise AssertionError(f"issue resolution should fail before {command}")

    monkeypatch.setattr(gate, "run", fake_run)
    with pytest.raises(RuntimeError, match="cannot resolve implementation issue"):
        gate.live_evaluate(_args())


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", None, "schema is unsupported"),
        ("head", "a" * 40, "head mismatch"),
        ("head", None, "head mismatch"),
        ("rules_sha256", "b" * 64, "rules digest mismatch"),
        ("changed_files", [], "empty or malformed"),
        ("scope", "unknown", "scope is unknown"),
        ("ui_changed", "false", "ui_changed is malformed"),
        ("ownership", {}, "ownership is missing or malformed"),
    ],
)
def test_classifier_authority_drift_fails_closed(field: str, value: object, message: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    gate = _load_gate(monkeypatch, tmp_path)
    scope = _scope(ui_changed=False)
    scope[field] = value
    with pytest.raises(RuntimeError, match=message):
        gate.validate_change_scope(scope, expected_head=HEAD)


def test_manifest_visual_gate_maps_exactly_three_targets() -> None:
    """The payload resolves to exactly canonical + user-level + app-local sibling."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    entry = next(e for e in manifest["entries"] if e["source_path"] == "claude/scripts/qa-visual-proof-gate.py")
    targets = [entry["canonical_target"], *entry.get("additional_targets", [])]
    assert targets == [APP_TARGET, INSTALLED_TARGET, APP_LOCAL_TARGET]
    assert len(targets) == len(set(targets))


def _load_installer():
    installer_path = ROOT / "scripts" / "install-release.py"
    spec = importlib.util.spec_from_file_location("install_release_visual_asset", installer_path)
    assert spec and spec.loader
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    return installer


def _qa_targets(root: Path) -> dict[str, Path]:
    return {
        "app": root / APP_TARGET.lstrip("/"),
        "installed": root / INSTALLED_TARGET.lstrip("/"),
        "app_local": root / APP_LOCAL_TARGET.lstrip("/"),
    }


def _seed_targets(targets: dict[str, Path], preimages: dict[str, bytes]) -> None:
    for name, path in targets.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(preimages[name])
        path.chmod(0o755)


def _copy_release(tmp_path: Path) -> Path:
    release = tmp_path / "release"
    shutil.copytree(SHARED, release / "scripts" / "pm" / "shared-assets")
    return release


_PREIMAGES = {
    "app": b"stale app target preimage\n",
    "installed": b"stale installed target preimage\n",
    "app_local": b"RETIRED stale app-local sibling preimage\n",
}


def test_installer_installs_all_three_targets_and_rolls_back_exact_preimages(tmp_path: Path) -> None:
    installer = _load_installer()
    release = _copy_release(tmp_path)
    targets = tmp_path / "targets"
    qa = _qa_targets(targets)
    _seed_targets(qa, _PREIMAGES)

    rollback = tmp_path / "rollback"
    result = installer.install_shared_assets(release_dir=release, target_root=targets, rollback_bundle=rollback)
    assert result["status"] == "SHARED_ASSETS_INSTALLED"
    source_bytes = SOURCE.read_bytes()
    for path in qa.values():
        assert path.read_bytes() == source_bytes
        assert stat.S_IMODE(path.stat().st_mode) == 0o755

    checked = installer.check_shared_assets(release_dir=release, target_root=targets)
    assert checked["status"] == "SHARED_ASSETS_PASS"
    for path in qa.values():
        assert str(path) in checked["targets"]

    installer.restore_rollback_bundle(rollback)
    for name, path in qa.items():
        assert path.read_bytes() == _PREIMAGES[name]
        assert stat.S_IMODE(path.stat().st_mode) == 0o755


def test_installer_rollback_restores_only_replaced_targets_on_partial_failure(tmp_path: Path) -> None:
    installer = _load_installer()
    release = _copy_release(tmp_path)
    targets = tmp_path / "targets"
    qa = _qa_targets(targets)
    _seed_targets(qa, _PREIMAGES)
    # An unrelated, unlisted file must survive untouched.
    unrelated = targets / "unrelated/keep.txt"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"keep me\n")

    rollback = tmp_path / "rollback"
    with pytest.raises(installer.InstallerError):
        installer.install_shared_assets(
            release_dir=release, target_root=targets, rollback_bundle=rollback, fail_after=2
        )
    # Every target restored to its exact preimage and the unlisted file is intact.
    for name, path in qa.items():
        assert path.read_bytes() == _PREIMAGES[name]
    assert unrelated.read_bytes() == b"keep me\n"


def test_installer_refuses_target_symlink_and_rolls_back(tmp_path: Path) -> None:
    installer = _load_installer()
    release = _copy_release(tmp_path)
    targets = tmp_path / "targets"
    qa = _qa_targets(targets)
    _seed_targets(qa, _PREIMAGES)
    # Replace the app-local target with a symlink to an outside file.
    decoy = tmp_path / "decoy.py"
    decoy.write_bytes(b"decoy\n")
    app_local = qa["app_local"]
    app_local.unlink()
    app_local.symlink_to(decoy)

    rollback = tmp_path / "rollback"
    with pytest.raises(installer.InstallerError):
        installer.install_shared_assets(release_dir=release, target_root=targets, rollback_bundle=rollback)
    # No target was left changed by the refused install.
    assert qa["app"].read_bytes() == _PREIMAGES["app"]
    assert qa["installed"].read_bytes() == _PREIMAGES["installed"]
    assert app_local.is_symlink()
    assert decoy.read_bytes() == b"decoy\n"


def test_restore_rollback_refuses_corrupt_payload_without_unrelated_change(tmp_path: Path) -> None:
    installer = _load_installer()
    release = _copy_release(tmp_path)
    targets = tmp_path / "targets"
    qa = _qa_targets(targets)
    _seed_targets(qa, _PREIMAGES)

    rollback = tmp_path / "rollback"
    installer.install_shared_assets(release_dir=release, target_root=targets, rollback_bundle=rollback)

    # Corrupt one backup payload so the bundle no longer matches its record.
    payloads = sorted((rollback / "payload").iterdir())
    payloads[-1].write_bytes(b"corrupt payload\n")

    with pytest.raises(installer.InstallerError):
        installer.restore_rollback_bundle(rollback)
    # The preflight refusal never partially restored an earlier target.
    source_bytes = SOURCE.read_bytes()
    assert qa["app"].read_bytes() == source_bytes
    assert qa["installed"].read_bytes() == source_bytes
    assert qa["app_local"].read_bytes() == source_bytes
