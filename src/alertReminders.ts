/**
 * MoP recurring Alerts reminders.
 *
 * Source: Rajiv, DM D0AMF0XE6TS ts 1794106952.185849: "how do we setup recurring alerts
 * reminder? ... can we add it in MoP and post to the thread directly with alerts user id?"
 *
 * A durable `alert_reminders` table plus a scheduler tick inside the MoP server. The tick
 * posts each due row once as the HeyDonna Alerts bot (chat.postMessage with
 * SLACK_ALERTS_BOT_TOKEN, read from the PM .env.local at send time and never logged), then
 * advances `next_at` by whole intervals so downtime never causes a burst of catch-up posts.
 *
 * Rules honoured here:
 * - Rule 50: this never touches PM input, tools or the UserPromptSubmit hook chain. The
 *   scheduler only talks to Slack and its own table.
 * - Delivery is classified. Only an AFFIRMATIVE zero-effect failure (a known pre-post Slack
 *   `ok:false` error, a 429, or a connection that never opened) restores the due time and retries
 *   with a bounded backoff. A timeout, lost response, unparseable reply, 5xx or unlisted Slack
 *   error may already have posted, so that occurrence stays consumed and the row records an
 *   UNKNOWN disposition; it is never retried (a retry could post twice). Never falls back to
 *   another token and never throws out of the tick.
 * - Every claim carries a per-row generation. Immediately before each send the exact claim and
 *   binding are revalidated, and the completion writes are fenced to the same generation, so a
 *   row that was cancelled, replaced, expired or whose stop obligation closed during a batch is
 *   not sent and a stale completion can never touch its replacement.
 * - `until_at` is mandatory and at most seven days out, so no reminder can run unbounded.
 */

import { readFileSync } from "node:fs";
import Database from "better-sqlite3";
import type { Hono } from "hono";

export const ALERT_REMINDER_DEFAULT_CHANNEL = "C0ALZJHGE49";
export const ALERT_REMINDER_MIN_INTERVAL_S = 600;
export const ALERT_REMINDER_MAX_HORIZON_S = 7 * 24 * 60 * 60;
export const ALERT_REMINDER_MAX_TEXT = 3000;
const DEFAULT_ENV_FILE = "/Users/rajiv/Downloads/projects/heydonna-app/.env.local";
const DEFAULT_PM_OPS_DB =
  "/Users/rajiv/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db";
const TOKEN_ENV_KEY = "SLACK_ALERTS_BOT_TOKEN";

const KEY_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$/;
const CHANNEL_RE = /^[CGD][A-Z0-9]{8,12}$/;
const THREAD_TS_RE = /^\d{9,11}\.\d{6}$/;

export type AlertReminderStatus = "active" | "stopped" | "expired";
export type AlertReminderOutcome = "posted" | "failed" | "unknown";

export interface AlertReminderRow {
  key: string;
  channel: string;
  thread_ts: string;
  text: string;
  interval_s: number;
  /** epoch ms of the next due post */
  next_at: number;
  /** epoch ms; no post is made after this instant */
  until_at: number;
  stop_obligation_id: number | null;
  status: AlertReminderStatus;
  stop_reason: string | null;
  post_count: number;
  last_post_ts: string | null;
  last_posted_at: number | null;
  last_error: string | null;
  fail_count: number;
  retry_at: number | null;
  created_by: string | null;
  source_ref: string | null;
  created_at: number;
  updated_at: number;
  /** Bumped by every claim and every add/replace/revive; fences send and completion to one claim. */
  claim_gen: number;
  /** Disposition of the most recent occurrence for the current claim. */
  last_outcome: AlertReminderOutcome | null;
  /** Occurrences whose delivery is unknown (may or may not have posted). Never retried. */
  unknown_count: number;
  last_unknown_at: number | null;
  last_unknown_reason: string | null;
}

export interface AlertReminderAddInput {
  key: string;
  channel: string;
  thread_ts: string;
  text: string;
  interval_s: number;
  until_at: number;
  /** When true the caller gave a relative `--until`, so it is not part of the idempotency comparison. */
  until_relative: boolean;
  first_at: number;
  stop_obligation_id: number | null;
  replace: boolean;
  created_by: string | null;
  source_ref: string | null;
}

export type AlertReminderAddOutcome = "created" | "noop" | "replaced" | "revived";

export type AlertReminderAddResult =
  | { ok: true; outcome: AlertReminderAddOutcome; row: AlertReminderRow }
  | { ok: false; error: string; status: 400 | 404 | 409 | 503; differs?: string[]; row?: AlertReminderRow };

/** Columns added after the first release. Additive with defaults, so rollback to the old code is safe. */
const ADDED_COLUMNS: ReadonlyArray<readonly [string, string]> = [
  ["claim_gen", "INTEGER NOT NULL DEFAULT 0"],
  ["last_outcome", "TEXT"],
  ["unknown_count", "INTEGER NOT NULL DEFAULT 0"],
  ["last_unknown_at", "INTEGER"],
  ["last_unknown_reason", "TEXT"],
];

