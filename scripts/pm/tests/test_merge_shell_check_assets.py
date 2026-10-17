"""Static mapping and sandbox-only scoped rollout proof for merge instructions."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[3]
MANIFEST = Path("scripts/pm/shared-assets/manifest.json")
SKILLS = ("heydonna-cto-pr-merge", "heydonna-open-pr-status")
SOURCES = {f"codex/skills/{name}/SKILL.md" for name in SKILLS}
SPEC = importlib.util.spec_from_file_location("shell_check_installer", ROOT / "scripts/install-release.py")
assert SPEC and SPEC.loader
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def scoped_manifest() -> dict:
    manifest = json.loads((ROOT / MANIFEST).read_text())
    manifest["entries"] = [e for e in manifest["entries"] if e["source_path"] in SOURCES]
    manifest["inventory"]["selected_count"] = len(manifest["entries"])
    manifest["rollback_compatibility"] = []
    return manifest


def stage(tmp_path: Path):
    candidate, baseline, installed = (tmp_path / name for name in ("candidate", "baseline", "installed"))
    new, old = scoped_manifest(), scoped_manifest()
    for entry, prior in zip(new["entries"], old["entries"]):
        source = Path("scripts/pm/shared-assets") / entry["source_path"]
        data = (ROOT / source).read_bytes()
        target = candidate / source
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(entry["mode"])
        preimage = f"previous {entry['source_path']}\n".encode()
        prior["sha256"] = digest(preimage)
        target = installed / entry["canonical_target"].lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(preimage)
        target.chmod(prior["mode"])
    for root, value in ((candidate, new), (baseline, old)):
        (root / MANIFEST).parent.mkdir(parents=True, exist_ok=True)
        (root / MANIFEST).write_text(json.dumps(value))
    return candidate, baseline, installed, new, old


def test_manifest_payloads_and_single_shared_shell_reference():
    manifest = INSTALLER._load_shared_manifest(ROOT)
    entries = [e for e in manifest["entries"] if e["source_path"] in SOURCES]
    assert len(entries) == 2
    for entry in entries:
        name = Path(entry["source_path"]).parent.name
        assert entry["canonical_target"] == f"/Users/rajiv/.codex/skills/{name}/SKILL.md"
        assert entry["mode"] == 0o644
    shared = ROOT / "scripts/pm/shared-assets/codex/skills"
    ordinary = (shared / SKILLS[0] / "SKILL.md").read_text()
    exempt = (shared / SKILLS[1] / "SKILL.md").read_text()
    assert ordinary.count("**SHELL_CHECK (within this visual verdict):**") == 1
    assert "../heydonna-cto-pr-merge/SKILL.md#4-visual-proof-matches-the-shipped-ui" in exempt
    assert "### 4. Visual proof matches the shipped UI" in ordinary
    assert "DashboardSideBar" not in exempt  # No second drifting shell rule.


def test_scoped_install_readback_and_guarded_rollback_preserve_unrelated_files(tmp_path):
    candidate, baseline, installed, new, old = stage(tmp_path)
    sentinel = installed / "unrelated-skill.md"
    sentinel.write_bytes(b"another owner's edit\n")
    allowed = INSTALLER.assert_no_live_drift(
        active_release=baseline, candidate_root=candidate, target_root=installed,
    )
    rollback = tmp_path / "rollback"
    INSTALLER.install_shared_assets(
        release_dir=candidate, target_root=installed, rollback_bundle=rollback,
        allowed_preimages=allowed,
    )
    INSTALLER.check_shared_assets(release_dir=candidate, target_root=installed)
    for entry in new["entries"]:
        target = installed / entry["canonical_target"].lstrip("/")
        assert digest(target.read_bytes()) == entry["sha256"]
        assert target.stat().st_mode & 0o777 == 0o644
    INSTALLER.restore_rollback_bundle(rollback, only_if_current={
        str(installed / e["canonical_target"].lstrip("/")): e["sha256"] for e in new["entries"]
    })
    for entry in old["entries"]:
        assert digest((installed / entry["canonical_target"].lstrip("/")).read_bytes()) == entry["sha256"]
    assert sentinel.read_bytes() == b"another owner's edit\n"


def test_live_drift_refused_without_overwrite_permission(tmp_path):
    candidate, baseline, installed, new, _old = stage(tmp_path)
    target = installed / new["entries"][0]["canonical_target"].lstrip("/")
    target.write_bytes(b"concurrent instruction edit\n")
    with pytest.raises(INSTALLER.InstallerError, match="live drift"):
        INSTALLER.assert_no_live_drift(
            active_release=baseline, candidate_root=candidate, target_root=installed,
        )
    assert target.read_bytes() == b"concurrent instruction edit\n"


def test_rollback_preserves_newer_installed_edits(tmp_path):
    candidate, baseline, installed, new, _old = stage(tmp_path)
    allowed = INSTALLER.assert_no_live_drift(
        active_release=baseline, candidate_root=candidate, target_root=installed,
    )
    rollback = tmp_path / "rollback"
    INSTALLER.install_shared_assets(
        release_dir=candidate, target_root=installed, rollback_bundle=rollback,
        allowed_preimages=allowed,
    )
    target = installed / new["entries"][0]["canonical_target"].lstrip("/")
    target.write_bytes(b"newer approved change\n")
    INSTALLER.restore_rollback_bundle(rollback, only_if_current={
        str(installed / e["canonical_target"].lstrip("/")): e["sha256"] for e in new["entries"]
    })
    assert target.read_bytes() == b"newer approved change\n"
