import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { readFile } from "node:fs/promises";
import { basename, join } from "node:path";
import test from "node:test";

import {
  RecentJsonlActivity,
  type JsonlActivitySignal,
} from "../src/jsonlActivity.js";
import { decidePMDrain } from "../src/relay.js";

const sourceRoot = process.env.MOP_SOURCE_ROOT ?? join(import.meta.dirname, "..", "src");

class FakeWatcher extends EventEmitter {
  close(): void {}
}

interface ActivityHarness {
  tracker: RecentJsonlActivity;
  emit: (filename: string | null) => void;
  fail: () => void;
  advance: (milliseconds: number) => void;
  silentWrite: (filename: string) => void;
  statCalls: () => string[];
}

function activityHarness(maxRecentFiles = 2): ActivityHarness {
  let now = 1_000;
  let listener: ((eventType: string, filename: string | Buffer | null) => void) | null = null;
  const watcher = new FakeWatcher();
  const mtimes = new Map<string, number>();
  const statCallNames: string[] = [];
  const tracker = new RecentJsonlActivity({
    maxRecentFiles,
    watchRetryMs: 120_000,
    now: () => now,
    watchFactory: (_dir, nextListener) => {
      listener = nextListener;
      return watcher;
    },
    statFactory: async (path) => {
      const name = basename(path);
      statCallNames.push(name);
      const mtimeMs = mtimes.get(name);
      if (mtimeMs === undefined) throw new Error("missing file");
      return { mtimeMs };
    },
  });
  tracker.watchDirectory("/sessions");

  return {
    tracker,
    emit: (filename) => {
      assert.ok(listener);
      if (filename) mtimes.set(filename, now);
      listener("change", filename);
    },
    fail: () => watcher.emit("error", new Error("watch failed")),
    advance: (milliseconds) => {
      now += milliseconds;
    },
    silentWrite: (filename) => {
      mtimes.set(filename, now);
    },
    statCalls: () => [...statCallNames],
  };
}

function assertUnknown(signal: JsonlActivitySignal): asserts signal is Extract<
  JsonlActivitySignal,
  { kind: "unknown" }
> {
  assert.equal(signal.kind, "unknown");
}

test("restart with no events is stable for health and never drains PM", async () => {
  const first = activityHarness();
  const signalA = await first.tracker.latestActivity("/sessions");
  first.advance(60_000);
  const signalB = await first.tracker.latestActivity("/sessions");

  assertUnknown(signalA);
  assertUnknown(signalB);
  assert.equal(signalA.token, signalB.token);
  assert.deepEqual(decidePMDrain(signalB, 61_000, 15_000, true), {
    action: "rearm",
    reason: "activity-unknown",
    ageMs: null,
  });

  const afterRestart = activityHarness();
  const restartedSignal = await afterRestart.tracker.latestActivity("/sessions");
  assertUnknown(restartedSignal);
  assert.notEqual(restartedSignal.token, signalA.token);
});

test("watch failure produces one stable unknown epoch for both consumers", async () => {
  const harness = activityHarness();
  harness.fail();
  const signalA = await harness.tracker.latestActivity("/sessions");
  harness.advance(60_000);
  const signalB = await harness.tracker.latestActivity("/sessions");

  assertUnknown(signalA);
  assertUnknown(signalB);
  assert.equal(signalA.reason, "watch-failed");
  assert.equal(signalA.token, signalB.token);
  assert.equal(decidePMDrain(signalB, 61_000, 15_000, true).action, "rearm");
});

test("watch failure retries after backoff and resumes observed activity", async () => {
  let now = 1_000;
  let attempts = 0;
  let listener: ((eventType: string, filename: string | Buffer | null) => void) | null =
    null;
  const failedWatcher = new FakeWatcher();
  const recoveredWatcher = new FakeWatcher();
  const tracker = new RecentJsonlActivity({
    now: () => now,
    watchRetryMs: 30_000,
    watchFactory: (_dir, nextListener) => {
      attempts += 1;
      listener = nextListener;
      return attempts === 1 ? failedWatcher : recoveredWatcher;
    },
    statFactory: async () => ({ mtimeMs: now }),
  });
  tracker.watchDirectory("/sessions");
  failedWatcher.emit("error", new Error("watch failed"));

  const failed = await tracker.latestActivity("/sessions");
  assertUnknown(failed);
  assert.equal(failed.reason, "watch-failed");
  assert.equal(attempts, 1);

  now += 29_999;
  const beforeBackoff = await tracker.latestActivity("/sessions");
  assertUnknown(beforeBackoff);
  assert.equal(beforeBackoff.token, failed.token);
  assert.equal(attempts, 1);

  now += 1;
  const probationary = await tracker.latestActivity("/sessions");
  assertUnknown(probationary);
  assert.equal(probationary.reason, "watch-failed");
  assert.equal(attempts, 2);
  assert.equal(probationary.token, failed.token);

  assert.ok(listener);
  listener("change", "recovered.jsonl");
  const observed = await tracker.latestActivity("/sessions");
  assert.equal(observed.kind, "observed");
  assert.equal(observed.observedAtMs, now);
});

