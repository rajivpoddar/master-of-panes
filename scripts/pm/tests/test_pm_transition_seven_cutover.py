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
    by_path = {item["source_path"]: item for item in entries}
    assert manifest["inventory"]["selected_count"] == len(entries)
    assert manifest["inventory"]["command_inventory"] == []
    axiom = by_path["claude/scripts/axiom-activity-report.py"]
    assert axiom["canonical_target"] == "/Users/rajiv/.claude/scripts/axiom-activity-report.py"
    assert axiom["mode"] == 493
    assert axiom["sha256"] == "8b67ea060d935f4be465aa342b0f3fe00e86950ce0ccbb80a58b1e2cc1d94abd"
    assert "claude/scripts/launch-dev-slot-claude.sh" in by_path
    for slot in range(1, 7):
        launcher = by_path[f"claude/scripts/launch-slot-{slot}.sh"]
        assert launcher["canonical_target"] == f"/Users/rajiv/.claude/scripts/launch-slot-{slot}.sh"
        assert launcher["mode"] == 493
    assert "claude/scripts/sync-dev-slot-skill-allowlist.mjs" in by_path
    assert {
        "codex/skills/codex-stdio-send-message/SKILL.md",
        "codex/skills/codex-stdio-send-message/agents/openai.yaml",
        "codex/skills/codex-stdio-send-message/scripts/send_message.py",
        "codex/skills/codex-stdio-send-message/scripts/test_send_message.py",
    } <= by_path.keys()
    assert all("claude/scripts/pm-operator.py" not in item["source_path"] for item in entries)
    assert all("claude/scripts/pm-transition.sh" not in item["source_path"] for item in entries)


def test_retired_pm_payloads_are_absent_from_mop_source() -> None:
    assert not (SHARED / "claude/scripts/pm-operator.py").exists()
    assert not (SHARED / "claude/scripts/pm-transition.sh").exists()


def test_manifest_is_sorted_and_matches_committed_json() -> None:
    parsed = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    paths = [item["source_path"] for item in parsed["entries"]]
    assert paths == sorted(paths)
