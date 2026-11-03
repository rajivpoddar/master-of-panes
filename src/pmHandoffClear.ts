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
/** created | uncertain: whether the request's obligation row is known. */
export const PM_HANDOFF_OBLIGATION_STATE_KEY = "pm_handoff_obligation_state";
export const PM_HANDOFF_RECONCILE_ATTEMPTS_KEY = "pm_handoff_reconcile_attempts";
const PM_TARGET_ID = "pm-dhruva";
/** Bounded reconciliation: after this many unresolved attempts the request is abandoned (audited). */
export const PM_HANDOFF_MAX_RECONCILE_ATTEMPTS = 3;

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

/**
 * The obligation row is keyed by the ORIGINAL request: target_id is
 * `pm-dhruva:<requested_at>`. pm-ops upserts match open rows by kind+target,
 * so a successor request can never reuse (or be resolved through) an earlier
 * request's row, and completion is always by exact `--id` (CTO REVISE ts
 * 1793736515.197999, fix 1).
 */
export function handoffRequestKey(requestedAt: string): string {
  return `${PM_TARGET_ID}:${requestedAt}`;
}

export type HandoffRowLookup =
  | { status: "found"; id: string; open: boolean; requestedAt: string | null; sessionId: string | null }
  | { status: "absent" }
  | { status: "unknown"; error: string };

export type HandoffObligationOps = {
  /** Idempotent per request key (an existing open row for that key is reused). */
  upsert: (requestKey: string, evidence: Record<string, unknown>) => Promise<string | null>;
  /** Exact-row resolve: `--id` plus kind; never by shared kind/target. */
  resolveById: (id: string, reason: string) => Promise<boolean>;
  /** Read-only newest row for this exact request key. */
  lookup: (requestKey: string) => HandoffRowLookup;
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
  for (const key of [PM_HANDOFF_REQUESTED_AT_KEY, PM_HANDOFF_SESSION_KEY, PM_HANDOFF_SOURCE_KEY,
    PM_HANDOFF_OBLIGATION_KEY, PM_HANDOFF_OBLIGATION_STATE_KEY, PM_HANDOFF_RECONCILE_ATTEMPTS_KEY]) {
    db.setConfig(key, "");
  }
}

/** True only while `requestedAt` is still the live request. */
function isCurrentRequest(db: Db, requestedAt: string): boolean {
  return db.getConfig(PM_HANDOFF_REQUESTED_AT_KEY) === requestedAt;
}

/** Resolve a row only when the lookup proves it belongs to `requestedAt`. */
async function resolveOwnRow(db: Db, ops: HandoffObligationOps, requestedAt: string, knownId: string | null,
  reason: string): Promise<boolean> {
  let id = knownId;
  if (!id) {
    const found = ops.lookup(handoffRequestKey(requestedAt));
    if (found.status !== "found" || !found.open) return false;
    id = found.id;
  }
  return ops.resolveById(id, reason).catch(() => false);
}

/**
 * Single-flight per ORIGINAL request (CTO review ts 1793737660.035519): the
 * entry lives from before the upsert starts until it settles, and one
 * reconciliation at a time runs per request. While a writer is active, an
 * empty lookup proves nothing, so it never authorizes a retry or abandonment.
 * Process-local is sufficient: MoP's server is the only handoff writer.
 */
const handoffWritersInFlight = new Map<string, Promise<string | null>>();
const handoffReconcilesInFlight = new Map<string, Promise<ReconcileOutcome>>();

export function isPmHandoffWriterActive(requestedAt: string): boolean {
  return handoffWritersInFlight.has(requestedAt);
}

/**
 * One obligation write for `requestedAt`. The returned id is published ONLY if
 * that request is still current; a stale id resolves its own row and is dropped.
 */
function createHandoffObligation(db: Db, ops: HandoffObligationOps, requestedAt: string): Promise<string | null> {
  const active = handoffWritersInFlight.get(requestedAt);
  if (active) return active;
  const writer = createHandoffObligationOnce(db, ops, requestedAt)
    .finally(() => handoffWritersInFlight.delete(requestedAt));
  handoffWritersInFlight.set(requestedAt, writer);
  return writer;
}

