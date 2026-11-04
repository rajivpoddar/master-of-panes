/**
 * PM exit-pending with a saved handoff and stopped subagents (Rajiv, DM
 * D0AMF0XE6TS thread 1793731816.594829, ts 1793752982.460979: "We should
 * implement the pm exit pending the same way. The additional step here will be
 * to stop all subagents after save handoff").
 *
 * A REST PM-only exit-pending request opens ONE `pm_save_handoff` obligation of
 * variant `exit`. The PM saves the SAME two handoff files the clear flow
 * watches (save detection is reused unchanged: both mtimes later than the
 * request, or an explicit resolve of this request's own row), stops every
 * running background agent with TaskStop, then acknowledges through
 * POST /exit-pending/subagents-stopped. Only when BOTH hold does the existing
 * PM Stop path send its single fenced /exit. Watchdog respawn, continuation
 * delivery and dev-only exits are unchanged.
 *
 * Rule 50: nothing here gates, holds or delays PM input or tools. Until both
 * conditions hold, PM Stops fall through to normal handling and the exit simply
 * stays pending under the normal obligation cadence. New input after the Stop
 * refuses the write (existing fence); a PM session change cancels the request,
 * exactly as the clear flow does.
 */
import type { MoPDatabase } from "./db.js";
import {
  handoffFilesSaved,
  PM_HANDOFF_FILES,
  type HandoffObligationOps,
  type HandoffObligationVariant,
} from "./pmHandoffClear.js";

export const PM_EXIT_HANDOFF_EPISODE_KEY = "pm_exit_handoff_episode";
export const PM_EXIT_HANDOFF_REQUESTED_AT_KEY = "pm_exit_handoff_requested_at";
export const PM_EXIT_HANDOFF_SESSION_KEY = "pm_exit_handoff_session";
export const PM_EXIT_HANDOFF_OBLIGATION_KEY = "pm_exit_handoff_obligation_id";
export const PM_EXIT_SUBAGENTS_STOPPED_KEY = "pm_exit_subagents_stopped_at";

export const PM_EXIT_HANDOFF_ACTION =
  "PM exit is pending (daily restart). 1) Save the PM handoff: update MEMORY-session-handoff-current.md "
  + "(~/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/memory/) and "
  + "data/pm-continuation-note.md. 2) Stop ALL running background agents/tasks with TaskStop. "
  + "3) Acknowledge: curl -s -X POST http://127.0.0.1:3100/exit-pending/subagents-stopped . "
  + "MoP then sends /exit at the next PM Stop; the watchdog respawns PM with the saved continuation. "
  + "Nothing is blocked meanwhile.";

export const PM_HANDOFF_EXIT_VARIANT: HandoffObligationVariant = {
  title: "Save PM handoff and stop subagents before MoP sends /exit",
  action: PM_EXIT_HANDOFF_ACTION,
  dedupeGroup: "pm_save_handoff:pm:exit",
};

type Db = Pick<MoPDatabase, "getConfig" | "setConfig" | "logEvent" | "getExitStatus" | "setExitPending">;

function parseMs(value: string | null | undefined): number | null {
  if (!value) return null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
}

/** Obligation target per ORIGINAL request; never collides with a clear request row. */
export function exitHandoffRequestKey(requestedAt: string): string {
  return `pm-dhruva:exit:${requestedAt}`;
}

/** The live exit-handoff request, only while it belongs to the current PM-only episode. */
export function currentPmExitHandoff(db: Pick<MoPDatabase, "getConfig" | "getExitStatus">):
  { requestedAt: string; requestedMs: number; episode: string; session: string } | null {
  const status = db.getExitStatus();
  const requestedAt = db.getConfig(PM_EXIT_HANDOFF_REQUESTED_AT_KEY) || "";
  const requestedMs = parseMs(requestedAt);
  const episode = db.getConfig(PM_EXIT_HANDOFF_EPISODE_KEY) || "";
  if (!status.pending || !status.pm_only || requestedMs === null || !episode || episode !== status.episode) return null;
  return { requestedAt, requestedMs, episode, session: db.getConfig(PM_EXIT_HANDOFF_SESSION_KEY) || "" };
}

