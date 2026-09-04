import type { Hono } from "hono";
import { z } from "zod";

import type { MoPDatabase } from "./db.js";
import { DEFAULT_DEV_SLOT_COUNT } from "./slotConfig.js";

export type AssignmentDelivery = (slot: number, task: string) => Promise<boolean>;

const assignmentSlotParamSchema = z.coerce.number().int().min(1).max(DEFAULT_DEV_SLOT_COUNT);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Assign one free numbered slot with the minimum PM-authored contract. */
export function registerAssignmentRoute(
  app: Hono,
  db: MoPDatabase,
  deliverAssignment: AssignmentDelivery = async () => true,
): void {
  app.post("/slots/:slotNum/assign", async (c) => {
    const slotParse = assignmentSlotParamSchema.safeParse(c.req.param("slotNum"));
    if (!slotParse.success) {
      return c.json({ success: false, reason: "invalid_slot" }, 400);
    }

    const body = await c.req.json().catch(() => null);
    if (
      !isRecord(body)
      || !Number.isInteger(body.issue)
      || Number(body.issue) <= 0
      || typeof body.task !== "string"
      || body.task.trim() === ""
    ) {
      return c.json({ success: false, reason: "invalid_assignment" }, 400);
    }
    const task = body.task;

    const repositoryId = (
      typeof body.repository_id === "string" || typeof body.repository_id === "number"
    ) ? body.repository_id : (process.env.MOP_LEGACY_REPOSITORY_ID ?? "heydonna-app/heydonna-app");
    const result = db.assignIssueToSlot(
      slotParse.data,
      Number(body.issue),
      task,
      repositoryId,
    );
    if (!result.ok) {
      return c.json({ success: false, reason: result.reason }, 409);
    }

    if (result.idempotent) {
      return c.json({
        success: false,
        assigned: true,
        idempotent: true,
        delivery_verified: false,
        reason: "assignment_delivery_already_reconciled",
        slot: db.getSlot(slotParse.data),
      }, 409);
    }

    db.logEvent(slotParse.data, "slot_assigned", null, null, {
      issue: Number(body.issue),
      assignment_epoch: result.assignment_epoch,
    });

    let delivered = false;
    try {
      delivered = await deliverAssignment(slotParse.data, task);
    } catch {
      delivered = false;
    }
    if (!delivered) {
      db.logEvent(slotParse.data, "slot_assignment_delivery_failed", null, null, {
        assignment_epoch: result.assignment_epoch,
        reason: "assignment_delivery_failed",
      });
      return c.json({
        success: false,
        assigned: true,
        delivery_verified: false,
        reason: "assignment_delivery_failed",
        assignment_epoch: result.assignment_epoch,
        slot: db.getSlot(slotParse.data),
      }, 502);
    }

    return c.json({
      ...db.getSlot(slotParse.data),
      success: true,
      assigned: true,
      delivery_verified: true,
      assignment_epoch: result.assignment_epoch,
    });
  });
}
