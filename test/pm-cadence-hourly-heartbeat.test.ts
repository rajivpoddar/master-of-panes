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
  // Pinned exact one-line injects (Rajiv 2026-09-29 20:15 IST, DM 1790693148.609479).
  // The old multi-line "1h/3h heartbeat due ... Invoke Skill(...)" text regressed once
  // when a release was rebuilt from a branch that lacked this change.
  assert.equal(hourly.command, "MoP: run Skill(hourly-heartbeat) now with a background agent.");
  const heartbeat = cadence.getStatus().tasks.find((task) => task.task === "heartbeat");
  assert.equal(heartbeat!.command, "MoP: run Skill(heartbeat-tasks) now with a background agent.");
  assert.equal(hourly.command.includes("\n"), false);
  assert.equal(heartbeat!.command.includes("\n"), false);
});

test("hourly and 3h heartbeat injects delivered to the PM are the pinned one-line texts", async () => {
  const relay = new FakeRelay();
  const cadence = new PMCadenceScheduler(new FakeMopDb() as never, relay as never);
  await cadence.runManual("hourly-heartbeat");
  await cadence.runManual("heartbeat");
  assert.deepEqual(relay.injections, [
    "MoP: run Skill(hourly-heartbeat) now with a background agent.",
    "MoP: run Skill(heartbeat-tasks) now with a background agent.",
  ]);
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

async function withClock<T>(at: Date, fn: () => Promise<T>): Promise<T> {
  const RealDate = Date;
  class FakeDate extends RealDate {
    constructor(...args: unknown[]) {
      if (args.length === 0) super(at.getTime());
      else super(...(args as [number]));
    }
    static now(): number {
      return at.getTime();
    }
  }
  globalThis.Date = FakeDate as DateConstructor;
  try {
    return await fn();
  } finally {
    globalThis.Date = RealDate;
  }
}

test("startup before :13 does not suppress that hour's :13 hourly heartbeat", async () => {
  const db = new FakeMopDb();
  const relay = new FakeRelay();
  const cadence = new PMCadenceScheduler(db as never, relay as never);
  const hourly = () => relay.injections.filter((m) => m.includes("Skill(hourly-heartbeat)")).length;
  await withClock(new Date(2026, 8, 29, 10, 5, 0), () => cadence.tick("boot"));
  assert.equal(hourly(), 0);
  await withClock(new Date(2026, 8, 29, 10, 12, 0), () => cadence.tick("scheduled"));
  assert.equal(hourly(), 0);
  await withClock(new Date(2026, 8, 29, 10, 13, 0), () => cadence.tick("scheduled"));
  assert.equal(hourly(), 1);
  await withClock(new Date(2026, 8, 29, 10, 40, 0), () => cadence.tick("scheduled"));
  assert.equal(hourly(), 1);
});

test("startup at or after :13 seeds the current hour and fires next hour", async () => {
  const db = new FakeMopDb();
  const relay = new FakeRelay();
  const cadence = new PMCadenceScheduler(db as never, relay as never);
  const hourly = () => relay.injections.filter((m) => m.includes("Skill(hourly-heartbeat)")).length;
  await withClock(new Date(2026, 8, 29, 10, 20, 0), () => cadence.tick("boot"));
  await withClock(new Date(2026, 8, 29, 10, 50, 0), () => cadence.tick("scheduled"));
  assert.equal(hourly(), 0);
  await withClock(new Date(2026, 8, 29, 11, 13, 0), () => cadence.tick("scheduled"));
  assert.equal(hourly(), 1);
});
