"""Hermetic proof for the managed cleanup-pr Step 4 slot-label enumeration.

Exercises the exact Step 4 bash fence from the adopted shared-asset
SKILL.md against a stubbed `gh`, plus the manifest mapping and the
canonical installer loader acceptance. No network, no live mutation.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).parents[3]
SKILL = REPO_ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "skills" / "cleanup-pr" / "SKILL.md"
MANIFEST = REPO_ROOT / "scripts" / "pm" / "shared-assets" / "manifest.json"
MODULE_PATH = REPO_ROOT / "scripts" / "install-release.py"

SPEC = importlib.util.spec_from_file_location("install_release_cleanup_step4", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

LIVE_TARGET = "/Users/rajiv/Downloads/projects/heydonna-app/.claude/skills/cleanup-pr/SKILL.md"
SOURCE_PATH = "claude/skills/cleanup-pr/SKILL.md"

FAKE_GH = """#!/usr/bin/env python3
import json, os, re, sys
capture = os.environ["GH_CAPTURE"]
mode = os.environ.get("GH_MODE", "ok")
labels = json.loads(os.environ.get("GH_LABELS", "[]"))
args = sys.argv[1:]
if args[:2] == ["issue", "view"]:
    if mode == "read-fail":
        print("fake-gh: label read failed", file=sys.stderr)
        sys.exit(1)
    # Emulate `gh --jq -r '.labels[].name | select(test(...))'`: the real
    # pipeline, not the stub, applies the filter; the stub only stands in
    # for transport. The snippet's filter text is pinned by a separate test.
    if "--jq" in args:
        selected = [name for name in labels if re.match(r"^slot:[0-9]+$", name)]
        if selected:
            print("\\n".join(selected))
    else:
        print(json.dumps({"labels": [{"name": name} for name in labels]}))
elif args[:2] == ["issue", "edit"]:
    with open(capture, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(args) + "\\n")
else:
    print("fake-gh: unexpected invocation", file=sys.stderr)
    sys.exit(2)
"""


def extract_step4() -> str:
    text = SKILL.read_text(encoding="utf-8")
    match = re.search(r"### Step 4: Update labels\n\n.*?\n```bash\n(.*?)```", text, re.S)
    assert match, "Step 4 bash fence missing from adopted SKILL.md"
    return match.group(1)


def run_step4(labels: list[str], mode: str = "ok") -> tuple[int, list[list[str]]]:
    snippet = extract_step4()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        fake_gh = root / "gh"
        fake_gh.write_text(FAKE_GH, encoding="utf-8")
        fake_gh.chmod(0o755)
        capture = root / "edits.jsonl"
        capture.write_text("", encoding="utf-8")
        env = dict(os.environ)
        env["PATH"] = str(root) + os.pathsep + env.get("PATH", "")
        env["GH_CAPTURE"] = str(capture)
        env["GH_LABELS"] = json.dumps(labels)
        env["GH_MODE"] = mode
        env["ISSUE"] = "123"
        completed = subprocess.run(
            ["bash", "-c", snippet], capture_output=True, text=True, env=env, timeout=30
        )
        edits = [
            json.loads(line)
            for line in capture.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return completed.returncode, edits


def removed_labels(edit_argv: list[str]) -> list[str]:
    values: list[str] = []
    for index, token in enumerate(edit_argv):
        if token == "--remove-label":
            values.append(edit_argv[index + 1])
    return values


class Step4SlotEnumerationTests(unittest.TestCase):
    def test_step4_derives_from_live_labels_not_a_fixed_list(self) -> None:
        snippet = extract_step4()
        self.assertIn('^slot:[0-9]+$', snippet)
        self.assertNotIn('"slot:1"', snippet)
        self.assertNotIn('"slot:4"', snippet)

    def test_slot_5_6_and_higher_are_removed(self) -> None:
        code, edits = run_step4(["status:in-progress", "slot:5", "slot:6", "slot:12", "priority:p1"])
        self.assertEqual(code, 0)
        self.assertEqual(len(edits), 1)
        removed = removed_labels(edits[0])
        for label in ("slot:5", "slot:6", "slot:12"):
            self.assertIn(label, removed)
        for label in ("status:todo", "status:in-progress", "status:in-review"):
            self.assertIn(label, removed)
        self.assertNotIn("priority:p1", removed)
        self.assertIn("--add-label", edits[0])
        self.assertIn("status:done", edits[0])

    def test_multiple_slot_labels_all_removed(self) -> None:
        code, edits = run_step4(["slot:1", "slot:2", "slot:9", "status:todo"])
        self.assertEqual(code, 0)
        removed = removed_labels(edits[0])
        for label in ("slot:1", "slot:2", "slot:9"):
            self.assertIn(label, removed)

    def test_zero_slot_labels_still_transitions_status(self) -> None:
        code, edits = run_step4(["status:in-progress", "priority:p1"])
        self.assertEqual(code, 0, "idempotent replay with no slot labels must still succeed")
        self.assertEqual(len(edits), 1)
        removed = removed_labels(edits[0])
        self.assertIn("status:done", edits[0])
        self.assertFalse([label for label in removed if label.startswith("slot:")])

    def test_near_miss_labels_are_preserved(self) -> None:
        code, edits = run_step4(["slot:abc", "slot:", "Slot:5", "slot:5x", "slot:7"])
        self.assertEqual(code, 0)
        removed = removed_labels(edits[0])
        self.assertIn("slot:7", removed)
        for label in ("slot:abc", "slot:", "Slot:5", "slot:5x"):
            self.assertNotIn(label, removed)

    def test_read_failure_blocks_before_any_write(self) -> None:
        code, edits = run_step4(["slot:5"], mode="read-fail")
        self.assertNotEqual(code, 0, "a failed label read must fail closed")
        self.assertEqual(edits, [], "no edit may be attempted after a failed read")


class CleanupPrManifestTests(unittest.TestCase):
    def test_manifest_entry_pins_adopted_bytes(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        entries = manifest["entries"]
        matches = [entry for entry in entries if entry["source_path"] == SOURCE_PATH]
        self.assertEqual(len(matches), 1)
        entry = matches[0]
        self.assertEqual(entry["canonical_target"], LIVE_TARGET)
        self.assertEqual(entry["ownership_class"], "shared-claude-pm-cleanup")
        self.assertEqual(entry["dependency_status"], "closed")
        self.assertEqual(entry["dependencies"], [])
        self.assertEqual(entry["mode"], 0o644)
        digest = MODULE.sha256(SKILL)
        self.assertEqual(entry["sha256"], digest)
        self.assertEqual(stat.S_IMODE(SKILL.stat().st_mode), entry["mode"])
        self.assertEqual(
            [item["source_path"] for item in entries],
            sorted(item["source_path"] for item in entries),
            "manifest entries must stay deterministically sorted",
        )
        self.assertEqual(manifest["inventory"]["selected_count"], len(entries))

    def test_canonical_loader_accepts_manifest(self) -> None:
        MODULE._load_shared_manifest(REPO_ROOT)


if __name__ == "__main__":
    unittest.main()
