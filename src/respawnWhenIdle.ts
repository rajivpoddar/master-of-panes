/**
 * Respawn-when-idle: schedule a one-shot `--continue` respawn of a dev slot
 * for its next natural idle. Never interrupts a working turn.
 *
 * Rajiv 2026-10-04 19:44 IST (DM D0AMF0XE6TS thread 1791122595.508319):
 * "dev slots need to be interrupted and restarted as well." Direct respawn
 * refuses busy slots (slot_busy_respawn_refused) and interrupt-turn is only
 * for proven wedges, so PM needs a sanctioned "at next idle" path.
 *
 * - POST   /slots/:n/respawn-when-idle  schedule (idempotent)
 * - DELETE /slots/:n/respawn-when-idle  cancel
 * - GET    /respawn-when-idle           pending requests
 *
 * The request is persisted in the config table so it survives a MoP restart.
 * A ticker fires it through the existing POST /slots/:n/respawn route
 * (pane identity pin, busy guard, crash suppression, cooldown), which never
 * touches assignment ownership or epoch. It fires once: success or a terminal
 * failure clears it; busy / identity-mismatch / in-progress refusals keep it
 * pending for the next tick. Uncertain attempted effects remain recorded and
 * cannot fire again; legacy requests without pinned identity stay pending.
 */
import type { Hono } from "hono";

import type { MoPDatabase } from "./db.js";
import type { PaneIdentityResult, PaneIdentitySnapshot } from "./paneIdentity.js";
import type { TmuxRelay, SlotActivityState } from "./relay.js";
import type { SlotState } from "./types.js";

export const RESPAWN_WHEN_IDLE_TICK_MS = 10_000;

const KEY_PREFIX = "respawn_when_idle_";

export interface RespawnWhenIdleRequest {
  slot: number;
  requested_at: string;
  assignment_epoch: number | null;
  /** Only a newer matching startup/resume hook can satisfy the request. */
  scheduled_event_id?: number | null;
  pane_id?: string;
  checkout_path?: string;
  session_id?: string;
  issue?: number | null;
  repository_id?: string | null;
  branch_ref?: string | null;
  occupied?: boolean;
  /** Persist before the first possible effect. Uncertainty cannot replay. */
  attempted_at?: string;
}

export interface RespawnResult {
  status: number;
  body: { reason?: string; error?: string; [k: string]: unknown };
}

export interface RespawnWhenIdleDeps {
  db: MoPDatabase;
  devSlots: readonly number[];
  verifyPaneIdentity: (slot: number) => Promise<PaneIdentityResult>;
  getSlotActivityState: (slot: number, paneId: string) => Promise<SlotActivityState>;
  /** Invoke the canonical respawn route with continue_session=true. */
  respawn: (slot: number, request: RespawnWhenIdleRequest) => Promise<RespawnResult>;
  injectToPM?: (text: string) => void;
  now?: () => Date;
}

export function getRespawnWhenIdle(db: MoPDatabase, slot: number): RespawnWhenIdleRequest | null {
  const raw = db.getConfig(`${KEY_PREFIX}${slot}`);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as RespawnWhenIdleRequest;
  } catch {
    return null;
  }
}

function clearRequest(db: MoPDatabase, slot: number): void {
  db.setConfig(`${KEY_PREFIX}${slot}`, "");
}

/** True only when the slot is at a natural idle: no active turn, not working. */
export function slotAtNaturalIdle(db: MoPDatabase, slot: number): boolean {
  const row = db.getSlot(slot);
  if (!row) return false;
  if (row.active_turn_state !== "inactive") return false;
  if (row.occupied && !row.idle) return false;
  return true;
}

/** Refusals that mean "not yet" rather than "failed": stay pending. */
const RETRYABLE_REASONS = new Set(["slot_busy_respawn_refused", "pane_identity_mismatch",
  "respawn_assignment_drift", "respawn_session_drift", "respawn_request_changed",
  "respawn_identity_unbound", "pane_not_idle", "composer_not_empty", "composer_unreadable"]);

const TUPLE_KEYS = ["assignment_epoch", "issue", "repository_id", "branch_ref", "occupied"] as const;

