import assert from "node:assert/strict";
import test from "node:test";
import { PMCadenceScheduler } from "../src/pmCadence.js";

const TASK = "rtb-split-update";
const PREFIX = "pm_cadence_rtb_split_update";
const PROMPT = "MoP: post the 30-min remote-box split update to C0ALZJHGE49 thread 1793299570.404429 via a bg agent: RTB candidate state, pool usage, cx33 status, next step.";

class FakeDb {
  config = new Map<string, string>();
  delivered = new Set<string>();
  getConfig(key: string) { return this.config.get(key) ?? null; }
  setConfig(key: string, value: string) { this.config.set(key, value); }
  logEvent() {}
  hasPMQueueDelivery(_slot: number, event: string) { return this.delivered.has(event); }
}
class FakeRelay {
  calls: Array<{ message: string; event: string }> = [];
  queued = false;
  async submitToPM(message: string, event: string) {
    this.calls.push({ message, event });
    return { ok: true, queued: this.queued };
  }
}
function fixture() {
  const db = new FakeDb();
  const relay = new FakeRelay();
  const cadence = new PMCadenceScheduler(db as never, relay as never);
  return { db, relay, cadence, calls: () => relay.calls.filter(c => c.message === PROMPT) };
}
async function at(hour: number, minute: number, fn: () => Promise<unknown>, day = 30) {
  const RealDate = Date;
  const time = new RealDate(2026, 9, day, hour, minute).getTime();
  class Clock extends RealDate {
    constructor(...args: unknown[]) { super(args.length ? args[0] as number : time); }
    static now() { return time; }
  }
  globalThis.Date = Clock as DateConstructor;
  try { await fn(); } finally { globalThis.Date = RealDate; }
}

test("RTB task is active by default and queues the exact normal prompt", async () => {
  const { cadence, relay, calls } = fixture();
  const status = cadence.getStatus().tasks.find(t => t.task as string === TASK);
  assert.ok(status, "rtb-split-update must be registered");
  assert.equal(status.paused, false);
  assert.equal(status.command, PROMPT);
  relay.queued = true;
  const result = await cadence.runManual(TASK as never);
  assert.equal(result.queued, true);
  assert.equal(result.injected, false);
  assert.equal(calls().length, 1);
  assert.match(calls()[0].event, /^cadence-rtb-split-update-/);
});

test("scheduled ticks fire at :13 and :43, once per local half hour", async () => {
  const { cadence, calls } = fixture();
  for (const minute of [5, 12]) await at(12, minute, () => cadence.tick("scheduled"));
  assert.equal(calls().length, 0);
  await at(12, 13, () => cadence.tick("scheduled"));
  assert.equal(calls().length, 1);
  assert.equal(calls()[0].event, "cadence-rtb-split-update-2026-10-30:12:13");
  await at(12, 42, () => cadence.tick("scheduled"));
  assert.equal(calls().length, 1);
  await at(12, 43, () => cadence.tick("scheduled"));
  assert.equal(calls().length, 2);
  await at(13, 12, () => cadence.tick("scheduled"));
  assert.equal(calls().length, 2);
  await at(13, 13, () => cadence.tick("scheduled"));
  assert.equal(calls().length, 3);
});

test("first boot seeds without catch-up; restart does not replay a delivered bucket", async () => {
  const { db, relay, cadence, calls } = fixture();
  await at(12, 20, () => cadence.tick("boot"));
  assert.equal(calls().length, 0);
  await at(12, 43, () => cadence.tick("scheduled"));
  assert.equal(calls().length, 1);
  const restarted = new PMCadenceScheduler(db as never, relay as never);
  await at(12, 50, () => restarted.tick("boot"));
  assert.equal(calls().length, 1);
  await at(13, 13, () => restarted.tick("scheduled"));
  assert.equal(calls().length, 2);
});

test("queue delivery receipts reconcile without requeueing, including across midnight", async () => {
  const { db, relay, cadence, calls } = fixture();
  relay.queued = true;
  await at(23, 42, () => cadence.tick("boot"));
  await at(23, 43, () => cadence.tick("scheduled"));
  assert.equal(calls().length, 1);
  assert.notEqual(db.getConfig(`${PREFIX}_last_due_key`), "2026-10-30:23:43");
  db.delivered.add(calls()[0].event);
  await at(0, 12, () => cadence.tick("scheduled"), 31);
  assert.equal(calls().length, 1);
  assert.equal(db.getConfig(`${PREFIX}_last_due_key`), "2026-10-30:23:43");
  await at(0, 13, () => cadence.tick("scheduled"), 31);
  assert.equal(calls().length, 2);
  assert.equal(calls()[1].event, "cadence-rtb-split-update-2026-10-31:00:13");
});

test("operator can pause the task until resumed, with no automatic expiry", async () => {
  const { cadence, calls } = fixture();
  await at(12, 12, () => cadence.tick("boot"));
  cadence.setPaused(true, TASK as never);
  await at(12, 13, () => cadence.tick("scheduled"));
  await at(12, 43, () => cadence.tick("scheduled"));
  assert.equal(calls().length, 0);
  cadence.setPaused(false, TASK as never);
  await at(13, 13, () => cadence.tick("scheduled"));
  assert.equal(calls().length, 1);
});