function clearExitHandoff(db: Db): void {
  for (const key of [PM_EXIT_HANDOFF_EPISODE_KEY, PM_EXIT_HANDOFF_REQUESTED_AT_KEY, PM_EXIT_HANDOFF_SESSION_KEY,
    PM_EXIT_HANDOFF_OBLIGATION_KEY, PM_EXIT_SUBAGENTS_STOPPED_KEY]) db.setConfig(key, "");
}

async function resolveRow(ops: HandoffObligationOps | null, requestedAt: string, knownId: string | null, reason: string): Promise<boolean> {
  if (!ops) return false;
  let id = knownId;
  if (!id) {
    const found = ops.lookup(exitHandoffRequestKey(requestedAt));
    if (found.status !== "found" || !found.open) return false;
    id = found.id;
  }
  return ops.resolveById(id, reason).catch(() => false);
}

const writersInFlight = new Map<string, Promise<string | null>>();

/**
 * Idempotent per request: adopt an existing row for this request key, else one
 * keyed upsert (pm-ops matches open rows by kind+target, so a retry can never
 * duplicate or touch another request). The id is published only while the
 * request is still current.
 */
export function ensurePmExitHandoffObligation(db: Db, ops: HandoffObligationOps): Promise<string | null> {
  const live = currentPmExitHandoff(db);
  if (!live) return Promise.resolve(null);
  const existing = db.getConfig(PM_EXIT_HANDOFF_OBLIGATION_KEY);
  if (existing) return Promise.resolve(existing);
  const active = writersInFlight.get(live.requestedAt);
  if (active) return active;
  const writer = (async () => {
    const found = ops.lookup(exitHandoffRequestKey(live.requestedAt));
    let id: string | null = found.status === "found" && found.open && found.requestedAt === live.requestedAt ? found.id : null;
    let error: string | null = null;
    if (!id) {
      try {
        id = await ops.upsert(exitHandoffRequestKey(live.requestedAt), {
          producer: "mop-pm-handoff-exit", variant: "exit", pm_stop_actionable: 1,
          requested_at: live.requestedAt, episode: live.episode, session_id: live.session, files: PM_HANDOFF_FILES,
          ack: "POST /exit-pending/subagents-stopped",
        });
      } catch (err) { error = String(err); }
    }
    const still = currentPmExitHandoff(db);
    if (!still || still.requestedAt !== live.requestedAt) {
      if (id) void resolveRow(ops, live.requestedAt, id, `stale pm_save_handoff exit for ${live.requestedAt}; request superseded`);
      return null;
    }
    if (id) {
      db.setConfig(PM_EXIT_HANDOFF_OBLIGATION_KEY, id);
      db.logEvent(0, "pm_exit_handoff_obligation", null, null, { requested_at: live.requestedAt, obligation_id: id });
    } else {
      db.logEvent(0, "pm_exit_handoff_obligation_uncertain", null, null, {
        requested_at: live.requestedAt, error: error ?? "missing obligation id",
        reason: "Retried by request key at the next PM Stop; the exit stays pending, nothing blocks",
      });
    }
    return id;
  })().finally(() => writersInFlight.delete(live.requestedAt));
  writersInFlight.set(live.requestedAt, writer);
  return writer;
}

/** Resolve the row of a request that a newer exit-pending call replaced or disabled. */
export function resolveSupersededPmExitHandoff(ops: HandoffObligationOps, previous: { requestedAt: string; obligationId: string | null },
  reason: string): Promise<boolean> {
  return resolveRow(ops, previous.requestedAt, previous.obligationId, reason);
}

