import assert from "node:assert/strict";
import test from "node:test";
import { MoPDatabase } from "../src/db.js";
import { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const pin = {
  assignment: "pinned", boot: 1, input: 1, work: null, turn: null,
  turnStarted: null, turnState: "inactive", stopSession: "sess", stop: 1,
};

function harness() {
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });
  const relay = new TmuxRelay(DEFAULT_CONFIG, { runShell: async () => ({ stdout: "", stderr: "" }) });
  relay.setDatabase(db);
  const notices: string[] = [];
  let attempts = 0;
  relay.injectToPM = (message: string) => { notices.push(message); return true; };
  let mode: "refuse" | "busy" | "delivered" = "refuse";
  relay.sendToSlotAsync = async () => {
    attempts++;
    if (mode === "busy") {
      db.logEvent(1, "guarded_delivery", null, null, {
        outcome: "refused_pre_effect", reason: "pane_not_idle",
      });
    }
    return mode === "delivered";
  };
  const exitOnce = async () => {
    relay.sendToSlot(1, "/exit", false, false, "episode-1", "sess", pin);
    await new Promise((resolve) => setTimeout(resolve, 0));
  };
  return {
    db, notices, exitOnce, attempts: () => attempts,
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
