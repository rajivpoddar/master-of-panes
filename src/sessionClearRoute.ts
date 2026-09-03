import { createHash, timingSafeEqual } from "node:crypto";

import type { Hono } from "hono";
import { z } from "zod";

import {
  type MoPDatabase,
  type SessionClearEffectInput,
  type SessionClearEffectReceipt,
} from "./db.js";
import type { CheckoutReadOnlyObservation } from "./slotRelease.js";
import type { TmuxRelay } from "./relay.js";
import { DEFAULT_DEV_SLOT_COUNT, PM_SLOT } from "./slotConfig.js";
import { verifyPaneIdentity } from "./paneIdentity.js";

export const SESSION_CLEAR_AUTHORITY = "mop-release-assign-v1";
export const SESSION_CLEAR_AUTHORITY_HEADER = "x-heydonna-direct-client";
export const SESSION_CLEAR_CAPABILITY_HEADER = "x-mop-capability";

export interface SessionClearRouteDependencies {
  db: MoPDatabase;
  relay: TmuxRelay;
  observeCheckout: (checkoutPath: string) => Promise<CheckoutReadOnlyObservation>;
  verifyPane?: typeof verifyPaneIdentity;
}

// PM is an explicit session boundary, not an alias for a numbered slot. Keep
// the public spelling stable while using slot 0 internally for the existing
// PM pane/DB row.
const slotParamSchema = z.union([
  z.literal("pm"),
  z.coerce.number().int().min(1).max(DEFAULT_DEV_SLOT_COUNT),
]);
const requestSchema = z.object({
  expected_epoch: z.number().int().nonnegative(),
  expected_session_id: z.string().min(1).max(255).refine((value) => !/[\u0000-\u001f\u007f]/.test(value)),
  expected_session_started_at: z.string().min(1).max(80),
  expected_age_seconds: z.number().finite().gt(6 * 60 * 60),
  checkout_path: z.string().min(1).max(500).refine((value) => value.startsWith("/")),
  checkout_branch: z.string().min(1).max(255),
  checkout_head: z.string().regex(/^[0-9a-f]{40}$/i),
  request_token: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/),
});

function equalSecret(actual: string | undefined, expected: string): boolean {
  const actualBytes = Buffer.from(actual ?? "", "utf8");
  const expectedBytes = Buffer.from(expected, "utf8");
  if (actualBytes.length !== expectedBytes.length) {
    const padded = Buffer.alloc(expectedBytes.length);
    actualBytes.copy(padded, 0, 0, Math.min(actualBytes.length, padded.length));
    timingSafeEqual(padded, expectedBytes);
    return false;
  }
  return timingSafeEqual(actualBytes, expectedBytes);
}

function hasLocalCapability(authority: string | undefined, capability: string | undefined): boolean {
  const configured = process.env.MOP_LOCAL_CAPABILITY ?? "";
  if (!/^[0-9a-f]{64}$/i.test(configured)) return false;
  return equalSecret(authority, SESSION_CLEAR_AUTHORITY)
    && equalSecret(capability, configured);
}

function responseForReceipt(receipt: SessionClearEffectReceipt, idempotent = true): Response {
  if (receipt.status === "completed") {
    return new Response(JSON.stringify({
      success: true,
      effect: !idempotent,
      idempotent,
      status: receipt.status,
      request_token: receipt.request_token,
    }), { status: 200, headers: { "content-type": "application/json" } });
  }
  return new Response(JSON.stringify({
    success: false,
    effect: false,
    code: "session_clear_effect_ambiguous",
    status: receipt.status,
    request_token: receipt.request_token,
  }), { status: 503, headers: { "content-type": "application/json" } });
}

function requestDigest(slot: number, request: z.infer<typeof requestSchema>): string {
  const canonical = JSON.stringify({ slot, ...request });
  return createHash("sha256").update(canonical, "utf8").digest("hex");
}

