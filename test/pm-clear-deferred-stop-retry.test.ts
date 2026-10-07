import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { MoPDatabase } from "../src/db.js";
import { HookProcessor } from "../src/hooks.js";
import { retryDeferredPmClearOnStop } from "../src/pmClearLatch.js";
import type { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";

function freshDb(prefix: string): { db: MoPDatabase; directory: string } {
  const directory = mkdtempSync(join(tmpdir(), prefix));
  return { db: new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") }), directory };
}

function baseOptions(db: MoPDatabase, nowMs: number, counter: { sends: number }, current = () => true) {
  return {
    db,
    source: "pm_status_stop_retry",
    nowMs,
    staleAfterMs: 10 * 60_000,
    recentSuppressMs: 10 * 60_000,
    hasRecentClearEvent: false,
    laterLifecycleEvent: null,
    backoffMs: 60_000,
    settleMs: 0,
    wait: async () => {},
    isCurrent: current,
    send: async () => {
      counter.sends += 1;
      return { success: true };
    },
  };
}

test("deferred-busy latch: a later Stop retries the guarded send exactly once", async () => {
  const { db, directory } = freshDb("mop-pm-clear-deferred-");
  try {
    const now = Date.parse("2026-10-07T12:40:00.000Z");
    db.setPendingClear(0);
    db.setConfig("pm_clear_requested_at", "2026-10-07T12:38:06.342Z");
    db.setConfig("pm_clear_delivery_state", "deferred_busy");
    const counter = { sends: 0 };

    const first = await retryDeferredPmClearOnStop(baseOptions(db, now, counter));
    assert.equal(first.kind, "sent");
    assert.equal(counter.sends, 1);
    assert.equal(db.getConfig("pm_clear_delivery_state"), "awaiting_ack");
    assert.equal(db.getConfig("pm_clear_requested_at"), "2026-10-07T12:38:06.342Z");

    // Delivered-awaiting-ack: further Stops never resend.
    const second = await retryDeferredPmClearOnStop(baseOptions(db, now + 120_000, counter));
    assert.equal(second.kind, "skipped");
    assert.equal(counter.sends, 1);

    const relay = { sendToSlot() { counter.sends += 100; return true; } } as unknown as TmuxRelay;
    await new HookProcessor(db, relay).process(0, { type: "Stop", session_id: "pm" });
    assert.equal(counter.sends, 1);
    assert.equal(db.getEvents(0, 10, "clear_pending_duplicate_suppressed").length, 1);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("deferred-busy retry is fenced by the stop generation and bounded by backoff", async () => {
  const { db, directory } = freshDb("mop-pm-clear-fence-");
  try {
    const now = Date.parse("2026-10-07T12:40:00.000Z");
    db.setPendingClear(0);
    db.setConfig("pm_clear_requested_at", "2026-10-07T12:38:06.342Z");
    db.setConfig("pm_clear_delivery_state", "deferred_busy");
    const counter = { sends: 0 };

    // PM started a new turn during the settle window: no send, no backoff stamp.
    const fenced = await retryDeferredPmClearOnStop(baseOptions(db, now, counter, () => false));
    assert.equal(fenced.kind, "skipped");
    assert.equal(counter.sends, 0);

    // A send that hits a busy PM re-defers; a Stop inside the backoff does not resend.
    const busySend = { ...baseOptions(db, now + 1000, counter), send: async () => { counter.sends += 1; return { success: false, busy: true, zeroEffect: true }; } };
    const deferred = await retryDeferredPmClearOnStop(busySend);
    assert.equal(deferred.kind, "deferred_busy");
    assert.equal(counter.sends, 1);
    const backoff = await retryDeferredPmClearOnStop(baseOptions(db, now + 30_000, counter));
    assert.equal(backoff.kind, "skipped");
    assert.equal(counter.sends, 1);

    const after = await retryDeferredPmClearOnStop(baseOptions(db, now + 62_000, counter));
    assert.equal(after.kind, "sent");
    assert.equal(counter.sends, 2);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("Stop hook does not claim an undelivered latch is awaiting acknowledgement", async () => {
  const { db, directory } = freshDb("mop-pm-clear-hook-");
  try {
    db.setPendingClear(0);
    db.setConfig("pm_clear_requested_at", "2026-10-07T12:38:06.342Z");
    db.setConfig("pm_clear_delivery_state", "deferred_busy");
    const relay = { sendToSlot() { throw new Error("hook must not send"); } } as unknown as TmuxRelay;
    await new HookProcessor(db, relay).process(0, { type: "Stop", session_id: "pm" });
    assert.equal(db.getEvents(0, 10, "clear_pending_duplicate_suppressed").length, 0);
    assert.equal(db.getEvents(0, 10, "clear_pending_undelivered_stop").length, 1);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
