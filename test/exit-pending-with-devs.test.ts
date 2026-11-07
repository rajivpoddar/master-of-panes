// Obligation 30559 (CTO rewrite C0ALZJHGE49 ts 1793889129.015359; Rajiv DM D0AMF0XE6TS
// ts 1793713170.231979 "just turn on exit pending on all slots in mop at 4 am"): ONE 04:00
// occurrence arms the PM (save-handoff, Stop-only /exit, fresh restart + continuation) AND
// the active non-DND dev slots together. No second dev phase after the PM restart.
import assert from "node:assert/strict";
import test from "node:test";
import { MoPDatabase } from "../src/db.js";
import { HookProcessor } from "../src/hooks.js";
import { ProcessHealthChecker } from "../src/health.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const database = () => new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });
const rule = "─".repeat(40);

/** Arm-time cohort as the REST route records it: each live dev's session/epoch. */
function armCohort(db: MoPDatabase, slots: number[]) {
  const episode = db.getExitStatus().episode!;
  for (const slot of slots) {
    db.logEvent(slot, "SessionStart", "SessionStart", null, { session_id: `s${slot}` });
    db.recordExitArm(slot, episode, `s${slot}`, db.getSlot(slot)?.assignment_epoch ?? null);
  }
}

function devStopHarness(db: MoPDatabase) {
  const exits: Array<[number, string]> = [];
  const relay = {
    sendToSlot: (slot: number, command: string) => { exits.push([slot, command]); return true; },
    injectToPM: () => true,
    injectToPMDirect: () => true,
    hasPendingExitConfirmation: () => false,
    observeSlotPane: async () => ({ paneId: "%40" }),
    deliverGuardedToSlot: async (slot: number, text: string) => { exits.push([slot, text]); return { outcome: "delivered" }; },
  };
  return { exits, processor: new HookProcessor(db, relay as any) as any };
}

test("with_devs arms PM and every dev in ONE episode; plain pm_only keeps devs untouched", () => {
  const db = database();
  try {
    for (const slot of [1, 3]) db.markSlotExitCycled(slot);
    db.setExitPending(true, false, true, "resume", true, true);
    const status = db.getExitStatus();
    assert.equal(status.pm_only, true); assert.equal(status.with_devs, true);
    for (let i = 0; i <= 8; i++) assert.equal(status.cycled[i], false, `slot ${i} armed`);
    for (const slot of [1, 3]) db.markSlotExitCycled(slot);
    db.setExitPending(true, false, true, "resume", true);
    assert.equal(db.getExitStatus().with_devs, false);
    assert.equal(db.getExitStatus().cycled[1], true, "PM-only arming never resets a dev flag");
  } finally { db.close(); }
});

test("with_devs: a dev Stop sends /exit in the same episode; DND skipped; no auto-clear before the PM continuation", async () => {
  const db = database();
  try {
    db.setExitPending(true, false, true, "resume", true, true);
    const episode = db.getExitStatus().episode;
    armCohort(db, [1, 2, 3]);
    db.updateSlot(2, { occupied: true, dnd: true });
    const { exits, processor } = devStopHarness(db);
    await processor.process(1, { type: "Stop", session_id: "s1" });
    await processor.process(2, { type: "Stop", session_id: "s2" });
    assert.deepEqual(exits, [[1, "/exit"]]);
    assert.equal(db.getExitStatus().cycled[1], true);
    assert.equal(db.getExitStatus().cycled[2], true, "DND dev: flag consumed, never exited (a DND release cannot inherit it)");
    // Every slot cycled (PM included) still keeps the episode until the continuation lands.
    for (let i = 0; i <= 8; i++) db.markSlotExitCycled(i);
    db.setConfig("exit_cycled_3", "false");
    await processor.process(3, { type: "Stop", session_id: "s3" });
    const status = db.getExitStatus();
    assert.equal(status.pending, true); assert.equal(status.pm_only, true); assert.equal(status.episode, episode);
  } finally { db.close(); }
});

test("plain pm_only (no with_devs): dev Stops never send /exit", async () => {
  const db = database();
  try {
    db.setExitPending(true, false, true, "resume", true);
    const { exits, processor } = devStopHarness(db);
    db.logEvent(1, "SessionStart", "SessionStart", null, { session_id: "s1" });
    await processor.process(1, { type: "Stop", session_id: "s1" });
    assert.deepEqual(exits, []);
  } finally { db.close(); }
});

