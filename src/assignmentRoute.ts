import type { Hono } from "hono";
import { z } from "zod";

import {
  isPmTransitionAssignmentRequest,
  PM_TRANSITION_ASSIGNMENT_HEADER,
} from "./assignmentAuthority.js";
import type { MoPDatabase } from "./db.js";

const assignmentSlotParamSchema = z.coerce.number().int().min(0).max(4);

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
      body.expected_epoch
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
