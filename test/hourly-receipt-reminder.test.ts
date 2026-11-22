import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { handoffHourlyReceipt, tickHourlyReminders, tickHourlyProcessing } from "../src/hourlyReceiptReminder.js";
import type { MoPDatabase } from "../src/db.js";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
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

// Authored regression definitions only. Standing MoP directive: NOT_RUN.
describe("receipt-bound hourly PM processing", () => {
  const ts = "1794375876.799129", now = Number(ts) * 1000;
  const receipt = `OK ts=${ts} channel=C0ALZJHGE49`;
  it("hands the confirmed parent to the PM relay once, independently of reminder completion", async () => {
    const f = fixture();
    assert.equal(handoffHourlyReceipt(f.db, ts, "failed", now), false);
    let calls = 0;
    const submit = async (message: string, event: string) => {
      calls++;
      assert.equal(f.state()[0].processing.state, "claimed");
      assert.equal(event, `cadence-hourly-processing-${ts}`);
      assert.match(message, /Invoke Skill\(hourly-report-processing\)/);
      assert.ok(message.includes(`C0ALZJHGE49/${ts}`));
      assert.match(message, /PM alone sends/);
      assert.match(message, /existing processing\/Step 3 owner/);
      assert.match(message, /no cap exception/);
      assert.match(message, /assignment\/epoch\/issue\/PR\/head/);
      return { ok: true, ambiguous: false, queued: true, submitKey: "Enter" as const };
    };
    await tickHourlyProcessing(f.db, submit, now);
    assert.equal(calls, 0);
    handoffHourlyReceipt(f.db, ts, receipt, now);
    const rows = f.state(); rows[0].state = "scheduled";
    f.db.setConfig("hourly_receipt_reminders_v1", JSON.stringify(rows));
    await tickHourlyProcessing(f.db, submit, now);
    handoffHourlyReceipt(f.db, ts, receipt, now);
    await tickHourlyProcessing(f.db, submit, now);
    assert.equal(calls, 1);
    assert.equal(f.state()[0].processing.state, "queued");
    assert.equal(f.state()[0].state, "scheduled");
    assert.equal(f.state()[0].processing.receipt.queued, true);
  });
  it("a concurrent tick cannot submit a second request while the first is in flight", async () => {
    const f = fixture(); handoffHourlyReceipt(f.db, ts, receipt, now);
    let finish!: () => void, calls = 0;
    const pending = new Promise<void>(resolve => { finish = resolve; });
    const submit = async () => {
      calls++; await pending;
      return { ok: true, ambiguous: false, submitKey: "Enter" as const };
    };
    const first = tickHourlyProcessing(f.db, submit, now);
    await tickHourlyProcessing(f.db, submit, now);
    // The original +5m scheduling is not gated on PM delivery/inspection.
    await tickHourlyReminders(f.db, async (_file, args) => ({
      stdout: `OK scheduled id=Q456 post_at=${args[args.indexOf("--at") + 1]}\n`,
    }));
    finish(); await first;
    assert.equal(calls, 1);
    assert.equal(f.state()[0].processing.state, "delivered");
    assert.equal(f.state()[0].state, "scheduled");
  });
  it("lost acknowledgement and restart with a consumed claim never resubmit", async () => {
    for (const disposition of ["throw", "ambiguous", "claimed", "refused"] as const) {
      const f = fixture(); handoffHourlyReceipt(f.db, ts, receipt, now);
      if (disposition === "claimed") {
        const rows = f.state(); rows[0].processing.state = "claimed";
        f.db.setConfig("hourly_receipt_reminders_v1", JSON.stringify(rows));
      }
      let calls = 0;
      const submit = async () => {
        calls++;
        if (disposition === "throw") throw Error("accepted then acknowledgement lost");
        if (disposition === "refused") return { ok: false, ambiguous: false, refused: "DND", submitKey: "Enter" as const };
        return { ok: false, ambiguous: true, submitKey: "Enter" as const };
      };
      await tickHourlyProcessing(f.db, submit, now);
      await tickHourlyProcessing(f.db, submit, now + 30_000);
      assert.equal(calls, disposition === "claimed" ? 0 : 1);
      assert.equal(f.state()[0].processing.state, disposition === "claimed" ? "claimed" : disposition === "refused" ? "refused" : "unknown");
    }
  });
  it("does not backfill historical reminder rows or dispatch an expired pending parent", async () => {
    const f = fixture();
    f.db.setConfig("hourly_receipt_reminders_v1", JSON.stringify([{ ts, due: now + 300_000, state: "scheduled" }]));
    // Re-presenting an old receipt does not add a processing field.
    handoffHourlyReceipt(f.db, ts, receipt, now);
    let calls = 0;
    const submit = async () => { calls++; return { ok: true, ambiguous: false, submitKey: "Enter" as const }; };
    await tickHourlyProcessing(f.db, submit, now);
    assert.equal(f.state()[0].processing, undefined);
    const rows = f.state(); rows[0].processing = { state: "pending" };
    f.db.setConfig("hourly_receipt_reminders_v1", JSON.stringify(rows));
    await tickHourlyProcessing(f.db, submit, now + 3 * 3600_000 + 1);
    assert.equal(calls, 0);
    assert.equal(f.state()[0].processing.state, "expired");
  });

  describe("routine batch bound to the one lane handoff (CTO REVISE #3)", () => {
    const bucket = "2026-11-22:10";
    function claim(payload: Record<string, unknown>) {
      const root = mkdtempSync(join(tmpdir(), "hourly-batch-"));
      mkdirSync(join(root, bucket), { recursive: true });
      writeFileSync(join(root, bucket, "routine-batch.json"), JSON.stringify(payload));
      return root;
    }
    const accepted = async () => ({ ok: true, ambiguous: false, submitKey: "Enter" as const });
    it("binds the lane prompt to the private file, records acceptance apart from the report post, and hands the bucket off once", async () => {
      const root = claim({ hourly_runnable: 2, fast_runnable: 5, rows: [{ id: 506, owner: "SECRET-OWNER", title: "SECRET-TITLE", step: "SECRET-STEP" }], ids: [], deferred: 0 });
      const f = fixture();
      assert.equal(handoffHourlyReceipt(f.db, ts, receipt, now, bucket), true);
      assert.equal(f.state()[0].batch_bucket, bucket);
      assert.equal(f.state()[0].processing.state, "pending", "the report post alone is not lane acceptance");
      const messages: string[] = [];
      const submit = async (message: string, event: string) => { messages.push(message); assert.equal(event, `cadence-hourly-processing-${ts}`); return accepted(); };
      await tickHourlyProcessing(f.db, submit, now, root);
      assert.equal(messages.length, 1);
      assert.ok(messages[0].includes(join(root, bucket, "routine-batch.json")));
      assert.match(messages[0], /2 runnable HOURLY obligation row\(s\) \(5 FAST row\(s\) stay on the Stop drain/);
      assert.doesNotMatch(messages[0], /SECRET-(OWNER|TITLE|STEP)/, "free-text ledger fields never ride the handoff text");
      const lane = f.state()[0].processing;
      assert.equal(lane.state, "delivered");
      assert.deepEqual(lane.batch, { bucket, file: join(root, bucket, "routine-batch.json"), hourly_runnable: 2, fast_open: 5 });
      // The same receipt again, and a second parent in the same bucket, never produce another batch handoff.
      handoffHourlyReceipt(f.db, ts, receipt, now, bucket);
      const second = "1794375877.000001";
      handoffHourlyReceipt(f.db, second, `OK ts=${second} channel=C0ALZJHGE49`, now, bucket);
      assert.equal(f.state().find((row: { ts: string }) => row.ts === second).batch_bucket, undefined);
      await tickHourlyProcessing(f.db, submit, now, root);
      assert.equal(messages.length, 2, "the second parent is handed to the lane without the batch");
      assert.doesNotMatch(messages[1], /ROUTINE BATCH/);
    });
    it("an invalid bucket, a missing file or nothing runnable leaves the handoff unchanged and unbound", async () => {
      const f = fixture();
      handoffHourlyReceipt(f.db, ts, receipt, now, "../../etc");
      assert.equal(f.state()[0].batch_bucket, undefined);
      for (const [label, root] of [["missing", mkdtempSync(join(tmpdir(), "hourly-batch-"))], ["empty", claim({ hourly_runnable: 0, fast_runnable: 3 })]] as const) {
        const g = fixture();
        handoffHourlyReceipt(g.db, ts, receipt, now, bucket);
        let text = "";
        await tickHourlyProcessing(g.db, async (message: string) => { text = message; return accepted(); }, now, root);
        assert.doesNotMatch(text, /ROUTINE BATCH/, label);
        assert.equal(g.state()[0].processing.batch, undefined, label);
        assert.equal(g.state()[0].processing.state, "delivered", label);
      }
    });
  });
});
