from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import stat
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).parents[3]
MANIFEST_PATH = REPO_ROOT / "scripts" / "pm" / "shared-assets" / "manifest.json"
STALE_MCP = Path(
    "/Users/rajiv/.claude/plugins/cache/rajiv-plugins/master-of-panes/1.0.0/dist/mcp.js"
)
STALE_MCP_SHA256 = "1265561cb760f5323c3ab38c1e2a7bae6d4e9195dfa62f6c3cb3d935347b809c"
MCP_RUNTIME = {
    "asyncCommand.js",
    "assignmentAuthority.js",
    "db.js",
    "mcp.js",
    "paneIdentity.js",
    "relay.js",
    "slotConfig.js",
    "types.js",
}


def load_installer():
    path = REPO_ROOT / "scripts" / "install-release.py"
    spec = importlib.util.spec_from_file_location("mop_mcp_install", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class McpSchemaInstallTests(unittest.TestCase):
    def test_manifest_binds_complete_six_slot_mcp_runtime(self) -> None:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        entries = {
            Path(entry["source_path"]).name: entry
            for entry in manifest["entries"]
            if entry["source_path"].startswith("claude/plugins/master-of-panes/1.0.0/dist/")
        }
        self.assertEqual(set(entries), MCP_RUNTIME)
        for name, entry in entries.items():
            source = MANIFEST_PATH.parent / entry["source_path"]
            self.assertEqual(digest(source), entry["sha256"], name)
            self.assertEqual(stat.S_IMODE(source.stat().st_mode), entry["mode"], name)
            self.assertTrue(entry["canonical_target"].endswith(f"/1.0.0/dist/{name}"))
            self.assertEqual(entry["dependency_status"], "closed")
            self.assertEqual(entry["dependencies"], [])

        mcp = (MANIFEST_PATH.parent / entries["mcp.js"]["source_path"]).read_text(encoding="utf-8")
        types = (MANIFEST_PATH.parent / entries["types.js"]["source_path"]).read_text(encoding="utf-8")
        self.assertIn("1-6", mcp)
        self.assertIn("0-6", mcp)
        self.assertNotIn("1-4", mcp)
        self.assertNotIn("0-4", mcp)
        self.assertIn("slotCount: 6", types)

    def test_disposable_install_replaces_stale_schema_and_dependencies(self) -> None:
        installer = load_installer()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            release = root / "release"
            shutil.copytree(REPO_ROOT / "scripts" / "pm" / "shared-assets", release / "scripts" / "pm" / "shared-assets")
            targets = root / "targets"
            result = installer.install_shared_assets(
                release_dir=release,
                target_root=targets,
                rollback_bundle=root / "rollback",
            )
            self.assertEqual(result["status"], "SHARED_ASSETS_INSTALLED")
            checked = installer.check_shared_assets(release_dir=release, target_root=targets)
            self.assertEqual(checked["status"], "SHARED_ASSETS_PASS")
            for name in MCP_RUNTIME:
                target = targets / "Users/rajiv/.claude/plugins/cache/rajiv-plugins/master-of-panes/1.0.0/dist" / name
                self.assertTrue(target.is_file())
                self.assertEqual(digest(target), digest(release / "scripts/pm/shared-assets/claude/plugins/master-of-panes/1.0.0/dist" / name))
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_red_snapshot_records_current_static_schema_when_present(self) -> None:
        if not STALE_MCP.is_file() or digest(STALE_MCP) != STALE_MCP_SHA256:
            self.skipTest("static 1.0.0 schema has already been replaced")
        text = STALE_MCP.read_text(encoding="utf-8")
        self.assertIn("all 4 dev slots", text)
        self.assertIn("max(4)", text)


if __name__ == "__main__":
    unittest.main()
