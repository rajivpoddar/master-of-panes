import assert from "node:assert/strict";
import test from "node:test";
import { MoPDatabase } from "../src/db.js";
import { ProcessHealthChecker } from "../src/health.js";
import { DEFAULT_CONFIG } from "../src/types.js";

// Episode f0a1b666 (2026-11-04 08:26 IST): dev-only exit stayed pending while
// slots sat idle at the prompt after an Escape-ended turn (no Stop hook, row
// still active_turn_state=active). An idle pane at an EMPTY prompt now gets its
// /exit from the health scan; a working turn or a draft is never touched, and
// the send never presses Escape/C-u.
const database = () => new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });
const RULE = "─".repeat(40);
const pane = (composer: string) => `output\n${RULE}\n❯ ${composer}\n${RULE}\n  ? for shortcuts\n`;

type Case = { name: string; stopOnly: boolean; activity: string; composer: string; expectExit: boolean };
const cases: Case[] = [
  { name: "REST dev-only, Escape-ended turn, idle empty prompt", stopOnly: false, activity: "idle", composer: "", expectExit: true },
  { name: "PM-successor (stop_only), idle empty prompt", stopOnly: true, activity: "idle", composer: "", expectExit: true },
  { name: "working turn", stopOnly: false, activity: "busy", composer: "", expectExit: false },
  { name: "idle pane with a draft", stopOnly: true, activity: "idle", composer: "half-typed draft", expectExit: false },
];

for (const c of cases) {
  test(`dev-only exit at idle prompt: ${c.name} -> ${c.expectExit ? "one /exit" : "untouched"}`, async () => {
    const db = database();
    const sent: Array<[number, string, boolean]> = [];
    const observed: number[] = [];
    try {
      db.setExitPending(true, true);
      if (c.stopOnly) db.setConfig("exit_pending_stop_only", "true");
      for (const slot of Object.keys(db.getExitStatus().cycled).map(Number).filter((n) => n > 1)) db.markSlotExitCycled(slot);
      db.updateSlot(1, { occupied: true, issue: 1, active_turn_state: "active", active_turn_id: "turn-1" } as any);
      const relay = {
        observeSlotPane: async (slot: number) => { observed.push(slot); return { paneId: `%${slot}`, text: pane(c.composer) }; },
        getSlotActivityState: async () => c.activity,
        deliverGuardedToSlot: async (slot: number, command: string, guard: any) => {
          assert.equal(await guard.recheck(), null);
          assert.equal(guard.finalCheck(), null);
          sent.push([slot, command, guard.skipSlashClear === true]);
          return { outcome: "delivered" };
        },
        injectToPM() {},
      };
      const health = new ProcessHealthChecker(db, relay as any) as any;
      health.getPaneCommand = async () => "claude";
      await health.checkExitPending();
      await health.checkExitPending();
      assert.equal(observed.includes(0), false, "PM is never scanned in a dev-only episode");
      if (c.expectExit) {
        assert.deepEqual(sent, [[1, "/exit", true]], "exactly one /exit, no Escape/C-u pre-clear");
        assert.equal(db.getExitPending(), false, "all dev slots consumed");
      } else {
        assert.deepEqual(sent, []);
        assert.equal(db.getExitStatus().cycled[1], false);
        assert.equal(db.getExitPending(), true);
      }
    } finally { db.close(); }
  });
}