async function createHandoffObligationOnce(db: Db, ops: HandoffObligationOps, requestedAt: string): Promise<string | null> {
  const evidence = {
    producer: "mop-pm-handoff-clear", pm_stop_actionable: 1, requested_at: requestedAt,
    source: db.getConfig(PM_HANDOFF_SOURCE_KEY) ?? "", session_id: db.getConfig(PM_HANDOFF_SESSION_KEY) ?? "",
    files: PM_HANDOFF_FILES,
  };
  let id: string | null = null;
  let error: string | null = null;
  try { id = await ops.upsert(handoffRequestKey(requestedAt), evidence); }
  catch (err) { error = String(err); }
  if (!isCurrentRequest(db, requestedAt)) {
    if (id) void resolveOwnRow(db, ops, requestedAt, id, `stale pm_save_handoff for ${requestedAt}; request superseded`);
    db.logEvent(0, "pm_handoff_obligation_stale_id_dropped", null, null, { requested_at: requestedAt, obligation_id: id });
    return null;
  }
  if (id) {
    db.setConfig(PM_HANDOFF_OBLIGATION_KEY, id);
    db.setConfig(PM_HANDOFF_OBLIGATION_STATE_KEY, "created");
    return id;
  }
  // Failed or lost-ACK write: never assume either outcome; reconcile by key.
  db.setConfig(PM_HANDOFF_OBLIGATION_STATE_KEY, "uncertain");
  db.logEvent(0, "pm_handoff_obligation_uncertain", null, null, {
    requested_at: requestedAt, error: error ?? "missing obligation id",
    reason: "Obligation write outcome unknown; reconciled by the request key, never blindly replayed",
  });
  return null;
}

/**
 * Recover an uncertain obligation write WITHOUT resetting requested_at.
 * found (lost ACK) -> adopt that row; absent (proven not written) -> one more
 * keyed write; unknown -> count it. After the bound the request is abandoned
 * with an audit row, so no permanent phantom awaiting request remains.
 */
export type ReconcileOutcome = "none" | "created" | "adopted" | "retried" | "unknown" | "abandoned" | "writer_active";

export function reconcilePmHandoffObligation(db: Db, ops: HandoffObligationOps): Promise<ReconcileOutcome> {
  const requestedAt = db.getConfig(PM_HANDOFF_REQUESTED_AT_KEY);
  if (!requestedAt || parseMs(requestedAt) === null) return Promise.resolve("none");
  // The original (or a retry) writer has not settled: nothing to decide yet.
  if (handoffWritersInFlight.has(requestedAt)) return Promise.resolve("writer_active");
  const active = handoffReconcilesInFlight.get(requestedAt);
  if (active) return active;
  const run = reconcileOnce(db, ops, requestedAt).finally(() => handoffReconcilesInFlight.delete(requestedAt));
  handoffReconcilesInFlight.set(requestedAt, run);
  return run;
}

