from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets"
MANIFEST_PATH = SHARED / "manifest.json"
INSTALLER_PATH = ROOT / "scripts" / "install-release.py"
SPEC = importlib.util.spec_from_file_location("install_release_retirement", INSTALLER_PATH)
assert SPEC and SPEC.loader
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)


def test_shared_manifest_contains_only_versioned_stdio_skill_assets() -> None:
    manifest = INSTALLER._load_shared_manifest(ROOT)
    entries = manifest["entries"]
    assert manifest["inventory"]["selected_count"] == 4
    assert manifest["inventory"]["command_inventory"] == []
    assert len(entries) == 4
    assert all(item["source_path"].startswith("codex/skills/codex-stdio-send-message/") for item in entries)
    assert all("claude/scripts/pm-operator.py" not in item["source_path"] for item in entries)
    assert all("claude/scripts/pm-transition.sh" not in item["source_path"] for item in entries)


def test_retired_pm_payloads_are_absent_from_mop_source() -> None:
    assert not (SHARED / "claude/scripts/pm-operator.py").exists()
    assert not (SHARED / "claude/scripts/pm-transition.sh").exists()


def test_manifest_is_sorted_and_matches_committed_json() -> None:
    parsed = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    paths = [item["source_path"] for item in parsed["entries"]]
    assert paths == sorted(paths)
