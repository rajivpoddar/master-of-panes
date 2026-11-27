import { describe, it } from "node:test";
import assert from "node:assert/strict";
import * as reminder from "../src/hourlyReceiptReminder.js";
import { handoffHourlyReceipt, tickHourlyReminders } from "../src/hourlyReceiptReminder.js";
import type { MoPDatabase } from "../src/db.js";
import { readFileSync } from "node:fs";
import { join } from "node:path";

function fixture() {
  let value: string | null = null;
  const db = { getConfig: () => value, setConfig: (_: string, next: string) => { value = next; } } as unknown as MoPDatabase;
  return { db, state: () => JSON.parse(value || "[]") };
}
describe("hourly receipt reminders", () => {
  it("schedules immediately for five minutes after the parent and records the receipt", async () => {
    const f = fixture(), ts = "1794321992.050059", now = Number(ts) * 1000;
    assert.equal(handoffHourlyReceipt(f.db, ts, "invalid", now), false);
    const receipt = `OK ts=${ts} channel=C0ALZJHGE49`;
    assert.equal(handoffHourlyReceipt(f.db, ts, receipt, now), true);
    assert.equal(handoffHourlyReceipt(f.db, ts, receipt, now), true);
    let calls = 0;
    await tickHourlyReminders(f.db, async (file, args) => {
      calls++;
      assert.equal(f.state()[0].state, "claimed");
      assert.equal(file, "python3");
      assert.match(args[0], /alerts-reminder\/scripts\/alerts-schedule\.py$/);
      assert.equal(args[args.indexOf("--thread") + 1], ts);
      const at = args[args.indexOf("--at") + 1];
      assert.equal(Date.parse(at), Math.floor(now / 1000) * 1000 + 300000);
      const time = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(now));
      const prompt = args[args.indexOf("--text") + 1];
      assert.ok(prompt.includes(`Hourly ${time}, parent ${ts}`));
      assert.match(prompt, /verify Step 3 actually inspected every current in-progress lane/);
      assert.match(prompt, /native communication receipt\/disposition/);
      assert.match(prompt, /owner, working or a generic reminder is not PASS/);
      assert.match(prompt, /DND\/refusal\/UNKNOWN\/unavailable/);
      assert.match(prompt, /never resend issued\/UNKNOWN/);
      return { stdout: `OK scheduled id=Q123 post_at=${at}\n` };
    });
    await tickHourlyReminders(f.db, async () => { calls++; return { stdout: "" }; });
    assert.equal(calls, 1);
    assert.equal(f.state()[0].state, "scheduled");
    assert.equal(f.state()[0].scheduled_id, "Q123");
  });
  it("does not retry unknown delivery", async () => {
    const f = fixture(), ts = "1794321992.050059", now = Number(ts) * 1000;
    handoffHourlyReceipt(f.db, ts, `OK ts=${ts} channel=C0ALZJHGE49`, now);
    let calls = 0;
    const runner = async () => { calls++; throw Error("timeout"); };
    await tickHourlyReminders(f.db, runner);
    await tickHourlyReminders(f.db, runner);
    assert.equal(calls, 1);
    assert.equal(f.state()[0].state, "unknown");
  });
});

// Rajiv D0AMF0XE6TS thread 1795794483.299309, ts 1795794743.693179: no cadence-hourly-processing PM injection.
describe("hourly processing is not injected into the PM pane", () => {
  it("the module exports no PM processing submitter and the server never calls one", () => {
    assert.equal((reminder as Record<string, unknown>).tickHourlyProcessing, undefined);
    assert.equal((reminder as Record<string, unknown>).hourlyProcessingPrompt, undefined);
    const server = readFileSync(join(import.meta.dirname, "../src/server.ts"), "utf8");
    assert.doesNotMatch(server, /cadence-hourly-processing|tickHourlyProcessing/);
  });
});