export function ensureAlertReminderSchema(db: Database.Database): void {
  db.exec(`
    CREATE TABLE IF NOT EXISTS alert_reminders (
      key TEXT PRIMARY KEY,
      channel TEXT NOT NULL DEFAULT '${ALERT_REMINDER_DEFAULT_CHANNEL}',
      thread_ts TEXT NOT NULL,
      text TEXT NOT NULL,
      interval_s INTEGER NOT NULL CHECK (interval_s >= ${ALERT_REMINDER_MIN_INTERVAL_S}),
      next_at INTEGER NOT NULL,
      until_at INTEGER NOT NULL,
      stop_obligation_id INTEGER,
      status TEXT NOT NULL CHECK (status IN ('active', 'stopped', 'expired')),
      stop_reason TEXT,
      post_count INTEGER NOT NULL DEFAULT 0,
      last_post_ts TEXT,
      last_posted_at INTEGER,
      last_error TEXT,
      fail_count INTEGER NOT NULL DEFAULT 0,
      retry_at INTEGER,
      created_by TEXT,
      source_ref TEXT,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      ${ADDED_COLUMNS.map(([name, ddl]) => `${name} ${ddl}`).join(",\n      ")}
    );
    CREATE INDEX IF NOT EXISTS idx_alert_reminders_status_next
      ON alert_reminders(status, next_at);
  `);
  // A table created by the first release lacks the added columns; existing rows keep their data.
  const present = new Set(
    (db.prepare("PRAGMA table_info(alert_reminders)").all() as Array<{ name: string }>).map((column) => column.name),
  );
  for (const [name, ddl] of ADDED_COLUMNS) {
    if (!present.has(name)) db.exec(`ALTER TABLE alert_reminders ADD COLUMN ${name} ${ddl}`);
  }
}

// ─── Pure helpers ────────────────────────────────────────

/** First grid point `next + k * interval` strictly after `now`; `next` itself when it is still in the future. */
export function advanceByWholeIntervals(nextAtMs: number, intervalMs: number, nowMs: number): number {
  if (nextAtMs > nowMs) return nextAtMs;
  const steps = Math.floor((nowMs - nextAtMs) / intervalMs) + 1;
  return nextAtMs + steps * intervalMs;
}

/** Retry spacing after a failed post: 60s, 120s, 240s ... capped at 10 minutes. */
export function retryBackoffMs(failCountBefore: number): number {
  return Math.min(60_000 * 2 ** Math.max(0, failCountBefore), 600_000);
}

function asString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function parseIsoMs(value: unknown): number | null {
  if (typeof value !== "string" || !value.trim()) return null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
}

export function normalizeAddRequest(
  body: Record<string, unknown>,
  nowMs: number,
): { ok: true; value: AlertReminderAddInput } | { ok: false; error: string } {
  const key = asString(body.key);
  if (!key || !KEY_RE.test(key)) return { ok: false, error: "key must match [A-Za-z0-9][A-Za-z0-9._:-]{0,79}" };

  const channel = body.channel === undefined || body.channel === null ? ALERT_REMINDER_DEFAULT_CHANNEL : asString(body.channel);
  if (!channel || !CHANNEL_RE.test(channel)) return { ok: false, error: "channel must be a Slack channel id such as C0ALZJHGE49" };

  const threadTs = asString(body.thread_ts);
  if (!threadTs || !THREAD_TS_RE.test(threadTs)) return { ok: false, error: "thread_ts must look like 1793953549.727149" };

  const text = asString(body.text)?.trim();
  if (!text) return { ok: false, error: "text is required" };
  if (text.length > ALERT_REMINDER_MAX_TEXT) return { ok: false, error: `text exceeds ${ALERT_REMINDER_MAX_TEXT} characters` };

  const interval = body.interval_s;
  if (typeof interval !== "number" || !Number.isInteger(interval)) return { ok: false, error: "interval_s must be an integer number of seconds" };
  if (interval < ALERT_REMINDER_MIN_INTERVAL_S) return { ok: false, error: `interval_s must be at least ${ALERT_REMINDER_MIN_INTERVAL_S} (10 minutes)` };
  if (interval > ALERT_REMINDER_MAX_HORIZON_S) return { ok: false, error: "interval_s must not exceed 7 days" };

  const untilAt = parseIsoMs(body.until_at);
  if (untilAt === null) return { ok: false, error: "until_at is required (ISO timestamp)" };
  if (untilAt <= nowMs) return { ok: false, error: "until_at must be in the future" };
  if (untilAt > nowMs + ALERT_REMINDER_MAX_HORIZON_S * 1000) return { ok: false, error: "until_at must be within 7 days" };

  let firstAt = nowMs + interval * 1000;
  if (body.first_at !== undefined && body.first_at !== null) {
    const parsed = parseIsoMs(body.first_at);
    if (parsed === null) return { ok: false, error: "first_at must be an ISO timestamp" };
    firstAt = Math.max(parsed, nowMs);
  }
  if (firstAt > untilAt) return { ok: false, error: "first post would fall after until_at; shorten the interval or extend until" };

  let stopObligation: number | null = null;
  if (body.stop_obligation_id !== undefined && body.stop_obligation_id !== null) {
    const id = body.stop_obligation_id;
    if (typeof id !== "number" || !Number.isInteger(id) || id <= 0) return { ok: false, error: "stop_obligation_id must be a positive integer" };
    stopObligation = id;
  }

  const createdBy = asString(body.created_by)?.trim().slice(0, 80) || null;
  const sourceRef = asString(body.source_ref)?.trim().slice(0, 300) || null;

  return {
    ok: true,
    value: {
      key,
      channel,
      thread_ts: threadTs,
      text,
      interval_s: interval,
      until_at: untilAt,
      until_relative: body.until_relative === true,
      first_at: firstAt,
      stop_obligation_id: stopObligation,
      replace: body.replace === true,
      created_by: createdBy,
      source_ref: sourceRef,
    },
  };
}

