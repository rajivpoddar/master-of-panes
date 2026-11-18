// CTO REVISE C0ALZJHGE49 thread 1794970059.617269, ts 1794975890.713959, on 6764ccd8: the PM-only terminal completion (clear
// pending, the final `delivered` marker, the exit_pending_complete record) was three-plus separate writes. A crash between
// them left pending=true with neither the PM-only flag nor the attempted marker (the PM recovery path skips it, generic
// completion waits on dev slots, the RSS guard refuses a new arm), or pending=false with no marker and no completion record.
// Each case interrupts INSIDE the terminal clear (a SQLite trigger aborts one write of it) and requires the episode to be
// either exactly as it was or fully complete, and the next tick to finish it from the confirmed receipt with no second send.
import assert from "node:assert/strict";
import test from "node:test";

import { MoPDatabase } from "../src/db.js";
import { ProcessHealthChecker } from "../src/health.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const PROMPT = "saved continuation";

type FaultPoint = { name: string; sql: string };
const FAULTS: FaultPoint[] = [
  {
    name: "the exit_pending write of setExitPending(false) (pm_only and the delivery marker were already cleared)",
    sql: "BEFORE INSERT ON config WHEN NEW.key = 'exit_pending' AND NEW.value = 'false'",
  },
  {
    name: "the final delivered marker write (pending already false)",
    sql: "BEFORE INSERT ON config WHEN NEW.key = 'exit_pending_continuation_delivery' AND NEW.value = 'delivered'",
  },
  {
    name: "the exit_pending_complete record (pending false, marker already restored)",
    sql: "BEFORE INSERT ON events WHEN NEW.event_type = 'exit_pending_complete'",
  },
];

// Which tick finishes a PM-only episode whose continuation is already confirmed.
type Entry = { name: string; marker: "attempted" | "delivered"; run: (health: any) => Promise<void> };
const ENTRIES: Entry[] = [
  // MoP died between the delivered receipt and finishPMContinuation: the claim still reads "attempted".
  { name: "settle from the recorded receipt", marker: "attempted", run: (health) => health.reconcilePMRestartContinuation() },
  // The /exit dialog confirmation held the clear: the episode already reads delivered; the health observer ends it.
  { name: "health observer after a held dialog confirmation", marker: "delivered", run: (health) => health.checkExitPending() },
];

function fresh(marker: string) {
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });
  db.logEvent(0, "SessionStart", "SessionStart", null, { source: "startup", session_id: "replacement", cwd: "/x" });
  db.setExitPending(true, false, true, PROMPT, true, false);
  const episode = db.getExitStatus().episode!;
  db.setConfig("exit_pending_continuation_delivery", marker);
  // The confirmed delivery receipt: the no-resend authority.
  db.logEvent(0, "pm_restart_continuation", null, null, { episode, boot_event_id: 1, delivery: "delivered" });
  const sends: string[] = [];
  const relay = {
    observeExitAttempts: async () => undefined,
    hasPendingExitConfirmation: () => false,
    deliverGuardedToSlot: async (_slot: number, prompt: string) => { sends.push(prompt); return { outcome: "delivered" }; },
  };
  const build = () => new ProcessHealthChecker(db, relay as never) as any;
  return { db, episode, sends, build };
}

const state = (db: MoPDatabase) => {
  const status = db.getExitStatus();
  return {
    pending: status.pending, pm_only: status.pm_only, with_devs: status.with_devs, dev_only: status.dev_only,
    marker: status.continuation_delivery, completes: db.getEvents(0, 10, "exit_pending_complete").length,
  };
};

for (const entry of ENTRIES) {
  for (const fault of FAULTS) {
    test(`interrupted inside the PM-only terminal clear at ${fault.name} [${entry.name}]: all-or-nothing, then the next tick completes once with no resend`, async () => {
      const { db, episode, sends, build } = fresh(entry.marker);
      try {
        const before = state(db);
        assert.deepEqual(before, { pending: true, pm_only: true, with_devs: false, dev_only: false, marker: entry.marker, completes: 0 });
        const raw = (db as any).db;
        raw.exec(`CREATE TRIGGER crash_in_terminal_clear ${fault.sql} BEGIN SELECT RAISE(ABORT, 'MoP killed inside terminal clear'); END`);
        await assert.rejects(entry.run(build()), /killed inside terminal clear/);
        raw.exec("DROP TRIGGER crash_in_terminal_clear");

        // Never a torn episode: it reads exactly as before the clear, or it is fully complete.
        const afterCrash = state(db);
        const complete = { pending: false, pm_only: false, with_devs: false, dev_only: false, marker: "delivered", completes: 1 };
        assert.ok(
          JSON.stringify(afterCrash) === JSON.stringify(before) || JSON.stringify(afterCrash) === JSON.stringify(complete),
          "torn terminal clear: " + JSON.stringify(afterCrash),
        );
        assert.equal(db.getExitStatus().episode, episode);

        // A fresh process finishes it from the confirmed receipt: once, PM-only, with no second send.
        await entry.run(build());
        await entry.run(build());
        assert.deepEqual(state(db), complete);
        assert.equal(db.getExitStatus().episode, episode);
        assert.deepEqual(sends, [], "the confirmed receipt is the no-resend authority");
      } finally { db.close(); }
    });
  }
}

test("an uninterrupted PM-only terminal clear commits pending=false, the delivered marker and one completion record together", async () => {
  const { db, episode, sends, build } = fresh("attempted");
  try {
    await build().reconcilePMRestartContinuation();
    assert.deepEqual(state(db), { pending: false, pm_only: false, with_devs: false, dev_only: false, marker: "delivered", completes: 1 });
    const record = JSON.parse(db.getEvents(0, 1, "exit_pending_complete")[0]!.payload);
    assert.equal(record.episode, episode);
    assert.equal(record.source, "pm_restart_continuation");
    assert.deepEqual(sends, []);
  } finally { db.close(); }
});
