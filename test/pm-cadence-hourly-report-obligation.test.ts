import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { MoPDatabase } from "../src/db.js";
import {
  classifyWriterError,
  hourlyReportUpsertArgs,
  readHourlyReportObligation,
  upsertHourlyReportObligation,
} from "../src/hourlyReportObligation.js";
import { PM_OPS_HOURLY_REPORT_DEPS, PMCadenceScheduler, type HourlyReportDeps } from "../src/pmCadence.js";
import { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";

// Rajiv D0AMF0XE6TS thread 1793783982.877709, ts 1793784122.949659:
// "hourly report should never be missed. can MoP add an obligation for it automatically?"
// CTO REVISE C0ALZJHGE49 thread 1793789393.531209, ts 1793790035.862469.

// Every test in this file uses an ISOLATED pm-ops ledger; the live one is never touched.
const LIVE_PM_OPS = "/Users/rajiv/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db";
const LEGACY = fileURLToPath(new URL("../scripts/pm/shared-assets/claude/scripts/pm-ops-legacy.py", import.meta.url));
let ledgerSeq = 0;
function isolatedLedger(): string {
  const dir = mkdtempSync(join(tmpdir(), `mop-hourly-ledger-${ledgerSeq++}-`));
  const db = join(dir, "pm-ops.db");
  process.env.PM_OPS_DB = db;
  process.env.MOP_PM_OPS_DB = db;
  process.env.MOP_PM_OPS_SCRIPT = LEGACY;
  assert.notEqual(db, LIVE_PM_OPS);
  return db;
}
isolatedLedger();

function ledgerRows(db: string): Array<{ id: number; target_id: string; status: string; resolved_reason: string | null }> {
  const out = execFileSync("python3", ["-c",
    "import sqlite3,sys,json;c=sqlite3.connect(sys.argv[1]);c.row_factory=sqlite3.Row;" +
    "print(json.dumps([dict(r) for r in c.execute(\"select id,target_id,status,resolved_reason from obligations where kind='hourly_report_due' order by id\")]))",
    db], { encoding: "utf8" });
  return JSON.parse(out);
}

function resolveRow(id: number, reason: string): void {
  execFileSync("python3", [LEGACY, "obligation-resolve", "--kind", "hourly_report_due", "--id", String(id), "--reason", reason], { env: process.env });
}

class FakeMopDb {
  readonly config = new Map<string, string>();
  events: Array<{ type: string; details: unknown }> = [];
  getConfig(key: string): string | null {
    return this.config.get(key) ?? null;
  }
  setConfig(key: string, value: string): void {
    this.config.set(key, value);
  }
  logEvent(_slot: number, type: string, _h: unknown, _t: unknown, details?: unknown): void {
    this.events.push({ type, details });
  }
  hasPMQueueDelivery(): boolean {
    return false;
  }
}

class FakeRelay {
  injections: Array<{ message: string; eventType?: string }> = [];
  onSubmit: ((message: string) => Promise<void> | void) | null = null;
  async submitToPM(message: string, eventType?: string): Promise<{ ok: boolean }> {
    this.injections.push({ message, eventType });
    if (this.onSubmit) await this.onSubmit(message);
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

const at = (h: number, m: number) => new Date(2026, 10, 4, h, m, 0);
const BUCKET = "2026-11-04:14";
const isHourly = (m: string) => m.startsWith("MoP: run Skill(hourly-heartbeat)");
const hourlyInjects = (relay: FakeRelay) => relay.injections.filter((i) => isHourly(i.message));

function countingDeps(base: HourlyReportDeps = PM_OPS_HOURLY_REPORT_DEPS) {
  const calls = { upserts: 0 };
  const deps: HourlyReportDeps = {
    upsert: async (bucket, intentAt) => { calls.upserts++; return base.upsert(bucket, intentAt); },
    readState: (bucket) => base.readState(bucket),
  };
  return { deps, calls };
}

async function bootAndFire(db: FakeMopDb, relay: FakeRelay, deps: HourlyReportDeps): Promise<PMCadenceScheduler> {
  const cadence = new PMCadenceScheduler(db as never, relay as never, deps);
  await withClock(at(14, 5), () => cadence.tick("boot"));
  await withClock(at(14, 13), () => cadence.tick("scheduled"));
  return cadence;
}

test("debt exists in the ledger BEFORE the hourly prompt is submitted; one row, idempotent per bucket", async () => {
  const ledger = isolatedLedger();
  const db = new FakeMopDb();
  const relay = new FakeRelay();
  const seenAtSubmit: string[] = [];
  relay.onSubmit = (m) => { if (isHourly(m)) seenAtSubmit.push(readHourlyReportObligation(BUCKET, ledger)); };
  const { deps, calls } = countingDeps();
  const cadence = await bootAndFire(db, relay, deps);
  assert.deepEqual(seenAtSubmit, ["open"], "the obligation is open when the prompt is submitted");
  await withClock(at(14, 14), () => cadence.runManual("hourly-heartbeat"));
  await withClock(at(14, 15), () => cadence.tick("scheduled"));
  assert.equal(calls.upserts, 1);
  assert.equal(ledgerRows(ledger).length, 1);
  const rec = cadence.readHourlyRecord()!;
  assert.equal(rec.writer, "created");
  assert.ok(rec.delivered_at);
  assert.deepEqual(cadence.getStatus().hourly_report_debt, rec, "debt state is surfaced in status");
});

test("crash after journalled delivery cannot leave the bucket without debt (real relay + MoP DB)", async () => {
  const ledger = isolatedLedger();
  const directory = mkdtempSync(join(tmpdir(), "mop-hourly-crash-"));
  const config = { ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") };
  const RealDate = Date;
  let now = new RealDate(2026, 10, 4, 14, 5).getTime();
  class Clock extends RealDate {
    constructor(...args: unknown[]) { super(args.length ? args[0] as number : now); }
    static now() { return now; }
  }
  globalThis.Date = Clock as DateConstructor;
  let db = new MoPDatabase(config);
  let buffer = "";
  const enters: string[] = [];
  const ledgerAtEnter: string[] = [];
  const createRelay = () => {
    const relay = new TmuxRelay(config, {
      pmRuntime: "claude",
      runShell: async (command) => {
        if (command.includes("pane_current_command")) return { stdout: "claude\n", stderr: "" };
        const load = /^tmux load-buffer -b \S+ '([^']+)'$/.exec(command);
        if (load) buffer = readFileSync(load[1]!, "utf8");
        if (command.endsWith(" Enter")) {
          enters.push(buffer);
          ledgerAtEnter.push(readHourlyReportObligation(BUCKET, ledger));
        }
        return { stdout: "", stderr: "" };
      },
    });
    relay.setDatabase(db);
    return relay;
  };
  const { deps, calls } = countingDeps();
  let scheduler = new PMCadenceScheduler(db, createRelay(), deps);
  try {
    for (const task of scheduler.getStatus().tasks) {
      if (task.task !== "hourly-heartbeat") scheduler.setPaused(true, task.task);
    }
    await scheduler.tick("boot");
    now = new RealDate(2026, 10, 4, 14, 13).getTime();
    // Crash after the journalled delivery, before last_due_key persists.
    const setConfig = db.setConfig.bind(db);
    db.setConfig = (key, value) => {
      if (key === "pm_cadence_hourly_heartbeat_last_due_key") throw new Error("crash after delivery");
      setConfig(key, value);
    };
    await assert.rejects(scheduler.tick("scheduled"), /crash after delivery/);
    assert.equal(enters.filter(isHourly).length, 1);
    assert.deepEqual(ledgerAtEnter, ["open"], "debt was created before the paste");
    db.close();
    db = new MoPDatabase(config);
    scheduler = new PMCadenceScheduler(db, createRelay(), deps);
    for (const task of scheduler.getStatus().tasks) {
      if (task.task !== "hourly-heartbeat") scheduler.setPaused(true, task.task);
    }
    now = new RealDate(2026, 10, 4, 14, 14).getTime();
    await scheduler.tick("boot");
    await scheduler.tick("scheduled");
    assert.equal(enters.filter(isHourly).length, 1, "no second command");
    assert.equal(calls.upserts, 1, "no second write");
    assert.deepEqual(ledgerRows(ledger).map((r) => r.status), ["open"]);
  } finally {
    globalThis.Date = RealDate;
    db.close();
  }
});

test("writer lost ACK (timeout/kill) keeps unknown custody: surfaced, never blindly re-written", async () => {
  isolatedLedger();
  assert.equal(classifyWriterError({ killed: true, signal: "SIGTERM" }), "unknown");
  assert.equal(classifyWriterError({ code: 2 }), "unknown", "exit 2 can be an accepted write (row id 2)");
  assert.equal(classifyWriterError({ code: 1 }), "failed");
  assert.equal(classifyWriterError({ code: "ENOENT" }), "failed");
  assert.equal(await upsertHourlyReportObligation(BUCKET, "2026-11-04T08:43:00Z", false, async () => {
    throw Object.assign(new Error("timed out"), { killed: true, signal: "SIGTERM" });
  }), "unknown");

  const db = new FakeMopDb();
  const relay = new FakeRelay();
  let upserts = 0;
  const deps: HourlyReportDeps = { upsert: async () => { upserts++; return "unknown"; }, readState: () => "absent" };
  const cadence = await bootAndFire(db, relay, deps);
  for (const m of [14, 20, 40]) await withClock(at(14, m), () => cadence.tick("scheduled"));
  assert.equal(upserts, 1);
  assert.equal(cadence.readHourlyRecord()!.writer, "unknown");
  assert.ok(db.events.some((e) => e.type === "pm_cadence_hourly_report_obligation_failed"));
  assert.equal(hourlyInjects(relay).length, 1, "the prompt itself still goes out (fail open)");
});

test("crash while the writer is unsettled: restart reconciles from the ledger, never re-writes", async () => {
  for (const ledgerState of ["open", "absent"] as const) {
    const db = new FakeMopDb();
    db.setConfig("pm_cadence_hourly_heartbeat_last_due_key", "2026-11-04:13");
    db.setConfig("pm_cadence_hourly_report_obligation", JSON.stringify({
      due_key: BUCKET, intent_at: "2026-11-04T08:43:00.000Z", escalated: false, writer: "pending", writer_attempts: 1,
    }));
    let upserts = 0;
    const relay = new FakeRelay();
    const cadence = new PMCadenceScheduler(db as never, relay as never, {
      upsert: async () => { upserts++; return "created"; },
      readState: () => ledgerState,
    });
    await withClock(at(14, 15), () => cadence.tick("scheduled"));
    assert.equal(upserts, 0, ledgerState);
    assert.equal(cadence.readHourlyRecord()!.writer, ledgerState === "open" ? "created" : "unknown");
  }
});

test("definite writer failure is surfaced and gets ONE verified retry only after the ledger shows no row", async () => {
  const db = new FakeMopDb();
  const relay = new FakeRelay();
  let upserts = 0;
  let ledger: "absent" | "open" = "absent";
  const cadence = await bootAndFire(db, relay, {
    upsert: async () => { upserts++; return "failed"; },
    readState: () => ledger,
  });
  assert.equal(upserts, 1);
  await withClock(at(14, 14), () => cadence.tick("scheduled"));
  await withClock(at(14, 15), () => cadence.tick("scheduled"));
  await withClock(at(14, 16), () => cadence.tick("scheduled"));
  assert.equal(upserts, 2, "exactly one verified retry");
  assert.equal(cadence.readHourlyRecord()!.writer, "failed");
  ledger = "open";
  await withClock(at(14, 17), () => cadence.tick("scheduled"));
  assert.equal(cadence.readHourlyRecord()!.writer, "created");
});

test("report resolved DURING reminder submission: no reopened debt, no second command", async () => {
  const ledger = isolatedLedger();
  const db = new FakeMopDb();
  const relay = new FakeRelay();
  const { deps, calls } = countingDeps();
  const cadence = await bootAndFire(db, relay, deps);
  relay.onSubmit = (m) => {
    if (m.includes("Re-inject")) resolveRow(ledgerRows(ledger)[0]!.id, "report posted during reminder submit");
  };
  await withClock(at(14, 33), () => cadence.tick("scheduled"));
  await withClock(at(14, 34), () => cadence.tick("scheduled"));
  await withClock(at(14, 50), () => cadence.tick("scheduled"));
  assert.equal(hourlyInjects(relay).length, 2, "one original + one consumed reminder, never another");
  assert.equal(calls.upserts, 1, "no post-effect upsert");
  assert.deepEqual(ledgerRows(ledger).map((r) => r.status), ["resolved"], "resolved debt is never recreated");
  assert.equal(cadence.readHourlyRecord()!.escalated, true);
});

test("re-inject happens once at +20m, survives restart, and skips resolved/unknown/past buckets", async () => {
  isolatedLedger();
  const db = new FakeMopDb();
  const relay = new FakeRelay();
  const first = await bootAndFire(db, relay, PM_OPS_HOURLY_REPORT_DEPS);
  await withClock(at(14, 32), () => first.tick("scheduled"));
  assert.equal(hourlyInjects(relay).length, 1);
  await withClock(at(14, 33), () => first.tick("scheduled"));
  const second = new PMCadenceScheduler(db as never, relay as never, PM_OPS_HOURLY_REPORT_DEPS);
  await withClock(at(14, 40), () => second.tick("scheduled"));
  const injects = hourlyInjects(relay);
  assert.equal(injects.length, 2);
  assert.match(injects[1]!.message, /Re-inject: no hourly report posted for 2026-11-04:14 after 20 min\.$/);
  assert.equal(injects[1]!.eventType, "cadence-hourly-heartbeat-2026-11-04:14-reinject");

  for (const state of ["resolved", "unknown", "absent"] as const) {
    const r = new FakeRelay();
    const c = await bootAndFire(new FakeMopDb(), r, { upsert: async () => "created", readState: () => state });
    await withClock(at(14, 40), () => c.tick("scheduled"));
    assert.equal(hourlyInjects(r).length, 1, state);
  }
  const r = new FakeRelay();
  const c = await bootAndFire(new FakeMopDb(), r, { upsert: async () => "created", readState: () => "open" });
  await withClock(at(14, 40), () => c.tick("boot"));
  await withClock(at(15, 5), () => c.tick("scheduled"));
  assert.equal(hourlyInjects(r).length, 1, "boot ticks and past buckets never re-inject");
});

test("queued reminder is revalidated at the paste boundary: expired/completed dropped, unknown retained, unrelated delivered", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-hourly-queued-"));
  const config = { ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") };
  const RealDate = Date;
  let now = new RealDate(2026, 10, 4, 14, 5).getTime();
  class Clock extends RealDate {
    constructor(...args: unknown[]) { super(args.length ? args[0] as number : now); }
    static now() { return now; }
  }
  globalThis.Date = Clock as DateConstructor;
  const db = new MoPDatabase(config);
  let ready = true;
  let buffer = "";
  const pastes: string[] = [];
  const relay = new TmuxRelay(config, {
    pmRuntime: "claude",
    runShell: async (command) => {
      if (command.includes("pane_current_command")) return { stdout: ready ? "claude\n" : "zsh\n", stderr: "" };
      const load = /^tmux load-buffer -b \S+ '([^']+)'$/.exec(command);
      if (load) buffer = readFileSync(load[1]!, "utf8");
      if (command.startsWith("tmux paste-buffer")) pastes.push(buffer);
      return { stdout: "", stderr: "" };
    },
  });
  relay.setDatabase(db);
  let ledger: "open" | "resolved" | "unknown" = "open";
  const scheduler = new PMCadenceScheduler(db, relay, { upsert: async () => "created", readState: () => ledger });
  const reminders = () => pastes.filter((p) => p.includes("Re-inject")).length;
  const queued = () => db.peekPendingPMEvents().map((r) => r.event_type);
  try {
    for (const task of scheduler.getStatus().tasks) {
      if (task.task !== "hourly-heartbeat") scheduler.setPaused(true, task.task);
    }
    await scheduler.tick("boot");
    now = new RealDate(2026, 10, 4, 14, 13).getTime();
    await scheduler.tick("scheduled");
    ready = false;
    now = new RealDate(2026, 10, 4, 14, 34).getTime();
    await scheduler.tick("scheduled");
    assert.deepEqual(queued(), ["cadence-hourly-heartbeat-2026-11-04:14-reinject"], "reminder attempt consumed and queued");
    await relay.submitToPM("unrelated queued PM work", "cadence-unrelated-probe");
    ready = true;

    ledger = "unknown";
    await relay.drainPMQueue();
    assert.equal(reminders(), 0);
    assert.ok(queued().includes("cadence-hourly-heartbeat-2026-11-04:14-reinject"), "uncertain ledger retains the row");
    assert.ok(!queued().includes("cadence-unrelated-probe"), "unrelated work still delivers");
    assert.ok(pastes.some((p) => p.includes("unrelated queued PM work")));

    ledger = "resolved";
    await relay.drainPMQueue();
    assert.equal(reminders(), 0, "a completed reminder is never pasted");
    assert.deepEqual(queued(), []);

    // Expired: a reminder still queued when the bucket rolls over is dropped, even if the debt is open.
    ledger = "open";
    db.setConfig("pm_cadence_hourly_heartbeat_last_due_key", "2026-11-04:15");
    db.setConfig("pm_cadence_hourly_report_obligation", JSON.stringify({
      due_key: "2026-11-04:15", intent_at: new RealDate(2026, 10, 4, 15, 13).toISOString(),
      delivered_at: new RealDate(2026, 10, 4, 15, 13).toISOString(), escalated: false,
      writer: "created", writer_attempts: 1,
    }));
    now = new RealDate(2026, 10, 4, 15, 34).getTime();
    ready = false;
    await scheduler.tick("scheduled");
    assert.deepEqual(queued(), ["cadence-hourly-heartbeat-2026-11-04:15-reinject"]);
    now = new RealDate(2026, 10, 4, 16, 2).getTime();
    ready = true;
    await relay.drainPMQueue();
    assert.equal(reminders(), 0, "an expired reminder is never pasted");
    assert.deepEqual(queued(), []);
  } finally {
    globalThis.Date = RealDate;
    db.close();
  }
});