function sessionAt(db: MoPDatabase, slot: number, checkout: string): string | null {
  for (const event of db.getEvents(slot, 100)) {
    if (!event.hook_type) continue;
    try {
      const payload = JSON.parse(event.payload);
      if (typeof payload.session_id !== "string" || !payload.session_id.trim()) continue;
      // A newer foreign/unbound hook must not expose an older session as current.
      return payload.cwd === checkout ? payload.session_id : null;
    } catch { return null; }
  }
  return null;
}

function captureRequest(db: MoPDatabase, row: SlotState, pane: PaneIdentitySnapshot, at: string): RespawnWhenIdleRequest | null {
  const session = sessionAt(db, row.slot, pane.currentPath);
  if (!session) return null;
  return { slot: row.slot, requested_at: at, scheduled_event_id: db.getEvents(undefined, 1)[0]?.id ?? 0,
    pane_id: pane.paneId, checkout_path: pane.currentPath, session_id: session,
    assignment_epoch: row.assignment_epoch, issue: row.issue, repository_id: row.repository_id,
    branch_ref: row.branch_ref, occupied: row.occupied };
}

function sameRequest(a: RespawnWhenIdleRequest | null, b: RespawnWhenIdleRequest): boolean {
  return !!a && a.requested_at === b.requested_at && a.pane_id === b.pane_id
    && a.session_id === b.session_id && a.checkout_path === b.checkout_path
    && a.scheduled_event_id === b.scheduled_event_id && TUPLE_KEYS.every(key => a[key] === b[key]);
}

/** Synchronous ownership/completion fence used again at both command boundaries. */
function requestFence(db: MoPDatabase, request: RespawnWhenIdleRequest, persistent: boolean, ownAttempt = false): string | null {
  if (!request.pane_id || !request.session_id || !request.checkout_path || request.scheduled_event_id == null
      || TUPLE_KEYS.some(key => request[key] === undefined)) return "respawn_identity_unbound";
  const pending = persistent ? getRespawnWhenIdle(db, request.slot) : null;
  if (persistent && !sameRequest(pending, request)) return "respawn_request_changed";
  const row = db.getSlot(request.slot);
  if (!row || TUPLE_KEYS.some(key => row[key] !== request[key])) return "respawn_assignment_drift";
  if (sessionAt(db, request.slot, request.checkout_path) !== request.session_id) return "respawn_session_drift";
  if (persistent) {
    const completed = db.getEvents(request.slot, 100, "SessionStart").find(event => {
      if (event.id <= request.scheduled_event_id!) return false;
      try {
        const p = JSON.parse(event.payload);
        return (p.source === "startup" || p.source === "resume")
          && p.session_id === request.session_id && p.cwd === request.checkout_path;
      } catch { return false; }
    });
    if (completed) {
      clearRequest(db, request.slot);
      db.logEvent(request.slot, "respawn_when_idle_satisfied", null, null, {
        reason: "matching_session_restarted_after_schedule", session_start_event_id: completed.id,
      });
      return "respawn_already_satisfied";
    }
    if (pending?.attempted_at && !ownAttempt) return "respawn_effect_already_attempted";
  }
  return slotAtNaturalIdle(db, request.slot) ? null : "slot_busy_respawn_refused";
}

