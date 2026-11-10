import assert from "node:assert/strict";
import test from "node:test";
import { MoPDatabase } from "../src/db.js";
import { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";

function harness() {
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });
  const relay = new TmuxRelay(DEFAULT_CONFIG, { runShell: async () => ({ stdout: "", stderr: "" }) });
  relay.setDatabase(db);
  relay.exitSettleMs = 0;
  const notices: string[] = [];
  let attempts = 0;
  let seenStop: number | null = null;
  relay.injectToPM = (message: string) => { notices.push(message); return true; };
  let mode: "refuse" | "busy" | "delivered" = "refuse";
  relay.sendToSlotAsync = async (_slot, _command, _force, _raw, _episode, pin) => {
    attempts++;
    seenStop = pin?.stop ?? null;
    if (mode === "busy") {
      db.logEvent(1, "guarded_delivery", null, null, {
        outcome: "refused_pre_effect", reason: "pane_not_idle",
      });
    }
    return mode === "delivered";
  };
  const exitOnce = async (stop = 1) => {
    relay.sendToSlot(1, "/exit", false, false, "episode-1", "sess", {
      assignment: "pinned", boot: 1, input: 1, work: null, turn: null,
      turnStarted: null, turnState: "inactive", stopSession: "sess", stop,
    });
    await new Promise((resolve) => setTimeout(resolve, 0));
  };
  return {
    db, notices, exitOnce, attempts: () => attempts, seenStop: () => seenStop,
    setMode: (next: typeof mode) => { mode = next; },
  };
}

test("same episode refused three times injects one PM notice and still retries", async () => {
  const box = harness();
  await box.exitOnce();
  await box.exitOnce();
  await box.exitOnce();
  assert.deepEqual(box.notices, ["# slot 1 /exit dispatch_refused_before_attempt"]);
  assert.equal(box.attempts(), 3);
  assert.equal(box.db.getEvents(1, 10, "exit_dispatch_refused").length, 3);
  assert.equal(box.db.getEvents(1, 10, "exit_pm_notice_suppressed").length, 2);
});

test("a changed refusal reason injects a second notice once", async () => {
  const box = harness();
  await box.exitOnce();
  await box.exitOnce();
  box.setMode("busy");
  await box.exitOnce();
  await box.exitOnce();
  assert.deepEqual(box.notices, [
    "# slot 1 /exit dispatch_refused_before_attempt",
    "# slot 1 /exit refused_pre_effect: pane_not_idle",
  ]);
  assert.equal(box.attempts(), 4);
});

test("a Stop that continues the turn does not attempt /exit or notify", async () => {
  const box = harness();
  box.db.updateSlot(1, { active_turn_state: "active", active_turn_id: "still-going" });
  await box.exitOnce();
  assert.deepEqual(box.notices, []);
  assert.equal(box.attempts(), 0);
  const skipped = box.db.getEvents(1, 5, "exit_dispatch_skipped");
  assert.equal(skipped.length, 1);
  assert.equal(JSON.parse(skipped[0].payload).reason, "turn_active");
});

test("a continued Stop is skipped and the following real Stop attempts once", async () => {
  const box = harness();
  box.db.logEvent(1, "PostToolUse", "PostToolUse", "Edit", { session_id: "sess" });
  await box.exitOnce(0);
  assert.equal(box.attempts(), 0);
  assert.deepEqual(box.notices, []);
  const stopId = box.db.logEvent(1, "Stop", "Stop", null, { session_id: "sess", mop_stop_allowed: 1 });
  const duplicate = box.db.logEvent(1, "Stop", "Stop", null, { session_id: "sess", mop_stop_allowed: 1 });
  await box.exitOnce(stopId);
  assert.equal(box.attempts(), 1);
  assert.equal(box.seenStop(), duplicate);
  assert.equal(box.notices.length, 1);
});

test("a delivered result notifies once even after the same episode was refused", async () => {
  const box = harness();
  await box.exitOnce();
  box.setMode("delivered");
  await box.exitOnce();
  await box.exitOnce();
  assert.deepEqual(box.notices, [
    "# slot 1 /exit dispatch_refused_before_attempt",
    "# slot 1 /exit delivered; cycle unverified",
  ]);
  assert.equal(box.attempts(), 3);
});
