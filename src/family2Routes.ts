import type { Hono } from "hono";
import { z } from "zod";

import {
  isPmTransitionAssignmentRequest,
  PM_TRANSITION_ASSIGNMENT_HEADER,
} from "./assignmentAuthority.js";
import type { MoPDatabase } from "./db.js";
import type {
  NativeSlotNoPaneReleaseRequest,
  NativeSlotReleaseCoordinator,
  NativeSlotReleaseRequest,
} from "./slotRelease.js";
import { DEFAULT_DEV_SLOT_COUNT } from "./slotConfig.js";

const slotParamSchema = z.coerce.number().int().min(1).max(DEFAULT_DEV_SLOT_COUNT);

export interface Family2RouteDependencies {
  db: MoPDatabase;
  nativeSlotRelease: NativeSlotReleaseCoordinator;
}

function authorized(authority: string | undefined): boolean {
  return isPmTransitionAssignmentRequest(authority);
}

/** Register the authenticated native release and Family-2 consumer boundary. */
export function registerFamily2Routes(
  app: Hono,
  dependencies: Family2RouteDependencies,
): void {
  const { db, nativeSlotRelease } = dependencies;

  app.post("/slots/:slotNum/release", async (c) => {
    // Authenticate before path/body processing, delivery/reset, or any DB use.
    if (!authorized(c.req.header(PM_TRANSITION_ASSIGNMENT_HEADER))) {
      return c.json({ success: false, code: "assignment_authority_required" }, 403);
    }
    const slotParse = slotParamSchema.safeParse(c.req.param("slotNum"));
    if (!slotParse.success) return c.json({ error: "Invalid slot number" }, 400);

    let body: Record<string, unknown> = {};
    try {
      body = await c.req.json();
    } catch {
      body = {};
    }
    const request: NativeSlotReleaseRequest = {
      slot: slotParse.data,
      expected_epoch: body.expected_epoch as number,
      expected_tuple: {
        repository_id: body.expected_repository_id as string | number | null,
        issue: body.expected_issue as number | null,
        pr: body.expected_pr as number | null,
        branch: body.expected_branch as string | null,
        head_sha: body.expected_head_sha as string | null,
        work_kind: body.expected_work_kind as string | null,
        handoff_id: body.expected_handoff_id as string | null,
        claimed_at: body.expected_claimed_at as string | null,
      },
      intended_main_head: body.intended_main_head as string,
      effect_id: body.effect_id as string | undefined,
      request_digest: body.request_digest as string | undefined,
      release_mode: body.release_mode as NativeSlotReleaseRequest["release_mode"],
    };
    const releaseResult = await nativeSlotRelease.release(request);
    if (releaseResult.success) {
      if (!releaseResult.idempotent) {
        db.logEvent(slotParse.data, "slot_released", null, null, {
          assignment_epoch: releaseResult.assignment_epoch,
          native_checkout_ack: true,
        });
      }
      return c.json(releaseResult);
    }
    return c.json(releaseResult, releaseResult.code === "invalid_request" ? 400 : 409);
  });

  app.post("/slots/:slotNum/release-no-pane", async (c) => {
    // This is an explicit stale-completed-lease boundary.  It is deliberately
    // separate from the legacy release route, which must retain its pane
    // delivery/reset semantics for existing callers.
    if (!authorized(c.req.header(PM_TRANSITION_ASSIGNMENT_HEADER))) {
      return c.json({ success: false, code: "assignment_authority_required" }, 403);
    }
    const slotParse = slotParamSchema.safeParse(c.req.param("slotNum"));
    if (!slotParse.success) return c.json({ error: "Invalid slot number" }, 400);
    let body: Record<string, unknown> = {};
    try {
      body = await c.req.json();
    } catch {
      body = {};
    }
    const request: NativeSlotNoPaneReleaseRequest = {
      slot: slotParse.data,
      expected_epoch: body.expected_epoch as number,
      expected_tuple: {
        repository_id: body.expected_repository_id as string | number | null,
        issue: body.expected_issue as number | null,
        pr: body.expected_pr as number | null,
        branch: body.expected_branch as string | null,
        head_sha: body.expected_head_sha as string | null,
        work_kind: body.expected_work_kind as string | null,
        handoff_id: body.expected_handoff_id as string | null,
        claimed_at: body.expected_claimed_at as string | null,
      },
      expected_task: body.expected_task as string,
      checkout_path: body.checkout_path as string,
      effect_id: body.effect_id as string,
      request_digest: body.request_digest as string,
    };
    const releaseResult = await nativeSlotRelease.releaseWithoutPane(request);
    return c.json(
      releaseResult,
      releaseResult.code === "invalid_request" ? 400 : releaseResult.success ? 200 : 409,
    );
  });

  app.get("/slots/:slotNum/release-receipt", (c) => {
    // Receipt reconciliation is authenticated too; it exposes ownership history.
    if (!authorized(c.req.header(PM_TRANSITION_ASSIGNMENT_HEADER))) {
      return c.json({ success: false, code: "assignment_authority_required" }, 403);
    }
    const slotParse = slotParamSchema.safeParse(c.req.param("slotNum"));
    if (!slotParse.success) return c.json({ success: false, code: "invalid_request" }, 400);
    const effectId = c.req.query("effect_id");
    if (!effectId) return c.json({ success: false, code: "invalid_request" }, 400);
    try {
      const receipt = db.getNativeReleaseEffectReceipt(effectId);
      if (!receipt || receipt.slot !== slotParse.data) {
        return c.json({ success: false, code: "effect_receipt_not_found" }, 404);
      }
      return c.json({ success: true, ...receipt });
    } catch {
      return c.json({ success: false, code: "effect_receipt_malformed" }, 500);
    }
  });

}
