import { closeSync, openSync, utimesSync } from "node:fs";

import type { MoPDatabase } from "./db.js";

const PM_CLEAR_REQUESTED_AT_KEY = "pm_clear_requested_at";
const PM_CLEAR_DELIVERY_STATE_KEY = "pm_clear_delivery_state";
const PM_CLEAR_ACK_MISSING_FOR_KEY = "pm_clear_ack_missing_for";

type PendingClearEvent = {
  id: number;
  event_type: string;
  timestamp: string;
};

export type PMClearClaim =
  | { kind: "send"; requestedAt: string }
  | { kind: "pending" }
  | { kind: "recent" };

export type PMClearDelivery =
  | PMClearClaim
  | { kind: "sent"; requestedAt: string }
  | { kind: "deferred_busy"; requestedAt: string }
  | { kind: "uncertain"; requestedAt: string; error: string }
  | { kind: "failed"; error: string };

/** Outcome of one guarded PM clear write. `zeroEffect` is set ONLY when the
 * writer proves nothing reached the PM composer (or its own paste was removed). */
export type PMClearSendResult = { success: boolean; busy?: boolean; zeroEffect?: boolean; error?: string };

export async function waitForPmIdleDrain(options: {
  afterEventId: number;
  getDrainEvent: () => { id: number } | null;
  isPMBusy: () => boolean;
  isCurrent: () => boolean;
  onDrain: () => Promise<void>;
  wait: (ms: number) => Promise<void>;
  timeoutMs?: number;
  nowMs?: () => number;
}): Promise<boolean> {
  const deadline = (options.nowMs ?? Date.now)() + (options.timeoutMs ?? 60_000);
  while (options.isCurrent() && (options.nowMs ?? Date.now)() < deadline) {
    const drainEvent = options.getDrainEvent();
    if (drainEvent && drainEvent.id > options.afterEventId && !options.isPMBusy()) {
      await options.onDrain();
      return true;
    }
    await options.wait(100);
  }
  return false;
}

function parseMoPIsoMs(value: string | null | undefined): number | null {
  if (!value) return null;
  const trimmed = value.trim();
  const normalized = /(?:Z|[+-]\d{2}:\d{2})$/.test(trimmed) ? trimmed : `${trimmed}Z`;
  const timestamp = Date.parse(normalized);
  return Number.isFinite(timestamp) ? timestamp : null;
}

/** Claim one PM self-clear send. Only a proven zero-effect deferred_busy latch is ever resent. */
export function claimPmClearRequest(options: {
  db: Pick<MoPDatabase, "hasPendingClear" | "getConfig" | "clearPendingClear" | "setPendingClear" | "setConfig" | "logEvent">;
  source: string;
  nowMs: number;
  staleAfterMs: number;
  recentSuppressMs: number;
  hasRecentClearEvent: boolean;
  laterLifecycleEvent: PendingClearEvent | null;
  operatorReRequest?: boolean;
  /** Fence: only this exact request may be sent; a replacement is refused. */
  expectedRequestedAt?: string;
}): PMClearClaim {
  const { db, source, nowMs, staleAfterMs, recentSuppressMs } = options;
  const requestedAt = db.getConfig(PM_CLEAR_REQUESTED_AT_KEY);

  if (db.hasPendingClear(0)) {
    const requestedMs = parseMoPIsoMs(requestedAt);
    const evidence = options.laterLifecycleEvent;
    const evidenceMs = parseMoPIsoMs(evidence?.timestamp);
    const stale =
      requestedMs !== null &&
      nowMs - requestedMs >= staleAfterMs &&
      evidence !== null &&
      evidenceMs !== null &&
      evidenceMs > requestedMs;

    const deliveryState = db.getConfig(PM_CLEAR_DELIVERY_STATE_KEY);
    if (requestedAt !== null && deliveryState === "deferred_busy") {
      // A replacement request must never be adopted by a caller that was
      // fenced to an earlier request (Stop settle, drain observation).
      if (options.expectedRequestedAt !== undefined && options.expectedRequestedAt !== requestedAt) {
        db.logEvent(0, "clear_pending_replaced_refused", null, null, {
          name: "PM",
          via: source,
          expected_requested_at: options.expectedRequestedAt,
          requested_at: requestedAt,
        });
        return { kind: "pending" };
      }
      return { kind: "send", requestedAt };
    }

    // Every other latched state (claimed, sending, awaiting_ack, uncertain,
    // or a legacy latch with no state) may already have reached the PM pane.
    // It is ONE durable consumed/possible-effect claim shared by Stop, drain
    // and restart: never automatically re-armed. Only SessionStart:clear or
    // an explicit operator re-request (which clears the latch first) ends it.
    if (stale && requestedAt !== null && db.getConfig(PM_CLEAR_ACK_MISSING_FOR_KEY) !== requestedAt) {
      db.setConfig(PM_CLEAR_ACK_MISSING_FOR_KEY, requestedAt);
      db.logEvent(0, "pm_clear_ack_missing", null, null, {
        requested_at: requestedAt,
        delivery_state: deliveryState,
        observed_at: new Date(nowMs).toISOString(),
        later_lifecycle_event_id: evidence?.id ?? null,
        later_lifecycle_event_type: evidence?.event_type ?? null,
        reason: "A possibly delivered PM clear was not acknowledged by SessionStart source=clear; automatic resends stay suppressed.",
        via: source,
      });
    }
    db.logEvent(0, "clear_pending_duplicate_suppressed", null, null, {
      name: "PM",
      via: source,
      reason: "pm_clear_already_requested",
      requested_at: requestedAt,
      delivery_state: deliveryState,
    });
    return { kind: "pending" };
  }

  const confirmedAt = db.getConfig("pm_clear_confirmed_at");
  const confirmedMs = parseMoPIsoMs(confirmedAt);
  const latestRequestedMs = parseMoPIsoMs(requestedAt);
  const recentlyConfirmed =
    confirmedMs !== null && nowMs >= confirmedMs && nowMs - confirmedMs < recentSuppressMs;
  const recentlyRequested =
    latestRequestedMs !== null &&
    nowMs >= latestRequestedMs &&
    nowMs - latestRequestedMs < recentSuppressMs;
  if (!options.operatorReRequest && (recentlyConfirmed || recentlyRequested || options.hasRecentClearEvent)) {
    db.logEvent(0, "clear_recent_duplicate_suppressed", null, null, {
      name: "PM",
      via: source,
      reason: "pm_clear_recently_confirmed",
      confirmed_at: confirmedAt,
      requested_at: requestedAt,
      suppress_window_ms: recentSuppressMs,
    });
    return { kind: "recent" };
  }

  const nextRequestedAt = new Date(nowMs).toISOString();
  if (options.operatorReRequest) {
    db.setConfig(PM_CLEAR_ACK_MISSING_FOR_KEY, "");
  }
  if (options.expectedRequestedAt !== undefined) {
    // A fenced retry may only resend its own latched request, never mint one.
    return { kind: "pending" };
  }
  db.setPendingClear(0);
  db.setConfig(PM_CLEAR_REQUESTED_AT_KEY, nextRequestedAt);
  db.setConfig(PM_CLEAR_DELIVERY_STATE_KEY, "claimed");
  return { kind: "send", requestedAt: nextRequestedAt };
}

