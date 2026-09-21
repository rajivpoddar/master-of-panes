"""Ruling 2026-09-21 (thread 1789994913.168479): post-merge housekeeping must not
transition slot ownership. The slot may have moved on by then; the correct control
point is the assignment boundary (assign/rebind CAS + /clear), not cleanup-pr."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SKILL = ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "skills" / "cleanup-pr" / "SKILL.md"

# An INVOCATION, not prose about the tool. Matches `mop_release_slot(...)`,
# `mop_release_slot(slot: N)`, or a release-endpoint POST - i.e. a call shape.
INVOCATION = re.compile(
    r"mop_release_slot\s*\(\s*(?:slot\s*[:=])?\s*[^)]*\)|/slots/[^\s`]*/release\b"
)


class CleanupPrNoRelease(unittest.TestCase):
    def test_cleanup_pr_never_invokes_release(self):
        text = SKILL.read_text()
        hits = [m.group(0) for m in INVOCATION.finditer(text)]
        self.assertEqual(hits, [], f"cleanup-pr must not invoke a release: {hits}")

    def test_cleanup_pr_names_the_assignment_boundary(self):
        text = SKILL.read_text()
        self.assertIn("assignment boundary", text)
        self.assertIn("/clear", text)

    def test_cleanup_pr_step5_is_not_a_release_step(self):
        text = SKILL.read_text()
        self.assertNotIn("Step 5: Release slot", text)


if __name__ == "__main__":
    unittest.main()
