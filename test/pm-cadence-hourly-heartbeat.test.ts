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

test("boot never injects stale persisted cadence work; scheduled ticks use the live bucket", async () => {
  const db = new FakeMopDb();
  const relay = new FakeRelay();
  db.setConfig("pm_cadence_hourly_heartbeat_last_due_key", "2026-11-13:02");
  const cadence = new PMCadenceScheduler(db as never, relay as never);
  await withClock(new Date(2026, 10, 13, 10, 20, 0), () => cadence.tick("boot"));
  assert.deepEqual(relay.injections, []);

  // Stale relay rows are independently rejected at their delivery boundary.
  assert.deepEqual(
    await withClock(new Date(2026, 10, 13, 11, 13, 0), () =>
      Promise.resolve(cadence.revalidateQueuedReminder("cadence-hourly-heartbeat-2026-11-13:02"))),
    { drop: "cadence-bucket-expired" },
  );

  // The next regular pass is evaluated from its current time, never the old boot key.
  await withClock(new Date(2026, 10, 13, 11, 13, 0), () => cadence.tick("scheduled"));
  assert.equal(relay.injections.length, 1);
});

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

test("boot before the morning-brief window leaves today's key for the 10:00 regular tick", async () => {
  const db = new FakeMopDb();
  const relay = new FakeRelay();
  const cadence = new PMCadenceScheduler(db as never, relay as never);
  const brief = () => relay.injections.filter((m) => m.includes("Skill(morning-brief)")).length;

  await withClock(new Date(2026, 10, 14, 9, 59, 0), () => cadence.tick("boot"));
  assert.equal(brief(), 0);
  assert.equal(db.getConfig("pm_cadence_morning_brief_last_due_key"), null);

  await withClock(new Date(2026, 10, 14, 10, 0, 0), () => cadence.tick("scheduled"));
  assert.equal(brief(), 1);
});

test("boot after the morning-brief window never delivers and suppresses only until tomorrow's window", async () => {
  const db = new FakeMopDb();
  const relay = new FakeRelay();
  const cadence = new PMCadenceScheduler(db as never, relay as never);
  const brief = () => relay.injections.filter((m) => m.includes("Skill(morning-brief)")).length;

  await withClock(new Date(2026, 10, 14, 10, 1, 0), () => cadence.tick("boot"));
  assert.equal(brief(), 0, "boot must never inject cadence work");
  assert.equal(db.getConfig("pm_cadence_morning_brief_last_due_key"), "2026-11-14");

  // The next regular tick sees today's bucket as handled; the next daily window
  // (2026-11-15 at 10:00 local) is the next eligible regular delivery.
  await withClock(new Date(2026, 10, 14, 10, 2, 0), () => cadence.tick("scheduled"));
  assert.equal(brief(), 0);
  await withClock(new Date(2026, 10, 15, 10, 0, 0), () => cadence.tick("scheduled"));
  assert.equal(brief(), 1);
});


test("start before the morning window keeps it eligible when boot timer crosses 10:00", async (t) => {
  const db = new FakeMopDb();
  const relay = new FakeRelay();
  const cadence = new PMCadenceScheduler(db as never, relay as never);
  t.mock.timers.enable({ apis: ["Date", "setTimeout", "setInterval"], now: new Date(2026, 10, 14, 9, 59, 30) });
  try {
    cadence.start();
    t.mock.timers.tick(45_000);
    // The timer callback intentionally does not await the asynchronous tick.
    for (let i = 0; i < 30; i++) await Promise.resolve();
    assert.equal(db.getConfig("pm_cadence_morning_brief_last_due_key"), null);
    assert.equal(relay.injections.some((m) => m.includes("Skill(morning-brief)")), false);
    await cadence.tick("scheduled");
    assert.equal(relay.injections.filter((m) => m.includes("Skill(morning-brief)")).length, 1);
  } finally {
    cadence.stop();
    t.mock.timers.reset();
  }
});
