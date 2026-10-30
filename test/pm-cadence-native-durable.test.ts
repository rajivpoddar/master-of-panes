import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { MoPDatabase } from "../src/db.js";
import { PMCadenceScheduler } from "../src/pmCadence.js";
import { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";

// Real scheduler + native relay + on-disk synthetic SQLite. Only tmux is stubbed.
test("native cadence preserves one durable occurrence through unavailable, ACK loss, crash, restart and pause", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-native-cadence-durable-"));
  const config = { ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") };
  const RealDate = Date;
  let now = new RealDate(2026, 9, 30, 12, 12).getTime();
  class Clock extends RealDate {
    constructor(...args: unknown[]) { super(args.length ? args[0] as number : now); }
    static now() { return now; }
  }
  globalThis.Date = Clock as DateConstructor;
  let db = new MoPDatabase(config);
  let ready = false;
  let loseAck = false;
  let buffer = "";
  const pastes: string[] = [];
  const enters: string[] = [];
  const prefix = "pm_cadence_rtb_split_update";
  const at = (h: number, m: number) => { now = new RealDate(2026, 9, 30, h, m).getTime(); };
  const createRelay = () => {
    const relay = new TmuxRelay(config, {
      pmRuntime: "claude",
      runShell: async command => {
        if (command.includes("pane_current_command")) return { stdout: ready ? "claude\n" : "zsh\n", stderr: "" };
        const load = /^tmux load-buffer -b \S+ '([^']+)'$/.exec(command);
        if (load) buffer = readFileSync(load[1], "utf8");
        if (command.startsWith("tmux paste-buffer")) pastes.push(buffer);
        if (command.endsWith(" Enter")) {
          enters.push(buffer); // The native composer accepted Enter, but its ACK can be lost.
          if (loseAck) { loseAck = false; throw new Error("accepted Enter; tmux ACK lost"); }
        }
        assert.equal(command.endsWith(" C-q"), false);
        return { stdout: "", stderr: "" };
      },
    });
    relay.setDatabase(db);
    return relay;
  };
  let relay = createRelay();
  let scheduler = new PMCadenceScheduler(db, relay);
  const prompt = scheduler.getStatus().tasks.find(t => t.task === "rtb-split-update")!.command;
  const count = (items: string[]) => items.filter(text => text === prompt).length;
  const restart = () => {
    db.close();
    db = new MoPDatabase(config);
    relay = createRelay();
    scheduler = new PMCadenceScheduler(db, relay);
  };
  try {
    for (const task of scheduler.getStatus().tasks) {
      if (task.task !== "rtb-split-update") scheduler.setPaused(true, task.task);
    }
    await scheduler.tick("boot");
    at(12, 13);
    await scheduler.tick("scheduled");
    const occurrence = db.peekPendingPMEvents()[0];
    assert.ok(occurrence);
    assert.equal(occurrence.event_type, "cadence-rtb-split-update-2026-10-30:12:13");
    assert.equal(count(pastes), 0);
    at(12, 14);
    await scheduler.tick("scheduled");
    const retained = db.peekPendingPMEvents()[0];
    ready = true;
    at(12, 15);
    await Promise.all([scheduler.tick("scheduled"), relay.drainPMQueue()]);
    assert.equal(count(pastes), 1);
    assert.equal(count(enters), 1);
    assert.deepEqual(retained, occurrence, "retry must preserve the original occurrence identity");
    assert.equal(db.getPMQueueDeliveryState(0, occurrence.event_type, prompt, occurrence.enqueued_at), "delivered");
    assert.equal(db.getConfig(`${prefix}_pending_verify`), null, "durable success must not arm a second raw transcript-based submit path");
    assert.equal(db.getPendingPMEventCount(), 0);

    // Ordinary inbound stays immediate, including while this cadence task is paused.
    scheduler.setPaused(true, "rtb-split-update");
    const inbound = await relay.submitToPM("ordinary inbound retains its native priority");
    assert.equal(inbound.ok, true);
    assert.equal(db.getPendingPMEventCount(), 0);
    scheduler.setPaused(false, "rtb-split-update");

    at(12, 43);
    loseAck = true;
    await scheduler.tick("scheduled");
    const ambiguous = db.peekPendingPMEvents()[0];
    assert.ok(ambiguous);
    assert.equal(count(enters), 2);
    assert.equal(db.getPMQueueDeliveryState(0, ambiguous.event_type, prompt, ambiguous.enqueued_at), "ambiguous");
    restart();
    at(12, 44);
    await scheduler.tick("boot");
    await relay.drainPMQueue();
    await scheduler.tick("scheduled");
    assert.equal(count(pastes), 2);
    assert.equal(count(enters), 2, "an accepted submit with lost ACK must never automatically replay after restart");
    assert.deepEqual(db.peekPendingPMEvents()[0], ambiguous);

    // Crash after journalled success, before last_due_key can persist.
    at(13, 13);
    const setConfig = db.setConfig.bind(db);
    db.setConfig = (key, value) => {
      if (key === `${prefix}_last_due_key`) throw new Error("crash before due key persisted");
      setConfig(key, value);
    };
    await assert.rejects(scheduler.tick("scheduled"), /crash before due key persisted/);
    assert.equal(count(enters), 3);
    restart();
    at(13, 14);
    await scheduler.tick("boot");
    await relay.drainPMQueue();
    assert.equal(count(enters), 3, "durable success, not last_due_key, is the replay authority");

    at(13, 43);
    await scheduler.tick("scheduled");
    assert.equal(count(enters), 4, "a healthy next bucket is not held by an ambiguous older occurrence");
    scheduler.setPaused(true, "rtb-split-update");
    at(14, 13);
    await scheduler.tick("scheduled");
    assert.equal(count(enters), 4);
    scheduler.setPaused(false, "rtb-split-update");
    at(14, 43);
    await scheduler.tick("scheduled");
    assert.equal(count(enters), 5);
    scheduler.setPaused(true);
    at(15, 13);
    await scheduler.tick("scheduled");
    assert.equal(count(enters), 5);
    scheduler.setPaused(false);
    at(15, 43);
    await scheduler.tick("scheduled");
    assert.equal(count(enters), 6);
    assert.equal(count(pastes), 6);
    assert.deepEqual(db.peekPendingPMEvents(), [ambiguous], "ambiguous original attempt remains durable for operator reconciliation");
  } finally {
    globalThis.Date = RealDate;
    db.close();
    // Synthetic DB retained with the OS temporary artifacts; no user files touched.
  }
});