/** Names of the arguments that differ between an existing active row and a new add request. */
export function differingArgs(row: AlertReminderRow, input: AlertReminderAddInput): string[] {
  const differs: string[] = [];
  if (row.channel !== input.channel) differs.push("channel");
  if (row.thread_ts !== input.thread_ts) differs.push("thread_ts");
  if (row.text !== input.text) differs.push("text");
  if (row.interval_s !== input.interval_s) differs.push("interval_s");
  if (row.stop_obligation_id !== input.stop_obligation_id) differs.push("stop_obligation_id");
  if (!input.until_relative && row.until_at !== input.until_at) differs.push("until_at");
  return differs;
}

const iso = (ms: number | null): string | null => (ms === null ? null : new Date(ms).toISOString());

export function alertReminderView(row: AlertReminderRow): Record<string, unknown> {
  return {
    key: row.key,
    channel: row.channel,
    thread_ts: row.thread_ts,
    text: row.text,
    interval_s: row.interval_s,
    next_at: iso(row.next_at),
    until_at: iso(row.until_at),
    stop_obligation_id: row.stop_obligation_id,
    status: row.status,
    stop_reason: row.stop_reason,
    post_count: row.post_count,
    last_post_ts: row.last_post_ts,
    last_posted_at: iso(row.last_posted_at),
    last_error: row.last_error,
    fail_count: row.fail_count,
    retry_at: iso(row.retry_at),
    created_by: row.created_by,
    source_ref: row.source_ref,
    created_at: iso(row.created_at),
    updated_at: iso(row.updated_at),
    claim_gen: row.claim_gen,
    last_outcome: row.last_outcome,
    unknown_count: row.unknown_count,
    last_unknown_at: iso(row.last_unknown_at),
    last_unknown_reason: row.last_unknown_reason,
  };
}

// ─── Store ───────────────────────────────────────────────

export interface ClaimedReminder {
  /** The row exactly as it was when claimed: this is the binding the send is validated against. */
  row: AlertReminderRow;
  /** `claim_gen` written by the claim; every send check and completion write is fenced to it. */
  generation: number;
  /** next_at before the claim; restored only when the post is a proven zero-effect failure. */
  previousNextAt: number;
  /** next_at written by the claim (the following grid point). */
  claimedNextAt: number;
}

export type ClaimStaleReason = "row_missing" | "claim_superseded" | "binding_changed" | "not_active" | "past_cutoff";

export type ClaimCheck = { ok: true; row: AlertReminderRow } | { ok: false; reason: ClaimStaleReason };

export class AlertReminderStore {
  constructor(private readonly db: Database.Database) {}

  get(key: string): AlertReminderRow | null {
    return (this.db.prepare("SELECT * FROM alert_reminders WHERE key = ?").get(key) as AlertReminderRow | undefined) ?? null;
  }

  list(status: AlertReminderStatus | "all" = "active"): AlertReminderRow[] {
    if (status === "all") {
      return this.db.prepare("SELECT * FROM alert_reminders ORDER BY created_at DESC, key ASC").all() as AlertReminderRow[];
    }
    return this.db
      .prepare("SELECT * FROM alert_reminders WHERE status = ? ORDER BY next_at ASC, key ASC")
      .all(status) as AlertReminderRow[];
  }

