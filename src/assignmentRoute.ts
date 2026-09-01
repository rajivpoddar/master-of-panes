import type { Hono } from "hono";
import { z } from "zod";

import {
  normalizeBranchIdentity,
  normalizeRepositoryId,
  type AssignmentTupleInput,
  type MoPDatabase,
} from "./db.js";
import { DEFAULT_DEV_SLOT_COUNT } from "./slotConfig.js";

const assignmentSlotParamSchema = z.coerce.number().int().min(1).max(DEFAULT_DEV_SLOT_COUNT);

// The historical /assign caller is intentionally issue-only. Once any
// extended identity field is supplied, the request is a complete claim and
// must not be silently downgraded to assignIssueToSlot.
const COMPLETE_ASSIGN_FIELDS = [
  "expected_epoch",
  "repository_id",
  "issue",
  "pr",
  "branch",
  "head_sha",
  "work_kind",
  "handoff_id",
  "task",
] as const;

const COMPLETE_ASSIGN_DISCRIMINATORS = [
  "expected_epoch",
  "pr",
  "branch",
  "branch_ref",
  "head_sha",
  "work_kind",
  "handoff_id",
  "claimed_at",
] as const;

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

function hasCompleteAssignmentValues(body: Record<string, unknown>): boolean {
  return normalizeRepositoryId(body.repository_id) !== null
    && Number.isInteger(body.issue) && Number(body.issue) > 0
    // A complete issue-only claim deliberately carries an explicit null PR.
    // Omitting pr still fails the complete-field presence check, while a
    // PR-bearing claim must remain a positive integer.
    && (body.pr === null || (Number.isInteger(body.pr) && Number(body.pr) > 0))
    && typeof body.branch === "string"
    && normalizeBranchIdentity(body.branch) !== null
    && typeof body.head_sha === "string"
    && /^[0-9a-f]{40}$/i.test(body.head_sha)
    && typeof body.work_kind === "string"
    && body.work_kind.trim() !== ""
    && typeof body.handoff_id === "string"
    && body.handoff_id.trim() !== ""
    && typeof body.task === "string"
    && body.task.trim() !== "";
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
    const body = await c.req.json();
    if (!isRecord(body) || !Number.isInteger(body.issue) || Number(body.issue) <= 0) {
      return c.json({
        success: false,
        conflict: true,
        error: "positive issue is required",
        reason: "invalid_issue",
      }, 409);
    }
    const completeRequested = COMPLETE_ASSIGN_DISCRIMINATORS.some((field) => hasOwn(body, field));
    const task = typeof body.task === "string" ? body.task : "";
    let result;
    if (completeRequested) {
      if (!hasEvery(body, COMPLETE_ASSIGN_FIELDS) || !hasCompleteAssignmentValues(body)) {
        return c.json({
          success: false,
          conflict: true,
          error: "complete assignment tuple is required",
          reason: "observed_tuple_mismatch",
        }, 409);
      }
      result = db.assignSlot(
        slotParse.data,
        task,
        body.repository_id as string | number | null,
        body.issue as number,
        body.branch as string | null,
        body.pr as number | null,
        body.head_sha as string | null,
        body.expected_epoch as number,
        body.work_kind as string | null,
        body.handoff_id as string | null,
        true,
      );
    } else {
      const repositoryId = (
        typeof body.repository_id === "string" || typeof body.repository_id === "number"
      ) ? body.repository_id : (process.env.MOP_LEGACY_REPOSITORY_ID ?? "heydonna-app/heydonna-app");
      result = db.assignIssueToSlot(
        slotParse.data,
        body.issue as number,
        task,
        repositoryId,
      );
    }

    if (!result.ok) {
      return c.json({ success: false, ...result }, 409);
    }

    db.logEvent(slotParse.data, "slot_assigned", null, null, {
      issue: body.issue,
      assignment_epoch: result.assignment_epoch,
      idempotent: result.idempotent,
      assignment_mode: completeRequested ? "complete" : "issue-only",
      ...(completeRequested ? {
        repository_id: body.repository_id,
        pr: body.pr,
        branch: body.branch,
        head_sha: body.head_sha,
        work_kind: body.work_kind,
        handoff_id: body.handoff_id,
      } : {}),
    });

    const updated = db.getSlot(slotParse.data);
    const expectedBranch = completeRequested && typeof body.branch === "string"
      ? normalizeBranchIdentity(body.branch)?.branch
      : null;
    const expectedWorkKind = completeRequested && typeof body.work_kind === "string"
      ? body.work_kind.trim()
      : null;
    const expectedHandoffId = completeRequested && typeof body.handoff_id === "string"
      ? body.handoff_id.trim()
      : null;
    if (completeRequested && (
      !updated
      || !updated.occupied
      || updated.assignment_epoch !== result.assignment_epoch
      || updated.task !== task
      || updated.repository_id !== normalizeRepositoryId(body.repository_id)
      || updated.issue !== body.issue
      || updated.pr !== body.pr
      || updated.branch !== expectedBranch
      || updated.head_sha !== body.head_sha
      || updated.work_kind !== expectedWorkKind
      || updated.handoff_id !== expectedHandoffId
    )) {
      return c.json({
        success: false,
        conflict: true,
        error: "complete assignment readback is not durable",
        reason: "observed_tuple_mismatch",
      }, 409);
    }
    return c.json(updated);
  });

  app.post("/slots/:slotNum/adopt-issue-claim", async (c) => {
    const slotParse = assignmentSlotParamSchema.safeParse(c.req.param("slotNum"));
    if (!slotParse.success) {
      return c.json({ error: "Invalid slot number" }, 400);
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