/** Use the existing pane send lock, verified composer and one-Enter guard for /exit. */
export async function deliverRespawnExit(args: {
  db: MoPDatabase;
  relay: Pick<TmuxRelay, "deliverGuardedToSlot" | "getSlotActivityState">;
  verifyPaneIdentity: (slot: number) => Promise<PaneIdentityResult>;
  slot: number;
  pane: PaneIdentitySnapshot;
  request?: RespawnWhenIdleRequest;
  beforeEffect: () => string | null;
}) {
  const row = args.db.getSlot(args.slot);
  const request = args.request ?? (row ? captureRequest(args.db, row, args.pane, "direct") : null);
  if (!request || request.slot !== args.slot || request.pane_id !== args.pane.paneId) {
    return { outcome: "refused_pre_effect" as const, reason: "respawn_identity_unbound", paneId: args.pane.paneId };
  }
  const turnId = args.db.getSlot(args.slot)?.active_turn_id;
  let ownAttempt = false;
  const fence = () => requestFence(args.db, request, !!args.request, ownAttempt)
    ?? (args.db.getSlot(args.slot)?.active_turn_id !== turnId ? "slot_busy_respawn_refused" : null);
  return args.relay.deliverGuardedToSlot(args.slot, "/exit", {
    expectedPaneId: request.pane_id!,
    recheck: async () => {
      const early = fence();
      if (early) return early;
      const identity = await args.verifyPaneIdentity(args.slot);
      if (!identity.ok || identity.snapshot.paneId !== request.pane_id
          || identity.snapshot.currentPath !== request.checkout_path) return "pane_identity_mismatch";
      if (await args.relay.getSlotActivityState(args.slot, request.pane_id!) !== "idle") {
        return fence() ?? "pane_not_idle";
      }
      return fence();
    },
    finalCheck: stage => {
      const refusal = fence();
      if (refusal) return refusal;
      if (stage === "pre_paste") {
        const before = args.beforeEffect();
        if (before) return before;
        if (args.request) args.db.setConfig(`${KEY_PREFIX}${args.slot}`, JSON.stringify({
          ...request, attempted_at: new Date().toISOString(),
        }));
        ownAttempt = true;
      }
      return null;
    },
  });
}

function isRetryable(result: RespawnResult): boolean {
  if (result.status !== 409) return false;
  if (result.body.reason?.startsWith("pane_identity:") || result.body.reason === "pane_changed_since_observation") return true;
  if (result.body.reason && RETRYABLE_REASONS.has(result.body.reason)) return true;
  return /already in progress/.test(String(result.body.error ?? ""));
}

export function createRespawnWhenIdleTicker(deps: RespawnWhenIdleDeps) {
  const inflight = new Set<number>();
  const lastDeferReason = new Map<number, string>();

  async function tickSlot(slot: number): Promise<"none" | "deferred" | "fired" | "failed" | "satisfied"> {
    const request = getRespawnWhenIdle(deps.db, slot);
    if (!request) return "none";
    if (inflight.has(slot)) return "deferred";
    const defer = (reason: string): "deferred" => {
      if (lastDeferReason.get(slot) !== reason) {
        lastDeferReason.set(slot, reason);
        deps.db.logEvent(slot, "respawn_when_idle_deferred", null, null, { reason });
      }
      return "deferred";
    };
    if (!request.pane_id || !request.session_id || !request.checkout_path) return defer("respawn_identity_unbound");
    inflight.add(slot);
    try {
      const identity = await deps.verifyPaneIdentity(slot);
      if (!identity.ok || identity.snapshot.paneId !== request.pane_id
          || identity.snapshot.currentPath !== request.checkout_path) return defer("pane_identity_mismatch");
      let refusal = requestFence(deps.db, request, true);
      if (refusal === "respawn_already_satisfied") return "satisfied";
      if (refusal) return defer(refusal);
      const activity = await deps.getSlotActivityState(slot, request.pane_id!);
      refusal = requestFence(deps.db, request, true);
      if (refusal === "respawn_already_satisfied") return "satisfied";
      if (refusal || activity !== "idle") return defer(refusal ?? `pane_activity_${activity}`);
      let result: RespawnResult;
      try { result = await deps.respawn(slot, request); }
      catch (error) {
        // An uncertain route invocation cannot be dispatched again.
        if (sameRequest(getRespawnWhenIdle(deps.db, slot), request)) {
          deps.db.setConfig(`${KEY_PREFIX}${slot}`, JSON.stringify({ ...request, attempted_at: new Date().toISOString() }));
        }
        result = { status: 500, body: { reason: "respawn_dispatch_uncertain", error: String(error) } };
      }
      if (result.status >= 200 && result.status < 300) {
        if (sameRequest(getRespawnWhenIdle(deps.db, slot), request)) clearRequest(deps.db, slot);
        lastDeferReason.delete(slot);
        if (result.body.already_satisfied) return "satisfied";
        deps.db.logEvent(slot, "respawn_when_idle_fired", null, null, {
          requested_at: request.requested_at,
          assignment_epoch_at_request: request.assignment_epoch,
          assignment_epoch_at_fire: deps.db.getSlot(slot)?.assignment_epoch ?? null,
        });
        deps.injectToPM?.(`# slot ${slot} respawned with --continue at idle (respawn-when-idle)`);
        return "fired";
      }
      if (isRetryable(result)) {
        const reason = result.body.reason ?? "respawn_in_progress";
        if (lastDeferReason.get(slot) !== reason) {
          lastDeferReason.set(slot, reason);
          deps.db.logEvent(slot, "respawn_when_idle_deferred", null, null, {
            reason,
            detail: result.body.error ?? null,
          });
        }
        return "deferred";
      }
      // Keep attempted records for reconciliation, but never dispatch them twice.
      if (!getRespawnWhenIdle(deps.db, slot)?.attempted_at
          && sameRequest(getRespawnWhenIdle(deps.db, slot), request)) clearRequest(deps.db, slot);
      lastDeferReason.delete(slot);
      deps.db.logEvent(slot, "respawn_when_idle_failed", null, null, {
        status: result.status,
        reason: result.body.reason ?? null,
        detail: result.body.error ?? null,
      });
      deps.injectToPM?.(
        `# slot ${slot} respawn-when-idle failed (HTTP ${result.status}): ${result.body.error ?? "unknown"}`,
      );
      return "failed";
    } finally {
      inflight.delete(slot);
    }
  }

  async function tick(): Promise<void> {
    for (const slot of deps.devSlots) {
      try {
        await tickSlot(slot);
      } catch (error) {
        console.error(`[respawn-when-idle] slot ${slot}:`, error);
      }
    }
  }

  return { tick, tickSlot };
}