  /**
   * Idempotent on key. Same args on an active row is a no-op; different args are refused unless
   * `replace`; an inactive row (stopped or expired) is revived as a fresh schedule.
   * `verifyObligation` runs only when a write is about to happen.
   *
   * A replace or revive bumps `claim_gen`, so any claim still in flight for the old binding is
   * stale: it is not sent if it has not gone out yet, and its completion writes match no row.
   * `unknown_count` / `last_unknown_*` are kept: an occurrence whose delivery is already unknown
   * stays on record for the key and is never replayed.
   */
  add(
    input: AlertReminderAddInput,
    nowMs: number,
    verifyObligation?: (id: number) => { error: string; status: 400 | 404 | 503 } | null,
  ): AlertReminderAddResult {
    const run = this.db.transaction((): AlertReminderAddResult => {
      const existing = this.get(input.key);
      let outcome: AlertReminderAddOutcome = "created";
      if (existing && existing.status === "active") {
        const differs = differingArgs(existing, input);
        if (differs.length === 0) return { ok: true, outcome: "noop", row: existing };
        if (!input.replace) {
          return { ok: false, status: 409, error: "key_exists_with_different_args", differs, row: existing };
        }
        outcome = "replaced";
      } else if (existing) {
        outcome = "revived";
      }
      if (input.stop_obligation_id !== null && verifyObligation) {
        const failure = verifyObligation(input.stop_obligation_id);
        if (failure) return { ok: false, status: failure.status, error: failure.error };
      }
      this.db
        .prepare(`
          INSERT INTO alert_reminders
            (key, channel, thread_ts, text, interval_s, next_at, until_at, stop_obligation_id,
             status, stop_reason, post_count, last_post_ts, last_posted_at, last_error,
             fail_count, retry_at, created_by, source_ref, created_at, updated_at)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, 0, NULL, NULL, NULL, 0, NULL, ?, ?, ?, ?)
          ON CONFLICT(key) DO UPDATE SET
            channel = excluded.channel, thread_ts = excluded.thread_ts, text = excluded.text,
            interval_s = excluded.interval_s, next_at = excluded.next_at, until_at = excluded.until_at,
            stop_obligation_id = excluded.stop_obligation_id, status = 'active', stop_reason = NULL,
            post_count = 0, last_post_ts = NULL, last_posted_at = NULL, last_error = NULL,
            fail_count = 0, retry_at = NULL, created_by = excluded.created_by,
            source_ref = excluded.source_ref, created_at = excluded.created_at,
            updated_at = excluded.updated_at,
            claim_gen = alert_reminders.claim_gen + 1, last_outcome = NULL
        `)
        .run(
          input.key, input.channel, input.thread_ts, input.text, input.interval_s, input.first_at,
          input.until_at, input.stop_obligation_id, input.created_by, input.source_ref, nowMs, nowMs,
        );
      return { ok: true, outcome, row: this.get(input.key) as AlertReminderRow };
    });
    return run();
  }

  /** Stop an active reminder. Returns the row, or null when the key is unknown. */
  stop(key: string, reason: string, nowMs: number): { row: AlertReminderRow; changed: boolean } | null {
    const result = this.db
      .prepare("UPDATE alert_reminders SET status = 'stopped', stop_reason = ?, updated_at = ? WHERE key = ? AND status = 'active'")
      .run(reason, nowMs, key);
    const row = this.get(key);
    return row ? { row, changed: result.changes > 0 } : null;
  }

  /**
   * Mark rows that can never post again as expired: past `until_at`, or whose next grid point
   * already lies beyond `until_at`. Returns the keys that were expired.
   */
  expireStale(nowMs: number): string[] {
    const rows = this.db
      .prepare("SELECT key FROM alert_reminders WHERE status = 'active' AND (until_at < ? OR next_at > until_at)")
      .all(nowMs) as Array<{ key: string }>;
    const update = this.db.prepare(
      "UPDATE alert_reminders SET status = 'expired', stop_reason = ?, updated_at = ? WHERE key = ? AND status = 'active'",
    );
    for (const { key } of rows) {
      const row = this.get(key) as AlertReminderRow;
      update.run(row.until_at < nowMs ? "until_passed" : "schedule_complete", nowMs, key);
    }
    return rows.map((row) => row.key);
  }

  activeWithObligation(): AlertReminderRow[] {
    return this.db
      .prepare("SELECT * FROM alert_reminders WHERE status = 'active' AND stop_obligation_id IS NOT NULL")
      .all() as AlertReminderRow[];
  }

  /**
   * Atomically take every due active row and advance it to the next whole-interval grid point
   * strictly after `nowMs`. At most one post per row per claim, whatever the downtime, which is
   * what prevents a burst of catch-up posts. A crash after the claim loses that one post
   * (at-most-once) instead of duplicating it.
   */
  claimDue(nowMs: number, limit: number): ClaimedReminder[] {
    const run = this.db.transaction((): ClaimedReminder[] => {
      const due = this.db
        .prepare(`
          SELECT * FROM alert_reminders
          WHERE status = 'active' AND next_at <= ? AND until_at >= ?
            AND (retry_at IS NULL OR retry_at <= ?)
          ORDER BY next_at ASC, key ASC LIMIT ?
        `)
        .all(nowMs, nowMs, nowMs, limit) as AlertReminderRow[];
      const advance = this.db.prepare(
        "UPDATE alert_reminders SET next_at = ?, claim_gen = claim_gen + 1, updated_at = ? WHERE key = ?",
      );
      return due.map((row) => {
        const claimedNextAt = advanceByWholeIntervals(row.next_at, row.interval_s * 1000, nowMs);
        advance.run(claimedNextAt, nowMs, row.key);
        return { row, generation: row.claim_gen + 1, previousNextAt: row.next_at, claimedNextAt };
      });
    });
    return run();
  }