export async function requestPmClearOnce(options: Parameters<typeof claimPmClearRequest>[0] & {
  isPMBusy: () => boolean;
  send: (request: { requestedAt: string }) => Promise<PMClearSendResult>;
}): Promise<PMClearDelivery> {
  const { db } = options;
  const claim = claimPmClearRequest(options);
  if (claim.kind !== "send") return claim;

  // Synchronous with the claim: from here this request is consumed. Any
  // concurrent Stop/drain caller sees "sending" and stays suppressed.
  db.setConfig(PM_CLEAR_DELIVERY_STATE_KEY, "sending");
  if (options.isPMBusy()) {
    db.setConfig(PM_CLEAR_DELIVERY_STATE_KEY, "deferred_busy");
    db.logEvent(0, "clear_pending_deferred_busy", null, null, {
      requested_at: claim.requestedAt,
      reason: "PM is busy at clear-send boundary; latch retained for retry when idle",
    });
    return { kind: "deferred_busy", requestedAt: claim.requestedAt };
  }

  let result: PMClearSendResult;
  try {
    result = await options.send({ requestedAt: claim.requestedAt });
  } catch (err) {
    result = { success: false, error: String(err) };
  }
  // Settle only our own request. If an operator replaced the latch while the
  // write was in flight, leave the replacement untouched.
  const ownsLatch =
    db.hasPendingClear(0) && db.getConfig(PM_CLEAR_REQUESTED_AT_KEY) === claim.requestedAt;
  if (result.success) {
    if (ownsLatch) db.setConfig(PM_CLEAR_DELIVERY_STATE_KEY, "awaiting_ack");
    return { kind: "sent", requestedAt: claim.requestedAt };
  }
  if (result.zeroEffect && result.busy) {
    if (ownsLatch) {
      db.setConfig(PM_CLEAR_DELIVERY_STATE_KEY, "deferred_busy");
      db.logEvent(0, "clear_pending_deferred_busy", null, null, {
        requested_at: claim.requestedAt,
        reason: "PM became busy before the guarded write; zero effect proven, latch retained for retry when idle",
      });
    }
    return { kind: "deferred_busy", requestedAt: claim.requestedAt };
  }
  if (result.zeroEffect) {
    // Proven zero effect, not a busy refusal (identity/session drift,
    // unreadable composer): release the latch without retrying.
    if (ownsLatch) {
      db.clearPendingClear(0);
      db.setConfig(PM_CLEAR_DELIVERY_STATE_KEY, "failed");
    }
    return { kind: "failed", error: result.error ?? "PM clear refused before any effect" };
  }
  // Anything else may have reached the pane: keep the claim forever-suppressed.
  if (ownsLatch) {
    db.setConfig(PM_CLEAR_DELIVERY_STATE_KEY, "uncertain");
    db.logEvent(0, "pm_clear_delivery_uncertain", null, null, {
      requested_at: claim.requestedAt,
      error: result.error ?? null,
      reason: "PM clear write outcome is not proven zero-effect; automatic resends are suppressed",
    });
  }
  return { kind: "uncertain", requestedAt: claim.requestedAt, error: result.error ?? "PM clear outcome uncertain" };
}

