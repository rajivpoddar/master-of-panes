import type { MoPDatabase } from "./db.js";

const PM_CLEAR_REQUESTED_AT_KEY = "pm_clear_requested_at";
const PM_CLEAR_DELIVERY_STATE_KEY = "pm_clear_delivery_state";
const PM_CLEAR_REARMED_REQUESTED_AT_KEY = "pm_clear_rearmed_requested_at";
const PM_CLEAR_ACK_MISSING_FOR_KEY = "pm_clear_ack_missing_for";

type PendingClearEvent = {
  id: number;
  event_type: string;
  timestamp: string;
};

export type PMClearClaim =
  | { kind: "send"; requestedAt: string; repaired?: PendingClearEvent }
  | { kind: "pending" }
  | { kind: "recent" };

export type PMClearDelivery =
  | PMClearClaim
  | { kind: "sent"; requestedAt: string }
  | { kind: "deferred_busy"; requestedAt: string }
  | { kind: "failed"; error: string };

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

/** Claim one PM self-clear send, reopening only an old latch crossed by a later PM lifecycle event. */
export function claimPmClearRequest(options: {
  db: Pick<MoPDatabase, "hasPendingClear" | "getConfig" | "clearPendingClear" | "setPendingClear" | "setConfig" | "logEvent">;
  source: string;
  nowMs: number;
  staleAfterMs: number;
  recentSuppressMs: number;
  hasRecentClearEvent: boolean;
  laterLifecycleEvent: PendingClearEvent | null;
  operatorReRequest?: boolean;
}): PMClearClaim {
  const { db, source, nowMs, staleAfterMs, recentSuppressMs } = options;
  const requestedAt = db.getConfig(PM_CLEAR_REQUESTED_AT_KEY);
  let repaired: PendingClearEvent | undefined;

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

    const deferredBusy =
      requestedAt !== null &&
      db.getConfig(PM_CLEAR_DELIVERY_STATE_KEY) === "deferred_busy";

    if (deferredBusy) {
      return { kind: "send", requestedAt };
    }

    if (!stale) {
      db.logEvent(0, "clear_pending_duplicate_suppressed", null, null, {
        name: "PM",
        via: source,
        reason: "pm_clear_already_requested",
        requested_at: requestedAt,
      });
      return { kind: "pending" };
    }

    if (requestedAt !== null && db.getConfig(PM_CLEAR_REARMED_REQUESTED_AT_KEY) === requestedAt) {
      if (db.getConfig(PM_CLEAR_ACK_MISSING_FOR_KEY) !== requestedAt) {
        db.setConfig(PM_CLEAR_ACK_MISSING_FOR_KEY, requestedAt);
        db.logEvent(0, "pm_clear_ack_missing", null, null, {
          requested_at: requestedAt,
          observed_at: new Date(nowMs).toISOString(),
          later_lifecycle_event_id: evidence.id,
          later_lifecycle_event_type: evidence.event_type,
          reason: "The single stale-latch re-arm was not acknowledged by SessionStart source=clear; further automatic sends are suppressed.",
          via: source,
        });
      }
      return { kind: "pending" };
    }

    repaired = evidence;
    db.clearPendingClear(0);
    db.logEvent(0, "clear_pending_stale_repaired", null, null, {
      name: "PM",
      requested_at: requestedAt,
      repaired_at: new Date(nowMs).toISOString(),
      later_lifecycle_event_id: evidence.id,
      later_lifecycle_event_type: evidence.event_type,
      later_lifecycle_event_timestamp: evidence.timestamp,
      via: source,
      reason: "Old clear latch crossed a later PM lifecycle event without SessionStart:clear acknowledgement; clearing only the latch before a new explicit request.",
    });
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
    db.setConfig(PM_CLEAR_REARMED_REQUESTED_AT_KEY, "");
    db.setConfig(PM_CLEAR_ACK_MISSING_FOR_KEY, "");
  }
  db.setPendingClear(0);
  db.setConfig(PM_CLEAR_REQUESTED_AT_KEY, nextRequestedAt);
  db.setConfig(PM_CLEAR_DELIVERY_STATE_KEY, "claimed");
  if (repaired) db.setConfig(PM_CLEAR_REARMED_REQUESTED_AT_KEY, nextRequestedAt);
  return { kind: "send", requestedAt: nextRequestedAt, repaired };
}

export async function requestPmClearOnce(options: Parameters<typeof claimPmClearRequest>[0] & {
  isPMBusy: () => boolean;
  send: () => Promise<{ success: boolean; busy?: boolean; error?: string }>;
}): Promise<PMClearDelivery> {
  const claim = claimPmClearRequest(options);
  if (claim.kind !== "send") return claim;

  options.db.setConfig(PM_CLEAR_DELIVERY_STATE_KEY, "sending");
  if (options.isPMBusy()) {
    options.db.setConfig(PM_CLEAR_DELIVERY_STATE_KEY, "deferred_busy");
    options.db.logEvent(0, "clear_pending_deferred_busy", null, null, {
      requested_at: claim.requestedAt,
      reason: "PM is busy at clear-send boundary; latch retained for retry when idle",
    });
    return { kind: "deferred_busy", requestedAt: claim.requestedAt };
  }

  const result = await options.send();
  if (result.busy) {
    options.db.setConfig(PM_CLEAR_DELIVERY_STATE_KEY, "deferred_busy");
    options.db.logEvent(0, "clear_pending_deferred_busy", null, null, {
      requested_at: claim.requestedAt,
      reason: "PM became busy before the send endpoint; latch retained for retry when idle",
    });
    return { kind: "deferred_busy", requestedAt: claim.requestedAt };
  }
  if (!result.success) {
    options.db.clearPendingClear(0);
    options.db.setConfig(PM_CLEAR_DELIVERY_STATE_KEY, "failed");
    return { kind: "failed", error: result.error ?? "PM clear send failed" };
  }
  options.db.setConfig(PM_CLEAR_DELIVERY_STATE_KEY, "awaiting_ack");
  return { kind: "sent", requestedAt: claim.requestedAt };
}
