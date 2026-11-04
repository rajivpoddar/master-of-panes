/**
 * Hourly report obligation (Rajiv DM D0AMF0XE6TS thread 1793783982.877709,
 * ts 1793784122.949659: "hourly report should never be missed. can MoP add an
 * obligation for it automatically?").
 *
 * Incident: the hourly-heartbeat inject for bucket 2026-11-04:14 landed while
 * the PM pane was mid-compaction; it never ran and nothing noticed.
 *
 * - When the hourly-heartbeat cadence injects bucket YYYY-MM-DD:HH, MoP upserts a
 *   pm-ops `hourly_report_due` obligation through the existing pm-ops writer.
 * - `hourly-thread-ts --set <ts>` (run by the heartbeat agent after its post)
 *   resolves it with the post ts as the reason.
 * - 20 minutes after injection, if the obligation is still open, the scheduler
 *   re-injects the hourly prompt ONCE for that bucket and marks it escalated.
 *
 * Rule 50: nothing here gates PM input or tools. Every pm-ops call fails open.
 */

import { execFile } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";

import Database from "better-sqlite3";

const run = promisify(execFile);

export const HOURLY_REPORT_KIND = "hourly_report_due";
export const HOURLY_REPORT_TARGET_TYPE = "hourly_bucket";
export const HOURLY_REPORT_ESCALATE_AFTER_MS = 20 * 60 * 1000;
/** The Stop surface waits this long so the inject itself gets a chance to run. */
export const HOURLY_REPORT_STOP_SURFACE_AFTER_MS = 10 * 60 * 1000;

const DEFAULT_PM_OPS_DB =
  "/Users/rajiv/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db";

export type Exec = (file: string, args: string[], options: { timeout: number }) => Promise<unknown>;
/** absent = the ledger was read and holds no row for the bucket; unknown = unreadable. */
export type ObligationState = "open" | "resolved" | "absent" | "unknown";
/**
 * created: the writer exited 0. failed: the writer definitely did not commit
 * (spawn failure or a non-ambiguous non-zero exit). unknown: commit status is
 * ambiguous (timeout/kill, or exit 2, which pm-ops also returns for an
 * accepted write whose row id is 2).
 */
export type WriterOutcome = "created" | "failed" | "unknown";

function pmOpsScript(): string {
  return process.env.MOP_PM_OPS_SCRIPT ?? join(homedir(), ".claude/scripts/pm-ops.py");
}

function isoZ(ms: number): string {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");
}

export function hourlyReportAction(bucket: string): string {
  return (
    `Invoke Skill(hourly-heartbeat); post report for ${bucket}. ` +
    "hourly-thread-ts --set <ts> --bucket <compose bucket> --receipt <slack-send OK line> resolves this after the post. " +
    "If the hourly agent for this bucket is already running, claim it instead: " +
    `pm-ops.py obligation-upsert --kind ${HOURLY_REPORT_KIND} --target-type ${HOURLY_REPORT_TARGET_TYPE} ` +
    `--target-id ${bucket} --suppress-until <UTC now+20min ISO Z>.`
  );
}

export function hourlyReportUpsertArgs(
  bucket: string,
  injectedAt: string,
  escalated: boolean,
): string[] {
  const injectedMs = Date.parse(injectedAt);
  const evidence: Record<string, unknown> = {
    source: "mop-pm-cadence",
    task: "hourly-heartbeat",
    bucket,
    injected_at: injectedAt,
    escalated,
    directive: "Rajiv D0AMF0XE6TS ts 1793784122.949659",
  };
  const args = [
    pmOpsScript(),
    "obligation-upsert",
    "--kind", HOURLY_REPORT_KIND,
    "--target-type", HOURLY_REPORT_TARGET_TYPE,
    "--target-id", bucket,
    "--owner", "pm",
    "--severity", "high",
    "--horizon", "hourly",
    "--title", `Hourly report due for ${bucket}${escalated ? " (re-injected)" : ""}`,
    "--action", hourlyReportAction(bucket),
    "--evidence-json", JSON.stringify(evidence),
  ];
  if (!escalated && Number.isFinite(injectedMs)) {
    args.push("--next-review-at", isoZ(injectedMs + HOURLY_REPORT_STOP_SURFACE_AFTER_MS));
  }
  return args;
}

/** Classify a pm-ops writer error without retrying it. */
export function classifyWriterError(error: unknown): WriterOutcome {
  const e = error as { code?: unknown; killed?: boolean; signal?: unknown } | null;
  if (!e || typeof e !== "object") return "unknown";
  if (e.killed || (typeof e.signal === "string" && e.signal)) return "unknown";
  if (e.code === 2) return "unknown";
  if (typeof e.code === "number") return "failed";
  if (e.code === "ENOENT" || e.code === "EACCES") return "failed";
  return "unknown";
}

/** Upsert through the canonical pm-ops writer exactly once. Never throws (fail open). */
export async function upsertHourlyReportObligation(
  bucket: string,
  injectedAt: string,
  escalated: boolean,
  execute: Exec = run,
): Promise<WriterOutcome> {
  try {
    await execute("python3", hourlyReportUpsertArgs(bucket, injectedAt, escalated), { timeout: 10_000 });
    return "created";
  } catch (error) {
    const outcome = classifyWriterError(error);
    console.error(`[pm-cadence] hourly_report_due upsert ${outcome} bucket=${bucket}: ${String(error)}`);
    return outcome;
  }
}

/** Read-only state of the newest obligation row for a bucket. Never throws. */
export function readHourlyReportObligation(
  bucket: string,
  dbPath: string = process.env.MOP_PM_OPS_DB ?? DEFAULT_PM_OPS_DB,
): ObligationState {
  try {
    const db = new Database(dbPath, { readonly: true, fileMustExist: true });
    try {
      const row = db.prepare(
        `SELECT status FROM obligations
         WHERE kind=? AND target_type=? AND target_id=?
         ORDER BY id DESC LIMIT 1`,
      ).get(HOURLY_REPORT_KIND, HOURLY_REPORT_TARGET_TYPE, bucket) as { status?: string } | undefined;
      if (!row) return "absent";
      return row.status === "open" ? "open" : "resolved";
    } finally {
      db.close();
    }
  } catch {
    return "unknown";
  }
}