  /**
   * Revalidate a claim immediately before its send: the row must still be the exact claim (same
   * generation and binding), active, and not past `until_at`. The stop obligation is checked by
   * the scheduler right after this, with no await in between.
   */
  checkClaim(claim: ClaimedReminder, nowMs: number): ClaimCheck {
    const current = this.get(claim.row.key);
    if (!current) return { ok: false, reason: "row_missing" };
    if (current.claim_gen !== claim.generation) return { ok: false, reason: "claim_superseded" };
    const bound = claim.row;
    if (
      current.channel !== bound.channel ||
      current.thread_ts !== bound.thread_ts ||
      current.text !== bound.text ||
      current.stop_obligation_id !== bound.stop_obligation_id
    ) {
      return { ok: false, reason: "binding_changed" };
    }
    if (current.status !== "active") return { ok: false, reason: "not_active" };
    if (nowMs > current.until_at) return { ok: false, reason: "past_cutoff" };
    return { ok: true, row: current };
  }

  /** Stop a row on behalf of one claim; a replaced or re-claimed row is left alone. */
  stopClaimed(claim: ClaimedReminder, reason: string, nowMs: number): boolean {
    return (
      this.db
        .prepare(
          "UPDATE alert_reminders SET status = 'stopped', stop_reason = ?, updated_at = ? WHERE key = ? AND claim_gen = ? AND status = 'active'",
        )
        .run(reason, nowMs, claim.row.key, claim.generation).changes > 0
    );
  }

  /**
   * Record a delivered post. Fenced to the claim: when the row was replaced or re-claimed in the
   * meantime nothing is written and false is returned, so a stale completion cannot clear errors
   * or attach its receipt to a replacement.
   */
  recordPost(claim: ClaimedReminder, slackTs: string, nowMs: number): boolean {
    const result = this.db
      .prepare(`
        UPDATE alert_reminders
        SET post_count = post_count + 1, last_post_ts = ?, last_posted_at = ?, last_error = NULL,
            last_outcome = 'posted', fail_count = 0, retry_at = NULL, updated_at = ?
        WHERE key = ? AND claim_gen = ?
      `)
      .run(slackTs, nowMs, nowMs, claim.row.key, claim.generation);
    if (result.changes === 0) return false;
    this.completeIfScheduleDone(claim, nowMs);
    return true;
  }

  /**
   * Record a failure that is KNOWN to have had no effect on Slack: restore the due time (only if
   * the row is still exactly where this claim left it) and back off. Fenced to the claim. Never
   * used for an ambiguous outcome; see recordUnknown.
   */
  recordFailure(claim: ClaimedReminder, error: string, nowMs: number): boolean {
    const failCountBefore = claim.row.fail_count;
    const restored = this.db
      .prepare(`
        UPDATE alert_reminders
        SET next_at = ?, last_error = ?, last_outcome = 'failed', fail_count = fail_count + 1,
            retry_at = ?, updated_at = ?
        WHERE key = ? AND claim_gen = ? AND status = 'active' AND next_at = ?
      `)
      .run(claim.previousNextAt, error, nowMs + retryBackoffMs(failCountBefore), nowMs, claim.row.key, claim.generation, claim.claimedNextAt);
    // Keep the error visible even if the row was cancelled while the post was in flight.
    const visible = this.db
      .prepare("UPDATE alert_reminders SET last_error = ?, last_outcome = 'failed', updated_at = ? WHERE key = ? AND claim_gen = ? AND status != 'active'")
      .run(error, nowMs, claim.row.key, claim.generation);
    return restored.changes + visible.changes > 0;
  }

  /**
   * Record an occurrence whose delivery is UNKNOWN (timeout, lost response, unparseable reply,
   * 5xx, unlisted Slack error, a throwing poster). The occurrence stays consumed: `next_at` keeps
   * the value the claim wrote, no retry is scheduled, and the row shows the uncertainty. Fenced
   * to the claim.
   */
  recordUnknown(claim: ClaimedReminder, reason: string, nowMs: number): boolean {
    const result = this.db
      .prepare(`
        UPDATE alert_reminders
        SET last_outcome = 'unknown', last_error = ?, unknown_count = unknown_count + 1,
            last_unknown_at = ?, last_unknown_reason = ?, fail_count = 0, retry_at = NULL, updated_at = ?
        WHERE key = ? AND claim_gen = ?
      `)
      .run(reason, nowMs, reason, nowMs, claim.row.key, claim.generation);
    if (result.changes === 0) return false;
    this.completeIfScheduleDone(claim, nowMs);
    return true;
  }

  /** The final scheduled occurrence has been consumed when the following grid point is past until_at. */
  private completeIfScheduleDone(claim: ClaimedReminder, nowMs: number): void {
    this.db
      .prepare(`
        UPDATE alert_reminders SET status = 'expired', stop_reason = 'schedule_complete', updated_at = ?
        WHERE key = ? AND claim_gen = ? AND status = 'active' AND next_at > until_at
      `)
      .run(nowMs, claim.row.key, claim.generation);
  }
}

