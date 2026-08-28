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


def test_shared_manifest_contains_versioned_stdio_and_axiom_assets() -> None:
    manifest = INSTALLER._load_shared_manifest(ROOT)
    entries = manifest["entries"]
    assert manifest["inventory"]["selected_count"] == 9
    assert manifest["inventory"]["command_inventory"] == []
    assert len(entries) == 9
    assert entries[0]["source_path"] == "claude/scripts/axiom-activity-report.py"
    assert entries[0]["canonical_target"] == "/Users/rajiv/.claude/scripts/axiom-activity-report.py"
    assert entries[0]["mode"] == 493
    assert entries[0]["sha256"] == "8b67ea060d935f4be465aa342b0f3fe00e86950ce0ccbb80a58b1e2cc1d94abd"
    assert entries[1]["source_path"] == "claude/scripts/launch-dev-slot-claude.sh"
    assert entries[2]["source_path"] == "claude/scripts/launch-slot-5.sh"
    assert entries[3]["source_path"] == "claude/scripts/launch-slot-6.sh"
    assert entries[4]["source_path"] == "claude/scripts/sync-dev-slot-skill-allowlist.mjs"
    assert all(item["source_path"].startswith("codex/skills/codex-stdio-send-message/") for item in entries[5:])
    assert all("claude/scripts/pm-operator.py" not in item["source_path"] for item in entries)
    assert all("claude/scripts/pm-transition.sh" not in item["source_path"] for item in entries)


def test_retired_pm_payloads_are_absent_from_mop_source() -> None:
    assert not (SHARED / "claude/scripts/pm-operator.py").exists()
    assert not (SHARED / "claude/scripts/pm-transition.sh").exists()


def test_manifest_is_sorted_and_matches_committed_json() -> None:
    parsed = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    paths = [item["source_path"] for item in parsed["entries"]]
    assert paths == sorted(paths)
