import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { MoPDatabase } from "../src/db.js";
import { PMCadenceScheduler } from "../src/pmCadence.js";
import { decidePMSubmitKey, TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";

test("PM submit-key contract is idle-only Enter and busy/unknown C-q", () => {
  assert.equal(decidePMSubmitKey(false), "Enter");
  assert.equal(decidePMSubmitKey(true), "C-q");
  assert.equal(decidePMSubmitKey(null), "C-q");
  assert.equal(decidePMSubmitKey(undefined), "C-q");
});

test("queued cadence due key coalesces across scheduler restart and is not marked delivered on enqueue", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-cadence-due-key-"));
  try {
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
    const queued: Array<{ message: string; eventType?: string }> = [];
    const relay = {
      isPMIdleProven: () => false,
      injectToPM: (message: string, eventType?: string) => {
        queued.push({ message, eventType });
        db.enqueuePendingPMEvent(0, eventType ?? "freeform-test", message);
        return true;
      },
      submitToPM: async () => ({ ok: true, submitKey: "Enter" as const }),
    } as unknown as TmuxRelay;

    const first = await new PMCadenceScheduler(db, relay).runManual("heartbeat");
    const second = await new PMCadenceScheduler(db, relay).runManual("heartbeat");

    assert.equal(first.queued, true);
    assert.equal(first.injected, false);
    assert.equal(second.queued, true);
    assert.equal(db.getConfig("pm_cadence_heartbeat_last_due_key"), null);
    assert.equal(db.getPendingPMEventCount(), 1);
    assert.equal(queued.length, 2);
    assert.equal(queued[0]?.eventType, queued[1]?.eventType);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("queued PM delivery uses shared submit key, retains failed rows, and records one durable success", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-pm-queue-delivery-"));
  try {
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
    const commands: string[] = [];
    const relay = new TmuxRelay(DEFAULT_CONFIG, {
      runShell: async (command) => {
        commands.push(command);
        return { stdout: "", stderr: "" };
      },
    });
    relay.setDatabase(db);
    (relay as unknown as { pmBusy: boolean | null }).pmBusy = null;
    relay.injectToPM("MoP: heartbeat due", "cadence-heartbeat-test");
    assert.equal(db.getPendingPMEventCount(), 1);

    const delivered = await relay.drainPMQueue();
    assert.equal(delivered, 1);
    assert.equal(db.getPendingPMEventCount(), 0);
    assert.equal(commands.filter((command) => command.includes("send-keys") && command.includes("C-q")).length, 1);
    assert.equal(db.getEvents(0, 10, "pm_queue_delivered").length, 1);

    // A restart/re-drain after the durable delivery marker must not send the
    // same due key again. The pending row is already gone here; recreate the
    // exact row to exercise the marker-based idempotency boundary.
    db.enqueuePendingPMEvent(0, "cadence-heartbeat-test", "MoP: heartbeat due");
    assert.equal(await relay.drainPMQueue(), 1);
    assert.equal(commands.filter((command) => command.includes("send-keys") && command.includes("C-q")).length, 1);
    assert.equal(db.getPendingPMEventCount(), 0);
    assert.equal(db.getEvents(0, 10, "pm_queue_replay_suppressed").length, 1);

    const failedDb = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "failed.db") });
    const failedRelay = new TmuxRelay(DEFAULT_CONFIG, {
      runShell: async (command) => {
        if (command.includes("send-keys") && command.includes("C-q")) throw new Error("synthetic submit failure");
        return { stdout: "", stderr: "" };
      },
    });
    failedRelay.setDatabase(failedDb);
    (failedRelay as unknown as { pmBusy: boolean | null }).pmBusy = null;
    failedRelay.injectToPM("queued failure", "freeform-failure");
    assert.equal(await failedRelay.drainPMQueue(), 0);
    assert.equal(failedDb.getPendingPMEventCount(), 1);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("Slack route and numbered-slot paths use shared PM/slot submit boundaries", () => {
  const source = readFileSync(new URL("../src/server.ts", import.meta.url), "utf8");
  const slackStart = source.indexOf('app.post("/api/slack-route"');
  const slackEnd = source.indexOf("// ─── Start", slackStart);
  assert.notEqual(slackStart, -1);
  const slackRoute = source.slice(slackStart, slackEnd);
  assert.match(slackRoute, /relay\.submitToPM\(formatted\)/);
  assert.match(slackRoute, /relay\.sendToSlotAsync\(slotNum, formatted, true, false\)/);
  assert.doesNotMatch(slackRoute, /tmux\s+(load-buffer|paste-buffer|send-keys)/);

  const relay = readFileSync(new URL("../src/relay.ts", import.meta.url), "utf8");
  const sendStart = relay.indexOf("async sendToSlotAsync");
  const sendEnd = relay.indexOf("\n  /**", sendStart);
  const sendBody = relay.slice(sendStart, sendEnd);
  assert.match(sendBody, /tmux send-keys -t \$\{paneAddr\} Enter/);
  assert.match(sendBody, /submitToPM\(command\)/);
  const numberedBody = sendBody.slice(sendBody.indexOf("const paneAddr"));
  assert.doesNotMatch(numberedBody, /C-q/);
});