test("with_devs health scan: devs exit only at an idle EMPTY prompt; the PM is never scanned", async () => {
  const db = database();
  try {
    db.setExitPending(true, false, true, "resume", true, true);
    armCohort(db, [1, 2, 3, 4, 5, 6, 7, 8]);
    const sent: Array<[number, string, any]> = [];
    const scanned: number[] = [];
    const health = new ProcessHealthChecker(db, {
      observeSlotPane: async (slot: number) => {
        scanned.push(slot);
        return { paneId: `%${slot}`, text: `${rule}\n❯ ${slot === 2 ? "typed work" : ""}\n${rule}` };
      },
      getSlotActivityState: async () => "idle",
      deliverGuardedToSlot: async (slot: number, text: string, opts: any) => { sent.push([slot, text, opts]); return { outcome: "delivered" }; },
      injectToPM: () => true,
      hasPendingExitConfirmation: () => false,
    } as any) as any;
    health.getPaneCommand = async () => "claude";
    await health.checkExitPending();
    assert.equal(scanned.includes(0), false, "PM exits only at its Stop");
    assert.equal(sent.some(([slot]) => slot === 2), false, "non-empty composer is never sent /exit");
    assert.ok(sent.length > 0 && sent.every(([, text, opts]) => text === "/exit" && opts.skipSlashClear === true));
    assert.equal(db.getExitStatus().pm_only, true, "episode stays PM-pending");
  } finally { db.close(); }
});

for (const devsDone of [false, true]) {
  test(`with_devs continuation keeps the episode and dev flags (devs ${devsDone ? "all" : "partly"} cycled)`, async () => {
    const db = database();
    try {
      db.setExitPending(true, false, true, "saved continuation", true, true);
      const episode = db.getExitStatus().episode;
      db.markSlotExitCycled(0);
      for (let i = 1; i <= 8; i++) if (devsDone || i === 1) db.markSlotExitCycled(i);
      const messages: string[] = [];
      const health = new ProcessHealthChecker(db, {
        deliverGuardedToSlot: async (_slot: number, text: string) => { messages.push(text); return { outcome: "delivered" }; },
        hasPendingExitConfirmation: () => false,
      } as any) as any;
      await health.deliverPMRestartContinuation("%40");
      await health.deliverPMRestartContinuation("%40");
      assert.deepEqual(messages, ["saved continuation"]);
      const status = db.getExitStatus();
      assert.equal(status.pm_only, false);
      if (devsDone) {
        assert.equal(status.pending, false, "episode complete");
        assert.equal(db.getEvents(0, 1, "exit_pending_complete").length, 1);
      } else {
        assert.equal(status.continuation_delivery, "delivered");
        assert.equal(status.pending, true); assert.equal(status.episode, episode, "no new dev phase");
        assert.equal(status.dev_only, true); assert.equal(status.stop_only, true);
        assert.equal(status.with_devs, true, "the arm-time cohort marker is kept");
        assert.equal(status.cycled[1], true, "cycled dev is never re-armed");
        assert.equal(status.cycled[2], false);
      }
    } finally { db.close(); }
  });
}

// CTO REVISE ts 1794018596.287469: the 04:00 occurrence is an arm-time cohort.
for (const change of ["new_session", "new_assignment", "never_armed"] as const) {
  test(`with_devs cohort: ${change} consumes the flag without an exit (Stop and health scan)`, async () => {
    const db = database();
    try {
      db.setExitPending(true, false, true, "resume", true, true);
      if (change !== "never_armed") armCohort(db, [1, 2]);
      if (change === "new_session") {
        for (const slot of [1, 2]) db.logEvent(slot, "SessionStart", "SessionStart", null, { session_id: `later-${slot}` });
      }
      if (change === "new_assignment") {
        for (const slot of [1, 2]) (db as any).db.prepare("UPDATE slots SET assignment_epoch = assignment_epoch + 1 WHERE slot = ?").run(slot);
      }
      const { exits, processor } = devStopHarness(db);
      await processor.process(1, { type: "Stop", session_id: change === "new_session" ? "later-1" : "s1" });
      const sent: number[] = [];
      const health = new ProcessHealthChecker(db, {
        observeSlotPane: async (slot: number) => ({ paneId: `%${slot}`, text: `${rule}\n❯ \n${rule}` }),
        getSlotActivityState: async () => "idle",
        deliverGuardedToSlot: async (slot: number) => { sent.push(slot); return { outcome: "delivered" }; },
        injectToPM: () => true, hasPendingExitConfirmation: () => false,
      } as any) as any;
      health.getPaneCommand = async () => "claude";
      await health.checkExitPending();
      assert.deepEqual(exits, [], "no /exit at Stop");
      assert.equal(sent.includes(2), false, "no /exit from the scan");
      for (const slot of [1, 2]) assert.equal(db.getExitStatus().cycled[slot], true, "consumed and retained");
      assert.ok(db.getEvents(1, 1, "exit_pending_cohort_superseded").length === 1);
    } finally { db.close(); }
  });
}
