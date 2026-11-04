import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  hourlyReportUpsertArgs,
  readHourlyReportObligation,
  upsertHourlyReportObligation,
} from "../src/hourlyReportObligation.js";
import { PMCadenceScheduler, type HourlyReportDeps } from "../src/pmCadence.js";

// Rajiv D0AMF0XE6TS thread 1793783982.877709, ts 1793784122.949659:
// "hourly report should never be missed. can MoP add an obligation for it automatically?"

class FakeMopDb {
  readonly config = new Map<string, string>();
  events: string[] = [];
  getConfig(key: string): string | null {
    return this.config.get(key) ?? null;
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
  injections: Array<{ message: string; eventType?: string }> = [];
  async submitToPM(message: string, eventType?: string): Promise<{ ok: boolean }> {
    this.injections.push({ message, eventType });
    return { ok: true };
  }
}

class FakeObligations implements HourlyReportDeps {
  rows = new Map<string, { status: "open" | "resolved"; escalated: boolean; upserts: number }>();
  async upsert(bucket: string, _injectedAt: string, escalated: boolean): Promise<boolean> {
    const row = this.rows.get(bucket);
    if (row && row.status === "open") {
      row.upserts++;
      row.escalated = escalated;
    } else {
      this.rows.set(bucket, { status: "open", escalated, upserts: 1 });
    }
    return true;
  }
  readState(bucket: string): "open" | "resolved" | "unknown" {
    return this.rows.get(bucket)?.status ?? "unknown";
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
const hourlyInjects = (relay: FakeRelay) =>
  relay.injections.filter((i) => i.message.startsWith("MoP: run Skill(hourly-heartbeat)"));

async function bootAndFire(db: FakeMopDb, relay: FakeRelay, obligations: FakeObligations): Promise<PMCadenceScheduler> {
  const cadence = new PMCadenceScheduler(db as never, relay as never, obligations);
  await withClock(at(14, 5), () => cadence.tick("boot"));
  await withClock(at(14, 13), () => cadence.tick("scheduled"));
  return cadence;
}

test("hourly inject opens one hourly_report_due obligation for its bucket", async () => {
  const db = new FakeMopDb();
  const relay = new FakeRelay();
  const obligations = new FakeObligations();
  await bootAndFire(db, relay, obligations);
  assert.equal(hourlyInjects(relay).length, 1);
  assert.deepEqual([...obligations.rows.keys()], ["2026-11-04:14"]);
  assert.equal(obligations.rows.get("2026-11-04:14")!.escalated, false);
  assert.ok(db.events.includes("pm_cadence_hourly_report_obligation"));
});

test("duplicate trigger for the same bucket is idempotent (no second upsert)", async () => {
  const db = new FakeMopDb();
  const relay = new FakeRelay();
  const obligations = new FakeObligations();
  const cadence = await bootAndFire(db, relay, obligations);
  await withClock(at(14, 14), () => cadence.runManual("hourly-heartbeat"));
  await withClock(at(14, 15), () => cadence.tick("scheduled"));
  assert.equal(obligations.rows.size, 1);
  assert.equal(obligations.rows.get("2026-11-04:14")!.upserts, 1);
});

test("still open 20 minutes after inject: re-injects exactly once and marks escalated", async () => {
  const db = new FakeMopDb();
  const relay = new FakeRelay();
  const obligations = new FakeObligations();
  const cadence = await bootAndFire(db, relay, obligations);
  await withClock(at(14, 32), () => cadence.tick("scheduled"));
  assert.equal(hourlyInjects(relay).length, 1, "no re-inject before 20 minutes");
  await withClock(at(14, 33), () => cadence.tick("scheduled"));
  await withClock(at(14, 34), () => cadence.tick("scheduled"));
  await withClock(at(14, 50), () => cadence.tick("scheduled"));
  const injects = hourlyInjects(relay);
  assert.equal(injects.length, 2, "exactly one re-inject per bucket");
  assert.match(injects[1]!.message, /Re-inject: no hourly report posted for 2026-11-04:14 after 20 min\.$/);
  assert.equal(injects[1]!.message.includes("\n"), false);
  assert.equal(injects[1]!.eventType, "cadence-hourly-heartbeat-2026-11-04:14-reinject");
  assert.equal(obligations.rows.get("2026-11-04:14")!.escalated, true);
  assert.ok(db.events.includes("pm_cadence_hourly_report_reinjected"));
});

test("re-inject state survives a scheduler restart (still once per bucket)", async () => {
  const db = new FakeMopDb();
  const relay = new FakeRelay();
  const obligations = new FakeObligations();
  const first = await bootAndFire(db, relay, obligations);
  await withClock(at(14, 33), () => first.tick("scheduled"));
  first.stop();
  const second = new PMCadenceScheduler(db as never, relay as never, obligations);
  await withClock(at(14, 40), () => second.tick("boot"));
  await withClock(at(14, 41), () => second.tick("scheduled"));
  assert.equal(hourlyInjects(relay).length, 2);
});

test("resolved (report posted) or unknown state never re-injects; boot ticks never re-inject", async () => {
  for (const state of ["resolved", "unknown"] as const) {
    const db = new FakeMopDb();
    const relay = new FakeRelay();
    const obligations = new FakeObligations();
    const cadence = await bootAndFire(db, relay, obligations);
    if (state === "resolved") obligations.rows.get("2026-11-04:14")!.status = "resolved";
    else obligations.rows.clear();
    await withClock(at(14, 40), () => cadence.tick("scheduled"));
    assert.equal(hourlyInjects(relay).length, 1, state);
  }
  const db = new FakeMopDb();
  const relay = new FakeRelay();
  const cadence = await bootAndFire(db, relay, new FakeObligations());
  await withClock(at(14, 40), () => cadence.tick("boot"));
  assert.equal(hourlyInjects(relay).length, 1, "boot catch-up never re-fires");
});

test("a past bucket is not re-injected after the hour rolls over", async () => {
  const db = new FakeMopDb();
  const relay = new FakeRelay();
  const obligations = new FakeObligations();
  const cadence = await bootAndFire(db, relay, obligations);
  await withClock(at(15, 5), () => cadence.tick("scheduled"));
  assert.equal(hourlyInjects(relay).length, 1);
});

test("pm-ops failure fails open: the hourly prompt is still delivered and nothing throws", async () => {
  const db = new FakeMopDb();
  const relay = new FakeRelay();
  const ok = await upsertHourlyReportObligation("2026-11-04:14", "2026-11-04T08:43:00Z", false, async () => {
    throw new Error("pm-ops down");
  });
  assert.equal(ok, false);
  assert.equal(readHourlyReportObligation("2026-11-04:14", "/nonexistent/pm-ops.db"), "unknown");
  const cadence = new PMCadenceScheduler(db as never, relay as never, {
    upsert: async () => false,
    readState: () => "unknown",
  });
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

test("the real pm-ops writer accepts the upsert; repeat upsert keeps one open row", () => {
  const dir = mkdtempSync(join(tmpdir(), "mop-hourly-oblig-"));
  const dbPath = join(dir, "pm-ops.db");
  const legacy = fileURLToPath(new URL("../scripts/pm/shared-assets/claude/scripts/pm-ops-legacy.py", import.meta.url));
  const env = { ...process.env, PM_OPS_DB: dbPath, MOP_PM_OPS_SCRIPT: legacy };
  const prev = process.env.MOP_PM_OPS_SCRIPT;
  process.env.MOP_PM_OPS_SCRIPT = legacy;
  try {
    for (const escalated of [false, false, true]) {
      execFileSync("python3", hourlyReportUpsertArgs("2026-11-04:14", "2026-11-04T08:43:00Z", escalated), { env });
    }
  } finally {
    if (prev === undefined) delete process.env.MOP_PM_OPS_SCRIPT;
    else process.env.MOP_PM_OPS_SCRIPT = prev;
  }
  assert.equal(readHourlyReportObligation("2026-11-04:14", dbPath), "open");
  const rows = execFileSync("python3", ["-c",
    "import sqlite3,sys,json;c=sqlite3.connect(sys.argv[1]);print(json.dumps(c.execute(\"select kind,target_type,target_id,owner,severity,horizon,required_action,next_review_at,evidence_json from obligations\").fetchall()))",
    dbPath], { encoding: "utf8" });
  const parsed = JSON.parse(rows) as string[][];
  assert.equal(parsed.length, 1);
  const [kind, targetType, targetId, owner, severity, horizon, action, nextReview, evidence] = parsed[0]!;
  assert.deepEqual([kind, targetType, targetId, owner, severity, horizon], ["hourly_report_due", "hourly_bucket", "2026-11-04:14", "pm", "high", "hourly"]);
  assert.match(action!, /^Invoke Skill\(hourly-heartbeat\); post report for 2026-11-04:14\./);
  assert.equal(nextReview, "2026-11-04T08:53:00Z");
  assert.equal(JSON.parse(evidence!).escalated, true);
});

test("production server wires the live pm-ops deps; the scheduler default is inert", async () => {
  const server = readFileSync(new URL("../src/server.ts", import.meta.url), "utf8");
  assert.match(server, /new PMCadenceScheduler\(db, relay, PM_OPS_HOURLY_REPORT_DEPS\)/);
  const relay = new FakeRelay();
  const cadence = new PMCadenceScheduler(new FakeMopDb() as never, relay as never);
  await withClock(at(14, 5), () => cadence.tick("boot"));
  await withClock(at(14, 13), () => cadence.tick("scheduled"));
  await withClock(at(14, 40), () => cadence.tick("scheduled"));
  assert.equal(hourlyInjects(relay).length, 1, "inert default: no live ledger read, no re-inject");
});
