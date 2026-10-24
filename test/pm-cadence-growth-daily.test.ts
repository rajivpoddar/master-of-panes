import assert from "node:assert/strict";
import test from "node:test";

import { PMCadenceScheduler, isGrowthDailyWindow } from "../src/pmCadence.js";

class FakeMopDb {
  private readonly config = new Map<string, string>();
  getConfig(key: string): string | null {
    return this.config.get(key) ?? null;
  }
  setConfig(key: string, value: string): void {
    this.config.set(key, value);
  }
  logEvent(): void {}
  hasPMQueueDelivery(): boolean {
    return false;
  }
}

class FakeRelay {
  injections: string[] = [];
  async submitToPM(message: string): Promise<{ ok: boolean }> {
    this.injections.push(message);
    return { ok: true };
  }
}

// Rajiv C0C7U35LBKR thread 1792821492.101599, ts 1792823200.845199: "add it at 10:30 am each day".
const PROMPT =
  "MoP: growth report due. Invoke Skill(growth-daily) now: launch the growth background agent with run_in_background=true (Mondays: Skill(growth-weekly)). Do not run it inline.";

test("growth-daily is a listed cadence task with the pinned prompt", () => {
  const cadence = new PMCadenceScheduler(new FakeMopDb() as never, new FakeRelay() as never);
  const task = cadence.getStatus().tasks.find((t) => t.task === "growth-daily");
  assert.ok(task);
  assert.equal(task.command, PROMPT);
  assert.equal(task.paused, false);
  assert.match(task.current_due_key, /^\d{4}-\d{2}-\d{2}$/);
});

test("growth-daily manual run injects the pinned prompt", async () => {
  const relay = new FakeRelay();
  const cadence = new PMCadenceScheduler(new FakeMopDb() as never, relay as never);
  const result = await cadence.runManual("growth-daily");
  assert.equal(result.task, "growth-daily");
  assert.deepEqual(relay.injections, [PROMPT]);
});

test("growth-daily window opens at 10:30 local, not 10:00", () => {
  const at = (h: number, m: number) => new Date(2026, 9, 24, h, m, 0);
  assert.equal(isGrowthDailyWindow(at(10, 29)), false);
  assert.equal(isGrowthDailyWindow(at(10, 30)), true);
  assert.equal(isGrowthDailyWindow(at(23, 59)), true);
  assert.equal(isGrowthDailyWindow(at(9, 45)), false);
});

test("growth-daily pauses per task and shows in status", () => {
  const cadence = new PMCadenceScheduler(new FakeMopDb() as never, new FakeRelay() as never);
  cadence.setPaused(true, "growth-daily");
  const st = cadence.getStatus();
  assert.equal(st.tasks.find((t) => t.task === "growth-daily")!.paused, true);
  assert.equal(st.tasks.find((t) => t.task === "morning-brief")!.paused, false);
});