export function registerRespawnWhenIdleRoutes(app: Hono, deps: RespawnWhenIdleDeps): void {
  const parseSlot = (raw: string): number | null => {
    const slot = Number(raw);
    return Number.isInteger(slot) && deps.devSlots.includes(slot) ? slot : null;
  };

  app.post("/slots/:slotNum/respawn-when-idle", async (c) => {
    const slot = parseSlot(c.req.param("slotNum"));
    if (slot === null) return c.json({ error: "respawn-when-idle applies to dev slots only" }, 400);
    const existing = getRespawnWhenIdle(deps.db, slot);
    if (existing) return c.json({ ok: true, already_pending: true, request: existing });
    const identity = await deps.verifyPaneIdentity(slot);
    const row = deps.db.getSlot(slot);
    if (!identity.ok || !row) return c.json({ error: "Verified pane unavailable", reason: "pane_identity_mismatch" }, 409);
    // Another schedule may have completed while the identity read awaited.
    const raced = getRespawnWhenIdle(deps.db, slot);
    if (raced) return c.json({ ok: true, already_pending: true, request: raced });
    const request = captureRequest(deps.db, row, identity.snapshot, (deps.now?.() ?? new Date()).toISOString());
    if (!request) return c.json({ error: "Current hook session identity unavailable", reason: "respawn_identity_unbound" }, 409);
    deps.db.setConfig(`${KEY_PREFIX}${slot}`, JSON.stringify(request));
    deps.db.logEvent(slot, "respawn_when_idle_scheduled", null, null, { ...request });
    return c.json({ ok: true, already_pending: false, request });
  });

  app.delete("/slots/:slotNum/respawn-when-idle", (c) => {
    const slot = parseSlot(c.req.param("slotNum"));
    if (slot === null) return c.json({ error: "respawn-when-idle applies to dev slots only" }, 400);
    const existing = getRespawnWhenIdle(deps.db, slot);
    clearRequest(deps.db, slot);
    if (existing) deps.db.logEvent(slot, "respawn_when_idle_cancelled", null, null, {});
    return c.json({ ok: true, cancelled: !!existing });
  });

  app.get("/respawn-when-idle", (c) => {
    const pending = deps.devSlots
      .map((slot) => getRespawnWhenIdle(deps.db, slot))
      .filter((r): r is RespawnWhenIdleRequest => r !== null);
    return c.json({ pending });
  });
}
