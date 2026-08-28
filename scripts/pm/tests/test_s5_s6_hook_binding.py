#!/usr/bin/env python3
"""Regression proof for the S5/S6 Claude hook install boundary."""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets"
MANIFEST = SHARED / "manifest.json"
RELAY = SHARED / "claude" / "plugins" / "master-of-panes" / "scripts" / "hook-relay.sh"
HOOKS = SHARED / "claude" / "plugins" / "master-of-panes" / "hooks" / "hooks.json"


def test_plugin_hook_assets_are_bound_to_the_actual_claude_install_path() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    entries = {entry["source_path"]: entry for entry in manifest["entries"]}
    relay = entries["claude/plugins/master-of-panes/scripts/hook-relay.sh"]
    hooks = entries["claude/plugins/master-of-panes/hooks/hooks.json"]
    assert relay["canonical_target"].endswith("master-of-panes/1.0.0/scripts/hook-relay.sh")
    assert hooks["canonical_target"].endswith("master-of-panes/1.0.0/hooks/hooks.json")
    assert relay["mode"] == 0o755
    assert hooks["mode"] == 0o644


def test_s5_s6_are_relayable_by_the_versioned_hook() -> None:
    text = RELAY.read_text(encoding="utf-8")
    match = re.search(r"heydonna-app-300\((\[[^]]+\])\)", text)
    assert match is not None
    assert match.group(1) == "[1-6]"
    assert "hook-relay.sh" in HOOKS.read_text(encoding="utf-8")
