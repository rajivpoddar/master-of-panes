#!/usr/bin/env python3
"""Focused proof for the runtime-and-branch contract in the app-review prompts.

Resolves the prompt files exactly the way the real reader does
(codex-review-companion.mjs loadPromptTemplate: skill dir + prompt.txt for an
initial review, prompt-rework.txt for a re-review) and asserts the contract is
present in all four code/QA initial+rework modes.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import unittest

ROOT = pathlib.Path(__file__).parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "skills"
MANIFEST = ROOT / "scripts" / "pm" / "shared-assets" / "manifest.json"
MARKER = "Runtime and branch coverage"
DIRECTIVE = "Ev0C3ECXCGJF"

MODES = {
    ("code", "initial"): "codex-app-code-review/templates/prompt.txt",
    ("code", "rework"): "codex-app-code-review/templates/prompt-rework.txt",
    ("qa", "initial"): "codex-app-qa-review/templates/prompt.txt",
    ("qa", "rework"): "codex-app-qa-review/templates/prompt-rework.txt",
}


def resolve(review_type: str, mode: str) -> pathlib.Path:
    """Mirror loadPromptTemplate's own resolution."""
    name = "prompt-rework.txt" if mode == "rework" else "prompt.txt"
    return SHARED / f"codex-app-{review_type}-review" / "templates" / name


class LoaderResolutionTests(unittest.TestCase):
    def test_every_mode_resolves_and_carries_the_contract(self):
        for (rt, mode), rel in MODES.items():
            with self.subTest(review=rt, mode=mode):
                path = resolve(rt, mode)
                self.assertTrue(path.is_file(), f"loader would hard-fail: {path}")
                body = path.read_text(encoding="utf-8")
                self.assertIn(MARKER, body)
                self.assertIn(DIRECTIVE, body)
                self.assertIn("actual execution runtime", body)
                self.assertIn("production-shaped state", body)
                self.assertIn("is not runtime-compatibility proof", body)

    def test_qa_rework_exists_where_it_previously_hard_failed(self):
        path = resolve("qa", "rework")
        self.assertTrue(path.is_file())
        self.assertIn("Re-review framing", path.read_text(encoding="utf-8"))


class SpecificsAreNotCrossTransplantedTests(unittest.TestCase):
    def test_code_specifics_only_in_the_code_prompts(self):
        for mode in ("initial", "rework"):
            code = resolve("code", mode).read_text(encoding="utf-8")
            self.assertIn("node` cannot make a", code.replace("`node`", "node`") + "`" if False else code)
            self.assertIn("Do NOT ban valid type imports", code)
            self.assertIn("Convex queries/mutations", code)
        for mode in ("initial", "rework"):
            qa = resolve("qa", mode).read_text(encoding="utf-8")
            self.assertNotIn("Do NOT ban valid type imports", qa, "code-only rule leaked into QA")

    def test_qa_specifics_only_in_the_qa_prompts(self):
        for mode in ("initial", "rework"):
            qa = resolve("qa", mode).read_text(encoding="utf-8")
            self.assertIn("convex-test", qa)
            self.assertIn("mocks/bypasses", qa)
            self.assertIn("never report it as PASS", qa)
        for mode in ("initial", "rework"):
            code = resolve("code", mode).read_text(encoding="utf-8")
            self.assertNotIn("never report it as PASS", code, "QA-only rule leaked into code")

    def test_rework_framing_is_scoped_correctly(self):
        # the new QA rework framing belongs only in the QA rework surface
        for rt in ("code", "qa"):
            self.assertNotIn("Re-review framing", resolve(rt, "initial").read_text(encoding="utf-8"))
        self.assertIn("Re-review framing", resolve("qa", "rework").read_text(encoding="utf-8"))
        # the code rework keeps its own pre-existing delta framing and must NOT be transplanted
        code_rw = resolve("code", "rework").read_text(encoding="utf-8")
        self.assertIn("DELTA REVIEW", code_rw)
        self.assertNotIn("Re-review framing", code_rw)


class ManagedRegistrationTests(unittest.TestCase):
    def test_exactly_four_rows_and_source_parity(self):
        import hashlib
        import stat
        entries = json.loads(MANIFEST.read_text(encoding="utf-8"))["entries"]
        rows = [e for e in entries if e["source_path"].endswith(("prompt.txt", "prompt-rework.txt")) and "codex-app-" in e["source_path"]]
        self.assertEqual(len(rows), 4)
        for row in rows:
            f = ROOT / "scripts" / "pm" / "shared-assets" / row["source_path"]
            self.assertEqual(row["mode"], 420)
            self.assertEqual(row["dependency_status"], "closed")
            self.assertEqual(row["dependencies"], [])
            self.assertEqual(hashlib.sha256(f.read_bytes()).hexdigest(), row["sha256"])
            self.assertEqual(stat.S_IMODE(f.stat().st_mode), 420)

    def test_parent_lacks_the_surface_red_witness(self):
        parent = subprocess.run(["git", "rev-parse", "HEAD^"], capture_output=True, text=True, cwd=str(ROOT)).stdout.strip()
        for rel in MODES.values():
            result = subprocess.run(["git", "show", f"{parent}:scripts/pm/shared-assets/{rel}"],
                                    capture_output=True, text=True, cwd=str(ROOT))
            self.assertNotEqual(result.returncode, 0, f"expected absent on parent: {rel}")


if __name__ == "__main__":
    unittest.main()
