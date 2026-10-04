import assert from "node:assert/strict";
import test from "node:test";

import type { MoPDatabase } from "../src/db.js";
import type { LogManager } from "../src/logs.js";
import type { TmuxRelay } from "../src/relay.js";
import { StuckDetector } from "../src/stuck.js";
import type { EventLogEntry, SlotState } from "../src/types.js";

// Regression: slot 4, 2026-10-04 ~08:50Z. The turn ended on
// "Owner / next action: PM ..." plus an Interrupted prompt with an empty
// composer, yet /slots/4 kept active_turn_state=active, so no stall nudge.
process.env.MOP_PM_WAIT_NUDGE_INTERVAL_MS = "300000";

const NOW = Date.parse("2026-10-04T09:30:00.000Z");
const RULE = "─".repeat(40);

function pane(body: string, composer = ""): string {
  return `${body}\n${RULE}\n❯ ${composer}\n${RULE}\n  ? for shortcuts\n`;
}

const PM_OWNER_PANE = pane(
  "⏺ Rework blocked on capture.\n  Owner / next action: PM to admit the head.\n  ⎿  Interrupted · What should Claude do instead?",
);
const PLAIN_PANE = pane("⏺ Done with the edit.");

function slot(overrides: Partial<SlotState> = {}): SlotState {
  return {
    slot: 4, address: "0:0.4", name: "Chitra", status: "active", occupied: true,
    session_id: "s4", task: "issue 8796", repository_id: "github:heydonna-app/heydonna-app",
    issue: 8796, branch: "fix/8796", branch_ref: "refs/heads/fix/8796", pr: 8800,
    head_sha: "b".repeat(40), assignment_epoch: 826, assigned_at: "2026-10-04T07:00:00.000Z",
    last_activity: "2026-10-04T09:29:00.000", dnd: false, idle: false, activity: null,
    active_turn_id: "t1", active_turn_started_at: "2026-10-04T08:40:00.000Z",
    active_turn_state: "active", last_meaningful_work_at: "2026-10-04T08:50:00.000Z",
    ...overrides,
  } as SlotState;
}

function harness(s: SlotState, opts: { output: string; activity?: "idle" | "busy"; silentMin: number }) {
  const events: EventLogEntry[] = [];
  const sends: string[] = [];
  let id = 1;
  const db = {
    getExitPending: () => false,
    hasPendingClear: () => false,
    hasActiveNativeReleaseIntent: () => false,
    claimNativeReleaseIntent: () => true,
    clearNativeReleaseIntent: () => undefined,
    hasRecentSubagentDispatch: () => null,
    getSlot: () => s,
    getAllSlots: () => [s],
    getEvents: (_slot: number, limit: number, type?: string) =>
      events.filter((e) => !type || e.event_type === type).sort((a, b) => b.id - a.id).slice(0, limit),
    logEvent: (slotNum: number, type: string, hook: string | null, tool: string | null, payload: unknown) => {
      events.push({ id: id++, timestamp: new Date(NOW).toISOString(), slot: slotNum, event_type: type,
        hook_type: hook, tool_name: tool, payload: JSON.stringify(payload), processed: false });
      return id - 1;
    },
  } as unknown as MoPDatabase;
  const relay = {
    captureOutput: async () => ({ output: opts.output, activity: opts.activity ?? "idle" }),
    sendToSlotAsync: async (_n: number, cmd: string) => { sends.push(cmd); return true; },
  } as unknown as TmuxRelay;
  const logManager = {
    getLogMtime: async () => new Date(NOW - opts.silentMin * 60_000),
  } as unknown as LogManager;
  return { detector: new StuckDetector(db, logManager, relay), events, sends };
}

async function atNow<T>(fn: () => Promise<T>): Promise<T> {
  const orig = Date.now;
  Date.now = () => NOW;
  try { return await fn(); } finally { Date.now = orig; }
}

test("stale active turn ending on PM owner + empty composer gets one stall nudge", async () => {
  const h = harness(slot(), { output: PM_OWNER_PANE, silentMin: 8 });
  await atNow(() => h.detector.checkStaleActiveTurn(slot()));
  assert.equal(h.sends.length, 1);
  assert.match(h.sends[0], /Skill\(pm-wait-nudge\) now with slot=4 assignment_epoch=826/);
  const ev = h.events.filter((e) => e.event_type === "stale_active_stall_nudge_injected");
  assert.equal(ev.length, 1);
  assert.equal(JSON.parse(ev[0].payload).pm_owner, true);
  // Same silent episode: no repeat.
  await atNow(() => h.detector.checkStaleActiveTurn(slot()));
  assert.equal(h.sends.length, 1);
});

test("stale active turn idle at empty prompt for 20+ min gets a stall nudge", async () => {
  const h = harness(slot(), { output: PLAIN_PANE, silentMin: 21 });
  await atNow(() => h.detector.checkStaleActiveTurn(slot()));
  assert.equal(h.sends.length, 1);
});

test("no nudge before 20 min without a PM-owner line", async () => {
  const h = harness(slot(), { output: PLAIN_PANE, silentMin: 12 });
  await atNow(() => h.detector.checkStaleActiveTurn(slot()));
  assert.equal(h.sends.length, 0);
});

test("no nudge when pane is busy or composer holds text", async () => {
  const busy = harness(slot(), { output: PM_OWNER_PANE, activity: "busy", silentMin: 30 });
  await atNow(() => busy.detector.checkStaleActiveTurn(slot()));
  assert.equal(busy.sends.length, 0);
  const typed = harness(slot(), { output: pane("Owner / next action: PM", "draft reply"), silentMin: 30 });
  await atNow(() => typed.detector.checkStaleActiveTurn(slot()));
  assert.equal(typed.sends.length, 0);
});

test("inactive turns are left to checkIdleOccupied", async () => {
  const s = slot({ active_turn_state: "inactive" });
  const h = harness(s, { output: PM_OWNER_PANE, silentMin: 30 });
  await atNow(() => h.detector.checkStaleActiveTurn(s));
  assert.equal(h.sends.length, 0);
});