async function reconcileOnce(db: Db, ops: HandoffObligationOps, requestedAt: string): Promise<ReconcileOutcome> {
  if (!isCurrentRequest(db, requestedAt)) return "none";
  if (db.getConfig(PM_HANDOFF_OBLIGATION_STATE_KEY) === "created") return "created";
  const attempts = Number(db.getConfig(PM_HANDOFF_RECONCILE_ATTEMPTS_KEY) || "0") + 1;
  db.setConfig(PM_HANDOFF_RECONCILE_ATTEMPTS_KEY, String(attempts));
  const found = ops.lookup(handoffRequestKey(requestedAt));
  if (found.status === "found" && found.requestedAt === requestedAt) {
    db.setConfig(PM_HANDOFF_OBLIGATION_KEY, found.id);
    db.setConfig(PM_HANDOFF_OBLIGATION_STATE_KEY, "created");
    db.logEvent(0, "pm_handoff_obligation_reconciled", null, null, { requested_at: requestedAt, obligation_id: found.id, via: "lookup" });
    return "adopted";
  }
  if (attempts > PM_HANDOFF_MAX_RECONCILE_ATTEMPTS) {
    clearHandoff(db);
    db.logEvent(0, "pm_handoff_request_abandoned", null, null, {
      requested_at: requestedAt, attempts: attempts - 1, last_lookup: found.status,
      reason: "pm_save_handoff obligation could not be created or found; request dropped (fail open, no clear)",
    });
    return "abandoned";
  }
  if (found.status === "absent") {
    // Proven not written: a keyed write is safe (it cannot touch another request).
    const id = await createHandoffObligation(db, ops, requestedAt);
    return id ? "retried" : "unknown";
  }
  return "unknown";
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
  if (parseMs(existing) !== null) {
    if (db.getConfig(PM_HANDOFF_OBLIGATION_STATE_KEY) !== "created") {
      const outcome = await reconcilePmHandoffObligation(db, options.ops);
      if (outcome !== "abandoned") return { kind: "already_awaiting", requestedAt: existing! };
      // Abandoned: fall through to a fresh request.
    } else {
      return { kind: "already_awaiting", requestedAt: existing! };
    }
  }
  const requestedAt = new Date(options.nowMs).toISOString();
  db.setConfig(PM_HANDOFF_REQUESTED_AT_KEY, requestedAt);
  db.setConfig(PM_HANDOFF_SESSION_KEY, options.sessionId);
  db.setConfig(PM_HANDOFF_SOURCE_KEY, options.source);
  db.setConfig(PM_HANDOFF_OBLIGATION_KEY, "");
  db.setConfig(PM_HANDOFF_OBLIGATION_STATE_KEY, "uncertain");
  db.setConfig(PM_HANDOFF_RECONCILE_ATTEMPTS_KEY, "0");
  const obligationId = await createHandoffObligation(db, options.ops, requestedAt);
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
 * Synchronous evaluation of the CURRENT request at a PM Stop. It never
 * mutates handoff state except to cancel on a session change; `ready` is
 * consumed by the caller only after its pinned Stop authority is rechecked.
 * An explicit resolve counts only for the row that belongs to this request.
 */
export function evaluatePmHandoffAtStop(db: Db, options: {
  currentSessionId: string;
  ops: HandoffObligationOps;
  files?: readonly string[];
}): HandoffStopOutcome {
  const requestedAt = db.getConfig(PM_HANDOFF_REQUESTED_AT_KEY);
  const requestedMs = parseMs(requestedAt);
  if (requestedMs === null || !requestedAt) return { kind: "none" };
  const session = db.getConfig(PM_HANDOFF_SESSION_KEY) ?? "";
  if (session && options.currentSessionId && session !== options.currentSessionId) {
    const id = db.getConfig(PM_HANDOFF_OBLIGATION_KEY) || null;
    clearHandoff(db);
    void resolveOwnRow(db, options.ops, requestedAt, id,
      `PM session changed (${session} -> ${options.currentSessionId}); handoff clear cancelled`);
    db.logEvent(0, "pm_handoff_clear_cancelled", "Stop", null, {
      requested_at: requestedAt, recorded_session: session, current_session: options.currentSessionId,
      reason: "session_change",
    });
    return { kind: "cancelled_session_change" };
  }
  if (handoffFilesSaved(requestedMs, options.files ?? PM_HANDOFF_FILES)) return { kind: "ready", via: "mtime", requestedAt };
  const obligationId = db.getConfig(PM_HANDOFF_OBLIGATION_KEY) ?? "";
  if (obligationId && db.getConfig(PM_HANDOFF_OBLIGATION_STATE_KEY) === "created") {
    const row = options.ops.lookup(handoffRequestKey(requestedAt));
    if (row.status === "found" && row.id === obligationId && !row.open && row.requestedAt === requestedAt) {
      return { kind: "ready", via: "explicit_resolve", requestedAt };
    }
  }
  return { kind: "waiting" };
}

/**
 * PM Stop entry point. `isCurrent` is the Stop authority pinned BEFORE the
 * first await of this Stop; it is rechecked before any state change, so an
 * old Stop can never arm a clear for newer work (CTO fix 2). A ready handoff
 * arms the existing Stop-fenced latch (deferUntilStop, never a direct send).
 */
export async function armPmClearAfterSavedHandoff(db: Db, options: {
  sessionId: string;
  ops: HandoffObligationOps;
  files?: readonly string[];
  isCurrent?: () => boolean;
  requestClear: () => Promise<{ kind: string }>;
}): Promise<{ outcome: HandoffStopOutcome | { kind: "stale_stop" }; delivery: string | null }> {
  if (!isPmHandoffPending(db)) return { outcome: { kind: "none" }, delivery: null };
  const current = options.isCurrent ?? (() => true);
  if (!current()) return { outcome: { kind: "stale_stop" }, delivery: null };
  if (db.getConfig(PM_HANDOFF_OBLIGATION_STATE_KEY) !== "created") {
    void reconcilePmHandoffObligation(db, options.ops).catch(() => "unknown");
  }
  const outcome = evaluatePmHandoffAtStop(db, {
    currentSessionId: options.sessionId, ops: options.ops, files: options.files,
  });
  if (outcome.kind !== "ready") return { outcome, delivery: null };
  if (!current() || !isCurrentRequest(db, outcome.requestedAt)) return { outcome: { kind: "stale_stop" }, delivery: null };
  const id = db.getConfig(PM_HANDOFF_OBLIGATION_KEY) || null;
  // Consume THIS request synchronously, then arm; the delayed resolve is
  // bound to this request's own row id, never to a successor.
  clearHandoff(db);
  if (outcome.via === "mtime") {
    void resolveOwnRow(db, options.ops, outcome.requestedAt, id,
      `PM handoff files saved after ${outcome.requestedAt}; MoP injects /clear at this Stop`);
  }
  db.logEvent(0, "pm_handoff_saved", "Stop", null, {
    requested_at: outcome.requestedAt, via: outcome.via, obligation_id: id,
    reason: "Handoff saved; arming the Stop-fenced /clear for this PM Stop",
  });
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
  pmOps: string = join(homedir(), ".claude/scripts/pm-ops.py"),
): HandoffObligationOps {
  return {
    async upsert(requestKey, evidence) {
      const { stdout } = await execute("python3", [pmOps, "obligation-upsert",
        "--kind", PM_HANDOFF_OBLIGATION_KIND, "--target-type", "session", "--target-id", requestKey,
        "--severity", "high", "--owner", "pm", "--horizon", "hourly",
        "--title", "Save PM handoff before MoP injects /clear",
        "--action", PM_HANDOFF_ACTION,
        "--dedupe-group", "pm_save_handoff:pm",
        "--evidence-json", JSON.stringify(evidence), "--print-id"], { timeout: 10_000 });
      const last = String(stdout).trim().split("\n").pop() ?? "";
      return /^\d+$/.test(last) ? last : null;
    },
    async resolveById(id, reason) {
      try {
        await execute("python3", [pmOps, "obligation-resolve", "--kind", PM_HANDOFF_OBLIGATION_KIND,
          "--id", id, "--reason", reason], { timeout: 10_000 });
        return true;
      } catch { return false; }
    },
    lookup(requestKey) {
      let con: Database.Database | null = null;
      try {
        con = new Database(pmOpsDb, { readonly: true, fileMustExist: true });
        const row = con.prepare(
          "SELECT id, status, evidence_json FROM obligations WHERE kind = ? AND target_type = 'session' AND target_id = ? ORDER BY id DESC LIMIT 1",
        ).get(PM_HANDOFF_OBLIGATION_KIND, requestKey) as { id: number; status: string; evidence_json: string | null } | undefined;
        if (!row) return { status: "absent" };
        let evidence: Record<string, unknown> = {};
        try { evidence = JSON.parse(row.evidence_json || "{}"); } catch { /* evidence unreadable */ }
        return {
          status: "found", id: String(row.id), open: row.status === "open",
          requestedAt: typeof evidence.requested_at === "string" ? evidence.requested_at : null,
          sessionId: typeof evidence.session_id === "string" ? evidence.session_id : null,
        };
      } catch (error) { return { status: "unknown", error: String(error) }; } finally { con?.close(); }
    },
  };
}
