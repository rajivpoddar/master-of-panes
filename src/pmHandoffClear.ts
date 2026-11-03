/**
 * PM save-handoff-then-clear (Rajiv, DM D0AMF0XE6TS thread 1793731816.594829,
 * ts 1793732417.459339: "add the obligation to save memory and then
 * automatically inject clear at next stop").
 *
 * A due PM clear (POST /slots/0/clear or the session-age policy) does NOT
 * inject. MoP upserts ONE pm-ops `pm_save_handoff` obligation, surfaced by the
 * existing PM Stop obligations validator. It resolves when BOTH handoff files
 * have an mtime later than the request (or on an explicit resolve). At the NEXT
 * PM Stop after that, MoP arms the existing Stop-fenced /clear writer, so new
 * input or a session change cancels the write (Rajiv procedure, thread
 * 1793283423.083209). There is no idle timer.
 *
 * Rule 50: nothing here gates, holds or delays PM input or tool calls, and the
 * UserPromptSubmit chain is untouched. A clear that is written once but never
 * observed fails open after one bounded attempt and is never auto re-requested
 * for the same session.
 */
import { execFile } from "node:child_process";
import { statSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import Database from "better-sqlite3";

import type { MoPDatabase } from "./db.js";

const run = promisify(execFile);

export const PM_HANDOFF_REQUESTED_AT_KEY = "pm_handoff_requested_at";
export const PM_HANDOFF_SESSION_KEY = "pm_handoff_session";
export const PM_HANDOFF_SOURCE_KEY = "pm_handoff_source";
export const PM_HANDOFF_OBLIGATION_KEY = "pm_handoff_obligation_id";
export const PM_CLEAR_FAILOPEN_SESSION_KEY = "pm_clear_failopen_session";
export const PM_HANDOFF_OBLIGATION_KIND = "pm_save_handoff";
const PM_TARGET_ID = "pm-dhruva";

/** One bounded attempt: a Stop this long after the write with no SessionStart:clear fails open. */
export const PM_CLEAR_UNOBSERVED_FAIL_OPEN_MS = parseInt(
  process.env.MOP_PM_CLEAR_UNOBSERVED_FAIL_OPEN_MS ?? `${2 * 60 * 1000}`,
  10,
);

export const PM_HANDOFF_FILES: readonly string[] = process.env.MOP_PM_HANDOFF_FILES
  ? process.env.MOP_PM_HANDOFF_FILES.split(":").filter(Boolean)
  : [
    join(homedir(), ".claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/memory/MEMORY-session-handoff-current.md"),
    "/Users/rajiv/Downloads/projects/heydonna-app/data/pm-continuation-note.md",
  ];

const PM_OPS_DB = process.env.MOP_PM_OPS_DB
  ?? join(homedir(), ".claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db");

export const PM_HANDOFF_ACTION =
  "Save the PM handoff now: update MEMORY-session-handoff-current.md "
  + "(~/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/memory/) and "
  + "data/pm-continuation-note.md. MoP then injects /clear at the next PM Stop automatically; "
  + "do not POST another clear.";

type Db = Pick<MoPDatabase, "getConfig" | "setConfig" | "logEvent" | "hasPendingClear" | "clearPendingClear">;

export type HandoffObligationOps = {
  upsert: (evidence: Record<string, unknown>) => Promise<string | null>;
  resolve: (reason: string) => Promise<boolean>;
  /** True only when the obligation row exists and is no longer open. */
  isExplicitlyResolved: (obligationId: string) => boolean;
};

export type HandoffRequest =
  | { kind: "awaiting_handoff"; requestedAt: string; obligationId: string | null }
  | { kind: "already_awaiting"; requestedAt: string }
  | { kind: "refused_failopen"; sessionId: string };

function parseMs(value: string | null | undefined): number | null {
  if (!value) return null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
}

export function isPmHandoffPending(db: Pick<MoPDatabase, "getConfig">): boolean {
  return parseMs(db.getConfig(PM_HANDOFF_REQUESTED_AT_KEY)) !== null;
}

function clearHandoff(db: Db): void {
  db.setConfig(PM_HANDOFF_REQUESTED_AT_KEY, "");
  db.setConfig(PM_HANDOFF_SESSION_KEY, "");
  db.setConfig(PM_HANDOFF_SOURCE_KEY, "");
  db.setConfig(PM_HANDOFF_OBLIGATION_KEY, "");
}

/** Record a due PM clear as a save-handoff obligation. Never injects. */
export async function requestPmHandoffClear(db: Db, options: {
  source: string;
  sessionId: string;
  nowMs: number;
  automatic: boolean;
  ops: HandoffObligationOps;
}): Promise<HandoffRequest> {
  if (options.automatic && options.sessionId
      && db.getConfig(PM_CLEAR_FAILOPEN_SESSION_KEY) === options.sessionId) {
    db.logEvent(0, "pm_handoff_clear_refused_failopen", null, null, {
      source: options.source, session_id: options.sessionId,
      reason: "A clear for this session already failed open; automatic re-requests are suppressed",
    });
    return { kind: "refused_failopen", sessionId: options.sessionId };
  }
  const existing = db.getConfig(PM_HANDOFF_REQUESTED_AT_KEY);
  if (parseMs(existing) !== null) return { kind: "already_awaiting", requestedAt: existing! };
  const requestedAt = new Date(options.nowMs).toISOString();
  db.setConfig(PM_HANDOFF_REQUESTED_AT_KEY, requestedAt);
  db.setConfig(PM_HANDOFF_SESSION_KEY, options.sessionId);
  db.setConfig(PM_HANDOFF_SOURCE_KEY, options.source);
  let obligationId: string | null = null;
  try {
    obligationId = await options.ops.upsert({
      producer: "mop-pm-handoff-clear", pm_stop_actionable: 1, requested_at: requestedAt,
      source: options.source, session_id: options.sessionId, files: PM_HANDOFF_FILES,
    });
  } catch {
    obligationId = null;
  }
  db.setConfig(PM_HANDOFF_OBLIGATION_KEY, obligationId ?? "");
  db.logEvent(0, "pm_handoff_clear_requested", null, null, {
    requested_at: requestedAt, source: options.source, session_id: options.sessionId,
    obligation_id: obligationId,
    reason: "PM clear due: save handoff first; /clear is injected at the next PM Stop after the save",
  });
  return { kind: "awaiting_handoff", requestedAt, obligationId };
}

/** Both handoff files have an mtime strictly later than the request. */
export function handoffFilesSaved(requestedAtMs: number, files: readonly string[] = PM_HANDOFF_FILES): boolean {
  if (files.length === 0) return false;
  return files.every((path) => {
    try { return statSync(path).mtimeMs > requestedAtMs; } catch { return false; }
  });
}

export type HandoffStopOutcome =
  | { kind: "none" }
  | { kind: "waiting" }
  | { kind: "cancelled_session_change" }
  | { kind: "ready"; via: "mtime" | "explicit_resolve"; requestedAt: string };

/**
 * Evaluate a pending handoff at a PM Stop. `ready` clears the handoff state;
 * the caller then arms the existing Stop-fenced clear latch for THIS Stop.
 */
export async function evaluatePmHandoffAtStop(db: Db, options: {
  currentSessionId: string;
  ops: HandoffObligationOps;
  files?: readonly string[];
}): Promise<HandoffStopOutcome> {
  const requestedAt = db.getConfig(PM_HANDOFF_REQUESTED_AT_KEY);
  const requestedMs = parseMs(requestedAt);
  if (requestedMs === null) return { kind: "none" };
  const session = db.getConfig(PM_HANDOFF_SESSION_KEY) ?? "";
  if (session && options.currentSessionId && session !== options.currentSessionId) {
    clearHandoff(db);
    // Fire-and-forget: the native Stop hook response is never held on pm-ops.
    void options.ops.resolve(`PM session changed (${session} -> ${options.currentSessionId}); handoff clear cancelled`)
      .catch(() => false);
    db.logEvent(0, "pm_handoff_clear_cancelled", "Stop", null, {
      requested_at: requestedAt, recorded_session: session, current_session: options.currentSessionId,
      reason: "session_change",
    });
    return { kind: "cancelled_session_change" };
  }
  const obligationId = db.getConfig(PM_HANDOFF_OBLIGATION_KEY) ?? "";
  let via: "mtime" | "explicit_resolve" | null = null;
  if (handoffFilesSaved(requestedMs, options.files ?? PM_HANDOFF_FILES)) via = "mtime";
  else if (obligationId && options.ops.isExplicitlyResolved(obligationId)) via = "explicit_resolve";
  if (!via) return { kind: "waiting" };
  clearHandoff(db);
  if (via === "mtime") {
    void options.ops.resolve(`PM handoff files saved after ${requestedAt}; MoP injects /clear at this Stop`)
      .catch(() => false);
  }
  db.logEvent(0, "pm_handoff_saved", "Stop", null, {
    requested_at: requestedAt, via, obligation_id: obligationId || null,
    reason: "Handoff saved; arming the Stop-fenced /clear for this PM Stop",
  });
  return { kind: "ready", via, requestedAt: requestedAt! };
}

/**
 * PM Stop entry point: when the handoff is saved, arm the existing
 * Stop-fenced clear latch (deferUntilStop, never a direct send) so THIS Stop
 * injects /clear; the writer's generation/session fence cancels it on new
 * input or a session change.
 */
export async function armPmClearAfterSavedHandoff(db: Db, options: {
  sessionId: string;
  ops: HandoffObligationOps;
  files?: readonly string[];
  requestClear: () => Promise<{ kind: string }>;
}): Promise<{ outcome: HandoffStopOutcome; delivery: string | null }> {
  if (!isPmHandoffPending(db)) return { outcome: { kind: "none" }, delivery: null };
  const outcome = await evaluatePmHandoffAtStop(db, {
    currentSessionId: options.sessionId, ops: options.ops, files: options.files,
  });
  if (outcome.kind !== "ready") return { outcome, delivery: null };
  const delivery = await options.requestClear();
  return { outcome, delivery: delivery.kind };
}

/**
 * Fail open a PM clear latch that was written once but never acknowledged by
 * SessionStart:clear. Bounded: one attempt, then the pending flag is cleared,
 * an audit row is written, and automatic re-requests for this session stop.
 */
export function failOpenUnobservedPmClear(db: Db, options: {
  nowMs: number;
  sessionId: string;
  boundMs?: number;
}): boolean {
  if (!db.hasPendingClear(0)) return false;
  const state = db.getConfig("pm_clear_delivery_state");
  if (state !== "awaiting_ack" && state !== "uncertain" && state !== "sending") return false;
  const attemptMs = parseMs(db.getConfig("pm_clear_stop_retry_at")) ?? parseMs(db.getConfig("pm_clear_requested_at"));
  if (attemptMs === null) return false;
  const requestedMs = parseMs(db.getConfig("pm_clear_requested_at"));
  const effectiveAttempt = requestedMs !== null ? Math.max(attemptMs, requestedMs) : attemptMs;
  if (options.nowMs - effectiveAttempt < (options.boundMs ?? PM_CLEAR_UNOBSERVED_FAIL_OPEN_MS)) return false;
  db.clearPendingClear(0);
  db.setConfig("pm_clear_delivery_state", "failed_open");
  if (options.sessionId) db.setConfig(PM_CLEAR_FAILOPEN_SESSION_KEY, options.sessionId);
  db.logEvent(0, "pm_clear_unobserved_fail_open", "Stop", null, {
    requested_at: db.getConfig("pm_clear_requested_at"),
    previous_state: state,
    session_id: options.sessionId || null,
    waited_ms: options.nowMs - effectiveAttempt,
    reason: "PM /clear was attempted once and SessionStart:clear was not observed; pending flag cleared, failing open (no automatic re-request)",
  });
  return true;
}

/** Default obligation ops through the canonical pm-ops.py writer. */
export function pmOpsHandoffObligationOps(
  execute: (file: string, args: string[], options: { timeout: number }) => Promise<{ stdout: string | Buffer }> = run,
  pmOpsDb: string = PM_OPS_DB,
): HandoffObligationOps {
  const pmOps = join(homedir(), ".claude/scripts/pm-ops.py");
  const target = ["--kind", PM_HANDOFF_OBLIGATION_KIND, "--target-type", "session", "--target-id", PM_TARGET_ID];
  return {
    async upsert(evidence) {
      const { stdout } = await execute("python3", [pmOps, "obligation-upsert", ...target,
        "--severity", "high", "--owner", "pm", "--horizon", "hourly",
        "--title", "Save PM handoff before MoP injects /clear",
        "--action", PM_HANDOFF_ACTION,
        "--dedupe-group", "pm_save_handoff:pm",
        "--evidence-json", JSON.stringify(evidence), "--print-id"], { timeout: 10_000 });
      const last = String(stdout).trim().split("\n").pop() ?? "";
      return /^\d+$/.test(last) ? last : null;
    },
    async resolve(reason) {
      try {
        await execute("python3", [pmOps, "obligation-resolve", ...target, "--reason", reason], { timeout: 10_000 });
        return true;
      } catch { return false; }
    },
    isExplicitlyResolved(obligationId) {
      let con: Database.Database | null = null;
      try {
        con = new Database(pmOpsDb, { readonly: true, fileMustExist: true });
        const row = con.prepare("SELECT status FROM obligations WHERE id = ?").get(Number(obligationId)) as
          { status?: string } | undefined;
        return !!row && row.status !== "open";
      } catch { return false; } finally { con?.close(); }
    },
  };
}
