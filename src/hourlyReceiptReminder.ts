import { execFile } from "node:child_process";
import { readFileSync } from "node:fs";
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
  state: "pending" | "claimed" | "delivered" | "queued" | "unknown" | "refused" | "expired" | "accepted";
  claimed_at?: number;
  /** When the hourly processing lane accepted this parent's routine batch (`accepted` only). */
  accepted_at?: number;
  receipt?: PMSubmitResult;
  /** Lane ACCEPTANCE of the batch: set with the lane receipt above, never by the report post. */
  batch?: RoutineBatch;
};
/** `ts`/`receipt` = the REPORT POST (Slack parent). `processing.state`/`processing.receipt` = the LANE ACCEPTANCE. They are separate outcomes. */
type Row = { ts: string; due: number; state: "pending" | "claimed" | "scheduled" | "unknown"; scheduled_id?: string; receipt?: string; processing?: Processing; batch_bucket?: string };
const BUCKET_RE = /^\d{4}-\d{2}-\d{2}:\d{2}$/;
const BATCH_ROOT = join(homedir(), ".claude/hourly-heartbeat-posts");
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

export type BatchAcceptance =
  | { ok: true; already: boolean; ts: string; batch: RoutineBatch; accepted_at: number }
  | { ok: false; reason: "invalid_args" | "no_parent" | "not_batch_owner" | "expired" | "batch_unreadable" | "already_settled" };

/** Lane ACCEPTANCE of a parent's routine batch, recorded on that parent's existing receipt row (`processing`).
 * CTO REVISE C0ALZJHGE49 thread 1795798240.220189, ts 1795798363.328589: "Update that consumer to take the same-bucket path from the
 * Alerts post and show one genuine lane acceptance on its existing receipt." MoP never injects the work (Rajiv ts 1795794743.693179);
 * the hourly processing lane calls this after it has read the batch file named in the Alerts post. It binds to the exact parent ts,
 * the bucket that parent owns, and that bucket's private file. It is written once; a repeat with the same binding is idempotent.
 */
export function acceptHourlyBatch(db: MoPDatabase, ts: unknown, bucket: unknown, now = Date.now(), batchRoot = BATCH_ROOT): BatchAcceptance {
  if (typeof ts !== "string" || !/^\d{10}\.\d{6}$/.test(ts) || typeof bucket !== "string" || !BUCKET_RE.test(bucket)) {
    return { ok: false, reason: "invalid_args" };
  }
  const rows = load(db);
  const row = rows.find(item => item.ts === ts);
  if (!row) return { ok: false, reason: "no_parent" };
  if (row.batch_bucket !== bucket) return { ok: false, reason: "not_batch_owner" };
  const file = join(batchRoot, bucket, "routine-batch.json");
  const prior = row.processing;
  if (prior?.state === "accepted" && prior.batch && prior.accepted_at) {
    return prior.batch.bucket === bucket && prior.batch.file === file
      ? { ok: true, already: true, ts, batch: prior.batch, accepted_at: prior.accepted_at }
      : { ok: false, reason: "already_settled" };
  }
  if (prior && prior.state !== "pending") return { ok: false, reason: "already_settled" };
  if (now - Number(ts) * 1000 > 3 * 3600_000) return { ok: false, reason: "expired" };
  let batch: RoutineBatch;
  try {
    const data = JSON.parse(readFileSync(file, "utf8")) as { hourly_runnable?: unknown; fast_runnable?: unknown };
    const runnable = Number(data.hourly_runnable);
    if (!Number.isInteger(runnable) || runnable < 1) return { ok: false, reason: "batch_unreadable" };
    const fast = Number(data.fast_runnable);
    batch = { bucket, file, hourly_runnable: runnable, fast_open: Number.isInteger(fast) && fast > 0 ? fast : 0 };
  } catch { return { ok: false, reason: "batch_unreadable" }; }
  row.processing = { state: "accepted", accepted_at: now, batch };
  save(db, rows);
  return { ok: true, already: false, ts, batch, accepted_at: now };
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
