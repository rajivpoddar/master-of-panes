from __future__ import annotations

import hashlib
import importlib.util
import json
import stat
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "shared-assets" / "claude" / "scripts" / "pm" / "control-plane" / "sakshi-heartbeat.py"
MANIFEST = ROOT / "shared-assets" / "manifest.json"


def canonical_parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


SPEC = importlib.util.spec_from_file_location("sakshi_latch_detector", SOURCE)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules["sakshi_latch_detector"] = MODULE
runtime_observation = types.ModuleType("control_plane.runtime_observation")
runtime_observation.RuntimeObservationAdapter = object
runtime_observation.parse_timestamp = canonical_parse_timestamp
control_plane = types.ModuleType("control_plane")
control_plane.runtime_observation = runtime_observation
sys.modules["control_plane"] = control_plane
sys.modules["control_plane.runtime_observation"] = runtime_observation
SPEC.loader.exec_module(MODULE)


def receipt(path: Path, status: str) -> None:
    path.write_text(json.dumps({
        "schema": "mop_issue_create_effect_v1",
        "effect": "issue_create",
        "status": status,
        "effect_id": f"issue-create:{status}",
        "request_digest": "a" * 64,
        "issue": 7609,
    }), encoding="utf-8")


class PostIssueLatchDetectorTests(unittest.TestCase):
    def test_worktrees_and_rollback_preimage_directories_are_not_latches(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            for name in (
                "mop-issue-guard-v1.ghZ2jB",
                "mop-issue-triage-automation-rollback-ee9c7f5",
                "mop-issue-triage-rollback-ee9c7f5",
                "mop-issue-triage-rollback-ee9c7f5-1788197013",
            ):
                directory = root / name
                directory.mkdir()
                (directory / "preimage").write_text("not an issue receipt", encoding="utf-8")
            self.assertEqual(MODULE.collect_post_issue_latches(root), {"count": 0, "paths": []})

    def test_unresolved_receipts_are_actionable_and_completed_are_not(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            for status in ("reserved", "effect_started", "ambiguous"):
                receipt(root / f"mop-issue-create-{status}.json", status)
            receipt(root / "mop-issue-create-completed.json", "completed")
            (root / "mop-issue-create-malformed.json").write_text("{}", encoding="utf-8")
            (root / "mop-issue-create-invalid-json.json").write_text("{not-json", encoding="utf-8")
            (root / "mop-issue-triage-rollback.json").write_text("{}", encoding="utf-8")
            result = MODULE.collect_post_issue_latches(root)
            self.assertEqual(result["count"], 3)
            self.assertEqual(
                sorted(Path(path).name for path in result["paths"]),
                [
                    "mop-issue-create-ambiguous.json",
                    "mop-issue-create-effect_started.json",
                    "mop-issue-create-reserved.json",
                ],
            )

    def test_numeric_and_unknown_fallback_flags_are_included_once(self) -> None:
        import tempfile

        contents = "ISSUE: #7609\nCREATED_AT: 2026-09-03T12:55:00Z\nSWEEP_REQUIRED: yes\n"
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            numeric = root / "post-issue-create-sweep-7609.flag"
            unknown = root / "post-issue-create-sweep-unknown-1788434823.flag"
            numeric.write_text(contents, encoding="utf-8")
            unknown.write_text(contents, encoding="utf-8")
            result = MODULE.collect_post_issue_latches(root)
            self.assertEqual(result["count"], 2)
            (unknown.with_name(f"{unknown.name}.resolved")).touch()
            result = MODULE.collect_post_issue_latches(root)
            self.assertEqual(result, {"count": 1, "paths": [str(numeric)]})

    def test_legacy_flags_require_exact_fields_timestamp_and_filename_binding(self) -> None:
        import tempfile

        cases = {
            "post-issue-create-sweep-7609.flag": "ISSUE: #7609\nCREATED_AT: not-a-timestamp\nSWEEP_REQUIRED: yes\n",
            "post-issue-create-sweep-7608.flag": "ISSUE: #7609\nCREATED_AT: 2026-09-03T12:55:00Z\nSWEEP_REQUIRED: yes\n",
            "post-issue-create-sweep-7607.flag": "ISSUE: #7607\nISSUE: #7607\nCREATED_AT: 2026-09-03T12:55:00Z\nSWEEP_REQUIRED: yes\n",
            "post-issue-create-sweep-7606.flag": "ISSUE: #7606\nCREATED_AT: 2026-09-03T12:55:00Z\nSWEEP_REQUIRED: no\n",
            "post-issue-create-sweep-7605.flag": "ISSUE: #7605\nCREATED_AT: 2026-09-03T12:55:00Z\nSWEEP_REQUIRED: yes\nEXTRA: no\n",
            "post-issue-create-sweep-7604.flag": "ISSUE: #7604\nCREATED_AT: 2026-09-03T12:55:00Z\n",
        }
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            for name, content in cases.items():
                (root / name).write_text(content, encoding="utf-8")
            valid_unknown = root / "post-issue-create-sweep-unknown-1788434823.flag"
            valid_unknown.write_text(
                "ISSUE: #7609\nCREATED_AT: 2026-09-03T12:55:00Z\nSWEEP_REQUIRED: yes\n",
                encoding="utf-8",
            )
            self.assertEqual(MODULE.collect_post_issue_latches(root), {"count": 1, "paths": [str(valid_unknown)]})

    def test_symlink_directory_and_filename_mismatch_are_excluded(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            source = root / "source"
            source.write_text(
                "ISSUE: #7609\nCREATED_AT: 2026-09-03T12:55:00Z\nSWEEP_REQUIRED: yes\n",
                encoding="utf-8",
            )
            (root / "post-issue-create-sweep-7609.flag").symlink_to(source)
            (root / "post-issue-create-sweep-7609.flag.resolved").touch()
            (root / "post-issue-create-sweep-7609.flag.directory").mkdir()
            (root / "post-issue-create-sweep-7608.flag").write_text(
                "ISSUE: #7609\nCREATED_AT: 2026-09-03T12:55:00Z\nSWEEP_REQUIRED: yes\n",
                encoding="utf-8",
            )
            self.assertEqual(MODULE.collect_post_issue_latches(root), {"count": 0, "paths": []})

    def test_latch_detector_manifest_source_parity(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        entries = {entry["source_path"]: entry for entry in manifest["entries"]}
        entry = entries["claude/scripts/pm/control-plane/sakshi-heartbeat.py"]
        self.assertEqual(hashlib.sha256(SOURCE.read_bytes()).hexdigest(), entry["sha256"])
        self.assertEqual(stat.S_IMODE(SOURCE.stat().st_mode), 0o755)
        self.assertEqual(entry["mode"], 0o755)
        self.assertEqual(entry["canonical_target"], "/Users/rajiv/.claude/scripts/sakshi-heartbeat.py")


if __name__ == "__main__":
    unittest.main()
