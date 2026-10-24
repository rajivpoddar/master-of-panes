import assert from "node:assert/strict";
import test from "node:test";

import { PMCadenceScheduler } from "../src/pmCadence.js";

// CTO REVISE C0ALZJHGE49 ts 1792824876.459089: growth-daily is the sole automatic
// Growth launcher; the morning-brief injection must not launch Growth.
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

const isBrief = (m: string) => m.includes("Skill(morning-brief)");
const isGrowth = (m: string) => m.includes("Skill(growth-daily)");

async function runDay(
  cadence: PMCadenceScheduler,
  relay: FakeRelay,
  day: number,
  times: Array<[number, number]>,
): Promise<void> {
  for (const [h, m] of times) {
    await withClock(new Date(2026, 9, day, h, m, 0), () => cadence.tick("scheduled"));
  }
}

test("only growth-daily launches Growth; brief held past 10:30, once per day, Monday carries weekly", async () => {
  const db = new FakeMopDb();
  const relay = new FakeRelay();
  const cadence = new PMCadenceScheduler(db as never, relay as never);
  // Sat 2026-10-24 and Mon 2026-10-26; brief pass is first due at 10:45 (held beyond 10:30).
  for (const day of [24, 26]) {
    const before = relay.injections.length;
    await runDay(cadence, relay, day, [[9, 0], [10, 29], [10, 45], [11, 15], [14, 0], [23, 30]]);
    const today = relay.injections.slice(before);
    const briefs = today.filter(isBrief);
    const growth = today.filter(isGrowth);
    assert.equal(briefs.length, 1, `day ${day}: one brief`);
    assert.equal(growth.length, 1, `day ${day}: growth-daily exactly once`);
    for (const b of briefs) {
      assert.doesNotMatch(b, /growth/i, "morning-brief injection must not launch Growth");
    }
    // Growth launched only by the growth-daily injection; no other injection mentions Growth.
    assert.equal(today.filter((m) => /growth/i.test(m)).length, 1);
    // Weekly variant is carried by the single growth-daily prompt (Mondays).
    assert.match(growth[0], /Mondays: Skill\(growth-weekly\)/);
  }
});

test("growth-daily does not fire before 10:30 even when brief fires", async () => {
  const db = new FakeMopDb();
  const relay = new FakeRelay();
  const cadence = new PMCadenceScheduler(db as never, relay as never);
  await runDay(cadence, relay, 26, [[9, 0], [10, 29]]);
  assert.equal(relay.injections.filter(isBrief).length, 1);
  assert.equal(relay.injections.filter(isGrowth).length, 0);
});

test("same-day restart does not re-fire growth-daily or the brief", async () => {
  const db = new FakeMopDb();
  const relay = new FakeRelay();
  await runDay(new PMCadenceScheduler(db as never, relay as never), relay, 24, [[10, 45]]);
  const count = () => relay.injections.filter((m) => isBrief(m) || isGrowth(m)).length;
  const n = count();
  const restarted = new PMCadenceScheduler(db as never, relay as never);
  await withClock(new Date(2026, 9, 24, 12, 0, 0), () => restarted.tick("boot"));
  await runDay(restarted, relay, 24, [[12, 5], [18, 0]]);
  assert.equal(count(), n);
  assert.equal(n, 2);
  assert.equal(relay.injections.filter(isGrowth).length, 1);
});

test("paused growth-daily does not launch Growth; brief still fires", async () => {
  const db = new FakeMopDb();
  const relay = new FakeRelay();
  const cadence = new PMCadenceScheduler(db as never, relay as never);
  cadence.setPaused(true, "growth-daily");
  await runDay(cadence, relay, 26, [[10, 45], [15, 0]]);
  assert.equal(relay.injections.filter(isGrowth).length, 0);
  assert.equal(relay.injections.filter(isBrief).length, 1);
  assert.equal(relay.injections.filter((m) => /growth/i.test(m)).length, 0);
});
