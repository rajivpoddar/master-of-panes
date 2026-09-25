import type { MoPDatabase } from "./db.js";

const PM_CLEAR_REQUESTED_AT_KEY = "pm_clear_requested_at";

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
  | { kind: "failed"; error: string };

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

    if (!stale) {
      db.logEvent(0, "clear_pending_duplicate_suppressed", null, null, {
        name: "PM",
        via: source,
        reason: "pm_clear_already_requested",
        requested_at: requestedAt,
      });
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
  if (recentlyConfirmed || recentlyRequested || options.hasRecentClearEvent) {
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
  db.setPendingClear(0);
  db.setConfig(PM_CLEAR_REQUESTED_AT_KEY, nextRequestedAt);
  return { kind: "send", requestedAt: nextRequestedAt, repaired };
}

export async function requestPmClearOnce(options: Parameters<typeof claimPmClearRequest>[0] & {
  send: () => Promise<{ success: boolean; error?: string }>;
}): Promise<PMClearDelivery> {
  const claim = claimPmClearRequest(options);
  if (claim.kind !== "send") return claim;

  const result = await options.send();
  if (!result.success) {
    options.db.clearPendingClear(0);
    return { kind: "failed", error: result.error ?? "PM clear send failed" };
  }
  return { kind: "sent", requestedAt: claim.requestedAt };
}
