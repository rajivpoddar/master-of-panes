import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { MoPDatabase } from "../src/db.js";
import {
  clearSlotForAssignmentWithReadyWait,
  findSessionStartClearEvent,
  latestSessionStartEventId,
  waitForSessionStartClear,
} from "../src/sessionStartClearWait.js";
import { DEFAULT_CONFIG } from "../src/types.js";

function withDb<T>(fn: (db: MoPDatabase) => T): T {
  const directory = mkdtempSync(join(tmpdir(), "mop-session-start-clear-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  try {
    return fn(db);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
}

// ─── findSessionStartClearEvent / latestSessionStartEventId ─────────────

test("findSessionStartClearEvent ignores non-clear sources and events at/before the baseline", () => {
  withDb((db) => {
    db.logEvent(1, "SessionStart", "SessionStart", null, { type: "SessionStart", source: "startup" });
    const baseline = latestSessionStartEventId(db, 1);
    db.logEvent(1, "SessionStart", "SessionStart", null, { type: "SessionStart", source: "compact" });
    assert.equal(findSessionStartClearEvent(db, 1, baseline), null);

    db.logEvent(1, "SessionStart", "SessionStart", null, { type: "SessionStart", source: "clear" });
    const found = findSessionStartClearEvent(db, 1, baseline);
    assert.ok(found);
    assert.ok(found!.id > baseline);
  });
});

test("findSessionStartClearEvent never matches an event at or before the given baseline id", () => {
  withDb((db) => {
    db.logEvent(2, "SessionStart", "SessionStart", null, { type: "SessionStart", source: "clear" });
    const [priorClear] = db.getEvents(2, 1, "SessionStart");
    // Baseline == the prior clear's own id: it must not "find" itself again.
    assert.equal(findSessionStartClearEvent(db, 2, priorClear.id), null);
  });
});

test("findSessionStartClearEvent is scoped per-slot", () => {
  withDb((db) => {
    const baselineSlot1 = latestSessionStartEventId(db, 1);
    db.logEvent(2, "SessionStart", "SessionStart", null, { type: "SessionStart", source: "clear" });
    assert.equal(findSessionStartClearEvent(db, 1, baselineSlot1), null);
  });
});

// ─── waitForSessionStartClear (deadline+poll, fake time) ─────────────────

function fakeClock(startMs = 0) {
  let now = startMs;
  const wait = async (ms: number) => {
    now += ms;
  };
  return { nowMs: () => now, wait };
}

test("waitForSessionStartClear resolves immediately once getEvent reports a match", async () => {
  const clock = fakeClock();
  const event = { id: 7, timestamp: "2026-01-01T00:00:00.000Z" };
  const result = await waitForSessionStartClear({
    getEvent: () => event,
    wait: clock.wait,
    nowMs: clock.nowMs,
    timeoutMs: 10_000,
    pollMs: 500,
  });
  assert.equal(result.ready, true);
  assert.deepEqual(result.event, event);
  assert.equal(result.waitedMs, 0);
});

test("waitForSessionStartClear times out and reports ready=false when no event ever lands", async () => {
  const clock = fakeClock();
  const result = await waitForSessionStartClear({
    getEvent: () => null,
    wait: clock.wait,
    nowMs: clock.nowMs,
    timeoutMs: 3_000,
    pollMs: 1_000,
  });
  assert.equal(result.ready, false);
  assert.equal(result.event, null);
  assert.equal(result.waitedMs, 3_000);
});

test("waitForSessionStartClear picks up an event that lands mid-poll, before the deadline", async () => {
  const clock = fakeClock();
  let calls = 0;
  const result = await waitForSessionStartClear({
    getEvent: () => {
      calls += 1;
      // Land on the 3rd poll (after ~2s of a 1s-interval, 10s-timeout wait).
      return calls >= 3 ? { id: 42, timestamp: "t" } : null;
    },
    wait: clock.wait,
    nowMs: clock.nowMs,
    timeoutMs: 10_000,
    pollMs: 1_000,
  });
  assert.equal(result.ready, true);
  assert.equal(result.event?.id, 42);
  assert.equal(result.waitedMs, 2_000);
});

test("waitForSessionStartClear defaults are the documented 45s/1s deadline+poll", async () => {
  const clock = fakeClock();
  const result = await waitForSessionStartClear({
    getEvent: () => null,
    wait: clock.wait,
    nowMs: clock.nowMs,
  });
  assert.equal(result.ready, false);
  assert.equal(result.waitedMs, 45_000);
});

// ─── clearSlotForAssignmentWithReadyWait (new_issue readiness wait) ──────

test("clearSlotForAssignmentWithReadyWait: successful clear waits for readiness then returns the unchanged {ok,reason} contract", async () => {
  const calls: string[] = [];
  const clock = fakeClock();
  const result = await clearSlotForAssignmentWithReadyWait(3, {
    clearPendingClear: (slot) => calls.push(`clearPendingClear:${slot}`),
    getBaselineEventId: (slot) => {
      calls.push(`baseline:${slot}`);
      return 5;
    },
    sendClear: async (slot) => {
      calls.push(`sendClear:${slot}`);
      return { success: true };
    },
    logSlotCleared: (slot) => calls.push(`logSlotCleared:${slot}`),
    waitForReady: async (slot, baselineEventId) => {
      calls.push(`waitForReady:${slot}:${baselineEventId}`);
      return { ready: true, event: { id: 6, timestamp: "t" }, waitedMs: 250 };
    },
    logReadyWait: (slot, result) => calls.push(`logReadyWait:${slot}:${result.ready}:${result.eventId}`),
  });

  assert.deepEqual(result, { ok: true, reason: "cleared" });
  // clearPendingClear runs both before the send (stale-latch guard) and
  // after a successful send (existing two-call shape, preserved).
  assert.deepEqual(calls, [
    "clearPendingClear:3",
    "baseline:3",
    "sendClear:3",
    "clearPendingClear:3",
    "logSlotCleared:3",
    "waitForReady:3:5",
    "logReadyWait:3:true:6",
  ]);
});

test("clearSlotForAssignmentWithReadyWait: a readiness timeout still returns ok=true (never turns a successful send into a refusal)", async () => {
  const result = await clearSlotForAssignmentWithReadyWait(4, {
    clearPendingClear: () => {},
    getBaselineEventId: () => 0,
    sendClear: async () => ({ success: true }),
    logSlotCleared: () => {},
    waitForReady: async () => ({ ready: false, event: null, waitedMs: 45_000 }),
  });
  assert.deepEqual(result, { ok: true, reason: "cleared" });
});

test("clearSlotForAssignmentWithReadyWait: a failed send never waits and returns the exact failure contract", async () => {
  let waited = false;
  const result = await clearSlotForAssignmentWithReadyWait(5, {
    clearPendingClear: () => {},
    getBaselineEventId: () => 0,
    sendClear: async () => ({ success: false, reason: "pm_busy_deferred" }),
    logSlotCleared: () => {
      throw new Error("logSlotCleared must not run on a failed send");
    },
    waitForReady: async () => {
      waited = true;
      return { ready: true, event: null, waitedMs: 0 };
    },
  });
  assert.equal(waited, false);
  assert.deepEqual(result, {
    ok: false,
    reason: "assignment_clear_not_applied",
    detail: "pm_busy_deferred",
  });
});

test("clearSlotForAssignmentWithReadyWait: falls back to status when send fails with neither reason nor error", async () => {
  const result = await clearSlotForAssignmentWithReadyWait(6, {
    clearPendingClear: () => {},
    getBaselineEventId: () => 0,
    sendClear: async () => ({ success: false, status: 503 }),
    logSlotCleared: () => {},
    waitForReady: async () => ({ ready: true, event: null, waitedMs: 0 }),
  });
  assert.deepEqual(result, {
    ok: false,
    reason: "assignment_clear_not_applied",
    detail: "send failed status=503",
  });
});
