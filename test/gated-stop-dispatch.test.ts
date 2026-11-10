// CTO REVISE c69f920 (C0ALZJHGE49 ts 1794274758.572099): a gated slot's
// allowed Stop is the disposition. Quiet time is not. A later Stop that
// arrives while the first exit op is still pending is kept, not dropped.
import assert from "node:assert/strict";
import test from "node:test";
import { MoPDatabase } from "../src/db.js";
import { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";

function pin(stop: number) {
  return {
    assignment: "pinned", boot: 1, input: 1, work: null, turn: null,
    turnStarted: null, turnState: "inactive" as const, stopSession: "sess", stop,
  };
}

function harness() {
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });
  const relay = new TmuxRelay(DEFAULT_CONFIG, { runShell: async () => ({ stdout: "", stderr: "" }) });
  relay.setDatabase(db);
  relay.exitSettleMs = 5_000;
  const seen: number[] = [];
  relay.sendToSlotAsync = async (_slot, _command, _force, _raw, _episode, owner) => {
    seen.push(owner?.stop ?? -1);
    return true;
  };
  relay.injectToPM = () => true;
  return { db, relay, seen };
}

test("a delayed blocked decision on a gated slot sends nothing", async () => {
  const box = harness();
  try {
    // The raw Stop path used to finish the turn immediately. The block
    // decision lands later and never marks the Stop allowed.
    box.db.updateSlot(1, { active_turn_state: "inactive", active_turn_id: null });
    const stop = box.db.logEvent(1, "Stop", "Stop", null, { session_id: "sess" });
    const started = Date.now();
    box.relay.sendToSlot(1, "/exit", false, false, "ep", "sess", pin(stop));
    await new Promise((resolve) => setTimeout(resolve, 200));
    assert.deepEqual(box.seen, []);
    assert.ok(Date.now() - started < 1_000, "did not wait out the quiet window");
    const skipped = box.db.getEvents(1, 5, "exit_dispatch_skipped");
    assert.equal(skipped.length, 1);
    assert.equal(JSON.parse(skipped[0].payload).reason, "stop_not_allowed");
  } finally {
    box.db.close();
  }
});

test("a real Stop overlapping the pending window dispatches once", async () => {
  const box = harness();
  try {
    box.db.updateSlot(1, { active_turn_state: "active", active_turn_id: "going" });
    const blocked = box.db.logEvent(1, "Stop", "Stop", null, { session_id: "sess" });
    box.relay.sendToSlot(1, "/exit", false, false, "ep", "sess", pin(blocked));
    box.db.updateSlot(1, { active_turn_state: "inactive", active_turn_id: null });
    const allowed = box.db.logEvent(1, "Stop", "Stop", null, { session_id: "sess", mop_stop_allowed: 1 });
    box.relay.sendToSlot(1, "/exit", false, false, "ep", "sess", pin(allowed));
    await new Promise((resolve) => setTimeout(resolve, 50));
    assert.deepEqual(box.seen, [allowed]);
  } finally {
    box.db.close();
  }
});
