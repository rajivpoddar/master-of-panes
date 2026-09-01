import type { Hono } from "hono";
import { z } from "zod";

import {
  isPmTransitionAssignmentRequest,
  PM_TRANSITION_ASSIGNMENT_HEADER,
} from "./assignmentAuthority.js";
import type { MoPDatabase } from "./db.js";
import type { CheckoutReadOnlyObservation } from "./slotRelease.js";
import { DEFAULT_DEV_SLOT_COUNT } from "./slotConfig.js";

const slotSchema = z.coerce.number().int().min(1).max(DEFAULT_DEV_SLOT_COUNT);
const requestSchema = z.object({
  expected_epoch: z.number().int().nonnegative(),
  expected_session_id: z.string().min(1),
  expected_session_started_at: z.string().datetime({ offset: true }),
  expected_age_seconds: z.number().finite().gt(6 * 60 * 60),
  checkout_path: z.string().startsWith("/"),
  checkout_branch: z.string().min(1),
  checkout_head: z.string().regex(/^[0-9a-f]{40}$/i),
  checkout_clean: z.literal(true),
  unpushed_commits: z.array(z.string()).length(0),
  request_token: z.string().min(1),
});

export interface SessionClearRouteDependencies {
  db: Pick<MoPDatabase, "getSlot" | "getSessionClearIntent" | "hasSessionClearIntent" | "claimSessionClearIntent" | "markSessionClearDeliveryStarted" | "clearSessionClearIntent" | "logEvent">;
  resolveCheckout: (slot: number) => Promise<string | null>;
  observeCheckout: (path: string) => Promise<CheckoutReadOnlyObservation>;
  deliverClear: (slot: number, beforeEffect: () => Promise<boolean>, finalEffectFence: () => boolean) => Promise<{ ok: boolean; effect_started: boolean; reason?: string }>;
  sleep?: (milliseconds: number) => Promise<void>;
  now?: () => number;
}

function sameInstant(left: string, right: string): boolean {
  const leftMs = Date.parse(left);
  const rightMs = Date.parse(right);
  return Number.isFinite(leftMs) && Number.isFinite(rightMs) && leftMs === rightMs;
}

function failure(code: string, reason: string, epoch: number) {
  return { success: false, code, reason, assignment_epoch: epoch };
}

function slotFenceMatches(
  current: ReturnType<MoPDatabase["getSlot"]>,
  request: z.infer<typeof requestSchema>,
): boolean {
  return Boolean(
    current
    && current.assignment_epoch === request.expected_epoch
    && current.session_id === request.expected_session_id
    && current.session_started_at !== null
    && sameInstant(current.session_started_at, request.expected_session_started_at)
    && current.occupied === false
    && current.dnd === false
    && current.idle === true
    && current.active_turn_id === null
    && current.active_turn_state === "inactive"
  );
}

/**
 * Explicit operator session replacement for an already-free slot.  This is
 * deliberately separate from the legacy /clear route: it cannot target an
 * occupied/active slot, and its short durable intent prevents assignment from
 * racing the one direct /clear delivery.
 */
