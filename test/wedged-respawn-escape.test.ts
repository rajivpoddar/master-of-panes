import assert from "node:assert/strict";
import test from "node:test";

import {
  evaluateWedgedRespawnEscape,
  WEDGED_BUSY_REMEDIATION,
  WEDGED_ESCAPE_MIN_QUIET_MINUTES,
} from "../src/wedgedRespawnEscape.js";

const BUSY = { occupied: true, idle: false };

test("idle slot is not a wedge case", () => {
  assert.equal(
    evaluateWedgedRespawnEscape({ occupied: true, idle: true }, undefined).decision,
    "not_busy",
  );
  assert.equal(
    evaluateWedgedRespawnEscape({ occupied: false, idle: true }, undefined).decision,
    "not_busy",
  );
});

test("busy slot without attestation stays refused with sanctioned recovery", () => {
  for (const body of [undefined, {}, { force_wedged_escape: false }, { force_wedged_escape: "yes" }]) {
    const verdict = evaluateWedgedRespawnEscape(BUSY, body);
    assert.equal(verdict.decision, "refused_busy");
    assert.equal(verdict.reason, "slot_busy_respawn_refused");
  }
  assert.match(WEDGED_BUSY_REMEDIATION, /POST \/slots\/:n\/respawn/);
  assert.match(WEDGED_BUSY_REMEDIATION, /only the wedged/);
});

test("genuinely working slot cannot pass as wedged", () => {
  // Prompt not observed.
  assert.equal(
    evaluateWedgedRespawnEscape(BUSY, {
      force_wedged_escape: true,
      wedge_attestation: { idle_prompt_observed: false, no_tool_progress_minutes: 30 },
    }).decision,
    "attestation_insufficient",
  );
  // Quiet period below the minimum.
  assert.equal(
    evaluateWedgedRespawnEscape(BUSY, {
      force_wedged_escape: true,
      wedge_attestation: {
        idle_prompt_observed: true,
        no_tool_progress_minutes: WEDGED_ESCAPE_MIN_QUIET_MINUTES - 1,
      },
    }).decision,
    "attestation_insufficient",
  );
  // Missing / malformed attestation.
  for (const wedge_attestation of [undefined, null, {}, { idle_prompt_observed: true }]) {
    assert.equal(
      evaluateWedgedRespawnEscape(BUSY, { force_wedged_escape: true, wedge_attestation } as any).decision,
      "attestation_insufficient",
    );
  }
});

test("attested wedge is allowed with quiet minutes carried", () => {
  const verdict = evaluateWedgedRespawnEscape(BUSY, {
    force_wedged_escape: true,
    wedge_attestation: { idle_prompt_observed: true, no_tool_progress_minutes: 6 },
  });
  assert.equal(verdict.decision, "allowed");
  assert.equal(verdict.quiet_minutes, 6);
});