function failure(code: string, status = 409): Response {
  return new Response(JSON.stringify({ success: false, effect: false, code }), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** Register the PM-Operator-free, one-shot session-age clear boundary. */
export function registerSessionClearRoute(
  app: Hono,
  dependencies: SessionClearRouteDependencies,
): void {
  const { db, relay, observeCheckout } = dependencies;
  const verifyPane = dependencies.verifyPane ?? verifyPaneIdentity;

  app.post("/slots/:slotNum/session/clear", async (c) => {
    // This is intentionally the first operation: no path parsing, body parsing,
    // slot reads, receipt reservation, or pane work precedes both credentials.
    if (!hasLocalCapability(
      c.req.header(SESSION_CLEAR_AUTHORITY_HEADER),
      c.req.header(SESSION_CLEAR_CAPABILITY_HEADER),
    )) {
      return failure("session_clear_authority_required", 403);
    }
    const slotParse = slotParamSchema.safeParse(c.req.param("slotNum"));
    if (!slotParse.success) return failure("invalid_slot", 400);

    let body: unknown;
    try {
      body = await c.req.json();
    } catch {
      return failure("invalid_request", 400);
    }
    const parsed = requestSchema.safeParse(body);
    if (!parsed.success) return failure("invalid_request", 400);
    const request = parsed.data;
    const slot = slotParse.data === "pm" ? PM_SLOT : slotParse.data;
    const digest = requestDigest(slot, request);
    const sessionStartedMs = Date.parse(request.expected_session_started_at);
    if (!Number.isFinite(sessionStartedMs) || Date.now() - sessionStartedMs <= 6 * 60 * 60 * 1000) {
      return failure("session_age_not_due");
    }

    // Reconcile a terminal receipt before consulting mutable slot/checkout
    // state.  A response-loss replay must be effect-free even after the slot
    // has advanced to a later epoch; a reused token with different bytes is
    // still rejected by the digest binding.
    const existing = db.getSessionClearEffect(request.request_token);
    if (existing) {
      if (existing.request_digest !== digest) return failure("session_clear_request_conflict");
      if (existing.status !== "reserved") return responseForReceipt(existing);
    }

    const identity = await verifyPane(slot);
    if (!identity.ok || identity.snapshot.currentPath !== request.checkout_path) {
      return failure("pane_or_checkout_identity_mismatch");
    }
    const checkout = await observeCheckout(request.checkout_path);
    if (
      checkout.checkout_path !== request.checkout_path
      || checkout.branch !== request.checkout_branch
      || checkout.head?.toLowerCase() !== request.checkout_head.toLowerCase()
      || checkout.clean !== true
      || checkout.unpushed_commits.length !== 0
    ) {
      return failure("checkout_state_drift");
    }

    const current = db.getSlot(slot);
    if (
      !current
      || current.assignment_epoch !== request.expected_epoch
      || current.session_id !== request.expected_session_id
      || current.session_started_at !== request.expected_session_started_at
      || current.occupied
      || current.dnd
      || !current.idle
      || current.active_turn_id !== null
      || current.active_turn_state !== "inactive"
    ) {
      return failure("session_state_drift");
    }

    const input: SessionClearEffectInput = {
      request_token: request.request_token,
      request_digest: digest,
      slot,
      expected_epoch: request.expected_epoch,
      expected_session_id: request.expected_session_id,
      expected_session_started_at: request.expected_session_started_at,
      pane_id: identity.snapshot.paneId,
      checkout_path: request.checkout_path,
      checkout_branch: request.checkout_branch,
      checkout_head: request.checkout_head.toLowerCase(),
    };
    const receipt = db.reserveSessionClearEffect(input);
    if (!receipt) return failure("session_clear_request_conflict");
    if (receipt.status !== "reserved") return responseForReceipt(receipt);

    let sent = false;
    let startedByThisRequest = false;
    try {
      sent = await relay.sendToSlotAsync(slot, "/clear", true, false, {
        noRetry: true,
        // The final pane and checkout reads are the last awaited preparation.
        // The synchronous receipt/slot fence below follows immediately before
        // relay's first load-buffer effect.
        prepareBeforeFirstEffect: async (initialPane) => {
          const finalPane = await verifyPane(slot);
          if (
            !finalPane.ok
            || finalPane.snapshot.paneId !== initialPane.paneId
            || finalPane.snapshot.currentPath !== input.checkout_path
          ) return null;
          const finalCheckout = await observeCheckout(input.checkout_path);
          if (
            finalCheckout.checkout_path !== input.checkout_path
            || finalCheckout.branch !== input.checkout_branch
            || finalCheckout.head?.toLowerCase() !== input.checkout_head.toLowerCase()
            || finalCheckout.clean !== true
            || finalCheckout.unpushed_commits.length !== 0
          ) return null;
          return finalPane.snapshot;
        },
        beforeFirstEffect: (pane) => {
          if (pane.paneId !== input.pane_id) return false;
          const started = db.startSessionClearEffect(input);
          if (!started || started.receipt.status !== "started") return false;
          startedByThisRequest = started.started;
          return startedByThisRequest;
        },
      });
    } catch {
      sent = false;
    }
    const terminal = db.getSessionClearEffect(request.request_token);
    if (!terminal) return failure("session_clear_receipt_missing", 503);
    if (terminal.status === "reserved") return failure("session_clear_final_fence_refused");
    if (terminal.status === "started" && startedByThisRequest && !sent) {
      const ambiguous = db.finishSessionClearEffect(request.request_token, digest, "ambiguous");
      return ambiguous ? responseForReceipt(ambiguous) : failure("session_clear_effect_ambiguous", 503);
    }
    if (terminal.status === "ambiguous" || !sent) return responseForReceipt(terminal);
    if (terminal.status === "started" && !startedByThisRequest) return responseForReceipt(terminal);
    const completed = db.finishSessionClearEffect(request.request_token, digest, "completed");
    return completed
      ? responseForReceipt(completed, false)
      : failure("session_clear_receipt_missing", 503);
  });
}

/** Keep retired broad clear aliases effect-free and body-free. */
export function registerRetiredClearRefusals(app: Hono): void {
  const response = () => new Response(JSON.stringify({
    success: false,
    effect: false,
    code: "session_clear_exact_route_required",
  }), { status: 410, headers: { "content-type": "application/json" } });
  app.post("/slots/:slotNum/clear", response);
  app.post("/clear", response);
}