export function registerSessionClearRoute(
  app: Hono,
  dependencies: SessionClearRouteDependencies,
): void {
  const sleep = dependencies.sleep ?? ((milliseconds: number) => new Promise<void>((resolve) => setTimeout(resolve, milliseconds)));
  const now = dependencies.now ?? (() => Date.now());

  app.post("/slots/:slotNum/session/clear", async (c) => {
    if (!isPmTransitionAssignmentRequest(c.req.header(PM_TRANSITION_ASSIGNMENT_HEADER))) {
      return c.json(failure("assignment_authority_required", "authenticated MoP authority is required", 0), 403);
    }
    const slotResult = slotSchema.safeParse(c.req.param("slotNum"));
    if (!slotResult.success) return c.json(failure("invalid_request", "invalid slot number", 0), 400);

    let raw: unknown;
    try {
      raw = await c.req.json();
    } catch {
      return c.json(failure("invalid_request", "JSON request required", 0), 400);
    }
    const parsed = requestSchema.safeParse(raw);
    if (!parsed.success) {
      return c.json(failure("invalid_request", "complete session/checkout facts are required", 0), 400);
    }
    const request = parsed.data;
    const slot = slotResult.data;
    const current = dependencies.db.getSlot(slot);
    const epoch = current?.assignment_epoch ?? 0;
    if (!current) return c.json(failure("slot_not_found", "slot not found", epoch), 404);

    const hasIntent = dependencies.db.hasSessionClearIntent(slot);
    const existing = dependencies.db.getSessionClearIntent(slot);
    if (hasIntent && !existing) {
      return c.json(failure("session_clear_intent_malformed", "stored session-clear intent is malformed; manual repair required", epoch), 409);
    }
    if (existing) {
      if (
        existing.token !== request.request_token
        || existing.expected_epoch !== request.expected_epoch
        || existing.expected_session_id !== request.expected_session_id
        || existing.expected_session_started_at !== request.expected_session_started_at
      ) {
        return c.json(failure("session_clear_in_progress", "another exact session clear is in progress", epoch), 409);
      }
      const reconciled = dependencies.db.getSlot(slot);
      if (
        reconciled
        && reconciled.assignment_epoch === request.expected_epoch
        && !reconciled.occupied
        && !reconciled.dnd
        && reconciled.idle
        && reconciled.active_turn_id === null
        && reconciled.active_turn_state === "inactive"
        && reconciled.session_id
        && reconciled.session_id !== request.expected_session_id
        && reconciled.session_started_at
        && Date.parse(reconciled.session_started_at) > Date.parse(request.expected_session_started_at)
      ) {
        dependencies.db.clearSessionClearIntent(slot, request.request_token);
        dependencies.db.logEvent(slot, "session_clear_reconciled", null, null, {
          request_token: request.request_token,
          session_id: reconciled.session_id,
          assignment_epoch: reconciled.assignment_epoch,
          idempotent: true,
        });
        return c.json({
          success: true,
          code: "session_cleared",
          idempotent: true,
          assignment_epoch: reconciled.assignment_epoch,
          previous_session_id: request.expected_session_id,
          session_id: reconciled.session_id,
          session_started_at: reconciled.session_started_at,
        });
      }
      return c.json(failure("session_clear_response_ambiguous", "existing clear intent has no fresh-session readback; no second delivery", epoch), 409);
    }

    const dueAge = current.session_started_at ? (now() - Date.parse(current.session_started_at)) / 1000 : NaN;
    if (!Number.isFinite(dueAge) || dueAge <= 6 * 60 * 60) {
      return c.json(failure("session_age_not_due", "session age is not greater than six hours", epoch), 409);
    }
    if (Math.abs(dueAge - request.expected_age_seconds) > 10) {
      return c.json(failure("session_age_drift", "session age drifted", epoch), 409);
    }
    if (
      current.occupied
      || current.dnd
      || !current.idle
      || current.active_turn_id !== null
      || current.active_turn_state !== "inactive"
      || current.assignment_epoch !== request.expected_epoch
      || current.session_id !== request.expected_session_id
      || !current.session_started_at
      || !sameInstant(current.session_started_at, request.expected_session_started_at)
    ) {
      return c.json(failure("session_state_drift", "slot is not the exact free inactive session", epoch), 409);
    }

    const readFreshFence = async (): Promise<{ ok: true } | { ok: false; code: string; reason: string; epoch: number }> => {
      const beforeResolve = dependencies.db.getSlot(slot);
      if (!slotFenceMatches(beforeResolve, request)) {
        return { ok: false, code: "session_state_drift", reason: "slot is not the exact free inactive session", epoch: beforeResolve?.assignment_epoch ?? epoch };
      }
      let resolvedCheckout: string | null;
      try {
        resolvedCheckout = await dependencies.resolveCheckout(slot);
      } catch {
        return { ok: false, code: "checkout_identity_unavailable", reason: "slot checkout identity could not be read", epoch: dependencies.db.getSlot(slot)?.assignment_epoch ?? epoch };
      }
      const afterResolve = dependencies.db.getSlot(slot);
      if (!slotFenceMatches(afterResolve, request)) {
        return { ok: false, code: "session_state_drift", reason: "slot drifted while resolving checkout", epoch: afterResolve?.assignment_epoch ?? epoch };
      }
      if (resolvedCheckout !== request.checkout_path) {
        return { ok: false, code: "checkout_identity_drift", reason: "slot checkout identity drifted", epoch: afterResolve?.assignment_epoch ?? epoch };
      }
      let checkout: CheckoutReadOnlyObservation;
      try {
        checkout = await dependencies.observeCheckout(request.checkout_path);
      } catch {
        return { ok: false, code: "checkout_unavailable", reason: "checkout facts could not be read", epoch: dependencies.db.getSlot(slot)?.assignment_epoch ?? epoch };
      }
      const afterObserve = dependencies.db.getSlot(slot);
      if (!slotFenceMatches(afterObserve, request)) {
        return { ok: false, code: "session_state_drift", reason: "slot drifted while observing checkout", epoch: afterObserve?.assignment_epoch ?? epoch };
      }
      if (
        checkout.checkout_path !== request.checkout_path
        || !checkout.clean
        || checkout.unpushed_commits.length !== 0
        || checkout.branch !== request.checkout_branch
        || checkout.head?.toLowerCase() !== request.checkout_head.toLowerCase()
      ) {
        return { ok: false, code: "checkout_not_clean", reason: "checkout is not the exact clean/unpushed-free snapshot", epoch: afterObserve?.assignment_epoch ?? epoch };
      }
      return { ok: true };
    };

    const initialFence = await readFreshFence();
    if (!initialFence.ok) return c.json(failure(initialFence.code, initialFence.reason, initialFence.epoch), 409);

    const claim = dependencies.db.claimSessionClearIntent(
      slot,
      request.expected_epoch,
      request.expected_session_id,
      request.expected_session_started_at,
      request.request_token,
    );
    if (!claim.ok) {
      return c.json(failure(claim.reason ?? "session_clear_refused", "session clear refused before delivery", claim.assignment_epoch), 409);
    }
    const replay = claim.idempotent;
    if (!replay) {
      const postClaimFence = await readFreshFence();
      if (!postClaimFence.ok) {
        dependencies.db.clearSessionClearIntent(slot, request.request_token);
        return c.json(failure(postClaimFence.code, postClaimFence.reason, postClaimFence.epoch), 409);
      }
      const started = dependencies.db.markSessionClearDeliveryStarted(
        slot,
        request.expected_epoch,
        request.expected_session_id,
        request.expected_session_started_at,
        request.request_token,
      );
      if (!started.ok) {
        return c.json(failure(started.reason ?? "session_clear_delivery_refused", "clear delivery refused before pane mutation", started.assignment_epoch), 409);
      }
      const delivered = await dependencies.deliverClear(slot, async () => {
        const fence = await readFreshFence();
        return fence.ok;
      }, () => slotFenceMatches(dependencies.db.getSlot(slot), request));
      if (!delivered.ok) {
        if (!delivered.effect_started) dependencies.db.clearSessionClearIntent(slot, request.request_token);
        return c.json(failure(
          delivered.effect_started ? "session_clear_response_ambiguous" : "session_clear_effect_refused",
          delivered.reason ?? (delivered.effect_started ? "direct clear delivery response was not confirmed; intent retained" : "direct clear effect fence refused before pane mutation"),
          epoch,
        ), 409);
      }
    }

    const deadline = now() + 30_000;
    while (now() < deadline) {
      const after = dependencies.db.getSlot(slot);
      const newSession = after?.session_id && after.session_id !== request.expected_session_id;
      if (after && newSession && after.session_started_at && Date.parse(after.session_started_at) > Date.parse(request.expected_session_started_at)) {
        if (
          after.assignment_epoch !== request.expected_epoch
          || after.occupied
          || after.dnd
          || !after.idle
          || after.active_turn_id !== null
          || after.active_turn_state !== "inactive"
        ) {
          return c.json(failure("session_clear_readback_drift", "new session readback is not free/inactive", after.assignment_epoch), 409);
        }
        dependencies.db.clearSessionClearIntent(slot, request.request_token);
        dependencies.db.logEvent(slot, "session_clear_committed", null, null, {
          request_token: request.request_token,
          previous_session_id: request.expected_session_id,
          session_id: after.session_id,
          assignment_epoch: after.assignment_epoch,
          idempotent: replay,
        });
        return c.json({
          success: true,
          code: "session_cleared",
          idempotent: replay,
          assignment_epoch: after.assignment_epoch,
          previous_session_id: request.expected_session_id,
          session_id: after.session_id,
          session_started_at: after.session_started_at,
        });
      }
      await sleep(100);
    }
    return c.json(failure("session_clear_response_ambiguous", "fresh session readback was not observed; intent retained", epoch), 409);
  });
}
