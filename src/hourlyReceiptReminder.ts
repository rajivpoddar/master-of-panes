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
type Processing = {
  state: "pending" | "claimed" | "delivered" | "queued" | "unknown" | "refused" | "expired";
  claimed_at?: number;
  receipt?: PMSubmitResult;
};
type Row = { ts: string; due: number; state: "pending" | "claimed" | "scheduled" | "unknown"; scheduled_id?: string; receipt?: string; processing?: Processing };
function load(db: MoPDatabase): Row[] {
  const value = db.getConfig(KEY);
  if (!value) return [];
  const rows: unknown = JSON.parse(value);
  if (!Array.isArray(rows)) throw new Error("invalid hourly receipt state");
  return rows as Row[];
}
function save(db: MoPDatabase, rows: Row[]): void { db.setConfig(KEY, JSON.stringify(rows)); }

/** Accept only the successful top-level sender receipt, keyed permanently by Slack ts. */
export function handoffHourlyReceipt(db: MoPDatabase, ts: string, receipt: string, now = Date.now()): boolean {
  if (!/^\d{10}\.\d{6}$/.test(ts) || receipt.trim() !== `OK ts=${ts} channel=${CHANNEL}`) return false;
  const posted = Number(ts) * 1000;
  if (!Number.isFinite(posted) || posted > now + 120_000 || posted < now - 3 * 3600_000) return false;
  const rows = load(db);
  if (!rows.some(row => row.ts === ts)) {
    rows.push({ ts, due: Math.floor(posted / 1000) * 1000 + 300_000, state: "pending", processing: { state: "pending" } });
    save(db, rows);
  }
  return true;
}

export function hourlyProcessingPrompt(ts: string): string {
  return `Confirmed hourly parent C0ALZJHGE49/${ts}. Invoke Skill(hourly-report-processing) for this exact thread, including Step 3 checkout review and PM steering of every eligible current lane. ` +
    `First reconcile the thread and any existing processing/Step 3 owner for this parent; reuse completed or in-flight work and its receipts, never launch a second inspector or repeat an issued/UNKNOWN slot message. ` +
    `Use one read-only inspection helper under existing agent limits (no cap exception), or PM's bounded inspection if capacity is unavailable. Read current assignment/epoch/issue/PR/head, parent-to-child mapping, substantive progress and AC/runtime/test evidence; revalidate the lane before any direction. ` +
    `PM alone sends one tailored continue/checkpoint or correction through Skill(message-slot), with tuple, observed blocker, requested result and existing time bound. Protect DND, real background work and dirty/unpushed WIP; no age-only kill, reset, reassignment, scope reduction or waiver. ` +
    `Reply in this hourly thread with each lane's evidence, decision, actual native send/acceptance/ack disposition and next checkpoint. DND/refusal/UNKNOWN/unavailable stays explicit with owner and wake; issued/UNKNOWN is reconciliation-only. Owner names, working and a generic reminder are not steering. ` +
    `The +5m CTO verification checks these outcomes. This is a PM work handoff, not a Stop, merge or customer-recovery gate; continue urgent recovery independently.`;
}

/** New confirmed parents only. Old reminder rows are not a processing backlog.
 * Claim in the existing receipt record before relay submission; a lost result or
 * interrupted claim is consumed, even if a later tick or restart sees it again.
 */
export async function tickHourlyProcessing(
  db: MoPDatabase,
  submit: (message: string, event: string) => Promise<PMSubmitResult>,
  now = Date.now(),
): Promise<void> {
  for (const row of load(db)) {
    if (row.processing?.state !== "pending") continue;
    const latest = load(db);
    const current = latest.find(item => item.ts === row.ts);
    if (current?.processing?.state !== "pending") continue;
    if (now - Number(row.ts) * 1000 > 3 * 3600_000) {
      current.processing = { state: "expired" };
      save(db, latest);
      continue;
    }
    current.processing = { state: "claimed", claimed_at: now };
    save(db, latest);
    let receipt: PMSubmitResult | undefined;
    let state: Processing["state"] = "unknown";
    try {
      receipt = await submit(hourlyProcessingPrompt(row.ts), `cadence-hourly-processing-${row.ts}`);
      if (receipt?.ok === true && receipt.ambiguous === false && !receipt.withheld && !receipt.refused) {
        state = receipt.queued === true ? "queued" : "delivered";
      } else if (receipt?.ok === false && receipt.ambiguous === false && receipt.refused && !receipt.withheld) {
        state = "refused";
      }
    } catch { /* Submission may have been accepted before its acknowledgement was lost. */ }
    const after = load(db);
    const consumed = after.find(item => item.ts === row.ts);
    if (consumed?.processing?.state === "claimed") {
      consumed.processing = { state, claimed_at: now, ...(receipt ? { receipt } : {}) };
      save(db, after);
    }
  }
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
