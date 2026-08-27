import assert from "node:assert/strict";
import test from "node:test";

import type { MoPDatabase } from "../src/db.js";
import type { LogManager } from "../src/logs.js";
import type { TmuxRelay } from "../src/relay.js";
import type { SlotState } from "../src/types.js";

function freeSlot(): SlotState {
  return {
    slot: 4,
    address: "0:0.4",
    name: "S4",
    status: "free",
    occupied: false,
    session_id: null,
    task: null,
    issue: null,
    branch: null,
    pr: null,
    head_sha: null,
    assignment_epoch: 643,
    assigned_at: null,
    last_activity: "2026-08-27T05:00:00.000",
    dnd: false,
    idle: true,
    activity: null,
    active_turn_id: null,
    active_turn_started_at: null,
    active_turn_state: "inactive",
    last_meaningful_work_at: null,
  };
}

test("a failed specialized phase does not suppress later idle phases", async () => {
  const slot = freeSlot();
  const db = { getAllSlots: () => [slot] } as unknown as MoPDatabase;
  const { StuckDetector } = await import("../dist/stuck.js");
  const detector = new StuckDetector(
    db,
    {} as LogManager,
    {} as TmuxRelay,
  );
  let freeChecks = 0;
  const runtime = detector as unknown as {
    checkContextOverflow: () => Promise<void>;
    checkApi500Backoff: () => Promise<void>;
    detectAnswerPromptBlock: () => Promise<void>;
    detectBgScriptFailures: () => Promise<void>;
    checkIdleOccupied: () => Promise<void>;
    checkIdleFree: () => Promise<void>;
  };
  runtime.checkContextOverflow = async () => undefined;
  runtime.checkApi500Backoff = async () => undefined;
  runtime.detectAnswerPromptBlock = async () => {
    throw new TypeError("this.relay.getSlotActivityState is not a function");
  };
  runtime.detectBgScriptFailures = async () => undefined;
  runtime.checkIdleOccupied = async () => undefined;
  runtime.checkIdleFree = async () => {
    freeChecks += 1;
  };

  await detector.checkAll();
  assert.equal(freeChecks, 1);
});

test("the release-shaped relay artifact exposes the activity capability", async () => {
  const relay = await import("../dist/relay.js");
  assert.equal(typeof relay.TmuxRelay.prototype.getSlotActivityState, "function");
  assert.equal(typeof relay.TmuxRelay.prototype.isSlotActive, "function");
});
