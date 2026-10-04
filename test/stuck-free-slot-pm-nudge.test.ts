import assert from "node:assert/strict";
import test from "node:test";

import type { MoPDatabase } from "../src/db.js";
import type { LogManager } from "../src/logs.js";
import type { TmuxRelay } from "../src/relay.js";
import { StuckDetector } from "../src/stuck.js";
import type { EventLogEntry, SlotState } from "../src/types.js";

// Regression: Rajiv 2026-10-04 14:47 IST, S1 and S6 sat free and unassigned
// for 10+ min with no PM nudge.
process.env.MOP_FREE_SLOT_NUDGE_ENABLED = "1";

function slot(n: number, overrides: Partial<SlotState> = {}): SlotState {
  return {
    slot: n, address: `0:0.${n}`, name: `S${n}name`, status: "free", occupied: false,
    session_id: `s${n}`, task: null, issue: null, branch: null, pr: null, head_sha: null,
    assignment_epoch: 100 + n, assigned_at: null, last_activity: null, dnd: false, idle: true,
    activity: null, active_turn_id: null, active_turn_started_at: null, active_turn_state: "inactive",
    ...overrides,
  } as SlotState;
}

function harness(
  slots: SlotState[], freeMinBySlot: Record<number, number>, sendOk = true,
  live: Record<number, SlotState> = {},
) {
  const events: EventLogEntry[] = [];
  const sends: Array<{ slot: number; cmd: string }> = [];
  let id = 1;
  const push = (slotNum: number, type: string, ts: string, payload: unknown) =>
    events.push({ id: id++, timestamp: ts, slot: slotNum, event_type: type, hook_type: "Stuck",
      tool_name: null, payload: JSON.stringify(payload), processed: false });
  for (const [n, min] of Object.entries(freeMinBySlot)) {
    push(Number(n), "slot_released", new Date(Date.now() - min * 60_000).toISOString().replace(/Z$/, ""), {});
  }
  const db = {
    getExitPending: () => false,
    hasPendingClear: () => false,
    getSlot: (n: number) => live[n] ?? slots.find((s) => s.slot === n),
    getAllSlots: () => slots,
    getEvents: (slotNum: number, limit: number, type?: string) =>
      events.filter((e) => e.slot === slotNum && (!type || e.event_type === type))
        .sort((a, b) => b.id - a.id).slice(0, limit),
    logEvent: (slotNum: number, type: string, _h: string | null, _t: string | null, payload: unknown) => {
      push(slotNum, type, new Date().toISOString().replace(/Z$/, ""), payload);
      return id - 1;
    },
  } as unknown as MoPDatabase;
  const relay = {
    sendToSlotAsync: async (n: number, cmd: string) => { sends.push({ slot: n, cmd }); return sendOk; },
  } as unknown as TmuxRelay;
  const logManager = { getLogMtime: async () => null } as unknown as LogManager;
  return { detector: new StuckDetector(db, logManager, relay), events, sends };
}

test("groups free slots 10+ min into one PM message, once per free stretch", async () => {
  const slots = [slot(1), slot(6), slot(3)];
  const h = harness(slots, { 1: 25, 6: 12, 3: 4 });
  await h.detector.checkFreeSlotsPmNudge(slots);
  assert.equal(h.sends.length, 1);
  assert.equal(h.sends[0].slot, 0);
  assert.match(h.sends[0].cmd, /S1 \(S1name\) free 25m/);
  assert.match(h.sends[0].cmd, /S6 \(S6name\) free 12m/);
  assert.doesNotMatch(h.sends[0].cmd, /S3 /);
  await h.detector.checkFreeSlotsPmNudge(slots);
  assert.equal(h.sends.length, 1, "same free stretch must not re-nudge");
});

test("a new free stretch nudges again", async () => {
  const slots = [slot(1)];
  const h = harness(slots, { 1: 15 });
  await h.detector.checkFreeSlotsPmNudge(slots);
  h.events.push({ id: 999, timestamp: new Date(Date.now() - 11 * 60_000).toISOString().replace(/Z$/, ""),
    slot: 1, event_type: "slot_released", hook_type: null, tool_name: null, payload: "{}", processed: false });
  await h.detector.checkFreeSlotsPmNudge(slots);
  assert.equal(h.sends.length, 2);
});

test("occupied, dnd and non-dev slots never trigger", async () => {
  const slots = [slot(2, { occupied: true }), slot(4, { dnd: true }), slot(0)];
  const h = harness(slots, { 2: 30, 4: 30, 0: 30 });
  await h.detector.checkFreeSlotsPmNudge(slots);
  assert.equal(h.sends.length, 0);
});

test("uncertain PM send consumes the free stretch (no retry)", async () => {
  const slots = [slot(1)];
  const h = harness(slots, { 1: 15 }, false);
  await h.detector.checkFreeSlotsPmNudge(slots);
  await h.detector.checkFreeSlotsPmNudge(slots);
  assert.equal(h.sends.length, 1);
  assert.ok(h.sends.every((x) => x.slot === 0), "free-slot nudge goes to PM only");
});

test("free-slot nudge is OFF unless MOP_FREE_SLOT_NUDGE_ENABLED=1", async () => {
  const slots = [slot(1)];
  const h = harness(slots, { 1: 30 });
  delete process.env.MOP_FREE_SLOT_NUDGE_ENABLED;
  try {
    await h.detector.checkFreeSlotsPmNudge(slots);
  } finally {
    process.env.MOP_FREE_SLOT_NUDGE_ENABLED = "1";
  }
  assert.equal(h.sends.length, 0);
});

test("CTO REVISE 880774e: slot assigned during the tick is not mentioned", async () => {
  const slots = [slot(1), slot(6), slot(5)];
  // Detection snapshot says all free; live re-read shows S6 assigned and S5 epoch bumped.
  const h = harness(slots, { 1: 25, 6: 25, 5: 25 }, true, {
    6: slot(6, { occupied: true, assignment_epoch: 107 }),
    5: slot(5, { assignment_epoch: 999 }),
  });
  await h.detector.checkFreeSlotsPmNudge(slots);
  assert.equal(h.sends.length, 1);
  assert.match(h.sends[0].cmd, /S1 \(S1name\)/);
  assert.doesNotMatch(h.sends[0].cmd, /S6 /);
  assert.doesNotMatch(h.sends[0].cmd, /S5 /);
  const all = harness([slot(6)], { 6: 25 }, true, { 6: slot(6, { occupied: true }) });
  await all.detector.checkFreeSlotsPmNudge([slot(6)]);
  assert.equal(all.sends.length, 0);
});