/** A fence refusal: `busy` marks a PM-turn/busy refusal (deferrable). */
export type PMClearFenceRefusal = { reason: string; busy: boolean };

/**
 * Build the authority the serialized PM writer rechecks at paste and at the
 * single Enter: the original request id (still latched and in "sending"), the
 * PM turn generation pinned by the caller, the PM hook session, and idle.
 */
export function makePmClearWriteFence(deps: {
  db: Pick<MoPDatabase, "hasPendingClear" | "getConfig">;
  requestedAt: string;
  generation: number;
  sessionToken: string;
  currentGeneration: () => number;
  currentSessionToken: () => string;
  /** True when the caller's generation is a PM Stop observation (idle proof). */
  stopFenced: boolean;
  idleProven: () => boolean;
}): () => PMClearFenceRefusal | null {
  return () => {
    const { db } = deps;
    if (
      !db.hasPendingClear(0) ||
      db.getConfig(PM_CLEAR_REQUESTED_AT_KEY) !== deps.requestedAt ||
      db.getConfig(PM_CLEAR_DELIVERY_STATE_KEY) !== "sending"
    ) {
      return { reason: "pm_clear_request_replaced", busy: false };
    }
    if (deps.currentGeneration() !== deps.generation) return { reason: "pm_turn_started", busy: true };
    if (deps.currentSessionToken() !== deps.sessionToken) return { reason: "pm_session_drift", busy: false };
    if (!deps.stopFenced && !deps.idleProven()) return { reason: "pm_busy", busy: true };
    return null;
  };
}

const PM_CLEAR_STOP_RETRY_AT_KEY = "pm_clear_stop_retry_at";

/** True when a PM clear is latched but was never delivered (deferred at a busy boundary). */
export function isPmClearUndelivered(db: Pick<MoPDatabase, "hasPendingClear" | "getConfig">): boolean {
  return db.hasPendingClear(0) && db.getConfig(PM_CLEAR_DELIVERY_STATE_KEY) === "deferred_busy";
}

/**
 * On a PM Stop, retry an undelivered (deferred_busy) clear latch once.
 * Delivered-awaiting-ack latches are never resent. The retry waits a short
 * settle window, then requires the stop generation to still be current (no PM
 * turn started since this Stop), and is bounded by a per-attempt backoff.
 */
export async function retryDeferredPmClearOnStop(options: Omit<Parameters<typeof requestPmClearOnce>[0], "isPMBusy"> & {
  backoffMs: number;
  settleMs: number;
  wait: (ms: number) => Promise<void>;
  isCurrent: () => boolean;
}): Promise<PMClearDelivery | { kind: "skipped"; reason: string }> {
  const { db } = options;
  if (!isPmClearUndelivered(db)) return { kind: "skipped", reason: "not_undelivered" };
  const lastMs = parseMoPIsoMs(db.getConfig(PM_CLEAR_STOP_RETRY_AT_KEY));
  if (lastMs !== null && options.nowMs >= lastMs && options.nowMs - lastMs < options.backoffMs) {
    return { kind: "skipped", reason: "backoff" };
  }
  const fencedRequestedAt = db.getConfig(PM_CLEAR_REQUESTED_AT_KEY) ?? "";
  if (options.settleMs > 0) await options.wait(options.settleMs);
  if (!options.isCurrent() || !isPmClearUndelivered(db) || db.getConfig(PM_CLEAR_REQUESTED_AT_KEY) !== fencedRequestedAt) {
    return { kind: "skipped", reason: "stop_generation_superseded" };
  }
  db.setConfig(PM_CLEAR_STOP_RETRY_AT_KEY, new Date(options.nowMs).toISOString());
  db.logEvent(0, "clear_pending_stop_retry", null, null, {
    name: "PM",
    requested_at: db.getConfig(PM_CLEAR_REQUESTED_AT_KEY),
    via: options.source,
    reason: "Undelivered (deferred_busy) PM clear latch retried once on PM Stop",
  });
  return requestPmClearOnce({
    ...options,
    expectedRequestedAt: fencedRequestedAt,
    isPMBusy: () => !options.isCurrent(),
  });
}

/**
 * Marker read by the PM `pm-self-clear-stop.sh` Stop hook: while a fresh PM
 * clear request is waiting for PM to go idle, the hook lets Stop through
 * instead of blocking (which would keep PM busy and deadlock the clear).
 */
export const PM_SELF_CLEAR_MARKER_PATH = "/tmp/pm-self-clear-requested";

export function touchPmClearRequestedMarker(
  kind: PMClearDelivery["kind"],
  markerPath: string = PM_SELF_CLEAR_MARKER_PATH,
): boolean {
  if (kind !== "sent" && kind !== "deferred_busy" && kind !== "pending") return false;
  try {
    closeSync(openSync(markerPath, "a"));
    const now = new Date();
    utimesSync(markerPath, now, now);
    return true;
  } catch {
    return false;
  }
}
