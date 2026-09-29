/**
 * Event-driven "wait for SessionStart source=clear" primitive.
 *
 * Every hook POST is logged verbatim at the top of HookProcessor.process()
 * (see hooks.ts) as an `events` row with `event_type = payload.type` — so a
 * SessionStart hook is always durably recorded as event_type "SessionStart"
 * with its full payload (including `source`) as JSON, for every slot, with
 * no extra wiring required. This module turns that existing log into a
 * bounded, deadline-polled readiness signal, mirroring the
 * deadline+poll style already used by `waitForAgentBoot` (health.ts) and
 * `waitForPmIdleDrain` (pmClearLatch.ts).
 *
 * Two call sites reuse this:
 *  - PM (slot 0) `/clear` resume_prompt fallback (server.ts)
 *  - assign-effect `new_issue` post-clear readiness wait (server.ts,
 *    replacing the old blind wait via clearSlotForAssignmentWithReadyWait)
 */

import type { MoPDatabase } from "./db.js";

export type SessionStartClearEvent = { id: number; timestamp: string };

type EventsSource = Pick<MoPDatabase, "getEvents">;

/** The most recent SessionStart event id for `slot`, or 0 when none exists. */
export function latestSessionStartEventId(db: EventsSource, slot: number): number {
  return db.getEvents(slot, 1, "SessionStart")[0]?.id ?? 0;
}

/**
 * Earliest SessionStart(source="clear") event for `slot` with id > afterEventId,
 * or null when none has landed yet. `limit` bounds how far back through recent
 * SessionStart rows we look (events are logged for every hook, so this stays
 * a shallow, cheap scan under normal slot activity).
 */
export function findSessionStartClearEvent(
  db: EventsSource,
  slot: number,
  afterEventId: number,
  limit = 20,
): SessionStartClearEvent | null {
  const rows = db.getEvents(slot, limit, "SessionStart");
  let match: SessionStartClearEvent | null = null;
  for (const row of rows) {
    if (row.id <= afterEventId) continue;
    let source: unknown;
    try {
      source = (JSON.parse(row.payload as unknown as string) as Record<string, unknown>)?.source;
    } catch {
      continue;
    }
    if (source === "clear" && (match === null || row.id < match.id)) {
      match = { id: row.id, timestamp: row.timestamp };
    }
  }
  return match;
}

export const DEFAULT_SESSION_START_CLEAR_TIMEOUT_MS = 45_000;
export const DEFAULT_SESSION_START_CLEAR_POLL_MS = 1_000;

export type WaitForSessionStartClearDeps = {
  getEvent: () => SessionStartClearEvent | null;
  wait: (ms: number) => Promise<void>;
  timeoutMs?: number;
  pollMs?: number;
  nowMs?: () => number;
};

export type SessionStartClearWaitResult = {
  ready: boolean;
  event: SessionStartClearEvent | null;
  waitedMs: number;
};

/**
 * Bounded, event-driven wait for a SessionStart(source=clear) acknowledgement.
 * Deadline+poll, same shape as `waitForAgentBoot` in health.ts: never blocks
 * past `timeoutMs`, and resolves immediately once `getEvent()` reports a
 * match. Callers own the DB/time-source binding via the injected closures so
 * this stays independently unit-testable with fake time.
 */
export async function waitForSessionStartClear(
  deps: WaitForSessionStartClearDeps,
): Promise<SessionStartClearWaitResult> {
  const now = deps.nowMs ?? Date.now;
  const timeoutMs = deps.timeoutMs ?? DEFAULT_SESSION_START_CLEAR_TIMEOUT_MS;
  const pollMs = deps.pollMs ?? DEFAULT_SESSION_START_CLEAR_POLL_MS;
  const startedAt = now();
  const deadline = startedAt + timeoutMs;

  for (;;) {
    const event = deps.getEvent();
    if (event) {
      return { ready: true, event, waitedMs: now() - startedAt };
    }
    const remaining = deadline - now();
    if (remaining <= 0) {
      return { ready: false, event: null, waitedMs: now() - startedAt };
    }
    await deps.wait(Math.min(pollMs, remaining));
  }
}

/** Convenience wrapper bound to a real MoPDatabase + real timers. */
export function waitForSessionStartClearOnDb(
  db: EventsSource,
  slot: number,
  afterEventId: number,
  options?: { timeoutMs?: number; pollMs?: number; wait?: (ms: number) => Promise<void> },
): Promise<SessionStartClearWaitResult> {
  return waitForSessionStartClear({
    getEvent: () => findSessionStartClearEvent(db, slot, afterEventId),
    wait: options?.wait ?? ((ms) => new Promise((resolve) => setTimeout(resolve, ms))),
    timeoutMs: options?.timeoutMs,
    pollMs: options?.pollMs,
  });
}

// ─── assign-effect new_issue: clear + event-driven readiness wait ─────────

export type ClearForAssignmentSendResult = {
  success: boolean;
  reason?: string;
  error?: string;
  status?: number;
};

export type ClearForAssignmentResult = { ok: boolean; reason: string; detail?: string };

export type ClearForAssignmentDeps = {
  clearPendingClear: (slot: number) => void;
  getBaselineEventId: (slot: number) => number;
  sendClear: (slot: number) => Promise<ClearForAssignmentSendResult>;
  logSlotCleared: (slot: number) => void;
  waitForReady: (slot: number, baselineEventId: number) => Promise<SessionStartClearWaitResult>;
  logReadyWait?: (
    slot: number,
    result: { ready: boolean; waitedMs: number; eventId: number | null },
  ) => void;
};

/**
 * Session clear for the atomic new-issue assignment boundary, factored out
 * of server.ts so it is independently testable.
 *
 * Sends `/clear` through the one existing send path regardless of occupancy
 * (assignment onto an occupied slot implicitly releases it first, so the
 * clear is never gated on "free"), then waits — event-driven, with a bounded
 * fallback poll — for the slot's SessionStart(source=clear) acknowledgement
 * before returning. This replaces relying on the downstream delivery paste's
 * instant composer read (which races the slot's actual session restart)
 * with genuine session-restart evidence. The return contract is unchanged:
 * the wait is purely an internal step and never turns a successful clear
 * send into a refusal — a wait timeout is recorded and delivery still
 * proceeds, exactly as before this readiness gate existed.
 */
export async function clearSlotForAssignmentWithReadyWait(
  slotNum: number,
  deps: ClearForAssignmentDeps,
): Promise<ClearForAssignmentResult> {
  deps.clearPendingClear(slotNum);
  const baselineEventId = deps.getBaselineEventId(slotNum);
  const sent = await deps.sendClear(slotNum);
  if (!sent.success) {
    return {
      ok: false,
      reason: "assignment_clear_not_applied",
      detail: sent.reason ?? sent.error ?? `send failed status=${sent.status ?? 0}`,
    };
  }

  deps.clearPendingClear(slotNum);
  deps.logSlotCleared(slotNum);

  const wait = await deps.waitForReady(slotNum, baselineEventId);
  deps.logReadyWait?.(slotNum, {
    ready: wait.ready,
    waitedMs: wait.waitedMs,
    eventId: wait.event?.id ?? null,
  });

  return { ok: true, reason: "cleared" };
}
