import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { handoffHourlyReceipt, tickHourlyReminders } from "../src/hourlyReceiptReminder.js";
import type { MoPDatabase } from "../src/db.js";

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
      assert.equal(args[args.indexOf("--text") + 1], `<@U0BNFGX2UAX> CTO verification for Hourly ${time}: check PM's thread reply covers (1) every ACTIONS line has an owner or was done; (2) every slot over 3h on its lane has a PM hard stop or close-and-reimplement; (3) training step advanced vs the previous hourly, or a restart was launched; (4) broken/expired boxes reset; (5) every STUCK/unowned PR row has an owner; (6) P0s older than 4h have an owner. Reply PASS or list the misses for PM.`);
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
