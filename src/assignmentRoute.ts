import type { Hono } from "hono";
import { z } from "zod";

import {
  isPmTransitionAssignmentRequest,
  PM_TRANSITION_ASSIGNMENT_HEADER,
} from "./assignmentAuthority.js";
import {
  type AssignmentTupleInput,
  type MoPDatabase,
} from "./db.js";
import { DEFAULT_DEV_SLOT_COUNT } from "./slotConfig.js";

const assignmentSlotParamSchema = z.coerce.number().int().min(1).max(DEFAULT_DEV_SLOT_COUNT);

const COMPLETE_REBIND_EXPECTED_FIELDS = [
  "expected_current_repository_id",
  "expected_current_issue",
  "expected_current_pr",
  "expected_current_branch",
  "expected_current_head_sha",
  "expected_current_work_kind",
  "expected_current_handoff_id",
  "expected_current_claimed_at",
] as const;

const COMPLETE_REBIND_DESIRED_FIELDS = [
  "repository_id",
  "issue",
  "pr",
  "branch",
  "head_sha",
  "work_kind",
  "handoff_id",
  "claimed_at",
] as const;

function hasOwn(body: Record<string, unknown>, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(body, key);
}

function hasEvery(body: Record<string, unknown>, fields: readonly string[]): boolean {
  return fields.every((field) => hasOwn(body, field));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function completeTuple(
  body: Record<string, unknown>,
  prefix = "",
): AssignmentTupleInput {
  return {
    repository_id: body[`${prefix}repository_id`] as string | number | null,
    issue: body[`${prefix}issue`] as number | null,
    pr: body[`${prefix}pr`] as number | null,
    branch: body[`${prefix}branch`] as string | null,
    head_sha: body[`${prefix}head_sha`] as string | null,
    work_kind: body[`${prefix}work_kind`] as string | null,
    handoff_id: body[`${prefix}handoff_id`] as string | null,
    claimed_at: body[`${prefix}claimed_at`] as string | null,
  };
}

export function registerAssignmentRoute(app: Hono, db: MoPDatabase): void {
  app.post("/slots/:slotNum/assign", async (c) => {
    const slotParse = assignmentSlotParamSchema.safeParse(c.req.param("slotNum"));
    if (!slotParse.success) {
      return c.json({ error: "Invalid slot number" }, 400);
    }
    if (!isPmTransitionAssignmentRequest(
      c.req.header(PM_TRANSITION_ASSIGNMENT_HEADER)
    )) {
      return c.json({
        success: false,
        conflict: true,
        error: "assignment authority is required",
        reason: "assignment_authority_required",
      }, 403);
    }

    const body = await c.req.json();
    if (!isRecord(body) || !Number.isInteger(body.issue) || Number(body.issue) <= 0) {
      return c.json({
        success: false,
        conflict: true,
        error: "positive issue is required",
        reason: "invalid_issue",
      }, 409);
    }
    const repositoryId = (
      typeof body.repository_id === "string" || typeof body.repository_id === "number"
    ) ? body.repository_id : (process.env.MOP_LEGACY_REPOSITORY_ID ?? "heydonna-app/heydonna-app");
    const result = db.assignIssueToSlot(
      slotParse.data,
      body.issue as number,
      typeof body.task === "string" ? body.task : "",
      repositoryId,
    );

    if (!result.ok) {
      return c.json({ success: false, ...result }, 409);
    }

    db.logEvent(slotParse.data, "slot_assigned", null, null, {
      issue: body.issue,
      assignment_epoch: result.assignment_epoch,
      idempotent: result.idempotent,
    });

    const updated = db.getSlot(slotParse.data);
    return c.json(updated);
  });

  app.post("/slots/:slotNum/adopt-issue-claim", async (c) => {
    const slotParse = assignmentSlotParamSchema.safeParse(c.req.param("slotNum"));
    if (!slotParse.success) {
      return c.json({ error: "Invalid slot number" }, 400);
    }
    if (!isPmTransitionAssignmentRequest(
      c.req.header(PM_TRANSITION_ASSIGNMENT_HEADER)
    )) {
      return c.json({
        success: false,
        conflict: true,
        error: "assignment authority is required",
        reason: "assignment_authority_required",
      }, 403);
    }

    const body = await c.req.json();
    if (!isRecord(body)) {
      return c.json({
        success: false,
        conflict: true,
        error: "complete expected and desired assignment tuples are required",
        reason: "observed_tuple_mismatch",
      }, 409);
    }
    if (!Number.isInteger(body.expected_epoch)) {
      return c.json({
        success: false,
        conflict: true,
        error: "expected_epoch is required and must be an integer",
      }, 409);
    }
    if (!hasEvery(body, COMPLETE_REBIND_EXPECTED_FIELDS)) {
      return c.json({
        success: false,
        conflict: true,
        error: "complete expected assignment tuple is required",
        reason: "observed_tuple_mismatch",
      }, 409);
    }
    if (!hasEvery(body, COMPLETE_REBIND_DESIRED_FIELDS)) {
      return c.json({
        success: false,
        conflict: true,
        error: "complete desired assignment tuple is required",
        reason: "observed_tuple_mismatch",
      }, 409);
    }
    const expectedTuple = completeTuple(body, "expected_current_");
    const desiredTuple = completeTuple(body);
    const result = db.rebindSlot(
      slotParse.data,
      body.expected_epoch as number,
      expectedTuple,
      desiredTuple,
      typeof body.task === "string" ? body.task : null,
    );
    if (!result.ok) {
      return c.json({ success: false, ...result }, 409);
    }

    if (!result.idempotent) {
      db.logEvent(slotParse.data, "slot_issue_claim_adopted", null, null, {
        ...body,
        rebind: true,
        assignment_epoch: result.assignment_epoch,
        idempotent: false,
      });
    }
    return c.json(db.getSlot(slotParse.data));
  });
}
