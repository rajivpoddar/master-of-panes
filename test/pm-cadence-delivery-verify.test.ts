import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync, appendFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { PMCadenceScheduler, verifyPMCadenceDelivery } from "../src/pmCadence.js";

// Rajiv 2026-10-16, DM D0AMF0XE6TS thread 1792121896.574029: "why are these
// getting dropped?" The 08:13 IST hourly inject reported injected=true but never
// reached the PM transcript; the due_key was marked done and never re-sent.

class FakeMopDb {
  readonly config = new Map<string, string>();
  readonly events: string[] = [];
  getConfig(key: string): string | null {
    return this.config.get(key) || null;
  }
  setConfig(key: string, value: string): void {
    this.config.set(key, value);
  }
  logEvent(_slot: number, type: string): void {
    this.events.push(type);
  }
  hasPMQueueDelivery(): boolean {
    return false;
  }
}

class FakeRelay {
  submissions: Array<{ message: string; eventType?: string }> = [];
  async submitToPM(message: string, eventType?: string) {
    this.submissions.push({ message, eventType });
    return { ok: true } as never;
  }
}

const HOURLY = "MoP: run Skill(hourly-heartbeat) now with a background agent.";

function record(ts: Date, content: string): string {
  return JSON.stringify({ type: "user", timestamp: ts.toISOString(), message: { role: "user", content } }) + "\n";
}

function withTranscript(fn: (path: string) => Promise<void>): Promise<void> {
  const dir = mkdtempSync(join(tmpdir(), "mop-cadence-verify-"));
  const path = join(dir, "pm.jsonl");
  return fn(path).finally(() => rmSync(dir, { recursive: true, force: true }));
}

const later = (ms: number) => new Date(Date.now() + ms);

test("inject reported ok, missing from transcript: re-sent exactly once for that due_key", () =>
  withTranscript(async (path) => {
    const db = new FakeMopDb();
    const relay = new FakeRelay();
    const cadence = new PMCadenceScheduler(db as never, relay as never);
    await cadence.runManual("hourly-heartbeat");
    assert.equal(relay.submissions.length, 1);

    // An old hourly from the previous hour is in the transcript; this one is not.
    writeFileSync(path, record(new Date(Date.now() - 3600_000), HOURLY) + record(new Date(), "unrelated slack event"));

    await verifyPMCadenceDelivery(db as never, relay, path, later(10_000));
    assert.equal(relay.submissions.length, 2, "missing prompt is re-submitted");
    assert.equal(relay.submissions[1].message, HOURLY);
    assert.match(relay.submissions[1].eventType ?? "", /^cadence-hourly-heartbeat-.*:redeliver$/);
    assert.ok(db.events.includes("pm_cadence_redelivered"));

    // Still missing at the following Stop: never sent a third time.
    await verifyPMCadenceDelivery(db as never, relay, path, later(30_000));
    await verifyPMCadenceDelivery(db as never, relay, path, later(60_000));
    assert.equal(relay.submissions.length, 2, "re-sent at most once per due_key");
    assert.ok(db.events.includes("pm_cadence_delivery_unverified"));
  }));

test("inject that landed in the transcript is verified and not re-sent", () =>
  withTranscript(async (path) => {
    const db = new FakeMopDb();
    const relay = new FakeRelay();
    const cadence = new PMCadenceScheduler(db as never, relay as never);
    await cadence.runManual("hourly-heartbeat");
    writeFileSync(path, record(new Date(), HOURLY));

    await verifyPMCadenceDelivery(db as never, relay, path, later(10_000));
    await verifyPMCadenceDelivery(db as never, relay, path, later(20_000));
    assert.equal(relay.submissions.length, 1);
    assert.ok(db.events.includes("pm_cadence_delivery_verified"));
  }));

test("a Stop inside the grace window (turn already in flight) defers the check", () =>
  withTranscript(async (path) => {
    const db = new FakeMopDb();
    const relay = new FakeRelay();
    const cadence = new PMCadenceScheduler(db as never, relay as never);
    await cadence.runManual("heartbeat");
    writeFileSync(path, record(new Date(), "something else"));

    await verifyPMCadenceDelivery(db as never, relay, path, new Date());
    assert.equal(relay.submissions.length, 1, "no judgement inside the grace window");

    appendFileSync(path, JSON.stringify({ type: "queue-operation", operation: "enqueue", timestamp: new Date().toISOString(), content: "MoP: run Skill(heartbeat-tasks) now with a background agent." }) + "\n");
    await verifyPMCadenceDelivery(db as never, relay, path, later(10_000));
    assert.equal(relay.submissions.length, 1, "a queued (enqueued) prompt counts as landed");
  }));

test("missing transcript_path leaves the check pending without re-sending", async () => {
  const db = new FakeMopDb();
  const relay = new FakeRelay();
  const cadence = new PMCadenceScheduler(db as never, relay as never);
  await cadence.runManual("morning-brief");
  await verifyPMCadenceDelivery(db as never, relay, undefined, later(10_000));
  assert.equal(relay.submissions.length, 1);
});
