import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { MoPDatabase } from "../src/db.js";
import { PMCadenceScheduler } from "../src/pmCadence.js";
import { decidePMSubmitKey, TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";

test("native Claude always uses Enter while OMP remains busy-aware", () => {
  assert.equal(decidePMSubmitKey(false, "claude"), "Enter");
  assert.equal(decidePMSubmitKey(true, "claude"), "Enter");
  assert.equal(decidePMSubmitKey(null, "claude"), "Enter");
  assert.equal(decidePMSubmitKey(undefined, "claude"), "Enter");
  assert.equal(decidePMSubmitKey(false, "omp"), "Enter");
  assert.equal(decidePMSubmitKey(true, "omp"), "C-q");
  assert.equal(decidePMSubmitKey(null, "omp"), "C-q");
  assert.equal(decidePMSubmitKey(undefined, "omp"), "C-q");
});

test("native Claude submits immediately without a PM pending-event row", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-pm-native-claude-"));
  try {
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
    const commands: string[] = [];
    const relay = new TmuxRelay(DEFAULT_CONFIG, {
      pmRuntime: "claude",
      runShell: async (command) => {
        commands.push(command);
        return { stdout: "", stderr: "" };
      },
    });
    relay.setDatabase(db);
    (relay as unknown as { pmBusy: boolean | null }).pmBusy = true;

    const result = await relay.submitToPM("native Claude immediate message");

    assert.equal(result.ok, true);
    assert.equal(result.submitKey, "Enter");
    assert.equal(commands.filter((command) => command.endsWith(" Enter")).length, 1);
    assert.equal(commands.some((command) => command.endsWith(" C-q")), false);
    assert.equal(db.getPendingPMEventCount(), 0);
    assert.equal(db.getEvents(0, 20, "pm_queue_enqueued").length, 0);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
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
      submitToPM: async (message: string, eventType?: string) => {
        queued.push({ message, eventType });
        db.enqueuePendingPMEvent(0, eventType ?? "freeform-test", message);
        return { ok: false, submitKey: "C-q" as const, ambiguous: false };
      },
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

test("slot-0 freeform/cadence/ops occurrences are not dropped by state coalescing", () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-pm-occurrence-preservation-"));
  try {
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
    db.enqueuePendingPMEvent(0, "cadence-heartbeat-2026-08-20:4", "heartbeat");
    db.enqueuePendingPMEvent(0, "cadence-morning-brief-2026-08-20", "morning brief");
    db.enqueuePendingPMEvent(0, "freeform-ops-audit-1", "ops audit");

    const drained = db.drainPendingPMEvents();
    assert.equal(drained.length, 3);
    assert.deepEqual(new Set(drained.map((row) => row.event_type)), new Set([
      "cadence-heartbeat-2026-08-20:4",
      "cadence-morning-brief-2026-08-20",
      "freeform-ops-audit-1",
    ]));
    assert.equal(db.getPendingPMEventCount(), 3);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("busy/unknown injects and cadence use immediate C-q without a later Stop", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-pm-immediate-followup-"));
  try {
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
    const commands: string[] = [];
    const relay = new TmuxRelay(DEFAULT_CONFIG, {
      pmRuntime: "omp",
      runShell: async (command) => {
        commands.push(command);
        return { stdout: "", stderr: "" };
      },
    });
    relay.setDatabase(db);
    (relay as unknown as { pmBusy: boolean | null }).pmBusy = true;
    assert.equal(relay.injectToPM("busy alert", "freeform-busy"), true);
    (relay as unknown as { pmBusy: boolean | null }).pmBusy = null;
    assert.equal(relay.injectToPM("unknown alert", "freeform-unknown"), true);

    const cadence = new PMCadenceScheduler(db, relay);
    const cadenceResult = await cadence.runManual("heartbeat");
    assert.equal(cadenceResult.injected, true);
    assert.equal(cadenceResult.queued, false);
    assert.equal(db.getConfig("pm_cadence_heartbeat_last_due_key") !== null, true);

    await new Promise((resolve) => setTimeout(resolve, 3_500));
    const submits = commands.filter((command) => command.includes("tmux send-keys"));
    assert.equal(submits.filter((command) => command.endsWith(" C-q")).length, 3);
    assert.equal(submits.some((command) => command.endsWith(" Enter")), false);
    assert.equal(db.getPendingPMEventCount(), 0);
    assert.equal(db.getEvents(0, 20, "pm_queue_delivered").length, 3);
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
      pmRuntime: "omp",
      runShell: async (command) => {
        commands.push(command);
        return { stdout: "", stderr: "" };
      },
    });
    relay.setDatabase(db);
    (relay as unknown as { pmBusy: boolean | null }).pmBusy = null;
    relay.injectToPM("MoP: heartbeat due", "cadence-heartbeat-test");
    assert.equal(db.getPendingPMEventCount(), 1);
    const firstOccurrence = db.peekPendingPMEvents()[0]?.enqueued_at;
    assert.ok(firstOccurrence);

    // injectToPM now enters the shared drain immediately; no later Stop hook
    // is required to produce the follow-up.
    await new Promise((resolve) => setTimeout(resolve, 1_300));
    const delivered = db.getEvents(0, 10, "pm_queue_delivered").length;
    assert.equal(delivered, 1);
    assert.equal(db.getPendingPMEventCount(), 0);
    assert.equal(commands.filter((command) => command.includes("send-keys") && command.includes("C-q")).length, 1);
    assert.equal(db.getEvents(0, 10, "pm_queue_delivered").length, 1);

    // A restart/re-drain after the durable delivery marker must not send the
    // same occurrence again. Recreate the exact row identity to exercise the
    // marker-based idempotency boundary.
    db.enqueuePendingPMEvent(0, "cadence-heartbeat-test", "MoP: heartbeat due", firstOccurrence);
    assert.equal(await relay.drainPMQueue(), 1);
    assert.equal(commands.filter((command) => command.includes("send-keys") && command.includes("C-q")).length, 1);
    assert.equal(db.getPendingPMEventCount(), 0);
    assert.equal(db.getEvents(0, 10, "pm_queue_replay_suppressed").length, 1);

    // A later legitimate occurrence with identical content gets a fresh
    // enqueue identity and must not be suppressed by the old marker.
    db.enqueuePendingPMEvent(0, "cadence-heartbeat-test", "MoP: heartbeat due");
    assert.equal(await relay.drainPMQueue(), 1);
    assert.equal(commands.filter((command) => command.includes("send-keys") && command.includes("C-q")).length, 2);

    const failedDb = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "failed.db") });
    const failedRelay = new TmuxRelay(DEFAULT_CONFIG, {
      pmRuntime: "omp",
      runShell: async (command) => {
        if (command.includes("send-keys") && command.includes("C-q")) throw new Error("synthetic submit failure");
        return { stdout: "", stderr: "" };
      },
    });
    failedRelay.setDatabase(failedDb);
    (failedRelay as unknown as { pmBusy: boolean | null }).pmBusy = null;
    failedRelay.injectToPM("queued failure", "freeform-failure");
    await new Promise((resolve) => setTimeout(resolve, 900));
    assert.equal(failedDb.getPendingPMEventCount(), 1);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("automated PM injects are durable before async submit and retain both idle/direct failures", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-pm-durable-inject-"));
  try {
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
    const relay = new TmuxRelay(DEFAULT_CONFIG, {
      pmRuntime: "omp",
      runShell: async () => { throw new Error("synthetic tmux failure"); },
    });
    relay.setDatabase(db);
    (relay as unknown as { pmBusy: boolean | null }).pmBusy = false;

    assert.equal(relay.injectToPM("idle automated alert", "freeform-idle-failure"), true);
    assert.equal(relay.injectToPMDirect("direct automated alert"), true);
    assert.equal(db.getPendingPMEventCount(), 2);
    await new Promise((resolve) => setTimeout(resolve, 20));
    assert.equal(db.getPendingPMEventCount(), 2);
    assert.equal(db.getEvents(0, 20, "pm_queue_delivery_deferred").length >= 1, true);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("paste-success/submit-failure retry submits the existing prompt once", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-pm-partial-submit-"));
  try {
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
    const commands: string[] = [];
    let submitAttempts = 0;
    const relay = new TmuxRelay(DEFAULT_CONFIG, {
      pmRuntime: "omp",
      runShell: async (command) => {
        commands.push(command);
        if (command.includes("send-keys") && command.includes("C-q") && !command.includes("paste-buffer")) {
          submitAttempts += 1;
          if (submitAttempts === 1) throw new Error("synthetic submit failure after paste");
        }
        return { stdout: "", stderr: "" };
      },
    });
    relay.setDatabase(db);
    (relay as unknown as { pmBusy: boolean | null }).pmBusy = null;

    const message = "partial delivery\nwith one pasted payload";
    assert.equal((await relay.submitToPM(message)).ok, false);
    // Recreate the relay against the same DB: the existing event log, not
    // transient relay memory, must tell the retry to submit only the key.
    const restartedRelay = new TmuxRelay(DEFAULT_CONFIG, {
      pmRuntime: "omp",
      runShell: async (command) => {
        commands.push(command);
        if (command.includes("send-keys") && command.includes("C-q") && !command.includes("paste-buffer")) {
          submitAttempts += 1;
          if (submitAttempts === 1) throw new Error("synthetic submit failure after paste");
        }
        return { stdout: "", stderr: "" };
      },
    });
    restartedRelay.setDatabase(db);
    (restartedRelay as unknown as { pmBusy: boolean | null }).pmBusy = null;
    assert.equal((await restartedRelay.submitToPM(message)).ok, false);
    assert.equal(commands.filter((command) => command.includes("paste-buffer")).length, 1);
    assert.equal(commands.filter((command) => command.includes("send-keys") && command.includes("C-q")).length, 1);
    assert.equal(db.getEvents(0, 20, "pm_queue_delivery_started").length, 1);
    assert.equal(db.getEvents(0, 20, "pm_queue_delivery_deferred").length, 1);
    assert.equal(db.getPendingPMEventCount(), 1);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("ambiguous queue occurrence stays fail-closed beyond the event read horizon", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-pm-ambiguous-horizon-"));
  try {
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
    const commands: string[] = [];
    let submitAttempts = 0;
    const runShell = async (command: string) => {
      commands.push(command);
      if (command.includes("send-keys") && command.includes("C-q") && !command.includes("paste-buffer")) {
        submitAttempts += 1;
        if (submitAttempts === 1) throw new Error("synthetic lost submit acknowledgement");
      }
      return { stdout: "", stderr: "" };
    };
    const relay = new TmuxRelay(DEFAULT_CONFIG, { runShell, pmRuntime: "omp" });
    relay.setDatabase(db);
    (relay as unknown as { pmBusy: boolean | null }).pmBusy = null;
    const message = "ambiguous durable occurrence";
    assert.equal((await relay.submitToPM(message)).ok, false);
    for (let i = 0; i < 600; i += 1) {
      db.logEvent(0, "unrelated-diagnostic", null, null, { i });
    }
    const restartedRelay = new TmuxRelay(DEFAULT_CONFIG, { runShell, pmRuntime: "omp" });
    restartedRelay.setDatabase(db);
    (restartedRelay as unknown as { pmBusy: boolean | null }).pmBusy = null;
    assert.equal((await restartedRelay.submitToPM(message)).ok, false);
    // This single-line occurrence has no paste-buffer phase; the durable
    // started marker still prevents a second submit-key after restart.
    assert.equal(commands.filter((command) => command.includes("paste-buffer")).length, 0);
    assert.equal(commands.filter((command) => command.includes("send-keys") && command.includes("C-q")).length, 1);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("concurrent idle/direct inject drains serialize the whole occurrence delivery", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-pm-drain-serialization-"));
  try {
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
    const commands: string[] = [];
    let firstSubmitStarted!: () => void;
    let releaseFirstSubmit!: () => void;
    const firstStarted = new Promise<void>((resolve) => { firstSubmitStarted = resolve; });
    const release = new Promise<void>((resolve) => { releaseFirstSubmit = resolve; });
    let submitCount = 0;
    const relay = new TmuxRelay(DEFAULT_CONFIG, {
      pmRuntime: "omp",
      runShell: async (command) => {
        commands.push(command);
        if (command.endsWith(" Enter")) {
          submitCount += 1;
          if (submitCount === 1) {
            firstSubmitStarted();
            await release;
          }
        }
        return { stdout: "", stderr: "" };
      },
    });
    relay.setDatabase(db);
    (relay as unknown as { pmBusy: boolean | null }).pmBusy = false;

    assert.equal(relay.injectToPM("concurrent A", "freeform-a"), true);
    assert.equal(relay.injectToPMDirect("concurrent B"), true);
    await firstStarted;
    assert.equal(commands.filter((command) => command.endsWith(" Enter")).length, 1);
    releaseFirstSubmit();
    await new Promise((resolve) => setTimeout(resolve, 1_800));
    assert.equal(db.getEvents(0, 20, "pm_queue_delivered").length, 2);
    assert.equal(db.getPendingPMEventCount(), 0);
    assert.deepEqual(
      commands.filter((command) => command.endsWith(" Enter") || command.endsWith(" C-q"))
        .map((command) => command.endsWith(" Enter") ? "Enter" : "C-q"),
      ["Enter", "C-q"],
    );
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("in-flight delivery only removes the selected occurrence, preserving a same-key replacement", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-pm-occurrence-race-"));
  try {
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
    let releaseSubmit!: () => void;
    const submitRelease = new Promise<void>((resolve) => { releaseSubmit = resolve; });
    let submitStarted!: () => void;
    const started = new Promise<void>((resolve) => { submitStarted = resolve; });
    const relay = new TmuxRelay(DEFAULT_CONFIG, {
      pmRuntime: "omp",
      runShell: async (command) => {
        if (command.endsWith(" Enter")) {
          submitStarted();
          await submitRelease;
        }
        return { stdout: "", stderr: "" };
      },
    });
    relay.setDatabase(db);
    (relay as unknown as { pmBusy: boolean | null }).pmBusy = false;
    db.enqueuePendingPMEvent(0, "freeform-replaced", "old payload", "2026-08-20T00:00:00.001Z");
    const draining = relay.drainPMQueue();
    await started;
    db.enqueuePendingPMEvent(0, "freeform-replaced", "new payload", "2026-08-20T00:00:00.002Z");
    releaseSubmit();
    await draining;
    assert.deepEqual(db.peekPendingPMEvents().map((row) => [row.payload, row.enqueued_at]), [
      ["new payload", "2026-08-20T00:00:00.002Z"],
    ]);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("queued drain demotes idle after Enter so later PM rows use C-q", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-pm-submit-order-"));
  try {
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
    const commands: string[] = [];
    const relay = new TmuxRelay(DEFAULT_CONFIG, {
      pmRuntime: "omp",
      runShell: async (command) => {
        commands.push(command);
        return { stdout: "", stderr: "" };
      },
    });
    relay.setDatabase(db);
    (relay as unknown as { pmBusy: boolean | null }).pmBusy = false;
    db.enqueuePendingPMEvent(0, "freeform-first", "first PM row");
    db.enqueuePendingPMEvent(1, "freeform-second", "second PM row");

    assert.equal(await relay.drainPMQueue(), 2);
    const submitKeys = commands
      .filter((command) => command.includes("tmux send-keys"))
      .map((command) => command.endsWith(" Enter") ? "Enter" : command.endsWith(" C-q") ? "C-q" : "other")
      .filter((key) => key !== "other");
    assert.deepEqual(submitKeys, ["Enter", "C-q"]);
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
  assert.match(slackRoute, /PM submit failed; Slack event was not acknowledged/);
  assert.match(slackRoute, /}, 502\);/);
  assert.doesNotMatch(slackRoute, /tmux\s+(load-buffer|paste-buffer|send-keys)/);

  const relay = readFileSync(new URL("../src/relay.ts", import.meta.url), "utf8");
  const sendStart = relay.indexOf("async sendToSlotAsync");
  const sendEnd = relay.indexOf("\n  /**", sendStart);
  const sendBody = relay.slice(sendStart, sendEnd);
  assert.match(sendBody, /tmux send-keys -t \$\{paneAddr\} Enter/);
  assert.match(sendBody, /submitToPM\(command\)/);
  const numberedBody = sendBody.slice(sendBody.indexOf("const paneAddr"));
  assert.doesNotMatch(numberedBody, /C-q/);

  const drainStart = relay.indexOf("private async runDrainCheck");
  const drainEnd = relay.indexOf("\n  /**", drainStart);
  const drainBody = relay.slice(drainStart, drainEnd);
  assert.doesNotMatch(drainBody, /latestActivity|PM_JSONL|recentJsonlActivity/);

  const opsAudit = readFileSync(new URL("../src/opsAudit.ts", import.meta.url), "utf8");
  assert.match(opsAudit, /relay\.injectToPM\(/);
});
