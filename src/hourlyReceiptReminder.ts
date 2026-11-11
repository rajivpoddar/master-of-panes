import { execFile } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import type { MoPDatabase } from "./db.js";

const KEY = "hourly_receipt_reminders_v1";
const CHANNEL = "C0ALZJHGE49";
const execute = promisify(execFile);
type Scheduler = (file: string, args: string[], options: { timeout: number }) => Promise<{ stdout: string }>;
type Row = { ts: string; due: number; state: "pending" | "claimed" | "scheduled" | "unknown"; scheduled_id?: string; receipt?: string };
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
    rows.push({ ts, due: Math.floor(posted / 1000) * 1000 + 300_000, state: "pending" });
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
      const result = await run("python3", [join(homedir(), ".claude/skills/alerts-reminder/scripts/alerts-schedule.py"), "--channel", CHANNEL, "--thread", row.ts, "--at", at, "--text", `<@U0BNFGX2UAX> CTO verification for Hourly ${time}: check PM's thread reply covers (1) every ACTIONS line has an owner or was done; (2) every slot over 3h on its lane has a PM hard stop or close-and-reimplement; (3) training step advanced vs the previous hourly, or a restart was launched; (4) broken/expired boxes reset; (5) every STUCK/unowned PR row has an owner; (6) P0s older than 4h have an owner. Reply PASS or list the misses for PM.`], { timeout: 15_000 });
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
