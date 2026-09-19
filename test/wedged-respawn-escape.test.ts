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

import {
  evaluateWedgeInterrupt,
  WEDGE_INTERRUPT_KEY,
} from "../src/wedgedRespawnEscape.js";

const WEDGE_BODY = {
  interrupt_attested: true,
  wedge_attestation: { idle_prompt_with_queued_input_observed: true, no_tool_progress_minutes: 6 },
};

test("interrupt sends Ctrl-C only — Enter and C-m are submits, never an escape", () => {
  assert.equal(WEDGE_INTERRUPT_KEY, "C-c");
  assert.match(WEDGED_BUSY_REMEDIATION, /POST \/slots\/:n\/interrupt-turn/);
  assert.match(WEDGED_BUSY_REMEDIATION, /Enter and C-m are submits/);
});

test("interrupt refuses when no turn is live", () => {
  for (const slot of [{ occupied: false, idle: true }, { occupied: true, idle: true }, null, undefined]) {
    const verdict = evaluateWedgeInterrupt(slot, WEDGE_BODY);
    assert.equal(verdict.decision, "nothing_to_interrupt");
  }
});

test("interrupt refuses DND and unattested callers without side effects", () => {
  assert.equal(
    evaluateWedgeInterrupt({ occupied: true, idle: false, dnd: true }, WEDGE_BODY).decision,
    "dnd_refused",
  );
  for (const body of [undefined, {}, { interrupt_attested: false }, { interrupt_attested: "yes" }]) {
    assert.equal(evaluateWedgeInterrupt(BUSY, body).decision, "attestation_insufficient");
  }
  // Queued-input prompt not observed, or quiet period too short, is not a wedge.
  for (const wedge_attestation of [
    { idle_prompt_with_queued_input_observed: false, no_tool_progress_minutes: 30 },
    { idle_prompt_with_queued_input_observed: true, no_tool_progress_minutes: 4 },
    { idle_prompt_with_queued_input_observed: true },
    {},
  ]) {
    assert.equal(
      evaluateWedgeInterrupt(BUSY, { interrupt_attested: true, wedge_attestation }).decision,
      "attestation_insufficient",
    );
  }
});

test("attested wedge interrupt is allowed with quiet minutes carried", () => {
  const verdict = evaluateWedgeInterrupt(BUSY, WEDGE_BODY);
  assert.equal(verdict.decision, "allowed");
  assert.equal(verdict.quiet_minutes, 6);
});