// ─── Slack transport (Alerts bot) ────────────────────────

/**
 * Outcome of one chat.postMessage attempt.
 * `effect: "none"`: the attempt is KNOWN to have posted nothing, so retrying cannot duplicate it.
 * `effect: "unknown"`: the message may or may not have been posted; the occurrence must never be
 * retried. When in doubt a failure is "unknown".
 */
export type AlertPostResult = { ok: true; ts: string } | { ok: false; error: string; effect: "none" | "unknown" };

/**
 * Slack `ok:false` errors that are returned before anything is posted (auth, channel, rate limit,
 * request-shape rejections). Anything not listed is treated as unknown, in particular
 * internal/server-side errors such as fatal_error, internal_error, service_unavailable and
 * request_timeout, which can occur after the message was accepted.
 */
const ZERO_EFFECT_SLACK_ERRORS: ReadonlySet<string> = new Set([
  "not_in_channel",
  "channel_not_found",
  "is_archived",
  "invalid_auth",
  "not_authed",
  "account_inactive",
  "token_revoked",
  "token_expired",
  "missing_scope",
  "access_denied",
  "ratelimited",
  "rate_limited",
  "msg_too_long",
  "no_text",
  "restricted_action",
  "invalid_arguments",
  "invalid_arg_name",
  "invalid_charset",
  "invalid_form_data",
]);

/** A fetch throw cannot prove whether Slack accepted the request. */
export function classifyTransportError(error: unknown): { error: string; effect: "unknown" } {
  const name = error instanceof Error ? error.name : "error";
  return { error: `slack_transport_${String(name).slice(0, 40)}`, effect: "unknown" };
}

export interface AlertPoster {
  post(channel: string, threadTs: string, text: string): Promise<AlertPostResult>;
  tokenConfigured(): boolean;
}

