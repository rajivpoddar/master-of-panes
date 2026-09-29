import assert from "node:assert/strict";
import test from "node:test";

import { PMCadenceScheduler, hourlyHeartbeatDueKey, isHourlyHeartbeatWindow } from "../src/pmCadence.js";

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

test("hourly heartbeat is a cadence task that invokes Skill(hourly-heartbeat) in the background", () => {
  const cadence = new PMCadenceScheduler(new FakeMopDb() as never, new FakeRelay() as never);
  const hourly = cadence.getStatus().tasks.find((task) => task.task === "hourly-heartbeat");
  assert.ok(hourly);
  assert.equal(hourly.label, "1h heartbeat");
  assert.match(hourly.command, /Invoke Skill\(hourly-heartbeat\)/);
  assert.match(hourly.command, /run_in_background=true/);
  const heartbeat = cadence.getStatus().tasks.find((task) => task.task === "heartbeat");
  assert.match(heartbeat!.command, /Invoke Skill\(heartbeat-tasks\)/);
});

test("hourly due key is per local hour and fires only from :13", () => {
  const at = (h: number, m: number) => new Date(2026, 8, 29, h, m, 0);
  assert.equal(hourlyHeartbeatDueKey(at(9, 13)), "2026-09-29:09");
  assert.notEqual(hourlyHeartbeatDueKey(at(9, 59)), hourlyHeartbeatDueKey(at(10, 0)));
  assert.equal(isHourlyHeartbeatWindow(at(10, 12)), false);
  assert.equal(isHourlyHeartbeatWindow(at(10, 13)), true);
});

test("first scheduled tick seeds the hourly bucket instead of firing; manual run injects", async () => {
  const db = new FakeMopDb();
  const relay = new FakeRelay();
  const cadence = new PMCadenceScheduler(db as never, relay as never);
  await cadence.tick("scheduled");
  assert.equal(relay.injections.some((m) => m.includes("hourly-heartbeat")), false);
  const result = await cadence.runManual("hourly-heartbeat");
  assert.equal(result.injected, true);
  assert.equal(relay.injections.filter((m) => m.includes("Skill(hourly-heartbeat)")).length, 1);
});
