import { execFile } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import type { MoPDatabase } from "./db.js";
import type { PMSubmitResult } from "./relay.js";

const KEY = "hourly_receipt_reminders_v1";
const CHANNEL = "C0ALZJHGE49";
const execute = promisify(execFile);
type Scheduler = (file: string, args: string[], options: { timeout: number }) => Promise<{ stdout: string }>;
/** Historical lane-acceptance shape kept so stored rows still parse. Rajiv D0AMF0XE6TS thread 1795794483.299309 (ts 1795794743.693179): MoP no longer
 * injects the hourly-processing prompt into the PM pane; the routine batch path and row count ride on the HeyDonna Alerts hourly post instead. */
export type RoutineBatch = { bucket: string; file: string; hourly_runnable: number; fast_open: number };
type Processing = {
  state: "pending" | "claimed" | "delivered" | "queued" | "unknown" | "refused" | "expired";
  claimed_at?: number;
  receipt?: PMSubmitResult;
  /** Lane ACCEPTANCE of the batch: set with the lane receipt above, never by the report post. */
  batch?: RoutineBatch;
};
/** `ts`/`receipt` = the REPORT POST (Slack parent). `processing.state`/`processing.receipt` = the LANE ACCEPTANCE. They are separate outcomes. */
type Row = { ts: string; due: number; state: "pending" | "claimed" | "scheduled" | "unknown"; scheduled_id?: string; receipt?: string; processing?: Processing; batch_bucket?: string };
const BUCKET_RE = /^\d{4}-\d{2}-\d{2}:\d{2}$/;
function load(db: MoPDatabase): Row[] {
  const value = db.getConfig(KEY);
  if (!value) return [];
  const rows: unknown = JSON.parse(value);
  if (!Array.isArray(rows)) throw new Error("invalid hourly receipt state");
  return rows as Row[];
}
function save(db: MoPDatabase, rows: Row[]): void { db.setConfig(KEY, JSON.stringify(rows)); }

/** Accept only the successful top-level sender receipt, keyed permanently by Slack ts.
 * `bucket` (optional, shape-checked) names the report bucket claim that holds this hour's private routine-batch.json. The first parent of
 * a bucket owns the batch; a later parent in the same bucket is handed off without it, so one bucket never hands the batch off twice.
 */
export function handoffHourlyReceipt(db: MoPDatabase, ts: string, receipt: string, now = Date.now(), bucket?: string): boolean {
  if (!/^\d{10}\.\d{6}$/.test(ts) || receipt.trim() !== `OK ts=${ts} channel=${CHANNEL}`) return false;
  const posted = Number(ts) * 1000;
  if (!Number.isFinite(posted) || posted > now + 120_000 || posted < now - 3 * 3600_000) return false;
  const rows = load(db);
  if (!rows.some(row => row.ts === ts)) {
    const owned = typeof bucket === "string" && BUCKET_RE.test(bucket) && !rows.some(row => row.batch_bucket === bucket);
    rows.push({ ts, due: Math.floor(posted / 1000) * 1000 + 300_000, state: "pending", processing: { state: "pending" }, ...(owned ? { batch_bucket: bucket } : {}) });
    save(db, rows);
  }
  return true;
}

/** Schedule immediately after a durable claim; ambiguous outcomes are never retried. */
export async function tickHourlyReminders(db: MoPDatabase, run: Scheduler = execute): Promise<void> {
  const rows = load(db);
  for (const row of rows) {
    if (row.state !== "pending") continue;
    const before = load(db);
    const claim = before.find(item => item.ts === row.ts);
    if (!claim || claim.state !== "pending") continue;
    claim.state = "claimed";
    save(db, before);
    let state: "scheduled" | "unknown" = "unknown";
    let receipt: string | undefined;
    let scheduledId: string | undefined;
    try {
      const time = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(Number(row.ts) * 1000));
      const at = new Date(row.due).toISOString().replace(".000Z", "Z");
      const verification = `<@U0BNFGX2UAX> CTO verification for Hourly ${time}, parent ${row.ts}: ` +
        `Apply $heydonna-cto-hourly-review from /Users/rajiv/.codex/skills/heydonna-cto-hourly-review/SKILL.md; CTO Decisions delegates this one parent-bound audit to the existing PR Reviews owner under the wake SOP. ` +
        `verify Step 3 actually inspected every current in-progress lane, not just ACTIONS or owner labels. ` +
        `Each eligible lane needs current assignment/epoch/issue/PR/head (explicit parent/child mapping), substantive progress and AC/runtime/test evidence, a tailored continue/checkpoint or correction, and PM's native communication receipt/disposition with next checkpoint. ` +
        `Distinguish accepted delivery from slot acknowledgement and completed work. Reconcile existing messages; never resend issued/UNKNOWN effects. ` +
        `DND/refusal/UNKNOWN/unavailable must name blocker, owner and wake; they are visible misses, not successful steering. List absent/unproven lanes; owner, working or a generic reminder is not PASS. ` +
        `Preserve the existing checks: every ACTIONS item has correct outcome/AC evidence or an explicit remaining disposition; overrun lanes have a current PM decision/checkpoint preserving WIP; training progress/recovery is checked under its existing rules; broken test/QA boxes have real recovery receipts or documented protected exceptions; STUCK PRs have current-head recovery actions; aged P0s have accountable custody and next action. ` +
        `Verify genuinely dead proxies were safely cleaned up by PM with receipts; a stale badge does not prove death. Named owners alone do not prove completed handling. ` +
        `Use audit key verify-hourly-pm:${row.ts}:v1 in this original thread; reuse an existing audit, never post a duplicate. No age-only kill/reset/reassign, forced recovery or product-merge gate. Reply PASS only for evidenced coverage, otherwise list misses for PM.`;
      const result = await run("python3", [join(homedir(), ".claude/skills/alerts-reminder/scripts/alerts-schedule.py"), "--channel", CHANNEL, "--thread", row.ts, "--at", at, "--text", verification], { timeout: 15_000 });
      const match = /^OK scheduled id=(\S+) post_at=(\S+)\s*$/.exec(result.stdout);
      if (match && match[2] === at) { state = "scheduled"; scheduledId = match[1]; receipt = result.stdout.trim(); }
    } catch { /* The helper may have scheduled before its result was lost. */ }
    const latest = load(db);
    const current = latest.find(item => item.ts === row.ts);
    if (current?.state === "claimed") {
      current.state = state;
      if (scheduledId && receipt) { current.scheduled_id = scheduledId; current.receipt = receipt; }
      save(db, latest);
    }
  }
}
