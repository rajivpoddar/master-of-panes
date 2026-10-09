#!/usr/bin/env python3
"""Plan-review budget counts only rounds on the CURRENT head.

Ported live edit (MoP drift reconcile 2026-10-09): plan-review markers and
reservations recorded against another head never carry over, and a repeated
blocker class can not cap before PLAN_REVIEW_CAP completed rounds.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).parents[3]
SRC = ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "skills" / "codex-review-companion" / "codex-review-companion.mjs"

PROBE = r"""
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
const [src, histDir, scenario] = process.argv.slice(2);
process.env.CODEX_REVIEW_HISTORY_DIR = histDir;
process.env.CODEX_REVIEW_LEGACY_DIR = path.join(histDir, "no-legacy");
const c = await import(src);
const runner = (cmd, args) => {
  if (args[0] === "rev-parse") return { code: 0, stdout: "/repo\n", stderr: "" };
  return { code: 0, stdout: "git@github.com:o/r.git\n", stderr: "" };
};
const issue = 4242;
const s = JSON.parse(scenario);
const base = { reviewType: "plan", issue, repoRoot: "/repo", _currentHead: s.current };
// Discover the per-repo history directory by running once (allowed, writes a reservation).
const first = c.runReviewBudget({ ...base, _currentHead: "f".repeat(40) }, runner);
const dir = fs.readdirSync(histDir).map((d) => path.join(histDir, d))
  .find((d) => fs.statSync(d).isDirectory() && d !== path.join(histDir, "no-legacy"));
for (const f of fs.readdirSync(dir)) fs.unlinkSync(path.join(dir, f));
s.rounds.forEach((r, i) => {
  const text = [
    "VERDICT: REJECT", "MARKER_PROVENANCE: codex-review-companion", "TYPE: plan-review",
    `ISSUE: #${issue}`, `HEAD_SHA: ${r.head}`, `TIMESTAMP: ${1791000000 + i}`,
    "BLOCKER_CLASS: same-class", "BLOCKER_STATUS: OPEN",
  ].join("\n");
  fs.writeFileSync(path.join(dir, crypto.createHash("sha256").update(text).digest("hex") + ".md"), text);
});
const out = c.runReviewBudget(base, runner);
console.log(JSON.stringify(out.budget));
"""

A = "a" * 40
B = "b" * 40


def run(current: str, round_heads: list[str]) -> dict:
    with tempfile.TemporaryDirectory() as hist:
        scenario = json.dumps({"current": current, "rounds": [{"head": h} for h in round_heads]})
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", PROBE, "probe", str(SRC), hist, scenario],
            capture_output=True, text=True, check=False,
        )
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout.strip().splitlines()[-1])


class PlanBudgetHeadBinding(unittest.TestCase):
    def test_rounds_on_another_head_do_not_cap_current_head(self):
        budget = run(B, [A, A, A])
        self.assertEqual(budget["decision"], "allowed")
        self.assertEqual(budget["blocking_round_counts_48h"], {"plan": 0})

    def test_repeated_blocker_needs_cap_rounds_before_capping(self):
        budget = run(A, [A, A])
        self.assertEqual(budget["decision"], "allowed")

    def test_cap_rounds_on_current_head_still_cap(self):
        budget = run(A, [A, A, A])
        self.assertEqual(budget["decision"], "rescue_required")


if __name__ == "__main__":
    unittest.main()