test("production server wires the live pm-ops deps; the scheduler default is inert", async () => {
  const server = readFileSync(new URL("../src/server.ts", import.meta.url), "utf8");
  assert.match(server, /new PMCadenceScheduler\(db, relay, PM_OPS_HOURLY_REPORT_DEPS\)/);
  const relay = new FakeRelay();
  const cadence = new PMCadenceScheduler(new FakeMopDb() as never, relay as never);
  await withClock(at(14, 5), () => cadence.tick("boot"));
  await withClock(at(14, 13), () => cadence.tick("scheduled"));
  await withClock(at(14, 40), () => cadence.tick("scheduled"));
  assert.equal(hourlyInjects(relay).length, 1);
});

test("no gating: the obligation module never touches PM input/tool admission or UserPromptSubmit", () => {
  const src = readFileSync(new URL("../src/hourlyReportObligation.ts", import.meta.url), "utf8");
  const code = src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
  assert.doesNotMatch(code, /UserPromptSubmit|permissionDecision|"deny"|setPMBusy|isAdmissionFenced|maintenance|hold/i);
});

test("the real pm-ops writer accepts the upsert args; repeat upsert keeps one open row", () => {
  const ledger = isolatedLedger();
  for (let i = 0; i < 3; i++) {
    execFileSync("python3", hourlyReportUpsertArgs(BUCKET, "2026-11-04T08:43:00Z", false), { env: process.env });
  }
  assert.equal(readHourlyReportObligation(BUCKET, ledger), "open");
  assert.equal(readHourlyReportObligation("2026-11-04:15", ledger), "absent");
  const rows = ledgerRows(ledger);
  assert.equal(rows.length, 1);
  const raw = execFileSync("python3", ["-c",
    "import sqlite3,sys,json;c=sqlite3.connect(sys.argv[1]);print(json.dumps(c.execute(\"select owner,severity,horizon,target_type,required_action,next_review_at from obligations\").fetchone()))",
    ledger], { encoding: "utf8" });
  const [owner, severity, horizon, targetType, action, nextReview] = JSON.parse(raw) as string[];
  assert.deepEqual([owner, severity, horizon, targetType], ["pm", "high", "hourly", "hourly_bucket"]);
  assert.match(action!, /^Invoke Skill\(hourly-heartbeat\); post report for 2026-11-04:14\./);
  assert.equal(nextReview, "2026-11-04T08:53:00Z");
});
