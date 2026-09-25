import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { MoPDatabase } from "../src/db.js";
import { HookProcessor } from "../src/hooks.js";
import { requestPmClearOnce, waitForPmIdleDrain } from "../src/pmClearLatch.js";
import type { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";

test("repeated PM Stop events never resend an already-latched clear", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-pm-clear-once-"));
  try {
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
    db.setPendingClear(0);
    db.setConfig("pm_clear_requested_at", "2026-01-01T00:00:00.000Z");

    const sent: string[] = [];
    const relay = {
      sendToSlot(_slot: number, command: string) {
        sent.push(command);
        return true;
      },
    } as TmuxRelay;
    const processor = new HookProcessor(db, relay);

    await processor.process(0, { type: "Stop", session_id: "pm-turn" });
    await processor.process(0, { type: "Stop", session_id: "pm-turn" });

    assert.deepEqual(sent, []);
    assert.equal(db.hasPendingClear(0), true);
    const held = db.getEvents(0, 10, "clear_pending_duplicate_suppressed");
    assert.equal(held.length, 2);
    assert.ok(held.every((event) => event.payload.includes('"via":"hook_stop"')));
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("a stale PM clear latch crossed by a later Stop re-arms one new delivery", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-pm-clear-stale-"));
  try {
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
    const now = Date.now();
    db.setPendingClear(0);
    db.setConfig("pm_clear_requested_at", new Date(now - 11 * 60_000).toISOString());
    db.logEvent(0, "Stop", "Stop", null, { session_id: "pm-turn-after-request" });

    const [laterStop] = db.getEvents(0, 1, "Stop");
    let deliveries = 0;
    let pmBusy = true;
    const options = {
      db,
      source: "test",
      staleAfterMs: 10 * 60_000,
      recentSuppressMs: 10 * 60_000,
      hasRecentClearEvent: false,
      laterLifecycleEvent: laterStop,
      isPMBusy: () => pmBusy,
      send: async () => {
        deliveries += 1;
        return { success: true };
      },
    };

    const first = await requestPmClearOnce({ ...options, nowMs: now });
    assert.equal(first.kind, "deferred_busy");
    assert.equal(db.hasPendingClear(0), true);
    assert.equal(db.getConfig("pm_clear_delivery_state"), "deferred_busy");
    assert.equal(deliveries, 0);

    pmBusy = false;
    const resumed = await requestPmClearOnce({ ...options, nowMs: now + 1000 });
    assert.equal(resumed.kind, "sent");
    assert.equal(db.getConfig("pm_clear_delivery_state"), "awaiting_ack");

    const duplicate = await requestPmClearOnce({ ...options, nowMs: now + 2000 });
    assert.equal(duplicate.kind, "pending");
    assert.equal(deliveries, 1);
    assert.equal(db.getEvents(0, 10, "clear_pending_stale_repaired").length, 1);
    assert.equal(db.getEvents(0, 10, "clear_pending_deferred_busy").length, 1);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("a fresh in-flight PM clear remains deduplicated", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-pm-clear-fresh-"));
  try {
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
    const now = Date.now();
    db.setPendingClear(0);
    db.setConfig("pm_clear_requested_at", new Date(now - 60_000).toISOString());
    let deliveries = 0;

    const result = await requestPmClearOnce({
      db,
      source: "test",
      nowMs: now,
      staleAfterMs: 10 * 60_000,
      recentSuppressMs: 10 * 60_000,
      hasRecentClearEvent: false,
      laterLifecycleEvent: null,
      isPMBusy: () => false,
      send: async () => {
        deliveries += 1;
        return { success: true };
      },
    });

    assert.equal(result.kind, "pending");
    assert.equal(deliveries, 0);
    assert.equal(db.hasPendingClear(0), true);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("a stale latch is re-armed by pm_status_idle_drained without hook Stop rows", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-pm-clear-idle-drained-"));
  try {
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
    const now = Date.now();
    db.setPendingClear(0);
    db.setConfig("pm_clear_requested_at", new Date(now - 11 * 60_000).toISOString());
    db.logEvent(0, "pm_status_idle_drained", null, null, { event: "stop" });

    assert.equal(db.getEvents(0, 10, "Stop").length, 0);
    const [idleDrained] = db.getEvents(0, 1, "pm_status_idle_drained");
    assert.ok(idleDrained);
    let deliveries = 0;
    let pmBusy = true;
    const options = {
      db,
      source: "pm_status_stop",
      nowMs: now,
      staleAfterMs: 10 * 60_000,
      recentSuppressMs: 10 * 60_000,
      hasRecentClearEvent: false,
      laterLifecycleEvent: idleDrained,
      isPMBusy: () => pmBusy,
      send: async () => {
        deliveries += 1;
        return { success: true };
      },
    };

    const deferred = await requestPmClearOnce(options);
    assert.equal(deferred.kind, "deferred_busy");
    assert.equal(deliveries, 0);
    assert.equal(db.hasPendingClear(0), true);

    pmBusy = false;
    const sent = await requestPmClearOnce({ ...options, nowMs: now + 1000 });
    assert.equal(sent.kind, "sent");
    const duplicate = await requestPmClearOnce({ ...options, nowMs: now + 2000, laterLifecycleEvent: null });
    assert.equal(duplicate.kind, "pending");
    assert.equal(deliveries, 1);
    assert.equal(db.getEvents(0, 10, "clear_pending_stale_repaired").length, 1);
    assert.equal(db.getEvents(0, 10, "clear_pending_deferred_busy").length, 1);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("a stale PM clear retries only after the debounced drain proves idle", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-pm-clear-after-drain-"));
  try {
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
    const now = Date.now();
    db.setPendingClear(0);
    db.setConfig("pm_clear_requested_at", new Date(now - 11 * 60_000).toISOString());
    const stopEventId = db.logEvent(0, "pm_status_idle_drained", null, null, { event: "stop" });
    let busy = true;
    let drainEvent: { id: number } | null = null;
    let waitCount = 0;
    let deliveries = 0;
    const retry = waitForPmIdleDrain({
      afterEventId: stopEventId,
      getDrainEvent: () => drainEvent,
      isPMBusy: () => busy,
      isCurrent: () => true,
      wait: async () => {
        waitCount += 1;
        if (waitCount === 2) {
          drainEvent = { id: db.logEvent(0, "pm_debounce_drain_fired", null, null, { drained: 1 }) };
          busy = false;
        }
      },
      onDrain: async () => {
        const [idleDrained] = db.getEvents(0, 1, "pm_status_idle_drained");
        const result = await requestPmClearOnce({
          db,
          source: "pm_status_stop",
          nowMs: Date.now(),
          staleAfterMs: 10 * 60_000,
          recentSuppressMs: 10 * 60_000,
          hasRecentClearEvent: false,
          laterLifecycleEvent: idleDrained,
          isPMBusy: () => busy,
          send: async () => {
            deliveries += 1;
            return { success: true };
          },
        });
        assert.equal(result.kind, "sent");
      },
    });

    assert.equal(deliveries, 0, "the stop request schedules observation but does not send");
    const completed = await retry;
    assert.equal(completed, true);
    assert.equal(deliveries, 1);
    assert.equal(waitCount, 2);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("one stale-latch re-arm reports a missing acknowledgement instead of sending repeatedly", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-pm-clear-ack-missing-"));
  try {
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
    const now = Date.now();
    db.setPendingClear(0);
    db.setConfig("pm_clear_requested_at", new Date(now - 11 * 60_000).toISOString());
    let deliveries = 0;
    const options = {
      db,
      source: "pm_status_stop",
      nowMs: now,
      staleAfterMs: 10 * 60_000,
      recentSuppressMs: 10 * 60_000,
      hasRecentClearEvent: false,
      laterLifecycleEvent: db.getEvents(0, 1, "pm_status_idle_drained")[0],
      isPMBusy: () => false,
      send: async () => {
        deliveries += 1;
        return { success: true };
      },
    };

    const stopAndDrain = async (nowMs: number) => {
      const stopEventId = db.logEvent(0, "pm_status_idle_drained", null, null, { event: "stop" });
      db.logEvent(0, "pm_debounce_drain_fired", null, null, { drained: 0 });
      let delivery: Awaited<ReturnType<typeof requestPmClearOnce>> | undefined;
      await waitForPmIdleDrain({
        afterEventId: stopEventId,
        getDrainEvent: () => db.getEvents(0, 1, "pm_debounce_drain_fired")[0] ?? null,
        isPMBusy: () => false,
        isCurrent: () => true,
        wait: async () => {},
        onDrain: async () => {
          delivery = await requestPmClearOnce({
            ...options,
            nowMs,
            laterLifecycleEvent: db.getEvents(0, 1, "pm_status_idle_drained")[0],
          });
        },
      });
      return delivery;
    };

    const first = await stopAndDrain(now);
    assert.equal(first.kind, "sent");
    assert.equal(deliveries, 1);
    const rearmedAt = db.getConfig("pm_clear_requested_at");
    const laterNow = Date.parse(rearmedAt!) + 11 * 60_000;

    for (let cycle = 0; cycle < 3; cycle += 1) {
      const result = await stopAndDrain(laterNow + cycle * 1000);
      assert.equal(result.kind, "pending");
    }

    assert.equal(deliveries, 1);
    const missing = db.getEvents(0, 10, "pm_clear_ack_missing");
    assert.equal(missing.length, 1);
    assert.equal(missing[0].payload.includes(`"requested_at":"${rearmedAt}"`), true);

    db.clearPendingClear(0);
    const explicit = await requestPmClearOnce({
      ...options,
      nowMs: laterNow + 5000,
      laterLifecycleEvent: null,
      operatorReRequest: true,
    });
    assert.equal(explicit.kind, "sent");
    assert.equal(deliveries, 2, "an explicit operator re-request can begin a new request");
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("PM status Stop schedules retry after drain and keeps explicit retry distinct", () => {
  const source = readFileSync(new URL("../src/server.ts", import.meta.url), "utf8");
  const start = source.indexOf('app.post("/pm-status"');
  const end = source.indexOf('app.get("/pm-status"', start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);

  const route = source.slice(start, end);
  assert.doesNotMatch(route, /requestPmClearOnce\(/);
  assert.doesNotMatch(route, /sendClearViaMopSendPath\(/);
  assert.match(route, /retryPendingPmClearAfterDrain\(idleDrainedEventId, generation\)/);
  assert.match(source, /getEvents\(0, 1, "pm_debounce_drain_fired"\)/);
  assert.match(source, /sendClearViaMopSendPath\(0, "pm_status_stop", false\)/);
  assert.match(source, /isPMBusy: \(\) => relay\.isPMBusy\(\)/);
  assert.match(source, /sendClearViaMopSendPath\(0, options\.source, false\)/);
  assert.match(source, /db\.getEvents\(0, 100, "pm_status_idle_drained"\)/);
  assert.match(source, /operatorReRequest: options\.clearExistingPendingForTargets/);

  const sendRouteStart = source.indexOf('app.post("/slots/:slotNum/send"');
  const pmSubmit = source.indexOf("const submitted = await relay.submitToPM(command)", sendRouteStart);
  const busyGuard = source.indexOf("if (allowPmClear && relay.isPMBusy())", sendRouteStart);
  assert.ok(sendRouteStart >= 0);
  assert.ok(busyGuard >= 0 && busyGuard < pmSubmit, "PM clear busy guard must precede the actual submit");
});
