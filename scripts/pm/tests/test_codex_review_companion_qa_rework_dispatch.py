#!/usr/bin/env python3
"""Real dispatch proof for QA rework (Ev0C3ECXCGJF).

Exercises the ACTUAL companion predicate and loader (imported from the managed
.mjs) against the candidate prompt bytes staged into a temporary HOME, and
reproduces the reviewed parent's predicate defect.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).parents[3]
COMPANION = ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "skills" / "codex-review-companion" / "codex-review-companion.mjs"
PROMPTS = ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "skills"
REVIEWED_PARENT = "51b31bce5ec226d418bd6f7bcebcbe927750713d"

# (reviewType, reworkItems, expected_isReReview, staged prompt rel, must_load)
CASES = [
    ("qa", None, False, "codex-app-qa-review/templates/prompt.txt", True),
    ("qa", "x", True, "codex-app-qa-review/templates/prompt-rework.txt", True),
    ("code", None, False, "codex-app-code-review/templates/prompt.txt", True),
    ("code", "x", True, "codex-app-code-review/templates/prompt-rework.txt", True),
    ("plan", "x", True, "codex-app-plan-review/templates/prompt-rework.txt", False),
]

NODE = """
const m = await import(process.argv[2]);
const cases = JSON.parse(process.argv[3]);
const out = [];
for (const c of cases) {
  const re = m.isReReview({reviewType: c[0], reworkItems: c[1]});
  if (c[2] === false) { out.push({reviewType: c[0], rework: Boolean(c[1]), isReReview: re, framing: false, err: null, skipped: true}); continue; }
  let loaded = null, err = null;
  try { loaded = m.loadPromptTemplate({reviewType: c[0], reworkItems: c[1], previousHead: "0".repeat(40)}); }
  catch (e) { err = String(e && e.message || e); }
  out.push({reviewType: c[0], rework: Boolean(c[1]), isReReview: re, framing: Boolean(loaded && loaded.includes("Re-review framing")), err});
}
console.log(JSON.stringify(out));
"""


def stage_home(tmp: pathlib.Path) -> pathlib.Path:
    home = tmp / "home"
    for rel in {c[3] for c in CASES}:
        dst = home / ".claude" / "skills" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        src = PROMPTS / rel
        if src.is_file():
            shutil.copyfile(src, dst)
    return home


def run_node(module_path: pathlib.Path, cases, home, tmp):
    script = tmp / "probe.mjs"
    script.write_text(NODE, encoding="utf-8")
    result = subprocess.run(
        ["node", str(script), str(module_path), json.dumps(cases)],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "HOME": str(home)},
    )
    return result


class QaReworkDispatchTests(unittest.TestCase):
    def test_real_predicate_and_loader_select_the_rework_prompt(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            home = stage_home(tmp)
            result = run_node(COMPANION, [[c[0], c[1], c[4]] for c in CASES], home, tmp)
            self.assertEqual(result.returncode, 0, result.stderr[-400:])
            rows = json.loads(result.stdout.strip().splitlines()[-1])
            for row, (rt, rw, want_re, _rel, must_load) in zip(rows, CASES):
                with self.subTest(review=rt, rework=bool(rw)):
                    self.assertEqual(row["isReReview"], want_re)
                    if must_load:
                        self.assertIsNone(row["err"], row["err"])
                        self.assertEqual(row["framing"], want_re)
                    else:
                        # plan keeps its pre-existing rework selection; no template is staged
                        self.assertTrue(row["isReReview"])

    def test_reviewed_parent_predicate_skips_qa_rework_red_witness(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            parent = tmp / "parent-companion.mjs"
            blob = subprocess.run(
                ["git", "show", f"{REVIEWED_PARENT}:scripts/pm/shared-assets/claude/skills/codex-review-companion/codex-review-companion.mjs"],
                capture_output=True, text=True, cwd=str(ROOT),
            )
            self.assertEqual(blob.returncode, 0)
            parent.write_text(blob.stdout, encoding="utf-8")
            home = stage_home(tmp)
            result = run_node(parent, [["qa", "x", True]], home, tmp)
            self.assertEqual(result.returncode, 0, result.stderr[-400:])
            row = json.loads(result.stdout.strip().splitlines()[-1])[0]
            self.assertFalse(row["isReReview"], "reviewed parent must NOT treat QA rework as rework")
            self.assertFalse(row["framing"], "reviewed parent therefore never loads the QA rework framing")


if __name__ == "__main__":
    unittest.main()
