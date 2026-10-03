#!/usr/bin/env python3
"""Verdict-parser regressions: blocking reviews must never publish passing markers.

CTO REVISE on MoP 662fb4f (Slack C0ALZJHGE49 thread 1791016590.458369):
(1) a quoted historical APPROVE overrode the current opening REJECT;
(2) a P0 SEVERITY_OVERRIDE was normalized to APPROVE.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import unittest

ROOT = pathlib.Path(__file__).parents[3]
SRC = ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "skills" / "codex-review-companion" / "codex-review-companion.mjs"

PROBE = r"""
import fs from "node:fs";
const c = await import(process.argv[2]);
// Real marker writer, kept in memory: capture its writes instead of touching disk.
const written = new Map();
fs.writeFileSync = (p, data) => { written.set(String(p), String(data)); };
fs.mkdirSync = () => {};
const cases = JSON.parse(process.argv[3]);
const out = {};
for (const [name, text] of Object.entries(cases)) {
  const p = c.parseVerdict("code", text);
  const head = "a".repeat(40);
  const marker = [
    "VERDICT: APPROVE", "COMPANION_VERDICT: APPROVE", "FINAL_REVIEWER_VERDICT: APPROVE",
    "MARKER_PROVENANCE: codex-review-companion", "TYPE: code-review",
    "TIMESTAMP: 1791016590", "ISSUE: -", "PR: #1", `HEAD_SHA: ${head}`,
    "--- Findings (0) ---", "--- Review Output ---", text,
  ].join("\n");
  const forged = c.verifyMarkerTerminal(marker, { head, number: 1, reviewType: "code" });
  const head2 = "b".repeat(40);
  const markerFile = `/mem/codex-app-code-review-1-${name}.txt`;
  const blocked = p.exitCode !== 0 || c.verdictContractError(p);
  let realMarker = null, realVerify = null;
  if (!blocked) {
    c.writeMarker({ reviewType: "code", pr: 1, markerFile, _currentHead: head2 }, p, text);
    realMarker = written.get(markerFile);
    realVerify = c.verifyMarkerTerminal(realMarker, { head: head2, number: 1, reviewType: "code" }).ok;
  }
  out[name] = {
    publishedPassing: !blocked && realVerify === true,
    verdict: p.verdict,
    exitCode: p.exitCode,
    contractError: c.verdictContractError(p),
    findings: p.findings.length,
    forgedApproveMarkerOk: forged.ok,
  };
}
console.log(JSON.stringify(out));
"""

# Literal reproduced inputs.
QUOTED_HISTORICAL_APPROVE = (
    "**REJECT** — the delta reintroduces the bypass.\n\n"
    "Prior round verdict, quoted for history:\n> APPROVE\n\n"
    "P1: bypass path reachable at gate.ts:40\n"
)
QUOTED_HISTORICAL_VERDICT_FIELD = "**REJECT** — still broken.\n\n> VERDICT: APPROVE\n\nP1: still broken\n"
P0_SEVERITY_OVERRIDE = (
    "VERDICT: REJECT\n\nP2: naming nit\n"
    "SEVERITY_OVERRIDE: P0\nSEVERITY_OVERRIDE_REASON: data loss in prod\n"
)

# CTO REVISE on d79389d (same thread): literal regressions.
NEGATED_APPROVE_WITH_QUOTED_HISTORY = (
    "I cannot approve this change; the bypass remains.\n\n"
    "> APPROVE\n\n"
    "P1: bypass path still reachable at gate.ts:40\n"
)
REJECT_WITH_QUOTED_OLD_NIT = (
    "**REJECT** — the bypass remains in the writer.\n\n"
    "> P2: naming nit\n"
)

# CTO REVISE on 3afda32 (same thread): literal regressions plus siblings.
FENCED_HISTORICAL_APPROVE = (
    "I cannot approve this change; the writer still drops data.\n\n"
    "Previous round, for reference:\n```\nVERDICT: APPROVE\n```\n\n"
    "P1: writer drops customer data at save.ts:88\n"
)
EMPHASIZED_NEGATION = "I cannot **APPROVE** this change: the writer drops customer data.\n"

# CTO REVISE on 2277b3a: nested-fence discriminators.
NESTED_BACKTICK_FENCE = (
    "**REJECT** — the writer still loses customer data.\n\n"
    "````\nPrior review:\n```\nsnippet\n```\nP2: naming nit\n````\n"
)
NESTED_TILDE_FENCE = (
    "**REJECT** — the writer still loses customer data.\n\n"
    "~~~~\nPrior review:\n~~~\nsnippet\n~~~\nP2: naming nit\n~~~~\n"
)

CASES = {
    "nested_backtick": NESTED_BACKTICK_FENCE,
    "nested_tilde": NESTED_TILDE_FENCE,
    "unclosed_fence": "**REJECT** — data loss.\n\n```\nP2: naming nit\n",
    "declared_reject_current_p2": "VERDICT: REJECT\n\nP2: naming nit\n",
    "fenced_history": FENCED_HISTORICAL_APPROVE,
    "emphasized_negation": EMPHASIZED_NEGATION,
    "tilde_fence": "The bypass remains.\n\n~~~\nVERDICT: APPROVE\n~~~\n\nP1: bypass at gate.ts:40\n",
    "underscore_negation": "We can't __APPROVE__ this yet.\n",
    "backtick_negation": "Unable to `APPROVE`: the guard is bypassed.\n",
    "wont_approve": "I won't approve this. APPROVE is withheld until the bypass is fixed.\n",
    "negated_quoted": NEGATED_APPROVE_WITH_QUOTED_HISTORY,
    "reject_quoted_nit": REJECT_WITH_QUOTED_OLD_NIT,
    "negated_upper": "We do not APPROVE this.\n\nP1: guard bypass\n",
    "quoted_approve": QUOTED_HISTORICAL_APPROVE,
    "quoted_field": QUOTED_HISTORICAL_VERDICT_FIELD,
    "p0_override": P0_SEVERITY_OVERRIDE,
    "p0_override_on_approve": "VERDICT: APPROVE\n\nSEVERITY_OVERRIDE: P0\nSEVERITY_OVERRIDE_REASON: x\n",
    "p1_override_on_p2": "VERDICT: REVISE\n\nP2: nit\nSEVERITY_OVERRIDE: P1\nSEVERITY_OVERRIDE_REASON: x\n",
    "open_blocker_on_approve": (
        "VERDICT: APPROVE\n\nBLOCKER_ID: PROOF-001\nBLOCKER_STATUS: OPEN\nBLOCKER_REASON: missing proof\n"
    ),
    "clean_approve": "VERDICT: APPROVE\n\nNo findings.\n",
    "bold_approve": "**APPROVE** — the revised plan closes every blocker.\n",
    "fallback_approve": "Looks good overall. APPROVE.\n",
    "p2_followup": "VERDICT: REVISE\n\nP2: rename helper for clarity\n",
    "p1_blocking": "VERDICT: REVISE\n\nP1: guard bypass in writer\n",
    "conflicting": "VERDICT: APPROVE\n\nSummary.\n\n**REJECT**\n",
    "conflicting_lead": "**APPROVE** — looks fine.\n\nVERDICT: REJECT\n",
}


class VerdictParserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", PROBE, "probe", str(SRC), json.dumps(CASES)],
            check=True, capture_output=True, text=True,
        )
        cls.r = json.loads(proc.stdout.strip().splitlines()[-1])

    def assert_never_passes(self, name: str) -> None:
        r = self.r[name]
        blocked = r["exitCode"] != 0 or r["contractError"] is not None
        self.assertTrue(blocked, r)
        self.assertFalse(r["forgedApproveMarkerOk"], r)
        self.assertFalse(r["publishedPassing"], r)

    def test_negated_approval_and_quoted_history_never_approve(self) -> None:
        for name in ("negated_quoted", "negated_upper"):
            self.assertNotIn(self.r[name]["verdict"], ("APPROVE", "CONFIRMED", "VERIFIED", "APPROVE_PENDING_CI"), name)
            self.assert_never_passes(name)

    def test_fenced_history_and_emphasized_negation_never_approve(self) -> None:
        for name in ("fenced_history", "emphasized_negation", "tilde_fence",
                     "underscore_negation", "backtick_negation", "wont_approve"):
            self.assertNotIn(self.r[name]["verdict"], ("APPROVE", "CONFIRMED", "VERIFIED", "APPROVE_PENDING_CI"), name)
            self.assert_never_passes(name)

    def test_nested_fences_and_declared_reject_stay_blocking(self) -> None:
        for name in ("nested_backtick", "nested_tilde", "unclosed_fence", "declared_reject_current_p2"):
            self.assertEqual(self.r[name]["verdict"], "REJECT", name)
            self.assert_never_passes(name)
        for name in ("nested_backtick", "nested_tilde", "unclosed_fence"):
            self.assertEqual(self.r[name]["findings"], 0, name)

    def test_quoted_old_nit_does_not_overturn_current_reject(self) -> None:
        r = self.r["reject_quoted_nit"]
        self.assertEqual((r["verdict"], r["findings"]), ("REJECT", 0), r)
        self.assert_never_passes("reject_quoted_nit")

    def test_quoted_historical_approve_does_not_override_opening_reject(self) -> None:
        self.assertEqual(self.r["quoted_approve"]["verdict"], "REJECT")
        self.assertIsNone(self.r["quoted_approve"]["contractError"])
        self.assert_never_passes("quoted_approve")
        self.assertEqual(self.r["quoted_field"]["verdict"], "REJECT")
        self.assert_never_passes("quoted_field")

    def test_p0_severity_override_is_never_approve(self) -> None:
        self.assertEqual(self.r["p0_override"]["verdict"], "REJECT")
        self.assert_never_passes("p0_override")

    def test_override_or_open_blocker_on_approve_is_refused(self) -> None:
        for name in ("p0_override_on_approve", "open_blocker_on_approve"):
            self.assertIn("VERDICT_HEADER_BODY_DISAGREEMENT", self.r[name]["contractError"] or "", name)
            self.assertNotEqual(self.r[name]["verdict"], "APPROVE", name)
            self.assert_never_passes(name)

    def test_p1_override_and_p1_finding_block(self) -> None:
        for name in ("p1_override_on_p2", "p1_blocking"):
            self.assertEqual(self.r[name]["verdict"], "REVISE", name)
            self.assert_never_passes(name)

    def test_conflicting_verdicts_refuse(self) -> None:
        for name in ("conflicting", "conflicting_lead"):
            self.assertIn("VERDICT_HEADER_BODY_DISAGREEMENT", self.r[name]["contractError"] or "", name)
            self.assert_never_passes(name)

    def test_controls_still_approve(self) -> None:
        clean = self.r["clean_approve"]
        self.assertEqual((clean["verdict"], clean["exitCode"], clean["contractError"]), ("APPROVE", 0, None))
        self.assertTrue(clean["forgedApproveMarkerOk"])
        self.assertTrue(clean["publishedPassing"])
        for name in ("bold_approve", "fallback_approve"):
            self.assertEqual(self.r[name]["verdict"], "APPROVE", name)
            self.assertTrue(self.r[name]["publishedPassing"], name)
        p2 = self.r["p2_followup"]
        self.assertEqual((p2["verdict"], p2["exitCode"], p2["contractError"]), ("APPROVE", 0, None))
        self.assertEqual(p2["findings"], 1)
        self.assertTrue(p2["publishedPassing"])


if __name__ == "__main__":
    unittest.main()