test("permanent watch failure keeps one stable token across retry ticks", async () => {
  let now = 1_000;
  let attempts = 0;
  const tracker = new RecentJsonlActivity({
    now: () => now,
    watchRetryMs: 30_000,
    watchFactory: () => {
      attempts += 1;
      throw new Error("watch unavailable");
    },
  });

  const first = await tracker.latestActivity("/sessions");
  assertUnknown(first);
  assert.equal(first.reason, "watch-failed");

  for (let tick = 1; tick <= 10; tick += 1) {
    now += 30_000;
    const signal = await tracker.latestActivity("/sessions");
    assertUnknown(signal);
    assert.equal(signal.reason, "watch-failed");
    assert.equal(signal.token, first.token);
  }
  assert.equal(attempts, 11);
});

test("real writes advance health tokens and allow observed-idle PM drains", async () => {
  const harness = activityHarness();
  harness.emit("recent-a.jsonl");
  const first = await harness.tracker.latestActivity("/sessions");
  assert.equal(first.kind, "observed");

  harness.advance(1_000);
  harness.emit("recent-b.jsonl");
  const second = await harness.tracker.latestActivity("/sessions");
  assert.equal(second.kind, "observed");
  assert.notEqual(first.token, second.token);

  assert.equal(decidePMDrain(second, 2_001, 15_000, false).action, "rearm");
  assert.equal(decidePMDrain(second, 20_000, 15_000, false).action, "drain");
});

test("bounded async stat catches a missed event on a recent JSONL", async () => {
  const harness = activityHarness();
  harness.emit("recent-a.jsonl");
  const first = await harness.tracker.latestActivity("/sessions");
  assert.equal(first.kind, "observed");

  harness.advance(1_000);
  harness.silentWrite("recent-a.jsonl");
  const second = await harness.tracker.latestActivity("/sessions");
  assert.equal(second.kind, "observed");
  assert.notEqual(second.token, first.token);
  assert.equal(second.observedAtMs, 2_000);
});

test("a stalled recent-file stat cannot wedge the activity check", async () => {
  let listener: ((eventType: string, filename: string | Buffer | null) => void) | null = null;
  const watcher = new FakeWatcher();
  const tracker = new RecentJsonlActivity({
    statTimeoutMs: 5,
    watchFactory: (_dir, nextListener) => {
      listener = nextListener;
      return watcher;
    },
    statFactory: () => new Promise(() => {}),
  });
  tracker.watchDirectory("/sessions");
  assert.ok(listener);
  listener("change", "recent-a.jsonl");

  const signal = await tracker.latestActivity("/sessions");
  assert.equal(signal.kind, "observed");
});

test("recent-name eviction does not lose the latest observed activity", async () => {
  const harness = activityHarness(2);
  harness.emit("recent-a.jsonl");
  harness.advance(1);
  harness.emit("recent-b.jsonl");
  harness.advance(1);
  harness.emit("recent-c.jsonl");

  const snapshot = await harness.tracker.snapshot("/sessions");
  assert.deepEqual(snapshot?.recentFiles, ["recent-b.jsonl", "recent-c.jsonl"]);
  assert.equal(snapshot?.signal.kind, "observed");
  assert.equal(snapshot?.signal.observedAtMs, 1_002);
  assert.deepEqual(harness.statCalls(), ["recent-b.jsonl", "recent-c.jsonl"]);
});

test("health and PM relay do not scan historical JSONLs synchronously", async () => {
  const health = await readFile(join(sourceRoot, "health.ts"), "utf8");
  const relay = await readFile(join(sourceRoot, "relay.ts"), "utf8");

  assert.doesNotMatch(health, /\breaddirSync\b|\bstatSync\b/);
  assert.doesNotMatch(relay, /readdirSync\(PM_JSONL_DIR\)/);
  assert.doesNotMatch(relay, /statSync\(path\.join\(PM_JSONL_DIR/);
});