/** Record the PM's subagents-stopped ack for the CURRENT request only. */
export function recordPmExitSubagentsStopped(db: Db, options: { nowMs: number; episode?: string }):
  { ok: true; requested_at: string; acknowledged_at: string } | { ok: false; status: 409; error: string } {
  const live = currentPmExitHandoff(db);
  if (!live) return { ok: false, status: 409, error: "no PM exit handoff is pending" };
  if (options.episode !== undefined && options.episode !== live.episode) {
    return { ok: false, status: 409, error: "episode does not match the pending PM exit" };
  }
  const acknowledgedAt = new Date(Math.max(options.nowMs, live.requestedMs)).toISOString();
  db.setConfig(PM_EXIT_SUBAGENTS_STOPPED_KEY, acknowledgedAt);
  db.logEvent(0, "pm_exit_subagents_stopped", null, null, {
    requested_at: live.requestedAt, episode: live.episode, acknowledged_at: acknowledgedAt,
    reason: "PM acknowledged that all background agents/tasks are stopped",
  });
  return { ok: true, requested_at: live.requestedAt, acknowledged_at: acknowledgedAt };
}

export type ExitHandoffStopOutcome =
  | { kind: "not_required" }
  | { kind: "cancelled_session_change" }
  | { kind: "waiting"; saved: boolean; subagentsStopped: boolean }
  | { kind: "ready"; via: "mtime" | "explicit_resolve"; requestedAt: string };

/**
 * Synchronous evaluation at a PM Stop. Save detection is the clear flow's,
 * unchanged: both files' mtimes after the request, or an explicit resolve of
 * this request's own row. The ack must belong to this request.
 */
export function evaluatePmExitHandoffAtStop(db: Db, options: {
  currentSessionId: string;
  ops: HandoffObligationOps | null;
  files?: readonly string[];
}): ExitHandoffStopOutcome {
  const live = currentPmExitHandoff(db);
  if (!live) return { kind: "not_required" };
  if (live.session && options.currentSessionId && live.session !== options.currentSessionId) {
    const id = db.getConfig(PM_EXIT_HANDOFF_OBLIGATION_KEY) || null;
    clearExitHandoff(db);
    db.setExitPending(false);
    void resolveRow(options.ops, live.requestedAt, id,
      `PM session changed (${live.session} -> ${options.currentSessionId}); handoff exit cancelled`);
    db.logEvent(0, "pm_exit_handoff_cancelled", "Stop", null, {
      requested_at: live.requestedAt, recorded_session: live.session, current_session: options.currentSessionId,
      reason: "session_change",
    });
    return { kind: "cancelled_session_change" };
  }
  let via: "mtime" | "explicit_resolve" | null = handoffFilesSaved(live.requestedMs, options.files ?? PM_HANDOFF_FILES) ? "mtime" : null;
  const obligationId = db.getConfig(PM_EXIT_HANDOFF_OBLIGATION_KEY) || "";
  if (!via && obligationId && options.ops) {
    const row = options.ops.lookup(exitHandoffRequestKey(live.requestedAt));
    if (row.status === "found" && row.id === obligationId && !row.open && row.requestedAt === live.requestedAt) via = "explicit_resolve";
  }
  const ackMs = parseMs(db.getConfig(PM_EXIT_SUBAGENTS_STOPPED_KEY));
  const subagentsStopped = ackMs !== null && ackMs >= live.requestedMs;
  if (via && subagentsStopped) return { kind: "ready", via, requestedAt: live.requestedAt };
  return { kind: "waiting", saved: via !== null, subagentsStopped };
}

/** Consume the request when its single /exit attempt is taken; resolves its own row. */
export function consumePmExitHandoff(db: Db, ops: HandoffObligationOps | null, requestedAt: string, via: string): void {
  const id = db.getConfig(PM_EXIT_HANDOFF_OBLIGATION_KEY) || null;
  clearExitHandoff(db);
  void resolveRow(ops, requestedAt, id, `PM handoff saved (${via}) and subagents stopped; MoP sends /exit at this Stop`);
  db.logEvent(0, "pm_exit_handoff_ready", "Stop", null, {
    requested_at: requestedAt, via, obligation_id: id,
    reason: "Handoff saved and subagents stopped; consuming the single PM /exit attempt",
  });
}