/** Read SLACK_ALERTS_BOT_TOKEN from an env-style file. The value is returned, never logged. */
export function readAlertsToken(envFile: string): string | null {
  let contents: string;
  try {
    contents = readFileSync(envFile, "utf8");
  } catch {
    return null;
  }
  for (const line of contents.split("\n")) {
    if (!line.startsWith(`${TOKEN_ENV_KEY}=`)) continue;
    const value = line.slice(TOKEN_ENV_KEY.length + 1).trim().replace(/^["']|["']$/g, "");
    return value || null;
  }
  return null;
}

export class SlackAlertsPoster implements AlertPoster {
  constructor(
    private readonly envFile: string = process.env.MOP_ALERTS_ENV_FILE ?? DEFAULT_ENV_FILE,
    private readonly fetchImpl: typeof fetch = fetch,
  ) {}

  tokenConfigured(): boolean {
    return readAlertsToken(this.envFile) !== null;
  }

  async post(channel: string, threadTs: string, text: string): Promise<AlertPostResult> {
    const token = readAlertsToken(this.envFile);
    if (!token) return { ok: false, error: "alerts_token_missing", effect: "none" };
    let response: Response;
    try {
      response = await this.fetchImpl("https://slack.com/api/chat.postMessage", {
        method: "POST",
        redirect: "manual",
        headers: { "Content-Type": "application/json; charset=utf-8", Authorization: `Bearer ${token}` },
        body: JSON.stringify({ channel, thread_ts: threadTs, text, unfurl_links: false, unfurl_media: false }),
        signal: AbortSignal.timeout(15_000),
      });
    } catch (error) {
      return { ok: false, ...classifyTransportError(error) };
    }
    // 429 is answered by Slack's edge before the request is processed.
    if (response.status === 429) return { ok: false, error: "ratelimited", effect: "none" };
    // Only a first-hop 200 has Slack's documented application response semantics.
    if (response.status !== 200) return { ok: false, error: `slack_http_${response.status}`, effect: "unknown" };
    type Payload = { ok?: boolean; ts?: string; error?: string } | null;
    let payload: Payload;
    try {
      payload = (await response.json()) as Payload;
    } catch {
      return { ok: false, error: "slack_response_unparseable", effect: "unknown" };
    }
    if (payload?.ok === true) {
      if (typeof payload.ts === "string") return { ok: true, ts: payload.ts };
      return { ok: false, error: "slack_ok_without_ts", effect: "unknown" };
    }
    const slackError = String(payload?.error ?? `http_${response.status}`).slice(0, 80);
    const known = payload?.ok === false && ZERO_EFFECT_SLACK_ERRORS.has(slackError);
    return { ok: false, error: `slack_${slackError}`, effect: known ? "none" : "unknown" };
  }
}

// ─── pm-ops obligation lookup (read-only) ────────────────

export type ObligationState = "open" | "closed" | "missing" | "unknown";

export function lookupObligationState(
  id: number,
  dbPath: string = process.env.MOP_PM_OPS_DB ?? DEFAULT_PM_OPS_DB,
): ObligationState {
  try {
    const pmOps = new Database(dbPath, { readonly: true, fileMustExist: true });
    try {
      const row = pmOps.prepare("SELECT status FROM obligations WHERE id = ?").get(id) as { status?: string } | undefined;
      if (!row) return "missing";
      return row.status === "open" || row.status === "in_progress" ? "open" : "closed";
    } finally {
      pmOps.close();
    }
  } catch {
    return "unknown";
  }
}

// ─── Scheduler ───────────────────────────────────────────

export interface AlertReminderTickResult {
  expired: string[];
  stopped: string[];
  claimed: number;
  posted: string[];
  /** Known zero-effect failures: due time restored, retried after a backoff. */
  failed: string[];
  /** Delivery unknown (may have posted): occurrence consumed, never retried. */
  unknown: string[];
  /** Claimed but not sent: cancelled, replaced, expired or obligation closed before the send. */
  skipped: string[];
  /** Sent, but the row was replaced or re-claimed meanwhile, so the completion was discarded. */
  stale: string[];
}

export interface AlertReminderSchedulerDeps {
  store: AlertReminderStore;
  poster: AlertPoster;
  obligationState?: (id: number) => ObligationState;
  logEvent?: (type: string, payload: Record<string, unknown>) => void;
  now?: () => number;
  tickMs?: number;
  bootDelayMs?: number;
  maxPostsPerTick?: number;
}

export class AlertReminderScheduler {
  private timer: NodeJS.Timeout | null = null;
  private bootTimer: NodeJS.Timeout | null = null;
  private ticking = false;
  private lastTickAt: string | null = null;
  private lastTick: AlertReminderTickResult | null = null;
  private readonly tickMs: number;
  private readonly bootDelayMs: number;
  private readonly maxPostsPerTick: number;

  constructor(private readonly deps: AlertReminderSchedulerDeps) {
    this.tickMs = deps.tickMs ?? parseInt(process.env.MOP_ALERT_REMINDER_TICK_MS ?? "30000", 10);
    this.bootDelayMs = deps.bootDelayMs ?? parseInt(process.env.MOP_ALERT_REMINDER_BOOT_DELAY_MS ?? "20000", 10);
    this.maxPostsPerTick = deps.maxPostsPerTick ?? 10;
  }

  start(): void {
    if (this.timer) return;
    this.bootTimer = setTimeout(() => void this.tick(), this.bootDelayMs);
    this.timer = setInterval(() => void this.tick(), this.tickMs);
    console.log(`[alert-reminders] scheduler started tick=${this.tickMs}ms token_configured=${this.deps.poster.tokenConfigured()}`);
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    if (this.bootTimer) clearTimeout(this.bootTimer);
    this.timer = null;
    this.bootTimer = null;
  }

  getStatus(): Record<string, unknown> {
    return {
      running: this.timer !== null,
      ticking: this.ticking,
      tick_ms: this.tickMs,
      token_configured: this.deps.poster.tokenConfigured(),
      last_tick_at: this.lastTickAt,
      last_tick: this.lastTick,
    };
  }

  async tick(): Promise<AlertReminderTickResult> {
    const result: AlertReminderTickResult = {
      expired: [], stopped: [], claimed: 0, posted: [], failed: [], unknown: [], skipped: [], stale: [],
    };
    if (this.ticking) return result;
    this.ticking = true;
    const { store, poster } = this.deps;
    const now = this.deps.now ?? Date.now;
    const log = this.deps.logEvent ?? (() => undefined);
    try {
      const startMs = now();
      result.expired = store.expireStale(startMs);

      if (this.deps.obligationState) {
        for (const row of store.activeWithObligation()) {
          if (this.deps.obligationState(row.stop_obligation_id as number) !== "closed") continue;
          const stopped = store.stop(row.key, "obligation_resolved", startMs);
          if (stopped?.changed) {
            result.stopped.push(row.key);
            log("alert_reminder_stopped", { key: row.key, reason: "obligation_resolved", obligation_id: row.stop_obligation_id });
          }
        }
      }
      for (const key of result.expired) log("alert_reminder_expired", { key });

      const claims = store.claimDue(startMs, this.maxPostsPerTick);
      result.claimed = claims.length;
      for (const claim of claims) {
        const { row } = claim;
        const base = { key: row.key, channel: row.channel, thread_ts: row.thread_ts, generation: claim.generation };

        // The batch is claimed up front and sent one by one, so by now the row may have been
        // cancelled, replaced, expired or lost its stop obligation. Revalidate the exact claim
        // immediately before the send. Nothing awaits between this check and the post call below,
        // so nothing in this process can interleave. A skipped claim stays consumed: no restore.
        const sendMs = now();
        const gate = store.checkClaim(claim, sendMs);
        if (!gate.ok) {
          result.skipped.push(row.key);
          log("alert_reminder_send_skipped", { ...base, reason: gate.reason });
          continue;
        }
        if (
          this.deps.obligationState &&
          row.stop_obligation_id !== null &&
          this.deps.obligationState(row.stop_obligation_id) === "closed"
        ) {
          if (store.stopClaimed(claim, "obligation_resolved", sendMs)) {
            result.stopped.push(row.key);
            log("alert_reminder_stopped", { key: row.key, reason: "obligation_resolved", obligation_id: row.stop_obligation_id });
          }
          result.skipped.push(row.key);
          log("alert_reminder_send_skipped", { ...base, reason: "obligation_resolved" });
          continue;
        }

        let outcome: AlertPostResult;
        try {
          outcome = await poster.post(row.channel, row.thread_ts, row.text);
        } catch {
          // A throw can happen after the request left: the delivery is unknown, never a retry.
          outcome = { ok: false, error: "poster_threw", effect: "unknown" };
        }
        const doneMs = now();

        if (outcome.ok) {
          if (store.recordPost(claim, outcome.ts, doneMs)) {
            result.posted.push(row.key);
            log("alert_reminder_posted", { ...base, slack_ts: outcome.ts, next_at: new Date(claim.claimedNextAt).toISOString() });
          } else {
            result.stale.push(row.key);
            log("alert_reminder_stale_completion", { ...base, outcome: "posted", slack_ts: outcome.ts });
          }
        } else if (outcome.effect === "none") {
          if (store.recordFailure(claim, outcome.error, doneMs)) {
            result.failed.push(row.key);
            log("alert_reminder_post_failed", { ...base, error: outcome.error });
          } else {
            result.stale.push(row.key);
            log("alert_reminder_stale_completion", { ...base, outcome: "failed", error: outcome.error });
          }
        } else if (store.recordUnknown(claim, outcome.error, doneMs)) {
          result.unknown.push(row.key);
          log("alert_reminder_post_unknown", { ...base, reason: outcome.error });
        } else {
          result.stale.push(row.key);
          log("alert_reminder_stale_completion", { ...base, outcome: "unknown", reason: outcome.error });
        }
      }
    } catch (error) {
      // A tick must never take down the MoP loop.
      log("alert_reminder_tick_error", { error: error instanceof Error ? error.message.slice(0, 200) : String(error).slice(0, 200) });
    } finally {
      this.ticking = false;
      this.lastTickAt = new Date((this.deps.now ?? Date.now)()).toISOString();
      this.lastTick = result;
    }
    return result;
  }
}

// ─── REST routes ─────────────────────────────────────────

export interface AlertReminderRouteDeps {
  store: AlertReminderStore;
  scheduler: AlertReminderScheduler;
  obligationState?: (id: number) => ObligationState;
  now?: () => number;
}

export function registerAlertReminderRoutes(app: Hono, deps: AlertReminderRouteDeps): void {
  const now = deps.now ?? Date.now;
  const lookup = deps.obligationState ?? ((id: number) => lookupObligationState(id));

  app.post("/alert-reminders", async (c) => {
    let body: Record<string, unknown>;
    try {
      body = (await c.req.json()) as Record<string, unknown>;
    } catch {
      return c.json({ success: false, error: "Body must be JSON" }, 400);
    }
    const nowMs = now();
    const normalized = normalizeAddRequest(body && typeof body === "object" ? body : {}, nowMs);
    if (!normalized.ok) return c.json({ success: false, error: normalized.error }, 400);
    const result = deps.store.add(normalized.value, nowMs, (id) => {
      const state = lookup(id);
      if (state === "open") return null;
      if (state === "closed") return { error: "stop_obligation_not_open", status: 400 };
      if (state === "missing") return { error: "stop_obligation_not_found", status: 404 };
      return { error: "stop_obligation_lookup_failed", status: 503 };
    });
    if (!result.ok) {
      return c.json(
        { success: false, error: result.error, differs: result.differs, reminder: result.row ? alertReminderView(result.row) : undefined },
        result.status,
      );
    }
    return c.json({ success: true, outcome: result.outcome, reminder: alertReminderView(result.row) });
  });

  app.get("/alert-reminders", (c) => {
    const requested = c.req.query("status") ?? "active";
    if (requested !== "active" && requested !== "stopped" && requested !== "expired" && requested !== "all") {
      return c.json({ success: false, error: "status must be active, stopped, expired or all" }, 400);
    }
    return c.json({
      success: true,
      reminders: deps.store.list(requested).map(alertReminderView),
      scheduler: deps.scheduler.getStatus(),
    });
  });

  app.get("/alert-reminders/:key", (c) => {
    const row = deps.store.get(c.req.param("key"));
    if (!row) return c.json({ success: false, error: "reminder not found" }, 404);
    return c.json({ success: true, reminder: alertReminderView(row) });
  });

  app.post("/alert-reminders/:key/cancel", (c) => {
    const result = deps.store.stop(c.req.param("key"), "cancelled", now());
    if (!result) return c.json({ success: false, error: "reminder not found" }, 404);
    return c.json({
      success: true,
      outcome: result.changed ? "cancelled" : `already_${result.row.status}`,
      reminder: alertReminderView(result.row),
    });
  });
}
