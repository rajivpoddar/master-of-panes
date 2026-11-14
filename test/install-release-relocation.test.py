"""Manifest-declared shared-asset relocation: a target may leave the shared manifest
only when the candidate retirements.json declares it DELETE with relocated_to."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    "relocation_installer", Path(__file__).parents[1] / "scripts/install-release.py"
)
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)

DIGEST = "a" * 64
OLD = "/home/u/.claude/skills/message-slot/SKILL.md"
NEW = "/home/u/app/.claude/skills/message-slot/SKILL.md"


def retirement(**overrides):
    item = {
        "absolute_current_path": OLD,
        "disposition": "DELETE",
        "preimage_sha256": DIGEST,
        "preimage_mode": 0o644,
        "installed_root": "/home/u/.claude/skills",
        "replacement": "PM-only skills install under the app .claude/skills directory.",
        "relocated_to": NEW,
    }
    item.update(overrides)
    return item


class RelocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.release = Path(self.temp.name)
        self.baseline = {OLD: {"sha256": DIGEST, "mode": 0o644}}
        self.candidate = {NEW}

    def write(self, *items):
        path = self.release / m.RETIREMENT_MANIFEST
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema": "mop_shared_retirements", "version": 1,
            "items": sorted(items, key=lambda item: item["absolute_current_path"]),
        }))

    def relocated(self):
        return m._relocated_baseline_targets(self.release, self.baseline, self.candidate)

    def test_declared_relocation_with_matching_preimage_is_allowed(self):
        self.write(retirement())
        self.assertEqual(self.relocated(), {OLD})

    def test_missing_retirement_manifest_allows_nothing(self):
        self.assertEqual(self.relocated(), set())

    def test_preimage_digest_mismatch_is_not_a_relocation(self):
        self.write(retirement(preimage_sha256="b" * 64))
        self.assertEqual(self.relocated(), set())

    def test_preimage_mode_mismatch_is_not_a_relocation(self):
        self.write(retirement(preimage_mode=0o755))
        self.assertEqual(self.relocated(), set())

    def test_destination_must_be_a_candidate_target(self):
        self.write(retirement(relocated_to="/home/u/app/.claude/skills/other/SKILL.md"))
        self.assertEqual(self.relocated(), set())

    def test_plain_retirement_without_destination_is_not_a_relocation(self):
        item = retirement()
        del item["relocated_to"]
        self.write(item)
        self.assertEqual(self.relocated(), set())

    def test_hold_disposition_is_not_a_relocation(self):
        self.write(retirement(disposition="HOLD_UNTIL_OPERATOR_LIVE"))
        self.assertEqual(self.relocated(), set())

    def test_target_still_in_candidate_is_not_a_removal(self):
        self.candidate = {OLD, NEW}
        self.write(retirement())
        self.assertEqual(self.relocated(), set())

    def test_target_absent_from_baseline_is_ignored(self):
        self.baseline = {}
        self.write(retirement())
        self.assertEqual(self.relocated(), set())

    def test_synced_live_edit_preimage_equal_to_destination_is_a_relocation(self):
        self.write(retirement(preimage_sha256="b" * 64))
        digests = {NEW: ("b" * 64, 0o644)}
        records = m._relocation_records(self.release, self.baseline, self.candidate, digests)
        self.assertEqual(records, {OLD: {"path": OLD, "kind": "file", "mode": 0o644, "sha256": "b" * 64}})
        self.assertEqual(m._relocated_baseline_targets(self.release, self.baseline, self.candidate, digests), {OLD})

    def test_preimage_matching_neither_baseline_nor_destination_is_not_a_relocation(self):
        self.write(retirement(preimage_sha256="c" * 64))
        digests = {NEW: ("b" * 64, 0o644)}
        self.assertEqual(m._relocated_baseline_targets(self.release, self.baseline, self.candidate, digests), set())

    def test_destination_digest_is_not_consulted_when_not_supplied(self):
        self.write(retirement(preimage_sha256="b" * 64))
        self.assertEqual(self.relocated(), set())

    def test_invalid_destination_is_refused_by_the_manifest_loader(self):
        for bad in ("relative/path", "", OLD, "/home/u/../etc/passwd", 7):
            with self.subTest(destination=bad):
                self.write(retirement(relocated_to=bad))
                with self.assertRaises(m.InstallerError):
                    m._load_retirement_manifest(self.release)


if __name__ == "__main__":
    unittest.main()
