import type { Hono } from "hono";
import { z } from "zod";

import {
  isPmTransitionAssignmentRequest,
  PM_TRANSITION_ASSIGNMENT_HEADER,
} from "./assignmentAuthority.js";
import type { AssignmentTupleInput, MoPDatabase } from "./db.js";

const assignmentSlotParamSchema = z.coerce.number().int().min(0).max(4);

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
    if (!Number.isInteger(body.expected_epoch)) {
      return c.json({
        success: false,
        conflict: true,
        error: "expected_epoch is required and must be an integer",
      }, 409);
    }
    const rawPr = body.pr ?? null;
    const pr = rawPr === null ? null : Number(rawPr);
    const rawIssue = body.issue ?? null;
    const issue = rawIssue === null ? null : Number(rawIssue);
    const result = db.assignSlot(
      slotParse.data,
      body.task ?? "",
      body.repository_id ?? null,
      typeof issue === "number" && Number.isInteger(issue) && issue > 0
        ? issue
        : null,
      body.branch ?? null,
      body.session_id ?? null,
      Number.isInteger(pr) ? pr : null,
      body.head_sha ?? null,
      body.expected_epoch,
      false,
      null,
      body.work_kind ?? null,
      body.handoff_id ?? null,
    );

    if (!result.ok) {
      return c.json({ success: false, ...result }, 409);
    }

    db.logEvent(slotParse.data, "slot_assigned", null, null, {
      ...body,
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
    if (!Number.isInteger(body.expected_epoch)) {
      return c.json({
        success: false,
        conflict: true,
        error: "expected_epoch is required and must be an integer",
      }, 409);
    }
    const hasCompleteExpected = COMPLETE_REBIND_EXPECTED_FIELDS.some((field) => hasOwn(body, field));
    if (hasCompleteExpected && !hasEvery(body, COMPLETE_REBIND_EXPECTED_FIELDS)) {
      return c.json({
        success: false,
        conflict: true,
        error: "complete expected assignment tuple is required",
        reason: "observed_tuple_mismatch",
      }, 409);
    }
    if (hasCompleteExpected && !hasEvery(body, COMPLETE_REBIND_DESIRED_FIELDS)) {
      return c.json({
        success: false,
        conflict: true,
        error: "complete desired assignment tuple is required",
        reason: "observed_tuple_mismatch",
      }, 409);
    }
    if (hasCompleteExpected) {
      const expectedTuple: AssignmentTupleInput = {
        repository_id: body.expected_current_repository_id as string | number | null,
        issue: body.expected_current_issue as number | null,
        pr: body.expected_current_pr as number | null,
        branch: body.expected_current_branch as string | null,
        head_sha: body.expected_current_head_sha as string | null,
        work_kind: body.expected_current_work_kind as string | null,
        handoff_id: body.expected_current_handoff_id as string | null,
        claimed_at: body.expected_current_claimed_at as string | null,
      };
      const desiredTuple: AssignmentTupleInput = {
        repository_id: body.repository_id as string | number | null,
        issue: body.issue as number | null,
        pr: body.pr as number | null,
        branch: body.branch as string | null,
        head_sha: body.head_sha as string | null,
        work_kind: body.work_kind as string | null,
        handoff_id: body.handoff_id as string | null,
        claimed_at: body.claimed_at as string | null,
      };
      const result = db.rebindSlot(
        slotParse.data,
        body.expected_epoch,
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
    }
    if (
      !Object.prototype.hasOwnProperty.call(body, "expected_current_pr")
      || !Object.prototype.hasOwnProperty.call(body, "expected_current_branch_ref")
      || !Object.prototype.hasOwnProperty.call(body, "expected_current_head_sha")
      || !(
        body.expected_current_pr === null
        || (
          Number.isInteger(body.expected_current_pr)
          && Number(body.expected_current_pr) > 0
        )
      )
      || typeof body.expected_current_branch_ref !== "string"
      || !body.expected_current_branch_ref.startsWith("refs/heads/")
      || !(
        body.expected_current_head_sha === null
        || (
          typeof body.expected_current_head_sha === "string"
          && /^[0-9a-f]{40}$/i.test(body.expected_current_head_sha)
        )
      )
    ) {
      return c.json({
        success: false,
        conflict: true,
        error: "observed current tuple is required",
        reason: "observed_tuple_mismatch",
      }, 409);
    }
    const result = db.adoptIssueClaimSlot(
      slotParse.data,
      body.task ?? "",
      body.repository_id ?? null,
      Number(body.issue),
      body.branch ?? null,
      Number(body.pr),
      body.head_sha ?? null,
      body.expected_current_pr ?? null,
      body.expected_current_branch_ref,
      body.expected_current_head_sha ?? null,
      body.expected_epoch
    );
    if (!result.ok) {
      return c.json({ success: false, ...result }, 409);
    }

    db.logEvent(slotParse.data, "slot_issue_claim_adopted", null, null, {
      ...body,
      assignment_epoch: result.assignment_epoch,
      idempotent: result.idempotent,
    });
    return c.json(db.getSlot(slotParse.data));
  });
}
