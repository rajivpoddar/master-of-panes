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
 * pending for the next tick.
 */
import type { Hono } from "hono";

import type { MoPDatabase } from "./db.js";

export const RESPAWN_WHEN_IDLE_TICK_MS = 10_000;

const KEY_PREFIX = "respawn_when_idle_";

export interface RespawnWhenIdleRequest {
  slot: number;
  requested_at: string;
  assignment_epoch: number | null;
}

export interface RespawnResult {
  status: number;
  body: { reason?: string; error?: string; [k: string]: unknown };
}

export interface RespawnWhenIdleDeps {
  db: MoPDatabase;
  devSlots: readonly number[];
  /** Live pane activity probe; true = working. */
  isSlotActive: (slot: number) => Promise<boolean>;
  /** Invoke the canonical respawn route with continue_session=true. */
  respawn: (slot: number) => Promise<RespawnResult>;
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
  if (row.active_turn_state === "active") return false;
  if (row.occupied && !row.idle) return false;
  return true;
}

/** Refusals that mean "not yet" rather than "failed": stay pending. */
const RETRYABLE_REASONS = new Set(["slot_busy_respawn_refused", "pane_identity_mismatch"]);

function isRetryable(result: RespawnResult): boolean {
  if (result.status !== 409) return false;
  if (result.body.reason && RETRYABLE_REASONS.has(result.body.reason)) return true;
  return /already in progress/.test(String(result.body.error ?? ""));
}

export function createRespawnWhenIdleTicker(deps: RespawnWhenIdleDeps) {
  const inflight = new Set<number>();
  const lastDeferReason = new Map<number, string>();

  async function tickSlot(slot: number): Promise<"none" | "deferred" | "fired" | "failed"> {
    const request = getRespawnWhenIdle(deps.db, slot);
    if (!request) return "none";
    if (inflight.has(slot)) return "deferred";
    if (!slotAtNaturalIdle(deps.db, slot) || (await deps.isSlotActive(slot))) return "deferred";
    inflight.add(slot);
    try {
      const result = await deps.respawn(slot);
      if (result.status >= 200 && result.status < 300) {
        clearRequest(deps.db, slot);
        lastDeferReason.delete(slot);
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
      clearRequest(deps.db, slot);
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

  app.post("/slots/:slotNum/respawn-when-idle", (c) => {
    const slot = parseSlot(c.req.param("slotNum"));
    if (slot === null) return c.json({ error: "respawn-when-idle applies to dev slots only" }, 400);
    const existing = getRespawnWhenIdle(deps.db, slot);
    if (existing) return c.json({ ok: true, already_pending: true, request: existing });
    const request: RespawnWhenIdleRequest = {
      slot,
      requested_at: (deps.now?.() ?? new Date()).toISOString(),
      assignment_epoch: deps.db.getSlot(slot)?.assignment_epoch ?? null,
    };
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
